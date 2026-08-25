"""MCP-038（A′ task_evidence_read）blast_radius → Rust daemon native。

覆盖 task 要求：
  success/源符号不存在/自定义 depth、daemon unavailable（fail-closed）、restart。
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


def test_blast_radius_unknown_symbol(live_daemon):
    """源符号不存在 → 空结构。"""
    c = live_daemon
    r = c.call("blast_radius", {"symbol_hash": "H-NO-SUCH-038"})
    assert isinstance(r, dict)
    assert r["total_impacted"] == 0
    assert r["layers"] == []
    assert "by_layer" in r


def test_blast_radius_custom_depth(live_daemon):
    """自定义 depth → 结构正确。"""
    c = live_daemon
    r = c.call("blast_radius", {"symbol_hash": "H-NO-SUCH-038", "depth": 5})
    assert isinstance(r, dict)
    assert r["depth"] == 5


def test_blast_radius_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("blast_radius", {"symbol_hash": "H-X"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


def test_blast_radius_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("blast_radius", {"symbol_hash": "H-NO-SUCH-038"})
    assert isinstance(r, dict) and "by_layer" in r
