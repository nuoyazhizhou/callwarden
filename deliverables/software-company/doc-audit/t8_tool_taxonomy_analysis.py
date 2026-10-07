#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T8 工具/命令分类梳理分析。

从 audit_cli_inventory.json + audit_mcp_inventory.json 自动分析：
1. 分类覆盖：CLI 功能域聚类 + MCP 模块分类
2. CLI<->MCP 覆盖对比：只在一侧的能力（潜在遗漏/重复）
3. 命名易混淆：编辑距离近的命令/工具名对
4. 参数易混淆：近似的 flag/参数名

纯分析，不改任何代码。输出 JSON + Markdown 片段。
"""
from __future__ import annotations

import json
import os
from itertools import combinations

_HERE = os.path.dirname(os.path.abspath(__file__))
CLI_INV = os.path.join(_HERE, "audit_cli_inventory.json")
MCP_INV = os.path.join(_HERE, "audit_mcp_inventory.json")


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def norm(name: str) -> str:
    """归一化：kebab/snake -> 统一下划线小写，去常见修饰。"""
    return name.replace("-", "_").replace(".", "_").lower()


def main():
    cli = json.load(open(CLI_INV, encoding="utf-8"))
    mcp = json.load(open(MCP_INV, encoding="utf-8"))

    cli_cmds = cli["commands"]
    mcp_tools = {t["name"]: t for t in mcp["tools"]}

    # ---- 1. CLI 叶子命令全集（top + top/sub）----
    cli_leaves = []
    for top, info in cli_cmds.items():
        subs = info.get("sub_actions", [])
        if subs:
            for s in subs:
                cli_leaves.append(f"{top} {s}")
        else:
            cli_leaves.append(top)

    # ---- 2. CLI 功能域聚类（按语义分组，人工定义域 + 自动归类）----
    # 域定义：前缀/关键词 -> 域名
    domains = {
        "符号与图谱查询": ["search", "symbol", "callers", "callees", "call-chain", "topo",
                     "query", "get", "impact", "who", "ownership-map"],
        "代码度量与健康": ["metrics", "complexity", "coupling", "coupled-fns", "fn-metrics",
                     "largest-fns", "comment-coverage", "uncommented", "stats", "status",
                     "health-report", "dashboard", "function-issues", "issues"],
        "缺陷与安全扫描": ["defect", "semgrep", "guardrail", "vuln-blast", "review"],
        "演化与历史": ["evolution", "hotspot", "churn", "symbol-history", "git"],
        "克隆检测": ["clone"],
        "构建与刷新": ["refresh", "graph", "build-context", "fts", "toolchain"],
        "任务编排": ["task", "lease", "assignment", "collab", "experiment", "check-gate",
                 "rule", "identity", "rollback"],
        "依赖分析": ["dependency", "test-impact", "tests", "coverage"],
        "GC 与运维": ["gc", "daemon", "workspace"],
        "文件与检索": ["file", "grep", "map", "brief"],
        "安装与配置": ["install", "install-agent", "install-hook", "setup", "config",
                  "doctor", "server", "test"],
        "审计": ["audit"],
    }
    cli_domain_map = {}
    for top in cli_cmds:
        placed = None
        for dom, kws in domains.items():
            if top in kws:
                placed = dom
                break
        cli_domain_map.setdefault(placed or "未分类", []).append(top)

    # ---- 3. CLI<->MCP 覆盖对比 ----
    # 归一化双方名字集合做模糊对应
    cli_norm = {}
    for leaf in cli_leaves:
        cli_norm[norm(leaf.replace(" ", "_"))] = leaf
    mcp_norm = {norm(n): n for n in mcp_tools}

    # MCP 工具是否有近似 CLI 命令（归一后相等或子串）
    mcp_only = []  # MCP 有但 CLI 无近似
    both = []
    cli_norm_keys = list(cli_norm.keys())
    for mn, orig in mcp_norm.items():
        hit = None
        if mn in cli_norm:
            hit = cli_norm[mn]
        else:
            # 子串/包含匹配（mcp get_callers <-> cli callers）
            core = mn.replace("get_", "").replace("query_", "")
            for ck in cli_norm_keys:
                ckc = ck.replace("get_", "").replace("query_", "")
                if core and (core == ckc or core in ckc or ckc in core):
                    hit = cli_norm[ck]
                    break
        if hit:
            both.append((orig, hit))
        else:
            mcp_only.append(orig)

    # CLI 命令是否有近似 MCP 工具
    cli_only = []
    mcp_norm_keys = list(mcp_norm.keys())
    for cn, orig in cli_norm.items():
        hit = None
        if cn in mcp_norm:
            hit = True
        else:
            core = cn.replace("get_", "").replace("query_", "")
            for mk in mcp_norm_keys:
                mkc = mk.replace("get_", "").replace("query_", "")
                if core and (core == mkc or core in mkc or mkc in core):
                    hit = True
                    break
        if not hit:
            cli_only.append(orig)

    # ---- 4. 命名易混淆：编辑距离 ----
    # 4a. CLI top 命令两两
    cli_tops = list(cli_cmds.keys())
    cli_confuse = []
    for a, b in combinations(cli_tops, 2):
        d = levenshtein(a, b)
        if 0 < d <= 2 and min(len(a), len(b)) >= 4:
            cli_confuse.append((a, b, d))

    # 4b. MCP 工具两两（量大，只报距离<=2 且长度>=5）
    mcp_names = list(mcp_tools.keys())
    mcp_confuse = []
    for a, b in combinations(mcp_names, 2):
        if abs(len(a) - len(b)) > 2:
            continue
        d = levenshtein(a, b)
        if 0 < d <= 2 and min(len(a), len(b)) >= 5:
            mcp_confuse.append((a, b, d))

    # 4c. 单复数/近义命令对（语义易混，人工关注）
    semantic_pairs = []
    allnames = cli_tops + [l.split()[-1] for l in cli_leaves] + mcp_names
    seen = set()
    for a, b in combinations(sorted(set(allnames)), 2):
        if (a, b) in seen:
            continue
        # 单复数：a+s==b 或词干相同
        if a + "s" == b or b + "s" == a or a + "es" == b or b + "es" == a:
            semantic_pairs.append((a, b, "单复数"))

    # ---- 5. 参数名易混淆（跨命令收集所有 long_opts）----
    all_opts = {}
    for top, info in cli_cmds.items():
        for o in info.get("long_opts", []):
            all_opts.setdefault(o, []).append(top)
    opt_confuse = []
    opt_names = [o for o in all_opts if o != "--help"]
    for a, b in combinations(sorted(set(opt_names)), 2):
        d = levenshtein(a, b)
        if 0 < d <= 2 and min(len(a), len(b)) >= 5:
            opt_confuse.append((a, b, d, all_opts[a], all_opts[b]))

    report = {
        "cli_top_count": len(cli_tops),
        "cli_leaf_count": len(cli_leaves),
        "mcp_tool_count": len(mcp_tools),
        "mcp_by_module": mcp["by_module"],
        "cli_domains": {k: sorted(v) for k, v in cli_domain_map.items()},
        "coverage": {
            "both_count": len(both),
            "mcp_only_count": len(mcp_only),
            "cli_only_count": len(cli_only),
            "mcp_only": sorted(mcp_only),
            "cli_only": sorted(cli_only),
        },
        "naming_confusion": {
            "cli_top_pairs": sorted(cli_confuse, key=lambda x: x[2]),
            "mcp_tool_pairs": sorted(mcp_confuse, key=lambda x: x[2]),
            "semantic_singular_plural": sorted(set(semantic_pairs)),
        },
        "param_confusion": sorted(opt_confuse, key=lambda x: x[2]),
    }
    out = os.path.join(_HERE, "t8_taxonomy_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 控制台摘要
    print("=" * 72)
    print(f"CLI: {len(cli_tops)} top / {len(cli_leaves)} 叶子命令")
    print(f"MCP: {len(mcp_tools)} 工具 / {len(mcp['by_module'])} 模块")
    print("=" * 72)
    print(f"\n[覆盖] 两侧都有(近似): {len(both)}")
    print(f"[覆盖] 仅 MCP(无 CLI 近似): {len(mcp_only)}")
    print(f"[覆盖] 仅 CLI(无 MCP 近似): {len(cli_only)}")
    print(f"\n[命名易混] CLI top 命令对(距离<=2): {len(cli_confuse)}")
    for a, b, d in sorted(cli_confuse, key=lambda x: x[2]):
        print(f"    {a} <-> {b} (距离 {d})")
    print(f"\n[命名易混] 单复数对: {len(set(semantic_pairs))}")
    for a, b, _ in sorted(set(semantic_pairs)):
        print(f"    {a} <-> {b}")
    print(f"\n[命名易混] MCP 工具对(距离<=2): {len(mcp_confuse)}")
    for a, b, d in sorted(mcp_confuse, key=lambda x: x[2])[:40]:
        print(f"    {a} <-> {b} (距离 {d})")
    print(f"\n[参数易混] flag 对(距离<=2): {len(opt_confuse)}")
    for a, b, d, ta, tb in sorted(opt_confuse, key=lambda x: x[2]):
        print(f"    {a} <-> {b} (距离 {d}) | {a}@{ta} vs {b}@{tb}")
    print(f"\n结果写入: {out}")


if __name__ == "__main__":
    main()
