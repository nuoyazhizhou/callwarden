"""
db_task_identity.py
===================

P3 Identity 验证、Attestation 签发与撤销派生 Mixin。

满足 Requirements 10.1–10.18：
- 校验 agent_id / session_id / model_id / role 完整性与分离策略
- Daemon Attestation 校验与签发（Req 10.8-10.9, 14.13）
- Attestation 撤销派生（Revocation_Mode 必填：compromised 全量失效，rotated 仅晚于撤销时间的记录失效）
- 撤销导致的 invalid 由查询时派生，不写入逐条失效事件（Req 10.10-10.17）

表对齐 schema v45：
- action_identities：action 身份记录
- attestation_records：daemon 签发的 Attestation
- attestation_revocation_records：Attestation 撤销记录（不可变，只追加）
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple


# ============================================
# 错误码与 Structured_Reason 构造
# ============================================

# 稳定错误码（Req 1.12：跨文案变化保持稳定）
ERR_IDENTITY_INCOMPLETE = "E_IDENTITY_INCOMPLETE"
ERR_IDENTITY_ROLE_MISMATCH = "E_IDENTITY_ROLE_MISMATCH"
ERR_IDENTITY_SESSION_NOT_SEPARATED = "E_IDENTITY_SESSION_NOT_SEPARATED"
ERR_IDENTITY_AGENT_FAMILY = "E_IDENTITY_AGENT_FAMILY_NOT_SEPARATED"
ERR_IDENTITY_MODEL_FAMILY = "E_IDENTITY_MODEL_FAMILY_NOT_SEPARATED"
ERR_IDENTITY_ACTION_DUPLICATE = "E_IDENTITY_ACTION_DUPLICATE"

ERR_ATTESTATION_ISSUER_NOT_DAEMON = "E_ATTESTATION_ISSUER_NOT_DAEMON"
ERR_ATTESTATION_SELF_SIGNED = "E_ATTESTATION_SELF_SIGNED"
ERR_ATTESTATION_BINDING_FAILED = "E_ATTESTATION_BINDING_FAILED"
ERR_ATTESTATION_NOT_YET_VALID = "E_ATTESTATION_NOT_YET_VALID"
ERR_ATTESTATION_EXPIRED = "E_ATTESTATION_EXPIRED"
ERR_ATTESTATION_REVOKED = "E_ATTESTATION_REVOKED"
ERR_ATTESTATION_INVALID = "E_ATTESTATION_INVALID"
ERR_ATTESTATION_ISSUANCE_FAILED = "E_ATTESTATION_ISSUANCE_FAILED"

ERR_REVOCATION_MODE_REQUIRED = "E_REVOCATION_MODE_REQUIRED"


def _reason(code: str, message_key: str, detail: str = "", **extra: Any) -> Dict[str, Any]:
    """构造 Structured_Reason（Req 1.12）

    Args:
        code: 稳定错误码
        message_key: i18n message key（zh_CN/en_US 均可解析）
        detail: 人类可读细节
        **extra: 附加字段
    """
    reason: Dict[str, Any] = {
        "code": code,
        "message_key": message_key,
        "detail": detail,
    }
    if extra:
        reason.update(extra)
    return reason


def _ok(**extra: Any) -> Dict[str, Any]:
    """构造成功结果"""
    result: Dict[str, Any] = {"code": "OK"}
    if extra:
        result.update(extra)
    return result


# authority 写路径未接入 gate 时的稳定错误码（0B fail-closed，计划 §3.3）。
ERR_CAPABILITY_DISABLED = "E_TASK_LOOP_CAPABILITY_DISABLED"


def _require_authority_gate() -> Optional[Tuple[bool, Dict[str, Any]]]:
    """authority 写入口的 gate 前置校验（0B gate-first）。

    - local 模式：无 daemon gate，保留 legacy 直写语义（不阻断）。
    - enterprise/auto 模式：authority 写必须经 daemon `CapabilityMutationGate`
      串行化（锁序 gate → authority store → task DB）；0B 阶段该 control-plane
      route 未接入，稳定 fail-closed，禁止绕过 gate 直写。
    """
    from callwarden.config import get_daemon_mode

    mode = get_daemon_mode()
    if mode in ("enterprise", "auto"):
        return False, _reason(
            ERR_CAPABILITY_DISABLED,
            "error.capability_disabled",
            detail=(
                "authority 写路径必须经 daemon CapabilityMutationGate 提交"
                "（锁序 gate → authority store → task DB）；0B 阶段 gate 接入尚未"
                "完成，拒绝直写（fail-closed），不回退本地 SQLite"
            ),
        )
    return None


# ============================================
# Mixin
# ============================================


class TaskIdentityMixin:
    """P3 Task Identity and Attestation Mixin

    为 contract/view/verdict/evidence/gate/state_transition action 记录
    agent_id/session_id/model_id/role（Req 10.1），并提供 Attestation 校验
    与撤销派生（Req 10.8-10.18）。

    身份仅作 actor attribution，不等于 assignment/lease/ownership 或 SQLite lock
    （Req 10.5, 10.7, 13.4-13.5）。
    """

    # ------------------------------------------------------------------
    # 1. Action Identity 记录（Req 10.1-10.4）
    # ------------------------------------------------------------------

    def record_action_identity(
        self,
        action_id: str,
        action_type: str,
        task_id: str,
        identity: Dict[str, Any],
        contract_id: str = "",
        contract_revision: int = 0,
        workspace_id: Optional[int] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """记录 action 身份到 action_identities 表（Req 10.1）

        Args:
            action_id: action 唯一标识（ACT-<uuid>）
            action_type: contract/view/verdict/evidence/gate/state_transition
            task_id: 关联任务 ID
            identity: {agent_id, session_id, model_id, role}
            contract_id: 关联契约 ID（如适用）
            contract_revision: 关联契约 revision（如适用）
            workspace_id: 工作区 ID，None 时取活动工作区

        Returns:
            (success, result_dict)
            失败时 result_dict 为 Structured_Reason
        """
        # 校验 Identity 完整性（不由自由文本或 ownership 补齐，Req 10.1）
        agent_id = identity.get("agent_id", "")
        session_id = identity.get("session_id", "")
        model_id = identity.get("model_id", "")
        role = identity.get("role", "")
        if not all([agent_id, session_id, model_id, role]):
            return False, _reason(
                ERR_IDENTITY_INCOMPLETE,
                "error.identity_incomplete",
                detail=f"缺失 Identity 字段: agent_id={bool(agent_id)}, "
                       f"session_id={bool(session_id)}, model_id={bool(model_id)}, "
                       f"role={bool(role)}",
            )

        if workspace_id is None:
            workspace_id = self._get_active_workspace_id()

        now = time.time()
        try:
            self.conn.execute(
                """
                INSERT INTO action_identities
                    (workspace_id, action_id, action_type, task_id, contract_id,
                     contract_revision, agent_id, session_id, model_id, role, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    action_id,
                    action_type,
                    task_id,
                    contract_id,
                    contract_revision,
                    agent_id,
                    session_id,
                    model_id,
                    role,
                    now,
                ),
            )
            self.conn.commit()
        except Exception as e:
            err_str = str(e).lower()
            if "unique" in err_str:
                return False, _reason(
                    ERR_IDENTITY_ACTION_DUPLICATE,
                    "error.identity_action_duplicate",
                    detail=f"action_id={action_id} 已存在",
                    action_id=action_id,
                )
            raise

        return True, _ok(action_id=action_id, recorded_at=now)

    def get_action_identity(
        self,
        action_id: str,
        workspace_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """查询 action 身份记录"""
        if workspace_id is None:
            workspace_id = self._get_active_workspace_id()
        cur = self.conn.execute(
            """
            SELECT * FROM action_identities
            WHERE workspace_id = ? AND action_id = ?
            """,
            (workspace_id, action_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_task_identity_by_role(
        self,
        task_id: str,
        role: str,
        workspace_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """查询指定任务中某角色的最近一条 Identity 记录（Req 10.5-10.7）

        用于 task_apply 时查找 implementer 的 Identity 以校验 session 分离。
        Identity 仅作 actor attribution，不等于 assignment/lease/ownership。

        Args:
            task_id: 任务 ID
            role: 角色（implementer/reviewer/tester/planner）
            workspace_id: 工作区 ID，None 时取活动工作区

        Returns:
            Identity 字典 {agent_id, session_id, model_id, role, ...} 或 None
        """
        if workspace_id is None:
            workspace_id = self._get_active_workspace_id()
        cur = self.conn.execute(
            """
            SELECT * FROM action_identities
            WHERE workspace_id = ? AND task_id = ? AND role = ?
            ORDER BY recorded_at DESC
            LIMIT 1
            """,
            (workspace_id, task_id, role),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # 2. Identity 验证与分离策略（Req 10.1-10.7）
    # ------------------------------------------------------------------

    def validate_action_identity(
        self,
        identity: Dict[str, Any],
        require_role: str = "",
    ) -> Tuple[bool, Dict[str, Any]]:
        """校验 Action Identity 完整性与角色约束（Req 10.1-10.4）

        Args:
            identity: {agent_id, session_id, model_id, role}
            require_role: 要求的角色（planner/implementer/reviewer/tester）

        Returns:
            (is_valid, reason_dict)
            失败时 reason_dict 为 Structured_Reason
        """
        agent_id = identity.get("agent_id", "")
        session_id = identity.get("session_id", "")
        model_id = identity.get("model_id", "")
        role = identity.get("role", "")

        if not all([agent_id, session_id, model_id, role]):
            return False, _reason(
                ERR_IDENTITY_INCOMPLETE,
                "error.identity_incomplete",
                detail="缺失必要的 Identity 字段 (agent_id, session_id, model_id, role)",
            )

        if require_role and role != require_role:
            return False, _reason(
                ERR_IDENTITY_ROLE_MISMATCH,
                "error.identity_role_mismatch",
                detail=f"角色不匹配: 期望 {require_role}, 实际 {role}",
                expected_role=require_role,
                actual_role=role,
            )

        return True, _ok()

    def validate_session_separation(
        self,
        reviewer_identity: Dict[str, Any],
        implementer_identity: Dict[str, Any],
    ) -> Tuple[bool, Dict[str, Any]]:
        """校验 Reviewer Session 与 Implementer Session 不同（Req 1.5, 10.2）

        Args:
            reviewer_identity: Reviewer 的 Identity
            implementer_identity: Implementer 的 Identity

        Returns:
            (is_separated, reason_dict)
        """
        reviewer_session = reviewer_identity.get("session_id", "")
        implementer_session = implementer_identity.get("session_id", "")
        if reviewer_session and implementer_session and reviewer_session == implementer_session:
            return False, _reason(
                ERR_IDENTITY_SESSION_NOT_SEPARATED,
                "error.identity_session_not_separated",
                detail=f"Reviewer Session ({reviewer_session}) 等于 Implementer Session",
                reviewer_session=reviewer_session,
                implementer_session=implementer_session,
            )
        return True, _ok()

    def validate_agent_family_separation(
        self,
        reviewer_identity: Dict[str, Any],
        implementer_identity: Dict[str, Any],
    ) -> Tuple[bool, Dict[str, Any]]:
        """校验 agent 家族分离（high_risk 策略，Req 10.3）

        agent 家族按 agent_id 前缀（"-" 之前的部分）分组。
        """
        reviewer_agent = reviewer_identity.get("agent_id", "")
        implementer_agent = implementer_identity.get("agent_id", "")
        reviewer_family = reviewer_agent.split("-")[0] if reviewer_agent else ""
        implementer_family = implementer_agent.split("-")[0] if implementer_agent else ""
        if reviewer_family and implementer_family and reviewer_family == implementer_family:
            return False, _reason(
                ERR_IDENTITY_AGENT_FAMILY,
                "error.identity_agent_family_not_separated",
                detail=f"Reviewer agent 家族 ({reviewer_family}) 等于 Implementer agent 家族",
                reviewer_agent_family=reviewer_family,
                implementer_agent_family=implementer_family,
            )
        return True, _ok()

    def validate_model_family_separation(
        self,
        reviewer_identity: Dict[str, Any],
        implementer_identity: Dict[str, Any],
    ) -> Tuple[bool, Dict[str, Any]]:
        """校验 model 家族分离（high_risk 策略，Req 10.4）

        model 家族按 model_id 前缀（"-" 之前的部分）分组。
        """
        reviewer_model = reviewer_identity.get("model_id", "")
        implementer_model = implementer_identity.get("model_id", "")
        reviewer_family = reviewer_model.split("-")[0] if reviewer_model else ""
        implementer_family = implementer_model.split("-")[0] if implementer_model else ""
        if reviewer_family and implementer_family and reviewer_family == implementer_family:
            return False, _reason(
                ERR_IDENTITY_MODEL_FAMILY,
                "error.identity_model_family_not_separated",
                detail=f"Reviewer model 家族 ({reviewer_family}) 等于 Implementer model 家族",
                reviewer_model_family=reviewer_family,
                implementer_model_family=implementer_family,
            )
        return True, _ok()

    # ------------------------------------------------------------------
    # 3. Attestation 签发与校验（Req 10.8-10.9, 14.13）
    # ------------------------------------------------------------------

    DAEMON_ISSUER = "daemon"

    def issue_attestation(
        self,
        action_id: str,
        signing_key_id: str,
        peer_identity: str,
        contract_hash: str,
        signature: str,
        valid_from: float,
        valid_until: float,
        bound_verdict_id: str = "",
        bound_evidence_id: str = "",
        view_manifest_hash: str = "",
        workspace_id: Optional[int] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """签发 Attestation（仅 daemon 调用，Req 10.8-10.9, 14.13）

        Args:
            action_id: 绑定的 action_identities.action_id
            signing_key_id: 签名密钥标识（Req 10.10-10.11）
            peer_identity: Peer_Identity 派生的 Identity（Req 10.8）
            contract_hash: Contract_Hash（Req 10.8）
            signature: 签名值
            valid_from: 有效窗口开始
            valid_until: 有效窗口结束
            bound_verdict_id: 绑定的 verdict_id（如适用）
            bound_evidence_id: 绑定的 evidence_id（如适用）
            view_manifest_hash: View_Manifest hash（Req 10.8）
            workspace_id: 工作区 ID

        Returns:
            (success, result_dict)
        """
        if workspace_id is None:
            workspace_id = self._get_active_workspace_id()

        attestation_id = f"ATT-{uuid.uuid4().hex[:16]}"
        now = time.time()

        # issuer 固定为 daemon（Req 14.13）
        try:
            self.conn.execute(
                """
                INSERT INTO attestation_records
                    (workspace_id, attestation_id, action_id, issuer, signing_key_id,
                     peer_identity, bound_verdict_id, bound_evidence_id,
                     view_manifest_hash, contract_hash, issued_at,
                     valid_from, valid_until, signature)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    attestation_id,
                    action_id,
                    self.DAEMON_ISSUER,
                    signing_key_id,
                    peer_identity,
                    bound_verdict_id,
                    bound_evidence_id,
                    view_manifest_hash,
                    contract_hash,
                    now,
                    valid_from,
                    valid_until,
                    signature,
                ),
            )
            self.conn.commit()
        except Exception as e:
            return False, _reason(
                ERR_ATTESTATION_ISSUANCE_FAILED,
                "error.attestation_issuance_failed",
                detail=str(e),
            )

        return True, _ok(
            attestation_id=attestation_id,
            issued_at=now,
            issuer=self.DAEMON_ISSUER,
        )

    def validate_attestation(
        self,
        attestation: Dict[str, Any],
        expected_contract_hash: str = "",
        expected_view_manifest_hash: str = "",
        check_time: Optional[float] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """校验 Attestation（Req 10.8-10.9, 14.13）

        校验项（任一失败即 fail closed，关联 verdict/Evidence 判为 invalid）：
        1. issuer 必须为 daemon（Req 14.13）
        2. 不允许客户端自签
        3. View_Manifest hash / Contract_Hash 绑定匹配
        4. 有效期窗口：valid_from <= check_time <= valid_until
        5. 撤销派生（调用 derive_attestation_validity）

        Args:
            attestation: attestation_records 行（dict）
            expected_contract_hash: 期望的 Contract_Hash
            expected_view_manifest_hash: 期望的 View_Manifest hash
            check_time: 校验时间（None 用当前时间）

        Returns:
            (is_valid, validity_status, reason_dict)
            validity_status: "valid" / "invalid"
        """
        if check_time is None:
            check_time = time.time()

        issuer = attestation.get("issuer", "")
        signing_key_id = attestation.get("signing_key_id", "")
        issued_at = attestation.get("issued_at", 0)
        valid_from = attestation.get("valid_from", 0)
        valid_until = attestation.get("valid_until", 0)
        contract_hash = attestation.get("contract_hash", "")
        view_manifest_hash = attestation.get("view_manifest_hash", "")
        peer_identity = attestation.get("peer_identity", "")

        # 1. issuer 必须为 daemon（Req 14.13）
        if issuer != self.DAEMON_ISSUER:
            return False, "invalid", _reason(
                ERR_ATTESTATION_ISSUER_NOT_DAEMON,
                "error.attestation_issuer_not_daemon",
                detail=f"issuer={issuer}, 期望 {self.DAEMON_ISSUER}",
                issuer=issuer,
            )

        # 2. 客户端自签检测：peer_identity 与 issuer 相同表示自签
        if peer_identity == issuer:
            return False, "invalid", _reason(
                ERR_ATTESTATION_SELF_SIGNED,
                "error.attestation_self_signed",
                detail=f"peer_identity={peer_identity} 等于 issuer，疑似客户端自签",
            )

        # 3. 绑定校验
        if expected_contract_hash and contract_hash != expected_contract_hash:
            return False, "invalid", _reason(
                ERR_ATTESTATION_BINDING_FAILED,
                "error.attestation_binding_failed",
                detail=f"Contract_Hash 不匹配: 期望 {expected_contract_hash}, 实际 {contract_hash}",
                expected=expected_contract_hash,
                actual=contract_hash,
            )
        if expected_view_manifest_hash and view_manifest_hash and \
           view_manifest_hash != expected_view_manifest_hash:
            return False, "invalid", _reason(
                ERR_ATTESTATION_BINDING_FAILED,
                "error.attestation_binding_failed",
                detail=f"View_Manifest hash 不匹配: 期望 {expected_view_manifest_hash}, "
                       f"实际 {view_manifest_hash}",
                expected=expected_view_manifest_hash,
                actual=view_manifest_hash,
            )

        # 4. 有效期窗口
        if check_time < valid_from:
            return False, "invalid", _reason(
                ERR_ATTESTATION_NOT_YET_VALID,
                "error.attestation_not_yet_valid",
                detail=f"check_time={check_time} < valid_from={valid_from}",
            )
        if check_time > valid_until:
            return False, "invalid", _reason(
                ERR_ATTESTATION_EXPIRED,
                "error.attestation_expired",
                detail=f"check_time={check_time} > valid_until={valid_until}",
            )

        # 5. 撤销派生（Req 10.10-10.17）
        workspace_id = attestation.get("workspace_id", self._get_active_workspace_id())
        validity = self.derive_attestation_validity(
            issuer=issuer,
            signing_key_id=signing_key_id,
            issuance_time=issued_at,
            workspace_id=workspace_id,
        )
        if validity == "invalid":
            return False, "invalid", _reason(
                ERR_ATTESTATION_REVOKED,
                "error.attestation_revoked",
                detail=f"issuer={issuer}, signing_key_id={signing_key_id} 已被撤销",
            )

        return True, "valid", _ok()

    # ------------------------------------------------------------------
    # 4. Attestation 撤销（Req 10.10-10.18）
    # ------------------------------------------------------------------

    def register_attestation_revocation(
        self,
        issuer: str,
        signing_key_id: str,
        revocation_mode: str,
        revocation_reason: str,
        initiating_actor: str,
        workspace_id: Optional[int] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """注册 Attestation 撤销记录（Req 10.10-10.18）

        追加**单条**不可变 `Attestation_Revocation_Record`，不写入逐条失效事件。
        撤销导致的 `invalid` 由查询时派生（见 derive_attestation_validity）。

        Args:
            issuer: 被撤销的 Attestation issuer 标识
            signing_key_id: 被撤销的签名密钥标识
            revocation_mode: **必填**，compromised 或 rotated（无默认值，Req 10.12）
            revocation_reason: 撤销原因
            initiating_actor: 发起者身份
            workspace_id: 工作区 ID

        Returns:
            (success, result_dict)
            缺 revocation_mode 时返回 Structured_Reason 拒绝，且**不追加任何记录**。
        """
        # 0B gate-first：authority 写入口须经 CapabilityMutationGate，禁止绕过 gate 直写。
        blocked = _require_authority_gate()
        if blocked is not None:
            return blocked

        # Revocation_Mode 必填且无默认值（Req 10.12）
        # 缺值时 Structured_Reason 拒绝，不追加记录
        if not revocation_mode or revocation_mode not in ("compromised", "rotated"):
            return False, _reason(
                ERR_REVOCATION_MODE_REQUIRED,
                "error.revocation_mode_missing",
                detail=f"revocation_mode={revocation_mode!r}, 必须为 'compromised' 或 'rotated'",
            )

        if workspace_id is None:
            workspace_id = self._get_active_workspace_id()

        revocation_id = f"REV-{uuid.uuid4().hex[:16]}"
        now = time.time()

        self.conn.execute(
            """
            INSERT INTO attestation_revocation_records
                (workspace_id, revocation_id, issuer, signing_key_id,
                 revocation_mode, revocation_reason, initiating_actor, revoked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                revocation_id,
                issuer,
                signing_key_id,
                revocation_mode,
                revocation_reason,
                initiating_actor,
                now,
            ),
        )
        self.conn.commit()

        return True, _ok(
            revocation_id=revocation_id,
            issuer=issuer,
            signing_key_id=signing_key_id,
            revocation_mode=revocation_mode,
            revoked_at=now,
        )

    # ------------------------------------------------------------------
    # 5. 撤销派生（Req 10.13-10.18）
    # ------------------------------------------------------------------

    def derive_attestation_validity(
        self,
        issuer: str,
        signing_key_id: str,
        issuance_time: float,
        workspace_id: Optional[int] = None,
    ) -> str:
        """派生 Attestation 有效性状态（Req 10.13-10.18）

        查询 attestation_revocation_records，按 Revocation_Mode 派生：
        - compromised：忽略 issuance_time，匹配 issuer/key 的记录一律为 invalid（Req 10.14）
        - rotated：仅当 issuance_time > revoked_at 时判为 invalid（Req 10.15）
          （轮换前的历史记录保持 valid，例行密钥轮换不误伤历史账本）

        派生确定性：同一记录与同一撤销记录集合，重复派生结果恒定（Req 10.16）。
        撤销派生的结论就是 Requirement 10.9 的 invalid，不引入第二个状态值（Req 10.17）。

        Args:
            issuer: Attestation issuer 标识
            signing_key_id: 签名密钥标识
            issuance_time: Attestation 签发时间（issued_at）
            workspace_id: 工作区 ID

        Returns:
            "valid" 或 "invalid"
        """
        if workspace_id is None:
            workspace_id = self._get_active_workspace_id()

        cur = self.conn.execute(
            """
            SELECT revocation_mode, revoked_at
            FROM attestation_revocation_records
            WHERE workspace_id = ? AND issuer = ? AND signing_key_id = ?
            ORDER BY revoked_at ASC
            """,
            (workspace_id, issuer, signing_key_id),
        )
        rows = cur.fetchall()

        for row in rows:
            mode = row["revocation_mode"]
            revoked_at = row["revoked_at"]
            if mode == "compromised":
                # compromised：忽略签发时间，匹配 issuer/key 的全部记录判为 invalid（Req 10.14）
                return "invalid"
            elif mode == "rotated":
                # rotated：仅签发时间晚于撤销时间的记录判为 invalid（Req 10.15）
                if issuance_time > revoked_at:
                    return "invalid"
            # 其他模式不应出现（CHECK 约束已限制），忽略

        return "valid"

    def list_attestation_revocations(
        self,
        issuer: str = "",
        signing_key_id: str = "",
        workspace_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """查询 Attestation 撤销记录（只读）"""
        if workspace_id is None:
            workspace_id = self._get_active_workspace_id()
        sql = """
            SELECT * FROM attestation_revocation_records
            WHERE workspace_id = ?
        """
        params: list = [workspace_id]
        if issuer:
            sql += " AND issuer = ?"
            params.append(issuer)
        if signing_key_id:
            sql += " AND signing_key_id = ?"
            params.append(signing_key_id)
        sql += " ORDER BY revoked_at ASC"
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
