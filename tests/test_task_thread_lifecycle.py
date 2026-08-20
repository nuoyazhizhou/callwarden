"""主任务线程与原地 remediation 的 focused 回归测试。"""

import json

from callwarden.db import CodeGraphDB


def _new_db(tmp_path):
    db = CodeGraphDB(str(tmp_path / "task-thread.db"))
    db.register_workspace("task-thread-tests", str(tmp_path))
    db.set_active_workspace("task-thread-tests")
    return db


def test_failed_report_appends_provenance_bound_fix_to_same_task(tmp_path):
    db = _new_db(tmp_path)
    try:
        task_id = db.task_create(
            "thread lifecycle",
            steps=[
                {
                    "action": "implement",
                    "target_file": "src/thread.py",
                    "check_items": "focused",
                }
            ],
        )
        source = db.task_next_step(task_id)
        assert source is not None

        db.task_report_step(
            task_id,
            source["step_id"],
            result="review finding reproduced",
            success=False,
        )

        rows = db.conn.execute(
            "SELECT id, action, target_file, status, result "
            "FROM task_steps WHERE task_id = ? ORDER BY step_index",
            (task_id,),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["status"] == "failed"
        assert rows[0]["result"] == "review finding reproduced"
        assert rows[1]["action"] == "fix_defect"
        assert rows[1]["target_file"] == "src/thread.py"
        provenance = json.loads(rows[1]["result"])
        assert provenance["remediation_of_step_id"] == rows[0]["id"]
        assert provenance["source_outcome"] == "executor_step_failed"
        child_count = db.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE parent_id = ?", (task_id,)
        ).fetchone()[0]
        assert child_count == 0
    finally:
        db.close()


def test_no_pending_does_not_fake_review_while_failed_history_unresolved(tmp_path):
    db = _new_db(tmp_path)
    try:
        task_id = db.task_create(
            "unresolved history",
            steps=[{"action": "implement", "target_file": "src/thread.py"}],
        )
        source = db.task_next_step(task_id)
        assert source is not None
        db.task_report_step(task_id, source["step_id"], "failed", success=False)

        remediation = db.task_next_step(task_id)
        assert remediation is not None
        assert remediation["action"] == "fix_defect"
        db.task_report_step(task_id, remediation["step_id"], "fixed", success=True)

        task_status = db.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        failed_status = db.conn.execute(
            "SELECT status FROM task_steps WHERE id = ?", (source["step_id"],)
        ).fetchone()[0]
        active_count = db.conn.execute(
            "SELECT COUNT(*) FROM task_steps WHERE task_id = ? "
            "AND status IN ('pending', 'in_progress')",
            (task_id,),
        ).fetchone()[0]
        assert active_count == 0
        assert failed_status == "failed"
        assert task_status == "in_progress"
    finally:
        db.close()


def test_python_parity_appends_reviewer_remediation_to_same_task(tmp_path):
    db = _new_db(tmp_path)
    try:
        task_id = db.task_create(
            "review remediation",
            steps=[{"action": "implement", "target_file": "src/review.py"}],
        )
        source = db.task_next_step(task_id)
        assert source is not None
        db.task_report_step(task_id, source["step_id"], "immutable delivery", success=True)
        db.conn.execute(
            "UPDATE tasks SET status = 'review' WHERE id = ?",
            (task_id,),
        )
        findings = [{"finding_id": "F-PY-1", "fact": "missing transition"}]
        db.conn.execute(
            "INSERT INTO task_verdict_events "
            "(verdict_id, task_id, contract_id, contract_revision, contract_hash, "
            "phase, reviewer_identity, findings, overall, attestation, submitted_at) "
            "VALUES ('V-PY-1', ?, 'TC-PY', 1, 'sha256:task', "
            "'blind_first_pass', '{}', ?, 'block', 'attested', 1.0)",
            (task_id, json.dumps(findings, ensure_ascii=False)),
        )
        db.conn.commit()
        identity = {
            "agent_id": "agent-python-remediation",
            "session_id": "python-remediation-session",
            "model_id": "test-model",
            "role": "implementer",
        }
        lease_ok, lease = db.acquire_lease(task_id, "implementer", identity)
        assert lease_ok
        params = {
            "task_id": task_id,
            "source_step_id": source["step_id"],
            "request_id": "python-review-remediation-1",
            "source_outcome": "reviewer_blocked",
            "source_verdict_id": "V-PY-1",
            "source_findings": findings,
            "identity": identity,
            "lease_token": lease["token"],
            "fencing_counter": lease["fencing_counter"],
        }
        created = db.task_append_remediation_step(**params)
        assert created["replayed"] is False
        replay = db.task_append_remediation_step(**params)
        assert replay["replayed"] is True
        assert replay["remediation_step_id"] == created["remediation_step_id"]

        changed = dict(params)
        changed["source_findings"] = [
            {"finding_id": "F-PY-CHANGED", "fact": "different request"}
        ]
        assert db.task_append_remediation_step(**changed)["error"] == (
            "E_REQUEST_ID_REUSE_MISMATCH"
        )
        task_status = db.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        source_row = db.conn.execute(
            "SELECT status, result FROM task_steps WHERE id = ?",
            (source["step_id"],),
        ).fetchone()
        remediation = db.conn.execute(
            "SELECT result FROM task_steps WHERE id = ?",
            (created["remediation_step_id"],),
        ).fetchone()
        transition = db.conn.execute(
            "SELECT from_status, to_status FROM task_events "
            "WHERE task_id = ? AND reason_code = 'remediation_created'",
            (task_id,),
        ).fetchone()
        assert task_status == "in_progress"
        assert source_row["status"] == "done"
        assert source_row["result"] == "immutable delivery"
        assert json.loads(remediation["result"])["source_verdict_id"] == "V-PY-1"
        assert tuple(transition) == ("review", "in_progress")
        assert db.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE parent_id = ?", (task_id,)
        ).fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM task_verdict_events WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == 1
    finally:
        db.close()
