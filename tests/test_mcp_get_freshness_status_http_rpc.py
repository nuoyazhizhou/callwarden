"""MCP-003（A′ task_evidence_read）get_freshness_status → Rust daemon native。

覆盖 task 要求：
  success（live daemon HTTP round-trip，items=[{evidence_id,status}]）、
  按 evidence_id / 按 task_id 批量派生、缺参（无 evidence_id 且缺 task_id → 空 items）、
  unknown/unauthorized workspace（空 items）、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_collab.get_freshness_status）已是 route_rpc 薄壳；本测试直连
  HTTP RPC `get_freshness_status`，验证 Rust daemon（task_collab.rs::handle_get_freshness_status +
  derive_evidence_freshness）为权威：复刻 Python db_task_evidence.derive_freshness 的
  全序 invalid > superseded > stale > fresh 派生逻辑。
- Python compat `_h_get_freshness_status` 已退役（_COLLAB_READ_ONLY_METHODS 移除该项）。
- 所有失败 fail-closed 返回稳定且可区分的结构化错误，绝不降级到本地 SQLite。

确定性 parity：T-1785767529976-1760c608 的 evidence（verifier=cw-agent，未注册于
verifier_registry）→ 当前 DB 派生结果为 invalid（与 Python VERIFIER_NOT_REGISTERED 一致）。
"""

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.config import get_http_authority_id

EVIDENCE_TASK = "T-1785767529976-1760c608"


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
def test_get_freshness_status_by_task_id(live_daemon):
    """Rust native 按 task_id 批量派生，每项含 evidence_id + status。"""
    c = live_daemon
    r = c.call("get_freshness_status", {"task_id": EVIDENCE_TASK})
    assert isinstance(r, dict), f"应返回 dict，实际 {type(r)}"
    assert "items" in r
    assert len(r["items"]) == 2
    for item in r["items"]:
        assert "evidence_id" in item
        assert "status" in item
        # 当前 DB：cw-agent 未注册于 verifier_registry → invalid（与 Python parity）
        assert item["status"] == "invalid"


def test_get_freshness_status_by_evidence_id(live_daemon):
    """按 evidence_id 精确查询单个 Evidence 的新鲜度。"""
    c = live_daemon
    evid = c.call("find_evidence", {"task_id": EVIDENCE_TASK})["items"][0]["evidence_id"]
    r = c.call("get_freshness_status", {"evidence_id": evid})
    assert isinstance(r, dict)
    assert len(r["items"]) == 1
    assert r["items"][0]["evidence_id"] == evid
    assert r["items"][0]["status"] == "invalid"


# ---------------------------------------------------------------------------
# 缺省参数：evidence_id 与 task_id 均无 → 空 items（不报错，fail-closed 语义）
# ---------------------------------------------------------------------------
def test_get_freshness_status_empty_when_no_id(live_daemon):
    c = live_daemon
    r = c.call("get_freshness_status", {})
    assert isinstance(r, dict)
    assert r["items"] == []


def test_get_freshness_status_unknown_task(live_daemon):
    """未知 task：返回空 items（不报错，fail-closed 语义）。"""
    c = live_daemon
    r = c.call("get_freshness_status", {"task_id": "T-NO-SUCH-TASK-MCP003"})
    assert isinstance(r, dict)
    assert r["items"] == []


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_freshness_status_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_freshness_status", {"task_id": EVIDENCE_TASK})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_freshness_status_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("get_freshness_status", {"task_id": EVIDENCE_TASK})
    assert len(r["items"]) == 2
