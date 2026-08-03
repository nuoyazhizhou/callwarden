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
from typing import Any, Dict, List, Optional, Tuple, Set


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
