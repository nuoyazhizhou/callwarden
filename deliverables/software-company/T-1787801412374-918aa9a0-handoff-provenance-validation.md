# T-1787801412374-918aa9a0 验证证据

本证据验证当前任务涉及的五个治理源文件均要求完整、可追溯且绑定当前 task 的 Handoff envelope：

- `AGENTS.md`
- `Callwarden 无人值守循环启动模板：Executor _ Planner v3.md`
- `Callwarden 无人值守循环启动模板：Reviewer v3.md`
- `Callwarden 无人值守循环启动模板：Adjudicator v3.md`
- `.agents/skills/cw-task-loop/SKILL.md`

静态一致性检查确认五个文件均包含以下字段：`task_id`、`from_role`、`outcome`、`next_role`、`next_action`、`reason`、`independence_requirement`、`request_id`、`step_id`、`report_request_id`、`evidence_path`、`evidence_hash`，以及完整 `identity` 字段：`agent_id`、`agent_instance_id`、`session_id`、`model_id`、`role`。

验证结果：

```text
handoff provenance field consistency ok
tokenslim run git diff --check: passed
```

规则同时明确：`task_id` 必须是 Handoff 的首字段；`next_action` 只能描述当前 task 的下一步，不能夹带领取后续任务；缺少 provenance 字段时必须 fail closed。
