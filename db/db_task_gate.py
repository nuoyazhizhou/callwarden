"""
db_task_gate.py
===============

统一 Evidence Gate 判定内核与 Profile_Policy_Matrix Mixin 类。

评估契约绑定、Blocking Clauses、Role Verdicts、Scope 封闭性、Quality Findings、
Evidence Freshness 与 Profile 规则。任何 unsatisfied/unknown/stale/invalid 一律阻断。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple, Set

from i18n import t


# Profile 策略查表矩阵 (Profile_Policy_Matrix)
PROFILE_POLICY_MATRIX: Dict[str, Dict[str, Any]] = {
    "default": {
        "require_reviewer_verdict": True,
        "require_tester_verdict": False,
        "require_independent_review": True,
        "min_distinct_sessions": 2,
    },
    "standard": {
        "require_reviewer_verdict": True,
        "require_tester_verdict": True,
        "require_independent_review": True,
        "min_distinct_sessions": 2,
    },
    "high_risk": {
        "require_reviewer_verdict": True,
        "require_tester_verdict": True,
        "require_independent_review": True,
        "min_distinct_sessions": 3,
        "reject_solo_policy": True,  # high_risk 恒定拒绝 solo
    },
    "fast_track": {
        "require_reviewer_verdict": False,
        "require_tester_verdict": False,
        "require_independent_review": False,
        "min_distinct_sessions": 1,
    },
}


def _canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _compute_hash(data: Any) -> str:
    content = _canonical_json(data)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ============================================================
# Gate 组装层 fail-closed 稳定错误码（Req 1.12）
# ============================================================
# 稳定错误码：跨文案变化保持稳定（Req 1.12）。组装层
# （evaluate_evidence_gate_for_task）在契约/快照/verdict/evidence 缺失时
# 只产出 block/unknown 语义的阻断原因，禁止伪造占位或身份换取 pass
# （Req 1.1/1.8/5.5/6.10/8.3/10.5）。

GATE_CONTRACT_ENVELOPE_MISSING = "GATE_CONTRACT_ENVELOPE_MISSING"
GATE_SNAPSHOT_UNAVAILABLE = "GATE_SNAPSHOT_UNAVAILABLE"
GATE_VERDICT_ABSENT = "GATE_VERDICT_ABSENT"
GATE_EVIDENCE_ABSENT = "GATE_EVIDENCE_ABSENT"
GATE_IDENTITY_FREE_TEXT_EXCLUDED = "GATE_IDENTITY_FREE_TEXT_EXCLUDED"

# 错误码 → i18n message key（Req 1.12）。
# error.identity_incomplete 已登记于 zh_CN/en_US catalog（daemon_errors），
# 其余 key 未登记时由 _GATE_BUNDLED_DEFAULTS 提供双语文案兜底。
GATE_I18N_KEYS: Dict[str, str] = {
    GATE_CONTRACT_ENVELOPE_MISSING: "errors.gate_contract_envelope_missing",
    GATE_SNAPSHOT_UNAVAILABLE: "errors.gate_snapshot_unavailable",
    GATE_VERDICT_ABSENT: "errors.gate_verdict_absent",
    GATE_EVIDENCE_ABSENT: "errors.gate_evidence_absent",
    GATE_IDENTITY_FREE_TEXT_EXCLUDED: "error.identity_incomplete",
}

# 双语默认文案（i18n catalog 尚未收录时使用，与 db_task_evidence.py 同模式）
_GATE_BUNDLED_DEFAULTS: Dict[str, Dict[str, str]] = {
    "errors.gate_contract_envelope_missing": {
        "zh_CN": "任务 {task_id} 无契约 Envelope（缺 Current_Envelope 绑定）；Evidence Gate fail-closed 阻断（Requirement 1.1, 8.3）。",
        "en_US": "Task {task_id} has no contract Envelope (Current_Envelope binding missing); Evidence Gate fails closed (Requirement 1.1, 8.3).",
    },
    "errors.gate_snapshot_unavailable": {
        "zh_CN": "任务 {task_id} 无可捕获的 Workspace_Snapshot（S0 缺失）；返回 unknown 并 fail-closed 阻断（Requirement 6.9–6.10）。",
        "en_US": "Task {task_id} has no capturable Workspace_Snapshot (S0 unavailable); returned unknown and failed closed (Requirement 6.9–6.10).",
    },
    "errors.gate_verdict_absent": {
        "zh_CN": "任务 {task_id} 无任何已封存 Verdict 记录；Evidence Gate fail-closed 阻断（Requirement 5.5, 8.3）。",
        "en_US": "Task {task_id} has no sealed Verdict record; Evidence Gate fails closed (Requirement 5.5, 8.3).",
    },
    "errors.gate_evidence_absent": {
        "zh_CN": "任务 {task_id} 无任何绑定契约的 Evidence 记录；Evidence Gate fail-closed 阻断（Requirement 1.8, 8.3）。",
        "en_US": "Task {task_id} has no contract-bound Evidence record; Evidence Gate fails closed (Requirement 1.8, 8.3).",
    },
    # error.identity_incomplete 已登记 catalog，catalog 文案优先；此处仅作兜底。
    "error.identity_incomplete": {
        "zh_CN": "Identity 字段不完整：agent_id、session_id、model_id、role 均为必填，不得由自由文本或 ownership 补齐。",
        "en_US": "Identity fields incomplete: agent_id, session_id, model_id and role are all required and must not be filled by free text or ownership.",
    },
}


def _resolve_gate_message(message_key: str, context: Dict[str, Any]) -> str:
    """解析 gate i18n 消息（与 db_task_evidence.py 同模式）。"""
    try:
        msg = t(message_key, **context)
        if msg and msg != message_key:
            return msg
    except Exception:
        pass
    lang = t("current_locale", default="zh_CN") or "zh_CN"
    defaults = _GATE_BUNDLED_DEFAULTS.get(message_key, {})
    template = defaults.get(lang) or defaults.get("zh_CN") or message_key
    try:
        return template.format(**context)
    except (KeyError, IndexError):
        return template


def _gate_reason(
    code: str,
    task_id: str,
    status: str = "unsatisfied",
    **extra: Any,
) -> Dict[str, Any]:
    """构造组装层 Structured_Reason（稳定错误码 + 双语可解析 message_key，Req 1.12）

    Args:
        code: GATE_* 稳定错误码
        task_id: 关联任务 ID
        status: 语义状态（unknown/invalid/unsatisfied 等，Req 1.8）
        **extra: 附加上下文字段（如 verdict_id）
    """
    message_key = GATE_I18N_KEYS[code]
    context: Dict[str, Any] = {"task_id": task_id}
    context.update(extra)
    reason: Dict[str, Any] = {
        "code": code,
        "message_key": message_key,
        "message": _resolve_gate_message(message_key, context),
        "status": status,
    }
    reason.update({k: v for k, v in extra.items()})
    return reason


class TaskGateMixin:
    """Evidence Gate 判定内核与 Profile_Policy_Matrix Mixin 类"""

    def evaluate_evidence_gate(
        self,
        task_id: str,
        profile: str,
        current_contract: Dict[str, Any],
        snapshot_s0: Dict[str, Any],
        verdicts: List[Dict[str, Any]],
        evidences: List[Dict[str, Any]],
        quality_findings: List[Dict[str, Any]],
        independence_policy: str = "required",
        authoritative_time: Optional[float] = None,
        workspace_id: Optional[int] = None,
        implementer_identity: Optional[Dict[str, Any]] = None,
        attestation: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """评估 Evidence Gate 并产出 gate decision

        P3 强化（Req 1.5, 10.2-10.18）：
        - Identity fail-closed：缺失/不完整的 reviewer_identity 排除 verdict clause satisfaction
        - apply session 分离：reviewer session != implementer session
        - attestation 校验：越窗/自签/issuer 不符/被撤销时 verdict/Evidence 判 invalid
        - gate decision 记录 issuer/signing_key_id/issued_at（Req 6.22 对称）

        Returns:
            Gate Decision 字典包含 decision ("pass" / "block"), findings, verifiers_used, timestamp 等
        """
        eval_time = authoritative_time if authoritative_time is not None else time.time()

        # 1. Profile 校验
        profile_key = profile.lower()
        if profile_key not in PROFILE_POLICY_MATRIX:
            return self._make_gate_decision(
                task_id, "block", "ERR_UNKNOWN_PROFILE", f"Unknown profile: {profile}", eval_time
            )

        policy_rules = PROFILE_POLICY_MATRIX[profile_key]

        # 处理 high_risk 对 solo 的恒定拒绝
        effective_independence_policy = independence_policy
        if policy_rules.get("reject_solo_policy") and independence_policy == "solo":
            effective_independence_policy = "required"

        reasons: List[Dict[str, Any]] = []

        # 2. 契约匹配性
        contract_hash = current_contract.get("contract_hash", "")
        if not contract_hash:
            reasons.append({"code": "ERR_NO_CONTRACT_HASH", "message": "Current contract hash is missing"})

        # 3. Quality Findings 检查 (open findings 必须为 0)
        open_findings = [f for f in quality_findings if f.get("status") == "open"]
        if open_findings:
            reasons.append({
                "code": "ERR_UNRESOLVED_FINDINGS",
                "message": f"Found {len(open_findings)} unresolved quality findings",
                "findings_count": len(open_findings),
            })

        # 4. Role Verdicts 评估
        if policy_rules["require_reviewer_verdict"]:
            reviewer_verdicts = [v for v in verdicts if v.get("role") == "reviewer" or v.get("verdict")]
            if not reviewer_verdicts:
                reasons.append({"code": "ERR_MISSING_REVIEWER_VERDICT", "message": "Sealed Reviewer verdict required"})

        if policy_rules["require_tester_verdict"]:
            tester_verdicts = [v for v in verdicts if v.get("role") == "tester"]
            if not tester_verdicts:
                reasons.append({"code": "ERR_MISSING_TESTER_VERDICT", "message": "Tester verdict required"})

        # 5. 独立性规则检查
        if policy_rules["require_independent_review"]:
            if effective_independence_policy == "solo":
                # solo 生效：独立审核按政策豁免
                pass
            else:
                # 检查 session 独立性
                sessions = {v.get("session_id") or v.get("reviewer_id") for v in verdicts if v.get("session_id") or v.get("reviewer_id")}
                if len(sessions) < policy_rules["min_distinct_sessions"]:
                    reasons.append({
                        "code": "ERR_INSUFFICIENT_DISTINCT_SESSIONS",
                        "message": f"Profile requires at least {policy_rules['min_distinct_sessions']} distinct sessions, got {len(sessions)}",
                    })

        # 5.5 P3: Identity fail-closed 检查（Req 1.5, 10.2-10.18）
        # 缺失/不完整的 reviewer_identity 排除 verdict clause satisfaction
        # apply session 必须不同于 Implementer session
        # attestation 越窗/自签/issuer 不符/被撤销时 verdict/Evidence 判 invalid
        attestation_meta: List[Dict[str, str]] = []
        for v in verdicts:
            v_identity = v.get("reviewer_identity") or v.get("identity")
            if not v_identity:
                # Identity 缺失：fail-closed，排除该 verdict
                reasons.append({
                    "code": "ERR_IDENTITY_MISSING",
                    "message": f"Verdict '{v.get('id', '?')}' 缺失 Identity，fail-closed 排除",
                    "verdict_id": v.get("id", ""),
                })
                continue

            # Identity 完整性校验
            agent_id = v_identity.get("agent_id", "")
            session_id = v_identity.get("session_id", "")
            model_id = v_identity.get("model_id", "")
            role = v_identity.get("role", "")
            if not all([agent_id, session_id, model_id, role]):
                reasons.append({
                    "code": "ERR_IDENTITY_INCOMPLETE",
                    "message": f"Verdict '{v.get('id', '?')}' Identity 不完整",
                    "verdict_id": v.get("id", ""),
                })
                continue

            # P3: apply session 分离（Req 1.5, 10.2）
            # reviewer session 必须不同于 implementer session
            if implementer_identity:
                impl_session = implementer_identity.get("session_id", "")
                if session_id and impl_session and session_id == impl_session:
                    reasons.append({
                        "code": "ERR_IDENTITY_SESSION_NOT_SEPARATED",
                        "message": f"Reviewer Session ({session_id}) 等于 Implementer Session",
                        "verdict_id": v.get("id", ""),
                    })

        # P3: attestation 校验（Req 10.8-10.18）
        if attestation is not None:
            try:
                att_ok, att_status, att_reason = self.validate_attestation(
                    attestation,
                    expected_contract_hash=contract_hash,
                    check_time=eval_time,
                )
                if not att_ok:
                    reasons.append({
                        "code": "ERR_ATTESTATION_INVALID",
                        "message": f"Attestation {att_status}: {att_reason}",
                        "attestation_status": att_status,
                    })
                # 记录 attestation 元数据（Req 6.22 对称：issuer/signing_key_id/issued_at）
                attestation_meta.append({
                    "issuer": attestation.get("issuer", ""),
                    "signing_key_id": attestation.get("signing_key_id", ""),
                    "issued_at": str(attestation.get("issued_at", "")),
                    "valid": att_ok,
                })
            except Exception as e:
                reasons.append({
                    "code": "ERR_ATTESTATION_CHECK_FAILED",
                    "message": str(e),
                })

        # 6. Evidence Freshness 与 Verifier 三元组记录
        verifiers_used: List[Dict[str, str]] = []
        for ev in evidences:
            v_triplet = {
                "verifier_name": ev.get("verifier_name", ""),
                "verifier_version": ev.get("verifier_version", ""),
                "config_hash": ev.get("config_hash", ""),
            }
            verifiers_used.append(v_triplet)

            status = ev.get("freshness_status", "fresh")
            if status in ("invalid", "superseded", "stale", "unknown", "unsatisfied"):
                reasons.append({
                    "code": f"ERR_EVIDENCE_{status.upper()}",
                    "message": f"Evidence '{ev.get('id')}' is {status}",
                })

        # 7. 依赖 freshness 与 interface 匹配检查（Req 9.3-9.5）
        #    consumer 的 requires_artifact 依赖：provider artifact 必须 fresh
        #    consumer 的 requires_interface 依赖：必须有匹配的 provider
        if workspace_id is not None:
            dep_issues = self._check_dependency_freshness(
                workspace_id, task_id, current_contract,
            )
            reasons.extend(dep_issues)

        decision_status = "pass" if not reasons else "block"
        independence_note = (
            "independence_exempted_by_policy"
            if effective_independence_policy == "solo"
            else "independence_required"
        )

        return {
            "task_id": task_id,
            "decision": decision_status,
            "profile": profile,
            "contract_hash": contract_hash,
            "snapshot_id": snapshot_s0.get("snapshot_id", ""),
            "independence_policy": effective_independence_policy,
            "independence_note": independence_note,
            "evaluated_at": eval_time,
            "verifiers_used": verifiers_used,
            # P3: attestation 元数据（Req 6.22 对称）——记录每条 verdict/Evidence
            # 所用 attestation 的 issuer 标识、签名密钥标识与 Attestation 签发时间，
            # 使"该记录在那次判定时刻是否已被撤销"可由匹配撤销记录的撤销时间与
            # Revocation_Mode 事后重算，不依赖历史失效事件。
            "attestation_meta": attestation_meta,
            "reasons": reasons,
        }

    def verify_snapshot_integrity(
        self,
        snapshot_s0: Dict[str, Any],
        snapshot_s1: Dict[str, Any],
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """验证 S0 与 S1 快照的一致性 (TOCTOU 防护)

        Returns:
            (is_valid, structured_reason)
        """
        if not snapshot_s0 or not snapshot_s1:
            return False, {
                "code": "ERR_SNAPSHOT_CAPTURE_FAILED",
                "message_key": "snapshot.error.capture_failed",
                "status": "unknown",
            }

        s0_id = snapshot_s0.get("snapshot_id", "")
        s1_id = snapshot_s1.get("snapshot_id", "")

        if not s0_id or not s1_id or s0_id != s1_id:
            return False, {
                "code": "ERR_SNAPSHOT_TOCTOU_DRIFT",
                "message_key": "snapshot.error.drift_detected",
                "status": "stale",
                "s0_id": s0_id,
                "s1_id": s1_id,
            }

        return True, None

    def _make_gate_decision(
        self,
        task_id: str,
        decision: str,
        err_code: str,
        err_msg: str,
        timestamp: float,
    ) -> Dict[str, Any]:
        return {
            "task_id": task_id,
            "decision": decision,
            "evaluated_at": timestamp,
            "reasons": [{"code": err_code, "message": err_msg}],
            "verifiers_used": [],
        }

    def _check_dependency_freshness(
        self,
        workspace_id: int,
        task_id: str,
        current_contract: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """检查 consumer 的依赖 freshness 和 interface 匹配（Req 9.3-9.5）。

        - requires_artifact：provider artifact 必须 fresh（Req 9.3）
        - requires_interface：必须有匹配的 provider（Req 9.5）

        Returns:
            阻断原因列表（空列表表示通过）
        """
        issues: List[Dict[str, Any]] = []
        contract_id = current_contract.get("contract_id", "")
        contract_revision = current_contract.get("revision", 0)

        # 1. requires_artifact：检查 provider artifact freshness（Req 9.3）
        cur = self.conn.execute(
            "SELECT target_ref, target_task_id FROM task_dependencies "
            "WHERE workspace_id = ? AND task_id = ? AND contract_id = ? "
            "AND contract_revision = ? AND dependency_type = ? "
            "AND is_informational = 0",
            (workspace_id, task_id, contract_id, contract_revision,
             "requires_artifact"),
        )
        for row in cur.fetchall():
            target_task_id = row["target_task_id"]
            if not target_task_id:
                continue
            artifact = self.get_artifact_freshness(workspace_id, target_task_id)
            if not artifact or artifact.get("freshness_status") != "fresh":
                issues.append({
                    "code": "ERR_ARTIFACT_NOT_FRESH",
                    "message": f"Artifact from task '{target_task_id}' is not fresh",
                    "target_task_id": target_task_id,
                })

        # 2. requires_interface：检查是否有匹配的 provider（Req 9.5）
        cur = self.conn.execute(
            "SELECT target_ref FROM task_dependencies "
            "WHERE workspace_id = ? AND task_id = ? AND contract_id = ? "
            "AND contract_revision = ? AND dependency_type = ? "
            "AND is_informational = 0",
            (workspace_id, task_id, contract_id, contract_revision,
             "requires_interface"),
        )
        for row in cur.fetchall():
            interface_name = row["target_ref"]
            providers = self.get_interface_providers(workspace_id, interface_name)
            if not providers:
                issues.append({
                    "code": "ERR_INTERFACE_NO_PROVIDER",
                    "message": f"No provider for interface '{interface_name}'",
                    "interface_name": interface_name,
                })

        return issues

    def evaluate_evidence_gate_for_task(
        self,
        task_id: str,
        profile: str = "default",
        identity: Optional[Dict[str, Any]] = None,
        authoritative_time: Optional[float] = None,
    ) -> Dict[str, Any]:
        """为特定 task_id 从 DB 智能组装契约、Verdict、Evidence 与 Findings，物理评估 Evidence Gate

        Fail-closed 语义（Req 1.1/1.8/5.5/6.10/8.3/10.5）：
        - 契约 Envelope 缺失时禁止伪造 HASH-{task_id}/C-{task_id} 占位，直接 block
        - Workspace_Snapshot 缺失时禁止伪造 SNAP-{task_id} 占位，按 unknown 语义 block
        - 自由文本 reviewer_identity 不构成身份证明，禁止据此伪造结构化 Identity，
          按 Identity 缺失 fail-closed（Req 10.5）
        - 调用方声明的 identity 参数不得用于补齐封存 Verdict 的身份（Req 10.5/14.5）

        Args:
            identity: 调用方声明的身份（保留参数，仅作审计元数据用途）；
                不参与 Verdict 身份证明，不用于补齐缺失 Identity 字段。
        """
        dec_time = authoritative_time if authoritative_time is not None else time.time()
        assembly_reasons: List[Dict[str, Any]] = []

        # 1. 查找契约 Envelope（缺失时不得伪造占位，Req 1.1）
        c_row = None
        if self._has_table("task_contract_revisions"):
            cur = self.conn.execute(
                "SELECT envelope_payload, contract_hash FROM task_contract_revisions WHERE task_id = ? ORDER BY revision DESC LIMIT 1",
                (task_id,),
            )
            c_row = cur.fetchone()
        elif self._has_table("contract_envelopes"):
            cur = self.conn.execute(
                "SELECT payload as envelope_payload, hash as contract_hash FROM contract_envelopes WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            )
            c_row = cur.fetchone()

        contract_data: Dict[str, Any] = {}
        if c_row and c_row["envelope_payload"]:
            try:
                parsed = json.loads(c_row["envelope_payload"])
                if isinstance(parsed, dict):
                    contract_data = parsed
            except Exception:
                pass
        if c_row and c_row["contract_hash"]:
            # 仅采用 DB 中真实存储的 contract_hash，禁止伪造 HASH-{task_id} 占位
            contract_data["contract_hash"] = c_row["contract_hash"]
        if not c_row:
            # 缺 Current_Envelope 绑定 → fail-closed（Req 1.1, 8.3）
            assembly_reasons.append(
                _gate_reason(GATE_CONTRACT_ENVELOPE_MISSING, task_id, status="unknown")
            )

        # 2. 查找快照 S0（缺失时不得伪造占位，Req 6.9-6.10）
        s0_data: Dict[str, Any] = {"snapshot_id": ""}
        if self._has_table("task_evidence_events"):
            cur = self.conn.execute(
                "SELECT workspace_snapshot_id FROM task_evidence_events WHERE task_id = ? AND workspace_snapshot_id != '' ORDER BY id DESC LIMIT 1",
                (task_id,),
            )
            s_row = cur.fetchone()
            if s_row and s_row["workspace_snapshot_id"]:
                s0_data["snapshot_id"] = s_row["workspace_snapshot_id"]
        if not s0_data["snapshot_id"]:
            # S0 无法捕获 → unknown + fail-closed（Req 6.10）
            assembly_reasons.append(
                _gate_reason(GATE_SNAPSHOT_UNAVAILABLE, task_id, status="unknown")
            )

        # 3. 查找 verdicts（自由文本 identity 不构成身份证明，Req 10.5）
        verdicts: List[Dict[str, Any]] = []
        if self._has_table("task_verdict_events"):
            cur = self.conn.execute(
                "SELECT verdict_id, phase, clause_results, findings, overall, reviewer_identity FROM task_verdict_events WHERE task_id = ?",
                (task_id,),
            )
            for vr in cur.fetchall():
                v_dict: Dict[str, Any] = {
                    "id": vr["verdict_id"],
                    "phase": vr["phase"],
                    "overall": vr["overall"],
                }
                if vr["clause_results"]:
                    try:
                        v_dict["clause_results"] = json.loads(vr["clause_results"])
                    except Exception:
                        pass
                if vr["findings"]:
                    try:
                        v_dict["findings"] = json.loads(vr["findings"])
                    except Exception:
                        pass
                raw_identity = vr["reviewer_identity"]
                if raw_identity:
                    parsed_identity = None
                    try:
                        candidate = json.loads(raw_identity)
                        if isinstance(candidate, dict):
                            parsed_identity = candidate
                    except Exception:
                        parsed_identity = None
                    if parsed_identity is not None:
                        v_dict["reviewer_identity"] = parsed_identity
                        # 顶层字段投影：判定内核按顶层读取 role/session_id/verdict
                        # （L219 role=="reviewer" 或顶层 verdict 计入 reviewer_verdicts；
                        #  L235 session_id/reviewer_id 计入独立性 session 集合）。
                        # 仅当 reviewer_identity 为结构化 dict（P3 四字段齐全）时，
                        # 才将其 role/session_id 投影到 verdict 顶层，禁止把自由文本
                        # 伪造成结构化身份（Req 10.5）。
                        if parsed_identity.get("role"):
                            v_dict["role"] = parsed_identity["role"]
                        if parsed_identity.get("session_id"):
                            v_dict["session_id"] = parsed_identity["session_id"]
                    else:
                        # 自由文本 reviewer：排除出身份证明（Req 10.5），
                        # 不伪造结构化 Identity；该 verdict 按 Identity 缺失
                        # 由判定内核 fail-closed（ERR_IDENTITY_MISSING）
                        assembly_reasons.append(
                            _gate_reason(
                                GATE_IDENTITY_FREE_TEXT_EXCLUDED,
                                task_id,
                                status="invalid",
                                verdict_id=vr["verdict_id"],
                            )
                        )
                if vr["overall"]:
                    # 顶层 verdict：该记录确为一条 verdict 事件（存在性判定，
                    # 判定内核按顶层 verdict 字段计入 reviewer_verdicts）。
                    # 取值来自 DB 真实存储的 overall（approved/rejected/needs_changes/
                    # unclear 或 pass/request_changes/block/abstain），不伪造占位。
                    v_dict["verdict"] = vr["overall"]
                # 注意：调用方声明的 identity 参数不用于补齐 Verdict 身份
                # （Req 10.5：自由文本与客户端声明字段不得作为身份证明）
                verdicts.append(v_dict)
        if not verdicts:
            assembly_reasons.append(
                _gate_reason(GATE_VERDICT_ABSENT, task_id, status="unknown")
            )

        # 4. 查找 evidences
        evidences: List[Dict[str, Any]] = []
        if self._has_table("task_evidence_events"):
            cur = self.conn.execute(
                "SELECT evidence_id as id, verifier_name, verifier_version, verifier_config_hash as config_hash FROM task_evidence_events WHERE task_id = ? AND event_type != 'evidence_invalidated'",
                (task_id,),
            )
            evidences = [dict(r) for r in cur.fetchall()]
        if not evidences:
            assembly_reasons.append(
                _gate_reason(GATE_EVIDENCE_ABSENT, task_id, status="unknown")
            )

        # 5. 查找 quality findings
        quality_findings: List[Dict[str, Any]] = []
        if self._has_table("task_quality_findings"):
            cur = self.conn.execute(
                "SELECT id, status FROM task_quality_findings WHERE task_id = ?",
                (task_id,),
            )
            quality_findings = [dict(r) for r in cur.fetchall()]

        # implementer Identity 查询（Req 10.5）：action_identities 表缺失
        # （schema 降级/迁移中）时不得抛裸异常，按 Identity 缺失处理，
        # 由判定内核 fail-closed（ERR_IDENTITY_MISSING）
        impl_identity = None
        if hasattr(self, "get_task_identity_by_role") and self._has_table("action_identities"):
            impl_identity = self.get_task_identity_by_role(task_id, "implementer")

        res = self.evaluate_evidence_gate(
            task_id=task_id,
            profile=profile,
            current_contract=contract_data,
            snapshot_s0=s0_data,
            verdicts=verdicts,
            evidences=evidences,
            quality_findings=quality_findings,
            authoritative_time=authoritative_time,
            implementer_identity=impl_identity,
        )

        # 合并组装层 fail-closed 原因：任一数据缺失原因都强制 block（Req 1.8）
        if assembly_reasons:
            res["reasons"] = assembly_reasons + list(res.get("reasons") or [])
            res["decision"] = "block"

        # 保存判定到 task_gate_decisions（缺失绑定时以空串/0 诚实落盘，
        # 禁止伪造 C-{task_id} 占位或虚构 revision）
        if self._has_table("task_gate_decisions"):
            contract_id = str(contract_data.get("contract_id") or "")
            try:
                rev = int(contract_data.get("revision", 0))
            except (TypeError, ValueError):
                rev = 0
            dec_id = f"GDEC-{task_id}-{int(dec_time * 1000)}-{uuid.uuid4().hex[:8]}"
            self.conn.execute(
                """
                INSERT INTO task_gate_decisions
                    (decision_id, task_id, contract_id, contract_revision, contract_hash, decision, reason, decision_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dec_id,
                    task_id,
                    contract_id,
                    rev,
                    contract_data.get("contract_hash", ""),
                    res["decision"],
                    json.dumps(res, ensure_ascii=False),
                    dec_time,
                ),
            )
            self.conn.commit()

        return res

    def _has_table(self, table_name: str) -> bool:
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return cur.fetchone() is not None

