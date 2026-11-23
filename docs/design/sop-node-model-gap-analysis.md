# LoopForge SOP 节点模型与 callwarden task/steps 差距分析

任务: T-1795466678317-5f23dbcc
参照实现: `testcode/LoopForge/internal/httpapi/runs.go`（runsHandlers）、`internal/domain/workflow.go`、`internal/domain/run.go`、`internal/engine/run_ops.go`、`docs/bot-sop-controls.md`

## 0. 结论摘要

callwarden 现有 task/steps 是**线性步骤队列**（step_index 顺序 + DFS 子任务树），LoopForge 是**有向节点图 + 人工关口 + 打回/重跑语义**。差距集中在四块：节点类型缺失、人工关口状态缺失、打回/任意节点重跑语义缺失、节点级产物留痕缺失。四项均可通过「task_steps 增量加列 + 新增少量 RPC 动作」引入，不需要重写既有 `task_next_step` 状态机，也不触碰冻结工具清单。

## 1. 节点类型：Agent / 脚本 / 人工关口 / 子 SOP

### LoopForge 参照

- `WorkflowNode.Type` 四类（workflow.go:11-16）：`agent`（LLM 执行）、`script`（硬脚本/校验）、`approval`（人工关口，`TaskPrompt` 为给审批人的上下文说明）、`sop`（子 SOP，`SOPWorkflowID` 指向子流程）。
- 节点级附加字段：`RejectTo`（打回目标节点）、`OnFailure`（fail/reject）、`RetryStrategy`（targeted_fix/full_redo）、`CarryOver`、`MaxAttempts`、`MaxReworks`、`ArtifactOutputs`（产物契约声明）。
- 运行态 `NodeRun`（run.go:146-168）：`Status`（pending/running/succeeded/failed/waiting_approval/pending_input…）、`Attempt`、`ReworkCount`、`Feedback`、`ChildRunID`（子 SOP run 回链）、`GateResult`（关口结构化裁决 pass/rework/fail/human）。

### callwarden 现状

- `task_steps` 表（schema.py:374-389）：`action`（自由文本）、`target_file`、`target_symbol`、`check_items`、`status`（pending/in_progress/done/failed/skipped/blocked）。**没有 node_type 列**，所有步骤同质，无法区分 LLM 步骤、脚本校验、人工关口、子流程。
- 子流程仅以 `tasks.parent_id` 子任务树形式存在（深度优先领取），不是「节点指向子 SOP 定义」的显式边。

### 差距与引入建议

| LoopForge 概念 | callwarden 落点 | 改动量 |
|---|---|---|
| node_type（agent/script/approval/sop） | `task_steps` 新增 `node_type TEXT DEFAULT 'agent'`，枚举对齐为 agent/script/manual_gate/sub_flow（任务描述口径） | 加列 + 默认值，旧行自动视为 agent，向后兼容 |
| approval 节点 TaskPrompt | 复用 `check_items` 或新增 `gate_prompt` 列承载审批说明 | 可复用，不必加列 |
| sop 节点 SOPWorkflowID | 复用 `tasks.parent_id` 子任务树，或新增 `sub_task_id` 列回链子任务 | 建议新增列，保留显式边 |
| RejectTo / MaxReworks / RetryStrategy | 新增 `reject_to_step_id`、`max_reworks`、`retry_strategy` 列（默认空 = 不启用） | 加列，默认关闭语义 |
| GateResult | 新增 `gate_result` JSON 列（裁决/理由/证据） | 加列 |

## 2. 重试 / 打回 / 从任意节点重跑语义

### LoopForge 参照

- **人工关口**：节点进入 `waiting_approval`，run 状态 `waiting`。
  - `Approve`（run_ops.go:140）：节点置 succeeded，写 `decision.md` 产物 + `gate.approved` 事件，run 回 running。
  - `Reject`（run_ops.go:271）：打回到 `RejectTo`（或显式 TargetID），把目标节点及其**全部后代**重置为 pending（清空 Output/Error/ChildRunID），目标节点 `ReworkCount++` 并记录 `Feedback`（打回理由），受 `MaxReworks` 上限约束，发 `gate.rejected` 事件；仍在 running 的旁支节点按「打断」处理，重跑计为新 attempt。
  - `Pass`（run_ops.go:186）：失败节点人工标记 succeeded 不重跑（环境问题等场景），仅限 script 节点。
  - `DeclineRetry`：拒绝自动重试，节点终态。
- **Retry / 从任意节点重跑**（run_ops.go:364）：显式指定 nodeID 时要求**上游节点全部 succeeded**（`replayUpstreamSucceeded`）；保留原 run、上游节点及其冻结产物；已清理 workspace 的 run 先恢复 checkpoint；自动 attempt 耗尽时人工 retry 只多给一次 attempt。
- **期望值乐观锁**：人工决策携带 `NodeExpectation{NodeID, Status, Attempt, ReworkCount}`（run_ops.go:157 等），状态不符即拒绝，防止并发误操作。

### callwarden 现状

- 步骤只有 `pending → in_progress → done/failed/blocked` 单向流；`failed` 不可重跑，只能由 daemon 自动追加 `fix_defect` 步骤（db_tasks.py:1769-1807）在队尾修复。
- `task.reopen`（tools_task.py:244-259）仅支持把 `blocked` 步骤恢复 `pending`（guardrail 解锁），**没有**「打回到指定环节并重置其后所有步骤」「从任意已完成节点重跑」的语义。
- 失败即插 fix_defect，与 LoopForge 的「打回 + ReworkCount + MaxReworks」是两套正交机制。
- 人工关口完全缺失：没有任何 waiting_approval 状态；审批目前只能通过 review 角色在任务级收口，无法在步骤级暂停等待人工确认后放行/打回。

### 差距与引入建议

1. **人工关口**：`node_type='manual_gate'` 的步骤领取后进入新状态 `waiting_approval`（run 级任务状态同步为 waiting），新增 RPC `task.gate_approve`（放行 → 步骤 done，记录裁决）与 `task.gate_reject`（打回 → 目标步骤及之后步骤重置 pending，目标步骤 rework_count++ 并记 feedback）。两者都要求携带与 LoopForge NodeExpectation 等价的乐观锁字段（step_id + 当前 status/attempt/rework_count），不匹配即拒绝。
2. **打回**：`task.gate_reject` 携带 `target_step_id`（缺省用步骤的 `reject_to_step_id`），重置范围 = 目标步骤及其后所有非终态步骤；受 `max_reworks` 约束；打回理由写入目标步骤 `feedback` 并发 `task_events` 事件（复用现有事件流，reason_code=gate_rejected）。
3. **失败步骤重试**：`task.report(success=False)` 现状插入 fix_defect 步骤；引入 `retry_strategy` 后，对可重试步骤改为「重置该步骤为 pending + attempt++」，超过 max_attempts 才落 fix_defect，保留既有兜底。
4. **从任意节点重跑**：新增 RPC `task.retry_from`（executor 可用，受 lease/identity 保护），指定 step_id，校验其上游步骤全部 done（对应 replayUpstreamSucceeded），将目标步骤及之后步骤重置 pending、attempt++，保留 `change_audit` 与既有产物（对应「保留前序产物」）；已 closed/review 的任务走既有 REOPEN_STATUSES 路径（db_tasks.py:477）。
5. **不破坏既有状态机**：新状态 waiting_approval 只对 `node_type='manual_gate'` 步骤生效；`task_next_step` 对普通步骤的领取顺序、fix_defect 优先级、runtime_deployment 门禁逻辑全部不变；不新增冻结工具清单之外的写路径（新 RPC 走 daemon PROTECTED_MUTATION 同级管控）。

## 3. 节点级产物与日志留痕

### LoopForge 参照

- `Artifact`（run.go:238-253）：按 `RunID + NodeID + Attempt` 不可变快照，含 Name/Path/MediaType/Size/SHA256；重做（rework）保留旧 attempt 产物，下游消费最新成功版本，审计可回溯每次重做。
- 遥测/日志：`TelemetryEvent` 带 `NodeID + Attempt`，`LogRecord` 带 NodeID；人工关口裁决同时落 `decision.md` 产物与 `gate.approved/gate.rejected` 控制事件。

### callwarden 现状

- `change_audit`（schema.py:392-404）：按 `task_id + step_id` 记文件级 hash_before/hash_after/diff —— 已是步骤级留痕，但**没有 attempt 维度**，重跑后同一 step_id 的记录混叠，无法区分第几次尝试。
- `task_events`（schema.py:407-426）：有 `step_id`、`actor_identity`、`role`、`contract_hash`、`snapshot_id`、`evidence_path/evidence_hash`，事件流本身能承载关口裁决事件；`task_evidence_events`、`task_gate_decisions`（schema.py:1276、1308）已有任务级裁决/证据表可复用。
- 缺口：无 attempt 计数列；无「产物」概念（change_audit 只记文件 diff，不记 SHA256 产物快照）；关口裁决没有结构化落点。

### 差距与引入建议

- `task_steps` 新增 `attempt INTEGER NOT NULL DEFAULT 0`、`rework_count INTEGER NOT NULL DEFAULT 0`、`feedback TEXT DEFAULT ''`、`gate_result TEXT DEFAULT ''`（JSON）。
- `change_audit` 新增 `attempt INTEGER DEFAULT 0` 列，写入时带上当前 attempt，实现 attempt 作用域留痕；旧记录默认 0 向后兼容。
- 关口裁决复用 `task_gate_decisions` / `task_events` 落 `gate.approved`/`gate.rejected` 事件（reason_code + snapshot_id/evidence_hash），不新建表。
- 产物快照复用 `task_evidence_events`（evidence_path + evidence_hash），每关口节点产物一条事件，天然 append-only。

## 4. 验收路径对照（任务总目标）

| 任务总目标 | 本分析给出的最小实现面 |
|---|---|
| step 增加 node_type | task_steps 加 node_type 列 + 枚举校验 |
| 人工关口暂停/放行/打回 | waiting_approval 状态 + task.gate_approve / task.gate_reject（含乐观锁） |
| 失败重试 + 从任意节点重跑 | retry_strategy + task.retry_from（上游全 done 校验，保留 change_audit） |
| 节点级产物与日志留痕 | attempt/rework_count/feedback/gate_result 列 + change_audit.attempt + task_events 关口事件 |
| 含人工关口的任务跑通 暂停→审批→继续 | 集成测试：创建含 manual_gate 步骤的任务，claim 后 gate_approve 放行继续，gate_reject 打回后重跑留痕 |
| 不破坏 task_next_step 状态机与冻结工具清单 | 新状态/新动作只对 manual_gate 与显式新 RPC 生效；普通步骤领取路径零改动 |

## 5. 风险与边界

- `task_next_step` 的 fix_defect / runtime_deployment 优先级查询以 action 字符串匹配（db_tasks.py:908、938），新动作命名不得与之冲突。
- 打回重置「目标步骤及之后步骤」时，若存在子任务树（parent_id），需按 DFS 顺序限定在同一 task 内重置，避免跨任务误伤（LoopForge descendants 仅限同图）。
- 乐观锁字段必须进 RPC 参数并由 daemon 原子校验（对齐 task.claim 的 claim 冲突语义），否则并发审批会互相覆盖。
- 冻结工具清单：新增 RPC 不改动既有 MCP 工具签名（task_next_step / task_report_step / task_resolve_block 保持不变），只做增量注册。
