"""MCP-023（A′ task_evidence_read）export_module_graph → Rust daemon native。

覆盖 task 要求：
  success（mermaid）/ dot 格式 / unknown workspace、daemon unavailable（fail-closed）、
  restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_query.export_module_graph）已从 _SYMBOL_READ_ONLY_METHODS
  移除 compat 注册（符号组全部迁移），改由 Rust daemon
  （task_collab.rs::handle_export_module_graph）为权威：查模块间调用边 → 生成
  mermaid/dot 依赖图文本（与 Python db_query.export_module_graph 一致，返回字符串）。
- 本测试直连 HTTP RPC，验证返回结构。
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
# success：HTTP round-trip，Rust daemon 为权威
# ---------------------------------------------------------------------------
def test_export_module_graph_mermaid(live_daemon):
    """默认 mermaid → 返回 flowchart TD 文本。"""
    c = live_daemon
    r = c.call("export_module_graph", {"workspace_id": 1})
    assert isinstance(r, str)
    assert r.startswith("flowchart TD")


def test_export_module_graph_dot(live_daemon):
    """format=dot → 返回 digraph 文本。"""
    c = live_daemon
    r = c.call("export_module_graph", {"workspace_id": 1, "format": "dot"})
    assert isinstance(r, str)
    assert r.startswith("digraph module_dependencies")


def test_export_module_graph_unknown_workspace(live_daemon):
    """未知 workspace：无调用边 → 空图骨架（不报错）。"""
    c = live_daemon
    r = c.call("export_module_graph", {"workspace_id": 999999})
    assert isinstance(r, str)
    assert r.startswith("flowchart TD")


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_export_module_graph_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("export_module_graph", {"workspace_id": 1})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_export_module_graph_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("export_module_graph", {"workspace_id": 1})
    assert isinstance(r, str)
