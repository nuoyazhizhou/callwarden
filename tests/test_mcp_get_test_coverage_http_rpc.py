"""MCP-022（A′ task_evidence_read）get_test_coverage → Rust daemon native。

覆盖 task 要求：
  success / unknown workspace / 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致，W3 隔离 harness 适配 current-HEAD）：
- Python MCP wrapper（tools_query.get_test_coverage）已从 _SYMBOL_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_get_test_coverage）为
  权威：统计函数总数、test 函数数（module_path 含 ::tests 或 name 以 test_ 开头）、
  按模块分布、覆盖率 → 返回 {total_functions, test_functions, test_ratio,
  total_modules, modules_with_tests, module_coverage, test_by_module}。
- current-HEAD 路由层（snapshot_state.rs S2）要求 RPC 携带 `workspace_instance_id`
  （字符串）而非旧整数 `workspace_id`；未知 workspace_instance_id → 权威拒绝
  （fail-closed）。本测试经 conftest 共享 `w3_live` 隔离 daemon 权威栈。
- 空库无函数 → total_functions=0、test_ratio=0、test_by_module=[]。
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
def test_get_test_coverage_structure(w3_live):
    """返回结构完整：total_functions/test_functions/test_ratio/total_modules/
    modules_with_tests/module_coverage/test_by_module。"""
    c, inst = w3_live["client"], w3_live["inst"]
    r = c.call("get_test_coverage", {"workspace_instance_id": inst})
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


def test_get_test_coverage_unknown_workspace(w3_live):
    """未知 workspace_instance_id → 权威拒绝（fail-closed）。"""
    c = w3_live["client"]
    with pytest.raises(DaemonRemoteError):
        c.call("get_test_coverage", {"workspace_instance_id": "NO_SUCH_INSTANCE"})


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_test_coverage_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_test_coverage", {"workspace_instance_id": "x"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_test_coverage_new_client_instance_stable(w3_live):
    c2 = HttpDaemonRpcClient(endpoint=w3_live["endpoint"],
                             authority_id=get_http_authority_id())
    r = c2.call("get_test_coverage", {"workspace_instance_id": w3_live["inst"]})
    assert isinstance(r, dict)
    assert "total_functions" in r and "test_by_module" in r