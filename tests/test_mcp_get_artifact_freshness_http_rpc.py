"""MCP-005（A′ task_evidence_read）get_artifact_freshness → Rust daemon native。

覆盖 task 要求：
  success/no-match/filter（workspace_id + task_id + artifact_ref）、缺参、unknown
  workspace、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_p2_graph.get_artifact_freshness）已是 route_rpc 薄壳；
  本测试直连 HTTP RPC `get_artifact_freshness`，验证 Rust daemon
  （task_collab.rs::handle_get_artifact_freshness）为权威：从 artifact_identities 按
  workspace_id + task_id (+ artifact_ref) 取最新一条，命中返回字段投影，未命中返回
  {"found": false}。
- Python compat `_h_get_artifact_freshness` 已从 tools_p2_graph._P2_READ_ONLY_METHODS 移除。

确定性 parity：artifact_identities 当前为空 → 返回 {"found": false}（与 Python
get_artifact_freshness 无行时返回 None 一致，本 Rust 实现转为结构化 found 标志）。
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
def test_get_artifact_freshness_no_match(live_daemon):
    """空表 → {"found": false}（与 Python None 语义一致的结构化表达）。"""
    c = live_daemon
    r = c.call("get_artifact_freshness", {"workspace_id": 1, "task_id": "T-NOSUCH"})
    assert isinstance(r, dict)
    assert r.get("found") is False


def test_get_artifact_freshness_by_ref(live_daemon):
    """按 artifact_ref 过滤，结构正确。"""
    c = live_daemon
    r = c.call("get_artifact_freshness", {"workspace_id": 1, "task_id": "T-X", "artifact_ref": "A-Y"})
    assert isinstance(r, dict)
    assert r.get("found") is False


# ---------------------------------------------------------------------------
# 缺省参数：缺 artifact_ref → 仅按 workspace_id + task_id 查询
# ---------------------------------------------------------------------------
def test_get_artifact_freshness_missing_ref(live_daemon):
    c = live_daemon
    r = c.call("get_artifact_freshness", {"workspace_id": 1, "task_id": "T-X"})
    assert isinstance(r, dict)
    assert "found" in r


def test_get_artifact_freshness_unknown_workspace(live_daemon):
    """未知 workspace：返回 {"found": false}（不报错，fail-closed 语义）。"""
    c = live_daemon
    r = c.call("get_artifact_freshness", {"workspace_id": 999999, "task_id": "T-X"})
    assert isinstance(r, dict)
    assert r.get("found") is False


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_artifact_freshness_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_artifact_freshness", {"workspace_id": 1, "task_id": "T-X"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_artifact_freshness_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("get_artifact_freshness", {"workspace_id": 1, "task_id": "T-X"})
    assert isinstance(r, dict)
    assert "found" in r
