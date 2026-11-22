"""M3-M8 迁移状态静态重筛（§48）。

对 server/tools/*.py 的全部 @mcp.tool() 函数做与 ledger §4.1（2026-08-11 初筛）
同口径的信号启发式分类，并提取 rust_ext/src/daemon 的 RPC method 注册清单，
比对产出五态矩阵：production_ready / routed_missing_handler / mixed /
migrate_ready / pending_migration。

用法：python scripts/mcp_migration_rescan.py [--json <out>]
输出：stdout 汇总 + （可选）JSON 明细。启发式只能用于排队，不能替代真实
RPC round-trip 验证（与 ledger §4.1 的限制声明一致）。
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "server" / "tools"
DAEMON_DIR = ROOT / "rust_ext" / "src" / "daemon"

DAEMON_SIGNALS = ("route_rpc", "route_task_write", "route_task_read",
                  "HttpDaemonRpcClient", "daemon_client", "SharedTaskWriter",
                  "_route(", "_call_daemon_rpc(", "governed_write_file(")
LOCAL_SIGNALS = ("get_db(", "_sql_fallback", "sqlite3", "CodeGraphDB(",
                 "get_graph_db", "_local_db", "get_db()")
# 间接局部 DB 信号：调用形如 db.xxx / self._db 的共享 helper（启发式放宽）
LOCAL_INDIRECT_RE = re.compile(r"\b(?:db|graph_db|_db)\s*\.\w+\(")


def classify(body: str) -> str:
    has_route = any(s in body for s in DAEMON_SIGNALS)
    has_local = any(s in body for s in LOCAL_SIGNALS) or bool(LOCAL_INDIRECT_RE.search(body))
    if has_route and has_local:
        return "mixed"
    if has_route:
        return "routed"
    if has_local:
        return "local"
    return "unknown"


def tool_functions(path: Path):
    src = path.read_text(encoding="utf-8", errors="ignore")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            d = ast.unparse(dec)
            if d in ("mcp.tool", "tool") or d.startswith("mcp.tool("):
                seg = ast.get_source_segment(src, node) or ""
                # 剥 docstring：文档性 db.xxx 描述不应计入 local 信号
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    doc = ast.get_source_segment(src, node.body[0]) or ""
                    seg = seg.replace(doc, "", 1)
                yield node.name, seg


def rust_methods():
    """提取 dispatch match 分支注册的 method（含 "a" | "b" => 多别名行）。"""
    methods = set()
    for f in DAEMON_DIR.rglob("*.rs"):
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=>" not in line or '"' not in line:
                continue
            for m in re.findall(r'"([\w.]+)"', line):
                methods.add(m)
    return methods


def matrix_rpc_methods():
    data = json.load(open(ROOT / ".trae-cn" / "evidence" /
                          "mcp-tool-matrix-T-1786437453328.json", encoding="utf-8"))
    items = data if isinstance(data, list) else next(
        v for v in data.values() if isinstance(v, list))
    return {i["tool_name"]: i.get("daemon_rpc_method", "") for i in items}


def main() -> int:
    json_out = None
    if "--json" in sys.argv:
        json_out = sys.argv[sys.argv.index("--json") + 1]

    rust = rust_methods()
    declared = matrix_rpc_methods()

    rows = []
    for py in sorted(TOOLS_DIR.glob("tools_*.py")):
        for name, body in tool_functions(py):
            state = classify(body)
            # 从函数体提取实际 RPC method 字面量（_route('m' / route_rpc("m" 等）
            mets = re.findall(
                r"""(?:_route|route_rpc|route_task_write|route_task_read)\(\s*['"]([\w.]+)['"]""",
                body)
            rpc = mets[0] if mets else declared.get(name, "")
            handler = rpc in rust if rpc else False
            if state == "routed" and handler:
                bucket = "production_ready"
            elif state == "routed" and rpc:
                bucket = "routed_missing_handler"
            elif state == "routed":
                bucket = "routed_no_method_literal"
            elif state == "mixed":
                bucket = "mixed"
            elif state == "local" and handler:
                bucket = "migrate_ready"
            elif state == "local":
                bucket = "pending_migration"
            else:
                bucket = "unknown"
            rows.append({"module": py.stem, "tool": name, "signal": state,
                         "rpc_method": rpc, "rust_handler": handler,
                         "bucket": bucket})

    from collections import Counter
    by_mod = {}
    for r in rows:
        by_mod.setdefault(r["module"], Counter())[r["bucket"]] += 1

    total = Counter(r["bucket"] for r in rows)
    print(f"tools scanned: {len(rows)} (declared matrix: {len(declared)})")
    print(f"rust daemon methods found: {len(rust)}")
    print("buckets:", dict(total))
    for mod, c in by_mod.items():
        print(f"  {mod}: {dict(c)}")

    miss = [r for r in rows if r["bucket"] == "routed_missing_handler"]
    if miss:
        print("\nrouted_missing_handler (route 侧无 Rust handler，潜在静默 fallback):")
        for r in miss[:30]:
            print(f"  {r['module']}:{r['tool']} -> {r['rpc_method'] or '(未声明)'}")

    ready = [r for r in rows if r["bucket"] == "migrate_ready"]
    if ready:
        print(f"\nmigrate_ready (handler 已备只差切换, 前 30): {len(ready)}")
        for r in ready[:30]:
            print(f"  {r['module']}:{r['tool']} -> {r['rpc_method']}")

    if json_out:
        json.dump({"rows": rows, "rust_methods": sorted(rust),
                   "summary": dict(total)},
                  open(json_out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"\ndetail -> {json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
