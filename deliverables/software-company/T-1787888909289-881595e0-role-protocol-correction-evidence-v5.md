# T-1787888909289-881595e0 角色治理修订 — 当前整改桥接事实更正证据（v5）

**任务：** T-1787888909289-881595e0  
**证据版本：** v5（append-only；v4 保持原样）  
**日期：** 2026-08-28  
**范围：** 纯文档/Skill/模板事实更正，不修改 daemon、CLI、MCP 或数据库

## 1. 更正结论

v4 §1 的 P1-2 正确说明了 `adjudicator_returned` 不会自动追加 step/reopen，但随后声称 Executor
当前可显式调用 `task.remediation.create` 完成闭环。该“当前 bridge 可用”结论不成立，本文撤回；
v4 作为历史证据原样保留。

当前可验证事实如下：

1. daemon 的自动 remediation 分支只覆盖 `reviewer_blocked`；`adjudicator_returned` 只持久化
   handoff 并固定路由 executor。
2. CLI 当前没有 `task remediation-create` 或等价的受支持命令面。
3. MCP `task_remediation_create` 把 `source_findings` 声明为字符串并原样透传，而 Rust handler
   要求该字段是 JSON array；这不是可工作的端到端契约。
4. 现有 handler 还要求非空 `source_step_id`，且 adjudicator 分支未完成对实际
   `adjudicator_returned` handoff event 的权威绑定。仅有内部 handler 不等于角色拥有可用治理路径。

因此，当前正确行为是：提交 `adjudicator_returned` 后重新查询同一 `task_id`；若仍投影
`READY/ADJUDICATE`，记录精确 task/verdict/handoff/source-step 治理缺口，交后续 daemon/CLI/MCP
实现任务。任何角色都不得改用通用 RPC、伪造整改 step 或自行声称 `remediation_pending`。

## 2. 本轮同步文件

```text
.agents/skills/cw-task-loop/references/role-protocol.md
.agents/skills/cw-task-loop/SKILL.md
AGENTS.md
Callwarden 无人值守循环启动模板：Adjudicator v4.md
docs/design/cw-role-handoff-task-loop-v2-amendment.md
deliverables/software-company/T-1787888909289-881595e0-role-protocol-correction-evidence-v5.md
```

同步结果：共享协议、项目入口、Adjudicator 模板和 v2 amendment 均不再把内部 handler
描述为当前受支持的角色整改桥接；未修改冻结 v1、历史模板或 production code。

## 3. 源码与命令面依据

```text
server/tools/tools_collab.py:314-357
  MCP source_findings: str，随后原样转发 task.remediation.create。

rust_ext/src/daemon/task_collab_lifecycle.rs:650-671
  daemon 要求 source_findings 为结构化 JSON array；reviewer_blocked 才校验与 verdict findings 一致。

rust_ext/src/daemon/task_collab_lifecycle.rs:433-784
  handler 要求 source_step_id/lease 等输入；内部入口存在不代表 CLI/MCP 已形成可用治理闭环。

cli/、cw.py、docs/cli_reference.md
  无 remediation-create 命令面。
```

## 4. 验证记录

以下门禁在本轮修改后执行：

```text
C:\Python314\python.exe scripts/validate_template_compliance.py
  PASS：协议单源、4 个角色模板、Skill/user-guide、设计 supersede 与归档 blob 一致。

C:\Python314\python.exe scripts/validate_template_compliance.py --self-test
  PASS：18/18 负向回归用例通过。

C:\Python314\python.exe -X utf8 C:\Users\wanpi\.codex\skills\.system\skill-creator\scripts\quick_validate.py .agents\skills\cw-task-loop
  PASS：初次 UTF-8 读取发现 description 含尖括号；把 <task-id> 更正为 TASK_ID 后复跑返回
  `Skill is valid!`。未加 `-X utf8` 的首次调用还因 Windows 默认 GBK 读取 UTF-8 文件失败，
  属校验器调用环境问题，未隐藏该失败。

C:\Python314\python.exe cw.py task --help
  exit=0；remediation 命令匹配数=0。

git diff --check
  目标白名单无空白错误；全工作区仅显示其他任务文件的 CRLF 警告。
```

review snapshot 自动生成、Planner 原生治理、decision request、adjacent relation，以及
`adjudicator_returned` 的原子/正式 client remediation bridge 均为后续 daemon/CLI/MCP 实现范围；
本轮不以文档声明伪装为已实现。
