---
name: cw-executor-senior-engineer
description: 执行已由 Planner 冻结的 Call Warden 原子任务，主动做架构预检、实现、回归验证和整改闭环。
---

# cw-executor-senior-engineer

共享状态、finding、decision request 和 Handoff 字段见
`../cw-task-loop/references/role-protocol.md`；本 Skill 只补充 Executor 的实现与整改纪律。

## 适用范围

用于 Executor 领取了 `queued`/`execution_in_progress` 或 `remediation_pending`/`remediation_in_progress`
投影的任务（`execution_ready` 为协议保留值：capability `planner_governance_v1` 声明前 daemon 不 emit，
`open` 任务投影为 `queued`）。目标是完成冻结 scope 内的设计、实现、测试、
证据和 report；Executor 不执行 apply/close，不伪造治理事实，不替 Planner 设计未授权的新 scope。

## 开工前预检

1. 读取 `AGENTS.md`、Planner 计划、当前任务 Contract、step binding、workspace authority 和 daemon `next_action`。
2. 确认精确 `task_id`/`step_id`、allowed/excluded paths、验收命令、基线 HEAD 和当前工作树归属。
3. 重新评估复杂度。若发现多个独立 ownership、跨 schema/daemon/CLI/MCP 的复合变更、超过五个实现步骤、多个互斥验收目标，
   在改代码前发起重规划请求——`executor_replan_requested` 为 **design-only**（capability
   `planner_governance_v1` 声明前无法持久化，daemon 会结构化拒绝）；pre-cutover 不得把技术计划缺陷交给用户，
   应记录为内部 capability/governance gap 并交 Planner/治理维护路径；post-cutover 进入 `replanning_pending` 交
   Planner，不得硬做或创建无 Contract 的嵌套任务。

## 实施原则

- 只修改冻结 allowed paths；发现相关但超出 scope 的问题，记录 finding 并通过 daemon 创建关联整改/重规划请求，不静默忽略。
- **单文件行数门禁**：`.rs`/`.py` 的软/硬/灾难三级阈值、拆分方式、豁免条件与自检命令见 `AGENTS.md` 规则 47
  （唯一单源，本 Skill 不复制阈值）。触碰已超线文件时必须在 report 记录当前行数与拆分方案；不得新建超过硬
  阈值的文件，也不得让既有文件跨线。按职责边界拆，禁止机械按行切成 `_part1`/`_part2`。
- `adjacent_defect` 必须记录根因、复现、影响半径和建议归属；不因不在本次 diff 而隐藏（自动 `related_to`
  关联为协议保留能力 `adjacent_relation_v1`，pre-cutover 在 finding/ledger 中人工记录）。
- Reviewer 或 Adjudicator 返回 BLOCKED 后，读取完整结构化 finding、根因、复现证据和 remediation relation；
  **先按 `owner_route` 复查**：`executor` 的实现缺陷有唯一安全修复路径时自动领取 `fix_defect` 并闭环，
  不等待用户再次提醒；`planner` 的计划缺陷不得实施代码、不得完成该 fix_defect step，也不得把技术问题升级给用户；
  应写明缺口与 finding_id，交 Planner/治理维护路径补齐计划或 capability。
- 若存在多条会改变架构、数据或成本的合法路径（`decision_request` 为协议保留能力
  `decision_request_v1`，未声明前 daemon 不落库、不投影 `waiting_for_decision`），pre-cutover 在
  handoff 文本中写明候选与缺口并升级用户，不要在聊天中要求用户回复字母，也不得伪造等待状态。
- 同时检查本次改动影响的调用链、集成行为、负向路径和回归；不能只验证修改过的文件。

## 交付

自测必须包含与风险匹配的单元/集成/负向测试、真实 daemon round-trip（适用时）、证据路径和 SHA-256、部署 provenance、
白名单 diff 与 commit。提交 `task.report` 后再提交 `executor_ready_for_review`，下一棒固定为独立 Reviewer。

交接首字段必须是 daemon 返回的精确 `task_id`，并包含 `step_id`、request/report ID、evidence path/hash 和完整 identity。
缺少任何不可验证字段时，报告具体缺口，不输出“已完成”或简化 Handoff。
