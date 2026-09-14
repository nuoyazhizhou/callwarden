# 治理动作证据：task.contract_revise 追加 identity_policy=role_worker_v1

> 任务：`T-1787796259862-e7dc8c9c`（修正角色无人值守模板的 task_id 交接绑定）
> 记录时间：2026-09-08
> 批量修复 run：`5ad121a6`

---

## 1. 阻塞

- 合同 revision 1 缺少可解析 identity_policy → claim/next_action fail-closed（禁止隐式降级）。

## 2. 治理动作

- `role_worker.rotate`（owner_recovery）：轮换 adjudicator Role Worker `cw-adjudicator-p0j-v1` 获取一次性 credential（仅内存，不落盘）。
- `task.contract_revise` 追加 revision 2：声明 `identity_policy=role_worker_v1`；`supersedes_contract_hash` = revision 1 hash（`sha256:c52e208ef9f236129191308fe33d1e731cb86b8f508e1f386a37fa8a5eb0fa5d`）。
- 授权：adjudicator Role Worker credential（worker-first）+ reviewer server-side proof（lease_id + fencing_counter），不携带 raw lease token。

## 3. 不变式

- `contract_id` 不变（`TC-T-1787796259862-e7dc8c9c`）；append-only 追加，不删历史。
