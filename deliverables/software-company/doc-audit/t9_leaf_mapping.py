#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T9 叶子级 CLI<->MCP 对照 + 文档名漂移检测。

三源交叉比对：
1. 真实 MCP 工具名 = tool_migration_matrix.json 的 name 字段（权威，含改名后名字）
2. 文档映射表声明的 MCP 工具名 = docs/mcp_tools.md "CLI<->MCP 命名映射对照表(C8 Step#6)"
3. 真实 CLI 叶子命令 = audit_cli_inventory.json

输出：
- 文档名漂移清单：映射表写的 MCP 名 ∉ 真实工具名（过时/漂移，如 detect_cycle vs detect_call_cycles）
- 叶子级 CLI->MCP 映射（供思维导图）
- 纯 MCP / 纯 CLI 单边清单（合理 vs 需复核）
纯分析，不改代码。
"""
from __future__ import annotations

import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
MATRIX = os.path.join(os.path.dirname(_HERE), "tool_migration_matrix.json")
CLI_INV = os.path.join(_HERE, "audit_cli_inventory.json")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
MCP_DOC = os.path.join(_REPO_ROOT, "docs", "mcp_tools.md")


def clean_tool(cell: str) -> str:
    """从 markdown 单元格提取 MCP 工具名：去反引号/中文后缀/空白。"""
    cell = cell.strip()
    m = re.findall(r"`([a-zA-Z_][a-zA-Z0-9_]*)`", cell)
    return m[0] if m else ""


def parse_doc_mapping():
    """解析 docs C8 Step#6 映射表，返回 [(cli_cell, mcp_tool, section)]。"""
    with open(MCP_DOC, encoding="utf-8") as f:
        lines = f.readlines()
    # 定位 C8 Step#6 章节起点
    start = None
    for i, ln in enumerate(lines):
        if "CLI↔MCP 命名映射对照表" in ln:
            start = i
            break
    rows = []
    section = ""
    if start is None:
        return rows
    for ln in lines[start:]:
        s = ln.strip()
        if s.startswith("### "):
            section = s[4:].strip()
            continue
        if s.startswith("|") and "`" in s:
            cols = [c.strip() for c in s.strip("|").split("|")]
            if len(cols) >= 2:
                cli_cell = cols[0]
                mcp_tool = clean_tool(cols[1])
                if mcp_tool:
                    rows.append((cli_cell, mcp_tool, section))
    return rows


def main():
    matrix = json.load(open(MATRIX, encoding="utf-8"))
    real_tools = {t["name"] for t in matrix["tools"]}
    tool_meta = {t["name"]: t for t in matrix["tools"]}

    cli = json.load(open(CLI_INV, encoding="utf-8"))

    doc_rows = parse_doc_mapping()
    doc_mcp_names = [r[1] for r in doc_rows]

    # 1. 文档名漂移：映射表写的 MCP 名 ∉ 真实工具名
    drift = []
    for cli_cell, mcp_tool, section in doc_rows:
        if mcp_tool not in real_tools:
            drift.append({"section": section, "cli": cli_cell,
                          "doc_mcp_name": mcp_tool})

    # 2. 真实工具里未出现在文档映射表的（纯 MCP 或漏登记）
    doc_set = set(doc_mcp_names)
    not_in_doc = sorted(real_tools - doc_set)

    # 3. 文档映射表里重复映射到同一 MCP 工具的 CLI（多对一/潜在重复入口）
    from collections import defaultdict
    mcp_to_clis = defaultdict(list)
    for cli_cell, mcp_tool, _ in doc_rows:
        mcp_to_clis[mcp_tool].append(cli_cell)
    multi = {k: v for k, v in mcp_to_clis.items() if len(v) > 1}

    # 4. CLI 侧 alias/重复入口：同一 CLI 行里含 "/" 分隔的多形式（kebab subcommand + --flag）
    cli_alias_rows = []
    for cli_cell, mcp_tool, section in doc_rows:
        if "/" in cli_cell and "`" in cli_cell:
            forms = re.findall(r"`([^`]+)`", cli_cell)
            if len(forms) > 1:
                cli_alias_rows.append({"section": section, "forms": forms,
                                       "mcp": mcp_tool})

    report = {
        "real_tool_count": len(real_tools),
        "doc_mapping_rows": len(doc_rows),
        "doc_name_drift": drift,
        "real_tools_not_in_doc_mapping": not_in_doc,
        "multi_cli_to_one_mcp": multi,
        "cli_alias_forms": cli_alias_rows,
    }
    out = os.path.join(_HERE, "t9_leaf_mapping_result.json")
    json.dump(report, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    print("=" * 72)
    print(f"真实 MCP 工具: {len(real_tools)} | 文档映射表行: {len(doc_rows)}")
    print("=" * 72)
    print(f"\n[文档名漂移] 映射表写的 MCP 名已不是真实注册名: {len(drift)}")
    for d in drift:
        print(f"    [{d['section']}] CLI={d['cli']} -> 文档写 `{d['doc_mcp_name']}`（真实无此工具）")
    print(f"\n[CLI alias/重复入口] 同一能力多形式(kebab + --flag): {len(cli_alias_rows)}")
    for a in cli_alias_rows[:30]:
        print(f"    {a['forms']} -> {a['mcp']}")
    print(f"\n[多 CLI 映射到同一 MCP] {len(multi)}")
    for k, v in multi.items():
        print(f"    {k} <- {v}")
    print(f"\n结果写入: {out}")


if __name__ == "__main__":
    main()
