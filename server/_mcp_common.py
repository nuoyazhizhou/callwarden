"""MCP 服务器公共依赖（db 单例与 daemon client），供 mcp_server.py 与 server/tools/* 复用

拆分自 server/mcp_server.py，避免 tools 模块与 mcp_server 循环导入。
"""
import os
from typing import Optional

from ..config import get_project_db_path
from ..db import CodeGraphDB


_db_instance: Optional[CodeGraphDB] = None



def _get_daemon_client():
    """获取 DaemonClient 单例（Phase 4.8: 高频查询走 daemon client）。

    延迟导入避免循环依赖。
    """
    from callwarden.server.daemon_client import get_daemon_client
    return get_daemon_client()


def _get_db_path_for_daemon() -> str:
    """获取当前 workspace 的 db_path（用于 daemon 自动发布 snapshot）。"""
    db = get_db()
    return get_project_db_path(db.workspace_root)


def get_db(workspace: Optional[str] = None) -> CodeGraphDB:
    """获取数据库单例（MCP 服务是长连接，复用连接）

    Args:
        workspace: 工作区路径或名称，为空则使用默认/活动工作区
    """
    global _db_instance
    if _db_instance is None:
        if workspace and os.path.isdir(workspace):
            _db_instance = CodeGraphDB(workspace_root=workspace)
        else:
            _db_instance = CodeGraphDB()
    elif workspace:
        # 如果指定了工作区且与当前不同，切换工作区
        current_root = _db_instance.workspace_root
        if os.path.isdir(workspace):
            ws_path = os.path.abspath(workspace)
            if os.path.abspath(current_root) != ws_path:
                _db_instance.close()
                _db_instance = CodeGraphDB(workspace_root=workspace)
        else:
            # 按名称切换
            active = _db_instance.get_active_workspace()
            if not active or active.get("name") != workspace:
                _db_instance.set_active_workspace(workspace)
    return _db_instance

