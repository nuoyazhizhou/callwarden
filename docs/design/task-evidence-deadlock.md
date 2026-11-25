# report/handoff 证据契约死锁：现状梳理与对齐方案

- 任务：T-1795492756996-4996aaf8（step S-1795492757019-4af60b50，write）
- 日期：2026-11-26
- 状态：设计文档（实现与负面测试见后续步骤）

## 1. 问题陈述

2026-11-24 executor 实测发现 report/handoff 证据契约死锁：

1. `task.report` 未携带 evidence 三元组（evidence_path/evidence_hash）被接受；
2. 随后 `task.handoff` 以「evidence 字段非空」拒绝，错误信息未说明根因与恢复路径；
3. 补带证据的 `task.report` 重报可恢复，但报错没有指引该路径；
4. `task.contract_bootstrap` bridge 对带 Role Contract 的任务 fail-closed，不可用作解锁手段。

目标：消除死锁窗口——report 阶段对后续 handoff 必需的证据提前校验，拒绝时错误信息明确指出缺什么、如何补；不改变成功路径语义。

## 2. 证据校验点梳理（file:line）

以下行号基于 master@e26f7fc。

### 2.1 task.report（`rust_ext/src/daemon/task_collab_lifecycle.rs` `handle_task_report`，L124）

| 校验点 | 位置 | 触发条件 | 行为/错误码 |
|---|---|---|---|
| evidence_path/evidence_hash 读取 | L142-149 | 无任何校验，`unwrap_or("")` 缺省空串 | 可选，空串静默落库 |
| snapshot_id 非空 | L242-247 | snapshot_id 为空 | `E_TASK_REPORT_SNAPSHOT_REQUIRED`（提前校验先例） |
| snapshot authority | L248 | snapshot 与任务 workspace authority 不符 | `validate_report_snapshot_authority` 拒绝 |
| `reported` event 落库 | L506-529 | 总是 | evidence_path/evidence_hash 以可能为空的值写入 task_events |

关键事实：report 全程不校验 evidence 非空。任务进入 `review` 的判定在 L472-487（无 pending/in_progress 步骤且无 unresolved failed），与 evidence 无关。

### 2.2 task.handoff（同文件 `handle_task_handoff`，L1209）

| 校验点 | 位置 | 触发条件 | 错误码 |
|---|---|---|---|
| evidence_path 必填非空 | L1254 | 缺失/空白 | `E_HANDOFF_STRUCTURED_REQUIRED` |
| evidence_hash 必填非空 | L1255 | 缺失/空白 | `E_HANDOFF_STRUCTURED_REQUIRED` |
| report provenance 存在 | L1344-1367 | report_request_id 无对应 `reported` event | `E_HANDOFF_REPORT_NOT_FOUND` |
| 三元组一致 | L1368-1376 | step_id/evidence_path/evidence_hash 任一与最近一条 `reported` event 不等 | `E_HANDOFF_REPORT_PROVENANCE_MISMATCH` |

`executor_ready_for_review` 的 provenance 查询（L1345-1351）取 `reason_code='reported'` 的**最近一条** event（`ORDER BY event_id DESC LIMIT 1`），三元组必须与 handoff 信封完全一致。

其它结构错误码：`E_HANDOFF_OUTCOME_INVALID`（L1271-1276）、`E_HANDOFF_STEP_REQUIRED`（L1278-1283）、`E_HANDOFF_ROUTE_INVALID`（L1284-1292）、`E_HANDOFF_ROLE_IDENTITY_MISMATCH`（L1293-1304）、`E_IDENTITY_REQUIRED`（L1257-1262）。

### 2.3 task.contract_bootstrap bridge（`rust_ext/src/daemon/task_collab_contract.rs` L760 + `rust_ext/src/daemon/task_loop/task_contract_bootstrap.rs` L238）

| 校验点 | 位置 | 触发条件 | 错误码 |
|---|---|---|--- |
| role=adjudicator | task_collab_contract.rs L804-809 | identity.role ≠ adjudicator | `E_TASK_CONTRACT_BOOTSTRAP_ROLE_REQUIRED` |
| evidence 必填 | task_collab_contract.rs L811-826 | evidence_path/evidence_hash 为空 | `E_TASK_CONTRACT_BOOTSTRAP_EVIDENCE_REQUIRED` |
| 治理投影状态 | task_contract_bootstrap.rs L188-210 | 已存在任一治理投影（唯一例外：`(1,0,0,0)` 半成品恢复） | `E_TASK_CONTRACT_BOOTSTRAP_NOT_EMPTY` |
| 三角色源预检 | task_contract_bootstrap.rs L218-235 | 非 allowlist 且缺 current legacy role contract | `E_TASK_CONTRACT_BOOTSTRAP_ROLE_SOURCE` |

## 3. 死锁场景实测记录

### 3.1 实测步骤与观测（2026-11-24 executor 会话）

| 步骤 | 操作 | 观测 |
|---|---|---|
| 1 | `task.report` 不携带 evidence_path/evidence_hash（step 完成、任务进入 review） | **被接受**，返回 request_id，task_events 落 `reported` event，evidence 字段为空串 |
| 2 | `task.handoff` outcome=executor_ready_for_review，携带非空 evidence 三元组 + 该 request_id | **拒绝** `E_HANDOFF_REPORT_PROVENANCE_MISMATCH`（report event 的 evidence 为空串，与 handoff 非空值不等） |
| 2' | `task.handoff` 不携带 evidence | **拒绝** `E_HANDOFF_STRUCTURED_REQUIRED`（evidence 字段非空硬门禁） |
| 3 | 补带 evidence_path/evidence_hash 的 `task.report`（新 request_id）后用新 request_id 重试 handoff | **恢复成功** |

### 3.2 代码机理解释

- 步骤 1 被接受：report 的 evidence 读取是 `unwrap_or("")`（L142-149），无校验；空串随 `reported` event 落库（L506-529）。
- 步骤 2 拒绝：handoff 信封要求 evidence 非空（L1254-1255），且与最近一条 `reported` event 三元组逐字段相等（L1368-1376）。原 report 的 evidence 是空串，永远无法与 handoff 的非空值相等。
- 步骤 2' 拒绝：`text_field` 对空值统一报 `E_HANDOFF_STRUCTURED_REQUIRED`（L1214-1226）。
- 步骤 3 恢复：新的 report 产生新的 `reported` event（带非空 evidence），成为「最近一条」，handoff 用新 request_id + 一致三元组即可通过。

### 3.3 死锁窗口与缺陷点

死锁窗口 = 「report 被接受」到「executor 主动补带证据重报」之间。此窗口内：

- 任务卡在 review 状态，handoff 两个方向都被拒（空 evidence 撞 L1254-1255，非空 evidence 撞 L1368-1376）；
- `E_HANDOFF_REPORT_PROVENANCE_MISMATCH` 的 message 只有「report_request_id 与 step_id/evidence_path/evidence_hash 不一致」，未指出根因（原 report 未携带证据）与恢复路径（补带证据重报 + 新 request_id），executor 只能靠猜测或外部知识解锁。

## 4. task_contract_bootstrap bridge fail-closed 实测结论

实测结论：**带 Role Contract 的任务无法通过 task.contract_bootstrap bridge 重建或修补治理投影，bridge 对其 fail-closed 不可用。**

机理（代码确证）：

1. 现代 `task.create` 携带 role_contracts 时，在同一事务写入 legacy role_contracts **和**完整治理投影（`rust_ext/src/daemon/task_collab.rs` L3456-3487：task_contract_revisions + role_contract_lineages/role_contract_revisions + task_step_role_contract_bindings）。本任务卡 T-1795492756996-4996aaf8 即由此路径创建（claim 需 identity + contract_claim 可证其带 Role Contract）。
2. bridge 的 `bootstrap_projection_state`（task_contract_bootstrap.rs L188-210）检测到任何治理投影即拒绝：`E_TASK_CONTRACT_BOOTSTRAP_NOT_EMPTY`（「task 已存在治理投影，bootstrap 只允许完全缺失的任务」），唯一例外是 `(1,0,0,0)` 半成品恢复。
3. 因此对带 Role Contract 的任务，bridge 在投影检查处必然 fail-closed；它只服务于「三层治理数据全缺失」的历史任务（allowlist / legacy 派生），不是 report/handoff 死锁的恢复通道。死锁恢复只能走「补带证据重报」路径（§3.1 步骤 3）。

## 5. 对齐方案

### 5.1 涉及模块

| 模块 | 文件 | 改动 |
|---|---|---|
| report 提前校验 | `rust_ext/src/daemon/task_collab_lifecycle.rs`（handle_task_report） | 新增 evidence 必填 guard + 新错误码 |
| handoff 指引增强 | 同文件（handle_task_handoff L1368-1376） | 细分错误码 + message 指明根因与恢复路径 |
| CLI 错误回显 | `cli/main.py`（task report/handoff 分支） | 无需逻辑改动（错误码/消息透传），如需可补错误码文案 |
| 负面测试 | `tests/`（Rust 或 Python 侧按现有 harness） | 死锁场景 + 恢复路径 + 过程性 report 回归 |

### 5.2 新错误码

| 错误码 | 触发点 | 语义 |
|---|---|---|
| `E_REPORT_EVIDENCE_REQUIRED` | report 使任务进入 review（next_status="review"）但 evidence_path/evidence_hash 为空 | report 缺少后续 handoff 必需的证据；message 指明「补带 evidence_path/evidence_hash 的 task.report（新 request_id）后重试」 |
| `E_HANDOFF_REPORT_EVIDENCE_MISSING` | handoff executor_ready_for_review 且最近一条 `reported` event 的 evidence 为空（handoff 侧非空） | 细分自 `E_HANDOFF_REPORT_PROVENANCE_MISMATCH`：原 report 未携带证据，非三元组伪造；message 给出与 report 侧一致的恢复指引 |

保留 `E_HANDOFF_REPORT_PROVENANCE_MISMATCH` 原语义，仅用于真正的三元组不一致（step_id 不匹配或 evidence 值不等但双方均非空），避免破坏既有错误面契约。

### 5.3 report 提前校验时机

插入点：`handle_task_report` 中 L487（remaining/unresolved_failed 计算完成、next_status 确定为 `review`）之后、L506（写 `reported` event）之前。判定条件：

```text
next_status == "review" && (evidence_path.is_empty() || evidence_hash.is_empty())
    → E_REPORT_EVIDENCE_REQUIRED
```

该时机恰好覆盖「任务进入 review 前的最后一次 report」——即后续 handoff provenance 查询（L1345-1351 取最近一条）将引用的那条 event。过程性 report（中间步骤完成、任务回 in_progress）不受影响，成功路径语义不变（成功路径本就应携带证据）。

### 5.4 handoff 错误信息指引增强

在 L1368-1376 分支中先判 `reported_evidence_path.is_empty() || reported_evidence_hash.is_empty()`：

- 是 → `E_HANDOFF_REPORT_EVIDENCE_MISSING`，message 格式：「原 report（request_id=…）未携带 evidence_path/evidence_hash，无法与 handoff 证据对齐；请补带证据的 task.report 生成新 request_id 后重试 handoff」。
- 否 → 维持 `E_HANDOFF_REPORT_PROVENANCE_MISMATCH`。

### 5.5 成功路径语义

- report 带 evidence 且任务进 review：行为不变。
- handoff 三元组一致：行为不变。
- 过程性 report（不进 review）：行为不变。
- 唯一行为变化：空 evidence 的终态 report 从「静默接受、后续死锁」变为「提前拒绝并给出恢复指引」。

### 5.6 测试计划（后续步骤执行）

负面测试：

1. report（终态、进 review）不带 evidence → 断言 `E_REPORT_EVIDENCE_REQUIRED`，且任务状态未推进、无 `reported` event 落库。
2. 预置空证据 `reported` event 的任务，handoff executor_ready_for_review 带非空 evidence → 断言 `E_HANDOFF_REPORT_EVIDENCE_MISSING`，message 含恢复指引。
3. 恢复路径：场景 2 后补带证据 report（新 request_id）→ 断言 handoff 成功。

回归：

4. 过程性 report（中间步骤、任务保持 in_progress）不带 evidence → 仍被接受。
5. 终态 report 带完整 evidence → 行为与 master 一致（handoff 成功）。

### 5.7 风险与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| 既有调用方终态 report 不带 evidence 被新 guard 拒绝 | CLI `task report`、脚本、e2e 中不带 evidence 的终态 report 会失败 | 提前校验的 message 明确恢复动作（补带证据重报）；排查 tests/ 与 deliverables/ 中的 report 调用点并补齐证据参数 |
| 新错误码破坏错误面契约 | 依赖精确错误码的调用方需适配 | 仅新增、不改既有码语义；`E_HANDOFF_REPORT_PROVENANCE_MISMATCH` 保留 |
| 提前校验误伤无 step 的 legacy report | 无 step_id 的 report 默认 next_status="review"，同样被校验 | 该路径与 handoff 引用目标一致，属预期收紧；如需灰度可先仅对带 step_id 的 report 生效 |

回滚：guard 为单一分支（判 next_status + 两字段非空），revert 对应 commit 即可完全回滚；新增错误码不影响既有语义，无需数据迁移。
