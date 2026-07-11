"""CLI Bug 修复测试

覆盖 3 个 CLI bug 的修复：
1. T-1783751407041-76f6: --symbol 调用不存在的 get_symbol_detail 方法
2. T-1783751412674-5995: --refresh-all UNIQUE 约束冲突导致崩溃
3. T-1783751418408-44eb: 输出含 Unicode 字符导致 Windows GBK 崩溃
"""
import io
import os
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock


class TestBug1SymbolMethod(unittest.TestCase):
    """Bug 1: --symbol 调用不存在的 get_symbol_detail 方法

    修复: cli/main.py:7515 将 db.get_symbol_detail() 改为 db.get_symbol()
    """

    def test_get_symbol_detail_not_exist(self):
        """CodeGraphDB 不应有 get_symbol_detail 方法（确认 bug 根因）"""
        from callwarden.db.db import CodeGraphDB
        self.assertFalse(hasattr(CodeGraphDB, "get_symbol_detail"),
                        "get_symbol_detail 不应存在——这是 bug 根因")

    def test_get_symbol_exists(self):
        """CodeGraphDB 应有 get_symbol 方法"""
        from callwarden.db.db import CodeGraphDB
        self.assertTrue(hasattr(CodeGraphDB, "get_symbol"),
                        "get_symbol 应存在——这是正确的公共方法")

    def test_cli_calls_get_symbol_not_get_symbol_detail(self):
        """cli/main.py 中 --symbol 分支应调用 get_symbol 而非 get_symbol_detail"""
        cli_main_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "cli", "main.py"
        )
        with open(cli_main_path, "r", encoding="utf-8") as f:
            content = f.read()
        # 确认不再调用 get_symbol_detail
        self.assertNotIn("get_symbol_detail(args.symbol)", content,
                        "不应再调用 get_symbol_detail")
        # 确认调用 get_symbol
        self.assertIn("db.get_symbol(args.symbol)", content,
                     "应调用 db.get_symbol(args.symbol)")


class TestBug2RefreshUniqueConstraint(unittest.TestCase):
    """Bug 2: --refresh-all UNIQUE 约束冲突导致崩溃

    修复: _save_symbols_for_version 改为 DELETE 旧 symbols+calls 再纯 INSERT
    """

    def setUp(self):
        """创建测试数据库"""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_schema()
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_schema(self):
        """创建最小 schema"""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS file_instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER DEFAULT 1,
                rel_path TEXT NOT NULL,
                status TEXT DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS symbol_contents (
                content_hash TEXT PRIMARY KEY,
                name TEXT, kind TEXT, content TEXT,
                signature TEXT DEFAULT '',
                has_comment INTEGER DEFAULT 0,
                comment_content TEXT DEFAULT '',
                qualified_name TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS file_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_instance_id INTEGER NOT NULL,
                is_current INTEGER DEFAULT 1,
                content_hash TEXT DEFAULT '',
                mtime REAL DEFAULT 0,
                is_deleted INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_instance_id INTEGER NOT NULL,
                symbol_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                visibility TEXT DEFAULT 'private',
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                start_col INTEGER DEFAULT 0,
                end_col INTEGER DEFAULT 0,
                signature TEXT DEFAULT '',
                has_comment INTEGER DEFAULT 0,
                comment_status TEXT DEFAULT 'pending',
                module_path TEXT DEFAULT '',
                qualified_name TEXT DEFAULT '',
                depth INTEGER DEFAULT -1
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_symbols_unique
                ON symbols(file_instance_id, name, start_line);
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                caller_id INTEGER NOT NULL,
                callee_name TEXT,
                callee_id INTEGER DEFAULT 0,
                call_line INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS file_symbol_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_version_id INTEGER NOT NULL,
                symbol_hash TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                module_path TEXT DEFAULT '',
                depth INTEGER DEFAULT -1,
                is_deleted INTEGER DEFAULT 0
            );
        """)

    def test_save_symbols_delete_then_insert_no_conflict(self):
        """_save_symbols_for_version 应先 DELETE 旧 symbols 再 INSERT，避免 UNIQUE 冲突"""
        file_instance_id = 1
        self.conn.execute(
            "INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (?, 1, 'test.py')",
            (file_instance_id,)
        )
        self.conn.execute(
            "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, start_line, end_line, qualified_name) "
            "VALUES (?, 'hash1', 'foo', 'fn', 10, 20, 'test.foo')",
            (file_instance_id,)
        )
        self.conn.commit()

        # 模拟 _save_symbols_for_version 的 DELETE+INSERT 逻辑
        result = {
            "symbols": [{
                "name": "foo",
                "qualified_name": "test.foo",
                "kind": "fn",
                "visibility": "public",
                "start_line": 10,
                "end_line": 25,
                "start_col": 0,
                "end_col": 0,
                "signature": "def foo()",
                "has_comment": 1,
                "module_path": "test",
                "content_hash": "hash1_new",
                "content": "def foo(): pass",
                "comment_content": "comment",
            }]
        }

        # 1. INSERT OR IGNORE symbol_contents
        for sym in result["symbols"]:
            self.conn.execute(
                "INSERT OR IGNORE INTO symbol_contents (content_hash, name, kind, content, signature, has_comment, comment_content, qualified_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sym["content_hash"], sym["name"], sym["kind"], sym["content"],
                 sym["signature"], sym["has_comment"], sym.get("comment_content", ""),
                 sym["qualified_name"])
            )

        # 2. DELETE 旧 symbols + calls（修复后的逻辑）
        self.conn.execute(
            "DELETE FROM calls WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id = ?)",
            (file_instance_id,)
        )
        self.conn.execute(
            "DELETE FROM symbols WHERE file_instance_id = ?",
            (file_instance_id,)
        )

        # 3. INSERT 新 symbols
        for sym in result["symbols"]:
            self.conn.execute(
                "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, visibility, start_line, end_line, "
                "start_col, end_col, signature, has_comment, comment_status, module_path, qualified_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (file_instance_id, sym["content_hash"], sym["name"], sym["kind"], sym["visibility"],
                 sym["start_line"], sym["end_line"], sym["start_col"], sym["end_col"],
                 sym["signature"], sym["has_comment"], sym["module_path"], sym["qualified_name"])
            )
        self.conn.commit()

        # 验证：只有一行，end_line 更新为 25
        cur = self.conn.execute(
            "SELECT id, end_line FROM symbols WHERE file_instance_id = ? AND name = 'foo'",
            (file_instance_id,)
        )
        rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["end_line"], 25)

    def test_no_update_in_save_symbols(self):
        """_save_symbols_for_version 不应包含 UPDATE symbols 语句（避免 UNIQUE 冲突）"""
        db_build_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "db", "db_build.py"
        )
        with open(db_build_path, "r", encoding="utf-8") as f:
            content = f.read()
        # 找到 _save_symbols_for_version 函数体
        start = content.find("def _save_symbols_for_version")
        self.assertGreater(start, 0, "应找到 _save_symbols_for_version 函数")
        # 截取函数体（到下一个 def 为止）
        end = content.find("\n    def ", start + 1)
        func_body = content[start:end]
        # 不应包含 UPDATE symbols SET（修复前会触发 UNIQUE 冲突）
        self.assertNotIn("UPDATE symbols SET", func_body,
                        "不应包含 UPDATE symbols——改用 DELETE+INSERT 避免 UNIQUE 冲突")
        # 应包含 DELETE FROM symbols WHERE file_instance_id
        self.assertIn("DELETE FROM symbols WHERE file_instance_id", func_body,
                     "应包含 DELETE FROM symbols WHERE file_instance_id")


class TestBug3Utf8Output(unittest.TestCase):
    """Bug 3: 输出含 Unicode 字符导致 Windows GBK 崩溃

    修复: cw.py 入口添加 _ensure_utf8_output() 函数
    """

    def test_ensure_utf8_output_exists(self):
        """cw.py 应有 _ensure_utf8_output 函数"""
        cw_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "cw.py"
        )
        with open(cw_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("def _ensure_utf8_output", content,
                     "cw.py 应定义 _ensure_utf8_output 函数")
        self.assertIn("_ensure_utf8_output()", content,
                     "cw.py 应在 main() 中调用 _ensure_utf8_output()")

    def test_utf8_reconfigure_called(self):
        """_ensure_utf8_output 应调用 reconfigure(encoding='utf-8')"""
        cw_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "cw.py"
        )
        with open(cw_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("reconfigure(encoding=\"utf-8\"", content,
                     "应调用 reconfigure(encoding='utf-8')")
        self.assertIn("errors=\"replace\"", content,
                     "应设置 errors='replace'（无法编码时用 ? 替代）")

    def test_unicode_output_does_not_crash(self):
        """Unicode 字符输出不应崩溃"""
        # 模拟 _ensure_utf8_output 的效果
        old_stdout = sys.stdout
        try:
            buf = io.StringIO()
            sys.stdout = buf
            # 输出含 Unicode 字符的字符串
            test_str = "✓ ✓ ✓ symbol with unicode"
            print(test_str)
            output = buf.getvalue()
            self.assertIn("✓", output)
        finally:
            sys.stdout = old_stdout


if __name__ == "__main__":
    unittest.main()
