"""通过当前 HTTP authority 创建 P0-G；不执行 bootstrap、lease、claim、report、apply 或 close。"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from callwarden.server.daemon_client import HttpDaemonRpcClient

PARENT_ID = "T-1787293451688-c14b1e44"
WORKSPACE_ID = 1
WORKSPACE_INSTANCE_ID = "ws-1"
ENDPOINT = "http://127.0.0.1:14012"
TITLE = "P0-G：A′ 批量任务合同 revision-2、lease 恢复与原子治理建卡修复"
CONTRACT = ROOT / "deliverables" / "software-company" / "p0g_batch_task_contract_repair_contract.md"
ROLE_DIR = ROOT / "deliverables" / "software-company" / "aprime_role_contracts"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def role_contracts() -> list[dict]:
    allowed = [
        "rust_ext/src/daemon/task_loop/task_contract_revise.rs",
        "rust_ext/src/daemon/task_loop/mod.rs",
        "rust_ext/src/daemon/task_collab.rs",
        "rust_ext/src/daemon/dispatch.rs",
        "server/daemon_client.py",
        "cli/main.py",
        "cli/task_commands.py",
        "rust_ext/src/daemon/task_loop/*test*.rs",
        "rust_ext/src/daemon/*test*.rs",
        "tests/",
        "deliverables/software-company/",
        "runtime/current",
    ]
    common = {
        "skill_id": "none",
        "skill_version": "",
        "allowed_paths": json.dumps(allowed, ensure_ascii=False),
        "forbidden_paths": json.dumps([
            "direct SQLite governance writes",
            "DELETE/UPDATE task_contract_revisions or lineage history",
            "bulk automatic revision-2 repair",
            "A′ MCP/CLI/SRV business migration",
            "task.apply",
            "task.close",
        ], ensure_ascii=False),
        "commands": json.dumps([
            "targeted Rust task-loop tests",
            "Python 3.14 HTTP thin-client fixtures",
            "daemon unavailable and stale-manifest probes",
            "read-only contract/lease audit",
        ], ensure_ascii=False),
        "acceptance_checks": json.dumps([
            "append-only revision continuity",
            "required full agent/instance/session/model identity",
            "reviewer/adjudicator triple separation",
            "lease fencing/release and multi-active rejection",
            "task.create full projection atomicity",
            "no local SQLite fallback",
        ], ensure_ascii=False),
        "required_evidence": json.dumps([
            "Rust test output",
            "HTTP round-trip output",
            "negative matrix",
            "migration audit",
            "runtime fingerprint",
        ], ensure_ascii=False),
    }
    return [
        {**common, "role": "executor", "prompt_template_id": "cw.aprime.executor.startup.v1", "prompt_hash": sha256(ROLE_DIR / "executor_planner_startup_v1.md"), "handoff_to": "reviewer", "independence": "required"},
        {**common, "role": "reviewer", "prompt_template_id": "cw.aprime.reviewer.startup.v1", "prompt_hash": sha256(ROLE_DIR / "reviewer_startup_v1.md"), "handoff_to": "adjudicator", "independence": "required"},
        {**common, "role": "adjudicator", "prompt_template_id": "cw.aprime.adjudicator.startup.v1", "prompt_hash": sha256(ROLE_DIR / "adjudicator_startup_v1.md"), "handoff_to": "complete", "independence": "required"},
    ]


def main() -> None:
    client = HttpDaemonRpcClient(endpoint=ENDPOINT, verify_health=False, validate_manifest=False, timeout=15.0)
    existing = client.call("task.status_tree", {"task_id": PARENT_ID, "workspace_id": WORKSPACE_ID, "workspace_instance_id": WORKSPACE_INSTANCE_ID})
    for task in existing.get("subtasks", []):
        if isinstance(task, dict) and task.get("title") == TITLE:
            print(json.dumps({"result": "exists", "task": task}, ensure_ascii=False))
            return
    result = client.call("task.create", {
        "request_id": "p0g-batch-contract-repair-create-v1",
        "title": TITLE,
        "description": CONTRACT.read_text(encoding="utf-8"),
        "creator": "executor-planner",
        "parent_id": PARENT_ID,
        "workspace_id": WORKSPACE_ID,
        "workspace_instance_id": WORKSPACE_INSTANCE_ID,
        "steps": [
            {"action": "audit_and_design", "target_file": "deliverables/software-company/batch_task_card_and_contract_backfill_defect_audit.md; rust_ext/src/daemon/task_collab.rs; rust_ext/src/daemon/task_loop/contract_set.rs", "target_symbol": "revision-2/lease/create atomicity repair design", "check_items": ["append-only invariants", "identity/lease gaps", "no direct SQLite"]},
            {"action": "implement", "target_file": "rust_ext/src/daemon/task_loop/task_contract_revise.rs; rust_ext/src/daemon/task_collab.rs; rust_ext/src/daemon/dispatch.rs; server/daemon_client.py", "target_symbol": "task.contract_revise + lease repair + atomic create projection", "check_items": ["canonical structured envelope", "revision continuity", "full identity", "single active lease", "transactional create"]},
            {"action": "test", "target_file": "rust_ext/src/daemon/task_loop/*test*.rs; rust_ext/src/daemon/*test*.rs; tests/", "target_symbol": "P0-G positive/negative/matrix fixture suite", "check_items": ["revision rejection matrix", "triple separation", "lease release", "create rollback", "HTTP fail-closed"]},
            {"action": "release_verify", "target_file": "runtime/current; deliverables/software-company/", "target_symbol": "P0-G release and authority proof", "check_items": ["daemon fingerprint", "targeted tests", "read-only A′ audit", "no automatic revision-2 writes"]},
        ],
        "role_contracts": role_contracts(),
    })
    print(json.dumps({"result": "created", "response": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
