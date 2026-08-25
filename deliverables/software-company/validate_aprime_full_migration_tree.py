"""验证 A′ 完整 166 张迁移卡子树；只读 daemon tree/receipt/manifest 工件。"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TREE = ROOT / "deliverables" / "software-company" / "aprime_children.json"
MCP = ROOT / "deliverables" / "software-company" / "aprime_task_factory_manifest.json"
CLI = ROOT / "deliverables" / "software-company" / "aprime_cli_residue_manifest.json"
CLI_RECEIPT = ROOT / "deliverables" / "software-company" / "aprime_cli_residue_precreation_receipt.json"
INT001_RECEIPT = ROOT / "deliverables" / "software-company" / "int001_task_creation.json"
SRV = ROOT / "deliverables" / "software-company" / "aprime_server_authority_manifest.json"
SRV_RECEIPT = ROOT / "deliverables" / "software-company" / "aprime_server_authority_precreation_receipt.json"
OUT = ROOT / "deliverables" / "software-company" / "aprime_full_migration_tree_validation.json"


def mcp_title(card: dict) -> str:
    tag = "Gate/" + card["port_type"] if card["gate"] else card["port_type"]
    return f"{card['card_key']} [{tag}]：{card['tool']} → Rust daemon native"


def cli_title(card: dict) -> str:
    return f"{card['card_key']} [{card['port_type']}]：{card['command']} → Rust daemon HTTP thin client"


def srv_title(card: dict) -> str:
    return f"{card['card_key']} [{card['port_type']}]：server {card['subsystem']} Python authority → Rust daemon"


def main() -> None:
    tree = json.loads(TREE.read_text(encoding="utf-8"))
    mcp_cards = json.loads(MCP.read_text(encoding="utf-8"))["cards"]
    cli_cards = json.loads(CLI.read_text(encoding="utf-8"))["cards"]
    cli_receipt = json.loads(CLI_RECEIPT.read_text(encoding="utf-8"))
    int001_receipt = json.loads(INT001_RECEIPT.read_text(encoding="utf-8"))
    srv_cards = json.loads(SRV.read_text(encoding="utf-8"))["cards"]
    srv_receipt = json.loads(SRV_RECEIPT.read_text(encoding="utf-8"))
    int001_title = int001_receipt["response"]["title"]
    expected = {
        "CLI-01 [Gate/control_plane]：cw daemon health / manifest / capability 诊断链路",
        "CLI-02 [control_plane]：cw search daemon-only 读取链路",
        "CLI-03 [control_plane]：cw task show/list/status-tree 只读 authority 诊断",
        *(mcp_title(card) for card in mcp_cards),
        *(cli_title(card) for card in cli_cards),
        *(srv_title(card) for card in srv_cards),
        int001_title,
    }
    children = tree.get("subtasks", [])
    actual_titles = [child.get("title") for child in children]
    duplicate_titles = sorted({title for title in actual_titles if actual_titles.count(title) > 1})
    bad_steps = [{"task_id": child.get("task_id"), "title": child.get("title"), "step_count": len(child.get("steps", []))} for child in children if len(child.get("steps", [])) != 4]
    bad_status = [{"task_id": child.get("task_id"), "title": child.get("title"), "status": child.get("status")} for child in children if child.get("status") != "open"]
    report = {
        "parent_id": tree.get("task_id"),
        "expected_direct_children": len(expected),
        "actual_direct_children": len(children),
        "mcp_card_count": len(mcp_cards),
        "initial_cli_card_count": 3,
        "cli_residue_card_count": len(cli_cards),
        "missing_titles": sorted(expected - set(actual_titles)),
        "unexpected_titles": sorted(set(actual_titles) - expected),
        "duplicate_titles": duplicate_titles,
        "bad_steps": bad_steps,
        "bad_status": bad_status,
        "cli_residue_created": cli_receipt.get("created"),
        "cli_residue_failures": cli_receipt.get("failed"),
        "internal_compat_task_id": int001_receipt["response"]["task_id"],
        "internal_compat_contract_count": int001_receipt["response"]["contract_count"],
        "server_authority_card_count": len(srv_cards),
        "server_authority_created": srv_receipt.get("created"),
        "server_authority_failures": srv_receipt.get("failed"),
        "valid": len(children) == len(expected) and not (expected - set(actual_titles)) and not (set(actual_titles) - expected) and not duplicate_titles and not bad_steps and not bad_status and not cli_receipt.get("failed") and not srv_receipt.get("failed") and int001_receipt["response"].get("contract_count") == 3 and int001_receipt["response"].get("step_count") == 4,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("expected_direct_children", "actual_direct_children", "mcp_card_count", "cli_residue_card_count", "valid")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
