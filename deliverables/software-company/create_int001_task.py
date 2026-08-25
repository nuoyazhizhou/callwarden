"""经 daemon authority 创建 INT-001 内部 stats_top_files 迁移任务。"""
from __future__ import annotations
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PARENT = ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))
from callwarden.server.daemon_client import get_daemon_client

PARENT = "T-1787293451688-c14b1e44"
TITLE = "INT-001 [graph_snapshot]：internal stats_top_files → Rust daemon native"
CONTRACT = ROOT / "deliverables" / "software-company" / "int001_stats_top_files_contract.md"
ROLE = ROOT / "deliverables" / "software-company" / "aprime_role_contracts"


def digest(name: str) -> str:
    return hashlib.sha256((ROLE / name).read_bytes()).hexdigest().upper()


def role_contracts() -> list[dict]:
    common = {
        "skill_id": "none", "skill_version": "",
        "allowed_paths": json.dumps([
            "server/compat_registry.py", "server/compat_worker.py", "server/daemon_client.py",
            "rust_ext/src/daemon/query_compat_handlers.rs", "rust_ext/src/daemon/dispatch.rs", "rust_ext/src/daemon/http_server.rs",
            "tests/test_internal_stats_top_files_http_rpc.py", "scripts/gen_route_matrix.py", "deliverables/software-company/",
        ]),
        "forbidden_paths": json.dumps(["db/schema.py", "other compat routes", "public MCP tool names", "task_collab.rs governance mutation", "lease/assignment/verdict/gate semantics", "task.apply", "task.close"]),
        "commands": json.dumps(["targeted Rust tests", "Python 3.14 HTTP fixture", "daemon unavailable/restart tests", "internal route inventory validation"]),
        "acceptance_checks": json.dumps(["Rust owns SQL", "Python registry thin adapter only", "limit clamp", "workspace isolation", "compat route retired", "negative matrix passes"]),
        "required_evidence": json.dumps(["Rust tests", "HTTP output", "negative matrix", "capability row", "internal route inventory diff", "runtime fingerprint"]),
    }
    return [
        {**common, "role": "executor", "prompt_template_id": "cw.aprime.executor.startup.v1", "prompt_hash": digest("executor_planner_startup_v1.md"), "handoff_to": "reviewer", "independence": "required"},
        {**common, "role": "reviewer", "prompt_template_id": "cw.aprime.reviewer.startup.v1", "prompt_hash": digest("reviewer_startup_v1.md"), "handoff_to": "adjudicator", "independence": "required"},
        {**common, "role": "adjudicator", "prompt_template_id": "cw.aprime.adjudicator.startup.v1", "prompt_hash": digest("adjudicator_startup_v1.md"), "handoff_to": "complete", "independence": "required"},
    ]


def main() -> None:
    client = get_daemon_client()
    tree = client.call("task.status_tree", {"task_id": PARENT, "workspace_id": 1, "workspace_instance_id": "ws-1"})
    if any(task.get("title") == TITLE for task in tree.get("subtasks", []) if isinstance(task, dict)):
        print(json.dumps({"result": "exists", "title": TITLE}, ensure_ascii=False)); return
    result = client.call("task.create", {
        "title": TITLE, "description": CONTRACT.read_text(encoding="utf-8"), "creator": "executor-planner",
        "parent_id": PARENT, "workspace_id": 1, "workspace_instance_id": "ws-1",
        "steps": [
            {"action": "port_rust_handler", "target_file": "rust_ext/src/daemon/query_compat_handlers.rs; rust_ext/src/daemon/dispatch.rs; rust_ext/src/daemon/http_server.rs", "target_symbol": "handle_stats_top_files / dispatch_rpc", "check_items": ["workspace filter", "limit clamp", "native authority"]},
            {"action": "retire_python_worker_entry", "target_file": "server/compat_registry.py", "target_symbol": "_stats_top_files / RUST_COMPAT_ROUTE.stats_top_files", "check_items": ["single route retirement", "no SQLite fallback", "retain registry framework"]},
            {"action": "fixture_negative_matrix", "target_file": "tests/test_internal_stats_top_files_http_rpc.py", "target_symbol": "stats_top_files", "check_items": ["success", "invalid limit", "workspace denial", "unavailable", "restart"]},
            {"action": "inventory_release_verify", "target_file": "scripts/gen_route_matrix.py; deliverables/software-company/", "target_symbol": "internal compat route inventory", "check_items": ["route removed", "capability native", "evidence manifest"]},
        ],
        "role_contracts": role_contracts(),
    })
    print(json.dumps({"result": "created", "response": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
