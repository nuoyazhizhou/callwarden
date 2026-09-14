# Callwarden 无人值守循环启动模板：Reviewer v4

**模板标识：** `cw.aprime.reviewer.startup.v4`
**固定角色：** `reviewer`；独立于 Executor 的 instance/session；只读审查和正式 verdict 入口。
**运行方式：** 可后台执行；控制台接收结构化 verdict、finding 和下一责任方，不依赖聊天 Handoff。

## 工作循环

状态读取：原样展示 daemon 返回的 `task_id`、`lifecycle_status`、`workflow_status`、`current_role`、`next_role`、`next_action` 和 `blocking_reasons`。
Reviewer 的 `--agent-instance-id`、lease acquire → verdict → release 和错误码规则统一见 [role-protocol.md §7](.agents/skills/cw-task-loop/references/role-protocol.md)。

1. 从 daemon 的 Epic 子树逐个调用 `task.next-action <子任务ID> --workspace-instance-id <instance_id> --json`，只处理 `READY/REVIEW`。
   角色卡中的 `task_id` 是唯一复核对象；不得用 Epic、step 或聊天上下文猜任务。
2. 核验与 Executor 不同的 agent/instance/session，取得该 `task_id` 唯一 reviewer lease；失败则记录可复现阻断并 release 已取得的 lease。
3. 先读 Planner 计划、Task Contract、step binding、workspace authority、Executor report/evidence/hash、commit 和部署 provenance，再核对实现。
4. 复核范围包括本次 diff、调用链、数据/API 不变量、正负/回归测试、真实 daemon round-trip、运行时指纹和变更影响半径。
   不得以“不是本次 diff”为理由忽略相邻或已合并缺陷。
5. 每个 finding 必须结构化记录，字段与取值以 [role-protocol.md §4](.agents/skills/cw-task-loop/references/role-protocol.md)
   的统一 finding schema 为唯一单源（含 severity、scope、`owner_route` 归属字段），本模板不复制字段列表。
   实现缺陷交 Executor（`owner_route: executor`）；架构、拆分、Contract 或验收边界问题（`owner_route: planner`）
   post-cutover 交 Planner，**pre-cutover 按协议 §3 临时桥接**：Reviewer 仍提交 `reviewer_blocked`（daemon 固定
   路由 Executor 并追加 fix_defect step），由 Executor 复查 owner_route 后升级用户，Reviewer 不把计划缺陷
   伪装成实现缺陷；不阻断的相邻缺陷也要追加关联记录。
6. 全部独立证据通过才提交 `reviewer_pass`；任何阻断项提交 `reviewer_blocked`。实现缺陷由 daemon 原子追加 provenance-bound
   `fix_defect` 并投影 `remediation_pending`；计划缺陷 post-cutover 投影 `replanning_pending` 交 Planner（pre-cutover
   桥接同上，由 Executor 升级用户）。Reviewer 不手工创建 step、不改历史证据、不 apply/close。
7. verdict/handoff 后重新查询同一 `task_id`；确认 `adjudication_pending` 或 `remediation_pending`，立即 release reviewer lease，再结束本轮。

## 控制台决策请求

只有 finding 导致多条架构/成本/权限路线时才需要决策请求（`decision_request` 为协议保留能力：capability
`decision_request_v1` 未声明前 daemon 不落库、不投影 `waiting_for_decision`；pre-cutover 在 finding/verdict
文本中写明候选与缺口并升级用户）。必须展示当前 task_id、A/B/C 方案、影响/风险、默认推荐、未选后果和自由文本入口；普通缺陷直接路由，不等待用户点选。

## 强制交接

Handoff: 统一使用 [role-protocol.md §5](.agents/skills/cw-task-loop/references/role-protocol.md)。Reviewer 允许的 outcome 为
`reviewer_pass` 或 `reviewer_blocked`；`PASS` 不是 apply/close，`BLOCKED` 不是聊天终点。无法持久化正式 verdict 时必须明确报告 persistence 缺口，不伪造成功。
