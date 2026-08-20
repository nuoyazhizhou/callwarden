"""P4 Assignment 与安全 Lease 工具（Req 11.1-11.13）

拆分自 server/mcp_server.py（5660-5914 行区间），由 register(mcp) 注册。

H4B-E（T-1786590214634-9e740cdc-h4b-unsupported-error）：governance/unsupported/error cutover
- dispatch.rs 有真实 lease.* RPC 分支（lease.acquire / lease.extend
  [lease.renew 兼容别名] / lease.release / lease.status / lease.list_events），
  5 个 lease 工具保留 HTTP 分支（走 _call_daemon_rpc 真名透传）。
- dispatch.rs 无任何 p4.* RPC 分支。本模块 3 个 assignment 工具
  （assignment_create/assignment_show/assignment_revoke）曾存在
  `_call_daemon_rpc("p4.xxx", ...)` 伪路由，指向不存在的 RPC——HTTP 模式
  必抛 method_not_found，违反 fail-closed 契约，已改为 _http_unsupported()
  结构化 unsupported（不构造 CodeGraphDB，无 SQLite fallback）。
- 非 HTTP（legacy）模式保持本地 get_db() 执行，公开方法语义不变。
"""

# P4: Assignment 与安全 Lease 工具（Req 11.1-11.13, 13.4-13.10）

from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import _call_daemon_rpc, get_db
from ...db import CodeGraphDB
from callwarden.config import get_daemon_mode
from callwarden.server.daemon_client import route_worker_call
from callwarden.server.daemon_client import is_http_transport_enabled
from callwarden.server.daemon_client import (
    DaemonUnavailableError,
    UnixDaemonRpcClient,
)
from callwarden.server.daemon_protocol import DaemonRemoteError

# H4C-2 第三批（T-1786747295227-b876fddf）：p4 assignment_show 只读工具接入
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
    def lease_acquire(
        task_id: str,
        role: str = "implementer",
        agent_id: str = "",
        session_id: str = "",
        model_id: str = "",
        ttl_seconds: float = 3600.0,
    ) -> dict:
        """获取安全 Lease（P4，Req 11.2-11.3）

        原子比较当前 Lease 状态：已有未过期 active lease 则拒绝；已过期则覆盖。
        fencing counter 单调递增（Req 11.3）。raw token 仅本次响应返回一次，
        数据库只存 sha256 hash（Req 11.2），调用方须妥善保存用于 renew/release
        与受保护写操作。

        Lease 保证的是 daemon 在线期间的并发正确性；防篡改归属 Attestation 校验
        与追加式 Evidence_Ledger，不防止离线直接改库（Req 11.13, 14.32）。

        Args:
            task_id: 关联任务 ID
            role: 角色（implementer/reviewer/tester/planner）
            agent_id: holder Agent 标识（必填，Req 11.2）
            session_id: holder Session 标识（必填）
            model_id: holder Model 标识（必填）
            ttl_seconds: 有效期（秒），expires_at = 权威时钟 + ttl

        Returns:
            成功：{ok: True, lease_id, token, fencing_counter, acquired_at, expires_at}
            失败：{ok: False, code, message_key, detail, ...}（结构化拒绝，Req 1.12）
        """
        return _route('lease.acquire', {"task_id": task_id, "role": role, "agent_id": agent_id, "session_id": session_id, "model_id": model_id, "ttl_seconds": ttl_seconds}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def lease_renew(
        task_id: str,
        role: str,
        token: str,
        agent_id: str = "",
        session_id: str = "",
        model_id: str = "",
        ttl_seconds: float = 3600.0,
    ) -> dict:
        """续租 Lease（P4，Req 11.4-11.5）

        要求当前 token hash、holder Identity 与未过期；校验通过后从权威时钟
        设置更晚的 expires_at 并更新 renewed_at。幂等：重复有效 renew 返回同一
        lease 状态，不递增 fencing counter，不创建新 lease（Req 11.5）。

        Args:
            task_id: 任务 ID
            role: 角色
            token: Lease raw token（acquire 返回）
            agent_id: holder Agent 标识
            session_id: holder Session 标识
            model_id: holder Model 标识
            ttl_seconds: 续租后有效期（秒）

        Returns:
            成功：{ok: True, lease_id, fencing_counter, renewed_at, expires_at}
            失败：{ok: False, code, message_key, detail, ...}
        """
        return _route('lease.renew', {"task_id": task_id, "role": role, "token": token, "agent_id": agent_id, "session_id": session_id, "model_id": model_id, "ttl_seconds": ttl_seconds}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def lease_release(
        task_id: str,
        role: str,
        token: str,
        agent_id: str = "",
        session_id: str = "",
        model_id: str = "",
    ) -> dict:
        """释放 Lease（P4，Req 11.6-11.7）

        当前 token 匹配时原子追加 release 审计事件并将 lease 置 released。
        幂等：重复 release 返回同一 released 状态，不改变 fencing counter，
        不创建第二个 active lease（Req 11.7）。

        Args:
            task_id: 任务 ID
            role: 角色
            token: Lease raw token
            agent_id: 发起者 Agent 标识
            session_id: 发起者 Session 标识
            model_id: 发起者 Model 标识

        Returns:
            成功：{ok: True, lease_id, fencing_counter, released_at, status}
            失败：{ok: False, code, message_key, detail, ...}
        """
        return _route('lease.release', {"task_id": task_id, "role": role, "token": token, "agent_id": agent_id, "session_id": session_id, "model_id": model_id}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def lease_status(task_id: str, role: str = "") -> dict:
        """查询 Lease 状态（P4，只读，Req 11.2）

        返回当前 active lease（含 token_hash 供校验，不含 raw token）；无 active
        lease 时返回最近一条历史 lease 的状态摘要。

        Args:
            task_id: 任务 ID
            role: 角色（空 = 最近一条）

        Returns:
            {status: active/released/expired/none, lease_id, task_id, role,
             agent_id, session_id, model_id, token_hash, fencing_counter,
             acquired_at, expires_at, renewed_at, released_at}
        """
        return _route('lease.status', {"task_id": task_id, "role": role}, 'READ_ONLY')

    @mcp.tool()
    def lease_list_events(task_id: str = "", role: str = "") -> list:
        """查询 Lease 审计事件（P4，只读，append-only 账本，Req 11.6）

        按事件顺序返回 acquire/renew/release 事件；不包含 raw token。

        Args:
            task_id: 任务 ID（可选过滤）
            role: 角色（可选过滤）

        Returns:
            [{event_id, lease_id, task_id, role, event_type, fencing_counter,
              event_at, actor_agent_id, actor_session_id, actor_model_id, detail}]
        """
        return _route('lease.list_events', {"task_id": task_id, "role": role}, 'READ_ONLY')

    @mcp.tool()
    def assignment_create(
        task_id: str,
        role: str = "implementer",
        agent_id: str = "",
        session_id: str = "",
        model_id: str = "",
    ) -> dict:
        """创建 Assignment（P4，Req 11.1）

        assignment 绑定 task+role+holder Identity，不把 workspace active_task_id
        当作 assignment authority（Req 13.4）；assignment 可以没有 lease（Req 11.12）。

        Args:
            task_id: 任务 ID
            role: 角色
            agent_id: holder Agent 标识（必填）
            session_id: holder Session 标识（必填）
            model_id: holder Model 标识（必填）

        Returns:
            成功：{ok: True, assignment_id, task_id, role, agent_id, session_id,
                   model_id, created_at}
            失败：{ok: False, code, message_key, detail, ...}
        """
        return _route('admin.assignment_create', {"task_id": task_id, "role": role, "agent_id": agent_id, "session_id": session_id, "model_id": model_id}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def assignment_show(task_id: str, role: str = "") -> dict:
        """查询任务当前 active Assignment（P4，只读，Req 11.1）

        Args:
            task_id: 任务 ID
            role: 角色（空 = 最近一条）

        Returns:
            {assignment_id, task_id, role, agent_id, session_id, model_id,
             status, created_at, revoked_at} 或 {status: "none"}
        """
        return _route('assignment_show', {"task_id": task_id, "role": role}, 'READ_ONLY')

    @mcp.tool()
    def assignment_revoke(assignment_id: str) -> dict:
        """撤销 Assignment（P4，Req 11.1）

        追加 revoked_at 并置 status=revoked，不删除记录（append 语义）。

        Args:
            assignment_id: Assignment ID（ASG-xxx）

        Returns:
            成功：{ok: True, assignment_id, revoked_at}
            失败：{ok: False, code, message_key, detail, ...}
        """
        return _route('admin.assignment_revoke', {"assignment_id": assignment_id}, 'PROTECTED_MUTATION')

    return mcp


# ---------------------------------------------------------------------------
# H4C-2 第三批（T-1786747295227-b876fddf）：p4 assignment_show 只读工具接入
# ---------------------------------------------------------------------------
# - 接入范围：assignment_show（1 个只读工具，db 层 get_assignment 纯 SELECT）。
#   lease_* 5 个工具是 rust_native（HTTP 走 _call_daemon_rpc 真名透传，不经
#   worker），assignment_create / assignment_revoke 是写语义工具
#   （governance_write）不接入 worker，维持 _http_unsupported fail-closed。
# - 轻量只读绑定：object.__new__(CodeGraphDB) 绕过 __init__（含 PRAGMA WAL /
#   schema 迁移 / workspace 注册等写副作用），注入 ctx.conn（worker 的
#   mode=ro 只读连接）+ ctx.workspace_id 后复用 db 层查询方法。
_P4_COMPAT_SCOPE = SCOPE_WORKSPACE  # 矩阵 workspace_scoped


def _bind_readonly_db(ctx: CompatCallContext) -> CodeGraphDB:
    """轻量只读绑定：绕过 CodeGraphDB.__init__，注入 worker 只读连接与显式 workspace。

    与 tools_query.py / tools_summary.py / tools_security.py / tools_collab.py /
    tools_p2_graph.py / tools_p3_identity.py 同款：ctx.conn 由 compat_worker 用
    `file:{db_path}?mode=ro` 打开（read_only 契约）；active_workspace 注入
    ctx.workspace_id，db 层查询基于 `_get_active_workspace_id()` 过滤。
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


def _h_assignment_show(ctx: CompatCallContext) -> Any:
    """worker handler：查询任务当前 active Assignment（只读，db 层纯 SELECT）"""
    db = _bind_readonly_db(ctx)
    try:
        result = db.get_assignment(
            ctx.params.get("task_id", ""),
            ctx.params.get("role", ""),
        )
        if result is None:
            return {
                "status": "none",
                "task_id": ctx.params.get("task_id", ""),
                "role": ctx.params.get("role", ""),
            }
        return result
    except Exception as e:
        return {"status": "none", "error": str(e)}


# p4 只读白名单（1 个）：lease_* 5 个为 rust_native 不走 worker；
# assignment_create / assignment_revoke（governance_write）不接入，fail-closed。
_P4_READ_ONLY_METHODS: Dict[str, Any] = {
    "assignment_show": _h_assignment_show,
}

# 模块级注册：worker 装配 import 本模块时执行，注册到 compat_registry 单例并
# 同步 RUST_COMPAT_ROUTE（Rust 侧 http_server.rs 白名单在步骤#2 同步）。
register_compat_routes(
    _P4_READ_ONLY_METHODS,
    workspace_scope=_P4_COMPAT_SCOPE,
    description="H4C-2 第三批 p4 assignment_show 只读工具（1 个，T-1786747295227-b876fddf 步骤#1）",
)
