"""P3 身份与独立审核集成测试（任务 8.10，Requirement 1.4-1.5/10.1-10.18/13.4）。

覆盖：
- Identity 校验：缺字段、角色不匹配、同 session、agent/model 家族分离
- Attestation 校验：客户端自签被拒、issuer 非 daemon 被拒、越窗 invalid、
  绑定不匹配 invalid（Req 10.8-10.9）
- apply session 分离端到端：同 session 被拒且保持 pre-request 状态、
  不同 session 成功（Req 1.5, 10.2, 10.6）
- active_task_id 无授权效果（Req 10.7, 13.4）
- issuer/签名密钥撤销：单条记录、无逐条失效事件、compromised 全量 invalid、
  rotated 仅晚于撤销时间、缺 Revocation_Mode 被拒且不追加、payload 逐字节不变
  （Req 10.10-10.18）

断言规则（AGENTS.md 规则 35）：只断言结构化错误码与数据库不变量，
不依赖单一自然语言错误文本。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PKG_PARENT = str(Path(__file__).resolve().parents[1].parent)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db import CodeGraphDB


# 稳定错误码（与 db/db_task_identity.py 常量对齐）
E_IDENTITY_INCOMPLETE = "E_IDENTITY_INCOMPLETE"
E_IDENTITY_ROLE_MISMATCH = "E_IDENTITY_ROLE_MISMATCH"
E_IDENTITY_SESSION_NOT_SEPARATED = "E_IDENTITY_SESSION_NOT_SEPARATED"
E_IDENTITY_AGENT_FAMILY = "E_IDENTITY_AGENT_FAMILY_NOT_SEPARATED"
E_IDENTITY_MODEL_FAMILY = "E_IDENTITY_MODEL_FAMILY_NOT_SEPARATED"
E_IDENTITY_ACTION_DUPLICATE = "E_IDENTITY_ACTION_DUPLICATE"
E_ATTESTATION_SELF_SIGNED = "E_ATTESTATION_SELF_SIGNED"
E_ATTESTATION_ISSUER_NOT_DAEMON = "E_ATTESTATION_ISSUER_NOT_DAEMON"
E_ATTESTATION_BINDING_FAILED = "E_ATTESTATION_BINDING_FAILED"
E_REVOCATION_MODE_REQUIRED = "E_REVOCATION_MODE_REQUIRED"


def _fresh_db(tmp_path):
    db = CodeGraphDB(str(tmp_path / "p3_identity.db"),
                     workspace_root=str(tmp_path))
    ws_id = db.register_workspace("p3-test", str(tmp_path), "P3 身份测试")
    db.set_active_workspace(ws_id)
    return db, ws_id


def _impl_identity(session="S-impl"):
    return {"agent_id": "agent-impl", "session_id": session,
            "model_id": "model-impl", "role": "implementer"}


def _reviewer_identity(session="S-review"):
    return {"agent_id": "agent-review", "session_id": session,
            "model_id": "model-review", "role": "reviewer"}


def _task_at_review(db, task_id):
    return db.conn.execute(
        "SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()["status"]


# ============================================
# 1. Identity 校验
# ============================================


class TestIdentityValidation:
    """Req 10.1/10.2/10.4/10.5 的校验语义。"""

    def test_incomplete_identity_fails_closed(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            ok, reason = db.validate_action_identity(
                {"agent_id": "A"})
            assert ok is False
            assert reason["code"] == E_IDENTITY_INCOMPLETE
        finally:
            db.close()

    def test_role_mismatch(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            ok, reason = db.validate_action_identity(
                _reviewer_identity(), require_role="implementer")
            assert ok is False
            assert reason["code"] == E_IDENTITY_ROLE_MISMATCH
        finally:
            db.close()

    def test_session_separation(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            ok, reason = db.validate_session_separation(
                _reviewer_identity("SAME"), _impl_identity("SAME"))
            assert ok is False
            assert reason["code"] == E_IDENTITY_SESSION_NOT_SEPARATED
            ok2, _ = db.validate_session_separation(
                _reviewer_identity("S-r"), _impl_identity("S-i"))
            assert ok2 is True
        finally:
            db.close()

    def test_agent_family_separation(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            ok, reason = db.validate_agent_family_separation(
                {"agent_id": "alpha-review"}, {"agent_id": "alpha-impl"})
            assert ok is False
            assert reason["code"] == E_IDENTITY_AGENT_FAMILY
            ok2, _ = db.validate_agent_family_separation(
                {"agent_id": "alpha-review"}, {"agent_id": "beta-impl"})
            assert ok2 is True
        finally:
            db.close()

    def test_model_family_separation(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            ok, reason = db.validate_model_family_separation(
                {"model_id": "alpha-m-review"}, {"model_id": "alpha-m-impl"})
            assert ok is False
            assert reason["code"] == E_IDENTITY_MODEL_FAMILY
            ok2, _ = db.validate_model_family_separation(
                {"model_id": "alpha-m-review"}, {"model_id": "beta-m-impl"})
            assert ok2 is True
        finally:
            db.close()

    def test_duplicate_action_identity_rejected(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            ok, result = db.record_action_identity(
                action_id="ACT-DUP", action_type="verdict",
                task_id="T-1", identity=_reviewer_identity())
            assert ok is True
            ok2, reason2 = db.record_action_identity(
                action_id="ACT-DUP", action_type="verdict",
                task_id="T-1", identity=_reviewer_identity())
            assert ok2 is False
            assert reason2["code"] == E_IDENTITY_ACTION_DUPLICATE
        finally:
            db.close()

    def test_active_task_id_not_authorization(self, tmp_path):
        """active_task_id 只作 UX 光标，不影响身份判定（Req 10.7, 13.4）。"""
        db, ws_id = _fresh_db(tmp_path)
        try:
            # 设置 active_task_id
            db.conn.execute(
                "UPDATE workspaces SET active_task_id='T-fake' "
                "WHERE id=?", (ws_id,))
            db.conn.commit()
            # 身份判定不因 active_task_id 改变
            ok, reason = db.validate_action_identity({"agent_id": "A"})
            assert ok is False
            assert reason["code"] == E_IDENTITY_INCOMPLETE
            ok2, _ = db.validate_session_separation(
                _reviewer_identity("S-r"), _impl_identity("S-i"))
            assert ok2 is True
        finally:
            db.close()


# ============================================
# 2. Attestation 校验
# ============================================


class TestAttestationValidation:
    """Req 10.8-10.9：自签/非 daemon/越窗/绑定失败一律 invalid。"""

    def _base_attestation(self, **overrides):
        att = {
            "issuer": "daemon",
            "signing_key_id": "K1",
            "peer_identity": "uid-1000",
            "contract_hash": "CH-1",
            "view_manifest_hash": "VM-1",
            "issued_at": 100.0,
            "valid_from": 90.0,
            "valid_until": 200.0,
        }
        att.update(overrides)
        return att

    def test_self_signed_rejected(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            ok, status, reason = db.validate_attestation(
                self._base_attestation(peer_identity="daemon"))
            assert ok is False
            assert status == "invalid"
            assert reason["code"] == E_ATTESTATION_SELF_SIGNED
        finally:
            db.close()

    def test_non_daemon_issuer_rejected(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            ok, status, reason = db.validate_attestation(
                self._base_attestation(issuer="client-self"))
            assert ok is False
            assert status == "invalid"
            assert reason["code"] == E_ATTESTATION_ISSUER_NOT_DAEMON
        finally:
            db.close()

    def test_out_of_window_invalid(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            # 越窗：check_time 早于 valid_from
            ok, status, _reason = db.validate_attestation(
                self._base_attestation(), check_time=50.0)
            assert ok is False
            assert status == "invalid"
            # 窗口内：valid
            ok2, status2, _ = db.validate_attestation(
                self._base_attestation(), check_time=150.0)
            assert ok2 is True
            assert status2 == "valid"
        finally:
            db.close()

    def test_binding_mismatch_invalid(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            ok, status, reason = db.validate_attestation(
                self._base_attestation(), expected_contract_hash="WRONG")
            assert ok is False
            assert status == "invalid"
            assert reason["code"] == E_ATTESTATION_BINDING_FAILED
        finally:
            db.close()


# ============================================
# 3. apply session 分离端到端
# ============================================


class TestApplySessionSeparation:
    """Req 1.5/10.2/10.6：apply session 必须不同于 implementer session。"""

    def _setup_task_in_review(self, db):
        tid = db.task_create("E2E", "t",
                             [{"action": "implement",
                               "target_file": "a.py"}],
                             creator="agent")
        step = db.task_next_step(tid)
        db.task_report_step(tid, step["step_id"], "done", True, None,
                            identity=_impl_identity())
        assert _task_at_review(db, tid) == "review"
        return tid

    def test_same_session_rejected_state_preserved(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            tid = self._setup_task_in_review(db)
            r = db.task_apply(tid, reviewer="r",
                              identity=_reviewer_identity("S-impl"))
            assert r["error"] == "ERR_IDENTITY_SESSION_NOT_SEPARATED"
            assert r["reason"]["code"] == E_IDENTITY_SESSION_NOT_SEPARATED
            # pre-request 状态保持
            assert _task_at_review(db, tid) == "review"
        finally:
            db.close()

    def test_diff_session_applied(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            tid = self._setup_task_in_review(db)
            r = db.task_apply(tid, reviewer="r",
                              identity=_reviewer_identity("S-review"))
            assert r["status"] == "applied"
            assert _task_at_review(db, tid) == "applied"
        finally:
            db.close()


# ============================================
# 4. 撤销：单条记录 / 无逐条失效事件 / 模式语义 / payload 不变
# ============================================


class TestAttestationRevocation:
    """Req 10.10-10.18 的撤销语义。"""

    def _register(self, db, issuer="daemon", key="K1", mode="rotated"):
        return db.register_attestation_revocation(
            issuer=issuer, signing_key_id=key, revocation_mode=mode,
            revocation_reason="rotation", initiating_actor="reviewer")

    def test_single_record_no_per_record_events(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            ev_before = db.conn.execute(
                "SELECT COUNT(*) FROM task_evidence_events").fetchone()[0]
            ver_before = db.conn.execute(
                "SELECT COUNT(*) FROM task_verdict_events").fetchone()[0]
            ok, result = self._register(db)
            assert ok is True
            count = db.conn.execute(
                "SELECT COUNT(*) FROM attestation_revocation_records "
                "WHERE issuer='daemon' AND signing_key_id='K1'").fetchone()[0]
            assert count == 1
            # 未产生任何逐条失效事件
            assert db.conn.execute(
                "SELECT COUNT(*) FROM task_evidence_events").fetchone()[0] == ev_before
            assert db.conn.execute(
                "SELECT COUNT(*) FROM task_verdict_events").fetchone()[0] == ver_before
        finally:
            db.close()

    def test_compromised_invalidates_all_issuance_times(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            self._register(db, mode="compromised")
            # 签发时间无论早晚一律 invalid
            assert db.derive_attestation_validity("daemon", "K1", 1.0) == "invalid"
            assert db.derive_attestation_validity("daemon", "K1", 1e18) == "invalid"
        finally:
            db.close()

    def test_rotated_only_after_revocation_time(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            ok, result = self._register(db, mode="rotated")
            revoked_at = result["revoked_at"]
            # 早于或等于撤销时间 → 保持 valid
            assert db.derive_attestation_validity("daemon", "K1",
                                                  revoked_at) == "valid"
            assert db.derive_attestation_validity("daemon", "K1",
                                                  revoked_at - 1) == "valid"
            # 晚于撤销时间 → invalid
            assert db.derive_attestation_validity("daemon", "K1",
                                                  revoked_at + 1) == "invalid"
        finally:
            db.close()

    def test_missing_mode_rejected_no_record(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            before = db.conn.execute(
                "SELECT COUNT(*) FROM attestation_revocation_records").fetchone()[0]
            ok, reason = db.register_attestation_revocation(
                issuer="daemon", signing_key_id="K9", revocation_mode="",
                revocation_reason="", initiating_actor="")
            assert ok is False
            assert reason["code"] == E_REVOCATION_MODE_REQUIRED
            after = db.conn.execute(
                "SELECT COUNT(*) FROM attestation_revocation_records").fetchone()[0]
            assert after == before  # 不追加任何记录
        finally:
            db.close()

    def test_existing_payload_byte_for_byte_unchanged(self, tmp_path):
        db, ws_id = _fresh_db(tmp_path)
        try:
            payload = '{"clause":"must_pass","decision":"pass"}'
            db.conn.execute(
                "INSERT INTO task_verdict_events "
                "(verdict_id, task_id, contract_id, contract_revision, "
                " contract_hash, phase, clause_results, submitted_at, "
                " workspace_id) VALUES ('V-PAYLOAD', 'T-1', 'C-1', 1, 'h', "
                " 'first_pass', ?, 100.0, ?)",
                (payload, ws_id))
            db.conn.commit()
            before = db.conn.execute(
                "SELECT clause_results FROM task_verdict_events "
                "WHERE verdict_id='V-PAYLOAD'").fetchone()[0]
            # 两种模式撤销后 payload 逐字节不变
            self._register(db, mode="compromised")
            self._register(db, key="K1", mode="rotated")
            after = db.conn.execute(
                "SELECT clause_results FROM task_verdict_events "
                "WHERE verdict_id='V-PAYLOAD'").fetchone()[0]
            assert after == before
            assert after == payload
        finally:
            db.close()
