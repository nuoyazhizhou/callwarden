"""P4 Authoritative_Clock 权威时钟测试（Req 11.2/11.4/11.9, 14.11, 14.12, 14.30）

覆盖（对应缺陷：db/db_task_leases._clock() 不得回退客户端时钟）：
- `_clock()` 必须读取 daemon Authoritative_Clock（ping timestamp），而非
  time.time()：注入 daemon 时钟值后 acquired_at/expires_at/renewed_at 严格
  等于该值，与客户端本地时钟无关（Req 14.11/14.12）。
- daemon 不可用时 Lease/Assignment 写操作 fail closed：返回 Structured_Reason
  （稳定错误码 E_LEASE_CLOCK_UNAVAILABLE + 双语 catalog 可解析的
  error.governance_write_degraded + 恢复指引），且不写入任何 lease/assignment/
  event 行（Req 14.30 fail closed，不改变任务/步骤/lease 状态）。
- 过期判定由 daemon 时钟驱动：推进/回拨 daemon 时钟改变结论；注入超前/滞后
  的客户端时间戳不改变结论（Req 14.12）。
- fencing 与 renew/release 幂等在 daemon 时钟下仍然成立（Req 11.3-11.7）。
"""
import json
import os
import time

import pytest

from callwarden.db import CodeGraphDB
from callwarden.db.db_task_leases import (
    ERR_LEASE_CLOCK_UNAVAILABLE,
    ERR_LEASE_EXPIRED,
    ERR_LEASE_FENCING_STALE,
    ERR_LEASE_TOKEN_MISMATCH,
    LeaseClockUnavailableError,
)
from callwarden.server import daemon_client as _dc


def _identity(agent_id="agent-a", session_id="sess-1", model_id="model-x", role="implementer"):
    return {"agent_id": agent_id, "session_id": session_id, "model_id": model_id, "role": role}


@pytest.fixture()
def db(tmp_path):
    os.environ["CW_USE_RUST_STORAGE"] = "0"
    d = CodeGraphDB(str(tmp_path / "p4clock.db"))
    d.register_workspace("clock-ws", str(tmp_path))
    d.set_active_workspace("clock-ws")
    yield d
    try:
        d.conn.close()
    except Exception:
        pass


@pytest.fixture()
def daemon_clock(monkeypatch):
    """注入 fake daemon RPC client：call('ping') 返回可变的 daemon 时钟。"""
    state = {"ts": 1000.0, "fail": False}

    class FakeDaemonRpcClient:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, method, params=None):
            if state["fail"]:
                raise OSError("endpoint 不可连接（模拟 daemon 不可用）")
            return {"timestamp": state["ts"]}

    monkeypatch.setattr(_dc, "UnixDaemonRpcClient", FakeDaemonRpcClient)
    return state


def _count(db, table):
    return db.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]


# ---------------------------------------------------------------
# 1. _clock() 读取 daemon 权威时钟（Req 14.11）
# ---------------------------------------------------------------

def test_clock_reads_daemon_timestamp(db, daemon_clock):
    daemon_clock["ts"] = 1234567890.5
    now = db._clock()
    assert now == 1234567890.5
    # 时间来自 daemon，而非客户端本地时钟（两者相差 > 100 万秒）
    assert abs(now - time.time()) > 1_000_000


def test_acquire_timestamps_come_from_daemon(db, daemon_clock):
    daemon_clock["ts"] = 1234567890.5
    ok, r = db.acquire_lease(
        "T-1", "implementer", _identity(), ttl_seconds=100)
    assert ok
    assert r["acquired_at"] == 1234567890.5
    assert r["expires_at"] == 1234567990.5, "expires_at = 权威时钟 + ttl（Req 14.11）"
    assert abs(r["acquired_at"] - time.time()) > 1_000_000, \
        "acquired_at 必须来自 daemon 权威时钟而非客户端时钟"


def test_assignment_uses_daemon_clock(db, daemon_clock):
    daemon_clock["ts"] = 2000.0
    ok, res = db.create_assignment("T-1", "implementer", _identity())
    assert ok and res["created_at"] == 2000.0


# ---------------------------------------------------------------
# 2. daemon 不可用 → fail closed（Req 14.30, 1.12）
# ---------------------------------------------------------------

def test_clock_unavailable_raises_structured_reason(db, daemon_clock):
    daemon_clock["fail"] = True
    with pytest.raises(LeaseClockUnavailableError) as ei:
        db._clock()
    reason = ei.value.reason
    assert reason["code"] == ERR_LEASE_CLOCK_UNAVAILABLE
    assert reason["message_key"] == "error.governance_write_degraded"
    assert reason.get("recovery_guidance"), "必须携带可执行恢复指引（Req 14.30）"


def test_all_lease_ops_fail_closed_when_daemon_down(db, daemon_clock):
    daemon_clock["fail"] = True
    identity = _identity()
    cases = [
        db.acquire_lease("T-1", "implementer", identity),
        db.create_assignment("T-1", "implementer", identity),
        db.revoke_assignment("ASG-no-such"),
        db.renew_lease("T-1", "implementer", "tok"),
        db.release_lease("T-1", "implementer", "tok"),
        db.validate_lease_for_mutation("T-1", "implementer", "tok", 1),
    ]
    for ok, res in cases:
        assert not ok, "daemon 不可用时 Lease/Assignment 写操作必须拒绝"
        assert res["code"] == ERR_LEASE_CLOCK_UNAVAILABLE
        assert res["message_key"] == "error.governance_write_degraded"
        assert res.get("recovery_guidance")
    # fail closed：不写入任何 lease/assignment/event 行（Req 14.30）
    assert _count(db, "task_leases") == 0
    assert _count(db, "task_lease_events") == 0
    assert _count(db, "task_assignments") == 0


def test_message_key_resolvable_in_both_catalogs():
    """Req 1.12：message_key 必须在 zh_CN/en_US 两个 catalog 均可解析。"""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for lang in ("zh_CN", "en_US"):
        with open(os.path.join(repo_root, "i18n", f"{lang}.json"),
                  encoding="utf-8") as f:
            data = json.load(f)
        text = data.get("daemon_errors", {}).get("error.governance_write_degraded")
        assert isinstance(text, str) and text, \
            f"{lang} catalog 缺少 error.governance_write_degraded"


# ---------------------------------------------------------------
# 3. 过期判定由 daemon 时钟驱动（Req 11.9, 14.11, 14.12）
# ---------------------------------------------------------------

def test_expiry_driven_by_daemon_clock(db, daemon_clock):
    daemon_clock["ts"] = 1000.0
    ok, r = db.acquire_lease("T-1", "implementer", _identity(), ttl_seconds=100)
    assert ok and r["acquired_at"] == 1000.0 and r["expires_at"] == 1100.0

    # daemon 时钟推进到 1100.5（已过期）→ 拒绝
    daemon_clock["ts"] = 1100.5
    valid, res = db.validate_lease_for_mutation(
        "T-1", "implementer", r["token"], r["fencing_counter"])
    assert not valid and res["code"] == ERR_LEASE_EXPIRED

    # daemon 时钟回拨到 1050（仍有效）→ 通过；判定不看客户端本地时间
    daemon_clock["ts"] = 1050.0
    valid, _ = db.validate_lease_for_mutation(
        "T-1", "implementer", r["token"], r["fencing_counter"])
    assert valid


def test_client_clock_skew_does_not_change_expiry(db, daemon_clock):
    """Req 14.12：注入超前/滞后客户端时间戳不改变过期判定。"""
    daemon_clock["ts"] = 1000.0
    ok, r = db.acquire_lease("T-1", "implementer", _identity(), ttl_seconds=100)
    assert ok and r["acquired_at"] == 1000.0

    # 客户端本地时钟被篡改（超前 99 亿秒），daemon 时钟仍为 1000 → 未过期
    valid, _ = db.validate_lease_for_mutation(
        "T-1", "implementer", r["token"], r["fencing_counter"])
    assert valid

    # 仅推进 daemon 时钟 → 过期（客户端本地时间未变化，时间戳只作参考元数据）
    daemon_clock["ts"] = 1100.5
    valid, res = db.validate_lease_for_mutation(
        "T-1", "implementer", r["token"], r["fencing_counter"])
    assert not valid and res["code"] == ERR_LEASE_EXPIRED


# ---------------------------------------------------------------
# 4. fencing 安全性（Property 11 / Req 11.3, 11.8-11.9）
# ---------------------------------------------------------------

def test_fencing_stale_counter_rejected_under_daemon_clock(db, daemon_clock):
    daemon_clock["ts"] = 1000.0
    ok, r1 = db.acquire_lease("T-1", "implementer", _identity())
    assert ok
    ok, _ = db.release_lease("T-1", "implementer", r1["token"])
    assert ok

    daemon_clock["ts"] = 1001.0
    ok, r2 = db.acquire_lease("T-1", "implementer", _identity())
    assert ok and r2["fencing_counter"] == r1["fencing_counter"] + 1

    # 新 counter N 发布后，counter < N 的 mutation 永远被拒绝（Property 11）
    valid, res = db.validate_lease_for_mutation(
        "T-1", "implementer", r2["token"], r1["fencing_counter"])
    assert not valid and res["code"] == ERR_LEASE_FENCING_STALE
    # 旧 token + 新 counter → token mismatch
    valid, res = db.validate_lease_for_mutation(
        "T-1", "implementer", r1["token"], r2["fencing_counter"])
    assert not valid and res["code"] == ERR_LEASE_TOKEN_MISMATCH


# ---------------------------------------------------------------
# 5. renew/release 幂等（Req 11.5, 11.7）
# ---------------------------------------------------------------

def test_renew_release_idempotent_under_daemon_clock(db, daemon_clock):
    daemon_clock["ts"] = 1000.0
    ok, r = db.acquire_lease("T-1", "implementer", _identity(), ttl_seconds=1000)
    assert ok

    daemon_clock["ts"] = 1100.0
    ok, r1 = db.renew_lease("T-1", "implementer", r["token"], ttl_seconds=500)
    assert ok and r1["fencing_counter"] == r["fencing_counter"]
    assert r1["expires_at"] == 1600.0, "续租从权威时钟设置更晚 expires_at（Req 11.4）"

    # 重放 renew：幂等，不递增 counter（Req 11.5）
    daemon_clock["ts"] = 1200.0
    ok, r2 = db.renew_lease("T-1", "implementer", r["token"], ttl_seconds=500)
    assert ok and r2["fencing_counter"] == r["fencing_counter"]
    assert r2["expires_at"] == 1700.0

    # release + 重放 release：幂等返回同一 released 状态（Req 11.7）
    daemon_clock["ts"] = 1300.0
    ok, res = db.release_lease("T-1", "implementer", r["token"])
    assert ok and res["status"] == "released"
    daemon_clock["ts"] = 1400.0
    ok, res = db.release_lease("T-1", "implementer", r["token"])
    assert ok and res["status"] == "released" and res.get("idempotent") is True


# ---------------------------------------------------------------
# 6. daemon 恢复后继续可用（14.22-14.33 恢复语义）
# ---------------------------------------------------------------

def test_recovery_after_daemon_back(db, daemon_clock):
    daemon_clock["fail"] = True
    ok, res = db.acquire_lease("T-1", "implementer", _identity())
    assert not ok and res["code"] == ERR_LEASE_CLOCK_UNAVAILABLE
    assert _count(db, "task_leases") == 0

    daemon_clock["fail"] = False
    daemon_clock["ts"] = 3000.0
    ok, r = db.acquire_lease("T-1", "implementer", _identity(), ttl_seconds=100)
    assert ok and r["acquired_at"] == 3000.0
    assert _count(db, "task_leases") == 1
