//! 路由矩阵 Rust 侧数据结构（T01 单一真相源镜像）。
//!
//! 数据来源：`deliverables/software-company/tool_migration_matrix.json`
//! （由 `scripts/gen_route_matrix.py --emit-json` 生成）。本文件是矩阵的
//! **编译期+运行时双校验**镜像：
//! - 编译期：条目总数、backend/op_class 合法值由 const 断言保证；
//! - 运行时：`ToolRegistry::lookup` / `validate_coverage` / `list_by_backend`
//!   供 dispatch fallback 与 `/v1/meta/tools` 自描述接口使用。
//!
//! 同步纪律：修改矩阵 JSON 后必须重新生成本文件（gen_route_matrix.py），
//! 禁止手改本文件（避免双实现漂移）。

use serde_json::Value;

/// 路由 backend 枚举（与矩阵 backends 字段一致）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Backend {
    /// Rust daemon 原生 handler（dispatch match 分支）
    RustNative,
    /// 异步长任务 job 状态机（task.job_submit / task.wait_for_job）
    TaskRpc,
    /// Python H3 compat worker 过渡（白名单只减不加，M2 deadline 清空）
    PythonCompat,
    /// 显式废弃（结构化 E_TOOL_DEPRECATED，仍占路由数）
    DeclaredUnavailable,
}

impl Backend {
    /// 返回矩阵 JSON 中的字符串表示。
    pub fn as_str(&self) -> &'static str {
        match self {
            Backend::RustNative => "rust_native",
            Backend::TaskRpc => "task_rpc",
            Backend::PythonCompat => "python_compat",
            Backend::DeclaredUnavailable => "declared_unavailable",
        }
    }
}

/// 操作分类（与矩阵 op_class 字段一致）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OpClass {
    /// 只读（不经串行化点）
    ReadOnly,
    /// 受保护写（Protected_Mutation，经 SerializationPoint 串行化）
    ProtectedMutation,
    /// 治理写（governance write，额外审计/独立性校验）
    GovernanceWrite,
}

impl OpClass {
    /// 返回矩阵 JSON 中的字符串表示。
    pub fn as_str(&self) -> &'static str {
        match self {
            OpClass::ReadOnly => "READ_ONLY",
            OpClass::ProtectedMutation => "PROTECTED_MUTATION",
            OpClass::GovernanceWrite => "GOVERNANCE_WRITE",
        }
    }

    /// 是否为写操作（Protected_Mutation / Governance_WRITE）。
    pub fn is_write(&self) -> bool {
        !matches!(self, OpClass::ReadOnly)
    }
}

/// 单工具路由条目（ToolRoute）。
#[derive(Debug, Clone, Copy)]
pub struct ToolRoute {
    /// MCP 工具名（对外契约，不变）
    pub name: &'static str,
    /// 所属 Python 工具模块（tools_workspace 等）
    pub module: &'static str,
    /// 收敛后目标 backend
    pub target_backend: Backend,
    /// daemon RPC method（python_compat 时为工具名）
    pub rpc_method: &'static str,
    /// 操作分类
    pub op_class: OpClass,
    /// 迁移批次（T02-fs / T02-metrics / T02-admin / T02-edit / T02-job / P0-compat / existing-*）
    pub batch: &'static str,
    /// 状态（migrated / transition / stable）
    pub status: &'static str,
}

/// 全部 239 工具路由条目（由矩阵 JSON 生成，勿手改）。
pub const TOOL_ROUTES: &[ToolRoute] = &[
    ToolRoute { name: "append_evidence", module: "tools_collab", target_backend: Backend::RustNative, rpc_method: "evidence.append", op_class: OpClass::GovernanceWrite, batch: "existing-native", status: "stable" },
    ToolRoute { name: "find_evidence", module: "tools_collab", target_backend: Backend::PythonCompat, rpc_method: "find_evidence", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_freshness_status", module: "tools_collab", target_backend: Backend::PythonCompat, rpc_method: "get_freshness_status", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_gate_decision", module: "tools_collab", target_backend: Backend::PythonCompat, rpc_method: "get_gate_decision", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_role_view", module: "tools_collab", target_backend: Backend::PythonCompat, rpc_method: "get_role_view", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "submit_verdict", module: "tools_collab", target_backend: Backend::RustNative, rpc_method: "verdict.submit", op_class: OpClass::GovernanceWrite, batch: "existing-native", status: "stable" },
    ToolRoute { name: "task_remediation_create", module: "tools_collab", target_backend: Backend::RustNative, rpc_method: "task.remediation.create", op_class: OpClass::ProtectedMutation, batch: "existing-native", status: "stable" },
    ToolRoute { name: "task_step_resolve", module: "tools_collab", target_backend: Backend::RustNative, rpc_method: "task.step.resolve", op_class: OpClass::ProtectedMutation, batch: "existing-native", status: "stable" },
    ToolRoute { name: "build_hard_dependency_edges", module: "tools_p2_graph", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "detect_cycle", module: "tools_p2_graph", target_backend: Backend::PythonCompat, rpc_method: "detect_cycle", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_artifact_freshness", module: "tools_p2_graph", target_backend: Backend::PythonCompat, rpc_method: "get_artifact_freshness", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_dependency_edges", module: "tools_p2_graph", target_backend: Backend::PythonCompat, rpc_method: "get_dependency_edges", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_interface_providers", module: "tools_p2_graph", target_backend: Backend::PythonCompat, rpc_method: "get_interface_providers", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "import_envelope_dependencies", module: "tools_p2_graph", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "publish_interface", module: "tools_p2_graph", target_backend: Backend::RustNative, rpc_method: "admin.publish_interface", op_class: OpClass::ProtectedMutation, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "record_artifact_identity", module: "tools_p2_graph", target_backend: Backend::RustNative, rpc_method: "admin.record_artifact_identity", op_class: OpClass::GovernanceWrite, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "select_interface_provider", module: "tools_p2_graph", target_backend: Backend::RustNative, rpc_method: "admin.select_interface_provider", op_class: OpClass::ProtectedMutation, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "validate_revision_dependencies", module: "tools_p2_graph", target_backend: Backend::PythonCompat, rpc_method: "validate_revision_dependencies", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "check_action_identity", module: "tools_p3_identity", target_backend: Backend::PythonCompat, rpc_method: "check_action_identity", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "check_session_separation", module: "tools_p3_identity", target_backend: Backend::PythonCompat, rpc_method: "check_session_separation", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_action_identity", module: "tools_p3_identity", target_backend: Backend::PythonCompat, rpc_method: "get_action_identity", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_attestation_validity", module: "tools_p3_identity", target_backend: Backend::PythonCompat, rpc_method: "get_attestation_validity", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "list_attestation_revocations", module: "tools_p3_identity", target_backend: Backend::PythonCompat, rpc_method: "list_attestation_revocations", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "record_action_identity", module: "tools_p3_identity", target_backend: Backend::RustNative, rpc_method: "admin.record_action_identity", op_class: OpClass::GovernanceWrite, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "register_attestation_revocation", module: "tools_p3_identity", target_backend: Backend::RustNative, rpc_method: "admin.register_attestation_revocation", op_class: OpClass::GovernanceWrite, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "assignment_create", module: "tools_p4_lease", target_backend: Backend::RustNative, rpc_method: "admin.assignment_create", op_class: OpClass::ProtectedMutation, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "assignment_revoke", module: "tools_p4_lease", target_backend: Backend::RustNative, rpc_method: "admin.assignment_revoke", op_class: OpClass::ProtectedMutation, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "assignment_show", module: "tools_p4_lease", target_backend: Backend::PythonCompat, rpc_method: "assignment_show", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "lease_acquire", module: "tools_p4_lease", target_backend: Backend::RustNative, rpc_method: "lease.acquire", op_class: OpClass::ProtectedMutation, batch: "existing-native", status: "stable" },
    ToolRoute { name: "lease_list_events", module: "tools_p4_lease", target_backend: Backend::RustNative, rpc_method: "lease.list_events", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "lease_release", module: "tools_p4_lease", target_backend: Backend::RustNative, rpc_method: "lease.release", op_class: OpClass::ProtectedMutation, batch: "existing-native", status: "stable" },
    ToolRoute { name: "lease_renew", module: "tools_p4_lease", target_backend: Backend::RustNative, rpc_method: "lease.renew", op_class: OpClass::ProtectedMutation, batch: "existing-native", status: "stable" },
    ToolRoute { name: "lease_status", module: "tools_p4_lease", target_backend: Backend::RustNative, rpc_method: "lease.status", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "detect_cycles", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "query.detect_cycles", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "export_module_graph", module: "tools_query", target_backend: Backend::PythonCompat, rpc_method: "export_module_graph", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "find_issues", module: "tools_query", target_backend: Backend::PythonCompat, rpc_method: "find_issues", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_call_chain_down", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "query.call_chain_down", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_call_heatmap", module: "tools_query", target_backend: Backend::PythonCompat, rpc_method: "get_call_heatmap", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_callees", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "query.callees", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_callers", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "query.callers", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_comment_coverage", module: "tools_query", target_backend: Backend::PythonCompat, rpc_method: "get_comment_coverage", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_comment_from_version", module: "tools_query", target_backend: Backend::PythonCompat, rpc_method: "get_comment_from_version", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_deepest_functions", module: "tools_query", target_backend: Backend::PythonCompat, rpc_method: "get_deepest_functions", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_file_history", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "query.file_history", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_file_symbols", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "query.file", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_impact", module: "tools_query", target_backend: Backend::PythonCompat, rpc_method: "get_impact", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_issue_summary", module: "tools_query", target_backend: Backend::PythonCompat, rpc_method: "get_issue_summary", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_module_call_stats", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "query.module_call_stats", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_orphan_symbols", module: "tools_query", target_backend: Backend::PythonCompat, rpc_method: "get_orphan_symbols", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_recent_changes", module: "tools_query", target_backend: Backend::PythonCompat, rpc_method: "get_recent_changes", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_semgrep_findings", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "query.semgrep_findings", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_semgrep_stats", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "query.semgrep_stats", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_stats", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "query.stats", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_symbol", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "query.symbol", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_symbol_history", module: "tools_query", target_backend: Backend::PythonCompat, rpc_method: "get_symbol_history", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_symbol_location", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "query.symbol_location", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_test_coverage", module: "tools_query", target_backend: Backend::PythonCompat, rpc_method: "get_test_coverage", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_top_callers", module: "tools_query", target_backend: Backend::PythonCompat, rpc_method: "get_top_callers", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_topological_order", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "query.topological_order", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_uncommented_symbols", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "query.uncommented_symbols", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "restore_all_comments", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "edit.restore_all_comments", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "restore_comment", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "edit.restore_comment", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "run_semgrep_scan", module: "tools_query", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "scan_semgrep_incremental", module: "tools_query", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "search_symbols", module: "tools_query", target_backend: Backend::RustNative, rpc_method: "query.search", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "count_resolved_edges", module: "tools_rules", target_backend: Backend::RustNative, rpc_method: "build_context.count_resolved_edges", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_active_build_context", module: "tools_rules", target_backend: Backend::RustNative, rpc_method: "build_context.active", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_build_context", module: "tools_rules", target_backend: Backend::RustNative, rpc_method: "build_context.get", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_metrics", module: "tools_rules", target_backend: Backend::RustNative, rpc_method: "admin.metrics_get", op_class: OpClass::ReadOnly, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "get_resolved_edges", module: "tools_rules", target_backend: Backend::RustNative, rpc_method: "build_context.resolved_edges", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_toolchain", module: "tools_rules", target_backend: Backend::PythonCompat, rpc_method: "get_toolchain", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_workspace_toolchains", module: "tools_rules", target_backend: Backend::PythonCompat, rpc_method: "get_workspace_toolchains", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "list_build_contexts", module: "tools_rules", target_backend: Backend::RustNative, rpc_method: "build_context.list", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "list_toolchains", module: "tools_rules", target_backend: Backend::PythonCompat, rpc_method: "list_toolchains", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "compare_snapshots", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "admin.snapshot_compare", op_class: OpClass::ReadOnly, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "cross_repo_impact", module: "tools_security", target_backend: Backend::PythonCompat, rpc_method: "cross_repo_impact", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "cross_repo_summary", module: "tools_security", target_backend: Backend::PythonCompat, rpc_method: "cross_repo_summary", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "detect_cross_repo_deps", module: "tools_security", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "diff_branches", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "query.diff_branches", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "diff_callees", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "query.diff_callees", op_class: OpClass::ReadOnly, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "diff_callers", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "query.diff_callers", op_class: OpClass::ReadOnly, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "extract_rule_candidates_from_quality_findings", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "rule.extract_candidates", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "find_shared_symbols", module: "tools_security", target_backend: Backend::PythonCompat, rpc_method: "find_shared_symbols", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_applicable_rules", module: "tools_security", target_backend: Backend::PythonCompat, rpc_method: "get_applicable_rules", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_edit_history", module: "tools_security", target_backend: Backend::PythonCompat, rpc_method: "get_edit_history", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_edit_stats", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "edit.stats", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "list_branches", module: "tools_security", target_backend: Backend::PythonCompat, rpc_method: "list_branches", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "lsp_check_available", module: "tools_security", target_backend: Backend::PythonCompat, rpc_method: "lsp_check_available", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "lsp_completion", module: "tools_security", target_backend: Backend::PythonCompat, rpc_method: "lsp_completion", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "lsp_definition", module: "tools_security", target_backend: Backend::PythonCompat, rpc_method: "lsp_definition", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "lsp_diagnostics", module: "tools_security", target_backend: Backend::PythonCompat, rpc_method: "lsp_diagnostics", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "lsp_hover", module: "tools_security", target_backend: Backend::PythonCompat, rpc_method: "lsp_hover", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "lsp_references", module: "tools_security", target_backend: Backend::PythonCompat, rpc_method: "lsp_references", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "merge_preview", module: "tools_security", target_backend: Backend::PythonCompat, rpc_method: "merge_preview", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "propose_edit", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "edit.propose", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "propose_range_patch", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "edit.propose_range_patch", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "propose_symbol_id_patch", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "edit.propose_symbol_id_patch", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "propose_symbol_patch", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "edit.propose_symbol_patch", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "register_branch", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "admin.branch_register", op_class: OpClass::ProtectedMutation, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "resolve_gate_findings", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "gate.resolve_findings", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "revert_edit", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "edit.revert", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "rule_candidate_accept", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "rule.candidate_accept", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "rule_candidate_create", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "rule.candidate_create", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "rule_candidate_list", module: "tools_security", target_backend: Backend::PythonCompat, rpc_method: "rule_candidate_list", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "rule_candidate_reject", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "rule.candidate_reject", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "rule_insert_agents_md_block", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "rule.insert_agents_md_block", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "rule_list", module: "tools_security", target_backend: Backend::PythonCompat, rpc_method: "rule_list", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "rule_sync_agents_md", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "rule.sync_agents_md", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "run_check_gate", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "gate.run_check", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "switch_branch", module: "tools_security", target_backend: Backend::RustNative, rpc_method: "admin.branch_switch", op_class: OpClass::ProtectedMutation, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "embed_single_symbol", module: "tools_semantic", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "embed_symbols", module: "tools_semantic", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "find_similar_functions", module: "tools_semantic", target_backend: Backend::PythonCompat, rpc_method: "find_similar_functions", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "gc_archive_import", module: "tools_semantic", target_backend: Backend::RustNative, rpc_method: "admin.gc_archive_import", op_class: OpClass::ProtectedMutation, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "gc_archive_inspect", module: "tools_semantic", target_backend: Backend::RustNative, rpc_method: "admin.gc_archive_inspect", op_class: OpClass::ReadOnly, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "gc_archive_list", module: "tools_semantic", target_backend: Backend::RustNative, rpc_method: "admin.gc_archive_list", op_class: OpClass::ReadOnly, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "gc_audit_get", module: "tools_semantic", target_backend: Backend::RustNative, rpc_method: "admin.gc_audit_get", op_class: OpClass::ReadOnly, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "gc_audit_list", module: "tools_semantic", target_backend: Backend::RustNative, rpc_method: "admin.gc_audit_list", op_class: OpClass::ReadOnly, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "gc_policy_get", module: "tools_semantic", target_backend: Backend::RustNative, rpc_method: "admin.gc_policy_get", op_class: OpClass::ReadOnly, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "gc_policy_set", module: "tools_semantic", target_backend: Backend::RustNative, rpc_method: "admin.gc_policy_set", op_class: OpClass::ProtectedMutation, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "gc_retention", module: "tools_semantic", target_backend: Backend::RustNative, rpc_method: "admin.gc_retention", op_class: OpClass::ReadOnly, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "get_project_dependencies", module: "tools_semantic", target_backend: Backend::PythonCompat, rpc_method: "get_project_dependencies", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_symbol_commit_history", module: "tools_semantic", target_backend: Backend::PythonCompat, rpc_method: "get_symbol_commit_history", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "import_codeowners", module: "tools_semantic", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "import_git_blame", module: "tools_semantic", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "import_project_dependencies", module: "tools_semantic", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "parse_codeowners", module: "tools_semantic", target_backend: Backend::PythonCompat, rpc_method: "parse_codeowners", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "prune_external_symbols", module: "tools_semantic", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "semantic_search", module: "tools_semantic", target_backend: Backend::PythonCompat, rpc_method: "semantic_search", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "ask_codebase", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "ask_codebase", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "blast_radius", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "blast_radius", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "churn_analysis", module: "tools_summary", target_backend: Backend::RustNative, rpc_method: "query.churn_analysis", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "cross_layer_impact", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "cross_layer_impact", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "defect_correlation", module: "tools_summary", target_backend: Backend::RustNative, rpc_method: "query.defect_correlation", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "defect_learn", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "defect_learn", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "defect_search", module: "tools_summary", target_backend: Backend::RustNative, rpc_method: "query.defect_search", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "defect_stats", module: "tools_summary", target_backend: Backend::RustNative, rpc_method: "defect.stats", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "defect_suggest_fix", module: "tools_summary", target_backend: Backend::RustNative, rpc_method: "query.defect_suggest_fix", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "diff_to_symbol", module: "tools_summary", target_backend: Backend::RustNative, rpc_method: "query.diff_to_symbol", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "evolution_frequency", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "evolution_frequency", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "find_uncovered_functions", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "find_uncovered_functions", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "generate_summary", module: "tools_summary", target_backend: Backend::RustNative, rpc_method: "summary.generate", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "get_clone_aware_impact", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "get_clone_aware_impact", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_coverage_for_symbol", module: "tools_summary", target_backend: Backend::RustNative, rpc_method: "query.coverage_for_symbol", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_ownership_map", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "get_ownership_map", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_summary", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "get_summary", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_token_savings_report", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "get_token_savings_report", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_vulnerability_blast_radius", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "get_vulnerability_blast_radius", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "guardrail_add_rule", module: "tools_summary", target_backend: Backend::RustNative, rpc_method: "guardrail.add_rule", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "guardrail_check_edit", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "guardrail_check_edit", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "guardrail_list_rules", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "guardrail_list_rules", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "guardrail_scan", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "guardrail_scan", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "hotspot_evolution", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "hotspot_evolution", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "import_coverage", module: "tools_summary", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "project_brief", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "project_brief", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "record_token_savings", module: "tools_summary", target_backend: Backend::RustNative, rpc_method: "edit.record_token_savings", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "repo_map", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "repo_map", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "review_readiness", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "review_readiness", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "test_impact_selection", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "test_impact_selection", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "who_to_ask", module: "tools_summary", target_backend: Backend::PythonCompat, rpc_method: "who_to_ask", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "audit_verify_chain", module: "tools_task", target_backend: Backend::PythonCompat, rpc_method: "audit_verify_chain", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "bootstrap_status", module: "tools_task", target_backend: Backend::PythonCompat, rpc_method: "bootstrap_status", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "cancel_job", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.job_cancel", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "cleanup_agent_rule_sync_log", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "admin.cleanup_rule_sync_log", op_class: OpClass::ProtectedMutation, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "clear_clones", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "admin.clear_clones", op_class: OpClass::ProtectedMutation, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "detect_clones", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "detect_clones_async", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "embed_symbols_async", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "get_clone_group_detail", module: "tools_task", target_backend: Backend::PythonCompat, rpc_method: "get_clone_group_detail", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_clone_group_stats", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "task.clone_group_stats", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_clone_stats", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "task.clone_stats", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_commit_tasks", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "query.commit_tasks", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_defect_correlation", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "query.get_defect_correlation", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_job_stats", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "task.job_stats", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_job_status", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "task.job_status", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_symbol_change_tasks", module: "tools_task", target_backend: Backend::PythonCompat, rpc_method: "get_symbol_change_tasks", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "get_symbol_issues", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "query.issues", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_task_commits", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.get_commits", op_class: OpClass::ReadOnly, batch: "existing-task", status: "stable" },
    ToolRoute { name: "get_task_symbol_changes", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.get_symbol_changes", op_class: OpClass::ReadOnly, batch: "existing-task", status: "stable" },
    ToolRoute { name: "get_test_cases", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "query.tests", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_test_coverage_summary", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "query.tests", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_test_stability", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "query.tests", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_tested_functions", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "query.tests", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "link_edit_audit_symbols", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.link_edit_audit_symbols", op_class: OpClass::ProtectedMutation, batch: "existing-task", status: "stable" },
    ToolRoute { name: "list_audit_signing_keys", module: "tools_task", target_backend: Backend::PythonCompat, rpc_method: "list_audit_signing_keys", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "list_clone_groups", module: "tools_task", target_backend: Backend::PythonCompat, rpc_method: "list_clone_groups", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "list_clones", module: "tools_task", target_backend: Backend::PythonCompat, rpc_method: "list_clones", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "list_jobs", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "task.list_jobs", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "record_task_symbol_change", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.record_symbol_change", op_class: OpClass::ProtectedMutation, batch: "existing-task", status: "stable" },
    ToolRoute { name: "rotate_audit_signing_key", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "admin.audit_rotate_key", op_class: OpClass::ProtectedMutation, batch: "T02-admin", status: "migrated" },
    ToolRoute { name: "rule_seed_bootstrap", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "rule.seed_bootstrap", op_class: OpClass::ProtectedMutation, batch: "T02-edit", status: "migrated" },
    ToolRoute { name: "semgrep_scan_async", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "task_apply", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.apply", op_class: OpClass::ProtectedMutation, batch: "existing-task", status: "stable" },
    ToolRoute { name: "task_capture_diff", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.capture_diff", op_class: OpClass::ProtectedMutation, batch: "existing-task", status: "stable" },
    ToolRoute { name: "task_close", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.close", op_class: OpClass::ProtectedMutation, batch: "existing-task", status: "stable" },
    ToolRoute { name: "task_completion_review", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.completion_review", op_class: OpClass::ProtectedMutation, batch: "existing-task", status: "stable" },
    ToolRoute { name: "task_create", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.create", op_class: OpClass::ProtectedMutation, batch: "existing-task", status: "stable" },
    ToolRoute { name: "task_create_from_plan", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.create_from_plan", op_class: OpClass::ProtectedMutation, batch: "existing-task", status: "stable" },
    ToolRoute { name: "task_create_subtask", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.create_subtask", op_class: OpClass::ProtectedMutation, batch: "existing-task", status: "stable" },
    ToolRoute { name: "task_list", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.list", op_class: OpClass::ReadOnly, batch: "existing-task", status: "stable" },
    ToolRoute { name: "task_next_step", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.claim", op_class: OpClass::ProtectedMutation, batch: "existing-task", status: "stable" },
    ToolRoute { name: "task_plan_template", module: "tools_task", target_backend: Backend::PythonCompat, rpc_method: "task_plan_template", op_class: OpClass::ReadOnly, batch: "P0-compat", status: "transition" },
    ToolRoute { name: "task_quality_findings", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.quality_findings", op_class: OpClass::ReadOnly, batch: "existing-task", status: "stable" },
    ToolRoute { name: "task_report_step", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.report", op_class: OpClass::ProtectedMutation, batch: "existing-task", status: "stable" },
    ToolRoute { name: "task_resolve_block", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.reopen", op_class: OpClass::ProtectedMutation, batch: "existing-task", status: "stable" },
    ToolRoute { name: "task_resolve_quality_finding", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.resolve_quality_finding", op_class: OpClass::ProtectedMutation, batch: "existing-task", status: "stable" },
    ToolRoute { name: "task_rollback", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.rollback", op_class: OpClass::ProtectedMutation, batch: "existing-task", status: "stable" },
    ToolRoute { name: "task_split", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.split", op_class: OpClass::ProtectedMutation, batch: "existing-task", status: "stable" },
    ToolRoute { name: "task_status", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.status", op_class: OpClass::ReadOnly, batch: "existing-task", status: "stable" },
    ToolRoute { name: "task_status_tree", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.status_tree", op_class: OpClass::ReadOnly, batch: "existing-task", status: "stable" },
    ToolRoute { name: "wait_for_job", module: "tools_task", target_backend: Backend::RustNative, rpc_method: "task.wait_for_job", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "work_next_job", module: "tools_task", target_backend: Backend::TaskRpc, rpc_method: "task.work_next", op_class: OpClass::ProtectedMutation, batch: "existing-task", status: "stable" },
    ToolRoute { name: "build_directory", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "workspace.build_directory", op_class: OpClass::ProtectedMutation, batch: "T02-fs", status: "migrated" },
    ToolRoute { name: "build_graph", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "workspace.build_graph", op_class: OpClass::ProtectedMutation, batch: "T02-fs", status: "migrated" },
    ToolRoute { name: "check_file_health", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "workspace.file.health", op_class: OpClass::ReadOnly, batch: "T02-fs", status: "migrated" },
    ToolRoute { name: "delete_workspace", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "workspace.remove", op_class: OpClass::ProtectedMutation, batch: "existing-native", status: "stable" },
    ToolRoute { name: "file_grep", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "workspace.file.grep", op_class: OpClass::ReadOnly, batch: "T02-fs", status: "migrated" },
    ToolRoute { name: "file_list", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "workspace.file.list", op_class: OpClass::ReadOnly, batch: "T02-fs", status: "migrated" },
    ToolRoute { name: "file_read", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "workspace.file.read", op_class: OpClass::ReadOnly, batch: "T02-fs", status: "migrated" },
    ToolRoute { name: "file_symbol_content", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "workspace.file.symbol_content", op_class: OpClass::ReadOnly, batch: "T02-fs", status: "migrated" },
    ToolRoute { name: "get_active_workspace", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "workspace.status", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_code_health_check", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "query.code_health", op_class: OpClass::ReadOnly, batch: "T02-metrics", status: "migrated" },
    ToolRoute { name: "get_code_metrics_summary", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "query.metrics_summary", op_class: OpClass::ReadOnly, batch: "T02-metrics", status: "migrated" },
    ToolRoute { name: "get_commit_changes", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "query.git_commit_changes", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_complexity_hotspots", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "query.complexity_hotspots", op_class: OpClass::ReadOnly, batch: "T02-metrics", status: "migrated" },
    ToolRoute { name: "get_coupling_analysis", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "query.coupling_analysis", op_class: OpClass::ReadOnly, batch: "T02-metrics", status: "migrated" },
    ToolRoute { name: "get_function_metrics", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "query.function_metrics", op_class: OpClass::ReadOnly, batch: "T02-metrics", status: "migrated" },
    ToolRoute { name: "get_git_commits", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "query.git_commits", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_git_stats", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "query.git_stats", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "get_largest_functions", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "query.largest_functions", op_class: OpClass::ReadOnly, batch: "T02-metrics", status: "migrated" },
    ToolRoute { name: "get_most_coupled_functions", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "query.most_coupled_functions", op_class: OpClass::ReadOnly, batch: "T02-metrics", status: "migrated" },
    ToolRoute { name: "get_status", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "query.status", op_class: OpClass::ReadOnly, batch: "T02-metrics", status: "migrated" },
    ToolRoute { name: "get_symbol_content_by_hash", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "query.symbol_content_by_hash", op_class: OpClass::ReadOnly, batch: "T02-metrics", status: "migrated" },
    ToolRoute { name: "import_git_history", module: "tools_workspace", target_backend: Backend::TaskRpc, rpc_method: "task.job_submit", op_class: OpClass::ProtectedMutation, batch: "T02-job", status: "migrated" },
    ToolRoute { name: "list_workspaces", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "workspace.list", op_class: OpClass::ReadOnly, batch: "existing-native", status: "stable" },
    ToolRoute { name: "refresh_file", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "workspace.file.refresh_file", op_class: OpClass::ProtectedMutation, batch: "T02-fs", status: "migrated" },
    ToolRoute { name: "register_workspace", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "workspace.register", op_class: OpClass::ProtectedMutation, batch: "existing-native", status: "stable" },
    ToolRoute { name: "remove_file", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "workspace.file.remove", op_class: OpClass::ProtectedMutation, batch: "T02-fs", status: "migrated" },
    ToolRoute { name: "set_active_workspace", module: "tools_workspace", target_backend: Backend::RustNative, rpc_method: "workspace.activate", op_class: OpClass::ProtectedMutation, batch: "existing-native", status: "stable" },
];

/// 路由矩阵注册表（ToolRegistry）。
pub struct ToolRegistry;

impl ToolRegistry {
    /// 按 RPC method 查找路由条目（python_compat 时 method=工具名）。
    pub fn lookup(method: &str) -> Option<&'static ToolRoute> {
        TOOL_ROUTES
            .iter()
            .find(|r| r.rpc_method == method || r.name == method)
    }

    /// 迭代全部路由条目。
    pub fn iter() -> impl Iterator<Item = &'static ToolRoute> {
        TOOL_ROUTES.iter()
    }

    /// 校验覆盖率：条目总数必须等于预期（239）。
    pub fn validate_coverage(total: usize) -> Result<(), String> {
        let actual = TOOL_ROUTES.len();
        if actual != total {
            return Err(format!(
                "路由矩阵覆盖率不匹配: 期望 {total} 条，实际 {actual} 条"
            ));
        }
        // 校验 name 唯一性（编译期常量表，运行时兜底）
        let mut seen = std::collections::HashSet::new();
        for r in TOOL_ROUTES {
            if !seen.insert(r.name) {
                return Err(format!("路由矩阵工具名重复: {}", r.name));
            }
            if r.rpc_method.is_empty() {
                return Err(format!("路由矩阵工具 {} 缺少 rpc_method（本地隐式路径）", r.name));
            }
        }
        Ok(())
    }

    /// 按 backend 过滤工具列表。
    pub fn list_by_backend(backend: Backend) -> Vec<&'static ToolRoute> {
        TOOL_ROUTES
            .iter()
            .filter(|r| r.target_backend == backend)
            .collect()
    }

    /// 导出 /v1/meta/tools 自描述 JSON 数组（HttpServer.meta_tools 使用）。
    pub fn meta_tools_value() -> Value {
        let rows: Vec<Value> = TOOL_ROUTES
            .iter()
            .map(|r| {
                serde_json::json!({
                    "name": r.name,
                    "module": r.module,
                    "target_backend": r.target_backend.as_str(),
                    "rpc_method": r.rpc_method,
                    "op_class": r.op_class.as_str(),
                    "batch": r.batch,
                    "status": r.status,
                })
            })
            .collect();
        Value::Array(rows)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_coverage_is_239() {
        assert_eq!(TOOL_ROUTES.len(), 239);
        assert!(ToolRegistry::validate_coverage(239).is_ok());
    }

    #[test]
    fn test_lookup_existing() {
        // rust_native 示例
        assert!(ToolRegistry::lookup("query.stats").is_some());
        // python_compat 示例（按工具名直达）
        assert!(ToolRegistry::lookup("get_impact").is_some());
        // 不存在的 method
        assert!(ToolRegistry::lookup("no.such.method").is_none());
    }

    #[test]
    fn test_list_by_backend() {
        let native = ToolRegistry::list_by_backend(Backend::RustNative);
        let compat = ToolRegistry::list_by_backend(Backend::PythonCompat);
        assert!(!native.is_empty());
        assert!(!compat.is_empty());
        let total: usize = ["rust_native", "task_rpc", "python_compat", "declared_unavailable"]
            .iter()
            .map(|b| {
                let backend = match *b {
                    "rust_native" => Backend::RustNative,
                    "task_rpc" => Backend::TaskRpc,
                    "python_compat" => Backend::PythonCompat,
                    _ => Backend::DeclaredUnavailable,
                };
                ToolRegistry::list_by_backend(backend).len()
            })
            .sum();
        assert_eq!(total, TOOL_ROUTES.len());
    }

    #[test]
    fn test_meta_tools_length() {
        let v = ToolRegistry::meta_tools_value();
        assert_eq!(v.as_array().map(|a| a.len()), Some(239));
    }
}
