"""Workspace manifest——clean snapshot 和 dirty overlay。

clean workspace：复用 snapshot 级 manifest
dirty workspace：独立 manifest + dirty overlay
"""

import sqlite3
import time
import hashlib
from typing import Optional, List, Dict, Any


_RUST_MANIFEST_FEATURE = "rust_manifest_query"
_RUST_UNAVAILABLE = object()


MANIFEST_SCHEMA_DDL = """
-- workspace manifests 表
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

-- snapshot 映射表（clean workspace 复用）
CREATE TABLE IF NOT EXISTS workspace_snapshot_map (
    snapshot_id TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    cas_key TEXT,
    PRIMARY KEY (snapshot_id, rel_path)
);

CREATE INDEX IF NOT EXISTS idx_manifests_hash ON workspace_manifests(content_hash);
CREATE INDEX IF NOT EXISTS idx_manifests_cas ON workspace_manifests(cas_key);
CREATE INDEX IF NOT EXISTS idx_manifests_dirty ON workspace_manifests(workspace_id, is_dirty);
"""


def _connection_db_path(conn: sqlite3.Connection) -> Optional[str]:
    """返回 main 数据库文件路径；内存数据库和异常连接返回 None。"""
    try:
        row = conn.execute(
            "PRAGMA database_list"
        ).fetchall()
    except sqlite3.Error:
        return None
    for item in row:
        if len(item) >= 3 and item[1] == "main" and item[2]:
            return str(item[2])
    return None


def _rust_manifest_module(conn: sqlite3.Connection):
    """按 rollback_config 决定是否启用 Rust manifest 查询 facade。"""
    db_path = _connection_db_path(conn)
    if not db_path:
        return None
    try:
        row = conn.execute(
            "SELECT rollback_flag FROM rollback_config "
            "WHERE feature_name = ? ORDER BY updated_at DESC LIMIT 1",
            (_RUST_MANIFEST_FEATURE,),
        ).fetchone()
        if row and int(row[0]) == 1:
            return None
    except sqlite3.Error:
        # 旧数据库可能还没有 rollback_config，默认允许已安装的 Rust facade。
        pass
    try:
        import callwarden_core
    except ImportError:
        return None
    return callwarden_core, db_path


def _rust_manifest_call(conn: sqlite3.Connection, function_name: str, *args):
    """调用 Rust manifest 查询；扩展未安装或调用失败时返回 sentinel。"""
    target = _rust_manifest_module(conn)
    if target is None:
        return _RUST_UNAVAILABLE
    module, db_path = target
    try:
        function = getattr(module, function_name)
        return function(db_path, *args)
    except Exception:
        # 查询 facade 失败时回退原 SQL，兼容旧 schema 和旧 wheel。
        return _RUST_UNAVAILABLE


def _rust_manifest_write_call(conn: sqlite3.Connection, function_name: str, *args):
    """调用 Rust manifest 写 facade。

    只有扩展未安装、旧扩展没有该符号或 rollback 开关开启时才返回
    ``_RUST_UNAVAILABLE``。Rust 已被选为生产写路径后，真实 SQL/事务错误必须
    直接抛出，不能静默回退到 Python 造成双写语义和可观测性缺失。
    """
    target = _rust_manifest_module(conn)
    if target is None:
        return _RUST_UNAVAILABLE
    module, db_path = target
    function = getattr(module, function_name, None)
    if function is None:
        # 兼容尚未带写 facade 的旧 wheel；下一次安装新扩展后自动切换。
        return _RUST_UNAVAILABLE
    return function(db_path, *args)


def init_manifest_schema(conn: sqlite3.Connection):
    """初始化 manifest schema。"""
    rust_result = _rust_manifest_write_call(conn, "manifest_init_schema")
    if rust_result is not _RUST_UNAVAILABLE:
        return
    conn.executescript(MANIFEST_SCHEMA_DDL)
    conn.commit()


def upsert_manifest(conn: sqlite3.Connection, workspace_id: int,
                    rel_path: str, content_hash: str, cas_key: str = "",
                    raw_hash: str = "", source_encoding: str = "utf-8",
                    bom_kind: str = "none", newline_style: str = "lf",
                    file_size: int = 0, mtime_ns: int = 0,
                    is_dirty: bool = False):
    """更新或插入 manifest 记录。"""
    rust_result = _rust_manifest_write_call(
        conn,
        "manifest_upsert",
        workspace_id,
        rel_path,
        content_hash,
        cas_key,
        raw_hash,
        source_encoding,
        bom_kind,
        newline_style,
        file_size,
        mtime_ns,
        bool(is_dirty),
    )
    if rust_result is not _RUST_UNAVAILABLE:
        return
    now = time.time()
    conn.execute(
        """INSERT OR REPLACE INTO workspace_manifests
           (workspace_id, rel_path, content_hash, cas_key, raw_hash,
            source_encoding, bom_kind, newline_style, file_size, mtime_ns,
            is_dirty, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (workspace_id, rel_path, content_hash, cas_key, raw_hash,
         source_encoding, bom_kind, newline_style, file_size, mtime_ns,
         int(is_dirty), now)
    )
    conn.commit()


def get_manifest(conn: sqlite3.Connection, workspace_id: int,
                 rel_path: str) -> Optional[Dict[str, Any]]:
    """获取单个文件 manifest。"""
    rust_result = _rust_manifest_call(conn, "manifest_get", workspace_id, rel_path)
    if rust_result is not _RUST_UNAVAILABLE:
        return dict(rust_result) if rust_result is not None else None
    row = conn.execute(
        "SELECT * FROM workspace_manifests WHERE workspace_id = ? AND rel_path = ?",
        (workspace_id, rel_path)
    ).fetchone()
    return dict(row) if row else None


def list_manifests(conn: sqlite3.Connection, workspace_id: int,
                   dirty_only: bool = False) -> List[Dict[str, Any]]:
    """列出 workspace 的所有 manifest。"""
    rust_result = _rust_manifest_call(
        conn, "manifest_list", workspace_id, dirty_only
    )
    if rust_result is not _RUST_UNAVAILABLE:
        return [dict(row) for row in rust_result]
    if dirty_only:
        rows = conn.execute(
            "SELECT * FROM workspace_manifests WHERE workspace_id = ? AND is_dirty = 1",
            (workspace_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM workspace_manifests WHERE workspace_id = ?",
            (workspace_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def count_manifests(conn: sqlite3.Connection, workspace_id: int,
                    dirty_only: bool = False) -> int:
    """统计 manifest 行数，优先走 Rust COUNT 查询。"""
    rust_result = _rust_manifest_call(
        conn, "manifest_count", workspace_id, dirty_only
    )
    if rust_result is not _RUST_UNAVAILABLE:
        return int(rust_result)
    if dirty_only:
        row = conn.execute(
            "SELECT COUNT(*) FROM workspace_manifests "
            "WHERE workspace_id = ? AND is_dirty = 1",
            (workspace_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM workspace_manifests WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
    return int(row[0])


def link_to_snapshot(conn: sqlite3.Connection, snapshot_id: str,
                    rel_path: str, content_hash: str, cas_key: str = ""):
    """将文件链接到 snapshot（clean workspace 复用）。"""
    rust_result = _rust_manifest_write_call(
        conn,
        "manifest_link_to_snapshot",
        snapshot_id,
        rel_path,
        content_hash,
        cas_key,
    )
    if rust_result is not _RUST_UNAVAILABLE:
        return
    conn.execute(
        "INSERT OR REPLACE INTO workspace_snapshot_map (snapshot_id, rel_path, content_hash, cas_key) VALUES (?, ?, ?, ?)",
        (snapshot_id, rel_path, content_hash, cas_key)
    )
    conn.commit()


def get_snapshot_files(conn: sqlite3.Connection, snapshot_id: str) -> List[Dict[str, Any]]:
    """获取 snapshot 的所有文件。"""
    rust_result = _rust_manifest_call(conn, "snapshot_get_files", snapshot_id)
    if rust_result is not _RUST_UNAVAILABLE:
        return [dict(row) for row in rust_result]
    rows = conn.execute(
        "SELECT * FROM workspace_snapshot_map WHERE snapshot_id = ?",
        (snapshot_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def verify_raw_hash(conn: sqlite3.Connection, workspace_id: int,
                    rel_path: str, expected_raw_hash: str) -> bool:
    """校验磁盘文件是否与 manifest 中记录的 raw_hash 一致。"""
    rust_result = _rust_manifest_call(
        conn, "manifest_verify_raw_hash", workspace_id, rel_path, expected_raw_hash
    )
    if rust_result is not _RUST_UNAVAILABLE:
        return bool(rust_result)
    manifest = get_manifest(conn, workspace_id, rel_path)
    if not manifest:
        return False
    return manifest.get("raw_hash") == expected_raw_hash
