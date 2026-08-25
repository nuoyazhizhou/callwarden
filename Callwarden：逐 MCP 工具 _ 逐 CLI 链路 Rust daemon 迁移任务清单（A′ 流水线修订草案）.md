# Callwarden：逐 MCP 工具 / 逐 CLI 链路 Rust daemon 迁移任务清单（A′ 流水线修订草案）

**状态：** 仅为任务树草案；**尚未创建、supersede、reopen 或修改任何 CW 任务**。

**父任务策略：** 不 reopen 已关闭的 `T-1787209886781-48b4cb0c`（S1 follow-up）。先在开放的祖父 Epic `T-1787203926824-9f873bfc` 下新建一个专用的“Rust daemon MCP 恢复与逐链路切换”父任务；CLI-01~03 与 MCP-001~070 均挂载于该新父任务。

**历史任务策略：** 旧 S2 与重建 S2 的同 scope 双 ownership 先通过可审计的 `supersede` 关系收口，不删除、不伪造 closed、不让新父任务继承其未经核验的完成度；矩阵中已为 `rust_native` 的 9 个 compat 工具只作为现状基线。

**本轮角色：** `executor / planner / T-1787209886781-48b4cb0c / Skill: none`。本草案的下一执行任务应在 Phase 1 创建后重新绑定到新父任务。

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

### 1.1 父任务与历史任务收口

`T-1787209886781-48b4cb0c` 是已关闭的 **S1：`cli/main.py` 迁移 daemon RPC** 闭环任务，不是 MCP 工具迁移 Epic。它必须保持为历史证据，而非被 reopen 后塞入 70 个新子任务。

正确的 Phase 0 / Phase 1 顺序如下：

1. **Phase 0 — supersede 收口。** 由具备任务治理权限的 Executor 记录旧 S2 与重建 S2 的替代关系，明确“9 个已迁 native 工具已并入当前矩阵基线、未迁 70 项归新恢复计划”，不删除旧任务、不改变 S3 的 `open` 状态。
2. **Phase 1 — 建立新父任务。** 在 `T-1787203926824-9f873bfc` 下创建 `Rust daemon MCP 恢复与逐链路切换（A′ 流水线）`。该任务只管理 CLI 控制面与 70 条兼容读链路；S3 保持独立 retirement 项，不能作为前置或子任务。
3. **Phase 2 — A′ 滚动建卡。** CLI-01 和各 MCP 任务以单条链路为最小 scope，在新父任务下按 A′ 模型滚动创建。

每个新子任务都必须表明其 `port_type`、`port_key`、前置 gate 与 successor rule；不得把新任务与旧 S1/S2/S3 的状态混写。

---

## 2. 所有子任务必须复制的固定合同

每一个任务均应写入以下不变内容；实际创建时只替换工具名、文件和函数。

| 项目 | 强制内容 |
|---|---|
| **Role Contract** | `executor / implementer`，handoff 至 `reviewer / independent_reviewer`；执行者不得 `apply/close`。 |
| **输入契约** | 保持既有 MCP 工具名、参数名、默认值、返回 JSON shape 与 stable error code；除非单独兼容任务批准，否则不能重命名 RPC。 |
| **Rust handler** | 使用 `pub fn handle_<tool>(conn: &Connection, workspace_id: i64, params: &Value) -> Result<Value, DaemonRpcError>` 或已有 readonly service 的等价签名；禁止客户端 SQL、字符串拼接 SQL 条件和 local DB fallback。现有 `query_compat_handlers.rs` 是函数签名与 SQL porting 的参考。 [2] |
| **port_type / port_key** | 每项必须声明一个稳定端口类型和资源键：`control_plane`（CLI）、`task_projection`（collab/task）、`graph_snapshot`（P2/query）、`identity_attestation`（P3）、`assignment_projection`（P4）、`semantic_projection`、`summary_projection`、`repository_security`、`lsp_session`。`port_key` 必须列出所争用的 Rust module、Python tool module、dispatch、capability registry 和 fixture family。 |
| **Python 变更** | 保留 MCP wrapper 函数，改为纯 `route_rpc()`/HTTP 适配；移除该单工具的 `_h_<tool>` 与其在 `*_READ_ONLY_METHODS`/`register_compat_routes` 中的行。不得删除同模块其他工具 handler。 |
| **Rust 路由** | 新增或修改仅该工具的 `DaemonState::handle_<tool>` 和 `dispatch.rs` method branch；`http_server.rs` capability registry 将该工具从 `python_compat` 更新为 `rust_native`。 |
| **真相源同步** | 更新生成器输入与 `tool_migration_matrix.json` 中该工具的 `target_backend=rust_native`、`status=migrated`、`batch=MCP-PER-TOOL-<NN>`；**不手工伪造** `mcp-tools-implementation-map.md`，应由其生成入口刷新。 |
| **测试** | 新建 `tests/test_mcp_<tool>_http_rpc.py`（或按同目录既有 fixture 扩展）：成功、非法参数、未知/未授权 workspace、daemon unavailable fail-closed、daemon restart 后同输出；对纯 SQL port 再增加 Python compat golden parity。 |
| **禁止项** | 不改 `db/schema.py`；不改 `task_collab.rs` 的治理写；不改 lease/assignment 语义；不把 worker 写权限扩展；不为“测试通过”保留 hidden SQLite fallback。 |
| **证据** | 记录 Git commit、Rust test、Python进程级 HTTP fixture、daemon binary/source fingerprint、capability registry row、矩阵行；报告前确认本工具不再在 Python compat registry 注册。 |

### 2.1 A′ 流水线：执行、审查与裁决解耦

共享热文件仍要求**单一 Executor 写入**，但不再要求“Task N 必须 Reviewer PASS 后才能创建或执行 Task N+1”。A′ 流水线将生产和审查解耦：

```text
Executor 执行 Task N（独占热文件）
  → report 并推进 review
  → 立即按滚动窗口创建 / 执行 Task N+1
Reviewer 异步消费 review queue
  → PASS：交 Adjudicator apply
  → BLOCKED：进入 blocked queue
Executor 在当前生产轮结束后批量返工 blocked queue，并重新推进 review
```

| 约束 | 自动推进规则 |
|---|---|
| **单写者约束** | 任一时刻只有一个 Executor 可修改共享 hot files：`dispatch.rs`、`http_server.rs`、`compat_registry.py`、route generator 输入与矩阵。Reviewer/Adjudicator 只读或按治理权限 apply，不改实现。 |
| **滚动窗口** | 正常工具完成并进入 `review` 后，Executor 可立即创建并执行同一全局单写队列的下一条任务，不等待 verdict。默认窗口为 `1 executing + 1 review + 1 next-created`。 |
| **Gate 约束** | Gate 任务未被 Adjudicator `apply` 前，禁止创建其同 `port_type` 的后继；若 Gate 得到 BLOCKED，立即停止该 port 的下游建卡，但可继续其他已获准端口。 |
| **Blocked 批处理** | BLOCKED 不是人工等待点。系统将其放入 blocked queue；Executor 在当前窗口清空或达到批次边界时领取整改，修复后重新 report/review。不得由 Reviewer 自建整改任务或改代码。 |

Gate 清单固定为：`CLI-01`，以及每个新端口的首卡 `MCP-001/005/010/015/016/024/029/048/054/063`。它们分别验证 collab、dependency、identity、assignment、query、semantic、summary、security、LSP 与 task projection 的首个 native port。每个 Gate 任务必须有 `gate=true`、`port_type`、`port_key` 和明确 `successor_rule` 字段。

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

## 6. A′ 有序写入序列与无人值守建卡

### 6.1 Phase 0 — 历史 S2 supersede 收口

由 Executor 在不删任务、不改代码的前提下，写入旧 S2 / 重建 S2 的替代关系和现状基线：`9 native + 70 python_compat`。Phase 0 的验收是任务关系、矩阵时间戳与 scope 都可读出；**不是**关闭、删除或假装旧 S2 已完成。

### 6.2 Phase 1 — 新建 A′ 恢复父任务

在 `T-1787203926824-9f873bfc` 下创建新父任务，并写入：目的、70 项初始基线、S3 为 retirement、A′ state machine、port_type 枚举、gate 清单、single-writer 约束与 blocked queue 规则。旧 `T-1787209886781-48b4cb0c` 不 reopen。

### 6.3 Phase 2 — A′ 滚动建卡

1. 建立 `CLI-01`（`control_plane`，Gate），由 Executor 实施并推进 `review`；
2. `CLI-01` 进入 `review` 后，Executor 可创建 `CLI-02`，但不得创建第一个 HTTP MCP port，直到 `CLI-01` 获 Adjudicator `apply`；
3. 每个 port 的 Gate 获 `apply` 后，Executor 按全局单写队列创建该 port 的下一个任务；Reviewer 的 PASS 不阻断其他已获准 port 的生产；
4. Reviewer 的 BLOCKED 将任务送入 blocked queue；该 `port_type` 立即停止下游创建，其他无关且已获准 port 可继续；
5. Executor 在一个生产窗口结束时，按最早 BLOCKED 任务批量整改、重新提交 review；
6. 只有 Adjudicator 才能对 PASS 执行 apply；只有 parent/status policy 允许时才执行 close。

### 6.4 推荐的初始窗口

第一窗口只创建：`CLI-01`。它 `review` 后再创建 `CLI-02`；`CLI-01` apply 后创建 `CLI-03` 与 `MCP-001`。这既避免 73 个 open 任务，也避免“每个任务等待人工 PASS”造成无人值守死锁。

### 6.5 模块边界复核

`MCP-004/009/014/015/023/028/047/053/059/062/070` 为模块或子端口边界。对应末项进入 review 时，Reviewer 额外核验该模块 compat registry 只保留未迁工具；该复核不阻止已经获 apply 的其他 port 继续。

### 6.6 S1-review 与 CLI-01 的边界

旧 S1 review 任务 `T-1787203937193-0993d120` 保持为历史 CLI/db 下沉审查；`CLI-01` 只解决 manifest、health、capability 和真实 HTTP 可观测性。它不得重做 `cli/main.py` 的 296 处引用清理，也不得以修复 manifest 为由重开旧 S1。若发现 CLI-01 需要修改旧 S1 的代码范围，应将该具体缺口放入 blocked evidence，交由用户或 Adjudicator 决定是否创建独立 superseding CLI 任务。

---

## 7. 创建前仍需确认的写入动作

本草案已不再请求 reopen `T-1787209886781-48b4cb0c`。实际写入前只需用户确认：

1. 允许 Phase 0 以任务事件或关联方式记录 S2 supersede，而不删除或改写历史证据；
2. 允许在 `T-1787203926824-9f873bfc` 下创建新的 A′ 恢复父任务；
3. 允许按 Phase 2 仅创建第一窗口的 `CLI-01`，后续由 A′ gate 和队列规则滚动建卡。

在获得确认前，本草案不落库、不改变任何 closed/open/review/applied 状态。

## References

[1]: `deliverables/software-company/tool_migration_matrix.json` — 当前 70 个 `python_compat` 工具、模块、RPC 和 batch 的权威矩阵。
[2]: `rust_ext/src/daemon/query_compat_handlers.rs` — 已迁只读 compat 工具的 Rust handler 签名、SQL 过滤和 QueryBudget 先例。
[3]: `server/compat_worker.py`、`server/compat_registry.py` — 现有 Python worker 私有 IPC、readonly registry 与同步白名单。
[4]: `server/tools/tools_*.py` — 上表列出的 MCP wrapper 和 `_h_<tool>` compatibility handler。
[5]: `callwarden_mcp_recovery_task_tree_review.md` 与 `callwarden_bootstrap_gap_analysis_independent_verification.md` — 先控制面、再逐能力切换，以及旧/新任务不可擅自废弃的约束。

**Handoff**

```text
from_role: executor
outcome: executor_ready_for_user_authorization
next_role: user
next_action: 确认 Phase 0 supersede 记录、Phase 1 在祖父 Epic 下新建 A′ 恢复父任务，以及 Phase 2 仅创建 CLI-01。
reason: A′ 流水线已将 Executor 生产、Reviewer 异步消费、Adjudicator apply 与 Blocked 批量返工解耦；不再 reopen 已关闭 S1 follow-up，也不一次预建 73 个任务。
independence_requirement: not_applicable
```
