"""10.10 属性测试：Property 11 fencing 安全性（Requirement 11.2-11.3）

Property：对任意 lease 获取/续租/释放/重新获取序列，一旦新 counter N 发布，
之后所有 counter < N 的 protected mutation 永远被拒绝（无论 token 新旧）。

测试策略：
- 确定性种子 random 生成任意操作序列（acquire/renew/release/re-acquire）
- 每个 trial 结束后发布新 lease（counter N），断言全部历史 (counter, token) 组合被拒
- 额外注入：token 被篡改、counter 乱序、跨 trial 混合提交，全部拒绝
"""
import os
import random

import pytest

from callwarden.db import CodeGraphDB
from callwarden.db.db_task_leases import (
    ERR_LEASE_FENCING_STALE,
    ERR_LEASE_HOLDER_MISMATCH,
    ERR_LEASE_TOKEN_MISMATCH,
)


def _identity(agent_id="agent-a", session_id="sess-1", model_id="model-x", role="implementer"):
    return {"agent_id": agent_id, "session_id": session_id, "model_id": model_id, "role": role}


@pytest.fixture()
def db(tmp_path):
    os.environ["CW_USE_RUST_STORAGE"] = "0"
    d = CodeGraphDB(str(tmp_path / "p4prop.db"))
    d.register_workspace("prop-ws", str(tmp_path))
    d.set_active_workspace("prop-ws")
    yield d
    try:
        d.conn.close()
    except Exception:
        pass


def _run_random_sequence(db, rng, task, n_ops):
    """执行随机 acquire/renew/release 序列，返回发布过的 [(counter, token), ...]。"""
    current = None  # (counter, token)
    issued = []  # 全部发布过的 lease 凭证
    for _ in range(n_ops):
        op = rng.choice(["acquire", "renew", "release"])
        if op == "acquire" or current is None:
            ok, r = db.acquire_lease(task, "implementer", _identity())
            if ok:
                current = (r["fencing_counter"], r["token"])
                issued.append(current)
        elif op == "renew":
            ok, _ = db.renew_lease(task, "implementer", current[1])
            if ok:
                issued.append(current)
        else:  # release
            ok, _ = db.release_lease(task, "implementer", current[1])
            if ok:
                current = None
    return issued, current


def test_fencing_property_all_sequences(db):
    rng = random.Random(20260803)
    for trial in range(12):
        task = f"T-prop-{trial}"
        issued, _current = _run_random_sequence(db, rng, task, rng.randint(3, 8))

        # 发布新 lease N（最终权威 counter）；先释放当前 active lease
        if _current is not None:
            db.release_lease(task, "implementer", _current[1])
        ok, rn = db.acquire_lease(task, "implementer", _identity())
        assert ok
        n = rn["fencing_counter"]
        assert all(c < n for c, _ in issued), "历史 counter 必须全部小于新 N"

        # 所有历史凭证（token+counter）一律拒绝
        for counter, token in issued:
            valid, res = db.validate_lease_for_mutation(task, "implementer", token, counter)
            assert not valid, f"历史凭证必须被拒 (trial={trial}, counter={counter})"
            assert res["code"] in (ERR_LEASE_FENCING_STALE, ERR_LEASE_TOKEN_MISMATCH)

        # 新 token + 历史 counter 也拒绝（fencing 以 counter 为准）
        for counter, _ in issued:
            valid, res = db.validate_lease_for_mutation(task, "implementer", rn["token"], counter)
            assert not valid and res["code"] == ERR_LEASE_FENCING_STALE


def test_fencing_property_token_tamper_and_order(db):
    """篡改 token / counter 乱序（超出发布集合）也必须拒绝。"""
    rng = random.Random(7)
    task = "T-tamper"
    issued, _current = _run_random_sequence(db, rng, task, 6)
    if _current is not None:
        db.release_lease(task, "implementer", _current[1])

    ok, rn = db.acquire_lease(task, "implementer", _identity())
    assert ok, "无 active lease 时 acquire 必须成功"
    n = rn["fencing_counter"]

    # token 篡改（合法 counter + 乱写 token）
    for counter, _ in issued:
        valid, res = db.validate_lease_for_mutation(task, "implementer", "tampered", counter)
        assert not valid
        assert res["code"] in (ERR_LEASE_TOKEN_MISMATCH, ERR_LEASE_FENCING_STALE)

    # counter 乱序：发布集合外的任意数（含负数、0、N 之后的超量）
    for weird in (n + 5, n + 100, 0, -3, n + 1):
        valid, res = db.validate_lease_for_mutation(task, "implementer", rn["token"], weird)
        assert not valid, f"乱序 counter {weird} 必须拒绝"


def test_fencing_property_holder_cross_trial(db):
    """跨 trial：不同持有者的历史凭证不能用于新的 lease（身份绑定 + fencing 双保险）。"""
    rng = random.Random(99)
    task = "T-cross"
    # agent-a 的序列
    issued_a, _current_a = _run_random_sequence(db, rng, task, 4)
    if _current_a is not None:
        db.release_lease(task, "implementer", _current_a[1])
    # agent-b 接管
    ok, rb = db.acquire_lease(task, "implementer", _identity("agent-b"))
    assert ok
    # agent-a 的任何历史凭证提交 → 拒绝
    for counter, token in issued_a:
        valid, res = db.validate_lease_for_mutation(task, "implementer", token, counter)
        assert not valid
    # agent-b 用 agent-a 身份提交自己 token → holder mismatch
    valid, res = db.validate_lease_for_mutation(
        task, "implementer", rb["token"], rb["fencing_counter"], identity=_identity("agent-a")
    )
    assert not valid and res["code"] == ERR_LEASE_HOLDER_MISMATCH
