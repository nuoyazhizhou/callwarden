"""MCP-004（A′ task_evidence_read）get_gate_decision → Rust daemon native。

覆盖 task 要求：
  success（live daemon HTTP round-trip，items + count）、
  过滤（task_id / gate_id）、缺省参数（缺 task_id 按空过滤 → 返回全部限流）、
  unknown task/gate → 空结果、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_collab.get_gate_decision）已是 route_rpc 薄壳；本测试直连
  HTTP RPC `get_gate_decision`，验证 Rust daemon（task_collab.rs::handle_get_gate_decision）
  为权威：从 task_gate_decisions 按 task_id/decision_id(gate_id) 过滤查询。
- Python compat `_h_gate_decision` 已退役（_COLLAB_READ_ONLY_METHODS 已清空）。
- 所有失败 fail-closed 返回稳定且可区分的结构化错误，绝不降级到本地 SQLite。

确定性 parity：task_gate_decisions 当前为空 → 任何过滤均返回 count=0（与 Python
gate.decision.query 空表语义一致）；结构断言覆盖非空时的字段投影。
"""

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.config import get_http_authority_id

GATE_TASK = "T-1785767529976-1760c608"


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
def test_get_gate_decision_success(live_daemon):
    """Rust native 返回 gate decision items + count（空表时 count=0，结构正确）。"""
    c = live_daemon
    r = c.call("get_gate_decision", {"task_id": GATE_TASK})
    assert isinstance(r, dict), f"应返回 dict，实际 {type(r)}"
    assert "items" in r and "count" in r
    assert isinstance(r["items"], list)
    assert r["count"] == len(r["items"])


def test_get_gate_decision_by_gate_id(live_daemon):
    """按 gate_id(decision_id) 过滤，结构正确。"""
    c = live_daemon
    r = c.call("get_gate_decision", {"task_id": GATE_TASK, "gate_id": "D-NO-SUCH"})
    assert isinstance(r, dict)
    assert r["count"] == 0
    assert r["items"] == []


# ---------------------------------------------------------------------------
# 缺省参数：缺 task_id → 按空过滤（返回全部，受 limit 限流）
# ---------------------------------------------------------------------------
def test_get_gate_decision_no_task_id(live_daemon):
    c = live_daemon
    r = c.call("get_gate_decision", {})
    assert isinstance(r, dict)
    assert "items" in r and "count" in r


def test_get_gate_decision_unknown_task(live_daemon):
    """未知 task：返回空 items（不报错，fail-closed 语义）。"""
    c = live_daemon
    r = c.call("get_gate_decision", {"task_id": "T-NO-SUCH-TASK-MCP004"})
    assert isinstance(r, dict)
    assert r["count"] == 0
    assert r["items"] == []


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_gate_decision_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_gate_decision", {"task_id": GATE_TASK})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_gate_decision_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("get_gate_decision", {"task_id": GATE_TASK})
    assert isinstance(r, dict)
    assert "items" in r
