"""P3 Identity/Attestation 工具（Req 10.1-10.18）+ _identity_* 辅助

拆分自 server/mcp_server.py（5329-5659 行区间），由 register(mcp) 注册。
"""

# P3 Identity / Attestation 工具（Req 10.1-10.18, 8.8 任务）

from typing import Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import get_db


def register(mcp: FastMCP) -> None:
    def _identity_mcp_reason(code: str, message_key: str,
                             detail: str) -> dict:
        """构造 P3 失败路径的 Structured_Reason（Requirement 1.12）。

        稳定错误码 + 可在 zh_CN/en_US 两个 catalog 解析的 i18n message key；
        文案变化不得改变错误码值。
        """
        return {
            "status": "error",
            "reason": {
                "code": code,
                "message_key": message_key,
                "detail": detail,
            },
        }

    def _resolve_identity_arg(db, identity: str):
        """解析并校验 MCP 传入的结构化身份（JSON 字符串，Req 10.5）。

        包装层不伪造缺省身份：未提供时返回 (None, None)；解析失败或
        校验失败返回 (None, structured_reason)。自由文本 reviewer 不能
        充当身份证明（Req 10.5）。
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

    def _db_method_accepts_identity(method_name: str) -> bool:
        """检查 db 方法是否接受 identity 关键字参数（8.6 接线后为 True）。"""
        db = get_db()
        fn = getattr(db, method_name, None)
        if fn is None:
            return False
        try:
            import inspect
            params = inspect.signature(fn).parameters
            return "identity" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in params.values())
        except (TypeError, ValueError):
            return False

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
        db = get_db()
        try:
            identity_dict, id_reason = _resolve_identity_arg(db, identity)
            if id_reason:
                return _identity_mcp_reason(
                    id_reason.get("code", "E_IDENTITY_INVALID"),
                    id_reason.get("message_key", "error.identity_incomplete"),
                    id_reason.get("detail", "身份校验失败"),
                )
            ok, result = db.record_action_identity(
                action_id=action_id,
                action_type=action_type,
                task_id=task_id,
                identity=identity_dict,
                contract_id=contract_id,
                contract_revision=contract_revision,
                workspace_id=workspace_id,
            )
            if not ok:
                return {"status": "error", "reason": result}
            return result
        except Exception as e:
            return _identity_mcp_reason(
                "E_IDENTITY_RECORD_FAILED",
                "error.identity_not_wired",
                f"身份记录失败: {e}",
            )

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
        db = get_db()
        try:
            return db.get_action_identity(action_id, workspace_id)
        except Exception as e:
            return _identity_mcp_reason(
                "E_IDENTITY_QUERY_FAILED",
                "error.identity_not_wired",
                f"身份查询失败: {e}",
            )

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
        db = get_db()
        try:
            identity_dict, id_reason = _resolve_identity_arg(db, identity)
            if id_reason:
                return {"valid": False, "reason": id_reason}
            ok, reason = db.validate_action_identity(
                identity_dict, require_role=require_role)
            return {"valid": bool(ok), "reason": reason}
        except Exception as e:
            return {"valid": False, "reason": {
                "code": "E_IDENTITY_VALIDATION_FAILED",
                "message_key": "error.identity_not_wired",
                "detail": str(e),
            }}

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
        db = get_db()
        try:
            import json as _json
            reviewer = _json.loads(reviewer_identity)
            implementer = _json.loads(implementer_identity)
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
        db = get_db()
        try:
            validity = db.derive_attestation_validity(
                issuer, signing_key_id, issuance_time, workspace_id)
            return {"validity": validity}
        except Exception as e:
            return _identity_mcp_reason(
                "E_ATTESTATION_INVALID",
                "error.attestation_invalid",
                f"Attestation 有效性派生失败: {e}",
            )

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
        db = get_db()
        try:
            items = db.list_attestation_revocations(
                issuer=issuer, signing_key_id=signing_key_id,
                workspace_id=workspace_id)
            return {"items": items, "count": len(items)}
        except Exception as e:
            return _identity_mcp_reason(
                "E_IDENTITY_QUERY_FAILED",
                "error.identity_not_wired",
                f"撤销账本查询失败: {e}",
            )

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
        if revocation_mode not in ("compromised", "rotated"):
            return _identity_mcp_reason(
                "E_REVOCATION_MODE_REQUIRED",
                "error.revocation_mode_missing",
                "撤销请求必须显式指定 Revocation_Mode（compromised/rotated），"
                "未提供时不追加任何撤销记录",
            )
        db = get_db()
        try:
            ok, result = db.register_attestation_revocation(
                issuer=issuer,
                signing_key_id=signing_key_id,
                revocation_mode=revocation_mode,
                revocation_reason=revocation_reason,
                initiating_actor=initiating_actor,
                workspace_id=workspace_id,
            )
            if not ok:
                return {"status": "error", "reason": result}
            return result
        except Exception as e:
            return _identity_mcp_reason(
                "E_REVOCATION_FAILED",
                "error.attestation_invalid",
                f"撤销记录写入失败: {e}",
            )
