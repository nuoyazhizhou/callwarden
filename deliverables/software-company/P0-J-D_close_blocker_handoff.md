# P0-J-D Adjudicator close — BLOCKER HANDOFF（纪律停止）

> 角色：Adjudicator（独立治理收口）｜任务：`T-1787402257549-67ba81e6`（P0-J-D）
> 触发：用户指令「切 Adjudicator 跑 P0-J-D close」
> 结论：**close 当前不可达**，被 `task.close` 的 S2 叶子步骤门禁拦截；
> 根因为 daemon 内部不一致（resolution 已上链但步骤 `status` 列未翻转）。
> 按纪律规则：冲突/门禁 → **停止并输出结构化交接表，待用户裁决后再行动**。

---

## 1. 链上事实（已核验，权威库 `~/.callwarden/callwarden.db`）

| 项 | 值 |
|---|---|
| 任务 id | `T-1787402257549-67ba81e6` |
| 当前 status | `review` |
| parent_id | `T-P0J-ROLE-WORKER-IDENTITY`（status `in_progress`） |
| 子任务数 | **0**（→ S1 父/子门禁不适用） |
| 步骤数 | 4 |
| Reviewer verdict | `V-fe7724c2df6a5115f3a4a463`，overall=`pass`，phase=`blind_first_pass`，step=`T-1787404873453-27fa7200` |
| identity_policy | `role_worker_v1`（需 role_worker 凭证） |

步骤状态：

| idx | step id | action | status |
|---|---|---|---|
| 0 | S-...216f2740 | freeze_task_id_grammar | done |
| 1 | S-...21705598 | implement_and_test_task_id_gate | done |
| 2 | **S-1787402257708-2170b8d0** | prepare_controlled_refresh_evidence | **failed** |
| 3 | T-...27fa7200 | fix_defect | done |

resolution 事件（`task_events` #3574，`role=implementer`）：

```json
{
  "request_id": "resolve-p0jd-step3-1787580016",
  "failed_step_id": "S-1787402257708-2170b8d0",
  "remediation_step_id": "T-1787404873453-27fa7200",
  "evidence_path": "deliverables/software-company/deploy_governance_task_id_evidence.md",
  "evidence_hash": "d3c3fe45d832e236a273f4e64f13620bf26738cfa46888428053d7141764a5ad"
}
```

`fix_defect` 步骤 `result.remediation_of_step_id` = `S-1787402257708-2170b8d0` → **链接核对通过**。

---

## 2. 门禁分析（代码权威源 `rust_ext/src/daemon/task_collab.rs`）

`handle_task_close`（14453+）执行顺序：

1. `require_lease_params` — 需 `lease_token` + `fencing_counter`（active reviewer lease）。
2. `authorize_role_worker_mutation(...,"task.close","adjudicator")`
   → `validate_and_record`：role_worker 凭证有效、role=`adjudicator`、instance active、无冲突角色 provenance。
3. `validate_reviewer_lease_for_role_worker_adjudication`：
   - 存在 active `reviewer` lease（token/fencing/expiry 匹配）；
   - 存在 `overall='pass'` verdict；
   - **reviewer_worker_id ≠ adjudicator_worker_id**（角色分离）。
4. **S1 子任务门禁**：本任务无子任务 → 跳过。
5. **S2 叶子步骤门禁（拦截点）**：
   ```sql
   SELECT COUNT(*) FROM task_steps
   WHERE task_id=?1 AND status IN ('pending','failed','blocked')
   ```
   → idx2 仍为 `failed` → `not_done=1` → **`E_STEPS_NOT_DONE`**。
6. S5 写 `status='closed', closed_at`。

**根因不一致**：`handle_task_step_resolve`（4370-4480）只写入 `step_resolved` 事件并**重算任务 status**，
**从不 UPDATE `task_steps.status`**（idx2 的 `failed` 列永不变更）。
而 `handle_task_close` 的 S2 读取的是**原始步骤 `status` 列**，非 resolution ledger。
→ 步骤已"决议解决"但 `status` 列仍是 `failed`，close 永远被 S2 拦下。

---

## 3. 影响范围（连锁阻塞）

```
P0-J-D(review) ──child-of──▶ P0-J(in_progress)
P0-J ──child-of──▶ P0-G(applied, 曾被 E_CHILD_TASKS_NOT_CLOSED 拦)
P0-G close ──unlock──▶ revision-2 批次（128 个机械契约）
```

- `handle_task_close` 仅检查**被关闭任务的子任务**（S1），不检查父任务状态
  → P0-J-D 可独立关闭，**不受 P0-J 仍 open 影响**。
- 但 **P0-J close 受 P0-J-D 未 closed 影响**（S1：子任务未全 closed → `E_CHILD_TASKS_NOT_CLOSED`）。
- P0-G close 受 P0-J 未 closed 影响。
- 故 **P0-J-D 不关闭 → P0-J/P0-G 均无法 close → revision-2 批次解锁被全盘阻塞**。

---

## 4. 备选路径（待用户裁决，AI 不得私自绕过门禁）

| 选项 | 动作 | 合规性 | 风险/代价 |
|---|---|---|---|
| **A（推荐）** | 修 daemon：`handle_task_step_resolve` 在写事件后 `UPDATE task_steps SET status='done' WHERE id=failed_step_id`；**或** 让 `handle_task_close` S2 改为基于 `unresolved_failed_step_ids` 判定（与 resolution ledger 对齐）。改完重建 daemon 后合法 `task.close`。 | ✅ 治本，门禁语义自洽 | 需改 Rust + 重编译/重测；小改动 |
| **B** | 授权一次**审计可溯**的步骤 `status` 修正（idx2 → `done`/`skipped`），经受控、留痕机制 | ⚠️ 需显式授权 | 当前**无对应 daemon RPC**；直接改 DB 违反 append-only 权威写原则，须格外谨慎并全程留痕 |
| **C** | 暂缓 P0-J-D close，保留 `review+pass`；先推其他卡 | ❌ 不可行 | P0-J-D 不关 → 连锁阻塞 P0-J/P0-G → revision-2 解锁停滞 |

---

## 5. 建议下一步

1. 用户裁决选 **A**（修 daemon 一行，让 close 门禁与 resolution ledger 自洽）或 **B**。
2. 若 A：定位 `rust_ext/src/daemon/task_collab.rs` 的 `handle_task_step_resolve`（约 4460 行后）补一行步骤状态翻转；或改 S2 判定。重编译 daemon 后，由本 Adjudicator 重新走 close 全流程（acquire reviewer lease → enroll adjudicator role_worker → `task.close`），并核对 `reviewer_worker_id ≠ adjudicator_worker_id`。
3. close 成功后回写 handoff（event id / closed_at），再推进 P0-J / P0-K / P0-G。

> 注：本 AI 已停手，**未执行任何状态变更 RPC，未改动 DB，未改 daemon 源码**。等待用户裁决。
