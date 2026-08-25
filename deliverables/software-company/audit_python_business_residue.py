"""静态审计 Python MCP/CLI 业务残留；不改源码、任务或矩阵。"""
from __future__ import annotations
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = [ROOT / "server" / "tools", ROOT / "cli", ROOT / "cw.py"]
OUT = ROOT / "deliverables" / "software-company" / "python_business_residue_audit.json"
NAMES = {"get_db", "CodeGraphDB", "direct_read", "UnixDaemonRpcClient", "route_worker_call", "route_rpc", "get_compat_registry", "open_readonly_conn", "compute_resolved_edges", "import_compile_commands"}


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def inspect(path: Path) -> list[dict]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError) as exc:
        return [{"file": str(path.relative_to(ROOT)).replace("\\", "/"), "error": str(exc)}]
    rows = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = dotted_name(node.func) or ""
            leaf = name.rsplit(".", 1)[-1]
            if leaf in NAMES or name.startswith("db."):
                rows.append({"file": str(path.relative_to(ROOT)).replace("\\", "/"), "line": node.lineno, "call": name})
    return rows


def main() -> None:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    rows = [item for path in files for item in inspect(path)]
    by_call: dict[str, int] = {}
    for row in rows:
        key = row.get("call", "error")
        by_call[key] = by_call.get(key, 0) + 1
    report = {"scanned_files": len(files), "residue_count": len(rows), "by_call": by_call, "findings": rows}
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"scanned_files": len(files), "residue_count": len(rows), "by_call": by_call}, ensure_ascii=False))


if __name__ == "__main__":
    main()
