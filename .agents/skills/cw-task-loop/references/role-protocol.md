# Callwarden 四角色共享协议

本文是 Planner、Executor、Reviewer、Adjudicator 和 `cw-task-loop` 共用的协议索引。
它只定义交接、状态和发现的语义；权限、合同和状态写入仍以 daemon 为唯一 authority。

> **实现状态与 capability 分层声明（与 daemon 源码对齐）：**
> - **`planner_governance_v1`（未声明）**：Planner 原生派工（`READY/PLAN`、`planning_*`/
>   `replanning_*` 投影）、`planner` Role Contract、`planner_ready_for_execution`/
>   `planner_replan_required`/`executor_replan_requested` 三种 outcome 持久化。
> - **`decision_request_v1`（未声明）**：`decision_request`/`decision.respond` 落库与
>   `waiting_for_decision`/`waiting_for_input` 投影。
> - **`adjacent_relation_v1`（未声明）**：`adjacent_defect → related_to` 自动关联。
> - **当前已实现（无 capability 门禁）**：`reviewer_blocked` 触发 daemon 在同一主任务原子追加
>   provenance-bound `fix_defect`（`task_collab_lifecycle.rs` 现有 verdict 流程行为）。
>   `adjudicator_returned` **不在此列**：它只固定路由 executor，不自动追加 step/reopen；当前 CLI
>   没有对应命令，MCP adapter 也不满足 daemon 的结构化 findings 契约，因此尚无受支持的端到端
>   remediation bridge。角色不得改用通用 RPC 或自行投影 `remediation_pending`（详见 §3/§4）。
>
> 上述 capability 未由 daemon 公共 capability 通道声明前：`task.next_action` 不返回 `READY/PLAN`、
> 不 emit `planning_*`/`replanning_*`/`waiting_*`；`task.create`/`--role-contracts` 拒绝 `planner`
> 角色；`task.handoff` 只接受 §5 的已实现六种 outcome。Planner v1 启动模板在此期间为
> **design-only**，不得作为现行派工入口；计划/scope/Contract/架构缺陷按 §3 的 pre-cutover
> 桥接处理，不得由 Reviewer/Adjudicator 直接塞给 Executor 当实现缺陷硬修。客户端不得自行合成
> 规划中状态；缺少 capability 时必须显示 `governance_blocked`/明确错误，并将其作为内部 capability gap 进入修复计划，
> 不得假装可领取，也不得把技术修复责任转给用户。

## 1. 角色边界

| 角色        | 责任                                                            | 交付给                                                              | 现行状态                                      |
| ----------- | --------------------------------------------------------------- | ------------------------------------------------------------------- | --------------------------------------------- |
| Planner     | 澄清目标、复杂度预检、拆分 ownership、冻结 Contract/依赖/验收   | Executor；需要选择时 User/控制台                                    | design-only（`planner_governance_v1` 未声明） |
| Executor    | 实现冻结 scope、整改、回归、证据和 report                       | Reviewer；计划缺陷按 §3 升级                                        | 已实现                                        |
| Reviewer    | 独立复核实现、回归、影响半径和历史相邻缺陷；只提交 PASS/BLOCKED | Adjudicator；实现缺陷 → Executor；计划缺陷按 §3                     | 已实现                                        |
| Adjudicator | 独立核验 Reviewer PASS 及最终门禁；接受后 apply/close，或退回   | complete；当前退回固定路由 Executor 但 bridge 未闭合；目标双轨见 §3 | 已实现                                        |

`cw-task-loop` 不是治理角色，只读调用 `task.next_action` 并原样渲染角色卡。`g0-experiment` 是独立的 G0
盲审流程，不并入普通 loop。

## 2. 双层状态与等待

客户端不得按标题、verdict 或 lease 自行推算状态，必须展示 daemon 投影：

- `lifecycle_status`：`open → in_progress → review → applied → closed`，是写入门禁。
`workflow_status` 的唯一枚举在本文维护；其他文档只引用本节，不再复制列表：

`queued`、`planning_pending`、`planning_in_progress`、`execution_ready`、`execution_in_progress`、
`replanning_pending`、`replanning_in_progress`、`remediation_pending`、`remediation_in_progress`、
`review_pending`、`adjudication_pending`、`applied_pending_close`、`completed`、`reverted`、
`governance_blocked`、`waiting_for_decision`、`waiting_for_input`、`unknown`。

实现分层（唯一标记处；其他文档禁止复制本列表后再各自标记）：

- **当前 daemon 已实现可 emit**：`queued`、`execution_in_progress`、`remediation_pending`、
  `remediation_in_progress`、`review_pending`、`adjudication_pending`、`applied_pending_close`、
  `completed`、`reverted`、`governance_blocked`、`unknown`（`workflow_status_for()` 实际返回集，
  其中 `unknown` 为未知 `tasks.status` 的兜底值；`open` 任务投影为 `queued`）；
- **协议保留值（daemon 不 emit；归属见顶部 capability 分层）**：`execution_ready`、
  `planning_pending`、`planning_in_progress`、`replanning_pending`、`replanning_in_progress`、
  `waiting_for_decision`、`waiting_for_input`。客户端不得自行合成保留值
  （`execution_ready` 依赖 `planner_governance_v1` 的计划冻结投影）。

当人工选择会改变方案、成本、数据迁移或权限时，daemon 追加 `decision_request`，投影为
`waiting_for_decision`；缺少用户事实或授权时投影为 `waiting_for_input`。**两者均为协议保留行为
（`decision_request_v1` 未声明前 daemon 不落库、不投影）**；pre-cutover 遇到真实多路线决策时，
由当前持棒角色在 handoff/verdict 文本中写明候选与缺口并升级用户，不得伪造 `waiting_*` 状态。升级内容只能是业务决策、外部事实、
验收或敏感授权；技术故障、数据错误、环境故障、Contract/binding 缺失和 daemon 能力缺口必须由内部角色继续修复。
这两种等待都必须展示：`request_id`、问题、候选方案、默认建议、截止/超时策略和不选择的后果。
没有多路线选择时，角色应直接执行唯一安全路径，不能把普通缺陷推给用户。

`READY/PLAN`、`READY/CLAIM`、`READY/REVIEW`、`READY/ADJUDICATE` 只是“可执行”，不是已领取或已完成。
只有 daemon 成功响应才代表事件发生；每次 report、verdict、apply、close 或 handoff 后都要重新查询同一 `task_id`。

## 3. 计划、重规划与双轨整改状态机

Planner 必须回答：目标/非目标、复杂度、ownership、依赖顺序、allowed/excluded paths、不变量、正负验收、
证据与部署门禁、回滚和 successor rule。满足任一条件即默认拆分或重规划：三个以上独立模块；schema+daemon+CLI/MCP/部署跨层；
超过五个步骤；多个独立验收目标；多个 owner；或单个 Executor 无法在原 scope 内安全修复。

### Planner 对自有任务树的治理缺口负责（强制）

任务树创建者或当前 Planner 发现其任务出现数据丢失/垃圾状态、Contract 或 workspace binding 缺失、认证恢复缺口、派工/交接
断链或 daemon/CLI/MCP 能力缺失时，必须将其归类为任务树治理缺陷并主动形成闭合路径。Planner 不得仅以“无现成 RPC”“当前
capability 未声明”或“需要用户手工修复”为终点。

若只有一条安全路径，Planner 应直接修订计划并安排最小的 Executor 实现范围，明确依赖、allowed/excluded paths、幂等键、正负
验收、证据、部署和回滚；没有现成接口本身就是需要补齐的 daemon/client capability gap。只有路线、成本、数据、权限或外部事实确实
存在不可合并的选择时，才升级用户，并提供 A/B/C、推荐项和未选择后果。用户只选择或授权敏感动作，不承担内部数据/合同/派工修复。

Planner 不因此获得超级管理员权限：仍不得直连 SQLite、伪造身份/lease、覆盖历史事件、写生产代码或代替 Reviewer/Adjudicator
裁决。当前 `planner_governance_v1` 未声明时，Planner 的原生派工/交接仍是 design-only；此限制必须如实记录为 capability gap，
并输出精确可执行的 Executor scope，而不是把治理责任转移给用户。

Executor 开工前必须重复复杂度预检。发现原 scope 过大、Contract 缺失或 ownership 相交时，不得先写代码或
创建无 Contract 的嵌套任务。post-cutover（`planner_governance_v1` 已声明）提交 `executor_replan_requested`
进入 `replanning_pending` 交 Planner；**pre-cutover 该 outcome 无法持久化**，不得因此把技术计划缺陷交给用户，
而应记录为内部 capability/governance gap，并由 Planner/维护任务准备可执行的修订或 Executor 实现路径。Planner 修订时保留历史版本，仅追加新 revision；
只有多条合法路线才需要决策请求（`decision_request` 为协议保留能力，pre-cutover 按本节
`waiting_for_decision` 段落的升级方式处理）。

### 双轨整改（按缺陷类别路由，不按方便性路由）

```text
实现缺陷（代码/测试/证据与冻结计划不符）
  → Reviewer BLOCKED：task.handoff reviewer_blocked
    → daemon 在同一事务原子追加 provenance-bound fix_defect
      （当前已实现的唯一自动 remediation 路径，task_collab_lifecycle.rs verdict 流程）
    → remediation_pending → Executor 领取 → remediation_in_progress
  → Adjudicator 退回：task.handoff adjudicator_returned
    → daemon 仅持久化 handoff 并固定路由 executor（不自动追加 step、不自动 reopen）
    → 当前 CLI/MCP 没有受支持的 provenance-bound remediation bridge
    → 重新查询同一 task；当前 next_action 可能仍按 Reviewer PASS 返回 READY/ADJUDICATE
    → 记录精确 task/verdict/handoff/source-step 缺口，交后续 daemon/CLI/MCP 实现任务；
      不得使用通用 RPC、伪造 step 或自行声称 remediation_pending

scope/Contract/架构缺陷（计划边界、拆分方式、验收目标、依赖或架构选择错误）
  → post-cutover：replan → replanning_pending → Planner
  → pre-cutover（临时桥接，如实反映 daemon 固定路由）：
      1. Reviewer 用现有 reviewer_blocked 提交；daemon 原子追加 fix_defect，
         finding.owner_route 必须写 planner，不得伪装成实现缺陷；
      2. Executor 领取该 fix_defect 后按 owner_route 复查：确认计划缺陷时不得实施代码、
         不得完成该 step，也不得把技术问题升级给用户；应将精确计划缺口、finding_id 和所需 capability 交 Planner/内部治理维护路径；
      3. Adjudicator 用 adjudicator_returned 提交后重新查询同一 task；由于当前没有受支持的
         remediation bridge，若仍为 READY/ADJUDICATE，必须记录治理实现缺口并等待后续
         daemon/CLI/MCP 修复，不得假装存在可领取的 Executor step。
    该桥接是 pre-cutover 的临时内部修复路径——Reviewer/Adjudicator 不能冒用
    executor_blocked_to_user（from_role 校验拒绝）；`next_role: user` 仅用于真正的业务决策、外部事实、验收或敏感授权。
```

**Parked remediation step（pre-cutover 无退出路径，如实披露）**：桥接第 2 步中，Executor 确认计划
缺陷后不完成该 fix_defect step，也不得把技术问题改交客户；该 step 将**保持未完成，且协议当前
不提供退出路径**——`workflow_status_for()` 对 `in_progress` + `revise_current_step` 仍投影
`remediation_in_progress`（可派工），`waiting_for_input` 是协议保留值（`decision_request_v1` 未声明，
daemon 不 emit），`step-resolve` 又要求 remediation step 已 done。因此该 step 保持未完成属**预期状态**：
**重复派工不构成新授权**；Executor 重复领取时应直接引用既有计划缺口事件与 finding_id，继续走内部 capability 修复，
不得重做、不得把技术问题重新升级给用户、不得实施未冻结代码。parked step 的 daemon 终止/取消语义列入
v2 amendment §3.3 未实施清单。

判定责任在 Reviewer/Adjudicator 的 finding 归属字段（见 §4）。Reviewer BLOCKED 路径由 Executor
领取前复查，不得把计划缺陷当实现缺陷硬修；Adjudicator 退回在 bridge 实现前没有可领取 step，必须
保持可解释阻断。目标双轨只追加（fix_defect step、计划 revision、decision event），不改写历史。

## 4. Review 的完整范围与统一 finding schema

Reviewer 和 Adjudicator 不只看本次 diff。除任务 scope 外，还要检查调用链、数据/接口不变量、运行时 round-trip、回归和变更影响半径。
发现非本次 scope 的已有问题时不得静默忽略，必须按下方统一 schema 记录为 `adjacent_defect`；
若它阻止当前交付，走 `reviewer_blocked`/`adjudicator_returned`；若不阻止，追加关联整改或 backlog 记录，由 daemon 保持 provenance。

**统一 finding schema（客户端约定，唯一单源；各模板/Skill 只引用本节，不得复制或改写字段列表）：**

| 字段                   | 含义                                                                                               |
| ---------------------- | -------------------------------------------------------------------------------------------------- |
| `id`/`finding_id`      | 唯一标识（daemon verdict 追加时生成）                                                              |
| `severity`             | `error`/`block`/`warn`/`info`                                                                      |
| `scope`                | `in_scope`/`adjacent_defect`/`governance`                                                          |
| `finding_type`         | 类别（如 semgrep、regression、architecture）                                                       |
| `introduced_by_change` | 是否由本次变更引入（相邻缺陷必填）                                                                 |
| `call_chain`           | 受影响调用链（相邻缺陷必填）                                                                       |
| `impact_radius`        | 变更影响半径                                                                                       |
| `root_cause`           | 根因                                                                                               |
| `reproduction`         | 复现证据/命令                                                                                      |
| `impact`               | 影响                                                                                               |
| `minimal_fix`          | 最小修复建议                                                                                       |
| `owner_route`          | **（所有 finding 必填）** 归属：`executor`（实现缺陷）或 `planner`（计划/scope/Contract/架构缺陷） |
| `blocking`             | 是否阻断当前交付                                                                                   |
| `acceptance`           | 修复验收标准                                                                                       |

> **daemon 契约边界（重要）**：上表 14 字段是**客户端约定，daemon 不校验**。daemon 的 findings
> 契约是独立的 3 字段 `[{severity, subject, fact}]`（`db/schema.py` 的 `task_verdict_events.findings`
> 与 `rust_ext/.../verdict_evidence_gate.rs` 的 `VerdictInput.findings`），以不透明字符串落库、零字段
> 校验；`owner_route`、`finding_type`、`scope`、`acceptance` 等字段在 daemon/CLI/server/db 中无落点。
> 14 字段按约定序列化进 3 字段：`severity` 对 `severity`，其余折叠进 `subject`（摘要，含 `owner_route`
> 归属标记）与 `fact`（事实/根因/复现/影响/最小修复）。
>
> **`owner_route` 必填（Reviewer/Adjudicator 侧义务）**：由于 daemon 不校验该字段，省略它在技术上
> 不会被拒绝，因此**提交不含可解析 `owner_route` 的 finding 本身即为 Reviewer/Adjudicator 的交付
> 缺陷**，不是可接受的默认状态。每条 finding 必须在 `subject` 中携带显式归属标记。
>
> **fail-closed 规则（例外分支，不是常态路径）**：若 Executor 收到的 finding 缺失 `owner_route`
> 或无法从 `subject`/`fact` 解析出 `executor`/`planner` 归属，必须按内部治理缺陷处理并记录上游交付缺陷；不得把协议缺陷升级给客户，
> 也不得使用 `executor_blocked_to_user` 伪装成客户阻塞。若当前 daemon 无法持久化内部路由，必须登记 capability gap 供后续实现；
> 不得默认为实现缺陷硬修，也不得自行推断归属。该分支应当罕见——若频繁触发，说明上游角色未遵守
> 上条必填义务，属需要单独整改的流程缺陷，而非 Executor 的常规升级理由。daemon 侧 findings schema
> 扩展与校验列入 v2 amendment §3.3。

**归属即路由**（与 §3 双轨一致）：`owner_route=executor` 的 finding 走 `fix_defect` → Executor；
`owner_route=planner` 的 finding post-cutover 走 `replan` → Planner，pre-cutover 按 §3 临时内部桥接
（Reviewer BLOCKED → Executor 复查后进入 Planner/治理维护路径；Adjudicator returned → 记录未闭合 bridge 的治理缺口）。

BLOCKED 不是终点。Reviewer 提交 `reviewer_blocked` 后，daemon 在同一主任务原子追加带
`source_verdict_id`、`finding_id`、`remediation_of_step_id` 的 `fix_defect` 并投影为
`remediation_pending`（**当前已实现的唯一自动 remediation**，无 capability 门禁）。Adjudicator 的
`adjudicator_returned` 退回**只持久化 handoff 并固定路由 executor，不自动追加 step/不自动 reopen**
（`task_collab_lifecycle.rs` 自动整改分支仅匹配 `reviewer_blocked`）。虽然 daemon 内部存在
`task.remediation.create` handler，当前 CLI 没有对应命令，MCP adapter 又把 `source_findings`
作为字符串透传，而 daemon 要求 JSON array；该入口也没有完成对 `adjudicator_returned` handoff event
与 source step 的权威绑定。因此当前不存在角色可合法使用的端到端 bridge。提交退回后必须重新查询并
如实显示治理缺口；不得改用通用 RPC、伪造整改 step 或自行声称 `remediation_pending`。该闭环属于后续
daemon/CLI/MCP 实现任务。
`adjacent_defect → related_to` 的自动关联才依赖 `adjacent_relation_v1`（未声明前由人工在
finding/ledger 中记录关联）。角色不应只在聊天中留下“blocked”，也不得手工改旧 verdict、旧 evidence 或数据库。

## 5. 结构化交接与人机分离

控制台/远端 worker 通过 task_id 订阅状态和 decision request；聊天文本只是通知，不是 authority。所有角色的交接首字段必须是精确 task_id：

```text
Handoff:
  task_id: <daemon next-action 返回的精确任务 ID>
  step_id: <相关步骤 ID 或 null>
  from_role: planner|executor|reviewer|adjudicator
  outcome: planner_ready_for_execution|planner_replan_required|executor_ready_for_review|executor_replan_requested|executor_blocked_to_user|reviewer_pass|reviewer_blocked|adjudicator_accepted|adjudicator_returned
  next_role: planner|executor|reviewer|adjudicator|complete|user
  next_action: <只针对当前 task_id 的明确动作>
  reason: <根因、证据或合同约束>
  independence_requirement: required|not_required|not_applicable
  request_id: <本次唯一请求 ID>
  report_request_id: <对应 report 请求 ID 或 unavailable>
  evidence_path: <任务证据或 manifest 路径>
  evidence_hash: <SHA-256 或明确 unavailable>
  identity:
    agent_id: <注册 agent ID>
    agent_instance_id: <注册 instance 或显式 unavailable>
    session_id: <当前独立 session>
    model_id: <当前模型>
    role: <当前治理角色>
```

上述字段顺序即规范顺序（`task_id` 首字段，`identity` 末位），本节是 Handoff 结构唯一单源；其他文档
与模板只引用本节，不得复制字段块或维护简化版。

**outcome 实现分层：**

- 已实现（daemon/CLI `task.handoff` 当前接受）：`executor_ready_for_review`、
  `executor_blocked_to_user`、`reviewer_pass`、`reviewer_blocked`、`adjudicator_accepted`、
  `adjudicator_returned`；
- design-only（`planner_governance_v1` 声明后启用）：`planner_ready_for_execution`、
  `planner_replan_required`、`executor_replan_requested`。pre-cutover 发送会得到 daemon 结构化拒绝，
  属预期 fail-closed，不得客户端本地补持久化。

**outcome 权威路由三元组（`from_role → next_role → independence_requirement`；本表为唯一单源，
已实现六种由 daemon `task.handoff` 逐字校验（`expected_route`，见 `task_collab_lifecycle.rs`），
design-only 三种为 `planner_governance_v1` 上线后的目标路由，客户端不得自行发明其他组合）：**

| outcome                                      | from_role   | next_role   | independence_requirement |
| -------------------------------------------- | ----------- | ----------- | ------------------------ |
| `executor_ready_for_review`                  | executor    | reviewer    | required                 |
| `executor_blocked_to_user`                   | executor    | user        | not_applicable           |
| `executor_replan_requested`                  | executor    | planner     | not_required             |
| `reviewer_pass`                              | reviewer    | adjudicator | required                 |
| `reviewer_blocked`                           | reviewer    | executor    | not_required             |
| `adjudicator_accepted`                       | adjudicator | complete    | not_applicable           |
| `adjudicator_returned`                       | adjudicator | executor    | not_required             |
| `planner_ready_for_execution`（design-only） | planner     | executor    | required                 |
| `planner_replan_required`（design-only）     | planner     | executor    | required                 |

`planner_replan_required` 固定 `planner → executor`（Planner 修订计划冻结新 revision 后重新交
Executor）；不得交 User，也不得跳过 Executor 直达 complete。需要用户决策时走 §6 决策请求，
不是改写路由。

字段无法验证时必须 fail-closed，不能声称 ready/pass/accepted。`next_action` 不得写“领取下一个任务”；当前 task 完成后再重新发现。

## 6. 决策请求显示规范

只有以下情形才暂停等待用户/控制台选择：存在两条以上安全但成本、风险或架构不同的路线；需要扩大 scope；需要外部授权；
或事实不足以安全继续。请求必须用清晰选择题，至少包含：

1. 问题与当前 task_id；
2. 候选 A/B（可有 C）及每项影响、风险和验证代价；
3. 默认推荐及原因；
4. 预计下一动作和未选择后果；
5. “以上都不合适/补充信息”的自由文本入口。

没有选择题时，agent 直接沿唯一安全路径推进；不得把身份缺失、可由同角色恢复的 stale claim 或普通代码缺陷伪装成用户问题。

## 7. 命令与已知坑（Windows/PowerShell）

以下是当前仓库的执行纪律，不是新的权限入口；所有写入仍须经过 daemon 和真实 Role Contract。
本仓库在 Windows 主机统一使用 Python 3.14（`C:\Python314\python.exe`，AGENTS.md 规则 42），命令一律为
PowerShell 语法，禁止 Bash 的 `$(...)`、`$VAR` 和未限定的 `python`/`python3`/`py`。

### Executor 交付与 VCS provenance

`task.report` 成功后，按以下顺序执行：

1. `git add <具体路径白名单>`，严禁 `git add .`；
2. `git commit -m "[<task_id>] <scope>: <what>"`，commit message 必须包含精确 task_id；
3. `$COMMIT = git rev-parse HEAD` 取得真实 commit id；
4. 将 `{task_id, commit_id: COMMIT, status: new_in_master, scope, note: "message 内嵌 task_id"}` 追加到
   `cw_task_commit_ledger.json`，再单独提交台账；
5. commit 之后才尝试 `& C:\Python314\python.exe C:/git_work/callwarden/cw.py refresh --all`（或项目规定的
   等价命令）。若 daemon 返回 `method_not_found: 未知方法: build_full_graph`，必须记录“刷新未完成”，
   不可用 SQLite/旧 CLI 旁路伪造成功。

**`.agents/` 曾长期被 gitignore 排除（历史陷阱，2026-08-28 已修复入库）**：旧版 `.gitignore`
（约 201 行）含 `.agents/` 规则，导致本目录 10 个文件（role-protocol.md、四个 Skill 及 g0 引用）
**从未入库**——尽管旧文本声称“已 tracked”，实际 `git ls-files .agents` 为 0，任何 clone 仓库的
agent 都拿不到治理协议。2026-08-28 已从 `.gitignore` 移除该规则并强制入库。现纪律：

- **新增文件必须显式核对**：在 `.agents/` 下新建 reference/skill 时，若 `git add` 静默跳过或 commit
  成功后文件不在提交内（旧规则残留或新忽略规则），必须用 `git ls-files .agents` 或
  `git show --stat <commit>` 确认该文件确实在提交内，不得只看 commit 成功。
- **禁止重新把 `.agents/` 加入 `.gitignore`**：本目录是四角色共享协议的权威源，AGENTS.md 多处引用，
  必须随仓库入库；任何新增忽略规则都不得覆盖 `.agents/`。

### Reviewer identity 与 lease

Reviewer 必须使用注册的五字段 identity。当前已登记实例的命令示例为：

```powershell
& C:\Python314\python.exe C:/git_work/callwarden/cw.py task next-action <task_id> --workspace-instance-id <instance_id> --json
& C:\Python314\python.exe C:/git_work/callwarden/cw.py lease acquire <task_id> --role reviewer `
  --agent-id <registered-reviewer-agent> --agent-instance-id <registered-reviewer-instance> `
  --session-id "$env:CW_AGENT_SESSION_ID" --model-id workbuddy
```

若使用已注册的 `reviewer-wb-186loop`，必须透传 `--agent-instance-id inst-reviewer-wb-186loop`，否则会得到
`E_IDENTITY_INSTANCE_MISMATCH`。lease 的 acquire → 使用 → release 必须完整；token 只在进程内使用，不能写入日志、
evidence、ledger 或模板。使用完必须执行（`--token` 必填，identity 与 acquire 时完全一致）：

```powershell
& C:\Python314\python.exe C:/git_work/callwarden/cw.py lease release <task_id> --role reviewer `
  --token <acquire 返回的真实 token> `
  --agent-id <registered-reviewer-agent> --agent-instance-id <registered-reviewer-instance> `
  --session-id "$env:CW_AGENT_SESSION_ID" --model-id workbuddy
```

无法取得唯一 reviewer lease 时 fail-closed，不借用其他角色身份。

### Adjudicator 的受保护收尾

Adjudicator 的 `apply`/`close` 使用** reviewer lease（不是 adjudicator lease）**，并携带真实
`--lease-token <token> --fencing-counter <n>` 以及完整 identity。完整命令序列（按
`apply → 验证 applied_pending_close → close → 验证 completed → release` 顺序执行）：

```powershell
& C:\Python314\python.exe C:/git_work/callwarden/cw.py task apply <task_id> `
  --agent-id <registered-adjudicator-agent> --agent-instance-id <registered-adjudicator-instance> `
  --session-id "$env:CW_AGENT_SESSION_ID" --model-id workbuddy --role adjudicator `
  --lease-token <reviewer lease token> --fencing-counter <n>

& C:\Python314\python.exe C:/git_work/callwarden/cw.py task next-action <task_id> --workspace-instance-id <instance_id> --json
# 确认 workflow_status=applied_pending_close 后：

& C:\Python314\python.exe C:/git_work/callwarden/cw.py task close <task_id> `
  --agent-id <registered-adjudicator-agent> --agent-instance-id <registered-adjudicator-instance> `
  --session-id "$env:CW_AGENT_SESSION_ID" --model-id workbuddy --role adjudicator `
  --lease-token <reviewer lease token> --fencing-counter <n>

& C:\Python314\python.exe C:/git_work/callwarden/cw.py lease release <task_id> --role reviewer `
  --token <reviewer lease token> `
  --agent-id <registered-reviewer-agent> --agent-instance-id <registered-reviewer-instance> `
  --session-id "$env:CW_AGENT_SESSION_ID" --model-id workbuddy
```

成功后立即 release reviewer lease，再重新查询同一 task_id。
任何 `E_LEASE_REQUIRED`、`E_LEASE_TOKEN_MISMATCH`、`E_LEASE_EXPIRED` 或 `E_LEASE_FENCING_STALE` 都不是完成，不能绕过。
