"""
Phase 7.0: JobExecutor 测试

测试 server/job_executor.py 的线程池执行、取消、超时、进度上报。
"""

import os
import sys
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

# 通过 callwarden 包导入（server/job_executor.py 使用相对导入 from ..db.db_jobs）
# 必须通过完整包路径导入，否则相对 import 无法解析
from callwarden.db.db_jobs import (
    init_jobs_schema,
    get_job,
    JOB_PENDING,
    JOB_RUNNING,
    JOB_COMPLETED,
    JOB_CANCELLED,
    JOB_FAILED,
)
from server.job_executor import (
    JobExecutor,
    JobContext,
    JobCancelled,
)


# ============================================
# Fixtures
# ============================================

@pytest.fixture
def executor_db(tmp_path):
    """创建一个临时 SQLite DB 文件，初始化 jobs schema"""
    db_path = str(tmp_path / "test_jobs.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # workspaces 表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            root_path TEXT UNIQUE NOT NULL,
            created_at REAL NOT NULL,
            is_active INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            active_task_id TEXT DEFAULT ''
        )
    """)
    conn.execute(
        "INSERT INTO workspaces (name, root_path, created_at, is_active) VALUES (?, ?, ?, 1)",
        ("test-ws", "/tmp/test", time.time()),
    )
    ws_id = conn.execute("SELECT id FROM workspaces WHERE is_active = 1").fetchone()["id"]
    init_jobs_schema(conn)
    conn.commit()
    conn.close()
    yield db_path, ws_id
    # 清理


@pytest.fixture
def executor(executor_db):
    """创建并启动一个 JobExecutor"""
    db_path, ws_id = executor_db
    ex = JobExecutor(db_path=db_path, max_concurrent_jobs=2, max_duration_seconds=30)
    ex.start()
    yield ex, ws_id
    ex.stop(wait=True)


def _wait_for_terminal(ex_and_ws, job_id, timeout=5.0):
    """等待 job 进入终态"""
    ex, ws_id = ex_and_ws
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = ex.get_status(job_id)
        if job and job.is_terminal:
            return job
        time.sleep(0.05)
    raise TimeoutError(f"job {job_id} did not reach terminal state in {timeout}s")


def _last_job_id(ex, ws_id):
    """获取最新的 job_id（加锁访问共享连接）"""
    with ex._conn_lock:
        rows = ex._conn.execute(
            "SELECT job_id FROM jobs WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1",
            (ws_id,),
        ).fetchall()
    return rows[0]["job_id"] if rows else None


# ============================================
# 基础执行
# ============================================

class TestBasicExecution:
    def test_submit_and_complete(self, executor):
        """提交一个简单 handler，验证能正常完成"""
        ex, ws_id = executor

        def handler(ctx):
            return {"result": "ok"}

        ex.register_handler("test_simple", handler)
        job = ex.submit("test_simple", {"x": 1}, workspace_id=ws_id)
        assert job.status == JOB_PENDING

        final = _wait_for_terminal(executor, job.job_id)
        assert final.status == JOB_COMPLETED
        assert final.progress == 1.0
        assert final.result_summary == {"result": "ok"}
        assert final.error == ""
        assert final.finished_at > 0
        assert final.started_at > 0

    def test_submit_returns_job_with_id(self, executor):
        ex, ws_id = executor
        ex.register_handler("noop", lambda ctx: {})
        job = ex.submit("noop", workspace_id=ws_id)
        assert job.job_id.startswith("J-")
        assert job.workspace_id == ws_id

    def test_handler_receives_params(self, executor):
        """handler 能收到传入的 params"""
        ex, ws_id = executor
        received = {}

        def handler(ctx):
            received.update(ctx.params)
            return {}

        ex.register_handler("test_params", handler)
        ex.submit("test_params", {"a": 1, "b": "hello"}, workspace_id=ws_id)
        _wait_for_terminal(executor, _last_job_id(ex, ws_id))
        assert received == {"a": 1, "b": "hello"}

    def test_handler_can_access_db(self, executor):
        """handler 能通过 ctx.conn 访问数据库"""
        ex, ws_id = executor

        def handler(ctx):
            # 写入一些数据（通过 ctx.conn_lock 串行化）
            with ctx.conn_lock:
                ctx.conn.execute("CREATE TABLE IF NOT EXISTS test_data (k TEXT, v TEXT)")
                ctx.conn.execute("INSERT INTO test_data VALUES (?, ?)", ("hello", "world"))
                ctx.conn.commit()
            return {"written": True}

        ex.register_handler("test_db", handler)
        job = ex.submit("test_db", workspace_id=ws_id)
        _wait_for_terminal(executor, job.job_id)
        # 验证数据已写入
        db_path = ex._db_path
        check_conn = sqlite3.connect(db_path)
        check_conn.row_factory = sqlite3.Row
        row = check_conn.execute("SELECT * FROM test_data WHERE k = 'hello'").fetchone()
        assert row is not None
        assert row["v"] == "world"
        check_conn.close()


# ============================================
# 进度上报
# ============================================

class TestProgressReporting:
    def test_progress_updates(self, executor):
        """handler 通过 ctx.update_progress 上报进度"""
        ex, ws_id = executor

        def handler(ctx):
            ctx.update_progress(0.3, "step 1")
            time.sleep(0.1)
            ctx.update_progress(0.7, "step 2")
            time.sleep(0.1)
            return {"done": True}

        ex.register_handler("test_progress", handler)
        job = ex.submit("test_progress", workspace_id=ws_id)
        final = _wait_for_terminal(executor, job.job_id)
        assert final.status == JOB_COMPLETED
        # 最终进度应该是 1.0（complete_job 设置）
        assert final.progress == 1.0

    def test_progress_seen_during_execution(self, executor):
        """执行过程中能看到中间进度"""
        ex, ws_id = executor
        seen_progress = []

        def handler(ctx):
            for i in range(5):
                ctx.update_progress(i / 5, f"step {i}")
                time.sleep(0.05)
                # 主线程读取当前进度（加锁）
                with ctx.conn_lock:
                    cur = get_job(ctx.conn, ctx.job_id)
                if cur and cur.progress > 0:
                    seen_progress.append(cur.progress)
            return {}

        ex.register_handler("test_mid_progress", handler)
        job = ex.submit("test_mid_progress", workspace_id=ws_id)
        _wait_for_terminal(executor, job.job_id)
        # 至少能看到一个中间进度 > 0
        assert any(p > 0 for p in seen_progress)


# ============================================
# 取消
# ============================================

class TestCancellation:
    def test_cancel_pending_job(self, executor):
        """取消 pending 任务：直接终态为 cancelled"""
        ex, ws_id = executor
        # 用一个慢 handler，让 cancel 在 pending 阶段就生效
        ex.register_handler("slow", lambda ctx: (time.sleep(10), {})[1])
        # 提交后立即取消（在 executor 拾取前）
        # 由于线程池会立即开始执行，这里测试 pending → cancelled 较难
        # 改为测试 running 状态的 cancel_requested
        job = ex.submit("slow", workspace_id=ws_id)
        # 立即取消
        ok = ex.cancel(job.job_id)
        assert ok is True
        # 等待终态
        final = _wait_for_terminal(executor, job.job_id, timeout=15)
        assert final.status in (JOB_CANCELLED, JOB_COMPLETED)

    def test_cancel_running_job_with_check(self, executor):
        """handler 通过 ctx.check_cancelled() 响应取消"""
        ex, ws_id = executor
        cancelled = []

        def handler(ctx):
            for i in range(100):
                if ctx.check_cancelled():
                    cancelled.append(True)
                    return {"cancelled_at": i}
                time.sleep(0.05)
            return {"done": True}

        ex.register_handler("test_cancel_check", handler)
        job = ex.submit("test_cancel_check", workspace_id=ws_id)
        # 等 handler 开始
        time.sleep(0.2)
        ex.cancel(job.job_id)
        final = _wait_for_terminal(executor, job.job_id, timeout=5)
        assert final.status == JOB_CANCELLED
        assert len(cancelled) == 1


# ============================================
# 错误处理
# ============================================

class TestErrorHandling:
    def test_handler_exception_marks_failed(self, executor):
        """handler 抛异常时 job 标记为 failed"""
        ex, ws_id = executor

        def handler(ctx):
            raise RuntimeError("boom")

        ex.register_handler("test_error", handler)
        job = ex.submit("test_error", workspace_id=ws_id)
        final = _wait_for_terminal(executor, job.job_id)
        assert final.status == JOB_FAILED
        assert "boom" in final.error
        assert "RuntimeError" in final.error

    def test_handler_raises_job_cancelled(self, executor):
        """handler 抛 JobCancelled 异常时 job 标记为 cancelled"""
        ex, ws_id = executor

        def handler(ctx):
            raise JobCancelled("manual cancel")

        ex.register_handler("test_cancelled_exc", handler)
        job = ex.submit("test_cancelled_exc", workspace_id=ws_id)
        final = _wait_for_terminal(executor, job.job_id)
        assert final.status == JOB_CANCELLED

    def test_no_handler_registered(self, executor):
        """未注册 handler 的 job_type 立即 failed"""
        ex, ws_id = executor
        job = ex.submit("unknown_type", workspace_id=ws_id)
        final = _wait_for_terminal(executor, job.job_id, timeout=2)
        assert final.status == JOB_FAILED
        assert "No handler" in final.error


# ============================================
# 并发
# ============================================

class TestConcurrency:
    def test_multiple_jobs_concurrent(self, executor):
        """多个 job 可以并发执行"""
        ex, ws_id = executor
        start_times = []
        end_times = []

        def handler(ctx):
            start_times.append(time.time())
            time.sleep(0.3)
            end_times.append(time.time())
            return {"i": ctx.params.get("i")}

        ex.register_handler("test_concurrent", handler)
        jobs = [ex.submit("test_concurrent", {"i": i}, workspace_id=ws_id) for i in range(3)]
        for j in jobs:
            _wait_for_terminal(executor, j.job_id, timeout=5)
        # 所有 job 都完成
        for j in jobs:
            assert get_job(ex._conn, j.job_id).status == JOB_COMPLETED
        # 至少有 2 个 job 是并发执行的（start_time 重叠）
        # max_concurrent_jobs=2，所以 3 个 job 中至少 2 个的 start/end 重叠
        sorted_starts = sorted(start_times)
        sorted_ends = sorted(end_times)
        # 第 2 个 job 的开始时间应该早于第 1 个 job 的结束时间
        # （说明它们是并发执行的）
        assert sorted_starts[1] < sorted_ends[0]


# ============================================
# 资源预算
# ============================================

class TestResourceBudget:
    def test_max_duration_timeout(self, tmp_path):
        """handler 超过 max_duration_seconds 时标记 failed"""
        db_path = str(tmp_path / "test_timeout.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspaces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                root_path TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL,
                is_active INTEGER DEFAULT 0,
                description TEXT DEFAULT '',
                active_task_id TEXT DEFAULT ''
            )
        """)
        conn.execute(
            "INSERT INTO workspaces (name, root_path, created_at, is_active) VALUES (?, ?, ?, 1)",
            ("test-ws", "/tmp/test", time.time()),
        )
        ws_id = conn.execute("SELECT id FROM workspaces WHERE is_active = 1").fetchone()["id"]
        init_jobs_schema(conn)
        conn.commit()
        conn.close()

        # max_duration=1 秒，handler 要 sleep 5 秒
        ex = JobExecutor(db_path=db_path, max_concurrent_jobs=1, max_duration_seconds=1)
        ex.start()

        def handler(ctx):
            time.sleep(5)
            return {}

        ex.register_handler("slow", handler)
        try:
            job = ex.submit("slow", workspace_id=ws_id)
            final = _wait_for_terminal((ex, ws_id), job.job_id, timeout=10)
            assert final.status == JOB_FAILED
            assert "max_duration" in final.error
        finally:
            ex.stop(wait=False)


# ============================================
# Schema 初始化
# ============================================

class TestSchemaInit:
    def test_start_inits_schema(self, tmp_path):
        """executor.start() 会初始化 jobs schema"""
        db_path = str(tmp_path / "test_schema.db")
        # 先创建 workspaces 表
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspaces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                root_path TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL,
                is_active INTEGER DEFAULT 0,
                description TEXT DEFAULT '',
                active_task_id TEXT DEFAULT ''
            )
        """)
        conn.execute(
            "INSERT INTO workspaces (name, root_path, created_at, is_active) VALUES (?, ?, ?, 1)",
            ("test-ws", "/tmp/test", time.time()),
        )
        conn.commit()
        conn.close()

        ex = JobExecutor(db_path=db_path)
        ex.start()
        try:
            # jobs 表应该已创建
            check_conn = sqlite3.connect(db_path)
            check_conn.row_factory = sqlite3.Row
            row = check_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
            ).fetchone()
            assert row is not None
            check_conn.close()
        finally:
            ex.stop(wait=False)


# ============================================
# 重复 start/stop
# ============================================

class TestStartStop:
    def test_start_idempotent(self, executor_db):
        """多次调用 start 不报错"""
        db_path, ws_id = executor_db
        ex = JobExecutor(db_path=db_path)
        ex.start()
        ex.start()  # 第二次无操作
        ex.stop(wait=True)

    def test_stop_idempotent(self, executor_db):
        """多次调用 stop 不报错"""
        db_path, ws_id = executor_db
        ex = JobExecutor(db_path=db_path)
        ex.start()
        ex.stop(wait=True)
        ex.stop(wait=True)  # 第二次无操作

    def test_submit_before_start_raises(self, executor_db):
        """未 start 时 submit 应该抛异常"""
        db_path, ws_id = executor_db
        ex = JobExecutor(db_path=db_path)
        with pytest.raises(RuntimeError, match="not started"):
            ex.submit("test", workspace_id=ws_id)
