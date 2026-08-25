"""经 daemon authority 创建 A′ CLI-01 单命令 control-plane Gate。"""
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

PARENT_ID = "T-1787293451688-c14b1e44"
WORKSPACE_ID = 1
WORKSPACE_INSTANCE_ID = "ws-1"
TITLE = "CLI-01 [Gate/control_plane]：cw daemon health / manifest / capability 诊断链路"
CONTRACT = ROOT / "deliverables" / "software-company" / "cli01_daemon_health_manifest_capability_contract.md"
ROLE_DIR = ROOT / "deliverables" / "software-company" / "aprime_role_contracts"


def role_hash(filename: str) -> str:
    return hashlib.sha256((ROLE_DIR / filename).read_bytes()).hexdigest().upper()


def role_contracts() -> list[dict]:
    common = {
        "skill_id": "none", "skill_version": "",
        "allowed_paths": json.dumps([
            "cw.py", "cli/daemon_commands.py", "server/daemon_autostart.py", "server/daemon_client.py",
            "rust_ext/src/daemon/http_server.rs", "rust_ext/src/daemon/health.rs",
            "tests/", "deliverables/software-company/", "runtime/current",
        ]),
        "forbidden_paths": json.dumps([
            "cli/main.py full S1 reference cleanup", "db/schema.py", "direct SQLite fallback",
            "rust_ext/src/daemon/task_collab.rs governance mutations", "lease/assignment/verdict/gate semantics",
            "task.apply", "task.close",
        ]),
        "commands": json.dumps([
            "targeted Rust health/capability tests", "Python 3.14 CLI process fixture",
            "HTTP round-trip and daemon unavailable probe", "runtime/current fingerprint verification",
        ]),
        "acceptance_checks": json.dumps([
            "health/manifest/capability success", "missing manifest fail-closed", "stale PID fail-closed",
            "wrong authority fail-closed", "daemon unavailable fail-closed", "real get_stats MCP round-trip",
        ]),
        "required_evidence": json.dumps([
            "test output", "CLI output", "HTTP output", "denial matrix", "runtime fingerprint", "capability registry row",
        ]),
    }
    return [
        {**common, "role": "executor", "prompt_template_id": "cw.aprime.executor.startup.v1",
         "prompt_hash": role_hash("executor_planner_startup_v1.md"), "handoff_to": "reviewer", "independence": "required"},
        {**common, "role": "reviewer", "prompt_template_id": "cw.aprime.reviewer.startup.v1",
         "prompt_hash": role_hash("reviewer_startup_v1.md"), "handoff_to": "adjudicator", "independence": "required"},
        {**common, "role": "adjudicator", "prompt_template_id": "cw.aprime.adjudicator.startup.v1",
         "prompt_hash": role_hash("adjudicator_startup_v1.md"), "handoff_to": "complete", "independence": "required"},
    ]


def main() -> None:
    client = get_daemon_client()
    existing = client.call("task.list", {
        "parent_id": PARENT_ID, "status": "", "limit": 200,
        "workspace_id": WORKSPACE_ID, "workspace_instance_id": WORKSPACE_INSTANCE_ID,
    })
    tasks = existing.get("tasks", []) if isinstance(existing, dict) else []
    for task in tasks:
        if isinstance(task, dict) and task.get("title") == TITLE:
            print(json.dumps({"result": "exists", "task": task}, ensure_ascii=False)); return
    result = client.call("task.create", {
        "title": TITLE,
        "description": CONTRACT.read_text(encoding="utf-8"),
        "creator": "executor-planner",
        "parent_id": PARENT_ID,
        "workspace_id": WORKSPACE_ID,
        "workspace_instance_id": WORKSPACE_INSTANCE_ID,
        "steps": [
            {"action": "inspect_contract", "target_file": "cw.py; cli/daemon_commands.py; server/daemon_autostart.py; rust_ext/src/daemon/http_server.rs", "target_symbol": "daemon health/manifest/capability command chain", "check_items": ["current route map", "manifest authority", "no cli/main.py S1 expansion"]},
            {"action": "implement", "target_file": "cw.py; cli/daemon_commands.py; server/daemon_autostart.py; server/daemon_client.py; rust_ext/src/daemon/http_server.rs", "target_symbol": "health/manifest/capability thin client and daemon authority", "check_items": ["Python HTTP shell only", "Rust health/capability authority", "stable errors"]},
            {"action": "test", "target_file": "tests/; rust_ext/src/daemon/", "target_symbol": "CLI-01 health/capability fixture family", "check_items": ["success", "missing manifest", "stale PID", "wrong authority", "daemon unavailable", "get_stats round-trip"]},
            {"action": "release_verify", "target_file": "runtime/current", "target_symbol": "CLI-01 runtime authority proof", "check_items": ["Python 3.14", "release build", "runtime/PID hash", "capability registry evidence"]},
        ],
        "role_contracts": role_contracts(),
    })
    print(json.dumps({"result": "created", "response": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
