"""L9 Rust multilang parser 补齐测试

验证 Kotlin/Swift 已纳入 Rust multilang parser 路径：
- supported_languages() 包含 kotlin + swift
- parse_file_lang 对 Kotlin/Swift 文件能提取符号
- _can_use_rust_parse 对 kotlin/swift 返回 True
- Elixir/HCL 保持 Python fallback（不在 Rust supported_languages 中）
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
def test_elixir_hcl_not_in_rust_supported():
    """测试 2：Elixir/HCL 不在 Rust supported_languages 中（保持 Python fallback）"""
    from callwarden_core import supported_languages

    langs = supported_languages()
    assert "elixir" not in langs, "elixir 不应在 Rust supported_languages 中"
    assert "hcl" not in langs, "hcl 不应在 Rust supported_languages 中"


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
def test_can_use_rust_parse_kotlin_swift():
    """测试 5：_can_use_rust_parse 对 kotlin/swift 返回 True"""
    from callwarden.db.db_build import _can_use_rust_parse

    assert _can_use_rust_parse("kotlin"), "_can_use_rust_parse('kotlin') 应返回 True"
    assert _can_use_rust_parse("swift"), "_can_use_rust_parse('swift') 应返回 True"


@rust_required
def test_elixir_hcl_use_python_fallback():
    """测试 6：Elixir/HCL 走 Python fallback（_can_use_rust_parse 返回 False）"""
    from callwarden.db.db_build import _can_use_rust_parse

    assert not _can_use_rust_parse("elixir"), "elixir 不应走 Rust 路径"
    assert not _can_use_rust_parse("hcl"), "hcl 不应走 Rust 路径"


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
    print("L9 Rust multilang parser 补齐测试")
    print("=" * 60)
    if not _rust_available():
        print("SKIP: callwarden_core 不可导入")
        return
    test_supported_languages_includes_kotlin_swift()
    print("PASS test_supported_languages_includes_kotlin_swift")
    test_elixir_hcl_not_in_rust_supported()
    print("PASS test_elixir_hcl_not_in_rust_supported")
    test_rust_parse_kotlin_symbols()
    print("PASS test_rust_parse_kotlin_symbols")
    test_rust_parse_swift_symbols()
    print("PASS test_rust_parse_swift_symbols")
    test_can_use_rust_parse_kotlin_swift()
    print("PASS test_can_use_rust_parse_kotlin_swift")
    test_elixir_hcl_use_python_fallback()
    print("PASS test_elixir_hcl_use_python_fallback")
    test_elixir_python_parser_still_works()
    print("PASS test_elixir_python_parser_still_works")
    test_hcl_python_parser_still_works()
    print("PASS test_hcl_python_parser_still_works")
    print("=" * 60)
    print("=== ALL L9 RUST MULTILANG TESTS PASSED ===")
    print("=" * 60)


if __name__ == "__main__":
    main()
