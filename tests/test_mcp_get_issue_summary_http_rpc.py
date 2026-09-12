"""MCP-020（A′ task_evidence_read）get_issue_summary → Rust daemon native。

覆盖 task 要求：
  success / module_filter / 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致，W3 隔离 harness 适配 current-HEAD）：
- Python MCP wrapper（tools_query.get_issue_summary）已从 _SYMBOL_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_get_issue_summary）为
  权威：查当前版本非测试函数 → 按语言规则正则匹配缺陷 → 按 issue_key 汇总 → 返回
  {total_functions, functions_with_issues, issue_free_functions, issue_free_ratio,
  issues}（与 Python analyzers.issues.get_issue_summary 一致）。
- current-HEAD 路由层（snapshot_state.rs S2）要求 RPC 携带 `workspace_instance_id`
  （字符串）而非旧整数 `workspace_id`；未知 workspace_instance_id → 权威拒绝
  （fail-closed）。本测试经 conftest 共享 `w3_live` 隔离 daemon 权威栈。
- 空库无函数 → total_functions=0、issue_free_ratio=0、issues=[]（合法空结构）。
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
def test_get_issue_summary_structure(w3_live):
    """返回结构完整：total_functions/functions_with_issues/issue_free_functions/
    issue_free_ratio/issues。"""
    c, inst = w3_live["client"], w3_live["inst"]
    r = c.call("get_issue_summary", {"workspace_instance_id": inst})
    assert isinstance(r, dict)
    assert isinstance(r.get("total_functions"), int)
    assert isinstance(r.get("functions_with_issues"), int)
    assert isinstance(r.get("issue_free_functions"), int)
    assert isinstance(r.get("issue_free_ratio"), (int, float))
    assert isinstance(r.get("issues"), list)
    # issues 列表每项含 type/label/severity/function_count
    for item in r["issues"]:
        assert "type" in item and "label" in item
        assert "severity" in item and "function_count" in item


def test_get_issue_summary_module_filter(w3_live):
    """module_filter 前缀过滤 → 结构完整。"""
    c, inst = w3_live["client"], w3_live["inst"]
    r = c.call("get_issue_summary",
               {"workspace_instance_id": inst, "module_filter": "NO_SUCH_MOD"})
    assert isinstance(r, dict)
    assert isinstance(r.get("issues"), list)


def test_get_issue_summary_unknown_workspace(w3_live):
    """未知 workspace_instance_id → 权威拒绝（fail-closed）。"""
    c = w3_live["client"]
    with pytest.raises(DaemonRemoteError):
        c.call("get_issue_summary", {"workspace_instance_id": "NO_SUCH_INSTANCE"})


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_issue_summary_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_issue_summary", {"workspace_instance_id": "x"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_issue_summary_new_client_instance_stable(w3_live):
    c2 = HttpDaemonRpcClient(endpoint=w3_live["endpoint"],
                             authority_id=get_http_authority_id())
    r = c2.call("get_issue_summary", {"workspace_instance_id": w3_live["inst"]})
    assert isinstance(r, dict)
    assert "total_functions" in r and "issues" in r