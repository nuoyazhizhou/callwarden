"""P3 Identity/Attestation smoke test（临时验证脚本）

验证 TaskIdentityMixin 已正确接入 CodeGraphDB，关键方法可调用。
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


def main():
    # 1. 导入检查
    from callwarden.db.db_task_identity import TaskIdentityMixin
    from callwarden.db import CodeGraphDB
    print("[1/8] 导入检查通过: TaskIdentityMixin, CodeGraphDB")

    # 确认 CodeGraphDB 继承了 TaskIdentityMixin
    assert issubclass(CodeGraphDB, TaskIdentityMixin), "CodeGraphDB 未继承 TaskIdentityMixin"
    print("[2/8] Mixin 接入检查通过: CodeGraphDB 继承 TaskIdentityMixin")

    # 2. 实例化（临时数据库）—— 强制走 Python schema 路径，避免 Rust StorageService 未含 P3 表
    os.environ["CW_USE_RUST_STORAGE"] = "0"
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        os.environ["CALLWARDEN_DB_PATH"] = db_path
        db = CodeGraphDB(db_path)
        try:
            # 注册 workspace 并设为 active
            db.register_workspace("test-ws", tmp)
            db.set_active_workspace("test-ws")

            # 确认 P3 表已创建
            cur = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('action_identities','attestation_records','attestation_revocation_records')"
            )
            tables = [r[0] for r in cur.fetchall()]
            assert len(tables) == 3, f"P3 表缺失: {tables}"
            print(f"[2.5/8] P3 表已创建: {tables}")

            # 3. record_action_identity
            action_id = "ACT-smoketest-0001"
            identity = {
                "agent_id": "agent-implementer-001",
                "session_id": "sess-impl-001",
                "model_id": "glm-5.2",
                "role": "implementer",
            }
            ok, result = db.record_action_identity(
                action_id=action_id,
                action_type="contract",
                task_id="T-smoketest",
                identity=identity,
            )
            assert ok, f"record_action_identity 失败: {result}"
            print(f"[3/8] record_action_identity 通过: {result}")

            # 4. validate_action_identity
            ok, result = db.validate_action_identity(identity, require_role="implementer")
            assert ok, f"validate_action_identity 失败: {result}"
            print(f"[4/8] validate_action_identity 通过")

            # 5. validate_session_separation
            reviewer_identity = {
                "agent_id": "agent-reviewer-001",
                "session_id": "sess-rev-001",  # 不同 session
                "model_id": "glm-5.2",
                "role": "reviewer",
            }
            ok, result = db.validate_session_separation(reviewer_identity, identity)
            assert ok, f"validate_session_separation 失败: {result}"
            print(f"[5/8] validate_session_separation 通过")

            # 6. issue_attestation
            ok, result = db.issue_attestation(
                action_id=action_id,
                signing_key_id="key-001",
                peer_identity="peer-001",
                contract_hash="hash-001",
                signature="sig-001",
                valid_from=time.time() - 60,
                valid_until=time.time() + 3600,
            )
            assert ok, f"issue_attestation 失败: {result}"
            attestation_id = result.get("attestation_id")
            print(f"[6/8] issue_attestation 通过: attestation_id={attestation_id}")

            # 7. validate_attestation（应通过）
            attestation = {
                "issuer": "daemon",
                "signing_key_id": "key-001",
                "issued_at": time.time(),
                "valid_from": time.time() - 60,
                "valid_until": time.time() + 3600,
                "contract_hash": "hash-001",
                "view_manifest_hash": "",
                "peer_identity": "peer-001",
                "workspace_id": db._get_active_workspace_id(),
            }
            ok, status, reason = db.validate_attestation(
                attestation,
                expected_contract_hash="hash-001",
            )
            assert ok, f"validate_attestation 失败: {reason}, status={status}"
            print(f"[7/8] validate_attestation 通过: status={status}")

            # 8. register_attestation_revocation + derive_attestation_validity
            # compromised 模式
            ok, result = db.register_attestation_revocation(
                issuer="daemon",
                signing_key_id="key-001",
                revocation_mode="compromised",
                revocation_reason="smoke test",
                initiating_actor="admin",
            )
            assert ok, f"register_attestation_revocation 失败: {result}"
            validity = db.derive_attestation_validity(
                issuer="daemon",
                signing_key_id="key-001",
                issuance_time=time.time(),
            )
            assert validity == "invalid", f"compromised 模式应判 invalid, 实际 {validity}"
            print(f"[8/8] register_attestation_revocation + derive_attestation_validity 通过: compromised→{validity}")

            # rotated 模式（issuance_time < revoked_at 应保持 valid）
            ok, result = db.register_attestation_revocation(
                issuer="daemon",
                signing_key_id="key-002",
                revocation_mode="rotated",
                revocation_reason="rotation test",
                initiating_actor="admin",
            )
            assert ok, f"register_attestation_revocation (rotated) 失败: {result}"
            validity_before = db.derive_attestation_validity(
                issuer="daemon",
                signing_key_id="key-002",
                issuance_time=time.time() - 100,  # 签发时间早于撤销
            )
            assert validity_before == "valid", f"rotated 模式 issuance<revoked 应 valid, 实际 {validity_before}"
            validity_after = db.derive_attestation_validity(
                issuer="daemon",
                signing_key_id="key-002",
                issuance_time=time.time() + 100,  # 签发时间晚于撤销
            )
            assert validity_after == "invalid", f"rotated 模式 issuance>revoked 应 invalid, 实际 {validity_after}"
            print(f"[bonus] rotated 模式派生正确: before_revoked→{validity_before}, after_revoked→{validity_after}")

            # Revocation_Mode 缺值应拒绝
            ok, result = db.register_attestation_revocation(
                issuer="daemon",
                signing_key_id="key-003",
                revocation_mode="",
                revocation_reason="should fail",
                initiating_actor="admin",
            )
            assert not ok, f"空 revocation_mode 应拒绝, 但返回 {result}"
            print(f"[bonus] 空 revocation_mode 正确拒绝: {result.get('code')}")
        finally:
            db.close()

    print("\n=== ALL SMOKE TESTS PASSED ===")


if __name__ == "__main__":
    main()
