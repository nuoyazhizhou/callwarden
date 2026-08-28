# 三角色 Loop 历史治理数据与持久化交接恢复计划

本计划挂载到 Epic `T-1787203926824-9f873bfc`，只创建两个非重叠子任务。两个子任务都必须通过 daemon 正式创建任务、步骤、workspace binding 和三角色合同；不得使用 SQL 直接修复生产数据库。

## 历史任务治理数据 reconciliation/migration
1. 只读盘点 Epic 全部子任务及其 lifecycle status、workflow projection、task steps、workspace binding、authority capture、Task Contract、Role Contract revision、handoff、verdict、evidence 和 commit 关联。
2. 生成带 task_id、问题分类、旧值、新值、修复依据和 hash 的 immutable reconciliation manifest，并区分可自动修复项与事实不足项。
3. 通过 daemon 正式 migration/reconciliation 接口幂等修复可确定的历史治理数据；保留原始事件和证据，不覆盖历史记录，不根据标题或聊天内容猜测合同、binding、verdict 或 handoff。
4. 对缺少不可变事实的任务统一输出 `governance_blocked`、blocking_reasons 和下一责任角色，确保 CLI/MCP/status-tree/list/status 使用同一投影。
5. 补齐回归测试：旧任务、缺 binding、缺 capture、缺合同、缺 revision、旧 progress ratio、已关闭但治理事实不完整等正负矩阵；完成部署后的真实 daemon round-trip。

验收：manifest 可复核且 hash 稳定；修复接口可重复执行不产生重复事件；历史证据保持 append-only；CLI/MCP 不再把缺失治理事实显示成 READY/COMPLETE；测试、release runtime 和 daemon round-trip 证据齐全。

禁止范围：不实现 handoff/assignment 工作队列；不修改 Reviewer/Adjudicator 裁决规则；不直接写 SQLite；不批量伪造合同、binding、verdict 或 evidence。

## daemon 持久化 handoff、assignment 与角色工作队列
1. 设计并实现 daemon 权威的 durable handoff/assignment/event 模型，精确绑定 task_id、step_id、from_role、next_role、next_action、source report/verdict/finding、request_id、evidence、identity、lease 和 fencing。
2. 将 Executor report、Reviewer PASS/BLOCKED、Adjudicator return/accept 转换为原子可重放的角色队列投影；Reviewer BLOCKED 必须在同一主任务追加 provenance-bound `fix_defect` step。
3. 提供按角色读取/领取/确认工作项的 daemon API，CLI/MCP 只做薄适配；聊天 Handoff 只能展示已持久化事件，不能作为唯一路由事实。
4. 实现 heartbeat、超时、stale claim 和同角色 Agent 原子接管；同角色新 Agent 可接管超时任务，禁止跨角色绕过 lease、合同或 fencing。
5. 补齐幂等、并发、重放、断线恢复和角色不在线时的可观测状态测试；验证 worker 轮询后无需用户手动戳一下即可推进下一棒。

验收：每次角色交接都能从 daemon 查询到唯一 task_id/step_id 和责任角色；重试不重复创建 handoff/verdict/fix_defect；超时后同角色可安全接管；CLI/MCP 显示 queued、execution_in_progress、review_pending、adjudication_pending、remediation_pending 等状态；完成真实多角色 round-trip 和部署证据。

禁止范围：不负责历史治理数据批量清洗；不允许聊天文本、客户端本地状态或 SQL 直接改变任务路由；不改变 Adjudicator 的 apply/close 权限边界；不引入 TLS/安全策略（当前仍为开发阶段）。
