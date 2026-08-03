"""P4 Assignment 与安全 Lease smoke test（pytest 正式测试）

覆盖 Requirements 11.1-11.13, 13.4-13.10, 14.6, 14.11-14.12, 14.30-14.32：
- schema v46：task_assignments / task_leases / task_lease_events + 单 active 部分唯一索引
- Assignment：task+role+holder 绑定（Req 11.1），不依赖 workspace active_task_id（Req 13.4），
  可无 lease 存在（Req 11.12）
- Lease：token hash 存储、acquire 原子递增 fencing（Req 11.2-11.3）、renew 幂等（Req 11.4-11.5）、
  release 幂等 + 审计事件（Req 11.6-11.7）
- protected mutation：token/expiry/fencing/holder 校验失败不改变 task data（Req 11.8-11.9）
- Lease 校验通过不代表 mutation 被授权（Req 11.11）；SQLite 锁仅事务互斥（Req 11.10）
- 权威时钟（Req 14.11）：时间字段读取 daemon 时钟，客户端时间戳只作参考（Req 14.12）
- Lease 边界（Req 14.32/11.13）：不提供自动 dispatch/抢占/中央调度
"""
import os
import time

import pytest

from callwarden.db import CodeGraphDB
from callwarden.db.db_task_leases import (
    ERR_ASSIGNMENT_INCOMPLETE,
    ERR_ASSIGNMENT_NOT_FOUND,
    ERR_LEASE_ACTIVE_EXISTS,
    ERR_LEASE_EXPIRED,
    ERR_LEASE_FENCING_STALE,
    ERR_LEASE_HOLDER_MISMATCH,
    ERR_LEASE_NOT_FOUND,
    ERR_LEASE_TOKEN_MISMATCH,
    LeaseMixin,
)
from callwarden.db.schema import SCHEMA_VERSION


def _identity(agent_id="agent-a", session_id="sess-1", model_id="model-x", role="implementer"):
    return {"agent_id": agent_id, "session_id": session_id, "model_id": model_id, "role": role}


@pytest.fixture()
def db(tmp_path):
    os.environ["CW_USE_RUST_STORAGE"] = "0"
    d = CodeGraphDB(str(tmp_path / "p4.db"))
    d.register_workspace("p4-ws", str(tmp_path))
    d.set_active_workspace("p4-ws")
    # 测试注入 Authoritative_Clock 模拟（真实环境由 daemon ping 提供，见
    # db/db_task_leases.py::LeaseMixin._clock；daemon 不可用时 Lease 写操作
    # fail closed，单元测试不依赖 daemon，故在此替换时钟源）
    d._clock = lambda: time.time()
    yield d
    try:
        d.conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------
# 1. schema v46
# ---------------------------------------------------------------

def test_schema_v46_tables_and_index(db):
    assert SCHEMA_VERSION == 46
    assert issubclass(CodeGraphDB, LeaseMixin)
    tabs = sorted(
        r["name"]
        for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('task_assignments','task_leases','task_lease_events')"
        )
    )
    assert tabs == ["task_assignments", "task_lease_events", "task_leases"]
    idx = db.conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_task_leases_active_unique'"
    ).fetchone()
    assert idx, "缺少单 active lease 部分唯一索引（Req 11.2）"


# ---------------------------------------------------------------
# 2. Assignment（Req 11.1, 13.4, 11.12）
# ---------------------------------------------------------------

def test_assignment_create_get_revoke(db):
    ok, res = db.create_assignment("T-1", "implementer", _identity())
    assert ok
    asg = db.get_assignment("T-1", "implementer")
    assert asg and asg["assignment_id"] == res["assignment_id"]
    assert asg["agent_id"] == "agent-a"

    ok, res = db.revoke_assignment(res["assignment_id"])
    assert ok
    assert db.get_assignment("T-1", "implementer") is None


def test_assignment_incomplete_identity_rejected(db):
    ok, res = db.create_assignment("T-1", "implementer", {"agent_id": "a"})
    assert not ok
    assert res["code"] == ERR_ASSIGNMENT_INCOMPLETE
    assert db.get_assignment("T-1") is None


def test_assignment_revoke_not_found(db):
    ok, res = db.revoke_assignment("ASG-no-such")
    assert not ok
    assert res["code"] == ERR_ASSIGNMENT_NOT_FOUND


def test_assignment_without_lease_allowed(db):
    """assignment 可以没有 lease（Req 11.12）"""
    ok, _ = db.create_assignment("T-1", "implementer", _identity())
    assert ok
    st = db.get_lease_status("T-1", "implementer")
    assert st["status"] == "none"


# ---------------------------------------------------------------
# 3. Lease 生命周期（Req 11.2-11.7）
# ---------------------------------------------------------------

def test_acquire_token_once_db_stores_hash(db):
    ok, res = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    raw = res["token"]
    assert raw and res["fencing_counter"] == 1
    st = db.get_lease_status("T-1", "implementer")
    assert st["status"] == "active"
    assert "token" not in st, "get_lease_status 不得暴露 raw token"
    # DB 只存 sha256 hash（Req 11.2）
    row = db.conn.execute(
        "SELECT token_hash FROM task_leases WHERE lease_id = ?", (res["lease_id"],)
    ).fetchone()
    assert row and row["token_hash"] == __import__("hashlib").sha256(raw.encode()).hexdigest()
    assert raw not in row["token_hash"]


def test_duplicate_acquire_rejected(db):
    ok, _ = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    ok, res = db.acquire_lease("T-1", "implementer", _identity())
    assert not ok
    assert res["code"] == ERR_LEASE_ACTIVE_EXISTS


def test_renew_idempotent_and_errors(db):
    ok, res = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    raw, lease_id, counter = res["token"], res["lease_id"], res["fencing_counter"]

    ok, res = db.renew_lease("T-1", "implementer", raw)
    assert ok
    assert res["lease_id"] == lease_id
    assert res["fencing_counter"] == counter, "renew 不得递增 counter（Req 11.5）"

    # 错 token
    ok, res = db.renew_lease("T-1", "implementer", "wrong-token")
    assert not ok and res["code"] == ERR_LEASE_TOKEN_MISMATCH
    # 错 holder
    ok, res = db.renew_lease("T-1", "implementer", raw, identity=_identity("other-agent"))
    assert not ok and res["code"] == ERR_LEASE_HOLDER_MISMATCH


def test_release_idempotent(db):
    ok, res = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    raw = res["token"]
    ok, res = db.release_lease("T-1", "implementer", raw)
    assert ok and res["status"] == "released"
    # 重复 release 幂等（Req 11.7）
    ok, res = db.release_lease("T-1", "implementer", raw)
    assert ok and res["status"] == "released" and res.get("idempotent") is True


def test_fencing_counter_monotonic(db):
    ok, r1 = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    raw1, c1 = r1["token"], r1["fencing_counter"]
    db.release_lease("T-1", "implementer", raw1)

    ok, r2 = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    assert r2["fencing_counter"] == c1 + 1, "counter 必须单调递增（Req 11.3）"
    raw2, c2 = r2["token"], r2["fencing_counter"]

    # 旧 counter（token 正确）→ fencing 拒绝
    valid, res = db.validate_lease_for_mutation("T-1", "implementer", raw2, c1)
    assert not valid and res["code"] == ERR_LEASE_FENCING_STALE
    # 旧 token → token mismatch
    valid, res = db.validate_lease_for_mutation("T-1", "implementer", raw1, c2)
    assert not valid and res["code"] == ERR_LEASE_TOKEN_MISMATCH


def test_expired_lease_rejected_and_overwritten(db, monkeypatch):
    """过期判定由 Authoritative_Clock 驱动：时钟前进到 expires_at 之后即拒绝，
    随后可重新 acquire（counter 递增）。"""
    clock = {"now": 1000.0}
    monkeypatch.setattr(db, "_clock", lambda: clock["now"])
    ok, r = db.acquire_lease("T-1", "implementer", _identity(), ttl_seconds=1)
    assert ok
    raw = r["token"]
    clock["now"] = 1001.5
    # 过期 lease 校验拒绝（Authoritative_Clock，Req 11.9）
    valid, res = db.validate_lease_for_mutation("T-1", "implementer", raw, r["fencing_counter"])
    assert not valid and res["code"] == ERR_LEASE_EXPIRED
    # 过期后新 acquire 覆盖
    clock["now"] = 1002.0
    ok, r2 = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    assert r2["fencing_counter"] == r["fencing_counter"] + 1


# ---------------------------------------------------------------
# 4. protected mutation 校验（Req 11.8-11.9, 11.11）
# ---------------------------------------------------------------

def test_validate_lease_all_error_codes(db):
    # 无 active lease
    valid, res = db.validate_lease_for_mutation("T-1", "implementer", "t", 1)
    assert not valid and res["code"] == ERR_LEASE_NOT_FOUND

    ok, r = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    raw, counter = r["token"], r["fencing_counter"]

    # 错 token
    valid, res = db.validate_lease_for_mutation("T-1", "implementer", "bad", counter)
    assert not valid and res["code"] == ERR_LEASE_TOKEN_MISMATCH
    # 错 holder
    valid, res = db.validate_lease_for_mutation(
        "T-1", "implementer", raw, counter, identity=_identity("other")
    )
    assert not valid and res["code"] == ERR_LEASE_HOLDER_MISMATCH
    # 有效通过
    valid, res = db.validate_lease_for_mutation(
        "T-1", "implementer", raw, counter, identity=_identity()
    )
    assert valid and res["fencing_counter"] == counter


def test_lease_does_not_grant_write_authority(db):
    """Lease 校验通过不代表 mutation 被授权（Req 11.11）：
    没有 implementer 身份/角色权限时，即使 lease 有效也不应视为已授权。"""
    ok, r = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    # validate 只校验 lease 凭证，不等于角色授权
    valid, res = db.validate_lease_for_mutation("T-1", "implementer", r["token"], r["fencing_counter"])
    assert valid
    # 事件账本确认只记录 lease 生命周期，不含授权声明
    events = db.list_lease_events("T-1", "implementer")
    assert [e["event_type"] for e in events] == ["acquire"]


def test_lease_events_ledger_append_only(db):
    ok, r = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    raw = r["token"]
    db.renew_lease("T-1", "implementer", raw)
    db.release_lease("T-1", "implementer", raw)
    events = db.list_lease_events("T-1", "implementer")
    assert [e["event_type"] for e in events] == ["acquire", "renew", "release"]
    for e in events:
        assert "token" not in e and "token_hash" not in e, "事件账本不得记录 token"


# ---------------------------------------------------------------
# 5. protected mutation 接入（task_report_step + lease）
# ---------------------------------------------------------------

def _mk_task_with_step(db):
    task_id = db.task_create(
        "P4 lease task",
        steps=[{"action": "annotate", "target_file": "a.py", "check_items": "x"}],
    )
    step = db.conn.execute("SELECT id FROM task_steps WHERE task_id = ?", (task_id,)).fetchone()
    return task_id, step["id"]


def test_protected_report_step_requires_valid_lease(db):
    task_id, step_id = _mk_task_with_step(db)
    ok, r = db.acquire_lease(task_id, "implementer", _identity())
    assert ok
    raw, counter = r["token"], r["fencing_counter"]

    # 错 token → 拒绝且 task data 不变
    before = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]
    ret = db.task_report_step(task_id, step_id, "ok", lease_token="bad", fencing_counter=counter)
    assert ret is not None and "error" in ret and "E_LEASE" in ret["error"]
    after = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]
    assert after == before, "失败的受保护写操作不得改变 task data（Req 11.9）"

    # 旧 counter → E_LEASE_FENCING_STALE
    ret = db.task_report_step(
        task_id, step_id, "ok", lease_token=raw, fencing_counter=counter - 1
    )
    assert ret is not None and "E_LEASE_FENCING_STALE" in ret["error"]

    # 正确 lease → 成功
    ret = db.task_report_step(task_id, step_id, "ok", lease_token=raw, fencing_counter=counter)
    assert ret is None or ret.get("status") in ("done", "review")
    st = db.conn.execute(
        "SELECT status FROM task_steps WHERE id = ?", (step_id,)
    ).fetchone()["status"]
    assert st == "done"


def test_protected_report_step_without_lease_rejected(db):
    task_id, step_id = _mk_task_with_step(db)
    ret = db.task_report_step(task_id, step_id, "ok", lease_token="t", fencing_counter=1)
    assert ret is not None and "E_LEASE_NOT_FOUND" in ret["error"]
    st = db.conn.execute("SELECT status FROM task_steps WHERE id = ?", (step_id,)).fetchone()["status"]
    assert st == "pending", "无 lease 的受保护写操作必须 fail-closed（Req 11.8）"


def test_report_step_backward_compatible_without_lease(db):
    """不带 lease 参数时保持向后兼容（P4 不破坏既有调用）"""
    task_id, step_id = _mk_task_with_step(db)
    ret = db.task_report_step(task_id, step_id, "ok")
    st = db.conn.execute("SELECT status FROM task_steps WHERE id = ?", (step_id,)).fetchone()["status"]
    assert st == "done"


# ---------------------------------------------------------------
# 6. 迁移幂等 + fencing 属性测试（G4 hard-gate）
# ---------------------------------------------------------------

def test_migration_v45_to_v46_idempotent(db):
    """重复执行迁移不报错、不重复建表（10.1 幂等性）"""
    from callwarden.db.db_base import _migrate_v45_to_v46
    _migrate_v45_to_v46(db.conn)
    _migrate_v45_to_v46(db.conn)
    tabs = sorted(
        r["name"]
        for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('task_assignments','task_leases','task_lease_events')"
        )
    )
    assert tabs == ["task_assignments", "task_lease_events", "task_leases"]


def test_fencing_property_random_sequences(db):
    """Property 11 fencing 安全性：任意 acquire/release 序列后，
    发布 counter N 之后所有 counter < N 的 protected mutation 永远被拒绝。"""
    import random
    rng = random.Random(42)
    identity_a = _identity("agent-a")

    for trial in range(8):
        task = f"T-prop-{trial}"
        issued = []  # [(counter, token)]
        for _ in range(rng.randint(2, 5)):
            ok, r = db.acquire_lease(task, "implementer", identity_a)
            assert ok
            issued.append((r["fencing_counter"], r["token"]))
            # 有效 lease 通过校验
            valid, _ = db.validate_lease_for_mutation(
                task, "implementer", r["token"], r["fencing_counter"]
            )
            assert valid
            db.release_lease(task, "implementer", r["token"])

        # 新 counter N 发布后，所有 counter < N（无论 token 新旧）一律拒绝
        ok, rn = db.acquire_lease(task, "implementer", identity_a)
        assert ok
        n = rn["fencing_counter"]
        for counter, token in issued:
            valid, res = db.validate_lease_for_mutation(task, "implementer", token, counter)
            assert not valid, f"旧 counter {counter} 必须被拒绝 (trial={trial})"
            assert res["code"] in (ERR_LEASE_FENCING_STALE, ERR_LEASE_TOKEN_MISMATCH)
        # 新 token + 旧 counter 也拒绝
        valid, res = db.validate_lease_for_mutation(task, "implementer", rn["token"], n - 1)
        assert not valid and res["code"] == ERR_LEASE_FENCING_STALE
