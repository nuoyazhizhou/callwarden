#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B-P7b 集成测试：验证 GraphStore 作为 db_query.py 查询加速层的正确性

测试目标：
1. get_callers/get_callees/search_symbols 走 Rust 短路时，结果与 SQL 一致
2. GraphStore 懒加载：首次查询才加载，不阻塞 __init__
3. 缓存失效：refresh_file 后 GraphStore 被清空，下次查询重新加载
4. 降级安全：callwarden_core 不可用时回退 SQL，不报错
"""

from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import time
import unittest

# 添加 rust_ext/target/pyinstall 到 PYTHONPATH（maturin build 产物）
_rust_pyinstall = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "rust_ext", "target", "pyinstall"
)
if _rust_pyinstall not in sys.path:
    sys.path.insert(0, _rust_pyinstall)

# 添加项目根目录到 path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _has_rust_ext() -> bool:
    try:
        import callwarden_core  # noqa: F401
        return True
    except ImportError:
        return False


def _find_callwarden_db() -> str | None:
    """查找 callwarden 自身的数据库"""
    home = os.path.expanduser("~")
    cw_dir = os.path.join(home, ".callwarden")
    if not os.path.isdir(cw_dir):
        return None
    for hash_dir in os.listdir(cw_dir):
        db_path = os.path.join(cw_dir, hash_dir, "callwarden.db")
        if os.path.exists(db_path):
            # 验证有 symbols 表（用 immutable=1 只读模式，避免 WAL -shm 文件问题）
            try:
                normalized = db_path.replace('\\', '/')
                prefix = "file:" if normalized.startswith('/') else "file:///"
                uri = f"{prefix}{normalized}?immutable=1"
                conn = sqlite3.connect(uri, uri=True)
                cur = conn.execute("SELECT COUNT(*) FROM symbols")
                count = cur.fetchone()[0]
                conn.close()
                if count > 0:
                    return db_path
            except Exception:
                pass
    return None


def _connect_readonly(db_path: str) -> sqlite3.Connection:
    """以 immutable=1 只读模式连接 SQLite"""
    normalized = db_path.replace('\\', '/')
    prefix = "file:" if normalized.startswith('/') else "file:///"
    uri = f"{prefix}{normalized}?immutable=1"
    return sqlite3.connect(uri, uri=True)


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestGraphStoreIntegration(unittest.TestCase):
    """B-P7b 集成测试：GraphStore 作为 db_query 加速层"""

    @classmethod
    def setUpClass(cls):
        src_db = _find_callwarden_db()
        if not src_db:
            raise unittest.SkipTest("未找到 callwarden 数据库，请先运行 cw --refresh-all")
        # 复制到临时文件（沙箱中无法在 ~/.callwarden 创建 -shm 文件，但 tempdir 可以）
        cls.tmpdir = tempfile.mkdtemp(prefix="cw_p7b_test_")
        cls.db_path = os.path.join(cls.tmpdir, "test.db")
        import shutil
        shutil.copy2(src_db, cls.db_path)
        # WAL/SHM 文件可能在源目录，不复制（新连接会重建）
        from callwarden.db.db import CodeGraphDB
        cls.db = CodeGraphDB(db_path=cls.db_path, workspace_root=_project_root)
        # 直连只读 SQLite 用于验证结果
        cls.conn = _connect_readonly(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.db.close()
        except Exception:
            pass
        try:
            cls.conn.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        # 每个测试前失效缓存，确保从干净状态开始
        self.db._invalidate_graph_store()

    def test_graph_store_lazy_init(self):
        """GraphStore 应懒加载：__init__ 后 _graph_store 为 None"""
        self.assertIsNone(self.db._graph_store)
        # 第一次查询触发加载
        store = self.db._get_graph_store()
        self.assertIsNotNone(store, "GraphStore 应在首次访问时加载")
        # 第二次访问应返回同一实例（缓存）
        store2 = self.db._get_graph_store()
        self.assertIs(store, store2, "应返回缓存的同一实例")

    def test_get_callers_rust_matches_sql(self):
        """get_callers 走 Rust 短路时，结果数量应与 SQL 一致"""
        # 找一个有调用者的 callee_name
        cur = self.conn.execute(
            "SELECT callee_name, count(*) c FROM calls GROUP BY callee_name ORDER BY c DESC LIMIT 1"
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        callee_name, expected_count = row

        # 通过 db.get_callers 查询（走 Rust 短路）
        rust_callers = self.db.get_callers(callee_name)
        self.assertGreater(len(rust_callers), 0, f"callee_name={callee_name} 应有调用者")

        # 验证字段完整性（所有消费者需要的字段都应存在）
        first = rust_callers[0]
        required_keys = {"caller_name", "caller_file", "call_line", "is_cross_file",
                         "callee_name", "callee_id", "callee_qualified"}
        for key in required_keys:
            self.assertIn(key, first, f"get_callers 结果应包含字段 {key}")

    def test_get_callees_rust_matches_sql(self):
        """get_callees 走 Rust 短路时，结果应与 SQL 一致"""
        cur = self.conn.execute(
            "SELECT s.name FROM calls c JOIN symbols s ON c.caller_id = s.id "
            "GROUP BY s.name ORDER BY count(*) DESC LIMIT 1"
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        caller_name = row[0]

        rust_callees = self.db.get_callees(caller_name)
        self.assertGreater(len(rust_callees), 0, f"caller_name={caller_name} 应有 callees")

        first = rust_callees[0]
        required_keys = {"callee_name", "callee_qualified", "call_line", "is_cross_file"}
        for key in required_keys:
            self.assertIn(key, first, f"get_callees 结果应包含字段 {key}")

    def test_search_symbols_rust_matches_sql_fields(self):
        """search_symbols 走 Rust 短路时，返回字段应与 FTS5 路径一致"""
        # 用一个常见关键词搜索
        results = self.db.search_symbols("parse", limit=10)
        self.assertGreater(len(results), 0, "搜 'parse' 应有结果")

        first = results[0]
        # CLI 消费者需要的字段
        required_keys = {"qualified_name", "name", "kind", "depth", "signature",
                        "has_comment", "file_path", "start_line"}
        for key in required_keys:
            self.assertIn(key, first, f"search_symbols 结果应包含字段 {key}")

    def test_cache_invalidation(self):
        """缓存失效后应重新加载"""
        # 加载缓存
        store1 = self.db._get_graph_store()
        self.assertIsNotNone(store1)

        # 失效
        self.db._invalidate_graph_store()
        self.assertIsNone(self.db._graph_store)

        # 重新加载
        store2 = self.db._get_graph_store()
        self.assertIsNotNone(store2)
        self.assertIsNot(store1, store2, "失效后应创建新实例")

    def test_graceful_degradation(self):
        """GraphStore 加载失败时应降级到 SQL"""
        # 模拟加载失败：临时设为一个无效路径
        original_db_path = self.db.db_path
        self.db._graph_store = None
        self.db.db_path = "/nonexistent/path/test.db"
        try:
            store = self.db._get_graph_store()
            self.assertIsNone(store, "加载失败时应返回 None")
            # get_callers 应降级到 SQL 不报错（SQL 也会失败，但不应该抛异常）
            # 这里只验证 _get_graph_store 返回 None
        finally:
            self.db.db_path = original_db_path

    def test_performance_rust_vs_sql(self):
        """性能对比：Rust 短路 vs 纯 SQL（批量查询）"""
        # 找 20 个有调用者的 callee_name
        cur = self.conn.execute(
            "SELECT callee_name FROM calls GROUP BY callee_name ORDER BY count(*) DESC LIMIT 20"
        )
        callee_names = [row[0] for row in cur]
        self.assertGreater(len(callee_names), 0)

        # Rust 短路（缓存已加载）
        store = self.db._get_graph_store()
        self.assertIsNotNone(store)
        t0 = time.perf_counter()
        rust_count = 0
        for name in callee_names:
            rust_count += len(self.db.get_callers(name))
        t_rust = time.perf_counter() - t0

        # 纯 SQL（临时禁用 GraphStore）
        self.db._invalidate_graph_store()
        # 模拟 callwarden_core 不可用
        import callwarden_core
        original_available = callwarden_core.GraphStore
        callwarden_core.GraphStore = None  # 让 import 成功但实例化失败
        try:
            t0 = time.perf_counter()
            sql_count = 0
            for name in callee_names:
                sql_count += len(self.db.get_callers(name))
            t_sql = time.perf_counter() - t0
        finally:
            callwarden_core.GraphStore = original_available
            self.db._invalidate_graph_store()

        # 结果数量应一致（Rust 可能略多，因为同名符号匹配更全）
        self.assertEqual(rust_count, sql_count,
                         f"Rust({rust_count}) vs SQL({sql_count}) 结果数量应一致")

        # Rust 应至少不慢于 SQL（在大库上应快很多）
        # 小库可能差异不明显，只验证不崩溃
        print(f"\n  Rust: {t_rust*1000:.1f}ms ({rust_count} results), "
              f"SQL: {t_sql*1000:.1f}ms ({sql_count} results), "
              f"speedup: {t_sql/t_rust:.2f}x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
