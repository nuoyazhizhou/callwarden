# Agent Identity、Prompt Provenance 与 Task Contract 设计

> 状态：设计草案，未表示代码已实现
> 日期：2026-08-11
> 适用范围：Call Warden 多 Agent 协同、Rust daemon、MCP/CLI/HTTP client
> 前置账本：`docs/design/daemon-rust-migration-ledger.md`

## 1. 问题

当前 Agent 主要依赖静态 `AGENTS.md` 和操作员复制提示词。多 Agent 并行时会出现：

- Agent 不知道自己应使用哪个 skill、修改哪些路径或执行哪些验收命令；
- Implementer、Tester、Reviewer 可能使用同一 session 或同一 Agent 实例；
- 任务已经写入数据库，但 MCP wrapper 返回错误，调用方误以为没有写入；
- `task.claim`、`task.next_step`、`task.report` 的步骤上下文不完整；
- 模型、系统提示词、工具 schema、runtime 和 Git 基线无法追溯；
- 代码提交和任务状态没有统一的 worktree/base ref/commit 关联。

## 2. 设计目标

1. Planner 创建任务时生成每个步骤的 Role Contract。
2. Agent 领取步骤时由 daemon 返回不可变 Task Envelope。
3. Agent 身份、模型、skill、prompt hash、工具 schema、runtime 和 Git 基线可审计。
4. Implementer、Tester、Evidence、Reviewer、Coordinator 的职责和权限可由状态机校验。
5. 错误响应必须明确区分“未写入”和“已写入但响应失败”。
6. 传输层与业务层分离，当前 Named Pipe/UDS/bridge 可复用，未来 HTTP 只替换 transport。
7. 不一次性迁移 237 个工具；每个功能 slice 独立通过后再切换。

## 3. 非目标

- 本阶段不实现 HTTP daemon。
- 本阶段不迁移全部 MCP 工具。
- 本阶段不自动替 Agent 做 Git merge。
- 本阶段不保存完整聊天记录或隐藏推理内容；只保存必要的 hash、结构化交接和公开证据。
- 不允许把 `model_id` 单独当作 Agent 独立性的证明。

## 4. 核心对象

### 4.1 Agent Identity

```json
{
  "agent_id": "claude-code-01",
  "agent_instance_id": "process-or-installation-uuid",
  "client_id": "claude-code",
  "provider": "deepseek",
  "model_id": "deepseek-v4-flash",
  "model_mode": "thinking",
  "system_fingerprint": "provider-returned-fingerprint",
  "session_id": "conversation-or-run-uuid",
  "role": "implementer",
  "runtime_hash": "binary-and-client-runtime-hash",
  "workspace_id": "workspace-id"
}
```

字段语义：

- `agent_id`：稳定的逻辑 Agent 身份。
- `agent_instance_id`：一次安装/进程实例，用于防止同一个实例同时扮演多个独立角色。
- `session_id`：单次任务会话。
- `client_id`：Codex、Claude Code、TRAE、Kiro 等外壳。
- `model_id`：请求声明的模型名。
- `system_fingerprint`：供应商返回的后端配置指纹，缺失时标记 `unverified`。
- `runtime_hash`：客户端和 daemon runtime 的 hash，避免旧 binary 冒充新实现。

### 4.2 Role Contract

```json
{
  "role": "implementer",
  "skill_id": "rust-daemon-task-state",
  "skill_version": "v1",
  "prompt_template_id": "implementer-v1",
  "prompt_hash": "sha256",
  "allowed_paths": ["rust_ext/src/daemon/task_collab.rs"],
  "forbidden_paths": ["server/tools/tools_query.py"],
  "commands": ["cargo test ...", "pytest ..."],
  "acceptance_checks": ["..."],
  "required_evidence": ["commit", "test_log", "binary_sha256"],
  "handoff_to": "tester",
  "independence": {
    "different_agent_instance_from": ["implementer"],
    "different_session_from": ["implementer"]
  }
}
```

Role Contract 由 Planner 生成，Coordinator 批准后冻结。Agent 不能在领取后扩大 `allowed_paths`、降低验收标准或替换 skill。

### 4.3 Task Envelope

`task.claim`/`task.next_step` 必须返回：

```json
{
  "task_id": "...",
  "step_id": "...",
  "parent_id": "...",
  "status": "in_progress",
  "role_contract": {},
  "identity_requirements": {},
  "git": {
    "base_ref": "refs/cw/integration",
    "worktree": "...",
    "branch": "cw/task/<task-id>"
  },
  "evidence_contract": {},
  "handoff": {"next_role": "tester"}
}
```

### 4.4 Prompt Provenance

不保存完整隐藏推理。保存以下可审计摘要：

- `prompt_template_id` 与版本；
- `system_prompt_hash`；
- `skill_id`/`skill_version`；
- `task_contract_hash`；
- `tool_schema_hash`；
- `model_id`、`system_fingerprint`、采样/思考模式摘要；
- 公开的用户目标、验收标准和 handoff 结果。

## 5. 状态机与权限

```text
planner:       draft -> planned
coordinator:   planned -> assigned
implementer:   assigned -> in_progress -> reported
tester:        reported -> tested / blocked
evidence:      tested -> evidence_ready / blocked
reviewer:      evidence_ready -> review_pass / request_changes
coordinator:   review_pass -> applied -> closed
```

规则：

- `independent_reviewer` 不能修改源码、证据或任务状态。
- `implementer`、`tester`、`evidence` 只能报告自己的步骤。
- `coordinator` 才能 apply/close，并必须提供真实 identity 和有效 lease。
- Reviewer 与 Implementer 至少不能共享 `agent_instance_id` 和 `session_id`。
- 高风险任务可要求不同 `model_id` 或不同 provider，但模型不同不是独立性的唯一证明。
- 任何 mutation 的响应失败都必须通过 `request_id`/事件查询确认是否已提交。

## 6. Git 协同模型

- 维护受保护的 `refs/cw/integration` 作为集成基线。
- 每个 Agent 使用独立 worktree 和 task branch。
- Agent 领取时记录 `base_ref` 和 `base_commit`。
- Agent 完成后提交 commit hash、测试结果和 evidence hash。
- 只有 Coordinator 可以把 task branch 合并到 integration ref。
- 冲突生成 `merge_conflict` 子任务，禁止 Agent 覆盖其他工作区。
- merge 后生成新的 `runtime_hash`，再进入 Reviewer。

## 7. API 最小变更

### `agent.register`

登记 identity、skill 能力、runtime 和可用 transport。

### `task.create`

接受 `role_contracts` 或由 Planner 生成并冻结合同。

### `task.claim`

校验 identity、角色独立性、skill 版本、lease 和 base ref，并返回完整 Task Envelope。

### `task.report`

接受 `commit_hash`、`test_runs`、`evidence`、`handoff` 和 identity；不能只接受自由文本。

### `task.handoff`

只允许交给 Role Contract 指定的下一个角色。

### `task.events`

追加领取、报告、交接、复审和合并事件，包含 identity 和 contract hash。

## 8. 实施顺序

1. 修复现有 task steps/claim/report/create_subtask 缺陷。✅
2. 增加最小 Agent Identity 注册和独立性校验。✅（v50：agent_registrations 扩展 identity 最小字段 + claim/report 独立性门禁 + 未注册 fail-closed）
3. 增加 Role Contract、skill/prompt hash 和 Task Envelope。✅（v50：role_contracts 冻结合同表 + task.create role_contracts + claim Envelope + contract_set revision 审计 + report 角色校验 + handoff target_role 校验）
4. 用新合同收口 M1 task state machine。
5. 再按迁移账本逐个切换 MCP slice。
6. 最后增加 HTTP transport、远程 Agent 和 merge queue。

