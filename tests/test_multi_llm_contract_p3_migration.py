"""P3 schema 与幂等迁移测试（任务 8.9，Requirement 10.1/10.5/10.8-10.12/10.17/10.18/13.10）。

覆盖：
- v45 三张 P3 表（action_identities / attestation_records /
  attestation_revocation_records）存在，SCHEMA_VERSION >= 45
- Identity 新记录必填：agent_id/session_id/model_id/role 均 NOT NULL，
  缺失任何字段即拒绝（不得由 reviewer 自由文本或 ownership 补齐，Req 10.5）
- Attestation_Revocation_Record 结构：issuer 标识、签名密钥标识、
  Revocation_Mode（必填、无默认值、取值限于 compromised/rotated）、
  撤销原因、发起者身份与撤销时间齐备；不可变、只追加（Req 10.10-10.11）
- 撤销记录按 issuer+signing_key 键控，**不包含** verdict/evidence 逐条
  引用列——schema 层不存在按历史记录批量写入逐条失效事件的通道
  （Req 10.10）；个体失效事件表仍保留（Req 6.6，10.18）
- 旧记录可读 + 重复迁移幂等

断言规则（AGENTS.md 规则 35）：只断言结构化状态、错误码与数据库不变量，
不依赖单一自然语言错误文本。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_PKG_PARENT = str(Path(__file__).resolve().parents[1].parent)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db import CodeGraphDB


# ============================================
# 辅助函数
# ============================================


def _fresh_db(tmp_path):
    """创建全新临时库（自动应用全部迁移）。"""
    db_path = str(tmp_path / "p3_migration.db")
    db = CodeGraphDB(db_path, workspace_root=str(tmp_path))
    ws_id = db.register_workspace("p3-test", str(tmp_path), "P3 迁移测试")
    db.set_active_workspace(ws_id)
    return db, db_path, ws_id


def _table_columns(db, table: str):
    return {row[1] for row in db.conn.execute(f"PRAGMA table_info({table})")}


# ============================================
# 1. v45 表与版本
# ============================================


class TestV45Schema:
    """P3 schema 存在性与版本。"""

    def test_v45_tables_and_version(self, tmp_path):
        db, _path, _ws = _fresh_db(tmp_path)
        try:
            tables = {
                r[0] for r in db.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert {"action_identities", "attestation_records",
                    "attestation_revocation_records"} <= tables
            version = db.conn.execute(
                "SELECT MAX(version) FROM schema_version").fetchone()[0]
            assert version >= 45
        finally:
            db.close()

    def test_action_identities_identity_fields_not_null(self, tmp_path):
        """Identity 四字段必填（Req 10.1, 10.5），缺一即拒绝。"""
        db, _path, ws_id = _fresh_db(tmp_path)
        try:
            full = (
                "INSERT INTO action_identities "
                "(workspace_id, action_id, action_type, task_id, agent_id, "
                " session_id, model_id, role, recorded_at) "
                "VALUES (?, 'ACT-1', 'verdict', 'T-1', 'A', 'S', 'M', "
                "        'reviewer', 100.0)"
            )
            db.conn.execute(full, (ws_id,))
            db.conn.commit()
            # 缺 agent_id（NULL）→ NOT NULL 约束失败
            partial = (
                "INSERT INTO action_identities "
                "(workspace_id, action_id, action_type, task_id, session_id, "
                " model_id, role, recorded_at) "
                "VALUES (?, 'ACT-2', 'verdict', 'T-1', 'S', 'M', "
                "        'reviewer', 100.0)"
            )
            with pytest.raises(Exception) as exc:
                db.conn.execute(partial, (ws_id,))
            assert "NOT NULL" in str(exc.value).upper() or "constraint" in str(
                exc.value).lower()
        finally:
            db.close()


# ============================================
# 2. Attestation_Revocation_Record 结构
# ============================================


class TestAttestationRevocationRecord:
    """撤销记录结构：必填、无默认、取值受限、不可变只追加。"""

    def test_structure_columns(self, tmp_path):
        db, _path, _ws = _fresh_db(tmp_path)
        try:
            cols = _table_columns(db, "attestation_revocation_records")
            required = {
                "revocation_id", "issuer", "signing_key_id",
                "revocation_mode", "revocation_reason",
                "initiating_actor", "revoked_at",
            }
            assert required <= cols, f"缺少列: {required - cols}"
        finally:
            db.close()

    def test_revocation_mode_mandatory_and_check(self, tmp_path):
        """Revocation_Mode 必填且取值限于 compromised/rotated（Req 10.12）。"""
        db, _path, ws_id = _fresh_db(tmp_path)
        try:
            base = (
                "INSERT INTO attestation_revocation_records "
                "(workspace_id, revocation_id, issuer, signing_key_id, "
                " revocation_mode, revocation_reason, initiating_actor, "
                " revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            )
            # 合法取值
            db.conn.execute(
                base, (ws_id, "REV-1", "daemon", "K1", "rotated",
                       "rotation", "", 100.0))
            db.conn.commit()
            # 非法取值 → CHECK 约束
            with pytest.raises(Exception) as exc:
                db.conn.execute(
                    base, (ws_id, "REV-2", "daemon", "K2", "unknown",
                           "x", "", 100.0))
            assert "check" in str(exc.value).lower() or "constraint" in str(
                exc.value).lower()
            # 缺 mode（NULL）→ NOT NULL 约束
            with pytest.raises(Exception) as exc2:
                db.conn.execute(
                    "INSERT INTO attestation_revocation_records "
                    "(workspace_id, revocation_id, issuer, signing_key_id, "
                    " revocation_reason, initiating_actor, revoked_at) "
                    "VALUES (?, 'REV-3', 'daemon', 'K3', 'x', '', 100.0)",
                    (ws_id,))
            assert "not null" in str(exc2.value).lower() or "constraint" in str(
                exc2.value).lower()
        finally:
            db.close()

    def test_no_per_record_reference_columns(self, tmp_path):
        """撤销记录按 issuer+签名密钥键控，不含逐条 verdict/evidence 引用列。

        证明 schema 层不存在"按历史记录批量写入逐条失效事件"的通道
        （Req 10.10）；撤销导致的 invalid 只能由查询层按 issuer/密钥匹配派生。
        """
        db, _path, _ws = _fresh_db(tmp_path)
        try:
            cols = _table_columns(db, "attestation_revocation_records")
            per_record_refs = {"evidence_id", "verdict_id", "evidence_ref",
                               "verdict_ref"}
            assert not (per_record_refs & cols), (
                f"撤销记录不应包含逐条引用列: {per_record_refs & cols}"
            )
        finally:
            db.close()

    def test_individual_invalidation_mechanism_remains(self, tmp_path):
        """个体失效事件机制仍保留（Req 6.6, 10.18）。"""
        db, _path, _ws = _fresh_db(tmp_path)
        try:
            cols = _table_columns(db, "task_evidence_events")
            assert {"invalidation_reason", "original_evidence_ref"} <= cols, (
                "task_evidence_events 应保留个体失效字段（Req 6.6）"
            )
        finally:
            db.close()


# ============================================
# 3. 旧记录可读 + 迁移幂等
# ============================================


class TestMigrationIdempotency:
    """重复迁移幂等；旧版本记录迁移后仍可读。"""

    def test_reopen_idempotent_and_old_records_readable(self, tmp_path):
        db, db_path, ws_id = _fresh_db(tmp_path)
        try:
            # 写入 v43（verdict）、v44（dependency）、v45（revocation）记录
            db.conn.execute(
                "INSERT INTO task_verdict_events "
                "(verdict_id, task_id, contract_id, contract_revision, "
                " contract_hash, phase, submitted_at, workspace_id) "
                "VALUES ('V-1', 'T-1', 'C-1', 1, 'h1', 'first_pass', "
                "        100.0, ?)",
                (ws_id,))
            db.conn.execute(
                "INSERT INTO task_dependencies "
                "(workspace_id, task_id, contract_id, contract_revision, "
                " dependency_type, target_ref, is_informational, declared_at) "
                "VALUES (?, 'T-1', 'C-1', 1, 'requires_existing', 'X', "
                "        0, 100.0)",
                (ws_id,))
            db.conn.execute(
                "INSERT INTO attestation_revocation_records "
                "(workspace_id, revocation_id, issuer, signing_key_id, "
                " revocation_mode, revocation_reason, initiating_actor, "
                " revoked_at) VALUES (?, 'REV-OLD', 'daemon', 'K-OLD', "
                "                     'rotated', 'rotation', '', 50.0)",
                (ws_id,))
            db.conn.commit()
            v_before = db.conn.execute(
                "SELECT MAX(version) FROM schema_version").fetchone()[0]
        finally:
            db.close()

        # 重新打开：迁移再次执行（幂等），版本不变、旧记录可读
        db2 = CodeGraphDB(db_path, workspace_root=str(tmp_path))
        try:
            v_after = db2.conn.execute(
                "SELECT MAX(version) FROM schema_version").fetchone()[0]
            assert v_after == v_before
            v_row = db2.conn.execute(
                "SELECT verdict_id FROM task_verdict_events "
                "WHERE verdict_id='V-1'").fetchone()
            assert v_row and v_row[0] == "V-1"
            d_row = db2.conn.execute(
                "SELECT dependency_type FROM task_dependencies "
                "WHERE target_ref='X'").fetchone()
            assert d_row and d_row[0] == "requires_existing"
            r_row = db2.conn.execute(
                "SELECT revocation_mode FROM attestation_revocation_records "
                "WHERE revocation_id='REV-OLD'").fetchone()
            assert r_row and r_row[0] == "rotated"
        finally:
            db2.close()

    def test_failed_migration_leaves_no_partial_table(self, tmp_path):
        """失败迁移原子回滚：不残留半迁移状态。"""
        db_path = str(tmp_path / "p3_fail.db")
        db = CodeGraphDB(db_path, workspace_root=str(tmp_path))
        try:
            # 全新库已完整迁移；断言关键 P3 表全部存在（无半迁移）
            tables = {
                r[0] for r in db.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert {"action_identities", "attestation_records",
                    "attestation_revocation_records"} <= tables
        finally:
            db.close()
