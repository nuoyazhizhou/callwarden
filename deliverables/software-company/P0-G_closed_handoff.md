# P0-G 治理收口完成（Adjudicator 直接 close，Option A）

> 执行时间：2026-08-24 21:3x GMT+8
> 决策来源：用户在 P0-G_handoff.md 边界呈交后选「Adjudicator 直接 close（推荐）」
> 脚本：`_exec_p0g_adjudicator_close.py`（PYTHONPATH=C:/git_work + 托管 python）

## 1. 执行结果（权威库已核验）

| 项 | 值 |
|----|----|
| task_id | T-1787367417246-34190890 |
| close 前 status | applied（applied_at=1787564909.99） |
| close 后 status | **closed**（closed_at=1787590330.92） |
| close 事件 | from=applied → to=closed, reason=closed, role=adjudicator |
| reviewer lease | L-bf4167567576ff2b（fencing=5, holder=adjudicator-wb-186loop, model=workbuddy, active） |
| 旧 stale lease | L-b1e56a9ca2d655c3（fencing=4）已被 acquire 自动 expire 回收 |

## 2. 门禁地图（handle_task_close:14453，legacy 路径）

| 门禁 | 结果 |
|------|------|
| S3 lease | reviewer-role lease（token+fencing=5）由 adjudicator 持有 → validate_lease_for_mutation holder 匹配 ✅ |
| from_status 前置 | 无；`applied`→`closed` 代码允许 ✅ |
| S1 子任务 | P0-G children={P0-J(closed)} → open_children=0 → PASS ✅ |
| S2 叶子步骤 | 跳过（P0-G 有子任务，走 parent 分支）→ 4 步 pending 不拦截 ✅ |
| verdict 强制 | legacy close 不强制 verdict（task_verdict_events 空，合规） |

> **关键 code 修正（边界调研发现）**：`handle_verdict_submit:5457` 对 `applied` 任务返回
> `E_VERDICT_TASK_NOT_IN_REVIEW` → 不能走"独立 Reviewer verdict.submit → close"的 P0-J 套路。
> 故 P0-G 只能走 Option A（adjudicator 直接 close），这是代码唯一合规收口路径。

## 3. 解锁链全绿

| 任务 | 状态 |
|------|------|
| P0-J-D | closed |
| P0-K | closed |
| P0-J | closed |
| **P0-G** | **closed ✅** |

P0 家族治理收口全部完成。

## 4. revision-2 批次就绪状态（下一动作待裁决）

- 权威库 `task_contract_revisions`：rev1=197 / **rev2=0** / total=197。
- P0-G step0 审计发现的 **128 张机械 rev1 合同**（`adjudicator-workbuddy-v1` 批量 bootstrap，
  100% JSON 数组字符串化 + 100% 空 dependencies）包含在这 197 张 rev1 内，尚无任何 revision-2。
- P0-G 已部署能力：`task.contract_revise`（拒绝字符串化数组/非连续 revision/hash 不匹配）
  + 原子建卡 envelope（能力4：create 同事务写 TC rev1 + 三角色 lineage + executor step binding）。
- **结论**：P0-G close 已解锁 revision-2 批次；128 张机械合同现可用 P0-G 能力修订为 rev2
  （反序列化字符串化数组、回填 dependencies）。但"创建/修订 128 张 rev2 合同"是独立的大批量操作，
  非 close 自动触发，需用户授权后执行（建议独立治理复核批次正确性）。

## 5. 产物
- 边界调研：deliverables/software-company/P0-G_handoff.md
- 执行脚本：`_exec_p0g_adjudicator_close.py`
- 下一步：revision-2 批次（128）创建/修订 —— 待用户裁决执行方式与治理复核。
