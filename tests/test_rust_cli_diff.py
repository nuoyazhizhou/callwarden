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
        "content_hash, name, kind, content, signature, has_comment, qualified_name"
        ") VALUES "
        "('sym-a', 'alpha', 'fn', 'def alpha(): pass', 'alpha()', 1, 'a.alpha'),"
        "('sym-b', 'Thing', 'struct', 'struct Thing {}', '', 0, 'a.Thing')"
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
        "file_version_id, symbol_hash, qualified_name, start_line, end_line"
        ") VALUES (?, 'sym-a', 'a.alpha', 1, 2)",
        (current_version,),
    )
    conn.execute(
        "INSERT INTO call_versions("
        "file_version_id, caller_qualified, callee_name, callee_qualified, call_line, is_cross_file"
        ") VALUES (?, 'a.alpha', 'Thing', 'a.Thing', 2, 1)",
        (current_version,),
    )
    conn.commit()
    return workspace_id


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
