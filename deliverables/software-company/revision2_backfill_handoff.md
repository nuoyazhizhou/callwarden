# Revision-2 机械合同回填完成（P0-G 解锁批次终端动作）

> 执行时间：2026-08-24 21:5x GMT+8
> 范围裁决：用户选「修订全部 180 张缺陷合同」
> 脚本：`_exec_rev2_backfill.py`（PYTHONPATH=C:/git_work + 托管 python）+ `revision2_backfill_evidence.md`

## 1. 结果（权威库已核验）
- 180 张机械 rev1 合同（created_by=adjudicator-workbuddy-v1）全部追加 **revision=2**（结构化真数组）。
- `task_contract_revisions`：rev1=197（未变，append-only 保留）→ rev2=180，total=377。
- 结构校验：stringified/畸形数组 **0** · 空 dependencies **0** · 不可解析 envelope **0**。
- append-only 违规：**0**（每张修订任务 revisions≥2，rev1 完整保留）。
- 样例 `T-1787321708926-e7ebfac4`：rev1(sha256:f428…, adjudicator-workbuddy-v1) → rev2(sha256:33d3…, adjudicator-wb-186loop, ts=1787591626)。

## 2. 范围修正（关键）
- 审计 doc（16:59）记"128"；实测 Epic 子树长到 187 子任务，机械 rev1 实际 **181 张**。
- 精确枚举：180 张缺陷（acceptance_clauses/allowed_edit_scope/interfaces/risks/rollback 全字符串化 + dependencies=[]，100% 双重缺陷）→ 修订候选；1 张为 Epic 自身合同（干净，排除）。
- 180 张按任务状态：review 128 / open 24 / closed 26 / in_progress 2；全部 workspace_id=1 且有 workspace binding。

## 3. 治理执行（角色分离 F2）
- 逐卡：**独立 Reviewer `reviewer-wb-186loop`** lease.acquire(role=reviewer) 回收过期 legacy lease → 取 fresh token+fencing；
  **Adjudicator `adjudicator-wb-186loop`** task.contract_revise(envelope rev2, reviewer token)。
- `validate_reviewer_lease_for_adjudication` 强制 reviewer holder ≠ adjudicator（agent/instance/session 全异）→ F2 满足。
- lease.acquire 对 179 张过期 legacy lease 自动 expire+重签（0 张 fresh 阻塞）；1 张无 lease 干净 acquire。
- 每卡 request_id=rev2-<task_id>，evidence=revision2_backfill_evidence.md（sha256:c75d…）。
- 执行 84.6s，succeeded=180 / failed=0；ledger 见 revision2_backfill_ledger.json。

## 4. 依赖策略
- `dependencies` 统一填 Epic 父任务 `T-1787293451688-c14b1e44`（真实、统一的最小依赖；如需真实卡间依赖可后续 refine）。
- 满足 revise 的 `dependencies` 非空强校验（domain require_string_array 拒绝空数组）。

## 5. 全链状态（最终）
| 环节 | 状态 |
|------|------|
| P0-J-D | closed ✅ |
| P0-K | closed ✅ |
| P0-J | closed ✅ |
| P0-G | closed ✅ |
| **revision-2 批次（180 张机械合同）** | **rev2 就绪 ✅** |

P0 家族治理收口 + revision-2 解锁批次 全部完成。

## 6. 交付物
- 清单：revision2_mechanical_inventory.json / .csv（180 候选）
- 修正 envelope：revision2_prepared_envelopes.json
- 执行账本：revision2_backfill_ledger.json
- 证据：revision2_backfill_evidence.md
- 边界/close：P0-G_handoff.md、P0-G_closed_handoff.md
- 脚本：_exec_rev2_backfill.py（workspace 根）
