# P0-E：Adjudicator 使用 Reviewer Lease 的跨角色委托校验

**父任务：** `T-1787203926824-9f873bfc`  
**任务类型：** governance / 独立修复任务  
**目标：** 修复当前 `validate_lease_for_mutation` 将所有 lease holder 与调用身份强制相等，导致已注册、独立的 Adjudicator 无法在持有 Reviewer 已获取 lease token/fencing 的前提下完成 `task.supersede`、`task.attest_legacy_workspace_binding`、`task.contract_bootstrap` 的矛盾。

> **治理不变量。** Reviewer 取得 reviewer lease，证明其已经独立审阅并授权该治理窗口；Adjudicator 以自身真实、已注册的 `role=adjudicator` identity 执行最终 mutation。因此，Adjudicator 不能伪装成 Reviewer，也不能和 Reviewer 使用同一 `agent_id`、`agent_instance_id` 或 `session_id`。受保护治理方法必须校验两人分离，而非错误地要求 Adjudicator 等于 Reviewer lease holder。

## 1. 最小实现范围

| 层 | 文件 / 函数 | 必须实现 |
|---|---|---|
| Rust lease policy | `rust_ext/src/daemon/task_collab.rs` | 新增明确命名的 `validate_reviewer_lease_for_adjudication`，保留现有 `validate_lease_for_mutation` 的同一身份语义供普通 mutation 使用。新方法需校验 active reviewer lease、token hash、expiry、fencing、lease holder registered/active/reviewer role，以及 holder 与 adjudicator 的 agent/instance/session 三重分离。 |
| Rust governance 调用点 | `rust_ext/src/daemon/task_supersede.rs`、`rust_ext/src/daemon/task_collab.rs` | 仅替换 `task.supersede`、`task.attest_legacy_workspace_binding`、`task.contract_bootstrap` 对 reviewer lease 的验证调用。其他 `task.apply`、`task.close`、普通 claim/report/handoff 绝不改变。 |
| Test | `rust_ext/src/daemon/task_supersede.rs` 或专属治理测试 | 覆盖：不同 registered reviewer/adjudicator 正向；同 agent、同 instance、同 session 逐项拒绝；lease holder 非 reviewer/不活跃拒绝；token/fencing/expiry 拒绝；普通同一身份 validation 保持原语义。 |
| Release | `runtime/current` | release build、daemon ping、一个缺凭证 fail-closed probe；不得以真实任务执行 mutation 作为 smoke。 |

## 2. 禁止范围

不得降低 `task.supersede`、P0-B、P0-C 对完整 identity、证据、workspace authority、operation ledger 的要求；不得让 Adjudicator acquire/release Reviewer lease；不得覆盖既有 reviewer verdict；不得修改 task 状态、binding/capture、schema 或使用直接 SQLite 写入；不得改动 Executor、Reviewer、Adjudicator 三份启动模板的角色边界。

## 3. 后续受控用途

P0-E 经独立 Reviewer PASS 与独立 Adjudicator `ACCEPT → apply → close → COMPLETE` 后，才可由真实 Reviewer 获取对应任务的 reviewer lease，再由独立 Adjudicator 依次调用 P0-C `task.contract_bootstrap`（P0-B、A′ 恢复父任务）、P0-B legacy attestation 和 P0-H `task.supersede`。每步均须独立 request ID 与 evidence manifest/hash。
