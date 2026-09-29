#!/usr/bin/env python3
"""parity_mcp_cli.py —— MCP 工具 ↔ CLI 命令 对等性核对(只读)。

目的:不强行拉平数量(尊重 frozen spec §12「避免 T=M=D」),而是把差异分成
三类并判断每类是否合理:
  - BOTH: MCP 与 CLI 都暴露(有对应)。
  - MCP_ONLY: 只有 MCP 工具,无 CLI 命令(是否合理:Agent 编排/治理增量域?)。
  - CLI_ONLY: 只有 CLI 命令,无 MCP 工具(是否合理:本地宿主操作 install/doctor?)。

匹配依据:mcp_tools.md「CLI↔MCP 命名映射对照表」的人工映射(权威对应关系)
+ 名称启发式(MCP 去掉 get_/前缀 与 CLI 顶层/子动作 token 比对)。
输出人工可判读的分类清单,不做自动裁决。
"""
from __future__ import annotations
import json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
DOC = os.path.join(REPO, "docs", "mcp_tools.md")
MCP_INV = os.path.join(HERE, "audit_mcp_inventory.json")
CLI_INV = os.path.join(HERE, "audit_cli_inventory.json")
OUT = os.path.join(HERE, "parity_mcp_cli_result.json")

# 已知合理的 CLI 专属域(本地宿主操作,不应做 MCP 工具)
CLI_ONLY_EXPECTED = {
    "install", "setup", "server", "test", "daemon", "doctor",
    "install-agent", "install-hook", "config", "rollback", "experiment",
}


def load_mcp_tool_names():
    inv = json.load(open(MCP_INV, encoding="utf-8"))
    return {t["name"] for t in inv["tools"]}, inv["tools"]


def load_cli_leaves():
    inv = json.load(open(CLI_INV, encoding="utf-8"))
    leaves = []
    for top, e in inv["commands"].items():
        subs = e["sub_actions"]
        if subs:
            for s in subs:
                leaves.append((top, s, f"{top} {s}"))
        else:
            leaves.append((top, "", top))
    return leaves


def parse_mapping_table():
    """从 mcp_tools.md「CLI↔MCP 命名映射对照表」提取 (cli_cmd_token -> mcp_tool)。"""
    text = open(DOC, encoding="utf-8").read().splitlines()
    in_map = False
    mapping = []  # (cli_raw, mcp_tool)
    row_re = re.compile(r"^\|\s*`([^`]+)`.*?\|\s*`([a-z_]+)`\s*\|")
    for line in text:
        if line.strip().startswith("## CLI↔MCP 命名映射对照表"):
            in_map = True
            continue
        if in_map and line.startswith("## ") and "映射对照表" not in line:
            break
        if in_map:
            m = row_re.match(line)
            if m:
                mapping.append((m.group(1).strip(), m.group(2).strip()))
    return mapping


def main():
    mcp_names, mcp_tools = load_mcp_tool_names()
    cli_leaves = load_cli_leaves()
    mapping = parse_mapping_table()

    mapped_mcp = {mcp for _, mcp in mapping}          # 映射表覆盖的 MCP 工具
    mapped_cli_tokens = set()
    for cli_raw, _ in mapping:
        # cli_raw 形如 "cw stats" / "cw workspace list" / "cw refresh <paths>" / "cw --refresh"
        toks = cli_raw.replace("cw", "", 1).strip().split()
        toks = [t for t in toks if not t.startswith(("<", "--", "/"))]
        if toks:
            mapped_cli_tokens.add(" ".join(toks[:2]) if len(toks) >= 2 else toks[0])

    # MCP 分类
    mcp_only = sorted(mcp_names - mapped_mcp)
    mcp_both = sorted(mcp_names & mapped_mcp)

    # CLI 分类:leaf 是否在映射表 or 是已知 CLI 专属域
    cli_only = []
    cli_both = []
    for top, sub, leaf in cli_leaves:
        key2 = f"{top} {sub}".strip()
        matched = (key2 in mapped_cli_tokens) or (top in mapped_cli_tokens)
        if matched:
            cli_both.append(leaf)
        else:
            cli_only.append({"leaf": leaf, "top": top,
                             "expected_cli_only": top in CLI_ONLY_EXPECTED})

    result = {
        "summary": {
            "mcp_total": len(mcp_names),
            "cli_leaf_total": len(cli_leaves),
            "mapping_table_rows": len(mapping),
            "mcp_both": len(mcp_both),
            "mcp_only": len(mcp_only),
            "cli_both": len(cli_both),
            "cli_only": len(cli_only),
            "cli_only_expected": sum(1 for x in cli_only if x["expected_cli_only"]),
            "cli_only_unexpected": sum(1 for x in cli_only if not x["expected_cli_only"]),
        },
        "mcp_only_tools": mcp_only,
        "cli_only_commands": cli_only,
        "note": "尊重 frozen spec §12(避免 T=M=D)。MCP_ONLY 预期为 [13]-[17] 增量域;CLI_ONLY 预期为本地宿主操作。unexpected 项需人工判断是否遗漏。",
    }
    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    s = result["summary"]
    print(f"写入: {OUT}")
    print(f"MCP {s['mcp_total']} / CLI叶子 {s['cli_leaf_total']} / 映射表 {s['mapping_table_rows']} 行")
    print(f"MCP: both={s['mcp_both']} only={s['mcp_only']}")
    print(f"CLI: both={s['cli_both']} only={s['cli_only']}(预期专属{s['cli_only_expected']} / 需判断{s['cli_only_unexpected']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
