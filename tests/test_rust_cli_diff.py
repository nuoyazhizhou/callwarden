"""Rust `cw` 生产命令与 Python 真相源的差分测试。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from callwarden.db.db import CodeGraphDB


def _rust_cw_binary() -> Path:
    override = os.environ.get("CW_RUST_CLI_BIN")
    if override:
        return Path(override)
    suffix = ".exe" if os.name == "nt" else ""
    return PROJECT_ROOT / "rust_ext" / "target" / "debug" / f"cw{suffix}"


def _run_rust_config(binary: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary), "config", *args],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _run_python_config(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "cw.py"), "config", *args],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _normalize_config_source(output: str) -> str:
    lines = output.splitlines()
    if lines and lines[0].startswith("# N4 ") and "（来源：" in lines[0]:
        lines[0] = lines[0].split("（来源：", 1)[0] + "（来源：<implementation>）"
    return "\n".join(lines)


def _seed_stats_fixture(db: CodeGraphDB) -> int:
    workspace_id = db._get_active_workspace_id()
    now = time.time()
    conn = db.conn
    conn.execute(
        "INSERT INTO file_contents(content_hash, language, total_lines, first_seen_at) "
        "VALUES ('file-a', 'python', 10, ?), ('file-b', 'rust', 20, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO file_instances("
        "workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, "
        "last_parsed, status, module_path"
        ") VALUES (?, 'a.py', ?, 'file-a', ?, 10, ?, 'active', 'a')",
        (workspace_id, str(Path(db.workspace_root) / "a.py"), now, now),
    )
    file_id = conn.execute(
        "SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = 'a.py'",
        (workspace_id,),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO symbol_contents("
        "content_hash, name, kind, content, signature, has_comment, "
        "comment_content, qualified_name"
        ") VALUES "
        "('sym-a', 'alpha', 'fn', 'def alpha(): pass', 'alpha()', 1, "
        "'alpha docs', 'a.alpha'),"
        "('sym-b', 'Thing', 'struct', 'struct Thing {}', '', 0, '', 'a.Thing')"
    )
    conn.execute(
        "INSERT INTO symbols("
        "file_instance_id, symbol_hash, name, kind, start_line, end_line, "
        "has_comment, comment_status, qualified_name, depth"
        ") VALUES "
        "(?, 'sym-a', 'alpha', 'fn', 1, 2, 1, 'done', 'a.alpha', 0),"
        "(?, 'sym-b', 'Thing', 'struct', 4, 5, 0, 'pending', 'a.Thing', -1)",
        (file_id, file_id),
    )
    alpha_id = conn.execute(
        "SELECT id FROM symbols WHERE file_instance_id = ? AND symbol_hash = 'sym-a'",
        (file_id,),
    ).fetchone()["id"]
    thing_id = conn.execute(
        "SELECT id FROM symbols WHERE file_instance_id = ? AND symbol_hash = 'sym-b'",
        (file_id,),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO calls("
        "caller_id, caller_name, caller_module, callee_name, callee_qualified, "
        "callee_id, call_line, is_cross_file"
        ") VALUES (?, 'alpha', 'a', 'Thing', 'a.Thing', ?, 2, 1)",
        (alpha_id, thing_id),
    )
    conn.execute(
        "INSERT INTO file_versions("
        "file_instance_id, version_num, content_hash, mtime, total_lines, parsed_at, is_current"
        ") VALUES (?, 1, 'file-a', ?, 10, ?, 0), (?, 2, 'file-a', ?, 10, ?, 1)",
        (file_id, now - 10, now - 10, file_id, now, now),
    )
    current_version = conn.execute(
        "SELECT id FROM file_versions WHERE file_instance_id = ? AND is_current = 1",
        (file_id,),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO file_symbol_versions("
        "file_version_id, symbol_hash, qualified_name, start_line, end_line, "
        "module_path, depth"
        ") VALUES "
        "(?, 'sym-a', 'a.alpha', 1, 2, 'a', 0), "
        "(?, 'sym-b', 'a.Thing', 4, 5, 'a', -1)",
        (current_version, current_version),
    )
    conn.execute(
        "INSERT INTO call_versions("
        "file_version_id, caller_qualified, caller_hash, callee_name, "
        "callee_module, callee_qualified, callee_file, call_line, is_cross_file"
        ") VALUES "
        "(?, 'a.alpha', 'sym-a', 'Thing', 'a', 'a.Thing', 'a.py', 2, 1), "
        "(?, 'a.Thing', 'sym-b', 'alpha', 'a', 'a.alpha', 'a.py', 5, 0)",
        (current_version, current_version),
    )
    conn.execute(
        "INSERT INTO semgrep_findings("
        "file_instance_id, content_hash, rule_id, rule_name, message, severity, "
        "confidence, start_line, end_line, snippet, fix, symbol_qualified"
        ") VALUES (?, 'file-a', 'python.eval', 'eval use', 'avoid eval', "
        "'ERROR', 'HIGH', 2, 2, 'eval(x)', 'use parser', 'a.alpha')",
        (file_id,),
    )
    conn.execute(
        "INSERT INTO guardrail_rules("
        "rule_id, category, severity, pattern, action, description, is_builtin, created_at"
        ") VALUES ('guard.db', 'db_safety', 'warn', 'execute', 'warn', "
        "'unsafe SQL', 0, ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO guardrail_findings("
        "rule_id, file_path, symbol_hash, severity, status, message, detected_at"
        ") VALUES ('guard.db', 'a.py', 'sym-a', 'warn', 'open', 'unsafe SQL', ?)",
        (now,),
    )
    conn.commit()
    return workspace_id


def _seed_status_files(db: CodeGraphDB, workspace_id: int) -> None:
    """构造 synced/new/stale/deleted 以及三类 ignore 文件。"""
    root = Path(db.workspace_root)
    tracked_mtime = db.conn.execute(
        "SELECT mtime FROM file_instances "
        "WHERE workspace_id = ? AND rel_path = 'a.py'",
        (workspace_id,),
    ).fetchone()["mtime"]
    synced = root / "a.py"
    synced.write_text("def alpha():\n    pass\n", encoding="utf-8")
    os.utime(synced, (tracked_mtime, tracked_mtime))

    (root / "new.py").write_text("value = 1\n", encoding="utf-8")
    stale = root / "stale.rs"
    stale.write_text("fn stale() {}\n", encoding="utf-8")
    stale_mtime = stale.stat().st_mtime

    (root / "target").mkdir()
    (root / "target" / "generated.py").write_text("", encoding="utf-8")
    (root / "custom").mkdir()
    (root / "custom" / "ignored.py").write_text("", encoding="utf-8")
    (root / "assets").mkdir()
    (root / "assets" / "bundle.min.js").write_text("", encoding="utf-8")
    (root / ".callwardenignore").write_text("custom/\n", encoding="utf-8")

    now = time.time()
    db.conn.execute(
        "INSERT INTO file_contents(content_hash, language, total_lines, first_seen_at) "
        "VALUES ('status-stale', 'rust', 1, ?), "
        "('status-deleted', 'go', 1, ?)",
        (now, now),
    )
    db.conn.execute(
        "INSERT INTO file_instances("
        "workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, "
        "last_parsed, status, module_path"
        ") VALUES "
        "(?, 'stale.rs', ?, 'status-stale', ?, 1, ?, 'active', 'stale'), "
        "(?, 'deleted.go', ?, 'status-deleted', ?, 1, ?, 'active', 'deleted')",
        (
            workspace_id,
            str(stale),
            stale_mtime - 100,
            now - 20,
            workspace_id,
            str(root / "deleted.go"),
            now - 100,
            now - 10,
        ),
    )
    db.conn.commit()


def _seed_issues_tests_fixture(db: CodeGraphDB, workspace_id: int) -> None:
    """补充 issues/tests 正向、反向和 history 的真实关系数据。"""
    conn = db.conn
    file_id = conn.execute(
        "SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = 'a.py'",
        (workspace_id,),
    ).fetchone()["id"]
    alpha_id = conn.execute(
        "SELECT id FROM symbols WHERE file_instance_id = ? AND qualified_name = 'a.alpha'",
        (file_id,),
    ).fetchone()["id"]
    thing_id = conn.execute(
        "SELECT id FROM symbols WHERE file_instance_id = ? AND qualified_name = 'a.Thing'",
        (file_id,),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO symbol_contents("
        "content_hash, name, kind, content, signature, has_comment, "
        "comment_content, qualified_name"
        ") VALUES ('sym-test-alpha', 'test_alpha', 'test_fn', "
        "'def test_alpha(): pass', 'test_alpha()', 0, '', 'a.test_alpha')"
    )
    conn.execute(
        "INSERT INTO symbols("
        "file_instance_id, symbol_hash, name, kind, start_line, end_line, "
        "has_comment, comment_status, qualified_name, depth"
        ") VALUES (?, 'sym-test-alpha', 'test_alpha', 'test_fn', 7, 9, "
        "0, 'pending', 'a.test_alpha', 0)",
        (file_id,),
    )
    test_id = conn.execute(
        "SELECT id FROM symbols WHERE file_instance_id = ? "
        "AND qualified_name = 'a.test_alpha'",
        (file_id,),
    ).fetchone()["id"]
    detected_at = 1_735_689_600.0
    conn.executemany(
        "INSERT INTO test_case_relations("
        "workspace_id, test_fn_id, tested_fn_id, match_method, confidence, detected_at"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                workspace_id,
                test_id,
                alpha_id,
                "direct_call",
                "high",
                detected_at,
            ),
            (
                workspace_id,
                test_id,
                thing_id,
                "name_convention",
                "mid",
                detected_at,
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO test_runs("
        "workspace_id, test_fn_id, test_name, test_class, test_file, status, "
        "duration_ms, error_message, error_type, ci_run_id, ci_url, run_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                workspace_id,
                test_id,
                "test_alpha",
                "TestAlpha",
                "a.py",
                "passed",
                10.0,
                "",
                "",
                "ci-1",
                "",
                1_735_689_600.0,
            ),
            (
                workspace_id,
                test_id,
                "test_alpha",
                "TestAlpha",
                "a.py",
                "failed",
                30.0,
                "expected 1 but got 2",
                "AssertionError",
                "ci-2",
                "",
                1_735_776_000.0,
            ),
        ],
    )
    conn.execute(
        "INSERT INTO semgrep_findings("
        "file_instance_id, content_hash, rule_id, rule_name, message, severity, "
        "confidence, start_line, end_line, snippet, fix, symbol_qualified"
        ") VALUES (?, 'file-a', 'python.info', 'style note', 'consider rename', "
        "'INFO', 'MEDIUM', 1, 1, 'alpha()', '', 'a.alpha')",
        (file_id,),
    )
    conn.commit()


def test_stats_binary_matches_python_get_stats(tmp_path: Path) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    db_path = tmp_path / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        expected = json.loads(json.dumps(db.get_stats(), ensure_ascii=False))
    finally:
        db.close()

    completed = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "stats",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == expected


def test_status_binary_matches_python_get_status(tmp_path: Path) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    db_path = tmp_path / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        _seed_status_files(db, workspace_id)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        expected = json.loads(json.dumps(db.get_status(), ensure_ascii=False))
    finally:
        db.close()

    completed = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "status",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == expected


@pytest.mark.parametrize("action", ["explain", "paths"])
def test_config_binary_matches_python_output(action: str) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    rust_result = _run_rust_config(binary, action)
    python_result = _run_python_config(action)

    assert rust_result.returncode == python_result.returncode == 0
    assert rust_result.stderr == python_result.stderr == ""
    assert _normalize_config_source(rust_result.stdout) == _normalize_config_source(
        python_result.stdout
    )


def test_config_check_role_binary_matches_python_output() -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    rust_result = _run_rust_config(binary, "check-role", "local")
    python_result = _run_python_config("check-role", "local")

    assert rust_result.returncode == python_result.returncode == 0
    assert rust_result.stderr == python_result.stderr == ""
    assert rust_result.stdout == python_result.stdout


@pytest.mark.parametrize(
    "search_args",
    [
        ("alpha",),
        ("alpha", "--kind", "fn"),
        ("alpha", "--limit", "1"),
        ("missing",),
    ],
)
def test_search_binary_matches_python_process_output(
    tmp_path: Path, search_args: tuple[str, ...]
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": "zh_CN",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    python_result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "cw.py"), "search", *search_args],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "search",
            *search_args,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert rust_result.returncode == python_result.returncode == 0
    assert rust_result.stderr == python_result.stderr == ""
    assert rust_result.stdout == python_result.stdout


@pytest.mark.parametrize("qualified_name", ["a.alpha", "missing"])
def test_symbol_binary_matches_python_process_output(
    tmp_path: Path, qualified_name: str
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": "zh_CN",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    python_result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "cw.py"), "symbol", qualified_name],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "symbol",
            qualified_name,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert rust_result.returncode == python_result.returncode == 0
    assert rust_result.stderr == python_result.stderr == ""
    assert rust_result.stdout == python_result.stdout


@pytest.mark.parametrize(
    ("file_name", "use_absolute"),
    [
        ("a.py", False),
        ("a.py", True),
        ("missing.py", False),
    ],
)
def test_file_binary_matches_python_process_output(
    tmp_path: Path, file_name: str, use_absolute: bool
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    file_arg = str(workspace_root / file_name) if use_absolute else file_name
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": "zh_CN",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    python_result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "cw.py"), "file", file_arg],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "file",
            file_arg,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert rust_result.returncode == python_result.returncode == 0
    assert rust_result.stderr == python_result.stderr == ""
    assert rust_result.stdout == python_result.stdout


@pytest.mark.parametrize(
    ("symbol_name", "use_absolute"),
    [
        ("alpha", False),
        ("alpha", True),
        ("missing", False),
    ],
)
def test_query_binary_matches_python_process_output(
    tmp_path: Path, symbol_name: str, use_absolute: bool
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    file_arg = str(workspace_root / "a.py") if use_absolute else "a.py"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": "zh_CN",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    python_result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "cw.py"),
            "query",
            symbol_name,
            file_arg,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "query",
            symbol_name,
            file_arg,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert rust_result.returncode == python_result.returncode == 0
    assert rust_result.stderr == python_result.stderr == ""
    assert rust_result.stdout == python_result.stdout


@pytest.mark.parametrize(
    ("grep_args", "force_fallback"),
    [
        (("needle", "--fixed"), False),
        (("needle.*time",), False),
        (("needle", "time", "--fixed"), False),
        (("needle", "--fixed", "--include-all"), False),
        (("needle", "--fixed", "--kind", "fn"), False),
        (("needle", "--fixed", "--limit", "1"), False),
        (("missing", "--fixed"), False),
        (("needle", "time", "--fixed"), True),
    ],
)
def test_grep_binary_matches_python_process_output(
    tmp_path: Path, grep_args: tuple[str, ...], force_fallback: bool
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "a.py").write_text(
        "def alpha():\n"
        '    needle = "time"\n'
        'needle = "top time"\n'
        "class Thing:\n"
        '    needle = "time"\n',
        encoding="utf-8",
    )
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": "zh_CN",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    if force_fallback:
        empty_path = tmp_path / "empty-path"
        empty_path.mkdir()
        # Windows 当前 Rust CLI 与 PyO3 同 crate，进程启动仍需找到 python DLL。
        # 保留 Python 目录但排除 rg，仍能真实覆盖内置 fallback。
        env["PATH"] = (
            os.pathsep.join((str(Path(sys.executable).parent), str(empty_path)))
            if os.name == "nt"
            else str(empty_path)
        )

    python_result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "cw.py"), "grep", *grep_args],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "grep",
            *grep_args,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert rust_result.returncode == python_result.returncode == 0
    assert rust_result.stderr == python_result.stderr == ""
    assert rust_result.stdout == python_result.stdout


@pytest.mark.parametrize(
    "issues_args",
    [
        ("a.alpha",),
        ("a.alpha", "--include-info"),
        ("a.missing",),
    ],
)
def test_issues_binary_matches_python_process_output(
    tmp_path: Path, issues_args: tuple[str, ...]
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        _seed_issues_tests_fixture(db, workspace_id)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": "zh_CN",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    python_result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "cw.py"), "issues", *issues_args],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "issues",
            *issues_args,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert rust_result.returncode == python_result.returncode == 0
    assert rust_result.stderr == python_result.stderr == ""
    assert rust_result.stdout == python_result.stdout


@pytest.mark.parametrize(
    "tests_args",
    [
        ("a.alpha",),
        ("a.test_alpha", "--reverse"),
        ("a.alpha", "--history"),
        ("a.missing",),
        ("a.missing", "--reverse"),
        ("a.missing", "--history"),
        (),
    ],
)
def test_tests_binary_matches_python_read_process_output(
    tmp_path: Path, tests_args: tuple[str, ...]
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        _seed_issues_tests_fixture(db, workspace_id)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": "zh_CN",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    python_result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "cw.py"), "tests", *tests_args],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "tests",
            *tests_args,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert rust_result.returncode == python_result.returncode == 0
    assert rust_result.stderr == python_result.stderr == ""
    assert rust_result.stdout == python_result.stdout


@pytest.mark.parametrize(
    ("command", "query_args"),
    [
        ("callers", ("Thing",)),
        ("callers", ("a.Thing",)),
        ("callers", ("Thing", "--qualified", "a.Thing")),
        ("callers", ("missing",)),
        ("callees", ("alpha",)),
        ("callees", ("a.alpha",)),
        ("callees", ("alpha", "--qualified", "a.alpha")),
        ("callees", ("missing",)),
        ("call-chain", ("a.alpha",)),
        ("call-chain", ("a.alpha", "--depth", "1")),
        ("call-chain", ("a.alpha", "--depth", "0")),
        ("call-chain", ("missing",)),
        ("topo", ()),
        ("topo", ("--limit", "1")),
        ("topo", ("--limit", "0")),
        ("impact", ("sym-b",)),
        ("impact", ("sym-b", "--depth", "0")),
        ("impact", ("sym-b", "--depth", "-1")),
        ("impact", ("missing",)),
    ],
)
def test_graph_query_binary_matches_python_process_output(
    tmp_path: Path, command: str, query_args: tuple[str, ...]
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": "zh_CN",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    python_result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "cw.py"), command, *query_args],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            command,
            *query_args,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert rust_result.returncode == python_result.returncode == 0
    assert rust_result.stderr == python_result.stderr == ""
    assert rust_result.stdout == python_result.stdout


def _refresh_db_snapshot(db_path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(db_path)
    try:
        return {
        "files": conn.execute(
            "SELECT rel_path, current_content_hash, total_lines, module_path "
            "FROM file_instances ORDER BY rel_path"
        ).fetchall(),
            "symbols": conn.execute(
                "SELECT s.name, s.kind, s.start_line, s.end_line, s.qualified_name "
                "FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id "
                "ORDER BY fi.rel_path, s.start_line, s.name"
            ).fetchall(),
            "calls": conn.execute(
                "SELECT caller_name, callee_name, call_line, is_cross_file "
                "FROM calls ORDER BY caller_name, call_line, callee_name"
            ).fetchall(),
            "versions": conn.execute(
                "SELECT version_num, content_hash, total_lines, is_current, is_deleted "
                "FROM file_versions ORDER BY version_num"
            ).fetchall(),
            "version_symbols": conn.execute(
                "SELECT qualified_name, start_line, end_line, module_path, is_deleted "
                "FROM file_symbol_versions ORDER BY qualified_name, start_line"
            ).fetchall(),
        }
    finally:
        conn.close()


def test_refresh_binary_matches_python_persisted_graph(tmp_path: Path) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    roots = {}
    workspace_ids = {}
    for implementation in ("python", "rust"):
        home = tmp_path / implementation / "home"
        workspace = tmp_path / implementation / "workspace"
        (home / ".callwarden").mkdir(parents=True)
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "lib.rs").write_text(
            "pub fn alpha() { beta(); }\nfn beta() {}\n",
            encoding="utf-8",
        )
        db_path = home / ".callwarden" / "callwarden.db"
        db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace))
        try:
            workspace_ids[implementation] = db._get_active_workspace_id()
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            db.close()
        roots[implementation] = (home, workspace, db_path)

    python_home, python_workspace, python_db = roots["python"]
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(python_home),
            "USERPROFILE": str(python_home),
            "CALLWARDEN_WORKSPACE": str(python_workspace),
            "CALLWARDEN_LANG": "en_US",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    python_result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "cw.py"), "refresh", "src/lib.rs"],
        cwd=python_workspace,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    _, rust_workspace, rust_db = roots["rust"]
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(rust_db),
            "--workspace-id",
            str(workspace_ids["rust"]),
            "refresh",
            "src/lib.rs",
        ],
        cwd=rust_workspace,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert python_result.returncode == 0, python_result.stderr
    assert rust_result.returncode == 0, rust_result.stderr
    assert "Refreshed: src/lib.rs" in python_result.stdout
    assert "Refreshed: src/lib.rs" in rust_result.stdout
    assert _refresh_db_snapshot(rust_db) == _refresh_db_snapshot(python_db)

    # Python 旧路径在成功解析后仍保留 pending；Rust 写路径闭合为 parsed。
    with sqlite3.connect(python_db) as conn:
        assert conn.execute("SELECT status FROM file_instances").fetchone()[0] == "pending"
    with sqlite3.connect(rust_db) as conn:
        assert conn.execute("SELECT status FROM file_instances").fetchone()[0] == "parsed"


def test_refresh_all_binary_matches_python_and_preserves_incremental_contract(
    tmp_path: Path,
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    roots: dict[str, tuple[Path, Path, Path]] = {}
    workspace_ids: dict[str, int] = {}
    for implementation in ("python", "rust"):
        home = tmp_path / implementation / "home"
        workspace = tmp_path / implementation / "workspace"
        (home / ".callwarden").mkdir(parents=True)
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "first.rs").write_text(
            "pub fn first() { shared(); }\nfn shared() {}\n",
            encoding="utf-8",
        )
        (workspace / "src" / "second.rs").write_text(
            "pub fn second() {}\n",
            encoding="utf-8",
        )
        db_path = home / ".callwarden" / "callwarden.db"
        db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace))
        try:
            workspace_ids[implementation] = db._get_active_workspace_id()
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            db.close()
        roots[implementation] = (home, workspace, db_path)

    python_home, python_workspace, python_db = roots["python"]
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(python_home),
            "USERPROFILE": str(python_home),
            "CALLWARDEN_WORKSPACE": str(python_workspace),
            "CALLWARDEN_LANG": "en_US",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    python_result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "cw.py"), "refresh", "--all"],
        cwd=python_workspace,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    _, rust_workspace, rust_db = roots["rust"]

    def run_rust_full(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(binary),
                "--mode",
                "local",
                "--db",
                str(rust_db),
                "--workspace-id",
                str(workspace_ids["rust"]),
                "refresh",
                "--all",
                *extra,
            ],
            cwd=rust_workspace,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    first = run_rust_full()
    assert python_result.returncode == 0, python_result.stderr
    assert first.returncode == 0, first.stderr
    assert "refreshed 2 / unchanged 0 / deleted 0 / failed 0" in first.stdout
    assert _refresh_db_snapshot(rust_db) == _refresh_db_snapshot(python_db)

    with sqlite3.connect(rust_db) as conn:
        version_count = conn.execute("SELECT COUNT(*) FROM file_versions").fetchone()[0]
    unchanged = run_rust_full()
    assert unchanged.returncode == 0, unchanged.stderr
    assert "refreshed 0 / unchanged 2 / deleted 0 / failed 0" in unchanged.stdout
    with sqlite3.connect(rust_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM file_versions").fetchone()[0] == version_count

    (rust_workspace / "src" / "first.rs").write_text(
        "pub fn first_changed() {}\n",
        encoding="utf-8",
    )
    (rust_workspace / "src" / "second.rs").unlink()
    changed = run_rust_full()
    assert changed.returncode == 0, changed.stderr
    assert "refreshed 1 / unchanged 0 / deleted 1 / failed 0" in changed.stdout
    with sqlite3.connect(rust_db) as conn:
        rows = conn.execute(
            "SELECT rel_path, status FROM file_instances ORDER BY rel_path"
        ).fetchall()
        assert rows == [("src/first.rs", "parsed"), ("src/second.rs", "deleted")]
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM symbols ORDER BY name"
            ).fetchall()
        }
        assert "first_changed" in names
        assert "second" not in names

    forced = run_rust_full("--force")
    assert forced.returncode == 0, forced.stderr
    assert "refreshed 1 / unchanged 0 / deleted 0 / failed 0" in forced.stdout


def test_workspace_lifecycle_binary_matches_python_process(tmp_path: Path) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    registered_root = tmp_path / "registered"
    workspace_root.mkdir()
    registered_root.mkdir()
    db_paths: dict[str, Path] = {}
    envs: dict[str, dict[str, str]] = {}

    for implementation in ("python", "rust"):
        home = tmp_path / implementation / "home"
        db_path = home / ".callwarden" / "callwarden.db"
        db_path.parent.mkdir(parents=True)
        db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
        try:
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            db.close()
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "CALLWARDEN_WORKSPACE": str(workspace_root),
                "CALLWARDEN_LANG": "zh_CN",
                "CALLWARDEN_SKIP_AUTO_SETUP": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            }
        )
        db_paths[implementation] = db_path
        envs[implementation] = env

    def run_python(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "cw.py"), "workspace", *args],
            cwd=workspace_root,
            env=envs["python"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def run_rust(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(binary),
                "--mode",
                "local",
                "--db",
                str(db_paths["rust"]),
                "workspace",
                *args,
            ],
            cwd=workspace_root,
            env=envs["rust"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    commands = [
        ("register", "secondary", str(registered_root)),
        ("list",),
        ("set", "secondary"),
        ("list",),
        ("set", "workspace"),
        ("delete", "secondary"),
        ("list",),
    ]
    for args in commands:
        python_result = run_python(*args)
        rust_result = run_rust(*args)
        assert python_result.returncode == 0, python_result.stderr
        assert rust_result.returncode == 0, rust_result.stderr
        assert rust_result.stderr == python_result.stderr == ""
        assert rust_result.stdout == python_result.stdout

    with sqlite3.connect(db_paths["python"]) as python_conn:
        python_rows = python_conn.execute(
            "SELECT name, root_path, is_active, description "
            "FROM workspaces ORDER BY id"
        ).fetchall()
    with sqlite3.connect(db_paths["rust"]) as rust_conn:
        rust_rows = rust_conn.execute(
            "SELECT name, root_path, is_active, description "
            "FROM workspaces ORDER BY id"
        ).fetchall()
    assert rust_rows == python_rows


def test_workspace_status_is_read_only_and_uses_active_workspace(tmp_path: Path) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    db_path = tmp_path / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = db._get_active_workspace_id()
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "workspace",
            "status",
        ],
        cwd=workspace_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["id"] == workspace_id
    assert payload["is_active"] is True

    with sqlite3.connect(db_path) as conn:
        assert conn.total_changes == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM workspaces WHERE is_active = 1"
        ).fetchone()[0] == 1


def test_workspace_remove_cleans_full_codegraph_schema(tmp_path: Path) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    db_path = tmp_path / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "workspace",
            "remove",
            str(workspace_id),
        ],
        cwd=workspace_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM file_instances WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM file_versions").fetchone()[0] == 0


def test_toolchain_and_build_context_binary_match_python_process(
    tmp_path: Path,
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    include_path = workspace_root / "include"
    compiler_path = workspace_root / "toolchain" / "bin" / "gcc.exe"
    include_path.mkdir(parents=True)
    compiler_path.parent.mkdir(parents=True)
    compiler_path.write_text("", encoding="utf-8")

    db_paths: dict[str, Path] = {}
    envs: dict[str, dict[str, str]] = {}
    workspace_ids: dict[str, int] = {}
    for implementation in ("python", "rust"):
        home = tmp_path / implementation / "home"
        db_path = home / ".callwarden" / "callwarden.db"
        db_path.parent.mkdir(parents=True)
        db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
        try:
            workspace_ids[implementation] = _seed_stats_fixture(db)
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            db.close()
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "CALLWARDEN_WORKSPACE": str(workspace_root),
                "CALLWARDEN_LANG": "zh_CN",
                "CALLWARDEN_SKIP_AUTO_SETUP": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            }
        )
        db_paths[implementation] = db_path
        envs[implementation] = env

    assert workspace_ids["python"] == workspace_ids["rust"]
    workspace_id = workspace_ids["python"]

    def run_python(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "cw.py"), *args],
            cwd=workspace_root,
            env=envs["python"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def run_rust(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(binary),
                "--mode",
                "local",
                "--db",
                str(db_paths["rust"]),
                *args,
            ],
            cwd=workspace_root,
            env=envs["rust"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def assert_same(*args: str) -> None:
        python_result = run_python(*args)
        rust_result = run_rust(*args)
        assert python_result.returncode == 0, python_result.stderr
        assert rust_result.returncode == 0, rust_result.stderr
        assert rust_result.stderr == python_result.stderr == ""
        assert rust_result.stdout == python_result.stdout

    assert_same(
        "toolchain",
        "register",
        "fixture-gcc",
        str(compiler_path),
        "--no-probe",
    )
    assert_same("toolchain", "list")
    assert_same("toolchain", "show", "fixture-gcc")

    assert_same(
        "build-context",
        "register",
        str(workspace_id),
        "debug",
        "--flags=-O2",
        "--defines",
        "DEBUG=1",
        "--includes",
        str(include_path),
        "--activate",
    )
    with sqlite3.connect(db_paths["python"]) as python_conn:
        python_hash = python_conn.execute(
            "SELECT build_context_hash FROM workspace_build_contexts "
            "WHERE workspace_id = ? AND name = 'debug'",
            (workspace_id,),
        ).fetchone()[0]
    with sqlite3.connect(db_paths["rust"]) as rust_conn:
        rust_hash = rust_conn.execute(
            "SELECT build_context_hash FROM workspace_build_contexts "
            "WHERE workspace_id = ? AND name = 'debug'",
            (workspace_id,),
        ).fetchone()[0]
    assert rust_hash == python_hash

    assert_same("build-context", "list", str(workspace_id))
    assert_same("build-context", "show", str(workspace_id), python_hash[:16])
    assert_same(
        "toolchain",
        "bind",
        str(workspace_id),
        "fixture-gcc",
        "--build-context-hash",
        python_hash,
    )
    assert_same(
        "toolchain",
        "list-bound",
        str(workspace_id),
        "--build-context-hash",
        python_hash,
    )
    assert_same("build-context", "resolve", str(workspace_id), python_hash)
    assert_same("build-context", "edges", str(workspace_id), python_hash)

    compile_commands_path = workspace_root / "compile_commands.json"
    compile_commands_path.write_text(
        json.dumps(
            [
                {
                    "directory": str(workspace_root),
                    "file": "a.c",
                    "arguments": ["-DIMPORT=1", "-I", "include", "-O2", "a.c"],
                }
            ]
        ),
        encoding="utf-8",
    )
    assert_same(
        "build-context",
        "import-compile-commands",
        str(compile_commands_path),
        str(workspace_id),
        "--name",
        "imported",
        "--workspace-root",
        str(workspace_root),
    )
    assert_same("build-context", "list", str(workspace_id))

    assert_same("build-context", "delete", str(workspace_id), python_hash)
    assert_same("toolchain", "delete", "fixture-gcc")


def _seed_task_read_fixture(db: CodeGraphDB) -> None:
    """构造 task 只读命令的树、阻塞 finding 与三角关联。"""
    workspace_id = db._get_active_workspace_id()
    conn = db.conn
    conn.executemany(
        "INSERT INTO tasks("
        "id, title, description, creator, status, created_at, updated_at, "
        "parent_id, depth, sort_order"
        ") VALUES (?, ?, ?, 'agent', ?, ?, ?, ?, ?, ?)",
        [
            ("task-root", "Root task", "root desc", "in_progress", 1735689600.0,
             1735689600.0, "", 0, 0),
            ("task-child-a", "Child A", "", "review", 1735689601.0,
             1735689601.0, "task-root", 1, 0),
            ("task-child-b", "Child B", "", "open", 1735689602.0,
             1735689602.0, "task-root", 1, 1),
        ],
    )
    conn.executemany(
        "INSERT INTO task_steps("
        "id, task_id, step_index, action, target_file, target_symbol, "
        "check_items, status, result, created_at, completed_at"
        ") VALUES (?, ?, ?, ?, ?, ?, '[]', ?, '', ?, ?)",
        [
            ("step-root", "task-root", 0, "inspect", "", "pkg.root",
             "done", 1735689600.0, 1735689610.0),
            ("step-child-a", "task-child-a", 0, "fix", "src/a.py", "",
             "pending", 1735689601.0, None),
            ("step-child-b", "task-child-b", 0, "verify", "", "",
             "skipped", 1735689602.0, 1735689612.0),
        ],
    )
    conn.executemany(
        "INSERT INTO task_quality_findings("
        "workspace_id, task_id, step_id, finding_type, severity, status, "
        "message, evidence, source, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)",
        [
            (workspace_id, "task-child-a", "step-child-a", "scope", "block",
             "open", "outside task scope", "scope", 1735689620.0),
            (workspace_id, "task-root", "", "style", "warn",
             "resolved", "style note", "manual", 1735689621.0),
        ],
    )
    conn.execute(
        "INSERT INTO git_commits("
        "commit_hash, message, author, email, timestamp, workspace_id"
        ") VALUES ('0123456789abcdef', 'Task commit\\nbody', 'Reviewer', "
        "'reviewer@example.com', ?, ?)",
        (1735689630.0, workspace_id),
    )
    conn.execute(
        "INSERT INTO task_symbol_changes("
        "workspace_id, task_id, step_id, file_path, qualified_name, symbol_name, "
        "change_type, source_commit_hash, created_at"
        ") VALUES (?, 'task-root', 'step-root', 'src/a.py', 'pkg.alpha', "
        "'alpha', 'modified', '0123456789abcdef', ?)",
        (workspace_id, 1735689640.0),
    )
    conn.commit()


def test_task_read_commands_match_python_and_ignore_daemon_route(
    tmp_path: Path,
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    db_paths: dict[str, Path] = {}
    envs: dict[str, dict[str, str]] = {}
    for implementation in ("python", "rust"):
        home = tmp_path / implementation / "home"
        db_path = home / ".callwarden" / "callwarden.db"
        db_path.parent.mkdir(parents=True)
        db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
        try:
            _seed_task_read_fixture(db)
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            db.close()
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "CALLWARDEN_WORKSPACE": str(workspace_root),
                "CALLWARDEN_LANG": "en_US",
                "CALLWARDEN_SKIP_AUTO_SETUP": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            }
        )
        db_paths[implementation] = db_path
        envs[implementation] = env

    def run_python(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "cw.py"), *args],
            cwd=workspace_root,
            env=envs["python"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def run_rust(*args: str, mode: str = "local") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(binary),
                "--mode",
                mode,
                "--db",
                str(db_paths["rust"]),
                *args,
            ],
            cwd=workspace_root,
            env=envs["rust"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    for args in (
        ("task", "list", "--limit", "20"),
        ("task", "list", "--blocked", "--limit", "20"),
        ("task", "list", "--status", "review", "--flat"),
        ("task", "show", "task-root"),
        ("task", "show", "task-root", "--flat"),
        ("task", "status-tree", "task-root"),
        ("task", "findings", "task-child-a"),
        ("task", "findings", "task-root", "--status", "all", "--severity", "warn"),
    ):
        python_result = run_python(*args)
        rust_result = run_rust(*args)
        assert python_result.returncode == 0, python_result.stderr
        assert rust_result.returncode == 0, rust_result.stderr
        assert rust_result.stderr == python_result.stderr == ""
        assert rust_result.stdout == python_result.stdout

    before = db_paths["rust"].read_bytes()
    enterprise_result = run_rust("task", "show", "task-root", mode="enterprise")
    assert enterprise_result.returncode == 0, enterprise_result.stderr
    assert enterprise_result.stdout == run_python(
        "task", "show", "task-root"
    ).stdout
    assert db_paths["rust"].read_bytes() == before


def _normalize_task_write_output(output: str) -> str:
    """仅归一化随机 ID 和真实时间，保留命令契约的其余字符。"""
    output = re.sub(r"\bT-\d+-[0-9a-f]{8}\b", "T-<id>", output)
    output = re.sub(r"\bS-\d+-[0-9a-f]{8}\b", "S-<id>", output)
    output = re.sub(
        r"(Reopened at:|重新打开时间:|Applied at:|审核时间:|Closed at:|关闭时间:)\s+\d+(?:\.\d+)?",
        r"\1 <timestamp>",
        output,
    )
    return output


def _task_write_snapshot(db_path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            "tasks": conn.execute(
                "SELECT t.title, t.description, t.creator, t.status, "
                "COALESCE(p.title, ''), t.depth, t.sort_order, "
                "t.applied_at IS NULL, t.closed_at IS NULL "
                "FROM tasks t LEFT JOIN tasks p ON p.id = t.parent_id "
                "ORDER BY t.depth, t.sort_order, t.title"
            ).fetchall(),
            "steps": conn.execute(
                "SELECT t.title, s.step_index, s.action, s.target_file, "
                "s.target_symbol, s.check_items, s.status, s.result, "
                "s.completed_at IS NOT NULL "
                "FROM task_steps s JOIN tasks t ON t.id = s.task_id "
                "ORDER BY t.title, s.step_index"
            ).fetchall(),
            "active": conn.execute(
                "SELECT COALESCE(t.title, '') "
                "FROM workspaces w LEFT JOIN tasks t ON t.id = w.active_task_id "
                "WHERE w.is_active = 1"
            ).fetchall(),
        }
    finally:
        conn.close()


def test_task_write_commands_match_python_persisted_state_and_fail_closed(
    tmp_path: Path,
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    db_paths: dict[str, Path] = {}
    envs: dict[str, dict[str, str]] = {}
    for implementation in ("python", "rust"):
        home = tmp_path / implementation / "home"
        db_path = home / ".callwarden" / "callwarden.db"
        db_path.parent.mkdir(parents=True)
        db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "CALLWARDEN_WORKSPACE": str(workspace_root),
                "CALLWARDEN_LANG": "en_US",
                "CALLWARDEN_SKIP_AUTO_SETUP": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            }
        )
        db_paths[implementation] = db_path
        envs[implementation] = env

    def run_python(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "cw.py"), *args],
            cwd=workspace_root,
            env=envs["python"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def run_rust(*args: str, mode: str = "local") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(binary),
                "--mode",
                mode,
                "--db",
                str(db_paths["rust"]),
                *args,
            ],
            cwd=workspace_root,
            env=envs["rust"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def ids(implementation: str) -> tuple[str, list[str]]:
        conn = sqlite3.connect(db_paths[implementation])
        try:
            task_id = conn.execute(
                "SELECT id FROM tasks WHERE title = 'Write task'"
            ).fetchone()[0]
            step_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM task_steps WHERE task_id = ? ORDER BY step_index",
                    (task_id,),
                )
            ]
            return task_id, step_ids
        finally:
            conn.close()

    def assert_output_pair(
        python_result: subprocess.CompletedProcess[str],
        rust_result: subprocess.CompletedProcess[str],
    ) -> None:
        assert python_result.returncode == rust_result.returncode == 0
        assert python_result.stderr == rust_result.stderr == ""
        assert _normalize_task_write_output(rust_result.stdout) == (
            _normalize_task_write_output(python_result.stdout)
        )

    steps_json = json.dumps(
        [
            {"action": "inspect", "check_items": ["read", "syntax"]},
            {"action": "verify", "target_file": "src/lib.rs"},
        ],
        ensure_ascii=False,
    )
    assert_output_pair(
        run_python(
            "task",
            "create",
            "--title",
            "Write task",
            "--desc",
            "state machine",
            "--steps",
            steps_json,
        ),
        run_rust(
            "task",
            "create",
            "--title",
            "Write task",
            "--desc",
            "state machine",
            "--steps",
            steps_json,
            mode="enterprise",
        ),
    )
    python_task, python_steps = ids("python")
    rust_task, rust_steps = ids("rust")

    assert_output_pair(
        run_python("task", "next", python_task),
        run_rust("task", "next", rust_task, mode="enterprise"),
    )
    assert_output_pair(
        run_python(
            "task", "report", python_task, python_steps[0], "--result", "inspected"
        ),
        run_rust(
            "task", "report", rust_task, rust_steps[0], "--result", "inspected",
            mode="enterprise",
        ),
    )
    assert_output_pair(
        run_python("task", "next", python_task),
        run_rust("task", "next", rust_task),
    )
    assert_output_pair(
        run_python(
            "task", "report", python_task, python_steps[1], "--result", "broken", "--fail"
        ),
        run_rust(
            "task", "report", rust_task, rust_steps[1], "--result", "broken", "--fail"
        ),
    )
    assert _task_write_snapshot(db_paths["rust"]) == _task_write_snapshot(
        db_paths["python"]
    )

    for implementation, task_id in (("python", python_task), ("rust", rust_task)):
        conn = sqlite3.connect(db_paths[implementation])
        conn.execute(
            "UPDATE tasks SET status = 'closed', applied_at = 1, closed_at = 2 "
            "WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        conn.close()
    assert_output_pair(
        run_python(
            "task",
            "reopen",
            python_task,
            "--reviewer",
            "diff-test",
            "--reason",
            "regression",
        ),
        run_rust(
            "task",
            "reopen",
            rust_task,
            "--reviewer",
            "diff-test",
            "--reason",
            "regression",
            mode="enterprise",
        ),
    )
    assert _task_write_snapshot(db_paths["rust"]) == _task_write_snapshot(
        db_paths["python"]
    )

    before = db_paths["rust"].read_bytes()
    invalid = run_rust("task", "report", rust_task, "S-missing", "--result", "bad")
    assert invalid.returncode != 0
    assert "task step not found" in invalid.stderr
    assert db_paths["rust"].read_bytes() == before


def _task_audit_snapshot(db_path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            "tasks": conn.execute(
                "SELECT t.title, t.status, COALESCE(p.title, ''), t.depth, t.sort_order "
                "FROM tasks t LEFT JOIN tasks p ON p.id = t.parent_id "
                "ORDER BY t.depth, t.sort_order, t.title"
            ).fetchall(),
            "steps": conn.execute(
                "SELECT t.title, s.step_index, s.action, s.target_file, s.status "
                "FROM task_steps s JOIN tasks t ON t.id = s.task_id "
                "ORDER BY t.title, s.step_index"
            ).fetchall(),
            "findings": conn.execute(
                "SELECT t.title, q.status, q.resolved_by "
                "FROM task_quality_findings q JOIN tasks t ON t.id = q.task_id "
                "ORDER BY q.id"
            ).fetchall(),
            "changes": conn.execute(
                "SELECT t.title, c.file_path, c.hash_before, c.hash_after, c.author "
                "FROM change_audit c JOIN tasks t ON t.id = c.task_id "
                "ORDER BY c.timestamp, c.file_path"
            ).fetchall(),
        }
    finally:
        conn.close()


def test_task_audit_commands_match_python_and_enforce_rust_reviewer_boundary(
    tmp_path: Path,
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    db_paths: dict[str, Path] = {}
    workspace_roots: dict[str, Path] = {}
    envs: dict[str, dict[str, str]] = {}
    for implementation in ("python", "rust"):
        workspace_root = tmp_path / implementation / "workspace"
        workspace_root.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=workspace_root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "diff@example.com"],
            cwd=workspace_root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Diff Test"],
            cwd=workspace_root,
            check=True,
        )
        tracked = workspace_root / "tracked.py"
        tracked.write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.py"], cwd=workspace_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=workspace_root, check=True)
        tracked.write_text("value = 2\n", encoding="utf-8")
        plan = workspace_root / "plan.md"
        plan.write_text(
            "## Parser\nMove parser.\n- edit @ src/parser.rs\n\n"
            "## Tests ##\n- test: tests/test_parser.py\n",
            encoding="utf-8",
        )

        home = tmp_path / implementation / "home"
        db_path = home / ".callwarden" / "callwarden.db"
        db_path.parent.mkdir(parents=True)
        db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
        workspace_id = db._get_active_workspace_id()
        db.conn.executemany(
            "INSERT INTO tasks(id,title,description,creator,status,created_at,updated_at,"
            "applied_at,closed_at,parent_id,depth,sort_order) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("review-task", "Review task", "", "builder", "review", 1, 1, None, None, "", 0, 0),
                ("finding-task", "Finding task", "", "builder", "in_progress", 2, 2, None, None, "", 0, 1),
                ("split-parent", "Split parent", "", "builder", "open", 3, 3, None, None, "", 0, 2),
                ("rollback-task", "Rollback task", "", "builder", "in_progress", 4, 4, None, None, "", 0, 3),
                ("capture-task", "Capture task", "", "builder", "in_progress", 5, 5, None, None, "", 0, 4),
            ],
        )
        db.conn.execute(
            "INSERT INTO task_steps(id,task_id,step_index,action,target_file,target_symbol,"
            "check_items,status,result,created_at,completed_at) "
            "VALUES('capture-step','capture-task',0,'edit','tracked.py','','','in_progress','',5,NULL)"
        )
        db.conn.execute(
            "INSERT INTO task_quality_findings(workspace_id,task_id,step_id,finding_type,"
            "severity,status,message,evidence,source,created_at,resolved_at,resolved_by) "
            "VALUES(?, 'finding-task', '', 'scope', 'warn', 'open', 'review warning', '', 'manual', 6, NULL, '')",
            (workspace_id,),
        )
        db.conn.execute(
            "INSERT INTO change_audit(id,task_id,step_id,file_path,hash_before,hash_after,diff,author,timestamp) "
            "VALUES('change-1','rollback-task','rollback-step','old.py','before','after','','agent',7)"
        )
        db.conn.execute(
            "UPDATE workspaces SET active_task_id = 'capture-task' WHERE id = ?",
            (workspace_id,),
        )
        db.conn.commit()
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()

        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "CALLWARDEN_WORKSPACE": str(workspace_root),
                "CALLWARDEN_LANG": "en_US",
                "CALLWARDEN_SKIP_AUTO_SETUP": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            }
        )
        db_paths[implementation] = db_path
        workspace_roots[implementation] = workspace_root
        envs[implementation] = env

    def run_python(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "cw.py"), *args],
            cwd=workspace_roots["python"],
            env=envs["python"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def run_rust(*args: str, mode: str = "local") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(binary), "--mode", mode, "--db", str(db_paths["rust"]), *args],
            cwd=workspace_roots["rust"],
            env=envs["rust"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def assert_pair(*args: str) -> None:
        python_result = run_python(*args)
        rust_result = run_rust(*args, mode="enterprise")
        assert python_result.returncode == rust_result.returncode == 0
        assert python_result.stderr == rust_result.stderr == ""
        assert _normalize_task_write_output(rust_result.stdout) == (
            _normalize_task_write_output(python_result.stdout)
        )

    assert_pair("task", "completion-review", "finding-task")
    assert_pair("task", "resolve-finding", "1", "--resolution", "fixed", "--by", "reviewer")
    assert_pair("task", "apply", "review-task", "--reviewer", "external-reviewer")
    assert_pair("task", "close", "review-task", "--reviewer", "external-reviewer")
    assert_pair("task", "rollback", "rollback-task", "change-1")
    assert_pair("task", "split", "split-parent", "--plan", str(workspace_roots["python"] / "plan.md"))

    python_capture = run_python("task", "capture-diff", "capture-task", "--step-id", "capture-step", "--dry-run")
    rust_capture = run_rust("task", "capture-diff", "capture-task", "--step-id", "capture-step", "--dry-run")
    assert python_capture.returncode == rust_capture.returncode == 0
    assert _normalize_task_write_output(rust_capture.stdout) == _normalize_task_write_output(
        python_capture.stdout
    )

    assert _task_audit_snapshot(db_paths["rust"]) == _task_audit_snapshot(db_paths["python"])

    before = db_paths["rust"].read_bytes()
    denied = run_rust("task", "apply", "finding-task", "--reviewer", "builder")
    assert denied.returncode != 0
    assert "self-approval is forbidden" in denied.stderr
    assert db_paths["rust"].read_bytes() == before
