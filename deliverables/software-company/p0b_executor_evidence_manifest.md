# P0-B Executor Evidence Manifest

**任务：** `T-1787293818274-1b87b6c4`  
**任务标题：** P0-B：历史任务 workspace authority attestation / binding（supersede 前置）  
**Executor 工作模式：** implementer / evidence  
**交付状态：** `executor_ready_for_review`（**未 apply、未 close**）  
**生成时间：** 2026-08-21（Windows authority runtime）

> 本证据仅证明 P0-B 的受控实现、测试和发布已就绪。它**不**证明旧 S2 已被 supersede；它也不替代独立 Reviewer 的 PASS 与 Adjudicator 的 `apply → close → COMPLETE` 收尾。

## 1. 实现范围与安全性质

| 组件 | P0-B 实现 | 核心约束 |
|---|---|---|
| `task_collab.rs` | 将既有 `bind_task_to_workspace` 提升为 `pub(crate)` 受限事务复用点 | 沿用 `task.create` 的 workspace 存在性、capture rule、稳定身份与不可变 binding 语义；不复制或绕过 SQL。 |
| `task_supersede.rs` | 新增 `TaskCollabStore::handle_task_attest_legacy_workspace_binding` 和稳定错误码 | 仅针对**无 binding 的历史任务**；要求已绑定 anchor、显式 authority、registered adjudicator identity、anchor reviewer lease/fencing、request ID、evidence；同事务写 capture、binding、task event、identity event 与 operation ledger。 |
| `dispatch.rs` | 注册 `task.attest_legacy_workspace_binding` handler、RPC 分支与 protected mutation 分类 | 所有调用经过 daemon 的唯一 serialization point。 |
| `operation_store.rs` | 将 P0-B 方法纳入固定 task-domain ledger scope | 同 request/canonical 参数只读重放；不能将重试变成第二个 binding/event。 |
| `daemon_client.py` | 新增 daemon-native thin wrapper | 无本地 SQLite fallback。 |
| `cli/main.py` | 新增 `cw task attest-legacy-workspace-binding` | 强制显式 authority、identity、lease/fencing 与 evidence；daemon 不可用时 fail-closed。 |

P0-B **未修改 schema**、未直接向生产 DB 补写、未改动旧 S2 的标题/描述/status/verdict/close 字段、未执行 `task.supersede`、`task.apply` 或 `task.close`。

## 2. Rust 测试结果

命令（经项目命令包装器执行）：

```text
tokenslim run cargo test --manifest-path rust_ext\Cargo.toml legacy_workspace_attestation
```

| 用例 | 结果 | 断言重点 |
|---|---:|---|
| `test_legacy_workspace_attestation_appends_binding_without_task_mutation` | PASS | 成功添加 binding/audit/ledger，legacy task status 仍为 `open`。 |
| `test_legacy_workspace_attestation_replays_without_duplicate_binding_or_audit` | PASS | 相同 request 重放不新增 binding 或 audit。 |
| `test_legacy_workspace_attestation_rejects_already_bound_wrong_role_and_stale_fencing` | PASS | 已绑定、非 adjudicator、过期 fencing 均拒绝。 |
| `test_legacy_workspace_attestation_rejects_unbound_anchor_missing_evidence_and_instance_mismatch` | PASS | anchor 无 binding、证据缺失、instance 不匹配均拒绝且不绑定 legacy task。 |

测试摘要：**4 passed / 0 failed**；完整输出见 `p0b_rust_test.log`。

## 3. CLI 与运行时验证

| 验证 | 结论 | 证据 |
|---|---|---|
| Python 语法 | PASS | `C:\Python314\python.exe -m py_compile cli\main.py server\daemon_client.py` 退出码 0。 |
| CLI 参数门禁 | PASS | `cw task attest-legacy-workspace-binding --help` 列出 `workspace-id`、`workspace-instance-id`、request/evidence、lease/fencing 与四字段 adjudicator identity。 |
| Release 构建与部署 | PASS | 受控 `refresh_shared_runtime.ps1` 成功；未回滚。 |
| Runtime daemon | PASS | `runtime/current/cw-daemon.exe`，PID `17808`，SHA-256 `9011eb073532561e55582e766657ebd4678d4ecb841840e40b7d25bad65af1dd`。 |
| Daemon ping/smoke | PASS | `cw.py daemon ping`、`cw.py --version` 均退出码 0；版本 `0.3.23`。 |
| 新 RPC runtime 路由 | PASS（fail-closed probe） | 用空 `legacy_task_id` 调用新 CLI，运行 daemon 返回 `E_LEGACY_BIND_TASK_NOT_FOUND`。该校验发生在 ledger/binding 前，未创建任何 authority 数据。 |

Release 版本：`20260821-144820-b345b9a3d656-ee12f55a`。完整 release evidence 见 `p0b_runtime_release_evidence.json`。

## 4. 关键工件哈希

| 工件 | SHA-256 |
|---|---|
| `rust_ext/src/daemon/task_supersede.rs` | `AA9A9A7134A48322408CFD709B12C1BF1D3ADC836A0879966375C241A9E753D5` |
| `rust_ext/src/daemon/task_collab.rs` | `050B5EE4F9DBF94808D96E0E7DD0949F96CE6BBC27D00EED30477C116F5FB851` |
| `rust_ext/src/daemon/dispatch.rs` | `F702A2D34D7115D76EA554F12C2C7D0E7586D33F3E2A2B9050FA56814EA0BBA9` |
| `rust_ext/src/daemon/task_loop/operation_store.rs` | `A1C857C64D08581D02895496D42DC2B5CE613CBAEBF01BF08920074BFB625561` |
| `server/daemon_client.py` | `C408241F52A760ABAD6ABBADFC170ED69FC75FAEC03D82A37CFC4FF37FC874E6` |
| `cli/main.py` | `5E6F6B5BA7F196CFDA57F09EB6C7C7C0C3BF629F23EDA5AA9ECD0B106E936A9C` |
| Rust 测试日志 | `0F1E3DF25AEDEB0F31739889480E4CD8270B14EC6C9B0B4ACE32516C6EDD0206` |
| Runtime release evidence | `D0DC364DF99F24B1DF1C6B0849C7BFDB2D1CDE771D155ABAC4A34D160282B5D4` |
| A′ supersede baseline manifest | `705A7D420E3F33BF6494E424121A3CE10760549AA136E800834872227D750786` |

完整散列清单：`p0b_evidence_hashes.txt`。

## 5. Reviewer 独立核验门禁

Reviewer 必须独立验证下列事项后才可提交 PASS 或 BLOCKED：

1. P0-B handler 不允许已有 binding 的 task 进入重绑定路径，且没有 `UPDATE/DELETE` 旧 task/binding/capture。
2. authority 仅由已绑定 anchor 的 immutable binding/capture 派生；请求 `workspace_id` 与 `workspace_instance_id` 均要匹配 anchor。
3. identity 必须是 registered 的 `adjudicator`；lease 是 **anchor task** 的 active reviewer lease，fencing counter 必须当前有效。
4. 所有成功写入（capture、binding、task event、identity event、ledger）同一事务；重放和确定性拒绝遵守 ledger 合同。
5. 新方法已在 dispatch protected mutation、operation ledger scope、daemon client 与 CLI daemon-only 路径中连通。
6. P0-B runtime build evidence 与当前 daemon PID/hash 一致；fail-closed probe 没有对生产数据造成写入。

## 6. Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立核验 P0-B 的 authority/bootstrap 边界、append-only 与事务幂等语义、四项 Rust 测试、release evidence、CLI daemon-only 路径；仅输出 PASS 或 BLOCKED。
  reason: P0-B 已解决 S2-original 无 workspace binding 的正式能力缺口，但不得由 executor 自行认可、apply、close 或代替后续两个 S2 的 supersede。
  independence_requirement: required
```

若 Reviewer PASS，独立 Adjudicator 必须对 **P0-B 本身** 取得真实 reviewer lease/fencing 后执行：`ACCEPT → apply → close → task.next_action=COMPLETE`。只有 P0-B 完成该闭环，才可针对 `T-1787203937201-0a156564` 发起其独立的 legacy workspace attestation，并随后分别 supersede 两个旧 S2。
