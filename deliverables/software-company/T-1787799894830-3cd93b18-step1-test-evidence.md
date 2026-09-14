# exec7 step1 test 证据：daemon governance_projection 权威状态投影（no-delta 回归）

- Task: `T-1787799894830-3cd93b18` 补齐 daemon governance_projection 权威状态投影
- Step: `S-1787799894831-3cebacf8` test（target: rust_ext/src/daemon/task_collab.rs;
  rust_ext/src/daemon/task_loop/next_action_test.rs; tests）
- Executor 结论：step0 为 no-delta，本 step 针对已并入既有实现的投影语义运行回归测试，
  验证 governance_projection 所依赖的权威投影无回归。

## 1. 测试范围与命令

- 命令：`cargo test -- projection`（rust_ext，lib unittests + 其余 test target）
- 日志：`rust_ext/_exec7_proj_test2.log`
- 覆盖对象正是 step0 证据引用的权威实现及其语义：
  - `task_collab.rs`/`task_collab_tests_projection.rs` 的
    `daemon::task_collab::tests::projection::*`（task.create/claim/lease/handoff/status/
    list 的 governance 投影与 fail-closed）；
  - `task_loop/next_action_test.rs` 的 claim/lease/inbound_handoff/task_contract_bootstrap/
    verdict_evidence_gate 投影语义与 `projection_is_strictly_read_only`。

## 2. 结果

- `test result: ok. 39 passed; 0 failed; 0 ignored; 0 measured; 1705 filtered out`
- 关键用例（节选，均通过）：
  - `test_task_status_historical_blocked_vs_bound_projection`：历史任务缺 binding → 
    workflow_status=governance_blocked + blocking_reasons 非空；
  - `test_task_status_includes_steps_and_normalized_progress`：status 透传 steps/进度；
  - `test_task_create_writes_modern_governance_projection_atomically`：create 原子写入
    权威投影；
  - `test_task_claim_unknown_or_unresolved_policy_is_fail_closed` / role_worker_v1 系列：
    claim 前置校验与身份策略 fail-closed；
  - `projection_is_strictly_read_only`：投影评估只读、不触发任务迁移。

## 3. 与 gp.get 的关系

- `task.governance_projection.get` handler（task_collab_contract.rs L1395-1586）仅做：
  读 task status / 顶层字段透传 `tree_governance_projection`（query.rs L949，内部调用
  `task.next-action` 同一 `evaluate_next_action`）+ 只读诊断快照。其输出正确性由上述
  投影单测覆盖；handler 无新增逻辑，因此回归通过即证明 gp.get 无回归。

## 4. 结论

投影回归 39 passed / 0 failed，无回归，step1 验收通过。
