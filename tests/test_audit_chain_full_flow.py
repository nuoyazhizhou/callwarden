"""audit_chain 签名链端到端完整性测试（H5 简化版）。

覆盖 task 全生命周期 create→next→report→apply→close 各阶段产生的审计签名：
- 签名链连续性（prev_signature 链接前后记录）
- record_signature 与 payload_hash 匹配
- 篡改检测（record_signature / prev_signature / payload_hash 任何一处被改都能发现）
- HMAC 模式下全生命周期一致性
- 多表（tasks / change_audit）混合时各自独立链
- task reopen 事件追加到链尾仍保持完整
- 密钥轮换后旧记录保持原签名、新记录用新密钥，verify 全部通过

与 test_audit_chain_mixin.py 的区别：
- mixin 测试：单元粒度，每个方法独立测试（3-5 条记录）
- 本测试：集成粒度，模拟真实 task 生命周期连续触发 5+ 次签名，
  覆盖跨阶段链断裂、跨阶段篡改、跨密钥模式混合等场景
"""

import os
import tempfile
from unittest import mock

from callwarden.db.db import CodeGraphDB


def _db_with_audit():
    """构造带 AuditChainMixin 的临时工作区数据库。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


# task 全生命周期事件序列（模拟 task_create → task_next → task_report → task_apply → task_close）
# 每项：(event_name, operation, payload)
TASK_LIFECYCLE_EVENTS = [
    ("create", "insert", {"task_id": "T-001", "title": "demo", "status": "open"}),
    ("next", "update", {"task_id": "T-001", "step": 0, "status": "in_progress"}),
    ("report", "update", {"task_id": "T-001", "step": 0, "status": "review", "result": "ok"}),
    ("apply", "update", {"task_id": "T-001", "status": "applied", "reviewer": "r1"}),
    ("close", "update", {"task_id": "T-001", "status": "closed", "closed_at": 100}),
]


def _sign_full_lifecycle(db, table_name="tasks"):
    """为 task 全生命周期 5 个事件依次签名。"""
    for event, op, payload in TASK_LIFECYCLE_EVENTS:
        db.sign_audit_record(table_name, f"T-001:{event}", payload, operation=op)


# ============================================
# 全生命周期签名链完整性
# ============================================


def test_full_flow_task_lifecycle_chain_complete():
    """task 全生命周期 5 个事件依次签名，verify 全部通过"""
    db, _root = _db_with_audit()
    try:
        _sign_full_lifecycle(db)
        result = db.verify_audit_chain(table_name="tasks")
        assert result["total_count"] == 5
        assert result["verified_count"] == 5
        assert result["broken_count"] == 0
        assert result["broken_records"] == []
    finally:
        db.close()


def test_full_flow_first_record_prev_empty():
    """全生命周期首条记录 prev_signature 为空串"""
    db, _root = _db_with_audit()
    try:
        first_event = TASK_LIFECYCLE_EVENTS[0]
        r = db.sign_audit_record(
            "tasks", f"T-001:{first_event[0]}", first_event[2], operation=first_event[1]
        )
        assert r["prev_signature"] == ""
    finally:
        db.close()


def test_full_flow_chain_links_each_to_previous():
    """每条记录 prev_signature 等于前一条的 record_signature"""
    db, _root = _db_with_audit()
    try:
        prev_sig = ""
        for event, op, payload in TASK_LIFECYCLE_EVENTS:
            r = db.sign_audit_record("tasks", f"T-001:{event}", payload, operation=op)
            assert r["prev_signature"] == prev_sig
            prev_sig = r["record_signature"]
    finally:
        db.close()


# ============================================
# 篡改检测
# ============================================


def test_full_flow_detect_tampered_record_signature():
    """篡改中间一条 record_signature，verify 应报告 signature_mismatch + 后续 chain_broken"""
    db, _root = _db_with_audit()
    try:
        _sign_full_lifecycle(db)

        # 篡改第 3 条记录的 record_signature
        db.conn.execute(
            "UPDATE audit_chain SET record_signature = ? WHERE id = ?",
            ("tampered_signature", 3),
        )
        db.conn.commit()

        result = db.verify_audit_chain(table_name="tasks")
        assert result["broken_count"] >= 1
        broken_ids = {r["id"] for r in result["broken_records"]}
        # 第 3 条 record_signature 不匹配
        assert 3 in broken_ids
        # 第 4 条 prev_signature 与第 3 条 record_signature 不匹配 → chain_broken
        assert 4 in broken_ids
    finally:
        db.close()


def test_full_flow_detect_tampered_prev_signature():
    """篡改某条 prev_signature，verify 应报告 chain_broken"""
    db, _root = _db_with_audit()
    try:
        _sign_full_lifecycle(db)

        # 篡改第 3 条的 prev_signature（应等于第 2 条的 record_signature）
        db.conn.execute(
            "UPDATE audit_chain SET prev_signature = ? WHERE id = ?",
            ("wrong_prev", 3),
        )
        db.conn.commit()

        result = db.verify_audit_chain(table_name="tasks")
        assert result["broken_count"] >= 1
        broken_ids = {r["id"] for r in result["broken_records"]}
        assert 3 in broken_ids
        reasons = next(
            r["reasons"] for r in result["broken_records"] if r["id"] == 3
        )
        assert "chain_broken" in reasons
    finally:
        db.close()


def test_full_flow_detect_tampered_payload_hash():
    """篡改 payload_hash，verify 应报告 signature_mismatch（record_signature 与 hash 不匹配）"""
    db, _root = _db_with_audit()
    try:
        _sign_full_lifecycle(db)

        # 篡改第 2 条的 payload_hash
        db.conn.execute(
            "UPDATE audit_chain SET payload_hash = ? WHERE id = ?",
            ("0" * 64, 2),
        )
        db.conn.commit()

        result = db.verify_audit_chain(table_name="tasks")
        assert result["broken_count"] >= 1
        broken_ids = {r["id"] for r in result["broken_records"]}
        assert 2 in broken_ids
        reasons = next(
            r["reasons"] for r in result["broken_records"] if r["id"] == 2
        )
        assert "signature_mismatch" in reasons
    finally:
        db.close()


# ============================================
# HMAC 模式全生命周期
# ============================================


def test_full_flow_hmac_mode_lifecycle():
    """HMAC 模式下全生命周期签名 + 验证一致"""
    db, _root = _db_with_audit()
    try:
        with mock.patch.dict(os.environ, {"CALLWARDEN_AUDIT_HMAC_KEY": "lifecycle-secret"}):
            for event, op, payload in TASK_LIFECYCLE_EVENTS:
                r = db.sign_audit_record("tasks", f"T-001:{event}", payload, operation=op)
                assert r["security_level"] == "hmac"
                assert r["signing_key_id"] == "hmac"

            result = db.verify_audit_chain(table_name="tasks")
            assert result["verified_count"] == 5
            assert result["broken_count"] == 0
            assert result["security_level"] == "hmac"
    finally:
        db.close()


def test_full_flow_hmac_mode_detect_tamper():
    """HMAC 模式下篡改同样能被检测"""
    db, _root = _db_with_audit()
    try:
        with mock.patch.dict(os.environ, {"CALLWARDEN_AUDIT_HMAC_KEY": "hmac-tamper-test"}):
            _sign_full_lifecycle(db)

            # 篡改第 2 条 record_signature
            db.conn.execute(
                "UPDATE audit_chain SET record_signature = ? WHERE id = ?",
                ("fake_hmac_sig", 2),
            )
            db.conn.commit()

            result = db.verify_audit_chain(table_name="tasks")
            assert result["broken_count"] >= 1
            broken_ids = {r["id"] for r in result["broken_records"]}
            assert 2 in broken_ids
    finally:
        db.close()


# ============================================
# 多表混合独立链
# ============================================


def test_full_flow_multiple_tables_independent():
    """tasks 表和 change_audit 表混合签名时各自维护独立链"""
    db, _root = _db_with_audit()
    try:
        # tasks 表 3 条（前 3 个生命周期事件）
        for event, op, payload in TASK_LIFECYCLE_EVENTS[:3]:
            db.sign_audit_record("tasks", f"T-001:{event}", payload, operation=op)
        # change_audit 表 2 条（交叉插入）
        db.sign_audit_record("change_audit", "C-1", {"change": "add", "file": "a.py"})
        db.sign_audit_record("change_audit", "C-2", {"change": "del", "file": "b.py"})

        # tasks 表验证
        r_tasks = db.verify_audit_chain(table_name="tasks")
        assert r_tasks["total_count"] == 3
        assert r_tasks["verified_count"] == 3

        # change_audit 表验证
        r_changes = db.verify_audit_chain(table_name="change_audit")
        assert r_changes["total_count"] == 2
        assert r_changes["verified_count"] == 2

        # 全表验证
        r_all = db.verify_audit_chain()
        assert r_all["total_count"] == 5
        assert r_all["verified_count"] == 5
    finally:
        db.close()


def test_full_flow_tamper_one_table_does_not_affect_other():
    """篡改 tasks 表的记录不影响 change_audit 表的验证"""
    db, _root = _db_with_audit()
    try:
        db.sign_audit_record("tasks", "T-1", {"k": "v1"})
        db.sign_audit_record("change_audit", "C-1", {"change": "add"})
        db.sign_audit_record("tasks", "T-2", {"k": "v2"})

        # 篡改 tasks 表第 1 条
        db.conn.execute(
            "UPDATE audit_chain SET record_signature = ? WHERE id = ?",
            ("tampered", 1),
        )
        db.conn.commit()

        # tasks 表应有 broken
        r_tasks = db.verify_audit_chain(table_name="tasks")
        assert r_tasks["broken_count"] >= 1

        # change_audit 表应全部通过
        r_changes = db.verify_audit_chain(table_name="change_audit")
        assert r_changes["verified_count"] == 1
        assert r_changes["broken_count"] == 0
    finally:
        db.close()


# ============================================
# task reopen 追加到链尾
# ============================================


def test_full_flow_task_reopen_extends_chain():
    """task close 后 reopen 追加签名，链仍保持完整"""
    db, _root = _db_with_audit()
    try:
        _sign_full_lifecycle(db)

        # reopen 事件追加到链尾
        db.sign_audit_record(
            "tasks", "T-001:reopen",
            {"task_id": "T-001", "operation": "reopen", "new_status": "in_progress"},
            operation="update",
        )

        result = db.verify_audit_chain(table_name="tasks")
        assert result["total_count"] == 6
        assert result["verified_count"] == 6
        assert result["broken_count"] == 0
    finally:
        db.close()


def test_full_flow_reopen_after_reopen():
    """连续 close → reopen → close → reopen，链仍完整"""
    db, _root = _db_with_audit()
    try:
        _sign_full_lifecycle(db)
        # reopen 1
        db.sign_audit_record("tasks", "T-001:reopen-1", {"operation": "reopen"}, operation="update")
        # 再次 close
        db.sign_audit_record("tasks", "T-001:close-2", {"status": "closed"}, operation="update")
        # reopen 2
        db.sign_audit_record("tasks", "T-001:reopen-2", {"operation": "reopen"}, operation="update")

        result = db.verify_audit_chain(table_name="tasks")
        assert result["total_count"] == 8
        assert result["verified_count"] == 8
    finally:
        db.close()


# ============================================
# 密钥轮换后链仍可验证
# ============================================


def test_full_flow_key_rotation_chain_still_valid():
    """签名过程中轮换密钥，旧记录保持原签名，新记录用新密钥，verify 全部通过"""
    db, _root = _db_with_audit()
    try:
        # 1. 用 local（无密钥）签前 2 条
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CALLWARDEN_AUDIT_HMAC_KEY", None)
            for event, op, payload in TASK_LIFECYCLE_EVENTS[:2]:
                db.sign_audit_record("tasks", f"T-001:{event}", payload, operation=op)

        # 2. 轮换到新密钥 key-2026-07
        db.rotate_signing_key("key-2026-07", "rotation-secret")

        # 3. 用新密钥签后 3 条
        for event, op, payload in TASK_LIFECYCLE_EVENTS[2:]:
            r = db.sign_audit_record("tasks", f"T-001:{event}", payload, operation=op)
            assert r["signing_key_id"] == "key-2026-07"

        # 4. verify 全部通过（旧记录用 SHA-256，新记录用新 HMAC 密钥）
        result = db.verify_audit_chain(table_name="tasks")
        assert result["total_count"] == 5
        assert result["verified_count"] == 5
        assert result["broken_count"] == 0
    finally:
        db.close()


def test_full_flow_key_rotation_then_tamper_new_record():
    """轮换后篡改用新密钥签的记录，verify 应检测到"""
    db, _root = _db_with_audit()
    try:
        # local 签 1 条
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CALLWARDEN_AUDIT_HMAC_KEY", None)
            db.sign_audit_record("tasks", "T-001:create", TASK_LIFECYCLE_EVENTS[0][2], operation="insert")

        # 轮换
        db.rotate_signing_key("key-2026-07", "rotation-secret")

        # 新密钥签 2 条
        db.sign_audit_record("tasks", "T-001:next", TASK_LIFECYCLE_EVENTS[1][2], operation="update")
        db.sign_audit_record("tasks", "T-001:report", TASK_LIFECYCLE_EVENTS[2][2], operation="update")

        # 篡改第 3 条（用新密钥签的）
        db.conn.execute(
            "UPDATE audit_chain SET record_signature = ? WHERE id = ?",
            ("tampered_after_rotation", 3),
        )
        db.conn.commit()

        result = db.verify_audit_chain(table_name="tasks")
        assert result["broken_count"] >= 1
        broken_ids = {r["id"] for r in result["broken_records"]}
        assert 3 in broken_ids
    finally:
        db.close()
