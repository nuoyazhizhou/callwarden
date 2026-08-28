# T-1787850432491-f42a2b8c remediation binding 修复证据

日期：2026-08-28

## 修复内容

- `task.report(success=false)` 自动追加的 `fix_defect` step，在同一事务内绑定当前任务的 Executor Role Contract。
- `task.remediation.create` 显式追加的 `fix_defect` step，在同一事务内绑定当前任务的 Executor Role Contract。
- `task.handoff(outcome=reviewer_blocked)` task-level remediation，在同一事务内绑定当前任务的 Executor Role Contract。
- 绑定 helper 只接受当前任务的不可变 workspace binding、连续的 Executor revision 链和当前 step；缺失或不一致时返回确定性错误，使外层事务回滚，避免留下“有步骤、无 binding”的治理孤儿。
- bootstrap 允许零 pending/in_progress 初始步骤，以支持 task-level remediation 在后续追加时完成绑定。
- report 的合同校验统一 legacy runtime role 到治理层 Executor role，兼容 `implementer` 等 Executor 工作模式。

## 验证

在 `C:\git_work\callwarden\rust_ext` 执行：

- `tokenslim run cargo check --quiet`：通过（仅已有 warning）。
- `tokenslim run cargo test test_failed_report_preserves_scope_and_requires_remediation_claim`：通过。
- `tokenslim run cargo test test_explicit_remediation_create_binds_failed_step_and_resolves`：通过。
- `tokenslim run cargo test test_step_resolution_is_idempotent_and_keeps_failed_history_immutable`：通过。
- `tokenslim run cargo test test_reviewer_blocked_reopens_same_task_for_multiple_revision_rounds`：通过。
- `tokenslim run cargo test task_collab::tests:: --quiet`：86 passed；3 个既有失败为完整生命周期事件计数、v46→v50 迁移计数和 stale-claim recovery，均未涉及本次 binding 修复。
- `tokenslim run git diff --check`：通过。

## 环境限制

提交前执行 `python C:/git_work/callwarden/cw.py --refresh-all`，当前 daemon 返回：

```text
method_not_found: 未知方法: build_full_graph
```

因此本轮未使用 SQLite fallback，数据库刷新/部署证明仍需 daemon 补齐 `build_full_graph` 后重新执行。

代码修复提交：`382b5b5`。
