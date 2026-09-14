# 整改闭环死锁实证：task-level reviewer_blocked 的 fix_defect 无法领取

> 任务：T-1787913039309-bd573918 step0 `reproduce_unclaimable_remediation`
> 记录时间：2026-09-07（含 2026-08-28 历史 round-trip 存档）
> 冻结依据：role-protocol §5（reviewer_blocked 允许 null step_id）、task-level remediation provenance 语义

---

## 1. 死锁形态（fix_defect 元数据实证，check item 2）

task-level reviewer_blocked（handoff `step_id=null`）后，daemon 原子追加的 `fix_defect` 步骤
其 `result` 元数据中 `remediation_of_step_id` 合法为 **null**（task 级退回无源步骤），
provenance 由 `source_verdict_id` + `source_handoff_event_id` 承载。

### 1.1 历史案例（原实证任务，已 closed）

- task: `T-1787823611412-2f503878`
- step: `S-b8afc6356bb9e3c0e2b93431`（action=fix_defect, status=done）
- 实测 result 元数据（daemon 权威存储）：

```json
{
  "remediation_of_step_id": null,
  "source_outcome": "reviewer_blocked",
  "source_verdict_id": "V-7f799284d77f7394c57dda91",
  "source_handoff_event_id": 4279,
  "source_handoff_request_id": "reviewer-block-handoff-5a596775",
  "source_findings": [3 条]
}
```

### 1.2 存活案例（当前仍不可领取）

- task: `T-1787801315246-e3e3a08c`（P0-L，status=in_progress）
- step: `S-72da265216fbc2ed108c0102`（action=fix_defect, status=pending）
- 实测 result 元数据（daemon 权威存储）：

```json
{
  "remediation_of_step_id": null,
  "source_outcome": "reviewer_blocked",
  "source_verdict_id": "V-cde37b1a23b090d01a3ed5e3",
  "source_handoff_event_id": 5909,
  "source_handoff_request_id": "handoff-T-1787801315246-e3e3a08c-1-1787992554192"
}
```

---

## 2. 真实 RPC round-trip（check item 1）

### 2.1 历史 round-trip（2026-08-28，任务卡存档实证）

在 `T-1787823611412-2f503878` 上对 fix_defect step `S-b8afc6356bb9e3c0e2b93431`
执行 `task.claim`（携带 `remediation_step_id`），daemon 返回：

```
E_REMEDIATION_STEP_MISMATCH: remediation 步骤缺少 remediation_of_step_id provenance
```

根因（旧代码，`task_collab_lease.rs` 原 269-279 行）：用
`filter(非空) + ok_or_else` 强制要求 `remediation_of_step_id` 非空，task-level（null）被直接拒绝。

### 2.2 存活 round-trip（2026-09-07，本次真实捕获）

对存活死锁 step `S-72da265216fbc2ed108c0102`（`T-1787801315246-e3e3a08c`）
执行 `task.claim`（携带 `remediation_step_id`=该 step），daemon 实际返回：

```
E_TASK_IDENTITY_POLICY_MISMATCH: 任务 T-1787801315246-e3e3a08c 合同 revision 缺少可解析
identity_policy，禁止 claim（禁止隐式降级为 legacy）
```

结论：该 task-level fix_defect **在当前生产路径上仍然结构性不可领取**，只是拦截点从
2026-08-28 的 remediation provenance 校验前移到了更早的 identity_policy 门禁
（`task_collab_lease.rs` 163-169 行 `TaskContractPolicyState::Unresolved`）。
同一元数据（null remediation_of_step_id）在两条链路均无法通过 claim。

### 2.3 受控对照（strict gate 存活证明）

在无待处理 remediation 的正常任务上传 `remediation_step_id` 会被拒绝——严格门禁未放宽
（cutover 路径 `task_loop/claim.rs` 607-610 行 `(None, given)` → `E_REMEDIATION_STEP_MISMATCH`；
legacy 路径同语义，见 `claim_test::no_remediation_but_provided_remediation_id_rejected`）。

---

## 3. 根因与修复落点（供 step1/step2 核对）

- **写入侧**：`task_collab_lifecycle.rs` task-level reviewer_blocked 追加 fix_defect 时
  `remediation_of_step_id` 写 null（role-protocol §5 合法）。
- **读取侧修复（legacy）**：`rust_ext/src/daemon/task_collab_lease.rs`
  - `task_level_remediation_provenance_ok`（22-53 行）：unresolved 非空→拒绝；
    source_outcome 必须为 reviewer_blocked/adjudicator_returned；
    source_verdict_id 非空 + source_handoff_event_id 存在（数字或非空字符串）→ 放行。
  - claim 校验 356-369 行：`remediation_of_step_id` 为 null 时走 task-level provenance 分支。
- **读取侧修复（cutover）**：`rust_ext/src/daemon/task_loop/claim.rs`
  - 同语义 `task_level_remediation_provenance_ok`（502 行起）；
  - `check_remediation` 587-594 行 null 分支与 legacy 逐项一致。
- **负向矩阵（step3）**：`rust_ext/src/daemon/task_loop/claim_test.rs`
  - `task_level_remediation_claim_succeeds`：null + verdict/handoff provenance → 可精确领取
  - `task_level_remediation_without_provenance_rejected`：缺 verdict/handoff → 仍拒绝
  - `task_level_provenance_cannot_bypass_failed_step`：unresolved failed step 不可绕过
  - `remediation_exact_claim_succeeds` / `remediation_requires_explicit_step` /
    `no_remediation_but_provided_remediation_id_rejected`：step-level 语义不变

**测试结果（2026-09-07）**：

```
cargo test --manifest-path rust_ext/Cargo.toml --no-default-features --lib claim
test result: ok. 52 passed; 0 failed
（其中 remediation 矩阵 6 项全过）
```

---

## 4. 结论

1. 死锁元数据模式（`remediation_of_step_id=null` + task-level provenance）在历史与存活任务上
   均有 daemon 权威存储实证。
2. 原 `E_REMEDIATION_STEP_MISMATCH`（2026-08-28）与现行 `E_TASK_IDENTITY_POLICY_MISMATCH`
   （2026-09-07）两次真实 round-trip 证明：**修复前该 step 永久不可领取**。
3. 两条 claim 路径（legacy + cutover）已落地 task-level provenance 修复，且负向矩阵测试
   6/6 通过——修复后具备 task-level 语义支持，同时未放宽无 provenance 拒绝。

---

## 5. step4 独立评审包（review manifest）

> 提交 HEAD: `f7b3b05e0d80f48e8fe9a59b5d600d19770003ef`（remote: `git@github.com:nuoyazhizhou/callwarden.git`）
> 记录时间：2026-09-07（step4 prepare_independent_review 冻结）

### 5.1 触碰文件行数

| 文件 | 行数 | 路径 |
|---|---|---|
| legacy claim 路径 | 1912 | `rust_ext/src/daemon/task_collab_lease.rs` |
| cutover claim 路径 | 693 | `rust_ext/src/daemon/task_loop/claim.rs` |
| 负向矩阵测试 | 623 | `rust_ext/src/daemon/task_loop/claim_test.rs` |

### 5.2 测试证据（cargo test --package callwarden-core --lib）

- 聚焦 remediation 矩阵：`task_loop::claim_test` **17/17 通过**
  - 正向：`task_level_remediation_claim_succeeds`、`remediation_exact_claim_succeeds`
  - 负向：`task_level_remediation_without_provenance_rejected`、
    `task_level_provenance_cannot_bypass_failed_step`、
    `no_remediation_but_provided_remediation_id_rejected`、`claim_rejects_step_not_owned_by_task`
- legacy 全量：`task_collab::tests::core` **35/35 通过**（含 reviewer_blocked / adjudicator_returned remediation 用例）
- daemon lib 全量：**1305 通过 / 2 失败**，两个失败用例（`snapshot_state::tests::test_resolve_true_workspace_id_prefers_source_of_truth`、
  `task_collab::tests::governance::begin_immediate_returns_after_bounded_busy_retries`）均为全量并行下的偶发失败——
  单独运行各自 **1/1 通过**，且属 snapshot_state / governance 模块，与本缺陷 remediation 修复无交集。

### 5.3 实况 claim round-trip（2026-09-07 复核）

对存活死锁 step `S-72da265216fbc2ed108c0102`（`T-1787801315246-e3e3a08c`，
`remediation_of_step_id=null` + reviewer_blocked + verdict `V-cde37b1a23b090d01a3ed5e3` + handoff event 5909）：

`
修复前（2026-08-28）：E_REMEDIATION_STEP_MISMATCH  remediation 步骤缺少 remediation_of_step_id provenance
修复后（2026-09-07）：已通过 remediation provenance 门槛，仅被更早的独立门禁拦截：
   E_TASK_IDENTITY_POLICY_MISMATCH  任务合同 revision 缺少可解析 identity_policy（旧任务合同缺陷，非本缺陷）
`

证明：同一请求已不再因 remediation provenance 被拒——修复在真实存活任务上生效；
剩余拦截属该旧任务合同独立缺陷（identity_policy 缺失），超出本缺陷范围。

### 5.4 评审范围说明

- 本缺陷仅改 claim 读取侧 remediation provenance 校验（legacy + cutover 双路径），
  不触碰 verdict/handoff/remediation 写入侧与任何历史数据。
- 修复已随 HEAD 提交（`4fac5a7`/`591bf86` 所在分支），工作区无相关未提交改动。
