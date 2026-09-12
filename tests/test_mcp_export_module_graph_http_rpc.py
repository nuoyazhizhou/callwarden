"""MCP-023（A′ task_evidence_read）export_module_graph → Rust daemon native。

覆盖 task 要求：
  success（mermaid）/ dot 格式 / unknown workspace、daemon unavailable（fail-closed）、
  restart。

设计要点（与 task 不变量一致，W3 隔离 harness 适配 current-HEAD）：
- Python MCP wrapper（tools_query.export_module_graph）已从 _SYMBOL_READ_ONLY_METHODS
  移除 compat 注册（符号组全部迁移），改由 Rust daemon
  （task_collab.rs::handle_export_module_graph）为权威：查模块间调用边 → 生成
  mermaid/dot 依赖图文本（与 Python db_query.export_module_graph 一致，返回字符串）。
- current-HEAD 路由层（snapshot_state.rs S2）要求 RPC 携带 `workspace_instance_id`
  （字符串）而非旧整数 `workspace_id`；未知 workspace_instance_id → 权威拒绝
  （fail-closed）。本测试经 conftest 共享 `w3_live` 隔离 daemon 权威栈。
- 空库无调用边 → 返回空图骨架（flowchart TD / digraph module_dependencies）。
"""

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.config import get_http_authority_id


# ---------------------------------------------------------------------------
# success：HTTP round-trip，Rust daemon 为权威
# ---------------------------------------------------------------------------
def test_export_module_graph_mermaid(w3_live):
    """默认 mermaid → 返回 flowchart TD 文本。"""
    c, inst = w3_live["client"], w3_live["inst"]
    r = c.call("export_module_graph", {"workspace_instance_id": inst})
    assert isinstance(r, str)
    assert r.startswith("flowchart TD")


def test_export_module_graph_dot(w3_live):
    """format=dot → 返回 digraph 文本。"""
    c, inst = w3_live["client"], w3_live["inst"]
    r = c.call("export_module_graph",
               {"workspace_instance_id": inst, "format": "dot"})
    assert isinstance(r, str)
    assert r.startswith("digraph module_dependencies")


def test_export_module_graph_unknown_workspace(w3_live):
    """未知 workspace_instance_id → 权威拒绝（fail-closed）。"""
    c = w3_live["client"]
    with pytest.raises(DaemonRemoteError):
        c.call("export_module_graph", {"workspace_instance_id": "NO_SUCH_INSTANCE"})


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_export_module_graph_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("export_module_graph", {"workspace_instance_id": "x"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_export_module_graph_new_client_instance_stable(w3_live):
    c2 = HttpDaemonRpcClient(endpoint=w3_live["endpoint"],
                             authority_id=get_http_authority_id())
    r = c2.call("export_module_graph", {"workspace_instance_id": w3_live["inst"]})
    assert isinstance(r, str)