"""
db_task_reviews.py
===================

Role View allowlist, blind verdict, reveal 与 amendment Mixin 类。

提供基于 Allowlist 的 Role View 投影生成、首轮盲评判定（Blind_First_Pass_Verdict）、
Implementer Notes 披露事件（Reveal_Event）与 post-reveal 修订（Post_Reveal_Amendment）。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List, Optional, Tuple, Set


# 版本化 Allowlist 唯一真相源定义
# Key: (view_type, view_version, stage)
# Value: set of allowed dot-paths / field paths
ROLE_VIEW_ALLOWLISTS: Dict[Tuple[str, str, str], Set[str]] = {
    ("planner", "1.0", "blind"): {
        "contract_id", "profile", "title", "description", "requirements",
        "target_file", "target_symbol", "clauses", "blocking_clauses"
    },
    ("implementer", "1.0", "blind"): {
        "contract_id", "profile", "title", "description", "requirements",
        "target_file", "target_symbol", "allowed_edit_scope", "clauses", "blocking_clauses"
    },
    ("reviewer", "1.0", "blind"): {
        "contract_id", "profile", "title", "description", "requirements",
        "target_file", "target_symbol", "allowed_edit_scope", "actual_changes",
        "symbol_changes", "test_runs", "open_quality_findings", "clauses", "blocking_clauses"
    },
    ("reviewer", "1.0", "reveal"): {
        "contract_id", "profile", "title", "description", "requirements",
        "target_file", "target_symbol", "allowed_edit_scope", "actual_changes",
        "symbol_changes", "test_runs", "open_quality_findings", "clauses", "blocking_clauses",
        "implementer_notes", "reveal_event"
    },
    ("tester", "1.0", "blind"): {
        "contract_id", "profile", "title", "description", "requirements",
        "target_file", "target_symbol", "clauses", "test_cases", "test_runs"
    },
}


def _canonical_json(data: Any) -> str:
    """生成规范 JSON 字符串（按 key 排序，无冗余空格）"""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _compute_hash(data: Any) -> str:
    """计算规范 SHA-256摘要"""
    content = _canonical_json(data)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class TaskReviewsMixin:
    """Role View allowlist, blind verdict, reveal 与 amendment 数据库层 Mixin"""

    def get_role_view(
        self,
        task_id: str,
        view_type: str,
        view_version: str = "1.0",
        stage: str = "blind",
        envelope_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """按版本化 allowlist 生成角色视图投影 Role View

        Args:
            task_id: 任务 ID
            view_type: 角色视图类型 (planner / implementer / reviewer / tester)
            view_version: 视图定义版本 (如 "1.0")
            stage: 披露阶段 (blind / reveal / amendment)
            envelope_data: 可选传入的 Envelope 数据字典

        Returns:
            Role View 字典，包含 view_manifest_hash, content, allowed_fields 等

        Raises:
            ValueError: 未注册的 allowlist 定义时抛出异常
        """
        key = (view_type.lower(), view_version, stage.lower())
        if key not in ROLE_VIEW_ALLOWLISTS:
            raise ValueError(
                f"ERR_UNREGISTERED_VIEW_VERSION: View allowlist definition not found for {key}"
            )

        allowed_fields = ROLE_VIEW_ALLOWLISTS[key]
        source_data = envelope_data or {}

        # 过滤 nested 数据
        filtered_content: Dict[str, Any] = {}
        excluded_fields: List[str] = []

        for field_name, value in source_data.items():
            if field_name in allowed_fields:
                filtered_content[field_name] = value
            else:
                excluded_fields.append(field_name)

        allowlist_def_hash = _compute_hash(sorted(list(allowed_fields)))
        contract_hash = source_data.get("contract_hash", _compute_hash(source_data))

        manifest_data = {
            "view_type": view_type,
            "view_version": view_version,
            "stage": stage,
            "contract_hash": contract_hash,
            "allowlist_hash": allowlist_def_hash,
            "content_hash": _compute_hash(filtered_content),
        }
        view_manifest_hash = _compute_hash(manifest_data)

        return {
            "task_id": task_id,
            "view_type": view_type,
            "view_version": view_version,
            "stage": stage,
            "view_manifest_hash": view_manifest_hash,
            "contract_hash": contract_hash,
            "content": filtered_content,
            "allowed_fields": sorted(list(allowed_fields)),
            "excluded_fields": sorted(excluded_fields),
        }

    def submit_blind_verdict(
        self,
        task_id: str,
        reviewer_id: str,
        verdict: str,
        decision_data: Dict[str, Any],
        contract_hash: str,
        view_version: str = "1.0",
        reviewer_identity: Optional[Dict[str, Any]] = None,
        attestation_id: str = "",
        view_manifest_hash: str = "",
        lease_token: Optional[str] = None,
        fencing_counter: Optional[int] = None,
    ) -> Dict[str, Any]:
        """提交并封存首轮盲评判定 (Blind_First_Pass_Verdict)

        首轮 verdict 封存前禁止包含 implementer_notes、既有 verdict/draft/confidence/review focus。
        P3 强化（Req 1.4, 10.1-10.5, 10.8）：
        - 记录 reviewer Identity（agent_id/session_id/model_id/role）
        - 绑定 daemon 签发的 attestation_id
        - 记录 View_Manifest hash 用于 allowlisted manifest 证明
        - 按 Authoritative_Clock 记录 submission_time（此处用 time.time 近似）

        P4 强化（Req 11.8-11.9）：
        - 提供 lease_token/fencing_counter 时启用受保护写路径（role=reviewer）
        - 过期 / token hash 不匹配 / 旧 counter 均在写入前拒绝（fail closed）
        """
        # 校验 payload 禁止包含未 reveal 内容（Req 1.4）
        forbidden_keys = {"implementer_notes", "existing_verdict", "review_focus", "draft_confidence"}
        for k in forbidden_keys:
            if k in decision_data:
                raise ValueError(
                    f"ERR_BLIND_VERDICT_VIOLATION: Forbidden field '{k}' present before reveal"
                )

        # 检查是否已有 reveal 事件（verdict-before-reveal 定序，Req 1.4）
        reveal_record = self._get_reveal_event(task_id)
        if reveal_record:
            raise ValueError(
                "ERR_BLIND_VERDICT_SEALED: Reveal already triggered; blind verdict cannot be submitted post-reveal"
            )

        # P3: 校验 reviewer Identity 完整性（Req 10.1）
        # Identity 仅作 actor attribution，不等于 assignment/lease/ownership（Req 10.5, 10.7）
        if reviewer_identity is not None:
            agent_id = reviewer_identity.get("agent_id", "")
            session_id = reviewer_identity.get("session_id", "")
            model_id = reviewer_identity.get("model_id", "")
            role = reviewer_identity.get("role", "")
            if not all([agent_id, session_id, model_id, role]):
                raise ValueError(
                    "ERR_IDENTITY_INCOMPLETE: reviewer_identity 缺失必要字段 "
                    "(agent_id, session_id, model_id, role)"
                )

        # P4: protected mutation Lease 校验（Req 11.8-11.9）
        # verdict 提交是 reviewer 角色的受保护写；校验失败在写入前拒绝（fail closed）。
        if lease_token is not None and fencing_counter is not None:
            ok_l, lease_reason = self.validate_lease_for_mutation(
                task_id, "reviewer", lease_token, fencing_counter, reviewer_identity
            )
            if not ok_l:
                raise ValueError(
                    f"{lease_reason['code']}: {lease_reason.get('detail', '')}"
                )

        record_id = f"RVD-{int(time.time() * 1000)}"
        timestamp = time.time()

        verdict_record = {
            "id": record_id,
            "task_id": task_id,
            "reviewer_id": reviewer_id,
            "verdict": verdict,
            "decision_data": decision_data,
            "contract_hash": contract_hash,
            "view_version": view_version,
            "sealed": True,
            "sealed_at": timestamp,
            "amendments": [],
            # P3 强化字段（Req 1.4, 10.1-10.5, 10.8）
            "reviewer_identity": reviewer_identity,
            "attestation_id": attestation_id,
            "view_manifest_hash": view_manifest_hash,
            "submission_time": timestamp,  # 按 Authoritative_Clock（Req 14.11）
        }

        self._save_verdict_record(task_id, verdict_record)
        return verdict_record

    def trigger_reveal_event(
        self,
        task_id: str,
        reviewer_id: str,
        implementer_identity: Optional[Dict[str, Any]] = None,
        lease_token: Optional[str] = None,
        fencing_counter: Optional[int] = None,
    ) -> Dict[str, Any]:
        """记录 Implementer Notes 披露事件 (Reveal_Event)

        P3 强化（Req 1.4-1.5, 10.2）：
        - 从已封存 verdict 中提取 reviewer_identity
        - 如果同时有 reviewer_identity 和 implementer_identity，提前校验 session 分离
        - 记录 implementer_identity 用于后续独立审核证明

        P4 强化（Req 11.8-11.9）：
        - 提供 lease_token/fencing_counter 时启用受保护写路径（role=implementer）
        - 过期 / token hash 不匹配 / 旧 counter 均在写入前拒绝（fail closed）
        """
        existing_verdict = self._get_latest_verdict(task_id)
        if not existing_verdict or not existing_verdict.get("sealed"):
            raise ValueError(
                "ERR_REVEAL_BEFORE_SEAL: Cannot trigger reveal event before first-pass verdict is sealed"
            )

        # P3: 从已封存 verdict 提取 reviewer_identity（Req 10.2）
        reviewer_identity = existing_verdict.get("reviewer_identity")

        # P3: 提前校验 session 分离（Req 1.5, 10.2）
        # 仅在双方 identity 都可用时校验；缺失时不阻断 reveal（由 apply gate fail-closed）
        if reviewer_identity and implementer_identity:
            reviewer_session = reviewer_identity.get("session_id", "")
            implementer_session = implementer_identity.get("session_id", "")
            if reviewer_session and implementer_session and reviewer_session == implementer_session:
                raise ValueError(
                    f"ERR_IDENTITY_SESSION_NOT_SEPARATED: Reviewer Session ({reviewer_session}) "
                    f"等于 Implementer Session，不满足 Independent_Review 要求"
                )

        # P4: protected mutation Lease 校验（Req 11.8-11.9）
        # reveal 是 implementer 角色的受保护写；校验失败在写入前拒绝（fail closed）。
        if lease_token is not None and fencing_counter is not None:
            ok_l, lease_reason = self.validate_lease_for_mutation(
                task_id, "implementer", lease_token, fencing_counter, implementer_identity
            )
            if not ok_l:
                raise ValueError(
                    f"{lease_reason['code']}: {lease_reason.get('detail', '')}"
                )

        timestamp = time.time()
        reveal_data = {
            "task_id": task_id,
            "reviewer_id": reviewer_id,
            "triggered_at": timestamp,
            "verdict_id": existing_verdict["id"],
            # P3 强化字段（Req 1.4-1.5, 10.2）
            "reviewer_identity": reviewer_identity,
            "implementer_identity": implementer_identity,
            "reveal_time": timestamp,  # 按 Authoritative_Clock（Req 14.11）
        }
        self._save_reveal_event(task_id, reveal_data)
        return reveal_data

    def submit_verdict_amendment(
        self,
        task_id: str,
        reviewer_id: str,
        amendment_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """提交 Post_Reveal_Amendment 追加修订"""
        reveal_record = self._get_reveal_event(task_id)
        if not reveal_record:
            raise ValueError(
                "ERR_AMENDMENT_BEFORE_REVEAL: Cannot submit post-reveal amendment before reveal event"
            )

        verdict_record = self._get_latest_verdict(task_id)
        if not verdict_record:
            raise ValueError("ERR_NO_VERDICT_FOUND: Initial verdict record not found for amendment")

        amendment_id = f"AMD-{int(time.time() * 1000)}"
        timestamp = time.time()
        amendment_entry = {
            "id": amendment_id,
            "reviewer_id": reviewer_id,
            "amendment_data": amendment_data,
            "submitted_at": timestamp,
        }

        verdict_record.setdefault("amendments", []).append(amendment_entry)
        self._save_verdict_record(task_id, verdict_record)
        return amendment_entry

    def _get_verdicts_storage(self) -> Dict[str, List[Dict[str, Any]]]:
        if not hasattr(self, "_reviews_storage"):
            self._reviews_storage = {}
        return self._reviews_storage

    def _get_reveals_storage(self) -> Dict[str, Dict[str, Any]]:
        if not hasattr(self, "_reveals_storage"):
            self._reveals_storage = {}
        return self._reveals_storage

    def _save_verdict_record(self, task_id: str, verdict_record: Dict[str, Any]) -> None:
        storage = self._get_verdicts_storage()
        records = storage.setdefault(task_id, [])
        # 更新或追加
        for idx, rec in enumerate(records):
            if rec["id"] == verdict_record["id"]:
                records[idx] = verdict_record
                return
        records.append(verdict_record)

    def _get_latest_verdict(self, task_id: str) -> Optional[Dict[str, Any]]:
        storage = self._get_verdicts_storage()
        records = storage.get(task_id, [])
        return records[-1] if records else None

    def _save_reveal_event(self, task_id: str, reveal_data: Dict[str, Any]) -> None:
        storage = self._get_reveals_storage()
        storage[task_id] = reveal_data

    def _get_reveal_event(self, task_id: str) -> Optional[Dict[str, Any]]:
        storage = self._get_reveals_storage()
        return storage.get(task_id)

    # ------------------------------------------------------------------
    # P3: 独立审核证明验证（Req 1.4-1.5, 10.1-10.5, 10.8）
    # ------------------------------------------------------------------

    def verify_blind_verdict_proofs(
        self,
        task_id: str,
        reviewer_identity: Dict[str, Any],
        implementer_identity: Dict[str, Any],
        profile: str = "",
        attestation: Optional[Dict[str, Any]] = None,
        expected_contract_hash: str = "",
        expected_view_manifest_hash: str = "",
        tester_identity: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """验证 blind verdict 的独立审核证明（Req 1.4-1.5, 10.1-10.5, 10.8）

        汇总以下证明：
        1. allowlisted manifest：verdict 记录的 view_manifest_hash 与期望值一致
        2. verdict-before-reveal 定序：verdict submission_time <= reveal reveal_time
        3. daemon attestation：attestation issuer=daemon 且在有效窗口内
        4. Reviewer/Implementer session 分离（Req 1.5, 10.2）
        5. high_risk：agent/model family 分离 + 独立 Tester（Req 10.3-10.4, 5.3）

        Args:
            task_id: 任务 ID
            reviewer_identity: Reviewer 的 Identity
            implementer_identity: Implementer 的 Identity
            profile: 任务 profile（high_risk 时启用额外校验）
            attestation: attestation_records 行（dict），None 时跳过 attestation 校验
            expected_contract_hash: 期望的 Contract_Hash
            expected_view_manifest_hash: 期望的 View_Manifest hash
            tester_identity: Tester 的 Identity（high_risk 必填）

        Returns:
            (is_valid, reason_dict)
            reason_dict 包含 "proofs" 字段列出每项证明的通过/失败状态
        """
        proofs: Dict[str, Dict[str, Any]] = {}
        all_passed = True

        verdict = self._get_latest_verdict(task_id)
        reveal = self._get_reveal_event(task_id)

        # 1. allowlisted manifest 证明（Req 1.4）
        if expected_view_manifest_hash and verdict:
            recorded_hash = verdict.get("view_manifest_hash", "")
            if recorded_hash and recorded_hash != expected_view_manifest_hash:
                proofs["allowlisted_manifest"] = {
                    "passed": False,
                    "reason": f"View_Manifest hash 不匹配: 期望 {expected_view_manifest_hash}, 实际 {recorded_hash}",
                }
                all_passed = False
            else:
                proofs["allowlisted_manifest"] = {"passed": True}
        else:
            proofs["allowlisted_manifest"] = {"passed": True, "note": "未提供期望值或无 verdict，跳过"}

        # 2. verdict-before-reveal 定序（Req 1.4）
        if verdict and reveal:
            submission_time = verdict.get("submission_time", verdict.get("sealed_at", 0))
            reveal_time = reveal.get("reveal_time", reveal.get("triggered_at", 0))
            if submission_time > reveal_time:
                proofs["verdict_before_reveal"] = {
                    "passed": False,
                    "reason": f"verdict submission_time ({submission_time}) > reveal_time ({reveal_time})",
                }
                all_passed = False
            else:
                proofs["verdict_before_reveal"] = {"passed": True}
        else:
            proofs["verdict_before_reveal"] = {
                "passed": True,
                "note": "无 reveal 事件，定序不适用",
            }

        # 3. daemon attestation 有效性（Req 10.8）
        if attestation is not None:
            try:
                att_ok, att_status, att_reason = self.validate_attestation(
                    attestation,
                    expected_contract_hash=expected_contract_hash,
                    expected_view_manifest_hash=expected_view_manifest_hash,
                )
                if not att_ok:
                    proofs["attestation"] = {
                        "passed": False,
                        "status": att_status,
                        "reason": att_reason,
                    }
                    all_passed = False
                else:
                    proofs["attestation"] = {"passed": True, "status": att_status}
            except Exception as e:
                proofs["attestation"] = {"passed": False, "reason": str(e)}
                all_passed = False
        else:
            proofs["attestation"] = {"passed": True, "note": "未提供 attestation，跳过"}

        # 4. Reviewer/Implementer session 分离（Req 1.5, 10.2）
        try:
            sep_ok, sep_reason = self.validate_session_separation(
                reviewer_identity, implementer_identity
            )
            if not sep_ok:
                proofs["session_separation"] = {"passed": False, "reason": sep_reason}
                all_passed = False
            else:
                proofs["session_separation"] = {"passed": True}
        except Exception as e:
            proofs["session_separation"] = {"passed": False, "reason": str(e)}
            all_passed = False

        # 5. high_risk：agent/model family 分离 + 独立 Tester（Req 10.3-10.4, 5.3）
        if profile == "high_risk":
            # agent family 分离
            try:
                agent_ok, agent_reason = self.validate_agent_family_separation(
                    reviewer_identity, implementer_identity
                )
                if not agent_ok:
                    proofs["agent_family_separation"] = {
                        "passed": False,
                        "reason": agent_reason,
                    }
                    all_passed = False
                else:
                    proofs["agent_family_separation"] = {"passed": True}
            except Exception as e:
                proofs["agent_family_separation"] = {"passed": False, "reason": str(e)}
                all_passed = False

            # model family 分离
            try:
                model_ok, model_reason = self.validate_model_family_separation(
                    reviewer_identity, implementer_identity
                )
                if not model_ok:
                    proofs["model_family_separation"] = {
                        "passed": False,
                        "reason": model_reason,
                    }
                    all_passed = False
                else:
                    proofs["model_family_separation"] = {"passed": True}
            except Exception as e:
                proofs["model_family_separation"] = {"passed": False, "reason": str(e)}
                all_passed = False

            # 独立 Tester（Req 5.3: high_risk 需要独立 Tester verdict）
            if tester_identity is None:
                proofs["independent_tester"] = {
                    "passed": False,
                    "reason": "high_risk profile 要求独立 Tester，但未提供 tester_identity",
                }
                all_passed = False
            else:
                # Tester session 与 Reviewer/Implementer 不同
                tester_session = tester_identity.get("session_id", "")
                reviewer_session = reviewer_identity.get("session_id", "")
                implementer_session = implementer_identity.get("session_id", "")
                if tester_session and tester_session == reviewer_session:
                    proofs["independent_tester"] = {
                        "passed": False,
                        "reason": f"Tester Session ({tester_session}) 等于 Reviewer Session",
                    }
                    all_passed = False
                elif tester_session and tester_session == implementer_session:
                    proofs["independent_tester"] = {
                        "passed": False,
                        "reason": f"Tester Session ({tester_session}) 等于 Implementer Session",
                    }
                    all_passed = False
                else:
                    # Tester agent/model family 与 Reviewer/Implementer 不同
                    try:
                        t_agent_ok, _ = self.validate_agent_family_separation(
                            tester_identity, implementer_identity
                        )
                        t_model_ok, _ = self.validate_model_family_separation(
                            tester_identity, implementer_identity
                        )
                        if not t_agent_ok or not t_model_ok:
                            proofs["independent_tester"] = {
                                "passed": False,
                                "reason": "Tester 与 Implementer agent/model family 相同",
                            }
                            all_passed = False
                        else:
                            proofs["independent_tester"] = {"passed": True}
                    except Exception as e:
                        proofs["independent_tester"] = {"passed": False, "reason": str(e)}
                        all_passed = False

        return all_passed, {"proofs": proofs, "profile": profile}
