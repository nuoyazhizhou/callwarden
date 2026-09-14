# exec7 step2 deploy 证据：daemon governance_projection 权威状态投影（no-delta）

- Task: `T-1787799894830-3cd93b18` 补齐 daemon governance_projection 权威状态投影
- Step: `S-1787799894831-3cebe088` deploy（target: scripts/refresh_shared_runtime.ps1）
- Executor 结论：**no-delta**。运行中共享 runtime（runtime/current/cw-daemon.exe）已由
  `refresh_shared_runtime.ps1` 产物刷新并包含 exec7 要求的 governance_projection 实现，
  与 HEAD 源码零差异；daemon 已 live 服务 `task.governance_projection.get`，无需再次切换。

## 1. 部署事实（runtime/current）

| 项 | 值 | 说明 |
|---|---|---|
| 运行 daemon | PID 25816 | http://127.0.0.1:13177 |
| 可执行文件 | `C:\Users\wanpi\.callwarden\runtime\current\cw-daemon.exe` | 启动于 8/9 10:17:37 |
| 产物构建 | 8/9 01:46:48–01:48:27 | `runtime/current` 各二进制同批刷新 |
| runtime 版本 | `20260908-013512-9057bc247685-713bf641` | refresh 脚本版本号 = git_head 9057bc2 + 时间戳 + runid |
| 部署方式 | `scripts/refresh_shared_runtime.ps1` | 构建并原子替换 runtime/current + daemon 探针去重重启 |

## 2. 与 HEAD 的源码一致性（部署无差量）

- `git diff --stat 9057bc2..HEAD -- task_collab_contract.rs task_collab_query.rs
  task_loop/next_action.rs dispatch.rs` → 空（exit 0）。
- 即：运行中 daemon 二进制所含 gp 实现与 HEAD `98dc77c` 完全一致；9057bc2..HEAD
  之间仅追加治理记录（ledger），不触碰投影源码。

## 3. live 验证（部署后生效）

对任务本身调用 `task.governance_projection.get`（daemon PID 25816）返回完整权威投影：

```json
{
  "status": "in_progress", "lifecycle_status": "in_progress",
  "workflow_status": "execution_in_progress", "current_role": "executor",
  "next_role": null, "next_action": "wait_for_current_lease",
  "review": {"state": "not_in_review"},
  "blocking_reasons": ["task 存在 active 未过期 lease（持有角色 executor），等待其释放"],
  "required_role": "executor", "step_id": "S-1787799894831-3cebe088",
  "identity_policy_status": "declared"
}
```

即部署态已具备任务验收的全部字段通道（顶层扁平 + governance 嵌套）。

## 4. 结论

共享 runtime 当前部署产物即含 gp 实现且与 HEAD 无源码差量；live RPC 生效。
deploy step 无差量，验收通过。如需复核，可用
`& scripts\refresh_shared_runtime.ps1 -TaskId T-1787799894830-3cd93b18` 触发等价重刷。
