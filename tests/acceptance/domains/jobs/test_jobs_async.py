"""TP-11J 异步作业/取消/重试验收：提交可观测性 + 取消语义 + 读面 fail-closed。

在完全隔离的 daemon（独立 run 根 + git provenance workspace + RPC 直调）上
验证异步作业面，每步断言 daemon 权威响应。

🔴 本套件的核心价值：修复前 `task.job_submit` 只写进程内内存注册表，
**从不落 jobs 表**，导致 job_status / list_jobs / wait_for_job / job_stats
四个读面永远观测不到已提交作业（提交成功却查不到）。J1 即该可观测性的
回归护栏。
"""

from __future__ import annotations

import json

import pytest

from jobs_harness import JobsEnv

# 确定性终态 job_type：fixture workspace 无 CODEOWNERS → 必然 failed，
# 不依赖外部工具/网络，适合断言终态语义。
TERMINAL_JOB_TYPE = "codeowners"


@pytest.fixture(scope="module")
def env():
    e = JobsEnv()
    # run 根自清洁（防上一轮残留 daemon/registry 污染本轮断言）
    JobsEnv.wipe_run_root(e.run_root)
    e = JobsEnv()
    e.start_daemon()
    try:
        e.build_graph()
        e.job_executor_start()
        yield e
    finally:
        e.stop_daemon()


def test_j1_submit_is_observable_by_read_surface(env):
    """J1（回归护栏）：提交的作业必须能被 DB 读面观测到。

    修复前 submit 只写内存 → job_status 返回 {"error": "job not found"}、
    list_jobs 恒空、job_stats 全 0。修复后提交即可查。
    """
    sub = env.job_submit(TERMINAL_JOB_TYPE)
    assert sub.ok, f"job_submit 失败：{sub!r}"
    jid = sub.get("job_id")
    assert jid, f"submit 应回 job_id：{sub.result}"
    assert sub.get("status") == "queued"
    assert sub.get("job_type") == TERMINAL_JOB_TYPE

    # 读面可观测（核心断言）。
    # 🔴 判别依据是 result 里有没有 job_id：查不到时读面返回
    # {"error": "job not found: ..."}（无 job_id）；而真实行自带 error 字段
    # （失败作业的非空错误信息），故不能用 "error" not in result 判别。
    st = env.job_status(jid)
    assert st.ok, f"job_status 失败：{st!r}"
    assert st.result.get("job_id") == jid, \
        f"已提交作业必须可查（修复前恒 job not found）：{st.result}"
    assert st.result.get("job_type") == TERMINAL_JOB_TYPE
    # 生命周期字段落库（pending → running → 终态）
    assert st.result.get("created_at"), f"应落 created_at：{st.result}"

    # list_jobs 能捞出它
    lst = env.list_jobs()
    assert lst.ok, f"list_jobs 失败：{lst!r}"
    ids = [j.get("job_id") for j in (lst.result or [])]
    assert jid in ids, f"list_jobs 应包含已提交作业：{ids}"

    # job_stats 计数非零（不再全 0）
    stats = env.job_stats()
    assert stats.ok, f"job_stats 失败：{stats!r}"
    assert (stats.result or {}).get("total", 0) >= 1, \
        f"job_stats total 应 >=1（修复前恒 0）：{stats.result}"

    # wait_for_job 对已提交作业能返回终态（不再 not found）
    waited = env.wait_for_job(jid, timeout=30.0, poll_interval=0.2)
    assert waited.ok, f"wait_for_job 失败：{waited!r}"
    assert waited.result.get("job_id") == jid, \
        f"wait_for_job 应观测到已提交作业：{waited.result}"
    assert waited.result.get("status") in (
        "pending", "running", "completed", "failed", "cancelled", "timeout"), \
        f"wait_for_job 状态应合法：{waited.result}"


def test_j1b_sync_submit_returns_terminal_state(env):
    """J1b：sync=true 同步执行，返回终态（确定性：无 CODEOWNERS → failed）。"""
    res = env.job_submit(TERMINAL_JOB_TYPE, sync=True)
    assert res.ok, f"sync submit 失败：{res!r}"
    assert res.get("status") in ("completed", "failed"), \
        f"sync submit 应直接返回终态：{res.result}"
    # 终态落库：读面可见且状态一致
    jid = res.get("job_id")
    st = env.job_status(jid)
    assert st.ok and st.result.get("job_id") == jid, \
        f"sync 作业应落库可查：{st!r}"
    assert st.result.get("status") in ("completed", "failed")


def test_j2_cancel_semantics(env):
    """J2：取消语义——不存在拒绝、终态幂等（回显原状态而非 cancelled）。"""
    # 不存在 → job_not_found（fail-closed）
    missing = env.job_cancel("job-999999")
    assert not missing.ok and missing.code == "job_not_found", \
        f"取消不存在的作业应被拒：{missing!r}"

    # 终态作业：cancel 幂等，回显原状态（不是 cancelled）
    res = env.job_submit(TERMINAL_JOB_TYPE, sync=True)
    assert res.ok, f"sync submit 失败：{res!r}"
    jid = res.get("job_id")
    original = res.get("status")
    assert original in ("completed", "failed")

    first = env.job_cancel(jid)
    assert first.ok, f"终态 cancel 应幂等成功：{first!r}"
    assert first.get("status") == original, \
        f"终态 cancel 不得改写状态（应回显 {original}）：{first.result}"

    again = env.job_cancel(jid)
    assert again.ok and again.get("status") == original, \
        f"重复 cancel 应保持幂等：{again.result}"

    # 终态未被改写为 cancelled（DB 面一致）
    st = env.job_status(jid)
    assert st.result.get("status") == original, \
        f"DB 面状态也应保持 {original}：{st.result}"


def test_j3_invalid_params_fail_closed(env):
    """J3：写/读面越界与缺失参数一律 fail-closed（不含糊放行）。"""
    # 非法 job_type
    bogus = env.job_submit("no_such_job")
    assert not bogus.ok and bogus.code == "invalid_params", \
        f"非法 job_type 应被拒：{bogus!r}"

    # 缺 job_type
    no_type = env.rpc("task.job_submit", {
        "workspace_instance_id": env.setup_workspace()[1]})
    assert not no_type.ok and no_type.code == "invalid_params", \
        f"缺 job_type 应被拒：{no_type!r}"

    # 缺 workspace_instance_id
    no_ws = env.rpc("task.job_submit", {"job_type": TERMINAL_JOB_TYPE})
    assert not no_ws.ok and no_ws.code == "invalid_params", \
        f"缺 workspace_instance_id 应被拒：{no_ws!r}"

    # 读面越界：limit 负数
    assert not env.list_jobs(limit=-1).ok
    assert env.list_jobs(limit=-1).code == "invalid_params"
    # 读面越界：timeout / poll_interval 负数
    neg_to = env.wait_for_job("job-999999", timeout=-1.0)
    assert not neg_to.ok and neg_to.code == "invalid_params", \
        f"timeout 负数应被拒：{neg_to!r}"
    neg_pi = env.wait_for_job("job-999999", poll_interval=-1.0)
    assert not neg_pi.ok and neg_pi.code == "invalid_params", \
        f"poll_interval 负数应被拒：{neg_pi!r}"

    # 读面缺必填
    no_jid = env.rpc("task.job_status",
                     {"workspace_instance_id": env.setup_workspace()[1]})
    assert not no_jid.ok and no_jid.code == "invalid_params"
    no_inst = env.rpc("task.job_stats", {})
    assert not no_inst.ok and no_inst.code == "invalid_params"


def test_j3b_job_not_found_is_not_error_code(env):
    """J3b：不存在的 job 读面返回 ok=True + payload {"error": ...}。

    实测语义（非猜测）：job_status / wait_for_job 对不存在 job 返回成功信封，
    错误放在 result.error 里，而不是 RPC 错误码。测试固化该契约，防止后续
    改动把「查不到」与「拒绝」混为一谈。
    """
    st = env.job_status("job-999999")
    assert st.ok, f"不存在 job 的读面应返回成功信封：{st!r}"
    assert "job not found" in json.dumps(st.result, ensure_ascii=False), \
        f"payload 应含 job not found：{st.result}"


def test_j4_jobs_db_init_is_idempotent(env):
    """J4：mcp.job_executor.start 幂等建表（jobs 表 + 4 索引）。"""
    r = env.job_executor_start()
    assert r.ok, f"job_executor.start 失败：{r!r}"
    assert r.result.get("schema_ready") is True, f"schema 应就绪：{r.result}"
    assert r.result.get("jobs_table") is True
    assert r.result.get("index_count") == 4, \
        f"应有 4 个索引：{r.result}"
    # 再次调用仍就绪（幂等 DDL 不破坏既有数据）
    r2 = env.job_executor_start()
    assert r2.ok and r2.result.get("schema_ready") is True
    # 既有作业未被清空
    stats = env.job_stats()
    assert (stats.result or {}).get("total", 0) >= 1, \
        f"幂等重建不得清空既有作业：{stats.result}"


def test_j5_lease_recover_requires_structured_params(env):
    """J5：lease.recover 的必填面（role + request_id + workspace_instance_id）。"""
    # 缺 role
    no_role = env.rpc("lease.recover", {"task_id": "T-none"})
    assert not no_role.ok and no_role.code == "invalid_params", \
        f"缺 role 应被拒：{no_role!r}"
    # role 非法
    bad_role = env.rpc("lease.recover",
                       {"task_id": "T-none", "role": "not_a_role"})
    assert not bad_role.ok and bad_role.code == "invalid_params", \
        f"非法 role 应被拒：{bad_role!r}"
    # 缺 request_id
    no_req = env.lease_recover("T-none")
    assert not no_req.ok and no_req.code == "invalid_params", \
        f"缺 request_id 应被拒：{no_req!r}"
    # 缺 workspace_instance_id
    no_ws = env.rpc("lease.recover", {"task_id": "T-none", "role": "executor",
                                      "request_id": "tp11j-rec-1"})
    assert not no_ws.ok and no_ws.code == "invalid_params", \
        f"缺 workspace_instance_id 应被拒：{no_ws!r}"
