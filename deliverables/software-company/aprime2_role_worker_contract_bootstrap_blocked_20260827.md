# A″ Role Worker Task Contract Bootstrap：BLOCKED（创建后无执行）

**日期**：2026-08-27  
**性质**：创建后的只读治理核验与阻断证据。  
**明确未执行**：没有 `claim`、`task.report`、`verdict.submit`、`task.apply`、`task.close`、`task.contract_bootstrap`、`task.contract_revise`、lease 操作、runtime refresh/deploy、direct SQLite/CAS 或 credential 读取。

## 已发生的最小入库事实

依用户授权，以下两个任务已经经当前 Rust HTTP daemon 的 `task.create` append-only path 创建，且创建回执显示 parent/G0 关系和 workspace binding 均存在。

| 对象 | 真实 task ID | 状态 | 作用 | 是否创建 implementation microtask |
|---|---|---|---|---|
| A″ parent | `T-1787800241076-0a1c1824` | `open` | A′ 后的可见 client-boundary 路线图 parent | 否 |
| A″-G0 | `T-1787800241077-e7fd7231` | `open` | 162 PyO3 exports 的静态 inventory/HTTP successor/retireability Gate | 否 |

创建时三份 executor/reviewer/adjudicator role contracts、workspace binding、parent 的唯一 `govern_visibility_and_release_boundary` step 和 G0 的四个静态 evidence steps 都由 `task.create` 同一事务写入。第一次建卡因 parent 没有 pending step，`task.create` 的 current governance projection 拒绝为 executor binding bootstrap；该失败 receipt 被保留，未删除或覆盖。恢复时只补入 parent 的最小 `govern` step，以原 generated task IDs 重试并成功创建 parent/G0；未创建 A″-01…A″-37。

## 发现：generic task.create 合同不含 identity policy，却被 next-action 视为可领取

创建后只读查询 `task.status`、`task.status_tree` 和带正确 workspace instance 的 `task.next_action`。parent/G0 均存在 current Task Contract revision 1，且 G0 是 parent 的唯一 child。然而 `task.next_action` 对 executor 返回了当前 step 和空 `blocking_conditions`。回传的 Task Contract 没有 `identity_policy` 字段。

| 验证项 | A″ parent | A″-G0 | 结论 |
|---|---|---|---|
| task 创建存在 | PASS | PASS | created receipt 与 status 一致 |
| G0 parent ID 指向 A″ parent | — | PASS | 唯一 child 正确 |
| implementation microtask 存在 | 否 | 否 | 符合最小入库边界 |
| current Task Contract revision | 1 | 1 | generic projection 已写入 |
| returned contract 有 `identity_policy` | 否 | 否 | **不可证明 role_worker_v1** |
| executor `task.next_action` 的 blocking conditions | 空 | 空 | **generic contract 可被派发，未强制 role-worker claim barrier** |
| claim 实际执行 | 未执行 | 未执行 | 保持 fail-closed |

因此，卡面文字中“必须先 contract bootstrap 才可 claim”的说明不能替代 daemon-side authorization。任何 executor 若以 legacy identity 取得 claim，都会违背 A″ R2 的稳定 Role Worker 原则。当前窗口不会借用 reviewer/adjudicator identity、credential 或 lease 修复这个问题。

## 源码原因

`TaskCollabStore::handle_task_create` 在 role contracts 存在时调用内部 `task_create_contract_envelope(task_id, title, description, steps)`，然后立即调用 `bootstrap_task_governance_contracts` 写入 current Task Contract/lineage/step binding。该 helper 使用固定的 generic code-change envelope，字段包括 objective/interfaces/allowed scope/acceptance/risks/rollback/dependencies/handoff，但**不读取 caller 提供的 `task_contract_envelope`，也不写 `identity_policy`**。[1]

另一方面，现有 `handle_task_contract_bootstrap` 的 contract creation flow 要求完整 legacy `ActionIdentity`，强制其 role 为 `adjudicator`，调用 `verify_registered_identity`，并以 `validate_reviewer_lease_for_adjudication` 验证 reviewer lease/fencing。它没有解析或验证 `role_worker_auth`。[2] `handle_task_contract_revise` 同样要求完整 legacy identity 和 adjudicator role；当前源码开头也没有 `role_worker_v1` path。[2]

> 这说明 P0-K 已修复的 `verdict.submit` / `task.apply` / `task.close` Role Worker 治理写路径还未覆盖 Task Contract 的 **bootstrap / revision / next-action claim enforcement**。这是独立的治理写入缺口，不能通过 A″ 描述文本、Python wrapper、伪造 session 或直接 SQLite 修补。

## 最小、正确的后续处置

A″ parent/G0 应保留为 `open` 可见路线图，但不可被领取或执行，直到独立治理修复完成。适当的修复应是一个新的、独立的 Role Worker governance task（不要把它混进 A″，也不要回写 P0-K 历史）。它的 scoped objective 应当是：

1. 对 `task.create` 的 Task Contract envelope 支持明确、fail-closed 的 `identity_policy`，并且拒绝 unknown/missing/multiple policy；
2. 对 `task.contract_bootstrap` / `task.contract_revise` 增加只针对 `role_worker_v1` 的 stable worker auth path，保留 `legacy_identity_v1` 的既有 identity path；
3. 将 `task.next_action` / `task.claim` 与 current Task Contract policy 绑定：role-worker policy 缺失、credential 无效/已撤销、wrong role、worker separation 不满足时，必须在 claim 前拒绝，且零 mutation；
4. 维持 reviewer lease/fencing 作为并发/审计 proof，不把 provider/account/model/agent/session 提升为授权锚点；
5. 增加 in-memory + handler tests：generic contract 不得被 role-worker-required task 派发、wrong/revoked worker fail preclaim、runtime provenance changes permitted、legacy behavior unchanged、no credential leakage；
6. 仅在独立 Reviewer PASS 后通过受控 runtime refresh 部署，核验 live binary/source/runtime schema/commit/SHA，且不改 P0-J-D 的历史 remediation。

该治理修复本身应先由合法 planner 在 root migration parent 下建立独立 card，冻结三角色 role-worker contract；完成独立 review/adjudication/controlled deployment 后，再由独立 Reviewer/Adjudicator 使用其 own worker credential、有效 lease/fencing 和无秘密 evidence 对 A″ parent/G0 追加符合 `role_worker_v1` 的 Task Contract revision。若 current `task.contract_bootstrap` 只适用于“projection completely missing”，则应在修复 scope 中定义 append-only `task.contract_revise` 的 role-worker-v1 branch，而不是删除 generic revision 或篡改历史。

在上述修复和独立处置完成前，**A″-G0 的唯一正确状态是 visible-but-not-claimable**。更不能创建、领取或实施 A″-01…A″-37。

## 证据索引

| 文件 | 内容 |
|---|---|
| `aprime2_task_creation_receipt.json` | 首次 `E_TASK_CONTRACT_BOOTSTRAP_INVALID` 的保留 receipt；无 task 写入成功 |
| `aprime2_task_creation_resume_receipt.json` | 恢复后的 parent/G0 成功创建 receipt；无 credential、无 implementation child |
| `aprime2_task_creation_verification.json` | 动态 manifest/health + task status/tree/next-action 的只读验证 |
| `aprime2_contract_dispatch_observation.json` | 精简后的 contract revision/identity-policy/next-action observation |
| `aprime2_pyo3_daemon_transport_convergence_task_draft_20260827.md` | A″ R1/R2：route-map visibility 与 role-worker bootstrap boundary |

## References

[1] [`rust_ext/src/daemon/task_collab.rs`](../../rust_ext/src/daemon/task_collab.rs)，`handle_task_create` 与 `task_create_contract_envelope`：generic projection 当前不带 `identity_policy`。  
[2] [`rust_ext/src/daemon/task_collab.rs`](../../rust_ext/src/daemon/task_collab.rs)，`handle_task_contract_bootstrap` / `handle_task_contract_revise`：当前仍要求 legacy complete identity 与 adjudicator/reviewer-lease path。  
[3] [`aprime2_task_creation_verification.json`](aprime2_task_creation_verification.json)：A″ parent/G0 的创建后只读 status/tree/next-action evidence。  
[4] [`p0k_role_worker_governance_mutation_implementation_evidence.md`](p0k_role_worker_governance_mutation_implementation_evidence.md)：P0-K 已覆盖的治理 mutation 及其明确边界。
