# P0-L step4 阻塞补充说明（合同内证据，证据原件在共享目录 .workbuddy/reports/）

- 任务：T-1787801315246-e3e3a08c（P0-L）
- 步骤：S-1787801315285-f68b44ac（prove_policy_and_claim_negative_matrix，pending，未领取）
- 日期：2026-08-27
- 本文件为补充件：原阻塞证据 `.workbuddy/reports/p0l_step4_blocked_evidence_20260827.md`
  已随 report/handoff 绑定哈希，本件不改动原件，仅将结论落入任务白名单路径。

## 阻塞结论

1. 运行中 daemon 已含 P0-L step3 claim 门禁（提交 5b3e6f5@分支 p0l-s3-tmp）。
2. 本任务合同仅 1 条 generic revision，`envelope_payload` 无 `identity_policy` 槽位
   （受限自举例外创建时的历史形态），`identity_policy_status=unresolved`。
3. `cw task next` 领取被拒：`E_TASK_IDENTITY_POLICY_MISMATCH`（fail-closed，设计如此；
   需求基线要求 missing policy must fail closed，禁止隐式 legacy 降级）。
4. 解锁需独立治理角色经 `task.contract_revise` 追加 policy revision
   （reviewer lease；role_worker_v1 另需 expected adjudicator worker credential）——
   超出 executor 权限，未绕过、未硬开发。

## 待办（等待裁决）

- 由持有效独立 reviewer lease 的角色为 P0-L 追加 `identity_policy=role_worker_v1`
  （任务 objective 要求的最终态），或授权显式 `legacy_identity_v1` 声明；
- 解锁后 executor 续做 step4（负矩阵）与 step5（review 材料）。
