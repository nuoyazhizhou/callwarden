"""task.split 原子治理初始化 fixture 矩阵测试（T-1787963386217-0ae6d628）。

覆盖 task step `fixture_matrix`（target_file: tests/test_task_split_governance.py）
要求的矩阵：
  success  —— 有绑定父任务 split → 子任务原子拥有 workspace binding + 3×Role
              Contract + Task Contract（identity_policy）+ step binding，可 claim；
  rollback —— 父任务无 workspace binding（不存在）→ E_TASK_BINDING_REQUIRED，
              且不留下任何半成品子任务；
  no-bypass —— 子任务治理完整性逐项断言（不允许"有任务有步骤但无合同"残缺态），
              split event 记录真实 workspace_id（非空串）。

依赖：live daemon（含 d7e9a95 修复）经 UnixDaemonRpcClient；权威库只读断言。
"""

import uuid

import pytest

from callwarden.server.daemon_client import UnixDaemonRpcClient
from callwarden.server.daemon_protocol import DaemonRemoteError

AUTH_DB = r"C:\Users\wanpi\.callwarden\callwarden.db"
IDENTITY = {
    "agent_id": "executor-workbuddy-v1-cur",
    "session_id": "sess-workbuddy-cw-20260822-0320",
    "model_id": "deepseek-v4-flash",
    "role": "executor",
}


def _db():
    import sqlite3
    conn = sqlite3.connect(AUTH_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _create_parent(client, steps=None):
    """经 daemon 创建带 workspace binding 的父任务。"""
    tid = "T-SPLIT-" + uuid.uuid4().hex[:10]
    steps = steps or [
        {"action": "port_rust_authority", "target_file": "rust_ext/src/daemon/task_collab_planning.rs"},
    ]
    r = client.call("task.create", {
        "task_id": tid, "title": "split 父任务", "description": "test",
        "steps": steps, "creator": "test",
        "role_contracts": [{"role": "executor"}, {"role": "reviewer"}, {"role": "adjudicator"}],
        "identity_policy": "legacy_identity_v1",
        "workspace_id": 1, "request_id": f"mk-{tid}-{uuid.uuid4().hex[:8]}",
    })
    return tid


def _subtask_defs():
    return [
        {"title": "子任务 A", "description": "a", "steps": [
            {"action": "port_rust_authority", "target_file": "a.rs"},
            {"action": "thin_cli_client", "target_file": "b.py"},
        ]},
        {"title": "子任务 B", "description": "b", "steps": [
            {"action": "fixture_matrix", "target_file": "tests/test_x.py"},
        ]},
    ]


@pytest.fixture(scope="module")
def client():
    return UnixDaemonRpcClient()


def test_split_success_full_governance(client):
    """success：子任务原子拥有 binding + 3×Role Contract + Task Contract(identity_policy) + step binding。"""
    parent = _create_parent(client)
    r = client.call("task.split", {
        "task_id": parent, "subtasks": _subtask_defs(),
        "identity_policy": "legacy_identity_v1",
        "request_id": f"sp-{parent}-{uuid.uuid4().hex[:8]}",
    })
    assert r.get("subtask_count") == 2
    subs = r["subtasks"]
    assert len(subs) == 2

    conn = _db()
    for sid in subs:
        # 1) workspace binding 继承
        n = conn.execute(
            "SELECT COUNT(*) FROM task_workspace_bindings WHERE task_id=?", (sid,)
        ).fetchone()[0]
        assert n == 1, f"{sid} 缺 workspace binding"
        # 2) 三角色 Role Contract
        n = conn.execute(
            "SELECT COUNT(*) FROM role_contracts WHERE task_id=?", (sid,)
        ).fetchone()[0]
        assert n == 3, f"{sid} 缺三角色 Role Contract (got {n})"
        # 3) Task Contract + identity_policy
        row = conn.execute(
            "SELECT envelope_payload FROM task_contract_revisions WHERE task_id=? ORDER BY revision DESC LIMIT 1",
            (sid,),
        ).fetchone()
        assert row is not None, f"{sid} 缺 Task Contract"
        import json as _json
        env = _json.loads(row[0])
        assert env.get("identity_policy") == "legacy_identity_v1", f"{sid} identity_policy 缺失"
        # 4) step binding == 步骤数
        n_steps = conn.execute(
            "SELECT COUNT(*) FROM task_steps WHERE task_id=?", (sid,)
        ).fetchone()[0]
        n_bind = conn.execute(
            "SELECT COUNT(*) FROM task_step_role_contract_bindings WHERE task_id=?", (sid,)
        ).fetchone()[0]
        assert n_bind == n_steps, f"{sid} step binding {n_bind} != steps {n_steps}"
    conn.close()


def test_split_rollback_on_unbound_parent(client):
    """rollback：父任务无 workspace binding → E_TASK_BINDING_REQUIRED，且无半成品子任务。"""
    ghost = "T-NOEXIST-" + uuid.uuid4().hex[:10]
    with pytest.raises(DaemonRemoteError) as ei:
        client.call("task.split", {
            "task_id": ghost, "subtasks": _subtask_defs(),
            "request_id": f"sp-{ghost}-{uuid.uuid4().hex[:8]}",
        })
    assert "E_TASK_BINDING_REQUIRED" in str(ei.value), str(ei.value)
    conn = _db()
    n = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE parent_id=?", (ghost,)
    ).fetchone()[0]
    assert n == 0, f"幽灵父任务不应产生子任务 (got {n})"
    conn.close()


def test_split_no_bypass_no_partial_governance(client):
    """no-bypass：子任务不允许残缺治理；split event 记录真实 workspace_id（非空串）。"""
    parent = _create_parent(client)
    r = client.call("task.split", {
        "task_id": parent, "subtasks": _subtask_defs(),
        "identity_policy": "legacy_identity_v1",
        "request_id": f"sp-{parent}-{uuid.uuid4().hex[:8]}",
    })
    subs = r["subtasks"]
    conn = _db()
    for sid in subs:
        # 治理三要素缺一即失败：binding / role_contracts / task_contract 必须同时存在
        missing = 0
        for tbl in ("task_workspace_bindings", "role_contracts", "task_contract_revisions"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE task_id=?", (sid,)
            ).fetchone()[0]
            if n == 0:
                missing += 1
        assert missing == 0, f"{sid} 存在治理残缺（缺 {missing} 类治理事实）"
    # split event 的 workspace_id 必须是真实值（非空串，修复前为 ''）
    ev = conn.execute(
        "SELECT workspace_id FROM task_events WHERE task_id=? AND reason_code='split' ORDER BY monotonic_seq DESC LIMIT 1",
        (parent,),
    ).fetchone()
    assert ev is not None and ev[0] not in ("", None), "split event workspace_id 为空串（修复前缺陷）"
    conn.close()
