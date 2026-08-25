"""MCP-015（A′ task_evidence_read）assignment_show → Rust daemon native。

覆盖 task 要求：
  success / no-match（status=none）/ role 过滤 / 缺省参数、daemon unavailable
  （fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_p4_lease.assignment_show）已从 _P4_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_assignment_show）为
  权威：按 workspace_id + task_id + status='active'（可选 role 过滤）查
  task_assignments，按 id DESC LIMIT 1；无匹配返回 {"status":"none", task_id, role}。
- 返回结构与 Python _h_assignment_show 一致。
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
def test_assignment_show_no_match(live_daemon):
    """无 active assignment → status=none。"""
    c = live_daemon
    r = c.call("assignment_show", {"workspace_id": 1, "task_id": "NO-SUCH-TASK"})
    assert isinstance(r, dict)
    assert r.get("status") == "none"
    assert r.get("task_id") == "NO-SUCH-TASK"


def test_assignment_show_with_role(live_daemon):
    """role 过滤 → 无匹配 status=none（结构完整）。"""
    c = live_daemon
    r = c.call("assignment_show",
               {"workspace_id": 1, "task_id": "NO-SUCH-TASK", "role": "implementer"})
    assert isinstance(r, dict)
    assert r.get("status") == "none"
    assert r.get("role") == "implementer"


def test_assignment_show_unknown_workspace(live_daemon):
    """未知 workspace：无数据 → status=none（不报错）。"""
    c = live_daemon
    r = c.call("assignment_show", {"workspace_id": 999999, "task_id": "X"})
    assert isinstance(r, dict)
    assert r.get("status") == "none"


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_assignment_show_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("assignment_show", {"workspace_id": 1, "task_id": "X"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_assignment_show_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("assignment_show", {"workspace_id": 1, "task_id": "X"})
    assert isinstance(r, dict)
    assert "status" in r
