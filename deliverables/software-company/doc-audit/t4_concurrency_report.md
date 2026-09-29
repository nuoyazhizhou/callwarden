# T4 实测报告:多用户/多 workspace/多 agent 并发正确性

日期:2026-09-30
环境:Windows,Python 3.14,隔离 HTTP daemon(release 二进制 + 临时 data root,不触碰生产 daemon)

## 1. 测试目标与方法

验证 Call Warden 在**多 workspace 隔离**与**多 agent 同 workspace 协同**下的并发正确性。M3(既有 `test_m3_concurrent_writes`)已覆盖单 workspace 内一致性,T4 补充隔离性与多 agent 协同。

**方法**:多线程各持独立 `HttpDaemonRpcClient`(模拟独立 agent 进程)打同一隔离 daemon,所有断言基于真实 daemon 响应。

## 2. 结果汇总

| 套件 | 测试数 | 结果 |
| --- | --- | --- |
| T4(本轮新增) | 6 | 全 PASS(1.80s) |
| M3(既有,顺带验证) | 8 | 全 PASS |
| **并发合计** | **14** | **全绿,无缺陷** |

## 3. T4 覆盖场景(6)

### 多 workspace 隔离(1)
- `test_tasks_isolated_between_workspaces`:两个独立 workspace(A/B)并发各创建 3 个 task,两组 task_id **完全不相交**;A workspace 的 `task.list` 不泄漏 B 的 task。**隔离性成立**。

### 并发 register 隔离(2)
- `test_distinct_roots_get_distinct_instances`:6 个不同 root 并发 register → 6 个不同 workspace_instance_id,**无碰撞**。
- `test_same_root_idempotent_instance`:同一 root 并发 5 次 register → 幂等,得**同一 instance_id**。

### 多 agent 同 workspace 协同(codex + kiro)(2)
- `test_two_agents_lease_contention`:codex 与 kiro 并发争用同 task 的 implementer lease → **恰好 1 winner**,败方收结构化 `E_LEASE_ACTIVE_EXISTS`/`E_LEASE_HOLDER_MISMATCH`(非连接错误)。
- `test_read_not_blocked_by_write_lease`:codex 持 implementer lease 时,kiro 并发只读(lease.status × 5 × 3 线程)**不被写 lease 阻塞**,全部正常返回。

### 跨 workspace lease 独立(1)
- `test_lease_in_a_does_not_block_b`:A workspace 某 task 持 implementer lease,**不影响** B workspace 另一 task 同 role acquire(B 成功得 counter=1)。

## 4. M3 既有覆盖(8,顺带验证全绿)

并发 task.create 同 title(无丢失更新)、N=8 并发 create 无误冲突、request_id 幂等 dedup(2)、lease 争用单 winner、lease fencing 旧 token 拒绝、并发 apply 同 lease 串行化、混合读写并发无死锁。

## 5. 正确性结论

- **workspace 隔离**:不同 workspace 的 task/lease 完全隔离,无串扰;instance_id 由 daemon 按 root 确定性派发,并发无碰撞、同 root 幂等。
- **多 agent 协同**:同 workspace 多 agent(codex/kiro)争用经 daemon SerializationPoint 串行化,恰好单 winner,败方得结构化 lease 冲突;读写隔离,读不被写 lease 阻塞。
- **跨 workspace 独立**:lease 按 (workspace, task, role) 隔离,A 的 lease 不阻塞 B。
- **无死锁/无误冲突/无丢失更新**:14 个并发测试全部在超时内完成,失败均为结构化 `E_*` 业务错误(非连接错误/崩溃)。

**未发现缺陷。**

## 6. 交付(tests/convergence/)

- `test_t4_multi_workspace_agent.py`:T4 并发测试(6 个,复用 conftest 的 isolated_http_daemon + _ensure_task_db_workspace)
- 复用既有 `test_m3_concurrent_writes.py`(8 个单 workspace 并发)

## 7. 运行说明

隔离收敛套件(M3/T4)需在**生产 daemon 停止时**运行(daemon 是 SID 级单实例锁)。本轮为运行 T4 临时停生产 daemon(PID 44584),测完用 `cw daemon start` 恢复(新 PID 7108 @ 端口 11867,git_commit 40d25fc,健康)。生产 daemon 运行时,套件会因隔离 daemon 无法启动而 skip(conftest 已处理 E_DAEMON_ALREADY_RUNNING → skip)。
