"""从 CLI 残留审计生成一命令一任务的 A′ 补卡清单；不修改任务库。"""
from __future__ import annotations
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "deliverables" / "software-company" / "cli_residue_by_handler_audit.json"
MATRIX = ROOT / "deliverables" / "software-company" / "tool_migration_matrix.json"
MCP_MANIFEST = ROOT / "deliverables" / "software-company" / "aprime_task_factory_manifest.json"
OUT = ROOT / "deliverables" / "software-company" / "aprime_cli_residue_manifest.json"
PARENT_ID = "T-1787293451688-c14b1e44"


def command_name(handler: str) -> str:
    if handler == "run_daemon_command":
        return "cw daemon（metrics/ping/workspace/publish/query）"
    if handler.startswith("_agent_"):
        return "cw-agent " + handler[len("_agent_"):].replace("_", "-")
    if handler.startswith("_handle_"):
        return "cw " + handler[len("_handle_"):].replace("_", "-")
    if handler.startswith("_dependency_"):
        return "cw dependency " + handler[len("_dependency_"):].replace("_", "-")
    if handler.startswith("_route_"):
        return "cw internal " + handler[len("_route_"):].replace("_", "-")
    return "cw " + handler.strip("_").replace("_", "-")


def port_type(handler: str, calls: list[str]) -> str:
    joined = " ".join(calls)
    if "lease" in handler or "assignment" in handler or "lease" in joined:
        return "assignment_projection"
    if "dependency" in handler or "call_" in handler or "build_context" in handler:
        return "graph_snapshot"
    if "task" in handler or "audit" in handler or "rollback" in handler:
        return "task_projection"
    if "agent" in handler or handler == "run_daemon_command" or "daemon" in handler:
        return "control_plane"
    if "lsp" in handler:
        return "lsp_session"
    return "cli_command_projection"


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


def target_for(call: str, handler: str, by_tool: dict, mcp_by_tool: dict) -> tuple[str, str | None]:
    leaf = call.rsplit(".", 1)[-1]
    if leaf in mcp_by_tool:
        card = mcp_by_tool[leaf]
        return (f"rust_ext/src/daemon/{card['rust_target']} (由 {card['card_key']} 迁移后供本 CLI 调用)", card["card_key"])
    if leaf in by_tool:
        tool = by_tool[leaf]
        return (f"rust_ext/src/daemon/dispatch.rs::dispatch_rpc [method={tool['rpc_method']}]", None)
    if call == "UnixDaemonRpcClient":
        return ("rust_ext/src/daemon/dispatch.rs::dispatch_rpc（HTTP transport 下的 ping/metrics/workspace/query/publish 已有或新增 method branch）", None)
    if leaf in {"open_readonly_conn", "compute_resolved_edges", "import_compile_commands"}:
        return ("rust_ext/src/daemon/build_context_handlers.rs::handle_import_compile_commands / handle_resolve", None)
    if leaf.startswith("register_rollback") or "rollback" in leaf:
        return ("rust_ext/src/daemon/rollback_handlers.rs::handle_register_rollback_config / handle_rollback_query / handle_set_rollback_flag", None)
    return (f"rust_ext/src/daemon/cli_{slug(handler)}_handlers.rs::handle_{slug(leaf)}", None)


def main() -> None:
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))["tools"]
    mcp_cards = json.loads(MCP_MANIFEST.read_text(encoding="utf-8"))["cards"]
    by_tool = {tool["name"]: tool for tool in matrix}
    mcp_by_tool = {card["tool"]: card for card in mcp_cards}
    cards = []
    for index, entry in enumerate(audit["handlers"], start=4):
        calls = sorted({item["call"] for item in entry["calls"]})
        targets, dependencies = [], []
        for call in calls:
            target, dependency = target_for(call, entry["handler"], by_tool, mcp_by_tool)
            if target not in targets:
                targets.append(target)
            if dependency and dependency not in dependencies:
                dependencies.append(dependency)
        cards.append({
            "card_key": f"CLI-{index:03d}", "command": command_name(entry["handler"]),
            "python_file": entry["file"], "python_handler": entry["handler"],
            "direct_calls": calls, "lines": sorted(item["line"] for item in entry["calls"]),
            "port_type": port_type(entry["handler"], calls), "rust_targets": targets,
            "mcp_dependencies": dependencies, "gate": False,
            "execution_dependency": "CLI-01 applied；本卡预建不等于可领取。若 mcp_dependencies 非空，所有列出的 MCP 卡也必须 applied；若所属端口另有已 applied Gate，按该 Gate 领取。",
        })
    OUT.write_text(json.dumps({
        "format": "cw.aprime.cli-residue.v2", "parent_id": PARENT_ID,
        "source": str(AUDIT.relative_to(ROOT)).replace("\\", "/"),
        "card_count": len(cards), "cards": cards,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"card_count": len(cards), "output": str(OUT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
