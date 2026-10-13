"""TP-11S CAS/GC/WAL/迁移与崩溃恢复环境 harness：复用 TP-11 底座 + 存储面助手。

在完全隔离的 daemon 上验证崩溃恢复与 GC 面：

- S1（核心，闭环验证 TP-11J 作业落库修复）：提交作业 → DB 行 status='running'
  → mcp.health_check.recover_stale_jobs → 标记为 failed。修复前 jobs 表恒空，
  该恢复逻辑永远匹配不到行（死代码）；修复后可真正回收。
- S2 恢复面 fail-soft：check_db_registry / recover_workspace_registry /
  recover_cas_db 在无故障环境返回 healthy，不抛错。
- S3 GC 面：gc_policy_get / gc_retention / gc_archive_list 可达且结构完整。

🔴 关键契约：`recover_stale_jobs` 的 DB 路径由 `registry_db_path` 参数决定
（health_check_handlers.rs:89 registry_db_param），**可指向任意 DB**；默认值
是 registry DB（其中**没有 jobs 表**）→ 恒返回 healthy/"no jobs table"。
故要真正回收 stale jobs 必须显式传入 jobs 表所在库（task DB）路径。
"""

from __future__ import annotations

import os
import sqlite3
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

# TP-11S 独立 run 根（不复用其他域默认值，避免套件互踩）
DEFAULT_RUN_ROOT_STORE = Path(
    os.environ.get("CW_TP11S_RUN_ROOT",
                   r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-11S"))


class StorageEnv(IsolationEnv):
    """TP-11S 存储/恢复执行环境：TP-11 底座 + 存储面 RPC 助手。"""

    def __init__(self, run_root: Optional[Path] = None,
                 daemon_src: Optional[Path] = None,
                 workspace_name: str = "tp11s-ws"):
        super().__init__(run_root=run_root or DEFAULT_RUN_ROOT_STORE,
                         daemon_src=daemon_src,
                         workspace_name=workspace_name)

    # ---------- 路径助手 ----------

    def task_db_path(self) -> Path:
        """jobs 表所在库（权威 task-DB）。"""
        return self.run_root / "home" / ".callwarden" / "callwarden.db"

    def registry_db_path(self) -> Path:
        """registry DB（默认恢复目标，其中无 jobs 表）。"""
        return self.run_root / "home" / ".callwarden" / "daemon" / "registry.db"

    def _task_conn(self, readonly: bool = False) -> sqlite3.Connection:
        p = self.task_db_path()
        if readonly:
            return sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        return sqlite3.connect(str(p))

    # ---------- fixture：构造可恢复的 stale job ----------

    def force_job_status(self, job_id: str, status: str) -> int:
        """直接改写 jobs 行 status（隔离库内 fixture），返回受影响行数。

        用于构造「daemon 崩溃遗留的 running 作业」这一恢复前提。
        """
        conn = self._task_conn()
        try:
            cur = conn.execute(
                "UPDATE jobs SET status=?1 WHERE job_id=?2", (status, job_id))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def job_row(self, job_id: str) -> Optional[Dict[str, Any]]:
        """读取 jobs 行（只读）。"""
        conn = self._task_conn(readonly=True)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?1", (job_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def insert_job_row(self, job_id: str, job_type: str, status: str,
                       workspace_id: int = 1) -> int:
        """直接插入 jobs 行（隔离库内 fixture）。"""
        conn = self._task_conn()
        try:
            cur = conn.execute(
                "INSERT OR REPLACE INTO jobs "
                "(job_id, workspace_id, job_type, status, created_at) "
                "VALUES (?1, ?2, ?3, ?4, ?5)",
                (job_id, workspace_id, job_type, status, 1.0))
            conn.commit()
            return cur.lastrowid or 0
        finally:
            conn.close()

    # ---------- 恢复面（mcp.health_check.*）----------

    def recover_stale_jobs(self, db_path: str = "") -> Any:
        """mcp.health_check.recover_stale_jobs：把 running 作业置 failed。

        db_path 为空 → 走默认 registry DB（无 jobs 表 → healthy/无 stale）。
        """
        params: Dict[str, Any] = {}
        if db_path:
            params["registry_db_path"] = db_path
        return self.rpc("mcp.health_check.recover_stale_jobs", params)

    def check_db_registry(self, db_path: str = "") -> Any:
        params: Dict[str, Any] = {}
        if db_path:
            params["registry_db_path"] = db_path
        return self.rpc("mcp.health_check.check_db_registry", params)

    def recover_workspace_registry(self, db_path: str = "") -> Any:
        params: Dict[str, Any] = {}
        if db_path:
            params["registry_db_path"] = db_path
        return self.rpc("mcp.health_check.recover_workspace_registry", params)

    def recover_cas_db(self, db_path: str = "") -> Any:
        params: Dict[str, Any] = {}
        if db_path:
            params["registry_db_path"] = db_path
        return self.rpc("mcp.health_check.recover_cas_db", params)

    # ---------- GC 面 ----------

    def gc_call(self, method: str, **extra: Any) -> Any:
        params: Dict[str, Any] = {
            "workspace_instance_id": self.setup_workspace()[1]}
        params.update(extra)
        return self.rpc(method, params)

    def gc_policy_get(self) -> Any:
        return self.gc_call("admin.gc_policy_get")

    def gc_retention(self) -> Any:
        return self.gc_call("admin.gc_retention")

    def gc_archive_list(self) -> Any:
        return self.gc_call("admin.gc_archive_list")
