#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T10 阶段2：核对每个 deprecated flag 的 subcommand 等价是否齐全。

从 cli/main.py 提取 _DEPRECATED_FLAG_MAPPING 和 _SUBCOMMANDS，核对映射表
第 3 列声明的 subcommand 顶层词是否真实存在于 _SUBCOMMANDS。
纯静态核对（import cli.main 读常量），不执行 CLI。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
_REPO_ROOT = r"C:\git_work\callwarden"
sys.path.insert(0, os.path.dirname(_REPO_ROOT))

from callwarden.cli import main as cm  # noqa: E402

mapping = cm._DEPRECATED_FLAG_MAPPING
subcommands = cm._SUBCOMMANDS

# 保留的新 subcommand 选项（不是独立顶层命令，是子命令的 flag/positional）
# 这些出现在 subcommand 目标串里属正常（如 "refresh --all" 的 --all）
KNOWN_SUB_OPTIONS = {
    "--all", "--watch", "--force", "--top", "--orphans", "--deepest",
    "--module-calls", "--detect-cycles", "--export-module-graph", "--heatmap",
    "--summary", "--semantic", "--embed", "--similar", "--test", "N",
    "[N]", "[KIND]", "[SINCE]", "[PATH]", "[FILTER]",
}


def top_word(subcmd_target: str) -> str:
    """取 subcommand 目标串的顶层命令词（第一个 token，去尖括号）。"""
    return subcmd_target.strip().split()[0]


print("=" * 72)
print(f"deprecated flag 总数: {len(mapping)}")
print(f"_SUBCOMMANDS 总数: {len(subcommands)}")
print("=" * 72)

ok, missing = [], []
for attr, (flag, subcmd_target) in mapping.items():
    tw = top_word(subcmd_target)
    # 顶层词可能是 subcommand，也可能是旧 flag 形式残留（如 subcmd_target 本身就是旧写法）
    present = tw in subcommands
    if present:
        ok.append((flag, subcmd_target, tw))
    else:
        missing.append((flag, subcmd_target, tw))

print(f"\n[OK] subcommand 顶层词在 _SUBCOMMANDS 中: {len(ok)}/{len(mapping)}")
print(f"\n[待确认] 顶层词不在 _SUBCOMMANDS（可能是子选项组合或需补）: {len(missing)}")
for flag, target, tw in missing:
    print(f"    {flag:28s} -> '{target}'  (顶层词 '{tw}')")

# 列出所有映射供人工核对
print("\n" + "=" * 72)
print("全量 flag -> subcommand 映射（供人工核对语义等价）:")
for attr, (flag, subcmd_target) in sorted(mapping.items()):
    tw = top_word(subcmd_target)
    mark = "OK" if tw in subcommands else "??"
    print(f"  [{mark}] {flag:28s} -> cw {subcmd_target}")
