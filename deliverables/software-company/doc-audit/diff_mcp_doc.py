#!/usr/bin/env python3
"""diff_mcp_doc.py —— MCP 文档 ↔ 实际清单双向比对（只读审计，v2）。

真相源：audit_mcp_inventory.json（243 工具，源码==矩阵已验证）。
文档：docs/mcp_tools.md 的「各分类工具清单」章节（#### [N] 分类名（X 个）下反引号工具名）。

该章节是文档正式声称的工具集合，比全文反引号更精确。

输出：
- doc_declared_tools：文档分类清单声称的全部工具名（去重）。
- doc_has_impl_missing：文档声称但不在实际 243 集里（文档有实现没有）。
- impl_has_doc_missing：实际 243 里但文档分类清单未列出（实现有文档没有）。
- category_count_mismatch：每个分类标注数字 vs 实际列出工具数不符。
- category_total：分类标注数字合计 vs 文档头/矩阵 243。
- count_claims：全文数量声明行（供数字矛盾判断）。
"""
from __future__ import annotations

import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_DOC = os.path.join(_REPO_ROOT, "docs", "mcp_tools.md")
_INV = os.path.join(_HERE, "audit_mcp_inventory.json")
_OUT = os.path.join(_HERE, "diff_mcp_doc_result.json")

_CAT_HEAD_RE = re.compile(r"^####\s*\[(\d+)\]\s*(.+?)（(\d+)\s*个）")
_BACKTICK_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")


def main():
    inv = json.load(open(_INV, encoding="utf-8"))
    tool_names = {t["name"] for t in inv["tools"]}
    lines = open(_DOC, encoding="utf-8").read().splitlines()

    # 定位「各分类工具清单」章节：从 "### 各分类工具清单" 到下一个 "---" 之后的 "> 注"
    start = None
    for i, l in enumerate(lines):
        if l.strip().startswith("### 各分类工具清单"):
            start = i
            break

    categories = []          # {idx, name, declared, actual, tools:[...]}
    doc_declared_tools = set()
    cur = None
    if start is not None:
        for i in range(start, len(lines)):
            line = lines[i]
            m = _CAT_HEAD_RE.match(line.strip())
            if m:
                if cur:
                    categories.append(cur)
                cur = {"idx": int(m.group(1)), "name": m.group(2).strip(),
                       "declared": int(m.group(3)), "tools": []}
                continue
            # 到下一个 "> **注**" 或再下一个 "## " 大标题则结束
            if cur is not None and (line.strip().startswith("> ") or line.startswith("## ")):
                # 但工具清单行紧跟在分类标题后，遇到注释块结束
                if line.strip().startswith("> "):
                    categories.append(cur)
                    cur = None
                    break
            if cur is not None:
                toks = _BACKTICK_RE.findall(line)
                for tok in toks:
                    cur["tools"].append(tok)
                    doc_declared_tools.add(tok)
        if cur:
            categories.append(cur)

    for c in categories:
        c["actual"] = len(c["tools"])

    category_count_mismatch = [
        {"idx": c["idx"], "name": c["name"], "declared": c["declared"], "actual_listed": c["actual"]}
        for c in categories if c["declared"] != c["actual"]
    ]

    doc_has_impl_missing = sorted(doc_declared_tools - tool_names)
    impl_has_doc_missing = sorted(tool_names - doc_declared_tools)

    declared_total = sum(c["declared"] for c in categories)
    listed_total = sum(c["actual"] for c in categories)

    # 全文数量声明
    count_claims = []
    for i, line in enumerate(lines, 1):
        if re.search(r"\b(243|242|241|239|237|200|193|142|107|58|44)\b", line) and \
           ("工具" in line or "tool" in line.lower() or "backend" in line.lower()
            or "rust_native" in line or "python_compat" in line or "合计" in line):
            count_claims.append({"line": i, "text": line.strip()})

    result = {
        "doc": "docs/mcp_tools.md",
        "truth_tool_count": len(tool_names),
        "category_count": len(categories),
        "category_declared_total": declared_total,
        "category_listed_total": listed_total,
        "categories": [{"idx": c["idx"], "name": c["name"], "declared": c["declared"], "listed": c["actual"]} for c in categories],
        "category_count_mismatch": category_count_mismatch,
        "doc_declared_tool_count": len(doc_declared_tools),
        "doc_has_impl_missing_count": len(doc_has_impl_missing),
        "doc_has_impl_missing": doc_has_impl_missing,
        "impl_has_doc_missing_count": len(impl_has_doc_missing),
        "impl_has_doc_missing": impl_has_doc_missing,
        "count_claims": count_claims,
    }

    with open(_OUT, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    print(f"MCP doc diff v2 写入: {_OUT}")
    print(f"  真相工具数 = {len(tool_names)}")
    print(f"  分类数 = {len(categories)}")
    print(f"  分类标注数字合计 = {declared_total}")
    print(f"  分类实际列出合计 = {listed_total}")
    print(f"  文档声称工具集大小(去重) = {len(doc_declared_tools)}")
    print(f"  分类数字不符 = {len(category_count_mismatch)}")
    print(f"  文档声称但实现没有 = {len(doc_has_impl_missing)} -> {doc_has_impl_missing}")
    print(f"  实现有但文档分类未列 = {len(impl_has_doc_missing)} -> {impl_has_doc_missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
