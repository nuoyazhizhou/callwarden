"""L9 Rust multilang parser 补齐测试

验证 15 种语言全部纳入 Rust multilang parser 路径：
- supported_languages() 包含 kotlin + swift + elixir + hcl
- parse_file_lang 对 Kotlin/Swift/Elixir/HCL 文件能提取符号
- _can_use_rust_parse 对 15 种语言全部返回 True
- Python parser 仍能工作（作为 fallback 保证）
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ====================================================================
# 测试样本
# ====================================================================

KOTLIN_SAMPLE = '''package com.example

import kotlin.collections.List

class UserService {
    fun findUser(id: Int): String {
        return getName(id)
    }
    fun getName(id: Int): String {
        return "user_$id"
    }
}
'''

SWIFT_SAMPLE = '''import Foundation

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

ELIXIR_SAMPLE = '''defmodule MyModule do
  def hello(name) do
    IO.puts("Hello, " <> name)
  end
end
'''

HCL_SAMPLE = '''resource "aws_instance" "web" {
  ami           = "ami-12345"
  instance_type = "t3.micro"
}

variable "region" {
  default = "us-east-1"
}
'''


# ====================================================================
# Rust 扩展可用性检查
# ====================================================================

def _rust_available():
    """检查 callwarden_core 是否可导入"""
    try:
        import callwarden_core  # noqa: F401
        return True
    except ImportError:
        return False


rust_required = pytest.mark.skipif(
    not _rust_available(),
    reason="callwarden_core 不可导入，需先 cargo build --release + 复制 .pyd"
)


# ====================================================================
# 测试用例
# ====================================================================

@rust_required
def test_supported_languages_includes_kotlin_swift():
    """测试 1：Rust supported_languages() 包含 kotlin + swift"""
    from callwarden_core import supported_languages

    langs = supported_languages()
    assert "kotlin" in langs, f"kotlin 应在 supported_languages 中，实际 {langs}"
    assert "swift" in langs, f"swift 应在 supported_languages 中，实际 {langs}"


@rust_required
def test_elixir_hcl_in_rust_supported():
    """测试 2：Elixir/HCL 已在 Rust supported_languages 中（补齐完成）"""
    from callwarden_core import supported_languages

    langs = supported_languages()
    assert "elixir" in langs, f"elixir 应在 supported_languages 中，实际 {langs}"
    assert "hcl" in langs, f"hcl 应在 supported_languages 中，实际 {langs}"
    # 验证总数：11 基础语言 + kotlin + swift + elixir + hcl = 15
    assert len(langs) == 15, f"应有 15 种语言，实际 {len(langs)}: {langs}"


@rust_required
def test_rust_parse_kotlin_symbols():
    """测试 3：Rust parse Kotlin 提取符号"""
    from callwarden_core import parse_file_lang

    with tempfile.NamedTemporaryFile(mode="w", suffix=".kt", delete=False, encoding="utf-8") as f:
        f.write(KOTLIN_SAMPLE)
        kt_path = f.name

    try:
        result = parse_file_lang(kt_path, "", "kotlin")

        assert result.get("error") is None, f"parse 不应有错误，实际 {result.get('error')}"
        assert result["language"] == "kotlin"

        symbols = result["symbols"]
        symbol_names = [s["name"] for s in symbols]
        symbol_kinds = {s["name"]: s["kind"] for s in symbols}

        # 应找到的符号
        assert "UserService" in symbol_names, f"应找到 UserService，实际 {symbol_names}"
        assert "findUser" in symbol_names, f"应找到 findUser，实际 {symbol_names}"
        assert "getName" in symbol_names, f"应找到 getName，实际 {symbol_names}"

        # kind 验证
        assert symbol_kinds["UserService"] == "class"
        assert symbol_kinds["findUser"] == "fn"
        assert symbol_kinds["getName"] == "fn"

        # qualified_name 验证（类内方法含类名前缀）
        symbol_quals = {s["name"]: s["qualified_name"] for s in symbols}
        assert "UserService.findUser" in symbol_quals["findUser"]
        assert "UserService.getName" in symbol_quals["getName"]

        # import 验证
        imports = result["imports"]
        assert len(imports) >= 1, f"应有至少 1 个 import，实际 {len(imports)}"

        # 调用关系验证（Rust 返回 dict 用 raw_calls key）
        calls = result.get("calls") or result.get("raw_calls") or []
        if calls:
            callee_names = [c["callee_name"] for c in calls]
            assert "getName" in callee_names, f"应找到对 getName 的调用，实际 {callee_names}"
    finally:
        os.unlink(kt_path)


@rust_required
def test_rust_parse_swift_symbols():
    """测试 4：Rust parse Swift 提取符号"""
    from callwarden_core import parse_file_lang

    with tempfile.NamedTemporaryFile(mode="w", suffix=".swift", delete=False, encoding="utf-8") as f:
        f.write(SWIFT_SAMPLE)
        sw_path = f.name

    try:
        result = parse_file_lang(sw_path, "", "swift")

        assert result.get("error") is None, f"parse 不应有错误，实际 {result.get('error')}"
        assert result["language"] == "swift"

        symbols = result["symbols"]
        symbol_names = [s["name"] for s in symbols]
        symbol_kinds = {s["name"]: s["kind"] for s in symbols}

        # 应找到的符号
        assert "UserService" in symbol_names, f"应找到 UserService，实际 {symbol_names}"
        assert "findUser" in symbol_names, f"应找到 findUser，实际 {symbol_names}"
        assert "getName" in symbol_names, f"应找到 getName，实际 {symbol_names}"
        assert "Drawable" in symbol_names, f"应找到 Drawable，实际 {symbol_names}"

        # kind 验证
        # 注意：tree-sitter-swift 0.7.x 把 struct/enum/actor 统一为 class_declaration
        # Rust multilang 框架暂不支持 declaration_kind 字段值映射，统一标记为 "class"
        assert symbol_kinds["UserService"] == "class"
        assert symbol_kinds["findUser"] == "fn"
        assert symbol_kinds["getName"] == "fn"
        assert symbol_kinds["Drawable"] == "protocol"

        # import 验证
        imports = result["imports"]
        assert len(imports) >= 1, f"应有至少 1 个 import，实际 {len(imports)}"
    finally:
        os.unlink(sw_path)


@rust_required
def test_rust_parse_elixir_symbols():
    """测试 4b：Rust parse Elixir 提取符号（defmodule/def/defp）"""
    from callwarden_core import parse_file_lang

    elixir_code = '''defmodule MyModule do
  def hello(name) do
    IO.puts("Hello, " <> name)
  end
  defp priv(x) do
    x
  end
end
'''
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ex", delete=False, encoding="utf-8") as f:
        f.write(elixir_code)
        ex_path = f.name

    try:
        result = parse_file_lang(ex_path, "", "elixir")

        assert result.get("error") is None, f"parse 不应有错误，实际 {result.get('error')}"
        assert result["language"] == "elixir"

        symbols = result["symbols"]
        symbol_names = [s["name"] for s in symbols]
        symbol_kinds = {s["name"]: s["kind"] for s in symbols}

        # 应找到的符号
        assert "MyModule" in symbol_names, f"应找到 MyModule，实际 {symbol_names}"
        assert "hello" in symbol_names, f"应找到 hello，实际 {symbol_names}"
        assert "priv" in symbol_names, f"应找到 priv，实际 {symbol_names}"

        # kind 验证
        assert symbol_kinds["MyModule"] == "module", f"MyModule kind 应为 module，实际 {symbol_kinds['MyModule']}"
        assert symbol_kinds["hello"] == "function", f"hello kind 应为 function，实际 {symbol_kinds['hello']}"
        assert symbol_kinds["priv"] == "function", f"priv kind 应为 function，实际 {symbol_kinds['priv']}"

        # qualified_name 验证（模块内函数含模块名前缀）
        symbol_quals = {s["name"]: s["qualified_name"] for s in symbols}
        assert "MyModule" in symbol_quals["hello"], f"hello qualified 应含 MyModule，实际 {symbol_quals['hello']}"
        assert "MyModule" in symbol_quals["priv"], f"priv qualified 应含 MyModule，实际 {symbol_quals['priv']}"
    finally:
        os.unlink(ex_path)


@rust_required
def test_rust_parse_hcl_symbols():
    """测试 4c：Rust parse HCL 提取符号（resource/variable block）"""
    from callwarden_core import parse_file_lang

    hcl_code = '''resource "aws_instance" "web" {
  ami           = "ami-12345"
  instance_type = "t3.micro"
}

variable "region" {
  default = "us-east-1"
}

provider "aws" {
  region = "us-east-1"
}
'''
    with tempfile.NamedTemporaryFile(mode="w", suffix=".hcl", delete=False, encoding="utf-8") as f:
        f.write(hcl_code)
        hcl_path = f.name

    try:
        result = parse_file_lang(hcl_path, "", "hcl")

        assert result.get("error") is None, f"parse 不应有错误，实际 {result.get('error')}"
        assert result["language"] == "hcl"

        symbols = result["symbols"]
        symbol_names = [s["name"] for s in symbols]
        symbol_kinds = {s["name"]: s["kind"] for s in symbols}

        # 应找到的符号
        assert "aws_instance.web" in symbol_names, f"应找到 aws_instance.web，实际 {symbol_names}"
        assert "region" in symbol_names, f"应找到 region，实际 {symbol_names}"
        assert "aws" in symbol_names, f"应找到 aws provider，实际 {symbol_names}"

        # kind 验证（HCL 块类型映射）
        assert symbol_kinds["aws_instance.web"] == "resource", f"aws_instance.web kind 应为 resource"
        assert symbol_kinds["region"] == "variable", f"region kind 应为 variable"
        assert symbol_kinds["aws"] == "provider", f"aws kind 应为 provider"

        # qualified_name 验证（HCL 块的 qualified_name 与 name 相同）
        symbol_quals = {s["name"]: s["qualified_name"] for s in symbols}
        assert symbol_quals["aws_instance.web"] == "aws_instance.web"
    finally:
        os.unlink(hcl_path)


@rust_required
def test_can_use_rust_parse_all_15_langs():
    """测试 5：_can_use_rust_parse 对 15 种语言全部返回 True"""
    from callwarden.db.db_build import _can_use_rust_parse

    all_langs = [
        "python", "rust", "go", "java", "typescript", "javascript",
        "ruby", "php", "scala", "csharp", "cpp",
        "kotlin", "swift", "elixir", "hcl",
    ]
    for lang in all_langs:
        assert _can_use_rust_parse(lang), f"_can_use_rust_parse('{lang}') 应返回 True"


@rust_required
def test_elixir_hcl_use_rust_path():
    """测试 6：Elixir/HCL 走 Rust 路径（_can_use_rust_parse 返回 True）"""
    from callwarden.db.db_build import _can_use_rust_parse

    assert _can_use_rust_parse("elixir"), "elixir 应走 Rust 路径"
    assert _can_use_rust_parse("hcl"), "hcl 应走 Rust 路径"


@rust_required
def test_elixir_python_parser_still_works():
    """测试 7：Elixir 仍能通过 Python parser 正常解析"""
    from callwarden.parsers import ElixirParser

    with tempfile.NamedTemporaryFile(mode="w", suffix=".ex", delete=False, encoding="utf-8") as f:
        f.write(ELIXIR_SAMPLE)
        ex_path = f.name

    try:
        parser = ElixirParser()
        result = parser.parse_file(ex_path)

        assert result["language"] == "elixir"
        symbols = result["symbols"]
        symbol_names = [s["name"] for s in symbols]
        assert "MyModule" in symbol_names, f"应找到 MyModule，实际 {symbol_names}"
    finally:
        os.unlink(ex_path)


@rust_required
def test_hcl_python_parser_still_works():
    """测试 8：HCL 仍能通过 Python parser 正常解析"""
    from callwarden.parsers import HclParser

    with tempfile.NamedTemporaryFile(mode="w", suffix=".hcl", delete=False, encoding="utf-8") as f:
        f.write(HCL_SAMPLE)
        hcl_path = f.name

    try:
        parser = HclParser()
        result = parser.parse_file(hcl_path)

        assert result["language"] == "hcl"
        symbols = result["symbols"]
        symbol_names = [s["name"] for s in symbols]
        assert len(symbols) >= 1, f"应找到至少 1 个 block，实际 {len(symbols)}"
    finally:
        os.unlink(hcl_path)


# ====================================================================
# 主入口
# ====================================================================

def main():
    print("=" * 60)
    print("L9 Rust multilang parser 补齐测试（15 语言全 Rust 化）")
    print("=" * 60)
    if not _rust_available():
        print("SKIP: callwarden_core 不可导入")
        return
    test_supported_languages_includes_kotlin_swift()
    print("PASS test_supported_languages_includes_kotlin_swift")
    test_elixir_hcl_in_rust_supported()
    print("PASS test_elixir_hcl_in_rust_supported")
    test_rust_parse_kotlin_symbols()
    print("PASS test_rust_parse_kotlin_symbols")
    test_rust_parse_swift_symbols()
    print("PASS test_rust_parse_swift_symbols")
    test_rust_parse_elixir_symbols()
    print("PASS test_rust_parse_elixir_symbols")
    test_rust_parse_hcl_symbols()
    print("PASS test_rust_parse_hcl_symbols")
    test_can_use_rust_parse_all_15_langs()
    print("PASS test_can_use_rust_parse_all_15_langs")
    test_elixir_hcl_use_rust_path()
    print("PASS test_elixir_hcl_use_rust_path")
    test_elixir_python_parser_still_works()
    print("PASS test_elixir_python_parser_still_works")
    test_hcl_python_parser_still_works()
    print("PASS test_hcl_python_parser_still_works")
    print("=" * 60)
    print("=== ALL L9 RUST MULTILANG TESTS PASSED ===")
    print("=" * 60)


if __name__ == "__main__":
    main()
