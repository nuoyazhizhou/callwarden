# Adjudicator 裁决报告 — CLI-02 / CLI-03 / MCP-001 治理投影缺失

- **Role**: adjudicator（`architecture` 专家视角，独立核验）
- **Task**: 父任务 `T-1787293451688-c14b1e44` 子任务
  - CLI-02 `T-1787321708568-d292ab3c`
  - CLI-03 `T-1787321708639-d6d362f4`
  - MCP-001 `T-1787321708699-da5d8224`
- **Skill**: none（纯裁决 + 设计决策，不改代码/证据）
- **Independence**: 本裁决独立于 executor 工件，基于源码与权威库（`~/.callwarden/callwarden.db`，314MB）独立核验

---

## 1. 独立核验结论（逐项确认 executor 主张）

| # | executor 主张 | 核验方式 | 结论 |
|---|---|---|---|
| R1 | 3 任务 `task_contract_revisions` 等投影**完全缺失** | 直查 DB | ✅ **确认**：3 任务 `tcr=0 / rcl=0 / bindings=0`，但 `legacy role_contracts`（executor/reviewer/adjudicator 各 1，`is_current`）齐全 |
| R2 | 该表是 `verdict.submit` 与 `report_handoff` 的**共用前置** | 读源码 | ✅ **确认**：`verdict_evidence_gate.rs:539` 与 `report_handoff.rs:558` 均按 `(contract_id,revision,hash,task_id)` 查 `task_contract_revisions`，缺失即 `contract_stale` 拒绝 |
| B1 | 数据补齐是 **adjudicator-only 写**（executor 越权被拒） | 读 `task_collab.rs:handle_task_contract_bootstrap` | ✅ **确认**：handler 硬性要求 `role=adjudicator`（:3229-3231）、`reviewer-adjudication` lease（:3295）、`evidence_path+evidence_hash`（:3233-3240）、workspace authority capture 匹配（:3272-3281）。executor 直接调用必拒；直插 SQL 违反 fail-closed |
| B2 | `task_contract_bootstrap.rs:202` 门禁拒绝全 `done` 任务，与 CLI-01 实际路径矛盾 | 读源码 + 查 CLI-01 `T-1787321020926-b7ed7500` | ✅ **确认 BUG**：CLI-01 实际 `tcr=1（bootstrap 成功）、bindings=0、steps={done:4}` —— 即 CLI-01 正是**在无 pending step 且零 step binding** 的情况下完成 bootstrap 的，当前 :202 门禁会拒绝这条已验证可行的路径 |

**补充（executor 未覆盖、裁决关键修正）**：executor 的两选一修复建议中，**选项 #2「无 pending 时跳过 step binding（CLI-01 即如此）」不充分**。
- 当前 `verdict.submit`（`verdict_evidence_gate.rs:467-475`）要求 step 存在 **verified current binding**，否则 `read_current_binding`（`claim.rs:144`，当绑定数=0 时返回 `None`）直接拒绝。
- CLI-01 的 `task_verdict_events` 行 `step_id` 与 `role_contract_*` **均为空** —— 说明 CLI-01 的 verdict 是在**更早/更宽松**的代码路径下提交的，当前严格 gate 并不会接受「无 binding / 空 step_id」的 verdict。
- 因此若仅「跳过 binding」，bootstrap 虽能过，但 `verdict.submit` 仍被 step-binding 校验挡住，**任务并未真正解阻塞**。

→ **正确修复 = executor 选项 #1：把 step binding 放宽到覆盖全部 step（含 `done`）**，使每个 done step 都得到 executor Role Contract binding，`verdict.submit` 的 `read_current_binding` 才能通过。

---

## 2. ADR-001：放宽 bootstrap 的 step-binding 状态门禁

```markdown
# ADR-001: 放宽 bootstrap 对 step 状态的要求（覆盖已完结任务）

## Status
Accepted（作为设计决策；实现路由给 executor fix_defect，需 Reviewer PASS）

## Context
task_contract_bootstrap.rs:202 仅对 pending/in_progress step 建 executor binding，
且无 pending step 时直接拒绝。这与 CLI-01 已验证成功的路径矛盾；且当前
verdict.submit 要求 step 有 verified current binding，仅“跳过 binding”无法解阻塞。
3 个目标任务的 step 全部为 done，必须建 binding 才能提交 verdict、关闭父任务。

## Decision
将 step 查询状态过滤从 ('pending','in_progress') 放宽为包含终态
('pending','in_progress','done','applied','closed')；保留“零 step”硬拒绝（语义不变）。
即：已完成任务的每个 step 都会被绑定 executor Role Contract（历史上这些 step 正是在
该 legacy executor role contract 下完成的，语义准确）。

## Consequences
+ bootstrap 对已完成任务可行（与 CLI-01 对齐）
+ 每个 done step 获得 verified current binding，verdict.submit / report_handoff 的
  step-binding 校验通过 → 任务真正可解阻塞
- 轻微语义张力：executor Role Contract 被绑到 done step（历史准确，可接受）
- 不改变 fail-closed：no_governance_projection、identity/lease/evidence/workspace
  全部校验仍强制；仅放宽“哪些 step 建 binding”
```

### 精确 diff（`rust_ext/src/daemon/task_loop/task_contract_bootstrap.rs`）

```rust
    // 已完结/终态任务的所有 step 也需要 executor Role Contract binding，
    // 否则 verdict.submit / report_handoff 的 step current-binding 校验会拒绝
    // （见 CLI-01 反例：旧路径 bootstrap 成功，但当前 gate 要求 verified current binding）。
    // 放宽状态过滤以覆盖全部 step。
    let mut stmt = tx.prepare(
        "SELECT id FROM task_steps WHERE task_id=?1 \
         AND status IN ('pending','in_progress','done','applied','closed') \
         ORDER BY step_index",
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("task step 查询失败: {e}")))?;
    let steps: Vec<String> = stmt.query_map([&input.task_id], |r| r.get(0))
        .map_err(|e| DaemonRpcError::internal_error(format!("task step 遍历失败: {e}")))?
        .collect::<Result<Vec<_>,_>>().map_err(|e| DaemonRpcError::internal_error(format!("task step 读取失败: {e}")))?;
    if steps.is_empty() {
        return Err(deterministic(ERR_BOOTSTRAP_INVALID, "task 没有任何 step，不能 bootstrap executor binding"));
    }
```

> 注：现有 `task_contract_bootstrap_test.rs` 未断言「空 step 拒绝」行为（grep 无匹配），放宽不会破坏既有测试；但 executor 实现时应**补充一条全 `done` step 得到 N 条 binding 的回归测试**。

---

## 3. 权限边界（裁决不可逾越）

- **adjudicator 不得改代码**（AGENTS.md：adjudicator「不得制定整改计划、修改实现/证据」）。
  因此 ADR-001 的 **实现必须由 executor 以 `fix_defect` 形式落地**，经 Reviewer PASS 后部署。
- **bootstrap 写操作是 adjudicator-only**，且须持有 **真实已注册 adjudicator 身份 + 有效 reviewer-adjudication lease + 在线 daemon**。
  本会话（对话 agent）**不持有**已注册 adjudicator 身份/lease，也无法代表真实 adjudicator actor 对生产库执行写操作 —— **因此本裁决不在此会话内执行 bootstrap 写**，必须由具备身份的 real adjudicator 在修复部署后执行。
- **禁止直插 SQL**（fail-closed），executor 不插行是正确的。

### 修复后 bootstrap 预检（已核验，3 任务均满足）
- `workspace_instance_id = ws-1`，与 evidence 一致 → 不会触发 `E_WORKSPACE_AUTHORITY_MISMATCH`
- `is_current` legacy `role_contracts`：executor/reviewer/adjudicator 各 1 → bootstrap 可读取
- 各 4 个 `done` step → 修复后各生成 4 条 binding
- 3 个 envelope 均合法：`contract_id` 匹配 task、`revision=1`、`profile=code_change`、`identity_policy=legacy_identity_v1`

---

## 4. 路由与下一步（顺序）

1. **Executor（fix_defect）**：在父任务下开 `fix_defect` step，冻结 scope = 仅 `task_contract_bootstrap.rs` 上述改动 + 回归测试；实现 ADR-001；跑 `cargo test` 对应模块。
2. **Reviewer**：对 fix_defect 出 `PASS`（只读核验，不改代码）。
3. **Real Adjudicator（持身份+lease）**：修复部署、daemon 在线后，对 3 任务分别调用
   `task.contract.bootstrap`（用 `bootstrap_prep/` 下 envelope + evidence，传 `workspace_instance_id=ws-1`、evidence_path/hash）。
4. **Reviewer**：重新 `verdict.submit`（此时 `task_contract` 三元组存在且 step binding 存在，`read_current_binding` 通过）。
5. **Adjudicator**：核验所有子任务 `closed` 后关闭父任务 `T-1787293451688-c14b1e44`。

---

## 5. Handoff

```text
Handoff:
  from_role: adjudicator
  outcome: adjudicator_returned
  next_role: executor
  next_action: 以 fix_defect 实现 ADR-001（放宽 bootstrap step-binding 状态过滤为含 done/applied/closed；保留零 step 拒绝）；补回归测试；交 Reviewer PASS
  reason: 根因（缺 task_contract_revisions 投影）与门禁缺陷均确认；但 (a) 代码修复超 adjudicator 权限须回 executor，(b) bootstrap 写须由持身份+lease 的 real adjudicator 在修复部署后执行，(c) 修正 executor 选项#2 不充分——必须建 step binding 才能过 verdict.submit
  independence_requirement: not_required
```

---

## 6. 范围扩展：MCP-002 / MCP-003（+ P0-G）纳入同一治理缺口

**独立复核（递归扫描父任务 190 个 descendants）**：缺失 `task_contract_revisions` 的任务共 **6 个**，

| 任务 | task_id | status | steps | ws | 入裁决范围 |
|---|---|---|---|---|---|
| CLI-02 | T-1787321708568-d292ab3c | review | done×4 | ws-1 | ✅ 主范围 |
| CLI-03 | T-1787321708639-d6d362f4 | review | done×4 | ws-1 | ✅ 主范围 |
| MCP-001 | T-1787321708699-da5d8224 | review | done×4 | ws-1 | ✅ 主范围 |
| **MCP-002** | T-1787321708760-de068a9c | review | done×4 | ws-1 | ✅ 主范围（本轮新增） |
| **MCP-003** | T-1787321708856-e3c10624 | review | done×4 | ws-1 | ✅ 主范围（本轮新增） |
| P0-G | T-1787367417246-34190890 | **open** | pending×4 | ws-1 | ⚠️ 另计（见下） |

**结论**：CLI-02/03/MCP-001/002/003 是**同一类 A′ 批量建卡未 bootstrap 缺口**（非实现缺陷），应统一由 adjudicator 补 `task.contract.bootstrap`。5 个任务 profile 完全一致（ws-1、全 done step、legacy role_contract 齐全），**全部依赖 ADR-001 门禁放宽**才能 bootstrap。

**P0-G 单独说明**：它同样缺投影，但 `status=open` 且 step 为 `pending` —— 当前门禁（pending/in_progress 允许）**不会**阻断其 bootstrap；且它是根因/meta 任务（标题即「A′ 批量任务合同 revision-2、lease 恢复与原子治理建卡修复」）。建议：(a) 其投影可在当前代码下单独 bootstrap（无需等 ADR-001）；(b) 但它很可能本身就是承载 ADR-001 类修复的载体，故纳入「统一 bootstrap」但单独跟踪生命周期，不与 5 个 review 任务混同关闭节奏。

### 6.1 工件缺口（执行前必补）
- `bootstrap_prep/` 当前**仅有 CLI-02/03/MCP-001 的 envelope+evidence**；**MCP-002、MCP-003 尚无工件**。
- Executor 须先按 CLI-01/CLI-02 的 envelope schema（contract_id=`C-<task_id>`、`revision=1`、`profile`∈{research,design,code_change,high_risk,review}、`identity_policy=legacy_identity_v1`、含 objective/interfaces/allowed_edit_scope/acceptance_clauses/risks/rollback/dependencies）为 MCP-002、MCP-003 生成 envelope + evidence（workspace_instance_id=ws-1），再进入 bootstrap。

### 6.2 更新后的路由（覆盖 5 review 任务 + P0-G 备注）
1. **Executor（fix_defect，建议挂在 P0-G 下或独立子任务）**：实现 ADR-001；补 MCP-002/003 的 envelope+evidence 工件。
2. **Reviewer**：fix_defect PASS。
3. **Real Adjudicator（持身份+lease）**：修复部署后，对 **5 个 review 任务**调用 `task.contract.bootstrap`（用各自 envelope+evidence）；P0-G 可在当前代码下视其生命周期另行 bootstrap。
4. **Reviewer**：对 5 任务重新 `verdict.submit`（task_contract 三元组 + step binding 均在）。
5. **Adjudicator**：核验所有子任务 closed 后关闭父任务。

> 注：递归扫描显示其余 184 个 descendants 均已含 `task_contract_revisions`（review 57 + open 124 + in_progress 3），故本缺口确为 A′ 批量建卡时的局部遗漏，非全局腐烂。
