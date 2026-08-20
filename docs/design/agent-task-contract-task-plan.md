# Agent Task Contract 实施任务计划

> 这是串行恢复计划，不允许多个阶段同时修改生产路径。
> 设计基线：`docs/design/agent-task-contract-design.md`
> 工具基线：`docs/design/daemon-rust-migration-ledger.md`

## 总原则

- 同时只有一个阶段处于 `in_progress`。
- 每个子任务必须有明确 steps、target_file、验收命令和 handoff。
- Implementer 不得 apply/close；Reviewer 不得修复；Coordinator 不得绕过 lease。
- 每次 commit 前刷新数据库；每次 runtime 变更后记录 binary hash。
- WSL/VM 不得直写 Windows 权威 SQLite。

## A0：冻结与基线

**目标**：固定当前代码、任务库、daemon、MCP 和 runtime，不回滚、不继续扩散变更。

验收：

- Git HEAD、dirty 文件清单、Python/Rust 版本记录；
- daemon binary、MCP client 和 authority fingerprint 记录；
- 当前 237 个 MCP 注册工具清单和初筛账本记录；
- 所有并行迁移任务列出 owner/status/下一步。

交付：`baseline.json`、`baseline.md`、任务事件和 hash 清单。

## A1：任务步骤与 mutation 结果修复

**目标**：修复当前阻塞，不涉及 HTTP 或 237 工具迁移。

范围：

- `task_next_step`/`task.claim` 返回步骤详情；
- `task_create_subtask` 保留 steps；
- MCP wrapper 正确处理结构化 daemon 响应；
- request_id dedup、claim 冲突、权限和失败语义保持不变；
- task events/task_steps/change_audit 证据闭合。

验收：真实 daemon CLI/MCP round-trip、Rust focused tests、步骤顺序、无步骤、并发 claim、重复 request_id。

## A2：Agent Identity

**目标**：登记并校验 `agent_id/agent_instance_id/client_id/provider/model_id/session_id/role/runtime_hash`。

验收：

- 未注册 identity fail-closed；
- 同一 instance 不能同时承担 Implementer 和独立 Reviewer；
- session 复用和 authority 变化可检测；
- identity 写入 task events 和 action identities。

## A3：Role Contract 与 Skill Provenance

**目标**：Planner 生成冻结的角色合同，`task.claim` 返回合同和 prompt/skill hash。

验收：

- allowed/forbidden paths 强制生效；
- skill 不存在、版本不符、prompt hash 不符时拒绝领取；
- handoff 角色不匹配时拒绝 report；
- 合同变更必须生成新版本和审计事件。

## A4：M1 Task State Machine 收口

**目标**：用 A2/A3 合同重新验收 create/split/claim/report/status/events/apply/close。

验收：

- fresh runtime；
- 真实 daemon round-trip；
- 所有步骤有 target_file/result/status；
- Reviewer PASS 后 Coordinator 合法 apply/close；
- 父子任务门禁有效。

## A5：逐 slice MCP 迁移

按 `daemon-rust-migration-ledger.md` 的 M2、M3、M4……顺序执行。每个 slice 都必须单独建任务、单独交付、单独复审。

## A6：HTTP 与 Loop Engineering

只有 A4 和至少一个 MCP slice 完成后才开始：

- HTTP transport；
- API key/TLS/authority；
- 远程 Agent claim/report；
- bare/reference integration ref；
- worktree prepare/submit/merge；
- merge conflict task。

