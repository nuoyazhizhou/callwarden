"""P3 Identity/Attestation 工具（Req 10.1-10.18）+ _identity_* 辅助

拆分自 server/mcp_server.py（5329-5659 行区间），由 register(mcp) 注册。

H4B-E（T-1786590214634-9e740cdc-h4b-unsupported-error）：governance/unsupported/error cutover
- dispatch.rs 无任何 p3.* RPC 分支（DaemonStateExt 默认 method_not_found）。
  本模块 7 个工具曾存在 `_call_daemon_rpc("p3.xxx", ...)` 伪路由，指向不存在的
  RPC——HTTP 模式必抛 method_not_found，违反 fail-closed 契约，已全部移除。
- 本模块工具在 HTTP 模式下 fail-closed：经 _http_unsupported() 返回结构化
  unsupported 错误，不直连本地 SQLite（不构造 CodeGraphDB，无 SQLite fallback）；
  非 HTTP（legacy）模式保持本地 get_db() 执行，公开方法语义不变。
"""

# P3 Identity / Attestation 工具（Req 10.1-10.18, 8.8 任务）

from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import get_db
from ...db import CodeGraphDB
from callwarden.server.daemon_client import route_worker_call

# H4C-2 第三批（T-1786747295227-b876fddf）：p3 身份/证明只读工具接入
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


# ---------------------------------------------------------------------------
# H4C-2 第三批（T-1786747295227-b876fddf）：p3 只读工具 compat worker 装配
# ---------------------------------------------------------------------------
# - 接入范围：get_action_identity / check_action_identity /
#   check_session_separation / get_attestation_validity /
#   list_attestation_revocations（5 个只读工具）。写语义工具
#   （record_action_identity / register_attestation_revocation，
#   governance_write）不接入 worker，维持 _http_unsupported fail-closed。
# - 轻量只读绑定：object.__new__(CodeGraphDB) 绕过 __init__（含 PRAGMA WAL /
#   schema 迁移 / workspace 注册等写副作用），注入 ctx.conn（worker 的
#   mode=ro 只读连接）+ ctx.workspace_id 后复用 db 层查询方法。
# - register() 内的 _resolve_identity_arg / _identity_mcp_reason 是闭包，
#   handler 无法访问，模块级复制同逻辑（纯 db 校验/纯构造，只读）。
_P3_COMPAT_SCOPE = SCOPE_WORKSPACE  # 矩阵 workspace_scoped


def _bind_readonly_db(ctx: CompatCallContext) -> CodeGraphDB:
    """轻量只读绑定：绕过 CodeGraphDB.__init__，注入 worker 只读连接与显式 workspace。

    与 tools_query.py / tools_summary.py / tools_security.py / tools_collab.py /
    tools_p2_graph.py 同款：ctx.conn 由 compat_worker 用 `file:{db_path}?mode=ro`
    打开（read_only 契约）；active_workspace 注入 ctx.workspace_id，db 层查询
    基于 `_get_active_workspace_id()` 过滤。
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


def _h_get_action_identity(ctx: CompatCallContext) -> Any:
    """worker handler：查询 action 身份记录（只读，db 层纯 SELECT）"""
    db = _bind_readonly_db(ctx)
    try:
        return db.get_action_identity(
            ctx.params.get("action_id", ""),
            ctx.params.get("workspace_id"),
        )
    except Exception as e:
        return _p3_identity_mcp_reason(
            "E_IDENTITY_QUERY_FAILED",
            "error.identity_not_wired",
            f"身份查询失败: {e}",
        )


def _h_check_action_identity(ctx: CompatCallContext) -> Any:
    """worker handler：校验结构化身份（只读）"""
    db = _bind_readonly_db(ctx)
    try:
        identity_dict, id_reason = _p3_resolve_identity_arg(
            db, ctx.params.get("identity", ""))
        if id_reason:
            return {"valid": False, "reason": id_reason}
        ok, reason = db.validate_action_identity(
            identity_dict, require_role=ctx.params.get("require_role", ""))
        return {"valid": bool(ok), "reason": reason}
    except Exception as e:
        return {"valid": False, "reason": {
            "code": "E_IDENTITY_VALIDATION_FAILED",
            "message_key": "error.identity_not_wired",
            "detail": str(e),
        }}


def _h_check_session_separation(ctx: CompatCallContext) -> Any:
    """worker handler：校验 Reviewer/Implementer 会话分离（只读）"""
    db = _bind_readonly_db(ctx)
    try:
        import json as _json
        reviewer = _json.loads(ctx.params.get("reviewer_identity", ""))
        implementer = _json.loads(ctx.params.get("implementer_identity", ""))
        if not isinstance(reviewer, dict) or not isinstance(implementer, dict):
            return {"valid": False, "reason": {
                "code": "E_IDENTITY_INCOMPLETE",
                "message_key": "error.identity_incomplete",
                "detail": "reviewer/implementer_identity 必须是 JSON 对象",
            }}
        ok, reason = db.validate_session_separation(reviewer, implementer)
        return {"valid": bool(ok), "reason": reason}
    except Exception as e:
        return {"valid": False, "reason": {
            "code": "E_IDENTITY_VALIDATION_FAILED",
            "message_key": "error.identity_not_wired",
            "detail": str(e),
        }}


def _h_get_attestation_validity(ctx: CompatCallContext) -> Any:
    """worker handler：派生 Attestation 有效性（只读，查询时刻派生不持久化）"""
    db = _bind_readonly_db(ctx)
    try:
        validity = db.derive_attestation_validity(
            ctx.params.get("issuer", ""),
            ctx.params.get("signing_key_id", ""),
            ctx.params.get("issuance_time", 0.0),
            ctx.params.get("workspace_id"),
        )
        return {"validity": validity}
    except Exception as e:
        return _p3_identity_mcp_reason(
            "E_ATTESTATION_INVALID",
            "error.attestation_invalid",
            f"Attestation 有效性派生失败: {e}",
        )


def _h_list_attestation_revocations(ctx: CompatCallContext) -> Any:
    """worker handler：查询 Attestation 撤销账本（只读，append-only）"""
    db = _bind_readonly_db(ctx)
    try:
        items = db.list_attestation_revocations(
            issuer=ctx.params.get("issuer", ""),
            signing_key_id=ctx.params.get("signing_key_id", ""),
            workspace_id=ctx.params.get("workspace_id"),
        )
        return {"items": items, "count": len(items)}
    except Exception as e:
        return _p3_identity_mcp_reason(
            "E_IDENTITY_QUERY_FAILED",
            "error.identity_not_wired",
            f"撤销账本查询失败: {e}",
        )


# p3 只读白名单（5 个）：写语义工具（record_action_identity /
# register_attestation_revocation，governance_write）不接入，fail-closed。
_P3_READ_ONLY_METHODS: Dict[str, Any] = {
    "get_action_identity": _h_get_action_identity,
    "check_action_identity": _h_check_action_identity,
    "check_session_separation": _h_check_session_separation,
    "get_attestation_validity": _h_get_attestation_validity,
    "list_attestation_revocations": _h_list_attestation_revocations,
}

# 模块级注册：worker 装配 import 本模块时执行，注册到 compat_registry 单例并
# 同步 RUST_COMPAT_ROUTE（Rust 侧 http_server.rs 白名单在步骤#2 同步）。
register_compat_routes(
    _P3_READ_ONLY_METHODS,
    workspace_scope=_P3_COMPAT_SCOPE,
    description="H4C-2 第三批 p3 身份/证明组只读工具（5 个，T-1786747295227-b876fddf 步骤#1）",
)


def _p3_resolve_identity_arg(db: CodeGraphDB, identity: str):
    """解析并校验结构化身份（JSON 字符串，Req 10.5，模块级副本）。

    register() 内的 _resolve_identity_arg 是闭包，handler 无法访问；
    本副本逻辑一致（json 解析 + db.validate_action_identity 只读校验）。
    """
    if not identity:
        return None, None
    try:
        import json as _json
        identity_dict = _json.loads(identity)
    except Exception:
        return None, {
            "code": "E_IDENTITY_INCOMPLETE",
            "message_key": "error.identity_incomplete",
            "detail": "identity 必须是 JSON 对象 {agent_id, session_id, model_id, role}",
        }
    if not isinstance(identity_dict, dict):
        return None, {
            "code": "E_IDENTITY_INCOMPLETE",
            "message_key": "error.identity_incomplete",
            "detail": "identity 必须是 JSON 对象",
        }
    ok, reason = db.validate_action_identity(identity_dict)
    if not ok:
        return None, reason
    return identity_dict, None


def _p3_identity_mcp_reason(code: str, message_key: str,
                            detail: str) -> dict:
    """构造 P3 失败路径的 Structured_Reason（模块级副本，与 register() 内同逻辑）。"""
    return {
        "status": "error",
        "reason": {
            "code": code,
            "message_key": message_key,
            "detail": detail,
        },
    }
