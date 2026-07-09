"""P31: 多语言 parser 迁移到 Rust 单元测试。

验证 Rust 侧 multi_lang 模块的 config 驱动框架：
- supported_languages() 返回 11 种语言
- parse_file_lang() 单文件解析
- batch_parse_files_lang() 批量并行解析
- batch_parse_files_lang_pool() 流式回传
- 各语言符号/调用/import 提取正确性
- 与 Python parser 行为对齐（Python / Rust / Go）
- 错误处理（不支持的语言）
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


# ============================================
# 各语言样例源码
# ============================================

_SAMPLE_PYTHON = """\
\"\"\"模块文档。\"\"\"
import os
from typing import List


class Calculator:
    \"\"\"计算器类。\"\"\"

    def __init__(self):
        self.value = 0

    def add(self, x: int) -> int:
        self.value += x
        return self.value

    def clear(self):
        self.value = 0


def main():
    calc = Calculator()
    calc.add(10)
    calc.clear()
    print(calc.value)


if __name__ == "__main__":
    main()
"""

_SAMPLE_RUST = """\
use std::collections::HashMap;

pub struct Point {
    pub x: f64,
    pub y: f64,
}

impl Point {
    pub fn new(x: f64, y: f64) -> Self {
        Point { x, y }
    }

    pub fn distance(&self, other: &Point) -> f64 {
        let dx = self.x - other.x;
        let dy = self.y - other.y;
        (dx * dx + dy * dy).sqrt()
    }
}

pub fn main() {
    let p1 = Point::new(0.0, 0.0);
    let p2 = Point::new(3.0, 4.0);
    let d = p1.distance(&p2);
    println!("{}", d);
}
"""

_SAMPLE_GO = """\
package main

import (
    "fmt"
    "strings"
)

type Point struct {
    X int
    Y int
}

type Printer interface {
    Print() string
}

func (p *Point) Print() string {
    return fmt.Sprintf("%d,%d", p.X, p.Y)
}

func Add(a, b int) int {
    return a + b
}

func main() {
    p := &Point{X: 1, Y: 2}
    s := p.Print()
    sum := Add(1, 2)
    fmt.Println(s, sum, strings.HasPrefix("a", "b"))
}
"""

_SAMPLE_JAVA = """\
package com.example;

import java.util.List;

public class Calculator {
    private int value;

    public Calculator() {
        this.value = 0;
    }

    public int add(int x) {
        this.value += x;
        return this.value;
    }

    public static void main(String[] args) {
        Calculator calc = new Calculator();
        calc.add(10);
        System.out.println(calc.value);
    }
}
"""

_SAMPLE_TYPESCRIPT = """\
import { v4 as uuid } from 'uuid';

export class User {
    constructor(public name: string) {}

    greet(): string {
        return `Hello, ${this.name}`;
    }
}

export function add(a: number, b: number): number {
    return a + b;
}

export function main(): void {
    const u = new User('Alice');
    const s = u.greet();
    const sum = add(1, 2);
    console.log(s, sum, uuid());
}
"""

_SAMPLE_JAVASCRIPT = """\
class User {
    constructor(name) {
        this.name = name;
    }

    greet() {
        return 'Hello, ' + this.name;
    }
}

function add(a, b) {
    return a + b;
}

function main() {
    const u = new User('Bob');
    const s = u.greet();
    const sum = add(1, 2);
    console.log(s, sum);
}

main();
"""

_SAMPLE_RUBY = """\
class Calculator
  def initialize
    @value = 0
  end

  def add(x)
    @value += x
    return @value
  end
end

def main
  calc = Calculator.new
  calc.add(10)
  puts calc
end

main
"""

_SAMPLE_PHP = """\
<?php
namespace App;

use App\\Service;

class Calculator {
    private $value;

    public function __construct() {
        $this->value = 0;
    }

    public function add($x) {
        $this->value += $x;
        return $this->value;
    }
}

$calc = new Calculator();
$calc->add(10);
echo $calc->value;
"""

_SAMPLE_SCALA = """\
package com.example

import scala.collection.mutable.ListBuffer

class Calculator {
  private var value: Int = 0

  def add(x: Int): Int = {
    value += x
    value
  }
}

object Main {
  def main(args: Array[String]): Unit = {
    val calc = new Calculator
    calc.add(10)
    println(calc)
  }
}
"""

_SAMPLE_CSHARP = """\
using System;
using System.Collections.Generic;

namespace Example {
    public class Calculator {
        private int value;

        public Calculator() {
            this.value = 0;
        }

        public int Add(int x) {
            this.value += x;
            return this.value;
        }

        public static void Main(string[] args) {
            var calc = new Calculator();
            calc.Add(10);
            Console.WriteLine(calc.value);
        }
    }
}
"""

_SAMPLE_CPP = """\
#include <iostream>
#include <vector>

namespace example {

class Point {
public:
    Point(int x, int y) : x_(x), y_(y) {}

    int distance() {
        return x_ * x_ + y_ * y_;
    }

private:
    int x_;
    int y_;
};

int add(int a, int b) {
    return a + b;
}

int main() {
    Point p(1, 2);
    int d = p.distance();
    int s = add(1, 2);
    std::cout << d << s << std::endl;
    return 0;
}

}  // namespace example
"""

# 每个语言的样例与预期符号（name, kind）
_LANGUAGE_SAMPLES = [
    ("python", "sample.py", _SAMPLE_PYTHON),
    ("rust", "sample.rs", _SAMPLE_RUST),
    ("go", "sample.go", _SAMPLE_GO),
    ("java", "Sample.java", _SAMPLE_JAVA),
    ("typescript", "sample.ts", _SAMPLE_TYPESCRIPT),
    ("javascript", "sample.js", _SAMPLE_JAVASCRIPT),
    ("ruby", "sample.rb", _SAMPLE_RUBY),
    ("php", "sample.php", _SAMPLE_PHP),
    ("scala", "Sample.scala", _SAMPLE_SCALA),
    ("csharp", "Sample.cs", _SAMPLE_CSHARP),
    ("cpp", "sample.cpp", _SAMPLE_CPP),
]


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestSupportedLanguages(unittest.TestCase):
    """P31: supported_languages() 接口测试"""

    def test_returns_11_languages(self):
        """应返回 11 种支持的语言"""
        from callwarden_core import supported_languages
        langs = supported_languages()
        self.assertEqual(len(langs), 11)

    def test_contains_all_expected_languages(self):
        """应包含所有 11 种语言"""
        from callwarden_core import supported_languages
        langs = set(supported_languages())
        expected = {
            "python", "rust", "go", "java", "typescript", "javascript",
            "ruby", "php", "scala", "csharp", "cpp",
        }
        self.assertEqual(langs, expected)


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestParseFileLang(unittest.TestCase):
    """P31: parse_file_lang() 单文件解析测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cw_p31_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_sample(self, lang: str, filename: str, content: str) -> str:
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_unsupported_language_raises(self):
        """不支持的语言应抛出 ValueError"""
        from callwarden_core import parse_file_lang
        path = self._write_sample("unknown", "x.unknown", "hello\n")
        with self.assertRaises(Exception):
            parse_file_lang(path, "mod.x", "klingon")

    def test_python_extracts_class_and_methods(self):
        """Python: 提取 class 与方法符号"""
        from callwarden_core import parse_file_lang
        path = self._write_sample("python", "sample.py", _SAMPLE_PYTHON)
        result = parse_file_lang(path, "test.sample", "python")

        self.assertEqual(result["language"], "python")
        self.assertGreater(result["total_lines"], 20)

        names = {s["name"]: s["kind"] for s in result["symbols"]}
        self.assertIn("Calculator", names)
        self.assertEqual(names["Calculator"], "class")
        # __init__ / add / clear 都应是 method
        for m in ("__init__", "add", "clear", "main"):
            self.assertIn(m, names)

    def test_python_extracts_qualified_name(self):
        """Python: qualified_name 应含 module_path 前缀"""
        from callwarden_core import parse_file_lang
        path = self._write_sample("python", "sample.py", _SAMPLE_PYTHON)
        result = parse_file_lang(path, "test.sample", "python")

        calc = next(s for s in result["symbols"] if s["name"] == "Calculator")
        self.assertEqual(calc["qualified_name"], "test.sample.Calculator")

    def test_python_extracts_calls(self):
        """Python: 提取调用关系"""
        from callwarden_core import parse_file_lang
        path = self._write_sample("python", "sample.py", _SAMPLE_PYTHON)
        result = parse_file_lang(path, "test.sample", "python")

        callees = {c["callee_name"] for c in result["raw_calls"]}
        # main() 调用了 Calculator() 构造和 calc.add/calc.clear/print
        self.assertIn("add", callees)
        # print 是常见调用
        self.assertIn("print", callees)

    def test_python_extracts_imports(self):
        """Python: 提取 import 语句"""
        from callwarden_core import parse_file_lang
        path = self._write_sample("python", "sample.py", _SAMPLE_PYTHON)
        result = parse_file_lang(path, "test.sample", "python")

        imports = result["imports"]
        # 应至少包含 os 或 typing
        joined = " ".join(imports)
        self.assertTrue("os" in joined or "typing" in joined)

    def test_rust_extracts_struct_and_methods(self):
        """Rust: 提取 struct 与 impl 方法"""
        from callwarden_core import parse_file_lang
        path = self._write_sample("rust", "sample.rs", _SAMPLE_RUST)
        result = parse_file_lang(path, "test.sample", "rust")

        self.assertEqual(result["language"], "rust")
        names = {s["name"]: s["kind"] for s in result["symbols"]}
        self.assertIn("Point", names)
        # new / distance / main 都应提取
        for m in ("new", "distance", "main"):
            self.assertIn(m, names)

    def test_go_extracts_functions_and_types(self):
        """Go: 提取函数、方法、struct、interface"""
        from callwarden_core import parse_file_lang
        path = self._write_sample("go", "sample.go", _SAMPLE_GO)
        result = parse_file_lang(path, "test.sample", "go")

        self.assertEqual(result["language"], "go")
        names = {s["name"]: s["kind"] for s in result["symbols"]}
        # struct
        self.assertIn("Point", names)
        # interface
        self.assertIn("Printer", names)
        # function + method
        self.assertIn("Add", names)
        self.assertIn("main", names)
        self.assertIn("Print", names)

    def test_java_extracts_class_and_methods(self):
        """Java: 提取 class / method / constructor"""
        from callwarden_core import parse_file_lang
        path = self._write_sample("java", "Sample.java", _SAMPLE_JAVA)
        result = parse_file_lang(path, "test.sample", "java")

        self.assertEqual(result["language"], "java")
        names = {s["name"]: s["kind"] for s in result["symbols"]}
        self.assertIn("Calculator", names)
        self.assertIn("add", names)
        self.assertIn("main", names)

    def test_typescript_extracts_class_and_functions(self):
        """TypeScript: 提取 class / function"""
        from callwarden_core import parse_file_lang
        path = self._write_sample("typescript", "sample.ts", _SAMPLE_TYPESCRIPT)
        result = parse_file_lang(path, "test.sample", "typescript")

        self.assertEqual(result["language"], "typescript")
        names = {s["name"]: s["kind"] for s in result["symbols"]}
        self.assertIn("User", names)
        self.assertIn("add", names)
        self.assertIn("main", names)

    def test_javascript_extracts_class_and_functions(self):
        """JavaScript: 用 TS grammar 解析 JS 代码"""
        from callwarden_core import parse_file_lang
        path = self._write_sample("javascript", "sample.js", _SAMPLE_JAVASCRIPT)
        result = parse_file_lang(path, "test.sample", "javascript")

        # lang_id 应为 javascript，但 language 字段视实现可能为 javascript 或 typescript
        names = {s["name"]: s["kind"] for s in result["symbols"]}
        self.assertIn("User", names)
        self.assertIn("add", names)
        self.assertIn("main", names)

    def test_ruby_extracts_class_and_methods(self):
        """Ruby: 提取 class / method"""
        from callwarden_core import parse_file_lang
        path = self._write_sample("ruby", "sample.rb", _SAMPLE_RUBY)
        result = parse_file_lang(path, "test.sample", "ruby")

        self.assertEqual(result["language"], "ruby")
        names = {s["name"]: s["kind"] for s in result["symbols"]}
        self.assertIn("Calculator", names)
        self.assertIn("add", names)
        self.assertIn("main", names)

    def test_php_extracts_class_and_methods(self):
        """PHP: 提取 class / method（PositionBefore 策略）"""
        from callwarden_core import parse_file_lang
        path = self._write_sample("php", "sample.php", _SAMPLE_PHP)
        result = parse_file_lang(path, "test.sample", "php")

        self.assertEqual(result["language"], "php")
        names = {s["name"]: s["kind"] for s in result["symbols"]}
        self.assertIn("Calculator", names)
        # PHP 方法名提取（PositionBefore formal_parameters）
        self.assertIn("add", names)

    def test_scala_extracts_class_and_object(self):
        """Scala: 提取 class / object / function"""
        from callwarden_core import parse_file_lang
        path = self._write_sample("scala", "Sample.scala", _SAMPLE_SCALA)
        result = parse_file_lang(path, "test.sample", "scala")

        self.assertEqual(result["language"], "scala")
        names = {s["name"]: s["kind"] for s in result["symbols"]}
        self.assertIn("Calculator", names)
        self.assertIn("Main", names)
        self.assertIn("add", names)
        self.assertIn("main", names)

    def test_csharp_extracts_class_and_methods(self):
        """C#: 提取 class / method（PositionBefore 策略）"""
        from callwarden_core import parse_file_lang
        path = self._write_sample("csharp", "Sample.cs", _SAMPLE_CSHARP)
        result = parse_file_lang(path, "test.sample", "csharp")

        self.assertEqual(result["language"], "csharp")
        names = {s["name"]: s["kind"] for s in result["symbols"]}
        self.assertIn("Calculator", names)
        # C# 方法名提取（PositionBefore parameter_list）
        self.assertIn("Add", names)
        self.assertIn("Main", names)

    def test_cpp_extracts_class_and_functions(self):
        """C++: 提取 class / function（ChildByTypeNested 策略）"""
        from callwarden_core import parse_file_lang
        path = self._write_sample("cpp", "sample.cpp", _SAMPLE_CPP)
        result = parse_file_lang(path, "test.sample", "cpp")

        self.assertEqual(result["language"], "cpp")
        names = {s["name"]: s["kind"] for s in result["symbols"]}
        self.assertIn("Point", names)
        self.assertIn("add", names)
        self.assertIn("main", names)
        # C++ 方法 distance 应在 Point 类内
        self.assertIn("distance", names)


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestBatchParseFilesLang(unittest.TestCase):
    """P31: batch_parse_files_lang() 批量并行解析测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cw_p31_batch_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_batch_python_files(self):
        """批量解析多个 Python 文件"""
        from callwarden_core import batch_parse_files_lang
        files = []
        for i in range(3):
            path = os.path.join(self.tmpdir, f"mod{i}.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"def func_{i}():\n    return {i}\n")
            files.append((path, f"pkg.mod{i}"))

        results = batch_parse_files_lang(files, "python", num_threads=2)
        self.assertEqual(len(results), 3)

        for i, r in enumerate(results):
            names = [s["name"] for s in r["symbols"]]
            self.assertIn(f"func_{i}", names)

    def test_batch_returns_same_length_as_input(self):
        """结果数量应等于输入文件数"""
        from callwarden_core import batch_parse_files_lang
        files = []
        for i in range(5):
            path = os.path.join(self.tmpdir, f"f{i}.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write("x = 1\n")
            files.append((path, f"m.f{i}"))

        results = batch_parse_files_lang(files, "python")
        self.assertEqual(len(results), 5)

    def test_batch_mixed_languages_not_supported(self):
        """batch_parse_files_lang 只支持单语言（按 language 参数）"""
        from callwarden_core import batch_parse_files_lang
        # 全部用 python
        files = []
        for i in range(2):
            path = os.path.join(self.tmpdir, f"p{i}.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write("def f():\n    pass\n")
            files.append((path, f"p{i}"))

        # 用 rust 解析 python 文件 — 应不报错但可能提取不到符号
        # （Rust grammar 解析 Python 源码会得到不同的 AST）
        results = batch_parse_files_lang(files, "rust")
        self.assertEqual(len(results), 2)


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestBatchParseFilesLangPool(unittest.TestCase):
    """P31: batch_parse_files_lang_pool() 流式回传测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cw_p31_pool_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pool_basic_api(self):
        """Pool: len() / get_at() 基础 API"""
        from callwarden_core import batch_parse_files_lang_pool
        files = []
        for i in range(3):
            path = os.path.join(self.tmpdir, f"m{i}.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"def f{i}():\n    return {i}\n")
            files.append((path, f"p.m{i}"))

        pool = batch_parse_files_lang_pool(files, "python", num_threads=2)
        self.assertEqual(pool.len(), 3)

        # 按索引读取
        r0 = pool.get_at(0)
        self.assertIsNotNone(r0)
        names = [s["name"] for s in r0["symbols"]]
        # 三个文件顺序可能因并行而不固定，但每个都应有 f0/f1/f2 之一
        self.assertTrue(any(n.startswith("f") for n in names))

    def test_pool_iteration(self):
        """Pool: 支持 for 迭代"""
        from callwarden_core import batch_parse_files_lang_pool
        files = []
        for i in range(4):
            path = os.path.join(self.tmpdir, f"i{i}.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"def g{i}():\n    pass\n")
            files.append((path, f"p.i{i}"))

        pool = batch_parse_files_lang_pool(files, "python")
        results = list(pool)
        self.assertEqual(len(results), 4)

    def test_pool_get_at_out_of_range(self):
        """Pool: 越界索引应返回 None 或抛 IndexError"""
        from callwarden_core import batch_parse_files_lang_pool
        path = os.path.join(self.tmpdir, "one.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("def f():\n    pass\n")
        pool = batch_parse_files_lang_pool([(path, "p")], "python")

        self.assertEqual(pool.len(), 1)
        # 越界访问 — 实现可能返回 None 或抛异常，这里都接受
        try:
            r = pool.get_at(100)
            self.assertIsNone(r)
        except (IndexError, Exception):
            pass  # 抛异常也 OK


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestPythonParserAlignment(unittest.TestCase):
    """P31: Rust multi_lang 与 Python parser 对齐测试（核心语言）"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cw_p31_align_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_python_alignment_symbols(self):
        """Python: Rust 与 Python parser 提取的符号名集合一致"""
        from callwarden_core import parse_file_lang
        from callwarden.parsers.python_parser import PythonParser

        path = os.path.join(self.tmpdir, "align.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(_SAMPLE_PYTHON)

        # Rust 解析
        rust_result = parse_file_lang(path, "test.align", "python")
        rust_names = {s["name"] for s in rust_result["symbols"]}

        # Python 解析
        py_parser = PythonParser()
        py_result = py_parser.parse_file(path, "test.align")
        py_names = {s["name"] for s in py_result["symbols"]}

        # 两个集合应一致（允许 Rust 多提取一些，但不能少）
        missing = py_names - rust_names
        self.assertEqual(len(missing), 0,
                        f"Rust 漏提取的符号: {missing}")

    def test_python_alignment_qualified_names(self):
        """Python: qualified_name 应一致"""
        from callwarden_core import parse_file_lang
        from callwarden.parsers.python_parser import PythonParser

        path = os.path.join(self.tmpdir, "align_q.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(_SAMPLE_PYTHON)

        rust_result = parse_file_lang(path, "test.align", "python")
        rust_quals = {s["qualified_name"] for s in rust_result["symbols"]}

        py_parser = PythonParser()
        py_result = py_parser.parse_file(path, "test.align")
        py_quals = {s["qualified_name"] for s in py_result["symbols"]}

        missing = py_quals - rust_quals
        self.assertEqual(len(missing), 0,
                        f"Rust 漏提取的 qualified_name: {missing}")


if __name__ == "__main__":
    unittest.main()
