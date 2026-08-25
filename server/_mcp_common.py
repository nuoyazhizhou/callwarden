"""MCP 服务器公共依赖（daemon client 与受限配置读取），供 mcp_server.py 与 server/tools/* 复用

T03（cw-rust-client-convergence）：Python 层收敛为纯 client 薄壳。
- `get_db()` 仅保留**配置读取**能力（workspace root 解析），不再承载业务 SQL；
  业务查询/写入全部经 daemon RPC（route_rpc）。
- `_call_daemon_rpc` 增强 fail-closed：daemon 不可用抛 DaemonUnavailableError，
  绝不回退本地 SQLite/CodeGraphDB。
"""
import os
from typing import Any, Dict, Optional

from ..db import CodeGraphDB


_db_instance: Optional[CodeGraphDB] = None


def _get_daemon_client():
    """获取 DaemonClient 单例（Phase 4.8: 高频查询走 daemon client）。

    延迟导入避免循环依赖。
    """
    from callwarden.server.daemon_client import get_daemon_client
    return get_daemon_client()


def _call_daemon_rpc(method: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """通过 daemon client 发起 RPC 调用（T03：统一 fail-closed）。

    任何模式下 daemon 不可用都抛 DaemonUnavailableError，绝不回退本地
    SQLite/CodeGraphDB（设计 §8.3 fail-closed 语义）。
    """
    from callwarden.server.daemon_client import get_daemon_client, DaemonUnavailableError
    from callwarden.config import E_HTTP_DAEMON_UNAVAILABLE
    client = get_daemon_client()
    try:
        call = getattr(client, "call", None) or getattr(client, "_rpc", None).call
        return call(method, params or {})
    except DaemonUnavailableError:
        raise
    except Exception as exc:
        raise DaemonUnavailableError(
            f"{E_HTTP_DAEMON_UNAVAILABLE}: daemon RPC 调用失败 ({method}): {exc}"
        ) from exc


def _get_db_path_for_daemon() -> str:
    """经 daemon RPC 获取权威任务库路径（SRV-001：Python authority → Rust daemon）。

    收敛架构下 Python 不再本地计算 SQLite 路径；改经 daemon RPC
    `mcp.common.get_db_path_for_daemon` 取 daemon 权威任务库路径（fail-closed）。

    任何模式下 daemon 不可用都抛 DaemonUnavailableError，绝不回退本地
    SQLite/CodeGraphDB（设计 §8.3 fail-closed 语义）。
    """
    result = _call_daemon_rpc("mcp.common.get_db_path_for_daemon", {})
    if isinstance(result, dict):
        return result.get("db_path", "")
    return result or ""


def get_db(workspace: Optional[str] = None) -> CodeGraphDB:
    """获取数据库单例（T03：仅保留配置读取能力，不执行业务 SQL）。

    收敛架构下 Python 是纯 client，业务查询/写入一律经 daemon RPC。
    本函数仅用于：
    - workspace root / db_path 解析（供 daemon snapshot.publish 定位源库）；
    - daemon 未启动时的 fail-closed 诊断（不执行查询）。

    Args:
        workspace: 工作区路径或名称，为空则使用默认/活动工作区

    Raises:
        RuntimeError: 未显式指定 workspace 且当前单例没有 active workspace 时
            fail-closed。旧实现会静默复用进程级 `_db_instance`（可能是上一个项目的
            workspace 上下文），正是“工作区身份混串”的根因之一；现在禁止无身份复用。
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
    # fail-closed：未显式指定 workspace 时，单例必须已绑定 active workspace，
    # 否则拒绝复用（绝不静默沿用上一个项目的 workspace 上下文）。
    if not _db_instance.active_workspace:
        raise RuntimeError(
            "没有可用的 active workspace，拒绝复用数据库上下文（fail-closed）；"
            "请显式传入 workspace 路径/名称"
        )
    return _db_instance
