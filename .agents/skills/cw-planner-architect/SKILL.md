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
> 用户/上游任务承担；Executor 发现计划缺陷时按协议 §3 的 pre-cutover 桥接升级用户。

共享状态、finding、decision request 和 Handoff 字段见
`../cw-task-loop/references/role-protocol.md`；本 Skill 只补充 Planner 的复杂度与拆分规则。

## 适用范围

当任务刚创建、Executor 请求重规划，或现有 scope 无法安全作为一个原子交付时使用。本 Skill 只负责需求分析、
架构边界、任务拆分和计划交接，不写生产代码，不做 Reviewer/Adjudicator 裁决，不执行 apply/close。

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
声明前该 outcome 无法持久化；pre-cutover 由 Executor 以 `executor_blocked_to_user` 升级，Planner
角色在本 Skill 生效前不接收该请求），先复核其 finding 和已完成事实，再选择：收紧原 scope、拆出独立子任务、
或明确拒绝并说明原因。原计划和历史事件只追加，不覆盖。若存在多条会改变架构或成本的合法方案，创建结构化
`decision_request`（**协议保留**：`decision_request_v1` 未声明前不落库，改在交接文本中升级用户）；
若只有一条安全方案，直接修订计划，不等待用户聊天回复。

## 交接

计划完成后提交 `planner_ready_for_execution`，下一棒为 Executor；需要重新设计时提交 `planner_replan_required`，
下一棒为 Executor 或用户（仅在缺少授权/事实时）。两种 outcome 均为 **design-only**（pre-cutover 发送会被
daemon 结构化拒绝，属预期 fail-closed）。交接必须首字段携带精确 `task_id`，并包含 step、request、证据和
完整 identity；字段不可验证时不得声称计划可执行。

Planner 交接前必须把当前精确 `task_id`、计划 revision、拆分理由、依赖、每步 owner 和验收写入证据；若等待控制台选择，
使用结构化 `decision_request`（协议保留，同上），不得用没有选项的自然语言“请确认”。
