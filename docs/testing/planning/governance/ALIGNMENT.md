# GAP-15 语义差异与对齐设计（TP-04 ALIGNMENT）

> 任务：`T-1791627786186-a562f188`（TP-04 GAP-15语义差异与对齐设计）
> Step 0 交付物：本文（权威文档 × 实现 × 静态测试合同的比较基线）
> 机器账本：`differences.json`（schema `governance-diff/v1`）
> 约束：静态规划，只读源码与权威文档；不运行产品测试/构建/联网；不把静态测试写成运行证明。

## 1. 比较范围与方法

比较对象三组：

| 组 | 角色 | 清单 |
|---|---|---|
| 权威文档 | 协议侧（声称"应当如何"） | `AGENTS.md`、`.agents/skills/cw-task-loop/references/role-protocol.md`、`docs/design/cw-role-handoff-task-loop-v2-amendment.md` |
| 实现源码 | 行为侧（实际如何） | `rust_ext/src/daemon/task_collab_lifecycle.rs`、`task_collab_lease.rs`、`task_collab_shared.rs`、`task_loop/claim.rs`、`task_loop/next_action.rs`、`task_loop/report_handoff.rs`、`cli/main.py`、`server/tools/tools_collab.py` |
| 静态测试合同 | 契约侧（要求如何） | `rust_ext/src/daemon/task_collab_tests_core.rs` |

方法：逐文件读取 → 精确定位语义声明与行为代码 → 抽取行号区间与原文摘录 → 磁盘 sha256 绑定 → 登记为 `differences.json` 的差异项/一致项。所有引用均可由 `differences.json` 的 `sha256` + `lines` 复算复核。

基线：`source_revision=db952964...`，与 `docs/testing/registration/source_baseline.json` 的 `head_reference` 一致。

## 2. GAP-15 核心差异（D01）：adjudicator_returned 是否自动追加整改 step

### 2.1 协议侧声称（3 份文档、9 处引用，一致否定）

三份权威文档**完全一致**地声称：`adjudicator_returned` 退回**只持久化 handoff 并固定路由 executor，不自动追加 step、不自动 reopen**；自动 remediation **仅** `reviewer_blocked` 触发。

| 文档 | 位置 | 声称要点 |
|---|---|---|
| `AGENTS.md` | 155–159（状态推进第 5 条） | daemon 仅固定路由 Executor，"不自动追加整改 step——自动 remediation 仅 `reviewer_blocked` 触发"；当前 CLI/MCP 尚无受支持的 provenance-bound remediation bridge |
| `role-protocol.md` | 13–17（顶部 capability 分层） | `adjudicator_returned` **不在此列**：只固定路由 executor，不自动追加 step/reopen；CLI 无命令、MCP 不满足结构化 findings 契约 |
| `role-protocol.md` | 34（§1 角色表） | Adjudicator 当前退回固定路由 Executor 但 bridge 未闭合 |
| `role-protocol.md` | 108–113（§3 双轨整改） | daemon 仅持久化 handoff 并固定路由 executor（不自动追加 step、不自动 reopen）；无受支持 bridge，不得自行声称 `remediation_pending` |
| `role-protocol.md` | 122–127（§3 计划缺陷分支） | adjudicator_returned 后无受支持 remediation bridge，不得假装存在可领取的 Executor step |
| `role-protocol.md` | 189–198（§4） | 只持久化 handoff；**并明称** "`task_collab_lifecycle.rs` 自动整改分支仅匹配 `reviewer_blocked`"；CLI 无对应命令、MCP adapter 把 `source_findings` 作为字符串透传而 daemon 要求 JSON array |
| `v2-amendment` | 61–64（§3.1） | `adjudicator_returned` 仅固定路由 Executor，不自动追加 step/reopen；CLI/MCP 尚无受支持的端到端 remediation bridge |
| `v2-amendment` | 75（§3.2 pre-cutover 行） | 因当前无受支持 remediation bridge，重新查询并如实记录治理实现缺口，不得合成 Executor step |
| `v2-amendment` | 113–118（§4 双轨整改） | daemon 仅持久化 handoff 并固定路由 executor（不自动追加 step、不自动 reopen） |

### 2.2 实现侧行为（6 个文件、9 处证据，一致肯定）

daemon 实现**系统性地**把 `adjudicator_returned` 与 `reviewer_blocked` 当作同一类可整改 outcome：

| 文件 | 行 | 行为 |
|---|---|---|
| `task_collab_lifecycle.rs` | 1521–1524 | `handle_task_handoff` 自动整改分支：`matches!(outcome, "reviewer_blocked" \| "adjudicator_returned")` —— **两种 outcome 都进入同事务 remediation 计算** |
| `task_collab_lifecycle.rs` | 1591–1595 | source verdict 取值：`reviewer_blocked→"block"`，其余（即 adjudicator_returned）→`"pass"`；两类 verdict 各自作为 remediation 的 source |
| `task_collab_lifecycle.rs` | 1720–1739 | findings 契约：`adjudicator_returned` 允许空数组（pass verdict 可无 finding），`reviewer_blocked` 必须非空——为 adjudicator_returned **专门留了通道** |
| `task_collab_lifecycle.rs` | 1782–1794 | `remediation_step_id` 对两种 outcome 同样生成（`S-<sha256[..24]>`），不区分 outcome |
| `task_collab_lifecycle.rs` | 638–646 | 显式 `task.remediation.create` handler：`source_outcome` 接受 `failed_step`/`reviewer_blocked`/`adjudicator_returned` 三者 |
| `task_collab_lease.rs` | 40–41 | lease 判定：`source_outcome ∈ {reviewer_blocked, adjudicator_returned}` 选中 remediation lease |
| `task_collab_shared.rs` | 311–312 | 共享判定：匹配两者 |
| `task_loop/claim.rs` | 513–514 | claim 判定：匹配两者 |
| `task_loop/next_action.rs` | 600–601 | next_action provenance 判定：匹配两者 |

这不是单点分支，而是 lease / claim / next_action / provenance / 显式 handler 全链路一致的设计。

### 2.3 静态测试合同（要求肯定）

`task_collab_tests_core.rs:722–921` 的
`test_adjudicator_returned_handoff_reopens_executor_remediation_atomically`：

- `expect("adjudicator_returned 应原子创建 Executor remediation")`
- 响应 `status == "in_progress"`、存在 `remediation_step_id` 与 `assignment_id`
- 落库 step：`action == "fix_defect"`、`status == "pending"`
- metadata 绑定：`source_outcome == "adjudicator_returned"`、`source_verdict_id == "V-ADJ-RETURNED-1"`、`remediation_of_step_id == source_step_id`
- 幂等：相同 `request_id` 重放返回 `replayed == true` 且 `remediation_step_id` 不变
- 投影：active assignment 恰 1 条，role 为 executor，绑定该 remediation step

### 2.4 冲突定性

**`protocol_claims_negative_vs_implementation_and_test_positive`**：协议三文档 9 处声称"不自动追加"，实现 6 文件 9 处"自动追加"，静态测试合同"要求自动追加"。协议落后于实现。

`runtime_status: unknown` —— 静态规划阶段不运行产品测试；该静态测试在当前 daemon 二进制上是否通过属运行验证（TP-09/11G 范围），本账本只登记源码级语义差异，不把静态测试写成运行证明。

### 2.5 目标语义候选（两个方向，均待项目现行权威审批）

| 项 | 方向 A：批准实现语义 | 方向 B：批准协议语义 |
|---|---|---|
| 语义 | `adjudicator_returned` 与 `reviewer_blocked` 均同事务追加 provenance-bound `fix_defect` | `adjudicator_returned` 仅固定路由 executor，不自动追加 step |
| 文档改动 | `AGENTS.md:155-159`、`role-protocol.md` 6 处、`v2-amendment` 3 处全部改写；删除"bridge 缺失"表述或降级为仅显式入口缺口 | 无 |
| 产品改动 | 无（实现与静态测试不变） | `task_collab_lifecycle.rs:1521-1524` 移除 `adjudicator_returned`；显式 handler:638-646 移除该 outcome；lease/claim/next_action/shared 四处同步收窄；静态测试改为断言拒绝 |
| 下游 | TP-05 同步全部现行权威文本 | 必要产品修复按 P-FIX 加入 TP-09/11G 前置；Planner 另冻 daemon/client 独立任务及回归合同 |

**审批边界**：TP-04 只登记差异并定义目标语义候选，不代替裁定。审批事件来源在未来合同绑定（卡片原文：设计裁定由项目现行权威审批，Planner 不冒充 Adjudicator）。

## 3. 桥接子差异（D02）：CLI/MCP 显式入口

协议 §4:193-198 关于"不存在角色可合法使用的端到端 bridge"的声称是**部分成立**的：

| 维度 | 协议声称 | 实现 | 判定 |
|---|---|---|---|
| CLI 显式命令 | 无对应命令 | `cli/main.py` 无 `cw task remediation` 子命令（仅 `--remediation-step-id` claim 参数与 `step-resolve`） | **一致** |
| MCP adapter | 把 `source_findings` 作为字符串透传，daemon 要求 JSON array | `server/tools/tools_collab.py:345` `task_remediation_create` 的 `source_findings: str = ""`；daemon handler:652 取 `Value` | **一致** |
| daemon 内部自动路径 | （隐含：不支持） | `task.handoff` 内部对 `adjudicator_returned` 同事务追加 fix_defect | **冲突** |

即："bridge 缺失"仅在 **CLI/MCP 显式入口**维度成立；在 **daemon 内部自动路径**维度不成立。D02 依赖 D01 的裁定方向：若批准方向 A，D02 的协议表述需改写为"显式入口缺失，自动路径可用"。

## 4. 已核实一致项（非冲突，作为对齐基线）

| 项 | 内容 | 证据 |
|---|---|---|
| AL-01 | 六种已实现 outcome 路由表一致：CLI `_STRUCTURED_HANDOFF_ROUTES`（cli/main.py:3529-3536）与 daemon `outcome_route`（report_handoff.rs:246-256）的 (from_role,next_role,independence) 三元组完全一致，与协议 §5 已实现集一致 | 双侧逐项比对 |
| AL-02 | 三种 design-only outcome（`planner_ready_for_execution`/`planner_replan_required`/`executor_replan_requested`）在 daemon rust_ext 源码与 CLI 路由表中**均无匹配**，`task.handoff` 经 `outcome_route` 返回 `None` 拒绝 | grep 全仓零命中 + 路由表 |
| AL-03 | `runtime_role` 把 `planner` 映射为 `executor`（report_handoff.rs:259-266），与 v2-amendment §2 的 pre-cutover 预期行为声明一致（预期行为，非缺陷） | 双侧比对 |
| AL-04 | capability 三件套（`planner_governance_v1`/`decision_request_v1`/`adjacent_relation_v1`）按文档一致登记为"未声明"；协议保留 workflow_status 值 daemon 不 emit | 协议 6-25 + amendment 55-68 |
| AL-05 | `reviewer_blocked` 自动整改"已实现、无 capability 门禁"：协议顶部、amendment §3.1 与实现 1521 的 `matches!` 一致 | 三侧比对 |

## 5. 比较结论

1. **GAP-15 是唯一的核心语义冲突**，且是"协议落后于实现"型：实现与静态测试合同已把 `adjudicator_returned` 当作可自动整改 outcome，三份权威文档仍统一声称不支持。
2. 冲突深度足够：不是单点分支，而是 lease/claim/next_action/provenance/显式 handler 全链路（6 文件 9 处）与静态测试合同的一致设计，回退到方向 B 的产品改动面较大。
3. 一致项（AL-01~AL-05）证明冲突是**局部的**：outcome 路由表、design-only 拒绝、runtime_role 映射、capability 分层、reviewer_blocked 门禁双侧均一致，对齐工作可聚焦在 adjudicator_returned 单点。
4. `runtime_status=unknown`：静态规划不宣称运行结论。D01 的运行验证（静态测试是否在当前 daemon 二进制通过）属 TP-09/11G；目标语义的正式审批属项目现行权威。

## 6. 目标语义定义（Step 1）

> 本节定义"已声明 capability/profile"的目标语义（target semantics）与显式审批边界。
> 设计裁定由项目现行权威审批，Planner 不冒充 Adjudicator；审批事件来源在未来合同绑定。

### 6.1 已实现 vs 未声明 capability 的目标语义

实现侧唯一**已实现并自带 capability 投影**的是 `role_prompt_compiler_v1`（`task_prompt/handler.rs:44`，经 `dispatch.rs:316` 暴露 `capability_projection()`：`name`/`schema_version`/`manifest_sha256`/`compiler_policy_sha256`/`enabled`）。它定义了本仓 capability 声明的**机械标准**：capability 有名字、schema 版本、build-generated manifest hash、enabled 状态与 health 投影入口。

**目标语义（已声明 capability 侧）**：

| capability | 当前状态 | 目标语义（冻结供审批） |
|---|---|---|
| `role_prompt_compiler_v1` | 已实现、有 capability 投影 | 保持：production prompt 唯一来源；未启用时 public RPC fail closed；投影字段不可被客户端合成 |
| `planner_governance_v1` | 未声明 | 目标：Planner 原生派工（`READY/PLAN`、`planning_*`/`replanning_*`/`execution_ready` 投影、三种 planner outcome 持久化）。声明前：`task.next_action` 不产 `READY/PLAN`；`task.create`/`--role-contracts` 拒绝 planner；`task.handoff` 拒绝三种 design-only outcome（双侧一致，AL-02） |
| `decision_request_v1` | 未声明 | 目标：`decision_request`/`decision.respond` 落库与 `waiting_for_decision`/`waiting_for_input` 投影。声明前不落库、不投影 |
| `adjacent_relation_v1` | 未声明 | 目标：`adjacent_defect → related_to` 自动关联。声明前由人工在 finding/ledger 记录 |

**`adjudicator_returned` 不是 capability 门控项**——它是"当前已实现"集的成员（协议 §5 已实现六种 outcome 之一，AL-01 双侧一致）。GAP-15 冲突的**本质**因此不是"capability 未声明"，而是**已实现行为的语义在协议层被错误描述**：协议把一个已实现的能力（自动追加 fix_defect）写成"不支持"。

### 6.2 目标语义候选的精确表述（供审批）

卡片要求"定义已声明 capability/profile 的目标语义"，且成功标准是"得到明确批准的语义与差异责任"。以下是两个方向各自冻结的、可审批的完整语义表述。

#### 方向 A：批准实现语义

**A-1（自动整改语义）**：`reviewer_blocked` 与 `adjudicator_returned` 两种退回，daemon 均在同一主任务原子追加一条 provenance-bound `fix_defect` step，绑定 `source_verdict_id`、`source_handoff_request_id`、`remediation_of_step_id`；任务回 `in_progress` → `remediation_pending` → Executor 领取 → `remediation_in_progress`。

**A-2（findings 契约差异保留）**：`reviewer_blocked` 的 source block verdict 必须携带至少一个结构化 finding（非空数组）；`adjudicator_returned` 的 source pass verdict 允许空 findings 数组（实现 1720-1739 已为此留通道）。这是两路径唯一刻意的行为差异，语义上正确（裁决退回可只指向证据缺口，不必须带 block finding）。

**A-3（幂等）**：同一 `source_verdict_id` + 同一 `request_id` 重放返回原 `remediation_step_id`（`replayed=true`）；同一 verdict 不同 request_id 被拒绝（`E_REMEDIATION_ALREADY_EXISTS`）。

**A-4（bridge 表述改写）**：协议"无可用 bridge"的表述收窄为**仅 CLI/MCP 显式入口**维度：CLI 无 `cw task remediation` 子命令（事实）；MCP `task_remediation_create` 的 `source_findings: str` 与 daemon 的 JSON array 契约不一致（事实）；daemon 内部自动路径完整可用（事实）。

**A 的文档责任**：`AGENTS.md:155-159`、`role-protocol.md` 6 处、`v2-amendment` 3 处共 10 段需改写（TP-05 执行）；删除或改写"bridge 缺失/不自动追加"表述。**A 无产品修复**——实现与静态测试不变。

#### 方向 B：批准协议语义

**B-1**：`adjudicator_returned` 仅持久化 handoff 并固定路由 executor，不自动追加 step/不自动 reopen。

**B-2**：`handle_task_handoff` 的 `matches!`（1521-1524）移除 `"adjudicator_returned"`；显式 handler（638-646）的 `source_outcome` 白名单移除该值；`task_collab_lease.rs:40`、`task_collab_shared.rs:311`、`claim.rs:513`、`next_action.rs:600` 四处同步收窄。

**B-3**：静态测试 `test_adjudicator_returned_handoff_reopens_executor_remediation_atomically`（722-921）改写为断言**拒绝**（无 remediation step、状态不变）。

**B 的产品责任**：必要产品修复按 P-FIX 加入 TP-09/11G 前置；Planner 另冻 daemon/client 独立任务（ownership=`rust_ext/src/daemon/task_collab*.rs` + `task_loop/{claim,next_action}.rs` + 测试）及回归合同。**B 无文档语义改写**（协议不变）。

### 6.3 显式审批边界（approval boundary）

以下边界由本任务冻结，供项目现行权威审批时参照：

1. **TP-04 只定义不裁定**：本节给出 A/B 两个方向的完整冻结语义，但**不选择**、**不宣称**任一方向已批准。任何"已批准"宣称必须由审批事件驱动（卡片：审批事件来源在未来合同绑定）。
2. **运行验证不在此卡**：`runtime_status=unknown` 的项（D01 静态测试是否在当前 daemon 二进制通过）属 TP-09/11G 的真实运行验证；本卡不运行产品测试，静态测试只作为**契约证据**登记，不作为运行通过证明。
3. **下游绑定**：
   - 批准 A → TP-05 承担全部权威文本同步（AGENTS/protocol/amendment + 模板/Skill 引用）；无 P-FIX。
   - 批准 B → P-FIX 承担产品修复并作为 TP-09/11G 前置；TP-05 只做协议侧不变情况下的最小校准。
   - 两种方向都要求：**所有适用对齐责任未闭合前，不得冻结下游合同或宣称运行通过**（卡片成功标准）。
4. **禁止项**（卡片负向）：缺权威批准时不得宣称对齐完成；实现/协议不一致不得未登记；不得预设 bridge 缺失或已通过；不得编造 capability（含把 `adjudicator_returned` 说成 capability 门控项）。

### 6.4 profile 适用性（candidate profile 边界）

本任务为 governance 设计卡，`candidate/profile/corpus/命令` 版本由 TP-02 产出。本卡引用的 profile 边界：

- **身份 profile**：executor（本卡执行角色），由 daemon role contract `rcl-T-1791627786186-a562f188-executor` r1 绑定（skill `cw-executor-senior-engineer`）。
- **证据 profile**：静态规划证据（文档 sha256 + 行号摘录 + git revision），不含运行时 round-trip。运行证据 profile 由 TP-09/11G 定义。
- **禁止 profile 冒充**：不得把静态文档比较表述为产品验收、daemon verdict 或用户批准（卡片："No static planning PASS represented as product PASS"）。

## 7. 后续步骤（本任务内）

- **Step 2**（`differences.json` + `acceptance_contract.json`）：形成 source/protocol 逐字段差异与最小责任路径，登记受阻下游。
- **Step 3**（`acceptance_contract.json` + `validate_contract.py`）：冻结正常/重放/冲突/计划缺陷路由验收 oracle，两验收命令 rc=0。
