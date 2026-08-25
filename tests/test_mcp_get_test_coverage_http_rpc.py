"""MCP-022（A′ task_evidence_read）get_test_coverage → Rust daemon native。

覆盖 task 要求：
  success / unknown workspace / 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_query.get_test_coverage）已从 _SYMBOL_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_get_test_coverage）为
  权威：统计函数总数、test 函数数（module_path 含 ::tests 或 name 以 test_ 开头）、
  按模块分布、覆盖率 → 返回 {total_functions, test_functions, test_ratio,
  total_modules, modules_with_tests, module_coverage, test_by_module}。
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
def test_get_test_coverage_structure(live_daemon):
    """返回结构完整：total_functions/test_functions/test_ratio/total_modules/
    modules_with_tests/module_coverage/test_by_module。"""
    c = live_daemon
    r = c.call("get_test_coverage", {"workspace_id": 1})
    assert isinstance(r, dict)
    assert isinstance(r.get("total_functions"), int)
    assert isinstance(r.get("test_functions"), int)
    assert isinstance(r.get("test_ratio"), (int, float))
    assert isinstance(r.get("total_modules"), int)
    assert isinstance(r.get("modules_with_tests"), int)
    assert isinstance(r.get("module_coverage"), (int, float))
    assert isinstance(r.get("test_by_module"), list)
    for item in r["test_by_module"]:
        assert "module" in item and "test_count" in item


def test_get_test_coverage_unknown_workspace(live_daemon):
    """未知 workspace：无函数 → total_functions=0, ratio=0。"""
    c = live_daemon
    r = c.call("get_test_coverage", {"workspace_id": 999999})
    assert isinstance(r, dict)
    assert r.get("total_functions") == 0
    assert r.get("test_ratio") == 0
    assert r.get("test_by_module") == []


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_test_coverage_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_test_coverage", {"workspace_id": 1})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_test_coverage_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("get_test_coverage", {"workspace_id": 1})
    assert isinstance(r, dict)
    assert "total_functions" in r and "test_by_module" in r
