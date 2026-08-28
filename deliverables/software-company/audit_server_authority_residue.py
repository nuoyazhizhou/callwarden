"""对 server Python 做最终 DB authority 静态审计；发现残留时 fail-closed。"""
from __future__ import annotations

import ast
import collections
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
OUT = ROOT / "deliverables" / "software-company" / "server_authority_residue_audit.json"

SQLITE_MODULES = {"sqlite3", "_sqlite3"}
DB_METHOD_LEAVES = {
    "get_db",
    "direct_read",
    "open_readonly_conn",
    "get_compat_registry",
}
SQL_EXECUTE_LEAVES = {"execute", "executemany", "executescript"}


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        root = dotted(node.value)
        return f"{root}.{node.attr}" if root else node.attr
    return ""


def _finding(path: Path, handler: str, line: int, kind: str, detail: str) -> dict[str, Any]:
    return {
        "file": str(path.relative_to(ROOT)).replace("\\", "/"),
        "handler": handler,
        "line": line,
        "kind": kind,
        "detail": detail,
    }


def audit_python_authority(root: Path = SERVER) -> dict[str, Any]:
    """返回 server 范围的 AST authority 审计结果，只读源码且 fail-closed。"""
    findings: list[dict[str, Any]] = []
    files = sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)

    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            findings.append({"file": str(path.relative_to(ROOT)).replace("\\", "/"), "error": str(exc), "kind": "parse_error"})
            continue

        sqlite_aliases: set[str] = set(SQLITE_MODULES)
        sqlite_connect_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    if item.name in SQLITE_MODULES:
                        sqlite_aliases.add(item.asname or item.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module in SQLITE_MODULES:
                for item in node.names:
                    if item.name == "connect":
                        sqlite_connect_aliases.add(item.asname or item.name)

        stack: list[str] = []
        rel = str(path.relative_to(ROOT)).replace("\\", "/")

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Import(self, node: ast.Import) -> None:
                for item in node.names:
                    if item.name in SQLITE_MODULES:
                        findings.append(_finding(path, stack[-1] if stack else "<module>", node.lineno, "sqlite_import", item.name))
                self.generic_visit(node)

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                if node.module in SQLITE_MODULES:
                    findings.append(_finding(path, stack[-1] if stack else "<module>", node.lineno, "sqlite_import", node.module))
                self.generic_visit(node)

            def visit_Call(self, node: ast.Call) -> None:
                call = dotted(node.func)
                leaf = call.rsplit(".", 1)[-1]
                root_name = call.split(".", 1)[0]
                if call in {f"{alias}.connect" for alias in sqlite_aliases} or call in sqlite_connect_aliases:
                    findings.append(_finding(path, stack[-1] if stack else "<module>", node.lineno, "sqlite_connect", call))
                elif call.startswith("db.") or leaf in DB_METHOD_LEAVES:
                    findings.append(_finding(path, stack[-1] if stack else "<module>", node.lineno, "db_helper", call))
                elif leaf in SQL_EXECUTE_LEAVES and (root_name in {"conn", "connection", "db", "db_conn", "cursor"} or "._conn" in call or "._connection" in call):
                    findings.append(_finding(path, stack[-1] if stack else "<module>", node.lineno, "sql_execute", call))
                self.generic_visit(node)

        Visitor().visit(tree)

    by_file = collections.Counter(row["file"] for row in findings if "file" in row)
    return {
        "scope": "server-python",
        "scanned_files": len(files),
        "finding_count": len(findings),
        "files_with_findings": len(by_file),
        "passed": not findings,
        "by_file": dict(sorted(by_file.items())),
        "findings": findings,
    }


def write_report(report: dict[str, Any], output: Path = OUT) -> None:
    """落地审计快照；不修改源码或数据库。"""
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    report = audit_python_authority()
    write_report(report)
    print(json.dumps({key: report[key] for key in ("scope", "scanned_files", "finding_count", "files_with_findings", "passed")}, ensure_ascii=False))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
