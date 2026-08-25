# P0-D：Immutable Binding 与 Workspace Capture Freshness 一致性修复

**父任务：** `T-1787203926824-9f873bfc`  
**任务类型：** governance / 独立修复任务  
**问题：** `task.next_action::verify_capture` 同时声明 task binding 不可变，却要求其 `workspace_capture_id` 必须等于同 instance 的当前 capture revision。任何同稳定身份的 re-attestation 都会推进 capture revision，使已绑定任务在无任何身份变化时永久被误判为 `E_WORKSPACE_AUTHORITY_MISMATCH`。

> 正确语义：`task_workspace_bindings.workspace_capture_id` 是任务创建时的**不可变 provenance snapshot**，不应因同一稳定 identity 的后续 capture revision 而被 UPDATE。验证者必须接受同一连续 capture 链中、registry identity 可重算且与当前 revision identity 一致的历史 capture；只在 binding capture 缺失、链不连续、workspace/instance 不一致、identity 无法重算或最新 capture 的 stable identity 与 binding capture 不同（表示 authority identity 变化）时 fail-closed。

## 1. 允许范围

| 文件 | 修改目标 |
|---|---|
| `rust_ext/src/daemon/task_loop/next_action.rs` | 修正 `verify_capture`：删除“binding capture 必须等于 current capture ID”的错误要求，改为 binding/current 两端 stable identity 同一性校验，保留所有缺失/跨 workspace/instance/identity 重算/链断裂拒绝。 |
| `rust_ext/src/daemon/task_loop/next_action_test.rs` | 增加覆盖 historical binding capture（revision n-1）与 current capture（revision n）同 identity 时可派发、identity changed 时拒绝、旧 capture 缺失时拒绝的测试。 |

## 2. 禁止范围

不得修改 `task_workspace_bindings` 既有行；不得新增 rebind RPC；不得修改 capture/binding schema；不得弱化 identity hash 重算或 workspace instance 解析；不得改 `task.contract_set`、P0-B legacy attestation、supersede、步骤、任务状态或 Executor 身份。

## 3. 验收

1. 历史 binding capture 与当前 capture 处于同 workspace/instance、连续链、相同 `registry_identity_hash` 时，`task.next_action` 可继续进入 Task Contract/step 门禁，不返回 authority mismatch。
2. 最新 capture identity 与 binding capture identity 不同，仍返回 `E_WORKSPACE_AUTHORITY_MISMATCH`。
3. binding capture 缺失、capture 链不连续、workspace/instance mismatch、重算 identity 不一致仍 fail-closed。
4. 相关 Rust 单元测试通过；release runtime 的 `task.next_action` 可针对 A′ 恢复任务越过 authority freshness 阶段（后续若缺 Task Contract，应只报告 Task Contract 阻断）。

## 4. Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立验证 P0-D 没有更新不可变 binding，且仅允许 same-identity capture re-attestation，不会把真正的 workspace identity 变化降级为可派发。
  reason: capture revision 是 append-only provenance 记录，不得反向使既有 immutable binding 失效。
  independence_requirement: required
```
