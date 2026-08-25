"""经 daemon authority 预建 A′ SRV authority 迁移卡；不 bootstrap、claim 或执行。"""
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

PARENT = "T-1787293451688-c14b1e44"
WORKSPACE_ID = 1
WORKSPACE_INSTANCE_ID = "ws-1"
MANIFEST = ROOT / "deliverables" / "software-company" / "aprime_server_authority_manifest.json"
OUT = ROOT / "deliverables" / "software-company" / "aprime_server_authority_precreation_receipt.json"
ROLE = ROOT / "deliverables" / "software-company" / "aprime_role_contracts"


def digest(name: str) -> str:
    return hashlib.sha256((ROLE / name).read_bytes()).hexdigest().upper()


def title(card: dict) -> str:
    return f"{card['card_key']} [{card['port_type']}]：server {card['subsystem']} Python authority → Rust daemon"


def rust_files(card: dict) -> list[str]:
    files = ["rust_ext/src/daemon/dispatch.rs", "rust_ext/src/daemon/http_server.rs"]
    for target in card["rust_targets"]:
        files.extend(re.findall(r"rust_ext/src/daemon/[a-zA-Z0-9_]+\.rs", target))
    return list(dict.fromkeys(files))


def contracts(card: dict) -> list[dict]:
    common = {
        "skill_id": "none", "skill_version": "",
        "allowed_paths": json.dumps([card["python_file"], *rust_files(card), f"tests/test_{card['card_key'].lower().replace('-', '_')}.py", "deliverables/software-company/"]),
        "forbidden_paths": json.dumps(["db/schema.py except a separately approved schema Rust migration", "server/tools/ public MCP handlers", "other server modules", "direct SQLite fallback", "task_collab.rs governance mutations", "task.apply", "task.close"]),
        "commands": json.dumps(["targeted Rust tests", "Python 3.14 process/API fixture", "daemon unavailable/restart negative test", "static zero-authority source scan"]),
        "acceptance_checks": json.dumps(["Python module no longer opens SQLite or executes business query", "Rust target owns authority", "HTTP/client semantics retained", "negative matrix passes", "no local fallback"]),
        "required_evidence": json.dumps(["before/after AST scan", "Rust test output", "HTTP/process fixture output", "negative matrix", "runtime fingerprint", "capability/dispatch proof"]),
    }
    return [
        {**common, "role": "executor", "prompt_template_id": "cw.aprime.executor.startup.v1", "prompt_hash": digest("executor_planner_startup_v1.md"), "handoff_to": "reviewer", "independence": "required"},
        {**common, "role": "reviewer", "prompt_template_id": "cw.aprime.reviewer.startup.v1", "prompt_hash": digest("reviewer_startup_v1.md"), "handoff_to": "adjudicator", "independence": "required"},
        {**common, "role": "adjudicator", "prompt_template_id": "cw.aprime.adjudicator.startup.v1", "prompt_hash": digest("adjudicator_startup_v1.md"), "handoff_to": "complete", "independence": "required"},
    ]


def description(card: dict) -> str:
    handlers = "\n".join(f"- `{handler}`" for handler in card["handlers"])
    calls = "\n".join(f"- `{call}`" for call in card["calls"])
    targets = "\n".join(f"- `{target}`" for target in card["rust_targets"])
    lines = ", ".join(str(line) for line in card["lines"]) or "全仓最终审计"
    final_gate = card["subsystem"] == "Python authority zero-residue final gate"
    extra = "本卡为最终 Gate：仅在所有 MCP、CLI、INT 与 SRV 迁移卡已 applied 后执行；审计结果必须为零可执行 Python DB authority，才可宣布迁移完成。" if final_gate else "本卡预建不等于可领取；只在既定 Gate 和依赖已 applied 后由 Executor 处理。"
    return f"""# {card['card_key']}：server {card['subsystem']} Python authority → Rust daemon

**父任务：** `{PARENT}`  
**port_type：** `{card['port_type']}`  
**gate：** `{'true' if final_gate else 'false'}`  
**execution_dependency：** {card['execution_dependency']}

## Python authority 残留

**文件：** `{card['python_file']}`  
**静态命中行：** {lines}

| 类别 | 精确范围 |
|---|---|
| Python handlers | {handlers} |
| 当前 direct authority 调用 | {calls} |
| Rust target functions | {targets} |
| dispatch/capability | `rust_ext/src/daemon/dispatch.rs::dispatch_rpc` 与 `rust_ext/src/daemon/http_server.rs`；只新增本模块所需 method/capability。 |
| fixture | `tests/test_{card['card_key'].lower().replace('-', '_')}.py`：success、invalid input、authority denial、daemon unavailable、restart。 |
| completion evidence | before/after AST scan，必须证明此 Python 模块不再 open SQLite、调用 `get_db()`、执行业务 SQL 或保留本地 business fallback。 |

## 不变量

Python 最终只保留 HTTP client、JSON/request serialization、CLI/MCP response formatting、进程启动/配置读取等非业务适配职责。数据库连接、schema/authority decision、业务查询、写入、后台 job、审计、复制、GC、备份和恢复均须位于 Rust daemon 或其 Rust-managed worker 内。禁止将失败降级回 Python SQLite。

{extra}

## Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立复现 HTTP/daemon success 与负向矩阵，并以静态扫描和运行时探针核验 {card['python_file']} 已无 DB authority 或业务 fallback。
  reason: {card['card_key']} 将一个独立 server subsystem 的 Python authority 下沉至 Rust daemon；完成态要求 Python 仅为 thin client/adapter。
  independence_requirement: required
```
"""


def steps(card: dict) -> list[dict]:
    return [
        {"action": "port_rust_authority", "target_file": "; ".join(rust_files(card)), "target_symbol": "; ".join(card["rust_targets"]), "check_items": ["native authority", "stable errors", "workspace/identity control"]},
        {"action": "retire_python_authority", "target_file": card["python_file"], "target_symbol": "; ".join(card["handlers"]), "check_items": ["no SQLite", "no get_db", "no business fallback", "HTTP thin adaptation only"]},
        {"action": "fixture_negative_matrix", "target_file": f"tests/test_{card['card_key'].lower().replace('-', '_')}.py", "target_symbol": card["subsystem"], "check_items": ["success", "invalid", "authority", "unavailable", "restart"]},
        {"action": "zero_authority_evidence", "target_file": "deliverables/software-company/", "target_symbol": card["card_key"], "check_items": ["AST scan", "runtime fingerprint", "capability evidence", "handoff manifest"]},
    ]


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    client = get_daemon_client()
    tree = client.call("task.status_tree", {"task_id": PARENT, "workspace_id": WORKSPACE_ID, "workspace_instance_id": WORKSPACE_INSTANCE_ID})
    existing = {row.get("title") for row in tree.get("subtasks", []) if isinstance(row, dict)}
    receipts = []
    for card in manifest["cards"]:
        task_title = title(card)
        if task_title in existing:
            receipts.append({"key": card["card_key"], "result": "exists"}); continue
        try:
            result = client.call("task.create", {"title": task_title, "description": description(card), "creator": "executor-planner", "parent_id": PARENT, "workspace_id": WORKSPACE_ID, "workspace_instance_id": WORKSPACE_INSTANCE_ID, "steps": steps(card), "role_contracts": contracts(card)})
            receipts.append({"key": card["card_key"], "result": "created", "response": result})
        except Exception as exc:
            receipts.append({"key": card["card_key"], "result": "failed", "error": str(exc)})
            break
    summary = {"parent_id": PARENT, "requested": len(manifest["cards"]), "created": sum(row["result"] == "created" for row in receipts), "existing": sum(row["result"] == "exists" for row in receipts), "failed": [row for row in receipts if row["result"] == "failed"], "receipts": receipts}
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("parent_id", "requested", "created", "existing", "failed")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
