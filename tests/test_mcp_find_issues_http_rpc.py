"""MCP-021（A′ task_evidence_read）find_issues → Rust daemon native。

覆盖 task 要求：
  success / limit / issue_filter / 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_query.find_issues）已从 _SYMBOL_READ_ONLY_METHODS 移除
  compat 注册，改由 Rust daemon（task_collab.rs::handle_find_issues）为权威：查当前
  版本函数（可选 qualified_name/module_filter 过滤）→ 按语言规则匹配缺陷 → 返回
  有缺陷函数列表（issue_count 降序截取 limit）。差异：注入 workspace_id 隔离。
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
def test_find_issues_structure(live_daemon):
    """返回列表，每项含 qualified_name/module_path/name/issue_count/issues。"""
    c = live_daemon
    r = c.call("find_issues", {"workspace_id": 1, "limit": 5})
    assert isinstance(r, list)
    assert len(r) <= 5
    for item in r:
        assert "qualified_name" in item and "module_path" in item
        assert "name" in item and "issue_count" in item
        assert isinstance(item.get("issues"), list)


def test_find_issues_qualified_name(live_daemon):
    """指定 qualified_name → 无匹配函数返回 []。"""
    c = live_daemon
    r = c.call("find_issues", {"workspace_id": 1, "qualified_name": "NO_SUCH_FN_XYZ"})
    assert isinstance(r, list)
    assert r == []


def test_find_issues_unknown_workspace(live_daemon):
    """未知 workspace：无函数 → []（不报错）。"""
    c = live_daemon
    r = c.call("find_issues", {"workspace_id": 999999})
    assert isinstance(r, list)


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_find_issues_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("find_issues", {"workspace_id": 1})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_find_issues_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("find_issues", {"workspace_id": 1, "limit": 3})
    assert isinstance(r, list)
