"""经 daemon authority 创建 P0-E Adjudicator/Reviewer lease delegation 治理任务。"""
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
TITLE = "P0-E：Adjudicator / Reviewer lease delegation（A′ 治理收尾前置）"
CONTRACT_PATH = PROJECT_ROOT / "deliverables" / "software-company" / "p0e_adjudicator_reviewer_lease_delegation_contract.md"
TEMPLATE_DIR = PROJECT_ROOT / "deliverables" / "software-company" / "aprime_role_contracts"


def digest(name: str) -> str:
    return hashlib.sha256((TEMPLATE_DIR / name).read_bytes()).hexdigest().upper()


def contracts() -> list[dict]:
    paths = [
        "rust_ext/src/daemon/task_collab.rs",
        "rust_ext/src/daemon/task_supersede.rs",
        "rust_ext/src/daemon/*test*.rs",
        "tests/",
    ]
    return [
        {
            "role": "executor", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.executor.startup.v1",
            "prompt_hash": digest("executor_planner_startup_v1.md"),
            "allowed_paths": json.dumps(paths),
            "forbidden_paths": json.dumps(["db/schema.py", "migrations/", "direct SQLite", "task status/verdict/binding/capture writes"]),
            "commands": "daemon-native Rust governance tests; release daemon round-trip",
            "acceptance_checks": "adjudicator/reviewer separation; holder registration/role/token/fencing/expiry all fail-closed; ordinary same-holder validation unchanged",
            "required_evidence": "test output, runtime transcript, evidence manifest/hash, cross-role separation matrix",
            "handoff_to": "reviewer", "independence": "required",
        },
        {
            "role": "reviewer", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.reviewer.startup.v1",
            "prompt_hash": digest("reviewer_startup_v1.md"),
            "allowed_paths": "read-only P0-E source, tests, evidence and runtime projection",
            "forbidden_paths": "production edits; task.apply; task.close; direct database writes",
            "commands": "read-only review",
            "acceptance_checks": "only governance call sites delegate reviewer lease; all separation and fencing negative cases plus runtime proof",
            "required_evidence": "independent PASS or BLOCKED record",
            "handoff_to": "adjudicator", "independence": "required",
        },
        {
            "role": "adjudicator", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.adjudicator.startup.v1",
            "prompt_hash": digest("adjudicator_startup_v1.md"),
            "allowed_paths": "P0-E lifecycle finalization only",
            "forbidden_paths": "production edits; direct database writes; P0-C bootstrap, P0-B attestation or S2 supersede before finalization",
            "commands": "task.apply; task.close; task.next_action after reviewer PASS and valid lease/fencing",
            "acceptance_checks": "independent reviewer PASS; apply then close then COMPLETE",
            "required_evidence": "finalization manifest",
            "handoff_to": "complete", "independence": "required",
        },
    ]


def main() -> None:
    client = get_daemon_client()
    listing = client.call("task.list", {
        "parent_id": PARENT_ID, "status": "", "limit": 200,
        "workspace_id": WORKSPACE_ID, "workspace_instance_id": WORKSPACE_INSTANCE_ID,
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
            {"action": "implement", "target_file": "rust_ext/src/daemon/task_collab.rs", "target_symbol": "validate_reviewer_lease_for_adjudication", "check_items": ["reviewer holder validation", "agent-instance-session separation", "ordinary mutation semantics unchanged"]},
            {"action": "wire", "target_file": "rust_ext/src/daemon/task_supersede.rs; rust_ext/src/daemon/task_collab.rs", "target_symbol": "governance reviewer lease call sites", "check_items": ["supersede", "legacy attestation", "contract bootstrap only"]},
            {"action": "test", "target_file": "rust_ext/src/daemon/*test*.rs; tests/", "target_symbol": "P0-E cross-role lease matrix", "check_items": ["distinct holder success", "same agent/instance/session reject", "token/fencing/expiry reject"]},
            {"action": "release_verify", "target_file": "runtime/current", "target_symbol": "P0-E daemon round-trip", "check_items": ["build deploy evidence", "fail-closed runtime probe", "no direct database mutation"]},
        ],
        "role_contracts": contracts(),
    })
    print(json.dumps({"result": "created", "response": response}, ensure_ascii=False))


if __name__ == "__main__":
    main()
