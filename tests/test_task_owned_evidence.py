"""task.report task-owned attribution contract checks."""

import hashlib
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_daemon_report_has_explicit_change_whitelist_and_atomic_audit():
    source = (ROOT / "rust_ext/src/daemon/task_collab.rs").read_text(encoding="utf-8")
    assert 'params.get("changes")' in source
    assert 'E_CHANGE_PATH_NOT_ALLOWED' in source
    assert 'INSERT OR REPLACE INTO change_audit' in source


def test_cli_report_exposes_task_bound_evidence_inputs():
    source = (ROOT / "cli/main.py").read_text(encoding="utf-8")
    assert '"--evidence-path"' in source
    assert '"--evidence-hash"' in source
    assert '"--changes-json"' in source
    assert '"changes"] = report_changes' in source


def test_daemon_authority_exposes_read_only_evidence_and_gate_queries():
    dispatch = (ROOT / "rust_ext/src/daemon/dispatch.rs").read_text(encoding="utf-8")
    source = (ROOT / "rust_ext/src/daemon/task_collab.rs").read_text(encoding="utf-8")
    assert '"evidence.query" => store.handle_evidence_query' in dispatch
    assert '"gate.decision.query" => store.handle_gate_decision_query' in dispatch
    assert "pub fn handle_evidence_query" in source
    assert "pub fn handle_gate_decision_query" in source


@pytest.mark.skipif(
    not os.environ.get("CW_TASK_OWNED_EVIDENCE_E2E"),
    reason="real daemon round-trip is enabled explicitly by the runtime gate acceptance",
)
def test_real_daemon_rejects_non_whitelisted_change_without_writing():
    from callwarden.server.daemon_client import UnixDaemonRpcClient

    task_id = os.environ["CW_EVIDENCE_TASK_ID"]
    step_id = os.environ["CW_EVIDENCE_STEP_ID"]
    token = os.environ["CW_EVIDENCE_LEASE_TOKEN"]
    counter = int(os.environ["CW_EVIDENCE_FENCING_COUNTER"])
    identity = {
        "agent_id": os.environ["CW_EVIDENCE_AGENT_ID"],
        "agent_instance_id": os.environ.get("CW_EVIDENCE_AGENT_INSTANCE_ID", ""),
        "client_id": os.environ.get("CW_EVIDENCE_CLIENT_ID", ""),
        "session_id": os.environ["CW_EVIDENCE_SESSION_ID"],
        "model_id": os.environ["CW_EVIDENCE_MODEL_ID"],
        "role": "implementer",
        "provider": "openai",
    }
    client = UnixDaemonRpcClient()
    before = client.call(
        "evidence.query",
        {"request_id": "e2e-before", "task_id": task_id},
    )
    before_count = len(before.get("change_audit", []))
    with pytest.raises(Exception) as exc_info:
        client.call(
            "task.report",
            {
                "request_id": "e2e-invalid-path",
                "task_id": task_id,
                "step_id": step_id,
                "summary": "must reject non-whitelisted path",
                "success": True,
                "identity": identity,
                "agent_session_id": identity["session_id"],
                "lease_token": token,
                "fencing_counter": counter,
                "changes": [{
                    "file_path": "unrelated/dirty.txt",
                    "hash_after": hashlib.sha256(b"unrelated").hexdigest(),
                }],
            },
        )
    assert "E_CHANGE_PATH_NOT_ALLOWED" in str(exc_info.value)
    after = client.call(
        "evidence.query",
        {"request_id": "e2e-after", "task_id": task_id},
    )
    assert len(after.get("change_audit", [])) == before_count
