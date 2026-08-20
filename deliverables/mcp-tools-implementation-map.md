# Call Warden MCP 工具实现方案盘点报告

> 生成时间：2026-08-20 06:18
> 数据来源：`server/tools/*.py`（MCP 注册）+ `rust_ext/src/daemon/dispatch.rs`（Rust handler）+ `rust_ext/src/daemon/http_server.rs`（capability registry / COMPAT_ROUTE_WHITELIST）

## 一、总量

| 口径 | 数量 | 说明 |
|---|---|---|
| 实际注册 MCP 工具 | **239** | 实际运行 `create_mcp_server()` 验证 |
| 文档记录（docs/mcp_tools.md / TOOLS.md） | 237 | 文档未收录 2 个新增 collab 写工具 |
| 差异工具 | 2 | `task_remediation_create`、`task_step_resolve`（P1 collab 写面，已注册但文档未同步） |

## 二、总体结论

| 方案 | 数量 | 占比 |
|---|---:|---:|
| **走 python client + rust daemon HTTP API** | **160** | **66.9%** |
| 其中：Rust 原生 handler 执行 | 81 | 33.9% |
| 其中：HTTP → daemon → Python worker 执行 | 79 | 33.1% |
| **传统方案（本地 SQL / legacy RPC）** | **79** | **33.1%** |
| 其中：HTTP 模式 fail-closed（仅 local/legacy 可用） | 61 | 25.5% |
| 其中：纯本地 SQL / 本地实现 | 18 | 7.5% |
| **合计** | **239** | **100%** |

## 三、五类实现方案说明

### HTTP-Rust 原生（59 个）

Python client → HttpDaemonRpcClient 便捷方法 → HTTP POST /v1/rpc → Rust 原生 handler（dispatch.rs 直连）

### HTTP-任务 RPC（22 个）

Python client → route_task_write/read → HTTP /v1/rpc → Rust task.* handler（daemon 权威写路径）

### HTTP-compat worker（79 个）

Python client → route_worker_call → HTTP /v1/rpc → daemon compat_route 白名单 → Python worker（H3 compat worker）执行

### 传统（HTTP 拒止）（61 个）

HTTP 模式下 fail-closed 拒绝（E_HTTP_COMPAT_UNSUPPORTED），local/legacy 模式走本地 SQL 或 UnixDaemonRpcClient

### 传统（纯本地 SQL）（18 个）

无 daemon 路由，直接 get_db() 本地 SQLite / 本地实现

## 四、逐模块工具 → 方案清单

> 每个工具列出：所属模块、MCP 工具名、实现方案、对应 daemon RPC method（若有）。

### Query（查询面）（32 个）

| MCP 工具 | 方案 | daemon RPC |
|---|---|---|
| `detect_cycles` | HTTP-Rust 原生 | `query.detect_cycles` |
| `export_module_graph` | HTTP-compat worker | `export_module_graph` |
| `find_issues` | HTTP-compat worker | `find_issues` |
| `get_call_chain_down` | HTTP-Rust 原生 | `query.call_chain_down` |
| `get_call_heatmap` | HTTP-compat worker | `get_call_heatmap` |
| `get_callees` | HTTP-Rust 原生 | `query.callees` |
| `get_callers` | HTTP-Rust 原生 | `query.callers` |
| `get_comment_coverage` | HTTP-compat worker | `get_comment_coverage` |
| `get_comment_from_version` | HTTP-compat worker | `get_comment_from_version` |
| `get_deepest_functions` | HTTP-compat worker | `get_deepest_functions` |
| `get_file_history` | HTTP-Rust 原生 | `query.file_history` |
| `get_file_symbols` | HTTP-Rust 原生 | `query.file` |
| `get_impact` | HTTP-compat worker | `get_impact` |
| `get_issue_summary` | HTTP-compat worker | `get_issue_summary` |
| `get_module_call_stats` | HTTP-Rust 原生 | `query.module_call_stats` |
| `get_orphan_symbols` | HTTP-compat worker | `get_orphan_symbols` |
| `get_recent_changes` | HTTP-compat worker | `get_recent_changes` |
| `get_semgrep_findings` | HTTP-Rust 原生 | `query.semgrep_findings` |
| `get_semgrep_stats` | HTTP-Rust 原生 | `query.semgrep_stats` |
| `get_stats` | HTTP-Rust 原生 | `query.stats` |
| `get_symbol` | HTTP-Rust 原生 | `query.symbol` |
| `get_symbol_history` | HTTP-compat worker | `get_symbol_history` |
| `get_symbol_location` | HTTP-Rust 原生 | `query.symbol_location` |
| `get_test_coverage` | HTTP-compat worker | `get_test_coverage` |
| `get_top_callers` | HTTP-compat worker | `get_top_callers` |
| `get_topological_order` | HTTP-Rust 原生 | `query.topological_order` |
| `get_uncommented_symbols` | HTTP-Rust 原生 | `query.uncommented_symbols` |
| `restore_all_comments` | 传统（HTTP 拒止） | `—` |
| `restore_comment` | 传统（HTTP 拒止） | `—` |
| `run_semgrep_scan` | 传统（HTTP 拒止） | `—` |
| `scan_semgrep_incremental` | 传统（HTTP 拒止） | `—` |
| `search_symbols` | HTTP-Rust 原生 | `query.search` |

### Workspace（工作区）（27 个）

| MCP 工具 | 方案 | daemon RPC |
|---|---|---|
| `build_directory` | 传统（纯本地 SQL） | `—` |
| `build_graph` | 传统（纯本地 SQL） | `—` |
| `check_file_health` | 传统（纯本地 SQL） | `—` |
| `delete_workspace` | HTTP-Rust 原生 | `workspace.remove` |
| `file_grep` | 传统（纯本地 SQL） | `—` |
| `file_list` | 传统（纯本地 SQL） | `—` |
| `file_read` | 传统（纯本地 SQL） | `—` |
| `file_symbol_content` | 传统（纯本地 SQL） | `—` |
| `get_active_workspace` | HTTP-Rust 原生 | `workspace.status` |
| `get_code_health_check` | 传统（纯本地 SQL） | `—` |
| `get_code_metrics_summary` | 传统（纯本地 SQL） | `—` |
| `get_commit_changes` | HTTP-Rust 原生 | `query.git_commit_changes` |
| `get_complexity_hotspots` | 传统（纯本地 SQL） | `—` |
| `get_coupling_analysis` | 传统（纯本地 SQL） | `—` |
| `get_function_metrics` | 传统（纯本地 SQL） | `—` |
| `get_git_commits` | HTTP-Rust 原生 | `query.git_commits` |
| `get_git_stats` | HTTP-Rust 原生 | `query.git_stats` |
| `get_largest_functions` | 传统（纯本地 SQL） | `—` |
| `get_most_coupled_functions` | 传统（纯本地 SQL） | `—` |
| `get_status` | 传统（纯本地 SQL） | `—` |
| `get_symbol_content_by_hash` | 传统（纯本地 SQL） | `—` |
| `import_git_history` | 传统（HTTP 拒止） | `—` |
| `list_workspaces` | HTTP-Rust 原生 | `workspace.list` |
| `refresh_file` | 传统（纯本地 SQL） | `—` |
| `register_workspace` | HTTP-Rust 原生 | `workspace.register` |
| `remove_file` | 传统（纯本地 SQL） | `—` |
| `set_active_workspace` | HTTP-Rust 原生 | `workspace.activate` |

### Semantic（语义）（19 个）

| MCP 工具 | 方案 | daemon RPC |
|---|---|---|
| `embed_single_symbol` | 传统（HTTP 拒止） | `—` |
| `embed_symbols` | 传统（HTTP 拒止） | `—` |
| `find_similar_functions` | HTTP-compat worker | `find_similar_functions` |
| `gc_archive_import` | 传统（HTTP 拒止） | `—` |
| `gc_archive_inspect` | 传统（HTTP 拒止） | `—` |
| `gc_archive_list` | 传统（HTTP 拒止） | `—` |
| `gc_audit_get` | 传统（HTTP 拒止） | `—` |
| `gc_audit_list` | 传统（HTTP 拒止） | `—` |
| `gc_policy_get` | 传统（HTTP 拒止） | `—` |
| `gc_policy_set` | 传统（HTTP 拒止） | `—` |
| `gc_retention` | 传统（HTTP 拒止） | `—` |
| `get_project_dependencies` | HTTP-compat worker | `get_project_dependencies` |
| `get_symbol_commit_history` | HTTP-compat worker | `get_symbol_commit_history` |
| `import_codeowners` | 传统（HTTP 拒止） | `—` |
| `import_git_blame` | 传统（HTTP 拒止） | `—` |
| `import_project_dependencies` | 传统（HTTP 拒止） | `—` |
| `parse_codeowners` | HTTP-compat worker | `parse_codeowners` |
| `prune_external_symbols` | 传统（HTTP 拒止） | `—` |
| `semantic_search` | HTTP-compat worker | `semantic_search` |

### Task（任务）（52 个）

| MCP 工具 | 方案 | daemon RPC |
|---|---|---|
| `audit_verify_chain` | HTTP-compat worker | `audit_verify_chain` |
| `bootstrap_status` | HTTP-compat worker | `bootstrap_status` |
| `cancel_job` | 传统（HTTP 拒止） | `—` |
| `cleanup_agent_rule_sync_log` | 传统（HTTP 拒止） | `—` |
| `clear_clones` | 传统（HTTP 拒止） | `—` |
| `detect_clones` | 传统（HTTP 拒止） | `—` |
| `detect_clones_async` | 传统（HTTP 拒止） | `—` |
| `embed_symbols_async` | 传统（HTTP 拒止） | `—` |
| `get_clone_group_detail` | HTTP-compat worker | `get_clone_group_detail` |
| `get_clone_group_stats` | HTTP-Rust 原生 | `task.clone_group_stats` |
| `get_clone_stats` | HTTP-Rust 原生 | `task.clone_stats` |
| `get_commit_tasks` | HTTP-Rust 原生 | `query.commit_tasks` |
| `get_defect_correlation` | HTTP-Rust 原生 | `query.get_defect_correlation` |
| `get_job_stats` | HTTP-Rust 原生 | `task.job_stats` |
| `get_job_status` | HTTP-Rust 原生 | `task.job_status` |
| `get_symbol_change_tasks` | HTTP-compat worker | `get_symbol_change_tasks` |
| `get_symbol_issues` | HTTP-Rust 原生 | `query.issues` |
| `get_task_commits` | HTTP-任务 RPC | `task.get_commits` |
| `get_task_symbol_changes` | HTTP-任务 RPC | `task.get_symbol_changes` |
| `get_test_cases` | HTTP-Rust 原生 | `query.tests` |
| `get_test_coverage_summary` | HTTP-Rust 原生 | `query.tests` |
| `get_test_stability` | HTTP-Rust 原生 | `query.tests` |
| `get_tested_functions` | HTTP-Rust 原生 | `query.tests` |
| `link_edit_audit_symbols` | HTTP-任务 RPC | `task.link_edit_audit_symbols` |
| `list_audit_signing_keys` | HTTP-compat worker | `list_audit_signing_keys` |
| `list_clone_groups` | HTTP-compat worker | `list_clone_groups` |
| `list_clones` | HTTP-compat worker | `list_clones` |
| `list_jobs` | HTTP-Rust 原生 | `task.list_jobs` |
| `record_task_symbol_change` | HTTP-任务 RPC | `task.record_symbol_change` |
| `rotate_audit_signing_key` | 传统（HTTP 拒止） | `—` |
| `rule_seed_bootstrap` | 传统（HTTP 拒止） | `—` |
| `semgrep_scan_async` | 传统（HTTP 拒止） | `—` |
| `task_apply` | HTTP-任务 RPC | `task.apply` |
| `task_capture_diff` | HTTP-任务 RPC | `task.capture_diff` |
| `task_close` | HTTP-任务 RPC | `task.close` |
| `task_completion_review` | HTTP-任务 RPC | `task.completion_review` |
| `task_create` | HTTP-任务 RPC | `task.create` |
| `task_create_from_plan` | HTTP-任务 RPC | `task.create_from_plan` |
| `task_create_subtask` | HTTP-任务 RPC | `task.create_subtask` |
| `task_list` | HTTP-任务 RPC | `task.list` |
| `task_next_step` | HTTP-任务 RPC | `task.claim` |
| `task_plan_template` | HTTP-compat worker | `task_plan_template` |
| `task_quality_findings` | HTTP-任务 RPC | `task.quality_findings` |
| `task_report_step` | HTTP-任务 RPC | `task.report` |
| `task_resolve_block` | HTTP-任务 RPC | `task.reopen` |
| `task_resolve_quality_finding` | HTTP-任务 RPC | `task.resolve_quality_finding` |
| `task_rollback` | HTTP-任务 RPC | `task.rollback` |
| `task_split` | HTTP-任务 RPC | `task.split` |
| `task_status` | HTTP-任务 RPC | `task.status` |
| `task_status_tree` | HTTP-任务 RPC | `task.status_tree` |
| `wait_for_job` | HTTP-Rust 原生 | `task.wait_for_job` |
| `work_next_job` | HTTP-任务 RPC | `task.work_next` |

### Summary（摘要）（31 个）

| MCP 工具 | 方案 | daemon RPC |
|---|---|---|
| `ask_codebase` | HTTP-compat worker | `ask_codebase` |
| `blast_radius` | HTTP-compat worker | `blast_radius` |
| `churn_analysis` | HTTP-Rust 原生 | `query.churn_analysis` |
| `cross_layer_impact` | HTTP-compat worker | `cross_layer_impact` |
| `defect_correlation` | HTTP-Rust 原生 | `query.defect_correlation` |
| `defect_learn` | HTTP-compat worker | `defect_learn` |
| `defect_search` | HTTP-Rust 原生 | `query.defect_search` |
| `defect_stats` | HTTP-Rust 原生 | `defect.stats` |
| `defect_suggest_fix` | HTTP-Rust 原生 | `query.defect_suggest_fix` |
| `diff_to_symbol` | HTTP-Rust 原生 | `query.diff_to_symbol` |
| `evolution_frequency` | HTTP-compat worker | `evolution_frequency` |
| `find_uncovered_functions` | HTTP-compat worker | `find_uncovered_functions` |
| `generate_summary` | 传统（HTTP 拒止） | `—` |
| `get_clone_aware_impact` | HTTP-compat worker | `get_clone_aware_impact` |
| `get_coverage_for_symbol` | HTTP-Rust 原生 | `query.coverage_for_symbol` |
| `get_ownership_map` | HTTP-compat worker | `get_ownership_map` |
| `get_summary` | HTTP-compat worker | `get_summary` |
| `get_token_savings_report` | HTTP-compat worker | `get_token_savings_report` |
| `get_vulnerability_blast_radius` | HTTP-compat worker | `get_vulnerability_blast_radius` |
| `guardrail_add_rule` | 传统（HTTP 拒止） | `—` |
| `guardrail_check_edit` | HTTP-compat worker | `guardrail_check_edit` |
| `guardrail_list_rules` | HTTP-compat worker | `guardrail_list_rules` |
| `guardrail_scan` | HTTP-compat worker | `guardrail_scan` |
| `hotspot_evolution` | HTTP-compat worker | `hotspot_evolution` |
| `import_coverage` | 传统（HTTP 拒止） | `—` |
| `project_brief` | HTTP-compat worker | `project_brief` |
| `record_token_savings` | 传统（HTTP 拒止） | `—` |
| `repo_map` | HTTP-compat worker | `repo_map` |
| `review_readiness` | HTTP-compat worker | `review_readiness` |
| `test_impact_selection` | HTTP-compat worker | `test_impact_selection` |
| `who_to_ask` | HTTP-compat worker | `who_to_ask` |

### Security（安全）（36 个）

| MCP 工具 | 方案 | daemon RPC |
|---|---|---|
| `compare_snapshots` | 传统（HTTP 拒止） | `—` |
| `cross_repo_impact` | HTTP-compat worker | `cross_repo_impact` |
| `cross_repo_summary` | HTTP-compat worker | `cross_repo_summary` |
| `detect_cross_repo_deps` | 传统（HTTP 拒止） | `—` |
| `diff_branches` | HTTP-Rust 原生 | `query.diff_branches` |
| `diff_callees` | 传统（HTTP 拒止） | `—` |
| `diff_callers` | 传统（HTTP 拒止） | `—` |
| `extract_rule_candidates_from_quality_findings` | 传统（HTTP 拒止） | `—` |
| `find_shared_symbols` | HTTP-compat worker | `find_shared_symbols` |
| `get_applicable_rules` | HTTP-compat worker | `get_applicable_rules` |
| `get_edit_history` | HTTP-compat worker | `get_edit_history` |
| `get_edit_stats` | HTTP-Rust 原生 | `edit.stats` |
| `list_branches` | HTTP-compat worker | `list_branches` |
| `lsp_check_available` | HTTP-compat worker | `lsp_check_available` |
| `lsp_completion` | HTTP-compat worker | `lsp_completion` |
| `lsp_definition` | HTTP-compat worker | `lsp_definition` |
| `lsp_diagnostics` | HTTP-compat worker | `lsp_diagnostics` |
| `lsp_hover` | HTTP-compat worker | `lsp_hover` |
| `lsp_references` | HTTP-compat worker | `lsp_references` |
| `merge_preview` | HTTP-compat worker | `merge_preview` |
| `propose_edit` | 传统（HTTP 拒止） | `—` |
| `propose_range_patch` | 传统（HTTP 拒止） | `—` |
| `propose_symbol_id_patch` | 传统（HTTP 拒止） | `—` |
| `propose_symbol_patch` | 传统（HTTP 拒止） | `—` |
| `register_branch` | 传统（HTTP 拒止） | `—` |
| `resolve_gate_findings` | 传统（HTTP 拒止） | `—` |
| `revert_edit` | 传统（HTTP 拒止） | `—` |
| `rule_candidate_accept` | 传统（HTTP 拒止） | `—` |
| `rule_candidate_create` | 传统（HTTP 拒止） | `—` |
| `rule_candidate_list` | HTTP-compat worker | `rule_candidate_list` |
| `rule_candidate_reject` | 传统（HTTP 拒止） | `—` |
| `rule_insert_agents_md_block` | 传统（HTTP 拒止） | `—` |
| `rule_list` | HTTP-compat worker | `rule_list` |
| `rule_sync_agents_md` | 传统（HTTP 拒止） | `—` |
| `run_check_gate` | 传统（HTTP 拒止） | `—` |
| `switch_branch` | 传统（HTTP 拒止） | `—` |

### Rules（规则）（9 个）

| MCP 工具 | 方案 | daemon RPC |
|---|---|---|
| `count_resolved_edges` | HTTP-Rust 原生 | `build_context.count_resolved_edges` |
| `get_active_build_context` | HTTP-Rust 原生 | `build_context.active` |
| `get_build_context` | HTTP-Rust 原生 | `build_context.get` |
| `get_metrics` | 传统（HTTP 拒止） | `—` |
| `get_resolved_edges` | HTTP-Rust 原生 | `build_context.resolved_edges` |
| `get_toolchain` | HTTP-compat worker | `get_toolchain` |
| `get_workspace_toolchains` | HTTP-compat worker | `get_workspace_toolchains` |
| `list_build_contexts` | HTTP-Rust 原生 | `build_context.list` |
| `list_toolchains` | HTTP-compat worker | `list_toolchains` |

### Collab（协同）（8 个）

| MCP 工具 | 方案 | daemon RPC |
|---|---|---|
| `append_evidence` | HTTP-Rust 原生 | `evidence.append` |
| `find_evidence` | HTTP-compat worker | `find_evidence` |
| `get_freshness_status` | HTTP-compat worker | `get_freshness_status` |
| `get_gate_decision` | HTTP-compat worker | `get_gate_decision` |
| `get_role_view` | HTTP-compat worker | `get_role_view` |
| `submit_verdict` | HTTP-Rust 原生 | `verdict.submit` |
| `task_remediation_create` | HTTP-Rust 原生 | `task.remediation.create` |
| `task_step_resolve` | HTTP-Rust 原生 | `task.step.resolve` |

### P2 依赖图（10 个）

| MCP 工具 | 方案 | daemon RPC |
|---|---|---|
| `build_hard_dependency_edges` | 传统（HTTP 拒止） | `—` |
| `detect_cycle` | HTTP-compat worker | `detect_cycle` |
| `get_artifact_freshness` | HTTP-compat worker | `get_artifact_freshness` |
| `get_dependency_edges` | HTTP-compat worker | `get_dependency_edges` |
| `get_interface_providers` | HTTP-compat worker | `get_interface_providers` |
| `import_envelope_dependencies` | 传统（HTTP 拒止） | `—` |
| `publish_interface` | 传统（HTTP 拒止） | `—` |
| `record_artifact_identity` | 传统（HTTP 拒止） | `—` |
| `select_interface_provider` | 传统（HTTP 拒止） | `—` |
| `validate_revision_dependencies` | HTTP-compat worker | `validate_revision_dependencies` |

### P3 身份（7 个）

| MCP 工具 | 方案 | daemon RPC |
|---|---|---|
| `check_action_identity` | HTTP-compat worker | `check_action_identity` |
| `check_session_separation` | HTTP-compat worker | `check_session_separation` |
| `get_action_identity` | HTTP-compat worker | `get_action_identity` |
| `get_attestation_validity` | HTTP-compat worker | `get_attestation_validity` |
| `list_attestation_revocations` | HTTP-compat worker | `list_attestation_revocations` |
| `record_action_identity` | 传统（HTTP 拒止） | `—` |
| `register_attestation_revocation` | 传统（HTTP 拒止） | `—` |

### P4 Lease/Assignment（8 个）

| MCP 工具 | 方案 | daemon RPC |
|---|---|---|
| `assignment_create` | 传统（HTTP 拒止） | `—` |
| `assignment_revoke` | 传统（HTTP 拒止） | `—` |
| `assignment_show` | HTTP-compat worker | `assignment_show` |
| `lease_acquire` | HTTP-Rust 原生 | `lease.acquire` |
| `lease_list_events` | HTTP-Rust 原生 | `lease.list_events` |
| `lease_release` | HTTP-Rust 原生 | `lease.release` |
| `lease_renew` | HTTP-Rust 原生 | `lease.renew` |
| `lease_status` | HTTP-Rust 原生 | `lease.status` |
