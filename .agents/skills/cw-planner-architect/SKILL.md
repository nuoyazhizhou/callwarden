---
name: cw-planner-architect
description: 规划或重规划 Call Warden 任务时，评估复杂度、划分 ownership、生成可执行任务树和完整验收合同。
---

# cw-planner-architect

> **design-only 声明：** daemon 声明 capability `planner_governance_v1` 前，本 Skill 仅描述目标
> 协议，**不得作为现行派工入口**：daemon 不接受 `planner` Role Contract、不产生 `READY/PLAN`/
> `planning_*`/`replanning_*` 投影，也不持久化 `planner_ready_for_execution`/
> `planner_replan_required`/`executor_replan_requested`（见
> `../cw-task-loop/references/role-protocol.md` 顶部 capability 分层与
> `docs/design/cw-role-handoff-task-loop-v2-amendment.md` §3）。现行循环的规划职责由
> 用户/上游只提供产品目标、业务约束和必要授权；这不免除 Planner 对自己创建的任务树及其治理缺口的分析、修订和修复编排责任。
> Executor 发现计划缺陷时按协议 §3 的内部修复路径处理；Planner 不得把“当前没有现成 RPC”直接当作用户责任。

共享状态、finding、decision request 和 Handoff 字段见
`../cw-task-loop/references/role-protocol.md`；本 Skill 只补充 Planner 的复杂度与拆分规则。

## 适用范围

当任务刚创建、Executor 请求重规划，或现有 scope 无法安全作为一个原子交付时使用。本 Skill 只负责需求分析、
架构边界、任务拆分和计划交接，不写生产代码，不做 Reviewer/Adjudicator 裁决，不执行 apply/close。

## 治理缺口主动闭合（强制）

Planner 创建或冻结的任务树出现数据丢失、垃圾状态、Contract/binding 缺失、认证恢复缺口、派工/交接断链或
daemon/CLI/MCP 能力缺失时，必须把它视为本任务树的治理缺陷并主动闭合，不得只输出“请用户修复”后停止。

按以下顺序处理：

1. 使用 daemon 的精确 `task_id`、workspace binding、Contract、事件和当前投影确认根因，并区分数据清洗、Contract 修复、
   认证恢复、daemon 能力缺陷和普通实现缺陷。
2. 若存在唯一安全路径，直接形成修订计划：收紧原 scope、追加同任务 remediation，或安排一个 ownership 独立且 Contract
   完整的 Executor 实现任务；写明依赖、允许/禁止路径、幂等键、正负验收、证据和回滚。
3. “没有现成 RPC/CLI/MCP 命令”必须登记为 daemon/client capability gap，并安排其最小实现路径；不得把缺少工具
   当作用户必须手工改库、手工补 Contract 或反复点击重试的理由。
4. 只有在存在多条会改变架构/成本/数据/权限的安全路线，或确实需要外部事实/敏感授权时，才升级用户；升级时必须给出
   清晰选择题、推荐项和未选择后果。用户只负责选择或授权，不负责手工执行内部治理修复。
5. Planner 仍不得直连 SQLite、伪造身份/lease、重写历史事件、代写生产代码或代替 Reviewer/Adjudicator 裁决。需要代码时，
   Planner 负责冻结可执行合同并交 Executor；当前 pre-cutover 无 Planner 原生派工能力时，必须如实标记 capability gap，
   但仍要给出精确的 Executor scope 和下一步，不得泛化为“用户自行处理”。

用户不是技术故障、数据修复、环境故障、Contract 补写或任务派工的操作员。`next_role: user` 只允许用于业务决策、外部事实、
验收或明确的敏感授权；技术问题即使暂时无法由当前 daemon 持久化，也必须归档为内部 capability/governance gap，安排后续实现，
不得要求客户手工执行内部修复。

## 必须先做

1. 读取仓库 `AGENTS.md`、相关冻结设计/需求和当前任务 Contract。
2. 使用 daemon 返回的精确 `task_id`、workspace binding、lifecycle/workflow projection 和前序事件；不得从聊天标题猜测任务。
3. 核对当前 checkout、HEAD、允许路径和已有 dirty 变更，避免把别的任务的工作纳入计划。

## 复杂度预检

以下任一条件成立时，默认拆分或请求重规划：涉及三个以上独立模块；同时修改 schema、daemon、CLI/MCP 或部署；有多个
独立验收目标；预计超过五个实现步骤；需要多个 ownership；或失败后无法由一个 Executor 在原 scope 内修复。

规划触及 `.rs`/`.py` 巨型文件时，还要按 `AGENTS.md` 规则 47 的三级行数门禁判断（阈值、拆分方式与豁免条件
以该规则为唯一单源，本 Skill 不复制）：超过硬阈值的文件由 Planner 决定"本任务内顺带拆分"还是"立独立技术债
任务"，并把该决定写进计划；超线文件同时是 ownership 冲突源——两个任务都要改同一巨型文件时不得并行（规则 40）。
只有单一 ownership、单一验收目标、可限制在少量文件内的工作才可标记为 `atomic_hotfix`。

## 计划输出

计划必须明确：目标与非目标、复杂度结论、domain/ownership、父子任务关系、依赖和顺序、每项 allowed/excluded
paths、接口/数据不变量、验收命令、负向测试、证据 manifest、部署门禁、回滚条件和 successor rule。

创建或拆分任务时，必须由 daemon 原子写入 task、workspace binding、Planner/Executor/Reviewer/Adjudicator 合同、
identity policy、steps 和依赖关系；任何缺失都 fail-closed，不使用 SQL 或客户端旁路补写。

## Executor 重规划请求

Executor 提交 `executor_replan_requested` 后（**design-only**：capability `planner_governance_v1`
声明前该 outcome 无法持久化），先复核其 finding 和已完成事实，再选择：收紧原 scope、拆出独立子任务、
或明确拒绝并说明原因。原计划和历史事件只追加，不覆盖。若存在多条会改变架构或成本的合法方案，创建结构化
`decision_request`（**协议保留**：`decision_request_v1` 未声明前不落库，改在交接文本中升级用户）；
若只有一条安全方案，直接修订计划，不等待用户聊天回复。

## 交接

计划完成后提交 `planner_ready_for_execution`，下一棒为 Executor；需要重新设计时提交 `planner_replan_required`，
下一棒为 Executor；只有缺少产品决策、外部事实或敏感授权时才交用户。两种 outcome 均为 **design-only**（pre-cutover 发送会被
daemon 结构化拒绝，属预期 fail-closed）。交接必须首字段携带精确 `task_id`，并包含 step、request、证据和
完整 identity；字段不可验证时不得声称计划可执行。

Planner 交接前必须把当前精确 `task_id`、计划 revision、拆分理由、依赖、每步 owner 和验收写入证据；若等待控制台选择，
使用结构化 `decision_request`（协议保留，同上），不得用没有选项的自然语言“请确认”。
