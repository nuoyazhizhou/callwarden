#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""覆盖测试 manifest 生成器（full-cli-mcp-coverage-test step 1）。

真相源（全部 import，不手抄）：
- CLI 顶层: cli.categories.COMMAND_CATEGORIES（84 顶层 / 21 分类）
- CLI 叶级: 运行时 `python cw.py <cmd> --help`，从 argparse usage 块的
  choices 组 {a,b,c} 动态提取（叶级真相 = 运行时 argparse，非手抄）
- MCP:      server.tools._categories.TOOL_CATEGORIES（分类归属）
            + scripts.gen_route_matrix.build_matrix()（rpc_method/op_class/backend）

输出: outputs/coverage_manifest.json
条目 schema（kind 区分）:
  cli_top : {id, kind, category, name}
  cli_leaf: {id, kind, category, name, parent}
  mcp     : {id, kind, category, name, rpc_method, op_class,
             target_backend, current_backend}

生成时强制自校验（任一失败即 SystemExit 非零）:
- CLI 顶层无重复、每个顶层命令恰好归一类
- 叶级提取两次结果一致（防竞态/非确定性）
- MCP 双真相源集合相等（TOOL_CATEGORIES vs build_matrix，无遗漏/重复/多出）
- op_class ∈ OP_CLASSES

用法:
    python scripts/gen_coverage_manifest.py                 # 生成并打印摘要
    python scripts/gen_coverage_manifest.py --print-summary # 仅打印摘要不写文件
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
from typing import Dict, List, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from cli.categories import COMMAND_CATEGORIES  # noqa: E402
from gen_route_matrix import OP_CLASSES, build_matrix  # noqa: E402
from server.tools._categories import TOOL_CATEGORIES  # noqa: E402

CW_ENTRY = os.path.join(_REPO_ROOT, "cw.py")
HELP_TIMEOUT_SEC = 60


# ---------------------------------------------------------------------------
# CLI 侧
# ---------------------------------------------------------------------------

def collect_cli_tops() -> List[Tuple[str, str]]:
    """从 COMMAND_CATEGORIES 收集 (顶层命令名, 分类 key)，校验无重复归类。"""
    tops: List[Tuple[str, str]] = []
    for cat in COMMAND_CATEGORIES:
        for cmd in cat.commands:
            tops.append((cmd.name, cat.key))
    dup = [n for n, c in Counter(n for n, _ in tops).items() if c > 1]
    if dup:
        raise SystemExit(f"CLI 顶层命令重复归类: {dup}")
    return tops


def extract_cli_leaves(top_cmd: str) -> List[str]:
    """运行 `python cw.py <cmd> --help`，从 usage 块提取子命令 choices。

    argparse 有子命令时 usage 首段必含 `{a,b,c}` choices 组；无子命令时
    返回空列表。跨行 usage 块先拼接再匹配，防折行拆散 choices token。
    """
    proc = subprocess.run(
        [sys.executable, CW_ENTRY, top_cmd, "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=HELP_TIMEOUT_SEC, stdin=subprocess.DEVNULL, cwd=_REPO_ROOT,
    )
    out = proc.stdout or ""
    # 标准 argparse 输出 "usage:"；部分 standalone 命令（install/server/test）
    # 用自定义中文 help，前缀为 "用法:"
    usage_prefixes = ("usage:", "用法:")
    if not any(p in out for p in usage_prefixes):
        raise RuntimeError(
            f"cw {top_cmd} --help 未产出 usage（rc={proc.returncode}）: "
            f"{(proc.stderr or out)[:200]}"
        )
    usage_lines: List[str] = []
    in_usage = False
    for ln in out.splitlines():
        if ln.startswith(usage_prefixes):
            in_usage = True
            usage_lines.append(ln)
            continue
        if in_usage:
            if not ln.strip():
                break
            usage_lines.append(ln)
    usage_blob = " ".join(usage_lines)
    m = re.search(r"\{([^{}]+)\}", usage_blob)
    if not m:
        return []
    choices = [c.strip() for c in m.group(1).split(",") if c.strip()]
    if "-h" in choices or "--help" in choices:
        raise RuntimeError(f"cw {top_cmd}: choices 组异常含 help 项: {choices}")
    return choices


def build_cli_entries() -> Tuple[List[dict], Dict[str, List[str]], Dict[str, str]]:
    """构建 CLI 双层条目；叶级提取两次，结果必须一致。

    提取失败的顶层命令（如 cw test 无 argparse 直通入口）降级为无子命令，
    失败原因记入 extract_errors 透明输出，不阻塞整体生成。
    """
    tops = collect_cli_tops()

    def extract_all() -> Tuple[Dict[str, List[str]], Dict[str, str]]:
        leaves: Dict[str, List[str]] = {}
        errors: Dict[str, str] = {}
        for name, _ in tops:
            try:
                leaves[name] = extract_cli_leaves(name)
            except Exception as ex:  # noqa: BLE001 —— 单命令降级，不炸整体
                leaves[name] = []
                errors[name] = str(ex)[:200]
        return leaves, errors

    leaves_pass1, errors_pass1 = extract_all()
    leaves_pass2, errors_pass2 = extract_all()
    if leaves_pass1 != leaves_pass2 or errors_pass1 != errors_pass2:
        diff = {
            n: (leaves_pass1.get(n), leaves_pass2.get(n))
            for n in leaves_pass1
            if leaves_pass1.get(n) != leaves_pass2.get(n)
        }
        raise SystemExit(f"CLI 叶级两次提取结果不一致（非确定性）: {diff}")
    entries: List[dict] = []
    for name, cat_key in tops:
        entries.append({
            "id": f"cli:{name}", "kind": "cli_top",
            "category": cat_key, "name": name,
        })
        for leaf in leaves_pass1[name]:
            entries.append({
                "id": f"cli:{name}:{leaf}", "kind": "cli_leaf",
                "category": cat_key, "name": leaf, "parent": name,
            })
    return entries, leaves_pass1, errors_pass1


# ---------------------------------------------------------------------------
# MCP 侧
# ---------------------------------------------------------------------------

def build_mcp_entries() -> List[dict]:
    """构建 MCP 条目；双真相源集合必须相等。"""
    matrix = build_matrix()
    by_name = {t["name"]: t for t in matrix["tools"]}
    if len(by_name) != len(matrix["tools"]):
        raise SystemExit("build_matrix 内部工具名重复")

    cat_of: Dict[str, str] = {}
    for cat in TOOL_CATEGORIES:
        for name in cat.tools:
            if name in cat_of:
                raise SystemExit(f"MCP 工具重复分类: {name}")
            cat_of[name] = cat.key

    matrix_names = set(by_name)
    cat_names = set(cat_of)
    if matrix_names != cat_names:
        raise SystemExit(
            "MCP 双真相源集合不一致: "
            f"matrix-only={sorted(matrix_names - cat_names)} "
            f"cats-only={sorted(cat_names - matrix_names)}"
        )

    entries: List[dict] = []
    for name in sorted(cat_names):
        t = by_name[name]
        if t["op_class"] not in OP_CLASSES:
            raise SystemExit(f"{name}: 非法 op_class {t['op_class']!r}")
        entries.append({
            "id": f"mcp:{name}", "kind": "mcp",
            "category": cat_of[name], "name": name,
            "rpc_method": t["rpc_method"], "op_class": t["op_class"],
            "target_backend": t["target_backend"],
            "current_backend": t["current_backend"],
        })
    return entries


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def generate_manifest() -> dict:
    cli_entries, leaves, extract_errors = build_cli_entries()
    mcp_entries = build_mcp_entries()
    manifest = {
        "schema_version": "1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counts": {
            "cli_top": sum(1 for e in cli_entries if e["kind"] == "cli_top"),
            "cli_leaf": sum(1 for e in cli_entries if e["kind"] == "cli_leaf"),
            "mcp": len(mcp_entries),
        },
        "cli_leaves": leaves,
        "leaf_extract_errors": extract_errors,
        "entries": cli_entries + mcp_entries,
    }
    return manifest


def print_summary(manifest: dict) -> None:
    c = manifest["counts"]
    print(f"cli_top={c['cli_top']}  cli_leaf={c['cli_leaf']}  mcp={c['mcp']}")
    by_cat: Counter = Counter()
    for e in manifest["entries"]:
        by_cat[e["category"]] += 1
    for cat, n in sorted(by_cat.items()):
        print(f"  {cat}: {n}")


def main() -> int:
    ap = argparse.ArgumentParser(description="生成覆盖测试 manifest")
    ap.add_argument(
        "--output",
        default=os.path.join(_REPO_ROOT, "outputs", "coverage_manifest.json"),
        help="输出 JSON 路径（默认 outputs/coverage_manifest.json）",
    )
    ap.add_argument("--print-summary", action="store_true",
                    help="仅打印摘要，不写文件")
    args = ap.parse_args()

    manifest = generate_manifest()
    print_summary(manifest)
    if args.print_summary:
        return 0
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print(f"written: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
