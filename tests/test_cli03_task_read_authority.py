"""CLI-03 (A′ control_plane) `cw task show/list/status-tree` 只读 authority 诊断。

覆盖 task 要求：
  success（live daemon HTTP round-trip：task.list / task.status / task.status_tree）、
  非法/空输入（task show 伪造 id → task_not_found 结构化错误，不 crash）、
  authority failure（伪造 workspace_instance_id → fail-closed）、
  daemon unavailable（端点不可达 → E_HTTP_DAEMON_UNAVAILABLE fail-closed）、
  restart（新 client 实例重查仍稳定）。

设计要点（与 task 不变量一致）：
- Python `route_task_read` 仅作 HTTP thin client；Rust daemon（task_collab.rs
  handle_task_status / handle_task_list / handle_task_status_tree）为权威。
- 只读诊断：不得写 active workspace、不得改 task state（前后 status 一致）。
- 所有失败 fail-closed 返回稳定且可区分的结构化错误，绝不降级到本地 SQLite。
"""

import os

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.server.daemon_protocol import DaemonRemoteError

# 真实存在的任务（fixture 依赖 daemon task DB，与 CLI-02/03 任务树同库）
A_PRIME_EPIC = "T-1787293451688-c14b1e44"
CLI_02_TASK = "T-1787321708568-d292ab3c"
BOGUS_TASK = "T-NO-SUCH-TASK-CLI03-999"


@pytest.fixture()
def live_daemon():
    c = HttpDaemonRpcClient()
    try:
        health = c.health()
    except Exception:
        pytest.skip("daemon 未运行（无 HTTP endpoint），跳过 live 用例")
    return c, health


# ---------------------------------------------------------------------------
# success：HTTP round-trip，Rust daemon 为权威
# （daemon task handler 要求显式 workspace_id>0，生产路径经 _inject_workspace_id
#  注入；本测试直接 RPC，显式传仓库 workspace_id=1。）
# ---------------------------------------------------------------------------
WS_ID = 1


def test_task_list_roundtrip(live_daemon):
    c, _ = live_daemon
    r = c.call("task.list", {"status": "", "limit": 200, "workspace_id": WS_ID})
    assert isinstance(r, dict), f"task.list 应返回 dict，实际 {type(r)}"
    tasks = r.get("tasks") or []
    assert len(tasks) > 0
    ids = [t.get("task_id") or t.get("id") for t in tasks]
    assert CLI_02_TASK in ids, f"CLI-02 任务应出现在 task.list 中: {ids[:5]}..."


def test_task_status_roundtrip(live_daemon):
    c, _ = live_daemon
    r = c.call("task.status", {"task_id": CLI_02_TASK})
    assert isinstance(r, dict)
    assert r.get("task_id") == CLI_02_TASK
    assert "status" in r
    assert "title" in r
    assert r["status"] == "open"


def test_task_status_tree_roundtrip(live_daemon):
    """父任务 status_tree：返回树结构且含子任务（target/父树区分）。"""
    c, _ = live_daemon
    r = c.call("task.status_tree", {"task_id": A_PRIME_EPIC})
    assert isinstance(r, dict)
    assert r.get("task_id") == A_PRIME_EPIC
    subtasks = r.get("subtasks") or []
    assert len(subtasks) >= 160, f"A′ 应有 160+ 直接子任务，实际 {len(subtasks)}"


def test_task_read_no_state_mutation(live_daemon):
    """只读诊断不改变 task state（前后 status/updated_at 一致）。"""
    c, _ = live_daemon
    before = c.call("task.status", {"task_id": CLI_02_TASK})
    # 触发三种只读投影
    c.call("task.list", {"status": "", "limit": 50, "workspace_id": WS_ID})
    c.call("task.status_tree", {"task_id": A_PRIME_EPIC})
    after = c.call("task.status", {"task_id": CLI_02_TASK})
    assert after.get("status") == before.get("status") == "open"
    assert after.get("updated_at") == before.get("updated_at")


# ---------------------------------------------------------------------------
# 非法/空输入：结构化错误，不 crash
# ---------------------------------------------------------------------------
def test_task_show_bogus_id_structured_error(live_daemon):
    c, _ = live_daemon
    with pytest.raises(DaemonRemoteError) as ei:
        c.call("task.status", {"task_id": BOGUS_TASK})
    err = ei.value
    assert getattr(err, "code", "") in ("task_not_found", "E_TASK_NOT_FOUND")


def test_task_show_missing_task_id_invalid_params(live_daemon):
    c, _ = live_daemon
    with pytest.raises(DaemonRemoteError) as ei:
        c.call("task.status", {})
    assert getattr(ei.value, "code", "") in ("invalid_params", "E_INVALID_PARAMS")


# ---------------------------------------------------------------------------
# authority failure：伪造 workspace_instance_id fail-closed
# ---------------------------------------------------------------------------
def test_task_read_authority_failure_fake_workspace(live_daemon):
    """伪造 workspace_id：authority-scoped fail-closed——只返回该 workspace 已绑定
    任务，未知 workspace 返回空列表（不跨 workspace 泄漏，不全表列出）。"""
    c, _ = live_daemon
    r = c.call("task.list", {"status": "", "limit": 5, "workspace_id": 999999})
    tasks = r.get("tasks") if isinstance(r, dict) else (r or [])
    assert tasks == [], f"未知 workspace 应返回空列表（fail-closed），实际 {tasks!r}"


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_task_read_daemon_unavailable_fail_closed():
    from callwarden.config import get_http_authority_id
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("task.list", {"status": "", "limit": 5, "workspace_id": 1})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_task_read_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()  # 新实例（同 endpoint 重新发现）
    r = c2.call("task.status", {"task_id": CLI_02_TASK})
    assert r.get("task_id") == CLI_02_TASK
    assert r.get("status") == "open"
