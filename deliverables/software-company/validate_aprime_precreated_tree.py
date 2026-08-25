"""验证 A′ 预建 3 CLI + 70 MCP 单链路任务树；只读本地 daemon tree/receipt 文件。"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TREE = ROOT / "deliverables" / "software-company" / "aprime_children.json"
RECEIPT = ROOT / "deliverables" / "software-company" / "aprime_precreation_receipt.json"
MANIFEST = ROOT / "deliverables" / "software-company" / "aprime_task_factory_manifest.json"
OUT = ROOT / "deliverables" / "software-company" / "aprime_precreation_validation.json"


def mcp_title(card: dict) -> str:
    tag = "Gate/" + card["port_type"] if card["gate"] else card["port_type"]
    return f"{card['card_key']} [{tag}]：{card['tool']} → Rust daemon native"


def main() -> None:
    tree = json.loads(TREE.read_text(encoding="utf-8"))
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    children = tree.get("subtasks", [])
    expected_titles = {
        "CLI-01 [Gate/control_plane]：cw daemon health / manifest / capability 诊断链路",
        "CLI-02 [control_plane]：cw search daemon-only 读取链路",
        "CLI-03 [control_plane]：cw task show/list/status-tree 只读 authority 诊断",
        *(mcp_title(card) for card in manifest["cards"]),
    }
    actual_titles = [child.get("title") for child in children]
    missing = sorted(expected_titles - set(actual_titles))
    unexpected = sorted(set(actual_titles) - expected_titles)
    duplicates = sorted({title for title in actual_titles if actual_titles.count(title) > 1})
    bad_steps = [
        {"task_id": child.get("task_id"), "title": child.get("title"), "step_count": len(child.get("steps", []))}
        for child in children if len(child.get("steps", [])) != 4
    ]
    non_open = [
        {"task_id": child.get("task_id"), "title": child.get("title"), "status": child.get("status")}
        for child in children if child.get("status") != "open"
    ]
    created = [item for item in receipt["receipts"] if item["result"] == "created"]
    bad_receipts = [item for item in created if item.get("response", {}).get("contract_count") != 3 or item.get("response", {}).get("step_count") != 4]
    report = {
        "parent_id": tree.get("task_id"),
        "direct_child_count": len(children),
        "expected_child_count": len(expected_titles),
        "missing_titles": missing,
        "unexpected_titles": unexpected,
        "duplicate_titles": duplicates,
        "bad_step_count": bad_steps,
        "non_open_children": non_open,
        "precreation_created_count": receipt.get("created"),
        "bad_receipts": bad_receipts,
        "valid": len(children) == len(expected_titles) and not missing and not unexpected and not duplicates and not bad_steps and not non_open and not bad_receipts,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
