"""经 daemon authority 创建 P0-B 历史任务 workspace binding 修复任务。

仅创建新的 Epic 子任务；不更新旧 S2、不执行 supersede/apply/close、不改生产代码。
"""
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
TITLE = "P0-B：历史任务 workspace authority attestation / binding（supersede 前置）"
CONTRACT_PATH = PROJECT_ROOT / "deliverables" / "software-company" / "p0b_legacy_task_workspace_attestation_binding_contract.md"
TEMPLATE_DIR = PROJECT_ROOT / "deliverables" / "software-company" / "aprime_role_contracts"


def file_hash(filename: str) -> str:
    return hashlib.sha256((TEMPLATE_DIR / filename).read_bytes()).hexdigest().upper()


def role_contracts() -> list[dict]:
    return [
        {
            "role": "executor",
            "skill_id": "none",
            "skill_version": "",
            "prompt_template_id": "cw.aprime.executor.startup.v1",
            "prompt_hash": file_hash("executor_planner_startup_v1.md"),
            "allowed_paths": json.dumps([
                "rust_ext/src/daemon/task_collab.rs",
                "rust_ext/src/daemon/task_supersede.rs",
                "rust_ext/src/daemon/dispatch.rs",
                "server/daemon_client.py",
                "cli/main.py",
                "rust_ext/src/daemon/*test*.rs",
                "tests/",
            ]),
            "forbidden_paths": json.dumps([
                "db/schema.py",
                "migrations/",
                "deliverables/software-company/tool_migration_matrix.json",
                "existing task records except daemon audit/projection during test fixtures",
            ]),
            "commands": "daemon-native unit tests; thin-client/CLI tests; release daemon round-trip",
            "acceptance_checks": "append-only legacy binding, operation-ledger idempotency, negative authority/lease/evidence cases, task status immutability",
            "required_evidence": "test output, runtime RPC transcript, evidence manifest/hash, database projection",
            "handoff_to": "reviewer",
            "independence": "required",
        },
        {
            "role": "reviewer",
            "skill_id": "none",
            "skill_version": "",
            "prompt_template_id": "cw.aprime.reviewer.startup.v1",
            "prompt_hash": file_hash("reviewer_startup_v1.md"),
            "allowed_paths": "read-only review of P0-B allowed paths and evidence",
            "forbidden_paths": "production edits; task.apply; task.close; task.supersede",
            "commands": "read-only code/test/evidence inspection",
            "acceptance_checks": "verify no local fallback, no schema change, no old task mutation, all P0-B positive/negative cases",
            "required_evidence": "independent review record with findings or PASS evidence hash",
            "handoff_to": "adjudicator",
            "independence": "required",
        },
        {
            "role": "adjudicator",
            "skill_id": "none",
            "skill_version": "",
            "prompt_template_id": "cw.aprime.adjudicator.startup.v1",
            "prompt_hash": file_hash("adjudicator_startup_v1.md"),
            "allowed_paths": "P0-B final adjudication and protected lifecycle actions only",
            "forbidden_paths": "production edits; historical task record mutation; direct SQLite fallback",
            "commands": "task.apply; task.close; task.next_action after validated reviewer lease/fencing",
            "acceptance_checks": "Reviewer PASS, role independence, valid reviewer lease/fencing, apply then close then COMPLETE",
            "required_evidence": "finalization evidence manifest with apply/close/COMPLETE projections",
            "handoff_to": "complete",
            "independence": "required",
        },
    ]


def main() -> None:
    client = get_daemon_client()
    existing = client.call(
        "task.list",
        {
            "parent_id": PARENT_ID,
            "status": "",
            "limit": 200,
            "workspace_id": WORKSPACE_ID,
            "workspace_instance_id": WORKSPACE_INSTANCE_ID,
        },
    )
    candidates = existing.get("tasks", existing) if isinstance(existing, dict) else existing
    for item in candidates if isinstance(candidates, list) else []:
        if isinstance(item, dict) and item.get("title") == TITLE:
            print(json.dumps({"result": "exists", "task": item}, ensure_ascii=False))
            return

    response = client.call(
        "task.create",
        {
            "title": TITLE,
            "description": CONTRACT_PATH.read_text(encoding="utf-8"),
            "creator": "executor-planner",
            "parent_id": PARENT_ID,
            "workspace_id": WORKSPACE_ID,
            "workspace_instance_id": WORKSPACE_INSTANCE_ID,
            "steps": [
                {
                    "action": "implement",
                    "target_file": "rust_ext/src/daemon/task_supersede.rs",
                    "target_symbol": "handle_task_attest_legacy_workspace_binding",
                    "check_items": [
                        "strict legacy/anchor/authority/identity/lease/evidence validation",
                        "operation ledger idempotency",
                        "append-only binding/capture/audit only",
                    ],
                },
                {
                    "action": "wire",
                    "target_file": "rust_ext/src/daemon/dispatch.rs",
                    "target_symbol": "task.attest_legacy_workspace_binding route",
                    "check_items": [
                        "exact RPC route",
                        "unknown/missing input fail-closed",
                        "no bypass around daemon authority",
                    ],
                },
                {
                    "action": "adapt_client_cli",
                    "target_file": "server/daemon_client.py; cli/main.py",
                    "target_symbol": "task_attest_legacy_workspace_binding",
                    "check_items": [
                        "thin client only",
                        "CLI explicit parameters",
                        "daemon unavailable has no local fallback",
                    ],
                },
                {
                    "action": "test",
                    "target_file": "rust_ext/src/daemon/*test*.rs; tests/",
                    "target_symbol": "P0-B positive/idempotent/negative matrix",
                    "check_items": [
                        "positive legacy attestation",
                        "replay and request mismatch",
                        "all authority/identity/lease/fencing/evidence negatives",
                        "legacy task status remains unchanged",
                    ],
                },
                {
                    "action": "release_verify",
                    "target_file": "runtime/current",
                    "target_symbol": "release daemon P0-B round-trip",
                    "check_items": [
                        "build/deploy evidence",
                        "runtime native RPC proof",
                        "no direct SQLite production mutation",
                    ],
                },
            ],
            "role_contracts": role_contracts(),
        },
    )
    print(json.dumps({"result": "created", "response": response}, ensure_ascii=False))


if __name__ == "__main__":
    main()
