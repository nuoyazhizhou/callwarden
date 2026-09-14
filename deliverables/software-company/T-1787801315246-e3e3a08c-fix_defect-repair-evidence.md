# P0-L fix_defect 整改证据：task-level reviewer_blocked 后 identity_policy 死锁解除

> 任务：`T-1787801315246-e3e3a08c`（P0-L）step6 `fix_defect`（S-72da265216fbc2ed108c0102）
> 记录时间：2026-09-07
> source：reviewer_blocked（verdict `V-cde37b1a23b090d01a3ed5e3`，finding `identity_policy_gap` block）
> 执行者：executor role worker（`cw-executor-p0j-v1`），修复授权：adjudicator role worker（`cw-adjudicator-p0j-v1`）

---

## 1. 死锁形态（claim 前实证）

P0-L 自身合同 rev1 envelope 缺少 `identity_policy` → claim 三态判为 `Unresolved` →
`E_TASK_IDENTITY_POLICY_MISMATCH`：fix_defect 步骤（role=executor）无法领取。
这是 reviewer_blocked 后 remediation 的闭环死锁：要修复存量任务 identity_policy 缺口，
但 P0-L 自己先缺 identity_policy。

## 2. 存量缺口盘点（Epic 子树 T-1787203926824-9f873bfc）

- 子树任务总数：227
- 有 contract revision 但缺 identity_policy：**183**
- 无 contract revision：**7**（Epic 根 + 2 open + 3 closed 历史 + 1 深层 closed）
- 已声明 identity_policy：37

评审阻断项聚焦 Epic 直接子任务层（2 缺 + 7 无），深层子树缺口待批修（超出本步骤范围）。

## 3. 修复动作（live daemon 事务，无 direct SQLite）

1. `role_worker.rotate`（owner_recovery）：轮换 `cw-adjudicator-p0j-v1` 获取一次性子凭证（内存中，不落盘）。
2. `task.p0l_identity_policy_repair`（frozen P0-L task，`repair_code=p0l_identity_policy_v1`）：
   - adjudicator Role Worker credential 授权（非 legacy identity / reviewer lease）
   - 对 P0-L 合同 append-only 追加 rev2，声明 `identity_policy=role_worker_v1`
   - one-shot：policy 解析后拒绝二次 repair
3. claim fix_defect（`S-72da265216fbc2ed108c0102`，executor role worker auth）。

## 4. 执行结果（repair 后回填）

<!-- repair 完成后更新 -->
