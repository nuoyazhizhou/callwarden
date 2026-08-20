"""W2.3 P1-B: five query methods through a real Rust daemon and UDS.

This test is intentionally Linux-only.  The Windows named-pipe transport and
the real two-UID matrix are covered by separate acceptance suites; this file
proves that the Linux production binary, UDS framing, dispatch, snapshot and
query layers are connected end to end.
"""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from callwarden.server.daemon_client import UnixDaemonRpcClient
from callwarden.server.daemon_protocol import DaemonRemoteError


pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="W2.3 P1-B requires the Linux Rust daemon and Unix domain sockets",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _fresh_daemon_binary() -> Path:
    configured = os.environ.get("CW_DAEMON_BIN", "").strip()
    if configured:
        binary = Path(configured)
        if not binary.is_file():
            pytest.fail(f"CW_DAEMON_BIN does not exist: {binary}")
        return binary

    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.fail("Linux W2.3 E2E requires cargo; refusing to skip the core test")
    root = _repo_root()
    target_dir = os.environ.get("CARGO_TARGET_DIR", "").strip()
    build_env = os.environ.copy()
    if target_dir:
        build_env["CARGO_TARGET_DIR"] = target_dir
    result = subprocess.run(
        [
            cargo,
            "build",
            "--no-default-features",
            "--manifest-path",
            str(root / "rust_ext/Cargo.toml"),
            "--bin",
            "cw-daemon",
        ],
        cwd=root,
        env=build_env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            "fresh cw-daemon build failed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    binary_root = Path(target_dir) if target_dir else root / "rust_ext" / "target"
    binary = binary_root / "debug" / "cw-daemon"
    if not binary.is_file():
        pytest.fail(f"cargo succeeded but cw-daemon is missing: {binary}")
    return binary


def _create_snapshot_db(db_path: Path, workspace_id: int, workspace_root: Path) -> None:
    """Create a complete, small snapshot fixture for all five query methods."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE workspaces (id INTEGER PRIMARY KEY, root_path TEXT NOT NULL);
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL,
            rel_path TEXT NOT NULL, abs_path TEXT NOT NULL, status TEXT NOT NULL
        );
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY, file_instance_id INTEGER NOT NULL,
            symbol_hash TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
            qualified_name TEXT NOT NULL, module_path TEXT NOT NULL,
            visibility TEXT NOT NULL, start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL, start_col INTEGER NOT NULL,
            end_col INTEGER NOT NULL, signature TEXT NOT NULL,
            has_comment INTEGER NOT NULL, comment_status TEXT NOT NULL,
            depth INTEGER NOT NULL
        );
        CREATE TABLE calls (
            caller_id INTEGER NOT NULL, callee_id INTEGER NOT NULL,
            callee_name TEXT NOT NULL, call_line INTEGER NOT NULL,
            is_cross_file INTEGER NOT NULL
        );
        CREATE TABLE file_versions (
            id INTEGER PRIMARY KEY, file_instance_id INTEGER NOT NULL,
            is_current INTEGER NOT NULL
        );
        CREATE TABLE symbol_contents (
            content_hash TEXT PRIMARY KEY, name TEXT NOT NULL,
            kind TEXT NOT NULL, content TEXT NOT NULL, signature TEXT,
            has_comment INTEGER, comment_content TEXT
        );
        CREATE TABLE file_symbol_versions (
            file_version_id INTEGER NOT NULL, symbol_hash TEXT NOT NULL,
            qualified_name TEXT NOT NULL, module_path TEXT,
            start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
            depth INTEGER NOT NULL, is_deleted INTEGER NOT NULL
        );
        CREATE TABLE call_versions (
            file_version_id INTEGER NOT NULL, caller_qualified TEXT NOT NULL,
            caller_hash TEXT, callee_name TEXT NOT NULL, callee_module TEXT,
            callee_qualified TEXT, callee_file TEXT, call_line INTEGER
        );
        CREATE TABLE semgrep_findings (
            file_instance_id INTEGER NOT NULL, rule_id TEXT NOT NULL,
            rule_name TEXT DEFAULT '', severity TEXT DEFAULT 'INFO',
            confidence TEXT DEFAULT 'UNKNOWN', message TEXT DEFAULT '',
            start_line INTEGER DEFAULT 0, end_line INTEGER DEFAULT 0,
            snippet TEXT DEFAULT '', fix TEXT DEFAULT '',
            symbol_qualified TEXT DEFAULT ''
        );
        CREATE TABLE guardrail_rules (
            rule_id TEXT PRIMARY KEY, category TEXT NOT NULL
        );
        CREATE TABLE guardrail_findings (
            workspace_id INTEGER NOT NULL DEFAULT 0, rule_id TEXT NOT NULL,
            file_path TEXT NOT NULL, symbol_hash TEXT DEFAULT '',
            severity TEXT NOT NULL DEFAULT 'warn',
            status TEXT NOT NULL DEFAULT 'open', message TEXT DEFAULT '',
            detected_at REAL NOT NULL
        );
        CREATE TABLE test_case_relations (
            workspace_id INTEGER NOT NULL, test_fn_id INTEGER NOT NULL,
            tested_fn_id INTEGER NOT NULL, match_method TEXT NOT NULL,
            confidence TEXT NOT NULL, detected_at REAL NOT NULL
        );
        CREATE TABLE test_runs (
            workspace_id INTEGER NOT NULL, test_fn_id INTEGER NOT NULL,
            test_name TEXT NOT NULL, status TEXT NOT NULL,
            duration_ms REAL DEFAULT 0, error_message TEXT DEFAULT '',
            error_type TEXT DEFAULT '', run_at REAL NOT NULL
        );
        """
    )
    source = workspace_root / "a.py"
    source.write_text(
        "def alpha():\n    # TODO: validate input\n    return beta()\n\n"
        "def beta():\n    return 1\n\n"
        "def test_alpha():\n    return alpha()\n",
        encoding="utf-8",
    )
    conn.execute("INSERT INTO workspaces VALUES (?, ?)", (workspace_id, str(workspace_root)))
    conn.execute(
        "INSERT INTO file_instances VALUES (1, ?, 'a.py', ?, 'active')",
        (workspace_id, str(source)),
    )
    conn.executemany(
        "INSERT INTO symbols VALUES (?, 1, ?, 'fn', ?, ?, 'a', 'public', ?, ?, 0, 10, ?, 0, 'absent', 0)",
        [
            (1, "hash-alpha", "alpha", "a.alpha", 1, 3, "alpha()"),
            (2, "hash-beta", "beta", "a.beta", 5, 6, "beta()"),
            (3, "hash-test", "test_alpha", "a.test_alpha", 8, 10, "test_alpha()"),
        ],
    )
    conn.execute("INSERT INTO calls VALUES (1, 2, 'beta', 3, 0)")
    conn.execute("INSERT INTO file_versions VALUES (10, 1, 1)")
    conn.executemany(
        "INSERT INTO symbol_contents VALUES (?, ?, 'fn', ?, ?, 0, '')",
        [
            ("hash-alpha", "alpha", "def alpha(): TODO", "alpha()"),
            ("hash-beta", "beta", "def beta(): return 1", "beta()"),
            ("hash-test", "test_alpha", "def test_alpha(): alpha()", "test_alpha()"),
        ],
    )
    conn.executemany(
        "INSERT INTO file_symbol_versions VALUES (10, ?, ?, 'a', ?, ?, 0, 0)",
        [
            ("hash-alpha", "a.alpha", 1, 3),
            ("hash-beta", "a.beta", 5, 6),
            ("hash-test", "a.test_alpha", 8, 10),
        ],
    )
    conn.execute("INSERT INTO call_versions VALUES (10, 'a.alpha', 'hash-alpha', 'beta', 'a', 'a.beta', 'a.py', 3)")
    conn.execute(
        "INSERT INTO semgrep_findings VALUES (1, 'python.todo', 'TODO rule', 'WARNING', 'HIGH', ?, 2, 2, '# TODO: validate input', '', 'a.alpha')",
        ("remove TODO",),
    )
    conn.execute("INSERT INTO guardrail_rules VALUES ('guard.secret', 'security')")
    conn.execute(
        "INSERT INTO guardrail_findings VALUES (?, 'guard.secret', 'a.py', 'hash-alpha', 'warn', 'open', 'secret-like value', 1000.0)",
        (workspace_id,),
    )
    conn.execute("INSERT INTO test_case_relations VALUES (?, 3, 1, 'direct_call', 'high', 1000.0)", (workspace_id,))
    conn.execute("INSERT INTO test_runs VALUES (?, 3, 'test_alpha', 'passed', 12.5, '', '', 1001.0)", (workspace_id,))
    conn.commit()
    conn.close()


@pytest.fixture
def real_rust_daemon(tmp_path):
    binary = _fresh_daemon_binary()
    socket_path = tmp_path / "callwarden.sock"
    registry_path = tmp_path / "registry.db"
    data_root = tmp_path / "data"
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "CW_DAEMON_SOCKET": str(socket_path),
            "CW_DAEMON_REGISTRY_DB": str(registry_path),
            "CW_DAEMON_DATA_ROOT": str(data_root),
            "CW_DAEMON_TASK_DB": str(tmp_path / "task.db"),
            "PYTHONHOME": "",
            "PYTHONPATH": str(_repo_root()),
        }
    )
    log_path = tmp_path / "daemon.log"
    with log_path.open("w", encoding="utf-8") as log:
        daemon = subprocess.Popen(
            [str(binary), "--socket", str(socket_path), "--registry", str(registry_path), "serve"],
            cwd=_repo_root(),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if daemon.poll() is not None:
                pytest.fail(f"Rust daemon exited early:\n{log_path.read_text(encoding='utf-8', errors='replace')}")
            if socket_path.exists():
                break
            time.sleep(0.2)
        else:
            pytest.fail(f"Rust daemon did not create UDS:\n{log_path.read_text(encoding='utf-8', errors='replace')}")
        yield UnixDaemonRpcClient(str(socket_path)), log_path
    finally:
        if daemon.poll() is None:
            daemon.terminate()
            try:
                daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=10)


def test_real_daemon_uds_covers_all_w23_query_methods(real_rust_daemon, tmp_path):
    client, log_path = real_rust_daemon
    workspace = client.call("workspace.register", {"client_view_root": str(tmp_path)})
    workspace_id = workspace["workspace_id"]
    workspace_instance_id = workspace["workspace_instance_id"]
    snapshot_db = tmp_path / "snapshot.db"
    _create_snapshot_db(snapshot_db, workspace_id, tmp_path)

    published = client.publish_snapshot(workspace_instance_id, str(snapshot_db), "w2.3-p1-b")
    assert published["generation"] == 1

    file_result = client.call(
        "query.file", {"workspace_instance_id": workspace_instance_id, "file_path": "a.py"}
    )
    assert [item["qualified_name"] for item in file_result] == [
        "a.alpha", "a.beta", "a.test_alpha"
    ]

    # query.symbol（W2.3 五类之一）：精确符号详情成功路径
    symbol = client.call(
        "query.symbol",
        {"workspace_instance_id": workspace_instance_id, "qualified_name": "a.alpha"},
    )
    assert symbol["qualified_name"] == "a.alpha"
    assert symbol["name"] == "alpha"
    assert symbol["kind"] == "fn"
    assert symbol["file_path"] == "a.py"
    assert len(symbol["calls_out"]) == 1
    assert symbol["calls_out"][0]["target_name"] == "a.beta"

    location = client.call(
        "query.symbol_location",
        {"workspace_instance_id": workspace_instance_id, "name": "alpha", "file_path": "a.py"},
    )
    assert location["qualified_name"] == "a.alpha"

    grep_result = client.call(
        "query.grep",
        {
            "workspace_instance_id": workspace_instance_id,
            "patterns": ["TODO"],
            "fixed": True,
            "limit": 10,
        },
    )
    assert "a.py:2" in grep_result
    assert "a.alpha" in grep_result

    issues = client.call(
        "query.issues",
        {"workspace_instance_id": workspace_instance_id, "qualified_name": "a.alpha"},
    )
    assert {item["source"] for item in issues} == {"semgrep", "guardrail"}

    tests = client.call(
        "query.tests",
        {"workspace_instance_id": workspace_instance_id, "qualified_name": "a.alpha"},
    )
    assert len(tests) == 1
    assert tests[0]["test_qualified_name"] == "a.test_alpha"

    with pytest.raises(DaemonRemoteError) as denied:
        client.call("query.grep", {"workspace_instance_id": "unknown-workspace", "patterns": ["TODO"]})
    assert denied.value.code in {"workspace_forbidden", "workspace_not_found"}

    with pytest.raises(DaemonRemoteError) as malformed:
        client.call("query.grep", {"workspace_instance_id": workspace_instance_id, "patterns": []})
    assert malformed.value.code == "invalid_params"

    # 未知 symbol：query.symbol 返回 null（非结构化错误，但必须不泄露数据）
    missing_symbol = client.call(
        "query.symbol",
        {"workspace_instance_id": workspace_instance_id, "qualified_name": "a.does_not_exist"},
    )
    assert missing_symbol is None or missing_symbol == {}

    # 非法/越界路径：query.file 越出 workspace 根必须 fail-closed。
    # 实现行为：越界绝对路径经 normalize 后与 rel_path 精确匹配不命中 → 空数组，
    # 不泄露其他路径数据。若实现改为抛结构化错误同样满足 fail-closed。
    escaped = client.call(
        "query.file",
        {"workspace_instance_id": workspace_instance_id, "file_path": "/etc/passwd"},
    )
    assert escaped == []

    # 未发布 snapshot 的 workspace 必须 fail-closed（snapshot_not_ready）
    orphan_root = tmp_path / "orphan"
    orphan_root.mkdir()
    orphan_ws = client.call("workspace.register", {"client_view_root": str(orphan_root)})
    with pytest.raises(DaemonRemoteError) as not_ready:
        client.call(
            "query.symbol",
            {
                "workspace_instance_id": orphan_ws["workspace_instance_id"],
                "qualified_name": "a.alpha",
            },
        )
    assert not_ready.value.code == "snapshot_not_ready"

    assert log_path.read_text(encoding="utf-8", errors="replace").count("ready") >= 1
