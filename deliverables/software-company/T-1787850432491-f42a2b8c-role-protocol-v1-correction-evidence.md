# T-1787850432491-f42a2b8c：角色协议 v1 回补证据

## 回补内容

- 在共享 `role-protocol.md` 增加实现状态声明：Planner/replanning、decision request、自动 fix_defect 和 related_to 当前尚未由 daemon 完整 emit；
- 将 `workflow_status` 17 项唯一枚举集中到共享协议，AGENTS/task-loop 不再维护独立枚举列表；
- 下沉 Executor 的白名单 add、task_id commit、rev-parse、commit ledger、commit 后 refresh 纪律；
- 下沉 Reviewer 的 `agent-instance-id`、lease acquire/release 和 `E_IDENTITY_INSTANCE_MISMATCH`；
- 下沉 Adjudicator 的 reviewer lease、lease token/fencing、apply/close/release 纪律；
- 统一 finding 字段：`introduced_by_change`、`call_chain`、`impact_radius`、root cause、reproduction、owner、blocking；
- 四份 v4/v1 模板只引用共享 Handoff 协议，不重复完整结构化字段；Planner 不再使用 Executor 的 `executor_blocked_to_user` outcome；
- 更新冻结设计文档的角色修订说明，保留其历史基线并明确 daemon capability 尚未完整实现；
- 校验器增加 protocol/design 输入、唯一枚举、命令陷阱、finding schema 和模板禁止内联 Handoff 检查。

## 验证

```text
模板合规检查通过: 4 个角色模板
python -m py_compile scripts/validate_template_compliance.py: pass
git diff --check: pass
```

## 未实施能力

本轮没有修改 daemon、CLI 或 MCP；`decision_request` 落库/响应、Planner 原生派工、自动 `fix_defect` 和
`adjacent_defect → related_to` 仍必须作为后续 daemon 任务实现，当前客户端不会自行合成对应状态。
