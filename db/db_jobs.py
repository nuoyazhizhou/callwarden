"""
Phase 7.0: 后台任务系统

设计参考：enterprise-daemon-shared-snapshot-plan.md §Phase 7

把 clone/vector/semgrep 等重任务从 MCP 在线请求中剥离，改为后台 job 异步执行。
job 系统提供：
- 唯一 job_id（人类可读）
- 状态机：pending → running → completed / cancelled / failed
- 进度上报（progress 0.0~1.0 + message）
- 取消支持（cancel_job 设置 cancelled 标志，executor 轮询检查）
- 资源预算（params.resource_budget 携带 max_duration_seconds 等）

注意：本模块只负责 job 元数据存储与状态流转，不负责实际执行。
实际执行由 server/job_executor.py 中的 JobExecutor 完成。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ============================================
# Schema
# ============================================

JOBS_SCHEMA_DDL = """
-- 后台任务表：记录 clone/vector/semgrep 等重任务的执行状态
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,              -- 人类可读 ID（如 "J-1783698970719-3a4b5c6d"）
    workspace_id INTEGER NOT NULL,
    job_type TEXT NOT NULL,                    -- 'clone_detect' / 'vector_index' / 'semgrep_scan'
    status TEXT NOT NULL DEFAULT 'pending',    -- pending / running / completed / cancelled / failed
    progress REAL DEFAULT 0.0,                  -- 0.0 ~ 1.0
    message TEXT DEFAULT '',                    -- 进度描述
    params TEXT DEFAULT '{}',                   -- JSON 输入参数
    result_summary TEXT DEFAULT '{}',           -- JSON 结果摘要
    error TEXT DEFAULT '',                      -- 失败时的错误信息
    cancel_requested INTEGER DEFAULT 0,         -- 0/1，是否被请求取消
    created_at REAL NOT NULL,
    started_at REAL DEFAULT 0,
    finished_at REAL DEFAULT 0,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_jobs_workspace ON jobs(workspace_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_type ON jobs(workspace_id, job_type);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""


# 状态常量
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_CANCELLED = "cancelled"
JOB_FAILED = "failed"

# 终态集合
_TERMINAL_STATES = {JOB_COMPLETED, JOB_CANCELLED, JOB_FAILED}


# ============================================
# 数据结构
# ============================================

@dataclass
class Job:
    """后台任务信息"""
    id: int = 0
    job_id: str = ""
    workspace_id: int = 0
    job_type: str = ""
    status: str = JOB_PENDING
    progress: float = 0.0
    message: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    result_summary: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    cancel_requested: bool = False
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 dict（包含解析后的 params/result_summary）"""
        return asdict(self)

    def summary(self) -> str:
        """简要描述"""
        return (
            f"Job(job_id={self.job_id}, type={self.job_type}, "
            f"status={self.status}, progress={self.progress:.0%})"
        )

    @property
    def is_terminal(self) -> bool:
        """是否处于终态（completed/cancelled/failed）"""
        return self.status in _TERMINAL_STATES


# ============================================
# Schema 初始化
# ============================================

def init_jobs_schema(conn: sqlite3.Connection):
    """初始化 jobs schema。"""
    conn.executescript(JOBS_SCHEMA_DDL)
    conn.commit()


# ============================================
# Job ID 生成
# ============================================

def _generate_job_id() -> str:
    """生成人类可读的 job_id

    格式：J-<13位时间戳>-<8位随机十六进制>
    例如：J-1783698970719-3a4b5c6d

    后缀长度选 8 位 hex（32 bit，~42 亿种）而非 4 位 hex：
    4 位 hex 在毫秒内连续生成 100 个 ID 时按生日悖论有 ~7.3% 碰撞概率；
    8 位 hex 将此概率降到 ~10⁻⁶，足以支撑测试断言 100% 唯一。
    """
    ts = int(time.time() * 1000)
    rand = secrets.token_hex(4)  # 8 个十六进制字符
    return f"J-{ts}-{rand}"


# ============================================
# 行 → 对象
# ============================================

def _row_to_job(row: sqlite3.Row) -> Job:
    """把数据库行转换为 Job 对象"""
    params_str = row["params"] or "{}"
    result_str = row["result_summary"] or "{}"
    try:
        params = json.loads(params_str) if params_str else {}
    except json.JSONDecodeError:
        params = {}
    try:
        result = json.loads(result_str) if result_str else {}
    except json.JSONDecodeError:
        result = {}
    return Job(
        id=row["id"],
        job_id=row["job_id"],
        workspace_id=row["workspace_id"],
        job_type=row["job_type"],
        status=row["status"],
        progress=row["progress"],
        message=row["message"] or "",
        params=params,
        result_summary=result,
        error=row["error"] or "",
        cancel_requested=bool(row["cancel_requested"]),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


# ============================================
# CRUD
# ============================================

def submit_job(
    conn: sqlite3.Connection,
    workspace_id: int,
    job_type: str,
    params: Optional[Dict[str, Any]] = None,
) -> Job:
    """提交一个后台任务

    参数：
        conn: SQLite 连接
        workspace_id: workspace ID
        job_type: 任务类型（如 'clone_detect'）
        params: 任务参数（JSON 可序列化）

    返回：新创建的 Job 对象（status=pending）
    """
    now = time.time()
    job_id = _generate_job_id()
    params_str = json.dumps(params or {}, ensure_ascii=False, separators=(",", ":"))

    cursor = conn.execute(
        """INSERT INTO jobs
           (job_id, workspace_id, job_type, status, progress, message,
            params, result_summary, error, cancel_requested,
            created_at, started_at, finished_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (job_id, workspace_id, job_type, JOB_PENDING, 0.0, "",
         params_str, "{}", "", 0, now, 0, 0),
    )
    conn.commit()
    job_row = conn.execute(
        "SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return _row_to_job(job_row)


def get_job(conn: sqlite3.Connection, job_id: str) -> Optional[Job]:
    """获取任务详情

    参数：
        job_id: 任务 ID（如 "J-1783698970719-3a4b5c6d"）

    返回：Job 对象；如果不存在返回 None
    """
    row = conn.execute(
        "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(
    conn: sqlite3.Connection,
    workspace_id: int,
    job_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Job]:
    """列出任务

    参数：
        workspace_id: workspace ID
        job_type: 过滤任务类型（None = 全部）
        status: 过滤状态（None = 全部）
        limit: 返回上限

    返回：按 created_at 降序的 Job 列表
    """
    sql = "SELECT * FROM jobs WHERE workspace_id = ?"
    params_list: List[Any] = [workspace_id]
    if job_type:
        sql += " AND job_type = ?"
        params_list.append(job_type)
    if status:
        sql += " AND status = ?"
        params_list.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params_list.append(limit)
    rows = conn.execute(sql, params_list).fetchall()
    return [_row_to_job(r) for r in rows]


def mark_job_running(conn: sqlite3.Connection, job_id: str) -> bool:
    """把任务标记为 running

    只能从 pending 状态转入 running。

    返回：True 表示成功转换；False 表示状态不匹配或任务不存在
    """
    now = time.time()
    cur = conn.execute(
        """UPDATE jobs
           SET status = ?, started_at = ?, progress = 0.0,
               message = 'running'
           WHERE job_id = ? AND status = ?""",
        (JOB_RUNNING, now, job_id, JOB_PENDING),
    )
    conn.commit()
    return cur.rowcount > 0


def update_job_progress(
    conn: sqlite3.Connection,
    job_id: str,
    progress: float,
    message: str = "",
) -> bool:
    """更新任务进度

    参数：
        progress: 0.0 ~ 1.0
        message: 进度描述

    返回：True 表示更新成功
    """
    progress = max(0.0, min(1.0, float(progress)))
    cur = conn.execute(
        """UPDATE jobs
           SET progress = ?, message = ?
           WHERE job_id = ? AND status = ?""",
        (progress, message, job_id, JOB_RUNNING),
    )
    conn.commit()
    return cur.rowcount > 0


def complete_job(
    conn: sqlite3.Connection,
    job_id: str,
    result_summary: Optional[Dict[str, Any]] = None,
) -> bool:
    """标记任务为 completed

    只能从 running 状态转入 completed。

    返回：True 表示成功转换
    """
    now = time.time()
    result_str = json.dumps(result_summary or {}, ensure_ascii=False, separators=(",", ":"))
    cur = conn.execute(
        """UPDATE jobs
           SET status = ?, progress = 1.0, result_summary = ?,
               finished_at = ?, message = 'completed'
           WHERE job_id = ? AND status = ?""",
        (JOB_COMPLETED, result_str, now, job_id, JOB_RUNNING),
    )
    conn.commit()
    return cur.rowcount > 0


def fail_job(
    conn: sqlite3.Connection,
    job_id: str,
    error: str,
) -> bool:
    """标记任务为 failed

    只能从 running/pending 状态转入 failed。

    返回：True 表示成功转换
    """
    now = time.time()
    cur = conn.execute(
        """UPDATE jobs
           SET status = ?, error = ?, finished_at = ?,
               message = 'failed'
           WHERE job_id = ? AND status IN (?, ?)""",
        (JOB_FAILED, error, now, job_id, JOB_RUNNING, JOB_PENDING),
    )
    conn.commit()
    return cur.rowcount > 0


def cancel_job(conn: sqlite3.Connection, job_id: str) -> bool:
    """请求取消任务

    行为：
    - pending 状态：直接标记为 cancelled（executor 不会拾取 cancelled 的任务）
    - running 状态：设置 cancel_requested=1，executor 轮询检查后自行退出
    - 终态：无操作，返回 False

    返回：True 表示成功发起取消请求
    """
    job = get_job(conn, job_id)
    if not job:
        return False
    if job.status in _TERMINAL_STATES:
        return False

    now = time.time()
    if job.status == JOB_PENDING:
        # pending 直接终态
        conn.execute(
            """UPDATE jobs
               SET status = ?, cancel_requested = 1, finished_at = ?,
                   message = 'cancelled before start'
               WHERE job_id = ?""",
            (JOB_CANCELLED, now, job_id),
        )
        conn.commit()
        return True

    # running：设置 cancel_requested，executor 轮询后退出
    conn.execute(
        """UPDATE jobs
           SET cancel_requested = 1, message = 'cancel requested'
           WHERE job_id = ?""",
        (job_id,),
    )
    conn.commit()
    return True


def is_cancelled(conn: sqlite3.Connection, job_id: str) -> bool:
    """检查任务是否被请求取消

    供 executor 在执行过程中轮询调用，以便及时退出。

    返回：True 表示应该停止执行
    """
    row = conn.execute(
        "SELECT cancel_requested, status FROM jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if not row:
        return True  # 任务不存在，视为应该退出
    if row["status"] == JOB_CANCELLED:
        return True
    return bool(row["cancel_requested"])


def delete_job(conn: sqlite3.Connection, job_id: str) -> bool:
    """删除任务记录

    只能删除终态任务。

    返回：True 表示删除成功
    """
    cur = conn.execute(
        "DELETE FROM jobs WHERE job_id = ? AND status IN (?, ?, ?)",
        (job_id, JOB_COMPLETED, JOB_CANCELLED, JOB_FAILED),
    )
    conn.commit()
    return cur.rowcount > 0


def clear_jobs(
    conn: sqlite3.Connection,
    workspace_id: int,
    status_list: Optional[List[str]] = None,
) -> int:
    """清理任务记录

    参数：
        workspace_id: workspace ID
        status_list: 只清理指定状态的任务；None 表示清理所有终态任务

    返回：被删除的记录数
    """
    if status_list is None:
        status_list = list(_TERMINAL_STATES)
    placeholders = ",".join("?" * len(status_list))
    cur = conn.execute(
        f"DELETE FROM jobs WHERE workspace_id = ? AND status IN ({placeholders})",
        [workspace_id] + status_list,
    )
    conn.commit()
    return cur.rowcount


def get_job_stats(
    conn: sqlite3.Connection,
    workspace_id: int,
) -> Dict[str, int]:
    """获取任务统计信息

    返回：按状态分组的任务计数
    """
    rows = conn.execute(
        """SELECT status, COUNT(*) as cnt
           FROM jobs WHERE workspace_id = ?
           GROUP BY status""",
        (workspace_id,),
    ).fetchall()
    stats = {s: 0 for s in [JOB_PENDING, JOB_RUNNING, JOB_COMPLETED, JOB_CANCELLED, JOB_FAILED]}
    for r in rows:
        stats[r["status"]] = r["cnt"]
    stats["total"] = sum(stats.values())
    return stats


# ============================================
# JobMixin（集成到 CodeGraphDB）
# ============================================

class JobMixin:
    """后台任务 Mixin

    通过 self.conn 访问数据库连接，提供 job 元数据管理。
    实际执行由 server/job_executor.py 完成。
    """

    def submit_job(
        self,
        job_type: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Job:
        """提交一个后台任务"""
        ws_id = self._get_active_workspace_id()
        return submit_job(self.conn, ws_id, job_type, params)

    def get_job(self, job_id: str) -> Optional[Job]:
        """获取任务详情"""
        return get_job(self.conn, job_id)

    def list_jobs(
        self,
        job_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Job]:
        """列出任务"""
        ws_id = self._get_active_workspace_id()
        return list_jobs(self.conn, ws_id, job_type, status, limit)

    def cancel_job(self, job_id: str) -> bool:
        """请求取消任务"""
        return cancel_job(self.conn, job_id)

    def delete_job(self, job_id: str) -> bool:
        """删除任务记录"""
        return delete_job(self.conn, job_id)

    def clear_jobs(self, status_list: Optional[List[str]] = None) -> int:
        """清理任务记录"""
        ws_id = self._get_active_workspace_id()
        return clear_jobs(self.conn, ws_id, status_list)

    def get_job_stats(self) -> Dict[str, int]:
        """获取任务统计信息"""
        ws_id = self._get_active_workspace_id()
        return get_job_stats(self.conn, ws_id)
