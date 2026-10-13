"""TP-11S CAS/GC/WAL/迁移与崩溃恢复验收：stale job 回收闭环 + 恢复面 + GC 面。

在完全隔离的 daemon 上验证崩溃恢复链路，每步断言 daemon 权威响应与 DB 行。

🔴 S1 是本套件核心：它**闭环验证 TP-11J 的作业落库修复**。修复前 jobs 表恒空，
`mcp.health_check.recover_stale_jobs` 永远匹配不到 status='running' 的行
（生产恢复逻辑沦为死代码）；修复后 submit → 落库 → 崩溃遗留 → 回收 全链路可跑通。

🔴 路径契约（实测，禁再猜）：recover_stale_jobs 的 DB 由 `registry_db_path`
参数决定（health_check_handlers.rs:89 可指向任意 DB）；**默认值是 registry DB，
其中没有 jobs 表** → 恒返回 healthy/"no jobs table, no stale jobs"。
要真正回收必须显式传入 jobs 表所在库（task DB）路径。测试固化该行为。
"""

from __future__ import annotations

import pytest

from storage_harness import StorageEnv


@pytest.fixture(scope="module")
def env():
    e = StorageEnv()
    # run 根自清洁（防上一轮残留 daemon/registry 污染本轮断言）
    StorageEnv.wipe_run_root(e.run_root)
    e = StorageEnv()
    e.start_daemon()
    try:
        e.build_graph()
        # jobs 表前置（读面/恢复面依赖）
        e.rpc("mcp.job_executor.start",
              {"db_path": str(e.run_root / "home" / ".callwarden"
                              / "callwarden.db")})
        yield e
    finally:
        e.stop_daemon()


def _submit(env, job_type: str = "codeowners") -> str:
    r = env.rpc("task.job_submit", {
        "workspace_instance_id": env.setup_workspace()[1],
        "job_type": job_type})
    assert r.ok, f"job_submit 失败：{r!r}"
    return r.result.get("job_id")


def test_s1_stale_job_recovery_closes_the_loop(env):
    """S1（核心）：崩溃遗留的 running 作业必须能被回收为 failed。

    链路：submit 落库 → fixture 置 running（模拟 daemon 崩溃）→
    recover_stale_jobs(task db) → 行变 failed 且 error 标明重启中断。
    这是 TP-11J 落库修复的端到端闭环证明。
    """
    jid = _submit(env)

    # 落库前提（TP-11J 修复点）：提交后 DB 行必须存在
    row = env.job_row(jid)
    assert row is not None, f"提交的作业必须落库（TP-11J 修复点）：{jid}"
    assert row.get("job_id") == jid

    # 模拟 daemon 崩溃：作业停在 running
    changed = env.force_job_status(jid, "running")
    assert changed == 1, f"fixture 应改写 1 行：{changed}"
    assert env.job_row(jid).get("status") == "running"

    # 回收（显式指向 jobs 表所在库）
    rec = env.recover_stale_jobs(str(env.task_db_path()))
    assert rec.ok, f"recover_stale_jobs 失败：{rec!r}"
    assert (rec.result or {}).get("details", {}).get("stale_jobs_cleaned") == 1, \
        f"应回收 1 个 stale job：{rec.result}"

    # 行状态与错误信息
    after = env.job_row(jid)
    assert after.get("status") == "failed", \
        f"回收后应为 failed：{after}"
    assert "daemon restarted" in (after.get("error") or ""), \
        f"错误信息应标明重启中断：{after}"


def test_s1b_recovery_is_idempotent(env):
    """S1b：无 running 作业时回收返回 0（幂等，不误伤终态作业）。"""
    jid = _submit(env)
    # 终态作业（codeowners 无 CODEOWNERS → failed）
    assert env.job_row(jid).get("status") in ("completed", "failed", "pending")

    rec = env.recover_stale_jobs(str(env.task_db_path()))
    assert rec.ok, f"recover_stale_jobs 失败：{rec!r}"
    assert (rec.result or {}).get("details", {}).get("stale_jobs_cleaned") == 0, \
        f"无 running 作业时应回收 0 个：{rec.result}"

    # 终态未被误改
    assert env.job_row(jid).get("status") != "running"


def test_s1c_default_path_has_no_jobs_table(env):
    """S1c：默认路径（registry DB）无 jobs 表 → healthy + 无 stale。

    固化实测行为：recover_stale_jobs 默认指向 registry DB，其中没有 jobs 表，
    故恒返回 "no jobs table, no stale jobs"。调用方要真正回收必须显式传
    jobs 表所在库路径（见 S1）。
    """
    rec = env.recover_stale_jobs()
    assert rec.ok, f"recover_stale_jobs 失败：{rec!r}"
    assert rec.result.get("status") == "healthy"
    assert "no jobs table" in (rec.result.get("message") or ""), \
        f"默认路径应提示无 jobs 表：{rec.result}"

    # DB 文件不存在 → fail-soft（不抛错）
    missing = env.recover_stale_jobs(str(env.run_root / "no" / "such.db"))
    assert missing.ok, f"缺失 DB 应 fail-soft：{missing!r}"
    assert "no registry DB" in (missing.result.get("message") or ""), \
        f"缺失 DB 应 fail-soft 提示：{missing.result}"


def test_s2_recovery_surface_fail_soft(env):
    """S2：恢复面三个检查在无故障环境返回 healthy，不抛错（fail-soft）。"""
    reg = env.check_db_registry()
    assert reg.ok, f"check_db_registry 失败：{reg!r}"
    assert reg.result.get("status") == "healthy", \
        f"registry 应健康：{reg.result}"
    assert "tables" in (reg.result.get("details") or {}), \
        f"应回表清单：{reg.result}"

    ws = env.recover_workspace_registry()
    assert ws.ok, f"recover_workspace_registry 失败：{ws!r}"
    assert ws.result.get("status") == "healthy", f"registry 恢复应健康：{ws.result}"

    cas = env.recover_cas_db()
    assert cas.ok, f"recover_cas_db 失败：{cas!r}"
    assert cas.result.get("status") == "healthy", f"CAS 恢复应健康：{cas.result}"


def test_s3_gc_surface(env):
    """S3：GC 读面可达且结构完整（策略/保留期/归档清单）。"""
    pol = env.gc_policy_get()
    assert pol.ok, f"gc_policy_get 失败：{pol!r}"
    for key in ("older_than_days", "keep_versions", "include_external",
                "backup_enabled", "vacuum_enabled"):
        assert key in pol.result, f"gc 策略缺 {key}：{pol.result}"

    ret = env.gc_retention()
    assert ret.ok, f"gc_retention 失败：{ret!r}"
    assert "retention_days" in ret.result, f"保留期缺字段：{ret.result}"

    lst = env.gc_archive_list()
    assert lst.ok, f"gc_archive_list 失败：{lst!r}"
    assert isinstance(lst.result, list), f"归档清单应为数组：{lst.result}"
