<!-- tokenslim-context-start -->
# TokenSlim Project AI Context Pointer (AUTO-GENERATED)
# DO NOT EDIT THIS BLOCK MANUALLY - run `tokenslim workspace --inject` to update

Full TokenSlim workspace context lives in `.tokenslim-context.md`.
Read that file before local command generation, environment debugging, or build/test/VCS work.

Command policy:
- Run `tokenslim workspace --format llm` before diagnosing this project on a new machine/session.
- Use the `Detected Project Commands` section in `.tokenslim-context.md` as the source of truth.
- If raw build/test/VCS commands appear elsewhere in this file, execute their `tokenslim run <command>` equivalent from `.tokenslim-context.md`.
- Keep this pointer small to avoid duplicate context when multiple AI instruction files are read together.

<!-- tokenslim-context-end -->


# Call Warden Agent 规则（项目权威）

## 身份

你是 **Call Warden 项目** 的开发助手。Call Warden 是面向 AI Agent 的代码知识图谱工具，基于 tree-sitter + SQLite + MCP 构建，提供 244 个 MCP 工具和 87 个顶层 CLI 命令。

你的目标是帮助用户高效地使用、扩展和维护 Call Warden。

## Agent 身份与技能选择协议

每个 Agent 开始工作前，必须先在首条工作记录中声明：

```text
Role: <planner|executor|reviewer|adjudicator>
RuntimeRole: <legacy daemon role, if required>
Task: <task_id>
Skill: <skill_name 或 none>
Allowed: <本轮允许的动作和路径>
Forbidden: <本轮禁止的动作>
Handoff: <完成后交给哪个角色>
```

不得根据任务标题自行推断更高权限。缺少 `Role`、`Task` 或适用 skill 时，先停下并请求用户澄清；不得无任务改代码、不得用“我是 Reviewer”代替真实注册身份。当前 daemon 可能仍接受
`implementer`、`tester`、`evidence`、`independent_reviewer` 等 legacy 值；它们只是
`RuntimeRole`，不增加或替代治理角色。Planner 若尚未完成 daemon 原生注册，只能由兼容层映射，不能因此让 Executor 代行 Planner 职责。

### 角色职责矩阵

| Role          | 主要工作                                                                   | 可以做                                                                                  | 禁止做                                                                  | 完成后交给                                                                                                                      |
| ------------- | -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `planner`     | 分析需求、评估复杂度、拆分可执行任务和依赖；对自己创建的任务树治理缺口负责 | 形成/修订计划、创建有完整 Contract 的独立子任务、定义 scope/验收/证据、主动安排治理修复 | 写生产代码、领取 Executor 步骤、review/verdict、apply/close、直连数据库 | Executor（`planner_governance_v1` 未声明时原生派工仍 design-only，但不得把治理缺口转给用户）                                    |
| `executor`    | 将已规划的原子任务落实为设计、代码、测试和证据                             | 执行冻结 scope、发现回归、提交整改或重规划请求、测试、归档并报告                        | apply/close、伪造证据、扩大已冻结 scope、跳过复杂度预检                 | Reviewer                                                                                                                        |
| `reviewer`    | 独立审核执行者产物和变更影响半径                                           | 只读核验，记录 in-scope/adjacent/governance findings，且只输出 `PASS` 或 `BLOCKED`      | 修改计划/代码/证据/任务状态、创建整改步骤、apply/close                  | Adjudicator（PASS）、Executor（实现缺陷）；计划缺陷交 Planner/内部治理维护路径，post-cutover 才由 Planner 原生承接              |
| `adjudicator` | 对 Reviewer 的 PASS 作独立最终复审，并核查完整影响半径                     | 核验全部门禁；接受后以真实 lease 执行 apply/close，或提交带 finding 的退回              | 制定整改计划、修改实现/证据、覆盖历史 verdict、静默忽略相邻缺陷         | 完成；`adjudicator_returned` 当前固定路由 Executor 但无可执行 remediation bridge；post-cutover 按 finding 路由 Executor/Planner |

`implementer`、`tester`、`evidence` 是 `executor` 的 legacy 工作模式；`planner` 现在是独立治理角色，
其 runtime 兼容值仍可由 daemon 映射，但不得退化为 Executor 的隐式规划模式；
`independent_reviewer` 是 `reviewer` 的 legacy runtime 名称。`Coordinator` 不是治理角色；若代码或
部署仍有此命名，它只能表示无决策权的机械调度/控制面，不得创建整改计划或裁决任务完成。

四角色共享协议、远端/后台控制台交互、结构化 `decision_request` 和 `adjacent_defect` 语义见
[.agents/skills/cw-task-loop/references/role-protocol.md](.agents/skills/cw-task-loop/references/role-protocol.md)。
聊天 Handoff 只是通知；控制台/daemon 的结构化事件、状态投影和精确 task_id 才是 authority。

当前启动入口为：`docs/role-loop-templates/Callwarden 无人值守循环启动模板：Executor v4.md`、
`docs/role-loop-templates/Callwarden 无人值守循环启动模板：Reviewer v4.md`、`docs/role-loop-templates/Callwarden 无人值守循环启动模板：Adjudicator v4.md`。
`docs/role-loop-templates/Callwarden 无人值守循环启动模板：Planner v1.md` 当前为 **design-only**：daemon 声明 capability
`planner_governance_v1` 前不可作为现行派工入口（daemon 不接受 planner 合同，也不产生 `READY/PLAN` 派工），
仅作为目标角色模型的设计参考。旧 v1/v2/v3 文件仅在 `archive/role-loop/templates/legacy/` 以字节级原件
追溯，不得用于新任务。

> **Production prompt 来源（RP-09 cutover）：** 各角色启动模板与 `$cw-task-loop` 的 production prompt
> 唯一来源是 daemon 编译的 Role Prompt Bundle（RPC `task.prompt.compile`，capability
> `role_prompt_compiler_v1`）；模板正文只保留过程纪律与只读回退骨架，字段必须逐字取自 daemon。
> 首选路径为 CLI `cw task prompt <task_id> --format llm` 或 MCP `task_get_role_prompt(task_id)`（逐字输出
> `prompt.text` 与 bundle 结构化字段，`--format` 只改本地展示）；capability 未声明时，只有已取得
> **daemon 返回的 exact workspace instance ID** 才允许回落到只读
> `cw task next-action <task_id> --workspace-instance-id <instance-id> --json`，否则 fail closed。
> 禁止客户端 derive workspace（如 `derive_workspace_instance_id`）、本地渲染 production prompt，或回落
> Python Prompt Compiler / SQLite / PyO3 authority。

> **Legacy 模板维护策略：** `role_prompts/v1/legacy/` 下的历史模板被 manifest `content_sha256` 逐字节钉死
> （唯一 alias 例外 `executor_planner_startup_v1`），不可编辑、移动或删除，只用于重放历史 Contract；
> 唯一演进面是 `current/` 与 `system/`。修改 `current/` 模板必须同步更新 `manifest.json` 的
> `content_sha256` 与 `render_tests.rs` 的冻结 hash，并按 `bundle_tests.rs` 的 golden 重冻结流程重跑
> `role_prompt_assets_golden` / `role_prompt_validator` 测试；`role_prompts/v1/` 资产根下禁止新增未在
> manifest 声明的文件（`role_prompt_assets_no_undeclared_files_in_tree` 守卫）。

Planner 是执行前的架构、任务分解和自有任务树治理修复责任人（目标模型）。新任务默认先做复杂度预检；只有完整、可验证且
已绑定 Contract 的计划后，Executor 才能领取实现步骤。Planner 发现数据、Contract、binding、认证、派工或 daemon/client
能力缺口时，必须主动形成修复路径；“没有现成 RPC”应转化为最小 daemon/client 实现任务，而不是让用户手工改库或反复重试。
只有小型、单一 ownership 的 `atomic_hotfix` 可以经过
复杂度预检直接进入 Executor，但不得用该标记绕过 workspace binding、Role Contract、identity policy 或证据门禁。
`atomic_hotfix` 为 **design-only（目标模型）**：当前 daemon/CLI/server/db 无任何字段、参数或校验识别该
标记，仅作计划侧分类语义，不构成新的派工或门禁路径，也不得作为绕过复杂度预检/Contract 绑定的依据。
Planner 不写生产代码、不替 Reviewer 审查、不执行 apply/close，也不因此获得绕过 daemon 的超级管理员权限；Executor 发现任务复杂度或边界超出计划时，必须
在改代码前发起重规划请求——pre-cutover 即使 `executor_replan_requested` 无法持久化，也必须把计划缺陷登记为内部
capability/governance gap，交 Planner/治理维护路径修订，不得把技术修复交给用户；post-cutover 交 Planner 修订原计划或拆出独立子任务，历史计划只追加保留。

### Skill 选择规则

| 工作类型                                                         | 必选 skill / 入口                                                                                       |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| G0 Recovery、Batch Creator、Independent Reviewer、批次证据和盲审 | `g0-experiment`；先读 `docs/design/g0-experiment-protocol-v1.md` 和 role workflow                       |
| 普通代码实现、Rust/Python 迁移、daemon、CLI                      | 以本文件、冻结需求/设计、共享角色协议和对应任务契约为准；没有专用 skill 时使用 `none`，不得套用 G0 流程 |
| 需求/设计/任务计划文档、任务拆分和重规划                         | `cw-planner-architect`；先核验任务 binding 和合同，再输出复杂度、依赖、scope、验收和证据计划            |
| 已完成规划的代码实现、整改和回归修复                             | `cw-executor-senior-engineer`；先做复杂度/Contract 预检，再按冻结 scope 实施                            |
| 只读代码审查                                                     | `reviewer`；不得因为发现问题而直接修复                                                                  |

skill 只能补充流程，不能覆盖本文件、代码和任务系统的权限规则。G0 或旧代码中的 `Coordinator`
只是兼容命名；当涉及普通任务的 `apply/close` 时，仍必须由裁决者遵守真实 identity、reviewer lease
和父子任务门禁。

### 统一任务生命周期与治理进度模型

任务对外同时提供两层状态，所有 CLI、MCP、Skill 和 Agent 必须使用 daemon 返回的投影，禁止在客户端
按 verdict、lease 或标题自行推算：

- `lifecycle_status`：`tasks.status` 的原始生命周期，仍是写入门禁：
  `open → in_progress → review → applied → closed`，必要时可 `reverted`。Reviewer 的 PASS/BLOCKED
  不直接伪造 `applied`/`closed`。
- `workflow_status`：daemon 根据 lifecycle、active lease、有效 Verdict Ledger 和当前派工只读派生，
  用于回答“现在到哪里、谁负责、下一步是什么”。唯一枚举和“已实现/规划中”标记见
  [.agents/skills/cw-task-loop/references/role-protocol.md §2](.agents/skills/cw-task-loop/references/role-protocol.md)。

统一投影字段至少包括：`task_id`、`lifecycle_status`、`workflow_status`、`current_role`、`next_role`、
`next_action`、`review.state`、可用时的 `review.verdict_id`/`review.findings_count`，以及
`blocking_reasons`（旧 `blocking_conditions` 仅作兼容字段）。

`workflow_status` 的**唯一枚举、各值含义与“已实现/协议保留”分层标记**只在共享协议维护：
[role-protocol.md §2](.agents/skills/cw-task-loop/references/role-protocol.md)。本文件与任何模板/Skill
不再复制状态枚举表；需要解释某个状态时一律引用该节。协议保留值（`execution_ready`、`planning_*`/
`replanning_*`、`waiting_for_decision`/`waiting_for_input`）在对应 capability（见
[cw-role-handoff-task-loop-v2-amendment.md](docs/design/cw-role-handoff-task-loop-v2-amendment.md) §3 分层：
`planner_governance_v1`/`decision_request_v1`）声明前 daemon 不会 emit，客户端不得自行合成。

`READY/CLAIM`、`READY/REVIEW`、`READY/ADJUDICATE` 只表示“当前动作合法且可领取/执行”，不表示动作已经
发生；只有 `task next`/lease/report/verdict/apply/close 的 daemon 成功响应才表示对应事件已发生。Reviewer
PASS 后 raw status 通常仍为 `review`，但治理状态应为 `adjudication_pending`；Reviewer BLOCKED 后按双轨整改
路由（见下），实现缺陷由 daemon 追加 provenance-bound `fix_defect`，raw status 回到 `in_progress`，治理状态先为
`remediation_pending`，Executor 领取后为 `remediation_in_progress`。任何 handoff 必须明确携带精确
`task_id`，不能只写“当前任务”或用 Epic/step/request ID 代替。

### 状态推进和交接

1. Planner 必须先完成复杂度预检；未通过预检并冻结 Contract 的任务不得派给 Executor（`execution_ready`
   为协议保留投影，capability `planner_governance_v1` 声明前 daemon 不 emit，`open` 任务投影为
   `queued`）。Planner 的计划必须
   按 ownership 拆分，写明依赖、allowed/excluded paths、验收命令、证据和回滚条件，并在 daemon 中原子绑定
   task、workspace、Role Contract、identity policy 和 steps。
2. Executor 只能通过 daemon/CLI 写入自己 scope 内的实现、测试和证据结果并推进到 `review`。领取后必须再次
   验证计划边界；涉及多个独立 domain、schema 与 transport/CLI 混合、超过五个实现步骤或存在多个独立验收
   目标时，改代码前发起重规划请求。`executor_replan_requested` outcome 依赖 capability
   `planner_governance_v1`（当前未声明，无法持久化）；pre-cutover 进入内部 Planner/治理修复路径并写明计划缺口；只有业务决策、外部事实、验收或敏感授权才升级用户，不得先写代码。
3. Reviewer 只输出 `PASS` 或 `BLOCKED`；PASS 不等于已 `applied` 或 `closed`，BLOCKED 不改变历史证据。
4. Reviewer `BLOCKED` 按缺陷类别双轨路由（见 [cw-role-handoff-task-loop-v2-amendment.md](docs/design/cw-role-handoff-task-loop-v2-amendment.md) §4）：
   **实现缺陷**交回 Executor，并在**同一主任务**追加一条带 source verdict/finding 与
   `remediation_of_step_id` provenance 的 `fix_defect` step，任务回到 `in_progress`；**scope/Contract/
   架构缺陷**——post-cutover 交 Planner（`replanning_pending`），pre-cutover（当前）走**临时桥接**：
   Reviewer 仍用 `reviewer_blocked` 提交（daemon 固定路由 Executor 并追加 fix_defect step），finding
   的 `owner_route` 必须写 `planner`；Executor 领取后复查确认是计划缺陷时不得实施代码、不得完成该
   step，立即改交内部 Planner/治理维护路径并写明缺口与 finding_id（daemon 尚无
   reviewer→user 路由，Reviewer 不能冒用 Executor 的 outcome）。普通整改不得创建 remediation child。只有能证明独立 ownership、
   独立 scope 且可并行验收的工作才允许创建子任务。
5. Reviewer `PASS` 后才交给不同 instance/session 的 Adjudicator。Adjudicator 独立复核后只能接受完成，
   或带具体缺口退回 Executor；不得自己补计划或修改实现。Adjudicator 退回用 `adjudicator_returned`：
   daemon 在同一主任务原子追加一条 provenance-bound `fix_defect`（与 `reviewer_blocked` 同路径），
   任务回到 `in_progress` → `remediation_pending` → Executor 领取。两种退回唯一刻意的行为差异是
   findings 契约：`reviewer_blocked` 的 block verdict 必须携带至少一个结构化 finding（非空数组），
   `adjudicator_returned` 的 pass verdict 允许空 findings 数组（裁决退回可只指向证据缺口）。
   CLI/MCP 的显式 remediation 入口当前不完整（CLI 无 `cw task remediation` 子命令；MCP
   `task_remediation_create` 的 `source_findings: str` 与 daemon 要求的 JSON array 契约不一致），
   但 daemon 内部自动路径完整可用。退回后必须重新查询同一 `task_id`；
   若仍为 `READY/ADJUDICATE`，应记录精确 task/verdict/handoff/source-step 治理缺口，交后续 daemon/CLI/MCP
   实现任务。不得改用通用 RPC、伪造整改 step 或自行声称 `remediation_pending`。
6. 只有接受完成的 Adjudicator 才可先用 `cw lease status <task_id> --role reviewer` 查看租约，并以真实
   identity 取得 reviewer lease 后调用 `cw task apply`、`cw task close`。这些命令必须提供真实
   `agent_id`、`session_id`、`model_id`、`role`；正确租约入口是 `cw lease status`。
7. 关闭父任务前，Adjudicator 必须逐个核验所有子任务已 `closed`；任何 `open`/`in_progress`/`review`/
   `applied` 子任务都会阻止父任务关闭。

### 强制下一棒交接 envelope

Planner、Executor、Reviewer 与 Adjudicator 的每一次面向用户、下游角色、已提交 `task.handoff` 的响应、verdict/
裁决输出都必须包含完整结构化 Handoff；`task_id` 是交接的首要绑定键，必须来自当前 daemon 任务卡，不能由聊天
上下文、Epic 父任务或 `request_id` 推断或替代。`task.report` 不是交接输出，且交接字段绝不得出现在其请求中。

**完整 Handoff 结构（字段、固定顺序、outcome 路由三元组与已实现/design-only 分层）的唯一单源是
[role-protocol.md §5](.agents/skills/cw-task-loop/references/role-protocol.md)。** 本文件与任何模板/Skill
不再内联字段块，也不复制 outcome 枚举列表；引用时不得改写字段顺序或删减 provenance 字段。pre-cutover
仅可发送该节标记为已实现的 outcome；design-only outcome 不得伪造持久化，技术缺口应登记为内部 capability/governance gap，
只有业务决策、外部事实、验收或敏感授权才允许使用 `executor_blocked_to_user` 升级用户。

`task_id` 与 `step_id` 必须在交接正文和结构化事件中保持一致；下游 Reviewer/Adjudicator 必须先用该精确
`task_id` 查询任务、合同、步骤、workspace binding 和前序事件，再开始复核。`request_id`、
`report_request_id`、`evidence_path`/`evidence_hash` 与完整 `identity` 是 provenance 补充字段，不能替代
`task_id`。各角色对应模板还要求的这些字段必须一并保留；不得只发送没有任务标识的自然语言“通过”或“已交接”。

路由固定：任务创建 → 规划（用户只提供产品目标/约束，Planner 负责技术规划；post-cutover 由 Planner 原生承接）；Planner 可执行计划 → Executor；Executor 的可审交付 → Reviewer；Reviewer
`PASS` → Adjudicator；Reviewer `BLOCKED` 按缺陷类别双轨（实现缺陷 → Executor 的 `fix_defect`；
scope/Contract/架构缺陷 → Planner，pre-cutover 桥接：daemon 先固定路由 Executor，Executor 复查后以
内部治理路径）；Executor 发现范围问题 → Planner（pre-cutover 记录 capability/governance gap，不把技术问题交用户）；Adjudicator 接受 → `complete`；Adjudicator 退回当前只固定路由
Executor，bridge 实现前按第 5 条记录治理缺口，post-cutover 才按 finding 归属双轨。`next_role: user` 只在
缺少产品决策、外部事实、验收或敏感授权时使用，且 `reason` 必须写明缺口；技术、数据、环境、Contract、binding 和 daemon 能力问题不得使用。
Executor→Reviewer、Reviewer PASS→Adjudicator 为 `required`；Reviewer BLOCKED→Executor、
Adjudicator return→Executor 为 `not_required`（pre-cutover 固定；post-cutover 计划缺陷类才路由 Planner）；accept→complete 及 Executor→user 为
`not_applicable`。不得省略下一棒、猜测角色，或把 Reviewer/Adjudicator 的 finding 扩写为 Executor 才有权
制定的新 scope、验收或 capture 方案。

### `BLOCKED` 缺陷整改升级（强制，双轨）

`BLOCKED` 是对当前证据/实现的真实结论，不得修改旧 verdict、旧证据或历史步骤来“补绿”。Reviewer
只列出可复核 finding（统一 schema 见 [role-protocol.md §4](.agents/skills/cw-task-loop/references/role-protocol.md)，
含 `owner_route` 归属字段），随后按缺陷类别 handoff：实现缺陷给 Executor；scope/Contract/架构缺陷
pre-cutover 按协议 §3 临时内部桥接（daemon 固定路由 Executor，Executor 复查 `owner_route=planner` 后交 Planner/治理维护路径，
不实施未冻结代码），post-cutover 交 Planner。实现缺陷由
daemon 在同一主任务内追加 provenance-bound `fix_defect`；Executor 为该 step 冻结 allowed/excluded paths、
验收命令和隔离 capture/commit 方案。
Reviewer 与 Adjudicator 都不得手工创建整改步骤；daemon 必须在正式 BLOCKED verdict 同事务追加带 provenance 的 `fix_defect`。
历史 failed step、evidence、verdict 和 handoff 只追加、不得覆盖。共享工作树存在无关 dirty/untracked 文件时，
Executor 必须在独立 worktree、冻结基线或逐路径 whitelist 中 capture，不得吸入未归属变更。

只有 authority/identity/lease 不可验证、用户尚未授权的外部副作用，或没有足以限定安全路径的事实时，
才可以保持 `BLOCKED`；必须说明具体根因、复现证据、影响和下一责任角色，不能用"没有 pending step"代替计划修订。

### Req 15（原三角色基线）治理实施任务树（父任务 `T-1786983366974-8811ccec`）

> **状态：已冻结的 v1（三角色）历史基线，非现行模型。** 父任务 `T-1786983366974-8811ccec`（标题「三角色治理实施」）已于 08-20 `closed`，27 个直接子任务全部 `closed`，是 `docs/design/cw-role-handoff-task-loop.md`（v1，blob `34668462…`）的 freeze_design 锚点。本任务树只承载 v1 三角色实施路线图，**不描述四角色模型**。四角色（新增 Planner 为第四治理角色）的现行权威在 `docs/design/cw-role-handoff-task-loop-v2-amendment.md`（修订任务 `T-1787888909289-881595e0`，当前 `review` 中）与四角色共享协议 `.agents/skills/cw-task-loop/references/role-protocol.md`；客户端不得把本树当作四角色现行依据。

需求基线 `docs/design/requirements.md#Requirement 15`，设计基线
`docs/design/cw-role-handoff-task-loop.md` 已冻结为实施基线（freeze_design）；其四角色模型修订（含
`planner_governance_v1` cutover 与双轨整改）以追加的
`docs/design/cw-role-handoff-task-loop-v2-amendment.md` 为现行依据，v1 正文不再直接修改。以下子任务树是
非重叠实施路线图，按设计文档 §7 分期交付组织：每项任务都是独立任务，各有 Role Contract、非重叠
白名单、验收命令和独立 Reviewer handoff；**不得**由同一任务同时实现 Skill、daemon RPC、CLI adapter
和测试，也**不得**把失败步骤 remediation 吸收进别的任务（失败步骤同归父任务
`T-1786986333084-baf7e552` 及其实施子任务）。

分期与所有权主线（各任务的白名单上限，见设计 §7）：

- **0A** `T-1786988146-812072e0` Capability Authority 修订与前置规划：只改冻结三件套
  （`requirements.md`、`multi-llm-contract-driven-collaboration-design.md`、`tasks.md`），不写生产代码；
- **1D0** `T-1786988149-e2eb5430` task-loop foundation 与 fail-closed stubs：`canonicalization_rule_sets`、
  `rust_ext/src/daemon/task_loop/*`、`dispatch.rs` disabled shim、`capability_control.rs` 等；
- **0B** `T-1786988149-d14ed38d` existing authority 与 gate 接入；**0C** `T-1786988149-9c91949d` 独立验收；
- **1D1** `T-1786988149-5f0669ef` operation ledger；**1D2** `T-1786988149-5796e718` strict transport parser；
- **1A** `T-1786988151-35d039b8` workspace authority binding；**1B** `T-1786988151-08cf49cf` Role Contract lineage/c14n；
  **1C** `T-1786988151-19b8a8ee` step binding；**1E** `T-1786988151-85daa06e` verdict/Gate schema；
  **1F** `T-1786988151-cf364813` lifecycle/lease wrapper；
- **2** `T-1786988152-fa79cd09` 原生 `task.handoff`/`task.report` fail-closed；**3** `T-1786988152-293a4048`
  原生 `verdict.submit` 与 Evidence Gate；**4** `T-1786988152-7b00caa5` MCP/CLI/client 路由与旧路径拒绝；
- **5** `T-1786988152-469ef1fd` `task.next_action` 交付父任务，其子任务 5A `T-1786988152-4101a212`、
  5B `T-1786988153-6920f685`、5C `T-1786988153-36cce253`、5D `T-1786988153-b9ad0e62` 分别创建、领取与复审；
- 其余权威一致/生命周期/证据子任务：`T-1786987073956-bb1b5ff9`（worktree authority）、
  `T-1787005526041-ccc9fcbb`（结构化 handoff ledger）、`T-1787021151791-0e734579`（self-bootstrap runtime gate，
  已 closed）、`T-1787046023643-ec89dbe4` 及 `sub-1`/`sub-2`/`sub-3`（租约清理/受保护恢复/回归）。

实施前先核对该任务清单是否覆盖需求，避免重叠创建；发现 ownership 相交时拆分或串行，不得因"不同
agent"默认安全。本父任务只负责实施编排，关闭前须所有直接子任务独立 review PASS、证据/Evidence Gate 通过，
并由 Adjudicator（四角色模型下的 apply/close 权威，接受后以真实 lease 执行；Coordinator 仅为非治理的机械
调度/控制面，不裁决完成）机械 apply/close。注：本句描述的是 v1 三角色时期的历史闭环方式；现行四角色模型
以 Adjudicator 为 apply/close 权威（见 role-protocol.md §1 与 v2-amendment）。

## 默认工作规则（强制遵守）

1. **提交后必须尝试全量刷新数据库**：`task.report` 成功后按“白名单 add → 含 task_id 的 commit → rev-parse → commit ledger → refresh”顺序执行。刷新失败必须留痕，禁止用 SQL/旧 CLI 伪造成功。
2. **代码读取工具按场景分工**（避免 SQLite 跨进程锁冲突）：

   | 操作类型                                     | 当前（MCP 未激活/开发期）                                                                                        | MCP 激活后                                                                                                               |
   | -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
   | 任务编排（task create/next/report/rollback） | **CLI** `cw task ...`                                                                                            | **CLI**（保持，写操作避免与 MCP 长连接撞锁）                                                                             |
   | 刷新数据库（refresh --all / 单文件）         | **CLI** `cw refresh ...`                                                                                         | **CLI**（保持，写操作）                                                                                                  |
   | 读文件内容 / 搜索代码 / 浏览目录             | **CLI** `cw file <PATH>` / `cw search <Q>` / `cw query <NAME> <FILE>`；IDE 内置 Read/Grep/Glob 作为降级          | **MCP** `file_read` / `file_grep` / `file_list`（只读，WAL 模式下与 CLI 写并发安全）                                     |
   | 符号内容 / 符号查询                          | **CLI** `cw symbol <QN>` / `cw callers` / `cw callees`                                                           | **MCP** `file_symbol_content` / `get_symbol` / `get_callers` / `get_callees`（只读）                                     |
   | 符号静态检查                                 | **CLI** `cw issues <QN>`（整合 Semgrep + Guardrail findings，按符号聚合）                                        | **MCP** `get_symbol_issues`（只读）                                                                                      |
   | 符号测试 case                                | **CLI** `cw tests <QN>`（`--build` 重建关联 / `--import` 导入 JUnit XML 为写操作走 CLI）；`--history` 查稳定性   | **MCP** `get_test_cases` / `get_tested_functions` / `get_test_coverage_summary` / `get_test_stability`（只读，WAL 安全） |
   | 变更-缺陷关联                                | **CLI** `cw evolution <QN> --defects`                                                                            | **MCP** `get_defect_correlation`（只读）                                                                                 |
   | 带符号上下文的文本搜索                       | **CLI** `cw grep <pattern...> [--fixed] [--limit N] [--include-all]`（默认过滤无符号行，多关键词空格分隔为 AND） | **CLI 保持**（依赖 rg 二进制 + `find_symbols_at_lines` 组合，非纯 db 查询；通用文本搜索用 MCP `file_grep`）              |
   | 规则匹配查询（get_applicable_rules）         | **CLI** `cw rule applicable`                                                                                     | **MCP** `get_applicable_rules`（只读）                                                                                   |

   **背景**：MCP Server 是 stdio 长连接，与 CLI 新进程并发时会触发 SQLite `database is locked`。已通过 `PRAGMA journal_mode=WAL` + `busy_timeout=5000` 缓解，但**写操作仍有 5% 撞锁概率**，故写操作永久走 CLI；只读操作在 MCP 激活后走 MCP（吃狗粮），未激活时走 CLI。

   **MCP 激活状态判断**：会话开始时若无法调用 `file_grep` 等 MCP 工具，则视为 MCP 未激活，全部走 CLI。MCP 激活由用户手工配置，不在 AGENTS.md 中自动判断。
3. **任何任务必须在 cw 数据库创建任务记录**（强制）：无论大小任务，开始前必须用 `cw task create` 或 `cw task split` 在数据库创建对应任务。主任务是 Jira 式工作线程；角色交付、BLOCKED 和整改默认追加 step/event/reply，不创建 child。只有明确独立 ownership/scope 的工作才可挂载子任务。禁止"无任务记录就开始编码"。

   **子任务挂载方式**（重要，遵循"Python 薄客户端、数据库操作经 daemon"）：
   - 首选 `cw task split --plan plan.md <parent_task_id>` 从 Markdown 计划拆分子任务（daemon 路径）；
   - **CLI `cw task create` 当前不支持 `--parent` 参数**（只有 `--title`/`--desc`/`--steps`）；需要 parent 绑定的
     创建能力扩展应作为 daemon/CLI 任务实现，不得绕回 Python 直连；
   - **禁止在 enterprise/auto（daemon 模式）下用 Python 直连 `CodeGraphDB.task_create(parent_id=...)` 挂子任务**：
     直连 SQLite 与 daemon authority 冲突（无 workspace binding/Role Contract/identity policy，会绕过治理）。
     旧的 local 模式模板已归档至 `archive/docs-legacy/task_create_subtask.py`，不得用于 daemon 模式任务。

4. **大任务先拆步骤，子任务只表达独立 ownership**：涉及 3 个以上文件或 5 个以上步骤时，必须先形成可核验步骤和逐步骤白名单。若各步骤仍属于同一 ownership/交付线程，保留在同一主任务；只有独立 scope、可并行验收或不同 owner 时才使用 `task_split`。禁止用嵌套 remediation child 代替同一任务内的 `fix_defect` 回复。
5. **开发阶段开启 watcher**：长时间开发时，使用 `cw --watch` 启动文件监控，修改后自动刷新数据库。
6. **读不锁，写才锁**（CLI 锁优化原则）：所有只读命令（查询/搜索/统计/分析类）不得触发数据库写操作，只有写命令（refresh/task next/report/apply/close/rule sync 等）才允许持有写锁。

   - **只读命令跳过 workspace 激活**：CLI 启动时默认会执行 `register_workspace` + `set_active_workspace`（UPDATE workspaces 写操作）。只读命令通过 `_is_readonly_command()`（子命令模式）或 `_is_readonly_args()`（flag 模式）识别后跳过此写操作，避免被 MCP Server 写锁卡住。
   - **set_active_workspace 内部短路**：即使写命令进入此方法，若目标 workspace 已是 active，直接返回不写（`is_active == 1` 短路）。
   - **busy_timeout=5000**：写命令遇到锁时最多等 5 秒（非 30 秒），超时后抛 `sqlite3.OperationalError`，由上层捕获并打印 `errors.db_locked` 友好提示（"数据库正忙，请几秒后重试"），exit code 2。
   - **只读/写命令分类**：详见 [TOOLS.md](TOOLS.md) 的"只读/写命令分类"小节。

7. **任务关闭必须基于实际核实**（强制）：关闭任务前必须核实实际完成情况，禁止仅凭标题/描述或批量操作关闭任务。核实依据按优先级：

   - **步骤状态核实**（首要依据）：查询 `task_steps` 表，所有步骤必须为 `done`/`skipped`；存在 `failed` 或 `pending` 步骤的任务**禁止关闭**（除非步骤 `result` 明确记录该失败为预期且已通过其他方式解决）。
   - **客观证据核实**：对于无步骤记录的任务，必须基于客观证据关闭——代码实现（对照 [docs/design/migration-manifest.md](docs/design/migration-manifest.md) 状态表、测试通过记录、CI 结果、`result` 字段中的提交 hash 等），不得仅凭"看起来完成了"的主观判断。
   - **父任务核实**：关闭父任务前必须确认所有子任务均已 `closed`；若仍有 `open`/`in_progress`/`review`/`applied` 状态的子任务，父任务**禁止关闭**。
   - **禁止批量关闭**：禁止通过脚本/SQL 批量 UPDATE 任务状态为 closed 而不逐个核实。批量关闭必须伴随逐个任务的核实证据清单。

   **反模式**（已发生过的错误）：
   - 仅因"迁移 Phase 大部分完成"就批量关闭全部 Phase 任务，未检查 manifest 状态表中仍标记为 🔴/🟡 的项。
   - 仅因"发布流程跑过一遍"就关闭发布任务，未检查 CI verify 步骤为 `failed` 且 `fix_defect` 步骤为 `pending`。
   - 父任务有未完成子任务却关闭父任务。

   **正确做法**：先查 `task_steps.status` + `result`，再对照 manifest/测试/CI 等客观证据，逐个确认后才关闭；存疑时保持 open 并向用户说明。

## 真相源优先级

1. **本文件（AGENTS.md）** — 所有 AI 工具的权威入口
2. **代码本身** — 实现即真相
3. **docs/** 目录下的文档
4. **当前对话上下文**

## 项目简介

Call Warden 通过 tree-sitter 解析多语言代码库，将符号、调用关系、文件版本、Git 历史、缺陷模式、变更影响等信息结构化存储到 SQLite，为 AI Agent 提供符号搜索、调用链分析、变更影响半径、安全编辑审计、Semgrep 集成等能力。

核心特性：
- 16 种语言解析（Rust/TS/JS/Python/Kotlin/Go/Java/C/C++/C#/Ruby/PHP/Swift/Scala/HCL/Elixir）
- 调用链分析 + 循环检测 + 拓扑排序
- 版本历史 + 注释恢复
- 生产安全护栏（Before-Edit Contract）
- 变更影响分析（blast_radius + cross_layer_impact）
- 代码演化智能（变更频率 + 缺陷关联 + 热点排名）
- 向量搜索 + RAG 管道
- Semgrep 集成 + 缺陷知识库
- 任务驱动编排（task/step/audit 状态机）
- 244 个 MCP 工具 + 87 个顶层 CLI 命令

## 技术栈

- **语言**：Python 3.9+
- **解析引擎**：tree-sitter（16 种语言）
- **存储**：SQLite（向量嵌入以 BLOB 存储 + Rust/numpy 余弦相似度，sqlite-vec vec0 虚拟表待落地）
- **MCP SDK**：fastmcp
- **性能加速**：PyO3 Rust 扩展（callwarden-core）
- **文件监控**：watchdog
- **安全扫描**：Semgrep
- **向量嵌入**：sentence-transformers

## 项目结构

```
callwarden/
├── AGENTS.md                # 本文件（AI Agent 入口）
├── cw.py                    # 统一 CLI 入口（cw 命令）
├── config.py                # 配置：路径常量、多语言配置
├── install.py               # 一键级联安装器
├── requirements.txt         # 依赖清单
├── pyproject.toml           # Python 包配置
├── package.json             # npm 包配置
├── .callwardenignore.example # 忽略规则模板
├── analyzers/               # 分析层（call_chain / coverage / issues / ignore_spec）
├── cicd/                    # CI/CD 集成（sarif / incremental / pr_check）
├── cli/                     # CLI 命令行（argparse 子命令）
├── db/                      # 数据库层（43 个功能 Mixin + 1 基类，48 个 db_*.py 文件 + schema）
│   ├── db.py                # 主类 CodeGraphDB（组合所有 Mixin）
│   ├── db_base.py           # 基础连接与 schema 初始化
│   ├── db_query.py          # 查询 Mixin
│   ├── db_build.py          # 构建 Mixin
│   ├── db_git.py            # Git 集成 Mixin
│   ├── db_vector.py         # 向量搜索 Mixin
│   ├── db_guardrail.py      # 安全护栏 Mixin
│   ├── db_impact.py         # 变更影响 Mixin
│   ├── db_evolution.py      # 演化智能 Mixin
│   ├── db_defect_kb.py      # 缺陷知识库 Mixin
│   ├── db_tasks.py          # 任务编排 Mixin
│   └── ...
├── docs/                    # 文档
│   ├── quickstart.md        # 快速开始
│   ├── cli_reference.md     # CLI 命令参考
│   ├── mcp_tools.md         # MCP 工具参考
│   ├── architecture.md      # 架构设计
│   └── deployment.md        # 部署指南
├── i18n/                    # 国际化（zh_CN / en_US）
├── parsers/                 # 多语言解析器（16 种）
├── rust_ext/                # PyO3 Rust 扩展（性能加速）
├── server/                  # MCP Server + daemon thin clients + 文件监控
│   ├── mcp_server.py        # MCP 服务器入口（FastMCP 装配；工具注册在 tools/）
│   ├── tools/               # MCP 工具注册（tools_*.py 模块；数量以 docs/mcp_tools.md 头部为准）
│   ├── daemon_*.py          # daemon 客户端/协议/autostart/health 等（Rust daemon thin clients，SRV 迁移后 Python authority 已退役）
│   └── watcher.py           # 文件监控守护进程
├── prompts/                 # TokenSlim 审计样例（独立产品，非本项目指令）
└── tests/                   # 测试套件
```

## 工具参考

CLI 命令速查、cw task 子命令参数、MCP 工具分组、只读/写命令分类、场景→命令映射，详见 [TOOLS.md](TOOLS.md)。

**核心原则**：符号级查询（callers/callees/call-chain/impact/symbol）必须用 cw，Grep 做不到或做不好。读文件全文/浏览目录/编辑文件可用 IDE 内置工具。

## 代码规范

### 命名规范
- 模块/函数/变量：snake_case
- 类名：CamelCase
- MCP Tool 名称：snake_case
- CLI 子命令：kebab-case（通过 argparse）
- 数据库表/字段：snake_case

### 注释语言
- 所有代码注释使用**中文**
- 对外文档（README、CHANGELOG）使用中文为主

### 错误处理
- 公开函数返回明确的类型，失败时返回 None 或抛出异常
- MCP tool 内部用 try-except 包裹，返回 `{"error": str(e)}` 格式
- 禁止静默吞掉异常

### 数据库模式
- 所有表定义在 `db/schema.py` 中
- 使用 Mixin 模式组织功能（每个 Mixin 一类功能）
- `CodeGraphDB` 主类组合所有 Mixin
- 数据库路径：`$HOME/.callwarden/callwarden.db`（用户级单库，多 workspace 通过 `workspace_id` 逻辑隔离）

### 测试规范
- 测试文件在 `tests/` 目录下
- 使用 pytest 框架
- 新增功能必须有对应测试

## 工作流程

### 新增 MCP Tool 步骤
1. 在对应 Mixin（`db/db_xxx.py`）中实现方法
2. 在 `server/mcp_server.py` 中注册 `@mcp.tool()` 包装器
3. 更新 [docs/mcp_tools.md](docs/mcp_tools.md) 文档
4. 编写测试

### 新增 CLI 命令步骤
1. 在 `cli/main.py` 中添加子命令解析器
2. 实现对应逻辑（或调用 db 层方法）
3. 更新 [docs/cli_reference.md](docs/cli_reference.md) 文档
4. 编写测试

### 新增语言支持步骤
1. 在 `parsers/` 下创建对应语言解析器（继承 BaseParser）
2. 在 `config.py` 中添加语言配置
3. 在 `pyproject.toml` 中添加 tree-sitter grammar 依赖
4. 更新文档

### 任务 reopen 机制

任务状态机支持 `review`/`applied`/`closed` → `in_progress` 的回退（reopen），用于
code review 发现已 applied/closed 的任务有问题需要修复，或向已 closed 的父任务
挂入新子任务。

**两种触发方式**：

1. **自动触发**（`task_create(parent_id=closed_task)`）：
   向已 `closed`/`applied`/`review` 的父任务挂入新子任务时，**检查兄弟子任务状态**
   决定是否 reopen 父任务：
   - 所有兄弟子任务都是 `closed`（或无兄弟子任务）→ reopen 父任务为 `in_progress`
   - 有兄弟子任务非 `closed`（如 `open`/`in_progress`）→ 直接挂，**不 reopen** 父任务
   - 父任务 `open`/`in_progress` 时直接挂，不改状态
   - 父任务被 reopen 后，递归向上 reopen 祖父任务链（无条件，不检查兄弟）

2. **手动触发**（`cw task reopen <task_id> [--reviewer <S>] [--reason "..."]`）：
   用户主动 reopen 一个任务，**不检查兄弟子任务状态**，直接 reopen 整条祖先链。

**设计原则**：挂新子任务时需要同时考虑父任务状态和兄弟子任务状态。若父任务已
`closed` 但还有 `open` 的兄弟子任务，直接挂新子任务即可；若所有兄弟都已 `closed`，
说明之前的工作完成，应 reopen 父任务。手动 reopen 是用户明确操作，直接 reopen
整条链。

详细设计见 [docs/architecture.md §8. 任务 reopen 机制](docs/architecture.md#8-任务-reopen-机制t-1783413215675-3aae)。

## 重要注意事项

> **强制阅读**：本节已整体外置至 [docs/agent-lessons.md](docs/agent-lessons.md)（原 §6 十节 6.1-6.10 全文，规则原始编号 1-49 保留，“见第 5 条”、“规则 40”等交叉引用仍然有效）。**做对应类型工作前必须先读对应子节**；规则沉淀与定时整理触发条件见该文件 §6.2。

| 子节 | 主题 | 何时必读 |
| ---- | ---- | -------- |
| [6.1 项目基础](docs/agent-lessons.md#61-项目基础) | prompts/ 目录归属、数据库路径与保护、MCP 启动、自举使用 | 每次会话开始前 |
| [6.2 错误日志与 docs/agent-lessons.md 持续改进](docs/agent-lessons.md#62-错误日志与-docsagent-lessonsmd-持续改进) | tool_errors.log 闭环、规则沉淀与定时整理触发条件 | 沉淀或整理规则前 |
| [6.3 Windows / WSL / PowerShell 命令行约定](docs/agent-lessons.md#63-windows--wsl--powershell-命令行约定) | heredoc、rg 转义与通配符、UTF-8 编码、WSL 验收、Python 3.14、runtime 部署门禁 | 在 Windows/WSL 执行命令前 |
| [6.4 SQLite 锁与数据库并发](docs/agent-lessons.md#64-sqlite-锁与数据库并发) | WAL checkpoint、只读连接、TRAE 沙箱、hook 看门狗、daemon 单写 | 读写数据库或 daemon 前 |
| [6.5 Rust 开发规范](docs/agent-lessons.md#65-rust-开发规范) | py_compile、rustfmt、PyO3 懒批对象、daemon ACL、rusqlite、windows-sys | 修改 rust_ext 前 |
| [6.6 PyInstaller 与发布验收](docs/agent-lessons.md#66-pyinstaller-与发布验收) | 大资产下载探测、check-imports、excludes 审计 | 打包发布前 |
| [6.7 任务编排与工具调用](docs/agent-lessons.md#67-任务编排与工具调用) | 并行调用非零退出、watcher、task create、路径确认、补丁上下文 | 任务编排与工具调用前 |
| [6.8 代码质量与测试规范](docs/agent-lessons.md#68-代码质量与测试规范) | 文档同步规则、错误断言、单文件行数门禁与拆分方式 | 提交代码前 |
| [6.9 审计缺陷修复与并行执行](docs/agent-lessons.md#69-审计缺陷修复与并行执行) | 挂载三原则、所有权文件不相交、共享 git 对象库保护 | 审计修复与并行开发前 |
| [6.10 CLI/MCP 验收矩阵收口门禁](docs/agent-lessons.md#610-climcp-验收矩阵收口门禁) | 收口看板、脚本门禁、豁免条件与遗留追踪 | 触及 CLI/MCP/E2E 矩阵前 |

## 文档索引

### 治理与角色协议（任务系统权威，改动前先读）

| 文档                                                                                                                                  | 说明                                                                                             |
| ------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| [.agents/skills/cw-task-loop/references/role-protocol.md](.agents/skills/cw-task-loop/references/role-protocol.md)                    | 四角色共享协议（workflow_status 枚举 / Handoff §5 / finding schema §4 / 命令陷阱 §7 的唯一单源） |
| [Callwarden 无人值守循环启动模板：Executor v4.md](docs/role-loop-templates/Callwarden%20无人值守循环启动模板：Executor%20v4.md)       | Executor 启动入口                                                                                |
| [Callwarden 无人值守循环启动模板：Reviewer v4.md](docs/role-loop-templates/Callwarden%20无人值守循环启动模板：Reviewer%20v4.md)       | Reviewer 启动入口                                                                                |
| [Callwarden 无人值守循环启动模板：Adjudicator v4.md](docs/role-loop-templates/Callwarden%20无人值守循环启动模板：Adjudicator%20v4.md) | Adjudicator 启动入口                                                                             |
| [Callwarden 无人值守循环启动模板：Planner v1.md](docs/role-loop-templates/Callwarden%20无人值守循环启动模板：Planner%20v1.md)         | Planner 启动入口（design-only，capability 声明前不可派工）                                       |
| [docs/design/requirements.md](docs/design/requirements.md)                                                                            | 需求基线（Req 15 治理三件套之一，冻结）                                                          |
| [docs/design/cw-role-handoff-task-loop.md](docs/design/cw-role-handoff-task-loop.md)                                                  | Req 15 角色交接设计基线（冻结，v1 正文不再直接修改）                                             |
| [docs/design/cw-role-handoff-task-loop-v2-amendment.md](docs/design/cw-role-handoff-task-loop-v2-amendment.md)                        | 四角色模型修订（capability 分层与双轨整改，现行依据）                                            |
| [docs/design/tasks.md](docs/design/tasks.md)                                                                                          | 实施任务树基线（冻结）                                                                           |
| [docs/design/g0-experiment-protocol-v1.md](docs/design/g0-experiment-protocol-v1.md)                                                  | G0 盲评实验协议（g0-experiment skill 前置阅读）                                                  |
| [deliverables/software-company/git_recovery_record_20260828.md](deliverables/software-company/git_recovery_record_20260828.md)        | git 对象库 gc/prune 事故恢复路径（规则 48 引用）                                                 |

### 使用与开发文档

| 文档                                                                         | 说明                                                  |
| ---------------------------------------------------------------------------- | ----------------------------------------------------- |
| [TOOLS.md](TOOLS.md)                                                         | 工具使用指南（CLI/MCP/场景映射）                      |
| [README.md](README.md)                                                       | 项目首页                                              |
| [docs/quickstart.md](docs/quickstart.md)                                     | 快速开始                                              |
| [docs/cli_reference.md](docs/cli_reference.md)                               | CLI 命令参考                                          |
| [docs/mcp_tools.md](docs/mcp_tools.md)                                       | MCP 工具参考（工具数量权威口径）                      |
| [docs/architecture.md](docs/architecture.md)                                 | 架构设计                                              |
| [docs/deployment.md](docs/deployment.md)                                     | 部署指南                                              |
| [docs/agent-usage-guide.md](docs/agent-usage-guide.md)                       | 面向使用者的指南（外部 agent 集成引用，从本文件抽取） |
| [docs/agent-lessons.md](docs/agent-lessons.md) | Agent 经验教训（原 §6「重要注意事项」十节全文，强制阅读） |
| [docs/design/implementation-status.md](docs/design/implementation-status.md) | 实现状态总览（规则 22 同步目标）                      |
| [docs/design/migration-manifest.md](docs/design/migration-manifest.md)       | Rust 迁移 manifest 状态表（规则 7 客观证据源）        |
| [CONTRIBUTING.md](CONTRIBUTING.md)                                           | 贡献指南                                              |
| [CHANGELOG.md](CHANGELOG.md)                                                 | 版本变更                                              |


## Call Warden 自动沉淀规则

<!-- CALLWARDEN_RULES_START -->
<!-- 自动同步区域，请通过 cw rule sync 更新，不要手改 -->
<!-- CALLWARDEN_RULES_END -->

<!-- cw-agent-rules:start -->
<!-- 由 cw rule sync-agents-md 自动维护，请勿手改 -->
- **no-bare-except**: 禁止裸 except（应捕获具体异常类型）
- **no-print-in-lib**: 库代码禁止 print 调试输出（应使用 logger）
- **no-todo-in-commit**: 代码提交不得包含 TODO/FIXME 占位（P0 质量门槛）
<!-- cw-agent-rules:end -->



