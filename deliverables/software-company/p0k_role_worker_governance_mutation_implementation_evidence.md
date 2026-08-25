# P0-K：Role Worker 治理写路径实施证据

**Task**：`T-1787407700109-f5562c60`  
**Implementation scope**：`verdict.submit`、`task.apply`、`task.close` 对冻结为 `identity_policy=role_worker_v1` 的 Task Contract 使用稳定本地 Role Worker credential；`legacy_identity_v1` 保持原有完整 `ActionIdentity` 与 lease-holder matching 语义。  
**Deployment status**：**未部署**。本文件不是 P0-J-D remediation disposition，也不改变其 append-only 历史事实。

## 实施内容

| Area | Change | Security property |
|---|---|---|
| Frozen policy lookup | `current_task_identity_policy` 在同一 authority transaction 内读取目标任务最新 `task_contract_revisions.envelope_payload`，只承认 `legacy_identity_v1` 与 `role_worker_v1`。malformed/unknown policy 失败关闭。 | Client request 无法选择或降级授权模型。没有 Task Contract 的历史任务继续走显式 legacy 路径。 |
| Role Worker mutation authorization | `authorize_role_worker_mutation` 只在 `role_worker_v1` 分支解析 `role_worker_auth`，并调用已有 `validate_and_record`。 | 验证 owner、CSPRNG credential hash、冻结 worker-role、active instance/status，并原子追加 runtime provenance。credential 不写数据库、event 或证据。 |
| `verdict.submit` | role-worker policy 强制 expected role=`reviewer`；reviewer lease 仍校验 token/expiry/fence，但不以 mutable external agent/session/model 作为 holder authorization。 | provider/account/model/agent/session 可变而不失效；worker credential 缺失、错误、撤销或角色错误在 verdict write 前拒绝。 |
| `task.apply` / `task.close` | role-worker policy 强制 expected role=`adjudicator`；独立 `validate_reviewer_lease_for_role_worker_adjudication` 保留 reviewer lease 的 token/expiry/fence，且要求已有可验证的 reviewer Role Worker `pass` verdict。 | Reviewer lease 是并发与时序 proof；adjudicator credential 是执行授权。没有 reviewer verdict、无/旧 lease、或同 worker 同时 reviewer/adjudicator 一律拒绝。 |
| Audit | verdict `reviewer_identity` 增加最小 `authorization` reference：mode、worker ID、instance ID、role session ID。runtime payload 继续只留在 append-only `role_runtime_provenance`。 | 可追溯无秘密身份锚点；不会把 credential、hash 或完整 runtime payload 泄露至 verdict projection。 |
| Legacy compatibility | legacy task 携带 `role_worker_auth` 得到 `E_TASK_CONTRACT_IDENTITY_POLICY_MISMATCH`；未携带时保留既有 parse identity 和 lease holder matching。 | 禁止隐式混合与全局放宽 legacy authority。 |

## 定向验证

| Command / test scope | Result | Coverage |
|---|---|---|
| `cargo test --lib p0k_` | PASS：5 passed | policy fail-closed、legacy rejection、wrong role pre-write、runtime rotation、reviewer lease/pass verdict/separation、真实 `verdict.submit` handler。 |
| `p0k_role_worker_v1_verdict_handler_requires_worker_auth_and_omits_credential` | PASS：1 passed | role-worker-v1 contract 上缺 auth 返回 `E_ROLE_WORKER_AUTH_REQUIRED`；变更 provider/account/model/session 后稳定 reviewer worker 可提交；verdict projection 不含 raw credential。 |
| `cargo test --lib daemon::task_loop::role_worker::tests` | PASS：5 passed | CSPRNG/hash-only、错误 credential、role impersonation、runtime append-only、runtime secret rejection、revoked worker rejection、status 不回显 hash。 |
| Existing legacy regression: `test_verdict_submit_appends_replays_and_rejects_conflicts` | PASS | Existing legacy verdict path 仍可 append/replay/reject conflict。 |
| Existing legacy regression: `test_task_apply_writes_applied_at` | PASS | Existing legacy apply transition/audit behavior retained。 |
| Existing legacy regression: `test_task_close_lease_validated_with_clock` | PASS | Existing legacy close lease/fencing validation retained。 |
| `cargo check --lib` | PASS | Rust library type/borrow/transaction compilation passes；既有 repository warnings 未作为 P0-K regression 处理。 |
| `python -m py_compile server/daemon_client.py` | PASS | Python retains thin-client syntax; no Python business authorization added。 |
| `git diff --check -- rust_ext/src/daemon/task_collab.rs` | PASS | P0-K target file无新增 whitespace error。 |

> 全 target test 会尝试替换受 live debug daemon 锁定的 `target\debug\cw-daemon.exe` 并得到 Windows `os error 5`。因此验证固定使用 `cargo test --lib`，既不停止、不替换、不重启 live authority，也不把该二进制锁误报为功能失败。

## Deployment and authority boundary

当前 live authority 的 executable 与 `%USERPROFILE%\.callwarden\runtime\current\cw-daemon.exe` 仍存在已记录的 drift。P0-K Executor 没有运行 `refresh_shared_runtime.ps1`、没有重启 daemon、没有做任何 `task.apply` / `task.close`，也没有对 P0-J-D 作 remediation disposition。独立 Reviewer 必须先审查本实施及测试；仅在独立 PASS 与 Adjudicator 后续授权成立后，才能通过受控 refresh 进行 binary convergence，并重新核验 manifest/PID/health/SHA/schema/commit。

## Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_implementation_ready_for_test
  next_role: executor
  next_action: 在已领取的 P0-K test step 上复核 test log、source diff、runtime-drift boundary与no-secret evidence，然后追加 test report；不得部署、apply 或 close。
  reason: Role Worker authorization已仅接入role_worker_v1治理写路径；legacy保持显式分支；测试与受控build均通过。
  independence_requirement: required
```
