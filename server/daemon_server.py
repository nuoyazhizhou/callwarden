"""Enterprise daemon server API——register/list/status。

提供 workspace 注册、查询、状态管理的 API 接口。
Phase 2: 最小骨架，实际 UDS 通信在 Phase 2 后续步骤实现。
"""

import sqlite3
from typing import Optional, List, Dict, Any

from callwarden.db.db_daemon import (
    init_daemon_schema,
    register_workspace,
    list_workspaces,
    get_workspace_status,
    update_workspace_status,
)
from callwarden.config import DAEMON_REGISTRY_DB


def get_registry_conn() -> sqlite3.Connection:
    """获取 workspace registry DB 连接。"""
    conn = sqlite3.connect(DAEMON_REGISTRY_DB)
    conn.row_factory = sqlite3.Row
    init_daemon_schema(conn)
    return conn


def api_register_workspace(owner_uid: int,
                           client_view_root: str,
                           host_real_root: str,
                           git_remote_url: str = "",
                           git_head_commit_sha: str = "",
                           toolchain_fingerprint: str = "") -> Dict[str, Any]:
    """API: 注册 workspace。"""
    conn = get_registry_conn()
    try:
        return register_workspace(
            conn, owner_uid, client_view_root, host_real_root,
            git_remote_url, git_head_commit_sha, toolchain_fingerprint
        )
    finally:
        conn.close()


def api_list_workspaces(owner_uid: Optional[int] = None) -> List[Dict[str, Any]]:
    """API: 列出 workspace。"""
    conn = get_registry_conn()
    try:
        return list_workspaces(conn, owner_uid)
    finally:
        conn.close()


def api_get_workspace_status(workspace_instance_id: str) -> Optional[Dict[str, Any]]:
    """API: 获取 workspace 状态。"""
    conn = get_registry_conn()
    try:
        return get_workspace_status(conn, workspace_instance_id)
    finally:
        conn.close()


def api_update_workspace_status(workspace_instance_id: str, status: str):
    """API: 更新 workspace 状态。"""
    conn = get_registry_conn()
    try:
        update_workspace_status(conn, workspace_instance_id, status)
    finally:
        conn.close()
