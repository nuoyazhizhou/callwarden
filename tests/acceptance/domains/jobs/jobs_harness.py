"""TP-11J 异步作业环境 harness：复用 TP-11 底座 + 作业面助手。

在完全隔离的 daemon 上验证异步作业面（提交/取消/等待/读面隔离/越界参数）：

- J1 生命周期：task.job_submit → job_status → wait_for_job → 终态；
- J2 取消语义：task.job_cancel（协作式 cancel_flag；终态幂等；不存在拒绝）；
- J3 读面隔离与 fail-closed：跨 workspace job 不可见（限定 workspace_id）；
  limit / timeout / poll_interval 负数 → invalid_params；
- J4 统计面：task.job_stats。

🔴 关键架构事实（实测前先记录，避免猜测）：
本域存在**两套并存**的 job 子系统——
1. 内存 JobRunner（job_runner.rs）：状态 queued/running/completed/failed/
   cancelled，取消为协作式（AtomicBool cancel_flag，executor 定期检查）；
2. DB jobs 表（job_executor_handlers.rs JOBS_SCHEMA_DDL）：
   status TEXT DEFAULT 'pending'、cancel_requested INTEGER、progress REAL，
   由 snapshot_state 的 task.job_status / list_jobs / wait_for_job 读取。
两者状态词不同（queued vs pending），是验收时必须如实记录的观察点。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_HERE = Path(__file__).resolve().parent
_GOV_DIR = _HERE.parent / "governance"
_ISO_DIR = _HERE.parent / "isolation"
for _d in (str(_HERE), str(_GOV_DIR), str(_ISO_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

# 复用 TP-11I 底座（GovernanceEnv + run 根自清洁 wipe_run_root）
from isolation_harness import IsolationEnv  # noqa: E402

# TP-11J 独立 run 根（不复用其他域默认值，避免套件互踩）
DEFAULT_RUN_ROOT_JOB = Path(
    os.environ.get("CW_TP11J_RUN_ROOT",
                   r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-11J"))

# job_runner.rs SUPPORTED_JOB_TYPES（14 种；submit 只接受这些）
SUPPORTED_JOB_TYPES: tuple = (
    "semgrep_scan", "semgrep_incremental", "clone_detect", "embed",
    "embed_single", "git_history", "git_blame", "codeowners",
    "project_deps", "envelope_deps", "coverage", "hard_dep_edges",
    "cross_repo_deps", "prune_external",
)


class JobsEnv(IsolationEnv):
    """TP-11J 异步作业执行环境：TP-11 底座 + 作业面 RPC 助手。"""

    def __init__(self, run_root: Optional[Path] = None,
                 daemon_src: Optional[Path] = None,
                 workspace_name: str = "tp11j-ws"):
        super().__init__(run_root=run_root or DEFAULT_RUN_ROOT_JOB,
                         daemon_src=daemon_src,
                         workspace_name=workspace_name)

    # ---------- jobs DB 初始化（读面前置） ----------

    def job_executor_start(self, db_path: str = "") -> Any:
        """mcp.job_executor.start：建 jobs 表 + 4 索引（幂等 DDL，fail-soft）。

        🔴 全新库里 jobs 表**不存在**：DB 读面（job_status / list_jobs /
        wait_for_job / job_stats）在读之前若未初始化会报
        `internal_error: no such table: jobs`。故读面前必须先跑本入口。
        """
        db = db_path or str(
            self.run_root / "home" / ".callwarden" / "callwarden.db")
        return self.rpc("mcp.job_executor.start", {"db_path": db})

    # ---------- 作业写面 ----------

    def job_submit(self, job_type: str, sync: bool = False,
                   **extra: Any) -> Any:
        """task.job_submit：job_type 必须命中 SUPPORTED_JOB_TYPES。

        🔴 workspace_instance_id 是必填（实测「缺少字段: workspace_instance_id」）。
        """
        params: Dict[str, Any] = {
            "job_type": job_type,
            "workspace_instance_id": self.setup_workspace()[1],
        }
        if sync:
            params["sync"] = True
        params.update(extra)
        return self.rpc("task.job_submit", params)

    def job_cancel(self, job_id: str) -> Any:
        """task.job_cancel：协作式取消（终态幂等，不存在 → job_not_found）。"""
        return self.rpc("task.job_cancel", {"job_id": job_id})

    # ---------- 作业读面（DB 表，均限定 workspace_instance_id） ----------

    def job_status(self, job_id: str,
                   workspace_instance_id: str = "") -> Any:
        inst = workspace_instance_id or self.setup_workspace()[1]
        return self.rpc("task.job_status",
                        {"workspace_instance_id": inst, "job_id": job_id})

    def list_jobs(self, job_type: str = "", status: str = "",
                  limit: Optional[int] = None,
                  workspace_instance_id: str = "") -> Any:
        inst = workspace_instance_id or self.setup_workspace()[1]
        params: Dict[str, Any] = {"workspace_instance_id": inst}
        if job_type:
            params["job_type"] = job_type
        if status:
            params["status"] = status
        if limit is not None:
            params["limit"] = limit
        return self.rpc("task.list_jobs", params)

    def wait_for_job(self, job_id: str, timeout: Optional[float] = None,
                     poll_interval: Optional[float] = None,
                     workspace_instance_id: str = "") -> Any:
        inst = workspace_instance_id or self.setup_workspace()[1]
        params: Dict[str, Any] = {
            "workspace_instance_id": inst, "job_id": job_id}
        if timeout is not None:
            params["timeout"] = timeout
        if poll_interval is not None:
            params["poll_interval"] = poll_interval
        return self.rpc("task.wait_for_job", params)

    def job_stats(self, workspace_instance_id: str = "") -> Any:
        inst = workspace_instance_id or self.setup_workspace()[1]
        return self.rpc("task.job_stats", {"workspace_instance_id": inst})

    # ---------- 租约恢复（异步/重试相关面） ----------

    def lease_recover(self, task_id: str, role: str = "executor",
                      **extra: Any) -> Any:
        """lease.recover：孤儿租约回收面。

        🔴 role 必填且必须是 executor/reviewer/adjudicator（合同角色名）。
        """
        params: Dict[str, Any] = {"task_id": task_id, "role": role}
        params.update(extra)
        return self.rpc("lease.recover", params)
