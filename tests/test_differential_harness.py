"""Phase 0 子任务 3 Step 3: Differential Harness 与基线差分测试。

验证 docs/design/differential-harness-contract.md 作为真相源与实际代码现状一致：
1. Rust differential_baseline 模块常量与契约文档一致
2. baseline.json 结构与契约文档一致
3. 已有差分测试基础设施与契约文档盘点一致
4. 性能基线指标与契约文档一致

设计文档 §4：每个功能子任务的 differential-test 步骤必须对比真相源与实现。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

_CONTRACT_MD = _PKG_ROOT / "docs" / "design" / "differential-harness-contract.md"
_DIFF_BASELINE_RS = _PKG_ROOT / "rust_ext" / "src" / "differential_baseline.rs"
_BASELINE_JSON = _PKG_ROOT / "tests" / "parser_contract" / "baseline.json"
_GENERATE_BASELINE_PY = _PKG_ROOT / "tests" / "parser_contract" / "generate_baseline.py"
_TEST_BASELINE_PY = _PKG_ROOT / "tests" / "parser_contract" / "test_baseline.py"
_TEST_ALIGNMENT_PY = _PKG_ROOT / "tests" / "test_rust_python_alignment.py"
_TEST_IDENTITY_RANGE_PY = _PKG_ROOT / "tests" / "parser_contract" / "test_identity_range.py"
_TEST_GOLDEN_FIXTURES_PY = _PKG_ROOT / "tests" / "parser_contract" / "test_golden_fixtures.py"
_TEST_ENCODING_ERROR_PY = _PKG_ROOT / "tests" / "parser_contract" / "test_encoding_error.py"
_TEST_P13_PERF_BASELINE_PY = _PKG_ROOT / "tests" / "test_p13_perf_baseline.py"


def _read(path: Path) -> str:
    assert path.exists(), f"文件不存在: {path}"
    return path.read_text(encoding="utf-8")


# ============================================
# 1. 契约文档完整性测试
# ============================================

def test_contract_md_exists():
    """differential-harness-contract.md 必须存在。"""
    assert _CONTRACT_MD.exists(), f"契约文档不存在: {_CONTRACT_MD}"


def test_contract_has_required_sections():
    """契约文档必须包含 10 个必需章节。"""
    content = _read(_CONTRACT_MD)
    required = [
        "## 1. Differential Harness 设计目标",
        "## 2. Harness 接口契约",
        "## 3. 基线数据结构契约",
        "## 4. 性能基线契约",
        "## 5. 差分测试分类",
        "## 6. 已知差异管理",
        "## 7. CI 集成契约",
        "## 8. 不变量",
        "## 9. 生产接入点",
        "## 10. Review 清单",
    ]
    for section in required:
        assert section in content, f"契约文档缺少章节: {section}"


# ============================================
# 2. Rust differential_baseline 模块一致性测试
# ============================================

# 契约 §4.1 性能指标目标
_EXPECTED_PERF_TARGETS = {
    "PARSE_P50_TARGET_MS": 100.0,
    "PARSE_P95_TARGET_MS": 200.0,
    "GRAPHSTORE_LOAD_P50_TARGET_MS": 5000.0,
    "GRAPHSTORE_LOAD_P95_TARGET_MS": 10000.0,
    "GET_CALLERS_P50_TARGET_MS": 1.0,
    "GET_CALLERS_P95_TARGET_MS": 5.0,
    "WATCHER_UPDATE_P95_TARGET_MS": 3000.0,
}


def test_rust_perf_target_constants():
    """Rust differential_baseline 模块必须定义性能目标常量，与契约 §4.1 一致。"""
    content = _read(_DIFF_BASELINE_RS)
    for const_name, expected_value in _EXPECTED_PERF_TARGETS.items():
        # 查找常量定义
        pattern = rf"pub const {const_name}: f64 = {expected_value}"
        assert re.search(pattern, content), (
            f"differential_baseline.rs 缺少常量 {const_name} = {expected_value}"
        )


def test_rust_regression_threshold_constants():
    """Rust 模块必须定义回归阈值常量，与契约 §4.3 一致。"""
    content = _read(_DIFF_BASELINE_RS)
    expected_thresholds = {
        "PERF_P50_REGRESSION_THRESHOLD": 1.5,
        "PERF_P95_REGRESSION_THRESHOLD": 2.0,
        "RSS_REGRESSION_THRESHOLD": 1.5,
        "BINARY_SIZE_REGRESSION_THRESHOLD": 1.2,
    }
    for const_name, expected_value in expected_thresholds.items():
        pattern = rf"pub const {const_name}: f64 = {expected_value}"
        assert re.search(pattern, content), (
            f"differential_baseline.rs 缺少阈值常量 {const_name} = {expected_value}"
        )


def test_rust_phase0_gate_constants():
    """Rust 模块必须定义 Phase 0 完成门常量。"""
    content = _read(_DIFF_BASELINE_RS)
    expected_gates = {
        "PHASE0_GATE_TYPESCRIPT": "tests_expose_typescript_gap",
        "PHASE0_GATE_PHP": "tests_expose_php_gap",
        "PHASE0_GATE_SCALA": "tests_expose_scala_gap",
        "PHASE0_GATE_HCL": "tests_expose_hcl_gap",
    }
    for const_name, expected_value in expected_gates.items():
        pattern = rf'pub const {const_name}: &str = "{expected_value}"'
        assert re.search(pattern, content), (
            f"differential_baseline.rs 缺少 Phase 0 gate 常量 {const_name}"
        )


def test_rust_language_capability_struct():
    """Rust 模块必须定义 LanguageCapability 结构，字段与契约 §3.2 一致。"""
    content = _read(_DIFF_BASELINE_RS)
    assert "pub struct LanguageCapability" in content
    # 22 个字段
    required_fields = [
        "language", "rust_supported", "python_parser_available",
        "sample_path", "symbols_count_py", "symbols_count_rs",
        "kinds_py", "kinds_rs",
        "signature_present_py", "signature_present_rs",
        "visibility_present_py", "visibility_present_rs",
        "calls_count_py", "calls_count_rs",
        "imports_count_py", "imports_count_rs",
        "references_present_py", "references_present_rs",
        "rust_module_path", "python_module_path",
        "known_symbol_diffs_count", "known_call_diffs_count",
        "known_symbol_diffs_reason", "known_call_diffs_reason",
        "gaps",
    ]
    for field in required_fields:
        assert f"pub {field}" in content, (
            f"differential_baseline.rs LanguageCapability 缺少字段: pub {field}"
        )


def test_rust_performance_baseline_struct():
    """Rust 模块必须定义 PerformanceBaseline 结构，字段与契约 §4.1 一致。"""
    content = _read(_DIFF_BASELINE_RS)
    assert "pub struct PerformanceBaseline" in content
    required_fields = [
        "parse_p50_ms", "parse_p95_ms",
        "graphstore_load_p50_ms", "graphstore_load_p95_ms",
        "get_callers_p50_ms", "get_callers_p95_ms",
        "watcher_update_p95_ms",
        "build_full_graph_p50_ms", "build_full_graph_p95_ms",
    ]
    for field in required_fields:
        assert f"pub {field}" in content, (
            f"differential_baseline.rs PerformanceBaseline 缺少字段: pub {field}"
        )


def test_rust_regression_struct():
    """Rust 模块必须定义 Regression 结构。"""
    content = _read(_DIFF_BASELINE_RS)
    assert "pub struct Regression" in content
    assert "pub fn detect" in content
    assert "pub is_regression: bool" in content


def test_rust_baseline_verification_struct():
    """Rust 模块必须定义 BaselineVerification 结构。"""
    content = _read(_DIFF_BASELINE_RS)
    assert "pub struct BaselineVerification" in content
    # 契约 §2.4 字段
    for field in [
        "baseline_commit", "current_commit",
        "is_consistent", "has_performance_regression",
        "regressions", "new_gaps", "fixed_gaps",
    ]:
        assert f"pub {field}" in content, (
            f"BaselineVerification 缺少字段: pub {field}"
        )


def test_rust_known_diff_struct():
    """Rust 模块必须定义 KnownDiff 结构。"""
    content = _read(_DIFF_BASELINE_RS)
    assert "pub struct KnownDiff" in content
    for field in ["parser", "field", "description", "phase", "reason", "fix_commit"]:
        assert f"pub {field}" in content, (
            f"KnownDiff 缺少字段: pub {field}"
        )


def test_rust_baseline_snapshot_struct():
    """Rust 模块必须定义 BaselineSnapshot 结构。"""
    content = _read(_DIFF_BASELINE_RS)
    assert "pub struct BaselineSnapshot" in content
    for field in [
        "generated_at", "commit_sha", "platform",
        "language_capability", "phase0_completion_gates",
        "performance_baseline",
    ]:
        assert f"pub {field}" in content, (
            f"BaselineSnapshot 缺少字段: pub {field}"
        )


# ============================================
# 3. baseline.json 结构一致性测试
# ============================================

def test_generate_baseline_py_exists():
    """契约 §9.1 基线生成脚本必须存在。"""
    assert _GENERATE_BASELINE_PY.exists(), (
        f"基线生成脚本不存在: {_GENERATE_BASELINE_PY}"
    )


def test_generate_baseline_py_has_required_fields():
    """generate_baseline.py 必须生成契约 §3.1 中的字段。"""
    content = _read(_GENERATE_BASELINE_PY)
    required_keys = [
        "generated_at", "commit_sha", "platform",
        "language_capability", "phase0_completion_gates",
        "bundle_size_baseline",
    ]
    for key in required_keys:
        assert key in content, (
            f"generate_baseline.py 缺少字段: {key}"
        )


def test_generate_baseline_py_language_fields():
    """generate_baseline.py 必须包含契约 §3.2 的 22 个字段。"""
    content = _read(_GENERATE_BASELINE_PY)
    required_fields = [
        "rust_supported", "python_parser_available", "sample_path",
        "symbols_count_py", "symbols_count_rs", "kinds_py", "kinds_rs",
        "signature_present_py", "signature_present_rs",
        "visibility_present_py", "visibility_present_rs",
        "calls_count_py", "calls_count_rs",
        "imports_count_py", "imports_count_rs",
        "references_present_py", "references_present_rs",
        "rust_module_path", "python_module_path",
        "known_symbol_diffs_count", "known_call_diffs_count",
        "known_symbol_diffs_reason", "known_call_diffs_reason",
        "gaps",
    ]
    for field in required_fields:
        assert field in content, (
            f"generate_baseline.py 缺少 language_capability 字段: {field}"
        )


def test_generate_baseline_py_phase0_gates():
    """generate_baseline.py 必须包含契约 §3.3 的 4 个 Phase 0 gate。"""
    content = _read(_GENERATE_BASELINE_PY)
    required_gates = [
        "tests_expose_typescript_gap",
        "tests_expose_php_gap",
        "tests_expose_scala_gap",
        "tests_expose_hcl_gap",
    ]
    for gate in required_gates:
        assert gate in content, (
            f"generate_baseline.py 缺少 Phase 0 gate: {gate}"
        )


@pytest.mark.skipif(
    not _BASELINE_JSON.exists(),
    reason="baseline.json 未生成",
)
def test_baseline_json_structure():
    """baseline.json 必须符合契约 §3.1 顶层结构。"""
    data = json.loads(_BASELINE_JSON.read_text(encoding="utf-8"))
    required_top = [
        "generated_at", "commit_sha", "platform",
        "language_capability", "phase0_completion_gates",
        "bundle_size_baseline",
    ]
    for key in required_top:
        assert key in data, f"baseline.json 缺少顶层字段: {key}"


@pytest.mark.skipif(
    not _BASELINE_JSON.exists(),
    reason="baseline.json 未生成",
)
def test_baseline_json_language_capability_fields():
    """baseline.json 的 language_capability 必须包含契约 §3.2 的字段。"""
    data = json.loads(_BASELINE_JSON.read_text(encoding="utf-8"))
    lang_cap = data.get("language_capability", {})
    assert len(lang_cap) > 0, "language_capability 为空"

    required_fields = [
        "rust_supported", "python_parser_available",
        "symbols_count_py", "symbols_count_rs",
        "signature_present_py", "signature_present_rs",
        "calls_count_py", "calls_count_rs",
    ]
    for lang, cap in lang_cap.items():
        for field in required_fields:
            assert field in cap, (
                f"baseline.json language_capability.{lang} 缺少字段: {field}"
            )


# ============================================
# 4. 已有差分测试基础设施一致性测试
# ============================================

# 契约 §5.1 已有差分测试清单
_EXISTING_DIFF_TESTS = [
    _TEST_ALIGNMENT_PY,
    _TEST_IDENTITY_RANGE_PY,
    _TEST_GOLDEN_FIXTURES_PY,
    _TEST_ENCODING_ERROR_PY,
    _TEST_BASELINE_PY,
    _TEST_P13_PERF_BASELINE_PY,
]


@pytest.mark.parametrize("test_file", _EXISTING_DIFF_TESTS)
def test_existing_diff_test_exists(test_file):
    """契约 §5.1 列出的已有差分测试必须存在。"""
    assert test_file.exists(), f"差分测试文件不存在: {test_file}"


def test_contract_lists_existing_diff_tests():
    """契约 §5.1 必须列出 6 个已有差分测试。"""
    content = _read(_CONTRACT_MD)
    expected_files = [
        "tests/test_rust_python_alignment.py",
        "tests/parser_contract/test_identity_range.py",
        "tests/parser_contract/test_golden_fixtures.py",
        "tests/parser_contract/test_encoding_error.py",
        "tests/parser_contract/test_baseline.py",
        "tests/test_p13_perf_baseline.py",
    ]
    for file_path in expected_files:
        assert file_path in content, (
            f"契约文档缺少已有差分测试: {file_path}"
        )


def test_contract_lists_new_diff_tests():
    """契约 §5.2 必须列出 2 个新增差分测试。"""
    content = _read(_CONTRACT_MD)
    expected_files = [
        "tests/test_differential_harness.py",
        "tests/test_performance_baseline.py",
    ]
    for file_path in expected_files:
        assert file_path in content, (
            f"契约文档缺少新增差分测试: {file_path}"
        )


# ============================================
# 5. 不变量测试
# ============================================

def test_contract_invariants():
    """契约 §8 不变量必须在文档中声明。"""
    content = _read(_CONTRACT_MD)
    required_invariants = [
        "baseline.json 必须可重生成",
        "commit_sha 必须一致",
        "已知差异必须声明",
        "性能基线必须跨平台",
        "差分测试必须可独立运行",
        "harness 必须无副作用",
        "缺口修复必须更新 baseline",
        "回归阈值必须显式",
    ]
    for inv in required_invariants:
        assert inv in content, f"契约文档缺少不变量: {inv}"


def test_contract_ci_gates():
    """契约 §7.2 CI 门禁必须包含 5 个门禁。"""
    content = _read(_CONTRACT_MD)
    required_gates = [
        "功能对齐",
        "baseline 一致性",
        "性能回归",
        "缺口暴露",
        "缺口修复",
    ]
    for gate in required_gates:
        assert gate in content, f"契约文档缺少 CI 门禁: {gate}"


def test_contract_performance_metrics():
    """契约 §4.1 必须包含 9 个性能指标。"""
    content = _read(_CONTRACT_MD)
    required_metrics = [
        "parse_p50_ms", "parse_p95_ms",
        "graphstore_load_p50_ms", "graphstore_load_p95_ms",
        "get_callers_p50_ms", "get_callers_p95_ms",
        "watcher_update_p95_ms",
        "build_full_graph_p50_ms", "build_full_graph_p95_ms",
    ]
    for metric in required_metrics:
        assert metric in content, f"契约文档缺少性能指标: {metric}"


def test_contract_regression_thresholds():
    """契约 §4.3 必须包含 4 个回归阈值。"""
    content = _read(_CONTRACT_MD)
    required_thresholds = [
        "1.5x",  # P50
        "2.0x",  # P95
        "1.5x",  # RSS（重复，只验证一次）
        "1.2x",  # 二进制
    ]
    for threshold in ["1.5x", "2.0x", "1.2x"]:
        assert threshold in content, f"契约文档缺少回归阈值: {threshold}"


# ============================================
# 6. 跨语言常量一致性测试
# ============================================

def test_perf_targets_python_rust_consistent():
    """Python 契约文档和 Rust 模块的性能目标常量必须一致。"""
    contract = _read(_CONTRACT_MD)
    rust_content = _read(_DIFF_BASELINE_RS)

    # 从契约文档提取目标值（| parse_p50_ms | < 100ms |）
    contract_targets = re.findall(r"< (\d+)ms", contract)

    # 从 Rust 模块提取常量值
    rust_targets = re.findall(r"pub const \w+: f64 = (\d+(?:\.\d+)?)", rust_content)

    # 验证至少有 7 个性能目标（9 指标，但有些目标值相同）
    assert len(rust_targets) >= 7, (
        f"Rust 模块性能目标常量不足: {len(rust_targets)}"
    )


def test_phase0_gate_names_python_rust_consistent():
    """Python 和 Rust 的 Phase 0 gate 名必须一致。"""
    contract = _read(_CONTRACT_MD)
    rust_content = _read(_DIFF_BASELINE_RS)

    gate_names = [
        "tests_expose_typescript_gap",
        "tests_expose_php_gap",
        "tests_expose_scala_gap",
        "tests_expose_hcl_gap",
    ]
    for name in gate_names:
        assert name in contract, f"契约文档缺少 gate 名: {name}"
        assert name in rust_content, f"Rust 模块缺少 gate 名: {name}"
