"""Global CAS（Content-Addressable Storage）缓存池。

相同文件跨用户、跨工作区只解析一次。
CAS key = sha256(content_hash + language + parser_version + callwarden_version + extraction_config_version + abi_version + input_abi_version)
"""

import sqlite3
import hashlib
import time
import os
from typing import Optional, List, Dict, Any, Tuple

# 跨平台 flock 支持
# 规范：cas-gc-protocol.md §3.1/§5.1
# Unix 用 fcntl.flock，Windows 用 msvcrt.locking，无两者时降级为无锁
try:
    import fcntl as _fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False
    try:
        import msvcrt as _msvcrt
        _HAS_MSVCRT = True
    except ImportError:
        _HAS_MSVCRT = False


def _flock_exclusive(fd: int):
    """获取排他锁（LOCK_EX），跨平台"""
    if _HAS_FCNTL:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
    elif _HAS_MSVCRT:
        _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)
    # 无 fcntl/msvcrt 时降级为无锁（仅靠 BEGIN IMMEDIATE 保护）


def _flock_shared(fd: int):
    """获取共享锁（LOCK_SH），跨平台"""
    if _HAS_FCNTL:
        _fcntl.flock(fd, _fcntl.LOCK_SH)
    elif _HAS_MSVCRT:
        # msvcrt 没有共享锁，用排他锁降级（Windows 开发环境可接受）
        _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)


def _flock_unlock(fd: int):
    """释放锁"""
    if _HAS_FCNTL:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
    elif _HAS_MSVCRT:
        try:
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


# file_generations DDL（共享定义，replicator.py 从此处导入，避免重复维护）
# 规范：cas-gc-protocol.md §4 / watcher-generation-state-machine.md
FILE_GENERATIONS_DDL = """CREATE TABLE IF NOT EXISTS file_generations (
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    latest_session_id TEXT DEFAULT '',
    latest_session_epoch INTEGER DEFAULT 0,
    latest_seq INTEGER DEFAULT 0,
    latest_seen_generation TEXT DEFAULT '',
    latest_committed_generation TEXT DEFAULT '',
    PRIMARY KEY (workspace_id, rel_path)
);"""

# CAS schema DDL
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
    """CAS 原子发布——四阶段：building → payload → raw calls → ready。

    规范：cas-gc-protocol.md §3
    修复 T-1783751461598-9e78: 加 BEGIN IMMEDIATE 事务包裹，保证四阶段原子性。
    崩溃后 building 状态残留由 GC 清理（不变量 C3）。
    """
    now = time.time()

    # BEGIN IMMEDIATE 获取写锁，保证四阶段原子性
    conn.execute("BEGIN IMMEDIATE")
    try:
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
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def cas_publish_with_retry(conn: sqlite3.Connection, cas_key: str, content_hash: str,
                           language: str, parse_result: Dict[str, Any],
                           workspace_id: int = 0, max_retries: int = 3, **kwargs):
    """带 busy retry 的 CAS 发布 + pin 包装层。

    规范：cas-gc-protocol.md §3
    修复 T-1783751474534-fd24: 原代码无 retry，遇到 database is locked 直接崩溃。
    本函数在 busy 时重试，已 ready 时只补 pin（不变量 C9）。
    """
    import time as _time

    for attempt in range(max_retries):
        try:
            cas_publish(conn, cas_key, content_hash, language, parse_result, **kwargs)
            # 发布成功后补 pin（GC 保护窗口）
            if workspace_id:
                cas_pin(conn, cas_key, workspace_id)
            return
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                _time.sleep(0.1 * (attempt + 1))
                # 检查是否已被其他进程发布为 ready
                row = conn.execute(
                    "SELECT state FROM cas_file_cache WHERE cas_key = ?", (cas_key,)
                ).fetchone()
                if row and row["state"] == "ready":
                    # 已 ready：只需补 pin
                    if workspace_id:
                        cas_pin(conn, cas_key, workspace_id)
                    return
                continue
            raise


def cas_pin(conn: sqlite3.Connection, cas_key: str, workspace_id: int,
            ttl_seconds: float = 3600):
    """添加 CAS pending ref（GC 保护窗口）。"""
    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO cas_pending_refs (cas_key, workspace_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (cas_key, workspace_id, now + ttl_seconds, now)
    )
    conn.commit()


def cas_gc(conn: sqlite3.Connection, live_keys: set, grace_period_days: float = 7,
           flock_path: str = "") -> bool:
    """唯一 GC 实现——mark-sweep。

    规范：cas-gc-protocol.md §5
    协议：LOCK_EX → BEGIN IMMEDIATE → scan manifests + pending refs → sweep → COMMIT → unlock
    修复 T-1783751468540-cdfc: 原代码无 flock 协调，GC 和 refresh 并发时有 TOCTOU 风险。
    """
    now = time.time()
    flock_fd = None
    if flock_path:
        # 获取排他锁，阻塞所有 refresh 的 LOCK_SH
        flock_fd = os.open(flock_path, os.O_CREAT | os.O_RDWR)
        _flock_exclusive(flock_fd)
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
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        if flock_fd is not None:
            _flock_unlock(flock_fd)
            os.close(flock_fd)


# ============================================
# file_generations 两阶段 CAS（daemon 侧）
# 规范：cas-gc-protocol.md §4
# 修复 T-1783751512576-caf4: 防止 stale manifest commit 和 CAS 投毒
# ============================================


def file_generation_seen(conn: sqlite3.Connection, workspace_id: int,
                          rel_path: str, session_id: str, epoch: int,
                          seq: int) -> bool:
    """第一阶段 seen：记录已看到的 generation。

    规范：cas-gc-protocol.md §4
    协议：BEGIN IMMEDIATE → 条件 UPDATE（generation < incoming 才更新）→ COMMIT
    stale seq 直接丢弃，不报错。

    Args:
        conn: CAS 数据库连接
        workspace_id: workspace ID
        rel_path: 文件相对路径
        session_id: 会话 ID
        epoch: 会话 epoch
        seq: 序列号

    Returns:
        True 如果 seen 更新成功，False 如果是 stale seq（已过期）
    """
    incoming_gen = f"{epoch}:{seq}"

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 确保 file_generations 行存在
        conn.execute(
            """INSERT OR IGNORE INTO file_generations
               (workspace_id, rel_path, latest_session_id, latest_session_epoch,
                latest_seq, latest_seen_generation, latest_committed_generation)
               VALUES (?, ?, '', 0, 0, '', '')""",
            (workspace_id, rel_path)
        )

        # 检查是否 stale
        row = conn.execute(
            "SELECT latest_seen_generation FROM file_generations WHERE workspace_id = ? AND rel_path = ?",
            (workspace_id, rel_path)
        ).fetchone()

        if row and row["latest_seen_generation"]:
            # 比较 generation：格式 "epoch:seq"
            existing = row["latest_seen_generation"]
            try:
                existing_epoch, existing_seq = existing.split(":")
                if epoch < int(existing_epoch) or (epoch == int(existing_epoch) and seq <= int(existing_seq)):
                    # stale：incoming_gen <= latest_seen
                    conn.execute("ROLLBACK")
                    return False
            except (ValueError, IndexError):
                pass  # 格式异常，允许更新

        # 更新 seen generation
        conn.execute(
            """UPDATE file_generations SET
               latest_session_id = ?, latest_session_epoch = ?,
               latest_seq = ?, latest_seen_generation = ?
               WHERE workspace_id = ? AND rel_path = ?""",
            (session_id, epoch, seq, incoming_gen, workspace_id, rel_path)
        )
        conn.execute("COMMIT")
        return True
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def file_generation_committed(conn: sqlite3.Connection, workspace_id: int,
                              rel_path: str, epoch: int, seq: int) -> bool:
    """第二阶段 committed：条件 UPDATE 确认 manifest 已提交。

    规范：cas-gc-protocol.md §4
    协议：BEGIN IMMEDIATE → manifest 提交 → 条件 UPDATE（latest_seen = incoming_gen）→ COMMIT
    stale manifest commit 被条件 UPDATE 阻止（不变量 C10）。

    Args:
        conn: CAS 数据库连接
        workspace_id: workspace ID
        rel_path: 文件相对路径
        epoch: 会话 epoch
        seq: 序列号

    Returns:
        True 如果 committed 更新成功，False 如果是 stale（其他 handler 已覆盖 seen）
    """
    incoming_gen = f"{epoch}:{seq}"

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 条件 UPDATE：只有 latest_seen_generation = incoming_gen 时才更新
        cur = conn.execute(
            """UPDATE file_generations SET latest_committed_generation = ?
               WHERE workspace_id = ? AND rel_path = ?
               AND latest_seen_generation = ?""",
            (incoming_gen, workspace_id, rel_path, incoming_gen)
        )
        if cur.rowcount != 1:
            # 0 rows：其他 handler 已覆盖 seen，stale
            conn.execute("ROLLBACK")
            return False
        conn.execute("COMMIT")
        return True
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
