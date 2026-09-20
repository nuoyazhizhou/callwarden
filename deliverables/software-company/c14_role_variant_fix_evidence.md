# C-14 修复证据：validate_lease_for_mutation role 变体集匹配

## 缺陷复述（C-14）

`validate_lease_for_mutation`（`rust_ext/src/daemon/task_collab_lifecycle_ops.rs`）使用精确
字符串匹配查找 active lease：

```rust
WHERE task_id = ?1 AND role = ?2 AND status = 'active'
```

C-24 role 归一化后，`lease.acquire` 调用 `canonical_claim_role()` 将 `implementer` 归一为
`executor` 后落库。因此当 `task.step.resolve` / `task.report` 等变更接口以 `role="implementer"`
调用 `validate_lease_for_mutation` 时，精确匹配永远查不到归一化后的 `executor` 行，恒返回
`E_LEASE_NOT_FOUND`，导致 remediation 已完成却无法 resolve、任务卡死在 `REVISE` 状态。

## 修复内容

改用与 `write_release`（`task_collab_lease.rs`）一致的变体集匹配：

```rust
// C-14：role 按治理角色变体集匹配（canonical_claim_role 归一化后新 lease 恒以
// 治理角色落库——如 implementer→executor；历史行可能仍是 runtime 名称）。
let (role_sql, role_params) = role_in_match(&canonical_claim_role(role));
```

`role_in_match()`（`task_collab_shared.rs`）展开为 `role IN (executor, planner, implementer,
tester, evidence)` 等 runtime 变体占位符，同时覆盖历史行（旧数据可能是 runtime 名称）与新行
（C-24 后恒为治理角色）。

## 行为级验证（2026-09-20，daemon 9424，binary sha 585c8459）

PYT-回归卡 `T-1788871227327-45c94bd8`（修复前卡在 `action=REVISE`，`task.step.resolve`
报 `E_LEASE_NOT_FOUND: role=implementer`）：

1. `task.step.resolve(failed_step_id=step5, remediation_step_id=step6)` →
   **成功**，`resolution_event_id=10388`（修复前同参数报 E_LEASE_NOT_FOUND）。
2. `revision_hint.failed_steps` 从 3 个（step3/step4/step5）降为 2 个（step3/step4），
   step5 成功移出 unresolved 集合。
3. 后续 step3/step4 的 remediation 创建、claim、report、resolve 全链路零
   `E_LEASE_NOT_FOUND`：resolution_event_id 10396（step3）、10402（step4）。
4. PYT 卡最终进入 `status=review, review=pending`——reviewer/adjudicator 环解锁。

## 影响面

- `task.step.resolve`、`task.report` 等经 `validate_lease_for_mutation` 的变更接口，
  以 runtime role（implementer/planner/tester/evidence）调用时不再误判 lease 缺失。
- 与 C-24 归一化语义对齐，与 `lease.acquire` / `lease.release` 的查询口径一致，
  消除同一治理角色三处查询口径不一致的隐患。

## 构建/部署

- `cargo build --release --bin cw-daemon`（隔离 `CARGO_TARGET_DIR`，rc=0，925s）
- 二进制 sha256 `585c84597748d28dfc0b96730dcfacb996906ce9556693731cc9582a66833108`
- git commit `4cd4f71`；daemon pid 17628 @ http://127.0.0.1:9424

## 代码变更

```
rust_ext/src/daemon/task_collab_lifecycle_ops.rs | 14 ++++++++++----
1 file changed
```

## A′ 环闭环终态（2026-09-20）

C-14 卡 `T-1789885106356-61299ee4` 全链闭环：

- **executor 环**：lease.acquire（orphan 自动回收，fencing=2）→ task.report（step0
  `S-1789885106360-615de168` done）→ 任务进入 review。
- **reviewer 环**：source-level（commit 8a5f83c：role_in_match+canonical_claim_role
  引入、精确 `AND role = ?2` 移除、工作区干净）+ behavior-level（live daemon
  pid 17628 / sha 585c8459：PYT 卡已 review、resolution ledger 3 条）→
  verdict.submit PASS（V-32d752e6cd62579c4fc62267，event 756）→
  task.handoff reviewer_pass（event 10406）。
- **adjudicator 环**：task.apply（event 10409，applied_at=1789889269.98）→
  task.close（event 10410，closed_at=1789889270.21）→ `status=closed`。

**附带解锁**：PYT-回归卡 `T-1788871227327-45c94bd8` 的 remediation 链路全通
（step5/step3/step4 resolve，事件 10388/10396/10402），任务从 REVISE 卡死状态
进入 review。
