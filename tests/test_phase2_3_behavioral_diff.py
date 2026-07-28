"""Phase 2 子任务 3：调用边解析、resolve 与批量写入 PyO3 暴露层差分测试。

**本文件是 manifest §7 中 Phase 2-3 的 ✅(behavioral) 标记载体。**

差分测试矩阵（契约 docs/design/phase2-3-batch-save-calls-contract.md §3）：
  TestBatchResolveAndSaveCallsDiff：C1-C14（batch_resolve_and_save_calls 差分）
    - C1: 单文件 + 1 个 call（策略 1 精确匹配 `module.name`）
    - C2: 单文件 + 1 个 call（策略 3 简名唯一匹配，无 callee_module）
    - C3: 单文件 + 1 个 call（策略 4 同文件简名匹配）
    - C4: 单文件 + 1 个 call（策略 5 external_symbols 唯一匹配）
    - C5: 单文件 + 1 个 call（import 映射，策略 2）
    - C6: 单文件 + 1 个 call（无 callee_name，空 call）
    - C7: 单文件 + 多个 call 混合策略
    - C8: caller_id=0 fallback 全失败 → 跳过 calls 表插入（仍写 call_versions）
    - C9: call_versions 写入（caller_qualified + caller_hash）
    - C10: caller_qualified 为空时推导 `{module_path}::{caller_name}`
    - C11: DELETE 旧 calls（changed_file_instance_ids 非空）
    - C12: 多文件批量（2 个文件，每个文件 2 个 call）
    - C13: HCL 多段 name（策略 4.5，callee_name="aws_security_group.this"）
    - C14: 空 file_results（无文件需处理）

预期差异（见契约 §4）：
  - executemany vs 循环 execute（行为等价，差分测试只断言表内容）
  - 事务边界：Python 在外层 _build_multi_lang 单一大事务中，Rust 独立子事务
    （差分测试在两端都包裹 BEGIN/COMMIT 模拟外层事务，行为一致）
  - file_results._from_db 短路不在范围（调用方控制，差分测试不传入 _from_db=True）

前置条件：
  - Rust 扩展 callwarden_core 必须可加载（为 Python 3.14 编译的 .pyd）
  - 如果当前 Python 不是 3.14，本测试套件会显式 skip 并给出修复指引

关联：
  - 契约：docs/design/phase2-3-batch-save-calls-contract.md
  - Python 真相源：db/db_build.py:_build_call_graph_multi_lang (L2030-2445)
  - Rust 真相源：rust_ext/src/batch_calls_query.rs::batch_resolve_and_save_calls
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
    if not hasattr(callwarden_core, "batch_resolve_and_save_calls"):
        _RUST_EXT_SKIP_REASON = (
            "callwarden_core 已加载但未暴露 batch_resolve_and_save_calls。"
            "请重新构建 Rust 扩展并替换项目根目录的 callwarden_core.pyd。"
        )
    else:
        _RUST_EXT_AVAILABLE = True
except ImportError as _e:
    _RUST_EXT_SKIP_REASON = (
        f"callwarden_core 不可加载：{_e}。"
        "本测试需要 Python 3.14 编译的 Rust 扩展。"
        "在 Windows 上若当前 Python 不是 3.14，请用 C:\\Python314\\python.exe 运行。"
    )


# ============================================
# CodeGraph DB schema（与 Phase 2-2 一致，附加 external_symbols 表）
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
CREATE TABLE IF NOT EXISTS external_symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    package_name TEXT DEFAULT '',
    package_version TEXT DEFAULT '',
    signature TEXT DEFAULT '',
    docstring TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_symbols_file_instance ON symbols(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);
CREATE INDEX IF NOT EXISTS idx_fsv_file_version ON file_symbol_versions(file_version_id);
CREATE INDEX IF NOT EXISTS idx_call_versions_fv ON call_versions(file_version_id);
"""


def _make_codegraph_db(db_path):
    """构建测试用 CodeGraph DB（核心表，与 schema.py 对齐）"""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_CODEGRAPH_SCHEMA_DDL)
    conn.commit()
    conn.close()


def _compute_content_hash(content: str) -> str:
    """与 Python config.compute_content_hash 一致（norm_newlines + SHA-256）"""
    import hashlib
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _prep_file_instance(db_path, workspace_id=1, rel_path="src/main.py",
                       abs_path="/app/src/main.py", content_hash="ch_file1",
                       language="python", total_lines=10, module_path="src.main"):
    """预填 workspace + file_contents + file_instances + file_versions

    与 Phase 2-2 相比增加 module_path 参数（用于 caller_qualified 推导）。
    返回 (file_instance_id, file_version_id)
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
        "VALUES (?, ?, ?, ?)",
        (content_hash, language, total_lines, now),
    )
    cur = conn.execute(
        "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
        "mtime, total_lines, last_parsed, status, module_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'parsed', ?)",
        (workspace_id, rel_path, abs_path, content_hash, now, total_lines, now, module_path),
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


def _insert_symbol(db_path, file_instance_id, name, qualified_name, kind="function",
                   start_line=1, content_hash=None, module_path=""):
    """插入单个 symbol 到 symbols 表，返回 symbol_id"""
    if content_hash is None:
        content_hash = _compute_content_hash(f"def {name}(): pass")
    end_line = start_line + 2
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, visibility, "
        "start_line, end_line, start_col, end_col, signature, has_comment, "
        "comment_status, module_path, qualified_name) "
        "VALUES (?, ?, ?, 'function', 'public', ?, ?, 0, 0, '', 0, 'pending', ?, ?)",
        (file_instance_id, content_hash, name, start_line, end_line,
         module_path, qualified_name),
    )
    symbol_id = cur.lastrowid
    conn.commit()
    conn.close()
    return symbol_id


def _insert_external_symbol(db_path, symbol_name, qualified_name, package_name):
    """插入单个外部符号，返回 id"""
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO external_symbols (symbol_name, qualified_name, package_name) "
        "VALUES (?, ?, ?)",
        (symbol_name, qualified_name, package_name),
    )
    ext_id = cur.lastrowid
    conn.commit()
    conn.close()
    return ext_id


def _insert_old_call(db_path, caller_symbol_id, caller_name="old_fn",
                     callee_name="old_callee", call_line=999):
    """预填旧 call 记录（用于 C11 DELETE 测试）"""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO calls (caller_id, caller_name, caller_module, callee_name, "
        "callee_module, callee_qualified, callee_file, callee_id, call_line, is_cross_file) "
        "VALUES (?, ?, '', ?, '', '', '', 0, ?, 0)",
        (caller_symbol_id, caller_name, callee_name, call_line),
    )
    conn.commit()
    conn.close()


# ============================================
# Python 路径与 Rust 路径封装
# ============================================

def _py_resolve_and_save_calls(codegraph_db_path, file_results, all_symbols=None,
                                external_symbols=None):
    """Python 路径：调用 db_build.BuildMixin._build_call_graph_multi_lang（unbound method）

    与 Phase 2-2 _py_save_symbols 一致：使用 unbound method + 最小 db-like 对象。
    预填 all_symbols/external_symbols 时构造 file_results 使其 _from_db=False。

    关键：传 only_files=set(file_results.keys())，让 Python 从 DB 全量读取符号索引
    （与 Rust 端从 all_symbols 参数构建索引的行为一致）。若不传 only_files，
    Python 会从 file_results[].symbols 构建索引（测试中为空，导致 resolve 全失败）。
    """
    from callwarden.db.db_build import BuildMixin

    class _MinimalDb:
        def __init__(self, conn):
            self.conn = conn
            # _build_call_graph_multi_lang 调用 _get_active_workspace_id()
            # 在 _MinimalDb 中提供该方法避免 AttributeError
            self._active_workspace_id = 1

        def _get_active_workspace_id(self):
            return self._active_workspace_id

        def _make_call_entry(self, raw, callee_qname, callee_file, callee_id, is_cross):
            """与 BuildMixin._make_call_entry 行为一致：构造调用关系记录"""
            return {
                "caller_name": raw.get("caller_name", ""),
                "caller_qualified": raw.get("caller_qualified", ""),
                "caller_module": raw.get("caller_module", ""),
                "callee_name": raw.get("callee_name", ""),
                "callee_module": raw.get("callee_module", ""),
                "callee_qualified": callee_qname,
                "callee_file": callee_file,
                "callee_id": callee_id,
                "call_line": raw.get("call_line", 0),
                "is_cross_file": is_cross,
            }

    conn = sqlite3.connect(str(codegraph_db_path))
    conn.row_factory = sqlite3.Row
    db = _MinimalDb(conn)
    # only_files 非空时，Python 从 DB 全量加载符号索引（与 Rust 端 all_symbols 一致）
    only_files = set(file_results.keys()) if file_results else None
    try:
        conn.execute("BEGIN IMMEDIATE;")
        BuildMixin._build_call_graph_multi_lang(db, file_results, only_files=only_files)
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()


def _rust_resolve_and_save_calls(codegraph_db_path, workspace_id, file_results,
                                  all_symbols, external_symbols,
                                  changed_file_instance_ids):
    """Rust 路径：调用 callwarden_core.batch_resolve_and_save_calls

    Rust 端内部打开读写连接 + BEGIN IMMEDIATE → 全部 SQL → COMMIT。
    """
    return callwarden_core.batch_resolve_and_save_calls(
        str(codegraph_db_path),
        workspace_id,
        file_results,
        all_symbols,
        external_symbols,
        changed_file_instance_ids,
    )


# ============================================
# 查询辅助函数
# ============================================

def _query_calls(codegraph_db_path):
    """查询 calls 表（按 caller_name, call_line 排序，返回 list of dict）"""
    conn = sqlite3.connect(str(codegraph_db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT caller_id, caller_name, caller_module, callee_name, "
        "callee_module, callee_qualified, callee_file, callee_id, call_line, "
        "is_cross_file FROM calls ORDER BY caller_name, call_line, callee_name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _query_call_versions(codegraph_db_path):
    """查询 call_versions 表（按 caller_qualified, call_line 排序）"""
    conn = sqlite3.connect(str(codegraph_db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT file_version_id, caller_qualified, caller_hash, callee_name, "
        "callee_module, callee_qualified, callee_file, call_line, is_cross_file "
        "FROM call_versions ORDER BY caller_qualified, call_line, callee_name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _load_all_symbols_from_db(codegraph_db_path):
    """从 DB 加载所有 symbols（用于传入 Rust 端的 all_symbols 参数）"""
    conn = sqlite3.connect(str(codegraph_db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT s.id, s.name, s.qualified_name, s.kind, s.file_instance_id, "
        "fi.rel_path FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id "
        "WHERE s.qualified_name != ''"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _load_external_symbols_from_db(codegraph_db_path):
    """从 DB 加载所有 external_symbols"""
    conn = sqlite3.connect(str(codegraph_db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, symbol_name, qualified_name, package_name FROM external_symbols"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _normalize_call_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """规范化 call 行用于差分比较：caller_id 替换为 caller_name（跨端 id 不同）"""
    r = dict(row)
    # caller_id 在两端不同（自增主键），用 caller_name 作为关联键
    # 但 caller_id 已包含在 ORDER BY 中，不影响排序
    return r


def _assert_calls_equal(py_calls, rust_calls, ignore_caller_id=True):
    """差分断言：两端 calls 表内容一致

    忽略 caller_id（两端 symbol id 不同，由自增主键决定），
    其他字段必须完全一致。
    """
    assert len(py_calls) == len(rust_calls), \
        f"calls 行数不一致: py={len(py_calls)} rust={len(rust_calls)}"
    for i, (py_c, rust_c) in enumerate(zip(py_calls, rust_calls)):
        for key in ["caller_name", "caller_module", "callee_name", "callee_module",
                   "callee_qualified", "callee_file", "callee_id", "call_line",
                   "is_cross_file"]:
            assert py_c[key] == rust_c[key], \
                f"calls[{i}] 字段 {key} 不一致: py={py_c[key]!r} rust={rust_c[key]!r}"
        if not ignore_caller_id:
            assert py_c["caller_id"] == rust_c["caller_id"], \
                f"calls[{i}] caller_id 不一致: py={py_c['caller_id']} rust={rust_c['caller_id']}"


def _assert_call_versions_equal(py_cvs, rust_cvs):
    """差分断言：两端 call_versions 表内容一致

    忽略 file_version_id（两端不同），其他字段必须完全一致。
    """
    assert len(py_cvs) == len(rust_cvs), \
        f"call_versions 行数不一致: py={len(py_cvs)} rust={len(rust_cvs)}"
    for i, (py_v, rust_v) in enumerate(zip(py_cvs, rust_cvs)):
        for key in ["caller_qualified", "caller_hash", "callee_name", "callee_module",
                   "callee_qualified", "callee_file", "call_line", "is_cross_file"]:
            assert py_v[key] == rust_v[key], \
                f"call_versions[{i}] 字段 {key} 不一致: py={py_v[key]!r} rust={rust_v[key]!r}"


# ============================================
# 默认测试 fixture 工厂
# ============================================

def _make_file_result(rel_path, file_instance_id, file_version_id, raw_calls,
                     imports=None, symbols=None, inline_modules=None,
                     fn_hash_map=None, module_path="src.main"):
    """构造 file_results 中的单文件 dict"""
    return {
        "rel_path": rel_path,
        "file_instance_id": file_instance_id,
        "file_version_id": file_version_id,
        "module_path": module_path,
        "raw_calls": raw_calls,
        "imports": imports or [],
        "symbols": symbols or [],
        "inline_modules": inline_modules or [],
        "fn_hash_map": fn_hash_map,
    }


def _make_raw_call(caller_name, callee_name, caller_qualified="", caller_module="",
                   callee_module="", call_line=10):
    """构造 raw_call dict"""
    return {
        "caller_name": caller_name,
        "caller_qualified": caller_qualified,
        "caller_module": caller_module,
        "callee_name": callee_name,
        "callee_module": callee_module,
        "call_line": call_line,
    }


def _make_symbol_dict(name, qualified_name, kind="function", file_instance_id=1,
                      rel_path="src/main.py", content_hash=None):
    """构造 all_symbols 列表中的 symbol dict"""
    if content_hash is None:
        content_hash = _compute_content_hash(f"def {name}(): pass")
    return {
        "id": 0,  # 由 DB 自增，Rust 端从 DB 读取时会填入真实 id
        "name": name,
        "qualified_name": qualified_name,
        "kind": kind,
        "file_instance_id": file_instance_id,
        "rel_path": rel_path,
        "content_hash": content_hash,
    }


# ============================================
# 测试类：C1-C14 batch_resolve_and_save_calls 差分
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestBatchResolveAndSaveCallsDiff:
    """C1-C14: batch_resolve_and_save_calls Python↔Rust 行为差分"""

    def test_c1_strategy1_exact_match(self, tmp_path):
        """C1: 策略 1 精确匹配（callee_module.callee_name 完全匹配 qualified_name）

        场景：单文件包含一个 caller symbol + 一个 callee symbol（在不同 qname 中），
        raw_call 的 callee_module.callee_name 精确匹配 callee 的 qualified_name。
        差分断言：两端 calls 表行完全一致（所有字段）
        """
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        # 两端各预填一个 caller symbol + 一个 callee symbol
        # caller: src.main::caller_fn
        # callee: src.utils.helper_fn（qualified_name="src.utils.helper_fn"）
        py_fi, py_fv = _prep_file_instance(py_db, rel_path="src/main.py",
                                           module_path="src.main")
        rust_fi, rust_fv = _prep_file_instance(rust_db, rel_path="src/main.py",
                                               module_path="src.main")

        # 在 src/main.py 文件中插入 caller symbol
        _insert_symbol(py_db, py_fi, "caller_fn", "src.main.caller_fn", start_line=1)
        _insert_symbol(rust_db, rust_fi, "caller_fn", "src.main.caller_fn", start_line=1)

        # 在 src/utils.py 文件中插入 callee symbol
        py_fi_utils, _ = _prep_file_instance(py_db, rel_path="src/utils.py",
                                              content_hash="ch_utils",
                                              module_path="src.utils")
        rust_fi_utils, _ = _prep_file_instance(rust_db, rel_path="src/utils.py",
                                                content_hash="ch_utils",
                                                module_path="src.utils")
        _insert_symbol(py_db, py_fi_utils, "helper_fn", "src.utils.helper_fn",
                       start_line=1)
        _insert_symbol(rust_db, rust_fi_utils, "helper_fn", "src.utils.helper_fn",
                       start_line=1)

        # 构造 raw_call：callee_module="src.utils", callee_name="helper_fn"
        raw_calls = [
            _make_raw_call("caller_fn", "helper_fn",
                           caller_qualified="src.main.caller_fn",
                           caller_module="src.main",
                           callee_module="src.utils", call_line=5),
        ]
        file_results = {
            "src/main.py": _make_file_result(
                "src/main.py", py_fi, py_fv, raw_calls,
                module_path="src.main",
            ),
        }

        # 加载 all_symbols（两端从各自 DB 读取）
        py_all_symbols = _load_all_symbols_from_db(py_db)
        rust_all_symbols = _load_all_symbols_from_db(rust_db)

        _py_resolve_and_save_calls(py_db, file_results)
        _rust_resolve_and_save_calls(
            rust_db, workspace_id=1,
            file_results=[_make_file_result(
                "src/main.py", rust_fi, rust_fv, raw_calls,
                module_path="src.main",
            )],
            all_symbols=rust_all_symbols,
            external_symbols=[],
            changed_file_instance_ids=[rust_fi],
        )

        py_calls = _query_calls(py_db)
        rust_calls = _query_calls(rust_db)
        _assert_calls_equal(py_calls, rust_calls)
        # 策略 1 命中：callee_qualified 应为 "src.utils.helper_fn"
        assert py_calls[0]["callee_qualified"] == "src.utils.helper_fn"
        assert rust_calls[0]["callee_qualified"] == "src.utils.helper_fn"
        # is_cross_file=1（callee 在不同文件）
        assert py_calls[0]["is_cross_file"] == 1
        assert rust_calls[0]["is_cross_file"] == 1

    def test_c2_strategy3_simple_name_unique(self, tmp_path):
        """C2: 策略 3 简名唯一匹配（无 callee_module）

        场景：raw_call 无 callee_module，callee_name 在全局符号中唯一存在。
        差分断言：两端 calls 表行一致，callee_qualified 为候选 qname
        """
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        py_fi, py_fv = _prep_file_instance(py_db, rel_path="src/main.py",
                                           module_path="src.main")
        rust_fi, rust_fv = _prep_file_instance(rust_db, rel_path="src/main.py",
                                                module_path="src.main")

        _insert_symbol(py_db, py_fi, "caller_fn", "src.main.caller_fn", start_line=1)
        _insert_symbol(rust_db, rust_fi, "caller_fn", "src.main.caller_fn", start_line=1)

        # callee 在另一个文件，简名 "unique_fn" 全局唯一
        py_fi_utils, _ = _prep_file_instance(py_db, rel_path="src/utils.py",
                                              content_hash="ch_utils",
                                              module_path="src.utils")
        rust_fi_utils, _ = _prep_file_instance(rust_db, rel_path="src/utils.py",
                                                content_hash="ch_utils",
                                                module_path="src.utils")
        _insert_symbol(py_db, py_fi_utils, "unique_fn", "src.utils.unique_fn",
                       start_line=1)
        _insert_symbol(rust_db, rust_fi_utils, "unique_fn", "src.utils.unique_fn",
                       start_line=1)

        # raw_call 无 callee_module
        raw_calls = [
            _make_raw_call("caller_fn", "unique_fn",
                           caller_qualified="src.main.caller_fn",
                           call_line=5),
        ]

        _py_resolve_and_save_calls(
            py_db,
            {"src/main.py": _make_file_result("src/main.py", py_fi, py_fv, raw_calls,
                                              module_path="src.main")},
        )
        _rust_resolve_and_save_calls(
            rust_db, workspace_id=1,
            file_results=[_make_file_result("src/main.py", rust_fi, rust_fv, raw_calls,
                                             module_path="src.main")],
            all_symbols=_load_all_symbols_from_db(rust_db),
            external_symbols=[],
            changed_file_instance_ids=[rust_fi],
        )

        py_calls = _query_calls(py_db)
        rust_calls = _query_calls(rust_db)
        _assert_calls_equal(py_calls, rust_calls)
        # 策略 3 命中：callee_qualified 应为 "src.utils.unique_fn"
        assert py_calls[0]["callee_qualified"] == "src.utils.unique_fn"
        assert rust_calls[0]["callee_qualified"] == "src.utils.unique_fn"

    def test_c3_strategy4_same_file_simple_name(self, tmp_path):
        """C3: 策略 4 同文件简名匹配

        场景：callee_name 在当前文件中存在，is_cross_file=0。
        差分断言：两端 calls 表行一致，callee_file=rel_path，is_cross_file=0
        """
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        py_fi, py_fv = _prep_file_instance(py_db, rel_path="src/main.py",
                                           module_path="src.main")
        rust_fi, rust_fv = _prep_file_instance(rust_db, rel_path="src/main.py",
                                                module_path="src.main")

        # caller 和 callee 在同一文件
        _insert_symbol(py_db, py_fi, "caller_fn", "src.main.caller_fn", start_line=1)
        _insert_symbol(py_db, py_fi, "local_helper", "src.main.local_helper",
                       start_line=10)
        _insert_symbol(rust_db, rust_fi, "caller_fn", "src.main.caller_fn", start_line=1)
        _insert_symbol(rust_db, rust_fi, "local_helper", "src.main.local_helper",
                       start_line=10)

        raw_calls = [
            _make_raw_call("caller_fn", "local_helper",
                           caller_qualified="src.main.caller_fn",
                           call_line=5),
        ]

        _py_resolve_and_save_calls(
            py_db,
            {"src/main.py": _make_file_result("src/main.py", py_fi, py_fv, raw_calls,
                                              module_path="src.main")},
        )
        _rust_resolve_and_save_calls(
            rust_db, workspace_id=1,
            file_results=[_make_file_result("src/main.py", rust_fi, rust_fv, raw_calls,
                                             module_path="src.main")],
            all_symbols=_load_all_symbols_from_db(rust_db),
            external_symbols=[],
            changed_file_instance_ids=[rust_fi],
        )

        py_calls = _query_calls(py_db)
        rust_calls = _query_calls(rust_db)
        _assert_calls_equal(py_calls, rust_calls)
        # 策略 4 命中：callee_file=rel_path，is_cross_file=0
        assert py_calls[0]["callee_file"] == "src/main.py"
        assert rust_calls[0]["callee_file"] == "src/main.py"
        assert py_calls[0]["is_cross_file"] == 0
        assert rust_calls[0]["is_cross_file"] == 0

    def test_c4_strategy5_external_symbol(self, tmp_path):
        """C4: 策略 5 external_symbols 唯一匹配

        场景：callee 未在前 4 策略匹配，但在 external_symbols 中唯一存在。
        差分断言：两端 calls 表行一致，callee_id 为负值，callee_file="external://{pkg}"
        """
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        py_fi, py_fv = _prep_file_instance(py_db, rel_path="src/main.py",
                                           module_path="src.main")
        rust_fi, rust_fv = _prep_file_instance(rust_db, rel_path="src/main.py",
                                                module_path="src.main")

        _insert_symbol(py_db, py_fi, "caller_fn", "src.main.caller_fn", start_line=1)
        _insert_symbol(rust_db, rust_fi, "caller_fn", "src.main.caller_fn", start_line=1)

        # 两端各插入外部符号
        _insert_external_symbol(py_db, "printf", "libc.printf", "libc")
        _insert_external_symbol(rust_db, "printf", "libc.printf", "libc")

        # raw_call 无 callee_module（走策略 5 的 ext_by_name 路径）
        raw_calls = [
            _make_raw_call("caller_fn", "printf",
                           caller_qualified="src.main.caller_fn",
                           call_line=5),
        ]

        _py_resolve_and_save_calls(
            py_db,
            {"src/main.py": _make_file_result("src/main.py", py_fi, py_fv, raw_calls,
                                              module_path="src.main")},
        )
        _rust_resolve_and_save_calls(
            rust_db, workspace_id=1,
            file_results=[_make_file_result("src/main.py", rust_fi, rust_fv, raw_calls,
                                             module_path="src.main")],
            all_symbols=_load_all_symbols_from_db(rust_db),
            external_symbols=_load_external_symbols_from_db(rust_db),
            changed_file_instance_ids=[rust_fi],
        )

        py_calls = _query_calls(py_db)
        rust_calls = _query_calls(rust_db)
        _assert_calls_equal(py_calls, rust_calls)
        # 策略 5 命中：callee_id 为负值
        assert py_calls[0]["callee_id"] < 0
        assert rust_calls[0]["callee_id"] < 0
        assert py_calls[0]["callee_file"] == "external://libc"
        assert rust_calls[0]["callee_file"] == "external://libc"
        assert py_calls[0]["is_cross_file"] == 1
        assert rust_calls[0]["is_cross_file"] == 1

    def test_c5_strategy2_import_mapping(self, tmp_path):
        """C5: 策略 2 import 映射

        场景：callee_module 在 file_imports 中有映射，通过 import 完整路径的末段组合
        匹配 callee_name。
        差分断言：两端 calls 表行一致
        """
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        py_fi, py_fv = _prep_file_instance(py_db, rel_path="src/main.py",
                                           module_path="src.main")
        rust_fi, rust_fv = _prep_file_instance(rust_db, rel_path="src/main.py",
                                                module_path="src.main")

        _insert_symbol(py_db, py_fi, "caller_fn", "src.main.caller_fn", start_line=1)
        _insert_symbol(rust_db, rust_fi, "caller_fn", "src.main.caller_fn", start_line=1)

        # callee 在 src/utils.py，qualified_name="src.utils.helper_fn"
        py_fi_utils, _ = _prep_file_instance(py_db, rel_path="src/utils.py",
                                              content_hash="ch_utils",
                                              module_path="src.utils")
        rust_fi_utils, _ = _prep_file_instance(rust_db, rel_path="src/utils.py",
                                                content_hash="ch_utils",
                                                module_path="src.utils")
        _insert_symbol(py_db, py_fi_utils, "helper_fn", "src.utils.helper_fn",
                       start_line=1)
        _insert_symbol(rust_db, rust_fi_utils, "helper_fn", "src.utils.helper_fn",
                       start_line=1)

        # raw_call: callee_module="utils"（别名，通过 import 映射到 "src.utils"）
        # imports=["src.utils"] → alias="utils"
        raw_calls = [
            _make_raw_call("caller_fn", "helper_fn",
                           caller_qualified="src.main.caller_fn",
                           callee_module="utils", call_line=5),
        ]
        file_results_py = {
            "src/main.py": _make_file_result(
                "src/main.py", py_fi, py_fv, raw_calls,
                imports=["src.utils"],
                module_path="src.main",
            ),
        }

        _py_resolve_and_save_calls(py_db, file_results_py)
        _rust_resolve_and_save_calls(
            rust_db, workspace_id=1,
            file_results=[_make_file_result(
                "src/main.py", rust_fi, rust_fv, raw_calls,
                imports=["src.utils"],
                module_path="src.main",
            )],
            all_symbols=_load_all_symbols_from_db(rust_db),
            external_symbols=[],
            changed_file_instance_ids=[rust_fi],
        )

        py_calls = _query_calls(py_db)
        rust_calls = _query_calls(rust_db)
        _assert_calls_equal(py_calls, rust_calls)
        # 策略 2 命中：callee_qualified 应为 "src.utils.helper_fn"
        assert py_calls[0]["callee_qualified"] == "src.utils.helper_fn"
        assert rust_calls[0]["callee_qualified"] == "src.utils.helper_fn"

    def test_c6_empty_callee_name(self, tmp_path):
        """C6: 无 callee_name（空 call）

        场景：raw_call 的 callee_name 为空字符串。
        差分断言：两端 calls 表行一致，callee_qualified=""
        """
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        py_fi, py_fv = _prep_file_instance(py_db, rel_path="src/main.py",
                                           module_path="src.main")
        rust_fi, rust_fv = _prep_file_instance(rust_db, rel_path="src/main.py",
                                                module_path="src.main")

        _insert_symbol(py_db, py_fi, "caller_fn", "src.main.caller_fn", start_line=1)
        _insert_symbol(rust_db, rust_fi, "caller_fn", "src.main.caller_fn", start_line=1)

        raw_calls = [
            _make_raw_call("caller_fn", "",
                           caller_qualified="src.main.caller_fn",
                           call_line=5),
        ]

        _py_resolve_and_save_calls(
            py_db,
            {"src/main.py": _make_file_result("src/main.py", py_fi, py_fv, raw_calls,
                                              module_path="src.main")},
        )
        _rust_resolve_and_save_calls(
            rust_db, workspace_id=1,
            file_results=[_make_file_result("src/main.py", rust_fi, rust_fv, raw_calls,
                                             module_path="src.main")],
            all_symbols=_load_all_symbols_from_db(rust_db),
            external_symbols=[],
            changed_file_instance_ids=[rust_fi],
        )

        py_calls = _query_calls(py_db)
        rust_calls = _query_calls(rust_db)
        _assert_calls_equal(py_calls, rust_calls)
        # 空 callee_name：callee_qualified=""
        assert py_calls[0]["callee_qualified"] == ""
        assert rust_calls[0]["callee_qualified"] == ""

    def test_c7_mixed_strategies(self, tmp_path):
        """C7: 单文件多个 call 混合策略

        场景：3 个 call：1 个策略 1（精确）+ 1 个策略 3（简名）+ 1 个未解析。
        差分断言：两端 calls 表 3 行完全一致
        """
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        py_fi, py_fv = _prep_file_instance(py_db, rel_path="src/main.py",
                                           module_path="src.main")
        rust_fi, rust_fv = _prep_file_instance(rust_db, rel_path="src/main.py",
                                                module_path="src.main")

        # caller: src.main.caller_fn
        _insert_symbol(py_db, py_fi, "caller_fn", "src.main.caller_fn", start_line=1)
        _insert_symbol(rust_db, rust_fi, "caller_fn", "src.main.caller_fn", start_line=1)

        # callee1: src.utils.helper_fn（策略 1 精确匹配）
        py_fi_u, _ = _prep_file_instance(py_db, rel_path="src/utils.py",
                                          content_hash="ch_u", module_path="src.utils")
        rust_fi_u, _ = _prep_file_instance(rust_db, rel_path="src/utils.py",
                                            content_hash="ch_u", module_path="src.utils")
        _insert_symbol(py_db, py_fi_u, "helper_fn", "src.utils.helper_fn", start_line=1)
        _insert_symbol(rust_db, rust_fi_u, "helper_fn", "src.utils.helper_fn",
                       start_line=1)

        # callee2: src.utils.simple_fn（策略 3 简名唯一匹配，无 callee_module）
        _insert_symbol(py_db, py_fi_u, "simple_fn", "src.utils.simple_fn", start_line=10)
        _insert_symbol(rust_db, rust_fi_u, "simple_fn", "src.utils.simple_fn",
                       start_line=10)

        raw_calls = [
            # call 1: 策略 1 精确匹配
            _make_raw_call("caller_fn", "helper_fn",
                           caller_qualified="src.main.caller_fn",
                           callee_module="src.utils", call_line=5),
            # call 2: 策略 3 简名唯一匹配
            _make_raw_call("caller_fn", "simple_fn",
                           caller_qualified="src.main.caller_fn",
                           call_line=6),
            # call 3: 未解析（callee_name 不存在）
            _make_raw_call("caller_fn", "nonexistent_fn",
                           caller_qualified="src.main.caller_fn",
                           call_line=7),
        ]

        _py_resolve_and_save_calls(
            py_db,
            {"src/main.py": _make_file_result("src/main.py", py_fi, py_fv, raw_calls,
                                              module_path="src.main")},
        )
        _rust_resolve_and_save_calls(
            rust_db, workspace_id=1,
            file_results=[_make_file_result("src/main.py", rust_fi, rust_fv, raw_calls,
                                             module_path="src.main")],
            all_symbols=_load_all_symbols_from_db(rust_db),
            external_symbols=[],
            changed_file_instance_ids=[rust_fi],
        )

        py_calls = _query_calls(py_db)
        rust_calls = _query_calls(rust_db)
        _assert_calls_equal(py_calls, rust_calls)
        assert len(py_calls) == 3
        assert len(rust_calls) == 3

    def test_c8_caller_id_zero_skipped(self, tmp_path):
        """C8: caller_id=0 fallback 全失败 → 跳过 calls 表插入

        场景：caller 不在 qname_id_map 且 file_sym_id_map 无 simple_name。
        差分断言：两端 calls 表行数一致（少 1 行，但 call_versions 仍写入）
        """
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        py_fi, py_fv = _prep_file_instance(py_db, rel_path="src/main.py",
                                           module_path="src.main")
        rust_fi, rust_fv = _prep_file_instance(rust_db, rel_path="src/main.py",
                                                module_path="src.main")

        # 不插入任何 caller symbol（caller_name 无法匹配）
        # callee 存在
        py_fi_u, _ = _prep_file_instance(py_db, rel_path="src/utils.py",
                                          content_hash="ch_u", module_path="src.utils")
        rust_fi_u, _ = _prep_file_instance(rust_db, rel_path="src/utils.py",
                                            content_hash="ch_u", module_path="src.utils")
        _insert_symbol(py_db, py_fi_u, "helper_fn", "src.utils.helper_fn", start_line=1)
        _insert_symbol(rust_db, rust_fi_u, "helper_fn", "src.utils.helper_fn",
                       start_line=1)

        raw_calls = [
            _make_raw_call("nonexistent_caller", "helper_fn",
                           caller_qualified="src.main.nonexistent_caller",
                           callee_module="src.utils", call_line=5),
        ]

        _py_resolve_and_save_calls(
            py_db,
            {"src/main.py": _make_file_result("src/main.py", py_fi, py_fv, raw_calls,
                                              module_path="src.main")},
        )
        _rust_resolve_and_save_calls(
            rust_db, workspace_id=1,
            file_results=[_make_file_result("src/main.py", rust_fi, rust_fv, raw_calls,
                                             module_path="src.main")],
            all_symbols=_load_all_symbols_from_db(rust_db),
            external_symbols=[],
            changed_file_instance_ids=[rust_fi],
        )

        py_calls = _query_calls(py_db)
        rust_calls = _query_calls(rust_db)
        # 两端 calls 表为空（caller_id=0 被跳过）
        assert len(py_calls) == 0
        assert len(rust_calls) == 0

        # 但 call_versions 仍写入（caller_id 不影响 call_versions）
        py_cvs = _query_call_versions(py_db)
        rust_cvs = _query_call_versions(rust_db)
        _assert_call_versions_equal(py_cvs, rust_cvs)
        assert len(py_cvs) == 1
        assert len(rust_cvs) == 1

    def test_c9_call_versions_with_hash(self, tmp_path):
        """C9: call_versions 写入（caller_qualified + caller_hash）

        场景：symbols 中包含 fn 类型 + content_hash，写入 call_versions 时
        caller_hash = fn_hash_map.get(caller_qualified, "")。
        差分断言：两端 call_versions 表行一致
        """
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        py_fi, py_fv = _prep_file_instance(py_db, rel_path="src/main.py",
                                           module_path="src.main")
        rust_fi, rust_fv = _prep_file_instance(rust_db, rel_path="src/main.py",
                                                module_path="src.main")

        # caller symbol（含 content_hash）
        caller_hash = "abc123hash"
        _insert_symbol(py_db, py_fi, "caller_fn", "src.main.caller_fn", start_line=1,
                       content_hash=caller_hash)
        _insert_symbol(rust_db, rust_fi, "caller_fn", "src.main.caller_fn", start_line=1,
                       content_hash=caller_hash)

        raw_calls = [
            _make_raw_call("caller_fn", "nonexistent_fn",
                           caller_qualified="src.main.caller_fn",
                           call_line=5),
        ]

        # file_results 中 symbols 提供 fn_hash_map
        symbols_meta = [{
            "name": "caller_fn",
            "qualified_name": "src.main.caller_fn",
            "kind": "fn",
            "content_hash": caller_hash,
        }]

        _py_resolve_and_save_calls(
            py_db,
            {"src/main.py": _make_file_result(
                "src/main.py", py_fi, py_fv, raw_calls,
                symbols=symbols_meta,
                module_path="src.main",
            )},
        )
        _rust_resolve_and_save_calls(
            rust_db, workspace_id=1,
            file_results=[_make_file_result(
                "src/main.py", rust_fi, rust_fv, raw_calls,
                symbols=symbols_meta,
                module_path="src.main",
            )],
            all_symbols=_load_all_symbols_from_db(rust_db),
            external_symbols=[],
            changed_file_instance_ids=[rust_fi],
        )

        py_cvs = _query_call_versions(py_db)
        rust_cvs = _query_call_versions(rust_db)
        _assert_call_versions_equal(py_cvs, rust_cvs)
        # caller_hash 应为预填的 hash
        assert py_cvs[0]["caller_hash"] == caller_hash
        assert rust_cvs[0]["caller_hash"] == caller_hash

    def test_c10_caller_qualified_derivation(self, tmp_path):
        """C10: caller_qualified 为空时推导 `{module_path}::{caller_name}`

        场景：raw_call 的 caller_qualified 为空，推导为 "src.main::caller_fn"。
        差分断言：两端 call_versions 表 caller_qualified 一致
        """
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        py_fi, py_fv = _prep_file_instance(py_db, rel_path="src/main.py",
                                           module_path="src.main")
        rust_fi, rust_fv = _prep_file_instance(rust_db, rel_path="src/main.py",
                                                module_path="src.main")

        _insert_symbol(py_db, py_fi, "caller_fn", "src.main.caller_fn", start_line=1)
        _insert_symbol(rust_db, rust_fi, "caller_fn", "src.main.caller_fn", start_line=1)

        # raw_call 的 caller_qualified 为空
        raw_calls = [
            _make_raw_call("caller_fn", "nonexistent_fn",
                           caller_qualified="",
                           call_line=5),
        ]

        _py_resolve_and_save_calls(
            py_db,
            {"src/main.py": _make_file_result("src/main.py", py_fi, py_fv, raw_calls,
                                              module_path="src.main")},
        )
        _rust_resolve_and_save_calls(
            rust_db, workspace_id=1,
            file_results=[_make_file_result("src/main.py", rust_fi, rust_fv, raw_calls,
                                             module_path="src.main")],
            all_symbols=_load_all_symbols_from_db(rust_db),
            external_symbols=[],
            changed_file_instance_ids=[rust_fi],
        )

        py_cvs = _query_call_versions(py_db)
        rust_cvs = _query_call_versions(rust_db)
        _assert_call_versions_equal(py_cvs, rust_cvs)
        # caller_qualified 推导为 "src.main::caller_fn"
        assert py_cvs[0]["caller_qualified"] == "src.main::caller_fn"
        assert rust_cvs[0]["caller_qualified"] == "src.main::caller_fn"

    def test_c11_delete_old_calls(self, tmp_path):
        """C11: DELETE 旧 calls（changed_file_instance_ids 非空）

        场景：预填 1 个旧 call，调用 batch_resolve_and_save_calls 时通过
        changed_file_instance_ids 触发 DELETE。
        差分断言：两端 old_calls_deleted 一致，预填的旧 calls 均被删除
        """
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        py_fi, py_fv = _prep_file_instance(py_db, rel_path="src/main.py",
                                           module_path="src.main")
        rust_fi, rust_fv = _prep_file_instance(rust_db, rel_path="src/main.py",
                                                module_path="src.main")

        # 预填 caller symbol（两端相同结构）
        py_caller_id = _insert_symbol(py_db, py_fi, "caller_fn", "src.main.caller_fn",
                                      start_line=1)
        rust_caller_id = _insert_symbol(rust_db, rust_fi, "caller_fn",
                                         "src.main.caller_fn", start_line=1)

        # 预填旧 call（两端相同）
        _insert_old_call(py_db, py_caller_id, caller_name="caller_fn",
                         callee_name="old_callee", call_line=100)
        _insert_old_call(rust_db, rust_caller_id, caller_name="caller_fn",
                         callee_name="old_callee", call_line=100)

        # 验证预填成功
        assert len(_query_calls(py_db)) == 1
        assert len(_query_calls(rust_db)) == 1

        raw_calls = [
            _make_raw_call("caller_fn", "new_callee",
                           caller_qualified="src.main.caller_fn",
                           call_line=5),
        ]

        _py_resolve_and_save_calls(
            py_db,
            {"src/main.py": _make_file_result("src/main.py", py_fi, py_fv, raw_calls,
                                              module_path="src.main")},
        )
        rust_result = _rust_resolve_and_save_calls(
            rust_db, workspace_id=1,
            file_results=[_make_file_result("src/main.py", rust_fi, rust_fv, raw_calls,
                                             module_path="src.main")],
            all_symbols=_load_all_symbols_from_db(rust_db),
            external_symbols=[],
            changed_file_instance_ids=[rust_fi],
        )

        # Rust 端返回的 old_calls_deleted 应为 1
        assert rust_result["success"] is True
        assert rust_result["old_calls_deleted"] == 1

        py_calls = _query_calls(py_db)
        rust_calls = _query_calls(rust_db)
        # 两端只剩 1 个新 call（旧 call 被 DELETE）
        assert len(py_calls) == 1
        assert len(rust_calls) == 1
        _assert_calls_equal(py_calls, rust_calls)

    def test_c12_multi_file_batch(self, tmp_path):
        """C12: 多文件批量（2 个文件，每个文件 2 个 call）

        场景：2 个文件各 2 个 call，共 4 行 calls + 4 行 call_versions。
        差分断言：两端 calls + call_versions 表行完全一致
        """
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        # 文件 1: src/main.py
        py_fi1, py_fv1 = _prep_file_instance(py_db, rel_path="src/main.py",
                                              content_hash="ch1", module_path="src.main")
        rust_fi1, rust_fv1 = _prep_file_instance(rust_db, rel_path="src/main.py",
                                                   content_hash="ch1",
                                                   module_path="src.main")

        # 文件 2: src/utils.py
        py_fi2, py_fv2 = _prep_file_instance(py_db, rel_path="src/utils.py",
                                              content_hash="ch2", module_path="src.utils")
        rust_fi2, rust_fv2 = _prep_file_instance(rust_db, rel_path="src/utils.py",
                                                   content_hash="ch2",
                                                   module_path="src.utils")

        # 两端各插入 4 个 symbols（每文件 2 个）
        _insert_symbol(py_db, py_fi1, "caller_fn1", "src.main.caller_fn1", start_line=1)
        _insert_symbol(py_db, py_fi1, "callee_fn1", "src.main.callee_fn1", start_line=10)
        _insert_symbol(py_db, py_fi2, "caller_fn2", "src.utils.caller_fn2", start_line=1)
        _insert_symbol(py_db, py_fi2, "callee_fn2", "src.utils.callee_fn2", start_line=10)

        _insert_symbol(rust_db, rust_fi1, "caller_fn1", "src.main.caller_fn1", start_line=1)
        _insert_symbol(rust_db, rust_fi1, "callee_fn1", "src.main.callee_fn1", start_line=10)
        _insert_symbol(rust_db, rust_fi2, "caller_fn2", "src.utils.caller_fn2", start_line=1)
        _insert_symbol(rust_db, rust_fi2, "callee_fn2", "src.utils.callee_fn2", start_line=10)

        raw_calls_1 = [
            _make_raw_call("caller_fn1", "callee_fn1",
                           caller_qualified="src.main.caller_fn1",
                           call_line=5),
            _make_raw_call("caller_fn1", "callee_fn2",
                           caller_qualified="src.main.caller_fn1",
                           callee_module="src.utils", call_line=6),
        ]
        raw_calls_2 = [
            _make_raw_call("caller_fn2", "callee_fn2",
                           caller_qualified="src.utils.caller_fn2",
                           call_line=5),
            _make_raw_call("caller_fn2", "callee_fn1",
                           caller_qualified="src.utils.caller_fn2",
                           callee_module="src.main", call_line=6),
        ]

        _py_resolve_and_save_calls(
            py_db,
            {
                "src/main.py": _make_file_result("src/main.py", py_fi1, py_fv1,
                                                  raw_calls_1, module_path="src.main"),
                "src/utils.py": _make_file_result("src/utils.py", py_fi2, py_fv2,
                                                  raw_calls_2, module_path="src.utils"),
            },
        )
        _rust_resolve_and_save_calls(
            rust_db, workspace_id=1,
            file_results=[
                _make_file_result("src/main.py", rust_fi1, rust_fv1, raw_calls_1,
                                  module_path="src.main"),
                _make_file_result("src/utils.py", rust_fi2, rust_fv2, raw_calls_2,
                                  module_path="src.utils"),
            ],
            all_symbols=_load_all_symbols_from_db(rust_db),
            external_symbols=[],
            changed_file_instance_ids=[rust_fi1, rust_fi2],
        )

        py_calls = _query_calls(py_db)
        rust_calls = _query_calls(rust_db)
        _assert_calls_equal(py_calls, rust_calls)
        assert len(py_calls) == 4
        assert len(rust_calls) == 4

        py_cvs = _query_call_versions(py_db)
        rust_cvs = _query_call_versions(rust_db)
        _assert_call_versions_equal(py_cvs, rust_cvs)
        assert len(py_cvs) == 4
        assert len(rust_cvs) == 4

    def test_c13_hcl_multi_segment_name(self, tmp_path):
        """C13: HCL 多段 name（策略 4.5，callee_name="aws_security_group.this"）

        场景：callee_name 含 "."，通过 name_to_qname（symbol.name 字段）匹配。
        差分断言：两端 calls 表行一致
        """
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        py_fi, py_fv = _prep_file_instance(py_db, rel_path="main.tf",
                                           content_hash="ch_hcl",
                                           module_path="", language="hcl")
        rust_fi, rust_fv = _prep_file_instance(rust_db, rel_path="main.tf",
                                                content_hash="ch_hcl",
                                                module_path="", language="hcl")

        # caller: qualified_name="aws_security_group.web"
        _insert_symbol(py_db, py_fi, "aws_security_group.web",
                       "aws_security_group.web", start_line=1)
        _insert_symbol(rust_db, rust_fi, "aws_security_group.web",
                       "aws_security_group.web", start_line=1)

        # callee: name="aws_security_group.this", qualified_name="aws_security_group.this"
        # symbol.name 字段直接匹配 callee_name
        _insert_symbol(py_db, py_fi, "aws_security_group.this",
                       "aws_security_group.this", start_line=10)
        _insert_symbol(rust_db, rust_fi, "aws_security_group.this",
                       "aws_security_group.this", start_line=10)

        # raw_call: callee_name="aws_security_group.this"（含 "."）
        raw_calls = [
            _make_raw_call("aws_security_group.web", "aws_security_group.this",
                           caller_qualified="aws_security_group.web",
                           call_line=5),
        ]

        _py_resolve_and_save_calls(
            py_db,
            {"main.tf": _make_file_result("main.tf", py_fi, py_fv, raw_calls,
                                          module_path="")},
        )
        _rust_resolve_and_save_calls(
            rust_db, workspace_id=1,
            file_results=[_make_file_result("main.tf", rust_fi, rust_fv, raw_calls,
                                             module_path="")],
            all_symbols=_load_all_symbols_from_db(rust_db),
            external_symbols=[],
            changed_file_instance_ids=[rust_fi],
        )

        py_calls = _query_calls(py_db)
        rust_calls = _query_calls(rust_db)
        _assert_calls_equal(py_calls, rust_calls)
        # 策略 4.5 命中：callee_qualified 应为 "aws_security_group.this"
        assert py_calls[0]["callee_qualified"] == "aws_security_group.this"
        assert rust_calls[0]["callee_qualified"] == "aws_security_group.this"

    def test_c14_empty_file_results(self, tmp_path):
        """C14: 空 file_results（无文件需处理）

        场景：传入空 file_results，无 SQL 执行。
        差分断言：两端无副作用，dict 返回值一致
        """
        py_db = tmp_path / "py.db"
        rust_db = tmp_path / "rust.db"
        _make_codegraph_db(py_db)
        _make_codegraph_db(rust_db)

        # Python 路径：传入空 file_results
        _py_resolve_and_save_calls(py_db, {})

        # Rust 路径：传入空 file_results
        rust_result = _rust_resolve_and_save_calls(
            rust_db, workspace_id=1,
            file_results=[],
            all_symbols=[],
            external_symbols=[],
            changed_file_instance_ids=[],
        )

        # 两端无副作用
        assert len(_query_calls(py_db)) == 0
        assert len(_query_calls(rust_db)) == 0
        assert len(_query_call_versions(py_db)) == 0
        assert len(_query_call_versions(rust_db)) == 0

        # Rust 返回值检查
        assert rust_result["success"] is True
        assert rust_result["total_calls"] == 0
        assert rust_result["resolved_count"] == 0
        assert rust_result["calls_inserted"] == 0
        assert rust_result["call_versions_inserted"] == 0
        assert rust_result["old_calls_deleted"] == 0
        assert rust_result["files_processed"] == 0
