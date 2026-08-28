# T-1787850432491-f42a2b8c Planner/Executor 角色与 Skill 更新证据

## 本次范围

- `AGENTS.md`：新增独立 Planner 角色、规划状态、复杂度预检、Executor 重规划路由和交接 outcome。
- `.agents/skills/cw-planner-architect/SKILL.md`：新增需求分析、复杂度评估、任务拆分和重规划 Skill。
- `.agents/skills/cw-executor-senior-engineer/SKILL.md`：新增高级工程实现、范围预检、整改闭环和回归验证 Skill。
- `.agents/skills/cw-task-loop/SKILL.md`：增加 Planner 角色卡、`READY/PLAN` 和规划/重规划状态投影。

## 规则结果

- 新任务默认先由 Planner 形成带 binding、Contract、identity policy、scope、验收和证据约束的可执行计划。
- Executor 发现复合 scope 或复杂度超限时，改代码前提交 `executor_replan_requested`，交回 Planner。
- 小型单一 ownership 任务可使用 `atomic_hotfix`，但不得绕过治理 binding 和证据门禁。
- Planner 不写生产代码、不 review、不 apply/close；Executor 不得自行硬做超出冻结范围的复合任务。

## 校验

```text
tokenslim run python -X utf8 C:/Users/wanpi/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/cw-planner-architect
tokenslim run python -X utf8 C:/Users/wanpi/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/cw-executor-senior-engineer
```

结果：两个 Skill 均返回 `Skill is valid!`。

提交前按项目规则执行 `cw --refresh-all` 时，当前 daemon 返回
`method_not_found: 未知方法: build_full_graph`；未使用 SQL 或其他旁路。该限制已记录，未影响本次 Markdown/Skill 静态变更。
