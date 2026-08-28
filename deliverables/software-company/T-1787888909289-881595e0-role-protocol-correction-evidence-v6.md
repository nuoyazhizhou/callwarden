# T-1787888909289-881595e0 角色治理修订 v5 — 第三轮复审阻断项修复证据（v6）

**任务：** T-1787888909289-881595e0
**证据版本：** v6（append-only；v5 保持原样，本文追加第三轮复审阻断项 F-1~F-4 的修复记录）
**日期：** 2026-08-28
**触发：** 第三次技术复审 BLOCKED（F-1/F-2 两项 block + F-3/F-4 两项 warn + F-5 一项 advisory）

## 1. 第三轮阻断项修复对照

### F-1（block）owner_route 与 daemon findings 契约冲突 + 缺失 fail-closed

role-protocol §4 的 14 字段 finding schema 原被宣称为「唯一单源」，但 daemon findings 契约是
独立的 3 字段 `[{severity, subject, fact}]`，`owner_route` 在 rust_ext/cli/server/db 中零出现。
修复：

- §4 标题与表注明确标注 schema 为**客户端约定，daemon 不校验**；
- 新增「daemon 契约边界」块：给出 14 字段 → 3 字段的序列化映射（`severity` 对 `severity`，其余折叠进
  `subject`/`fact`），并注明 `owner_route` 等字段无 daemon 落点；
- 补 **fail-closed 规则**：`owner_route` 缺失或无法从 `subject`/`fact` 解析出 `executor`/`planner`
  归属时，Executor 必须按计划缺陷升级用户（`executor_blocked_to_user`），不得默认为实现缺陷硬修；
- v2 amendment §3.3 新增第 9 项：findings schema 扩展与校验。

### F-2（block）pre-cutover 桥接的 parked remediation step 无退出路径

§3 桥接第 2 步会让计划缺陷对应的 fix_defect step 永远停在未完成，且协议未定义退出路径，形成重复
派工活锁。修复：

- §3 桥接末尾新增「Parked remediation step（pre-cutover 无退出路径，如实披露）」段：明确该 step
  保持未完成属**预期状态**、`workflow_status_for()` 仍投影 `remediation_in_progress`、
  `waiting_for_input` 为协议保留值、`step-resolve` 要求 done；
- 明确 **重复派工不构成新授权**：Executor 重复领取时应直接引用既有 `executor_blocked_to_user`
  升级事件与 finding_id，不得重做、不得重新升级、不得实施代码；
- v2 amendment §3.3 新增第 10 项：parked remediation step 的 daemon 终止/取消语义。

### F-3（warn）workflow_status 枚举遗漏 unknown

`workflow_status_for()` 的 `_ => "unknown"` 兜底会返回 `unknown`，但 §2 的 17 值枚举未含它，且
「已实现可 emit」列表标注「实际返回集」不准确。修复：

- §2 枚举补 `unknown`（17 → 18 项）；
- 已实现列表补 `unknown` 并注明它是未知 `tasks.status` 的兜底值；
- 校验器 `REQUIRED_STATUS_ENUM` 同步补 `unknown`（17 → 18），负向用例 `proto_status_layer_fixed`
  的分支随已实现列表措辞更新。

### F-4（warn）atomic_hotfix 零落点且缺 design-only 标注

`atomic_hotfix` 在 rust_ext/cli/server/db 中零出现，仅 AGENTS.md 与 planner SKILL.md 两处。修复：

- AGENTS.md 第 69 行段补 `atomic_hotfix` 为 **design-only（目标模型）** 标注：daemon/CLI/server/db
  无任何字段/参数/校验识别该标记，仅作计划侧分类语义，不构成派工/门禁路径，不得作为绕过复杂度预检/
  Contract 绑定的依据。

### F-5（advisory）决策卡 pre-cutover 结构化承载

不阻断。建议作为独立后续任务：规定 pre-cutover 决策卡以固定子块格式写入 `reason` 或经
`evidence_path` 指向 evidence artifact。本轮未实施，留待 `decision_request_v1` 落库与渲染契约分开推进。

## 2. 验证输出

```text
& C:\Python314\python.exe scripts/validate_template_compliance.py
  结构化合规检查通过：协议单源、4 个角色模板、Skill/user-guide、设计 supersede 与归档 blob 均一致

& C:\Python314\python.exe scripts/validate_template_compliance.py --self-test
  PASS proto_status_layer_fixed -> E_PROTO_IMPLEMENTED_FIXED
  self-test: 18 通过, 0 失败（共 18 用例）

& git diff --check   # 工作区
  （零输出，无空白错误）
```

acceptance 逐项：§4 含「daemon 不校验」标注 + `owner_route` 缺失 fail-closed 规则；§3 含 parked
step 段落并明确「重复派工不构成新授权」；§2 已实现列表含 `unknown`；AGENTS.md 含 atomic_hotfix
design-only 标注；§3.3 新增第 9/10 项。

## 3. 本轮提交范围（白名单）

```text
.agents/skills/cw-task-loop/references/role-protocol.md
AGENTS.md
docs/design/cw-role-handoff-task-loop-v2-amendment.md
scripts/validate_template_compliance.py
deliverables/software-company/T-1787888909289-881595e0-role-protocol-correction-evidence-v6.md
```

工作区中 `rust_ext/src/daemon/*.rs`、`server/*.py`、`tests/*.py` 等并行修改（P0-L transport / 其他任务）
**不属于本任务 scope**，未纳入本提交。

## 4. daemon 状态说明

任务 5/5 步骤 done，投影 `review_pending`（reviewer assignment queued）。本轮修复以增量 commit +
evidence v6 + 台账完整 SHA entry 提交，供正式 Reviewer（独立 instance/session）领取 reviewer lease
后对最新 HEAD 复核。
