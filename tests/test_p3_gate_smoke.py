"""P3 Evidence Gate Identity fail-closed smoke test（临时验证脚本）

验证 evaluate_evidence_gate 的 P3 强化逻辑：
- Identity 缺失/不完整 → fail-closed 排除 verdict
- Reviewer/Implementer session 未分离 → 阻断
- Attestation 无效（issuer 非 daemon / 越窗 / 被撤销）→ 阻断
- gate decision 记录 attestation_meta（issuer/signing_key_id/issued_at/valid）

验证后可删除。
"""
import os
import sys
import tempfile
import time

# 项目根的父目录（c:\git_work）在 path，callwarden 是包名
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)


def _make_identity(agent_id, session_id, model_id, role):
    return {
        "agent_id": agent_id,
        "session_id": session_id,
        "model_id": model_id,
        "role": role,
    }


def main():
    from callwarden.db import CodeGraphDB
    from callwarden.db.db_task_gate import TaskGateMixin
    print("[setup] 导入检查通过")

    assert issubclass(CodeGraphDB, TaskGateMixin), "CodeGraphDB 未继承 TaskGateMixin"

    os.environ["CW_USE_RUST_STORAGE"] = "0"
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        os.environ["CALLWARDEN_DB_PATH"] = db_path
        db = CodeGraphDB(db_path)
        try:
            db.register_workspace("test-ws", tmp)
            db.set_active_workspace("test-ws")

            now = time.time()
            contract = {"contract_hash": "hash-001", "contract_id": "C-001", "revision": 1}
            snapshot_s0 = {"snapshot_id": "S0-001"}

            # ---------- 1. Identity 缺失 → fail-closed ----------
            verdicts_no_identity = [
                {"id": "V-001", "verdict": "approve", "role": "reviewer", "session_id": "sess-rev-001"}
            ]
            decision = db.evaluate_evidence_gate(
                task_id="T-gate-001",
                profile="default",
                current_contract=contract,
                snapshot_s0=snapshot_s0,
                verdicts=verdicts_no_identity,
                evidences=[],
                quality_findings=[],
            )
            assert decision["decision"] == "block", f"Identity 缺失应 block, 实际 {decision['decision']}"
            codes = [r["code"] for r in decision["reasons"]]
            assert "ERR_IDENTITY_MISSING" in codes, f"应含 ERR_IDENTITY_MISSING, 实际 {codes}"
            print("[1/7] Identity 缺失 → fail-closed 阻断通过")

            # ---------- 2. Identity 不完整 → fail-closed ----------
            verdicts_incomplete = [
                {
                    "id": "V-002",
                    "verdict": "approve",
                    "role": "reviewer",
                    "reviewer_identity": _make_identity("agent-1", "", "glm-5.2", "reviewer"),  # 缺 session_id
                }
            ]
            decision = db.evaluate_evidence_gate(
                task_id="T-gate-002",
                profile="default",
                current_contract=contract,
                snapshot_s0=snapshot_s0,
                verdicts=verdicts_incomplete,
                evidences=[],
                quality_findings=[],
            )
            assert decision["decision"] == "block"
            codes = [r["code"] for r in decision["reasons"]]
            assert "ERR_IDENTITY_INCOMPLETE" in codes, f"应含 ERR_IDENTITY_INCOMPLETE, 实际 {codes}"
            print("[2/7] Identity 不完整 → fail-closed 阻断通过")

            # ---------- 3. Reviewer/Implementer session 未分离 → 阻断 ----------
            same_session = "sess-same-001"
            reviewer_id = _make_identity("agent-rev", same_session, "glm-5.2", "reviewer")
            implementer_id = _make_identity("agent-impl", same_session, "glm-5.2", "implementer")
            verdicts_same_session = [
                {"id": "V-003", "verdict": "approve", "role": "reviewer", "reviewer_identity": reviewer_id}
            ]
            decision = db.evaluate_evidence_gate(
                task_id="T-gate-003",
                profile="default",
                current_contract=contract,
                snapshot_s0=snapshot_s0,
                verdicts=verdicts_same_session,
                evidences=[],
                quality_findings=[],
                implementer_identity=implementer_id,
            )
            assert decision["decision"] == "block"
            codes = [r["code"] for r in decision["reasons"]]
            assert "ERR_IDENTITY_SESSION_NOT_SEPARATED" in codes, f"应含 ERR_IDENTITY_SESSION_NOT_SEPARATED, 实际 {codes}"
            print("[3/7] Session 未分离 → 阻断通过")

            # ---------- 4. Attestation issuer 非 daemon → 阻断 ----------
            reviewer_ok = _make_identity("agent-rev-2", "sess-rev-002", "glm-5.2", "reviewer")
            implementer_ok = _make_identity("agent-impl-2", "sess-impl-002", "glm-5.2", "implementer")
            verdicts_ok = [
                {"id": "V-004", "verdict": "approve", "role": "reviewer", "reviewer_identity": reviewer_ok}
            ]
            attestation_wrong_issuer = {
                "issuer": "client-self",  # 非 daemon
                "signing_key_id": "key-001",
                "issued_at": now,
                "valid_from": now - 60,
                "valid_until": now + 3600,
                "contract_hash": "hash-001",
                "peer_identity": "peer-001",
                "workspace_id": db._get_active_workspace_id(),
            }
            decision = db.evaluate_evidence_gate(
                task_id="T-gate-004",
                profile="default",
                current_contract=contract,
                snapshot_s0=snapshot_s0,
                verdicts=verdicts_ok,
                evidences=[],
                quality_findings=[],
                implementer_identity=implementer_ok,
                attestation=attestation_wrong_issuer,
            )
            assert decision["decision"] == "block"
            codes = [r["code"] for r in decision["reasons"]]
            assert "ERR_ATTESTATION_INVALID" in codes, f"应含 ERR_ATTESTATION_INVALID, 实际 {codes}"
            print("[4/7] Attestation issuer 非 daemon → 阻断通过")

            # ---------- 5. Attestation 被撤销 (compromised) → 阻断 ----------
            # 先签发一个合法 attestation
            ok, issue_result = db.issue_attestation(
                action_id="ACT-gate-005",
                signing_key_id="key-gate-005",
                peer_identity="peer-gate-005",
                contract_hash="hash-001",
                signature="sig-005",
                valid_from=now - 60,
                valid_until=now + 3600,
            )
            assert ok, f"issue_attestation 失败: {issue_result}"

            # 撤销（compromised 模式）
            ok, rev_result = db.register_attestation_revocation(
                issuer="daemon",
                signing_key_id="key-gate-005",
                revocation_mode="compromised",
                revocation_reason="gate smoke test",
                initiating_actor="admin",
            )
            assert ok, f"register_attestation_revocation 失败: {rev_result}"

            attestation_revoked = {
                "issuer": "daemon",
                "signing_key_id": "key-gate-005",
                "issued_at": now,
                "valid_from": now - 60,
                "valid_until": now + 3600,
                "contract_hash": "hash-001",
                "peer_identity": "peer-gate-005",
                "workspace_id": db._get_active_workspace_id(),
            }
            decision = db.evaluate_evidence_gate(
                task_id="T-gate-005",
                profile="default",
                current_contract=contract,
                snapshot_s0=snapshot_s0,
                verdicts=verdicts_ok,
                evidences=[],
                quality_findings=[],
                implementer_identity=implementer_ok,
                attestation=attestation_revoked,
            )
            assert decision["decision"] == "block"
            codes = [r["code"] for r in decision["reasons"]]
            assert "ERR_ATTESTATION_INVALID" in codes, f"撤销后应含 ERR_ATTESTATION_INVALID, 实际 {codes}"
            print("[5/7] Attestation 被撤销 (compromised) → 阻断通过")

            # ---------- 6. 全部合法 → gate pass + attestation_meta 记录 ----------
            ok, issue_result2 = db.issue_attestation(
                action_id="ACT-gate-006",
                signing_key_id="key-gate-006",
                peer_identity="peer-gate-006",
                contract_hash="hash-001",
                signature="sig-006",
                valid_from=now - 60,
                valid_until=now + 3600,
            )
            assert ok, f"issue_attestation (pass case) 失败: {issue_result2}"

            attestation_valid = {
                "issuer": "daemon",
                "signing_key_id": "key-gate-006",
                "issued_at": now,
                "valid_from": now - 60,
                "valid_until": now + 3600,
                "contract_hash": "hash-001",
                "peer_identity": "peer-gate-006",
                "workspace_id": db._get_active_workspace_id(),
            }
            decision = db.evaluate_evidence_gate(
                task_id="T-gate-006",
                profile="default",
                current_contract=contract,
                snapshot_s0=snapshot_s0,
                verdicts=verdicts_ok,
                evidences=[],
                quality_findings=[],
                implementer_identity=implementer_ok,
                attestation=attestation_valid,
            )
            # 检查是否有非 Identity 相关的阻断原因（contract_hash 已提供，findings 为空，
            # reviewer verdict 存在；但可能有独立性检查的 session 数量问题）
            # 关键验证：不应有 ERR_IDENTITY_* 或 ERR_ATTESTATION_* 原因
            codes = [r["code"] for r in decision["reasons"]]
            identity_att_codes = [c for c in codes if c.startswith("ERR_IDENTITY") or c.startswith("ERR_ATTESTATION")]
            assert not identity_att_codes, f"合法场景不应有 Identity/Attestation 错误, 实际 {identity_att_codes}"
            print(f"[6/7] 全部合法 → 无 Identity/Attestation 阻断通过 (decision={decision['decision']}, codes={codes})")

            # ---------- 7. gate decision 记录 attestation_meta ----------
            meta = decision.get("attestation_meta", [])
            assert len(meta) > 0, f"attestation_meta 应非空, 实际 {meta}"
            entry = meta[0]
            assert entry["issuer"] == "daemon", f"issuer 应为 daemon, 实际 {entry.get('issuer')}"
            assert entry["signing_key_id"] == "key-gate-006", f"signing_key_id 不匹配, 实际 {entry.get('signing_key_id')}"
            assert "issued_at" in entry, f"attestation_meta 缺 issued_at: {entry}"
            assert entry["valid"] is True, f"合法 attestation 应 valid=True, 实际 {entry.get('valid')}"
            print(f"[7/7] attestation_meta 记录通过: issuer={entry['issuer']}, key={entry['signing_key_id']}, valid={entry['valid']}")

            # ---------- bonus: 越窗 attestation → 阻断 ----------
            attestation_expired = {
                "issuer": "daemon",
                "signing_key_id": "key-gate-006",
                "issued_at": now - 7200,
                "valid_from": now - 7200,
                "valid_until": now - 3600,  # 已过期
                "contract_hash": "hash-001",
                "peer_identity": "peer-gate-006",
                "workspace_id": db._get_active_workspace_id(),
            }
            decision = db.evaluate_evidence_gate(
                task_id="T-gate-007",
                profile="default",
                current_contract=contract,
                snapshot_s0=snapshot_s0,
                verdicts=verdicts_ok,
                evidences=[],
                quality_findings=[],
                implementer_identity=implementer_ok,
                attestation=attestation_expired,
                authoritative_time=now,
            )
            codes = [r["code"] for r in decision["reasons"]]
            assert "ERR_ATTESTATION_INVALID" in codes, f"越窗应含 ERR_ATTESTATION_INVALID, 实际 {codes}"
            print("[bonus] 越窗 attestation → 阻断通过")

        finally:
            db.close()

    print("\n=== ALL P3 GATE SMOKE TESTS PASSED ===")


if __name__ == "__main__":
    main()
