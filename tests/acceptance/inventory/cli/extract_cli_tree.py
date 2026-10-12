"""TP-08C CLI 真实树/参数提取器。

提取方式（真实树，非静态解析）：
- 顶层命令集合取 `cli/categories.py`（`all_command_names()`，唯一权威分类源，
  由 tests/test_category_source.py 锁定 84 入口不重不漏）；
- 每个命令的 argparse 树通过**进程内捕获真实 parser 对象**取得：CLI 自身提供
  `_dispatch_subcommand_help()`（跳过 db 初始化的 help 快路径），本脚本在其执行
  期间挂钩 `ArgumentParser.format_help`，把被打印的那个 parser 连同全部
  action / subparser / 互斥组原样序列化。这比解析 help 文本多出
  default / choices / nargs / required / type / 互斥关系——help 文本不保证暴露。

副作用边界：help 快路径不初始化 db；本脚本直接调 `_dispatch_subcommand_help`，
绕过 `_run_subcommand_mode` 的 lazy auto-setup（避免写 AUTO_SETUP_MARKER）。
生产写守卫由 test_cli_tree.py 另测（提取前后生产目录快照不变）。

输出：docs/testing/manifests/cli/cli_tree.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
REPO_PARENT = REPO_ROOT.parent
if str(REPO_PARENT) not in sys.path:
    sys.path.insert(0, str(REPO_PARENT))

OUT = REPO_ROOT / "docs" / "testing" / "manifests" / "cli" / "cli_tree.json"

_captured: list[argparse.ArgumentParser] = []
_orig_format_help = argparse.ArgumentParser.format_help


def _hooked_format_help(self):
    _captured.append(self)
    return _orig_format_help(self)


# 安装挂钩：捕获被打印的那个 parser 对象（help 文本之外的 default/choices/required 真相）
argparse.ArgumentParser.format_help = _hooked_format_help


# ---------------- parser 序列化 ----------------

def _type_name(t) -> str:
    if t is None:
        return ""
    return getattr(t, "__name__", str(t))


def _action_to_dict(action) -> dict:
    d: dict = {
        "option_strings": list(action.option_strings),
        "dest": action.dest,
        "nargs": action.nargs,
        "required": bool(getattr(action, "required", False)),
        "default": action.default if _jsonable(action.default) else repr(action.default),
        "type": _type_name(getattr(action, "type", None)),
        "choices": list(action.choices) if action.choices else None,
        "help": (action.help or "")[:400],
        "metavar": action.metavar,
    }
    if isinstance(action, argparse._SubParsersAction):
        d["subcommands"] = list(action.choices.keys()) if action.choices else []
        d["is_subparsers"] = True
    return d


def _jsonable(v) -> bool:
    return isinstance(v, (str, int, float, bool, type(None), list, dict))


def _parser_to_dict(parser: argparse.ArgumentParser) -> dict:
    groups = []
    for g in parser._mutually_exclusive_groups:
        groups.append([list(a.option_strings) or a.dest for a in g._group_actions])
    subs = []
    if parser._subparsers is not None:
        for sub in parser._subparsers._group_actions:
            for name in (sub.choices or {}):
                subs.append({"name": name,
                             "help": (sub.choices[name].description or "")[:200]})
    return {
        "prog": parser.prog,
        "description": (parser.description or "")[:300],
        "usage": parser.format_usage().strip(),
        "arguments": [_action_to_dict(a) for a in parser._actions],
        "mutually_exclusive_groups": groups,
        "subparsers": subs,
    }


# ---------------- 提取 ----------------

def capture(cmd: str, sub_argv: list) -> tuple[dict | None, str | None]:
    """执行 CLI help 快路径并捕获被打印的 parser；返回 (parser_dict, error)。"""
    import callwarden.cli.main as main_mod

    saved_argv = sys.argv
    sys.argv = ["cw", cmd] + sub_argv
    _captured.clear()
    try:
        with contextlib.suppress(SystemExit):
            main_mod._dispatch_subcommand_help(cmd, sub_argv)
    except Exception as exc:  # noqa: BLE001 - 提取期错误必须记录而非中断
        return None, f"{type(exc).__name__}: {exc}"[:400]
    finally:
        sys.argv = saved_argv
    if not _captured:
        return None, "no parser captured (help not printed)"
    return _parser_to_dict(_captured[-1]), None


def subcommands_of(parser_dict: dict) -> list[str]:
    for a in parser_dict["arguments"]:
        if a.get("is_subparsers"):
            return list(a.get("subcommands") or [])
    return []


def capture_cw_py_entry(entry: str) -> tuple[dict | None, str | None]:
    """cw.py 层 standalone 入口（install/server/test/setup/daemon）的真实 --help。

    这些入口在 cw.py main() 内分发，不经过 cli.main._dispatch_subcommand，
    因此用真实子进程取 help 文本（`cw.py <entry> --help`）。
    """
    argv = [sys.executable, str(REPO_ROOT / "cw.py"), entry, "--help"]
    r = subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=90)
    text = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 and not text.strip():
        return None, f"rc={r.returncode} with empty output"
    return {"entry": entry, "rc": r.returncode, "help_text": text[:4000]}, None


def extract() -> dict:
    import callwarden.cli.categories as cats

    names = cats.all_command_names()
    # cw.py 层入口：不走 _dispatch_subcommand，单独按真实子进程提取
    cw_py_entries = {"install", "server", "test", "setup", "daemon"}
    dispatch_names = [n for n in names if n not in cw_py_entries]

    entries: dict = {}
    errors: list = []

    for cmd in dispatch_names:
        node: dict = {
            "category": cats.get_category_of(cmd).key,
            "top_parser": None,
            "nested": {},
            "error": None,
        }
        p, err = capture(cmd, ["-h"])
        if err:
            node["error"] = err
            errors.append({"command": cmd, "stage": "top", "error": err})
        else:
            node["top_parser"] = p
            for sub in subcommands_of(p):
                np_, nerr = capture(cmd, [sub, "-h"])
                if nerr:
                    node["nested"][sub] = {"error": nerr}
                    errors.append({"command": f"{cmd} {sub}", "stage": "nested", "error": nerr})
                else:
                    node["nested"][sub] = {"parser": np_}
        entries[cmd] = node

    for entry in sorted(cw_py_entries):
        p, err = capture_cw_py_entry(entry)
        if err:
            errors.append({"command": entry, "stage": "cw_py", "error": err})
        entries[entry] = {
            "category": cats.get_category_of(entry).key if entry in names else "cw_py_standalone",
            "top_parser": p,
            "nested": {},
            "error": err,
            "dispatch": "cw.py main() 内分发，不经 _dispatch_subcommand",
        }

    # standalone 入口（cw.py 之外的 pyproject scripts + 原生 Rust CLI）
    standalone = {}
    for script in ("cw-client", "cw-agent", "cw-daemon"):
        p, err = capture_standalone(script)
        standalone[script] = {"parser": p, "error": err}
        if err:
            errors.append({"command": script, "stage": "standalone", "error": err})

    rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                         capture_output=True, text=True).stdout.strip()
    return {
        "schema": "cli-tree/v1",
        "card": "TP-08C",
        "source_revision": rev,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "top_level_count": len(names),
        "nested_total": sum(len(e["nested"]) for e in entries.values()),
        "standalone_entries": standalone,
        "entries": entries,
        "errors": errors,
        "error_count": len(errors),
        "pending_entries": [
            {
                "entry": "native-rust-cli (cw.exe)",
                "reason": "TP-06 候选构建只含 --bin cw-daemon；原生 Rust CLI 未构建",
                "blocked_by": "profiles.json DP-04「原生 Rust CLI 是否在发布范围」= DECISION_REQUIRED",
                "policy": "TASK_BREAKDOWN：若纳入发布，TP-08C 必须按独立命令树分卡，"
                          "不能把 Python CLI ownership 无限扩展；未决前不冒充已提取",
                "cargo": "cargo build --release --manifest-path rust_ext/Cargo.toml "
                         "--no-default-features --bin cw",
            }
        ],
        "extraction_method": {
            "top_level_source": "cli/categories.py::all_command_names()（唯一权威分类源，"
                                "tests/test_category_source.py 锁定 84 入口不重不漏）",
            "parser_capture": "进程内挂钩 argparse.ArgumentParser.format_help，"
                              "经 CLI 自带 help 快路径 _dispatch_subcommand_help 捕获真实 parser",
            "cw_py_entries": "install/server/test/setup/daemon 经真实子进程 cw.py <entry> --help",
            "skips_db_init": "help 快路径不初始化 db；绕过 _run_subcommand_mode 的 lazy auto-setup",
        },
    }


def capture_standalone(script: str) -> tuple[dict | None, str | None]:
    """standalone 入口（cw-client/cw-agent/cw-daemon）的真实 --help。"""
    exe = REPO_ROOT / "cli" / f"{script.replace('cw-', '')}.py"
    if not exe.is_file():
        return None, f"entry file missing: {exe}"
    r = subprocess.run([sys.executable, str(exe), "--help"],
                       cwd=REPO_ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    if r.returncode != 0:
        return None, f"rc={r.returncode} err={r.stderr[:200]}"
    return {"help_text": r.stdout[:4000], "rc": 0}, None


def main() -> int:
    data = extract()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes((json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    print(f"top_level={data['top_level_count']} nested={data['nested_total']} "
          f"errors={data['error_count']}")
    print(f"written: {OUT} ({OUT.stat().st_size} bytes)")
    if data["errors"]:
        for e in data["errors"][:10]:
            print("  ERR", e["command"], "->", e["error"][:120])
    return 1 if data["error_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
