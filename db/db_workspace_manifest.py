"""Workspace manifest——clean snapshot 和 dirty overlay。

clean workspace：复用 snapshot 级 manifest
dirty workspace：独立 manifest + dirty overlay
"""

import sqlite3
import time
import hashlib
from typing import Optional, List, Dict, Any


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


def init_manifest_schema(conn: sqlite3.Connection):
    """初始化 manifest schema。"""
    conn.executescript(MANIFEST_SCHEMA_DDL)
    conn.commit()


def upsert_manifest(conn: sqlite3.Connection, workspace_id: int,
                    rel_path: str, content_hash: str, cas_key: str = "",
                    raw_hash: str = "", source_encoding: str = "utf-8",
                    bom_kind: str = "none", newline_style: str = "lf",
                    file_size: int = 0, mtime_ns: int = 0,
                    is_dirty: bool = False):
    """更新或插入 manifest 记录。"""
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
    row = conn.execute(
        "SELECT * FROM workspace_manifests WHERE workspace_id = ? AND rel_path = ?",
        (workspace_id, rel_path)
    ).fetchone()
    return dict(row) if row else None


def list_manifests(conn: sqlite3.Connection, workspace_id: int,
                   dirty_only: bool = False) -> List[Dict[str, Any]]:
    """列出 workspace 的所有 manifest。"""
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


def link_to_snapshot(conn: sqlite3.Connection, snapshot_id: str,
                    rel_path: str, content_hash: str, cas_key: str = ""):
    """将文件链接到 snapshot（clean workspace 复用）。"""
    conn.execute(
        "INSERT OR REPLACE INTO workspace_snapshot_map (snapshot_id, rel_path, content_hash, cas_key) VALUES (?, ?, ?, ?)",
        (snapshot_id, rel_path, content_hash, cas_key)
    )
    conn.commit()


def get_snapshot_files(conn: sqlite3.Connection, snapshot_id: str) -> List[Dict[str, Any]]:
    """获取 snapshot 的所有文件。"""
    rows = conn.execute(
        "SELECT * FROM workspace_snapshot_map WHERE snapshot_id = ?",
        (snapshot_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def verify_raw_hash(conn: sqlite3.Connection, workspace_id: int,
                    rel_path: str, expected_raw_hash: str) -> bool:
    """校验磁盘文件是否与 manifest 中记录的 raw_hash 一致。"""
    manifest = get_manifest(conn, workspace_id, rel_path)
    if not manifest:
        return False
    return manifest.get("raw_hash") == expected_raw_hash
