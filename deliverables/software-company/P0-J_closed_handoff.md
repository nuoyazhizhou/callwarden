# P0-J Adjudicator close 完成 — 解锁链推进至 P0-G

**任务**: `T-P0J-ROLE-WORKER-IDENTITY`
**Adjudicator 身份**: `adjudicator-wb-186loop`（注册 active，session `sess-adjudicator-wb-186loop`）
**日期**: 2026-08-25
**状态**: ✅ P0-J `closed`（closed_at=1787588637.5），解锁链推进至 P0-G。

---

## Adjudicator close 关键修正（legacy identity 路径）

P0-J identity_policy = legacy_identity_v1（Task Contract envelope 无 identity_policy 字段）。
`handle_task_close` 走 legacy 分支（`authorize_role_worker_mutation` 返回 None →
`validate_lease_for_mutation`），强制 **holder Identity == caller Identity**：

```
E_LEASE_HOLDER_MISMATCH: holder Identity 与 lease 不一致
```

即 legacy close 必须由 **reviewer lease 的持有者** 调用，且该持有者身份须匹配 `identity`。
正确模式（与 role_worker_v1 路径不同）：**Adjudicator 以自身身份 acquire 一个
`reviewer`-role 的 lease（lease.acquire 不强制 identity.role==role），再以同一
adjudicator 身份调用 task.close**。lease role=reviewer 满足 close 门禁的
"active reviewer lease" 查找，holder/actor=adjudicator 满足 holder-match。

首跑用 reviewer-wb-186loop 持 lease + adjudicator-wb-186loop 调用 → holder mismatch；
修正为 adjudicator-wb-186loop 持 lease 并调用 → 成功。

## 执行步骤

1. `lease.release` 回收早期失败尝试残留的 active reviewer lease（L-1ac6023f84a5d78a, fencing=4）
2. `lease.acquire`（role=reviewer，holder=adjudicator-wb-186loop）→ L-e499f94953424481, fencing=5, token=a76e6a50...
3. `task.close`（identity=adjudicator-wb-186loop 四元组 + lease_token/fencing）→ status=closed

## 权威 DB 核验

- P0-J `status=closed`，close event role=adjudicator，from review→closed
- task_contract_revisions / role_contracts 未改动；reviewer verdict V-9cd4646297d9b8e8e2bf3517 仍是 pass 前置
- S1 子任务 fence：P0-J children=0 → PASS；S2 步骤全 done → PASS（沿用 P0-J-D 的 S2 修复）

## 解锁链当前位置

| 任务 | 状态 |
|------|------|
| P0-J-D | closed ✅ |
| P0-K | closed ✅ |
| **P0-J** | **closed ✅** |
| P0-G (`T-1787367417246-34190890`) | applied（下一活阻塞） |
| revision-2（128 机械 rev1 合同） | 待 P0-G close 触发 |

## 下一步（P0-G 独立治理周期）

P0-G 当前 `applied`，需其自身治理收口（reviewer PASS + adjudicator close，或 applied 任务的
专用 close 路径）才能触发 revision-2 批次。需独立 Reviewer/Adjudicator 会话，按同等纪律推进。

**本 agent（Adjudicator）到此合法终点，不越权推进 P0-G。**
