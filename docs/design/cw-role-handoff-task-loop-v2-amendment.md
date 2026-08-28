# cw Task Loop 与角色交接协议 v2 Amendment（四角色与 capability 分层 Cutover）

> 状态：**current amendment（追加修订，不改写 v1）**。本文件是对冻结基线
> `docs/design/cw-role-handoff-task-loop.md`（`cw-task-loop/v1`，freeze_design 任务
> `T-1786983366974-8811ccec`）的现行修订；v1 正文自本文件生效起**不再直接修改**，
> 其与四角色模型的差异以下方 supersede 映射为准。
> **冻结 v1 原始 blob：`34668462a8c135e106d32fea869b66cb8eec8a56`**（`git hash-object`
> 可复核；v1 内部不携带 supersede 指针，指针唯一存在于本文件——修复了此前把 supersede
> 声明写进冻结文件导致的基线改写）。曾被 `7c4edda` 改写为 `d1e91b1…`，已恢复。
> 修订任务：`T-1787888909289-881595e0`（角色治理修订 v2）。
> 共享协议（状态枚举、finding schema、Handoff 结构、命令纪律）以
> `.agents/skills/cw-task-loop/references/role-protocol.md` 为唯一单源；本文件只定义
> 设计层修订与 capability 分层，不复制其枚举/字段列表。

## 1. Supersede 映射（精确到节）

v1 原文保持冻结原样（blob `34668462…`）；以下 v1 条目由本 amendment 对应章节 supersede，
阅读 v1 时必须按下表替换理解：

| v1 冻结原文 | 与四角色模型的冲突 | 由本文件 supersede 为 |
|---|---|---|
| §1「本协议只有三个治理角色：executor/reviewer/adjudicator；Executor 包含规划…」 | Planner 已独立为第四治理角色 | §2 四角色模型 |
| §1「`planner`…是 RuntimeRole 兼容值…不增加治理角色」 | planner 现为目标治理角色（capability 上线前 design-only） | §2、§3 cutover |
| §3 规则 5「`BLOCKED/REVISE`，目标为 Executor」 | 退回应按缺陷类别双轨路由 | §4 双轨整改状态机 |
| §3 规则 9「`READY/REVISE` 给 Executor」（reviewer BLOCKED） | 同上 | §4 双轨整改状态机 |
| §3 规则 11「若不接受则 `READY/REVISE`，带具体退回 finding 交给 Executor」 | Adjudicator 退回同样双轨 | §4 双轨整改状态机 |
| §5「Skill 固定流程 1. 读取 AGENTS.md、适用的冻结设计和…」 | live Skill 不得再以 v1 §3/§5 为现行依据 | §6 live 文档阅读清单 |
| §5「`role_contract.skill_id`…只由 Executor 冻结」 | capability 上线后由 Planner 冻结计划侧合同 | §3 cutover 条件 |
| §6 验收矩阵「六种 handoff outcome 与 next_role/independence 组合」 | 六种为当前已实现集；planner outcome 为 design-only | §5 outcome 分层 |
| §6「`reviewer BLOCKED` … READY / REVISE / executor」 | BLOCKED 退回按缺陷类别路由 | §4 双轨整改状态机 |

不在上表中的 v1 条目（workspace authority preflight、Verdict Ledger、Evidence Gate、lease/fencing、
幂等与 dedupe、§7 分期路线图等）继续有效，不受本 amendment 影响。

## 2. 四角色模型（supersede v1 §1）

治理角色为 `planner`、`executor`、`reviewer`、`adjudicator` 四个。职责矩阵、共享状态枚举、
结构化 finding schema、decision request 规范与 Handoff 字段见
`.agents/skills/cw-task-loop/references/role-protocol.md`（唯一单源）。

`implementer`、`tester`、`evidence`、`independent_reviewer` 仍是 RuntimeRole 兼容值（映射为
executor/reviewer），不增加治理角色。`planner` 是新增**目标**治理角色；在 daemon 通过
`planner_governance_v1` capability cutover（§3）前，runtime 将 `planner` 映射为 executor 兼容值
（`rust_ext/src/daemon/task_loop/next_action.rs::runtime_role`），CLI `--role-contracts` 也只接受
executor/reviewer/adjudicator（`cli/main.py::_build_role_contracts`）——这是**预期的 pre-cutover
行为**，不是缺陷；文档与模板不得据此声称 Planner 已可领取派工。

## 3. Capability 分层与 Cutover

### 3.1 门禁定义（按能力拆分，与 daemon 源码对齐）

能力拆分为三个独立 capability，各自独立声明、独立 cutover（避免"一个 capability 混装已实现与
未实现行为"的矛盾）：

| capability | 把关能力 | 未声明前 |
|---|---|---|
| `planner_governance_v1` | Planner 原生派工（`READY/PLAN`、`planning_*`/`replanning_*` 投影、`execution_ready`）、`planner` Role Contract、`planner_ready_for_execution`/`planner_replan_required`/`executor_replan_requested` outcome 持久化 | `task.next_action` 不产生 `READY/PLAN`；`task.create`/`--role-contracts` 拒绝 `planner`；`task.handoff` 拒绝上述三种 outcome（CLI `_STRUCTURED_HANDOFF_ROUTES` 与 daemon `outcome_route` 仍只有已实现六种） |
| `decision_request_v1` | `decision_request`/`decision.respond` 落库、`waiting_for_decision`/`waiting_for_input` 投影 | 不落库、不投影；多路线决策由当前持棒角色在 handoff/verdict 文本中升级用户 |
| `adjacent_relation_v1` | `adjacent_defect → related_to` 自动关联 | 由人工在 finding/ledger 中记录关联 |

**明确不在任何 capability 门禁内（当前已实现）**：`reviewer_blocked` 触发 daemon 在同一主任务
原子追加 provenance-bound `fix_defect`（`task_collab_lifecycle.rs` 现有 verdict 流程行为）。
`adjudicator_returned` 仅固定路由 Executor，不自动追加 step/reopen；当前 CLI/MCP 尚无受支持的
端到端 remediation bridge，不得把固定路由描述成可执行整改能力。

capability 声明必须走 daemon 公共 capability 通道（v1 §7 0A Capability Authority Amendment 的
public promote 路径），不得用客户端文档、模板标注或聊天声明代替。客户端查询方式以 daemon
capability 投影为准；查询不到该 capability 时一律按未声明处理（fail-closed）。

### 3.2 Cutover 前后的行为差异

| 行为 | pre-cutover（当前） | post-cutover |
|---|---|---|
| 新任务规划 | Executor（复杂度预检由其自查）或用户手动拆分 | Planner 领取 `READY/PLAN` |
| 计划/scope/Contract/架构缺陷 | **临时桥接**（§4）：Reviewer 用 `reviewer_blocked` 提交后由 daemon 追加 `fix_defect`，Executor 按 `owner_route=planner` 复查并以 `executor_blocked_to_user` 升级用户；Adjudicator 用 `adjudicator_returned` 提交后因当前无受支持 remediation bridge，重新查询并如实记录治理实现缺口，不得合成 Executor step | `replan` 路由 Planner（`replanning_pending`） |
| 实现缺陷 | daemon 追加 `fix_defect` → Executor（现有行为） | 同左（不变） |
| `decision_request` | 不落库（`decision_request_v1` 未声明）；文本升级 | daemon 落库并投影 `waiting_for_decision` |
| Planner 模板 | **design-only**，不可作为现行派工入口 | 现行入口 |

### 3.3 Cutover 的 daemon 实现前置工作（未实施清单）

以下为后续独立 daemon/CLI/MCP 任务的实现范围，本 amendment 只冻结目标语义：

1. Planner 原生注册：runtime_role 不再把 `planner` 映射为 executor；claim/lease 接受 planner 合同；
2. 四角色 `task.create`：`--role-contracts` 接受 planner；
3. `PLAN`/`replan` 投影：`planning_*`/`replanning_*`（含 `execution_ready`）workflow_status 的
   daemon 派生与 emit；
4. 动态整改路由：verdict/退回按 finding 类别（实现 vs 计划）路由 Executor 或 Planner；补齐
   `adjudicator_returned` 的原子 remediation 或正式 CLI/MCP 路由，并将 source handoff event、
   verdict、findings 与 source step 绑定为可验证 provenance；
5. `planner_ready_for_execution`/`planner_replan_required`/`executor_replan_requested` 三种
   outcome 的 handoff 持久化（CLI 与 daemon `outcome_route` 同步扩展）；
6. `decision_request` 落库与 `decision.respond` RPC（`decision_request_v1`）；
7. `adjacent_defect → related_to` 自动关联（`adjacent_relation_v1`）；
8. capability 声明 `planner_governance_v1`/`decision_request_v1`/`adjacent_relation_v1`
   （依赖 v1 §7 0A Capability Authority，各自独立声明）。
9. findings schema 扩展与校验：daemon 从 `[{severity, subject, fact}]` 不透明字符串升级为结构化
   校验，落库 `owner_route` 等归属字段并用于整改路由判定（消除 role-protocol §4 客户端约定与
   daemon 3 字段契约之间的 gap，及 `owner_route` 缺失导致计划缺陷静默退化为实现缺陷硬修的风险）；
10. parked remediation step 的 daemon 终止/取消语义：pre-cutover 桥接产出的未完成 fix_defect step
    如何显式终止/挂起，避免 `in_progress` + `revise_current_step` 反复投影 `remediation_in_progress`
    导致的重复派工活锁。

## 4. 双轨整改状态机（supersede v1 §3 规则 5/9/11、§6 部分行）

整改只有两条互斥轨道，按缺陷类别判定，不按"谁方便"判定：

```text
实现缺陷（代码/测试/证据与冻结计划不符）
  → Reviewer BLOCKED：task.handoff reviewer_blocked
    → daemon 在同一事务原子追加 provenance-bound fix_defect（当前已实现的唯一自动路径）
    → remediation_pending → Executor 领取 → remediation_in_progress
  → Adjudicator 退回：task.handoff adjudicator_returned
    → daemon 仅持久化 handoff 并固定路由 executor（不自动追加 step、不自动 reopen）
    → 当前 CLI/MCP 没有受支持的 provenance-bound remediation bridge
    → 重新查询同一 task；若 next_action 仍按 Reviewer PASS 返回 READY/ADJUDICATE，
      记录精确 task/verdict/handoff/source-step 治理缺口，交后续 daemon/CLI/MCP 实现任务；
      不得使用通用 RPC、伪造 step 或自行声称 remediation_pending

scope/Contract/架构缺陷（计划边界、拆分方式、验收目标、依赖或架构选择错误）
  → post-cutover：replan → replanning_pending → Planner
  → pre-cutover（临时桥接，如实反映 daemon 固定路由）：
      1. Reviewer 用 reviewer_blocked 提交；daemon 原子追加 fix_defect，finding.owner_route
         必须写 planner；
      2. Executor 领取该 fix_defect 后按 owner_route 复查：确认计划缺陷时不得实施代码、
         不得完成该 step，立即改交 executor_blocked_to_user 升级用户
         （next_role=user，reason 写明计划缺口与 finding_id）；
      3. Adjudicator 用 adjudicator_returned 提交后重新查询同一 task；由于当前没有受支持的
         remediation bridge，若仍为 READY/ADJUDICATE，记录治理实现缺口并等待后续
         daemon/CLI/MCP 修复，不得伪造可领取的 Executor step。
    该桥接是 pre-cutover 唯一合法升级路径——daemon 尚未提供 reviewer/adjudicator→user 路由，
    Reviewer/Adjudicator 也不能冒用 executor_blocked_to_user（from_role 校验拒绝）。
```

判定责任：Reviewer/Adjudicator 在 finding 中写明归属（实现缺陷 → Executor；计划缺陷 → Planner）；
Executor 领取前复查，发现计划缺陷按 pre/post-cutover 规则处理，不得把计划缺陷当作实现缺陷硬修。
两条轨道都只追加（fix_defect step、计划 revision、decision event），不改写历史 verdict/evidence。

## 5. Handoff outcome 分层（supersede v1 §6 outcome 行）

完整字段、固定顺序与路由三元组以 role-protocol.md §5 为唯一单源。本节只声明实现分层：

- **当前已实现（daemon/CLI 接受）**：`executor_ready_for_review`、`executor_blocked_to_user`、
  `reviewer_pass`、`reviewer_blocked`、`adjudicator_accepted`、`adjudicator_returned`；
- **design-only（`planner_governance_v1` 上线后启用）**：`planner_ready_for_execution`、
  `planner_replan_required`、`executor_replan_requested`。

客户端在 pre-cutover 阶段发送 design-only outcome 会得到 daemon 结构化拒绝（如
`E_LEASE_REQUIRED`/invalid outcome）；这是 fail-closed 正常行为，不得在客户端本地"补持久化"。

## 6. Live 文档阅读清单（supersede v1 §5 对 Skill 的阅读要求）

现行文档消费顺序（live）：

1. `AGENTS.md`（身份、角色矩阵、默认工作规则）；
2. `.agents/skills/cw-task-loop/references/role-protocol.md`（唯一单源：状态枚举、finding
   schema、Handoff、decision request、命令纪律）；
3. **本 amendment**（四角色模型、cutover、双轨整改）；
4. v1 `docs/design/cw-role-handoff-task-loop.md` 仅作历史冻结基线与未 supersede 条目的依据
   （§7 分期路线图、workspace authority、Verdict Ledger 等仍有效）。

`cw-task-loop` Skill、四角色模板与 user-guide 不再要求阅读 v1 §3/§5 作为现行规则来源。

## 7. Provenance 与审计对照

本 amendment 由任务 `T-1787888909289-881595e0` 产生，修复 2026-08-28 角色治理审计的 P1/P2 项：
「Planner 文档已启用而 daemon 不支持」「handoff outcome 无法持久化」「整改路由自相矛盾」
「冻结 v1 顶部声明不足以消除正文三角色矛盾」。越界提交（`5452bdc`/`e70b0b7`/`7c4edda` 绑定
`T-1787850432491-f42a2b8c`）的 provenance 更正见 `cw_task_commit_ledger.json` 追加条目
（不改写旧记录）；归档改写（`Executor _ Planner v3` blob `8ba6501…` → `fd03368…`）已恢复原始
blob，见 `archive/role-loop/templates/README.md`。
