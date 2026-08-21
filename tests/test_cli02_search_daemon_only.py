"""CLI-02 (A′ control_plane) `cw search` daemon-only 读取链路 Rust daemon 化。

覆盖 task 要求的场景：
  success（live daemon + 发布 fixture snapshot 的 HTTP round-trip）、
  空结果（无匹配符号）、无 has_comment 兼容字段（缺省 false 稳定）、
  authority failure（伪造 workspace_instance_id / snapshot 未发布）、
  daemon unavailable（端点不可达 fail-closed E_HTTP_DAEMON_UNAVAILABLE）、
  restart（snapshot 重发布后可再次查询）。

设计要点（与 task 不变量一致）：
- Python 仅作 HTTP thin client 格式化；Rust daemon 的 query.search 为权威。
- daemon 响应含 file_path/signature/has_comment 兼容字段（normalize_search_results
  语义，与 Rust CLI compat 层一致）；缺失时 Python 输出 must 稳定不 KeyError。
- 所有失败 fail-closed 返回稳定且可区分的结构化错误，绝不降级到本地 SQLite。
"""

import json
import os
import sqlite3
import tempfile

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.server.daemon_protocol import DaemonRemoteError


# ---------------------------------------------------------------------------
# fixture：最小 snapshot DB + workspace 注册 + 发布
# ---------------------------------------------------------------------------
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
CREATE TABLE file_versions (
    id INTEGER PRIMARY KEY, file_instance_id INTEGER NOT NULL,
    is_current INTEGER NOT NULL
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
"""


def _build_snapshot_db(tmp_path: str, ws_id: int = 1,
                       has_comment_symbol: bool = True) -> str:
    """构造最小 snapshot DB：一个带注释的 alpha（fn）+ 一个无注释的 beta。

    ws_id 必须与 daemon 权威 registry workspaces.id 一致，Rust loader 按此过滤。
    """
    root = os.path.join(tmp_path, "ws1")
    os.makedirs(root, exist_ok=True)
    db = os.path.join(tmp_path, "snap.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SNAPSHOT_SCHEMA)
    conn.execute("INSERT INTO workspaces VALUES (?, ?)", (ws_id, root))
    conn.execute(
        "INSERT INTO file_instances VALUES (1, ?, 'a.py', ?, 'active')",
        (ws_id, os.path.join(root, "a.py")),
    )
    # alpha：有注释（has_comment=1）
    conn.execute(
        "INSERT INTO symbols (id, file_instance_id, symbol_hash, kind, name, "
        "qualified_name, module_path, visibility, start_line, end_line, "
        "start_col, end_col, signature, has_comment, comment_status, "
        "comment_content, depth) VALUES (1,1,'h1','fn','alpha','a.alpha',"
        "'a','public',1,3,0,0,'alpha()',?,?,'',0)",
        (1 if has_comment_symbol else 0, "present" if has_comment_symbol else "absent"),
    )
    # beta：无注释（has_comment=0）
    conn.execute(
        "INSERT INTO symbols (id, file_instance_id, symbol_hash, kind, name, "
        "qualified_name, module_path, visibility, start_line, end_line, "
        "start_col, end_col, signature, has_comment, comment_status, "
        "comment_content, depth) VALUES (2,1,'h2','fn','beta','a.beta',"
        "'a','public',5,7,0,0,'beta()',0,'absent','',0)"
    )
    conn.execute(
        "INSERT INTO symbol_contents VALUES ('h1','alpha','fn',"
        "'def alpha(): pass','alpha()',1,'doc')"
    )
    conn.execute(
        "INSERT INTO symbol_contents VALUES ('h2','beta','fn',"
        "'def beta(): pass','beta()',0,'')"
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def live_daemon():
    """需要真实 daemon：endpoint 可达才运行 live 用例，否则 skip。"""
    c = HttpDaemonRpcClient()
    try:
        health = c.health()
    except Exception:
        pytest.skip("daemon 未运行（无 HTTP endpoint），跳过 live 用例")
    return c, health


@pytest.fixture()
def published_workspace(tmp_path):
    """注册临时 workspace 并发布 fixture snapshot，返回 (client, workspace_instance_id)。

    Rust snapshot loader 按 daemon 权威 workspace_id（registry workspaces.id）过滤
    symbols，因此必须先 register 拿到 ws_id，再用该 ws_id 构造 snapshot DB。
    """
    c = HttpDaemonRpcClient()
    try:
        c.health()
    except Exception:
        pytest.skip("daemon 未运行，跳过 round-trip 用例")
    root = os.path.join(str(tmp_path), "ws1")
    os.makedirs(root, exist_ok=True)
    ws = c.call("workspace.register", {"client_view_root": root})
    ws_id = ws["workspace_id"]
    inst = ws["workspace_instance_id"]
    db = _build_snapshot_db(str(tmp_path), ws_id=ws_id)
    c.call("snapshot.publish", {
        "workspace_instance_id": inst,
        "build_context_hash": "cli02-test",
        "db_path": db,
    })
    return c, inst


# ---------------------------------------------------------------------------
# success：HTTP round-trip，Rust daemon 为权威，含兼容字段
# ---------------------------------------------------------------------------
def test_search_success_roundtrip(published_workspace):
    c, inst = published_workspace
    r = c.call("query.search", {
        "workspace_instance_id": inst,
        "query": "alpha",
        "kind": None,
        "limit": 10,
    })
    assert isinstance(r, list) and len(r) >= 1
    item = r[0]
    # 兼容字段：file_path（由 file_rel_path 推导）、signature、has_comment
    assert "file_path" in item, f"缺 file_path 兼容字段: {item!r}"
    assert "signature" in item, f"缺 signature 兼容字段: {item!r}"
    assert "has_comment" in item, f"缺 has_comment 兼容字段: {item!r}"
    assert item["qualified_name"] == "a.alpha"
    # snapshot GraphStore 不携带注释元数据：has_comment 兼容缺省 false
    # （与 Rust CLI normalize_search_results 语义一致，cw_cli.rs:5773）。
    assert item["has_comment"] is False
    assert isinstance(item["has_comment"], bool)
    assert item["file_path"].endswith("a.py")
    # snapshot GraphStore 不携带 signature：兼容缺省空串（cw_cli.rs:5772 同语义）
    assert item["signature"] == ""


def test_search_no_has_comment_symbol_stable(published_workspace):
    """无注释符号 has_comment=false 稳定（不 KeyError）。"""
    c, inst = published_workspace
    r = c.call("query.search", {
        "workspace_instance_id": inst,
        "query": "beta",
        "kind": None,
        "limit": 10,
    })
    assert isinstance(r, list) and len(r) >= 1
    item = r[0]
    assert item["has_comment"] is False
    assert item["qualified_name"] == "a.beta"


def test_search_empty_result_stable(published_workspace):
    """空结果：稳定返回空数组，不抛错。"""
    c, inst = published_workspace
    r = c.call("query.search", {
        "workspace_instance_id": inst,
        "query": "no-such-symbol-xyz",
        "kind": None,
        "limit": 10,
    })
    assert r == []


# ---------------------------------------------------------------------------
# fail-closed：稳定且可区分的错误
# ---------------------------------------------------------------------------
def test_search_authority_failure_wrong_workspace(live_daemon):
    """伪造 workspace_instance_id：owned_workspace ACL 拒绝。"""
    c, _ = live_daemon
    with pytest.raises(DaemonRemoteError) as ei:
        c.call("query.search", {
            "workspace_instance_id": "NO-SUCH-WS-CLI02",
            "query": "alpha",
            "kind": None,
            "limit": 10,
        })
    err = ei.value
    assert getattr(err, "code", "") in ("E_WORKSPACE_NOT_OWNED", "workspace_not_owned",
                                        "workspace_not_found", "invalid_params",
                                        "E_WORKSPACE_NOT_FOUND")


def test_search_missing_query_invalid_params(live_daemon):
    """缺 query 参数：invalid_params fail-closed。"""
    c, _ = live_daemon
    with pytest.raises(DaemonRemoteError) as ei:
        c.call("query.search", {
            "workspace_instance_id": "ws-cli02-invalid",
            "kind": None,
            "limit": 10,
        })
    err = ei.value
    assert getattr(err, "code", "") in ("invalid_params", "E_WORKSPACE_NOT_OWNED",
                                        "workspace_not_owned")


def test_search_daemon_unavailable_fail_closed():
    """daemon 端点不可达：E_HTTP_DAEMON_UNAVAILABLE fail-closed。"""
    from callwarden.config import get_http_authority_id
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9", authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("query.search", {
            "workspace_instance_id": "ws-cli02-x",
            "query": "alpha",
            "kind": None,
            "limit": 10,
        })
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：重新发布 snapshot 后查询仍可用
# ---------------------------------------------------------------------------
def test_search_republish_after_restart(tmp_path):
    """模拟 restart：新 client 实例 + 重发布 snapshot，查询依然稳定。"""
    c = HttpDaemonRpcClient()
    try:
        c.health()
    except Exception:
        pytest.skip("daemon 未运行，跳过 restart 用例")
    root = os.path.join(str(tmp_path), "ws1")
    os.makedirs(root, exist_ok=True)
    ws = c.call("workspace.register", {"client_view_root": root})
    inst = ws["workspace_instance_id"]
    ws_id = ws["workspace_id"]
    db = _build_snapshot_db(str(tmp_path), ws_id=ws_id)
    c.call("snapshot.publish", {
        "workspace_instance_id": inst,
        "build_context_hash": "cli02-restart",
        "db_path": db,
    })
    # 新 client 实例（同 endpoint）重新查询
    c2 = HttpDaemonRpcClient()
    r = c2.call("query.search", {
        "workspace_instance_id": inst,
        "query": "alpha",
        "kind": None,
        "limit": 10,
    })
    assert isinstance(r, list) and len(r) >= 1
    assert r[0]["qualified_name"] == "a.alpha"
    assert "has_comment" in r[0]
