"""TP-11G · G3 —— 负向治理拒绝验收（fail-closed 门禁矩阵）。

selector: tests/acceptance/domains/governance/test_g3_negative_governance.py

验证治理/租约面的 fail-closed 拒绝（全部为 daemon 实测错误码，非猜测）：

- review 态再次 claim 被拒（禁双重领取）；
- review 态无 pass verdict 时 apply/close 被拒（E_VERDICT_REQUIRED）；
- close 缺 lease 凭证被拒（E_LEASE_REQUIRED）；
- handoff 缺 lease 凭证被拒（E_LEASE_REQUIRED）；
- report 缺 daemon authority snapshot 被拒（E_TASK_REPORT_SNAPSHOT_REQUIRED）；
- 未注册 agent claim 被拒（E_IDENTITY_UNREGISTERED）；
- supersede 未注册身份被拒（E_IDENTITY_UNREGISTERED）；
- supersede → superseded_by 作废出口 + cascade_close 收尾。
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


def _create(env: GovernanceEnv, title: str) -> str:
    cr = env.task_create(title, "g3 negative", steps=[
        {"action": "annotate", "target_file": "docs/note.md"}])
    assert cr.ok, f"task.create 失败：{cr!r}"
    return cr.get("task_id")


def _claim(env: GovernanceEnv, tid: str) -> str:
    cl = env.task_claim(tid, "executor")
    assert cl.ok, f"task.claim 失败：{cl!r}"
    return cl.get("step_id")


def _push_to_review(env: GovernanceEnv, tid: str, step_id: str) -> None:
    """完整执行到 review 态（lease → snapshot → report → handoff → release）。"""
    la = env.lease_acquire(tid, "executor")
    assert la.ok
    token = env.extract_token(la)
    snap = env.snapshot_publish()
    assert snap.ok and snap.get("snapshot_id")
    ev_path, ev_hash = env.write_evidence(
        f"tp11g_g3_report_{tid}.json",
        {"task_id": tid, "step_id": step_id, "kind": "step_report"})
    rp = env.task_report(tid, step_id, "executor", snapshot_id=snap.get("snapshot_id"),
                         evidence_path=ev_path, evidence_hash=ev_hash)
    assert rp.ok, f"task.report 失败：{rp!r}"
    ho = env.task_handoff(tid, "executor", "reviewer",
                          outcome="executor_ready_for_review",
                          next_action="review", step_id=step_id,
                          report_request_id=rp.get("request_id") or "",
                          evidence_path=ev_path, evidence_hash=ev_hash,
                          lease_token=token,
                          fencing_counter=env.extract_counter(la))
    assert ho.ok, f"task.handoff 失败：{ho!r}"
    assert env.lease_release(tid, "executor", token).ok


def test_g3_review_state_claim_rejected(env):
    """review 态再次 claim 必须被拒（禁双重领取）。"""
    tid = _create(env, "tp11g g3 review claim")
    step_id = _claim(env, tid)
    _push_to_review(env, tid, step_id)
    st = env.task_status(tid)
    assert st.ok and st.get("status") == "review"

    again = env.task_claim(tid, "executor")
    assert not again.ok, f"review 态再次 claim 应被拒：{again!r}"
    assert again.code, "拒绝必须带结构化错误码"


def test_g3_apply_without_verdict_rejected(env):
    """review 态无 pass verdict 时 apply 被拒（E_VERDICT_REQUIRED）。"""
    tid = _create(env, "tp11g g3 apply no verdict")
    step_id = _claim(env, tid)
    _push_to_review(env, tid, step_id)

    rl = env.lease_acquire(tid, "reviewer")
    assert rl.ok
    ap = env.task_apply(tid, "reviewer", lease_token=env.extract_token(rl),
                        fencing_counter=env.extract_counter(rl))
    assert not ap.ok, f"无 verdict 的 apply 应被拒：{ap!r}"
    assert ap.code == "E_VERDICT_REQUIRED", f"实际错误码 {ap.code}"

    # 任务状态不得被改变
    st = env.task_status(tid)
    assert st.get("status") == "review", f"被拒的 apply 不得改状态：{st.get('status')}"


def test_g3_close_without_lease_rejected(env):
    """close 缺 lease 凭证被拒（E_LEASE_REQUIRED，写入前拒绝）。"""
    tid = _create(env, "tp11g g3 close no lease")
    clo = env.task_close(tid, "adjudicator")
    assert not clo.ok and clo.code == "E_LEASE_REQUIRED", f"实际：{clo!r}"
    st = env.task_status(tid)
    assert st.get("status") == "open", "被拒的 close 不得改状态"


def test_g3_handoff_without_lease_rejected(env):
    """handoff 缺 lease 凭证被拒（E_LEASE_REQUIRED）。"""
    tid = _create(env, "tp11g g3 handoff no lease")
    step_id = _claim(env, tid)
    ev_path, ev_hash = env.write_evidence(
        f"tp11g_g3_handoff_{tid}.json", {"task_id": tid, "kind": "handoff"})
    ho = env.task_handoff(tid, "executor", "reviewer",
                          outcome="executor_ready_for_review",
                          next_action="review", step_id=step_id,
                          evidence_path=ev_path, evidence_hash=ev_hash)
    assert not ho.ok and ho.code == "E_LEASE_REQUIRED", f"实际：{ho!r}"


def test_g3_report_without_snapshot_rejected(env):
    """report 缺 daemon authority snapshot 被拒（E_TASK_REPORT_SNAPSHOT_REQUIRED）。"""
    tid = _create(env, "tp11g g3 report no snapshot")
    step_id = _claim(env, tid)
    rp = env.task_report(tid, step_id, "executor")
    assert not rp.ok, f"缺 snapshot 的 report 应被拒：{rp!r}"
    assert rp.code == "E_TASK_REPORT_SNAPSHOT_REQUIRED", f"实际错误码 {rp.code}"


def test_g3_unregistered_agent_claim_rejected(env):
    """未注册 agent 的 claim 被拒（E_IDENTITY_UNREGISTERED，fail-closed）。"""
    tid = _create(env, "tp11g g3 unregistered claim")
    r = env.rpc("task.claim", {
        "task_id": tid,
        "identity": {
            "agent_id": "tp11g-ghost-agent",
            "session_id": "tp11g-ghost-sess",
            "model_id": "tp11g-ghost-model",
            "role": "implementer",
            "agent_instance_id": "tp11g-ghost-inst",
        },
        "agent_session_id": "tp11g-ghost-sess",
    })
    assert not r.ok, f"未注册身份 claim 应被拒：{r!r}"
    assert r.code == "E_IDENTITY_UNREGISTERED", f"实际错误码 {r.code}"


def test_g3_supersede_and_cascade_close(env):
    """supersede 作废出口：old 被 new 承接 + superseded_by 可查 + cascade_close。

    cascade_close 的**被替代卡豁免**（task_collab_lifecycle_apply.rs:508）：
    有 supersede 出边的叶子卡即使步骤未 done 也可收尾——「步骤已由后继卡
    scope 承接，属作废遗留」。这正是 E2E 作废出口的完整语义。
    """
    old_tid = _create(env, "tp11g g3 supersede old")
    new_tid = _create(env, "tp11g g3 supersede new")

    # 未注册身份的 supersede 被拒（治理写 fail-closed）
    ghost = env.rpc("task.supersede", {
        "superseded_id": old_tid, "superseding_id": new_tid,
        "reason": "tp11g ghost supersede",
        "request_id": f"tp11g-ghost-sup-{old_tid}",
        "workspace_instance_id": env.setup_workspace()[1],
        "identity": {
            "agent_id": "tp11g-ghost-agent",
            "session_id": "tp11g-ghost-sess",
            "model_id": "tp11g-ghost-model",
            "role": "adjudicator",
            "agent_instance_id": "tp11g-ghost-inst"}})
    assert not ghost.ok, f"未注册身份 supersede 应被拒：{ghost!r}"

    # 已注册 adjudicator 的 supersede 成功（append-only 关系，不改被替代任务行）。
    # supersede 是治理写：reviewer lease 必须持在**被替代任务（superseded_id）**
    # 上（task_supersede.rs:454 用 superseded_id 校验 adjudication lease）。
    sup_lease = env.lease_acquire(old_tid, "reviewer")
    assert sup_lease.ok, f"supersede 前置 reviewer lease 失败：{sup_lease!r}"
    sup = env.task_supersede(old_tid, new_tid, reason="tp11g g3 supersede",
                             lease_token=env.extract_token(sup_lease),
                             fencing_counter=env.extract_counter(sup_lease))
    assert sup.ok, f"task.supersede 失败：{sup!r}"

    # superseded_by 反向可查
    sb = env.task_superseded_by(old_tid)
    assert sb.ok, f"task.superseded_by 失败：{sb!r}"
    blob = __import__("json").dumps(sb.result, ensure_ascii=False)
    assert new_tid in blob, f"superseded_by 应指向 new：{sb.result}"

    # append-only：supersede 本身不改变被替代任务状态行
    st_old = env.task_status(old_tid)
    assert st_old.get("status") == "open", \
        f"supersede 不得改被替代任务状态行：{st_old.get('status')}"

    # cascade_close：被替代卡豁免步骤未 done → 可收尾
    cc = env.task_cascade_close(old_tid)
    assert cc.ok, f"task.cascade_close 失败：{cc!r}"
    closed = cc.get("closed") or []
    assert old_tid in closed, \
        f"被替代卡应被 cascade_close 收尾：closed={closed} blocked={cc.get('blocked')}"
    st_old2 = env.task_status(old_tid)
    assert st_old2.get("status") == "closed", \
        f"cascade_close 后 old 应为 closed：{st_old2.get('status')}"

    # 幂等：再次 cascade_close 命中 skipped 分支
    cc2 = env.task_cascade_close(old_tid)
    assert cc2.ok
    assert old_tid in (cc2.get("skipped") or []), \
        f"重复 cascade_close 应走 skipped：{cc2.result}"


def test_g3_cascade_close_blocked_reports_reason(env):
    """GOV-FIX-06：cascade_close 门禁未满足时必须记 blocked（禁假成功）。

    未被替代的叶子卡且有 **pending** 步骤 → cascade_close 返回 ok 但 blocked
    带 leaf_steps_pending，任务不得被静默关闭。
    注意：claim 后步骤为 in_progress（不在 pending/blocked 集合）不阻塞；
    只有未领取的 pending 步骤阻塞。
    """
    tid = _create(env, "tp11g g3 cascade blocked")
    # 不 claim：步骤保持 pending → 阻塞

    cc = env.task_cascade_close(tid)
    assert cc.ok, f"cascade_close 应返回 ok（带 blocked 语义）：{cc!r}"
    blocked = cc.get("blocked")
    assert blocked, f"门禁未满足必须记 blocked（GOV-FIX-06 禁假成功）：{cc.result}"
    assert blocked.get("task_id") == tid
    assert blocked.get("reason") in ("leaf_steps_pending", "children_not_closed"), \
        f"blocked reason 应可程序化判定：{blocked}"
    assert tid not in (cc.get("closed") or []), "blocked 卡不得出现在 closed"

    st = env.task_status(tid)
    assert st.get("status") != "closed", \
        f"blocked 的卡不得被关闭：{st.get('status')}"
