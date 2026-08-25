"""静态审计 server Python 中的 DB authority/业务残留；不修改源码。"""
from __future__ import annotations
import ast
import collections
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
OUT = ROOT / "deliverables" / "software-company" / "server_authority_residue_audit.json"
LEAVES = {"get_db", "direct_read", "open_readonly_conn", "get_compat_registry", "sqlite3.connect", "connect"}


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        root = dotted(node.value)
        return f"{root}.{node.attr}" if root else node.attr
    return ""


def main() -> None:
    findings = []
    files = [p for p in SERVER.rglob("*.py") if "__pycache__" not in p.parts]
    for path in files:
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            findings.append({"file": rel, "error": str(exc)}); continue
        stack: list[str] = []
        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                stack.append(node.name); self.generic_visit(node); stack.pop()
            visit_AsyncFunctionDef = visit_FunctionDef
            def visit_Call(self, node: ast.Call) -> None:
                call = dotted(node.func)
                leaf = call.rsplit(".", 1)[-1]
                if call.startswith("db.") or leaf in LEAVES or call == "sqlite3.connect":
                    findings.append({"file": rel, "handler": stack[-1] if stack else "<module>", "line": node.lineno, "call": call})
                self.generic_visit(node)
        V().visit(tree)
    by_file = collections.Counter(row["file"] for row in findings if "file" in row)
    report = {"scanned_files": len(files), "finding_count": len(findings), "files_with_findings": len(by_file), "by_file": dict(sorted(by_file.items())), "findings": findings}
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"scanned_files": len(files), "finding_count": len(findings), "files_with_findings": len(by_file)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
