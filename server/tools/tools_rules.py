"""规则与只读查询面：guardrail 规则匹配与只读查询

拆分自 server/mcp_server.py（4584-4858 行区间），由 register(mcp) 注册。

H4B-I（T-1786590214634-9e740cdc-h4b-index-job）：index-write/job HTTP cutover
- dispatch.rs 无任何 rules.* / security.* / index.* / job.* RPC 分支
  （DaemonStateExt 默认返回 method_not_found）。本模块 9 个工具曾存在
  `_call_daemon_rpc("rules.xxx", ...)` 伪路由，指向不存在的 RPC——HTTP 模式
  必抛 method_not_found，违反 fail-closed 契约，已全部移除。
- 本模块工具在 HTTP 模式下 fail-closed：经 _http_unsupported() 返回结构化
  unsupported 错误，不直连本地 SQLite（不构造 CodeGraphDB，无 SQLite fallback）；
  非 HTTP（legacy）模式保持本地 get_db() 执行，公开方法语义不变。
- compat_route 扩展（把 python_compat 方法注册到 H3 compat worker）由
  h4b-registry-docs（...h4b-registry-docs）承接；本任务不触碰 Rust/compat_registry。

H4C-2 第三批（T-1786747295227-49c90d68）：本模块 toolchain/edge 查询工具接入
H3 compat worker（route_worker_call + handler + 白名单 + 模块级装配）。
W3-1（T-1786861820150-bfe5e805）：list_build_contexts / get_build_context /
get_active_build_context / get_resolved_edges / count_resolved_edges 已迁移
rust_native（HTTP 模式经 daemon client 走 build_context.* RPC，主库只读 +
require_bound_workspace_id fail-closed），已从 compat 白名单移除。
S2 批次2（T-1787209948470-a59bcf9c）：list_toolchains / get_toolchain /
get_workspace_toolchains 已迁移 rust_native（读权威 task DB），白名单清空。
get_metrics 不依赖 db.conn（走 UnixDaemonRpcClient / 本地 MetricsCollector），
不属于 SQLite 只读查询面，不接入 worker，维持 fail-closed。
"""

# 只读查询工具；写操作（register/bind/import）走 CLI 避免与 MCP 长连接撞锁

import os
import time
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import _get_daemon_client, _get_db_path_for_daemon, get_db
from callwarden.config import is_http_transport_enabled
from callwarden.server.daemon_client import route_worker_call
from ..daemon_client import route_rpc as _route


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_toolchains() -> list:
        """列出所有已注册的工具链

        Returns:
            工具链摘要列表（id/name/compiler_type/version/target_triple/fingerprint）
        """
        return _route('list_toolchains', {}, 'READ_ONLY')

    @mcp.tool()
    def get_toolchain(name_or_id: str) -> Optional[dict]:
        """查询工具链详情（含 include_dirs + predefined_macros）

        Args:
            name_or_id: 工具链名称或 ID
        """
        return _route('get_toolchain', {"name_or_id": name_or_id}, 'READ_ONLY')

    @mcp.tool()
    def get_workspace_toolchains(
        workspace_id: int,
        build_context_hash: str = "",
    ) -> list:
        """查询 workspace 绑定的工具链

        Args:
            workspace_id: workspace ID
            build_context_hash: 可选过滤
        """
        return _route('get_workspace_toolchains', {"workspace_id": workspace_id, "build_context_hash": build_context_hash}, 'READ_ONLY')

    @mcp.tool()
    def list_build_contexts(workspace_id: int) -> list:
        """列出 workspace 的所有构建上下文（build variants）

        Args:
            workspace_id: workspace ID
        """
        return _route('build_context.list', {"workspace_id": workspace_id}, 'READ_ONLY')

    @mcp.tool()
    def get_build_context(workspace_id: int, build_context_hash: str) -> Optional[dict]:
        """查询构建上下文详情（flags + defines + include_paths）

        Args:
            workspace_id: workspace ID
            build_context_hash: 构建上下文哈希
        """
        return _route('build_context.get', {"workspace_id": workspace_id, "build_context_hash": build_context_hash}, 'READ_ONLY')

    @mcp.tool()
    def get_active_build_context(workspace_id: int) -> Optional[dict]:
        """查询当前活跃的构建上下文

        Args:
            workspace_id: workspace ID
        """
        return _route('build_context.active', {"workspace_id": workspace_id}, 'READ_ONLY')

    @mcp.tool()
    def get_resolved_edges(
        workspace_id: int,
        build_context_hash: str,
        caller_symbol_id: Optional[int] = None,
        limit: int = 50,
    ) -> list:
        """查询解析后的跨文件调用边（resolved_edges）

        resolved_edges 是用 build context（include 路径 + defines + toolchain）
        解析 raw_calls 后得到的具体调用目标符号。未绑定 build context 时
        返回空列表（精度降级模式）。

        Args:
            workspace_id: workspace ID
            build_context_hash: 构建上下文哈希
            caller_symbol_id: 可选，按调用方过滤
            limit: 返回数量上限
        """
        return _route('build_context.resolved_edges', {"workspace_id": workspace_id, "build_context_hash": build_context_hash, "caller_symbol_id": caller_symbol_id, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def count_resolved_edges(
        workspace_id: int,
        build_context_hash: str,
    ) -> dict:
        """统计构建上下文下的 resolved_edges 数量

        Args:
            workspace_id: workspace ID
            build_context_hash: 构建上下文哈希

        Returns:
            {"count": int}
        """
        return _route('build_context.count_resolved_edges', {"workspace_id": workspace_id, "build_context_hash": build_context_hash}, 'READ_ONLY')

    @mcp.tool()
    def get_metrics(
        format: str = "json",
        name: str = "",
        reset: bool = False,
        source: str = "auto",
    ) -> dict:
        """Phase 8: 查询 daemon 运行时指标（counters/gauges/histograms）

        G13（2026-07-20）：默认通过 daemon RPC 拉取 daemon 进程的运行时指标；
        连不上 daemon 或 source="local" 时降级本进程 MetricsCollector 单例。

        Args:
            format: 输出格式 — "json"（默认，结构化 dict）或 "prometheus"（Prometheus 文本格式，存到 "text" 字段）
            name: 仅显示指定指标名（缺省显示全部）
            reset: True 则重置所有计数器/仪表/直方图（仅 source="local" 模式支持）
            source: 指标来源 — "auto"（默认，优先 RPC 失败降级 local）/
                    "rpc"（强制 daemon RPC，失败返回 error）/
                    "local"（本进程直读）

        Returns:
            format="json": 完整指标 dict（timestamp/uptime/counters/gauges/histograms）
            format="json" + name: {"found": bool, "counters": {...}, "gauges": {...}, "histograms": {...}}
            format="prometheus": {"text": "<prometheus 文本>", "format": "prometheus"}
            reset=True: {"status": "reset", "timestamp": float}
        """
        return _route('admin.metrics_get', {"format": format, "name": name, "reset": reset, "source": source}, 'READ_ONLY')


# ============================================================
# 本模块的 compat 只读白名单（list_toolchains / get_toolchain /
# get_workspace_toolchains / list_build_contexts / get_build_context /
# get_active_build_context / get_resolved_edges / count_resolved_edges / get_metrics）
# 已全部迁移 rust_native（S2 批次2 T-1787209948470-a59bcf9c / W3-1
# T-1786861820150-bfe5e805），不再经 python_compat worker 注册，故无模块级
# register_compat_routes 调用（空白名单会触发 register_read_only_batch 的
# "methods 不能为空" fail-closed 校验）。
