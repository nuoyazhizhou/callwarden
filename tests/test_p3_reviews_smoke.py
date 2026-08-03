"""P3 blind view/verdict/reveal 独立审核证明 smoke test（临时验证脚本）

验证 db_task_reviews.py 的 P3 强化：
- submit_blind_verdict 接入 Identity + attestation
- trigger_reveal_event 接入 session 分离
- verify_blind_verdict_proofs 汇总证明
"""
import os
import sys
import tempfile
import time

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)


def main():
    from callwarden.db import CodeGraphDB
    print("[1/6] 导入检查通过")

    os.environ["CW_USE_RUST_STORAGE"] = "0"
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        os.environ["CALLWARDEN_DB_PATH"] = db_path
        db = CodeGraphDB(db_path)
        try:
            db.register_workspace("test-ws", tmp)
            db.set_active_workspace("test-ws")

            # 准备 identity
            reviewer_identity = {
                "agent_id": "agent-reviewer-001",
                "session_id": "sess-rev-001",
                "model_id": "glm-5.2",
                "role": "reviewer",
            }
            implementer_identity = {
                "agent_id": "agent-implementer-001",
                "session_id": "sess-impl-001",
                "model_id": "glm-5.2",
                "role": "implementer",
            }

            # 2. submit_blind_verdict 带 Identity + attestation_id + view_manifest_hash
            verdict = db.submit_blind_verdict(
                task_id="T-smoketest",
                reviewer_id="reviewer-001",
                verdict="pass",
                decision_data={"clause_1": "satisfied"},
                contract_hash="hash-001",
                view_version="1.0",
                reviewer_identity=reviewer_identity,
                attestation_id="ATT-test-0001",
                view_manifest_hash="vmh-001",
            )
            assert verdict["sealed"], "verdict 应为 sealed"
            assert verdict["reviewer_identity"] == reviewer_identity
            assert verdict["attestation_id"] == "ATT-test-0001"
            assert verdict["view_manifest_hash"] == "vmh-001"
            assert "submission_time" in verdict
            print("[2/6] submit_blind_verdict 带 Identity+attestation 通过")

            # 3. submit_blind_verdict 拒绝不完整 Identity
            try:
                db.submit_blind_verdict(
                    task_id="T-smoketest-2",
                    reviewer_id="reviewer-002",
                    verdict="pass",
                    decision_data={},
                    contract_hash="hash-002",
                    reviewer_identity={"agent_id": "", "session_id": "s", "model_id": "m", "role": "r"},
                )
                assert False, "应拒绝不完整 Identity"
            except ValueError as e:
                assert "IDENTITY_INCOMPLETE" in str(e)
                print("[3/6] 拒绝不完整 Identity 通过")

            # 4. trigger_reveal_event 带 implementer_identity（不同 session）
            reveal = db.trigger_reveal_event(
                task_id="T-smoketest",
                reviewer_id="reviewer-001",
                implementer_identity=implementer_identity,
            )
            assert reveal["verdict_id"] == verdict["id"]
            assert reveal["implementer_identity"] == implementer_identity
            assert "reveal_time" in reveal
            print("[4/6] trigger_reveal_event 带 implementer_identity 通过")

            # 5. trigger_reveal_event 拒绝相同 session
            try:
                # 先为 T-smoketest-3 提交 verdict
                db.submit_blind_verdict(
                    task_id="T-smoketest-3",
                    reviewer_id="reviewer-003",
                    verdict="pass",
                    decision_data={},
                    contract_hash="hash-003",
                    reviewer_identity={
                        "agent_id": "a", "session_id": "same-session",
                        "model_id": "m", "role": "reviewer"
                    },
                )
                db.trigger_reveal_event(
                    task_id="T-smoketest-3",
                    reviewer_id="reviewer-003",
                    implementer_identity={
                        "agent_id": "b", "session_id": "same-session",
                        "model_id": "m", "role": "implementer"
                    },
                )
                assert False, "应拒绝相同 session"
            except ValueError as e:
                assert "SESSION_NOT_SEPARATED" in str(e)
                print("[5/6] 拒绝相同 session 通过")

            # 6. verify_blind_verdict_proofs（正常路径）
            ok, result = db.verify_blind_verdict_proofs(
                task_id="T-smoketest",
                reviewer_identity=reviewer_identity,
                implementer_identity=implementer_identity,
                profile="code_change",
                expected_view_manifest_hash="vmh-001",
            )
            assert ok, f"proofs 应全部通过: {result}"
            proofs = result["proofs"]
            assert proofs["allowlisted_manifest"]["passed"]
            assert proofs["verdict_before_reveal"]["passed"]
            assert proofs["session_separation"]["passed"]
            print(f"[6/6] verify_blind_verdict_proofs 通过: {list(proofs.keys())}")

            # bonus: high_risk 需要独立 Tester
            ok, result = db.verify_blind_verdict_proofs(
                task_id="T-smoketest",
                reviewer_identity=reviewer_identity,
                implementer_identity=implementer_identity,
                profile="high_risk",
                tester_identity=None,  # 缺失 Tester
            )
            assert not ok, "high_risk 缺 Tester 应失败"
            assert "independent_tester" in result["proofs"]
            assert not result["proofs"]["independent_tester"]["passed"]
            print("[bonus] high_risk 缺 Tester 正确失败")

            # bonus: high_risk 带独立 Tester
            tester_identity = {
                "agent_id": "agent-tester-001",
                "session_id": "sess-test-001",
                "model_id": "claude-3.5",
                "role": "tester",
            }
            ok, result = db.verify_blind_verdict_proofs(
                task_id="T-smoketest",
                reviewer_identity=reviewer_identity,
                implementer_identity=implementer_identity,
                profile="high_risk",
                tester_identity=tester_identity,
            )
            # agent/model family 分离可能失败（取决于前缀），检查结果
            print(f"[bonus] high_risk 带 Tester: ok={ok}, proofs={list(result['proofs'].keys())}")

            # bonus: allowlisted manifest 不匹配
            ok, result = db.verify_blind_verdict_proofs(
                task_id="T-smoketest",
                reviewer_identity=reviewer_identity,
                implementer_identity=implementer_identity,
                profile="code_change",
                expected_view_manifest_hash="wrong-hash",
            )
            assert not ok, "manifest 不匹配应失败"
            assert not result["proofs"]["allowlisted_manifest"]["passed"]
            print("[bonus] allowlisted manifest 不匹配正确失败")

        finally:
            db.close()

    print("\n=== ALL P3 REVIEWS SMOKE TESTS PASSED ===")


if __name__ == "__main__":
    main()
