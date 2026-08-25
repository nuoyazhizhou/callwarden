"""从权威矩阵提取 A′ 每工具迁移候选；不修改矩阵或任务库。"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "deliverables" / "software-company" / "tool_migration_matrix.json"
OUTPUT = ROOT / "deliverables" / "software-company" / "aprime_python_compat_candidates.json"


def main() -> None:
    data = json.loads(MATRIX.read_text(encoding="utf-8"))
    selected = [
        {key: tool.get(key) for key in ("name", "module", "rpc_method", "op_class", "batch", "status", "target_backend")}
        for tool in data.get("tools", [])
        if tool.get("target_backend") == "python_compat" and tool.get("status") == "transition"
    ]
    selected.sort(key=lambda item: (item["module"] or "", item["name"] or ""))
    OUTPUT.write_text(json.dumps({
        "source": str(MATRIX.relative_to(ROOT)).replace("\\", "/"),
        "source_generated_at": data.get("generated_at"),
        "candidate_count": len(selected),
        "candidates": selected,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"candidate_count": len(selected), "output": str(OUTPUT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
