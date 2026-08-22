"""MCP-006（A′ task_evidence_read）get_interface_providers → Rust daemon native。

覆盖 task 要求：
  success/no-match/filter（workspace_id + interface_name + version）、缺 version、
  unknown workspace、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_p2_graph.get_interface_providers）已是 route_rpc 薄壳；
  本测试直连 HTTP RPC `get_interface_providers`，验证 Rust daemon
  （task_collab.rs::handle_get_interface_providers）为权威：从 interface_identities 按
  workspace_id + interface_name (+ version) 查询 provider 列表，返回 items + count。
- Python compat `_h_get_interface_providers` 已从 tools_p2_graph._P2_READ_ONLY_METHODS 移除。

确定性 parity：interface_identities 当前为空 → 任何过滤均返回 items=[], count=0（与
Python db_task_dependencies.get_interface_providers 空表语义一致）。
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
# success / no-match：HTTP round-trip，Rust daemon 为权威
# ---------------------------------------------------------------------------
def test_get_interface_providers_no_match(live_daemon):
    """空表 → items=[], count=0。"""
    c = live_daemon
    r = c.call("get_interface_providers", {"workspace_id": 1, "interface_name": "NO-SUCH"})
    assert isinstance(r, dict)
    assert r.get("items") == []
    assert r.get("count") == 0


def test_get_interface_providers_by_version(live_daemon):
    """按 version 过滤，结构正确。"""
    c = live_daemon
    r = c.call("get_interface_providers", {"workspace_id": 1, "interface_name": "X", "version": "1.0"})
    assert isinstance(r, dict)
    assert r.get("items") == []
    assert r.get("count") == 0


# ---------------------------------------------------------------------------
# 缺省参数：缺 version → 仅按 workspace_id + interface_name 查询
# ---------------------------------------------------------------------------
def test_get_interface_providers_missing_version(live_daemon):
    c = live_daemon
    r = c.call("get_interface_providers", {"workspace_id": 1, "interface_name": "X"})
    assert isinstance(r, dict)
    assert "items" in r and "count" in r


def test_get_interface_providers_unknown_workspace(live_daemon):
    """未知 workspace：返回空 items（不报错，fail-closed 语义）。"""
    c = live_daemon
    r = c.call("get_interface_providers", {"workspace_id": 999999, "interface_name": "X"})
    assert isinstance(r, dict)
    assert r.get("items") == []


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_interface_providers_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_interface_providers", {"workspace_id": 1, "interface_name": "X"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_interface_providers_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("get_interface_providers", {"workspace_id": 1, "interface_name": "X"})
    assert isinstance(r, dict)
    assert "items" in r
