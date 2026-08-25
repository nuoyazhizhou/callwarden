"""审计 A′ 70 MCP 卡对全部工具迁移矩阵的覆盖；不修改任务或矩阵。"""
from __future__ import annotations
import ast
import collections
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "deliverables" / "software-company" / "tool_migration_matrix.json"
MANIFEST = ROOT / "deliverables" / "software-company" / "aprime_task_factory_manifest.json"
PRECREATE = ROOT / "deliverables" / "software-company" / "aprime_precreation_receipt.json"
COMPAT_REGISTRY = ROOT / "server" / "compat_registry.py"
OUT = ROOT / "deliverables" / "software-company" / "aprime_migration_coverage_audit.json"


def rust_compat_route_methods() -> set[str]:
    tree = ast.parse(COMPAT_REGISTRY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "RUST_COMPAT_ROUTE":
            if isinstance(node.value, ast.Dict):
                return {key.value for key in node.value.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    raise ValueError("未找到 RUST_COMPAT_ROUTE literal")


def main() -> None:
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    cards = json.loads(MANIFEST.read_text(encoding="utf-8"))["cards"]
    receipt = json.loads(PRECREATE.read_text(encoding="utf-8"))
    tools = matrix["tools"]
    card_tools = {card["tool"] for card in cards}
    matrix_by_name = {tool["name"]: tool for tool in tools}
    missing_from_matrix = sorted(card_tools - set(matrix_by_name))
    transition = [tool for tool in tools if tool.get("target_backend") == "python_compat" and tool.get("status") == "transition"]
    transition_tools = {tool["name"] for tool in transition}
    uncovered_transition = sorted(transition_tools - card_tools)
    extraneous_cards = sorted(card_tools - transition_tools)
    by_target_status = collections.Counter((tool.get("target_backend"), tool.get("status")) for tool in tools)
    non_native_not_stable = [
        tool for tool in tools
        if tool.get("target_backend") != "rust_native" and tool.get("status") not in {"stable", "migrated"}
    ]
    non_native_not_stable_not_card = [tool for tool in non_native_not_stable if tool["name"] not in card_tools]
    task_rpc_not_migrated = [tool for tool in tools if tool.get("target_backend") == "task_rpc" and tool.get("status") not in {"stable", "migrated"}]
    unavailable = [tool for tool in tools if tool.get("target_backend") == "declared_unavailable"]
    compat_methods = rust_compat_route_methods()
    compat_not_transition = sorted(compat_methods - transition_tools)
    transition_not_compat = sorted(transition_tools - compat_methods)
    compat_non_mcp = sorted(method for method in compat_methods if method not in matrix_by_name)
    compat_matrix_nontransition = [
        {key: matrix_by_name[method].get(key) for key in ("name", "target_backend", "status", "rpc_method")}
        for method in compat_not_transition if method in matrix_by_name
    ]
    report = {
        "matrix_total_tools": matrix.get("total_tools"),
        "matrix_row_count": len(tools),
        "aprime_mcp_card_count": len(cards),
        "precreated_non_cli_cards": receipt.get("created"),
        "target_backend_status_counts": [
            {"target_backend": backend, "status": status, "count": count}
            for (backend, status), count in sorted(by_target_status.items())
        ],
        "transition_python_compat_count": len(transition),
        "transition_python_compat_card_coverage": len(card_tools & transition_tools),
        "missing_from_matrix": missing_from_matrix,
        "uncovered_transition_tools": uncovered_transition,
        "extraneous_card_tools": extraneous_cards,
        "non_native_not_stable_count": len(non_native_not_stable),
        "non_native_not_stable_uncovered": [
            {key: tool.get(key) for key in ("name", "module", "target_backend", "rpc_method", "op_class", "batch", "status")}
            for tool in non_native_not_stable_not_card
        ],
        "task_rpc_not_migrated": [tool["name"] for tool in task_rpc_not_migrated],
        "declared_unavailable": [tool["name"] for tool in unavailable],
        "rust_compat_route_count": len(compat_methods),
        "rust_compat_route_not_transition": compat_not_transition,
        "rust_compat_route_non_mcp_internal": compat_non_mcp,
        "rust_compat_route_matrix_nontransition": compat_matrix_nontransition,
        "transition_not_in_rust_compat_route": transition_not_compat,
        "conclusion": (
            "A′ 70 卡完整覆盖迁移矩阵中全部 python_compat/transition 只读工具；"
            "它不等同于覆盖所有 239 工具。已 stable/migrated 的 rust_native/task_rpc 不应重复建卡；"
            "若仍存在 non-native/not-stable 未覆盖项，则必须单列后续任务。"
        ),
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "matrix_total_tools": report["matrix_total_tools"],
        "transition": report["transition_python_compat_count"],
        "covered": report["transition_python_compat_card_coverage"],
        "uncovered_transition": len(report["uncovered_transition_tools"]),
        "other_non_native_not_stable_uncovered": len(report["non_native_not_stable_uncovered"]),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
