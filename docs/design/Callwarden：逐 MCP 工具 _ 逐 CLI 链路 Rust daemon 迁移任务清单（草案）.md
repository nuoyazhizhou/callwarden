# Callwarden：逐 MCP 工具 / 逐 CLI 链路 Rust daemon 迁移任务清单（草案）

**状态：** 仅为任务树草案；**尚未创建或修改任何 CW 任务**。

**拟定父任务：** `T-1787209886781-48b4cb0c`，当前状态为 `closed`。若用户确认创建子任务，系统可能按既有规则将其 reopen；这是显式、可审计的状态变化，不会在本草案阶段发生。

**本轮角色：** `executor / planner / T-1787209886781-48b4cb0c / Skill: none`。

---

## 1. 计划目标与硬边界

本计划将每个可迁移单元限定为：**一个公开 MCP 工具，或一个明确的 `cw` CLI 命令链路**。每个任务交付一条完整链路：

```text
MCP tool / cw command
  → Python thin wrapper
  → HTTP JSON-RPC method
  → Rust dispatch
  → Rust native handler
  → authoritative readonly connection / service
  → success + structured-error + daemon-restart fixture
```

每个任务只允许把一个工具从 `python_compat` 变为 `rust_native`，或者修复一个明确 CLI 链路。不得顺带迁移同模块的其他工具，不得删除 `db/`，不得触及任务、lease、verdict、gate 等治理写入语义。所有 70 个当前 `python_compat` 工具的 operation class 都是 `READ_ONLY`；它们不得借迁移改成 write。 [1]

> **执行顺序不是“先删 Python，再证明可用”。** 每个工具应先在 Rust daemon 内实现等价 readonly handler，完成真实 HTTP round-trip 与负向测试，最后才移除它在 Python compatibility worker 的单项注册。未通过验收的工具维持 `python_compat/transition` 或明确 `disabled`，不得假装已迁移。

### 1.1 父任务结构风险

`T-1787209886781-48b4cb0c` 原本是 **S1：`cli/main.py` 迁移 daemon RPC** 的闭环任务，而不是 MCP 工具迁移 Epic。将 70 个 MCP 子任务挂在它下面是用户指定的结构性例外，会使其职责变为“渐进迁移总入口”。如果执行该例外，必须在每个子任务说明中保留：

- 仅覆盖一个工具链路；
- 严格串行，不并行；
- 不修改旧 S1 / 旧 S2 / S3 的状态；
- 不将 `closed` 父任务重新关闭；完成后只移交 Reviewer；
- 后续可将已完成子任务整体迁移到正式的“Rust daemon MCP 恢复”Epic，但不得在未证明 `supersedes` 前删除历史任务。

---

## 2. 所有子任务必须复制的固定合同

每一个任务均应写入以下不变内容；实际创建时只替换工具名、文件和函数。

| 项目 | 强制内容 |
|---|---|
| **Role Contract** | `executor / implementer`，handoff 至 `reviewer / independent_reviewer`；执行者不得 `apply/close`。 |
| **输入契约** | 保持既有 MCP 工具名、参数名、默认值、返回 JSON shape 与 stable error code；除非单独兼容任务批准，否则不能重命名 RPC。 |
| **Rust handler** | 使用 `pub fn handle_<tool>(conn: &Connection, workspace_id: i64, params: &Value) -> Result<Value, DaemonRpcError>` 或已有 readonly service 的等价签名；禁止客户端 SQL、字符串拼接 SQL 条件和 local DB fallback。现有 `query_compat_handlers.rs` 是函数签名与 SQL porting 的参考。 [2] |
| **Python 变更** | 保留 MCP wrapper 函数，改为纯 `route_rpc()`/HTTP 适配；移除该单工具的 `_h_<tool>` 与其在 `*_READ_ONLY_METHODS`/`register_compat_routes` 中的行。不得删除同模块其他工具 handler。 |
| **Rust 路由** | 新增或修改仅该工具的 `DaemonState::handle_<tool>` 和 `dispatch.rs` method branch；`http_server.rs` capability registry 将该工具从 `python_compat` 更新为 `rust_native`。 |
| **真相源同步** | 更新生成器输入与 `tool_migration_matrix.json` 中该工具的 `target_backend=rust_native`、`status=migrated`、`batch=MCP-PER-TOOL-<NN>`；**不手工伪造** `mcp-tools-implementation-map.md`，应由其生成入口刷新。 |
| **测试** | 新建 `tests/test_mcp_<tool>_http_rpc.py`（或按同目录既有 fixture 扩展）：成功、非法参数、未知/未授权 workspace、daemon unavailable fail-closed、daemon restart 后同输出；对纯 SQL port 再增加 Python compat golden parity。 |
| **禁止项** | 不改 `db/schema.py`；不改 `task_collab.rs` 的治理写；不改 lease/assignment 语义；不把 worker 写权限扩展；不为“测试通过”保留 hidden SQLite fallback。 |
| **证据** | 记录 Git commit、Rust test、Python进程级 HTTP fixture、daemon binary/source fingerprint、capability registry row、矩阵行；报告前确认本工具不再在 Python compat registry 注册。 |

### 2.1 共享文件串行化规则

每个工具都可能触及 `rust_ext/src/daemon/dispatch.rs`、`rust_ext/src/daemon/http_server.rs`、`server/compat_registry.py` 和生成的矩阵。因此这些任务**必须严格串行**。新工具任务必须依赖前一个已 Reviewer PASS 的工具任务；不得把“不同 MCP 工具”误判成可并行 ownership。

每个领域的第一个工具允许创建其指定的新 Rust module；后续工具只能编辑该领域 module 内自己的 `handle_<tool>`、自己的 MCP wrapper/worker 注册行以及自己的测试文件。

---

## 3. 先行 CLI / 控制面修复任务

这些不是 70 个 `python_compat` MCP 项目的一部分，但若不先完成，执行者无法可靠地用 `cw` 观察迁移结果。

| 顺序 | 拟建子任务 | Python/CLI 源端 | Rust/目标修改点 | 必须验收 |
|---:|---|---|---|---|
| CLI-01 | `cw daemon health / manifest / capability 诊断链路` | `cw.py`、`cli/daemon_commands.py`、`server/daemon_autostart.py::resolve_http_endpoint_and_manifest` | `rust_ext/src/daemon/http_server.rs` 的 `/health`、capability response；必要时 `health.rs` | 缺 manifest、stale PID、wrong authority、fresh daemon 分别有稳定错误；至少 `get_stats` 真实 MCP call 成功；不得关闭 manifest 校验。 |
| CLI-02 | `cw search <query>` 的 daemon-only 读取链路 | `cli/main.py` 的 `search` 子命令与结果格式化（当前会因 `has_comment` KeyError 失败） | 若后端缺字段，在 `query_handlers.rs` 或对应 RPC response 追加兼容字段；否则仅修 Python formatter | `cw search find_evidence` 不再 `KeyError`；空结果、无 `has_comment`、daemon unavailable 都有稳定行为；禁止把 `get_db()` 重新塞回 CLI。 |
| CLI-03 | `cw task show/list/status-tree` 只读 authority 诊断 | `cli/main.py` 的 task read subcommands、`server/daemon_client.py::route_task_read` | `task_collab.rs` 的只读投影或未来 readonly task query module；不改 task state | target task、父树、未绑定/错误 authority、manifest stale 都可区分；只读查询不得写 active workspace。 |

**门禁：** CLI-01 必须先通过。CLI-02 与 CLI-03 仅在 CLI-01 后执行。之后才开始 MCP 工具逐条迁移。

---

## 4. 逐 MCP 工具迁移清单

### 4.1 Collab 只读（4 项）

**Rust module：** 新建 `rust_ext/src/daemon/collab_query_handlers.rs`；首任务同时在 `mod.rs` 声明。每条任务新增 `handle_<tool>`，从 task DB 只读查询；不要调用 legacy `task_collab.rs` 的 mutation handler。

| 序号 | 工具 / RPC | Python wrapper 与 compat handler | Rust 新增函数 | 关键语义 |
|---:|---|---|---|---|
| MCP-001 | `get_role_view` / `get_role_view` | `server/tools/tools_collab.py::get_role_view` L115；`_h_get_role_view` L442 | `collab_query_handlers.rs::handle_get_role_view` | 只读 Role View；无 task/binding 时 structured not-found/unverified。 |
| MCP-002 | `find_evidence` / `find_evidence` | `::find_evidence` L133；`_h_find_evidence` L447 | `::handle_find_evidence` | 只读 evidence query；保留 task/contract/verifier/limit 过滤。 |
| MCP-003 | `get_freshness_status` / `get_freshness_status` | `::get_freshness_status` L153；`_h_get_freshness_status` L452 | `::handle_get_freshness_status` | freshness 为派生 read，不得重新计算或写 Gate。 |
| MCP-004 | `get_gate_decision` / `get_gate_decision` | `::get_gate_decision` L174；`_h_gate_decision` L457 | `::handle_get_gate_decision` | 只读历史 decision；不得触发 gate evaluation。 |

### 4.2 P2 dependency graph 只读（5 项）

**Rust module：** 新建 `rust_ext/src/daemon/dependency_query_handlers.rs`；首任务声明 module。Python 参考 handler 均在 `tools_p2_graph.py` L205–319 的 `_h_*` 与 `_P2_GRAPH_READ_ONLY_METHODS`。

| 序号 | 工具 / RPC | Python wrapper / handler | Rust 新增函数 | 关键语义 |
|---:|---|---|---|---|
| MCP-005 | `get_artifact_freshness` | `tools_p2_graph.py::get_artifact_freshness` L88 / `_h_get_artifact_freshness` | `dependency_query_handlers.rs::handle_get_artifact_freshness` | 只读 artifact freshness，不写 identity。 |
| MCP-006 | `get_interface_providers` | `::get_interface_providers` L118 / `_h_get_interface_providers` | `::handle_get_interface_providers` | 保持 provider 过滤与 workspace isolation。 |
| MCP-007 | `detect_cycle` | `::detect_cycle` L163 / `_h_detect_cycle` | `::handle_detect_cycle` | 只读 cycle detection；QueryBudget 限制。 |
| MCP-008 | `validate_revision_dependencies` | `::validate_revision_dependencies` L174 / `_h_validate_revision_dependencies` | `::handle_validate_revision_dependencies` | 只返回 validation projection，不写依赖。 |
| MCP-009 | `get_dependency_edges` | `::get_dependency_edges` L190 / `_h_get_dependency_edges` | `::handle_get_dependency_edges` | 只读 edge query；未知 workspace fail-closed。 |

### 4.3 P3 identity / attestation 只读（5 项）

**Rust module：** 新建 `rust_ext/src/daemon/identity_query_handlers.rs`；不得把客户端自报 identity 当作鉴权结果。Python compatibility handlers 位于 `tools_p3_identity.py` L194 后。

| 序号 | 工具 / RPC | Python wrapper | Rust 新增函数 | 关键语义 |
|---:|---|---|---|---|
| MCP-010 | `get_action_identity` | `tools_p3_identity.py::get_action_identity` L69 / `_h_get_action_identity` | `identity_query_handlers.rs::handle_get_action_identity` | 返回持久化 identity record，不增加 attestation claim。 |
| MCP-011 | `check_action_identity` | `::check_action_identity` L85 / `_h_check_action_identity` | `::handle_check_action_identity` | 只读比较结果；dev HTTP 不得声称企业级可信。 |
| MCP-012 | `check_session_separation` | `::check_session_separation` L101 / `_h_check_session_separation` | `::handle_check_session_separation` | 保持 session/instance separation 的现有枚举。 |
| MCP-013 | `get_attestation_validity` | `::get_attestation_validity` L117 / `_h_get_attestation_validity` | `::handle_get_attestation_validity` | 无有效 attestation 显式 `unverified`，不能默认 valid。 |
| MCP-014 | `list_attestation_revocations` | `::list_attestation_revocations` L142 / `_h_list_attestation_revocations` | `::handle_list_attestation_revocations` | 只读 revocation ledger。 |

### 4.4 P4 assignment 查询（1 项）

**Rust module：** 新建 `rust_ext/src/daemon/assignment_query_handlers.rs`。这是只读任务，但其验收必须与 B3 assignment→role→identity 缺口相容：只能呈现事实，不得借该任务自创授权语义。

| 序号 | 工具 / RPC | Python wrapper / handler | Rust 新增函数 | 关键语义 |
|---:|---|---|---|---|
| MCP-015 | `assignment_show` | `tools_p4_lease.py::assignment_show` L202 / `_h_assignment_show`（L233–280 block） | `assignment_query_handlers.rs::handle_assignment_show` | 查询 assignment revision/status；未绑定任务显式 UNVERIFIED。 |

### 4.5 Query 兼容工具（8 项）

**Rust module：** 现有 `rust_ext/src/daemon/query_compat_handlers.rs`。所有函数沿用该文件当前 `handle_<tool>(conn, workspace_id, params)` 风格；不得另建 Python worker fallback。

| 序号 | 工具 / RPC | Python wrapper | Rust 新增函数 |
|---:|---|---|---|
| MCP-016 | `get_symbol_history` | `tools_query.py::get_symbol_history` L139 / `_h_get_symbol_history` | `query_compat_handlers.rs::handle_get_symbol_history` |
| MCP-017 | `get_recent_changes` | `::get_recent_changes` L165 / `_h_get_recent_changes` | `::handle_get_recent_changes` |
| MCP-018 | `get_impact` | `::get_impact` L183 / `_h_get_impact` | `::handle_get_impact` |
| MCP-019 | `get_comment_from_version` | `::get_comment_from_version` L263 / `_h_get_comment_from_version` | `::handle_get_comment_from_version` |
| MCP-020 | `get_issue_summary` | `::get_issue_summary` L282 / `_h_get_issue_summary` | `::handle_get_issue_summary` |
| MCP-021 | `find_issues` | `::find_issues` L292 / `_h_find_issues` | `::handle_find_issues` |
| MCP-022 | `get_test_coverage` | `::get_test_coverage` L426 / `_h_get_test_coverage` | `::handle_get_test_coverage` |
| MCP-023 | `export_module_graph` | `::export_module_graph` L437 / `_h_export_module_graph` | `::handle_export_module_graph` |

### 4.6 Semantic / external symbol 工具（5 项）

**Rust module：** 新建 `rust_ext/src/daemon/semantic_query_handlers.rs`；该任务域可能需要调用既有 parser/graph query service，但不得把 embedding、GC 或 import write 混入 readonly handler。

| 序号 | 工具 / RPC | Python wrapper / handler | Rust 新增函数 |
|---:|---|---|---|
| MCP-024 | `semantic_search` | `tools_semantic.py::semantic_search` L44 / `_h_semantic_search` L313 | `semantic_query_handlers.rs::handle_semantic_search` |
| MCP-025 | `find_similar_functions` | `::find_similar_functions` L57 / `_h_find_similar_functions` L321 | `::handle_find_similar_functions` |
| MCP-026 | `get_symbol_commit_history` | `::get_symbol_commit_history` L99 / `_h_get_symbol_commit_history` L330 | `::handle_get_symbol_commit_history` |
| MCP-027 | `parse_codeowners` | `::parse_codeowners` L114 / `_h_parse_codeowners` L338 | `::handle_parse_codeowners` |
| MCP-028 | `get_project_dependencies` | `::get_project_dependencies` L153 / `_h_get_project_dependencies` L345 | `::handle_get_project_dependencies` |

### 4.7 Summary / analysis 工具（19 项）

**Rust module：** 新建 `rust_ext/src/daemon/summary_query_handlers.rs`。每条任务须先确认其 Python handler 是确定性 SQL/graph projection；若涉及 LLM、外部网络或隐式 cache，任务应 fail-closed 并交由后续独立 `job` 设计，而不能伪装成同步 readonly native handler。

| 序号 | 工具 / RPC | Python wrapper / compat handler | Rust 新增函数 |
|---:|---|---|---|
| MCP-029 | `get_summary` | `tools_summary.py::get_summary` L63 / `_h_get_summary` L553 | `summary_query_handlers.rs::handle_get_summary` |
| MCP-030 | `project_brief` | `::project_brief` L75 / `_h_project_brief` L558 | `::handle_project_brief` |
| MCP-031 | `repo_map` | `::repo_map` L87 / `_h_repo_map` L563 | `::handle_repo_map` |
| MCP-032 | `test_impact_selection` | `::test_impact_selection` L138 / `_h_test_impact_selection` L573 | `::handle_test_impact_selection` |
| MCP-033 | `who_to_ask` | `::who_to_ask` L150 / `_h_who_to_ask` L578 | `::handle_who_to_ask` |
| MCP-034 | `get_ownership_map` | `::get_ownership_map` L164 / `_h_get_ownership_map` L583 | `::handle_get_ownership_map` |
| MCP-035 | `guardrail_scan` | `::guardrail_scan` L176 / `_h_guardrail_scan` L590 | `::handle_guardrail_scan` |
| MCP-036 | `guardrail_check_edit` | `::guardrail_check_edit` L190 / `_h_guardrail_check_edit` L595 | `::handle_guardrail_check_edit` |
| MCP-037 | `guardrail_list_rules` | `::guardrail_list_rules` L205 / `_h_guardrail_list_rules` L603 | `::handle_guardrail_list_rules` |
| MCP-038 | `blast_radius` | `::blast_radius` L235 / `_h_blast_radius` L608 | `::handle_blast_radius` |
| MCP-039 | `ask_codebase` | `::ask_codebase` L250 / `_h_ask_codebase` L616 | `::handle_ask_codebase` **或明确改为 job/unsupported** |
| MCP-040 | `get_token_savings_report` | `::get_token_savings_report` L285 / `_h_get_token_savings_report` L627 | `::handle_get_token_savings_report` |
| MCP-041 | `get_vulnerability_blast_radius` | `::get_vulnerability_blast_radius` L297 / `_h_get_vulnerability_blast_radius` L632 | `::handle_get_vulnerability_blast_radius` |
| MCP-042 | `get_clone_aware_impact` | `::get_clone_aware_impact` L310 / `_h_get_clone_aware_impact` L641 | `::handle_get_clone_aware_impact` |
| MCP-043 | `review_readiness` | `::review_readiness` L339 / `_h_review_readiness` L649 | `::handle_review_readiness` |
| MCP-044 | `cross_layer_impact` | `::cross_layer_impact` L353 / `_h_cross_layer_impact` L654 | `::handle_cross_layer_impact` |
| MCP-045 | `evolution_frequency` | `::evolution_frequency` L367 / `_h_evolution_frequency` L659 | `::handle_evolution_frequency` |
| MCP-046 | `hotspot_evolution` | `::hotspot_evolution` L403 / `_h_hotspot_evolution` L667 | `::handle_hotspot_evolution` |
| MCP-047 | `defect_learn` | `::defect_learn` L481 / `_h_defect_learn` L672 | `::handle_defect_learn` **或明确改为 job/unsupported** |

### 4.8 Security / branch / LSP 工具（15 项）

**Rust modules：** `security_query_handlers.rs`（新）用于 Git/SQL/rule projections；`lsp_handlers.rs`（新）用于 LSP 后端。LSP 类工具必须经过 daemon-managed process/session，不能把 IDE process handle 或任意 shell command 交给 client。

| 序号 | 工具 / RPC | Python wrapper / compat handler | Rust 新增函数 / 文件 |
|---:|---|---|---|
| MCP-048 | `list_branches` | `tools_security.py::list_branches` L65 / `_h_list_branches` | `security_query_handlers.rs::handle_list_branches` |
| MCP-049 | `merge_preview` | `::merge_preview` L177 / `_h_merge_preview` L838 | `security_query_handlers.rs::handle_merge_preview` |
| MCP-050 | `get_edit_history` | `::get_edit_history` L294 / `_h_get_edit_history` | `security_query_handlers.rs::handle_get_edit_history` |
| MCP-051 | `find_shared_symbols` | `::find_shared_symbols` L364 / `_h_find_shared_symbols` | `security_query_handlers.rs::handle_find_shared_symbols` |
| MCP-052 | `cross_repo_impact` | `::cross_repo_impact` L390 / `_h_cross_repo_impact` | `security_query_handlers.rs::handle_cross_repo_impact` |
| MCP-053 | `cross_repo_summary` | `::cross_repo_summary` L416 / `_h_cross_repo_summary` | `security_query_handlers.rs::handle_cross_repo_summary` |
| MCP-054 | `lsp_hover` | `::lsp_hover` L434 / `_h_lsp_hover` L944 | `lsp_handlers.rs::handle_lsp_hover` |
| MCP-055 | `lsp_definition` | `::lsp_definition` L454 / `_h_lsp_definition` L953 | `lsp_handlers.rs::handle_lsp_definition` |
| MCP-056 | `lsp_references` | `::lsp_references` L473 / `_h_lsp_references` L962 | `lsp_handlers.rs::handle_lsp_references` |
| MCP-057 | `lsp_diagnostics` | `::lsp_diagnostics` L499 / `_h_lsp_diagnostics` L972 | `lsp_handlers.rs::handle_lsp_diagnostics` |
| MCP-058 | `lsp_completion` | `::lsp_completion` L521 / `_h_lsp_completion` | `lsp_handlers.rs::handle_lsp_completion` |
| MCP-059 | `lsp_check_available` | `::lsp_check_available` L541 / `_h_lsp_check_available` | `lsp_handlers.rs::handle_lsp_check_available` |
| MCP-060 | `rule_candidate_list` | `::rule_candidate_list` L631 / `_h_rule_candidate_list` L995 | `security_query_handlers.rs::handle_rule_candidate_list` |
| MCP-061 | `rule_list` | `::rule_list` L680 / `_h_rule_list` L1005 | `security_query_handlers.rs::handle_rule_list` |
| MCP-062 | `get_applicable_rules` | `::get_applicable_rules` L693 / `_h_get_applicable_rules` | `security_query_handlers.rs::handle_get_applicable_rules` |

### 4.9 Task / audit / clone 只读工具（8 项）

**Rust module：** 新建 `rust_ext/src/daemon/task_read_handlers.rs`。此模块只做 projection；不得使用该计划顺带修改 `task.report`、claim、lease、verdict 或 Gate mutation 路径。

| 序号 | 工具 / RPC | Python wrapper / compat handler | Rust 新增函数 |
|---:|---|---|---|
| MCP-063 | `get_symbol_change_tasks` | `tools_task.py::get_symbol_change_tasks` L168 / `_h_get_symbol_change_tasks` L1010 | `task_read_handlers.rs::handle_get_symbol_change_tasks` |
| MCP-064 | `audit_verify_chain` | `::audit_verify_chain` L240 / `_h_audit_verify_chain` L1019 | `::handle_audit_verify_chain` |
| MCP-065 | `list_audit_signing_keys` | `::list_audit_signing_keys` L297 / `_h_list_audit_signing_keys` L1031 | `::handle_list_audit_signing_keys` |
| MCP-066 | `bootstrap_status` | `::bootstrap_status` L313 / `_h_bootstrap_status` L1040 | `::handle_bootstrap_status` |
| MCP-067 | `list_clones` | `::list_clones` L390 / `_h_list_clones` L1045 | `::handle_list_clones` |
| MCP-068 | `list_clone_groups` | `::list_clone_groups` L728 / `_h_list_clone_groups` L1059 | `::handle_list_clone_groups` |
| MCP-069 | `get_clone_group_detail` | `::get_clone_group_detail` L749 / `_h_get_clone_group_detail` L1073 | `::handle_get_clone_group_detail` |
| MCP-070 | `task_plan_template` | `::task_plan_template` L940 / `_h_task_plan_template` L1091 | `::handle_task_plan_template` |

---

## 5. 每项子任务的精确修改列表

每个 MCP-001 至 MCP-070 都必须在任务描述中列出以下五类文件；其中 target Rust module 和 Python function 以上表为准。

1. **Python public entry**：`server/tools/<module>.py::<tool>`。保留 MCP 名称、签名和 docstring；将调用改为 `route_rpc(<rpc_method>, params, "READ_ONLY")` 或该模块已有等价 thin-shell 函数。
2. **Python worker retirement**：同一文件内 `_h_<tool>` 和该工具在 `*_READ_ONLY_METHODS` 中的一项；删除该单项注册，不删除 `_bind_readonly_db` 或其他工具。
3. **Rust business handler**：表中指定的 `*.rs::handle_<tool>`。若是该领域首项则创建 module 与 `mod.rs` declaration；SQL 必须带 workspace filter 和 limit clamp；不接受客户端 SQL。
4. **Rust transport/capability**：`rust_ext/src/daemon/dispatch.rs` 的单 method route、`rust_ext/src/daemon/http_server.rs` 的 capability row/route matrix。所有 response 均经过同一 HTTP JSON-RPC envelope。
5. **测试与矩阵**：新增该工具专属 HTTP fixture；更新 route-matrix generator 的输入与生成结果。`tool_migration_matrix.json` 只有在 fixture 与 Reviewer 验收成功后才能把该行从 `python_compat/transition` 变为 `rust_native/migrated`。

---

## 6. 推荐的逐项创建顺序

不要一次性创建 73 个 open 子任务。虽然用户要求“一工具一任务”，也应以**滚动窗口**创建，避免任务树中堆积大量无证据 open 项：

1. 创建 CLI-01、CLI-02、CLI-03；
2. CLI-01 通过独立复审后，仅创建 MCP-001；
3. MCP-001 完成并 Reviewer PASS 后，创建 MCP-002；依此严格串行；
4. 每完成一个工具，立即做 registry/matrix/fixture 三方核验；
5. 在每个模块边界（004、009、014、015、023、028、047、062、070）由 Reviewer 复核该模块 Python compat registration 是否只剩未迁工具；
6. 70 项全部完成后，另立**独立 retirement 评审任务**，而不是在本任务中删除 `db/`、PyO3 或 compatibility worker。

如果必须在一次操作中预建全部任务，任务描述必须额外写明 `status=open but blocked by predecessor`，且绝不可并行领取；但这比滚动创建更容易制造治理噪声，因此不推荐。

---

## 7. 创建前必须确认的事项

当前父任务已 `closed`，而且其原 scope 是 CLI S1；因此在写入前需要用户再次明确确认以下两项：

1. **允许**对 `T-1787209886781-48b4cb0c` 执行必要的 reopen，并将其作为逐工具迁移父任务；
2. **选择创建节奏**：
   - **推荐：** 仅创建 CLI-01（或 CLI-01~03），通过后再逐项创建 MCP-001、MCP-002……；
   - **不推荐：** 一次预建全部 73 个顺序 blocked 子任务。

在获得确认前，本草案不落库、不改变现有 closed/open/review 状态。

## References

[1]: `deliverables/software-company/tool_migration_matrix.json` — 当前 70 个 `python_compat` 工具、模块、RPC 和 batch 的权威矩阵。
[2]: `rust_ext/src/daemon/query_compat_handlers.rs` — 已迁只读 compat 工具的 Rust handler 签名、SQL 过滤和 QueryBudget 先例。
[3]: `server/compat_worker.py`、`server/compat_registry.py` — 现有 Python worker 私有 IPC、readonly registry 与同步白名单。
[4]: `server/tools/tools_*.py` — 上表列出的 MCP wrapper 和 `_h_<tool>` compatibility handler。
[5]: `callwarden_mcp_recovery_task_tree_review.md` 与 `callwarden_bootstrap_gap_analysis_independent_verification.md` — 先控制面、再逐能力切换，以及旧/新任务不可擅自废弃的约束。

**Handoff**

```text
from_role: executor
outcome: executor_blocked_to_user
next_role: user
next_action: 确认是否允许 reopen 已关闭父任务，并选择“滚动创建 CLI-01~03 后逐项创建”或“一次预建 73 个顺序 blocked 子任务”。
reason: 当前父任务是已关闭 CLI S1；直接挂载 70 MCP 子任务将扩大其范围并触发 reopen，必须由用户显式确认。
independence_requirement: not_applicable
```
