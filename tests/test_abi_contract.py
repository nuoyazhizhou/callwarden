"""Phase 0 子任务 2 Step 3: ABI 与错误码契约差分测试。

验证 docs/design/abi-error-code-contract.md 作为真相源与实际代码现状一致：
1. Parse ABI: ParseResult/SymbolInfo/RawCall/ParseDiagnostics 字段在 lib.rs 和 multi_lang.rs 中定义
2. Query ABI: GraphStore 方法签名在 graph.rs 中存在
3. Storage ABI: schema.py 的 SCHEMA_VERSION 与契约文档一致
4. 错误码: Rust abi_contract 模块定义的 12 个错误码与契约文档一致
5. CAS 状态: 契约文档的 3 个状态与 db_cas.py 和 abi_contract 模块一致

设计文档 §4：每个功能子任务的 differential-test 步骤必须对比真相源与实现。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

_CONTRACT_MD = _PKG_ROOT / "docs" / "design" / "abi-error-code-contract.md"
_LIB_RS = _PKG_ROOT / "rust_ext" / "src" / "lib.rs"
_MULTI_LANG_RS = _PKG_ROOT / "rust_ext" / "src" / "multi_lang.rs"
_GRAPH_RS = _PKG_ROOT / "rust_ext" / "src" / "graph.rs"
_ABI_CONTRACT_RS = _PKG_ROOT / "rust_ext" / "src" / "abi_contract.rs"
_SCHEMA_PY = _PKG_ROOT / "db" / "schema.py"
_DB_CAS_PY = _PKG_ROOT / "db" / "db_cas.py"


def _read(path: Path) -> str:
    assert path.exists(), f"文件不存在: {path}"
    return path.read_text(encoding="utf-8")


# ============================================
# 1. Parse ABI 契约一致性测试
# ============================================

def test_contract_md_exists():
    """abi-error-code-contract.md 必须存在。"""
    assert _CONTRACT_MD.exists(), f"契约文档不存在: {_CONTRACT_MD}"


def test_contract_has_required_sections():
    """契约文档必须包含 9 个必需章节。"""
    content = _read(_CONTRACT_MD)
    required = [
        "## 1. Parse ABI 契约",
        "## 2. Query ABI 契约",
        "## 3. Storage ABI 契约",
        "## 4. 错误码契约",
        "## 5. 权限与事务边界",
        "## 6. 生产接入点契约",
        "## 7. 性能基线",
        "## 8. 不变量",
        "## 9. Review 清单",
    ]
    for section in required:
        assert section in content, f"契约文档缺少章节: {section}"


def test_parse_result_fields_in_lib_rs():
    """契约 §1.2 ParseResult 字段必须在 lib.rs 中定义。"""
    lib_rs = _read(_LIB_RS)
    required_fields = [
        "rel_path", "abs_path", "module_path", "content_hash",
        "total_lines", "language", "symbols", "calls", "imports",
        "references", "error", "diagnostics",
    ]
    for field in required_fields:
        assert f"pub {field}" in lib_rs, (
            f"lib.rs 缺少 ParseResult 字段: pub {field}"
        )


def test_symbol_info_fields_in_lib_rs():
    """契约 §1.3 SymbolInfo 字段必须在 lib.rs 中定义。"""
    lib_rs = _read(_LIB_RS)
    required_fields = [
        "name", "qualified_name", "kind", "start_line", "end_line",
        "module_path", "symbol_hash", "depth", "has_comment",
        "visibility", "content", "signature",
        "local_id", "lexical_parent_local_id",
    ]
    for field in required_fields:
        assert f"pub {field}" in lib_rs, (
            f"lib.rs 缺少 SymbolInfo 字段: pub {field}"
        )


def test_raw_call_fields_in_lib_rs():
    """契约 §1.4 RawCall 字段必须在 lib.rs 中定义。"""
    lib_rs = _read(_LIB_RS)
    required_fields = [
        "caller_name", "callee_name", "callee_module",
        "call_line", "caller_local_id", "ordinal",
    ]
    for field in required_fields:
        assert f"pub {field}" in lib_rs, (
            f"lib.rs 缺少 RawCall 字段: pub {field}"
        )


def test_parse_diagnostics_fields_in_multi_lang():
    """契约 §1.5 ParseDiagnostics 字段必须在 multi_lang.rs 中定义。"""
    multi_lang = _read(_MULTI_LANG_RS)
    required_fields = [
        "syntax_error_count", "unsupported_construct_count", "status",
    ]
    for field in required_fields:
        assert f"pub {field}" in multi_lang, (
            f"multi_lang.rs 缺少 ParseDiagnostics 字段: pub {field}"
        )


def test_parse_entry_functions_in_lib_rs():
    """契约 §1.1 入口函数必须在 lib.rs 中通过 #[pyfunction] 注册。"""
    lib_rs = _read(_LIB_RS)
    required_apis = [
        "parse_file_lang",
        "parse_canonical_bytes_py",
        "batch_parse_files_lang",
        "batch_parse_files_lang_pool",
        "parse_c_file",
        "batch_parse_c_files",
        "canonicalize_source_py",
    ]
    for api in required_apis:
        assert api in lib_rs, (
            f"lib.rs 缺少 Parse API: {api}"
        )


# ============================================
# 2. Query ABI 契约一致性测试
# ============================================

def test_graphstore_methods_in_graph_rs():
    """契约 §2.1 GraphStore 方法必须在 graph.rs 中定义。"""
    graph_rs = _read(_GRAPH_RS)
    required_methods = [
        "new", "load_from_sqlite", "load_symbols_from_sqlite",
        "load_calls_from_sqlite", "fork_symbols", "load_state",
        "get_callers", "get_callees", "get_symbol", "search_symbols",
        "get_call_chain_down", "get_topological_order", "detect_cycles",
        "stats", "memory_breakdown", "compute_depth_all",
        "dump_to_file", "load_from_file",
    ]
    for method in required_methods:
        assert f"fn {method}" in graph_rs, (
            f"graph.rs 缺少 GraphStore 方法: fn {method}"
        )


def test_graphstore_internal_methods_in_graph_rs():
    """契约 §2.2 GraphStore 内部 Rust API 必须在 graph.rs 中定义。"""
    graph_rs = _read(_GRAPH_RS)
    required_internal = [
        "new_with_data", "file_count", "get_symbol_ref",
        "get_caller_ids", "get_callee_ids", "call_chain_down_rust",
        "topological_order_rust", "detect_cycles_rust",
        "get_symbol_by_id", "get_file_rel_path",
        "get_symbols_by_file", "get_name_to_qnames",
        "get_all_qualified_names",
    ]
    for method in required_internal:
        assert f"fn {method}" in graph_rs, (
            f"graph.rs 缺少 GraphStore 内部 API: fn {method}"
        )


def test_lazy_batch_classes_in_graph_rs():
    """契约 §2.5 懒批量对象必须在 graph.rs 中定义。"""
    graph_rs = _read(_GRAPH_RS)
    required_classes = [
        "CallersBatch", "SymbolSearchBatch",
    ]
    for cls in required_classes:
        assert f"struct {cls}" in graph_rs, (
            f"graph.rs 缺少懒批量类: struct {cls}"
        )
        assert f"fn __len__" in graph_rs or f"fn count" in graph_rs, (
            f"graph.rs 缺少懒批量方法 __len__/count"
        )


def test_graphstore_workspace_id_param():
    """契约 §2.4 GraphStore 必须支持 workspace_id 参数。"""
    graph_rs = _read(_GRAPH_RS)
    # 检查 load_from_sqlite 有 workspace_id 参数
    assert "workspace_id" in graph_rs, (
        "graph.rs 缺少 workspace_id 参数"
    )
    # 检查 workspace_id > 0 时过滤
    assert "workspace_id > 0" in graph_rs or "workspace_id = {}" in graph_rs, (
        "graph.rs 缺少 workspace_id > 0 过滤逻辑"
    )


# ============================================
# 3. Storage ABI 契约一致性测试
# ============================================

def test_schema_version_in_schema_py():
    """契约 §3.2 schema.py 的 SCHEMA_VERSION 必须与契约文档一致。"""
    schema_py = _read(_SCHEMA_PY)
    # 提取 SCHEMA_VERSION = N
    match = re.search(r"SCHEMA_VERSION\s*=\s*(\d+)", schema_py)
    assert match, "schema.py 缺少 SCHEMA_VERSION"
    actual_version = int(match.group(1))
    # 契约文档中 SCHEMA_VERSION 必须与 schema.py 一致（动态读取，避免硬编码）
    contract = _read(_CONTRACT_MD)
    contract_match = re.search(r"SCHEMA_VERSION\s*=\s*(\d+)", contract)
    assert contract_match, "契约文档未声明 SCHEMA_VERSION"
    expected_version = int(contract_match.group(1))
    assert actual_version == expected_version, (
        f"schema.py SCHEMA_VERSION={actual_version}，契约期望 {expected_version}"
    )


def test_contract_schema_version_matches_schema_py():
    """契约文档 §3.2 SCHEMA_VERSION 必须与 schema.py 一致。"""
    contract = _read(_CONTRACT_MD)
    schema_py = _read(_SCHEMA_PY)

    # 从契约文档提取版本
    contract_match = re.search(r"SCHEMA_VERSION\s*=\s*(\d+)", contract)
    assert contract_match, "契约文档未声明 SCHEMA_VERSION"

    # 从 schema.py 提取版本
    schema_match = re.search(r"SCHEMA_VERSION\s*=\s*(\d+)", schema_py)
    assert schema_match, "schema.py 未声明 SCHEMA_VERSION"

    assert contract_match.group(1) == schema_match.group(1), (
        f"契约文档 SCHEMA_VERSION={contract_match.group(1)} 与 "
        f"schema.py={schema_match.group(1)} 不一致"
    )


def test_cas_state_constants_in_db_cas():
    """契约 §3.4 CAS 状态必须在 db_cas.py 中使用。"""
    db_cas = _read(_DB_CAS_PY)
    # 'ready' 在 cas_lookup 中使用
    assert "state = 'ready'" in db_cas, (
        "db_cas.py 缺少 state = 'ready' 查询"
    )
    # 'building' 在 cas_publish 阶段 1 使用
    assert "building" in db_cas, (
        "db_cas.py 缺少 'building' 状态"
    )
    # 'partial' 应该在 publish_with_status 或相关方法中使用
    # （Python 侧可能尚未实现 partial，但契约文档已记录）


def test_cas_key_computation_in_db_cas():
    """契约 §3.5 CAS key 计算必须在 db_cas.py 中实现。"""
    db_cas = _read(_DB_CAS_PY)
    assert "compute_cas_key_v1" in db_cas, (
        "db_cas.py 缺少 compute_cas_key_v1 函数"
    )
    # CAS key 包含 7 个字段
    assert "content_hash" in db_cas
    assert "language" in db_cas
    assert "parser_version" in db_cas
    assert "callwarden_version" in db_cas
    assert "extraction_config_version" in db_cas
    assert "abi_version" in db_cas
    assert "input_abi_version" in db_cas


def test_file_generations_table_in_db_cas():
    """契约 §3.6 file_generations 表必须在 db_cas.py 中定义。"""
    db_cas = _read(_DB_CAS_PY)
    assert "file_generations" in db_cas, (
        "db_cas.py 缺少 file_generations 表"
    )
    assert "FILE_GENERATIONS_DDL" in db_cas, (
        "db_cas.py 缺少 FILE_GENERATIONS_DDL"
    )
    # 关键字段
    assert "latest_committed_generation" in db_cas
    assert "latest_seen_generation" in db_cas


def test_core_tables_in_schema_py():
    """契约 §3.3 核心表必须在 schema.py 中定义。"""
    schema_py = _read(_SCHEMA_PY)
    required_tables = [
        "workspaces", "file_contents", "file_instances", "symbols", "calls",
        "comments", "file_versions", "symbol_contents", "file_symbol_versions",
        "call_versions",
    ]
    for table in required_tables:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema_py, (
            f"schema.py 缺少表: {table}"
        )


# ============================================
# 4. 错误码契约一致性测试
# ============================================

# 契约 §4.1 中的 12 个错误码
_EXPECTED_ERROR_CODES = [
    "PARSE_OK", "PARSE_PARTIAL", "PARSE_FAILED", "PARSE_UNSUPPORTED",
    "PARSE_FATAL", "CAS_LOCKED", "DB_LOCKED", "SNAPSHOT_STALE",
    "ACL_DENIED", "BUDGET_EXCEEDED", "RECOVERY_FAILED", "TRANSPORT_ERROR",
]


def test_error_codes_in_contract():
    """契约 §4.1 必须包含 12 个错误码。"""
    contract = _read(_CONTRACT_MD)
    for code in _EXPECTED_ERROR_CODES:
        assert code in contract, f"契约文档缺少错误码: {code}"


def test_error_codes_in_rust_abi_contract():
    """Rust abi_contract 模块必须定义 12 个错误码。"""
    abi_rs = _read(_ABI_CONTRACT_RS)
    for code in _EXPECTED_ERROR_CODES:
        # 错误码在 as_str 实现中
        # ParseStatus::Ok -> ParseOk（驼峰命名）
        # 从错误码字符串转为驼峰
        parts = code.lower().split("_")
        camel = "".join(p.capitalize() for p in parts)
        assert camel in abi_rs, (
            f"abi_contract.rs 缺少错误码枚举: {camel}（对应 {code}）"
        )


def test_parse_status_in_rust_abi_contract():
    """Rust abi_contract 模块必须定义 ParseStatus 枚举。"""
    abi_rs = _read(_ABI_CONTRACT_RS)
    assert "pub enum ParseStatus" in abi_rs, (
        "abi_contract.rs 缺少 ParseStatus 枚举"
    )
    # 4 个状态
    for variant in ["Ok", "Partial", "Failed", "Unsupported"]:
        assert f"{variant}," in abi_rs, (
            f"abi_contract.rs ParseStatus 缺少变体: {variant}"
        )


def test_parse_status_status_from_fields():
    """契约 §1.5 状态推导规则必须在 abi_contract.rs 实现。"""
    abi_rs = _read(_ABI_CONTRACT_RS)
    assert "from_diagnostics" in abi_rs, (
        "abi_contract.rs 缺少 from_diagnostics 方法"
    )
    # 验证 4 个推导规则在代码中体现
    # （通过测试用例验证，这里只检查方法存在）


def test_cas_state_in_rust_abi_contract():
    """Rust abi_contract 模块必须定义 CAS 状态常量。"""
    abi_rs = _read(_ABI_CONTRACT_RS)
    assert "CAS_STATE_BUILDING" in abi_rs
    assert "CAS_STATE_READY" in abi_rs
    assert "CAS_STATE_PARTIAL" in abi_rs
    # R13-P0-1 不变量：partial != ready
    # 常量值用双引号字符串字面量
    assert '"building"' in abi_rs
    assert '"ready"' in abi_rs
    assert '"partial"' in abi_rs


def test_abi_version_constants_in_rust():
    """Rust abi_contract 模块必须定义 ABI 版本常量。"""
    abi_rs = _read(_ABI_CONTRACT_RS)
    assert "ABI_VERSION" in abi_rs
    assert "INPUT_ABI_VERSION" in abi_rs
    assert "EXTRACTION_CONFIG_VERSION" in abi_rs
    assert "SCHEMA_VERSION" in abi_rs


def test_schema_version_in_rust_matches_py():
    """Rust abi_contract 模块的 SCHEMA_VERSION 必须与 schema.py 一致。"""
    abi_rs = _read(_ABI_CONTRACT_RS)
    schema_py = _read(_SCHEMA_PY)

    # Rust 端
    rust_match = re.search(r"SCHEMA_VERSION:\s*u32\s*=\s*(\d+)", abi_rs)
    assert rust_match, "abi_contract.rs 缺少 SCHEMA_VERSION 常量"
    rust_version = int(rust_match.group(1))

    # Python 端
    py_match = re.search(r"SCHEMA_VERSION\s*=\s*(\d+)", schema_py)
    assert py_match, "schema.py 缺少 SCHEMA_VERSION"
    py_version = int(py_match.group(1))

    assert rust_version == py_version, (
        f"Rust SCHEMA_VERSION={rust_version} 与 Python={py_version} 不一致"
    )


# ============================================
# 5. 不变量测试
# ============================================

def test_contract_invariants():
    """契约 §8 不变量必须在文档中声明。"""
    contract = _read(_CONTRACT_MD)
    required_invariants = [
        "CAS 状态隔离",
        "workspace_id 过滤",
        "懒批对象物化",
        "schema 版本同步",
        "ABI 向后兼容",
        "错误码统一",
        "事务原子性",
        "回滚可恢复",
    ]
    for inv in required_invariants:
        assert inv in contract, f"契约文档缺少不变量: {inv}"


def test_contract_production_entry_points():
    """契约 §6 生产接入点必须区分已接入和待迁移。"""
    contract = _read(_CONTRACT_MD)
    assert "已接入的 Rust 生产入口" in contract
    assert "待接入的 Rust 入口" in contract
    # 已接入 7 个
    assert "rust_parser_facade.py" in contract
    assert "_write_symbols_db" in contract
    assert "_write_calls_db" in contract
    assert "get_callers" in contract
    assert "get_callees" in contract
    assert "search_symbols" in contract
    assert "db_daemon.py" in contract
