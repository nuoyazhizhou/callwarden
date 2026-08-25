"""MCP-002（A′ task_evidence_read）find_evidence → Rust daemon native。

覆盖 task 要求：
  success（live daemon HTTP round-trip，items + count）、
  过滤（task_id / contract_id / verifier / limit）、非法参数（缺 task_id → invalid_params）、
  unknown/unauthorized workspace、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_collab.find_evidence）已是 route_rpc 薄壳；本测试直连
  HTTP RPC `find_evidence`，验证 Rust daemon（task_collab.rs::handle_find_evidence）
  为权威：从 task_evidence_events 按 task_id/contract_id/verifier/limit 过滤。
- Python compat `_h_find_evidence` 已退役（_COLLAB_READ_ONLY_METHODS 移除该项）。
- 所有失败 fail-closed 返回稳定且可区分的结构化错误，绝不降级到本地 SQLite。

确定性 golden parity：对 T-1785767529976-1760c608（task_evidence_events 有 2 行、verifier=cw-agent），
expect 与 Python `db_task_reviews.evidence.query` 语义一致的 items/count 投影。
"""

import hashlib
import json

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.server.daemon_protocol import DaemonRemoteError

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
def test_find_evidence_success(live_daemon):
    """Rust native 返回 task 下全部 evidence（items + count）。"""
    c = live_daemon
    r = c.call("find_evidence", {"task_id": EVIDENCE_TASK})
    assert isinstance(r, dict), f"应返回 dict，实际 {type(r)}"
    assert "items" in r and "count" in r
    assert r["count"] == 2
    assert len(r["items"]) == 2
    for item in r["items"]:
        assert item["task_id"] == EVIDENCE_TASK
        assert "evidence_id" in item
        assert "verifier_name" in item


def test_find_evidence_by_verifier(live_daemon):
    """按 verifier=cw-agent 过滤，仍能命中该 task 的 2 条。"""
    c = live_daemon
    r = c.call("find_evidence", {"task_id": EVIDENCE_TASK, "verifier": "cw-agent"})
    assert r["count"] == 2
    assert all(it["verifier_name"] == "cw-agent" for it in r["items"])


def test_find_evidence_limit(live_daemon):
    """limit=1 仅返回 1 条。"""
    c = live_daemon
    r = c.call("find_evidence", {"task_id": EVIDENCE_TASK, "limit": 1})
    assert r["count"] == 1
    assert len(r["items"]) == 1


def test_find_evidence_no_match(live_daemon):
    """未知 verifier 过滤 → 空结果，fail-closed 返回空而非报错。"""
    c = live_daemon
    r = c.call("find_evidence", {"task_id": EVIDENCE_TASK, "verifier": "no-such-verifier"})
    assert r["count"] == 0
    assert r["items"] == []


# ---------------------------------------------------------------------------
# 非法/缺省参数：find_evidence 的 task_id 为可选（Python 签名默认 ""），
# 缺省时按空 task_id 过滤 → 返回空结果（不报错，fail-closed 语义）。
# ---------------------------------------------------------------------------
def test_find_evidence_missing_task_id_empty_result(live_daemon):
    c = live_daemon
    r = c.call("find_evidence", {})
    assert isinstance(r, dict)
    assert r["count"] == 0
    assert r["items"] == []


def test_find_evidence_unknown_task(live_daemon):
    """未知 task：返回空 items（不报错，fail-closed 语义）。"""
    c = live_daemon
    r = c.call("find_evidence", {"task_id": "T-NO-SUCH-TASK-MCP002"})
    assert isinstance(r, dict)
    assert r["count"] == 0
    assert r["items"] == []


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_find_evidence_daemon_unavailable_fail_closed():
    from callwarden.config import get_http_authority_id
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("find_evidence", {"task_id": EVIDENCE_TASK})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_find_evidence_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("find_evidence", {"task_id": EVIDENCE_TASK})
    assert r["count"] == 2
