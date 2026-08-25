"""经 daemon authority 创建 P0-F bootstrap evidence/review bridge 治理任务。"""
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
TITLE = "P0-F：Bootstrap evidence / review bridge（A′ 冷启动治理死锁修复）"
CONTRACT_PATH = PROJECT_ROOT / "deliverables" / "software-company" / "p0f_bootstrap_evidence_review_bridge_contract.md"
TEMPLATE_DIR = PROJECT_ROOT / "deliverables" / "software-company" / "aprime_role_contracts"


def digest(name: str) -> str:
    return hashlib.sha256((TEMPLATE_DIR / name).read_bytes()).hexdigest().upper()


def role_contracts() -> list[dict]:
    return [
        {"role":"executor","skill_id":"none","skill_version":"","prompt_template_id":"cw.aprime.executor.startup.v1","prompt_hash":digest("executor_planner_startup_v1.md"),"allowed_paths":json.dumps(["rust_ext/src/daemon/task_loop/bootstrap_review_bridge.rs","rust_ext/src/daemon/task_loop/mod.rs","rust_ext/src/daemon/task_collab.rs","rust_ext/src/daemon/dispatch.rs","rust_ext/src/daemon/task_loop/operation_store.rs","server/daemon_client.py","cli/main.py","rust_ext/src/daemon/*test*.rs","tests/"]),"forbidden_paths":json.dumps(["db/schema.py","migrations/","direct SQLite","task status/verdict/binding/capture direct writes","task.apply","task.close"]),"commands":"daemon-native Rust tests; Python client/CLI tests; release daemon round-trip","acceptance_checks":"only zero-projection bound tasks; identity/evidence/authority/ledger fail-closed; no normal-task shortcut","required_evidence":"test output, runtime probe, evidence manifest/hash, denial matrix","handoff_to":"reviewer","independence":"required"},
        {"role":"reviewer","skill_id":"none","skill_version":"","prompt_template_id":"cw.aprime.reviewer.startup.v1","prompt_hash":digest("reviewer_startup_v1.md"),"allowed_paths":"read-only P0-F source, tests, evidence and runtime projections","forbidden_paths":"production edits; task.apply; task.close; direct database writes","commands":"read-only review","acceptance_checks":"verify bridge only permits empty-projection root bootstrap and preserves normal lifecycle gates","required_evidence":"independent PASS or BLOCKED record","handoff_to":"adjudicator","independence":"required"},
        {"role":"adjudicator","skill_id":"none","skill_version":"","prompt_template_id":"cw.aprime.adjudicator.startup.v1","prompt_hash":digest("adjudicator_startup_v1.md"),"allowed_paths":"P0-F lifecycle finalization only","forbidden_paths":"production edits; direct database writes; P0-C bootstrap of other tasks before P0-F finalization","commands":"task.apply; task.close; task.next_action after reviewer PASS and valid lease/fencing","acceptance_checks":"independent reviewer PASS; apply then close then COMPLETE","required_evidence":"finalization manifest","handoff_to":"complete","independence":"required"},
    ]


def main() -> None:
    client = get_daemon_client()
    listing = client.call("task.list", {"parent_id":PARENT_ID,"status":"","limit":200,"workspace_id":WORKSPACE_ID,"workspace_instance_id":WORKSPACE_INSTANCE_ID})
    tasks = listing.get("tasks", listing) if isinstance(listing, dict) else listing
    for task in tasks if isinstance(tasks, list) else []:
        if isinstance(task, dict) and task.get("title") == TITLE:
            print(json.dumps({"result":"exists","task":task}, ensure_ascii=False)); return
    result = client.call("task.create", {
        "title": TITLE, "description": CONTRACT_PATH.read_text(encoding="utf-8"), "creator":"executor-planner",
        "parent_id":PARENT_ID,"workspace_id":WORKSPACE_ID,"workspace_instance_id":WORKSPACE_INSTANCE_ID,
        "steps":[
            {"action":"implement","target_file":"rust_ext/src/daemon/task_loop/bootstrap_review_bridge.rs","target_symbol":"bootstrap_executor_evidence/bootstrap_reviewer_pass","check_items":["zero-projection gate","append-only evidence","identity separation","durable idempotency"]},
            {"action":"wire","target_file":"rust_ext/src/daemon/task_collab.rs; rust_ext/src/daemon/dispatch.rs; rust_ext/src/daemon/task_loop/operation_store.rs","target_symbol":"protected bridge RPC routes","check_items":["authority","ledger","no normal lifecycle bypass"]},
            {"action":"adapt_client_cli","target_file":"server/daemon_client.py; cli/main.py","target_symbol":"bootstrap bridge wrappers","check_items":["daemon-only","explicit evidence/identity","no local fallback"]},
            {"action":"test","target_file":"rust_ext/src/daemon/*test*.rs; tests/","target_symbol":"P0-F bridge denial matrix","check_items":["positive root bootstrap","existing projection reject","identity conflict","missing evidence","replay mismatch"]},
            {"action":"release_verify","target_file":"runtime/current","target_symbol":"P0-F runtime probe","check_items":["build deploy","fail-closed probe","no real task mutation during smoke"]},
        ],"role_contracts":role_contracts(),
    })
    print(json.dumps({"result":"created","response":result}, ensure_ascii=False))

if __name__ == "__main__":
    main()
