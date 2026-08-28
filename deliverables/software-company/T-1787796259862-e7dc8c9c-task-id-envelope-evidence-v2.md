# T-1787796259862-e7dc8c9c task_id 交接绑定证据 v2

## 范围

本次规范变更涉及以下五个目标入口：

- `AGENTS.md`
- `Callwarden 无人值守循环启动模板：Adjudicator v3.md`
- `Callwarden 无人值守循环启动模板：Reviewer v3.md`
- `Callwarden 无人值守循环启动模板：Executor _ Planner v3.md`
- `.agents/skills/cw-task-loop/SKILL.md`

## 验收

执行静态内容检查，结果全部 PASS：

- `AGENTS.md` 的强制 Handoff envelope 增加一级 `task_id`、`step_id`，并明确禁止用父任务、聊天上下文或 request ID 替代。
- Executor 紧凑循环明确从 `next-action` 保存精确 `task_id`，并贯穿 claim/report/handoff/证据/commit message。
- Reviewer 紧凑循环明确以精确 `task_id` 作为 review/verdict/handoff 对象，并要求先按该 ID 查询。
- Adjudicator 紧凑循环明确以精确 `task_id` 贯穿复审、verdict、lease、apply/close 和 finalization。
- `cw-task-loop` skill 增加 Task Binding and Handoff 规则，缺少 `task_id` 时 fail-closed。
- `tokenslim run git diff --check` 通过；无 whitespace error。
- 提交前按项目纪律执行 `tokenslim run python cw.py --refresh-all`，因当前 daemon 未提供 `build_full_graph` RPC 返回 `method_not_found`；错误已记录到 `.trae-cn/memory/tool_errors.log`，未使用 SQLite 或其他旁路刷新。

## 结论

模板、项目总规范和 task-loop skill 现在都要求交接方显式携带当前 `task_id`，下游角色不得从 Epic、step、request 或聊天上下文推断复核对象。
