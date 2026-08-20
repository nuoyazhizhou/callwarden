"""H4C-2+3 合并：符号+任务 read-only 工具接入 worker 的真实进程门测试。

覆盖（派发单 T-1786716190783-ba187c88 步骤#3）：
- 单元层：compat_worker 装配导入后 registry 总数 = 26（H4C-1 默认 1 +
  H4C-2 符号 15 + H4C-3 任务 10），RUST_COMPAT_ROUTE 同步，两端对齐门
  validate_against_rust_route aligned；写语义工具（run_semgrep_scan /
  detect_clones / task_create 等）保持 fail-closed 未注册；源码级对比
  http_server.rs COMPAT_ROUTE_WHITELIST 与 Python RUST_COMPAT_ROUTE 完全一致；
- 真实进程门 TestRealDaemonCombinedWorkerCutover：隔离 daemon + 生产
  HttpDaemonRpcClient，正向覆盖符号组（get_top_callers 经 worker 返回
  种子库空调用数据）与任务组（task_plan_template 经 worker 返回模板字符串），
  负向断言未知方法返回 method_not_found 结构化错误（不得 skip）。
  W2-2（T-1786840097330-a9e0ec69）：get_clone_stats / get_job_stats /
  get_clone_group_stats 3 个迁移 rust_native，任务组 16->13（正向改用
  list_jobs 仍走 worker）。
  W3-2（T-1786861820151-f3cecf40）：get_job_status / list_jobs / wait_for_job
  3 个迁移 rust_native，任务组 13->10（正向改用仍走 worker 的
  task_plan_template，纯模板返回、无表依赖）。
  W3-3（T-1786861820151-deb64c48）：get_semgrep_findings 迁移 rust_native，
  符号组 15->14（正向改用仍走 worker 的 get_top_callers，minimal 种子库
  空调用数据 → 空列表，验证符号组方法经 worker 路由可用；
  预热改用 task_plan_template）。
  W4-1（T-1786886251769-22b94ee8-sub-1）：get_file_history / get_commit_tasks
  2 个迁移 rust_native，符号组 14->13、任务组 10->9（正向仍走 worker 的
  get_top_callers / task_plan_template 不变）。
  W4-2（T-1786886251769-22b94ee8-sub-2）：get_coverage_for_symbol /
  diff_to_symbol 2 个迁移 rust_native，摘要组 26->24，88->86
  （正向仍走 worker 的 get_top_callers / task_plan_template 不变）。
  W4-3（T-1786886251769-22b94ee8-sub-3）：defect_correlation / churn_analysis /
  defect_search / defect_suggest_fix 4 个迁移 rust_native，摘要组 24->20；
  get_defect_correlation 迁移 rust_native，任务组 9->8，86->81。
  W4-4（T-1786886251769-22b94ee8-sub-4）：diff_branches 迁移 rust_native，
  security 组 16->15，81->80（import_git_history 写面保持 python_compat，
  HTTP 模式 fail-closed，见 ledger §9.25）。

归类依据：http-daemon-mvp-compatibility-contract.md §3.3 worker 契约；
Rust 侧 http_server.rs COMPAT_ROUTE_WHITELIST 与 Python 侧
compat_registry.py RUST_COMPAT_ROUTE 由 validate_against_rust_route 对齐
（H4C-1 基建），本任务工具层接入同步 3 处约束。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 装配导入：import compat_worker 即触发其内部对 tools_query / tools_task 的
# 装配 import，工具模块模块级 register_compat_routes 随之注册到 registry 单例。
import server.compat_worker as _compat_worker_asm  # noqa: E402,F401
from server import compat_registry as reg  # noqa: E402
from callwarden.server.daemon_client import (  # noqa: E402
    DaemonRemoteError,
    DaemonUnavailableError,
    E_HTTP_REQUEST_TIMEOUT,
    HttpDaemonRpcClient,
    route_worker_call,
)

# 注册计数口径：H4C-1 默认 1 + H4C-2 符号 15 + H4C-3 任务 10 +
# H4C-2 第二批摘要/演化/护栏/缺陷组 27 + 语义/外部符号组 5 +
# H4C-2 第三批 security 组 17 + rules 组 8 +
# H4C-2 第三批（T-1786747295227-b876fddf）：collab 组 4 + p2 组 5 +
# p3 组 5 + p4 组 1 = 101
# （整改 T-1786747295227-49c90d68：rule_candidate_list/rule_list/
# get_applicable_rules 3 个纯 SELECT 只读方法接入 worker，security 组 14→17；
# W2-1 T-1786840097330-dec66710：get_uncommented_symbols / get_module_call_stats /
# get_semgrep_stats 3 个迁移 rust_native，H4C-1 默认 2→1、符号组 17→15，107→104；
# W2-2 T-1786840097330-a9e0ec69：get_clone_stats / get_job_stats /
# get_clone_group_stats 3 个迁移 rust_native，任务组 16→13，104→101；
# W2-3 T-1786840097331-fd01a3f8：defect_stats / get_edit_stats 2 个迁移
# rust_native，摘要组 27→26、security 组 17→16，101→99；
# W3-1 T-1786861820150-bfe5e805：list_build_contexts / get_build_context /
# get_active_build_context / get_resolved_edges / count_resolved_edges 5 个迁移
# rust_native，rules 组 8→3，99→94；
# W3-2 T-1786861820151-f3cecf40：get_job_status / list_jobs / wait_for_job
# 3 个迁移 rust_native，任务组 13→10，94→91；
# W3-3 T-1786861820151-deb64c48：get_semgrep_findings 迁移 rust_native，
# 符号组 15→14，91→90；
# W4-1 T-1786886251769-22b94ee8-sub-1：get_file_history / get_commit_tasks
# 2 个迁移 rust_native，符号组 14→13、任务组 10→9，90→88；
# W4-2 T-1786886251769-22b94ee8-sub-2：get_coverage_for_symbol /
# diff_to_symbol 2 个迁移 rust_native，摘要组 26→24，88→86；
# W4-3 T-1786886251769-22b94ee8-sub-3：defect_correlation / churn_analysis /
# defect_search / defect_suggest_fix 4 个迁移 rust_native，摘要组 24→20；
# get_defect_correlation 迁移 rust_native，任务组 9→8，86→81；
# W4-4 T-1786886251769-22b94ee8-sub-4：diff_branches 迁移 rust_native，
# security 组 16→15，81→80）
EXPECTED_TOTAL = 80

SYMBOL_METHODS = [
    "get_symbol_history",
    "get_recent_changes",
    "get_impact",
    "get_top_callers",
    "get_orphan_symbols",
    "get_deepest_functions",
    "get_comment_from_version",
    "get_issue_summary",
    "find_issues",
    "get_comment_coverage",
    "get_call_heatmap",
    "get_test_coverage",
    "export_module_graph",
]

# W2-2（T-1786840097330-a9e0ec69）：get_clone_stats / get_job_stats /
# get_clone_group_stats 3 个迁移 rust_native，任务组 16→13；
# W3-2（T-1786861820151-f3cecf40）：get_job_status / list_jobs / wait_for_job
# 3 个迁移 rust_native，任务组 13→10；
# W4-1（T-1786886251769-22b94ee8-sub-1）：get_commit_tasks 迁移 rust_native，
# 任务组 10→9；
# W4-3（T-1786886251769-22b94ee8-sub-3）：get_defect_correlation 迁移
# rust_native，任务组 9→8（仍走 worker 的任务只读方法如下 8 个）。
TASK_METHODS = [
    "get_symbol_change_tasks",
    "audit_verify_chain",
    "list_audit_signing_keys",
    "bootstrap_status",
    "list_clones",
    "list_clone_groups",
    "get_clone_group_detail",
    "task_plan_template",
]

# H4C-2 第二批（T-1786747295213-64204cce）：摘要/演化/护栏/缺陷组只读白名单 20 个
# （tools_summary.py 模块级 register_compat_routes 注册；defect_stats 已 W2-3 迁移
# rust_native，T-1786840097331-fd01a3f8；get_coverage_for_symbol / diff_to_symbol
# 已 W4-2 T-1786886251769-22b94ee8-sub-2 迁移 rust_native；defect_correlation /
# churn_analysis / defect_search / defect_suggest_fix 已 W4-3
# T-1786886251769-22b94ee8-sub-3 迁移 rust_native；defect_learn 写面保留
# python_compat）。
SUMMARY_METHODS = [
    "get_summary",
    "project_brief",
    "repo_map",
    "find_uncovered_functions",
    "test_impact_selection",
    "who_to_ask",
    "get_ownership_map",
    "guardrail_scan",
    "guardrail_check_edit",
    "guardrail_list_rules",
    "blast_radius",
    "ask_codebase",
    "get_token_savings_report",
    "get_vulnerability_blast_radius",
    "get_clone_aware_impact",
    "review_readiness",
    "cross_layer_impact",
    "evolution_frequency",
    "hotspot_evolution",
    "defect_learn",
]

# H4C-2 第二批（T-1786747295213-64204cce）：语义/外部符号组只读白名单 5 个
# （tools_semantic.py 模块级 register_compat_routes 注册）。
SEMANTIC_METHODS = [
    "semantic_search",
    "find_similar_functions",
    "get_symbol_commit_history",
    "parse_codeowners",
    "get_project_dependencies",
]

# H4C-2 第三批（T-1786747295227-49c90d68）：security 组只读白名单 15 个
# （get_edit_stats 已 W2-3 T-1786840097331-fd01a3f8 迁移 rust_native 移除；
# diff_branches 已 W4-4 T-1786886251769-22b94ee8-sub-4 迁移 rust_native 移除）
# （tools_security.py 模块级 register_compat_routes 注册）。
# 分支/编辑历史/跨仓库/LSP 组：db 层纯 SELECT 或只读等价版（merge_preview
# handler 跳过 switch_branch_context 写副作用）；规则查询组
# （rule_candidate_list/rule_list/get_applicable_rules）：db_agent_rules 纯
# SELECT（查 agent_rule_candidates / agent_rules），整改接入 worker。
SECURITY_METHODS = [
    "list_branches",
    "merge_preview",
    "get_edit_history",
    "find_shared_symbols",
    "cross_repo_impact",
    "cross_repo_summary",
    "lsp_hover",
    "lsp_definition",
    "lsp_references",
    "lsp_diagnostics",
    "lsp_completion",
    "lsp_check_available",
    "rule_candidate_list",
    "rule_list",
    "get_applicable_rules",
]

# H4C-2 第三批（T-1786747295227-49c90d68）：rules 组只读白名单 3 个
# （tools_rules.py 模块级 register_compat_routes 注册；list_build_contexts /
# get_build_context / get_active_build_context / get_resolved_edges /
# count_resolved_edges 已 W3-1 T-1786861820150-bfe5e805 迁移 rust_native，
# 8->3，不再注册于 compat registry）。
# toolchain 组：db_toolchain 查询函数均纯 SELECT（handler 跳过
# init_toolchain_schema 写操作，表已由本地/daemon 初始化过）。
RULES_METHODS = [
    "list_toolchains",
    "get_toolchain",
    "get_workspace_toolchains",
]

# H4C-2 第三批（T-1786747295227-b876fddf）：collab 组只读白名单 4 个
# （tools_collab.py 模块级 register_compat_routes 注册，HTTP 经 worker 执行；
# _local 保留 _collab_rpc_call 直连语义，本地/auto 模式不变）。
COLLAB_METHODS = [
    "get_role_view",
    "find_evidence",
    "get_freshness_status",
    "get_gate_decision",
]

# H4C-2 第三批（T-1786747295227-b876fddf）：p2 依赖图/环检测组只读白名单 5 个
# （tools_p2_graph.py 模块级 register_compat_routes 注册；validate_revision_dependencies
# handler 为只读等价版——db 层原实现内部调用 build_hard_dependency_edges（含 INSERT+
# commit 写操作），handler 用纯 SELECT 查询现有依赖 + 内存建边模拟，不触碰写路径）。
P2_METHODS = [
    "get_artifact_freshness",
    "get_interface_providers",
    "detect_cycle",
    "validate_revision_dependencies",
    "get_dependency_edges",
]

# H4C-2 第三批（T-1786747295227-b876fddf）：p3 身份/证明组只读白名单 5 个
# （tools_p3_identity.py 模块级 register_compat_routes 注册；handler 复用模块级
# _p3_resolve_identity_arg / _p3_identity_mcp_reason 副本解析参数）。
P3_METHODS = [
    "get_action_identity",
    "check_action_identity",
    "check_session_separation",
    "get_attestation_validity",
    "list_attestation_revocations",
]

# H4C-2 第三批（T-1786747295227-b876fddf）：p4 租约组只读白名单 1 个
# （tools_p4_lease.py 模块级 register_compat_routes 注册；lease_* 5 项为
# rust_native 不经 python compat worker，assignment_create/revoke 为写语义不接入）。
P4_METHODS = [
    "assignment_show",
]

# 写语义工具（用户决策 Q3：不接入，保持 _http_unsupported fail-closed）+
# governance_write 任务工具（task_create/next/report/apply/close 等）。
# 均不得出现在 registry / RUST_COMPAT_ROUTE。
# H4C-4（T-1786745594007-b2d57524）：补全 13 个写语义工具（含 restore_comment /
# rotate_audit_signing_key / clear_clones / cancel_job / embed_symbols_async）。
WRITE_SEMANTICS_METHODS = [
    "run_semgrep_scan",
    "scan_semgrep_incremental",
    "detect_clones",
    "detect_clones_async",
    "semgrep_scan_async",
    "rule_seed_bootstrap",
    "cleanup_agent_rule_sync_log",
    "restore_comment",
    "restore_all_comments",
    "rotate_audit_signing_key",
    "clear_clones",
    "cancel_job",
    "embed_symbols_async",
    "task_create",
    "task_next",
    "task_report",
    "task_apply",
    "task_close",
    # H4C-2 第二批（T-1786747295213-64204cce）：summary/semantic 写语义/治理工具
    # （generate_summary 矩阵标注 read_only 属异常，实为写语义，不接入 worker）。
    "generate_summary",
    "import_coverage",
    "guardrail_add_rule",
    "record_token_savings",
    "embed_symbols",
    "embed_single_symbol",
    "import_codeowners",
    "import_git_blame",
    "import_project_dependencies",
    "prune_external_symbols",
    "gc_retention",
    "gc_policy_get",
    "gc_policy_set",
    "gc_archive_list",
    "gc_archive_inspect",
    "gc_audit_list",
    "gc_audit_get",
    "gc_archive_import",
    # H4C-2 第三批（T-1786747295227-49c90d68）：security 组写语义/治理工具
    # （register_branch/switch_branch 有 set_active_workspace 写副作用；
    # propose_*/revert_edit 编辑审计；run_check_gate/resolve_gate_findings 门槛；
    # rule_*/extract_* 规则管理；detect_cross_repo_deps 写依赖图）。
    "register_branch",
    "switch_branch",
    "propose_edit",
    "propose_range_patch",
    "propose_symbol_patch",
    "propose_symbol_id_patch",
    "revert_edit",
    "detect_cross_repo_deps",
    "run_check_gate",
    "resolve_gate_findings",
    "rule_candidate_create",
    "rule_candidate_accept",
    "rule_candidate_reject",
    "rule_sync_agents_md",
    "rule_insert_agents_md_block",
    "extract_rule_candidates_from_quality_findings",
    # high-risk 未接入（依赖 Rust 内存 GraphStore，db 层无 SQL 等价实现，
    # worker 只读 SQLite 连接无法承载，维持 fail-closed）。
    "diff_callers",
    "diff_callees",
    "compare_snapshots",
    # rules 组 get_metrics 不依赖 db.conn（走 daemon RPC / 本地 MetricsCollector），
    # 不接入 worker，维持 fail-closed。
    "get_metrics",
    # H4C-2 第三批（T-1786747295227-b876fddf）：collab/p2/p3/p4 写语义/治理工具
    # （submit_verdict/append_evidence 写 gate/evidence；p2 5 项写依赖图与接口
    # 选择；p3 record/register 写身份证明；p4 assignment_create/revoke 写任务
    # 分配。worker 只读连接无法承载，维持 fail-closed；lease_* 5 项为
    # rust_native 不经 python compat registry，天然不在本清单）。
    "submit_verdict",
    "append_evidence",
    "import_envelope_dependencies",
    "record_artifact_identity",
    "publish_interface",
    "select_interface_provider",
    "build_hard_dependency_edges",
    "record_action_identity",
    "register_attestation_revocation",
    "assignment_create",
    "assignment_revoke",
]

# H4C-4（T-1786745594007-b2d57524）：13 个写语义工具 → 工具层入口 fail-closed
# 断言参数化（module, tool_name, 调用参数）。HTTP 模式必须短路 _http_unsupported
# 返回 E_HTTP_COMPAT_UNSUPPORTED，绝不触碰 get_db()/本地 SQLite。
HTTP_UNSUPPORTED_WRITE_TOOLS = [
    ("tools_query", "restore_comment", {"spec": "a.py:foo@v1", "preview": True}),
    ("tools_query", "restore_all_comments", {}),
    ("tools_query", "run_semgrep_scan", {}),
    ("tools_query", "scan_semgrep_incremental", {}),
    ("tools_task", "rotate_audit_signing_key", {"key_id": "key-h4c4-test"}),
    ("tools_task", "detect_clones", {}),
    ("tools_task", "clear_clones", {}),
    ("tools_task", "cancel_job", {"job_id": "J-h4c4"}),
    ("tools_task", "detect_clones_async", {}),
    ("tools_task", "embed_symbols_async", {}),
    ("tools_task", "semgrep_scan_async", {}),
    ("tools_task", "rule_seed_bootstrap", {}),
    ("tools_task", "cleanup_agent_rule_sync_log", {}),
    # H4C-2 第二批（T-1786747295213-64204cce）：summary/semantic 写语义/治理工具
    # HTTP 模式必须短路 _http_unsupported（不注册 worker handler、不构造 CodeGraphDB）。
    ("tools_summary", "generate_summary", {"qualified_name": "a.b", "summary": "x"}),
    ("tools_summary", "import_coverage", {"file_path": "coverage.info"}),
    ("tools_summary", "guardrail_add_rule", {"category": "db", "pattern": "x"}),
    ("tools_summary", "record_token_savings", {"operation": "rag", "original_tokens": 10, "actual_tokens": 5}),
    ("tools_semantic", "embed_symbols", {}),
    ("tools_semantic", "embed_single_symbol", {"symbol_hash": "h1"}),
    ("tools_semantic", "import_codeowners", {}),
    ("tools_semantic", "import_git_blame", {}),
    ("tools_semantic", "import_project_dependencies", {}),
    ("tools_semantic", "prune_external_symbols", {}),
    ("tools_semantic", "gc_retention", {}),
    ("tools_semantic", "gc_policy_get", {}),
    ("tools_semantic", "gc_policy_set", {}),
    ("tools_semantic", "gc_archive_list", {}),
    ("tools_semantic", "gc_archive_inspect", {"path": "a.db.gz"}),
    ("tools_semantic", "gc_audit_list", {}),
    ("tools_semantic", "gc_audit_get", {"audit_id": 1}),
    ("tools_semantic", "gc_archive_import", {"path": "a.db.gz", "file_path": "src/a.py"}),
    # H4C-2 第三批（T-1786747295227-49c90d68）：security 组写语义/治理 + high-risk
    # 未接入 + rules 组 get_metrics。HTTP 模式必须短路 _http_unsupported。
    ("tools_security", "register_branch", {"branch_name": "feature-x"}),
    ("tools_security", "switch_branch", {"branch_name": "main"}),
    ("tools_security", "propose_edit", {"file_path": "a.py", "new_content": "x"}),
    ("tools_security", "propose_range_patch", {"file_path": "a.py", "start_line": 1, "end_line": 2, "replacement": "x"}),
    ("tools_security", "propose_symbol_patch", {"file_path": "a.py", "symbol_name": "f", "patch": "x"}),
    ("tools_security", "propose_symbol_id_patch", {"symbol_id": 1, "patch": "x"}),
    ("tools_security", "revert_edit", {"audit_id": 1}),
    ("tools_security", "detect_cross_repo_deps", {"source_workspace": "repo-a"}),
    ("tools_security", "run_check_gate", {"task_id": "T-1", "step_id": "S-1", "changed_files": ["a.py"]}),
    ("tools_security", "resolve_gate_findings", {"task_id": "T-1"}),
    ("tools_security", "rule_candidate_create", {"title": "t", "rule_text": "x"}),
    ("tools_security", "rule_candidate_accept", {"candidate_id": "ARC-1"}),
    ("tools_security", "rule_candidate_reject", {"candidate_id": "ARC-1"}),
    ("tools_security", "rule_sync_agents_md", {}),
    ("tools_security", "rule_insert_agents_md_block", {}),
    ("tools_security", "extract_rule_candidates_from_quality_findings", {}),
    # high-risk 项：依赖 Rust 内存 GraphStore，db 层无 SQL 等价实现，不接入。
    ("tools_security", "diff_callers", {"left_workspace_id": "1", "right_workspace_id": "2", "qualified_name": "a.b"}),
    ("tools_security", "diff_callees", {"left_workspace_id": "1", "right_workspace_id": "2", "qualified_name": "a.b"}),
    ("tools_security", "compare_snapshots", {"left_workspace_id": "1", "right_workspace_id": "2"}),
    # rules 组 get_metrics 不依赖 db.conn（daemon RPC / MetricsCollector），不接入。
    ("tools_rules", "get_metrics", {}),
    # H4C-2 第三批（T-1786747295227-b876fddf）：collab/p2/p3/p4 写语义/治理工具
    # HTTP 模式必须短路 _http_unsupported（不注册 worker handler、不构造 CodeGraphDB）。
    # 参数按各工具真实签名提供（仅需满足 Python 调用不抛 TypeError，_http_unsupported
    # 在进入 db 写路径前拦截）。
    # 注意：任务 4 起 collab 写 2（submit_verdict/append_evidence）改经 daemon RPC
    # 薄壳转发（不再 _http_unsupported），其 HTTP/legacy 路由见
    # test_http_governance_error_cutover.py::TestHttpGovernanceUnsupported。
    ("tools_p2_graph", "import_envelope_dependencies", {"workspace_id": 1, "task_id": "T-1", "contract_id": "C-1", "contract_revision": 1, "dependencies": []}),
    ("tools_p2_graph", "record_artifact_identity", {"workspace_id": 1, "task_id": "T-1", "contract_id": "C-1", "contract_revision": 1, "artifact_type": "file", "artifact_ref": "a.py"}),
    ("tools_p2_graph", "publish_interface", {"workspace_id": 1, "task_id": "T-1", "contract_id": "C-1", "contract_revision": 1, "interface_name": "iface", "version": "1.0.0"}),
    ("tools_p2_graph", "select_interface_provider", {"workspace_id": 1, "consumer_task_id": "T-1", "contract_id": "C-1", "contract_revision": 1, "interface_name": "iface", "selected_provider_task_id": "T-2"}),
    ("tools_p2_graph", "build_hard_dependency_edges", {"workspace_id": 1, "contract_id": "C-1", "contract_revision": 1}),
    ("tools_p3_identity", "record_action_identity", {"action_id": "A-1", "action_type": "contract", "task_id": "T-1", "identity": '{"agent_id":"ag","session_id":"s","model_id":"m","role":"implementer"}'}),
    ("tools_p3_identity", "register_attestation_revocation", {"issuer": "iss", "signing_key_id": "k1", "revocation_mode": "compromised"}),
    ("tools_p4_lease", "assignment_create", {"task_id": "T-1", "role": "implementer"}),
    ("tools_p4_lease", "assignment_revoke", {"assignment_id": "AS-1"}),
]


# ============================================================
# 1. 单元层：装配导入后的 registry 状态
# ============================================================


class TestCombinedRegistry:
    """装配导入后符号+任务组注册与对齐门（fail-closed 不变）。"""

    def test_total_registered(self):
        registry = reg.get_compat_registry()
        assert len(registry) == EXPECTED_TOTAL, (
            f"registry 方法数应 = {EXPECTED_TOTAL}"
            f"（H4C-1 默认 1 + 符号组 13 + 任务组 8 + 摘要组 20 + 语义组 5 + "
            f"security 组 15 + rules 组 3 + collab 组 4 + p2 组 5 + p3 组 5 + p4 组 1），"
            f"实际 {len(registry)}"
        )
        assert len(reg.RUST_COMPAT_ROUTE) == EXPECTED_TOTAL, (
            f"RUST_COMPAT_ROUTE 应同步为 {EXPECTED_TOTAL}，实际 {len(reg.RUST_COMPAT_ROUTE)}"
        )

    def test_symbol_group_registered(self):
        registry = reg.get_compat_registry()
        assert len(SYMBOL_METHODS) == 13
        for m in SYMBOL_METHODS:
            assert registry.is_compat_method(m), f"符号工具 {m} 未注册"
            assert registry.operation_class(m) == reg.READ_ONLY
            assert reg.compat_route(m) == reg.READ_ONLY

    def test_summary_group_registered(self):
        """H4C-2 第二批：摘要/演化/护栏/缺陷组 24 个只读工具注册与路由可见（defect_stats 已 W2-3 迁移）。"""
        registry = reg.get_compat_registry()
        assert len(SUMMARY_METHODS) == 20
        for m in SUMMARY_METHODS:
            assert registry.is_compat_method(m), f"摘要工具 {m} 未注册"
            assert registry.operation_class(m) == reg.READ_ONLY
            assert reg.compat_route(m) == reg.READ_ONLY

    def test_semantic_group_registered(self):
        """H4C-2 第二批：语义/外部符号组 5 个只读工具注册与路由可见。"""
        registry = reg.get_compat_registry()
        assert len(SEMANTIC_METHODS) == 5
        for m in SEMANTIC_METHODS:
            assert registry.is_compat_method(m), f"语义工具 {m} 未注册"
            assert registry.operation_class(m) == reg.READ_ONLY
            assert reg.compat_route(m) == reg.READ_ONLY

    def test_security_group_registered(self):
        """H4C-2 第三批：security 组 15 个只读工具注册与路由可见。"""
        registry = reg.get_compat_registry()
        assert len(SECURITY_METHODS) == 15
        for m in SECURITY_METHODS:
            assert registry.is_compat_method(m), f"security 工具 {m} 未注册"
            assert registry.operation_class(m) == reg.READ_ONLY
            assert reg.compat_route(m) == reg.READ_ONLY

    def test_rules_group_registered(self):
        """H4C-2 第三批：rules 组 3 个只读工具注册与路由可见（build_context 5 个
        已 W3-1 T-1786861820150-bfe5e805 迁移 rust_native，8->3）。"""
        registry = reg.get_compat_registry()
        assert len(RULES_METHODS) == 3
        for m in RULES_METHODS:
            assert registry.is_compat_method(m), f"rules 工具 {m} 未注册"
            assert registry.operation_class(m) == reg.READ_ONLY
            assert reg.compat_route(m) == reg.READ_ONLY

    def test_collab_group_registered(self):
        """H4C-2 第三批：collab 组 4 个只读工具注册与路由可见。"""
        registry = reg.get_compat_registry()
        assert len(COLLAB_METHODS) == 4
        for m in COLLAB_METHODS:
            assert registry.is_compat_method(m), f"collab 工具 {m} 未注册"
            assert registry.operation_class(m) == reg.READ_ONLY
            assert reg.compat_route(m) == reg.READ_ONLY

    def test_p2_group_registered(self):
        """H4C-2 第三批：p2 依赖图/环检测组 5 个只读工具注册与路由可见。"""
        registry = reg.get_compat_registry()
        assert len(P2_METHODS) == 5
        for m in P2_METHODS:
            assert registry.is_compat_method(m), f"p2 工具 {m} 未注册"
            assert registry.operation_class(m) == reg.READ_ONLY
            assert reg.compat_route(m) == reg.READ_ONLY

    def test_p3_group_registered(self):
        """H4C-2 第三批：p3 身份/证明组 5 个只读工具注册与路由可见。"""
        registry = reg.get_compat_registry()
        assert len(P3_METHODS) == 5
        for m in P3_METHODS:
            assert registry.is_compat_method(m), f"p3 工具 {m} 未注册"
            assert registry.operation_class(m) == reg.READ_ONLY
            assert reg.compat_route(m) == reg.READ_ONLY

    def test_p4_group_registered(self):
        """H4C-2 第三批：p4 租约组 1 个只读工具注册与路由可见。"""
        registry = reg.get_compat_registry()
        assert len(P4_METHODS) == 1
        for m in P4_METHODS:
            assert registry.is_compat_method(m), f"p4 工具 {m} 未注册"
            assert registry.operation_class(m) == reg.READ_ONLY
            assert reg.compat_route(m) == reg.READ_ONLY

    def test_task_group_registered(self):
        registry = reg.get_compat_registry()
        assert len(TASK_METHODS) == 8
        for m in TASK_METHODS:
            assert registry.is_compat_method(m), f"任务工具 {m} 未注册"
            assert registry.operation_class(m) == reg.READ_ONLY
            assert reg.compat_route(m) == reg.READ_ONLY

    def test_validate_against_rust_route_aligned(self):
        # 两端对齐门：Python registry 与 RUST_COMPAT_ROUTE 完全一致
        result = reg.validate_against_rust_route()
        assert result["aligned"] is True, result
        assert result["missing"] == []
        assert result["extra"] == []
        assert result["mismatch"] == {}

    def test_rust_whitelist_source_sync(self):
        """源码级对比：http_server.rs COMPAT_ROUTE_WHITELIST 与 Python 侧完全一致。

        防漏同步（派发单"3 处同步约束"）：Rust 数组漏一个方法会让 HTTP 入口
        走 rust_native dispatch → method_not_found 泄漏；Python 侧多一个方法
        会让两端对齐门报警。此测试把两处源码拉齐。
        """
        rs_path = _REPO_ROOT / "rust_ext" / "src" / "daemon" / "http_server.rs"
        text = rs_path.read_text(encoding="utf-8")
        m = re.search(
            r"const COMPAT_ROUTE_WHITELIST:\s*&\[\(&str, &str\)\]\s*=\s*&\[(.*?)\];",
            text,
            re.S,
        )
        assert m is not None, "http_server.rs 找不到 COMPAT_ROUTE_WHITELIST 常量"
        rust_methods = set(
            re.findall(r'\(\s*"([a-z0-9_.]+)"\s*,\s*"([a-z_]+)"\s*\)', m.group(1))
        )
        assert len(rust_methods) == EXPECTED_TOTAL, (
            f"Rust 白名单应含 {EXPECTED_TOTAL} 个方法，实际 {len(rust_methods)}"
        )
        rust_map = dict(rust_methods)
        assert set(rust_map.keys()) == set(reg.RUST_COMPAT_ROUTE.keys()), (
            "Rust COMPAT_ROUTE_WHITELIST 与 Python RUST_COMPAT_ROUTE 方法名集合不一致"
        )
        for k, op in rust_map.items():
            assert op == reg.RUST_COMPAT_ROUTE[k], (
                f"方法 {k} operation_class 不一致: Rust={op} Python={reg.RUST_COMPAT_ROUTE[k]}"
            )

    def test_write_semantics_fail_closed(self):
        """写语义 + governance_write 工具不接入：registry 与路由均不可见。"""
        registry = reg.get_compat_registry()
        for m in WRITE_SEMANTICS_METHODS:
            assert not registry.is_compat_method(m), f"写语义工具 {m} 不应注册"
            assert reg.compat_route(m) is None, f"写语义工具 {m} 不应有 compat 路由"

    def test_default_registry_methods_preserved(self):
        # H4C-1 默认方法保持注册（未被覆盖）；get_uncommented_symbols 已 W2-1
        # 迁移 rust_native，默认 registry 仅剩 stats_top_files
        registry = reg.get_compat_registry()
        assert registry.is_compat_method("stats_top_files")
        assert not registry.is_compat_method("get_uncommented_symbols")


# ============================================================
# 2. 真实进程门：隔离 daemon + 生产 HttpDaemonRpcClient
# ============================================================

# 覆盖符号组（get_top_callers，W3-3 起）与任务组（task_plan_template）正向
# 路径所需的表：workspaces（_bind_readonly_db 解析 workspace_root）+
# file_instances + file_versions/call_versions（符号组正向 get_top_callers 的
# minimal 空表）+ semgrep_findings（历史保留：W3-3 起 get_semgrep_findings 已
# 迁移 rust_native 不再走 worker）+ jobs（历史保留：W3-2 后 list_jobs 已迁移
# rust_native，任务组正向改用 task_plan_template）。
COMBINED_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT DEFAULT '',
    root_path TEXT DEFAULT '',
    is_active INTEGER DEFAULT 0,
    created_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS file_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    status TEXT DEFAULT 'pending'
);
-- W3-3（T-1786861820151-deb64c48）：符号组正向改用 get_top_callers，
-- 需要 file_versions + call_versions（minimal 空表：无种子数据 → 空列表）。
CREATE TABLE IF NOT EXISTS file_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    version_num INTEGER DEFAULT 1,
    is_current INTEGER DEFAULT 1,
    parsed_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS call_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_version_id INTEGER NOT NULL,
    caller_qualified TEXT DEFAULT '',
    callee_qualified TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS semgrep_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    content_hash TEXT DEFAULT '',
    rule_id TEXT NOT NULL,
    rule_name TEXT DEFAULT '',
    message TEXT DEFAULT '',
    severity TEXT DEFAULT 'INFO',
    confidence TEXT DEFAULT 'UNKNOWN',
    language TEXT DEFAULT '',
    start_line INTEGER DEFAULT 0,
    end_line INTEGER DEFAULT 0,
    snippet TEXT DEFAULT '',
    fix TEXT DEFAULT '',
    symbol_id INTEGER DEFAULT 0,
    symbol_qualified TEXT DEFAULT '',
    scanned_at REAL DEFAULT 0,
    scan_id INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    workspace_id INTEGER NOT NULL,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    progress REAL DEFAULT 0.0,
    message TEXT DEFAULT '',
    params TEXT DEFAULT '{}',
    result_summary TEXT DEFAULT '{}',
    error TEXT DEFAULT '',
    cancel_requested INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL DEFAULT 0,
    finished_at REAL DEFAULT 0
);
-- security 组 list_branches：list_branch_workspaces 子查询 JOIN symbols（需表存在）
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    qualified_name TEXT DEFAULT '',
    symbol_hash TEXT DEFAULT '',
    name TEXT DEFAULT '',
    kind TEXT DEFAULT ''
);
-- rules 组 list_toolchains：db_toolchain.list_toolchains 全列 SELECT
-- （handler 跳过 init_toolchain_schema，种子库必须预建表）
CREATE TABLE IF NOT EXISTS toolchains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    compiler_path TEXT DEFAULT '',
    compiler_type TEXT DEFAULT '',
    version TEXT DEFAULT '',
    target_triple TEXT DEFAULT '',
    sysroot TEXT DEFAULT '',
    include_dirs TEXT DEFAULT '[]',
    predefined_macros TEXT DEFAULT '{}',
    fingerprint TEXT DEFAULT '',
    created_at REAL DEFAULT 0,
    updated_at REAL DEFAULT 0,
    description TEXT DEFAULT ''
);
-- security 组规则查询（整改 T-1786747295227-49c90d68）：rule_candidate_list 查
-- agent_rule_candidates、rule_list / get_applicable_rules 查 agent_rules。
-- 全局表（无 workspace_id），表结构镜像 db/schema.py。
CREATE TABLE IF NOT EXISTS agent_rule_candidates (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    rule_text TEXT NOT NULL,
    scope_json TEXT DEFAULT '{}',
    severity TEXT DEFAULT 'info',
    source TEXT DEFAULT 'manual',
    evidence_json TEXT DEFAULT '{}',
    confidence REAL DEFAULT 0.0,
    status TEXT DEFAULT 'pending',
    created_at REAL NOT NULL,
    reviewed_at REAL,
    reviewer TEXT DEFAULT '',
    linked_rule_id TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS agent_rules (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    rule_text TEXT NOT NULL,
    scope_json TEXT DEFAULT '{}',
    severity TEXT DEFAULT 'info',
    status TEXT DEFAULT 'active',
    source_candidate_id TEXT DEFAULT '',
    evidence_json TEXT DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    synced_to_agents_md INTEGER DEFAULT 0,
    sync_hash TEXT DEFAULT ''
);
"""


def _seed_combined_db(home_dir: Path, root_path: str) -> str:
    """在隔离 USERPROFILE 下建 worker 可读的种子库，返回 db 路径。

    - workspaces 注册 workspace_id=1（root_path 指向隔离临时目录）；
    - file_instances 1 条（workspace 1, src/app.py）；
    - file_versions / call_versions 空表（W3-3 起符号组正向 get_top_callers
      返回空列表）；
    - semgrep_findings 1 条（severity=ERROR, language=python）历史保留
      （W3-3 T-1786861820151-deb64c48 后 get_semgrep_findings 已迁移
      rust_native，不再经 worker 服务，符号组正向不再依赖该数据）；
    - jobs 1 条 completed（历史保留；W3-2 T-1786861820151-f3cecf40 后
      list_jobs 已迁移 rust_native，任务组正向改用 task_plan_template）。
    """
    db_file = home_dir / ".callwarden" / "callwarden.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file))
    try:
        conn.executescript(COMBINED_SCHEMA)
        conn.execute(
            "INSERT INTO workspaces (id, name, root_path, is_active) VALUES (1, 'seed-repo', ?, 1)",
            (root_path,),
        )
        cur = conn.execute(
            "INSERT INTO file_instances (workspace_id, rel_path, status) VALUES (1, 'src/app.py', 'parsed')"
        )
        fi_id = cur.lastrowid
        conn.execute(
            """INSERT INTO semgrep_findings
               (file_instance_id, content_hash, rule_id, rule_name, message,
                severity, confidence, language, start_line, end_line,
                snippet, fix, symbol_id, symbol_qualified, scanned_at, scan_id)
               VALUES (?, '', 'rule-no-else-return', 'no-else-return',
                       'simplify if-else', 'ERROR', 'HIGH', 'python',
                       10, 12, '', '', 0, 'app.run', 0, 0)""",
            (fi_id,),
        )
        conn.execute(
            """INSERT INTO jobs
               (job_id, workspace_id, job_type, status, progress, message,
                params, result_summary, error, cancel_requested, created_at,
                started_at, finished_at)
               VALUES ('J-seed-1', 1, 'clone_detect', 'completed', 1.0, 'done',
                       '{}', '{}', '', 0, 1000.0, 1000.0, 2000.0)"""
        )
        conn.execute(
            """INSERT INTO toolchains
               (name, compiler_path, compiler_type, version, target_triple, sysroot,
                include_dirs, predefined_macros, fingerprint, created_at, updated_at,
                description)
               VALUES ('gcc-12', '/usr/bin/gcc', 'gcc', '12.2.0', 'x86_64-linux-gnu',
                       '', '[]', '{}', 'fp-seed', 1000.0, 1000.0, 'seed toolchain')"""
        )
        # security 组规则查询种子（整改 T-1786747295227-49c90d68）：
        # 1 条 pending 候选（rule_candidate_list 正向）+ 1 条 active 空 scope 全局
        # 规则（rule_list / get_applicable_rules 正向，空 scope 匹配任意上下文）。
        conn.execute(
            """INSERT INTO agent_rule_candidates
               (id, title, rule_text, scope_json, severity, source, evidence_json,
                confidence, status, created_at, reviewed_at, reviewer, linked_rule_id)
               VALUES ('ARC-seed-1', 'seed candidate', 'do not use bare except',
                       '{}', 'warning', 'manual', '{}', 0.8, 'pending',
                       1000.0, NULL, '', '')"""
        )
        conn.execute(
            """INSERT INTO agent_rules
               (id, title, rule_text, scope_json, severity, status,
                source_candidate_id, evidence_json, created_at, updated_at,
                synced_to_agents_md, sync_hash)
               VALUES ('AR-seed-1', 'seed rule', 'use explicit error handling',
                       '{}', 'info', 'active', 'ARC-seed-1', '{}',
                       1000.0, 1000.0, 0, '')"""
        )
        conn.commit()
    finally:
        conn.close()
    return str(db_file)


def _find_daemon_binary():
    """定位 current-HEAD 构建的 cw-daemon 二进制（与 H4C-1 真实进程门同源）。"""
    candidates = [
        os.path.join("rust_ext", "target", "debug", "cw-daemon.exe"),
        os.path.join("rust_ext", "target", "debug", "cw-daemon"),
        os.environ.get("CW_DAEMON_BIN", ""),
        os.path.join("runtime", "current", "cw-daemon.exe"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def _wait_manifest(data_root, proc, timeout=10.0):
    """等待隔离 daemon 发布 authority-scoped manifest（仅接受 pid 匹配当前进程）。

    H6 修复（9d6ca63，2026-08-15）后 manifest 固定写 `USERPROFILE/.callwarden/`
    （http_manifest_dir），隔离 daemon 的 USERPROFILE = data_root/userhome，
    故轮询 data_root/userhome/.callwarden；data_root 根目录不再有 manifest。
    """
    manifest_dir = os.path.join(data_root, "userhome", ".callwarden")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        if os.path.isdir(manifest_dir):
            for f in os.listdir(manifest_dir):
                if f.startswith("http-daemon.") and f.endswith(".manifest.json"):
                    p = os.path.join(manifest_dir, f)
                    try:
                        m = json.loads(open(p, encoding="utf-8").read())
                    except (OSError, ValueError):
                        continue
                    if m.get("pid") == proc.pid:
                        return m
        time.sleep(0.2)
    return None


def _terminate(proc):
    """终止 daemon 进程（terminate 优先，兜底 kill）。"""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _spawn_isolated_daemon(bin_path, data_root, http_bind):
    """启动隔离 daemon（临时 task DB / registry / 管道 / USERPROFILE）。"""
    env = os.environ.copy()
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_SOCKET"] = os.path.join(data_root, "pipe")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    # compat worker 使用与 daemon 同版本的 Python 解释器
    env["CW_COMPAT_PYTHON"] = sys.executable
    home_dir = Path(data_root) / "userhome"
    home_dir.mkdir(parents=True, exist_ok=True)
    # H6：manifest 固定写 USERPROFILE/.callwarden，须先建目录否则 daemon 发布失败
    (home_dir / ".callwarden").mkdir(parents=True, exist_ok=True)
    env["USERPROFILE"] = str(home_dir)
    proc = subprocess.Popen(
        [bin_path, "--http-bind=" + http_bind],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


class TestRealDaemonCombinedWorkerCutover:
    """真实进程门：隔离 daemon + 生产 HttpDaemonRpcClient 覆盖合并接入。

    覆盖（派发单步骤#3）：
    - 符号组正向：get_top_callers 经 worker 返回种子库空调用数据（空列表）
      （W3-3 T-1786861820151-deb64c48：get_semgrep_findings 已迁移
      rust_native 不再走 worker，minimal 种子库无其他符号组方法数据源，
      正向改用仍走 worker 的 get_top_callers 验证符号组 worker 路由）；
    - 任务组正向：task_plan_template 经 worker 返回模板字符串
      （W2-2 T-1786840097330-a9e0ec69：get_job_stats 已迁移 rust_native，
      正向改用仍走 worker 的 list_jobs；
      W3-2 T-1786861820151-f3cecf40：list_jobs 已迁移 rust_native，
      正向改用仍走 worker 的 task_plan_template）；
    - 负向：未知方法 → method_not_found 结构化错误（绝不泄漏为成功）。
    任一断言失败即整体失败；不得 skip。
    """

    @pytest.fixture
    def daemon_bin(self):
        bin_path = _find_daemon_binary()
        if bin_path is None:
            pytest.fail(
                "cw-daemon 二进制不可用（H4C-2+3 真实进程门不得 skip："
                "需先 cargo build --manifest-path rust_ext/Cargo.toml --bin cw-daemon）"
            )
        return bin_path

    def _spawn_with_client(self, daemon_bin, tmp_path):
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        root_path = str(tmp_path / "seed-repo")
        _seed_combined_db(Path(data_root) / "userhome", root_path)
        proc = _spawn_isolated_daemon(daemon_bin, data_root, "127.0.0.1:0")
        manifest = _wait_manifest(data_root, proc)
        if manifest is None:
            _terminate(proc)
            pytest.fail("隔离 daemon 未发布 manifest")
        client = HttpDaemonRpcClient(
            endpoint=manifest["endpoint"],
            verify_health=False,
            timeout=5.0,
        )
        # 整改 4：manifest 只代表 HTTP 层就绪，compat worker（Python 子进程 +
        # 装配导入 tools_query/tools_task）首次调用才 spawn，冷启动可能超过
        # client.timeout（5s）→ E_HTTP_REQUEST_TIMEOUT。这里对 retryable 超时
        # 统一预热重试（最多 2 次），根治 flaky；非超时错误立即上抛不掩盖。
        self._wait_worker_ready(proc, client)
        return proc, client

    def _wait_worker_ready(self, proc, client, retries: int = 2):
        """整改 4：worker 冷启动就绪等待（flaky 根治，无 sleep 兜底）。

        用仍走 worker 的只读方法 task_plan_template 做预热调用，触发
        worker spawn；对 retryable 的 E_HTTP_REQUEST_TIMEOUT 统一重试 retries
        次，每次重新走完整 HTTP 请求（有界重试，非固定 sleep）；非超时错误上抛。
        （W3-3：get_semgrep_findings 已迁移 rust_native 不再走 worker，
        预热改用纯模板返回、无表依赖的 task_plan_template。）
        """
        last_err = None
        for _ in range(retries + 1):
            try:
                client.call(
                    "task_plan_template",
                    {"workspace_id": 1, "deadline_ms": 10000},
                )
                return
            except DaemonUnavailableError as e:
                if E_HTTP_REQUEST_TIMEOUT not in str(e):
                    raise
                last_err = e
            except DaemonRemoteError:
                raise
        _terminate(proc)
        pytest.fail(
            f"compat worker 冷启动就绪超时（预热重试 {retries} 次仍超时）: {last_err}"
        )

    def test_symbol_group_worker_positive(self, daemon_bin, tmp_path):
        """符号组正向：get_top_callers 经 worker 返回种子库空调用数据（空列表）。

        W3-3（T-1786861820151-deb64c48）：get_semgrep_findings 已迁移
        rust_native（不再走 compat worker），minimal 种子库无其他符号组方法
        数据源（call_versions 等为空表），本用例改用同组仍走 worker 的
        get_top_callers 验证符号组 worker 正向路径：经 worker 路由成功返回
        空列表（非 method_not_found，非业务错误）。
        """
        proc, client = self._spawn_with_client(daemon_bin, tmp_path)
        try:
            result = client.call(
                "get_top_callers",
                {"workspace_id": 1, "limit": 20, "deadline_ms": 10000},
            )
            assert result is not None
            assert isinstance(result, list) and len(result) == 0, (
                f"符号组正向应经 worker 返回种子库空调用数据: {result}"
            )
        finally:
            _terminate(proc)

    def test_task_group_worker_positive(self, daemon_bin, tmp_path):
        """任务组正向：task_plan_template 经 worker 返回计划模板字符串。

        W2-2（T-1786840097330-a9e0ec69）：get_job_stats 已迁移 rust_native，
        任务组正向改用仍走 worker 的 list_jobs；
        W3-2（T-1786861820151-f3cecf40）：list_jobs 已迁移 rust_native，
        任务组正向改用仍走 worker 的 task_plan_template（纯模板返回、
        无表依赖，worker 端 _h_task_plan_template 必然可服务）。
        """
        proc, client = self._spawn_with_client(daemon_bin, tmp_path)
        try:
            result = client.call(
                "task_plan_template",
                {"workspace_id": 1, "deadline_ms": 10000},
            )
            assert isinstance(result, str) and "Root task title" in result, (
                f"任务组正向应经 worker 返回计划模板字符串: {result!r}"
            )
        finally:
            _terminate(proc)

    def test_unknown_method_negative_method_not_found(self, daemon_bin, tmp_path):
        """负向：未知方法 → method_not_found 结构化错误（fail-closed，不泄漏成功）。"""
        proc, client = self._spawn_with_client(daemon_bin, tmp_path)
        try:
            with pytest.raises(DaemonRemoteError) as ei:
                client.call("no.such.tool", {"workspace_id": 1})
            assert ei.value.code == "method_not_found", (
                f"未知方法应返回 method_not_found，实际 {ei.value.code}: {ei.value.message}"
            )
        finally:
            _terminate(proc)

    def test_security_rules_group_worker_positive(self, daemon_bin, tmp_path):
        """H4C-2 第三批：security/rules 组正向经 worker 返回种子库真实数据。

        - list_branches → list_branch_workspaces（workspaces 表 + symbols 子查询）；
        - list_toolchains → db_toolchain.list_toolchains（toolchains 表，handler
          跳过 init_toolchain_schema 写操作，验证种子库预建表路径）；
        - rule_candidate_list / rule_list / get_applicable_rules（整改
          T-1786747295227-49c90d68）：db_agent_rules 纯 SELECT 正向返回种子库
          候选/规则真实数据。
        """
        proc, client = self._spawn_with_client(daemon_bin, tmp_path)
        try:
            branches = client.call(
                "list_branches", {"workspace_id": 1, "deadline_ms": 10000}
            )
            assert branches is not None
            assert len(branches) == 1, f"security 组正向应返回种子库 workspaces: {branches}"
            assert branches[0]["name"] == "seed-repo"
            assert branches[0]["is_active"] == 1
            assert branches[0]["symbol_count"] == 0

            tcs = client.call(
                "list_toolchains", {"workspace_id": 1, "deadline_ms": 10000}
            )
            assert tcs is not None
            assert len(tcs) == 1, f"rules 组正向应返回种子库 toolchains: {tcs}"
            assert tcs[0]["name"] == "gcc-12"
            assert tcs[0]["compiler_type"] == "gcc"

            # 整改（T-1786747295227-49c90d68）：3 个规则查询只读方法正向
            cands = client.call(
                "rule_candidate_list",
                {"workspace_id": 1, "status": "pending", "limit": 50, "deadline_ms": 10000},
            )
            assert cands is not None
            assert cands["count"] == 1, f"rule_candidate_list 应返回种子库候选: {cands}"
            assert cands["candidates"][0]["id"] == "ARC-seed-1"

            rules = client.call(
                "rule_list",
                {"workspace_id": 1, "status": "active", "limit": 100, "deadline_ms": 10000},
            )
            assert rules is not None
            assert rules["count"] == 1, f"rule_list 应返回种子库规则: {rules}"
            assert rules["rules"][0]["id"] == "AR-seed-1"

            applicable = client.call(
                "get_applicable_rules",
                {"workspace_id": 1, "context": {"language": "python"}, "limit": 10, "deadline_ms": 10000},
            )
            assert applicable is not None
            assert applicable["count"] == 1, (
                f"get_applicable_rules 空 scope 全局规则应匹配任意上下文: {applicable}"
            )
            assert applicable["rules"][0]["id"] == "AR-seed-1"
            assert applicable["rules"][0]["matched_scope"] == ["global"]
        finally:
            _terminate(proc)


# ============================================================
# 3. 工具层路由接入（整改 3：堵住"注册完成但工具函数未接入"的绕过缺口）
# ============================================================
# 上面真实进程门走 HttpDaemonRpcClient.call 直调，无法发现工具函数体仍裸
# get_db() 的绕过（BLOCKED 实证 1-4 根因）。本组用例经 MCP 工具函数入口调用：
# mock is_http_transport_enabled()=True + mock rpc client 返回 worker 结果，
# 断言 ① 工具函数返回 worker 数据 ② get_db 未被调用（monkeypatch 抛错桩，
# 调用成功即证明未走本地 SQLite）③ HTTP 模式对未注册方法 fail-closed 返回
# E_HTTP_COMPAT_UNSUPPORTED 结构化错误。


def _register_tools(module, mcp=None):
    """注册工具模块到 mock MCP，返回 {name: fn} 字典（H4B-E 同款）。"""
    if mcp is None:
        mcp = MagicMock()
    registrations = {}

    def tool_capture(name=None):
        def decorator(fn):
            registrations[fn.__name__] = fn
            return fn
        return decorator

    mcp.tool = tool_capture
    module.register(mcp)
    return registrations


@pytest.fixture
def mock_http_worker_route(monkeypatch):
    """HTTP 模式 + mock rpc client 的 route_worker_call 基座（与
    test_http_compat_worker_batch.mock_route_env 同款，此处用于工具层入口）。
    """

    def _apply(mode="auto"):
        client = MagicMock()
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.get_daemon_mode",
            lambda: mode,
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client._get_rpc_client_for_route",
            lambda: client,
        )
        return client

    return _apply


class TestToolLayerWorkerRouting:
    """整改 3：经 MCP 工具函数入口验证工具层已接入 worker（非裸 get_db）。"""

    def test_tool_layer_returns_worker_data(self, mock_http_worker_route):
        """① HTTP 模式工具函数返回 worker 数据（经 route_worker_call，不落本地）。

        W2-1（T-1786840097330-dec66710）：get_semgrep_stats 已迁移 rust_native
        （HTTP 分支直连便捷方法）；
        W3-3（T-1786861820151-deb64c48）：get_semgrep_findings 已迁移
        rust_native（HTTP 分支直连便捷方法），本用例改用同模块仍走 compat
        worker 的 find_issues（python_compat，HTTP 模式经 route_worker_call →
        client.call），验证对象仍是「工具层已接入 worker 路由」。
        """
        import callwarden.server.tools.tools_query as tools_query

        client = mock_http_worker_route()
        expected = [{"qualified_name": "app.run", "issue_type": "missing_comment"}]
        client.call.return_value = expected

        tools = _register_tools(tools_query)
        result = tools["find_issues"](issue_type="missing_comment")

        client.call.assert_called_once_with(
            "find_issues",
            {"issue_type": "missing_comment", "limit": 30},
        )
        assert result == expected

    def test_tool_layer_does_not_call_get_db(self, mock_http_worker_route, monkeypatch):
        """② HTTP 模式 get_db 不被调用：monkeypatch 为抛错桩，调用成功即证明
        未走本地 SQLite（若工具函数仍裸 get_db()，会先于 mock client 命中抛错）。

        W2-2（T-1786840097330-a9e0ec69）：get_job_stats 已迁移 rust_native
        （HTTP 分支直连 client.get_job_stats，不经 route_worker_call）；
        W3-2（T-1786861820151-f3cecf40）：get_job_status 已迁移 rust_native；
        W4-3（T-1786886251769-22b94ee8-sub-3）：get_defect_correlation 已迁移
        rust_native（HTTP 分支直连 client.get_defect_correlation），本用例改用
        仍走 compat worker 的 get_symbol_change_tasks（python_compat，HTTP 模式
        经 route_worker_call → client.call），验证对象仍是「工具层已接入 worker」。
        """
        import callwarden.server.tools.tools_task as tools_task

        client = mock_http_worker_route()
        expected = [{"symbol_hash": "h1", "change_count": 2}]
        client.call.return_value = expected

        def _boom(*args, **kwargs):
            raise AssertionError("HTTP 模式不应调用 get_db（本地 SQLite 被绕过）")

        monkeypatch.setattr(tools_task, "get_db", _boom)

        tools = _register_tools(tools_task)
        result = tools["get_symbol_change_tasks"](symbol_hash="h1")

        client.call.assert_called_once_with(
            "get_symbol_change_tasks",
            {"symbol_hash": "h1", "qualified_name": "", "limit": 50},
        )
        assert result == expected

    def test_tool_layer_task_plan_template_routed(self, mock_http_worker_route):
        """整改 2 回归：task_plan_template 经 worker 执行，非 _http_unsupported。"""
        import callwarden.server.tools.tools_task as tools_task

        client = mock_http_worker_route()
        expected = "## 任务计划模板（示例）"
        client.call.return_value = expected

        tools = _register_tools(tools_task)
        result = tools["task_plan_template"]()

        client.call.assert_called_once_with("task_plan_template", {})
        assert result == expected

    @pytest.mark.parametrize(
        "tool_name,call_kwargs,client_method,expect_kwargs,expected",
        [
            pytest.param(
                "get_uncommented_symbols",
                {"kind": "fn", "module_filter": "src", "limit": 10},
                "get_uncommented_symbols",
                {"kind": "fn", "module_filter": "src", "limit": 10,
                 "db_path": "/tmp/w2_1.db"},
                [{"rel_path": "a.py", "name": "f", "kind": "fn", "start_line": 1}],
                id="get_uncommented_symbols",
            ),
            pytest.param(
                "get_module_call_stats",
                {"limit": 10},
                "get_module_call_stats",
                {"limit": 10, "db_path": "/tmp/w2_1.db"},
                [{"caller_module": "a", "callee_module": "b", "call_count": 3}],
                id="get_module_call_stats",
            ),
            pytest.param(
                "get_semgrep_stats",
                {},
                "get_semgrep_stats",
                {"db_path": "/tmp/w2_1.db"},
                {"total_findings": 1, "by_severity": {"ERROR": 1}},
                id="get_semgrep_stats",
            ),
        ],
    )
    def test_tool_layer_migrated_native_uses_convenience_methods(
        self, monkeypatch, tool_name, call_kwargs, client_method, expect_kwargs,
        expected,
    ):
        """W2-1 复审整改：三工具已迁移 rust_native，HTTP 模式直连便捷方法。

        原用例 patch _get_rpc_client_for_route（compat worker 路由）已失效——
        HTTP 模式下三工具直接走 tools_query._get_daemon_client()（来自
        _mcp_common）返回 HttpDaemonRpcClient 的便捷方法（get_uncommented_symbols
        / get_module_call_stats / get_semgrep_stats，注入权威 workspace_instance_id）。
        本用例断言工具层入口按便捷方法签名透传参数（含 db_path）。
        """
        import callwarden.server.tools.tools_query as tools_query

        client = MagicMock()
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query.is_http_transport_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query._get_daemon_client",
            lambda: client,
        )
        monkeypatch.setattr(
            "callwarden.server.tools.tools_query._get_db_path_for_daemon",
            lambda: "/tmp/w2_1.db",
        )
        mock_method = getattr(client, client_method)
        mock_method.return_value = expected

        tools = _register_tools(tools_query)
        result = tools[tool_name](**call_kwargs)

        mock_method.assert_called_once_with(**expect_kwargs)
        assert result == expected

    def test_tool_layer_summary_routed(self, mock_http_worker_route, monkeypatch):
        """H4C-2 第二批：摘要/演化组 read_only 工具经 worker 执行（get_db 不触碰）。

        HTTP 模式 monkeypatch get_db 为抛错桩：若工具函数仍裸 get_db()，会先于
        mock client 命中抛错桩 → 用例失败；成功返回 mock worker 数据即证明
        route_worker_call 已接入。
        """
        import callwarden.server.tools.tools_summary as tools_summary

        client = mock_http_worker_route()
        expected = {"summary": "test summary", "version": 1}
        client.call.return_value = expected

        def _boom(*args, **kwargs):
            raise AssertionError("HTTP 模式不应调用 get_db（摘要组本地 SQLite 被绕过）")

        monkeypatch.setattr(tools_summary, "get_db", _boom)

        tools = _register_tools(tools_summary)
        result = tools["get_summary"](qualified_name="mod.fn")

        client.call.assert_called_once_with(
            "get_summary", {"qualified_name": "mod.fn"}
        )
        assert result == expected

    def test_tool_layer_guardrail_check_edit_routed(self, mock_http_worker_route, monkeypatch):
        """H4C-2 第二批：guardrail_check_edit（try-except 型）经 worker 执行。"""
        import callwarden.server.tools.tools_summary as tools_summary

        client = mock_http_worker_route()
        expected = {"decision": "pass", "findings": [], "message": "ok"}
        client.call.return_value = expected

        def _boom(*args, **kwargs):
            raise AssertionError("HTTP 模式不应调用 get_db（护栏组本地 SQLite 被绕过）")

        monkeypatch.setattr(tools_summary, "get_db", _boom)

        tools = _register_tools(tools_summary)
        result = tools["guardrail_check_edit"](file_path="src/a.py", proposed_change="x")

        client.call.assert_called_once_with(
            "guardrail_check_edit", {"file_path": "src/a.py", "proposed_change": "x"}
        )
        assert result == expected

    def test_tool_layer_semantic_routed(self, mock_http_worker_route, monkeypatch):
        """H4C-2 第二批：语义组 read_only 工具经 worker 执行（get_db 不触碰）。"""
        import callwarden.server.tools.tools_semantic as tools_semantic

        client = mock_http_worker_route()
        expected = [{"qualified_name": "mod.fn", "score": 0.9}]
        client.call.return_value = expected

        def _boom(*args, **kwargs):
            raise AssertionError("HTTP 模式不应调用 get_db（语义组本地 SQLite 被绕过）")

        monkeypatch.setattr(tools_semantic, "get_db", _boom)

        tools = _register_tools(tools_semantic)
        result = tools["semantic_search"](query="auth", top_k=3)

        client.call.assert_called_once_with(
            "semantic_search", {"query": "auth", "top_k": 3}
        )
        assert result == expected

    def test_tool_layer_parse_codeowners_routed(self, mock_http_worker_route, monkeypatch):
        """H4C-2 第二批：parse_codeowners（try-except 型）经 worker 执行。"""
        import callwarden.server.tools.tools_semantic as tools_semantic

        client = mock_http_worker_route()
        expected = [{"pattern": "*.py", "owners": ["@team"]}]
        client.call.return_value = expected

        def _boom(*args, **kwargs):
            raise AssertionError("HTTP 模式不应调用 get_db（语义组本地 SQLite 被绕过）")

        monkeypatch.setattr(tools_semantic, "get_db", _boom)

        tools = _register_tools(tools_semantic)
        result = tools["parse_codeowners"](file_path="CODEOWNERS")

        client.call.assert_called_once_with(
            "parse_codeowners", {"file_path": "CODEOWNERS"}
        )
        assert result == expected

    def test_tool_layer_security_routed(self, mock_http_worker_route, monkeypatch):
        """H4C-2 第三批：security 组 list_branches 经 worker 执行（get_db 不触碰）。"""
        import callwarden.server.tools.tools_security as tools_security

        client = mock_http_worker_route()
        expected = [{"workspace_id": 1, "name": "main", "root_path": "/repo", "is_active": 1}]
        client.call.return_value = expected

        def _boom(*args, **kwargs):
            raise AssertionError("HTTP 模式不应调用 get_db（security 组本地 SQLite 被绕过）")

        monkeypatch.setattr(tools_security, "get_db", _boom)

        tools = _register_tools(tools_security)
        result = tools["list_branches"]()

        client.call.assert_called_once_with("list_branches", {})
        assert result == expected

    def test_tool_layer_rules_routed(self, mock_http_worker_route, monkeypatch):
        """H4C-2 第三批：rules 组 list_toolchains 经 worker 执行（get_db 不触碰）。"""
        import callwarden.server.tools.tools_rules as tools_rules

        client = mock_http_worker_route()
        expected = [{"id": 1, "name": "gcc-12", "compiler_type": "gcc"}]
        client.call.return_value = expected

        def _boom(*args, **kwargs):
            raise AssertionError("HTTP 模式不应调用 get_db（rules 组本地 SQLite 被绕过）")

        monkeypatch.setattr(tools_rules, "get_db", _boom)

        tools = _register_tools(tools_rules)
        result = tools["list_toolchains"]()

        client.call.assert_called_once_with("list_toolchains", {})
        assert result == expected

    def test_tool_layer_security_rules_readonly_routed(self, mock_http_worker_route, monkeypatch):
        """整改（T-1786747295227-49c90d68）：security 组 3 个规则查询只读工具
        （rule_candidate_list / rule_list / get_applicable_rules）经 worker 执行
        （get_db 不触碰，HTTP 模式不再 fail-closed）。"""
        import callwarden.server.tools.tools_security as tools_security

        client = mock_http_worker_route()
        expected = {
            "candidates": [{"id": "ARC-1", "title": "t"}],
            "count": 1,
        }
        client.call.return_value = expected

        def _boom(*args, **kwargs):
            raise AssertionError("HTTP 模式不应调用 get_db（security 规则查询本地 SQLite 被绕过）")

        monkeypatch.setattr(tools_security, "get_db", _boom)

        tools = _register_tools(tools_security)
        result = tools["rule_candidate_list"](status="pending", limit=50)

        client.call.assert_called_once_with(
            "rule_candidate_list", {"status": "pending", "limit": 50}
        )
        assert result == expected

        # rule_list：同样经 worker 路由（reset 后再断言，避免累计调用次数）
        expected_rules = {"rules": [{"id": "AR-1", "title": "r"}], "count": 1}
        client.call.return_value = expected_rules
        client.call.reset_mock()
        result = tools["rule_list"](status="active", limit=100)
        client.call.assert_called_once_with(
            "rule_list", {"status": "active", "limit": 100}
        )
        assert result == expected_rules

        # get_applicable_rules：context dict 原样透传
        ctx = {"language": "python"}
        expected_applicable = {"rules": [{"id": "AR-1", "matched_scope": ["language:python"]}], "count": 1}
        client.call.return_value = expected_applicable
        client.call.reset_mock()
        result = tools["get_applicable_rules"](context=ctx, limit=10)
        client.call.assert_called_once_with(
            "get_applicable_rules", {"context": ctx, "limit": 10}
        )
        assert result == expected_applicable

    def test_tool_layer_get_metrics_fail_closed(self, mock_http_worker_route):
        """H4C-2 第三批：get_metrics 未注册 worker handler → fail-closed 负向。

        get_metrics 不依赖 db.conn（走 daemon RPC / 本地 MetricsCollector），
        不接入 worker；HTTP 模式经工具函数入口必须返回 E_HTTP_COMPAT_UNSUPPORTED。
        """
        import callwarden.server.tools.tools_rules as tools_rules

        mock_http_worker_route()

        tools = _register_tools(tools_rules)
        result = tools["get_metrics"]()

        assert isinstance(result, dict)
        assert result.get("error") == "E_HTTP_COMPAT_UNSUPPORTED", (
            f"get_metrics HTTP 模式应 fail-closed: {result}"
        )
        assert result.get("tool") == "get_metrics"
        assert result.get("backend") == "python_compat"

    def test_tool_layer_unregistered_method_fail_closed(self, mock_http_worker_route):
        """③ HTTP 模式未注册方法 → E_HTTP_COMPAT_UNSUPPORTED（fail-closed 负向）。

        写语义/治理工具（task_create 等）不注册 worker handler，route_worker_call
        白名单前置检查必须拦截，不得泄漏 method_not_found 或降级本地 SQLite。
        """
        mock_http_worker_route()
        result = route_worker_call("task_create", {}, lambda: {"fallback": True})
        assert result.get("error") == "E_HTTP_COMPAT_UNSUPPORTED"
        assert result.get("tool") == "task_create"
        assert result.get("backend") == "python_compat"
        assert "method_not_found" not in str(result)


# ============================================================
# 4. H4C-4 写语义工具 fail-closed（T-1786745594007-b2d57524）
# ============================================================
# 矩阵交叉核验：80 个符号/任务工具中 13 个写语义工具 daemon_rpc_method=none，
# 无 worker/route_task_write 可接入，唯一合规整改是 _http_unsupported fail-closed。
# 本组用例 mock is_http_transport_enabled()=True + monkeypatch get_db 为抛错桩，
# 经工具函数入口断言：返回 E_HTTP_COMPAT_UNSUPPORTED 且 get_db 未被调用
# （若工具函数仍裸 get_db()，会在返回结构化错误前命中抛错桩 → 用例失败）。


class TestToolLayerWriteSemanticsFailClosed:
    """H4C-4：13 个写语义工具 HTTP 模式 fail-closed（经工具层入口，非直调 daemon）。"""

    @pytest.mark.parametrize(
        "module_name, tool_name, kwargs",
        HTTP_UNSUPPORTED_WRITE_TOOLS,
        ids=[f"{m}.{t}" for m, t, _ in HTTP_UNSUPPORTED_WRITE_TOOLS],
    )
    def test_http_fail_closed(
        self,
        mock_http_worker_route,
        monkeypatch,
        module_name,
        tool_name,
        kwargs,
    ):
        import callwarden.server.tools.tools_query as tools_query
        import callwarden.server.tools.tools_task as tools_task
        import callwarden.server.tools.tools_summary as tools_summary
        import callwarden.server.tools.tools_semantic as tools_semantic
        import callwarden.server.tools.tools_security as tools_security
        import callwarden.server.tools.tools_rules as tools_rules
        import callwarden.server.tools.tools_collab as tools_collab
        import callwarden.server.tools.tools_p2_graph as tools_p2_graph
        import callwarden.server.tools.tools_p3_identity as tools_p3_identity
        import callwarden.server.tools.tools_p4_lease as tools_p4_lease

        mod = {
            "tools_query": tools_query,
            "tools_task": tools_task,
            "tools_summary": tools_summary,
            "tools_semantic": tools_semantic,
            "tools_security": tools_security,
            "tools_rules": tools_rules,
            "tools_collab": tools_collab,
            "tools_p2_graph": tools_p2_graph,
            "tools_p3_identity": tools_p3_identity,
            "tools_p4_lease": tools_p4_lease,
        }[module_name]
        client = mock_http_worker_route()

        def _boom(*args, **kwargs):
            raise AssertionError(
                f"HTTP 模式不应调用 get_db（{tool_name} 本地 SQLite 被绕过）"
            )

        monkeypatch.setattr(mod, "get_db", _boom)

        tools = _register_tools(mod)
        result = tools[tool_name](**kwargs)

        assert isinstance(result, dict), (
            f"{tool_name} HTTP 模式应返回结构化错误 dict，实际 {type(result)}"
        )
        assert result.get("error") == "E_HTTP_COMPAT_UNSUPPORTED", (
            f"{tool_name} HTTP 模式应 fail-closed 返回 E_HTTP_COMPAT_UNSUPPORTED: {result}"
        )
        assert result.get("tool") == tool_name
        assert result.get("backend") == "python_compat"
        # fail-closed 短路于 _http_unsupported，不得进入 route_worker_call
        client.call.assert_not_called()
