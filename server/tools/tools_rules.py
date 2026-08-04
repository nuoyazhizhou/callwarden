"""规则与只读查询面：guardrail 规则匹配与只读查询

拆分自 server/mcp_server.py（4584-4858 行区间），由 register(mcp) 注册。
"""

# 只读查询工具；写操作（register/bind/import）走 CLI 避免与 MCP 长连接撞锁

from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import get_db


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_toolchains() -> list:
        """L5: 列出所有已注册的工具链

        Returns:
            工具链摘要列表（id/name/compiler_type/version/target_triple/fingerprint）
        """
        try:
            from callwarden.db.db_toolchain import init_toolchain_schema, list_toolchains as _list
            db = get_db()
            init_toolchain_schema(db.conn)
            tcs = _list(db.conn)
            return [tc.to_dict() for tc in tcs]
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def get_toolchain(name_or_id: str) -> Optional[dict]:
        """L5: 查询工具链详情（含 include_dirs + predefined_macros）

        Args:
            name_or_id: 工具链名称或 ID
        """
        try:
            from callwarden.db.db_toolchain import init_toolchain_schema, get_toolchain as _get
            db = get_db()
            init_toolchain_schema(db.conn)
            # 尝试 int 转换
            try:
                key = int(name_or_id)
            except (ValueError, TypeError):
                key = name_or_id
            tc = _get(db.conn, key)
            return tc.to_dict() if tc else None
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_workspace_toolchains(
        workspace_id: int,
        build_context_hash: str = "",
    ) -> list:
        """L5: 查询 workspace 绑定的工具链

        Args:
            workspace_id: workspace ID
            build_context_hash: 可选过滤
        """
        try:
            from callwarden.db.db_toolchain import (
                init_toolchain_schema, get_workspace_toolchains as _get_ws,
            )
            db = get_db()
            init_toolchain_schema(db.conn)
            tcs = _get_ws(db.conn, workspace_id, build_context_hash)
            return [tc.to_dict() for tc in tcs]
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def list_build_contexts(workspace_id: int) -> list:
        """L5: 列出 workspace 的所有构建上下文（build variants）

        Args:
            workspace_id: workspace ID
        """
        try:
            from callwarden.db.db_toolchain import init_toolchain_schema, list_build_contexts as _list
            db = get_db()
            init_toolchain_schema(db.conn)
            ctxs = _list(db.conn, workspace_id)
            return [c.to_dict() for c in ctxs]
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def get_build_context(workspace_id: int, build_context_hash: str) -> Optional[dict]:
        """L5: 查询构建上下文详情（flags + defines + include_paths）

        Args:
            workspace_id: workspace ID
            build_context_hash: 构建上下文哈希
        """
        try:
            from callwarden.db.db_toolchain import init_toolchain_schema, get_build_context as _get
            db = get_db()
            init_toolchain_schema(db.conn)
            ctx = _get(db.conn, workspace_id, build_context_hash)
            return ctx.to_dict() if ctx else None
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_active_build_context(workspace_id: int) -> Optional[dict]:
        """L5: 查询当前活跃的构建上下文

        Args:
            workspace_id: workspace ID
        """
        try:
            from callwarden.db.db_toolchain import (
                init_toolchain_schema, get_active_build_context as _get_active,
            )
            db = get_db()
            init_toolchain_schema(db.conn)
            ctx = _get_active(db.conn, workspace_id)
            return ctx.to_dict() if ctx else None
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_resolved_edges(
        workspace_id: int,
        build_context_hash: str,
        caller_symbol_id: Optional[int] = None,
        limit: int = 50,
    ) -> list:
        """L5: 查询解析后的跨文件调用边（resolved_edges）

        resolved_edges 是用 build context（include 路径 + defines + toolchain）
        解析 raw_calls 后得到的具体调用目标符号。未绑定 build context 时
        返回空列表（精度降级模式）。

        Args:
            workspace_id: workspace ID
            build_context_hash: 构建上下文哈希
            caller_symbol_id: 可选，按调用方过滤
            limit: 返回数量上限
        """
        try:
            from callwarden.db.db_toolchain import init_toolchain_schema, get_resolved_edges as _get_edges
            db = get_db()
            init_toolchain_schema(db.conn)
            edges = _get_edges(
                db.conn, workspace_id, build_context_hash,
                caller_symbol_id=caller_symbol_id, limit=limit,
            )
            return [e.to_dict() for e in edges]
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def count_resolved_edges(
        workspace_id: int,
        build_context_hash: str,
    ) -> dict:
        """L5: 统计构建上下文下的 resolved_edges 数量

        Args:
            workspace_id: workspace ID
            build_context_hash: 构建上下文哈希

        Returns:
            {"count": int}
        """
        try:
            from callwarden.db.db_toolchain import init_toolchain_schema, count_resolved_edges as _count
            db = get_db()
            init_toolchain_schema(db.conn)
            n = _count(db.conn, workspace_id, build_context_hash)
            return {"count": n}
        except Exception as e:
            return {"error": str(e), "count": 0}

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
        try:
            from callwarden.server.metrics import get_metrics_collector

            # reset 仅 local 模式支持
            if reset and source not in ("local", "auto"):
                return {"error": "reset 仅 source=local 模式支持"}

            # source=auto / rpc：优先尝试 daemon RPC
            if source in ("auto", "rpc"):
                try:
                    from callwarden.server.daemon_client import UnixDaemonRpcClient
                    socket_path = os.environ.get("CW_DAEMON_SOCKET", "")
                    if not socket_path:
                        # 默认 UDS 路径
                        from callwarden.config import DAEMON_SOCKET_PATH
                        socket_path = DAEMON_SOCKET_PATH
                    client = UnixDaemonRpcClient(socket_path)
                    rpc_method = ("metrics.prometheus" if format == "prometheus"
                                  else "metrics.snapshot")
                    rpc_result = client.call(rpc_method)
                    if format == "prometheus":
                        return {"format": "prometheus", "text": rpc_result,
                                "source": "rpc"}
                    if name:
                        found = False
                        filtered: Dict[str, Any] = {
                            "timestamp": rpc_result.get("timestamp"),
                            "uptime": rpc_result.get("uptime"),
                            "name_filter": name,
                            "source": "rpc",
                            "counters": {},
                            "gauges": {},
                            "histograms": {},
                        }
                        for category in ("counters", "gauges", "histograms"):
                            cat_data = rpc_result.get(category, {})
                            if name in cat_data:
                                filtered[category] = {name: cat_data[name]}
                                found = True
                        filtered["found"] = found
                        return filtered
                    rpc_result["source"] = "rpc"
                    return rpc_result
                except Exception as rpc_exc:
                    if source == "rpc":
                        return {"error": f"daemon RPC 失败: {rpc_exc}",
                                "source": "rpc"}
                    # source=auto → 降级 local
                    # 继续走下方 local 分支
            # 本进程直读
            collector = get_metrics_collector()
            if reset:
                collector.reset()
                return {"status": "reset", "timestamp": time.time(),
                        "source": "local"}
            if format == "prometheus":
                return {"format": "prometheus", "text": collector.to_prometheus(),
                        "source": "local"}
            data = collector.to_json()
            if name:
                found = False
                filtered = {
                    "timestamp": data["timestamp"],
                    "uptime": data["uptime"],
                    "name_filter": name,
                    "source": "local",
                    "counters": {},
                    "gauges": {},
                    "histograms": {},
                }
                for category in ("counters", "gauges", "histograms"):
                    if name in data[category]:
                        filtered[category] = {name: data[category][name]}
                        found = True
                filtered["found"] = found
                return filtered
            data["source"] = "local"
            return data
        except Exception as e:
            return {"error": str(e)}
