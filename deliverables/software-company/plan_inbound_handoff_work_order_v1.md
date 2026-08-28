# 计划：任务交接自描述化（inbound_handoff + work_order 只读投影）

版本：v1（Planner 冻结）
计划日期：2026-08-28
基线 HEAD：869a92dfb419eb068f3385e54ccc39e8e20da54b

## 1. 问题（已核实的源码事实）

结构化交接**已经落库**，不是只打印到控制台：

- `rust_ext/src/daemon/task_loop/report_handoff.rs`（约 592-640 行）在 `task_events`
  追加 `reason_code='handoff_structured'`，`reason` 为完整 envelope JSON，含
  `handoff_event_id`、`task_id`、`step_id`、`source_role`、`target_role`、`outcome`、
  `reason`、`request_id`、contract 三元组、`workspace_id`、identity/session、
  `monotonic_seq`、`authoritative_timestamp`。
- 该写入受 lease/fencing、step binding、Contract 一致性、路由三元组和幂等重放门禁保护
  （`report_handoff_test.rs` 已验证非法路由 0 事件、重放不追加事件）。

**缺口**：`rust_ext/src/daemon/task_loop/next_action.rs` 全文不查询 handoff 事件。派工
只读 `role_contracts.handoff_to`，再由 `tasks.status` + steps + verdicts 重新推算。因此
上一棒写入的 `next_action`、`reason`、证据引用和归属对下一棒**不可见**，新 agent 只能拿到
"该 review / 该 claim"，具体做什么仍依赖人工贴提示词。

## 2. 目标

让 `task.next_action` 的响应自带可执行工单，使新 agent 无需人工输入提示词即可接棒。

### 非目标（明确排除，另立任务）

- 不实现 park/cancel/supersede/decompose 等**终止语义**（successor 任务）；
- 不实现 `planner_governance_v1`、`decision_request_v1`、`adjacent_relation_v1`；
- 不实现 `task.inbox` / 唤醒/webhook；
- 不改 findings schema（daemon 仍是 3 字段不透明契约）；
- 不改 `cli/main.py`（17101 行，灾难线；`task next-action --json` 已原样输出 daemon
  响应，新增字段自动可见，无需改 CLI）；
- 不修 `task.reconcile`（`T-1787823611412-2f503878` 独立在办）；
- 不碰 P0-L / A″ 任何任务或其 ownership 文件。

## 3. 复杂度预检结论

单一 ownership：daemon `task_loop` 只读派工投影。无 schema 变更、无新 mutation、无 CLI
变更、无部署门禁扩张。判定为**可由一个 Executor 完成的原子任务**，不拆子任务。

### 规则 47 行数门禁（强制）

`next_action.rs` 当前 **1477 行**，距 1500 硬阈值仅 23 行。因此：

- **禁止**把投影逻辑写进 `next_action.rs`；必须新建独立模块 `inbound_handoff.rs`；
- `next_action.rs` 只允许极小接线（导入 + 一处调用 + 字段插入），且提交前必须核对该文件
  **仍 < 1500 行**；越线即为不可接受的实现方式。

## 4. 所有权白名单（allowed paths）

```text
rust_ext/src/daemon/task_loop/inbound_handoff.rs          (新建)
rust_ext/src/daemon/task_loop/inbound_handoff_test.rs     (新建)
rust_ext/src/daemon/task_loop/mod.rs                      (仅模块注册)
rust_ext/src/daemon/task_loop/next_action.rs              (仅最小接线，须保持 <1500 行)
rust_ext/src/daemon/task_loop/next_action_test.rs         (新增用例)
deliverables/software-company/inbound_handoff_work_order_evidence_*.md  (证据)
```

## 5. 排除路径（excluded paths）

```text
rust_ext/src/daemon/dispatch.rs                  (并行任务在途 dirty)
rust_ext/src/daemon/task_loop/operation_store.rs (并行任务在途 dirty)
rust_ext/src/daemon/task_collab*.rs
rust_ext/src/daemon/task_loop/report_handoff.rs  (写入侧不改，只读其已落库事件)
db/schema.py, db/db_base.py                      (本任务无 schema 变更)
cli/main.py, server/**
任何 P0-L / A″ / T-1787823611412 相关文件
```

## 6. 投影契约（本任务冻结）

在 `task.next_action` 响应中新增两个**派生只读**字段。真相源仍是 append-only
`task_events`；投影不得写库、不得改写事件。

### 6.1 `inbound_handoff`

取该 `task_id` 最近一条 `reason_code='handoff_structured'` 事件（按 `monotonic_seq`
降序），解析其 envelope 后输出：

```json
{
  "handoff_event_id": "he-<task_id>-<request_id>",
  "from_role": "<source_role>",
  "target_role": "<target_role>",
  "outcome": "<outcome>",
  "reason": "<reason>",
  "request_id": "<request_id>",
  "step_id": "<step_id 或 null>",
  "monotonic_seq": 0,
  "authoritative_timestamp": 0.0,
  "matches_current_routing": true
}
```

规则：

- 无 handoff 事件时字段为 `{"diagnosis": "no_handoff"}`，**不得**省略字段、不得编造；
- envelope JSON 解析失败时输出 `{"diagnosis": "unparsable_handoff", "handoff_event_id": ...}`，
  fail-soft 但必须可见，不得静默丢弃；
- `matches_current_routing` = envelope 的 `target_role` 是否等于当前 `routing.next_role`
  的 runtime 映射。**为 false 时不得改写 `routing`**——`routing` 仍由 evaluator 计算，
  该布尔只用于向人和 agent 暴露"上一棒指向与当前派工不一致"这一事实（正是
  `adjudicator_returned` 后 bridge 未闭合的可观测信号）。

### 6.2 `work_order`

从**已有**权威数据派生，不新增数据源：

```json
{
  "objective": "<current_step.action 或 review/adjudicate 语义>",
  "task_title": "<tasks.title>",
  "allowed_paths": ["<来自当前 role_contract>"],
  "excluded_paths": ["<来自当前 role_contract forbidden_paths>"],
  "acceptance_checks": "<role_contract.acceptance_checks>",
  "required_evidence": "<role_contract.required_evidence>",
  "commands": "<role_contract.commands>",
  "prior_attempts": [
    {"step_id": "...", "step_index": 0, "action": "...", "status": "failed", "result": "..."}
  ],
  "prior_handoffs": [
    {"handoff_event_id": "...", "outcome": "...", "reason": "...", "monotonic_seq": 0}
  ]
}
```

规则：

- `prior_attempts` 只取该 task 的 `failed` 步骤（含 `result`），**上限 20 条**，按
  `step_index` 升序。这是防止新 agent 重复走死路的关键字段。
- `prior_handoffs` 取全部 `handoff_structured` 事件的摘要，**上限 20 条**，按
  `monotonic_seq` 升序；超限时截断并附 `"truncated": true`。
- 所有值必须来自 daemon 已有数据；缺失一律输出空数组/空串，禁止编造或从聊天推断。
- 不得输出任何 lease token、raw credential 或 secret（envelope 本身不含，但实现必须
  显式不透传未知字段）。

### 6.3 不变量

1. 纯只读：本模块不得执行任何 `INSERT`/`UPDATE`/`DELETE`；
2. 不改写既有响应字段：`decision`、`action`、`routing`、`next_session`、
   `blocking_reasons`、`workflow_status`、`lifecycle_status` 全部保持现值；
3. 不新增 `workflow_status` 枚举值（终止语义在 successor 任务）；
4. handoff 缺失或损坏时，派工行为与本任务实施前完全一致（向后兼容）。

## 7. 验收命令

```powershell
$env:PYTHON = 'C:\Python314\python.exe'
$env:PYO3_PYTHON = 'C:\Python314\python.exe'

rustfmt --edition 2021 rust_ext/src/daemon/task_loop/inbound_handoff.rs
rustfmt --edition 2021 rust_ext/src/daemon/task_loop/inbound_handoff_test.rs
tokenslim run "cargo check --manifest-path rust_ext/Cargo.toml --lib"
tokenslim run "cargo test --manifest-path rust_ext/Cargo.toml daemon:: --lib"
(Get-Content -LiteralPath rust_ext/src/daemon/task_loop/next_action.rs | Measure-Object).Count
```

最后一条必须 < 1500（规则 47 硬阈值）。

`cargo test daemon:: --lib` 为完整 daemon 回归（AGENTS.md 规则 24），不接受只跑新模块。
注意 `cargo test` 只接受**一个**过滤器（规则 37）。

## 8. 负向测试矩阵（必须全部为真实 Rust 测试，禁止源码字符串断言）

| 用例 | 期望 |
|---|---|
| 无 handoff 事件 | `inbound_handoff.diagnosis == "no_handoff"`，`routing` 与实施前逐字一致 |
| 单条 handoff | 逐字段等于落库 envelope（不得重算 outcome/reason） |
| 多条 handoff | `inbound_handoff` 取 `monotonic_seq` 最大者；`prior_handoffs` 升序完整 |
| envelope 非法 JSON | `diagnosis == "unparsable_handoff"`，不 panic，`routing` 不受影响 |
| `target_role` 与当前 routing 不一致 | `matches_current_routing == false` 且 `routing` **未被改写** |
| 存在 failed step | `prior_attempts` 含该 step 的 `step_id`/`status`/`result` |
| 超过 20 条 handoff/failed step | 截断且 `truncated == true` |
| 只读性 | 调用前后 `task_events`/`tasks`/`task_steps` 行数与内容完全不变 |

## 9. 证据要求

`deliverables/software-company/inbound_handoff_work_order_evidence_20260828.md` 必须含：

- 完整当前 HEAD；
- `cargo check` 与 `cargo test daemon:: --lib` 的真实输出摘要（通过/失败计数）；
- `next_action.rs` 实施后的精确行数；
- 8 条负向用例逐项结果；
- 一次真实 `task next-action <task_id> --json` 的响应片段，展示新字段；
- 证据文件自身 SHA-256。

## 10. 部署门禁

本 workspace 为 `self_bootstrap`，但本任务**只改 daemon 库代码且不要求 live runtime 行为
变更**。Executor 必须在 report 中明确二选一并给出依据：

- 若判定需要 runtime 部署以验证 live `next-action` 输出：按 AGENTS.md 规则 43 执行
  `scripts/refresh_shared_runtime.ps1`，核对 build hash = `runtime\current` hash =
  运行 PID executable hash，并记录 `cw daemon ping/health`；
- 若不部署：必须写明"本轮未部署 runtime，live daemon 仍为旧 binary，新字段仅在库测试中
  验证"，不得含糊。

注意 `scripts/refresh_shared_runtime.ps1` 当前在工作树中为 dirty（属并行任务），Executor
不得修改它。

## 11. 回滚条件

任一项成立即回滚本任务改动并升级：

- `next_action.rs` 越过 1500 行；
- 现有 `daemon::` 测试出现新增失败；
- 派工 `routing`/`decision`/`action` 在任何既有场景下发生变化；
- 发现需要 schema 变更或新 mutation 才能完成（说明 scope 判断错误，应重规划）。

## 12. Successor rule

本任务 closed 后，才创建 successor：

```text
终止语义任务：task.step.park / task.step.cancel / task.plan.supersede /
task.decompose / task.resume + workflow_status parked/superseded/decomposed/cancelled
```

理由：先让工单可自描述，再让卡住的工单可合法退出。若顺序颠倒，parked 状态没有可见的
交接上下文，仍需人工介入判断。

## 13. VCS

- commit message 必须含 `[<task_id>]`；
- 按白名单 `git add`，严禁 `git add .`（工作树有大量并行 dirty/untracked）；
- 追加 `cw_task_commit_ledger.json` 后单独提交；
- 之后再尝试 `cw refresh --all`；若返回 `method_not_found: build_full_graph`，如实记录
  "刷新未完成"，不得旁路伪造。
