"""Enterprise daemon workspace registry 数据库层。

提供 workspace 注册、查询、状态管理功能。
Phase 2: 最小骨架，后续 Phase 3 接入 CAS。
"""

import sqlite3
import time
import hashlib
import os
from typing import Optional, List, Dict, Any


# workspace registry schema DDL
WORKSPACE_REGISTRY_DDL = """
-- workspace 注册表
CREATE TABLE IF NOT EXISTS daemon_workspaces (
    workspace_id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_instance_id TEXT NOT NULL UNIQUE,  -- sha256(owner_uid + host_real_root + git_remote + git_head + ...)
    snapshot_id TEXT,                              -- 可跨用户共享的代码快照身份
    owner_uid INTEGER NOT NULL,                   -- 哪个用户的 workspace
    git_remote_url TEXT DEFAULT '',
    git_head_commit_sha TEXT DEFAULT '',
    client_view_root TEXT NOT NULL,               -- 客户端看到的路径（如 Z:\\work\\firmware）
    host_real_root TEXT NOT NULL,                 -- 宿主机真实根目录（realpath 解析后）
    toolchain_fingerprint TEXT DEFAULT '',
    registered_at REAL NOT NULL,
    last_active_at REAL NOT NULL,
    status TEXT DEFAULT 'active'                  -- active / inactive / archived
);

-- container mount mapping
CREATE TABLE IF NOT EXISTS container_mount_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container_id TEXT NOT NULL,                   -- 容器标识
    container_path TEXT NOT NULL,                 -- 容器内路径
    host_path TEXT NOT NULL,                      -- 宿主机路径
    mapping_type TEXT DEFAULT 'bind',             -- bind / volume / smb
    UNIQUE(container_id, container_path)
);

-- daemon 状态表
CREATE TABLE IF NOT EXISTS daemon_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_workspaces_owner ON daemon_workspaces(owner_uid);
CREATE INDEX IF NOT EXISTS idx_workspaces_snapshot ON daemon_workspaces(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_workspaces_status ON daemon_workspaces(status);
"""


def init_daemon_schema(conn: sqlite3.Connection):
    """初始化 daemon workspace registry schema。"""
    conn.executescript(WORKSPACE_REGISTRY_DDL)
    conn.executescript(CREATE_INDEX_SQL)
    conn.commit()


def register_workspace(conn: sqlite3.Connection,
                       owner_uid: int,
                       client_view_root: str,
                       host_real_root: str,
                       git_remote_url: str = "",
                       git_head_commit_sha: str = "",
                       toolchain_fingerprint: str = "") -> Dict[str, Any]:
    """注册一个 workspace，返回 workspace 信息。"""
    workspace_instance_id = hashlib.sha256(
        f"{owner_uid}|{host_real_root}|{git_remote_url}|{git_head_commit_sha}".encode()
    ).hexdigest()[:16]

    now = time.time()
    snapshot_id = None
    if git_remote_url and git_head_commit_sha:
        snapshot_id = hashlib.sha256(
            f"{git_remote_url}|{git_head_commit_sha}|{toolchain_fingerprint}".encode()
        ).hexdigest()[:16]

    conn.execute(
        """INSERT OR REPLACE INTO daemon_workspaces
           (workspace_instance_id, snapshot_id, owner_uid, git_remote_url,
            git_head_commit_sha, client_view_root, host_real_root,
            toolchain_fingerprint, registered_at, last_active_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
        (workspace_instance_id, snapshot_id, owner_uid, git_remote_url,
         git_head_commit_sha, client_view_root, host_real_root,
         toolchain_fingerprint, now, now)
    )
    conn.commit()

    row = conn.execute(
        "SELECT * FROM daemon_workspaces WHERE workspace_instance_id = ?",
        (workspace_instance_id,)
    ).fetchone()
    return dict(row) if row else {}


def list_workspaces(conn: sqlite3.Connection,
                    owner_uid: Optional[int] = None) -> List[Dict[str, Any]]:
    """列出 workspace，可按 owner_uid 过滤。"""
    if owner_uid is not None:
        rows = conn.execute(
            "SELECT * FROM daemon_workspaces WHERE owner_uid = ? ORDER BY last_active_at DESC",
            (owner_uid,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM daemon_workspaces ORDER BY last_active_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_workspace_status(conn: sqlite3.Connection,
                         workspace_instance_id: str) -> Optional[Dict[str, Any]]:
    """获取 workspace 状态。"""
    row = conn.execute(
        "SELECT * FROM daemon_workspaces WHERE workspace_instance_id = ?",
        (workspace_instance_id,)
    ).fetchone()
    return dict(row) if row else None


def update_workspace_status(conn: sqlite3.Connection,
                            workspace_instance_id: str,
                            status: str):
    """更新 workspace 状态。"""
    now = time.time()
    conn.execute(
        "UPDATE daemon_workspaces SET status = ?, last_active_at = ? WHERE workspace_instance_id = ?",
        (status, now, workspace_instance_id)
    )
    conn.commit()


# ============================================
# G4: Container Mount Mapping CRUD
# ============================================

# 允许的 mapping_type 取值（与 Rust workspace.rs handle_mount_register 一致）
_VALID_MOUNT_TYPES = ("bind", "volume", "smb")


def register_mount_mapping(conn: sqlite3.Connection,
                           container_id: str,
                           container_path: str,
                           host_path: str,
                           mapping_type: str = "bind") -> Dict[str, Any]:
    """注册容器挂载映射（INSERT OR REPLACE，UNIQUE(container_id, container_path) 约束）。

    同一 (container_id, container_path) 二次调用会替换 host_path 和 mapping_type。

    Args:
        container_id: 容器标识（如 "ubuntu_2204"）
        container_path: 容器内路径前缀（如 "/home/user1"）
        host_path: 宿主机真实路径（如 "/data/volumes/user1"）
        mapping_type: bind / volume / smb（默认 bind）

    Returns:
        新插入或替换后的映射记录（dict）
    """
    if mapping_type not in _VALID_MOUNT_TYPES:
        raise ValueError(
            f"mapping_type 必须是 { _VALID_MOUNT_TYPES }，得到: {mapping_type}"
        )
    conn.execute(
        """INSERT OR REPLACE INTO container_mount_mappings
           (container_id, container_path, host_path, mapping_type)
           VALUES (?, ?, ?, ?)""",
        (container_id, container_path, host_path, mapping_type)
    )
    conn.commit()
    row = conn.execute(
        """SELECT id, container_id, container_path, host_path, mapping_type
           FROM container_mount_mappings
           WHERE container_id = ? AND container_path = ?""",
        (container_id, container_path)
    ).fetchone()
    return dict(row) if row else {}


def list_mount_mappings(conn: sqlite3.Connection,
                       container_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出容器挂载映射。

    Args:
        container_id: 缺省则列出全部，否则按 container_id 过滤

    Returns:
        list[dict]，按 id 升序
    """
    if container_id is not None:
        rows = conn.execute(
            """SELECT id, container_id, container_path, host_path, mapping_type
               FROM container_mount_mappings
               WHERE container_id = ?
               ORDER BY id ASC""",
            (container_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, container_id, container_path, host_path, mapping_type
               FROM container_mount_mappings
               ORDER BY id ASC"""
        ).fetchall()
    return [dict(r) for r in rows]


def delete_mount_mapping(conn: sqlite3.Connection,
                        container_id: str,
                        container_path: str) -> int:
    """删除容器挂载映射。

    Args:
        container_id: 容器标识
        container_path: 容器内路径

    Returns:
        删除的行数（0 表示不存在，1 表示已删除）
    """
    cur = conn.execute(
        """DELETE FROM container_mount_mappings
           WHERE container_id = ? AND container_path = ?""",
        (container_id, container_path)
    )
    conn.commit()
    return cur.rowcount
