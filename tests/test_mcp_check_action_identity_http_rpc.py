"""MCP-011（A′ task_evidence_read）check_action_identity → Rust daemon native。

覆盖 task 要求：
  success（四字段齐全）/ require_role 匹配与不匹配 / 缺字段 / 非法 JSON /
  缺 identity 参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_p3_identity.check_action_identity）已从 _P3_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_check_action_identity）为
  权威：解析 identity JSON 字符串 → 校验 agent_id/session_id/model_id/role 四字段 +
  require_role 匹配 → 返回 {"valid": bool, "reason": {...}}。
- 纯逻辑校验（无 DB 查询），返回结构与 Python _h_check_action_identity 一致。
"""

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.config import get_http_authority_id


def _valid_identity():
    return '{"agent_id":"a1","session_id":"s1","model_id":"m1","role":"implementer"}'


@pytest.fixture()
def live_daemon():
    c = HttpDaemonRpcClient()
    try:
        c.health()
    except Exception:
        pytest.skip("daemon 未运行（无 HTTP endpoint），跳过 live 用例")
    return c


# ---------------------------------------------------------------------------
# success / 校验分支：HTTP round-trip，Rust daemon 为权威
# ---------------------------------------------------------------------------
def test_check_action_identity_valid(live_daemon):
    """四字段齐全 → valid=true, reason.code=OK。"""
    c = live_daemon
    r = c.call("check_action_identity", {"identity": _valid_identity()})
    assert isinstance(r, dict)
    assert r.get("valid") is True
    assert r["reason"].get("code") == "OK"


def test_check_action_identity_role_match(live_daemon):
    """require_role 匹配 → valid=true。"""
    c = live_daemon
    r = c.call("check_action_identity",
               {"identity": _valid_identity(), "require_role": "implementer"})
    assert r.get("valid") is True


def test_check_action_identity_role_mismatch(live_daemon):
    """require_role 不匹配 → valid=false, E_IDENTITY_ROLE_MISMATCH。"""
    c = live_daemon
    r = c.call("check_action_identity",
               {"identity": _valid_identity(), "require_role": "reviewer"})
    assert r.get("valid") is False
    assert r["reason"].get("code") == "E_IDENTITY_ROLE_MISMATCH"
    assert r["reason"].get("expected_role") == "reviewer"
    assert r["reason"].get("actual_role") == "implementer"


def test_check_action_identity_missing_field(live_daemon):
    """缺字段 → valid=false, E_IDENTITY_INCOMPLETE。"""
    c = live_daemon
    r = c.call("check_action_identity",
               {"identity": '{"agent_id":"a1","session_id":"s1","model_id":"m1"}'})
    assert r.get("valid") is False
    assert r["reason"].get("code") == "E_IDENTITY_INCOMPLETE"


def test_check_action_identity_invalid_json(live_daemon):
    """非法 JSON → valid=false, E_IDENTITY_INCOMPLETE。"""
    c = live_daemon
    r = c.call("check_action_identity", {"identity": "not-json"})
    assert r.get("valid") is False
    assert r["reason"].get("code") == "E_IDENTITY_INCOMPLETE"


def test_check_action_identity_empty_identity(live_daemon):
    """缺 identity 参数 → valid=false, E_IDENTITY_INCOMPLETE。"""
    c = live_daemon
    r = c.call("check_action_identity", {})
    assert r.get("valid") is False
    assert r["reason"].get("code") == "E_IDENTITY_INCOMPLETE"


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_check_action_identity_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("check_action_identity", {"identity": _valid_identity()})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_check_action_identity_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("check_action_identity", {"identity": _valid_identity()})
    assert isinstance(r, dict)
    assert "valid" in r and "reason" in r
