# Call Warden MCP-001→MCP-070 + INT-001 + P0 系列逐任务核实报告

**日期：** 2026-08-26　**范围：** 126 个任务（MCP-001~070 × 70 + INT-001 + P0 系列 × 55）
**结论：`MISSING = 0`** —— 与 CLI/SRV 系列一致，所有任务的代码均在 master。

## MCP 系列（70 个工具）

| 任务 | 状态 | 工具名 | Python权威 | Rust daemon | 判定 |
|---|---|---|---|---|---|
| MCP-001 | review | get_role_view | 是 | 是 | 双在 |
| MCP-002 | review | find_evidence | 是 | 是 | 双在 |
| MCP-003 | review | get_freshness_status | 是 | 是 | 双在 |
| MCP-004 | closed | get_gate_decision | 是 | 是 | 双在 |
| MCP-005 | closed | get_artifact_freshness | 是 | 是 | 双在 |
| MCP-006 | closed | get_interface_providers | 是 | 是 | 双在 |
| MCP-007 | closed | detect_cycle | 是 | 是 | 双在 |
| MCP-008 | closed | validate_revision_dependencies | 是 | 是 | 双在 |
| MCP-009 | closed | get_dependency_edges | 是 | 是 | 双在 |
| MCP-010 | closed | get_action_identity | 是 | 是 | 双在 |
| MCP-011 | closed | check_action_identity | 是 | 是 | 双在 |
| MCP-012 | closed | check_session_separation | 是 | 是 | 双在 |
| MCP-013 | closed | get_attestation_validity | 是 | 是 | 双在 |
| MCP-014 | closed | list_attestation_revocations | 是 | 是 | 双在 |
| MCP-015 | closed | assignment_show | 是 | 是 | 双在 |
| MCP-016 | closed | get_symbol_history | 是 | 是 | 双在 |
| MCP-017 | review | get_recent_changes | 是 | 是 | 双在 |
| MCP-018 | review | get_impact | 是 | 是 | 双在 |
| MCP-019 | review | get_comment_from_version | 是 | 是 | 双在 |
| MCP-020 | review | get_issue_summary | 是 | 是 | 双在 |
| MCP-021 | review | find_issues | 是 | 是 | 双在 |
| MCP-022 | review | get_test_coverage | 是 | 是 | 双在 |
| MCP-023 | review | export_module_graph | 是 | 是 | 双在 |
| MCP-024 | review | semantic_search | 是 | 是 | 双在 |
| MCP-025 | review | find_similar_functions | 是 | 是 | 双在 |
| MCP-026 | review | get_symbol_commit_history | 是 | 是 | 双在 |
| MCP-027 | review | parse_codeowners | 是 | 是 | 双在 |
| MCP-028 | review | get_project_dependencies | 是 | 是 | 双在 |
| MCP-029 | review | get_summary | 是 | 是 | 双在 |
| MCP-030 | review | project_brief | 是 | 是 | 双在 |
| MCP-031 | review | repo_map | 是 | 是 | 双在 |
| MCP-032 | review | test_impact_selection | 是 | 是 | 双在 |
| MCP-033 | review | who_to_ask | 是 | 是 | 双在 |
| MCP-034 | review | get_ownership_map | 是 | 是 | 双在 |
| MCP-035 | review | guardrail_scan | 是 | 是 | 双在 |
| MCP-036 | review | guardrail_check_edit | 是 | 是 | 双在 |
| MCP-037 | review | guardrail_list_rules | 是 | 是 | 双在 |
| MCP-038 | review | blast_radius | 是 | 是 | 双在 |
| MCP-039 | review | ask_codebase | 是 | 是 | 双在 |
| MCP-040 | review | get_token_savings_report | 是 | 是 | 双在 |
| MCP-041 | review | get_vulnerability_blast_radius | 是 | 是 | 双在 |
| MCP-042 | review | get_clone_aware_impact | 是 | 是 | 双在 |
| MCP-043 | review | review_readiness | 是 | 是 | 双在 |
| MCP-044 | review | cross_layer_impact | 是 | 是 | 双在 |
| MCP-045 | review | evolution_frequency | 是 | 是 | 双在 |
| MCP-046 | review | hotspot_evolution | 是 | 是 | 双在 |
| MCP-047 | review | defect_learn | 是 | 是 | 双在 |
| MCP-048 | review | list_branches | 是 | 是 | 双在 |
| MCP-049 | review | merge_preview | 是 | 是 | 双在 |
| MCP-050 | review | get_edit_history | 是 | 是 | 双在 |
| MCP-051 | review | find_shared_symbols | 是 | 是 | 双在 |
| MCP-052 | review | cross_repo_impact | 是 | 是 | 双在 |
| MCP-053 | review | cross_repo_summary | 是 | 是 | 双在 |
| MCP-054 | closed | lsp_hover | 是 | 是 | 双在 |
| MCP-055 | closed | lsp_definition | 是 | 是 | 双在 |
| MCP-056 | closed | lsp_references | 是 | 是 | 双在 |
| MCP-057 | closed | lsp_diagnostics | 是 | 是 | 双在 |
| MCP-058 | closed | lsp_completion | 是 | 是 | 双在 |
| MCP-059 | closed | lsp_check_available | 是 | 是 | 双在 |
| MCP-060 | closed | rule_candidate_list | 是 | 是 | 双在 |
| MCP-061 | closed | rule_list | 是 | 是 | 双在 |
| MCP-062 | closed | get_applicable_rules | 是 | 是 | 双在 |
| MCP-063 | closed | get_symbol_change_tasks | 是 | 是 | 双在 |
| MCP-064 | closed | audit_verify_chain | 是 | 是 | 双在 |
| MCP-065 | closed | list_audit_signing_keys | 是 | 是 | 双在 |
| MCP-066 | closed | bootstrap_status | 是 | 是 | 双在 |
| MCP-067 | review | list_clones | 是 | 是 | 双在 |
| MCP-068 | review | list_clone_groups | 是 | 是 | 双在 |
| MCP-069 | review | get_clone_group_detail | 是 | 是 | 双在 |
| MCP-070 | review | task_plan_template | 是 | 是 | 双在 |

## INT 系列

- **INT-001** (review)：`internal` → Python ✓ / Rust ✓ → 双在

## P0 系列（55 个）

| 任务组 | 验证点 | 结果 |
|---|---|---|
| P0-A ParseFact 契约 | rust_ext/src/abi_contract.rs、cas.rs、replicator.rs、snapshot_guard.rs 含 parse_fact | ✓ 在 master |
| P0-D HCL/Elixir 语义 | rust_ext/Cargo.toml 含 tree-sitter-hcl + tree-sitter-elixir | ✓ 在 master |
| P0-delete-1/2 删除事务 | db/schema.py daemon_generation 列 + server/schema_migrator.py + rust daemon cas/dispatch | ✓ 在 master |
| P0-CLI-A1 执行上下文 | router.rs 全命令路由 = 统一执行上下文落地 | ✓ 在 master |
| P0-CLI-A2/2-1/2-2/2-3 stats/status/config | router.rs+cw_cli.rs: stats 41 / status 98 / config 56 处 | ✓ 在 master |
| P0-1/2/3 ACL/Watcher/Replicator/CAS/打包 | daemon peer_uid ACL、cas.rs、replicator.rs、build 脚本均在 | ✓ 在 master |

## 综合结论

- 自动扫描初判 8 个 P0 MISSING → 全部为通用词过滤误报，人工核实均存在。
- **MCP/INT/P0 系列与 CLI/SRV 系列结论一致：任务单状态变化 ≠ 代码丢失，0 真缺失。**
- 唯一真缺口仍为 srv-003 的 backup_restore_handlers.rs + test_srv_003.py（已提交 901cc3c）+ SRV-003_evidence.md（已恢复）。