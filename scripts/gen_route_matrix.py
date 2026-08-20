#!/usr/bin/env python3
"""gen_route_matrix.py —— 从工具路由矩阵生成派生物（T01 脚手架）。

设计契约：`deliverables/software-company/tool_migration_matrix.json` 是 239 个
MCP 工具的**单一真相源**；本脚本只做读取与派生，不修改矩阵内容。

输出（写入仓库对应位置）：
- `--emit-json`           刷新/校验矩阵 JSON（工具名集合来自本文件 TOOL_MODULES 提取，
                          路由元数据来自 ROUTE_OVERRIDES；二者合并后写回 JSON）。
- `--emit-dispatch`       打印 dispatch.rs 分支声明清单（人工核对/生成代码片段）。
- `--emit-registry`       打印 capability registry 行清单。
- `--emit-whitelist`      打印 COMPAT_ROUTE_WHITELIST 条目清单。
- `--emit-shell`          打印 Python 薄壳工具函数骨架（name → rpc_method 映射）。
- `--report`              打印迁移核对报告（backend/op_class/batch 分布）。

用法：
    python scripts/gen_route_matrix.py --emit-json
    python scripts/gen_route_matrix.py --report
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MATRIX_PATH = os.path.join(
    _REPO_ROOT, "deliverables", "software-company", "tool_migration_matrix.json"
)

# ---------------------------------------------------------------------------
# 1. 工具模块清单（与 server/tools/__init__.py 的 _MODULES 一致）
# ---------------------------------------------------------------------------
TOOL_MODULES: List[str] = [
    "tools_query",
    "tools_workspace",
    "tools_semantic",
    "tools_task",
    "tools_summary",
    "tools_security",
    "tools_rules",
    "tools_collab",
    "tools_p2_graph",
    "tools_p3_identity",
    "tools_p4_lease",
]


def extract_tool_names(module_name: str) -> List[str]:
    """从 server/tools/<module>.py 提取 @mcp.tool() 装饰的 MCP 工具函数名。

    兼容 `@mcp.tool()` 与 `@mcp.tool(name=...)` 两种装饰器写法；
    找不到文件时回退到内置清单（保证脚手架在薄壳化前后均可用）。
    """
    path = os.path.join(_REPO_ROOT, "server", "tools", f"{module_name}.py")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        return _FALLBACK_NAMES.get(module_name, [])
    names = re.findall(r"@mcp\.tool\([^)]*\)\s*\n\s*def (\w+)\(", src)
    names += re.findall(r"@mcp\.tool\(\)\s*\n\s*def (\w+)\(", src)
    return sorted(set(names))


# 内置回退清单（与 `python - <<` 提取结果一致；薄壳化后可删）
_FALLBACK_NAMES: Dict[str, List[str]] = {
    "tools_query": [
        "detect_cycles", "export_module_graph", "find_issues", "get_call_chain_down",
        "get_call_heatmap", "get_callees", "get_callers", "get_comment_coverage",
        "get_comment_from_version", "get_deepest_functions", "get_file_history",
        "get_file_symbols", "get_impact", "get_issue_summary", "get_module_call_stats",
        "get_orphan_symbols", "get_recent_changes", "get_semgrep_findings",
        "get_semgrep_stats", "get_stats", "get_symbol", "get_symbol_history",
        "get_symbol_location", "get_test_coverage", "get_top_callers",
        "get_topological_order", "get_uncommented_symbols", "restore_all_comments",
        "restore_comment", "run_semgrep_scan", "scan_semgrep_incremental",
        "search_symbols",
    ],
    "tools_workspace": [
        "build_directory", "build_graph", "check_file_health", "delete_workspace",
        "file_grep", "file_list", "file_read", "file_symbol_content",
        "get_active_workspace", "get_code_health_check", "get_code_metrics_summary",
        "get_commit_changes", "get_complexity_hotspots", "get_coupling_analysis",
        "get_function_metrics", "get_git_commits", "get_git_stats",
        "get_largest_functions", "get_most_coupled_functions", "get_status",
        "get_symbol_content_by_hash", "import_git_history", "list_workspaces",
        "refresh_file", "register_workspace", "remove_file", "set_active_workspace",
    ],
    "tools_semantic": [
        "embed_single_symbol", "embed_symbols", "find_similar_functions",
        "gc_archive_import", "gc_archive_inspect", "gc_archive_list", "gc_audit_get",
        "gc_audit_list", "gc_policy_get", "gc_policy_set", "gc_retention",
        "get_project_dependencies", "get_symbol_commit_history", "import_codeowners",
        "import_git_blame", "import_project_dependencies", "parse_codeowners",
        "prune_external_symbols", "semantic_search",
    ],
    "tools_task": [
        "audit_verify_chain", "bootstrap_status", "cancel_job",
        "cleanup_agent_rule_sync_log", "clear_clones", "detect_clones",
        "detect_clones_async", "embed_symbols_async", "get_clone_group_detail",
        "get_clone_group_stats", "get_clone_stats", "get_commit_tasks",
        "get_defect_correlation", "get_job_stats", "get_job_status",
        "get_symbol_change_tasks", "get_symbol_issues", "get_task_commits",
        "get_task_symbol_changes", "get_test_cases", "get_test_coverage_summary",
        "get_test_stability", "get_tested_functions", "link_edit_audit_symbols",
        "list_audit_signing_keys", "list_clone_groups", "list_clones", "list_jobs",
        "record_task_symbol_change", "rotate_audit_signing_key", "rule_seed_bootstrap",
        "semgrep_scan_async", "task_apply", "task_capture_diff", "task_close",
        "task_completion_review", "task_create", "task_create_from_plan",
        "task_create_subtask", "task_list", "task_next_step", "task_plan_template",
        "task_quality_findings", "task_report_step", "task_resolve_block",
        "task_resolve_quality_finding", "task_rollback", "task_split", "task_status",
        "task_status_tree", "wait_for_job", "work_next_job",
    ],
    "tools_summary": [
        "ask_codebase", "blast_radius", "churn_analysis", "cross_layer_impact",
        "defect_correlation", "defect_learn", "defect_search", "defect_stats",
        "defect_suggest_fix", "diff_to_symbol", "evolution_frequency",
        "find_uncovered_functions", "generate_summary", "get_clone_aware_impact",
        "get_coverage_for_symbol", "get_ownership_map", "get_summary",
        "get_token_savings_report", "get_vulnerability_blast_radius",
        "guardrail_add_rule", "guardrail_check_edit", "guardrail_list_rules",
        "guardrail_scan", "hotspot_evolution", "import_coverage", "project_brief",
        "record_token_savings", "repo_map", "review_readiness",
        "test_impact_selection", "who_to_ask",
    ],
    "tools_security": [
        "compare_snapshots", "cross_repo_impact", "cross_repo_summary",
        "detect_cross_repo_deps", "diff_branches", "diff_callees", "diff_callers",
        "extract_rule_candidates_from_quality_findings", "find_shared_symbols",
        "get_applicable_rules", "get_edit_history", "get_edit_stats",
        "list_branches", "lsp_check_available", "lsp_completion", "lsp_definition",
        "lsp_diagnostics", "lsp_hover", "lsp_references", "merge_preview",
        "propose_edit", "propose_range_patch", "propose_symbol_id_patch",
        "propose_symbol_patch", "register_branch", "resolve_gate_findings",
        "revert_edit", "rule_candidate_accept", "rule_candidate_create",
        "rule_candidate_list", "rule_candidate_reject", "rule_insert_agents_md_block",
        "rule_list", "rule_sync_agents_md", "run_check_gate", "switch_branch",
    ],
    "tools_rules": [
        "count_resolved_edges", "get_active_build_context", "get_build_context",
        "get_metrics", "get_resolved_edges", "get_toolchain",
        "get_workspace_toolchains", "list_build_contexts", "list_toolchains",
    ],
    "tools_collab": [
        "append_evidence", "find_evidence", "get_freshness_status",
        "get_gate_decision", "get_role_view", "submit_verdict",
        "task_remediation_create", "task_step_resolve",
    ],
    "tools_p2_graph": [
        "build_hard_dependency_edges", "detect_cycle", "get_artifact_freshness",
        "get_dependency_edges", "get_interface_providers",
        "import_envelope_dependencies", "publish_interface",
        "record_artifact_identity", "select_interface_provider",
        "validate_revision_dependencies",
    ],
    "tools_p3_identity": [
        "check_action_identity", "check_session_separation", "get_action_identity",
        "get_attestation_validity", "list_attestation_revocations",
        "record_action_identity", "register_attestation_revocation",
    ],
    "tools_p4_lease": [
        "assignment_create", "assignment_revoke", "assignment_show",
        "lease_acquire", "lease_list_events", "lease_release", "lease_renew",
        "lease_status",
    ],
}

# ---------------------------------------------------------------------------
# 2. 路由元数据（rpc_method / target_backend / op_class / batch / status）
# ---------------------------------------------------------------------------
# 约定：
#   backend ∈ {rust_native, task_rpc, python_compat, declared_unavailable}
#   op_class ∈ {READ_ONLY, PROTECTED_MUTATION, GOVERNANCE_WRITE}
#   batch    ∈ {T02-fs, T02-metrics, T02-admin, T02-edit, T02-job,
#               P0-compat, existing-native, existing-task}
#   status   ∈ {migrated, transition, stable}
# 未列出的工具：target_backend=python_compat（P0 过渡，白名单只减不加），
# rpc_method=工具名（compat 路由按工具名直达 worker）。
ROUTE_OVERRIDES: Dict[str, Dict[str, str]] = {
    # ============ tools_query（32） ============
    "detect_cycles": {"rpc_method": "query.detect_cycles", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "export_module_graph": {"rpc_method": "export_module_graph", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "find_issues": {"rpc_method": "find_issues", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_call_chain_down": {"rpc_method": "query.call_chain_down", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_call_heatmap": {"rpc_method": "get_call_heatmap", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_callees": {"rpc_method": "query.callees", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_callers": {"rpc_method": "query.callers", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_comment_coverage": {"rpc_method": "get_comment_coverage", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_comment_from_version": {"rpc_method": "get_comment_from_version", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_deepest_functions": {"rpc_method": "get_deepest_functions", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_file_history": {"rpc_method": "query.file_history", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_file_symbols": {"rpc_method": "query.file", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_impact": {"rpc_method": "get_impact", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_issue_summary": {"rpc_method": "get_issue_summary", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_module_call_stats": {"rpc_method": "query.module_call_stats", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_orphan_symbols": {"rpc_method": "get_orphan_symbols", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_recent_changes": {"rpc_method": "get_recent_changes", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_semgrep_findings": {"rpc_method": "query.semgrep_findings", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_semgrep_stats": {"rpc_method": "query.semgrep_stats", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_stats": {"rpc_method": "query.stats", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_symbol": {"rpc_method": "query.symbol", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_symbol_history": {"rpc_method": "get_symbol_history", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_symbol_location": {"rpc_method": "query.symbol_location", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_test_coverage": {"rpc_method": "get_test_coverage", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_top_callers": {"rpc_method": "get_top_callers", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_topological_order": {"rpc_method": "query.topological_order", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_uncommented_symbols": {"rpc_method": "query.uncommented_symbols", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "restore_all_comments": {"rpc_method": "edit.restore_all_comments", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "restore_comment": {"rpc_method": "edit.restore_comment", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "run_semgrep_scan": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "semgrep_scan"},
    "scan_semgrep_incremental": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "semgrep_incremental"},
    "search_symbols": {"rpc_method": "query.search", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},

    # ============ tools_workspace（27） ============
    "build_directory": {"rpc_method": "workspace.build_directory", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-fs", "status": "migrated"},
    "build_graph": {"rpc_method": "workspace.build_graph", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-fs", "status": "migrated"},
    "check_file_health": {"rpc_method": "workspace.file.health", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-fs", "status": "migrated"},
    "delete_workspace": {"rpc_method": "workspace.remove", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "existing-native", "status": "stable"},
    "file_grep": {"rpc_method": "workspace.file.grep", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-fs", "status": "migrated"},
    "file_list": {"rpc_method": "workspace.file.list", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-fs", "status": "migrated"},
    "file_read": {"rpc_method": "workspace.file.read", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-fs", "status": "migrated"},
    "file_symbol_content": {"rpc_method": "workspace.file.symbol_content", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-fs", "status": "migrated"},
    "get_active_workspace": {"rpc_method": "workspace.status", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_code_health_check": {"rpc_method": "query.code_health", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-metrics", "status": "migrated"},
    "get_code_metrics_summary": {"rpc_method": "query.metrics_summary", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-metrics", "status": "migrated"},
    "get_commit_changes": {"rpc_method": "query.git_commit_changes", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_complexity_hotspots": {"rpc_method": "query.complexity_hotspots", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-metrics", "status": "migrated"},
    "get_coupling_analysis": {"rpc_method": "query.coupling_analysis", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-metrics", "status": "migrated"},
    "get_function_metrics": {"rpc_method": "query.function_metrics", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-metrics", "status": "migrated"},
    "get_git_commits": {"rpc_method": "query.git_commits", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_git_stats": {"rpc_method": "query.git_stats", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_largest_functions": {"rpc_method": "query.largest_functions", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-metrics", "status": "migrated"},
    "get_most_coupled_functions": {"rpc_method": "query.most_coupled_functions", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-metrics", "status": "migrated"},
    "get_status": {"rpc_method": "query.status", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-metrics", "status": "migrated"},
    "get_symbol_content_by_hash": {"rpc_method": "query.symbol_content_by_hash", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-metrics", "status": "migrated"},
    "import_git_history": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "git_history"},
    "list_workspaces": {"rpc_method": "workspace.list", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "refresh_file": {"rpc_method": "workspace.file.refresh", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-fs", "status": "migrated"},
    "register_workspace": {"rpc_method": "workspace.register", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "existing-native", "status": "stable"},
    "remove_file": {"rpc_method": "workspace.file.remove", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-fs", "status": "migrated"},
    "set_active_workspace": {"rpc_method": "workspace.activate", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "existing-native", "status": "stable"},

    # ============ tools_semantic（19） ============
    "embed_single_symbol": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "embed_single"},
    "embed_symbols": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "embed"},
    "find_similar_functions": {"rpc_method": "find_similar_functions", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "gc_archive_import": {"rpc_method": "admin.gc_archive_import", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-admin", "status": "migrated"},
    "gc_archive_inspect": {"rpc_method": "admin.gc_archive_inspect", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-admin", "status": "migrated"},
    "gc_archive_list": {"rpc_method": "admin.gc_archive_list", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-admin", "status": "migrated"},
    "gc_audit_get": {"rpc_method": "admin.gc_audit_get", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-admin", "status": "migrated"},
    "gc_audit_list": {"rpc_method": "admin.gc_audit_list", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-admin", "status": "migrated"},
    "gc_policy_get": {"rpc_method": "admin.gc_policy_get", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-admin", "status": "migrated"},
    "gc_policy_set": {"rpc_method": "admin.gc_policy_set", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-admin", "status": "migrated"},
    "gc_retention": {"rpc_method": "admin.gc_retention", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-admin", "status": "migrated"},
    "get_project_dependencies": {"rpc_method": "get_project_dependencies", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_symbol_commit_history": {"rpc_method": "get_symbol_commit_history", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "import_codeowners": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "codeowners"},
    "import_git_blame": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "git_blame"},
    "import_project_dependencies": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "project_deps"},
    "parse_codeowners": {"rpc_method": "parse_codeowners", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "prune_external_symbols": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "prune_external"},
    "semantic_search": {"rpc_method": "semantic_search", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},

    # ============ tools_task（52） ============
    "audit_verify_chain": {"rpc_method": "audit_verify_chain", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "bootstrap_status": {"rpc_method": "bootstrap_status", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "cancel_job": {"rpc_method": "task.job_cancel", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated"},
    "cleanup_agent_rule_sync_log": {"rpc_method": "admin.cleanup_rule_sync_log", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-admin", "status": "migrated"},
    "clear_clones": {"rpc_method": "admin.clear_clones", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-admin", "status": "migrated"},
    "detect_clones": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "clone_detect"},
    "detect_clones_async": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "clone_detect"},
    "embed_symbols_async": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "embed"},
    "get_clone_group_detail": {"rpc_method": "get_clone_group_detail", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_clone_group_stats": {"rpc_method": "task.clone_group_stats", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_clone_stats": {"rpc_method": "task.clone_stats", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_commit_tasks": {"rpc_method": "query.commit_tasks", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_defect_correlation": {"rpc_method": "query.get_defect_correlation", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_job_stats": {"rpc_method": "task.job_stats", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_job_status": {"rpc_method": "task.job_status", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_symbol_change_tasks": {"rpc_method": "get_symbol_change_tasks", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_symbol_issues": {"rpc_method": "query.issues", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_task_commits": {"rpc_method": "task.get_commits", "backend": "task_rpc", "op": "READ_ONLY", "batch": "existing-task", "status": "stable"},
    "get_task_symbol_changes": {"rpc_method": "task.get_symbol_changes", "backend": "task_rpc", "op": "READ_ONLY", "batch": "existing-task", "status": "stable"},
    "get_test_cases": {"rpc_method": "query.tests", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_test_coverage_summary": {"rpc_method": "query.tests", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_test_stability": {"rpc_method": "query.tests", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_tested_functions": {"rpc_method": "query.tests", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "link_edit_audit_symbols": {"rpc_method": "task.link_edit_audit_symbols", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "existing-task", "status": "stable"},
    "list_audit_signing_keys": {"rpc_method": "list_audit_signing_keys", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "list_clone_groups": {"rpc_method": "list_clone_groups", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "list_clones": {"rpc_method": "list_clones", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "list_jobs": {"rpc_method": "task.list_jobs", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "record_task_symbol_change": {"rpc_method": "task.record_symbol_change", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "existing-task", "status": "stable"},
    "rotate_audit_signing_key": {"rpc_method": "admin.audit_rotate_key", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-admin", "status": "migrated"},
    "rule_seed_bootstrap": {"rpc_method": "rule.seed_bootstrap", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "semgrep_scan_async": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "semgrep_scan"},
    "task_apply": {"rpc_method": "task.apply", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "existing-task", "status": "stable"},
    "task_capture_diff": {"rpc_method": "task.capture_diff", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "existing-task", "status": "stable"},
    "task_close": {"rpc_method": "task.close", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "existing-task", "status": "stable"},
    "task_completion_review": {"rpc_method": "task.completion_review", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "existing-task", "status": "stable"},
    "task_create": {"rpc_method": "task.create", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "existing-task", "status": "stable"},
    "task_create_from_plan": {"rpc_method": "task.create_from_plan", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "existing-task", "status": "stable"},
    "task_create_subtask": {"rpc_method": "task.create_subtask", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "existing-task", "status": "stable"},
    "task_list": {"rpc_method": "task.list", "backend": "task_rpc", "op": "READ_ONLY", "batch": "existing-task", "status": "stable"},
    "task_next_step": {"rpc_method": "task.claim", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "existing-task", "status": "stable"},
    "task_plan_template": {"rpc_method": "task_plan_template", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "task_quality_findings": {"rpc_method": "task.quality_findings", "backend": "task_rpc", "op": "READ_ONLY", "batch": "existing-task", "status": "stable"},
    "task_report_step": {"rpc_method": "task.report", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "existing-task", "status": "stable"},
    "task_resolve_block": {"rpc_method": "task.reopen", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "existing-task", "status": "stable"},
    "task_resolve_quality_finding": {"rpc_method": "task.resolve_quality_finding", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "existing-task", "status": "stable"},
    "task_rollback": {"rpc_method": "task.rollback", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "existing-task", "status": "stable"},
    "task_split": {"rpc_method": "task.split", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "existing-task", "status": "stable"},
    "task_status": {"rpc_method": "task.status", "backend": "task_rpc", "op": "READ_ONLY", "batch": "existing-task", "status": "stable"},
    "task_status_tree": {"rpc_method": "task.status_tree", "backend": "task_rpc", "op": "READ_ONLY", "batch": "existing-task", "status": "stable"},
    "wait_for_job": {"rpc_method": "task.wait_for_job", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "work_next_job": {"rpc_method": "task.work_next", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "existing-task", "status": "stable"},

    # ============ tools_summary（31） ============
    "ask_codebase": {"rpc_method": "ask_codebase", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "blast_radius": {"rpc_method": "blast_radius", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "churn_analysis": {"rpc_method": "query.churn_analysis", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "cross_layer_impact": {"rpc_method": "cross_layer_impact", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "defect_correlation": {"rpc_method": "query.defect_correlation", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "defect_learn": {"rpc_method": "defect_learn", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "defect_search": {"rpc_method": "query.defect_search", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "defect_stats": {"rpc_method": "defect.stats", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "defect_suggest_fix": {"rpc_method": "query.defect_suggest_fix", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "diff_to_symbol": {"rpc_method": "query.diff_to_symbol", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "evolution_frequency": {"rpc_method": "evolution_frequency", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "find_uncovered_functions": {"rpc_method": "find_uncovered_functions", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "generate_summary": {"rpc_method": "summary.generate", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "get_clone_aware_impact": {"rpc_method": "get_clone_aware_impact", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_coverage_for_symbol": {"rpc_method": "query.coverage_for_symbol", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_ownership_map": {"rpc_method": "get_ownership_map", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_summary": {"rpc_method": "get_summary", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_token_savings_report": {"rpc_method": "get_token_savings_report", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_vulnerability_blast_radius": {"rpc_method": "get_vulnerability_blast_radius", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "guardrail_add_rule": {"rpc_method": "guardrail.add_rule", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "guardrail_check_edit": {"rpc_method": "guardrail_check_edit", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "guardrail_list_rules": {"rpc_method": "guardrail_list_rules", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "guardrail_scan": {"rpc_method": "guardrail_scan", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "hotspot_evolution": {"rpc_method": "hotspot_evolution", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "import_coverage": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "coverage"},
    "project_brief": {"rpc_method": "project_brief", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "record_token_savings": {"rpc_method": "edit.record_token_savings", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "repo_map": {"rpc_method": "repo_map", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "review_readiness": {"rpc_method": "review_readiness", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "test_impact_selection": {"rpc_method": "test_impact_selection", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "who_to_ask": {"rpc_method": "who_to_ask", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},

    # ============ tools_security（36） ============
    "compare_snapshots": {"rpc_method": "admin.snapshot_compare", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-admin", "status": "migrated"},
    "cross_repo_impact": {"rpc_method": "cross_repo_impact", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "cross_repo_summary": {"rpc_method": "cross_repo_summary", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "detect_cross_repo_deps": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "cross_repo_deps"},
    "diff_branches": {"rpc_method": "query.diff_branches", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "diff_callees": {"rpc_method": "query.diff_callees", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-edit", "status": "migrated"},
    "diff_callers": {"rpc_method": "query.diff_callers", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-edit", "status": "migrated"},
    "extract_rule_candidates_from_quality_findings": {"rpc_method": "rule.extract_candidates", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "find_shared_symbols": {"rpc_method": "find_shared_symbols", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_applicable_rules": {"rpc_method": "get_applicable_rules", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_edit_history": {"rpc_method": "get_edit_history", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_edit_stats": {"rpc_method": "edit.stats", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "list_branches": {"rpc_method": "list_branches", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "lsp_check_available": {"rpc_method": "lsp_check_available", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "lsp_completion": {"rpc_method": "lsp_completion", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "lsp_definition": {"rpc_method": "lsp_definition", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "lsp_diagnostics": {"rpc_method": "lsp_diagnostics", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "lsp_hover": {"rpc_method": "lsp_hover", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "lsp_references": {"rpc_method": "lsp_references", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "merge_preview": {"rpc_method": "merge_preview", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "propose_edit": {"rpc_method": "edit.propose", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "propose_range_patch": {"rpc_method": "edit.propose_range_patch", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "propose_symbol_id_patch": {"rpc_method": "edit.propose_symbol_id_patch", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "propose_symbol_patch": {"rpc_method": "edit.propose_symbol_patch", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "register_branch": {"rpc_method": "admin.branch_register", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-admin", "status": "migrated"},
    "resolve_gate_findings": {"rpc_method": "gate.resolve_findings", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "revert_edit": {"rpc_method": "edit.revert", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "rule_candidate_accept": {"rpc_method": "rule.candidate_accept", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "rule_candidate_create": {"rpc_method": "rule.candidate_create", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "rule_candidate_list": {"rpc_method": "rule_candidate_list", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "rule_candidate_reject": {"rpc_method": "rule.candidate_reject", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "rule_insert_agents_md_block": {"rpc_method": "rule.insert_agents_md_block", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "rule_list": {"rpc_method": "rule_list", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "rule_sync_agents_md": {"rpc_method": "rule.sync_agents_md", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "run_check_gate": {"rpc_method": "gate.run_check", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-edit", "status": "migrated"},
    "switch_branch": {"rpc_method": "admin.branch_switch", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-admin", "status": "migrated"},

    # ============ tools_rules（9） ============
    "count_resolved_edges": {"rpc_method": "build_context.count_resolved_edges", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_active_build_context": {"rpc_method": "build_context.active", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_build_context": {"rpc_method": "build_context.get", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_metrics": {"rpc_method": "admin.metrics_get", "backend": "rust_native", "op": "READ_ONLY", "batch": "T02-admin", "status": "migrated"},
    "get_resolved_edges": {"rpc_method": "build_context.resolved_edges", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "get_toolchain": {"rpc_method": "get_toolchain", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_workspace_toolchains": {"rpc_method": "get_workspace_toolchains", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "list_build_contexts": {"rpc_method": "build_context.list", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "list_toolchains": {"rpc_method": "list_toolchains", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},

    # ============ tools_collab（8） ============
    "append_evidence": {"rpc_method": "evidence.append", "backend": "rust_native", "op": "GOVERNANCE_WRITE", "batch": "existing-native", "status": "stable"},
    "find_evidence": {"rpc_method": "find_evidence", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_freshness_status": {"rpc_method": "get_freshness_status", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_gate_decision": {"rpc_method": "get_gate_decision", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_role_view": {"rpc_method": "get_role_view", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "submit_verdict": {"rpc_method": "verdict.submit", "backend": "rust_native", "op": "GOVERNANCE_WRITE", "batch": "existing-native", "status": "stable"},
    "task_remediation_create": {"rpc_method": "task.remediation.create", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "existing-native", "status": "stable"},
    "task_step_resolve": {"rpc_method": "task.step.resolve", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "existing-native", "status": "stable"},

    # ============ tools_p2_graph（10） ============
    "build_hard_dependency_edges": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "hard_dep_edges"},
    "detect_cycle": {"rpc_method": "detect_cycle", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_artifact_freshness": {"rpc_method": "get_artifact_freshness", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_dependency_edges": {"rpc_method": "get_dependency_edges", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_interface_providers": {"rpc_method": "get_interface_providers", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "import_envelope_dependencies": {"rpc_method": "task.job_submit", "backend": "task_rpc", "op": "PROTECTED_MUTATION", "batch": "T02-job", "status": "migrated", "job_type": "envelope_deps"},
    "publish_interface": {"rpc_method": "admin.publish_interface", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-admin", "status": "migrated"},
    "record_artifact_identity": {"rpc_method": "admin.record_artifact_identity", "backend": "rust_native", "op": "GOVERNANCE_WRITE", "batch": "T02-admin", "status": "migrated"},
    "select_interface_provider": {"rpc_method": "admin.select_interface_provider", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-admin", "status": "migrated"},
    "validate_revision_dependencies": {"rpc_method": "validate_revision_dependencies", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},

    # ============ tools_p3_identity（7） ============
    "check_action_identity": {"rpc_method": "check_action_identity", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "check_session_separation": {"rpc_method": "check_session_separation", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_action_identity": {"rpc_method": "get_action_identity", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "get_attestation_validity": {"rpc_method": "get_attestation_validity", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "list_attestation_revocations": {"rpc_method": "list_attestation_revocations", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "record_action_identity": {"rpc_method": "admin.record_action_identity", "backend": "rust_native", "op": "GOVERNANCE_WRITE", "batch": "T02-admin", "status": "migrated"},
    "register_attestation_revocation": {"rpc_method": "admin.register_attestation_revocation", "backend": "rust_native", "op": "GOVERNANCE_WRITE", "batch": "T02-admin", "status": "migrated"},

    # ============ tools_p4_lease（8） ============
    "assignment_create": {"rpc_method": "admin.assignment_create", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-admin", "status": "migrated"},
    "assignment_revoke": {"rpc_method": "admin.assignment_revoke", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "T02-admin", "status": "migrated"},
    "assignment_show": {"rpc_method": "assignment_show", "backend": "python_compat", "op": "READ_ONLY", "batch": "P0-compat", "status": "transition"},
    "lease_acquire": {"rpc_method": "lease.acquire", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "existing-native", "status": "stable"},
    "lease_list_events": {"rpc_method": "lease.list_events", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
    "lease_release": {"rpc_method": "lease.release", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "existing-native", "status": "stable"},
    "lease_renew": {"rpc_method": "lease.renew", "backend": "rust_native", "op": "PROTECTED_MUTATION", "batch": "existing-native", "status": "stable"},
    "lease_status": {"rpc_method": "lease.status", "backend": "rust_native", "op": "READ_ONLY", "batch": "existing-native", "status": "stable"},
}

# ---------------------------------------------------------------------------
# 3. 矩阵构建
# ---------------------------------------------------------------------------
BACKENDS = ("rust_native", "task_rpc", "python_compat", "declared_unavailable")
OP_CLASSES = ("READ_ONLY", "PROTECTED_MUTATION", "GOVERNANCE_WRITE")


def build_matrix() -> Dict[str, Any]:
    """构建 239 工具矩阵（工具名集合以实际提取为准，路由元数据以 ROUTE_OVERRIDES 为准）。"""
    tools: List[Dict[str, Any]] = []
    seen: Dict[str, str] = {}
    for module in TOOL_MODULES:
        for name in extract_tool_names(module):
            if name in seen:
                raise SystemExit(f"工具重复注册: {name}（{seen[name]} 与 {module}）")
            seen[name] = module
            override = ROUTE_OVERRIDES.get(name, {})
            rpc_method = override.get("rpc_method", name)
            backend = override.get("backend", "python_compat")
            op_class = override.get("op", "READ_ONLY")
            batch = override.get("batch", "P0-compat")
            status = override.get("status", "transition")
            if backend not in BACKENDS:
                raise SystemExit(f"{name}: 非法 backend {backend!r}")
            if op_class not in OP_CLASSES:
                raise SystemExit(f"{name}: 非法 op_class {op_class!r}")
            entry: Dict[str, Any] = {
                "name": name,
                "module": module,
                "current_backend": "unknown",
                "target_backend": backend,
                "rpc_method": rpc_method,
                "op_class": op_class,
                "batch": batch,
                "status": status,
            }
            if "job_type" in override:
                entry["job_type"] = override["job_type"]
            tools.append(entry)
    tools.sort(key=lambda t: (t["module"], t["name"]))
    return {
        "schema_version": "1.0",
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "total_tools": len(tools),
        "backends": list(BACKENDS),
        "op_classes": list(OP_CLASSES),
        "tools": tools,
    }


def load_matrix() -> Dict[str, Any]:
    """读取磁盘上的矩阵 JSON（单一真相源）。"""
    with open(_MATRIX_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_matrix(matrix: Dict[str, Any]) -> None:
    """写回矩阵 JSON（保持字段顺序与缩进稳定）。"""
    os.makedirs(os.path.dirname(_MATRIX_PATH), exist_ok=True)
    with open(_MATRIX_PATH, "w", encoding="utf-8") as fh:
        json.dump(matrix, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")


# ---------------------------------------------------------------------------
# 4. 派生输出
# ---------------------------------------------------------------------------

def emit_dispatch_manifest(matrix: Dict[str, Any]) -> str:
    """输出 dispatch.rs match 分支声明清单（method → handler 归属模块）。"""
    lines: List[str] = []
    for t in matrix["tools"]:
        backend = t["target_backend"]
        if backend in ("rust_native", "task_rpc"):
            lines.append(f'{t["rpc_method"]:48s} # {t["module"]}.{t["name"]} [{backend}]')
    return "\n".join(lines)


def emit_registry_rows(matrix: Dict[str, Any]) -> str:
    """输出 capability registry add() 行清单。"""
    lines: List[str] = []
    for t in matrix["tools"]:
        backend = t["target_backend"]
        if backend == "declared_unavailable":
            continue
        scope = "workspace" if t["op_class"] != "READ_ONLY" else "snapshot"
        op = t["op_class"].lower()
        lines.append(
            f'add("{t["rpc_method"]}", "{t["name"]}", "{t["name"]}", '
            f'"{backend}", "available", "{op}", "{scope}", false, "/v1/rpc", '
            f'"fixture-{t["name"]}-ok", "fixture-{t["name"]}-err", "{t["batch"]}", "")'
        )
    return "\n".join(lines)


def emit_compat_whitelist(matrix: Dict[str, Any]) -> str:
    """输出 COMPAT_ROUTE_WHITELIST 条目清单（只含 target_backend=python_compat）。"""
    lines: List[str] = []
    for t in matrix["tools"]:
        if t["target_backend"] == "python_compat":
            lines.append(f'("{t["name"]}", "read_only"),')
    return "\n".join(lines)


def emit_shell_skeleton(matrix: Dict[str, Any]) -> str:
    """输出 Python 薄壳工具函数骨架（name → rpc_method）。"""
    lines: List[str] = []
    for t in matrix["tools"]:
        lines.append(f'# {t["module"]}.{t["name"]} -> {t["rpc_method"]} [{t["target_backend"]}]')
    return "\n".join(lines)


def emit_report(matrix: Dict[str, Any]) -> str:
    """输出迁移核对报告。"""
    by_backend: Dict[str, int] = {}
    by_batch: Dict[str, int] = {}
    by_op: Dict[str, int] = {}
    for t in matrix["tools"]:
        by_backend[t["target_backend"]] = by_backend.get(t["target_backend"], 0) + 1
        by_batch[t["batch"]] = by_batch.get(t["batch"], 0) + 1
        by_op[t["op_class"]] = by_op.get(t["op_class"], 0) + 1
    lines = [
        f"工具总数: {matrix['total_tools']}",
        "按 target_backend:",
    ]
    for backend in BACKENDS:
        lines.append(f"  {backend}: {by_backend.get(backend, 0)}")
    lines.append("按 op_class:")
    for op in OP_CLASSES:
        lines.append(f"  {op}: {by_op.get(op, 0)}")
    lines.append("按 batch:")
    for batch, count in sorted(by_batch.items()):
        lines.append(f"  {batch}: {count}")
    return "\n".join(lines)


def main() -> int:
    args = sys.argv[1:]
    if "--emit-json" in args:
        matrix = build_matrix()
        save_matrix(matrix)
        print(f"已写回矩阵: {_MATRIX_PATH}（{matrix['total_tools']} 工具）")
        return 0
    matrix = load_matrix()
    if "--emit-dispatch" in args:
        print(emit_dispatch_manifest(matrix))
        return 0
    if "--emit-registry" in args:
        print(emit_registry_rows(matrix))
        return 0
    if "--emit-whitelist" in args:
        print(emit_compat_whitelist(matrix))
        return 0
    if "--emit-shell" in args:
        print(emit_shell_skeleton(matrix))
        return 0
    if "--report" in args:
        print(emit_report(matrix))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
