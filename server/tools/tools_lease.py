"""P4 Assignment 与安全 Lease 工具（Req 11.1-11.13）

拆分自 server/mcp_server.py（5660-5914 行区间），由 register(mcp) 注册。

H4B-E（T-1786590214634-9e740cdc-h4b-unsupported-error）：governance/unsupported/error cutover
- dispatch.rs 有真实 lease.* RPC 分支（lease.acquire / lease.extend
  [lease.renew 兼容别名] / lease.release / lease.status / lease.list_events），
  5 个 lease 工具走 route_rpc 真名透传。
- dispatch.rs 无任何 p4.* RPC 分支。本模块 3 个 assignment 工具
  （assignment_create/assignment_show/assignment_revoke）曾存在
  `_call_daemon_rpc("p4.xxx", ...)` 伪路由，指向不存在的 RPC——HTTP 模式
  必抛 method_not_found，违反 fail-closed 契约，后统一改为 route_rpc
  （assignment_show 走裸工具名，P0-COMPAT-v3 已迁 rust_native）。
- 本模块工具全部经 route_rpc（HTTP 模式走 daemon RPC，非 HTTP 模式回落本地
  执行）；compat worker 只读接入层已随 P0-COMPAT-v3 退役清理
  （T-1789789687355-e9816058），不构造 CodeGraphDB、无 SQLite fallback。
"""

# P4: Assignment 与安全 Lease 工具（Req 11.1-11.13, 13.4-13.10）

from typing import Any, Dict

from mcp.server.fastmcp import FastMCP

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


# p4 只读白名单：lease_* 5 个为 rust_native 不走 worker；
# assignment_show 已 P0-COMPAT-v3（T-1788963088148-495d7208）迁移 rust_native；
# assignment_create / assignment_revoke（governance_write）不接入，fail-closed。
# 空常量保留供退役断言引用
# （tests/test_mcp_compat_identity-lease-small_http_rpc.py not-in 断言）。
# 退役 handler、_bind_readonly_db 与 H4B-E 时期 compat / legacy 旧 import
# 已于 T-1789789687355-e9816058 清理（全部活工具走 route_rpc，RUST_COMPAT_ROUTE=0）。
_P4_READ_ONLY_METHODS: Dict[str, Any] = {}
