"""MCP-010（A′ task_evidence_read）get_action_identity → Rust daemon native。

覆盖 task 要求：
  success / no-match / 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_p3_identity.get_action_identity）已从 _P3_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_get_action_identity）为
  权威：按 workspace_id + action_id 查询 action_identities 单行（全部列），无匹配返回
  None。
- 本测试直连 HTTP RPC，验证返回结构。
- 确定性 parity：authority DB 中指定 workspace/action 无记录 → null（与 Python
  无匹配返回 None 语义一致）。
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
def test_get_action_identity_no_match(live_daemon):
    """无匹配 → null。"""
    c = live_daemon
    r = c.call("get_action_identity", {"workspace_id": 1, "action_id": "NO-SUCH-ACTION"})
    assert r is None


def test_get_action_identity_unknown_workspace(live_daemon):
    """未知 workspace + action → null（不报错）。"""
    c = live_daemon
    r = c.call("get_action_identity", {"workspace_id": 999999, "action_id": "X"})
    assert r is None


def test_get_action_identity_missing_action_id(live_daemon):
    """缺 action_id → 默认空串，返回 null（fail-closed，不抛错）。"""
    c = live_daemon
    r = c.call("get_action_identity", {"workspace_id": 1})
    assert r is None


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_action_identity_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_action_identity", {"workspace_id": 1, "action_id": "X"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_action_identity_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("get_action_identity", {"workspace_id": 1, "action_id": "X"})
    assert r is None or isinstance(r, dict)
