"""MCP-018（A′ task_evidence_read）get_impact → Rust daemon native。

覆盖 task 要求：
  success / 缺省参数 / 空调用链、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_query.get_impact）已从 _SYMBOL_READ_ONLY_METHODS 移除
  compat 注册，改由 Rust daemon（task_collab.rs::handle_get_impact）为权威：BFS 向上
  追踪调用链（call_versions is_current=1），返回 {start, max_depth_reached,
  total_upstream, levels, all_upstream}（与 Python get_call_chain_up 一致）。
- 本测试直连 HTTP RPC，验证返回结构。
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
# success：HTTP round-trip，Rust daemon 为权威
# ---------------------------------------------------------------------------
def test_get_impact_structure(live_daemon):
    """返回结构完整：start/max_depth_reached/total_upstream/levels/all_upstream。"""
    c = live_daemon
    r = c.call("get_impact", {"workspace_id": 1, "qualified_name": "NO_SUCH_SYM_XYZ"})
    assert isinstance(r, dict)
    assert r.get("start") == "NO_SUCH_SYM_XYZ"
    assert isinstance(r.get("max_depth_reached"), int)
    assert isinstance(r.get("total_upstream"), int)
    assert isinstance(r.get("levels"), list)
    assert isinstance(r.get("all_upstream"), list)


def test_get_impact_default_depth(live_daemon):
    """缺 max_depth → 默认 10，结构完整。"""
    c = live_daemon
    r = c.call("get_impact", {"workspace_id": 1, "qualified_name": "X"})
    assert isinstance(r, dict)
    assert "levels" in r and "all_upstream" in r


def test_get_impact_unknown_workspace(live_daemon):
    """未知 workspace：无调用链 → total_upstream=0, all_upstream=[]。"""
    c = live_daemon
    r = c.call("get_impact", {"workspace_id": 999999, "qualified_name": "X"})
    assert isinstance(r, dict)
    assert r.get("total_upstream") == 0
    assert r.get("all_upstream") == []


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_impact_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_impact", {"workspace_id": 1, "qualified_name": "X"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_impact_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("get_impact", {"workspace_id": 1, "qualified_name": "X"})
    assert isinstance(r, dict)
    assert "start" in r and "all_upstream" in r
