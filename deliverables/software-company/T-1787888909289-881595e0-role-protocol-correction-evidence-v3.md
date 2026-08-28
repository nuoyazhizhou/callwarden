# T-1787888909289-881595e0 角色治理修订 v2 — 复审阻断项修复证据（v3）

**任务：** T-1787888909289-881595e0
**证据版本：** v3（append-only；v2 保持原样，本文仅追加更正与修复记录）
**日期：** 2026-08-28
**触发：** 技术复审 BLOCKED（5 个 P1 阻断 + 6 项其他问题）

## 1. 复审阻断项修复对照

### P1-1 冻结 v1 恢复原始 blob

- `docs/design/cw-role-handoff-task-loop.md` 已通过 `git cat-file blob 34668462` 恢复字节级原始内容；
  工作区 `git hash-object` 复核 = `34668462a8c135e106d32fea869b66cb8eec8a56`（与冻结前一致，
  `7c4edda` 写入的标题/顶部声明/§1 四角色改写已全部撤销）。
- supersede 指针唯一存在于 v2 amendment（`cw-role-handoff-task-loop-v2-amendment.md` 记录
  v1 原始 blob id）；v1 不再包含任何 supersede/role-protocol 指针。
- 校验器不再要求 v1 包含 supersede 指针，改为核验 v1 固定 blob：
  `E_DESIGN_V1_BLOB_MISMATCH`（期望 `34668462…`，任何改写含追加指针均报错）；
  负向用例 `design_v1_blob_mismatch` 已加入 self-test。

### P1-2 pre-cutover 双轨路由改为如实桥接

文档不再描述「计划缺陷直接升级 user」这条 daemon 不存在的路由，改为如实规定临时桥接：

- Reviewer/Adjudicator 仍提交 `reviewer_blocked`/`adjudicator_returned`（daemon 现有固定路由 →
  Executor 并原子追加 provenance-bound `fix_defect`）；
- finding 结构化标记 `owner_route: planner`（scope/Contract/架构缺陷）或 `owner_route: executor`
  （实现缺陷）；
- Executor 领取后**先复查 owner_route**：`planner` 项不得实施代码、不得完成该 fix_defect step，
  改以 `executor_blocked_to_user` 升级用户并写明缺口与 finding_id（升级动作由 Executor 合法完成）；
  post-cutover（`planner_governance_v1` 声明后）才由 daemon 直接路由 Planner。
- 同步对齐：AGENTS.md 职责矩阵、role-protocol §1/§3/§4、四个角色模板、user-guide、三 Skill。

### P1-3 capability 分层与源码一致

- capability 拆分为三个独立门禁（role-protocol 顶部声明 + v2 amendment §1）：
  - `planner_governance_v1`：Planner 原生派工（`READY/PLAN`、`planning_*`/`replanning_*` 投影、
    `execution_ready`）、`planner` Role Contract、三种 planner outcome 持久化；
  - `decision_request_v1`：`decision_request`/`decision.respond` 落库、`waiting_for_*` 投影；
  - `adjacent_relation_v1`：`adjacent_defect → related_to` 自动关联。
- **`reviewer_blocked` 自动追加 `fix_defect` 明确为当前已实现能力**（`task_collab_lifecycle.rs`
  现有 verdict 流程行为，无 capability 门禁）——更正 v2 evidence「自动 fix_defect 归入未实施清单」
  的错误表述；v2 amendment §3.1/§3.2 矛盾已消除。
- `execution_ready` 降级为**协议保留值**（`workflow_status_for()` 从不返回；open 任务投影 `queued`），
  归 `planner_governance_v1`；校验器以 `REQUIRED_RESERVED_STATUSES` 断言其不得被标记为已实现
  （`E_PROTO_RESERVED_MARKED_IMPLEMENTED`）。

### P1-4 两个角色 Skill 同步

- `cw-planner-architect/SKILL.md`：新增 design-only/cutover 声明；`executor_replan_requested`
  标注 pre-cutover 无法持久化；
- `cw-executor-senior-engineer/SKILL.md`：移除对 `execution_ready` 的实现要求（改为协议保留值
  说明）；删除直接创建 `decision_request` 的指引（改为协议保留能力说明 + pre-cutover 升级用户）；
  补充计划缺陷 `owner_route=planner` 复查后升级用户的流程；
- `cw-task-loop/SKILL.md`：Role Contract「只由 Executor 冻结」更正为四角色各自的 Role Contract
  由对应角色经 daemon 权威写点冻结；阅读清单改为 v2 amendment + role-protocol（不再引用被
  supersede 的 v1 §3/§5）。

### P1-5 正式 Review 输入

- 本轮全部修复以包含 task_id 的 git commit + 本 evidence（v3）+ ledger entry 构成可复核的
  审查输入；正式 review snapshot（`review_input_snapshot`/`evidence_gate`）由 daemon 在
  Reviewer 领取 review assignment 时评估生成，聊天总结不作为替代。
- Reviewer Role Contract 当前仍绑定 `cw.aprime.reviewer.startup.v1`（daemon 侧投影），v4 模板
  对齐需要后续 contract-revise/daemon 更新——**如实记录为待办**，不在本轮文档 scope 内伪造闭合。

## 2. 其他问题修复

- **17 状态更正**：v2 evidence 声称 18 个状态为计数错误；协议解析实际为 17 个
  `workflow_status`（`REQUIRED_STATUS_ENUM` 固定集，见 §3 验证）。
- **校验器固定枚举**：新增 `REQUIRED_STATUS_ENUM`（17 项）断言枚举与固定集完全一致
  （`E_PROTO_REQUIRED_STATUS_MISSING`）；复审操作「从协议删除 `execution_ready`」现为负向
  self-test 用例 `proto_required_status_missing`，删枚举必报错，不再静默通过。
- **outcome 真实断言**：删除无约束的 `pass`；新增 `REQUIRED_IMPLEMENTED_OUTCOMES`（六项）与
  `REQUIRED_DESIGN_ONLY_OUTCOMES`（planner 三项）精确集合断言（`E_PROTO_OUTCOME_LAYER_FIXED`），
  负向用例 `proto_outcome_fixed` 证明「分区成立但分层错误」必报错。
- **归档 README EOF**：`archive/role-loop/templates/README.md` 末尾多余空行已删除，
  `git diff --check` 工作区零告警。
- **§7 命令补全**：role-protocol §7 补全 Reviewer lease release 完整命令（含必填 `--token` 与
  完整 identity）；Adjudicator 模板的 apply/close 引用改为与 §7 实际命令一致的表述。
- **provenance correction 结构**：ledger 条目 `t1787888909289_provenance_correction_20260828`
  的非法拼接哈希 `commit_id: "5452bdc/e70b0b7/7c4edda"` 更正为
  `corrects_commit_ids: ["5452bdc", "e70b0b7", "7c4edda"]` 数组（JSON 解析复核通过）。

## 3. 验证输出

```text
& C:\Python314\python.exe scripts/validate_template_compliance.py
  结构化合规检查通过：协议单源、4 个角色模板、Skill/user-guide、设计 supersede 与归档 blob 均一致

& C:\Python314\python.exe scripts/validate_template_compliance.py --self-test
  PASS proto_required_status_missing -> E_PROTO_REQUIRED_STATUS_MISSING
  PASS proto_outcome_fixed -> E_PROTO_OUTCOME_LAYER_FIXED
  PASS design_v1_blob_mismatch -> E_DESIGN_V1_BLOB_MISMATCH
  （含既有 14 用例共 17 项）
  self-test: 17 通过, 0 失败（共 17 用例）

& git hash-object docs/design/cw-role-handoff-task-loop.md
  34668462a8c135e106d32fea869b66cb8eec8a56

& git diff --check   # 工作区
  （零输出，无空白错误）

& C:\Python314\python.exe -c "import json; json.load(open('cw_task_commit_ledger.json', encoding='utf-8'))"
  JSON OK
```

## 4. 本轮提交范围（白名单）

```text
.agents/skills/cw-executor-senior-engineer/SKILL.md
.agents/skills/cw-planner-architect/SKILL.md
.agents/skills/cw-task-loop/SKILL.md
.agents/skills/cw-task-loop/references/role-protocol.md
.agents/skills/cw-task-loop/references/user-guide.md
AGENTS.md
Callwarden 无人值守循环启动模板：Adjudicator v4.md
Callwarden 无人值守循环启动模板：Executor v4.md
Callwarden 无人值守循环启动模板：Planner v1.md
Callwarden 无人值守循环启动模板：Reviewer v4.md
archive/role-loop/templates/README.md
cw_task_commit_ledger.json
docs/design/cw-role-handoff-task-loop.md
docs/design/cw-role-handoff-task-loop-v2-amendment.md
deliverables/software-company/T-1787888909289-881595e0-role-protocol-correction-evidence-v3.md
scripts/validate_template_compliance.py
```

工作区中 rust_ext/server/tests 的并行修改（P0-L transport 相关与格式化）**不属于本任务 scope**，
未纳入本提交。

## 5. daemon 状态说明

任务 5/5 步骤 done，投影 `review_pending`（READY/REVIEW，reviewer assignment
`S-1787888909301-88cb1974` queued）。复审 Recommended Handoff 的 `reviewer_blocked` 未由
正式 Reviewer 身份提交（无 lease/identity），daemon 未追加 fix_defect step；本轮修复作为同任务
增量 commit + evidence v3 提交，供正式 Reviewer 领取后对最新 HEAD 复核。
