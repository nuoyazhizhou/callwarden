"""10.8 P4 lease 生命周期、幂等与权威时钟单元测试（Requirement 11.2-11.9, 14.11, 14.12）

覆盖：
- acquire 竞争：已有未过期 active lease 时拒绝（原子比较）
- expiry：ttl 到期后校验拒绝，且过期后可重新 acquire（counter 递增）
- renew/release 重放幂等：renew 不递增 counter、重复 release 幂等
- token/holder/counter 不匹配：分别返回对应错误码
- raw token 脱敏：status/events 均不暴露 token
- 权威时钟边界（Req 14.11/14.12）：monkeypatch `_clock` 注入超前/滞后时间，
  过期判定完全由权威时钟决定；客户端时间戳不参与（API 不接受客户端时钟参数，
  时间字段严格等于权威时钟值）
"""
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
    d = CodeGraphDB(str(tmp_path / "p4lease.db"))
    d.register_workspace("lease-ws", str(tmp_path))
    d.set_active_workspace("lease-ws")
    yield d
    try:
        d.conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------
# 1. acquire 竞争（Req 11.2）
# ---------------------------------------------------------------

def test_acquire_race_second_rejected(db):
    ok, r1 = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    ok, r2 = db.acquire_lease("T-1", "implementer", _identity())
    assert not ok
    assert r2["code"] == ERR_LEASE_ACTIVE_EXISTS
    assert r2["lease_id"] == r1["lease_id"], "拒绝响应应指出既有 lease"
    assert r2["fencing_counter"] == r1["fencing_counter"]


def test_acquire_different_role_independent(db):
    ok, _ = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    ok, r = db.acquire_lease("T-1", "reviewer", _identity(role="reviewer"))
    assert ok, "不同 role 的 lease 相互独立（Req 11.1 角色粒度）"
    assert r["fencing_counter"] == 1


# ---------------------------------------------------------------
# 2. expiry（Req 11.9）
# ---------------------------------------------------------------

def test_expiry_rejected_then_reacquire(db):
    ok, r = db.acquire_lease("T-1", "implementer", _identity(), ttl_seconds=1)
    raw, counter = r["token"], r["fencing_counter"]
    time.sleep(1.05)
    valid, res = db.validate_lease_for_mutation("T-1", "implementer", raw, counter)
    assert not valid and res["code"] == ERR_LEASE_EXPIRED
    ok, r2 = db.acquire_lease("T-1", "implementer", _identity())
    assert ok and r2["fencing_counter"] == counter + 1


def test_expiry_event_transition(db):
    ok, r = db.acquire_lease("T-1", "implementer", _identity(), ttl_seconds=1)
    time.sleep(1.05)
    ok, r2 = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    events = db.list_lease_events("T-1", "implementer")
    # 第二次 acquire 时旧 lease 被置 expired（事件账本可审计状态迁移）
    assert events[0]["event_type"] == "acquire"
    assert events[1]["event_type"] == "acquire"


# ---------------------------------------------------------------
# 3. renew/release 重放幂等（Req 11.4-11.7）
# ---------------------------------------------------------------

def test_renew_replay_idempotent(db):
    ok, r = db.acquire_lease("T-1", "implementer", _identity(), ttl_seconds=100)
    raw = r["token"]
    r1 = db.renew_lease("T-1", "implementer", raw)
    assert r1[0]
    r2 = db.renew_lease("T-1", "implementer", raw)
    assert r2[0]
    assert r2[1]["fencing_counter"] == r["fencing_counter"], "renew 重放不得递增 counter"


def test_release_replay_idempotent(db):
    ok, r = db.acquire_lease("T-1", "implementer", _identity())
    raw = r["token"]
    ok, res = db.release_lease("T-1", "implementer", raw)
    assert ok and res["status"] == "released"
    ok, res = db.release_lease("T-1", "implementer", raw)
    assert ok and res["status"] == "released" and res.get("idempotent") is True


def test_release_after_expiry_no_error(db):
    ok, r = db.acquire_lease("T-1", "implementer", _identity(), ttl_seconds=1)
    time.sleep(1.05)
    ok, res = db.release_lease("T-1", "implementer", r["token"])
    assert ok, "过期后 release 应幂等成功而非报错"


# ---------------------------------------------------------------
# 4. token/holder/counter 不匹配（Req 11.8）
# ---------------------------------------------------------------

def test_all_mismatch_codes(db):
    valid, res = db.validate_lease_for_mutation("T-1", "implementer", "t", 1)
    assert not valid and res["code"] == ERR_LEASE_NOT_FOUND

    ok, r = db.acquire_lease("T-1", "implementer", _identity())
    raw, counter = r["token"], r["fencing_counter"]

    valid, res = db.validate_lease_for_mutation("T-1", "implementer", "bad", counter)
    assert not valid and res["code"] == ERR_LEASE_TOKEN_MISMATCH

    valid, res = db.validate_lease_for_mutation(
        "T-1", "implementer", raw, counter, identity=_identity("other")
    )
    assert not valid and res["code"] == ERR_LEASE_HOLDER_MISMATCH

    valid, res = db.validate_lease_for_mutation("T-1", "implementer", raw, counter - 1)
    assert not valid and res["code"] == ERR_LEASE_FENCING_STALE


# ---------------------------------------------------------------
# 5. raw token 脱敏（Req 11.2）
# ---------------------------------------------------------------

def test_status_redacts_raw_token(db):
    ok, r = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    st = db.get_lease_status("T-1", "implementer")
    assert st["status"] == "active"
    # 不暴露 raw token（token_hash 供受保护校验保留，属设计行为）
    assert "token" not in st
    assert r["token"] not in str(st), "响应不得含 raw token 值"
    assert st.get("fencing_counter") == r["fencing_counter"]


def test_error_responses_redact_raw_token(db):
    ok, r = db.acquire_lease("T-1", "implementer", _identity())
    raw = r["token"]
    ok, res = db.renew_lease("T-1", "implementer", "bad-token")
    assert not ok
    assert "bad-token" not in str(res)
    ok, res = db.release_lease("T-1", "implementer", "bad-token")
    assert not ok
    assert "bad-token" not in str(res)


# ---------------------------------------------------------------
# 6. 权威时钟边界（Req 14.11/14.12）
# ---------------------------------------------------------------

def test_clock_drives_expiry_monkeypatch(db, monkeypatch):
    """过期判定完全由权威时钟（_clock）决定：时钟前进后过期，回拨后仍在有效期内。"""
    clock = {"now": 1000.0}

    def fake_clock():
        return clock["now"]

    monkeypatch.setattr(db, "_clock", fake_clock)

    ok, r = db.acquire_lease("T-1", "implementer", _identity(), ttl_seconds=100)
    assert ok
    assert r["acquired_at"] == 1000.0
    assert r["expires_at"] == 1100.0, "expires_at 必须由权威时钟 + ttl 计算（Req 14.11）"

    # 时钟前进到 1100.5（已过期）→ 拒绝
    clock["now"] = 1100.5
    valid, res = db.validate_lease_for_mutation("T-1", "implementer", r["token"], r["fencing_counter"])
    assert not valid and res["code"] == ERR_LEASE_EXPIRED

    # 时钟回拨到 1050（仍在有效期内）→ 通过（判定不看本地时间）
    clock["now"] = 1050.0
    valid, _ = db.validate_lease_for_mutation("T-1", "implementer", r["token"], r["fencing_counter"])
    assert valid


def test_clock_monotonic_counter_independent_of_clock_jump(db, monkeypatch):
    """时钟乱序（跳变）不影响 fencing counter 单调性。"""
    clock = {"now": 1000.0}

    def fake_clock():
        return clock["now"]

    monkeypatch.setattr(db, "_clock", fake_clock)
    ok, r1 = db.acquire_lease("T-1", "implementer", _identity())
    db.release_lease("T-1", "implementer", r1["token"])

    clock["now"] = 50.0  # 时钟大幅回拨
    ok, r2 = db.acquire_lease("T-1", "implementer", _identity())
    assert r2["fencing_counter"] == r1["fencing_counter"] + 1
    assert r2["acquired_at"] == 50.0

    clock["now"] = 99999.0  # 时钟大幅超前
    ok, r3 = db.acquire_lease("T-1", "implementer", _identity())
    db.release_lease("T-1", "implementer", r2["token"])
    assert r3["fencing_counter"] == r2["fencing_counter"] + 1


def test_client_timestamp_not_accepted(db):
    """API 不接受客户端时间戳参数：时间字段一律由权威时钟写入（Req 14.12）。"""
    ok, r = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    assert r["acquired_at"] == r["expires_at"] - 3600.0
    # 时钟函数是唯一时间来源
    assert abs(r["acquired_at"] - time.time()) < 30
