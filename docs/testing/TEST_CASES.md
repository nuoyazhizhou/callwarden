# CallWarden 分级测试用例清单（锚定权威真相源）

> 由 `docs/testing/gen_test_cases.py` 从**实仓权威数据**生成，所有命令/参数/工具均真实，非虚构。

> 真相源：`cli/categories.py`(21类/84顶层) · `cli_full_params.json`(234叶子/提取233) · 
`server/tools/_categories.py`(17类/243工具) · `mcp_full_schema.json`(243工具) · `seed_sample/`(真实种子)

> 本清单是收敛套件（T1–T5 / M1–M4）的**人工可读映射层**；真正执行由收敛套件以同批权威 JSON 全量驱动。


## 0. 权威口径总览

| 项 | 真实值 | 来源 |
|----|--------|------|
| CLI 顶层命令 | 84（分 21 类，[18]-[21] 为 CLI-only） | `cli/categories.py` |
| CLI 叶子命令 | 234（已提取参数 233，跳过 1） | `cli_full_params.json` |
| MCP 工具 | 243（分 17 类，与 CLI [1]-[17] 同构） | `server/tools/_categories.py` |
| 真实种子 fixture | `tests/convergence/seed_sample/`（calc.py + service.ts） | 实仓 |

## 1. CLI 叶子用例分级统计

| 优先级 | 用例数 | 说明 |
|--------|--------|------|
| **P0** | 19 | 只读查询精确断言 + 已知缺陷钉死（fail-closed） |
| **P1** | 187 | 常规契约 / 写隔离 |
| **P2** | 27 | 破坏性/重操作 → 隔离沙箱，不进主回归 |
| **合计** | 233 | CLI 叶子全覆盖 |

按类别分布：GENERAL=174、DESTRUCTIVE=27、WRITE_ISO=13、READONLY=11、FAIL_SOFT=5、TRACEBACK=3


## 2. P0 用例（先做，含 fail-closed 钉死）

统一 AAA：`Arrange`=种子 workspace 已 build_graph（seed_sample）｜`Act`=真实 argv｜`Assert`=精确断言

| case_id | 优先级 | 类别 | Act（真实 argv） | Assert（精确断言） |
|---------|--------|------|------------------|-------------------|
| TC-CLI-001 | P0 | READONLY | `python cw.py brief` | 返回可解析结构 且 关键字段存在 且 无 traceback |
| TC-CLI-002 | P0 | FAIL_SOFT | `python cw.py call-chain multiply` | result 非空 且 首节点 == "multiply"；⚠ 已知 fail-soft，rc==0 但空结果须判失败 |
| TC-CLI-003 | P0 | READONLY | `python cw.py callees multiply` | set(result["callees"]) == {"add"}  # multiply -> add 真实调用边 |
| TC-CLI-004 | P0 | READONLY | `python cw.py callers multiply` | set(result["callers"]) == set()  # multiply 无调用者；add 的调用者 == {"multiply"} |
| TC-CLI-005 | P0 | TRACEBACK | `python cw.py collab publish --json` | exit code / 结构契约：返回可解析结构 且 关键字段存在 且 无 traceback |
| TC-CLI-006 | P0 | FAIL_SOFT | `python cw.py coupled-fns 20` | exit code / 结构契约：返回可解析结构 且 关键字段存在 且 无 traceback |
| TC-CLI-007 | P0 | TRACEBACK | `python cw.py daemon publish <workspace_id> <db_path>` | exit code / 结构契约：返回可解析结构 且 关键字段存在 且 无 traceback |
| TC-CLI-008 | P0 | TRACEBACK | `python cw.py daemon snapshot-stats` | exit code / 结构契约：返回可解析结构 且 关键字段存在 且 无 traceback |
| TC-CLI-009 | P0 | READONLY | `python cw.py file calc.py` | set(s["name"] for s in result) == {"add","multiply"}  # calc.py 恰 2 函数 |
| TC-CLI-010 | P0 | READONLY | `python cw.py grep <patterns> <same>` | 返回可解析结构 且 关键字段存在 且 无 traceback |
| TC-CLI-011 | P0 | READONLY | `python cw.py impact <symbol_hash>` | result["impacted"] 集合确定（multiply → add）；入参是 hash 不是限定名 |
| TC-CLI-012 | P0 | FAIL_SOFT | `python cw.py largest-fns 20` | exit code / 结构契约：返回可解析结构 且 关键字段存在 且 无 traceback |
| TC-CLI-013 | P0 | READONLY | `python cw.py query multiply calc.py` | 返回可解析结构 且 关键字段存在 且 无 traceback |
| TC-CLI-014 | P0 | FAIL_SOFT | `python cw.py rule applicable` | exit code / 结构契约：返回可解析结构 且 关键字段存在 且 无 traceback |
| TC-CLI-015 | P0 | READONLY | `python cw.py search <query>` | multiply 必在结果内；结果条数 == 已知固定值 |
| TC-CLI-016 | P0 | READONLY | `python cw.py stats` | result["symbols"]/["calls"] 为 int 且 >0（>0 非空） |
| TC-CLI-017 | P0 | FAIL_SOFT | `python cw.py status` | exit code / 结构契约：返回可解析结构 且 关键字段存在 且 无 traceback |
| TC-CLI-018 | P0 | READONLY | `python cw.py symbol multiply` | result["name"]=="multiply" 且 有 line/signature 字段 |
| TC-CLI-019 | P0 | READONLY | `python cw.py topo` | 返回可解析结构 且 无 traceback |


## 3. 已知缺陷基线组（来自 T3 首轮，必须先钉住不许回升）

T3 首轮结果（生产 daemon b495919）：**70 PASS / 114 EXPECTED_BUSINESS / 18 DEFECT / 31 SKIP**

| 缺陷类 | 数量 | 代表命令 | 钉死方式 |
|--------|------|----------|----------|
| rc=0 掩盖真 bug（fail-soft 吞异常） | 5 | call-chain, coupled-fns, largest-fns, rule applicable, status | 断言：rc==0 时必须返回有效载荷，**空结果/吞异常 = FAIL** |
| traceback | 3 | collab publish, daemon publish, daemon snapshot-stats | 断言：stdout/stderr 不得含 `Traceback (most recent call last)` |
| method_not_found（CLI→daemon compat RPC 未实现） | 10 | 由探测得出 | 断言：不得新增，只许减少 |

> **关键**：rc=0 却返回空/错误 = 比崩溃更危险。这 5 个 fail-soft 是当前最高价值用例。


## 4. MCP 侧分级（243 工具，T2 已 157 PASS / 72 BUSINESS / **0 DEFECT**）

共 243 个工具，按动词/描述判定：**只读 ≈ 171（P0）**，**写 ≈ 72（P1）**。

- 只读类工具 → P0，用 seed 事实做集合/数量断言；
- 写类工具（`propose_`/`task_`/`lease_`/`guardrail_add`/`gc_*` 等）→ P1，跑在隔离 daemon；
- 基线门禁：`DEFECT == 0` 且 `PASS >= 100` 且 `覆盖合计 == 243`。

### 4.1 17 分类 × 243 工具（权威，与 CLI [1]-[17] 同构）


#### [1] `workspace_database` — Workspace & Database（16：读 9 / 写 7）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `list_workspaces` | P0 | READ |
| `register_workspace` | P1 | WRITE |
| `set_active_workspace` | P1 | WRITE |
| `delete_workspace` | P1 | WRITE |
| `get_active_workspace` | P0 | READ |
| `build_graph` | P1 | WRITE |
| `refresh_file` | P0 | READ |
| `build_directory` | P1 | WRITE |
| `remove_file` | P1 | WRITE |
| `get_stats` | P0 | READ |
| `get_status` | P0 | READ |
| `register_branch` | P1 | WRITE |
| `list_branches` | P0 | READ |
| `diff_branches` | P0 | READ |
| `switch_branch` | P0 | READ |
| `merge_preview` | P0 | READ |

#### [2] `query_search` — Query & Search（24：读 24 / 写 0）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `search_symbols` | P0 | READ |
| `get_symbol` | P0 | READ |
| `get_symbol_location` | P0 | READ |
| `get_file_symbols` | P0 | READ |
| `get_symbol_history` | P0 | READ |
| `get_file_history` | P0 | READ |
| `get_recent_changes` | P0 | READ |
| `get_symbol_content_by_hash` | P0 | READ |
| `file_read` | P0 | READ |
| `file_grep` | P0 | READ |
| `file_list` | P0 | READ |
| `file_symbol_content` | P0 | READ |
| `semantic_search` | P0 | READ |
| `find_similar_functions` | P0 | READ |
| `embed_symbols` | P0 | READ |
| `embed_symbols_async` | P0 | READ |
| `embed_single_symbol` | P0 | READ |
| `generate_summary` | P0 | READ |
| `get_summary` | P0 | READ |
| `project_brief` | P0 | READ |
| `repo_map` | P0 | READ |
| `ask_codebase` | P0 | READ |
| `record_token_savings` | P0 | READ |
| `get_token_savings_report` | P0 | READ |

#### [3] `call_chain` — Call Chain Analysis（14：读 13 / 写 1）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `get_callers` | P0 | READ |
| `get_callees` | P0 | READ |
| `get_impact` | P0 | READ |
| `get_call_chain_down` | P0 | READ |
| `get_top_callers` | P0 | READ |
| `get_orphan_symbols` | P0 | READ |
| `get_deepest_functions` | P0 | READ |
| `get_module_call_stats` | P0 | READ |
| `detect_call_cycles` | P1 | WRITE |
| `get_call_heatmap` | P0 | READ |
| `export_module_graph` | P0 | READ |
| `get_topological_order` | P0 | READ |
| `diff_callers` | P0 | READ |
| `diff_callees` | P0 | READ |

#### [4] `code_health` — Code Health & Metrics（12：读 12 / 写 0）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `get_code_metrics_summary` | P0 | READ |
| `get_complexity_hotspots` | P0 | READ |
| `get_coupling_analysis` | P0 | READ |
| `get_function_metrics` | P0 | READ |
| `get_largest_functions` | P0 | READ |
| `get_most_coupled_functions` | P0 | READ |
| `get_code_health_check` | P0 | READ |
| `check_file_health` | P0 | READ |
| `evolution_frequency` | P0 | READ |
| `defect_correlation` | P0 | READ |
| `hotspot_evolution` | P0 | READ |
| `churn_analysis` | P0 | READ |

#### [5] `task` — Task Orchestration（36：读 11 / 写 25）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `task_create` | P1 | WRITE |
| `task_create_subtask` | P1 | WRITE |
| `task_split` | P1 | WRITE |
| `task_create_from_plan` | P1 | WRITE |
| `task_plan_template` | P1 | WRITE |
| `task_next_step` | P1 | WRITE |
| `work_next_job` | P0 | READ |
| `task_resolve_block` | P1 | WRITE |
| `task_report_step` | P1 | WRITE |
| `task_rollback` | P1 | WRITE |
| `task_apply` | P1 | WRITE |
| `task_close` | P1 | WRITE |
| `task_capture_diff` | P1 | WRITE |
| `task_list` | P1 | WRITE |
| `task_status` | P1 | WRITE |
| `task_governance_projection` | P1 | WRITE |
| `task_status_tree` | P1 | WRITE |
| `task_completion_review` | P1 | WRITE |
| `task_quality_findings` | P1 | WRITE |
| `task_resolve_quality_finding` | P1 | WRITE |
| `record_task_symbol_change` | P0 | READ |
| `link_edit_audit_symbols` | P0 | READ |
| `get_task_symbol_changes` | P0 | READ |
| `get_symbol_change_tasks` | P0 | READ |
| `cancel_job` | P1 | WRITE |
| `list_jobs` | P0 | READ |
| `get_job_stats` | P0 | READ |
| `wait_for_job` | P0 | READ |
| `get_job_status` | P0 | READ |
| `get_task_commits` | P0 | READ |
| `get_commit_tasks` | P0 | READ |
| `task_assignment_status` | P1 | WRITE |
| `task_assignment_heartbeat` | P1 | WRITE |
| `task_get_role_prompt` | P1 | WRITE |
| `task_remediation_create` | P1 | WRITE |
| `task_step_resolve` | P1 | WRITE |

#### [6] `rule_memory` — Agent Rule Memory（11：读 11 / 写 0）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `rule_candidate_create` | P0 | READ |
| `rule_candidate_list` | P0 | READ |
| `rule_candidate_accept` | P0 | READ |
| `rule_candidate_reject` | P0 | READ |
| `rule_list` | P0 | READ |
| `get_applicable_rules` | P0 | READ |
| `rule_sync_agents_md` | P0 | READ |
| `rule_insert_agents_md_block` | P0 | READ |
| `extract_rule_candidates_from_quality_findings` | P0 | READ |
| `rule_seed_bootstrap` | P0 | READ |
| `cleanup_agent_rule_sync_log` | P0 | READ |

#### [7] `audit_bootstrap` — Audit & Bootstrap（10：读 7 / 写 3）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `audit_verify_chain` | P1 | WRITE |
| `rotate_audit_signing_key` | P1 | WRITE |
| `list_audit_signing_keys` | P0 | READ |
| `bootstrap_status` | P0 | READ |
| `run_check_gate` | P0 | READ |
| `resolve_gate_findings` | P0 | READ |
| `guardrail_scan` | P0 | READ |
| `guardrail_check_edit` | P0 | READ |
| `guardrail_list_rules` | P0 | READ |
| `guardrail_add_rule` | P1 | WRITE |

#### [8] `git` — Git Integration（6：读 5 / 写 1）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `import_git_history` | P1 | WRITE |
| `get_git_commits` | P0 | READ |
| `get_commit_changes` | P0 | READ |
| `get_git_stats` | P0 | READ |
| `get_symbol_commit_history` | P0 | READ |
| `compare_snapshots` | P0 | READ |

#### [9] `semgrep_defects` — Semgrep & Defects（18：读 18 / 写 0）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `run_semgrep_scan` | P0 | READ |
| `semgrep_scan_async` | P0 | READ |
| `scan_semgrep_incremental` | P0 | READ |
| `get_semgrep_stats` | P0 | READ |
| `get_semgrep_findings` | P0 | READ |
| `get_issue_summary` | P0 | READ |
| `get_symbol_issues` | P0 | READ |
| `find_issues` | P0 | READ |
| `defect_search` | P0 | READ |
| `defect_suggest_fix` | P0 | READ |
| `defect_learn` | P0 | READ |
| `defect_stats` | P0 | READ |
| `get_defect_correlation` | P0 | READ |
| `blast_radius` | P0 | READ |
| `get_vulnerability_blast_radius` | P0 | READ |
| `diff_to_symbol` | P0 | READ |
| `review_readiness` | P0 | READ |
| `cross_layer_impact` | P0 | READ |

#### [10] `coverage_ownership` — Coverage & Ownership（19：读 14 / 写 5）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `get_comment_coverage` | P0 | READ |
| `get_uncommented_symbols` | P0 | READ |
| `get_test_coverage` | P0 | READ |
| `get_test_cases` | P0 | READ |
| `get_tested_functions` | P0 | READ |
| `get_test_coverage_summary` | P0 | READ |
| `get_test_stability` | P0 | READ |
| `get_comment_from_version` | P0 | READ |
| `restore_comment` | P1 | WRITE |
| `restore_all_comments` | P1 | WRITE |
| `import_coverage` | P1 | WRITE |
| `get_coverage_for_symbol` | P0 | READ |
| `find_uncovered_functions` | P0 | READ |
| `test_impact_selection` | P0 | READ |
| `who_to_ask` | P0 | READ |
| `get_ownership_map` | P0 | READ |
| `parse_codeowners` | P0 | READ |
| `import_codeowners` | P1 | WRITE |
| `import_git_blame` | P1 | WRITE |

#### [11] `gc` — GC（11：读 2 / 写 9）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `get_project_dependencies` | P0 | READ |
| `import_project_dependencies` | P1 | WRITE |
| `prune_external_symbols` | P0 | READ |
| `gc_retention` | P1 | WRITE |
| `gc_policy_get` | P1 | WRITE |
| `gc_policy_set` | P1 | WRITE |
| `gc_archive_list` | P1 | WRITE |
| `gc_archive_inspect` | P1 | WRITE |
| `gc_archive_import` | P1 | WRITE |
| `gc_audit_list` | P1 | WRITE |
| `gc_audit_get` | P1 | WRITE |

#### [12] `diagnostics` — Diagnostics（27：读 20 / 写 7）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `detect_clones` | P1 | WRITE |
| `detect_clones_async` | P1 | WRITE |
| `list_clone_groups` | P0 | READ |
| `get_clone_group_detail` | P0 | READ |
| `get_clone_group_stats` | P0 | READ |
| `list_clones` | P0 | READ |
| `get_clone_stats` | P0 | READ |
| `clear_clones` | P1 | WRITE |
| `get_clone_aware_impact` | P0 | READ |
| `propose_edit` | P1 | WRITE |
| `propose_range_patch` | P1 | WRITE |
| `propose_symbol_patch` | P1 | WRITE |
| `propose_symbol_id_patch` | P1 | WRITE |
| `revert_edit` | P0 | READ |
| `get_edit_history` | P0 | READ |
| `get_edit_stats` | P0 | READ |
| `detect_cross_repo_deps` | P0 | READ |
| `find_shared_symbols` | P0 | READ |
| `cross_repo_impact` | P0 | READ |
| `cross_repo_summary` | P0 | READ |
| `lsp_hover` | P0 | READ |
| `lsp_definition` | P0 | READ |
| `lsp_references` | P0 | READ |
| `lsp_diagnostics` | P0 | READ |
| `lsp_completion` | P0 | READ |
| `lsp_check_available` | P0 | READ |
| `get_metrics` | P0 | READ |

#### [13] `build_context` — 构建上下文感知（8：读 8 / 写 0）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `get_toolchain` | P0 | READ |
| `list_toolchains` | P0 | READ |
| `get_build_context` | P0 | READ |
| `list_build_contexts` | P0 | READ |
| `get_active_build_context` | P0 | READ |
| `get_workspace_toolchains` | P0 | READ |
| `get_resolved_edges` | P0 | READ |
| `count_resolved_edges` | P0 | READ |

#### [14] `collab` — 只读协同查询（6：读 3 / 写 3）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `append_evidence` | P1 | WRITE |
| `find_evidence` | P0 | READ |
| `get_freshness_status` | P1 | WRITE |
| `get_gate_decision` | P0 | READ |
| `get_role_view` | P0 | READ |
| `submit_verdict` | P1 | WRITE |

#### [15] `dependency` — 依赖图与环检测（10：读 6 / 写 4）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `build_hard_dependency_edges` | P1 | WRITE |
| `get_dependency_edges` | P0 | READ |
| `detect_dependency_cycle` | P1 | WRITE |
| `validate_revision_dependencies` | P0 | READ |
| `publish_interface` | P1 | WRITE |
| `get_interface_providers` | P0 | READ |
| `select_interface_provider` | P0 | READ |
| `import_envelope_dependencies` | P1 | WRITE |
| `record_artifact_identity` | P0 | READ |
| `get_artifact_freshness` | P0 | READ |

#### [16] `assignment_lease` — Assignment 与 Lease（8：读 0 / 写 8）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `lease_acquire` | P1 | WRITE |
| `lease_renew` | P1 | WRITE |
| `lease_release` | P1 | WRITE |
| `lease_status` | P1 | WRITE |
| `lease_list_events` | P1 | WRITE |
| `assignment_create` | P1 | WRITE |
| `assignment_show` | P1 | WRITE |
| `assignment_revoke` | P1 | WRITE |

#### [17] `identity` — Identity 与 Attestation（7：读 6 / 写 1）

| 工具 | 优先级 | 读写 |
|------|--------|------|
| `record_action_identity` | P0 | READ |
| `get_action_identity` | P0 | READ |
| `check_action_identity` | P0 | READ |
| `check_session_separation` | P0 | READ |
| `get_attestation_validity` | P0 | READ |
| `register_attestation_revocation` | P1 | WRITE |
| `list_attestation_revocations` | P0 | READ |


## 5. 21 个 CLI 分类 × 84 顶层命令（权威）

> 注：[18]-[21] 为 CLI-only（MCP 无对应分类）。新增命令漏归类会被既有 `test_category_source.py` 拦截。


### [1] `workspace_database` — Workspace & Database（6）

`workspace`, `refresh`, `stats`, `status`, `graph`, `config`

### [2] `query_search` — Query & Search（13）

`search`, `grep`, `symbol`, `file`, `query`, `brief`, `map`, `fts`, `semantic-search`, `similar`, `embed`, `diff`, `changes`

### [3] `call_chain` — Call Chain Analysis（12）

`callers`, `callees`, `call-chain`, `topo`, `impact`, `deepest`, `module-calls`, `detect-cycles`, `export-module-graph`, `call-heatmap`, `top-callers`, `orphan-symbols`

### [4] `code_health` — Code Health & Metrics（11）

`metrics`, `complexity`, `coupling`, `largest-fns`, `coupled-fns`, `fn-metrics`, `evolution`, `hotspot`, `churn`, `health-report`, `dashboard`

### [5] `task` — Task Orchestration（1）

`task`

### [6] `rule_memory` — Agent Rule Memory（1）

`rule`

### [7] `audit_bootstrap` — Audit & Bootstrap（4）

`audit`, `bootstrap`, `check-gate`, `guardrail`

### [8] `git` — Git Integration（2）

`git`, `symbol-history`

### [9] `semgrep_defects` — Semgrep & Defects（6）

`semgrep`, `defect`, `vuln-blast`, `review`, `issues`, `function-issues`

### [10] `coverage_ownership` — Coverage & Ownership（9）

`coverage`, `tests`, `test-impact`, `comment-coverage`, `uncommented`, `restore-comment`, `restore-all-comments`, `who`, `ownership-map`

### [11] `gc` — GC（1）

`gc`

### [12] `diagnostics` — Diagnostics（2）

`doctor`, `clone`

### [13] `build_context` — 构建上下文感知（2）

`build-context`, `toolchain`

### [14] `collab` — 只读协同查询（1）

`collab`

### [15] `dependency` — 依赖图与环检测（1）

`dependency`

### [16] `assignment_lease` — Assignment 与 Lease（2）

`lease`, `assignment`

### [17] `identity` — Identity 与 Attestation（1）

`identity`

### [18] `rollback` — Migration Rollback（1） *(CLI-only)*

`rollback`

### [19] `daemon_ops` — Daemon 运维（1） *(CLI-only)*

`daemon`

### [20] `setup_install` — 安装与初始化（6） *(CLI-only)*

`install`, `install-agent`, `install-hook`, `setup`, `server`, `test`

### [21] `experiment` — 盲评实验（1） *(CLI-only)*

`experiment`


## 6. 收敛套件覆盖映射（执行层 ↔ 本清单）

| 套件 | 职责 | 覆盖本清单的哪部分 | 基线 / 门禁 |
|------|------|---------------------|----------------|
| **T1** 基建冒烟 | 种子 workspace fixture + param_provider 骨架 | 不直接测业务，是 T2/T3 的前提 | 必须 PASS 才能跑 T2/T3 |
| **T2** MCP 全参数 | 243 MCP 工具真实调用 | §4 全部 243 工具 | 157 PASS / 72 BUSINESS / **0 DEFECT**；门禁 DEFECT==0 & PASS>=100 & 覆盖==243 |
| **T3** CLI 全参数 | 234 CLI 叶子真实调用 | §1 全部 233 提取叶子 + §5 84 顶层 | 70 PASS / 114 BUSINESS / 18 DEFECT / 31 SKIP；门禁 DEFECT 不回升 |
| **T4** 多 workspace 隔离 | 多用户/多 workspace/多 agent 并发正确性 | §4 写类工具的并发隔离场景 | 并发无死锁、隔离性成立 |
| **T5** LLM 可理解性 | 真实 LLM 按工具 description 选对率 | §4 工具 description 质量 | 选对率 ≥ 基线阈值（需 OPENAI_API_KEY） |
| **M1** 路由矩阵 | 239/239 路由验证（每个工具 rpc_method 在 dispatch.rs） | §4 全部 243 工具的路由可达性 | 239/239 通过 |
| **M2** 纯 client 审计 | Python 侧无新违例 | cli/ 包 purity | 零新增违例 |
| **M3** 并发写 | 双 agent 单 workspace 并发一致性 | §4 写类工具的并发正确性 | 混合读写无脏写 |
| **M4** fail-closed | daemon 不可达 → DaemonUnavailableError（不降级本地） | §2 的 fail-closed 钉死 + §5 所有 CLI 命令 | 不可达一律结构化错误，绝不本地执行 |

> 覆盖结论：**T2 + T3 已对全部 243 MCP 工具与 234 CLI 叶子做全参数真实调用**，
> 本清单的 §1/§4 即这两层的「人工可读映射」。剩余 219 处 `pytest.skip` 绝大多数是
> 环境/平台类（二进制未构建、命名管道被生产 daemon 占用、Windows 无 AF_UNIX、缺语言 fixture），
> 详见 `COVERAGE_AUDIT.md`。
