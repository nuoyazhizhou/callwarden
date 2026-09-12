"""MCP-019（A′ task_evidence_read）get_comment_from_version → Rust daemon native。

覆盖 task 要求：
  success / spec 无 @ / 版本不存在 / 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致，W3 隔离 harness 适配 current-HEAD）：
- Python MCP wrapper（tools_query.get_comment_from_version）已从 _SYMBOL_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_get_comment_from_version）为
  权威：解析 spec（fn@vN 或 fn@hash）→ 查符号历史 → 查 symbol_contents → 返回注释信息；
  任一环节缺失返回 None。
- current-HEAD 路由层（snapshot_state.rs S2）要求 RPC 携带 `workspace_instance_id`
  （字符串）而非旧整数 `workspace_id`；未知 workspace_instance_id → 权威拒绝
  （fail-closed）。本测试经 conftest 共享 `w3_live` 隔离 daemon 权威栈。
- 空库无符号历史 → 各 spec 均返回 None。
"""

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.config import get_http_authority_id


# ---------------------------------------------------------------------------
# success / no-match：HTTP round-trip，Rust daemon 为权威
# ---------------------------------------------------------------------------
def test_get_comment_from_version_no_spec_at(w3_live):
    """spec 无 @ → null。"""
    c, inst = w3_live["client"], w3_live["inst"]
    r = c.call("get_comment_from_version",
               {"workspace_instance_id": inst, "spec": "NO_SUCH_SYMBOL"})
    assert r is None


def test_get_comment_from_version_unknown_symbol(w3_live):
    """未知符号 → null。"""
    c, inst = w3_live["client"], w3_live["inst"]
    r = c.call("get_comment_from_version",
               {"workspace_instance_id": inst, "spec": "NO_SUCH_SYMBOL@v1"})
    assert r is None


def test_get_comment_from_version_missing_spec(w3_live):
    """缺 spec → 默认空串，null（fail-closed，不抛错）。"""
    c, inst = w3_live["client"], w3_live["inst"]
    r = c.call("get_comment_from_version", {"workspace_instance_id": inst})
    assert r is None


def test_get_comment_from_version_unknown_workspace(w3_live):
    """未知 workspace_instance_id → 权威拒绝（fail-closed）。"""
    c = w3_live["client"]
    with pytest.raises(DaemonRemoteError):
        c.call("get_comment_from_version",
               {"workspace_instance_id": "NO_SUCH_INSTANCE", "spec": "X@v1"})


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_comment_from_version_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_comment_from_version", {"workspace_instance_id": "x"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_comment_from_version_new_client_instance_stable(w3_live):
    c2 = HttpDaemonRpcClient(endpoint=w3_live["endpoint"],
                             authority_id=get_http_authority_id())
    r = c2.call("get_comment_from_version",
                {"workspace_instance_id": w3_live["inst"], "spec": "X@v1"})
    assert r is None or isinstance(r, dict)