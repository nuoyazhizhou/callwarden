"""Rust `cw` 生产命令与 Python 真相源的差分测试。"""

from __future__ import annotations

import json
import os
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
