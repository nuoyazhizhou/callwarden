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
require_bound_workspace_id fail-closed），已从 compat 白名单移除；剩
list_toolchains / get_toolchain / get_workspace_toolchains 3 项保持 python_compat。
get_metrics 不依赖 db.conn（走 UnixDaemonRpcClient / 本地 MetricsCollector），
不属于 SQLite 只读查询面，不接入 worker，维持 fail-closed。
"""

# 只读查询工具；写操作（register/bind/import）走 CLI 避免与 MCP 长连接撞锁

import os
import time
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import _get_daemon_client, _get_db_path_for_daemon, get_db
from ...db import CodeGraphDB
from callwarden.config import is_http_transport_enabled
from callwarden.server.daemon_client import route_worker_call

# H4C-2 第三批（T-1786747295227-49c90d68）：rules 组只读工具接入 compat worker。
# 注意：必须用顶层 `server.compat_registry` 导入，与 compat_worker.py 保持同一
# 模块单例（模块单例风险，见 tools_query.py L41-49 注释）。
from server.compat_registry import (  # noqa: E402
    SCOPE_WORKSPACE,
    CompatCallContext,
    register_compat_routes,
)

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
# H4C-2 第三批（T-1786747295227-49c90d68）：toolchain/edge 只读查询工具
# worker handler（index 组，步骤#1）
# ============================================================
# 接入说明（遵循 tools_summary.py 已收口模式）：
# - handler 定义在工具模块内，由 compat_worker.handle_frame 通用派发按 registry
#   分发；本模块被 worker 装配 import 后模块级注册随之执行；
# - db_toolchain 查询函数均为纯 SELECT（只读），直接用 ctx.conn（worker 的
#   mode=ro 只读连接）调用；
# - handler 跳过 init_toolchain_schema（executescript CREATE TABLE + commit，
#   写操作，worker 只读连接无法承载；表已由本地/daemon 初始化过，见
#   db_toolchain.open_toolchain_db 与工具函数体 _local 注释）；
# - get_metrics 不依赖 db.conn（走 UnixDaemonRpcClient / 本地 MetricsCollector），
#   不属于 SQLite 只读查询面，不接入 worker，维持 fail-closed（矩阵标注同步修正）。
_RULES_COMPAT_SCOPE = SCOPE_WORKSPACE  # 矩阵 workspace_scoped


def _bind_readonly_db(ctx: CompatCallContext) -> CodeGraphDB:
    """轻量只读绑定：绕过 CodeGraphDB.__init__，注入 worker 只读连接与显式 workspace。

    与 tools_query.py / tools_summary.py 同款：ctx.conn 由 compat_worker 用
    `file:{db_path}?mode=ro` 打开（read_only 契约）；active_workspace 注入
    ctx.workspace_id，db 层查询基于 `_get_active_workspace_id()` 过滤；
    workspace_root 从 workspaces 表解析。
    """
    db = object.__new__(CodeGraphDB)
    db.conn = ctx.conn
    db.active_workspace = {"id": ctx.workspace_id} if ctx.workspace_id else None
    db.workspace_root = None
    if ctx.workspace_id is not None:
        try:
            row = ctx.conn.execute(
                "SELECT root_path FROM workspaces WHERE id = ?",
                (ctx.workspace_id,),
            ).fetchone()
            if row is not None:
                db.workspace_root = row["root_path"]
        except Exception:
            db.workspace_root = None
    return db


def _h_list_toolchains(ctx: CompatCallContext) -> Any:
    """worker handler：列出所有工具链（只读，跳过 init_toolchain_schema 写操作）"""
    from callwarden.db.db_toolchain import list_toolchains as _list
    return [tc.to_dict() for tc in _list(ctx.conn)]


def _h_get_toolchain(ctx: CompatCallContext) -> Any:
    """worker handler：查询工具链详情（只读，跳过 init_toolchain_schema）"""
    from callwarden.db.db_toolchain import get_toolchain as _get
    name_or_id = ctx.params.get("name_or_id", "")
    try:
        key = int(name_or_id)
    except (ValueError, TypeError):
        key = name_or_id
    tc = _get(ctx.conn, key)
    return tc.to_dict() if tc else None


def _h_get_workspace_toolchains(ctx: CompatCallContext) -> Any:
    """worker handler：查询 workspace 绑定的工具链（只读，跳过 init_toolchain_schema）"""
    from callwarden.db.db_toolchain import get_workspace_toolchains as _get_ws
    tcs = _get_ws(
        ctx.conn,
        ctx.params.get("workspace_id", 0),
        ctx.params.get("build_context_hash", ""),
    )
    return [tc.to_dict() for tc in tcs]


# toolchain/edge 只读白名单（3 个）：list_build_contexts / get_build_context /
# get_active_build_context / get_resolved_edges / count_resolved_edges 已 W3-1
# （T-1786861820150-bfe5e805）迁移 rust_native 并从白名单移除。get_metrics 不依赖
# db.conn（走 daemon RPC / 本地 MetricsCollector），不接入 worker，维持 fail-closed。
_RULES_READ_ONLY_METHODS: Dict[str, Any] = {
    "list_toolchains": _h_list_toolchains,
    "get_toolchain": _h_get_toolchain,
    "get_workspace_toolchains": _h_get_workspace_toolchains,
}

# 模块级注册：worker 装配 import 本模块时执行，注册到 compat_registry 单例并
# 同步 RUST_COMPAT_ROUTE（Rust 侧 http_server.rs 白名单在步骤#2 同步）。
register_compat_routes(
    _RULES_READ_ONLY_METHODS,
    workspace_scope=_RULES_COMPAT_SCOPE,
    description="H4C-2 第三批 toolchain 组只读工具（3 个，T-1786747295227-49c90d68 步骤#1；"
    "build_context 5 个已 W3-1 T-1786861820150-bfe5e805 迁移 rust_native）",
)
