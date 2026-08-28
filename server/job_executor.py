"""
Phase 7.0: 后台任务执行器

设计参考：enterprise-daemon-shared-snapshot-plan.md §Phase 7

把 clone/vector/semgrep 等重任务从 MCP 在线请求中剥离，放到后台线程池执行。
特点：
- 单独线程池（与 MCP server 的请求线程隔离）
- 资源预算：max_concurrent_jobs / max_duration_seconds
- 取消支持：cancel_requested 标志，handler 轮询 is_cancelled(job_id)
- 进度上报：handler 调用 update_job_progress(job_id, progress, message)

使用方式：
    executor = JobExecutor(db_path, workspace_root)
    executor.register_handler("clone_detect", clone_detect_handler)
    executor.start()
    job = executor.submit("clone_detect", {"file_filter": "src/"})
    # ... 异步轮询 get_job(job.job_id) ...

注意：每个 JobExecutor 实例持有自己的 SQLite 连接（独立于 MCP server），
避免与 MCP server 的长连接撞锁。executor 的连接在 handler 执行期间独占。

权威归属（SRV-011，T-1787323461285-e8a7a12c）：
- `JobExecutor.start` 的 jobs DB 权威初始化形态（连接 + 批次10 PRAGMA 集
  + JOBS_SCHEMA_DDL）已下沉 Rust daemon `mcp.job_executor.start`
  （rust_ext/src/daemon/job_executor_handlers.rs::handle_start）。
- 生产链重任务执行权威由 Rust `job_runner.rs`（task_rpc：task.job_submit +
  task.wait_for_job）承担，不依赖本模块。
- 本文件保留的 sqlite3.connect 仅供存量 phase7 测试与进程内 executor
  生命周期使用，不再承担生产权威。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Any, Callable, Dict, Optional

from ..db.db_jobs import (
    init_jobs_schema,
    submit_job,
    get_job,
    mark_job_running,
    update_job_progress,
    complete_job,
    fail_job,
    is_cancelled,
    cancel_job,
    JOB_PENDING,
    JOB_RUNNING,
    JOB_COMPLETED,
    JOB_CANCELLED,
    JOB_FAILED,
    Job,
)


# Handler 签名：(ctx) -> result_summary dict
# ctx 是 JobContext，提供 conn / workspace_id / job / params / update_progress / check_cancelled
JobHandler = Callable[["JobContext"], Dict[str, Any]]


class JobContext:
    """Handler 执行上下文

    提供 handler 内部使用的辅助方法：
    - update_progress(progress, message)：更新进度
    - is_cancelled()：检查是否被取消
    - job：原始 Job 对象

    所有数据库访问都通过 conn_lock 串行化，避免多线程 SQLite 冲突。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        job: Job,
        conn_lock: threading.RLock,
    ):
        self._conn = conn
        self._job = job
        self._conn_lock = conn_lock
        self._cancelled = False

    @property
    def job(self) -> Job:
        return self._job

    @property
    def job_id(self) -> str:
        return self._job.job_id

    @property
    def workspace_id(self) -> int:
        return self._job.workspace_id

    @property
    def params(self) -> Dict[str, Any]:
        return self._job.params

    @property
    def conn(self) -> sqlite3.Connection:
        """数据库连接（handler 内访问数据库用）

        注意：所有 conn 操作应通过 ctx 提供的方法（update_progress / check_cancelled）
        或自行加锁（ctx._conn_lock）。直接使用 conn 时需注意线程安全。
        """
        return self._conn

    @property
    def conn_lock(self) -> threading.RLock:
        """数据库连接锁（handler 内自行操作 conn 时使用）"""
        return self._conn_lock

    def update_progress(self, progress: float, message: str = "") -> None:
        """更新任务进度（handler 内调用）"""
        if self._cancelled:
            return
        try:
            with self._conn_lock:
                update_job_progress(
                    self._conn, self._job.job_id, progress, message)
        except Exception:
            # 进度更新失败不应中断任务
            pass

    def check_cancelled(self) -> bool:
        """检查任务是否被取消（handler 内调用）

        handler 应在长循环中定期调用此方法，及时退出。
        """
        if self._cancelled:
            return True
        try:
            with self._conn_lock:
                self._cancelled = is_cancelled(self._conn, self._job.job_id)
        except Exception:
            self._cancelled = False
        return self._cancelled


class JobExecutor:
    """后台任务执行器

    使用独立线程池运行重任务，与 MCP server 请求线程隔离。

    资源预算：
    - max_concurrent_jobs：最大并发任务数（默认 2）
    - max_duration_seconds：单任务最大执行时长（默认 1800 秒）

    权威归属（SRV-011）：jobs DB 权威初始化形态已下沉 Rust daemon
    `mcp.job_executor.start`（job_executor_handlers.rs::handle_start）；
    生产重任务执行权威为 Rust job_runner.rs。本类保留进程内线程池调度
    与存量测试兼容接口。
    """

    def __init__(
        self,
        db_path: str,
        max_concurrent_jobs: int = 2,
        max_duration_seconds: int = 1800,
    ):
        """初始化 JobExecutor

        参数：
            db_path: SQLite 数据库路径
            max_concurrent_jobs: 最大并发任务数
            max_duration_seconds: 单任务最大执行时长（秒）
        """
        self._db_path = db_path
        self._max_concurrent = max(1, max_concurrent_jobs)
        self._max_duration = max(1, max_duration_seconds)

        self._handlers: Dict[str, JobHandler] = {}
        self._executor: Optional[ThreadPoolExecutor] = None
        self._futures: Dict[str, Future] = {}
        self._lock = threading.Lock()
        # _conn_lock 保护所有 self._conn 的访问（多线程共享连接）
        self._conn_lock = threading.RLock()
        self._started = False
        self._conn: Optional[sqlite3.Connection] = None

    def register_handler(self, job_type: str, handler: JobHandler) -> None:
        """注册 job handler

        参数：
            job_type: 任务类型（如 "clone_detect"）
            handler: 处理函数，签名为 (ctx: JobContext) -> dict
        """
        self._handlers[job_type] = handler

    def start(self) -> None:
        """启动 executor

        创建线程池和共享 SQLite 连接。
        可重复调用（已启动时无操作）。

        权威归属（SRV-011）：本函数内 sqlite3.connect + 批次10 PRAGMA 集
        + init_jobs_schema 的 jobs DB 权威初始化形态已下沉 Rust daemon
        RPC `mcp.job_executor.start`（rust_ext/src/daemon/
        job_executor_handlers.rs::handle_start，JOBS_SCHEMA_DDL 逐字对齐
        db/db_jobs.py）。函数体因存量 phase7 测试（test_start_inits_schema /
        test_start_idempotent 等）锁定保留，仅承载进程内 executor 生命周期。
        """
        if self._started:
            return
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_concurrent,
            thread_name_prefix="job-executor",
        )
        self._conn = sqlite3.connect(
            self._db_path,
            timeout=30.0,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        # WAL 模式：与 MCP server 并发读安全
        # 批次10（P2 性能优化）：补全 synchronous=NORMAL / cache_size / mmap_size
        # / temp_store=MEMORY / wal_autocheckpoint，与 daemon 子连接配置对齐
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA wal_autocheckpoint=1000")
            self._conn.execute("PRAGMA cache_size=-262144")  # 256MB
            self._conn.execute("PRAGMA mmap_size=268435456")  # 256MB
            self._conn.execute("PRAGMA temp_store=MEMORY")
        except Exception:
            pass
        # 确保 jobs schema 存在
        init_jobs_schema(self._conn)
        self._started = True

    def stop(self, wait: bool = True) -> None:
        """停止 executor

        参数：
            wait: 是否等待运行中的任务完成（True=阻塞等待，False=立即返回）
        """
        if not self._started:
            return
        if self._executor is not None:
            if wait:
                self._executor.shutdown(wait=True)
            else:
                self._executor.shutdown(wait=False)
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        self._started = False

    def submit(
        self,
        job_type: str,
        params: Optional[Dict[str, Any]] = None,
        workspace_id: Optional[int] = None,
    ) -> Job:
        """提交任务

        参数：
            job_type: 任务类型（必须已注册 handler）
            params: 任务参数
            workspace_id: workspace ID（None 时从 db 查询 active workspace）

        返回：新创建的 Job 对象（status=pending）

        如果 job_type 未注册 handler，仍创建 Job 但立即标记为 failed。
        """
        if not self._started:
            raise RuntimeError("JobExecutor not started")

        # 解析 workspace_id
        if workspace_id is None:
            workspace_id = self._lookup_active_workspace()
        if workspace_id is None:
            raise RuntimeError("No active workspace")

        # 创建 job 记录（加锁，避免与 worker 线程的 conn 访问冲突）
        with self._conn_lock:
            job = submit_job(self._conn, workspace_id, job_type, params)

        # 检查 handler 是否注册
        handler = self._handlers.get(job_type)
        if handler is None:
            with self._conn_lock:
                fail_job(self._conn, job.job_id,
                         f"No handler registered for job_type: {job_type}")
            return job

        # 提交到线程池
        future = self._executor.submit(self._run_job, job.job_id, handler)
        with self._lock:
            self._futures[job.job_id] = future
        return job

    def cancel(self, job_id: str) -> bool:
        """请求取消任务

        行为：
        - pending：直接终态
        - running：设置 cancel_requested，handler 轮询后自行退出

        返回：True 表示成功发起取消请求
        """
        if not self._started:
            return False
        with self._conn_lock:
            return cancel_job(self._conn, job_id)

    def get_status(self, job_id: str) -> Optional[Job]:
        """查询任务状态"""
        if not self._started:
            return None
        with self._conn_lock:
            return get_job(self._conn, job_id)

    def list_pending(self) -> list:
        """列出 pending 状态的任务（供调试）"""
        if not self._started:
            return []
        with self._conn_lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC",
                (JOB_PENDING,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ========================================
    # 内部实现
    # ========================================

    def _lookup_active_workspace(self) -> Optional[int]:
        """查询当前 active workspace ID"""
        try:
            with self._conn_lock:
                row = self._conn.execute(
                    "SELECT id FROM workspaces WHERE is_active = 1 LIMIT 1"
                ).fetchone()
                return row["id"] if row else None
        except Exception:
            return None

    def _run_job(self, job_id: str, handler: JobHandler) -> None:
        """线程池 worker：执行单个 job

        流程：
        1. mark_job_running（pending → running）
        2. 检查取消
        3. 调用 handler
        4. complete_job 或 fail_job
        5. 超时保护
        """
        # 1. 标记 running
        with self._conn_lock:
            if not mark_job_running(self._conn, job_id):
                # 已经不是 pending 了（可能被取消或被其他 worker 拾取）
                return

        with self._conn_lock:
            job = get_job(self._conn, job_id)
        if job is None:
            return

        # 2. 再次检查取消（避免在 mark_running 之前被取消）
        with self._conn_lock:
            cancelled_before_start = is_cancelled(self._conn, job_id)
        if cancelled_before_start:
            self._mark_cancelled(job_id)
            return

        # 3. 准备 context
        ctx = JobContext(self._conn, job, self._conn_lock)

        # 4. 在独立线程中跑 handler，主线程做超时监控
        result_holder: Dict[str, Any] = {}
        handler_thread = threading.Thread(
            target=self._run_handler_inner,
            args=(handler, ctx, result_holder),
            name=f"job-{job_id}",
            daemon=True,
        )
        start_time = time.time()
        handler_thread.start()

        # 5. 等待 handler 完成（带超时）
        deadline = start_time + self._max_duration
        while True:
            handler_thread.join(timeout=1.0)
            if not handler_thread.is_alive():
                break
            # 检查超时
            if time.time() > deadline:
                # 超时：标记 failed
                # 注意：Python 无法强制 kill 线程，handler 仍会继续运行
                # 但 job 状态已变 failed，外部不可见
                with self._conn_lock:
                    fail_job(
                        self._conn,
                        job_id,
                        f"Job exceeded max_duration_seconds={self._max_duration}",
                    )
                return
            # 检查取消（让 handler 有机会退出）
            with self._conn_lock:
                cancel_flag = is_cancelled(self._conn, job_id)
            if cancel_flag:
                # 等待 handler 自行退出（最多 5 秒）
                handler_thread.join(timeout=5.0)
                self._mark_cancelled(job_id)
                return

        # 6. handler 完成，检查结果
        if result_holder.get("error"):
            with self._conn_lock:
                fail_job(self._conn, job_id, str(result_holder["error"]))
            return

        if result_holder.get("cancelled"):
            self._mark_cancelled(job_id)
            return

        # 6.1 再次检查是否被取消（handler 可能正常返回但期间被取消）
        with self._conn_lock:
            was_cancelled = is_cancelled(self._conn, job_id)
        if was_cancelled:
            self._mark_cancelled(job_id)
            return

        summary = result_holder.get("result", {})
        with self._conn_lock:
            complete_job(self._conn, job_id, summary)

    def _run_handler_inner(
        self,
        handler: JobHandler,
        ctx: JobContext,
        result_holder: Dict[str, Any],
    ) -> None:
        """实际执行 handler（在 handler_thread 内）"""
        try:
            result = handler(ctx)
            result_holder["result"] = result or {}
        except JobCancelled as e:
            result_holder["cancelled"] = True
            result_holder["message"] = str(e)
        except Exception as e:
            result_holder["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"

    def _mark_cancelled(self, job_id: str) -> None:
        """标记任务为 cancelled（已处于 running 状态）"""
        now = time.time()
        with self._conn_lock:
            self._conn.execute(
                """UPDATE jobs
                   SET status = ?, cancel_requested = 1, finished_at = ?,
                       message = 'cancelled during execution'
                   WHERE job_id = ? AND status = ?""",
                (JOB_CANCELLED, now, job_id, JOB_RUNNING),
            )
            self._conn.commit()

        # 清理 future
        with self._lock:
            self._futures.pop(job_id, None)


class JobCancelled(Exception):
    """Handler 主动抛出，表示任务被取消"""
    pass
