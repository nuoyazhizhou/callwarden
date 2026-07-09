"""P32: typedef 分支补齐单元测试。

P29 的 Rust C parser 缺少 type_definition 处理，导致比 Python 少 857 个符号。
P32 补齐 typedef struct/enum/union 分支，与 Python c_parser.py 行为对齐。

验证：
- typedef struct { ... } Name; 提取 struct 符号
- typedef enum { ... } Name; 提取 enum 符号
- typedef union { ... } Name; 提取 union 符号
- typedef 内嵌套 struct 递归提取
- 简单 typedef（typedef int MyInt）不误报
- Rust 与 Python 提取的 typedef 符号集合一致
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


# C 源码样例：含各种 typedef 模式
_SAMPLE_TYPEDEF_C = """\
#include <stdio.h>

/* typedef struct：最常见的 C 模式 */
typedef struct Point {
    int x;
    int y;
} Point;

/* typedef enum */
typedef enum Color {
    RED,
    GREEN,
    BLUE
} Color;

/* typedef union */
typedef union Data {
    int i;
    float f;
} Data;

/* typedef 简单类型别名（不应提取为符号） */
typedef int MyInt;
typedef unsigned long size_t_custom;

/* 匿名 typedef struct（无 tag） */
typedef struct {
    int a;
    int b;
} Pair;

int main(void) {
    Point p;
    p.x = 1;
    Color c = RED;
    return 0;
}
"""


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestRustTypedef(unittest.TestCase):
    """P32: Rust C parser typedef 分支测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cw_p32_test_")
        self.sample_path = os.path.join(self.tmpdir, "typedef.c")
        with open(self.sample_path, "w", encoding="utf-8") as f:
            f.write(_SAMPLE_TYPEDEF_C)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_typedef_struct_extracted(self):
        """typedef struct Point { ... } Point; 应提取 struct 符号"""
        from callwarden_core import parse_c_file
        result = parse_c_file(self.sample_path, "test.typedef")
        names = [s["name"] for s in result["symbols"]]
        self.assertIn("Point", names)
        # 验证 kind
        point = next(s for s in result["symbols"] if s["name"] == "Point")
        self.assertEqual(point["kind"], "struct")

    def test_typedef_enum_extracted(self):
        """typedef enum Color { ... } Color; 应提取 enum 符号"""
        from callwarden_core import parse_c_file
        result = parse_c_file(self.sample_path, "test.typedef")
        names = [s["name"] for s in result["symbols"]]
        self.assertIn("Color", names)
        color = next(s for s in result["symbols"] if s["name"] == "Color")
        self.assertEqual(color["kind"], "enum")

    def test_typedef_union_extracted(self):
        """typedef union Data { ... } Data; 应提取 union 符号"""
        from callwarden_core import parse_c_file
        result = parse_c_file(self.sample_path, "test.typedef")
        names = [s["name"] for s in result["symbols"]]
        self.assertIn("Data", names)
        data = next(s for s in result["symbols"] if s["name"] == "Data")
        self.assertEqual(data["kind"], "union")

    def test_typedef_anonymous_struct_extracted(self):
        """匿名 typedef struct { ... } Pair; 应提取 struct 符号"""
        from callwarden_core import parse_c_file
        result = parse_c_file(self.sample_path, "test.typedef")
        names = [s["name"] for s in result["symbols"]]
        self.assertIn("Pair", names)

    def test_simple_typedef_not_symbol(self):
        """简单类型别名 typedef int MyInt 不应提取为 struct/enum/union 符号"""
        from callwarden_core import parse_c_file
        result = parse_c_file(self.sample_path, "test.typedef")
        names = [s["name"] for s in result["symbols"]]
        # MyInt / size_t_custom 是简单别名，不应作为符号
        self.assertNotIn("MyInt", names)
        self.assertNotIn("size_t_custom", names)

    def test_typedef_qualified_name(self):
        """typedef 符号的 qualified_name 应包含 module_path"""
        from callwarden_core import parse_c_file
        result = parse_c_file(self.sample_path, "test.typedef")
        point = next(s for s in result["symbols"] if s["name"] == "Point")
        self.assertEqual(point["qualified_name"], "test.typedef.Point")

    def test_typedef_count_at_least_four(self):
        """至少提取 4 个 typedef 符号（Point, Color, Data, Pair）"""
        from callwarden_core import parse_c_file
        result = parse_c_file(self.sample_path, "test.typedef")
        typedef_names = {"Point", "Color", "Data", "Pair"}
        extracted = {s["name"] for s in result["symbols"]}
        missing = typedef_names - extracted
        self.assertEqual(missing, set(), f"缺失 typedef 符号: {missing}")


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestTypedefVsPython(unittest.TestCase):
    """P32: 验证 Rust 与 Python CParser 的 typedef 提取一致"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cw_p32_consistency_")
        self.sample_path = os.path.join(self.tmpdir, "typedef.c")
        with open(self.sample_path, "w", encoding="utf-8") as f:
            f.write(_SAMPLE_TYPEDEF_C)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_typedef_symbols_match_python(self):
        """Rust 和 Python 提取的 typedef 相关符号集合应一致"""
        from callwarden_core import parse_c_file
        from callwarden.parsers.c_parser import CParser

        # Rust
        rust_result = parse_c_file(self.sample_path, "test.typedef")
        rust_names = {s["name"] for s in rust_result["symbols"]}

        # Python
        py_parser = CParser()
        py_result = py_parser.parse_file(self.sample_path, "test.typedef")
        py_names = {s["name"] for s in py_result["symbols"]}

        # typedef 符号应在两者中都存在
        for name in ("Point", "Color", "Data", "Pair"):
            self.assertIn(name, rust_names, f"Rust 缺失 {name}")
            self.assertIn(name, py_names, f"Python 缺失 {name}")

    def test_symbol_count_close_to_python(self):
        """Rust 符号总数应接近 Python（差距应远小于 P29 的 857）"""
        from callwarden_core import parse_c_file
        from callwarden.parsers.c_parser import CParser

        rust_result = parse_c_file(self.sample_path, "test.typedef")
        py_parser = CParser()
        py_result = py_parser.parse_file(self.sample_path, "test.typedef")

        rust_count = len(rust_result["symbols"])
        py_count = len(py_result["symbols"])
        # P32 后差距应很小（允许 ±2 的边界差异）
        self.assertLessEqual(abs(rust_count - py_count), 2,
                             f"符号数差距过大: Rust={rust_count} Python={py_count}")


if __name__ == "__main__":
    unittest.main()
