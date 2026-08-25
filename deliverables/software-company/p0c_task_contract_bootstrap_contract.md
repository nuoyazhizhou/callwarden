# P0-C：Task Contract bootstrap / publication（A′ 调度前置）

**父任务：** `T-1787203926824-9f873bfc`  
**任务类型：** governance / 独立修复任务  
**目标：** 为已绑定 workspace、但 `task_contract_revisions` 为空的历史或自举任务提供唯一的 daemon-native、append-only、durably idempotent、fail-closed 的 Task Envelope 初始发布能力；解除 `task.next_action` 的 Task Contract 缺失阻断。

> **前置事实。** `task.next_action` 在完成 authority capture 复核后，依次要求：`task_contract_revisions` 存在连续单一 lineage、当前 pending step 有唯一 `task_step_role_contract_bindings`，且其引用的 `role_contract_lineages` / `role_contract_revisions` 可复核。两项 A′ 任务的这三类投影均为空，只有旧 `role_contracts` 表中的展示合同。现有 RPC `task.contract_set` 只更新旧 `role_contracts`，并**不会**写任何 `task_contract_revisions`、`role_contract_lineages` 或 `task_step_role_contract_bindings`，因此不能修复该阻断。

## 1. 新 RPC 合同

新增 protected mutation：`task.contract_bootstrap`。

| 参数 | 约束 |
|---|---|
| `task_id` | 必须存在、已有不可变 `task_workspace_binding`，且尚无任何 `task_contract_revisions` 行。已有任何 revision 一律拒绝，禁止覆盖/新建第二 contract_id。 |
| `workspace_id` / `workspace_instance_id` | 必须显式给出，并与 task binding/capture 的 workspace 一致；capture 必须在请求 instance 链中为当前 revision，复用 `task.next_action` 同等 authority 校验。 |
| `envelope` | 必须是完整 Task Envelope 初始 revision：`contract_id`、`revision=1`、合法 profile、objective、interfaces、allowed_edit_scope、acceptance_clauses、risks、rollback、dependencies。handler 对 canonical payload（排除 `contract_hash`、`created_at`、`created_by`）用现有 envelope 规范进行 SHA-256 复核并自行持久化 hash。 |
| `identity` | 已注册完整四字段身份，严格 `role=adjudicator`。 |
| `lease_token` / `fencing_counter` | 该 **task_id** 的有效 reviewer lease；若 v1 lease 获取路径不依赖 Task Contract，则必须在测试中证明。若不能获取 lease，P0-C 必须保持 fail-closed 并明确返回稳定错误。 |
| `request_id` | Operation Ledger 幂等 key 成员；相同 canonical 参数只读重放，复用 ID 但参数不同拒绝。 |
| `evidence_path` / `evidence_hash` | 任务 bootstrap 的审计依据；缺失拒绝。 |

成功时必须在同一 SQLite transaction 追加：一行 `task_contract_revisions` revision 1、`executor` / `reviewer` / `adjudicator` 三条 `role_contract_lineages` 及其 revision 1、所有 pending/in_progress steps 对 executor revision 1 的唯一 `task_step_role_contract_bindings`、权威 `task_events`（不改变 task status）、action identity audit 与 ledger result。不得写 `UPDATE/DELETE`，不得改 task row、workspace binding/capture、旧 `role_contracts`、step、verdict 或既有新式 revision。

## 2. 允许路径与交付

| 层 | 允许文件 | 必须交付 |
|---|---|---|
| Rust domain | `rust_ext/src/daemon/task_loop/task_contract_bootstrap.rs`、`rust_ext/src/daemon/task_loop/mod.rs` | Envelope 严格解析、canonical hash、binding/capture/identity/lease/evidence 校验；同事务追加 Task Contract、三角色 lineage/revision 和每步 executor binding，再写 ledger/audit。 |
| Rust adapter | `rust_ext/src/daemon/task_collab.rs` | 专用 handler，调用 task-loop domain；不混入 `task.contract_set` 的 Role Contract 语义。 |
| Rust dispatch | `rust_ext/src/daemon/dispatch.rs` | handler shim、精确 method route、Protected Mutation 串行化。 |
| Python thin client / CLI | `server/daemon_client.py`、`cli/main.py` | daemon-only wrapper/`cw task contract-bootstrap`；无 SQLite fallback。 |
| Test | Rust task-loop/daemon tests，Python client/CLI tests | 正向、重放、request mismatch、已有 contract、缺 binding、旧 capture、workspace mismatch、invalid envelope/hash、identity/role/lease/fencing/evidence 拒绝、task status 不变、runtime round trip。 |

## 3. 允许的首批用途

P0-C 经独立 Reviewer PASS 和 Adjudicator `apply → close → COMPLETE` 后，才可作为治理能力用于：

1. `T-1787293818274-1b87b6c4`（P0-B）追加 `design` Task Envelope revision 1、三角色 lineage/revision 1 和五个 executor step bindings；
2. `T-1787293451688-c14b1e44`（A′ 恢复父任务）追加 `design` Task Envelope revision 1、三角色 lineage/revision 1 和两个 executor step bindings；
3. 仅在 P0-B 与 A′ 父的完整 task-level / role / step 合同投影以及 authority capture 均通过 `task.next_action` 后，让独立 Executor 再领取下一步。

## 4. 禁止范围

不得直接 SQL 补插 contract revision；不得把 `task.contract_set` 误称为 Task Contract 发布；不得更新已有 capture/binding；不得在 P0-C 中执行 legacy attestation、S2 supersede、CLI-01 创建、task.apply 或 task.close；不得改变用户已有 Executor 的身份、lease 或任务状态。

## 5. Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立审阅 task.contract_bootstrap 是否只初始化空 Task Contract、是否正确复核 authority/envelope/identity/lease/evidence/ledger，以及全部 runtime proof。
  reason: 现有 task.contract_set 只写 Role Contract，无法解除 task.next_action 对 task_contract_revisions 的 BLOCKED；P0-C 是最小且唯一的正式修复入口。
  independence_requirement: required
```
