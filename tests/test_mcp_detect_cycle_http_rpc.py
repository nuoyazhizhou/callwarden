"""MCP-007（A′ task_evidence_read）detect_cycle → Rust daemon native。

覆盖 task 要求：
  success / no-cycle / workspace 隔离、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_p2_graph.detect_cycle）已从 _P2_READ_ONLY_METHODS 移除 compat
  注册，改由 Rust daemon（task_collab.rs::handle_detect_cycle）为权威：从 dependency_edges
  取 workspace 内 is_hard=1 边，DFS 三色检测环 + BFS 最短 cycle path。
- 本测试直连 HTTP RPC `detect_cycle`，验证返回结构（has_cycle / cycle_path / checked_nodes）。
- 确定性 parity：authority DB 中检测用 workspace 若无硬依赖环 → has_cycle=false,
  cycle_path=[]（与 Python db_task_dependencies.detect_cycle 空图/无环语义一致）。
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
# success / no-cycle：HTTP round-trip，Rust daemon 为权威
# ---------------------------------------------------------------------------
def test_detect_cycle_no_cycle_workspace(live_daemon):
    """无环 workspace → has_cycle=false, cycle_path=[]，结构完整。"""
    c = live_daemon
    r = c.call("detect_cycle", {"workspace_id": 1})
    assert isinstance(r, dict)
    assert r.get("has_cycle") is False
    assert r.get("cycle_path") == []
    assert isinstance(r.get("checked_nodes"), int)


def test_detect_cycle_missing_workspace_defaults_zero(live_daemon):
    """缺 workspace_id → 默认 0，返回结构仍完整（fail-closed，不抛错）。"""
    c = live_daemon
    r = c.call("detect_cycle", {})
    assert isinstance(r, dict)
    assert "has_cycle" in r and "cycle_path" in r and "checked_nodes" in r


def test_detect_cycle_unknown_workspace(live_daemon):
    """未知 workspace：无硬边 → has_cycle=false（不报错）。"""
    c = live_daemon
    r = c.call("detect_cycle", {"workspace_id": 999999})
    assert isinstance(r, dict)
    assert r.get("has_cycle") is False
    assert r.get("cycle_path") == []


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_detect_cycle_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("detect_cycle", {"workspace_id": 1})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_detect_cycle_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("detect_cycle", {"workspace_id": 1})
    assert isinstance(r, dict)
    assert "has_cycle" in r
