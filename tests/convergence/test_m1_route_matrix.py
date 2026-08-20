"""M1 239/239 路由验证（PRD §3.2 M1 + R1.1）。

验证目标：
- 路由矩阵（tool_migration_matrix.json）239/239 覆盖；
- 四端一致：矩阵 ↔ dispatch.rs 分支 ↔ http_server.rs 白名单 ↔ compat_registry.py
  两端对齐 ↔ server/tools 薄壳注册；
- 无 local 隐式路径（每个工具都有 daemon 路由）；
- MCP server 注册工具数 = 239 且与矩阵 1:1（工具名不变，对外契约不破坏）。

方法：独立重跑 scripts/verify_route_matrix.py，并用独立断言复刻其关键核对
（不完全信任脚本自身——测试与脚本双写，互证）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MATRIX = os.path.join(_REPO_ROOT, "deliverables", "software-company",
                       "tool_migration_matrix.json")
_DISPATCH = os.path.join(_REPO_ROOT, "rust_ext", "src", "daemon", "dispatch.rs")
_HTTP_SERVER = os.path.join(_REPO_ROOT, "rust_ext", "src", "daemon", "http_server.rs")
_COMPAT_REG = os.path.join(_REPO_ROOT, "server", "compat_registry.py")
_TOOLS_DIR = os.path.join(_REPO_ROOT, "server", "tools")
_EXPECTED_TOTAL = 239


def _load_matrix():
    with open(_MATRIX, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _dispatch_methods():
    src = open(_DISPATCH, encoding="utf-8").read()
    methods = set()
    for line in src.splitlines():
        m = re.search(r'"([a-zA-Z0-9_.]+)"\s*=>', line)
        if m:
            methods.add(m.group(1))
    m2 = re.search(r"CONVERGENCE_RPC_METHODS: &\[&str\] = &\[(.*?)\];", src, re.S)
    if m2:
        methods.update(re.findall(r'"([a-zA-Z0-9_.]+)"', m2.group(1)))
    return methods


def _rust_whitelist():
    src = open(_HTTP_SERVER, encoding="utf-8").read()
    m = re.search(r"COMPAT_ROUTE_WHITELIST: &\[\(&str, &str\)\] = &\[(.*?)\];", src, re.S)
    return set(re.findall(r'\("([a-zA-Z0-9_]+)",\s*"[a-z_]+"\)', m.group(1))) if m else set()


def _py_compat_routes():
    src = open(_COMPAT_REG, encoding="utf-8").read()
    m = re.search(r"RUST_COMPAT_ROUTE: Dict\[str, str\] = \{(.*?)\}", src, re.S)
    return set(re.findall(r'"([a-zA-Z0-9_]+)"\s*:', m.group(1))) if m else set()


def _registered_tool_names():
    names = set()
    for fname in os.listdir(_TOOLS_DIR):
        if not fname.startswith("tools_") or not fname.endswith(".py"):
            continue
        src = open(os.path.join(_TOOLS_DIR, fname), encoding="utf-8").read()
        names |= set(re.findall(r"@mcp\.tool\([^)]*\)\s*\n\s*def (\w+)\(", src))
        names |= set(re.findall(r"@mcp\.tool\(\)\s*\n\s*def (\w+)\(", src))
    return names


class TestMatrixCoverage:
    def test_matrix_has_239_tools(self):
        m = _load_matrix()
        assert m["total_tools"] == _EXPECTED_TOTAL
        assert len(m["tools"]) == _EXPECTED_TOTAL
        names = [t["name"] for t in m["tools"]]
        assert len(set(names)) == _EXPECTED_TOTAL, "工具名重复"

    def test_no_local_implicit_path(self):
        """每个工具必须有非空 rpc_method（declared_unavailable 除外也应有声明）。"""
        m = _load_matrix()
        for t in m["tools"]:
            assert t.get("rpc_method"), f"{t['name']}: 缺少 rpc_method（本地隐式路径）"
            assert t["rpc_method"] not in ("—", "-", ""), f"{t['name']}: rpc_method 为占位符"
            assert t["target_backend"] in ("rust_native", "task_rpc", "python_compat",
                                           "declared_unavailable")
            assert t["op_class"] in ("READ_ONLY", "PROTECTED_MUTATION", "GOVERNANCE_WRITE")

    def test_dispatch_covers_rust_native_and_task_rpc(self):
        """矩阵 target_backend ∈ {rust_native, task_rpc} 的 rpc_method 必须在 dispatch.rs。"""
        m = _load_matrix()
        dispatch = _dispatch_methods()
        missing = []
        for t in m["tools"]:
            if t["target_backend"] in ("rust_native", "task_rpc"):
                if t["rpc_method"] not in dispatch:
                    missing.append((t["name"], t["rpc_method"]))
        assert not missing, f"dispatch.rs 未覆盖: {missing[:10]}"

    def test_compat_whitelist_two_side_aligned(self):
        """python_compat 工具必须同时出现在 Rust 白名单与 Python RUST_COMPAT_ROUTE。"""
        m = _load_matrix()
        rust_wl = _rust_whitelist()
        py_routes = _py_compat_routes()
        for t in m["tools"]:
            if t["target_backend"] == "python_compat":
                assert t["rpc_method"] in rust_wl, \
                    f"{t['name']}: 不在 http_server.rs COMPAT_ROUTE_WHITELIST"
                assert t["rpc_method"] in py_routes, \
                    f"{t['name']}: 不在 compat_registry.py RUST_COMPAT_ROUTE"

    def test_all_tools_registered_in_mcp_shell(self):
        """每个矩阵工具名仍注册在 server/tools/*.py（MCP 不丢失）。"""
        m = _load_matrix()
        registered = _registered_tool_names()
        missing = [t["name"] for t in m["tools"] if t["name"] not in registered]
        assert not missing, f"MCP 未注册: {missing[:10]}"

    def test_verify_route_matrix_script_exit_zero(self):
        """独立重跑门禁脚本：退出码必须为 0。"""
        r = subprocess.run(
            [sys.executable, os.path.join(_REPO_ROOT, "scripts", "verify_route_matrix.py")],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        assert r.returncode == 0, f"verify_route_matrix 退出码 {r.returncode}\n{r.stdout}\n{r.stderr}"

    def test_mcp_server_registers_239_tools(self):
        """create_mcp_server 注册工具数 = 239 且与矩阵 1:1（签名不丢）。"""
        m = _load_matrix()
        matrix_names = {t["name"] for t in m["tools"]}
        from callwarden.server.mcp_server import create_mcp_server
        mcp = create_mcp_server()
        tools = mcp._tool_manager.list_tools()
        reg_names = {t.name for t in tools}
        assert len(reg_names) == _EXPECTED_TOTAL, \
            f"MCP 注册 {len(reg_names)} != 239"
        assert matrix_names == reg_names, (
            f"矩阵与 MCP 注册不一致: 缺 {matrix_names - reg_names}, 多 {reg_names - matrix_names}"
        )
