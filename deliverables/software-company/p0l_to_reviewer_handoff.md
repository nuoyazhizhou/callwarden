# P0-L → 独立 Reviewer 交接信封（executor_ready_for_review）

> 本文件由用户治理代理（WorkBuddy）依据 `T-1787850432491-f42a2b8c` 的 executor_blocked_to_user 交接、
> 经权威库 `~/.callwarden/callwarden.db` 核验后整理，用于把 P0-L 路由给**独立 Reviewer**（不同 session/instance）。
> 注意：reviewer verdict 只能由独立 Reviewer 角色提交；executor / adjudicator 不得出具或冒充（否则 `E_CONTRACT_ROLE_MISMATCH`）。

## 最小路由 envelope

```text
Handoff:
  task_id: T-1787801315246-e3e3a08c
  step_id: S-1787801315285-f68b625c
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立 reviewer 只读核验提交 12aecc1 与本 review packet（R1/R2/R3 语义、负矩阵、归因、无部署）
  reason: R1/R2/R3 代码整改与负矩阵已在同一任务内完成并提交（commit 12aecc1）；全量回归失败均已归因并行会话；治理门禁未绕过；P0-L 当前 verdict 事件=0，须独立 Reviewer 出具正式 verdict。
  independence_requirement: required
```

## 完整 provenance

```text
Handoff:
  task_id: T-1787801315246-e3e3a08c
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立 reviewer 只读核验提交 12aecc1 与本 review packet
  reason: R1/R2/R3 代码整改与负矩阵已在同一任务内完成并提交；全量回归失败均已归因并行会话；治理门禁未绕过
  independence_requirement: required
  request_id: unavailable
  step_id: S-1787801315285-f68b625c
  report_request_id: unavailable
  evidence_path: p0l_step5_review_packet_20260828.md
  evidence_hash: sha256:c9d2682f6bc575207416b6db0f2a422841f0d716e41606710b1f65545ecff849
  identity:
    agent_id: executor-workbuddy-v1-cur
    agent_instance_id: unavailable
    session_id: cw-exec-wb-20260827-p0l
    model_id: workbuddy
    role: executor
```

## 背景与核验结论（已查权威库）

- P0-L `T-1787801315246-e3e3a08c`：`status=review`，6 步骤全 `done`；**verdict 事件 = 0**（既无 PASS 也无 BLOCKED 正式提交）。
- 独立治理核验曾 BLOCKED（R1 角色锚点 / R2 raw token / R3 投影一致性），结论要求「不得由 executor/adjudicator 冒充 reviewer verdict」，须独立 Reviewer → Adjudicator。
- 参考（同一主任务内已完成的整改）：`p0l_step5_review_packet_20260828.md`（commit `12aecc1`）、
  `P0-L Step3_Step4 独立治理核验：BLOCKED.md`（sha256:18078190bce15804ffb554c61a68266831bba633212ff4bb34423a09f1704a8d）。

## Reviewer 接手动作（独立、只读）

1. `task.next_action` 只读 probe P0-L，确认 `review` 态、无 claim 误导信号。
2. 只读核验 git commit `12aecc1`：R1/R2/R3 语义、负矩阵覆盖、§4 归因、无 scope 外吸入、无部署。
3. 仅输出 `PASS` 或 `BLOCKED`；PASS 后交**不同 instance/session** 的 Adjudicator（worker auth + server-side reviewer proof）。
4. Reviewer 不改合同、不传 token、不 claim/apply/close。
