"""P1-E Step 6: Rust-only parser 静态边界门禁。

设计文档：docs/design/rust-only-parser-cutover-plan.md Phase 3 Step 8
> 增加静态门禁：正式模块 import graph 中出现 callwarden.parsers 即失败。

本测试用 AST 扫描生产代码（db/server/cli/analyzers/cicd），确保：
1. 生产路径不再调用 Python parser 工厂 create_parser()（来自 callwarden.parsers）
2. 生产路径不再调用 legacy Python parse 函数：
   - _python_multiprocess_parse
   - _python_parse_single_file
   - _get_or_create_parser
   - _parse_file_worker（仅当其在生产路径被调用时）
3. 生产路径不再实例化 Python tree-sitter parser 类
   （PythonParser/TypeScriptParser/KotlinParser/... 等 16 种语言 parser）

已知例外（P1-E 范围外，文档化保留）：
- db/db_base.py:2132 `self.parser = RustParser()` ——
  RustParser（parsers/rust.py，Python tree-sitter-rust 绑定）用于
  ModuleResolver.resolve_all() 提取 Rust mod_decls。
  Rust 扩展（callwarden_core）尚未返回 mod_decls，模块结构发现暂保留
  Python reference，待后续 phase 统一迁移。
- db/db_build.py:28 / db/db_base.py:14 的 `create_parser` import 带
  `# noqa: F401`，仅保留供 dev reference / 测试 mock，生产路径不调用。
- db/db_build.py 中 _get_or_create_parser / _python_parse_single_file /
  _python_multiprocess_parse / _parse_file_worker 为 legacy 定义，
  生产路径已不再调用（本测试会验证）。

cli/main.py 的 `def create_parser() -> argparse.ArgumentParser` 是同名
但完全不同的函数（CLI 参数解析器构建器），不属于 callwarden.parsers.create_parser，
本测试通过校验调用点所在模块是否有 callwarden.parsers 导入来区分。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Tuple

import pytest


# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 生产代码目录（不含 tests/parsers/docs）
PRODUCTION_DIRS = ["db", "server", "cli", "analyzers", "cicd"]

# 禁止在生产路径调用的 Python parser 工厂 / legacy 函数名
FORBIDDEN_PARSER_CALLS = {
    "_python_multiprocess_parse",
    "_python_parse_single_file",
    "_get_or_create_parser",
}

# 16 种 Python tree-sitter parser 类（实例化即视为 Python parser 调用）
PYTHON_PARSER_CLASSES = {
    "RustParser",
    "TypeScriptParser",
    "PythonParser",
    "KotlinParser",
    "GoParser",
    "JavaParser",
    "CParser",
    "CppParser",
    "CSharpParser",
    "RubyParser",
    "PhpParser",
    "SwiftParser",
    "ScalaParser",
    "HclParser",
    "ElixirParser",
}

# 已知例外：db_base.py 的 RustParser() 用于 ModuleResolver 模块结构发现
# 文档化保留，待 Rust 扩展返回 mod_decls 后迁移
KNOWN_RUSTPARSER_EXCEPTIONS = {
    str(PROJECT_ROOT / "db" / "db_base.py"),
}


# ----------------------------------------------------------------------
# AST 扫描工具
# ----------------------------------------------------------------------


def _collect_production_py_files() -> List[Path]:
    """收集所有生产 .py 文件（db/server/cli/analyzers/cicd，排除 tests）。"""
    files: List[Path] = []
    for dir_name in PRODUCTION_DIRS:
        dir_path = PROJECT_ROOT / dir_name
        if not dir_path.exists():
            continue
        for py_file in dir_path.rglob("*.py"):
            # 掱除 __pycache__
            if "__pycache__" in py_file.parts:
                continue
            files.append(py_file)
    return files


def _collect_legacy_function_ranges(tree: ast.AST) -> List[Tuple[str, int, int]]:
    """收集 legacy Python parse 函数的定义范围 (name, start_line, end_line)。

    这些函数在 db_build.py 中保留为死代码（供 dev reference / 测试），
    其内部的 parser 实例化和相互调用不计入违规。
    """
    legacy_names = FORBIDDEN_PARSER_CALLS | {"_parse_file_worker"}
    ranges: List[Tuple[str, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in legacy_names:
                end_line = getattr(node, "end_lineno", node.lineno) or node.lineno
                ranges.append((node.name, node.lineno, end_line))
    return ranges


def _is_inside_legacy_function(lineno: int, legacy_ranges: List[Tuple[str, int, int]]) -> bool:
    """判断给定行号是否落在某个 legacy 函数定义范围内。"""
    for _name, start, end in legacy_ranges:
        if start <= lineno <= end:
            return True
    return False


def _list_calls(tree: ast.AST) -> List[Tuple[str, int]]:
    """提取 AST 中所有 Call 节点的被调用函数名（仅简单 Name 调用）。"""
    calls: List[Tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # 只关心直接 Name 调用，如 create_parser(...) / _get_or_create_parser(...)
            if isinstance(node.func, ast.Name):
                calls.append((node.func.id, node.lineno))
            # self.parser.parse_file 这类 Attribute 调用不在本门禁范围
            # （ModuleResolver/CallResolver 内部 self.parser 调用属已知例外）
    return calls


def _list_instantiations(tree: ast.AST) -> List[Tuple[str, int]]:
    """提取 AST 中所有 ClassName() 实例化（直接 Name 构造调用）。"""
    instantiations: List[Tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in PYTHON_PARSER_CLASSES:
                instantiations.append((node.func.id, node.lineno))
    return instantiations


def _has_callwarden_parsers_import(tree: ast.AST) -> bool:
    """判断模块是否 import 了 callwarden.parsers（绝对或相对导入）。

    相对导入 `from ..parsers import ...` 也算 callwarden.parsers 依赖。
    """
    for node in ast.walk(tree):
        # 绝对导入: from callwarden.parsers import ... / import callwarden.parsers
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "callwarden.parsers" or alias.name.startswith(
                    "callwarden.parsers."
                ):
                    return True
        elif isinstance(node, ast.ImportFrom):
            # 相对导入: from ..parsers import ... (module=None, level>=2)
            if node.module and (
                node.module == "callwarden.parsers"
                or node.module.startswith("callwarden.parsers.")
            ):
                return True
            # 相对导入 from ..parsers / from .parsers
            if node.level and node.level >= 1 and node.module == "parsers":
                return True
    return False


def _collect_create_parser_calls_with_context(
    tree: ast.AST,
) -> List[Tuple[int, bool]]:
    """提取 create_parser(...) 调用，并标注是否位于 cli/main.py 的 argparse builder 内。

    Returns:
        [(lineno, is_argparse_builder_context)]
        is_argparse_builder_context=True 表示调用点所在模块未导入
        callwarden.parsers，视为 cli 同名函数（argparse builder），不视为违规。
    """
    calls: List[Tuple[int, bool]] = []
    has_parsers_import = _has_callwarden_parsers_import(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "create_parser":
                # cli/main.py 的 create_parser 是本地 argparse builder，
                # 模块内不导入 callwarden.parsers，视为合法同名函数。
                calls.append((node.lineno, not has_parsers_import))
    return calls


# ----------------------------------------------------------------------
# 门禁测试
# ----------------------------------------------------------------------


class TestRustOnlyParserBoundary:
    """P1-E Step 6: 生产代码 Python parser 调用静态门禁。"""

    def test_no_create_parser_factory_calls_in_production(self):
        """生产代码不得调用 callwarden.parsers.create_parser() 工厂。

        cli/main.py 的 def create_parser() 是 argparse builder（同名不同源），
        通过校验调用模块是否导入 callwarden.parsers 区分。
        """
        violations: List[str] = []
        for py_file in _collect_production_py_files():
            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(py_file))
            except (SyntaxError, UnicodeDecodeError):
                continue

            for lineno, is_argparse_ctx in _collect_create_parser_calls_with_context(
                tree
            ):
                if is_argparse_ctx:
                    # cli/main.py 的 argparse builder，合法同名函数
                    continue
                violations.append(
                    f"{py_file}:{lineno} 调用 create_parser()（Python parser 工厂）"
                )

        assert not violations, (
            "生产代码禁止调用 callwarden.parsers.create_parser()，发现违规:\n"
            + "\n".join(violations)
        )

    def test_no_legacy_python_parse_function_calls_in_production(self):
        """生产代码不得调用 legacy Python parse 函数。

        禁止调用的函数：
        - _python_multiprocess_parse
        - _python_parse_single_file
        - _get_or_create_parser

        这些函数在 db_build.py 中保留为 legacy 定义（供 dev reference / 测试），
        但生产路径不得调用。legacy 函数内部的相互调用（死代码内部）不计违规。
        """
        violations: List[str] = []
        for py_file in _collect_production_py_files():
            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(py_file))
            except (SyntaxError, UnicodeDecodeError):
                continue

            legacy_ranges = _collect_legacy_function_ranges(tree)

            for func_name, lineno in _list_calls(tree):
                if func_name in FORBIDDEN_PARSER_CALLS:
                    # 跳过 legacy 函数内部的相互调用（死代码内部）
                    if _is_inside_legacy_function(lineno, legacy_ranges):
                        continue
                    violations.append(
                        f"{py_file}:{lineno} 调用 {func_name}()（legacy Python parse）"
                    )

        assert not violations, (
            "生产代码禁止调用 legacy Python parse 函数，发现违规:\n"
            + "\n".join(violations)
        )

    def test_no_python_parser_class_instantiation_in_production(self):
        """生产代码不得实例化 Python tree-sitter parser 类。

        已知例外：
        1. db/db_base.py 的 RustParser() 用于 ModuleResolver
           模块结构发现（mod_decls 提取），Rust 扩展尚未提供 mod_decls，
           文档化保留待后续 phase 迁移。
        2. db/db_build.py 中 _get_or_create_parser 等 legacy 函数内部的
           parser 实例化（死代码，保留供 dev reference / 测试）。
        """
        violations: List[str] = []
        for py_file in _collect_production_py_files():
            # 跳过已知例外文件（整体豁免，因 RustParser 已文档化）
            if str(py_file) in KNOWN_RUSTPARSER_EXCEPTIONS:
                continue

            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(py_file))
            except (SyntaxError, UnicodeDecodeError):
                continue

            legacy_ranges = _collect_legacy_function_ranges(tree)

            for class_name, lineno in _list_instantiations(tree):
                # 跳过 legacy 函数内部的 parser 实例化（死代码）
                if _is_inside_legacy_function(lineno, legacy_ranges):
                    continue
                violations.append(
                    f"{py_file}:{lineno} 实例化 {class_name}()（Python tree-sitter parser）"
                )

        assert not violations, (
            "生产代码禁止实例化 Python tree-sitter parser 类，发现违规:\n"
            + "\n".join(violations)
        )

    def test_db_base_rustparser_is_documented_exception(self):
        """验证 db_base.py 的 RustParser() 实例化是已知文档化例外。

        这是 ModuleResolver.resolve_all(self.parser) 的依赖，用于 Rust
        模块结构发现。Rust 扩展未返回 mod_decls，暂保留 Python reference。
        """
        db_base = PROJECT_ROOT / "db" / "db_base.py"
        assert db_base.exists(), "db/db_base.py 应存在"

        source = db_base.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(db_base))

        instantiations = _list_instantiations(tree)
        rust_parser_instantiations = [
            (name, line) for name, line in instantiations if name == "RustParser"
        ]

        # 应恰好有 1 处 RustParser() 实例化（self.parser = RustParser()）
        assert len(rust_parser_instantiations) == 1, (
            f"db_base.py 应仅有 1 处 RustParser() 实例化（ModuleResolver 依赖），"
            f"实际 {len(rust_parser_instantiations)} 处: {rust_parser_instantiations}"
        )

        # 验证紧邻有 ModuleResolver / CallResolver 初始化（证明用于模块解析）
        assert "ModuleResolver" in source, "db_base.py 应使用 ModuleResolver"
        assert "CallResolver" in source, "db_base.py 应使用 CallResolver"
        assert "self.parser = RustParser()" in source, (
            "db_base.py 应包含 self.parser = RustParser() 语句"
        )

    def test_legacy_python_parse_functions_are_dead_in_production(self):
        """验证 legacy Python parse 函数在生产路径未被调用（仅定义存在）。

        db_build.py 中 _get_or_create_parser / _python_parse_single_file /
        _python_multiprocess_parse / _parse_file_worker 保留为 legacy 定义，
        但生产路径不得有调用点。这些函数仅被测试代码引用。

        本测试比 test_no_legacy_python_parse_function_calls_in_production 更严格：
        额外覆盖 _parse_file_worker，并确认 legacy 函数仅被自身内部相互调用
        （死代码内部），不被生产路径（如 BuildMixin 方法）调用。
        """
        legacy_funcs = FORBIDDEN_PARSER_CALLS | {"_parse_file_worker"}
        violations: List[str] = []
        for py_file in _collect_production_py_files():
            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(py_file))
            except (SyntaxError, UnicodeDecodeError):
                continue

            legacy_ranges = _collect_legacy_function_ranges(tree)

            for func_name, lineno in _list_calls(tree):
                if func_name in legacy_funcs:
                    # 跳过 legacy 函数内部的相互调用（死代码内部）
                    if _is_inside_legacy_function(lineno, legacy_ranges):
                        continue
                    violations.append(
                        f"{py_file}:{lineno} 调用 {func_name}()（legacy 函数，应为死代码）"
                    )

        assert not violations, (
            "legacy Python parse 函数在生产路径应未被调用，发现违规:\n"
            + "\n".join(violations)
        )

    def test_no_callwarden_parsers_import_in_server_cli_analyzers_cicd(self):
        """server/cli/analyzers/cicd 不得 import callwarden.parsers。

        这些模块是纯消费层，不应直接依赖 parser 实现。
        db/ 目录允许有限导入（db_base.py 的 ModuleResolver/CallResolver/RustParser
        是已知例外；db_build.py 的 create_parser import 带 noqa 供 dev reference）。
        """
        strict_dirs = {"server", "cli", "analyzers", "cicd"}
        violations: List[str] = []
        for py_file in _collect_production_py_files():
            # 仅检查 server/cli/analyzers/cicd（db/ 有已知例外）
            rel = py_file.relative_to(PROJECT_ROOT)
            if rel.parts[0] not in strict_dirs:
                continue

            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(py_file))
            except (SyntaxError, UnicodeDecodeError):
                continue

            if _has_callwarden_parsers_import(tree):
                violations.append(
                    f"{py_file} 导入 callwarden.parsers（server/cli/analyzers/cicd 禁止）"
                )

        assert not violations, (
            "server/cli/analyzers/cicd 禁止 import callwarden.parsers，发现违规:\n"
            + "\n".join(violations)
        )

    def test_db_build_create_parser_import_is_noqa_marked(self):
        """验证 db_build.py 的 create_parser import 带 noqa 标记（dev reference 用途）。

        生产路径不调用 create_parser，但 import 保留供 dev reference / 测试 mock。
        必须有 `# noqa: F401` 标记表明这是有意保留的未使用导入。
        """
        db_build = PROJECT_ROOT / "db" / "db_build.py"
        assert db_build.exists(), "db/db_build.py 应存在"

        source = db_build.read_text(encoding="utf-8")
        # 查找 create_parser 的 import 行
        import_lines = [
            line
            for line in source.splitlines()
            if "create_parser" in line and ("import" in line or "from" in line)
        ]
        assert import_lines, "db_build.py 应有 create_parser import 行"

        # 至少一行带 noqa: F401 标记
        noqa_lines = [line for line in import_lines if "noqa" in line.lower()]
        assert noqa_lines, (
            "db_build.py 的 create_parser import 必须带 # noqa 标记"
            "（表明是 dev reference / 测试 mock 用途，非生产调用）"
        )
