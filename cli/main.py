"""
cli/main.py
===========

代码知识图谱数据库命令行接口。

提供完整的 CLI 参数定义和命令处理逻辑，支持：
- 数据库初始化与文件监控
- 符号查询、调用关系分析
- 拓扑排序、影响面分析
- 注释管理与覆盖率统计
- 缺陷检测与多语言静态分析
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from ..db import CodeGraphDB
from ..config import detect_project_root, get_default_workspace_name, atomic_write_file
from ..server.watcher import FileWatcher
from ..i18n import t, set_language, get_arg_help, get_msg, get_error, DEFAULT_LANG
from .console import cprint


# ====================================================================
# 代码守护者架构子命令（四大支柱）
# ====================================================================

# 子命令关键字集合
_SUBCOMMANDS = {"guardrail", "impact", "review", "evolution", "hotspot", "churn", "defect",
                "task", "vuln-blast", "symbol-history", "check-gate", "test-impact",
                "gc", "doctor", "install-agent"}


def _run_subcommand_mode():
    """子命令模式入口：初始化 db 并调度代码守护者架构子命令

    优化：当子命令参数中含 --help/-h 时，跳过 db 初始化直接 print 帮助。
    这避免了在 MCP Server 占用 db 锁时 cw task --help 卡死的问题。
    """
    # 使用系统检测到的默认语言（CALLWARDEN_LANG / LANG / LC_ALL / 系统语言）
    # 用户可通过环境变量 CALLWARDEN_LANG=en_US 切换为英文
    set_language(DEFAULT_LANG)

    # 检测子命令是否请求帮助（避免初始化 db 触发锁等待）
    sub_argv = sys.argv[2:] if len(sys.argv) > 2 else []
    wants_help = any(a in ("-h", "--help") for a in sub_argv)
    if wants_help:
        _dispatch_subcommand_help(sys.argv[1], sub_argv)
        return

    # 自动检测工作区根目录
    cwd = os.getcwd()
    detected = detect_project_root(cwd)
    workspace_root = detected if detected else None

    # 初始化数据库
    db = CodeGraphDB(workspace_root=workspace_root) if workspace_root else CodeGraphDB()

    try:
        # 自动注册工作区（与 --flag 模式行为一致）
        if workspace_root:
            ws_name = get_default_workspace_name(workspace_root)
            existing = None
            for ws in db.list_workspaces():
                if ws["root_path"] == workspace_root:
                    existing = ws
                    break
            if not existing:
                ws_id = db.register_workspace(ws_name, workspace_root)
                db.set_active_workspace(ws_id)
            elif not existing.get("is_active"):
                db.set_active_workspace(existing["id"])

        # 调度子命令
        _dispatch_subcommand(sub_argv, db)
    finally:
        db.close()


def _dispatch_subcommand_help(cmd: str, sub_argv: list):
    """子命令帮助模式：跳过 db 初始化，直接构造 argparse 并 print help

    Args:
        cmd: 子命令关键字（如 "task"）
        sub_argv: 子命令参数（含 -h/--help）
    """
    # 复用各 _handle_* 内部的 argparse 定义，但只触发 --help 行为
    # 通过构造一个空 args 强制 argparse 走 help 分支
    try:
        # 借用对应 _handle_* 函数的 parser 构造逻辑
        # 但因为各 _handle_* 的 parser 是函数内局部变量，这里用通用方式：
        # 调用一次 _dispatch_subcommand 但传 None 作为 db，让 argparse 自己处理 -h
        # argparse 遇到 -h 会调用 sys.exit(0)，不会触达 db 访问代码
        _dispatch_subcommand(sub_argv, db=None)
    except SystemExit:
        # argparse 处理 -h/--help 时会 sys.exit(0)
        pass


def _dispatch_subcommand(argv, db):
    """调度代码守护者架构子命令

    Args:
        argv: 子命令参数（不含子命令关键字本身，即 sys.argv[2:]）
        db: CodeGraphDB 实例

    Returns:
        True 表示已处理，False 表示不是有效子命令
    """
    # 子命令关键字（_run_subcommand_mode 调用时已确认在 _SUBCOMMANDS 中）
    cmd = sys.argv[1]

    try:
        if cmd == "guardrail":
            return _handle_guardrail(argv, db)
        elif cmd == "impact":
            return _handle_impact(argv, db)
        elif cmd == "review":
            return _handle_review(argv, db)
        elif cmd == "evolution":
            return _handle_evolution(argv, db)
        elif cmd == "hotspot":
            return _handle_hotspot(argv, db)
        elif cmd == "churn":
            return _handle_churn(argv, db)
        elif cmd == "defect":
            return _handle_defect(argv, db)
        elif cmd == "task":
            return _handle_task(argv, db)
        elif cmd == "vuln-blast":
            return _handle_vuln_blast(argv, db)
        elif cmd == "symbol-history":
            return _handle_symbol_history(argv, db)
        elif cmd == "check-gate":
            return _handle_check_gate(argv, db)
        elif cmd == "test-impact":
            return _handle_test_impact(argv, db)
        elif cmd == "gc":
            return _handle_gc(argv, db)
        elif cmd == "doctor":
            return _handle_doctor(argv, db)
        elif cmd == "install-agent":
            return _handle_install_agent(argv, db)
    except Exception as e:
        cprint(t("cli.messages.subcommand_fail", cmd=cmd, error=e), "red")
        return True

    return False


def _handle_install_agent(args, db):
    """生成 Agent 集成包（MCP + skills/rules + hooks）"""
    parser = argparse.ArgumentParser(
        prog="cw install-agent",
        description=t("cli.messages.install_agent_desc", default="Generate Call Warden integration files for Codex/Claude/Cursor"),
    )
    parser.add_argument(
        "agent",
        choices=["codex", "claude", "cursor", "all"],
        help=t("cli.messages.install_agent_arg_agent", default="Target Agent"),
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help=t("cli.messages.install_agent_arg_output_dir", default="Output directory, defaults to .callwarden/agent-integrations"),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=t("cli.messages.install_agent_arg_force", default="Overwrite existing integration files"),
    )
    opts = parser.parse_args(args)

    root = db.workspace_root
    out_root = opts.output_dir or os.path.join(root, ".callwarden", "agent-integrations")
    out_root = os.path.abspath(out_root)
    agents = ["codex", "claude", "cursor"] if opts.agent == "all" else [opts.agent]

    created = []
    for agent in agents:
        created.extend(_write_agent_integration(root, out_root, agent, opts.force))

    cprint(t("cli.messages.install_agent_title", default="=== Agent Integration Generated ==="), "cyan", bold=True)
    print(t("cli.messages.install_agent_root", default="  Root: {root}", root=root))
    print(t("cli.messages.install_agent_output", default="  Output: {path}", path=out_root))
    print(t("cli.messages.install_agent_agents", default="  Agents: {agents}", agents=', '.join(agents)))
    print()
    for path in created:
        print(f"  - {path}")
    print()
    cprint(t(
        "cli.messages.install_agent_next",
        default="Next: enable hooks/MCP/plugin using the generated README for the target Agent.",
    ), "green")
    return True


def _write_if_needed(path: str, content: str, force: bool, created: list) -> None:
    """写入文件，默认不覆盖已有内容"""
    if os.path.exists(path) and not force:
        created.append(path + t("cli.messages.install_agent_exists_skipped", default=" (exists, skipped)"))
        return
    atomic_write_file(path, content)
    created.append(path)


def _write_agent_integration(root: str, out_root: str, agent: str, force: bool) -> list:
    """写入单个 Agent 的集成模板"""
    created = []
    base = os.path.join(out_root, agent)
    os.makedirs(base, exist_ok=True)

    hook_dir = os.path.join(base, "hooks")
    os.makedirs(hook_dir, exist_ok=True)
    hook_script = os.path.join(hook_dir, "callwarden_hook.py")
    _write_if_needed(hook_script, _agent_hook_script(), force, created)

    if agent == "codex":
        plugin_root = os.path.join(base, "callwarden-plugin")
        os.makedirs(os.path.join(plugin_root, ".codex-plugin"), exist_ok=True)
        os.makedirs(os.path.join(plugin_root, "skills", "callwarden-workflow"), exist_ok=True)
        os.makedirs(os.path.join(plugin_root, "hooks"), exist_ok=True)
        _write_if_needed(
            os.path.join(plugin_root, ".codex-plugin", "plugin.json"),
            json.dumps({
                "name": "callwarden",
                "version": "0.1.0",
                "description": t(
                    "cli.messages.install_agent_plugin_description",
                    default="Call Warden Agent workflow, MCP tools, and lifecycle hooks.",
                ),
                "skills": "./skills/",
                "mcpServers": "./.mcp.json",
                "hooks": "./hooks/hooks.json",
                "interface": {
                    "displayName": "Call Warden",
                    "shortDescription": t(
                        "cli.messages.install_agent_plugin_short_description",
                        default="Code graph workflow and safe patch tools for coding agents.",
                    ),
                    "capabilities": ["Read", "Write"],
                },
            }, ensure_ascii=False, indent=2) + "\n",
            force,
            created,
        )
        _write_if_needed(
            os.path.join(plugin_root, ".mcp.json"),
            json.dumps({
                "mcpServers": {
                    "callwarden": {
                        "command": "python",
                        "args": [os.path.join(root, "cw.py"), "server"],
                    }
                }
            }, ensure_ascii=False, indent=2) + "\n",
            force,
            created,
        )
        _write_if_needed(
            os.path.join(plugin_root, "skills", "callwarden-workflow", "SKILL.md"),
            _callwarden_skill_md(),
            force,
            created,
        )
        _write_if_needed(
            os.path.join(plugin_root, "hooks", "hooks.json"),
            _codex_hooks_json(hook_script),
            force,
            created,
        )

    elif agent == "claude":
        _write_if_needed(
            os.path.join(base, "settings.snippet.json"),
            _claude_settings_json(hook_script),
            force,
            created,
        )
        _write_if_needed(
            os.path.join(base, "CALLWARDEN.md"),
            _callwarden_skill_md(),
            force,
            created,
        )

    elif agent == "cursor":
        _write_if_needed(
            os.path.join(base, "callwarden.mdc"),
            _cursor_rule_mdc(),
            force,
            created,
        )
        _write_if_needed(
            os.path.join(base, "mcp.json"),
            json.dumps({
                "mcpServers": {
                    "callwarden": {
                        "command": "python",
                        "args": [os.path.join(root, "cw.py"), "server"],
                    }
                }
            }, ensure_ascii=False, indent=2) + "\n",
            force,
            created,
        )

    _write_if_needed(os.path.join(base, "README.md"), _agent_readme(agent), force, created)
    return created


def _agent_hook_script() -> str:
    return t("cli.messages.install_agent_hook_script", default=r'''#!/usr/bin/env python3
"""Call Warden Agent hook.

Reads Agent hook JSON from stdin, blocks destructive shell commands,
and refreshes the code graph after file edits when possible.
"""
import json
import os
import subprocess
import sys


BLOCKED_COMMANDS = [
    "git reset --hard",
    "git checkout .",
    "git clean -fd",
    "git clean -fx",
    "rm -rf",
    "Remove-Item -Recurse",
]


def read_payload():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def find_command(payload):
    tool_input = payload.get("tool_input") or payload.get("input") or {}
    return (
        tool_input.get("command")
        or tool_input.get("cmd")
        or payload.get("command")
        or ""
    )


def find_file(payload):
    tool_input = payload.get("tool_input") or payload.get("input") or {}
    return (
        tool_input.get("file_path")
        or tool_input.get("path")
        or payload.get("file_path")
        or ""
    )


def block(message):
    print(json.dumps({"decision": "block", "reason": message}, ensure_ascii=False))
    return 2


def main():
    payload = read_payload()
    command = find_command(payload)
    for blocked in BLOCKED_COMMANDS:
        if blocked.lower() in command.lower():
            return block(f"Call Warden blocked destructive command: {blocked}")

    file_path = find_file(payload)
    event = str(payload.get("event") or payload.get("hook_event_name") or "")
    if file_path and ("post" in event.lower() or payload.get("tool_name") in ("Edit", "Write", "apply_patch")):
        try:
            subprocess.run(
                ["python", "cw.py", "--refresh", file_path],
                cwd=os.getcwd(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except Exception:
            pass

    print(json.dumps({"decision": "pass"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''')


def _callwarden_skill_md() -> str:
    return t("cli.messages.install_agent_skill_md", default="""---
name: callwarden-workflow
description: Use Call Warden for codebase-aware tasks in large or risky repositories. Trigger for bugfixes, refactors, code review, annotation, broad edits, impact analysis, or any task touching more than one file or more than a few steps.
---

Use the Call Warden MCP server as the primary workflow entrypoint.

1. Prefer `work_next_job` over manual file search when a task_id exists.
2. Prefer `file_symbol_content` / `symbol_context` style tools over whole-file reads.
3. Prefer `propose_symbol_patch` or `propose_range_patch` over whole-file rewrites.
4. For broad tasks, create/split tasks first, then repeatedly call `work_next_job`.
5. After editing, report the step with `task_report_step` and include changed files.
6. Do not run destructive git commands such as `git reset --hard`, `git checkout .`, or `git clean -fd`.
""")


def _codex_hooks_json(hook_script: str) -> str:
    return json.dumps({
        "PreToolUse": [
            {
                "matcher": "Bash|Shell|LocalShell",
                "hooks": [{"type": "command", "command": f"python \"{hook_script}\""}],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Edit|Write|apply_patch",
                "hooks": [{"type": "command", "command": f"python \"{hook_script}\""}],
            }
        ],
    }, ensure_ascii=False, indent=2) + "\n"


def _claude_settings_json(hook_script: str) -> str:
    return json.dumps({
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash|Shell",
                    "hooks": [{"type": "command", "command": f"python \"{hook_script}\""}],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "Edit|Write",
                    "hooks": [{"type": "command", "command": f"python \"{hook_script}\""}],
                }
            ],
        }
    }, ensure_ascii=False, indent=2) + "\n"


def _cursor_rule_mdc() -> str:
    return t("cli.messages.install_agent_cursor_rule", default="""---
description: Call Warden workflow for codebase-aware Agent tasks
alwaysApply: true
---

When the Call Warden MCP server is available, prefer it for broad or risky coding tasks.

- Use `work_next_job` for task-driven work instead of manually deciding the next file.
- Use symbol/range patch tools instead of full-file rewrites.
- Use Call Warden impact and guardrail tools before DB/API/config changes.
- Avoid destructive git cleanup commands unless the user explicitly requests them.
""")


def _agent_readme(agent: str) -> str:
    return t("cli.messages.install_agent_readme", default="""# Call Warden {agent} integration

This directory contains generated integration files for {agent}.

Recommended workflow:

1. Enable the MCP server config that points to `python cw.py server`.
2. Enable the generated skill/rule instructions.
3. Enable hooks where the target Agent supports them.
4. For large tasks, ask the Agent to start from Call Warden `work_next_job`.

The hook script blocks destructive git cleanup commands and refreshes the graph after edits when the Agent exposes edited file paths in hook payloads.
""", agent=agent)


# --------------------------------------------------------------------
# 安全护栏子命令
# --------------------------------------------------------------------

def _handle_guardrail(args, db):
    """处理 guardrail 子命令（安全护栏）"""
    parser = argparse.ArgumentParser(
        prog="cw guardrail",
        description=t("cli.messages.guardrail_desc", default="Production safety guardrails"),
    )
    sub = parser.add_subparsers(dest="action", required=True)

    scan_p = sub.add_parser("scan", help=t("cli.messages.guardrail_scan_help", default="Scan guardrail violations"))
    scan_p.add_argument("--file", default="", help=t("cli.messages.guardrail_file_help", default="Filter by file path prefix"))
    scan_p.add_argument("--category", default="",
                        help=t("cli.messages.guardrail_category_help", default="Filter by category (db_safety/api_compat/incident)"))

    rules_p = sub.add_parser("rules", help=t("cli.messages.guardrail_rules_help", default="List guardrail rules"))
    rules_p.add_argument("--category", default="", help=t("cli.messages.guardrail_category_filter_help", default="Filter by category"))

    opts = parser.parse_args(args)

    if opts.action == "scan":
        findings = db.scan_guardrails(file_filter=opts.file)
        # findings 没有 category 字段，通过 rule_id 映射到类别后过滤
        if opts.category:
            rules = db.guardrail_list_rules()
            rule_cat = {r["rule_id"]: r["category"] for r in rules}
            findings = [f for f in findings
                        if rule_cat.get(f.get("rule_id", ""), "") == opts.category]

        cprint(t("cli.messages.guardrail_scan_title"), "cyan", bold=True)
        file_info = f"（{t('cli.args.file')}: {opts.file}）" if opts.file else ""
        cat_info = f"（{t('cli.args.category')}: {opts.category}）" if opts.category else ""
        print(t("cli.messages.guardrail_scan_found", count=len(findings),
                file_info=file_info, cat_info=cat_info))
        print()

        if not findings:
            cprint(t("cli.messages.guardrail_scan_no_violation"), "green")
        else:
            sev_icon = {"block": "[!]", "warn": "[~]", "info": "[i]"}
            sev_color = {"block": "red", "warn": "yellow", "info": "cyan"}
            for i, f in enumerate(findings, 1):
                sev = f.get("severity", "warn")
                icon = sev_icon.get(sev, "[?]")
                color = sev_color.get(sev, "white")
                cprint(t("cli.messages.guardrail_scan_item",
                         idx=i, icon=icon, severity=sev,
                         rule_id=f.get('rule_id', '')), color)
                print(t("cli.messages.guardrail_scan_file",
                        path=f.get('file_path', '')))
                print(t("cli.messages.guardrail_scan_message",
                        msg=f.get('message', '')))
                if f.get("symbol_hash"):
                    print(t("cli.messages.guardrail_scan_symbol",
                            hash=f['symbol_hash'][:12]))
                print()
        print()

    elif opts.action == "rules":
        rules = db.guardrail_list_rules(category_filter=opts.category)
        cprint(t("cli.messages.guardrail_rules_title"), "cyan", bold=True)
        cat_info = f"（{t('cli.args.category')}: {opts.category}）" if opts.category else ""
        print(t("cli.messages.guardrail_rules_count", count=len(rules), cat_info=cat_info))
        print()

        if not rules:
            cprint(t("cli.messages.guardrail_rules_none"), "dim")
        else:
            sev_icon = {"block": "[!]", "warn": "[~]", "info": "[i]"}
            for i, r in enumerate(rules, 1):
                sev = r.get("severity", "warn")
                icon = sev_icon.get(sev, "[?]")
                builtin = t("cli.messages.guardrail_builtin") if r.get("is_builtin") else t("cli.messages.guardrail_custom")
                print(t("cli.messages.guardrail_rules_item",
                        idx=i, icon=icon, rule_id=r['rule_id'],
                        category=r.get('category', ''), builtin=builtin))
                print(t("cli.messages.guardrail_rules_severity",
                        severity=sev, action=r.get('action', '')))
                if r.get("description"):
                    print(t("cli.messages.guardrail_rules_desc", desc=r['description']))
                print()
        print()

    return True


# --------------------------------------------------------------------
# 变更影响子命令
# --------------------------------------------------------------------

def _handle_impact(args, db):
    """处理 impact 子命令（变更影响半径）"""
    parser = argparse.ArgumentParser(
        prog="cw impact",
        description=t("cli.messages.impact_desc", default="Change impact radius analysis"),
    )
    parser.add_argument("symbol_hash", help=t("cli.messages.impact_symbol_hash_help", default="Source symbol hash"))
    parser.add_argument("--depth", type=int, default=3, help=t("cli.messages.impact_depth_help", default="Maximum BFS traversal depth (default: 3)"))

    opts = parser.parse_args(args)
    result = db.blast_radius(opts.symbol_hash, depth=opts.depth)

    cprint(t("cli.messages.impact_title"), "cyan", bold=True)

    if not result.get("source_symbol"):
        cprint(t("cli.messages.impact_symbol_not_found",
                 symbol_hash=opts.symbol_hash), "red")
        return True

    print(t("cli.messages.impact_source_symbol",
            symbol=result['source_symbol']))
    print(t("cli.messages.impact_source_hash",
            hash=result['source_hash'][:12]))
    print(t("cli.messages.impact_depth", depth=result['depth']))
    print(t("cli.messages.impact_total", total=result['total_impacted']))
    print()

    # 跨层影响分布
    by_layer = result.get("by_layer", {})
    print(t("cli.messages.impact_by_layer_title"))
    print(t("cli.messages.impact_layer_code", count=by_layer.get('code', 0)))
    print(t("cli.messages.impact_layer_db", count=by_layer.get('db', 0)))
    print(t("cli.messages.impact_layer_api", count=by_layer.get('api', 0)))
    print(t("cli.messages.impact_layer_config", count=by_layer.get('config', 0)))
    print()

    # 各层符号详情
    layers = result.get("layers", [])
    for layer in layers:
        depth = layer["depth"]
        symbols = layer["symbols"]
        label = (t("cli.messages.impact_layer_label_source")
                 if depth == 0
                 else t("cli.messages.impact_layer_label_depth", depth=depth))
        print(t("cli.messages.impact_layer_symbols",
                label=label, count=len(symbols)))
        for sym in symbols[:15]:
            qn = sym.get("qualified_name", "")
            kind = sym.get("kind", "")
            fp = sym.get("file_path", "")
            print(t("cli.messages.impact_symbol_item", kind=kind, name=qn))
            if fp:
                print(t("cli.messages.impact_symbol_file", file=fp))
        if len(symbols) > 15:
            print(t("cli.messages.impact_more_symbols",
                    count=len(symbols) - 15))
        print()

    return True


def _handle_review(args, db):
    """处理 review 子命令（审查就绪报告）"""
    parser = argparse.ArgumentParser(prog="cw review", description=t("cli.messages.review_title"))
    parser.add_argument("symbol_hash", help=t("cli_review_arg_symbol_hash", default="Source symbol hash"))

    opts = parser.parse_args(args)
    report = db.review_readiness_report(opts.symbol_hash)

    cprint(t("cli.messages.review_title"), "cyan", bold=True)

    scope = report.get("impact_scope", "low")
    scope_color = {"high": "red", "medium": "yellow", "low": "green"}.get(scope, "white")
    scope_i18n = {
        "high": t("cli.messages.review_scope_high"),
        "medium": t("cli.messages.review_scope_medium"),
        "low": t("cli.messages.review_scope_low"),
    }.get(scope, scope)
    print(t("cli.messages.review_risk_level"), end="")
    cprint(scope_i18n, scope_color, bold=True)
    print(t("cli.messages.review_impact_scope", count=report.get('total_impacted', 0)))
    print()

    # 跨层影响
    by_layer = report.get("by_layer", {})
    print(t("cli.messages.review_cross_layer_title"))
    print(t("cli.messages.review_layer_counts",
          code=by_layer.get('code', 0), db=by_layer.get('db', 0),
          api=by_layer.get('api', 0), config=by_layer.get('config', 0)))
    print()

    # 必测项
    must_test = report.get("must_test", [])
    print(t("cli.messages.review_must_test_title", count=len(must_test)))
    if must_test:
        for i, m in enumerate(must_test, 1):
            print(t("cli.messages.review_must_test_item",
                    idx=i, name=m.get('qualified_name', '')))
            if m.get("file_path"):
                print(t("cli.messages.review_must_test_file", path=m['file_path']))
    else:
        cprint(t("cli.messages.review_none"), "dim")
    print()

    # 人工审查点
    review_points = report.get("review_points", [])
    print(t("cli.messages.review_points_title", count=len(review_points)))
    if review_points:
        for r in review_points:
            layer = r.get("layer", "")
            target = r.get("target", "")
            msg = r.get("message", "")
            icon = "[!]" if layer in ("db", "api") else "[~]"
            cprint(t("cli.messages.review_point_item",
                     icon=icon, layer=layer, target=target), "yellow")
            print(t("cli.messages.review_point_msg", msg=msg))
    else:
        cprint(t("cli.messages.review_none"), "dim")
    print()

    # 覆盖率（可选）
    cov = report.get("coverage")
    if cov:
        print(t("cli.messages.review_coverage_title"))
        print(t("cli.messages.review_coverage_fn", name=cov.get('qualified_name', '')))
        print(t("cli.messages.review_coverage_pct",
                pct=cov.get('coverage_pct', 0),
                covered=cov.get('covered_lines', 0),
                total=cov.get('tracked_lines', 0)))
        print()

    return True


# --------------------------------------------------------------------
# 演化智能子命令
# --------------------------------------------------------------------

def _handle_evolution(args, db):
    """处理 evolution 子命令（函数变更频率）"""
    parser = argparse.ArgumentParser(prog="cw evolution", description=t("cli.messages.evolution_title"))
    parser.add_argument("qualified_name", help=t("cli_evolution_arg_qualified_name", default="Function qualified name"))
    parser.add_argument("--window", default="", help=t("cli_evolution_arg_window", default="Time window (for example 30d/90d/1y)"))

    opts = parser.parse_args(args)
    result = db.function_change_frequency(opts.qualified_name, time_window=opts.window)

    cprint(t("cli.messages.evolution_title"), "cyan", bold=True)
    print(t("cli.messages.evolution_function", name=result.get('qualified_name', '')))

    window_info = (t("cli.messages.evolution_window", window=opts.window)
                   if opts.window
                   else t("cli.messages.evolution_all_history"))
    print(t("cli.messages.evolution_change_count",
            count=result.get('change_count', 0), window=window_info))

    if result.get("first_seen"):
        first = time.strftime("%Y-%m-%d %H:%M", time.localtime(result["first_seen"]))
        print(t("cli.messages.evolution_first_seen", time=first))
    if result.get("last_changed"):
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(result["last_changed"]))
        print(t("cli.messages.evolution_last_changed", time=last))

    avg_interval = result.get("avg_interval", 0)
    if avg_interval > 0:
        print(t("cli.messages.evolution_avg_interval", days=f"{avg_interval / 86400:.1f}"))
    print()

    # 变更者
    changers = result.get("changers", [])
    print(t("cli.messages.evolution_changers_title", count=len(changers)))
    if changers:
        for c in changers[:10]:
            print(t("cli.messages.evolution_changer_item", name=c))
        if len(changers) > 10:
            print(t("cli.messages.evolution_more_changers", count=len(changers) - 10))
    else:
        cprint(t("cli.messages.review_none"), "dim")
    print()

    # 变更时间线
    timeline = result.get("timeline", [])
    print(t("cli.messages.evolution_timeline_title", count=len(timeline)))
    if timeline:
        for i, t in enumerate(timeline[:20], 1):
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(t.get("timestamp", 0)))
            author = (t.get("author", "") or "unknown")[:12]
            msg = (t.get("message", "") or "")[:50]
            commit = (t.get("commit_hash", "") or "")[:8]
            print(t("cli.messages.evolution_timeline_item",
                    idx=i, time=ts, author=author, commit=commit, msg=msg))
        if len(timeline) > 20:
            print(t("cli.messages.evolution_more_timeline", count=len(timeline) - 20))
    else:
        cprint(t("cli.messages.evolution_no_timeline"), "dim")
    print()

    # 变更分布
    dist = result.get("distribution", {})
    if dist:
        print(t("cli.messages.evolution_distribution_title"))
        for period, counts in dist.items():
            print(t("cli.messages.evolution_distribution_item", period=period, counts=counts))
        print()

    return True


def _handle_hotspot(args, db):
    """处理 hotspot 子命令（热点函数排名）"""
    parser = argparse.ArgumentParser(prog="cw hotspot", description=t("cli.messages.hotspot_title"))
    parser.add_argument("--module", default="", help=t("cli_hotspot_arg_module", default="Filter by module path prefix"))
    parser.add_argument("--limit", type=int, default=20, help=t("cli_hotspot_arg_limit", default="Number of items to show (default: 20)"))

    opts = parser.parse_args(args)
    results = db.hotspot_evolution(module_filter=opts.module)

    cprint(t("cli.messages.hotspot_title"), "cyan", bold=True)
    mod_info = (t("cli.messages.hotspot_module_info", module=opts.module)
                if opts.module else "")
    print(t("cli.messages.hotspot_count", count=len(results), mod_info=mod_info))
    print()

    if not results:
        cprint(t("cli.messages.hotspot_no_data"), "dim")
    else:
        shown = results[:opts.limit]
        # 表头：用 i18n key 取列名，避免硬编码
        hash_h = t("cli_hotspot_col_hash", default="#")
        score_h = t("cli_hotspot_col_score", default="热点分")
        changes_h = t("cli_hotspot_col_changes", default="变更")
        defects_h = t("cli_hotspot_col_defects", default="缺陷")
        complexity_h = t("cli_hotspot_col_complexity", default="复杂度")
        label_h = t("cli_hotspot_col_label", default="标签")
        fn_h = t("cli_hotspot_col_function", default="函数名")
        print(t("cli.messages.hotspot_header",
                hash=hash_h, score=score_h, changes=changes_h, defects=defects_h,
                complexity=complexity_h, label=label_h))
        print(t("cli.messages.hotspot_separator",
                sep1="-"*3, sep2="-"*6, sep3="-"*4, sep4="-"*4, sep5="-"*6, sep6="-"*8, sep7="-"*50))

        for i, item in enumerate(shown, 1):
            score = item.get("hotspot_score", 0)
            changes = item.get("change_count", 0)
            defects = item.get("defect_count", 0)
            complexity = item.get("complexity", 0)
            label = item.get("label") or ""
            qn = item.get("qualified_name", "")[:60]

            line = t("cli.messages.hotspot_item",
                     idx=i, score=score, changes=changes, defects=defects,
                     complexity=complexity, label=label, name=qn)
            # 标签翻译
            label_high = t("cli_hotspot_label_persistent", default="持续热点")
            label_emerging = t("cli_hotspot_label_emerging", default="新兴热点")
            if label == label_high:
                cprint(line, "red")
            elif label == label_emerging:
                cprint(line, "yellow")
            else:
                print(line)

        if len(results) > opts.limit:
            print(t("cli.messages.hotspot_more", count=len(results) - opts.limit))
    print()

    return True


def _handle_churn(args, db):
    """处理 churn 子命令（代码流失分析）"""
    parser = argparse.ArgumentParser(prog="cw churn", description=t("cli.messages.churn_title"))
    parser.add_argument("--module", default="", help=t("cli_churn_arg_module", default="Filter by module path prefix"))
    parser.add_argument("--window", default="90d", help=t("cli_churn_arg_window", default="Time window (default: 90d)"))

    opts = parser.parse_args(args)
    result = db.churn_analysis(module_filter=opts.module, time_window=opts.window)

    cprint(t("cli.messages.churn_title"), "cyan", bold=True)
    mod_info = (t("cli.messages.hotspot_module_info", module=opts.module)
                if opts.module else "")
    print(t("cli.messages.churn_window", window=opts.window, mod_info=mod_info))
    print()

    print(t("cli.messages.churn_changed_files", count=result.get('changed_files', 0)))
    print(t("cli.messages.churn_total_lines", count=result.get('total_lines_current', 0)))
    print(t("cli.messages.churn_total_churned", count=result.get('total_churned_lines', 0)))
    churn_rate = result.get("churn_rate", 0)
    print(t("cli.messages.churn_rate", rate=f"{churn_rate * 100:.2f}"))
    print()

    # 高频变更文件
    top_files = result.get("top_churned_files", [])
    print(t("cli.messages.churn_top_files_title"))
    if top_files:
        for i, f in enumerate(top_files, 1):
            path = (f.get("rel_path", "") or "")[:60]
            changes = f.get("change_count", 0)
            churned = f.get("churned_lines", 0)
            print(t("cli.messages.churn_top_file_item", idx=i, path=path))
            print(t("cli.messages.churn_top_file_detail", changes=changes, churned=churned))
    else:
        cprint(t("cli.messages.review_none"), "dim")
    print()

    # 流失趋势
    trend = result.get("trend", [])
    print(t("cli.messages.churn_trend_title", count=len(trend)))
    if trend:
        for t in trend[:20]:
            date = t.get("date", "")
            lines = t.get("churned_lines", 0)
            bar_len = min(int(lines / 10), 30)
            bar = "█" * bar_len
            print(t("cli.messages.churn_trend_item", date=date, bar=bar, lines=lines))
        if len(trend) > 20:
            print(t("cli.messages.churn_more_trend", count=len(trend) - 20))
    else:
        cprint(t("cli.messages.churn_no_trend"), "dim")
    print()

    return True


# --------------------------------------------------------------------
# 缺陷知识库子命令
# --------------------------------------------------------------------

def _handle_defect(args, db):
    """处理 defect 子命令（缺陷知识库）"""
    parser = argparse.ArgumentParser(prog="cw defect", description=t("cli_defect_desc", default="Defect knowledge base"))
    sub = parser.add_subparsers(dest="action", required=True)

    search_p = sub.add_parser("search", help=t("cli_defect_search_desc", default="Search defect patterns"))
    search_p.add_argument("--category", default="", help=t("cli_defect_arg_category", default="Category filter (prefix match)"))
    search_p.add_argument("--severity", default="",
                          help=t("cli_defect_arg_severity", default="Severity filter (error/warning/info)"))
    search_p.add_argument("--limit", type=int, default=20, help=t("cli_defect_arg_limit", default="Number of items to show"))

    suggest_p = sub.add_parser("suggest", help=t("cli_defect_suggest_desc", default="Suggest fixes"))
    suggest_p.add_argument("symbol_hash", help=t("cli_defect_arg_symbol_hash", default="Symbol content hash"))
    suggest_p.add_argument("--finding", type=int, default=0, help=t("cli_defect_arg_finding", default="Specific finding ID"))

    learn_p = sub.add_parser("learn", help=t("cli_defect_learn_desc", default="Learn defect patterns from a fix commit"))
    learn_p.add_argument("commit_hash", help=t("cli_defect_arg_commit_hash", default="Fix commit hash"))

    sub.add_parser("stats", help=t("cli_defect_stats_desc", default="Defect knowledge base statistics"))
    sub.add_parser("build", help=t("cli_defect_build_desc", default="Build defect knowledge base"))

    opts = parser.parse_args(args)
    sev_icon_map = {"error": "[!]", "warning": "[~]", "info": "[i]"}

    if opts.action == "search":
        patterns = db.defect_pattern_search(
            category=opts.category, severity_filter=opts.severity
        )

        cprint(t("cli.messages.defect_search_title"), "cyan", bold=True)
        filter_parts = []
        if opts.category:
            filter_parts.append(t("cli.messages.defect_filter_label", cat=opts.category))
        if opts.severity:
            filter_parts.append(t("cli.messages.defect_severity_label", sev=opts.severity))
        filter_str = " | ".join(filter_parts) if filter_parts else t("cli.messages.defect_filter_all")
        print(t("cli.messages.defect_filter_str", filter=filter_str))
        print(t("cli.messages.defect_search_count", count=len(patterns)))
        print()

        if not patterns:
            cprint(t("cli.messages.defect_search_empty"), "dim")
        else:
            shown = patterns[:opts.limit]
            for i, p in enumerate(shown, 1):
                pid = p.get("pattern_id", "")
                cat = p.get("category", "")
                sev = p.get("severity", "")
                desc = (p.get("description", "") or "")[:60]
                cnt = p.get("case_count", 0)
                icon = sev_icon_map.get(sev, "[?]")
                print(t("cli.messages.defect_search_item",
                        idx=i, icon=icon, pid=pid, cat=cat, cnt=cnt))
                if desc:
                    print(t("cli.messages.defect_search_desc", desc=desc))
            if len(patterns) > opts.limit:
                print(t("cli.messages.defect_search_more", count=len(patterns) - opts.limit))
        print()

    elif opts.action == "suggest":
        result = db.suggest_fix(opts.symbol_hash, finding_id=opts.finding)

        cprint(t("cli.messages.defect_suggest_title"), "cyan", bold=True)
        print(t("cli.messages.defect_suggest_symbol", hash=opts.symbol_hash[:12]))
        if opts.finding:
            print(t("cli.messages.defect_suggest_finding", id=opts.finding))
        print()

        pid = result.get("pattern_id", "")
        if pid:
            print(t("cli.messages.defect_suggest_pattern", pid=pid))
        else:
            cprint(t("cli.messages.defect_suggest_no_pattern"), "yellow")

        eff = result.get("effectiveness_score", 0)
        print(t("cli.messages.defect_suggest_score", score=f"{eff:.2f}"))
        print()

        fix = result.get("fix_template", "")
        if fix:
            cprint(t("cli.messages.defect_suggest_fix_title"), "green")
            for line in fix.split("\n")[:20]:
                print(t("cli.messages.defect_suggest_fix_line", line=line))
            if len(fix.split("\n")) > 20:
                print("    ...")
        else:
            cprint(t("cli.messages.defect_suggest_no_fix"), "yellow")
        print()

        similar = result.get("similar_fixes", [])
        print(t("cli.messages.defect_suggest_similar_title", count=len(similar)))
        if similar:
            for i, s in enumerate(similar, 1):
                eff_s = s.get("effectiveness", 0)
                print(t("cli.messages.defect_suggest_similar_item",
                        idx=i, eff=f"{eff_s:.2f}", pid=s.get('pattern_id', '')))
        else:
            cprint(t("cli.messages.review_none"), "dim")
        print()

    elif opts.action == "learn":
        cprint(t("cli.messages.defect_learn_title", hash=opts.commit_hash), "cyan")
        result = db.learn_defect_from_fix(opts.commit_hash)

        print()
        print(t("cli.messages.defect_learn_patterns", count=result.get('learned_patterns', 0)))
        print(t("cli.messages.defect_learn_fixes", count=result.get('learned_fixes', 0)))

        details = result.get("details", [])
        if details:
            print()
            print(t("cli.messages.defect_learn_details_title", count=len(details)))
            for i, d in enumerate(details[:20], 1):
                print(t("cli.messages.defect_learn_detail_item", idx=i, detail=d))
            if len(details) > 20:
                print(t("cli.messages.defect_learn_more_details", count=len(details) - 20))
        print()

    elif opts.action == "stats":
        stats = db.defect_stats()

        cprint(t("cli.messages.defect_stats_title"), "cyan", bold=True)
        print(t("cli.messages.defect_stats_patterns", count=stats.get('total_patterns', 0)))
        print(t("cli.messages.defect_stats_fixes", count=stats.get('total_fixes', 0)))
        print(t("cli.messages.defect_stats_effectiveness",
                score=f"{stats.get('avg_effectiveness', 0):.2f}"))
        print()

        by_cat = stats.get("by_category", {})
        if by_cat:
            print(t("cli.messages.defect_stats_by_cat_title"))
            for cat, cnt in sorted(by_cat.items(), key=lambda x: -x[1]):
                print(t("cli.messages.defect_stats_by_cat_item", cat=cat, cnt=cnt))
            print()

        by_sev = stats.get("by_severity", {})
        if by_sev:
            print(t("cli.messages.defect_stats_by_sev_title"))
            for sev, cnt in sorted(by_sev.items(), key=lambda x: -x[1]):
                icon = sev_icon_map.get(sev, "[?]")
                print(t("cli.messages.defect_stats_by_sev_item", icon=icon, sev=sev, cnt=cnt))
            print()

        top = stats.get("top_defects", [])
        if top:
            print(t("cli.messages.defect_stats_top_title"))
            for i, d in enumerate(top, 1):
                pid = d.get("pattern_id", "")
                cat = d.get("category", "")
                cnt = d.get("case_count", 0)
                desc = (d.get("description", "") or "")[:50]
                print(t("cli.messages.defect_stats_top_item", idx=i, cat=cat, pid=pid, cnt=cnt))
                if desc:
                    print(t("cli.messages.defect_search_desc", desc=desc))
            print()
        else:
            cprint(t("cli.messages.defect_stats_empty"), "yellow")

    elif opts.action == "build":
        cprint(t("cli.messages.defect_build_start"), "cyan")
        result = db.build_defect_knowledge()

        print()
        cprint(t("cli.messages.defect_build_done"), "green")
        print(t("cli.messages.defect_build_patterns", count=result.get('patterns_built', 0)))
        print(t("cli.messages.defect_build_fixes", count=result.get('fixes_learned', 0)))

        cats = result.get("categories", {})
        if cats:
            print()
            print(t("cli.messages.defect_build_cat_title"))
            for cat, cnt in sorted(cats.items(), key=lambda x: -x[1]):
                print(t("cli.messages.defect_build_cat_item", cat=cat, cnt=cnt))
        print()

    return True


# --------------------------------------------------------------------
# 任务管理 / 漏洞爆炸半径 / 符号 Git 历史
# --------------------------------------------------------------------

def _handle_task(args, db):
    """处理 task 子命令（任务管理：create/next/report/rollback）"""
    parser = argparse.ArgumentParser(prog="cw task", description=t("cli_task_desc", default="Task management"))
    sub = parser.add_subparsers(dest="action", required=True)

    # create：创建任务和步骤
    create_p = sub.add_parser("create", help=t("cli_task_create_desc", default="Create task and steps"))
    create_p.add_argument("--title", required=True, help=t("cli_task_arg_title", default="Task title"))
    create_p.add_argument("--desc", default="", help=t("cli_task_arg_desc", default="Task description"))
    create_p.add_argument("--steps", default="",
                          help=t("cli_task_arg_steps", default='Step JSON array, for example [{"action":"annotate","target_file":"a.py"}]'))

    # next：领取下一个待执行步骤
    next_p = sub.add_parser("next", help=t("cli_task_next_desc", default="Claim current pending step"))
    next_p.add_argument("task_id", help=t("cli_task_arg_task_id", default="Task ID"))

    # report：回报步骤执行结果
    report_p = sub.add_parser("report", help=t("cli_task_report_desc", default="Report step result"))
    report_p.add_argument("task_id", help=t("cli_task_arg_task_id", default="Task ID"))
    report_p.add_argument("step_id", help=t("cli_task_arg_step_id", default="Step ID"))
    report_p.add_argument("--result", default="", help=t("cli_task_arg_result", default="Result description"))
    report_p.add_argument("--fail", action="store_true", help=t("cli_task_arg_fail", default="Mark as failed (default: success)"))

    # rollback：回滚变更
    rollback_p = sub.add_parser("rollback", help=t("cli_task_rollback_desc", default="Roll back changes"))
    rollback_p.add_argument("task_id", help=t("cli_task_arg_task_id", default="Task ID"))
    rollback_p.add_argument("step_id", help=t("cli_task_arg_step_id_rollback", default="Step ID (used as change_id to locate rollback scope)"))

    # findings：查看任务质量门禁发现
    findings_p = sub.add_parser(
        "findings", help=t("cli_task_findings_desc", default="List task quality findings")
    )
    findings_p.add_argument("task_id", help=t("cli_task_arg_task_id", default="Task ID"))
    findings_p.add_argument(
        "--status", default="open",
        help=t("cli_task_arg_status", default="Status filter (open/resolved/wontfix/all)")
    )
    findings_p.add_argument(
        "--severity", default="",
        help=t("cli_task_arg_severity", default="Severity filter (info/warn/error/block)")
    )

    # resolve-finding：解决或豁免质量门禁发现
    resolve_p = sub.add_parser(
        "resolve-finding",
        help=t("cli_task_resolve_finding_desc", default="Resolve a task quality finding")
    )
    resolve_p.add_argument("finding_id", type=int, help=t("cli_task_arg_finding_id", default="Finding ID"))
    resolve_p.add_argument(
        "--resolution", default="fixed",
        help=t("cli_task_arg_resolution", default="Resolution (fixed/wontfix/false_positive)")
    )
    resolve_p.add_argument(
        "--by", default="agent",
        help=t("cli_task_arg_by", default="Resolver (agent/human/system)")
    )

    # list：列出任务（支持 --blocked 过滤）
    list_p = sub.add_parser("list", help=t("cli_task_list_desc", default="List tasks"))
    list_p.add_argument(
        "--blocked", action="store_true",
        help=t("cli_task_arg_blocked", default="Only show tasks with blocking findings")
    )
    list_p.add_argument(
        "--limit", type=int, default=200,
        help=t("cli_task_arg_limit", default="Maximum number of tasks to list (default: 200)")
    )
    list_p.add_argument(
        "--status", default="",
        help=t("cli_task_arg_status_filter", default="Status filter (open/in_progress/review/applied/closed/reverted)")
    )
    list_p.add_argument(
        "--flat", action="store_true",
        help=t("cli_task_arg_flat", default="Flat list (no tree indentation)")
    )

    # show：查看任务详情（默认树形展示子任务）
    show_p = sub.add_parser("show", help=t("cli_task_show_desc", default="Show task details (tree mode by default)"))
    show_p.add_argument("task_id", help=t("cli_task_arg_task_id", default="Task ID"))
    show_p.add_argument(
        "--flat", action="store_true",
        help=t("cli_task_arg_flat_show", default="Flat mode (do not show subtasks recursively)")
    )

    opts = parser.parse_args(args)

    if opts.action == "create":
        # 解析步骤 JSON（若提供）
        steps = []
        if opts.steps:
            try:
                steps = json.loads(opts.steps)
                if not isinstance(steps, list):
                    cprint(t("cli.messages.task_steps_invalid"), "red")
                    return True
            except json.JSONDecodeError as e:
                cprint(t("cli.messages.task_steps_parse_error", error=e), "red")
                return True

        task_id = db.task_create(opts.title, opts.desc, steps, creator="agent")

        cprint(t("cli.messages.task_create_title"), "cyan", bold=True)
        print(t("cli.messages.task_id_label", id=task_id))
        print(t("cli.messages.task_title_label", title=opts.title))
        if opts.desc:
            print(t("cli.messages.task_desc_label", desc=opts.desc))
        print(t("cli.messages.task_steps_count", count=len(steps)))
        if steps:
            print()
            print(t("cli.messages.task_steps_list_title"))
            for i, s in enumerate(steps, 1):
                action = s.get("action", "")
                target = s.get("target_file", "") or s.get("target_symbol", "")
                print(t("cli.messages.task_step_item", idx=i, action=action, target=target))
        print()
        return True

    elif opts.action == "next":
        result = db.task_next_step(opts.task_id)

        cprint(t("cli.messages.task_next_title"), "cyan", bold=True)
        print(t("cli.messages.task_id_label", id=opts.task_id))
        if result is None:
            cprint(t("cli.messages.task_no_pending"), "yellow")
            return True

        print(t("cli.messages.task_step_id", id=result.get('step_id', '')))
        print(t("cli.messages.task_step_index", idx=result.get('step_index', 0)))
        print(t("cli.messages.task_action", action=result.get('action', '')))
        if result.get("target_file"):
            print(t("cli.messages.task_target_file", file=result['target_file']))
        if result.get("target_symbol"):
            print(t("cli.messages.task_target_symbol", symbol=result['target_symbol']))
        print(t("cli.messages.task_status", status=result.get('status', '')))
        print()

        # 检查项
        check_items = result.get("check_items", "")
        if check_items:
            print(t("cli.messages.task_check_items_title"))
            if isinstance(check_items, list):
                for ci in check_items:
                    print(t("cli.messages.task_check_item", item=ci))
            else:
                print(t("cli.messages.task_check_item_str", item=check_items))
            print()

        # 护栏阻断告警（block）
        alert = result.get("guardrail_alert")
        if alert:
            cprint(t("cli.messages.task_guardrail_alert_title"), "red", bold=True)
            print(t("cli.messages.task_guardrail_decision", decision=alert.get('decision', '')))
            print(t("cli.messages.task_guardrail_message", msg=alert.get('message', '')))
            findings = alert.get("findings", [])
            if findings:
                print(t("cli.messages.task_guardrail_findings_count", count=len(findings)))
            cprint(t("cli.messages.task_guardrail_resolve_hint"), "yellow")
            print()

        # 护栏警告（warn）
        warning = result.get("guardrail_warning")
        if warning:
            cprint(t("cli.messages.task_guardrail_warning_title"), "yellow")
            print(t("cli.messages.task_guardrail_message", msg=warning.get('message', '')))
            print()

        # F7: 结构化指令展示（Agent 必须遵循的操作约束）
        si = result.get("structured_instruction")
        if si:
            cprint(t("cli.messages.task_structured_instruction_title"), "cyan", bold=True)
            if si.get("read_targets"):
                print(t("cli.messages.task_si_read_targets"))
                for rt in si["read_targets"]:
                    print(t("cli.messages.task_si_read_target_item",
                            file=rt.get('file', '?'),
                            lines=rt.get('lines', '?'),
                            symbol=rt.get('symbol', '')))
            if si.get("constraints"):
                print(t("cli.messages.task_si_constraints"))
                for c in si["constraints"]:
                    print(t("cli.messages.task_si_constraint_item", constraint=c))
            if si.get("checks"):
                print(t("cli.messages.task_si_checks", checks=', '.join(si['checks'])))
            ctx = si.get("context", {})
            if ctx.get("callers"):
                callers_str = ", ".join(c.get("name", "") for c in ctx["callers"][:3])
                print(t("cli.messages.task_si_callers", callers=callers_str))
            if ctx.get("existing_summary"):
                print(t("cli.messages.task_si_existing_summary",
                        summary=ctx['existing_summary'][:60]))
            print()

        return True

    elif opts.action == "report":
        success = not opts.fail
        result = db.task_report_step(
            opts.task_id, opts.step_id, opts.result, success, None
        )

        cprint(t("cli.messages.task_report_title"), "cyan", bold=True)
        print(t("cli.messages.task_id_label", id=opts.task_id))
        print(t("cli.messages.task_step_id", id=opts.step_id))
        result_str = (t("cli.messages.task_result_success") if success
                      else t("cli.messages.task_result_fail"))
        print(t("cli.messages.task_report_result", result=result_str))
        if opts.result:
            print(t("cli.messages.task_result_desc", desc=opts.result))
        print()

        if result is None:
            cprint(t("cli.messages.task_no_more_steps"), "yellow")
        else:
            cprint(t("cli.messages.task_next_ready"), "green")
            print(t("cli.messages.task_next_step_id", id=result.get('step_id', '')))
            print(t("cli.messages.task_next_action", action=result.get('action', '')))
            if result.get("target_file"):
                print(t("cli.messages.task_next_target_file", file=result['target_file']))
        print()
        return True

    elif opts.action == "rollback":
        # 优先调用 task_rollback_step；方法不存在则回退到 task_rollback
        if hasattr(db, "task_rollback_step"):
            result = db.task_rollback_step(opts.task_id, opts.step_id)
        else:
            result = db.task_rollback(opts.task_id, opts.step_id)

        cprint(t("cli.messages.task_rollback_title"), "cyan", bold=True)
        print(t("cli.messages.task_id_label", id=opts.task_id))
        print(t("cli.messages.task_rollback_status", status=result.get('task_status', '')))
        rolled = result.get("rolled_back_changes", [])
        print(t("cli.messages.task_rollback_count", count=len(rolled)))
        print()

        if rolled:
            print(t("cli.messages.task_rollback_details_title"))
            for i, c in enumerate(rolled, 1):
                fp = c.get("file_path", "")
                restorable = c.get("restorable", False)
                icon = "[✓]" if restorable else "[✗]"
                print(t("cli.messages.task_rollback_item", idx=i, icon=icon, path=fp))
                if c.get("hash_before"):
                    print(t("cli.messages.task_rollback_hash", hash=c['hash_before'][:12]))

        note = result.get("note", "")
        if note:
            print()
            cprint(t("cli.messages.task_note", note=note), "yellow")
        print()
        return True

    elif opts.action == "findings":
        # 查询任务质量发现
        findings = db.get_task_quality_findings(
            opts.task_id, status=opts.status, severity=opts.severity
        )
        cprint(t("cli.messages.task_findings_title"), "cyan", bold=True)
        print(t("cli.messages.task_id_label", id=opts.task_id))
        print(t("cli.messages.task_findings_count", count=len(findings)))
        print()

        if not findings:
            cprint(t("cli.messages.task_no_findings"), "yellow")
            return True

        # 按严重度分组展示
        for f in findings:
            sev = f.get("severity", "warn")
            color = {"error": "red", "block": "red", "warn": "yellow", "info": "cyan"}.get(sev, "white")
            status = f.get("status", "open")
            icon = "[!]" if sev in ("error", "block") else ("[~]" if sev == "warn" else "[i]")
            cprint(
                t("cli.messages.task_finding_item",
                  icon=icon, id=f.get('id', 0), sev=sev, status=status,
                  ftype=f.get('finding_type', '')),
                color
            )
            print(t("cli.messages.task_finding_msg", msg=f.get('message', '')))
            if f.get("step_id"):
                print(t("cli.messages.task_finding_step", step=f['step_id']))
            print(t("cli.messages.task_finding_source", src=f.get('source', '')))
            print()
        return True

    elif opts.action == "resolve-finding":
        # 解决或豁免质量门禁发现
        result = db.resolve_task_quality_finding(
            opts.finding_id, resolution=opts.resolution, resolved_by=opts.by
        )
        cprint(t("cli.messages.task_resolve_finding_title"), "cyan", bold=True)
        if result.get("success"):
            cprint(
                t("cli.messages.task_resolve_finding_ok",
                  id=result.get('finding_id', 0),
                  status=result.get('status', '')),
                "green"
            )
            print(t("cli.messages.task_resolve_finding_resolution",
                    resolution=result.get('resolution', '')))
        else:
            cprint(
                t("cli.messages.task_resolve_finding_fail",
                  err=result.get('error', '')),
                "red"
            )
        print()
        return True

    elif opts.action == "list":
        # 列出任务（支持 --blocked 过滤 / 树形展示）
        # 统一调用 db.task_list()，与 --task-list 走同一份数据源
        try:
            status_filter = opts.status or None
            tasks = db.task_list(status_filter=status_filter, limit=opts.limit)
        except Exception:
            tasks = []

        cprint(t("cli.messages.task_panel_title"), "cyan", bold=True)
        if opts.blocked:
            cprint(t("cli.messages.task_panel_blocked_only"), "yellow")
        if opts.status:
            cprint(t("cli.messages.task_panel_status_filter", status=opts.status), "yellow")
        if not opts.flat:
            cprint(t("cli.messages.task_panel_tree_mode"), "yellow")
        print(t("cli.messages.task_panel_count", count=len(tasks)))
        print()

        # 构造任务 id → 子任务列表 的映射，便于树形遍历
        # db.task_list() 已按 (根优先, parent_id, sort_order, created_at) 排序
        children_map: dict = {}
        root_tasks: list = []
        for tk in tasks:
            parent_id = tk.get("parent_id") or ""
            if parent_id:
                children_map.setdefault(parent_id, []).append(tk)
            else:
                root_tasks.append(tk)

        # 递归打印树形结构
        def _print_task_tree(task_node, depth):
            tid = task_node.get("task_id") or task_node.get("id", "")
            title = task_node.get("title", "")
            status = task_node.get("status", "")
            # 若 --blocked，跳过无阻塞发现的任务（但仍递归其子任务）
            blocking = db.task_has_blocking_findings(tid) if hasattr(db, "task_has_blocking_findings") else False
            if opts.blocked and not blocking:
                # 检查子任务是否有阻塞发现，没有则整体跳过
                has_blocked_child = False
                stack = list(children_map.get(tid, []))
                while stack:
                    cur_node = stack.pop()
                    cur_tid = cur_node.get("task_id") or cur_node.get("id", "")
                    if db.task_has_blocking_findings(cur_tid) if hasattr(db, "task_has_blocking_findings") else False:
                        has_blocked_child = True
                        break
                    stack.extend(children_map.get(cur_tid, []))
                if not has_blocked_child:
                    return
            icon = "[!]" if blocking else "[ ]"
            color = "red" if blocking else "white"
            indent = "" if opts.flat else (t("cli.messages.task_list_indent") * depth)
            cprint(
                t("cli.messages.task_panel_item",
                  icon=icon, id=tid, status=status, title=title),
                color
            ) if not indent else cprint(
                t("cli.messages.task_panel_item_indented",
                  indent=indent, icon=icon, id=tid, status=status, title=title),
                color
            )
            # 递归打印子任务
            for child in children_map.get(tid, []):
                _print_task_tree(child, depth + 1)

        if opts.flat:
            # 扁平模式：直接按 db 返回顺序打印
            for tk in tasks:
                tid = tk.get("task_id") or tk.get("id", "")
                title = tk.get("title", "")
                status = tk.get("status", "")
                if opts.blocked:
                    if not (db.task_has_blocking_findings(tid) if hasattr(db, "task_has_blocking_findings") else False):
                        continue
                blocking = db.task_has_blocking_findings(tid) if hasattr(db, "task_has_blocking_findings") else False
                icon = "[!]" if blocking else "[ ]"
                color = "red" if blocking else "white"
                cprint(
                    t("cli.messages.task_panel_item",
                      icon=icon, id=tid, status=status, title=title),
                    color
                )
        else:
            # 树形模式：从根任务开始 DFS
            for root in root_tasks:
                _print_task_tree(root, 0)
            # 检查是否有"孤儿"任务（parent_id 不在当前结果集中）
            seen_ids = {tk.get("task_id") or tk.get("id", "") for tk in tasks}
            orphans = [
                tk for tk in tasks
                if (tk.get("parent_id") or "")
                and (tk.get("parent_id") not in seen_ids)
            ]
            for orphan in orphans:
                _print_task_tree(orphan, 0)
        print()
        return True

    elif opts.action == "show":
        # 查看任务详情（默认树形展示子任务，--flat 退回扁平）
        return _print_task_show(db, opts.task_id, flat=opts.flat)

    return True


def _print_task_show(db, task_id: str, flat: bool = False) -> bool:
    """打印任务详情，默认按树形递归展示子任务

    Args:
        db: CodeGraphDB 实例
        task_id: 任务 ID
        flat: True 时仅显示主任务（不递归子任务），False 时递归展示整棵树

    Returns:
        True 表示处理完成
    """
    if flat:
        # 扁平模式：使用 task_status 仅显示主任务
        detail = db.task_status(task_id)
        if not detail:
            print(t("cli.messages.task_show_not_found", id=task_id))
            return True
        _print_task_detail_single(detail, indent_depth=0)
        return True

    # 树形模式：使用 task_status_tree 递归展示
    tree = db.task_status_tree(task_id) if hasattr(db, "task_status_tree") else None
    if not tree:
        print(t("cli.messages.task_show_not_found", id=task_id))
        return True

    cprint(t("cli.messages.task_show_title"), "cyan", bold=True)
    print("-" * 50)
    _print_task_tree_node(tree, depth=0)
    print()
    return True


def _print_task_detail_single(detail: dict, indent_depth: int = 0):
    """打印单个任务详情（扁平模式，不递归子任务）"""
    indent = t("cli.messages.task_list_indent") * indent_depth
    print(t("cli.messages.task_show_id", id=detail['task_id']) if not indent else
          t("cli.messages.task_show_id_indented", indent=indent, id=detail['task_id']))
    print(t("cli.messages.task_show_title_label", title=detail['title']))
    print(t("cli.messages.task_show_status", status=detail['status']))
    if detail.get('description'):
        print(t("cli.messages.task_show_desc", desc=detail['description']))
    if detail.get('creator'):
        print(t("cli.messages.task_show_creator", creator=detail['creator']))
    created = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(detail['created_at'])) if detail.get('created_at') else '?'
    print(t("cli.messages.task_show_created", time=created))
    print()
    steps = detail.get('steps', [])
    print(t("cli.messages.task_show_steps", count=len(steps)))
    for s in steps:
        print(t("cli.messages.task_show_step", idx=s['step_index'], status=s['status'], action=s['action']))
        if s.get('target_file'):
            print(t("cli.messages.task_show_step_file", file=s['target_file']))
        if s.get('target_symbol'):
            print(t("cli.messages.task_show_step_symbol", symbol=s['target_symbol']))


def _print_task_tree_node(node: dict, depth: int = 0):
    """递归打印任务树节点（带缩进）

    Args:
        node: task_status_tree 返回的节点 dict
        depth: 当前深度（0 = 根任务）
    """
    indent = t("cli.messages.task_list_indent") * depth
    # 主任务详情
    if depth == 0:
        # 根任务用顶级格式（无缩进）
        print(t("cli.messages.task_show_id", id=node.get('task_id', '')))
        print(t("cli.messages.task_show_title_label", title=node.get('title', '')))
        print(t("cli.messages.task_show_status", status=node.get('status', '')))
        if node.get('description'):
            print(t("cli.messages.task_show_desc", desc=node['description']))
        if node.get('creator'):
            print(t("cli.messages.task_show_creator", creator=node['creator']))
        created = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(node['created_at'])) if node.get('created_at') else '?'
        print(t("cli.messages.task_show_created", time=created))
    else:
        # 子任务用缩进格式
        cprint(
            t("cli.messages.task_show_subtask_item",
              indent=indent, id=node.get('task_id', ''), status=node.get('status', ''),
              title=node.get('title', '')),
            "white"
        )

    # 进度
    progress = node.get('progress') or {}
    total = progress.get('total', 0)
    done = progress.get('done', 0)
    pct = progress.get('progress', 0)
    if total > 0:
        cprint(
            t("cli.messages.task_show_progress",
              indent=indent, done=done, total=total, pct=pct),
            "green"
        )

    # 自身步骤（仅根任务显示步骤明细，避免子任务过多噪音）
    if depth == 0:
        steps = node.get('steps', [])
        print(t("cli.messages.task_show_steps", count=len(steps)))
        for s in steps:
            print(t("cli.messages.task_show_step", idx=s['step_index'], status=s['status'], action=s['action']))
            if s.get('target_file'):
                print(t("cli.messages.task_show_step_file", file=s['target_file']))
            if s.get('target_symbol'):
                print(t("cli.messages.task_show_step_symbol", symbol=s['target_symbol']))

    # 递归子任务
    subtasks = node.get('subtasks', []) or []
    if subtasks:
        if depth == 0:
            print()
            cprint(t("cli.messages.task_show_subtasks_title", count=len(subtasks)), "cyan", bold=True)
        for st in subtasks:
            _print_task_tree_node(st, depth + 1)
    elif depth > 0:
        # 叶子节点不显示
        pass


def _handle_vuln_blast(args, db):
    """处理 vuln-blast 子命令（漏洞爆炸半径分析）"""
    parser = argparse.ArgumentParser(
        prog="cw vuln-blast",
        description=t("cli_vuln_blast_desc", default="Vulnerability blast radius analysis"))
    parser.add_argument("--finding-id", type=int, default=0,
                        help=t("cli_vuln_blast_arg_finding_id", default="Specify Semgrep finding ID (default: scan all)"))
    parser.add_argument("--severity", default="",
                        help=t("cli_vuln_blast_arg_severity", default="Severity filter (ERROR/WARN/INFO)"))
    parser.add_argument("--depth", type=int, default=3,
                        help=t("cli_vuln_blast_arg_depth", default="Reverse call graph traversal depth (default: 3)"))

    opts = parser.parse_args(args)
    result = db.get_vulnerability_blast_radius(
        finding_id=opts.finding_id, severity_filter=opts.severity, depth=opts.depth
    )

    cprint(t("cli.messages.vuln_blast_title"), "cyan", bold=True)

    # 风险等级
    risk = result.get("risk_level", "low")
    risk_color = {"critical": "red", "high": "red",
                  "medium": "yellow", "low": "green"}.get(risk, "white")
    print(t("cli.messages.vuln_blast_risk_level"), end="")
    cprint(risk, risk_color, bold=True)
    print(t("cli.messages.vuln_blast_total_findings", count=result.get('total_findings', 0)))
    print(t("cli.messages.vuln_blast_impacted_symbols", count=result.get('total_impacted_symbols', 0)))
    print()

    # 过滤条件
    if opts.finding_id:
        print(t("cli.messages.vuln_blast_filter_finding", id=opts.finding_id))
    elif opts.severity:
        print(t("cli.messages.vuln_blast_filter_severity", sev=opts.severity))
    print()

    # 各漏洞影响详情
    findings = result.get("findings", [])
    if not findings:
        cprint(t("cli.messages.vuln_blast_no_findings"), "yellow")
        return True

    print(t("cli.messages.vuln_blast_findings_title", count=len(findings)))
    sev_icon = {"ERROR": "[!]", "WARN": "[~]", "INFO": "[i]"}
    for i, f in enumerate(findings, 1):
        sev = f.get("severity", "")
        icon = sev_icon.get(sev, "[?]")
        rule = f.get('rule_name', '') or f.get('rule_id', '')
        print(t("cli.messages.vuln_blast_finding_item",
                idx=i, icon=icon, fid=f.get('finding_id', ''), sev=sev))
        print(t("cli.messages.vuln_blast_finding_rule", rule=rule))
        if f.get("file_path"):
            print(t("cli.messages.vuln_blast_finding_file", file=f['file_path']))
        if f.get("symbol_qualified"):
            print(t("cli.messages.vuln_blast_finding_symbol", symbol=f['symbol_qualified']))
        print(t("cli.messages.vuln_blast_finding_impacted", count=f.get('impacted_count', 0)))

        # 影响树（复用 blast_radius 输出）
        br = f.get("blast_radius", {})
        by_layer = br.get("by_layer", {}) if br else {}
        if by_layer:
            layer_str = "  ".join(f"{k}:{v}" for k, v in by_layer.items())
            print(t("cli.messages.vuln_blast_cross_layer", layer_str=layer_str))
        print()

    # 受影响符号汇总
    summary = result.get("impacted_symbols_summary", {})
    if summary:
        by_layer = summary.get("by_layer", {})
        if by_layer:
            print(t("cli.messages.vuln_blast_summary_title"))
            print(t("cli.messages.vuln_blast_summary_layers",
                    code=by_layer.get('code', 0), db=by_layer.get('db', 0),
                    api=by_layer.get('api', 0), config=by_layer.get('config', 0)))
            print()

        high_risk = summary.get("high_risk_callers", [])
        if high_risk:
            print(t("cli.messages.vuln_blast_high_risk_title", count=len(high_risk)))
            for i, h in enumerate(high_risk[:10], 1):
                qn = h.get("qualified_name", "") if isinstance(h, dict) else str(h)
                print(t("cli.messages.vuln_blast_high_risk_item", idx=i, name=qn))
            if len(high_risk) > 10:
                print(t("cli.messages.vuln_blast_high_risk_more", count=len(high_risk) - 10))
            print()

    return True


def _handle_symbol_history(args, db):
    """处理 symbol-history 子命令（符号 Git 变更历史）"""
    parser = argparse.ArgumentParser(
        prog="cw symbol-history",
        description=t("cli_symbol_history_desc", default="Symbol Git change history"))
    parser.add_argument("symbol_hash",
                        help=t("cli_symbol_history_arg_symbol_hash", default="Symbol content hash"))
    parser.add_argument("--limit", type=int, default=20,
                        help=t("cli_symbol_history_arg_limit", default="Result limit (default: 20)"))

    opts = parser.parse_args(args)
    commits = db.get_symbol_commit_history(opts.symbol_hash, limit=opts.limit)

    cprint(t("cli.messages.symbol_history_title"), "cyan", bold=True)
    print(t("cli.messages.symbol_history_hash", hash=opts.symbol_hash[:12]))
    print(t("cli.messages.symbol_history_change_count", count=len(commits)))
    print()

    if not commits:
        cprint(t("cli.messages.symbol_history_no_records"), "yellow")
        return True

    print(t("cli.messages.symbol_history_commits_title"))
    # 变更类型图标映射
    type_icon = {"added": "[+]", "modified": "[~]", "deleted": "[-]"}.get
    for i, c in enumerate(commits, 1):
        commit_hash = c.get("commit_hash", "")
        change_type = c.get("change_type", "")
        author = c.get("author", "")
        message = (c.get("message", "") or "").strip()
        ts = c.get("timestamp", 0)

        icon = type_icon(change_type, "[?]")
        print(t("cli.messages.symbol_history_commit_item",
                idx=i, icon=icon, hash=commit_hash[:12], change_type=change_type))
        if author:
            print(t("cli.messages.symbol_history_author", author=author))
        if ts:
            t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            print(t("cli.messages.symbol_history_time", time=t_str))
        if message:
            # 消息截断显示首行
            msg_line = message.split("\n")[0][:80]
            print(t("cli.messages.symbol_history_message", msg=msg_line))
        print()

    return True


# --------------------------------------------------------------------
# 检查门禁子命令（F6）
# --------------------------------------------------------------------

def _handle_check_gate(args, db):
    """处理 check-gate 子命令（手动运行检查门禁）

    用法:
      cw check-gate <task_id>              检查任务关联的变更文件
      cw check-gate <task_id> --resolve    标记门禁发现为已解决
    """
    parser = argparse.ArgumentParser(
        prog="cw check-gate",
        description=t("cli_check_gate_desc", default="Check gate (F6)"))
    parser.add_argument("task_id", help=t("cli_check_gate_arg_task_id", default="Task ID"))
    parser.add_argument("--resolve", action="store_true",
                        help=t("cli_check_gate_arg_resolve", default="Mark gate findings for this task as resolved (call after agent fix)"))
    parser.add_argument("--step-id", default="",
                        help=t("cli_check_gate_arg_step_id", default="Related step ID (optional)"))

    opts = parser.parse_args(args)

    if opts.resolve:
        result = db.resolve_gate_findings(task_id=opts.task_id)
        cprint(t("cli.messages.check_gate_resolved_title"), "cyan", bold=True)
        print(t("cli.messages.check_gate_task_id", id=opts.task_id))
        print(t("cli.messages.check_gate_resolved_count", count=result.get('resolved_count', 0)))
        print()
        return True

    # 查找任务关联的变更文件
    changed_files = db.get_task_changed_files(opts.task_id)
    if not changed_files:
        cprint(t("cli.messages.check_gate_no_changes", id=opts.task_id), "yellow")
        print()
        return True

    result = db.run_check_gate(opts.task_id, opts.step_id, changed_files)
    icon = "✅" if result["passed"] else "❌"
    cprint(t("cli.messages.check_gate_result_title", icon=icon), "cyan", bold=True)
    print(t("cli.messages.check_gate_task_id", id=opts.task_id))
    print(t("cli.messages.check_gate_result", result=result['summary']))
    checks_run = ', '.join(result.get('checks_run', []))
    print(t("cli.messages.check_gate_checks", checks=checks_run))
    print()

    findings = result.get("findings", [])
    if findings:
        cprint(t("cli.messages.check_gate_findings_title", count=len(findings)), "yellow")
        sev_icon = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}
        sev_color = {"ERROR": "red", "WARNING": "yellow", "INFO": "cyan"}
        for i, f in enumerate(findings, 1):
            sev = f.get("severity", "WARNING")
            f_icon = sev_icon.get(sev, "[?]")
            color = sev_color.get(sev, "white")
            cprint(t("cli.messages.check_gate_finding_item",
                     idx=i, icon=f_icon, sev=sev,
                     file=f.get('file', ''), line=f.get('line', '?'),
                     check=f.get('check', '')), color)
            if f.get("message"):
                print(t("cli.messages.check_gate_finding_msg", msg=f['message']))
        print()

    if result.get("fix_required"):
        cprint(t("cli.messages.check_gate_fix_required"), "yellow")
        print()

    return True


# --------------------------------------------------------------------
# 测试影响选择子命令
# --------------------------------------------------------------------

def _handle_test_impact(args, db):
    """处理 test-impact 子命令（改了某函数后需要运行哪些测试）

    通过反向调用链 BFS，找到所有直接和间接调用该函数的测试函数。
    """
    parser = argparse.ArgumentParser(
        prog="cw test-impact",
        description=t("cli_test_impact_desc", default="Test impact selection"))
    parser.add_argument("qualified_name",
                        help=t("cli_test_impact_arg_qualified_name", default="Qualified name of the modified function"))
    opts = parser.parse_args(args)

    tests = db.test_impact_selection(qualified_name=opts.qualified_name)

    cprint(t("cli.messages.test_impact_title"), "cyan", bold=True)
    print(t("cli.messages.test_impact_target", name=opts.qualified_name))
    print(t("cli.messages.test_impact_count", count=len(tests)))
    print()

    if not tests:
        cprint(t("cli.messages.test_impact_no_tests"), "yellow")
        return True

    print(t("cli.messages.test_impact_tests_title"))
    for i, t in enumerate(tests, 1):
        name = t.get("name", "")
        qn = t.get("qualified_name", "")
        fp = t.get("file_path", "")
        line = t.get("start_line", "?")
        print(t("cli.messages.test_impact_test_item", idx=i, name=name))
        print(t("cli.messages.test_impact_test_qn", qn=qn))
        print(t("cli.messages.test_impact_test_location", file=fp, line=line))
    print()

    return True


# --------------------------------------------------------------------
# 代码图谱 GC 子命令（归档被 .gitignore/.callwardenignore 命中的文件）
# --------------------------------------------------------------------

def _handle_gc(args, db):
    """处理 gc 子命令（代码图谱 GC）"""
    parser = argparse.ArgumentParser(
        prog="cw gc",
        description=t("cli_gc_desc", default="Code graph GC (archive/restore/purge ignored files)"))
    sub = parser.add_subparsers(dest="action", required=True)

    # gc archive
    archive_p = sub.add_parser("archive",
                              help=t("cli_gc_archive_desc", default="Archive files matched by ignore rules"))
    archive_p.add_argument("--force", action="store_true",
                           help=t("cli_gc_archive_arg_force", default="Full GC: scan all active files (default: only pending)"))
    archive_p.add_argument("--dry-run", action="store_true",
                           help=t("cli_gc_archive_arg_dry_run", default="Dry run: only report counts, do not archive"))

    # gc restore
    restore_p = sub.add_parser("restore",
                               help=t("cli_gc_restore_desc", default="Restore archived files"))
    restore_p.add_argument("--path", nargs="*", default=None,
                           help=t("cli_gc_restore_arg_path", default="Relative path to restore (empty scans all archived files)"))
    restore_p.add_argument("--force", action="store_true",
                           help=t("cli_gc_restore_arg_force", default="Restore even if still matched by ignore rules"))

    # gc status
    sub.add_parser("status", help=t("cli_gc_status_desc", default="Show GC status"))

    # gc purge
    purge_p = sub.add_parser("purge",
                             help=t("cli_gc_purge_desc", default="Permanently purge files archived for more than N days"))
    purge_p.add_argument("--older-than", type=int, default=30,
                         help=t("cli_gc_purge_arg_older_than", default="Purge files archived more than this many days (default: 30)"))

    # gc policy
    policy_p = sub.add_parser("policy",
                              help=t("cli_gc_policy_desc", default="Show or update GC retention policy"))
    policy_sub = policy_p.add_subparsers(dest="policy_action", required=True)
    policy_sub.add_parser("show", help=t("cli_gc_policy_show_desc", default="Show current GC retention policy"))
    policy_set = policy_sub.add_parser("set", help=t("cli_gc_policy_set_desc", default="Update GC retention policy"))
    _add_gc_policy_options(policy_set)

    # gc retention
    retention_p = sub.add_parser("retention",
                                 help=t("cli_gc_retention_desc", default="Prune cold historical data after compressed backup"))
    _add_gc_policy_options(retention_p)
    run_group = retention_p.add_mutually_exclusive_group()
    run_group.add_argument("--dry-run", action="store_true", dest="dry_run", default=True,
                           help=t("cli_gc_retention_arg_dry_run", default="Preview only; do not modify database"))
    run_group.add_argument("--apply", action="store_false", dest="dry_run",
                             help=t("cli_gc_retention_arg_apply", default="Apply changes; default is dry run"))
    retention_p.add_argument("--save-policy", action="store_true",
                             help=t("cli_gc_retention_arg_save_policy", default="Persist provided policy options before running"))

    # gc archive-list（v20 新增：列出 gc_archives/*.db.gz 备份文件）
    archive_list_p = sub.add_parser("archive-list",
                                    help=t("cli_gc_archive_list_desc", default="List GC backup files"))
    archive_list_p.add_argument("--limit", type=int, default=20,
                                help=t("cli_gc_archive_list_arg_limit", default="Maximum number of entries to show (default 20)"))

    # gc archive-inspect（v20 新增：检查备份文件内容，只读模式）
    archive_inspect_p = sub.add_parser("archive-inspect",
                                       help=t("cli_gc_archive_inspect_desc", default="Inspect GC backup file contents (read-only)"))
    archive_inspect_p.add_argument("path",
                                   help=t("cli_gc_archive_inspect_arg_path",
                                          default="Backup file path (.db.gz, supports shorthand relative to gc_archives directory)"))

    # gc audit-list（v20 新增：查看 GC 审计历史）
    audit_list_p = sub.add_parser("audit-list",
                                  help=t("cli_gc_audit_list_desc", default="View GC audit history"))
    audit_list_p.add_argument("--limit", type=int, default=20,
                              help=t("cli_gc_audit_list_arg_limit", default="Maximum number of entries to show (default 20)"))
    audit_list_p.add_argument("--operation", default=None,
                              help=t("cli_gc_audit_list_arg_operation", default="Filter by operation type (retention/archive/purge)"))

    # gc audit-show（v20 新增：查看单条审计记录详情）
    audit_show_p = sub.add_parser("audit-show",
                                  help=t("cli_gc_audit_show_desc", default="View details of a single GC audit record"))
    audit_show_p.add_argument("id", type=int,
                              help=t("cli_gc_audit_show_arg_id", default="Audit record ID"))

    # gc archive-import（v20 新增：从备份导回历史数据到当前库）
    archive_import_p = sub.add_parser("archive-import",
                                     help=t("cli_gc_archive_import_desc", default="Import historical data from GC backup file"))
    archive_import_p.add_argument("path",
                                 help=t("cli_gc_archive_import_arg_path",
                                        default="Backup file path (.db.gz, supports shorthand relative to gc_archives)"))
    archive_import_p.add_argument("--file", default="",
                                  help=t("cli_gc_archive_import_arg_file", default="Relative file path to import (e.g. src/a.py)"))
    archive_import_p.add_argument("--package", default="",
                                  help=t("cli_gc_archive_import_arg_package", default="External package name to import"))
    archive_import_p.add_argument("--dry-run", action="store_true", dest="dry_run", default=True,
                                 help=t("cli_gc_archive_import_arg_dry_run", default="Preview only (default); do not modify database"))
    archive_import_p.add_argument("--apply", action="store_false", dest="dry_run",
                                   help=t("cli_gc_archive_import_arg_apply", default="Apply import; default is dry run"))

    parsed = parser.parse_args(args)

    if parsed.action == "archive":
        result = db.gc_archive(force=parsed.force, dry_run=parsed.dry_run)
        mode = t("cli.messages.gc_mode_full") if parsed.force else t("cli.messages.gc_mode_young")
        dry = t("cli.messages.gc_dry_run") if parsed.dry_run else ""
        cprint(t("cli.messages.gc_archive_title", mode=mode, dry=dry), "cyan", bold=True)
        cprint(t("cli.messages.gc_scanned", count=result['scanned']), "dim")
        cprint(t("cli.messages.gc_archived", count=result['archived']),
               "yellow" if result["archived"] else "green")
        cprint(t("cli.messages.gc_skipped", count=result['skipped']), "dim")
        if result["reasons"]:
            cprint(t("cli.messages.gc_reasons_title"), "dim")
            for reason, count in result["reasons"].items():
                cprint(t("cli.messages.gc_reason_item", reason=reason, count=count), "dim")
        cprint()
        return True

    elif parsed.action == "restore":
        result = db.gc_restore(rel_paths=parsed.path, force=parsed.force)
        cprint(t("cli.messages.gc_restore_title"), "cyan", bold=True)
        cprint(t("cli.messages.gc_scanned_archived", count=result['scanned']), "dim")
        cprint(t("cli.messages.gc_restored", count=result['restored']),
               "green" if result["restored"] else "dim")
        cprint(t("cli.messages.gc_still_ignored", count=result['still_ignored']), "dim")
        if result["restored"] > 0:
            cprint(t("cli.messages.gc_restore_hint"), "yellow")
        cprint()
        return True

    elif parsed.action == "status":
        status = db.gc_status()
        cprint(t("cli.messages.gc_status_title"), "cyan", bold=True)
        cprint(t("cli.messages.gc_active_files", count=status['active_files']), "green")
        cprint(t("cli.messages.gc_archived_files", count=status['archived_files']),
               "yellow" if status["archived_files"] else "dim")
        cprint(t("cli.messages.gc_deleted_files", count=status['deleted_files']), "dim")
        ratio = f"{status['archive_ratio']*100:.1f}"
        cprint(t("cli.messages.gc_archive_ratio", ratio=ratio), "dim")
        if status["archived_files"] > 0:
            cprint(t("cli.messages.gc_archived_symbols", count=status['archived_symbols']), "dim")
            cprint(t("cli.messages.gc_archived_calls", count=status['archived_calls']), "dim")
        if status["recent_archives"]:
            cprint(t("cli.messages.gc_recent_archives"), "dim")
            for r in status["recent_archives"]:
                from datetime import datetime
                ts = datetime.fromtimestamp(r["archived_at"]).strftime("%Y-%m-%d %H:%M")
                cprint(t("cli.messages.gc_recent_archive_item",
                         ts=ts, path=r['rel_path'],
                         count=r['symbol_count'], reason=r['archive_reason']), "dim")
        cprint()
        return True

    elif parsed.action == "purge":
        result = db.gc_purge(older_than_days=parsed.older_than)
        cprint(t("cli.messages.gc_purge_title"), "cyan", bold=True)
        cprint(t("cli.messages.gc_purged_files", count=result['purged_files']),
               "yellow" if result["purged_files"] else "green")
        cprint(t("cli.messages.gc_purged_symbols", count=result['purged_symbols']), "dim")
        cprint(t("cli.messages.gc_purged_calls", count=result['purged_calls']), "dim")
        cprint()
        return True

    elif parsed.action == "policy":
        if parsed.policy_action == "show":
            policy = db.get_gc_policy()
        else:
            policy = db.set_gc_policy(
                older_than_days=parsed.older_than,
                keep_versions=parsed.keep_versions,
                include_external=parsed.include_external,
                external_stale_days=parsed.external_stale_days,
                backup_enabled=parsed.backup,
                vacuum_enabled=parsed.vacuum,
            )
        _print_gc_policy(policy)
        return True

    elif parsed.action == "retention":
        result = db.gc_retention(
            older_than_days=parsed.older_than,
            keep_versions=parsed.keep_versions,
            include_external=parsed.include_external,
            external_stale_days=parsed.external_stale_days,
            dry_run=parsed.dry_run,
            backup=parsed.backup,
            vacuum=parsed.vacuum,
            save_policy=parsed.save_policy,
        )
        cprint(t("cli.messages.gc_retention_title"), "cyan", bold=True)
        mode_key = "cli.messages.gc_retention_mode_dry_run" if result["dry_run"] else "cli.messages.gc_retention_mode_apply"
        cprint(t(mode_key), "yellow" if result["dry_run"] else "green")
        if result["saved_policy"]:
            cprint(t("cli.messages.gc_retention_policy_saved"), "green")
        cprint(t("cli.messages.gc_retention_candidate_versions", count=result["candidate_file_versions"]), "dim")
        cprint(t("cli.messages.gc_retention_candidate_external", count=result["candidate_external_packages"]), "dim")
        if result["backup_path"]:
            cprint(t("cli.messages.gc_retention_backup", path=result["backup_path"]), "dim")
        if not result["dry_run"]:
            cprint(t("cli.messages.gc_retention_deleted_versions", count=result["deleted_file_versions"]), "dim")
            cprint(t("cli.messages.gc_retention_deleted_external", count=result["deleted_external_symbols"]), "dim")
            cprint(t("cli.messages.gc_retention_deleted_orphans", count=result["deleted_orphan_symbol_contents"]), "dim")

        # v20 新增：Top N 收益预估（dry-run 和 apply 都展示）
        estimate = result.get("estimate") or {}
        approx = estimate.get("approximate_deleted_rows") or {}
        affected_files = estimate.get("affected_files_top_n") or []
        external_top = estimate.get("external_packages_top_n") or []
        # 仅在有候选数据时输出，避免空预演产生噪声
        if approx or affected_files or external_top:
            cprint(t("cli.messages.gc_retention_estimate_title"), "dim")
            cprint(t(
                "cli.messages.gc_retention_estimate_rows",
                fv=approx.get("file_versions", 0),
                fsv=approx.get("file_symbol_versions", 0),
                cv=approx.get("call_versions", 0),
                sc=approx.get("symbol_contents", 0),
                es=approx.get("external_symbols", 0),
                ep=approx.get("external_packages", 0),
            ), "dim")
            if affected_files:
                cprint(t("cli.messages.gc_retention_estimate_files_top", top_n=len(affected_files)), "dim")
                for idx, item in enumerate(affected_files, 1):
                    newest = item.get("newest_parsed") or 0
                    cprint(t(
                        "cli.messages.gc_retention_estimate_files_item",
                        idx=idx,
                        path=item.get("rel_path", ""),
                        count=item.get("candidate_versions", 0),
                        newest=_format_ts(newest),
                    ), "dim")
            else:
                cprint(t("cli.messages.gc_retention_estimate_files_empty"), "dim")
            if external_top:
                cprint(t("cli.messages.gc_retention_estimate_pkgs_top", top_n=len(external_top)), "dim")
                for idx, item in enumerate(external_top, 1):
                    last = item.get("last_touch") or 0
                    cprint(t(
                        "cli.messages.gc_retention_estimate_pkgs_item",
                        idx=idx,
                        name=item.get("package_name", ""),
                        version=item.get("package_version", ""),
                        count=item.get("symbol_count", 0),
                        last=_format_ts(last),
                    ), "dim")
            else:
                cprint(t("cli.messages.gc_retention_estimate_pkgs_empty"), "dim")
            # VACUUM 提示
            cprint(t("cli.messages.gc_retention_estimate_vacuum_hint"), "dim")
        cprint()
        return True

    elif parsed.action == "archive-list":
        # v20 新增：列出 gc_archives/*.db.gz 备份文件
        items = db.gc_archive_list(limit=parsed.limit)
        cprint(t("cli.messages.gc_archive_list_title"), "cyan", bold=True)
        if not items:
            cprint(t("cli.messages.gc_archive_list_empty"), "dim")
            cprint()
            return True
        from datetime import datetime
        for idx, item in enumerate(items, 1):
            cprint(t("cli.messages.gc_archive_list_item", idx=idx, name=item["name"]), "dim")
            ts = datetime.fromtimestamp(item["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
            size_str = _format_bytes(item["size"])
            cprint(t("cli.messages.gc_archive_list_size",
                     size=size_str, reason=item["reason"]), "dim")
            cprint(t("cli.messages.gc_archive_list_mtime", ts=ts), "dim")
        cprint()
        return True

    elif parsed.action == "archive-inspect":
        # v20 新增：检查备份文件内容（只读）
        try:
            info = db.gc_archive_inspect(path=parsed.path)
        except (FileNotFoundError, ValueError) as e:
            cprint(str(e), "red")
            return False
        cprint(t("cli.messages.gc_archive_inspect_title"), "cyan", bold=True)
        cprint(t("cli.messages.gc_archive_inspect_file", name=info["name"]), "dim")
        cprint(t("cli.messages.gc_archive_inspect_size", size=_format_bytes(info["size"])), "dim")
        cprint(t("cli.messages.gc_archive_inspect_schema_version", version=info["schema_version"]), "dim")
        cprint(t("cli.messages.gc_archive_inspect_tables_title"), "dim")
        for tb in info["tables"]:
            cprint(t("cli.messages.gc_archive_inspect_table_item",
                     name=tb["name"], rows=tb["rows"]), "dim")
        cprint(t("cli.messages.gc_archive_inspect_summary"), "dim")
        summary_items = [
            ("workspaces", info["workspace_count"]),
            ("file_versions", info["file_version_count"]),
            ("symbols", info["symbol_count"]),
            ("calls", info["call_count"]),
            ("gc_runs", info["gc_runs_count"]),
            ("archived_files", info["archived_files_count"]),
        ]
        for label, count in summary_items:
            cprint(t("cli.messages.gc_archive_inspect_summary_item",
                     label=label, count=count), "dim")
        cprint()
        return True

    elif parsed.action == "audit-list":
        # v20 新增：查看 GC 审计历史
        from datetime import datetime
        rows = db.gc_audit_list(limit=parsed.limit, operation=parsed.operation)
        cprint(t("cli.messages.gc_audit_list_title"), "cyan", bold=True)
        if not rows:
            cprint(t("cli.messages.gc_audit_list_empty"), "dim")
            cprint()
            return True
        for idx, row in enumerate(rows, 1):
            dry_label = t("cli.messages.gc_audit_dry_run_yes") if row["dry_run"] else t("cli.messages.gc_audit_dry_run_no")
            cprint(t("cli.messages.gc_audit_list_item",
                     idx=idx, id=row["id"], operation=row["operation"],
                     status=row["status"], dry_run=dry_label), "dim")
            ts = datetime.fromtimestamp(row["started_at"]).strftime("%Y-%m-%d %H:%M:%S")
            cprint(t("cli.messages.gc_audit_list_started", ts=ts), "dim")
            if row.get("backup_path"):
                cprint(t("cli.messages.gc_audit_list_backup", path=row["backup_path"]), "dim")
            cands = row.get("candidate_counts") or {}
            if cands:
                cprint(t("cli.messages.gc_audit_list_candidates", candidates=cands), "dim")
            dels = row.get("deleted_counts") or {}
            if dels:
                cprint(t("cli.messages.gc_audit_list_deleted", deleted=dels), "dim")
            if row["status"] == "failed" and row.get("error"):
                cprint(t("cli.messages.gc_audit_list_error", error=row["error"]), "red")
        cprint()
        return True

    elif parsed.action == "audit-show":
        # v20 新增：查看单条审计记录详情
        from datetime import datetime
        row = db.gc_audit_get(audit_id=parsed.id)
        if not row:
            cprint(t("errors.gc_audit_not_found", id=parsed.id), "red")
            return False
        cprint(t("cli.messages.gc_audit_show_title"), "cyan", bold=True)
        cprint(t("cli.messages.gc_audit_show_id", id=row["id"]), "dim")
        cprint(t("cli.messages.gc_audit_show_operation", operation=row["operation"]), "dim")
        cprint(t("cli.messages.gc_audit_show_status", status=row["status"]), "dim")
        cprint(t("cli.messages.gc_audit_show_dry_run",
                 value=str(row["dry_run"]).lower()), "dim")
        cprint(t("cli.messages.gc_audit_show_operator", operator=row["operator"]), "dim")
        started_ts = datetime.fromtimestamp(row["started_at"]).strftime("%Y-%m-%d %H:%M:%S")
        cprint(t("cli.messages.gc_audit_show_started", ts=started_ts), "dim")
        if row.get("completed_at"):
            completed_ts = datetime.fromtimestamp(row["completed_at"]).strftime("%Y-%m-%d %H:%M:%S")
            cprint(t("cli.messages.gc_audit_show_completed", ts=completed_ts), "dim")
        policy = row.get("policy_json") or {}
        if policy:
            cprint(t("cli.messages.gc_audit_show_policy"), "dim")
            for k, v in policy.items():
                cprint(t("cli.messages.gc_audit_show_policy_item", key=k, value=v), "dim")
        cands = row.get("candidate_counts") or {}
        if cands:
            cprint(t("cli.messages.gc_audit_show_candidates"), "dim")
            for k, v in cands.items():
                cprint(t("cli.messages.gc_audit_show_count_item", key=k, count=v), "dim")
        dels = row.get("deleted_counts") or {}
        if dels:
            cprint(t("cli.messages.gc_audit_show_deleted"), "dim")
            for k, v in dels.items():
                cprint(t("cli.messages.gc_audit_show_count_item", key=k, count=v), "dim")
        if row.get("backup_path"):
            cprint(t("cli.messages.gc_audit_show_backup",
                     path=row["backup_path"], size=row.get("backup_size", 0)), "dim")
        if row["status"] == "failed" and row.get("error"):
            cprint(t("cli.messages.gc_audit_show_error", error=row["error"]), "red")
        cprint()
        return True

    elif parsed.action == "archive-import":
        # v20 新增：从备份导回历史数据到当前库
        try:
            result = db.gc_archive_import(
                path=parsed.path,
                file_path=parsed.file,
                package_name=parsed.package,
                dry_run=parsed.dry_run,
            )
        except (FileNotFoundError, ValueError) as e:
            cprint(str(e), "red")
            return False
        cprint(t("cli.messages.gc_archive_import_title"), "cyan", bold=True)
        mode_key = "cli.messages.gc_archive_import_mode_dry_run" if result["dry_run"] else "cli.messages.gc_archive_import_mode_apply"
        cprint(t(mode_key), "yellow" if result["dry_run"] else "green")
        cprint(t("cli.messages.gc_archive_import_target",
                 target=result["target"], value=result["target_value"]), "dim")
        cprint(t("cli.messages.gc_archive_import_path", path=result["path"]), "dim")
        if result.get("errors"):
            cprint(t("cli.messages.gc_archive_import_errors_title"), "red")
            for err in result["errors"]:
                cprint(t("cli.messages.gc_archive_import_error_item", error=err), "red")
        # 导入明细
        imported = result.get("imported") or {}
        if imported:
            cprint(t("cli.messages.gc_archive_import_imported_title"), "green")
            for k, v in imported.items():
                if v > 0:
                    cprint(t("cli.messages.gc_archive_import_count_item", key=k, count=v), "green")
        # 跳过明细
        skipped = result.get("skipped") or {}
        if any(v > 0 for v in skipped.values()):
            cprint(t("cli.messages.gc_archive_import_skipped_title"), "yellow")
            for k, v in skipped.items():
                if v > 0:
                    cprint(t("cli.messages.gc_archive_import_count_item", key=k, count=v), "yellow")
        cprint()
        return True

    return False


def _add_gc_policy_options(parser):
    """添加 GC policy 选项。"""
    parser.add_argument("--older-than", type=int, default=None,
                        help=t("cli_gc_retention_arg_older_than", default="Prune file versions older than this many days"))
    parser.add_argument("--keep-versions", type=int, default=None,
                        help=t("cli_gc_retention_arg_keep_versions", default="Keep at least this many recent versions per file"))
    parser.add_argument("--include-external", action=argparse.BooleanOptionalAction, default=None,
                        help=t("cli_gc_retention_arg_include_external", default="Also prune cold external package symbols"))
    parser.add_argument("--external-stale-days", type=int, default=None,
                        help=t("cli_gc_retention_arg_external_stale_days", default="External package stale threshold in days"))
    parser.add_argument("--backup", action=argparse.BooleanOptionalAction, default=None,
                        help=t("cli_gc_retention_arg_backup", default="Create compressed database backup before pruning"))
    parser.add_argument("--vacuum", action=argparse.BooleanOptionalAction, default=None,
                        help=t("cli_gc_retention_arg_vacuum", default="Run VACUUM after pruning to release disk space"))


def _print_gc_policy(policy):
    """打印 GC policy。"""
    cprint(t("cli.messages.gc_policy_title"), "cyan", bold=True)
    cprint(t("cli.messages.gc_policy_older_than", count=policy["older_than_days"]), "dim")
    cprint(t("cli.messages.gc_policy_keep_versions", count=policy["keep_versions"]), "dim")
    cprint(t("cli.messages.gc_policy_include_external", value=str(policy["include_external"]).lower()), "dim")
    cprint(t("cli.messages.gc_policy_external_stale_days", count=policy["external_stale_days"]), "dim")
    cprint(t("cli.messages.gc_policy_backup", value=str(policy["backup_enabled"]).lower()), "dim")
    cprint(t("cli.messages.gc_policy_vacuum", value=str(policy["vacuum_enabled"]).lower()), "dim")
    cprint()


def _format_bytes(num_bytes: int) -> str:
    """字节数格式化为人类可读字符串（B/KB/MB/GB）。

    Args:
        num_bytes: 字节数

    Returns:
        形如 "1.50 KB" 的字符串；输入非整数或负数返回原始字符串
    """
    try:
        n = int(num_bytes)
    except (TypeError, ValueError):
        return str(num_bytes)
    if n < 0:
        return str(num_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f"{n} {units[idx]}"
    return f"{size:.2f} {units[idx]}"


def _format_ts(ts: float) -> str:
    """Unix 时间戳格式化为 "YYYY-MM-DD HH:MM" 字符串。

    Args:
        ts: Unix 时间戳（秒）

    Returns:
        形如 "2026-07-05 14:30" 的字符串；输入非数值或 0 返回 "-"
    """
    try:
        n = float(ts)
    except (TypeError, ValueError):
        return "-"
    if n <= 0:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(n))


# --------------------------------------------------------------------
# 诊断与维护子命令
# --------------------------------------------------------------------

def _handle_doctor(args, db):
    """处理 doctor 子命令（环境诊断与维护）

    提供两种模式：
    1. cw doctor           - 检查环境、数据库状态、推荐优化
    2. cw doctor --add-defender-exclusion - 添加 Windows Defender 排除项（需管理员）
    """
    parser = argparse.ArgumentParser(
        prog="cw doctor",
        description=t("cli.messages.doctor_desc", default="Environment diagnostics and maintenance"),
    )
    parser.add_argument("--add-defender-exclusion", action="store_true",
                       help=t("cli.messages.doctor_add_defender_help", default="Add .callwarden directory to Windows Defender exclusions (requires admin privileges)"))
    opts = parser.parse_args(args)

    if opts.add_defender_exclusion:
        return _doctor_add_defender_exclusion(db)

    return _doctor_check(db)


def _doctor_check(db):
    """环境诊断：检查数据库状态、性能配置、Defender 状态等"""
    cprint(t("cli.messages.doctor_title", default="=== Call Warden Environment Diagnostics ==="), "cyan", bold=True)
    print()

    # 1. 数据库基本信息
    cprint(t("cli.messages.doctor_db_info_title", default="[1] Database information"), "yellow", bold=True)
    db_path = db.db_path
    import os
    import sqlite3
    print(t("cli.messages.doctor_db_path", default="  Path: {path}", path=db_path))
    print(t("cli.messages.doctor_db_size", default="  Size: {size:.2f} MB", size=os.path.getsize(db_path) / 1024 / 1024))

    # PRAGMA 检查
    # 注意：SQLite 返回值可能是小写或数字（如 wal 而非 WAL，1 而非 NORMAL）
    # 用等价映射统一比较
    pragma_aliases = {
        "journal_mode": {"wal": "WAL", "wal2": "WAL"},
        "synchronous": {"0": "OFF", "1": "NORMAL", "2": "FULL", "3": "EXTRA"},
    }
    pragmas = {
        "journal_mode": "WAL",
        "synchronous": "NORMAL",
        "busy_timeout": "30000",
        "cache_size": "-64000",
        "mmap_size": "268435456",
    }
    print(t("cli.messages.doctor_pragma_config", default="  PRAGMA config:"))
    all_pragma_ok = True
    for key, expected in pragmas.items():
        actual = db.conn.execute(f"PRAGMA {key}").fetchone()[0]
        actual_str = str(actual)
        # 应用别名映射
        aliases = pragma_aliases.get(key, {})
        actual_normalized = aliases.get(actual_str.lower(), aliases.get(actual_str, actual_str))
        expected_normalized = aliases.get(expected.lower(), aliases.get(expected, expected))
        ok = actual_normalized == expected_normalized
        mark = "✓" if ok else "✗"
        color = "green" if ok else "red"
        cprint(t("cli.messages.doctor_pragma_item", default="    {mark} {key} = {actual} (expected: {expected})", mark=mark, key=key, actual=actual_str, expected=expected), color)
        if not ok:
            all_pragma_ok = False
    print()

    # 2. WAL 文件检查
    cprint(t("cli.messages.doctor_wal_title", default="[2] WAL file status"), "yellow", bold=True)
    wal_path = db_path + "-wal"
    shm_path = db_path + "-shm"
    print(t("cli.messages.doctor_wal_file", default="  WAL file: {path}", path=wal_path))
    if os.path.exists(wal_path):
        wal_size = os.path.getsize(wal_path) / 1024
        print(t("cli.messages.doctor_wal_size", default="    Size: {size:.1f} KB", size=wal_size))
        if wal_size > 1024 * 10:  # > 10MB
            cprint(t("cli.messages.doctor_wal_large", default="    ! WAL file is large; consider running cw doctor --checkpoint"), "yellow")
        else:
            print(t("cli.messages.doctor_wal_size_ok", default="    ✓ Size is normal"))
    else:
        print(t("cli.messages.doctor_wal_missing_ok", default="    ✓ Not present (checkpointed)"))
    print()

    # 3. Defender 排除项检查（仅 Windows）
    if sys.platform == "win32":
        cprint(t("cli.messages.doctor_defender_title", default="[3] Windows Defender exclusions"), "yellow", bold=True)
        callwarden_dir = os.path.dirname(db_path)
        try:
            import subprocess
            result = subprocess.run(
                ["powershell", "-Command",
                 f"Get-MpPreference | Select-Object -ExpandProperty ExclusionPath"],
                capture_output=True, text=True, timeout=10,
            )
            exclusions = result.stdout.strip()
            if callwarden_dir.lower() in exclusions.lower():
                cprint(t("cli.messages.doctor_defender_added", default="  ✓ Exclusion already added: {path}", path=callwarden_dir), "green")
            else:
                cprint(t("cli.messages.doctor_defender_missing", default="  ✗ Exclusion missing (recommended to avoid intermittent SQLITE_CANTOPEN)"), "red")
                cprint(t("cli.messages.doctor_defender_dir", default="    Exclusion directory: {path}", path=callwarden_dir), "dim")
                cprint(t("cli.messages.doctor_defender_command", default="    Add command (requires admin):"), "dim")
                cprint(f"      cw doctor --add-defender-exclusion", "dim")
                cprint(t("cli.messages.doctor_defender_manual", default="    Or run manually:"), "dim")
                cprint(f"      powershell -Command \"Add-MpPreference -ExclusionPath '{callwarden_dir}'\"",
                       "dim")
        except Exception as e:
            cprint(t("cli.messages.doctor_defender_check_failed", default="  ? Could not check Defender status: {error}", error=e), "yellow")
        print()

    # 4. 快速连接测试
    cprint(t("cli.messages.doctor_connection_title", default="[4] Database connection test"), "yellow", bold=True)
    import time
    success = 0
    fail = 0
    for i in range(5):
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("SELECT 1").fetchone()
            conn.close()
            success += 1
        except sqlite3.OperationalError:
            fail += 1
        time.sleep(0.1)
    if fail == 0:
        cprint(t("cli.messages.doctor_connection_success", default="  ✓ All 5 connection tests succeeded"), "green")
    else:
        cprint(t("cli.messages.doctor_connection_failed", default="  ✗ {fail}/5 failed, possible intermittent Defender lock", fail=fail), "red")
    print()

    # 5. 总体评估
    cprint(t("cli.messages.doctor_overall_title", default="[5] Overall assessment"), "yellow", bold=True)
    if all_pragma_ok and fail == 0:
        cprint(t("cli.messages.doctor_overall_healthy", default="  ✓ Environment is healthy"), "green")
    elif all_pragma_ok:
        cprint(t("cli.messages.doctor_overall_mostly_healthy", default="  ~ Environment is mostly healthy, but connection failures occurred (consider adding Defender exclusion)"), "yellow")
    else:
        cprint(t("cli.messages.doctor_overall_needs_work", default="  ✗ Environment needs optimization (PRAGMA config is incorrect)"), "red")
    print()

    return True


def _doctor_add_defender_exclusion(db):
    """添加 Windows Defender 排除项（需管理员权限）"""
    if sys.platform != "win32":
        cprint(t("cli.messages.doctor_windows_only", default="✗ This command is only available on Windows"), "red")
        return True

    import os
    import subprocess
    callwarden_dir = os.path.dirname(db.db_path)
    # 排除到 .callwarden 根目录（涵盖所有项目的 db）
    parent_dir = os.path.dirname(callwarden_dir)

    cprint(t("cli.messages.doctor_add_defender_title", default="=== Add Windows Defender Exclusion ==="), "cyan", bold=True)
    print(t("cli.messages.doctor_add_defender_dir", default="  Exclusion directory to add: {path}", path=parent_dir))
    cprint(t("cli.messages.doctor_add_defender_admin_note", default="  Note: this operation requires admin privileges"), "yellow")
    print()

    # 检查当前是否已是管理员
    try:
        import ctypes
        is_admin = ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        is_admin = False

    if not is_admin:
        cprint(t("cli.messages.doctor_uac_try", default="✗ Current process is not elevated; trying UAC elevation..."), "yellow")
        # 通过 PowerShell Start-Process -Verb RunAs 提权
        cmd = f"Add-MpPreference -ExclusionPath '{parent_dir}'"
        try:
            subprocess.Popen(
                ["powershell", "-Command",
                 f"Start-Process powershell -Verb RunAs -ArgumentList '-Command', '{cmd}; Start-Sleep 2'"],
            )
            cprint(t("cli.messages.doctor_uac_prompted", default="✓ UAC prompt opened; confirm in the popup window"), "green")
            print()
            print(t("cli.messages.doctor_verify_hint", default="  Verify success with:"))
            cprint(f"    cw doctor", "cyan")
        except Exception as e:
            cprint(t("cli.messages.doctor_uac_failed", default="✗ UAC elevation failed: {error}", error=e), "red")
            print()
            print(t("cli.messages.doctor_manual_admin_hint", default="  Run PowerShell as administrator and execute:"))
            cprint(f"    Add-MpPreference -ExclusionPath '{parent_dir}'", "yellow")
        return True

    # 已是管理员
    try:
        subprocess.run(
            ["powershell", "-Command",
             f"Add-MpPreference -ExclusionPath '{parent_dir}'"],
            capture_output=True, text=True, timeout=10,
        )
        cprint(t("cli.messages.doctor_add_defender_success", default="✓ Defender exclusion added: {path}", path=parent_dir), "green")
    except Exception as e:
        cprint(t("cli.messages.doctor_add_defender_failed", default="✗ Add failed: {error}", error=e), "red")

    return True


def create_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器（使用 i18n 文本）"""
    parser = argparse.ArgumentParser(
        description=t("cli.description") + "\n\n" + t("cli.subcommand_help"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--lang", metavar="LANG", default=DEFAULT_LANG,
                       help=get_arg_help("lang"))
    parser.add_argument("--workspace", metavar="ROOT", help=get_arg_help("workspace"))
    parser.add_argument("--root", metavar="ROOT", help=get_arg_help("root"))
    parser.add_argument("--list-workspaces", action="store_true", help=get_arg_help("list_workspaces"))
    parser.add_argument("--register-workspace", nargs=2, metavar=("NAME", "ROOT"), help=get_arg_help("register_workspace"))
    parser.add_argument("--set-workspace", metavar="ID_OR_NAME", help=get_arg_help("set_workspace"))
    parser.add_argument("--delete-workspace", metavar="ID_OR_NAME", help=get_arg_help("delete_workspace"))
    parser.add_argument("--refresh-all", action="store_true", dest="refresh_all", help=get_arg_help("refresh_all"))
    parser.add_argument("--force", action="store_true", help=get_arg_help("force"))
    parser.add_argument("--watch", action="store_true", help=get_arg_help("watch"))
    parser.add_argument("--stats", action="store_true", help=get_arg_help("stats"))
    parser.add_argument("--status", action="store_true", help=get_arg_help("status"))
    parser.add_argument("--query", nargs=2, metavar=("NAME", "FILE"), help=get_arg_help("query"))
    parser.add_argument("--callers", metavar="NAME", help=get_arg_help("callers"))
    parser.add_argument("--callees", metavar="NAME", help=get_arg_help("callees"))
    parser.add_argument("--topo", action="store_true", help=get_arg_help("topo"))
    parser.add_argument("--topo-limit", type=int, default=50, help=get_arg_help("topo_limit"))
    parser.add_argument("--file", metavar="PATH", help=get_arg_help("file"))
    parser.add_argument("--refresh", metavar="PATH", help=get_arg_help("refresh"))
    parser.add_argument("--history", metavar="NAME", help=get_arg_help("history"))
    parser.add_argument("--show-content", action="store_true", help=get_arg_help("show_content"))
    parser.add_argument("--diff", nargs=2, metavar=("HASH1", "HASH2"), help=get_arg_help("diff"))
    parser.add_argument("--changes", metavar="SINCE", nargs="?", const="1h", help=get_arg_help("changes"))
    parser.add_argument("--changes-detail", action="store_true", help=get_arg_help("changes_detail"))
    parser.add_argument("--restore-comment", metavar="SPEC", help=get_arg_help("restore_comment"))
    parser.add_argument("--restore-all-comments", action="store_true", help=get_arg_help("restore_all_comments"))
    parser.add_argument("--restore-file", metavar="PATH", help=get_arg_help("restore_file"))
    parser.add_argument("--preview", action="store_true", help=get_arg_help("preview"))
    parser.add_argument("--comment-coverage", action="store_true", help=get_arg_help("comment_coverage"))
    parser.add_argument("--coverage-by", metavar="GROUP", default="module", help=get_arg_help("coverage_by"))
    parser.add_argument("--uncommented", metavar="KIND", nargs="?", const="fn", help=get_arg_help("uncommented"))
    parser.add_argument("--uncommented-module", metavar="MODULE", help=get_arg_help("uncommented_module"))
    parser.add_argument("--uncommented-limit", metavar="N", type=int, default=50, help=get_arg_help("uncommented_limit"))
    parser.add_argument("--search", metavar="QUERY", help=get_arg_help("search"))
    parser.add_argument("--search-kind", metavar="KIND", help=get_arg_help("search_kind"))
    parser.add_argument("--search-limit", metavar="N", type=int, default=50, help=get_arg_help("search_limit"))
    parser.add_argument("--symbol", metavar="QUALIFIED_NAME", help=get_arg_help("symbol"))
    parser.add_argument("--impact", metavar="QUALIFIED_NAME", help=get_arg_help("impact"))
    parser.add_argument("--call-chain", metavar="QUALIFIED_NAME", help=get_arg_help("call_chain"))
    parser.add_argument("--chain-depth", metavar="N", type=int, default=10, help=get_arg_help("chain_depth"))
    parser.add_argument("--top-callers", metavar="N", type=int, nargs="?", const=20, help=get_arg_help("top_callers"))
    parser.add_argument("--top-callers-module", metavar="MODULE", help=get_arg_help("top_callers_module"))
    parser.add_argument("--orphan-symbols", metavar="KIND", nargs="?", const="fn", help=get_arg_help("orphan_symbols"))
    parser.add_argument("--orphan-module", metavar="MODULE", help=get_arg_help("orphan_module"))
    parser.add_argument("--orphan-limit", metavar="N", type=int, default=50, help=get_arg_help("orphan_limit"))
    parser.add_argument("--deepest", metavar="N", type=int, nargs="?", const=20, help=get_arg_help("deepest"))
    parser.add_argument("--deepest-module", metavar="MODULE", help=get_arg_help("deepest_module"))
    parser.add_argument("--module-calls", metavar="N", type=int, nargs="?", const=20, help=get_arg_help("module_calls"))
    parser.add_argument("--detect-cycles", action="store_true", help=get_arg_help("detect_cycles"))
    parser.add_argument("--cycle-depth", metavar="N", type=int, default=10, help=get_arg_help("cycle_depth"))
    parser.add_argument("--export-module-graph", metavar="FORMAT", nargs="?", const="mermaid", help=get_arg_help("export_module_graph"))
    parser.add_argument("--graph-output", metavar="FILE", help=get_arg_help("graph_output"))
    parser.add_argument("--call-heatmap", metavar="GROUP_BY", nargs="?", const="module", help=get_arg_help("call_heatmap"))
    parser.add_argument("--heatmap-limit", metavar="N", type=int, default=20, help=get_arg_help("heatmap_limit"))
    parser.add_argument("--test-coverage", action="store_true", help=get_arg_help("test_coverage"))
    parser.add_argument("--function-issues", metavar="FN", nargs="?", const="", help=get_arg_help("function_issues"))
    parser.add_argument("--issue-summary", action="store_true", help=get_arg_help("issue_summary"))
    parser.add_argument("--issue-type", metavar="TYPE", help=get_arg_help("issue_type"))
    parser.add_argument("--issue-module", metavar="MODULE", help=get_arg_help("issue_module"))
    parser.add_argument("--issue-limit", metavar="N", type=int, default=30, help=get_arg_help("issue_limit"))
    parser.add_argument("--semgrep", metavar="PATH", nargs="*", help=get_arg_help("semgrep"))
    parser.add_argument("--semgrep-config", metavar="CONFIG", default="p/default", help=get_arg_help("semgrep_config"))
    parser.add_argument("--semgrep-scan-lang", metavar="LANG", nargs="*", help=get_arg_help("semgrep_scan_lang"))
    parser.add_argument("--semgrep-timeout", metavar="N", type=int, default=180, help=get_arg_help("semgrep_timeout"))
    parser.add_argument("--semgrep-quick", action="store_true", help=get_arg_help("semgrep_quick"))
    parser.add_argument("--semgrep-save", action="store_true", help=get_arg_help("semgrep_save"))
    parser.add_argument("--semgrep-list", nargs="?", const="", metavar="FILTER", help=get_arg_help("semgrep_list"))
    parser.add_argument("--semgrep-severity", metavar="SEV", help=get_arg_help("semgrep_severity"))
    parser.add_argument("--semgrep-list-lang", metavar="LANG", help=get_arg_help("semgrep_list_lang"))
    parser.add_argument("--semgrep-stats", action="store_true", help=get_arg_help("semgrep_stats"))
    parser.add_argument("--semgrep-limit", metavar="N", type=int, default=50, help=get_arg_help("semgrep_limit"))
    
    # Git 集成
    parser.add_argument("--git-import", metavar="N", type=int, nargs="?", const=100, help=get_arg_help("git_import"))
    parser.add_argument("--git-log", metavar="N", type=int, nargs="?", const=20, help=get_arg_help("git_log"))
    parser.add_argument("--git-show", metavar="COMMIT", help=get_arg_help("git_show"))
    parser.add_argument("--git-stats", action="store_true", help=get_arg_help("git_stats"))

    # 代码度量
    parser.add_argument("--metrics", action="store_true", help=get_arg_help("metrics"))
    parser.add_argument("--complexity", metavar="N", type=int, nargs="?", const=20, help=get_arg_help("complexity"))
    parser.add_argument("--complexity-module", metavar="MODULE", help=get_arg_help("complexity_module"))
    parser.add_argument("--coupling", action="store_true", help=get_arg_help("coupling"))
    parser.add_argument("--largest-fns", metavar="N", type=int, nargs="?", const=20, help=get_arg_help("largest_fns"))
    parser.add_argument("--coupled-fns", metavar="N", type=int, nargs="?", const=20, help=get_arg_help("coupled_fns"))
    parser.add_argument("--fn-metrics", metavar="NAME", help=get_arg_help("fn_metrics"))

    # 语义搜索
    parser.add_argument("--semantic-search", metavar="QUERY", help=get_arg_help("semantic_search"))
    parser.add_argument("--embed", action="store_true", help=get_arg_help("embed"))
    parser.add_argument("--embed-force", action="store_true", help=get_arg_help("embed_force"))
    parser.add_argument("--similar", metavar="NAME", help=get_arg_help("similar"))

    # 任务管理
    parser.add_argument("--task-list", action="store_true", help=get_arg_help("task_list"))
    parser.add_argument("--task-show", metavar="TASK_ID", help=get_arg_help("task_show"))

    # 项目简报和仓库地图
    parser.add_argument("--brief", action="store_true", help=get_arg_help("brief"))
    parser.add_argument("--map", action="store_true", help=get_arg_help("map"))
    parser.add_argument("--map-format", choices=["text", "mermaid"], default="text", help=get_arg_help("map_format"))

    # 覆盖率
    parser.add_argument("--coverage-import", metavar="FILE", help=get_arg_help("coverage_import"))
    parser.add_argument("--coverage-format", choices=["lcov", "cobertura"], default="lcov", help=get_arg_help("coverage_format"))
    parser.add_argument("--coverage-fn", metavar="NAME", help=get_arg_help("coverage_fn"))
    parser.add_argument("--coverage-uncovered", action="store_true", help=get_arg_help("coverage_uncovered"))

    # 所有权
    parser.add_argument("--who", metavar="FILE", help=get_arg_help("who"))
    parser.add_argument("--ownership-map", action="store_true", help=get_arg_help("ownership_map"))

    return parser


def main():
    """CLI 主入口函数"""
    # 代码守护者架构子命令拦截（四大支柱）
    # 子命令格式: cw <subcommand> [options]，如 cw defect stats
    if len(sys.argv) > 1 and sys.argv[1] in _SUBCOMMANDS:
        _run_subcommand_mode()
        return

    # 显示 --help 时，先打印子命令概览（确保 head -50 可见）
    # argparse 默认将 description 放在超长 usage 之后，子命令信息会被推到 50 行之外
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print(t("cli.subcommand_help"))
        print()

    # 第一阶段：先解析 --lang 参数（不创建完整 parser）
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--lang", metavar="LANG", default=DEFAULT_LANG)
    pre_args, _ = pre_parser.parse_known_args()
    
    # 设置语言
    set_language(pre_args.lang)
    
    # 第二阶段：用正确的语言创建完整 parser 并解析所有参数
    parser = create_parser()
    args = parser.parse_args()
    
    # 确定工作区根目录
    workspace_root = None
    if args.workspace:
        workspace_root = args.workspace
    elif args.root:
        workspace_root = args.root
    else:
        # 自动检测：从当前目录向上查找项目根
        cwd = os.getcwd()
        detected = detect_project_root(cwd)
        if detected:
            workspace_root = detected
    
    # 初始化数据库
    db = CodeGraphDB(workspace_root=workspace_root) if workspace_root else CodeGraphDB()

    # 如果自动检测到了工作区，自动注册并设置为活动工作区
    if workspace_root and not args.list_workspaces:
        ws_name = get_default_workspace_name(workspace_root)
        # 检查是否已注册
        existing = None
        for ws in db.list_workspaces():
            if ws["root_path"] == workspace_root:
                existing = ws
                break
        if not existing:
            ws_id = db.register_workspace(ws_name, workspace_root)
            db.set_active_workspace(ws_id)
        elif not existing.get("is_active"):
            db.set_active_workspace(existing["id"])
    
    try:
        # 工作区管理命令
        if args.list_workspaces:
            workspaces = db.list_workspaces()
            print(t("cli.messages.workspaces_title", count=len(workspaces)))
            for ws in workspaces:
                active_mark = t("cli.messages.workspace_active_mark") if ws.get("is_active") else ""
                print(t("cli.messages.workspace_normal", id=ws['id'], name=ws['name']) + active_mark)
                print(t("cli.messages.workspace_path", path=ws['root_path']))
                if ws.get("description"):
                    print(t("cli.messages.workspace_desc", desc=ws['description']))
            return

        if args.register_workspace:
            name, root = args.register_workspace
            ws_id = db.register_workspace(name, root)
            print(t("cli.messages.register_success", id=ws_id, name=name, root=root))
            return

        if args.set_workspace:
            ws_arg = args.set_workspace
            # 尝试转换为 int（ID）
            try:
                ws_id = int(ws_arg)
                success = db.set_active_workspace(ws_id)
            except ValueError:
                success = db.set_active_workspace(ws_arg)
            if success:
                active = db.get_active_workspace()
                print(t("cli.messages.set_success", name=active['name'], root=active['root_path']))
            else:
                print(t("cli.messages.workspace_set_fail", name=ws_arg))
            return

        if args.delete_workspace:
            ws_arg = args.delete_workspace
            # 尝试转换为 int（ID）
            try:
                ws_id = int(ws_arg)
                success = db.delete_workspace(ws_id)
            except ValueError:
                success = db.delete_workspace(ws_arg)
            if success:
                print(t("cli.messages.delete_success", name=ws_arg))
            else:
                print(t("cli.messages.delete_not_found", name=ws_arg))
            return

        if args.refresh_all:
            if args.force:
                print(t("cli.messages.building_force"))
            else:
                print(t("cli.messages.building_incremental"))
            db.build_full_graph(force=args.force)
        
        elif args.watch:
            watcher = FileWatcher(db)
            watcher.start()

        elif args.stats:
            stats = db.get_stats()
            print(json.dumps(stats, indent=2, ensure_ascii=False))

        elif args.status:
            status = db.get_status()
            ws = status["workspace"]
            fi = status["files"]
            sy = status["symbols"]
            ca = status["calls"]

            def fmt_size(n):
                """格式化字节数为人类可读字符串（B/KB/MB）"""
                if n < 1024:
                    return f"{n} B"
                if n < 1024 * 1024:
                    return f"{n/1024:.1f} KB"
                return f"{n/1024/1024:.1f} MB"

            def fmt_ago(ts):
                """格式化时间戳为"多久之前"的相对描述（刚刚/N 分钟前/N 小时前/N 天前）"""
                if not ts:
                    return t("cli.messages.status_never_built")
                delta = time.time() - ts
                if delta < 60:
                    return t("cli.messages.status_just_now")
                if delta < 3600:
                    m = int(delta // 60)
                    return t("cli.messages.status_minutes_ago", m=m)
                if delta < 86400:
                    h = int(delta // 3600)
                    return t("cli.messages.status_hours_ago", h=h)
                d = int(delta // 86400)
                return t("cli.messages.status_days_ago", d=d)

            print()
            print(f"  {t('cli.messages.status_title')}")
            print()
            print(f"  {t('cli.messages.status_workspace')}: {ws['name']}")
            print(f"  {t('cli.messages.status_root')}: {ws['root']}")
            print(f"  {t('cli.messages.status_db_size')}: {fmt_size(ws['db_size'])}")
            print(f"  {t('cli.messages.status_last_build')}: {fmt_ago(status['last_build'])}")
            print()
            print(f"  {t('cli.messages.status_files_title')}")
            on_disk = t("cli.messages.status_files_on_disk")
            tracked = t("cli.messages.status_files_tracked")
            print(f"    {on_disk}: {fi['on_disk']}  ({tracked}: {fi['tracked']})")
            if fi["new"]:
                new_label = t("cli.messages.status_files_new")
                print(f"    {new_label}: {fi['new']}  {', '.join(fi['new_files'][:5])}{'...' if len(fi['new_files'])>5 else ''}")
            if fi["stale"]:
                stale_label = t("cli.messages.status_files_stale")
                print(f"    {stale_label}: {fi['stale']}  {', '.join(fi['stale_files'][:5])}{'...' if len(fi['stale_files'])>5 else ''}")
            if fi["deleted"]:
                deleted_label = t("cli.messages.status_files_deleted")
                print(f"    {deleted_label}: {fi['deleted']}  {', '.join(fi['deleted_files'][:5])}{'...' if len(fi['deleted_files'])>5 else ''}")
            if fi["by_language"]:
                parts = []
                for ext, cnt in sorted(fi["by_language"].items(), key=lambda x: -x[1])[:6]:
                    parts.append(f"{ext}: {cnt}")
                by_lang = t("cli.messages.status_by_language")
                print(f"    {by_lang}: {', '.join(parts)}")
            print()
            print(f"  {t('cli.messages.status_symbols_title')}")
            print(f"    {t('cli.messages.status_symbols_total')}: {sy['total']}")
            kind_parts = []
            kind_names = {"fn": t("cli.messages.kind_fn"), "test_fn": t("cli.messages.kind_test_fn"), "struct": t("cli.messages.kind_struct"),
                          "enum": t("cli.messages.kind_enum"), "trait": t("cli.messages.kind_trait"), "impl": "impl",
                          "const": "const", "static": "static", "method": t("cli.messages.kind_method"),
                          "class": t("cli.messages.kind_class"), "interface": t("cli.messages.kind_interface")}
            for kind, cnt in sorted(sy["by_kind"].items(), key=lambda x: -x[1])[:8]:
                kn = kind_names.get(kind, kind)
                kind_parts.append(f"{kn}: {cnt}")
            print(f"    {t('cli.messages.status_by_kind')}: {', '.join(kind_parts)}")
            print(f"    {t('cli.messages.status_uncommented_fns')}: {sy['uncommented_fns']}")
            print()
            print(f"  {t('cli.messages.status_calls_title')}")
            print(f"    {t('cli.messages.status_calls_total')}: {ca['total']}")
            resolved_label = t("cli.messages.status_calls_resolved")
            rate_label = t("cli.messages.status_calls_rate")
            print(f"    {resolved_label}: {ca['resolved']}  ({rate_label}: {ca['resolve_rate']}%)")
            print(f"    {t('cli.messages.status_calls_cross')}: {ca['cross_file']}")
            print()
            if status["needs_rebuild"]:
                print(f"  ⚠ {t('cli.messages.status_rebuild_hint')}")
            else:
                print(f"  {t('cli.messages.status_up_to_date')}")
            print()

        elif args.query:
            name, file_path = args.query
            result = db.get_symbol_location(name, file_path)
            if result:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print(t("cli.messages.query_not_found", name=name))

        elif args.callers:
            callers = db.get_callers(args.callers)
            print(t("cli.messages.callers_title", name=args.callers, count=len(callers)))
            for c in callers:
                cross = t("cli.messages.callers_cross_file") if c["is_cross_file"] else ""
                print(f"  {c['caller_file']}:{c['call_line']} -> {c['caller_name']}{cross}")

        elif args.callees:
            callees = db.get_callees(args.callees)
            print(t("cli.messages.callees_title", name=args.callees, count=len(callees)))
            for c in callees:
                cross = t("cli.messages.callees_cross_file") if c["is_cross_file"] else ""
                file_info = f" ({c['callee_file']})" if c["callee_file"] else t("cli.messages.callees_unresolved")
                print(f"  line {c['call_line']}: {c['callee_name']}{cross}{file_info}")

        elif args.topo:
            order = db.get_topological_order(args.topo_limit)
            print(t("cli.messages.topo_title", count=len(order)))
            for i, sym in enumerate(order):
                print(f"  {i+1}. depth={sym['depth']:2d}  {sym['path']}:{sym['start_line']}  {sym['name']}")

        elif args.file:
            symbols = db.get_file_symbols(args.file)
            print(t("cli.messages.file_symbols_title", path=args.file, count=len(symbols)))
            for s in symbols:
                print(f"  {s['start_line']}-{s['end_line']}: {s['kind']} {s['name']} ({s['visibility']})")

        elif args.refresh:
            db.refresh_file(args.refresh)
            print(t("cli.messages.refresh_done", path=args.refresh))

        elif args.history:
            history = db.get_history(args.history)
            if not history:
                print(t("cli.messages.history_not_found", name=args.history))
            else:
                print(t("cli.messages.history_title", name=args.history, count=len(history)))
                for i, h in enumerate(history, 1):
                    current = t("cli.messages.history_current") if h["is_current"] else ""
                    parsed_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(h["parsed_at"]))
                    print(f"  {i}. v{h['version_num']}{current} | {parsed_time} | hash={h['symbol_hash'][:12]}... | {h['file_path']}:{h['start_line']}-{h['end_line']}")

                    if args.show_content:
                        content = db.get_symbol_content_by_hash(h["symbol_hash"])
                        if content:
                            print(t("cli.messages.history_content"))
                            for line in content["content"].split("\n")[:5]:
                                print(f"       {line}")
                            if len(content["content"].split("\n")) > 5:
                                print(f"       ...")

        elif args.diff:
            hash1, hash2 = args.diff
            content1 = db.get_symbol_content_by_hash(hash1)
            content2 = db.get_symbol_content_by_hash(hash2)

            if not content1:
                print(t("cli.messages.diff_hash_not_found", hash=hash1))
            elif not content2:
                print(t("cli.messages.diff_hash_not_found", hash=hash2))
            else:
                print(t("cli.messages.diff_title", hash1=hash1[:12], hash2=hash2[:12]))
                print(t("cli.messages.diff_function", name=content1['qualified_name']))
                print(t("cli.messages.diff_type", kind=content1['kind']))
                print("-" * 40)

                lines1 = content1["content"].split("\n")
                lines2 = content2["content"].split("\n")

                # 简单对比：显示差异行数
                max_lines = max(len(lines1), len(lines2))
                for i in range(max_lines):
                    l1 = lines1[i] if i < len(lines1) else ""
                    l2 = lines2[i] if i < len(lines2) else ""
                    if l1 != l2:
                        if l1:
                            print(f"  - {i+1}: {l1}")
                        if l2:
                            print(f"  + {i+1}: {l2}")

        elif args.changes:
            result = db.get_recent_changes(args.changes)
            changed_files = result["changed_files"]
            changed_funcs = result["changed_functions"]

            # 只显示真正有变化的文件（有多个版本的）
            multi_version_files = [f for f in changed_files if f["version_num"] > 1]

            print(t("cli.messages.changes_title", since=args.changes))
            print(t("cli.messages.changes_file_versions", count=len(changed_files)))
            print(t("cli.messages.changes_multi_files", count=len(multi_version_files)))
            print(t("cli.messages.changed_funcs_count", count=len(changed_funcs)))
            print()

            if multi_version_files:
                print(t("cli.messages.changed_files_title"))
                for fv in multi_version_files:
                    parsed_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(fv["parsed_at"]))
                    current = t("cli.messages.history_current") if fv["is_current"] else ""
                    print(f"  v{fv['version_num']}{current} | {parsed_time} | {fv['path']}")

            if changed_funcs:
                print()
                print(t("cli.messages.changed_funcs_title"))
                for cf in changed_funcs:
                    parsed_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cf["parsed_at"]))
                    type_tag = f"[{cf['change_type']}]"
                    print(f"  {type_tag:4} {cf['qualified_name']}")
                    print(f"       {cf['file_path']}:{cf['line']} | {parsed_time}")

                    if args.changes_detail:
                        prev = cf['prev_hash']
                        curr = cf['curr_hash']
                        if prev:
                            print(f"       prev: {prev[:12]}...")
                        else:
                            print(t("cli.messages.changes_prev_none"))
                        if curr:
                            print(f"       curr: {curr[:12]}...")
                        else:
                            print(t("cli.messages.changes_curr_none"))
        
        elif args.restore_comment:
            result = db.restore_comment(args.restore_comment, preview=args.preview)

            if not result["success"]:
                print(t("cli.messages.restore_fail", error=result['error']))
            elif result.get("preview"):
                print(t("cli.messages.restore_preview_title"))
                print(t("cli.messages.restore_function", name=result['qualified_name']))
                print(t("cli.messages.restore_file", path=result['file_path']))
                print(t("cli.messages.restore_current_comment", comment=result['old_comment']))
                print(t("cli.messages.restore_new_comment"))
                print(result['new_comment'])
                print()
                print(t("cli.messages.restore_new_content_preview"))
                print(result['new_content_preview'])
            else:
                print(t("cli.messages.restore_success"))
                print(t("cli.messages.restore_function", name=result['qualified_name']))
                print(t("cli.messages.restore_file", path=result['file_path']))
                print(t("cli.messages.restore_from_version", version=result['restored_from'], lines=result['comment_lines']))

        elif args.restore_all_comments:
            file_filter = args.restore_file if args.restore_file else None
            result = db.restore_all_comments(preview=args.preview, file_filter=file_filter)

            mode = t("cli.messages.restore_all_mode_preview") if args.preview else t("cli.messages.restore_all_mode_restore")
            print(t("cli.messages.restore_all_done", mode=mode))
            print(t("cli.messages.restore_all_found", count=result['total_found']))
            print(t("cli.messages.restore_all_restored", count=result['restored']))
            print(t("cli.messages.restore_all_skipped", count=result['skipped']))
            print(t("cli.messages.restore_all_failed", count=result['failed']))
            print(t("cli.messages.restore_all_files", count=len(result['files'])))

            if result["files"]:
                print()
                print(t("cli.messages.restore_all_by_file_title"))
                for fpath, finfo in sorted(result["files"].items()):
                    if finfo["restored"] > 0 or finfo["failed"] > 0:
                        print(t("cli.messages.restore_all_file_item",
                               path=fpath, restored=finfo['restored'], skipped=finfo['skipped'],
                               failed=finfo['failed'], total=finfo['total']))

            if result["errors"]:
                print()
                print(t("cli.messages.restore_all_errors_title"))
                for err in result["errors"]:
                    print(f"  - {err}")

        elif args.comment_coverage:
            result = db.get_comment_coverage(group_by=args.coverage_by)

            print(t("cli.messages.comment_coverage_title"))
            print(t("cli.messages.comment_coverage_total", count=result['total']))
            print(t("cli.messages.comment_coverage_commented", count=result['commented']))
            print(t("cli.messages.comment_coverage_rate", pct=result['coverage']))
            print()

            print(t("cli.messages.comment_coverage_by_kind"))
            for kind, info in sorted(result["by_kind"].items(), key=lambda x: -x[1]["total"]):
                pct = round(info["commented"] / info["total"] * 100, 1) if info["total"] > 0 else 0
                bar_len = int(pct / 5)
                bar = "█" * bar_len + "░" * (20 - bar_len)
                print(f"  {bar} {pct:5.1f}%  {kind:12s}  ({info['commented']}/{info['total']})")

            if result.get("by_module"):
                print()
                print(t("cli.messages.comment_coverage_by_module"))
                modules = sorted(result["by_module"].items(), key=lambda x: x[1]["coverage"])
                for i, (mod, info) in enumerate(modules[:30]):
                    pct = info["coverage"]
                    bar_len = int(pct / 5)
                    bar = "█" * bar_len + "░" * (20 - bar_len)
                    print(f"  {bar} {pct:5.1f}%  {mod:50s}  ({info['commented']}/{info['total']})")
                if len(modules) > 30:
                    print(t("cli.messages.comment_coverage_more_modules", count=len(modules) - 30))

            if result.get("by_file"):
                print()
                print(t("cli.messages.comment_coverage_by_file"))
                files = sorted(result["by_file"].items(), key=lambda x: x[1]["coverage"])
                for i, (fpath, info) in enumerate(files[:30]):
                    pct = info["coverage"]
                    bar_len = int(pct / 5)
                    bar = "█" * bar_len + "░" * (20 - bar_len)
                    print(f"  {bar} {pct:5.1f}%  {fpath:50s}  ({info['commented']}/{info['total']})")
                if len(files) > 30:
                    print(t("cli.messages.comment_coverage_more_files", count=len(files) - 30))

        elif args.uncommented is not None:
            kind = args.uncommented
            mod_filter = args.uncommented_module
            limit = args.uncommented_limit

            symbols = db.get_uncommented_symbols(kind=kind, module_filter=mod_filter)

            filter_info = t("cli.messages.uncommented_module_filter", module=mod_filter) if mod_filter else ""
            print(t("cli.messages.uncommented_title", kind=kind, filter_info=filter_info,
                   total=len(symbols), shown=min(limit, len(symbols))))
            print()

            for i, sym in enumerate(symbols[:limit]):
                depth = sym["depth"] if sym["depth"] >= 0 else "?"
                sig = sym.get("signature", "")[:60] if sym.get("signature") else ""
                print(f"  [{i+1:3d}] depth={depth:>3}  {sym['qualified_name']}")
                print(f"         {sym['file_path']}:{sym['start_line']}")
                if sig:
                    print(f"         {sig}")

            if len(symbols) > limit:
                print()
                print(t("cli.messages.uncommented_more", count=len(symbols) - limit))
        
        elif args.search:
            kind = args.search_kind
            limit = args.search_limit

            symbols = db.search_symbols(args.search, kind=kind, limit=limit)

            kind_info = t("cli.messages.search_kind_info", kind=kind) if kind else ""
            print(t("cli.messages.search_title", query=args.search, kind_info=kind_info, total=len(symbols), shown=min(limit, len(symbols))))
            print()

            for i, sym in enumerate(symbols[:limit]):
                depth = sym["depth"] if sym["depth"] >= 0 else "?"
                sig = sym.get("signature", "")[:50] if sym.get("signature") else ""
                comment_mark = "✓" if sym["has_comment"] else " "
                print(f"  [{i+1:3d}] depth={depth:>3} [{comment_mark}] {sym['kind']:8s} {sym['qualified_name']}")
                print(f"         {sym['file_path']}:{sym['start_line']}")
                if sig:
                    print(f"         {sig}")

            if len(symbols) >= limit:
                print()
                print(t("cli.messages.search_more"))

        elif args.symbol:
            detail = db.get_symbol_detail(args.symbol)

            if not detail:
                print(t("cli.messages.symbol_not_found", name=args.symbol))
                print(t("cli.messages.symbol_search_hint"))
            else:
                print(t("cli.messages.symbol_detail_title"))
                print(t("cli.messages.symbol_name", name=detail['qualified_name']))
                print(t("cli.messages.symbol_kind", kind=detail['kind']))
                print(t("cli.messages.symbol_depth", depth=detail['depth']))
                file_loc = f"{detail['file_path']}:{detail['start_line']}-{detail['end_line']}"
                print(t("cli.messages.symbol_file", file=file_loc))
                sig = detail['signature'][:100] if detail['signature'] else None
                if sig:
                    print(t("cli.messages.symbol_signature", sig=sig))
                else:
                    print(t("cli.messages.symbol_signature_none"))
                if detail['has_comment']:
                    print(t("cli.messages.symbol_comment_yes"))
                else:
                    print(t("cli.messages.symbol_comment_no"))
                if detail.get("comment_content"):
                    print(t("cli.messages.symbol_comment_content"))
                    for line in detail["comment_content"].split("\n")[:10]:
                        print(f"    {line}")

                print()
                print(t("cli.messages.symbol_calls_out_title", count=len(detail['calls_out'])))
                if detail["calls_out"]:
                    for call in detail["calls_out"][:20]:
                        target = call["target_name"]
                        line = call.get("call_line", "")
                        line_info = f" (line {line})" if line else ""
                        print(f"  → {target}{line_info}")
                    if len(detail["calls_out"]) > 20:
                        print(t("cli.messages.symbol_more", count=len(detail['calls_out']) - 20))
                else:
                    print(t("cli.messages.symbol_none"))

                print()
                print(t("cli.messages.symbol_called_by_title", count=len(detail['called_by'])))
                if detail["called_by"]:
                    for call in detail["called_by"][:20]:
                        caller = call["caller_name"]
                        line = call.get("call_line", "")
                        line_info = f" (line {line})" if line else ""
                        print(f"  ← {caller}{line_info}")
                    if len(detail["called_by"]) > 20:
                        print(t("cli.messages.symbol_more", count=len(detail['called_by']) - 20))
                else:
                    print(t("cli.messages.symbol_none"))
        
        elif args.impact:
            result = db.get_call_chain_up(args.impact, max_depth=args.chain_depth)

            print(t("cli.messages.impact_up_title", name=result['start']))
            print(t("cli.messages.impact_up_total", count=result['total_upstream']))
            print(t("cli.messages.impact_up_max_depth", depth=result['max_depth_reached']))
            print()

            for level in result["levels"]:
                print(t("cli.messages.impact_up_level", depth=level['depth'], count=level['count']))
                for item in level["callers"][:15]:
                    print(f"  ← {item['caller']}")
                if level["count"] > 15:
                    print(t("cli.messages.impact_up_more", count=level['count'] - 15))
                print()

        elif args.call_chain:
            result = db.get_call_chain_down(args.call_chain, max_depth=args.chain_depth)

            print(t("cli.messages.call_chain_down_title", name=result['start']))
            print(t("cli.messages.call_chain_down_total", count=result['total_downstream']))
            print(t("cli.messages.call_chain_down_max_depth", depth=result['max_depth_reached']))
            print()

            for level in result["levels"]:
                print(t("cli.messages.call_chain_down_level", depth=level['depth'], count=level['count']))
                for item in level["callees"][:15]:
                    print(f"  → {item['callee']}")
                if level["count"] > 15:
                    print(t("cli.messages.call_chain_down_more", count=level['count'] - 15))
                print()
        
        elif args.top_callers is not None:
            limit = args.top_callers if args.top_callers else 20
            module_filter = args.top_callers_module or ""
            results = db.get_top_callers(limit=limit, module_filter=module_filter)

            if module_filter:
                print(t("cli.messages.top_callers_title_module", module=module_filter, count=len(results)))
            else:
                print(t("cli.messages.top_callers_title", count=len(results)))
            print()

            # 计算排名宽度
            rank_width = len(str(len(results)))

            for i, item in enumerate(results, 1):
                rank = str(i).rjust(rank_width)
                callers = t("cli.messages.top_callers_callers", count=item['caller_count'])
                calls = t("cli.messages.top_callers_calls", count=item['call_count'])
                print(f"  #{rank}  {item['qualified_name']}")
                print(f"        {callers} {calls}")
            print()

        elif args.orphan_symbols:
            kind = args.orphan_symbols
            module_filter = args.orphan_module or ""
            limit = args.orphan_limit
            results = db.get_orphan_symbols(kind=kind, module_filter=module_filter, limit=limit)

            if module_filter:
                print(t("cli.messages.orphan_title_module", kind=kind, module=module_filter, count=len(results)))
            else:
                print(t("cli.messages.orphan_title", kind=kind, count=len(results)))
            print()

            if results:
                # 按模块分组显示
                current_module = ""
                for item in results:
                    mod = item.get("module_path", "") or "(unknown)"
                    if mod != current_module:
                        current_module = mod
                        print(f"  [{current_module}]")
                    print(f"    {item['qualified_name']}")

                if len(results) >= limit:
                    print(t("cli.messages.orphan_more"))
            else:
                print(t("cli.messages.orphan_none"))
            print()

        elif args.deepest is not None:
            limit = args.deepest if args.deepest else 20
            module_filter = args.deepest_module or ""
            results = db.get_deepest_functions(limit=limit, module_filter=module_filter)

            if module_filter:
                print(t("cli.messages.deepest_title_module", module=module_filter, count=len(results)))
            else:
                print(t("cli.messages.deepest_title", count=len(results)))
            print()

            rank_width = len(str(len(results)))

            for i, item in enumerate(results, 1):
                rank = str(i).rjust(rank_width)
                print(t("cli.messages.deepest_item", default="  #{rank}  [depth {depth:2d}]  {name}", rank=rank, depth=item["depth"], name=item["qualified_name"]))
            print()

        elif args.module_calls is not None:
            limit = args.module_calls if args.module_calls else 20
            results = db.get_module_call_stats(limit=limit)

            print(t("cli.messages.module_calls_title", count=len(results)))
            print()

            # 计算列宽
            max_caller_len = max(len(r["caller_module"]) for r in results) if results else 0
            max_callee_len = max(len(r["callee_module"]) for r in results) if results else 0

            for i, item in enumerate(results, 1):
                caller = item["caller_module"].ljust(max_caller_len)
                callee = item["callee_module"].ljust(max_callee_len)
                print(t("cli.messages.module_calls_item", idx=i, caller=caller, callee=callee, calls=item['call_count'], callers=item['unique_caller_count'], callees=item['unique_callee_count']))
            print()

        elif args.detect_cycles:
            cycles = db.detect_cycles(max_depth=args.cycle_depth)

            print(t("cli.messages.cycles_title"))
            print(t("cli.messages.cycles_max_depth", depth=args.cycle_depth))
            print(t("cli.messages.cycles_count", count=len(cycles)))
            print()

            if cycles:
                # 按环的长度排序
                cycles_sorted = sorted(cycles, key=lambda c: len(c) - 1)

                for i, cycle in enumerate(cycles_sorted[:20], 1):
                    cycle_len = len(cycle) - 1  # 减去重复的结尾
                    print(t("cli.messages.cycles_item", idx=i, len=cycle_len))
                    for j, fn in enumerate(cycle):
                        arrow = " → " if j < len(cycle) - 1 else ""
                        print(f"      {fn}{arrow}")
                    print()

                if len(cycles) > 20:
                    print(t("cli.messages.cycles_more", count=len(cycles) - 20))
                    print()
            else:
                print(t("cli.messages.cycles_none"))
                print()

        elif args.export_module_graph:
            fmt = args.export_module_graph
            output_file = args.graph_output or ""

            if fmt not in ("mermaid", "dot"):
                print(t("cli.messages.module_graph_unsupported", fmt=fmt))
            else:
                result = db.export_module_graph(format=fmt, output_file=output_file)

                if output_file:
                    print(t("cli.messages.module_graph_exported", file=output_file))
                    print(t("cli.messages.module_graph_format", fmt=fmt))
                else:
                    print(t("cli.messages.module_graph_title", fmt=fmt))
                    print()
                    print(result)
                print()

        elif args.call_heatmap:
            group_by = args.call_heatmap
            top_n = args.heatmap_limit

            if group_by not in ("module", "file"):
                print(t("cli.messages.heatmap_unsupported", group=group_by))
            else:
                results = db.get_call_heatmap(group_by=group_by, top_n=top_n)

                unit = t("cli.messages.heatmap_unit_module") if group_by == "module" else t("cli.messages.heatmap_unit_file")
                print(t("cli.messages.heatmap_title", unit=unit, count=len(results)))
                print()

                if results:
                    max_calls = max(r["total_calls"] for r in results)
                    max_group_len = max(len(r["group"]) for r in results)

                    # 热力图标度：用不同字符表示密度
                    heat_chars = " ▁▂▃▄▅▆▇█"

                    for i, item in enumerate(results, 1):
                        # 计算热力等级（0-8）
                        ratio = item["total_calls"] / max_calls if max_calls > 0 else 0
                        heat_level = min(int(ratio * 8), 8)
                        heat_bar = heat_chars[heat_level] * (heat_level + 1)

                        group_name = item["group"].ljust(max_group_len)
                        print(t(
                            "cli.messages.heatmap_item",
                            default="  #{idx:2d}  {group}  {bar}  {calls:4d} calls  ({callers} callers, {callees} callees)",
                            idx=i,
                            group=group_name,
                            bar=heat_bar,
                            calls=item["total_calls"],
                            callers=item["unique_callers"],
                            callees=item["unique_callees"],
                        ))
                else:
                    print(t("cli.messages.heatmap_none"))
                print()
        
        elif args.test_coverage:
            stats = db.get_test_coverage()

            print(t("cli.messages.test_coverage_title"))
            print()
            print(t("cli.messages.test_coverage_total_fns", count=stats['total_functions']))
            print(t("cli.messages.test_coverage_test_fns", count=stats['test_functions']))
            print(t("cli.messages.test_coverage_ratio", pct=stats['test_ratio']))
            print()
            print(t("cli.messages.test_coverage_total_mods", count=stats['total_modules']))
            print(t("cli.messages.test_coverage_mods_with_tests", count=stats['modules_with_tests']))
            print(t("cli.messages.test_coverage_mod_ratio", pct=stats['module_coverage']))
            print()

            if stats["test_by_module"]:
                print(t("cli.messages.test_coverage_dist_title"))
                print()

                max_test_count = max(m["test_count"] for m in stats["test_by_module"])
                max_mod_len = max(len(m["module"]) for m in stats["test_by_module"][:20])

                for i, mod in enumerate(stats["test_by_module"][:20], 1):
                    bar_len = int(mod["test_count"] / max_test_count * 30) if max_test_count > 0 else 0
                    bar = "█" * bar_len
                    mod_name = mod["module"].ljust(max_mod_len)
                    print(f"  #{i:2d}  {mod_name}  {bar}  {mod['test_count']:3d} {t('cli.messages.test_coverage_test_count', count='')}".rstrip())

                if len(stats["test_by_module"]) > 20:
                    print(t("cli.messages.test_coverage_more", count=len(stats['test_by_module']) - 20))
            print()

        elif args.function_issues is not None:
            fn_name = args.function_issues
            module_filter = args.issue_module or ""
            issue_filter = args.issue_type or ""
            limit = args.issue_limit

            results = db.get_function_issues(
                qualified_name=fn_name,
                module_filter=module_filter,
                issue_filter=issue_filter,
                limit=limit,
            )

            # 严重程度标记
            severity_icon = {"danger": "[!]", "warn": "[~]", "info": "[i]"}

            if fn_name:
                # 单函数详情模式
                if results:
                    r = results[0]
                    print(t("cli.messages.function_issues_title", name=r['qualified_name']))
                    print(t("cli.messages.function_issues_module", module=r['module_path'] or '(unknown)'))
                    print(t("cli.messages.function_issues_count", count=r['issue_count']))
                    print()
                    for issue in r["issues"]:
                        icon = severity_icon.get(issue["severity"], "[?]")
                        print(f"  {icon} {issue['label']}  (x{issue['count']})")
                        print(f"      {issue['description']}")
                    print()
                else:
                    print(t("cli.messages.function_issues_title", name=fn_name))
                    filter_str = t("cli.messages.function_issues_filter", filter=issue_filter) if issue_filter else ""
                    print(t("cli.messages.function_issues_no_issues") + filter_str)
                    print()
            else:
                # 列表模式
                if issue_filter:
                    print(t("cli.messages.function_issues_list_title_type", filter=issue_filter, count=len(results)))
                elif module_filter:
                    print(t("cli.messages.function_issues_list_title_module", module=module_filter, count=len(results)))
                else:
                    print(t("cli.messages.function_issues_list_title", count=len(results)))
                print()

                for i, r in enumerate(results, 1):
                    issue_labels = []
                    for issue in r["issues"]:
                        icon = severity_icon.get(issue["severity"], "")
                        issue_labels.append(f"{icon}{issue['label']}" + (f"(x{issue['count']})" if issue["count"] > 1 else ""))

                    issue_str = "  ".join(issue_labels)
                    print(f"  #{i:2d}  {r['qualified_name']}")
                    print(f"        {issue_str}")
                print()

        elif args.issue_summary:
            module_filter = args.issue_module or ""
            stats = db.get_issue_summary(module_filter=module_filter)

            if module_filter:
                print(t("cli.messages.issue_summary_title_module", module=module_filter))
            else:
                print(t("cli.messages.issue_summary_title"))
            print()
            print(t("cli.messages.issue_summary_total_fns", count=stats['total_functions']))
            print(t("cli.messages.issue_summary_with_issues", count=stats['functions_with_issues']))
            print(t("cli.messages.issue_summary_issue_free", count=stats['issue_free_functions'], pct=stats['issue_free_ratio']))
            print()

            severity_icon = {"danger": "[!]", "warn": "[~]", "info": "[i]"}

            print(t("cli.messages.issue_summary_dist_title"))
            print()

            # 按严重程度分组
            for severity in ["danger", "warn", "info"]:
                severity_issues = [i for i in stats["issues"] if i["severity"] == severity and i["function_count"] > 0]
                if severity_issues:
                    if severity == "danger":
                        severity_label = t("cli.messages.issue_summary_severity_danger")
                    elif severity == "warn":
                        severity_label = t("cli.messages.issue_summary_severity_warn")
                    else:
                        severity_label = t("cli.messages.issue_summary_severity_info")
                    print(f"  [{severity_label}]")
                    for issue in severity_issues:
                        icon = severity_icon.get(issue["severity"], "")
                        bar_len = int(issue["function_count"] / stats["total_functions"] * 40) if stats["total_functions"] > 0 else 0
                        bar = "█" * bar_len
                        print(t(
                            "cli.messages.issue_summary_dist_item",
                            default="    {icon} {label:<14s}  {bar} {function_count:4d} functions ({ratio}%)  {occurrences} occurrences",
                            icon=icon,
                            label=issue["label"],
                            bar=bar,
                            function_count=issue["function_count"],
                            ratio=issue["ratio"],
                            occurrences=issue["total_occurrences"],
                        ))
                    print()

            # 显示零缺陷的函数
            zero_issues = [i for i in stats["issues"] if i["function_count"] == 0]
            if zero_issues:
                print(t("cli.messages.issue_summary_zero_title"))
                for issue in zero_issues:
                    print(t("cli.messages.issue_summary_zero_item", label=issue['label']))
                print()
        
        elif args.semgrep is not None:
            # Semgrep 多语言静态分析
            target_paths = args.semgrep if args.semgrep else None  # None 表示扫描整个 workspace
            config = args.semgrep_config
            languages = args.semgrep_scan_lang
            timeout = args.semgrep_timeout

            print(t("cli.messages.semgrep_title"))
            print(t("cli.messages.semgrep_config_label", config=config))
            if languages:
                print(t("cli.messages.semgrep_lang_limit", langs=", ".join(languages)))
            print(t("cli.messages.semgrep_timeout_label", timeout=timeout))
            print()

            if args.semgrep_save:
                # 扫描并存入数据库
                result = db.run_semgrep_and_save(
                    target_paths=target_paths or [db.workspace_root],
                    config=config,
                    languages=languages,
                    timeout=timeout,
                )
                if not result.get("success"):
                    print(t("cli.messages.semgrep_error", error=result.get('error', t("cli.messages.semgrep_unknown_error"))))
                else:
                    print(t("cli.messages.semgrep_scan_done", count=result['total_findings']))
                    print(t("cli.messages.semgrep_saved", count=result['saved_findings']))
                    print()
                    print(t("cli.messages.semgrep_save_hint"))

            elif args.semgrep_quick:
                # 快速扫描（只显示汇总）
                result = db.get_semgrep_summary(target_paths)

                if not result.get("success"):
                    print(t("cli.messages.semgrep_error", error=result.get('error', t("cli.messages.semgrep_unknown_error"))))
                else:
                    print(t("cli.messages.semgrep_quick_total", count=result['total_findings']))
                    print()

                    # 按严重程度展示
                    if result.get("by_severity"):
                        print(t("cli.messages.semgrep_severity_dist"))
                        for sev in ["ERROR", "WARNING", "INFO"]:
                            count = result["by_severity"].get(sev, 0)
                            if count > 0:
                                icon = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}[sev]
                                print(t("cli.messages.semgrep_severity_count", icon=icon, sev=sev, count=count))
                        print()

                    # 按语言展示
                    if result.get("by_language"):
                        print(t("cli.messages.semgrep_lang_dist"))
                        for lang, count in sorted(result["by_language"].items(), key=lambda x: x[1], reverse=True):
                            print(t("cli.messages.semgrep_lang_count", lang=lang, count=count))
                        print()

                    # Top 规则
                    if result.get("top_rules"):
                        print(t("cli.messages.semgrep_top_rules"))
                        for rule_id, stats in result["top_rules"][:10]:
                            sev_icon = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}[stats.get("severity", "INFO")]
                            print(t("cli.messages.semgrep_rule_item", icon=sev_icon, rule=rule_id, count=stats['count']))
                            print(f"        {stats['message'][:80]}...")
                        print()

                if result.get("errors"):
                    print(t("cli.messages.semgrep_warning_count", count=len(result['errors'])))

            else:
                # 详细扫描
                result = db.run_semgrep(
                    target_paths=target_paths or [db.workspace_root],
                    config=config,
                    languages=languages,
                    timeout=timeout,
                )

                if not result.get("success"):
                    print(t("cli.messages.semgrep_error", error=result.get('error', t("cli.messages.semgrep_unknown_error"))))
                else:
                    print(t("cli.messages.semgrep_scan_done", count=result['total_findings']))
                    print()

                    # 按严重程度分组展示
                    severity_icon = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}

                    for sev in ["ERROR", "WARNING", "INFO"]:
                        sev_findings = [f for f in result["results"] if f["severity"] == sev]
                        if sev_findings:
                            icon = severity_icon[sev]
                            if sev == "ERROR":
                                sev_label = t("cli.messages.semgrep_sev_error")
                            elif sev == "WARNING":
                                sev_label = t("cli.messages.semgrep_sev_warning")
                            else:
                                sev_label = t("cli.messages.semgrep_sev_info")
                            print(t("cli.messages.semgrep_detail_title", label=sev_label, count=len(sev_findings)))
                            print()

                            for f in sev_findings[:15]:
                                print(f"    {icon} {f['rule_name']}")
                                print(t("cli.messages.semgrep_finding_file", file=f['path'], line=f['start_line']))
                                print(t("cli.messages.semgrep_finding_lang", lang=f['language']))
                                print(t("cli.messages.semgrep_finding_msg", msg=f['message'][:100]))
                                if f.get("fix"):
                                    print(t("cli.messages.semgrep_fix_hint", fix=f['fix'][:50]))
                                print()

                            if len(sev_findings) > 15:
                                print(t("cli.messages.semgrep_more", count=len(sev_findings) - 15))
                                print()

            print(t("cli.messages.semgrep_hint"))
            print()

        elif args.semgrep_stats:
            stats = db.get_semgrep_stats()
            print(t("cli.messages.semgrep_stats_title"))
            print(t("cli.messages.semgrep_stats_total", count=stats['total_findings']))
            print()

            if stats["by_severity"]:
                print(t("cli.messages.semgrep_stats_by_sev"))
                for sev in ["ERROR", "WARNING", "INFO"]:
                    count = stats["by_severity"].get(sev, 0)
                    if count > 0:
                        icon = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}[sev]
                        print(t("cli.messages.semgrep_severity_count", icon=icon, sev=sev, count=count))
                print()

            if stats["by_language"]:
                print(t("cli.messages.semgrep_stats_by_lang"))
                for lang, count in sorted(stats["by_language"].items(), key=lambda x: x[1], reverse=True):
                    print(f"    {lang:<15s} {count:4d}")
                print()

            if stats["by_rule"]:
                print(t("cli.messages.semgrep_stats_top_rules"))
                for i, rule in enumerate(stats["by_rule"][:10], 1):
                    sev_icon = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}.get(rule["severity"], "[?]")
                    print(f"    #{i:2d} {sev_icon} {rule['rule_id'][:50]:<50s}  {rule['cnt']:3d}")
                print()

            if stats["by_symbol"]:
                print(t("cli.messages.semgrep_stats_top_symbols"))
                for i, sym in enumerate(stats["by_symbol"][:10], 1):
                    print(f"    #{i:2d} {sym['symbol_qualified'][:60]:<60s}  {sym['cnt']:2d}")
                print()

        elif args.semgrep_list is not None:
            rule_filter = args.semgrep_list if args.semgrep_list else ""
            severity = args.semgrep_severity or ""
            language = args.semgrep_list_lang or ""
            limit = args.semgrep_limit

            findings = db.get_semgrep_findings(
                severity=severity,
                language=language,
                rule_id=rule_filter,
                limit=limit,
            )

            filter_parts = []
            if severity:
                filter_parts.append(t("cli.messages.semgrep_list_filter_sev", sev=severity))
            if language:
                filter_parts.append(t("cli.messages.semgrep_list_filter_lang", lang=language))
            if rule_filter:
                filter_parts.append(t("cli.messages.semgrep_list_filter_rule", rule=rule_filter))
            filter_str = " | ".join(filter_parts) if filter_parts else t("cli.messages.semgrep_list_filter_all")

            print(t("cli.messages.semgrep_list_title", filter=filter_str, total=len(findings), shown=min(limit, len(findings))))
            print()

            sev_icon_map = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}

            for i, f in enumerate(findings[:limit], 1):
                icon = sev_icon_map.get(f["severity"], "[?]")
                sym_info = f" -> {f['symbol_qualified']}" if f["symbol_qualified"] else ""
                print(f"  #{i:3d} {icon} {f['rule_name'][:40]:<40s} {f['language']:<12s}{sym_info}")
                print(f"        {f['file_path']}:{f['start_line']}")
                print(f"        {f['message'][:80]}")
                print()

            if len(findings) > limit:
                print(t("cli.messages.semgrep_list_more", count=len(findings) - limit))
        
        elif args.git_import is not None:
            max_commits = args.git_import if args.git_import else 100
            print(t("cli.messages.git_import_start", count=max_commits))
            result = db.import_git_history(max_commits=max_commits)
            if result.get("success"):
                print(t("cli.messages.git_import_success", count=result['commits_imported']))
                print(t("cli.messages.git_import_total", count=result['total_commits']))
            else:
                print(t("cli.messages.git_import_fail", error=result.get('error', t("cli.messages.semgrep_unknown_error"))))

        elif args.git_log is not None:
            limit = args.git_log if args.git_log else 20
            commits = db.get_git_commits(limit=limit)
            print(t("cli.messages.git_log_title", count=len(commits)))
            print()
            for c in commits:
                short_hash = c['commit_hash'][:8]
                timestamp = time.strftime('%Y-%m-%d %H:%M', time.localtime(c['timestamp']))
                msg = c['message'][:60] if c['message'] else t("cli.messages.git_log_no_msg")
                author = c['author'][:15] if c['author'] else 'unknown'
                print(f"  {short_hash}  {timestamp}  {author:<15s}  {msg}")

        elif args.git_show:
            details = db.get_commit_changes(args.git_show)
            commit = details.get("commit")
            if not commit:
                print(t("cli.messages.git_show_not_found", hash=args.git_show))
            else:
                print(t("cli.messages.git_show_commit", hash=commit['commit_hash']))
                print(t("cli.messages.git_show_author", author=commit['author'], email=commit['email']))
                print(t("cli.messages.git_show_time", time=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(commit['timestamp']))))
                print(t("cli.messages.git_show_message", msg=commit['message']))
                print()
                file_changes = details.get("file_changes", [])
                print(t("cli.messages.git_show_files", count=len(file_changes)))
                type_map = {'A': t("cli.messages.git_type_added"), 'M': t("cli.messages.git_type_modified"), 'D': t("cli.messages.git_type_deleted"), 'R': t("cli.messages.git_type_renamed")}
                for fc in file_changes:
                    ct = fc.get('change_type', '?')
                    type_label = type_map.get(ct, ct)
                    path = fc.get('rel_path') or fc.get('abs_path') or 'unknown'
                    print(f"  [{type_label}] {path}")

        elif args.git_stats:
            stats = db.get_git_stats()
            print(t("cli.messages.git_stats_title"))
            print(t("cli.messages.git_stats_commits", count=stats['commit_count']))
            print(t("cli.messages.git_stats_file_changes", count=stats['file_change_count']))
            print()
            if stats.get("change_types"):
                print(t("cli.messages.git_stats_by_type"))
                type_map = {'A': t("cli.messages.git_type_added"), 'M': t("cli.messages.git_type_modified"), 'D': t("cli.messages.git_type_deleted"), 'R': t("cli.messages.git_type_renamed")}
                for ct, cnt in sorted(stats["change_types"].items(), key=lambda x: x[1], reverse=True):
                    label = type_map.get(ct, ct)
                    print(t("cli.messages.git_stats_type_count", default="    {label}: {count} times", label=label, count=cnt))
        
        # ----------------------------------------------------------------
        # 代码度量
        # ----------------------------------------------------------------
        
        elif args.metrics:
            summary = db.get_code_metrics_summary()
            print(t("cli.messages.metrics_title"))
            print(t("cli.messages.metrics_files", count=summary['file_count']))
            print(t("cli.messages.metrics_functions", count=summary['function_count']))
            print(t("cli.messages.metrics_total_lines", count=summary['total_lines']))
            print(t("cli.messages.metrics_calls", count=summary['total_calls']))
            print()
            print(t("cli.messages.metrics_avg_complexity", value=summary['avg_complexity']))
            print(t("cli.messages.metrics_max_complexity", value=summary['max_complexity']))
            print()
            print(t("cli.messages.metrics_complexity_dist"))
            dist = summary["complexity_distribution"]
            total_fn = sum(dist.values()) or 1
            for level, count in dist.items():
                pct = count / total_fn * 100
                bar = "#" * int(pct / 2)
                print(f"    {level:<12s} {count:4d} ({pct:5.1f}%) {bar}")
            print()
            print(t("cli.messages.metrics_comment_coverage", pct=summary['comment_coverage']))

        elif args.complexity is not None:
            limit = args.complexity if args.complexity else 20
            mod_filter = args.complexity_module or ""
            hotspots = db.get_complexity_hotspots(limit=limit, module_filter=mod_filter)

            filter_info = t("cli.messages.complexity_filter", module=mod_filter) if mod_filter else ""
            print(t("cli.messages.complexity_title", filter_info=filter_info, count=len(hotspots)))
            print()
            complexity_h = t("cli.messages.col_complexity", default="Complexity")
            lines_h = t("cli.messages.col_lines", default="Lines")
            depth_h = t("cli.messages.col_depth", default="Depth")
            fn_h = t("cli.messages.col_function", default="Function")
            print(f"  {'#':>3}  {complexity_h:>6}  {lines_h:>5}  {depth_h:>4}  {fn_h}")
            print(f"  {'-'*3}  {'-'*6}  {'-'*5}  {'-'*4}  {'-'*50}")

            for i, fn in enumerate(hotspots, 1):
                risk = "!" if fn["cyclomatic_complexity"] > 10 else " "
                print(f"  {i:3d}{risk}  {fn['cyclomatic_complexity']:>6}  {fn['line_count']:>5}  {fn['depth']:>4}  {fn['qualified_name'][:60]}")
                print(f"        {fn['file_path']}:{fn['start_line']}")
            print()
            print(t("cli.messages.complexity_hint"))

        elif args.coupling:
            modules = db.get_coupling_analysis(limit=30)
            print(t("cli.messages.coupling_title", count=len(modules)))
            print()
            module_h = t("cli.messages.col_module", default="Module")
            afferent_h = t("cli.messages.col_afferent", default="In")
            efferent_h = t("cli.messages.col_efferent", default="Out")
            total_h = t("cli.messages.col_total", default="Total")
            instability_h = t("cli.messages.col_instability", default="Instab")
            print(f"  {'#':>3}  {module_h:<40s}  {afferent_h:>4}  {efferent_h:>4}  {total_h:>4}  {instability_h:>6}")
            print(f"  {'-'*3}  {'-'*40}  {'-'*4}  {'-'*4}  {'-'*4}  {'-'*6}")

            for i, mod in enumerate(modules, 1):
                inst = mod["instability"]
                inst_label = f"{inst:.2f}"
                if inst > 0.7:
                    inst_label += t("cli.messages.coupling_unstable")
                elif inst < 0.3:
                    inst_label += t("cli.messages.coupling_stable")
                print(f"  {i:3d}  {mod['module'][:40]:<40s}  {mod['afferent']:>4}  {mod['efferent']:>4}  {mod['total_coupling']:>4}  {inst_label:>6}")

        elif args.largest_fns is not None:
            limit = args.largest_fns if args.largest_fns else 20
            fns = db.get_largest_functions(limit=limit)
            print(t("cli.messages.largest_fns_title", count=len(fns)))
            print()
            lines_h = t("cli.messages.col_lines", default="Lines")
            depth_h = t("cli.messages.col_depth", default="Depth")
            fn_h = t("cli.messages.col_function", default="Function")
            print(f"  {'#':>3}  {lines_h:>5}  {depth_h:>4}  {fn_h}")
            print(f"  {'-'*3}  {'-'*5}  {'-'*4}  {'-'*50}")

            for i, fn in enumerate(fns, 1):
                print(f"  {i:3d}  {fn['line_count']:>5}  {fn['depth']:>4}  {fn['qualified_name'][:60]}")
                print(f"        {fn['file_path']}:{fn['start_line']}")

        elif args.coupled_fns is not None:
            limit = args.coupled_fns if args.coupled_fns else 20
            fns = db.get_most_coupled_functions(limit=limit)
            print(t("cli.messages.coupled_fns_title", count=len(fns)))
            print()
            fan_in_h = t("cli.messages.col_fan_in", default="Fan-in")
            fan_out_h = t("cli.messages.col_fan_out", default="Fan-out")
            total_h = t("cli.messages.col_total", default="Total")
            fn_h = t("cli.messages.col_function", default="Function")
            print(f"  {'#':>3}  {fan_in_h:>4}  {fan_out_h:>4}  {total_h:>4}  {fn_h}")
            print(f"  {'-'*3}  {'-'*4}  {'-'*4}  {'-'*4}  {'-'*50}")

            for i, fn in enumerate(fns, 1):
                print(f"  {i:3d}  {fn['fan_in']:>4}  {fn['fan_out']:>4}  {fn['total_coupling']:>4}  {fn['qualified_name'][:60]}")
                print(f"        {fn['file_path']}")

        elif args.fn_metrics:
            metrics = db.get_function_metrics(args.fn_metrics)
            if not metrics:
                print(t("cli.messages.fn_metrics_not_found", name=args.fn_metrics))
                print(t("cli.messages.fn_metrics_search_hint"))
            else:
                print(t("cli.messages.fn_metrics_title", name=metrics['qualified_name']))
                print(t("cli.messages.fn_metrics_kind", kind=metrics['kind']))
                print(t("cli.messages.fn_metrics_file", file=metrics['file_path'], start=metrics['start_line'], end=metrics['end_line']))
                print(t("cli.messages.fn_metrics_lines", count=metrics['line_count']))
                print(t("cli.messages.fn_metrics_complexity", value=metrics['cyclomatic_complexity'], risk=metrics['risk_level']))
                print(t("cli.messages.fn_metrics_fan_in", count=metrics['fan_in']))
                print(t("cli.messages.fn_metrics_fan_out", count=metrics['fan_out']))
                print(t("cli.messages.fn_metrics_depth", depth=metrics['depth']))
                print(t("cli.messages.fn_metrics_module", module=metrics['module_path']))
                if metrics['signature']:
                    print(t("cli.messages.fn_metrics_signature", sig=metrics['signature'][:100]))

        # ----------------------------------------------------------------
        # 语义搜索 / 向量嵌入
        # ----------------------------------------------------------------

        elif args.semantic_search:
            query = args.semantic_search
            print(t("cli.messages.semantic_title", query=query))
            print("-" * 50)
            results = db.semantic_search(query, top_k=10)
            if not results:
                print(t("cli.messages.semantic_no_match"))
                print(t("cli.messages.semantic_hint"))
            else:
                for i, r in enumerate(results, 1):
                    print(t("cli.messages.semantic_similarity", idx=i, value=r['similarity'], name=r['qualified_name']))
                    print(t("cli.messages.semantic_location", file=r['file_path'], line=r['start_line']))
                    if r.get('summary'):
                        print(t("cli.messages.semantic_summary", summary=r['summary'][:80]))
            print()

        elif args.embed or args.embed_force:
            force = args.embed_force
            mode = t("cli.messages.embed_mode_force") if force else t("cli.messages.embed_mode_incremental")
            print(t("cli.messages.embed_title", mode=mode))
            print("-" * 50)
            stats = db.embed_all_symbols(force=force)
            print(t("cli.messages.embed_total", count=stats['total']))
            print(t("cli.messages.embed_success", count=stats['success']))
            print(t("cli.messages.embed_skipped", count=stats['skipped']))
            print(t("cli.messages.embed_failed", count=stats['failed']))
            if stats['success'] == 0 and stats['total'] > 0:
                print()
                print(t("cli.messages.embed_hint"))
            print()

        elif args.similar:
            name = args.similar
            print(t("cli.messages.similar_title", name=name))
            print("-" * 50)
            results = db.find_similar_functions(name, threshold=0.7)
            if not results:
                print(t("cli.messages.similar_no_match"))
                print(t("cli.messages.similar_hint"))
            else:
                for i, r in enumerate(results, 1):
                    print(t("cli.messages.semantic_similarity", idx=i, value=r['similarity'], name=r['qualified_name']))
                    print(t("cli.messages.semantic_location", file=r['file_path'], line=r['start_line']))
                    if r.get('summary'):
                        print(t("cli.messages.semantic_summary", summary=r['summary'][:80]))
            print()

        # ----------------------------------------------------------------
        # 任务管理
        # ----------------------------------------------------------------

        elif args.task_list:
            # --task-list 作为兼容入口，内部转调 _handle_task list
            # 保证与 `cw task list` 行为完全一致，避免两套实现产生分歧
            cprint(t("cli.messages.task_list_deprecated_hint"), "yellow")
            return _handle_task(["list"], db)

        elif args.task_show:
            # --task-show 作为兼容入口，默认按树形展示子任务
            # 等价于 `cw task show TASK_ID`
            cprint(t("cli.messages.task_show_deprecated_hint"), "yellow")
            return _print_task_show(db, args.task_show, flat=False)

        # ----------------------------------------------------------------
        # 项目简报和仓库地图
        # ----------------------------------------------------------------

        elif args.brief:
            brief = db.project_brief()
            print(t("cli.messages.brief_title"))
            print()
            print(t("cli.messages.brief_project_type", type=brief['project_type']))
            print(t("cli.messages.brief_files", count=brief['file_count']))
            print(t("cli.messages.brief_functions", count=brief['function_count']))
            print(t("cli.messages.brief_total_lines", count=brief['total_lines']))
            print(t("cli.messages.brief_health", score=brief['health_score'], level=brief['health_level']))
            print(t("cli.messages.brief_avg_complexity", value=brief['avg_complexity']))
            print(t("cli.messages.brief_comment_coverage", pct=brief['comment_coverage']))
            print()
            modules = brief.get('modules', [])
            if modules:
                print(t("cli.messages.brief_modules", count=len(modules)))
                for i, m in enumerate(modules, 1):
                    print(t("cli.messages.brief_module_item", idx=i, module=m['module'], count=m['function_count']))
                print()
            hotspots = brief.get('hot_functions', [])
            if hotspots:
                print(t("cli.messages.brief_hotspots", count=len(hotspots)))
                for i, fn in enumerate(hotspots, 1):
                    print(t("cli.messages.brief_hotspot_item", idx=i, value=fn['cyclomatic_complexity'], name=fn['qualified_name']))
            print()

        elif args.map:
            output = db.repo_map(format=args.map_format)
            print(t("cli.messages.map_title", format=args.map_format))
            print()
            print(output)
            print()

        # ----------------------------------------------------------------
        # 覆盖率导入与查询
        # ----------------------------------------------------------------

        elif args.coverage_import:
            file_path = args.coverage_import
            fmt = args.coverage_format
            print(t("cli.messages.coverage_import_title", file=file_path, format=fmt))
            print("-" * 50)
            try:
                if fmt == "lcov":
                    stats = db.import_lcov(file_path)
                else:
                    stats = db.import_cobertura(file_path)
                print(t("cli.messages.coverage_import_files_total", count=stats['files_total']))
                print(t("cli.messages.coverage_import_files_matched", count=stats['files_matched']))
                print(t("cli.messages.coverage_import_lines", count=stats['lines_imported']))
                print(t("cli.messages.coverage_import_symbols", count=stats['symbols_matched']))
            except FileNotFoundError:
                print(t("cli.messages.coverage_import_file_not_found", file=file_path))
            except Exception as e:
                print(t("cli.messages.coverage_import_parse_error", error=e))
            print()

        elif args.coverage_fn:
            name = args.coverage_fn
            info = db.get_coverage_for_symbol(name)
            if not info:
                print(t("cli.messages.coverage_fn_not_found", name=name))
                print(t("cli.messages.coverage_fn_search_hint"))
            else:
                print(t("cli.messages.coverage_fn_title", name=info['qualified_name']))
                print("-" * 50)
                print(t("cli.messages.coverage_fn_file", file=info['file_path'], start=info['start_line'], end=info['end_line']))
                print(t("cli.messages.coverage_fn_total", count=info['total_lines']))
                print(t("cli.messages.coverage_fn_tracked", count=info['tracked_lines']))
                print(t("cli.messages.coverage_fn_covered", count=info['covered_lines']))
                print(t("cli.messages.coverage_fn_pct", pct=info['coverage_pct']))
                if info['uncovered_lines']:
                    lines_preview = info['uncovered_lines'][:30]
                    more = '...' if len(info['uncovered_lines']) > 30 else ''
                    print(t("cli.messages.coverage_fn_uncovered", lines=lines_preview, more=more))
            print()

        elif args.coverage_uncovered:
            results = db.find_uncovered_functions()
            print(t("cli.messages.coverage_uncovered_title", count=len(results)))
            print("-" * 50)
            for i, r in enumerate(results, 1):
                pct_label = t("cli.messages.coverage_fn_pct", pct="").strip().rstrip(":").strip()
                print(f"  [{i:3d}] {pct_label}={r['coverage_pct']:5.1f}%  {r['qualified_name']}")
                print(t("cli.messages.coverage_uncovered_item", file=r['file_path'], start=r['start_line'], end=r['end_line'], covered=r['covered_lines'], tracked=r['tracked_lines']))
            print()

        # ----------------------------------------------------------------
        # 所有权查询
        # ----------------------------------------------------------------

        elif args.who:
            info = db.who_to_ask(args.who)
            if not info:
                print(t("cli.messages.who_not_found", file=args.who))
                print(t("cli.messages.who_hint"))
            else:
                print(t("cli.messages.who_title"))
                print("-" * 50)
                print(t("cli.messages.who_file", file=info['file_path']))
                print(t("cli.messages.who_owner", owner=info['owner']))
                print(t("cli.messages.who_source", source=info['source']))
                print(t("cli.messages.who_confidence", confidence=info['confidence']))
                if info.get('last_commit_author'):
                    print(t("cli.messages.who_last_author", author=info['last_commit_author']))
                if info.get('last_commit_time'):
                    ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(info['last_commit_time']))
                    print(t("cli.messages.who_last_time", time=ts))
                if info.get('last_commit_hash'):
                    print(t("cli.messages.who_last_hash", hash=info['last_commit_hash'][:12]))
            print()

        elif args.ownership_map:
            results = db.get_ownership_map()
            print(t("cli.messages.ownership_map_title", count=len(results)))
            print("-" * 50)
            for i, m in enumerate(results, 1):
                print(f"  [{i}] {m['module']}")
                print(t("cli.messages.ownership_map_primary", owner=m['primary_owner'], count=m['file_count']))
                owners_str = ", ".join(f"{o['name']}({o['file_count']})" for o in m['owners'][:5])
                print(t("cli.messages.ownership_map_dist", owners=owners_str))
                if len(m['owners']) > 5:
                    print(t("cli.messages.ownership_map_more", count=len(m['owners']) - 5))
            print()

        else:
            parser.print_help()
    
    finally:
        db.close()


if __name__ == "__main__":
    main()
