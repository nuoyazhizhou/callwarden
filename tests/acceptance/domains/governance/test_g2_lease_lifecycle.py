"""TP-11G · G2 —— 租约全生命周期验收（P4 Lease，Req 11.2-11.7）。

selector: tests/acceptance/domains/governance/test_g2_lease_lifecycle.py

在隔离 daemon 上验证受保护写 Lease 的完整生命周期与门禁：

- acquire 返回 raw token（仅此一次）与 fencing_counter；DB/审计只存 hash；
- status / list_events **绝不泄露 raw token**（只回 token_hash）；
- renew 幂等（同 token + 同 counter → counter 不变）；旧 counter 被拒
  （E_LEASE_FENCING_STALE）；
- 错 token 被拒（E_LEASE_TOKEN_MISMATCH）；
- 跨角色凭证被拒（executor 的 token 不能动 reviewer 的 lease）；
- 同 role 已有 active lease 时再 acquire 被拒（E_LEASE_ACTIVE_EXISTS）；
- release 幂等；release 后 status 不再 active。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from governance_harness import GovernanceEnv  # noqa: E402


@pytest.fixture(scope="module")
def env():
    e = GovernanceEnv()
    e.start_daemon()
    try:
        e.build_graph()
        for role_key, res in e.register_all_roles().items():
            assert res.ok, f"agent.register {role_key} 失败：{res!r}"
        yield e
    finally:
        e.stop_daemon()


def _make_claimed_task(env: GovernanceEnv, title: str) -> tuple:
    """create + claim，返回 (task_id, step_id)。"""
    cr = env.task_create(title, "g2 lease", steps=[
        {"action": "annotate", "target_file": "docs/note.md"}])
    assert cr.ok, f"task.create 失败：{cr!r}"
    tid = cr.get("task_id")
    cl = env.task_claim(tid, "executor")
    assert cl.ok, f"task.claim 失败：{cl!r}"
    return tid, cl.get("step_id")


def test_g2_lease_acquire_status_renew_release(env):
    """acquire → status → renew(幂等) → release 全链路 + token 不泄露。"""
    tid, _ = _make_claimed_task(env, "tp11g g2 lease lifecycle")

    la = env.lease_acquire(tid, "executor")
    assert la.ok, f"lease.acquire 失败：{la!r}"
    token = env.extract_token(la)
    assert token, f"acquire 必须返回 raw token：{la.result}"
    counter = env.extract_counter(la)
    assert isinstance(counter, int) and counter >= 1

    # status：active + token_hash，且绝不回显 raw token
    st = env.lease_status(tid)
    assert st.ok, f"lease.status 失败：{st!r}"
    assert st.get("status") == "active"
    assert st.get("token_hash"), "status 应回 token_hash"
    assert token not in (st.result and __import__("json").dumps(
        st.result, ensure_ascii=False)), "lease.status 泄露 raw token"

    # list_events：审计只存 hash
    ev = env.lease_list_events(tid)
    assert ev.ok, f"lease.list_events 失败：{ev!r}"
    blob = __import__("json").dumps(ev.result, ensure_ascii=False)
    assert token not in blob, "lease.list_events 泄露 raw token"
    assert any(e.get("event_type") == "acquire" for e in (ev.result or []))

    # renew 幂等：同 token + 同 counter → counter 不变
    rn = env.lease_renew(tid, "executor", token)
    assert rn.ok, f"lease.renew 失败：{rn!r}"
    rn_counter = env.extract_counter(rn)
    assert rn_counter == counter, \
        f"renew 后 fencing_counter 应不变（{counter} → {rn_counter}）"

    # 旧 counter 续租被拒（Property 11：旧持有者写入被拒）
    stale = env.lease_renew(tid, "executor", token)
    stale_counter = (env.extract_counter(stale) or counter) - 1
    rn_bad = env.rpc("lease.renew", {
        "task_id": tid, "role": env.lease_role("executor"),
        "token": token,
        "identity": env._lease_identity("executor"),
        "ttl_seconds": 3600.0,
        "fencing_counter": max(stale_counter, 0)})
    assert not rn_bad.ok and rn_bad.code == "E_LEASE_FENCING_STALE", \
        f"旧 counter 续租应被拒：{rn_bad!r}"

    # 错 token 被拒
    rn_wrong = env.lease_renew(tid, "executor", "0" * 64)
    assert not rn_wrong.ok and rn_wrong.code == "E_LEASE_TOKEN_MISMATCH", \
        f"错 token 续租应被拒：{rn_wrong!r}"

    # release 幂等 + release 后不再 active
    rel = env.lease_release(tid, "executor", token)
    assert rel.ok, f"lease.release 失败：{rel!r}"
    st2 = env.lease_status(tid)
    assert st2.ok
    assert st2.get("status") != "active", \
        f"release 后 lease 不应仍 active：{st2.result}"

    # release 后可重新 acquire（同一 role）
    la2 = env.lease_acquire(tid, "executor")
    assert la2.ok, f"release 后重新 acquire 失败：{la2!r}"
    assert env.extract_token(la2)
    env.lease_release(tid, "executor", env.extract_token(la2))


def test_g2_lease_cross_role_and_conflict(env):
    """跨角色凭证拒绝 + 同 role 冲突（E_LEASE_ACTIVE_EXISTS）。"""
    tid, _ = _make_claimed_task(env, "tp11g g2 lease cross role")

    la_exec = env.lease_acquire(tid, "executor")
    assert la_exec.ok
    exec_token = env.extract_token(la_exec)
    la_rev = env.lease_acquire(tid, "reviewer")
    assert la_rev.ok, f"不同 role 应可分别持有 lease：{la_rev!r}"
    rev_token = env.extract_token(la_rev)
    assert rev_token != exec_token

    # 同 role 已有 active lease → 再 acquire 被拒
    dup = env.lease_acquire(tid, "executor")
    assert not dup.ok and dup.code == "E_LEASE_ACTIVE_EXISTS", \
        f"同 role 重复 acquire 应被拒：{dup!r}"

    # 跨角色凭证：用 reviewer token 去 release executor lease → token 不匹配
    cross = env.lease_release(tid, "executor", rev_token)
    assert not cross.ok and cross.code == "E_LEASE_TOKEN_MISMATCH", \
        f"跨角色 token 应被拒：{cross!r}"

    # 清理：各自 release
    assert env.lease_release(tid, "executor", exec_token).ok
    assert env.lease_release(tid, "reviewer", rev_token).ok


def test_g2_lease_holder_identity_mismatch(env):
    """holder 身份三元组与 lease 不一致 → E_LEASE_HOLDER_MISMATCH。"""
    tid, _ = _make_claimed_task(env, "tp11g g2 lease holder mismatch")
    la = env.lease_acquire(tid, "executor")
    assert la.ok
    token = env.extract_token(la)
    counter = env.extract_counter(la)

    # 用 reviewer 的身份三元组去续租 executor 的 lease
    wrong = env.rpc("lease.renew", {
        "task_id": tid, "role": env.lease_role("executor"),
        "token": token,
        "identity": env._lease_identity("reviewer"),
        "ttl_seconds": 3600.0,
        "fencing_counter": counter})
    assert not wrong.ok and wrong.code in (
        "E_LEASE_HOLDER_MISMATCH", "E_LEASE_TOKEN_MISMATCH"), \
        f"holder 不一致应被拒：{wrong!r}"

    assert env.lease_release(tid, "executor", token).ok
