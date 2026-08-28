# P0-L step4 阻塞证据：任务自身合同缺 identity_policy，claim fail-closed

- 任务：T-1787801315246-e3e3a08c（P0-L）
- 被阻塞步骤：S-1787801315285-f68b44ac（prove_policy_and_claim_negative_matrix，pending，未领取）
- 记录者：executor-workbuddy-v1-cur（session cw-exec-wb-20260827-p0l）
- 日期：2026-08-27

## 1. 阻塞事实（实证）

1. `cw task next T-1787801315246-e3e3a08c`（四字段齐备身份）→
   `E_TASK_IDENTITY_POLICY_MISMATCH: 任务 T-1787801315246-e3e3a08c 合同 revision 缺少可解析
   identity_policy，禁止 claim（禁止隐式降级为 legacy）`。claim 未发生，任务/步骤状态零变更。
2. `cw task next-action T-1787801315246-e3e3a08c --workspace-instance-id 4baea3ff12c2ea5c --json` →
   `identity_policy=null, identity_policy_status=unresolved,
   claim_requirements={blocked:true, reason:合同 revision 缺少可解析 identity_policy…}`。
3. 权威库核验：`task_contract_revisions` 中该任务仅 1 条 revision（TC-…, revision=1, profile=code_change），
   `envelope_payload` 无 `identity_policy` 槽位——任务在"受限自举例外"下以 generic projection 创建，
   当时 create 尚不持久化 policy（正是 P0-L step1 修复的 gap）。
4. 运行中 daemon 已含 P0-L step3 claim 门禁（next-action 投影与 claim 拒绝消息均与
   5b3e6f5 实现逐字一致）——门禁按设计 fail-closed，工作正常。

## 2. 为什么不是缺陷而是设计

- 需求基线（任务 objective "Identity policy rules"）：missing/unknown/multiple/malformed policy
  must fail closed—not silently default legacy。
- 任务 objective 明确："P0-L must itself receive an append-only role_worker_v1 policy revision
  before final close."
- 受限自举例外只覆盖 task.create 与 executor implementation 的既有 claim；step3 门禁上线后，
  未声明 policy 的合同任务按规则一律拒绝，无隐式降级。

## 3. 解锁路径（超出 executor 权限，需独立治理角色）

`task.contract_revise`（rust_ext/src/daemon/task_collab.rs handle_task_contract_revise）：
- 需要 **独立于本执行会话的 reviewer lease**（validate_reviewer_lease_for_adjudication）+
  注册身份 + workspace binding 一致；
- 追加声明 `identity_policy=role_worker_v1` 的 hash-linked revision 时，额外强制
  **expected adjudicator worker credential**（enforce_role_worker_governance_write，step2 门禁）；
- 或（若治理决策为 legacy）显式声明 `legacy_identity_v1`（当前 policy 非 role_worker_v1 时
  走原路径，仅需 reviewer lease）。
- executor 自行执行将违反角色分离与本任务 forbidden（"implicit legacy downgrade" 禁止），不予尝试。

## 4. 请求

交 planner/user 裁决：
a) 由持有效 reviewer lease 的独立角色执行 contract_revise，为 P0-L 追加
   `identity_policy=role_worker_v1`（符合 objective 的最终态要求），随后 executor 继续 step4/step5；或
b) 明确其他受控解锁方式。
在此之前，step4（负矩阵）与 step5（review 材料）无法领取；本任务保持阻塞，不硬开发。
