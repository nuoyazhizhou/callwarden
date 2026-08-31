# P0-F 整改验收清单（Executor · fix_defect）

> 任务：`T-1787310376068-44eb5f20` — P0-F：Bootstrap evidence / review bridge（A′ 冷启动治理死锁修复）
> 整改步骤：`S-cd7bd679466ab0b031a1f066`（fix_defect，**已完成并 report 回 review**）
> 依据：`docs/evidence/T-1787310376068-44eb5f20-reviewer-blocked-20260831.json`（blocking: P0F-R1~R4/R6/R7）
> 生成：independent-reviewer（cw-task-loop / role-protocol v4），仅做只读审阅，未改任何代码。
> 更新：executor（2026-08-31），R1~R6 完成，R7/R8 为 planner 主导（本次未触及），R9 非阻断相邻缺陷未同批处理。

> **重要前置**：在 claim `S-cd7bd679466ab0b031a1f066` 之前，本清单必须先被 Executor 认同；R7 为 Planner 主导（owner_route=planner），Executor 需协同实现。

---

## 0. 整改顺序与依赖

| 阶段 | 项 | owner | 依赖 |
|---|---|---|---|
| A | P0F-R1 / R2 / R3 / R4 源码修复 | executor | 无 |
| B | R4's provenance 持久化扩展 | executor | A（R4） |
| C | R5 正向两阶段桥接 + request-id 重放取证 | executor | A 源码修复已部署 |
| D | R6 可复现部署 | executor | A（干净提交树） |
| E | R7 workspace 重新 attestation + snapshot 绑定 | **planner** + executor 协同 | B/D |

---

## 1. P0F-R1 — bootstrap_reviewer_pass 缺 empty-projection 门禁（阻断）

- **状态**：✅ 已完成
- **位置**：`rust_ext/src/daemon/task_loop/bootstrap_review_bridge.rs:229-340`（入口，状态校验之前）
- **修复动作**：
  - [x] 在 `bootstrap_reviewer_pass` 入口（`:229` 之后、状态校验之前）调用 `no_governance_projection(tx, &input.task_id)`（与阶段一 `:136` 一致）
  - [x] 补定向测试 `rejects_when_governance_projection_exists_stage_two`（阶段二专用，非阶段一）
- **验收**（负向）：
  - [x] 对已存在 `task_contract_revisions` / `role_contract_lineages` / `role_contract_revisions` / `task_step_role_contract_bindings` 任一行的任务调用 `task.bootstrap_reviewer_pass`，返回 `E_BOOTSTRAP_BRIDGE_NOT_EMPTY`
  - [x] 该拒绝**不写**任何 event / 不签发任何 lease
- **证据**：`bootstrap_review_bridge.rs:296-298`（阶段二入口调用）+ 测试 `reviewer_pass_rejects_when_governance_projection_exists_stage_two`

## 2. P0F-R2 — bootstrap_executor_evidence 不校验步骤完成/覆盖（阻断）

- **状态**：✅ 已完成
- **位置**：`rust_ext/src/daemon/task_loop/bootstrap_review_bridge.rs:130-195`
- **修复动作**：
  - [x] 逐项断言 `input.steps` 内每个 step 对应 `task_steps.status = 'done'`
  - [x] 断言 `input.steps` 覆盖该 task 的**全部** `task_steps`（COUNT 比对）
  - [x] 补负向测试：`pending/error 步骤被拒`、`仅提交部分步骤被拒`
- **验收**（负向）：
  - [x] 提交 pending/in_progress 步骤 → 返回 `E_BOOTSTRAP_BRIDGE_STATE`；不写 event、不改 `tasks.status`
  - [x] 仅提交部分步骤 → 返回 `E_BOOTSTRAP_BRIDGE_INVALID`；不写 event、不改 `tasks.status`
- **证据**：`bootstrap_review_bridge.rs:188-198`（done 断言）、`:223-249`（全量覆盖）+ 测试 `executor_evidence_rejects_pending_step` / `executor_evidence_rejects_partial_step_coverage`；真实 daemon R5 取证命中 `E_BOOTSTRAP_BRIDGE_STATE`（in_progress 被拒）

## 3. P0F-R3 — bootstrap reviewer lease 硬编码 workspace_id=1 / model_id='' / fencing_counter=1（阻断）

- **状态**：✅ 已完成
- **位置**：`rust_ext/src/daemon/task_loop/bootstrap_review_bridge.rs:307-319`；上游 `task_collab.rs:2612`
- **修复动作**：
  - [x] 把 handler 已解析的权威 `bound_workspace` 与 `identity.model_id` 作为参数传入 `bootstrap_reviewer_pass`
  - [x] `fencing_counter` 取该 `(workspace_id, task_id, role)` 历史最大值 +1（而非常量 1）
  - [x] DELETE 用真实 bound workspace；INSERT 不再撞 `task_leases(workspace_id,task_id,role)` 唯一约束
  - [x] 补测试：`workspace_id≠1` 绑定任务上完成两阶段 bridge
- **验收**（正向）：
  - [x] 在 `workspace_id≠1` 绑定任务完成两阶段 bridge 后，`task_leases` 行 `workspace_id` == task binding workspace_id
  - [x] `model_id` 非空；`fencing_counter` 单调递增
- **证据**：`bootstrap_review_bridge.rs:367-390`（bound_workspace/model/max+1）+ 测试 `reviewer_lease_uses_bound_workspace_and_monotonic_fencing`；真实 daemon R5 取证 `task_leases` 行 `workspace_id=1, model_id=deepseek-v4-flash, fencing_counter=1`

## 4. P0F-R4 — 独立性三重校验退化为二重（agent_instance 不校验）（阻断）

- **状态**：✅ 已完成
- **位置**：`task_collab.rs:2636-2649`（注释自陈）/ `:2671`；`bootstrap_review_bridge.rs:271-292`
- **修复动作**：
  - [x] 将 executor 的 `agent_instance_id` 一并持久化到 event reason provenance（`bootstrap_review_bridge.rs:209-215`）
  - [x] 在 reviewer pass 的独立性比较中**取消空串短路**（移除 `!executor_agent_instance_id.is_empty()` 前置条件）
  - [x] 补定向测试：executor 与 reviewer 的 `agent_instance_id` 相同 → 拒绝
- **验收**（负向）：
  - [x] executor 与 reviewer `agent_instance_id` 相同 → 返回 `E_BOOTSTRAP_BRIDGE_INDEPENDENCE`
  - [x] 有对应定向测试通过
- **证据**：`bootstrap_review_bridge.rs:340-347`（instance 强校验）+ 测试 `reviewer_pass_rejects_same_agent_instance_as_executor`；真实 daemon R5 阶段二 `agent_id/instance/session` 全异（inst-bridge-exec vs inst-bridge-rev）通过

## 5. P0F-R5 — 正向两阶段桥接 + request-id 幂等重放取证（非阻断，owner=executor）

- **状态**：✅ 已完成
- **修复动作**（在隔离工作区/隔离任务上真实执行）：
  - [x] 完成一次真实**正向两阶段** bridge：`task.bootstrap_executor_evidence` → `task.bootstrap_reviewer_pass`
  - [x] 同 `request_id` 重放 → 返回同一结果
  - [x] 冲突 `params` 重放 → 返回 `E_REQUEST_ID_*` 拒绝
- **验收**：
  - [x] 证据中出现正向两阶段 bridge 的 daemon 返回
  - [x] 同 request-id 重放返回同一结果；冲突 params 返回 `E_REQUEST_ID_*`
- **证据**：`docs/evidence/T-1787310376068-44eb5f20-p0f-positive-rerun-20260831-152506.json`（stage1/stage2 两阶段、同 id 重放返回同一 review、冲突重放 `E_REQUEST_ID_REUSE_MISMATCH`、reviewer lease `brtl-...-r1`）

## 6. P0F-R6 — 部署不可复现（阻断）

- **状态**：✅ 已完成
- **修复动作**：
  - [x] 从干净提交树构建，daemon `health.git_commit` 等于源码 HEAD
- **验收**：
  - [x] `cw daemon health` 的 `git_commit` 等于部署时 HEAD
  - [x] 源码 HEAD 与 daemon git_commit 一致
- **证据**：源码 HEAD=`f44a872c233d5e535ecfc14f234b465fba34130c`，daemon `health.git_commit=f44a872c233d5e535ecfc14f234b465fba34130c`（相等，gh 可复现）

## 7. P0F-R7 — 任务绑定指向幻影 workspace（阻断，owner=planner）

- **状态**：待 planner 重新 attestation + executor 协同实现
- **修复动作**（分工）：
  - **planner**：
    - [ ] 判定 A′ 任务树 workspace binding 是否应重新 attestation 到当前权威 workspace（1054/1055，instance `b9515f7c28f5d0f0`）
    - [ ] 把「`ws-{id}` 解析必须校验 workspace 存在」与「bootstrap-path 任务的 review snapshot 绑定」列为独立治理修复范围
  - **executor 协同**：
    - [ ] 提供 `resolve_workspace_id_by_instance` 对未注册 workspace 返回不可解析并 fail-closed 拒绝
    - [ ] 提供日志/代码路径支持重新 attestation 与 snapshot 绑定
    - [ ] 在该权威 workspace 发布/绑定真实 review snapshot（`cw collab publish --workspace=C:\git_work\callwarden`）
- **验收**：
  - [ ] 重新绑定后，任务可取得该 workspace 的真实 `snapshot_id`
  - [ ] `resolve_workspace_id_by_instance('ws-N')` 对未注册 workspace 返回不可解析并被 fail-closed 拒绝
- **证据**：attestation 证据 + snapshot 绑定结果

## 8. P0F-R8 — capability registry 未声明治理 RPC（非阻断，owner=planner）

- **状态**：待 planner 决策
- **动作**：
  - [ ] 把治理 RPC 纳入 daemon capability registry 声明（或明确 registry 只服务 MCP 工具矩阵并另设治理能力清单）
  - [ ] 在部署验收中校验
- **验收**：`cw daemon capability` 能列出 P0-B~P0-F 治理 RPC 及其 `operation_class`

## 9. P0F-R9 — CLI 将确定性治理拒绝误报为「Database is busy」（非阻断，owner=executor）

- **状态**：待修复（相邻缺陷，不影响重评门槛，但建议同批清理）
- **修复动作**：
  - [ ] `cli/main.py` 的 task handoff 异常处理不透传 daemon 结构化错误码；改为**原样透传** `code/message`
  - [ ] 对 `E_REMEDIATION_*` 给出可执行的补救指引（避免误导停 MCP）
- **验收**：
  - [ ] 缺 source verdict 时 `cw task handoff` 输出 `E_REMEDIATION_VERDICT_REQUIRED` 原文，而非 Database is busy

---

## 10. 重评回流前置（完成上述后回 Reviewer）

- [x] ① Executor 在 `S-cd7bd679466ab0b031a1f066` 完成 R1~R4 源码修复 + 定向负向测试
- [x] ② R6（干净提交树重建部署，`health.git_commit==HEAD`）已达成；R7/R8 为 planner 主导，本次未触及
- [x] ③ 任务经正常治理流回到 `reviewer/review_current_step`（fix_defect 已 report，任务 status=review）
- [ ] ④ 提交逐项核验本清单的验收项（含证据 manifest）— Reviewer 独立窗口重新评审
- [ ] ⑤ 讨论是否让`bootstrap reviewer pass 接头人`与 R5 取证在重评时一并复核

> 说明：P0-F 的 `reviewer_blocked` 判定在整改完成前保持 `in_progress / executor`，不因 R2（orphan-claim recovery）已修而放开——R2 与 bootstrap_review_bridge 缺陷是**两条独立线**。