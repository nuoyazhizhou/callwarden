"""Rust `cw` 生产命令与 Python 真相源的差分测试。"""

from __future__ import annotations

import json
import os
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
        env["PATH"] = str(empty_path)

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
