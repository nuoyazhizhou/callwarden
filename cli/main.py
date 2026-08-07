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
import sqlite3
import sys
import time

from ..db import CodeGraphDB
from ..config import detect_project_root, get_default_workspace_name, atomic_write_file, AUTO_SETUP_MARKER
from ..server.watcher import FileWatcher
from ..server.daemon_client import route_task_write, route_task_read, DaemonUnavailableError
from ..i18n import t, set_language, get_arg_help, get_msg, get_error, DEFAULT_LANG
from .console import cprint
from .agent_registry import get_merged_specs


# ====================================================================
# 代码守护者架构子命令（四大支柱）
# ====================================================================

# 子命令关键字集合
_SUBCOMMANDS = {"guardrail", "impact", "review", "evolution", "hotspot", "churn", "defect",
                "task", "vuln-blast", "symbol-history", "check-gate", "test-impact",
                "gc", "doctor", "install-agent", "install-hook", "rule", "audit", "bootstrap",
                "clone", "fts", "identity", "lease", "assignment",
                # C8 Step #1: 新增 8 大类 subcommand 入口（保留旧 flag 兼容）
                "workspace", "refresh", "stats", "status",
                "search", "grep", "symbol", "file", "query", "issues", "tests",
                "callers", "callees", "call-chain", "topo",
                "metrics", "complexity", "coupling", "comment-coverage", "uncommented",
                "function-issues", "largest-fns", "coupled-fns", "fn-metrics",
                "git", "semgrep",
                "coverage", "who", "ownership-map",
                "brief", "map",
                "health-report",
                # L5: build-context + toolchain 子命令（resolved_edges 引擎入口）
                "build-context", "toolchain",
                # F11（2026-07-20 批次6）：build_graph_from_c_files 接入生产路径
                "graph",
                # N4（2026-07-20 批次6）：config_loader 分层配置接入
                "config",
                # 项目综合状态驾驶舱（7 个 section + 风险预警）
                "dashboard",
                # Phase 0 子任务 4：迁移回滚配置
                "rollback",
                # P0 盲评对照实验命令组（Requirement 12）
                "experiment",
                # D0 多 LLM 契约协同：经 daemon 的治理写命令面（Req 14）
                "collab",
                # P2 依赖图与环检测诊断（Req 9.1-9.10）
                "dependency",
                # P3 Identity/Attestation（Req 10.1-10.18）
                "identity"}

# 只读子命令集合：这些命令不修改数据库，在 workspace 已激活时可跳过注册/激活写操作
# 判断依据：子命令+action 组合是否涉及 INSERT/UPDATE/DELETE
_READONLY_TASK_ACTIONS = {"list", "show", "status-tree", "findings"}
_READONLY_RULE_ACTIONS = {"list", "candidate", "applicable", "extract"}
# audit verify/keys 只读（只查询 audit_chain/audit_key_rotations 表，不写数据库）
# audit rotate-key 是写（INSERT/UPDATE audit_key_rotations）
_READONLY_AUDIT_ACTIONS = {"verify", "keys"}
# bootstrap status 只读（汇总查询，不写数据库）
_READONLY_BOOTSTRAP_ACTIONS = {"status"}
# clone list/stats 只读（查询 clone_pairs 表）；clone detect/clear 写
_READONLY_CLONE_ACTIONS = {"list", "stats"}
# C8 Step #1: 新增 subcommand 的只读 action 集合
# workspace list 只读；register/set/delete 写
_READONLY_WORKSPACE_ACTIONS = {"list"}
# git log/show/stats 只读；git import 写
# git log/show/stats/check-task/destructive-log 只读；git import/check-push 写
# check-task 读 active_task（只读）；check-push 写 destructive_operations（写）
_READONLY_GIT_ACTIONS = {"log", "show",
                         "stats", "check-task", "destructive-log"}
# semgrep list/stats 只读；semgrep scan 含 --save 写，默认视为写以避免锁
_READONLY_SEMGREP_ACTIONS = {"list", "stats"}
# coverage fn/uncovered 只读；coverage import 写
_READONLY_COVERAGE_ACTIONS = {"fn", "uncovered"}
# fts status 只读（查询 FTS5 索引状态）；fts rebuild 写（重建索引）
_READONLY_FTS_ACTIONS = {"status"}
# F11（2026-07-20 批次6）：graph build-from-c 只读（仅 parse + 内存构 CSR + 报告；
# 不写数据库，可选 dump 到 .cwsnap 文件不算写 DB）
_READONLY_GRAPH_ACTIONS = {"build-from-c"}
# N4（2026-07-20 批次6）：config explain/paths 只读（只读 TOML + 打印路径）
_READONLY_CONFIG_ACTIONS = {"explain", "paths"}
# Phase 0 子任务 4：rollback config/show/is-rolled-back 只读；register/set 写
_READONLY_ROLLBACK_ACTIONS = {"config", "show", "is-rolled-back"}

# 写 flag 集合：设置这些 flag 的命令需要写数据库，必须激活 workspace
# 不在此集合内的 flag 命令均为只读（search/symbol/callers/callees/topo/file/history/diff/
# changes/comment_coverage/stats/status/query/top_callers/orphan_symbols/deepest/module_calls/
# detect_cycles/export_module_graph/call_heatmap/impact/uncommented/who/ownership_map 等）
_WRITE_FLAGS = {
    "refresh_all", "refresh", "watch",
    "register_workspace", "set_workspace", "delete_workspace",
    "restore_comment", "restore_all_comments",
    "coverage_import",
}


# ====================================================================
# C8 Step #2: --flag 命令的 deprecated 警告映射
# --------------------------------------------------------------------
# 每个 entry: (args 属性名, --flag 名, 推荐的 subcommand)
# 不在此表中的 --flag 通用选项（--lang/--workspace/--root/--force/--preview
# 等辅助/通用 flag）不输出 deprecated 警告。
# --task-list / --task-show 已有自己的 deprecated 提示实现，不在此表中。
# ====================================================================
_DEPRECATED_FLAG_MAPPING = {
    # [1] 工作区管理
    "list_workspaces": ("--list-workspaces", "workspace list"),
    "register_workspace": ("--register-workspace", "workspace register <NAME> <ROOT>"),
    "set_workspace": ("--set-workspace", "workspace set <ID_OR_NAME>"),
    "delete_workspace": ("--delete-workspace", "workspace delete <ID_OR_NAME>"),

    # [2] 数据库构建与监控
    "refresh_all": ("--refresh-all", "refresh --all"),
    "refresh": ("--refresh", "refresh <PATH>"),
    "watch": ("--watch", "refresh --watch"),
    "stats": ("--stats", "stats"),
    "status": ("--status", "status"),

    # [3] 符号查询
    "query": ("--query", "query <NAME> <FILE>"),
    "search": ("--search", "search <QUERY>"),
    "symbol": ("--symbol", "symbol <QUALIFIED_NAME>"),
    "file": ("--file", "file <PATH>"),

    # [4] 调用链分析
    "callers": ("--callers", "callers <NAME>"),
    "callees": ("--callees", "callees <NAME>"),
    "call_chain": ("--call-chain", "call-chain <QUALIFIED_NAME>"),
    "impact": ("--impact", "impact <QUALIFIED_NAME>"),
    "topo": ("--topo", "topo"),
    "top_callers": ("--top-callers", "callers --top N"),
    "orphan_symbols": ("--orphan-symbols", "callers --orphans"),
    "deepest": ("--deepest", "call-chain --deepest N"),
    "module_calls": ("--module-calls", "call-chain --module-calls N"),
    "detect_cycles": ("--detect-cycles", "call-chain --detect-cycles"),
    "export_module_graph": ("--export-module-graph", "call-chain --export-module-graph"),
    "call_heatmap": ("--call-heatmap", "call-chain --heatmap"),

    # [5] 代码度量
    "metrics": ("--metrics", "metrics"),
    "complexity": ("--complexity", "complexity [N]"),
    "coupling": ("--coupling", "coupling"),
    "largest_fns": ("--largest-fns", "largest-fns [N]"),
    "coupled_fns": ("--coupled-fns", "coupled-fns [N]"),
    "fn_metrics": ("--fn-metrics", "fn-metrics <NAME>"),
    "comment_coverage": ("--comment-coverage", "comment-coverage"),
    "uncommented": ("--uncommented", "uncommented [KIND]"),

    # [6] 编辑与版本历史
    "restore_comment": ("--restore-comment", "file restore-comment <SPEC>"),
    "restore_all_comments": ("--restore-all-comments", "file restore-all-comments"),
    "restore_file": ("--restore-file", "file restore-file <PATH>"),
    "history": ("--history", "symbol-history <NAME>"),
    "diff": ("--diff", "file diff <HASH1> <HASH2>"),
    "changes": ("--changes", "file changes [SINCE]"),

    # [7] Git 集成
    "git_import": ("--git-import", "git import [N]"),
    "git_log": ("--git-log", "git log [N]"),
    "git_show": ("--git-show", "git show <COMMIT>"),
    "git_stats": ("--git-stats", "git stats"),

    # [8] Semgrep 静态扫描
    "semgrep": ("--semgrep", "semgrep scan [PATH]"),
    "semgrep_list": ("--semgrep-list", "semgrep list [FILTER]"),
    "semgrep_stats": ("--semgrep-stats", "semgrep stats"),

    # [9] 缺陷检测
    "function_issues": ("--function-issues", "function-issues [FN]"),
    "issue_summary": ("--issue-summary", "function-issues --summary"),

    # [10] 覆盖率
    "coverage_import": ("--coverage-import", "coverage import <FILE>"),
    "coverage_fn": ("--coverage-fn", "coverage fn <NAME>"),
    "coverage_uncovered": ("--coverage-uncovered", "coverage uncovered"),
    "test_coverage": ("--test-coverage", "coverage --test"),

    # [11] 所有权
    "who": ("--who", "who <FILE>"),
    "ownership_map": ("--ownership-map", "ownership-map"),

    # [12] 向量语义搜索
    "semantic_search": ("--semantic-search", "search --semantic <QUERY>"),
    "embed": ("--embed", "search --embed"),
    "embed_force": ("--embed-force", "search --embed --force"),
    "similar": ("--similar", "search --similar <NAME>"),

    # [13] 项目简报与仓库地图
    "brief": ("--brief", "brief"),
    "map": ("--map", "map"),
}


def _emit_deprecated_flag_warning(args):
    """C8 Step #2: 扫描 args，对每个被设置为真值的 deprecated --flag 输出 stderr 警告。

    设计：
    - 不阻断执行，仅 warning；
    - 输出到 stderr（不污染 stdout 管道）；
    - 不包含 --task-list / --task-show（已有自己的 deprecated 提示实现）；
    - 通用 flag（--lang/--workspace/--root/--force/--preview 等辅助 flag）不在映射表中，不输出警告。
    """
    for attr, (flag_name, subcommand) in _DEPRECATED_FLAG_MAPPING.items():
        val = getattr(args, attr, None)
        if val:  # truthy (含非空字符串、True、列表等)
            # 嵌入多语言提示，写入 stderr
            msg = t("cli.messages.deprecated_flag_warning",
                    flag=flag_name, subcommand=subcommand)
            cprint(msg, "yellow", file=sys.stderr)
            # 通用引导提示（每个 deprecated flag 触发后输出一次）
            hint = t("cli.messages.deprecated_flag_hint")
            cprint(hint, "yellow", file=sys.stderr)


# ====================================================================
# C8 Step #3: 主 --help 输出（12 组分组结构）
# --------------------------------------------------------------------
# 替代旧的 4-pillar 分组 + argparse 默认 description。
# 输出：标题 + 12 组分组 + 底部 deprecated flag 清单 + 最底部全局选项
# ====================================================================

# 12 组分组数据：每组 (group_title_key, [(cmd, desc_key), ...])
_MAIN_HELP_GROUPS = [
    ("cli.messages.help_group_workspace", [
        ("workspace list", "cli.messages.help_workspace_list"),
        ("workspace register <NAME> <ROOT>",
         "cli.messages.help_workspace_register"),
        ("workspace set <ID_OR_NAME>", "cli.messages.help_workspace_set"),
        ("workspace delete <ID_OR_NAME>", "cli.messages.help_workspace_delete"),
        ("workspace scan [<DIR>]", "cli.messages.help_workspace_scan"),
        ("workspace generate-ignore [<DIR>] [--apply]",
         "cli.messages.help_workspace_generate_ignore"),
        ("refresh all | <paths> | --watch", "cli.messages.help_refresh"),
        ("stats", "cli.messages.help_stats"),
        ("status", "cli.messages.help_status"),
        ("doctor", "cli.messages.help_doctor"),
    ]),
    ("cli.messages.help_group_query", [
        ("search <QUERY>", "cli.messages.help_search"),
        ("symbol <QUALIFIED_NAME>", "cli.messages.help_symbol"),
        ("file <PATH>", "cli.messages.help_file"),
        ("query <NAME> <FILE>", "cli.messages.help_query"),
        ("brief", "cli.messages.help_brief"),
        ("map", "cli.messages.help_map"),
    ]),
    ("cli.messages.help_group_call_chain", [
        ("callers <NAME>", "cli.messages.help_callers"),
        ("callees <NAME>", "cli.messages.help_callees"),
        ("call-chain <QUALIFIED_NAME>", "cli.messages.help_call_chain"),
        ("topo", "cli.messages.help_topo"),
        ("impact <SYMBOL_HASH>", "cli.messages.help_chain_impact"),
        ("call-chain --detect-cycles", "cli.messages.help_chain_cycles"),
        ("call-chain --orphans", "cli.messages.help_chain_orphans"),
        ("call-chain --deepest N", "cli.messages.help_chain_deepest"),
        ("callers --top N", "cli.messages.help_chain_top_callers"),
        ("call-chain --module-calls", "cli.messages.help_chain_module_calls"),
        ("call-chain --heatmap", "cli.messages.help_chain_heatmap"),
        ("call-chain --export-module-graph",
         "cli.messages.help_chain_module_graph"),
    ]),
    ("cli.messages.help_group_metrics", [
        ("metrics", "cli.messages.help_metrics"),
        ("complexity [N]", "cli.messages.help_complexity"),
        ("coupling", "cli.messages.help_coupling"),
        ("largest-fns [N]", "cli.messages.help_largest_fns"),
        ("coupled-fns [N]", "cli.messages.help_coupled_fns"),
        ("fn-metrics <NAME>", "cli.messages.help_fn_metrics"),
        ("comment-coverage", "cli.messages.help_comment_coverage"),
        ("uncommented [KIND]", "cli.messages.help_uncommented"),
        ("function-issues [FN]", "cli.messages.help_function_issues"),
    ]),
    ("cli.messages.help_group_task", [
        ("task create --title ... --steps ...", "cli.messages.help_task_create"),
        ("task next <TASK_ID>", "cli.messages.help_task_next"),
        ("task report <TASK_ID> <STEP_ID>", "cli.messages.help_task_report"),
        ("task rollback <TASK_ID> <STEP_ID>", "cli.messages.help_task_rollback"),
        ("task apply <TASK_ID>", "cli.messages.help_task_apply"),
        ("task close <TASK_ID>", "cli.messages.help_task_close"),
        ("task reopen <TASK_ID>", "cli.messages.help_task_reopen"),
        ("task list [--blocked]", "cli.messages.help_task_list"),
        ("task show <TASK_ID>", "cli.messages.help_task_show"),
        ("task findings <TASK_ID>", "cli.messages.help_task_findings"),
        ("task capture-diff [TASK_ID] [--auto]",
         "cli.messages.help_task_capture_diff"),
        ("task resolve-finding <FINDING_ID>",
         "cli.messages.help_task_resolve_finding"),
        ("task completion-review <TASK_ID>",
         "cli.messages.help_task_completion_review"),
        ("task split <TASK_ID>", "cli.messages.help_task_split"),
        ("task status-tree", "cli.messages.help_task_status_tree"),
    ]),
    ("cli.messages.help_group_rule", [
        ("rule candidate create/list/accept/reject",
         "cli.messages.help_rule_candidate"),
        ("rule list", "cli.messages.help_rule_list"),
        ("rule applicable --context ...", "cli.messages.help_rule_applicable"),
        ("rule sync [--target AGENTS.md]", "cli.messages.help_rule_sync"),
        ("rule insert-block", "cli.messages.help_rule_insert_block"),
        ("rule extract", "cli.messages.help_rule_extract"),
        ("rule seed-bootstrap", "cli.messages.help_rule_seed_bootstrap"),
        ("rule cleanup-sync-log", "cli.messages.help_rule_cleanup_sync_log"),
    ]),
    ("cli.messages.help_group_audit", [
        ("audit verify [--table T] [--limit N]",
         "cli.messages.help_audit_verify"),
        ("audit rotate-key --key-id <ID>", "cli.messages.help_audit_rotate_key"),
        ("audit keys", "cli.messages.help_audit_keys"),
        ("bootstrap status", "cli.messages.help_bootstrap_status"),
        ("check-gate <TASK_ID> [--resolve]", "cli.messages.help_check_gate"),
        ("test-impact <QUALIFIED_NAME>", "cli.messages.help_test_impact"),
    ]),
    ("cli.messages.help_group_git", [
        ("git import [N]", "cli.messages.help_git_import"),
        ("git log [N]", "cli.messages.help_git_log"),
        ("git show <COMMIT>", "cli.messages.help_git_show"),
        ("git stats", "cli.messages.help_git_stats"),
        ("symbol-history <SYMBOL_HASH>", "cli.messages.help_symbol_history"),
    ]),
    ("cli.messages.help_group_semgrep", [
        ("semgrep scan [PATH]", "cli.messages.help_semgrep_scan"),
        ("semgrep list [FILTER]", "cli.messages.help_semgrep_list"),
        ("semgrep stats", "cli.messages.help_semgrep_stats"),
        ("defect search [--category C] [--severity S]",
         "cli.messages.help_defect_search"),
        ("defect suggest <SYMBOL_HASH>", "cli.messages.help_defect_suggest"),
        ("defect learn <COMMIT_HASH>", "cli.messages.help_defect_learn"),
        ("defect stats", "cli.messages.help_defect_stats"),
        ("defect build", "cli.messages.help_defect_build"),
        ("vuln-blast [--finding-id N]", "cli.messages.help_vuln_blast"),
        ("impact <SYMBOL_HASH>", "cli.messages.help_impact"),
        ("review <SYMBOL_HASH>", "cli.messages.help_review"),
    ]),
    ("cli.messages.help_group_coverage", [
        ("coverage import <FILE>", "cli.messages.help_coverage_import"),
        ("coverage fn <NAME>", "cli.messages.help_coverage_fn"),
        ("coverage uncovered", "cli.messages.help_coverage_uncovered"),
        ("who <FILE>", "cli.messages.help_who"),
        ("ownership-map", "cli.messages.help_ownership_map"),
    ]),
    ("cli.messages.help_group_gc", [
        ("gc archive [--force] [--dry-run]", "cli.messages.help_gc_archive"),
        ("gc restore [--path P ...] [--force]",
         "cli.messages.help_gc_restore"),
        ("gc status", "cli.messages.help_gc_status"),
        ("gc purge [--older-than N]", "cli.messages.help_gc_purge"),
        ("gc policy show|set", "cli.messages.help_gc_policy"),
        ("gc retention [--apply]", "cli.messages.help_gc_retention"),
        ("gc archive-list", "cli.messages.help_gc_archive_list"),
        ("gc archive-inspect <PATH>", "cli.messages.help_gc_archive_inspect"),
        ("gc archive-import <PATH>", "cli.messages.help_gc_archive_import"),
        ("gc audit-list", "cli.messages.help_gc_audit_list"),
        ("gc audit-show <ID>", "cli.messages.help_gc_audit_show"),
        ("gc db-cleanup [--apply]", "cli.messages.help_gc_db_cleanup"),
    ]),
    ("cli.messages.help_group_diagnostics", [
        ("doctor [--add-defender-exclusion]", "cli.messages.help_doctor"),
        ("install-agent <codex|claude|cursor|all>",
         "cli.messages.help_install_agent"),
        ("install-hook", "cli.messages.help_install_hook"),
        ("guardrail scan [--file P] [--category C]",
         "cli.messages.help_guardrail_scan"),
        ("guardrail rules [--category C]",
         "cli.messages.help_guardrail_rules"),
        ("clone detect [--file-filter P]", "cli.messages.help_clone_detect"),
        ("clone list [--type 1|2|3]", "cli.messages.help_clone_list"),
        ("clone stats", "cli.messages.help_clone_stats"),
        ("clone clear", "cli.messages.help_clone_clear"),
        ("evolution <QUALIFIED_NAME>", "cli.messages.help_evolution"),
        ("hotspot [--module P]", "cli.messages.help_hotspot"),
        ("churn [--module P] [--window 90d]", "cli.messages.help_churn"),
        ("setup [--force] [--dry-run]", "cli.messages.help_setup"),
    ]),
]


def _print_main_help():
    """打印主 --help 输出（12 组分组结构，C8 Step #3）

    替代旧的 4-pillar 分组。输出顺序：
    1. 标题 + intro
    2. 12 组分组（每组：组标题 + 命令-说明对）
    3. 底部 deprecated flag 清单（前 10 个，指向替代 subcommand）
    4. 最底部全局选项（--lang/--workspace/--root/--help）
    """
    # 标题
    cprint(t("cli.messages.main_help_title"), "cyan", bold=True)
    print(t("cli.messages.main_help_intro"))
    print()

    # 获取当前可用 Agent 数量，用于 help_install_agent 等动态占位符
    try:
        _agent_count = len(get_merged_specs(""))
    except Exception:
        _agent_count = 0

    # 12 组分组
    for group_title_key, items in _MAIN_HELP_GROUPS:
        cprint(t(group_title_key), "yellow", bold=True)
        for cmd, desc_key in items:
            desc = t(desc_key, count=_agent_count)
            print(f"  {cmd:45s}  {desc}")
        print()

    # 底部 deprecated flag 清单（指向替代 subcommand）
    cprint(t("cli.messages.help_deprecated_title"), "yellow", bold=True)
    print(t("cli.messages.help_deprecated_intro"))
    # 显示前 10 个最常用的 deprecated flag → subcommand 映射
    deprecated_items = list(_DEPRECATED_FLAG_MAPPING.items())[:10]
    for _attr, (flag_name, subcommand) in deprecated_items:
        print(f"  {flag_name:30s}  -> cw {subcommand}")
    remaining = len(_DEPRECATED_FLAG_MAPPING) - 10
    if remaining > 0:
        print(t("cli.messages.help_deprecated_more", count=remaining))
    print()

    # 最底部全局选项
    cprint(t("cli.messages.help_global_options_title"), "cyan", bold=True)
    print(f"  --lang LANG                 {t('cli.messages.help_lang')}")
    print(
        f"  --workspace ROOT            {t('cli.messages.help_workspace_root')}")
    print(f"  --root ROOT                 {t('cli.messages.help_root')}")
    print(f"  -h, --help                  {t('cli.messages.help_help')}")
    print()
    print(t("cli.messages.help_footer"))


# ====================================================================
# Lazy Auto-Setup：首次运行自动探测 AI 工具并注册 MCP Server
# ====================================================================

def _check_auto_setup():
    """检查并执行首次自动配置（幂等）

    在 main() 参数解析后、命令分发前调用。
    跳过条件：
    1. 环境变量 CALLWARDEN_SKIP_AUTO_SETUP=1
    2. CLI flag --no-auto-setup（pre-parse 检查 sys.argv）
    3. 标记文件已存在（幂等）
    4. setup/install 等命令本身自己处理，不重复触发
    """
    # Opt-out 检查：环境变量禁用
    if os.environ.get("CALLWARDEN_SKIP_AUTO_SETUP") == "1":
        return
    # CLI flag opt-out（pre-parse 检查 sys.argv）
    if "--no-auto-setup" in sys.argv:
        return

    # 检查标记文件是否已存在（快速短路，避免导入 installer 开销）
    if os.path.isfile(AUTO_SETUP_MARKER):
        return

    # 跳过某些不需要自动配置的命令
    skip_commands = {"server", "setup", "install",
                     "daemon", "install-agent", "install-hook"}
    if len(sys.argv) > 1 and sys.argv[1] in skip_commands:
        return

    # 执行自动配置
    try:
        from ..install import CallWardenInstaller
        installer = CallWardenInstaller()
        configured = installer.auto_setup()
        if configured:
            names = ", ".join(configured)
            print(t("cli.messages.auto_setup_done",
                    default=f"已自动为 {names} 配置 CW MCP Server",
                    agents=names))
    except Exception as e:
        # 自动配置失败时只打印警告，不影响主命令执行
        print(t("cli.messages.auto_setup_error",
                default="[WARN] Auto-setup skipped: {error}",
                error=str(e)))


def _handle_setup():
    """处理 cw setup 子命令

    解析 setup 专属参数（--force / --dry-run），探测已安装 AI 工具并配置 MCP 集成。
    不需要数据库初始化，在 main() 中单独处理。
    """
    import argparse as _argparse
    from ..install import CallWardenInstaller

    parser = _argparse.ArgumentParser(
        prog="cw setup",
        description=t("cli.messages.setup_command_help",
                      default="自动配置已安装 AI 工具的 MCP 集成"),
    )
    parser.add_argument("--force", action="store_true",
                        help=t("cli.messages.setup_force_help",
                               default="强制重新配置（忽略已完成标记）"))
    parser.add_argument("--dry-run", action="store_true",
                        help=t("cli.messages.setup_dry_run_help",
                               default="仅探测不写入"))

    args = parser.parse_args(sys.argv[2:])

    installer = CallWardenInstaller()

    # 探测已安装的 AI 工具
    detected = installer.detect_installed_agents()
    if not detected:
        print(t("cli.messages.setup_no_agents",
                default="未检测到已安装的 AI 编码工具"))
        return

    print(t("cli.messages.setup_detected",
            default=f"检测到 {len(detected)} 个 AI 工具：",
            count=len(detected)))
    for d in detected:
        icon = "[CLI]" if d.detected_by == "cli" else (
            "[CFG]" if d.detected_by == "config_dir" else "[WIN]")
        print(f"  {icon} {d.display} ({d.agent_key})")
        print(f"       -> {d.detect_detail}")
    print()

    if args.dry_run:
        print(t("cli.messages.setup_dry_run_msg",
                default="（dry-run 模式，未写入配置）"))
        return

    # --force 时删除标记文件以允许重新配置
    if args.force and os.path.isfile(AUTO_SETUP_MARKER):
        try:
            os.remove(AUTO_SETUP_MARKER)
        except OSError:
            pass

    configured = installer.auto_setup(force=args.force)
    if configured:
        print(t("cli.messages.setup_done",
                default=f"已为 {len(configured)} 个工具配置 CW MCP Server",
                count=len(configured)))
    elif not args.force and os.path.isfile(AUTO_SETUP_MARKER):
        # 标记文件已存在，说明已完成过配置
        print(t("cli.messages.setup_already_done",
                default="已完成标记存在，使用 --force 重新配置"))
    else:
        # 探测到 Agent 但未能写入配置（权限等原因）
        print(t("cli.messages.setup_no_write",
                default="探测到 AI 工具但未能写入配置，请检查权限"))


# ====================================================================
# C8 Step #4: 子命令 --help 统一模板
# --------------------------------------------------------------------
# 模板包含 5 个章节：用法 / 描述 / 参数（必填|可选）/ 示例 / 退出码
# 通过 argparse 的 epilog + RawDescriptionHelpFormatter 实现多行帮助
# ====================================================================

def _format_subcommand_help(usage: str, description: str, parameters: list,
                            examples: list, exit_codes: list) -> str:
    """按统一模板格式化子命令帮助文本（C8 Step #4）

    模板章节（5 个）：
    - 用法 / Usage
    - 描述 / Description
    - 参数 / Parameters（含必填 [必填] / 可选 [可选] 标记）
    - 示例 / Examples（至少 2 个）
    - 退出码 / Exit Codes

    Args:
        usage: 用法字符串，如 "cw task <subcommand> [options]"
        description: 命令描述
        parameters: 参数列表，每项为 (name, required, desc) 三元组；
                    required=True 表示必填，False 表示可选
        examples: 示例字符串列表（至少 2 个）
        exit_codes: 退出码列表，每项为 (code, meaning) 二元组

    Returns:
        格式化后的多行帮助文本
    """
    lines = []
    # 顶部装饰分隔
    lines.append("=" * 60)
    # 用法
    lines.append(t("cli.messages.help_template_usage"))
    lines.append(f"  {usage}")
    lines.append("")
    # 描述
    lines.append(t("cli.messages.help_template_description"))
    lines.append(f"  {description}")
    lines.append("")
    # 参数
    lines.append(t("cli.messages.help_template_parameters"))
    for name, required, desc in parameters:
        mark = t("cli.messages.help_template_required") if required else t(
            "cli.messages.help_template_optional")
        lines.append(f"  {mark} {name:25s}  {desc}")
    lines.append("")
    # 示例
    lines.append(t("cli.messages.help_template_examples"))
    for i, ex in enumerate(examples, 1):
        lines.append(f"  {i}. {ex}")
    lines.append("")
    # 退出码
    lines.append(t("cli.messages.help_template_exit_codes"))
    for code, meaning in exit_codes:
        lines.append(f"  {code}  {meaning}")
    # 底部装饰分隔
    lines.append("=" * 60)
    return "\n".join(lines)


# ====================================================================
# C8 Step #4: 18+ 子命令的统一帮助模板规格
# --------------------------------------------------------------------
# 每条规格：{cmd: {"usage", "description", "parameters", "examples",
#                  "exit_codes", "desc_key"}}
# 通过 _get_subcommand_epilog(cmd) 取出格式化后的 epilog 文本。
# ====================================================================
_SUBCOMMAND_HELP_SPECS = {
    "task": {
        "usage": "cw task <subcommand> [options]",
        "description": "Task lifecycle: create / next / report / rollback / apply / close / list / show / findings / capture-diff / resolve-finding / completion-review / split / status-tree",
        "parameters": [
            ("create --title T --steps J", True, "Create task and steps"),
            ("next <task_id>", True, "Claim current pending step"),
            ("report <task_id> <step_id> [--fail]",
             True, "Report step result"),
            ("rollback <task_id> <step_id>", True, "Roll back changes"),
            ("apply <task_id> [--reviewer R]", True,
             "Approve task (review -> applied)"),
            ("close <task_id> [--reviewer R]", True,
             "Close task (applied -> closed)"),
            ("capture-diff [task_id] [--auto] [--dry-run]",
             False, "Capture external agent file changes"),
            ("list [--blocked] [--status S] [--limit N]", False, "List tasks"),
            ("show <task_id> [--flat]", False, "Show task details"),
            ("findings <task_id> [--status S] [--severity S]",
             False, "List task quality findings"),
            ("resolve-finding <finding_id> [--resolution R]",
             False, "Resolve a quality gate finding"),
        ],
        "examples": [
            "cw task create --title 'Add login feature' --steps '[{\"action\":\"annotate\",\"target_file\":\"a.py\"}]'",
            "cw task next T-1783350489327",
            "cw task report T-1783350489327 S-1783350489328 --result 'done'",
            "cw task list --blocked",
            "cw task capture-diff --auto",
        ],
        "exit_codes": [
            ("0", "Success"),
            ("1", "Failure (invalid args, db error, task not found)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "rule": {
        "usage": "cw rule <subcommand> [options]",
        "description": "Agent Rule Memory: candidate create/list/accept/reject, list, applicable, sync, insert-block, extract, seed-bootstrap, cleanup-sync-log",
        "parameters": [
            ("candidate create --title T --text T",
             True, "Create pending candidate rule"),
            ("candidate list [--status S] [--limit N]",
             False, "List candidate rules"),
            ("candidate accept <candidate_id> [--reviewer R]",
             True, "Accept candidate -> active"),
            ("candidate reject <candidate_id> [--reason R]",
             True, "Reject candidate"),
            ("list [--status S] [--limit N]", False, "List active rules"),
            ("applicable --context JSON [--limit N]",
             False, "Get applicable rules by context"),
            ("sync [--target AGENTS.md]", False,
             "Sync active rules to AGENTS.md marker block"),
            ("extract", False, "Aggregate findings into candidates"),
            ("seed-bootstrap", False, "Seed rule library from built-in templates"),
        ],
        "examples": [
            "cw rule candidate create --title 'Avoid raw SQL' --text 'Never use string concatenation for SQL'",
            "cw rule list --status active",
            "cw rule applicable --context '{\"languages\":[\"python\"],\"actions\":[\"edit\"]}'",
            "cw rule sync --target AGENTS.md",
            "cw rule seed-bootstrap",
        ],
        "exit_codes": [
            ("0", "Success"),
            ("1", "Failure (invalid args, db error)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "gc": {
        "usage": "cw gc <subcommand> [options]",
        "description": "Code graph GC: archive / restore / status / purge / policy / retention / archive-list / archive-inspect / archive-import / audit-list / audit-show",
        "parameters": [
            ("archive [--force] [--dry-run]", True,
             "Archive files matched by ignore rules"),
            ("restore [--path P ...] [--force]",
             False, "Restore archived files"),
            ("status", False, "View GC status"),
            ("purge [--older-than N]", False,
             "Permanently purge files archived >N days"),
            ("policy show|set", False, "Show or update GC retention policy"),
            ("retention [--apply] [--save-policy]", False,
             "Cold data pruning with compressed backup"),
            ("archive-list [--limit N]", False, "List GC backup files"),
            ("archive-inspect <path>", False,
             "Inspect backup file contents (read-only)"),
            ("archive-import <path> [--apply]", False,
             "Import historical data from backup"),
            ("audit-list [--limit N] [--operation O]",
             False, "View GC audit history"),
            ("audit-show <id>", False, "View details of a single GC audit record"),
        ],
        "examples": [
            "cw gc archive --dry-run",
            "cw gc status",
            "cw gc purge --older-than 30",
            "cw gc policy show",
            "cw gc archive-list --limit 10",
        ],
        "exit_codes": [
            ("0", "Success"),
            ("1", "Failure (invalid args, db error)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "audit": {
        "usage": "cw audit <subcommand> [options]",
        "description": "Audit chain verification and signing key rotation: verify / rotate-key / keys",
        "parameters": [
            ("verify [--table T] [--limit N]", False,
             "Verify audit chain continuity and signatures"),
            ("rotate-key --key-id ID [--secret S]",
             True, "Rotate audit signing key"),
            ("keys", False, "List all signing key rotation records"),
        ],
        "examples": [
            "cw audit verify",
            "cw audit verify --table task_steps --limit 500",
            "cw audit rotate-key --key-id key-2026-07",
            "cw audit keys",
        ],
        "exit_codes": [
            ("0", "Success (audit chain intact or operation completed)"),
            ("1", "Failure (invalid args, db error, broken audit chain)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "bootstrap": {
        "usage": "cw bootstrap <subcommand>",
        "description": "Bootstrap health summary: status",
        "parameters": [
            ("status", True, "Show bootstrap health summary"),
        ],
        "examples": [
            "cw bootstrap status",
            "cw bootstrap status 2>&1 | tee bootstrap-report.txt",
        ],
        "exit_codes": [
            ("0", "Success"),
            ("1", "Failure (db error)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "defect": {
        "usage": "cw defect <subcommand> [options]",
        "description": "Defect knowledge base: search / suggest / learn / stats / build",
        "parameters": [
            ("search [--category C] [--severity S] [--limit N]",
             False, "Search defect patterns"),
            ("suggest <symbol_hash> [--finding ID]",
             True, "Recommend fix suggestions"),
            ("learn <commit_hash>", True, "Learn defect patterns from a fix commit"),
            ("stats", False, "Defect knowledge base statistics"),
            ("build", False, "Build defect knowledge base"),
        ],
        "examples": [
            "cw defect search --severity error",
            "cw defect suggest abc123def456",
            "cw defect learn a1b2c3d4",
            "cw defect stats",
        ],
        "exit_codes": [
            ("0", "Success"),
            ("1", "Failure (invalid args, db error, pattern not found)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "guardrail": {
        "usage": "cw guardrail <subcommand> [options]",
        "description": "Production safety guardrails: scan / rules",
        "parameters": [
            ("scan [--file P] [--category C]",
             True, "Scan guardrail violations"),
            ("rules [--category C]", False, "List guardrail rules"),
        ],
        "examples": [
            "cw guardrail scan",
            "cw guardrail scan --file src/db/ --category db_safety",
            "cw guardrail rules --category api_compat",
        ],
        "exit_codes": [
            ("0", "Success"),
            ("1", "Failure (invalid args, db error)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "impact": {
        "usage": "cw impact <symbol_hash> [--depth N]",
        "description": "Change impact radius analysis (BFS over reverse call graph)",
        "parameters": [
            ("symbol_hash", True, "Source symbol content hash"),
            ("--depth N", False, "Maximum BFS traversal depth (default: 3)"),
        ],
        "examples": [
            "cw impact abc123def456",
            "cw impact abc123def456 --depth 5",
        ],
        "exit_codes": [
            ("0", "Success"),
            ("1", "Failure (symbol not found, db error)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "review": {
        "usage": "cw review <symbol_hash>",
        "description": "Review readiness report (impact scope + must-test + review points)",
        "parameters": [
            ("symbol_hash", True, "Source symbol content hash"),
        ],
        "examples": [
            "cw review abc123def456",
            "cw review deadbeefcafe",
        ],
        "exit_codes": [
            ("0", "Success"),
            ("1", "Failure (symbol not found, db error)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "evolution": {
        "usage": "cw evolution <qualified_name> [--window W]",
        "description": "Function change frequency over time (commit history analysis)",
        "parameters": [
            ("qualified_name", True, "Function qualified name"),
            ("--window W", False, "Time window (e.g. 30d/90d/1y, empty = all history)"),
        ],
        "examples": [
            "cw evolution module::function_name",
            "cw evolution module::function_name --window 90d",
        ],
        "exit_codes": [
            ("0", "Success"),
            ("1", "Failure (function not found, db error)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "hotspot": {
        "usage": "cw hotspot [--module P] [--limit N]",
        "description": "Hotspot function ranking (change frequency + defect correlation + complexity)",
        "parameters": [
            ("--module P", False, "Filter by module path prefix"),
            ("--limit N", False, "Number of items to show (default: 20)"),
        ],
        "examples": [
            "cw hotspot",
            "cw hotspot --module src/core/ --limit 50",
        ],
        "exit_codes": [
            ("0", "Success"),
            ("1", "Failure (db error)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "churn": {
        "usage": "cw churn [--module P] [--window W]",
        "description": "Code churn analysis (changed files + churned lines + trend)",
        "parameters": [
            ("--module P", False, "Filter by module path prefix"),
            ("--window W", False, "Time window (default: 90d)"),
        ],
        "examples": [
            "cw churn",
            "cw churn --module src/api/ --window 30d",
        ],
        "exit_codes": [
            ("0", "Success"),
            ("1", "Failure (db error)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "vuln-blast": {
        "usage": "cw vuln-blast [--finding-id N] [--severity S] [--depth N]",
        "description": "Vulnerability blast radius analysis (reverse call graph from findings)",
        "parameters": [
            ("--finding-id N", False, "Specify Semgrep finding ID (default: scan all)"),
            ("--severity S", False, "Severity filter (ERROR/WARN/INFO)"),
            ("--depth N", False, "Reverse call graph traversal depth (default: 3)"),
        ],
        "examples": [
            "cw vuln-blast",
            "cw vuln-blast --finding-id 42",
            "cw vuln-blast --severity ERROR --depth 5",
        ],
        "exit_codes": [
            ("0", "Success"),
            ("1", "Failure (db error, no findings)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "symbol-history": {
        "usage": "cw symbol-history <symbol_hash> [--limit N]",
        "description": "Symbol Git change history (commits that touched this symbol)",
        "parameters": [
            ("symbol_hash", True, "Symbol content hash"),
            ("--limit N", False, "Result limit (default: 20)"),
        ],
        "examples": [
            "cw symbol-history abc123def456",
            "cw symbol-history abc123def456 --limit 50",
        ],
        "exit_codes": [
            ("0", "Success"),
            ("1", "Failure (symbol not found, db error)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "check-gate": {
        "usage": "cw check-gate <task_id> [--resolve] [--step-id S]",
        "description": "Check gate (F6): run quality gate checks on task's changed files",
        "parameters": [
            ("task_id", True, "Task ID"),
            ("--resolve", False,
             "Mark gate findings for this task as resolved (after agent fix)"),
            ("--step-id S", False, "Related step ID (optional)"),
        ],
        "examples": [
            "cw check-gate T-1783350489327",
            "cw check-gate T-1783350489327 --resolve",
        ],
        "exit_codes": [
            ("0", "Success (gate passed or findings resolved)"),
            ("1", "Failure (task not found, db error)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "test-impact": {
        "usage": "cw test-impact <qualified_name>",
        "description": "Test impact selection (which tests to run after changing a function)",
        "parameters": [
            ("qualified_name", True, "Qualified name of the modified function"),
        ],
        "examples": [
            "cw test-impact module::function_name",
            "cw test-impact another_module::another_fn",
        ],
        "exit_codes": [
            ("0", "Success"),
            ("1", "Failure (function not found, db error)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "doctor": {
        "usage": "cw doctor [--add-defender-exclusion]",
        "description": "Environment diagnostics and maintenance (db status, PRAGMA, WAL, Defender)",
        "parameters": [
            ("--add-defender-exclusion", False,
             "Add .callwarden to Windows Defender exclusions (requires admin)"),
        ],
        "examples": [
            "cw doctor",
            "cw doctor --add-defender-exclusion",
        ],
        "exit_codes": [
            ("0", "Success"),
            ("1", "Failure (db error, admin required)"),
            ("2", "Database locked (retry later)"),
        ],
    },
    "install-agent": {
        "usage": "cw install-agent <agent|all> [--output-dir D] [--force] [--global]",
        "description": "Generate Call Warden integration files for {count} AI agents (project-level bundle or --global MCP config)",
        "parameters": [
            ("agent", True, "Target Agent: claude-code/claude-desktop/cursor/cline/windsurf/trae/gemini-cli/codex/opencode/kiro/antigravity/qoder/jetbrains-junie/zed/pearai/kimi-code/codebuddy-cli/deep-code/comate/all"),
            ("--output-dir D", False,
             "Output directory (default: .callwarden/agent-integrations)"),
            ("--force", False, "Overwrite existing integration files"),
            ("--global", False,
             "Write to user global MCP config instead of project-level bundle"),
        ],
        "examples": [
            "cw install-agent claude-code",
            "cw install-agent all --force",
            "cw install-agent cline --global",
            "cw install-agent codex --output-dir ./integrations",
        ],
        "exit_codes": [
            ("0", "Success"),
            ("1", "Failure (invalid agent, write error)"),
        ],
    },
}


def _get_subcommand_epilog(cmd: str, **kwargs) -> str:
    """根据子命令名取出统一模板格式化的 epilog 文本（C8 Step #4）

    Args:
        cmd: 子命令名（如 "task"/"rule"/"gc" 等）
        **kwargs: 占位符参数（如 count=19），用于 description 中的动态替换

    Returns:
        格式化后的 epilog 字符串；若 cmd 不在规格表中，返回空字符串
    """
    spec = _SUBCOMMAND_HELP_SPECS.get(cmd)
    if not spec:
        return ""
    # 支持 description 中的 {count} 等动态占位符
    desc = spec["description"]
    if kwargs:
        try:
            desc = desc.format(**kwargs)
        except (KeyError, ValueError, IndexError):
            pass
    return _format_subcommand_help(
        usage=spec["usage"],
        description=desc,
        parameters=spec["parameters"],
        examples=spec["examples"],
        exit_codes=spec["exit_codes"],
    )


def _run_subcommand_mode():
    """子命令模式入口：初始化 db 并调度代码守护者架构子命令

    优化：当子命令参数中含 --help/-h 时，跳过 db 初始化直接 print 帮助。
    这避免了在 MCP Server 占用 db 锁时 cw task --help 卡死的问题。
    """
    # 使用系统检测到的默认语言（CALLWARDEN_LANG / LANG / LC_ALL / 系统语言）
    # 用户可通过环境变量 CALLWARDEN_LANG=en_US 切换为英文
    set_language(DEFAULT_LANG)

    # Lazy Auto-Setup：首次运行自动探测 AI 工具并注册 MCP Server（幂等，失败不影响主命令）
    _check_auto_setup()

    # 检测子命令是否请求帮助（避免初始化 db 触发锁等待）
    sub_argv = sys.argv[2:] if len(sys.argv) > 2 else []
    wants_help = any(a in ("-h", "--help") for a in sub_argv)
    if wants_help:
        _dispatch_subcommand_help(sys.argv[1], sub_argv)
        return

    # 自动检测工作区根目录
    # 优先级：CALLWARDEN_WORKSPACE 环境变量 > cwd 自动检测
    ws_env = os.environ.get("CALLWARDEN_WORKSPACE")
    if ws_env:
        workspace_root = ws_env
    else:
        cwd = os.getcwd()
        detected = detect_project_root(cwd)
        workspace_root = detected if detected else None

    # 初始化数据库
    db = CodeGraphDB(
        workspace_root=workspace_root) if workspace_root else CodeGraphDB()

    try:
        # 自动注册工作区（与 --flag 模式行为一致）
        # 优化：只读子命令跳过 register/set_active_workspace 写操作，避免被 MCP Server 写锁卡住
        # （set_active_workspace 内部还会做 is_active 短路判断，已 active 时直接返回不写）
        skip_workspace_write = _is_readonly_command(sys.argv[1], sub_argv)
        if workspace_root and not skip_workspace_write:
            try:
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
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower():
                    cprint(get_error("db_locked"), "red")
                    sys.exit(2)
                raise

        # 调度子命令
        _dispatch_subcommand(sub_argv, db)
    finally:
        db.close()


def _is_readonly_command(cmd: str, sub_argv: list) -> bool:
    """判断子命令是否为只读命令（不修改数据库）

    只读命令可以跳过 workspace 注册/激活写操作，在 MCP Server 持有写锁时也能立即返回。
    设计原则：所有读操作都不应该被锁住，只有写操作才需要锁。

    Args:
        cmd: 子命令关键字（如 "task"）
        sub_argv: 子命令参数（不含子命令关键字本身）

    Returns:
        True 表示只读命令，可跳过 workspace 写操作
    """
    action = sub_argv[0] if sub_argv else ""
    if cmd == "task":
        # task list/show/findings 是只读，create/next/report/apply/close 等是写
        return action in _READONLY_TASK_ACTIONS
    if cmd == "rule":
        # rule list/candidate/applicable/extract 是只读，sync/insert-block 是写
        return action in _READONLY_RULE_ACTIONS
    if cmd in {"doctor", "check-gate", "test-impact", "hotspot", "churn", "evolution",
               "impact", "review", "vuln-blast", "symbol-history"}:
        # 这些子命令默认只读（分析/查询类，不写数据库）
        return True
    if cmd == "guardrail":
        # guardrail scan 是只读（扫描展示），不带写
        return True
    if cmd == "defect":
        # defect stats/list 是只读，import/add 是写
        return action in {"stats", "list", "show"}
    if cmd == "gc":
        # gc list/inspect/db-cleanup 是只读，archive/import 是写
        # db-cleanup 默认 dry-run（只读报告），--apply 时才删除（但也不操作当前 workspace）
        return action in {"list", "inspect", "db-cleanup"}
    if cmd == "audit":
        # audit verify 只读（查询 audit_chain 表，不写数据库）
        return action in _READONLY_AUDIT_ACTIONS
    if cmd == "bootstrap":
        # bootstrap status 只读（汇总查询，不写数据库）
        return action in _READONLY_BOOTSTRAP_ACTIONS
    if cmd == "clone":
        # clone list/stats 只读（查询 clone_pairs 表）；clone detect/clear 写
        return action in _READONLY_CLONE_ACTIONS
    if cmd == "tests":
        # tests --build / tests --import 写；其他（含 --history、--reverse）只读
        return not ("--build" in sub_argv or "--import" in sub_argv)
    # C8 Step #1: 新增 subcommand 只读判断
    if cmd in {"search", "grep", "symbol", "file", "query",
               "callers", "callees", "call-chain", "topo",
               "metrics", "complexity", "coupling", "comment-coverage", "uncommented",
               "function-issues", "largest-fns", "coupled-fns", "fn-metrics",
               "who", "ownership-map", "brief", "map", "stats", "status",
               "health-report", "dashboard"}:
        # 这些查询/分析类子命令均为只读，不写数据库
        return True
    if cmd == "workspace":
        # workspace list 只读；register/set/delete 写
        return action in _READONLY_WORKSPACE_ACTIONS
    if cmd == "git":
        # git log/show/stats 只读；git import 写
        return action in _READONLY_GIT_ACTIONS
    if cmd == "semgrep":
        # semgrep list/stats 只读；semgrep scan 视为写（含 --save 选项）
        return action in _READONLY_SEMGREP_ACTIONS
    if cmd == "coverage":
        # coverage fn/uncovered 只读；coverage import 写
        return action in _READONLY_COVERAGE_ACTIONS
    if cmd == "fts":
        # fts status 只读（查询 FTS5 状态）；fts rebuild 写（重建索引）
        return action in _READONLY_FTS_ACTIONS
    if cmd == "graph":
        # F11（2026-07-20 批次6）：graph build-from-c 只读（不写 DB，仅 parse + 内存构 CSR）
        return action in _READONLY_GRAPH_ACTIONS
    if cmd == "config":
        # N4（2026-07-20 批次6）：config explain/paths 只读（只读 TOML）
        return action in _READONLY_CONFIG_ACTIONS
    if cmd == "rollback":
        # Phase 0 子任务 4：rollback config/show/is-rolled-back 只读；register/set 写
        return action in _READONLY_ROLLBACK_ACTIONS
    if cmd == "dependency":
        # P2：dependency inspect/list/cycle/explain 只读；provider-select 写
        return action in {"inspect", "list", "cycle", "explain"}
    if cmd == "identity":
        # P3：identity revoke 追加 Attestation 撤销记录（写操作，Req 10.10-10.12）
        return False
    if cmd == "lease":
        # P4：lease status 只读；acquire/renew/release 写（Req 11.2-11.7）
        return action in {"status", "list"}
    if cmd == "assignment":
        # P4：assignment show/list 只读；create/revoke 写（Req 11.1）
        return action in {"show", "list"}
    if cmd == "refresh":
        # refresh 始终是写操作（build_full_graph / refresh_file）
        return False
    return False


def _is_readonly_args(args) -> bool:
    """判断 flag 模式命令是否为只读（不修改数据库）

    设计原则：不在 _WRITE_FLAGS 集合内的 flag 命令均为只读。
    只读命令跳过 workspace 激活写操作，避免被 MCP Server 写锁卡住。

    Args:
        args: argparse 解析后的 args 对象

    Returns:
        True 表示只读命令，可跳过 workspace 写操作
    """
    for flag in _WRITE_FLAGS:
        if getattr(args, flag, None):
            return False
    return True


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
        elif cmd == "fts":
            return _handle_fts(argv, db)
        elif cmd == "doctor":
            return _handle_doctor(argv, db)
        elif cmd == "install-agent":
            return _handle_install_agent(argv, db)
        elif cmd == "install-hook":
            return _handle_install_hook(argv, db)
        elif cmd == "rule":
            return _handle_rule(argv, db)
        elif cmd == "audit":
            return _handle_audit(argv, db)
        elif cmd == "bootstrap":
            return _handle_bootstrap(argv, db)
        elif cmd == "clone":
            return _handle_clone(argv, db)
        # C8 Step #1: 新增 8 大类 subcommand 调度
        elif cmd == "workspace":
            return _handle_workspace(argv, db)
        elif cmd == "refresh":
            return _handle_refresh(argv, db)
        elif cmd == "stats":
            return _handle_stats(argv, db)
        elif cmd == "health-report":
            return _handle_health_report(argv, db)
        elif cmd == "dashboard":
            return _handle_dashboard(argv, db)
        elif cmd == "status":
            return _handle_status(argv, db)
        elif cmd == "search":
            return _handle_search(argv, db)
        elif cmd == "grep":
            return _handle_grep(argv, db)
        elif cmd == "issues":
            return _handle_issues(argv, db)
        elif cmd == "tests":
            return _handle_tests(argv, db)
        elif cmd == "symbol":
            return _handle_symbol(argv, db)
        elif cmd == "file":
            return _handle_file(argv, db)
        elif cmd == "query":
            return _handle_query(argv, db)
        elif cmd == "callers":
            return _handle_callers(argv, db)
        elif cmd == "callees":
            return _handle_callees(argv, db)
        elif cmd == "call-chain":
            return _handle_call_chain(argv, db)
        elif cmd == "topo":
            return _handle_topo(argv, db)
        elif cmd == "metrics":
            return _handle_metrics(argv, db)
        elif cmd == "complexity":
            return _handle_complexity(argv, db)
        elif cmd == "coupling":
            return _handle_coupling(argv, db)
        elif cmd == "comment-coverage":
            return _handle_comment_coverage(argv, db)
        elif cmd == "uncommented":
            return _handle_uncommented(argv, db)
        elif cmd == "function-issues":
            return _handle_function_issues(argv, db)
        elif cmd == "largest-fns":
            return _handle_largest_fns(argv, db)
        elif cmd == "coupled-fns":
            return _handle_coupled_fns(argv, db)
        elif cmd == "fn-metrics":
            return _handle_fn_metrics(argv, db)
        elif cmd == "git":
            return _handle_git(argv, db)
        elif cmd == "semgrep":
            return _handle_semgrep(argv, db)
        elif cmd == "coverage":
            return _handle_coverage(argv, db)
        elif cmd == "who":
            return _handle_who(argv, db)
        elif cmd == "ownership-map":
            return _handle_ownership_map(argv, db)
        elif cmd == "brief":
            return _handle_brief(argv, db)
        elif cmd == "map":
            return _handle_map(argv, db)
        elif cmd == "toolchain":
            return _handle_toolchain(argv, db)
        elif cmd == "build-context":
            return _handle_build_context(argv, db)
        elif cmd == "graph":
            return _handle_graph(argv, db)
        elif cmd == "config":
            return _handle_config(argv, db)
        elif cmd == "rollback":
            return _handle_rollback(argv, db)
        elif cmd == "experiment":
            return _handle_experiment(argv, db)
        elif cmd == "collab":
            return _handle_collab(argv, db)
        elif cmd == "dependency":
            return _handle_dependency(argv, db)
        elif cmd == "identity":
            return _handle_identity(argv, db)
        elif cmd == "lease":
            return _handle_lease(argv, db)
        elif cmd == "assignment":
            return _handle_assignment(argv, db)
    except sqlite3.OperationalError as e:
        # 锁错误友好提示（写命令在 task report/apply 等执行时也可能遇到锁）
        if "locked" in str(e).lower():
            cprint(get_error("db_locked"), "red")
            sys.exit(2)
        cprint(t("cli.messages.subcommand_fail", cmd=cmd, error=e), "red")
        return True
    except Exception as e:
        cprint(t("cli.messages.subcommand_fail", cmd=cmd, error=e), "red")
        return True

    return False


# --------------------------------------------------------------------
# install-agent：AI Agent 集成包生成（数据驱动，支持动态 Agent 数量 + --global）
# --------------------------------------------------------------------

# Agent 注册表：描述每个 Agent 的配置能力与路径
#
# 字段说明：
# - display:              显示名
# - supports_mcp:         是否支持 MCP
# - supports_hooks:       是否支持生命周期 hooks（仅 claude-code/codex/cursor）
# - supports_rules:       是否生成 skill/rules 文件
# - reads_agents_md:      是否读取项目根 AGENTS.md
# - project_mcp_relpath:  项目级 MCP 配置实际路径（相对项目根，文档/参考用）
# - project_mcp_format:   项目级 MCP 格式（mcpServers / merge_mcpServers）
# - global_mcp_relpath:   全局 MCP 配置路径（~ 展开，--global 模式写入）
# - global_mcp_relpath_win: Windows 下的全局路径（可选，缺省回退 global_mcp_relpath）
# - global_mcp_format:    全局 MCP 合并格式（merge_mcpServers 安全合并）
# - rules_relpath:        规则文件实际路径（相对项目根，文档/参考用）
# - rules_type:           规则文件类型
#       skill_md   → CALLWARDEN.md（claude-code/trae 等）
#       cursor_mdc → callwarden.mdc（cursor）
#       generic_md → callwarden.md（windsurf/kiro/antigravity）
#       codex_skill→ 插件包内 SKILL.md（codex）
# - hooks_type:           hooks 配置类型
#       claude_settings → settings.snippet.json
#       codex_hooks     → 插件包内 hooks/hooks.json
#       none            → 无 hooks
# AGENT_SPECS 数据已迁移至 cli/agent_registry.py 模块
# 通过 get_merged_specs(overlay_path) 获取合并后的 specs（内置 + 外部 JSON 叠加层）


def _mcp_callwarden_entry(root: str) -> dict:
    """构造 callwarden MCP server 配置条目"""
    return {
        "command": "python",
        "args": [os.path.join(root, "cw.py"), "server"],
    }


def _handle_install_agent(args, db):
    """生成 Agent 集成包（MCP + skills/rules + hooks），支持动态注册表与 --global 全局写入"""
    # 先解析 --registry 参数以便传入 get_merged_specs
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--registry", default="")
    pre_opts, _ = pre_parser.parse_known_args(args)
    merged_specs = get_merged_specs(pre_opts.registry)

    parser = argparse.ArgumentParser(
        prog="cw install-agent",
        description=t("cli.messages.install_agent_desc",
                      default=f"Generate Call Warden integration files for {len(merged_specs)} AI agents", count=len(merged_specs)),
        epilog=_get_subcommand_epilog(
            "install-agent", count=len(merged_specs)),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "agent",
        nargs="?",
        choices=list(merged_specs.keys()) + ["all"],
        help=t("cli.messages.install_agent_arg_agent",
               default=f"Target Agent (one of {len(merged_specs)} supported agents or 'all')", count=len(merged_specs)),
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help=t("cli.messages.install_agent_arg_output_dir",
               default="Output directory, defaults to .callwarden/agent-integrations"),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=t("cli.messages.install_agent_arg_force",
               default="Overwrite existing integration files"),
    )
    parser.add_argument(
        "--global",
        dest="global_mode",
        action="store_true",
        help=t("cli.messages.install_agent_arg_global",
               default="Write to user global MCP config instead of project-level bundle"),
    )
    parser.add_argument(
        "--auto-detect",
        action="store_true",
        help=t("cli.messages.install_agent_arg_auto_detect",
               default="Auto-detect installed agents and install for all detected"),
    )
    parser.add_argument(
        "--registry",
        default="",
        help=t("cli.messages.install_agent_arg_registry",
               default="Path to custom agent registry JSON (extends built-in agents)"),
    )
    opts = parser.parse_args(args)

    root = db.workspace_root
    out_root = os.path.abspath(opts.output_dir or os.path.join(
        root, ".callwarden", "agent-integrations"))
    mode = "global" if opts.global_mode else "project"

    # --auto-detect 模式：自动探测已安装的 Agent
    if opts.auto_detect:
        from ..install import CallWardenInstaller
        installer = CallWardenInstaller()
        detected = installer.detect_installed_agents()
        if not detected:
            cprint(t("cli.messages.install_agent_no_detected",
                     default="No supported AI agents detected on this system."), "yellow")
            return True
        # 共享配置家族去重（如 cline 家族只需安装一次）
        detected = installer._deduplicate_by_shared_config(detected)
        agents = [d.agent_key for d in detected]
        cprint(t("cli.messages.install_agent_detected",
                 default="Detected {count} agents: {names}",
                 count=len(agents), names=", ".join(agents)), "cyan")
    elif opts.agent == "all":
        agents = list(merged_specs.keys())
    elif opts.agent is None:
        # 未提供 agent 且未使用 --auto-detect
        parser.error(
            "the following arguments are required: agent (or use --auto-detect)")
    else:
        agents = [opts.agent]

    # --global 模式跳过无全局路径的 agent（如 qoder 走 DeepLink）
    if opts.global_mode:
        skipped = [a for a in agents if not merged_specs[a].get(
            "global_mcp_relpath")]
        agents = [a for a in agents if merged_specs[a].get(
            "global_mcp_relpath")]
        for a in skipped:
            cprint(t(
                "cli.messages.install_agent_global_skipped",
                default="  Skipped {agent}: no global config path (use project mode instead)",
                agent=a,
            ), "yellow")

    if not agents:
        cprint(t("cli.messages.install_agent_no_agents",
               default="No agents to process."), "yellow")
        return True

    created = []
    for agent in agents:
        spec = merged_specs[agent]
        if opts.global_mode:
            created.extend(_write_global_mcp_config(spec, root, opts.force))
        else:
            created.extend(_write_agent_integration(
                root, out_root, agent, spec, opts.force))

    # 输出摘要
    cprint(t("cli.messages.install_agent_title",
           default="=== Agent Integration Generated ==="), "cyan", bold=True)
    print(t("cli.messages.install_agent_root",
          default="  Root: {root}", root=root))
    if not opts.global_mode:
        print(t("cli.messages.install_agent_output",
              default="  Output: {path}", path=out_root))
    print(t("cli.messages.install_agent_agents",
          default="  Agents: {agents}", agents=', '.join(agents)))
    print(t("cli.messages.install_agent_mode",
          default="  Mode: {mode}", mode=mode))
    print()
    cprint(t("cli.messages.install_agent_files",
           default="Files created/updated:"), "cyan")
    for path in created:
        print(t("cli.messages.install_agent_path_item", path=path))
    print()
    cprint(t(
        "cli.messages.install_agent_next",
        default="Next: enable hooks/MCP/plugin using the generated README for the target Agent.",
    ), "green")
    return True


def _write_if_needed(path: str, content: str, force: bool, created: list) -> None:
    """写入文件，默认不覆盖已有内容"""
    if os.path.exists(path) and not force:
        created.append(
            path + t("cli.messages.install_agent_exists_skipped", default=" (exists, skipped)"))
        return
    atomic_write_file(path, content)
    created.append(path)


def _global_mcp_path(spec: dict) -> str:
    """返回 agent 的全局 MCP 配置路径（平台感知，已展开 ~），无则返回空串"""
    if sys.platform == "win32":
        rel = spec.get("global_mcp_relpath_win") or spec.get(
            "global_mcp_relpath")
    else:
        rel = spec.get("global_mcp_relpath")
    if not rel:
        return ""
    return os.path.expanduser(rel)


def _mcp_callwarden_entry_zed(root: str) -> dict:
    """构造 Zed 风格的 MCP server 配置条目（command 为嵌套对象）"""
    return {
        "command": {
            "command": "python",
            "args": [os.path.join(root, "cw.py"), "server"],
        }
    }


def _write_global_mcp_config(spec: dict, root: str, force: bool) -> list:
    """安全合并写入用户全局 MCP 配置（不覆盖已有配置）

    支持三种格式：
    - merge_mcpServers: JSON，合并到 mcpServers 字段（标准 MCP 格式）
    - merge_context_servers: JSON，合并到 context_servers 字段（Zed 格式）
    - toml_mcp_servers: TOML，合并到 [mcp_servers] 节（Codex CLI 格式）
    """
    created = []
    target = _global_mcp_path(spec)
    if not target:
        return created

    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)

    fmt = spec.get("global_mcp_format") or "merge_mcpServers"

    if fmt == "toml_mcp_servers":
        return _write_global_toml_mcp_config(target, root, force)

    # JSON 格式（merge_mcpServers 和 merge_context_servers）
    existing = {}
    existed = os.path.exists(target)
    if existed:
        try:
            with open(target, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if not isinstance(existing, dict):
                existing = {}
        except (ValueError, OSError):
            existing = {}

    if fmt == "merge_context_servers":
        # Zed 格式：context_servers 键 + 嵌套 command 对象
        servers_key = "context_servers"
        entry = _mcp_callwarden_entry_zed(root)
    else:
        # 标准 MCP 格式：mcpServers 键
        servers_key = "mcpServers"
        entry = _mcp_callwarden_entry(root)

    # 安全合并：仅更新 callwarden 条目，保留其他配置
    servers = existing.get(servers_key) or {}
    servers["callwarden"] = entry
    existing[servers_key] = servers

    atomic_write_file(target, json.dumps(
        existing, ensure_ascii=False, indent=2) + "\n")
    if existed:
        created.append(
            target + t("cli.messages.install_agent_updated", default=" (updated)"))
    else:
        created.append(
            target + t("cli.messages.install_agent_created", default=" (created)"))
    return created


def _write_global_toml_mcp_config(target: str, root: str, force: bool) -> list:
    """安全合并写入 TOML 格式的全局 MCP 配置（Codex CLI 用）

    Codex CLI 使用 ~/.codex/config.toml，MCP 配置格式：
        [mcp_servers.callwarden]
        command = "python"
        args = ["path/to/cw.py", "server"]
    """
    created = []
    existed = os.path.exists(target)

    # 尝试读取现有 TOML
    existing_lines = []
    if existed:
        try:
            with open(target, "r", encoding="utf-8") as f:
                existing_lines = f.readlines()
        except OSError:
            existing_lines = []

    cw_cmd = "python"
    cw_args = [os.path.join(root, "cw.py"), "server"]
    # 构造 callwarden 节的 TOML 文本
    args_str = ", ".join(json.dumps(a) for a in cw_args)
    cw_section = (
        f"\n[mcp_servers.callwarden]\n"
        f'command = {json.dumps(cw_cmd)}\n'
        f'args = [{args_str}]\n'
    )

    # 检查是否已有 [mcp_servers.callwarden] 节
    has_cw = any("[mcp_servers.callwarden]" in line for line in existing_lines)

    if has_cw and not force:
        created.append(
            target + t("cli.messages.install_agent_exists_skipped", default=" (exists, skipped)"))
        return created

    if has_cw:
        # 替换已有节：找到 [mcp_servers.callwarden] 行，替换到下一个节或文件末尾
        new_lines = []
        i = 0
        replaced = False
        while i < len(existing_lines):
            line = existing_lines[i]
            if "[mcp_servers.callwarden]" in line:
                # 跳过旧节（到下一个 [ 节或文件末尾）
                i += 1
                while i < len(existing_lines) and not existing_lines[i].strip().startswith("["):
                    i += 1
                # 插入新节
                new_lines.append(cw_section)
                replaced = True
                continue
            new_lines.append(line)
            i += 1
        if not replaced:
            new_lines.append(cw_section)
        content = "".join(new_lines)
    else:
        # 追加新节
        content = "".join(existing_lines)
        if content and not content.endswith("\n"):
            content += "\n"
        content += cw_section

    atomic_write_file(target, content)
    if existed:
        created.append(
            target + t("cli.messages.install_agent_updated", default=" (updated)"))
    else:
        created.append(
            target + t("cli.messages.install_agent_created", default=" (created)"))
    return created


def _write_codex_plugin_package(base: str, root: str, hook_script: str, force: bool) -> list:
    """生成 codex 完整插件包（.codex-plugin/ + skills + hooks + .mcp.json）"""
    created = []
    plugin_root = os.path.join(base, "callwarden-plugin")
    os.makedirs(os.path.join(plugin_root, ".codex-plugin"), exist_ok=True)
    os.makedirs(os.path.join(plugin_root, "skills",
                "callwarden-workflow"), exist_ok=True)
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
        json.dumps({"mcpServers": {"callwarden": _mcp_callwarden_entry(root)}},
                   ensure_ascii=False, indent=2) + "\n",
        force,
        created,
    )
    _write_if_needed(
        os.path.join(plugin_root, "skills", "callwarden-workflow", "SKILL.md"),
        _callwarden_skill_md(),
        force,
        created,
    )
    if hook_script:
        _write_if_needed(
            os.path.join(plugin_root, "hooks", "hooks.json"),
            _codex_hooks_json(hook_script),
            force,
            created,
        )
    return created


def _write_agent_integration(root: str, out_root: str, agent: str, spec: dict, force: bool) -> list:
    """写入单个 Agent 的集成模板（数据驱动，按 spec 生成对应文件）"""
    created = []
    base = os.path.join(out_root, agent)
    os.makedirs(base, exist_ok=True)

    # hooks 脚本（仅支持 hooks 的 agent：claude-code/codex/cursor）
    hook_script = None
    if spec.get("supports_hooks"):
        hook_dir = os.path.join(base, "hooks")
        os.makedirs(hook_dir, exist_ok=True)
        hook_script = os.path.join(hook_dir, "callwarden_hook.py")
        _write_if_needed(hook_script, _agent_hook_script(), force, created)

    hooks_type = spec.get("hooks_type") or "none"
    rules_type = spec.get("rules_type")

    # codex 走完整插件包逻辑
    if agent == "codex":
        created.extend(_write_codex_plugin_package(
            base, root, hook_script, force))
    else:
        # hooks 配置文件
        if hooks_type == "claude_settings" and hook_script:
            _write_if_needed(
                os.path.join(base, "settings.snippet.json"),
                _claude_settings_json(hook_script),
                force,
                created,
            )
        # rules/skill 文件
        if rules_type == "skill_md":
            _write_if_needed(
                os.path.join(base, "CALLWARDEN.md"),
                _callwarden_skill_md(),
                force,
                created,
            )
        elif rules_type == "cursor_mdc":
            _write_if_needed(
                os.path.join(base, "callwarden.mdc"),
                _cursor_rule_mdc(),
                force,
                created,
            )
        elif rules_type == "generic_md":
            _write_if_needed(
                os.path.join(base, "callwarden.md"),
                _generic_rules_md(spec.get("display", agent)),
                force,
                created,
            )
        # MCP 配置 snippet（项目级参考文件，供用户复制到实际路径）
        if spec.get("supports_mcp") and spec.get("project_mcp_relpath"):
            _write_if_needed(
                os.path.join(base, "mcp.json"),
                json.dumps({"mcpServers": {"callwarden": _mcp_callwarden_entry(root)}},
                           ensure_ascii=False, indent=2) + "\n",
                force,
                created,
            )

    _write_if_needed(os.path.join(base, "README.md"),
                     _agent_readme(agent), force, created)
    return created


# --------------------------------------------------------------------
# install-hook：Git hook 安装/卸载
# --------------------------------------------------------------------


def _handle_install_hook(args, db):
    """处理 install-hook 子命令：安装或卸载 Git hook

    用法：
        cw install-hook post-commit                    # 安装（--auto 模式，自动检测 in_progress 任务）
        cw install-hook post-commit --task-id T-xxx    # 安装（硬编码 task_id）
        cw install-hook post-commit --uninstall        # 卸载

    注意：`cw install --hooks` 已默认包含 post-commit（--auto 模式），
    通常无需单独执行此命令。此接口保留用于单独卸载或硬编码 task_id 场景。
    """
    parser = argparse.ArgumentParser(
        prog="cw install-hook",
        description=t(
            "cli.messages.install_hook_desc",
            default="Install or uninstall Call Warden Git hooks",
        ),
    )
    parser.add_argument(
        "hook",
        choices=["post-commit"],
        help=t(
            "cli.messages.install_hook_arg_hook",
            default="Hook name (post-commit: auto capture-diff after commit)",
        ),
    )
    parser.add_argument(
        "--task-id", default="",
        help=t(
            "cli.messages.install_hook_arg_task_id",
            default="Task ID to hardcode in hook (empty = --auto mode, auto-detect in_progress task via active_task)",
        ),
    )
    parser.add_argument(
        "--uninstall", action="store_true",
        help=t(
            "cli.messages.install_hook_arg_uninstall",
            default="Uninstall the hook instead of installing",
        ),
    )
    opts = parser.parse_args(args)

    from ..install import CallWardenInstaller

    installer = CallWardenInstaller()
    success = installer.install_post_commit_hook(
        task_id=opts.task_id,
        uninstall=opts.uninstall,
    )
    return bool(success)


# --------------------------------------------------------------------
# Agent Rule Memory（候选-审核-生效-同步）
# --------------------------------------------------------------------


def _handle_rule(args, db):
    """处理 rule 子命令（候选-审核-生效-同步全生命周期）

    子命令：
    - candidate create/list/accept/reject：候选规则管理
    - list：列出已生效规则
    - applicable：按上下文查询匹配规则
    - sync：把 active 规则同步到 AGENTS.md 标记区
    - insert-block：在 AGENTS.md 末尾插入规则标记块
    - extract：从 task_quality_findings 聚合重复问题生成候选
    """
    parser = argparse.ArgumentParser(
        prog="cw rule",
        description=t(
            "cli_rule_desc", default="Agent Rule Memory: candidate / accept / active / sync"),
        epilog=_get_subcommand_epilog("rule"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # candidate：候选规则子命令组
    cand_p = sub.add_parser(
        "candidate", help=t("cli_rule_candidate_desc", default="Candidate rule lifecycle")
    )
    cand_sub = cand_p.add_subparsers(dest="cand_action", required=True)

    cand_create = cand_sub.add_parser(
        "create", help=t("cli_rule_candidate_create_desc", default="Create pending candidate rule")
    )
    cand_create.add_argument("--title", required=True,
                             help=t("cli_rule_arg_title", default="Rule title (short)"))
    cand_create.add_argument("--text", required=True,
                             help=t("cli_rule_arg_text", default="Rule text (Agent will receive it verbatim)"))
    cand_create.add_argument("--scope", default="",
                             help=t("cli_rule_arg_scope", default='Scope JSON, e.g. {"languages":["python"],"actions":["edit"]}'))
    cand_create.add_argument("--severity", default="info",
                             help=t("cli_rule_arg_severity", default="Severity: critical/error/warning/info"))
    cand_create.add_argument("--source", default="manual",
                             help=t("cli_rule_arg_source", default="Source: manual/auto_quality_findings/auto_semgrep/task_review/other"))
    cand_create.add_argument("--evidence", default="",
                             help=t("cli_rule_arg_evidence", default='Evidence JSON, e.g. {"task_id":"T-xxx","occurrences":3}'))
    cand_create.add_argument("--confidence", type=float, default=0.0,
                             help=t("cli_rule_arg_confidence", default="Confidence 0.0-1.0"))

    cand_list = cand_sub.add_parser(
        "list", help=t("cli_rule_candidate_list_desc", default="List candidate rules")
    )
    cand_list.add_argument("--status", default="pending",
                           help=t("cli_rule_arg_status_filter", default="Status filter: pending/accepted/rejected, empty = all"))
    cand_list.add_argument("--limit", type=int, default=50,
                           help=t("cli_rule_arg_limit", default="Maximum count (default 50)"))

    cand_accept = cand_sub.add_parser(
        "accept", help=t("cli_rule_candidate_accept_desc", default="Accept candidate -> active rule")
    )
    cand_accept.add_argument("candidate_id",
                             help=t("cli_rule_arg_candidate_id", default="Candidate rule ID (ARC-xxx)"))
    cand_accept.add_argument("--reviewer", default="agent",
                             help=t("cli_rule_arg_reviewer", default="Reviewer identifier"))

    cand_reject = cand_sub.add_parser(
        "reject", help=t("cli_rule_candidate_reject_desc", default="Reject candidate rule")
    )
    cand_reject.add_argument("candidate_id",
                             help=t("cli_rule_arg_candidate_id", default="Candidate rule ID (ARC-xxx)"))
    cand_reject.add_argument("--reviewer", default="agent",
                             help=t("cli_rule_arg_reviewer", default="Reviewer identifier"))
    cand_reject.add_argument("--reason", default="",
                             help=t("cli_rule_arg_reason", default="Reject reason (optional)"))

    # list：已生效规则
    list_p = sub.add_parser(
        "list", help=t("cli_rule_list_desc", default="List active rules")
    )
    list_p.add_argument("--status", default="active",
                        help=t("cli_rule_arg_status_filter_active", default="Status: active/deprecated/removed, empty = all"))
    list_p.add_argument("--limit", type=int, default=100,
                        help=t("cli_rule_arg_limit_active", default="Maximum count (default 100)"))

    # applicable：按上下文查询
    app_p = sub.add_parser(
        "applicable", help=t("cli_rule_applicable_desc", default="Get applicable rules by context")
    )
    app_p.add_argument("--context", default="{}",
                       help=t("cli_rule_arg_context", default='Context JSON, e.g. {"languages":["python"],"actions":["edit"]}'))
    app_p.add_argument("--limit", type=int, default=10,
                       help=t("cli_rule_arg_applicable_limit", default="Maximum count (default 10)"))

    # sync：同步到 AGENTS.md
    sync_p = sub.add_parser(
        "sync", help=t("cli_rule_sync_desc", default="Sync active rules to AGENTS.md marker block")
    )
    sync_p.add_argument("--target", default="AGENTS.md",
                        help=t("cli_rule_arg_target", default="Target AGENTS.md path (relative to workspace or absolute)"))
    sync_p.add_argument("--apply", action="store_true",
                        help=t("cli_rule_arg_apply", default="Actually write to file (default: dry-run)"))
    sync_p.add_argument("--actor", default="agent",
                        help=t("cli_rule_arg_actor", default="Actor identifier"))

    # insert-block：插入标记块
    insert_p = sub.add_parser(
        "insert-block", help=t("cli_rule_insert_block_desc", default="Insert marker block at end of AGENTS.md")
    )
    insert_p.add_argument("--target", default="AGENTS.md",
                          help=t("cli_rule_arg_target_insert", default="Target AGENTS.md path"))
    insert_p.add_argument("--actor", default="agent",
                          help=t("cli_rule_arg_actor", default="Actor identifier"))

    # extract：从质量发现聚合
    extract_p = sub.add_parser(
        "extract", help=t("cli_rule_extract_desc", default="Extract candidate rules from task quality findings")
    )
    extract_p.add_argument("--task-id", default="",
                           help=t("cli_rule_arg_task_id_extract", default="Task ID (empty = scan all tasks)"))
    extract_p.add_argument("--min-occurrences", type=int, default=2,
                           help=t("cli_rule_arg_min_occurrences", default="Min occurrences threshold (default 2)"))

    # seed-bootstrap：种子化内置自举规则
    seed_p = sub.add_parser(
        "seed-bootstrap", help=t("cli_rule_seed_bootstrap_desc", default="Seed built-in bootstrap active rules")
    )
    seed_p.add_argument("--apply", action="store_true",
                        help=t("cli_rule_seed_bootstrap_arg_apply", default="Actually write to db (default: dry-run)"))

    # cleanup-sync-log：清理 agent_rule_sync_log 旧记录（C6 GC）
    cleanup_p = sub.add_parser(
        "cleanup-sync-log",
        help=t("cli_rule_cleanup_sync_log_desc",
               default="Cleanup old agent_rule_sync_log records")
    )
    cleanup_p.add_argument("--older-than", type=int, default=90,
                           help=t("cli_rule_cleanup_sync_log_arg_older_than", default="Records older than N days (default 90)"))
    cleanup_p.add_argument("--keep-latest", type=int, default=100,
                           help=t("cli_rule_cleanup_sync_log_arg_keep_latest", default="Keep latest N records (default 100)"))
    cleanup_p.add_argument("--apply", action="store_true",
                           help=t("cli_rule_cleanup_sync_log_arg_apply", default="Actually delete (default: dry-run)"))

    opts = parser.parse_args(args)

    if opts.action == "candidate":
        return _handle_rule_candidate(opts, db)
    elif opts.action == "list":
        return _handle_rule_list(opts, db)
    elif opts.action == "applicable":
        return _handle_rule_applicable(opts, db)
    elif opts.action == "sync":
        return _handle_rule_sync(opts, db)
    elif opts.action == "insert-block":
        return _handle_rule_insert_block(opts, db)
    elif opts.action == "extract":
        return _handle_rule_extract(opts, db)
    elif opts.action == "seed-bootstrap":
        return _handle_rule_seed_bootstrap(opts, db)
    elif opts.action == "cleanup-sync-log":
        return _handle_rule_cleanup_sync_log(opts, db)
    return True


def _handle_rule_candidate(opts, db):
    """处理 rule candidate 子命令组"""
    if opts.cand_action == "create":
        scope = _parse_json_arg(opts.scope, default={})
        evidence = _parse_json_arg(opts.evidence, default={})
        try:
            cid = db.rule_candidate_create(
                title=opts.title,
                rule_text=opts.text,
                scope=scope,
                severity=opts.severity,
                source=opts.source,
                evidence=evidence,
                confidence=opts.confidence,
            )
            cprint(t("cli.messages.rule_candidate_created",
                   default="Created candidate: {id}", id=cid), "green")
        except Exception as e:
            cprint(t("cli.messages.rule_candidate_create_failed",
                   default="Create failed: {error}", error=e), "red")
        return True

    elif opts.cand_action == "list":
        rows = db.rule_candidate_list(status=opts.status, limit=opts.limit)
        cprint(t("cli.messages.rule_candidate_list_title",
                 default="=== Candidates ({count}) ===", count=len(rows)), "cyan", bold=True)
        if not rows:
            print(t("cli.messages.rule_candidate_list_empty", default="(empty)"))
            return True
        for r in rows:
            print(t("cli.messages.rule_candidate_item",
                    default="[{id}] {title} (severity={sev}, source={src}, status={status})",
                    id=r["id"], title=r["title"], sev=r["severity"], src=r["source"], status=r["status"]))
            print(t("cli.messages.rule_candidate_text",
                    default="    text: {text}", text=r["rule_text"]))
            if r.get("scope"):
                print(t("cli.messages.rule_candidate_scope",
                        default="    scope: {scope}", scope=json.dumps(r["scope"], ensure_ascii=False)))
        return True

    elif opts.cand_action == "accept":
        try:
            rid = db.rule_candidate_accept(
                candidate_id=opts.candidate_id, reviewer=opts.reviewer)
            cprint(t("cli.messages.rule_candidate_accepted",
                     default="Accepted: candidate={cid} -> active_rule={rid}",
                     cid=opts.candidate_id, rid=rid), "green")
        except Exception as e:
            cprint(t("cli.messages.rule_candidate_accept_failed",
                     default="Accept failed: {error}", error=e), "red")
        return True

    elif opts.cand_action == "reject":
        try:
            ok = db.rule_candidate_reject(
                candidate_id=opts.candidate_id, reviewer=opts.reviewer, reason=opts.reason,
            )
            cprint(t("cli.messages.rule_candidate_rejected",
                     default="Rejected: {cid} ({ok})", cid=opts.candidate_id, ok=ok), "yellow")
        except Exception as e:
            cprint(t("cli.messages.rule_candidate_reject_failed",
                     default="Reject failed: {error}", error=e), "red")
        return True
    return True


def _handle_rule_list(opts, db):
    """rule list 子命令"""
    rules = db.rule_list(status=opts.status, limit=opts.limit)
    cprint(t("cli.messages.rule_list_title",
             default="=== Active Rules ({count}) ===", count=len(rules)), "cyan", bold=True)
    if not rules:
        print(t("cli.messages.rule_list_empty", default="(empty)"))
        return True
    for r in rules:
        print(t("cli.messages.rule_item",
                default="[{id}] {title} (severity={sev}, synced={synced})",
                id=r["id"], title=r["title"], sev=r["severity"],
                synced=("yes" if r.get("synced_to_agents_md") else "no")))
        print(t("cli.messages.rule_text",
                default="    text: {text}", text=r["rule_text"]))
        if r.get("scope"):
            print(t("cli.messages.rule_scope",
                    default="    scope: {scope}", scope=json.dumps(r["scope"], ensure_ascii=False)))
    return True


def _handle_rule_applicable(opts, db):
    """rule applicable 子命令"""
    context = _parse_json_arg(opts.context, default={})
    rules = db.get_applicable_rules(context=context, limit=opts.limit)
    cprint(t("cli.messages.rule_applicable_title",
             default="=== Applicable Rules ({count}) ===", count=len(rules)), "cyan", bold=True)
    if not rules:
        print(t("cli.messages.rule_applicable_empty", default="(no rule matched)"))
        return True
    for r in rules:
        print(t("cli.messages.rule_applicable_item",
                default="[{id}] {title} (severity={sev})",
                id=r.get("id", ""), title=r.get("title", ""), sev=r.get("severity", "info")))
        print(t("cli.messages.rule_text",
                default="    text: {text}", text=r.get("rule_text", "")))
    return True


def _handle_rule_sync(opts, db):
    """rule sync 子命令"""
    result = db.rule_sync_agents_md(
        target_path=opts.target,
        dry_run=not opts.apply,
        actor=opts.actor,
    )
    if not result.get("success"):
        cprint(t("cli.messages.rule_sync_failed",
                 default="Sync failed: {error}", error=result.get("error", "")), "red")
        if result.get("suggested_block"):
            print()
            cprint(t("cli.messages.rule_sync_suggested_block_title",
                     default="Suggested marker block (insert into AGENTS.md first):"), "yellow")
            print(result["suggested_block"])
        return True

    if result.get("dry_run"):
        cprint(t("cli.messages.rule_sync_dry_run_title",
                 default="=== Dry-run Preview ({count} rules) ===",
                 count=result.get("rule_count", 0)), "cyan", bold=True)
        print(t("cli.messages.rule_sync_target_label",
                default="target: {path}", path=opts.target))
        print(t("cli.messages.rule_sync_after_hash_label",
                default="after_hash: {hash}", hash=result.get("after_hash", "")[:16]))
        print()
        print(result.get("preview", ""))
        print()
        cprint(t("cli.messages.rule_sync_dry_run_hint",
                 default="Use --apply to write to file."), "yellow")
    else:
        cprint(t("cli.messages.rule_sync_apply_title",
                 default="=== Synced ({count} rules) ===",
                 count=result.get("rule_count", 0)), "green", bold=True)
        print(t("cli.messages.rule_sync_target_label",
                default="target: {path}", path=opts.target))
        print(t("cli.messages.rule_sync_after_hash_label",
                default="after_hash: {hash}", hash=result.get("after_hash", "")[:16]))
    return True


def _handle_rule_insert_block(opts, db):
    """rule insert-block 子命令"""
    result = db.rule_insert_agents_md_block(
        target_path=opts.target, actor=opts.actor)
    if result.get("success"):
        cprint(t("cli.messages.rule_insert_block_success",
                 default="Inserted marker block: {path}", path=opts.target), "green")
    else:
        cprint(t("cli.messages.rule_insert_block_failed",
                 default="Insert failed: {msg}", msg=result.get("message", "")), "yellow")
    return True


def _handle_rule_extract(opts, db):
    """rule extract 子命令"""
    cids = db.extract_rule_candidates_from_quality_findings(
        task_id=opts.task_id, min_occurrences=opts.min_occurrences,
    )
    cprint(t("cli.messages.rule_extract_title",
             default="=== Extracted Candidates ({count}) ===",
             count=len(cids)), "cyan", bold=True)
    if opts.task_id:
        print(t("cli.messages.rule_extract_task_filter",
                default="task_id: {tid}", tid=opts.task_id))
    print(t("cli.messages.rule_extract_threshold",
            default="min_occurrences: {n}", n=opts.min_occurrences))
    if not cids:
        print(t("cli.messages.rule_extract_empty",
                default="(no repeated findings above threshold)"))
        return True
    for cid in cids:
        print(t("cli.messages.rule_extract_item",
                default="  - {cid}", cid=cid))
    return True


def _handle_rule_seed_bootstrap(opts, db):
    """rule seed-bootstrap 子命令：种子化内置自举 active rules

    通过固定 ID（AR-bootstrap-*）实现幂等，已存在则跳过或更新。
    """
    result = db.rule_seed_bootstrap(dry_run=not opts.apply)
    total = result.get("total", 0)
    created = result.get("created", 0)
    updated = result.get("updated", 0)
    skipped = result.get("skipped", 0)

    if result.get("dry_run"):
        cprint(t("cli.messages.rule_seed_bootstrap_dry_run_title",
                 default="=== Bootstrap Seed Dry-Run ({total} rules) ===",
                 total=total), "cyan", bold=True)
    else:
        cprint(t("cli.messages.rule_seed_bootstrap_apply_title",
                 default="=== Bootstrap Seed Applied ({total} rules) ===",
                 total=total), "green", bold=True)

    print(t("cli.messages.rule_seed_bootstrap_summary",
            default="created: {created} | updated: {updated} | skipped: {skipped}",
            created=created, updated=updated, skipped=skipped))
    print()

    for rule in result.get("rules", []):
        action = rule.get("action", "skip")
        rid = rule.get("id", "")
        title = rule.get("title", "")
        if action == "create":
            color = "green"
        elif action == "update":
            color = "yellow"
        else:
            color = "white"
        cprint(t("cli.messages.rule_seed_bootstrap_item",
                 default="[{action}] {id}  {title}",
                 action=action, id=rid, title=title), color)

    if result.get("dry_run"):
        print()
        cprint(t("cli.messages.rule_seed_bootstrap_dry_run_hint",
                 default="Use --apply to write rules to agent_rules table."), "yellow")
    return True


def _handle_rule_cleanup_sync_log(opts, db):
    """rule cleanup-sync-log 子命令：清理 agent_rule_sync_log 旧记录（C6 GC）

    默认 dry-run，需 --apply 才真正执行删除。
    """
    result = db.cleanup_sync_log(
        older_than_days=opts.older_than,
        keep_latest=opts.keep_latest,
        dry_run=not opts.apply,
    )

    total_before = result.get("total_before", -1)
    deleted = result.get("deleted_count", 0)
    remaining = result.get("remaining_count", -1)

    if result.get("dry_run"):
        cprint(t("cli.messages.rule_cleanup_sync_log_dry_run_title",
                 default="=== Sync Log Cleanup Dry-Run ===",
                 older_than=opts.older_than, keep_latest=opts.keep_latest), "cyan", bold=True)
    else:
        cprint(t("cli.messages.rule_cleanup_sync_log_apply_title",
                 default="=== Sync Log Cleanup Applied ===",
                 older_than=opts.older_than, keep_latest=opts.keep_latest), "green", bold=True)

    if not result.get("success"):
        cprint(t("cli.messages.rule_cleanup_sync_log_failed",
                 default="Cleanup failed: {error}", error=result.get("error", "")), "red")
        return True

    print(t("cli.messages.rule_cleanup_sync_log_summary",
            default="total_before: {total_before} | deleted: {deleted} | remaining: {remaining}",
            total_before=total_before, deleted=deleted, remaining=remaining))

    if result.get("dry_run"):
        print()
        cprint(t("cli.messages.rule_cleanup_sync_log_dry_run_hint",
                 default="Use --apply to actually delete records."), "yellow")
    return True


def _parse_json_arg(raw: str, default=None):
    """解析 JSON 命令行参数，失败返回 default

    - raw 为空时返回 default（若 default=None 则返回 {}）
    - 解析失败或非 dict/list 时返回 default（若 default=None 则返回 {}）
    - 显式传 default=None 表示希望返回 None 时，需改传 default=... 哨兵值
    """
    fallback = {} if default is None else default
    if not raw:
        return fallback
    try:
        result = json.loads(raw)
        return result if isinstance(result, (dict, list)) else fallback
    except (ValueError, TypeError):
        return fallback


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

Full usage guide: https://github.com/nuoyazhizhou/callwarden/blob/master/docs/agent-usage-guide.md

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


def _generic_rules_md(display: str) -> str:
    """通用 rules 文件（windsurf/kiro/antigravity 等 .md 格式）"""
    return t("cli.messages.install_agent_generic_rule", default="""---
description: Call Warden workflow for codebase-aware Agent tasks
globs: "**/*"
---

When the Call Warden MCP server is available, prefer it for broad or risky coding tasks in {display}.

- Use `work_next_job` for task-driven work instead of manually deciding the next file.
- Use symbol/range patch tools (`propose_range_patch` / `propose_symbol_patch`) instead of full-file rewrites.
- Use Call Warden impact and guardrail tools before DB/API/config changes.
- After editing, report the step with `task_report_step` and include changed files.
- Avoid destructive git cleanup commands (`git reset --hard`, `git checkout .`, `git clean -fd`) unless explicitly requested.

Full usage guide: https://github.com/nuoyazhizhou/callwarden/blob/master/docs/agent-usage-guide.md
""", display=display)


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
        description=t("cli.messages.guardrail_desc",
                      default="Production safety guardrails"),
        epilog=_get_subcommand_epilog("guardrail"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="action", required=True)

    scan_p = sub.add_parser("scan", help=t(
        "cli.messages.guardrail_scan_help", default="Scan guardrail violations"))
    scan_p.add_argument("--file", default="", help=t(
        "cli.messages.guardrail_file_help", default="Filter by file path prefix"))
    scan_p.add_argument("--category", default="",
                        help=t("cli.messages.guardrail_category_help", default="Filter by category (db_safety/api_compat/incident)"))

    rules_p = sub.add_parser("rules", help=t(
        "cli.messages.guardrail_rules_help", default="List guardrail rules"))
    rules_p.add_argument("--category", default="", help=t(
        "cli.messages.guardrail_category_filter_help", default="Filter by category"))

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
        print(t("cli.messages.guardrail_rules_count",
              count=len(rules), cat_info=cat_info))
        print()

        if not rules:
            cprint(t("cli.messages.guardrail_rules_none"), "dim")
        else:
            sev_icon = {"block": "[!]", "warn": "[~]", "info": "[i]"}
            for i, r in enumerate(rules, 1):
                sev = r.get("severity", "warn")
                icon = sev_icon.get(sev, "[?]")
                builtin = t("cli.messages.guardrail_builtin") if r.get(
                    "is_builtin") else t("cli.messages.guardrail_custom")
                print(t("cli.messages.guardrail_rules_item",
                        idx=i, icon=icon, rule_id=r['rule_id'],
                        category=r.get('category', ''), builtin=builtin))
                print(t("cli.messages.guardrail_rules_severity",
                        severity=sev, action=r.get('action', '')))
                if r.get("description"):
                    print(t("cli.messages.guardrail_rules_desc",
                          desc=r['description']))
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
        description=t("cli.messages.impact_desc",
                      default="Change impact radius analysis"),
        epilog=_get_subcommand_epilog("impact"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("symbol_hash", help=t(
        "cli.messages.impact_symbol_hash_help", default="Source symbol hash"))
    parser.add_argument("--depth", type=int, default=3, help=t(
        "cli.messages.impact_depth_help", default="Maximum BFS traversal depth (default: 3)"))

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
    parser = argparse.ArgumentParser(
        prog="cw review",
        description=t("cli.messages.review_title"),
        epilog=_get_subcommand_epilog("review"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("symbol_hash", help=t(
        "cli_review_arg_symbol_hash", default="Source symbol hash"))

    opts = parser.parse_args(args)
    report = db.review_readiness_report(opts.symbol_hash)

    cprint(t("cli.messages.review_title"), "cyan", bold=True)

    scope = report.get("impact_scope", "low")
    scope_color = {"high": "red", "medium": "yellow",
                   "low": "green"}.get(scope, "white")
    scope_i18n = {
        "high": t("cli.messages.review_scope_high"),
        "medium": t("cli.messages.review_scope_medium"),
        "low": t("cli.messages.review_scope_low"),
    }.get(scope, scope)
    print(t("cli.messages.review_risk_level"), end="")
    cprint(scope_i18n, scope_color, bold=True)
    print(t("cli.messages.review_impact_scope",
          count=report.get('total_impacted', 0)))
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
                print(t("cli.messages.review_must_test_file",
                      path=m['file_path']))
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
        print(t("cli.messages.review_coverage_fn",
              name=cov.get('qualified_name', '')))
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
    parser = argparse.ArgumentParser(
        prog="cw evolution",
        description=t("cli.messages.evolution_title"),
        epilog=_get_subcommand_epilog("evolution"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("qualified_name", help=t(
        "cli_evolution_arg_qualified_name", default="Function qualified name"))
    parser.add_argument("--window", default="", help=t("cli_evolution_arg_window",
                        default="Time window (for example 30d/90d/1y)"))
    parser.add_argument("--defects", action="store_true",
                        help="Show defect correlation (changes vs defects introduced)")

    opts = parser.parse_args(args)

    # --defects 模式：显示变更-缺陷关联
    if opts.defects:
        result = db.get_defect_correlation_by_qn(opts.qualified_name)
        cprint("Defect Correlation", "cyan", bold=True)
        print(f"  Function:       {result['qualified_name']}")
        print(f"  Change count:   {result['change_count']}")
        print(f"  Defect count:   {result['defect_count']}")
        print(f"  Defect rate:    {result['defect_rate']:.1%}")
        if result.get("defect_types"):
            print(f"  Defect types:   {result['defect_types']}")
        print()
        recent = result.get("recent_defects", [])
        if recent:
            print(f"Recent defects ({len(recent)} shown):")
            for d in recent:
                sev = d.get("severity", "?").upper()
                rule = d.get("rule_id", "?")
                msg = d.get("message", "")
                line = d.get("start_line", 0)
                line_info = f" L{line}" if line else ""
                print(f"  [{sev}] {rule}{line_info}: {msg}")
        else:
            print("No defects found after changes.")
        return True

    result = db.function_change_frequency(
        opts.qualified_name, time_window=opts.window)

    cprint(t("cli.messages.evolution_title"), "cyan", bold=True)
    print(t("cli.messages.evolution_function",
          name=result.get('qualified_name', '')))

    window_info = (t("cli.messages.evolution_window", window=opts.window)
                   if opts.window
                   else t("cli.messages.evolution_all_history"))
    print(t("cli.messages.evolution_change_count",
            count=result.get('change_count', 0), window=window_info))

    if result.get("first_seen"):
        first = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(result["first_seen"]))
        print(t("cli.messages.evolution_first_seen", time=first))
    if result.get("last_changed"):
        last = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(result["last_changed"]))
        print(t("cli.messages.evolution_last_changed", time=last))

    avg_interval = result.get("avg_interval", 0)
    if avg_interval > 0:
        print(t("cli.messages.evolution_avg_interval",
              days=f"{avg_interval / 86400:.1f}"))
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
        for i, tl in enumerate(timeline[:20], 1):
            ts = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(tl.get("timestamp", 0)))
            author = (tl.get("author", "") or "unknown")[:12]
            msg = (tl.get("message", "") or "")[:50]
            commit = (tl.get("commit_hash", "") or "")[:8]
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
            print(t("cli.messages.evolution_distribution_item",
                  period=period, counts=counts))
        print()

    return True


def _handle_hotspot(args, db):
    """处理 hotspot 子命令（热点函数排名）"""
    parser = argparse.ArgumentParser(
        prog="cw hotspot",
        description=t("cli.messages.hotspot_title"),
        epilog=_get_subcommand_epilog("hotspot"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--module", default="", help=t(
        "cli_hotspot_arg_module", default="Filter by module path prefix"))
    parser.add_argument("--limit", type=int, default=20, help=t(
        "cli_hotspot_arg_limit", default="Number of items to show (default: 20)"))

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
    parser = argparse.ArgumentParser(
        prog="cw churn",
        description=t("cli.messages.churn_title"),
        epilog=_get_subcommand_epilog("churn"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--module", default="", help=t("cli_churn_arg_module",
                        default="Filter by module path prefix"))
    parser.add_argument("--window", default="90d",
                        help=t("cli_churn_arg_window", default="Time window (default: 90d)"))

    opts = parser.parse_args(args)
    result = db.churn_analysis(
        module_filter=opts.module, time_window=opts.window)

    cprint(t("cli.messages.churn_title"), "cyan", bold=True)
    mod_info = (t("cli.messages.hotspot_module_info", module=opts.module)
                if opts.module else "")
    print(t("cli.messages.churn_window", window=opts.window, mod_info=mod_info))
    print()

    print(t("cli.messages.churn_changed_files",
          count=result.get('changed_files', 0)))
    print(t("cli.messages.churn_total_lines",
          count=result.get('total_lines_current', 0)))
    print(t("cli.messages.churn_total_churned",
          count=result.get('total_churned_lines', 0)))
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
            print(t("cli.messages.churn_top_file_detail",
                  changes=changes, churned=churned))
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
            print(t("cli.messages.churn_trend_item",
                  date=date, bar=bar, lines=lines))
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
    parser = argparse.ArgumentParser(
        prog="cw defect",
        description=t("cli_defect_desc", default="Defect knowledge base"),
        epilog=_get_subcommand_epilog("defect"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="action", required=True)

    search_p = sub.add_parser("search", help=t(
        "cli_defect_search_desc", default="Search defect patterns"))
    search_p.add_argument("--category", default="", help=t(
        "cli_defect_arg_category", default="Category filter (prefix match)"))
    search_p.add_argument("--severity", default="",
                          help=t("cli_defect_arg_severity", default="Severity filter (error/warning/info)"))
    search_p.add_argument("--limit", type=int, default=20,
                          help=t("cli_defect_arg_limit", default="Number of items to show"))

    suggest_p = sub.add_parser("suggest", help=t(
        "cli_defect_suggest_desc", default="Suggest fixes"))
    suggest_p.add_argument("symbol_hash", help=t(
        "cli_defect_arg_symbol_hash", default="Symbol content hash"))
    suggest_p.add_argument("--finding", type=int, default=0,
                           help=t("cli_defect_arg_finding", default="Specific finding ID"))

    learn_p = sub.add_parser("learn", help=t(
        "cli_defect_learn_desc", default="Learn defect patterns from a fix commit"))
    learn_p.add_argument("commit_hash", help=t(
        "cli_defect_arg_commit_hash", default="Fix commit hash"))

    sub.add_parser("stats", help=t("cli_defect_stats_desc",
                   default="Defect knowledge base statistics"))
    sub.add_parser("build", help=t("cli_defect_build_desc",
                   default="Build defect knowledge base"))

    opts = parser.parse_args(args)
    sev_icon_map = {"error": "[!]", "warning": "[~]", "info": "[i]"}

    if opts.action == "search":
        patterns = db.defect_pattern_search(
            category=opts.category, severity_filter=opts.severity
        )

        cprint(t("cli.messages.defect_search_title"), "cyan", bold=True)
        filter_parts = []
        if opts.category:
            filter_parts.append(
                t("cli.messages.defect_filter_label", cat=opts.category))
        if opts.severity:
            filter_parts.append(
                t("cli.messages.defect_severity_label", sev=opts.severity))
        filter_str = " | ".join(filter_parts) if filter_parts else t(
            "cli.messages.defect_filter_all")
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
                print(t("cli.messages.defect_search_more",
                      count=len(patterns) - opts.limit))
        print()

    elif opts.action == "suggest":
        result = db.suggest_fix(opts.symbol_hash, finding_id=opts.finding)

        cprint(t("cli.messages.defect_suggest_title"), "cyan", bold=True)
        print(t("cli.messages.defect_suggest_symbol",
              hash=opts.symbol_hash[:12]))
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
                print(t("cli.messages.defect_suggest_fix_truncated"))
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
        print(t("cli.messages.defect_learn_patterns",
              count=result.get('learned_patterns', 0)))
        print(t("cli.messages.defect_learn_fixes",
              count=result.get('learned_fixes', 0)))

        details = result.get("details", [])
        if details:
            print()
            print(t("cli.messages.defect_learn_details_title", count=len(details)))
            for i, d in enumerate(details[:20], 1):
                print(t("cli.messages.defect_learn_detail_item", idx=i, detail=d))
            if len(details) > 20:
                print(t("cli.messages.defect_learn_more_details",
                      count=len(details) - 20))
        print()

    elif opts.action == "stats":
        stats = db.defect_stats()

        cprint(t("cli.messages.defect_stats_title"), "cyan", bold=True)
        print(t("cli.messages.defect_stats_patterns",
              count=stats.get('total_patterns', 0)))
        print(t("cli.messages.defect_stats_fixes",
              count=stats.get('total_fixes', 0)))
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
                print(t("cli.messages.defect_stats_by_sev_item",
                      icon=icon, sev=sev, cnt=cnt))
            print()

        top = stats.get("top_defects", [])
        if top:
            print(t("cli.messages.defect_stats_top_title"))
            for i, d in enumerate(top, 1):
                pid = d.get("pattern_id", "")
                cat = d.get("category", "")
                cnt = d.get("case_count", 0)
                desc = (d.get("description", "") or "")[:50]
                print(t("cli.messages.defect_stats_top_item",
                      idx=i, cat=cat, pid=pid, cnt=cnt))
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
        print(t("cli.messages.defect_build_patterns",
              count=result.get('patterns_built', 0)))
        print(t("cli.messages.defect_build_fixes",
              count=result.get('fixes_learned', 0)))

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
    parser = argparse.ArgumentParser(
        prog="cw task",
        description=t("cli_task_desc", default="Task management"),
        epilog=_get_subcommand_epilog("task"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # create：创建任务和步骤
    create_p = sub.add_parser("create", help=t(
        "cli_task_create_desc", default="Create task and steps"))
    create_p.add_argument("--title", required=True,
                          help=t("cli_task_arg_title", default="Task title"))
    create_p.add_argument(
        "--desc", default="", help=t("cli_task_arg_desc", default="Task description"))
    create_p.add_argument("--steps", default="",
                          help=t("cli_task_arg_steps", default='Step JSON array, for example [{"action":"annotate","target_file":"a.py"}]'))

    # next：领取下一个待执行步骤
    next_p = sub.add_parser("next", help=t(
        "cli_task_next_desc", default="Claim current pending step"))
    next_p.add_argument("task_id", help=t(
        "cli_task_arg_task_id", default="Task ID"))

    # report：回报步骤执行结果
    report_p = sub.add_parser("report", help=t(
        "cli_task_report_desc", default="Report step result"))
    report_p.add_argument("task_id", help=t(
        "cli_task_arg_task_id", default="Task ID"))
    report_p.add_argument("step_id", help=t(
        "cli_task_arg_step_id", default="Step ID"))
    report_p.add_argument("--result", default="",
                          help=t("cli_task_arg_result", default="Result description"))
    report_p.add_argument("--fail", action="store_true", help=t(
        "cli_task_arg_fail", default="Mark as failed (default: success)"))

    # rollback：回滚变更
    rollback_p = sub.add_parser("rollback", help=t(
        "cli_task_rollback_desc", default="Roll back changes"))
    rollback_p.add_argument("task_id", help=t(
        "cli_task_arg_task_id", default="Task ID"))
    rollback_p.add_argument("step_id", help=t("cli_task_arg_step_id_rollback",
                            default="Step ID (used as change_id to locate rollback scope)"))

    # apply：审核通过（review -> applied），由其他会话的 LLM 调用
    apply_p = sub.add_parser("apply", help=t(
        "cli_task_apply_desc", default="Approve task (review -> applied)"))
    apply_p.add_argument("task_id", help=t(
        "cli_task_arg_task_id", default="Task ID"))
    apply_p.add_argument(
        "--reviewer", default="reviewer",
        help=t("cli_task_arg_reviewer", default="Reviewer identity")
    )

    # close：关闭任务（applied -> closed），由其他会话的 LLM 调用
    close_p = sub.add_parser("close", help=t(
        "cli_task_close_desc", default="Close task (applied -> closed)"))
    close_p.add_argument("task_id", help=t(
        "cli_task_arg_task_id", default="Task ID"))
    close_p.add_argument(
        "--reviewer", default="reviewer",
        help=t("cli_task_arg_reviewer", default="Reviewer identity")
    )

    # reopen：重新打开任务（review/applied/closed -> in_progress），用于 code review 发现问题或挂新子任务
    reopen_p = sub.add_parser(
        "reopen",
        help=t("cli_task_reopen_desc",
               default="Reopen task (review/applied/closed -> in_progress)"),
    )
    reopen_p.add_argument("task_id", help=t(
        "cli_task_arg_task_id", default="Task ID"))
    reopen_p.add_argument(
        "--reviewer", default="reviewer",
        help=t("cli_task_arg_reviewer", default="Reviewer identity")
    )
    reopen_p.add_argument(
        "--reason", default="",
        help=t("cli_task_arg_reopen_reason",
               default="Reason for reopening (optional)")
    )

    # P3：task report/apply/close/reopen 接收结构化身份（Req 10.1-10.7）。
    # --reviewer 自由文本不是身份证明（Req 10.5）；身份证明只能来自结构化
    # Identity 与 daemon 签发的 Attestation（Req 10.8, 14.13）。
    for _identity_parser in (report_p, apply_p, close_p, reopen_p):
        _identity_parser.add_argument(
            "--agent-id", default="", metavar="ID",
            help=t("cli_task_arg_agent_id", default="Agent ID (P3 Identity)"))
        _identity_parser.add_argument(
            "--session-id", default="", metavar="ID",
            help=t("cli_task_arg_session_id", default="Session ID (P3 Identity)"))
        _identity_parser.add_argument(
            "--model-id", default="", metavar="ID",
            help=t("cli_task_arg_model_id", default="Model ID (P3 Identity)"))
        _identity_parser.add_argument(
            "--role", default="", metavar="ROLE",
            help=t("cli_task_arg_role",
                   default="Role (planner/implementer/reviewer/tester)"))

    # P4：task report/apply/close/reopen 支持受保护写 Lease 凭证（Req 11.8-11.9）。
    # 提供 --lease-token 时启用受保护写路径：过期/token 不匹配/旧 counter 在写入前拒绝。
    for _lease_parser in (report_p, apply_p, close_p, reopen_p):
        _lease_parser.add_argument(
            "--lease-token", default="", metavar="TOKEN",
            help=t("cli_task_arg_lease_token",
                   default="Lease raw token (P4 protected mutation)"))
        _lease_parser.add_argument(
            "--fencing-counter", type=int, default=-1, metavar="N",
            help=t("cli_task_arg_fencing_counter",
                   default="Current fencing counter (P4 protected mutation)"))

    # capture-diff：捕获外部 Agent 真实文件改动到 task/change/symbol/audit 闭环
    capture_p = sub.add_parser(
        "capture-diff",
        help=t("cli_task_capture_diff_desc",
               default="Capture external agent file changes into task/audit closure")
    )
    # task_id 在 --auto 模式下可省略（nargs='?'）
    capture_p.add_argument(
        "task_id", nargs="?", default="",
        help=t("cli_task_arg_task_id", default="Task ID")
    )
    capture_p.add_argument(
        "--step-id", default="",
        help=t("cli_task_arg_step_id_capture",
               default="Associated step ID (optional)")
    )
    capture_p.add_argument(
        "--base", default="",
        help=t("cli_task_arg_base",
               default="Base commit (empty = latest scan baseline)")
    )
    capture_p.add_argument(
        "--dry-run", action="store_true",
        help=t("cli_task_arg_dry_run",
               default="Dry-run mode: only return plan, do not write to DB")
    )
    capture_p.add_argument(
        "--auto", action="store_true",
        help=t("cli_task_arg_auto_capture",
               default="Auto mode: detect in_progress task, use HEAD~1 as base, auto apply (fail-soft)")
    )
    capture_p.add_argument(
        "--skip-quality-review", action="store_true",
        help=t("cli_task_arg_skip_quality_review",
               default="Skip run_task_completion_review (Semgrep + 5 extension checkers). "
                       "Auto mode always skips; manual mode defaults to False.")
    )
    capture_p.add_argument(
        "--source-commit-hash", default="",
        help=t("cli_task_arg_source_commit_hash",
               default="Source commit hash for task↔commit triangulation")
    )

    # findings：查看任务质量门禁发现
    findings_p = sub.add_parser(
        "findings", help=t("cli_task_findings_desc", default="List task quality findings")
    )
    findings_p.add_argument("task_id", help=t(
        "cli_task_arg_task_id", default="Task ID"))
    findings_p.add_argument(
        "--status", default="open",
        help=t("cli_task_arg_status",
               default="Status filter (open/resolved/wontfix/all)")
    )
    findings_p.add_argument(
        "--severity", default="",
        help=t("cli_task_arg_severity",
               default="Severity filter (info/warn/error/block)")
    )

    # resolve-finding：解决或豁免质量门禁发现
    resolve_p = sub.add_parser(
        "resolve-finding",
        help=t("cli_task_resolve_finding_desc",
               default="Resolve a task quality finding")
    )
    resolve_p.add_argument("finding_id", type=int, help=t(
        "cli_task_arg_finding_id", default="Finding ID"))
    resolve_p.add_argument(
        "--resolution", default="fixed",
        help=t("cli_task_arg_resolution",
               default="Resolution (fixed/wontfix/false_positive)")
    )
    resolve_p.add_argument(
        "--by", default="agent",
        help=t("cli_task_arg_by", default="Resolver (agent/human/system)")
    )

    # list：列出任务（支持 --blocked 过滤）
    list_p = sub.add_parser("list", help=t(
        "cli_task_list_desc", default="List tasks"))
    list_p.add_argument(
        "--blocked", action="store_true",
        help=t("cli_task_arg_blocked",
               default="Only show tasks with blocking findings")
    )
    list_p.add_argument(
        "--limit", type=int, default=200,
        help=t("cli_task_arg_limit",
               default="Maximum number of tasks to list (default: 200)")
    )
    list_p.add_argument(
        "--status", default="",
        help=t("cli_task_arg_status_filter",
               default="Status filter (open/in_progress/review/applied/closed/reverted)")
    )
    list_p.add_argument(
        "--flat", action="store_true",
        help=t("cli_task_arg_flat", default="Flat list (no tree indentation)")
    )

    # show：查看任务详情（默认树形展示子任务）
    show_p = sub.add_parser("show", help=t(
        "cli_task_show_desc", default="Show task details (tree mode by default)"))
    show_p.add_argument("task_id", help=t(
        "cli_task_arg_task_id", default="Task ID"))
    show_p.add_argument(
        "--flat", action="store_true",
        help=t("cli_task_arg_flat_show",
               default="Flat mode (do not show subtasks recursively)")
    )

    # completion-review：运行任务完成质量审查（C9 新增）
    cr_p = sub.add_parser(
        "completion-review",
        help=t("cli_task_completion_review_desc",
               default="Run task completion quality review"),
    )
    cr_p.add_argument("task_id", help=t(
        "cli_task_arg_task_id", default="Task ID"))
    cr_p.add_argument(
        "--step-id", default="",
        help=t("cli_task_arg_step_id",
               default="Step ID (optional, task-level review if empty)"),
    )

    # split：从 Markdown 计划拆分父子任务树（C9 新增）
    split_p = sub.add_parser(
        "split",
        help=t("cli_task_split_desc",
               default="Split task into subtasks from Markdown plan"),
    )
    split_p.add_argument("task_id", help=t(
        "cli_task_arg_task_id", default="Task ID"))
    split_p.add_argument(
        "--plan", required=True,
        help=t("cli_task_arg_plan_file", default="Markdown plan file path"),
    )

    # status-tree：以树形显示任务状态（C9 新增，task show --tree 的别名）
    st_p = sub.add_parser(
        "status-tree",
        help=t("cli_task_status_tree_desc", default="Show task status tree"),
    )
    st_p.add_argument("task_id", help=t(
        "cli_task_arg_task_id", default="Task ID"))

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

        # C1: 孤儿任务 soft warning（CLI 层显式提示，db 层也会输出到 stderr）
        if steps:
            step_count = len(steps)
            files_set = set()
            for s in steps:
                tf = s.get("target_file", "")
                if tf:
                    for f in str(tf).split("+"):
                        f = f.strip()
                        if f:
                            files_set.add(f)
            file_count = len(files_set)
            if step_count > 5 or file_count > 3:
                cprint(
                    t(
                        "cli.messages.task_orphan_warning",
                        title=opts.title,
                        step_count=step_count,
                        file_count=file_count,
                    ),
                    "yellow",
                )

        def _local_create():
            return db.task_create(opts.title, opts.desc, steps, creator="agent")

        create_res = route_task_write("task.create", {
            "title": opts.title, "description": opts.desc, "steps": steps, "creator": "agent",
        }, _local_create)
        task_id = create_res["task_id"] if isinstance(create_res, dict) and "task_id" in create_res else create_res

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
                print(t("cli.messages.task_step_item",
                      idx=i, action=action, target=target))
        print()
        return True

    elif opts.action == "next":
        def _local_next():
            return db.task_next_step(opts.task_id)

        result = route_task_write("task.claim", {"task_id": opts.task_id}, _local_next)

        cprint(t("cli.messages.task_next_title"), "cyan", bold=True)
        print(t("cli.messages.task_id_label", id=opts.task_id))
        if result is None:
            cprint(t("cli.messages.task_no_pending"), "yellow")
            return True

        print(t("cli.messages.task_step_id", id=result.get('step_id', '')))
        print(t("cli.messages.task_step_index", idx=result.get('step_index', 0)))
        print(t("cli.messages.task_action", action=result.get('action', '')))
        if result.get("target_file"):
            print(t("cli.messages.task_target_file",
                  file=result['target_file']))
        if result.get("target_symbol"):
            print(t("cli.messages.task_target_symbol",
                  symbol=result['target_symbol']))
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
            print(t("cli.messages.task_guardrail_decision",
                  decision=alert.get('decision', '')))
            print(t("cli.messages.task_guardrail_message",
                  msg=alert.get('message', '')))
            findings = alert.get("findings", [])
            if findings:
                print(t("cli.messages.task_guardrail_findings_count",
                      count=len(findings)))
            cprint(t("cli.messages.task_guardrail_resolve_hint"), "yellow")
            print()

        # 护栏警告（warn）
        warning = result.get("guardrail_warning")
        if warning:
            cprint(t("cli.messages.task_guardrail_warning_title"), "yellow")
            print(t("cli.messages.task_guardrail_message",
                  msg=warning.get('message', '')))
            print()

        # F7: 结构化指令展示（Agent 必须遵循的操作约束）
        si = result.get("structured_instruction")
        if si:
            cprint(t("cli.messages.task_structured_instruction_title"),
                   "cyan", bold=True)
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
                print(t("cli.messages.task_si_checks",
                      checks=', '.join(si['checks'])))
            ctx = si.get("context", {})
            if ctx.get("callers"):
                callers_str = ", ".join(c.get("name", "")
                                        for c in ctx["callers"][:3])
                print(t("cli.messages.task_si_callers", callers=callers_str))
            if ctx.get("existing_summary"):
                print(t("cli.messages.task_si_existing_summary",
                        summary=ctx['existing_summary'][:60]))
            print()

        return True

    elif opts.action == "report":
        success = not opts.fail
        # P3：收集并校验结构化身份（可选；提供后必须完整，否则 fail closed）
        identity, ireason = _collect_identity(opts)
        if ireason:
            _identity_reason_output(ireason, False)
            return True
        if identity:
            ok, vreason = _validate_identity(db, identity)
            if not ok:
                _identity_reason_output(vreason, False)
                return True
            if not _method_accepts_identity(db, "task_report_step"):
                _identity_reason_output({
                    "code": "E_IDENTITY_NOT_WIRED",
                    "message_key": "daemon_errors.error.identity_not_wired",
                    "detail": "task_report_step 尚不支持 identity 参数（8.6 接线后可用）",
                }, False)
                return True
        # P4：受保护写 Lease 凭证（可选；凭证不完整时 fail closed）
        lease_kwargs = _collect_lease_creds(opts)
        if "error" in lease_kwargs:
            _lease_reason_output(lease_kwargs["error"], False)
            return True
        def _local_report():
            return db.task_report_step(
                opts.task_id, opts.step_id, opts.result, success, None,
                **( {"identity": identity} if identity else {} ),
                **lease_kwargs,
            )
        result = route_task_write("task.report", {
            "task_id": opts.task_id, "step_id": opts.step_id, "summary": opts.result, "success": success,
        }, _local_report)

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
            print(t("cli.messages.task_next_action",
                  action=result.get('action', '')))
            if result.get("target_file"):
                print(t("cli.messages.task_next_target_file",
                      file=result['target_file']))
        print()
        return True

    elif opts.action == "rollback":
        def _local_rollback():
            if hasattr(db, "task_rollback_step"):
                return db.task_rollback_step(opts.task_id, opts.step_id)
            return db.task_rollback(opts.task_id, opts.step_id)

        result = route_task_write("task.rollback", {
            "task_id": opts.task_id, "step_id": opts.step_id,
        }, _local_rollback)

        cprint(t("cli.messages.task_rollback_title"), "cyan", bold=True)
        print(t("cli.messages.task_id_label", id=opts.task_id))
        print(t("cli.messages.task_rollback_status",
              status=result.get('task_status', '')))
        rolled = result.get("rolled_back_changes", [])
        print(t("cli.messages.task_rollback_count", count=len(rolled)))
        print()

        if rolled:
            print(t("cli.messages.task_rollback_details_title"))
            for i, c in enumerate(rolled, 1):
                fp = c.get("file_path", "")
                restorable = c.get("restorable", False)
                icon = "[✓]" if restorable else "[✗]"
                print(t("cli.messages.task_rollback_item",
                      idx=i, icon=icon, path=fp))
                if c.get("hash_before"):
                    print(t("cli.messages.task_rollback_hash",
                          hash=c['hash_before'][:12]))

        note = result.get("note", "")
        if note:
            print()
            cprint(t("cli.messages.task_note", note=note), "yellow")
        print()
        return True

    elif opts.action == "apply":
        # 审核通过：review -> applied（由其他会话的 LLM 调用）
        identity, ireason = _collect_identity(opts)
        if ireason:
            _identity_reason_output(ireason, False)
            return True
        apply_kwargs = {"reviewer": opts.reviewer}
        if identity:
            ok, vreason = _validate_identity(db, identity)
            if not ok:
                _identity_reason_output(vreason, False)
                return True
            if not _method_accepts_identity(db, "task_apply"):
                _identity_reason_output({
                    "code": "E_IDENTITY_NOT_WIRED",
                    "message_key": "daemon_errors.error.identity_not_wired",
                    "detail": "task_apply 尚不支持 identity 参数（8.6 接线后可用）",
                }, False)
                return True
            apply_kwargs["identity"] = identity
        else:
            # --reviewer 自由文本不是身份证明（Req 10.5）；P3 门禁在 db 层 fail closed
            cprint(t("cli.messages.identity_reviewer_free_text_warning"), "yellow")
        # P4：受保护写 Lease 凭证（可选；凭证不完整时 fail closed）
        lease_kwargs = _collect_lease_creds(opts)
        if "error" in lease_kwargs:
            _lease_reason_output(lease_kwargs["error"], False)
            return True
        apply_kwargs.update(lease_kwargs)
        
        def _local_apply():
            return db.task_apply(opts.task_id, **apply_kwargs)

        result = route_task_write("task.apply", {
            "task_id": opts.task_id, "reviewer": opts.reviewer,
        }, _local_apply)

        if "error" in result:
            cprint(t("cli.messages.task_apply_failed",
                   error=result["error"]), "red")
            print()
            return True
        cprint(t("cli.messages.task_apply_success",
               id=result["task_id"]), "green", bold=True)
        print(t("cli.messages.task_status_label", status=result["status"]))
        if result.get("applied_at"):
            print(t("cli.messages.task_applied_at", ts=result["applied_at"]))
        if identity:
            cprint(t("cli.messages.identity_recorded"), "green")
        print()
        return True

    elif opts.action == "close":
        # 关闭任务：applied -> closed（由其他会话的 LLM 调用）
        identity, ireason = _collect_identity(opts)
        if ireason:
            _identity_reason_output(ireason, False)
            return True
        close_kwargs = {"reviewer": opts.reviewer}
        if identity:
            ok, vreason = _validate_identity(db, identity)
            if not ok:
                _identity_reason_output(vreason, False)
                return True
            if not _method_accepts_identity(db, "task_close"):
                _identity_reason_output({
                    "code": "E_IDENTITY_NOT_WIRED",
                    "message_key": "daemon_errors.error.identity_not_wired",
                    "detail": "task_close 尚不支持 identity 参数（8.6 接线后可用）",
                }, False)
                return True
            close_kwargs["identity"] = identity
        else:
            cprint(t("cli.messages.identity_reviewer_free_text_warning"), "yellow")
        # P4：受保护写 Lease 凭证（可选；凭证不完整时 fail closed）
        lease_kwargs = _collect_lease_creds(opts)
        if "error" in lease_kwargs:
            _lease_reason_output(lease_kwargs["error"], False)
            return True
        close_kwargs.update(lease_kwargs)

        def _local_close():
            return db.task_close(opts.task_id, **close_kwargs)

        result = route_task_write("task.close", {
            "task_id": opts.task_id, "reviewer": opts.reviewer,
        }, _local_close)

        if "error" in result:
            cprint(t("cli.messages.task_close_failed",
                   error=result["error"]), "red")
            print()
            return True
        cprint(t("cli.messages.task_close_success",
               id=result["task_id"]), "green", bold=True)
        print(t("cli.messages.task_status_label", status=result["status"]))
        if result.get("closed_at"):
            print(t("cli.messages.task_closed_at", ts=result["closed_at"]))
        if identity:
            cprint(t("cli.messages.identity_recorded"), "green")
        print()
        return True

    elif opts.action == "reopen":
        # 重新打开任务：review/applied/closed -> in_progress
        identity, ireason = _collect_identity(opts)
        if ireason:
            _identity_reason_output(ireason, False)
            return True
        reopen_kwargs = {"reviewer": opts.reviewer, "reason": opts.reason}
        if identity:
            ok, vreason = _validate_identity(db, identity)
            if not ok:
                _identity_reason_output(vreason, False)
                return True
            if not _method_accepts_identity(db, "task_reopen"):
                _identity_reason_output({
                    "code": "E_IDENTITY_NOT_WIRED",
                    "message_key": "daemon_errors.error.identity_not_wired",
                    "detail": "task_reopen 尚不支持 identity 参数（8.6 接线后可用）",
                }, False)
                return True
            reopen_kwargs["identity"] = identity
        else:
            cprint(t("cli.messages.identity_reviewer_free_text_warning"), "yellow")
        # P4：受保护写 Lease 凭证（可选；凭证不完整时 fail closed）
        lease_kwargs = _collect_lease_creds(opts)
        if "error" in lease_kwargs:
            _lease_reason_output(lease_kwargs["error"], False)
            return True
        reopen_kwargs.update(lease_kwargs)

        def _local_reopen():
            return db.task_reopen(opts.task_id, **reopen_kwargs)

        result = route_task_write("task.reopen", {
            "task_id": opts.task_id, "reviewer": opts.reviewer, "reason": opts.reason,
        }, _local_reopen)
        if "error" in result:
            cprint(t("cli.messages.task_reopen_failed",
                   error=result["error"]), "red")
            print()
            return True
        cprint(
            t("cli.messages.task_reopen_success",
              id=result["task_id"],
              previous=result["previous_status"]),
            "green", bold=True,
        )
        print(t("cli.messages.task_status_label", status=result["status"]))
        if result.get("reopened_at"):
            print(t("cli.messages.task_reopened_at", ts=result["reopened_at"]))
        if opts.reason:
            print(t("cli.messages.task_reopen_reason_label", reason=opts.reason))
        if identity:
            cprint(t("cli.messages.identity_recorded"), "green")
        print()
        return True

    elif opts.action == "capture-diff":
        # 捕获外部 Agent 真实文件改动到 task/change/symbol/audit 闭环
        # --auto 模式：自动检测 in_progress 任务 + HEAD~1 base + 自动 apply（fail-soft）
        if opts.auto:
            # 自动模式：调用 route_task_write("task.capture_diff")，fail-soft
            try:
                def _local_capture_auto():
                    return db.task_capture_diff_auto()
                result = route_task_write("task.capture_diff", {"auto": True}, _local_capture_auto)
            except Exception as exc:
                # 兜底：db 层未捕获的异常也封装为 fail-soft 结果
                result = {
                    "auto": True,
                    "success": False,
                    "reason": "cli_exception",
                    "error": str(exc),
                    "task_id": "",
                    "base": "",
                    "dry_run": False,
                    "changed_files": [],
                    "linked_symbols": [],
                    "quality_findings": [],
                    "quality_decision": "",
                    "next_action": "noop",
                }

            cprint(t("cli.messages.task_capture_diff_title"), "cyan", bold=True)
            cprint(t("cli.messages.task_capture_diff_auto_mode"),
                   "yellow", bold=True)
            print()

            if not result.get("success"):
                # fail-soft：失败不阻断，仅提示
                reason = result.get("reason", "")
                error = result.get("error", "")
                if reason == "no_in_progress_task":
                    cprint(t("cli.messages.task_capture_diff_auto_no_task"), "yellow")
                elif reason == "task_not_in_progress":
                    # active_task 已完成（review/applied/closed），跳过自动捕获
                    task_id = result.get("task_id", "")
                    cprint(t("cli.messages.task_capture_diff_auto_task_not_in_progress",
                             default="[Call Warden] Active task {task_id} is not in_progress (review/applied/closed). Skipping auto-capture. Run 'cw task completion-review {task_id}' explicitly to run quality review.",
                             task_id=task_id), "yellow")
                elif reason == "exception":
                    cprint(t("cli.messages.task_capture_diff_auto_exception",
                             error=error), "red")
                else:
                    cprint(t("cli.messages.task_capture_diff_auto_failed",
                             reason=reason, error=error), "red")
                print()
                # fail-soft：返回 True，不阻断 git commit
                return True

            # 成功：展示 task_id / base / 变更摘要
            task_id = result.get("task_id", "")
            base = result.get("base", "")
            print(t("cli.messages.task_id_label", id=task_id))
            if base:
                print(t("cli.messages.task_capture_diff_base_commit", base=base))
            print(t("cli.messages.task_capture_diff_mode",
                    mode=t("cli.messages.task_capture_diff_mode_apply")))
            print()

            changed = result.get("changed_files", [])
            print(t("cli.messages.task_capture_diff_changed_count", count=len(changed)))
            if changed:
                print()
                for c in changed:
                    print(t("cli.messages.task_capture_diff_changed_item",
                            path=c.get("path", ""), status=c.get("status", "M")))
                print()

            print(t("cli.messages.task_capture_diff_scan_id",
                    scan_id=result.get("scan_id", 0)))
            linked = result.get("linked_symbols", [])
            print(t("cli.messages.task_capture_diff_linked_count", count=len(linked)))

            decision = result.get("quality_decision", "")
            findings = result.get("quality_findings", [])
            decision_color = {"pass": "green", "warn": "yellow",
                              "block": "red"}.get(decision, "white")
            if decision:
                cprint(t("cli.messages.task_capture_diff_quality_decision",
                         decision=decision, count=len(findings)), decision_color)
                for f in findings:
                    sev = f.get("severity", "info")
                    color = {"error": "red", "block": "red",
                             "warn": "yellow", "info": "cyan"}.get(sev, "white")
                    cprint(t("cli.messages.task_capture_diff_finding_item",
                             sev=sev, ftype=f.get("finding_type", ""),
                             msg=f.get("message", "")), color)
            print()

            next_action = result.get("next_action", "")
            next_color = {"review": "green", "fix": "red",
                          "noop": "yellow"}.get(next_action, "white")
            cprint(t("cli.messages.task_capture_diff_next_action",
                     action=next_action), next_color, bold=True)
            print()
            return True

        # 手动模式：必须指定 task_id
        if not opts.task_id:
            capture_p.error(t("cli.messages.task_capture_diff_missing_task_id",
                              default="task_id is required (or use --auto)"))

        def _local_capture_manual():
            return db.task_capture_diff(
                task_id=opts.task_id,
                step_id=opts.step_id,
                base=opts.base,
                dry_run=opts.dry_run,
                source_commit_hash=getattr(opts, "source_commit_hash", "") or "",
                skip_quality_review=getattr(opts, "skip_quality_review", False),
            )

        result = route_task_write("task.capture_diff", {
            "task_id": opts.task_id,
            "step_id": opts.step_id,
            "base": opts.base,
            "dry_run": opts.dry_run,
            "source_commit_hash": getattr(opts, "source_commit_hash", "") or "",
            "skip_quality_review": getattr(opts, "skip_quality_review", False),
        }, _local_capture_manual)

        cprint(t("cli.messages.task_capture_diff_title"), "cyan", bold=True)
        print(t("cli.messages.task_id_label", id=opts.task_id))
        if opts.step_id:
            print(t("cli.messages.task_capture_diff_step_id", step=opts.step_id))
        if opts.base:
            print(t("cli.messages.task_capture_diff_base_commit", base=opts.base))
        print(t("cli.messages.task_capture_diff_mode",
                mode=t("cli.messages.task_capture_diff_mode_dry_run") if opts.dry_run
                else t("cli.messages.task_capture_diff_mode_apply")))
        print()

        changed = result.get("changed_files", [])
        print(t("cli.messages.task_capture_diff_changed_count", count=len(changed)))
        if changed:
            print()
            for c in changed:
                print(t("cli.messages.task_capture_diff_changed_item",
                        path=c.get("path", ""), status=c.get("status", "M")))
            print()

        # dry-run 模式只显示计划，不显示后续字段
        if result.get("dry_run"):
            cprint(t("cli.messages.task_capture_diff_dry_run_hint",
                     next_action=result.get("next_action", "")), "yellow")
            print()
            return True

        # apply 模式：显示 scan_id / linked_symbols / quality_findings / next_action
        print(t("cli.messages.task_capture_diff_scan_id",
              scan_id=result.get("scan_id", 0)))
        linked = result.get("linked_symbols", [])
        print(t("cli.messages.task_capture_diff_linked_count", count=len(linked)))

        # 质量审查结果
        decision = result.get("quality_decision", "")
        findings = result.get("quality_findings", [])
        decision_color = {"pass": "green", "warn": "yellow",
                          "block": "red"}.get(decision, "white")
        if decision:
            cprint(t("cli.messages.task_capture_diff_quality_decision",
                     decision=decision, count=len(findings)), decision_color)
            for f in findings:
                sev = f.get("severity", "info")
                color = {"error": "red", "block": "red",
                         "warn": "yellow", "info": "cyan"}.get(sev, "white")
                cprint(t("cli.messages.task_capture_diff_finding_item",
                         sev=sev, ftype=f.get("finding_type", ""),
                         msg=f.get("message", "")), color)
        print()

        # next_action 提示
        next_action = result.get("next_action", "")
        next_color = {"review": "green", "fix": "red",
                      "noop": "yellow"}.get(next_action, "white")
        cprint(t("cli.messages.task_capture_diff_next_action",
                 action=next_action), next_color, bold=True)
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
            color = {"error": "red", "block": "red",
                     "warn": "yellow", "info": "cyan"}.get(sev, "white")
            status = f.get("status", "open")
            icon = "[!]" if sev in ("error", "block") else (
                "[~]" if sev == "warn" else "[i]")
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
            cprint(t("cli.messages.task_panel_status_filter",
                   status=opts.status), "yellow")
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
            blocking = db.task_has_blocking_findings(tid) if hasattr(
                db, "task_has_blocking_findings") else False
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
            indent = "" if opts.flat else (
                t("cli.messages.task_list_indent") * depth)
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
                blocking = db.task_has_blocking_findings(tid) if hasattr(
                    db, "task_has_blocking_findings") else False
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

    elif opts.action == "completion-review":
        # 运行任务完成质量审查（C9 新增）
        if not hasattr(db, "run_task_completion_review"):
            cprint(t("cli.messages.task_completion_review_unavailable",
                     default="Task completion review not available"), "red")
            return True
        result = db.run_task_completion_review(
            opts.task_id, step_id=opts.step_id)
        if "error" in result:
            cprint(t("cli.messages.task_completion_review_failed",
                     error=result["error"]), "red")
            print()
            return True
        decision = result.get("decision", "unknown")
        counts = result.get("counts", {})
        decision_color = {"pass": "green", "warn": "yellow",
                          "block": "red"}.get(decision, "white")
        cprint(t("cli.messages.task_completion_review_result",
                 decision=decision), decision_color, bold=True)
        print(t("cli.messages.task_completion_review_task", task_id=opts.task_id))
        if opts.step_id:
            print(t("cli.messages.task_completion_review_step", step_id=opts.step_id))
        summary = result.get("summary", "")
        if summary:
            print(t("cli.messages.task_completion_review_summary", summary=summary))
        if counts:
            print(t("cli.messages.task_completion_review_counts",
                    total=counts.get("total", 0),
                    info=counts.get("info", 0),
                    warn=counts.get("warn", 0),
                    error=counts.get("error", 0),
                    block=counts.get("block", 0)))
        findings = result.get("findings", [])
        if findings:
            print()
            print(t("cli.messages.task_completion_review_findings_title",
                  count=len(findings)))
            for i, f in enumerate(findings, 1):
                sev = f.get("severity", "?")
                msg = f.get("message", "")
                print(t("cli.messages.task_completion_review_finding_item",
                        idx=i, severity=sev, message=msg))
        print()
        return True

    elif opts.action == "split":
        # 从 Markdown 计划拆分父子任务树（C9 新增）
        plan_path = opts.plan
        if not os.path.exists(plan_path):
            cprint(t("cli.messages.task_split_plan_not_found",
                     path=plan_path), "red")
            print()
            return True
        with open(plan_path, encoding="utf-8") as f:
            plan_md = f.read()
        # 验证任务存在
        cur = db.conn.execute(
            "SELECT title FROM tasks WHERE id = ?", (opts.task_id,))
        task_row = cur.fetchone()
        if not task_row:
            cprint(t("cli.messages.task_not_found",
                   default="Task not found"), "red")
            print()
            return True
        # 解析 Markdown 计划为子任务定义
        subtasks = _parse_plan_to_subtasks(plan_md)
        if not subtasks:
            cprint(t("cli.messages.task_split_no_subtasks",
                     default="No subtasks found in plan"), "yellow")
            print()
            return True
        def _local_split():
            return db.task_split(opts.task_id, subtasks)

        sub_ids = route_task_write("task.split", {
            "task_id": opts.task_id,
            "subtasks": subtasks,
            "plan_file": str(opts.plan),
        }, _local_split)
        cprint(t("cli.messages.task_split_success",
                 task_id=opts.task_id, count=len(sub_ids)), "green", bold=True)
        for i, sid in enumerate(sub_ids):
            title = subtasks[i].get("title", "")
            print(t("cli.messages.task_split_subtask_item",
                    idx=i + 1, id=sid, title=title))
        print()
        return True

    elif opts.action == "status-tree":
        # 以树形显示任务状态（C9 新增，task show --tree 的别名）
        return _print_task_show(db, opts.task_id, flat=False)

    return True


def _parse_plan_to_subtasks(plan_md: str):
    """从 Markdown 计划解析子任务定义列表（C9 新增，供 task split 使用）

    解析格式（与 task_create_from_plan 兼容）：
    - ## 标题 = 子任务标题
    - ## 标题下的描述行 = 子任务描述
    - - / * / + 开头的列表项 = 步骤（action @ target_file 格式）

    Args:
        plan_md: Markdown 格式的计划文本

    Returns:
        子任务定义列表 [{title, description, steps}]
    """
    import re

    re_h2 = re.compile(r'^##\s+(.+?)\s*#*\s*$')
    re_list = re.compile(r'^[-*+]\s+(.+)$')

    lines = plan_md.strip().split("\n")
    subtasks = []
    current_title = None
    current_desc_lines = []
    current_steps = []
    in_code_block = False

    for line in lines:
        stripped = line.strip()

        # 代码块围栏检测
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        # 二级标题 = 新子任务
        m_h2 = re_h2.match(stripped)
        if m_h2:
            # 保存前一个子任务
            if current_title:
                subtasks.append({
                    "title": current_title,
                    "description": "\n".join(current_desc_lines).strip(),
                    "steps": current_steps,
                })
            current_title = m_h2.group(1)
            current_desc_lines = []
            current_steps = []
            continue

        # 列表项 = 步骤
        m_list = re_list.match(stripped)
        if m_list and current_title:
            content = m_list.group(1)
            # 解析 "action @ target_file" 或 "action: target_file" 格式
            action = "edit"
            target_file = ""
            if "@" in content:
                parts = content.split("@", 1)
                action = parts[0].strip()
                target_file = parts[1].strip()
            elif ":" in content:
                parts = content.split(":", 1)
                action = parts[0].strip()
                target_file = parts[1].strip()
            else:
                action = content.strip()
            current_steps.append({
                "action": action,
                "target_file": target_file,
                "check_items": "",
            })
            continue

        # 普通行 = 描述
        if stripped and current_title:
            # H2 已在上方处理，此处跳过其余以 # 开头的标题行（H1/H3/H4...）
            # 避免 #标题 / ###标题 等被当作描述行
            if stripped.startswith("#"):
                continue
            current_desc_lines.append(stripped)

    # 保存最后一个子任务
    if current_title:
        subtasks.append({
            "title": current_title,
            "description": "\n".join(current_desc_lines).strip(),
            "steps": current_steps,
        })

    return subtasks


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
        _print_task_link_section(db, task_id)
        return True

    # 树形模式：使用 task_status_tree 递归展示
    tree = db.task_status_tree(task_id) if hasattr(
        db, "task_status_tree") else None
    if not tree:
        print(t("cli.messages.task_show_not_found", id=task_id))
        return True

    cprint(t("cli.messages.task_show_title"), "cyan", bold=True)
    print("-" * 50)
    _print_task_tree_node(tree, depth=0)
    print()
    _print_task_link_section(db, task_id)
    return True


def _print_task_link_section(db, task_id: str):
    """打印任务的三角关联段（commits + symbol_changes）

    在 _print_task_show 末尾调用，展示 task → commit / task → symbol 关联。
    fail-soft：方法不存在或查询失败时静默跳过。
    """
    try:
        commits = db.get_task_commits(task_id) if hasattr(
            db, "get_task_commits") else []
    except Exception:
        commits = []
    try:
        changes = db.get_task_symbol_changes(task_id, limit=20) if hasattr(
            db, "get_task_symbol_changes") else []
    except Exception:
        changes = []

    if not commits and not changes:
        return

    cprint(t("cli.messages.task_show_link_title", default="── Related ──"), "cyan")
    if commits:
        print(t("cli.messages.task_show_commits_count",
              default="Commits ({}):".format(len(commits)), count=len(commits)))
        for c in commits:
            short = (c.get("source_commit_hash") or "")[:8]
            subject = c.get("commit_subject") or ""
            author = c.get("commit_author") or ""
            cnt = c.get("change_count", 0)
            print("  {} {} [{} change{}]".format(
                short, subject, cnt, "s" if cnt != 1 else ""))
            if author:
                print("       by {}".format(author))
    if changes:
        print(t("cli.messages.task_show_changes_count",
              default="Symbol changes ({}):".format(len(changes)), count=len(changes)))
        for ch in changes[:10]:
            qn = ch.get("qualified_name") or ch.get("symbol_name") or ""
            ct = ch.get("change_type", "")
            sch = (ch.get("source_commit_hash") or "")[:8]
            tag = " [commit:{}]".format(sch) if sch else ""
            print("  {} {}{}".format(qn, ct, tag))
        if len(changes) > 10:
            print("  ... and {} more".format(len(changes) - 10))


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
    created = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(
        detail['created_at'])) if detail.get('created_at') else '?'
    print(t("cli.messages.task_show_created", time=created))
    print()
    steps = detail.get('steps', [])
    print(t("cli.messages.task_show_steps", count=len(steps)))
    for s in steps:
        print(t("cli.messages.task_show_step",
              idx=s['step_index'], status=s['status'], action=s['action']))
        if s.get('target_file'):
            print(t("cli.messages.task_show_step_file", file=s['target_file']))
        if s.get('target_symbol'):
            print(t("cli.messages.task_show_step_symbol",
                  symbol=s['target_symbol']))


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
        created = time.strftime(
            '%Y-%m-%d %H:%M:%S', time.localtime(node['created_at'])) if node.get('created_at') else '?'
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
            print(t("cli.messages.task_show_step",
                  idx=s['step_index'], status=s['status'], action=s['action']))
            if s.get('target_file'):
                print(t("cli.messages.task_show_step_file",
                      file=s['target_file']))
            if s.get('target_symbol'):
                print(t("cli.messages.task_show_step_symbol",
                      symbol=s['target_symbol']))

    # 递归子任务
    subtasks = node.get('subtasks', []) or []
    if subtasks:
        if depth == 0:
            print()
            cprint(t("cli.messages.task_show_subtasks_title",
                   count=len(subtasks)), "cyan", bold=True)
        for st in subtasks:
            _print_task_tree_node(st, depth + 1)
    elif depth > 0:
        # 叶子节点不显示
        pass


def _handle_audit(args, db):
    """处理 audit 子命令（审计签名链验证 + 密钥轮换）

    支持的 action：
    - verify：调用 verify_audit_chain 校验 audit_chain 表的连续性与签名匹配，
      输出 total/verified/broken/security_level/broken_records。
    - rotate-key：轮换审计签名密钥（C7）。新记录用新 key 签名，
      旧记录保持原签名不变（signing_key_id 不变），验证时按 key_id 查找对应密钥。
    - keys：列出所有签名密钥轮换记录（不返回 key_secret，避免泄露）。
    """
    parser = argparse.ArgumentParser(
        prog="cw audit",
        description=t(
            "cli_audit_desc", default="Audit chain verification and signing key rotation"),
        epilog=_get_subcommand_epilog("audit"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # verify：验证审计签名链
    verify_p = sub.add_parser(
        "verify",
        help=t("cli_audit_verify_desc",
               default="Verify audit chain continuity and signatures"),
    )
    verify_p.add_argument(
        "--table", default="",
        help=t("cli_audit_verify_arg_table",
               default="Filter by table name (empty = all tables)"),
    )
    verify_p.add_argument(
        "--limit", type=int, default=1000,
        help=t("cli_audit_verify_arg_limit",
               default="Maximum records to verify (default 1000)"),
    )

    # rotate-key：轮换审计签名密钥（C7）
    rotate_p = sub.add_parser(
        "rotate-key",
        help=t("cli_audit_rotate_key_desc",
               default="Rotate audit signing key (new records use new key, old records keep original signature)"),
    )
    rotate_p.add_argument(
        "--key-id", required=True,
        help=t("cli_audit_rotate_key_arg_key_id",
               default="New key identifier (unique, e.g. 'key-2026-07')"),
    )
    rotate_p.add_argument(
        "--secret", default=None,
        help=t("cli_audit_rotate_key_arg_secret",
               default="New key secret (omit to auto-generate a random secret)"),
    )

    # keys：列出签名密钥轮换记录
    keys_p = sub.add_parser(
        "keys",
        help=t("cli_audit_keys_desc",
               default="List all signing key rotation records (key_secret not shown)"),
    )

    opts = parser.parse_args(args)

    if opts.action == "verify":
        # 调用 verify_audit_chain 进行验证
        result = db.verify_audit_chain(table_name=opts.table, limit=opts.limit)

        # 标题（pre-existing key 自带 \n=== ... === 包装）
        cprint(t("cli.messages.audit_verify_title").strip(), "cyan", bold=True)

        # 验证范围：--table 指定时显示表名，否则显示全部表
        scope = opts.table if opts.table else t(
            "cli.messages.audit_verify_all_tables",
            default="all tables",
        )
        print(t("cli.messages.audit_verify_table", scope=scope))
        print(t("cli.messages.audit_verify_limit", limit=opts.limit))
        print()

        # 汇总统计
        total = result.get("total_count", 0)
        verified = result.get("verified_count", 0)
        broken = result.get("broken_count", 0)
        security_level = result.get("security_level", "hash_only")

        print(t("cli.messages.audit_verify_security_level", level=security_level))
        print(t("cli.messages.audit_verify_summary",
                total=total, verified=verified, broken=broken))
        print()

        # 损坏记录详情
        broken_records = result.get("broken_records", [])
        if broken_records:
            for idx, r in enumerate(broken_records, start=1):
                reasons = r.get("reasons", [])
                reasons_str = ", ".join(reasons) if reasons else "unknown"
                print(t("cli.messages.audit_verify_broken_item",
                        idx=idx,
                        id=r.get("id", 0),
                        table=r.get("table_name", ""),
                        rid=r.get("record_id", ""),
                        reasons=reasons_str))
            print()

        # 最终结论
        if total == 0:
            cprint(t("cli.messages.audit_verify_no_records"), "yellow")
        elif broken == 0:
            cprint(t("cli.messages.audit_verify_pass"), "green", bold=True)
        else:
            cprint(t("cli.messages.audit_verify_fail",
                   count=broken), "red", bold=True)
        print()
        return True

    if opts.action == "rotate-key":
        # 处理 audit rotate-key：轮换审计签名密钥（C7）
        key_id = opts.key_id.strip()
        secret = opts.secret

        # 若未提供 --secret，自动生成 32 字节随机密钥（hex 编码）
        if secret is None:
            import secrets as _secrets
            secret = _secrets.token_hex(32)

        try:
            result = db.rotate_signing_key(
                new_key_id=key_id,
                new_key_secret=secret,
            )
        except ValueError as exc:
            cprint(t("cli.messages.audit_rotate_key_invalid_arg",
                     error=str(exc)), "red")
            return True
        except Exception as exc:
            cprint(t("cli.messages.audit_rotate_key_failed",
                     error=str(exc)), "red")
            return True

        cprint(t("cli.messages.audit_rotate_key_title").strip(),
               "green", bold=True)
        print(t("cli.messages.audit_rotate_key_key_id",
                key_id=result.get("key_id", "")))
        ts = result.get("rotated_at", 0.0)
        print(t("cli.messages.audit_rotate_key_rotated_at", ts=ts))
        prev = result.get("previous_key_id", "")
        if prev:
            print(t("cli.messages.audit_rotate_key_previous", prev=prev))
        else:
            print(t("cli.messages.audit_rotate_key_no_previous"))
        # 提示：旧记录保持原签名，验证时按 key_id 查找
        cprint(t("cli.messages.audit_rotate_key_hint").strip(), "cyan")
        print()
        return True

    if opts.action == "keys":
        # 处理 audit keys：列出签名密钥轮换记录
        rows = db.list_signing_keys()
        cprint(t("cli.messages.audit_keys_title").strip(),
               "cyan", bold=True)
        if not rows:
            cprint(t("cli.messages.audit_keys_empty"), "yellow")
            return True
        print(t("cli.messages.audit_keys_count", count=len(rows)))
        print()
        for idx, r in enumerate(rows, start=1):
            active_flag = t("cli.messages.audit_keys_active_yes") if r.get(
                "is_active") else t("cli.messages.audit_keys_active_no")
            ts = r.get("rotated_at", 0.0)
            print(t("cli.messages.audit_keys_item",
                    idx=idx,
                    key_id=r.get("key_id", ""),
                    ts=ts,
                    active=active_flag))
        print()
        # 提示当前活跃密钥
        active_rows = [r for r in rows if r.get("is_active")]
        if active_rows:
            print(t("cli.messages.audit_keys_current_active",
                    key_id=active_rows[0].get("key_id", "")))
        return True

    return False


def _handle_bootstrap(args, db):
    """处理 bootstrap 子命令（自举健康摘要）

    目前仅支持 status action：调用 bootstrap_status 汇总自举闭环健康度，
    输出 db_stale / active_rules / pending_candidates / open_findings /
    blocking_findings / audit_verify / latest_scan_run / tasks / recommended。
    """
    parser = argparse.ArgumentParser(
        prog="cw bootstrap",
        description=t("cli_bootstrap_desc",
                      default="Bootstrap health summary"),
        epilog=_get_subcommand_epilog("bootstrap"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # status：自举健康摘要
    status_p = sub.add_parser(
        "status",
        help=t("cli_bootstrap_status_desc",
               default="Show bootstrap health summary"),
    )

    opts = parser.parse_args(args)

    if opts.action == "status":
        # 调用 bootstrap_status 获取健康摘要
        result = db.bootstrap_status()

        cprint(t("cli.messages.bootstrap_status_title"), "cyan", bold=True)
        print()

        # 1. DB stale 状态
        db_stale = result.get("db_stale", False)
        current_head = result.get("current_head", "")
        if db_stale:
            cprint(t("cli.messages.bootstrap_status_db_stale_yes"),
                   "red", bold=True)
        else:
            cprint(t("cli.messages.bootstrap_status_db_stale_no"), "green")
        if current_head:
            print(t("cli.messages.bootstrap_status_current_head",
                  head=current_head[:12]))
        print()

        # 2. 规则与候选
        active_count = result.get("active_rules_count", 0)
        pending_count = result.get("pending_candidates_count", 0)
        print(t("cli.messages.bootstrap_status_active_rules", count=active_count))
        print(t("cli.messages.bootstrap_status_pending_candidates", count=pending_count))
        print()

        # 3. 质量发现
        open_count = result.get("open_findings_count", 0)
        blocking_count = result.get("blocking_findings_count", 0)
        print(t("cli.messages.bootstrap_status_open_findings", count=open_count))
        if blocking_count > 0:
            cprint(t("cli.messages.bootstrap_status_blocking_findings",
                     count=blocking_count), "red", bold=True)
        else:
            print(t("cli.messages.bootstrap_status_blocking_findings", count=0))
        print()

        # 4. 审计链验证
        audit = result.get("audit_verify", {})
        audit_total = audit.get("total_count", 0)
        audit_broken = audit.get("broken_count", 0)
        audit_level = audit.get("security_level", "")
        audit_color = "green" if audit_broken == 0 and audit_total > 0 else (
            "yellow" if audit_total == 0 else "red"
        )
        cprint(t("cli.messages.bootstrap_status_audit_verify",
                 total=audit_total, broken=audit_broken, level=audit_level), audit_color)
        print()

        # 5. 最近扫描基线
        latest = result.get("latest_scan_run")
        if latest:
            print(t("cli.messages.bootstrap_status_latest_scan",
                    scan_id=latest.get("id", 0),
                    head=latest.get("git_head", "")[:12],
                    status=latest.get("status", "")))
        else:
            print(t("cli.messages.bootstrap_status_no_scan"))
        print()

        # 6. 任务按状态分组
        tasks = result.get("tasks", {})
        print(t("cli.messages.bootstrap_status_tasks",
                open=tasks.get("open", 0),
                in_progress=tasks.get("in_progress", 0),
                review=tasks.get("review", 0),
                applied=tasks.get("applied", 0)))
        print()

        # 7. 推荐下一条命令
        recommended = result.get("recommended_next_action", "")
        cprint(t("cli.messages.bootstrap_status_recommended",
                 action=recommended), "yellow", bold=True)
        print()
        return True

    return False


def _handle_clone(args, db):
    """处理 clone 子命令（重复代码检测）

    子命令：
    - cw clone detect [--file-filter <path>] [--min-lines <n>] [--similarity <f>]
      检测 Type-1/2/3 克隆，结果持久化到 clone_pairs 表
    - cw clone list [--type <1|2|3>] [--min-similarity <f>] [--limit <n>]
      列出已检测到的克隆对
    - cw clone stats
      显示克隆检测统计信息
    - cw clone clear
      清空当前 workspace 的所有克隆检测结果
    """
    parser = argparse.ArgumentParser(
        prog="cw clone",
        description=t("cli_clone_desc",
                      default="Duplicate code detection (Type-1/2/3 clones)"),
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # detect：检测克隆
    detect_p = sub.add_parser(
        "detect",
        help=t("cli_clone_detect_desc",
               default="Detect Type-1/2/3 clones and persist to clone_pairs table"),
    )
    detect_p.add_argument(
        "--file-filter", default="",
        help=t("cli_clone_detect_arg_file_filter",
               default="File path prefix filter (e.g. 'src/core/'), empty for all"),
    )
    detect_p.add_argument(
        "--min-lines", type=int, default=5,
        help=t("cli_clone_detect_arg_min_lines",
               default="Minimum symbol line count (default: 5, skip shorter symbols)"),
    )
    detect_p.add_argument(
        "--similarity", type=float, default=0.8,
        help=t("cli_clone_detect_arg_similarity",
               default="Type-3 similarity threshold [0,1] (default: 0.8)"),
    )

    # list：列出克隆对
    list_p = sub.add_parser(
        "list",
        help=t("cli_clone_list_desc", default="List detected clone pairs"),
    )
    list_p.add_argument(
        "--type", type=int, default=0, choices=[0, 1, 2, 3],
        help=t("cli_clone_list_arg_type",
               default="Clone type filter (0=all, 1/2/3=Type-N)"),
    )
    list_p.add_argument(
        "--min-similarity", type=float, default=0.0,
        help=t("cli_clone_list_arg_min_similarity",
               default="Minimum similarity filter (default: 0.0)"),
    )
    list_p.add_argument(
        "--limit", type=int, default=100,
        help=t("cli_clone_list_arg_limit",
               default="Max results (default: 100)"),
    )
    list_p.add_argument(
        "--symbol", default="",
        help="Filter by symbol qualified_name (show only clones involving this symbol)",
    )

    # stats：统计信息
    sub.add_parser(
        "stats",
        help=t("cli_clone_stats_desc", default="Show clone detection statistics"),
    )

    # clear：清空结果
    sub.add_parser(
        "clear",
        help=t("cli_clone_clear_desc",
               default="Clear all clone detection results for current workspace"),
    )

    opts = parser.parse_args(args)

    if opts.action == "detect":
        result = db.detect_clones(
            file_filter=opts.file_filter,
            min_lines=opts.min_lines,
            similarity_threshold=opts.similarity,
        )
        cprint(t("cli.messages.clone_detect_title"), "cyan", bold=True)
        print()
        print(t("cli.messages.clone_detect_total_pairs",
              count=result.get("total_pairs", 0)))
        print(t("cli.messages.clone_detect_type1",
              count=result.get("type1_pairs", 0)))
        print(t("cli.messages.clone_detect_type2",
              count=result.get("type2_pairs", 0)))
        print(t("cli.messages.clone_detect_type3",
              count=result.get("type3_pairs", 0)))
        print()
        print(t("cli.messages.clone_detect_scanned",
              count=result.get("scanned_symbols", 0)))
        print(t("cli.messages.clone_detect_skipped",
              count=result.get("skipped_symbols", 0)))
        print(t("cli.messages.clone_detect_threshold",
                sim=result.get("similarity_threshold", 0.8),
                min_lines=result.get("min_lines", 5)))
        return True

    if opts.action == "list":
        # --symbol 过滤：查符号 ID 后传给 list_clones
        symbol_id = 0
        if opts.symbol:
            sym = db.get_symbol(opts.symbol)
            if not sym:
                print(f"Symbol not found: {opts.symbol}")
                return False
            # 取 symbol id（get_symbol 不返回 id，用 location 查）
            loc = db.get_symbol_location(opts.symbol.split(".")[-1])
            if loc:
                symbol_id = loc.get("id", 0)
            if not symbol_id:
                # 兜底：直接 SQL 查
                cur = db.conn.execute(
                    "SELECT id FROM symbols WHERE qualified_name = ? LIMIT 1",
                    (opts.symbol,),
                )
                row = cur.fetchone()
                if row:
                    symbol_id = row[0]

        clones = db.list_clones(
            clone_type=opts.type,
            min_similarity=opts.min_similarity,
            limit=opts.limit,
            symbol_id=symbol_id,
        )
        cprint(t("cli.messages.clone_list_title",
               count=len(clones)), "cyan", bold=True)
        print()
        if not clones:
            print(t("cli.messages.clone_list_empty"))
            return True
        for c in clones:
            type_label = {1: "Type-1", 2: "Type-2",
                          3: "Type-3"}.get(c["clone_type"], "?")
            print(t("cli.messages.clone_list_item",
                    type=type_label,
                    sim=c["similarity"],
                    a_file=c.get("file_a", ""),
                    a_line=c.get("symbol_a_line", 0),
                    a_name=c.get("symbol_a_name", ""),
                    b_file=c.get("file_b", ""),
                    b_line=c.get("symbol_b_line", 0),
                    b_name=c.get("symbol_b_name", "")))
        return True

    if opts.action == "stats":
        stats = db.get_clone_stats()
        cprint(t("cli.messages.clone_stats_title"), "cyan", bold=True)
        print()
        print(t("cli.messages.clone_stats_total", count=stats.get("total", 0)))
        print(t("cli.messages.clone_stats_type1", count=stats.get("type1", 0)))
        print(t("cli.messages.clone_stats_type2", count=stats.get("type2", 0)))
        print(t("cli.messages.clone_stats_type3", count=stats.get("type3", 0)))
        print()
        print(t("cli.messages.clone_stats_affected_files",
              count=stats.get("affected_files", 0)))
        print(t("cli.messages.clone_stats_affected_symbols",
              count=stats.get("affected_symbols", 0)))
        return True

    if opts.action == "clear":
        deleted = db.clear_clones()
        cprint(t("cli.messages.clone_clear_done", count=deleted), "green")
        return True

    return False


def _handle_vuln_blast(args, db):
    """处理 vuln-blast 子命令（漏洞爆炸半径分析）"""
    parser = argparse.ArgumentParser(
        prog="cw vuln-blast",
        description=t("cli_vuln_blast_desc",
                      default="Vulnerability blast radius analysis"),
        epilog=_get_subcommand_epilog("vuln-blast"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
    print(t("cli.messages.vuln_blast_total_findings",
          count=result.get('total_findings', 0)))
    print(t("cli.messages.vuln_blast_impacted_symbols",
          count=result.get('total_impacted_symbols', 0)))
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
            print(t("cli.messages.vuln_blast_finding_file",
                  file=f['file_path']))
        if f.get("symbol_qualified"):
            print(t("cli.messages.vuln_blast_finding_symbol",
                  symbol=f['symbol_qualified']))
        print(t("cli.messages.vuln_blast_finding_impacted",
              count=f.get('impacted_count', 0)))

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
                qn = h.get("qualified_name", "") if isinstance(
                    h, dict) else str(h)
                print(t("cli.messages.vuln_blast_high_risk_item", idx=i, name=qn))
            if len(high_risk) > 10:
                print(t("cli.messages.vuln_blast_high_risk_more",
                      count=len(high_risk) - 10))
            print()

    return True


def _handle_symbol_history(args, db):
    """处理 symbol-history 子命令（符号 Git 变更历史）"""
    parser = argparse.ArgumentParser(
        prog="cw symbol-history",
        description=t("cli_symbol_history_desc",
                      default="Symbol Git change history"),
        epilog=_get_subcommand_epilog("symbol-history"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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

    # 三角关联段：symbol → task
    try:
        related_tasks = db.get_symbol_change_tasks(
            symbol_hash=opts.symbol_hash, limit=20) if hasattr(db, "get_symbol_change_tasks") else []
    except Exception:
        related_tasks = []
    if related_tasks:
        cprint(t("cli.messages.symbol_history_tasks_title",
               default="── Related Tasks ──"), "cyan")
        print(t("cli.messages.symbol_history_tasks_count", default="Tasks ({}):".format(
            len(related_tasks)), count=len(related_tasks)))
        for rt in related_tasks:
            tid = rt.get("task_id", "")
            ct = rt.get("change_type", "")
            sch = (rt.get("source_commit_hash") or "")[:8]
            qn = rt.get("qualified_name") or ""
            tag = " [commit:{}]".format(sch) if sch else ""
            print("  {} {} {}{}".format(tid, qn, ct, tag))

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
        description=t("cli_check_gate_desc", default="Check gate (F6)"),
        epilog=_get_subcommand_epilog("check-gate"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("task_id", help=t(
        "cli_check_gate_arg_task_id", default="Task ID"))
    parser.add_argument("--resolve", action="store_true",
                        help=t("cli_check_gate_arg_resolve", default="Mark gate findings for this task as resolved (call after agent fix)"))
    parser.add_argument("--step-id", default="",
                        help=t("cli_check_gate_arg_step_id", default="Related step ID (optional)"))

    opts = parser.parse_args(args)

    if opts.resolve:
        result = db.resolve_gate_findings(task_id=opts.task_id)
        cprint(t("cli.messages.check_gate_resolved_title"), "cyan", bold=True)
        print(t("cli.messages.check_gate_task_id", id=opts.task_id))
        print(t("cli.messages.check_gate_resolved_count",
              count=result.get('resolved_count', 0)))
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
        cprint(t("cli.messages.check_gate_findings_title",
               count=len(findings)), "yellow")
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
                print(
                    t("cli.messages.check_gate_finding_msg", msg=f['message']))
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
        description=t("cli_test_impact_desc", default="Test impact selection"),
        epilog=_get_subcommand_epilog("test-impact"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
        description=t(
            "cli_gc_desc", default="Code graph GC (archive/restore/purge ignored files)"),
        epilog=_get_subcommand_epilog("gc"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
    sub.add_parser("status", help=t(
        "cli_gc_status_desc", default="Show GC status"))

    # gc purge
    purge_p = sub.add_parser("purge",
                             help=t("cli_gc_purge_desc", default="Permanently purge files archived for more than N days"))
    purge_p.add_argument("--older-than", type=int, default=30,
                         help=t("cli_gc_purge_arg_older_than", default="Purge files archived more than this many days (default: 30)"))

    # gc policy
    policy_p = sub.add_parser("policy",
                              help=t("cli_gc_policy_desc", default="Show or update GC retention policy"))
    policy_sub = policy_p.add_subparsers(dest="policy_action", required=True)
    policy_sub.add_parser("show", help=t(
        "cli_gc_policy_show_desc", default="Show current GC retention policy"))
    policy_set = policy_sub.add_parser("set", help=t(
        "cli_gc_policy_set_desc", default="Update GC retention policy"))
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

    # gc db-cleanup（扫描 ~/.callwarden/ 下所有数据库，找出孤儿数据库）
    db_cleanup_p = sub.add_parser("db-cleanup",
                                  help=t("cli_gc_db_cleanup_desc",
                                         default="Scan ~/.callwarden/ for orphan databases (test residue / deleted projects)"))
    db_cleanup_p.add_argument("--dry-run", action="store_true", dest="dry_run", default=True,
                              help=t("cli_gc_db_cleanup_arg_dry_run",
                                     default="Preview only (default); do not delete"))
    db_cleanup_p.add_argument("--apply", action="store_false", dest="dry_run",
                              help=t("cli_gc_db_cleanup_arg_apply",
                                     default="Actually delete orphan databases; default is dry run"))
    db_cleanup_p.add_argument("--all-but-current", action="store_true",
                              help=t("cli_gc_db_cleanup_arg_all_but_current",
                                     default="Mark all databases except current workspace as orphan"))

    # db-migrate-single：旧版多库 → 用户级单库迁移
    db_migrate_p = sub.add_parser("db-migrate-single",
                                  help="Migrate legacy per-project databases to single user database")
    db_migrate_p.add_argument("--dry-run", action="store_true", dest="dry_run", default=True,
                              help="Preview only (default); do not write")
    db_migrate_p.add_argument("--apply", action="store_false", dest="dry_run",
                              help="Actually migrate data; default is dry run")
    db_migrate_p.add_argument("--no-backup", action="store_false", dest="backup", default=True,
                              help="Skip backup of unified database before migration")

    parsed = parser.parse_args(args)

    if parsed.action == "archive":
        result = db.gc_archive(force=parsed.force, dry_run=parsed.dry_run)
        mode = t("cli.messages.gc_mode_full") if parsed.force else t(
            "cli.messages.gc_mode_young")
        dry = t("cli.messages.gc_dry_run") if parsed.dry_run else ""
        cprint(t("cli.messages.gc_archive_title",
               mode=mode, dry=dry), "cyan", bold=True)
        cprint(t("cli.messages.gc_scanned", count=result['scanned']), "dim")
        cprint(t("cli.messages.gc_archived", count=result['archived']),
               "yellow" if result["archived"] else "green")
        cprint(t("cli.messages.gc_skipped", count=result['skipped']), "dim")
        if result["reasons"]:
            cprint(t("cli.messages.gc_reasons_title"), "dim")
            for reason, count in result["reasons"].items():
                cprint(t("cli.messages.gc_reason_item",
                       reason=reason, count=count), "dim")
        cprint()
        return True

    elif parsed.action == "restore":
        result = db.gc_restore(rel_paths=parsed.path, force=parsed.force)
        cprint(t("cli.messages.gc_restore_title"), "cyan", bold=True)
        cprint(t("cli.messages.gc_scanned_archived",
               count=result['scanned']), "dim")
        cprint(t("cli.messages.gc_restored", count=result['restored']),
               "green" if result["restored"] else "dim")
        cprint(t("cli.messages.gc_still_ignored",
               count=result['still_ignored']), "dim")
        if result["restored"] > 0:
            cprint(t("cli.messages.gc_restore_hint"), "yellow")
        cprint()
        return True

    elif parsed.action == "status":
        status = db.gc_status()
        cprint(t("cli.messages.gc_status_title"), "cyan", bold=True)
        cprint(t("cli.messages.gc_active_files",
               count=status['active_files']), "green")
        cprint(t("cli.messages.gc_archived_files", count=status['archived_files']),
               "yellow" if status["archived_files"] else "dim")
        cprint(t("cli.messages.gc_deleted_files",
               count=status['deleted_files']), "dim")
        ratio = f"{status['archive_ratio']*100:.1f}"
        cprint(t("cli.messages.gc_archive_ratio", ratio=ratio), "dim")
        if status["archived_files"] > 0:
            cprint(t("cli.messages.gc_archived_symbols",
                   count=status['archived_symbols']), "dim")
            cprint(t("cli.messages.gc_archived_calls",
                   count=status['archived_calls']), "dim")
        if status["recent_archives"]:
            cprint(t("cli.messages.gc_recent_archives"), "dim")
            for r in status["recent_archives"]:
                from datetime import datetime
                ts = datetime.fromtimestamp(
                    r["archived_at"]).strftime("%Y-%m-%d %H:%M")
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
        cprint(t("cli.messages.gc_purged_symbols",
               count=result['purged_symbols']), "dim")
        cprint(t("cli.messages.gc_purged_calls",
               count=result['purged_calls']), "dim")
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
        mode_key = "cli.messages.gc_retention_mode_dry_run" if result[
            "dry_run"] else "cli.messages.gc_retention_mode_apply"
        cprint(t(mode_key), "yellow" if result["dry_run"] else "green")
        if result["saved_policy"]:
            cprint(t("cli.messages.gc_retention_policy_saved"), "green")
        cprint(t("cli.messages.gc_retention_candidate_versions",
               count=result["candidate_file_versions"]), "dim")
        cprint(t("cli.messages.gc_retention_candidate_external",
               count=result["candidate_external_packages"]), "dim")
        if result["backup_path"]:
            cprint(t("cli.messages.gc_retention_backup",
                   path=result["backup_path"]), "dim")
        if not result["dry_run"]:
            cprint(t("cli.messages.gc_retention_deleted_versions",
                   count=result["deleted_file_versions"]), "dim")
            cprint(t("cli.messages.gc_retention_deleted_external",
                   count=result["deleted_external_symbols"]), "dim")
            cprint(t("cli.messages.gc_retention_deleted_orphans",
                   count=result["deleted_orphan_symbol_contents"]), "dim")

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
                cprint(t("cli.messages.gc_retention_estimate_files_top",
                       top_n=len(affected_files)), "dim")
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
                cprint(t("cli.messages.gc_retention_estimate_pkgs_top",
                       top_n=len(external_top)), "dim")
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
            cprint(t("cli.messages.gc_archive_list_item",
                   idx=idx, name=item["name"]), "dim")
            ts = datetime.fromtimestamp(
                item["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
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
        cprint(t("cli.messages.gc_archive_inspect_file",
               name=info["name"]), "dim")
        cprint(t("cli.messages.gc_archive_inspect_size",
               size=_format_bytes(info["size"])), "dim")
        cprint(t("cli.messages.gc_archive_inspect_schema_version",
               version=info["schema_version"]), "dim")
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
            dry_label = t("cli.messages.gc_audit_dry_run_yes") if row["dry_run"] else t(
                "cli.messages.gc_audit_dry_run_no")
            cprint(t("cli.messages.gc_audit_list_item",
                     idx=idx, id=row["id"], operation=row["operation"],
                     status=row["status"], dry_run=dry_label), "dim")
            ts = datetime.fromtimestamp(
                row["started_at"]).strftime("%Y-%m-%d %H:%M:%S")
            cprint(t("cli.messages.gc_audit_list_started", ts=ts), "dim")
            if row.get("backup_path"):
                cprint(t("cli.messages.gc_audit_list_backup",
                       path=row["backup_path"]), "dim")
            cands = row.get("candidate_counts") or {}
            if cands:
                cprint(t("cli.messages.gc_audit_list_candidates",
                       candidates=cands), "dim")
            dels = row.get("deleted_counts") or {}
            if dels:
                cprint(t("cli.messages.gc_audit_list_deleted", deleted=dels), "dim")
            if row["status"] == "failed" and row.get("error"):
                cprint(t("cli.messages.gc_audit_list_error",
                       error=row["error"]), "red")
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
        cprint(t("cli.messages.gc_audit_show_operation",
               operation=row["operation"]), "dim")
        cprint(t("cli.messages.gc_audit_show_status",
               status=row["status"]), "dim")
        cprint(t("cli.messages.gc_audit_show_dry_run",
                 value=str(row["dry_run"]).lower()), "dim")
        cprint(t("cli.messages.gc_audit_show_operator",
               operator=row["operator"]), "dim")
        started_ts = datetime.fromtimestamp(
            row["started_at"]).strftime("%Y-%m-%d %H:%M:%S")
        cprint(t("cli.messages.gc_audit_show_started", ts=started_ts), "dim")
        if row.get("completed_at"):
            completed_ts = datetime.fromtimestamp(
                row["completed_at"]).strftime("%Y-%m-%d %H:%M:%S")
            cprint(t("cli.messages.gc_audit_show_completed", ts=completed_ts), "dim")
        policy = row.get("policy_json") or {}
        if policy:
            cprint(t("cli.messages.gc_audit_show_policy"), "dim")
            for k, v in policy.items():
                cprint(t("cli.messages.gc_audit_show_policy_item",
                       key=k, value=v), "dim")
        cands = row.get("candidate_counts") or {}
        if cands:
            cprint(t("cli.messages.gc_audit_show_candidates"), "dim")
            for k, v in cands.items():
                cprint(t("cli.messages.gc_audit_show_count_item",
                       key=k, count=v), "dim")
        dels = row.get("deleted_counts") or {}
        if dels:
            cprint(t("cli.messages.gc_audit_show_deleted"), "dim")
            for k, v in dels.items():
                cprint(t("cli.messages.gc_audit_show_count_item",
                       key=k, count=v), "dim")
        if row.get("backup_path"):
            cprint(t("cli.messages.gc_audit_show_backup",
                     path=row["backup_path"], size=row.get("backup_size", 0)), "dim")
        if row["status"] == "failed" and row.get("error"):
            cprint(t("cli.messages.gc_audit_show_error",
                   error=row["error"]), "red")
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
        mode_key = "cli.messages.gc_archive_import_mode_dry_run" if result[
            "dry_run"] else "cli.messages.gc_archive_import_mode_apply"
        cprint(t(mode_key), "yellow" if result["dry_run"] else "green")
        cprint(t("cli.messages.gc_archive_import_target",
                 target=result["target"], value=result["target_value"]), "dim")
        cprint(t("cli.messages.gc_archive_import_path",
               path=result["path"]), "dim")
        if result.get("errors"):
            cprint(t("cli.messages.gc_archive_import_errors_title"), "red")
            for err in result["errors"]:
                cprint(
                    t("cli.messages.gc_archive_import_error_item", error=err), "red")
        # 导入明细
        imported = result.get("imported") or {}
        if imported:
            cprint(t("cli.messages.gc_archive_import_imported_title"), "green")
            for k, v in imported.items():
                if v > 0:
                    cprint(t("cli.messages.gc_archive_import_count_item",
                           key=k, count=v), "green")
        # 跳过明细
        skipped = result.get("skipped") or {}
        if any(v > 0 for v in skipped.values()):
            cprint(t("cli.messages.gc_archive_import_skipped_title"), "yellow")
            for k, v in skipped.items():
                if v > 0:
                    cprint(t("cli.messages.gc_archive_import_count_item",
                           key=k, count=v), "yellow")
        cprint()
        return True

    elif parsed.action == "db-cleanup":
        return _handle_gc_db_cleanup(dry_run=parsed.dry_run,
                                     all_but_current=parsed.all_but_current,
                                     current_workspace_root=db.workspace_root)

    elif parsed.action == "db-migrate-single":
        return _handle_db_migrate_single(
            dry_run=parsed.dry_run,
            backup=parsed.backup,
        )

    return False


def _handle_db_migrate_single(dry_run: bool = True, backup: bool = True) -> bool:
    """旧版多库 → 用户级单库迁移

    将 ~/.callwarden/<hash>/callwarden.db 的数据合并到 ~/.callwarden/callwarden.db。
    迁移 workspaces / tasks / task_steps 表，符号图谱数据建议迁移后 refresh 重建。
    """
    from ..db.db_migrate import migrate_to_single_db

    mode = "[DRY-RUN] " if dry_run else ""
    cprint(f"{mode}Database migration: legacy per-project → single user database",
           "cyan", bold=True)

    result = migrate_to_single_db(dry_run=dry_run, backup=backup)

    if result["errors"] and not result["legacy_dbs"]:
        for err in result["errors"]:
            cprint(f"  {err}", "yellow")
        return True

    cprint(f"  Legacy databases found: {len(result['legacy_dbs'])}", "dim")
    for d in result["legacy_dbs"]:
        cprint(f"    {d}", "dim")

    cprint(f"  Workspaces migrated: {result['migrated_workspaces']}",
           "green" if result["migrated_workspaces"] else "dim")
    cprint(
        f"  Workspaces skipped (root_path exists): {result['skipped_workspaces']}", "dim")
    cprint(f"  Tasks migrated: {result['migrated_tasks']}",
           "green" if result["migrated_tasks"] else "dim")
    cprint(f"  Task steps migrated: {result['migrated_steps']}",
           "green" if result["migrated_steps"] else "dim")

    if result["errors"]:
        cprint("  Errors:", "yellow")
        for err in result["errors"]:
            cprint(f"    {err}", "yellow")

    if result["backup_path"]:
        cprint(f"  Backup created: {result['backup_path']}", "dim")

    if dry_run:
        cprint(
            "  [DRY-RUN] No data written. Run with --apply to migrate.", "yellow")
    else:
        cprint(
            "  Migration complete. Run 'cw refresh --all' to rebuild symbol graph.", "green")
        cprint(
            "  Legacy databases kept for backup. Delete manually after verification:", "dim")
        for d in result["legacy_dbs"]:
            cprint(f"    rm -rf {d}", "dim")

    return True


def _handle_gc_db_cleanup(dry_run: bool = True, all_but_current: bool = False,
                          current_workspace_root: str = "") -> bool:
    """扫描 ~/.callwarden/ 下的旧版 hash 数据库目录，找出并清理孤儿数据库

    扫描 CALLWARDEN_DIR 下的 16 位 hex hash 子目录（每个目录内含 callwarden.db），
    打开每个数据库检查 workspaces 表的 root_path：
    - root_path 为空 → 孤儿
    - root_path 路径不存在（项目目录已删除）→ 孤儿
    - root_path 指向系统临时目录（pytest 残留）→ 孤儿
    - --all-but-current：除当前 workspace 外全部判为孤儿

    dry_run=False 时删除孤儿 hash 目录（整个目录，含 callwarden.db）。

    Args:
        dry_run: True 只报告不删除（默认），False 实际删除
        all_but_current: 保留当前 workspace，其他全部判为孤儿
        current_workspace_root: 当前 workspace 的根路径（用于 --all-but-current）
    """
    import shutil
    import sqlite3
    import tempfile
    from ..config import CALLWARDEN_DIR, norm_path

    # 系统临时目录特征（用于检测 pytest 残留）
    _temp_dir = tempfile.gettempdir().lower().replace("\\", "/")
    _temp_markers = (
        _temp_dir,
        "/tmp/",
        "\\temp\\",
        "/appdata/local/temp/",
        "pytest-",
    )

    current_norm = norm_path(os.path.abspath(
        current_workspace_root)) if current_workspace_root else ""

    def _is_orphan(root_path: str) -> tuple:
        """判断 workspace root_path 是否为孤儿

        Returns:
            (is_orphan, reason)
        """
        if not root_path:
            return True, "empty_path"
        if not os.path.isdir(root_path):
            return True, "path_not_exist"
        rp_lower = root_path.lower().replace("\\", "/")
        for marker in _temp_markers:
            if marker and marker in rp_lower:
                return True, "temp_dir"
        return False, None

    # CALLWARDEN_DIR 不存在
    if not os.path.isdir(CALLWARDEN_DIR):
        cprint(f"Directory not found: {CALLWARDEN_DIR}", "dim")
        cprint("Run 'cw --refresh-all' to initialize.", "dim")
        cprint()
        return True

    # 扫描 hash 子目录（16 位十六进制名，内含 callwarden.db）
    hash_dirs = []
    for name in os.listdir(CALLWARDEN_DIR):
        if len(name) == 16 and all(c in "0123456789abcdef" for c in name):
            dir_path = os.path.join(CALLWARDEN_DIR, name)
            db_file = os.path.join(dir_path, "callwarden.db")
            if os.path.isfile(db_file):
                hash_dirs.append((name, dir_path, db_file))

    # 输出报告
    cprint("Database Cleanup (legacy hash directories)", "cyan", bold=True)
    if dry_run:
        cprint("Mode: DRY RUN (preview only)", "yellow")
    else:
        cprint("Mode: APPLY (will delete orphan directories)", "green")
    cprint()

    if not hash_dirs:
        cprint("No legacy databases found.", "green")
        cprint()
        return True

    orphans = []
    valid = []

    for hash_name, dir_path, db_file in hash_dirs:
        # 打开 hash 目录下的数据库，查询 workspaces 表
        try:
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, name, root_path FROM workspaces ORDER BY id"
            ).fetchall()
            conn.close()
        except sqlite3.OperationalError as e:
            # 无法读取 workspaces 表 → 判为孤儿
            orphans.append({
                "hash": hash_name,
                "dir": dir_path,
                "name": "",
                "root_path": "",
                "reason": f"read_error: {e}",
            })
            continue

        # 检查该目录下所有 workspace（旧版通常只有一个）
        # 只要有一个 workspace 有效，就保留整个目录
        dir_is_orphan = True
        dir_reason = "no_workspaces"
        ws_name = ""
        ws_root = ""

        for row in rows:
            ws_name = row["name"] or ""
            root_path = row["root_path"] or ""

            if all_but_current:
                rp_norm = norm_path(os.path.abspath(
                    root_path)) if root_path else ""
                if current_norm and rp_norm == current_norm:
                    is_orphan = False
                    reason = None
                else:
                    is_orphan = True
                    reason = "not_current"
            else:
                is_orphan, reason = _is_orphan(root_path)

            if not is_orphan:
                dir_is_orphan = False
                dir_reason = None
                ws_root = root_path
                break
            else:
                dir_reason = reason
                ws_root = root_path

        entry = {
            "hash": hash_name,
            "dir": dir_path,
            "name": ws_name,
            "root_path": ws_root,
            "reason": dir_reason,
        }
        if dir_is_orphan:
            orphans.append(entry)
        else:
            valid.append(entry)

    total = len(orphans) + len(valid)
    cprint(f"Total databases: {total}", "dim")
    cprint(f"  Valid:   {len(valid)}", "green")
    orphan_color = "yellow" if orphans else "dim"
    cprint(f"  Orphan:  {len(orphans)}", orphan_color)
    cprint()

    if not orphans:
        cprint("No orphan databases found.", "green")
        cprint()
        return True

    # 显示有效数据库
    if valid:
        cprint("Valid databases (will keep):", "green")
        for idx, entry in enumerate(valid, 1):
            cprint(
                f"  {idx}. {entry['hash']} {entry['name']} -> {entry['root_path']}", "dim")
        cprint()

    # 显示孤儿数据库
    cprint("Orphan databases (will be deleted):", "yellow", bold=True)
    for idx, entry in enumerate(orphans, 1):
        cprint(f"  {idx}. {entry['hash']} {entry['name']}", "yellow")
        cprint(f"     Path: {entry['dir']}", "dim")
        cprint(f"     Reason: {entry['reason']}", "dim")
    cprint()

    if not dry_run:
        # 实际删除孤儿 hash 目录（整个目录）
        deleted = 0
        for entry in orphans:
            try:
                shutil.rmtree(entry["dir"])
                deleted += 1
            except OSError as e:
                cprint(f"  Failed to delete {entry['hash']}: {e}", "red")
        cprint(f"Deleted {deleted} orphan directories.", "green")
    else:
        cprint("Run with --apply to actually delete orphan directories.", "dim")

    cprint()
    return True


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
    cprint(t("cli.messages.gc_policy_older_than",
           count=policy["older_than_days"]), "dim")
    cprint(t("cli.messages.gc_policy_keep_versions",
           count=policy["keep_versions"]), "dim")
    cprint(t("cli.messages.gc_policy_include_external",
           value=str(policy["include_external"]).lower()), "dim")
    cprint(t("cli.messages.gc_policy_external_stale_days",
           count=policy["external_stale_days"]), "dim")
    cprint(t("cli.messages.gc_policy_backup", value=str(
        policy["backup_enabled"]).lower()), "dim")
    cprint(t("cli.messages.gc_policy_vacuum", value=str(
        policy["vacuum_enabled"]).lower()), "dim")
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
# FTS5 全文索引维护子命令（P29）
# --------------------------------------------------------------------

def _handle_fts(args, db):
    """处理 fts 子命令（FTS5 全文索引维护）

    P29：refresh 中断后 symbols_fts 可能为空，search 返回 0 结果。
    提供 `cw fts rebuild` 独立重建命令和 `cw fts status` 状态查询。
    """
    parser = argparse.ArgumentParser(
        prog="cw fts",
        description=t(
            "cli_fts_desc", default="FTS5 full-text index maintenance (rebuild/status)"),
        epilog=_get_subcommand_epilog("fts"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("rebuild",
                   help=t("cli_fts_rebuild_desc",
                          default="Rebuild FTS5 index from symbols table (fix empty search results after interrupted refresh)"))
    sub.add_parser("status",
                   help=t("cli_fts_status_desc",
                          default="Show FTS5 index status (row count, triggers, consistency)"))

    opts = parser.parse_args(args)

    if opts.action == "rebuild":
        print(t("cli_fts_rebuilding", default="Rebuilding FTS5 index..."))
        result = db.rebuild_fts_index()
        if result["success"]:
            print(t("cli_fts_rebuild_done",
                    symbols=result["symbols_count"],
                    fts_rows=result["fts_rows"],
                    triggers=result["triggers_recreated"],
                    elapsed=f"{result['elapsed']:.2f}s"))
            if result["fts_rows"] != result["symbols_count"]:
                print(t("cli_fts_rebuild_mismatch_warning",
                        symbols=result["symbols_count"],
                        fts_rows=result["fts_rows"]))
        else:
            print(t("cli_fts_rebuild_failed", error=result["error"]))
            return False
    elif opts.action == "status":
        status = db.get_fts_status()
        if not status["exists"]:
            print(t("cli_fts_not_exist",
                  default="symbols_fts table does not exist (database version too low or not initialized)"))
            return True
        print(t("cli_fts_status_symbols",
                default="Symbols: {count}",
                count=status["symbols_count"]))
        print(t("cli_fts_status_fts_rows",
                default="FTS5 rows: {count}",
                count=status["fts_rows"]))
        print(t("cli_fts_status_triggers",
                default="Triggers: {triggers}",
                triggers=", ".join(status["triggers"]) if status["triggers"] else "(none)"))
        if status["consistent"]:
            print(t("cli_fts_status_consistent",
                  default="✓ Consistent (fts_rows == symbols_count)"))
        else:
            print(t("cli_fts_status_inconsistent",
                    default="✗ Inconsistent (symbols={symbols}, fts_rows={fts_rows})",
                    symbols=status["symbols_count"],
                    fts_rows=status["fts_rows"]))
            print(t("cli_fts_status_inconsistent_hint",
                    default="  → Run `cw fts rebuild` to fix"))
    return True


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
        description=t("cli.messages.doctor_desc",
                      default="Environment diagnostics and maintenance"),
        epilog=_get_subcommand_epilog("doctor"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--add-defender-exclusion", action="store_true",
                        help=t("cli.messages.doctor_add_defender_help", default="Add .callwarden directory to Windows Defender exclusions (requires admin privileges)"))
    opts = parser.parse_args(args)

    if opts.add_defender_exclusion:
        return _doctor_add_defender_exclusion(db)

    return _doctor_check(db)


def _doctor_check(db):
    """环境诊断：检查数据库状态、性能配置、Defender 状态等"""
    cprint(t("cli.messages.doctor_title",
           default="=== Call Warden Environment Diagnostics ==="), "cyan", bold=True)
    print()

    # 1. 数据库基本信息
    cprint(t("cli.messages.doctor_db_info_title",
           default="[1] Database information"), "yellow", bold=True)
    db_path = db.db_path
    import os
    import sqlite3
    print(t("cli.messages.doctor_db_path",
          default="  Path: {path}", path=db_path))
    print(t("cli.messages.doctor_db_size",
          default="  Size: {size:.2f} MB", size=os.path.getsize(db_path) / 1024 / 1024))

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
    # PRAGMA 不支持绑定参数，用静态 SQL 分派避免字符串拼接（semgrep: sqlalchemy-execute-raw-query / formatted-sql-query）
    _PRAGMA_QUERIES = {
        "journal_mode": "PRAGMA journal_mode",
        "synchronous": "PRAGMA synchronous",
        "busy_timeout": "PRAGMA busy_timeout",
        "cache_size": "PRAGMA cache_size",
        "mmap_size": "PRAGMA mmap_size",
    }
    for key, expected in pragmas.items():
        actual = db.conn.execute(_PRAGMA_QUERIES[key]).fetchone()[0]
        actual_str = str(actual)
        # 应用别名映射
        aliases = pragma_aliases.get(key, {})
        actual_normalized = aliases.get(
            actual_str.lower(), aliases.get(actual_str, actual_str))
        expected_normalized = aliases.get(
            expected.lower(), aliases.get(expected, expected))
        ok = actual_normalized == expected_normalized
        mark = "✓" if ok else "✗"
        color = "green" if ok else "red"
        cprint(t("cli.messages.doctor_pragma_item",
               default="    {mark} {pragma_key} = {actual} (expected: {expected})", mark=mark, pragma_key=key, actual=actual_str, expected=expected), color)
        if not ok:
            all_pragma_ok = False
    print()

    # 2. WAL 文件检查
    cprint(t("cli.messages.doctor_wal_title",
           default="[2] WAL file status"), "yellow", bold=True)
    wal_path = db_path + "-wal"
    shm_path = db_path + "-shm"
    print(t("cli.messages.doctor_wal_file",
          default="  WAL file: {path}", path=wal_path))
    if os.path.exists(wal_path):
        wal_size = os.path.getsize(wal_path) / 1024
        print(t("cli.messages.doctor_wal_size",
              default="    Size: {size:.1f} KB", size=wal_size))
        if wal_size > 1024 * 10:  # > 10MB
            cprint(t("cli.messages.doctor_wal_large",
                   default="    ! WAL file is large; consider running cw doctor --checkpoint"), "yellow")
        else:
            print(t("cli.messages.doctor_wal_size_ok",
                  default="    ✓ Size is normal"))
    else:
        print(t("cli.messages.doctor_wal_missing_ok",
              default="    ✓ Not present (checkpointed)"))
    print()

    # 3. Defender 排除项检查（仅 Windows）
    if sys.platform == "win32":
        cprint(t("cli.messages.doctor_defender_title",
               default="[3] Windows Defender exclusions"), "yellow", bold=True)
        callwarden_dir = os.path.dirname(db_path)
        try:
            import subprocess
            result = subprocess.run(
                ["powershell", "-Command",
                 f"Get-MpPreference | Select-Object -ExpandProperty ExclusionPath"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            )
            exclusions = result.stdout.strip()
            if callwarden_dir.lower() in exclusions.lower():
                cprint(t("cli.messages.doctor_defender_added",
                       default="  ✓ Exclusion already added: {path}", path=callwarden_dir), "green")
            else:
                cprint(t("cli.messages.doctor_defender_missing",
                       default="  ✗ Exclusion missing (recommended to avoid intermittent SQLITE_CANTOPEN)"), "red")
                cprint(t("cli.messages.doctor_defender_dir",
                       default="    Exclusion directory: {path}", path=callwarden_dir), "dim")
                cprint(t("cli.messages.doctor_defender_command",
                       default="    Add command (requires admin):"), "dim")
                cprint(f"      cw doctor --add-defender-exclusion", "dim")
                cprint(t("cli.messages.doctor_defender_manual",
                       default="    Or run manually:"), "dim")
                cprint(f"      powershell -Command \"Add-MpPreference -ExclusionPath '{callwarden_dir}'\"",
                       "dim")
        except Exception as e:
            cprint(t("cli.messages.doctor_defender_check_failed",
                   default="  ? Could not check Defender status: {error}", error=e), "yellow")
        print()

    # 4. 快速连接测试
    cprint(t("cli.messages.doctor_connection_title",
           default="[4] Database connection test"), "yellow", bold=True)
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
        cprint(t("cli.messages.doctor_connection_success",
               default="  ✓ All 5 connection tests succeeded"), "green")
    else:
        cprint(t("cli.messages.doctor_connection_failed",
               default="  ✗ {fail}/5 failed, possible intermittent Defender lock", fail=fail), "red")
    print()

    # 5. 总体评估
    cprint(t("cli.messages.doctor_overall_title",
           default="[5] Overall assessment"), "yellow", bold=True)
    if all_pragma_ok and fail == 0:
        cprint(t("cli.messages.doctor_overall_healthy",
               default="  ✓ Environment is healthy"), "green")
    elif all_pragma_ok:
        cprint(t("cli.messages.doctor_overall_mostly_healthy",
               default="  ~ Environment is mostly healthy, but connection failures occurred (consider adding Defender exclusion)"), "yellow")
    else:
        cprint(t("cli.messages.doctor_overall_needs_work",
               default="  ✗ Environment needs optimization (PRAGMA config is incorrect)"), "red")
    print()

    return True


def _doctor_add_defender_exclusion(db):
    """添加 Windows Defender 排除项（需管理员权限）"""
    if sys.platform != "win32":
        cprint(t("cli.messages.doctor_windows_only",
               default="✗ This command is only available on Windows"), "red")
        return True

    import os
    import subprocess
    callwarden_dir = os.path.dirname(db.db_path)
    # 排除到 .callwarden 根目录（涵盖所有项目的 db）
    parent_dir = os.path.dirname(callwarden_dir)

    cprint(t("cli.messages.doctor_add_defender_title",
           default="=== Add Windows Defender Exclusion ==="), "cyan", bold=True)
    print(t("cli.messages.doctor_add_defender_dir",
          default="  Exclusion directory to add: {path}", path=parent_dir))
    cprint(t("cli.messages.doctor_add_defender_admin_note",
           default="  Note: this operation requires admin privileges"), "yellow")
    print()

    # 检查当前是否已是管理员
    try:
        import ctypes
        is_admin = ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        is_admin = False

    if not is_admin:
        cprint(t("cli.messages.doctor_uac_try",
               default="✗ Current process is not elevated; trying UAC elevation..."), "yellow")
        # 通过 PowerShell Start-Process -Verb RunAs 提权
        cmd = f"Add-MpPreference -ExclusionPath '{parent_dir}'"
        try:
            subprocess.Popen(
                ["powershell", "-Command",
                 f"Start-Process powershell -Verb RunAs -ArgumentList '-Command', '{cmd}; Start-Sleep 2'"],
            )
            cprint(t("cli.messages.doctor_uac_prompted",
                   default="✓ UAC prompt opened; confirm in the popup window"), "green")
            print()
            print(t("cli.messages.doctor_verify_hint",
                  default="  Verify success with:"))
            cprint(f"    cw doctor", "cyan")
        except Exception as e:
            cprint(t("cli.messages.doctor_uac_failed",
                   default="✗ UAC elevation failed: {error}", error=e), "red")
            print()
            print(t("cli.messages.doctor_manual_admin_hint",
                  default="  Run PowerShell as administrator and execute:"))
            cprint(
                f"    Add-MpPreference -ExclusionPath '{parent_dir}'", "yellow")
        return True

    # 已是管理员
    try:
        subprocess.run(
            ["powershell", "-Command",
             f"Add-MpPreference -ExclusionPath '{parent_dir}'"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        cprint(t("cli.messages.doctor_add_defender_success",
               default="✓ Defender exclusion added: {path}", path=parent_dir), "green")
    except Exception as e:
        cprint(t("cli.messages.doctor_add_defender_failed",
               default="✗ Add failed: {error}", error=e), "red")

    return True


# ====================================================================
# C8 Step #1: subcommand 模式对齐 handler
# 为 8 大类新增 subcommand 入口，等价于对应 flag 模式
# ====================================================================


# --------------------------------------------------------------------
# [1] Workspace & Database
# --------------------------------------------------------------------


def _handle_workspace(args, db):
    """处理 workspace 子命令（工作区管理）

    等价 flag: --list-workspaces / --register-workspace / --set-workspace / --delete-workspace
    """
    parser = argparse.ArgumentParser(
        prog="cw workspace",
        description=t("cli.messages.workspace_subcommand_desc",
                      default="Workspace management (list/register/set/delete)"),
    )
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("list", help=t(
        "cli.messages.workspace_action_list", default="List all workspaces"))

    reg = sub.add_parser("register", help=t(
        "cli.messages.workspace_action_register", default="Register a new workspace"))
    reg.add_argument("name", help=t(
        "cli.messages.workspace_arg_name", default="Workspace name"))
    reg.add_argument("root", help=t(
        "cli.messages.workspace_arg_root", default="Workspace root path"))

    set_p = sub.add_parser("set", help=t(
        "cli.messages.workspace_action_set", default="Set active workspace"))
    set_p.add_argument("id_or_name", help=t(
        "cli.messages.workspace_arg_id_or_name", default="Workspace ID or name"))

    del_p = sub.add_parser("delete", help=t(
        "cli.messages.workspace_action_delete", default="Delete a workspace"))
    del_p.add_argument("id_or_name", help=t(
        "cli.messages.workspace_arg_id_or_name", default="Workspace ID or name"))

    scan_p = sub.add_parser("scan", help=t(
        "cli.messages.workspace_action_scan", default="Scan directory for subprojects"))
    scan_p.add_argument("dir", nargs="?", default=".",
                        help="Directory to scan (default: current)")
    scan_p.add_argument("--register", action="store_true",
                        help="Register all found projects as workspaces")
    scan_p.add_argument("--include-all", action="store_true",
                        help="Include non-real subprojects (tests/fixtures/npm/examples etc)")
    scan_p.add_argument("--deep", action="store_true",
                        help="Deep scan: enter git repo to find monorepo subprojects "
                        "(default: shallow mode, each .git = 1 project)")

    gen_ignore_p = sub.add_parser("generate-ignore",
                                  help="Auto-generate .callwardenignore based on project characteristics")
    gen_ignore_p.add_argument("dir", nargs="?", default=".",
                              help="Directory to scan (default: current workspace root)")
    gen_ignore_p.add_argument("--apply", action="store_true",
                              help="Actually write .callwardenignore (default: dry-run preview)")

    opts = parser.parse_args(args)

    if opts.action == "list":
        workspaces = db.list_workspaces()
        print(t("cli.messages.workspaces_title", count=len(workspaces)))
        for ws in workspaces:
            active_mark = t("cli.messages.workspace_active_mark") if ws.get(
                "is_active") else ""
            print(t("cli.messages.workspace_normal",
                  id=ws['id'], name=ws['name']) + active_mark)
            print(t("cli.messages.workspace_path", path=ws['root_path']))
            if ws.get("description"):
                print(t("cli.messages.workspace_desc", desc=ws['description']))
        return True

    if opts.action == "register":
        name, root = opts.name, opts.root
        ws_id = db.register_workspace(name, root)
        print(t("cli.messages.register_success", id=ws_id, name=name, root=root))
        return True

    if opts.action == "set":
        ws_arg = opts.id_or_name
        try:
            ws_id = int(ws_arg)
            success = db.set_active_workspace(ws_id)
        except ValueError:
            success = db.set_active_workspace(ws_arg)
        if success:
            active = db.get_active_workspace()
            print(t("cli.messages.set_success",
                  name=active['name'], root=active['root_path']))
        else:
            print(t("cli.messages.workspace_set_fail", name=ws_arg))
        return True

    if opts.action == "delete":
        ws_arg = opts.id_or_name
        try:
            ws_id = int(ws_arg)
            success = db.delete_workspace(ws_id)
        except ValueError:
            success = db.delete_workspace(ws_arg)
        if success:
            print(t("cli.messages.delete_success", name=ws_arg))
        else:
            print(t("cli.messages.delete_not_found", name=ws_arg))
        return True

    if opts.action == "scan":
        from ..config import scan_subprojects
        scan_dir = os.path.abspath(opts.dir)
        if not os.path.isdir(scan_dir):
            print(t("cli.messages.workspace_scan_not_dir", path=scan_dir))
            return True
        projects = scan_subprojects(scan_dir,
                                    skip_non_real=not opts.include_all,
                                    shallow=not opts.deep)
        print(t("cli.messages.workspace_scan_found", count=len(projects)))
        # 按语言统计
        lang_stats: dict = {}
        for p in projects:
            lang_stats[p["lang"]] = lang_stats.get(p["lang"], 0) + 1
        print(t("cli.messages.workspace_scan_lang_stats"))
        for lang, cnt in sorted(lang_stats.items(), key=lambda x: -x[1]):
            print(f"  {lang:15s}: {cnt}")
        print()
        # 列出每个项目
        for p in projects:
            print(f"  {p['lang']:10s}  {p['name']}  ({p['manifest']})")
            if p["rel_path"]:
                print(f"             {p['rel_path']}")
        # 可选：注册为 workspace
        if opts.register:
            registered = 0
            for p in projects:
                try:
                    db.register_workspace(p["name"], p["root"])
                    registered += 1
                except Exception:
                    pass
            print(t("cli.messages.workspace_scan_registered", count=registered))
        return True

    if opts.action == "generate-ignore":
        from ..config import auto_generate_ignore
        scan_dir = os.path.abspath(opts.dir)
        if not os.path.isdir(scan_dir):
            print(f"Error: {scan_dir} is not a directory")
            return True

        dry_run = not opts.apply
        mode = "DRY RUN (preview)" if dry_run else "APPLY (will write)"
        print(f"Auto-generate .callwardenignore  [{mode}]")
        print(f"  Target: {scan_dir}")
        print()

        result = auto_generate_ignore(scan_dir, dry_run=dry_run)

        if result["written"]:
            print(f"✓ Written to: {result['ignore_file']}")
        else:
            print(f"Would write to: {result['ignore_file']}")

        print(f"  New patterns:     {len(result['new_patterns'])}")
        print(f"  Existing patterns: {len(result['existing_patterns'])}")
        print(
            f"  Default covered:   {len(result['default_covered'])} (built-in, not listed)")
        print()

        if result["new_patterns"]:
            print("New patterns to add:")
            for p in result["new_patterns"]:
                print(f"  {p}")
            print()

        if not dry_run and not result["new_patterns"]:
            print(
                "No new patterns needed (all covered by default baseline or existing rules).")
        elif dry_run and result["new_patterns"]:
            print("Run with --apply to write these rules to .callwardenignore")

        return True

    return True


def _handle_refresh(args, db):
    """处理 refresh 子命令（数据库刷新）

    等价 flag: --refresh-all / --refresh <path> / --force
    用法:
        cw refresh --all            # 增量刷新（等价 --refresh-all）
        cw refresh --all --force     # 强制全量重新解析
        cw refresh <path1> [path2]   # 刷新指定文件（支持多路径）
    """
    parser = argparse.ArgumentParser(
        prog="cw refresh",
        description=t("cli.messages.refresh_subcommand_desc",
                      default="Refresh code graph (incremental or by file)"),
    )
    parser.add_argument("--all", action="store_true", dest="refresh_all",
                        help=t("cli.messages.refresh_arg_all", default="Refresh all files (equivalent to --refresh-all)"))
    parser.add_argument("--force", action="store_true",
                        help=t("cli.messages.refresh_arg_force", default="Force full rebuild (only with --all)"))
    parser.add_argument(
        "paths", nargs="*", help=t("cli.messages.refresh_arg_paths", default="File paths to refresh"))
    opts = parser.parse_args(args)

    if opts.refresh_all:
        # 全量刷新（等价 --refresh-all）
        if opts.force:
            print(t("cli.messages.building_force"))
        else:
            print(t("cli.messages.building_incremental"))
        db.build_full_graph(force=opts.force)
        # 自动同步 AGENTS.md（fail-soft，不阻断 refresh）
        try:
            sync_result = db.rule_sync_agents_md(
                target_path="AGENTS.md",
                dry_run=False,
                actor="cli_refresh",
            )
            if sync_result.get("success"):
                print(t(
                    "cli.messages.agents_md_auto_sync_success",
                    count=sync_result.get("rule_count", 0),
                ))
            else:
                error = sync_result.get("error", "")
                if "marker" in error.lower() or "not found" in error.lower():
                    print(t("cli.messages.agents_md_auto_sync_no_marker"))
                else:
                    print(t(
                        "cli.messages.agents_md_auto_sync_skipped",
                        error=error,
                    ))
        except Exception as exc:
            print(t(
                "cli.messages.agents_md_auto_sync_skipped",
                error=str(exc),
            ))
        return True

    if opts.paths:
        # 刷新指定文件（支持多路径，等价多次 --refresh <path>，C8 Step #5）
        success_count = 0
        failure_count = 0
        failed_paths = []
        start_ts = time.time()
        for path in opts.paths:
            try:
                db.refresh_file(path)
                print(t("cli.messages.refresh_done", path=path))
                success_count += 1
            except Exception as exc:
                failure_count += 1
                failed_paths.append((path, str(exc)))
                cprint(t("cli.messages.refresh_failed",
                       path=path, error=str(exc)), "red")
        elapsed = time.time() - start_ts
        # 多文件时输出汇总
        if len(opts.paths) > 1:
            cprint(t("cli.messages.refresh_multi_summary",
                     success=success_count, failure=failure_count,
                     total=len(opts.paths), elapsed=f"{elapsed:.2f}"), "cyan", bold=True)
            if failed_paths:
                cprint(t("cli.messages.refresh_multi_failed_title"),
                       "red", bold=True)
                for path, err in failed_paths:
                    print(t("cli.messages.refresh_multi_failed_item",
                            path=path, error=err))
        return True

    # 未指定 --all 也未指定路径：打印帮助
    parser.print_help()
    return True


def _handle_stats(args, db):
    """处理 stats 子命令（统计信息）

    等价 flag: --stats

    Phase 5-1 C wire-production: Rust 短路 + fail-soft 降级
    默认走 Rust PyO3 API（callwarden_core.stats_command_run_py），
    rollback_config 中 feature=rust_cli_stats 置为 1 时回退 Python。
    Rust 失败时 fail-soft 降级到 Python json.dumps 路径。
    """
    parser = argparse.ArgumentParser(
        prog="cw stats",
        description=t("cli.messages.stats_subcommand_desc",
                      default="Show database statistics"),
    )
    parser.parse_args(args)
    stats = db.get_stats()

    # Phase 5-1 C wire-production: Rust 短路
    try:
        from callwarden_core import stats_command_run_py
        stats_json = json.dumps(stats, ensure_ascii=False)
        exit_code, stdout, stderr = stats_command_run_py(stats_json)
        if stdout:
            print(stdout)
        if stderr:
            print(stderr, file=sys.stderr)
        return exit_code == 0
    except (ImportError, Exception):
        pass  # fail-soft → 降级 Python 路径

    # Python 实现（fail-soft 降级路径）
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    return True


def _handle_health_report(args, db):
    """处理 health-report 子命令（项目整体健康报告）

    聚合：基础统计 + 演化热点 + 问题统计 + Token 节省，一眼看清项目健康状态。
    """
    parser = argparse.ArgumentParser(
        prog="cw health-report",
        description="Show project overall health report (stats + hotspots + issues + token savings)",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    opts = parser.parse_args(args)

    report: Dict[str, Any] = {}

    # 1. 基础统计
    try:
        report["stats"] = db.get_stats()
    except Exception as e:
        report["stats"] = {"error": str(e)}

    # 2. 演化热点 Top 5（变更最频繁的符号）
    try:
        if hasattr(db, "hotspot_evolution"):
            report["hotspots"] = db.hotspot_evolution()[:5]
    except Exception as e:
        report["hotspots"] = [{"error": str(e)}]

    # 3. 问题统计（Semgrep findings 按 severity 分组）
    try:
        if hasattr(db, "get_semgrep_stats"):
            report["issues"] = db.get_semgrep_stats()
        else:
            cur = db.conn.execute(
                "SELECT severity, COUNT(*) as cnt FROM semgrep_findings GROUP BY severity"
            )
            report["issues"] = {row["severity"]: row["cnt"] for row in cur}
    except Exception as e:
        report["issues"] = {"error": str(e)}

    # 4. Token 节省摘要
    try:
        if hasattr(db, "get_token_savings_report"):
            report["token_savings"] = db.get_token_savings_report(
                time_window="30d")
    except Exception as e:
        report["token_savings"] = {"error": str(e)}

    if opts.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return True

    # 格式化输出
    print()
    cprint("=" * 60, "cyan")
    cprint("  Call Warden 项目健康报告", "cyan", bold=True)
    cprint("=" * 60, "cyan")
    print()

    # 基础统计
    st = report.get("stats", {})
    if isinstance(st, dict) and "error" not in st:
        cprint("  [基础统计]", "yellow", bold=True)
        print(f"    符号总数:     {st.get('total_symbols', 'N/A')}")
        print(f"    文件总数:     {st.get('total_files', 'N/A')}")
        print(f"    调用关系数:   {st.get('total_calls', 'N/A')}")
        print(
            f"    语言数:       {st.get('total_languages', st.get('languages', 'N/A'))}")
        print()
    else:
        print(f"  [基础统计] 获取失败: {st.get('error', 'unknown')}")
        print()

    # 演化热点
    hs = report.get("hotspots", [])
    if hs and isinstance(hs, list) and isinstance(hs[0], dict) and "error" not in hs[0]:
        cprint("  [演化热点 Top 5]（变更最频繁）", "yellow", bold=True)
        for i, h in enumerate(hs, 1):
            name = h.get("qualified_name") or h.get(
                "symbol_name") or h.get("name", "?")
            score = h.get("hotspot_score") or h.get("score", 0)
            changes = h.get("change_count") or h.get("commits", 0)
            print(f"    {i}. {name}  (变更 {changes} 次, 热点分 {score:.1f})")
        print()
    else:
        print("  [演化热点] 无数据或获取失败")
        print()

    # 问题统计
    iss = report.get("issues", {})
    if isinstance(iss, dict) and "error" not in iss:
        cprint("  [静态检查问题]", "yellow", bold=True)
        total = iss.get("total_findings", 0) if isinstance(
            iss.get("total_findings"), int) else 0
        by_sev = iss.get("by_severity", {}) if isinstance(
            iss.get("by_severity"), dict) else iss
        if total:
            print(f"    总数: {total}")
        if by_sev:
            for sev, cnt in by_sev.items():
                print(f"    {sev}: {cnt}")
        if not total and not by_sev:
            print("    无问题")
        print()
    else:
        print(
            f"  [静态检查] 获取失败: {iss.get('error', 'unknown') if isinstance(iss, dict) else iss}")
        print()

    # Token 节省
    ts = report.get("token_savings", {})
    if isinstance(ts, dict) and "error" not in ts:
        cprint("  [Token 节省（近 30 天）]", "yellow", bold=True)
        saved = ts.get("total_saved") or ts.get("tokens_saved") or 0
        calls = ts.get("total_calls") or ts.get("call_count") or 0
        print(f"    节省 Token:   {saved}")
        print(f"    MCP 调用数:   {calls}")
        print()
    else:
        print("  [Token 节省] 无数据")
        print()

    cprint("=" * 60, "cyan")
    return True


def _handle_dashboard(args, db):
    """处理 dashboard 子命令（项目综合状态驾驶舱）

    聚合 7 个 section：overview / code_scale / code_quality / call_graph /
    task_risk / audit / evolution，并附风险预警列表。

    默认 quick=True（100K ~280ms），--full 启用完整模式（含圈复杂度计算），
    --with-cycles 启用循环检测，--with-evolution 启用演化趋势（需 git history）。
    """
    parser = argparse.ArgumentParser(
        prog="cw dashboard",
        description="Show project dashboard (7 sections + risk warnings)",
    )
    parser.add_argument("--full", action="store_true",
                        help="Full mode (compute cyclomatic complexity, slow on large repos)")
    parser.add_argument("--with-cycles", action="store_true",
                        help="Detect call cycles (detect_cycles, may be slow without Rust GraphStore)")
    parser.add_argument("--with-evolution", action="store_true",
                        help="Show evolution trends (requires git history imported)")
    parser.add_argument("--risks", action="store_true",
                        help="Show project risk warnings (high complexity / oversized / blocking findings)")
    parser.add_argument("--top", type=int, default=5,
                        help="Top N for risk lists (default 5)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    opts = parser.parse_args(args)

    import time as _time
    t_start = _time.time()

    dashboard = db.get_project_dashboard(
        with_cycles=opts.with_cycles,
        with_evolution=opts.with_evolution,
        quick=not opts.full,
        top_n=opts.top,
    )

    if opts.json:
        out = dict(dashboard)
        out["_elapsed_ms"] = round((_time.time() - t_start) * 1000, 1)
        if opts.risks:
            out["risks"] = db.get_project_risks(top_n=opts.top)
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return True

    # 格式化输出
    print()
    cprint("=" * 70, "cyan")
    cprint("  Call Warden 项目驾驶舱", "cyan", bold=True)
    cprint("=" * 70, "cyan")

    # ── 1. 概览 ─────────────────────────────────────────────────────
    ov = dashboard.get("overview", {})
    if isinstance(ov, dict) and "error" not in ov:
        cprint("\n  [1] 概览", "yellow", bold=True)
        print(f"    Workspace:    {ov.get('workspace_name', '?')}")
        print(f"    Root:         {ov.get('root_path', '?')}")
        print(f"    Git HEAD:     {ov.get('git_head') or '(not a git repo)'}")
        if ov.get('db_stale'):
            cprint(f"    DB 状态:      ⚠ 滞后于 git HEAD（建议 cw --refresh-all）", "red")
        else:
            cprint(f"    DB 状态:      ✓ 同步", "green")
        lb = ov.get('last_build_ts', 0)
        if lb:
            ago = _format_ago(lb)
            print(f"    最近构建:     {ago}")
        db_size = ov.get('db_size_bytes', 0)
        print(f"    DB 大小:      {_format_size(db_size)}")

    # ── 2. 代码规模 ─────────────────────────────────────────────────
    cs = dashboard.get("code_scale", {})
    if isinstance(cs, dict) and "error" not in cs:
        cprint("\n  [2] 代码规模", "yellow", bold=True)
        print(f"    文件总数:     {cs.get('total_files', 0)}")
        print(f"    代码总行数:   {cs.get('total_lines', 0):,}")
        print(f"    符号总数:     {cs.get('total_symbols', 0):,}")
        print(f"    已注释符号:   {cs.get('commented_symbols', 0):,}")
        by_kind = cs.get('by_kind', {})
        if by_kind:
            kind_str = ", ".join(f"{k}={v}" for k, v in sorted(
                by_kind.items(), key=lambda x: -x[1])[:6])
            print(f"    符号分布:     {kind_str}")
        by_lang = cs.get('by_language', {})
        if by_lang:
            lang_str = ", ".join(
                f".{k}={v}" for k, v in list(by_lang.items())[:6])
            print(f"    语言分布:     {lang_str}")

    # ── 3. 代码质量 ─────────────────────────────────────────────────
    cq = dashboard.get("code_quality", {})
    if isinstance(cq, dict) and "error" not in cq:
        cprint("\n  [3] 代码质量", "yellow", bold=True)
        if cq.get('quick_mode'):
            cprint("    (quick 模式，未算圈复杂度；--full 启用)", "dark_grey")
        else:
            print(f"    平均圈复杂度: {cq.get('avg_complexity', 0)}")
            print(f"    最大圈复杂度: {cq.get('max_complexity', 0)}")
            dist = cq.get('complexity_distribution', {}) or {}
            if dist:
                print("    复杂度分布:")
                for level, cnt in dist.items():
                    print(f"      {level:<14s} {cnt}")
        print(f"    注释覆盖率:   {cq.get('comment_coverage_pct', 0)}%")
        print(f"    未注释函数:   {cq.get('uncommented_fns', 0)}")
        largest = cq.get('largest_fns_top', []) or []
        if largest:
            cprint("\n    Top 大函数:", "cyan")
            for i, f in enumerate(largest, 1):
                name = f.get('qualified_name', '?')
                lc = f.get('line_count', 0)
                fp = f.get('file_path', '?')
                print(f"      {i}. {name}  ({lc} 行, {fp})")
        hotspots = cq.get('complexity_hotspots_top', []) or []
        if hotspots:
            cprint("\n    Top 复杂度函数:", "cyan")
            for i, h in enumerate(hotspots, 1):
                name = h.get('qualified_name', '?')
                cx = h.get('cyclomatic_complexity',
                           0) or h.get('complexity', 0)
                print(f"      {i}. {name}  (圈复杂度 {cx})")

    # ── 4. 调用图 ───────────────────────────────────────────────────
    cg = dashboard.get("call_graph", {})
    if isinstance(cg, dict) and "error" not in cg:
        cprint("\n  [4] 调用图", "yellow", bold=True)
        tc = cg.get('total_calls', 0)
        rc = cg.get('resolved_calls', 0)
        rate = cg.get('resolve_rate_pct', 0)
        cf = cg.get('cross_file_calls', 0)
        print(f"    调用总数:     {tc:,}")
        print(f"    已解析:       {rc:,} ({rate}%)")
        print(f"    跨文件调用:   {cf:,}")
        cyc = cg.get('cycles_count')
        if cyc is None:
            print("    循环调用:     (未计算，--with-cycles 启用)")
        else:
            if cyc > 0:
                cprint(
                    f"    循环调用:     ⚠ {cyc} 个（cw call-chain --detect-cycles 查看）", "red")
            else:
                cprint(f"    循环调用:     ✓ 无循环", "green")
        orphans = cg.get('orphans_count', 0)
        if orphans > 0:
            print(f"    孤立函数:     {orphans}（未被任何函数调用）")
        depth = cg.get('depth_distribution', {}) or {}
        if depth:
            items = sorted(depth.items(), key=lambda x: int(x[0]))[:10]
            depth_str = ", ".join(f"d{k}={v}" for k, v in items)
            print(
                f"    深度分布:     {depth_str}{'...' if len(depth) > 10 else ''}")

    # ── 5. 任务与风险 ───────────────────────────────────────────────
    tr = dashboard.get("task_risk", {})
    if isinstance(tr, dict) and "error" not in tr:
        cprint("\n  [5] 任务与风险", "yellow", bold=True)
        tc = tr.get('task_counts', {})
        print(f"    任务状态:     open={tc.get('open', 0)}, in_progress={tc.get('in_progress', 0)}, "
              f"review={tc.get('review', 0)}, applied={tc.get('applied', 0)}")
        of = tr.get('open_findings_count', 0)
        bf = tr.get('blocking_findings_count', 0)
        if bf > 0:
            cprint(f"    阻塞 findings: ⚠ {bf} 条（cw task findings 查看）", "red")
        else:
            cprint(f"    阻塞 findings: ✓ 无", "green")
        print(f"    open findings: {of}")
        pc = tr.get('pending_rule_candidates', 0)
        if pc > 0:
            print(f"    待审规则候选: {pc}")
        rec = tr.get('recommended_action', '')
        if rec:
            cprint(f"    推荐下一步:   {rec}", "green")

    # ── 6. 审计 ─────────────────────────────────────────────────────
    au = dashboard.get("audit", {})
    if isinstance(au, dict) and "error" not in au:
        cprint("\n  [6] 审计状态", "yellow", bold=True)
        print(f"    active rules: {au.get('active_rules_count', 0)}")
        broken = au.get('audit_broken_count', 0)
        if broken > 0:
            cprint(f"    审计链损坏:   ⚠ {broken} 条（cw audit verify 查看）", "red")
        else:
            cprint(f"    审计链:       ✓ 完整", "green")

    # ── 7. 演化趋势 ─────────────────────────────────────────────────
    ev = dashboard.get("evolution")
    if ev is None:
        cprint("\n  [7] 演化趋势 (未计算，--with-evolution 启用)", "dark_grey")
    elif isinstance(ev, dict) and "error" not in ev:
        cprint("\n  [7] 演化趋势", "yellow", bold=True)
        commits = ev.get('recent_commits', []) or []
        if commits:
            cprint(f"    最近 {len(commits)} 次提交:", "cyan")
            for c in commits[:5]:
                h = (c.get('commit_hash', '') or '')[:8]
                msg = (c.get('message', '') or '').split('\n')[0][:60]
                author = (c.get('author', '') or '')[:20]
                print(f"      {h}  {author:<20s}  {msg}")
        else:
            print("    最近提交:     无（cw git import 导入 git history）")
        churn = ev.get('churn_30d')
        if isinstance(churn, dict):
            print(f"    30 天 churn:  {churn.get('total_lines_changed', 0):,} 行变更, "
                  f"{churn.get('files_changed', 0)} 文件")
        hs = ev.get('hotspot_top', []) or []
        if hs:
            cprint("\n    Top 演化热点:", "cyan")
            for i, h in enumerate(hs, 1):
                name = h.get('qualified_name') or h.get('symbol_name', '?')
                score = h.get('hotspot_score', 0) or 0
                print(f"      {i}. {name}  (热点分 {score:.1f})")

    # ── 风险预警 ────────────────────────────────────────────────────
    if opts.risks:
        cprint("\n  [风险预警]", "yellow", bold=True)
        # quick 模式跟随 dashboard 的 --full（quick 默认跳过高复杂度计算）
        risks = db.get_project_risks(top_n=opts.top, quick=not opts.full)
        if not risks:
            cprint("    ✓ 无风险预警", "green")
        else:
            sev_color = {"high": "red", "medium": "yellow", "low": "dark_grey"}
            for i, r in enumerate(risks, 1):
                sev = r.get('severity', 'low')
                sev_marker = {"high": "⚠⚠", "medium": "⚠",
                              "low": "•"}.get(sev, "•")
                rtype = r.get('type', '?')
                detail = r.get('detail', '')
                qn = r.get('qualified_name', '')
                color = sev_color.get(sev, "dark_grey")
                cprint(f"    {i}. [{sev_marker} {sev}] {rtype}", color)
                if qn:
                    cprint(f"       函数: {qn}", color)
                cprint(f"       详情: {detail}", color)

    # ── 耗时 ────────────────────────────────────────────────────────
    elapsed = (_time.time() - t_start) * 1000
    print()
    cprint(f"  (报表生成耗时 {elapsed:.1f}ms)", "dark_grey")
    cprint("=" * 70, "cyan")
    return True


def _format_size(n: int) -> str:
    """格式化字节数为 B/KB/MB/GB"""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n/1024/1024:.1f} MB"
    return f"{n/1024/1024/1024:.2f} GB"


def _format_ago(ts: float) -> str:
    """格式化时间戳为"多久之前"的相对描述"""
    if not ts:
        return "从未"
    delta = time.time() - ts
    if delta < 60:
        return f"{int(delta)} 秒前"
    if delta < 3600:
        return f"{int(delta/60)} 分钟前"
    if delta < 86400:
        return f"{int(delta/3600)} 小时前"
    return f"{int(delta/86400)} 天前"


def _handle_status(args, db):
    """处理 status 子命令（完整状态概览）

    等价 flag: --status
    """
    parser = argparse.ArgumentParser(
        prog="cw status",
        description=t("cli.messages.status_subcommand_desc",
                      default="Show full status overview"),
    )
    parser.parse_args(args)
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
        """格式化时间戳为"多久之前"的相对描述"""
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
    print(
        f"  {t('cli.messages.status_last_build')}: {fmt_ago(status['last_build'])}")
    print()
    print(f"  {t('cli.messages.status_files_title')}")
    on_disk = t("cli.messages.status_files_on_disk")
    tracked = t("cli.messages.status_files_tracked")
    print(f"    {on_disk}: {fi['on_disk']}  ({tracked}: {fi['tracked']})")
    if fi["new"]:
        new_label = t("cli.messages.status_files_new")
        print(
            f"    {new_label}: {fi['new']}  {', '.join(fi['new_files'][:5])}{'...' if len(fi['new_files']) > 5 else ''}")
    if fi["stale"]:
        stale_label = t("cli.messages.status_files_stale")
        print(
            f"    {stale_label}: {fi['stale']}  {', '.join(fi['stale_files'][:5])}{'...' if len(fi['stale_files']) > 5 else ''}")
    if fi["deleted"]:
        deleted_label = t("cli.messages.status_files_deleted")
        print(
            f"    {deleted_label}: {fi['deleted']}  {', '.join(fi['deleted_files'][:5])}{'...' if len(fi['deleted_files']) > 5 else ''}")
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
    print(
        f"    {t('cli.messages.status_uncommented_fns')}: {sy['uncommented_fns']}")
    print()
    print(f"  {t('cli.messages.status_calls_title')}")
    print(f"    {t('cli.messages.status_calls_total')}: {ca['total']}")
    resolved_label = t("cli.messages.status_calls_resolved")
    rate_label = t("cli.messages.status_calls_rate")
    print(
        f"    {resolved_label}: {ca['resolved']}  ({rate_label}: {ca['resolve_rate']}%)")
    print(f"    {t('cli.messages.status_calls_cross')}: {ca['cross_file']}")
    print()
    if status["needs_rebuild"]:
        print(f"  ⚠ {t('cli.messages.status_rebuild_hint')}")
    else:
        print(f"  {t('cli.messages.status_up_to_date')}")
    print()
    return True


# --------------------------------------------------------------------
# [2] Query & Search
# --------------------------------------------------------------------


def _handle_search(args, db):
    """处理 search 子命令（符号搜索）

    等价 flag: --search
    """
    parser = argparse.ArgumentParser(
        prog="cw search",
        description=t("cli.messages.search_subcommand_desc",
                      default="Search symbols"),
    )
    parser.add_argument("query", help=t(
        "cli.messages.search_arg_query", default="Search query"))
    parser.add_argument("--kind", default=None,
                        help=t("cli.messages.search_arg_kind", default="Filter by kind"))
    parser.add_argument("--limit", type=int, default=50, help=t(
        "cli.messages.search_arg_limit", default="Max results (default 50)"))
    opts = parser.parse_args(args)

    symbols = db.search_symbols(opts.query, kind=opts.kind, limit=opts.limit)
    kind_info = t("cli.messages.search_kind_info",
                  kind=opts.kind) if opts.kind else ""
    print(t("cli.messages.search_title", query=opts.query, kind_info=kind_info,
            total=len(symbols), shown=min(opts.limit, len(symbols))))
    print()
    for i, sym in enumerate(symbols[:opts.limit]):
        depth = sym["depth"] if sym["depth"] >= 0 else "?"
        sig = sym.get("signature", "")[:50] if sym.get("signature") else ""
        comment_mark = "✓" if sym["has_comment"] else " "
        print(
            f"  [{i+1:3d}] depth={depth:>3} [{comment_mark}] {sym['kind']:8s} {sym['qualified_name']}")
        print(f"         {sym['file_path']}:{sym['start_line']}")
        if sig:
            print(f"         {sig}")
    if len(symbols) >= opts.limit:
        print()
        print(t("cli.messages.search_more"))
    return True


def _handle_grep(args, db):
    """处理 grep 子命令（带符号上下文的文本搜索）

    核心价值：返回 file:line [in fn xxx] content，每行带符号归属。
    默认只展示有符号归属的行（过滤 import/文档/注释噪音），--include-all 恢复全部。

    支持多关键词 AND：cw grep import time → 找同时含 "import" 和 "time" 的行。
    用引号包住含空格的字符串：cw grep "import time" → 整体作为 pattern。

    实现：
    1. 调 rg 做文本匹配（用第一个 pattern，速度快）
    2. 多 pattern 时 Python 端 AND 过滤（每行必须含全部 patterns）
    3. 解析匹配行（file:line:content）
    4. 按文件分组，批量查 find_symbols_at_lines 拿符号归属
    5. 默认过滤 [no symbol]，--include-all 时保留全部
    6. 截断到 limit
    7. 输出 file:line [in fn qualified_name] content
    """
    import shutil
    import subprocess
    import re as _re

    parser = argparse.ArgumentParser(
        prog="cw grep",
        description="Grep with symbol context (file:line [in fn xxx] content)",
    )
    parser.add_argument("patterns", nargs="+",
                        help="Search pattern(s). Multiple patterns = AND (all must match on same line). "
                             "Quote to treat as single pattern: \"import time\"")
    parser.add_argument("--fixed", action="store_true",
                        help="Treat patterns as fixed strings (rg -F)")
    parser.add_argument("--limit", type=int, default=200,
                        help="Max matches (default 200, applied after symbol filter)")
    parser.add_argument("--path", default=None,
                        help="Search path (default: workspace root)")
    parser.add_argument("--include-all", action="store_true",
                        help="Include matches outside symbols (imports/docs/comments). "
                             "Default: only show matches inside symbols.")
    parser.add_argument("--kind", default=None,
                        help="Only show matches inside symbols of this kind (fn/class/...)")
    opts = parser.parse_args(args)

    # 多 pattern 处理：
    # - len == 1：单 pattern（原行为），rg 用这个 pattern
    # - len > 1：AND 语义，rg 用第一个 pattern 搜索，Python 端过滤同时包含所有 patterns 的行
    patterns: List[str] = opts.patterns
    primary_pattern = patterns[0]
    is_multi_and = len(patterns) > 1

    # 1. 检查 rg 是否可用，不可用回退 Python re
    rg_path = shutil.which("rg")
    search_root = opts.path if opts.path else db.workspace_root
    if not search_root or not os.path.isdir(search_root):
        # path 参数可能传的是文件，转父目录
        if opts.path and os.path.isfile(opts.path):
            search_root = os.path.dirname(opts.path)
        else:
            search_root = db.workspace_root

    raw_lines: List[str] = []  # 形如 "file:line:content"
    if rg_path:
        # 2a. 用 rg 做文本搜索（rg 速度远快于 Python re）
        cmd = [rg_path, "-n", "--no-heading", "--color", "never"]
        if opts.fixed:
            cmd.append("-F")
        cmd.extend([primary_pattern, search_root])
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, shell=False,
            )
            # rg exit code 1 = 无匹配（不是错误）
            if result.returncode == 0:
                raw_lines = result.stdout.splitlines()
            elif result.returncode == 1:
                raw_lines = []
            elif result.returncode == 2:
                # rg 错误（正则语法错等）
                print(f"Error: {result.stderr.strip()}")
                return False
        except subprocess.TimeoutExpired:
            print("Error: grep timeout after 30s")
            return False
        except Exception as e:
            print(f"Error: {e}")
            return False
    else:
        # 2b. 无 rg → Python re 回退（慢但可用）
        try:
            # fixed 模式：用 re.escape 转义所有正则元字符
            pat = _re.escape(
                primary_pattern) if opts.fixed else primary_pattern
            pattern = _re.compile(pat)
        except _re.error as e:
            print(f"Error: regex error: {e}")
            return False
        for dirpath, dirnames, filenames in os.walk(search_root):
            # 跳过常见忽略目录
            dirnames[:] = [d for d in dirnames if d not in (
                ".git", "__pycache__", "node_modules", "target", ".venv", "venv", "dist", "build")]
            for fname in filenames:
                # 只扫源码文件（避免二进制和大文件）
                if not fname.endswith((".py", ".rs", ".ts", ".js", ".go", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".kt", ".swift", ".scala")):
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if pattern.search(line):
                                raw_lines.append(
                                    f"{fpath}:{i}:{line.rstrip()}")
                except OSError:
                    continue

    if not raw_lines:
        print(f"No matches for: {primary_pattern}")
        return True

    # 3. 解析所有 rg 输出为 (file, line, content)
    matches: List[Tuple[str, int, str]] = []
    for raw in raw_lines:
        m = _re.match(r"^(.+?):(\d+):(.*)$", raw)
        if not m:
            continue
        fpath, lineno_str, content = m.group(1), int(m.group(2)), m.group(3)
        matches.append((fpath, lineno_str, content))

    # 3b. 多 pattern AND 过滤：rg 已匹配第一个 pattern，Python 端检查剩余 patterns
    if is_multi_and:
        remaining_patterns = patterns[1:]
        if opts.fixed:
            # fixed 模式：直接子串匹配
            matches = [
                (f, l, c) for f, l, c in matches
                if all(p in c for p in remaining_patterns)
            ]
        else:
            # 正则模式：编译剩余 patterns
            try:
                remaining_compiled = [_re.compile(
                    p) for p in remaining_patterns]
            except _re.error as e:
                print(f"Error: regex error in pattern: {e}")
                return False
            matches = [
                (f, l, c) for f, l, c in matches
                if all(p.search(c) for p in remaining_compiled)
            ]
        if not matches:
            print(f"No matches for AND: {' '.join(patterns)}")
            return True

    # 4. 按文件分组，每组一次性查所有行号
    file_to_lines: Dict[str, List[int]] = {}
    for fpath, lineno, _ in matches:
        file_to_lines.setdefault(fpath, []).append(lineno)

    # 批量查符号归属（在截断前完成，确保不会因 limit 丢失代码匹配）
    file_line_symbols: Dict[str, Dict[int, Optional[Dict]]] = {}
    for fpath, lines in file_to_lines.items():
        file_line_symbols[fpath] = db.find_symbols_at_lines(fpath, lines)

    # 5. 过滤 + 截断
    # 5a. 默认过滤 [no symbol]（agent 要有效信息）；--include-all 时保留全部
    no_symbol_filtered = 0
    if not opts.include_all:
        before_no_symbol_filter = len(matches)
        matches = [
            (f, l, c) for f, l, c in matches
            if file_line_symbols.get(f, {}).get(l) is not None
        ]
        no_symbol_filtered = before_no_symbol_filter - len(matches)
    # 5b. --kind：过滤指定符号类型
    if opts.kind:
        matches = [
            (f, l, c) for f, l, c in matches
            if (file_line_symbols.get(f, {}).get(l) or {}).get("kind") == opts.kind
        ]
    # 5c. 截断到 limit（此时已过滤无符号行，截断不会丢代码匹配）
    total_before_limit = len(matches)
    matches = matches[:opts.limit]

    # 6. 输出
    pattern_display = " ".join(patterns) if is_multi_and else patterns[0]
    filter_note = ""
    if not opts.include_all and no_symbol_filtered > 0:
        filter_note = f", filtered {no_symbol_filtered} no-symbol"
    kind_note = f" [kind={opts.kind}]" if opts.kind else ""
    print(f"Grep with symbol context: pattern={pattern_display!r}, {len(matches)} matches"
          f" (of {total_before_limit} after filter{filter_note}){kind_note}")
    print()
    for fpath, lineno, content in matches:
        sym_map = file_line_symbols.get(fpath, {})
        sym = sym_map.get(lineno)
        if sym:
            # 带符号上下文：file:line [in fn xxx] content
            kind_label = sym.get("kind", "?")
            qname = sym.get("qualified_name") or sym.get("name") or "?"
            print(f"{fpath}:{lineno} [in {kind_label} {qname}] {content}")
        else:
            # 仅 --include-all 时才会到这（import/顶层语句/注释块外）
            print(f"{fpath}:{lineno} [no symbol] {content}")
    print()
    print(
        f"Total: {len(matches)} matches (of {total_before_limit} after filter)")
    return True


def _handle_issues(args, db):
    """处理 issues 子命令（符号级静态检查问题）

    查询符号相关的 Semgrep findings + Guardrail findings，让 agent 一站式看到
    已知缺陷/告警。相比 cw symbol（只注入前 5 条），cw issues 返回完整列表。

    用法：
        cw issues <qualified_name>              # 默认 WARNING+ 级别
        cw issues <qualified_name> --include-info  # 包含 INFO 级别
    """
    parser = argparse.ArgumentParser(
        prog="cw issues",
        description="Symbol-level static check issues (Semgrep + Guardrail findings)",
    )
    parser.add_argument("qualified_name", help="Symbol qualified name")
    parser.add_argument("--include-info", action="store_true",
                        help="Include INFO level findings (default: WARNING+ only)")
    opts = parser.parse_args(args)

    issues = db.get_symbol_issues(
        opts.qualified_name, include_info=opts.include_info)

    if not issues:
        print(f"No issues found for: {opts.qualified_name}")
        print("（可能原因：1. 未运行 semgrep 扫描；2. 符号无 WARNING+ 问题；3. 符号不存在）")
        return True

    # 统计
    by_source = {"semgrep": 0, "guardrail": 0}
    by_severity = {}
    for issue in issues:
        src = issue.get("source", "?")
        sev = issue.get("severity", "?").upper()
        by_source[src] = by_source.get(src, 0) + 1
        by_severity[sev] = by_severity.get(sev, 0) + 1

    sev_str = ", ".join(f"{k}:{v}" for k, v in sorted(
        by_severity.items(), key=lambda x: -x[1]))
    src_str = ", ".join(f"{k}:{v}" for k, v in sorted(
        by_source.items(), key=lambda x: -x[1]))
    info_note = " (+INFO)" if opts.include_info else ""
    print(f"Issues for {opts.qualified_name}: {len(issues)} total{info_note}")
    print(f"  by severity: {sev_str}")
    print(f"  by source:   {src_str}")
    print()

    for i, issue in enumerate(issues, 1):
        src = issue.get("source", "?")
        sev = issue.get("severity", "?").upper()
        rule_id = issue.get("rule_id", "?")
        rule_name = issue.get("rule_name", "")
        msg = issue.get("message", "")
        start_line = issue.get("start_line", 0)
        end_line = issue.get("end_line", 0)
        confidence = issue.get("confidence", "")
        status = issue.get("status", "")
        snippet = issue.get("snippet", "")
        fix = issue.get("fix", "")

        # 标题行：[i] [source] [severity] rule_id (line range)
        line_range = f"L{start_line}" if start_line == end_line or not end_line else f"L{start_line}-{end_line}"
        line_info = f" {line_range}" if start_line else ""
        conf_info = f" conf={confidence}" if confidence and confidence != "UNKNOWN" else ""
        status_info = f" [{status}]" if status else ""
        print(f"[{i}] [{src}] [{sev}] {rule_id}{line_info}{conf_info}{status_info}")
        if rule_name:
            print(f"    rule: {rule_name}")
        if msg:
            print(f"    msg:  {msg}")
        if snippet:
            # snippet 可能多行，缩进展示
            snippet_lines = snippet.strip().split("\n")
            for sl in snippet_lines[:3]:  # 最多展示 3 行
                print(f"    code: {sl}")
            if len(snippet_lines) > 3:
                print(f"    code: ... ({len(snippet_lines)-3} more lines)")
        if fix:
            print(f"    fix:  {fix}")
        print()

    print(f"Total: {len(issues)} issues")
    return True


def _handle_tests(args, db):
    """处理 tests 子命令（符号级单元测试 case 查询 + 测试运行结果）

    回答 agent 高频问题："foo() 有哪些 test 在测它？" "foo() 的测试最近稳定吗？"

    用法：
        cw tests <qualified_name>            # 查 foo 的测试列表
        cw tests <qualified_name> --reverse   # 反向：查 test_foo 测了哪些函数
        cw tests --build [--force]            # 全量重建测试关联（refresh 测试文件后调用）
        cw tests <qualified_name> --history   # 查 foo 的测试运行历史与稳定性
        cw tests --import <junit.xml>         # 导入 JUnit XML 测试运行结果
                [--ci-run-id ID] [--ci-url URL]
    """
    parser = argparse.ArgumentParser(
        prog="cw tests",
        description="Symbol-level test case relations and test run history",
    )
    parser.add_argument("qualified_name", nargs="?",
                        help="Symbol qualified name")
    parser.add_argument("--reverse", action="store_true",
                        help="Reverse query: what does this test function test?")
    parser.add_argument("--build", action="store_true",
                        help="Build/rebuild test case relations (run after refresh of test files)")
    parser.add_argument("--force", action="store_true",
                        help="With --build: force full rebuild (clear existing relations first)")
    parser.add_argument("--history", action="store_true",
                        help="Show test run history/stability for the symbol (requires test_runs data)")
    parser.add_argument("--import", dest="import_file", default="",
                        help="Import JUnit XML test results (file path or XML content)")
    parser.add_argument("--ci-run-id", default="",
                        help="CI run ID (optional, used with --import to group one CI run)")
    parser.add_argument("--ci-url", default="",
                        help="CI run URL (optional, used with --import)")
    parser.add_argument("--limit", type=int, default=50,
                        help="Max run records to display (with --history, default 50)")
    opts = parser.parse_args(args)

    # --import 模式：导入 JUnit XML 测试运行结果
    if opts.import_file:
        stats = db.import_test_results(
            opts.import_file,
            ci_run_id=opts.ci_run_id,
            ci_url=opts.ci_url,
        )
        if "parse_error" in stats:
            print(f"Error: {stats['parse_error']}")
            return False
        print("Test results imported:")
        print(f"  Total:    {stats['total']}")
        print(f"  Passed:   {stats['passed']}")
        print(f"  Failed:   {stats['failed']}")
        print(f"  Skipped:  {stats['skipped']}")
        print(f"  Error:    {stats['error']}")
        print(f"  Matched:  {stats['matched']}  (test_name → symbol_id)")
        return True

    # --history 模式：查符号的测试运行历史与稳定性
    if opts.history:
        if not opts.qualified_name:
            print("Error: qualified_name required with --history")
            return False
        result = db.get_test_stability(opts.qualified_name, limit=opts.limit)
        print(f"Test stability for {opts.qualified_name}:")
        print(f"  Total runs:   {result['total_runs']}")
        if result['total_runs'] > 0:
            print(f"  Pass rate:    {result['pass_rate']*100:.1f}%")
            print(f"  Avg duration: {result['avg_duration_ms']:.1f} ms")
        else:
            print("  (no test runs found)")
            print("  提示：先运行 'cw tests --import <junit.xml>' 导入 CI 测试结果")
            return True

        if result['recent_failures']:
            print(f"\nRecent failures (top {len(result['recent_failures'])}):")
            for f in result['recent_failures']:
                ts = time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(f.get('run_at', 0)))
                err_type = f.get('error_type', '') or '?'
                print(f"  - {f['test_name']} [{err_type}] @ {ts}")
                msg = f.get('error_message', '')
                if msg:
                    print(f"    {msg[:100]}")

        if result['by_test']:
            print(f"\nBy test (pass/total):")
            # 按通过率升序（最不稳定的在前）
            sorted_tests = sorted(
                result['by_test'].items(),
                key=lambda kv: (kv[1]['passed'] / kv[1]['total']
                                if kv[1]['total'] else 1, kv[1]['total']),
            )
            for name, st in sorted_tests:
                rate = st['passed'] * 100 / st['total'] if st['total'] else 0
                failed_str = f", {st['failed']} failed" if st.get(
                    'failed', 0) else ""
                print(
                    f"  {name}: {st['passed']}/{st['total']} ({rate:.0f}%{failed_str})")
        return True

    # --build 模式：重建关联表
    if opts.build:
        stats = db.build_test_relations(force=opts.force)
        print(f"Test relations built:")
        print(f"  Total test functions scanned: {stats['total_test_fns']}")
        print(f"  direct_call relations:     {stats.get('direct_call', 0)}")
        print(
            f"  name_convention relations:  {stats.get('name_convention', 0)}")
        print(f"  indirect relations:         {stats.get('indirect', 0)}")
        print(f"  Total inserted:             {stats['inserted']}")
        return True

    if not opts.qualified_name:
        print("Error: qualified_name required (or use --build/--import/--history)")
        return False

    if opts.reverse:
        # 反向查询：test_foo 测了哪些函数
        tested = db.get_tested_functions(opts.qualified_name)
        if not tested:
            print(f"No tested functions found for: {opts.qualified_name}")
            print("（可能原因：1. 未运行 cw tests --build；2. 此 test_fn 未关联到任何函数；3. 符号不存在）")
            return True

        print(
            f"Tested functions for {opts.qualified_name}: {len(tested)} total")
        print()
        for i, t in enumerate(tested, 1):
            method = t.get("match_method", "?")
            confidence = t.get("confidence", "?")
            tested_qn = t.get("tested_qualified_name", "?")
            tested_file = t.get("tested_file", "")
            line = t.get("tested_start_line", 0)
            print(f"[{i}] [{method}] [{confidence}] {tested_qn}")
            if tested_file:
                print(f"    file: {tested_file}:{line}")
        print()
        print(f"Total: {len(tested)} tested functions")
        return True

    # 正向查询：foo 有哪些 test
    test_cases = db.get_test_cases(opts.qualified_name)

    if not test_cases:
        print(f"No test cases found for: {opts.qualified_name}")
        print("（可能原因：1. 未运行 cw tests --build；2. 此函数无测试；3. 符号不存在）")
        print("提示：运行 'cw tests --build' 重建测试关联表")
        return True

    # 统计
    by_confidence = {}
    by_method = {}
    for tc in test_cases:
        conf = tc.get("confidence", "?")
        method = tc.get("match_method", "?")
        by_confidence[conf] = by_confidence.get(conf, 0) + 1
        by_method[method] = by_method.get(method, 0) + 1

    conf_str = ", ".join(f"{k}:{v}" for k, v in sorted(
        by_confidence.items(), key=lambda x: -x[1]))
    method_str = ", ".join(f"{k}:{v}" for k, v in sorted(
        by_method.items(), key=lambda x: -x[1]))
    print(f"Test cases for {opts.qualified_name}: {len(test_cases)} total")
    print(f"  by confidence: {conf_str}")
    print(f"  by method:     {method_str}")
    print()

    for i, tc in enumerate(test_cases, 1):
        method = tc.get("match_method", "?")
        confidence = tc.get("confidence", "?")
        test_qn = tc.get("test_qualified_name", "?")
        test_name = tc.get("test_name", "")
        test_file = tc.get("test_file", "")
        test_line = tc.get("test_start_line", 0)
        print(f"[{i}] [{method}] [{confidence}] {test_qn}")
        if test_file:
            print(f"    file: {test_file}:{test_line}")
    print()
    print(f"Total: {len(test_cases)} test cases")
    return True


def _handle_symbol(args, db):
    """处理 symbol 子命令（符号详情）

    等价 flag: --symbol
    """
    parser = argparse.ArgumentParser(
        prog="cw symbol",
        description=t("cli.messages.symbol_subcommand_desc",
                      default="Show symbol detail"),
    )
    parser.add_argument("name", help=t(
        "cli.messages.symbol_arg_name", default="Qualified symbol name"))
    opts = parser.parse_args(args)

    detail = db.get_symbol(opts.name)
    if not detail:
        print(t("cli.messages.symbol_not_found", name=opts.name))
        print(t("cli.messages.symbol_search_hint"))
        return True
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
    print(t("cli.messages.symbol_calls_out_title",
          count=len(detail['calls_out'])))
    if detail["calls_out"]:
        for call in detail["calls_out"][:20]:
            target = call["target_name"]
            line = call.get("call_line", "")
            line_info = f" (line {line})" if line else ""
            print(f"  → {target}{line_info}")
        if len(detail["calls_out"]) > 20:
            print(t("cli.messages.symbol_more",
                  count=len(detail['calls_out']) - 20))
    else:
        print(t("cli.messages.symbol_none"))
    print()
    print(t("cli.messages.symbol_called_by_title",
          count=len(detail['called_by'])))
    if detail["called_by"]:
        for call in detail["called_by"][:20]:
            caller = call["caller_name"]
            line = call.get("call_line", "")
            line_info = f" (line {line})" if line else ""
            print(f"  ← {caller}{line_info}")
        if len(detail["called_by"]) > 20:
            print(t("cli.messages.symbol_more",
                  count=len(detail['called_by']) - 20))
    else:
        print(t("cli.messages.symbol_none"))

    # 展示静态检查问题（fail-soft 注入，最多 5 条；完整列表用 cw issues <QN>）
    issues = detail.get("issues", [])
    issues_total = detail.get("issues_total", 0)
    if issues:
        print()
        print(
            f"Issues ({len(issues)} of {issues_total}, use 'cw issues {opts.name}' for full):")
        for issue in issues:
            src = issue.get("source", "?")
            sev = issue.get("severity", "?").upper()
            rule_id = issue.get("rule_id", "?")
            msg = issue.get("message", "")
            start_line = issue.get("start_line", 0)
            line_info = f" L{start_line}" if start_line else ""
            print(f"  [{src}] [{sev}] {rule_id}{line_info}: {msg}")
    elif issues_total == 0:
        # 无问题时不打印噪音（保持输出简洁）
        pass
    else:
        # 有问题但都被过滤（如全是 INFO），提示
        print()
        print(
            f"Issues: {issues_total} total (filtered, use 'cw issues {opts.name} --include-info')")
    return True


def _handle_file(args, db):
    """处理 file 子命令（文件符号列表）

    等价 flag: --file
    """
    parser = argparse.ArgumentParser(
        prog="cw file",
        description=t("cli.messages.file_subcommand_desc",
                      default="List symbols in a file"),
    )
    parser.add_argument("path", help=t(
        "cli.messages.file_arg_path", default="File path"))
    opts = parser.parse_args(args)

    symbols = db.get_file_symbols(opts.path)
    print(t("cli.messages.file_symbols_title", path=opts.path, count=len(symbols)))
    for s in symbols:
        print(
            f"  {s['start_line']}-{s['end_line']}: {s['kind']} {s['name']} ({s['visibility']})")
    return True


def _handle_query(args, db):
    """处理 query 子命令（符号定位）

    等价 flag: --query
    """
    parser = argparse.ArgumentParser(
        prog="cw query",
        description=t("cli.messages.query_subcommand_desc",
                      default="Query symbol location"),
    )
    parser.add_argument("name", help=t(
        "cli.messages.query_arg_name", default="Symbol name"))
    parser.add_argument("file", help=t(
        "cli.messages.query_arg_file", default="File path"))
    opts = parser.parse_args(args)

    result = db.get_symbol_location(opts.name, opts.file)
    if result:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(t("cli.messages.query_not_found", name=opts.name))
    return True


# --------------------------------------------------------------------
# [3] Call Chain Analysis
# --------------------------------------------------------------------


def _handle_callers(args, db):
    """处理 callers 子命令（调用方查询）

    等价 flag: --callers
    P28：支持 --qualified 可选参数，大规模项目避免短名跨模块误匹配
    """
    parser = argparse.ArgumentParser(
        prog="cw callers",
        description=t("cli.messages.callers_subcommand_desc",
                      default="Show callers of a symbol"),
    )
    parser.add_argument("name", help=t(
        "cli.messages.callers_arg_name", default="Symbol name"))
    parser.add_argument("--qualified", default=None,
                        help="完整限定名（如 module::Class::method），精确匹配避免跨模块误匹配")
    opts = parser.parse_args(args)

    callers = db.get_callers(opts.name, opts.qualified)
    print(t("cli.messages.callers_title", name=opts.name, count=len(callers)))
    for c in callers:
        cross = t(
            "cli.messages.callers_cross_file") if c["is_cross_file"] else ""
        print(t("cli.messages.callers_item",
                file=c['caller_file'], line=c['call_line'], name=c['caller_name'], cross=cross))
    return True


def _handle_callees(args, db):
    """处理 callees 子命令（被调用方查询）

    等价 flag: --callees
    P28：支持 --qualified 可选参数，大规模项目避免短名跨模块误匹配
    """
    parser = argparse.ArgumentParser(
        prog="cw callees",
        description=t("cli.messages.callees_subcommand_desc",
                      default="Show callees of a symbol"),
    )
    parser.add_argument("name", help=t(
        "cli.messages.callees_arg_name", default="Symbol name"))
    parser.add_argument("--qualified", default=None,
                        help="完整限定名（如 module::Class::method），精确匹配避免跨模块误匹配")
    opts = parser.parse_args(args)

    callees = db.get_callees(opts.name, opts.qualified)
    print(t("cli.messages.callees_title", name=opts.name, count=len(callees)))
    for c in callees:
        cross = t(
            "cli.messages.callees_cross_file") if c["is_cross_file"] else ""
        file_info = f" ({c['callee_file']})" if c["callee_file"] else t(
            "cli.messages.callees_unresolved")
        print(t("cli.messages.callees_item",
                line=c['call_line'], name=c['callee_name'], cross=cross, file_info=file_info))
    return True


def _handle_call_chain(args, db):
    """处理 call-chain 子命令（向下调用链）

    等价 flag: --call-chain
    """
    parser = argparse.ArgumentParser(
        prog="cw call-chain",
        description=t("cli.messages.call_chain_subcommand_desc",
                      default="Show call chain down"),
    )
    parser.add_argument("name", help=t(
        "cli.messages.call_chain_arg_name", default="Symbol name"))
    parser.add_argument("--depth", type=int, default=10, help=t(
        "cli.messages.call_chain_arg_depth", default="Max depth (default 10)"))
    opts = parser.parse_args(args)

    result = db.get_call_chain_down(opts.name, max_depth=opts.depth)
    print(t("cli.messages.call_chain_down_title", name=result['start']))
    print(t("cli.messages.call_chain_down_total",
          count=result['total_downstream']))
    print(t("cli.messages.call_chain_down_max_depth",
          depth=result['max_depth_reached']))
    print()
    for level in result["levels"]:
        print(t("cli.messages.call_chain_down_level",
              depth=level['depth'], count=level['count']))
        for item in level["callees"][:15]:
            print(f"  → {item['callee']}")
        if level["count"] > 15:
            print(t("cli.messages.call_chain_down_more",
                  count=level['count'] - 15))
        print()
    return True


def _handle_topo(args, db):
    """处理 topo 子命令（拓扑排序）

    等价 flag: --topo
    """
    parser = argparse.ArgumentParser(
        prog="cw topo",
        description=t("cli.messages.topo_subcommand_desc",
                      default="Topological order"),
    )
    parser.add_argument("--limit", type=int, default=50, help=t(
        "cli.messages.topo_arg_limit", default="Max results (default 50)"))
    opts = parser.parse_args(args)

    order = db.get_topological_order(opts.limit)
    print(t("cli.messages.topo_title", count=len(order)))
    for i, sym in enumerate(order):
        path = sym.get("path", sym.get("rel_path", ""))
        print(t("cli.messages.topo_item",
                idx=i+1, depth=f"{sym['depth']:2d}", path=path, line=sym['start_line'], name=sym['name']))
    return True


def _handle_graph(args, db):
    """处理 graph 子命令（F11: build_graph_from_c_files 接入生产路径）

    F11（2026-07-20 批次6）：将 rust_ext/src/lib.rs 的 `build_graph_from_c_files`
    PyO3 函数接入 CLI。该函数能从 C 文件列表 rayon 并行构建完整 GraphStore
    （CSR + 符号表 + 调用边），跳过 SQLite INSERT 中间层，适用于 C 重型
    代码库（如固件）的快速符号图谱构建。

    子命令格式：
        cw graph build-from-c <dir> [--threads N] [--dump <path>] [--max-files N]

    当前为"可选加速路径"，不替代 db_build.py 的标准 build_full_graph：
    - 标准路径：parse → SQLite INSERT → GraphStore.load_from_sqlite（持久化）
    - F11 路径：parse + 内存构 CSR → 可选 dump 到 .cwsnap（无 DB 写入）

    场景：H6 验收 C 语言大规模符号图谱构建；固件项目快速概览调用链。
    """
    parser = argparse.ArgumentParser(
        prog="cw graph",
        description="Graph 构建（F11: Rust 并行构 CSR）",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # graph build-from-c
    bfc = sub.add_parser("build-from-c",
                         help="从 C 文件列表 rayon 并行构建 GraphStore（F11）")
    bfc.add_argument("directory", help="目标目录（递归扫描 .c 文件）")
    bfc.add_argument("--threads", type=int, default=None,
                     help="rayon 线程数（默认 None=自动）")
    bfc.add_argument("--dump", default=None,
                     help="将 GraphStore dump 到 .cwsnap 文件（可选）")
    bfc.add_argument("--max-files", type=int, default=10000,
                     help="最大文件数（避免误扫描超大目录，默认 10000）")
    bfc.add_argument("--query", default=None,
                     help="构建后查询指定符号的 callers（可选，用于自检）")

    opts = parser.parse_args(args)

    if opts.action != "build-from-c":
        print(f"ERROR: 未知 graph action: {opts.action}", file=sys.stderr)
        return False

    import os as _os
    import time as _time
    target_dir = _os.path.abspath(opts.directory)
    if not _os.path.isdir(target_dir):
        print(f"ERROR: 目标目录不存在：{target_dir}", file=sys.stderr)
        return False

    # 1. 扫描 .c 文件
    print(f"[F11] 扫描 C 文件：{target_dir}")
    t0 = _time.perf_counter()
    c_files: list = []
    for root, _dirs, files in _os.walk(target_dir):
        for fname in files:
            if fname.endswith(".c"):
                abs_path = _os.path.join(root, fname)
                # module_path 用相对路径去后缀（与 db_build.py 一致）
                rel_path = _os.path.relpath(abs_path, target_dir)
                module_path = _os.path.splitext(
                    rel_path)[0].replace(_os.sep, ".")
                c_files.append((abs_path, module_path))
                if len(c_files) >= opts.max_files:
                    print(f"  达到 --max-files 上限 {opts.max_files}，停止扫描")
                    break
        if len(c_files) >= opts.max_files:
            break
    scan_t = _time.perf_counter() - t0

    if not c_files:
        print(f"  未找到任何 .c 文件")
        return True

    print(f"  扫描到 {len(c_files)} 个 .c 文件（耗时 {scan_t:.2f}s）")

    # 2. 调用 build_graph_from_c_files
    try:
        from callwarden_core import build_graph_from_c_files
    except ImportError as e:
        print(f"ERROR: callwarden_core 不可用（{e}）",
              file=sys.stderr)
        print("提示：请先构建 Rust 扩展（cd rust_ext && cargo build --release）",
              file=sys.stderr)
        return False

    print(f"[F11] 构建完整 GraphStore（threads={opts.threads}）...")
    t0 = _time.perf_counter()
    try:
        store, sym_count, edge_count = build_graph_from_c_files(
            c_files, num_threads=opts.threads
        )
    except Exception as e:
        print(f"ERROR: build_graph_from_c_files 失败：{e}", file=sys.stderr)
        return False
    build_t = _time.perf_counter() - t0

    print(f"  构建完成：{build_t:.2f}s")
    print(f"  符号数: {sym_count:,}")
    print(f"  调用边: {edge_count:,}")
    if sym_count > 0:
        print(f"  平均每符号边数: {edge_count / sym_count:.2f}")

    # 3. 可选：dump 到 .cwsnap
    if opts.dump:
        dump_path = _os.path.abspath(opts.dump)
        print(f"[F11] dump 到文件：{dump_path}")
        t0 = _time.perf_counter()
        try:
            store.dump_to_file(dump_path)
            dump_t = _time.perf_counter() - t0
            size_mb = _os.path.getsize(dump_path) / (1024 * 1024)
            print(f"  dump 完成：{dump_t:.2f}s, 大小: {size_mb:.2f} MB")
        except Exception as e:
            print(f"ERROR: dump_to_file 失败：{e}", file=sys.stderr)
            return False

    # 4. 可选：自检查询
    if opts.query:
        print(f"[F11] 自检 get_callers({opts.query})...")
        try:
            callers = store.get_callers(opts.query)
            if callers:
                print(f"  找到 {len(callers)} 个 callers")
                for c in callers[:5]:
                    print(f"    - {c}")
            else:
                print(f"  未找到 callers（符号不存在或无调用方）")
        except Exception as e:
            print(f"WARNING: 自检查询失败：{e}", file=sys.stderr)

    print(f"\n[F11] 完成。耗时: build={build_t:.2f}s")
    return True


def _handle_config(args, db):
    """处理 config 子命令（N4: config_loader 分层配置接入）

    N4（2026-07-20 批次6）：将 release/config_loader.py 接入 CLI。
    分层加载器实现存在但此前没有 Python CLI/daemon 生产 import。

    子命令格式：
        cw config explain          # 输出每个有效值的来源（CLI>env>user>system>default）
        cw config paths            # 输出当前平台的配置/数据目录路径
        cw config check-role <r>   # 检查当前平台是否支持指定角色（local/client/agent/daemon/all）

    设计：纯查询命令，不写数据库；可独立于 daemon 运行；TOML 文件不存在时
    返回默认值，不抛异常（fail-soft）。
    """
    parser = argparse.ArgumentParser(
        prog="cw config",
        description="分层配置加载器（N4: release/config_loader 接入）",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # config explain
    sub.add_parser("explain",
                   help="输出每个配置值及其来源（CLI>env>user>system>default）")

    # config paths
    sub.add_parser("paths",
                   help="输出当前平台的配置/数据目录路径")

    # config check-role
    cr = sub.add_parser("check-role",
                        help="检查当前平台是否支持指定角色")
    cr.add_argument("role", choices=["local", "client", "agent", "daemon", "all"],
                    help="要检查的角色")

    opts = parser.parse_args(args)

    # N4：直接 import release.config_loader
    # 注意：release/ 是 package 化的，需通过 callwarden.release.* 路径或 sys.path 注入
    import os as _os
    import sys as _sys

    # 优先尝试 callwarden.release.config_loader（package 模式）
    config_loader = None
    try:
        from callwarden.release.config_loader import (
            load_config, PlatformPaths, check_role_supported, fail_closed_unsupported,
        )
        config_loader = "callwarden.release.config_loader"
    except ImportError:
        # fallback：直接从 release 目录加载
        release_dir = _os.path.join(_os.path.dirname(
            _os.path.dirname(_os.path.abspath(__file__))), "release")
        if _os.path.isdir(release_dir):
            if release_dir not in _sys.path:
                _sys.path.insert(0, _os.path.dirname(release_dir))
            try:
                from release.config_loader import (
                    load_config, PlatformPaths, check_role_supported, fail_closed_unsupported,
                )
                config_loader = "release.config_loader"
            except ImportError as e:
                print(f"ERROR: 无法 import config_loader: {e}", file=_sys.stderr)
                return False
        else:
            print(f"ERROR: release/ 目录不存在", file=_sys.stderr)
            return False

    if opts.action == "explain":
        # 加载分层配置（无 CLI overrides，仅 env + TOML + default）
        config = load_config()
        explained = config.explain()
        print(f"# N4 分层配置（来源：{config_loader}）")
        print(f"# 优先级：CLI > env(CW_*) > user_config > system_config > default")
        print()
        print(f"{'Key':<30} {'Value':<40} {'Source'}")
        print(f"{'-'*30} {'-'*40} {'-'*20}")
        for item in explained:
            key = item["key"]
            value = item["value"]
            source = item["source"]
            # 截断过长的 value
            if len(value) > 38:
                value = value[:35] + "..."
            print(f"{key:<30} {value:<40} {source}")
        print(f"\n共 {len(explained)} 个配置项")
        return True

    if opts.action == "paths":
        paths = PlatformPaths.detect()
        print(f"# N4 PlatformPaths（来源：{config_loader}）")
        print(f"# 平台：{_sys.platform}")
        print()
        print(f"{'Name':<20} {'Path'}")
        print(f"{'-'*20} {'-'*60}")
        print(f"{'system_config':<20} {paths.system_config}")
        print(f"{'user_config':<20} {paths.user_config}")
        print(f"{'system_data':<20} {paths.system_data}")
        print(f"{'user_data':<20} {paths.user_data}")
        if paths.runtime:
            print(f"{'runtime':<20} {paths.runtime}")
        print()
        print("提示：")
        print(f"  - 系统配置文件：{paths.system_config}（需 root/admin 写入）")
        print(f"  - 用户配置文件：{paths.user_config}（普通用户写入）")
        print(f"  - 数据目录：{paths.user_data}（数据库等持久化数据）")
        return True

    if opts.action == "check-role":
        supported = check_role_supported(opts.role)
        if supported:
            print(f"角色 '{opts.role}' 在当前平台 {_sys.platform} 上 ✅ 支持")
            return True
        else:
            print(f"角色 '{opts.role}' 在当前平台 {_sys.platform} 上 ❌ 不支持")
            print("提示：")
            print("  - Windows/macOS 仅支持 local/client 角色")
            print("  - Linux 才支持 agent/daemon/all 角色（需 SO_PEERCRED + SCM_RIGHTS + UDS）")
            return False

    print(f"ERROR: 未知 config action: {opts.action}", file=_sys.stderr)
    return False


# --------------------------------------------------------------------
# [4] Code Health & Metrics
# --------------------------------------------------------------------


def _handle_metrics(args, db):
    """处理 metrics 子命令（度量汇总）

    等价 flag: --metrics
    """
    parser = argparse.ArgumentParser(
        prog="cw metrics",
        description=t("cli.messages.metrics_subcommand_desc",
                      default="Show code metrics summary"),
    )
    parser.parse_args(args)
    summary = db.get_code_metrics_summary()
    print(t("cli.messages.metrics_title"))
    print(t("cli.messages.metrics_files", count=summary['file_count']))
    print(t("cli.messages.metrics_functions", count=summary['function_count']))
    print(t("cli.messages.metrics_total_lines", count=summary['total_lines']))
    print(t("cli.messages.metrics_calls", count=summary['total_calls']))
    print()
    print(t("cli.messages.metrics_avg_complexity",
          value=summary['avg_complexity']))
    print(t("cli.messages.metrics_max_complexity",
          value=summary['max_complexity']))
    print()
    print(t("cli.messages.metrics_complexity_dist"))
    dist = summary["complexity_distribution"]
    total_fn = sum(dist.values()) or 1
    for level, count in dist.items():
        pct = count / total_fn * 100
        bar = "#" * int(pct / 2)
        print(f"    {level:<12s} {count:4d} ({pct:5.1f}%) {bar}")
    print()
    print(t("cli.messages.metrics_comment_coverage",
          pct=summary['comment_coverage']))
    return True


def _handle_complexity(args, db):
    """处理 complexity 子命令（复杂度热点）

    等价 flag: --complexity
    """
    parser = argparse.ArgumentParser(
        prog="cw complexity",
        description=t("cli.messages.complexity_subcommand_desc",
                      default="Show complexity hotspots"),
    )
    parser.add_argument("limit", type=int, nargs="?", default=20, help=t(
        "cli.messages.complexity_arg_limit", default="Max results (default 20)"))
    parser.add_argument("--module", default=None, help=t(
        "cli.messages.complexity_arg_module", default="Filter by module"))
    opts = parser.parse_args(args)

    hotspots = db.get_complexity_hotspots(
        limit=opts.limit, module_filter=opts.module or "")
    filter_info = t("cli.messages.complexity_filter",
                    module=opts.module) if opts.module else ""
    print(t("cli.messages.complexity_title",
          filter_info=filter_info, count=len(hotspots)))
    print()
    complexity_h = t("cli.messages.col_complexity", default="Complexity")
    lines_h = t("cli.messages.col_lines", default="Lines")
    depth_h = t("cli.messages.col_depth", default="Depth")
    fn_h = t("cli.messages.col_function", default="Function")
    print(f"  {'#':>3}  {complexity_h:>6}  {lines_h:>5}  {depth_h:>4}  {fn_h}")
    print(f"  {'-'*3}  {'-'*6}  {'-'*5}  {'-'*4}  {'-'*50}")
    for i, fn in enumerate(hotspots, 1):
        risk = "!" if fn["cyclomatic_complexity"] > 10 else " "
        print(
            f"  {i:3d}{risk}  {fn['cyclomatic_complexity']:>6}  {fn['line_count']:>5}  {fn['depth']:>4}  {fn['qualified_name'][:60]}")
        print(f"        {fn['file_path']}:{fn['start_line']}")
    print()
    print(t("cli.messages.complexity_hint"))
    return True


def _handle_coupling(args, db):
    """处理 coupling 子命令（模块耦合分析）

    等价 flag: --coupling
    """
    parser = argparse.ArgumentParser(
        prog="cw coupling",
        description=t("cli.messages.coupling_subcommand_desc",
                      default="Show module coupling analysis"),
    )
    parser.parse_args(args)
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
        print(
            f"  {i:3d}  {mod['module'][:40]:<40s}  {mod['afferent']:>4}  {mod['efferent']:>4}  {mod['total_coupling']:>4}  {inst_label:>6}")
    return True


def _handle_comment_coverage(args, db):
    """处理 comment-coverage 子命令（注释覆盖率）

    等价 flag: --comment-coverage
    """
    parser = argparse.ArgumentParser(
        prog="cw comment-coverage",
        description=t("cli.messages.comment_coverage_subcommand_desc",
                      default="Show comment coverage"),
    )
    parser.add_argument("--by", default="module", dest="group_by", help=t(
        "cli.messages.comment_coverage_arg_by", default="Group by (default module)"))
    opts = parser.parse_args(args)

    result = db.get_comment_coverage(group_by=opts.group_by)
    print(t("cli.messages.comment_coverage_title"))
    print(t("cli.messages.comment_coverage_total", count=result['total']))
    print(t("cli.messages.comment_coverage_commented",
          count=result['commented']))
    print(t("cli.messages.comment_coverage_rate", pct=result['coverage']))
    print()
    print(t("cli.messages.comment_coverage_by_kind"))
    for kind, info in sorted(result["by_kind"].items(), key=lambda x: -x[1]["total"]):
        pct = round(info["commented"] / info["total"] *
                    100, 1) if info["total"] > 0 else 0
        bar_len = int(pct / 5)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        print(
            f"  {bar} {pct:5.1f}%  {kind:12s}  ({info['commented']}/{info['total']})")
    if result.get("by_module"):
        print()
        print(t("cli.messages.comment_coverage_by_module"))
        modules = sorted(result["by_module"].items(),
                         key=lambda x: x[1]["coverage"])
        for i, (mod, info) in enumerate(modules[:30]):
            pct = info["coverage"]
            bar_len = int(pct / 5)
            bar = "█" * bar_len + "░" * (20 - bar_len)
            print(
                f"  {bar} {pct:5.1f}%  {mod:50s}  ({info['commented']}/{info['total']})")
        if len(modules) > 30:
            print(t("cli.messages.comment_coverage_more_modules",
                  count=len(modules) - 30))
    if result.get("by_file"):
        print()
        print(t("cli.messages.comment_coverage_by_file"))
        files = sorted(result["by_file"].items(),
                       key=lambda x: x[1]["coverage"])
        for i, (fpath, info) in enumerate(files[:30]):
            pct = info["coverage"]
            bar_len = int(pct / 5)
            bar = "█" * bar_len + "░" * (20 - bar_len)
            print(
                f"  {bar} {pct:5.1f}%  {fpath:50s}  ({info['commented']}/{info['total']})")
        if len(files) > 30:
            print(t("cli.messages.comment_coverage_more_files", count=len(files) - 30))
    return True


def _handle_uncommented(args, db):
    """处理 uncommented 子命令（未注释符号列表）

    等价 flag: --uncommented
    """
    parser = argparse.ArgumentParser(
        prog="cw uncommented",
        description=t("cli.messages.uncommented_subcommand_desc",
                      default="Show uncommented symbols"),
    )
    parser.add_argument("kind", nargs="?", default="fn", help=t(
        "cli.messages.uncommented_arg_kind", default="Symbol kind (default fn)"))
    parser.add_argument("--module", default=None, help=t(
        "cli.messages.uncommented_arg_module", default="Filter by module"))
    parser.add_argument("--limit", type=int, default=50, help=t(
        "cli.messages.uncommented_arg_limit", default="Max results (default 50)"))
    opts = parser.parse_args(args)

    symbols = db.get_uncommented_symbols(
        kind=opts.kind, module_filter=opts.module)
    filter_info = t("cli.messages.uncommented_module_filter",
                    module=opts.module) if opts.module else ""
    print(t("cli.messages.uncommented_title", kind=opts.kind, filter_info=filter_info,
            total=len(symbols), shown=min(opts.limit, len(symbols))))
    print()
    for i, sym in enumerate(symbols[:opts.limit]):
        depth = sym["depth"] if sym["depth"] >= 0 else "?"
        sig = sym.get("signature", "")[:60] if sym.get("signature") else ""
        print(f"  [{i+1:3d}] depth={depth:>3}  {sym['qualified_name']}")
        print(f"         {sym['file_path']}:{sym['start_line']}")
        if sig:
            print(f"         {sig}")
    if len(symbols) > opts.limit:
        print()
        print(t("cli.messages.uncommented_more", count=len(symbols) - opts.limit))
    return True


def _handle_function_issues(args, db):
    """处理 function-issues 子命令（函数缺陷检测）

    等价 flag: --function-issues
    """
    parser = argparse.ArgumentParser(
        prog="cw function-issues",
        description=t("cli.messages.function_issues_subcommand_desc",
                      default="Show function issues"),
    )
    parser.add_argument("fn", nargs="?", default="", help=t(
        "cli.messages.function_issues_arg_fn", default="Function name (empty for list mode)"))
    parser.add_argument("--type", default=None, help=t(
        "cli.messages.function_issues_arg_type", default="Filter by issue type"))
    parser.add_argument("--module", default=None, help=t(
        "cli.messages.function_issues_arg_module", default="Filter by module"))
    parser.add_argument("--limit", type=int, default=30, help=t(
        "cli.messages.function_issues_arg_limit", default="Max results (default 30)"))
    opts = parser.parse_args(args)

    fn_name = opts.fn
    module_filter = opts.module or ""
    issue_filter = opts.type or ""
    results = db.get_function_issues(
        qualified_name=fn_name,
        module_filter=module_filter,
        issue_filter=issue_filter,
        limit=opts.limit,
    )
    severity_icon = {"danger": "[!]", "warn": "[~]", "info": "[i]"}

    if fn_name:
        # 单函数详情模式
        if results:
            r = results[0]
            print(t("cli.messages.function_issues_title",
                  name=r['qualified_name']))
            print(t("cli.messages.function_issues_module",
                  module=r['module_path'] or '(unknown)'))
            print(t("cli.messages.function_issues_count",
                  count=r['issue_count']))
            print()
            for issue in r["issues"]:
                icon = severity_icon.get(issue["severity"], "[?]")
                print(f"  {icon} {issue['label']}  (x{issue['count']})")
                print(f"      {issue['description']}")
            print()
        else:
            print(t("cli.messages.function_issues_title", name=fn_name))
            filter_str = t("cli.messages.function_issues_filter",
                           filter=issue_filter) if issue_filter else ""
            print(t("cli.messages.function_issues_no_issues") + filter_str)
            print()
    else:
        # 列表模式
        if issue_filter:
            print(t("cli.messages.function_issues_list_title_type",
                  filter=issue_filter, count=len(results)))
        elif module_filter:
            print(t("cli.messages.function_issues_list_title_module",
                  module=module_filter, count=len(results)))
        else:
            print(t("cli.messages.function_issues_list_title", count=len(results)))
        print()
        for i, r in enumerate(results, 1):
            issue_labels = []
            for issue in r["issues"]:
                icon = severity_icon.get(issue["severity"], "")
                issue_labels.append(
                    f"{icon}{issue['label']}" + (f"(x{issue['count']})" if issue["count"] > 1 else ""))
            issue_str = "  ".join(issue_labels)
            print(f"  #{i:2d}  {r['qualified_name']}")
            print(f"        {issue_str}")
        print()
    return True


def _handle_largest_fns(args, db):
    """处理 largest-fns 子命令（最大函数列表）

    等价 flag: --largest-fns
    """
    parser = argparse.ArgumentParser(
        prog="cw largest-fns",
        description=t("cli.messages.largest_fns_subcommand_desc",
                      default="Show largest functions"),
    )
    parser.add_argument("limit", type=int, nargs="?", default=20, help=t(
        "cli.messages.largest_fns_arg_limit", default="Max results (default 20)"))
    opts = parser.parse_args(args)

    fns = db.get_largest_functions(limit=opts.limit)
    print(t("cli.messages.largest_fns_title", count=len(fns)))
    print()
    lines_h = t("cli.messages.col_lines", default="Lines")
    depth_h = t("cli.messages.col_depth", default="Depth")
    fn_h = t("cli.messages.col_function", default="Function")
    print(f"  {'#':>3}  {lines_h:>5}  {depth_h:>4}  {fn_h}")
    print(f"  {'-'*3}  {'-'*5}  {'-'*4}  {'-'*50}")
    for i, fn in enumerate(fns, 1):
        print(
            f"  {i:3d}  {fn['line_count']:>5}  {fn['depth']:>4}  {fn['qualified_name'][:60]}")
        print(f"        {fn['file_path']}:{fn['start_line']}")
    return True


def _handle_coupled_fns(args, db):
    """处理 coupled-fns 子命令（耦合度最高的函数）

    等价 flag: --coupled-fns
    """
    parser = argparse.ArgumentParser(
        prog="cw coupled-fns",
        description=t("cli.messages.coupled_fns_subcommand_desc",
                      default="Show most coupled functions"),
    )
    parser.add_argument("limit", type=int, nargs="?", default=20, help=t(
        "cli.messages.coupled_fns_arg_limit", default="Max results (default 20)"))
    opts = parser.parse_args(args)

    fns = db.get_most_coupled_functions(limit=opts.limit)
    print(t("cli.messages.coupled_fns_title", count=len(fns)))
    print()
    fan_in_h = t("cli.messages.col_fan_in", default="Fan-in")
    fan_out_h = t("cli.messages.col_fan_out", default="Fan-out")
    total_h = t("cli.messages.col_total", default="Total")
    fn_h = t("cli.messages.col_function", default="Function")
    print(f"  {'#':>3}  {fan_in_h:>4}  {fan_out_h:>4}  {total_h:>4}  {fn_h}")
    print(f"  {'-'*3}  {'-'*4}  {'-'*4}  {'-'*4}  {'-'*50}")
    for i, fn in enumerate(fns, 1):
        print(
            f"  {i:3d}  {fn['fan_in']:>4}  {fn['fan_out']:>4}  {fn['total_coupling']:>4}  {fn['qualified_name'][:60]}")
        print(f"        {fn['file_path']}")
    return True


def _handle_fn_metrics(args, db):
    """处理 fn-metrics 子命令（单函数度量）

    等价 flag: --fn-metrics
    """
    parser = argparse.ArgumentParser(
        prog="cw fn-metrics",
        description=t("cli.messages.fn_metrics_subcommand_desc",
                      default="Show function metrics"),
    )
    parser.add_argument("name", help=t(
        "cli.messages.fn_metrics_arg_name", default="Function name"))
    opts = parser.parse_args(args)

    metrics = db.get_function_metrics(opts.name)
    if not metrics:
        print(t("cli.messages.fn_metrics_not_found", name=opts.name))
        print(t("cli.messages.fn_metrics_search_hint"))
    else:
        print(t("cli.messages.fn_metrics_title",
              name=metrics['qualified_name']))
        print(t("cli.messages.fn_metrics_kind", kind=metrics['kind']))
        print(t("cli.messages.fn_metrics_file",
              file=metrics['file_path'], start=metrics['start_line'], end=metrics['end_line']))
        print(t("cli.messages.fn_metrics_lines", count=metrics['line_count']))
        print(t("cli.messages.fn_metrics_complexity",
              value=metrics['cyclomatic_complexity'], risk=metrics['risk_level']))
        print(t("cli.messages.fn_metrics_fan_in", count=metrics['fan_in']))
        print(t("cli.messages.fn_metrics_fan_out", count=metrics['fan_out']))
        print(t("cli.messages.fn_metrics_depth", depth=metrics['depth']))
        print(t("cli.messages.fn_metrics_module",
              module=metrics['module_path']))
        if metrics['signature']:
            print(t("cli.messages.fn_metrics_signature",
                  sig=metrics['signature'][:100]))
    return True


# --------------------------------------------------------------------
# [8] Git Integration
# --------------------------------------------------------------------


def _handle_git(args, db):
    """处理 git 子命令（Git 集成）

    等价 flag: --git-import / --git-log / --git-show / --git-stats
    """
    parser = argparse.ArgumentParser(
        prog="cw git",
        description=t("cli.messages.git_subcommand_desc",
                      default="Git integration (import/log/show/stats)"),
    )
    sub = parser.add_subparsers(dest="action", required=True)

    imp = sub.add_parser("import", help=t(
        "cli.messages.git_action_import", default="Import git history"))
    imp.add_argument("limit", type=int, nargs="?", default=100, help=t(
        "cli.messages.git_arg_limit", default="Max commits (default 100)"))

    log_p = sub.add_parser("log", help=t(
        "cli.messages.git_action_log", default="Show git log"))
    log_p.add_argument("limit", type=int, nargs="?", default=20, help=t(
        "cli.messages.git_arg_log_limit", default="Max commits (default 20)"))

    show_p = sub.add_parser("show", help=t(
        "cli.messages.git_action_show", default="Show commit details"))
    show_p.add_argument("commit", help=t(
        "cli.messages.git_arg_commit", default="Commit hash"))

    sub.add_parser("stats", help=t("cli.messages.git_action_stats",
                   default="Show git integration stats"))

    # L3: pre-commit hook 调用 — 检查 active_task（软门禁）
    sub.add_parser("check-task",
                   help=t("cli.messages.git_action_check_task",
                          default="Check active task before commit (soft guardrail)"))

    # L2: pre-push hook 调用 — 检测 force push
    push_p = sub.add_parser("check-push",
                            help=t("cli.messages.git_action_check_push",
                                   default="Detect force push (soft guardrail, log only)"))
    push_p.add_argument("local_ref")
    push_p.add_argument("local_sha")
    push_p.add_argument("remote_ref")
    push_p.add_argument("remote_sha")

    # L2: reference-transaction hook 调用 — 审计 ref 变更（reset_hard / branch -f / force push）
    # 仅记录到 destructive_operations 表，不能拦截 working tree 破坏
    reftx_p = sub.add_parser("check-ref-transaction",
                             help=t("cli.messages.git_action_check_ref_transaction",
                                    default="Audit ref updates (reset_hard/branch -f/force push, soft guardrail, log only)"))
    reftx_p.add_argument("old_value")
    reftx_p.add_argument("new_value")
    reftx_p.add_argument("ref_name")
    reftx_p.add_argument("flags", nargs="?", default="",
                         help=t("cli.messages.git_arg_ref_flags",
                                default="ref-transaction flags (e.g. 'forced')"))

    # L2: 查询破坏性操作历史
    dlog_p = sub.add_parser("destructive-log",
                            help=t("cli.messages.git_action_destructive_log",
                                   default="Show destructive git operations log"))
    dlog_p.add_argument("limit", type=int, nargs="?", default=20,
                        help=t("cli.messages.git_arg_limit", default="Max results (default 20)"))
    dlog_p.add_argument("--type", default="",
                        help=t("cli.messages.git_arg_op_type",
                               default="Filter by operation type (force_push/reset_hard/checkout_clean)"))

    opts = parser.parse_args(args)

    if opts.action == "import":
        print(t("cli.messages.git_import_start", count=opts.limit))
        result = db.import_git_history(max_commits=opts.limit)
        if result.get("success"):
            print(t("cli.messages.git_import_success",
                  count=result['commits_imported']))
            print(t("cli.messages.git_import_total",
                  count=result['total_commits']))
        else:
            print(t("cli.messages.git_import_fail", error=result.get(
                'error', t("cli.messages.semgrep_unknown_error"))))
        return True

    if opts.action == "log":
        commits = db.get_git_commits(limit=opts.limit)
        print(t("cli.messages.git_log_title", count=len(commits)))
        print()
        for c in commits:
            short_hash = c['commit_hash'][:8]
            timestamp = time.strftime(
                '%Y-%m-%d %H:%M', time.localtime(c['timestamp']))
            msg = c['message'][:60] if c['message'] else t(
                "cli.messages.git_log_no_msg")
            author = c['author'][:15] if c['author'] else 'unknown'
            print(f"  {short_hash}  {timestamp}  {author:<15s}  {msg}")
        return True

    if opts.action == "show":
        details = db.get_commit_changes(opts.commit)
        commit = details.get("commit")
        if not commit:
            print(t("cli.messages.git_show_not_found", hash=opts.commit))
        else:
            print(t("cli.messages.git_show_commit",
                  hash=commit['commit_hash']))
            print(t("cli.messages.git_show_author",
                  author=commit['author'], email=commit['email']))
            print(t("cli.messages.git_show_time", time=time.strftime(
                '%Y-%m-%d %H:%M:%S', time.localtime(commit['timestamp']))))
            print(t("cli.messages.git_show_message", msg=commit['message']))
            print()
            file_changes = details.get("file_changes", [])
            print(t("cli.messages.git_show_files", count=len(file_changes)))
            type_map = {'A': t("cli.messages.git_type_added"), 'M': t("cli.messages.git_type_modified"),
                        'D': t("cli.messages.git_type_deleted"), 'R': t("cli.messages.git_type_renamed")}
            for fc in file_changes:
                ct = fc.get('change_type', '?')
                type_label = type_map.get(ct, ct)
                path = fc.get('rel_path') or fc.get('abs_path') or 'unknown'
                print(f"  [{type_label}] {path}")
            # 三角关联段：commit → task
            try:
                related_tasks = db.get_commit_tasks(
                    commit["commit_hash"]) if hasattr(db, "get_commit_tasks") else []
            except Exception:
                related_tasks = []
            if related_tasks:
                print()
                cprint(t("cli.messages.git_show_tasks_title",
                       default="── Related Tasks ──"), "cyan")
                print(t("cli.messages.git_show_tasks_count", default="Tasks ({}):".format(
                    len(related_tasks)), count=len(related_tasks)))
                for rt in related_tasks:
                    tid = rt.get("task_id", "")
                    title = rt.get("task_title") or ""
                    cnt = rt.get("change_count", 0)
                    print("  {} {} [{} change{}]".format(
                        tid, title, cnt, "s" if cnt != 1 else ""))
        return True

    if opts.action == "stats":
        stats = db.get_git_stats()
        print(t("cli.messages.git_stats_title"))
        print(t("cli.messages.git_stats_commits", count=stats['commit_count']))
        print(t("cli.messages.git_stats_file_changes",
              count=stats['file_change_count']))
        print()
        if stats.get("change_types"):
            print(t("cli.messages.git_stats_by_type"))
            type_map = {'A': t("cli.messages.git_type_added"), 'M': t("cli.messages.git_type_modified"),
                        'D': t("cli.messages.git_type_deleted"), 'R': t("cli.messages.git_type_renamed")}
            for ct, cnt in sorted(stats["change_types"].items(), key=lambda x: x[1], reverse=True):
                label = type_map.get(ct, ct)
                print(t("cli.messages.git_stats_type_count",
                      default="    {label}: {count} times", label=label, count=cnt))
        return True

    # L3: pre-commit hook 调用 — 检查 active_task（软门禁）
    if opts.action == "check-task":
        try:
            active_task_id = db.get_active_task() if hasattr(db, "get_active_task") else None
        except Exception:
            active_task_id = None

        if active_task_id:
            # 查询 task 详情（title/status）
            task_title = ""
            task_status = ""
            try:
                row = db.conn.execute(
                    "SELECT title, status FROM tasks WHERE id = ?",
                    (active_task_id,),
                ).fetchone()
                if row:
                    task_title = row["title"] or ""
                    task_status = row["status"] or ""
            except Exception:
                pass
            print(t("cli.messages.git_check_task_active",
                    default="[Call Warden] Active task: {task_id} ({title}) [{status}]",
                    task_id=active_task_id, title=task_title, status=task_status))
        else:
            # 软门禁：无 active task 时仅警告，不阻止 commit
            print(t("cli.messages.git_check_task_none",
                    default="[Call Warden] Warning: no active task. "
                            "Consider 'cw task next' to claim a task before committing."))
        return True

    # L2: pre-push hook 调用 — 检测 force push
    if opts.action == "check-push":
        local_ref = opts.local_ref
        local_sha = opts.local_sha
        remote_ref = opts.remote_ref
        remote_sha = opts.remote_sha

        # 检测是否为 force push
        is_force = False
        try:
            is_force = db.check_force_push(local_sha, remote_sha)
        except Exception:
            pass

        if is_force:
            # 获取当前 active task（用于关联）
            task_id = ""
            try:
                task_id = db.get_active_task() or ""
            except Exception:
                pass

            # 记录到 destructive_operations 表
            try:
                db.log_destructive_operation(
                    operation_type="force_push",
                    local_ref=local_ref,
                    local_sha=local_sha,
                    remote_ref=remote_ref,
                    remote_sha=remote_sha,
                    task_id=task_id,
                    message=f"Force push: {local_ref} -> {remote_ref}",
                )
            except Exception:
                pass

            # 软门禁：警告不阻止
            print(t("cli.messages.git_check_push_force",
                    default="[Call Warden] Warning: force push detected "
                            "({local_ref} -> {remote_ref}). "
                            "Operation logged but not blocked (soft guardrail).",
                    local_ref=local_ref, remote_ref=remote_ref))
        return True

    # L2: reference-transaction hook 调用 — 审计 ref 变更
    # git 无 pre-checkout/pre-reset hook；reset --hard 的 working tree 写入
    # 先于 ref 更新，故此 hook 仅作审计层，不拦截
    if opts.action == "check-ref-transaction":
        old_value = opts.old_value
        new_value = opts.new_value
        ref_name = opts.ref_name
        flags = opts.flags or ""

        # 识别破坏性 ref 变更：
        # - flags 包含 "forced"：reset --hard / branch -f / push --force-with-lease
        # - new_value 全 0（40 位）：分支删除
        # - old_value 全 0（40 位）：分支新建（非破坏性，仅记录）
        # - 其余为常规 fast-forward / commit：忽略（避免噪音）
        # 空字符串/None 视为无效输入跳过（git 不会传空 sha，但 hook 解析
        # 可能产生空值，避免误分类为 branch_delete）
        def is_zero_sha(sha): return bool(sha) and len(
            sha) == 40 and set(sha) == {"0"}
        is_destructive = "forced" in flags.lower() or is_zero_sha(new_value)
        is_create = is_zero_sha(old_value) and not is_zero_sha(new_value)

        # 常规 fast-forward 不记录（避免日志噪音）
        if not is_destructive and not is_create:
            return True

        # 映射到 destructive_operations.operation_type
        if is_zero_sha(new_value):
            op_type = "branch_delete"
        elif "forced" in flags.lower():
            op_type = "reset_hard"
        else:
            op_type = "branch_create"

        # 获取当前 active task（用于关联）
        task_id = ""
        try:
            task_id = db.get_active_task() or ""
        except Exception:
            pass

        # 记录到 destructive_operations 表（软门禁，不阻止）
        try:
            db.log_destructive_operation(
                operation_type=op_type,
                local_ref=ref_name,
                local_sha=old_value,
                remote_ref="",
                remote_sha=new_value,
                task_id=task_id,
                message=f"ref-transaction: {ref_name} {old_value[:8]} -> {new_value[:8]} flags={flags!r}",
            )
        except Exception:
            pass

        # 软门禁：警告不阻止
        if is_destructive:
            print(t("cli.messages.git_check_ref_tx_destructive",
                    default="[Call Warden] Warning: destructive ref update detected "
                            "({ref_name}: {op_type}). "
                            "Operation logged but not blocked (audit-only).",
                    ref_name=ref_name, op_type=op_type))
        return True

    # L2: 查询破坏性操作历史
    if opts.action == "destructive-log":
        ops = db.list_destructive_operations(
            limit=opts.limit, operation_type=opts.type)
        if not ops:
            print(t("cli.messages.git_destructive_log_empty",
                    default="No destructive operations recorded."))
            return True

        print(t("cli.messages.git_destructive_log_title",
                default="Destructive Operations Log ({count} records)",
                count=len(ops)))
        print()
        for op in ops:
            timestamp = time.strftime(
                '%Y-%m-%d %H:%M:%S', time.localtime(op['created_at']))
            op_type = op['operation_type']
            task_id = op.get('task_id', '')
            msg = op.get('message', '')
            short_local = (op.get('local_sha', '') or '')[:8]
            short_remote = (op.get('remote_sha', '') or '')[:8]
            task_label = f" task={task_id}" if task_id else ""
            print(
                f"  [{timestamp}] {op_type} {short_local} -> {short_remote}{task_label}")
            if msg:
                print(f"    {msg}")
        return True

    return True


# --------------------------------------------------------------------
# [9] Semgrep & Defects
# --------------------------------------------------------------------


def _handle_semgrep(args, db):
    """处理 semgrep 子命令（Semgrep 静态分析）

    等价 flag: --semgrep / --semgrep-list / --semgrep-stats
    """
    parser = argparse.ArgumentParser(
        prog="cw semgrep",
        description=t("cli.messages.semgrep_subcommand_desc",
                      default="Semgrep static analysis (scan/list/stats)"),
    )
    sub = parser.add_subparsers(dest="action", required=True)

    scan_p = sub.add_parser("scan", help=t(
        "cli.messages.semgrep_action_scan", default="Run Semgrep scan"))
    scan_p.add_argument("paths", nargs="*", help=t("cli.messages.semgrep_arg_paths",
                        default="Target paths (empty for workspace root)"))
    scan_p.add_argument("--config", default="p/default", help=t(
        "cli.messages.semgrep_arg_config", default="Semgrep config (default p/default)"))
    scan_p.add_argument("--lang", dest="languages", nargs="*",
                        help=t("cli.messages.semgrep_arg_lang", default="Limit to languages"))
    scan_p.add_argument("--timeout", type=int, default=180, help=t(
        "cli.messages.semgrep_arg_timeout", default="Timeout seconds (default 180)"))
    scan_p.add_argument("--save", action="store_true", help=t(
        "cli.messages.semgrep_arg_save", default="Save findings to database"))
    scan_p.add_argument("--quick", action="store_true",
                        help=t("cli.messages.semgrep_arg_quick", default="Quick summary scan"))
    # A14（2026-07-20）：增量扫描模式 — 只扫 git diff 变更文件并清理旧 findings
    scan_p.add_argument("--incremental", action="store_true",
                        help=t("cli.messages.semgrep_arg_incremental",
                               default="Incremental scan: only scan git diff changed files and clean stale findings"))
    scan_p.add_argument("--base", dest="base_branch", default="main",
                        help=t("cli.messages.semgrep_arg_base_branch",
                               default="Base branch for incremental scan (default main)"))
    scan_p.add_argument("--head", default="HEAD",
                        help=t("cli.messages.semgrep_arg_head",
                               default="Head ref for incremental scan (default HEAD)"))

    list_p = sub.add_parser("list", help=t(
        "cli.messages.semgrep_action_list", default="List saved findings"))
    list_p.add_argument("filter", nargs="?", default="", help=t(
        "cli.messages.semgrep_arg_filter", default="Rule id filter"))
    list_p.add_argument("--severity", default=None, help=t(
        "cli.messages.semgrep_arg_severity", default="Filter by severity"))
    list_p.add_argument("--lang", dest="language", default=None, help=t(
        "cli.messages.semgrep_arg_list_lang", default="Filter by language"))
    list_p.add_argument("--limit", type=int, default=50, help=t(
        "cli.messages.semgrep_arg_list_limit", default="Max results (default 50)"))

    sub.add_parser("stats", help=t(
        "cli.messages.semgrep_action_stats", default="Show Semgrep stats"))

    opts = parser.parse_args(args)

    if opts.action == "scan":
        target_paths = opts.paths if opts.paths else None
        print(t("cli.messages.semgrep_title"))
        print(t("cli.messages.semgrep_config_label", config=opts.config))
        if opts.languages:
            print(t("cli.messages.semgrep_lang_limit",
                  langs=", ".join(opts.languages)))
        print(t("cli.messages.semgrep_timeout_label", timeout=opts.timeout))
        # A14: 增量扫描模式提示
        if opts.incremental:
            print(t("cli.messages.semgrep_incremental_mode",
                    base=opts.base_branch, head=opts.head))
        print()
        # A14: 增量扫描分支（优先于 --save / --quick）
        if opts.incremental:
            result = db.scan_semgrep_incremental(
                base_branch=opts.base_branch,
                head=opts.head,
                config=opts.config,
                languages=opts.languages,
                timeout=opts.timeout,
            )
            if not result.get("success"):
                print(t("cli.messages.semgrep_error",
                        error=result.get('error', t("cli.messages.semgrep_unknown_error"))))
            else:
                print(t("cli.messages.semgrep_incremental_done",
                        changed=result['changed_files'],
                        scanned=result['scanned_files'],
                        saved=result['saved_findings'],
                        total=result['total_findings'],
                        stale=result['stale_file_ids']))
        elif opts.save:
            result = db.run_semgrep_and_save(
                target_paths=target_paths or [db.workspace_root],
                config=opts.config,
                languages=opts.languages,
                timeout=opts.timeout,
            )
            if not result.get("success"):
                print(t("cli.messages.semgrep_error", error=result.get(
                    'error', t("cli.messages.semgrep_unknown_error"))))
            else:
                print(t("cli.messages.semgrep_scan_done",
                      count=result['total_findings']))
                print(t("cli.messages.semgrep_saved",
                      count=result['saved_findings']))
                print()
                print(t("cli.messages.semgrep_save_hint"))
        elif opts.quick:
            result = db.get_semgrep_summary(target_paths)
            if not result.get("success"):
                print(t("cli.messages.semgrep_error", error=result.get(
                    'error', t("cli.messages.semgrep_unknown_error"))))
            else:
                print(t("cli.messages.semgrep_quick_total",
                      count=result['total_findings']))
                print()
                if result.get("by_severity"):
                    print(t("cli.messages.semgrep_severity_dist"))
                    for sev in ["ERROR", "WARNING", "INFO"]:
                        count = result["by_severity"].get(sev, 0)
                        if count > 0:
                            icon = {
                                "ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}[sev]
                            print(t("cli.messages.semgrep_severity_count",
                                  icon=icon, sev=sev, count=count))
                    print()
                if result.get("by_language"):
                    print(t("cli.messages.semgrep_lang_dist"))
                    for lang, count in sorted(result["by_language"].items(), key=lambda x: x[1], reverse=True):
                        print(t("cli.messages.semgrep_lang_count",
                              lang=lang, count=count))
                    print()
                if result.get("top_rules"):
                    print(t("cli.messages.semgrep_top_rules"))
                    for rule_id, stats in result["top_rules"][:10]:
                        sev_icon = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}[
                            stats.get("severity", "INFO")]
                        print(t("cli.messages.semgrep_rule_item",
                              icon=sev_icon, rule=rule_id, count=stats['count']))
                        print(f"        {stats['message'][:80]}...")
                    print()
            if result.get("errors"):
                print(t("cli.messages.semgrep_warning_count",
                      count=len(result['errors'])))
        else:
            result = db.run_semgrep(
                target_paths=target_paths or [db.workspace_root],
                config=opts.config,
                languages=opts.languages,
                timeout=opts.timeout,
            )
            if not result.get("success"):
                print(t("cli.messages.semgrep_error", error=result.get(
                    'error', t("cli.messages.semgrep_unknown_error"))))
            else:
                print(t("cli.messages.semgrep_scan_done",
                      count=result['total_findings']))
                print()
                severity_icon = {"ERROR": "[!]",
                                 "WARNING": "[~]", "INFO": "[i]"}
                for sev in ["ERROR", "WARNING", "INFO"]:
                    sev_findings = [f for f in result["results"]
                                    if f["severity"] == sev]
                    if sev_findings:
                        icon = severity_icon[sev]
                        if sev == "ERROR":
                            sev_label = t("cli.messages.semgrep_sev_error")
                        elif sev == "WARNING":
                            sev_label = t("cli.messages.semgrep_sev_warning")
                        else:
                            sev_label = t("cli.messages.semgrep_sev_info")
                        print(t("cli.messages.semgrep_detail_title",
                              label=sev_label, count=len(sev_findings)))
                        print()
                        for f in sev_findings[:15]:
                            print(f"    {icon} {f['rule_name']}")
                            print(t("cli.messages.semgrep_finding_file",
                                  file=f['path'], line=f['start_line']))
                            print(
                                t("cli.messages.semgrep_finding_lang", lang=f['language']))
                            print(t("cli.messages.semgrep_finding_msg",
                                  msg=f['message'][:100]))
                            if f.get("fix"):
                                print(
                                    t("cli.messages.semgrep_fix_hint", fix=f['fix'][:50]))
                            print()
                        if len(sev_findings) > 15:
                            print(t("cli.messages.semgrep_more",
                                  count=len(sev_findings) - 15))
                            print()
        print(t("cli.messages.semgrep_hint"))
        print()
        return True

    if opts.action == "stats":
        stats = db.get_semgrep_stats()
        print(t("cli.messages.semgrep_stats_title"))
        print(t("cli.messages.semgrep_stats_total",
              count=stats['total_findings']))
        print()
        if stats["by_severity"]:
            print(t("cli.messages.semgrep_stats_by_sev"))
            for sev in ["ERROR", "WARNING", "INFO"]:
                count = stats["by_severity"].get(sev, 0)
                if count > 0:
                    icon = {"ERROR": "[!]",
                            "WARNING": "[~]", "INFO": "[i]"}[sev]
                    print(t("cli.messages.semgrep_severity_count",
                          icon=icon, sev=sev, count=count))
            print()
        if stats["by_language"]:
            print(t("cli.messages.semgrep_stats_by_lang"))
            for lang, count in sorted(stats["by_language"].items(), key=lambda x: x[1], reverse=True):
                print(f"    {lang:<15s} {count:4d}")
            print()
        if stats["by_rule"]:
            print(t("cli.messages.semgrep_stats_top_rules"))
            for i, rule in enumerate(stats["by_rule"][:10], 1):
                sev_icon = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}.get(
                    rule["severity"], "[?]")
                print(
                    f"    #{i:2d} {sev_icon} {rule['rule_id'][:50]:<50s}  {rule['cnt']:3d}")
            print()
        if stats["by_symbol"]:
            print(t("cli.messages.semgrep_stats_top_symbols"))
            for i, sym in enumerate(stats["by_symbol"][:10], 1):
                print(
                    f"    #{i:2d} {sym['symbol_qualified'][:60]:<60s}  {sym['cnt']:2d}")
            print()
        return True

    if opts.action == "list":
        rule_filter = opts.filter if opts.filter else ""
        severity = opts.severity or ""
        language = opts.language or ""
        findings = db.get_semgrep_findings(
            severity=severity,
            language=language,
            rule_id=rule_filter,
            limit=opts.limit,
        )
        filter_parts = []
        if severity:
            filter_parts.append(
                t("cli.messages.semgrep_list_filter_sev", sev=severity))
        if language:
            filter_parts.append(
                t("cli.messages.semgrep_list_filter_lang", lang=language))
        if rule_filter:
            filter_parts.append(
                t("cli.messages.semgrep_list_filter_rule", rule=rule_filter))
        filter_str = " | ".join(filter_parts) if filter_parts else t(
            "cli.messages.semgrep_list_filter_all")
        print(t("cli.messages.semgrep_list_title", filter=filter_str,
              total=len(findings), shown=min(opts.limit, len(findings))))
        print()
        sev_icon_map = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}
        for i, f in enumerate(findings[:opts.limit], 1):
            icon = sev_icon_map.get(f["severity"], "[?]")
            sym_info = f" -> {f['symbol_qualified']}" if f["symbol_qualified"] else ""
            print(
                f"  #{i:3d} {icon} {f['rule_name'][:40]:<40s} {f['language']:<12s}{sym_info}")
            print(f"        {f['file_path']}:{f['start_line']}")
            print(f"        {f['message'][:80]}")
            print()
        if len(findings) > opts.limit:
            print(t("cli.messages.semgrep_list_more",
                  count=len(findings) - opts.limit))
        return True

    return True


# --------------------------------------------------------------------
# [10] Coverage & Ownership
# --------------------------------------------------------------------


def _handle_coverage(args, db):
    """处理 coverage 子命令（覆盖率导入与查询）

    等价 flag: --coverage-import / --coverage-fn / --coverage-uncovered
    """
    parser = argparse.ArgumentParser(
        prog="cw coverage",
        description=t("cli.messages.coverage_subcommand_desc",
                      default="Coverage import and query (import/fn/uncovered)"),
    )
    sub = parser.add_subparsers(dest="action", required=True)

    imp = sub.add_parser("import", help=t(
        "cli.messages.coverage_action_import", default="Import coverage report"))
    imp.add_argument("file", help=t(
        "cli.messages.coverage_arg_file", default="Coverage report file"))
    imp.add_argument("--format", choices=["lcov", "cobertura"], default="lcov", help=t(
        "cli.messages.coverage_arg_format", default="Report format (default lcov)"))

    fn_p = sub.add_parser("fn", help=t(
        "cli.messages.coverage_action_fn", default="Coverage for a function"))
    fn_p.add_argument("name", help=t(
        "cli.messages.coverage_arg_name", default="Function name"))

    sub.add_parser("uncovered", help=t(
        "cli.messages.coverage_action_uncovered", default="Find uncovered functions"))

    opts = parser.parse_args(args)

    if opts.action == "import":
        print(t("cli.messages.coverage_import_title",
              file=opts.file, format=opts.format))
        print("-" * 50)
        try:
            if opts.format == "lcov":
                stats = db.import_lcov(opts.file)
            else:
                stats = db.import_cobertura(opts.file)
            print(t("cli.messages.coverage_import_files_total",
                  count=stats['files_total']))
            print(t("cli.messages.coverage_import_files_matched",
                  count=stats['files_matched']))
            print(t("cli.messages.coverage_import_lines",
                  count=stats['lines_imported']))
            print(t("cli.messages.coverage_import_symbols",
                  count=stats['symbols_matched']))
        except FileNotFoundError:
            print(t("cli.messages.coverage_import_file_not_found", file=opts.file))
        except Exception as e:
            print(t("cli.messages.coverage_import_parse_error", error=e))
        print()
        return True

    if opts.action == "fn":
        info = db.get_coverage_for_symbol(opts.name)
        if not info:
            print(t("cli.messages.coverage_fn_not_found", name=opts.name))
            print(t("cli.messages.coverage_fn_search_hint"))
        else:
            print(t("cli.messages.coverage_fn_title",
                  name=info['qualified_name']))
            print("-" * 50)
            print(t("cli.messages.coverage_fn_file",
                  file=info['file_path'], start=info['start_line'], end=info['end_line']))
            print(t("cli.messages.coverage_fn_total",
                  count=info['total_lines']))
            print(t("cli.messages.coverage_fn_tracked",
                  count=info['tracked_lines']))
            print(t("cli.messages.coverage_fn_covered",
                  count=info['covered_lines']))
            print(t("cli.messages.coverage_fn_pct", pct=info['coverage_pct']))
            if info['uncovered_lines']:
                lines_preview = info['uncovered_lines'][:30]
                more = '...' if len(info['uncovered_lines']) > 30 else ''
                print(t("cli.messages.coverage_fn_uncovered",
                      lines=lines_preview, more=more))
        print()
        return True

    if opts.action == "uncovered":
        results = db.find_uncovered_functions()
        print(t("cli.messages.coverage_uncovered_title", count=len(results)))
        print("-" * 50)
        for i, r in enumerate(results, 1):
            pct_label = t("cli.messages.coverage_fn_pct",
                          pct="").strip().rstrip(":").strip()
            print(
                f"  [{i:3d}] {pct_label}={r['coverage_pct']:5.1f}%  {r['qualified_name']}")
            print(t("cli.messages.coverage_uncovered_item",
                  file=r['file_path'], start=r['start_line'], end=r['end_line'], covered=r['covered_lines'], tracked=r['tracked_lines']))
        print()
        return True

    return True


def _handle_who(args, db):
    """处理 who 子命令（文件负责人）

    等价 flag: --who
    """
    parser = argparse.ArgumentParser(
        prog="cw who",
        description=t("cli.messages.who_subcommand_desc",
                      default="Show file owner (who to ask)"),
    )
    parser.add_argument("file", help=t(
        "cli.messages.who_arg_file", default="File path"))
    opts = parser.parse_args(args)

    info = db.who_to_ask(opts.file)
    if not info:
        print(t("cli.messages.who_not_found", file=opts.file))
        print(t("cli.messages.who_hint"))
    else:
        print(t("cli.messages.who_title"))
        print("-" * 50)
        print(t("cli.messages.who_file", file=info['file_path']))
        print(t("cli.messages.who_owner", owner=info['owner']))
        print(t("cli.messages.who_source", source=info['source']))
        print(t("cli.messages.who_confidence", confidence=info['confidence']))
        if info.get('last_commit_author'):
            print(t("cli.messages.who_last_author",
                  author=info['last_commit_author']))
        if info.get('last_commit_time'):
            ts = time.strftime('%Y-%m-%d %H:%M:%S',
                               time.localtime(info['last_commit_time']))
            print(t("cli.messages.who_last_time", time=ts))
        if info.get('last_commit_hash'):
            print(t("cli.messages.who_last_hash",
                  hash=info['last_commit_hash'][:12]))
    print()
    return True


def _handle_ownership_map(args, db):
    """处理 ownership-map 子命令（所有权映射）

    等价 flag: --ownership-map
    """
    parser = argparse.ArgumentParser(
        prog="cw ownership-map",
        description=t("cli.messages.ownership_map_subcommand_desc",
                      default="Show ownership map"),
    )
    parser.parse_args(args)
    results = db.get_ownership_map()
    print(t("cli.messages.ownership_map_title", count=len(results)))
    print("-" * 50)
    for i, m in enumerate(results, 1):
        print(f"  [{i}] {m['module']}")
        print(t("cli.messages.ownership_map_primary",
              owner=m['primary_owner'], count=m['file_count']))
        owners_str = ", ".join(
            f"{o['name']}({o['file_count']})" for o in m['owners'][:5])
        print(t("cli.messages.ownership_map_dist", owners=owners_str))
        if len(m['owners']) > 5:
            print(t("cli.messages.ownership_map_more",
                  count=len(m['owners']) - 5))
    print()
    return True


# --------------------------------------------------------------------
# [12] Diagnostics
# --------------------------------------------------------------------


def _handle_brief(args, db):
    """处理 brief 子命令（项目简报）

    等价 flag: --brief
    """
    parser = argparse.ArgumentParser(
        prog="cw brief",
        description=t("cli.messages.brief_subcommand_desc",
                      default="Show project brief"),
    )
    parser.parse_args(args)
    brief = db.project_brief()
    print(t("cli.messages.brief_title"))
    print()
    print(t("cli.messages.brief_project_type", type=brief['project_type']))
    print(t("cli.messages.brief_files", count=brief['file_count']))
    print(t("cli.messages.brief_functions", count=brief['function_count']))
    print(t("cli.messages.brief_total_lines", count=brief['total_lines']))
    print(t("cli.messages.brief_health",
          score=brief['health_score'], level=brief['health_level']))
    print(t("cli.messages.brief_avg_complexity",
          value=brief['avg_complexity']))
    print(t("cli.messages.brief_comment_coverage",
          pct=brief['comment_coverage']))
    print()
    modules = brief.get('modules', [])
    if modules:
        print(t("cli.messages.brief_modules", count=len(modules)))
        for i, m in enumerate(modules, 1):
            print(t("cli.messages.brief_module_item", idx=i,
                  module=m['module'], count=m['function_count']))
        print()
    hotspots = brief.get('hot_functions', [])
    if hotspots:
        print(t("cli.messages.brief_hotspots", count=len(hotspots)))
        for i, fn in enumerate(hotspots, 1):
            print(t("cli.messages.brief_hotspot_item", idx=i,
                  value=fn['cyclomatic_complexity'], name=fn['qualified_name']))
    print()
    return True


def _handle_map(args, db):
    """处理 map 子命令（仓库模块依赖图）

    等价 flag: --map
    """
    parser = argparse.ArgumentParser(
        prog="cw map",
        description=t("cli.messages.map_subcommand_desc",
                      default="Show repo module map"),
    )
    parser.add_argument("--format", choices=["text", "mermaid"], default="text", help=t(
        "cli.messages.map_arg_format", default="Output format (default text)"))
    opts = parser.parse_args(args)

    output = db.repo_map(format=opts.format)
    print(t("cli.messages.map_title", format=opts.format))
    print()
    print(output)
    print()
    return True


def _handle_toolchain(args, db):
    """处理 toolchain 子命令（工具链注册与管理）

    子命令：
        register <NAME> <COMPILER_PATH> [--sysroot SYSROOT] [--description DESC]
        list
        show <NAME_OR_ID>
        delete <NAME_OR_ID>
        bind <WORKSPACE_ID> <TOOLCHAIN_NAME> [--build-context-hash HASH]
    """
    from callwarden.db.db_toolchain import (
        init_toolchain_schema, register_toolchain, get_toolchain,
        list_toolchains, delete_toolchain, bind_toolchain_to_workspace,
        get_workspace_toolchains,
    )

    parser = argparse.ArgumentParser(
        prog="cw toolchain",
        description="Toolchain management (register/list/show/delete/bind)",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # register
    reg = sub.add_parser("register", help="Register a new toolchain")
    reg.add_argument("name", help="Toolchain name (unique)")
    reg.add_argument("compiler_path", help="Compiler executable path")
    reg.add_argument("--sysroot", default="", help="Sysroot path")
    reg.add_argument("--description", default="", help="Description")
    reg.add_argument("--no-probe", action="store_true", help="Skip auto-probe")

    # list
    sub.add_parser("list", help="List all toolchains")

    # show
    show_p = sub.add_parser("show", help="Show toolchain details")
    show_p.add_argument("name_or_id", help="Toolchain name or ID")

    # delete
    del_p = sub.add_parser("delete", help="Delete a toolchain")
    del_p.add_argument("name_or_id", help="Toolchain name or ID")

    # bind
    bind_p = sub.add_parser("bind", help="Bind toolchain to workspace")
    bind_p.add_argument("workspace_id", type=int, help="Workspace ID")
    bind_p.add_argument("toolchain_name", help="Toolchain name")
    bind_p.add_argument("--build-context-hash", default="",
                        help="Build context hash")

    # list-bound
    bound_p = sub.add_parser(
        "list-bound", help="List toolchains bound to a workspace")
    bound_p.add_argument("workspace_id", type=int, help="Workspace ID")
    bound_p.add_argument("--build-context-hash", default="",
                         help="Filter by build context hash")

    opts = parser.parse_args(args)

    # 初始化 schema（幂等）
    init_toolchain_schema(db.conn)

    if opts.action == "register":
        try:
            tc = register_toolchain(
                conn=db.conn,
                name=opts.name,
                compiler_path=opts.compiler_path,
                sysroot=opts.sysroot,
                description=opts.description,
                probe=not opts.no_probe,
            )
            print(f"Toolchain registered: {tc.summary()}")
            print(f"  fingerprint: {tc.fingerprint}")
            if tc.include_dirs:
                print(
                    f"  include_dirs ({len(tc.include_dirs)}): {', '.join(tc.include_dirs[:3])}...")
            if tc.predefined_macros:
                print(
                    f"  predefined_macros: {len(tc.predefined_macros)} macros")
        except Exception as e:
            print(f"Error: {e}")
            return False

    elif opts.action == "list":
        tcs = list_toolchains(db.conn)
        if not tcs:
            print("No toolchains registered.")
            return True
        print(f"{'ID':<5} {'Name':<20} {'Type':<20} {'Version':<30} {'Target':<25}")
        print("-" * 100)
        for tc in tcs:
            print(
                f"{tc.id:<5} {tc.name:<20} {tc.compiler_type:<20} {tc.version[:30]:<30} {tc.target_triple:<25}")

    elif opts.action == "show":
        # 尝试 ID 或名称
        try:
            name_or_id = int(opts.name_or_id)
        except ValueError:
            name_or_id = opts.name_or_id
        tc = get_toolchain(db.conn, name_or_id)
        if tc is None:
            print(f"Toolchain not found: {opts.name_or_id}")
            return False
        print(f"Toolchain: {tc.name}")
        print(f"  ID: {tc.id}")
        print(f"  Compiler: {tc.compiler_path}")
        print(f"  Type: {tc.compiler_type}")
        print(f"  Version: {tc.version}")
        print(f"  Target: {tc.target_triple}")
        print(f"  Sysroot: {tc.sysroot or '(none)'}")
        print(f"  Fingerprint: {tc.fingerprint}")
        print(f"  Include dirs ({len(tc.include_dirs)}):")
        for d in tc.include_dirs[:10]:
            print(f"    {d}")
        if len(tc.include_dirs) > 10:
            print(f"    ... and {len(tc.include_dirs) - 10} more")
        print(f"  Predefined macros: {len(tc.predefined_macros)}")
        print(f"  Description: {tc.description or '(none)'}")

    elif opts.action == "delete":
        try:
            name_or_id = int(opts.name_or_id)
        except ValueError:
            name_or_id = opts.name_or_id
        if delete_toolchain(db.conn, name_or_id):
            print(f"Toolchain deleted: {opts.name_or_id}")
        else:
            print(f"Toolchain not found: {opts.name_or_id}")
            return False

    elif opts.action == "bind":
        tc = get_toolchain(db.conn, opts.toolchain_name)
        if tc is None:
            print(f"Toolchain not found: {opts.toolchain_name}")
            return False
        if bind_toolchain_to_workspace(
            db.conn, opts.workspace_id, tc.id, opts.build_context_hash
        ):
            print(
                f"Toolchain '{tc.name}' bound to workspace {opts.workspace_id}")
        else:
            print(f"Failed to bind (already bound?)")
            return False

    elif opts.action == "list-bound":
        tcs = get_workspace_toolchains(
            db.conn, opts.workspace_id, opts.build_context_hash
        )
        if not tcs:
            print(f"No toolchains bound to workspace {opts.workspace_id}")
            return True
        for tc in tcs:
            print(f"  {tc.summary()}")

    return True


def _handle_rollback(args, db):
    """处理 rollback 子命令（迁移回滚配置管理）

    Phase 0 子任务 4：全量 Rust 迁移自举计划使用。
    每个功能子任务在 wire-production step 登记回滚配置。

    子命令：
        register --task-id <ID> --feature <NAME> --phase <N>
                 --production-entry <PATH> --rollback-entry <PATH>
                 [--window <ISO8601>] [--config-json <JSON>]
            注册一条 rollback_config 记录
        show <TASK_ID>
            查询单个任务的回滚配置
        config [--phase <N>] [--flag <0|1>]
            列出回滚配置
        set <TASK_ID> <0|1> [--reason "..."]
            设置回滚标志（0=正常 Rust，1=已回滚到 Python）
        is-rolled-back <FEATURE_NAME>
            查询功能是否已回滚（生产入口快速查询）
    """
    parser = argparse.ArgumentParser(
        prog="cw rollback",
        description=t("cli_rollback_desc",
                      default="Migration rollback config management"),
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # register：注册回滚配置
    reg_p = sub.add_parser("register", help=t(
        "cli_rollback_register_desc",
        default="Register rollback config for a migration feature"))
    reg_p.add_argument("--task-id", required=True, help="Migration task ID")
    reg_p.add_argument("--feature", required=True, help="Feature name")
    reg_p.add_argument("--phase", type=int, required=True, help="Phase number (0-7)")
    reg_p.add_argument("--production-entry", required=True,
                       help="Production entry path (e.g. db/db_base.py:CodeGraphDB._connect)")
    reg_p.add_argument("--rollback-entry", required=True,
                       help="Rollback entry path (Python fallback location)")
    reg_p.add_argument("--window", default="",
                       help="Rollback window until (ISO8601, e.g. 2026-12-31T00:00:00)")
    reg_p.add_argument("--config-json", default="",
                       help="Extra config JSON (e.g. {\"flag\":\"CW_USE_RUST\"})")

    # show：查询单个任务
    show_p = sub.add_parser("show", help=t(
        "cli_rollback_show_desc",
        default="Show rollback config for a task"))
    show_p.add_argument("task_id", help="Task ID")

    # config：列出所有配置
    list_p = sub.add_parser("config", help=t(
        "cli_rollback_config_desc",
        default="List rollback configs"))
    list_p.add_argument("--phase", type=int, default=0,
                        help="Filter by phase (0=all)")
    list_p.add_argument("--flag", type=int, default=-1, choices=[-1, 0, 1],
                        help="Filter by rollback flag (-1=all, 0=normal, 1=rolled-back)")

    # set：设置回滚标志
    set_p = sub.add_parser("set", help=t(
        "cli_rollback_set_desc",
        default="Set rollback flag (0=Rust, 1=rollback to Python)"))
    set_p.add_argument("task_id", help="Task ID")
    set_p.add_argument("flag", type=int, choices=[0, 1],
                       help="Rollback flag (0=normal Rust, 1=rolled back to Python)")
    set_p.add_argument("--reason", default="", help="Rollback reason")

    # is-rolled-back：快速查询
    check_p = sub.add_parser("is-rolled-back", help=t(
        "cli_rollback_is_rolled_back_desc",
        default="Check if a feature is rolled back"))
    check_p.add_argument("feature_name", help="Feature name")

    opts = parser.parse_args(args)

    if opts.action == "register":
        # 解析 config-json
        config_blob = None
        if opts.config_json:
            import json as _json
            try:
                config_blob = _json.loads(opts.config_json)
            except _json.JSONDecodeError as e:
                cprint(t("cli.messages.rollback_config_invalid_json",
                         error=e), "red")
                return True
        result = db.register_rollback_config(
            task_id=opts.task_id,
            feature_name=opts.feature,
            phase=opts.phase,
            production_entry=opts.production_entry,
            rollback_entry=opts.rollback_entry,
            rollback_window_until=opts.window,
            config_blob=config_blob,
        )
        if not result.get("success"):
            cprint(t("cli.messages.rollback_register_failed",
                     error=result.get("error", "unknown")), "red")
            return True
        action = result.get("action", "unknown")
        cprint(t("cli.messages.rollback_register_ok",
                 action=action, id=result.get("id", ""),
                 task_id=opts.task_id), "green")
        return True

    if opts.action == "show":
        config = db.get_rollback_config(opts.task_id)
        if not config:
            cprint(t("cli.messages.rollback_config_not_found",
                     task_id=opts.task_id), "yellow")
            return True
        print(f"Task ID:          {config['task_id']}")
        print(f"Feature:          {config['feature_name']}")
        print(f"Phase:            {config['phase']}")
        print(f"Production entry: {config['production_entry']}")
        print(f"Rollback entry:   {config['rollback_entry']}")
        flag_str = "ROLLED BACK (Python)" if config['rollback_flag'] == 1 else "Normal (Rust)"
        print(f"Rollback flag:    {config['rollback_flag']} ({flag_str})")
        if config.get('rollback_window_until'):
            print(f"Window until:     {config['rollback_window_until']}")
        if config.get('config_blob'):
            import json as _json
            print(f"Config blob:      {_json.dumps(config['config_blob'], ensure_ascii=False)}")
        return True

    if opts.action == "config":
        configs = db.list_rollback_configs(phase=opts.phase, rollback_flag=opts.flag)
        if not configs:
            print(t("cli.messages.rollback_config_empty",
                    default="No rollback configs found"))
            return True
        print(f"{'Task ID':<40} {'Phase':<6} {'Feature':<30} {'Flag':<5} {'Production Entry'}")
        print("-" * 120)
        for c in configs:
            flag_str = "ROLLBACK" if c['rollback_flag'] == 1 else "normal"
            print(f"{c['task_id']:<40} {c['phase']:<6} {c['feature_name']:<30} {flag_str:<5} {c['production_entry']}")
        print(f"\nTotal: {len(configs)} config(s)")
        return True

    if opts.action == "set":
        result = db.set_rollback_flag(opts.task_id, opts.flag, reason=opts.reason)
        if not result.get("success"):
            cprint(t("cli.messages.rollback_set_failed",
                     error=result.get("error", "unknown")), "red")
            return True
        flag_str = "ROLLED BACK to Python" if opts.flag == 1 else "Normal (Rust)"
        cprint(t("cli.messages.rollback_set_ok",
                 feature=result.get("feature_name", ""),
                 flag_str=flag_str,
                 previous=result.get("previous_flag", "?")), "green")
        return True

    if opts.action == "is-rolled-back":
        rolled_back = db.is_feature_rolled_back(opts.feature_name)
        if rolled_back:
            cprint(t("cli.messages.rollback_feature_rolled_back",
                     feature=opts.feature_name), "yellow")
        else:
            cprint(t("cli.messages.rollback_feature_normal",
                     feature=opts.feature_name), "green")
        # exit code: 0=normal, 1=rolled back (便于脚本判断)
        import sys as _sys
        _sys.exit(1 if rolled_back else 0)

    return True


def _handle_build_context(args, db):
    """处理 build-context 子命令（构建上下文管理）

    子命令：
        register <WORKSPACE_ID> <NAME> [--flags ...] [--defines ...] [--includes ...] [--activate]
        list <WORKSPACE_ID>
        show <WORKSPACE_ID> <HASH>
        activate <WORKSPACE_ID> <HASH>
        delete <WORKSPACE_ID> <HASH>
        import-compile-commands <FILE> <WORKSPACE_ID> [--name NAME] [--activate]
        resolve <WORKSPACE_ID> <HASH>            计算 resolved_edges（先清旧再写入）
        edges <WORKSPACE_ID> <HASH> [--caller SYM_ID] [--limit N]
    """
    from ..db.db_toolchain import (
        init_toolchain_schema, register_build_context, get_build_context,
        list_build_contexts, set_active_build_context, delete_build_context,
        get_active_build_context, get_resolved_edges, count_resolved_edges,
        store_resolved_edges, delete_resolved_edges,
    )

    parser = argparse.ArgumentParser(
        prog="cw build-context",
        description="Build context management (L5: 构建上下文感知)",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # register
    reg = sub.add_parser("register", help="Register a build context")
    reg.add_argument("workspace_id", type=int, help="Workspace ID")
    reg.add_argument("name", help="Context name (e.g. debug, release)")
    reg.add_argument("--flags", nargs="*", default=[],
                     help="Compile flags (e.g. -O2 -g)")
    reg.add_argument("--defines", nargs="*", default=[],
                     help="Defines (e.g. DEBUG=1 BOARD=A98)")
    reg.add_argument("--includes", nargs="*", default=[], help="Include paths")
    reg.add_argument("--activate", action="store_true",
                     help="Set as active context")

    # list
    lst = sub.add_parser("list", help="List build contexts")
    lst.add_argument("workspace_id", type=int, help="Workspace ID")

    # show
    show_p = sub.add_parser("show", help="Show build context details")
    show_p.add_argument("workspace_id", type=int, help="Workspace ID")
    show_p.add_argument("hash", help="Build context hash")

    # activate
    act_p = sub.add_parser("activate", help="Set active build context")
    act_p.add_argument("workspace_id", type=int, help="Workspace ID")
    act_p.add_argument("hash", help="Build context hash")

    # delete
    del_p = sub.add_parser("delete", help="Delete a build context")
    del_p.add_argument("workspace_id", type=int, help="Workspace ID")
    del_p.add_argument("hash", help="Build context hash")

    # import-compile-commands
    imp = sub.add_parser("import-compile-commands",
                         help="Import from compile_commands.json")
    imp.add_argument("file", help="Path to compile_commands.json")
    imp.add_argument("workspace_id", type=int, help="Workspace ID")
    imp.add_argument("--name", default="imported", help="Context name")
    imp.add_argument("--activate", action="store_true",
                     help="Set as active context")
    imp.add_argument("--workspace-root", default="",
                     help="Workspace root for path normalization")

    # resolve（计算 resolved_edges）
    resolve_p = sub.add_parser(
        "resolve", help="Compute resolved edges for a build context")
    resolve_p.add_argument("workspace_id", type=int, help="Workspace ID")
    resolve_p.add_argument("hash", help="Build context hash")

    # edges
    edges_p = sub.add_parser(
        "edges", help="List resolved edges for a build context")
    edges_p.add_argument("workspace_id", type=int, help="Workspace ID")
    edges_p.add_argument("hash", help="Build context hash")
    edges_p.add_argument("--caller", type=int, default=None,
                         help="Filter by caller symbol ID")
    edges_p.add_argument("--limit", type=int, default=50, help="Max results")

    opts = parser.parse_args(args)

    # 初始化 schema（幂等）
    init_toolchain_schema(db.conn)

    if opts.action == "register":
        # 解析 defines: ["DEBUG=1", "BOARD=A98"] → {"DEBUG": "1", "BOARD": "A98"}
        defines_dict = {}
        for d in opts.defines:
            if "=" in d:
                k, v = d.split("=", 1)
                defines_dict[k] = v
            else:
                defines_dict[d] = ""

        ctx = register_build_context(
            conn=db.conn,
            workspace_id=opts.workspace_id,
            name=opts.name,
            compile_flags=opts.flags,
            defines=defines_dict,
            include_paths=opts.includes,
            set_active=opts.activate,
        )
        print(f"Build context registered: {ctx.name}")
        print(f"  hash: {ctx.build_context_hash}")
        print(f"  flags: {ctx.compile_flags}")
        print(f"  defines: {len(ctx.defines)} macros")
        print(f"  includes: {len(ctx.include_paths)} paths")
        if opts.activate:
            print(f"  (set as active)")

    elif opts.action == "list":
        ctxs = list_build_contexts(db.conn, opts.workspace_id)
        if not ctxs:
            print(f"No build contexts for workspace {opts.workspace_id}")
            return True
        print(f"Build contexts for workspace {opts.workspace_id}:")
        print(f"{'Name':<20} {'Active':<8} {'Hash':<20} {'Defines':<8} {'Includes':<8}")
        print("-" * 80)
        for ctx in ctxs:
            active = "✓" if ctx.is_active else ""
            print(f"{ctx.name:<20} {active:<8} {ctx.build_context_hash[:16]:<20} "
                  f"{len(ctx.defines):<8} {len(ctx.include_paths):<8}")

    elif opts.action == "show":
        ctx = get_build_context(db.conn, opts.workspace_id, opts.hash)
        if ctx is None:
            print(f"Build context not found: {opts.hash}")
            return False
        print(f"Build Context: {ctx.name}")
        print(f"  Hash: {ctx.build_context_hash}")
        print(f"  Active: {'yes' if ctx.is_active else 'no'}")
        print(f"  Compile flags ({len(ctx.compile_flags)}):")
        for f in ctx.compile_flags:
            print(f"    {f}")
        print(f"  Defines ({len(ctx.defines)}):")
        for k, v in list(ctx.defines.items())[:20]:
            print(f"    {k}={v}")
        if len(ctx.defines) > 20:
            print(f"    ... and {len(ctx.defines) - 20} more")
        print(f"  Include paths ({len(ctx.include_paths)}):")
        for p in ctx.include_paths[:10]:
            print(f"    {p}")
        if len(ctx.include_paths) > 10:
            print(f"    ... and {len(ctx.include_paths) - 10} more")
        # 统计 resolved edges（用完整 hash）
        count = count_resolved_edges(
            db.conn, opts.workspace_id, ctx.build_context_hash)
        print(f"  Resolved edges: {count}")

    elif opts.action == "activate":
        ctx = get_build_context(db.conn, opts.workspace_id, opts.hash)
        if ctx is None:
            print(f"Build context not found: {opts.hash}")
            return False
        full_hash = ctx.build_context_hash
        if set_active_build_context(db.conn, opts.workspace_id, full_hash):
            print(f"Activated: {ctx.name} ({full_hash[:16]})")
        else:
            print(f"Failed to activate")
            return False

    elif opts.action == "delete":
        ctx = get_build_context(db.conn, opts.workspace_id, opts.hash)
        if ctx is None:
            print(f"Not found: {opts.hash}")
            return False
        full_hash = ctx.build_context_hash
        if delete_build_context(db.conn, opts.workspace_id, full_hash):
            print(f"Deleted: {ctx.name} ({full_hash[:16]})")
        else:
            print(f"Failed to delete")
            return False

    elif opts.action == "import-compile-commands":
        from ..analyzers.compile_commands import import_compile_commands
        if not os.path.exists(opts.file):
            print(f"File not found: {opts.file}")
            return False

        agg = import_compile_commands(
            opts.file, opts.workspace_root or os.getcwd())
        print(f"Imported {agg.file_count} compile entries:")
        print(f"  compiler: {agg.compiler_path or '(not detected)'}")
        print(f"  defines: {len(agg.defines)}")
        print(f"  include_paths: {len(agg.include_paths)}")
        print(f"  compile_flags: {len(agg.compile_flags)}")

        # 注册 build context
        ctx = register_build_context(
            conn=db.conn,
            workspace_id=opts.workspace_id,
            name=opts.name,
            compile_flags=agg.compile_flags,
            defines=agg.defines,
            include_paths=agg.include_paths,
            set_active=opts.activate,
        )
        print(f"Build context registered: {ctx.name}")
        print(f"  hash: {ctx.build_context_hash}")
        if opts.activate:
            print(f"  (set as active)")

        # 如果检测到编译器，提示注册 toolchain
        if agg.compiler_path:
            print(f"\n  Hint: Detected compiler '{agg.compiler_path}'")
            print(
                f"  Run: cw toolchain register auto_{int(time.time())} {agg.compiler_path}")

    elif opts.action == "resolve":
        from ..analyzers.resolved_edges_engine import compute_resolved_edges
        # 验证 build context 存在
        ctx = get_build_context(db.conn, opts.workspace_id, opts.hash)
        if ctx is None:
            print(f"Build context not found: {opts.hash}")
            return False
        # 使用完整 hash（get_build_context 已支持短 hash 前缀匹配，但引擎内部需要完整 hash）
        full_hash = ctx.build_context_hash
        # 计算
        result = compute_resolved_edges(db.conn, opts.workspace_id, full_hash)
        if result.get("error"):
            print(f"Error: {result['error']}")
            return False
        # 先清旧再写入（重建）
        deleted = delete_resolved_edges(db.conn, opts.workspace_id, full_hash)
        stored = store_resolved_edges(
            db.conn, opts.workspace_id, full_hash, result["edges"]
        )
        print(f"Resolved edges computed for: {ctx.name}")
        print(f"  source: {result['source']}")
        print(f"  computed: {result['count']} edges")
        if result.get("skipped"):
            print(f"  skipped (caller unmapped): {result['skipped']}")
        print(f"  deleted old: {deleted}")
        print(f"  stored: {stored}")

    elif opts.action == "edges":
        # 先用短 hash 查找完整 hash
        ctx = get_build_context(db.conn, opts.workspace_id, opts.hash)
        if ctx is None:
            print(f"Build context not found: {opts.hash}")
            return False
        full_hash = ctx.build_context_hash
        edges = get_resolved_edges(
            db.conn, opts.workspace_id, full_hash,
            caller_symbol_id=opts.caller, limit=opts.limit,
        )
        if not edges:
            print(f"No resolved edges found")
            return True
        print(f"Resolved edges ({len(edges)} shown):")
        print(
            f"{'Caller':<10} {'Callee':<10} {'Callee Name':<30} {'File':<20} {'Line':<6} {'Method':<15}")
        print("-" * 95)
        for e in edges:
            print(f"{e.caller_symbol_id:<10} {e.callee_symbol_id:<10} "
                  f"{e.callee_name[:30]:<30} {e.callee_file[:20]:<20} "
                  f"{e.call_line:<6} {e.resolution_method:<15}")

    return True


def create_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器（使用 i18n 文本）"""
    parser = argparse.ArgumentParser(
        description=t("cli.description") + "\n\n" + t("cli.subcommand_help"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--lang", metavar="LANG", default=DEFAULT_LANG,
                        help=get_arg_help("lang"))
    parser.add_argument("--workspace", metavar="ROOT",
                        help=get_arg_help("workspace"))
    parser.add_argument("--root", metavar="ROOT", help=get_arg_help("root"))
    parser.add_argument("--list-workspaces", action="store_true",
                        help=get_arg_help("list_workspaces"))
    parser.add_argument("--register-workspace", nargs=2, metavar=("NAME",
                        "ROOT"), help=get_arg_help("register_workspace"))
    parser.add_argument("--set-workspace", metavar="ID_OR_NAME",
                        help=get_arg_help("set_workspace"))
    parser.add_argument("--delete-workspace", metavar="ID_OR_NAME",
                        help=get_arg_help("delete_workspace"))
    parser.add_argument("--refresh-all", action="store_true",
                        dest="refresh_all", help=get_arg_help("refresh_all"))
    parser.add_argument("--force", action="store_true",
                        help=get_arg_help("force"))
    parser.add_argument("--watch", action="store_true",
                        help=get_arg_help("watch"))
    parser.add_argument("--stats", action="store_true",
                        help=get_arg_help("stats"))
    parser.add_argument("--status", action="store_true",
                        help=get_arg_help("status"))
    parser.add_argument("--query", nargs=2, metavar=("NAME",
                        "FILE"), help=get_arg_help("query"))
    parser.add_argument("--callers", metavar="NAME",
                        help=get_arg_help("callers"))
    parser.add_argument("--callees", metavar="NAME",
                        help=get_arg_help("callees"))
    parser.add_argument("--topo", action="store_true",
                        help=get_arg_help("topo"))
    parser.add_argument("--topo-limit", type=int, default=50,
                        help=get_arg_help("topo_limit"))
    parser.add_argument("--file", metavar="PATH", help=get_arg_help("file"))
    parser.add_argument("--refresh", metavar="PATH [...]", nargs="+",
                        help=get_arg_help("refresh"))
    parser.add_argument("--history", metavar="NAME",
                        help=get_arg_help("history"))
    parser.add_argument("--show-content", action="store_true",
                        help=get_arg_help("show_content"))
    parser.add_argument("--diff", nargs=2, metavar=("HASH1",
                        "HASH2"), help=get_arg_help("diff"))
    parser.add_argument("--changes", metavar="SINCE", nargs="?",
                        const="1h", help=get_arg_help("changes"))
    parser.add_argument("--changes-detail", action="store_true",
                        help=get_arg_help("changes_detail"))
    parser.add_argument("--restore-comment", metavar="SPEC",
                        help=get_arg_help("restore_comment"))
    parser.add_argument("--restore-all-comments", action="store_true",
                        help=get_arg_help("restore_all_comments"))
    parser.add_argument("--restore-file", metavar="PATH",
                        help=get_arg_help("restore_file"))
    parser.add_argument("--preview", action="store_true",
                        help=get_arg_help("preview"))
    parser.add_argument("--comment-coverage", action="store_true",
                        help=get_arg_help("comment_coverage"))
    parser.add_argument("--coverage-by", metavar="GROUP",
                        default="module", help=get_arg_help("coverage_by"))
    parser.add_argument("--uncommented", metavar="KIND",
                        nargs="?", const="fn", help=get_arg_help("uncommented"))
    parser.add_argument("--uncommented-module", metavar="MODULE",
                        help=get_arg_help("uncommented_module"))
    parser.add_argument("--uncommented-limit", metavar="N", type=int,
                        default=50, help=get_arg_help("uncommented_limit"))
    parser.add_argument("--search", metavar="QUERY",
                        help=get_arg_help("search"))
    parser.add_argument("--search-kind", metavar="KIND",
                        help=get_arg_help("search_kind"))
    parser.add_argument("--search-limit", metavar="N", type=int,
                        default=50, help=get_arg_help("search_limit"))
    parser.add_argument("--symbol", metavar="QUALIFIED_NAME",
                        help=get_arg_help("symbol"))
    parser.add_argument("--impact", metavar="QUALIFIED_NAME",
                        help=get_arg_help("impact"))
    parser.add_argument("--call-chain", metavar="QUALIFIED_NAME",
                        help=get_arg_help("call_chain"))
    parser.add_argument("--chain-depth", metavar="N", type=int,
                        default=10, help=get_arg_help("chain_depth"))
    parser.add_argument("--top-callers", metavar="N", type=int,
                        nargs="?", const=20, help=get_arg_help("top_callers"))
    parser.add_argument("--top-callers-module", metavar="MODULE",
                        help=get_arg_help("top_callers_module"))
    parser.add_argument("--orphan-symbols", metavar="KIND",
                        nargs="?", const="fn", help=get_arg_help("orphan_symbols"))
    parser.add_argument("--orphan-module", metavar="MODULE",
                        help=get_arg_help("orphan_module"))
    parser.add_argument("--orphan-limit", metavar="N", type=int,
                        default=50, help=get_arg_help("orphan_limit"))
    parser.add_argument("--deepest", metavar="N", type=int,
                        nargs="?", const=20, help=get_arg_help("deepest"))
    parser.add_argument("--deepest-module", metavar="MODULE",
                        help=get_arg_help("deepest_module"))
    parser.add_argument("--module-calls", metavar="N", type=int,
                        nargs="?", const=20, help=get_arg_help("module_calls"))
    parser.add_argument("--detect-cycles", action="store_true",
                        help=get_arg_help("detect_cycles"))
    parser.add_argument("--cycle-depth", metavar="N", type=int,
                        default=10, help=get_arg_help("cycle_depth"))
    parser.add_argument("--export-module-graph", metavar="FORMAT", nargs="?",
                        const="mermaid", help=get_arg_help("export_module_graph"))
    parser.add_argument("--graph-output", metavar="FILE",
                        help=get_arg_help("graph_output"))
    parser.add_argument("--call-heatmap", metavar="GROUP_BY",
                        nargs="?", const="module", help=get_arg_help("call_heatmap"))
    parser.add_argument("--heatmap-limit", metavar="N", type=int,
                        default=20, help=get_arg_help("heatmap_limit"))
    parser.add_argument("--test-coverage", action="store_true",
                        help=get_arg_help("test_coverage"))
    parser.add_argument("--function-issues", metavar="FN",
                        nargs="?", const="", help=get_arg_help("function_issues"))
    parser.add_argument("--issue-summary", action="store_true",
                        help=get_arg_help("issue_summary"))
    parser.add_argument("--issue-type", metavar="TYPE",
                        help=get_arg_help("issue_type"))
    parser.add_argument("--issue-module", metavar="MODULE",
                        help=get_arg_help("issue_module"))
    parser.add_argument("--issue-limit", metavar="N", type=int,
                        default=30, help=get_arg_help("issue_limit"))
    parser.add_argument("--semgrep", metavar="PATH",
                        nargs="*", help=get_arg_help("semgrep"))
    parser.add_argument("--semgrep-config", metavar="CONFIG",
                        default="p/default", help=get_arg_help("semgrep_config"))
    parser.add_argument("--semgrep-scan-lang", metavar="LANG",
                        nargs="*", help=get_arg_help("semgrep_scan_lang"))
    parser.add_argument("--semgrep-timeout", metavar="N", type=int,
                        default=180, help=get_arg_help("semgrep_timeout"))
    parser.add_argument("--semgrep-quick", action="store_true",
                        help=get_arg_help("semgrep_quick"))
    parser.add_argument("--semgrep-save", action="store_true",
                        help=get_arg_help("semgrep_save"))
    parser.add_argument("--semgrep-list", nargs="?", const="",
                        metavar="FILTER", help=get_arg_help("semgrep_list"))
    parser.add_argument("--semgrep-severity", metavar="SEV",
                        help=get_arg_help("semgrep_severity"))
    parser.add_argument("--semgrep-list-lang", metavar="LANG",
                        help=get_arg_help("semgrep_list_lang"))
    parser.add_argument("--semgrep-stats", action="store_true",
                        help=get_arg_help("semgrep_stats"))
    parser.add_argument("--semgrep-limit", metavar="N", type=int,
                        default=50, help=get_arg_help("semgrep_limit"))

    # Git 集成
    parser.add_argument("--git-import", metavar="N", type=int,
                        nargs="?", const=100, help=get_arg_help("git_import"))
    parser.add_argument("--git-log", metavar="N", type=int,
                        nargs="?", const=20, help=get_arg_help("git_log"))
    parser.add_argument("--git-show", metavar="COMMIT",
                        help=get_arg_help("git_show"))
    parser.add_argument("--git-stats", action="store_true",
                        help=get_arg_help("git_stats"))

    # 代码度量
    parser.add_argument("--metrics", action="store_true",
                        help=get_arg_help("metrics"))
    parser.add_argument("--complexity", metavar="N", type=int,
                        nargs="?", const=20, help=get_arg_help("complexity"))
    parser.add_argument("--complexity-module", metavar="MODULE",
                        help=get_arg_help("complexity_module"))
    parser.add_argument("--coupling", action="store_true",
                        help=get_arg_help("coupling"))
    parser.add_argument("--largest-fns", metavar="N", type=int,
                        nargs="?", const=20, help=get_arg_help("largest_fns"))
    parser.add_argument("--coupled-fns", metavar="N", type=int,
                        nargs="?", const=20, help=get_arg_help("coupled_fns"))
    parser.add_argument("--fn-metrics", metavar="NAME",
                        help=get_arg_help("fn_metrics"))

    # 语义搜索
    parser.add_argument("--semantic-search", metavar="QUERY",
                        help=get_arg_help("semantic_search"))
    parser.add_argument("--embed", action="store_true",
                        help=get_arg_help("embed"))
    parser.add_argument("--embed-force", action="store_true",
                        help=get_arg_help("embed_force"))
    parser.add_argument("--similar", metavar="NAME",
                        help=get_arg_help("similar"))

    # 任务管理
    parser.add_argument("--task-list", action="store_true",
                        help=get_arg_help("task_list"))
    parser.add_argument("--task-show", metavar="TASK_ID",
                        help=get_arg_help("task_show"))

    # 项目简报和仓库地图
    parser.add_argument("--brief", action="store_true",
                        help=get_arg_help("brief"))
    parser.add_argument("--map", action="store_true", help=get_arg_help("map"))
    parser.add_argument(
        "--map-format", choices=["text", "mermaid"], default="text", help=get_arg_help("map_format"))

    # 覆盖率
    parser.add_argument("--coverage-import", metavar="FILE",
                        help=get_arg_help("coverage_import"))
    parser.add_argument("--coverage-format", choices=[
                        "lcov", "cobertura"], default="lcov", help=get_arg_help("coverage_format"))
    parser.add_argument("--coverage-fn", metavar="NAME",
                        help=get_arg_help("coverage_fn"))
    parser.add_argument("--coverage-uncovered", action="store_true",
                        help=get_arg_help("coverage_uncovered"))

    # 所有权
    parser.add_argument("--who", metavar="FILE", help=get_arg_help("who"))
    parser.add_argument("--ownership-map", action="store_true",
                        help=get_arg_help("ownership_map"))

    return parser


def main():
    """CLI 主入口函数"""
    # 强制 UTF-8 输出，避免 Windows GBK 控制台无法输出 Unicode 字符（↳ ✓ ⚠ 等）
    from .console import ensure_utf8_output
    ensure_utf8_output()

    # daemon 必须绕过本地 CodeGraphDB 初始化，所有状态均通过 UDS/registry 管理。
    if len(sys.argv) > 1 and sys.argv[1] == "daemon":
        from .daemon_commands import run_daemon_command
        raise SystemExit(run_daemon_command(sys.argv[2:]))

    # --workspace 预扫描：允许 `cw --workspace ROOT task show T-xxx` 形式
    # 提取 --workspace ROOT 到环境变量，从 argv 移除后让 sys.argv[1] 指向真正子命令
    if "--workspace" in sys.argv[1:]:
        idx = sys.argv.index("--workspace")
        if idx + 1 < len(sys.argv):
            os.environ["CALLWARDEN_WORKSPACE"] = sys.argv[idx + 1]
            del sys.argv[idx:idx + 2]

    # 代码守护者架构子命令拦截（四大支柱）
    # 子命令格式: cw <subcommand> [options]，如 cw defect stats
    if len(sys.argv) > 1 and sys.argv[1] in _SUBCOMMANDS:
        _run_subcommand_mode()
        return

    # 显示 --help 时，调用 _print_main_help() 输出完整 12 组结构（C8 Step #3）
    # 替代旧的 4-pillar 分组 + argparse 默认 description
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        _print_main_help()
        return

    # Lazy Auto-Setup：setup 子命令拦截（不需要数据库初始化，在 _SUBCOMMANDS 检查前处理）
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        # 先设置语言（复用 pre_parser 解析 --lang）
        _pre = argparse.ArgumentParser(add_help=False)
        _pre.add_argument("--lang", metavar="LANG", default=DEFAULT_LANG)
        _pa, _ = _pre.parse_known_args()
        set_language(_pa.lang)
        _handle_setup()
        return

    # P0-3 修复：支持 cw --version / cw -V（复审报告 §3 P0-3 问题 6）
    # Gate 3 的首个黑盒命令 `cw --version` 依赖此分支。
    # version.toml §6 注释声称 `cw --version` 是版本源之一，必须真实实现。
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        try:
            from . import __version__ as _cw_version
        except ImportError:
            # cw.py 透传模式下，callwarden 包已可导入
            import importlib
            _cw_version = importlib.import_module("callwarden").__version__
        print(f"callwarden {_cw_version}")
        return

    # 第一阶段：先解析 --lang 参数（不创建完整 parser）
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--lang", metavar="LANG", default=DEFAULT_LANG)
    pre_args, _ = pre_parser.parse_known_args()

    # 设置语言
    set_language(pre_args.lang)

    # Lazy Auto-Setup：首次运行自动探测 AI 工具并注册 MCP Server（幂等，失败不影响主命令）
    _check_auto_setup()

    # 第二阶段：用正确的语言创建完整 parser 并解析所有参数
    parser = create_parser()
    args = parser.parse_args()

    # C8 Step #2: --flag deprecated 警告（输出到 stderr，不阻断执行）
    _emit_deprecated_flag_warning(args)

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
    db = CodeGraphDB(
        workspace_root=workspace_root) if workspace_root else CodeGraphDB()

    # 如果自动检测到了工作区，自动注册并设置为活动工作区
    # 优化：只读命令（search/symbol/callers 等查询类）跳过 register/set_active_workspace 写操作，
    # 避免被 MCP Server 写锁卡住。set_active_workspace 内部也有 is_active 短路判断。
    if workspace_root and not _is_readonly_args(args):
        try:
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
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                cprint(get_error("db_locked"), "red")
                sys.exit(2)
            raise

    try:
        # 工作区管理命令
        if args.list_workspaces:
            workspaces = db.list_workspaces()
            print(t("cli.messages.workspaces_title", count=len(workspaces)))
            for ws in workspaces:
                active_mark = t("cli.messages.workspace_active_mark") if ws.get(
                    "is_active") else ""
                print(t("cli.messages.workspace_normal",
                      id=ws['id'], name=ws['name']) + active_mark)
                print(t("cli.messages.workspace_path", path=ws['root_path']))
                if ws.get("description"):
                    print(t("cli.messages.workspace_desc",
                          desc=ws['description']))
            return

        if args.register_workspace:
            name, root = args.register_workspace
            ws_id = db.register_workspace(name, root)
            print(t("cli.messages.register_success",
                  id=ws_id, name=name, root=root))
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
                print(t("cli.messages.set_success",
                      name=active['name'], root=active['root_path']))
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
            # C2: refresh-all 完成后自动同步 AGENTS.md（fail-soft，不阻断 refresh）
            try:
                sync_result = db.rule_sync_agents_md(
                    target_path="AGENTS.md",
                    dry_run=False,
                    actor="cli_refresh_all",
                )
                if sync_result.get("success"):
                    print(t(
                        "cli.messages.agents_md_auto_sync_success",
                        count=sync_result.get("rule_count", 0),
                    ))
                else:
                    error = sync_result.get("error", "")
                    if "marker" in error.lower() or "not found" in error.lower():
                        print(t("cli.messages.agents_md_auto_sync_no_marker"))
                    else:
                        print(t(
                            "cli.messages.agents_md_auto_sync_skipped",
                            error=error,
                        ))
            except Exception as exc:
                # fail-soft：同步失败不阻断 refresh，仅输出提示
                print(t(
                    "cli.messages.agents_md_auto_sync_skipped",
                    error=str(exc),
                ))

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
            print(
                f"  {t('cli.messages.status_db_size')}: {fmt_size(ws['db_size'])}")
            print(
                f"  {t('cli.messages.status_last_build')}: {fmt_ago(status['last_build'])}")
            print()
            print(f"  {t('cli.messages.status_files_title')}")
            on_disk = t("cli.messages.status_files_on_disk")
            tracked = t("cli.messages.status_files_tracked")
            print(
                f"    {on_disk}: {fi['on_disk']}  ({tracked}: {fi['tracked']})")
            if fi["new"]:
                new_label = t("cli.messages.status_files_new")
                print(
                    f"    {new_label}: {fi['new']}  {', '.join(fi['new_files'][:5])}{'...' if len(fi['new_files']) > 5 else ''}")
            if fi["stale"]:
                stale_label = t("cli.messages.status_files_stale")
                print(
                    f"    {stale_label}: {fi['stale']}  {', '.join(fi['stale_files'][:5])}{'...' if len(fi['stale_files']) > 5 else ''}")
            if fi["deleted"]:
                deleted_label = t("cli.messages.status_files_deleted")
                print(
                    f"    {deleted_label}: {fi['deleted']}  {', '.join(fi['deleted_files'][:5])}{'...' if len(fi['deleted_files']) > 5 else ''}")
            if fi["by_language"]:
                parts = []
                for ext, cnt in sorted(fi["by_language"].items(), key=lambda x: -x[1])[:6]:
                    parts.append(f"{ext}: {cnt}")
                by_lang = t("cli.messages.status_by_language")
                print(f"    {by_lang}: {', '.join(parts)}")
            print()
            print(f"  {t('cli.messages.status_symbols_title')}")
            print(
                f"    {t('cli.messages.status_symbols_total')}: {sy['total']}")
            kind_parts = []
            kind_names = {"fn": t("cli.messages.kind_fn"), "test_fn": t("cli.messages.kind_test_fn"), "struct": t("cli.messages.kind_struct"),
                          "enum": t("cli.messages.kind_enum"), "trait": t("cli.messages.kind_trait"), "impl": "impl",
                          "const": "const", "static": "static", "method": t("cli.messages.kind_method"),
                          "class": t("cli.messages.kind_class"), "interface": t("cli.messages.kind_interface")}
            for kind, cnt in sorted(sy["by_kind"].items(), key=lambda x: -x[1])[:8]:
                kn = kind_names.get(kind, kind)
                kind_parts.append(f"{kn}: {cnt}")
            print(
                f"    {t('cli.messages.status_by_kind')}: {', '.join(kind_parts)}")
            print(
                f"    {t('cli.messages.status_uncommented_fns')}: {sy['uncommented_fns']}")
            print()
            print(f"  {t('cli.messages.status_calls_title')}")
            print(f"    {t('cli.messages.status_calls_total')}: {ca['total']}")
            resolved_label = t("cli.messages.status_calls_resolved")
            rate_label = t("cli.messages.status_calls_rate")
            print(
                f"    {resolved_label}: {ca['resolved']}  ({rate_label}: {ca['resolve_rate']}%)")
            print(
                f"    {t('cli.messages.status_calls_cross')}: {ca['cross_file']}")
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
            print(t("cli.messages.callers_title",
                  name=args.callers, count=len(callers)))
            for c in callers:
                cross = t(
                    "cli.messages.callers_cross_file") if c["is_cross_file"] else ""
                print(t("cli.messages.callers_item",
                        file=c['caller_file'], line=c['call_line'], name=c['caller_name'], cross=cross))

        elif args.callees:
            callees = db.get_callees(args.callees)
            print(t("cli.messages.callees_title",
                  name=args.callees, count=len(callees)))
            for c in callees:
                cross = t(
                    "cli.messages.callees_cross_file") if c["is_cross_file"] else ""
                file_info = f" ({c['callee_file']})" if c["callee_file"] else t(
                    "cli.messages.callees_unresolved")
                print(t("cli.messages.callees_item",
                        line=c['call_line'], name=c['callee_name'], cross=cross, file_info=file_info))

        elif args.topo:
            order = db.get_topological_order(args.topo_limit)
            print(t("cli.messages.topo_title", count=len(order)))
            for i, sym in enumerate(order):
                print(t("cli.messages.topo_item",
                        idx=i+1, depth=f"{sym['depth']:2d}", path=sym['path'], line=sym['start_line'], name=sym['name']))

        elif args.file:
            symbols = db.get_file_symbols(args.file)
            print(t("cli.messages.file_symbols_title",
                  path=args.file, count=len(symbols)))
            for s in symbols:
                print(
                    f"  {s['start_line']}-{s['end_line']}: {s['kind']} {s['name']} ({s['visibility']})")

        elif args.refresh:
            # C8 Step #5: --refresh 支持多 path（nargs='+'）
            # 循环调用 db.refresh_file(p)，输出每个文件刷新结果汇总
            paths = args.refresh if isinstance(
                args.refresh, list) else [args.refresh]
            success_count = 0
            failure_count = 0
            failed_paths = []
            start_ts = time.time()
            for path in paths:
                try:
                    db.refresh_file(path)
                    print(t("cli.messages.refresh_done", path=path))
                    success_count += 1
                except Exception as exc:
                    failure_count += 1
                    failed_paths.append((path, str(exc)))
                    cprint(t("cli.messages.refresh_failed",
                           path=path, error=str(exc)), "red")
            elapsed = time.time() - start_ts
            # 输出汇总
            if len(paths) > 1:
                cprint(t("cli.messages.refresh_multi_summary",
                         success=success_count, failure=failure_count,
                         total=len(paths), elapsed=f"{elapsed:.2f}"), "cyan", bold=True)
                if failed_paths:
                    cprint(t("cli.messages.refresh_multi_failed_title"),
                           "red", bold=True)
                    for path, err in failed_paths:
                        print(t("cli.messages.refresh_multi_failed_item",
                                path=path, error=err))

        elif args.history:
            history = db.get_history(args.history)
            if not history:
                print(t("cli.messages.history_not_found", name=args.history))
            else:
                print(t("cli.messages.history_title",
                      name=args.history, count=len(history)))
                for i, h in enumerate(history, 1):
                    current = t(
                        "cli.messages.history_current") if h["is_current"] else ""
                    parsed_time = time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(h["parsed_at"]))
                    print(
                        f"  {i}. v{h['version_num']}{current} | {parsed_time} | hash={h['symbol_hash'][:12]}... | {h['file_path']}:{h['start_line']}-{h['end_line']}")

                    if args.show_content:
                        content = db.get_symbol_content_by_hash(
                            h["symbol_hash"])
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
                print(t("cli.messages.diff_title",
                      hash1=hash1[:12], hash2=hash2[:12]))
                print(t("cli.messages.diff_function",
                      name=content1['qualified_name']))
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
                            print(t("cli.messages.diff_remove_line",
                                  idx=i+1, content=l1))
                        if l2:
                            print(t("cli.messages.diff_add_line",
                                  idx=i+1, content=l2))

        elif args.changes:
            result = db.get_recent_changes(args.changes)
            changed_files = result["changed_files"]
            changed_funcs = result["changed_functions"]

            # 只显示真正有变化的文件（有多个版本的）
            multi_version_files = [
                f for f in changed_files if f["version_num"] > 1]

            print(t("cli.messages.changes_title", since=args.changes))
            print(t("cli.messages.changes_file_versions", count=len(changed_files)))
            print(t("cli.messages.changes_multi_files",
                  count=len(multi_version_files)))
            print(t("cli.messages.changed_funcs_count", count=len(changed_funcs)))
            print()

            if multi_version_files:
                print(t("cli.messages.changed_files_title"))
                for fv in multi_version_files:
                    parsed_time = time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(fv["parsed_at"]))
                    current = t(
                        "cli.messages.history_current") if fv["is_current"] else ""
                    print(
                        f"  v{fv['version_num']}{current} | {parsed_time} | {fv['path']}")

            if changed_funcs:
                print()
                print(t("cli.messages.changed_funcs_title"))
                for cf in changed_funcs:
                    parsed_time = time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(cf["parsed_at"]))
                    type_tag = f"[{cf['change_type']}]"
                    print(f"  {type_tag:4} {cf['qualified_name']}")
                    print(
                        f"       {cf['file_path']}:{cf['line']} | {parsed_time}")

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
            result = db.restore_comment(
                args.restore_comment, preview=args.preview)

            if not result["success"]:
                print(t("cli.messages.restore_fail", error=result['error']))
            elif result.get("preview"):
                print(t("cli.messages.restore_preview_title"))
                print(t("cli.messages.restore_function",
                      name=result['qualified_name']))
                print(t("cli.messages.restore_file", path=result['file_path']))
                print(t("cli.messages.restore_current_comment",
                      comment=result['old_comment']))
                print(t("cli.messages.restore_new_comment"))
                print(result['new_comment'])
                print()
                print(t("cli.messages.restore_new_content_preview"))
                print(result['new_content_preview'])
            else:
                print(t("cli.messages.restore_success"))
                print(t("cli.messages.restore_function",
                      name=result['qualified_name']))
                print(t("cli.messages.restore_file", path=result['file_path']))
                print(t("cli.messages.restore_from_version",
                      version=result['restored_from'], lines=result['comment_lines']))

        elif args.restore_all_comments:
            file_filter = args.restore_file if args.restore_file else None
            result = db.restore_all_comments(
                preview=args.preview, file_filter=file_filter)

            mode = t("cli.messages.restore_all_mode_preview") if args.preview else t(
                "cli.messages.restore_all_mode_restore")
            print(t("cli.messages.restore_all_done", mode=mode))
            print(t("cli.messages.restore_all_found",
                  count=result['total_found']))
            print(t("cli.messages.restore_all_restored",
                  count=result['restored']))
            print(t("cli.messages.restore_all_skipped",
                  count=result['skipped']))
            print(t("cli.messages.restore_all_failed", count=result['failed']))
            print(t("cli.messages.restore_all_files",
                  count=len(result['files'])))

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
                    print(t("cli.messages.restore_all_error_item", err=err))

        elif args.comment_coverage:
            result = db.get_comment_coverage(group_by=args.coverage_by)

            print(t("cli.messages.comment_coverage_title"))
            print(t("cli.messages.comment_coverage_total",
                  count=result['total']))
            print(t("cli.messages.comment_coverage_commented",
                  count=result['commented']))
            print(t("cli.messages.comment_coverage_rate",
                  pct=result['coverage']))
            print()

            print(t("cli.messages.comment_coverage_by_kind"))
            for kind, info in sorted(result["by_kind"].items(), key=lambda x: -x[1]["total"]):
                pct = round(info["commented"] / info["total"]
                            * 100, 1) if info["total"] > 0 else 0
                bar_len = int(pct / 5)
                bar = "█" * bar_len + "░" * (20 - bar_len)
                print(
                    f"  {bar} {pct:5.1f}%  {kind:12s}  ({info['commented']}/{info['total']})")

            if result.get("by_module"):
                print()
                print(t("cli.messages.comment_coverage_by_module"))
                modules = sorted(
                    result["by_module"].items(), key=lambda x: x[1]["coverage"])
                for i, (mod, info) in enumerate(modules[:30]):
                    pct = info["coverage"]
                    bar_len = int(pct / 5)
                    bar = "█" * bar_len + "░" * (20 - bar_len)
                    print(
                        f"  {bar} {pct:5.1f}%  {mod:50s}  ({info['commented']}/{info['total']})")
                if len(modules) > 30:
                    print(t("cli.messages.comment_coverage_more_modules",
                          count=len(modules) - 30))

            if result.get("by_file"):
                print()
                print(t("cli.messages.comment_coverage_by_file"))
                files = sorted(result["by_file"].items(),
                               key=lambda x: x[1]["coverage"])
                for i, (fpath, info) in enumerate(files[:30]):
                    pct = info["coverage"]
                    bar_len = int(pct / 5)
                    bar = "█" * bar_len + "░" * (20 - bar_len)
                    print(
                        f"  {bar} {pct:5.1f}%  {fpath:50s}  ({info['commented']}/{info['total']})")
                if len(files) > 30:
                    print(t("cli.messages.comment_coverage_more_files",
                          count=len(files) - 30))

        elif args.uncommented is not None:
            kind = args.uncommented
            mod_filter = args.uncommented_module
            limit = args.uncommented_limit

            symbols = db.get_uncommented_symbols(
                kind=kind, module_filter=mod_filter)

            filter_info = t("cli.messages.uncommented_module_filter",
                            module=mod_filter) if mod_filter else ""
            print(t("cli.messages.uncommented_title", kind=kind, filter_info=filter_info,
                    total=len(symbols), shown=min(limit, len(symbols))))
            print()

            for i, sym in enumerate(symbols[:limit]):
                depth = sym["depth"] if sym["depth"] >= 0 else "?"
                sig = sym.get("signature", "")[
                    :60] if sym.get("signature") else ""
                print(f"  [{i+1:3d}] depth={depth:>3}  {sym['qualified_name']}")
                print(f"         {sym['file_path']}:{sym['start_line']}")
                if sig:
                    print(f"         {sig}")

            if len(symbols) > limit:
                print()
                print(t("cli.messages.uncommented_more",
                      count=len(symbols) - limit))

        elif args.search:
            kind = args.search_kind
            limit = args.search_limit

            symbols = db.search_symbols(args.search, kind=kind, limit=limit)

            kind_info = t("cli.messages.search_kind_info",
                          kind=kind) if kind else ""
            print(t("cli.messages.search_title", query=args.search,
                  kind_info=kind_info, total=len(symbols), shown=min(limit, len(symbols))))
            print()

            for i, sym in enumerate(symbols[:limit]):
                depth = sym["depth"] if sym["depth"] >= 0 else "?"
                sig = sym.get("signature", "")[
                    :50] if sym.get("signature") else ""
                comment_mark = "✓" if sym["has_comment"] else " "
                print(
                    f"  [{i+1:3d}] depth={depth:>3} [{comment_mark}] {sym['kind']:8s} {sym['qualified_name']}")
                print(f"         {sym['file_path']}:{sym['start_line']}")
                if sig:
                    print(f"         {sig}")

            if len(symbols) >= limit:
                print()
                print(t("cli.messages.search_more"))

        elif args.symbol:
            detail = db.get_symbol(args.symbol)

            if not detail:
                print(t("cli.messages.symbol_not_found", name=args.symbol))
                print(t("cli.messages.symbol_search_hint"))
            else:
                print(t("cli.messages.symbol_detail_title"))
                print(t("cli.messages.symbol_name",
                      name=detail['qualified_name']))
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
                print(t("cli.messages.symbol_calls_out_title",
                      count=len(detail['calls_out'])))
                if detail["calls_out"]:
                    for call in detail["calls_out"][:20]:
                        target = call["target_name"]
                        line = call.get("call_line", "")
                        line_info = f" (line {line})" if line else ""
                        print(f"  → {target}{line_info}")
                    if len(detail["calls_out"]) > 20:
                        print(t("cli.messages.symbol_more",
                              count=len(detail['calls_out']) - 20))
                else:
                    print(t("cli.messages.symbol_none"))

                print()
                print(t("cli.messages.symbol_called_by_title",
                      count=len(detail['called_by'])))
                if detail["called_by"]:
                    for call in detail["called_by"][:20]:
                        caller = call["caller_name"]
                        line = call.get("call_line", "")
                        line_info = f" (line {line})" if line else ""
                        print(f"  ← {caller}{line_info}")
                    if len(detail["called_by"]) > 20:
                        print(t("cli.messages.symbol_more",
                              count=len(detail['called_by']) - 20))
                else:
                    print(t("cli.messages.symbol_none"))

        elif args.impact:
            result = db.get_call_chain_up(
                args.impact, max_depth=args.chain_depth)

            print(t("cli.messages.impact_up_title", name=result['start']))
            print(t("cli.messages.impact_up_total",
                  count=result['total_upstream']))
            print(t("cli.messages.impact_up_max_depth",
                  depth=result['max_depth_reached']))
            print()

            for level in result["levels"]:
                print(t("cli.messages.impact_up_level",
                      depth=level['depth'], count=level['count']))
                for item in level["callers"][:15]:
                    print(f"  ← {item['caller']}")
                if level["count"] > 15:
                    print(t("cli.messages.impact_up_more",
                          count=level['count'] - 15))
                print()

        elif args.call_chain:
            result = db.get_call_chain_down(
                args.call_chain, max_depth=args.chain_depth)

            print(t("cli.messages.call_chain_down_title",
                  name=result['start']))
            print(t("cli.messages.call_chain_down_total",
                  count=result['total_downstream']))
            print(t("cli.messages.call_chain_down_max_depth",
                  depth=result['max_depth_reached']))
            print()

            for level in result["levels"]:
                print(t("cli.messages.call_chain_down_level",
                      depth=level['depth'], count=level['count']))
                for item in level["callees"][:15]:
                    print(f"  → {item['callee']}")
                if level["count"] > 15:
                    print(t("cli.messages.call_chain_down_more",
                          count=level['count'] - 15))
                print()

        elif args.top_callers is not None:
            limit = args.top_callers if args.top_callers else 20
            module_filter = args.top_callers_module or ""
            results = db.get_top_callers(
                limit=limit, module_filter=module_filter)

            if module_filter:
                print(t("cli.messages.top_callers_title_module",
                      module=module_filter, count=len(results)))
            else:
                print(t("cli.messages.top_callers_title", count=len(results)))
            print()

            # 计算排名宽度
            rank_width = len(str(len(results)))

            for i, item in enumerate(results, 1):
                rank = str(i).rjust(rank_width)
                callers = t("cli.messages.top_callers_callers",
                            count=item['caller_count'])
                calls = t("cli.messages.top_callers_calls",
                          count=item['call_count'])
                print(f"  #{rank}  {item['qualified_name']}")
                print(f"        {callers} {calls}")
            print()

        elif args.orphan_symbols:
            kind = args.orphan_symbols
            module_filter = args.orphan_module or ""
            limit = args.orphan_limit
            results = db.get_orphan_symbols(
                kind=kind, module_filter=module_filter, limit=limit)

            if module_filter:
                print(t("cli.messages.orphan_title_module", kind=kind,
                      module=module_filter, count=len(results)))
            else:
                print(t("cli.messages.orphan_title",
                      kind=kind, count=len(results)))
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
            results = db.get_deepest_functions(
                limit=limit, module_filter=module_filter)

            if module_filter:
                print(t("cli.messages.deepest_title_module",
                      module=module_filter, count=len(results)))
            else:
                print(t("cli.messages.deepest_title", count=len(results)))
            print()

            rank_width = len(str(len(results)))

            for i, item in enumerate(results, 1):
                rank = str(i).rjust(rank_width)
                print(t("cli.messages.deepest_item",
                      default="  #{rank}  [depth {depth:2d}]  {name}", rank=rank, depth=item["depth"], name=item["qualified_name"]))
            print()

        elif args.module_calls is not None:
            limit = args.module_calls if args.module_calls else 20
            results = db.get_module_call_stats(limit=limit)

            print(t("cli.messages.module_calls_title", count=len(results)))
            print()

            # 计算列宽
            max_caller_len = max(len(r["caller_module"])
                                 for r in results) if results else 0
            max_callee_len = max(len(r["callee_module"])
                                 for r in results) if results else 0

            for i, item in enumerate(results, 1):
                caller = item["caller_module"].ljust(max_caller_len)
                callee = item["callee_module"].ljust(max_callee_len)
                print(t("cli.messages.module_calls_item", idx=i, caller=caller, callee=callee,
                      calls=item['call_count'], callers=item['unique_caller_count'], callees=item['unique_callee_count']))
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
                result = db.export_module_graph(
                    format=fmt, output_file=output_file)

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

                unit = t("cli.messages.heatmap_unit_module") if group_by == "module" else t(
                    "cli.messages.heatmap_unit_file")
                print(t("cli.messages.heatmap_title",
                      unit=unit, count=len(results)))
                print()

                if results:
                    max_calls = max(r["total_calls"] for r in results)
                    max_group_len = max(len(r["group"]) for r in results)

                    # 热力图标度：用不同字符表示密度
                    heat_chars = " ▁▂▃▄▅▆▇█"

                    for i, item in enumerate(results, 1):
                        # 计算热力等级（0-8）
                        ratio = item["total_calls"] / \
                            max_calls if max_calls > 0 else 0
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
            print(t("cli.messages.test_coverage_total_fns",
                  count=stats['total_functions']))
            print(t("cli.messages.test_coverage_test_fns",
                  count=stats['test_functions']))
            print(t("cli.messages.test_coverage_ratio",
                  pct=stats['test_ratio']))
            print()
            print(t("cli.messages.test_coverage_total_mods",
                  count=stats['total_modules']))
            print(t("cli.messages.test_coverage_mods_with_tests",
                  count=stats['modules_with_tests']))
            print(t("cli.messages.test_coverage_mod_ratio",
                  pct=stats['module_coverage']))
            print()

            if stats["test_by_module"]:
                print(t("cli.messages.test_coverage_dist_title"))
                print()

                max_test_count = max(m["test_count"]
                                     for m in stats["test_by_module"])
                max_mod_len = max(len(m["module"])
                                  for m in stats["test_by_module"][:20])

                for i, mod in enumerate(stats["test_by_module"][:20], 1):
                    bar_len = int(
                        mod["test_count"] / max_test_count * 30) if max_test_count > 0 else 0
                    bar = "█" * bar_len
                    mod_name = mod["module"].ljust(max_mod_len)
                    print(
                        f"  #{i:2d}  {mod_name}  {bar}  {mod['test_count']:3d} {t('cli.messages.test_coverage_test_count', count='')}".rstrip())

                if len(stats["test_by_module"]) > 20:
                    print(t("cli.messages.test_coverage_more",
                          count=len(stats['test_by_module']) - 20))
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
                    print(t("cli.messages.function_issues_title",
                          name=r['qualified_name']))
                    print(t("cli.messages.function_issues_module",
                          module=r['module_path'] or '(unknown)'))
                    print(t("cli.messages.function_issues_count",
                          count=r['issue_count']))
                    print()
                    for issue in r["issues"]:
                        icon = severity_icon.get(issue["severity"], "[?]")
                        print(
                            f"  {icon} {issue['label']}  (x{issue['count']})")
                        print(f"      {issue['description']}")
                    print()
                else:
                    print(t("cli.messages.function_issues_title", name=fn_name))
                    filter_str = t("cli.messages.function_issues_filter",
                                   filter=issue_filter) if issue_filter else ""
                    print(t("cli.messages.function_issues_no_issues") + filter_str)
                    print()
            else:
                # 列表模式
                if issue_filter:
                    print(t("cli.messages.function_issues_list_title_type",
                          filter=issue_filter, count=len(results)))
                elif module_filter:
                    print(t("cli.messages.function_issues_list_title_module",
                          module=module_filter, count=len(results)))
                else:
                    print(
                        t("cli.messages.function_issues_list_title", count=len(results)))
                print()

                for i, r in enumerate(results, 1):
                    issue_labels = []
                    for issue in r["issues"]:
                        icon = severity_icon.get(issue["severity"], "")
                        issue_labels.append(
                            f"{icon}{issue['label']}" + (f"(x{issue['count']})" if issue["count"] > 1 else ""))

                    issue_str = "  ".join(issue_labels)
                    print(f"  #{i:2d}  {r['qualified_name']}")
                    print(f"        {issue_str}")
                print()

        elif args.issue_summary:
            module_filter = args.issue_module or ""
            stats = db.get_issue_summary(module_filter=module_filter)

            if module_filter:
                print(t("cli.messages.issue_summary_title_module",
                      module=module_filter))
            else:
                print(t("cli.messages.issue_summary_title"))
            print()
            print(t("cli.messages.issue_summary_total_fns",
                  count=stats['total_functions']))
            print(t("cli.messages.issue_summary_with_issues",
                  count=stats['functions_with_issues']))
            print(t("cli.messages.issue_summary_issue_free",
                  count=stats['issue_free_functions'], pct=stats['issue_free_ratio']))
            print()

            severity_icon = {"danger": "[!]", "warn": "[~]", "info": "[i]"}

            print(t("cli.messages.issue_summary_dist_title"))
            print()

            # 按严重程度分组
            for severity in ["danger", "warn", "info"]:
                severity_issues = [i for i in stats["issues"]
                                   if i["severity"] == severity and i["function_count"] > 0]
                if severity_issues:
                    if severity == "danger":
                        severity_label = t(
                            "cli.messages.issue_summary_severity_danger")
                    elif severity == "warn":
                        severity_label = t(
                            "cli.messages.issue_summary_severity_warn")
                    else:
                        severity_label = t(
                            "cli.messages.issue_summary_severity_info")
                    print(f"  [{severity_label}]")
                    for issue in severity_issues:
                        icon = severity_icon.get(issue["severity"], "")
                        bar_len = int(issue["function_count"] / stats["total_functions"]
                                      * 40) if stats["total_functions"] > 0 else 0
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
            zero_issues = [i for i in stats["issues"]
                           if i["function_count"] == 0]
            if zero_issues:
                print(t("cli.messages.issue_summary_zero_title"))
                for issue in zero_issues:
                    print(t("cli.messages.issue_summary_zero_item",
                          label=issue['label']))
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
                print(t("cli.messages.semgrep_lang_limit",
                      langs=", ".join(languages)))
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
                    print(t("cli.messages.semgrep_error", error=result.get(
                        'error', t("cli.messages.semgrep_unknown_error"))))
                else:
                    print(t("cli.messages.semgrep_scan_done",
                          count=result['total_findings']))
                    print(t("cli.messages.semgrep_saved",
                          count=result['saved_findings']))
                    print()
                    print(t("cli.messages.semgrep_save_hint"))

            elif args.semgrep_quick:
                # 快速扫描（只显示汇总）
                result = db.get_semgrep_summary(target_paths)

                if not result.get("success"):
                    print(t("cli.messages.semgrep_error", error=result.get(
                        'error', t("cli.messages.semgrep_unknown_error"))))
                else:
                    print(t("cli.messages.semgrep_quick_total",
                          count=result['total_findings']))
                    print()

                    # 按严重程度展示
                    if result.get("by_severity"):
                        print(t("cli.messages.semgrep_severity_dist"))
                        for sev in ["ERROR", "WARNING", "INFO"]:
                            count = result["by_severity"].get(sev, 0)
                            if count > 0:
                                icon = {
                                    "ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}[sev]
                                print(t("cli.messages.semgrep_severity_count",
                                      icon=icon, sev=sev, count=count))
                        print()

                    # 按语言展示
                    if result.get("by_language"):
                        print(t("cli.messages.semgrep_lang_dist"))
                        for lang, count in sorted(result["by_language"].items(), key=lambda x: x[1], reverse=True):
                            print(t("cli.messages.semgrep_lang_count",
                                  lang=lang, count=count))
                        print()

                    # Top 规则
                    if result.get("top_rules"):
                        print(t("cli.messages.semgrep_top_rules"))
                        for rule_id, stats in result["top_rules"][:10]:
                            sev_icon = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}[
                                stats.get("severity", "INFO")]
                            print(t("cli.messages.semgrep_rule_item",
                                  icon=sev_icon, rule=rule_id, count=stats['count']))
                            print(f"        {stats['message'][:80]}...")
                        print()

                if result.get("errors"):
                    print(t("cli.messages.semgrep_warning_count",
                          count=len(result['errors'])))

            else:
                # 详细扫描
                result = db.run_semgrep(
                    target_paths=target_paths or [db.workspace_root],
                    config=config,
                    languages=languages,
                    timeout=timeout,
                )

                if not result.get("success"):
                    print(t("cli.messages.semgrep_error", error=result.get(
                        'error', t("cli.messages.semgrep_unknown_error"))))
                else:
                    print(t("cli.messages.semgrep_scan_done",
                          count=result['total_findings']))
                    print()

                    # 按严重程度分组展示
                    severity_icon = {"ERROR": "[!]",
                                     "WARNING": "[~]", "INFO": "[i]"}

                    for sev in ["ERROR", "WARNING", "INFO"]:
                        sev_findings = [
                            f for f in result["results"] if f["severity"] == sev]
                        if sev_findings:
                            icon = severity_icon[sev]
                            if sev == "ERROR":
                                sev_label = t("cli.messages.semgrep_sev_error")
                            elif sev == "WARNING":
                                sev_label = t(
                                    "cli.messages.semgrep_sev_warning")
                            else:
                                sev_label = t("cli.messages.semgrep_sev_info")
                            print(t("cli.messages.semgrep_detail_title",
                                  label=sev_label, count=len(sev_findings)))
                            print()

                            for f in sev_findings[:15]:
                                print(f"    {icon} {f['rule_name']}")
                                print(t("cli.messages.semgrep_finding_file",
                                      file=f['path'], line=f['start_line']))
                                print(
                                    t("cli.messages.semgrep_finding_lang", lang=f['language']))
                                print(
                                    t("cli.messages.semgrep_finding_msg", msg=f['message'][:100]))
                                if f.get("fix"):
                                    print(
                                        t("cli.messages.semgrep_fix_hint", fix=f['fix'][:50]))
                                print()

                            if len(sev_findings) > 15:
                                print(t("cli.messages.semgrep_more",
                                      count=len(sev_findings) - 15))
                                print()

            print(t("cli.messages.semgrep_hint"))
            print()

        elif args.semgrep_stats:
            stats = db.get_semgrep_stats()
            print(t("cli.messages.semgrep_stats_title"))
            print(t("cli.messages.semgrep_stats_total",
                  count=stats['total_findings']))
            print()

            if stats["by_severity"]:
                print(t("cli.messages.semgrep_stats_by_sev"))
                for sev in ["ERROR", "WARNING", "INFO"]:
                    count = stats["by_severity"].get(sev, 0)
                    if count > 0:
                        icon = {"ERROR": "[!]",
                                "WARNING": "[~]", "INFO": "[i]"}[sev]
                        print(t("cli.messages.semgrep_severity_count",
                              icon=icon, sev=sev, count=count))
                print()

            if stats["by_language"]:
                print(t("cli.messages.semgrep_stats_by_lang"))
                for lang, count in sorted(stats["by_language"].items(), key=lambda x: x[1], reverse=True):
                    print(f"    {lang:<15s} {count:4d}")
                print()

            if stats["by_rule"]:
                print(t("cli.messages.semgrep_stats_top_rules"))
                for i, rule in enumerate(stats["by_rule"][:10], 1):
                    sev_icon = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}.get(
                        rule["severity"], "[?]")
                    print(
                        f"    #{i:2d} {sev_icon} {rule['rule_id'][:50]:<50s}  {rule['cnt']:3d}")
                print()

            if stats["by_symbol"]:
                print(t("cli.messages.semgrep_stats_top_symbols"))
                for i, sym in enumerate(stats["by_symbol"][:10], 1):
                    print(
                        f"    #{i:2d} {sym['symbol_qualified'][:60]:<60s}  {sym['cnt']:2d}")
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
                filter_parts.append(
                    t("cli.messages.semgrep_list_filter_sev", sev=severity))
            if language:
                filter_parts.append(
                    t("cli.messages.semgrep_list_filter_lang", lang=language))
            if rule_filter:
                filter_parts.append(
                    t("cli.messages.semgrep_list_filter_rule", rule=rule_filter))
            filter_str = " | ".join(filter_parts) if filter_parts else t(
                "cli.messages.semgrep_list_filter_all")

            print(t("cli.messages.semgrep_list_title", filter=filter_str,
                  total=len(findings), shown=min(limit, len(findings))))
            print()

            sev_icon_map = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}

            for i, f in enumerate(findings[:limit], 1):
                icon = sev_icon_map.get(f["severity"], "[?]")
                sym_info = f" -> {f['symbol_qualified']}" if f["symbol_qualified"] else ""
                print(
                    f"  #{i:3d} {icon} {f['rule_name'][:40]:<40s} {f['language']:<12s}{sym_info}")
                print(f"        {f['file_path']}:{f['start_line']}")
                print(f"        {f['message'][:80]}")
                print()

            if len(findings) > limit:
                print(t("cli.messages.semgrep_list_more",
                      count=len(findings) - limit))

        elif args.git_import is not None:
            max_commits = args.git_import if args.git_import else 100
            print(t("cli.messages.git_import_start", count=max_commits))
            result = db.import_git_history(max_commits=max_commits)
            if result.get("success"):
                print(t("cli.messages.git_import_success",
                      count=result['commits_imported']))
                print(t("cli.messages.git_import_total",
                      count=result['total_commits']))
            else:
                print(t("cli.messages.git_import_fail", error=result.get(
                    'error', t("cli.messages.semgrep_unknown_error"))))

        elif args.git_log is not None:
            limit = args.git_log if args.git_log else 20
            commits = db.get_git_commits(limit=limit)
            print(t("cli.messages.git_log_title", count=len(commits)))
            print()
            for c in commits:
                short_hash = c['commit_hash'][:8]
                timestamp = time.strftime(
                    '%Y-%m-%d %H:%M', time.localtime(c['timestamp']))
                msg = c['message'][:60] if c['message'] else t(
                    "cli.messages.git_log_no_msg")
                author = c['author'][:15] if c['author'] else 'unknown'
                print(f"  {short_hash}  {timestamp}  {author:<15s}  {msg}")

        elif args.git_show:
            details = db.get_commit_changes(args.git_show)
            commit = details.get("commit")
            if not commit:
                print(t("cli.messages.git_show_not_found", hash=args.git_show))
            else:
                print(t("cli.messages.git_show_commit",
                      hash=commit['commit_hash']))
                print(t("cli.messages.git_show_author",
                      author=commit['author'], email=commit['email']))
                print(t("cli.messages.git_show_time", time=time.strftime(
                    '%Y-%m-%d %H:%M:%S', time.localtime(commit['timestamp']))))
                print(t("cli.messages.git_show_message",
                      msg=commit['message']))
                print()
                file_changes = details.get("file_changes", [])
                print(t("cli.messages.git_show_files", count=len(file_changes)))
                type_map = {'A': t("cli.messages.git_type_added"), 'M': t("cli.messages.git_type_modified"), 'D': t(
                    "cli.messages.git_type_deleted"), 'R': t("cli.messages.git_type_renamed")}
                for fc in file_changes:
                    ct = fc.get('change_type', '?')
                    type_label = type_map.get(ct, ct)
                    path = fc.get('rel_path') or fc.get(
                        'abs_path') or 'unknown'
                    print(f"  [{type_label}] {path}")

        elif args.git_stats:
            stats = db.get_git_stats()
            print(t("cli.messages.git_stats_title"))
            print(t("cli.messages.git_stats_commits",
                  count=stats['commit_count']))
            print(t("cli.messages.git_stats_file_changes",
                  count=stats['file_change_count']))
            print()
            if stats.get("change_types"):
                print(t("cli.messages.git_stats_by_type"))
                type_map = {'A': t("cli.messages.git_type_added"), 'M': t("cli.messages.git_type_modified"), 'D': t(
                    "cli.messages.git_type_deleted"), 'R': t("cli.messages.git_type_renamed")}
                for ct, cnt in sorted(stats["change_types"].items(), key=lambda x: x[1], reverse=True):
                    label = type_map.get(ct, ct)
                    print(t("cli.messages.git_stats_type_count",
                          default="    {label}: {count} times", label=label, count=cnt))

        # ----------------------------------------------------------------
        # 代码度量
        # ----------------------------------------------------------------

        elif args.metrics:
            summary = db.get_code_metrics_summary()
            print(t("cli.messages.metrics_title"))
            print(t("cli.messages.metrics_files", count=summary['file_count']))
            print(t("cli.messages.metrics_functions",
                  count=summary['function_count']))
            print(t("cli.messages.metrics_total_lines",
                  count=summary['total_lines']))
            print(t("cli.messages.metrics_calls",
                  count=summary['total_calls']))
            print()
            print(t("cli.messages.metrics_avg_complexity",
                  value=summary['avg_complexity']))
            print(t("cli.messages.metrics_max_complexity",
                  value=summary['max_complexity']))
            print()
            print(t("cli.messages.metrics_complexity_dist"))
            dist = summary["complexity_distribution"]
            total_fn = sum(dist.values()) or 1
            for level, count in dist.items():
                pct = count / total_fn * 100
                bar = "#" * int(pct / 2)
                print(f"    {level:<12s} {count:4d} ({pct:5.1f}%) {bar}")
            print()
            print(t("cli.messages.metrics_comment_coverage",
                  pct=summary['comment_coverage']))

        elif args.complexity is not None:
            limit = args.complexity if args.complexity else 20
            mod_filter = args.complexity_module or ""
            hotspots = db.get_complexity_hotspots(
                limit=limit, module_filter=mod_filter)

            filter_info = t("cli.messages.complexity_filter",
                            module=mod_filter) if mod_filter else ""
            print(t("cli.messages.complexity_title",
                  filter_info=filter_info, count=len(hotspots)))
            print()
            complexity_h = t("cli.messages.col_complexity",
                             default="Complexity")
            lines_h = t("cli.messages.col_lines", default="Lines")
            depth_h = t("cli.messages.col_depth", default="Depth")
            fn_h = t("cli.messages.col_function", default="Function")
            print(
                f"  {'#':>3}  {complexity_h:>6}  {lines_h:>5}  {depth_h:>4}  {fn_h}")
            print(f"  {'-'*3}  {'-'*6}  {'-'*5}  {'-'*4}  {'-'*50}")

            for i, fn in enumerate(hotspots, 1):
                risk = "!" if fn["cyclomatic_complexity"] > 10 else " "
                print(
                    f"  {i:3d}{risk}  {fn['cyclomatic_complexity']:>6}  {fn['line_count']:>5}  {fn['depth']:>4}  {fn['qualified_name'][:60]}")
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
            print(
                f"  {'#':>3}  {module_h:<40s}  {afferent_h:>4}  {efferent_h:>4}  {total_h:>4}  {instability_h:>6}")
            print(f"  {'-'*3}  {'-'*40}  {'-'*4}  {'-'*4}  {'-'*4}  {'-'*6}")

            for i, mod in enumerate(modules, 1):
                inst = mod["instability"]
                inst_label = f"{inst:.2f}"
                if inst > 0.7:
                    inst_label += t("cli.messages.coupling_unstable")
                elif inst < 0.3:
                    inst_label += t("cli.messages.coupling_stable")
                print(
                    f"  {i:3d}  {mod['module'][:40]:<40s}  {mod['afferent']:>4}  {mod['efferent']:>4}  {mod['total_coupling']:>4}  {inst_label:>6}")

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
                print(
                    f"  {i:3d}  {fn['line_count']:>5}  {fn['depth']:>4}  {fn['qualified_name'][:60]}")
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
            print(
                f"  {'#':>3}  {fan_in_h:>4}  {fan_out_h:>4}  {total_h:>4}  {fn_h}")
            print(f"  {'-'*3}  {'-'*4}  {'-'*4}  {'-'*4}  {'-'*50}")

            for i, fn in enumerate(fns, 1):
                print(
                    f"  {i:3d}  {fn['fan_in']:>4}  {fn['fan_out']:>4}  {fn['total_coupling']:>4}  {fn['qualified_name'][:60]}")
                print(f"        {fn['file_path']}")

        elif args.fn_metrics:
            metrics = db.get_function_metrics(args.fn_metrics)
            if not metrics:
                print(t("cli.messages.fn_metrics_not_found", name=args.fn_metrics))
                print(t("cli.messages.fn_metrics_search_hint"))
            else:
                print(t("cli.messages.fn_metrics_title",
                      name=metrics['qualified_name']))
                print(t("cli.messages.fn_metrics_kind", kind=metrics['kind']))
                print(t("cli.messages.fn_metrics_file",
                      file=metrics['file_path'], start=metrics['start_line'], end=metrics['end_line']))
                print(t("cli.messages.fn_metrics_lines",
                      count=metrics['line_count']))
                print(t("cli.messages.fn_metrics_complexity",
                      value=metrics['cyclomatic_complexity'], risk=metrics['risk_level']))
                print(t("cli.messages.fn_metrics_fan_in",
                      count=metrics['fan_in']))
                print(t("cli.messages.fn_metrics_fan_out",
                      count=metrics['fan_out']))
                print(t("cli.messages.fn_metrics_depth",
                      depth=metrics['depth']))
                print(t("cli.messages.fn_metrics_module",
                      module=metrics['module_path']))
                if metrics['signature']:
                    print(t("cli.messages.fn_metrics_signature",
                          sig=metrics['signature'][:100]))

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
                    print(t("cli.messages.semantic_similarity", idx=i,
                          value=r['similarity'], name=r['qualified_name']))
                    print(t("cli.messages.semantic_location",
                          file=r['file_path'], line=r['start_line']))
                    if r.get('summary'):
                        print(t("cli.messages.semantic_summary",
                              summary=r['summary'][:80]))
            print()

        elif args.embed or args.embed_force:
            force = args.embed_force
            mode = t("cli.messages.embed_mode_force") if force else t(
                "cli.messages.embed_mode_incremental")
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
                    print(t("cli.messages.semantic_similarity", idx=i,
                          value=r['similarity'], name=r['qualified_name']))
                    print(t("cli.messages.semantic_location",
                          file=r['file_path'], line=r['start_line']))
                    if r.get('summary'):
                        print(t("cli.messages.semantic_summary",
                              summary=r['summary'][:80]))
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
            print(t("cli.messages.brief_project_type",
                  type=brief['project_type']))
            print(t("cli.messages.brief_files", count=brief['file_count']))
            print(t("cli.messages.brief_functions",
                  count=brief['function_count']))
            print(t("cli.messages.brief_total_lines",
                  count=brief['total_lines']))
            print(t("cli.messages.brief_health",
                  score=brief['health_score'], level=brief['health_level']))
            print(t("cli.messages.brief_avg_complexity",
                  value=brief['avg_complexity']))
            print(t("cli.messages.brief_comment_coverage",
                  pct=brief['comment_coverage']))
            print()
            modules = brief.get('modules', [])
            if modules:
                print(t("cli.messages.brief_modules", count=len(modules)))
                for i, m in enumerate(modules, 1):
                    print(t("cli.messages.brief_module_item", idx=i,
                          module=m['module'], count=m['function_count']))
                print()
            hotspots = brief.get('hot_functions', [])
            if hotspots:
                print(t("cli.messages.brief_hotspots", count=len(hotspots)))
                for i, fn in enumerate(hotspots, 1):
                    print(t("cli.messages.brief_hotspot_item", idx=i,
                          value=fn['cyclomatic_complexity'], name=fn['qualified_name']))
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
            print(t("cli.messages.coverage_import_title",
                  file=file_path, format=fmt))
            print("-" * 50)
            try:
                if fmt == "lcov":
                    stats = db.import_lcov(file_path)
                else:
                    stats = db.import_cobertura(file_path)
                print(t("cli.messages.coverage_import_files_total",
                      count=stats['files_total']))
                print(t("cli.messages.coverage_import_files_matched",
                      count=stats['files_matched']))
                print(t("cli.messages.coverage_import_lines",
                      count=stats['lines_imported']))
                print(t("cli.messages.coverage_import_symbols",
                      count=stats['symbols_matched']))
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
                print(t("cli.messages.coverage_fn_title",
                      name=info['qualified_name']))
                print("-" * 50)
                print(t("cli.messages.coverage_fn_file",
                      file=info['file_path'], start=info['start_line'], end=info['end_line']))
                print(t("cli.messages.coverage_fn_total",
                      count=info['total_lines']))
                print(t("cli.messages.coverage_fn_tracked",
                      count=info['tracked_lines']))
                print(t("cli.messages.coverage_fn_covered",
                      count=info['covered_lines']))
                print(t("cli.messages.coverage_fn_pct",
                      pct=info['coverage_pct']))
                if info['uncovered_lines']:
                    lines_preview = info['uncovered_lines'][:30]
                    more = '...' if len(info['uncovered_lines']) > 30 else ''
                    print(t("cli.messages.coverage_fn_uncovered",
                          lines=lines_preview, more=more))
            print()

        elif args.coverage_uncovered:
            results = db.find_uncovered_functions()
            print(t("cli.messages.coverage_uncovered_title", count=len(results)))
            print("-" * 50)
            for i, r in enumerate(results, 1):
                pct_label = t("cli.messages.coverage_fn_pct",
                              pct="").strip().rstrip(":").strip()
                print(
                    f"  [{i:3d}] {pct_label}={r['coverage_pct']:5.1f}%  {r['qualified_name']}")
                print(t("cli.messages.coverage_uncovered_item",
                      file=r['file_path'], start=r['start_line'], end=r['end_line'], covered=r['covered_lines'], tracked=r['tracked_lines']))
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
                print(t("cli.messages.who_confidence",
                      confidence=info['confidence']))
                if info.get('last_commit_author'):
                    print(t("cli.messages.who_last_author",
                          author=info['last_commit_author']))
                if info.get('last_commit_time'):
                    ts = time.strftime('%Y-%m-%d %H:%M:%S',
                                       time.localtime(info['last_commit_time']))
                    print(t("cli.messages.who_last_time", time=ts))
                if info.get('last_commit_hash'):
                    print(t("cli.messages.who_last_hash",
                          hash=info['last_commit_hash'][:12]))
            print()

        elif args.ownership_map:
            results = db.get_ownership_map()
            print(t("cli.messages.ownership_map_title", count=len(results)))
            print("-" * 50)
            for i, m in enumerate(results, 1):
                print(f"  [{i}] {m['module']}")
                print(t("cli.messages.ownership_map_primary",
                      owner=m['primary_owner'], count=m['file_count']))
                owners_str = ", ".join(
                    f"{o['name']}({o['file_count']})" for o in m['owners'][:5])
                print(t("cli.messages.ownership_map_dist", owners=owners_str))
                if len(m['owners']) > 5:
                    print(t("cli.messages.ownership_map_more",
                          count=len(m['owners']) - 5))
            print()

        else:
            parser.print_help()

    except sqlite3.OperationalError as e:
        # 锁错误友好提示（写命令执行 SQL 时也可能遇到锁）
        if "locked" in str(e).lower():
            cprint(get_error("db_locked"), "red")
            sys.exit(2)
        raise
    finally:
        db.close()


# ============================================
# 角色化入口（cw-client / cw-agent / cw-daemon）
# ============================================


def run_client_mode(argv: list) -> int:
    """cw-client 入口：仅 RPC proxy，不含 parser 和本地 DB 写能力。

    角色定位（与 `cw daemon` 的差异）：
    - `cw daemon`：daemon 管理员视角，含 `serve` 启动 daemon 本身
    - `cw-client`：纯 client 视角，禁止 `serve`，只做 RPC 调用

    平台门禁：非 Linux 直接 return 2，与 cw-daemon / cw-agent 一致
    （UDS + SCM_RIGHTS 是 Linux 特有，Windows/macOS 上 daemon 不可用）。

    Phase 5-2 Slice 7：wire-production 路由整合
    ------------------------------------------------------------
    默认走 Python `run_daemon_command`（保持真相源 + 差分基线稳定）。
    设置 `CW_USE_RUST_CLIENT=1` 时探测 Rust cw-client binary，存在则 exec，
    不存在或执行失败时降级回 Python（fail-soft）。

    回滚机制：
    - 清除 `CW_USE_RUST_CLIENT` 环境变量即回滚到 Python（即时生效）
    - rollback_config 表已登记此功能的回滚入口（见 wire-production 文档）
    - rollback_flag=1 时强制走 Python（通过 `is_feature_rolled_back` 查询）

    实现委托 `run_daemon_command(argv, include_serve=False)`，复用 daemon_commands
    已实现的 31 个 RPC 方法 CLI 调用（ping/register/list/status/publish/query/
    health/schema-version/backup/restore/gc-cas/gc-snapshots/mount/toolchain/mode）。
    """
    import os as _os
    import sys as _sys
    if _sys.platform not in ("linux", "win32", "darwin"):
        print("ERROR: cw-client is only supported on Linux/Windows/macOS.",
              file=_sys.stderr)
        return 2

    # 无参数时打印简介 + 帮助提示
    if not argv:
        print("Call Warden Client Mode")
        print("  Connects to Enterprise Daemon via UDS/NamedPipe")
        print("  No local parser or CAS write capability")
        print("  Subcommands: ping, register, list, status, publish, query,")
        print("               health, schema-version, backup, restore,")
        print("               gc-cas, gc-snapshots, mount, toolchain, mode")
        print("  Use 'cw-client --help' for details.")
        return 0

    # Phase 5-2 Slice 7: Rust 加速路径（环境变量开关，默认 Python）
    if _os.environ.get("CW_USE_RUST_CLIENT") == "1":
        rc = _try_exec_rust_cw_client(argv)
        if rc is not None:
            return rc
        # Rust binary 不可用或执行失败，降级回 Python（fail-soft）
        print("WARNING: CW_USE_RUST_CLIENT=1 but Rust cw-client unavailable; "
              "falling back to Python run_daemon_command",
              file=_sys.stderr)

    from callwarden.cli.daemon_commands import run_daemon_command
    return run_daemon_command(argv, include_serve=False)


def _try_exec_rust_cw_client(argv: list):
    """Phase 5-2 Slice 7: 尝试 exec Rust cw-client binary。

    返回值：
    - int: Rust binary 执行的退出码（成功 exec 时）
    - None: Rust binary 不可用或 exec 失败（应降级回 Python）
    """
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    binary = _find_cw_client_binary()
    if binary is None:
        return None

    # 透传环境变量 + 参数
    env = _os.environ.copy()
    try:
        proc = _subprocess.run([str(binary), *argv], env=env)
        return proc.returncode
    except OSError as e:
        print(f"WARNING: failed to exec Rust cw-client binary: {e}",
              file=_sys.stderr)
        return None


def _find_cw_client_binary():
    """Phase 5-2 Slice 7: 查找 Rust cw-client 二进制。

    查找顺序（与 `_find_cw_daemon_binary` 对齐）：
    1. CW_CLIENT_BIN 环境变量（显式覆盖）
    2. PATH 中的 cw-client（生产安装，连字符命名）
    3. rust_ext/target/release/cw-client（cargo build --release）
    4. rust_ext/target/debug/cw-client（cargo build）

    返回 Path 或 None。
    """
    import os as _os
    import shutil as _shutil
    import sys as _sys
    from pathlib import Path as _Path

    names = ["cw-client.exe", "cw_client.exe"] if _sys.platform == "win32" else ["cw-client", "cw_client"]

    # 1. 显式环境变量覆盖
    env_bin = _os.environ.get("CW_CLIENT_BIN")
    if env_bin and _os.path.isfile(env_bin):
        return _Path(env_bin)

    # 2. PATH 查找
    for name in names:
        found = _shutil.which(name)
        if found:
            return _Path(found)

    # PyInstaller 冻结包内的 binary
    if getattr(_sys, "frozen", False):
        roots = []
        meipass = getattr(_sys, "_MEIPASS", None)
        if meipass:
            roots.append(_Path(meipass))
        roots.append(_Path(_sys.executable).resolve().parent)
        for root in roots:
            for name in names:
                candidate = root / name
                if candidate.is_file():
                    return candidate

    # 3./4. 开发构建路径（仓库根目录下的 rust_ext/target/）
    try:
        root = _Path(__file__).resolve().parent.parent
        rust_target = root / "rust_ext" / "target"
        for profile in ("release", "debug"):
            for name in names:
                candidate = rust_target / profile / name
                if candidate.is_file():
                    return candidate
    except Exception:
        pass

    return None


def run_agent_mode(argv: list) -> int:
    """cw-agent 入口：per-UID watcher agent。

    子命令：
    - start [--watch-dir DIR] [--workspace-id ID]：启动 watcher（前台运行）
    - stop：发送 SIGTERM 停止运行中的 agent
    - status：查询 agent 运行状态
    """
    import os as _os
    import sys as _sys
    if _sys.platform not in ("linux", "win32", "darwin"):
        print("ERROR: cw-agent is only supported on Linux/Windows/macOS.", file=_sys.stderr)
        return 2

    if not argv:
        _print_agent_usage()
        return 0

    cmd = argv[0]
    rest = argv[1:]

    if cmd in {"-h", "--help"}:
        _print_agent_usage()
        return 0
    if cmd == "start":
        return _agent_start(rest)
    if cmd == "stop":
        return _agent_stop(rest)
    if cmd == "status":
        return _agent_status(rest)
    print(f"ERROR: unknown command: {cmd}", file=_sys.stderr)
    _print_agent_usage()
    return 2


def _print_agent_usage() -> None:
    """打印 cw-agent 用法。"""
    print("Call Warden Agent Mode")
    print("  Per-UID file watcher → IPC → Enterprise Daemon")
    print()
    print("Usage: cw-agent <command> [options]")
    print()
    print("Commands:")
    print("  start [--watch-dir DIR] [--workspace-id ID]")
    print("          启动 watcher")
    print("  stop    停止运行中的 agent（读取 PID 文件，发送 SIGTERM）")
    print("  status  查询 agent 运行状态")


def _agent_pid_file() -> str:
    """agent PID 文件路径（per-UID）：~/.callwarden/agent.pid。"""
    import os as _os
    return _os.path.join(
        _os.path.expanduser("~"), ".callwarden", "agent.pid",
    )


def _agent_log_file() -> str:
    """agent 日志文件路径：~/.callwarden/agent.log。"""
    import os as _os
    return _os.path.join(
        _os.path.expanduser("~"), ".callwarden", "agent.log",
    )


def _parse_agent_start_args(argv: list) -> dict:
    """解析 `cw-agent start` 参数。"""
    import os as _os
    opts = {
        "watch_dir": _os.getcwd(),
        "workspace_id": None,
        "unknown": [],
        "help": False,
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--watch-dir" and i + 1 < len(argv):
            opts["watch_dir"] = argv[i + 1]
            i += 2
        elif arg == "--workspace-id" and i + 1 < len(argv):
            opts["workspace_id"] = argv[i + 1]
            i += 2
        elif arg == "--help" or arg == "-h":
            opts["help"] = True
            i += 1
        else:
            opts["unknown"].append(arg)
            i += 1
    return opts


def _agent_start(argv: list) -> int:
    """cw-agent start 实现：启动 watcher 主循环。"""
    import os as _os
    import sys as _sys
    import signal as _signal
    import threading as _threading
    import logging as _logging

    opts = _parse_agent_start_args(argv)
    if opts.get("help"):
        print("Usage: cw-agent start [--watch-dir DIR] [--workspace-id ID]")
        print()
        print("Options:")
        print("  --watch-dir DIR       监控目录（默认当前工作目录）")
        print("  --workspace-id ID     workspace_instance_id（默认从 watch-dir 推导）")
        return 0

    watch_dir = _os.path.abspath(opts["watch_dir"])
    if not _os.path.isdir(watch_dir):
        print(f"ERROR: watch-dir 不存在：{watch_dir}", file=_sys.stderr)
        return 2

    # 配置日志（写入 ~/.callwarden/agent.log）
    log_file = _agent_log_file()
    _os.makedirs(_os.path.dirname(log_file), exist_ok=True)
    _logging.basicConfig(
        filename=log_file,
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    console = _logging.StreamHandler()
    console.setLevel(_logging.INFO)
    console.setFormatter(_logging.Formatter("%(levelname)s: %(message)s"))
    _logging.getLogger().addHandler(console)

    # 1. 加载或创建 AgentSession
    from callwarden.server.agent_session import AgentSession
    session = AgentSession.create_or_load()
    _logging.info("agent session 加载：%s", session)

    # 2. 推导 workspace_instance_id
    workspace_id = opts["workspace_id"]
    if not workspace_id:
        from callwarden.server.daemon_client import derive_workspace_instance_id
        workspace_id = derive_workspace_instance_id(watch_dir)
    _logging.info("workspace_instance_id=%s", workspace_id)

    # 3. 写 PID 文件
    pid_file = _agent_pid_file()
    _os.makedirs(_os.path.dirname(pid_file), exist_ok=True)
    with open(pid_file, "w") as f:
        f.write(str(_os.getpid()))
    _logging.info("PID 文件：%s", pid_file)

    # 4. 与 daemon 握手（user_agent_connect）
    from callwarden.server.daemon_client import UnixDaemonRpcClient
    from callwarden.server.agent_protocol import (
        user_agent_connect, user_agent_ping, AgentProtocolError,
    )
    from callwarden.config import get_default_daemon_endpoint
    rpc_client = UnixDaemonRpcClient(socket_path=get_default_daemon_endpoint())

    try:
        ping_resp = user_agent_ping(rpc_client)
        _logging.info(
            "daemon ping OK：peer_uid=%s pid=%s",
            ping_resp.get("peer_uid"), ping_resp.get("pid"),
        )
    except AgentProtocolError as e:
        _logging.error("daemon 不可达：%s", e)
        try:
            _os.remove(pid_file)
        except OSError:
            pass
        return 2

    try:
        epoch = user_agent_connect(rpc_client, workspace_id, session)
        _logging.info("session_epoch=%d", epoch)
    except AgentProtocolError as e:
        _logging.error("握手失败：%s", e)
        try:
            _os.remove(pid_file)
        except OSError:
            pass
        return 2

    # 5. 注册 workspace（如果尚未注册）
    try:
        rpc_client.call("workspace.register", {
            "client_view_root": watch_dir,
        })
    except Exception as e:
        _logging.warning("workspace.register 失败（可能已注册）：%s", e)

    # 6. 加载支持的扩展名集合
    from callwarden.config import get_supported_extensions
    supported_exts = get_supported_extensions()
    _logging.info("支持的扩展名：%d 个", len(supported_exts))

    # 7. 启动 watcher 主循环
    from callwarden.server.agent_watcher import (
        run_agent_watcher_loop, HAS_WATCHDOG,
    )
    if not HAS_WATCHDOG:
        _logging.error("watchdog 未安装")
        try:
            _os.remove(pid_file)
        except OSError:
            pass
        return 2

    stop_event = _threading.Event()

    def _signal_handler(signum, frame):
        _logging.info("收到信号 %d，准备退出", signum)
        stop_event.set()

    if hasattr(_signal, "SIGTERM"):
        _signal.signal(_signal.SIGTERM, _signal_handler)
    _signal.signal(_signal.SIGINT, _signal_handler)

    try:
        return run_agent_watcher_loop(
            agent_session=session,
            daemon_rpc_client=rpc_client,
            workspace_instance_id=workspace_id,
            watch_dir=watch_dir,
            supported_exts=supported_exts,
            stop_event=stop_event,
        )
    finally:
        try:
            _os.remove(pid_file)
        except OSError:
            pass
        _logging.info("agent 退出")


def _agent_stop(argv: list) -> int:
    """cw-agent stop 实现：发送 SIGTERM 停止运行中的 agent。"""
    import os as _os
    import sys as _sys
    pid_file = _agent_pid_file()
    if not _os.path.isfile(pid_file):
        print("agent 未运行（PID 文件不存在）")
        return 1
    try:
        with open(pid_file, "r") as f:
            pid = int(f.read().strip())
    except (ValueError, OSError) as e:
        print(f"ERROR: 读取 PID 文件失败：{e}", file=_sys.stderr)
        return 1
    try:
        import signal as _signal
        sig = getattr(_signal, "SIGTERM", _signal.SIGINT)
        _os.kill(pid, sig)
        print(f"已发送 停止信号 到 PID {pid}")
        import time as _time
        for _ in range(50):
            if not _os.path.isfile(pid_file):
                print("agent 已停止")
                return 0
            _time.sleep(0.1)
        print("WARNING: agent 5 秒内未退出", file=_sys.stderr)
        return 1
    except ProcessLookupError:
        print(f"PID {pid} 不存在，清理 PID 文件")
        try:
            _os.remove(pid_file)
        except OSError:
            pass
        return 0
    except PermissionError as e:
        print(f"ERROR: 无权限发送信号：{e}", file=_sys.stderr)
        return 1


def _agent_status(argv: list) -> int:
    """cw-agent status 实现：查询 agent 运行状态。"""
    import os as _os
    import json as _json
    pid_file = _agent_pid_file()
    print("=== Call Warden Agent Status ===")
    print(f"PID 文件: {pid_file}")
    if not _os.path.isfile(pid_file):
        print("状态: 未运行")
    else:
        try:
            with open(pid_file, "r") as f:
                pid = int(f.read().strip())
            print(f"PID: {pid}")
            try:
                _os.kill(pid, 0)
                print("状态: 运行中")
            except ProcessLookupError:
                print("状态: PID 不存在（PID 文件过期）")
            except PermissionError:
                print("状态: 运行中（无权限检查 PID）")
        except (ValueError, OSError) as e:
            print(f"状态: PID 文件损坏（{e}）")

    from callwarden.server.agent_session import (
        AgentSession, DEFAULT_AGENT_SESSION_FILE,
    )
    print(f"\nSession 文件: {DEFAULT_AGENT_SESSION_FILE}")
    if _os.path.isfile(DEFAULT_AGENT_SESSION_FILE):
        try:
            with open(DEFAULT_AGENT_SESSION_FILE, "r", encoding="utf-8") as f:
                data = _json.load(f)
            print(f"  session_id: {data.get('session_id')}")
            print(f"  workspaces: {len(data.get('workspaces') or {})}")
            for ws_id, ws_data in (data.get("workspaces") or {}).items():
                print(f"    - {ws_id}: epoch={ws_data.get('epoch')}, "
                      f"seq={ws_data.get('seq_counter')}")
        except (ValueError, OSError) as e:
            print(f"  解析失败：{e}")
    else:
        print("  （无 session 文件）")

    print("\n=== Daemon 连接 ===")
    try:
        from callwarden.server.daemon_client import UnixDaemonRpcClient
        from callwarden.config import get_default_daemon_endpoint
        rpc = UnixDaemonRpcClient(socket_path=get_default_daemon_endpoint())
        resp = rpc.call("ping")
        print(f"daemon 状态: {resp.get('status', 'unknown')}")
        print(f"  peer_uid: {resp.get('peer_uid')}")
        print(f"  pid: {resp.get('pid')}")
    except Exception as e:
        print(f"daemon 不可达：{e}")
    return 0


def run_daemon_mode(argv: list) -> int:
    """cw-daemon 入口：system daemon。

    R7: 优先调度已安装的 Rust cw_daemon 二进制（生产路径），找不到时回退到
    rust_ext/target/{release,debug}/cw_daemon（开发路径）。两者都不可用时
    打印错误并返回退出码 2。
    """
    import os as _os
    import shutil as _shutil
    import sys as _sys
    import subprocess as _subprocess
    if _sys.platform not in ("linux", "win32", "darwin"):
        print("ERROR: cw-daemon is only supported on Linux/Windows/macOS.", file=_sys.stderr)
        return 2

    binary = _find_cw_daemon_binary()
    if binary is None:
        print(
            "ERROR: cw_daemon binary not found.\n"
            "  Production: install callwarden-daemon package (provides cw-daemon).\n"
            "  Development: run `cargo build --no-default-features --bin cw_daemon` "
            "in rust_ext/ first.\n"
            "  Or set CW_DAEMON_BIN env var to the binary path.",
            file=_sys.stderr,
        )
        return 2

    # exec Rust binary，透传所有参数和环境变量
    try:
        env = _os.environ.copy()
        env.setdefault("CW_DAEMON_MODE", "enterprise")
        proc = _subprocess.run([str(binary), *argv], env=env)
        return proc.returncode
    except OSError as e:
        print(f"ERROR: failed to exec cw_daemon binary: {e}", file=_sys.stderr)
        return 1


def _find_cw_daemon_binary():
    """R7: 查找 cw_daemon Rust 二进制。

    查找顺序：
    1. CW_DAEMON_BIN 环境变量（显式覆盖）
    2. PATH 中的 cw-daemon / cw_daemon
    3. rust_ext/target/release/cw_daemon
    4. rust_ext/target/debug/cw_daemon

    返回 Path 或 None。
    """
    import os as _os
    import shutil as _shutil
    import sys as _sys
    from pathlib import Path as _Path

    names = ("cw-daemon.exe", "cw_daemon.exe") if _sys.platform == "win32" else ("cw-daemon", "cw_daemon")

    # 1. 显式环境变量覆盖
    env_bin = _os.environ.get("CW_DAEMON_BIN")
    if env_bin and _os.path.isfile(env_bin):
        return _Path(env_bin)

    # 2. PATH 查找
    for name in names:
        found = _shutil.which(name)
        if found:
            return _Path(found)

    # 冻结 one-dir 包内的 daemon 二进制。
    if getattr(_sys, "frozen", False):
        roots = []
        meipass = getattr(_sys, "_MEIPASS", None)
        if meipass:
            roots.append(_Path(meipass))
        roots.append(_Path(_sys.executable).resolve().parent)
        for root in roots:
            for name in names:
                candidate = root / name
                if candidate.is_file():
                    return candidate

    # 3./4. 开发构建路径（仓库根目录下的 rust_ext/target/）
    try:
        root = _Path(__file__).resolve().parent.parent
        rust_target = root / "rust_ext" / "target"
        for profile in ("release", "debug"):
            for name in names:
                candidate = rust_target / profile / name
                if candidate.is_file():
                    return candidate
    except Exception:
        pass

    return None


# --------------------------------------------------------------------
# experiment：P0 盲评对照实验命令组（Requirement 12）
# --------------------------------------------------------------------


def _handle_experiment(args, db):
    """处理 experiment 子命令（P0 盲评对照实验全生命周期操作）

    子命令：
        batch-create     创建批次 + 默认协议 + 锁定
        batch-lock       冻结协议（手动补锁）
        batch-list       列出所有已登记批次
        toggle-set       设置 P0 Stage_Toggle
        toggle-show      显示解析后的 P0 开关
        admit            纳样（资格检查→分组→blind view→JSONL）
        record-metrics   记录 review 原始指标
        record-verdict   记录 reveal 前后 verdict 变更
        record-reveal    记录 Implementer_Notes 揭示事件
        record-invalid   记录无效样本
        record-incident  记录披露/完整性事件
        pause            手动暂停批次
        report           汇总评估 + 机器可读 G0 决策
    """
    # 延迟导入 experiments 包（避免非 experiment 命令的导入开销）
    from datetime import datetime, timezone
    from ..experiments.blind_review_protocol import (
        Experiment_Batch_Config, ExperimentBatch, build_default_protocol,
        ExperimentProtocolError, BatchStatus, ToggleScope, ToggleValue, PauseTrigger,
        SuccessThresholds, GroupAssignment,
    )
    from ..experiments.blind_review_views import (
        ViewDisclosureError, build_minimal_blind_view, build_verdict_change_record,
        BlindViewGroup, BlindViewPhase, collect_source_facts_from_db,
        ViewErrorCode, make_view_reason,
    )
    from ..experiments.blind_review_jsonl import (
        ExperimentJsonlWriter, build_blind_view_record,
        build_review_metrics_record,
        build_reveal_event_record, build_invalid_sample_record,
        build_incident_record, canonical_incident_record_type, write_evidence_bundle,
    )
    from ..experiments.blind_review_evaluator import (
        EvaluatorError, compute_group_metrics, evaluate_success,
        evaluate_gray_zone, evaluate_pause_conditions,
        build_evaluation_report, SampleRecord, validate_blind_view_records,
        make_evaluator_reason, EvaluatorErrorCode,
    )

    parser = argparse.ArgumentParser(
        prog="cw experiment",
        description=t("cli_experiment_desc",
                      default="P0 blind-review controlled experiment management"),
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # --- batch-create ---
    bc_p = sub.add_parser("batch-create", help=t(
        "cli_experiment_batch_create_desc",
        default="Create a new experiment batch with default protocol and lock it"))
    bc_p.add_argument("--seed", type=int, required=True,
                      help="Random seed for deterministic group assignment")
    bc_p.add_argument("--min-valid", type=int, default=30,
                      help="Minimum valid tasks for success evaluation (default 30)")
    bc_p.add_argument("--min-nontrivial", type=int, default=10,
                      help="Minimum non-trivial code changes (default 10)")
    bc_p.add_argument("--assignment-mode", choices=["hash", "paired", "paired_v2"], default="hash",
                      help="Group assignment mode; paired_v2 requires pair-id and pair-slot")
    bc_p.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    # --- batch-lock ---
    bl_p = sub.add_parser("batch-lock", help=t(
        "cli_experiment_batch_lock_desc",
        default="Freeze/lock the protocol of an existing batch"))
    bl_p.add_argument("batch_id", help="Batch ID to lock")
    bl_p.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    # --- batch-list ---
    blist_p = sub.add_parser("batch-list", help=t(
        "cli_experiment_batch_list_desc",
        default="List all registered experiment batches"))
    blist_p.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    # --- toggle-set ---
    ts_p = sub.add_parser("toggle-set", help=t(
        "cli_experiment_toggle_set_desc",
        default="Set P0 Stage_Toggle value for a scope"))
    ts_p.add_argument("--scope", required=True, choices=["global", "workspace", "task"],
                      help="Toggle scope")
    ts_p.add_argument("--value", required=True, choices=["on", "off"],
                      help="Toggle value")
    ts_p.add_argument("--scope-key", default=None,
                      help="Scope key (workspace_id or task_id; required for workspace/task)")
    ts_p.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    # --- toggle-show ---
    tshow_p = sub.add_parser("toggle-show", help=t(
        "cli_experiment_toggle_show_desc",
        default="Show resolved P0 toggle (task > workspace > global inheritance)"))
    tshow_p.add_argument("--task-id", default=None, help="Task ID for task-level lookup")
    tshow_p.add_argument("--workspace-id", default=None, help="Workspace ID for workspace-level lookup")
    tshow_p.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    # --- admit ---
    adm_p = sub.add_parser("admit", help=t(
        "cli_experiment_admit_desc",
        default="Admit a task into the experiment (eligibility check, group assignment, blind view, JSONL)"))
    adm_p.add_argument("task_id", help="Task ID to admit")
    adm_p.add_argument("batch_id", help="Batch ID to admit into")
    adm_p.add_argument("--strata", default="", help="Stratification key for group assignment")
    adm_p.add_argument("--pair-slot", type=int, choices=[0, 1], default=None,
                      help="Paired mode slot; same strata must use one 0 and one 1")
    adm_p.add_argument("--pair-id", default=None,
                      help="Paired mode unique pair identifier shared by exactly two tasks")
    adm_p.add_argument("--notes-file", default=None,
                      help="Control 组 Implementer_Notes UTF-8 文件；Treatment 不得提供")
    adm_p.add_argument("--scope-contract", default=None,
                      help="新批次纳样必填范围契约 JSON；声明 profile/required_paths/allowed_paths")
    adm_p.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    # --- record-metrics ---
    rm_p = sub.add_parser("record-metrics", help=t(
        "cli_experiment_record_metrics_desc",
        default="Record review metrics (TP/FP/misses/duration/tokens/reopen/defects)"))
    rm_p.add_argument("task_id", help="Task ID")
    rm_p.add_argument("batch_id", help="Batch ID")
    rm_p.add_argument("--tp", type=int, required=True, help="Verified true positives")
    rm_p.add_argument("--fp", type=int, required=True, help="Verified false positives")
    rm_p.add_argument("--misses", type=int, required=True, help="Verified misses")
    rm_p.add_argument("--duration", type=float, required=True, help="Review duration (seconds)")
    rm_p.add_argument("--tokens", type=int, default=None,
                      help="真实 token usage；必须与 --tokens-source real 一起使用")
    rm_p.add_argument("--tokens-source", choices=["real", "unavailable"], required=True,
                      help="token 来源：real 为实际 provider 计数，unavailable 为无法采集")
    rm_p.add_argument("--tokens-unavailable-reason", default=None,
                      help="tokens-source=unavailable 时的非空原因")
    rm_p.add_argument("--reopen", type=int, default=0, help="Reopen events count")
    rm_p.add_argument("--defects", type=int, default=0, help="Post-apply defects")
    rm_p.add_argument("--rollbacks", type=int, default=0, help="Post-apply rollbacks")
    rm_p.add_argument("--obs-window", default="", help="Observation window ID")
    rm_p.add_argument("--group", choices=["control", "treatment"], default=None,
                      help="Review group; inferred from the admitted blind view when omitted")
    rm_p.add_argument("--nontrivial", action="store_true",
                      help="Mark this sample as a non-trivial code_change for the G0 threshold")
    rm_p.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    # --- record-verdict ---
    rv_p = sub.add_parser("record-verdict", help=t(
        "cli_experiment_record_verdict_desc",
        default="Record verdict change before/after reveal (Req 12.7)"))
    rv_p.add_argument("task_id", help="Task ID")
    rv_p.add_argument("batch_id", help="Batch ID")
    rv_p.add_argument("--changed", required=True, choices=["yes", "no"],
                      help="Whether verdict changed after reveal")
    rv_p.add_argument("--reason-code", default="no_change",
                      help="Verdict change reason (no_change/new_fact/corrected_misunderstanding)")
    rv_p.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    # --- record-reveal ---
    rr_p = sub.add_parser("record-reveal", help=t(
        "cli_experiment_record_reveal_desc",
        default="Record Implementer_Notes reveal event (Req 12.7)"))
    rr_p.add_argument("task_id", help="Task ID")
    rr_p.add_argument("batch_id", help="Batch ID")
    rr_p.add_argument("--sealed", action="store_true",
                      help="First verdict already sealed before reveal")
    rr_p.add_argument("--notes-file", default=None,
                      help="Treatment post-reveal Implementer_Notes UTF-8 文件（可审计揭示来源，Req 12.7）")
    rr_p.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    # --- record-invalid ---
    ri_p = sub.add_parser("record-invalid", help=t(
        "cli_experiment_record_invalid_desc",
        default="Record an invalid sample (Req 12.8)"))
    ri_p.add_argument("task_id", help="Task ID")
    ri_p.add_argument("batch_id", help="Batch ID")
    ri_p.add_argument("--reason-code", required=True, help="Invalid sample reason code")
    ri_p.add_argument("--detail", default="", help="Additional detail")
    ri_p.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    # --- record-incident ---
    rinc_p = sub.add_parser("record-incident", help=t(
        "cli_experiment_record_incident_desc",
        default="Record a disclosure or integrity incident (Req 12.18/12.20)"))
    rinc_p.add_argument("task_id", help="Task ID")
    rinc_p.add_argument("batch_id", help="Batch ID")
    rinc_p.add_argument("--type", required=True, choices=["disclosure", "integrity"],
                        help="Incident type")
    rinc_p.add_argument("--reason-code", required=True, help="Incident reason code")
    rinc_p.add_argument("--detail", default="", help="Additional detail")
    rinc_p.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    # --- pause ---
    pa_p = sub.add_parser("pause", help=t(
        "cli_experiment_pause_desc",
        default="Manually pause an experiment batch (Req 12.15-12.21)"))
    pa_p.add_argument("batch_id", help="Batch ID to pause")
    pa_p.add_argument("--trigger", required=True,
                      choices=[trigger.value for trigger in PauseTrigger],
                      help="Pause trigger identifier")
    pa_p.add_argument("--reason", default="", help="Pause reason text")
    pa_p.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    # --- report ---
    rep_p = sub.add_parser("report", help=t(
        "cli_experiment_report_desc",
        default="Generate evaluation report with machine-readable G0 decision"))
    rep_p.add_argument("batch_id", help="Batch ID to evaluate")
    rep_p.add_argument("--artifacts-dir", default=None,
                       help="将 report 与 evidence manifest 原子写入 JSONL 同一目录")
    rep_p.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    opts = parser.parse_args(args)

    # 公共辅助：JSONL 文件路径
    def _jsonl_path(batch_id: str) -> str:
        exp_dir = os.path.join(os.path.expanduser("~"), ".callwarden", "experiments")
        os.makedirs(exp_dir, exist_ok=True)
        return os.path.join(exp_dir, f"{batch_id}.jsonl")

    # 公共辅助：输出结果（--json 纯 JSON / 否则人类可读）
    def _output(data: dict, human_lines: list, use_json: bool) -> None:
        if use_json:
            print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        else:
            for line in human_lines:
                cprint(line)

    # 公共辅助：错误输出（Structured_Reason → JSON / 人类可读）
    def _output_error(reason_obj, use_json: bool) -> None:
        d = reason_obj.to_dict() if hasattr(reason_obj, "to_dict") else {"error": str(reason_obj)}
        if use_json:
            print(json.dumps(d, indent=2, ensure_ascii=False, default=str))
        else:
            msg = reason_obj.message() if hasattr(reason_obj, "message") else str(reason_obj)
            cprint(f"[ERROR] {msg}", "red")
            code = getattr(reason_obj, "code", None)
            if code:
                cprint(f"  code: {code}", "red")

    def _read_records(batch_id: str):
        """读取一个批次的可恢复 JSONL 记录，不创建任何产品数据库记录。"""
        return ExperimentJsonlWriter(_jsonl_path(batch_id)).read_records()

    def _resolve_sample_group(batch_id: str, task_id: str, explicit_group=None):
        """从纳样时的 blind_view 解析分组，避免指标记录丢失 Control/Treatment 归属。"""
        if explicit_group:
            return explicit_group
        for record in reversed(_read_records(batch_id)):
            if (record.get("record_type") == "blind_view"
                    and record.get("task_id") == task_id):
                group = record.get("group")
                if group in ("control", "treatment"):
                    return group
        from ..experiments.blind_review_evaluator import make_evaluator_reason, EvaluatorErrorCode
        raise EvaluatorError(make_evaluator_reason(
            EvaluatorErrorCode.EVALUATION_INPUT_INVALID,
            detail=f"task {task_id} has no admitted blind_view; admit it before recording metrics"))

    def _msg(key: str, default: str, **kwargs) -> str:
        """统一通过双语 catalog 输出 CLI 人类可读消息；JSON 字段保持稳定英文。"""
        return t(key, default=default, **kwargs)

    def _require_batch(batch_id: str):
        """所有记录命令都必须绑定已登记批次，避免孤立 JSONL 伪造实验样本。"""
        config = Experiment_Batch_Config()
        config.load()
        return config, config.get_batch(batch_id)

    try:
        # ============================================================
        # batch-create：创建批次 + 默认协议 + 锁定
        # ============================================================
        if opts.action == "batch-create":
            config = Experiment_Batch_Config()
            config.load()
            batch_id = f"B-{int(time.time() * 1000)}-{os.urandom(4).hex()}"
            protocol = build_default_protocol(opts.seed, assignment_mode=opts.assignment_mode)
            # 若用户自定义了阈值，替换协议中的 success_thresholds
            if opts.min_valid != 30 or opts.min_nontrivial != 10:
                protocol.success_thresholds = SuccessThresholds(
                    min_valid_tasks=opts.min_valid,
                    min_nontrivial_code_change_tasks=opts.min_nontrivial,
                )
            now_iso = datetime.now(timezone.utc).isoformat()
            batch = ExperimentBatch(
                batch_id=batch_id,
                created_at=now_iso,
                protocol=protocol,
            )
            batch.lock_protocol()
            config.put_batch(batch)
            config.save()
            result = {"batch_id": batch_id, "status": "locked",
                      "seed": opts.seed, "assignment_mode": opts.assignment_mode,
                      "protocol_fingerprint": batch.frozen_protocol_fingerprint,
                      "non_product_evidence": True}
            _output(result,
                    [_msg("cli_experiment_batch_created",
                          "Experiment batch created and locked: {batch_id}", batch_id=batch_id),
                     _msg("cli_experiment_batch_thresholds",
                          "  seed={seed}, min_valid={min_valid}, min_nontrivial={min_nontrivial}",
                          seed=opts.seed, min_valid=opts.min_valid,
                          min_nontrivial=opts.min_nontrivial),
                     f"  assignment_mode={opts.assignment_mode}, fingerprint={batch.frozen_protocol_fingerprint}",
                     _msg("cli_experiment_non_product_notice",
                          "  non_product_evidence=True; P0 records are not product Evidence." )],
                    opts.json)

        # ============================================================
        # batch-lock：冻结协议（手动补锁）
        # ============================================================
        elif opts.action == "batch-lock":
            config = Experiment_Batch_Config()
            config.load()
            batch = config.get_batch(opts.batch_id)
            # 已锁定批次重复执行 freeze 是幂等成功；纳样后/暂停后仍禁止改协议。
            if batch.status != BatchStatus.LOCKED:
                batch.lock_protocol()
            config.put_batch(batch)
            config.save()
            result = {"batch_id": opts.batch_id, "status": "locked",
                      "non_product_evidence": True}
            _output(result,
                    [_msg("cli_experiment_batch_locked",
                          "Experiment batch {batch_id} protocol is locked.",
                          batch_id=opts.batch_id)],
                    opts.json)

        # ============================================================
        # batch-list：列出所有已登记批次
        # ============================================================
        elif opts.action == "batch-list":
            config = Experiment_Batch_Config()
            config.load()
            batches = config.list_batches()
            summaries = []
            for b in batches:
                is_paused = b.paused_at is not None
                summaries.append({
                    "batch_id": b.batch_id,
                    "paused": is_paused,
                    "pause_trigger": b.pause_trigger.value if (is_paused and b.pause_trigger) else None,
                    "first_sample_admitted_at": b.first_sample_admitted_at,
                    "non_product_evidence": True,
                })
            if opts.json:
                print(json.dumps(summaries, indent=2, ensure_ascii=False, default=str))
            else:
                if not summaries:
                    cprint(_msg("cli_experiment_batch_list_empty", "No experiment batches registered."))
                for s in summaries:
                    status = "PAUSED" if s["paused"] else "active"
                    cprint(_msg("cli_experiment_batch_list_item",
                                "  {batch_id} [{status}]",
                                batch_id=s["batch_id"], status=status))

        # ============================================================
        # toggle-set：设置 P0 Stage_Toggle
        # ============================================================
        elif opts.action == "toggle-set":
            config = Experiment_Batch_Config()
            config.load()
            scope = ToggleScope(opts.scope)
            value = ToggleValue.ENABLED if opts.value == "on" else ToggleValue.DISABLED
            session_marker = f"cli-{int(time.time())}"
            now_iso = datetime.now(timezone.utc).isoformat()
            change = config.set_p0_toggle(
                scope, value, session_marker,
                client_clock_time=now_iso,
                scope_key=opts.scope_key,
            )
            config.save()
            result = {"scope": opts.scope, "value": opts.value,
                      "scope_key": opts.scope_key, "non_product_evidence": True}
            _output(result,
                    [_msg("cli_experiment_toggle_set",
                          "P0 toggle set: {scope}={value}{suffix}",
                          scope=opts.scope, value=opts.value,
                          suffix=(f" (key={opts.scope_key})" if opts.scope_key else ""))],
                    opts.json)

        # ============================================================
        # toggle-show：显示解析后的 P0 开关
        # ============================================================
        elif opts.action == "toggle-show":
            config = Experiment_Batch_Config()
            config.load()
            resolved = config.resolve_p0_toggle(
                task_id=opts.task_id, workspace_id=opts.workspace_id)
            resolved_str = resolved.value if hasattr(resolved, "value") else str(resolved)
            result = {"resolved_value": resolved_str,
                      "task_id": opts.task_id, "workspace_id": opts.workspace_id,
                      "non_product_evidence": True}
            _output(result,
                    [_msg("cli_experiment_toggle_show",
                          "Resolved P0 toggle: {value}{suffix}",
                          value=result["resolved_value"],
                          suffix=((f" (task={opts.task_id})" if opts.task_id else "")
                                  + (f" (workspace={opts.workspace_id})" if opts.workspace_id else "")))],
                    opts.json)

        # ============================================================
        # admit：纳样（资格检查→分组→blind view→JSONL）
        # ============================================================
        elif opts.action == "admit":
            config = Experiment_Batch_Config()
            config.load()
            batch = config.get_batch(opts.batch_id)
            # 先构造/验证盲视图，再提交首次纳样状态；来源缺失时不得提前冻结批次。
            batch.ensure_admission_allowed()
            strata_key = opts.strata or opts.task_id
            assignment = batch.protocol.assign_group(
                strata_key, pair_slot=opts.pair_slot, pair_id=opts.pair_id)
            group = BlindViewGroup.CONTROL if assignment == GroupAssignment.CONTROL else BlindViewGroup.TREATMENT
            implementer_notes = None
            if opts.notes_file:
                notes_path = os.path.abspath(opts.notes_file)
                if not os.path.isfile(notes_path):
                    raise ViewDisclosureError(make_view_reason(
                        ViewErrorCode.VIEW_SOURCE_MISSING,
                        task_id=opts.task_id,
                        field=f"implementer_notes:{notes_path}"))
                with open(notes_path, "r", encoding="utf-8") as notes_handle:
                    implementer_notes = notes_handle.read()
            if (assignment == GroupAssignment.CONTROL
                    and (not isinstance(implementer_notes, str)
                         or not implementer_notes.strip())):
                raise ViewDisclosureError(make_view_reason(
                    ViewErrorCode.CONTROL_NOTES_MISSING, task_id=opts.task_id))
            if assignment == GroupAssignment.TREATMENT and implementer_notes:
                raise ViewDisclosureError(make_view_reason(
                    ViewErrorCode.DISCLOSURE_VIOLATION,
                    task_id=opts.task_id,
                    field="implementer_notes"))
            source_facts = collect_source_facts_from_db(db, opts.task_id)
            if not opts.scope_contract:
                raise ViewDisclosureError(make_view_reason(
                    ViewErrorCode.VIEW_SOURCE_MISSING,
                    task_id=opts.task_id,
                    field="scope_contract:EXP_SCOPE_CONTRACT_REQUIRED"))
            from ..experiments.admission_scope import ScopeContractError, validate_scope_contract
            contract_path = os.path.abspath(opts.scope_contract)
            if not os.path.isfile(contract_path):
                raise ViewDisclosureError(make_view_reason(
                    ViewErrorCode.VIEW_SOURCE_MISSING,
                    task_id=opts.task_id,
                    field=f"scope_contract:{contract_path}"))
            try:
                with open(contract_path, "r", encoding="utf-8") as contract_handle:
                    scope_contract = json.load(contract_handle)
                audited_paths = [
                    str(row.get("file_path") or "")
                    for row in (source_facts.change_audit_diffs or [])
                    if isinstance(row, dict) and row.get("file_path")
                ]
                import subprocess
                tracked_result = subprocess.run(
                    ["git", "ls-files", "--", *audited_paths],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    check=False,
                )
                if tracked_result.returncode != 0:
                    raise ScopeContractError(
                        "EXP_SCOPE_TRACKED_PATHS_UNAVAILABLE",
                        tracked_result.stderr.strip() or "git ls-files failed",
                    )
                validated_scope_contract = validate_scope_contract(
                    source_facts, scope_contract,
                    tracked_paths=tracked_result.stdout.splitlines(),
                )
            except ScopeContractError as exc:
                raise ViewDisclosureError(make_view_reason(
                    ViewErrorCode.VIEW_SOURCE_MISSING,
                    task_id=opts.task_id,
                    field=f"scope_contract:{exc.code}:{exc.detail}")) from exc
            except (OSError, ValueError, TypeError) as exc:
                raise ViewDisclosureError(make_view_reason(
                    ViewErrorCode.VIEW_SOURCE_MISSING,
                    task_id=opts.task_id,
                    field=f"scope_contract:{exc}")) from exc
            view = build_minimal_blind_view(
                task_id=opts.task_id,
                source=source_facts,
                group=group,
                phase=BlindViewPhase.PRE_VERDICT,
                implementer_notes=implementer_notes,
            )
            if batch.first_sample_admitted_at is None:
                batch.mark_first_admission(datetime.now(timezone.utc).isoformat())
                config.put_batch(batch)
                config.save()
            writer = ExperimentJsonlWriter(_jsonl_path(opts.batch_id))
            record = build_blind_view_record(view=view, batch_id=opts.batch_id)
            record["assignment_mode"] = batch.protocol.assignment_mode
            record["pair_slot"] = opts.pair_slot
            record["pair_id"] = opts.pair_id
            record["scope_contract"] = validated_scope_contract
            writer.append(record)
            result = {"task_id": opts.task_id, "batch_id": opts.batch_id,
                      "group": assignment.value, "strata_key": strata_key,
                      "pair_slot": opts.pair_slot, "pair_id": opts.pair_id,
                      "disclosed_fields": view.disclosed_fields,
                      "scope_contract": validated_scope_contract,
                      "non_product_evidence": True}
            _output(result,
                    [_msg("cli_experiment_admitted",
                          "Task {task_id} admitted to experiment batch {batch_id}",
                          task_id=opts.task_id, batch_id=opts.batch_id),
                     _msg("cli_experiment_group",
                          "  group: {group} (strata={strata})",
                          group=assignment.value, strata=strata_key),
                     _msg("cli_experiment_disclosed_fields",
                          "  disclosed fields: {fields}",
                          fields=", ".join(view.disclosed_fields))],
                    opts.json)

        # ============================================================
        # record-metrics：记录 review 原始指标
        # ============================================================
        elif opts.action == "record-metrics":
            _require_batch(opts.batch_id)
            if opts.tokens_source == "real":
                if opts.tokens is None or opts.tokens < 0:
                    raise EvaluatorError(make_evaluator_reason(
                        EvaluatorErrorCode.EVALUATION_INPUT_INVALID,
                        detail="tokens-source=real requires a non-negative integer"))
                if opts.tokens_unavailable_reason:
                    raise EvaluatorError(make_evaluator_reason(
                        EvaluatorErrorCode.EVALUATION_INPUT_INVALID,
                        detail="tokens-unavailable-reason is invalid for real tokens"))
            elif opts.tokens is not None:
                raise EvaluatorError(make_evaluator_reason(
                    EvaluatorErrorCode.EVALUATION_INPUT_INVALID,
                    detail="tokens must be omitted when source is unavailable"))
            elif not opts.tokens_unavailable_reason or not opts.tokens_unavailable_reason.strip():
                raise EvaluatorError(make_evaluator_reason(
                    EvaluatorErrorCode.EVALUATION_INPUT_INVALID,
                    detail="tokens-source=unavailable requires a non-empty reason"))
            group = _resolve_sample_group(opts.batch_id, opts.task_id, opts.group)
            writer = ExperimentJsonlWriter(_jsonl_path(opts.batch_id))
            record = build_review_metrics_record(
                task_id=opts.task_id,
                batch_id=opts.batch_id,
                group=group,
                first_pass_findings=opts.tp + opts.fp,
                final_findings=opts.tp + opts.fp + opts.misses,
                verified_true_positives=opts.tp,
                verified_false_positives=opts.fp,
                verified_misses=opts.misses,
                review_duration_seconds=opts.duration,
                token_usage=opts.tokens,
                token_usage_source=opts.tokens_source,
                token_usage_unavailable_reason=opts.tokens_unavailable_reason,
                reopen_events=opts.reopen,
                post_apply_defects=opts.defects,
                post_apply_rollbacks=opts.rollbacks,
                observation_window_id=opts.obs_window,
            )
            # G0 补实验：nontrivial 自动判定（Req 12.26）——从 change_audit.diff +
            # task_symbol_changes 计算，替代手填 --nontrivial（12.20 禁止人工打标伪造）。
            # 自动判定在 DB 有 diff 数据时为准；仅当 change_audit 无 diff 记录
            # （无法自动判定）时才回退到显式 --nontrivial。
            auto_nontrivial: Optional[bool] = None
            try:
                from ..experiments.blind_review_views import collect_source_facts_from_db
                from ..experiments.blind_review_evaluator import (
                    nontrivial_code_change_from_change_audit,
                )
                facts = collect_source_facts_from_db(db, opts.task_id)
                if facts.change_audit_diffs:
                    auto_nontrivial = nontrivial_code_change_from_change_audit(
                        facts.change_audit_diffs,
                        facts.symbol_changes,
                    )
            except Exception:
                auto_nontrivial = None
            is_nontrivial = (
                auto_nontrivial
                if auto_nontrivial is not None
                else bool(opts.nontrivial)
            )
            record["is_nontrivial_code_change"] = bool(is_nontrivial)
            writer.append(record)
            result = {"task_id": opts.task_id, "batch_id": opts.batch_id,
                      "group": group, "tp": opts.tp, "fp": opts.fp, "misses": opts.misses,
                      "duration_s": opts.duration, "token_usage_source": opts.tokens_source,
                      "is_nontrivial_code_change": bool(is_nontrivial),
                      "non_product_evidence": True,
                      "nontrivial_auto_detected": auto_nontrivial is not None}
            _output(result,
                    [_msg("cli_experiment_metrics_recorded",
                          "Review metrics recorded for task {task_id} (group={group})",
                          task_id=opts.task_id, group=group),
                     _msg("cli_experiment_metrics_values",
                          "  TP={tp} FP={fp} misses={misses} duration={duration}s",
                          tp=opts.tp, fp=opts.fp, misses=opts.misses, duration=opts.duration),
                     (f"  nontrivial={is_nontrivial} (auto-detect)"
                      if auto_nontrivial is not None
                      else f"  nontrivial={is_nontrivial} (manual)")],
                    opts.json)

        # ============================================================
        # record-verdict：记录 reveal 前后 verdict 变更
        # ============================================================
        elif opts.action == "record-verdict":
            _require_batch(opts.batch_id)
            writer = ExperimentJsonlWriter(_jsonl_path(opts.batch_id))
            changed = opts.changed == "yes"
            record = build_verdict_change_record(
                task_id=opts.task_id,
                batch_id=opts.batch_id,
                verdict_changed=changed,
                change_reason_code=opts.reason_code,
            )
            writer.append(record)
            result = {"task_id": opts.task_id, "batch_id": opts.batch_id,
                      "verdict_changed": changed, "reason_code": opts.reason_code,
                      "non_product_evidence": True}
            _output(result,
                    [_msg("cli_experiment_verdict_recorded",
                          "Verdict change recorded for task {task_id}: changed={changed}",
                          task_id=opts.task_id, changed=changed)],
                    opts.json)

        # ============================================================
        # record-reveal：记录 Implementer_Notes 揭示事件
        # ============================================================
        elif opts.action == "record-reveal":
            _require_batch(opts.batch_id)
            writer = ExperimentJsonlWriter(_jsonl_path(opts.batch_id))
            implementer_notes = None
            if opts.notes_file:
                notes_path = os.path.abspath(opts.notes_file)
                if not os.path.isfile(notes_path):
                    raise ViewDisclosureError(make_view_reason(
                        ViewErrorCode.VIEW_SOURCE_MISSING,
                        task_id=opts.task_id,
                        field=f"implementer_notes:{notes_path}"))
                with open(notes_path, "r", encoding="utf-8") as notes_handle:
                    implementer_notes = notes_handle.read()
            record = build_reveal_event_record(
                task_id=opts.task_id,
                batch_id=opts.batch_id,
                first_verdict_sealed=opts.sealed,
                implementer_notes=implementer_notes,
            )
            writer.append(record)
            result = {"task_id": opts.task_id, "batch_id": opts.batch_id,
                      "first_verdict_sealed": opts.sealed,
                      "implementer_notes_included": bool(
                          implementer_notes and implementer_notes.strip()),
                      "non_product_evidence": True}
            _output(result,
                    [_msg("cli_experiment_reveal_recorded",
                          "Reveal event recorded for task {task_id}: sealed={sealed}",
                          task_id=opts.task_id, sealed=opts.sealed)],
                    opts.json)

        # ============================================================
        # record-invalid：记录无效样本
        # ============================================================
        elif opts.action == "record-invalid":
            _require_batch(opts.batch_id)
            writer = ExperimentJsonlWriter(_jsonl_path(opts.batch_id))
            record = build_invalid_sample_record(
                task_id=opts.task_id,
                batch_id=opts.batch_id,
                reason_code=opts.reason_code,
                reason_detail=opts.detail,
            )
            writer.append(record)
            result = {"task_id": opts.task_id, "batch_id": opts.batch_id,
                      "reason_code": opts.reason_code, "non_product_evidence": True}
            _output(result,
                    [_msg("cli_experiment_invalid_recorded",
                          "Invalid sample recorded for task {task_id}: reason={reason}",
                          task_id=opts.task_id, reason=opts.reason_code)],
                    opts.json)

        # ============================================================
        # record-incident：记录披露/完整性事件
        # ============================================================
        elif opts.action == "record-incident":
            config, batch = _require_batch(opts.batch_id)
            writer = ExperimentJsonlWriter(_jsonl_path(opts.batch_id))
            record = build_incident_record(
                task_id=opts.task_id,
                batch_id=opts.batch_id,
                incident_type=opts.type,
                reason_code=opts.reason_code,
                reason_detail=opts.detail,
            )
            writer.append(record)
            # 披露/完整性事件属于明确暂停触发器；记录成功后立即停止新纳样。
            incident_trigger = (PauseTrigger.DISCLOSURE_INCIDENT
                                if opts.type == "disclosure"
                                else PauseTrigger.FABRICATED_INDEPENDENCE_OR_EVIDENCE)
            batch.pause(incident_trigger, opts.reason_code,
                        datetime.now(timezone.utc).isoformat())
            config.put_batch(batch)
            config.save()
            result = {"task_id": opts.task_id, "batch_id": opts.batch_id,
                      "incident_type": opts.type, "reason_code": opts.reason_code,
                      "paused": True, "non_product_evidence": True}
            _output(result,
                    [_msg("cli_experiment_incident_recorded",
                          "{incident_type} incident recorded for task {task_id}: reason={reason}",
                          incident_type=opts.type, task_id=opts.task_id,
                          reason=opts.reason_code)],
                    opts.json)

        # ============================================================
        # pause：手动暂停批次
        # ============================================================
        elif opts.action == "pause":
            config = Experiment_Batch_Config()
            config.load()
            batch = config.get_batch(opts.batch_id)
            # 将字符串 trigger 转换为 PauseTrigger 枚举
            trigger = PauseTrigger(opts.trigger)
            now_iso = datetime.now(timezone.utc).isoformat()
            batch.pause(trigger, opts.reason, now_iso)
            config.put_batch(batch)
            config.save()
            result = {"batch_id": opts.batch_id, "paused": True,
                      "trigger": opts.trigger, "non_product_evidence": True}
            _output(result,
                    [_msg("cli_experiment_paused",
                          "Experiment batch {batch_id} paused (trigger={trigger})",
                          batch_id=opts.batch_id, trigger=opts.trigger),
                     _msg("cli_experiment_pause_notice",
                          "  fail-safe: new admissions are rejected and the existing review flow is restored.")],
                    opts.json)

        # ============================================================
        # report：汇总评估 + 机器可读 G0 决策
        # ============================================================
        elif opts.action == "report":
            config = Experiment_Batch_Config()
            config.load()
            batch = config.get_batch(opts.batch_id)
            # 从 JSONL 恢复样本记录
            jsonl_path = _jsonl_path(opts.batch_id)
            writer = ExperimentJsonlWriter(jsonl_path)
            records = writer.read_records()
            # 按组构建 SampleRecord 列表
            control_samples = []
            treatment_samples = []
            invalid_reason_codes = []
            invalid_task_ids = set()
            metric_parse_failures = []
            reveal_by_task = {}
            incident_types = set()
            # 先扫描控制记录，再解析指标，保证追加顺序不会让 invalid/reveal 影响结果。
            for rec in records:
                rec_type = rec.get("record_type", "")
                task_id = rec.get("task_id")
                if rec_type == "invalid_sample":
                    invalid_reason_codes.append(rec.get("invalid_reason_code", "unknown"))
                    if task_id:
                        invalid_task_ids.add(task_id)
                elif rec_type == "reveal_event" and task_id:
                    reveal_by_task[task_id] = bool(
                        rec.get("first_verdict_sealed_before_reveal", False))
                else:
                    canonical_type = canonical_incident_record_type(rec_type)
                    if canonical_type in ("disclosure_incident", "integrity_incident"):
                        incident_types.add(canonical_type)
            for rec in records:
                if rec.get("record_type") != "review_metrics":
                    continue
                task_id = rec.get("task_id")
                if task_id in invalid_task_ids:
                    continue
                try:
                    sample = SampleRecord.from_review_metrics_record(
                        rec,
                        is_nontrivial_code_change=bool(rec.get("is_nontrivial_code_change", False)),
                        verdict_before_reveal=bool(reveal_by_task.get(task_id, False)),
                    )
                except EvaluatorError:
                    invalid_reason_codes.append("EXP_EVALUATION_INPUT_INVALID")
                    metric_parse_failures.append({
                        "task_id": task_id,
                        "detail": "review_metrics record failed validation",
                    })
                    continue
                if sample.group == "control":
                    control_samples.append(sample)
                elif sample.group == "treatment":
                    treatment_samples.append(sample)
            # 计算组指标
            control_metrics = compute_group_metrics("control", control_samples)
            treatment_metrics = compute_group_metrics("treatment", treatment_samples)
            thresholds = batch.protocol.success_thresholds
            pause_thresholds = batch.protocol.pause_thresholds
            valid_task_count = control_metrics.valid_n + treatment_metrics.valid_n
            nontrivial_count = (control_metrics.nontrivial_code_change_count
                                + treatment_metrics.nontrivial_code_change_count)
            # 无效样本统计必须先于暂停判定；invalid 率是 12.17 的暂停输入。
            from ..experiments.blind_review_evaluator import compute_invalid_sample_stats
            invalid_rate, invalid_counts = compute_invalid_sample_stats(
                invalid_reason_codes, valid_task_count)

            # 计算成功条件 / 灰区 / 暂停。灰区是观察状态，不单独触发暂停。
            success_eval = evaluate_success(
                control_metrics, treatment_metrics, thresholds,
                valid_task_count=valid_task_count,
                nontrivial_code_change_count=nontrivial_count,
                batch_id=opts.batch_id,
            ) if thresholds else None
            gray_eval = evaluate_gray_zone(
                control_metrics, treatment_metrics, thresholds, pause_thresholds,
                batch_id=opts.batch_id,
            ) if (thresholds and pause_thresholds) else None
            pause_eval = evaluate_pause_conditions(
                control_metrics, treatment_metrics, pause_thresholds,
                invalid_sample_rate=invalid_rate,
                disclosure_incident=("disclosure_incident" in incident_types),
                integrity_incident=("integrity_incident" in incident_types),
                batch_id=opts.batch_id,
            ) if pause_thresholds else None

            metric_groups = [
                (sample.task_id, sample.group)
                for sample in control_samples + treatment_samples
            ]
            view_integrity = validate_blind_view_records(
                records, metric_groups, expected_batch_id=opts.batch_id)
            if metric_parse_failures:
                view_integrity["passed"] = False
                view_integrity["metric_parse_failures"] = metric_parse_failures
                view_integrity["reasons"].append({
                    "condition": "malformed_review_metrics",
                    "count": len(metric_parse_failures),
                    "samples": metric_parse_failures,
                })

            # 构建评估报告；指标定义与观察窗口来自冻结协议，不能由旧报告移动目标线。
            report_data = build_evaluation_report(
                batch_id=opts.batch_id,
                control=control_metrics,
                treatment=treatment_metrics,
                success=success_eval,
                gray_zone=gray_eval,
                pause=pause_eval,
                invalid_reason_counts=invalid_counts,
                invalid_sample_rate=invalid_rate,
                metric_definitions=batch.protocol.metric_definitions,
                observation_windows=batch.protocol.observation_windows,
                valid_task_count=valid_task_count,
                nontrivial_code_change_count=nontrivial_count,
            )
            report_data["view_integrity"] = view_integrity
            token_records = [
                rec for rec in records if rec.get("record_type") == "review_metrics"
            ]
            token_source_counts = {}
            for rec in token_records:
                source = rec.get("token_usage_source", "legacy_unspecified")
                token_source_counts[source] = token_source_counts.get(source, 0) + 1
            report_data["token_usage_quality"] = {
                "record_count": len(token_records),
                "source_counts": token_source_counts,
                "all_real": bool(token_records)
                and all(rec.get("token_usage_source") == "real" for rec in token_records),
                "unavailable_records": sum(
                    1 for rec in token_records
                    if rec.get("token_usage_source") == "unavailable"
                ),
                "legacy_or_estimated_records": sum(
                    1 for rec in token_records
                    if rec.get("token_usage_source", "legacy_unspecified")
                    in {"legacy_unspecified", "estimated"}
                ),
            }
            report_data["evidence"] = ExperimentJsonlWriter(jsonl_path).evidence_summary()
            # G0 是 P0 实验的机器可读决策摘要，不是产品 Evidence，也不开放 P1 hard gate。
            eligible = bool(success_eval and success_eval.eligible_for_p1)
            if not view_integrity["passed"]:
                eligible = False
                report_data["eligible_for_p1"] = False
                report_data.setdefault("authorization_blockers", {})[
                    "view_integrity_failed"
                ] = True
            has_gray = bool(gray_eval and gray_eval.gray_zone_unresolved)
            has_pause = bool(pause_eval and pause_eval.should_pause)
            gray_zones_list = []
            if gray_eval:
                if gray_eval.fp_gray_zone:
                    gray_zones_list.append("false_positive")
                if gray_eval.latency_gray_zone:
                    gray_zones_list.append("latency")
            failure_reasons = []
            if success_eval:
                failure_reasons.extend(
                    reason for reason in success_eval.reasons
                    if reason.get("satisfied") is False)
                if success_eval.insufficient_reason:
                    failure_reasons.append(success_eval.insufficient_reason)
            if gray_eval and gray_eval.gray_zone_unresolved:
                failure_reasons.extend(gray_eval.observations)
            if pause_eval and pause_eval.should_pause and pause_eval.reason:
                failure_reasons.append(pause_eval.reason)
            if not view_integrity["passed"]:
                failure_reasons.append({
                    "condition": "view_integrity",
                    "satisfied": False,
                    "reasons": view_integrity["reasons"],
                })
            if not failure_reasons and not eligible:
                failure_reasons.append({"condition": "g0_not_eligible", "reason": "unspecified"})
            if has_pause:
                g0_status = "paused"
            elif has_gray:
                g0_status = "gray_zone_observed"
            elif success_eval and success_eval.directional_only:
                g0_status = "directional_only"
            elif eligible:
                g0_status = "eligible_for_p1"
            else:
                g0_status = "not_eligible"
            g0_decision = {
                "decision": "g0",
                "status": g0_status,
                "eligible_for_p1": bool(eligible and not has_gray and not has_pause),
                "failure_reasons": failure_reasons,
                "gray_zone_observed": bool(gray_zones_list),
                "gray_zones": gray_zones_list,
                "pause": pause_eval.trigger.value if (pause_eval and pause_eval.should_pause and pause_eval.trigger) else None,
                "insufficient_sample": not success_eval.min_sample_satisfied if success_eval else True,
                "p1_hard_gate_open": False,
                "decision_scope": "p0_experiment_only",
                "non_product_evidence": True,
            }
            report_data["g0_decision"] = g0_decision

            if opts.artifacts_dir:
                try:
                    bundle = write_evidence_bundle(
                        jsonl_path=jsonl_path,
                        artifacts_dir=opts.artifacts_dir,
                        batch_id=opts.batch_id,
                        report=report_data,
                        reviewer_home=os.path.expanduser("~"),
                        reviewer_phase="final",
                    )
                except (OSError, ValueError) as exc:
                    raise EvaluatorError(make_evaluator_reason(
                        EvaluatorErrorCode.EVALUATION_INPUT_INVALID,
                        detail=f"evidence_bundle: {exc}"))
                report_data["evidence_bundle"] = bundle

            if opts.json:
                print(json.dumps(report_data, indent=2, ensure_ascii=False, default=str))
            else:
                cprint(_msg("cli_experiment_report_title",
                            "=== Experiment evaluation report: {batch_id} ===",
                            batch_id=opts.batch_id))
                cprint(_msg("cli_experiment_group_summary",
                            "  {group}: n={count}, recall={recall:.3f}, fp_rate={fp_rate:.3f}",
                            group="Control", count=control_metrics.valid_n,
                            recall=control_metrics.recall,
                            fp_rate=control_metrics.false_positive_rate))
                cprint(_msg("cli_experiment_group_summary",
                            "  {group}: n={count}, recall={recall:.3f}, fp_rate={fp_rate:.3f}",
                            group="Treatment", count=treatment_metrics.valid_n,
                            recall=treatment_metrics.recall,
                            fp_rate=treatment_metrics.false_positive_rate))
                cprint(_msg("cli_experiment_invalid_rate",
                            "  invalid sample rate: {rate:.3f}", rate=invalid_rate))
                if success_eval:
                    cprint(_msg("cli_experiment_sample_sufficient",
                                "  sample sufficient: {value}",
                                value=success_eval.min_sample_satisfied))
                if gray_zones_list:
                    cprint(_msg("cli_experiment_gray_zone",
                                "  gray-zone observation: {zones}",
                                zones=", ".join(gray_zones_list)), "yellow")
                if pause_eval and pause_eval.should_pause:
                    cprint(_msg("cli_experiment_pause_triggered",
                                "  pause triggered: {trigger}",
                                trigger=pause_eval.trigger.value), "red")
                cprint(_msg("cli_experiment_g0_decision",
                            "  G0 decision: eligible_for_p1={eligible}",
                            eligible=g0_decision["eligible_for_p1"]))
                cprint(_msg("cli_experiment_non_product_notice",
                            "  P0 records are non-product Evidence; P1 hard gate remains unavailable."))

    except (ExperimentProtocolError, ViewDisclosureError, EvaluatorError) as e:
        reason = getattr(e, "reason", None)
        if reason:
            _output_error(reason, getattr(opts, "json", False))
        else:
            cprint(f"[ERROR] {e}", "red")
        sys.exit(1)

    return True


# --------------------------------------------------------------------
# collab：多 LLM 契约协同——经 daemon 的治理写命令面（Req 14, D0 任务 3.14）
# --------------------------------------------------------------------


def _handle_collab(args, db):
    """处理 collab 子命令（多 LLM 契约协同治理写操作）

    所有治理写操作必须经 Daemon_Endpoint 序列化点，不可绕过 daemon。
    连接失败时：auto-start（3.25）→ Degraded_Mode（3.27/3.28）→
    Governance_Write fail closed + 平台相关恢复指引。

    子命令：
        publish       发布 Envelope（snapshot.publish）
        verdict       提交 Verdict 并封存（verdict.submit）
        reveal        提交 Reveal_Event（reveal.submit）
        gate-trigger  触发 Gate 判定（gate.decide）
    """
    # 公共选项父解析器（--json 可在子命令前后使用）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true",
                        help="以 JSON 格式输出结果")

    parser = argparse.ArgumentParser(
        prog="cw collab",
        description="多 LLM 契约协同：经 daemon 的治理写命令面（Req 14）",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # --- publish: 发布 Envelope ---
    pub_p = sub.add_parser("publish", parents=[common],
                           help="发布 Envelope（snapshot.publish）")
    pub_p.add_argument("--workspace", metavar="PATH", required=True,
                       help="workspace 根目录路径")
    pub_p.add_argument("--envelope", metavar="FILE",
                       help="Envelope JSON 文件路径（可选，缺省由 daemon 生成）")

    # --- verdict: 提交 Verdict ---
    ver_p = sub.add_parser("verdict", parents=[common],
                           help="提交 Verdict 并封存（verdict.submit）")
    ver_p.add_argument("--verdict-id", metavar="ID", required=True,
                       help="Verdict 记录 ID")
    ver_p.add_argument("--decision", choices=["approve", "reject", "abstain"],
                       required=True, help="判定结论")
    ver_p.add_argument("--reason", metavar="TEXT", default="",
                       help="判定理由（可选）")
    ver_p.add_argument("--seal", action="store_true", default=True,
                       help="提交后封存（默认开启）")
    ver_p.add_argument("--no-seal", action="store_true",
                       help="提交但不封存")

    # --- reveal: 提交 Reveal_Event ---
    rev_p = sub.add_parser("reveal", parents=[common],
                           help="提交 Reveal_Event（reveal.submit）")
    rev_p.add_argument("--event-id", metavar="ID", required=True,
                       help="Reveal 事件 ID")
    rev_p.add_argument("--task-id", metavar="ID", required=True,
                       help="关联任务 ID")
    rev_p.add_argument("--notes", metavar="FILE",
                       help="Implementer_Notes 文件路径（可选）")

    # --- gate-trigger: 触发 Gate 判定 ---
    gate_p = sub.add_parser("gate-trigger", parents=[common],
                            help="触发 Gate 判定（gate.decide）")
    gate_p.add_argument("--gate-id", metavar="ID", required=True,
                        help="Gate 会话 ID")
    gate_p.add_argument("--clause", metavar="NAME", required=True,
                        help="判定子句名称")
    gate_p.add_argument("--value", choices=["true", "false"], required=True,
                        help="子句判定值")

    opts = parser.parse_args(args)
    use_json = getattr(opts, "json", False)

    # 构造 RPC 参数并确定方法名
    method = None
    params = {}

    if opts.action == "publish":
        method = "snapshot.publish"
        params["workspace_root"] = os.path.abspath(opts.workspace)
        if opts.envelope:
            envelope_path = os.path.abspath(opts.envelope)
            if not os.path.isfile(envelope_path):
                _collab_error("E_FILE_NOT_FOUND", "cli.collab.file_not_found",
                              f"Envelope 文件不存在: {envelope_path}", use_json)
                return True
            with open(envelope_path, "r", encoding="utf-8") as f:
                params["envelope"] = json.load(f)

    elif opts.action == "verdict":
        method = "verdict.submit"
        params["verdict_id"] = opts.verdict_id
        params["decision"] = opts.decision
        params["reason"] = opts.reason
        params["seal"] = not opts.no_seal

    elif opts.action == "reveal":
        method = "reveal.submit"
        params["event_id"] = opts.event_id
        params["task_id"] = opts.task_id
        if opts.notes:
            notes_path = os.path.abspath(opts.notes)
            if not os.path.isfile(notes_path):
                _collab_error("E_FILE_NOT_FOUND", "cli.collab.file_not_found",
                              f"Notes 文件不存在: {notes_path}", use_json)
                return True
            with open(notes_path, "r", encoding="utf-8") as f:
                params["implementer_notes"] = f.read()

    elif opts.action == "gate-trigger":
        method = "gate.decide"
        params["gate_id"] = opts.gate_id
        params["clause"] = opts.clause
        params["value"] = opts.value == "true"

    # 通过 daemon 执行治理写操作（不可绕过）
    try:
        from ..server.daemon_client import DaemonClient, DaemonUnavailableError
        client = DaemonClient.get_instance()
        response = client.call_with_autostart(method, params)
    except DaemonUnavailableError as e:
        # Governance_Write fail closed：输出 Structured_Reason + 平台恢复指引
        _collab_governance_rejection(method, str(e), use_json)
        return True
    except Exception as e:
        _collab_error("E_RPC_FAILED", "cli.collab.rpc_failed",
                      f"RPC 调用失败 ({method}): {e}", use_json)
        return True

    # 处理降级响应（Governance_Write 不应降级，但防御性检查）
    if response.get("degraded"):
        reason = response.get("reason")
        if reason:
            _collab_output_structured_reason(reason, use_json)
        else:
            _collab_governance_rejection(method, "降级模式", use_json)
        return True

    # 成功路径
    result = response.get("result")
    if use_json:
        print(json.dumps({"ok": True, "method": method, "result": result},
                         ensure_ascii=False, indent=2))
    else:
        cprint(f"[OK] {method} 执行成功", "green")
        if result and isinstance(result, dict):
            for k, v in result.items():
                cprint(f"  {k}: {v}")

    return True


def _collab_governance_rejection(method: str, detail: str, use_json: bool):
    """输出 Governance_Write fail closed 的 Structured_Reason [Req 14.30]。"""
    import sys as _sys
    platform = "windows" if _sys.platform == "win32" else (
        "macos" if _sys.platform == "darwin" else "linux"
    )
    try:
        from ..server.daemon_autostart import get_default_endpoint
        endpoint = get_default_endpoint()
    except Exception:
        endpoint = "(unknown)"

    # 平台相关恢复指引
    if platform == "windows":
        recovery = (f"daemon 未运行。请执行: cw daemon start "
                    f"或确认 Windows 服务 CallWarden Daemon 已启动。端点: {endpoint}")
    elif platform == "macos":
        recovery = (f"daemon 未运行。请执行: cw daemon start "
                    f"或 launchctl load ~/Library/LaunchAgents/com.callwarden.daemon.plist。"
                    f"端点: {endpoint}")
    else:
        recovery = (f"daemon 未运行。请执行: cw daemon start "
                    f"或 systemctl --user start callwarden-daemon。端点: {endpoint}")

    reason = {
        "code": "E_GOVERNANCE_WRITE_DEGRADED",
        "message_key": "error.governance_write_degraded",
        "recovery_guidance": recovery,
        "executed_components": [],
        "rejected_components": [method],
        "context": {"method": method, "endpoint": endpoint, "platform": platform,
                    "detail": detail},
    }
    _collab_output_structured_reason(reason, use_json)


def _collab_output_structured_reason(reason: dict, use_json: bool):
    """输出 Structured_Reason（JSON 或人类可读格式）。"""
    if use_json:
        print(json.dumps({"ok": False, "reason": reason},
                         ensure_ascii=False, indent=2))
    else:
        code = reason.get("code", "E_UNKNOWN")
        msg_key = reason.get("message_key", "")
        recovery = reason.get("recovery_guidance", "")
        # 尝试 i18n 翻译 message_key（flat key 可能无法嵌套解析，给有意义默认值）
        _DEFAULTS = {
            "error.governance_write_degraded": "daemon 不可用，治理写操作被拒绝（Degraded_Mode fail closed）。状态保持不变。",
            "error.cross_class_partial_rejection": "跨类操作部分执行：索引部分完成，治理部分因 daemon 不可用被拒绝。",
        }
        fallback = _DEFAULTS.get(msg_key, msg_key or "操作被拒绝")
        msg = t(msg_key, default=fallback) if msg_key else "操作被拒绝"
        cprint(f"[REJECTED] {code}", "red")
        cprint(f"  {msg}", "red")
        if recovery:
            cprint(f"  恢复指引: {recovery}", "yellow")
        rejected = reason.get("rejected_components", [])
        if rejected:
            cprint(f"  被拒组成部分: {', '.join(rejected)}", "yellow")


def _collab_error(code: str, message_key: str, detail: str, use_json: bool):
    """输出一般性错误。"""
    if use_json:
        print(json.dumps({"ok": False, "error": {"code": code, "detail": detail}},
                         ensure_ascii=False, indent=2))
    else:
        cprint(f"[ERROR] {code}: {detail}", "red")


# ============================================
# dependency：P2 依赖图与环检测诊断（Req 9.1-9.10, 6.5 任务）
# ============================================


def _handle_dependency(args, db):
    """处理 dependency 子命令（P2 依赖图与环检测诊断）

    子命令：
    - inspect：查看任务/契约的依赖声明与 artifact/interface 状态
    - list：列出所有依赖边
    - cycle：检测硬依赖图中的环
    - explain：解释指定 revision 的依赖验证结果
    - provider-select：记录显式 provider 选择（Req 9.9）

    明确不提供：自动排程、assignment、抢占（Req 9.10）。
    """
    parser = argparse.ArgumentParser(
        prog="cw dependency",
        description="P2 依赖图与环检测诊断（Req 9.1-9.10）；不提供自动排程/assignment/抢占",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # --- inspect：查看依赖声明与 freshness ---
    inspect_p = sub.add_parser("inspect", help="查看任务或契约的依赖声明与 freshness")
    inspect_p.add_argument("--task-id", metavar="ID", help="任务 ID")
    inspect_p.add_argument("--contract-id", metavar="ID", help="契约 ID")
    inspect_p.add_argument("--revision", type=int, default=0, help="契约 revision")
    inspect_p.add_argument("--json", action="store_true", help="JSON 输出")

    # --- list：列出依赖边 ---
    list_p = sub.add_parser("list", help="列出硬依赖图边")
    list_p.add_argument("--contract-id", metavar="ID", help="按契约 ID 过滤")
    list_p.add_argument("--json", action="store_true", help="JSON 输出")

    # --- cycle：检测环 ---
    cycle_p = sub.add_parser("cycle", help="检测硬依赖图中的环")
    cycle_p.add_argument("--json", action="store_true", help="JSON 输出")

    # --- explain：解释 revision 依赖验证 ---
    explain_p = sub.add_parser("explain", help="解释指定 revision 的依赖验证结果")
    explain_p.add_argument("--contract-id", metavar="ID", required=True, help="契约 ID")
    explain_p.add_argument("--revision", type=int, required=True, help="契约 revision")
    explain_p.add_argument("--json", action="store_true", help="JSON 输出")

    # --- provider-select：记录显式 provider 选择（Req 9.9，写操作）---
    select_p = sub.add_parser("provider-select", help="记录显式 interface provider 选择")
    select_p.add_argument("--consumer-task-id", metavar="ID", required=True, help="消费者任务 ID")
    select_p.add_argument("--contract-id", metavar="ID", required=True, help="消费者契约 ID")
    select_p.add_argument("--revision", type=int, required=True, help="消费者契约 revision")
    select_p.add_argument("--interface-name", metavar="NAME", required=True, help="接口名")
    select_p.add_argument("--provider-task-id", metavar="ID", required=True, help="选择的 provider 任务 ID")
    select_p.add_argument("--json", action="store_true", help="JSON 输出")

    opts = parser.parse_args(args)
    use_json = getattr(opts, "json", False)

    # 获取当前 workspace_id
    active_ws = db.get_active_workspace()
    if not active_ws:
        cprint(t("cli.messages.no_active_workspace", default="未激活工作区"), "red")
        return True
    workspace_id = active_ws["id"]

    if opts.action == "inspect":
        return _dependency_inspect(db, workspace_id, opts, use_json)
    elif opts.action == "list":
        return _dependency_list(db, workspace_id, opts, use_json)
    elif opts.action == "cycle":
        return _dependency_cycle(db, workspace_id, opts, use_json)
    elif opts.action == "explain":
        return _dependency_explain(db, workspace_id, opts, use_json)
    elif opts.action == "provider-select":
        return _dependency_provider_select(db, workspace_id, opts, use_json)

    return False


def _dependency_inspect(db, workspace_id, opts, use_json):
    """查看任务或契约的依赖声明与 freshness。"""
    result = {"dependencies": [], "artifacts": [], "interfaces": []}

    if opts.task_id:
        # 查询任务的依赖声明
        cur = db.conn.execute(
            "SELECT dependency_type, target_ref, target_task_id, is_informational, "
            "contract_id, contract_revision, declared_at "
            "FROM task_dependencies WHERE workspace_id = ? AND task_id = ?",
            (workspace_id, opts.task_id),
        )
        for row in cur.fetchall():
            result["dependencies"].append(dict(row))

        # 查询任务产出的 artifact
        cur = db.conn.execute(
            "SELECT artifact_id, artifact_type, artifact_ref, artifact_hash, "
            "freshness_status, produced_at "
            "FROM artifact_identities WHERE workspace_id = ? AND task_id = ?",
            (workspace_id, opts.task_id),
        )
        for row in cur.fetchall():
            result["artifacts"].append(dict(row))

        # 查询任务发布的 interface
        cur = db.conn.execute(
            "SELECT interface_id, interface_name, version, interface_hash "
            "FROM interface_identities WHERE workspace_id = ? AND provider_task_id = ?",
            (workspace_id, opts.task_id),
        )
        for row in cur.fetchall():
            result["interfaces"].append(dict(row))

    elif opts.contract_id:
        # 按契约查询依赖
        if opts.revision > 0:
            cur = db.conn.execute(
                "SELECT dependency_type, target_ref, target_task_id, is_informational, "
                "task_id, declared_at FROM task_dependencies "
                "WHERE workspace_id = ? AND contract_id = ? AND contract_revision = ?",
                (workspace_id, opts.contract_id, opts.revision),
            )
        else:
            cur = db.conn.execute(
                "SELECT dependency_type, target_ref, target_task_id, is_informational, "
                "task_id, contract_revision, declared_at FROM task_dependencies "
                "WHERE workspace_id = ? AND contract_id = ?",
                (workspace_id, opts.contract_id),
            )
        for row in cur.fetchall():
            result["dependencies"].append(dict(row))

    if use_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        dep_count = len(result["dependencies"])
        art_count = len(result["artifacts"])
        ifc_count = len(result["interfaces"])
        print(f"依赖声明: {dep_count} 条")
        for d in result["dependencies"]:
            info = " [informational]" if d.get("is_informational") else ""
            print(f"  {d['dependency_type']} → {d['target_ref']}{info}")
        print(f"Artifact: {art_count} 个")
        for a in result["artifacts"]:
            print(f"  {a['artifact_id']} ({a['freshness_status']}) {a['artifact_ref']}")
        print(f"Interface: {ifc_count} 个")
        for i in result["interfaces"]:
            print(f"  {i['interface_name']} v{i['version']} ({i['interface_hash'][:16]})")

    return True


def _dependency_list(db, workspace_id, opts, use_json):
    """列出硬依赖图边。"""
    edges = db.get_dependency_edges(workspace_id)

    if opts.contract_id:
        edges = [e for e in edges if e.get("contract_id") == opts.contract_id]

    if use_json:
        print(json.dumps(edges, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"硬依赖图边: {len(edges)} 条")
        for e in edges:
            hard = "[hard]" if e.get("is_hard") else "[info]"
            print(f"  {e['provider_task_id']} → {e['consumer_task_id']} "
                  f"({e['edge_type']}, {e['source_type']}) {hard}")

    return True


def _dependency_cycle(db, workspace_id, opts, use_json):
    """检测硬依赖图中的环。"""
    result = db.detect_cycle(workspace_id)

    if use_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        if result.get("has_cycle"):
            print(t("cli.messages.dependency_cycle_detected"))
            path = result.get("cycle_path", [])
            print(t("cli.messages.dependency_cycle_path", path=" → ".join(path)))
        else:
            print(t("cli.messages.dependency_cycle_none"))

    return True


def _dependency_explain(db, workspace_id, opts, use_json):
    """解释指定 revision 的依赖验证结果。"""
    result = db.validate_revision_dependencies(
        workspace_id, opts.contract_id, opts.revision,
    )

    if use_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        status = t("cli.messages.dependency_explain_valid") if result.get("valid") else t("cli.messages.dependency_explain_invalid")
        print(f"Revision {opts.contract_id}@{opts.revision}: {status}")
        if result.get("errors"):
            print(t("cli.messages.dependency_explain_errors"))
            for e in result["errors"]:
                print(f"  - {e}")
        if result.get("cycle_path"):
            print(t("cli.messages.dependency_cycle_path", path=" → ".join(result["cycle_path"])))
        # 明确说明无自动排程
        print(f"\n{t('cli.messages.dependency_no_scheduling')}")

    return True


def _dependency_provider_select(db, workspace_id, opts, use_json):
    """记录显式 interface provider 选择（Req 9.9，写操作）。"""
    result = db.select_interface_provider(
        workspace_id=workspace_id,
        consumer_task_id=opts.consumer_task_id,
        contract_id=opts.contract_id,
        contract_revision=opts.revision,
        interface_name=opts.interface_name,
        selected_provider_task_id=opts.provider_task_id,
    )

    if use_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result.get("success"):
            print(t("cli.messages.dependency_provider_select_done",
                    interface=opts.interface_name, provider=opts.provider_task_id))
        else:
            print(t("cli.messages.dependency_provider_select_fail",
                    error=result.get("error", "unknown")))

    return True


# ============================================
# identity：P3 Identity/Attestation 命令（Req 10.1-10.18, 8.7 任务）
# ============================================

# 身份/撤销 Structured_Reason 的默认文案（i18n key 在两个 catalog 的
# daemon_errors 扁平结构中均可解析；这里的 default 只作为解析失败的兜底，
# 不应改变错误码语义）
_IDENTITY_REASON_DEFAULTS = {
    "daemon_errors.error.identity_incomplete": "身份信息不完整：必须同时提供 agent_id/session_id/model_id/role（Req 10.1），不得由自由文本补齐。",
    "daemon_errors.error.identity_role_mismatch": "角色不匹配：动作要求的角色与所提供身份不一致（Req 10.5）。",
    "daemon_errors.error.identity_not_wired": "数据库层尚未支持身份参数，动作 fail closed，不回退为自由文本身份（Req 10.5）。",
}


def _t_structured(message_key: str, default: str) -> str:
    """解析 Structured_Reason 的 i18n message_key（Requirement 1.12）。

    t() 按点拆分逐层解析嵌套 dict，无法处理 ``daemon_errors`` 下含点的
    扁平键（如 ``daemon_errors.error.identity_incomplete``）；本函数先尝试
    t()，失败后回退到当前语言 catalog 的 daemon_errors 扁平查找。
    """
    try:
        resolved = t(message_key, default=None)
        if resolved is not None and resolved != message_key:
            return resolved
    except Exception:
        pass
    try:
        import json as _json
        from ..i18n import DEFAULT_LANG, get_language
        lang = get_language() or DEFAULT_LANG
        _i18n_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "i18n")
        with open(os.path.join(_i18n_dir, f"{lang}.json"),
                  encoding="utf-8") as _f:
            _data = _json.load(_f)
        if message_key.startswith("daemon_errors."):
            _flat = message_key[len("daemon_errors."):]
            _val = _data.get("daemon_errors", {}).get(_flat)
            if isinstance(_val, str):
                return _val
    except Exception:
        pass
    return default


def _collect_identity(opts):
    """从 CLI 选项收集结构化身份（Req 10.1）。

    Returns:
        (identity_dict, None)：四个字段齐备时的结构化身份；
        (None, reason_dict)：任一字段出现但四者不全时的 Structured_Reason；
        (None, None)：未提供任何身份字段（沿用既有行为）。
    """
    parts = {
        "agent_id": getattr(opts, "agent_id", ""),
        "session_id": getattr(opts, "session_id", ""),
        "model_id": getattr(opts, "model_id", ""),
        "role": getattr(opts, "role", ""),
    }
    provided = {k: v for k, v in parts.items() if v}
    if not provided:
        return None, None
    if len(provided) != 4:
        return None, {
            "code": "E_IDENTITY_INCOMPLETE",
            "message_key": "daemon_errors.error.identity_incomplete",
            "detail": "必须同时提供 agent_id/session_id/model_id/role 四个字段",
        }
    return parts, None


def _validate_identity(db, identity):
    """调用 db 层校验身份完整性与角色约束（Req 10.2, 10.5）。

    Returns:
        (True, reason)：校验通过；
        (False, reason)：校验失败或 db 层未接线（fail closed）。
    """
    validator = getattr(db, "validate_action_identity", None)
    if validator is None:
        return False, {
            "code": "E_IDENTITY_NOT_WIRED",
            "message_key": "daemon_errors.error.identity_not_wired",
            "detail": "数据库层未提供 validate_action_identity",
        }
    try:
        ok, reason = validator(identity)
    except Exception as exc:
        # 禁止静默吞异常：封装为显式 Structured_Reason
        return False, {
            "code": "E_IDENTITY_VALIDATION_FAILED",
            "message_key": "daemon_errors.error.identity_not_wired",
            "detail": f"身份校验异常: {exc}",
        }
    return bool(ok), (reason or {})


def _method_accepts_identity(db, method_name: str) -> bool:
    """检查 db 方法是否接受 identity 关键字参数（8.6 接线后为 True）。

    未接线的 db 层若收到 identity 参数会 TypeError；此处显式探测签名，
    探测失败按未接线处理（fail closed，不静默降级）。
    """
    fn = getattr(db, method_name, None)
    if fn is None:
        return False
    try:
        import inspect
        params = inspect.signature(fn).parameters
        return "identity" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in params.values()
        )
    except (TypeError, ValueError):
        return False


def _identity_reason_output(reason: dict, use_json: bool):
    """输出身份/撤销 Structured_Reason（Requirement 1.12）。

    必须携带稳定错误码 + 可在 zh_CN/en_US 两个 catalog 解析的 i18n message key；
    文案变化不得改变错误码。
    """
    code = reason.get("code", "E_IDENTITY_VALIDATION_FAILED")
    msg_key = reason.get("message_key", "")
    # db 层（8.2）返回的 message_key 形如 error.identity_incomplete，
    # 统一规范为 daemon_errors 命名空间的完整路径，保证 catalog 可解析
    if msg_key.startswith("error."):
        msg_key = "daemon_errors." + msg_key
    detail = reason.get("detail", "")
    if use_json:
        print(json.dumps({"ok": False, "reason": {
            "code": code, "message_key": msg_key, "detail": detail}},
                         ensure_ascii=False, indent=2))
        return
    fallback = _IDENTITY_REASON_DEFAULTS.get(
        msg_key, msg_key or "身份校验失败")
    msg = _t_structured(msg_key, fallback)
    cprint(f"[REJECTED] {code}", "red")
    cprint(f"  {msg}", "red")
    if detail:
        cprint(f"  {detail}", "red")


def _handle_identity(args, db):
    """处理 identity 子命令（P3 Identity/Attestation，Req 10.1-10.18）

    子命令：
    - revoke：撤销 Attestation issuer/签名密钥。Revocation_Mode 必填且无默认值
      （compromised/rotated，Req 10.12）；未携带时以 Structured_Reason 拒绝并
      不追加任何撤销记录。Attestation 只能由 daemon 签发（Req 14.13），
      撤销针对的是 daemon 签发链路中的 issuer/签名密钥。
    """
    parser = argparse.ArgumentParser(
        prog="cw identity",
        description=t(
            "cli_identity_desc",
            default="P3 Identity/Attestation 命令（Revocation_Mode 必填，无默认值）"),
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # --- revoke：撤销 Attestation issuer/签名密钥 ---
    revoke_p = sub.add_parser(
        "revoke",
        help=t("cli_identity_revoke_desc",
               default="撤销 Attestation issuer/签名密钥（Revocation_Mode 必填）"))
    revoke_p.add_argument(
        "--issuer", metavar="S", required=True,
        help=t("cli_identity_arg_issuer", default="Attestation issuer 标识"))
    revoke_p.add_argument(
        "--signing-key-id", metavar="S", required=True,
        help=t("cli_identity_arg_signing_key_id", default="签名密钥标识"))
    # Revocation_Mode 必填但不在 argparse 层强制：缺省时走 Structured_Reason
    # 拒绝路径（Req 10.12），而不是 argparse usage 错误
    revoke_p.add_argument(
        "--revocation-mode", metavar="MODE",
        choices=("compromised", "rotated"), default=None,
        help=t("cli_identity_arg_revocation_mode",
               default="Revocation_Mode：compromised/rotated（必填，无默认值）"))
    revoke_p.add_argument(
        "--reason", metavar="TEXT", default="",
        help=t("cli_identity_arg_reason", default="撤销原因（可选）"))
    # 发起者身份（可选；提供后必须完整）
    revoke_p.add_argument(
        "--agent-id", default="", metavar="ID",
        help=t("cli_task_arg_agent_id", default="发起者 Agent ID"))
    revoke_p.add_argument(
        "--session-id", default="", metavar="ID",
        help=t("cli_task_arg_session_id", default="发起者 Session ID"))
    revoke_p.add_argument(
        "--model-id", default="", metavar="ID",
        help=t("cli_task_arg_model_id", default="发起者 Model ID"))
    revoke_p.add_argument(
        "--role", default="", metavar="ROLE",
        help=t("cli_task_arg_role", default="发起者 Role"))
    revoke_p.add_argument(
        "--json", action="store_true",
        help=t("cli_identity_arg_json", default="JSON 输出"))

    opts = parser.parse_args(args)
    use_json = getattr(opts, "json", False)

    if opts.action == "revoke":
        return _identity_revoke(db, opts, use_json)
    return False


def _identity_revoke(db, opts, use_json):
    """执行 Attestation issuer/签名密钥撤销（Req 10.10-10.18）。

    Revocation_Mode 缺失时以 Structured_Reason 拒绝，不调用 db 层、
    不追加任何撤销记录（Req 10.12）。
    """
    if not opts.revocation_mode:
        _identity_reason_output({
            "code": "E_REVOCATION_MODE_REQUIRED",
            "message_key": "daemon_errors.error.revocation_mode_missing",
            "detail": ("撤销请求必须显式指定 --revocation-mode "
                       "（compromised 或 rotated）；未提供时不追加任何撤销记录"),
        }, use_json)
        return True

    # 发起者身份（可选；提供则必须完整，否则 fail closed）
    initiator_identity = None
    if opts.agent_id or opts.session_id or opts.model_id or opts.role:
        if not all((opts.agent_id, opts.session_id, opts.model_id, opts.role)):
            _identity_reason_output({
                "code": "E_IDENTITY_INCOMPLETE",
                "message_key": "daemon_errors.error.identity_incomplete",
                "detail": "发起者身份必须同时提供 --agent-id/--session-id/--model-id/--role",
            }, use_json)
            return True
        initiator_identity = {
            "agent_id": opts.agent_id,
            "session_id": opts.session_id,
            "model_id": opts.model_id,
            "role": opts.role,
        }

    register = getattr(db, "register_attestation_revocation", None)
    if register is None:
        _identity_reason_output({
            "code": "E_IDENTITY_NOT_WIRED",
            "message_key": "daemon_errors.error.identity_not_wired",
            "detail": "数据库层未提供 register_attestation_revocation",
        }, use_json)
        return True

    # 工作区 ID：写命令启动时已激活工作区；取不到时交由 db 层解析
    ws_id = None
    try:
        active_ws = db.get_active_workspace()
        if active_ws:
            ws_id = active_ws["id"]
    except Exception:
        ws_id = None

    try:
        ok, result = register(
            issuer=opts.issuer,
            signing_key_id=opts.signing_key_id,
            revocation_mode=opts.revocation_mode,
            revocation_reason=opts.reason,
            initiating_actor=(
                json.dumps(initiator_identity, ensure_ascii=False)
                if initiator_identity else ""
            ),
            workspace_id=ws_id,
        )
    except Exception as exc:
        # 禁止静默吞异常：封装为显式 Structured_Reason
        _identity_reason_output({
            "code": "E_REVOCATION_FAILED",
            "message_key": "daemon_errors.error.attestation_invalid",
            "detail": f"撤销记录写入失败: {exc}",
        }, use_json)
        return True

    if not ok:
        _identity_reason_output(result or {}, use_json)
        return True

    if use_json:
        print(json.dumps({"ok": True, "revocation": result},
                         ensure_ascii=False, indent=2))
    else:
        cprint(t("cli.messages.identity_revoke_success"), "green", bold=True)
        print(t("cli.messages.identity_revocation_id",
                id=result.get("revocation_id", "")))
        print(t("cli.messages.identity_revocation_issuer",
                issuer=opts.issuer))
        print(t("cli.messages.identity_revocation_signing_key",
                signing_key=opts.signing_key_id))
        print(t("cli.messages.identity_revocation_mode",
                mode=opts.revocation_mode))
        if result.get("revoked_at") or result.get("revocation_time"):
            print(t("cli.messages.identity_revocation_time",
                    ts=result.get("revoked_at") or result.get("revocation_time")))
        # 说明 Attestation 只能由 daemon 签发（Req 14.13），客户端自签不作为授权证明
        cprint(t("cli.messages.identity_attestation_daemon_only"), "yellow")
    return True


# ============================================================
# lease / assignment：P4 Assignment 与安全 Lease 命令（Req 11.1-11.13, 10.5 任务）
# ============================================================

def _collect_lease_identity(opts):
    """收集 Lease 命令的 holder Identity（agent_id/session_id/model_id 必填）

    Returns:
        (identity_dict, reason)：齐备时 identity_dict 非空、reason 为 None；
        缺失时 identity_dict 为 None、reason 为 Structured_Reason
    """
    agent_id = getattr(opts, "agent_id", "")
    session_id = getattr(opts, "session_id", "")
    model_id = getattr(opts, "model_id", "")
    if not all([agent_id, session_id, model_id]):
        return None, {
            "code": "E_ASSIGNMENT_INCOMPLETE",
            "message_key": "daemon_errors.error.identity_incomplete",
            "detail": "Lease/Assignment 需要同时提供 --agent-id/--session-id/--model-id（Req 11.2）",
        }
    return {
        "agent_id": agent_id,
        "session_id": session_id,
        "model_id": model_id,
    }, None


def _collect_lease_creds(opts):
    """收集 P4 受保护写 Lease 凭证（--lease-token/--fencing-counter）

    - 两者都未提供 → 返回 {}（向后兼容路径，不启用受保护写）
    - 只提供其一 → 返回 {"error": reason}（fail closed，Requirement 11.9）
    - 两者都提供 → 返回 {"lease_token": ..., "fencing_counter": ...}

    Returns:
        dict：含 "error" 键表示凭证不完整；否则为透传给 db 方法的关键字参数
    """
    token = getattr(opts, "lease_token", "") or ""
    counter = getattr(opts, "fencing_counter", -1) or -1
    if not token and counter < 0:
        return {}
    if not token:
        return {"error": {
            "code": "E_LEASE_CRED_INCOMPLETE",
            "message_key": "daemon_errors.error.identity_not_wired",
            "detail": "提供 --fencing-counter 时必须同时提供 --lease-token（Req 11.9）",
        }}
    if counter < 0:
        return {"error": {
            "code": "E_LEASE_CRED_INCOMPLETE",
            "message_key": "daemon_errors.error.identity_not_wired",
            "detail": "提供 --lease-token 时必须同时提供 --fencing-counter（Req 11.9）",
        }}
    return {"lease_token": token, "fencing_counter": counter}


def _lease_reason_output(reason: dict, use_json: bool):
    """输出 Lease/Assignment 的结构化拒绝原因（与 _identity_reason_output 同风格）"""
    if use_json:
        print(json.dumps(reason, ensure_ascii=False, indent=2))
        return
    detail = reason.get("detail", "")
    cprint(t("cli.messages.lease_denied", default="Lease 校验未通过"), "red")
    if detail:
        print(detail)


def _handle_lease(args, db):
    """处理 lease 子命令（P4 安全 Lease，Req 11.2-11.7）

    raw token 仅在 acquire 成功响应返回一次（Req 11.2）；日志/数据库只存 hash。
    Degraded_Mode 下 Lease 获取/续租/释放属 Governance_Write，一律 fail closed（Req 14.31）。
    """
    parser = argparse.ArgumentParser(
        prog="cw lease",
        description=t("cli_lease_desc",
                      default="P4 Lease: acquire/renew/release/status (Req 11.2-11.7)"),
    )
    sub = parser.add_subparsers(dest="action")

    acquire_p = sub.add_parser(
        "acquire",
        help=t("cli_lease_acquire_desc",
               default="Acquire a lease (returns raw token once)"))
    acquire_p.add_argument("task_id", help=t("cli_task_arg_task_id", default="Task ID"))
    acquire_p.add_argument("--role", default="implementer",
                           help=t("cli_lease_arg_role", default="Role"))
    acquire_p.add_argument("--agent-id", default="", metavar="ID",
                           help=t("cli_task_arg_agent_id", default="Agent ID"))
    acquire_p.add_argument("--session-id", default="", metavar="ID",
                           help=t("cli_task_arg_session_id", default="Session ID"))
    acquire_p.add_argument("--model-id", default="", metavar="ID",
                           help=t("cli_task_arg_model_id", default="Model ID"))
    acquire_p.add_argument("--ttl", type=float, default=3600.0,
                           help=t("cli_lease_arg_ttl",
                                  default="TTL seconds (default 3600)"))
    acquire_p.add_argument("--json", action="store_true",
                           help=t("cli_identity_arg_json", default="JSON output"))

    renew_p = sub.add_parser(
        "renew",
        help=t("cli_lease_renew_desc",
               default="Renew an active lease (idempotent, counter unchanged)"))
    renew_p.add_argument("task_id", help=t("cli_task_arg_task_id", default="Task ID"))
    renew_p.add_argument("--role", default="implementer",
                         help=t("cli_lease_arg_role", default="Role"))
    renew_p.add_argument("--token", default="", required=True,
                         help=t("cli_lease_arg_token", default="Lease raw token"))
    renew_p.add_argument("--agent-id", default="", metavar="ID",
                         help=t("cli_task_arg_agent_id", default="Agent ID"))
    renew_p.add_argument("--session-id", default="", metavar="ID",
                         help=t("cli_task_arg_session_id", default="Session ID"))
    renew_p.add_argument("--model-id", default="", metavar="ID",
                         help=t("cli_task_arg_model_id", default="Model ID"))
    renew_p.add_argument("--ttl", type=float, default=3600.0,
                         help=t("cli_lease_arg_ttl", default="TTL seconds"))
    renew_p.add_argument("--json", action="store_true",
                         help=t("cli_identity_arg_json", default="JSON output"))

    release_p = sub.add_parser(
        "release",
        help=t("cli_lease_release_desc",
               default="Release an active lease (idempotent)"))
    release_p.add_argument("task_id", help=t("cli_task_arg_task_id", default="Task ID"))
    release_p.add_argument("--role", default="implementer",
                           help=t("cli_lease_arg_role", default="Role"))
    release_p.add_argument("--token", default="", required=True,
                           help=t("cli_lease_arg_token", default="Lease raw token"))
    release_p.add_argument("--agent-id", default="", metavar="ID",
                           help=t("cli_task_arg_agent_id", default="Agent ID"))
    release_p.add_argument("--session-id", default="", metavar="ID",
                           help=t("cli_task_arg_session_id", default="Session ID"))
    release_p.add_argument("--model-id", default="", metavar="ID",
                           help=t("cli_task_arg_model_id", default="Model ID"))
    release_p.add_argument("--json", action="store_true",
                           help=t("cli_identity_arg_json", default="JSON output"))

    status_p = sub.add_parser(
        "status",
        help=t("cli_lease_status_desc",
               default="Show lease status (read-only)"))
    status_p.add_argument("task_id", help=t("cli_task_arg_task_id", default="Task ID"))
    status_p.add_argument("--role", default="",
                          help=t("cli_lease_arg_role", default="Role (empty = latest)"))
    status_p.add_argument("--json", action="store_true",
                          help=t("cli_identity_arg_json", default="JSON output"))

    list_p = sub.add_parser(
        "list",
        help=t("cli_lease_list_desc",
               default="List lease audit events (read-only)"))
    list_p.add_argument("--task-id", default="",
                        help=t("cli_task_arg_task_id", default="Task ID (optional)"))
    list_p.add_argument("--role", default="",
                        help=t("cli_lease_arg_role", default="Role (optional)"))
    list_p.add_argument("--json", action="store_true",
                        help=t("cli_identity_arg_json", default="JSON output"))

    opts = parser.parse_args(args)
    if not getattr(opts, "action", None):
        parser.print_help()
        return True
    use_json = getattr(opts, "json", False)

    # lease status/list 是只读命令，其余为写命令（写命令已在 _is_readonly_command 归类）
    if opts.action == "status":
        result = db.get_lease_status(opts.task_id, opts.role)
        if use_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(t("cli.messages.lease_status_title", default="Lease Status"))
            for k, v in result.items():
                print(f"  {k}: {v}")
        return True

    if opts.action == "list":
        events = db.list_lease_events(opts.task_id, opts.role)
        if use_json:
            print(json.dumps(events, ensure_ascii=False, indent=2))
        else:
            print(t("cli.messages.lease_events_title", default="Lease Audit Events"))
            for e in events:
                print(f"  {e['event_at']:.1f} [{e['event_type']}] "
                      f"lease={e['lease_id']} counter={e['fencing_counter']} "
                      f"role={e['role']}")
        return True

    identity, ireason = _collect_lease_identity(opts)
    if ireason is not None:
        _lease_reason_output(ireason, use_json)
        return True

    if opts.action == "acquire":
        ok, result = db.acquire_lease(
            opts.task_id, opts.role, identity, ttl_seconds=opts.ttl)
        if not ok:
            _lease_reason_output(result, use_json)
            return True
        if use_json:
            print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
        else:
            cprint(t("cli.messages.lease_acquired", default="Lease acquired"), "green", bold=True)
            # raw token 仅此一次返回（Req 11.2）
            print(t("cli.messages.lease_token_once",
                    default="Lease token (仅此一次返回，请妥善保存，日志/数据库不存储):"))
            print(f"  {result['token']}")
            print(t("cli.messages.lease_id_label", default="Lease ID"), ":", result["lease_id"])
            print(t("cli.messages.lease_fencing_label", default="Fencing Counter"), ":", result["fencing_counter"])
            print(t("cli.messages.lease_expires_label", default="Expires At"), ":", f"{result['expires_at']:.1f}")
        return True

    if opts.action == "renew":
        ok, result = db.renew_lease(
            opts.task_id, opts.role, opts.token, identity=identity, ttl_seconds=opts.ttl)
        if not ok:
            _lease_reason_output(result, use_json)
            return True
        if use_json:
            print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
        else:
            cprint(t("cli.messages.lease_renewed", default="Lease renewed"), "green", bold=True)
            print(t("cli.messages.lease_id_label", default="Lease ID"), ":", result["lease_id"])
            print(t("cli.messages.lease_fencing_label", default="Fencing Counter"), ":", result["fencing_counter"])
            print(t("cli.messages.lease_expires_label", default="Expires At"), ":", f"{result['expires_at']:.1f}")
        return True

    if opts.action == "release":
        ok, result = db.release_lease(
            opts.task_id, opts.role, opts.token, identity=identity)
        if not ok:
            _lease_reason_output(result, use_json)
            return True
        if use_json:
            print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
        else:
            cprint(t("cli.messages.lease_released", default="Lease released"), "green", bold=True)
            print(t("cli.messages.lease_id_label", default="Lease ID"), ":", result["lease_id"])
            print(t("cli.messages.lease_fencing_label", default="Fencing Counter"), ":", result["fencing_counter"])
            if result.get("released_at"):
                print(t("cli.messages.lease_released_label", default="Released At"),
                      ":", f"{result['released_at']:.1f}")
        return True

    parser.print_help()
    return True


def _handle_assignment(args, db):
    """处理 assignment 子命令（P4 Assignment 绑定，Req 11.1）

    assignment 只绑定 task+role+holder Identity，不把 workspace active_task_id
    当作 assignment authority（Req 13.4）；assignment 可以没有 lease（Req 11.12）。
    """
    parser = argparse.ArgumentParser(
        prog="cw assignment",
        description=t("cli_assignment_desc",
                      default="P4 Assignment: bind task+role+holder Identity (Req 11.1)"),
    )
    sub = parser.add_subparsers(dest="action")

    create_p = sub.add_parser(
        "create",
        help=t("cli_assignment_create_desc", default="Create an assignment"))
    create_p.add_argument("task_id", help=t("cli_task_arg_task_id", default="Task ID"))
    create_p.add_argument("--role", default="implementer",
                          help=t("cli_lease_arg_role", default="Role"))
    create_p.add_argument("--agent-id", default="", metavar="ID",
                          help=t("cli_task_arg_agent_id", default="Agent ID"))
    create_p.add_argument("--session-id", default="", metavar="ID",
                          help=t("cli_task_arg_session_id", default="Session ID"))
    create_p.add_argument("--model-id", default="", metavar="ID",
                          help=t("cli_task_arg_model_id", default="Model ID"))
    create_p.add_argument("--json", action="store_true",
                          help=t("cli_identity_arg_json", default="JSON output"))

    show_p = sub.add_parser(
        "show",
        help=t("cli_assignment_show_desc",
               default="Show current active assignment (read-only)"))
    show_p.add_argument("task_id", help=t("cli_task_arg_task_id", default="Task ID"))
    show_p.add_argument("--role", default="",
                        help=t("cli_lease_arg_role", default="Role (empty = latest)"))
    show_p.add_argument("--json", action="store_true",
                        help=t("cli_identity_arg_json", default="JSON output"))

    revoke_p = sub.add_parser(
        "revoke",
        help=t("cli_assignment_revoke_desc", default="Revoke an assignment"))
    revoke_p.add_argument("assignment_id", help=t(
        "cli_assignment_arg_assignment_id", default="Assignment ID (ASG-xxx)"))
    revoke_p.add_argument("--json", action="store_true",
                          help=t("cli_identity_arg_json", default="JSON output"))

    opts = parser.parse_args(args)
    if not getattr(opts, "action", None):
        parser.print_help()
        return True
    use_json = getattr(opts, "json", False)

    if opts.action == "show":
        result = db.get_assignment(opts.task_id, opts.role)
        if result is None:
            print(t("cli.messages.assignment_not_found",
                    default="No active assignment found"))
            return True
        if use_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(t("cli.messages.assignment_title", default="Active Assignment"))
            for k in ("assignment_id", "task_id", "role", "agent_id", "session_id", "model_id", "created_at"):
                print(f"  {k}: {result.get(k)}")
        return True

    if opts.action == "revoke":
        ok, result = db.revoke_assignment(opts.assignment_id)
        if not ok:
            _lease_reason_output(result, use_json)
            return True
        if use_json:
            print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
        else:
            cprint(t("cli.messages.assignment_revoked",
                     default="Assignment revoked"), "green", bold=True)
        return True

    # create：需要完整 holder Identity（Req 11.1）
    identity, ireason = _collect_lease_identity(opts)
    if ireason is not None:
        _lease_reason_output(ireason, use_json)
        return True

    ok, result = db.create_assignment(opts.task_id, opts.role, identity)
    if not ok:
        _lease_reason_output(result, use_json)
        return True
    if use_json:
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    else:
        cprint(t("cli.messages.assignment_created",
                 default="Assignment created"), "green", bold=True)
        print(t("cli.messages.assignment_id_label", default="Assignment ID"),
              ":", result["assignment_id"])
    return True


if __name__ == "__main__":
    main()
