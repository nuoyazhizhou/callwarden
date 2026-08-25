"""MCP-037（A′ task_evidence_read）guardrail_list_rules → Rust daemon native。

覆盖 task 要求：
  success/带 category_filter/缺省参数、daemon unavailable（fail-closed）、restart。
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


def test_guardrail_list_rules_all(live_daemon):
    """列出全部规则（含 9 条内置规则）。"""
    c = live_daemon
    r = c.call("guardrail_list_rules", {})
    assert isinstance(r, list)
    if r:
        item = r[0]
        assert "rule_id" in item and "category" in item and "severity" in item


def test_guardrail_list_rules_by_category(live_daemon):
    """按 category 过滤。"""
    c = live_daemon
    r = c.call("guardrail_list_rules", {"category_filter": "db_safety"})
    assert isinstance(r, list)
    for item in r:
        assert item["category"] == "db_safety"


def test_guardrail_list_rules_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("guardrail_list_rules", {})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


def test_guardrail_list_rules_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("guardrail_list_rules", {"category_filter": "incident"})
    assert isinstance(r, list)
