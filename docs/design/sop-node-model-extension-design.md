# callwarden 任务节点类型扩展设计（node_type / 人工关口 / 打回重跑）

> 任务 ID: T-1795466678317-5f23dbcc
> 前置分析: [sop-node-model-gap-analysis.md](sop-node-model-gap-analysis.md)
> 参照实现: testcode/LoopForge（internal/domain/workflow.go、run.go，internal/engine/run_ops.go）
> 设计原则: 增量扩展，不破坏既有 task_next_step 状态机与冻结工具清单

## 1. 目标与范围

1. `task_steps` 增加节点类型 `node_type`：`agent` / `script` / `manual_gate` / `sub_flow`。
2. 人工关口：步骤运行暂停等待人工确认，可放行（approve）或打回到指定环节（reject）。
3. 失败步骤重试 + 从任意节点重跑（保留前序产物），节点级产物与日志留痕。
4. 验收：含人工关口的任务跑通 暂停→审批→继续；失败节点打回重跑有留痕；不破坏既有状态机与冻结工具清单。

## 2. 数据模型（schema v62）

### 2.1 task_steps 加列（DDL）

```sql
ALTER TABLE task_steps ADD COLUMN node_type TEXT NOT NULL DEFAULT 'agent';
ALTER TABLE task_steps ADD COLUMN gate_prompt TEXT DEFAULT '';          -- 人工关口审批说明（LoopForge TaskPrompt）
ALTER TABLE task_steps ADD COLUMN sub_task_id TEXT DEFAULT '';          -- sub_flow 节点回链子任务（LoopForge SOPWorkflowID）
ALTER TABLE task_steps ADD COLUMN reject_to_step_id TEXT DEFAULT '';    -- 打回目标步骤（LoopForge RejectTo）
ALTER TABLE task_steps ADD COLUMN max_reworks INTEGER NOT NULL DEFAULT 0;   -- 0 = 不限打回次数
ALTER TABLE task_steps ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 1;  -- 自动重试上限
ALTER TABLE task_steps ADD COLUMN retry_strategy TEXT DEFAULT '';       -- '' / targeted_fix / full_redo
ALTER TABLE task_steps ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0;       -- 当前尝试序号（0 起）
ALTER TABLE task_steps ADD COLUMN rework_count INTEGER NOT NULL DEFAULT 0;  -- 被打回次数
ALTER TABLE task_steps ADD COLUMN feedback TEXT DEFAULT '';             -- 最近一次打回理由
ALTER TABLE task_steps ADD COLUMN gate_result TEXT DEFAULT '';          -- JSON：{decision, reason, snapshot_id, decided_by, decided_at}
```

- `node_type` 取值约束在 db 层校验（SQLite CHECK 不便加列后补充），非法值在 task_create/task_create_from_plan 入口拒绝。
- 旧行默认 `agent`，行为与现状完全一致（向后兼容）。
- 迁移：`db/db_base.py` 新增 `_migrate_v61_to_v62(conn)`，按 `PRAGMA table_info(task_steps)` 逐列判存在再 ADD COLUMN（幂等）；`SCHEMA_VERSION = 62`，`db/schema.py` 头部注释追加 v62 说明。

### 2.2 change_audit 加列

```sql
ALTER TABLE change_audit ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0;
```

写入时带当前 `attempt`，同一 step_id 重跑的留痕按 attempt 区分（LoopForge Artifact 的 attempt 作用域语义）；旧记录默认 0。

## 3. 状态机

### 3.1 步骤状态扩展

```
pending → in_progress → done / failed / blocked          （既有，agent/script/sub_flow 步骤）
pending → in_progress → waiting_approval → done          （manual_gate 放行）
pending → in_progress → waiting_approval → pending       （manual_gate 打回自身重做）
```

- `waiting_approval` 仅对 `node_type='manual_gate'` 步骤合法；其他类型步骤出现该状态视为非法（db 层拒绝）。
- 打回目标步骤及其后步骤重置为 `pending` 时，`attempt/rework_count` 处理见 §4.2。

### 3.2 任务状态扩展

```
in_progress → waiting → in_progress    （manual_gate 暂停/放行，新增 waiting 任务状态）
```

- 任务进入 waiting 的条件：存在 `waiting_approval` 步骤。
- 任务回到 in_progress：关口被 approve（继续跑后续步骤）。
- `review/applied/closed/reverted` 既有转换不变；waiting 任务不可被 task_apply/task_close（状态机前置校验自然拒绝）。
- 任务的 daemon assignment/lease 在 waiting 期间保持挂起：关口等待不是执行租约失效，approve 由具备身份的角色调用（见 §4.4）。

## 4. 动作与 RPC 契约

### 4.1 task_next_step（既有，不改动签名）

- manual_gate 步骤照常通过 pending → in_progress 领取；领取后 executor 执行到关口点，调 `task.report`？——不。关口暂停不走 task.report（report 语义是步骤终态），而是领取后由 daemon 依据 node_type 直接置 `waiting_approval`：
  - 简化实现：`task.claim` 领取 `node_type='manual_gate'` 步骤时，直接置步骤 `waiting_approval`、任务 `waiting`，返回体带 `gate_prompt` 与期望值字段 `expect={step_id, status, attempt, rework_count}`。
  - executor 无需为关口跑任何代码，领取即暂停。
- `_find_next_pending_step_tree` 排除 `waiting_approval` 步骤（只取 pending），防止并发重复领取。
- fix_quality_gate_failure / runtime_deployment 优先级查询（db_tasks.py:908/938）不变，manual_gate 步骤不参与这两个优先级。

### 4.2 新增 RPC（daemon 注册，PROTECTED_MUTATION 级）

```python
task.gate_approve(task_id, step_id, note="", expect=None, identity=None, snapshot_id="")
task.gate_reject(task_id, step_id, target_step_id="", reason="", expect=None, identity=None)
task.retry_from(task_id, step_id, reason="", expect=None, identity=None)
```

- **expect（乐观锁）**：`{status, attempt, rework_count}`，daemon 原子校验 step 当前值，不匹配返回 `E_GATE_STATE_MISMATCH`（对齐 LoopForge NodeExpectation 与 task.claim 的冲突语义）。
- **gate_approve**：校验步骤为 `waiting_approval` + `node_type='manual_gate'`；置 done，写 `gate_result` JSON（decision=approve, reason=note, snapshot_id, decided_by=identity.agent_id, decided_at），任务 waiting → in_progress；发 `task_events` 事件（reason_code=gate.approved，evidence_hash 取 snapshot_id）。
- **gate_reject**：目标 `target_step_id` 缺省取步骤的 `reject_to_step_id`，两者皆空返回 `E_REJECT_TARGET_REQUIRED`；重置范围 = 目标步骤及其后**同任务内**所有非终态步骤（`status IN ('pending','in_progress','waiting_approval','blocked')` → pending，清 result/gate_result/feedback；目标步骤 `rework_count++`、`feedback=reason`；`max_reworks>0` 且超限返回 `E_REWORK_LIMIT`）；不跨 parent_id 子任务边界（LoopForge descendants 仅限同图）；发 reason_code=gate.rejected 事件。
- **retry_from**：校验目标步骤上游（同任务 step_index 更小者）全部 `done`，否则 `E_UPSTREAM_NOT_DONE`；将目标步骤及其后非终态步骤重置 pending、`attempt++`；保留 change_audit（带旧 attempt）与既有产物；已 review/applied/closed 任务走既有 REOPEN_STATUSES 路径（db_tasks.py:477）后再重置；发 reason_code=step.retried 事件。
- **失败步骤重试（既有 report 路径增强）**：`task.report(success=False)` 时，若步骤 `retry_strategy` 非空且 `attempt+1 < max_attempts`：置步骤 `pending`、`attempt++`，不插 fix_defect；否则维持现状（插 fix_defect 步骤）作为兜底。
- **错误码**：`E_GATE_STATE_MISMATCH` / `E_REWORK_LIMIT` / `E_REJECT_TARGET_REQUIRED` / `E_UPSTREAM_NOT_DONE` / `E_NOT_MANUAL_GATE` / `E_IDENTITY_*`（复用既有）/ `E_LEASE_*`（复用既有）。

### 4.3 身份与权限

- 三个新 RPC 均要求 identity（agent_id/agent_instance_id/session_id/model_id/role），写入 `action_identities`（action_type=gate_decision / step_retry）与 `task_events.actor_identity`。
- gate_approve / gate_reject 允许 executor 与 reviewer 角色调用（关口是执行流程的一部分，冻结工具清单不涉及）；retry_from 仅 executor（带 lease/fencing 时走受保护写路径，复用 validate_lease_for_mutation）。

### 4.4 MCP 工具清单

- 不改既有工具签名（task_next_step / task_report_step / task_resolve_block 不变）。
- `server/tools/tools_task.py` 增量注册 `task_gate_approve` / `task_gate_reject` / `task_retry_from` 三个工具，`_categories.py` 归入任务编排类（与 task_resolve_block 同级）。

## 5. 留痕

| 数据 | 落点 |
|---|---|
| 关口裁决 | `task_steps.gate_result` JSON + `task_events`（reason_code=gate.approved/gate.rejected，snapshot_id/evidence_hash）+ `task_gate_decisions`（复用既有表） |
| 打回理由 | 目标步骤 `feedback` + `task_events` |
| 重跑留痕 | `change_audit.attempt`（旧尝试记录保留，新尝试新记录）+ `task_events`（step.retried） |
| 执行者身份 | `action_identities` + `task_events.actor_identity` |

## 6. 实现计划（后续步骤拆分）

| 步骤 | 内容 | 涉及文件 |
|---|---|---|
| S-a | schema v62 迁移 + db 层列读写 | db/schema.py、db/db_base.py、db/db_tasks.py |
| S-b | claim 对 manual_gate 置 waiting_approval + 任务 waiting + expect 返回 | db/db_tasks.py（task_next_step） |
| S-c | gate_approve / gate_reject + 乐观锁 + 事件留痕 | db/db_tasks.py、rust_ext daemon dispatch、server/tools/tools_task.py |
| S-d | retry_from + report 失败重试增强（retry_strategy/attempt） | 同上 |
| S-e | 集成测试：暂停→审批→继续；打回重跑留痕；负测试（状态不匹配/超限/上游未 done/非 manual_gate） | tests/ |

## 7. 测试计划

- **状态机单测**：waiting_approval 仅 manual_gate 合法；waiting 任务不可 apply/close；approve 后任务回 in_progress。
- **打回单测**：重置范围正确（目标及之后 pending、目标 rework_count++、feedback 写入）；MaxReworks 超限拒绝；不跨子任务边界。
- **retry_from 单测**：上游未全 done 拒绝；重置后 attempt++；change_audit 旧记录保留且带旧 attempt。
- **负测试**：expect 不匹配并发拒绝（E_GATE_STATE_MISMATCH）；非 manual_gate 调 gate_approve（E_NOT_MANUAL_GATE）；无 identity 调用拒绝。
- **集成（daemon round-trip）**：创建含 manual_gate 步骤的任务 → claim（步骤 waiting_approval、任务 waiting）→ gate_approve（继续后续步骤至 review）→ 全链路 RPC 走 daemon HTTP 客户端验证；再跑一条 gate_reject 打回路径验证重跑留痕。
- **回归**：既有 task_next_step / task_report_step / task_resolve_block / fix_defect 相关测试全量通过（普通步骤路径零改动）。

## 8. 风险与边界

- `_find_next_pending_step_tree` 及优先级查询以 action 字符串匹配（db_tasks.py:908/938），新动作命名不得冲突（gate/retry 逻辑走 RPC 不走 action）。
- 打回重置与 claim 并发：重置在单事务内完成，乐观锁 expect 在事务内校验，冲突方收到 E_GATE_STATE_MISMATCH 直接失败（不重试）。
- 冻结工具清单：三个新 RPC 为增量注册，不改既有签名与行为；task.apply/close/supersede 仍为 forbidden。
- 旧任务（无 node_type 列数据）默认 agent，全部既有路径不受影响；迁移幂等可重跑。

## 9. 验收对照

| 总目标验收 | 本设计落点 |
|---|---|
| 含人工关口的任务跑通 暂停→审批→继续 | §3.2 waiting 状态 + §4.2 gate_approve + §7 集成测试 |
| 失败节点打回重跑有留痕 | §4.2 gate_reject/retry_from + §5 change_audit.attempt + task_events |
| 不破坏 task_next_step 状态机 | §4.1 普通步骤路径零改动，manual_gate 仅增量分支 |
| 不破坏冻结工具清单 | §4.4 既有工具签名不变，新 RPC 增量注册 |
