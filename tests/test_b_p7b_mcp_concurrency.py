#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B-P7b MCP 并发安全测试

验证 MCP Server 长连接场景下：
1. GraphStore 在 MCP 单例 db 上正确懒加载
2. 写入后 _invalidate_graph_store() 触发 WAL checkpoint
3. 重新加载后 GraphStore 能读到刚写入的数据（不读旧 WAL 数据）
4. 多次 write-invalidate-reload 循环数据一致
"""

from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import unittest

# 添加 rust_ext/target/pyinstall 到 PYTHONPATH
_rust_pyinstall = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "rust_ext", "target", "pyinstall"
)
if _rust_pyinstall not in sys.path:
    sys.path.insert(0, _rust_pyinstall)

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _has_rust_ext() -> bool:
    try:
        import callwarden_core  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestMCPGraphStoreConcurrency(unittest.TestCase):
    """MCP Server 长连接场景下的 GraphStore 并发安全"""

    @classmethod
    def setUpClass(cls):
        """创建临时数据库，模拟 MCP Server 的 CodeGraphDB 单例"""
        cls.tmpdir = tempfile.mkdtemp(prefix="cw_mcp_test_")
        cls.db_path = os.path.join(cls.tmpdir, "test.db")
        # 初始化 schema + 测试数据
        from callwarden.db.db import CodeGraphDB
        cls.db = CodeGraphDB(db_path=cls.db_path, workspace_root=_project_root)
        # 插入测试数据
        cls._insert_test_symbols(cls.db, count=10)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.db.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    @staticmethod
    def _insert_test_symbols(db, count=10):
        """插入测试符号和调用关系"""
        ws_id = db._get_active_workspace_id()
        for i in range(count):
            # 插入文件实例
            cur = db.conn.execute(
                "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, mtime, status) "
                "VALUES (?, ?, ?, '', ?, 'active')",
                (ws_id, f"test_file_{i}.py", f"/test/test_file_{i}.py", 0.0)
            )
            fi_id = cur.lastrowid
            # 插入符号
            db.conn.execute(
                "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, qualified_name, start_line, end_line, depth) "
                "VALUES (?, '', ?, 'fn', ?, ?, ?, -1)",
                (fi_id, f"func_{i}", f"module.func_{i}", i * 10, i * 10 + 5)
            )
        db.conn.commit()

    def setUp(self):
        self.db._invalidate_graph_store()

    def test_graph_store_loads_on_first_query(self):
        """MCP 场景：首次查询触发懒加载"""
        self.assertIsNone(self.db._graph_store)
        store = self.db._get_graph_store()
        self.assertIsNotNone(store)
        # 查询应返回结果
        callers = self.db.get_callers("func_0")
        self.assertIsInstance(callers, list)

    def test_wal_checkpoint_on_invalidation(self):
        """写入后失效时触发 WAL checkpoint，再加载能读到新数据"""
        # 1. 加载 GraphStore（缓存旧数据）
        store = self.db._get_graph_store()
        self.assertIsNotNone(store)
        old_stats = store.stats()
        old_count = old_stats["symbol_count"]

        # 2. 写入新符号（通过 Python SQL）
        ws_id = self.db._get_active_workspace_id()
        cur = self.db.conn.execute(
            "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, mtime, status) "
            "VALUES (?, ?, ?, '', ?, 'active')",
            (ws_id, "new_file.py", "/test/new_file.py", 0.0)
        )
        fi_id = cur.lastrowid
        self.db.conn.execute(
            "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, qualified_name, start_line, end_line, depth) "
            "VALUES (?, '', 'new_func', 'fn', 'module.new_func', 1, 5, -1)",
            (fi_id,)
        )
        self.db.conn.commit()

        # 3. 失效缓存（应触发 WAL checkpoint）
        self.db._invalidate_graph_store()
        self.assertIsNone(self.db._graph_store)

        # 4. 重新加载，应包含新符号
        store = self.db._get_graph_store()
        self.assertIsNotNone(store)
        new_stats = store.stats()
        new_count = new_stats["symbol_count"]
        self.assertEqual(new_count, old_count + 1,
                         f"重新加载后符号数应 +1（{old_count} → {new_count}）")

    def test_write_invalidate_reload_cycle(self):
        """多次 write-invalidate-reload 循环数据一致"""
        for cycle in range(3):
            # 加载
            store = self.db._get_graph_store()
            self.assertIsNotNone(store)
            before = store.stats()["symbol_count"]

            # 写入
            ws_id = self.db._get_active_workspace_id()
            cur = self.db.conn.execute(
                "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, mtime, status) "
                "VALUES (?, ?, ?, '', ?, 'active')",
                (ws_id, f"cycle_{cycle}.py", f"/test/cycle_{cycle}.py", 0.0)
            )
            fi_id = cur.lastrowid
            self.db.conn.execute(
                "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, qualified_name, start_line, end_line, depth) "
                "VALUES (?, '', ?, 'fn', ?, 1, 5, -1)",
                (fi_id, f"cycle_func_{cycle}", f"module.cycle_func_{cycle}")
            )
            self.db.conn.commit()

            # 失效 + 重载
            self.db._invalidate_graph_store()
            store = self.db._get_graph_store()
            after = store.stats()["symbol_count"]
            self.assertEqual(after, before + 1,
                             f"cycle {cycle}: 符号数应 +1（{before} → {after}）")

    def test_graceful_degradation_when_rust_fails(self):
        """Rust 加载失败时降级到 SQL"""
        # 模拟加载失败：临时设为无效路径
        original_db_path = self.db.db_path
        self.db._graph_store = None
        self.db.db_path = "/nonexistent/path/test.db"
        try:
            store = self.db._get_graph_store()
            self.assertIsNone(store, "加载失败应返回 None")
            # get_callers 应降级到 SQL 不报错
            callers = self.db.get_callers("func_0")
            self.assertIsInstance(callers, list)
        finally:
            self.db.db_path = original_db_path
            self.db._invalidate_graph_store()


if __name__ == "__main__":
    unittest.main(verbosity=2)
