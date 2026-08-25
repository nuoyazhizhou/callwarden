"""A′ 单 MCP 工具任务工厂：一次仅创建一张受 Gate 约束的任务卡。"""
from __future__ import annotations
import argparse
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
CLI01_ID = "T-1787321020926-b7ed7500"
WORKSPACE_ID = 1
WORKSPACE_INSTANCE_ID = "ws-1"
MANIFEST = ROOT / "deliverables" / "software-company" / "aprime_task_factory_manifest.json"
ROLE_DIR = ROOT / "deliverables" / "software-company" / "aprime_role_contracts"


def sha(filename: str) -> str:
    return hashlib.sha256((ROLE_DIR / filename).read_bytes()).hexdigest().upper()


def contracts(card: dict) -> list[dict]:
    python_file = card["python_source"].split("::")[0]
    rust_file = f"rust_ext/src/daemon/{card['rust_target'].split('::')[0]}"
    common = {
        "skill_id": "none", "skill_version": "",
        "allowed_paths": json.dumps([
            python_file, "server/compat_registry.py", "server/compat_worker.py", "server/daemon_client.py",
            rust_file, "rust_ext/src/daemon/dispatch.rs", "rust_ext/src/daemon/http_server.rs",
            "rust_ext/src/daemon/task_loop/mod.rs", f"tests/test_mcp_{card['tool']}_http_rpc.py",
            "deliverables/software-company/tool_migration_matrix.json", "deliverables/mcp-tools-implementation-map.md",
        ]),
        "forbidden_paths": json.dumps([
            "db/schema.py", "direct SQLite or get_db fallback", "task_collab.rs governance mutations",
            "lease/assignment/verdict/gate semantics", "other MCP tool handlers", "task.apply", "task.close",
        ]),
        "commands": json.dumps([
            "targeted Rust handler tests", "Python 3.14 HTTP RPC fixture", "daemon restart parity fixture",
            "daemon unavailable fail-closed fixture", "migration matrix generator refresh",
        ]),
        "acceptance_checks": json.dumps([
            "public MCP name/parameters/result shape stable", "Rust handler owns business logic",
            "Python wrapper only routes HTTP RPC", "single compat registration retired", "negative paths fail-closed",
            "matrix updates only after tests and independent review evidence",
        ]),
        "required_evidence": json.dumps([
            "Rust test output", "HTTP fixture output", "compat golden parity", "restart output",
            "daemon-unavailable output", "capability registry row", "matrix generator output",
        ]),
    }
    return [
        {**common, "role": "executor", "prompt_template_id": "cw.aprime.executor.startup.v1", "prompt_hash": sha("executor_planner_startup_v1.md"), "handoff_to": "reviewer", "independence": "required"},
        {**common, "role": "reviewer", "prompt_template_id": "cw.aprime.reviewer.startup.v1", "prompt_hash": sha("reviewer_startup_v1.md"), "handoff_to": "adjudicator", "independence": "required"},
        {**common, "role": "adjudicator", "prompt_template_id": "cw.aprime.adjudicator.startup.v1", "prompt_hash": sha("adjudicator_startup_v1.md"), "handoff_to": "complete", "independence": "required"},
    ]


def task_description(card: dict) -> str:
    gate = "true" if card["gate"] else "false"
    return f"""# {card['card_key']}：{card['tool']} 单 MCP→Rust daemon 原生链路迁移

**父任务：** `{PARENT_ID}`  
**port_type：** `{card['port_type']}`  
**port_key：** `{card['port_key']}`  
**gate：** `{gate}`  
**successor_rule：** {card['successor_rule']}

## 唯一交付链路

```text
MCP `{card['tool']}`
  → Python thin wrapper
  → HTTP JSON-RPC `{card['rpc_method']}`
  → dispatch.rs route
  → Rust `{card['rust_target']}`
  → authoritative read-only service/connection
```

## 精确范围

| 类别 | 唯一目标 |
|---|---|
| Python public entry | `{card['python_source']}`；保留 MCP 名称、参数、默认值、返回 JSON shape 和 stable error code，仅改为 `route_rpc()`/HTTP thin shell。 |
| Python compat retirement | 删除本工具 `_h_{card['tool']}` 与 `*_READ_ONLY_METHODS`/`register_compat_routes` 中的**单项**；不得删同模块任何其他 handler。 |
| Rust business handler | `{card['rust_target']}`；如本卡是端口首卡才创建该领域 module 与 `mod.rs` declaration；SQL/workspace/filter/limit 必须在 Rust authority 中。 |
| Dispatch/capability | 仅新增本工具 RPC branch 与 `http_server.rs` capability row；从 `python_compat` 变为 `rust_native`。 |
| Fixture | `tests/test_mcp_{card['tool']}_http_rpc.py` 或同族专属 fixture：success、非法参数、unknown/unauthorized workspace、daemon unavailable、daemon restart；确定性 Python compat golden parity。 |
| Matrix | 只有在 fixture、Reviewer 证据与 capability row 齐全后，生成器才可把 `{card['tool']}` 更新为 `target_backend=rust_native`、`status=migrated`、`batch={card['card_key']}`。 |

## 禁止范围

禁止修改 `db/schema.py`、治理 mutation、lease/assignment/verdict/gate 语义、其他 MCP 工具 handler，禁止客户端 SQL、direct `get_db()` fallback、隐藏 compat worker fallback 或同模块批量迁移。

## Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立复现 `{card['tool']}` 的 HTTP success、参数/authority/daemon-unavailable/restart 负向矩阵；核验 Python 已是 thin client、Rust handler 是唯一业务实现，compat registry 只移除了此单项。
  reason: {card['card_key']} 是 `{card['port_type']}` 的单链路迁移任务，必须以可复现 evidence 证明 Python 不再承载业务逻辑。
  independence_requirement: required
```
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("card_key", help="例如 MCP-001")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    card = next((row for row in manifest["cards"] if row["card_key"] == args.card_key), None)
    if card is None:
        raise SystemExit(f"未知 card_key: {args.card_key}")
    client = get_daemon_client()
    tree = client.call("task.status_tree", {"task_id": PARENT_ID, "workspace_id": WORKSPACE_ID, "workspace_instance_id": WORKSPACE_INSTANCE_ID})
    sibling_rows = tree.get("subtasks", [])
    cli01 = next((row for row in sibling_rows if isinstance(row, dict) and row.get("task_id") == CLI01_ID), None)
    cli01_status = cli01.get("status") if cli01 else "missing"
    if cli01_status not in ("applied", "closed"):
        raise SystemExit(f"拒绝越序建卡：CLI-01 必须 applied/closed，当前={cli01_status}")
    title = f"{card['card_key']} [{'Gate/' + card['port_type'] if card['gate'] else card['port_type']}]：{card['tool']} → Rust daemon native"
    if any(row.get("title") == title for row in sibling_rows if isinstance(row, dict)):
        print(json.dumps({"result": "exists", "title": title}, ensure_ascii=False)); return
    if not card["gate"]:
        gate_rows = [row for row in manifest["cards"] if row["port_type"] == card["port_type"] and row["gate"]]
        port_gate = min(gate_rows, key=lambda row: row["card_key"])
        gate_title = f"{port_gate['card_key']} [Gate/{port_gate['port_type']}]：{port_gate['tool']} → Rust daemon native"
        existing_gate = next((row for row in sibling_rows if isinstance(row, dict) and row.get("title") == gate_title), None)
        if not existing_gate or existing_gate.get("status") not in ("applied", "closed"):
            raise SystemExit(f"拒绝越序建卡：{port_gate['card_key']} 必须 applied/closed")
    result = client.call("task.create", {
        "title": title, "description": task_description(card), "creator": "executor-planner",
        "parent_id": PARENT_ID, "workspace_id": WORKSPACE_ID, "workspace_instance_id": WORKSPACE_INSTANCE_ID,
        "steps": [
            {"action": "port_rust_handler", "target_file": f"rust_ext/src/daemon/{card['rust_target'].split('::')[0]}; rust_ext/src/daemon/dispatch.rs; rust_ext/src/daemon/http_server.rs", "target_symbol": card["rust_target"], "check_items": ["native authority", "workspace isolation", "bounded read"]},
            {"action": "thin_python_client", "target_file": card["python_source"].split("::")[0], "target_symbol": card["tool"], "check_items": ["HTTP route_rpc", "single compat entry retirement", "stable response shape"]},
            {"action": "fixture_matrix", "target_file": f"tests/test_mcp_{card['tool']}_http_rpc.py", "target_symbol": card["tool"], "check_items": ["success", "invalid params", "workspace denial", "unavailable", "restart", "golden parity"]},
            {"action": "matrix_verify", "target_file": "deliverables/software-company/tool_migration_matrix.json", "target_symbol": card["tool"], "check_items": ["registry row", "generator", "evidence manifest"]},
        ],
        "role_contracts": contracts(card),
    })
    print(json.dumps({"result": "created", "card": card["card_key"], "response": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
