# T4 独立评审报告 — claim 硬依赖门禁（E_PREREQUISITE_NOT_MET）

- **Role**: reviewer（柯明察）
- **Task**: T-1795663549865-19deda64（T4 claim 硬依赖门禁）
- **Skill**: code-review-cw（按独立评审纪律执行）
- **独立性**: `independence=local_declaration_new_session`
  - daemon 调用 `check_session_separation` 返回 `E_HTTP_MANIFEST_STALE: manifest PID 46340 已不存活（stale manifest）` → **daemon 不可用**；按纪律本地声明全新身份 `agent=reviewer-t4-9c2f-4d17`、`session=sess-reviewer-t4-9c2f-4d17`，与 executor 的 `inst-t4-exec-9c2f` / `sess-t4-exec-9c2f` 均不同。未复用任何 executor/前会话身份。
- **评审对象**: worktree `C:/git_work/callwarden-worker/runs/impl-t4-claim-9c2f`，分支 `impl/t4-claim`，基线 `origin/master=40a21d4`
- **改动面**: `claim.rs`（+146/-21）、`claim_test.rs`（+268/-6），合计 +393/-21；`git status` 确认仅此两文件修改，无越界改动。
- **只读声明**: 本次未改代码/证据/任务状态，未建整改单，未 apply/close/supersede。本报告为评审产出文档。

---

## 结论：PASS

无 in-scope 🔴 blocker。AC-07 按契约原文应判定为**领域错误层**要求，实现满足；其余五项审查点均核验通过。另有 2 条 🟡（governance 提示 / 证据留痕缺口）与 2 条 💭，均不阻断。

---

## 首要审查点：AC-07 的验收口径判定（逐字引用原文）

### 契约原文

`contracts.md` 卡 5「claim 硬依赖门禁 E_PREREQUISITE_NOT_MET」：

> - 新增错误码常量 `pub const ERR_PREREQUISITE_NOT_MET: &str = "E_PREREQUISITE_NOT_MET";`
> - `hard_unmet` 非空 → `ClaimDomainError::Deterministic { code: ERR_PREREQUISITE_NOT_MET, message 含每个 provider_task_id 与 unmet_reason }`。
> - **出参契约**：阻断时 message 形如 `"task X 存在未满足硬依赖: provider Y (requires_existing, provider_not_completed)；请先完成 provider 任务"`——**错误消息直接指路 provider**。
> - GWT 测试要点 1：Given enabled + 硬依赖未满足 → E_PREREQUISITE_NOT_MET、message 含 provider_task_id、任务状态与步骤零变化（**失败分支 + 无副作用断言**）

`ac-map.md` AC-07：

> | AC-07 | REQ-PREQ-02 | claim 硬依赖未满足 → `E_PREREQUISITE_NOT_MET`，消息含每个 provider task_id 与缺口；无 lease 插入、任务状态不变 | `cargo test` claim 门禁测试 |

`symbol-anchor.md` #10（佐证，非 message 透传要求）：

> | 10 | E_PREREQUISITE_NOT_MET | **new** | 错误码常量（错误码清单参照 `task_prompt/context.rs` 的 `E_TASK_PROMPT_*` 模式） | DaemonRpcError::new(code, msg) 模式 |

### 判定

**AC-07 的「消息含 provider_task_id」指领域错误层（`ClaimDomainError::Deterministic.message`），不是 RPC 出口层的序列化 message。** 依据：

1. 契约把 message 明确写在 `ClaimDomainError::Deterministic { code, message }` 结构体内，这是领域错误类型，不是 RPC 响应对象。
2. 同一张卡对 warning 明确使用「并入**响应 JSON**（`prerequisite_warnings`）」措辞——契约作者清楚区分「领域错误 message」与「响应 JSON」两层，没有混用。因此「错误消息直接指路 provider」应落在领域错误层。
3. `ac-map.md` AC-07 的验收方式是「`cargo test` claim 门禁测试」，与契约 GWT 要点 1 的领域断言语境一致。
4. `symbol-anchor.md` #10 的 `DaemonRpcError::new(code, msg)` 是**错误码常量模式的参照**，不构成「RPC 出口 message 必须含 provider」的约束。

### 实现核验

- `claim.rs::ClaimDomainError::prerequisite_not_met` 构造 `Deterministic { code: E_PREREQUISITE_NOT_MET, message: "task {task_id} step {step_id} 存在未满足硬依赖: {provider} ({dep_type}, {unmet_reason})；请先完成 provider 任务" }`，逐条拼接全部 `hard_unmet`，**含每个 provider_task_id + unmet_reason**，符合契约出参契约。
- 测试 `prereq_hard_unmet_blocks_claim_with_zero_side_effect` 直调 `check_prerequisites_gate` 断言 `message.contains("t-p-open")` / `"t-p-gone"` / `"provider_not_completed"`，覆盖两个 provider 的指路。
- **RPC 出口层**：`executor.rs::stable_error_message` 对 `StableDomainError::DeterministicReject { code }` 统一生成 `"确定性领域拒绝：{code}"`，不含 provider。经基线核对（`git show origin/master:.../executor.rs:166-168`），该收敛逻辑 **T4 之前即已存在**，是 foundation 独占的既有架构约束，所有 claim 领域错误（含 `E_STEP_BINDING_INVALID` 等）一致，`into_outcome()` 同样为基线原样。**非 T4 引入的偏差。**

**结论：此项不构成 finding（不是 in-scope 缺口）。** 若需求方坚持 RPC 出口 message 本身必须含 provider，则需改 foundation 的稳定错误消息载体（如给 `DeterministicReject` 增 `detail` 字段并保持可重放性），超出 T4 scope → 定性为 **governance**（见 🟡-1），不应据此 BLOCK T4。

---

## 覆盖矩阵

| 改动点 | 调用方（静态调用图） | 覆盖 | 缺口 |
|---|---|---|---|
| `check_prerequisites_gate`（gate 本体） | 生产唯一上游 `write_domain`（step 6.5）；测试直调 `claim_test.rs:758` | AC-07 领域层 message 断言 + transport 层 code/零副作用/ledger 断言 | RPC 出口 message 不含 provider（foundation 既有收敛，非 T4 缺口） |
| `claim_step_inner` 的 `gate_enabled` 参数化 | 生产 `claim_step` → `capability_enabled()`（恒 true）；测试注入 false | AC-10 disabled 与 baseline 响应逐字段相等 + binding 数=1 | 生产运行时 enabled=false 路径不可达（v1 契约即恒 true，非缺口） |
| `write_domain` step 6.5 插入点 | `apply_claim` → `TaskMutationExecutor::run` | AC-07/08/09 三分支 + 异常矩阵 #5 测试全绿 | step1-6 各只读步骤未逐一做「无写」断言，已静态确认全为 SELECT |
| `prerequisite_warnings` 响应键 | `claim_step` RPC 出口 → `claim_test.rs:848` | AC-09 shape（provider_task_id/unmet_reason/dep_type/freshness_status）断言 | 上层 MCP/CLI 透传未做端到端验证 |
| 引擎 DB 失败 → Infrastructure | `evaluate_task_prerequisites` → `ClaimDomainError::infra` → `DomainOutcome::RollbackInfrastructureError` | 异常矩阵 #5：DROP TABLE 注入，断言 binding=0、ledger=0 | 引擎内 artifact/interface 表读失败未逐一注入（T1 单测覆盖，T4 未重复） |
| `claim_step` 公开签名 | 全局调用方：task_prompt/context_tests、inbound_handoff_test、rpc_tests、next_action_test、report_handoff_test、verdict_evidence_gate_test 等 | 签名保持 4 参数不变，全部存量测试绿 | 无（调用图搜索确认无签名破坏） |

---

## 五项审查点核验结论

**1. 门禁插入点语义（step6 binding 链校验后、step7 INSERT 前）** ✅
`write_domain` step1-6 全部为 `SELECT`（workspace binding、step 归属、remediation、revision、lineage、binding 链），gate 在 step7 `INSERT` 之前执行。gate 失败时 `ClaimDomainError::Deterministic` → `into_outcome()` → `DomainOutcome::CommitDeterministicError` → foundation `executor.rs:105-129` 执行 `ROLLBACK TO task_domain_callback`，callback 内局部写入全部撤销。**无部分写入**。测试断言 `count(task_step_role_contract_bindings)==0` + ledger 落可重放 error，与契约「dry_run=true 无副作用；实际路径外层事务回滚」一致。

**2. enabled=false 逐字节无行为变化 / 参数化风险** ✅
`claim_step` 签名（4 参数）未变，新增 `claim_step_inner`（pub(crate) + `#[doc(hidden)]`）仅注入开关；生产唯一入口传 `capability_enabled()`（prerequisites.rs:70-72，恒 true）。全局 grep 确认所有 `claim_step(` 外部调用方签名兼容。`prereq_disabled_bypasses_gate_and_matches_baseline` 用 `assert_eq!(resp_disabled, resp_baseline)` 逐字段比对，且断言无 `prerequisite_warnings` 键。无签名/调用方风险。轻微可见性扩大记为 💭-1。

**3. AC-08 / AC-09 分支逻辑与 warning shape** ✅
AC-08：`prereq_hard_satisfied_allows_claim` provider=closed → `ok=true`、binding=1、无 warnings 键。AC-09：`prereq_informational_unmet_warns_but_allows` is_hard=0 未满足 → 放行 + `prerequisite_warnings` 数组含 `provider_task_id`/`unmet_reason`/`dep_type`/`freshness_status`，与卡 1 `PrerequisiteItem` 同构。契约仅要求「并入响应 JSON」，未规定元素 schema，实现采用全字段是合理超集。

**4. 异常矩阵 #5（引擎 DB 读失败）** ✅
`evaluate_task_prerequisites::read_declarations`（prerequisites.rs:152-180）对 prepare/query_map/行读取均映射 `DaemonRpcError::internal_error`；gate 经 `ClaimDomainError::infra` → `Infrastructure` → foundation 外层事务整体回滚。测试用 `DROP TABLE task_dependencies` 注入，断言 `err.code == E_TASK_DB_TRANSACTION`、binding=0、ledger result 行数=0。**零部分写入 + Infrastructure 错误**，符合 fail-closed。

**5. 调用图影响半径 / T1 符号稳定性** ✅
- `check_prerequisites_gate` 生产上游仅 `write_domain`（claim.rs:529），测试上游 1 处；无其他模块直调，影响半径封闭在 claim 领域内。
- `capability_enabled()` 来源 `prerequisites.rs`（基线已存在，非本次改动），`evaluate_task_prerequisites` / `PrerequisiteItem` 同属 T1 交付。本次 diff 未改 prerequisites.rs，符号稳定；最终轮 57 passed 证明编译链接通过。
- daemon 图查询不可用（manifest stale），以静态 grep 调用图替代；此为**方法学降级**，已在报告注明。

**6. 断言质量（MASKING 判据）** ✅
- 新测试无 `if is_err() { assert_eq!(code, X) }` 恒真模式；AC-07 用 `match ... { Err(Deterministic{..}) => 断言, other => panic! }` 兜底，非恒真。
- `prereq_hard_unmet_blocks_claim_with_zero_side_effect` 的 `message.contains(...)` 断言**可再红**：删除 `prerequisite_not_met` 中 provider 拼接内容即失败。双层断言（领域层 message / transport 层 code+副作用）分工明确，不是用弱断言掩盖缺口。
- 首轮红→绿的修复在代码层面合法：领域 message 由 gate 正确产生；transport 层只验稳定 code 符合 foundation 既有语义。

---

## Findings

- 🟡 **[governance]** `task_loop/executor.rs::stable_error_message`（基线既有，非 T4 改动）— RPC 出口对 `DeterministicReject{code}` 统一生成 `"确定性领域拒绝：{code}"`，不含 provider_task_id。若验收方将 AC-07 的「消息含 provider」理解为 RPC 出口 message，则需在 foundation 扩展稳定错误载体（`DeterministicReject` 增加可重放 detail 或专用变体），属独立治理项。**建议动作**：由 planner 判定是否需要 foundation 修复单；如需，单独立单，不并入 T4。

- 🟡 **[证据缺口]** `.t4_test.log` 仅 1346 行、47745 字节，只保留最终轮（`test result: ok. 57 passed; 0 failed`，CARGO_RC=0）；5 个新测试 `prereq_*` 均 ok（line 1304-1308）。team-lead 交接中所述「首轮 line 2770 FAILED 56/1 / 143KB 日志」在当前 run 目录不存在——全目录扫描无 143KB 测试日志、无第二条含 FAILED 的日志。**影响**：无法从日志复核首轮红；但代码层断言可再红性已静态验证，最终轮全绿可复核。**建议动作**：保留首轮日志原件（或测试记录）作为过程证据，供 adjudicator 核验红→绿链路。

- 💭 **[nit]** `ClaimDomainError` 与 `check_prerequisites_gate` 为供测试断言而从 private 提升为 `pub(crate)`。可见性扩大仅限 crate 内，风险低；可选做法是把领域 message 断言移到模块内 `#[cfg(test)]` 以缩小公开面。

- 💭 **[nit]** T4 新增的 5 个测试仅使用 `requires_existing` dep_type；`requires_artifact` / `requires_interface` / `unknown_dep_type` 在 claim 门禁下的放行/阻断行为由 T1 单测覆盖，T4 未重复注入。可选补一条 artifact-stale 的端到端门禁测试。

---

## 未覆盖路径

1. **RPC/MCP 端到端**：`task.claim` 经 dispatch/route 到 `claim_step` 后，provider 信息是否在 MCP/CLI 层以其他字段对调用者可见——本次验证止于 `DaemonRpcError` 出口，未做 MCP 序列化端到端。
2. **引擎其他 dep_type 的门禁分支**：`requires_artifact`（fresh/stale/unresolved）、`requires_interface`、`unknown_dep_type` 在 claim 门禁下的行为——依赖 T1 单测，T4 未重复验证。
3. **生产 enabled=false 路径**：`capability_enabled()` 恒 true，只能通过 `claim_step_inner` 注入 false；真实运行时开关切换未验证（契约约定 v1 无运行时开关）。
4. **引擎内其余表读失败**：`artifact_identities` / `interface_provider_selections` 读取失败的回滚路径——T4 仅注入 `task_dependencies` DROP TABLE。
5. **首轮红的原始日志**：当前 run 目录无首轮 FAILED 留痕，红→绿链路仅凭 executor 披露与代码静态可再红性支撑，未经日志复核。
6. **daemon 调用图查询**：因 `E_HTTP_MANIFEST_STALE` 不可用，影响半径以静态 grep 调用图替代，未用 daemon 图数据交叉验证。

---

## Handoff

`PASS` → 交 adjudicator。governance 🟡-1 与证据缺口 🟡-2 请 planner 决定是否单独处理，不阻断 T4 关单。
