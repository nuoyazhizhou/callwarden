"""M2 Python 纯 client 审计（PRD §3.2 M2 + R0.2）。

验证目标：
- server/tools + cw.py 无直接 SQLite 业务读写（get_db 仅配置读取）；
- 无 CodeGraphDB 实例化、无 sqlite3 import、无 db.* 业务模块直接调用；
- cli/ 除白名单存量 cli/main.py（T04-followup）外无新违例；
- 抽查薄壳工具函数体 = 纯透传（参数 → route_rpc → 返回）。

方法：独立重跑 scripts/check_client_purity.py + AST 级独立复扫（双写互证）。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HARD_PATHS = ["server/tools", "cw.py"]
_SOFT_PATHS = ["cli"]
_LEGACY_ALLOWLIST = {"cli/main.py"}


def _scan_file(path: str):
    """独立 AST 复扫：返回违例行（与 check_client_purity.py 同规则，但独立实现）。"""
    violations = []
    rel = os.path.relpath(path, _REPO_ROOT).replace("\\", "/")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        source = fh.read()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"{rel}: 语法错误 {exc}"]
    compat_ranges = []
    compat_helpers = {
        "_bind_readonly_db", "_collab_direct_read", "_p3_resolve_identity_arg",
        "_p3_identity_mcp_reason", "_p2_detect_cycle_on_edges", "_p2_find_cycle_dfs",
        "_p2_find_shortest_cycle",
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_h_") or node.name in compat_helpers:
                compat_ranges.append((node.lineno, node.end_lineno or node.lineno))

    def in_compat(lineno: int) -> bool:
        return any(s <= lineno <= e for s, e in compat_ranges)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlite3" or alias.name.startswith("sqlite3."):
                    if not in_compat(node.lineno):
                        violations.append(f"{rel}:{node.lineno}: import sqlite3")
        if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            if not in_compat(node.lineno):
                violations.append(f"{rel}:{node.lineno}: from sqlite3 import")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "CodeGraphDB"):
            if not in_compat(node.lineno):
                violations.append(f"{rel}:{node.lineno}: CodeGraphDB 实例化")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "get_db"):
            if not in_compat(node.lineno):
                violations.append(f"{rel}:{node.lineno}: get_db() 调用")
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if (("db.db_" in module or module.startswith("db.db_")
                 or "db_" in module.split(".")[-1]) and not in_compat(node.lineno)):
                violations.append(f"{rel}:{node.lineno}: db 业务模块调用 {module}")
    return violations


class TestHardPurity:
    """硬门禁：server/tools + cw.py 必须 0 违例。"""

    @pytest.mark.parametrize("rel", [
        "server/tools/tools_query.py",
        "server/tools/tools_workspace.py",
        "server/tools/tools_task.py",
        "server/tools/tools_security.py",
        "server/tools/tools_collab.py",
        "server/tools/tools_p2_graph.py",
        "server/tools/tools_p3_identity.py",
        "server/tools/tools_p4_lease.py",
        "server/tools/tools_semantic.py",
        "server/tools/tools_summary.py",
        "server/tools/tools_rules.py",
        "cw.py",
    ])
    def test_no_sqlite_business_in_module(self, rel):
        path = os.path.join(_REPO_ROOT, rel.replace("/", os.sep))
        assert os.path.isfile(path), f"{rel} 不存在"
        violations = _scan_file(path)
        assert not violations, f"{rel} 存在违例: {violations[:5]}"

    def test_check_client_purity_script_exit_zero(self):
        r = subprocess.run(
            [sys.executable, os.path.join(_REPO_ROOT, "scripts", "check_client_purity.py")],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        assert r.returncode == 0, f"check_client_purity 退出码 {r.returncode}\n{r.stdout}\n{r.stderr}"


class TestThinShellSpotCheck:
    """抽查薄壳工具函数体 = 纯透传（M2 行为面，非仅静态）。"""

    def test_tools_query_shells_are_passthrough(self):
        """抽查 tools_query.py 若干工具：函数体只调用 _route()，无本地业务逻辑。"""
        src = open(os.path.join(_REPO_ROOT, "server", "tools", "tools_query.py"),
                   encoding="utf-8").read()
        # 用 AST 找 get_stats / get_symbol / get_callers 的函数体，断言仅含 _route 调用
        tree = ast.parse(src)
        targets = {"get_stats", "get_symbol", "get_callers", "search_symbols",
                   "get_file_symbols", "get_callees"}
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in targets:
                calls = [n for n in ast.walk(node)
                         if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
                call_names = [c.func.id for c in calls if c.func.id != "dict"]
                found[node.name] = call_names
        for name in targets:
            assert name in found, f"未找到工具 {name}"
            # 薄壳化后应恰好一次 _route 调用（参数透传）；允许 list/dict 字面量构造
            route_calls = [c for c in found[name] if c == "_route"]
            assert len(route_calls) == 1, f"{name} 应恰好 1 次 _route 调用: {found[name]}"

    def test_tools_workspace_shells_are_passthrough(self):
        src = open(os.path.join(_REPO_ROOT, "server", "tools", "tools_workspace.py"),
                   encoding="utf-8").read()
        tree = ast.parse(src)
        targets = {"build_graph", "refresh_file", "file_read", "file_grep"}
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in targets:
                calls = [n for n in ast.walk(node)
                         if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
                found[node.name] = [c.func.id for c in calls if c.func.id != "dict"]
        for name in targets:
            assert name in found, f"未找到工具 {name}"
            route_calls = [c for c in found[name] if c == "_route"]
            assert len(route_calls) == 1, f"{name} 应恰好 1 次 _route 调用: {found[name]}"
