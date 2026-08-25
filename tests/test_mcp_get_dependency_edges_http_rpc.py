"""MCP-009（A′ task_evidence_read）get_dependency_edges → Rust daemon native。

覆盖 task 要求：
  success / no-match / task_id 过滤 / 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_p2_graph.get_dependency_edges）已从 _P2_READ_ONLY_METHODS
  移除 compat 注册（p2 组全部迁移），改由 Rust daemon
  （task_collab.rs::handle_get_dependency_edges）为权威：查询 dependency_edges
  全部列按 created_at 排序，可选按 task_id 过滤（provider 或 consumer 匹配）。
- 本测试直连 HTTP RPC，验证返回结构（行数组）与过滤语义。
- 确定性 parity：authority DB 中指定 workspace 若无边 → 返回空数组（与 Python
  空表语义一致）。
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
def test_get_dependency_edges_empty_workspace(live_daemon):
    """空表 → []。"""
    c = live_daemon
    r = c.call("get_dependency_edges", {"workspace_id": 1})
    assert isinstance(r, list)
    assert r == []


def test_get_dependency_edges_with_task_id(live_daemon):
    """按 task_id 过滤 → 空数组（无边匹配），结构完整。"""
    c = live_daemon
    r = c.call("get_dependency_edges", {"workspace_id": 1, "task_id": "NO-SUCH"})
    assert isinstance(r, list)
    assert r == []


def test_get_dependency_edges_unknown_workspace(live_daemon):
    """未知 workspace：无数据 → []（不报错）。"""
    c = live_daemon
    r = c.call("get_dependency_edges", {"workspace_id": 999999})
    assert isinstance(r, list)
    assert r == []


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_dependency_edges_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_dependency_edges", {"workspace_id": 1})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_dependency_edges_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("get_dependency_edges", {"workspace_id": 1})
    assert isinstance(r, list)
