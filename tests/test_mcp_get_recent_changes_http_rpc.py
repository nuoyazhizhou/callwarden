"""MCP-017（A′ task_evidence_read）get_recent_changes → Rust daemon native。

覆盖 task 要求：
  success / 缺省 since / since 解析、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致，W3 隔离 harness 适配 current-HEAD）：
- Python MCP wrapper（tools_query.get_recent_changes）已从 _SYMBOL_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_get_recent_changes）为
  权威：解析 since（1h/30m/1d/2h30m）→ 查 changed_files（parsed_at > cutoff）→ 对比
  前后版本符号 hash 计算 change_type → 返回 {changed_files, changed_functions,
  since_seconds}。
- current-HEAD 路由层（snapshot_state.rs S2）要求 RPC 携带 `workspace_instance_id`
  （字符串）而非旧整数 `workspace_id`；未知 workspace_instance_id → 权威拒绝
  （fail-closed，不回退 SQLite）。本测试经 conftest 共享 `w3_live` 隔离 daemon 权威栈。
- 空库对未知符号返回空结构（changed_files=[]），满足结构断言。
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
def test_get_recent_changes_structure(w3_live):
    """返回结构完整：changed_files / changed_functions / since_seconds。"""
    c, inst = w3_live["client"], w3_live["inst"]
    r = c.call("get_recent_changes",
               {"workspace_instance_id": inst, "since": "1d"})
    assert isinstance(r, dict)
    assert isinstance(r.get("changed_files"), list)
    assert isinstance(r.get("changed_functions"), list)
    assert r.get("since_seconds") == 86400


def test_get_recent_changes_default_since(w3_live):
    """缺 since → 默认 1d（since_seconds=86400）。"""
    c, inst = w3_live["client"], w3_live["inst"]
    r = c.call("get_recent_changes", {"workspace_instance_id": inst})
    assert isinstance(r, dict)
    assert r.get("since_seconds") == 86400


def test_get_recent_changes_since_parse(w3_live):
    """since=2h30m → since_seconds=9000。"""
    c, inst = w3_live["client"], w3_live["inst"]
    r = c.call("get_recent_changes",
               {"workspace_instance_id": inst, "since": "2h30m"})
    assert isinstance(r, dict)
    assert r.get("since_seconds") == 9000


def test_get_recent_changes_unknown_workspace(w3_live):
    """未知 workspace_instance_id → 权威拒绝（fail-closed）。"""
    c = w3_live["client"]
    with pytest.raises(DaemonRemoteError):
        c.call("get_recent_changes",
               {"workspace_instance_id": "NO_SUCH_INSTANCE", "since": "1d"})


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_recent_changes_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_recent_changes", {"workspace_instance_id": "x"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_recent_changes_new_client_instance_stable(w3_live):
    c2, inst, endpoint = (HttpDaemonRpcClient(endpoint=w3_live["endpoint"],
                          authority_id=get_http_authority_id()),
                          w3_live["inst"], w3_live["endpoint"])
    r = c2.call("get_recent_changes", {"workspace_instance_id": inst})
    assert isinstance(r, dict)
    assert "changed_files" in r and "changed_functions" in r