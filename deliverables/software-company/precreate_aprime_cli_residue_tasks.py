"""经 daemon authority 预建 A′ CLI-004+ 单命令迁移卡；不 bootstrap、claim 或执行。"""
from __future__ import annotations
import hashlib
import json
import re
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
MANIFEST = ROOT / "deliverables" / "software-company" / "aprime_cli_residue_manifest.json"
OUT = ROOT / "deliverables" / "software-company" / "aprime_cli_residue_precreation_receipt.json"
ROLE_DIR = ROOT / "deliverables" / "software-company" / "aprime_role_contracts"


def sha(name: str) -> str:
    return hashlib.sha256((ROLE_DIR / name).read_bytes()).hexdigest().upper()


def rust_files(card: dict) -> list[str]:
    rows = ["rust_ext/src/daemon/dispatch.rs", "rust_ext/src/daemon/http_server.rs"]
    for target in card["rust_targets"]:
        rows.extend(re.findall(r"rust_ext/src/daemon/[a-zA-Z0-9_]+\.rs", target))
    return list(dict.fromkeys(rows))


def contracts(card: dict) -> list[dict]:
    allowed = [card["python_file"], *rust_files(card), f"tests/test_{card['card_key'].lower().replace('-', '_')}_http_rpc.py", "deliverables/software-company/"]
    common = {
        "skill_id": "none", "skill_version": "",
        "allowed_paths": json.dumps(allowed),
        "forbidden_paths": json.dumps([
            "db/schema.py", "direct SQLite/get_db/open_readonly_conn after migration", "other CLI command handlers",
            "other MCP handlers except declared dependencies", "task_collab.rs governance mutations",
            "lease/assignment/verdict/gate semantics", "task.apply", "task.close",
        ]),
        "commands": json.dumps([
            "targeted Rust handler/dispatch tests", "Python 3.14 CLI process fixture", "HTTP round-trip",
            "daemon unavailable/restart negative fixture", "daemon capability inspection",
        ]),
        "acceptance_checks": json.dumps([
            "Python handler has no direct db/Unix RPC/local analysis business path", "Rust handler/dispatch owns business logic",
            "response and stable errors preserved", "success/invalid/authority/unavailable/restart fixture passes",
            "MCP dependency applied where declared",
        ]),
        "required_evidence": json.dumps([
            "before/after source scan", "Rust test output", "CLI process output", "HTTP output",
            "negative matrix", "runtime fingerprint", "capability/dispatch evidence",
        ]),
    }
    return [
        {**common, "role": "executor", "prompt_template_id": "cw.aprime.executor.startup.v1", "prompt_hash": sha("executor_planner_startup_v1.md"), "handoff_to": "reviewer", "independence": "required"},
        {**common, "role": "reviewer", "prompt_template_id": "cw.aprime.reviewer.startup.v1", "prompt_hash": sha("reviewer_startup_v1.md"), "handoff_to": "adjudicator", "independence": "required"},
        {**common, "role": "adjudicator", "prompt_template_id": "cw.aprime.adjudicator.startup.v1", "prompt_hash": sha("adjudicator_startup_v1.md"), "handoff_to": "complete", "independence": "required"},
    ]


def title(card: dict) -> str:
    return f"{card['card_key']} [{card['port_type']}]：{card['command']} → Rust daemon HTTP thin client"


def description(card: dict) -> str:
    targets = "\n".join(f"- `{target}`" for target in card["rust_targets"])
    dependencies = ", ".join(card["mcp_dependencies"]) or "无"
    calls = ", ".join(f"`{call}`" for call in card["direct_calls"])
    lines = ", ".join(str(line) for line in card["lines"])
    return f"""# {card['card_key']}：{card['command']} 单 CLI→Rust daemon HTTP 链路迁移

**父任务：** `{PARENT_ID}`  
**port_type：** `{card['port_type']}`  
**gate：** `false`  
**预建执行约束：** {card['execution_dependency']}

## 唯一范围

| 类别 | 精确目标 |
|---|---|
| CLI Python entry | `{card['python_file']}::{card['python_handler']}`，当前本地/legacy 调用行 `{lines}`。 |
| 必须移除的 Python 业务路径 | {calls}；迁移后该 command 只作 HTTP request 参数组装与输出格式化。 |
| Rust business/transport target | {targets} |
| dispatch/capability | `rust_ext/src/daemon/dispatch.rs::dispatch_rpc` 和 `rust_ext/src/daemon/http_server.rs` 中仅本 command 所需 method/capability route；保留 stable RPC 名称和 error code。 |
| MCP 依赖 | `{dependencies}`；若非空，先核验所有依赖卡为 applied，不得复制或绕过其业务 handler。 |
| fixture | `tests/test_{card['card_key'].lower().replace('-', '_')}_http_rpc.py`：success、invalid input、wrong/unknown authority、daemon unavailable、restart 后行为一致。 |
| matrix/evidence | 更新 CLI migration inventory/route generator 输入；仅在独立 review evidence 后写 migrated。不得篡改 MCP 工具矩阵中其他行。 |

## 验收与禁止

Executor 必须证明 `{card['python_handler']}` 不再执行 direct DB、`UnixDaemonRpcClient` 或本地分析器业务逻辑；Rust daemon 是唯一 authority。禁止改 `db/schema.py`、task/lease/verdict/gate 治理路径、其他 CLI handler、未列 MCP 依赖，禁止保留 hidden local fallback。

## Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立复现 {card['command']} 的 HTTP success 与参数/authority/daemon-unavailable/restart 负向矩阵；核验指定 Python handler 已无 local DB、Unix transport 或本地业务路径，且 MCP dependencies 未被绕过。
  reason: {card['card_key']} 只迁移一个 CLI command handler，使 Python 成为 HTTP thin client、Rust daemon 成为唯一业务 authority。
  independence_requirement: required
```
"""


def steps(card: dict) -> list[dict]:
    return [
        {"action": "port_or_verify_rust", "target_file": "; ".join(rust_files(card)), "target_symbol": "; ".join(card["rust_targets"]), "check_items": ["native authority", "stable RPC", "workspace/identity validation"]},
        {"action": "thin_cli_client", "target_file": card["python_file"], "target_symbol": card["python_handler"], "check_items": ["remove direct DB/Unix/local analyzer", "HTTP only", "output compatibility"]},
        {"action": "fixture_negative_matrix", "target_file": f"tests/test_{card['card_key'].lower().replace('-', '_')}_http_rpc.py", "target_symbol": card["command"], "check_items": ["success", "invalid", "authority", "unavailable", "restart"]},
        {"action": "evidence_and_dependency_verify", "target_file": "deliverables/software-company/", "target_symbol": card["card_key"], "check_items": ["source scan", "MCP dependency applied", "runtime fingerprint", "evidence manifest"]},
    ]


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    client = get_daemon_client()
    tree = client.call("task.status_tree", {"task_id": PARENT_ID, "workspace_id": WORKSPACE_ID, "workspace_instance_id": WORKSPACE_INSTANCE_ID})
    existing_titles = {row.get("title") for row in tree.get("subtasks", []) if isinstance(row, dict)}
    receipts = []
    for card in manifest["cards"]:
        task_title = title(card)
        if task_title in existing_titles:
            receipts.append({"key": card["card_key"], "result": "exists"}); continue
        try:
            result = client.call("task.create", {
                "title": task_title, "description": description(card), "creator": "executor-planner",
                "parent_id": PARENT_ID, "workspace_id": WORKSPACE_ID, "workspace_instance_id": WORKSPACE_INSTANCE_ID,
                "steps": steps(card), "role_contracts": contracts(card),
            })
            receipts.append({"key": card["card_key"], "result": "created", "response": result})
        except Exception as exc:
            receipts.append({"key": card["card_key"], "result": "failed", "error": str(exc)})
            break
    summary = {"parent_id": PARENT_ID, "requested": len(manifest["cards"]), "created": sum(item["result"] == "created" for item in receipts), "existing": sum(item["result"] == "exists" for item in receipts), "failed": [item for item in receipts if item["result"] == "failed"], "receipts": receipts}
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("parent_id", "requested", "created", "existing", "failed")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
