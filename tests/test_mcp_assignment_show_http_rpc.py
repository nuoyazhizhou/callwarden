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
- C-18（backlog §W19）：C-16 起 handle_assignment_show 走权威 resolver
  task_bound_workspace_id——无 binding 的 task fail-closed（E_TASK_WORKSPACE_UNBOUND），
  显式传不一致 workspace_id → E_WORKSPACE_AUTHORITY_MISMATCH。原 4 例以合成
  task_id 断言「无 active assignment → none」编码的是 pre-authority「静默 none」
  语义，属陈旧断言：改经 _w3_harness.seed_task_workspace_binding 指向 well-known
  已绑定 task（保留 no-match → none 的原覆盖），另补无 binding fail-closed 正例。
"""

import os
import sqlite3

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.config import get_http_authority_id


@pytest.fixture()
def live_daemon(w3_live):
    """W3 隔离 harness：注入隔离 daemon 的 client / inst / endpoint。"""
    global CANONICAL_INSTANCE, _CANONICAL_ENDPOINT
    CANONICAL_INSTANCE = w3_live["inst"]
    _CANONICAL_ENDPOINT = w3_live["endpoint"]
    return w3_live["client"]


@pytest.fixture()
def bound_task(live_daemon, w3_live):
    """C-18：向隔离 task.db 追加 well-known 已绑定 task（不改 harness 既有行）。

    返回已绑定 task_id；不种任何 task_assignments 行（保留 no-match → none 覆盖）。
    """
    from _w3_harness import seed_task_workspace_binding, W3_ASSIGN_SHOW_BOUND_TASK

    conn = sqlite3.connect(os.path.join(w3_live["data_root"], "task.db"), timeout=10)
    try:
        seed_task_workspace_binding(conn, 1)
        conn.commit()
    finally:
        conn.close()
    return W3_ASSIGN_SHOW_BOUND_TASK


# ---------------------------------------------------------------------------
# success / no-match：HTTP round-trip，Rust daemon 为权威
# ---------------------------------------------------------------------------
def test_assignment_show_no_match(live_daemon, bound_task):
    """已绑定 task 无 active assignment → status=none（C-18：task 须先有 binding）。"""
    c = live_daemon
    r = c.call("assignment_show", {"workspace_id": 1, "task_id": bound_task})
    assert isinstance(r, dict)
    assert r.get("status") == "none"
    assert r.get("task_id") == bound_task


def test_assignment_show_with_role(live_daemon, bound_task):
    """role 过滤 → 无匹配 status=none（结构完整）。"""
    c = live_daemon
    r = c.call("assignment_show",
               {"workspace_id": 1, "task_id": bound_task, "role": "implementer"})
    assert isinstance(r, dict)
    assert r.get("status") == "none"
    assert r.get("role") == "implementer"


def test_assignment_show_unknown_workspace(live_daemon, bound_task):
    """C-18：显式 workspace_id 与不可变 binding 不一致 → E_WORKSPACE_AUTHORITY_MISMATCH。

    （原断言「未知 workspace → status=none」编码 pre-authority 静默语义，已陈旧。）
    """
    c = live_daemon
    with pytest.raises(DaemonRemoteError) as excinfo:
        c.call("assignment_show", {"workspace_id": 999999, "task_id": bound_task})
    assert excinfo.value.code == "E_WORKSPACE_AUTHORITY_MISMATCH", (
        f"错误码不符: {excinfo.value.code} / {excinfo.value}"
    )


def test_assignment_show_unbound_task_fail_closed(live_daemon):
    """C-18 新增正例：无 binding 的合成 task → E_TASK_WORKSPACE_UNBOUND（fail-closed，
    绝不静默 none）。"""
    c = live_daemon
    with pytest.raises(DaemonRemoteError) as excinfo:
        c.call("assignment_show", {"workspace_id": 1, "task_id": "NO-SUCH-TASK"})
    assert excinfo.value.code == "E_TASK_WORKSPACE_UNBOUND", (
        f"错误码不符: {excinfo.value.code} / {excinfo.value}"
    )


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_assignment_show_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError):
        c.call("assignment_show", {"workspace_id": 1, "task_id": "X"})


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_assignment_show_new_client_instance_stable(live_daemon, bound_task):
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    r = c2.call("assignment_show", {"workspace_id": 1, "task_id": bound_task})
    assert isinstance(r, dict)
    assert "status" in r
