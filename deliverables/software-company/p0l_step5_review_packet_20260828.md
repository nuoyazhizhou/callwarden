# P0-L Step5 Review Packet：R1/R2/R3 整改证据包（2026-08-28）

- 任务：`T-1787801315246-e3e3a08c`（P0-L，in_progress）
- 角色：executor（`executor-workbuddy-v1-cur` / session `cw-exec-wb-20260827-p0l` / model `workbuddy`）
- 依据：用户独立治理核验结论（`p0l_step3_step4_independent_governance_assessment_20260828.md`）——P0-L BLOCKED，
  由 executor 在**同一任务**中依已授权的 P0-L-only 限定自举例外修复 R1/R2/R3，补齐 step4 完整负矩阵与 step5 review packet；
  不得创建新 A″ 卡、不得部署。
- 整改提交：`12aecc192648635cc9da8da0c0ed2dc22cf040f8`（master，白名单隔离提交，4 文件，+5931/-1668）
- 台账：`cw_task_commit_ledger.json` 条目 `t1787801315246_p0l_step4_r1r2r3_fix_20260828`

## 1. 三项 Finding 修复映射

### R1 — worker-first 授权，runtime identity 仅 provenance

| 变更 | 位置（提交 12aecc1 内） |
|---|---|
| `validate_and_record_worker_first`（expected_role=None，返回 DB 登记 `worker_role`） | `rust_ext/src/daemon/task_loop/role_worker.rs` L408；共享内核 `validate_and_record_inner` L427 |
| `handle_task_claim` policy 四臂分流 + role_worker_v1 worker-first 分支 | `rust_ext/src/daemon/task_collab.rs` L2345 |
| `verify_contract_claim_match`（claim 与 worker 两路径共用三项合同匹配） | `task_collab.rs` L1297 |
| `handle_task_contract_revise` identity 可选 + `use_worker_path` 分流，事件/`created_by` 用 actor 归属 | `task_collab.rs` L3561 |

语义要点：
- role_worker_v1 分支下，**角色锚点唯一来源是 `role_workers` 登记的映射角色**（`worker_role`）；
  客户端提供的 `identity.role` 不参与任何授权判定，仅当提供时经 `record_action_identity` 追加为
  append-only provenance（可选、可为 None）。
- claim 下游（接管比较、`claimed_by`、`claim_role_owned`、响应 `role_contract`）统一按
  `worker_claim_role.or(identity.role)` 适配；接管在 worker 路径不要求 identity。
- `legacy_identity_v1` 与无合同历史路径**全部既有刚性 identity/registration/lease 校验原样保留**。

### R2 — server-side reviewer proof（raw token 不进入请求）

| 变更 | 位置 |
|---|---|
| `validate_reviewer_lease_proof_server_side` | `task_collab.rs` L9423 |
| revise worker 分支：raw token 拒绝 / proof 必填 / 错误映射 | `task_collab.rs` L3561（`use_worker_path` 分支） |

语义要点：
- adjudicator Role Worker 请求只携带 `reviewer_lease_id` + `fencing_counter`（daemon 存储、capability-scoped、
  不可导出的 reference）；请求含非空 `lease_token` → `E_REVIEWER_PROOF_RAW_TOKEN_FORBIDDEN`；
  缺 proof 任一字段 → `E_REVIEWER_PROOF_REQUIRED`。
- daemon 在同一事务内按 `lease_id` 查 `task_leases`（`role='reviewer' AND status='active'`）并逐项核验：
  任务归属（`E_REVIEWER_PROOF_TASK_MISMATCH`）、权威时钟过期（`E_LEASE_EXPIRED`）、
  fencing（`E_LEASE_FENCING_STALE` → 对外映射 `E_TASK_CONTRACT_REVISE_FENCED`）、
  holder 为 active registered reviewer 且注册 session 与 lease 一致（`E_GOVERNANCE_REVIEWER_UNREGISTERED`/
  `E_GOVERNANCE_REVIEWER_INVALID`）、与 adjudicator worker 三项分离
  （`E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_AGENT`/`_INSTANCE`/`_SESSION`）。
- `validate_reviewer_lease_for_adjudication`（legacy 路径）**函数体语义未变**（仅格式化重排），
  其 `registered_role != "reviewer"` 检查在 HEAD 即已存在（见 §4 归因）。

### R3 — next_action 投影的机器一致性（fail-closed 不与 blocked 矛盾）

| 变更 | 位置 |
|---|---|
| `handle_task_next_action` blocked_reason 覆盖 | `rust_ext/src/daemon/dispatch.rs` L1207 |

语义要点：
- policy `Unresolved` 或 `Declared(未知)` 时：`next_action=resolve_identity_policy`、`action/decision=BLOCKED`、
  `next_role=adjudicator`；reason 去重后**同时**写入 `blocking_reasons` 与 `blocking_conditions`，
  并保证 `claim_requirements.blocked=true` + `reason` 存在。
- `claim_current_step` 不再与 `blocked=true` 并列；`task.claim` 的同一事务门禁保留为权威第二道防线。
- role_worker_v1 任务投影 `claim_requirements.identity={"required":false,"provenance_only":true}`。

## 2. Step4 完整负矩阵（本次补齐）

### 2.1 claim（`task_collab.rs` 测试）
- 缺 `role_worker_auth` → `E_TASK_IDENTITY_POLICY_REQUIRED`
- 错误 credential → `E_ROLE_WORKER_CREDENTIAL_INVALID`
- 正例：成功领取 + `action_identities` append-only provenance + 事件 `role==worker 登记角色`
  （`test_task_claim_role_worker_v1_requires_expected_worker_and_records_provenance` L17783）
- **锚点证明**：adjudicator worker 携带伪装 `identity.role="executor"` → 仍成功，且
  `claimed_by`/事件 session/`role_contract.role` 全部为 worker 登记的 `adjudicator`，
  证明角色锚点与 runtime identity 解耦
  （`test_task_claim_role_worker_v1_role_anchor_is_worker_mapping_not_runtime_identity` L17920）

### 2.2 revise（`task_collab.rs` 测试，`test_contract_revise_role_worker_policy_upgrade_hash_linked_and_downgrade_forbidden` L17471）
- 缺 auth → `E_TASK_IDENTITY_POLICY_REQUIRED`
- 携带 raw `lease_token` → `E_REVIEWER_PROOF_RAW_TOKEN_FORBIDDEN`
- 缺 `reviewer_lease_id`/`fencing_counter` → `E_REVIEWER_PROOF_REQUIRED`
- 未知 `reviewer_lease_id` → `E_REVIEWER_PROOF_LEASE_NOT_FOUND`
- fencing counter stale（+1）→ `E_TASK_CONTRACT_REVISE_FENCED`
- adjudicator 与 reviewer lease holder 同 session → `E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_SESSION`
- 正例：**无 identity** 成功升级 revision 2，hash 链 + 事件归属
  （actor=`rwr-adj-worker`、session=`rwr-adj-sess`、role=`adjudicator`）
- 降级（role_worker_v1 → legacy）→ 拒绝（同闭包，带 proof+auth）

### 2.3 next_action 投影（`dispatch.rs` 测试，`test_task_next_action_unresolved_or_unknown_policy_projects_machine_blocked` L3847）
- revision envelope 无 `identity_policy`（unresolved）→ `resolve_identity_policy`/`BLOCKED`/`adjudicator`/
  `claim_requirements.blocked=true`/reason 恰一条写入 `blocking_reasons` 与 `blocking_conditions`
- `identity_policy="mystery_policy_v9"`（declared 未知）→ 同上

### 2.4 既有回归（不受影响证明）
- `test_task_next_action_projection_carries_policy_and_claim_requirements`（role_worker_v1 正投影 + 无合同原路径不变）
- `test_contract_revise_appends_revision_n_plus_1_via_handler` 等 legacy revise 路径
- `role_worker` 过滤器 20 项（含跨角色冒充矩阵、secret 拒绝、append-only provenance）

## 3. 回归证据

| 运行 | 结果 |
|---|---|
| `cargo check --lib` | 通过（0 error） |
| `cargo test --lib role_worker` | 20 passed / 0 failed |
| `cargo test --lib next_action` | 22 passed / 0 failed（含新增 R3 测试与修复的 `review_with_reviewer_lease_yields_review_not_waiting`） |
| `cargo test --lib "task_collab::tests"` | 90 passed / 0 failed |
| `cargo test --lib "dispatch::tests"` | 47 passed / 0 failed |
| `cargo test --lib`（全量） | **1535 passed / 14 failed**（归因见 §4） |

附带最小修复（非本整改 scope 的阻塞项，一行测试 setup 补齐）：
`next_action_test.rs` 的 `review_with_reviewer_lease_yields_review_not_waiting` 缺 `setup_task`
（commit `3cb5b28` 重排遗漏），导致 `set_task_contract` 因无 task binding 拒绝；补齐后通过。

## 4. 全量回归 14 项失败归因（均非本整改引入）

| 失败 | 数量 | 归因 |
|---|---|---|
| `daemon::task_supersede::*` | 8 | 并行会话提交 `bdfe012`（15 个未跟踪文件）自带测试：其 `register_agent` 以 `role='adjudicator'` 注册，而 `validate_reviewer_lease_for_adjudication` 的 `registered_role != "reviewer"` 检查在 HEAD 即已存在（本次仅格式化重排，语义未变） |
| `daemon::route_matrix::test_coverage_is_239` / `test_meta_tools_length` | 2 | 并行会话提交 `ddf2a87` 新增 `task.reconcile` 路由（239→240）未同步基线断言 |
| `cli::runtime::enterprise_mode_is_fail_closed_when_socket_is_missing` | 1 | `cli/runtime.rs` 最后变更为并行会话提交 `ca38130`；本次未触碰该文件（无本地 diff） |

归因方法：`git log` 文件归属 + `git diff` 核验 `validate_reviewer_lease_for_adjudication` 函数体
（仅空白/格式化差异）+ 失败文件均不在本次白名单内。

## 5. 遗留与边界声明

1. **bootstrap 路径残留（如实披露）**：`task_contract_bootstrap` 的 reviewer lease 校验仍走
   `validate_reviewer_lease_for_adjudication`（raw token 语义）。独立核验仅点名
   `task.claim` 与 `task.contract_revise`；bootstrap 属另一治理面，未在本任务 scope 内扩大。
   建议后续任务统一迁移到 server-side proof。
2. **R4 部署 provenance**：本轮**未部署、未构建发布产物、未刷新运行中 daemon**；
   live daemon 与 `runtime/current` 的 SHA 一致性及其 source-commit→artifact build provenance
   由独立核验指出的后续受控 convergence 流程处理。`p0l-s3-tmp`（d528283）非 HEAD 祖先，
   未移动该分支锚点。
3. **提交纪律**：白名单隔离提交（仅 4 个目标文件）；共享工作树中并行会话的脏文件
   （`cli_admin_handlers.rs`、`mod.rs`、`operation_store.rs` 等）未吸入；
   commit message 内嵌 task_id；ledger 已同步。
4. **no-secret scan**：对本提交全部新增行做凭证模式扫描（`sk-`/`secret`/`Bearer`/`api_key`/`AKIA`），
   0 命中（2 个命中为普通注释/标识符，无凭证）。
5. **治理边界**：未创建新任务/子任务；未执行 `task.contract_revise`；未 apply/close；
   P0-L 自身的 `role_worker_v1` policy revision 仅在通过独立 review / adjudication /
   受控 runtime convergence 之后，用新 path 追加。

## 6. 下一步（交独立 Reviewer）

- 只读核验提交 `12aecc1`：R1/R2/R3 语义与本包 §1 一致；负矩阵 §2 覆盖三项 finding；
  §4 归因成立；无 scope 外吸入。
- Reviewer 仅输出 `PASS`/`BLOCKED`；PASS 后交不同 instance/session 的 Adjudicator。

Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立 reviewer 只读核验提交 12aecc1 与本 review packet（R1/R2/R3 语义、负矩阵、归因、无部署）
  reason: R1/R2/R3 代码整改与负矩阵已在同一任务内完成并提交；全量回归失败均已归因并行会话；治理门禁未绕过
  independence_requirement: required
