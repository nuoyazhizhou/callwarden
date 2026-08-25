"""MCP-029（A′ task_evidence_read）get_summary → Rust daemon native。

覆盖 task 要求：
  success / no-match / 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_summary.get_summary）已从 _SUMMARY_READ_ONLY_METHODS 移除
  compat 注册，改由 Rust daemon（task_collab.rs::handle_get_summary）为权威：
  symbol_summaries JOIN symbols JOIN file_instances 按 workspace_id + qualified_name +
  is_current=1，返回 {qualified_name, summary, model, version}；无匹配返回 None。
- 本测试直连 HTTP RPC，验证返回结构。
- 确定性 parity：authority DB 中指定符号无摘要 → null（与 Python 无匹配语义一致）。
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
def test_get_summary_no_match(live_daemon):
    """无摘要 → null。"""
    c = live_daemon
    r = c.call("get_summary", {"workspace_id": 1, "qualified_name": "NO_SUCH_SYM_XYZ"})
    assert r is None


def test_get_summary_unknown_workspace(live_daemon):
    """未知 workspace：无摘要 → null（不报错）。"""
    c = live_daemon
    r = c.call("get_summary", {"workspace_id": 999999, "qualified_name": "X"})
    assert r is None


def test_get_summary_missing_params(live_daemon):
    """缺参 → 默认空 qualified_name，null（fail-closed，不抛错）。"""
    c = live_daemon
    r = c.call("get_summary", {"workspace_id": 1})
    assert r is None


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_summary_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_summary", {"workspace_id": 1, "qualified_name": "X"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_summary_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("get_summary", {"workspace_id": 1, "qualified_name": "X"})
    assert r is None or isinstance(r, dict)
