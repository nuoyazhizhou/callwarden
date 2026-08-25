"""经 daemon authority 预建 A′ 完整任务树；不 bootstrap、不 claim、不改任务状态。"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PARENT = ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))
from callwarden.server.daemon_client import get_daemon_client
from create_aprime_mcp_task import PARENT_ID, WORKSPACE_ID, WORKSPACE_INSTANCE_ID, contracts, task_description

MANIFEST = ROOT / "deliverables" / "software-company" / "aprime_task_factory_manifest.json"
OUT = ROOT / "deliverables" / "software-company" / "aprime_precreation_receipt.json"

CLI_SPECS = [
    {
        "key": "CLI-02", "title": "CLI-02 [control_plane]：cw search daemon-only 读取链路",
        "python": "cli/main.py::search 子命令与结果格式化（仅该 command）",
        "rust": "query_handlers.rs::search response compatibility 或现有 query.search RPC response",
        "gate": False,
        "body": "修复 `cw search <query>` daemon-only 读取链路的 `has_comment` 兼容字段与输出格式。不得把 get_db() 回填 CLI；success、空结果、无 has_comment、daemon unavailable 均须稳定。",
    },
    {
        "key": "CLI-03", "title": "CLI-03 [control_plane]：cw task show/list/status-tree 只读 authority 诊断",
        "python": "cli/main.py::task show/list/status-tree；server/daemon_client.py::route_task_read",
        "rust": "task_collab.rs readonly projection 或 dedicated readonly task query handler",
        "gate": False,
        "body": "迁移 task show/list/status-tree 的只读 authority 诊断；区分 target、父树、未绑定/错误 authority 与 stale manifest，且不得写 active workspace 或改 task state。",
    },
]


def cli_contract(spec: dict) -> str:
    return f"""# {spec['key']}：单 CLI→HTTP Rust daemon 链路迁移

**父任务：** `{PARENT_ID}`  
**port_type：** `control_plane`  
**gate：** `false`  
**execution_dependency：** `CLI-01` 必须由 Adjudicator `apply`；任务已预建仅用于完整任务树，不授权 Executor 领取、实现或 report。

## 唯一范围

- **Python public entry：** `{spec['python']}`。
- **Rust target：** `{spec['rust']}`。
- **dispatch/capability：** 只改本 command 所需 HTTP RPC route/capability response；Python 保持 thin client，不得 local SQLite fallback。
- **fixture：** 专属 CLI process + HTTP round-trip fixture，覆盖 success、非法/空输入、authority failure、daemon unavailable、restart。
- **矩阵：** 仅在独立 review 和 runtime evidence 后更新该 command route/matrix 输入；不把本卡当作 `cli/main.py` 旧 S1 全量清理。

## 验收与禁止

{spec['body']}

禁止修改 `db/schema.py`、任务/lease/verdict/gate mutation，禁止扩展到其他 CLI 命令、MCP 工具或 S1 的 296 处引用清理。

## Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立复现本 CLI command 的 HTTP success、authority/参数/daemon-unavailable/restart 负向矩阵，核验 Python 无业务逻辑与无 SQLite fallback。
  reason: {spec['key']} 是 A′ control_plane 的单命令链路卡，必须独立证明 Rust daemon 为 authority。
  independence_requirement: required
```
"""


def cli_steps(spec: dict) -> list[dict]:
    return [
        {"action": "port_rust_authority", "target_file": spec["rust"].split("::")[0], "target_symbol": spec["rust"], "check_items": ["daemon authority", "response compatibility", "stable errors"]},
        {"action": "thin_cli_client", "target_file": spec["python"].split("::")[0], "target_symbol": spec["python"], "check_items": ["HTTP client only", "no SQLite fallback", "format compatibility"]},
        {"action": "fixture_matrix", "target_file": f"tests/test_{spec['key'].lower().replace('-', '_')}_http_rpc.py", "target_symbol": spec["key"], "check_items": ["success", "invalid/empty", "authority", "unavailable", "restart"]},
        {"action": "matrix_verify", "target_file": "deliverables/software-company/tool_migration_matrix.json", "target_symbol": spec["key"], "check_items": ["route matrix", "runtime evidence", "handoff manifest"]},
    ]


def mcp_title(card: dict) -> str:
    tag = "Gate/" + card["port_type"] if card["gate"] else card["port_type"]
    return f"{card['card_key']} [{tag}]：{card['tool']} → Rust daemon native"


def mcp_steps(card: dict) -> list[dict]:
    return [
        {"action": "port_rust_handler", "target_file": f"rust_ext/src/daemon/{card['rust_target'].split('::')[0]}; rust_ext/src/daemon/dispatch.rs; rust_ext/src/daemon/http_server.rs", "target_symbol": card["rust_target"], "check_items": ["native authority", "workspace isolation", "bounded read"]},
        {"action": "thin_python_client", "target_file": card["python_source"].split("::")[0], "target_symbol": card["tool"], "check_items": ["HTTP route_rpc", "single compat entry retirement", "stable response shape"]},
        {"action": "fixture_matrix", "target_file": f"tests/test_mcp_{card['tool']}_http_rpc.py", "target_symbol": card["tool"], "check_items": ["success", "invalid params", "workspace denial", "unavailable", "restart", "golden parity"]},
        {"action": "matrix_verify", "target_file": "deliverables/software-company/tool_migration_matrix.json", "target_symbol": card["tool"], "check_items": ["registry row", "generator", "evidence manifest"]},
    ]


def precreated_description(card: dict) -> str:
    return task_description(card).replace(
        "## 唯一交付链路",
        "## 预建但不可执行的 Gate 约束\n\n本卡已入库以形成完整 A′ 工作分解结构；在 `CLI-01` 及本卡 `port_type` 的首卡被 Adjudicator `apply` 前，不得对本卡 bootstrap governance projection、claim、report 或 handoff。预建不是派发。\n\n## 唯一交付链路",
        1,
    )


def main() -> None:
    client = get_daemon_client()
    tree = client.call("task.status_tree", {"task_id": PARENT_ID, "workspace_id": WORKSPACE_ID, "workspace_instance_id": WORKSPACE_INSTANCE_ID})
    children = tree.get("subtasks", [])
    titles = {row.get("title") for row in children if isinstance(row, dict)}
    receipts = []
    for spec in CLI_SPECS:
        if spec["title"] in titles:
            receipts.append({"key": spec["key"], "result": "exists"}); continue
        result = client.call("task.create", {
            "title": spec["title"], "description": cli_contract(spec), "creator": "executor-planner",
            "parent_id": PARENT_ID, "workspace_id": WORKSPACE_ID, "workspace_instance_id": WORKSPACE_INSTANCE_ID,
            "steps": cli_steps(spec), "role_contracts": contracts({"python_source": spec["python"], "rust_target": spec["rust"], "tool": spec["key"].lower().replace("-", "_")}),
        })
        receipts.append({"key": spec["key"], "result": "created", "response": result})
    cards = json.loads(MANIFEST.read_text(encoding="utf-8"))["cards"]
    for card in cards:
        title = mcp_title(card)
        if title in titles:
            receipts.append({"key": card["card_key"], "result": "exists"}); continue
        result = client.call("task.create", {
            "title": title, "description": precreated_description(card), "creator": "executor-planner",
            "parent_id": PARENT_ID, "workspace_id": WORKSPACE_ID, "workspace_instance_id": WORKSPACE_INSTANCE_ID,
            "steps": mcp_steps(card), "role_contracts": contracts(card),
        })
        receipts.append({"key": card["card_key"], "result": "created", "response": result})
    summary = {"parent_id": PARENT_ID, "requested": len(CLI_SPECS) + len(cards), "created": sum(item["result"] == "created" for item in receipts), "existing": sum(item["result"] == "exists" for item in receipts), "receipts": receipts}
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("parent_id", "requested", "created", "existing")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
