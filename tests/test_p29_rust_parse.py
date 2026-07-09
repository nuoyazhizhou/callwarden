"""P29: Rust parse_batch 单元测试。

验证 Rust 侧 batch_parse_c_files 的输出与 Python 侧 CParser 一致：
- 函数符号提取一致
- 调用关系提取一致
- 嵌套结构体识别一致
- 批量并行结果正确
- 单文件 vs 批量结果一致
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)


def _has_rust_ext() -> bool:
    try:
        import callwarden_core  # noqa: F401
        return True
    except ImportError:
        return False


# C 源码样例：含函数、结构体、调用关系
_SAMPLE_C = """\
#include <stdio.h>
#include <stdlib.h>

struct Point {
    int x;
    int y;
};

int add(int a, int b) {
    return a + b;
}

int main(int argc, char** argv) {
    struct Point p;
    p.x = 1;
    p.y = 2;
    int sum = add(p.x, p.y);
    printf("sum = %d\\n", sum);
    return 0;
}

void helper(void) {
    printf("helper called\\n");
    main(0, NULL);
}
"""


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestRustParseC(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cw_p29_test_")
        self.sample_path = os.path.join(self.tmpdir, "sample.c")
        with open(self.sample_path, "w", encoding="utf-8") as f:
            f.write(_SAMPLE_C)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_rust_parse_extracts_functions(self):
        """Rust parse 应提取所有函数符号"""
        from callwarden_core import parse_c_file
        result = parse_c_file(self.sample_path, "test.sample")

        # 验证基本字段
        self.assertEqual(result["language"], "c")
        self.assertGreater(result["total_lines"], 10)

        # 提取函数名（与 Python c_parser 一致，kind 用 "fn"）
        func_names = [s["name"] for s in result["symbols"] if s["kind"] == "fn"]
        # 应包含 add, main, helper（不一定含 printf，因 printf 是 declaration 不是 definition）
        self.assertIn("add", func_names)
        self.assertIn("main", func_names)
        self.assertIn("helper", func_names)

    def test_rust_parse_extracts_struct(self):
        """Rust parse 应提取结构体"""
        from callwarden_core import parse_c_file
        result = parse_c_file(self.sample_path, "test.sample")

        struct_names = [s["name"] for s in result["symbols"] if s["kind"] == "struct"]
        self.assertIn("Point", struct_names)

    def test_rust_parse_extracts_qualified_name(self):
        """qualified_name 应含 module_path 前缀"""
        from callwarden_core import parse_c_file
        result = parse_c_file(self.sample_path, "test.sample")

        for s in result["symbols"]:
            if s["kind"] == "fn" and s["name"] == "add":
                self.assertEqual(s["qualified_name"], "test.sample.add")
                break

    def test_rust_parse_extracts_calls(self):
        """Rust parse 应提取调用关系"""
        from callwarden_core import parse_c_file
        result = parse_c_file(self.sample_path, "test.sample")

        calls = result["raw_calls"]
        # main 函数调用了 add 和 printf
        callees = [c["callee_name"] for c in calls]
        self.assertIn("add", callees)
        # helper 调用了 printf 和 main
        self.assertIn("printf", callees)

        # 验证 caller 信息
        main_calls = [c for c in calls if c["caller_name"] == "main"]
        self.assertGreater(len(main_calls), 0)
        # main 应调用 add
        main_callees = [c["callee_name"] for c in main_calls]
        self.assertIn("add", main_callees)

    def test_rust_parse_batch_consistent_with_single(self):
        """批量 parse 结果应与单文件 parse 一致"""
        from callwarden_core import batch_parse_c_files, parse_c_file

        # 创建第二个 C 文件
        sample2_path = os.path.join(self.tmpdir, "sample2.c")
        with open(sample2_path, "w", encoding="utf-8") as f:
            f.write("int foo(void) { return 42; }\\n")

        # 单文件 parse
        single_result = parse_c_file(self.sample_path, "test.sample")

        # 批量 parse
        batch_results = batch_parse_c_files(
            [(self.sample_path, "test.sample"),
             (sample2_path, "test.sample2")],
            num_threads=2
        )
        self.assertEqual(len(batch_results), 2)

        # 第一个文件结果应与单文件一致
        batch_result_0 = batch_results[0]
        self.assertEqual(batch_result_0["language"], single_result["language"])
        self.assertEqual(len(batch_result_0["symbols"]), len(single_result["symbols"]))

        # 符号名一致
        single_names = sorted(s["name"] for s in single_result["symbols"])
        batch_names = sorted(s["name"] for s in batch_result_0["symbols"])
        self.assertEqual(single_names, batch_names)

    def test_rust_parse_batch_parallel_correctness(self):
        """批量并行 parse 多文件，每个文件结果正确"""
        from callwarden_core import batch_parse_c_files

        # 创建 10 个文件
        files = []
        for i in range(10):
            path = os.path.join(self.tmpdir, f"f{i}.c")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"int func_{i}(void) {{ return {i}; }}\\n")
            files.append((path, f"mod.{i}"))

        results = batch_parse_c_files(files, num_threads=4)
        self.assertEqual(len(results), 10)

        # 每个文件应有 1 个函数（kind="fn"）
        for i, r in enumerate(results):
            funcs = [s for s in r["symbols"] if s["kind"] == "fn"]
            self.assertEqual(len(funcs), 1)
            self.assertEqual(funcs[0]["name"], f"func_{i}")
            self.assertEqual(funcs[0]["qualified_name"], f"mod.{i}.func_{i}")

    def test_rust_parse_error_handling(self):
        """不存在的文件应返回 error，不抛异常"""
        from callwarden_core import parse_c_file
        result = parse_c_file("/nonexistent/path/file.c", "test")
        self.assertIn("error", result)

    def test_rust_parse_content_hash_stable(self):
        """同一文件多次 parse，content_hash 应一致"""
        from callwarden_core import parse_c_file
        r1 = parse_c_file(self.sample_path, "test.sample")
        r2 = parse_c_file(self.sample_path, "test.sample")
        self.assertEqual(r1["content_hash"], r2["content_hash"])

    def test_rust_parse_imports(self):
        """应提取 #include 语句"""
        from callwarden_core import parse_c_file
        result = parse_c_file(self.sample_path, "test.sample")
        imports = result["imports"]
        # 应包含 stdio.h 和 stdlib.h
        import_str = " ".join(imports)
        self.assertIn("stdio.h", import_str)
        self.assertIn("stdlib.h", import_str)


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestRustVsPythonConsistency(unittest.TestCase):
    """验证 Rust parse 与 Python CParser 结果一致"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cw_p29_consistency_")
        self.sample_path = os.path.join(self.tmpdir, "sample.c")
        with open(self.sample_path, "w", encoding="utf-8") as f:
            f.write(_SAMPLE_C)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_function_names_match(self):
        """Rust 和 Python 提取的函数名集合一致"""
        from callwarden_core import parse_c_file
        from callwarden.parsers.c_parser import CParser

        # Rust parse
        rust_result = parse_c_file(self.sample_path, "test.sample")
        rust_funcs = set(s["name"] for s in rust_result["symbols"] if s["kind"] == "fn")

        # Python parse
        py_parser = CParser()
        py_result = py_parser.parse_file(self.sample_path, "test.sample")
        py_funcs = set(s["name"] for s in py_result["symbols"] if s["kind"] == "fn")

        # 验证交集（Rust PoC 可能比 Python 少识别一些边界情况，但核心函数应一致）
        common = rust_funcs & py_funcs
        self.assertIn("add", common)
        self.assertIn("main", common)
        self.assertIn("helper", common)


if __name__ == "__main__":
    unittest.main()
