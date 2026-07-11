"""Phase 3 CAS 协议规范偏离修复测试

覆盖 4 个 Critical 修复：
1. T-1783751461598-9e78: cas_publish 缺 BEGIN IMMEDIATE 事务包裹
2. T-1783751468540-cdfc: 无 flock 协调（GC/refresh TOCTOU）
3. T-1783751474534-fd24: 无 cas_publish_with_retry
4. T-1783751512576-caf4: 无 file_generations 两阶段 CAS

规范：cas-gc-protocol.md §3/§4/§5
"""
import os
import sys
import sqlite3
import tempfile
import unittest

# 确保能导入 db_cas
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from callwarden.db.db_cas import (
    init_cas_schema, cas_publish, cas_publish_with_retry, cas_lookup,
    cas_pin, cas_gc, file_generation_seen, file_generation_committed,
    _flock_exclusive, _flock_shared, _flock_unlock, _HAS_FCNTL
)


class TestCasPublishTransaction(unittest.TestCase):
    """Bug 1: cas_publish 缺 BEGIN IMMEDIATE 事务包裹"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "cas.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        init_cas_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cas_publish_has_begin_immediate(self):
        """cas_publish 应包含 BEGIN IMMEDIATE"""
        import inspect
        src = inspect.getsource(cas_publish)
        self.assertIn("BEGIN IMMEDIATE", src,
                     "cas_publish 应包含 BEGIN IMMEDIATE 事务包裹")
        self.assertIn("COMMIT", src)
        self.assertIn("ROLLBACK", src)

    def test_cas_publish_atomic_success(self):
        """cas_publish 成功时应原子写入所有阶段"""
        parse_result = {
            "file_size": 100,
            "total_lines": 10,
            "symbols": [
                {"name": "foo", "qualified_name": "test.foo", "content": "def foo(): pass",
                 "kind": "fn", "start_line": 1, "end_line": 1, "start_col": 0, "end_col": 0,
                 "start_byte": 0, "end_byte": 15, "visibility": "public", "signature": "foo()",
                 "has_comment": False, "depth": 0},
            ],
            "raw_calls": [{"caller_id": 0, "caller_name": "foo", "callee_name": "bar", "line": 2, "ordinal": 0}],
            "imports": [{"path": "os", "kind": "import"}],
        }
        cas_publish(self.conn, "key1", "hash1", "python", parse_result)

        # 验证 state = ready
        row = self.conn.execute("SELECT state FROM cas_file_cache WHERE cas_key = 'key1'").fetchone()
        self.assertEqual(row["state"], "ready")

        # 验证 symbols 写入
        row = self.conn.execute("SELECT COUNT(*) as c FROM cas_symbols WHERE cas_key = 'key1'").fetchone()
        self.assertEqual(row["c"], 1)

    def test_cas_publish_rollback_on_error(self):
        """cas_publish 失败时应 ROLLBACK，不留半成品"""
        # 用一个无效的 parse_result 触发异常
        bad_result = {"symbols": "not_a_list"}  # 应为 list
        with self.assertRaises(Exception):
            cas_publish(self.conn, "key2", "hash2", "python", bad_result)

        # 验证没有 building 状态残留（事务回滚了）
        row = self.conn.execute("SELECT state FROM cas_file_cache WHERE cas_key = 'key2'").fetchone()
        self.assertIsNone(row, "失败时应 ROLLBACK，不留 building 残留")


class TestCasPublishWithRetry(unittest.TestCase):
    """Bug 3: 无 cas_publish_with_retry"""

    def test_cas_publish_with_retry_exists(self):
        """cas_publish_with_retry 函数应存在"""
        self.assertTrue(callable(cas_publish_with_retry))

    def test_cas_publish_with_retry_signature(self):
        """cas_publish_with_retry 应有 max_retries 参数"""
        import inspect
        sig = inspect.signature(cas_publish_with_retry)
        self.assertIn("max_retries", sig.parameters)
        self.assertIn("workspace_id", sig.parameters)

    def test_cas_publish_with_retry_success(self):
        """cas_publish_with_retry 成功时应发布 + pin"""
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "cas.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        init_cas_schema(conn)

        parse_result = {"symbols": [], "raw_calls": [], "imports": []}
        cas_publish_with_retry(conn, "key3", "hash3", "python", parse_result, workspace_id=1)

        # 验证 state = ready
        row = conn.execute("SELECT state FROM cas_file_cache WHERE cas_key = 'key3'").fetchone()
        self.assertEqual(row["state"], "ready")

        # 验证 pin 存在
        row = conn.execute("SELECT COUNT(*) as c FROM cas_pending_refs WHERE cas_key = 'key3'").fetchone()
        self.assertEqual(row["c"], 1)

        conn.close()
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


class TestCasGcFlock(unittest.TestCase):
    """Bug 2: 无 flock 协调（GC/refresh TOCTOU）"""

    def test_cas_gc_has_flock_path_param(self):
        """cas_gc 应有 flock_path 参数"""
        import inspect
        sig = inspect.signature(cas_gc)
        self.assertIn("flock_path", sig.parameters)

    def test_flock_helpers_exist(self):
        """跨平台 flock 工具函数应存在"""
        self.assertTrue(callable(_flock_exclusive))
        self.assertTrue(callable(_flock_shared))
        self.assertTrue(callable(_flock_unlock))

    def test_cas_gc_with_flock(self):
        """cas_gc 在 flock_path 指定时应正常工作"""
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "cas.db")
        flock_path = os.path.join(tmpdir, "cas.flock")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        init_cas_schema(conn)

        # 写入一些数据
        parse_result = {"symbols": [], "raw_calls": [], "imports": []}
        cas_publish(conn, "key1", "hash1", "python", parse_result)

        # GC with flock
        result = cas_gc(conn, live_keys={"key1"}, flock_path=flock_path)
        self.assertTrue(result)

        # key1 仍在 live set 中，不应被删除
        row = conn.execute("SELECT state FROM cas_file_cache WHERE cas_key = 'key1'").fetchone()
        self.assertIsNotNone(row)

        conn.close()
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


class TestFileGenerationsTwoPhaseCAS(unittest.TestCase):
    """Bug 4: 无 file_generations 两阶段 CAS"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "cas.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        init_cas_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_file_generations_table_exists(self):
        """file_generations 表应在 init_cas_schema 后存在"""
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='file_generations'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_file_generation_seen_success(self):
        """第一阶段 seen：新 generation 应成功记录"""
        result = file_generation_seen(self.conn, workspace_id=1, rel_path="test.py",
                                       session_id="s1", epoch=1, seq=1)
        self.assertTrue(result)

        row = self.conn.execute(
            "SELECT latest_seen_generation FROM file_generations WHERE workspace_id = 1 AND rel_path = 'test.py'"
        ).fetchone()
        self.assertEqual(row["latest_seen_generation"], "1:1")

    def test_file_generation_seen_stale_rejected(self):
        """第一阶段 seen：stale seq 应被拒绝"""
        # 先写入 epoch=2, seq=5
        file_generation_seen(self.conn, 1, "test.py", "s1", 2, 5)

        # 尝试写入更旧的 epoch=1, seq=10（stale）
        result = file_generation_seen(self.conn, 1, "test.py", "s1", 1, 10)
        self.assertFalse(result, "stale epoch 应被拒绝")

        # 尝试写入同 epoch 但更旧的 seq（stale）
        result = file_generation_seen(self.conn, 1, "test.py", "s1", 2, 3)
        self.assertFalse(result, "stale seq 应被拒绝")

        # latest_seen_generation 不应被覆盖
        row = self.conn.execute(
            "SELECT latest_seen_generation FROM file_generations WHERE workspace_id = 1 AND rel_path = 'test.py'"
        ).fetchone()
        self.assertEqual(row["latest_seen_generation"], "2:5")

    def test_file_generation_committed_success(self):
        """第二阶段 committed：seen 后 committed 应成功"""
        # 先 seen
        file_generation_seen(self.conn, 1, "test.py", "s1", 1, 1)

        # 再 committed
        result = file_generation_committed(self.conn, 1, "test.py", 1, 1)
        self.assertTrue(result)

        row = self.conn.execute(
            "SELECT latest_committed_generation FROM file_generations WHERE workspace_id = 1 AND rel_path = 'test.py'"
        ).fetchone()
        self.assertEqual(row["latest_committed_generation"], "1:1")

    def test_file_generation_committed_stale_rejected(self):
        """第二阶段 committed：stale manifest commit 应被条件 UPDATE 阻止"""
        # seen epoch=1, seq=1
        file_generation_seen(self.conn, 1, "test.py", "s1", 1, 1)

        # 另一个 handler 覆盖 seen 为 epoch=1, seq=2
        file_generation_seen(self.conn, 1, "test.py", "s2", 1, 2)

        # 旧 handler 尝试 committed epoch=1, seq=1（stale）
        result = file_generation_committed(self.conn, 1, "test.py", 1, 1)
        self.assertFalse(result, "stale manifest commit 应被条件 UPDATE 阻止")

        # latest_committed_generation 不应被旧值覆盖
        row = self.conn.execute(
            "SELECT latest_committed_generation FROM file_generations WHERE workspace_id = 1 AND rel_path = 'test.py'"
        ).fetchone()
        self.assertEqual(row["latest_committed_generation"], "")


if __name__ == "__main__":
    unittest.main()
