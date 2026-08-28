---
name: cw-task-loop
description: Chat entrypoint for the cw task loop. Use when the user or an agent asks "what should happen next" for a cw task (`$cw-task-loop TASK_ID`). It runs the read-only `cw task next-action --json` evaluator, renders the Planner/Executor/Reviewer/Adjudicator role card verbatim from the daemon response, and gives claim/independence/lease guidance without ever mutating task state or faking identity.
---

# cw-task-loop Skill

共享状态、角色边界、结构化 finding、decision request 和 Handoff 字段见
`references/role-protocol.md`。本 Skill 只读渲染 daemon 派工；不复制治理角色的执行规则。

## Purpose

`task.next_action` is the single source of truth for the next legal action on a cw
task. This skill is a thin chat entrypoint: it reads `cw task next-action --json`
(daemon evaluator), renders the returned decision/action as a copyable role card, and
explains what the target role's **new** session may do. It never computes business
logic, never claims a step, never switches identity, and never mutates task state.

> 本入口 Skill 不是任务 Role Contract 所要求的 skill，绝不写入 `role_contract.skill_id`；
> 后者当前由 Executor 冻结并由 claim 校验；post-cutover（capability `planner_governance_v1`
> 声明后）计划侧合同由 Planner 冻结（见 v2 amendment §3）。

## Governance Status Projection

除 `decision`/`action` 外，必须原样展示 daemon 返回的任务进度字段：
`lifecycle_status`、`workflow_status`、`current_role`、`next_role`、`next_action`、
`review.state`、可用时的 `review.verdict_id`/`review.findings_count`，以及
`blocking_reasons`。`lifecycle_status` 是 `tasks.status` 的 apply/close 门禁；
`workflow_status` 是 daemon 根据 lease、Verdict Ledger 和派工事实派生的用户可读阶段。

`workflow_status` 的唯一枚举、各值含义与“已实现/协议保留”分层的**唯一单源**是
`references/role-protocol.md §2`；本入口不复制枚举列表，也不按职责方自行推算状态含义。
Planner 相关的 `planning_*`/`replanning_*` 与 `waiting_*` 为协议保留值：capability
`planner_governance_v1` 声明前 daemon 不 emit，出现于聊天时必须核对 daemon 投影而非采信。

`READY/PLAN`（协议保留：capability `planner_governance_v1` 声明前 daemon 不产生）、`READY/CLAIM`、`READY/REVIEW`、`READY/ADJUDICATE` 只表示动作可执行，不表示动作已经发生；
必须看到对应 daemon 成功响应。Reviewer PASS 不等于 `applied`，Reviewer BLOCKED 也不等于
“没有状态变化”，应分别显示 `adjudication_pending` 和 `remediation_pending`。不得以父任务、
step_id、request_id 或聊天文本替代精确 `task_id`。

## Handoff Output Contract

当本 Skill 的输出包含 Handoff、verdict 或最终状态说明时，必须先通过完整字段检查。完整字段列表、
固定顺序与 outcome 路由三元组的**唯一单源**是 `references/role-protocol.md §5`；本入口不内联字段块。
缺任一字段时，只能报告缺失事实，不能输出 `executor_ready_for_review`、`reviewer_pass` 或
`adjudicator_accepted`。`next_action` 不得写“领取下一个任务”；当前 task_id 交棒完成后，才可
重新执行发现循环。历史 Handoff 不可覆写，只能追加新的更正事件或说明。

## Use When

- 用户/Agent 询问「这个任务下一步该做什么 / 谁来做 / 能否领取」。
- 需要一张可复制的角色卡：Role、Task、Step、Skill、Allowed、Forbidden、Handoff。
- 需要在 report / verdict / handoff 后重新核对 daemon 的真实决策。

## Required Reading

1. 先读 `AGENTS.md`（身份声明、角色职责矩阵、强制下一棒交接 envelope）。
2. 读 `references/role-protocol.md`（唯一单源：状态枚举、finding schema、Handoff、capability 分层、命令纪律）。
3. 读 `docs/design/cw-role-handoff-task-loop-v2-amendment.md`（现行修订：四角色模型、capability
   分层 cutover、双轨整改；其 §1 supersede 映射指明 v1 中被替代的条目）。
4. 冻结设计 `docs/design/cw-role-handoff-task-loop.md`（v1）仅作**历史冻结基线**（blob
   `34668462…`）与未 supersede 条目（§7 分期路线图、workspace authority、Verdict Ledger 等）
   的依据；其 §3/§5 与 v2 amendment 冲突处一律以 v2 为准，不作为现行规则来源。
5. 执行 `& C:\Python314\python.exe cw.py task next-action <task-id> --workspace-instance-id <instance_id> --json` 并以其响应为唯一事实来源。
   - daemon 未启动时命令返回 `E_DAEMON_UNAVAILABLE`：向用户说明需 `cw daemon start`，不得伪造决策。

## Fixed Procedure

对 `<task-id>` 依序执行，不得跳步：

1. **读取** AGENTS.md、`references/role-protocol.md` 与 v2 amendment（见 Required Reading），并调用 `& C:\Python314\python.exe cw.py task next-action <task-id> --workspace-instance-id <instance_id> --json`。
2. **逐字渲染角色卡**（仅派生，不改写任何字段），格式见下节。
3. **`READY/PLAN`**（协议保留：capability `planner_governance_v1` 声明前 daemon 不产生）：只输出目标角色（planner）新会话应执行的规划指引、任务 Contract 和 identity 要求；
   **`READY/CLAIM`**：只输出目标角色（executor）**新会话**应执行的领取指引、
   task contract / role contract id 与 identity 要求；仅该新会话可显式调用现有
   claim/lease 路径；不得从聊天内容伪造 identity。
4. **`READY/REVIEW`**：要求创建独立 Reviewer window/session；**`READY/ADJUDICATE`**：
   要求创建同时独立于 Executor 与 Reviewer 的 Adjudicator window/session；不得在同一
   窗口内转换角色。
5. **`WAITING` / `BLOCKED` / `COMPLETE`**：只解释来自 cw 的原因，不执行任何写操作；
   对含 `revision_hint` 的 **`READY/REVISE`**：逐字呈现 revision card 并交回 Executor——
   该派工只承载**实现缺陷**的 `fix_defect` 整改；若 finding 标注 `owner_route=planner`
   （scope/Contract/架构缺陷），Executor 复查确认后不得实施代码，按
   `references/role-protocol.md §3` 的 pre-cutover 临时桥接以 `executor_blocked_to_user`
   升级用户（post-cutover 由 Planner 处理）。Skill 不得替 Executor 修订计划，也不得让
   Reviewer/Adjudicator 创建整改步骤。
6. **每次 report 或 verdict 后重新查询**，不轻信 Agent 的最终自然语言。

当 daemon 返回 `decision_request` 时（协议保留：capability `decision_request_v1` 未声明前
daemon 不产生；若出现在聊天中，先核对 daemon 投影），必须显示当前精确 `task_id`、候选 A/B/C、
影响/风险、默认建议、未选后果和自由文本补充入口；没有多路线时不得制造选择题，唯一安全路径
直接交给 daemon 投影的下一责任角色。

## Task Binding and Handoff

`task_id` 是本 Skill 派生角色卡和后续交接的一级必填字段。它必须逐字取自
`cw task next-action <task-id> --json` 的顶层 `task_id`，并在角色卡、claim/review/verdict/handoff 的
输出中保持不变；`step_id` 只能标识步骤，`request_id` 或 `report_request_id` 只能补充 provenance，均不能
替代 `task_id`。

- 每次角色卡只描述该精确 `task_id`，不得把 Epic/父任务 ID 当作当前复核对象。
- Reviewer 或 Adjudicator 开始工作前，必须用该精确 `task_id` 重新查询任务、合同、步骤、workspace binding
  和前序事件；不得从聊天文本、标题或 evidence 文件名猜测另一个任务。
- 输出 `Handoff`、verdict 或最终状态说明时，第一项写 `task_id`；同时保留 daemon 返回的 `step_id`、请求/报告
  ID、证据路径与哈希、以及完整 identity（若这些字段适用于该事件）。缺少 `task_id` 时停止并报告绑定缺口，
  不输出“通过”“已交接”之类的替代结论。

## Role Card Rendering（逐字派生）

以 `cw task next-action <task-id> --json` 的 JSON 为唯一来源，按下表映射。字段缺失
则对应行标 `—`（不猜测、不补造）：

| 角色卡行 | 来源字段（JSON） | 说明 |
|---|---|---|
| `Role` | `authorization.acting_role`（缺省回退 `required_role`） | planner / executor / reviewer / adjudicator |
| `Task (task_id)` | `task_id`（附 `task_contract.id` / `revision` / `hash`） | 必须与 daemon 逐字一致；下游只复核此任务 |
| `Step` | `step_id` | 领取/整改步骤 id；review/adjudicate 为 `—` |
| `Skill` | `role_contract.skill_id` @ `role_contract.skill_version` | 只读呈现，绝不写入 |
| `Allowed` | `allowed_paths` | 角色合同允许路径，原样列出 |
| `Forbidden` | `forbidden_paths` | 角色合同禁止路径，原样列出 |
| `Handoff` | `routing.next_role` → `routing.next_action`（附 `routing.reason`） | 下一棒动作与理由；`origin_kind` 恒为 `system_evaluator` |

附加呈现（不写入）：

- `decision` / `action`：`READY/CLAIM`、`READY/REVIEW`、`READY/ADJUDICATE`、
  `READY/REVISE`、`WAITING/WAIT`、`BLOCKED/NONE`、`COMPLETE/NONE`；协议保留值
  `READY/PLAN` 仅在 capability `planner_governance_v1` 声明后出现。
- `next_session.must_be_new_session`：`true` → 必须新建独立窗口/会话。
- `authorization.different_agent_instance_from` / `different_session_from`：独立性约束。
- `blocking_conditions`：原样列出阻断原因。
- `blocking_reasons`：优先展示 daemon 提供的人类可读阻断原因；缺失时才展示兼容字段
  `blocking_conditions`，不得自行改写或合并。
- `lifecycle_status` / `workflow_status` / `review`：放在角色卡的状态段，帮助用户区分 raw 门禁和
  当前治理进度；字段缺失标为 `—`，不得用旧 status 猜测新状态。
- `revision_hint`：仅对 `READY/REVISE` 逐字呈现（含 `source_reviewer_verdict_id`、
  `finding_ids`、`proposed_action`、`allowed_paths`、`excluded_paths`、`acceptance`、
  `capture_isolation`），交回 Executor 自行修订计划。

## 决策行为表

| `decision/action` | 本 Skill 行为 |
|---|---|
| `READY/PLAN` | 协议保留：capability `planner_governance_v1` 声明前 daemon 不产生此派工；若出现在聊天中，先核对 daemon 投影。渲染角色卡；显示 `planning_pending`/`planning_in_progress`；仅提示 Planner 新会话分析复杂度、拆分任务和提交计划 |
| `READY/CLAIM` | 渲染角色卡；显示 `queued`/`execution_in_progress`；仅提示 executor 新会话的领取/lease 路径与 identity 要求 |
| `READY/REVIEW` | 渲染角色卡；显示 `review_pending`；要求独立 Reviewer window/session（`must_be_new_session: true`） |
| `READY/ADJUDICATE` | 渲染角色卡；显示 `adjudication_pending`；要求独立 Adjudicator window/session；apply/close 前须取真实 reviewer lease |
| `READY/REVISE` | 渲染角色卡；显示 `remediation_pending` 或 `remediation_in_progress`；逐字呈现 revision card，交回 Executor（仅实现缺陷的 `fix_defect`；`owner_route=planner` 的计划缺陷由 Executor 复查后按 §3 pre-cutover 桥接升级用户，不硬修）；Skill 不修订计划 |
| `WAITING/WAIT` | 只解释 lease 持有角色与等待原因；不写操作 |
| `BLOCKED/NONE` | 只解释 `blocking_conditions`；禁止 claim；不写操作 |
| `COMPLETE/NONE` | 只解释终态；不写操作 |

## Forbidden Shortcuts

- 不得调用任何 mutation（claim/lease/report/verdict/handoff/apply/close 等）作为本 Skill 动作。
- 不得领取步骤、切换身份、修改任务状态或替代 lease 校验。
- 不得从聊天内容伪造 `agent_id` / `session_id` / `model_id` / `role` / identity。
- 不得重算或改写 daemon 返回的 `decision` / `action` / `routing` / `next_session`。
- 不得替 Executor 修订计划，也不得让 Reviewer/Adjudicator 创建整改步骤。
- 不得在同一窗口从 Executor 转换到 Reviewer/Adjudicator。
- 不得在 daemon 不可用（`E_DAEMON_UNAVAILABLE`）时输出虚构决策。
- 不得输出只有六个路由字段的简化 Handoff；完整 provenance 字段缺失时必须 fail-closed。
- 不得把 `adjacent_defect` 隐藏为“超出本次 diff”；Reviewer/Adjudicator 的相邻缺陷必须按共享协议显示归属和阻断等级。

## Reference Map

- 决策形态与 12 条规则的**现行修订**：`docs/design/cw-role-handoff-task-loop-v2-amendment.md`
  §4 双轨整改（v1 §3/§5 已被 supersede，仅作历史基线，见该 amendment §1 映射表）
- 身份声明与交接 envelope：`AGENTS.md`
- 用户文档（窗口/会话独立性、角色卡示例、常见问答）：`references/user-guide.md`
- 四角色共享协议（状态、finding、决策请求、远端交互、命令纪律）：`references/role-protocol.md`
- 模板合规检查：`scripts/validate_template_compliance.py`
