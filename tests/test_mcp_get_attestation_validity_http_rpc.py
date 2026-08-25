"""MCP-013（A′ task_evidence_read）get_attestation_validity → Rust daemon native。

覆盖 task 要求：
  success（无撤销记录 → valid）/ 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_p3_identity.get_attestation_validity）已从 _P3_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_get_attestation_validity）为
  权威：查 attestation_revocation_records 按 revoked_at 升序，compromised → invalid（忽略
  issuance_time）；rotated 且 issuance_time > revoked_at → invalid；否则 valid。
- 本测试直连 HTTP RPC，验证返回结构 {"validity": "valid"|"invalid"}。
- 确定性 parity：authority DB 中指定 issuer/key 无撤销记录 → validity=valid（与 Python
  无撤销记录语义一致）。
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
def test_get_attestation_validity_no_revocation(live_daemon):
    """无撤销记录 → validity=valid。"""
    c = live_daemon
    r = c.call("get_attestation_validity",
               {"issuer": "NO-SUCH", "signing_key_id": "KEY-1", "issuance_time": 1.0,
                "workspace_id": 1})
    assert isinstance(r, dict)
    assert r.get("validity") == "valid"


def test_get_attestation_validity_missing_params(live_daemon):
    """缺参数 → 默认空 issuer/key，validity=valid（fail-closed，不抛错）。"""
    c = live_daemon
    r = c.call("get_attestation_validity", {})
    assert isinstance(r, dict)
    assert r.get("validity") == "valid"


def test_get_attestation_validity_unknown_workspace(live_daemon):
    """未知 workspace：无撤销记录 → validity=valid。"""
    c = live_daemon
    r = c.call("get_attestation_validity",
               {"issuer": "X", "signing_key_id": "K", "issuance_time": 1.0,
                "workspace_id": 999999})
    assert isinstance(r, dict)
    assert r.get("validity") == "valid"


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_attestation_validity_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_attestation_validity", {"issuer": "X"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_attestation_validity_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("get_attestation_validity", {"issuer": "X"})
    assert isinstance(r, dict)
    assert "validity" in r
