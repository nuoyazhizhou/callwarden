# P0-L Step3/Step4 独立治理核验：BLOCKED

**核验日期**：2026-08-28  
**任务**：`T-1787801315246-e3e3a08c`，P0-L：Role Worker Task Contract policy / preclaim enforcement remediation。  
**核验角色边界**：独立只读架构/治理核验；本文件不是 daemon `verdict.submit`。当前 P0-L current contract 未解析 `identity_policy`，因此不能以聊天文本冒充合法 reviewer verdict。  
**未执行的动作**：未申请/续期/release lease；未读取 worker credential/lease token；未 claim/report/handoff/verdict/bootstrap/revise/apply/close；未改源码、未合并分支、未构建、未启动/停止/刷新 daemon 或 runtime，未直接访问 SQLite。

## 结论

**P0-L 不能在此时通过 independent review，也不能执行 `task.contract_revise` 解锁 step4。**Executor 报告的“step4 被新门禁 fail-closed 拒绝”本身是预期行为；但独立核验发现，P0-L step2/step3 尚未达到其自己的 Role Worker authority objective，并且 live authority 与 isolated source branch 的可证明来源不完整。因而此处不是“由 adjudicator 直接追加 policy revision，然后继续 step4”的合法时点。

> 当前最小结论是：**保留 P0-L 为 `in_progress`，不领取 step4；将下列可复现缺口作为同一 P0-L 的 remediation finding 交回 executor。**不能创建另一个 A″ task、不能批量 bootstrap/revise A″、不能让任何人借用 reviewer lease token 或 registered agent identity 规避它。

## 实测任务事实

当前 daemon 的 `task.status` 记录 P0-L 为 `in_progress` / `execution_in_progress`，共 6 steps，其中 step0–step3 为 `done`，step4 `S-1787801315285-f68b44ac` 和 step5 为 `pending`。没有 daemon verdict，review state=`not_in_review`。[1]

P0-L current Task Contract 是 revision 1，hash `sha256:aad8924e…cadfb3db`，但 envelope 未含可解析 `identity_policy`。对当前 HTTP authority 的两种 workspace-instance 输入进行的只读 `task.next_action` probe 均得到：

```text
identity_policy = null
identity_policy_status = unresolved
claim_requirements = { blocked: true, reason: "合同 revision 缺少可解析 identity_policy，claim fail-closed" }
```

这与 Executor 的 `E_TASK_IDENTITY_POLICY_MISMATCH` claim 阻断一致。然而同一 projection 仍保留 `next_action="claim_current_step"`，且 `blocking_conditions=[]`；系统若把 `next_action` 作为 machine-actionable command，仍会出现“文本 hint 可领取”与“claim 実际拒绝”相互矛盾的输出。[1]

| 项目 | 实际状态 | 结论 |
|---|---|---|
| step0–step3 | `done` | 有 daemon status/step result，但不等于 independent pass |
| step4/step5 | `pending` | 未领取；Executor 没有越权继续 |
| current identity policy | unresolved | P0-L self-bootstrap trap 真实存在 |
| `task.next_action` structured requirements | `blocked=true` | 部分符合 fail-closed |
| `task.next_action` routing/conditions | `claim_current_step` / empty blockers | **不符合“不得给出无条件 claim signal”验收项** |
| current review/verdict | `not_in_review` / none | 不具备裁决/关闭条件 |

## Finding P0L-R1：Role Worker 分支仍把 runtime ActionIdentity 当作强制授权前置

`handle_task_contract_revise` 在它读取或决定 `identity_policy` 前，无条件执行：

1. `parse_action_identity(params)`；
2. 检查非空 `agent_instance_id`；
3. `identity.role == "adjudicator"`；
4. `verify_registered_identity(&tx, &identity)`；
5. `validate_reviewer_lease_for_adjudication(..., &identity)`。

只有这些 legacy identity 规则通过后，代码才在目标/当前 policy 为 `role_worker_v1` 时额外调用 `enforce_role_worker_governance_write`，后者从 `role_worker_auth` 验证 expected adjudicator worker。[2]

`handle_task_claim` 同样在 policy branch 前要求可选 `ActionIdentity`，再对有 identity 的请求做 `agent_registrations` active、registered `agent_instance_id` matching、session matching、role independence 和 role-contract checks；进入 `role_worker_v1` branch 后还将 `identity.role` 作为 expected worker role。[3]

这意味着 provider/model/session/agent instance 仍不仅是 provenance：它们必须匹配预注册值才能完成 role-worker task 的 revise/claim。它与 P0-L 的唯一目标和用户冻结的核心身份规则冲突：**stable CW-local Role Worker + local credential 必须是授权锚点，runtime identity 只能追加记录、可变且无秘密。**

**R1 判定**：BLOCKED。修复要求是 policy-aware branch first，Role Worker mapped role（而非 client-supplied `identity.role`）决定 expected role；runtime identity 可选地记录为 provenance，不能作为 worker path 的 active/instance/session/registration authorization predicate。明确 `legacy_identity_v1` 才保留全部既有 rigid identity checks。

## Finding P0L-R2：contract revision 仍要求 adjudicator 使用 raw reviewer lease token

`handle_task_contract_revise` 仍读取一个 `(token, counter)`，再调用 `validate_reviewer_lease_for_adjudication`。该 validator 从 active reviewer lease 读取 stored token hash，要求调用者提供的 raw token 哈希匹配，并将 caller 的 adjudicator identity 与 reviewer lease holder 的 agent/instance/session 做不同性比较。[2] [4]

因此合同行为仍要求 **adjudicator 请求携带 reviewer lease 的 raw token**，即使在 role-worker branch 额外要求 adjudicator worker credential。这与 P0-L step2 的验收语义（reviewer proof 独立于 adjudicator；不得传递/暴露 raw reviewer token）不一致，也使当前“请独立治理角色直接 `task.contract_revise`”的建议不安全。

**R2 判定**：BLOCKED。修复要求是将 reviewer proof 改为 daemon-stored、capability-scoped、不可导出的 reference/receipt（例如 `reviewer_lease_id` + fencing + server-side proof lookup，并在 transaction 中验证 holder role/status/worker separation/currentness），而不是由 adjudicator 传入 reviewer raw token。此项修复不得将 reviewer credential 或 lease token 写入 Python、evidence、chat、DB payload 或日志。

## Finding P0L-R3：next-action 读模型同时给出 blocked requirements 与可 claim 路由

P0-L step3 evidence 描述目标是：unresolved policy 的 `next_action` “附 blocked 标记”，并且 P0-L acceptance 要求是“不出现 unqualified claim signal”。当前只读 probe 确认 `claim_requirements.blocked=true`，但同一 response 的 `next_action` 为 `claim_current_step`，且 `blocking_conditions` / `blocking_reasons` 均为空。[1]

**R3 判定**：BLOCKED。对于 `TaskContractPolicyState::Unresolved`、unknown policy、worker credential/role/separation known preconditions impossible 的情形，`next_action` 必须 machine-consistently return a blocked/diagnostic action（例如 `resolve_identity_policy`，next_role=`adjudicator` 或 `user` depending on contract state），并把 reason 同时写入 canonical `blocking_reasons` and compatibility `blocking_conditions`。`claim_current_step` 不能和 `blocked=true` 并列。`task.claim` 的 transaction gate继续保留作为 authoritative second line。

## Finding P0L-R4：运行中的 gate 不能被归因至未合并 P0-L commit

Executor 声称 step3 code commit `5b3e6f51…` 位于 `p0l-s3-tmp`，尚未并入 `master`。只读 Git 核验确认：该 commit 是 `p0l-s3-tmp` 的祖先，**不是 master 的祖先**；当前 master head 与 p0l branch head 已发生分叉，工作树中 `task_collab.rs`/`dispatch.rs` 也有未提交修改。[5]

同时当前 manifest/health 提供 PID 一致、schema=60、worker healthy；live executable 与 `%USERPROFILE%\.callwarden\runtime\current\cw-daemon.exe` 的 SHA 一致。但现有 manifest/receipt 不包含从 live binary 到 source commit 的构建 provenance，因此不能证明这个运行中的 policy gate 正是由 `5b3e6f5` 构建，更不能将“runtime/current 与 live 同 SHA”误称为 P0-L 的已审部署证明。[5]

**R4 判定**：BLOCKED（deployment provenance）。不要求停掉当前健康 daemon；但 P0-L 不得宣称已部署/已合规 live。待 P0-L source remediation 经过独立 review PASS 后，Adjudicator 才能授权受控 `refresh_shared_runtime.ps1`，并生成包含 source commit、artifact SHA、runtime/current SHA、live manifest PID/endpoint/health/schema 的一致性证据。

## 正确的解锁顺序

1. **不要现在 revise P0-L contract，也不要让 executor 继续 claim step4。**当前 revise path 本身含 R1/R2，且 P0-L 没有 reviewer PASS。
2. 将 R1–R3 作为 P0-L **同一主任务内**的具体 remediation finding；不要新建 A″ 或无关 child。由于 P0-L live gate 已 self-lock，其有限 bootstrap exception 需要用户明确决定为“仅允许 P0-L executor 在已存在 task/step 上做一次 resume to fix R1–R3”，并把该 exception append-only 记录在 P0-L evidence。它不扩展给任何其它 task，也不允许改变 role contract/历史事实。
3. Executor 只处理 R1–R3：在 code/test branch 修复 worker-first authorization、server-side reviewer proof，以及 next-action routing consistency；step4 test matrix必须覆盖三项。不得直接 deploy。
4. 重新进入 `review` 前，完成 step4/step5 report，并提供 clean captured commit（不吸收并行 dirty hunk）、negative test log、no-secret scan、source-to-binary provenance plan。
5. **独立 Reviewer**（不同 stable reviewer worker）在 P0-L contract revision available/legacy bootstrap exception strictly recorded情况下只读核验并在合法 daemon policy下提交 PASS/BLOCKED；Reviewer不改合同、不传 token。
6. **独立 Adjudicator**（different worker）仅在 PASS 后，以 worker auth + server-side reviewer proof/fencing，运行 policy-aware contract revision for P0-L self-bootstrap；再 apply/close，并在同一次受控发布流程中获准 refresh。
7. 只有 P0-L closed + source/build/live provenance converged 后，才允许为 A″ parent/G0 逐卡追加 policy revision；仍不可一次创建/claim A″-01…A″-37，直到 A″ release gates满足。

## Required Handoff

```text
Handoff:
  task_id: T-1787801315246-e3e3a08c
  step_id: S-1787801315285-f68b44ac
  from_role: reviewer
  outcome: reviewer_blocked
  next_role: executor
  next_action: 在同一 P0-L task 修复 R1 worker-first authorization、R2 server-side reviewer proof和R3 next-action blocking consistency；记录一次性 P0-L-only resume exception，不部署、不改 A″。
  reason: P0-L current contract unresolved而self-lock真实；但contract revise与claim仍依赖强制 runtime ActionIdentity，revision仍要求raw reviewer lease token，next-action仍输出无条件claim路由，未达到Role Worker as sole authorization anchor与fail-closed projection一致性。
  independence_requirement: required
```

## References

[1] [`p0l_task_creation_verification.json`](p0l_task_creation_verification.json) and [`p0l_next_action_policy_gate_probe.json`](p0l_next_action_policy_gate_probe.json): current P0-L task/step/projection facts.  
[2] [`rust_ext/src/daemon/task_collab.rs`](../../rust_ext/src/daemon/task_collab.rs), current `handle_task_contract_revise` lines 3322–3587 and `enforce_role_worker_governance_write` lines 1098–1123.  
[3] [`rust_ext/src/daemon/task_collab.rs`](../../rust_ext/src/daemon/task_collab.rs), current `handle_task_claim` lines 2116–2305.  
[4] [`rust_ext/src/daemon/task_collab.rs`](../../rust_ext/src/daemon/task_collab.rs), `validate_reviewer_lease_for_adjudication` lines 8729–8845.  
[5] [`p0l_branch_runtime_boundary_audit.json`](p0l_branch_runtime_boundary_audit.json): live runtime health/SHA and unmerged branch evidence.  
[6] [`p0l_step3_evidence_20260827.md`](../../.workbuddy/reports/p0l_step3_evidence_20260827.md) and [`p0l_step4_blocked_evidence_20260827.md`](../../.workbuddy/reports/p0l_step4_blocked_evidence_20260827.md): executor’s implementation claim and self-lock observation.
