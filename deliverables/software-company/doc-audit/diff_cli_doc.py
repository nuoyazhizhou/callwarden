#!/usr/bin/env python3
"""diff_cli_doc.py —— CLI 文档 ↔ 实际清单双向比对（只读审计）。

真相源：audit_cli_inventory.json（70 顶层命令 + 各子动作，来自 _SUBCOMMANDS + `cw <cmd> --help`）。
文档：docs/cli_reference.md。

比对分两个粒度：
1. 顶层命令级（最稳健）：
   - doc_top_has_impl_missing：文档以 `### `cmd`` 或概览表介绍的顶层命令，实际不存在。
   - impl_top_has_doc_missing：实际 70 顶层命令，文档从未作为命令介绍（正文标题/概览表都没有）。
2. 数量声明：文档「13 大分类 / 60 个 --flag / 150+ 命令」等声明 vs 真相。

文档命令 token 提取：
- 标题 `### `xxx`` / `#### `xxx`` 里反引号内的首个 token（顶层命令名）。
- 概览表「主要 subcommand」列里的反引号 token 顶层名。
- 末尾 --flag 清单里的 `--xxx`。
"""
from __future__ import annotations

import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_DOC = os.path.join(_REPO_ROOT, "docs", "cli_reference.md")
_INV = os.path.join(_HERE, "audit_cli_inventory.json")
_OUT = os.path.join(_HERE, "diff_cli_doc_result.json")

# 标题里的反引号命令：### `gc policy show` / ### `--refresh-all`
_HEAD_CMD_RE = re.compile(r"^#{2,4}\s*`([^`]+)`")
# 标题里的裸命令形式：## cw lease / ## cw collab（多...） / ### cw assignment create
_HEAD_CW_RE = re.compile(r"^#{2,4}\s*cw\s+([a-z][a-z0-9\-]*)")
# 正文/表格里的反引号 token
_BACKTICK_RE = re.compile(r"`([^`]+)`")
# 代码块/正文里的 `cw <cmd>` 实际用法（用于顶层命令是否被介绍的兜底判定）
_CW_USAGE_RE = re.compile(r"\bcw\s+([a-z][a-z0-9\-]*)")


def main():
    inv = json.load(open(_INV, encoding="utf-8"))
    truth_top = set(inv["commands"].keys())          # 70 顶层
    # 真相叶子集合（top + top sub 规范化，sub 里 '-' 保留）
    truth_leaves = set()
    for top, e in inv["commands"].items():
        subs = e["sub_actions"]
        if subs:
            for s in subs:
                truth_leaves.add(f"{top} {s}")
        else:
            truth_leaves.add(top)

    lines = open(_DOC, encoding="utf-8").read().splitlines()

    # 1) 文档标题里的命令（区分 subcommand vs --flag）
    doc_head_cmds = []       # (line, raw, top_token, is_flag)
    doc_head_cw_top = set()  # ## cw xxx 形式标题的顶层名
    for i, line in enumerate(lines, 1):
        s = line.strip()
        m = _HEAD_CMD_RE.match(s)
        if m:
            raw = m.group(1).strip()
            first = raw.split()[0] if raw.split() else raw
            is_flag = first.startswith("--")
            doc_head_cmds.append({"line": i, "raw": raw, "top": first, "is_flag": is_flag})
        mc = _HEAD_CW_RE.match(s)
        if mc:
            doc_head_cw_top.add(mc.group(1))

    # 文档以标题正式介绍的顶层子命令名（非 flag）+ ## cw xxx 标题
    doc_head_top = {c["top"] for c in doc_head_cmds if not c["is_flag"]} | doc_head_cw_top

    # 2) 概览表（第一张表）里的 subcommand 反引号 token 顶层名
    #    概览表在「## 命令概览」到下一个 "## " 之间
    overview_tops = set()
    in_overview = False
    for i, line in enumerate(lines, 1):
        if line.strip().startswith("## 命令概览"):
            in_overview = True
            continue
        if in_overview and line.startswith("## ") and "命令概览" not in line:
            break
        if in_overview and line.startswith("|"):
            for tok in _BACKTICK_RE.findall(line):
                # 取每个反引号片段的第一个词作为顶层名；片段可能是 "gc archive/restore..." 或 "workspace list/register..."
                first = re.split(r"[\s/]", tok.strip())[0]
                if re.fullmatch(r"[a-z][a-z0-9\-]*", first):
                    overview_tops.add(first)

    # 2b) 全文 `cw <cmd>` 用法兜底（代码块/正文），只保留是真实顶层命令的
    cw_usage_tops = set()
    for line in lines:
        for mm in _CW_USAGE_RE.finditer(line):
            tok = mm.group(1)
            if tok in truth_top:
                cw_usage_tops.add(tok)

    doc_all_tops = doc_head_top | overview_tops | cw_usage_tops

    # 3) 双向顶层比对
    doc_top_has_impl_missing = sorted((doc_head_top | overview_tops) - truth_top)
    impl_top_has_doc_missing = sorted(truth_top - doc_all_tops)

    # 4) --flag 清单：末尾 Deprecated 章节 + 全文标题 flag
    doc_flags = sorted({c["raw"] for c in doc_head_cmds if c["is_flag"]})
    all_flag_tokens = set()
    for line in lines:
        for tok in _BACKTICK_RE.findall(line):
            for f in re.findall(r"--[a-z][a-z0-9\-]*", tok):
                all_flag_tokens.add(f)

    # 5) 数量声明
    count_claims = []
    for i, line in enumerate(lines, 1):
        if re.search(r"(13|12)\s*大?(功能)?分类|60\s*个|150\+|150 个|\b70\b|\b234\b", line):
            count_claims.append({"line": i, "text": line.strip()})

    result = {
        "doc": "docs/cli_reference.md",
        "doc_total_lines": len(lines),
        "truth_top_count": len(truth_top),
        "truth_leaf_count": len(truth_leaves),
        "doc_head_command_count": len(doc_head_cmds),
        "doc_head_cw_style_tops": sorted(doc_head_cw_top),
        "doc_introduced_top_count": len(doc_all_tops),
        "doc_introduced_tops": sorted(doc_all_tops),
        "doc_top_has_impl_missing_count": len(doc_top_has_impl_missing),
        "doc_top_has_impl_missing": doc_top_has_impl_missing,
        "impl_top_has_doc_missing_count": len(impl_top_has_doc_missing),
        "impl_top_has_doc_missing": impl_top_has_doc_missing,
        "doc_title_flag_count": len(doc_flags),
        "doc_all_flag_token_count": len(all_flag_tokens),
        "doc_all_flag_tokens": sorted(all_flag_tokens),
        "count_claims": count_claims,
    }

    with open(_OUT, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    print(f"CLI doc diff 写入: {_OUT}")
    print(f"  真相顶层命令 = {len(truth_top)}  叶子 = {len(truth_leaves)}")
    print(f"  文档介绍的顶层命令 = {len(doc_all_tops)}")
    print(f"  文档介绍但实现没有(顶层) = {len(doc_top_has_impl_missing)} -> {doc_top_has_impl_missing}")
    print(f"  实现有但文档未介绍(顶层) = {len(impl_top_has_doc_missing)} -> {impl_top_has_doc_missing}")
    print(f"  文档全文 --flag token 数 = {len(all_flag_tokens)}")
    print(f"  数量声明行 = {len(count_claims)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
