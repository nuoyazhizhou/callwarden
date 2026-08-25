# P0-F：治理 Bootstrap Evidence / Review Bridge（解除 A′ 冷启动死锁）

**父任务：** `T-1787203926824-9f873bfc`  
**来源授权：** 用户已明确授权“直接处理” A′ 任务治理阻断。  
**问题：** P0-B/P0-C/P0-D/P0-E 均由 `task.create` 写入 role contracts 和步骤，但没有 Task Contract revision 或 step-role binding。`task.next_action` 因而在任何角色路由前 fail-closed。正常 `task.report`/`task.handoff` 又要求 executor lease 与 verified step binding；正常 Reviewer PASS 又要求正式 Executor handoff。由此形成不能由任一角色自行跨越的冷启动环。

> **这不是 Reviewer 应判 BLOCKED 的任务缺陷。**这是调度器缺少“已绑定任务、完整治理投影为空时，如何把真实执行证据引入首个 immutable Task Envelope”的 bootstrap protocol。

## 1. 新的受保护、窄范围 RPC

新增两个 **daemon-only、append-only、durably idempotent** 的受保护 mutation；均必须经过 `CapabilityMutationGate`、workspace authority、registered identity、operation ledger、evidence path/hash 和 action identity audit。

| RPC | 调用角色 | 前置条件 | 写入 | 明确禁止 |
|---|---|---|---|---|
| `task.bootstrap_executor_evidence` | `executor` | task 已 immutable workspace binding；**Task Contract、role lineage/revision、step binding 均为零**；任务 open/in_progress；所有 pending/in-progress 步骤由调用方提交逐项 completion evidence | 仅追加每项 completed-step evidence event，追加 `bootstrap_executor_ready_for_review` event；任务状态转为 `review` | 不写 Task Contract、role contract、binding、verdict、lease；已有任一治理投影即拒绝 |
| `task.bootstrap_reviewer_pass` | `reviewer` | task 处于 review；存在唯一 bootstrap executor evidence event；Reviewer identity 已注册且与 executor 的 agent/instance/session 三重不同；Reviewer 以专用 bootstrap review lease（由本 RPC 同事务签发）完成只读核验 | 仅追加 `bootstrap_reviewer_pass` verdict-equivalent event 和 immutable reviewer evidence；不改变 task contract | 不写普通 verdict、Task Contract、step binding、apply/close；无 executor evidence 或任何既有 projection 一律拒绝 |

### 1.1 受控衔接

`task.contract_bootstrap`（P0-C）唯一放宽：仅当任务存在 P0-F 的有效 `bootstrap_reviewer_pass` event 时，允许 **Adjudicator** 使用该事件的 reviewer lease/fencing 完成 Task/Role/step contracts 的首次追加。其余 P0-C authority、identity、evidence、ledger、empty-projection 和 append-only 门禁保持不变。

P0-C 成功后，任务将拥有标准 Task Contract、三角色 lineage/revision 和 executor step bindings；之后所有 lifecycle 必须回到正常 `task.next_action → claim → report → handoff → reviewer verdict → adjudicator apply/close` 路径。P0-F 不能成为常规捷径。

## 2. 最小文件范围

| 文件 | 修改范围 |
|---|---|
| `rust_ext/src/daemon/task_loop/bootstrap_review_bridge.rs` | bridge domain、empty-projection 与 evidence event 校验、严格 replay/拒绝规则 |
| `rust_ext/src/daemon/task_loop/mod.rs` | 模块注册和定向测试模块 |
| `rust_ext/src/daemon/task_collab.rs` | two daemon handlers，复用身份/authority/ledger/clock；专用 bootstrap reviewer lease 签发和读取 |
| `rust_ext/src/daemon/dispatch.rs` | 仅两个 protected mutation route |
| `rust_ext/src/daemon/task_loop/operation_store.rs` | 两个方法进入 task ledger scope |
| `server/daemon_client.py` / `cli/main.py` | daemon-only wrappers；无本地 SQLite fallback |
| 定向 Rust tests | 正向两阶段桥接；任何既有 Task/Role/step projection 拒绝；非 executor/reviewer、身份复用、证据缺失、重复/冲突 request ID、直接 contract bootstrap、未完成步骤、错误状态全部拒绝 |

## 3. Root bootstrap release 与事后独立审计

P0-F 是修复冷启动死锁的**唯一 root bootstrap**。它可在当前用户明确授权下由 Executor 实现、测试、发布，但不得被同一 Executor 自行判 PASS 或关闭。发布后：

1. Executor 使用 `task.bootstrap_executor_evidence` 把 P0-F 的真实测试/发布证据追加到 P0-F；
2. 独立 Reviewer 使用 `task.bootstrap_reviewer_pass` 进行只读审阅；
3. 独立 Adjudicator 调用 P0-C 完成 P0-F 的标准合同首次发布；
4. P0-F 回到正常 apply → close → COMPLETE；
5. 然后 P0-F bridge 才用于 P0-D、P0-C、P0-E（按最小风险顺序）恢复它们的标准审阅链。

## 4. 禁止范围

禁止数据库直写、schema 修改、静默 task status 改写、历史 verdict 覆盖、任何普通任务使用 bridge、任何已存在 Task/Role/step projection 的任务使用 bridge、用 description 中的 Handoff 文本替代结构化 event、Adjudicator 伪装 Reviewer、Reviewer 伪装 Executor、Executor apply/close。

## 5. Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立核验 P0-F 是否只在“完整治理投影为空”的 root bootstrap 边界内追加真实 Executor evidence；验证所有扩大范围和身份复用路径均 fail-closed。
  reason: 没有该 bridge，P0-C 及其他 A′ 治理任务无法获得其首个可审核的 Task Contract/step binding 投影。
  independence_requirement: required
```
