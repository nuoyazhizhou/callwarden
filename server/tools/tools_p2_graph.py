"""P2 依赖图与环检测工具（Req 9.1-9.10）

拆分自 server/mcp_server.py（5138-5328 行区间），由 register(mcp) 注册。

H4B-E（T-1786590214634-9e740cdc-h4b-unsupported-error）：governance/unsupported/error cutover
- dispatch.rs 无任何 p2.* RPC 分支（DaemonStateExt 默认 method_not_found）。
  本模块 10 个工具曾存在 `_call_daemon_rpc("p2.xxx", ...)` 伪路由，指向不存在的
  RPC——HTTP 模式必抛 method_not_found，违反 fail-closed 契约，已全部移除。
- 本模块工具在 HTTP 模式下 fail-closed：经 _http_unsupported() 返回结构化
  unsupported 错误，不直连本地 SQLite（不构造 CodeGraphDB，无 SQLite fallback）；
  非 HTTP（legacy）模式保持本地 get_db() 执行，公开方法语义不变。
"""

# P2: 依赖图与环检测工具（Req 9.1-9.10, 13.7-13.8）

from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import get_db
from ...db import CodeGraphDB
from callwarden.server.daemon_client import route_worker_call

# H4C-2 第三批（T-1786747295227-b876fddf）：p2 依赖图/环检测只读工具接入
# compat worker。注意：必须用顶层 `server.compat_registry` 导入，与
# compat_worker.py 保持同一模块单例（模块单例风险，见 tools_query.py L41-49 注释）。
from server.compat_registry import (  # noqa: E402
    SCOPE_WORKSPACE,
    CompatCallContext,
    register_compat_routes,
)

from ..daemon_client import route_rpc as _route


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def import_envelope_dependencies(
        workspace_id: int,
        task_id: str,
        contract_id: str,
        contract_revision: int,
        dependencies: list,
    ) -> dict:
        """从 Envelope 导入四类依赖声明（Req 9.1）。

        依赖类型：requires_existing / requires_artifact /
        provides_interface / requires_interface。

        Args:
            workspace_id: 工作区 ID
            task_id: 声明依赖的任务 ID
            contract_id: 契约 ID
            contract_revision: 契约 revision
            dependencies: 依赖列表，每项含 dependency_type/target_ref/target_task_id/is_informational

        Returns:
            {"imported": int, "skipped": int, "errors": list}
        """
        _res = _route('task.job_submit', {**{"workspace_id": workspace_id, "task_id": task_id, "contract_id": contract_id, "contract_revision": contract_revision, "dependencies": dependencies}, "job_type": "envelope_deps", "sync": True}, 'PROTECTED_MUTATION')
        return _res.get("result") if isinstance(_res, dict) and "result" in _res else _res

    @mcp.tool()
    def record_artifact_identity(
        workspace_id: int,
        task_id: str,
        contract_id: str,
        contract_revision: int,
        artifact_type: str,
        artifact_ref: str,
        artifact_hash: str = "",
        workspace_snapshot_id: str = "",
    ) -> str:
        """记录 artifact identity（provider 产出 artifact 时调用，Req 9.3）。

        Args:
            artifact_type: file/symbol/resource
            artifact_ref: 文件路径或符号限定名
            artifact_hash: artifact 内容摘要（sha256:...），非空时 freshness=fresh
            workspace_snapshot_id: 产出时绑定的工作区快照

        Returns:
            artifact_id（ART-<uuid>）
        """
        return _route('admin.record_artifact_identity', {"workspace_id": workspace_id, "task_id": task_id, "contract_id": contract_id, "contract_revision": contract_revision, "artifact_type": artifact_type, "artifact_ref": artifact_ref, "artifact_hash": artifact_hash, "workspace_snapshot_id": workspace_snapshot_id}, 'GOVERNANCE_WRITE')

    @mcp.tool()
    def get_artifact_freshness(
        workspace_id: int,
        task_id: str,
        artifact_ref: str = "",
    ) -> Optional[dict]:
        """查询 artifact freshness 状态（Req 9.3，Gate 判定用）。

        Returns:
            {"artifact_id", "freshness_status", "artifact_hash", "produced_at"} 或 None
        """
        return _route('get_artifact_freshness', {"workspace_id": workspace_id, "task_id": task_id, "artifact_ref": artifact_ref}, 'READ_ONLY')

    @mcp.tool()
    def publish_interface(
        workspace_id: int,
        task_id: str,
        contract_id: str,
        contract_revision: int,
        interface_name: str,
        version: str,
        interface_hash: str = "",
    ) -> str:
        """发布 interface identity（provider 声明 provides_interface，Req 9.4）。

        Returns:
            interface_id（IF-<uuid>）
        """
        return _route('admin.publish_interface', {"workspace_id": workspace_id, "task_id": task_id, "contract_id": contract_id, "contract_revision": contract_revision, "interface_name": interface_name, "version": version, "interface_hash": interface_hash}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def get_interface_providers(
        workspace_id: int,
        interface_name: str,
        version: str = "",
    ) -> list:
        """查询匹配的 interface provider 列表（Req 9.5, 9.9）。

        Returns:
            provider 列表，每项含 interface_id/provider_task_id/version/hash
        """
        return _route('get_interface_providers', {"workspace_id": workspace_id, "interface_name": interface_name, "version": version}, 'READ_ONLY')

    @mcp.tool()
    def select_interface_provider(
        workspace_id: int,
        consumer_task_id: str,
        contract_id: str,
        contract_revision: int,
        interface_name: str,
        selected_provider_task_id: str,
    ) -> dict:
        """记录 Planner 的显式 provider 选择（Req 9.9）。

        Returns:
            {"success": bool, "error": str}
        """
        return _route('admin.select_interface_provider', {"workspace_id": workspace_id, "consumer_task_id": consumer_task_id, "contract_id": contract_id, "contract_revision": contract_revision, "interface_name": interface_name, "selected_provider_task_id": selected_provider_task_id}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def build_hard_dependency_edges(
        workspace_id: int,
        contract_id: str,
        contract_revision: int,
    ) -> dict:
        """为指定契约 revision 构建硬依赖图边（Req 9.6）。

        边方向 provider→consumer，去重后用于环检测。

        Returns:
            {"edges_built": int, "edges_skipped": int}
        """
        _res = _route('task.job_submit', {**{"workspace_id": workspace_id, "contract_id": contract_id, "contract_revision": contract_revision}, "job_type": "hard_dep_edges", "sync": True}, 'PROTECTED_MUTATION')
        return _res.get("result") if isinstance(_res, dict) and "result" in _res else _res

    @mcp.tool()
    def detect_cycle(workspace_id: int) -> dict:
        """检测硬依赖图中的环，返回最小 cycle path（Req 9.7）。

        只做无环校验和诊断，不提供自动排程/assignment/抢占（Req 9.10）。

        Returns:
            {"has_cycle": bool, "cycle_path": list}
        """
        return _route('detect_cycle', {"workspace_id": workspace_id}, 'READ_ONLY')

    @mcp.tool()
    def validate_revision_dependencies(
        workspace_id: int,
        contract_id: str,
        contract_revision: int,
    ) -> dict:
        """验证指定 Revision 的依赖完整性（Req 9.7, 9.9）。

        验证内容：硬依赖图无环 + 多 provider 有显式选择。
        revision 有环时原子拒绝。

        Returns:
            {"valid": bool, "errors": list, "cycle_path": list}
        """
        return _route('validate_revision_dependencies', {"workspace_id": workspace_id, "contract_id": contract_id, "contract_revision": contract_revision}, 'READ_ONLY')

    @mcp.tool()
    def get_dependency_edges(
        workspace_id: int,
        task_id: str = "",
    ) -> list:
        """查询硬依赖图边（Req 9.6，诊断用）。

        Args:
            task_id: 可选，按任务 ID 过滤（provider 或 consumer 匹配）

        Returns:
            依赖边列表，每项含 provider_task_id/consumer_task_id/edge_type/source_type/is_hard
        """
        return _route('get_dependency_edges', {"workspace_id": workspace_id, "task_id": task_id}, 'READ_ONLY')


# ---------------------------------------------------------------------------
# H4C-2 第三批（T-1786747295227-b876fddf）：p2 只读工具 compat worker 装配
# ---------------------------------------------------------------------------
# - 接入范围：get_artifact_freshness / get_interface_providers / detect_cycle /
#   validate_revision_dependencies / get_dependency_edges（5 个只读工具）。
#   写语义工具（import_envelope_dependencies / record_artifact_identity /
#   publish_interface / select_interface_provider / build_hard_dependency_edges，
#   governance_write）不接入 worker，维持 _http_unsupported fail-closed。
# - 轻量只读绑定：object.__new__(CodeGraphDB) 绕过 __init__（含 PRAGMA WAL /
#   schema 迁移 / workspace 注册等写副作用），注入 ctx.conn（worker 的
#   mode=ro 只读连接）+ ctx.workspace_id 后复用 db 层查询方法。
# - validate_revision_dependencies 的 db 层实现内部调用 build_hard_dependency_edges
#   （含 INSERT dependency_edges + commit 写操作），handler 实现只读等价：
#   内存模拟构建硬边（不写库）+ 合并现有表边做环检测，语义与 db 层一致。
_P2_COMPAT_SCOPE = SCOPE_WORKSPACE  # 矩阵 workspace_scoped


def _bind_readonly_db(ctx: CompatCallContext) -> CodeGraphDB:
    """轻量只读绑定：绕过 CodeGraphDB.__init__，注入 worker 只读连接与显式 workspace。

    与 tools_query.py / tools_summary.py / tools_security.py / tools_collab.py 同款：
    ctx.conn 由 compat_worker 用 `file:{db_path}?mode=ro` 打开（read_only 契约）；
    active_workspace 注入 ctx.workspace_id，db 层查询基于
    `_get_active_workspace_id()` 过滤。
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


def _h_get_artifact_freshness(ctx: CompatCallContext) -> Any:
    """worker handler：查询 artifact freshness（只读，db 层纯 SELECT）"""
    return _bind_readonly_db(ctx).get_artifact_freshness(
        ctx.params.get("workspace_id"),
        ctx.params.get("task_id", ""),
        ctx.params.get("artifact_ref", ""),
    )


def _h_get_interface_providers(ctx: CompatCallContext) -> Any:
    """worker handler：查询 interface provider 列表（只读，db 层纯 SELECT）"""
    return _bind_readonly_db(ctx).get_interface_providers(
        ctx.params.get("workspace_id"),
        ctx.params.get("interface_name", ""),
        ctx.params.get("version", ""),
    )


def _h_detect_cycle(ctx: CompatCallContext) -> Any:
    """worker handler：检测硬依赖图环（只读，db 层纯 SELECT + 内存 DFS）"""
    return _bind_readonly_db(ctx).detect_cycle(
        ctx.params.get("workspace_id"),
    )


def _h_validate_revision_dependencies(ctx: CompatCallContext) -> Any:
    """worker handler：验证 revision 依赖完整性（只读等价版）。

    db 层 validate_revision_dependencies 内部调用 build_hard_dependency_edges
    （含 INSERT dependency_edges + commit 写操作），worker 只读连接无法承载。
    本 handler 在内存中模拟构建硬边（不写库）：查询 task_dependencies →
    解析 requires_interface 的 provider / 检查多 provider 显式选择 →
    计算 edges_built/edges_skipped/resolution_errors/provider_conflicts；
    环检测合并"现有表硬边 ∪ 本次模拟边"（与 db 层 build 幂等写表后的
    detect_cycle(整表) 语义等价，DFS 三色 + BFS 最短环路径与 db 层一致）。
    """
    db = _bind_readonly_db(ctx)
    workspace_id = ctx.params.get("workspace_id")
    contract_id = ctx.params.get("contract_id", "")
    contract_revision = ctx.params.get("contract_revision")

    # 1. 内存模拟 build_hard_dependency_edges（不写 dependency_edges 表）
    cur = db.conn.execute(
        """
        SELECT task_id, dependency_type, target_ref, target_task_id,
               contract_id, contract_revision
        FROM task_dependencies
        WHERE workspace_id = ?
          AND contract_id = ?
          AND contract_revision = ?
          AND is_informational = 0
        """,
        (workspace_id, contract_id, contract_revision),
    )
    deps = [dict(r) for r in cur.fetchall()]

    edges_built = 0
    edges_skipped = 0
    resolution_errors: List[str] = []
    provider_conflicts: List[Dict[str, Any]] = []
    new_edges = set()  # (provider_task_id, consumer_task_id)

    for dep in deps:
        consumer_task_id = dep["task_id"]
        dtype = dep["dependency_type"]
        target_ref = dep["target_ref"]
        target_task_id = dep.get("target_task_id", "")
        dep_contract_id = dep["contract_id"]
        dep_revision = dep["contract_revision"]

        if dtype == "requires_artifact":
            # requires_artifact: target_task_id 是 provider
            if not target_task_id:
                resolution_errors.append(
                    f"requires_artifact 依赖缺少 target_task_id "
                    f"(task={consumer_task_id}, ref={target_ref})"
                )
                edges_skipped += 1
                continue
            new_edges.add((target_task_id, consumer_task_id))
            edges_built += 1
        elif dtype == "requires_interface":
            # requires_interface: 需要解析 provides_interface
            providers = db.get_interface_providers(workspace_id, target_ref)
            if not providers:
                resolution_errors.append(
                    f"requires_interface '{target_ref}' 无匹配 provider "
                    f"(task={consumer_task_id})"
                )
                edges_skipped += 1
                continue
            if len(providers) > 1:
                # 多 provider：检查是否有显式选择（Req 9.9）
                selected = db.get_provider_selection(
                    workspace_id, consumer_task_id,
                    dep_contract_id, dep_revision, target_ref,
                )
                if not selected:
                    provider_conflicts.append({
                        "consumer_task_id": consumer_task_id,
                        "interface_name": target_ref,
                        "providers": [
                            p["provider_task_id"] for p in providers
                        ],
                    })
                    edges_skipped += 1
                    continue
                provider_task_id = selected
            else:
                provider_task_id = providers[0]["provider_task_id"]
            new_edges.add((provider_task_id, consumer_task_id))
            edges_built += 1
        # requires_existing 和 provides_interface 不建边

    # 2. 合并现有表硬边（db 层 build 幂等写表后 detect_cycle 检测整表）
    graph: Dict[str, List[str]] = {}
    cur2 = db.conn.execute(
        """
        SELECT DISTINCT provider_task_id, consumer_task_id
        FROM dependency_edges
        WHERE workspace_id = ? AND is_hard = 1
        """,
        (workspace_id,),
    )
    for r in cur2.fetchall():
        graph.setdefault(r["provider_task_id"], []).append(
            r["consumer_task_id"]
        )
    for provider_task_id, consumer_task_id in new_edges:
        graph.setdefault(provider_task_id, []).append(consumer_task_id)

    # 3. 环检测（复刻 db 层 detect_cycle）
    cycle_result = _p2_detect_cycle_on_edges(graph)
    cycle_path = cycle_result["cycle_path"]

    # 4. 组装结果（与 db 层 validate_revision_dependencies 相同结构）
    errors = list(resolution_errors)
    for conflict in provider_conflicts:
        errors.append(
            f"interface '{conflict['interface_name']}' 有多个 provider "
            f"{conflict['providers']} 但无 Planner 显式选择 "
            f"(consumer={conflict['consumer_task_id']})"
        )
    if cycle_result["has_cycle"]:
        errors.append(
            f"硬依赖图存在环: {' → '.join(cycle_path)}"
        )

    valid = len(errors) == 0 and not provider_conflicts

    return {
        "valid": valid,
        "errors": errors,
        "cycle_path": cycle_path if cycle_result["has_cycle"] else [],
        "provider_conflicts": provider_conflicts,
        "edges_built": edges_built,
        "edges_skipped": edges_skipped,
    }


def _h_get_dependency_edges(ctx: CompatCallContext) -> Any:
    """worker handler：查询硬依赖图边（只读，db 层纯 SELECT）"""
    return _bind_readonly_db(ctx).get_dependency_edges(
        ctx.params.get("workspace_id"),
        ctx.params.get("task_id", ""),
    )


# p2 只读白名单已清空（get_artifact_freshness 已 MCP-005、get_interface_providers
# 已 MCP-006、detect_cycle 已 MCP-007、validate_revision_dependencies 已 MCP-008、
# get_dependency_edges 已 MCP-009 迁移 rust_native，移除 compat 注册）：写语义工具
# （import_envelope_dependencies / record_artifact_identity / publish_interface /
# select_interface_provider / build_hard_dependency_edges，governance_write）不接入，
# fail-closed。
_P2_READ_ONLY_METHODS: Dict[str, Any] = {}

# 模块级注册：worker 装配 import 本模块时执行，注册到 compat_registry 单例并
# 同步 RUST_COMPAT_ROUTE（Rust 侧 http_server.rs 白名单在步骤#2 同步）。
# p2 只读白名单已清空（已迁移 rust_native，见上方注释）：空表直接跳过注册，
# 保持 fail-closed 语义，避免 register_read_only_batch 抛出
# "methods 不能为空"（空白名单 = 无 compat 路由，而非遗漏注册）。
if _P2_READ_ONLY_METHODS:
    register_compat_routes(
        _P2_READ_ONLY_METHODS,
        workspace_scope=_P2_COMPAT_SCOPE,
        description="H4C-2 第三批 p2 依赖图/环检测组只读工具（5 个，T-1786747295227-b876fddf 步骤#1）",
    )


def _p2_detect_cycle_on_edges(graph: Dict[str, List[str]]) -> Dict[str, Any]:
    """在内存边集合上做环检测（复刻 db 层 detect_cycle 的 DFS 三色 + BFS 语义）。"""
    if not graph:
        return {"has_cycle": False, "cycle_path": [], "checked_nodes": 0}

    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {}
    has_cycle = False
    cycle_start_node = None

    def dfs_has_cycle(node: str) -> bool:
        nonlocal has_cycle, cycle_start_node
        color[node] = GRAY
        for neighbor in graph.get(node, []):
            if color.get(neighbor, WHITE) == GRAY:
                has_cycle = True
                cycle_start_node = neighbor
                return True
            if color.get(neighbor, WHITE) == WHITE:
                if dfs_has_cycle(neighbor):
                    return True
        color[node] = BLACK
        return False

    for node in graph:
        if color.get(node, WHITE) == WHITE:
            if dfs_has_cycle(node):
                break

    if not has_cycle or cycle_start_node is None:
        return {"has_cycle": False, "cycle_path": [], "checked_nodes": len(graph)}
    cycle_path = _p2_find_shortest_cycle(graph, cycle_start_node)
    return {"has_cycle": True, "cycle_path": cycle_path, "checked_nodes": len(graph)}


def _p2_find_cycle_dfs(graph: Dict[str, List[str]], start: str) -> List[str]:
    """DFS 找从 start 出发的 cycle path（复刻 db 层 _find_cycle_dfs）。"""
    path: List[str] = []
    visited = set()

    def dfs(node: str) -> List[str]:
        path.append(node)
        visited.add(node)
        for neighbor in graph.get(node, []):
            if neighbor == start and len(path) >= 1:
                return path + [start]
            if neighbor not in visited:
                result = dfs(neighbor)
                if result:
                    return result
        path.pop()
        return []

    return dfs(start)


def _p2_find_shortest_cycle(graph: Dict[str, List[str]], start: str) -> List[str]:
    """BFS 找从 start 出发回到 start 的最短 cycle path（复刻 db 层 _find_shortest_cycle）。"""
    from collections import deque

    queue = deque()
    queue.append((start, [start]))
    visited = {start}
    while queue:
        node, path = queue.popleft()
        for neighbor in graph.get(node, []):
            if neighbor == start and len(path) >= 1:
                return path + [start]
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [neighbor]))
    # BFS 未找到：回退 DFS 找任意 cycle path（与 db 层一致）
    return _p2_find_cycle_dfs(graph, start)
