"""cas_schema.py — CAS schema DDL 与初始化（db/ 退休 phase-3 本地化）

原 db/db_cas.py 的 CAS_SCHEMA_DDL / CAS_INDEX_SQL / init_cas_schema 搬迁至
本模块，去除 daemon_server 对 callwarden.db.db_cas 的依赖。
file_generations DDL 复用 replicator 本地常量（避免重复维护）。
"""
from __future__ import annotations

import sqlite3

from callwarden.server.replicator import FILE_GENERATIONS_DDL

# CAS schema DDL（原 db_cas.CAS_SCHEMA_DDL，逐行保持一致）
CAS_SCHEMA_DDL = f"""
-- CAS 文件缓存表
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

-- CAS 符号正文表
CREATE TABLE IF NOT EXISTS cas_symbol_contents (
    content_hash TEXT PRIMARY KEY,
    content TEXT NOT NULL
);

-- CAS 符号表
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

-- CAS raw calls（单文件内）
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

-- CAS imports
CREATE TABLE IF NOT EXISTS cas_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cas_key TEXT NOT NULL,
    import_path TEXT NOT NULL,
    import_kind TEXT DEFAULT 'import',
    FOREIGN KEY (cas_key) REFERENCES cas_file_cache(cas_key),
    UNIQUE(cas_key, import_path, import_kind)
);

-- GC 窗口期引用保护
CREATE TABLE IF NOT EXISTS cas_pending_refs (
    cas_key TEXT NOT NULL,
    workspace_id INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (cas_key, workspace_id)
);

-- file_generations 两阶段 CAS（daemon 侧）
-- 规范：cas-gc-protocol.md §4
-- 防止 stale manifest commit 和 CAS 投毒
{FILE_GENERATIONS_DDL}
"""

CAS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_cas_symbols_cas_key ON cas_symbols(cas_key);
CREATE INDEX IF NOT EXISTS idx_cas_symbols_content_hash ON cas_symbols(symbol_content_hash);
CREATE INDEX IF NOT EXISTS idx_cas_raw_calls_cas_key ON cas_raw_calls(cas_key);
CREATE INDEX IF NOT EXISTS idx_cas_file_cache_content_hash ON cas_file_cache(content_hash);
CREATE INDEX IF NOT EXISTS idx_cas_file_cache_state ON cas_file_cache(state);
"""


def init_cas_schema(conn: sqlite3.Connection) -> None:
    """初始化 CAS schema。"""
    conn.executescript(CAS_SCHEMA_DDL)
    conn.executescript(CAS_INDEX_SQL)
    conn.commit()
