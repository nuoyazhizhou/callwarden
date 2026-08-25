# P0-G 治理收口交接表（结构化 Handoff）

> 生成时间：2026-08-24 21:34 GMT+8
> 上游链：P0-J-D(closed) → P0-K(closed) → P0-J(closed ✅) → **P0-G(applied)** → revision-2(128)
> 本文档为本 Agent 在 P0-G 边界处的合法停点；不越权推进 P0-G 的写操作。

## 1. 解锁链当前状态

| 任务 | ID | 状态 | 角色周期 | 备注 |
|------|-----|------|----------|------|
| P0-J-D | T-P0JD-... | closed | reviewer PASS + adjudicator close | 已完成 |
| P0-K | T-P0K-... | closed | reviewer PASS + adjudicator close | 已完成 |
| P0-J | T-P0J-ROLE-WORKER-IDENTITY | closed | reviewer PASS + adjudicator close | 已完成（2026-08-24） |
| **P0-G** | **T-1787367417246-34190890** | **applied** | **待定（见 §4）** | **当前活阻塞** |
| revision-2 批次 | 128 个机械 rev1 合同 | pending | — | 等待 P0-G 收口触发 |

## 2. P0-G 权威库快照（`~/.callwarden/callwarden.db`）

| 字段 | 值 |
|------|-----|
| id | T-1787367417246-34190890 |
| title | P0-G：A′ 批量任务合同 revision-2、lease 恢复与原子治理建卡修复 |
| status | **applied** |
| parent_id | T-1787293451688-c14b1e44（Epic） |
| depth | 0 |
| applied_at | 1787564909.99 |
| closed_at | None |
| identity_policy | **legacy_identity_v1**（contract envelope `ct-p0g-revise-atomic-v1` r1） |
| creator | S-1-5-21-...-1001 |

### task_steps（4 步，全部 `pending`）
| idx | action | target | status |
|-----|--------|--------|--------|
| 0 | audit_and_design | batch_task_card_and_contract_backfill_defect_audit.md; task_contract_revise.rs | pending |
| 1 | implement | task_contract_revise.rs; task_collab.rs; dispatch.* | pending |
| 2 | test | *test*.rs; tests/ | pending |
| 3 | release_verify | runtime/current; deliverables/ | pending |

### task_verdict_events
**空** —— 无任何正式 `verdict.submit` 记录。

### task_leases（reviewer 角色，按时间倒序）
| lease_id | holder agent_id | session | status | fencing | acquired | expires |
|----------|-----------------|---------|--------|---------|----------|---------|
| L-b1e56a9ca2d655c3 | adjudicator-wb-186loop | sess-adjudicator-wb-186loop | active* | 4 | 1787564935 | 1787572135(墙钟已过期) |
| L-d4f8cf4281d3fbe7 | adjudicator-wb-186loop | sess-adjudicator-wb-186loop | expired | 3 | 1787564926 | 1787572126 |
| L-f1e00e4c66151f86 | adjudicator-wb-186loop | sess-adjudicator-wb-186loop | expired | 2 | 1787564909 | 1787572109 |
| brtl-...-r1 | reviewer-wb-186loop | sess-reviewer-wb-186loop | expired | 1 | 1787563430 | 1787567030 |

> *L-b1e56a9ca2d655c3 在 DB 中 status='active'，但 `expires_at=1787572135` 远早于当前墙钟（2026-08-24），属"活性过期"lease。

### task_events 关键转场
| from | to | reason | role | ts |
|------|----|--------|------|----|
| none | open | created | — | 1787367417 |
| open | review | bootstrap_executor_ready_for_review | executor | 1787563262 |
| review | review | bootstrap_reviewer_pass | reviewer-wb-186loop | 1787563430 |
| review | review | task_contract_bootstrapped | adjudicator | 1787564847 |
| review | applied | applied | adjudicator | 1787564909 |

### 子任务（S1 子任务门禁）
P0-G 仅 1 个子任务：**P0-J（T-P0J-ROLE-WORKER-IDENTITY，status=closed）** → S1 子任务门禁 **PASS**。
（注：P0-J 的 parent_id 即 P0-G；128 个 revision-2 合同不在 P0-G 子树下，是其实现产物/解锁目标。）

## 3. `handle_task_close` 门禁分析（task_collab.rs:14453–14585，已核对权威源）

| 门禁 | 行为 | P0-G 判定 |
|------|------|-----------|
| S3 lease（14473–14490） | 必须带 reviewer-role lease（token+fencing），legacy 走 `validate_lease_for_mutation`（holder 匹配） | 需持 reviewer-role lease；现 active lease 由 adjudicator 持有（可重获取） |
| from_status 前置 | **无** `review` 限制，直接写 `closed` | `applied`→`closed` **代码允许** |
| S1 子任务（14496–14518） | 存在非 closed 子任务则 E_CHILD_TASKS_NOT_CLOSED | P0-J 已 closed → **PASS** |
| S2 叶子步骤（14519–14551） | 仅当 child_total==0 时检查步骤全 done | P0-G 有子任务 → **S2 跳过**（故 4 步 pending 不拦截 close） |
| verdict 强制 | **无**——legacy close 不强制 reviewer verdict | 代码不要求 verdict |

**结论**：就代码而言，持有 reviewer-role lease 的 adjudicator 可直接 `task.close` P0-G，`applied`→`closed` 合法，S1 通过，S2 跳过。

## 4. 治理异常（纪律问题 → 必须呈交用户决策）

1. **verdict.submit 对 applied 任务被 code 禁止**：`handle_verdict_submit` 5457–5462 行 `if task_status != "review" → E_VERDICT_TASK_NOT_IN_REVIEW`。
   因此"镜像 P0-J：独立 Reviewer verdict.submit(pass) → adjudicator close"**不可行**——P0-G 已不在 review 态。
2. **P0-G 无正式 reviewer verdict**：仅有 `bootstrap_reviewer_pass`（轻量引导门禁，非正式 verdict）。
3. **4 步骤全 pending 但 status=applied**：步骤追踪未回填为 done/skipped，与 applied 语义不一致（治理卫生缺口）。
4. **active reviewer lease 由 adjudicator 持有且墙钟过期**：独立 reviewer 视角缺失。

## 5. 待用户决策的可选路径

- **路径 A（推荐，代码合规）**：Adjudicator 直接 close。
  重取 reviewer-role lease（adjudicator 持有）→ `task.close` → `applied`→`closed`。
  依据：legacy close 不强制 verdict；S1 因 P0-J closed 通过；S2 跳过。P0-G 已有 bootstrap_reviewer_pass 轻量门禁。
- **路径 B（更严格合规）**：先把 applied 退回 review（若支持该转场），跑独立 Reviewer `verdict.submit(pass)`，再 close。
  风险：`applied`→`review` 转场未必受支持；步骤多。
- **路径 C（先核验再决策）**：先核对 P0-G 实现证据（revision-2 回填交付物 / 源码改动是否属实落地），再选 A 或 B。

> 本 Agent 在 P0-G 边界合法停点；等待用户选择后，再切对应角色执行。
