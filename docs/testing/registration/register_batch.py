"""Register the approved first batch through TokenSlim + cw.py CLI, never SQL."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PYTHON = "C:/Python314/python.exe"
TOKENSLIM = Path(os.environ["APPDATA"]) / "npm/node_modules/tokenslim/bin/tokenslim.js"
NODE = "C:/Program Files/nodejs/node.exe"
env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")


def cli(label, args):
    proc = subprocess.run([NODE, str(TOKENSLIM), "run", PYTHON, *args], cwd=ROOT,
                          env=env, capture_output=True, text=True, encoding="utf-8")
    (HERE / (label + ".stdout.txt")).write_text(proc.stdout, encoding="utf-8")
    (HERE / (label + ".stderr.txt")).write_text(proc.stderr, encoding="utf-8")
    (HERE / (label + ".rc.json")).write_text(json.dumps({"exit_code": proc.returncode}) + "\n")
    if proc.returncode:
        raise RuntimeError(f"{label}: CLI exit={proc.returncode}; inspect archived output")
    return proc.stdout


def json_output(raw):
    for match in re.finditer(r"[\[{]", raw):
        try:
            value, _ = json.JSONDecoder().raw_decode(raw[match.start():])
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    raise RuntimeError("CLI did not return a JSON object")


def main():
    health = json_output(cli("health", ["cw.py", "daemon", "health"]))
    caps = json_output(cli("capabilities", ["cw.py", "daemon", "capability"]))
    pair = json_output(cli("workspace-pair", ["-c", "import json; from callwarden.server.daemon_client import resolve_workspace_pair_from_daemon; print(json.dumps(resolve_workspace_pair_from_daemon('C:/git_work/callwarden')))"]))
    if pair != {"workspace_id": 1, "workspace_instance_id": "4baea3ff12c2ea5c"}:
        raise RuntimeError("current daemon authority no longer matches prepared contract pair")
    # /capabilities lists native routes; existing task_rpc routes have their
    # own live /v1/meta/tools declaration. Do not infer one from the other.
    meta = json_output(cli("task-route-meta", ["-c", "import json, urllib.request; print(urllib.request.urlopen('" + health["endpoint"] + "/v1/meta/tools').read().decode())"]))
    method = next((t for t in meta.get("tools", []) if t.get("rpc_method") == "task.create"), {})
    if method.get("status") not in ("stable", "available") or method.get("target_backend") != "task_rpc":
        raise RuntimeError("task.create task_rpc route is not declared stable/available")
    rpc_health = json_output(cli("rpc-health", ["-c", "import json; from callwarden.server.daemon_client import HttpDaemonRpcClient; print(json.dumps(HttpDaemonRpcClient().call('health',{})))"]))
    compiler = rpc_health.get("role_prompt_compiler", {})
    (HERE / "preflight.json").write_text(json.dumps({"health": health, "task_create": method,
        "workspace_pair": pair, "planner_governance_declared": "planner_governance_v1" in json.dumps(caps),
        "rpc_health": rpc_health, "production_prompt_capability": compiler}, indent=2) + "\n", encoding="utf-8")
    receipt_path = HERE / "registered_tasks.json"
    receipts = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else []
    wanted = set(sys.argv[1:]) or {"TP-01", "TP-02", "TP-03", "TP-04"}
    for card in json.loads((HERE / "cards.json").read_text(encoding="utf-8")):
        key = card["document_key"]
        if key not in wanted or any(x["document_key"] == key for x in receipts):
            continue
        output = cli(key + "-create", [str(HERE / "invoke_cli.py"), key])
        match = re.search(r"\bT-\d+-[0-9a-f]+\b", output)
        if not match:
            raise RuntimeError(f"{key}: no daemon task_id in create output; do not retry blindly")
        task_id = match.group()
        receipts.append({"document_key": key, "task_id": task_id, "title": card["title"],
                         "workspace_id": pair["workspace_id"], "workspace_instance_id": pair["workspace_instance_id"],
                         "assignment_status": "UNASSIGNED", "create_cli_exit": 0})
        receipt_path.write_text(json.dumps(receipts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        projection = json_output(cli(key + "-next-action", ["cw.py", "task", "next-action", task_id,
            "--workspace-instance-id", pair["workspace_instance_id"], "--json"]))
        (HERE / (key + "-projection.json")).write_text(json.dumps(projection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if compiler.get("enabled") is True:
            prompt = json_output(cli(key + "-prompt", ["cw.py", "task", "prompt", task_id, "--format", "json",
                "--expected-workspace-instance-id", pair["workspace_instance_id"]]))
            (HERE / (key + "-prompt.json")).write_text(json.dumps(prompt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            (HERE / (key + "-prompt.json")).write_text(json.dumps({"status":"CAPABILITY_NOT_DECLARED",
                "fallback":"daemon task.next_action read-only with exact daemon workspace instance",
                "local_prompt_rendered":False}) + "\n", encoding="utf-8")
        print(key, task_id, "created; exact-authority next-action archived; no claim", flush=True)


if __name__ == "__main__":
    main()
