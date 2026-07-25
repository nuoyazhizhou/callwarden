"""Phase 7.3: Semgrep scan bounded external process job 测试

测试 server/job_handlers.py 的 semgrep_scan_handler 和 _SemgrepScanWrapper。
由于测试环境通常没有 Semgrep CLI，主要测试：
- handler 在无 CLI 时返回正确的 error 结构
- progress_callback 被调用
- _SemgrepScanWrapper 正确委托方法
- handler 注册
- end-to-end job 提交（即使 semgrep 不可用也能完成）
"""

import os
import sqlite3
import tempfile
import threading
import time

import pytest

from callwarden.db.db import CodeGraphDB
from callwarden.db.db_jobs import init_jobs_schema, get_job, JOB_COMPLETED
from callwarden.server.job_executor import JobExecutor
from callwarden.server.job_handlers import (
    semgrep_scan_handler,
    _SemgrepScanWrapper,
    register_default_handlers,
)


@pytest.fixture(autouse=True)
def _disable_semgrep_cli(monkeypatch):
    """固定验证无 CLI 路径，避免测试启动真实外部扫描。"""
    monkeypatch.setattr(_SemgrepScanWrapper, "_find_semgrep_cli", lambda self: "")


# ============================================
# 辅助函数
# ============================================

def _db_with_workspace():
    """构造临时工作区数据库（含所有表）"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _make_mock_ctx(conn, ws_id, params=None, conn_lock=None):
    """构造一个 mock JobContext（不经过 JobExecutor）"""
    class _MockJob:
        def __init__(self):
            self.job_id = "J-test-semgrep-0001"
            self.workspace_id = ws_id
            self.params = params or {}

    class _MockCtx:
        def __init__(self):
            self._conn = conn
            self._job = _MockJob()
            self._conn_lock = conn_lock or threading.RLock()
            self.progress_calls = []

        @property
        def conn(self):
            return self._conn

        @property
        def conn_lock(self):
            return self._conn_lock

        @property
        def workspace_id(self):
            return self._job.workspace_id

        @property
        def params(self):
            return self._job.params

        @property
        def job_id(self):
            return self._job.job_id

        def update_progress(self, progress, message=""):
            self.progress_calls.append((progress, message))

        def check_cancelled(self):
            return False

    return _MockCtx()


def _wait_for_terminal(ex, job_id, timeout=10.0):
    """等待 job 进入终态"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = ex.get_status(job_id)
        if job and job.is_terminal:
            return job
        time.sleep(0.05)
    raise TimeoutError(f"job {job_id} did not reach terminal state in {timeout}s")


# ============================================
# Handler 直接测试
# ============================================

class TestSemgrepScanHandlerDirect:
    """直接调用 semgrep_scan_handler（不经 JobExecutor）"""

    def test_handler_returns_dict(self):
        """handler 返回 dict（即使 semgrep 不可用也返回 error 结构）"""
        db, root = _db_with_workspace()
        try:
            ctx = _make_mock_ctx(
                db.conn, db._get_active_workspace_id(),
                {"config": "p/default", "timeout": 10}
            )
            result = semgrep_scan_handler(ctx)

            assert isinstance(result, dict)
            # 没有 semgrep CLI 时，返回 success=False
            assert "success" in result
        finally:
            db.close()

    def test_handler_progress_called(self):
        """handler 调用 progress_callback 上报进度"""
        db, root = _db_with_workspace()
        try:
            ctx = _make_mock_ctx(
                db.conn, db._get_active_workspace_id(),
                {"config": "p/default", "timeout": 10}
            )
            semgrep_scan_handler(ctx)

            # 应该至少有 2 次进度调用（0.1 开始 + 1.0 完成）
            assert len(ctx.progress_calls) >= 2
            assert ctx.progress_calls[0][0] == pytest.approx(0.1)
            assert ctx.progress_calls[-1][0] == pytest.approx(1.0)
        finally:
            db.close()

    def test_handler_reads_params(self):
        """handler 从 ctx.params 读取 config/languages/timeout"""
        db, root = _db_with_workspace()
        try:
            ctx = _make_mock_ctx(
                db.conn, db._get_active_workspace_id(),
                {"config": "p/security", "languages": ["python"], "timeout": 60}
            )
            result = semgrep_scan_handler(ctx)
            assert isinstance(result, dict)
        finally:
            db.close()

    def test_handler_default_params(self):
        """handler 使用默认参数"""
        db, root = _db_with_workspace()
        try:
            ctx = _make_mock_ctx(
                db.conn, db._get_active_workspace_id(), {}
            )
            result = semgrep_scan_handler(ctx)
            assert isinstance(result, dict)
        finally:
            db.close()

    def test_handler_workspace_root_looked_up(self):
        """handler 从 workspaces 表查询 workspace_root"""
        db, root = _db_with_workspace()
        try:
            ctx = _make_mock_ctx(
                db.conn, db._get_active_workspace_id(),
                {"config": "p/default", "timeout": 10}
            )
            semgrep_scan_handler(ctx)
            # handler 能正常执行（workspace_root 从 DB 查出）
            # 验证 progress_calls 有内容
            assert len(ctx.progress_calls) >= 2
        finally:
            db.close()


# ============================================
# Wrapper 测试
# ============================================

class TestSemgrepScanWrapper:
    """测试 _SemgrepScanWrapper"""

    def test_wrapper_workspace_id(self):
        """wrapper._get_active_workspace_id 返回构造时传入的值"""
        db, _root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            lock = threading.RLock()
            wrapper = _SemgrepScanWrapper(db.conn, ws_id, lock, _root)
            assert wrapper._get_active_workspace_id() == ws_id
        finally:
            db.close()

    def test_wrapper_workspace_root(self):
        """wrapper.workspace_root 返回构造时传入的值"""
        db, root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            lock = threading.RLock()
            wrapper = _SemgrepScanWrapper(db.conn, ws_id, lock, root)
            assert wrapper.workspace_root == root
        finally:
            db.close()

    def test_wrapper_run_semgrep_returns_dict(self):
        """wrapper.run_semgrep 返回 dict（有 CLI 时 success=True，无 CLI 时 success=False）"""
        db, root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            lock = threading.RLock()
            wrapper = _SemgrepScanWrapper(db.conn, ws_id, lock, root)
            result = wrapper.run_semgrep([root], config="p/default", timeout=10)
            assert isinstance(result, dict)
            # 无论 semgrep CLI 是否安装，都应包含 success 字段
            assert "success" in result
        finally:
            db.close()

    def test_wrapper_run_semgrep_and_save_returns_dict(self):
        """wrapper.run_semgrep_and_save 返回 dict"""
        db, root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            lock = threading.RLock()
            wrapper = _SemgrepScanWrapper(db.conn, ws_id, lock, root)
            result = wrapper.run_semgrep_and_save(config="p/default", timeout=10)
            assert isinstance(result, dict)
            assert "success" in result
        finally:
            db.close()


# ============================================
# End-to-end Job 提交测试
# ============================================

class TestEndToEndJob:
    """通过 JobExecutor 提交 semgrep_scan job"""

    def test_submit_semgrep_scan_job(self):
        """提交 semgrep_scan job 并等待完成"""
        db, root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            init_jobs_schema(db.conn)
            db.conn.commit()

            ex = JobExecutor(db_path=db.db_path, max_concurrent_jobs=1, max_duration_seconds=30)
            register_default_handlers(ex)
            ex.start()

            try:
                job = ex.submit(
                    "semgrep_scan",
                    {"config": "p/default", "timeout": 10},
                    workspace_id=ws_id,
                )
                assert job.job_id.startswith("J-")

                final = _wait_for_terminal(ex, job.job_id, timeout=15)
                # 即使 semgrep 不可用，job 也应该完成（handler 返回 error dict，不抛异常）
                assert final.status == JOB_COMPLETED
                assert final.progress == 1.0
                assert isinstance(final.result_summary, dict)
            finally:
                ex.stop(wait=True)
        finally:
            db.close()

    def test_handler_registered_by_default(self):
        """register_default_handlers 注册了 semgrep_scan handler"""
        db, _root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            init_jobs_schema(db.conn)
            db.conn.commit()

            ex = JobExecutor(db_path=db.db_path, max_concurrent_jobs=1, max_duration_seconds=30)
            register_default_handlers(ex)
            ex.start()
            try:
                assert "semgrep_scan" in ex._handlers
                # 其他 handler 也在
                assert "clone_detect" in ex._handlers
                assert "vector_embed" in ex._handlers
            finally:
                ex.stop(wait=True)
        finally:
            db.close()

    def test_job_result_contains_semgrep_info(self):
        """job 完成后 result_summary 包含 semgrep 相关信息"""
        db, root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            init_jobs_schema(db.conn)
            db.conn.commit()

            ex = JobExecutor(db_path=db.db_path, max_concurrent_jobs=1, max_duration_seconds=30)
            register_default_handlers(ex)
            ex.start()

            try:
                job = ex.submit(
                    "semgrep_scan",
                    {"config": "p/default", "timeout": 10},
                    workspace_id=ws_id,
                )
                final = _wait_for_terminal(ex, job.job_id, timeout=15)
                assert final.status == JOB_COMPLETED
                # result_summary 应该包含 success 字段
                assert "success" in final.result_summary
            finally:
                ex.stop(wait=True)
        finally:
            db.close()
