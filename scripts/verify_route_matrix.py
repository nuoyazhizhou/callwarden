#!/usr/bin/env python3
"""verify_route_matrix.py —— 路由矩阵一致性机器核对（T01/T05 门禁）。

核对内容：
1. **239/239 覆盖率**：矩阵工具总数必须为 239，且每个工具 name/module/rpc_method/
   target_backend/op_class 字段完整合法。
2. **dispatch 一致性**：target_backend ∈ {rust_native, task_rpc} 的工具，其
   rpc_method 必须在 `rust_ext/src/daemon/dispatch.rs` 中有 match 分支。
3. **白名单一致性**：target_backend = python_compat 的工具，其 rpc_method（=工具名）
   必须同时出现在 `rust_ext/src/daemon/http_server.rs` COMPAT_ROUTE_WHITELIST 与
   `server/compat_registry.py` RUST_COMPAT_ROUTE 中（两端对齐门）。
4. **薄壳一致性**：每个工具名必须仍注册在 `server/tools/<module>.py`（MCP 注册不丢失）。
5. **无 local 隐式路径**：不允许 rpc_method 为空/`—`/缺失；不允许出现
   target_backend=declared_unavailable 之外的无路由工具。

退出码：0 = 全部通过；1 = 任一核对失败（CI 门禁）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Set, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MATRIX_PATH = os.path.join(
    _REPO_ROOT, "deliverables", "software-company", "tool_migration_matrix.json"
)
_DISPATCH_PATH = os.path.join(_REPO_ROOT, "rust_ext", "src", "daemon", "dispatch.rs")
_HTTP_SERVER_PATH = os.path.join(_REPO_ROOT, "rust_ext", "src", "daemon", "http_server.rs")
_COMPAT_REGISTRY_PATH = os.path.join(_REPO_ROOT, "server", "compat_registry.py")
_TOOLS_DIR = os.path.join(_REPO_ROOT, "server", "tools")

EXPECTED_TOTAL = 239
BACKENDS = ("rust_native", "task_rpc", "python_compat", "declared_unavailable")
OP_CLASSES = ("READ_ONLY", "PROTECTED_MUTATION", "GOVERNANCE_WRITE")


def load_matrix() -> Dict[str, Any]:
    with open(_MATRIX_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def extract_dispatch_methods() -> Set[str]:
    """提取 dispatch.rs match 分支的 method 字面量 + CONVERGENCE_RPC_METHODS 清单。"""
    if not os.path.exists(_DISPATCH_PATH):
        return set()
    with open(_DISPATCH_PATH, "r", encoding="utf-8") as fh:
        src = fh.read()
    # 匹配 `"method.name" =>` 以及 `"a" | "b" =>` 多分支
    methods: Set[str] = set()
    for line in src.splitlines():
        m = re.search(r'"([a-zA-Z0-9_.]+)"\s*=>', line)
        if m:
            methods.add(m.group(1))
    # 收敛架构 RPC 经 CONVERGENCE_RPC_METHODS const 注册（is_convergence_rpc 分发）
    m2 = re.search(r"CONVERGENCE_RPC_METHODS: &\[&str\] = &\[(.*?)\];", src, re.S)
    if m2:
        methods.update(re.findall(r'"([a-zA-Z0-9_.]+)"', m2.group(1)))
    return methods


def extract_compat_whitelist() -> Set[str]:
    """提取 http_server.rs COMPAT_ROUTE_WHITELIST 条目（method 名）。"""
    if not os.path.exists(_HTTP_SERVER_PATH):
        return set()
    with open(_HTTP_SERVER_PATH, "r", encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"COMPAT_ROUTE_WHITELIST: &\[\(&str, &str\)\] = &\[(.*?)\];", src, re.S)
    if not m:
        return set()
    block = m.group(1)
    return set(re.findall(r'\("([a-zA-Z0-9_]+)",\s*"[a-z_]+"\)', block))


def extract_python_compat_routes() -> Set[str]:
    """提取 compat_registry.py RUST_COMPAT_ROUTE 键。"""
    if not os.path.exists(_COMPAT_REGISTRY_PATH):
        return set()
    with open(_COMPAT_REGISTRY_PATH, "r", encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"RUST_COMPAT_ROUTE: Dict\[str, str\] = \{(.*?)\}", src, re.S)
    if not m:
        return set()
    block = m.group(1)
    return set(re.findall(r'"([a-zA-Z0-9_]+)"\s*:', block))


def extract_tool_names_by_module() -> Dict[str, Set[str]]:
    """提取 server/tools/*.py 中实际注册的工具名。"""
    result: Dict[str, Set[str]] = {}
    for fname in sorted(os.listdir(_TOOLS_DIR)):
        if not fname.startswith("tools_") or not fname.endswith(".py"):
            continue
        path = os.path.join(_TOOLS_DIR, fname)
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        names = set(re.findall(r"@mcp\.tool\([^)]*\)\s*\n\s*def (\w+)\(", src))
        names |= set(re.findall(r"@mcp\.tool\(\)\s*\n\s*def (\w+)\(", src))
        result[fname[:-3]] = names
    return result


def main() -> int:
    errors: List[str] = []
    warnings: List[str] = []
    matrix = load_matrix()
    tools: List[Dict[str, Any]] = matrix.get("tools", [])
    total = matrix.get("total_tools", len(tools))

    # ---- 1. 239/239 覆盖率 ----
    if total != EXPECTED_TOTAL:
        errors.append(f"矩阵声明 total_tools={total}，期望 {EXPECTED_TOTAL}")
    if len(tools) != EXPECTED_TOTAL:
        errors.append(f"矩阵实际工具数 {len(tools)}，期望 {EXPECTED_TOTAL}")

    names: Set[str] = set()
    for t in tools:
        name = t.get("name", "")
        if not name:
            errors.append("存在 name 为空的行")
            continue
        if name in names:
            errors.append(f"工具名重复: {name}")
        names.add(name)
        if not t.get("module"):
            errors.append(f"{name}: 缺少 module")
        if not t.get("rpc_method"):
            errors.append(f"{name}: 缺少 rpc_method（本地隐式路径）")
        elif t["rpc_method"] in ("—", "-", ""):
            errors.append(f"{name}: rpc_method 为占位符（本地隐式路径）")
        if t.get("target_backend") not in BACKENDS:
            errors.append(f"{name}: 非法 target_backend {t.get('target_backend')!r}")
        if t.get("op_class") not in OP_CLASSES:
            errors.append(f"{name}: 非法 op_class {t.get('op_class')!r}")

    # ---- 2. dispatch 一致性 ----
    dispatch_methods = extract_dispatch_methods()
    for t in tools:
        if t["target_backend"] in ("rust_native", "task_rpc"):
            if t["rpc_method"] not in dispatch_methods:
                errors.append(
                    f"{t['name']}: rpc_method {t['rpc_method']} 未在 dispatch.rs 注册 "
                    f"（target_backend={t['target_backend']}）"
                )

    # ---- 3. 白名单一致性 ----
    rust_whitelist = extract_compat_whitelist()
    py_routes = extract_python_compat_routes()
    for t in tools:
        if t["target_backend"] == "python_compat":
            method = t["rpc_method"]
            if method not in rust_whitelist:
                errors.append(
                    f"{t['name']}: python_compat 方法 {method} 未在 "
                    f"http_server.rs COMPAT_ROUTE_WHITELIST"
                )
            if method not in py_routes:
                errors.append(
                    f"{t['name']}: python_compat 方法 {method} 未在 "
                    f"compat_registry.py RUST_COMPAT_ROUTE"
                )

    # ---- 4. 薄壳一致性 ----
    registered = extract_tool_names_by_module()
    all_registered: Set[str] = set()
    for mod_names in registered.values():
        all_registered |= mod_names
    for t in tools:
        if t["name"] not in all_registered:
            errors.append(
                f"{t['name']}: 未在 server/tools/{t['module']}.py 注册（MCP 丢失）"
            )
    for mod_name, mod_names in registered.items():
        for name in sorted(mod_names):
            if name not in names:
                warnings.append(f"{mod_name}.{name}: 已注册但不在矩阵中")

    # ---- 5. 无 local 隐式路径 ----
    for t in tools:
        if t.get("current_backend") == "legacy_local" and t["target_backend"] == "python_compat":
            warnings.append(
                f"{t['name']}: legacy_local → python_compat（过渡），需在 M2 前迁 native"
            )

    # ---- 汇总 ----
    print(f"矩阵工具总数: {total}（期望 {EXPECTED_TOTAL}）")
    print(f"dispatch.rs match 分支: {len(dispatch_methods)}")
    print(f"http_server.rs 白名单: {len(rust_whitelist)}")
    print(f"compat_registry.py RUST_COMPAT_ROUTE: {len(py_routes)}")
    print(f"tools 模块实际注册: {len(all_registered)}")
    print()
    if warnings:
        print("警告:")
        for w in warnings:
            print(f"  - {w}")
        print()
    if errors:
        print("失败（错误）:")
        for e in errors:
            print(f"  - {e}")
        print(f"共 {len(errors)} 个错误，{len(warnings)} 个警告")
        return 1
    print(f"核对通过: 239/239 覆盖，无本地隐式路径，{len(warnings)} 个警告")
    return 0


if __name__ == "__main__":
    sys.exit(main())
