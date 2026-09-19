"""P3 Identity/Attestation 工具（Req 10.1-10.18）+ _identity_* 辅助

拆分自 server/mcp_server.py（5329-5659 行区间），由 register(mcp) 注册。

H4B-E（T-1786590214634-9e740cdc-h4b-unsupported-error）：governance/unsupported/error cutover
- dispatch.rs 无任何 p3.* RPC 分支（DaemonStateExt 默认 method_not_found）。
  本模块 7 个工具曾存在 `_call_daemon_rpc("p3.xxx", ...)` 伪路由，指向不存在的
  RPC——HTTP 模式必抛 method_not_found，违反 fail-closed 契约，已全部移除。
- 本模块工具全部经 route_rpc（HTTP 模式走 daemon RPC，非 HTTP 模式回落本地
  执行）；compat worker 只读接入层已随 P0-COMPAT-v3 退役清理
  （T-1789789687355-e9816058），不构造 CodeGraphDB、无 SQLite fallback。
"""

# P3 Identity / Attestation 工具（Req 10.1-10.18, 8.8 任务）

from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from ..daemon_client import route_rpc as _route


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def record_action_identity(
        action_id: str,
        action_type: str,
        task_id: str,
        identity: str,
        contract_id: str = "",
        contract_revision: int = 0,
        workspace_id: Optional[int] = None,
    ) -> dict:
        """记录 action 身份（写操作，Req 10.1）

        为 contract/view/verdict/evidence/gate/state_transition 动作记录
        agent_id/session_id/model_id/role。身份仅作 actor attribution，
        不等于 assignment、lease、ownership 或 SQLite lock（Req 10.7）。

        Args:
            action_id: 动作唯一标识
            action_type: 动作类型（contract/view/verdict/evidence/gate/state_transition）
            task_id: 任务 ID
            identity: JSON 字符串 {agent_id, session_id, model_id, role}
            contract_id: 关联契约 ID（可选）
            contract_revision: 契约 revision（可选）
            workspace_id: 工作区 ID（可选，缺省取当前工作区）

        Returns:
            {"code": "OK", "action_id": ..., "recorded_at": ...}；失败返回
            {"status": "error", "reason": {...}}（Structured_Reason）
        """
        return _route('admin.record_action_identity', {"action_id": action_id, "action_type": action_type, "task_id": task_id, "identity": identity, "contract_id": contract_id, "contract_revision": contract_revision, "workspace_id": workspace_id}, 'GOVERNANCE_WRITE')

    @mcp.tool()
    def get_action_identity(
        action_id: str,
        workspace_id: Optional[int] = None,
    ) -> dict:
        """查询 action 身份记录（只读，Req 10.1）

        Args:
            action_id: 动作唯一标识
            workspace_id: 工作区 ID（可选）

        Returns:
            身份记录 dict 或 None
        """
        return _route('get_action_identity', {"action_id": action_id, "workspace_id": workspace_id}, 'READ_ONLY')

    @mcp.tool()
    def check_action_identity(
        identity: str,
        require_role: str = "",
    ) -> dict:
        """校验结构化身份（只读，Req 10.2/10.5）

        Args:
            identity: JSON 字符串 {agent_id, session_id, model_id, role}
            require_role: 要求的角色（planner/implementer/reviewer/tester，可选）

        Returns:
            {"valid": bool, "reason": {...}}；reason 为 Structured_Reason
        """
        return _route('check_action_identity', {"identity": identity, "require_role": require_role}, 'READ_ONLY')

    @mcp.tool()
    def check_session_separation(
        reviewer_identity: str,
        implementer_identity: str,
    ) -> dict:
        """校验 Reviewer/Implementer 会话分离（只读，Req 1.5, 10.2）

        Args:
            reviewer_identity: Reviewer 身份 JSON 字符串
            implementer_identity: Implementer 身份 JSON 字符串

        Returns:
            {"valid": bool, "reason": {...}}
        """
        return _route('check_session_separation', {"reviewer_identity": reviewer_identity, "implementer_identity": implementer_identity}, 'READ_ONLY')

    @mcp.tool()
    def get_attestation_validity(
        issuer: str,
        signing_key_id: str,
        issuance_time: float,
        workspace_id: Optional[int] = None,
    ) -> dict:
        """派生 Attestation 有效性（只读，Req 10.13-10.15）

        撤销导致的 invalid 是**查询时刻**按 Revocation_Mode 语义计算的派生值：
        compromised 命中匹配 issuer/签名密钥的全部记录（与签发时间无关）；
        rotated 仅命中签发时间晚于撤销时间的记录。本工具不持久化派生状态，
        也不代为写入逐条失效事件（Req 10.10）。

        Args:
            issuer: Attestation issuer 标识
            signing_key_id: 签名密钥标识
            issuance_time: Attestation 签发时间（Authoritative_Clock 时间戳）
            workspace_id: 工作区 ID（可选）

        Returns:
            {"validity": "valid" | "invalid"}
        """
        return _route('get_attestation_validity', {"issuer": issuer, "signing_key_id": signing_key_id, "issuance_time": issuance_time, "workspace_id": workspace_id}, 'READ_ONLY')

    @mcp.tool()
    def list_attestation_revocations(
        issuer: str = "",
        signing_key_id: str = "",
        workspace_id: Optional[int] = None,
    ) -> dict:
        """查询 Attestation 撤销账本（只读，Req 10.11）

        返回不可变、只追加的 Attestation_Revocation_Record 列表；每条对应
        一次撤销（issuer 标识 + 签名密钥标识），撤销导致的 invalid 由
        get_attestation_validity 在查询时派生，本工具不写入任何失效事件。

        Args:
            issuer: 按 issuer 过滤（可选）
            signing_key_id: 按签名密钥过滤（可选）
            workspace_id: 工作区 ID（可选）

        Returns:
            {"items": [...], "count": N}
        """
        return _route('list_attestation_revocations', {"issuer": issuer, "signing_key_id": signing_key_id, "workspace_id": workspace_id}, 'READ_ONLY')

    @mcp.tool()
    def register_attestation_revocation(
        issuer: str,
        signing_key_id: str,
        revocation_mode: str = "",
        revocation_reason: str = "",
        initiating_actor: str = "",
        workspace_id: Optional[int] = None,
    ) -> dict:
        """追加 Attestation 撤销记录（写操作，Req 10.10-10.12）

        Revocation_Mode 必填且无默认值（compromised/rotated）：未携带或取值
        非法时以 Structured_Reason 拒绝，**不追加任何撤销记录**。每次撤销
        只追加一条不可变记录，不写入逐条失效事件；撤销导致的 invalid 由
        get_attestation_validity 在查询时派生（Req 10.10, 10.15）。

        Args:
            issuer: 被撤销的 Attestation issuer 标识
            signing_key_id: 被撤销的签名密钥标识
            revocation_mode: Revocation_Mode（必填，无默认值：compromised/rotated）
            revocation_reason: 撤销原因（可选）
            initiating_actor: 发起者身份（可选）
            workspace_id: 工作区 ID（可选）

        Returns:
            {"code": "OK", "revocation_id": ..., ...}；Revocation_Mode 缺失时
            返回 {"status": "error", "reason": {...}} 且不追加记录
        """
        return _route('admin.register_attestation_revocation', {"issuer": issuer, "signing_key_id": signing_key_id, "revocation_mode": revocation_mode, "revocation_reason": revocation_reason, "initiating_actor": initiating_actor, "workspace_id": workspace_id}, 'GOVERNANCE_WRITE')


# p3 只读白名单：get_action_identity 已 MCP-010、check_action_identity 已 MCP-011、
# check_session_separation 已 MCP-012 迁移 rust_native；
# get_attestation_validity / list_attestation_revocations 已 P0-COMPAT-v3
# （T-1788963088148-495d7208）迁移 rust_native。空常量保留供退役断言引用
# （tests/test_mcp_compat_identity-lease-small_http_rpc.py not-in 断言）。
# 退役 handler、_bind_readonly_db、无调用方的 _p3_resolve_identity_arg /
# _p3_identity_mcp_reason 与 compat_registry / daemon_client / db 旧 import
# 已于 T-1789789687355-e9816058 清理（RUST_COMPAT_ROUTE=0、registry 运行时注册数=0）。
_P3_READ_ONLY_METHODS: Dict[str, Any] = {}
