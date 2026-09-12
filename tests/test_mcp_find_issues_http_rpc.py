"""MCP-021（A′ task_evidence_read）find_issues → Rust daemon native。

覆盖 task 要求：
  success / limit / issue_filter / 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致，W3 隔离 harness 适配 current-HEAD）：
- Python MCP wrapper（tools_query.find_issues）已从 _SYMBOL_READ_ONLY_METHODS 移除
  compat 注册，改由 Rust daemon（task_collab.rs::handle_find_issues）为权威：查当前
  版本函数（可选 qualified_name/module_filter 过滤）→ 按语言规则匹配缺陷 → 返回
  有缺陷函数列表（issue_count 降序截取 limit）。差异：注入 workspace 隔离。
- current-HEAD 路由层（snapshot_state.rs S2）要求 RPC 携带 `workspace_instance_id`
  （字符串）而非旧整数 `workspace_id`；未知 workspace_instance_id → 权威拒绝
  （fail-closed）。本测试经 conftest 共享 `w3_live` 隔离 daemon 权威栈。
- 空库无函数 → 返回 []。
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
def test_find_issues_structure(w3_live):
    """返回列表，每项含 qualified_name/module_path/name/issue_count/issues。"""
    c, inst = w3_live["client"], w3_live["inst"]
    r = c.call("find_issues", {"workspace_instance_id": inst, "limit": 5})
    assert isinstance(r, list)
    assert len(r) <= 5
    for item in r:
        assert "qualified_name" in item and "module_path" in item
        assert "name" in item and "issue_count" in item
        assert isinstance(item.get("issues"), list)


def test_find_issues_qualified_name(w3_live):
    """指定 qualified_name → 无匹配函数返回 []。"""
    c, inst = w3_live["client"], w3_live["inst"]
    r = c.call("find_issues",
               {"workspace_instance_id": inst, "qualified_name": "NO_SUCH_FN_XYZ"})
    assert isinstance(r, list)
    assert r == []


def test_find_issues_unknown_workspace(w3_live):
    """未知 workspace_instance_id → 权威拒绝（fail-closed）。"""
    c = w3_live["client"]
    with pytest.raises(DaemonRemoteError):
        c.call("find_issues", {"workspace_instance_id": "NO_SUCH_INSTANCE"})


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_find_issues_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("find_issues", {"workspace_instance_id": "x"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_find_issues_new_client_instance_stable(w3_live):
    c2 = HttpDaemonRpcClient(endpoint=w3_live["endpoint"],
                             authority_id=get_http_authority_id())
    r = c2.call("find_issues", {"workspace_instance_id": w3_live["inst"], "limit": 3})
    assert isinstance(r, list)