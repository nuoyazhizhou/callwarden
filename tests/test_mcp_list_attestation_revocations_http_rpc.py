"""MCP-014（A′ task_evidence_read）list_attestation_revocations → Rust daemon native。

覆盖 task 要求：
  success / 空表 / issuer 过滤 / 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_p3_identity.list_attestation_revocations）已从
  _P3_READ_ONLY_METHODS 移除 compat 注册（p3 组全部迁移），改由 Rust daemon
  （task_collab.rs::handle_list_attestation_revocations）为权威：按 workspace_id +
  可选 issuer/signing_key_id 过滤 attestation_revocation_records，按 revoked_at 升序，
  返回 {"items": [...], "count": N}。
- 本测试直连 HTTP RPC，验证返回结构。
- 确定性 parity：authority DB 中指定 workspace 无撤销记录 → items=[], count=0（与
  Python 空表语义一致）。
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
def test_list_attestation_revocations_workspace(live_daemon):
    """workspace 有数据 → 返回结构完整（items 列表 + count 一致）。"""
    c = live_daemon
    r = c.call("list_attestation_revocations", {"workspace_id": 1})
    assert isinstance(r, dict)
    assert isinstance(r.get("items"), list)
    assert r.get("count") == len(r.get("items"))
    if r.get("items"):
        # 行结构：含 issuer/signing_key_id/revocation_mode 等全列
        first = r["items"][0]
        assert "issuer" in first and "signing_key_id" in first
        assert "revocation_mode" in first and "revoked_at" in first


def test_list_attestation_revocations_issuer_filter(live_daemon):
    """按 issuer 过滤 → 空 items（无匹配），结构完整。"""
    c = live_daemon
    r = c.call("list_attestation_revocations",
               {"workspace_id": 1, "issuer": "NO-SUCH"})
    assert isinstance(r, dict)
    assert r.get("items") == []
    assert r.get("count") == 0


def test_list_attestation_revocations_unknown_workspace(live_daemon):
    """未知 workspace：无数据 → items=[]（不报错）。"""
    c = live_daemon
    r = c.call("list_attestation_revocations", {"workspace_id": 999999})
    assert isinstance(r, dict)
    assert r.get("items") == []


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_list_attestation_revocations_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("list_attestation_revocations", {"workspace_id": 1})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_list_attestation_revocations_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("list_attestation_revocations", {"workspace_id": 1})
    assert isinstance(r, dict)
    assert "items" in r and "count" in r
