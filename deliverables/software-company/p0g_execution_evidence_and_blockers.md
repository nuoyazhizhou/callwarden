# P0-G：治理修复实施证据与受控阻断说明

**任务：** `T-1787367417246-34190890`  
**父任务：** `T-1787293451688-c14b1e44`  
**工作区：** `workspace_id=1`、`workspace_instance_id=ws-1`  
**日期：** 2026-08-22（本机 Windows authority）

> 本轮仅实施 P0-G 代码、测试、daemon 发布、HTTP fail-closed 探针与只读审计。未直接写 SQLite；未修改既有 Task Contract revision；未伪造 executor/reviewer/adjudicator identity；未领取或释放任何遗留 lease；未 claim/report/handoff/apply/close 任一 A′ 迁移任务。

## 已完成的实现

| 层 | 实现结果 | 关键约束 |
|---|---|---|
| `task.contract_revise` domain | 新增 append-only revision `n+1` 领域实现 | 旧 revision/hash 锚定、连续 revision、不可改变 lineage、严格 JSON array、canonical hash 与 operation ledger 语义 |
| daemon handler / dispatch | `task.contract_revise` 已注册为受保护 mutation 并通过 `DaemonStateExt` 转发 | 仅 adjudicator；完整 identity；Reviewer lease token/fencing；agent/instance/session 三重分离 |
| `task.create` | 新任务在同一 transaction 原子写 task、binding、steps、Task Contract revision-1、三角色 lineage/revision、executor step binding 和 created event | 缺 envelope、steps、三角色或 nested JSON strings 时 fail-closed；任一投影失败整体回滚 |
| Python thin client | IPC 与实际 `HttpDaemonRpcClient` 均增加 `task_contract_revise` 和 atomic `task_create` envelope 透传；HTTP client 增加正式 `lease_release` 转发 | 无 SQL fallback；业务/identity/lease 判断均留在 Rust daemon |

## 定向测试和运行时证据

| 检查 | 结果 | 证据 |
|---|---:|---|
| revision-2 连续性、hash 冲突、必填字段与结构化 array | 5 passed | `p0g_task_contract_revise_test.log` |
| adjudicator 缺 `agent_instance_id` 的 handler 门禁 | 1 passed | `p0g_handler_identity_test.log` |
| Reviewer/Adjudicator agent、instance、session 三重分离与 token/fencing | 1 passed | `p0g_lease_identity_matrix_test.log` |
| task.create 原子完整投影与失败零残留 | 2 passed | `p0g_atomic_create_test.log` |
| Rust daemon 编译检查 | passed | `p0g_cargo_check.log` |
| Python thin client 语法检查 | passed | 本轮执行记录；`server/daemon_client.py` 已以 Python 3.14 编译 |
| debug daemon build | passed | `p0g_cargo_build.log` |
| HTTP health | `127.0.0.1:14012`、PID `14400` | `p0g_http_daemon_health.json` |
| 缺 `expected_previous_hash` HTTP 探针 | 正确拒绝 `E_TASK_CONTRACT_REVISE_PREVIOUS_HASH_REQUIRED` | `p0g_http_fail_closed_probe.json` |

## 只读修复审计

| 项目 | 观察值 | 处理边界 |
|---|---:|---|
| Task Contract revisions | 188 | 不删除、不 UPDATE、不覆盖 revision-1 |
| placeholder revision-1 | 185 | 已生成逐卡 revision-2 准备队列；禁止自动伪造语义 |
| reviewer-wb-186loop active reviewer leases | 187 | 仅能通过原 raw token 的 `lease.release`，或既有 TTL/stale-holder 正式回收；禁止直接改 SQLite |
| 已到期 lease | 1 | `lease.status` 仍显示 `active`；当前未发现单独的 reap/recover RPC，且无原 token，未执行写入 |
| 尚未到期 lease | 186 | 最晚 `expires_at=1787451170`；不能提前释放或伪造 token |

对应只读附件为：

- `p0g_revision2_repair_manifest.md`：185 张 placeholder 合同逐卡 revision-2 准备清单。
- `p0g_lease_recovery_readiness.json`：187 个遗留 reviewer lease 的 TTL/回收就绪度。
- `p0g_http_lease_status_probe.json`：已到期样本仍为 active 的 daemon 只读观察。

## 未跨越的阻断与下一步

当前 `task.next_action(T-1787367417246-34190890)` 返回 `BLOCKED`，原因是 **P0-G 任务自身缺少 Task Contract revision**，见 `p0g_http_next_action_probe.json`。这不是代码实现失败，而是旧式预原子建卡留下的治理元数据缺口。

P0-G 的 Task Contract 初始发布或 revision-2 追加都是受保护治理 mutation；执行需要一个真实已注册、独立的 reviewer 持有有效 lease，以及与其 agent/instance/session 三重分离的真实 adjudicator identity、request evidence 和 fencing counter。当前会话不具备可证明的这组身份，且不得凭空构造。因此未调用 `task.contract_bootstrap`、`task.contract_revise` 或 `lease.release` 写真实任务。

此外，严格的 future `task.create` 门禁使历史测试 fixture（例如 `test_task_collab_full_lifecycle`）因缺少 envelope/三角色合同而按预期 fail-closed。生产门禁符合 P0-G 合同，但历史测试与遗留创建调用方需要在独立迁移中补齐真实 envelope 和三角色 Role Contract，不能以降低新门禁来恢复绿灯。

```text
Handoff:
  from_role: executor
  outcome: executor_blocked_to_reviewer
  next_role: reviewer
  next_action: 独立核验本报告列出的代码 diff、四组定向测试、HTTP fail-closed probe、Task Contract/lease 只读审计；确认不得在缺少真实独立 identity/Reviewer lease 时对 P0-G 或 185 张任务执行治理 mutation。
  reason: P0-G 实现与发布证据已完成；真实任务交接仍被 P0-G 本身缺 Task Contract 和遗留 lease token 不可得所阻断。
  independence_requirement: required
```

> 上述 handoff 是**文件化交接草案**，并未作为 `task.handoff` 写入 daemon；这样避免把没有真实 executor identity/lease 的状态伪装为正式治理事件。
