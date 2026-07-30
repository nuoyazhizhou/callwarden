"""Phase 2-6-1 增量构建 PyO3 暴露层差分测试。

**本文件是 manifest §7 中 Phase 2-6-1 的 ✅(behavioral) 标记载体。**

差分测试矩阵（契约 docs/design/phase2-6-1-incremental-build-contract.md §3）：
  TestComputeSymbolDiffDiff：D1-D10（compute_and_apply_symbol_diff 差分）
    - D1: 无删除符号（prev={A,B}, curr={A,B}）→ removed_count=0
    - D2: 全部删除（prev={A,B}, curr={}）→ removed_count=2
    - D3: 部分删除（prev={A,B,C}, curr={B}）→ removed_count=2
    - D4: 新增符号（prev={A}, curr={A,B}）→ removed_count=0
    - D5: 混合变更（prev={A,B,C}, curr={B,C,D}）→ removed_count=1
    - D6: prev 为空（prev={}, curr={A,B}）→ removed_count=0
    - D7: curr 为空（prev={A,B}, curr={}）→ removed_count=2
    - D8: prev_version_id 不存在 → success=false（与 Python 一致：prev_symbols 为空）
    - D9: curr_version_id 不存在 → success=false（FK 失败）
    - D10: 位置信息正确性（start_line/end_line/module_path/depth 与 prev 一致）

  TestLoadFileResultFromDbDiff：L1-L7（load_file_result_from_db 差分）
    - L1: 正常加载（3 符号 + 2 calls）
    - L2: 空版本（0 符号 0 calls）
    - L3: version 不存在 → None
    - L4: is_deleted 过滤（5 符号其中 2 个 is_deleted=1 → symbols 只含 3 个）
    - L5: content_hash + total_lines 字段一致性
    - L6: calls 关联（通过 file_instance_id JOIN）
    - L7: 字段完整性（含 doc_comment/calls/issues 等所有字段）

预期差异（见契约 §4 + §8）：
  - 事务边界：Python `_compute_and_apply_symbol_diff` 在外层事务中执行（无显式 BEGIN/COMMIT）；
    Rust 是独立子事务（BEGIN IMMEDIATE → COMMIT）。差分测试用 BEGIN/COMMIT 包裹 Python 路径。
  - 不切换默认路径：Rust API 仅作为可选短路，通过 `is_feature_rolled_back` 控制。

前置条件：
  - Rust 扩展 callwarden_core 必须可加载（为 Python 3.14 编译的 .pyd）
  - 如果当前 Python 不是 3.14，本测试套件会显式 skip 并给出修复指引

关联：
  - 契约：docs/design/phase2-6-1-incremental-build-contract.md
  - Python 真相源：
    - db/db_build.py:_compute_and_apply_symbol_diff (L3169-3209)
    - db/db_build.py:_load_file_result_from_db (L1936-1993)
  - Rust 真相源：rust_ext/src/incremental_build_query.rs
"""
from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import time
from typing import Any, Dict, List, Optional

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
        "在 Windows 上若当前 Python 不是 3.14，请用 C:\\Python314\\python.exe 运行。"
    )


# ============================================
# CodeGraph DB schema（与 db/schema.py 对齐，核心表子集）
# ============================================

_CODEGRAPH_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    root_path TEXT UNIQUE NOT NULL,
    created_at REAL NOT NULL,
    is_active INTEGER DEFAULT 0,
    description TEXT DEFAULT '',
    active_task_id TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS file_contents (
    content_hash TEXT PRIMARY KEY,
    language TEXT DEFAULT '',
    total_lines INTEGER DEFAULT 0,
    first_seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS file_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    abs_path TEXT NOT NULL,
    current_content_hash TEXT DEFAULT '',
    mtime REAL NOT NULL,
    total_lines INTEGER DEFAULT 0,
    last_parsed REAL DEFAULT 0,
    status TEXT DEFAULT 'pending',
    module_path TEXT DEFAULT '',
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY (current_content_hash) REFERENCES file_contents(content_hash),
    UNIQUE(workspace_id, rel_path)
);
CREATE TABLE IF NOT EXISTS file_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    version_num INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    mtime REAL NOT NULL,
    total_lines INTEGER DEFAULT 0,
    parsed_at REAL NOT NULL,
    is_current INTEGER DEFAULT 1,
    is_deleted INTEGER DEFAULT 0,
    commit_hash TEXT DEFAULT '',
    ast_cache BLOB DEFAULT NULL,
    FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
    FOREIGN KEY (content_hash) REFERENCES file_contents(content_hash)
);
CREATE TABLE IF NOT EXISTS symbol_contents (
    content_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    signature TEXT DEFAULT '',
    has_comment INTEGER DEFAULT 0,
    comment_content TEXT DEFAULT '',
    qualified_name TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    symbol_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    visibility TEXT DEFAULT 'private',
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    start_col INTEGER DEFAULT 0,
    end_col INTEGER DEFAULT 0,
    signature TEXT DEFAULT '',
    has_comment INTEGER DEFAULT 0,
    comment_status TEXT DEFAULT 'pending',
    module_path TEXT DEFAULT '',
    qualified_name TEXT DEFAULT '',
    depth INTEGER DEFAULT -1,
    FOREIGN KEY (file_instance_id) REFERENCES file_instances(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_symbols_unique
    ON symbols(file_instance_id, name, start_line);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id INTEGER NOT NULL,
    caller_name TEXT NOT NULL,
    caller_module TEXT NOT NULL,
    callee_name TEXT NOT NULL,
    callee_module TEXT DEFAULT '',
    callee_qualified TEXT DEFAULT '',
    callee_file TEXT DEFAULT '',
    callee_id INTEGER DEFAULT 0,
    call_line INTEGER DEFAULT 0,
    is_cross_file INTEGER DEFAULT 0,
    FOREIGN KEY (caller_id) REFERENCES symbols(id)
);
CREATE TABLE IF NOT EXISTS file_symbol_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_version_id INTEGER NOT NULL,
    symbol_hash TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    module_path TEXT DEFAULT '',
    depth INTEGER DEFAULT -1,
    is_deleted INTEGER DEFAULT 0,
    FOREIGN KEY (file_version_id) REFERENCES file_versions(id),
    FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
);
CREATE INDEX IF NOT EXISTS idx_file_versions_instance ON file_versions(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_file_versions_current ON file_versions(is_current);
CREATE INDEX IF NOT EXISTS idx_symbols_file_instance ON symbols(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);
CREATE INDEX IF NOT EXISTS idx_fsv_file_version ON file_symbol_versions(file_version_id);
"""


def _make_codegraph_db(db_path):
    """构建测试用 CodeGraph DB（核心表，与 schema.py 对齐）"""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_CODEGRAPH_SCHEMA_DDL)
    conn.commit()
    conn.close()


# ============================================
# 通用 fixture：临时 CodeGraph DB
# ============================================

@pytest.fixture
def codegraph_db(tmp_path):
    """提供带 schema 的临时 CodeGraph DB"""
    db_path = tmp_path / "codegraph.db"
    _make_codegraph_db(db_path)
    return db_path


def _prep_workspace_file_version(db_path, workspace_id=1, rel_path="src/main.py",
                                  abs_path="/app/src/main.py",
                                  content_hash="ch_v1", total_lines=10):
    """预填 workspace + file_contents + file_instances + file_versions（含 1 个当前版本）。

    返回 (file_instance_id, file_version_id)。
    """
    conn = sqlite3.connect(str(db_path))
    now = time.time()
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) "
        "VALUES (?, ?, ?, ?, 1)",
        (workspace_id, f"ws-{workspace_id}", f"/app-{workspace_id}", now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) "
        "VALUES (?, 'python', ?, ?)",
        (content_hash, total_lines, now),
    )
    cur = conn.execute(
        "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
        "mtime, total_lines, last_parsed, status, module_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'parsed', '')",
        (workspace_id, rel_path, abs_path, content_hash, now, total_lines, now),
    )
    file_instance_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO file_versions (file_instance_id, version_num, content_hash, mtime, "
        "total_lines, parsed_at, is_current, is_deleted, commit_hash) "
        "VALUES (?, 1, ?, ?, ?, ?, 1, 0, '')",
        (file_instance_id, content_hash, now, total_lines, now),
    )
    file_version_id = cur.lastrowid
    conn.commit()
    conn.close()
    return file_instance_id, file_version_id


def _insert_symbol_version(db_path, file_version_id, symbol_hash, qualified_name,
                            start_line, end_line, module_path="", depth=-1,
                            is_deleted=0):
    """插入 file_symbol_versions 一行"""
    conn = sqlite3.connect(str(db_path))
    # 同时确保 symbol_contents 存在（用 symbol_hash 作 PK）
    conn.execute(
        "INSERT OR IGNORE INTO symbol_contents (content_hash, name, kind, content, "
        "signature, has_comment, comment_content, qualified_name) "
        "VALUES (?, ?, 'function', '', '', 0, '', ?)",
        (symbol_hash, qualified_name, qualified_name),
    )
    conn.execute(
        "INSERT INTO file_symbol_versions "
        "(file_version_id, symbol_hash, qualified_name, start_line, end_line, "
        "module_path, depth, is_deleted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (file_version_id, symbol_hash, qualified_name,
         start_line, end_line, module_path, depth, is_deleted),
    )
    conn.commit()
    conn.close()


def _make_version_with_symbols(db_path, file_instance_id, version_num,
                                 content_hash, symbols):
    """创建一个新 file_version 并写入符号。

    Args:
        symbols: List[Dict] 每项含 qualified_name/start_line/end_line/module_path/depth/symbol_hash

    Returns: file_version_id
    """
    conn = sqlite3.connect(str(db_path))
    now = time.time()
    # 旧版本 is_current=0
    conn.execute(
        "UPDATE file_versions SET is_current = 0 WHERE file_instance_id = ?",
        (file_instance_id,),
    )
    cur = conn.execute(
        "INSERT INTO file_versions (file_instance_id, version_num, content_hash, mtime, "
        "total_lines, parsed_at, is_current, is_deleted, commit_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, 0, '')",
        (file_instance_id, version_num, content_hash, now, 10, now),
    )
    file_version_id = cur.lastrowid
    for s in symbols:
        conn.execute(
            "INSERT OR IGNORE INTO symbol_contents (content_hash, name, kind, content, "
            "signature, has_comment, comment_content, qualified_name) "
            "VALUES (?, ?, 'function', '', '', 0, '', ?)",
            (s["symbol_hash"], s["qualified_name"], s["qualified_name"]),
        )
        conn.execute(
            "INSERT INTO file_symbol_versions "
            "(file_version_id, symbol_hash, qualified_name, start_line, end_line, "
            "module_path, depth, is_deleted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (file_version_id, s["symbol_hash"], s["qualified_name"],
             s["start_line"], s["end_line"], s.get("module_path", ""),
             s.get("depth", -1)),
        )
    conn.commit()
    conn.close()
    return file_version_id


def _insert_call(db_path, file_instance_id, caller_name="fn1",
                  callee_name="callee1", call_line=10, callee_module="",
                  callee_qualified="", callee_file="", callee_id=0,
                  is_cross_file=0):
    """插入 symbol + call 记录（用于 L1/L6/L7 测试）"""
    conn = sqlite3.connect(str(db_path))
    now = time.time()
    # 插入 symbol（caller）
    cur = conn.execute(
        "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, visibility, "
        "start_line, end_line, module_path, qualified_name) "
        "VALUES (?, ?, ?, 'function', 'private', ?, ?, '', ?)",
        (file_instance_id, f"sh_{caller_name}", caller_name, call_line, call_line + 5, caller_name),
    )
    caller_id = cur.lastrowid
    conn.execute(
        "INSERT INTO calls (caller_id, caller_name, caller_module, callee_name, callee_module, "
        "callee_qualified, callee_file, callee_id, call_line, is_cross_file) "
        "VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?)",
        (caller_id, caller_name, callee_name, callee_module, callee_qualified,
         callee_file, callee_id, call_line, is_cross_file),
    )
    conn.commit()
    conn.close()


# ============================================
# Python 路径与 Rust 路径封装
# ============================================

def _py_compute_symbol_diff(codegraph_db_path, prev_version_id, curr_version_id):
    """Python 路径：调用 db_build.BuildMixin._compute_and_apply_symbol_diff_python（unbound method）

    直接调用 _python 后缀方法，跳过 Rust 短路调度器（_MinimalDb 无 is_feature_rolled_back）。
    """
    from callwarden.db.db_build import BuildMixin

    class _MinimalDb:
        def __init__(self, conn):
            self.conn = conn

    conn = sqlite3.connect(str(codegraph_db_path))
    conn.row_factory = sqlite3.Row
    db = _MinimalDb(conn)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        BuildMixin._compute_and_apply_symbol_diff_python(db, prev_version_id, curr_version_id)
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()


def _rust_compute_symbol_diff(codegraph_db_path, prev_version_id, curr_version_id):
    """Rust 路径：调用 callwarden_core.compute_and_apply_symbol_diff

    Rust 端内部打开读写连接 + BEGIN IMMEDIATE → 全部 SQL → COMMIT。
    """
    return callwarden_core.compute_and_apply_symbol_diff(
        str(codegraph_db_path),
        prev_version_id,
        curr_version_id,
    )


def _py_load_file_result_from_db(codegraph_db_path, file_instance_id, file_version_id,
                                    rel_path, abs_path, module_path):
    """Python 路径：调用 db_build.BuildMixin._load_file_result_from_db_python（unbound method）

    直接调用 _python 后缀方法，跳过 Rust 短路调度器。
    """
    from callwarden.db.db_build import BuildMixin

    class _MinimalDb:
        def __init__(self, conn):
            self.conn = conn

    conn = sqlite3.connect(str(codegraph_db_path))
    conn.row_factory = sqlite3.Row
    db = _MinimalDb(conn)
    try:
        result = BuildMixin._load_file_result_from_db_python(
            db, file_instance_id, file_version_id, rel_path, abs_path, module_path
        )
        # 将 sqlite3.Row 转为 dict（递归）
        return _normalize_row(result)
    finally:
        conn.close()


def _rust_load_file_result_from_db(codegraph_db_path, file_instance_id, file_version_id,
                                     rel_path, abs_path, module_path):
    """Rust 路径：调用 callwarden_core.load_file_result_from_db

    返回 dict 或 None。Rust 端用 PyDict 组装，字段名与 Python 一致。
    """
    return callwarden_core.load_file_result_from_db(
        str(codegraph_db_path),
        file_instance_id,
        file_version_id,
        rel_path,
        abs_path,
        module_path,
    )


def _normalize_row(obj):
    """递归将 sqlite3.Row / list / dict 转为纯 dict / list（便于差分对比）"""
    if obj is None:
        return None
    if isinstance(obj, sqlite3.Row):
        return {k: obj[k] for k in obj.keys()}
    if isinstance(obj, dict):
        return {k: _normalize_row(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_row(x) for x in obj]
    return obj


# ============================================
# 查询辅助函数
# ============================================

def _query_fsv(codegraph_db_path, file_version_id):
    """查询 file_symbol_versions（按 id 排序）"""
    conn = sqlite3.connect(str(codegraph_db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, file_version_id, symbol_hash, qualified_name, start_line, end_line, "
        "module_path, depth, is_deleted "
        "FROM file_symbol_versions WHERE file_version_id = ? ORDER BY id",
        (file_version_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _count_fsv(codegraph_db_path, file_version_id):
    """统计 file_symbol_versions 行数"""
    conn = sqlite3.connect(str(codegraph_db_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM file_symbol_versions WHERE file_version_id = ?",
        (file_version_id,),
    ).fetchone()[0]
    conn.close()
    return count


def _count_deleted_fsv(codegraph_db_path, file_version_id):
    """统计 is_deleted=1 的行数"""
    conn = sqlite3.connect(str(codegraph_db_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM file_symbol_versions WHERE file_version_id = ? AND is_deleted = 1",
        (file_version_id,),
    ).fetchone()[0]
    conn.close()
    return count


# ============================================
# TestComputeSymbolDiffDiff：D1-D10
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestComputeSymbolDiffDiff:
    """compute_and_apply_symbol_diff 差分测试（D1-D10）

    差分策略：
    1. 两端各用一个独立的 DB 副本（避免 Python 已写入影响 Rust 端）
    2. 在两端分别预填 prev_version + curr_version 的符号
    3. 调用各自的 compute_and_apply_symbol_diff
    4. 断言：返回值（removed_count/removed_names）一致 + DB 内容（fsv 表）一致
    """

    def _setup_two_dbs(self, tmp_path, prev_symbols, curr_symbols):
        """构造两个相同 schema 的 DB，分别预填 prev + curr 符号。

        Args:
            prev_symbols: List[Dict] 含 qualified_name/symbol_hash/start_line/end_line/module_path/depth
            curr_symbols: 同上

        Returns: (py_db, rust_db, prev_version_id, curr_version_id)
            其中 prev_version_id / curr_version_id 在两个 DB 中相同
        """
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        # 两个 DB 各自预填 workspace + file_instance + 2 个 file_versions
        for db_path in [py_db, rust_db]:
            file_instance_id, prev_version_id = _prep_workspace_file_version(
                db_path, content_hash="ch_prev"
            )
            # 创建 curr_version（version_num=2，新内容）
            curr_version_id = _make_version_with_symbols(
                db_path, file_instance_id, version_num=2,
                content_hash="ch_curr", symbols=curr_symbols,
            )
            # 在 prev_version 中写入 prev_symbols
            for s in prev_symbols:
                _insert_symbol_version(
                    db_path, prev_version_id,
                    symbol_hash=s["symbol_hash"],
                    qualified_name=s["qualified_name"],
                    start_line=s["start_line"],
                    end_line=s["end_line"],
                    module_path=s.get("module_path", ""),
                    depth=s.get("depth", -1),
                )

        return py_db, rust_db, prev_version_id, curr_version_id

    def _assert_diff_consistent(self, py_db, rust_db, prev_version_id, curr_version_id,
                                  expected_removed_count):
        """断言 Python↔Rust 两端 DB 状态一致"""
        py_fsv = _query_fsv(py_db, curr_version_id)
        rust_fsv = _query_fsv(rust_db, curr_version_id)
        # 行数一致
        assert len(py_fsv) == len(rust_fsv), (
            f"file_symbol_versions 行数不一致: py={len(py_fsv)}, rust={len(rust_fsv)}"
        )
        # is_deleted=1 行数一致
        py_deleted = sum(1 for r in py_fsv if r["is_deleted"] == 1)
        rust_deleted = sum(1 for r in rust_fsv if r["is_deleted"] == 1)
        assert py_deleted == rust_deleted == expected_removed_count, (
            f"is_deleted=1 行数不一致: py={py_deleted}, rust={rust_deleted}, "
            f"expected={expected_removed_count}"
        )
        # 删除的符号 qualified_name 集合一致（按 qualified_name 排序）
        py_removed_names = sorted(r["qualified_name"] for r in py_fsv if r["is_deleted"] == 1)
        rust_removed_names = sorted(r["qualified_name"] for r in rust_fsv if r["is_deleted"] == 1)
        assert py_removed_names == rust_removed_names, (
            f"删除符号集合不一致: py={py_removed_names}, rust={rust_removed_names}"
        )
        # 删除符号的位置信息一致（start_line/end_line/module_path/depth）
        py_removed_map = {
            r["qualified_name"]: r for r in py_fsv if r["is_deleted"] == 1
        }
        rust_removed_map = {
            r["qualified_name"]: r for r in rust_fsv if r["is_deleted"] == 1
        }
        for name in py_removed_names:
            py_r = py_removed_map[name]
            rust_r = rust_removed_map[name]
            for field in ["start_line", "end_line", "module_path", "depth", "symbol_hash"]:
                assert py_r[field] == rust_r[field], (
                    f"符号 {name} 的 {field} 不一致: py={py_r[field]}, rust={rust_r[field]}"
                )

    # ---------- D1: 无删除符号 ----------
    def test_d1_no_removed(self, tmp_path):
        """D1: prev={A,B}, curr={A,B} → removed_count=0"""
        symbols = [
            {"qualified_name": "A", "symbol_hash": "sh_A", "start_line": 1, "end_line": 10},
            {"qualified_name": "B", "symbol_hash": "sh_B", "start_line": 11, "end_line": 20},
        ]
        py_db, rust_db, prev_vid, curr_vid = self._setup_two_dbs(tmp_path, symbols, symbols)

        # Python 路径
        _py_compute_symbol_diff(py_db, prev_vid, curr_vid)
        # Rust 路径
        rust_ret = _rust_compute_symbol_diff(rust_db, prev_vid, curr_vid)

        assert rust_ret["success"] is True
        assert rust_ret["removed_count"] == 0
        assert rust_ret["removed_names"] == []
        self._assert_diff_consistent(py_db, rust_db, prev_vid, curr_vid, expected_removed_count=0)

    # ---------- D2: 全部删除 ----------
    def test_d2_all_removed(self, tmp_path):
        """D2: prev={A,B}, curr={} → removed_count=2"""
        prev_symbols = [
            {"qualified_name": "A", "symbol_hash": "sh_A", "start_line": 1, "end_line": 10},
            {"qualified_name": "B", "symbol_hash": "sh_B", "start_line": 11, "end_line": 20},
        ]
        py_db, rust_db, prev_vid, curr_vid = self._setup_two_dbs(tmp_path, prev_symbols, [])

        _py_compute_symbol_diff(py_db, prev_vid, curr_vid)
        rust_ret = _rust_compute_symbol_diff(rust_db, prev_vid, curr_vid)

        assert rust_ret["success"] is True
        assert rust_ret["removed_count"] == 2
        assert set(rust_ret["removed_names"]) == {"A", "B"}
        self._assert_diff_consistent(py_db, rust_db, prev_vid, curr_vid, expected_removed_count=2)

    # ---------- D3: 部分删除 ----------
    def test_d3_partial_removed(self, tmp_path):
        """D3: prev={A,B,C}, curr={B} → removed_count=2（A, C）"""
        prev_symbols = [
            {"qualified_name": "A", "symbol_hash": "sh_A", "start_line": 1, "end_line": 10},
            {"qualified_name": "B", "symbol_hash": "sh_B", "start_line": 11, "end_line": 20},
            {"qualified_name": "C", "symbol_hash": "sh_C", "start_line": 21, "end_line": 30},
        ]
        curr_symbols = [
            {"qualified_name": "B", "symbol_hash": "sh_B", "start_line": 11, "end_line": 20},
        ]
        py_db, rust_db, prev_vid, curr_vid = self._setup_two_dbs(tmp_path, prev_symbols, curr_symbols)

        _py_compute_symbol_diff(py_db, prev_vid, curr_vid)
        rust_ret = _rust_compute_symbol_diff(rust_db, prev_vid, curr_vid)

        assert rust_ret["success"] is True
        assert rust_ret["removed_count"] == 2
        assert set(rust_ret["removed_names"]) == {"A", "C"}
        self._assert_diff_consistent(py_db, rust_db, prev_vid, curr_vid, expected_removed_count=2)

    # ---------- D4: 新增符号 ----------
    def test_d4_added(self, tmp_path):
        """D4: prev={A}, curr={A,B} → removed_count=0（B 是新增，不处理）"""
        prev_symbols = [
            {"qualified_name": "A", "symbol_hash": "sh_A", "start_line": 1, "end_line": 10},
        ]
        curr_symbols = [
            {"qualified_name": "A", "symbol_hash": "sh_A", "start_line": 1, "end_line": 10},
            {"qualified_name": "B", "symbol_hash": "sh_B", "start_line": 11, "end_line": 20},
        ]
        py_db, rust_db, prev_vid, curr_vid = self._setup_two_dbs(tmp_path, prev_symbols, curr_symbols)

        _py_compute_symbol_diff(py_db, prev_vid, curr_vid)
        rust_ret = _rust_compute_symbol_diff(rust_db, prev_vid, curr_vid)

        assert rust_ret["success"] is True
        assert rust_ret["removed_count"] == 0
        assert rust_ret["removed_names"] == []
        self._assert_diff_consistent(py_db, rust_db, prev_vid, curr_vid, expected_removed_count=0)

    # ---------- D5: 混合变更 ----------
    def test_d5_mixed(self, tmp_path):
        """D5: prev={A,B,C}, curr={B,C,D} → removed_count=1（A），D 是新增不处理"""
        prev_symbols = [
            {"qualified_name": "A", "symbol_hash": "sh_A", "start_line": 1, "end_line": 10},
            {"qualified_name": "B", "symbol_hash": "sh_B", "start_line": 11, "end_line": 20},
            {"qualified_name": "C", "symbol_hash": "sh_C", "start_line": 21, "end_line": 30},
        ]
        curr_symbols = [
            {"qualified_name": "B", "symbol_hash": "sh_B", "start_line": 11, "end_line": 20},
            {"qualified_name": "C", "symbol_hash": "sh_C", "start_line": 21, "end_line": 30},
            {"qualified_name": "D", "symbol_hash": "sh_D", "start_line": 31, "end_line": 40},
        ]
        py_db, rust_db, prev_vid, curr_vid = self._setup_two_dbs(tmp_path, prev_symbols, curr_symbols)

        _py_compute_symbol_diff(py_db, prev_vid, curr_vid)
        rust_ret = _rust_compute_symbol_diff(rust_db, prev_vid, curr_vid)

        assert rust_ret["success"] is True
        assert rust_ret["removed_count"] == 1
        assert set(rust_ret["removed_names"]) == {"A"}
        self._assert_diff_consistent(py_db, rust_db, prev_vid, curr_vid, expected_removed_count=1)

    # ---------- D6: prev 为空 ----------
    def test_d6_empty_prev(self, tmp_path):
        """D6: prev={}, curr={A,B} → removed_count=0"""
        curr_symbols = [
            {"qualified_name": "A", "symbol_hash": "sh_A", "start_line": 1, "end_line": 10},
            {"qualified_name": "B", "symbol_hash": "sh_B", "start_line": 11, "end_line": 20},
        ]
        py_db, rust_db, prev_vid, curr_vid = self._setup_two_dbs(tmp_path, [], curr_symbols)

        _py_compute_symbol_diff(py_db, prev_vid, curr_vid)
        rust_ret = _rust_compute_symbol_diff(rust_db, prev_vid, curr_vid)

        assert rust_ret["success"] is True
        assert rust_ret["removed_count"] == 0
        assert rust_ret["removed_names"] == []
        self._assert_diff_consistent(py_db, rust_db, prev_vid, curr_vid, expected_removed_count=0)

    # ---------- D7: curr 为空 ----------
    def test_d7_empty_curr(self, tmp_path):
        """D7: prev={A,B}, curr={} → removed_count=2"""
        prev_symbols = [
            {"qualified_name": "A", "symbol_hash": "sh_A", "start_line": 1, "end_line": 10},
            {"qualified_name": "B", "symbol_hash": "sh_B", "start_line": 11, "end_line": 20},
        ]
        py_db, rust_db, prev_vid, curr_vid = self._setup_two_dbs(tmp_path, prev_symbols, [])

        _py_compute_symbol_diff(py_db, prev_vid, curr_vid)
        rust_ret = _rust_compute_symbol_diff(rust_db, prev_vid, curr_vid)

        assert rust_ret["success"] is True
        assert rust_ret["removed_count"] == 2
        assert set(rust_ret["removed_names"]) == {"A", "B"}
        self._assert_diff_consistent(py_db, rust_db, prev_vid, curr_vid, expected_removed_count=2)

    # ---------- D8: prev_version_id 不存在 ----------
    def test_d8_prev_not_exist(self, tmp_path):
        """D8: prev_version_id=999999 不存在 → success=true（与 Python 一致：prev_symbols 为空）

        Python 路径：`SELECT ... WHERE file_version_id = 999999` 返回空，prev_symbols={}，
        removed_names = set() - set(curr_names) = set()，无 INSERT，无异常。

        Rust 路径：同样查询返回空，prev_symbols 空，removed_names 空，无 INSERT，
        返回 success=true（无错误）。
        """
        # 构造一个有 curr 版本的 DB
        curr_symbols = [
            {"qualified_name": "A", "symbol_hash": "sh_A", "start_line": 1, "end_line": 10},
        ]
        py_db, rust_db, _, curr_vid = self._setup_two_dbs(tmp_path, [], curr_symbols)
        nonexistent_prev_vid = 999999

        # Python 路径
        _py_compute_symbol_diff(py_db, nonexistent_prev_vid, curr_vid)
        # Rust 路径
        rust_ret = _rust_compute_symbol_diff(rust_db, nonexistent_prev_vid, curr_vid)

        # 两端都应成功（无错误，无删除）
        assert rust_ret["success"] is True
        assert rust_ret["removed_count"] == 0
        assert rust_ret["removed_names"] == []
        # DB 状态一致：curr_version 无新增 is_deleted=1 行
        self._assert_diff_consistent(py_db, rust_db, nonexistent_prev_vid, curr_vid, expected_removed_count=0)

    # ---------- D9: curr_version_id 不存在 ----------
    def test_d9_curr_not_exist(self, tmp_path):
        """D9: curr_version_id=999999 不存在 → 两端都 INSERT 成功（foreign_keys=OFF 默认）

        说明：SQLite 默认 `PRAGMA foreign_keys=OFF`，Python sqlite3 和 Rust rusqlite
        均不显式启用 FK 检查。因此 INSERT 到不存在的 file_version_id 不会抛 IntegrityError，
        两端都 INSERT 2 条 is_deleted=1 记录。

        Python 路径：prev_symbols={A,B}, curr_symbols={}（curr_version_id 不存在），
        removed_names={A,B}，每条 INSERT 一条 is_deleted=1 记录到 file_version_id=999999。

        Rust 路径：同样 prev_symbols={A,B}, curr_symbols={}，INSERT 2 条，
        返回 success=true, removed_count=2。
        """
        prev_symbols = [
            {"qualified_name": "A", "symbol_hash": "sh_A", "start_line": 1, "end_line": 10},
            {"qualified_name": "B", "symbol_hash": "sh_B", "start_line": 11, "end_line": 20},
        ]
        py_db, rust_db, prev_vid, _ = self._setup_two_dbs(tmp_path, prev_symbols, [])
        nonexistent_curr_vid = 999999

        # Python 路径：不会 FK 失败（foreign_keys=OFF），INSERT 成功
        _py_compute_symbol_diff(py_db, prev_vid, nonexistent_curr_vid)

        # Rust 路径：同样 INSERT 成功，返回 success=true
        rust_ret = _rust_compute_symbol_diff(rust_db, prev_vid, nonexistent_curr_vid)
        assert rust_ret["success"] is True
        assert rust_ret["removed_count"] == 2
        assert set(rust_ret["removed_names"]) == {"A", "B"}

        # 验证两端都写入了 2 条 is_deleted=1 记录到 curr_version_id=999999
        py_deleted = _count_deleted_fsv(py_db, nonexistent_curr_vid)
        rust_deleted = _count_deleted_fsv(rust_db, nonexistent_curr_vid)
        assert py_deleted == 2
        assert rust_deleted == 2

    # ---------- D10: 位置信息正确性 ----------
    def test_d10_position_correctness(self, tmp_path):
        """D10: 删除符号的位置信息（start_line/end_line/module_path/depth）与 prev 一致"""
        prev_symbols = [
            {"qualified_name": "fn_a", "symbol_hash": "sh_a",
             "start_line": 10, "end_line": 20, "module_path": "mod_a", "depth": 1},
            {"qualified_name": "fn_b", "symbol_hash": "sh_b",
             "start_line": 30, "end_line": 40, "module_path": "mod_b", "depth": 2},
            {"qualified_name": "fn_c", "symbol_hash": "sh_c",
             "start_line": 50, "end_line": 60, "module_path": "mod_c", "depth": 3},
        ]
        curr_symbols = [
            {"qualified_name": "fn_b", "symbol_hash": "sh_b",
             "start_line": 30, "end_line": 40, "module_path": "mod_b", "depth": 2},
        ]
        py_db, rust_db, prev_vid, curr_vid = self._setup_two_dbs(tmp_path, prev_symbols, curr_symbols)

        _py_compute_symbol_diff(py_db, prev_vid, curr_vid)
        rust_ret = _rust_compute_symbol_diff(rust_db, prev_vid, curr_vid)

        assert rust_ret["success"] is True
        assert rust_ret["removed_count"] == 2
        assert set(rust_ret["removed_names"]) == {"fn_a", "fn_c"}

        # 验证删除符号的位置信息与 prev 一致
        py_fsv = _query_fsv(py_db, curr_vid)
        rust_fsv = _query_fsv(rust_db, curr_vid)

        py_deleted = {r["qualified_name"]: r for r in py_fsv if r["is_deleted"] == 1}
        rust_deleted = {r["qualified_name"]: r for r in rust_fsv if r["is_deleted"] == 1}

        for name in ["fn_a", "fn_c"]:
            assert name in py_deleted
            assert name in rust_deleted
            # 位置信息与 prev 一致
            prev_sym = next(s for s in prev_symbols if s["qualified_name"] == name)
            for field, prev_val in [
                ("start_line", prev_sym["start_line"]),
                ("end_line", prev_sym["end_line"]),
                ("module_path", prev_sym["module_path"]),
                ("depth", prev_sym["depth"]),
                ("symbol_hash", prev_sym["symbol_hash"]),
            ]:
                assert py_deleted[name][field] == prev_val, (
                    f"Python 路径 {name}.{field}={py_deleted[name][field]} 与 prev={prev_val} 不一致"
                )
                assert rust_deleted[name][field] == prev_val, (
                    f"Rust 路径 {name}.{field}={rust_deleted[name][field]} 与 prev={prev_val} 不一致"
                )


# ============================================
# TestLoadFileResultFromDbDiff：L1-L7
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestLoadFileResultFromDbDiff:
    """load_file_result_from_db 差分测试（L1-L7）

    差分策略：
    1. 两端共用一个 DB（只读查询，无副作用）
    2. 预填 file_versions + file_symbol_versions + symbol_contents + calls + symbols
    3. 调用各自的 load_file_result_from_db
    4. 断言返回 dict 结构一致（symbols/raw_calls/content_hash/total_lines/_from_db）
    """

    def _setup_db_with_version_and_symbols(self, db_path, symbols, calls=None,
                                             content_hash="ch_v1", total_lines=100):
        """预填 DB：workspace + file_instance + file_version + symbols + calls

        Args:
            symbols: List[Dict] 含 qualified_name/symbol_hash/start_line/end_line/
                     module_path/depth/name/kind/content/signature/has_comment/doc_comment
            calls: List[Dict] 含 caller_name/callee_name/...

        Returns: (file_instance_id, file_version_id)
        """
        calls = calls or []
        file_instance_id, file_version_id = _prep_workspace_file_version(
            db_path, content_hash=content_hash, total_lines=total_lines
        )
        # 写入 symbol_contents + file_symbol_versions
        for s in symbols:
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "INSERT OR IGNORE INTO symbol_contents "
                "(content_hash, name, kind, content, signature, has_comment, "
                "comment_content, qualified_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (s["symbol_hash"], s.get("name", s["qualified_name"]),
                 s.get("kind", "function"), s.get("content", ""),
                 s.get("signature", ""), s.get("has_comment", 0),
                 s.get("doc_comment", ""), s["qualified_name"]),
            )
            conn.execute(
                "INSERT INTO file_symbol_versions "
                "(file_version_id, symbol_hash, qualified_name, start_line, end_line, "
                "module_path, depth, is_deleted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (file_version_id, s["symbol_hash"], s["qualified_name"],
                 s["start_line"], s["end_line"],
                 s.get("module_path", ""), s.get("depth", -1),
                 s.get("is_deleted", 0)),
            )
            conn.commit()
            conn.close()
        # 写入 symbols + calls
        for c in calls:
            _insert_call(
                db_path, file_instance_id,
                caller_name=c.get("caller_name", "fn1"),
                callee_name=c.get("callee_name", "callee1"),
                call_line=c.get("call_line", 10),
                callee_module=c.get("callee_module", ""),
                callee_qualified=c.get("callee_qualified", ""),
                callee_file=c.get("callee_file", ""),
                callee_id=c.get("callee_id", 0),
                is_cross_file=c.get("is_cross_file", 0),
            )
        return file_instance_id, file_version_id

    def _normalize_result(self, result):
        """规范化 load_file_result 返回值，便于差分对比。

        - symbols 列表按 qualified_name 排序（避免顺序差异）
        - raw_calls 列表按 (caller_name, callee_name, call_line) 排序
        - 每个 symbol dict 只保留关键字段（避免额外字段差异）
        """
        if result is None:
            return None
        result = _normalize_row(result)
        # 排序 symbols
        if "symbols" in result:
            symbols = result["symbols"]
            # 每个 sym 只保留关键字段
            key_fields = ["id", "symbol_hash", "qualified_name", "start_line", "end_line",
                          "module_path", "depth", "is_deleted", "name", "kind", "content",
                          "signature", "has_comment", "doc_comment"]
            normalized_syms = []
            for sym in symbols:
                normalized_sym = {k: sym.get(k) for k in key_fields}
                # calls/issues 列表应存在（Python 添加空列表）
                normalized_sym["calls"] = sym.get("calls", [])
                normalized_sym["issues"] = sym.get("issues", [])
                normalized_syms.append(normalized_sym)
            normalized_syms.sort(key=lambda x: x["qualified_name"])
            result["symbols"] = normalized_syms
        # 排序 raw_calls
        if "raw_calls" in result:
            calls = result["raw_calls"]
            calls.sort(key=lambda x: (x.get("caller_name", ""), x.get("callee_name", ""),
                                       x.get("call_line", 0)))
            result["raw_calls"] = calls
        return result

    def _assert_results_equal(self, py_result, rust_result):
        """断言 Python↔Rust 两端 load_file_result 返回值一致"""
        py_norm = self._normalize_result(py_result)
        rust_norm = self._normalize_result(rust_result)
        if py_norm is None:
            assert rust_norm is None, "Python 返回 None，Rust 应也返回 None"
            return
        assert rust_norm is not None, "Python 返回 dict，Rust 不应返回 None"
        # 顶层字段
        for key in ["abs_path", "rel_path", "module_path", "file_instance_id",
                    "file_version_id", "content_hash", "total_lines", "_from_db",
                    "imports", "inline_modules"]:
            assert py_norm[key] == rust_norm[key], (
                f"字段 {key} 不一致: py={py_norm[key]!r}, rust={rust_norm[key]!r}"
            )
        # symbols
        assert len(py_norm["symbols"]) == len(rust_norm["symbols"]), (
            f"symbols 长度不一致: py={len(py_norm['symbols'])}, rust={len(rust_norm['symbols'])}"
        )
        for py_sym, rust_sym in zip(py_norm["symbols"], rust_norm["symbols"]):
            for key in py_sym:
                assert py_sym[key] == rust_sym[key], (
                    f"symbol.{key} 不一致: py={py_sym[key]!r}, rust={rust_sym[key]!r}"
                )
        # raw_calls
        assert len(py_norm["raw_calls"]) == len(rust_norm["raw_calls"]), (
            f"raw_calls 长度不一致: py={len(py_norm['raw_calls'])}, rust={len(rust_norm['raw_calls'])}"
        )
        for py_call, rust_call in zip(py_norm["raw_calls"], rust_norm["raw_calls"]):
            for key in py_call:
                assert py_call[key] == rust_call[key], (
                    f"raw_calls.{key} 不一致: py={py_call[key]!r}, rust={rust_call[key]!r}"
                )

    # ---------- L1: 正常加载 ----------
    def test_l1_normal_load(self, tmp_path):
        """L1: file_version_id 存在，3 符号 + 2 calls → 返回 dict，symbols=[3], raw_calls=[2]"""
        db_path = tmp_path / "test.db"
        _make_codegraph_db(db_path)
        symbols = [
            {"qualified_name": "fn_a", "symbol_hash": "sh_a",
             "start_line": 1, "end_line": 10, "name": "fn_a", "kind": "function"},
            {"qualified_name": "fn_b", "symbol_hash": "sh_b",
             "start_line": 11, "end_line": 20, "name": "fn_b", "kind": "function"},
            {"qualified_name": "fn_c", "symbol_hash": "sh_c",
             "start_line": 21, "end_line": 30, "name": "fn_c", "kind": "function"},
        ]
        calls = [
            {"caller_name": "fn_a", "callee_name": "fn_b", "call_line": 5},
            {"caller_name": "fn_b", "callee_name": "fn_c", "call_line": 15},
        ]
        file_instance_id, file_version_id = self._setup_db_with_version_and_symbols(
            db_path, symbols, calls
        )

        py_result = _py_load_file_result_from_db(
            db_path, file_instance_id, file_version_id,
            "src/main.py", "/app/src/main.py", ""
        )
        rust_result = _rust_load_file_result_from_db(
            db_path, file_instance_id, file_version_id,
            "src/main.py", "/app/src/main.py", ""
        )

        self._assert_results_equal(py_result, rust_result)
        # 验证具体值
        assert py_result["symbols"] is not None
        assert len(py_result["symbols"]) == 3
        assert len(py_result["raw_calls"]) == 2
        assert py_result["_from_db"] is True

    # ---------- L2: 空版本 ----------
    def test_l2_empty_version(self, tmp_path):
        """L2: file_version_id 存在，0 符号 0 calls → 返回 dict，symbols=[], raw_calls=[]"""
        db_path = tmp_path / "test.db"
        _make_codegraph_db(db_path)
        file_instance_id, file_version_id = _prep_workspace_file_version(db_path)

        py_result = _py_load_file_result_from_db(
            db_path, file_instance_id, file_version_id,
            "src/main.py", "/app/src/main.py", ""
        )
        rust_result = _rust_load_file_result_from_db(
            db_path, file_instance_id, file_version_id,
            "src/main.py", "/app/src/main.py", ""
        )

        self._assert_results_equal(py_result, rust_result)
        assert len(py_result["symbols"]) == 0
        assert len(py_result["raw_calls"]) == 0

    # ---------- L3: version 不存在 ----------
    def test_l3_version_not_exist(self, tmp_path):
        """L3: file_version_id=999999 → 返回 None"""
        db_path = tmp_path / "test.db"
        _make_codegraph_db(db_path)
        file_instance_id, _ = _prep_workspace_file_version(db_path)
        nonexistent_version_id = 999999

        py_result = _py_load_file_result_from_db(
            db_path, file_instance_id, nonexistent_version_id,
            "src/main.py", "/app/src/main.py", ""
        )
        rust_result = _rust_load_file_result_from_db(
            db_path, file_instance_id, nonexistent_version_id,
            "src/main.py", "/app/src/main.py", ""
        )

        assert py_result is None
        assert rust_result is None

    # ---------- L4: is_deleted 过滤 ----------
    def test_l4_is_deleted_filter(self, tmp_path):
        """L4: file_version 有 5 符号，其中 2 个 is_deleted=1 → symbols 只含 3 个 is_deleted=0"""
        db_path = tmp_path / "test.db"
        _make_codegraph_db(db_path)
        symbols = [
            {"qualified_name": "fn_a", "symbol_hash": "sh_a", "start_line": 1, "end_line": 10},
            {"qualified_name": "fn_b", "symbol_hash": "sh_b", "start_line": 11, "end_line": 20},
            {"qualified_name": "fn_c", "symbol_hash": "sh_c", "start_line": 21, "end_line": 30},
            {"qualified_name": "fn_d", "symbol_hash": "sh_d", "start_line": 31, "end_line": 40,
             "is_deleted": 1},
            {"qualified_name": "fn_e", "symbol_hash": "sh_e", "start_line": 41, "end_line": 50,
             "is_deleted": 1},
        ]
        file_instance_id, file_version_id = self._setup_db_with_version_and_symbols(db_path, symbols)

        py_result = _py_load_file_result_from_db(
            db_path, file_instance_id, file_version_id,
            "src/main.py", "/app/src/main.py", ""
        )
        rust_result = _rust_load_file_result_from_db(
            db_path, file_instance_id, file_version_id,
            "src/main.py", "/app/src/main.py", ""
        )

        self._assert_results_equal(py_result, rust_result)
        # 验证 is_deleted=0 过滤
        assert len(py_result["symbols"]) == 3
        assert all(s["is_deleted"] == 0 for s in py_result["symbols"])

    # ---------- L5: content_hash + total_lines 字段一致性 ----------
    def test_l5_content_hash_and_total_lines(self, tmp_path):
        """L5: file_versions.content_hash="abc", total_lines=100 → dict 字段一致"""
        db_path = tmp_path / "test.db"
        _make_codegraph_db(db_path)
        symbols = [
            {"qualified_name": "fn_a", "symbol_hash": "sh_a", "start_line": 1, "end_line": 10},
        ]
        file_instance_id, file_version_id = self._setup_db_with_version_and_symbols(
            db_path, symbols, content_hash="ch_abc_123", total_lines=100
        )

        py_result = _py_load_file_result_from_db(
            db_path, file_instance_id, file_version_id,
            "src/main.py", "/app/src/main.py", ""
        )
        rust_result = _rust_load_file_result_from_db(
            db_path, file_instance_id, file_version_id,
            "src/main.py", "/app/src/main.py", ""
        )

        self._assert_results_equal(py_result, rust_result)
        assert py_result["content_hash"] == "ch_abc_123"
        assert py_result["total_lines"] == 100

    # ---------- L6: calls 关联 ----------
    def test_l6_calls_association(self, tmp_path):
        """L6: calls 通过 caller_id JOIN symbols.file_instance_id 关联"""
        db_path = tmp_path / "test.db"
        _make_codegraph_db(db_path)
        symbols = [
            {"qualified_name": "fn_a", "symbol_hash": "sh_a", "start_line": 1, "end_line": 10},
        ]
        calls = [
            {"caller_name": "fn_a", "callee_name": "ext_callee",
             "call_line": 5, "callee_module": "ext_mod",
             "callee_qualified": "ext_mod.ext_callee", "callee_file": "ext.py",
             "callee_id": 99, "is_cross_file": 1},
        ]
        file_instance_id, file_version_id = self._setup_db_with_version_and_symbols(db_path, symbols, calls)

        # 额外插入一个其他 file_instance 的 call，验证不会混淆
        other_instance_id, other_version_id = _prep_workspace_file_version(
            db_path, workspace_id=2, rel_path="other.py", abs_path="/app/other.py",
            content_hash="ch_other"
        )
        _insert_call(
            db_path, other_instance_id,
            caller_name="fn_a", callee_name="other_callee", call_line=100
        )

        py_result = _py_load_file_result_from_db(
            db_path, file_instance_id, file_version_id,
            "src/main.py", "/app/src/main.py", ""
        )
        rust_result = _rust_load_file_result_from_db(
            db_path, file_instance_id, file_version_id,
            "src/main.py", "/app/src/main.py", ""
        )

        self._assert_results_equal(py_result, rust_result)
        # 验证只含当前 file_instance_id 的 calls
        assert len(py_result["raw_calls"]) == 1
        assert py_result["raw_calls"][0]["callee_name"] == "ext_callee"
        assert py_result["raw_calls"][0]["callee_id"] == 99

    # ---------- L7: 字段完整性 ----------
    def test_l7_field_completeness(self, tmp_path):
        """L7: 符号含 name/kind/content/signature/has_comment/doc_comment 等所有字段"""
        db_path = tmp_path / "test.db"
        _make_codegraph_db(db_path)
        symbols = [
            {
                "qualified_name": "fn_full", "symbol_hash": "sh_full",
                "start_line": 10, "end_line": 50, "module_path": "mod.full", "depth": 2,
                "name": "fn_full", "kind": "function", "content": "def fn_full(): pass",
                "signature": "fn_full() -> None", "has_comment": 1, "doc_comment": "docstring",
            },
            {
                "qualified_name": "ClassA", "symbol_hash": "sh_class",
                "start_line": 60, "end_line": 100, "module_path": "mod.class", "depth": 1,
                "name": "ClassA", "kind": "class", "content": "class ClassA: pass",
                "signature": "", "has_comment": 0, "doc_comment": "",
            },
        ]
        calls = [
            {"caller_name": "fn_full", "callee_name": "ext_fn",
             "call_line": 20, "callee_module": "ext",
             "callee_qualified": "ext.ext_fn", "callee_file": "ext.py",
             "callee_id": 50, "is_cross_file": 1},
        ]
        file_instance_id, file_version_id = self._setup_db_with_version_and_symbols(db_path, symbols, calls)

        py_result = _py_load_file_result_from_db(
            db_path, file_instance_id, file_version_id,
            "src/main.py", "/app/src/main.py", ""
        )
        rust_result = _rust_load_file_result_from_db(
            db_path, file_instance_id, file_version_id,
            "src/main.py", "/app/src/main.py", ""
        )

        self._assert_results_equal(py_result, rust_result)
        # 验证所有字段都存在且值正确
        assert len(py_result["symbols"]) == 2
        sym_fn_full = next(s for s in py_result["symbols"] if s["qualified_name"] == "fn_full")
        # 验证所有关键字段
        for field, expected in [
            ("name", "fn_full"), ("kind", "function"),
            ("content", "def fn_full(): pass"), ("signature", "fn_full() -> None"),
            ("has_comment", 1), ("doc_comment", "docstring"),
            ("module_path", "mod.full"), ("depth", 2),
            ("start_line", 10), ("end_line", 50), ("is_deleted", 0),
            ("symbol_hash", "sh_full"),
        ]:
            assert sym_fn_full[field] == expected, f"字段 {field}={sym_fn_full[field]!r}, 期望 {expected!r}"
        # 验证 calls 和 issues 字段存在（Python 添加空列表）
        assert "calls" in sym_fn_full
        assert "issues" in sym_fn_full
        # 验证 raw_calls 字段完整性
        assert len(py_result["raw_calls"]) == 1
        call = py_result["raw_calls"][0]
        for field, expected in [
            ("caller_name", "fn_full"), ("callee_name", "ext_fn"),
            ("callee_module", "ext"), ("callee_qualified", "ext.ext_fn"),
            ("callee_file", "ext.py"), ("callee_id", 50),
            ("call_line", 20), ("is_cross_file", 1),
        ]:
            assert call[field] == expected, f"raw_calls.{field}={call[field]!r}, 期望 {expected!r}"
