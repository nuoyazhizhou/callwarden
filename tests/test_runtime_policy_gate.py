"""项目级 runtime deployment gate 回归测试。"""

import hashlib
import json
import time

from callwarden.db.db import CodeGraphDB


def _db(tmp_path):
    return CodeGraphDB(
        db_path=str(tmp_path / "runtime-policy.db"),
        workspace_root=str(tmp_path),
    )


def _set_policy(db, policy):
    db.conn.execute(
        "UPDATE workspaces SET runtime_policy = ? WHERE is_active = 1",
        (policy,),
    )
    db.conn.commit()


def _report_daemon_change(db, task_id):
    step = db.task_next_step(task_id)
    assert step is not None
    return db.task_report_step(
        task_id,
        step["step_id"],
        result="daemon source changed",
        success=True,
        changes=[{
            "file_path": "rust_ext/src/daemon/task_collab.rs",
            "hash_before": "sha256:before",
            "hash_after": "sha256:after",
            "diff": "structured handoff",
        }],
    )


def _runtime_provenance():
    return {
        "build_hash": "sha256:" + "a" * 64,
        "runtime_hash": "sha256:" + "a" * 64,
        "pid_hash": "sha256:" + "a" * 64,
        "daemon_ping": True,
        "rpc_round_trip": True,
    }


def _insert_runtime_evidence(db, task_id, evidence_id="E-runtime-test"):
    provenance = _runtime_provenance()
    payload_hash = "sha256:" + hashlib.sha256(
        json.dumps(provenance, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    db.conn.execute(
        """
        INSERT INTO task_evidence_events
            (evidence_id, task_id, contract_id, contract_revision, contract_hash,
             evidence_type, event_type, workspace_snapshot_id, file_hashes,
             payload_hash, produced_at)
        VALUES (?, ?, ?, 1, ?, 'runtime_deployment', 'evidence_appended', ?, ?, ?, ?)
        """,
        (
            evidence_id,
            task_id,
            "C-runtime-test",
            "sha256:contract",
            "snapshot-runtime",
            json.dumps({"runtime_provenance": provenance}, sort_keys=True),
            payload_hash,
            time.time(),
        ),
    )
    db.conn.commit()


def test_self_bootstrap_adds_runtime_step_and_blocks_review(tmp_path):
    db = _db(tmp_path)
    assert "runtime_policy" in {
        row[1] for row in db.conn.execute("PRAGMA table_info(workspaces)").fetchall()
    }
    _set_policy(db, "self_bootstrap")
    task_id = db.task_create(
        "daemon change",
        steps=[{"action": "modify", "target_file": "rust_ext/src/daemon/task_collab.rs"}],
    )

    result = _report_daemon_change(db, task_id)

    assert result["runtime_gate"]["code"] == "E_RUNTIME_DEPLOYMENT_REQUIRED"
    runtime = db.conn.execute(
        "SELECT action, status, target_file FROM task_steps WHERE task_id = ? AND action = 'runtime_deployment'",
        (task_id,),
    ).fetchone()
    assert runtime["status"] == "pending"
    assert runtime["target_file"] == "scripts/refresh_shared_runtime.ps1"
    status = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert status["status"] != "review"
    db.close()


def test_runtime_step_retry_does_not_duplicate_without_evidence(tmp_path):
    db = _db(tmp_path)
    _set_policy(db, "self_bootstrap")
    task_id = db.task_create(
        "runtime deployment evidence required",
        steps=[{"action": "modify", "target_file": "rust_ext/src/daemon/task_collab.rs"}],
    )
    first = _report_daemon_change(db, task_id)
    runtime_id = first["runtime_gate"]["runtime_step_id"]
    claimed = db.task_next_step(task_id)
    assert claimed["step_id"] == runtime_id
    second = db.task_report_step(task_id, runtime_id, result="missing evidence", success=True)
    assert second["runtime_gate"]["code"] == "E_RUNTIME_EVIDENCE_REQUIRED"
    rows = db.conn.execute(
        "SELECT id, status FROM task_steps WHERE task_id = ? AND action = 'runtime_deployment'",
        (task_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "in_progress"
    db.close()


def test_standard_project_does_not_require_runtime_deployment(tmp_path):
    db = _db(tmp_path)
    _set_policy(db, "standard")
    task_id = db.task_create(
        "ordinary daemon change",
        steps=[{"action": "modify", "target_file": "rust_ext/src/daemon/task_collab.rs"}],
    )

    result = _report_daemon_change(db, task_id)

    assert result is None
    assert not db.conn.execute(
        "SELECT 1 FROM task_steps WHERE task_id = ? AND action = 'runtime_deployment'",
        (task_id,),
    ).fetchone()
    status = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert status["status"] == "review"
    db.close()


def test_self_bootstrap_runtime_evidence_allows_review(tmp_path):
    db = _db(tmp_path)
    _set_policy(db, "self_bootstrap")
    task_id = db.task_create(
        "daemon change with deployed runtime",
        steps=[{"action": "modify", "target_file": "rust_ext/src/daemon/task_collab.rs"}],
    )
    blocked = _report_daemon_change(db, task_id)
    runtime_step_id = blocked["runtime_gate"]["runtime_step_id"]
    _insert_runtime_evidence(db, task_id)

    result = db.task_report_step(
        task_id,
        runtime_step_id,
        result="runtime deployed",
        success=True,
    )

    assert result is None
    runtime = db.conn.execute(
        "SELECT status FROM task_steps WHERE task_id = ? AND action = 'runtime_deployment'",
        (task_id,),
    ).fetchone()
    assert runtime["status"] == "done"
    status = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert status["status"] == "review"
    db.close()


def test_runtime_evidence_without_provenance_is_rejected(tmp_path):
    db = _db(tmp_path)
    _set_policy(db, "self_bootstrap")
    task_id = db.task_create(
        "daemon change with incomplete runtime evidence",
        steps=[{"action": "modify", "target_file": "rust_ext/src/daemon/task_collab.rs"}],
    )
    blocked = _report_daemon_change(db, task_id)
    runtime_step_id = blocked["runtime_gate"]["runtime_step_id"]
    db.conn.execute(
        """
        INSERT INTO task_evidence_events
            (evidence_id, task_id, contract_id, contract_revision, contract_hash,
             evidence_type, event_type, workspace_snapshot_id, payload_hash, produced_at)
        VALUES (?, ?, ?, 1, ?, 'runtime_deployment', 'evidence_appended', ?, ?, ?)
        """,
        ("E-runtime-incomplete", task_id, "C-runtime-test", "sha256:contract",
         "snapshot-runtime", "sha256:" + "b" * 64, time.time()),
    )
    db.conn.commit()
    result = db.task_report_step(task_id, runtime_step_id, result="incomplete", success=True)
    assert result["runtime_gate"]["code"] == "E_RUNTIME_EVIDENCE_REQUIRED"
    db.close()


def test_runtime_evidence_direct_outer_hashes_are_rejected(tmp_path):
    db = _db(tmp_path)
    _set_policy(db, "self_bootstrap")
    task_id = db.task_create(
        "daemon change with direct runtime hashes",
        steps=[{"action": "modify", "target_file": "rust_ext/src/daemon/task_collab.rs"}],
    )
    blocked = _report_daemon_change(db, task_id)
    runtime_step_id = blocked["runtime_gate"]["runtime_step_id"]
    provenance = _runtime_provenance()
    payload_hash = "sha256:" + hashlib.sha256(
        json.dumps(provenance, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    db.conn.execute(
        """
        INSERT INTO task_evidence_events
            (evidence_id, task_id, contract_id, contract_revision, contract_hash,
             evidence_type, event_type, workspace_snapshot_id, file_hashes,
             payload_hash, produced_at)
        VALUES (?, ?, ?, 1, ?, 'runtime_deployment', 'evidence_appended', ?, ?, ?, ?)
        """,
        ("E-runtime-direct", task_id, "C-runtime-test", "sha256:contract",
         "snapshot-runtime", json.dumps(provenance, sort_keys=True), payload_hash, time.time()),
    )
    db.conn.commit()

    assert db._has_valid_runtime_evidence(task_id) is False
    result = db.task_report_step(
        task_id, runtime_step_id, result="direct envelope", success=True
    )
    assert result["runtime_gate"]["code"] == "E_RUNTIME_EVIDENCE_REQUIRED"
    db.close()


def test_target_file_without_changes_cannot_bypass_runtime_gate(tmp_path):
    db = _db(tmp_path)
    _set_policy(db, "self_bootstrap")
    task_id = db.task_create(
        "target-only daemon change",
        steps=[{"action": "modify", "target_file": "rust_ext/src/daemon/task_collab.rs"}],
    )
    step = db.task_next_step(task_id)
    result = db.task_report_step(task_id, step["step_id"], result="target only", success=True)
    assert result["runtime_gate"]["code"] == "E_RUNTIME_DEPLOYMENT_REQUIRED"
    assert db.conn.execute(
        "SELECT 1 FROM task_steps WHERE task_id = ? AND action = 'runtime_deployment'",
        (task_id,),
    ).fetchone()
    db.close()


def test_standard_project_explicit_contract_requires_runtime(tmp_path):
    db = _db(tmp_path)
    _set_policy(db, "standard")
    task_id = db.task_create(
        "ordinary project deployment:required",
        description="deployment:required",
        steps=[{"action": "modify", "target_file": "rust_ext/src/daemon/task_collab.rs"}],
    )
    result = _report_daemon_change(db, task_id)
    assert result["runtime_gate"]["code"] == "E_RUNTIME_DEPLOYMENT_REQUIRED"
    db.close()


def test_self_bootstrap_missing_path_provenance_fails_closed(tmp_path):
    db = _db(tmp_path)
    _set_policy(db, "self_bootstrap")
    task_id = db.task_create(
        "change without path provenance",
        steps=[{"action": "modify"}],
    )
    step = db.task_next_step(task_id)
    result = db.task_report_step(
        task_id,
        step["step_id"],
        result="reported without changes or target",
        success=True,
        changes=[],
    )
    assert result["runtime_gate"]["required"] is True
    assert result["runtime_gate"]["passed"] is False
    assert result["runtime_gate"]["code"] == "E_RUNTIME_DEPLOYMENT_REQUIRED"
    assert db.conn.execute(
        "SELECT 1 FROM task_steps WHERE task_id = ? AND action = 'runtime_deployment'",
        (task_id,),
    ).fetchone()
    db.close()


def test_explicit_deployment_required_missing_path_provenance_fails_closed(tmp_path):
    db = _db(tmp_path)
    _set_policy(db, "standard")
    task_id = db.task_create(
        "explicit deployment without path provenance",
        description="runtime_deployment_required",
        steps=[{"action": "modify"}],
    )
    step = db.task_next_step(task_id)
    result = db.task_report_step(
        task_id,
        step["step_id"],
        result="reported without changes or target",
        success=True,
        changes=[],
    )
    assert result["runtime_gate"]["required"] is True
    assert result["runtime_gate"]["passed"] is False
    assert result["runtime_gate"]["code"] == "E_RUNTIME_DEPLOYMENT_REQUIRED"
    assert db.conn.execute(
        "SELECT 1 FROM task_steps WHERE task_id = ? AND action = 'runtime_deployment'",
        (task_id,),
    ).fetchone()
    db.close()
