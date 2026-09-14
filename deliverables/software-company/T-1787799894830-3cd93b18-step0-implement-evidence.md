# exec7 step0 implement 证据：daemon governance_projection 权威状态投影（no-delta）

- Task: `T-1787799894830-3cd93b18` 补齐 daemon governance_projection 权威状态投影
- Step: `S-1787799894831-3ceb1e64` implement
- Executor 结论：**no-delta**。任务要求的治理投影能力在 claim 前已由既有合并提交完整并入
  HEAD（`98dc77c7108f462f0b97eb818e3ed71263a8998d`），本 step 无需新增代码改动；
  证据覆盖「字段验收映射」「单一权威实现」「门禁边界」「运行态验证」「回归测试」五部分。

## 1. 验收项 → 既有实现映射（字段级）

任务要求 `task.governance_projection.get` 统一返回的字段，逐一落在权威实现上：

| 要求 | 权威位置（HEAD 98dc77c） | 说明 |
|---|---|---|
| `lifecycle_status` | task_collab_query.rs `tree_governance_projection`（L949 起）；handler 透传（task_collab_contract.rs L1540） | 由 tasks.status 派生，历史任务无 binding 时保留原始 lifecycle 而非伪造 |
| `workflow_status` | 同上（governance_blocked/execution_in_progress/…） | 缺 binding/capture 或合同不可解析时 fail-closed 为 governance_blocked |
| `current_role` | 同上（来自 evaluate_next_action 投影） | 当前持有 lease 的角色；无则 Null |
| `next_role` | 同上 | 由 next_action 派生，如 adjudicator/reviewer |
| `next_action` | **复用 task_loop/next_action.rs `evaluate_next_action`（L1811 pub fn）**（query.rs L979 调用） | 与 task.next-action 同一派生语义，杜绝双实现漂移 |
| `review`（state/verdict/finding 数量） | next_action.rs review 结构：`state`(L1042) + `verdict_id`(L1044) + `findings_count`(L1047，verdict_findings_count L903 只回数量不回原文) | handler 整体透传 `governance.review` |
| `blocking_reasons` | tree_governance_projection 各 fail-closed 分支显式注入（query.rs L973/L996/L1092 等） | 返回原因文案，不做 token 泄露 |
| CLI/MCP 不显示空字段 | handler 顶层字段补齐循环（task_collab_contract.rs L1539-1559）+ 完整 `governance` 对象（L1560） | 顶层扁平 + 嵌套双通道返回 |

`task.status` / `task.status_tree` 同样返回 `governance`：
- task_collab_query.rs L90（task_list）与 L671（status_tree 节点）调用同一
  `tree_governance_projection` 并入 `governance`，客户端无需重算。

## 2. 单一权威实现 / 客户端零重算

- 投影全部在 Rust daemon 侧由 SQLite 只读事实（tasks/task_workspace_bindings/
  workspace_authority_captures/task_contract_revisions/task_verdict_events/task_events）派生；
- dispatch.rs L2973 `task.governance_projection.get` → store handler（L1356/L1362），
  CLI/MCP 均为纯透传客户端，不参与投影计算，不直写任何治理表。

## 3. 门禁边界未变

- 本 step 未改动 `task_collab_contract.rs` / `task_collab_query.rs` /
  `task_loop/next_action.rs` / `dispatch.rs`（`git diff HEAD` 为空）；
- apply/close 门禁逻辑保持原状：投影为只读派生，`evaluate_next_action` 仅做评估，
  不触发任务状态迁移（回归单测覆盖 `projection_is_strictly_read_only`）。

## 4. 运行态验证（live RPC）

daemon（PID 25816，127.0.0.1:13177）对任务本身返回：

```json
{
  "task_id": "T-1787799894830-3cd93b18",
  "status": "in_progress",
  "lifecycle_status": "in_progress",
  "workflow_status": "execution_in_progress",
  "current_role": "executor",
  "next_role": null,
  "next_action": "wait_for_current_lease",
  "review": {"state": "not_in_review"},
  "blocking_reasons": ["task 存在 active 未过期 lease（持有角色 executor），等待其释放"],
  "identity_policy_status": "declared",
  "governance": { "decision": "WAITING", "action": "WAIT", "required_role": "executor",
                  "step_id": "S-1787799894831-3ceb1e64", "blocking_conditions": [/*…*/] }
}
```

字段齐全、空字段被显式 Null/语义值替代、lease 不泄露 token 原文。

## 5. 回归测试

- `cargo test -- projection`（lib unittests，HEAD 98dc77c + 工作树无涉 exec7 的测试改动）：
  `test result: ok. 39 passed; 0 failed; 0 ignored; 1705 filtered out`
- 关键覆盖（节选）：`test_task_status_historical_blocked_vs_bound_projection`、
  `test_task_status_includes_steps_and_normalized_progress`、
  `test_task_create_writes_modern_governance_projection_atomically`、
  `projection_is_strictly_read_only`、claim/lease 系列 fail-closed 用例。
- 编译器无 error；日志：`rust_ext/_exec7_proj_test.log`。

## 6. 结论

实现要求已由既有提交完整满足；本 step 提交无差量（no-delta）证据，供 reviewer 复核。
