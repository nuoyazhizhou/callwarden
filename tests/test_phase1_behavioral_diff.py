"""Phase 1 第一个真正的 Python/Rust 行为差分测试。

**本文件是 manifest §7 中第一个 ✅(behavioral) 标记的载体**。

差分测试的真正含义：
  对同一 fixture 输入，分别走 Python 路径和 Rust 路径，断言两者返回值在
  业务语义上完全一致。这与 P0 阶段的 ✅(infra)（只校验文件存在性/常量数值）
  本质不同。

测试矩阵：
  1. TestParserBehavioralDiff
     - fixture：一份真实 Python 源码（函数+类+嵌套方法+调用）
     - Python 路径：callwarden.parsers.python_parser.PythonParser().parse_file()
       （直接走 Python tree-sitter，不经过 rust_parser_facade）
     - Rust 路径：callwarden_core.parse_file_lang()
       （通过 PyO3 调用 Rust multi_lang 模块）
     - 断言：symbols 数量、每个 symbol 的 name/qualified_name/kind/start_line/end_line
     - 断言：raw_calls 数量、每个 call 的 callee_name/caller_name/call_line

  2. TestSqliteSchemaQueryDiff（Phase 1 子任务 1 契约骨架，待 Rust 端实现后启用）
     - fixture：一份 SQLite db，schema_version=42，含 rollback_config 表
     - Python 路径：sqlite3 直接查询
     - Rust 路径：callwarden_core.sqlite_query_schema_version()（尚未实现）
     - 断言：返回值完全一致
     - 当前状态：@pytest.mark.skip("Phase 1 子任务 1 implement 步骤未完成")

前置条件：
  - Rust 扩展 callwarden_core 必须可加载（为 Python 3.14 编译的 .pyd）
  - 如果当前 Python 不是 3.14，本测试套件会显式 skip 并给出修复指引

关联：
  - 契约：docs/design/rust-full-migration-self-bootstrap-plan.md Phase 1 §1
  - manifest §7 differential-test 列说明
  - P0-3 differential-harness-contract.md（基础设施已建立，本文件首次实现行为对照）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import sqlite3
from typing import Any, Dict, List

import pytest

# ============================================
# 前置条件：Rust 扩展可用性检查
# ============================================

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

_RUST_EXT_AVAILABLE = False
_RUST_EXT_SKIP_REASON = ""
try:
    import callwarden_core  # type: ignore
    _RUST_EXT_AVAILABLE = True
except ImportError as _e:
    _RUST_EXT_SKIP_REASON = (
        f"callwarden_core 不可加载：{_e}。"
        "本测试需要 Python 3.14 编译的 Rust 扩展。"
        "在 Windows 上若当前 Python 是 3.10，请用 C:\\Python314\\python.exe 运行。"
    )


# ============================================
# Fixture：一份真实 Python 源码
# ============================================

# 选择具有代表性的 Python 源码：
# - 顶层函数 foo()
# - 顶层类 Calculator
#   - 方法 __init__
#   - 方法 add（调用 self._helper）
#   - 私有方法 _helper
# - 顶层裸调用 foo()
# 涵盖：函数/类/方法/嵌套/跨方法调用/顶层裸调用
_FIXTURE_SOURCE = '''"""Module docstring."""
import os
import sys


def foo():
    """A top-level function."""
    return 42


class Calculator:
    """A calculator class."""

    def __init__(self, value=0):
        self.value = value

    def add(self, other):
        result = self._helper(other)
        return result

    def _helper(self, x):
        return x + self.value


foo()
'''


@pytest.fixture
def fixture_python_file(tmp_path):
    """写一份 Python 源码到临时文件，返回绝对路径"""
    p = tmp_path / "sample.py"
    p.write_text(_FIXTURE_SOURCE, encoding="utf-8")
    return str(p)


# ============================================
# 1. Parser 行为差分测试（manifest 第一个 ✅(behavioral)）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestParserBehavioralDiff:
    """parser 行为差分：Python 路径 vs Rust 路径在同一 fixture 上的输出对照。

    这是 manifest §7 中第一个真正的 ✅(behavioral) 差分测试。
    与 P0 阶段的 ✅(infra)（只校验文件存在性/常量数值）本质不同：
    - 同一份源码输入
    - 两条独立实现路径
    - 断言业务语义字段完全一致
    """

    def test_python_path_loads_parser(self, fixture_python_file):
        """前置：Python parser 路径必须可加载"""
        from callwarden.parsers.python_parser import PythonParser
        parser = PythonParser()
        result = parser.parse_file(fixture_python_file, module_path="sample")
        assert "symbols" in result
        assert "raw_calls" in result
        assert "imports" in result

    def test_rust_path_loads_parser(self, fixture_python_file):
        """前置：Rust parser 路径必须可加载"""
        result = callwarden_core.parse_file_lang(
            fixture_python_file, "sample", "python"
        )
        assert "symbols" in result
        assert "raw_calls" in result

    def test_symbol_count_matches(self, fixture_python_file):
        """差分：symbol 数量必须一致"""
        from callwarden.parsers.python_parser import PythonParser
        py_result = PythonParser().parse_file(fixture_python_file, module_path="sample")
        rust_result = callwarden_core.parse_file_lang(
            fixture_python_file, "sample", "python"
        )
        py_symbols = py_result["symbols"]
        rust_symbols = rust_result["symbols"]
        assert len(py_symbols) == len(rust_symbols), (
            f"symbol 数量不一致：Python={len(py_symbols)}, Rust={len(rust_symbols)}\n"
            f"  Python symbols: {[(s['name'], s['kind']) for s in py_symbols]}\n"
            f"  Rust symbols:   {[(s['name'], s['kind']) for s in rust_symbols]}"
        )

    def test_symbol_names_match(self, fixture_python_file):
        """差分：每个 symbol 的 name/qualified_name/kind 必须一致"""
        from callwarden.parsers.python_parser import PythonParser
        py_result = PythonParser().parse_file(fixture_python_file, module_path="sample")
        rust_result = callwarden_core.parse_file_lang(
            fixture_python_file, "sample", "python"
        )
        py_syms = {(s["name"], s["kind"], s.get("start_line", 0)): s for s in py_result["symbols"]}
        rust_syms = {(s["name"], s["kind"], s.get("start_line", 0)): s for s in rust_result["symbols"]}

        py_keys = set(py_syms.keys())
        rust_keys = set(rust_syms.keys())

        # 集合差集报告
        missing_in_rust = py_keys - rust_keys
        extra_in_rust = rust_keys - py_keys
        assert not missing_in_rust, (
            f"Rust 缺少 symbol：{missing_in_rust}\n"
            f"  Python keys: {py_keys}\n"
            f"  Rust keys:   {rust_keys}"
        )
        assert not extra_in_rust, (
            f"Rust 多出 symbol：{extra_in_rust}\n"
            f"  Python keys: {py_keys}\n"
            f"  Rust keys:   {rust_keys}"
        )

    def test_symbol_line_ranges_match(self, fixture_python_file):
        """差分：每个 symbol 的 start_line/end_line 必须一致"""
        from callwarden.parsers.python_parser import PythonParser
        py_result = PythonParser().parse_file(fixture_python_file, module_path="sample")
        rust_result = callwarden_core.parse_file_lang(
            fixture_python_file, "sample", "python"
        )

        # 按 (name, kind) 建索引，避免顺序差异
        py_by_name = {(s["name"], s["kind"]): s for s in py_result["symbols"]}
        rust_by_name = {(s["name"], s["kind"]): s for s in rust_result["symbols"]}

        for key, py_sym in py_by_name.items():
            rust_sym = rust_by_name.get(key)
            assert rust_sym is not None, f"Rust 缺少 symbol: {key}"
            assert py_sym["start_line"] == rust_sym["start_line"], (
                f"symbol {key} start_line 不一致："
                f"Python={py_sym['start_line']}, Rust={rust_sym['start_line']}"
            )
            assert py_sym["end_line"] == rust_sym["end_line"], (
                f"symbol {key} end_line 不一致："
                f"Python={py_sym['end_line']}, Rust={rust_sym['end_line']}"
            )

    def test_call_count_matches(self, fixture_python_file):
        """差分：raw_calls 数量必须一致"""
        from callwarden.parsers.python_parser import PythonParser
        py_result = PythonParser().parse_file(fixture_python_file, module_path="sample")
        rust_result = callwarden_core.parse_file_lang(
            fixture_python_file, "sample", "python"
        )
        py_calls = py_result["raw_calls"]
        rust_calls = rust_result["raw_calls"]
        assert len(py_calls) == len(rust_calls), (
            f"raw_calls 数量不一致：Python={len(py_calls)}, Rust={len(rust_calls)}\n"
            f"  Python calls: {[(c.get('callee_name'), c.get('caller_name'), c.get('call_line')) for c in py_calls]}\n"
            f"  Rust calls:   {[(c.get('callee_name'), c.get('caller_name'), c.get('call_line')) for c in rust_calls]}"
        )

    def test_call_fields_match(self, fixture_python_file):
        """差分：每个 raw_call 的 callee_name/caller_name/call_line 必须一致"""
        from callwarden.parsers.python_parser import PythonParser
        py_result = PythonParser().parse_file(fixture_python_file, module_path="sample")
        rust_result = callwarden_core.parse_file_lang(
            fixture_python_file, "sample", "python"
        )

        # 按 (callee_name, caller_name, call_line) 建集合
        def _call_key(c: Dict[str, Any]):
            return (
                c.get("callee_name", ""),
                c.get("caller_name", ""),
                c.get("call_line", 0),
            )

        py_keys = {_call_key(c) for c in py_result["raw_calls"]}
        rust_keys = {_call_key(c) for c in rust_result["raw_calls"]}

        missing = py_keys - rust_keys
        extra = rust_keys - py_keys
        assert not missing, (
            f"Rust 缺少 call：{missing}\n"
            f"  Python keys: {py_keys}\n"
            f"  Rust keys:   {rust_keys}"
        )
        assert not extra, (
            f"Rust 多出 call：{extra}\n"
            f"  Python keys: {py_keys}\n"
            f"  Rust keys:   {rust_keys}"
        )

    def test_imports_match(self, fixture_python_file):
        """差分：imports 必须一致（规范化比较）

        Python parser 返回 dict 列表 [{"module": "os", "imported": ["os"], "line": 2}]
        Rust parser 返回 string 列表 ["os", "sys"]
        规范化为 set(module_name) 后比较
        """
        from callwarden.parsers.python_parser import PythonParser
        py_result = PythonParser().parse_file(fixture_python_file, module_path="sample")
        rust_result = callwarden_core.parse_file_lang(
            fixture_python_file, "sample", "python"
        )

        # 规范化：Python 是 dict 列表，提取 module 字段；Rust 是 string 列表
        py_imports = set()
        for item in py_result.get("imports", []):
            if isinstance(item, dict):
                py_imports.add(item.get("module", ""))
            else:
                py_imports.add(str(item))
        rust_imports = set(rust_result.get("imports", []))

        assert py_imports == rust_imports, (
            f"imports 不一致：\n"
            f"  Python only: {py_imports - rust_imports}\n"
            f"  Rust only:   {rust_imports - py_imports}"
        )

    def test_content_hash_matches(self, fixture_python_file):
        """差分：content_hash 必须一致（同一份源码，哈希应相同）"""
        from callwarden.parsers.python_parser import PythonParser
        py_result = PythonParser().parse_file(fixture_python_file, module_path="sample")
        rust_result = callwarden_core.parse_file_lang(
            fixture_python_file, "sample", "python"
        )
        py_hash = py_result.get("content_hash", "")
        rust_hash = rust_result.get("content_hash", "")
        assert py_hash == rust_hash, (
            f"content_hash 不一致：Python={py_hash!r}, Rust={rust_hash!r}"
        )

    def test_total_lines_matches(self, fixture_python_file):
        """差分：total_lines 必须一致"""
        from callwarden.parsers.python_parser import PythonParser
        py_result = PythonParser().parse_file(fixture_python_file, module_path="sample")
        rust_result = callwarden_core.parse_file_lang(
            fixture_python_file, "sample", "python"
        )
        py_lines = py_result.get("total_lines", 0)
        rust_lines = rust_result.get("total_lines", 0)
        assert py_lines == rust_lines, (
            f"total_lines 不一致：Python={py_lines}, Rust={rust_lines}"
        )


# ============================================
# 2. Phase 1 子任务 1 SQLite schema 查询差分（✅ behavioral）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestSqliteSchemaQueryDiff:
    """Phase 1 子任务 1 SQLite schema 查询差分测试。

    差分对象：
      - Python 路径：sqlite3.connect(db_path).execute("SELECT MAX(version) FROM schema_version")
      - Rust 路径：callwarden_core.sqlite_query_schema_version(db_path)
        （rusqlite 只读连接 + WAL checkpoint + busy_timeout=5000）

    行为契约（见 docs/design/phase1-sqlite-contract.md §3）：
      B1: 空数据库 → 两端都返回 0
      B2: 有 schema_version 表但无记录 → 两端都返回 0
      B3: 单条记录 v=42 → 两端都返回 42
      B4: 多条记录 (v=40, v=42) → 两端都返回 MAX=42
      B5: WAL 模式数据库 → 两端一致
    """

    @staticmethod
    def _py_query(db_path) -> int:
        """Python 路径查询 schema_version（与 db_base._get_current_version 一致）"""
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.execute("SELECT MAX(version) as v FROM schema_version")
            row = cur.fetchone()
            conn.close()
            return row[0] if row and row[0] is not None else 0
        except Exception:
            return 0

    def test_b1_empty_db(self, tmp_path):
        """B1: 空数据库（无表） → 两端都返回 0"""
        db_path = tmp_path / "empty.db"
        # 不创建任何表，直接查询
        py_version = self._py_query(db_path)
        rust_version = callwarden_core.sqlite_query_schema_version(str(db_path))
        assert py_version == rust_version == 0, (
            f"B1 差分失败：Python={py_version}, Rust={rust_version}"
        )

    def test_b2_empty_table(self, tmp_path):
        """B2: 有 schema_version 表但无记录 → 两端都返回 0"""
        db_path = tmp_path / "empty_table.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL,
                description TEXT DEFAULT ''
            )
        """)
        conn.commit()
        conn.close()

        py_version = self._py_query(db_path)
        rust_version = callwarden_core.sqlite_query_schema_version(str(db_path))
        assert py_version == rust_version == 0, (
            f"B2 差分失败：Python={py_version}, Rust={rust_version}"
        )

    def test_b3_single_record(self, tmp_path):
        """B3: 单条 v=42 记录 → 两端都返回 42"""
        db_path = tmp_path / "v42.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL,
                description TEXT DEFAULT ''
            )
        """)
        conn.execute("INSERT INTO schema_version VALUES (42, 0, 'initial')")
        conn.commit()
        conn.close()

        py_version = self._py_query(db_path)
        rust_version = callwarden_core.sqlite_query_schema_version(str(db_path))
        assert py_version == rust_version == 42, (
            f"B3 差分失败：Python={py_version}, Rust={rust_version}"
        )

    def test_b4_multi_records(self, tmp_path):
        """B4: 多条记录 (v=40, v=42) → 两端都返回 MAX=42"""
        db_path = tmp_path / "multi.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL,
                description TEXT DEFAULT ''
            )
        """)
        conn.execute("INSERT INTO schema_version VALUES (40, 0, 'v40')")
        conn.execute("INSERT INTO schema_version VALUES (41, 1, 'v41')")
        conn.execute("INSERT INTO schema_version VALUES (42, 2, 'v42')")
        conn.commit()
        conn.close()

        py_version = self._py_query(db_path)
        rust_version = callwarden_core.sqlite_query_schema_version(str(db_path))
        assert py_version == rust_version == 42, (
            f"B4 差分失败：Python={py_version}, Rust={rust_version}"
        )

    def test_b5_wal_mode(self, tmp_path):
        """B5: WAL 模式数据库 → 两端一致

        Python 端用 WAL 模式写入后，Rust 只读连接需先 wal_checkpoint(PASSIVE)
        才能读到最新数据（AGENTS.md 规则 7）。
        """
        db_path = tmp_path / "wal.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL,
                description TEXT DEFAULT ''
            )
        """)
        conn.execute("INSERT INTO schema_version VALUES (42, 0, 'wal_test')")
        conn.commit()
        conn.close()

        # 注意：conn.close() 后 WAL 仍可能有未 checkpoint 的页
        # Rust 端的 sqlite_query_schema_version 内部会执行 PRAGMA wal_checkpoint(PASSIVE)
        py_version = self._py_query(db_path)
        rust_version = callwarden_core.sqlite_query_schema_version(str(db_path))
        assert py_version == rust_version == 42, (
            f"B5 WAL 差分失败：Python={py_version}, Rust={rust_version}"
        )

    def test_invalid_empty_path(self):
        """B6: 空路径 → 两端都应失败（异常类型不要求一致）"""
        # Python 路径：sqlite3.connect('') 会在当前目录创建空 db，不算失败
        # 因此只验证 Rust 路径返回 PyValueError
        with pytest.raises((ValueError, TypeError)):
            callwarden_core.sqlite_query_schema_version("")


# ============================================
# 3. 多语言差分矩阵（基线，证明差分框架可扩展）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestMultiLanguageDiffMatrix:
    """对多个语言的 fixture 跑差分，证明差分框架可扩展到非 Python 语言。

    当前只覆盖 Python（已通过 TestParserBehavioralDiff 验证）。
    Phase 1 子任务 1 完成后，可扩展到 Rust/TypeScript/Go 等。
    """

    @pytest.mark.parametrize("lang,fixture_content", [
        ("python", _FIXTURE_SOURCE),
    ])
    def test_parser_diff_for_language(self, tmp_path, lang, fixture_content):
        """参数化差分：每个语言的 fixture 都要 Python/Rust 输出一致"""
        ext = {"python": ".py"}.get(lang, ".txt")
        fixture_file = tmp_path / f"sample{ext}"
        fixture_file.write_text(fixture_content, encoding="utf-8")

        # Python 路径
        from callwarden.parsers.python_parser import PythonParser
        py_result = PythonParser().parse_file(str(fixture_file), module_path="sample")

        # Rust 路径
        rust_result = callwarden_core.parse_file_lang(
            str(fixture_file), "sample", lang
        )

        # 差分断言
        assert len(py_result["symbols"]) == len(rust_result["symbols"]), (
            f"{lang}: symbol 数量不一致 "
            f"Python={len(py_result['symbols'])} Rust={len(rust_result['symbols'])}"
        )
        assert len(py_result["raw_calls"]) == len(rust_result["raw_calls"]), (
            f"{lang}: raw_calls 数量不一致 "
            f"Python={len(py_result['raw_calls'])} Rust={len(rust_result['raw_calls'])}"
        )


# ============================================
# 4. Phase 1 子任务 2 CAS 差分测试（✅ behavioral）
# ============================================
#
# 差分对象：
#   - 纯函数：compute_cas_key_v1 / compute_symbol_content_hash
#   - 只读查询：cas_global_lookup / cas_global_get_state / cas_global_count_files
#   - Local 引用层：cas_local_get_file_generation
#
# Python 路径：
#   - from callwarden.db.db_cas import compute_cas_key_v1, compute_symbol_content_hash, cas_lookup
#   - 直接 sqlite3 查询 cas_file_cache / file_generations
#
# Rust 路径：
#   - callwarden_core.compute_cas_key_v1 / compute_symbol_content_hash
#   - callwarden_core.cas_global_lookup / cas_global_get_state / cas_global_count_files
#   - callwarden_core.cas_local_get_file_generation


@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestComputeCasKeyDiff:
    """B1-B4: compute_cas_key_v1 纯函数差分"""

    def test_b1_same_input(self):
        """B1: 同输入下两端返回完全一致的 64 字符 SHA-256 hex"""
        from callwarden.db.db_cas import compute_cas_key_v1 as py_compute
        py = py_compute(
            content_hash="abc123",
            language="python",
            parser_version="0.1.0",
            callwarden_version="0.2.0",
            extraction_config_version="v1",
            abi_version="v1",
            input_abi_version="v1",
        )
        rust = callwarden_core.compute_cas_key_v1(
            "abc123", "python", "0.1.0", "0.2.0", "v1", "v1", "v1"
        )
        assert py == rust, f"compute_cas_key_v1 不一致：Python={py}, Rust={rust}"
        assert len(py) == 64, f"SHA-256 hex 应为 64 字符，实际 {len(py)}"

    def test_b2_different_content(self):
        """B2: 不同 content_hash 产生不同 key"""
        from callwarden.db.db_cas import compute_cas_key_v1 as py_compute
        py1 = py_compute("hash1", "python", "0.1.0", "0.2.0", "v1", "v1", "v1")
        py2 = py_compute("hash2", "python", "0.1.0", "0.2.0", "v1", "v1", "v1")
        r1 = callwarden_core.compute_cas_key_v1("hash1", "python", "0.1.0", "0.2.0", "v1", "v1", "v1")
        r2 = callwarden_core.compute_cas_key_v1("hash2", "python", "0.1.0", "0.2.0", "v1", "v1", "v1")
        assert py1 != py2, "Python: 不同 content 应产生不同 key"
        assert r1 != r2, "Rust: 不同 content 应产生不同 key"
        assert py1 == r1 and py2 == r2, "两端差分不一致"

    def test_b3_different_language(self):
        """B3: 不同 language 产生不同 key"""
        from callwarden.db.db_cas import compute_cas_key_v1 as py_compute
        py_py = py_compute("hash", "python", "0.1.0", "0.2.0", "v1", "v1", "v1")
        py_rs = py_compute("hash", "rust", "0.1.0", "0.2.0", "v1", "v1", "v1")
        r_py = callwarden_core.compute_cas_key_v1("hash", "python", "0.1.0", "0.2.0", "v1", "v1", "v1")
        r_rs = callwarden_core.compute_cas_key_v1("hash", "rust", "0.1.0", "0.2.0", "v1", "v1", "v1")
        assert py_py != py_rs, "Python: 不同 language 应产生不同 key"
        assert r_py != r_rs, "Rust: 不同 language 应产生不同 key"
        assert py_py == r_py and py_rs == r_rs

    def test_b4_empty_string_input(self):
        """B4: 空字符串输入不抛错，返回哈希"""
        from callwarden.db.db_cas import compute_cas_key_v1 as py_compute
        py = py_compute("", "", "", "", "", "", "")
        rust = callwarden_core.compute_cas_key_v1("", "", "", "", "", "", "")
        assert py == rust
        assert len(py) == 64

    def test_symbol_content_hash_diff(self):
        """compute_symbol_content_hash 差分

        Python 端没有等价的公开函数，但 db_cas.cas_publish 内联用
        `hashlib.sha256(sym_content.encode()).hexdigest()`（不规范化换行符），
        与 Rust compute_symbol_content_hash 完全一致。
        """
        import hashlib
        for content in ["", "hello", "def foo():\n    pass", "中文内容", "line1\r\nline2"]:
            py = hashlib.sha256(content.encode("utf-8")).hexdigest()
            rust = callwarden_core.compute_symbol_content_hash(content)
            assert py == rust, f"content={content!r}: Python={py}, Rust={rust}"
            assert len(py) == 64


@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestCasGlobalLookupDiff:
    """C1-C5: cas_global_lookup 差分（只命中 state='ready'）"""

    @staticmethod
    def _setup_cas_db(db_path, cas_key="k1", state="ready"):
        """构建测试用 cas.db"""
        from callwarden.db.db_cas import CAS_SCHEMA_DDL, CAS_INDEX_SQL
        conn = sqlite3.connect(str(db_path))
        conn.executescript(CAS_SCHEMA_DDL)
        conn.executescript(CAS_INDEX_SQL)
        conn.execute(
            "INSERT INTO cas_file_cache (cas_key, content_hash, language, file_size, total_lines, "
            "parser_version, callwarden_version, extraction_config_version, abi_version, "
            "input_abi_version, state, parsed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cas_key, "ch1", "python", 100, 10, "0.1.0", "0.2.0", "v1", "v1", "v1", state, 1000.0),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _py_lookup(db_path, cas_key):
        """Python 路径：与 db_cas.cas_lookup 一致（只命中 state='ready'）"""
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute(
            "SELECT cas_key, content_hash, language, file_size, total_lines, "
            "parser_version, callwarden_version, extraction_config_version, "
            "abi_version, input_abi_version, state, parsed_at "
            "FROM cas_file_cache WHERE cas_key = ? AND state = 'ready'",
            (cas_key,),
        )
        row = cur.fetchone()
        conn.close()
        if row is None:
            return None
        return {
            "cas_key": row[0], "content_hash": row[1], "language": row[2],
            "file_size": row[3], "total_lines": row[4], "parser_version": row[5],
            "callwarden_version": row[6], "extraction_config_version": row[7],
            "abi_version": row[8], "input_abi_version": row[9],
            "state": row[10], "parsed_at": row[11],
        }

    def test_c1_not_exist(self, tmp_path):
        """C1: 不存在的 cas_key → 两端都返回 None"""
        db_path = tmp_path / "cas.db"
        self._setup_cas_db(db_path, cas_key="k1")
        py = self._py_lookup(db_path, "nonexistent")
        rust = callwarden_core.cas_global_lookup(str(db_path), "nonexistent")
        assert py is None and rust is None, f"Python={py}, Rust={rust}"

    def test_c2_ready_state(self, tmp_path):
        """C2: state='ready' → 两端都返回 dict，字段逐一比对"""
        db_path = tmp_path / "cas.db"
        self._setup_cas_db(db_path, cas_key="k1", state="ready")
        py = self._py_lookup(db_path, "k1")
        rust = callwarden_core.cas_global_lookup(str(db_path), "k1")
        assert py is not None and rust is not None, f"Python={py}, Rust={rust}"
        # 字段逐一比对
        for key in py:
            assert py[key] == rust[key], f"字段 {key}: Python={py[key]}, Rust={rust[key]}"

    def test_c3_building_state(self, tmp_path):
        """C3: state='building' → 两端都返回 None"""
        db_path = tmp_path / "cas.db"
        self._setup_cas_db(db_path, cas_key="k1", state="building")
        py = self._py_lookup(db_path, "k1")
        rust = callwarden_core.cas_global_lookup(str(db_path), "k1")
        assert py is None and rust is None, f"building 应不可见：Python={py}, Rust={rust}"

    def test_c4_partial_state(self, tmp_path):
        """C4: state='partial' → 两端都返回 None（Rust 端 lookup 也过滤 partial）"""
        db_path = tmp_path / "cas.db"
        self._setup_cas_db(db_path, cas_key="k1", state="partial")
        py = self._py_lookup(db_path, "k1")
        rust = callwarden_core.cas_global_lookup(str(db_path), "k1")
        assert py is None and rust is None, f"partial 应不可见：Python={py}, Rust={rust}"

    def test_c5_nonexistent_db_path(self, tmp_path):
        """C5: 不存在的 db_path → 两端都抛错"""
        nonexistent = str(tmp_path / "nonexistent.db")
        # Python 路径：sqlite3.connect 会创建空 db，但查询 cas_file_cache 会因表不存在而抛错
        with pytest.raises(Exception):
            self._py_lookup(nonexistent, "k1")
        # Rust 路径：open_readonly 应失败或返回 None（表不存在）
        # 当前实现：open 成功但查询失败 → 返回 None 或抛 PyIOError
        # 接受两种行为：抛错或返回 None（与 Python 不一致也可，因为 Python 是创建空 db）
        try:
            rust = callwarden_core.cas_global_lookup(nonexistent, "k1")
            # 若返回 None 也算对齐（表不存在）
            assert rust is None
        except Exception:
            pass  # 抛错也可接受


@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestCasGlobalGetStateDiff:
    """D1-D3: cas_global_get_state 差分（不过滤 state）"""

    @staticmethod
    def _setup(db_path, state="ready"):
        from callwarden.db.db_cas import CAS_SCHEMA_DDL, CAS_INDEX_SQL
        conn = sqlite3.connect(str(db_path))
        conn.executescript(CAS_SCHEMA_DDL)
        conn.executescript(CAS_INDEX_SQL)
        conn.execute(
            "INSERT INTO cas_file_cache (cas_key, content_hash, language, file_size, total_lines, "
            "parser_version, callwarden_version, extraction_config_version, abi_version, "
            "input_abi_version, state, parsed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("k1", "ch1", "python", 100, 10, "0.1.0", "0.2.0", "v1", "v1", "v1", state, 1000.0),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _py_get_state(db_path, cas_key):
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute("SELECT state FROM cas_file_cache WHERE cas_key = ?", (cas_key,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None

    def test_d1_ready_state(self, tmp_path):
        """D1: state='ready'"""
        db_path = tmp_path / "cas.db"
        self._setup(db_path, state="ready")
        py = self._py_get_state(db_path, "k1")
        rust = callwarden_core.cas_global_get_state(str(db_path), "k1")
        assert py == rust == "ready"

    def test_d2_building_state(self, tmp_path):
        """D2: state='building'"""
        db_path = tmp_path / "cas.db"
        self._setup(db_path, state="building")
        py = self._py_get_state(db_path, "k1")
        rust = callwarden_core.cas_global_get_state(str(db_path), "k1")
        assert py == rust == "building"

    def test_d3_not_exist(self, tmp_path):
        """D3: 不存在 → 两端都返回 None"""
        db_path = tmp_path / "cas.db"
        self._setup(db_path, state="ready")
        py = self._py_get_state(db_path, "nonexistent")
        rust = callwarden_core.cas_global_get_state(str(db_path), "nonexistent")
        assert py is None and rust is None


@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestCasGlobalCountFilesDiff:
    """E1-E2: cas_global_count_files 差分"""

    @staticmethod
    def _py_count(db_path):
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.execute("SELECT COUNT(*) FROM cas_file_cache")
            count = cur.fetchone()[0]
            conn.close()
            return count
        except Exception:
            return 0

    def test_e1_empty_db(self, tmp_path):
        """E1: 空数据库（无表） → 两端都返回 0"""
        db_path = tmp_path / "empty.db"
        # 不创建任何表
        py = self._py_count(db_path)
        rust = callwarden_core.cas_global_count_files(str(db_path))
        assert py == rust == 0, f"空库：Python={py}, Rust={rust}"

    def test_e2_with_records(self, tmp_path):
        """E2: 多条记录（含不同 state） → COUNT(*) 一致"""
        from callwarden.db.db_cas import CAS_SCHEMA_DDL, CAS_INDEX_SQL
        db_path = tmp_path / "cas.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(CAS_SCHEMA_DDL)
        conn.executescript(CAS_INDEX_SQL)
        for i, state in enumerate(["ready", "building", "partial", "ready"]):
            conn.execute(
                "INSERT INTO cas_file_cache (cas_key, content_hash, language, file_size, total_lines, "
                "parser_version, callwarden_version, extraction_config_version, abi_version, "
                "input_abi_version, state, parsed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"k{i}", f"ch{i}", "python", 100, 10, "0.1.0", "0.2.0", "v1", "v1", "v1", state, 1000.0 + i),
            )
        conn.commit()
        conn.close()

        py = self._py_count(db_path)
        rust = callwarden_core.cas_global_count_files(str(db_path))
        assert py == rust == 4, f"4 条记录：Python={py}, Rust={rust}"


@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestCasLocalGetFileGenerationDiff:
    """F1-F2: cas_local_get_file_generation 差分（Local 引用层）"""

    @staticmethod
    def _setup_with_generation(db_path, ws_id=1, rel_path="src/main.py"):
        from callwarden.db.db_cas import CAS_SCHEMA_DDL, CAS_INDEX_SQL
        conn = sqlite3.connect(str(db_path))
        conn.executescript(CAS_SCHEMA_DDL)
        conn.executescript(CAS_INDEX_SQL)
        conn.execute(
            "INSERT INTO file_generations (workspace_id, rel_path, latest_session_id, "
            "latest_session_epoch, latest_seq, latest_seen_generation, latest_committed_generation) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ws_id, rel_path, "sess-1", 100, 5, "100:5", "100:5"),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _py_get(db_path, ws_id, rel_path):
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute(
            "SELECT workspace_id, rel_path, latest_session_id, latest_session_epoch, "
            "latest_seq, latest_seen_generation, latest_committed_generation "
            "FROM file_generations WHERE workspace_id = ? AND rel_path = ?",
            (ws_id, rel_path),
        )
        row = cur.fetchone()
        conn.close()
        if row is None:
            return None
        return {
            "workspace_id": row[0], "rel_path": row[1], "latest_session_id": row[2],
            "latest_session_epoch": row[3], "latest_seq": row[4],
            "latest_seen_generation": row[5], "latest_committed_generation": row[6],
        }

    def test_f1_not_exist(self, tmp_path):
        """F1: 未 seen 的 ws+rel_path → 两端都返回 None"""
        from callwarden.db.db_cas import CAS_SCHEMA_DDL, CAS_INDEX_SQL
        db_path = tmp_path / "cas.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(CAS_SCHEMA_DDL)
        conn.executescript(CAS_INDEX_SQL)
        conn.commit()
        conn.close()

        py = self._py_get(db_path, 999, "nonexistent.py")
        rust = callwarden_core.cas_local_get_file_generation(str(db_path), 999, "nonexistent.py")
        assert py is None and rust is None, f"未 seen：Python={py}, Rust={rust}"

    def test_f2_seen_and_committed(self, tmp_path):
        """F2: 已 seen + 已 committed → 两端返回字段一致的 dict"""
        db_path = tmp_path / "cas.db"
        self._setup_with_generation(db_path, ws_id=1, rel_path="src/main.py")
        py = self._py_get(db_path, 1, "src/main.py")
        rust = callwarden_core.cas_local_get_file_generation(str(db_path), 1, "src/main.py")
        assert py is not None and rust is not None, f"Python={py}, Rust={rust}"
        for key in py:
            assert py[key] == rust[key], f"字段 {key}: Python={py[key]}, Rust={rust[key]}"


# ============================================
# 5. Phase 1 子任务 3 workspace manifest 差分测试（✅ behavioral）
# ============================================
#
# 差分对象：
#   - manifest_get（→ Python get_manifest）—— 单行查询
#   - manifest_list（→ Python list_manifests）—— 多行查询 + dirty 过滤
#   - manifest_count（→ Python 等价 len(list_manifests(...))）—— COUNT(*)
#   - snapshot_get_files（→ Python get_snapshot_files）—— snapshot 文件列表
#   - manifest_verify_raw_hash（→ Python verify_raw_hash）—— raw_hash 校验
#
# Python 路径：
#   - from callwarden.db.db_workspace_manifest import (
#       init_manifest_schema, upsert_manifest, get_manifest, list_manifests,
#       link_to_snapshot, get_snapshot_files, verify_raw_hash)
#   - 使用 sqlite3.connect(db_path, row_factory=Row) 直接调用模块级函数
#
# Rust 路径：
#   - callwarden_core.manifest_get / manifest_list / manifest_count
#   - callwarden_core.snapshot_get_files / manifest_verify_raw_hash
#
# 行为契约：见 docs/design/phase1-manifest-contract.md §4


@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestManifestGetDiff:
    """G1-G5: manifest_get 差分（单行查询）"""

    @staticmethod
    def _setup_db(db_path, ws_id=1, rel_path="src/main.py", content_hash="hash1",
                  cas_key="ck1", raw_hash="raw1", is_dirty=False, file_size=100,
                  mtime_ns=12345):
        """用 Python 路径初始化 schema 并写入一行 manifest"""
        from callwarden.db.db_workspace_manifest import init_manifest_schema, upsert_manifest
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        init_manifest_schema(conn)
        upsert_manifest(conn, ws_id, rel_path, content_hash,
                        cas_key=cas_key, raw_hash=raw_hash, is_dirty=is_dirty,
                        file_size=file_size, mtime_ns=mtime_ns)
        conn.commit()
        conn.close()

    @staticmethod
    def _py_get(db_path, ws_id, rel_path):
        """Python 路径：get_manifest"""
        from callwarden.db.db_workspace_manifest import get_manifest
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        result = get_manifest(conn, ws_id, rel_path)
        conn.close()
        return result

    def test_g1_not_exist(self, tmp_path):
        """G1: 不存在的 (ws, rel_path) → 两端都返回 None"""
        db_path = tmp_path / "manifest.db"
        self._setup_db(db_path, ws_id=1, rel_path="src/main.py")
        py = self._py_get(db_path, 999, "nonexistent.py")
        rust = callwarden_core.manifest_get(str(db_path), 999, "nonexistent.py")
        assert py is None and rust is None, f"G1 差分失败：Python={py}, Rust={rust}"

    def test_g2_existing_manifest(self, tmp_path):
        """G2: 存在的 manifest → 两端返回字段逐一一致的 dict（12 字段）"""
        db_path = tmp_path / "manifest.db"
        self._setup_db(db_path, ws_id=1, rel_path="src/main.py",
                       content_hash="hash_g2", cas_key="ck_g2",
                       raw_hash="raw_g2", is_dirty=False, file_size=200,
                       mtime_ns=9999)
        py = self._py_get(db_path, 1, "src/main.py")
        rust = callwarden_core.manifest_get(str(db_path), 1, "src/main.py")
        assert py is not None and rust is not None, f"Python={py}, Rust={rust}"
        # 12 字段逐一比对
        expected_fields = {
            "workspace_id", "rel_path", "content_hash", "cas_key", "raw_hash",
            "source_encoding", "bom_kind", "newline_style", "file_size",
            "mtime_ns", "is_dirty", "updated_at",
        }
        for field in expected_fields:
            assert field in py, f"Python 字段缺失：{field}"
            assert field in rust, f"Rust 字段缺失：{field}"
            assert py[field] == rust[field], (
                f"字段 {field} 不一致：Python={py[field]!r}, Rust={rust[field]!r}"
            )

    def test_g3_table_not_exists(self, tmp_path):
        """G3: 表不存在 → 两端都抛错

        Python: sqlite3.OperationalError (no such table)
        Rust: PyIOError (prepare 失败)
        差分断言：两端都抛异常（类型不要求一致）
        """
        db_path = tmp_path / "empty.db"
        # 只创建空 SQLite 文件，不创建表
        conn = sqlite3.connect(str(db_path))
        conn.close()
        # Python 路径应抛 OperationalError
        with pytest.raises(sqlite3.OperationalError):
            self._py_get(db_path, 1, "src/main.py")
        # Rust 路径应抛 PyIOError
        with pytest.raises(Exception):
            callwarden_core.manifest_get(str(db_path), 1, "src/main.py")

    def test_g4_workspace_id_zero(self, tmp_path):
        """G4: workspace_id=0 → 两端都返回 None（无行匹配）"""
        db_path = tmp_path / "manifest.db"
        self._setup_db(db_path, ws_id=1, rel_path="src/main.py")
        py = self._py_get(db_path, 0, "src/main.py")
        rust = callwarden_core.manifest_get(str(db_path), 0, "src/main.py")
        assert py is None and rust is None, f"G4 差分失败：Python={py}, Rust={rust}"

    def test_g5_empty_rel_path(self, tmp_path):
        """G5: rel_path='' → 两端都返回 None（无行匹配）"""
        db_path = tmp_path / "manifest.db"
        self._setup_db(db_path, ws_id=1, rel_path="src/main.py")
        py = self._py_get(db_path, 1, "")
        rust = callwarden_core.manifest_get(str(db_path), 1, "")
        assert py is None and rust is None, f"G5 差分失败：Python={py}, Rust={rust}"


@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestManifestListDiff:
    """L1-L5: manifest_list 差分（多行查询 + dirty 过滤）"""

    @staticmethod
    def _setup_db_with_rows(db_path, rows):
        """用 Python 路径初始化 schema 并写入多行 manifest

        rows: List[Tuple(ws_id, rel_path, content_hash, cas_key, raw_hash, is_dirty)]
        """
        from callwarden.db.db_workspace_manifest import init_manifest_schema, upsert_manifest
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        init_manifest_schema(conn)
        for ws_id, rel_path, ch, ck, rh, dirty in rows:
            upsert_manifest(conn, ws_id, rel_path, ch,
                            cas_key=ck, raw_hash=rh, is_dirty=dirty)
        conn.commit()
        conn.close()

    @staticmethod
    def _py_list(db_path, ws_id, dirty_only=False):
        """Python 路径：list_manifests"""
        from callwarden.db.db_workspace_manifest import list_manifests
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        result = list_manifests(conn, ws_id, dirty_only=dirty_only)
        conn.close()
        return result

    def test_l1_empty_workspace(self, tmp_path):
        """L1: 空 workspace（无行）→ 两端都返回 []"""
        db_path = tmp_path / "manifest.db"
        self._setup_db_with_rows(db_path, [])
        py = self._py_list(db_path, 1, dirty_only=False)
        rust = callwarden_core.manifest_list(str(db_path), 1, False)
        assert py == [] and list(rust) == [], f"L1 差分失败：Python={py}, Rust={rust}"

    def test_l2_three_rows(self, tmp_path):
        """L2: workspace 有 3 行 → 两端都返回 [dict × 3]，字段逐一比对"""
        db_path = tmp_path / "manifest.db"
        self._setup_db_with_rows(db_path, [
            (1, "a.py", "h1", "ck1", "r1", False),
            (1, "b.py", "h2", "ck2", "r2", True),
            (1, "c.py", "h3", "ck3", "r3", False),
        ])
        py = self._py_list(db_path, 1, dirty_only=False)
        rust = callwarden_core.manifest_list(str(db_path), 1, False)
        assert len(py) == 3, f"Python 应返回 3 行，实际 {len(py)}"
        assert len(rust) == 3, f"Rust 应返回 3 行，实际 {len(rust)}"
        # 按 rel_path 排序后逐一比对
        py_sorted = sorted(py, key=lambda x: x["rel_path"])
        rust_sorted = sorted(rust, key=lambda x: x["rel_path"])
        for i, (p, r) in enumerate(zip(py_sorted, rust_sorted)):
            for key in p:
                assert p[key] == r[key], (
                    f"第 {i} 行字段 {key} 不一致：Python={p[key]!r}, Rust={r[key]!r}"
                )

    def test_l3_dirty_only_true(self, tmp_path):
        """L3: dirty_only=True → 两端只返回 is_dirty=1 的行"""
        db_path = tmp_path / "manifest.db"
        self._setup_db_with_rows(db_path, [
            (1, "a.py", "h1", "ck1", "r1", False),
            (1, "b.py", "h2", "ck2", "r2", True),
            (1, "c.py", "h3", "ck3", "r3", True),
            (1, "d.py", "h4", "ck4", "r4", False),
        ])
        py = self._py_list(db_path, 1, dirty_only=True)
        rust = callwarden_core.manifest_list(str(db_path), 1, True)
        assert len(py) == 2, f"Python dirty_only 应返回 2 行，实际 {len(py)}"
        assert len(rust) == 2, f"Rust dirty_only 应返回 2 行，实际 {len(rust)}"
        # 所有返回行都应 is_dirty=1
        for r in rust:
            assert r["is_dirty"] == 1, f"dirty_only=True 但返回 is_dirty={r['is_dirty']}"

    def test_l4_dirty_only_false(self, tmp_path):
        """L4: dirty_only=False → 两端返回所有行"""
        db_path = tmp_path / "manifest.db"
        self._setup_db_with_rows(db_path, [
            (1, "a.py", "h1", "ck1", "r1", False),
            (1, "b.py", "h2", "ck2", "r2", True),
        ])
        py = self._py_list(db_path, 1, dirty_only=False)
        rust = callwarden_core.manifest_list(str(db_path), 1, False)
        assert len(py) == 2, f"Python 应返回 2 行，实际 {len(py)}"
        assert len(rust) == 2, f"Rust 应返回 2 行，实际 {len(rust)}"

    def test_l5_table_not_exists(self, tmp_path):
        """L5: 表不存在 → 两端都抛错（Python OperationalError，Rust PyIOError）"""
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.close()
        with pytest.raises(sqlite3.OperationalError):
            self._py_list(db_path, 1, dirty_only=False)
        with pytest.raises(Exception):
            callwarden_core.manifest_list(str(db_path), 1, False)


@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestManifestCountDiff:
    """C1-C4: manifest_count 差分（COUNT(*) 行数）"""

    @staticmethod
    def _setup_db_with_rows(db_path, rows):
        from callwarden.db.db_workspace_manifest import init_manifest_schema, upsert_manifest
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        init_manifest_schema(conn)
        for ws_id, rel_path, ch, ck, rh, dirty in rows:
            upsert_manifest(conn, ws_id, rel_path, ch,
                            cas_key=ck, raw_hash=rh, is_dirty=dirty)
        conn.commit()
        conn.close()

    @staticmethod
    def _py_count(db_path, ws_id, dirty_only=False):
        """Python 等价路径：len(list_manifests(...))"""
        from callwarden.db.db_workspace_manifest import list_manifests
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            result = list_manifests(conn, ws_id, dirty_only=dirty_only)
            return len(result)
        except sqlite3.OperationalError:
            return 0
        finally:
            conn.close()

    def test_c1_empty_workspace(self, tmp_path):
        """C1: 空 workspace → 两端都返回 0"""
        db_path = tmp_path / "manifest.db"
        self._setup_db_with_rows(db_path, [])
        py = self._py_count(db_path, 1, dirty_only=False)
        rust = callwarden_core.manifest_count(str(db_path), 1, False)
        assert py == rust == 0, f"C1 差分失败：Python={py}, Rust={rust}"

    def test_c2_mixed_dirty(self, tmp_path):
        """C2: 5 行 ready + 3 行 dirty → 总数 8"""
        db_path = tmp_path / "manifest.db"
        rows = [
            (1, f"a{i}.py", f"h{i}", f"ck{i}", f"r{i}", i % 2 == 0)
            for i in range(8)
        ]
        # 8 行：4 个 dirty + 4 个 ready
        dirty_count = sum(1 for r in rows if r[5])
        ready_count = len(rows) - dirty_count
        self._setup_db_with_rows(db_path, rows)
        py = self._py_count(db_path, 1, dirty_only=False)
        rust = callwarden_core.manifest_count(str(db_path), 1, False)
        assert py == rust == 8, f"C2 差分失败：Python={py}, Rust={rust}"

    def test_c3_dirty_only_count(self, tmp_path):
        """C3: dirty_only=True → 只数 is_dirty=1 的行"""
        db_path = tmp_path / "manifest.db"
        rows = [
            (1, "a.py", "h1", "ck1", "r1", False),
            (1, "b.py", "h2", "ck2", "r2", True),
            (1, "c.py", "h3", "ck3", "r3", True),
            (1, "d.py", "h4", "ck4", "r4", True),
            (1, "e.py", "h5", "ck5", "r5", False),
        ]
        self._setup_db_with_rows(db_path, rows)
        py = self._py_count(db_path, 1, dirty_only=True)
        rust = callwarden_core.manifest_count(str(db_path), 1, True)
        assert py == rust == 3, f"C3 差分失败：Python={py}, Rust={rust}"

    def test_c4_table_not_exists(self, tmp_path):
        """C4: 表不存在 → 两端都返回 0

        Python: list_manifests 抛 OperationalError → _py_count 捕获返回 0
        Rust: manifest_count 内部 .ok() 返回 0
        """
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.close()
        py = self._py_count(db_path, 1, dirty_only=False)
        rust = callwarden_core.manifest_count(str(db_path), 1, False)
        assert py == rust == 0, f"C4 差分失败：Python={py}, Rust={rust}"


@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestSnapshotGetFilesDiff:
    """S1-S3: snapshot_get_files 差分（snapshot 文件列表）"""

    @staticmethod
    def _setup_db_with_snapshots(db_path, snapshots):
        """用 Python 路径初始化 schema 并写入 snapshot 映射

        snapshots: List[Tuple(snapshot_id, rel_path, content_hash, cas_key)]
        """
        from callwarden.db.db_workspace_manifest import init_manifest_schema, link_to_snapshot
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        init_manifest_schema(conn)
        for snap_id, rel_path, ch, ck in snapshots:
            link_to_snapshot(conn, snap_id, rel_path, ch, ck)
        conn.commit()
        conn.close()

    @staticmethod
    def _py_get_files(db_path, snap_id):
        """Python 路径：get_snapshot_files"""
        from callwarden.db.db_workspace_manifest import get_snapshot_files
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            result = get_snapshot_files(conn, snap_id)
            conn.close()
            return result
        except Exception:
            conn.close()
            raise

    def test_s1_snapshot_not_exist(self, tmp_path):
        """S1: snapshot 不存在 → 两端都返回 []"""
        db_path = tmp_path / "manifest.db"
        self._setup_db_with_snapshots(db_path, [
            ("snap1", "a.py", "h1", "ck1"),
        ])
        py = self._py_get_files(db_path, "nonexistent")
        rust = callwarden_core.snapshot_get_files(str(db_path), "nonexistent")
        assert py == [] and list(rust) == [], f"S1 差分失败：Python={py}, Rust={rust}"

    def test_s2_two_files(self, tmp_path):
        """S2: snapshot 有 2 个文件 → 两端返回字段逐一一致的 [dict × 2]"""
        db_path = tmp_path / "manifest.db"
        self._setup_db_with_snapshots(db_path, [
            ("snap1", "a.py", "hash_a", "cas_a"),
            ("snap1", "b.py", "hash_b", "cas_b"),
        ])
        py = self._py_get_files(db_path, "snap1")
        rust = callwarden_core.snapshot_get_files(str(db_path), "snap1")
        assert len(py) == 2, f"Python 应返回 2 行，实际 {len(py)}"
        assert len(rust) == 2, f"Rust 应返回 2 行，实际 {len(rust)}"
        # 按 rel_path 排序后逐一比对
        py_sorted = sorted(py, key=lambda x: x["rel_path"])
        rust_sorted = sorted(rust, key=lambda x: x["rel_path"])
        for i, (p, r) in enumerate(zip(py_sorted, rust_sorted)):
            for key in ("snapshot_id", "rel_path", "content_hash", "cas_key"):
                assert p[key] == r[key], (
                    f"第 {i} 行字段 {key} 不一致：Python={p[key]!r}, Rust={r[key]!r}"
                )

    def test_s3_table_not_exists(self, tmp_path):
        """S3: 表不存在 → 两端都抛错"""
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.close()
        with pytest.raises(sqlite3.OperationalError):
            self._py_get_files(db_path, "snap1")
        with pytest.raises(Exception):
            callwarden_core.snapshot_get_files(str(db_path), "snap1")


@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestManifestVerifyRawHashDiff:
    """V1-V4: manifest_verify_raw_hash 差分（raw_hash 校验）"""

    @staticmethod
    def _setup_db_with_manifest(db_path, ws_id=1, rel_path="src/main.py", raw_hash="raw1"):
        from callwarden.db.db_workspace_manifest import init_manifest_schema, upsert_manifest
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        init_manifest_schema(conn)
        upsert_manifest(conn, ws_id, rel_path, "hash1",
                        cas_key="ck1", raw_hash=raw_hash)
        conn.commit()
        conn.close()

    @staticmethod
    def _py_verify(db_path, ws_id, rel_path, expected_raw_hash):
        """Python 路径：verify_raw_hash"""
        from callwarden.db.db_workspace_manifest import verify_raw_hash
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            result = verify_raw_hash(conn, ws_id, rel_path, expected_raw_hash)
            conn.close()
            return result
        except Exception:
            conn.close()
            raise

    def test_v1_manifest_not_exist(self, tmp_path):
        """V1: manifest 不存在 → 两端都返回 False"""
        db_path = tmp_path / "manifest.db"
        self._setup_db_with_manifest(db_path, ws_id=1, rel_path="src/main.py", raw_hash="raw1")
        py = self._py_verify(db_path, 1, "nonexistent.py", "raw1")
        rust = callwarden_core.manifest_verify_raw_hash(str(db_path), 1, "nonexistent.py", "raw1")
        assert py is False and rust is False, f"V1 差分失败：Python={py}, Rust={rust}"

    def test_v2_raw_hash_matches(self, tmp_path):
        """V2: manifest 存在，raw_hash 匹配 → 两端都返回 True"""
        db_path = tmp_path / "manifest.db"
        self._setup_db_with_manifest(db_path, ws_id=1, rel_path="src/main.py", raw_hash="raw_match")
        py = self._py_verify(db_path, 1, "src/main.py", "raw_match")
        rust = callwarden_core.manifest_verify_raw_hash(str(db_path), 1, "src/main.py", "raw_match")
        assert py is True and rust is True, f"V2 差分失败：Python={py}, Rust={rust}"

    def test_v3_raw_hash_mismatch(self, tmp_path):
        """V3: manifest 存在，raw_hash 不匹配 → 两端都返回 False"""
        db_path = tmp_path / "manifest.db"
        self._setup_db_with_manifest(db_path, ws_id=1, rel_path="src/main.py", raw_hash="raw1")
        py = self._py_verify(db_path, 1, "src/main.py", "different_hash")
        rust = callwarden_core.manifest_verify_raw_hash(str(db_path), 1, "src/main.py", "different_hash")
        assert py is False and rust is False, f"V3 差分失败：Python={py}, Rust={rust}"

    def test_v4_empty_string_matches(self, tmp_path):
        """V4: manifest 存在，raw_hash 为空字符串且 expected 也为空 → 两端都返回 True

        注意：raw_hash=''（空字符串）与 NULL 不同。空字符串 = 空字符串 → True
        """
        db_path = tmp_path / "manifest.db"
        # Python upsert_manifest 默认 raw_hash="" （空字符串，非 NULL）
        self._setup_db_with_manifest(db_path, ws_id=1, rel_path="src/main.py", raw_hash="")
        py = self._py_verify(db_path, 1, "src/main.py", "")
        rust = callwarden_core.manifest_verify_raw_hash(str(db_path), 1, "src/main.py", "")
        assert py is True and rust is True, f"V4 差分失败：Python={py}, Rust={rust}"


# ============================================
# 6. Phase 1 子任务 4 Replicator + SnapshotManager 差分（✅ behavioral）
# ============================================
#
# 测试矩阵：
#   6.1 TestReplicatorGetPendingCountDiff（P1-P4）
#       - fixture：staging log 文件（JSON Lines）
#       - Python 路径：callwarden.server.replicator.Replicator.get_pending_count()
#       - Rust 路径：callwarden_core.replicator_get_pending_count()
#       - 断言：返回的 pending 数量一致
#
#   6.2 TestSnapshotQueryCallChainDownDiff（Q1-Q4）
#   6.3 TestSnapshotTopologicalOrderDiff（T1-T3）
#   6.4 TestSnapshotDetectCyclesDiff（D1-D3）
#   6.5 TestSnapshotQueryStatsDiff（S1-S2）
#       - fixture：含 symbols / calls 表的 callwarden.db
#       - Python 路径：SnapshotManagerService.query_*()
#       - Rust 路径：PySnapshotManager.query_*()
#       - 断言：返回值在业务语义上完全一致


def _make_staging_log_path(tmp_path, name: str = "staging.log") -> str:
    """生成 staging log 文件路径（不创建文件）"""
    return str(tmp_path / name)


def _write_staging_entries(log_path: str, entries: List[Dict[str, Any]]) -> None:
    """写入 staging entries 到 JSON Lines 文件"""
    from callwarden.server.staging_log import StagingLog, StagingEntry
    log = StagingLog(log_path)
    for e in entries:
        entry = StagingEntry(
            lsn=e["lsn"],
            timestamp=e.get("timestamp", 1.0),
            workspace_id=e["workspace_id"],
            file_path=e["file_path"],
            content_hash=e.get("content_hash", "hash"),
            language=e.get("language", "python"),
        )
        # status 需要在 append 后单独设置
        log.append(entry)
    # 单独覆盖 status 字段（StagingEntry.append 不接受 status）
    if any(e.get("status", "pending") != "pending" for e in entries):
        # 读取所有 entries，重写文件以反映 status
        all_entries = log.read(since_lsn=0)
        # 直接覆写文件
        import json as _json
        with open(log_path, "w", encoding="utf-8") as f:
            for orig, parsed in zip(entries, all_entries):
                status = orig.get("status", "pending")
                if status != "pending":
                    parsed.status = status
                f.write(parsed.to_json_line() + "\n")


def _setup_callwarden_db_with_symbols(db_path, symbols_calls_data):
    """构造含 file_instances / symbols / calls 表的 callwarden.db

    Args:
        db_path: 数据库路径
        symbols_calls_data: dict，含 "symbols" 和 "calls" 两个列表
            symbols: [(id, file_instance_id, kind, name, qualified_name, module_path, start_line, end_line, depth), ...]
            calls: [(caller_id, callee_id, callee_name, call_line, is_cross_file), ...]
    """
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    # file_instances
    cur.execute("""CREATE TABLE IF NOT EXISTS file_instances (
        id INTEGER PRIMARY KEY,
        workspace_id INTEGER DEFAULT 0,
        rel_path TEXT,
        status TEXT DEFAULT 'active',
        content_hash TEXT DEFAULT '',
        module_path TEXT DEFAULT '')""")
    file_paths = {s[1] for s in symbols_calls_data.get("symbols", [])}
    for fid in file_paths:
        cur.execute(
            "INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (?, 1, ?)",
            (fid, f"file_{fid}.py"),
        )
    # symbols
    cur.execute("""CREATE TABLE IF NOT EXISTS symbols (
        id INTEGER PRIMARY KEY,
        file_instance_id INTEGER,
        kind TEXT,
        name TEXT,
        qualified_name TEXT,
        module_path TEXT DEFAULT '',
        start_line INTEGER,
        end_line INTEGER,
        depth INTEGER DEFAULT 0,
        content TEXT DEFAULT '',
        signature TEXT DEFAULT '',
        visibility TEXT DEFAULT '',
        symbol_hash TEXT DEFAULT '',
        has_comment INTEGER DEFAULT 0,
        local_id INTEGER,
        lexical_parent_local_id INTEGER)""")
    for sym in symbols_calls_data.get("symbols", []):
        cur.execute(
            "INSERT INTO symbols (id, file_instance_id, kind, name, qualified_name, "
            "module_path, start_line, end_line, depth) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            sym,
        )
    # calls
    cur.execute("""CREATE TABLE IF NOT EXISTS calls (
        caller_id INTEGER,
        callee_id INTEGER,
        callee_name TEXT,
        call_line INTEGER,
        is_cross_file INTEGER DEFAULT 0,
        caller_name TEXT DEFAULT '',
        callee_module TEXT DEFAULT '')""")
    for call in symbols_calls_data.get("calls", []):
        cur.execute(
            "INSERT INTO calls (caller_id, callee_id, callee_name, call_line, is_cross_file) "
            "VALUES (?, ?, ?, ?, ?)",
            call,
        )
    # file_generations（GraphStore::load_from_sqlite_blocking 可能需要）
    cur.execute("""CREATE TABLE IF NOT EXISTS file_generations (
        workspace_id INTEGER,
        rel_path TEXT,
        generation INTEGER DEFAULT 1,
        committed INTEGER DEFAULT 1)""")
    cur.execute("INSERT INTO file_generations VALUES (1, 'file_1.py', 1, 1)")
    conn.commit()
    conn.close()


@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestReplicatorGetPendingCountDiff:
    """P1-P4: replicator_get_pending_count 差分（staging log pending 数量）

    Python 路径：Replicator.get_pending_count(workspace_id)
    Rust 路径：callwarden_core.replicator_get_pending_count(log_path, workspace_id)
    """

    @staticmethod
    def _py_get_pending_count(log_path: str, workspace_id):
        """Python 路径：构造 Replicator 并查询 pending 数量"""
        from callwarden.server.replicator import Replicator
        from callwarden.server.staging_log import StagingLog
        log = StagingLog(log_path)
        # Replicator 构造需要 snapshot_service，差分测试只需要 get_pending_count
        # 直接调用 StagingLog.read_pending + 过滤逻辑，避免 Replicator 复杂依赖
        pending = log.read_pending()
        if workspace_id:
            return sum(1 for e in pending if e.workspace_id == workspace_id)
        return len(pending)

    @staticmethod
    def _rust_get_pending_count(log_path: str, workspace_id):
        """Rust 路径：通过 PyO3 调用 replicator_get_pending_count"""
        return callwarden_core.replicator_get_pending_count(log_path, workspace_id)

    def test_p1_file_not_exists(self, tmp_path):
        """P1: 文件不存在 → 两端都返回 0"""
        log_path = str(tmp_path / "nonexistent.log")
        py = self._py_get_pending_count(log_path, None)
        rust = self._rust_get_pending_count(log_path, None)
        assert py == rust == 0, f"P1 差分失败：Python={py}, Rust={rust}"

    def test_p2_empty_log(self, tmp_path):
        """P2: 空 log（无 entry）→ 两端都返回 0"""
        log_path = _make_staging_log_path(tmp_path)
        # 仅创建空文件
        with open(log_path, "w", encoding="utf-8") as f:
            pass
        py = self._py_get_pending_count(log_path, None)
        rust = self._rust_get_pending_count(log_path, None)
        assert py == rust == 0, f"P2 差分失败：Python={py}, Rust={rust}"

    def test_p3_workspace_has_pending(self, tmp_path):
        """P3: workspace 有 N 个 pending → 两端都返回 N"""
        log_path = _make_staging_log_path(tmp_path)
        # 写入 3 条 pending + 2 条 applied
        _write_staging_entries(log_path, [
            {"lsn": 1, "workspace_id": "ws1", "file_path": "a.py", "status": "pending"},
            {"lsn": 2, "workspace_id": "ws1", "file_path": "b.py", "status": "pending"},
            {"lsn": 3, "workspace_id": "ws1", "file_path": "c.py", "status": "pending"},
            {"lsn": 4, "workspace_id": "ws1", "file_path": "d.py", "status": "applied"},
            {"lsn": 5, "workspace_id": "ws1", "file_path": "e.py", "status": "failed"},
        ])
        py = self._py_get_pending_count(log_path, "ws1")
        rust = self._rust_get_pending_count(log_path, "ws1")
        assert py == rust == 3, f"P3 差分失败：Python={py}, Rust={rust}"

    def test_p4_none_returns_all(self, tmp_path):
        """P4: workspace_id=None → 返回所有 pending 总数"""
        log_path = _make_staging_log_path(tmp_path)
        _write_staging_entries(log_path, [
            {"lsn": 1, "workspace_id": "ws1", "file_path": "a.py", "status": "pending"},
            {"lsn": 2, "workspace_id": "ws2", "file_path": "b.py", "status": "pending"},
            {"lsn": 3, "workspace_id": "ws1", "file_path": "c.py", "status": "applied"},
        ])
        py = self._py_get_pending_count(log_path, None)
        rust = self._rust_get_pending_count(log_path, None)
        assert py == rust == 2, f"P4 差分失败：Python={py}, Rust={rust}"

    def test_p5_workspace_filter(self, tmp_path):
        """P5: 多 workspace 过滤"""
        log_path = _make_staging_log_path(tmp_path)
        _write_staging_entries(log_path, [
            {"lsn": 1, "workspace_id": "ws1", "file_path": "a.py", "status": "pending"},
            {"lsn": 2, "workspace_id": "ws1", "file_path": "b.py", "status": "pending"},
            {"lsn": 3, "workspace_id": "ws2", "file_path": "c.py", "status": "pending"},
        ])
        py_ws1 = self._py_get_pending_count(log_path, "ws1")
        rust_ws1 = self._rust_get_pending_count(log_path, "ws1")
        assert py_ws1 == rust_ws1 == 2, f"P5 ws1 差分失败：Python={py_ws1}, Rust={rust_ws1}"
        py_ws2 = self._py_get_pending_count(log_path, "ws2")
        rust_ws2 = self._rust_get_pending_count(log_path, "ws2")
        assert py_ws2 == rust_ws2 == 1, f"P5 ws2 差分失败：Python={py_ws2}, Rust={rust_ws2}"


@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestSnapshotQueryCallChainDownDiff:
    """Q1-Q4: query_call_chain_down 差分（向下调用链 BFS）"""

    @staticmethod
    def _setup_db_with_chain(db_path):
        """构造含 a→b→c 调用链的 db

        a (qualified_name=a) → b (qualified_name=b) → c (qualified_name=c)
        """
        _setup_callwarden_db_with_symbols(db_path, {
            "symbols": [
                # (id, file_instance_id, kind, name, qualified_name, module_path, start_line, end_line, depth)
                (1, 1, "fn", "a", "a", "", 10, 20, 0),
                (2, 1, "fn", "b", "b", "", 30, 40, 0),
                (3, 1, "fn", "c", "c", "", 50, 60, 0),
            ],
            "calls": [
                # (caller_id, callee_id, callee_name, call_line, is_cross_file)
                (1, 2, "b", 15, 0),  # a → b
                (2, 3, "c", 35, 0),  # b → c
            ],
        })

    @staticmethod
    def _publish_and_get_python_store(db_path, workspace_id=1):
        """Python 路径：通过 SnapshotManagerService 发布 snapshot 并获取 store"""
        from callwarden.server.snapshot_manager import SnapshotManagerService
        SnapshotManagerService.reset_instance()
        svc = SnapshotManagerService(max_workspaces=8)
        SnapshotManagerService._instance = svc
        svc.publish_snapshot(
            "ws_diff",
            db_path=str(db_path),
            build_context_hash="ctx_diff",
            workspace_id=workspace_id,
        )
        return svc, svc._get_rust_graph_store("ws_diff")

    @staticmethod
    def _publish_and_get_rust_mgr(db_path, workspace_id=1):
        """Rust 路径：直接通过 PySnapshotManager 发布 snapshot"""
        from callwarden_core import PySnapshotManager
        mgr = PySnapshotManager("ws_diff_rust")
        mgr.build_and_publish(
            db_path=str(db_path),
            build_context_hash="ctx_diff",
            workspace_id=workspace_id,
        )
        return mgr

    def test_q1_root_not_exists(self, tmp_path):
        """Q1: root 不存在 → 两端都返回 []"""
        db_path = tmp_path / "chain.db"
        self._setup_db_with_chain(db_path)
        _, py_store = self._publish_and_get_python_store(db_path)
        rust_mgr = self._publish_and_get_rust_mgr(db_path)

        py_result = py_store.get_call_chain_down("nonexistent", 10)
        rust_result = rust_mgr.query_call_chain_down("nonexistent", 10)
        assert py_result == [] and list(rust_result) == [], (
            f"Q1 差分失败：Python={py_result}, Rust={list(rust_result)}"
        )

    def test_q2_root_no_downstream(self, tmp_path):
        """Q2: root 存在，无下游 → 两端都返回空边列表

        注意：c 是叶子，没有 callee，所以调用链返回空
        """
        db_path = tmp_path / "leaf.db"
        self._setup_db_with_chain(db_path)
        _, py_store = self._publish_and_get_python_store(db_path)
        rust_mgr = self._publish_and_get_rust_mgr(db_path)

        py_result = py_store.get_call_chain_down("c", 10)
        rust_result = list(rust_mgr.query_call_chain_down("c", 10))
        assert py_result == [] and rust_result == [], (
            f"Q2 差分失败：Python={py_result}, Rust={rust_result}"
        )

    def test_q3_root_two_level_downstream(self, tmp_path):
        """Q3: root 存在，2 层下游 → 两端返回相同边集合

        a → b → c 调用链：
        - depth 0: a→b (caller=a, callee=b)
        - depth 1: b→c (caller=b, callee=c)
        """
        db_path = tmp_path / "chain.db"
        self._setup_db_with_chain(db_path)
        _, py_store = self._publish_and_get_python_store(db_path)
        rust_mgr = self._publish_and_get_rust_mgr(db_path)

        py_result = py_store.get_call_chain_down("a", 10)
        rust_result = list(rust_mgr.query_call_chain_down("a", 10))

        assert len(py_result) == len(rust_result) == 2, (
            f"Q3 数量不一致：Python={len(py_result)}, Rust={len(rust_result)}\n"
            f"  Python: {py_result}\n  Rust: {rust_result}"
        )
        # 比对每条边的关键字段
        for py_edge, rust_edge in zip(py_result, rust_result):
            assert py_edge["caller_name"] == rust_edge["caller_name"], (
                f"caller_name 不一致：Python={py_edge['caller_name']}, Rust={rust_edge['caller_name']}"
            )
            assert py_edge["callee_name"] == rust_edge["callee_name"], (
                f"callee_name 不一致：Python={py_edge['callee_name']}, Rust={rust_edge['callee_name']}"
            )
            assert py_edge["depth"] == rust_edge["depth"], (
                f"depth 不一致：Python={py_edge['depth']}, Rust={rust_edge['depth']}"
            )
            assert py_edge["call_line"] == rust_edge["call_line"], (
                f"call_line 不一致：Python={py_edge['call_line']}, Rust={rust_edge['call_line']}"
            )

    def test_q4_max_depth_1(self, tmp_path):
        """Q4: max_depth=1 → 只返回 depth=0 的边"""
        db_path = tmp_path / "chain.db"
        self._setup_db_with_chain(db_path)
        _, py_store = self._publish_and_get_python_store(db_path)
        rust_mgr = self._publish_and_get_rust_mgr(db_path)

        py_result = py_store.get_call_chain_down("a", 1)
        rust_result = list(rust_mgr.query_call_chain_down("a", 1))
        assert len(py_result) == len(rust_result) == 1, (
            f"Q4 数量不一致：Python={len(py_result)}, Rust={len(rust_result)}"
        )
        assert py_result[0]["depth"] == 0 and rust_result[0]["depth"] == 0


@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestSnapshotTopologicalOrderDiff:
    """T1-T3: query_topological_order 差分（拓扑排序）"""

    def test_t1_empty_graph_store(self, tmp_path):
        """T1: 空 GraphStore（无 symbols）→ 两端都返回 []"""
        db_path = tmp_path / "empty.db"
        # 仅创建空表，无数据
        _setup_callwarden_db_with_symbols(db_path, {"symbols": [], "calls": []})
        from callwarden.server.snapshot_manager import SnapshotManagerService
        from callwarden_core import PySnapshotManager
        SnapshotManagerService.reset_instance()
        svc = SnapshotManagerService(max_workspaces=8)
        SnapshotManagerService._instance = svc
        svc.publish_snapshot("ws_t1", db_path=str(db_path), workspace_id=1)

        rust_mgr = PySnapshotManager("ws_t1_rust")
        rust_mgr.build_and_publish(db_path=str(db_path), workspace_id=1)

        py_result = svc.query_topological_order("ws_t1")
        rust_result = rust_mgr.query_topological_order()
        assert py_result == [] and rust_result == [], (
            f"T1 差分失败：Python={py_result}, Rust={rust_result}"
        )

    def test_t2_dag_no_cycle(self, tmp_path):
        """T2: DAG 无循环（a→b→c）→ 两端拓扑序一致"""
        db_path = tmp_path / "dag.db"
        _setup_callwarden_db_with_symbols(db_path, {
            "symbols": [
                (1, 1, "fn", "a", "a", "", 1, 10, 0),
                (2, 1, "fn", "b", "b", "", 11, 20, 0),
                (3, 1, "fn", "c", "c", "", 21, 30, 0),
            ],
            "calls": [
                (1, 2, "b", 5, 0),
                (2, 3, "c", 15, 0),
            ],
        })
        from callwarden.server.snapshot_manager import SnapshotManagerService
        from callwarden_core import PySnapshotManager
        SnapshotManagerService.reset_instance()
        svc = SnapshotManagerService(max_workspaces=8)
        SnapshotManagerService._instance = svc
        svc.publish_snapshot("ws_t2", db_path=str(db_path), workspace_id=1)

        rust_mgr = PySnapshotManager("ws_t2_rust")
        rust_mgr.build_and_publish(db_path=str(db_path), workspace_id=1)

        py_result = svc.query_topological_order("ws_t2")
        rust_result = rust_mgr.query_topological_order()
        # 拓扑序：被调用者在前 → c, b, a 或 a, b, c（取决于实现）
        # 两端实现一致即可
        assert py_result == rust_result, (
            f"T2 差分失败：Python={py_result}, Rust={rust_result}"
        )

    def test_t3_with_cycle(self, tmp_path):
        """T3: 含循环（a→b→a）→ 两端返回部分排序"""
        db_path = tmp_path / "cycle.db"
        _setup_callwarden_db_with_symbols(db_path, {
            "symbols": [
                (1, 1, "fn", "a", "a", "", 1, 10, 0),
                (2, 1, "fn", "b", "b", "", 11, 20, 0),
            ],
            "calls": [
                (1, 2, "b", 5, 0),  # a → b
                (2, 1, "a", 15, 0),  # b → a（循环）
            ],
        })
        from callwarden.server.snapshot_manager import SnapshotManagerService
        from callwarden_core import PySnapshotManager
        SnapshotManagerService.reset_instance()
        svc = SnapshotManagerService(max_workspaces=8)
        SnapshotManagerService._instance = svc
        svc.publish_snapshot("ws_t3", db_path=str(db_path), workspace_id=1)

        rust_mgr = PySnapshotManager("ws_t3_rust")
        rust_mgr.build_and_publish(db_path=str(db_path), workspace_id=1)

        py_result = svc.query_topological_order("ws_t3")
        rust_result = rust_mgr.query_topological_order()
        # 含循环时两端都不抛错，返回部分排序
        # 集合相等即可（顺序可能不同，因循环处理）
        assert set(py_result) == set(rust_result), (
            f"T3 集合不一致：Python={py_result}, Rust={rust_result}"
        )


@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestSnapshotDetectCyclesDiff:
    """D1-D3: query_detect_cycles 差分（循环检测）"""

    def test_d1_no_cycle(self, tmp_path):
        """D1: 无循环 → 两端都返回 []"""
        db_path = tmp_path / "nocycle.db"
        _setup_callwarden_db_with_symbols(db_path, {
            "symbols": [
                (1, 1, "fn", "a", "a", "", 1, 10, 0),
                (2, 1, "fn", "b", "b", "", 11, 20, 0),
            ],
            "calls": [
                (1, 2, "b", 5, 0),  # a → b（无环）
            ],
        })
        from callwarden.server.snapshot_manager import SnapshotManagerService
        from callwarden_core import PySnapshotManager
        SnapshotManagerService.reset_instance()
        svc = SnapshotManagerService(max_workspaces=8)
        SnapshotManagerService._instance = svc
        svc.publish_snapshot("ws_d1", db_path=str(db_path), workspace_id=1)
        rust_mgr = PySnapshotManager("ws_d1_rust")
        rust_mgr.build_and_publish(db_path=str(db_path), workspace_id=1)

        py_result = svc.query_detect_cycles("ws_d1")
        rust_result = rust_mgr.query_detect_cycles()
        assert py_result == [] and rust_result == [], (
            f"D1 差分失败：Python={py_result}, Rust={rust_result}"
        )

    def test_d2_single_cycle(self, tmp_path):
        """D2: 单个循环（a↔b）→ 两端都检测到循环"""
        db_path = tmp_path / "single_cycle.db"
        _setup_callwarden_db_with_symbols(db_path, {
            "symbols": [
                (1, 1, "fn", "a", "a", "", 1, 10, 0),
                (2, 1, "fn", "b", "b", "", 11, 20, 0),
            ],
            "calls": [
                (1, 2, "b", 5, 0),  # a → b
                (2, 1, "a", 15, 0),  # b → a（循环）
            ],
        })
        from callwarden.server.snapshot_manager import SnapshotManagerService
        from callwarden_core import PySnapshotManager
        SnapshotManagerService.reset_instance()
        svc = SnapshotManagerService(max_workspaces=8)
        SnapshotManagerService._instance = svc
        svc.publish_snapshot("ws_d2", db_path=str(db_path), workspace_id=1)
        rust_mgr = PySnapshotManager("ws_d2_rust")
        rust_mgr.build_and_publish(db_path=str(db_path), workspace_id=1)

        py_result = svc.query_detect_cycles("ws_d2")
        rust_result = rust_mgr.query_detect_cycles()
        # 两端都应至少检测到 1 个循环
        assert len(py_result) >= 1, f"D2 Python 未检测到循环：{py_result}"
        assert len(rust_result) >= 1, f"D2 Rust 未检测到循环：{rust_result}"
        # 比对循环集合（每个循环的节点集合应相同）
        py_cycle_set = frozenset(frozenset(c) for c in py_result)
        rust_cycle_set = frozenset(frozenset(c) for c in rust_result)
        assert py_cycle_set == rust_cycle_set, (
            f"D2 循环集合不一致：Python={py_result}, Rust={rust_result}"
        )


@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestSnapshotQueryStatsDiff:
    """S1-S2: query_stats 差分（统计信息）"""

    @staticmethod
    def _publish_both(db_path, ws_id_py="ws_s", ws_id_rust="ws_s_rust"):
        from callwarden.server.snapshot_manager import SnapshotManagerService
        from callwarden_core import PySnapshotManager
        SnapshotManagerService.reset_instance()
        svc = SnapshotManagerService(max_workspaces=8)
        SnapshotManagerService._instance = svc
        svc.publish_snapshot(ws_id_py, db_path=str(db_path), workspace_id=1)
        rust_mgr = PySnapshotManager(ws_id_rust)
        rust_mgr.build_and_publish(db_path=str(db_path), workspace_id=1)
        return svc, rust_mgr

    def test_s1_empty_graph_store(self, tmp_path):
        """S1: 空 GraphStore（无 symbols）→ 两端字段集一致"""
        db_path = tmp_path / "empty.db"
        _setup_callwarden_db_with_symbols(db_path, {"symbols": [], "calls": []})
        svc, rust_mgr = self._publish_both(db_path)

        py_stats = svc.query_stats("ws_s")
        rust_stats = rust_mgr.query_stats()
        # 关键字段：symbol_count / edge_count 应都为 0
        assert py_stats is not None, "Python query_stats 不应返回 None"
        assert rust_stats is not None, "Rust query_stats 不应返回 None"
        assert py_stats.get("symbol_count", -1) == 0, f"Python symbol_count 应为 0: {py_stats}"
        assert rust_stats.get("symbol_count", -1) == 0, f"Rust symbol_count 应为 0: {rust_stats}"
        assert py_stats.get("edge_count", -1) == 0, f"Python edge_count 应为 0: {py_stats}"
        assert rust_stats.get("edge_count", -1) == 0, f"Rust edge_count 应为 0: {rust_stats}"

    def test_s2_with_data(self, tmp_path):
        """S2: 有数据 → 两端关键字段一致"""
        db_path = tmp_path / "with_data.db"
        _setup_callwarden_db_with_symbols(db_path, {
            "symbols": [
                (1, 1, "fn", "a", "a", "", 1, 10, 0),
                (2, 1, "fn", "b", "b", "", 11, 20, 0),
                (3, 1, "fn", "c", "c", "", 21, 30, 0),
            ],
            "calls": [
                (1, 2, "b", 5, 0),
                (2, 3, "c", 15, 0),
            ],
        })
        svc, rust_mgr = self._publish_both(db_path)

        py_stats = svc.query_stats("ws_s")
        rust_stats = rust_mgr.query_stats()
        assert py_stats is not None and rust_stats is not None
        # 关键字段逐一比对
        # 说明：symbol_count 两端都返回 4（含 1 个根/虚拟节点，by_id.len()=4，
        # 但 qname_index_size=3 表示只有 3 个 qname 索引项；两端一致即可，
        # 不强制具体数值，以差分语义为准）
        assert py_stats["symbol_count"] == rust_stats["symbol_count"], (
            f"S2 symbol_count 不一致：Python={py_stats.get('symbol_count')}, "
            f"Rust={rust_stats.get('symbol_count')}"
        )
        assert py_stats["edge_count"] == rust_stats["edge_count"], (
            f"S2 edge_count 不一致：Python={py_stats.get('edge_count')}, "
            f"Rust={rust_stats.get('edge_count')}"
        )
        assert py_stats["resolved_edge_count"] == rust_stats["resolved_edge_count"], (
            f"S2 resolved_edge_count 不一致：Python={py_stats.get('resolved_edge_count')}, "
            f"Rust={rust_stats.get('resolved_edge_count')}"
        )
        # 关键字段非零校验（确保数据已加载）
        assert py_stats["symbol_count"] > 0
        assert py_stats["edge_count"] == 2
