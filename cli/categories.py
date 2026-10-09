"""CLI 顶层命令分类真相源（cli-mcp-surface-audit Phase 1）。

本模块是全部 84 个 CLI 顶层命令归属的唯一权威数据：

- 调用链 1：cli.main._print_main_help() 导入 COMMAND_CATEGORIES 渲染主
  --help（替代手写 _MAIN_HELP_GROUPS 静态块，防再漂移）；
- 调用链 2：scripts/gen_category_overview.py 读取本模块生成
  docs/cli_reference.md「命令概览」标记块；
- 调用链 3：tests/test_category_source.py 校验「每个顶层命令恰好归一类」，
  与 cli.main._SUBCOMMANDS（79）+ cw.py standalone（install/server/test）
  + setup + daemon = 84 个入口做集合比对。

分类体系与 server/tools/_categories.py 的 MCP 17 分类同构
（key 一一对应）；[18]-[21] 为 CLI-only 能力域（MCP 无对应分类）。

新增顶层命令的流程：先在下方对应分类追加 CommandInfo，再接 argparse
dispatch——漏归类会被 test_category_source.py 拦截。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class CommandInfo:
    """单个顶层命令的元数据。

    name:        dispatch 关键字（_SUBCOMMANDS 成员，或 cw.py/setup/daemon 入口）
    desc_key:    命令描述 i18n key（cli.messages.*，空串表示无 key，
                 直接用 default_desc，避免 t() 裸 key 输出）
    default_desc: i18n 缺 key 时的回退描述（中文）
    """

    name: str
    desc_key: str = ""
    default_desc: str = ""


@dataclass(frozen=True)
class CommandCategory:
    """命令分类（与 MCP 分类 key 同构 + CLI-only 增量）。

    key:       稳定标识，供 server/tools/_categories.py 的
               cli_category 字段与 gen_category_overview.py 引用
    title:     默认标题（i18n 缺 key 回退值，中文）
    title_key: 主 --help 组标题 i18n key（空串则直接用 title）
    scope:     概览表「涵盖范围」列文案
    cli_only:  True 表示 MCP 侧无同构分类（CLI 独有运维面）
    """

    key: str
    title: str
    title_key: str
    scope: str
    cli_only: bool
    commands: List[CommandInfo]


def _cmd(name: str, desc_key: str = "", default_desc: str = "") -> CommandInfo:
    """构造 CommandInfo 的简写（数据区用，减少重复样板）。"""
    return CommandInfo(name=name, desc_key=desc_key, default_desc=default_desc)


# ====================================================================
# 命令分类数据（84 个顶层命令，以 MCP 17 分类为基准统一 + CLI-only 行）
# --------------------------------------------------------------------
# 排列顺序即 --help 与 cli_reference.md 概览表的输出顺序。
# desc_key 仅为 i18n 已有 key 的命令填写；新命令留空走 default_desc，
# 保证任何情况下 help 不出现裸 key（cli-mcp-surface-audit AC-2）。
# ====================================================================

COMMAND_CATEGORIES: List[CommandCategory] = [
    CommandCategory(
        key="workspace_database",
        title="Workspace & Database",
        title_key="cli.messages.help_group_workspace",
        scope="工作区管理、数据库刷新、状态概览、分支感知、分层配置、C 图构建",
        cli_only=False,
        commands=[
            _cmd("workspace",
                 default_desc="工作区管理（list/register/set/delete/scan/generate-ignore）"),
            _cmd("refresh", desc_key="cli.messages.help_refresh"),
            _cmd("stats", desc_key="cli.messages.help_stats"),
            _cmd("status", desc_key="cli.messages.help_status"),
            _cmd("graph", default_desc="构建 C 文件调用图（build-from-c）"),
            _cmd("config", default_desc="分层配置查看与解释（explain/paths）"),
        ],
    ),
    CommandCategory(
        key="query_search",
        title="Query & Search",
        title_key="cli.messages.help_group_query",
        scope="符号/文件/语义搜索、摘要、RAG、版本对比、最近变更、FTS 全文索引",
        cli_only=False,
        commands=[
            _cmd("search", desc_key="cli.messages.help_search"),
            _cmd("grep", default_desc="文件内容正则检索"),
            _cmd("symbol", desc_key="cli.messages.help_symbol"),
            _cmd("file", desc_key="cli.messages.help_file"),
            _cmd("query", desc_key="cli.messages.help_query"),
            _cmd("brief", desc_key="cli.messages.help_brief"),
            _cmd("map", desc_key="cli.messages.help_map"),
            _cmd("fts", default_desc="FTS5 全文索引构建与状态（rebuild/status）"),
            _cmd("semantic-search", default_desc="语义向量搜索"),
            _cmd("similar", default_desc="查找相似函数（向量相似度）"),
            _cmd("embed", default_desc="符号向量化嵌入（批量增量 / --force 全量）"),
            _cmd("diff", default_desc="对比两个符号内容版本（按 hash）"),
            _cmd("changes", default_desc="最近变更的文件与函数"),
        ],
    ),
    CommandCategory(
        key="call_chain",
        title="Call Chain Analysis",
        title_key="cli.messages.help_group_call_chain",
        scope="调用链、拓扑、循环、孤儿、模块图、热力图、变更影响半径",
        cli_only=False,
        commands=[
            _cmd("callers", desc_key="cli.messages.help_callers"),
            _cmd("callees", desc_key="cli.messages.help_callees"),
            _cmd("call-chain", desc_key="cli.messages.help_call_chain"),
            _cmd("topo", desc_key="cli.messages.help_topo"),
            _cmd("impact", desc_key="cli.messages.help_impact"),
            _cmd("deepest", desc_key="cli.messages.help_chain_deepest"),
            _cmd("module-calls", desc_key="cli.messages.help_chain_module_calls"),
            _cmd("detect-cycles", desc_key="cli.messages.help_chain_cycles"),
            _cmd("export-module-graph",
                 desc_key="cli.messages.help_chain_module_graph"),
            _cmd("call-heatmap", desc_key="cli.messages.help_chain_heatmap"),
            _cmd("top-callers", desc_key="cli.messages.help_chain_top_callers"),
            _cmd("orphan-symbols", desc_key="cli.messages.help_chain_orphans"),
        ],
    ),
    CommandCategory(
        key="code_health",
        title="Code Health & Metrics",
        title_key="cli.messages.help_group_metrics",
        scope="度量、复杂度、耦合、演化、热点、流失、健康报告、驾驶舱",
        cli_only=False,
        commands=[
            _cmd("metrics", desc_key="cli.messages.help_metrics"),
            _cmd("complexity", desc_key="cli.messages.help_complexity"),
            _cmd("coupling", desc_key="cli.messages.help_coupling"),
            _cmd("largest-fns", desc_key="cli.messages.help_largest_fns"),
            _cmd("coupled-fns", desc_key="cli.messages.help_coupled_fns"),
            _cmd("fn-metrics", desc_key="cli.messages.help_fn_metrics"),
            _cmd("evolution", desc_key="cli.messages.help_evolution"),
            _cmd("hotspot", desc_key="cli.messages.help_hotspot"),
            _cmd("churn", desc_key="cli.messages.help_churn"),
            _cmd("health-report", default_desc="项目健康报告（多维度评分）"),
            _cmd("dashboard", default_desc="项目综合状态驾驶舱"),
        ],
    ),
    CommandCategory(
        key="task",
        title="Task Orchestration",
        title_key="cli.messages.help_group_task",
        scope="任务创建/认领/上报/回滚/审批/关闭、派工查询、角色提示、capture-diff",
        cli_only=False,
        commands=[
            _cmd("task",
                 default_desc="任务编排（create/next/report/apply/close 等 20+ 子命令）"),
        ],
    ),
    CommandCategory(
        key="rule_memory",
        title="Agent Rule Memory",
        title_key="cli.messages.help_group_rule",
        scope="规则候选/审核/生效/同步/提取/清理/种子化",
        cli_only=False,
        commands=[
            _cmd("rule",
                 default_desc="Agent 规则记忆（candidate/list/applicable/sync/extract）"),
        ],
    ),
    CommandCategory(
        key="audit_bootstrap",
        title="Audit & Bootstrap",
        title_key="cli.messages.help_group_audit",
        scope="审计链、密钥轮换、自举健康、检查门禁、安全护栏",
        cli_only=False,
        commands=[
            _cmd("audit", default_desc="审计链（verify/rotate-key/keys）"),
            _cmd("bootstrap", desc_key="cli.messages.help_bootstrap_status"),
            _cmd("check-gate", desc_key="cli.messages.help_check_gate"),
            _cmd("guardrail",
                 # 修正（MCP↔CLI 映射真相源审计）：CLI guardrail 域实际仅有
                 # scan/rules 两个子命令；check-edit/list-rules/add-rule 只是
                 # MCP 工具面（guardrail_check_edit 等），无 CLI 入口。
                 default_desc="安全护栏（scan/rules）"),
        ],
    ),
    CommandCategory(
        key="git",
        title="Git Integration",
        title_key="cli.messages.help_group_git",
        scope="git 历史、commit、变更、统计、符号历史",
        cli_only=False,
        commands=[
            _cmd("git",
                 default_desc="Git 集成（import/log/show/stats/check-task）"),
            _cmd("symbol-history", desc_key="cli.messages.help_symbol_history"),
        ],
    ),
    CommandCategory(
        key="semgrep_defects",
        title="Semgrep & Defects",
        title_key="cli.messages.help_group_semgrep",
        scope="Semgrep 扫描、缺陷知识库、漏洞爆炸半径、审查就绪、符号静态检查",
        cli_only=False,
        commands=[
            _cmd("semgrep", default_desc="Semgrep 静态分析（scan/list/stats）"),
            _cmd("defect",
                 default_desc="缺陷知识库（search/suggest/learn/stats/build）"),
            _cmd("vuln-blast", desc_key="cli.messages.help_vuln_blast"),
            _cmd("review", desc_key="cli.messages.help_review"),
            _cmd("issues", default_desc="符号静态检查（Semgrep+Guardrail 聚合）"),
            _cmd("function-issues", desc_key="cli.messages.help_function_issues"),
        ],
    ),
    CommandCategory(
        key="coverage_ownership",
        title="Coverage & Ownership",
        title_key="cli.messages.help_group_coverage",
        scope="注释/测试覆盖率、测试 case 关联、所有权、注释恢复",
        cli_only=False,
        commands=[
            _cmd("coverage",
                 default_desc="覆盖率（import/fn/uncovered/test）"),
            # 修正（2026-10-08 分类复审）：原 desc 列出 `case`/`coverage` 两个
            # 不存在的"子命令"——`cw tests` 仅 flag 面（无 subparser）；实际
            # flag 为 --reverse / --build / --history / --import 等，`coverage
            # test` 属 `cw coverage` 子命令，不在本命令下。
            _cmd("tests",
                 default_desc="测试 case 关联与稳定性（--reverse / --build / --history / --import）"),
            _cmd("test-impact", desc_key="cli.messages.help_test_impact"),
            _cmd("comment-coverage", desc_key="cli.messages.help_comment_coverage"),
            _cmd("uncommented", desc_key="cli.messages.help_uncommented"),
            _cmd("restore-comment", default_desc="从历史版本恢复单个符号注释"),
            _cmd("restore-all-comments", default_desc="批量恢复全项目缺失注释"),
            _cmd("who", desc_key="cli.messages.help_who"),
            _cmd("ownership-map", desc_key="cli.messages.help_ownership_map"),
        ],
    ),
    CommandCategory(
        key="gc",
        title="GC",
        title_key="cli.messages.help_group_gc",
        scope="归档、恢复、清理、策略、备份、审计、外部符号",
        cli_only=False,
        commands=[
            _cmd("gc",
                 default_desc="数据生命周期（archive/restore/purge/policy/retention/audit 等 13 子命令）"),
        ],
    ),
    CommandCategory(
        key="diagnostics",
        title="Diagnostics",
        title_key="cli.messages.help_group_diagnostics",
        scope="doctor 环境诊断、clone 重复检测",
        cli_only=False,
        commands=[
            _cmd("doctor", desc_key="cli.messages.help_doctor"),
            _cmd("clone", default_desc="代码克隆检测（detect/list/stats/clear）"),
        ],
    ),
    CommandCategory(
        key="build_context",
        title="构建上下文感知",
        title_key="",
        # 修正（2026-10-08 分类复审）：删除残留的「指标」——`cw build-context` /
        # `cw toolchain` 均无 metrics 子命令；daemon 运行时指标属 [19] Daemon 运维
        # 的 CLI-only 命令 `cw daemon metrics`，与 MCP [12] Diagnostics 的
        # `get_metrics` 共享数据源（同 MCP [13] scope 的同步修正）。
        scope="工具链注册、build context、resolved edges",
        cli_only=False,
        commands=[
            _cmd("build-context", default_desc="构建上下文与 resolved edges 查询"),
            _cmd("toolchain", default_desc="工具链注册表管理"),
        ],
    ),
    CommandCategory(
        key="collab",
        title="只读协同查询",
        title_key="",
        scope="协同证据、门禁决策、角色视图、新鲜度",
        cli_only=False,
        commands=[
            _cmd("collab",
                 default_desc="只读协同查询（evidence/gate/role/freshness/publish）"),
        ],
    ),
    CommandCategory(
        key="dependency",
        title="依赖图与环检测",
        title_key="",
        scope="依赖边、接口提供者、环检测、版本校验、工件身份",
        cli_only=False,
        commands=[
            _cmd("dependency",
                 default_desc="依赖图、环检测与接口提供者管理"),
        ],
    ),
    CommandCategory(
        key="assignment_lease",
        title="Assignment 与 Lease",
        title_key="",
        scope="lease 获取/续租/释放、assignment 创建/查询/撤销",
        cli_only=False,
        commands=[
            _cmd("lease",
                 default_desc="租约管理（acquire/renew/release/status/events）"),
            _cmd("assignment", default_desc="assignment 创建、查询与撤销"),
        ],
    ),
    CommandCategory(
        key="identity",
        title="Identity 与 Attestation",
        title_key="",
        scope="Attestation 撤销（CLI 仅暴露 revoke，避免 T=M=D）",
        cli_only=False,
        commands=[
            _cmd("identity", default_desc="Identity/Attestation 撤销（revoke）"),
        ],
    ),
    # ---- 以下为 CLI-only 能力域（MCP 侧无同构分类）----
    CommandCategory(
        key="rollback",
        title="Migration Rollback",
        title_key="",
        scope="全量迁移自举计划的回滚配置登记与紧急回滚开关",
        cli_only=True,
        commands=[
            _cmd("rollback",
                 default_desc="迁移回滚（register/show/config/set/is-rolled-back）"),
        ],
    ),
    CommandCategory(
        key="daemon_ops",
        title="Daemon 运维",
        title_key="",
        scope="daemon 启动/状态/查询/快照/备份等运维操作（UDS/HTTP 客户端）",
        cli_only=True,
        commands=[
            _cmd("daemon",
                 default_desc="daemon 运维（ping/start/list/status/publish/query 等 23 子命令）"),
        ],
    ),
    CommandCategory(
        key="setup_install",
        title="安装与初始化",
        title_key="",
        scope="安装部署、Agent 集成包、Git hook、MCP Server、测试运行器",
        cli_only=True,
        commands=[
            _cmd("install", default_desc="安装部署 Call Warden（cw.py standalone）"),
            _cmd("install-agent", desc_key="cli.messages.help_install_agent"),
            _cmd("install-hook", desc_key="cli.messages.help_install_hook"),
            _cmd("setup", default_desc="自动配置已安装 AI 工具的 MCP 集成"),
            _cmd("server", default_desc="启动 MCP Server（stdio/SSE）"),
            _cmd("test", default_desc="运行 tests/ 下的测试模块"),
        ],
    ),
    CommandCategory(
        key="experiment",
        title="盲评实验",
        title_key="",
        scope="P0 盲评对照实验的批次/纳样/指标记录/揭示全生命周期",
        cli_only=True,
        commands=[
            _cmd("experiment",
                 default_desc="盲评对照实验（batch-create/admit/record-metrics/reveal 等）"),
        ],
    ),
]


def all_command_names() -> List[str]:
    """按分类顺序展开全部顶层命令名（供校验与文档生成使用）。"""
    return [c.name for cat in COMMAND_CATEGORIES for c in cat.commands]


def get_category_of(command: str) -> CommandCategory:
    """查询命令所属分类；未注册时抛 KeyError（调用方应保证先校验）。"""
    for cat in COMMAND_CATEGORIES:
        for c in cat.commands:
            if c.name == command:
                return cat
    raise KeyError(f"command not categorized: {command}")
