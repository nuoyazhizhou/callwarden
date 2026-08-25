from __future__ import annotations
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))
from callwarden.server.daemon_client import UnixDaemonRpcClient

OUTPUT_DIR = PROJECT_ROOT / "deliverables" / "software-company"
TASKS = {
    "T-1787305175972-8712da28": {
        "name": "P0-C task contract bootstrap",
        "summary": "Implemented daemon-native, append-only task.contract_bootstrap; atomically initializes Task Contract, role contract lineage/revisions and executor step bindings only where the complete governance projection is absent.",
        "files": [
            "rust_ext/src/daemon/task_loop/task_contract_bootstrap.rs",
            "rust_ext/src/daemon/task_loop/mod.rs",
            "rust_ext/src/daemon/task_collab.rs",
            "rust_ext/src/daemon/dispatch.rs",
            "rust_ext/src/daemon/task_loop/operation_store.rs",
            "server/daemon_client.py",
            "cli/main.py",
            "deliverables/software-company/p0c_task_contract_bootstrap_test.log",
            "deliverables/software-company/p0c_runtime_probe.json",
        ],
        "tests": ["task_contract_bootstrap targeted library tests: 2 passed, 0 failed", "CLI parser and Python module compile checks passed", "runtime probe: empty request rejected as E_TASK_CONTRACT_BOOTSTRAP_WORKSPACE_REQUIRED"],
        "steps": {
            "implement": "Rust task-loop domain and daemon handler completed.",
            "wire": "Protected dispatch and operation ledger registration completed.",
            "adapt_client_cli": "daemon-only client wrapper and CLI command completed.",
            "test": "Targeted Rust and Python/CLI validation completed.",
            "release_verify": "Released before P0-E; later P0-E release includes this source baseline.",
        },
    },
    "T-1787305268313-06fcef5c": {
        "name": "P0-D immutable binding/capture freshness consistency",
        "summary": "Corrected task.next_action authority validation to accept an immutable binding to a historical capture when the current capture preserves the same stable registry identity; real identity drift remains fail-closed.",
        "files": [
            "rust_ext/src/daemon/task_loop/next_action.rs",
            "rust_ext/src/daemon/task_loop/next_action_test.rs",
            "deliverables/software-company/p0d_next_action_test.log",
        ],
        "tests": ["Historical same-identity capture acceptance and real identity-drift rejection targeted Rust regression tests completed", "Current P0-E release contains the P0-D source baseline"],
        "steps": {
            "implement": "Authority freshness validation corrected without mutating historical binding/capture records.",
            "test": "Regression coverage added for same-identity history and true identity drift.",
            "release_verify": "Released before P0-E; later P0-E release includes this source baseline.",
        },
    },
    "T-1787307743865-696714f0": {
        "name": "P0-E adjudicator/reviewer lease delegation",
        "summary": "Introduced a governance-only reviewer-lease validator: a registered adjudicator can finalize only with an active registered reviewer holder that is distinct by agent, instance and session; ordinary same-holder mutation checks remain unchanged.",
        "files": [
            "rust_ext/src/daemon/task_collab.rs",
            "rust_ext/src/daemon/task_supersede.rs",
            "deliverables/software-company/p0e_lease_delegation_test.log",
            "deliverables/software-company/p0e_adjudicator_reviewer_lease_delegation_contract.md",
        ],
        "tests": ["P0-E targeted Rust test: 1 passed, 0 failed", "Distinct reviewer/adjudicator path passes; same agent, instance, session, token and fencing failures are rejected"],
        "steps": {
            "implement": "Cross-role governance validator implemented in task_collab.",
            "wire": "Only supersede, legacy authority attestation and contract bootstrap use the delegation validator.",
            "test": "Cross-role separation and lease rejection matrix passed.",
            "release_verify": "P0-E release runtime refresh completed; current daemon ping is captured below.",
        },
    },
}

def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main() -> None:
    ping = UnixDaemonRpcClient().call("ping", {})
    now = datetime.now(timezone.utc).isoformat()
    output = {}
    for task_id, spec in TASKS.items():
        artifact_hashes = {}
        missing = []
        for rel in spec["files"]:
            p = PROJECT_ROOT / rel
            if p.exists():
                artifact_hashes[rel] = {"sha256": sha256(p), "bytes": p.stat().st_size}
            else:
                missing.append(rel)
        manifest = {
            "manifest_version": "p0-executor-handoff/v1",
            "task_id": task_id,
            "task_name": spec["name"],
            "executor_identity": {
                "agent_id": "executor-manus-governance-v1",
                "session_id": "manus-governance-reconciliation-20260821",
                "model_id": "manus-agent",
                "role": "executor",
            },
            "created_at": now,
            "summary": spec["summary"],
            "step_results": spec["steps"],
            "test_results": spec["tests"],
            "runtime_ping_after_latest_release": ping,
            "artifact_hashes": artifact_hashes,
            "missing_artifacts": missing,
            "reviewer_scope": "Read-only independent verification of source diffs, test logs, runtime probe/ping, release baseline and append-only/no-direct-SQL claims.",
        }
        out_path = OUTPUT_DIR / f"executor_handoff_manifest_{task_id}.json"
        out_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output[task_id] = {"path": str(out_path), "sha256": sha256(out_path)}
    print(json.dumps(output, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
