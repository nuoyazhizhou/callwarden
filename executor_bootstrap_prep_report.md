# Executor 补齐报告 — 父任务 T-1787293451688-c14b1e44

**Role:** executor
**RuntimeRole:** none（补齐治理投影，非治理裁决）
**Task:** T-1787293451688-c14b1e44 下 3 个缺失治理投影的子任务
**Skill:** none（直接数据/配置补齐）
**Allowed:** 只读核验、生成 envelope/evidence 工件、排查 bootstrap 代码门禁
**Forbidden:** 直接写 `task_contract_revisions` / `role_contract_revisions`（adjudicator-only）、修改治理代码、伪造 contract 绑定
**Handoff:** adjudicator（执行 `task.contract.bootstrap`）

---

## 1. 背景与根因

Reviewer 阶段已独立确认：CLI-02 / CLI-03 / MCP-001 三个子任务在 `verdict.submit` 时被 `E_TASK_CONTRACT_BINDING_INVALID` 拒绝。根因是**三者完全缺失 Task Contract 治理投影**：

| 投影表 | CLI-01（参照） | CLI-02 | CLI-03 | MCP-001 |
|---|---|---|---|---|
| `task_contract_revisions` | 1 | **0** | **0** | **0** |
| `role_contract_lineages` | 3 | 0 | 0 | 0 |
| `role_contract_revisions` | 3 | 0 | 0 | 0 |
| `task_step_role_contract_bindings` | 0 | 0 | 0 | 0 |
| legacy `role_contracts`（已有） | 4 | 4 | 4 | 4 |
| `task_workspace_bindings` | 1 | 1 | 1 | 1 |

`task_contract_revisions` 是 **`verdict.submit` 与 `report_handoff`（父任务关闭）共用** 的唯一前置（见 `verdict_evidence_gate.rs:539` 与 `report_handoff.rs:558`）。缺它，子任务无法提交 verdict，父任务也无法关闭。

## 2. 已完成的 Executor 工作（补齐准备）

为 3 个任务各生成了 **Task Envelope** 与 **Evidence** 工件，供 adjudicator 执行 bootstrap：

- `bootstrap_prep/envelope_CLI-02_T-1787321708568-d292ab3c.json`
- `bootstrap_prep/envelope_CLI-03_T-1787321708639-d6d362f4.json`
- `bootstrap_prep/envelope_MCP-001_T-1787321708699-da5d8224.json`
- `bootstrap_prep/evidence_{CLI-02,CLI-03,MCP-001}_*.json`（含 reviewer 结论 pass、audit 1000/0、workspace_instance_id=ws-1）
- `bootstrap_prep/bootstrap_manifest.json`（汇总，含 evidence_hash）

Envelope 结构对齐 CLI-01 已存的 `task_contract_revisions.envelope_payload`（objective / interfaces / allowed_edit_scope / acceptance_clauses / risks / rollback / dependencies），`contract_id=C-<task_id>`、`revision=1`、`profile=code_change`、`identity_policy=legacy_identity_v1`，`allowed_edit_scope.files` 取自各任务当前 executor（rev2）的 `allowed_paths`。

## 3. 关键发现：bootstrap 代码门禁缺陷（必须修复才能 bootstrap）

`task_contract_bootstrap.rs:197-210` 要求任务存在 **至少 1 个 `pending`/`in_progress` step**，否则返回 `E_TASK_CONTRACT_BOOTSTRAP_INVALID`。但：

- CLI-01（已成功 bootstrap 且 verdict 已提交）的 4 个 step **全部 `done`**，其 `task_step_role_contract_bindings=0`；
- CLI-02/03/MCP-001 的 4 个 step 也**全部 `done`**。

即：当前 bootstrap 代码会**拒绝所有已完成的任务**，与 CLI-01 的实际成功路径矛盾（CLI-01 的 bootstrap 早于该门禁）。这是一个**代码缺陷**，不是数据问题。

**修复建议（交 planner/adjudicator 决策）：** 将 step 选择条件放宽为「`pending`/`in_progress`/`done` 全部纳入」，或当无 pending/in_progress 时跳过 step binding 生成（CLI-01 即如此，其 verdict 经 MCP 薄壳路径以 `step_id=''` 提交，未触发 step-binding ABA 校验）。

## 4. 为什么不直接插入行 / 不直接调用 bootstrap

- `task.contract.bootstrap` handler（`task_collab.rs:3181`）硬性要求 `asserted_role == "adjudicator"` + 有效 reviewer-adjudication lease + `evidence_path/evidence_hash` + workspace authority capture 匹配。**Executor 不在允许角色内**，直接调用会被 `E_TASK_CONTRACT_BOOTSTRAP_ROLE_REQUIRED` 拒绝。
- 直接 SQL 插入 `task_contract_revisions` 绕过 adjudicator 门禁，违反 fail-closed 治理原则（AGENTS.md §状态推进和交接）。

因此补齐动作必须由 **adjudicator** 在修复上述门禁缺陷后执行。

## 5. Handoff

```
Handoff:
  from_role: executor
  outcome: executor_blocked_to_user
  next_role: adjudicator
  next_action: >
    1) 修复 task_contract_bootstrap.rs:202 的 pending-step 门禁（允许 done-step 任务）；
    2) 以 adjudicator 身份、持 reviewer-adjudication lease，对 3 个任务分别调用
       task.contract.bootstrap（envelope/evidence 见 bootstrap_prep/）；
    3) bootstrap 完成后，3 个任务的 reviewer 可取 lease 重新 submit_verdict。
  reason: 缺 task_contract_revisions 治理投影（数据缺失）+ bootstrap 代码 pending-step 门禁缺陷（代码缺陷），二者均超 Executor 权限/范围
  independence_requirement: not_required
```

---

**附：本次未改动任何代码或数据库。** 仅生成 `bootstrap_prep/` 工件（只读派生），所有治理写操作等待 adjudicator 在修复门禁后执行。
