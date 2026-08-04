"""T-1785831377544-d99b57de: db/schema.py 与 Rust schema checksum 漂移回归测试。

**背景**：commit a8580e9（8/3，P3/P4 lease schema）修改 db/schema.py 的
SCHEMA_SQL 后，Rust `storage.rs` 编译时 `include_str!` 嵌入 db/schema.py 并计算
`canonical_schema_checksum()`；DB 的 `schema_migrations` 表 v46 stored checksum
（8/1 旧 pyd 写入的 `23534594...`）与当前 binary（`94e9d963...`）不匹配，导致
新编译 callwarden_core 打开 DB 时 `initialize_or_migrate` fail-closed 报
`MIGRATION_FAILED: schema checksum mismatch`。

**修复（策略 A）**：`storage.rs` 的 `initialize_or_migrate` 对
`current_version == expected_version` 且 checksum 不匹配的场景，改为校验 DB 实际
表结构完整性（所有 canonical SCHEMA_SQL 表存在即视为一致），通过则接受并重写
stored checksum 为当前 canonical；存在缺失表（真正 schema 漂移）才 fail-closed。

**测试目标**：
1. Rust 与 Python 的 SCHEMA_SQL 提取逻辑一致（checksum 可复现，防止未来再漂移）。
2. 模拟"stored checksum 过期但表齐全"的 DB → storage_initialize_or_migrate 不再
   报 MIGRATION_FAILED，且 checksum 被重写为当前 canonical。
3. 模拟"缺表 + checksum 过期"的 DB → 仍 fail-closed（保留对真正漂移的阻断）。

前置条件：
- Rust 扩展 callwarden_core 必须可加载（Windows 编译的 .pyd）。
- 扩展不可用时 checksum 提取一致性用例仍运行（纯 Python），
  storage 相关用例显式 skip。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

_RUST_EXT_AVAILABLE = False
_RUST_EXT_SKIP_REASON = ""
try:
    import callwarden_core  # type: ignore
    _RUST_EXT_AVAILABLE = True
except ImportError as _e:
    _RUST_EXT_SKIP_REASON = (
        f"callwarden_core 不可加载：{_e}。"
        "本测试需要 Windows 编译的 Rust 扩展（callwarden_core.pyd）。"
    )

_SCHEMA_PY = _PKG_ROOT / "db" / "schema.py"


def _extract_schema_sql_rust_logic() -> str:
    """按 Rust storage.rs::canonical_schema_sql() 的提取逻辑取 SCHEMA_SQL 块。

    Rust 逻辑：
      marker = "SCHEMA_SQL = \"\"\""
      start = index(marker) + marker.len()
      end = index("\n\"\"\"", start)   // 换行 + 三个引号
      sql = source[start..end]
    """
    src = _SCHEMA_PY.read_text(encoding="utf-8")
    marker = 'SCHEMA_SQL = """'
    start = src.index(marker) + len(marker)
    end = src.index('\n"""', start)
    return src[start:end]


def _extract_schema_sql_python_native() -> str:
    """按 db/schema.py 实际执行得到的 SCHEMA_SQL 值（直接 import）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_cw_schema_probe", _SCHEMA_PY)
    mod = importlib.util.module_from_spec(spec)
    # 防止 schema.py 的顶层副作用影响（只读模块，import 应安全）
    sys.modules["_cw_schema_probe"] = mod
    spec.loader.exec_module(mod)
    return mod.SCHEMA_SQL


def _canonical_checksum() -> str:
    """Rust canonical_schema_checksum() 等价：SHA256(Rust 提取逻辑的 SCHEMA_SQL)。"""
    return hashlib.sha256(_extract_schema_sql_rust_logic().encode("utf-8")).hexdigest()


def _expected_table_names() -> set:
    """Rust expected_canonical_table_names() 等价：SCHEMA_SQL 中 CREATE TABLE 表名集合。"""
    names = set()
    for line in _extract_schema_sql_rust_logic().splitlines():
        trimmed = line.lstrip()
        if trimmed.startswith("CREATE TABLE "):
            rest = trimmed[len("CREATE TABLE "):]
            if rest.startswith("IF NOT EXISTS "):
                rest = rest[len("IF NOT EXISTS "):]
            name = rest.split()[0].strip("(")
            if name:
                names.add(name)
    return names


def _make_db_with_full_schema(db_path) -> int:
    """用 Rust storage_initialize_or_migrate 建一个完整 schema 的 DB，返回 SCHEMA_VERSION。"""
    from callwarden.db.schema import SCHEMA_VERSION
    from callwarden_core import storage_initialize_or_migrate
    res = storage_initialize_or_migrate(str(db_path), SCHEMA_VERSION)
    assert res is not None, "storage_initialize_or_migrate 应返回结果"
    assert res.get("success"), f"初始化失败: {res!r}"
    return SCHEMA_VERSION


# ============================================
# 1. checksum 提取逻辑一致性（纯 Python，无需 Rust 扩展）
# ============================================

class TestChecksumExtractionConsistency:
    """Rust 与 Python 的 SCHEMA_SQL 提取必须得到同一份 SQL 文本。"""

    def test_schema_sql_marker_present(self):
        """db/schema.py 必须包含 SCHEMA_SQL 块标记。"""
        src = _SCHEMA_PY.read_text(encoding="utf-8")
        assert 'SCHEMA_SQL = """' in src, "SCHEMA_SQL 块标记缺失"

    def test_rust_extraction_matches_python_value(self):
        """Rust 提取逻辑的 SCHEMA_SQL 与 schema.py 实际 SCHEMA_SQL 一致。"""
        rust_logic = _extract_schema_sql_rust_logic()
        python_native = _extract_schema_sql_python_native()
        assert rust_logic.strip() == python_native.strip(), (
            "Rust 提取逻辑与 Python SCHEMA_SQL 不一致——Rust canonical_schema_sql() "
            "可能因块边界变化而漂移，需同步"
        )

    def test_expected_table_names_match_python(self):
        """Rust 预期表名集合与 schema.py 实际 CREATE TABLE 表名一致。"""
        rust_names = _expected_table_names()
        python_names = set(_extract_schema_sql_python_native())
        # Python 侧解析出所有 CREATE TABLE 表名
        import re
        py_tables = set(
            re.findall(
                r"CREATE TABLE(?: IF NOT EXISTS)?\s+([a-z_0-9]+)",
                _extract_schema_sql_python_native(),
            )
        )
        assert rust_names == py_tables, (
            f"Rust 提取表名({len(rust_names)}) 与 Python({len(py_tables)}) 不一致"
        )

    def test_canonical_checksum_stable_shape(self):
        """canonical checksum 为 64 位 hex；SCHEMA_SQL 非空。"""
        csum = _canonical_checksum()
        assert len(csum) == 64
        assert len(_extract_schema_sql_rust_logic()) > 1000


# ============================================
# 2. checksum 过期但表齐全 → 不再 MIGRATION_FAILED（需 Rust 扩展）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestChecksumReconcile:
    """策略 A 核心回归：stored checksum 过期 + 表结构完整 → 接受并重写。"""

    def _make_db_with_stale_checksum(self, db_path):
        """建完整 schema DB，然后把 schema_migrations 的 checksum 改成过期值。"""
        from callwarden.db.schema import SCHEMA_VERSION
        from callwarden_core import storage_initialize_or_migrate
        res = storage_initialize_or_migrate(str(db_path), SCHEMA_VERSION)
        assert res.get("success"), f"初始化失败: {res!r}"

        conn = sqlite3.connect(str(db_path))
        # 篡改 checksum，模拟 8/1 旧 pyd 写入的过期值
        conn.execute(
            "UPDATE schema_migrations SET checksum='23534594b2d61be7_deadbeef' "
            "WHERE version=?",
            (SCHEMA_VERSION,),
        )
        conn.commit()
        conn.close()

    def test_stale_checksum_with_complete_schema_succeeds(self, tmp_path):
        """表齐全 + checksum 过期 → 重新初始化成功，且 checksum 被重写。"""
        from callwarden.db.schema import SCHEMA_VERSION
        from callwarden_core import storage_initialize_or_migrate

        db_path = tmp_path / "stale_checksum.db"
        self._make_db_with_stale_checksum(db_path)

        # 关键断言：不应抛 MIGRATION_FAILED
        res = storage_initialize_or_migrate(str(db_path), SCHEMA_VERSION)
        assert res is not None and res.get("success"), (
            f"表齐全但 checksum 过期不应 MIGRATION_FAILED: {res!r}"
        )
        assert res.get("version") == SCHEMA_VERSION

        # checksum 应被重写为当前 canonical
        conn = sqlite3.connect(str(db_path))
        stored = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?",
            (SCHEMA_VERSION,),
        ).fetchone()[0]
        conn.close()
        assert stored == _canonical_checksum(), (
            f"checksum 应被重写为当前 canonical: stored={stored[:16]}..."
        )

    def test_stale_checksum_with_missing_table_fails_closed(self, tmp_path):
        """缺表 + checksum 过期 → 仍 fail-closed（保留对真正漂移的阻断）。"""
        from callwarden.db.schema import SCHEMA_VERSION
        from callwarden_core import storage_initialize_or_migrate

        db_path = tmp_path / "missing_table.db"
        self._make_db_with_stale_checksum(db_path)

        # 删掉一个 canonical 表模拟真正的 schema 漂移
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS task_dependencies")
        conn.commit()
        conn.close()

        # 应 fail-closed
        with pytest.raises(Exception) as excinfo:
            storage_initialize_or_migrate(str(db_path), SCHEMA_VERSION)
        assert "MIGRATION_FAILED" in str(excinfo.value) or "checksum" in str(
            excinfo.value
        ), f"缺表时应 fail-closed: {excinfo.value!r}"

    def test_matching_checksum_still_fast_path(self, tmp_path):
        """checksum 匹配时仍走快速路径（返回成功，不重写）。"""
        from callwarden.db.schema import SCHEMA_VERSION
        from callwarden_core import storage_initialize_or_migrate

        db_path = tmp_path / "matching_checksum.db"
        res = storage_initialize_or_migrate(str(db_path), SCHEMA_VERSION)
        assert res.get("success")

        # 再次初始化，checksum 已匹配 → 快速返回
        res2 = storage_initialize_or_migrate(str(db_path), SCHEMA_VERSION)
        assert res2.get("success")
        assert res2.get("version") == SCHEMA_VERSION
