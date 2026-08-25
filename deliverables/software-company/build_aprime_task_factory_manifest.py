"""从 A′ 已批准草案生成逐 MCP 单链路任务工厂 manifest；不触碰任务库。"""
from __future__ import annotations
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRAFT = ROOT / "Callwarden：逐 MCP 工具 _ 逐 CLI 链路 Rust daemon 迁移任务清单（A′ 流水线修订草案）.md"
CANDIDATES = ROOT / "deliverables" / "software-company" / "aprime_python_compat_candidates.json"
OUT = ROOT / "deliverables" / "software-company" / "aprime_task_factory_manifest.json"
PARENT_ID = "T-1787293451688-c14b1e44"

PORTS = {
    range(1, 5): ("task_projection", "collab_query_handlers"),
    range(5, 10): ("graph_snapshot", "dependency_query_handlers"),
    range(10, 15): ("identity_attestation", "identity_query_handlers"),
    range(15, 16): ("assignment_projection", "assignment_query_handlers"),
    range(16, 24): ("graph_snapshot", "query_compat_handlers"),
    range(24, 29): ("semantic_projection", "semantic_query_handlers"),
    range(29, 48): ("summary_projection", "summary_query_handlers"),
    range(48, 63): ("repository_security", "security_or_lsp_handlers"),
    range(63, 71): ("task_projection", "task_read_handlers"),
}
GATES = {1, 5, 10, 15, 16, 24, 29, 48, 54, 63}


def port_for(number: int) -> tuple[str, str]:
    if 54 <= number <= 59:
        return ("lsp_session", "lsp_handlers")
    if 60 <= number <= 62:
        return ("repository_security", "security_query_handlers")
    for numbers, port in PORTS.items():
        if number in numbers:
            return port
    raise ValueError(number)


def clean(cell: str) -> str:
    return cell.strip().replace("`", "")


def main() -> None:
    text = DRAFT.read_text(encoding="utf-8")
    candidate_rows = json.loads(CANDIDATES.read_text(encoding="utf-8"))["candidates"]
    candidates_by_name = {item["name"]: item for item in candidate_rows}
    candidate_names = set(candidates_by_name)
    cards: list[dict] = []
    for line in text.splitlines():
        if not re.match(r"\|\s*MCP-\d{3}\s*\|", line):
            continue
        cells = [clean(cell) for cell in line.strip().strip("|").split("|")]
        number = int(re.search(r"MCP-(\d{3})", cells[0]).group(1))
        tool_cell = cells[1]
        tool = re.search(r"([a-z][a-z0-9_]+)", tool_cell).group(1)
        if tool not in candidate_names:
            raise ValueError(f"草案工具 {tool} 未在 python_compat candidates 中")
        python_source = cells[2]
        rust_target = cells[3]
        port_type, port_family = port_for(number)
        module = candidates_by_name[tool]["module"]
        if not python_source.startswith("server/"):
            detail = python_source.split("::", 1)[-1] if "::" in python_source else python_source
            python_source = f"server/tools/{module}.py::{detail}"
        if rust_target.startswith("::"):
            rust_target = f"{port_family}.rs{rust_target}"
        cards.append({
            "card_key": f"MCP-{number:03d}", "tool": tool, "rpc_method": tool_cell,
            "python_source": python_source, "python_module": module, "rust_target": rust_target,
            "port_type": port_type, "port_key": port_family,
            "gate": number in GATES,
            "successor_rule": (
                f"MCP-{number:03d} 为 {port_type} Gate；Adjudicator apply 前不得创建同 port_type 后继"
                if number in GATES else f"前置 {port_type} Gate 已 applied；单写队列中前一任务进入 review 后才可执行"
            ),
        })
    cards.sort(key=lambda row: row["card_key"])
    if len(cards) != 70 or len({row["tool"] for row in cards}) != 70:
        raise ValueError(f"期望 70 张唯一工具卡，实际 {len(cards)}")
    OUT.write_text(json.dumps({
        "format": "cw.aprime.task-factory.v1", "parent_id": PARENT_ID,
        "source_draft": str(DRAFT.relative_to(ROOT)).replace("\\", "/"),
        "candidate_source": str(CANDIDATES.relative_to(ROOT)).replace("\\", "/"),
        "card_count": len(cards), "cards": cards,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"card_count": len(cards), "output": str(OUT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
