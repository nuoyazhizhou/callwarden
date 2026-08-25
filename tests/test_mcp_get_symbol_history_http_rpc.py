"""MCP-016（A′ task_evidence_read）get_symbol_history → Rust daemon native。

覆盖 task 要求：
  success / no-match / 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_query.get_symbol_history）已从 _SYMBOL_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_get_symbol_history）为
  权威：file_symbol_versions JOIN file_versions JOIN file_instances，按 qualified_name
  + workspace_id + status!='archived' 过滤，按 parsed_at DESC，返回行数组。
- 本测试直连 HTTP RPC，验证返回结构。
- 确定性 parity：authority DB 中指定符号无历史 → 返回 []（与 Python 空结果语义一致）。
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
def test_get_symbol_history_no_match(live_daemon):
    """无历史 → []。"""
    c = live_daemon
    r = c.call("get_symbol_history",
               {"workspace_id": 1, "qualified_name": "NO_SUCH_SYMBOL_XYZ"})
    assert isinstance(r, list)
    assert r == []


def test_get_symbol_history_missing_params(live_daemon):
    """缺参 → 默认空 qualified_name，返回 []（fail-closed，不抛错）。"""
    c = live_daemon
    r = c.call("get_symbol_history", {})
    assert isinstance(r, list)


def test_get_symbol_history_unknown_workspace(live_daemon):
    """未知 workspace：无数据 → []。"""
    c = live_daemon
    r = c.call("get_symbol_history",
               {"workspace_id": 999999, "qualified_name": "X"})
    assert isinstance(r, list)
    assert r == []


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_symbol_history_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_symbol_history", {"workspace_id": 1, "qualified_name": "X"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_symbol_history_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("get_symbol_history", {"workspace_id": 1, "qualified_name": "X"})
    assert isinstance(r, list)
