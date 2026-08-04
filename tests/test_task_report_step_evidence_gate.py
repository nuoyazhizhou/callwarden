"""S4：task_report_step 接入 Evidence Gate（P1 契约硬门禁）专项测试

验收点（对应计划 4.8：Completion Gate 接入 task_report_step）：
- 已发布契约 Envelope 但缺少 reviewer verdict → Evidence Gate block
- block 时 step 标记为 blocked + 自动插入 fix_gate_failure 修复步骤
- 返回信息携带 evidence_gate.decision=block（即使任务树中还有 pending 修复步骤）
- legacy 任务（无契约/verdict/evidence）→ Evidence Gate pass，不阻塞既有流程
"""

import json
import os
import sqlite3
import sys
import tempfile
import time

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db import CodeGraphDB  # noqa: E402
from callwarden.server.stage_toggle_migration import ensure_stage_toggle_schema  # noqa: E402


def _set_p1_enabled(store_path):
    """写入 P1 Stage_Toggle（global scope enabled），使 Evidence Gate 评估 P1 条款。"""
    conn = sqlite3.connect(store_path)
    try:
        ensure_stage_toggle_schema(conn)
        conn.execute(
            "INSERT OR REPLACE INTO stage_toggles (stage, scope_key, enabled, actor, changed_at) "
            "VALUES ('P1', 'global', 1, 'test', ?)",
            (str(time.time()),),
        )
        conn.commit()
    finally:
        conn.close()


def _db_with_workspace():
    """构造临时工作区数据库（触发完整 schema 初始化）。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _create_task_with_step(db, title="evidence-gate-test", step_count=1):
    """创建带 N 个步骤的任务，返回 task_id（target_file 留空避免 scope 干扰）。"""
    steps = [{"action": "edit", "target_file": ""} for _ in range(step_count)]
    task_id = db.task_create(title, steps=steps)
    return task_id


def _inject_contract(db, task_id, contract_id="C-evidence-001"):
    """注入一条契约 Envelope 事件，使契约查询非空（绕过 legacy 跳过分支）。"""
    db.conn.execute(
        """
        INSERT INTO task_contract_revisions
            (contract_id, revision, contract_hash, profile, task_id, workspace_id,
             envelope_payload, created_at, created_by)
        VALUES (?, 1, ?, 'code_change', ?, ?, ?, ?, ?)
        """,
        (
            contract_id,
            f"HASH-{contract_id}",
            task_id,
            db._get_active_workspace_id(),
            json.dumps({"contract_id": contract_id, "revision": 1, "objective": "test"}),
            time.time(),
            "sess-impl-001",
        ),
    )
    db.conn.commit()


def test_task_report_step_evidence_gate_block_missing_verdict(monkeypatch):
    """P1 enabled + 契约已发布但缺 reviewer verdict → Evidence Gate block：step→blocked + fix_gate_failure + 返回 evidence_gate"""
    db, root = _db_with_workspace()
    # 注入 P1-enabled toggle（Stage_Toggle 感知，Req 13.1/13.15）
    store_path = os.path.join(root, "daemon_config.db")
    monkeypatch.setenv("CW_DAEMON_CONFIG_DB", store_path)
    _set_p1_enabled(store_path)
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_contract(db, task_id)

        result = db.task_report_step(task_id, step["step_id"], result="done", success=True)

        # step 应为 blocked（Evidence Gate 阻断，不允许完成）
        cur = db.conn.execute("SELECT status FROM task_steps WHERE id = ?", (step["step_id"],))
        assert cur.fetchone()["status"] == "blocked"

        # 应自动插入 fix_gate_failure 步骤，check_items 记录 gate_decision=block
        cur = db.conn.execute(
            "SELECT * FROM task_steps WHERE task_id = ? AND action = ?",
            (task_id, "fix_gate_failure"),
        )
        fix_row = cur.fetchone()
        assert fix_row is not None, "Evidence Gate block 后应插入 fix_gate_failure 步骤"
        check_items = json.loads(fix_row["check_items"])
        assert check_items.get("gate_decision") == "block"
        assert check_items.get("gate_reason")

        # 任务不应被推到 review（gate_failed 阻止状态转换）
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
        assert cur.fetchone()["status"] != "review"

        # 返回值应含 evidence_gate.decision=block
        assert result is not None
        assert result["evidence_gate"]["decision"] == "block"
    finally:
        db.close()


def test_task_report_step_evidence_gate_pass_legacy():
    """legacy 任务（无契约/verdict/evidence）→ Evidence Gate pass，step 正常完成，不插修复步骤"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)

        result = db.task_report_step(task_id, step["step_id"], result="done", success=True)

        cur = db.conn.execute("SELECT status FROM task_steps WHERE id = ?", (step["step_id"],))
        assert cur.fetchone()["status"] == "done"

        # legacy 任务不应插入 fix_gate_failure
        cur = db.conn.execute(
            "SELECT COUNT(*) as cnt FROM task_steps WHERE task_id = ? AND action = ?",
            (task_id, "fix_gate_failure"),
        )
        assert cur.fetchone()["cnt"] == 0

        # 单步任务完成后无下一步，返回 None（Evidence Gate pass 不阻断）
        assert result is None
    finally:
        db.close()
