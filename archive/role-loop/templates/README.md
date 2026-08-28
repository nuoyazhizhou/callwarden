# 角色循环模板归档

本目录保留旧模板，供历史任务复核和 provenance 追溯使用；它们不是新任务的启动入口，不能覆盖当前 `AGENTS.md`、Role Contract 或 daemon 投影。

| 旧模板 | 替代入口 |
|---|---|
| `Executor _ Planner v1/v2/v3` | `Callwarden 无人值守循环启动模板：Planner v1.md` + `Callwarden 无人值守循环启动模板：Executor v4.md` |
| `Reviewer v1/v2/v3` | `Callwarden 无人值守循环启动模板：Reviewer v4.md` |
| `Adjudicator v1/v2/v3` | `Callwarden 无人值守循环启动模板：Adjudicator v4.md` |

归档原因：旧版本将 Planner 混入 Executor，或缺少精确 `task_id`、结构化 finding、相邻缺陷、decision request、
远端/后台控制台和完整 provenance 约束。归档不代表历史任务、verdict 或 evidence 被删除。

## 字节级原件纪律（append-only provenance）

归档文件必须是**原样字节副本**（blob id 可用 `git hash-object` 复核），迁移时禁止删除段落、改写格式或
"顺手清理"。曾发生违规：`Executor _ Planner v3` 在迁移提交 `e70b0b7` 中被改写（原始 blob
`8ba6501fb8c1ba30dad66729997584133df62fd0` → 改写后 `fd03368d0537b2b93be3aa630c448fdc7c612ece`，
删除了旧 Handoff 段并调整格式）；已由任务 `T-1787888909289-881595e0` 恢复为原始 blob
`8ba6501f`。`scripts/validate_template_compliance.py` 会复核该归档 blob id，防止再次改写。

| 归档文件 | 原始 blob id |
|---|---|
| `Callwarden 无人值守循环启动模板：Executor _ Planner v3.md` | `8ba6501fb8c1ba30dad66729997584133df62fd0` |
