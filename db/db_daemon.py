"""db_daemon.py —— db/ 退休过渡期重导出壳

db/ 退休 phase-3：workspace registry 数据库层已整体搬迁至
``callwarden.server.daemon_registry``。本模块仅保留重导出，供仍在过渡期
引用 ``callwarden.db.db_daemon`` 的代码（主要是测试）使用；新代码应直接
``from callwarden.server.daemon_registry import ...``。
"""
from __future__ import annotations

from callwarden.server.daemon_registry import (  # noqa: F401
    WORKSPACE_REGISTRY_DDL,
    CREATE_INDEX_SQL,
    init_daemon_schema,
    register_workspace,
    list_workspaces,
    get_workspace_status,
    update_workspace_status,
    register_mount_mapping,
    list_mount_mappings,
    delete_mount_mapping,
)
