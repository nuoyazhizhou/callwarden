# 治理动作证据：task.contract_revise 追加 identity_policy=role_worker_v1

> 任务：`T-1787799894830-3cd93b18`（补齐 daemon governance_projection 权威状态投影）
> 记录时间：2026-09-08
> 批量修复 run：`5ad121a6`

---

## 1. 阻塞

- 合同 revision 1 缺少可解析 identity_policy → claim/next_action fail-closed（禁止隐式降级）。

## 2. 治理动作

- `role_worker.rotate`（owner_recovery）：轮换 adjudicator Role Worker `cw-adjudicator-p0j-v1` 获取一次性 credential（仅内存，不落盘）。
- `task.contract_revise` 追加 revision 2：声明 `identity_policy=role_worker_v1`；`supersedes_contract_hash` = revision 1 hash（`sha256:7df572530851929552a6053eddef905480588139319cfdf97a179a31a12e1f57`）。
- 授权：adjudicator Role Worker credential（worker-first）+ reviewer server-side proof（lease_id + fencing_counter），不携带 raw lease token。

## 3. 不变式

- `contract_id` 不变（`TC-T-1787799894830-3cd93b18`）；append-only 追加，不删历史。
