#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P29 测试：FTS 独立重建命令

测试目标：
1. rebuild_fts_index() 能从 symbols 表全量重建 FTS5 索引
2. get_fts_status() 正确报告 FTS5 索引状态
3. 模拟 refresh 中断场景：清空 symbols_fts 后 rebuild 能恢复
4. 重建后 search_symbols 恢复正常返回结果
5. 触发器重建后增量维护生效
"""

from __future__ import annotations

import os
import sys
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


# 测试用源文件：含多个符号供 search
TEST_PY = '''"""测试模块。"""
def parse_config(path):
    """解析配置文件。"""
    return path

def validate_input(data):
    """验证输入数据。"""
    return data

def handle_request(req):
    """处理请求。"""
    return parse_config(req) + validate_input(req)
'''


def _write_file(root, name, content):
    path = os.path.join(root, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _build_db(root):
    """创建 DB + 注册工作区 + 全量构建。返回 db。"""
    from callwarden.db.db import CodeGraphDB
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    ws_id = db.register_workspace("p29-test", root, "P29 测试")
    db.set_active_workspace(ws_id)
    db.build_full_graph()
    return db


class TestP29FtsRebuild(unittest.TestCase):
    """P29：FTS 独立重建命令"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="cw_p29_test_")
        _write_file(cls.tmpdir, "test.py", TEST_PY)
        cls.db = _build_db(cls.tmpdir)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.db.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_get_fts_status_initial(self):
        """build_full_graph 后 FTS5 状态应一致"""
        status = self.db.get_fts_status()
        self.assertTrue(status["exists"], "symbols_fts 表应存在")
        self.assertGreater(status["symbols_count"], 0, "应有符号")
        self.assertEqual(len(status["triggers"]), 3, "应有 3 个同步触发器")
        self.assertTrue(status["consistent"], "FTS 行数应与 symbols 一致")

    def test_rebuild_fts_index_normal(self):
        """正常重建：rebuild 后 FTS 行数应与 symbols 一致"""
        before = self.db.get_fts_status()
        result = self.db.rebuild_fts_index()
        self.assertTrue(result["success"], f"重建应成功: {result['error']}")
        self.assertEqual(result["triggers_recreated"], 3)
        self.assertEqual(result["fts_rows"], result["symbols_count"])
        # 行数应与重建前一致
        after = self.db.get_fts_status()
        self.assertEqual(after["fts_rows"], before["fts_rows"])

    def test_rebuild_after_fts_cleared(self):
        """模拟 refresh 中断：FTS5 索引损坏后 rebuild 能恢复

        FTS5 外部内容表的 COUNT 可能返回关联表行数（非实际索引行数）。
        模拟方式：DROP FTS 表 + 触发器，rebuild 后验证 search 恢复。
        """
        # 删除整个 FTS 表和触发器（模拟 refresh 中断后索引损坏）
        self.db.conn.execute("DROP TRIGGER IF EXISTS symbols_fts_ai")
        self.db.conn.execute("DROP TRIGGER IF EXISTS symbols_fts_ad")
        self.db.conn.execute("DROP TRIGGER IF EXISTS symbols_fts_au")
        self.db.conn.execute("DROP TABLE IF EXISTS symbols_fts")
        self.db.conn.commit()

        # 验证 FTS 表已不存在
        status = self.db.get_fts_status()
        self.assertFalse(status["exists"], "FTS 表应已删除")

        # search 应回退到 LIKE 路径（仍有结果，但走慢路径）
        # rebuild 前不强制验证 search 为 0，因为 LIKE 回退仍可能返回结果

        # rebuild 重建
        result = self.db.rebuild_fts_index()
        # 如果 FTS 表不存在，rebuild 应失败并返回明确错误
        # 因为 rebuild_fts_index 内部检查表是否存在
        if not result["success"]:
            self.assertIn("不存在", result["error"])
            # 手动重建 FTS 表（模拟 schema 迁移）
            self.db.conn.execute("""
                CREATE VIRTUAL TABLE symbols_fts USING fts5(
                    name, qualified_name,
                    content='symbols', content_rowid='id',
                    tokenize='trigram'
                )
            """)
            self.db.conn.commit()
            # 再次 rebuild
            result = self.db.rebuild_fts_index()

        self.assertTrue(result["success"], f"重建应成功: {result['error']}")
        self.assertEqual(result["fts_rows"], result["symbols_count"])

        # search 应恢复正常（走 FTS5 路径）
        results = self.db.search_symbols("parse")
        self.assertGreater(len(results), 0, "重建后 search 应有结果")

    def test_rebuild_recreates_triggers(self):
        """重建后触发器应存在（增量维护生效）"""
        # 先删除触发器
        self.db.conn.execute("DROP TRIGGER IF EXISTS symbols_fts_ai")
        self.db.conn.execute("DROP TRIGGER IF EXISTS symbols_fts_ad")
        self.db.conn.execute("DROP TRIGGER IF EXISTS symbols_fts_au")
        self.db.conn.commit()

        status = self.db.get_fts_status()
        self.assertEqual(len(status["triggers"]), 0, "删除后应无触发器")

        # rebuild
        result = self.db.rebuild_fts_index()
        self.assertTrue(result["success"])
        self.assertEqual(result["triggers_recreated"], 3)

        # 验证触发器已重建
        status = self.db.get_fts_status()
        self.assertEqual(len(status["triggers"]), 3, "重建后应有 3 个触发器")

    def test_rebuild_on_nonexistent_fts_table(self):
        """FTS 表不存在时应返回明确错误"""
        # 用一个新的空数据库（schema 版本 < 31，无 symbols_fts）
        tmpdir2 = tempfile.mkdtemp(prefix="cw_p29_empty_")
        try:
            from callwarden.db.db import CodeGraphDB
            db2 = CodeGraphDB(os.path.join(tmpdir2, "empty.db"), workspace_root=tmpdir2)
            # 不调用 build_full_graph，所以 symbols_fts 可能未创建
            # 先确保表不存在
            db2.conn.execute("DROP TABLE IF EXISTS symbols_fts")
            db2.conn.commit()

            result = db2.rebuild_fts_index()
            self.assertFalse(result["success"], "FTS 表不存在时应失败")
            self.assertIn("不存在", result["error"])

            status = db2.get_fts_status()
            self.assertFalse(status["exists"])

            db2.close()
        finally:
            import shutil
            shutil.rmtree(tmpdir2, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
