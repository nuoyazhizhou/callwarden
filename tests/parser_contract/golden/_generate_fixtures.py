"""P0-A Step 1: 一次性生成 16 语言 golden contract fixtures。

本脚本是**手工校对**的预期输出固化器，基于源码语义分析（非任一 parser 当前输出）：
- ``expected`` 字段是长期契约真相（设计文档 §6.1）
- ``known_gaps`` 字段记录当前 Rust/Python parser 相对真相的偏差

用法：
    py -3.14 tests/parser_contract/golden/_generate_fixtures.py

注意：此脚本仅在样本源码变更或 gap 修复后重跑，常规 CI 不执行。
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

_COMMIT_SHA = "9409ede8fdfb1daa491db28a797bb0da94519118"
_CONFIRMED_AT = "2026-07-25T14:10:00+00:00"
_CONFIRMED_BY = "P0-A Step 1 (S-1784986236713-936ac928)"
_METHOD = (
    "hand-curated based on source code semantic analysis; "
    "represents long-term contract truth, not current parser output. "
    "Cross-referenced with tests/parser_contract/_probe_output.json (Rust+Python actual outputs)."
)

# 样本源码（与 generate_baseline.py _ALL_LANGUAGE_SAMPLES 完全一致）
_SAMPLE_PYTHON = '''"""模块文档。"""
import os
from typing import List


class Calculator:
    """计算器类。"""

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
'''

_SAMPLE_RUST = '''use std::collections::HashMap;

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
'''

_SAMPLE_GO = '''package main

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
'''

_SAMPLE_JAVA = '''package com.example;

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
'''

_SAMPLE_TYPESCRIPT = '''import { v4 as uuid } from 'uuid';

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
'''

_SAMPLE_JAVASCRIPT = '''class User {
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
'''

_SAMPLE_RUBY = '''class Calculator
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
'''

_SAMPLE_PHP = '''<?php
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
'''

_SAMPLE_SCALA = '''package com.example

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
'''

_SAMPLE_CSHARP = '''using System;
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
'''

_SAMPLE_CPP = '''#include <iostream>
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
'''

_SAMPLE_KOTLIN = '''package com.example

import kotlin.collections.List

class UserService {
    fun findUser(id: Int): String {
        return getName(id)
    }
    fun getName(id: Int): String {
        return "user_" + id.toString()
    }
}
'''

_SAMPLE_SWIFT = '''import Foundation

class UserService {
    func findUser(id: Int) -> String {
        return getName(id: Int)
    }
    func getName(id: Int) -> String {
        return "user_\\(id)"
    }
}

protocol Drawable {
    func draw()
}
'''

_SAMPLE_ELIXIR = '''defmodule MyModule do
  def hello(name) do
    IO.puts("Hello, " <> name)
  end
end
'''

_SAMPLE_HCL = '''resource "aws_instance" "web" {
  ami           = "ami-12345"
  instance_type = "t3.micro"
  public_ip     = aws_instance.web.private_ip
}

variable "region" {
  default = "us-east-1"
}
'''

_SAMPLE_C = '''#include <stdio.h>

struct Point {
    int x;
    int y;
};

int add(int a, int b) {
    return a + b;
}

int main() {
    struct Point p;
    p.x = 1;
    p.y = 2;
    int s = add(1, 2);
    printf("%d\\n", s);
    return 0;
}
'''


def _provenance(source_ref: str) -> dict:
    """构造 provenance 字段。"""
    return {
        "source": source_ref,
        "confirmed_by": _CONFIRMED_BY,
        "confirmed_at": _CONFIRMED_AT,
        "commit_sha": _COMMIT_SHA,
        "method": _METHOD,
    }


# ============================================
# 16 语言手工校对的预期输出
# ============================================

_FIXTURES: dict[str, dict] = {
    "python": {
        "language": "python",
        "sample_file": "sample.py",
        "sample_source": _SAMPLE_PYTHON,
        "provenance": _provenance(
            "tests/test_p31_multi_lang.py _SAMPLE_PYTHON"
        ),
        "expected": {
            "symbols": [
                {
                    "name": "Calculator",
                    "kind": "class",
                    "signature": "class Calculator",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 6,
                    "line_end": 17,
                },
                {
                    "name": "__init__",
                    "kind": "method",
                    "signature": "def __init__(self)",
                    "visibility": "public",
                    "lexical_parent": "Calculator",
                    "line_start": 9,
                    "line_end": 10,
                },
                {
                    "name": "add",
                    "kind": "method",
                    "signature": "def add(self, x: int) -> int",
                    "visibility": "public",
                    "lexical_parent": "Calculator",
                    "line_start": 12,
                    "line_end": 14,
                },
                {
                    "name": "clear",
                    "kind": "method",
                    "signature": "def clear(self)",
                    "visibility": "public",
                    "lexical_parent": "Calculator",
                    "line_start": 16,
                    "line_end": 17,
                },
                {
                    "name": "main",
                    "kind": "function",
                    "signature": "def main()",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 20,
                    "line_end": 24,
                },
            ],
            "raw_calls": [
                {
                    "caller_name": "main",
                    "callee_name": "Calculator",
                    "callee_module": "",
                    "ordinal": 0,
                    "line": 21,
                },
                {
                    "caller_name": "main",
                    "callee_name": "add",
                    "callee_module": "calc",
                    "ordinal": 1,
                    "line": 22,
                },
                {
                    "caller_name": "main",
                    "callee_name": "clear",
                    "callee_module": "calc",
                    "ordinal": 2,
                    "line": 23,
                },
                {
                    "caller_name": "main",
                    "callee_name": "print",
                    "callee_module": "",
                    "ordinal": 3,
                    "line": 24,
                },
            ],
            "imports": [
                {"source_text": "import os", "normalized_target": "os"},
                {
                    "source_text": "from typing import List",
                    "normalized_target": "typing.List",
                },
            ],
            "references": [],
        },
        "known_gaps": [
            {
                "parser": "rust",
                "field": "signature",
                "description": "Rust SymbolInfo.signature 始终为空字符串；Python parser 提取了完整签名",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols.kind",
                "description": "Rust 把所有函数统一标记为 'fn'，golden 期望区分顶层 function（kind='function'）与方法（kind='method'）",
                "phase": "Phase 2.7",
            },
            {
                "parser": "python",
                "field": "symbols.visibility",
                "description": "Python parser 把 __init__ 标记为 'private'（按 __ 前缀规则），但 __init__ 是公开构造器，应为 'public'",
                "phase": "Phase 1.x",
            },
            {
                "parser": "python",
                "field": "raw_calls",
                "description": "Python parser 不提取 print(calc.value) 中的 print 调用；Rust 提取了。Golden 期望包含 print",
                "phase": "Phase 1.x",
            },
        ],
    },
    "rust": {
        "language": "rust",
        "sample_file": "sample.rs",
        "sample_source": _SAMPLE_RUST,
        "provenance": _provenance(
            "tests/test_p31_multi_lang.py _SAMPLE_RUST"
        ),
        "expected": {
            "symbols": [
                {
                    "name": "Point",
                    "kind": "struct",
                    "signature": "pub struct Point { pub x: f64, pub y: f64 }",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 3,
                    "line_end": 6,
                },
                {
                    "name": "Point",
                    "kind": "impl",
                    "signature": "impl Point",
                    "visibility": "private",
                    "lexical_parent": "",
                    "line_start": 8,
                    "line_end": 18,
                },
                {
                    "name": "new",
                    "kind": "method",
                    "signature": "pub fn new(x: f64, y: f64) -> Self",
                    "visibility": "public",
                    "lexical_parent": "Point",
                    "line_start": 9,
                    "line_end": 11,
                },
                {
                    "name": "distance",
                    "kind": "method",
                    "signature": "pub fn distance(&self, other: &Point) -> f64",
                    "visibility": "public",
                    "lexical_parent": "Point",
                    "line_start": 13,
                    "line_end": 17,
                },
                {
                    "name": "main",
                    "kind": "function",
                    "signature": "pub fn main()",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 20,
                    "line_end": 25,
                },
            ],
            "raw_calls": [
                {
                    "caller_name": "main",
                    "callee_name": "new",
                    "callee_module": "Point",
                    "ordinal": 0,
                    "line": 21,
                },
                {
                    "caller_name": "main",
                    "callee_name": "new",
                    "callee_module": "Point",
                    "ordinal": 1,
                    "line": 22,
                },
                {
                    "caller_name": "main",
                    "callee_name": "distance",
                    "callee_module": "p1",
                    "ordinal": 2,
                    "line": 23,
                },
                {
                    "caller_name": "main",
                    "callee_name": "println",
                    "callee_module": "",
                    "ordinal": 3,
                    "line": 24,
                },
            ],
            "imports": [
                {
                    "source_text": "use std::collections::HashMap",
                    "normalized_target": "std::collections::HashMap",
                },
            ],
            "references": [],
        },
        "known_gaps": [
            {
                "parser": "rust",
                "field": "signature",
                "description": "Rust SymbolInfo.signature 始终为空字符串",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols",
                "description": "Rust 不递归提取 impl 块内部方法（new/distance）；golden 期望含方法符号",
                "phase": "Phase 2.6",
            },
            {
                "parser": "rust",
                "field": "symbols.kind",
                "description": "Rust 把所有函数统一标记为 'fn'，golden 期望区分 function 与 method",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "raw_calls",
                "description": "Rust 不提取 println! 宏调用；golden 期望包含 println",
                "phase": "Phase 2.6",
            },
            {
                "parser": "python",
                "field": "raw_calls",
                "description": "Python parser 不提取 p1.distance(&p2) 对象方法调用（Python 限制）",
                "phase": "Phase 1.x",
            },
        ],
    },
    "go": {
        "language": "go",
        "sample_file": "sample.go",
        "sample_source": _SAMPLE_GO,
        "provenance": _provenance(
            "tests/test_p31_multi_lang.py _SAMPLE_GO"
        ),
        "expected": {
            "symbols": [
                {
                    "name": "Point",
                    "kind": "struct",
                    "signature": "type Point struct",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 8,
                    "line_end": 11,
                },
                {
                    "name": "Printer",
                    "kind": "interface",
                    "signature": "type Printer interface",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 13,
                    "line_end": 15,
                },
                {
                    "name": "Print",
                    "kind": "method",
                    "signature": "func (p *Point) Print() string",
                    "visibility": "public",
                    "lexical_parent": "Point",
                    "line_start": 17,
                    "line_end": 19,
                },
                {
                    "name": "Add",
                    "kind": "function",
                    "signature": "func Add(a, b int) int",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 21,
                    "line_end": 23,
                },
                {
                    "name": "main",
                    "kind": "function",
                    "signature": "func main()",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 25,
                    "line_end": 30,
                },
            ],
            "raw_calls": [
                {
                    "caller_name": "Print",
                    "callee_name": "Sprintf",
                    "callee_module": "fmt",
                    "ordinal": 0,
                    "line": 18,
                },
                {
                    "caller_name": "main",
                    "callee_name": "Print",
                    "callee_module": "p",
                    "ordinal": 0,
                    "line": 26,
                },
                {
                    "caller_name": "main",
                    "callee_name": "Add",
                    "callee_module": "",
                    "ordinal": 1,
                    "line": 27,
                },
                {
                    "caller_name": "main",
                    "callee_name": "Println",
                    "callee_module": "fmt",
                    "ordinal": 2,
                    "line": 28,
                },
                {
                    "caller_name": "main",
                    "callee_name": "HasPrefix",
                    "callee_module": "strings",
                    "ordinal": 3,
                    "line": 28,
                },
            ],
            "imports": [
                {"source_text": '"fmt"', "normalized_target": "fmt"},
                {"source_text": '"strings"', "normalized_target": "strings"},
            ],
            "references": [],
        },
        "known_gaps": [
            {
                "parser": "rust",
                "field": "signature",
                "description": "Rust SymbolInfo.signature 始终为空字符串",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols.kind",
                "description": "Rust 把所有函数统一标记为 'fn'，golden 期望区分 function 与 method",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols.visibility",
                "description": "Rust 把 main 标记为 'public'，但 Go 约定 main 是包私有（应为 'private' 或 'package-private'）",
                "phase": "Phase 2.7",
            },
            {
                "parser": "python",
                "field": "symbols.signature",
                "description": "Python parser 的 signature 缺参数列表（如 'func Add()' 而非 'func Add(a, b int) int'）",
                "phase": "Phase 1.x",
            },
        ],
    },
    "java": {
        "language": "java",
        "sample_file": "Sample.java",
        "sample_source": _SAMPLE_JAVA,
        "provenance": _provenance(
            "tests/test_p31_multi_lang.py _SAMPLE_JAVA"
        ),
        "expected": {
            "symbols": [
                {
                    "name": "Calculator",
                    "kind": "class",
                    "signature": "public class Calculator",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 5,
                    "line_end": 22,
                },
                {
                    "name": "Calculator",
                    "kind": "constructor",
                    "signature": "public Calculator()",
                    "visibility": "public",
                    "lexical_parent": "Calculator",
                    "line_start": 8,
                    "line_end": 10,
                },
                {
                    "name": "add",
                    "kind": "method",
                    "signature": "public int add(int x)",
                    "visibility": "public",
                    "lexical_parent": "Calculator",
                    "line_start": 12,
                    "line_end": 15,
                },
                {
                    "name": "main",
                    "kind": "method",
                    "signature": "public static void main(String[] args)",
                    "visibility": "public",
                    "lexical_parent": "Calculator",
                    "line_start": 17,
                    "line_end": 21,
                },
            ],
            "raw_calls": [
                {
                    "caller_name": "main",
                    "callee_name": "Calculator",
                    "callee_module": "",
                    "ordinal": 0,
                    "line": 19,
                },
                {
                    "caller_name": "main",
                    "callee_name": "add",
                    "callee_module": "calc",
                    "ordinal": 1,
                    "line": 20,
                },
                {
                    "caller_name": "main",
                    "callee_name": "println",
                    "callee_module": "System.out",
                    "ordinal": 2,
                    "line": 20,
                },
            ],
            "imports": [
                {
                    "source_text": "import java.util.List",
                    "normalized_target": "java.util.List",
                },
            ],
            "references": [],
        },
        "known_gaps": [
            {
                "parser": "rust",
                "field": "signature",
                "description": "Rust SymbolInfo.signature 始终为空字符串",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "raw_calls",
                "description": "Rust 把 calc.add(10) 错误地解析为对 calc 的调用（callee_name='calc'），应解析为对 add 的调用（callee_name='add', callee_module='calc'）",
                "phase": "Phase 2.x",
            },
            {
                "parser": "python",
                "field": "raw_calls",
                "description": "Python parser 把 calc.add(10) 错误地解析为对 calc 的调用（callee_name='calc'），应解析为对 add 的调用",
                "phase": "Phase 1.x",
            },
        ],
    },
    "typescript": {
        "language": "typescript",
        "sample_file": "sample.ts",
        "sample_source": _SAMPLE_TYPESCRIPT,
        "provenance": _provenance(
            "tests/test_p31_multi_lang.py _SAMPLE_TYPESCRIPT"
        ),
        "expected": {
            "symbols": [
                {
                    "name": "User",
                    "kind": "class",
                    "signature": "export class User",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 3,
                    "line_end": 9,
                },
                {
                    "name": "constructor",
                    "kind": "constructor",
                    "signature": "constructor(public name: string)",
                    "visibility": "public",
                    "lexical_parent": "User",
                    "line_start": 4,
                    "line_end": 4,
                },
                {
                    "name": "greet",
                    "kind": "method",
                    "signature": "greet(): string",
                    "visibility": "public",
                    "lexical_parent": "User",
                    "line_start": 6,
                    "line_end": 8,
                },
                {
                    "name": "add",
                    "kind": "function",
                    "signature": "export function add(a: number, b: number): number",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 11,
                    "line_end": 13,
                },
                {
                    "name": "main",
                    "kind": "function",
                    "signature": "export function main(): void",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 15,
                    "line_end": 20,
                },
            ],
            "raw_calls": [
                {
                    "caller_name": "main",
                    "callee_name": "User",
                    "callee_module": "",
                    "ordinal": 0,
                    "line": 16,
                },
                {
                    "caller_name": "main",
                    "callee_name": "greet",
                    "callee_module": "u",
                    "ordinal": 1,
                    "line": 17,
                },
                {
                    "caller_name": "main",
                    "callee_name": "add",
                    "callee_module": "",
                    "ordinal": 2,
                    "line": 18,
                },
                {
                    "caller_name": "main",
                    "callee_name": "log",
                    "callee_module": "console",
                    "ordinal": 3,
                    "line": 19,
                },
                {
                    "caller_name": "main",
                    "callee_name": "uuid",
                    "callee_module": "",
                    "ordinal": 4,
                    "line": 19,
                },
            ],
            "imports": [
                {
                    "source_text": "import { v4 as uuid } from 'uuid'",
                    "normalized_target": "uuid",
                },
            ],
            "references": [],
        },
        "known_gaps": [
            {
                "parser": "rust",
                "field": "signature",
                "description": "Rust SymbolInfo.signature 始终为空字符串",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols.kind",
                "description": "Rust 把所有函数统一标记为 'fn'，golden 期望区分 function 与 method",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "raw_calls",
                "description": "Rust 不提取 new User('Alice') 构造调用；golden 期望包含 User 构造调用",
                "phase": "Phase 2.1",
            },
            {
                "parser": "python",
                "field": "symbols",
                "description": "Python parser 重复提取符号（每个符号出现 2 次），是已知 bug",
                "phase": "Phase 1.x",
            },
            {
                "parser": "python",
                "field": "raw_calls",
                "description": "Python parser 不提取 new User('Alice') 构造调用；golden 期望包含",
                "phase": "Phase 1.x",
            },
        ],
    },
    "javascript": {
        "language": "javascript",
        "sample_file": "sample.js",
        "sample_source": _SAMPLE_JAVASCRIPT,
        "provenance": _provenance(
            "tests/test_p31_multi_lang.py _SAMPLE_JAVASCRIPT"
        ),
        "expected": {
            "symbols": [
                {
                    "name": "User",
                    "kind": "class",
                    "signature": "class User",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 1,
                    "line_end": 9,
                },
                {
                    "name": "constructor",
                    "kind": "constructor",
                    "signature": "constructor(name)",
                    "visibility": "public",
                    "lexical_parent": "User",
                    "line_start": 2,
                    "line_end": 4,
                },
                {
                    "name": "greet",
                    "kind": "method",
                    "signature": "greet()",
                    "visibility": "public",
                    "lexical_parent": "User",
                    "line_start": 6,
                    "line_end": 8,
                },
                {
                    "name": "add",
                    "kind": "function",
                    "signature": "function add(a, b)",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 11,
                    "line_end": 13,
                },
                {
                    "name": "main",
                    "kind": "function",
                    "signature": "function main()",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 15,
                    "line_end": 20,
                },
            ],
            "raw_calls": [
                {
                    "caller_name": "main",
                    "callee_name": "User",
                    "callee_module": "",
                    "ordinal": 0,
                    "line": 16,
                },
                {
                    "caller_name": "main",
                    "callee_name": "greet",
                    "callee_module": "u",
                    "ordinal": 1,
                    "line": 17,
                },
                {
                    "caller_name": "main",
                    "callee_name": "add",
                    "callee_module": "",
                    "ordinal": 2,
                    "line": 18,
                },
                {
                    "caller_name": "main",
                    "callee_name": "log",
                    "callee_module": "console",
                    "ordinal": 3,
                    "line": 19,
                },
            ],
            "imports": [],
            "references": [],
        },
        "known_gaps": [
            {
                "parser": "rust",
                "field": "signature",
                "description": "Rust SymbolInfo.signature 始终为空字符串",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols.kind",
                "description": "Rust 把所有函数统一标记为 'fn'，golden 期望区分 function 与 method",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols.visibility",
                "description": "Rust 把 User class 标记为 'public'，但 JS 无 export 关键字时应为 'private'（模块私有）",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "raw_calls",
                "description": "Rust 不提取 new User('Bob') 构造调用；golden 期望包含 User 构造调用",
                "phase": "Phase 2.1",
            },
            {
                "parser": "python",
                "field": "symbols.visibility",
                "description": "Python parser 把 User class 标记为 'private'，add/main 也标记为 'private'，这与 Python 限制（无 export 概念）一致但与 JS 语义不完全匹配",
                "phase": "Phase 1.x",
            },
            {
                "parser": "python",
                "field": "raw_calls",
                "description": "Python parser 不提取 new User('Bob') 构造调用；golden 期望包含",
                "phase": "Phase 1.x",
            },
        ],
    },
    "ruby": {
        "language": "ruby",
        "sample_file": "sample.rb",
        "sample_source": _SAMPLE_RUBY,
        "provenance": _provenance(
            "tests/test_p31_multi_lang.py _SAMPLE_RUBY"
        ),
        "expected": {
            "symbols": [
                {
                    "name": "Calculator",
                    "kind": "class",
                    "signature": "class Calculator",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 1,
                    "line_end": 10,
                },
                {
                    "name": "initialize",
                    "kind": "constructor",
                    "signature": "def initialize",
                    "visibility": "public",
                    "lexical_parent": "Calculator",
                    "line_start": 2,
                    "line_end": 4,
                },
                {
                    "name": "add",
                    "kind": "method",
                    "signature": "def add(x)",
                    "visibility": "public",
                    "lexical_parent": "Calculator",
                    "line_start": 6,
                    "line_end": 9,
                },
                {
                    "name": "main",
                    "kind": "function",
                    "signature": "def main",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 12,
                    "line_end": 16,
                },
            ],
            "raw_calls": [
                {
                    "caller_name": "main",
                    "callee_name": "new",
                    "callee_module": "Calculator",
                    "ordinal": 0,
                    "line": 13,
                },
                {
                    "caller_name": "main",
                    "callee_name": "add",
                    "callee_module": "calc",
                    "ordinal": 1,
                    "line": 14,
                },
                {
                    "caller_name": "main",
                    "callee_name": "puts",
                    "callee_module": "",
                    "ordinal": 2,
                    "line": 15,
                },
            ],
            "imports": [],
            "references": [],
        },
        "known_gaps": [
            {
                "parser": "rust",
                "field": "signature",
                "description": "Rust SymbolInfo.signature 始终为空字符串",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols.kind",
                "description": "Rust 把所有函数统一标记为 'fn'，golden 期望区分 class method（kind='method'）与顶层 function（kind='function'）",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols",
                "description": "Rust 不提取 Ruby initialize 构造器；golden 期望含 initialize",
                "phase": "Phase 2.x",
            },
            {
                "parser": "rust",
                "field": "raw_calls",
                "description": "Rust 不提取 Calculator.new / calc.add / puts 调用；golden 期望含 3 个调用",
                "phase": "Phase 2.x",
            },
            {
                "parser": "python",
                "field": "raw_calls",
                "description": "Python parser 不提取 Calculator.new / calc.add 对象方法调用（Python 限制）",
                "phase": "Phase 1.x",
            },
        ],
    },
    "php": {
        "language": "php",
        "sample_file": "sample.php",
        "sample_source": _SAMPLE_PHP,
        "provenance": _provenance(
            "tests/test_p31_multi_lang.py _SAMPLE_PHP"
        ),
        "expected": {
            "symbols": [
                {
                    "name": "Calculator",
                    "kind": "class",
                    "signature": "class Calculator",
                    "visibility": "public",
                    "lexical_parent": "App",
                    "line_start": 6,
                    "line_end": 17,
                },
                {
                    "name": "value",
                    "kind": "property",
                    "signature": "private $value",
                    "visibility": "private",
                    "lexical_parent": "Calculator",
                    "line_start": 7,
                    "line_end": 7,
                },
                {
                    "name": "__construct",
                    "kind": "constructor",
                    "signature": "public function __construct()",
                    "visibility": "public",
                    "lexical_parent": "Calculator",
                    "line_start": 9,
                    "line_end": 11,
                },
                {
                    "name": "add",
                    "kind": "method",
                    "signature": "public function add($x)",
                    "visibility": "public",
                    "lexical_parent": "Calculator",
                    "line_start": 13,
                    "line_end": 16,
                },
            ],
            "raw_calls": [
                {
                    "caller_name": "<file-scope>",
                    "callee_name": "Calculator",
                    "callee_module": "",
                    "ordinal": 0,
                    "line": 19,
                },
                {
                    "caller_name": "<file-scope>",
                    "callee_name": "add",
                    "callee_module": "calc",
                    "ordinal": 1,
                    "line": 20,
                },
            ],
            "imports": [
                {
                    "source_text": "use App\\Service",
                    "normalized_target": "App\\Service",
                },
            ],
            "references": [],
        },
        "known_gaps": [
            {
                "parser": "rust",
                "field": "signature",
                "description": "Rust SymbolInfo.signature 始终为空字符串",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols",
                "description": "Rust 不提取 PHP property 符号（$value）；golden 期望含 property",
                "phase": "Phase 2.2",
            },
            {
                "parser": "rust",
                "field": "symbols.kind",
                "description": "Rust 把 __construct 标记为 'method'，golden 期望为 'constructor'",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "raw_calls",
                "description": "Rust 不提取 $calc->add(10) 调用；golden 期望含 add 调用",
                "phase": "Phase 2.2",
            },
            {
                "parser": "rust",
                "field": "symbols.module_path",
                "description": "Rust module_path 用传入参数（test.sample），未读取 PHP namespace（App）",
                "phase": "Phase 2.2",
            },
            {
                "parser": "python",
                "field": "raw_calls",
                "description": "Python parser 不提取 $calc->add(10) 对象方法调用（Python 限制）",
                "phase": "Phase 1.x",
            },
        ],
    },
    "scala": {
        "language": "scala",
        "sample_file": "Sample.scala",
        "sample_source": _SAMPLE_SCALA,
        "provenance": _provenance(
            "tests/test_p31_multi_lang.py _SAMPLE_SCALA"
        ),
        "expected": {
            "symbols": [
                {
                    "name": "Calculator",
                    "kind": "class",
                    "signature": "class Calculator",
                    "visibility": "public",
                    "lexical_parent": "com.example",
                    "line_start": 5,
                    "line_end": 12,
                },
                {
                    "name": "add",
                    "kind": "method",
                    "signature": "def add(x: Int): Int",
                    "visibility": "public",
                    "lexical_parent": "Calculator",
                    "line_start": 8,
                    "line_end": 11,
                },
                {
                    "name": "Main",
                    "kind": "object",
                    "signature": "object Main",
                    "visibility": "public",
                    "lexical_parent": "com.example",
                    "line_start": 14,
                    "line_end": 20,
                },
                {
                    "name": "main",
                    "kind": "method",
                    "signature": "def main(args: Array[String]): Unit",
                    "visibility": "public",
                    "lexical_parent": "Main",
                    "line_start": 15,
                    "line_end": 19,
                },
            ],
            "raw_calls": [
                {
                    "caller_name": "main",
                    "callee_name": "Calculator",
                    "callee_module": "",
                    "ordinal": 0,
                    "line": 16,
                },
                {
                    "caller_name": "main",
                    "callee_name": "add",
                    "callee_module": "calc",
                    "ordinal": 1,
                    "line": 17,
                },
                {
                    "caller_name": "main",
                    "callee_name": "println",
                    "callee_module": "",
                    "ordinal": 2,
                    "line": 18,
                },
            ],
            "imports": [
                {
                    "source_text": "import scala.collection.mutable.ListBuffer",
                    "normalized_target": "scala.collection.mutable.ListBuffer",
                },
            ],
            "references": [],
        },
        "known_gaps": [
            {
                "parser": "rust",
                "field": "signature",
                "description": "Rust SymbolInfo.signature 始终为空字符串",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols.kind",
                "description": "Rust 把方法统一标记为 'fn'，golden 期望为 'method'",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "raw_calls",
                "description": "Rust 不提取 calc.add(10) 对象方法调用；golden 期望含 add 调用（Phase 1.4 重点）",
                "phase": "Phase 2.3",
            },
            {
                "parser": "rust",
                "field": "raw_calls",
                "description": "Rust 不提取 new Calculator 构造调用；golden 期望含 Calculator 构造调用",
                "phase": "Phase 2.3",
            },
            {
                "parser": "rust",
                "field": "symbols.module_path",
                "description": "Rust module_path 用传入参数（test.sample），未读取 Scala package（com.example）",
                "phase": "Phase 2.3",
            },
            {
                "parser": "python",
                "field": "raw_calls",
                "description": "Python parser 不提取 new Calculator 构造调用",
                "phase": "Phase 1.x",
            },
        ],
    },
    "csharp": {
        "language": "csharp",
        "sample_file": "Sample.cs",
        "sample_source": _SAMPLE_CSHARP,
        "provenance": _provenance(
            "tests/test_p31_multi_lang.py _SAMPLE_CSHARP"
        ),
        "expected": {
            "symbols": [
                {
                    "name": "Calculator",
                    "kind": "class",
                    "signature": "public class Calculator",
                    "visibility": "public",
                    "lexical_parent": "Example",
                    "line_start": 5,
                    "line_end": 22,
                },
                {
                    "name": "value",
                    "kind": "field",
                    "signature": "private int value",
                    "visibility": "private",
                    "lexical_parent": "Calculator",
                    "line_start": 6,
                    "line_end": 6,
                },
                {
                    "name": "Calculator",
                    "kind": "constructor",
                    "signature": "public Calculator()",
                    "visibility": "public",
                    "lexical_parent": "Calculator",
                    "line_start": 8,
                    "line_end": 10,
                },
                {
                    "name": "Add",
                    "kind": "method",
                    "signature": "public int Add(int x)",
                    "visibility": "public",
                    "lexical_parent": "Calculator",
                    "line_start": 12,
                    "line_end": 15,
                },
                {
                    "name": "Main",
                    "kind": "method",
                    "signature": "public static void Main(string[] args)",
                    "visibility": "public",
                    "lexical_parent": "Calculator",
                    "line_start": 17,
                    "line_end": 21,
                },
            ],
            "raw_calls": [
                {
                    "caller_name": "Main",
                    "callee_name": "Calculator",
                    "callee_module": "",
                    "ordinal": 0,
                    "line": 18,
                },
                {
                    "caller_name": "Main",
                    "callee_name": "Add",
                    "callee_module": "calc",
                    "ordinal": 1,
                    "line": 19,
                },
                {
                    "caller_name": "Main",
                    "callee_name": "WriteLine",
                    "callee_module": "Console",
                    "ordinal": 2,
                    "line": 20,
                },
            ],
            "imports": [
                {"source_text": "using System", "normalized_target": "System"},
                {
                    "source_text": "using System.Collections.Generic",
                    "normalized_target": "System.Collections.Generic",
                },
            ],
            "references": [],
        },
        "known_gaps": [
            {
                "parser": "rust",
                "field": "signature",
                "description": "Rust SymbolInfo.signature 始终为空字符串",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols",
                "description": "Rust 不提取 C# field（value）；golden 期望含 field",
                "phase": "Phase 2.x",
            },
            {
                "parser": "rust",
                "field": "raw_calls",
                "description": "Rust 不提取 calc.Add(10) / Console.WriteLine 调用；golden 期望含 Add 与 WriteLine 调用（当前 Rust raw_calls=0）",
                "phase": "Phase 2.x",
            },
            {
                "parser": "rust",
                "field": "symbols.module_path",
                "description": "Rust module_path 用传入参数（test.sample），未读取 C# namespace（Example）",
                "phase": "Phase 2.x",
            },
            {
                "parser": "python",
                "field": "raw_calls",
                "description": "Python parser 不提取 calc.Add(10) 对象方法调用（Python 限制）",
                "phase": "Phase 1.x",
            },
        ],
    },
    "cpp": {
        "language": "cpp",
        "sample_file": "sample.cpp",
        "sample_source": _SAMPLE_CPP,
        "provenance": _provenance(
            "tests/test_p31_multi_lang.py _SAMPLE_CPP"
        ),
        "expected": {
            "symbols": [
                {
                    "name": "example",
                    "kind": "namespace",
                    "signature": "namespace example",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 4,
                    "line_end": 26,
                },
                {
                    "name": "Point",
                    "kind": "class",
                    "signature": "class Point",
                    "visibility": "public",
                    "lexical_parent": "example",
                    "line_start": 6,
                    "line_end": 16,
                },
                {
                    "name": "Point",
                    "kind": "constructor",
                    "signature": "Point(int x, int y)",
                    "visibility": "public",
                    "lexical_parent": "Point",
                    "line_start": 8,
                    "line_end": 8,
                },
                {
                    "name": "distance",
                    "kind": "method",
                    "signature": "int distance()",
                    "visibility": "public",
                    "lexical_parent": "Point",
                    "line_start": 10,
                    "line_end": 12,
                },
                {
                    "name": "add",
                    "kind": "function",
                    "signature": "int add(int a, int b)",
                    "visibility": "public",
                    "lexical_parent": "example",
                    "line_start": 19,
                    "line_end": 21,
                },
                {
                    "name": "main",
                    "kind": "function",
                    "signature": "int main()",
                    "visibility": "public",
                    "lexical_parent": "example",
                    "line_start": 23,
                    "line_end": 25,
                },
            ],
            "raw_calls": [
                {
                    "caller_name": "main",
                    "callee_name": "Point",
                    "callee_module": "",
                    "ordinal": 0,
                    "line": 23,
                },
                {
                    "caller_name": "main",
                    "callee_name": "distance",
                    "callee_module": "p",
                    "ordinal": 1,
                    "line": 24,
                },
                {
                    "caller_name": "main",
                    "callee_name": "add",
                    "callee_module": "",
                    "ordinal": 2,
                    "line": 24,
                },
                {
                    "caller_name": "main",
                    "callee_name": "operator<<",
                    "callee_module": "std::cout",
                    "ordinal": 3,
                    "line": 25,
                },
                {
                    "caller_name": "main",
                    "callee_name": "endl",
                    "callee_module": "std",
                    "ordinal": 4,
                    "line": 25,
                },
            ],
            "imports": [
                {"source_text": "#include <iostream>", "normalized_target": "<iostream>"},
                {"source_text": "#include <vector>", "normalized_target": "<vector>"},
            ],
            "references": [],
        },
        "known_gaps": [
            {
                "parser": "rust",
                "field": "signature",
                "description": "Rust SymbolInfo.signature 始终为空字符串",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols.kind",
                "description": "Rust 把 namespace 标记为 'namespace' 但 method 标记为 'fn'，golden 期望区分 function 与 method",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "raw_calls",
                "description": "Rust 不提取 Point 构造调用 / p.distance() / operator<< / endl 调用；golden 期望含 5 个调用",
                "phase": "Phase 2.6",
            },
            {
                "parser": "rust",
                "field": "symbols",
                "description": "Rust 额外提取 'example' namespace 符号（kind='namespace'）；Python 不提取。golden 期望含 namespace 符号（Rust 正确，Python 缺失）",
                "phase": "Phase 1.x",
            },
            {
                "parser": "python",
                "field": "raw_calls",
                "description": "Python parser 不提取 p.distance() 对象方法调用（Python 限制）",
                "phase": "Phase 1.x",
            },
        ],
    },
    "kotlin": {
        "language": "kotlin",
        "sample_file": "Sample.kt",
        "sample_source": _SAMPLE_KOTLIN,
        "provenance": _provenance(
            "tests/parser_contract/generate_baseline.py _KOTLIN_SAMPLE"
        ),
        "expected": {
            "symbols": [
                {
                    "name": "UserService",
                    "kind": "class",
                    "signature": "class UserService",
                    "visibility": "public",
                    "lexical_parent": "com.example",
                    "line_start": 5,
                    "line_end": 12,
                },
                {
                    "name": "findUser",
                    "kind": "method",
                    "signature": "fun findUser(id: Int): String",
                    "visibility": "public",
                    "lexical_parent": "UserService",
                    "line_start": 6,
                    "line_end": 8,
                },
                {
                    "name": "getName",
                    "kind": "method",
                    "signature": "fun getName(id: Int): String",
                    "visibility": "public",
                    "lexical_parent": "UserService",
                    "line_start": 9,
                    "line_end": 11,
                },
            ],
            "raw_calls": [
                {
                    "caller_name": "findUser",
                    "callee_name": "getName",
                    "callee_module": "",
                    "ordinal": 0,
                    "line": 7,
                },
                {
                    "caller_name": "getName",
                    "callee_name": "toString",
                    "callee_module": "id",
                    "ordinal": 0,
                    "line": 10,
                },
            ],
            "imports": [
                {
                    "source_text": "import kotlin.collections.List",
                    "normalized_target": "kotlin.collections.List",
                },
            ],
            "references": [],
        },
        "known_gaps": [
            {
                "parser": "rust",
                "field": "signature",
                "description": "Rust SymbolInfo.signature 始终为空字符串",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols.kind",
                "description": "Rust 把所有函数统一标记为 'fn'，golden 期望为 'method'",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "raw_calls",
                "description": "Rust 不提取 id.toString() 对象方法调用；golden 期望含 toString 调用",
                "phase": "Phase 2.x",
            },
            {
                "parser": "rust",
                "field": "symbols.module_path",
                "description": "Rust module_path 用传入参数（test.sample），未读取 Kotlin package（com.example）",
                "phase": "Phase 2.x",
            },
            {
                "parser": "python",
                "field": "raw_calls",
                "description": "Python parser 不提取 id.toString() 对象方法调用（Python 限制）",
                "phase": "Phase 1.x",
            },
        ],
    },
    "swift": {
        "language": "swift",
        "sample_file": "sample.swift",
        "sample_source": _SAMPLE_SWIFT,
        "provenance": _provenance(
            "tests/parser_contract/generate_baseline.py _SWIFT_SAMPLE"
        ),
        "expected": {
            "symbols": [
                {
                    "name": "UserService",
                    "kind": "class",
                    "signature": "class UserService",
                    "visibility": "internal",
                    "lexical_parent": "",
                    "line_start": 3,
                    "line_end": 10,
                },
                {
                    "name": "findUser",
                    "kind": "method",
                    "signature": "func findUser(id: Int) -> String",
                    "visibility": "internal",
                    "lexical_parent": "UserService",
                    "line_start": 4,
                    "line_end": 6,
                },
                {
                    "name": "getName",
                    "kind": "method",
                    "signature": "func getName(id: Int) -> String",
                    "visibility": "internal",
                    "lexical_parent": "UserService",
                    "line_start": 7,
                    "line_end": 9,
                },
                {
                    "name": "Drawable",
                    "kind": "protocol",
                    "signature": "protocol Drawable",
                    "visibility": "internal",
                    "lexical_parent": "",
                    "line_start": 12,
                    "line_end": 14,
                },
                {
                    "name": "draw",
                    "kind": "method",
                    "signature": "func draw()",
                    "visibility": "internal",
                    "lexical_parent": "Drawable",
                    "line_start": 13,
                    "line_end": 13,
                },
            ],
            "raw_calls": [
                {
                    "caller_name": "findUser",
                    "callee_name": "getName",
                    "callee_module": "",
                    "ordinal": 0,
                    "line": 5,
                },
            ],
            "imports": [
                {"source_text": "import Foundation", "normalized_target": "Foundation"},
            ],
            "references": [],
        },
        "known_gaps": [
            {
                "parser": "rust",
                "field": "signature",
                "description": "Rust SymbolInfo.signature 始终为空字符串",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols.kind",
                "description": "Rust 把方法标记为 'fn'，golden 期望为 'method'",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols.visibility",
                "description": "Rust 把所有符号标记为 'public'，但 Swift 默认访问级别是 'internal'，golden 期望为 'internal'",
                "phase": "Phase 2.7",
            },
            {
                "parser": "python",
                "field": "symbols.kind",
                "description": "Python parser 把方法标记为 'function'，golden 期望区分 class method（'method'）与 protocol method（'method'）",
                "phase": "Phase 1.x",
            },
        ],
    },
    "elixir": {
        "language": "elixir",
        "sample_file": "sample.ex",
        "sample_source": _SAMPLE_ELIXIR,
        "provenance": _provenance(
            "tests/parser_contract/generate_baseline.py _ELIXIR_SAMPLE"
        ),
        "expected": {
            "symbols": [
                {
                    "name": "MyModule",
                    "kind": "module",
                    "signature": "defmodule MyModule",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 1,
                    "line_end": 5,
                },
                {
                    "name": "hello",
                    "kind": "function",
                    "signature": "def hello(name)",
                    "visibility": "public",
                    "lexical_parent": "MyModule",
                    "line_start": 2,
                    "line_end": 4,
                },
            ],
            "raw_calls": [
                {
                    "caller_name": "hello",
                    "callee_name": "puts",
                    "callee_module": "IO",
                    "ordinal": 0,
                    "line": 3,
                },
            ],
            "imports": [],
            "references": [],
        },
        "known_gaps": [
            {
                "parser": "rust",
                "field": "signature",
                "description": "Rust SymbolInfo.signature 始终为空字符串",
                "phase": "Phase 2.7",
            },
            {
                "parser": "python",
                "field": "raw_calls",
                "description": "Python parser 不提取 IO.puts 调用（Python 限制）",
                "phase": "Phase 1.x",
            },
        ],
    },
    "hcl": {
        "language": "hcl",
        "sample_file": "sample.tf",
        "sample_source": _SAMPLE_HCL,
        "provenance": _provenance(
            "tests/parser_contract/generate_baseline.py _HCL_SAMPLE"
        ),
        "expected": {
            "symbols": [
                {
                    "name": "aws_instance.web",
                    "kind": "resource",
                    "signature": 'resource "aws_instance" "web"',
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 1,
                    "line_end": 5,
                },
                {
                    "name": "region",
                    "kind": "variable",
                    "signature": 'variable "region"',
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 7,
                    "line_end": 9,
                },
            ],
            "raw_calls": [
                {
                    "caller_name": "aws_instance.web",
                    "callee_name": "aws_instance.web.private_ip",
                    "callee_module": "test.sample",
                    "ordinal": 0,
                    "line": 4,
                },
            ],
            "imports": [],
            "references": [
                {
                    "caller_name": "aws_instance.web",
                    "callee_name": "aws_instance.web",
                    "line": 4,
                    "reference_kind": "attribute_traversal",
                    "source_text": "aws_instance.web.private_ip",
                },
            ],
        },
        "known_gaps": [
            {
                "parser": "rust",
                "field": "signature",
                "description": "Rust SymbolInfo.signature 始终为空字符串（HCL block 签名未实现）",
                "phase": "Phase 2.7",
            },
            {
                "parser": "python",
                "field": "signature",
                "description": "Python parser signature 形如 'resource \"aws_instance\" \"web\"'，与 golden 一致（Python 正确）",
                "phase": "Phase 1.x",
            },
        ],
    },
    "c": {
        "language": "c",
        "sample_file": "sample.c",
        "sample_source": _SAMPLE_C,
        "provenance": _provenance(
            "tests/parser_contract/generate_baseline.py _C_SAMPLE"
        ),
        "expected": {
            "symbols": [
                {
                    "name": "Point",
                    "kind": "struct",
                    "signature": "struct Point { int x; int y; }",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 3,
                    "line_end": 6,
                },
                {
                    "name": "add",
                    "kind": "function",
                    "signature": "int add(int a, int b)",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 8,
                    "line_end": 10,
                },
                {
                    "name": "main",
                    "kind": "function",
                    "signature": "int main()",
                    "visibility": "public",
                    "lexical_parent": "",
                    "line_start": 12,
                    "line_end": 19,
                },
            ],
            "raw_calls": [
                {
                    "caller_name": "main",
                    "callee_name": "add",
                    "callee_module": "",
                    "ordinal": 0,
                    "line": 16,
                },
                {
                    "caller_name": "main",
                    "callee_name": "printf",
                    "callee_module": "",
                    "ordinal": 1,
                    "line": 17,
                },
            ],
            "imports": [
                {"source_text": "#include <stdio.h>", "normalized_target": "<stdio.h>"},
            ],
            "references": [],
        },
        "known_gaps": [
            {
                "parser": "rust",
                "field": "signature",
                "description": "Rust SymbolInfo.signature 始终为空字符串（C 走专用 parse_c_file 路径）",
                "phase": "Phase 2.7",
            },
            {
                "parser": "rust",
                "field": "symbols.kind",
                "description": "Rust 把函数标记为 'fn'，golden 期望为 'function'",
                "phase": "Phase 2.7",
            },
            {
                "parser": "python",
                "field": "raw_calls",
                "description": "Python parser 不提取 printf 调用；golden 期望含 printf 调用（Python 限制）",
                "phase": "Phase 1.x",
            },
        ],
    },
}


def main() -> int:
    out_dir = Path(__file__).resolve().parent
    print(f"写入 {len(_FIXTURES)} 个 golden fixture 到 {out_dir}")
    for lang, fixture in _FIXTURES.items():
        path = out_dir / f"{lang}.json"
        path.write_text(
            json.dumps(fixture, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        gap_count = len(fixture["known_gaps"])
        sym_count = len(fixture["expected"]["symbols"])
        call_count = len(fixture["expected"]["raw_calls"])
        print(
            f"  {lang:12s}: symbols={sym_count} calls={call_count} "
            f"gaps={gap_count} → {path.name}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
