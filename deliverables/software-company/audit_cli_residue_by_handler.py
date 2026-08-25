"""聚合 CLI handler 中的 Python 本地业务/Unix transport 调用；只读静态审计。"""
from __future__ import annotations
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATHS = [ROOT / "cli", ROOT / "cw.py"]
OUT = ROOT / "deliverables" / "software-company" / "cli_residue_by_handler_audit.json"
LEAVES = {"UnixDaemonRpcClient", "open_readonly_conn", "compute_resolved_edges", "import_compile_commands", "get_db", "CodeGraphDB", "direct_read"}


def dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


class Visitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stack: list[str] = []
        self.findings: list[dict] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        call = dotted_name(node.func)
        leaf = call.rsplit(".", 1)[-1]
        if call.startswith("db.") or leaf in LEAVES:
            self.findings.append({
                "file": str(self.path.relative_to(ROOT)).replace("\\", "/"),
                "handler": self.stack[-1] if self.stack else "<module>",
                "line": node.lineno,
                "call": call,
            })
        self.generic_visit(node)


def main() -> None:
    rows: list[dict] = []
    for base in PATHS:
        files = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            visitor = Visitor(path)
            visitor.visit(tree)
            rows.extend(visitor.findings)
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((row["file"], row["handler"]), []).append(row)
    report = {
        "handler_count": len(groups),
        "call_count": len(rows),
        "handlers": [
            {"file": file, "handler": handler, "calls": sorted(items, key=lambda item: item["line"])}
            for (file, handler), items in sorted(groups.items())
        ],
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"handler_count": report["handler_count"], "call_count": report["call_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
