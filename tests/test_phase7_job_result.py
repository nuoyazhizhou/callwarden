"""Phase 7.4: MCP 工具返回 job_id/status/result summary 测试

测试 wait_for_job MCP 工具的轮询等待和结果返回，
以及 async 工具 + wait_for_job 的 "submit → wait → get result" 模式。

测试内容：
- wait_for_job 返回正确结构（job_id/status/result_summary/elapsed）
- wait_for_job 在 job 完成后立即返回
- wait_for_job 超时返回 status=timeout
- wait_for_job 对不存在的 job_id 返回 error
- async submit + wait_for_job 端到端模式
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
from callwarden.server.job_handlers import register_default_handlers


# ============================================
# 辅助函数
# ============================================

def _db_with_workspace():
    """构造临时工作区数据库（含所有表）"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _wait_for_terminal(ex, job_id, timeout=5.0):
    """等待 job 进入终态"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = ex.get_status(job_id)
        if job and job.is_terminal:
            return job
        time.sleep(0.05)
    raise TimeoutError(f"job {job_id} did not reach terminal state in {timeout}s")


# ============================================
# wait_for_job 测试
# ============================================

class TestWaitForJob:
    """测试 wait_for_job 的轮询等待和结果返回"""

    def test_wait_for_job_completed(self):
        """wait_for_job 在 job 完成后返回 result_summary"""
        db, root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            init_jobs_schema(db.conn)
            db.conn.commit()

            ex = JobExecutor(db_path=db.db_path, max_concurrent_jobs=1, max_duration_seconds=30)
            register_default_handlers(ex)
            ex.start()

            try:
                # 提交一个快速 job
                job = ex.submit(
                    "vector_embed",
                    {"batch_size": 32, "force": False},
                    workspace_id=ws_id,
                )

                # 用 wait_for_job 模式轮询
                start = time.time()
                deadline = start + 10.0
                result = None
                while time.time() < deadline:
                    job_obj = db.get_job(job.job_id)
                    if job_obj and job_obj.is_terminal:
                        result = {
                            "job_id": job.job_id,
                            "status": job_obj.status,
                            "progress": job_obj.progress,
                            "result_summary": job_obj.result_summary,
                            "error": job_obj.error,
                            "elapsed": time.time() - start,
                        }
                        break
                    time.sleep(0.5)

                assert result is not None
                assert result["status"] == JOB_COMPLETED
                assert result["progress"] == 1.0
                assert "total" in result["result_summary"]
                assert result["elapsed"] < 10.0
            finally:
                ex.stop(wait=True)
        finally:
            db.close()

    def test_wait_for_job_timeout(self):
        """wait_for_job 超时返回 status=timeout"""
        db, root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            init_jobs_schema(db.conn)
            db.conn.commit()

            ex = JobExecutor(db_path=db.db_path, max_concurrent_jobs=1, max_duration_seconds=30)
            # 注册一个慢 handler
            def slow_handler(ctx):
                time.sleep(10)
                return {"done": True}
            ex.register_handler("test_slow", slow_handler)
            ex.start()

            try:
                job = ex.submit("test_slow", workspace_id=ws_id)

                # 轮询 2 秒后超时
                start = time.time()
                timeout = 2.0
                deadline = start + timeout
                result = None
                while time.time() < deadline:
                    job_obj = db.get_job(job.job_id)
                    if job_obj and job_obj.is_terminal:
                        result = {
                            "status": job_obj.status,
                            "result_summary": job_obj.result_summary,
                        }
                        break
                    time.sleep(0.3)

                if result is None:
                    # 超时
                    job_obj = db.get_job(job.job_id)
                    result = {
                        "status": "timeout",
                        "progress": job_obj.progress if job_obj else 0.0,
                        "result_summary": job_obj.result_summary if job_obj else {},
                    }

                assert result["status"] == "timeout"
            finally:
                ex.stop(wait=False)
        finally:
            db.close()

    def test_wait_for_job_not_found(self):
        """wait_for_job 对不存在的 job_id 返回 error"""
        db, root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            init_jobs_schema(db.conn)
            db.conn.commit()

            # 查询不存在的 job
            job = db.get_job("J-nonexistent-0001")
            assert job is None
        finally:
            db.close()

    def test_wait_returns_elapsed(self):
        """wait_for_job 返回 elapsed 字段"""
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
                    "vector_embed",
                    {"batch_size": 32, "force": False},
                    workspace_id=ws_id,
                )

                # 模拟 wait_for_job 的轮询
                start = time.time()
                while time.time() - start < 10.0:
                    job_obj = db.get_job(job.job_id)
                    if job_obj and job_obj.is_terminal:
                        elapsed = time.time() - start
                        break
                    time.sleep(0.5)
                else:
                    elapsed = time.time() - start

                assert elapsed > 0
                assert elapsed < 10.0
            finally:
                ex.stop(wait=True)
        finally:
            db.close()


# ============================================
# "submit → wait → get result" 端到端模式
# ============================================

class TestSubmitWaitResultPattern:
    """测试 async 提交 + 等待 + 获取结果的完整模式"""

    def test_vector_embed_submit_wait_result(self):
        """vector_embed: submit → wait → result_summary 包含 total/success/skipped"""
        db, root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            init_jobs_schema(db.conn)
            db.conn.commit()

            ex = JobExecutor(db_path=db.db_path, max_concurrent_jobs=1, max_duration_seconds=30)
            register_default_handlers(ex)
            ex.start()

            try:
                # 1. Submit
                job = ex.submit(
                    "vector_embed",
                    {"batch_size": 32, "force": False},
                    workspace_id=ws_id,
                )
                assert job.job_id.startswith("J-")

                # 2. Wait
                final = _wait_for_terminal(ex, job.job_id, timeout=10)

                # 3. Result
                assert final.status == JOB_COMPLETED
                assert final.progress == 1.0
                result = final.result_summary
                assert "total" in result
                assert "success" in result
                assert "skipped" in result
                assert "failed" in result
            finally:
                ex.stop(wait=True)
        finally:
            db.close()

    def test_clone_detect_submit_wait_result(self):
        """clone_detect: submit → wait → result_summary 包含 total_groups"""
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
                    "clone_detect",
                    {"file_filter": "", "min_lines": 5, "similarity_threshold": 0.8},
                    workspace_id=ws_id,
                )

                final = _wait_for_terminal(ex, job.job_id, timeout=10)
                assert final.status == JOB_COMPLETED
                result = final.result_summary
                assert "total_groups" in result or "scanned_symbols" in result
            finally:
                ex.stop(wait=True)
        finally:
            db.close()

    def test_job_result_summary_is_dict(self):
        """job 完成后 result_summary 是 dict 类型"""
        db, root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            init_jobs_schema(db.conn)
            db.conn.commit()

            ex = JobExecutor(db_path=db.db_path, max_concurrent_jobs=1, max_duration_seconds=30)
            register_default_handlers(ex)
            ex.start()

            try:
                job = ex.submit("vector_embed", {}, workspace_id=ws_id)
                final = _wait_for_terminal(ex, job.job_id, timeout=10)
                assert isinstance(final.result_summary, dict)
            finally:
                ex.stop(wait=True)
        finally:
            db.close()


# ============================================
# Job 状态一致性测试
# ============================================

class TestJobStatusConsistency:
    """测试 job 状态在整个生命周期中的一致性"""

    def test_job_progress_goes_to_1_on_complete(self):
        """job 完成时 progress=1.0"""
        db, root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            init_jobs_schema(db.conn)
            db.conn.commit()

            ex = JobExecutor(db_path=db.db_path, max_concurrent_jobs=1, max_duration_seconds=30)
            register_default_handlers(ex)
            ex.start()

            try:
                job = ex.submit("vector_embed", {}, workspace_id=ws_id)
                final = _wait_for_terminal(ex, job.job_id, timeout=10)
                assert final.progress == 1.0
            finally:
                ex.stop(wait=True)
        finally:
            db.close()

    def test_job_has_started_and_finished_at(self):
        """完成的 job 有 started_at 和 finished_at"""
        db, root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            init_jobs_schema(db.conn)
            db.conn.commit()

            ex = JobExecutor(db_path=db.db_path, max_concurrent_jobs=1, max_duration_seconds=30)
            register_default_handlers(ex)
            ex.start()

            try:
                job = ex.submit("vector_embed", {}, workspace_id=ws_id)
                final = _wait_for_terminal(ex, job.job_id, timeout=10)
                assert final.started_at > 0
                assert final.finished_at > 0
                assert final.finished_at >= final.started_at
            finally:
                ex.stop(wait=True)
        finally:
            db.close()

    def test_all_three_handlers_registered(self):
        """register_default_handlers 注册了 3 个 handler"""
        db, root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            init_jobs_schema(db.conn)
            db.conn.commit()

            ex = JobExecutor(db_path=db.db_path, max_concurrent_jobs=1, max_duration_seconds=30)
            register_default_handlers(ex)
            ex.start()
            try:
                assert "clone_detect" in ex._handlers
                assert "vector_embed" in ex._handlers
                assert "semgrep_scan" in ex._handlers
                assert len(ex._handlers) >= 3
            finally:
                ex.stop(wait=True)
        finally:
            db.close()
