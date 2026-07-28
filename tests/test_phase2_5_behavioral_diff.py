"""Phase 2-5 行为差分测试：搜索、callers/callees、call-chain 与拓扑

**本文件是 manifest §7 中 Phase 2-5 的 ✅(behavioral) 标记载体。**

差分测试矩阵（契约 docs/design/phase2-5-search-callers-callees-chain-topo-contract.md §3）：
  TestGetCallersDiff：Q1-Q6（get_callers 差分）
    - Q1: 短名匹配（无 QN）
    - Q2: 显式 QN 精确匹配
    - Q3: 显式 QN 未找到
    - Q4: 自动 QN 识别 + 降级
    - Q5: 无调用者
    - Q6: 多调用者（3+ 个）
  TestGetCalleesDiff：C1-C6（get_callees 差分）
    - C1: 短名匹配（无 QN）
    - C2: 显式 QN 精确匹配
    - C3: 显式 QN 未找到
    - C4: 自动 QN 识别 + 降级
    - C5: 无被调用者
    - C6: 多被调用者（3+ 个）
  TestSearchSymbolsDiff：S1-S5（search_symbols 差分）
    - S1: 精确匹配
    - S2: 部分匹配
    - S3: kind 过滤
    - S4: limit 限制
    - S5: 无匹配
  TestDetectCyclesDiff：D1-D4（detect_cycles 差分）
    - D1: 无环
    - D2: 单环（A→B→A）
    - D3: 多环
    - D4: 自环（A→A）—— 已知两端实现均偏离契约，标记 xfail
  TestGetTopologicalOrderDiff：T1-T3（get_topological_order 差分）
    - T1: 空图
    - T2: 线性链（A→B→C）
    - T3: 菱形（A→B, A→C, B→D, C→D）

差分策略：
  - Python 路径走 db_query.QueryMixin / call_chain.CallChainMixin 真实方法
    （通过 _MinimalDb 模拟，_get_graph_store() 返回 None 强制 SQL 降级）
  - Rust 路径走 callwarden_core.GraphStore pyclass（load_from_sqlite → 查询方法）
  - 两端使用同一 DB 文件初始化，确保数据一致

前置条件：
  - Rust 扩展 callwarden_core 必须可加载（为 Python 3.14 编译的 .pyd）
  - 如果当前 Python 不是 3.14，本测试套件会显式 skip 并给出修复指引

关联：
  - 契约：docs/design/phase2-5-search-callers-callees-chain-topo-contract.md
  - Python 真相源：db/db_query.py（get_callers L273, get_callees L363,
    search_symbols L622, get_topological_order L260）
  - Python 真相源：analyzers/call_chain.py（detect_cycles L382）
  - Rust 真相源：rust_ext/src/graph.rs（GraphStore pyclass）
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

# ============================================
# 前置条件：Rust 扩展可用性检查
# ============================================

# 优先从 rust_ext/target/pyinstall 加载编译产物
_RUST_TARGET = Path(__file__).parent.parent / "rust_ext" / "target" / "pyinstall"
if str(_RUST_TARGET) not in sys.path:
    sys.path.insert(0, str(_RUST_TARGET))

# 同时把仓库根目录加入 sys.path，使 `from callwarden.db.db_query import ...` 可用
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

rust_skip = pytest.mark.skipif(
    not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON
)


# ============================================
# Schema DDL（与 db/schema.py 对齐，核心表子集）
# ============================================

_SCHEMA_DDL = """
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
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id INTEGER NOT NULL,
    caller_name TEXT NOT NULL,
    caller_module TEXT DEFAULT '',
    callee_name TEXT NOT NULL,
    callee_module TEXT DEFAULT '',
    callee_qualified TEXT DEFAULT '',
    callee_file TEXT DEFAULT '',
    callee_id INTEGER DEFAULT 0,
    call_line INTEGER DEFAULT 0,
    is_cross_file INTEGER DEFAULT 0,
    FOREIGN KEY (caller_id) REFERENCES symbols(id)
);
CREATE TABLE IF NOT EXISTS call_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_version_id INTEGER NOT NULL,
    caller_qualified TEXT NOT NULL,
    caller_hash TEXT DEFAULT '',
    callee_name TEXT NOT NULL,
    callee_module TEXT DEFAULT '',
    callee_qualified TEXT DEFAULT '',
    callee_file TEXT DEFAULT '',
    call_line INTEGER DEFAULT 0,
    is_cross_file INTEGER DEFAULT 0,
    FOREIGN KEY (file_version_id) REFERENCES file_versions(id)
);
CREATE INDEX IF NOT EXISTS idx_file_versions_instance ON file_versions(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_file_versions_current ON file_versions(is_current);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
"""


# ============================================
# Python 路径模拟对象（强制 SQL 降级）
# ============================================

# 延迟导入，避免在模块加载期就要求 callwarden 包可用
def _get_query_mixin():
    from callwarden.db.db_query import QueryMixin
    return QueryMixin


def _get_call_chain_mixin():
    from callwarden.analyzers.call_chain import CallChainMixin
    return CallChainMixin


# 通过动态继承让 _MinimalDb 同时获得 QueryMixin 和 CallChainMixin 的全部方法
# （search_symbols 内部会调用 self._search_symbols_like / self._build_fts_query 等
# 辅助方法；直接通过 unbound method 调用会缺失这些方法）
_QueryMixin = _get_query_mixin()
_CallChainMixin = _get_call_chain_mixin()


class _MinimalDb(_QueryMixin, _CallChainMixin):
    """最小 db-like 对象，强制 Python 走 SQL 路径（_get_graph_store 返回 None）

    用于差分测试：Python 真相源通过 unbound method 调用，
    _get_graph_store() 返回 None 触发 SQL 降级，避免 Rust 短路。

    继承 QueryMixin + CallChainMixin 以获得 search_symbols / detect_cycles 等
    方法内部依赖的辅助方法（_search_symbols_like / _build_fts_query 等）。
    """

    def __init__(self, conn, workspace_id: int = 1, head_commit: str = ""):
        self.conn = conn
        self._ws_id = workspace_id
        self._cached_head_commit = head_commit

    def _get_active_workspace_id(self):
        return self._ws_id

    def _get_graph_store(self):
        return None  # 强制 SQL 降级

    def _wait_for_calls_ready(self, timeout: float = 2.0):
        pass


def _rust_get_callers_with_auto_qn(store, callee_name: str,
                                    qualified_name: Optional[str] = None):
    """Rust 路径 get_callers，包含 Python 生产包装器的 auto-QN 预处理逻辑

    生产 Python get_callers 在调用 Rust 前会做 QN 自动识别：
    - 含 "." 或 "::" 的 callee_name（且 qualified_name=None）→ 视为 QN
    - 提取短名用于 CSR 索引查找
    - QN 查不到时降级到纯短名（auto_qn_fallback=True）

    差分测试需在 Rust 侧复刻此预处理，否则输入不一致。
    """
    auto_qn_fallback = False
    if qualified_name is None and ("." in callee_name or "::" in callee_name):
        qualified_name = callee_name
        callee_name = callee_name.rsplit(".", 1)[-1].rsplit("::", 1)[-1]
        auto_qn_fallback = True

    rust_callers = store.get_callers(callee_name, qualified_name)
    if rust_callers is None:
        return []
    materialized = list(rust_callers)
    if qualified_name is not None:
        if materialized:
            return materialized
        # QN 过滤返回空：仅当自动识别 QN 时降级到纯短名
        if auto_qn_fallback:
            rust_callers = store.get_callers(callee_name, None)
            if rust_callers is not None:
                return list(rust_callers)
        return []  # 显式 QN 未找到 → 返回空
    return materialized


def _rust_get_callees_with_auto_qn(store, caller_name: str,
                                    qualified_name: Optional[str] = None):
    """Rust 路径 get_callees，包含 Python 生产包装器的 auto-QN 预处理逻辑"""
    auto_qn_fallback = False
    if qualified_name is None and ("." in caller_name or "::" in caller_name):
        qualified_name = caller_name
        caller_name = caller_name.rsplit(".", 1)[-1].rsplit("::", 1)[-1]
        auto_qn_fallback = True

    rust_callees = store.get_callees(caller_name, qualified_name)
    if rust_callees is None:
        return []
    materialized = list(rust_callees)
    if qualified_name is not None:
        if materialized:
            return materialized
        if auto_qn_fallback:
            rust_callees = store.get_callees(caller_name, None)
            if rust_callees is not None:
                return list(rust_callees)
        return []
    return materialized


# ============================================
# 测试 DB 构建辅助函数
# ============================================

def _open_db(db_path) -> sqlite3.Connection:
    """打开测试 DB 并初始化 schema"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_DDL)
    return conn


def _insert_workspace(conn, ws_id: int = 1, name: str = "test") -> int:
    """插入工作区，返回 ws_id"""
    now = time.time()
    conn.execute(
        "INSERT INTO workspaces (id, name, root_path, created_at, is_active) "
        "VALUES (?, ?, ?, ?, 1)",
        (ws_id, name, f"/test-{ws_id}", now),
    )
    return ws_id


def _insert_file(conn, ws_id: int, rel_path: str, content_hash: str = "ch1",
                 language: str = "python", total_lines: int = 100) -> Tuple[int, int]:
    """插入 file_contents + file_instances + file_versions（is_current=1），
    返回 (file_instance_id, file_version_id)"""
    now = time.time()
    conn.execute(
        "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) "
        "VALUES (?, ?, ?, ?)",
        (content_hash, language, total_lines, now),
    )
    cur = conn.execute(
        "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
        "mtime, total_lines, last_parsed, status, module_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'parsed', '')",
        (ws_id, rel_path, f"/test/{rel_path}", content_hash, now, total_lines, now),
    )
    fi_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO file_versions (file_instance_id, version_num, content_hash, mtime, "
        "total_lines, parsed_at, is_current, is_deleted, commit_hash) "
        "VALUES (?, 1, ?, ?, ?, ?, 1, 0, '')",
        (fi_id, content_hash, now, total_lines, now),
    )
    fv_id = cur.lastrowid
    return fi_id, fv_id


def _insert_symbol(conn, fi_id: int, name: str, kind: str, qualified_name: str,
                   depth: int = 0, start_line: int = 1, end_line: int = 10,
                   symbol_hash: Optional[str] = None) -> int:
    """插入 symbols 行，返回 symbol id

    同时插入 symbol_contents + file_symbol_versions，使 Python search_symbols
    LIKE 降级路径能查到该符号。两端数据保持一致。
    """
    if symbol_hash is None:
        symbol_hash = f"hash_{qualified_name}"

    # symbol_contents（Python search_symbols LIKE 路径需要）
    conn.execute(
        "INSERT OR IGNORE INTO symbol_contents (content_hash, name, kind, content, signature, has_comment) "
        "VALUES (?, ?, ?, '', '', 0)",
        (symbol_hash, name, kind),
    )

    # symbols（Rust GraphStore 加载 + Python get_callers/get_callees/get_topological_order 使用）
    cur = conn.execute(
        "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, qualified_name, depth, "
        "start_line, end_line, module_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')",
        (fi_id, symbol_hash, name, kind, qualified_name, depth, start_line, end_line),
    )
    sym_id = cur.lastrowid

    # file_symbol_versions（Python search_symbols LIKE 路径需要）
    fv_id = conn.execute(
        "SELECT id FROM file_versions WHERE file_instance_id = ? AND is_current = 1",
        (fi_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO file_symbol_versions (file_version_id, symbol_hash, qualified_name, "
        "start_line, end_line, depth, is_deleted) "
        "VALUES (?, ?, ?, ?, ?, ?, 0)",
        (fv_id, symbol_hash, qualified_name, start_line, end_line, depth),
    )
    return sym_id


def _insert_call(conn, caller_id: int, caller_name: str, callee_id: int,
                 callee_name: str, call_line: int = 1,
                 callee_qualified: str = "", caller_qualified: str = "",
                 fv_id: Optional[int] = None) -> None:
    """插入 calls 行（Rust CSR 使用）和 call_versions 行（Python detect_cycles 使用）

    两端数据保持一致。caller_qualified / callee_qualified 用于 call_versions。
    """
    conn.execute(
        "INSERT INTO calls (caller_id, caller_name, callee_name, callee_id, callee_qualified, call_line) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (caller_id, caller_name, callee_name, callee_id, callee_qualified, call_line),
    )
    if fv_id is not None and caller_qualified and callee_qualified:
        conn.execute(
            "INSERT INTO call_versions (file_version_id, caller_qualified, callee_name, callee_qualified, call_line) "
            "VALUES (?, ?, ?, ?, ?)",
            (fv_id, caller_qualified, callee_name, callee_qualified, call_line),
        )


def _load_rust_store(db_path, workspace_id: int = 0):
    """加载 Rust GraphStore，workspace_id=0 不过滤（兼容单 workspace 测试 DB）"""
    store = callwarden_core.GraphStore()
    n_sym, n_edge = store.load_from_sqlite(str(db_path), workspace_id)
    assert store.load_state() == "graph_ready", (
        f"Rust GraphStore 未就绪：state={store.load_state()}，"
        f"sym={n_sym}, edge={n_edge}"
    )
    return store


def _py_callers(conn, callee_name: str, qualified_name: Optional[str] = None,
                ws_id: int = 1) -> List[Dict]:
    """Python 路径：get_callers 走 SQL（_MinimalDb 强制 _get_graph_store 返回 None）"""
    from callwarden.db.db_query import QueryMixin
    return QueryMixin.get_callers(_MinimalDb(conn, ws_id), callee_name, qualified_name)


def _py_callees(conn, caller_name: str, qualified_name: Optional[str] = None,
                ws_id: int = 1) -> List[Dict]:
    """Python 路径：get_callees 走 SQL"""
    from callwarden.db.db_query import QueryMixin
    return QueryMixin.get_callees(_MinimalDb(conn, ws_id), caller_name, qualified_name)


def _py_search_symbols(conn, query: str, kind: Optional[str] = None,
                       limit: int = 50, ws_id: int = 1) -> List[Dict]:
    """Python 路径：search_symbols 走 LIKE 降级（无 FTS5 表 + 无 Rust 短路）

    Python search_symbols 三级路由：
      1. FTS5（无 symbols_fts 表 → 异常 → 降级）
      2. Rust GraphStore（_get_graph_store 返回 None → 跳过）
      3. LIKE %query%（最终命中）
    """
    from callwarden.db.db_query import QueryMixin
    return QueryMixin.search_symbols(_MinimalDb(conn, ws_id), query, kind, limit)


def _py_detect_cycles(conn, ws_id: int = 1) -> List[List[str]]:
    """Python 路径：detect_cycles 走 DFS on call_versions（无 Rust 短路）"""
    from callwarden.analyzers.call_chain import CallChainMixin
    return CallChainMixin.detect_cycles(_MinimalDb(conn, ws_id))


def _py_topological_order(conn, limit: int = 100, ws_id: int = 1) -> List[Dict]:
    """Python 路径：get_topological_order 走 SQL（无 Rust 短路）"""
    from callwarden.db.db_query import QueryMixin
    return QueryMixin.get_topological_order(_MinimalDb(conn, ws_id), limit)


# ============================================
# TestGetCallersDiff：Q1-Q6
# ============================================

@rust_skip
class TestGetCallersDiff:
    """get_callers Python SQL 路径 vs Rust GraphStore CSR 差分测试"""

    @pytest.fixture
    def callers_db(self, tmp_path):
        """构建测试 DB：
        - 4 个 fn 符号：A, B, C, target（qualified_name 带 module 前缀）
        - 1 个 lonely 符号（无调用者）
        - 3 条调用边：A→target, B→target, C→target
        """
        db_path = tmp_path / "callers.db"
        conn = _open_db(db_path)
        _insert_workspace(conn, ws_id=1)
        fi_id, fv_id = _insert_file(conn, ws_id=1, rel_path="src/main.py", content_hash="ch_callers")

        a_id = _insert_symbol(conn, fi_id, "A", "fn", "mod.A", depth=1, start_line=1)
        b_id = _insert_symbol(conn, fi_id, "B", "fn", "mod.B", depth=1, start_line=10)
        c_id = _insert_symbol(conn, fi_id, "C", "fn", "mod.C", depth=1, start_line=20)
        target_id = _insert_symbol(conn, fi_id, "target", "fn", "mod.target", depth=0, start_line=30)
        _insert_symbol(conn, fi_id, "lonely", "fn", "mod.lonely", depth=0, start_line=40)

        _insert_call(conn, a_id, "A", target_id, "target", call_line=5,
                     callee_qualified="mod.target", caller_qualified="mod.A", fv_id=fv_id)
        _insert_call(conn, b_id, "B", target_id, "target", call_line=15,
                     callee_qualified="mod.target", caller_qualified="mod.B", fv_id=fv_id)
        _insert_call(conn, c_id, "C", target_id, "target", call_line=25,
                     callee_qualified="mod.target", caller_qualified="mod.C", fv_id=fv_id)

        conn.commit()
        conn.close()
        return db_path

    def _assert_callers_equal(self, db_path, callee_name, qualified_name=None):
        """差分断言：比较 Python SQL 路径和 Rust CSR 路径的 callers 结果

        比对 (caller_name, call_line) 集合（忽略自增 id 差异）
        """
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            py_results = _py_callers(conn, callee_name, qualified_name)
        finally:
            conn.close()

        store = _load_rust_store(db_path, workspace_id=0)
        # 使用 auto-QN 包装器，复刻生产 Python 在调用 Rust 前的预处理
        rust_results = _rust_get_callers_with_auto_qn(store, callee_name, qualified_name)

        py_set = {(r["caller_name"], r["call_line"]) for r in py_results}
        rust_set = {(r["caller_name"], r["call_line"]) for r in rust_results}
        assert py_set == rust_set, (
            f"get_callers({callee_name!r}, {qualified_name!r}) 差分失败：\n"
            f"  Python: {sorted(py_set)}\n"
            f"  Rust:   {sorted(rust_set)}"
        )
        return py_results, rust_results

    def test_q1_short_name_match(self, callers_db):
        """Q1: 短名匹配（无 QN）——两端 callers 列表一致"""
        py, rust = self._assert_callers_equal(callers_db, "target", None)
        assert len(py) == 3 and len(rust) == 3

    def test_q2_explicit_qn_match(self, callers_db):
        """Q2: 显式 QN 精确匹配——两端 callers 列表一致"""
        py, rust = self._assert_callers_equal(callers_db, "target", "mod.target")
        assert len(py) == 3 and len(rust) == 3

    def test_q3_explicit_qn_not_found(self, callers_db):
        """Q3: 显式 QN 未找到——两端均返回空"""
        py, rust = self._assert_callers_equal(callers_db, "target", "mod.nonexistent")
        assert py == [] and rust == []

    def test_q4_auto_qn_fallback(self, callers_db):
        """Q4: 自动 QN 识别 + 降级

        传入 "missing.target"（含 "."）→ 自动识别为 QN → QN "missing.target" 查不到
        → 降级到短名 "target" → 返回 3 个 callers
        """
        py, rust = self._assert_callers_equal(callers_db, "missing.target", None)
        assert len(py) == 3 and len(rust) == 3, "自动 QN 降级后应返回所有 callers"

    def test_q5_no_callers(self, callers_db):
        """Q5: 无调用者——两端均返回空"""
        py, rust = self._assert_callers_equal(callers_db, "lonely", None)
        assert py == [] and rust == []

    def test_q6_multiple_callers(self, callers_db):
        """Q6: 多调用者（3+ 个）——两端返回所有 callers，count >= 3"""
        py, rust = self._assert_callers_equal(callers_db, "target", None)
        assert len(py) >= 3, f"Python callers 数量 < 3：{len(py)}"
        assert len(rust) >= 3, f"Rust callers 数量 < 3：{len(rust)}"


# ============================================
# TestGetCalleesDiff：C1-C6
# ============================================

@rust_skip
class TestGetCalleesDiff:
    """get_callees Python SQL 路径 vs Rust GraphStore CSR 差分测试"""

    @pytest.fixture
    def callees_db(self, tmp_path):
        """构建测试 DB：
        - 4 个 fn 符号：source, A, B, C
        - 1 个 lonely 符号（无被调用者）
        - 3 条调用边：source→A, source→B, source→C
        """
        db_path = tmp_path / "callees.db"
        conn = _open_db(db_path)
        _insert_workspace(conn, ws_id=1)
        fi_id, fv_id = _insert_file(conn, ws_id=1, rel_path="src/main.py", content_hash="ch_callees")

        source_id = _insert_symbol(conn, fi_id, "source", "fn", "mod.source", depth=1, start_line=1)
        a_id = _insert_symbol(conn, fi_id, "A", "fn", "mod.A", depth=0, start_line=10)
        b_id = _insert_symbol(conn, fi_id, "B", "fn", "mod.B", depth=0, start_line=20)
        c_id = _insert_symbol(conn, fi_id, "C", "fn", "mod.C", depth=0, start_line=30)
        _insert_symbol(conn, fi_id, "lonely", "fn", "mod.lonely", depth=0, start_line=40)

        _insert_call(conn, source_id, "source", a_id, "A", call_line=5,
                     callee_qualified="mod.A", caller_qualified="mod.source", fv_id=fv_id)
        _insert_call(conn, source_id, "source", b_id, "B", call_line=10,
                     callee_qualified="mod.B", caller_qualified="mod.source", fv_id=fv_id)
        _insert_call(conn, source_id, "source", c_id, "C", call_line=15,
                     callee_qualified="mod.C", caller_qualified="mod.source", fv_id=fv_id)

        conn.commit()
        conn.close()
        return db_path

    def _assert_callees_equal(self, db_path, caller_name, qualified_name=None):
        """差分断言：比较 Python SQL 路径和 Rust CSR 路径的 callees 结果

        比对 (callee_name, call_line) 集合（忽略自增 id 差异）
        """
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            py_results = _py_callees(conn, caller_name, qualified_name)
        finally:
            conn.close()

        store = _load_rust_store(db_path, workspace_id=0)
        # 使用 auto-QN 包装器，复刻生产 Python 在调用 Rust 前的预处理
        rust_list = _rust_get_callees_with_auto_qn(store, caller_name, qualified_name)

        py_set = {(r["callee_name"], r["call_line"]) for r in py_results}
        rust_set = {(r["callee_name"], r["call_line"]) for r in rust_list}
        assert py_set == rust_set, (
            f"get_callees({caller_name!r}, {qualified_name!r}) 差分失败：\n"
            f"  Python: {sorted(py_set)}\n"
            f"  Rust:   {sorted(rust_set)}"
        )
        return py_results, rust_list

    def test_c1_short_name_match(self, callees_db):
        """C1: 短名匹配（无 QN）——两端 callees 列表一致"""
        py, rust = self._assert_callees_equal(callees_db, "source", None)
        assert len(py) == 3 and len(rust) == 3

    def test_c2_explicit_qn_match(self, callees_db):
        """C2: 显式 QN 精确匹配——两端 callees 列表一致"""
        py, rust = self._assert_callees_equal(callees_db, "source", "mod.source")
        assert len(py) == 3 and len(rust) == 3

    def test_c3_explicit_qn_not_found(self, callees_db):
        """C3: 显式 QN 未找到——两端均返回空"""
        py, rust = self._assert_callees_equal(callees_db, "source", "mod.nonexistent")
        assert py == [] and rust == []

    def test_c4_auto_qn_fallback(self, callees_db):
        """C4: 自动 QN 识别 + 降级

        传入 "missing.source"（含 "."）→ 自动识别为 QN → QN 查不到
        → 降级到短名 "source" → 返回 3 个 callees
        """
        py, rust = self._assert_callees_equal(callees_db, "missing.source", None)
        assert len(py) == 3 and len(rust) == 3, "自动 QN 降级后应返回所有 callees"

    def test_c5_no_callees(self, callees_db):
        """C5: 无被调用者——两端均返回空"""
        py, rust = self._assert_callees_equal(callees_db, "lonely", None)
        assert py == [] and rust == []

    def test_c6_multiple_callees(self, callees_db):
        """C6: 多被调用者（3+ 个）——两端返回所有 callees，count >= 3"""
        py, rust = self._assert_callees_equal(callees_db, "source", None)
        assert len(py) >= 3, f"Python callees 数量 < 3：{len(py)}"
        assert len(rust) >= 3, f"Rust callees 数量 < 3：{len(rust)}"


# ============================================
# TestSearchSymbolsDiff：S1-S5
# ============================================

@rust_skip
class TestSearchSymbolsDiff:
    """search_symbols Python LIKE 路径 vs Rust GraphStore memchr 差分测试

    注意（契约 §6.3）：FTS5 trigram 与 memchr 子串匹配语义可能不同。
    本测试通过 _MinimalDb（_get_graph_store 返回 None）强制 Python 走 LIKE 降级路径，
    LIKE %query% 与 memchr 子串匹配语义一致（均大小写不敏感子串），
    使用 >= 3 字符查询避免 FTS5 trigram 限制。
    """

    @pytest.fixture
    def search_db(self, tmp_path):
        """构建测试 DB：
        - 4 个 fn 符号：process_order, process_payment, validate_input, handle_request
        - 1 个 class 符号：OrderProcessor
        """
        db_path = tmp_path / "search.db"
        conn = _open_db(db_path)
        _insert_workspace(conn, ws_id=1)
        fi_id, fv_id = _insert_file(conn, ws_id=1, rel_path="src/main.py", content_hash="ch_search")

        _insert_symbol(conn, fi_id, "process_order", "fn", "mod.process_order",
                       depth=0, start_line=1)
        _insert_symbol(conn, fi_id, "process_payment", "fn", "mod.process_payment",
                       depth=0, start_line=10)
        _insert_symbol(conn, fi_id, "validate_input", "fn", "mod.validate_input",
                       depth=0, start_line=20)
        _insert_symbol(conn, fi_id, "handle_request", "fn", "mod.handle_request",
                       depth=0, start_line=30)
        _insert_symbol(conn, fi_id, "OrderProcessor", "class", "mod.OrderProcessor",
                       depth=0, start_line=40)

        conn.commit()
        conn.close()
        return db_path

    def _assert_search_equal(self, db_path, query, kind=None, limit=50):
        """差分断言：比较 Python LIKE 路径和 Rust memchr 路径的搜索结果

        比对 name 集合（两端返回字段不同，但 name 是公共字段）
        """
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            py_results = _py_search_symbols(conn, query, kind, limit)
        finally:
            conn.close()

        store = _load_rust_store(db_path, workspace_id=0)
        rust_batch = store.search_symbols(query, kind, limit)
        assert rust_batch is not None, "Rust search_symbols 返回 None"
        rust_results = list(rust_batch)

        py_names = {r["name"] for r in py_results}
        rust_names = {r["name"] for r in rust_results}
        assert py_names == rust_names, (
            f"search_symbols({query!r}, kind={kind!r}, limit={limit!r}) 差分失败：\n"
            f"  Python: {sorted(py_names)}\n"
            f"  Rust:   {sorted(rust_names)}"
        )
        return py_results, rust_results

    def test_s1_exact_match(self, search_db):
        """S1: 精确匹配——两端结果一致"""
        py, rust = self._assert_search_equal(search_db, "process_order")
        assert "process_order" in {r["name"] for r in py}

    def test_s2_partial_match(self, search_db):
        """S2: 部分匹配（"process" 匹配 process_order + process_payment）"""
        py, rust = self._assert_search_equal(search_db, "process")
        py_names = {r["name"] for r in py}
        assert "process_order" in py_names
        assert "process_payment" in py_names

    def test_s3_kind_filter(self, search_db):
        """S3: kind 过滤——只返回 fn，排除 class"""
        py, rust = self._assert_search_equal(search_db, "process", kind="fn")
        py_names = {r["name"] for r in py}
        # process_order 和 process_payment 都是 fn，应被返回
        assert "process_order" in py_names
        assert "process_payment" in py_names
        # OrderProcessor 是 class，应被排除
        assert "OrderProcessor" not in py_names

    def test_s4_limit(self, search_db):
        """S4: limit 限制——两端结果数一致（<= limit）

        契约 §3.3 S4 仅要求"两端结果数一致"，不要求 name 一致：
        Python LIKE 走 ORDER BY sc.kind, fsv.depth DESC 排序，
        Rust memchr 走 search_pool 顺序扫描，两端排序语义不同，
        limit 截断后可能返回不同符号（但数量一致）。
        """
        conn = sqlite3.connect(str(search_db))
        conn.row_factory = sqlite3.Row
        try:
            py_results = _py_search_symbols(conn, "process", None, 1)
        finally:
            conn.close()

        store = _load_rust_store(search_db, workspace_id=0)
        rust_batch = store.search_symbols("process", None, 1)
        rust_results = list(rust_batch)

        assert len(py_results) <= 1, f"Python 结果数 > limit：{len(py_results)}"
        assert len(rust_results) <= 1, f"Rust 结果数 > limit：{len(rust_results)}"
        assert len(py_results) == len(rust_results), (
            f"limit 下两端结果数不一致：Python={len(py_results)}, Rust={len(rust_results)}"
        )
        # 同时验证不限 limit 时两端 name 集合一致（排除 limit 因排序差异导致的假阴性）
        conn = sqlite3.connect(str(search_db))
        conn.row_factory = sqlite3.Row
        try:
            py_all = _py_search_symbols(conn, "process", None, 50)
        finally:
            conn.close()
        rust_all = list(store.search_symbols("process", None, 50))
        assert {r["name"] for r in py_all} == {r["name"] for r in rust_all}, (
            "不限 limit 时两端 name 集合应一致"
        )

    def test_s5_no_match(self, search_db):
        """S5: 无匹配——两端均返回空"""
        py, rust = self._assert_search_equal(search_db, "nonexistent_xyz")
        assert py == [] and rust == []


# ============================================
# TestDetectCyclesDiff：D1-D4
# ============================================

@rust_skip
class TestDetectCyclesDiff:
    """detect_cycles Python DFS（call_versions 表）vs Rust CSR 三色 DFS 差分测试

    差分断言用 {frozenset(cycle)} 集合比较，因为：
    - Python 环格式：[A, B, A]（首尾重复）
    - Rust 环格式：[A, B]（首尾不重复）
    - frozenset 标准化后两者相等

    已知差异（D4）：自环 A→A
    - Python：返回 [[A, A]]（path_stack 逻辑记录自环）
    - Rust：返回 []（cycle.len() > 1 过滤掉单节点自环）
    - 契约期望 [[A]]，两端实现均偏离契约 → D4 标记 xfail
    """

    def _build_cycle_db(self, tmp_path, edges: List[Tuple[str, str]],
                        db_name: str = "cycles.db") -> Path:
        """构建测试 DB：
        - 每条 edge (caller_qname, callee_qname) 创建对应的符号和调用关系
        - 同时填充 calls 表（Rust CSR 用）和 call_versions 表（Python DFS 用）
        """
        db_path = tmp_path / db_name
        conn = _open_db(db_path)
        _insert_workspace(conn, ws_id=1)
        fi_id, fv_id = _insert_file(conn, ws_id=1, rel_path="src/main.py",
                                    content_hash=f"ch_{db_name}")

        # 收集所有唯一节点名
        nodes = set()
        for caller, callee in edges:
            nodes.add(caller)
            nodes.add(callee)

        # 插入符号（QN 同时作为 name 和 qualified_name 的短名部分）
        qname_to_id: Dict[str, int] = {}
        for i, qname in enumerate(sorted(nodes)):
            short_name = qname.rsplit(".", 1)[-1]
            sym_id = _insert_symbol(
                conn, fi_id, short_name, "fn", qname,
                depth=0, start_line=i * 10 + 1,
                symbol_hash=f"hash_{qname}",
            )
            qname_to_id[qname] = sym_id

        # 插入调用边（calls + call_versions）
        for caller_qname, callee_qname in edges:
            caller_id = qname_to_id[caller_qname]
            callee_id = qname_to_id[callee_qname]
            callee_short = callee_qname.rsplit(".", 1)[-1]
            _insert_call(
                conn, caller_id, caller_qname.rsplit(".", 1)[-1],
                callee_id, callee_short,
                call_line=1,
                callee_qualified=callee_qname,
                caller_qualified=caller_qname,
                fv_id=fv_id,
            )

        conn.commit()
        conn.close()
        return db_path

    def _assert_cycles_equal(self, db_path):
        """差分断言：比较 Python 和 Rust 的环检测结果

        用 {frozenset(cycle) for cycle in cycles} 比较，环内顺序无关
        """
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            py_cycles = _py_detect_cycles(conn)
        finally:
            conn.close()

        store = _load_rust_store(db_path, workspace_id=0)
        rust_cycles = store.detect_cycles()

        py_set = {frozenset(c) for c in py_cycles}
        rust_set = {frozenset(c) for c in rust_cycles}
        assert py_set == rust_set, (
            f"detect_cycles 差分失败：\n"
            f"  Python cycles: {py_cycles}\n"
            f"  Rust cycles:   {rust_cycles}\n"
            f"  Python frozensets: {py_set}\n"
            f"  Rust frozensets:   {rust_set}"
        )
        return py_cycles, rust_cycles

    def test_d1_no_cycle(self, tmp_path):
        """D1: 无环（A→B, B→C，DAG）——两端均返回空"""
        db_path = self._build_cycle_db(
            tmp_path,
            [("mod.A", "mod.B"), ("mod.B", "mod.C")],
            db_name="d1_no_cycle.db",
        )
        py, rust = self._assert_cycles_equal(db_path)
        assert py == [] and rust == []

    def test_d2_single_cycle(self, tmp_path):
        """D2: 单环（A→B→A）——两端环列表一致（frozenset 比较）"""
        db_path = self._build_cycle_db(
            tmp_path,
            [("mod.A", "mod.B"), ("mod.B", "mod.A")],
            db_name="d2_single_cycle.db",
        )
        py, rust = self._assert_cycles_equal(db_path)
        assert len(py) >= 1, f"Python 未检测到环：{py}"
        assert len(rust) >= 1, f"Rust 未检测到环：{rust}"

    def test_d3_multiple_cycles(self, tmp_path):
        """D3: 多环（A→B→A, C→D→C，两个独立环）——两端环列表一致"""
        db_path = self._build_cycle_db(
            tmp_path,
            [
                ("mod.A", "mod.B"), ("mod.B", "mod.A"),
                ("mod.C", "mod.D"), ("mod.D", "mod.C"),
            ],
            db_name="d3_multiple_cycles.db",
        )
        py, rust = self._assert_cycles_equal(db_path)
        assert len(py) >= 2, f"Python 检测到的环数 < 2：{py}"
        assert len(rust) >= 2, f"Rust 检测到的环数 < 2：{rust}"

    @pytest.mark.xfail(
        reason=(
            "D4 自环（A→A）已知两端实现均偏离契约："
            "Python detect_cycles 返回 [[A, A]]（path_stack 逻辑记录自环），"
            "Rust detect_cycles 返回 []（cycle.len() > 1 过滤单节点自环），"
            "契约期望 [[A]]。两端均需修复才能通过差分。"
        ),
        strict=True,
    )
    def test_d4_self_loop(self, tmp_path):
        """D4: 自环（A→A）——两端环列表一致（xfail：已知两端实现偏离契约）"""
        db_path = self._build_cycle_db(
            tmp_path,
            [("mod.A", "mod.A")],
            db_name="d4_self_loop.db",
        )
        self._assert_cycles_equal(db_path)


# ============================================
# TestGetTopologicalOrderDiff：T1-T3
# ============================================

@rust_skip
class TestGetTopologicalOrderDiff:
    """get_topological_order Python SQL（ORDER BY depth）vs Rust Kahn 差分测试

    差分断言用 name 集合比较，因为（契约 §6.4）：
    - Python: ORDER BY depth ASC, start_line ASC
    - Rust: Kahn 算法（入度 0 入队 → BFS）
    - 同 depth 节点的顺序不保证一致
    """

    @pytest.fixture
    def empty_db(self, tmp_path):
        """空图：无 fn 符号"""
        db_path = tmp_path / "topo_empty.db"
        conn = _open_db(db_path)
        _insert_workspace(conn, ws_id=1)
        fi_id, _ = _insert_file(conn, ws_id=1, rel_path="src/main.py",
                                content_hash="ch_topo_empty")
        conn.commit()
        conn.close()
        return db_path

    @pytest.fixture
    def linear_db(self, tmp_path):
        """线性链：A→B→C（depth: A=2, B=1, C=0）"""
        db_path = tmp_path / "topo_linear.db"
        conn = _open_db(db_path)
        _insert_workspace(conn, ws_id=1)
        fi_id, fv_id = _insert_file(conn, ws_id=1, rel_path="src/main.py",
                                    content_hash="ch_topo_linear")

        a_id = _insert_symbol(conn, fi_id, "A", "fn", "mod.A", depth=2, start_line=1)
        b_id = _insert_symbol(conn, fi_id, "B", "fn", "mod.B", depth=1, start_line=10)
        c_id = _insert_symbol(conn, fi_id, "C", "fn", "mod.C", depth=0, start_line=20)

        _insert_call(conn, a_id, "A", b_id, "B", call_line=5,
                     callee_qualified="mod.B", caller_qualified="mod.A", fv_id=fv_id)
        _insert_call(conn, b_id, "B", c_id, "C", call_line=15,
                     callee_qualified="mod.C", caller_qualified="mod.B", fv_id=fv_id)

        conn.commit()
        conn.close()
        return db_path

    @pytest.fixture
    def diamond_db(self, tmp_path):
        """菱形：A→B, A→C, B→D, C→D（depth: A=0, B=1, C=1, D=2）"""
        db_path = tmp_path / "topo_diamond.db"
        conn = _open_db(db_path)
        _insert_workspace(conn, ws_id=1)
        fi_id, fv_id = _insert_file(conn, ws_id=1, rel_path="src/main.py",
                                    content_hash="ch_topo_diamond")

        a_id = _insert_symbol(conn, fi_id, "A", "fn", "mod.A", depth=0, start_line=1)
        b_id = _insert_symbol(conn, fi_id, "B", "fn", "mod.B", depth=1, start_line=10)
        c_id = _insert_symbol(conn, fi_id, "C", "fn", "mod.C", depth=1, start_line=20)
        d_id = _insert_symbol(conn, fi_id, "D", "fn", "mod.D", depth=2, start_line=30)

        _insert_call(conn, a_id, "A", b_id, "B", call_line=5,
                     callee_qualified="mod.B", caller_qualified="mod.A", fv_id=fv_id)
        _insert_call(conn, a_id, "A", c_id, "C", call_line=10,
                     callee_qualified="mod.C", caller_qualified="mod.A", fv_id=fv_id)
        _insert_call(conn, b_id, "B", d_id, "D", call_line=15,
                     callee_qualified="mod.D", caller_qualified="mod.B", fv_id=fv_id)
        _insert_call(conn, c_id, "C", d_id, "D", call_line=25,
                     callee_qualified="mod.D", caller_qualified="mod.C", fv_id=fv_id)

        conn.commit()
        conn.close()
        return db_path

    def _py_topo_names(self, db_path) -> set:
        """Python 路径：返回 qualified_name 集合"""
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            py_results = _py_topological_order(conn, limit=100)
        finally:
            conn.close()
        return {r["qualified_name"] for r in py_results}

    def _rust_topo_names(self, db_path) -> set:
        """Rust 路径：返回 qualified_name 集合"""
        store = _load_rust_store(db_path, workspace_id=0)
        rust_results = store.get_topological_order()
        return set(rust_results)

    def test_t1_empty_graph(self, empty_db):
        """T1: 空图——两端均返回空"""
        py_names = self._py_topo_names(empty_db)
        rust_names = self._rust_topo_names(empty_db)
        assert py_names == set(), f"Python 空图应返回空：{py_names}"
        assert rust_names == set(), f"Rust 空图应返回空：{rust_names}"

    def test_t2_linear_chain(self, linear_db):
        """T2: 线性链（A→B→C）——两端 name 集合一致（顺序可能不同）"""
        py_names = self._py_topo_names(linear_db)
        rust_names = self._rust_topo_names(linear_db)
        expected = {"mod.A", "mod.B", "mod.C"}
        assert py_names == expected, f"Python 线性链结果异常：{py_names}"
        assert rust_names == expected, f"Rust 线性链结果异常：{rust_names}"
        assert py_names == rust_names, (
            f"线性链两端 name 集合不一致：\n"
            f"  Python: {sorted(py_names)}\n"
            f"  Rust:   {sorted(rust_names)}"
        )

    def test_t3_diamond(self, diamond_db):
        """T3: 菱形（A→B, A→C, B→D, C→D）——两端 name 集合一致（顺序可能不同）

        契约 §6.4：Kahn 对同 depth 节点顺序不保证，差分断言用 name 集合
        """
        py_names = self._py_topo_names(diamond_db)
        rust_names = self._rust_topo_names(diamond_db)
        expected = {"mod.A", "mod.B", "mod.C", "mod.D"}
        assert py_names == expected, f"Python 菱形结果异常：{py_names}"
        assert rust_names == expected, f"Rust 菱形结果异常：{rust_names}"
        assert py_names == rust_names, (
            f"菱形两端 name 集合不一致：\n"
            f"  Python: {sorted(py_names)}\n"
            f"  Rust:   {sorted(rust_names)}"
        )
