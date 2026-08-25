"""经 daemon authority 创建 P0-C Task Contract bootstrap 治理任务。"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from callwarden.server.daemon_client import get_daemon_client

PARENT_ID = "T-1787203926824-9f873bfc"
WORKSPACE_ID = 1
WORKSPACE_INSTANCE_ID = "ws-1"
TITLE = "P0-C：Task Contract bootstrap / publication（A′ 调度前置）"
CONTRACT_PATH = PROJECT_ROOT / "deliverables" / "software-company" / "p0c_task_contract_bootstrap_contract.md"
TEMPLATE_DIR = PROJECT_ROOT / "deliverables" / "software-company" / "aprime_role_contracts"


def digest(name: str) -> str:
    return hashlib.sha256((TEMPLATE_DIR / name).read_bytes()).hexdigest().upper()


def contracts() -> list[dict]:
    common_executor_paths = [
        "rust_ext/src/daemon/task_loop/task_contract_bootstrap.rs",
        "rust_ext/src/daemon/task_loop/mod.rs",
        "rust_ext/src/daemon/task_collab.rs",
        "rust_ext/src/daemon/dispatch.rs",
        "server/daemon_client.py",
        "cli/main.py",
        "rust_ext/src/daemon/task_loop/*test*.rs",
        "rust_ext/src/daemon/*test*.rs",
        "tests/",
    ]
    return [
        {
            "role": "executor",
            "skill_id": "none",
            "skill_version": "",
            "prompt_template_id": "cw.aprime.executor.startup.v1",
            "prompt_hash": digest("executor_planner_startup_v1.md"),
            "allowed_paths": json.dumps(common_executor_paths),
            "forbidden_paths": json.dumps([
                "db/schema.py", "migrations/", "task_workspace_bindings direct SQL",
                "task_contract_revisions direct SQL", "existing task fields/status/verdict",
            ]),
            "commands": "daemon-native Rust tests; Python client/CLI tests; release daemon round-trip",
            "acceptance_checks": "only empty task contracts bootstrapped; authority/identity/lease/evidence/ledger fail-closed; append-only revision and audit",
            "required_evidence": "test output, runtime transcript, evidence manifest/hash, append-only database projections",
            "handoff_to": "reviewer",
            "independence": "required",
        },
        {
            "role": "reviewer",
            "skill_id": "none",
            "skill_version": "",
            "prompt_template_id": "cw.aprime.reviewer.startup.v1",
            "prompt_hash": digest("reviewer_startup_v1.md"),
            "allowed_paths": "read-only P0-C source, test, evidence and runtime projection",
            "forbidden_paths": "production edits; task.apply; task.close; direct database writes",
            "commands": "read-only review",
            "acceptance_checks": "verify task.contract_set is not misused; only canonical envelope revision 1; all negative cases and runtime proof",
            "required_evidence": "independent PASS or BLOCKED record",
            "handoff_to": "adjudicator",
            "independence": "required",
        },
        {
            "role": "adjudicator",
            "skill_id": "none",
            "skill_version": "",
            "prompt_template_id": "cw.aprime.adjudicator.startup.v1",
            "prompt_hash": digest("adjudicator_startup_v1.md"),
            "allowed_paths": "P0-C lifecycle finalization only",
            "forbidden_paths": "production edits; direct database writes; S2 supersede before finalization",
            "commands": "task.apply; task.close; task.next_action after reviewer PASS and valid lease/fencing",
            "acceptance_checks": "independent reviewer PASS; apply then close then COMPLETE",
            "required_evidence": "finalization manifest",
            "handoff_to": "complete",
            "independence": "required",
        },
    ]


def main() -> None:
    client = get_daemon_client()
    listing = client.call("task.list", {
        "parent_id": PARENT_ID,
        "status": "",
        "limit": 200,
        "workspace_id": WORKSPACE_ID,
        "workspace_instance_id": WORKSPACE_INSTANCE_ID,
    })
    existing = listing.get("tasks", listing) if isinstance(listing, dict) else listing
    for item in existing if isinstance(existing, list) else []:
        if isinstance(item, dict) and item.get("title") == TITLE:
            print(json.dumps({"result": "exists", "task": item}, ensure_ascii=False))
            return
    response = client.call("task.create", {
        "title": TITLE,
        "description": CONTRACT_PATH.read_text(encoding="utf-8"),
        "creator": "executor-planner",
        "parent_id": PARENT_ID,
        "workspace_id": WORKSPACE_ID,
        "workspace_instance_id": WORKSPACE_INSTANCE_ID,
        "steps": [
            {"action": "implement", "target_file": "rust_ext/src/daemon/task_loop/task_contract_bootstrap.rs", "target_symbol": "bootstrap_task_contract", "check_items": ["strict envelope canonicalization", "authority identity lease evidence", "ledger atomicity"]},
            {"action": "wire", "target_file": "rust_ext/src/daemon/task_collab.rs; rust_ext/src/daemon/dispatch.rs", "target_symbol": "task.contract_bootstrap", "check_items": ["protected mutation", "daemon-only route", "no contract_set confusion"]},
            {"action": "adapt_client_cli", "target_file": "server/daemon_client.py; cli/main.py", "target_symbol": "task_contract_bootstrap", "check_items": ["explicit credentials", "no local fallback"]},
            {"action": "test", "target_file": "rust_ext/src/daemon/task_loop/*test*.rs; tests/", "target_symbol": "P0-C positive/idempotent/negative matrix", "check_items": ["empty contract success", "existing contract reject", "authority/lease/evidence rejects", "status unchanged"]},
            {"action": "release_verify", "target_file": "runtime/current", "target_symbol": "P0-C daemon round-trip", "check_items": ["build deploy evidence", "runtime RPC proof", "no direct database mutation"]},
        ],
        "role_contracts": contracts(),
    })
    print(json.dumps({"result": "created", "response": response}, ensure_ascii=False))


if __name__ == "__main__":
    main()
