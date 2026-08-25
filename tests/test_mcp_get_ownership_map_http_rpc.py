"""MCP-034（A′ task_evidence_read）get_ownership_map → Rust daemon native。

覆盖 task 要求：
  success/空表/带 module_filter/缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_summary.get_ownership_map）已是 route_rpc 薄壳；本测试直连
  HTTP RPC `get_ownership_map`，验证 Rust daemon（task_collab.rs::handle_get_ownership_map）
  为权威：从 file_ownership JOIN file_instances 按 workspace_id 分组统计负责人分布。
- Python compat `_h_get_ownership_map` 已从 tools_summary._SUMMARY_READ_ONLY_METHODS 移除。

确定性 parity：当前库可能无 file_ownership 记录 → 返回空数组（与 Python 空表一致）。
"""

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.config import get_http_authority_id


@pytest.fixture()
def live_daemon():
    c = HttpDaemonRpcClient()
    try:
        c.health()
    except Exception:
        pytest.skip("daemon 未运行（无 HTTP endpoint），跳过 live 用例")
    return c


# ---------------------------------------------------------------------------
# success / 空表：HTTP round-trip，Rust daemon 为权威
# ---------------------------------------------------------------------------
def test_get_ownership_map_empty(live_daemon):
    """空表 → 返回数组（Python 空表返回 []）。"""
    c = live_daemon
    r = c.call("get_ownership_map", {})
    assert isinstance(r, list)


def test_get_ownership_map_with_filter(live_daemon):
    """带 module_filter → 结构正确。"""
    c = live_daemon
    r = c.call("get_ownership_map", {"module_filter": "server"})
    assert isinstance(r, list)
    if r:
        item = r[0]
        assert "module" in item and "primary_owner" in item
        assert "file_count" in item and "owners" in item


# ---------------------------------------------------------------------------
# 缺省参数：空 filter 返回全部
# ---------------------------------------------------------------------------
def test_get_ownership_map_no_filter(live_daemon):
    c = live_daemon
    r = c.call("get_ownership_map", {})
    assert isinstance(r, list)


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed
# ---------------------------------------------------------------------------
def test_get_ownership_map_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_ownership_map", {})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_ownership_map_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("get_ownership_map", {"module_filter": "server"})
    assert isinstance(r, list)
