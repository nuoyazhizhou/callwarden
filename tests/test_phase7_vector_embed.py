"""Phase 7.2: Vector embed 增量 job 测试

测试 server/job_handlers.py 的 vector_embed_handler 和 _VectorEmbedWrapper，
以及 db/db_vector.py 的 embed_all_symbols 的 progress_callback 支持。

测试内容：
- vector_embed_handler 返回正确结构
- progress_callback 被调用（0.05 / 0.1 / 1.0 等关键点）
- 增量模式（force=False）跳过已有嵌入的符号
- 强制模式（force=True）重新嵌入所有符号
- _VectorEmbedWrapper 正确委托 embed_all_symbols
- embedder 不可用时全部跳过
- end-to-end job 提交 + 完成
"""

import os
import sqlite3
import tempfile
import threading
import time

import pytest

from callwarden.db.db import CodeGraphDB
from callwarden.db.db_jobs import init_jobs_schema, get_job, JOB_COMPLETED
from callwarden.server.job_executor import JobExecutor, JobContext
from callwarden.server.job_handlers import (
    vector_embed_handler,
    _VectorEmbedWrapper,
    register_default_handlers,
)


# ============================================
# 辅助函数
# ============================================

def _db_with_workspace():
    """构造临时工作区数据库（含所有表）"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _seed_symbol(
    db,
    rel_path,
    symbol_name,
    content,
    start_line=1,
    end_line=10,
    symbol_hash=None,
):
    """辅助：创建一个文件实例 + 符号 + 符号内容

    与 test_clone_detection.py 中的 _seed_symbol 一致。
    """
    ws_id = db._get_active_workspace_id()
    ch = symbol_hash or f"hash_{symbol_name}_{rel_path}"

    db.conn.execute(
        "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) "
        "VALUES (?, 'python', ?, 0)",
        (ch, end_line - start_line + 1),
    )
    db.conn.execute(
        "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
        "mtime, status, module_path) VALUES (?, ?, ?, ?, 0, 'parsed', '')",
        (ws_id, rel_path, os.path.join(db.workspace_root, rel_path), ch),
    )
    fi_id = db.conn.execute(
        "SELECT id FROM file_instances WHERE rel_path=?", (rel_path,)
    ).fetchone()[0]

    db.conn.execute(
        "INSERT OR REPLACE INTO symbol_contents (content_hash, name, kind, content, signature, "
        "has_comment, comment_content, qualified_name) "
        "VALUES (?, ?, 'fn', ?, '', 0, '', ?)",
        (ch, symbol_name, content, symbol_name),
    )

    db.conn.execute(
        "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, start_line, end_line, "
        "qualified_name, comment_status) VALUES (?, ?, ?, 'fn', ?, ?, ?, 'pending') "
        "ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET symbol_hash = excluded.symbol_hash",
        (fi_id, ch, symbol_name, start_line, end_line, symbol_name),
    )
    sym_id = db.conn.execute(
        "SELECT id FROM symbols WHERE file_instance_id=? AND name=? AND start_line=?",
        (fi_id, symbol_name, start_line),
    ).fetchone()[0]

    db.conn.commit()
    return sym_id


def _make_mock_ctx(conn, ws_id, params=None, conn_lock=None):
    """构造一个 mock JobContext（不经过 JobExecutor）

    用于直接测试 handler 而不需要启动线程池。
    """
    class _MockJob:
        def __init__(self):
            self.job_id = "J-test-mock-0001"
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
# Handler 直接测试
# ============================================

class TestVectorEmbedHandlerDirect:
    """直接调用 vector_embed_handler（不经 JobExecutor）"""

    def test_handler_returns_stats_dict(self):
        """handler 返回包含 total/success/skipped/failed 的 dict"""
        db, _root = _db_with_workspace()
        try:
            _seed_symbol(db, "a.py", "foo", "def foo():\n    return 1\n")
            _seed_symbol(db, "b.py", "bar", "def bar():\n    return 2\n")

            ctx = _make_mock_ctx(
                db.conn, db._get_active_workspace_id(), {"batch_size": 32, "force": False}
            )
            result = vector_embed_handler(ctx)

            assert "total" in result
            assert "success" in result
            assert "skipped" in result
            assert "failed" in result
            # 没有 embedder 时，全部 skipped
            assert result["total"] == 2
            assert result["success"] == 0
            assert result["skipped"] == 2
        finally:
            db.close()

    def test_handler_progress_callback_called(self):
        """handler 调用 progress_callback 上报进度"""
        db, _root = _db_with_workspace()
        try:
            _seed_symbol(db, "a.py", "foo", "def foo():\n    return 1\n")

            ctx = _make_mock_ctx(
                db.conn, db._get_active_workspace_id(), {"batch_size": 32, "force": False}
            )
            vector_embed_handler(ctx)

            # embedder 不可用时也会调用 progress_callback
            # 0.05 (loading) + 1.0 (done/skipped)
            assert len(ctx.progress_calls) >= 2
            # 第一个进度是 loading（0.05）
            assert ctx.progress_calls[0][0] == pytest.approx(0.05)
            # 最后一个进度是 1.0
            assert ctx.progress_calls[-1][0] == pytest.approx(1.0)
        finally:
            db.close()

    def test_handler_no_symbols_returns_zero(self):
        """没有符号时返回 total=0"""
        db, _root = _db_with_workspace()
        try:
            ctx = _make_mock_ctx(
                db.conn, db._get_active_workspace_id(), {"batch_size": 32, "force": False}
            )
            result = vector_embed_handler(ctx)
            assert result["total"] == 0
        finally:
            db.close()

    def test_handler_reads_params(self):
        """handler 从 ctx.params 读取 batch_size 和 force"""
        db, _root = _db_with_workspace()
        try:
            _seed_symbol(db, "a.py", "foo", "def foo():\n    return 1\n")

            # 测试 force=True
            ctx = _make_mock_ctx(
                db.conn, db._get_active_workspace_id(), {"batch_size": 10, "force": True}
            )
            result = vector_embed_handler(ctx)
            # embedder 不可用，仍然全部 skipped
            assert result["total"] == 1
        finally:
            db.close()


# ============================================
# 增量模式测试
# ============================================

class TestIncrementalMode:
    """测试 force=False（增量模式）跳过已有嵌入的符号"""

    def test_incremental_skips_embedded_symbols(self):
        """已有嵌入的符号在增量模式下被跳过"""
        db, _root = _db_with_workspace()
        try:
            # 3 个符号
            _seed_symbol(db, "a.py", "foo", "def foo():\n    return 1\n", symbol_hash="hash_a")
            _seed_symbol(db, "b.py", "bar", "def bar():\n    return 2\n", symbol_hash="hash_b")
            _seed_symbol(db, "c.py", "baz", "def baz():\n    return 3\n", symbol_hash="hash_c")

            # 预先给 hash_a 插入一条嵌入
            import numpy
            fake_vec = numpy.array([0.1] * 768, dtype=numpy.float32).tobytes()
            db.conn.execute(
                "INSERT OR REPLACE INTO symbol_embeddings "
                "(symbol_hash, embedding, model_version, dim, embedded_at) "
                "VALUES (?, ?, 'test', 768, ?)",
                ("hash_a", fake_vec, time.time()),
            )
            db.conn.commit()

            # 增量模式：只查没有嵌入的符号
            ctx = _make_mock_ctx(
                db.conn, db._get_active_workspace_id(), {"force": False}
            )
            result = vector_embed_handler(ctx)

            # hash_a 被跳过（已有嵌入），只查 hash_b 和 hash_c
            assert result["total"] == 2
        finally:
            db.close()

    def test_force_re_embeds_all(self):
        """force=True 时查所有符号（包括已有嵌入的）"""
        db, _root = _db_with_workspace()
        try:
            _seed_symbol(db, "a.py", "foo", "def foo():\n    return 1\n", symbol_hash="hash_a")
            _seed_symbol(db, "b.py", "bar", "def bar():\n    return 2\n", symbol_hash="hash_b")

            # 预先给 hash_a 插入一条嵌入
            import numpy
            fake_vec = numpy.array([0.1] * 768, dtype=numpy.float32).tobytes()
            db.conn.execute(
                "INSERT OR REPLACE INTO symbol_embeddings "
                "(symbol_hash, embedding, model_version, dim, embedded_at) "
                "VALUES (?, ?, 'test', 768, ?)",
                ("hash_a", fake_vec, time.time()),
            )
            db.conn.commit()

            # force=True：查所有符号
            ctx = _make_mock_ctx(
                db.conn, db._get_active_workspace_id(), {"force": True}
            )
            result = vector_embed_handler(ctx)

            # 两个符号都查出来
            assert result["total"] == 2
        finally:
            db.close()


# ============================================
# Wrapper 测试
# ============================================

class TestVectorEmbedWrapper:
    """测试 _VectorEmbedWrapper"""

    def test_wrapper_workspace_id(self):
        """wrapper._get_active_workspace_id 返回构造时传入的值"""
        db, _root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            lock = threading.RLock()
            wrapper = _VectorEmbedWrapper(db.conn, ws_id, lock)
            assert wrapper._get_active_workspace_id() == ws_id
        finally:
            db.close()

    def test_wrapper_embedder_instance_init_none(self):
        """wrapper 初始化时 _embedder_instance 为 None"""
        db, _root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            lock = threading.RLock()
            wrapper = _VectorEmbedWrapper(db.conn, ws_id, lock)
            assert wrapper._embedder_instance is None
        finally:
            db.close()

    def test_wrapper_embed_all_symbols_delegates(self):
        """wrapper.embed_all_symbols 正确委托到 VectorSearchMixin"""
        db, _root = _db_with_workspace()
        try:
            _seed_symbol(db, "a.py", "foo", "def foo():\n    return 1\n")
            ws_id = db._get_active_workspace_id()
            lock = threading.RLock()
            wrapper = _VectorEmbedWrapper(db.conn, ws_id, lock)

            progress_calls = []
            result = wrapper.embed_all_symbols(
                batch_size=32,
                force=False,
                progress_callback=lambda p, m: progress_calls.append((p, m)),
            )

            assert "total" in result
            assert result["total"] == 1
            # embedder 不可用，全部 skipped
            assert result["skipped"] == 1
            assert len(progress_calls) >= 2
        finally:
            db.close()


# ============================================
# embed_all_symbols progress_callback 测试
# ============================================

class TestEmbedAllSymbolsProgress:
    """测试 embed_all_symbols 的 progress_callback 参数"""

    def test_progress_called_without_embedder(self):
        """embedder 不可用时 progress_callback 仍被调用"""
        db, _root = _db_with_workspace()
        try:
            _seed_symbol(db, "a.py", "foo", "def foo():\n    return 1\n")
            _seed_symbol(db, "b.py", "bar", "def bar():\n    return 2\n")

            # 预设 embedder 不可用，避免加载 sentence-transformers
            db._embedder_instance = None

            progress_calls = []
            result = db.embed_all_symbols(progress_callback=lambda p, m: progress_calls.append((p, m)))

            # embedder 不可用
            assert result["success"] == 0
            assert result["skipped"] == 2
            # 进度调用：0.05 (loading) + 1.0 (done)
            assert len(progress_calls) >= 2
            assert progress_calls[0][0] == pytest.approx(0.05)
            assert progress_calls[-1][0] == pytest.approx(1.0)
        finally:
            db.close()

    def test_progress_none_callback(self):
        """progress_callback=None 时不报错"""
        db, _root = _db_with_workspace()
        try:
            _seed_symbol(db, "a.py", "foo", "def foo():\n    return 1\n")
            db._embedder_instance = None
            result = db.embed_all_symbols(progress_callback=None)
            assert result["total"] == 1
        finally:
            db.close()

    def test_progress_message_content(self):
        """progress message 包含有意义的信息"""
        db, _root = _db_with_workspace()
        try:
            _seed_symbol(db, "a.py", "foo", "def foo():\n    return 1\n")
            db._embedder_instance = None

            progress_calls = []
            db.embed_all_symbols(progress_callback=lambda p, m: progress_calls.append((p, m)))

            # 检查 message 非空
            messages = [m for _, m in progress_calls]
            assert all(m for m in messages)  # 所有 message 非空
            # 包含 "embedder" 相关信息（不可用时）
            assert any("skip" in m.lower() or "embedder" in m.lower() or "done" in m.lower() for m in messages)
        finally:
            db.close()


# ============================================
# End-to-end Job 提交测试
# ============================================

class TestEndToEndJob:
    """通过 JobExecutor 提交 vector_embed job"""

    def test_submit_vector_embed_job(self):
        """提交 vector_embed job 并等待完成"""
        db, _root = _db_with_workspace()
        try:
            _seed_symbol(db, "a.py", "foo", "def foo():\n    return 1\n")
            _seed_symbol(db, "b.py", "bar", "def bar():\n    return 2\n")

            ws_id = db._get_active_workspace_id()
            # 初始化 jobs schema（CodeGraphDB 可能没有）
            init_jobs_schema(db.conn)
            db.conn.commit()

            # 创建 executor 并注册 handlers
            ex = JobExecutor(db_path=db.db_path, max_concurrent_jobs=1, max_duration_seconds=30)
            register_default_handlers(ex)
            ex.start()

            try:
                job = ex.submit(
                    "vector_embed",
                    {"batch_size": 32, "force": False},
                    workspace_id=ws_id,
                )
                assert job.job_id.startswith("J-")
                assert job.status in ("pending", "running")

                final = _wait_for_terminal(ex, job.job_id, timeout=10)
                assert final.status == JOB_COMPLETED
                assert final.progress == 1.0
                # result_summary 包含 total/success/skipped/failed
                assert "total" in final.result_summary
                assert final.result_summary["total"] == 2
                # embedder 不可用，全部 skipped
                assert final.result_summary["skipped"] == 2
            finally:
                ex.stop(wait=True)
        finally:
            db.close()

    def test_submit_vector_embed_force_mode(self):
        """提交 force=True 的 vector_embed job"""
        db, _root = _db_with_workspace()
        try:
            _seed_symbol(db, "a.py", "foo", "def foo():\n    return 1\n", symbol_hash="hash_a")

            ws_id = db._get_active_workspace_id()
            init_jobs_schema(db.conn)
            db.conn.commit()

            ex = JobExecutor(db_path=db.db_path, max_concurrent_jobs=1, max_duration_seconds=30)
            register_default_handlers(ex)
            ex.start()

            try:
                job = ex.submit(
                    "vector_embed",
                    {"batch_size": 10, "force": True},
                    workspace_id=ws_id,
                )
                final = _wait_for_terminal(ex, job.job_id, timeout=10)
                assert final.status == JOB_COMPLETED
                assert final.result_summary["total"] == 1
            finally:
                ex.stop(wait=True)
        finally:
            db.close()

    def test_handler_registered_by_default(self):
        """register_default_handlers 注册了 vector_embed handler"""
        db, _root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            init_jobs_schema(db.conn)
            db.conn.commit()

            ex = JobExecutor(db_path=db.db_path, max_concurrent_jobs=1, max_duration_seconds=30)
            register_default_handlers(ex)
            ex.start()
            try:
                # vector_embed 应该已注册
                assert "vector_embed" in ex._handlers
                # clone_detect 也应该已注册
                assert "clone_detect" in ex._handlers
            finally:
                ex.stop(wait=True)
        finally:
            db.close()


# ============================================
# 边界条件测试
# ============================================

class TestEdgeCases:
    def test_empty_content_symbol_skipped(self):
        """content 为空的符号不会被查出来"""
        db, _root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            # 插入一个 content 为空的符号
            ch = "hash_empty"
            db.conn.execute(
                "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) "
                "VALUES (?, 'python', 0, 0)",
                (ch,),
            )
            db.conn.execute(
                "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
                "mtime, status, module_path) VALUES (?, 'empty.py', ?, ?, 0, 'parsed', '')",
                (ws_id, os.path.join(db.workspace_root, "empty.py"), ch),
            )
            fi_id = db.conn.execute(
                "SELECT id FROM file_instances WHERE rel_path='empty.py'"
            ).fetchone()[0]
            db.conn.execute(
                "INSERT OR REPLACE INTO symbol_contents (content_hash, name, kind, content, signature, "
                "has_comment, comment_content, qualified_name) "
                "VALUES (?, 'empty_fn', 'fn', '', '', 0, '', 'empty_fn')",
                (ch,),
            )
            db.conn.execute(
                "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, start_line, end_line, "
                "qualified_name, comment_status) VALUES (?, ?, 'empty_fn', 'fn', 1, 5, 'empty_fn', 'pending')",
                (fi_id, ch),
            )
            db.conn.commit()

            ctx = _make_mock_ctx(db.conn, ws_id, {"force": False})
            result = vector_embed_handler(ctx)
            # content 为空，不被查出
            assert result["total"] == 0
        finally:
            db.close()

    def test_non_fn_kind_excluded(self):
        """非 fn/function/method 类型的符号被排除"""
        db, _root = _db_with_workspace()
        try:
            ws_id = db._get_active_workspace_id()
            # 插入一个 class 类型符号
            ch = "hash_class"
            db.conn.execute(
                "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) "
                "VALUES (?, 'python', 5, 0)",
                (ch,),
            )
            db.conn.execute(
                "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
                "mtime, status, module_path) VALUES (?, 'cls.py', ?, ?, 0, 'parsed', '')",
                (ws_id, os.path.join(db.workspace_root, "cls.py"), ch),
            )
            fi_id = db.conn.execute(
                "SELECT id FROM file_instances WHERE rel_path='cls.py'"
            ).fetchone()[0]
            db.conn.execute(
                "INSERT OR REPLACE INTO symbol_contents (content_hash, name, kind, content, signature, "
                "has_comment, comment_content, qualified_name) "
                "VALUES (?, 'MyClass', 'class', 'class MyClass: pass', '', 0, '', 'MyClass')",
                (ch,),
            )
            db.conn.execute(
                "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, start_line, end_line, "
                "qualified_name, comment_status) VALUES (?, ?, 'MyClass', 'class', 1, 1, 'MyClass', 'pending')",
                (fi_id, ch),
            )
            db.conn.commit()

            ctx = _make_mock_ctx(db.conn, ws_id, {"force": False})
            result = vector_embed_handler(ctx)
            # class 类型被排除
            assert result["total"] == 0
        finally:
            db.close()
