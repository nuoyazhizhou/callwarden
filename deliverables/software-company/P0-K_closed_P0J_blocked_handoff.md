# P0-K 已收口 · P0-J 待 Executor/Reviewer 前置 — 交接表

> 角色：Adjudicator（独立治理收口）｜本轮目标：推 P0-J-D → P0-K → P0-J → P0-G 解锁链
> 状态：**P0-J-D ✅ closed、P0-K ✅ closed；P0-J ⛔ 不能 close（缺前置）；P0-G 待 P0-J 后 close**

---

## 1. 已完成（已核验，权威库）

| 任务 | id | 结果 | 关键证据 |
|---|---|---|---|
| P0-J-D | T-1787402257549-67ba81e6 | ✅ closed | close 事件 #3575（adjudicator）；adjudicator role_worker `rw-adjudicator-wb-186loop-p0jd-8ec889d8` ≠ reviewer `rw-reviewer-wb-186loop-p0jd-a4f6cb5e` |
| P0-K | T-1787407700109-f5562c60 | ✅ closed | close 事件（closed_at 1787583420）；adjudicator `rw-adjudicator-wb-186loop-p0k-10ed3aec` ≠ reviewer `rw-reviewer-wb-186loop-p0k-eaa0693b` |

**daemon 修复**（方案 A）已部署并生效：
- `handle_task_close` S2 改为 `not_done = pending/blocked + unresolved_failed_step_ids(...)`，与 `handle_task_step_resolve` 同源 resolution-ledger 语义。
- 二进制 `~/.callwarden/runtime/current/cw-daemon.exe` sha `e2835341…`（pid 21380 运行中）。
- 两任务 close 均证明：已 step_resolved 的 failed 步骤不再拦截 close。

---

## 2. P0-J 卡点（⛔ 不能 close，需前置）

任务 `T-1787293818274-1b87b6c4`（P0-J）实测：

| 项 | 值 | 结论 |
|---|---|---|
| status | `review` | — |
| identity_policy | **`legacy_identity_v1`** | ⚠️ 与 P0-J-D/K 的 `role_worker_v1` **不同** → close 走 legacy identity 四元组，role_worker.enroll 不适用 |
| S1 子任务 | 0 children | ✅ PASS |
| S2 步骤 | 5 step 全 **`pending`** | ⛔ E_STEPS_NOT_DONE |
| verdicts | **0** | ⛔ 缺 reviewer pass，verdict 门禁不通过 |

5 个 pending 步骤：`implement / wire / adapt_client_cli / test / release_verify`。
已走桥记录：executor bootstrap_evidence(3543-3545) → review(3546) → bootstrap_reviewer_pass(3556) → task_contract_bootstrapped(3561)，但**步骤状态仍为 pending**（bridge 只写事件、未驱动 step 状态列）。

### 关闭 P0-J 的前置（按角色分离纪律，须交对应会话）
1. **Executor**：领 5 个 step（claim）+ 报 done（report，带 evidence_path/evidence_hash），使 5 step → `done`。
2. **独立 Reviewer**：对 P0-J 提交 `verdict.submit overall=pass`（session 须异于 executor，满足 F2）。
3. 之后 **Adjudicator** 方可 `task.close`（legacy identity 四元组；S1 PASS + S2 全 done + 有 pass verdict → 通过）。

> 本 Adjudicator 角色**不宜越权**代 Executor 推进 step0 或代 Reviewer 出 pass——按纪律交对应角色执行。

---

## 3. 解锁链当前状态

```
P0-J-D ✅ closed
P0-K   ✅ closed
P0-J   ⛔ review（5 step pending + 0 verdict）→ 需 Executor step0 + Reviewer PASS
P0-G   applied（待 P0-J close 后 Adjudicator close → 建 revision-2 批次解锁 128 卡）
```

注意：P0-J-D/K 的 `parent_id` 实为占位串 `T-P0J-ROLE-WORKER-IDENTITY`（非 P0-J 数字 id），
实测 P0-J 的 children 查询返回 0 条 → **P0-J close 不依赖 P0-J-D/K**；P0-G 的 S1 查自身 children。
故整链唯一活卡点是 **P0-J 自身 step 未 done + 缺 verdict**。

---

## 4. 下一步（待用户派工）
- **派 Executor** 推进 P0-J step0（5 step → done，附证据）。
- **派独立 Reviewer** 对 P0-J 出 pass verdict。
- 两者完成后，回本 Adjudicator 跑 P0-J close → 再 P0-G close → 建 revision-2 批次。

> 本 AI 已停手于 P0-J 卡点，未越权改写 step 状态、未伪造 verdict、未改动 DB/daemon。
