"""cas_schema.py — CAS schema DDL 与存储操作（db/ 退休 phase-3 本地化）

原 db/db_cas.py 的全部内容搬迁至本模块：
- CAS_SCHEMA_DDL / CAS_INDEX_SQL / init_cas_schema（DDL 与初始化）
- compute_cas_key_v1 / cas_lookup / cas_pin / cas_publish / cas_publish_with_retry
  （从 replicator 重导出，replicator 已含逐字副本且为 Rust 优先版本）
- _flock_* / cas_gc / file_generation_seen / file_generation_committed
  （GC 与两阶段 generation，逐字搬迁）

本模块是 CAS 存储的单一引用面：db_cas.py 已退化为纯重导出壳。
"""
from __future__ import annotations

import sqlite3
import hashlib
import time
import os

from callwarden.server.replicator import (
    FILE_GENERATIONS_DDL,
    _compute_cas_key_v1 as compute_cas_key_v1,
    _python_cas_lookup as cas_lookup,
    _python_cas_pin as cas_pin,
    _python_cas_publish as cas_publish,
    _python_cas_publish_with_retry as cas_publish_with_retry,
)

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


# ============================================
# 跨平台 flock 支持
# 规范：cas-gc-protocol.md §3.1/§5.1
# Unix 用 fcntl.flock，Windows 用 msvcrt.locking，无两者时降级为无锁
# ============================================
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


def cas_gc(conn: sqlite3.Connection, live_keys: set, grace_period_days: float = 7,
           flock_path: str = "") -> bool:
    """唯一 GC 实现——mark-sweep。

    规范：cas-gc-protocol.md §5
    协议：LOCK_EX → BEGIN IMMEDIATE → scan manifests + pending refs → sweep → COMMIT → unlock
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
# 防止 stale manifest commit 和 CAS 投毒
# ============================================


def file_generation_seen(conn: sqlite3.Connection, workspace_id: int,
                         rel_path: str, session_id: str, epoch: int,
                         seq: int) -> bool:
    """第一阶段 seen：记录已看到的 generation。

    规范：cas-gc-protocol.md §4
    协议：BEGIN IMMEDIATE → 条件 UPDATE（generation < incoming 才更新）→ COMMIT
    stale seq 直接丢弃，不报错。
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
    stale manifest commit 被条件 UPDATE 阻止（不变量 C10）。
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
