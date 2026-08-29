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

# ---------------------------------------------------------------------------
# Retired legacy 白名单（SRV-019 最终 Gate 的"零可执行 authority"判定）。
#
# 语义：以下文件是 legacy Python daemon 的实现（生产权威已由 Rust daemon
# 完全接管，见 `docs/evidence/srv019_rust_takeover_matrix_20260829.md`），
# 保留仅为存量测试的兼容基线；其 Python DB authority 不再构成"可执行生产
# authority"。白名单**只减不加**：任何新文件一律不得加入；若未来 Rust
# 接管被回滚，对应条目必须移除并恢复 fail-closed 审计。
#
# 每个条目：{相对路径: 退休依据（Rust 接管证据）}
RETIRED_LEGACY_FILES: dict[str, str] = {
    "server/daemon_server.py":
        "SRV-008 已接管 6 个 Python 权威符号（daemon_server_handlers.rs，"
        "dispatch.rs mcp.daemon_server.* 6 路由）；文件内 _is_rust_acl_rolled_back/"
        "_is_rust_health_rolled_back 已改经 RPC，本地 sqlite3 查询退役",
    "server/daemon_config.py":
        "Rust daemon（cw-daemon.exe）配置加载在 Rust 侧（daemon_config.rs 语义）；"
        "本文件 PRAGMA 辅助仅 legacy Python daemon 使用",
    "server/durable_staging.py":
        "SRV-009 权威归属声明（文件头）；Rust durable_staging_handlers.rs 已实现"
        "mcp.durable_staging.init/stats",
    "server/health_check.py":
        "health.rs（G14）已接管 4 个 direct authority 函数（health_check_handlers.rs）；"
        "文件头声明 compat/test-only 形态",
    "server/job_executor.py":
        "文件头声明：sqlite3.connect 仅供存量 phase7 测试与进程内 executor；"
        "Rust task.job_status/list_jobs/job_stats 已由 SnapshotDaemonState 实现",
    "server/replicator.py":
        "Rust mcp.replicator.* 路由已接管（dispatch.rs:2960-2966）；"
        "本文件 sqlite3 实现仅 legacy 复制器使用",
    "server/compat_registry.py":
        "compat 过渡期声明文件（http-daemon-mvp-compatibility-contract §3.3）；"
        "生产主链 = Rust dispatch_rpc，registry 仅 _h_* worker 用",
    "server/compat_worker.py":
        "compat 过渡期 worker（Rust compat_adapter 管理）；check_client_purity 对"
        "_h_* 区间豁免（设计 Q5，白名单只减不加）",
    "server/tools/tools_collab.py":
        "残留全在 compat _h_* 处理器区间（check_client_purity 实测 0 违例）；"
        "HTTP 模式走 Rust dispatch.handle_collab_rpc",
    "server/tools/tools_p2_graph.py":
        "残留全在 compat _h_* 处理器区间；HTTP 模式走 Rust query.* 路由",
    "server/tools/tools_p3_identity.py":
        "残留全在 compat _h_* 处理器区间；HTTP 模式走 Rust identity 路由",
    "server/tools/tools_p4_lease.py":
        "残留全在 compat _h_* 处理器区间；HTTP 模式走 Rust lease 路由",
    "server/tools/tools_security.py":
        "残留全在 compat _h_* 处理器区间；HTTP 模式走 Rust 安全路由",
    "server/tools/tools_task.py":
        "残留全在 compat _h_* 处理器区间；HTTP 模式走 Rust task.* 路由",
}


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
    """返回 server 范围的 AST authority 审计结果，只读源码且 fail-closed。

    Retired legacy 文件（`RETIRED_LEGACY_FILES`）跳过——其 authority 已由
    Rust daemon 接管（见矩阵文档），保留仅为存量测试基线；扫描结果计入
    `retired_files` 报告，但不阻塞 passed。
    """
    findings: list[dict[str, Any]] = []
    retired_report: dict[str, str] = {}
    files = sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)

    for path in files:
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel in RETIRED_LEGACY_FILES:
            retired_report[rel] = RETIRED_LEGACY_FILES[rel]
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            findings.append({"file": rel, "error": str(exc), "kind": "parse_error"})
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
        "retired_files_count": len(retired_report),
        "retired_files": retired_report,
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
    print(json.dumps({key: report[key] for key in ("scope", "scanned_files", "finding_count", "files_with_findings", "retired_files_count", "passed")}, ensure_ascii=False))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
