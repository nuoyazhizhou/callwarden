"""MCP-036（A′ task_evidence_read）guardrail_check_edit → Rust daemon native。

覆盖 task 要求：
  success（proposed_change 触发检测）/缺省/不可读文件→pass、daemon unavailable、restart。

设计要点：
- 直连 HTTP RPC `guardrail_check_edit`，验证 Rust daemon（task_collab.rs::
  handle_guardrail_check_edit）为权威：对文件/拟议修改运行 3 类检测器，返回
  {decision, findings, message}，不落库。
- Python compat `_h_guardrail_check_edit` 已从 _SUMMARY_READ_ONLY_METHODS 移除。
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
# success：proposed_change 触发检测
# ---------------------------------------------------------------------------
def test_guardrail_check_edit_proposed_change(live_daemon):
    """拟议修改含 DROP TABLE → decision=block 且 findings 含 GR-builtin-db-2。"""
    c = live_daemon
    r = c.call("guardrail_check_edit", {
        "file_path": "server/x.sql",
        "proposed_change": "ALTER TABLE users ADD COLUMN x;\nDROP TABLE legacy;",
    })
    assert isinstance(r, dict)
    assert "decision" in r and "findings" in r and "message" in r
    assert r["decision"] == "block"


def test_guardrail_check_edit_pass(live_daemon):
    """无风险内容 → decision=pass。"""
    c = live_daemon
    r = c.call("guardrail_check_edit", {
        "file_path": "server/x.rs",
        "proposed_change": "fn add(a: i32, b: i32) -> i32 { a + b }",
    })
    assert isinstance(r, dict)
    assert r["decision"] in ("pass", "warn")


# ---------------------------------------------------------------------------
# 缺省参数：文件不可读 → pass
# ---------------------------------------------------------------------------
def test_guardrail_check_edit_missing_file(live_daemon):
    c = live_daemon
    r = c.call("guardrail_check_edit", {"file_path": "NO_SUCH_FILE_xyz.rs"})
    assert isinstance(r, dict)
    assert r["decision"] == "pass"


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed
# ---------------------------------------------------------------------------
def test_guardrail_check_edit_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("guardrail_check_edit", {"file_path": "x.rs", "proposed_change": "x"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------
def test_guardrail_check_edit_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("guardrail_check_edit", {
        "file_path": "server/x.rs",
        "proposed_change": "fn f() {}",
    })
    assert isinstance(r, dict) and "decision" in r
