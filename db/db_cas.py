"""Global CAS（Content-Addressable Storage）缓存池。

相同文件跨用户、跨工作区只解析一次。
CAS key = sha256(content_hash + language + parser_version + callwarden_version + extraction_config_version + abi_version + input_abi_version)
"""

import sqlite3
import hashlib
import time
from typing import Optional, List, Dict, Any, Tuple


# CAS schema DDL
CAS_SCHEMA_DDL = """
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
"""

CAS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_cas_symbols_cas_key ON cas_symbols(cas_key);
CREATE INDEX IF NOT EXISTS idx_cas_symbols_content_hash ON cas_symbols(symbol_content_hash);
CREATE INDEX IF NOT EXISTS idx_cas_raw_calls_cas_key ON cas_raw_calls(cas_key);
CREATE INDEX IF NOT EXISTS idx_cas_file_cache_content_hash ON cas_file_cache(content_hash);
CREATE INDEX IF NOT EXISTS idx_cas_file_cache_state ON cas_file_cache(state);
"""


def init_cas_schema(conn: sqlite3.Connection):
    """初始化 CAS schema。"""
    conn.executescript(CAS_SCHEMA_DDL)
    conn.executescript(CAS_INDEX_SQL)
    conn.commit()


def compute_cas_key_v1(content_hash: str, language: str, parser_version: str,
                       callwarden_version: str, extraction_config_version: str,
                       abi_version: str, input_abi_version: str) -> str:
    """计算 CAS key——全文档唯一的 CAS key 计算函数。"""
    raw = (f"{content_hash}|{language}|{parser_version}|{callwarden_version}|"
           f"{extraction_config_version}|{abi_version}|{input_abi_version}")
    return hashlib.sha256(raw.encode()).hexdigest()


def cas_lookup(conn: sqlite3.Connection, cas_key: str) -> Optional[Dict[str, Any]]:
    """查询 CAS 是否命中（state='ready'）。"""
    row = conn.execute(
        "SELECT * FROM cas_file_cache WHERE cas_key = ? AND state = 'ready'",
        (cas_key,)
    ).fetchone()
    return dict(row) if row else None


def cas_publish(conn: sqlite3.Connection, cas_key: str, content_hash: str,
                language: str, parse_result: Dict[str, Any],
                parser_version: str = "0.1.0", callwarden_version: str = "0.2.0",
                extraction_config_version: str = "v1", abi_version: str = "v1",
                input_abi_version: str = "v1"):
    """CAS 原子发布——四阶段：building → payload → raw calls → ready。"""
    now = time.time()

    # 阶段 1: 插入 building 状态
    conn.execute(
        """INSERT OR IGNORE INTO cas_file_cache
           (cas_key, content_hash, language, file_size, total_lines,
            parser_version, callwarden_version, extraction_config_version,
            abi_version, input_abi_version, state, parsed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'building', ?)""",
        (cas_key, content_hash, language,
         parse_result.get("file_size", 0), parse_result.get("total_lines", 0),
         parser_version, callwarden_version, extraction_config_version,
         abi_version, input_abi_version, now)
    )

    # 阶段 2: 写入符号正文
    symbols = parse_result.get("symbols", [])
    for sym in symbols:
        sym_content = sym.get("content", "")
        sym_content_hash = hashlib.sha256(sym_content.encode()).hexdigest()
        conn.execute(
            "INSERT OR IGNORE INTO cas_symbol_contents (content_hash, content) VALUES (?, ?)",
            (sym_content_hash, sym_content)
        )

    # 阶段 3: 写入符号 + raw calls + imports
    for i, sym in enumerate(symbols):
        sym_content = sym.get("content", "")
        sym_content_hash = hashlib.sha256(sym_content.encode()).hexdigest()
        conn.execute(
            """INSERT OR REPLACE INTO cas_symbols
               (cas_key, local_symbol_id, symbol_content_hash, name,
                local_qualified_name, lexical_parent_local_id, kind,
                start_line, end_line, start_col, end_col, start_byte, end_byte,
                visibility, signature, has_comment, depth)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cas_key, i, sym_content_hash, sym.get("name", ""),
             sym.get("qualified_name", ""), sym.get("parent_id"),
             sym.get("kind", "function"), sym.get("start_line", 0),
             sym.get("end_line", 0), sym.get("start_col", 0),
             sym.get("end_col", 0), sym.get("start_byte", 0),
             sym.get("end_byte", 0), sym.get("visibility", "private"),
             sym.get("signature", ""), int(sym.get("has_comment", False)),
             sym.get("depth", -1))
        )

    for call in parse_result.get("raw_calls", []):
        conn.execute(
            """INSERT OR IGNORE INTO cas_raw_calls
               (cas_key, caller_local_id, caller_name, callee_name, call_line, call_ordinal)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cas_key, call.get("caller_id"), call.get("caller_name", ""),
             call.get("callee_name", ""), call.get("line", 0),
             call.get("ordinal", 0))
        )

    for imp in parse_result.get("imports", []):
        conn.execute(
            """INSERT OR IGNORE INTO cas_imports
               (cas_key, import_path, import_kind) VALUES (?, ?, ?)""",
            (cas_key, imp.get("path", ""), imp.get("kind", "import"))
        )

    # 阶段 4: 原子切换 ready
    conn.execute(
        "UPDATE cas_file_cache SET state = 'ready' WHERE cas_key = ?",
        (cas_key,)
    )
    conn.commit()


def cas_pin(conn: sqlite3.Connection, cas_key: str, workspace_id: int,
            ttl_seconds: float = 3600):
    """添加 CAS pending ref（GC 保护窗口）。"""
    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO cas_pending_refs (cas_key, workspace_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (cas_key, workspace_id, now + ttl_seconds, now)
    )
    conn.commit()


def cas_gc(conn: sqlite3.Connection, live_keys: set, grace_period_days: float = 7) -> bool:
    """唯一 GC 实现——mark-sweep。

    协议：先删子表 → 正文表 → 父表 → building 孤儿 → pending_refs TTL。
    """
    now = time.time()
    try:
        conn.execute("BEGIN IMMEDIATE")

        # 阶段 2：pending_refs（未过期）并入 live set（不变量 C9）
        pending_keys = conn.execute(
            "SELECT DISTINCT cas_key FROM cas_pending_refs WHERE expires_at > ?",
            (now,)
        ).fetchall()
        live_keys = set(live_keys)
        live_keys.update(r["cas_key"] for r in pending_keys)

        # 创建临时 live 表
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _gc_live (cas_key TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _gc_live")
        conn.executemany("INSERT OR IGNORE INTO _gc_live VALUES (?)",
                         [(k,) for k in live_keys])

        # 3a. 先删子表
        conn.execute("DELETE FROM cas_symbols WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)")
        conn.execute("DELETE FROM cas_raw_calls WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)")
        conn.execute("DELETE FROM cas_imports WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)")

        # 3b. 再删正文表
        conn.execute("""DELETE FROM cas_symbol_contents
                        WHERE content_hash NOT IN
                        (SELECT DISTINCT symbol_content_hash FROM cas_symbols)""")

        # 3c. 最后删父表（只删 ready）
        conn.execute("""DELETE FROM cas_file_cache
                        WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)
                        AND state = 'ready'""")

        # 3d. 清理孤儿 building 条目
        conn.execute("DELETE FROM cas_symbols WHERE cas_key IN (SELECT cas_key FROM cas_file_cache WHERE state = 'building')")
        conn.execute("DELETE FROM cas_raw_calls WHERE cas_key IN (SELECT cas_key FROM cas_file_cache WHERE state = 'building')")
        conn.execute("DELETE FROM cas_imports WHERE cas_key IN (SELECT cas_key FROM cas_file_cache WHERE state = 'building')")
        conn.execute("DELETE FROM cas_symbol_contents WHERE content_hash NOT IN (SELECT DISTINCT symbol_content_hash FROM cas_symbols)")
        conn.execute("DELETE FROM cas_file_cache WHERE state = 'building'")

        # 3e. 清理过期 pending_refs
        conn.execute("DELETE FROM cas_pending_refs WHERE expires_at <= ?", (now,))

        conn.execute("DROP TABLE _gc_live")
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise
