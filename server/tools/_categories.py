"""MCP 工具分类真相源（cli-mcp-surface-audit Phase 1）。

本模块是全部 243 个 MCP 工具归属的唯一权威数据（17 分类），清单迁自
docs/mcp_tools.md「各分类工具清单」章节（2026-10-08 审计逐条核实）。

- 调用链 1：scripts/gen_category_overview.py 读取 TOOL_CATEGORIES 生成
  docs/mcp_tools.md 概览表标记块（含「对应 CLI 主分类」列，修复
  [13]-[17] 与文末增量映射的自相矛盾）；
- 调用链 2：tests/test_category_source.py 校验「17 分类恰好划分
  server/tools/*.py 注册的全部 @mcp.tool() 工具（无重复、无遗漏）」。

分类 key 与 cli/categories.py 的 COMMAND_CATEGORIES 同构（[1]-[17]）；
cli_category 字段指向同构的 CLI 分类 key，供概览表引用。工具本体、
命名与注册顺序不受本模块影响（仅审计与归档，不重命名）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class ToolCategory:
    """MCP 工具分类。

    key:          稳定标识（与 cli/categories.py 同构 key 对应）
    title:        分类标题（与 mcp_tools.md 既有标题一致）
    scope:        概览表「涵盖范围」列文案
    cli_category: 对应的 CLI 分类 key（cli.categories.COMMAND_CATEGORIES
                  中必须存在；概览表「对应 CLI 主分类」列数据源）
    tools:        该分类的工具名列表（@mcp.tool() 注册名原样）
    """

    key: str
    title: str
    scope: str
    cli_category: str
    tools: List[str] = field(default_factory=list)


# ====================================================================
# 17 分类 × 243 工具（清单迁自 docs/mcp_tools.md L193-259，已逐条核实）
# ====================================================================

TOOL_CATEGORIES: List[ToolCategory] = [
    ToolCategory(
        key="workspace_database",
        title="Workspace & Database",
        scope="workspace / db 构建 / branch",
        cli_category="workspace_database",
        tools=[
            "list_workspaces", "register_workspace", "set_active_workspace",
            "delete_workspace", "get_active_workspace", "build_graph",
            "refresh_file", "build_directory", "remove_file", "get_stats",
            "get_status", "register_branch", "list_branches", "diff_branches",
            "switch_branch", "merge_preview",
        ],
    ),
    ToolCategory(
        key="query_search",
        title="Query & Search",
        scope="符号 / 文件 / 语义搜索 / 摘要 / RAG / Token 账本",
        cli_category="query_search",
        tools=[
            "search_symbols", "get_symbol", "get_symbol_location",
            "get_file_symbols", "get_symbol_history", "get_file_history",
            "get_recent_changes", "get_symbol_content_by_hash", "file_read",
            "file_grep", "file_list", "file_symbol_content",
            "semantic_search", "find_similar_functions", "embed_symbols",
            "embed_symbols_async", "embed_single_symbol", "generate_summary",
            "get_summary", "project_brief", "repo_map", "ask_codebase",
            "record_token_savings", "get_token_savings_report",
        ],
    ),
    ToolCategory(
        key="call_chain",
        title="Call Chain Analysis",
        scope="调用链 / 拓扑 / 循环 / 孤儿 / 模块图 / 热力图 / 调用差异",
        cli_category="call_chain",
        tools=[
            "get_callers", "get_callees", "get_impact", "get_call_chain_down",
            "get_top_callers", "get_orphan_symbols", "get_deepest_functions",
            "get_module_call_stats", "detect_call_cycles", "get_call_heatmap",
            "export_module_graph", "get_topological_order", "diff_callers",
            "diff_callees",
        ],
    ),
    ToolCategory(
        key="code_health",
        title="Code Health & Metrics",
        scope="度量 / 健康检查 / 演化 / 热点 / 流失 / 缺陷关联",
        cli_category="code_health",
        tools=[
            "get_code_metrics_summary", "get_complexity_hotspots",
            "get_coupling_analysis", "get_function_metrics",
            "get_largest_functions", "get_most_coupled_functions",
            "get_code_health_check", "check_file_health",
            "evolution_frequency", "defect_correlation",
            "hotspot_evolution", "churn_analysis",
        ],
    ),
    ToolCategory(
        key="task",
        title="Task Orchestration",
        scope="任务 CRUD / 审批 / 质量门禁 / 符号归因 / capture-diff / 后台 job / 任务-提交关联 / assignment / role prompt / remediation / step 解决",
        cli_category="task",
        tools=[
            "task_create", "task_create_subtask", "task_split",
            "task_create_from_plan", "task_plan_template", "task_next_step",
            "work_next_job", "task_resolve_block", "task_report_step",
            "task_rollback", "task_apply", "task_close", "task_capture_diff",
            "task_list", "task_status", "task_governance_projection",
            "task_status_tree", "task_completion_review",
            "task_quality_findings", "task_resolve_quality_finding",
            "record_task_symbol_change", "link_edit_audit_symbols",
            "get_task_symbol_changes", "get_symbol_change_tasks", "cancel_job",
            "list_jobs", "get_job_stats", "wait_for_job", "get_job_status",
            "get_task_commits", "get_commit_tasks", "task_assignment_status",
            "task_assignment_heartbeat", "task_get_role_prompt",
            "task_remediation_create", "task_step_resolve",
        ],
    ),
    ToolCategory(
        key="rule_memory",
        title="Agent Rule Memory",
        scope="候选 / 审核 / 生效 / 同步 / 提取 / 清理 / 种子",
        cli_category="rule_memory",
        tools=[
            "rule_candidate_create", "rule_candidate_list",
            "rule_candidate_accept", "rule_candidate_reject", "rule_list",
            "get_applicable_rules", "rule_sync_agents_md",
            "rule_insert_agents_md_block",
            "extract_rule_candidates_from_quality_findings",
            "rule_seed_bootstrap", "cleanup_agent_rule_sync_log",
        ],
    ),
    ToolCategory(
        key="audit_bootstrap",
        title="Audit & Bootstrap",
        scope="审计链 / 密钥轮换 / 自举 / 检查门禁 / 安全护栏",
        cli_category="audit_bootstrap",
        tools=[
            "audit_verify_chain", "rotate_audit_signing_key",
            "list_audit_signing_keys", "bootstrap_status", "run_check_gate",
            "resolve_gate_findings", "guardrail_scan", "guardrail_check_edit",
            "guardrail_list_rules", "guardrail_add_rule",
        ],
    ),
    ToolCategory(
        key="git",
        title="Git Integration",
        scope="git 历史 / commit / 变更 / 统计 / 符号历史 / 快照对比",
        cli_category="git",
        tools=[
            "import_git_history", "get_git_commits", "get_commit_changes",
            "get_git_stats", "get_symbol_commit_history", "compare_snapshots",
        ],
    ),
    ToolCategory(
        key="semgrep_defects",
        title="Semgrep & Defects",
        scope="Semgrep / 缺陷知识库 / 影响半径 / 审查就绪 / 跨层 / 符号静态检查 / 变更-缺陷关联",
        cli_category="semgrep_defects",
        tools=[
            "run_semgrep_scan", "semgrep_scan_async",
            "scan_semgrep_incremental", "get_semgrep_stats",
            "get_semgrep_findings", "get_issue_summary", "get_symbol_issues",
            "find_issues", "defect_search", "defect_suggest_fix",
            "defect_learn", "defect_stats", "get_defect_correlation",
            "blast_radius", "get_vulnerability_blast_radius",
            "diff_to_symbol", "review_readiness", "cross_layer_impact",
        ],
    ),
    ToolCategory(
        key="coverage_ownership",
        title="Coverage & Ownership",
        scope="注释 / 测试覆盖率 / 测试 case 关联 / 测试稳定性 / CODEOWNERS / 所有权 / 注释恢复",
        cli_category="coverage_ownership",
        tools=[
            "get_comment_coverage", "get_uncommented_symbols",
            "get_test_coverage", "get_test_cases", "get_tested_functions",
            "get_test_coverage_summary", "get_test_stability",
            "get_comment_from_version", "restore_comment",
            "restore_all_comments", "import_coverage",
            "get_coverage_for_symbol", "find_uncovered_functions",
            "test_impact_selection", "who_to_ask", "get_ownership_map",
            "parse_codeowners", "import_codeowners", "import_git_blame",
        ],
    ),
    ToolCategory(
        key="gc",
        title="GC",
        scope="外部符号 / retention / policy / 备份 / 审计",
        cli_category="gc",
        tools=[
            "get_project_dependencies", "import_project_dependencies",
            "prune_external_symbols", "gc_retention", "gc_policy_get",
            "gc_policy_set", "gc_archive_list", "gc_archive_inspect",
            "gc_archive_import", "gc_audit_list", "gc_audit_get",
        ],
    ),
    ToolCategory(
        key="diagnostics",
        title="Diagnostics",
        scope="clone 检测 / clone group / LSP / 安全编辑 / 跨仓库分析 / clone 感知影响 / daemon 运行时指标",
        cli_category="diagnostics",
        tools=[
            "detect_clones", "detect_clones_async", "list_clone_groups",
            "get_clone_group_detail", "get_clone_group_stats", "list_clones",
            "get_clone_stats", "clear_clones", "get_clone_aware_impact",
            "propose_edit", "propose_range_patch", "propose_symbol_patch",
            "propose_symbol_id_patch", "revert_edit", "get_edit_history",
            "get_edit_stats", "detect_cross_repo_deps",
            "find_shared_symbols", "cross_repo_impact", "cross_repo_summary",
            "lsp_hover", "lsp_definition", "lsp_references",
            "lsp_diagnostics", "lsp_completion", "lsp_check_available",
            "get_metrics",
        ],
    ),
    ToolCategory(
        key="build_context",
        title="构建上下文感知",
        scope="工具链注册 / build context / resolved edges",
        cli_category="build_context",
        tools=[
            "get_toolchain", "list_toolchains", "get_build_context",
            "list_build_contexts", "get_active_build_context",
            "get_workspace_toolchains", "get_resolved_edges",
            "count_resolved_edges",
        ],
    ),
    ToolCategory(
        key="collab",
        title="只读协同查询",
        scope="协同证据 / 门禁决策 / 角色视图 / 新鲜度",
        cli_category="collab",
        tools=[
            "append_evidence", "find_evidence", "get_freshness_status",
            "get_gate_decision", "get_role_view", "submit_verdict",
        ],
    ),
    ToolCategory(
        key="dependency",
        title="依赖图与环检测",
        scope="依赖边 / 接口提供者 / 环检测 / 版本校验 / 工件身份",
        cli_category="dependency",
        tools=[
            "build_hard_dependency_edges", "get_dependency_edges",
            "detect_dependency_cycle", "validate_revision_dependencies",
            "publish_interface", "get_interface_providers",
            "select_interface_provider", "import_envelope_dependencies",
            "record_artifact_identity", "get_artifact_freshness",
        ],
    ),
    ToolCategory(
        key="assignment_lease",
        title="Assignment 与 Lease",
        scope="lease 获取 / 续租 / 释放 / assignment 创建撤销",
        cli_category="assignment_lease",
        tools=[
            "lease_acquire", "lease_renew", "lease_release", "lease_status",
            "lease_list_events", "assignment_create", "assignment_show",
            "assignment_revoke",
        ],
    ),
    ToolCategory(
        key="identity",
        title="Identity 与 Attestation",
        scope="动作身份 / 会话隔离 / attestation 撤销 / 注册",
        cli_category="identity",
        tools=[
            "record_action_identity", "get_action_identity",
            "check_action_identity", "check_session_separation",
            "get_attestation_validity", "register_attestation_revocation",
            "list_attestation_revocations",
        ],
    ),
]


def all_tool_names() -> List[str]:
    """按分类顺序展开全部工具名（供校验与文档生成使用）。"""
    return [name for cat in TOOL_CATEGORIES for name in cat.tools]


# ====================================================================
# MCP 工具 → CLI 入口全量映射（cli-mcp-surface-audit Phase 2，2026-10-08）
# --------------------------------------------------------------------
# 值：(CLI 命令路径, 说明) 或 None（MCP 专属，无 CLI 入口）。
# 规则：
# - CLI 命令路径不含 "cw " 前缀：顶层命令（如 "stats"）或含子命令
#   （如 "task next"、"gc archive-inspect"、"rule candidate create"）；
#   带 flag/参数占位的用法只存命令路径，flag 写进说明（如
#   `cw refresh --all` 存 "refresh"、`cw evolution --defects` 存 "evolution"）。
# - [18]-[21] CLI-only 分类（rollback / daemon_ops / setup_install /
#   experiment）不参与映射（MCP 侧无同构域，属有意的暴露面差异）。
#
# 数据来源：docs/mcp_tools.md「CLI↔MCP 命名映射对照表」人工审计版
# 逐条转录；转录后对每条非 None 路径做了 CLI 侧存在性核对
# （cli/main.py + cli/daemon_commands.py 的 add_parser 静态提取，
# 见 tests/test_category_source.py::TestToolCliMapping），并对疑似
# 错位条目做了 RPC 级复核（如 `cw impact` 实调 blast_radius 而非
# get_impact、`cw symbol-history` 实调 get_symbol_commit_history）。
#
# 复核（mcp-cli-parity-audit，2026-10-08）：对全部 100 条 None 判定做
# RPC 级交叉比对（.temp/rpc_cross_check.py，AST 提取两侧工具/handler
# 函数体的 RPC 调用与 db 方法→RpcDBProxy._METHOD_MAP 映射），修正
# 10 条漏映射（5 条同 RPC 实锤 + 5 条语义等价），其余 90 条确认
# MCP-only（判定与证据明细见 docs/design/mcp-cli-parity-audit.md）。
# ====================================================================

TOOL_CLI_MAPPING: "dict[str, tuple[str, str] | None]" = {
    # [1] workspace_database
    "list_workspaces": ("workspace list", "列出所有工作区"),
    "register_workspace": ("workspace register", "注册新工作区"),
    "set_active_workspace": ("workspace set", "设置活动工作区"),
    "delete_workspace": ("workspace delete", "删除工作区"),
    "get_active_workspace": None,
    "build_graph": ("refresh", "全量构建代码图谱（cw refresh --all）"),
    "refresh_file": ("refresh", "刷新指定文件（cw refresh <paths>）"),
    "build_directory": None,
    "remove_file": None,
    "get_stats": ("stats", "代码图谱统计信息"),
    "get_status": ("status", "完整状态概览"),
    "register_branch": None,
    "list_branches": None,
    "diff_branches": None,
    "switch_branch": None,
    "merge_preview": None,

    # [2] query_search
    "search_symbols": ("search", "模糊搜索符号"),
    "get_symbol": ("symbol", "符号详情"),
    "get_symbol_location": ("query", "符号定位"),
    "get_file_symbols": ("file", "文件符号列表"),
    "get_symbol_history": None,  # CLI symbol-history 实调 get_symbol_commit_history
    "get_file_history": None,
    "get_recent_changes": ("changes", "近期变更"),
    "get_symbol_content_by_hash": ("diff", "按 hash 取符号内容（cw diff <hash1> <hash2> 取两版本内容对比；单取内容无独立入口，语义等价）"),
    "file_read": None,
    "file_grep": ("grep", "搜索内容（带符号归属）"),
    "file_list": None,
    "file_symbol_content": None,
    "semantic_search": ("semantic-search", "语义搜索"),
    "find_similar_functions": ("similar", "相似函数"),
    "embed_symbols": ("embed", "批量向量嵌入"),
    "embed_symbols_async": None,
    "embed_single_symbol": None,
    "generate_summary": None,
    "get_summary": None,
    "project_brief": ("brief", "项目简报"),
    "repo_map": ("map", "仓库模块图"),
    "ask_codebase": None,
    "record_token_savings": None,
    "get_token_savings_report": ("health-report", "Token 节省报告（cw health-report 的 token_savings 段，固定 30d 窗口；MCP 支持自定义窗口）"),

    # [3] call_chain
    "get_callers": ("callers", "调用者查询"),
    "get_callees": ("callees", "被调用者查询"),
    "get_call_chain_down": ("call-chain", "调用链向下"),
    "get_impact": None,  # CLI impact 实调 blast_radius，get_impact 无 CLI 入口
    "get_topological_order": ("topo", "拓扑排序"),
    "get_top_callers": ("top-callers", "调用排行"),
    "get_orphan_symbols": ("orphan-symbols", "孤儿符号"),
    "get_deepest_functions": ("deepest", "最深函数"),
    "get_module_call_stats": ("module-calls", "模块调用统计"),
    "detect_call_cycles": ("detect-cycles", "调用图循环检测"),
    "get_call_heatmap": ("call-heatmap", "调用热力图"),
    "export_module_graph": ("export-module-graph", "模块图导出"),
    "diff_callers": None,
    "diff_callees": None,

    # [4] code_health
    "get_code_metrics_summary": ("metrics", "度量汇总"),
    "get_complexity_hotspots": ("complexity", "复杂度热点"),
    "get_coupling_analysis": ("coupling", "耦合分析"),
    "get_function_metrics": ("fn-metrics", "单函数度量"),
    "get_largest_functions": ("largest-fns", "最大函数"),
    "get_most_coupled_functions": ("coupled-fns", "高耦合函数"),
    "get_code_health_check": None,
    "check_file_health": None,
    "evolution_frequency": ("evolution", "变更频率（cw evolution <QN>）"),
    "defect_correlation": None,  # CLI evolution --defects 实调 get_defect_correlation
    "hotspot_evolution": ("hotspot", "热点演化"),
    "churn_analysis": ("churn", "代码流失"),

    # [5] task
    "task_create": ("task create", "创建任务"),
    "task_create_subtask": ("task create", "创建子任务（cw task create --parent-id；MCP 走 task.create_subtask 专项治理，语义等价）"),
    "task_split": ("task split", "拆分任务"),
    "task_create_from_plan": None,
    "task_plan_template": None,
    "task_next_step": ("task next", "认领步骤"),
    "work_next_job": None,
    "task_resolve_block": None,
    "task_report_step": ("task report", "上报步骤"),
    "task_rollback": ("task rollback", "回滚任务"),
    "task_apply": ("task apply", "审核通过"),
    "task_close": ("task close", "关闭任务"),
    "task_capture_diff": ("task capture-diff", "捕获改动"),
    "task_list": ("task list", "列出任务"),
    "task_status": ("task show", "任务详情"),
    "task_governance_projection": ("task governance-projection", "治理投影"),
    "task_status_tree": ("task status-tree", "任务树"),
    "task_completion_review": ("task completion-review", "完成审查"),
    "task_quality_findings": ("task findings", "质量发现"),
    "task_resolve_quality_finding": ("task resolve-finding", "解决发现"),
    "record_task_symbol_change": None,
    "link_edit_audit_symbols": None,
    "get_task_symbol_changes": ("task show", "任务归因符号变化（cw task show 的 Related 段，RPC task.get_symbol_changes）"),
    "get_symbol_change_tasks": ("symbol-history", "符号反查关联任务（cw symbol-history 的 Related Tasks 段，同 RPC 原名路由）"),
    "cancel_job": None,
    "list_jobs": None,
    "get_job_stats": None,
    "wait_for_job": None,
    "get_job_status": None,
    "get_task_commits": ("task show", "任务关联 commit（cw task show 的 Related 段，RPC task.get_commits）"),
    "get_commit_tasks": ("git show", "commit 关联任务（cw git show 的 Related Tasks 段；CLI 走 db.get_commit_tasks 原名路由，MCP 走 query.commit_tasks，语义等价）"),
    "task_assignment_status": ("task assignment-status", "派工队列投影"),
    "task_assignment_heartbeat": ("task assignment-heartbeat", "派工心跳"),
    "task_get_role_prompt": ("task prompt", "角色提示编译"),
    "task_remediation_create": None,
    "task_step_resolve": ("task step-resolve", "失败步骤回审"),

    # [6] rule_memory
    "rule_candidate_create": ("rule candidate create", "创建候选"),
    "rule_candidate_list": ("rule candidate list", "候选列表"),
    "rule_candidate_accept": ("rule candidate accept", "接受候选"),
    "rule_candidate_reject": ("rule candidate reject", "拒绝候选"),
    "rule_list": ("rule list", "已生效规则"),
    "get_applicable_rules": ("rule applicable", "上下文匹配"),
    "rule_sync_agents_md": ("rule sync", "同步 AGENTS.md"),
    "rule_insert_agents_md_block": ("rule insert-block", "插入标记块"),
    "extract_rule_candidates_from_quality_findings": ("rule extract", "提取候选"),
    "rule_seed_bootstrap": ("rule seed-bootstrap", "种子化"),
    "cleanup_agent_rule_sync_log": ("rule cleanup-sync-log", "清理日志"),

    # [7] audit_bootstrap
    "audit_verify_chain": ("audit verify", "审计链验证"),
    "rotate_audit_signing_key": ("audit rotate-key", "密钥轮换"),
    "list_audit_signing_keys": ("audit keys", "密钥列表"),
    "bootstrap_status": ("bootstrap status", "自举健康"),
    "run_check_gate": ("check-gate", "检查门禁"),
    "resolve_gate_findings": ("check-gate", "解决门禁发现（cw check-gate <task_id> --resolve，经 _METHOD_MAP 同 RPC gate.resolve_findings；CLI 仅传 task_id，MCP 契约为 gate_id 必填）"),
    "guardrail_scan": ("guardrail scan", "安全扫描"),
    "guardrail_check_edit": None,  # CLI guardrail 域仅有 scan/rules 子命令
    "guardrail_list_rules": ("guardrail rules", "规则列表"),
    "guardrail_add_rule": None,

    # [8] git
    "import_git_history": ("git import", "导入 Git 历史"),
    "get_git_commits": ("git log", "commit 列表"),
    "get_commit_changes": ("git show", "commit 详情"),
    "get_git_stats": ("git stats", "Git 统计"),
    "get_symbol_commit_history": ("symbol-history", "符号 Git 变更历史"),
    "compare_snapshots": None,

    # [9] semgrep_defects
    "run_semgrep_scan": ("semgrep scan", "Semgrep 扫描"),
    "semgrep_scan_async": None,
    "scan_semgrep_incremental": ("semgrep scan", "增量 Semgrep 扫描（cw semgrep scan --incremental；MCP 经 job 通道 sync 执行，语义等价）"),
    "get_semgrep_stats": ("semgrep stats", "Semgrep 统计"),
    "get_semgrep_findings": ("semgrep list", "Semgrep 发现"),
    "get_issue_summary": ("function-issues", "缺陷汇总（cw function-issues --summary）"),
    "get_symbol_issues": ("issues", "符号静态检查（Semgrep + Guardrail 聚合）"),
    "find_issues": ("function-issues", "缺陷查找"),
    "defect_search": ("defect search", "缺陷模式搜索"),
    "defect_suggest_fix": ("defect suggest", "修复建议"),
    "defect_learn": ("defect learn", "从修复学习"),
    "defect_stats": ("defect stats", "缺陷库统计"),
    "get_defect_correlation": ("evolution", "变更-缺陷关联（cw evolution --defects）"),
    "blast_radius": ("impact", "变更影响半径（cw impact <hash>，BFS 反向调用图）"),
    "get_vulnerability_blast_radius": ("vuln-blast", "漏洞爆炸半径"),
    "diff_to_symbol": None,
    "review_readiness": ("review", "审查就绪报告"),
    "cross_layer_impact": None,

    # [10] coverage_ownership
    "get_comment_coverage": ("comment-coverage", "注释覆盖率"),
    "get_uncommented_symbols": ("uncommented", "未注释符号"),
    "get_test_coverage": ("coverage test", "测试覆盖率"),
    "get_test_cases": ("tests", "符号的测试 case 列表（cw tests <QN>）"),
    "get_tested_functions": ("tests", "反向查询（cw tests <QN> --reverse）"),
    "get_test_coverage_summary": None,  # CLI tests 无 --coverage flag
    "get_test_stability": ("tests", "测试稳定性（cw tests <QN> --history）"),
    "get_comment_from_version": ("restore-comment", "历史版本注释预览（cw restore-comment <spec> --preview 只读预览，语义等价）"),
    "restore_comment": ("restore-comment", "恢复注释"),
    "restore_all_comments": ("restore-all-comments", "批量恢复注释"),
    "import_coverage": ("coverage import", "导入覆盖率"),
    "get_coverage_for_symbol": ("coverage fn", "函数覆盖率"),
    "find_uncovered_functions": ("coverage uncovered", "未覆盖函数"),
    "test_impact_selection": ("test-impact", "测试影响选择"),
    "who_to_ask": ("who", "文件负责人"),
    "get_ownership_map": ("ownership-map", "所有权映射"),
    "parse_codeowners": None,
    "import_codeowners": None,
    "import_git_blame": None,

    # [11] gc
    "get_project_dependencies": None,
    "import_project_dependencies": None,
    "prune_external_symbols": None,
    "gc_retention": ("gc retention", "GC retention 清理"),
    "gc_policy_get": ("gc policy show", "策略查询"),
    "gc_policy_set": ("gc policy set", "策略设置"),
    "gc_archive_list": ("gc archive-list", "备份列表"),
    "gc_archive_inspect": ("gc archive-inspect", "检查备份"),
    "gc_archive_import": ("gc archive-import", "导入备份"),
    "gc_audit_list": ("gc audit-list", "审计历史"),
    "gc_audit_get": ("gc audit-show", "审计详情"),

    # [12] diagnostics
    "detect_clones": ("clone detect", "克隆检测"),
    "detect_clones_async": None,
    "list_clone_groups": None,
    "get_clone_group_detail": None,
    "get_clone_group_stats": None,
    "list_clones": ("clone list", "克隆列表"),
    "get_clone_stats": ("clone stats", "克隆统计"),
    "clear_clones": ("clone clear", "清空克隆"),
    "get_clone_aware_impact": None,
    "propose_edit": None,
    "propose_range_patch": None,
    "propose_symbol_patch": None,
    "propose_symbol_id_patch": None,
    "revert_edit": None,
    "get_edit_history": None,
    "get_edit_stats": None,
    "detect_cross_repo_deps": None,
    "find_shared_symbols": None,
    "cross_repo_impact": None,
    "cross_repo_summary": None,
    "lsp_hover": None,
    "lsp_definition": None,
    "lsp_references": None,
    "lsp_diagnostics": None,
    "lsp_completion": None,
    "lsp_check_available": None,

    # [13] build_context
    "get_toolchain": ("toolchain show", "工具链详情（按 name 或 ID）"),
    "list_toolchains": ("toolchain list", "工具链列表"),
    "get_build_context": ("build-context show", "build context 详情"),
    "list_build_contexts": ("build-context list", "build context 列表"),
    "get_active_build_context": None,  # CLI activate 为写（set_active），查询无入口
    "get_workspace_toolchains": ("toolchain list-bound", "workspace 绑定工具链"),
    "get_resolved_edges": ("build-context edges", "已解析边查询"),
    "count_resolved_edges": ("build-context show", "show 详情含 resolved edges 计数"),
    "get_metrics": None,  # cw daemon metrics 属 CLI-only daemon_ops 域

    # [14] collab
    "append_evidence": None,  # CLI collab publish 走 snapshot.publish，非 evidence.append
    "find_evidence": None,  # CLI collab reveal 走 reveal.submit（写），查询无入口
    "get_freshness_status": None,
    "get_gate_decision": None,  # CLI collab gate-trigger 走 gate.decide（写）
    "get_role_view": None,
    "submit_verdict": ("collab verdict", "提交裁决（verdict.submit）"),

    # [15] dependency
    "build_hard_dependency_edges": ("dependency inspect", "硬依赖边构建"),
    "get_dependency_edges": ("dependency list", "依赖边查询"),
    "detect_dependency_cycle": ("dependency cycle", "依赖图环检测"),
    "validate_revision_dependencies": ("dependency explain", "版本依赖校验"),
    "publish_interface": None,
    "get_interface_providers": None,
    "select_interface_provider": ("dependency provider-select", "provider 选择"),
    "import_envelope_dependencies": None,
    "record_artifact_identity": None,
    "get_artifact_freshness": None,

    # [16] assignment_lease
    "lease_acquire": ("lease acquire", "获取 lease"),
    "lease_renew": ("lease renew", "续租"),
    "lease_release": ("lease release", "释放"),
    "lease_status": ("lease status", "lease 状态"),
    "lease_list_events": ("lease list", "lease 事件"),
    "assignment_create": ("assignment create", "创建 assignment"),
    "assignment_show": ("assignment show", "assignment 详情"),
    "assignment_revoke": ("assignment revoke", "撤销 assignment"),

    # [17] identity
    "record_action_identity": None,
    "get_action_identity": None,
    "check_action_identity": None,
    "check_session_separation": None,
    "get_attestation_validity": None,
    "register_attestation_revocation": ("identity revoke", "撤销 attestation"),
    "list_attestation_revocations": None,
}


def cli_only_in_shared_categories(cli_categories=None) -> "List[tuple[str, List[str]]]":
    """[1]-[17] 共享分类域内，无任何 MCP 工具映射的 CLI 顶层命令（按分类分组）。

    判定：cli_only=False 分类的命令，若其命令名未出现在
    TOOL_CLI_MAPPING 任何非 None 值的命令路径首段中，则为 CLI 独有
    （如 `graph`、`config`、`fts`、`doctor`）。按「命令级」判定：
    像 `task`（31 个子命令仅 19 个有 MCP 对应）只要任一子命令有映射
    即不算 CLI 独有——子命令级缺口由 TOOL_CLI_MAPPING 全表自行呈现。

    Args:
        cli_categories: COMMAND_CATEGORIES 数据（避免 scripts/ 按路径
            加载本模块时反向 import cli 包；缺省时尝试常规导入）。

    Returns:
        [(分类 key, [命令名...]), ...]，仅含有 CLI 独有命令的分类。
    """
    if cli_categories is None:
        try:
            from callwarden.cli.categories import COMMAND_CATEGORIES as cli_categories
        except ImportError:
            from cli.categories import COMMAND_CATEGORIES as cli_categories  # type: ignore
    # 映射值中出现过的顶层命令集合（命令级前缀判定）
    mapped_top = {
        entry[0].split()[0]
        for entry in TOOL_CLI_MAPPING.values()
        if entry is not None
    }
    result: "List[tuple[str, List[str]]]" = []
    for cat in cli_categories:
        if cat.cli_only:
            continue
        only = [c.name for c in cat.commands if c.name not in mapped_top]
        if only:
            result.append((cat.key, only))
    return result


def get_tool_category_of(tool: str) -> ToolCategory:
    """查询工具所属分类；未注册时抛 KeyError（调用方应保证先校验）。"""
    for cat in TOOL_CATEGORIES:
        if tool in cat.tools:
            return cat
    raise KeyError(f"tool not categorized: {tool}")
