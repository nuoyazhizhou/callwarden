#!/usr/bin/env python3
"""extract_cli_params.py —— 提取 234 个 CLI 叶子命令的完整参数(positional + optional)。

用于 T1 全量测试基建。遍历 audit_cli_inventory.json 的叶子命令,对每个跑
`cw <leaf> --help`,从 argparse 输出解析:
- positional 参数(必填,按顺序)
- optional 参数(--xxx,是否带值)

结果写 cli_full_params.json。server 命令特殊处理(--help 会挂起,跳过)。
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_PY = os.environ.get("PYTHON", sys.executable)
_CW = os.path.join(_REPO, "cw.py")
_INV = os.path.join(_HERE, "audit_cli_inventory.json")
_OUT = os.path.join(_HERE, "cli_full_params.json")

# server --help 会挂起(启动 MCP);其余 standalone 正常
SKIP_HELP = {"server"}

_POS_HDR = re.compile(r"^positional arguments:", re.I)
_OPT_HDR = re.compile(r"^options:|^optional arguments:", re.I)
# usage 行里 positional(不带方括号、不以 - 开头、非 {choices})
_OPT_LINE = re.compile(r"^\s+(-\S+)")


def run_help(argv):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"; env["PYTHONIOENCODING"] = "utf-8"
    env["CW_DAEMON_TRANSPORT"] = "http"
    try:
        p = subprocess.run([_PY, "-u", _CW] + argv + ["--help"], cwd=_REPO,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=25, env=env, stdin=subprocess.DEVNULL)
        return (p.stdout or "") + "\n" + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return ""


def parse_help(text):
    """解析 argparse help:返回 positionals(list) + options(list of dict)。"""
    lines = text.splitlines()
    positionals = []
    options = []
    section = None  # 'pos' | 'opt'
    for line in lines:
        if _POS_HDR.match(line):
            section = "pos"; continue
        if _OPT_HDR.match(line):
            section = "opt"; continue
        if section == "pos":
            m = re.match(r"^\s{2,}(\{[^}]+\}|[a-z_][a-z0-9_]*)\b", line)
            if m and not line.strip().startswith("-"):
                tok = m.group(1)
                # {a,b,c} choices → 记为 choice positional
                if tok.startswith("{"):
                    positionals.append({"name": "action", "choices": tok.strip("{}").split(",")})
                else:
                    positionals.append({"name": tok})
        elif section == "opt":
            m = _OPT_LINE.match(line)
            if m:
                opt = m.group(1)
                if opt in ("-h",):
                    continue
                # 是否带值:usage/详情里 --xxx VALUE 形式
                rest = line.strip()[len(opt):].strip()
                takes_value = bool(re.match(r"^[A-Z<\[]", rest)) or bool(re.match(r"^\s+[A-Z]", rest))
                options.append({"opt": opt, "takes_value": takes_value})
    return positionals, options


def main():
    inv = json.load(open(_INV, encoding="utf-8"))
    cmds = inv["commands"]
    leaves = []
    for top in sorted(cmds):
        subs = cmds[top]["sub_actions"]
        if subs:
            for s in subs:
                leaves.append((top, s))
        else:
            leaves.append((top, ""))

    out = {}
    skipped = []
    for top, sub in leaves:
        leaf = f"{top} {sub}".strip()
        if top in SKIP_HELP:
            skipped.append(leaf); continue
        argv = [top] + ([sub] if sub else [])
        text = run_help(argv)
        if not text.strip():
            out[leaf] = {"error": "help_empty_or_timeout"}
            continue
        pos, opts = parse_help(text)
        out[leaf] = {"positionals": pos, "options": opts}

    result = {"total_leaves": len(leaves), "extracted": len(out),
              "skipped": skipped, "commands": out}
    json.dump(result, open(_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"提取 {len(out)} 个 CLI 叶子参数 -> {_OUT}", flush=True)
    print(f"skipped(server): {skipped}", flush=True)
    # 抽样
    for k in ("task report", "gc archive-inspect", "search"):
        if k in out:
            print(f"  {k}: {out[k]}", flush=True)


if __name__ == "__main__":
    main()
