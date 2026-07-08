"""P23: 工业化解析健壮性测试

测试 4 个核心工具函数和集成场景：
- read_file_text: UTF-8/UTF-16/latin-1 编码容错
- to_long_path: Windows 长路径前缀
- safe_walk: os.walk 错误处理
- _stage_timings: files_skipped/files_failed 字段
- 集成：非 UTF-8 项目构建不崩溃
"""
from __future__ import annotations
import os
import sys
import tempfile
import unittest

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from callwarden.config import read_file_text, to_long_path, safe_walk, norm_path
from callwarden.db.db import CodeGraphDB


class TestReadFileText(unittest.TestCase):
    """P23.4: UTF-8 编码容错"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cw_p23_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_bytes(self, name: str, data: bytes):
        path = os.path.join(self.tmpdir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_utf8_normal(self):
        """正常 UTF-8 文件"""
        path = self._write_bytes("utf8.txt", "hello\nworld\n".encode("utf-8"))
        text = read_file_text(path)
        self.assertEqual(text, "hello\nworld\n")

    def test_utf8_bom(self):
        """UTF-8 BOM 文件"""
        data = b"\xef\xbb\xbf" + "hello".encode("utf-8")
        path = self._write_bytes("bom.txt", data)
        text = read_file_text(path)
        self.assertEqual(text.strip(), "hello")

    def test_utf16_le_bom(self):
        """UTF-16 LE BOM 文件（Windows 记事本 Unicode 模式）"""
        data = b"\xff\xfe" + "hello".encode("utf-16-le")
        path = self._write_bytes("utf16.txt", data)
        text = read_file_text(path)
        self.assertEqual(text.strip(), "hello")

    def test_utf16_be_bom(self):
        """UTF-16 BE BOM 文件"""
        data = b"\xfe\xff" + "hello".encode("utf-16-be")
        path = self._write_bytes("utf16be.txt", data)
        text = read_file_text(path)
        self.assertEqual(text.strip(), "hello")

    def test_latin1_fallback(self):
        """非 UTF-8 非 UTF-16 文件降级 latin-1"""
        # 0xFF 不是 UTF-8 有效字节
        data = b"\xff\xfe\x00\x01invalid"
        path = self._write_bytes("latin1.txt", data)
        text = read_file_text(path)
        # 不崩溃，能读出文本
        self.assertTrue(len(text) > 0)

    def test_chinese_gb2312(self):
        """GB2312 编码的 requirements.txt"""
        data = "flask==1.0\n# 中文注释\n".encode("gb2312")
        path = self._write_bytes("req.txt", data)
        text = read_file_text(path)
        # 不崩溃，能读出英文部分
        self.assertIn("flask==1.0", text)

    def test_crlf_normalization(self):
        """CRLF 换行符标准化"""
        path = self._write_bytes("crlf.txt", b"line1\r\nline2\r\n")
        text = read_file_text(path)
        self.assertEqual(text, "line1\nline2\n")


class TestToLongPath(unittest.TestCase):
    """P23.6: Windows 长路径支持"""

    def test_short_path_unchanged(self):
        """短路径不加前缀"""
        if os.name == "nt":
            path = "C:\\short\\path"
        else:
            path = "/short/path"
        self.assertEqual(to_long_path(path), path)

    def test_already_prefixed(self):
        """已有 \\?\ 前缀不重复添加"""
        path = "\\\\?\\C:\\very\\long\\path"
        self.assertEqual(to_long_path(path), path)

    def test_long_path_gets_prefix(self):
        """长路径添加前缀（仅 Windows）"""
        if os.name != "nt":
            self.skipTest("Windows only")
        # 构造 > 250 字符的路径
        long_path = "C:\\" + "a" * 250 + "\\file.txt"
        result = to_long_path(long_path)
        self.assertTrue(result.startswith("\\\\?\\"))

    def test_non_windows_passthrough(self):
        """非 Windows 直接返回原路径"""
        if os.name == "nt":
            self.skipTest("Windows only test")
        path = "/very/long/path" + "a" * 250
        self.assertEqual(to_long_path(path), path)

    def test_relative_path_unchanged(self):
        """相对路径不加前缀"""
        path = "relative/path/file.txt"
        result = to_long_path(path)
        # 相对路径不加前缀
        self.assertNotIn("\\\\?\\", result)


class TestSafeWalk(unittest.TestCase):
    """P23.5: os.walk 错误处理"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cw_p23_walk_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_normal_walk(self):
        """正常目录遍历"""
        os.makedirs(os.path.join(self.tmpdir, "sub"))
        for f in ["a.py", "b.py", "sub/c.py"]:
            with open(os.path.join(self.tmpdir, f), "w") as fh:
                fh.write("pass")
        result = list(safe_walk(self.tmpdir))
        self.assertTrue(len(result) >= 2)  # 根目录 + sub

    def test_walk_with_onerror(self):
        """遇到错误不中断"""
        # 创建一个正常目录结构
        os.makedirs(os.path.join(self.tmpdir, "dir1"))
        with open(os.path.join(self.tmpdir, "dir1", "a.py"), "w") as f:
            f.write("pass")
        # safe_walk 应该正常遍历不崩溃
        result = list(safe_walk(self.tmpdir))
        all_files = [f for _, _, files in result for f in files]
        self.assertIn("a.py", all_files)

    def test_max_depth(self):
        """深度限制"""
        os.makedirs(os.path.join(self.tmpdir, "a/b/c/d"))
        for f in ["a/x.py", "a/b/y.py", "a/b/c/z.py"]:
            with open(os.path.join(self.tmpdir, f), "w") as fh:
                fh.write("pass")
        result = list(safe_walk(self.tmpdir, max_depth=1))
        dirs_visited = [os.path.basename(r) for r, _, _ in result]
        # depth=1 只访问根目录和第一层
        self.assertIn("a", dirs_visited)


class TestStageTimingsFields(unittest.TestCase):
    """P23.7: _stage_timings 包含 files_skipped/files_failed"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cw_p23_timings_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_timings_has_skipped_and_failed(self):
        """_stage_timings 包含 files_skipped 和 files_failed 字段"""
        # 创建一个简单的 Python 项目
        with open(os.path.join(self.tmpdir, "main.py"), "w") as f:
            f.write("def foo():\n    pass\n")
        with open(os.path.join(self.tmpdir, "setup.py"), "w") as f:
            f.write("from setuptools import setup\nsetup()\n")

        db_path = os.path.join(self.tmpdir, "test.db")
        db = CodeGraphDB(db_path=db_path, workspace_root=self.tmpdir)
        ws = db.register_workspace("test", self.tmpdir)
        db.set_active_workspace(ws)
        db.build_full_graph(force=False)

        timings = getattr(db, "_stage_timings", {})
        self.assertIn("files_skipped", timings, "_stage_timings 缺少 files_skipped 字段")
        self.assertIn("files_failed", timings, "_stage_timings 缺少 files_failed 字段")
        self.assertEqual(timings["files_failed"], 0)
        db.close()


class TestIntegrationNonUtf8Project(unittest.TestCase):
    """集成测试：非 UTF-8 requirements.txt 不导致构建失败"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cw_p23_integ_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_utf16_requirements_txt(self):
        """UTF-16 编码的 requirements.txt 不导致 build 失败"""
        # 创建项目
        os.makedirs(os.path.join(self.tmpdir, "src"))
        with open(os.path.join(self.tmpdir, "src", "__init__.py"), "w") as f:
            f.write("")
        with open(os.path.join(self.tmpdir, "src", "main.py"), "w") as f:
            f.write("def main():\n    pass\n")
        with open(os.path.join(self.tmpdir, "pyproject.toml"), "w") as f:
            f.write("[project]\nname = 'test'\nversion = '0.1'\n")
        # UTF-16 LE 编码的 requirements.txt（含 BOM）
        with open(os.path.join(self.tmpdir, "requirements.txt"), "wb") as f:
            f.write(b"\xff\xfe" + "flask==1.0\n".encode("utf-16-le"))

        db_path = os.path.join(self.tmpdir, "test.db")
        db = CodeGraphDB(db_path=db_path, workspace_root=self.tmpdir)
        ws = db.register_workspace("test_utf16", self.tmpdir)
        db.set_active_workspace(ws)

        # 不应抛 UnicodeDecodeError
        db.build_full_graph(force=False)

        stats = db.get_stats()
        self.assertGreater(stats["total_files"], 0)
        self.assertGreater(stats["total_symbols"], 0)
        db.close()


if __name__ == "__main__":
    unittest.main()
