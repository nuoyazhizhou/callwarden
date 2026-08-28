# T-1787888909289-881595e0 角色治理修订 v3 — 复审阻断项修复证据（v4）

**任务：** T-1787888909289-881595e0
**证据版本：** v4（append-only；v3 保持原样，本文追加第二轮复审阻断项的更正与修复记录）
**日期：** 2026-08-28
**触发：** 第二次技术复审 BLOCKED（P1-2 `adjudicator_returned`、P1-5 snapshot + 三项 P2）

## 1. 第二轮阻断项修复对照

### P1-2 `adjudicator_returned` 自动整改与源码不符（已修复）

v3 之前 role-protocol §1/§3/§4 仍残留「Reviewer/Adjudicator 退回都会自动追加 `fix_defect`」
的表述，与 `task_collab_lifecycle.rs` 不一致。本轮已逐字对齐源码：

- 自动 remediation 分支**仅匹配 `outcome == "reviewer_blocked"`**（`task_collab_lifecycle.rs`）。
- `adjudicator_returned` 只有固定路由映射（`expected_route` → `(adjudicator, executor, not_required)`），
  **不自动追加 step、不自动 reopen**。
- 已同步文件：role-protocol §1 顶部 capability 分层、§3 双轨整改图、§4 BLOCKED 段落；
  `AGENTS.md` 职责矩阵第 5 条；`Callwarden 无人值守循环启动模板：Adjudicator v4.md` 第 5 条；
  `docs/design/cw-role-handoff-task-loop-v2-amendment.md` §3 双轨图。
- 关键后果写明：Executor（持 implementer lease）须显式 `task.remediation.create`
  （`source_outcome=adjudicator_returned`）补建并 reopen；未补建时 `next_action` 仍按 Reviewer
  PASS 返回 `READY/ADJUDICATE` 形成循环。

### P1-5 review snapshot 陈述更正（v3 陈述撤回）

v3 evidence §1「P1-5 正式 Review 输入」曾称「正式 review snapshot（`review_input_snapshot`/
`evidence_gate`）由 daemon 在 Reviewer 领取 review assignment 时评估生成」——**此陈述与源码不符，
现撤回**。事实：

- `task_collab_contract.rs`（governance projection）只从已有 `task_events.snapshot_id` 读取最近
  一条带 snapshot 的事件，缺省返回 `{"diagnosis": "no_snapshot"}`；**不在 Reviewer claim/lease
  时生成任何 snapshot**。
- `snapshot_id` 仅在 workspace 注册时计算（`workspace.rs` `register_workspace`，由 git remote URL +
  head commit SHA + 指纹），与 review assignment 领取无耦合；证据侧 `workspace_snapshot_id` 与
  verdict 输入的 `snapshot_id` 均由提交方显式提供，非 claim 时自动生成。
- 本轮可复核审查输入 = 含 task_id 的 git commit + 本 evidence + 台账完整 SHA entry；正式 review
  snapshot 的自动生成列为独立 backlog（需 daemon capability/扩展），**不在本轮文档 scope 内伪造闭合**。

### P2-1 Planner outcome 权威路由三元组（已补齐）

role-protocol §5 新增「outcome 权威路由三元组」表（`from_role → next_role →
independence_requirement`，唯一单源）：已实现六种逐字对齐 daemon `expected_route`
（`task_collab_lifecycle.rs`），design-only 三种为 `planner_governance_v1` 上线后的目标路由。
并明确 `planner_replan_required` 固定 `planner → executor → required`（Planner 修订冻结新
revision 后重新交 Executor），不得交 User、不得跳过 Executor 直达 complete。

### P2-2 校验器精确分层断言（已补齐 + 负向用例）

`scripts/validate_template_compliance.py`：

- 新增 `REQUIRED_IMPLEMENTED_STATUSES`（= 全集 − 保留集精确差）与精确保留集
  `REQUIRED_RESERVED_STATUSES`；
- `check_protocol_internal` 新增对称差断言 `E_PROTO_IMPLEMENTED_FIXED`/`E_PROTO_RESERVED_FIXED`
  （取代旧单向「保留值不得混入已实现」检查），把 `queued` 等单项从已实现层搬到保留层必报错；
- 负向 self-test 用例 `proto_status_layer_fixed`（搬移 `governance_blocked` 到保留层，分区仍完整）
  必报 `E_PROTO_IMPLEMENTED_FIXED`。

### P2-3 台账完整 SHA（已补全）

`cw_task_commit_ledger.json`：

- `t1787888909289_review_blocker_fix_20260828.commit_id`：`a686f18` →
  `a686f1854a1cbbf45dd283cf99c35b028f2b3f6c`；
- `t1787888909289_provenance_correction_20260828.corrects_commit_ids`：
  `5452bdc` → `5452bdc0ed9d912999d071952c25b086a622b1e8`、
  `e70b0b7` → `e70b0b7a4355a5a421b8f6fdbde4dbeb74b91c3e`、
  `7c4edda` → `7c4edda3c0d1e05fe8aca15f0ba3b68b77fc0f94`。

## 2. 验证输出

```text
& C:\Python314\python.exe scripts/validate_template_compliance.py
  结构化合规检查通过：协议单源、4 个角色模板、Skill/user-guide、设计 supersede 与归档 blob 均一致

& C:\Python314\python.exe scripts/validate_template_compliance.py --self-test
  PASS proto_status_layer_fixed -> E_PROTO_IMPLEMENTED_FIXED
  （含既有 17 用例共 18 项）
  self-test: 18 通过, 0 失败（共 18 用例）

& git diff --check   # 工作区
  （零输出，无空白错误）

& C:\Python314\python.exe -c "import json; json.load(open('cw_task_commit_ledger.json', encoding='utf-8'))"
  JSON OK
```

## 3. 本轮提交范围（白名单）

```text
.agents/skills/cw-task-loop/references/role-protocol.md
AGENTS.md
Callwarden 无人值守循环启动模板：Adjudicator v4.md
docs/design/cw-role-handoff-task-loop-v2-amendment.md
scripts/validate_template_compliance.py
cw_task_commit_ledger.json
deliverables/software-company/T-1787888909289-881595e0-role-protocol-correction-evidence-v4.md
```

工作区中 `rust_ext/src/daemon/*.rs`、`server/*.py`、`tests/*.py`、`scripts/refresh_shared_runtime.ps1`
及 `deliverables/software-company/p0l_*` 的并行修改（P0-L transport / 其他任务）**不属于本任务 scope**，
未纳入本提交。

## 4. daemon 状态说明

任务 5/5 步骤 done，投影 `review_pending`（READY/REVIEW，reviewer assignment queued）。本轮修复
以增量 commit + evidence v4 + 台账完整 SHA entry 提交，供正式 Reviewer 领取后对最新 HEAD 复核。
