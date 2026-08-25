# P0-K：治理写路径 Role Worker 授权矩阵

**Task**：`T-1787407700109-f5562c60`  
**Scope**：修复 P0-J independent Reviewer 已确认的 capability gap：对明确冻结 `identity_policy=role_worker_v1` 的任务，把稳定本地 Role Worker credential 接入 `verdict.submit`、`task.apply` 与 `task.close`；legacy 任务保持显式 legacy identity 语义。  
**非目标**：不删除 P0-J-D 的 pre-review deployment remediation，不处理 CLI-02/CLI-03/MCP-001 bootstrap，不回写历史 verdict/lease，不把 provider/account/model/session 变为授权条件。

## 当前实现事实

| RPC | 当前 handler | 现有强制 | 已验证 gap |
|---|---|---|---|
| `verdict.submit` | `task_collab.rs::handle_verdict_submit` | `parse_action_identity`、Reviewer role string、reviewer lease token/fence、Task/Role Contract binding | 本地 Role Worker credential 没有被解析或验证；external `agent/session/model` 是 lease holder matching 的授权条件。 |
| `task.apply` | `task_collab.rs::handle_task_apply` | reviewer lease token/fence；提供 `ActionIdentity` 时严格匹配其 agent/session/model | 无 `identity_policy` 分支、无 expected adjudicator Role Worker 验证、无 stable worker separation。 |
| `task.close` | `task_collab.rs::handle_task_close` | 与 `task.apply` 相同 reviewer lease token/fence；子任务/step close gate | 同上。 |
| `task.contract_bootstrap` | `task_collab.rs` 的 staged branch | 明确 `identity_policy=role_worker_v1` 时调用 `parse_role_worker_auth` 与 `validate_and_record(expected_role=adjudicator)`；legacy 同时拒绝 worker auth | P0-J 已部署的正确模式，可作为 P0-K 模板。 |
| `role_worker` domain | `task_loop/role_worker.rs` | OS CSPRNG、hash-only persistence、worker/instance/owner/credential/status 验证、跨角色 provenance conflict、无秘密 runtime provenance | 已具备，可重用，不应在 mutation handler 重新实现。 |

## 冻结的 staged policy

| Task Contract `identity_policy` | `verdict.submit` | `task.apply` / `task.close` | 禁止行为 |
|---|---|---|---|
| `role_worker_v1` | 必须 `role_worker_auth`，expected role=`reviewer`；验证 credential/worker/instance/status/owner；记录 `verdict.submit` runtime provenance；lease 只作 token/fence proof | 必须 `role_worker_auth`，expected role=`adjudicator`；验证 credential/worker/instance/status/owner；记录对应 runtime provenance；reviewer lease 只作 token/fence proof | 不得要求或比较外部 agent/session/model；不得在 credential 缺失时 fallback legacy；不得将 credential 写进 verdict/event/provenance。 |
| 未设置或 legacy policy | 现有 `ActionIdentity` + reviewer role + lease holder identity matching | 现有 legacy identity + lease behaviour | 若携带 `role_worker_auth`，明确拒绝，避免隐式 policy downgrade/混合。 |

`provider`、`account`、`model_id`、`agent_id`、`agent_instance_id` 和 runtime session 在 role-worker policy 中只允许进入 `role_runtime_provenance.runtime_payload_json`。这些字段可变，且不参与 credential、worker-role、instance、owner、lease token 或 fencing 的授权比较。

## 最小实现结构

1. 在 `task_collab.rs` 提取只读 helper：从最新 `task_contract_revisions.envelope_payload` 解析并严格识别 `identity_policy=role_worker_v1`。缺失、非 object 或其他值全部视为 legacy；该 helper 不能由 client request 覆盖。
2. 对每个 mutation，在事务开始后、任何写入前按 helper 分支：
   - **role-worker branch**：`parse_role_worker_auth(params)` 必须成功且存在；调用 `validate_and_record(tx, auth, peer.owner_key(), workspace_id, task_id, action_type, expected_role)`；lease token/fence 仍强制校验，但不得传外部 `ActionIdentity` 作为 holder-binding 条件。
   - **legacy branch**：保留现有 `parse_action_identity`、role check 和 `validate_lease_for_mutation(..., Some(identity))`。
3. verdict event 的 `reviewer_identity` JSON 加入无秘密 `authorization` reference：`mode`, `role_worker_id`, `role_instance_id`, `role_session_id`；runtime payload 只保留在 append-only `role_runtime_provenance`，credential 永不序列化。
4. 对 `task.apply` / `task.close` 的 role-worker branch，新增 reviewer-lease validator：校验 reviewer lease token、expiry、fence；查询当前任务已存在 role-worker-v1 reviewer verdict/provenance；然后由 `validate_and_record(... expected_role=adjudicator)` 自动拒绝同一 worker 的跨角色复用。该 helper 不比较 reviewer/adjudicator 的 external agent/session/model。
5. 所有验证、provenance append、verdict/state update 必须位于同一 daemon transaction；任何失败在写入前或事务 rollback 后返回。

## 必需测试矩阵

| Case | 预期 |
|---|---|
| role_worker_v1 `verdict.submit` 缺 credential | `E_ROLE_WORKER_AUTH_REQUIRED`，无 verdict/provenance 写入。 |
| role_worker_v1 reviewer credential 错误/撤销/worker-role=executor | credential/role error，事务无 mutation。 |
| role_worker_v1 reviewer 变更 provider/account/model/session | PASS；追加第二条无秘密 runtime provenance；worker 仍可授权。 |
| 同一 worker 先 reviewer verdict、后尝试 adjudicator apply/close | `E_ROLE_WORKER_SEPARATION_VIOLATION`。 |
| 独立 reviewer lease + 独立 adjudicator worker apply/close | 在 Reviewer verdict、lease/fencing、task state/steps gate 均满足时 PASS。 |
| legacy task 携带 role_worker_auth | 明确拒绝，不可混用。 |
| legacy task 不携带 role_worker_auth | 保持现有 identity/lease 行为。 |
| live authority drift | 受控刷新前后 manifest PID/executable/SHA/schema/commit 必须与 `runtime/current` 完全对应；不对应则 deployment proof FAIL-CLOSED。 |

## Deployment gate

P0-K 不得在独立 Reviewer PASS 前运行 `refresh_shared_runtime.ps1`。部署后只读 probe 必须同时报告 manifest executable path、PID、binary SHA、schema version、commit、health，以及 `role_worker.status`、缺 worker credential 的 `verdict.submit`/`task.apply`/`task.close` 的 fail-closed error。任何 debug `target\debug\cw-daemon.exe` 覆盖 `runtime/current\cw-daemon.exe` 的情况都必须返回 remediation，而不是宣称 capability live。

## Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_design_ready
  next_role: executor
  next_action: 在 P0-K lease 下实现 policy helper、Role Worker mutation authorization branch 与定向 Rust negative matrix；不得部署。
  reason: source-to-RPC authorization matrix、legacy compatibility boundary、runtime provenance boundary、transaction ordering 与 live-authority convergence gate 已冻结。
  independence_requirement: required
```
