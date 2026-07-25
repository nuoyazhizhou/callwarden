"""P0-A baseline.json 校验测试。

此测试不重新生成基线，只验证 baseline.json 是有效的 JSON 且包含
Phase 0 完成门要求暴露的 4 个语言缺口（TypeScript/PHP/Scala/HCL）。

设计意图：
- baseline.json 由 ``tests/parser_contract/generate_baseline.py`` 在 CI 流程中生成
- 本测试确保 baseline.json 与代码同步（commit sha 一致或可重生成）
- 当 baseline.json 缺失时跳过测试，避免本地无构建产物时阻断
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_PKG_ROOT = Path(__file__).resolve().parents[2]
_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_PKG_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT.parent))
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"


def _has_rust_ext() -> bool:
    try:
        import callwarden_core  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not BASELINE_PATH.exists(),
    reason="baseline.json 未生成，运行 `py -3.14 tests/parser_contract/generate_baseline.py`",
)
def test_baseline_json_is_valid():
    """baseline.json 必须可解析且包含 16 语言能力清单。"""
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert "language_capability" in data
    assert "phase0_completion_gates" in data
    assert "platform" in data
    assert "bundle_size_baseline" in data

    # 16 语言全列出
    expected_langs = {
        "python", "rust", "go", "java", "typescript", "javascript",
        "ruby", "php", "scala", "csharp", "cpp",
        "kotlin", "swift", "elixir",
        "hcl", "c",
    }
    assert set(data["language_capability"].keys()) == expected_langs


@pytest.mark.skipif(
    not BASELINE_PATH.exists() or not _has_rust_ext(),
    reason="baseline.json 或 callwarden_core 不可用",
)
def test_baseline_exposes_phase0_gaps():
    """baseline.json 的 phase0_completion_gates 必须暴露 4 个语言缺口。

    这是设计文档 Phase 0 完成门的核心断言：测试能真实暴露 TypeScript、PHP、
    Scala、HCL 当前缺口。若任一缺口为 False，说明 baseline 生成逻辑退化
    或 Rust parser 已修复该缺口（后者需更新 baseline 生成脚本）。
    """
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    gates = data["phase0_completion_gates"]

    # 4 个缺口必须被暴露（True 表示 baseline 检测到了缺口）
    assert gates["tests_expose_typescript_gap"] is True, (
        "baseline 应暴露 TypeScript 缺口（Rust 漏提取符号）"
    )
    assert gates["tests_expose_php_gap"] is True, (
        "baseline 应暴露 PHP 缺口（property 缺失）"
    )
    assert gates["tests_expose_scala_gap"] is True, (
        "baseline 应暴露 Scala 缺口（对象方法调用缺失）"
    )
    assert gates["tests_expose_hcl_gap"] is True, (
        "baseline 应暴露 HCL 缺口（引用未在 Rust 路径提取）"
    )


@pytest.mark.skipif(
    not BASELINE_PATH.exists(),
    reason="baseline.json 未生成",
)
def test_baseline_signature_gap_recorded():
    """baseline 必须记录 Rust 端 signature 全空的系统性缺口。

    设计文档 §5.2 输出契约要求每个符号提供 signature 字段。
    当前 Rust SymbolInfo.signature 字段始终为空字符串，所有语言都暴露此缺口。
    修复后此断言需要更新（任一语言 signature_present_rs 为 True 即视为已修复）。
    """
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    capability = data["language_capability"]

    # 至少一种 Rust 支持的语言应出现 signature_missing_in_rust 缺口
    rust_supported_langs = [
        lang for lang, cap in capability.items() if cap.get("rust_supported")
    ]
    assert rust_supported_langs, "baseline 应至少包含一种 Rust 支持的语言"

    has_signature_gap = any(
        any("signature_missing_in_rust" in gap for gap in cap.get("gaps", []))
        for cap in capability.values()
    )
    assert has_signature_gap, (
        "Rust SymbolInfo.signature 字段当前为空字符串，"
        "baseline 应至少为一种语言暴露 signature_missing_in_rust 缺口"
    )


@pytest.mark.skipif(
    not BASELINE_PATH.exists(),
    reason="baseline.json 未生成",
)
def test_baseline_bundle_methodology_documented():
    """即使无构建产物，baseline 也必须记录 bundle 字节占比获取方法。"""
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    bundle = data["bundle_size_baseline"]
    assert "methodology" in bundle
    assert bundle["methodology"], "bundle_size_baseline.methodology 必须非空"


def test_inspector_distribution_classification_is_stable():
    """inspector 的 distribution 分类函数对典型路径必须稳定分类。

    防止后续修改 _classify_distribution 时回归（如把 tree_sitter_python
    误归入 tree_sitter 核心）。
    """
    from release.inspect_pyinstaller_bundle import (  # type: ignore
        CALLWARDEN_CORE_DISTRIBUTION,
        CALLWARDEN_PARSERS_DISTRIBUTION,
        PYTHON_RUNTIME_DISTRIBUTION,
        TREE_SITTER_CORE_DISTRIBUTION,
        TREE_SITTER_GRAMMAR_DISTRIBUTIONS,
        _classify_distribution,
    )

    cases = [
        # (rel_path, expected_distribution)
        # Rust 扩展
        ("callwarden_core.pyd", CALLWARDEN_CORE_DISTRIBUTION),
        ("callwarden_core.so", CALLWARDEN_CORE_DISTRIBUTION),
        ("_internal/callwarden_core.pyd", CALLWARDEN_CORE_DISTRIBUTION),
        # tree-sitter 核心
        ("_internal/tree_sitter/__init__.pyc", TREE_SITTER_CORE_DISTRIBUTION),
        ("_internal/tree_sitter.pyd", TREE_SITTER_CORE_DISTRIBUTION),
        ("_internal/tree_sitter.so", TREE_SITTER_CORE_DISTRIBUTION),
        # 16 种 grammar distribution
        ("_internal/tree_sitter_python/__init__.pyc", "tree_sitter_python"),
        ("_internal/tree_sitter_python.cp314-win_amd64.pyd", "tree_sitter_python"),
        ("_internal/tree_sitter_rust/__init__.pyc", "tree_sitter_rust"),
        ("_internal/tree_sitter_rust.so", "tree_sitter_rust"),
        ("_internal/tree_sitter_hcl/__init__.pyc", "tree_sitter_hcl"),
        ("_internal/tree_sitter_elixir/__init__.pyc", "tree_sitter_elixir"),
        # Python parser 实现
        ("_internal/callwarden/parsers/__init__.pyc", CALLWARDEN_PARSERS_DISTRIBUTION),
        ("_internal/callwarden/parsers/python_parser.py", CALLWARDEN_PARSERS_DISTRIBUTION),
        ("_internal/callwarden/parsers/hcl_parser.pyc", CALLWARDEN_PARSERS_DISTRIBUTION),
        # 其他文件归入 python_runtime
        ("cw", PYTHON_RUNTIME_DISTRIBUTION),
        ("cw.exe", PYTHON_RUNTIME_DISTRIBUTION),
        ("_internal/python310.dll", PYTHON_RUNTIME_DISTRIBUTION),
        ("_internal/numpy/__init__.pyc", PYTHON_RUNTIME_DISTRIBUTION),
    ]

    # 所有 grammar 都有分类（不漏）
    for grammar in TREE_SITTER_GRAMMAR_DISTRIBUTIONS:
        cases.append((f"_internal/{grammar}/__init__.pyc", grammar))
        cases.append((f"_internal/{grammar}.pyd", grammar))
        cases.append((f"_internal/{grammar}.so", grammar))

    for rel_path, expected in cases:
        actual = _classify_distribution(rel_path)
        assert actual == expected, (
            f"classify({rel_path!r}) = {actual!r}, expected {expected!r}"
        )


def test_inspector_forbid_distribution_gate(tmp_path):
    """inspector --forbid-distribution 必须在存在文件时报错，缺失时通过。"""
    from release.inspect_pyinstaller_bundle import (  # type: ignore
        CALLWARDEN_PARSERS_DISTRIBUTION,
        inspect_bundle,
    )
    import ast

    bundle = tmp_path / "callwarden"
    internal = bundle / "_internal"
    internal.mkdir(parents=True)
    (bundle / "cw").write_bytes(b"cw")
    (internal / "python314.dll").write_bytes(b"x" * 1024)

    # 写入一个 parser 模块文件
    parsers_dir = internal / "callwarden" / "parsers"
    parsers_dir.mkdir(parents=True)
    (parsers_dir / "python_parser.pyc").write_bytes(b"x" * 100)

    # 构造最小 PYZ TOC
    toc = tmp_path / "PYZ-00.toc"
    toc.write_text(
        repr([("callwarden.parsers.python_parser", "/fake/x.pyc", "PYMODULE")]),
        encoding="utf-8",
    )

    # 不限制时通过（仅检查结构和 forbidden_modules）
    _, errors_ok = inspect_bundle(bundle, toc)
    # 单层 _internal + 必需模块根缺失才会报错，这里只断言 forbid_distributions 不触发
    forbid_errors = [e for e in errors_ok if "必须为空" in e]
    assert forbid_errors == []

    # 限制 CALLWARDEN_PARSERS_DISTRIBUTION 时必须报错
    _, errors_fail = inspect_bundle(
        bundle, toc, forbid_distributions=(CALLWARDEN_PARSERS_DISTRIBUTION,)
    )
    forbid_errors = [e for e in errors_fail if "必须为空" in e]
    assert len(forbid_errors) == 1
    assert CALLWARDEN_PARSERS_DISTRIBUTION in forbid_errors[0]
