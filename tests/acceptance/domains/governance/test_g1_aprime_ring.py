"""TP-11G · G1 —— A′ 治理环全生命周期验收（Executor→Reviewer→Adjudicator）。

selector: tests/acceptance/domains/governance/test_g1_aprime_ring.py

在完全隔离的 daemon 上把 A′ 环**真实走完一遍**，每一步断言 daemon 权威
投影的状态跃迁（不本地推导状态）：

    build_graph → agent.register×3 → task.create → task.claim →
    lease.acquire(executor) → snapshot.publish → task.report →
    task.handoff(executor→reviewer) → lease.release(executor) →
    lease.acquire(reviewer) → verdict.submit → task.apply →
    task.close(adjudicator 持 reviewer lease)

关键门禁（全部来自 daemon 实测，非猜测）：
- task.create 必须携带 A′ 三角色 legacy 合同（daemon 不建默认模板，缺合同
  任务 governance_blocked 且无法 claim）；
- task.create 的 workspace_id 是**权威库自增 id**（registry numeric id 是
  另一个 ID 空间），按 root_path 只读解析；
- snapshot_id 只在 register 提供 git provenance 时计算，task.report 必须携带
  daemon authority 发布的 snapshot_id（E_TASK_REPORT_SNAPSHOT_REQUIRED）；
- task.handoff 的 from/next_role 用**合同角色名**（executor/reviewer）、
  independence_requirement=required，且 step/evidence 三元组必须与已持久化
  report 事件一致（E_HANDOFF_REPORT_PROVENANCE_MISMATCH）；
- close 的 holder 校验只比对 agent_id/session_id/model_id：adjudicator 以
  reviewer 三元组 + role=adjudicator 执行（P0-E 语义）。
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
    """模块级隔离环境：一个 daemon + 一个 workspace，G1 全测试共享。"""
    e = GovernanceEnv()
    e.start_daemon()
    try:
        e.build_graph()
        for role_key, res in e.register_all_roles().items():
            assert res.ok, f"agent.register {role_key} 失败：{res!r}"
        yield e
    finally:
        e.stop_daemon()


def _status_of(env: GovernanceEnv, task_id: str) -> dict:
    st = env.task_status(task_id)
    assert st.ok, f"task.status 失败：{st!r}"
    return st.result or {}


def _gov_of(status: dict) -> dict:
    return status.get("governance") or status.get("governance_projection") or {}


def test_g1_ring_open_to_claim(env):
    """create → claim：open/queued → in_progress/execution_in_progress。"""
    cr = env.task_create(
        "tp11g g1 aprime ring", "g1 full ring",
        steps=[{"action": "annotate", "target_file": "docs/note.md"}])
    assert cr.ok, f"task.create 失败：{cr!r}"
    tid = cr.get("task_id")
    assert tid, f"create 响应缺 task_id：{cr.result}"
    # 默认三角色合同必须落库（否则任务无法 claim）
    assert cr.get("contract_count") == 3, \
        f"A′ 三角色合同未落库（contract_count={cr.get('contract_count')}）"
    assert cr.get("status") == "open"

    st = _status_of(env, tid)
    assert st.get("status") == "open"
    gov = _gov_of(st)
    assert gov.get("decision") == "READY", f"创建后应 READY，实际 {gov.get('decision')}"
    assert gov.get("action") == "CLAIM"
    assert gov.get("required_role") == "executor"
    step_id = gov.get("step_id")
    assert step_id, "READY 投影必须给出可 claim 的 step_id"

    cl = env.task_claim(tid, "executor")
    assert cl.ok, f"task.claim 失败：{cl!r}"
    assert cl.get("status") == "in_progress"
    assert cl.get("claimed_by") == env.identity("executor")["session_id"]
    assert cl.get("step_id") == step_id
    assert cl.get("step_status") == "in_progress"

    st2 = _status_of(env, tid)
    assert st2.get("status") == "in_progress"
    assert _gov_of(st2).get("workflow_status") == "execution_in_progress"

    # 供同模块后续测试复用（模块级单例，串行执行）
    env._g1_task_id = tid
    env._g1_step_id = step_id


def test_g1_ring_execute_to_review(env):
    """lease acquire → snapshot → report → handoff → release → review_pending。"""
    tid = getattr(env, "_g1_task_id", None)
    step_id = getattr(env, "_g1_step_id", None)
    assert tid and step_id, "依赖 test_g1_ring_open_to_claim 先执行"

    la = env.lease_acquire(tid, "executor")
    assert la.ok, f"lease.acquire(executor) 失败：{la!r}"
    exec_token = env.extract_token(la)
    assert exec_token, f"acquire 响应未返回 raw token：{la.result}"
    exec_counter = env.extract_counter(la)
    assert isinstance(exec_counter, int) and exec_counter >= 1
    env._g1_exec_token = exec_token
    env._g1_exec_counter = exec_counter

    # snapshot 必须先发布：report 的 snapshot_id 前置
    snap = env.snapshot_publish()
    assert snap.ok, f"snapshot.publish 失败：{sn!r}"
    snap_id = snap.get("snapshot_id")
    assert snap_id, f"git provenance 下 publish 必须返回 snapshot_id：{snap.result}"
    env._g1_snapshot_id = snap_id

    # report 与 handoff 共用同一 evidence（handoff provenance 三元组校验）
    ev_path, ev_hash = env.write_evidence(
        f"tp11g_g1_report_{tid}.json",
        {"task_id": tid, "step_id": step_id, "kind": "step_report"})
    rp = env.task_report(tid, step_id, "executor", summary="tp11g g1 step done",
                         snapshot_id=snap_id,
                         evidence_path=ev_path, evidence_hash=ev_hash)
    assert rp.ok, f"task.report 失败：{rp!r}"
    assert rp.get("status") == "review"
    assert rp.get("snapshot_id") == snap_id

    ho = env.task_handoff(tid, "executor", "reviewer",
                          outcome="executor_ready_for_review",
                          next_action="review",
                          step_id=step_id,
                          report_request_id=rp.get("request_id") or "",
                          evidence_path=ev_path, evidence_hash=ev_hash,
                          lease_token=exec_token,
                          fencing_counter=exec_counter)
    assert ho.ok, f"task.handoff 失败：{ho!r}"

    # executor lease 必须释放，否则 review 态阻塞在 wait_for_current_lease
    rel = env.lease_release(tid, "executor", exec_token)
    assert rel.ok, f"lease.release(executor) 失败：{rel!r}"
    assert rel.get("status") == "released"

    st = _status_of(env, tid)
    assert st.get("status") == "review"
    gov = _gov_of(st)
    assert gov.get("workflow_status") == "review_pending"
    assert gov.get("required_role") == "reviewer"
    assert gov.get("action") == "REVIEW"
    assert (st.get("review") or {}).get("state") == "pending"


def test_g1_ring_reviewer_verdict(env):
    """reviewer lease → verdict.submit（blind_first_pass, pass）。"""
    tid = getattr(env, "_g1_task_id", None)
    step_id = getattr(env, "_g1_step_id", None)
    snap_id = getattr(env, "_g1_snapshot_id", None)
    assert tid and step_id and snap_id

    rl = env.lease_acquire(tid, "reviewer")
    assert rl.ok, f"lease.acquire(reviewer) 失败：{rl!r}"
    rev_token = env.extract_token(rl)
    assert rev_token
    rev_counter = env.extract_counter(rl)
    env._g1_rev_token = rev_token
    env._g1_rev_counter = rev_counter

    # verdict 绑定权威来源：next_action 的 task_contract / role_contract
    na = env.next_action(tid)
    assert na.ok, f"task.next_action 失败：{na!r}"
    na_gov = na.result or {}
    t_contract = na_gov.get("task_contract") or {}
    r_contract = na_gov.get("role_contract") or {}
    assert t_contract.get("id") and t_contract.get("hash"), \
        f"next_action 缺 task_contract 绑定：{t_contract}"
    assert (r_contract.get("lineage_id") or r_contract.get("id")) and \
        r_contract.get("hash"), f"next_action 缺 role_contract 绑定：{r_contract}"

    vs = env.verdict_submit(tid, step_id, t_contract, r_contract, snap_id,
                            lease_token=rev_token, fencing_counter=rev_counter)
    assert vs.ok, f"verdict.submit 失败：{vs!r}"
    assert vs.get("success") is True
    verdict_id = vs.get("verdict_id")
    assert verdict_id
    env._g1_verdict_id = verdict_id

    # verdict 幂等重放（同 request_id）：不得产生重复 verdict/event。
    # 注意 daemon 的 dedup 直接回放首次响应信封，故 replayed 标志可能仍为
    # False——幂等性的可观测判据是 verdict_id/event_id 不变（无第二份入账）。
    vs2 = env.verdict_submit(tid, step_id, t_contract, r_contract, snap_id,
                             lease_token=rev_token, fencing_counter=rev_counter,
                             request_id=f"tp11g-verdict-{tid}")
    assert vs2.ok, f"verdict 重放失败：{vs2!r}"
    assert vs2.get("verdict_id") == verdict_id, \
        f"同 request_id 重放不得产生新 verdict：{verdict_id} vs {vs2.get('verdict_id')}"
    assert vs2.get("event_id") == vs.get("event_id"), \
        f"同 request_id 重放不得产生新 event：{vs.get('event_id')} vs {vs2.get('event_id')}"


def test_g1_ring_apply_and_close(env):
    """apply（review→applied）→ close（applied→closed，P0-E 语义）。"""
    tid = getattr(env, "_g1_task_id", None)
    rev_token = getattr(env, "_g1_rev_token", "")
    rev_counter = getattr(env, "_g1_rev_counter", -1)
    assert tid and rev_token

    ap = env.task_apply(tid, "reviewer", lease_token=rev_token,
                        fencing_counter=rev_counter)
    assert ap.ok, f"task.apply 失败：{ap!r}"
    assert ap.get("status") == "applied"

    st = _status_of(env, tid)
    assert st.get("status") == "applied"
    assert _gov_of(st).get("workflow_status") == "applied_pending_close"

    # close：adjudicator 持 reviewer lease（holder 三元组 = reviewer，
    # role 标 adjudicator）；adjudicator 无法另 acquire（同 role lease 已存）
    clo = env.task_close(tid, "adjudicator", lease_token=rev_token,
                         fencing_counter=rev_counter,
                         lease_holder_key="reviewer")
    assert clo.ok, f"task.close 失败：{clo!r}"
    assert clo.get("status") == "closed"

    st2 = _status_of(env, tid)
    assert st2.get("status") == "closed"
    gov = _gov_of(st2)
    assert gov.get("lifecycle_status") == "closed"
    assert gov.get("workflow_status") == "completed"
    assert gov.get("decision") == "COMPLETE"
    assert gov.get("next_action") == "finalize"
    assert not (st2.get("blocking_reasons") or []), \
        f"closed 态不应有阻塞原因：{st2.get('blocking_reasons')}"

    # 全链路 lease 审计：executor acquire/release + reviewer acquire
    ev = env.lease_list_events(tid)
    assert ev.ok, f"lease.list_events 失败：{ev!r}"
    types = [(e.get("role"), e.get("event_type"))
             for e in (ev.result or [])]
    assert ("executor", "acquire") in types
    assert ("executor", "release") in types
    assert ("reviewer", "acquire") in types
