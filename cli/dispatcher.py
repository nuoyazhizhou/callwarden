"""CliDispatcher —— CLI 纯 client 化路由层（T04 收敛）。

设计契约（cw-rust-client-convergence-design.md §3.3）：
- CLI 业务子命令 = 「解析 → call_daemon() → 格式化输出」；
- fail-closed：daemon 不可用抛 DaemonUnavailableError(E_HTTP_DAEMON_UNAVAILABLE)，
  绝不降级本地 SQLite；
- local/legacy 仅 CW_TEST_MODE=1 下可用（E_MODE_DEPRECATED）。

用法（新式子命令接入）：
    from callwarden.cli.dispatcher import call_daemon, DaemonUnavailableError
    result = call_daemon("query.stats", {})

异常类型统一（QA CW-1 反馈）：`DaemonUnavailableError` 直接 re-export
`server.daemon_client.DaemonUnavailableError`（唯一实现，避免双类漂移；
调用方 `except DaemonUnavailableError` 与 MCP/CLI 薄壳一致）。
"""
from __future__ import annotations

from typing import Any, Dict

from callwarden.server.daemon_client import DaemonUnavailableError

__all__ = ["DaemonUnavailableError", "call_daemon", "CliDispatcher"]


def call_daemon(method: str, params: Dict[str, Any], op_class: str = "READ_ONLY") -> Any:
    """经 daemon RPC 执行业务调用（统一薄壳路由）。

    Args:
        method: daemon RPC method（矩阵 rpc_method）。
        params: 命令参数（原样透传）。
        op_class: READ_ONLY / PROTECTED_MUTATION / GOVERNANCE_WRITE。

    Returns:
        daemon 响应的 result 字段。

    Raises:
        DaemonUnavailableError: daemon 不可用（fail-closed，不回退本地 SQLite）。
    """
    from callwarden.server.daemon_client import route_rpc

    return route_rpc(method, params, op_class)


class CliDispatcher:
    """CLI 子命令分发器（设计 §3.3 CliDispatcher）。

    业务子命令注册 `method` 映射后统一走 daemon；daemon 管理/自举例外
    （serve/start/stop/status/ping/health/install/doctor/config）保持本地
    （Q2 例外清单）。
    """

    def __init__(self) -> None:
        self._routes: Dict[str, str] = {}

    def register(self, subcommand: str, method: str) -> None:
        """注册子命令 → daemon RPC method 映射。"""
        self._routes[subcommand] = method

    def method_for(self, subcommand: str) -> str:
        return self._routes.get(subcommand, "")

    def dispatch(self, subcommand: str, params: Dict[str, Any],
                 op_class: str = "READ_ONLY") -> Any:
        """按子命令分发到 daemon RPC（未注册子命令抛 KeyError）。"""
        method = self._routes.get(subcommand)
        if not method:
            raise KeyError(f"子命令 {subcommand} 未注册 daemon RPC 路由")
        return call_daemon(method, params, op_class)
