#!/usr/bin/env python3
"""thinify_tools.py —— 将 server/tools/*.py 的 MCP 工具函数薄壳化（T03 一次性转换）。

设计契约（cw-rust-client-convergence-design.md §1.2 / §8）：
- 每个 `@mcp.tool()` 函数改为「参数 → route_rpc() → 结果原样返回」；
- 保留：函数签名（对外契约不变）、docstring（MCP 工具描述）、
  compat worker 注册块（_h_* handler + register_compat_routes，过渡期合法窗口）；
- 移除：工具路径中的 get_db() / 本地 SQL / _http_unsupported 业务分支；
- 写操作（PROTECTED_MUTATION / GOVERNANCE_WRITE）自动附加幂等 request_id。

用法：
    python scripts/thinify_tools.py [--dry-run]
"""
from __future__ import annotations

import ast
import json
import os
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MATRIX_PATH = os.path.join(
    _REPO_ROOT, "deliverables", "software-company", "tool_migration_matrix.json"
)
_TOOLS_DIR = os.path.join(_REPO_ROOT, "server", "tools")

TOOL_MODULES = [
    "tools_query",
    "tools_workspace",
    "tools_semantic",
    "tools_task",
    "tools_summary",
    "tools_security",
    "tools_rules",
    "tools_collab",
    "tools_p2_graph",
    "tools_p3_identity",
    "tools_p4_lease",
]

# 同步 job 工具：job_submit(sync=true) 后解包 result（返回结构不变）
SYNC_JOB_TOOLS: Set[str] = {
    "run_semgrep_scan",
    "scan_semgrep_incremental",
    "detect_clones",
    "embed_symbols",
    "embed_single_symbol",
    "import_git_history",
    "import_git_blame",
    "import_codeowners",
    "import_project_dependencies",
    "import_envelope_dependencies",
    "import_coverage",
    "build_hard_dependency_edges",
    "detect_cross_repo_deps",
    "prune_external_symbols",
}

# 异步 job 工具：job_submit(sync=false) 返回 {job_id, status}
ASYNC_JOB_TOOLS: Set[str] = {
    "semgrep_scan_async",
    "detect_clones_async",
    "embed_symbols_async",
}

# 需要解包 job result 的工具（SYNC_JOB_TOOLS 子集，等价）
_UNWRAP_JOB_RESULT: Set[str] = set(SYNC_JOB_TOOLS)

# 工具参数映射特例（RPC 契约与 MCP 参数名不一致时，透传时补映射）
# 格式：{工具名: {MCP参数名: RPC字段名}}
# 例：workspace.register 要求 client_view_root（daemon 强制 require），
# MCP register_workspace 参数名为 root_path → 透传时映射为 client_view_root。
SPECIAL_PARAM_MAP: Dict[str, Dict[str, str]] = {
    "register_workspace": {"root_path": "client_view_root"},
}


def params_expr_with_special(names: List[str], tool_name: str) -> str:
    """构造 params dict，应用 SPECIAL_PARAM_MAP 的字段映射。"""
    entries: List[str] = []
    used_targets: Set[str] = set()
    special = SPECIAL_PARAM_MAP.get(tool_name, {})
    for name in names:
        if name.startswith("**"):
            key = name[2:]
            entries.append(f'"{key}": dict({key})')
        elif name.startswith("*"):
            key = name[1:]
            entries.append(f'"{key}": list({key})')
        else:
            target = special.get(name, name)
            used_targets.add(target)
            if target != name:
                entries.append(f'"{target}": {name}')
            else:
                entries.append(f'"{name}": {name}')
    # 补充特例中未覆盖的静态字段（如 client_view_root 映射到 root_path 参数）
    return "{" + ", ".join(entries) + "}"


def load_matrix() -> Dict[str, Dict[str, Any]]:
    with open(_MATRIX_PATH, "r", encoding="utf-8") as fh:
        matrix = json.load(fh)
    by_name: Dict[str, Dict[str, Any]] = {}
    for t in matrix["tools"]:
        by_name[t["name"]] = t
    return by_name


def param_names(node: ast.FunctionDef) -> List[str]:
    """提取函数参数名（位置 + 关键字，含 vararg/kwarg 特殊名）。"""
    names: List[str] = []
    for a in node.args.posonlyargs + node.args.args:
        names.append(a.arg)
    if node.args.vararg is not None:
        names.append(f"*{node.args.vararg.arg}")
    for a in node.args.kwonlyargs:
        names.append(a.arg)
    if node.args.kwarg is not None:
        names.append(f"**{node.args.kwarg.arg}")
    return names


def params_expr(names: List[str]) -> str:
    """构造 params dict 字面量：{"a": a, "b": b, "*args": list(args), "**kwargs": dict(kwargs)}"""
    entries: List[str] = []
    for name in names:
        if name.startswith("**"):
            key = name[2:]
            entries.append(f'"{key}": dict({key})')
        elif name.startswith("*"):
            key = name[1:]
            entries.append(f'"{key}": list({key})')
        else:
            entries.append(f'"{name}": {name}')
    return "{" + ", ".join(entries) + "}"


def build_tool_body(
    tool_name: str,
    names: List[str],
    route: Dict[str, Any],
) -> str:
    """构造薄壳函数体（缩进 8 空格，嵌套于 register(mcp) 内）。"""
    indent = "        "
    rpc_method = route["rpc_method"]
    op_class = route["op_class"]
    params = params_expr_with_special(names, tool_name)

    if tool_name in SYNC_JOB_TOOLS or tool_name in ASYNC_JOB_TOOLS:
        job_type = route.get("job_type", "")
        # Python 布尔字面量必须是 True/False（大写），不能用 JSON 小写 true/false
        # （CW-1 回归：小写会触发 NameError: name 'true' is not defined）。
        sync = "True" if tool_name in SYNC_JOB_TOOLS else "False"
        base_params = f'{{**{params}, "job_type": "{job_type}", "sync": {sync}}}'
        if tool_name in _UNWRAP_JOB_RESULT:
            return (
                f"{indent}_res = _route({rpc_method!r}, {base_params}, {op_class!r})\n"
                f"{indent}return _res.get(\"result\") if isinstance(_res, dict) and \"result\" in _res else _res\n"
            )
        return f"{indent}return _route({rpc_method!r}, {base_params}, {op_class!r})\n"
    if tool_name == "cancel_job":
        return f"{indent}return _route({rpc_method!r}, {params}, {op_class!r})\n"
    return f"{indent}return _route({rpc_method!r}, {params}, {op_class!r})\n"


def find_tool_functions(source: str) -> List[Tuple[ast.FunctionDef, int, int, int]]:
    """返回 (fn_node, decorator_start_line, docstring_end_line, fn_end_line)。"""
    tree = ast.parse(source)
    out: List[Tuple[ast.FunctionDef, int, int, int]] = []
    for mod_node in tree.body:
        if not isinstance(mod_node, ast.FunctionDef) or mod_node.name != "register":
            continue
        for stmt in mod_node.body:
            if not isinstance(stmt, ast.FunctionDef):
                continue
            has_tool_decorator = any(
                isinstance(d, ast.Name) and d.id == "mcp" and isinstance(d, ast.Name)
                for d in stmt.decorator_list
            )
            # 更稳健：检查装饰器为 mcp.tool() 调用
            has_tool_decorator = any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr == "tool"
                for d in stmt.decorator_list
            )
            if not has_tool_decorator:
                continue
            decorator_start = stmt.decorator_list[0].lineno
            docstring_end: Optional[int] = None
            if stmt.body and isinstance(stmt.body[0], ast.Expr) and isinstance(
                stmt.body[0].value, ast.Constant
            ) and isinstance(stmt.body[0].value.value, str):
                docstring_end = stmt.body[0].end_lineno
            out.append((stmt, decorator_start, docstring_end or stmt.lineno, stmt.end_lineno))
    return out


def inject_route_import(source: str) -> Tuple[str, bool]:
    """在顶部导入区注入 `from ..daemon_client import route_rpc as _route`（幂等）。

    用 AST 定位最后一个顶层 Import/ImportFrom 节点的 end_lineno，插入到其后
    （正确处理多行括号 import，避免拆裂 `from x import (...)`）。
    """
    if "route_rpc as _route" in source:
        return source, False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, False
    last_import_end = -1
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import_end = max(last_import_end, node.end_lineno or node.lineno)
    if last_import_end < 0:
        return "from ..daemon_client import route_rpc as _route\n" + source, True
    lines = source.splitlines(keepends=True)
    lines.insert(
        last_import_end,
        "\nfrom ..daemon_client import route_rpc as _route\n",
    )
    return "".join(lines), True


def thinify_module(module_name: str, matrix: Dict[str, Dict[str, Any]]) -> Tuple[int, List[str]]:
    path = os.path.join(_TOOLS_DIR, f"{module_name}.py")
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    tools = find_tool_functions(source)
    if not tools:
        return 0, [f"{module_name}: 未找到工具函数"]

    lines = source.splitlines(keepends=True)
    # 收集替换区间（按行号降序处理，避免偏移）
    replacements: List[Tuple[int, int, str]] = []
    errors: List[str] = []
    for fn, dec_start, keep_end, fn_end in tools:
        tool_name = fn.name
        route = matrix.get(tool_name)
        if route is None:
            errors.append(f"{module_name}.{tool_name}: 不在路由矩阵中")
            continue
        names = param_names(fn)
        body = build_tool_body(tool_name, names, route)
        # 区间：[dec_start, keep_end] 保留，插入 body，跳过 (keep_end, fn_end]
        replacements.append((dec_start, keep_end, fn_end, body))

    # 从后往前应用
    for dec_start, keep_end, fn_end, body in sorted(
        replacements, key=lambda r: r[0], reverse=True
    ):
        # 保留 [dec_start-1, keep_end-1]（行号 1-based → index）
        head = lines[dec_start - 1 : keep_end]
        tail = lines[fn_end:]  # 从 fn_end（1-based end 行）之后继续
        # fn_end 行本身可能不是最后一行 body；用 fn_end 行索引处理：
        # fn_end 是函数结束行（含），tail 应从 fn_end（索引 fn_end）开始
        tail = lines[fn_end:]
        lines = lines[: dec_start - 1] + head + [body] + tail

    new_source = "".join(lines)
    new_source, _ = inject_route_import(new_source)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_source)
    return len(replacements), errors


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    matrix = load_matrix()
    total = 0
    all_errors: List[str] = []
    for module in TOOL_MODULES:
        if dry_run:
            path = os.path.join(_TOOLS_DIR, f"{module}.py")
            src = open(path, encoding="utf-8").read()
            tools = find_tool_functions(src)
            print(f"{module}: {len(tools)} 个工具待薄壳化")
            total += len(tools)
            continue
        count, errors = thinify_module(module, matrix)
        total += count
        all_errors.extend(errors)
        print(f"{module}: 薄壳化 {count} 个工具")
    if all_errors:
        print("错误:")
        for e in all_errors:
            print(f"  - {e}")
        return 1
    print(f"完成: 共处理 {total} 个工具")
    return 0


if __name__ == "__main__":
    sys.exit(main())
