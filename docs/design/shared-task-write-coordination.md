# 共享任务写入协调契约

## 目标

同一 Windows 用户下的多个 CLI、MCP 和 Agent 进程共享一份
`~/.callwarden/callwarden.db` 时，所有任务状态写操作使用同一个 daemon 单写点。
避免多个 Python 进程分别打开 SQLite，造成锁冲突、旧状态读取和半完成审计链。

## 写入策略

`route_task_write()` 对 `task.*` 写操作采用以下规则：

| 模式 | 默认行为 | 说明 |
|---|---|---|
| `enterprise` | daemon，失败即报错 | 生产/企业模式，禁止本地回退 |
| `auto` | daemon，失败即报错 | 共享 Agent 默认路径，禁止本地回退 |
| `local` + `CW_TASK_WRITE_POLICY=shared` | 拒绝本地 task 写入 | 防止 Agent 误用 local 绕过单写点 |
| `local` + `CW_TASK_WRITE_POLICY=isolated` | 允许本地写入 | 仅单进程测试、离线迁移或显式维护窗口 |

默认 `CW_TASK_WRITE_POLICY=shared`。该变量只控制 task 写操作，普通本地查询不受影响。

错误必须是结构化的 `E_SHARED_TASK_WRITER_REQUIRED`，并明确提示启动/连接当前用户 daemon；不能执行 fallback，也不能把 daemon 失败伪装成本地成功。

## 事务与证据

daemon 侧的 `task.report` 必须在一个 SQLite 事务中完成：

1. 校验 request_id、task/step 所属关系、claim/session/identity；
2. 更新 `task_steps` 和 `tasks`；
3. 写入 `task_events`，递增全局 `monotonic_seq`；
4. 写入可提供的 `evidence_path/evidence_hash`；
5. 全部成功后一次 COMMIT，任何失败全部 ROLLBACK。

`change_audit` 只有在请求携带真实代码变更时写入；evidence-only 任务不得伪造源码 diff。

## 失败与并发语义

- daemon 不可用：共享模式 fail-closed，不打开本地 fallback。
- 多个 Agent 同时 claim：只有一个成功，其余返回 `task_conflict`。
- 同一 `request_id` 重试：返回已提交结果，不重复写 event/audit。
- 业务错误必须原样透传，不能包装成“连接失败”。
- Reviewer/Coordinator 读取状态时必须确认 task status、step status、task_events 和证据绑定来自同一权威库。

## 验收标准

1. 两个或更多并发 CLI/MCP 写请求都不会触发 Python 本地直写。
2. daemon 可用时，所有写入都能在同一权威库查询到。
3. daemon 不可用且 policy=shared 时，fallback 函数调用次数为 0，并返回
   `E_SHARED_TASK_WRITER_REQUIRED`。
4. policy=isolated 仅用于明确的单进程测试，现有 local fallback 行为保留。
5. 并发 claim 只有一个胜者，失败者收到 `task_conflict`。
6. report 后 `task_steps`、`tasks`、`task_events` 的状态一致；任一事务失败不留下半条记录。
7. focused Python 测试、Rust `task_collab` 测试和 Windows daemon writer E2E 通过。

## 状态边界

本契约不处理 snapshot、CAS、G0 盲评或跨机器分布式锁。跨用户/跨主机部署另行设计。

