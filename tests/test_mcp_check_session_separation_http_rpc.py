"""MCP-012（A′ task_evidence_read）check_session_separation → Rust daemon native。

覆盖 task 要求：
  success（session 不同）/ session 相同（未分离）/ 非法 JSON / 缺参数、
  daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_p3_identity.check_session_separation）已从
  _P3_READ_ONLY_METHODS 移除 compat 注册，改由 Rust daemon
  （task_collab.rs::handle_check_session_separation）为权威：解析 reviewer/
  implementer_identity JSON → 校验 session 分离 → 返回 {"valid": bool, "reason": {...}}。
- 返回结构与 Python _h_check_session_separation 一致。
"""

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.config import get_http_authority_id


def _rev(session):
    return '{"agent_id":"rev","session_id":"%s","model_id":"m","role":"reviewer"}' % session


def _impl(session):
    return '{"agent_id":"imp","session_id":"%s","model_id":"m","role":"implementer"}' % session


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
def test_check_session_separation_separated(live_daemon):
    """session 不同 → valid=true, reason.code=OK。"""
    c = live_daemon
    r = c.call("check_session_separation",
               {"reviewer_identity": _rev("sess-A"), "implementer_identity": _impl("sess-B")})
    assert isinstance(r, dict)
    assert r.get("valid") is True
    assert r["reason"].get("code") == "OK"


def test_check_session_separation_not_separated(live_daemon):
    """session 相同 → valid=false, E_IDENTITY_SESSION_NOT_SEPARATED。"""
    c = live_daemon
    r = c.call("check_session_separation",
               {"reviewer_identity": _rev("sess-X"), "implementer_identity": _impl("sess-X")})
    assert r.get("valid") is False
    assert r["reason"].get("code") == "E_IDENTITY_SESSION_NOT_SEPARATED"
    assert r["reason"].get("reviewer_session") == "sess-X"
    assert r["reason"].get("implementer_session") == "sess-X"


def test_check_session_separation_missing_session(live_daemon):
    """任一 session 为空 → 视为分离（valid=true，与 Python 空值跳过逻辑一致）。"""
    c = live_daemon
    r = c.call("check_session_separation",
               {"reviewer_identity": _rev(""), "implementer_identity": _impl("")})
    assert r.get("valid") is True


def test_check_session_separation_invalid_json(live_daemon):
    """非法 JSON → valid=false, E_IDENTITY_INCOMPLETE。"""
    c = live_daemon
    r = c.call("check_session_separation",
               {"reviewer_identity": "not-json", "implementer_identity": _impl("sess-B")})
    assert r.get("valid") is False
    assert r["reason"].get("code") == "E_IDENTITY_INCOMPLETE"


def test_check_session_separation_empty_params(live_daemon):
    """缺参数 → valid=false, E_IDENTITY_INCOMPLETE。"""
    c = live_daemon
    r = c.call("check_session_separation", {})
    assert r.get("valid") is False
    assert r["reason"].get("code") == "E_IDENTITY_INCOMPLETE"


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_check_session_separation_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("check_session_separation",
               {"reviewer_identity": _rev("A"), "implementer_identity": _impl("B")})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_check_session_separation_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("check_session_separation",
                {"reviewer_identity": _rev("A"), "implementer_identity": _impl("B")})
    assert isinstance(r, dict)
    assert "valid" in r and "reason" in r
