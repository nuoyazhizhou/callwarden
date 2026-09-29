#!/usr/bin/env python3
"""build_cli_inventory.py —— 生成 CLI 命令实际清单真相源（只读审计产物）。

真相源分两层：
1. 顶层命令：cli.main._SUBCOMMANDS 集合 + 独立 flag 命令（install/setup/server 等由 cw.py 分发）。
2. 子动作 / 参数：对每个顶层命令运行 `python cw.py <cmd> --help`，
   从 argparse usage 行的 `{a,b,c}` choices 提取子动作，从 `--xxx` 提取长选项。

不修改任何源码，只运行 --help 并汇总。命令名为 ASCII，中文 help 文本乱码不影响提取。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_PKG_PARENT = os.path.dirname(_REPO_ROOT)  # callwarden 包的父目录
_PKG_NAME = os.path.basename(_REPO_ROOT)
sys.path.insert(0, _PKG_PARENT)

_OUT = os.path.join(_HERE, "audit_cli_inventory.json")
_PYTHON = os.environ.get("PYTHON", sys.executable)
_CW = os.path.join(_REPO_ROOT, "cw.py")

# cw.py 直接分发（不在 _SUBCOMMANDS 里）的独立顶层命令
_STANDALONE_TOP = ["install", "setup", "server", "test", "daemon"]

_CHOICES_RE = re.compile(r"\{([a-z0-9\-,]+)\}")
_LONGOPT_RE = re.compile(r"(--[a-z0-9][a-z0-9\-]*)")


def _run_help(argv):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [_PYTHON, _CW] + argv + ["--help"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=env,
        )
        return (proc.stdout or "") + "\n" + (proc.stderr or ""), proc.returncode
    except subprocess.TimeoutExpired:
        return "", -1


def _parse_help(text):
    """从 help 文本提取子动作 choices 和长选项。"""
    sub_actions = []
    # 只取 usage 行区块里的第一个 {..} choices（子动作集合）
    m = _CHOICES_RE.search(text)
    if m:
        sub_actions = [x for x in m.group(1).split(",") if x]
    long_opts = sorted(set(_LONGOPT_RE.findall(text)))
    return sub_actions, long_opts


def main():
    import importlib
    main_mod = importlib.import_module(f"{_PKG_NAME}.cli.main")
    _SUBCOMMANDS = main_mod._SUBCOMMANDS

    top_cmds = sorted(set(_SUBCOMMANDS) | set(_STANDALONE_TOP))

    inventory = {
        "source": "cli.main._SUBCOMMANDS + standalone (cw.py dispatch) + `cw <cmd> --help` usage choices",
        "top_command_count": len(top_cmds),
        "subcommands_set_size": len(_SUBCOMMANDS),
        "standalone_top": _STANDALONE_TOP,
        "commands": {},
    }

    total_leaves = 0
    help_failures = []
    for cmd in top_cmds:
        text, rc = _run_help([cmd])
        sub_actions, long_opts = _parse_help(text)
        if rc != 0 and not sub_actions and not long_opts:
            help_failures.append({"cmd": cmd, "rc": rc})
        entry = {
            "help_rc": rc,
            "sub_actions": sub_actions,
            "long_opts": long_opts,
        }
        inventory["commands"][cmd] = entry
        # leaf 计数：有子动作则按子动作数，否则该命令本身算 1 个 leaf
        total_leaves += len(sub_actions) if sub_actions else 1

    inventory["total_leaf_commands"] = total_leaves
    inventory["help_failures"] = help_failures

    with open(_OUT, "w", encoding="utf-8") as fh:
        json.dump(inventory, fh, ensure_ascii=False, indent=2)

    print(f"CLI inventory 写入: {_OUT}")
    print(f"  顶层命令数 = {len(top_cmds)}")
    print(f"  叶子命令(子动作)总数 = {total_leaves}")
    print(f"  help 失败命令 = {help_failures}")
    # 抽样校验：task / gc
    print(f"  task 子动作 = {inventory['commands'].get('task', {}).get('sub_actions')}")
    print(f"  gc 子动作 = {inventory['commands'].get('gc', {}).get('sub_actions')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
