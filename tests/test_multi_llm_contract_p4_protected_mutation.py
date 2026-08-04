"""10.9 P4 protected mutation 与 Gate 组合集成测试（Requirement 1.11, 11.8-11.11, 13.5, 14.6-14.7）

覆盖场景（全部要求失败时无 task data 变更，fail-closed）：
- 旧持有者复活：release 后旧 token+counter 提交 → 拒绝
- 并发 acquire：已有 active lease 时重复 acquire → 拒绝
- 过期 lease：ttl 到期后受保护写 → 拒绝
- 角色越权：holder 身份不匹配 → 拒绝
- 有效 lease 但 Evidence Gate 失败：契约已发布、verdict 缺失 → block 且 task data 不变
- SQLite 获锁但 lease 无效：无 lease 的受保护写 → E_LEASE_NOT_FOUND
"""
import json
import os
import time

import pytest

from callwarden.db import CodeGraphDB
from callwarden.db.db_task_leases import (
    ERR_LEASE_ACTIVE_EXISTS,
    ERR_LEASE_EXPIRED,
    ERR_LEASE_FENCING_STALE,
    ERR_LEASE_HOLDER_MISMATCH,
    ERR_LEASE_NOT_FOUND,
    ERR_LEASE_TOKEN_MISMATCH,
)


def _identity(agent_id="agent-a", session_id="sess-1", model_id="model-x", role="implementer"):
    return {"agent_id": agent_id, "session_id": session_id, "model_id": model_id, "role": role}


@pytest.fixture()
def db(tmp_path):
    os.environ["CW_USE_RUST_STORAGE"] = "0"
    d = CodeGraphDB(str(tmp_path / "p4mut.db"))
    d.register_workspace("mut-ws", str(tmp_path))
    d.set_active_workspace("mut-ws")
    yield d
    try:
        d.conn.close()
    except Exception:
        pass


def _mk_task_with_step(db, title="P4 protected task"):
    task_id = db.task_create(
        title,
        steps=[{"action": "annotate", "target_file": "a.py", "check_items": "x"}],
    )
    step = db.conn.execute("SELECT id FROM task_steps WHERE task_id = ?", (task_id,)).fetchone()
    return task_id, step["id"]


def _task_status(db, task_id):
    return db.conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]


def _inject_contract(db, task_id, contract_id="C-p4mut-001"):
    db.conn.execute(
        """
        INSERT INTO task_contract_revisions
            (contract_id, revision, contract_hash, profile, task_id, workspace_id,
             envelope_payload, created_at, created_by)
        VALUES (?, 1, ?, 'code_change', ?, ?, ?, ?, ?)
        """,
        (
            contract_id,
            f"HASH-{contract_id}",
            task_id,
            db._get_active_workspace_id(),
            json.dumps({"contract_id": contract_id, "revision": 1, "objective": "test"}),
            time.time(),
            "sess-impl-001",
        ),
    )
    db.conn.commit()


def test_old_holder_resurrection_rejected(db):
    """旧持有者复活：release 后旧 token+counter 提交 → fencing/token 拒绝，task data 不变。"""
    task_id, step_id = _mk_task_with_step(db)
    ok, r1 = db.acquire_lease(task_id, "implementer", _identity())
    raw1, c1 = r1["token"], r1["fencing_counter"]
    db.release_lease(task_id, "implementer", raw1)

    # 新持有者获取新 lease（counter 递增）
    ok, r2 = db.acquire_lease(task_id, "implementer", _identity("agent-b"))
    raw2, c2 = r2["token"], r2["fencing_counter"]
    assert c2 == c1 + 1

    before = _task_status(db, task_id)
    # 旧持有者用旧 token + 旧 counter 提交 → 拒绝且 task data 不变
    ret = db.task_report_step(task_id, step_id, "ok", lease_token=raw1, fencing_counter=c1)
    assert ret is not None and ("E_LEASE" in ret.get("error", ""))
    assert _task_status(db, task_id) == before

    # 旧 token + 新 counter → token mismatch
    ret = db.task_report_step(task_id, step_id, "ok", lease_token=raw1, fencing_counter=c2)
    assert ret is not None and "E_LEASE_TOKEN_MISMATCH" in ret.get("error", "")


def test_concurrent_acquire_second_rejected(db):
    task_id, _ = _mk_task_with_step(db)
    ok, r = db.acquire_lease(task_id, "implementer", _identity())
    assert ok
    ok, res = db.acquire_lease(task_id, "implementer", _identity())
    assert not ok and res["code"] == ERR_LEASE_ACTIVE_EXISTS


def test_expired_lease_write_rejected_no_change(db):
    task_id, step_id = _mk_task_with_step(db)
    ok, r = db.acquire_lease(task_id, "implementer", _identity(), ttl_seconds=1)
    time.sleep(1.05)
    before = _task_status(db, task_id)
    ret = db.task_report_step(
        task_id, step_id, "ok", lease_token=r["token"], fencing_counter=r["fencing_counter"]
    )
    assert ret is not None and "E_LEASE_EXPIRED" in ret.get("error", "")
    assert _task_status(db, task_id) == before
    st = db.conn.execute("SELECT status FROM task_steps WHERE id = ?", (step_id,)).fetchone()["status"]
    assert st == "pending"


def test_role_escalation_rejected(db):
    """角色越权：lease 属于 agent-a（implementer），其他 agent 用其 token 提交 → holder mismatch。"""
    task_id, step_id = _mk_task_with_step(db)
    ok, r = db.acquire_lease(task_id, "implementer", _identity())
    before = _task_status(db, task_id)
    ret = db.task_report_step(
        task_id, step_id, "ok",
        lease_token=r["token"], fencing_counter=r["fencing_counter"],
        identity=_identity("agent-b", role="reviewer"),
    )
    assert ret is not None and "E_LEASE_HOLDER_MISMATCH" in ret.get("error", "")
    assert _task_status(db, task_id) == before


def test_valid_lease_but_evidence_gate_block(db):
    """有效 lease 但 Evidence Gate 失败：契约已发布、无 verdict → step blocked，task data 不变。"""
    task_id, step_id = _mk_task_with_step(db)
    ok, r = db.acquire_lease(task_id, "implementer", _identity())
    assert ok
    _inject_contract(db, task_id)

    before = _task_status(db, task_id)
    ret = db.task_report_step(
        task_id, step_id, "ok", lease_token=r["token"], fencing_counter=r["fencing_counter"]
    )
    # Evidence Gate block：返回信息含 evidence_gate.decision=block
    assert ret is not None
    assert ret.get("evidence_gate", {}).get("decision") == "block"
    st = db.conn.execute("SELECT status FROM task_steps WHERE id = ?", (step_id,)).fetchone()["status"]
    assert st == "blocked"
    assert _task_status(db, task_id) == before, "Evidence Gate block 不得改变 task data（Req 1.11）"


def test_sqlite_locked_but_lease_invalid(db):
    """SQLite 写锁只提供事务互斥：即使事务内已获锁，lease 无效的写仍被拒绝（Req 11.10）。"""
    task_id, step_id = _mk_task_with_step(db)
    # 无 lease 时受保护写 fail-closed
    before = _task_status(db, task_id)
    ret = db.task_report_step(task_id, step_id, "ok", lease_token="t", fencing_counter=1)
    assert ret is not None and "E_LEASE_NOT_FOUND" in ret.get("error", "")
    assert _task_status(db, task_id) == before

    # 有效 lease 但 counter 错 → 拒绝（BEGIN IMMEDIATE 内校验，非 SQLite 锁本身）
    ok, r = db.acquire_lease(task_id, "implementer", _identity())
    assert ok
    ret = db.task_report_step(
        task_id, step_id, "ok", lease_token=r["token"], fencing_counter=r["fencing_counter"] - 1
    )
    assert ret is not None and "E_LEASE_FENCING_STALE" in ret.get("error", "")


def test_valid_lease_write_succeeds(db):
    """对照：有效 lease + 无契约（legacy）→ 写成功，step done。"""
    task_id, step_id = _mk_task_with_step(db)
    ok, r = db.acquire_lease(task_id, "implementer", _identity())
    assert ok
    ret = db.task_report_step(
        task_id, step_id, "ok", lease_token=r["token"], fencing_counter=r["fencing_counter"]
    )
    assert ret is None or ret.get("status") in ("done", "review")
    st = db.conn.execute("SELECT status FROM task_steps WHERE id = ?", (step_id,)).fetchone()["status"]
    assert st == "done"
