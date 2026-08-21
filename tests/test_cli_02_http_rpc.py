"""CLI-02 (A′ control_plane) `cw search` HTTP RPC fixture 矩阵测试。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_02_http_rpc.py）
要求的 HTTP RPC 层场景：
  success（live daemon + 发布 fixture snapshot 的 HTTP round-trip）、
  authority failure（伪造 workspace_instance_id 被 ACL 拒绝）、
  daemon unavailable（端点不可达 fail-closed E_HTTP_DAEMON_UNAVAILABLE）。

与 test_cli02_search_daemon_only.py 互补：本文件聚焦 HTTP RPC 传输层与
authority 边界；那文件覆盖 Python thin client 格式化与 has_comment 兼容字段。
两者共同证明 Rust daemon 为 query.search 权威、Python 无 SQLite fallback。
"""

import os

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.server.daemon_protocol import DaemonRemoteError


_SNAPSHOT_SCHEMA = """
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
    end_line INTEGER NOT NULL, start_col INTEGER, end_col INTEGER,
    signature TEXT, has_comment INTEGER, comment_status TEXT,
    comment_content TEXT, depth INTEGER NOT NULL
);
CREATE TABLE calls (
    caller_id INTEGER NOT NULL, callee_id INTEGER NOT NULL,
    callee_name TEXT NOT NULL, call_line INTEGER NOT NULL,
    is_cross_file INTEGER NOT NULL
);
CREATE TABLE symbol_contents (
    content_hash TEXT PRIMARY KEY, name TEXT NOT NULL,
    kind TEXT NOT NULL, content TEXT NOT NULL, signature TEXT,
    has_comment INTEGER, comment_content TEXT
);
"""


def _build_snapshot_db(tmp_path: str, ws_id: int) -> str:
    root = os.path.join(str(tmp_path), "ws1")
    os.makedirs(root, exist_ok=True)
    db = os.path.join(str(tmp_path), "snap.db")
    import sqlite3
    conn = sqlite3.connect(db)
    conn.executescript(_SNAPSHOT_SCHEMA)
    conn.execute("INSERT INTO workspaces VALUES (?, ?)", (ws_id, root))
    conn.execute(
        "INSERT INTO file_instances VALUES (1, ?, 'a.py', ?, 'active')",
        (ws_id, os.path.join(root, "a.py")),
    )
    conn.execute(
        "INSERT INTO symbols (id, file_instance_id, symbol_hash, kind, name, "
        "qualified_name, module_path, visibility, start_line, end_line, "
        "start_col, end_col, signature, has_comment, comment_status, "
        "comment_content, depth) VALUES (1,1,'h1','fn','alpha','a.alpha',"
        "'a','public',1,3,0,0,'alpha()',0,'absent','',0)"
    )
    conn.execute(
        "INSERT INTO symbol_contents VALUES ('h1','alpha','fn',"
        "'def alpha(): pass','alpha()',0,'')"
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def published_workspace(tmp_path):
    c = HttpDaemonRpcClient()
    try:
        c.health()
    except Exception:
        pytest.skip("daemon 未运行，跳过 HTTP RPC round-trip 用例")
    root = os.path.join(str(tmp_path), "ws1")
    os.makedirs(root, exist_ok=True)
    ws = c.call("workspace.register", {"client_view_root": root})
    ws_id = ws["workspace_id"]
    inst = ws["workspace_instance_id"]
    db = _build_snapshot_db(str(tmp_path), ws_id=ws_id)
    c.call("snapshot.publish", {
        "workspace_instance_id": inst,
        "build_context_hash": "cli02-http-rpc",
        "db_path": db,
    })
    return c, inst


def test_http_rpc_search_success(published_workspace):
    """HTTP RPC query.search success：Rust daemon 为权威，返回列表。"""
    c, inst = published_workspace
    r = c.call("query.search", {
        "workspace_instance_id": inst,
        "query": "alpha",
        "kind": None,
        "limit": 10,
    })
    assert isinstance(r, list) and len(r) >= 1
    assert r[0]["qualified_name"] == "a.alpha"


def test_http_rpc_search_authority_failure(published_workspace):
    """HTTP RPC authority failure：伪造 workspace_instance_id 被 ACL 拒绝。"""
    c, _ = published_workspace
    with pytest.raises(DaemonRemoteError) as ei:
        c.call("query.search", {
            "workspace_instance_id": "NO-SUCH-WS-CLI02-RPC",
            "query": "alpha",
            "kind": None,
            "limit": 10,
        })
    assert getattr(ei.value, "code", "") in (
        "E_WORKSPACE_NOT_OWNED", "workspace_not_owned",
        "E_WORKSPACE_NOT_FOUND", "workspace_not_found", "invalid_params",
    )


def test_http_rpc_search_daemon_unavailable_fail_closed():
    """HTTP RPC daemon unavailable：E_HTTP_DAEMON_UNAVAILABLE fail-closed。"""
    from callwarden.config import get_http_authority_id
    c = HttpDaemonRpcClient(
        endpoint="http://127.0.0.1:9", authority_id=get_http_authority_id()
    )
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("query.search", {
            "workspace_instance_id": "ws-cli02-rpc-x",
            "query": "alpha",
            "kind": None,
            "limit": 10,
        })
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)
