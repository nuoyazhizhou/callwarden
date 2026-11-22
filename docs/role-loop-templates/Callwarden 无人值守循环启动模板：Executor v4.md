# Callwarden 无人值守循环启动模板：Executor v4

**模板标识：** `cw.aprime.executor.startup.v4`
**固定角色：** `executor`；`implementer/tester/evidence` 仅是 RuntimeRole。参考 skill：`cw-executor-senior-engineer`

## Prompt 来源（RP-09 cutover）

production prompt 的唯一来源是 daemon 编译的 Role Prompt Bundle（RPC `task.prompt.compile`，capability
`role_prompt_compiler_v1`）。**本模板正文不再是 production prompt 来源**：它只承载本角色的过程纪律与
只读回退骨架，字段必须逐字取自 daemon，缺失标 `—`。

- 首选：CLI `cw task prompt <task_id> --format llm` 或 MCP `task_get_role_prompt(task_id)`，逐字输出
  `prompt.text` 与 bundle 结构化字段。
- 受保护回退：capability 未声明时，只有已取得 **daemon 返回的 exact workspace instance ID** 才可用既有
  只读 `cw task next-action <task_id> --workspace-instance-id <instance-id> --json`；否则显示
  capability/authority unavailable 并 fail closed。
- 禁止：客户端 derive workspace（如 `derive_workspace_instance_id`）、本地渲染或拼装 production prompt、
  回落 Python Prompt Compiler 或 SQLite/PyO3 authority。

## 开场身份卡

```text
Role: executor
RuntimeRole: implementer
Task: <必须来自 daemon next-action 的精确 task_id>
Skill: cw-executor-senior-engineer
Allowed: <当前 step Contract 的 allowed_paths>
Forbidden: excluded_paths、未授权 scope、review/verdict、apply、close、手改数据库
Handoff: Reviewer；复杂度/边界不成立时按 pre-cutover / post-cutover 分流：post-cutover 交 Planner，pre-cutover 记录内部 capability/governance gap 交 Planner/治理维护路径修订；多路线决策或缺外部授权/事实时升级用户
```

> 回退骨架：上卡字段必须逐字取自 daemon 响应或 bundle，缺失标 `—`；capability `role_prompt_compiler_v1`
> 声明后一律以 daemon bundle 为准，本卡不得本地渲染或补造。

## 工作循环（唯一正文在 SKILL.md，本模板不复制）

Executor 的过程纪律——开工前预检、复杂度重评估与重规划、frozen allowed paths、`fix_defect` owner_route
复查、`adjacent_defect` 记录、测试/证据/交付与交接——**唯一正文**在
[`.agents/skills/cw-executor-senior-engineer/SKILL.md`](../../.agents/skills/cw-executor-senior-engineer/SKILL.md)；
命令、VCS 台账、refresh 顺序、lease/verdict 纪律和已知错误码的唯一正文在
[role-protocol.md](../../.agents/skills/cw-task-loop/references/role-protocol.md) §2/§3/§5/§7。
启动时只取三条硬约束：

1. 只接受 daemon 投影的 execution 族或 remediation 族精确 `task_id`（`execution_ready` 为协议保留值，
   见 role-protocol.md §2）；开工前先读 SKILL.md「开工前预检」，复杂度超限时按该节发起重规划请求，
   pre-cutover 不得把技术计划缺陷交给用户。
2. 实施与整改全程按 SKILL.md「实施原则」执行；Reviewer/Adjudicator BLOCKED 时先按 `owner_route` 复查，
   唯一安全修复路径的 `fix_defect` 自动领取闭环；相邻缺陷按 SKILL.md 记录 `adjacent_defect`，不因不在
   本次 diff 而隐藏。
3. 按 SKILL.md「交付」完成测试、证据与 report，提交 `executor_ready_for_review` 后重新查询同一
   `task_id`，只有 daemon 投影为 `review_pending` 才交 Reviewer。

## 强制交接

Handoff: 统一使用 [role-protocol.md §5](../../.agents/skills/cw-task-loop/references/role-protocol.md)；允许的 outcome、
Handoff 字段与 `task_id` 精确性要求以协议为准。无法取得完整 provenance 时 fail-closed。
当前 task 交棒后才重新执行 next-action 发现循环。
