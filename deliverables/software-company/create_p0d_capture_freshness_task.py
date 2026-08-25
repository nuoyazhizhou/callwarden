"""经 daemon authority 创建 P0-D capture freshness consistency 修复任务。"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))
from callwarden.server.daemon_client import get_daemon_client

PARENT_ID = "T-1787203926824-9f873bfc"
WORKSPACE_ID = 1
WORKSPACE_INSTANCE_ID = "ws-1"
TITLE = "P0-D：Immutable Binding 与 Workspace Capture Freshness 一致性修复"
CONTRACT = ROOT / "deliverables" / "software-company" / "p0d_capture_freshness_consistency_contract.md"
TEMPLATES = ROOT / "deliverables" / "software-company" / "aprime_role_contracts"


def digest(name: str) -> str:
    return hashlib.sha256((TEMPLATES / name).read_bytes()).hexdigest().upper()


def role_contracts() -> list[dict]:
    return [
        {
            "role": "executor", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.executor.startup.v1",
            "prompt_hash": digest("executor_planner_startup_v1.md"),
            "allowed_paths": json.dumps([
                "rust_ext/src/daemon/task_loop/next_action.rs",
                "rust_ext/src/daemon/task_loop/next_action_test.rs",
            ]),
            "forbidden_paths": json.dumps([
                "db/schema.py", "migrations/", "task_workspace_bindings direct SQL",
                "workspace_authority_captures direct SQL", "task status or contract mutations",
            ]),
            "commands": "targeted next_action Rust tests; release daemon next_action authority proof",
            "acceptance_checks": "same-identity historical capture accepted; identity change/missing/chain mismatch rejected; no binding updates",
            "required_evidence": "test output, runtime next_action transcript, immutable binding projection",
            "handoff_to": "reviewer", "independence": "required",
        },
        {
            "role": "reviewer", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.reviewer.startup.v1",
            "prompt_hash": digest("reviewer_startup_v1.md"),
            "allowed_paths": "read-only next_action implementation, tests and runtime evidence",
            "forbidden_paths": "production edits; task.apply; task.close; direct DB writes",
            "commands": "read-only review",
            "acceptance_checks": "only same stable identity re-attestation is accepted; actual identity drift stays fail-closed",
            "required_evidence": "independent PASS or BLOCKED record",
            "handoff_to": "adjudicator", "independence": "required",
        },
        {
            "role": "adjudicator", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.adjudicator.startup.v1",
            "prompt_hash": digest("adjudicator_startup_v1.md"),
            "allowed_paths": "P0-D lifecycle finalization only",
            "forbidden_paths": "production edits; binding/capture direct writes",
            "commands": "task.apply; task.close; task.next_action after reviewer PASS",
            "acceptance_checks": "independent review PASS and full ACCEPT→apply→close→COMPLETE",
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
    items = listing.get("tasks", listing) if isinstance(listing, dict) else listing
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("title") == TITLE:
            print(json.dumps({"result": "exists", "task": item}, ensure_ascii=False))
            return
    response = client.call("task.create", {
        "title": TITLE,
        "description": CONTRACT.read_text(encoding="utf-8"),
        "creator": "executor-planner", "parent_id": PARENT_ID,
        "workspace_id": WORKSPACE_ID, "workspace_instance_id": WORKSPACE_INSTANCE_ID,
        "steps": [
            {"action": "implement", "target_file": "rust_ext/src/daemon/task_loop/next_action.rs", "target_symbol": "verify_capture", "check_items": ["historical same-identity capture acceptance", "identity change remains fail-closed", "no binding update"]},
            {"action": "test", "target_file": "rust_ext/src/daemon/task_loop/next_action_test.rs", "target_symbol": "capture freshness tests", "check_items": ["same identity accept", "changed identity reject", "missing/chain mismatch reject"]},
            {"action": "release_verify", "target_file": "runtime/current", "target_symbol": "task.next_action authority proof", "check_items": ["release build", "runtime call past authority stage", "binding remains immutable"]},
        ],
        "role_contracts": role_contracts(),
    })
    print(json.dumps({"result": "created", "response": response}, ensure_ascii=False))

if __name__ == "__main__":
    main()
