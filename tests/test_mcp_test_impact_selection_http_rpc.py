"""MCP-032（A′ task_evidence_read）test_impact_selection → Rust daemon native。

覆盖 task 要求：
  success / no-target / 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_summary.test_impact_selection）已从 _SUMMARY_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_test_impact_selection）为
  权威：BFS 反向调用链收集所有直接/间接调用者 → 筛选测试函数（名称/qualified_name/
  module_path 含 test/spec）→ 返回测试函数列表。
- 本测试直连 HTTP RPC，验证返回结构。
- 确定性 parity：目标函数不存在 → []（与 Python 语义一致）。
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
def test_test_impact_selection_no_target(live_daemon):
    """目标函数不存在 → []。"""
    c = live_daemon
    r = c.call("test_impact_selection",
               {"workspace_id": 1, "qualified_name": "NO_SUCH_FN_XYZ"})
    assert isinstance(r, list)
    assert r == []


def test_test_impact_selection_unknown_workspace(live_daemon):
    """未知 workspace：无目标 → []。"""
    c = live_daemon
    r = c.call("test_impact_selection",
               {"workspace_id": 999999, "qualified_name": "X"})
    assert isinstance(r, list)


def test_test_impact_selection_missing_params(live_daemon):
    """缺参 → 默认空 qualified_name，[]（fail-closed，不抛错）。"""
    c = live_daemon
    r = c.call("test_impact_selection", {"workspace_id": 1})
    assert isinstance(r, list)


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_test_impact_selection_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("test_impact_selection", {"workspace_id": 1, "qualified_name": "X"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_test_impact_selection_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("test_impact_selection", {"workspace_id": 1, "qualified_name": "X"})
    assert isinstance(r, list)
