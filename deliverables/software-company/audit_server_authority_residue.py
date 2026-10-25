"""对 server Python 做最终 DB authority 静态审计；发现残留时 fail-closed。"""
from __future__ import annotations

import argparse
import ast
import collections
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
OUT = ROOT / "deliverables" / "software-company" / "server_authority_residue_audit.json"
TRANSITION_BASELINE = (
    ROOT / "deliverables" / "software-company" / "db_retire_transition_baseline.json"
)

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
    "server/tools/tools_dependency_graph.py":
        "残留全在 compat _h_* 处理器区间；HTTP 模式走 Rust query.* 路由",
    "server/tools/tools_identity.py":
        "残留全在 compat _h_* 处理器区间；HTTP 模式走 Rust identity 路由",
    "server/tools/tools_lease.py":
        "残留全在 compat _h_* 处理器区间；HTTP 模式走 Rust lease 路由",
    "server/tools/tools_security.py":
        "残留全在 compat _h_* 处理器区间；HTTP 模式走 Rust 安全路由",
    "server/tools/tools_task.py":
        "残留全在 compat _h_* 处理器区间；HTTP 模式走 Rust task.* 路由",
}


TRANSITION_KINDS = {"sqlite_import", "sqlite_connect", "db_helper", "sql_execute"}


def _transition_key(row: dict[str, Any]) -> tuple:
    """第二类条目的比对粒度：file:line:handler:kind（detail 不参与身份）。"""
    return (row.get("file"), row.get("line"), row.get("handler"), row.get("kind"))


def load_transition_declaration(path: Path = TRANSITION_BASELINE) -> dict[str, Any]:
    """读取并校验第二类 db-retire 过渡声明（冻结基线）。

    声明语义（与 known_red 的反豁免纪律同族）：
    - 只覆盖已由 Rust 1:1 接管生产权威的 Python 影子存储层；
    - 条目粒度 file:line:handler:kind，逐条比对、**只减不加**；
    - 日落条款绑 db-retire 里程碑：过期即 fail-closed；
    - 条目文件必须在 `files` 中附接管证据（非空长文本），禁止无理由豁免。
    任一条不符 ⇒ ValueError（调用方按 fail-closed 处理，不静默放行）。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("过渡声明顶层必须为对象")
    if data.get("schema_version") != 1:
        raise ValueError("过渡声明 schema_version 必须为 1")

    sunset = data.get("sunset")
    if not isinstance(sunset, dict):
        raise ValueError("过渡声明缺 sunset 对象")
    if not isinstance(sunset.get("milestone"), str) or not sunset["milestone"].strip():
        raise ValueError("过渡声明 sunset.milestone 必须为非空字符串")
    expiry_raw = sunset.get("expiry_date")
    if not isinstance(expiry_raw, str):
        raise ValueError("过渡声明 sunset.expiry_date 必须为 YYYY-MM-DD 字符串")
    try:
        date.fromisoformat(expiry_raw)
    except ValueError as e:
        raise ValueError(f"过渡声明 sunset.expiry_date 非法：{expiry_raw!r}（{e}）")

    files = data.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("过渡声明 files 必须为非空对象（文件 → 接管证据）")
    for rel, why in files.items():
        if not isinstance(rel, str) or not isinstance(why, str) or len(why.strip()) < 20:
            raise ValueError(f"过渡声明 files[{rel!r}] 缺接管证据（value 必须为 ≥20 字字符串）")

    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ValueError("过渡声明 entries 必须为数组")
    normalized: list[dict[str, Any]] = []
    seen: set = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"过渡声明 entries[{i}] 必须为对象")
        missing = [f for f in ("file", "line", "handler", "kind") if f not in entry]
        if missing:
            raise ValueError(f"过渡声明 entries[{i}] 缺字段：{', '.join(missing)}")
        if entry["file"] not in files:
            raise ValueError(f"过渡声明 entries[{i}].file 未在 files 中声明接管证据")
        if not isinstance(entry["line"], int):
            raise ValueError(f"过渡声明 entries[{i}].line 必须为整数")
        if not isinstance(entry["handler"], str) or not entry["handler"]:
            raise ValueError(f"过渡声明 entries[{i}].handler 必须为非空字符串")
        if entry["kind"] not in TRANSITION_KINDS:
            raise ValueError(
                f"过渡声明 entries[{i}].kind 非法：{entry['kind']!r}"
                f"（允许 {'/'.join(sorted(TRANSITION_KINDS))}）"
            )
        key = _transition_key(entry)
        if key in seen:
            raise ValueError(f"过渡声明条目重复：{key}")
        seen.add(key)
        normalized.append(
            {
                "file": entry["file"],
                "line": entry["line"],
                "handler": entry["handler"],
                "kind": entry["kind"],
                "detail": entry.get("detail", ""),
            }
        )

    return {
        "sunset": sunset,
        "evidence": data.get("evidence", {}),
        "files": files,
        "entries": normalized,
    }


def prune_transition_baseline(
    report: dict[str, Any], path: Path = TRANSITION_BASELINE
) -> int:
    """把声明收缩到「当前仍存在」的条目（只减不加），返回删除条数。

    仅在无未声明残留时允许收缩；新增残留必须走根因整改，不能靠改基线放行。
    """
    if report["finding_count"] or report["transition_declaration_error"]:
        raise ValueError("存在未声明残留或声明非法，拒绝收缩基线")
    data = json.loads(path.read_text(encoding="utf-8"))
    actual = {_transition_key(f) for f in report["transition_findings"]}
    kept = [e for e in data["entries"] if _transition_key(e) in actual]
    removed = len(data["entries"]) - len(kept)
    if removed:
        data["entries"] = kept
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return removed


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


def audit_python_authority(
    root: Path = SERVER,
    transition_path: Path | None = TRANSITION_BASELINE,
) -> dict[str, Any]:
    """返回 server 范围的 AST authority 审计结果，只读源码且 fail-closed。

    Retired legacy 文件（`RETIRED_LEGACY_FILES`）跳过——其 authority 已由
    Rust daemon 接管（见矩阵文档），保留仅为存量测试基线；扫描结果计入
    `retired_files` 报告，但不阻塞 passed。

    第二类 db-retire 过渡声明（`db_retire_transition_baseline.json`）中的
    条目同样不阻塞 passed，但**单列**为 `transition_findings`；未声明残留
    （新文件/新行/新 kind）与基线未收缩（声明条目已不存在）均 fail-closed，
    日落条款过期同样 fail-closed。
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

    declaration = None
    declaration_error = ""
    if transition_path is not None:
        try:
            declaration = load_transition_declaration(transition_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            declaration_error = str(exc)

    transition_findings: list[dict[str, Any]] = []
    undeclared: list[dict[str, Any]] = findings
    stale_entries: list[dict[str, Any]] = []
    if declaration is not None:
        declared_keys = {_transition_key(e) for e in declaration["entries"]}
        actual_keys = {_transition_key(f) for f in findings}
        transition_findings = [
            f for f in findings if _transition_key(f) in declared_keys
        ]
        undeclared = [f for f in findings if _transition_key(f) not in declared_keys]
        stale_entries = [
            e for e in declaration["entries"] if _transition_key(e) not in actual_keys
        ]

    by_file = collections.Counter(row["file"] for row in undeclared if "file" in row)
    transition_by_file = collections.Counter(
        row["file"] for row in transition_findings if "file" in row
    )
    sunset_expired = bool(
        declaration
        and date.today() > date.fromisoformat(declaration["sunset"]["expiry_date"])
    )

    try:
        transition_ref = (
            str(transition_path.relative_to(ROOT)).replace("\\", "/")
            if transition_path is not None
            else ""
        )
    except ValueError:
        transition_ref = str(transition_path)

    return {
        "scope": "server-python",
        "scanned_files": len(files),
        "finding_count": len(undeclared),
        "files_with_findings": len(by_file),
        "retired_files_count": len(retired_report),
        "retired_files": retired_report,
        "passed": (
            not undeclared
            and not stale_entries
            and not declaration_error
            and not sunset_expired
        ),
        "by_file": dict(sorted(by_file.items())),
        "findings": undeclared,
        "transition_declaration_path": transition_ref,
        "transition_declaration_error": declaration_error,
        "transition_sunset": declaration["sunset"] if declaration else {},
        "transition_sunset_expired": sunset_expired,
        "transition_files": declaration["files"] if declaration else {},
        "transition_evidence": declaration["evidence"] if declaration else {},
        "transition_finding_count": len(transition_findings),
        "transition_by_file": dict(sorted(transition_by_file.items())),
        "transition_findings": transition_findings,
        "stale_transition_count": len(stale_entries),
        "stale_transition_entries": stale_entries,
    }


def write_report(report: dict[str, Any], output: Path = OUT) -> None:
    """落地审计快照；不修改源码或数据库。"""
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="server Python DB authority 审计（未声明残留 fail-closed）"
    )
    parser.add_argument(
        "--prune-baseline",
        action="store_true",
        help="把第二类过渡声明收缩到当前仍存在的条目（只减不加；有未声明残留时拒绝）",
    )
    args = parser.parse_args(argv)

    report = audit_python_authority()
    write_report(report)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "scope",
                    "scanned_files",
                    "finding_count",
                    "files_with_findings",
                    "retired_files_count",
                    "transition_finding_count",
                    "stale_transition_count",
                    "transition_sunset_expired",
                    "passed",
                )
            },
            ensure_ascii=False,
        )
    )

    if args.prune_baseline:
        try:
            removed = prune_transition_baseline(report)
        except ValueError as exc:
            print(f"prune-baseline 拒绝：{exc}", file=sys.stderr)
            raise SystemExit(1)
        print(f"prune-baseline: removed {removed} stale entries")
        return

    if not report["passed"]:
        if report["transition_declaration_error"]:
            print(
                f"transition 声明非法：{report['transition_declaration_error']}",
                file=sys.stderr,
            )
        if report["stale_transition_count"]:
            print(
                f"transition 基线未收缩：{report['stale_transition_count']} 条声明条目"
                "已不存在（只减不加，须 --prune-baseline 收缩）",
                file=sys.stderr,
            )
        if report["transition_sunset_expired"]:
            print(
                "transition 日落条款已过期："
                f"{report['transition_sunset'].get('expiry_date')}（绑 db-retire 里程碑）",
                file=sys.stderr,
            )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
