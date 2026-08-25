"""经 daemon task.create 创建 A′ 逐链路恢复父任务。

该脚本仅创建一个新的 Epic 子任务。它不修改旧任务状态，不执行 task.supersede，
不 apply/close 任务，也不改生产代码或 schema。创建走 daemon 权威写入路径，确保
父链、步骤、workspace authority capture/binding 与 role contracts 位于同一事务。
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
TITLE = "A′ 逐链路 Rust daemon 迁移恢复（MCP/CLI 渐进切换）"
CONTRACT_PATH = PROJECT_ROOT / "deliverables" / "software-company" / "aprime_recovery_parent_task_contract.md"
TEMPLATE_DIR = PROJECT_ROOT / "deliverables" / "software-company" / "aprime_role_contracts"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def contract(role: str, filename: str, handoff_to: str, independence: str,
             allowed: str, forbidden: str, commands: str, acceptance: str,
             evidence: str) -> dict:
    path = TEMPLATE_DIR / filename
    return {
        "role": role,
        "skill_id": "none",
        "skill_version": "",
        "prompt_template_id": f"cw.aprime.{role}.startup.v1",
        "prompt_hash": sha256(path),
        "allowed_paths": allowed,
        "forbidden_paths": forbidden,
        "commands": commands,
        "acceptance_checks": acceptance,
        "required_evidence": evidence,
        "handoff_to": handoff_to,
        "independence": independence,
    }


def role_contracts() -> list[dict]:
    return [
        contract(
            "executor",
            "executor_planner_startup_v1.md",
            "reviewer",
            "required",
            "task-card scoped paths only",
            "task.apply; task.close; task.supersede; out-of-scope production/schema changes",
            "task.next_action; task.claim; task.report; task.handoff",
            "one tool/CLI link; tests; evidence manifest/hash; executor_ready_for_review",
            "implementation plan; test output; negative test; daemon round-trip evidence",
        ),
        contract(
            "reviewer",
            "reviewer_startup_v1.md",
            "adjudicator",
            "required",
            "read-only review evidence and structured review handoff",
            "production edits; task.apply; task.close; task.supersede",
            "task.next_action; task.contract.get; task.handoff",
            "independent verification of scope, diff, tests, evidence, gate and matrix condition",
            "review record; findings or reviewer_pass evidence manifest/hash",
        ),
        contract(
            "adjudicator",
            "adjudicator_startup_v1.md",
            "complete",
            "required",
            "final review and protected task finalization within daemon authority",
            "production edits; local SQLite fallback; status forgery",
            "task.next_action; task.apply; task.close; task.handoff; task.supersede when separately authorized",
            "ACCEPT requires valid reviewer lease/fencing then apply, close and next_action=COMPLETE",
            "final review; lease/fencing provenance; apply/close/COMPLETE verification",
        ),
    ]


def find_existing(client) -> dict | None:
    result = client.call(
        "task.list",
        {
            "parent_id": PARENT_ID,
            "status": "",
            "limit": 200,
            "workspace_id": WORKSPACE_ID,
            "workspace_instance_id": WORKSPACE_INSTANCE_ID,
        },
    )
    candidates = result.get("tasks", result) if isinstance(result, dict) else result
    if not isinstance(candidates, list):
        raise RuntimeError(f"unexpected task.list response: {result!r}")
    for item in candidates:
        if isinstance(item, dict) and item.get("title") == TITLE:
            return item
    return None


def main() -> None:
    client = get_daemon_client()
    existing = find_existing(client)
    if existing is not None:
        print(json.dumps({"result": "exists", "task": existing}, ensure_ascii=False))
        return

    description = CONTRACT_PATH.read_text(encoding="utf-8")
    params = {
        "title": TITLE,
        "description": description,
        "creator": "executor-planner",
        "parent_id": PARENT_ID,
        "workspace_id": WORKSPACE_ID,
        "workspace_instance_id": WORKSPACE_INSTANCE_ID,
        "steps": [
            {
                "action": "govern",
                "target_file": "deliverables/software-company/aprime_recovery_parent_task_contract.md",
                "target_symbol": "A′ rolling pipeline governance",
                "check_items": [
                    "A′ parent is workspace-bound",
                    "three role contracts are frozen",
                    "CLI-01 is the only initially permitted child",
                    "old S2 supersede is deferred to authorized adjudicator",
                ],
            },
            {
                "action": "verify",
                "target_file": "deliverables/software-company/aprime_role_contracts/",
                "target_symbol": "startup templates v1",
                "check_items": [
                    "executor/reviewer/adjudicator template hashes match role contracts",
                    "Adjudicator contract includes ACCEPT → lease → apply → close → COMPLETE",
                    "no child port_type is released before its gate is applied",
                ],
            },
        ],
        "role_contracts": role_contracts(),
    }
    response = client.call("task.create", params)
    print(json.dumps({"result": "created", "response": response}, ensure_ascii=False))


if __name__ == "__main__":
    main()
