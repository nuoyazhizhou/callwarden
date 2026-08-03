"""Stage_Toggle 感知的 Evidence Gate 门控测试（任务 F，Requirement 13.1/13.12/13.15）。

覆盖：
- legacy 任务（P1 Stage_Toggle 默认 disabled，无 daemon 配置存储/表）不再被 Evidence
  Gate block：evaluate_evidence_gate_for_task 记录 P1_NOT_ENABLED 且判定 pass
  （可审计、不伪造产物）；task_report_step 正常完成并推进 review；task_apply 正常通过。
- 注入 P1-enabled（global scope）后缺产物仍 block：契约/快照/verdict/evidence 缺失
  时保持 A 的严格 fail-closed（GATE_CONTRACT_ENVELOPE_MISSING / GATE_VERDICT_ABSENT /
  GATE_EVIDENCE_ABSENT），task_report_step 步骤标记 blocked，task_apply 返回
  ERR_EVIDENCE_GATE_BLOCKED 且任务状态保持。
- P3 门控（内核 enabled_stages）：P1 启用而 P3 未启用时跳过 Identity fail-closed
  （记录 P3_NOT_ENABLED，pass）；P1+P3 均启用时保持 ERR_IDENTITY_MISSING fail-closed。
- task_gate_decisions 记录 resolved_stage_toggle_set（Req 13.15）。

断言规则（AGENTS.md 规则 35）：只断言结构化错误码与数据库不变量，
不依赖单一自然语言错误文本。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_PKG_PARENT = str(Path(__file__).resolve().parents[1].parent)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db import CodeGraphDB
from callwarden.db.db_task_gate import (
    P1_NOT_ENABLED,
    P3_NOT_ENABLED,
    GATE_CONTRACT_ENVELOPE_MISSING,
    GATE_VERDICT_ABSENT,
    GATE_EVIDENCE_ABSENT,
)
from callwarden.server.stage_toggle_migration import ensure_stage_toggle_schema


def _fresh_db(tmp_path):
    db = CodeGraphDB(
        str(tmp_path / "stage_toggle_gate.db"),
        workspace_root=str(tmp_path),
    )
    ws_id = db.register_workspace("stage-toggle-test", str(tmp_path), "Stage_Toggle 门控测试")
    db.set_active_workspace(ws_id)
    return db, ws_id


def _impl_identity(session="S-impl"):
    return {"agent_id": "agent-impl", "session_id": session,
            "model_id": "model-impl", "role": "implementer"}


def _reviewer_identity(session="S-review"):
    return {"agent_id": "agent-review", "session_id": session,
            "model_id": "model-review", "role": "reviewer"}


def _task_status(db, task_id):
    return db.conn.execute(
        "SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()["status"]


def _step_status(db, step_id):
    return db.conn.execute(
        "SELECT status FROM task_steps WHERE id=?", (step_id,)).fetchone()["status"]


def _create_task_at_review(db):
    """P1 默认 disabled 下完成单步任务并推进到 review。"""
    tid = db.task_create("E2E", "t",
                         [{"action": "implement", "target_file": "a.py"}],
                         creator="agent")
    step = db.task_next_step(tid)
    db.task_report_step(tid, step["step_id"], "done", True, None,
                        identity=_impl_identity())
    assert _task_status(db, tid) == "review"
    return tid


def _set_p1(store_path, scope_key="global", enabled=True):
    """写入 P1 Stage_Toggle（复用 daemon 配置存储 schema）。"""
    conn = sqlite3.connect(str(store_path))
    try:
        ensure_stage_toggle_schema(conn)
        conn.execute(
            "INSERT OR REPLACE INTO stage_toggles (stage, scope_key, enabled, actor, changed_at) "
            "VALUES ('P1', ?, ?, 'test', ?)",
            (scope_key, int(enabled), int(time.time() * 1000)),
        )
        conn.commit()
    finally:
        conn.close()


# ============================================
# 1. P1 默认 disabled：legacy 任务不再被 block
# ============================================


class TestP1DisabledLegacy:
    """Req 13.1/13.12/13.15：无 daemon 存储/无表 → P1 disabled → 记录 NOT_ENABLED 且 pass。"""

    def test_gate_pass_with_p1_not_enabled(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            tid = _create_task_at_review(db)
            r = db.evaluate_evidence_gate_for_task(task_id=tid)
            assert r["decision"] == "pass"
            codes = [x["code"] for x in r["reasons"]]
            assert P1_NOT_ENABLED in codes
            assert r["stage_toggle"] == {"P1": False, "P3": False}
            # 可审计：task_gate_decisions 落盘 pass + resolved toggle 集合
            row = db.conn.execute(
                "SELECT decision, resolved_stage_toggle_set FROM task_gate_decisions "
                "WHERE task_id=?", (tid,)).fetchone()
            assert row is not None
            assert row["decision"] == "pass"
            assert "P1" in row["resolved_stage_toggle_set"]
        finally:
            db.close()

    def test_task_report_step_not_blocked(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            tid = db.task_create("L", "legacy",
                                 [{"action": "implement", "target_file": "a.py"}],
                                 creator="agent")
            step = db.task_next_step(tid)
            db.task_report_step(tid, step["step_id"], "done", True, None,
                                identity=_impl_identity())
            assert _step_status(db, step["step_id"]) == "done"
            assert _task_status(db, tid) == "review"
        finally:
            db.close()

    def test_task_apply_not_blocked(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            tid = _create_task_at_review(db)
            r = db.task_apply(tid, reviewer="r", identity=_reviewer_identity())
            assert r.get("error") is None
            assert r.get("status") == "applied"
            assert _task_status(db, tid) == "applied"
        finally:
            db.close()


# ============================================
# 2. P1 enabled：缺产物保持严格 fail-closed
# ============================================


class TestP1EnabledFailsClosed:
    """Req 13.15：P1 启用时只评估 P1 条款，缺产物仍 block（保持 A 的 fail-closed）。"""

    def test_gate_block_missing_artifacts(self, tmp_path, monkeypatch):
        store_path = tmp_path / "daemon_config.db"
        monkeypatch.setenv("CW_DAEMON_CONFIG_DB", str(store_path))
        _set_p1(store_path)
        db, _ws = _fresh_db(tmp_path)
        try:
            tid = db.task_create("E", "enabled",
                                 [{"action": "implement", "target_file": "a.py"}],
                                 creator="agent")
            r = db.evaluate_evidence_gate_for_task(task_id=tid)
            assert r["decision"] == "block"
            codes = [x["code"] for x in r["reasons"]]
            assert GATE_CONTRACT_ENVELOPE_MISSING in codes
            assert GATE_VERDICT_ABSENT in codes
            assert GATE_EVIDENCE_ABSENT in codes
            assert r["stage_toggle"] == {"P1": True, "P3": False}
            # 记录 resolved toggle 集合（Req 13.15）
            row = db.conn.execute(
                "SELECT resolved_stage_toggle_set FROM task_gate_decisions "
                "WHERE task_id=?", (tid,)).fetchone()
            assert row is not None
            assert json.loads(row["resolved_stage_toggle_set"])["P1"] is True
        finally:
            db.close()

    def test_task_report_step_blocked(self, tmp_path, monkeypatch):
        store_path = tmp_path / "daemon_config.db"
        monkeypatch.setenv("CW_DAEMON_CONFIG_DB", str(store_path))
        _set_p1(store_path)
        db, _ws = _fresh_db(tmp_path)
        try:
            tid = db.task_create("E", "enabled",
                                 [{"action": "implement", "target_file": "a.py"}],
                                 creator="agent")
            step = db.task_next_step(tid)
            res = db.task_report_step(tid, step["step_id"], "done", True, None,
                                      identity=_impl_identity())
            # 缺产物 → step 标记 blocked，任务不推进 review
            assert _step_status(db, step["step_id"]) == "blocked"
            assert _task_status(db, tid) != "review"
        finally:
            db.close()

    def test_task_apply_blocked_after_p1_enabled(self, tmp_path, monkeypatch):
        store_path = tmp_path / "daemon_config.db"
        monkeypatch.setenv("CW_DAEMON_CONFIG_DB", str(store_path))
        db, _ws = _fresh_db(tmp_path)
        try:
            # 先以 legacy（P1 disabled）完成步骤到 review
            tid = _create_task_at_review(db)
            # 之后注入 P1 enabled → apply 走 fail-closed
            _set_p1(store_path)
            r = db.task_apply(tid, reviewer="r", identity=_reviewer_identity())
            assert r.get("error") == "ERR_EVIDENCE_GATE_BLOCKED"
            assert _task_status(db, tid) == "review"
        finally:
            db.close()


# ============================================
# 3. P3 门控：enabled_stages 控制 Identity fail-closed
# ============================================


class TestP3GateToggle:
    """Req 13.15：P3 未启用时跳过 P3 条款（Identity fail-closed），启用时保持 fail-closed。"""

    def test_p3_disabled_skips_identity_fail_closed(self, tmp_path):
        db, _ws = _fresh_db(tmp_path)
        try:
            tid = db.task_create("T", "p3",
                                 [{"action": "implement", "target_file": "a.py"}],
                                 creator="agent")
            base = {
                "task_id": tid,
                "profile": "fast_track",
                "current_contract": {"contract_hash": "H1"},
                "snapshot_s0": {"snapshot_id": "SNAP-1"},
                "evidences": [{"id": "E1", "verifier_name": "v",
                               "verifier_version": "1", "config_hash": "h",
                               "freshness_status": "fresh"}],
                "quality_findings": [],
                # 无 reviewer_identity 的 verdict（P3 Identity fail-closed 触发场景）
                "verdicts": [{"id": "V1", "role": "reviewer", "verdict": "approved"}],
            }
            # P3 未启用：跳过 Identity fail-closed → pass（记录由组装层负责）
            r1 = db.evaluate_evidence_gate(**base, enabled_stages={"P1"})
            assert r1["decision"] == "pass"
            assert not any(x["code"] == "ERR_IDENTITY_MISSING" for x in r1["reasons"])
            # P1+P3 均启用：Identity fail-closed 生效 → block
            r2 = db.evaluate_evidence_gate(**base, enabled_stages={"P1", "P3"})
            assert r2["decision"] == "block"
            assert any(x["code"] == "ERR_IDENTITY_MISSING" for x in r2["reasons"])
        finally:
            db.close()

    def test_assembly_records_p3_not_enabled(self, tmp_path, monkeypatch):
        """组装层：P1 enabled + P3 disabled 时记录 P3_NOT_ENABLED（skipped，不阻断）。"""
        store_path = tmp_path / "daemon_config.db"
        monkeypatch.setenv("CW_DAEMON_CONFIG_DB", str(store_path))
        _set_p1(store_path)  # 仅启用 P1，P3 保持 disabled
        db, _ws = _fresh_db(tmp_path)
        try:
            tid = db.task_create("E", "p1-only",
                                 [{"action": "implement", "target_file": "a.py"}],
                                 creator="agent")
            r = db.evaluate_evidence_gate_for_task(task_id=tid)
            # P1 enabled 但缺产物 → 仍 block（fail-closed 保持），且带 P3_NOT_ENABLED 审计记录
            assert r["decision"] == "block"
            codes = [x["code"] for x in r["reasons"]]
            assert P3_NOT_ENABLED in codes
            assert r["stage_toggle"] == {"P1": True, "P3": False}
        finally:
            db.close()
