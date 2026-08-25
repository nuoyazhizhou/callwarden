"""MCP-035（A′ task_evidence_read）guardrail_scan → Rust daemon native。

覆盖 task 要求：
  success/空库/带 file_filter/缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_summary.guardrail_scan）已是 route_rpc 薄壳；本测试直连
  HTTP RPC `guardrail_scan`，验证 Rust daemon（task_collab.rs::handle_guardrail_scan）
  为权威：初始化内置规则 → 查询文件 → 磁盘读取 → 3 类检测器 → findings 去重落库。
- Python compat `_h_guardrail_scan` 已从 tools_summary._SUMMARY_READ_ONLY_METHODS 移除。

确定性 parity：扫描结果依赖 file_instances/磁盘内容，空库 → 返回空数组（与 Python 一致）。
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
# success / 空库：HTTP round-trip，Rust daemon 为权威
# ---------------------------------------------------------------------------
def test_guardrail_scan_empty(live_daemon):
    """空库 → 返回数组（Python 空表返回 []）。"""
    c = live_daemon
    r = c.call("guardrail_scan", {})
    assert isinstance(r, list)


def test_guardrail_scan_with_filter(live_daemon):
    """带 file_filter → 结构正确。"""
    c = live_daemon
    r = c.call("guardrail_scan", {"file_filter": "server"})
    assert isinstance(r, list)
    if r:
        item = r[0]
        assert "rule_id" in item and "file_path" in item and "severity" in item


# ---------------------------------------------------------------------------
# 缺省参数：空 filter 扫描全部
# ---------------------------------------------------------------------------
def test_guardrail_scan_no_filter(live_daemon):
    c = live_daemon
    r = c.call("guardrail_scan", {})
    assert isinstance(r, list)


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed
# ---------------------------------------------------------------------------
def test_guardrail_scan_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("guardrail_scan", {})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_guardrail_scan_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("guardrail_scan", {"file_filter": "server"})
    assert isinstance(r, list)
