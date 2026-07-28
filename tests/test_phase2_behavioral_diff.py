"""Phase 2 子任务 1：CAS→CodeGraph Merge PyO3 暴露层差分测试。

**本文件是 manifest §7 中 Phase 2-1 的 ✅(behavioral) 标记载体**。

差分测试矩阵（契约 docs/design/phase2-1-cas-merge-py暴露-contract.md §4）：
  TestCasMergeDiff：M1-M8（cas_merge_to_codegraph 差分）
    - M1: CAS miss（cas_key 不在 cas_file_cache）
    - M2: CAS hit + fresh CodeGraph DB（Rust 自动 init_schema）
    - M3: CAS hit + 已有 CodeGraph DB（核心场景）
    - M4: 重复 merge 同一 cas_key（幂等性）
    - M5a: 单文件 calls（无跨文件，两端一致）
    - M6: workspace 不存在（INSERT OR IGNORE 自动创建）
    - M7: file_size 字段来源一致性
    - M8: ORDER BY 稳定性（单文件场景，两端一致）

  TestCasMergeInitSchemaDiff：S1-S2（schema 初始化差分）
    - S1: fresh DB（无任何表）
    - S2: 已有 schema 的 DB（幂等）

预期差异（见契约 §4.3）：
  - M5b（跨文件 calls 回扫）：Rust 多解析 N 个，Python 不解析。本测试不构造跨文件
    场景，只验证 M5a（单文件，两端一致）。M5b 留待 Phase 2-2 跨文件 resolve 迁移。
  - M8（ORDER BY 稳定性）：差分测试用单文件场景避免触发跨文件同名符号歧义。
  - 返回格式差异：Python 返回 dict 含 `merge_status`，Rust 返回 dict 含 `success`。
    差分断言聚焦业务语义（symbols/calls 数量），不强制 dict 结构完全一致。

前置条件：
  - Rust 扩展 callwarden_core 必须可加载（为 Python 3.14 编译的 .pyd）
  - 如果当前 Python 不是 3.14，本测试套件会显式 skip 并给出修复指引

关联：
  - 契约：docs/design/phase2-1-cas-merge-py暴露-contract.md
  - Python 真相源：db/db_cas_merge.py:merge_cas_to_codegraph
  - Rust 真相源：rust_ext/src/daemon/cas_merge.rs + rust_ext/src/cas_merge_query.rs
"""
from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
from typing import Any, Dict, Optional

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
# CAS DB 与 CodeGraph DB fixture 构造
# ============================================

# CAS DB schema（与 db/db_cas.py:CAS_SCHEMA_DDL 对齐，子集）
_CAS_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS cas_file_cache (
    cas_key TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    language TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    total_lines INTEGER DEFAULT 0,
    parser_version TEXT NOT NULL,
    callwarden_version TEXT NOT NULL,
    extraction_config_version TEXT NOT NULL,
    abi_version TEXT NOT NULL,
    input_abi_version TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ready',
    parsed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cas_symbol_contents (
    content_hash TEXT PRIMARY KEY,
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cas_symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cas_key TEXT NOT NULL,
    local_symbol_id INTEGER NOT NULL,
    symbol_content_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    local_qualified_name TEXT NOT NULL,
    lexical_parent_local_id INTEGER DEFAULT NULL,
    kind TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    start_col INTEGER DEFAULT 0,
    end_col INTEGER DEFAULT 0,
    start_byte INTEGER DEFAULT 0,
    end_byte INTEGER DEFAULT 0,
    visibility TEXT DEFAULT 'private',
    signature TEXT DEFAULT '',
    has_comment INTEGER DEFAULT 0,
    depth INTEGER DEFAULT -1,
    FOREIGN KEY (cas_key) REFERENCES cas_file_cache(cas_key),
    UNIQUE(cas_key, local_symbol_id)
);
CREATE TABLE IF NOT EXISTS cas_raw_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cas_key TEXT NOT NULL,
    caller_local_id INTEGER DEFAULT NULL,
    caller_name TEXT NOT NULL,
    callee_name TEXT NOT NULL,
    call_line INTEGER NOT NULL,
    call_ordinal INTEGER DEFAULT 0,
    FOREIGN KEY (cas_key) REFERENCES cas_file_cache(cas_key),
    UNIQUE(cas_key, caller_local_id, call_line, callee_name, call_ordinal)
);
CREATE INDEX IF NOT EXISTS idx_cas_symbols_cas_key ON cas_symbols(cas_key);
CREATE INDEX IF NOT EXISTS idx_cas_raw_calls_cas_key ON cas_raw_calls(cas_key);
"""

# CodeGraph DB schema（与 db/schema.py:SCHEMA_SQL 对齐，核心表子集）
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
CREATE TABLE IF NOT EXISTS workspace_manifests (
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    cas_key TEXT,
    raw_hash TEXT,
    source_encoding TEXT DEFAULT 'utf-8',
    bom_kind TEXT DEFAULT 'none',
    newline_style TEXT DEFAULT 'lf',
    file_size INTEGER DEFAULT 0,
    mtime_ns INTEGER DEFAULT 0,
    is_dirty INTEGER DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_manifests_hash ON workspace_manifests(content_hash);
CREATE INDEX IF NOT EXISTS idx_manifests_cas ON workspace_manifests(cas_key);
CREATE INDEX IF NOT EXISTS idx_manifests_dirty ON workspace_manifests(workspace_id, is_dirty);
"""


def _make_cas_db(db_path, cas_key="k1", content_hash="ch1", language="python",
                 file_size=100, total_lines=10, state="ready",
                 symbols=None, calls=None):
    """构建测试用 CAS DB（含 cas_file_cache + cas_symbols + cas_raw_calls）

    Args:
        symbols: list of dict，每个 dict 含 local_symbol_id / symbol_content_hash /
                 name / local_qualified_name / kind / start_line / end_line /
                 start_col / end_col / visibility / signature / has_comment / depth
        calls: list of dict，每个 dict 含 caller_local_id / caller_name /
               callee_name / call_line
    """
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_CAS_SCHEMA_DDL)
    conn.execute(
        "INSERT INTO cas_file_cache (cas_key, content_hash, language, file_size, "
        "total_lines, parser_version, callwarden_version, extraction_config_version, "
        "abi_version, input_abi_version, state, parsed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cas_key, content_hash, language, file_size, total_lines,
         "0.1.0", "0.2.0", "v1", "v1", "v1", state, 1000.0),
    )
    # 写入 symbol_contents（如有）
    seen_hashes = set()
    for sym in (symbols or []):
        sch = sym.get("symbol_content_hash", "")
        if sch and sch not in seen_hashes:
            conn.execute(
                "INSERT OR IGNORE INTO cas_symbol_contents (content_hash, content) VALUES (?, ?)",
                (sch, sym.get("content", "")),
            )
            seen_hashes.add(sch)
    # 写入 cas_symbols
    for sym in (symbols or []):
        conn.execute(
            "INSERT INTO cas_symbols (cas_key, local_symbol_id, symbol_content_hash, "
            "name, local_qualified_name, kind, start_line, end_line, start_col, end_col, "
            "visibility, signature, has_comment, depth) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cas_key, sym["local_symbol_id"], sym.get("symbol_content_hash", ""),
             sym["name"], sym.get("local_qualified_name", sym["name"]),
             sym.get("kind", "function"), sym["start_line"], sym["end_line"],
             sym.get("start_col", 0), sym.get("end_col", 0),
             sym.get("visibility", "private"), sym.get("signature", ""),
             sym.get("has_comment", 0), sym.get("depth", -1)),
        )
    # 写入 cas_raw_calls
    for call in (calls or []):
        conn.execute(
            "INSERT INTO cas_raw_calls (cas_key, caller_local_id, caller_name, "
            "callee_name, call_line) VALUES (?, ?, ?, ?, ?)",
            (cas_key, call.get("caller_local_id"), call["caller_name"],
             call["callee_name"], call["call_line"]),
        )
    conn.commit()
    conn.close()


def _make_codegraph_db(db_path):
    """构建测试用 CodeGraph DB（核心表，与 schema.py 对齐）"""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_CODEGRAPH_SCHEMA_DDL)
    conn.commit()
    conn.close()


def _default_symbols():
    """默认测试 symbols：2 个函数 + 1 个调用关系"""
    return [
        {
            "local_symbol_id": 1,
            "symbol_content_hash": "sch_foo",
            "name": "foo",
            "local_qualified_name": "foo",
            "kind": "function",
            "start_line": 1, "end_line": 3,
            "start_col": 0, "end_col": 0,
            "visibility": "public", "signature": "def foo()",
            "has_comment": 0, "depth": -1,
            "content": "def foo():\n    pass\n",
        },
        {
            "local_symbol_id": 2,
            "symbol_content_hash": "sch_bar",
            "name": "bar",
            "local_qualified_name": "bar",
            "kind": "function",
            "start_line": 5, "end_line": 7,
            "start_col": 0, "end_col": 0,
            "visibility": "public", "signature": "def bar()",
            "has_comment": 0, "depth": -1,
            "content": "def bar():\n    foo()\n",
        },
    ]


def _default_calls():
    """默认测试 calls：bar 调用 foo（单文件内）"""
    return [
        {
            "caller_local_id": 2,
            "caller_name": "bar",
            "callee_name": "foo",
            "call_line": 6,
        },
    ]


# ============================================
# Python 路径与 Rust 路径封装
# ============================================

def _py_merge(cas_db_path, codegraph_db_path, cas_key="k1", workspace_id=1,
              rel_path="src/main.py", abs_path="/app/src/main.py",
              content_hash="ch1", language="python",
              workspace_root_path="/app"):
    """Python 路径：调用 db_cas_merge.merge_cas_to_codegraph

    与生产路径一致：打开两个连接，传给 merge_cas_to_codegraph。
    CodeGraph DB 需已初始化 schema（与 daemon_handle_refresh 一致）。
    """
    from callwarden.db.db_cas_merge import merge_cas_to_codegraph
    cas_conn = sqlite3.connect(str(cas_db_path))
    cas_conn.row_factory = sqlite3.Row
    codegraph_conn = sqlite3.connect(str(codegraph_db_path))
    codegraph_conn.row_factory = sqlite3.Row
    try:
        result = merge_cas_to_codegraph(
            cas_conn, codegraph_conn,
            cas_key=cas_key,
            workspace_id=workspace_id,
            rel_path=rel_path,
            abs_path=abs_path,
            content_hash=content_hash,
            language=language,
            workspace_root_path=workspace_root_path,
        )
        return result
    finally:
        cas_conn.close()
        codegraph_conn.close()


def _rust_merge(cas_db_path, codegraph_db_path, cas_key="k1", workspace_id=1,
                rel_path="src/main.py", abs_path="/app/src/main.py",
                content_hash="ch1", language="python",
                workspace_root_path="/app"):
    """Rust 路径：调用 callwarden_core.cas_merge_to_codegraph

    Rust 端内部打开两个连接（cas 只读 + codegraph 读写）。
    """
    return callwarden_core.cas_merge_to_codegraph(
        str(cas_db_path),
        str(codegraph_db_path),
        cas_key,
        workspace_id,
        rel_path,
        abs_path,
        content_hash,
        language,
        workspace_root_path,
    )


def _query_symbol_count(codegraph_db_path, workspace_id=1):
    """查询 CodeGraph DB 中指定 workspace 的 symbols 数量"""
    conn = sqlite3.connect(str(codegraph_db_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM symbols s "
        "JOIN file_instances fi ON s.file_instance_id = fi.id "
        "WHERE fi.workspace_id = ?",
        (workspace_id,),
    ).fetchone()[0]
    conn.close()
    return count


def _query_call_count(codegraph_db_path, workspace_id=1):
    """查询 CodeGraph DB 中指定 workspace 的 calls 数量"""
    conn = sqlite3.connect(str(codegraph_db_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM calls c "
        "JOIN symbols s ON c.caller_id = s.id "
        "JOIN file_instances fi ON s.file_instance_id = fi.id "
        "WHERE fi.workspace_id = ?",
        (workspace_id,),
    ).fetchone()[0]
    conn.close()
    return count


def _query_calls_resolved(codegraph_db_path, workspace_id=1):
    """查询 CodeGraph DB 中已 resolve 的 calls 数量（callee_id != 0）"""
    conn = sqlite3.connect(str(codegraph_db_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM calls c "
        "JOIN symbols s ON c.caller_id = s.id "
        "JOIN file_instances fi ON s.file_instance_id = fi.id "
        "WHERE fi.workspace_id = ? AND c.callee_id > 0",
        (workspace_id,),
    ).fetchone()[0]
    conn.close()
    return count


def _query_workspace_exists(codegraph_db_path, workspace_id):
    """查询 CodeGraph DB 中 workspace 是否存在"""
    conn = sqlite3.connect(str(codegraph_db_path))
    row = conn.execute(
        "SELECT id FROM workspaces WHERE id = ?", (workspace_id,)
    ).fetchone()
    conn.close()
    return row is not None


def _query_manifest(codegraph_db_path, workspace_id, rel_path):
    """查询 workspace_manifests 表"""
    conn = sqlite3.connect(str(codegraph_db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM workspace_manifests WHERE workspace_id = ? AND rel_path = ?",
        (workspace_id, rel_path),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ============================================
# 测试类 1：M1-M8 cas_merge_to_codegraph 差分
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestCasMergeDiff:
    """M1-M8: cas_merge_to_codegraph Python↔Rust 行为差分"""

    def test_m1_cas_miss(self, tmp_path):
        """M1: CAS miss（cas_key 不在 cas_file_cache）

        Python 返回 {"merge_status": "cas_miss", ...}
        Rust 返回 {"success": True, "symbols_inserted": 0, ...}（merge 函数成功执行，但无数据）
        差分断言：两端 symbols/calls 数量均为 0
        """
        cas_db = tmp_path / "cas.db"
        cg_db = tmp_path / "codegraph.db"
        _make_cas_db(cas_db, cas_key="k1")  # 只有 k1
        _make_codegraph_db(cg_db)

        py_result = _py_merge(cas_db, cg_db, cas_key="nonexistent")
        rust_result = _rust_merge(cas_db, cg_db, cas_key="nonexistent")

        # Python: merge_status == "cas_miss"
        assert py_result["merge_status"] == "cas_miss"
        assert py_result["symbols_inserted"] == 0
        assert py_result["calls_inserted"] == 0

        # Rust: success=True（merge 函数成功），但 0 symbols/calls
        assert rust_result["success"] is True
        assert rust_result["symbols_inserted"] == 0
        assert rust_result["calls_inserted"] == 0

        # 差分断言：两端 0 一致
        assert py_result["symbols_inserted"] == rust_result["symbols_inserted"]
        assert py_result["calls_inserted"] == rust_result["calls_inserted"]

    def test_m2_fresh_codegraph_db(self, tmp_path):
        """M2: CAS hit + fresh CodeGraph DB（无表）

        Python：需先 init_schema，否则抛 OperationalError
        Rust：自动调用 init_codegraph_schema 建表后合并
        差分断言：Rust 路径在 fresh DB 上可成功合并
        """
        cas_db = tmp_path / "cas.db"
        cg_db = tmp_path / "codegraph.db"
        _make_cas_db(cas_db, cas_key="k1", content_hash="ch1", language="python",
                     file_size=100, total_lines=10,
                     symbols=_default_symbols(), calls=_default_calls())
        # 不初始化 CodeGraph DB schema（fresh DB）

        # Python 路径：不初始化 schema 会失败
        from callwarden.db.db_cas_merge import merge_cas_to_codegraph
        cas_conn = sqlite3.connect(str(cas_db))
        cas_conn.row_factory = sqlite3.Row
        codegraph_conn = sqlite3.connect(str(cg_db))  # fresh，无表
        codegraph_conn.row_factory = sqlite3.Row
        with pytest.raises(sqlite3.OperationalError):
            merge_cas_to_codegraph(
                cas_conn, codegraph_conn,
                cas_key="k1", workspace_id=1,
                rel_path="src/main.py", abs_path="/app/src/main.py",
                content_hash="ch1", language="python",
                workspace_root_path="/app",
            )
        cas_conn.close()
        codegraph_conn.close()

        # Rust 路径：自动 init_schema 后合并
        rust_result = _rust_merge(cas_db, cg_db, cas_key="k1")
        assert rust_result["success"] is True, f"Rust merge 失败: {rust_result.get('error')}"
        assert rust_result["symbols_inserted"] == 2
        assert rust_result["calls_inserted"] == 1

    def test_m3_cas_hit_existing_db(self, tmp_path):
        """M3: CAS hit + 已有 CodeGraph DB（核心场景）

        两端均成功合并，symbols/calls 数量一致
        """
        cas_db = tmp_path / "cas.db"
        cg_db_py = tmp_path / "codegraph_py.db"
        cg_db_rust = tmp_path / "codegraph_rust.db"
        _make_cas_db(cas_db, cas_key="k1", content_hash="ch1", language="python",
                     file_size=100, total_lines=10,
                     symbols=_default_symbols(), calls=_default_calls())
        _make_codegraph_db(cg_db_py)
        _make_codegraph_db(cg_db_rust)

        py_result = _py_merge(cas_db, cg_db_py, cas_key="k1")
        rust_result = _rust_merge(cas_db, cg_db_rust, cas_key="k1")

        # 两端均成功
        assert py_result["merge_status"] == "merged"
        assert rust_result["success"] is True, f"Rust merge 失败: {rust_result.get('error')}"

        # 差分断言：symbols/calls 数量一致
        assert py_result["symbols_inserted"] == rust_result["symbols_inserted"], \
            f"symbols_inserted: Python={py_result['symbols_inserted']}, Rust={rust_result['symbols_inserted']}"
        assert py_result["calls_inserted"] == rust_result["calls_inserted"], \
            f"calls_inserted: Python={py_result['calls_inserted']}, Rust={rust_result['calls_inserted']}"

        # 验证 DB 中实际数据一致
        assert _query_symbol_count(cg_db_py) == _query_symbol_count(cg_db_rust)
        assert _query_call_count(cg_db_py) == _query_call_count(cg_db_rust)

    def test_m4_idempotent_remerge(self, tmp_path):
        """M4: 重复 merge 同一 cas_key（幂等性）

        第二次 merge 应替换（DELETE+INSERT），symbols/calls 数量不变
        """
        cas_db = tmp_path / "cas.db"
        cg_db_py = tmp_path / "codegraph_py.db"
        cg_db_rust = tmp_path / "codegraph_rust.db"
        _make_cas_db(cas_db, cas_key="k1", content_hash="ch1", language="python",
                     file_size=100, total_lines=10,
                     symbols=_default_symbols(), calls=_default_calls())
        _make_codegraph_db(cg_db_py)
        _make_codegraph_db(cg_db_rust)

        # 第一次 merge
        py1 = _py_merge(cas_db, cg_db_py, cas_key="k1")
        rust1 = _rust_merge(cas_db, cg_db_rust, cas_key="k1")

        # 第二次 merge（幂等性）
        py2 = _py_merge(cas_db, cg_db_py, cas_key="k1")
        rust2 = _rust_merge(cas_db, cg_db_rust, cas_key="k1")

        # 两端第二次数量不变
        assert py2["symbols_inserted"] == py1["symbols_inserted"]
        assert py2["calls_inserted"] == py1["calls_inserted"]
        assert rust2["symbols_inserted"] == rust1["symbols_inserted"]
        assert rust2["calls_inserted"] == rust1["calls_inserted"]

        # 差分断言：两端幂等后数量一致
        assert py2["symbols_inserted"] == rust2["symbols_inserted"]
        assert py2["calls_inserted"] == rust2["calls_inserted"]

        # DB 中实际数据一致（不翻倍）
        assert _query_symbol_count(cg_db_py) == _query_symbol_count(cg_db_rust)
        assert _query_call_count(cg_db_py) == _query_call_count(cg_db_rust)

    def test_m5a_single_file_calls(self, tmp_path):
        """M5a: 单文件内 calls（无跨文件，预期差异）

        bar 调用 foo，foo 在同文件中。

        **预期差异**（契约 §4.3）：
        - Python：INSERT calls 时直接写 `callee_id=0`，不做 resolve
        - Rust：INSERT 时立即 `resolve_callee`，命中本文件 foo，`callee_id != 0`

        差分断言：
        - 两端 calls 总数一致（都是 1）
        - Python resolved=0（不 resolve）
        - Rust resolved=1（本文件 resolve 命中）
        """
        cas_db = tmp_path / "cas.db"
        cg_db_py = tmp_path / "codegraph_py.db"
        cg_db_rust = tmp_path / "codegraph_rust.db"
        _make_cas_db(cas_db, cas_key="k1", content_hash="ch1", language="python",
                     file_size=100, total_lines=10,
                     symbols=_default_symbols(), calls=_default_calls())
        _make_codegraph_db(cg_db_py)
        _make_codegraph_db(cg_db_rust)

        _py_merge(cas_db, cg_db_py, cas_key="k1")
        _rust_merge(cas_db, cg_db_rust, cas_key="k1")

        # 两端 calls 总数一致
        py_calls = _query_call_count(cg_db_py)
        rust_calls = _query_call_count(cg_db_rust)
        assert py_calls == rust_calls, f"calls 总数: Python={py_calls}, Rust={rust_calls}"
        assert py_calls == 1, f"calls 总数应为 1，实际: {py_calls}"

        # 预期差异：Rust resolve 本文件 calls，Python 不 resolve
        py_resolved = _query_calls_resolved(cg_db_py)
        rust_resolved = _query_calls_resolved(cg_db_rust)
        assert py_resolved == 0, f"Python 不 resolve，应=0，实际={py_resolved}"
        assert rust_resolved == 1, f"Rust 应 resolve 1 个本文件 call，实际={rust_resolved}"

    def test_m6_workspace_not_exist(self, tmp_path):
        """M6: workspace 不存在（INSERT OR IGNORE 自动创建）

        两端均应自动创建 workspace 行
        """
        cas_db = tmp_path / "cas.db"
        cg_db_py = tmp_path / "codegraph_py.db"
        cg_db_rust = tmp_path / "codegraph_rust.db"
        _make_cas_db(cas_db, cas_key="k1", content_hash="ch1", language="python",
                     file_size=100, total_lines=10,
                     symbols=_default_symbols(), calls=_default_calls())
        _make_codegraph_db(cg_db_py)
        _make_codegraph_db(cg_db_rust)

        # workspace_id=999 不存在
        _py_merge(cas_db, cg_db_py, cas_key="k1", workspace_id=999)
        _rust_merge(cas_db, cg_db_rust, cas_key="k1", workspace_id=999)

        # 两端均创建 workspace
        assert _query_workspace_exists(cg_db_py, 999)
        assert _query_workspace_exists(cg_db_rust, 999)

    def test_m7_file_size_in_manifest(self, tmp_path):
        """M7: workspace_manifests.file_size 字段来源一致性

        Python：file_size 不写入 manifest（db_cas_merge 不写 workspace_manifests）
        Rust：file_size 来自 cas_file_cache.file_size
        差分策略：验证 Rust 路径的 manifest file_size 与 cas_file_cache.file_size 一致
        """
        cas_db = tmp_path / "cas.db"
        cg_db_rust = tmp_path / "codegraph_rust.db"
        expected_file_size = 256
        _make_cas_db(cas_db, cas_key="k1", content_hash="ch1", language="python",
                     file_size=expected_file_size, total_lines=10,
                     symbols=_default_symbols(), calls=_default_calls())
        _make_codegraph_db(cg_db_rust)

        _rust_merge(cas_db, cg_db_rust, cas_key="k1",
                    rel_path="src/main.py", workspace_root_path="/app")

        manifest = _query_manifest(cg_db_rust, 1, "src/main.py")
        assert manifest is not None, "Rust 路径应写入 workspace_manifests"
        assert manifest["file_size"] == expected_file_size, \
            f"file_size: expected={expected_file_size}, actual={manifest['file_size']}"
        assert manifest["is_dirty"] == 1

    def test_m8_order_by_stability_single_file(self, tmp_path):
        """M8: callee resolve 行为差异（单文件同名符号，预期差异）

        构造两个同名函数 foo，bar 调用 foo。

        **预期差异**（契约 §4.3）：
        - Python：INSERT calls 时直接写 `callee_id=0`，不 resolve
        - Rust：INSERT 时立即 `resolve_callee`，本文件内按 qualified_name/name 匹配
          + ORDER BY s.id ASC LIMIT 1，命中 id 最小的 foo

        差分断言：
        - 两端 symbols/calls 总数一致
        - Python resolved=0（不 resolve）
        - Rust resolved=1（ORDER BY s.id ASC，命中第一个 foo）
        """
        symbols = [
            {
                "local_symbol_id": 1,
                "symbol_content_hash": "sch_foo1",
                "name": "foo",
                "local_qualified_name": "foo",
                "kind": "function",
                "start_line": 1, "end_line": 3,
                "start_col": 0, "end_col": 0,
                "visibility": "public", "signature": "def foo()",
                "has_comment": 0, "depth": -1,
                "content": "def foo():\n    pass\n",
            },
            {
                "local_symbol_id": 2,
                "symbol_content_hash": "sch_foo2",
                "name": "foo",
                "local_qualified_name": "foo",
                "kind": "function",
                "start_line": 5, "end_line": 7,
                "start_col": 0, "end_col": 0,
                "visibility": "public", "signature": "def foo()",
                "has_comment": 0, "depth": -1,
                "content": "def foo():\n    pass\n",
            },
            {
                "local_symbol_id": 3,
                "symbol_content_hash": "sch_bar",
                "name": "bar",
                "local_qualified_name": "bar",
                "kind": "function",
                "start_line": 9, "end_line": 11,
                "start_col": 0, "end_col": 0,
                "visibility": "public", "signature": "def bar()",
                "has_comment": 0, "depth": -1,
                "content": "def bar():\n    foo()\n",
            },
        ]
        calls = [
            {
                "caller_local_id": 3,
                "caller_name": "bar",
                "callee_name": "foo",
                "call_line": 10,
            },
        ]

        cas_db = tmp_path / "cas.db"
        cg_db_py = tmp_path / "codegraph_py.db"
        cg_db_rust = tmp_path / "codegraph_rust.db"
        _make_cas_db(cas_db, cas_key="k1", content_hash="ch1", language="python",
                     file_size=100, total_lines=11,
                     symbols=symbols, calls=calls)
        _make_codegraph_db(cg_db_py)
        _make_codegraph_db(cg_db_rust)

        _py_merge(cas_db, cg_db_py, cas_key="k1")
        _rust_merge(cas_db, cg_db_rust, cas_key="k1")

        # 两端 symbols/calls 总数一致
        assert _query_symbol_count(cg_db_py) == _query_symbol_count(cg_db_rust)
        assert _query_call_count(cg_db_py) == _query_call_count(cg_db_rust)

        # 预期差异：Rust resolve 本文件 calls（ORDER BY s.id ASC 命中第一个 foo），Python 不 resolve
        py_resolved = _query_calls_resolved(cg_db_py)
        rust_resolved = _query_calls_resolved(cg_db_rust)
        assert py_resolved == 0, f"Python 不 resolve，应=0，实际={py_resolved}"
        assert rust_resolved == 1, f"Rust 应 resolve 1 个本文件 call，实际={rust_resolved}"

        # 验证 Rust 端 callee_id 指向 id 最小的 foo（ORDER BY s.id ASC LIMIT 1）
        conn = sqlite3.connect(str(cg_db_rust))
        row = conn.execute(
            "SELECT c.callee_id, s.name, s.start_line FROM calls c "
            "JOIN symbols s ON c.callee_id = s.id "
            "WHERE c.callee_id > 0"
        ).fetchone()
        conn.close()
        assert row is not None, "Rust 应有 resolved call"
        callee_id, callee_name, callee_start_line = row
        assert callee_name == "foo"
        assert callee_start_line == 1, \
            f"ORDER BY s.id ASC 应命中 start_line=1 的 foo，实际 start_line={callee_start_line}"


# ============================================
# 测试类 2：S1-S2 cas_merge_init_schema 差分
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestCasMergeInitSchemaDiff:
    """S1-S2: cas_merge_init_schema Python↔Rust 行为差分"""

    # 核心表清单（init_codegraph_schema 应创建的表）
    _CORE_TABLES = {
        "workspaces", "file_contents", "file_instances",
        "symbols", "calls", "symbol_contents",
    }

    def _get_tables(self, db_path):
        """获取 DB 中所有业务表名"""
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}

    def test_s1_fresh_db(self, tmp_path):
        """S1: fresh DB（无任何表）

        Rust cas_merge_init_schema 应创建核心表（workspaces/file_instances/symbols/calls 等）
        差分断言：6 个核心表全部存在
        """
        db_path = tmp_path / "fresh.db"
        # fresh DB：无任何业务表
        tables_before = self._get_tables(db_path)
        assert not (self._CORE_TABLES & tables_before), \
            f"fresh DB 不应有核心表，实际有: {self._CORE_TABLES & tables_before}"

        # Rust 路径
        result = callwarden_core.cas_merge_init_schema(str(db_path))
        assert result is True, "cas_merge_init_schema 应返回 True"

        # 验证核心表已创建
        tables_after = self._get_tables(db_path)
        created = self._CORE_TABLES & tables_after
        missing = self._CORE_TABLES - tables_after
        assert not missing, f"未创建的核心表: {missing}"
        assert len(created) == 6, f"应创建 6 个核心表，实际: {created}"

    def test_s2_existing_schema_idempotent(self, tmp_path):
        """S2: 已有 schema 的 DB（幂等，不报错）

        对已有 schema 的 DB 再次调用 init_schema，应成功返回 True。
        注意：Rust 的 init_codegraph_schema 会创建 FTS5 虚拟表（symbols_fts 等），
        这是预期行为（与 Python init_schema 一致），差分断言只比较核心业务表。
        """
        db_path = tmp_path / "existing.db"
        # 先创建 schema
        _make_codegraph_db(db_path)
        tables_before = self._get_tables(db_path)
        assert self._CORE_TABLES <= tables_before, "预置 DB 应已含核心表"

        # 再次初始化（幂等）
        result = callwarden_core.cas_merge_init_schema(str(db_path))
        assert result is True, "幂等调用应返回 True"

        # 核心业务表不丢失（幂等）
        tables_after = self._get_tables(db_path)
        assert self._CORE_TABLES <= tables_after, "幂等调用不应丢失核心表"
        # 核心表集合不变化（只比较核心表，FTS5 表新增是预期行为）
        core_before = self._CORE_TABLES & tables_before
        core_after = self._CORE_TABLES & tables_after
        assert core_before == core_after, "核心表集合不应变化"
