#!/usr/bin/env python3
"""build_mcp_inventory.py —— 生成 MCP 工具实际清单真相源（只读审计产物）。

真相源 = server/tools/*.py 的 @mcp.tool() 注册（经 gen_route_matrix 提取）+
tool_migration_matrix.json 的路由元数据。二者交叉验证后输出 inventory JSON。

不修改任何源码/矩阵，只读取并汇总。
"""
from __future__ import annotations

import collections
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts"))

import gen_route_matrix as gen  # noqa: E402

_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_mcp_inventory.json")


def main() -> int:
    # 1) 源码真相：每个 module 的 @mcp.tool() 名字集合
    src_by_module: dict[str, list[str]] = {}
    src_names: set[str] = set()
    for module in gen.TOOL_MODULES:
        names = gen.extract_tool_names(module)
        src_by_module[module] = sorted(names)
        src_names |= set(names)

    # 2) 矩阵真相：路由元数据
    matrix = json.load(open(gen._MATRIX_PATH, encoding="utf-8"))
    tools = matrix["tools"]
    matrix_names = {t["name"] for t in tools}

    # 3) 交叉验证
    only_src = sorted(src_names - matrix_names)
    only_matrix = sorted(matrix_names - src_names)

    inventory = {
        "source": "server/tools/*.py @mcp.tool() (via gen_route_matrix) + tool_migration_matrix.json",
        "src_tool_count": len(src_names),
        "matrix_tool_count": len(matrix_names),
        "matrix_total_tools_field": matrix.get("total_tools"),
        "consistent": (src_names == matrix_names),
        "only_in_source_code": only_src,
        "only_in_matrix": only_matrix,
        "by_module": {m: len(n) for m, n in sorted(src_by_module.items())},
        "by_target_backend": dict(collections.Counter(t["target_backend"] for t in tools)),
        "by_current_backend": dict(collections.Counter(t["current_backend"] for t in tools)),
        "by_op_class": dict(collections.Counter(t["op_class"] for t in tools)),
        "tools": sorted(
            [
                {
                    "name": t["name"],
                    "module": t["module"],
                    "rpc_method": t["rpc_method"],
                    "target_backend": t["target_backend"],
                    "current_backend": t["current_backend"],
                    "op_class": t["op_class"],
                    "status": t["status"],
                }
                for t in tools
            ],
            key=lambda x: (x["module"], x["name"]),
        ),
    }

    with open(_OUT, "w", encoding="utf-8") as fh:
        json.dump(inventory, fh, ensure_ascii=False, indent=2)

    print(f"MCP inventory 写入: {_OUT}")
    print(f"  源码工具数 = {len(src_names)}")
    print(f"  矩阵工具数 = {len(matrix_names)}")
    print(f"  一致 = {src_names == matrix_names}")
    print(f"  仅源码 = {only_src}")
    print(f"  仅矩阵 = {only_matrix}")
    print(f"  by_target_backend = {inventory['by_target_backend']}")
    print(f"  by_op_class = {inventory['by_op_class']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
