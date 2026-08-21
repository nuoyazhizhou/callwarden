#!/usr/bin/env python3
"""check_client_purity.py —— Python 薄壳层静态纯净度门禁（T01/T05）。

设计契约（cw-rust-client-convergence-design.md §1.3.2）：
- Python `server/tools/`、`cli/`、`cw.py` 只实现 client 薄壳（MCP 透传 + CLI 转发）；
- 禁止 `import sqlite3` / `from sqlite3`；
- 禁止业务性 `get_db()` 调用（`_mcp_common.get_db` 仅保留配置读取）；
- 禁止 `CodeGraphDB(` 实例化；
- 禁止 `db.*` 业务模块直接调用。

实现：基于 `ast` 精确扫描（忽略注释/文档字符串中的字样），
避免正则误报 docstring 中的 "get_db()" 描述。

门禁分层（T04-followup S1 已收敛）：
- **硬门禁（必须 0 违例）**：`server/tools/`（239 工具薄壳化）、`cw.py`
  （SQLite 预热已移除）；
- **软门禁（必须 0 违例）**：`cli/`——存量 `cli/main.py`（15K 行、~318 处
  DB 引用）已按 T04-followup S1 迁移为 daemon RPC 转发（RpcDBProxy +
  route_rpc），白名单已清空；其余 `cli/*.py`（daemon_commands/agent/client/
  console 等）同规则扫描，新违例一律拒绝（只减不加）。

退出码：0 = 通过（硬门禁 0 违例，软门禁违例均在白名单内）；1 = 失败。
"""
from __future__ import annotations

import ast
import os
import sys
from typing import Dict, List, Set, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 硬门禁：必须 0 违例
HARD_PATHS = ["server/tools", "cw.py"]

# 软门禁：报告 + 白名单（存量合法遗留，迁移后移除）
SOFT_PATHS = ["cli"]

# 存量遗留白名单：T04-followup S1 已完成 cli/main.py 迁移（daemon RPC 转发 +
# 移除 sqlite3/CodeGraphDB/db 业务模块直接调用），白名单应为空。
# 新增文件一律不得加入本白名单（只减不加）。
LEGACY_CLI_ALLOWLIST: Set[str] = set()


def scan_ast(path: str) -> List[str]:
    """AST 扫描单个 Python 文件，返回违例行描述。

    跳过 compat worker 处理函数（`_h_*` 与 `_bind_readonly_db`）——这些是
    daemon compat_adapter 调度的 Python H3 worker 侧实现，属于 compat 过渡期
    合法窗口（设计 Q5，白名单只减不加），不参与 MCP 薄壳层纯净度判定。
    """
    violations: List[str] = []
    rel = os.path.relpath(path, _REPO_ROOT).replace("\\", "/")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            source = fh.read()
    except OSError as exc:
        return [f"{rel}: 无法读取（{exc}）"]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"{rel}: 语法错误（{exc}）"]

    # 计算 compat worker 代码行区间（跳过）
    # 覆盖：`_h_*` 处理器、`_bind_readonly_db` 绑定助手，以及仅被它们调用的
    # worker 侧业务助手（compat 过渡期合法窗口，M2 deadline 后随白名单删除）。
    compat_ranges: List[Tuple[int, int]] = []
    compat_helper_names = {
        "_bind_readonly_db",
        "_collab_direct_read",
        "_p3_resolve_identity_arg",
        "_p3_identity_mcp_reason",
        "_p2_detect_cycle_on_edges",
        "_p2_find_cycle_dfs",
        "_p2_find_shortest_cycle",
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_h_") or node.name in compat_helper_names:
                compat_ranges.append((node.lineno, node.end_lineno or node.lineno))

    def in_compat(lineno: int) -> bool:
        return any(start <= lineno <= end for start, end in compat_ranges)

    for node in ast.walk(tree):
        # 1. sqlite3 导入
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlite3" or alias.name.startswith("sqlite3."):
                    if not in_compat(node.lineno):
                        violations.append(f"{rel}:{node.lineno}: 禁止 import sqlite3")
        if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            if not in_compat(node.lineno):
                violations.append(f"{rel}:{node.lineno}: 禁止 from sqlite3 import ...")
        # 2. CodeGraphDB 实例化（构造调用）
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "CodeGraphDB":
            if not in_compat(node.lineno):
                violations.append(f"{rel}:{node.lineno}: 禁止 CodeGraphDB 实例化")
        # 3. get_db() 业务调用（_mcp_common.py 自身定义除外）
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "get_db":
            if not in_compat(node.lineno):
                violations.append(f"{rel}:{node.lineno}: 禁止业务 get_db() 调用")
        # 4. db 业务模块直接调用（from callwarden.db.db_* / ..db.db_* import）
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if re_db_module(module) and not in_compat(node.lineno):
                violations.append(f"{rel}:{node.lineno}: 禁止 db 业务模块直接调用（{module}）")
        # 5. 属性访问 db.conn / db.db_path / db.workspace_root（仅读上下文）
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "db"
            and isinstance(node.ctx, ast.Load)
            and node.attr in ("db_path", "conn", "workspace_root")
            and not in_compat(node.lineno)
        ):
            violations.append(f"{rel}:{node.lineno}: 禁止 db 变量业务属性访问（db.{node.attr}）")

    return violations


def re_db_module(module: str) -> bool:
    """匹配 db 业务模块：callwarden.db.db_* / ..db.db_* / callwarden.analyzers.*"""
    return (
        module.endswith(".db") is False
        and ("db.db_" in module or module.startswith("db.db_") or "db_" in module.split(".")[-1])
    ) or module.startswith("callwarden.analyzers")


def main() -> int:
    hard_violations: List[str] = []
    soft_violations: List[str] = []
    legacy_report: List[str] = []
    hard_count = 0
    soft_count = 0

    def collect(paths: List[str], hard: bool) -> None:
        nonlocal hard_count, soft_count
        for rel_path in paths:
            full = os.path.join(_REPO_ROOT, rel_path)
            if os.path.isdir(full):
                for root, _dirs, files in os.walk(full):
                    for fname in sorted(files):
                        if fname.endswith(".py"):
                            file_path = os.path.join(root, fname)
                            rel = os.path.relpath(file_path, _REPO_ROOT).replace("\\", "/")
                            vs = scan_ast(file_path)
                            if hard:
                                hard_count += 1
                                hard_violations.extend(vs)
                            else:
                                soft_count += 1
                                if rel in LEGACY_CLI_ALLOWLIST:
                                    legacy_report.append(f"{rel}: 存量遗留（白名单，迁移 ticket T04-followup）")
                                    for v in vs:
                                        legacy_report.append(f"    - {v}")
                                else:
                                    soft_violations.extend(vs)
            elif os.path.isfile(full):
                rel = os.path.relpath(full, _REPO_ROOT).replace("\\", "/")
                vs = scan_ast(full)
                if hard:
                    hard_count += 1
                    hard_violations.extend(vs)
                else:
                    soft_count += 1
                    if rel in LEGACY_CLI_ALLOWLIST:
                        legacy_report.append(f"{rel}: 存量遗留（白名单，迁移 ticket T04-followup）")
                        for v in vs:
                            legacy_report.append(f"    - {v}")
                    else:
                        soft_violations.extend(vs)

    collect(HARD_PATHS, hard=True)
    collect(SOFT_PATHS, hard=False)

    print(f"硬门禁: 扫描 {hard_count} 个文件（server/tools + cw.py）")
    print(f"软门禁: 扫描 {soft_count} 个文件（cli/，含白名单存量）")
    print()
    if hard_violations:
        print(f"硬门禁失败（{len(hard_violations)} 个违例）:")
        for v in hard_violations:
            print(f"  - {v}")
        return 1

    if soft_violations:
        print(f"软门禁失败（{len(soft_violations)} 个新违例，白名单外）:")
        for v in soft_violations:
            print(f"  - {v}")
        return 1

    print("通过: server/tools + cw.py + cli/ 纯净（0 违例）")
    if legacy_report:
        print("提示: 白名单存量已清零（T04-followup S1 迁移完成），无遗留报告")
    return 0


if __name__ == "__main__":
    sys.exit(main())
