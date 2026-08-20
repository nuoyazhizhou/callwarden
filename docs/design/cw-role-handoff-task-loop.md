# cw Task Loop 与角色交接协议 v1

> 状态：已冻结为实施基线（freeze_design，任务 `T-1786983366974-8811ccec`）；本协议描述的是
> 分阶段待落地能力，不表示已实现，实现与门禁遵循 Requirement 15 的阶段门禁。
> 需求基线：`docs/design/requirements.md#Requirement 15 三角色治理与任务交接协议`
> 规划任务：`T-1786804435485-cc2eefc8`
> 权威依赖（按优先级）：
> `docs/design/requirements.md`、`docs/design/multi-llm-contract-driven-collaboration-design.md`、
> `docs/design/tasks.md`（协同、verdict、Gate 与任务计划不可拆分的冻结三件套；冲突优先级依次为
> requirements、主协同设计、tasks）、
> `docs/design/agent-task-contract-design.md`（Task/Role Contract、Envelope 与 RPC 的派生主设计）、
> `docs/design/agent-role-prompts-v1.md`（仅 fallback prompt，服从前述文档）。
> 不存在且**不是**本协议依赖的文件：`docs/design/agent-role-contract-design.md`。
> 本协议版本：`cw-task-loop/v1`；它只扩展前述主设计，冲突时以主设计和 requirements 为准。
> **公开 capability 前置条件**：当前冻结三件套和现有 `StageToggleStore` 仅定义 P0–P4 的
> `enabled/actor/changed_at` 开关，未定义 `task_loop_public`、authority id/revision/fencing、expiry 或 revoke。
> 因此本协议不复用或扩展既有 P-stage 作为 public permit authority。必须先完成 §7 的 **0A
> Capability Authority Amendment**，正式修订冻结三件套并由独立任务实现其 schema/API/migration；在 0A–0C
> 未通过 preflight 前，public discovery 和所有已知 public RPC 一律 `E_TASK_LOOP_CAPABILITY_DISABLED`。

## 1. 决策

本协议只有三个治理角色：`executor`、`reviewer`、`adjudicator`；聊天窗口不得在三者之间任意
切换。Executor 包含规划、实现、测试和证据工作模式：它把用户语言写成需求/设计，也把设计落实为
代码和证据。Reviewer 只审核；Adjudicator 只在 Reviewer PASS 后作独立完成裁决。

现有 daemon/CLI 中的 `planner`、`implementer`、`tester`、`evidence`、
`independent_reviewer` 是 RuntimeRole 兼容值，分别映射为 Executor 或 Reviewer，并不增加治理角色。
当前 runtime 中遗留的 Coordinator 命名，以及本文的 Capability Authority 或 control-plane 表/字段，
都只是运行控制面 provenance，不表示存在 Coordinator 治理角色；它们不得创建整改计划或裁决完成。

复用 append-only `task_verdict_events` 作为唯一 Verdict Ledger。由 daemon 原生
`verdict.submit` 在受保护 mutation 串行化点写入 verdict；它必须绑定 Canonical Task
Envelope、当前步骤 Role Contract、快照、真实 identity、reviewer lease/fencing 和
Authoritative_Clock。Evidence Gate 与后续查询都消费这一个 ledger。若需要让
`task.events` 时间线展示 verdict，只能追加含 `verdict_id` 的派生通知，不能让该通知
成为 verdict 判定真相源。

在此 ledger 和显式的 step→Role Contract 绑定就绪后，新增由 cw 权威状态驱动的只读派工
查询 `task.next_action`，并提供一个 `cw-task-loop` Skill 作为聊天入口。入口 Skill
只读取和呈现 cw 返回的角色卡与领取指引；它不能领取步骤、切换身份、修改任务状态或
替代 lease 校验。

聊天窗口应固定一个角色和 agent instance。一次 handoff 的目标角色必须在新的、满足
独立性约束的窗口/会话中领取。显式 Skill 调用使用 `$cw-task-loop`；不能把项目治理
角色假定为客户端的 `@` 可提及实体。

## 2. 问题与目标

当前 Role Contract 已冻结 `role`、`skill`、允许路径、验收、`handoff_to` 和独立性。
Task Envelope 也能在领取时提供这些信息。但最终回复是自由文本，不能保证每次都明确
指出下一合法动作，因此使用者会看到有时有 handoff、有时没有的情况。

本协议的目标是：

1. 由任务、步骤、依赖、显式 step→Role Contract 绑定、lease 与有效 verdict/evidence/gate
   投影计算下一合法动作；
2. 在任何状态下给出确定的 `READY`、`WAITING`、`BLOCKED` 或 `COMPLETE`；
3. 给聊天窗口一张可复制的角色卡，而非让模型猜测或自我授权；
4. 保持 Executor、Reviewer 与 Adjudicator 的 instance/session 隔离；
5. 将 Task Contract 与 Role Contract 分别绑定、分别哈希，避免单一 `contract_hash` 的
   语义歧义；
6. 不改变现有 task.claim/report/apply/close 的权威门禁，也不增加 SQLite fallback。

非目标：自动创建聊天窗口、自动注册 identity、自动取得 lease、自动 apply/close、
保存隐藏推理、或把 `@` 变成自定义角色 UI。

## 3. 权威接口

新增 daemon RPC `task.next_action`，并提供等价 CLI：

```text
cw task next-action <task_id> --json
```

这必须是严格只读操作：不激活 workspace、不创建 lease、不写 task event、不更新
`task_steps`。客户端/MCP/CLI 都只能通过 daemon/当前 authority 查询，禁止直读 SQLite。

### 3.1 响应

```json
{
  "task_id": "T-…",
  "decision": "READY",
  "action": "CLAIM",
  "required_role": "executor",
  "step_id": "S-…",
  "task_contract": {
    "id": "TC-…",
    "revision": 7,
    "hash": "sha256:…"
  },
  "role_contract": {
    "id": "RCL-T-…-executor",
    "revision_id": "RCR-…-r1",
    "revision": 1,
    "hash": "sha256:…",
    "canonicalization_version": "role-contract-c14n/v1",
    "canonicalization_rules_hash": "sha256:…",
    "skill_id": "rust-daemon-task-state",
    "skill_version": "v1",
    "prompt_template_id": "executor-v1",
    "handoff_to": "reviewer"
  },
  "authorization": {
    "acting_role": "executor",
    "lease_role": "executor",
    "lease_required": true,
    "fencing_required": true,
    "different_agent_instance_from": [],
    "different_session_from": []
  },
  "allowed_paths": ["…"],
  "forbidden_paths": ["…"],
  "eligibility": {
    "verdict": "not_required_for_claim",
    "evidence_gate": "not_evaluated",
    "snapshot": "not_evaluated",
    "mutation_recheck_required": true
  },
  "blocking_conditions": [],
  "revision_hint": null,
  "routing": {
    "origin_kind": "system_evaluator",
    "next_role": "executor",
    "next_action": "claim_current_step",
    "reason": ["current task state and persisted evidence"]
  },
  "next_session": {
    "role": "executor",
    "task_id": "T-…",
    "step_id": "S-…",
    "must_be_new_session": false
  },
  "source": {
    "task_status": "in_progress",
    "task_contract_hash": "sha256:…",
    "role_contract_hash": "sha256:…",
    "evaluated_at": "…"
  }
}
```

`decision` 是稳定枚举：`READY`、`WAITING`、`BLOCKED`、`COMPLETE`。`action` 是
`CLAIM`、`REVIEW`、`ADJUDICATE`、`REVISE`、`WAIT` 或 `NONE`。`authorization.acting_role` 与
`authorization.lease_role` 不可混用：Adjudicator 执行 apply/close 的 acting role 是
`adjudicator`，取得的 lease role 是 `reviewer`。无合同、合同不一致、绑定缺失或信息不足时必须返回
`BLOCKED`，不得以默认 Executor 角色猜测补全。

### 3.2 计算规则

规则按以下优先级评估：

1. 先只验证请求的 `workspace_instance_id` 是否可由 daemon registry/authority 复核到可用的
   authority workspace；不可达、失效或无法复核：`BLOCKED/NONE` +
   `E_WORKSPACE_AUTHORITY_UNAVAILABLE`，不得用 active workspace 补齐；
2. 再查询 task。task 不存在时返回非泄露 `BLOCKED/NONE` +
   `E_TASK_NOT_FOUND_OR_UNAUTHORIZED`；对尚未证明同 workspace 的 caller，task 存在但 binding/capture
   与请求 authority 不匹配也返回同一外部错误（审计日志可保存内部原因）；
3. 仅对已存在 task 验证不可变 workspace binding、capture 链与稳定 identity/hash。缺失、冲突、
   链不连续或不匹配：`BLOCKED/NONE` + `E_WORKSPACE_AUTHORITY_MISMATCH`；不得进入 lease/claim；
4. Task Contract 或 Role Contract 缺失/多版本冲突、step→Role Contract
   绑定不是唯一当前项、任一 hash 无法验证：`BLOCKED/NONE`；
5. 任务依赖、父子 close 门禁或合同 acceptance check 不满足：`BLOCKED/REVISE`，目标为 Executor；
6. 当前步骤已有未过期 lease：`WAITING/WAIT`，同时返回持有角色但不泄露 lease token；
7. 可领取的当前步骤：`READY/CLAIM`，角色只从该步骤的唯一当前 Role Contract 得到；若任务存在
   unresolved `failed` step，`task.next_action` 必须把其对应的 `fix_defect`/remediation step
   作为唯一可领取目标，不能因为后续 step 的 `step_index` 更小或更大而跳过它；
8. 任务在 `review` 且尚无有效持久化 verdict，且不存在 unresolved `failed` step：
   `READY/REVIEW`，目标为
   `reviewer`，并要求新的 instance/session；
9. Reviewer 的有效 verdict 为 `BLOCKED`：daemon 在同一 ledger 事务内把原 task 回到
   `in_progress`，追加唯一的 `fix_defect` step，并返回 `READY/REVISE` 给 Executor。该 step 必须绑定
   `remediation_of_step_id`、source verdict id、结构化 findings 和 source handoff event/request id；
   响应中的只读 `revision_hint` 只能回显这些来源、既有合同约束和观察事实。Reviewer/Adjudicator 不得
   创建整改 child、生成新 scope 或改写历史 verdict；
10. Reviewer 的有效 verdict 为 `PASS`：`READY/ADJUDICATE`，目标为新的、独立的 Adjudicator
    instance/session；此结果不等于已经 apply/close；
11. Adjudicator 接受 PASS 且步骤、子任务、Evidence Gate 和 lease 条件均满足：`READY/ADJUDICATE`，
    它可执行最终 apply/close；若不接受则 `READY/REVISE`，带具体退回 finding 交给 Executor；
12. 所有终态门禁满足且任务已 closed：`COMPLETE/NONE`。

H1/H2 等显式依赖图由 Role Contract/任务依赖而不是 task title 推断。例如 H2I 在
H1/H2 都已 reviewer PASS 且 Adjudicator close 前必须返回 `BLOCKED/REVISE`。

“有效”不是从 `acceptance_checks` 或 `required_evidence` JSON 文本猜测 PASS。查询只消费
Evidence Gate 已持久化的有效投影：Task Contract `(id, revision, hash)`、Role Contract
`(id, revision, hash)`、step binding、Gate Snapshot、attestation/identity、verdict phase 与
amendment 链必须全部匹配当前输入；旧 revision、旧 snapshot、无效或撤销 attestation、
identity 不独立、冲突 amendment 或未知 freshness 一律 fail closed。`next_action` 不运行
verifier，所有实际 mutation 仍须在同一事务/串行化点原子重检。

### 3.3 角色 handoff 与系统 routing

每个角色完成交接时的角色卡、已提交 `task.handoff` 的响应或 verdict/裁决输出，都必须携带：

```json
{
  "from_role": "executor | reviewer | adjudicator",
  "outcome": "executor_ready_for_review | executor_blocked_to_user | reviewer_pass | reviewer_blocked | adjudicator_accepted | adjudicator_returned",
  "next_role": "executor | reviewer | adjudicator | complete | user",
  "next_action": "single concrete action",
  "reason": ["finding/evidence/immutable contract reference"],
  "independence_requirement": "required | not_required | not_applicable"
}
```

路由没有自由裁量：Executor→Reviewer 与 Reviewer PASS→Adjudicator 的 requirement 为 `required`；
Reviewer BLOCKED→Executor 为 `not_required`；Adjudicator 接受→complete 为 `not_applicable`；
Adjudicator 退回→Executor 为 `not_required`；Executor→user 为 `not_applicable`。`user` 只用于缺失用户授权或必要事实。

`task.next_action` 是无角色来源的系统查询，必须只输出 `routing { origin_kind: system_evaluator,
next_role, next_action, reason }`，不得输出 `from_role`、角色 outcome 或伪造已发生的 handoff。其
`next_role` 枚举为 `executor|reviewer|adjudicator|user|complete|null`，`next_session` 为角色卡或
`null`：READY/CLAIM→executor/新的或已存在的 Executor session；READY/REVIEW→reviewer/新独立 session；
READY/ADJUDICATE→adjudicator/新独立 session；READY/REVISE→executor/同或新 Executor session；
WAITING/WAIT、BLOCKED/NONE→null/null；COMPLETE/NONE→complete/null。`READY/CLAIM` 只指向 Executor；
只有 Executor 实际 report 后才会产生 Executor→Reviewer handoff。
任何角色不知道下一棒时必须 fail closed 到 `user`，说明缺口，不能猜测角色。

### 3.4 `BLOCKED` 的执行者修订

同一聊天窗口不得因此切换角色：Reviewer 的 `BLOCKED` 只交付 finding 和只读 `revision_hint`，不取得
Executor 的计划修订权；Adjudicator 也不得把退回理由变成新的整改计划。`reviewer_blocked` handoff
提交时，daemon 必须原子地在**同一 task** 追加 `fix_defect` 并 reopen；Executor 读取 finding 后在该
step 内自行冻结修订 scope。普通整改不得创建 child：

```json
{
  "task_id": "T-…",
  "remediation_step_id": "S-…",
  "remediation_of_step_id": "S-source-…",
  "source_reviewer_verdict_id": "V-…",
  "finding_ids": ["F-…"],
  "proposed_action": "revise_in_place",
  "allowed_paths": ["…"],
  "excluded_paths": ["…"],
  "acceptance": ["…"],
  "capture_isolation": "frozen_baseline | isolated_worktree | exact_whitelist",
  "blocking_reason": "only when authority or safe scope cannot be verified"
}
```

该修订计划是 Executor 的工作产物，不是另一个持久化 verdict/event 真相源。只有明确独立 ownership、
独立 scope 且可单独验收的工作才能创建 child；`BLOCKED` 本身不满足该条件。任何 shared dirty/untracked 文件都必须排除在该 scope 之外，直到冻结基线、独立
worktree 或逐路径 whitelist capture 证明归属；不允许通过全量 capture 吸收它们。

## 4. 持久化交接

`task.next_action` 只计算。当前 `task.report` 只接收步骤结果、证据和 identity，并**没有**
结构化 handoff 字段；它继续只负责报告当前步骤，不在本设计中被隐式扩展为 handoff 写入。

`task.report` 的响应摘要永远不得宣称 handoff，`handoff` 固定为 `null`。只有独立 `task.handoff`
已提交的 event/响应可携带 handoff envelope；它必须持久化 `handoff_event_id`、source/target role、
`outcome` 和 request_id。`task.report` 请求含任何 handoff 字段仍按 §4.4 拒绝。

真正的 handoff 使用既有、独立的 `task.handoff` RPC。它须在 Contract/binding 交付项中扩展为
追加式结构化记录：请求和 task event 都必须绑定 source step、source/target role、Task Contract
三元组、Role Contract 三元组、reason、identity 与 Authoritative_Clock。它校验 source Role
Contract 的 `handoff_to`，但不得自行生成 verdict、取得 lease 或把目标 Agent 当成已领取。
当前 `task.handoff` 把 task 改回 `open` 的遗留行为必须在同一交付项中明确定义为兼容迁移或
fail-closed 拒绝；`next_action` 不得依赖这一隐式状态回退猜测目标角色。结构化 handoff 形态为：

```json
{
  "task.handoff": {
    "step_id": "S-…",
    "source_role": "executor",
    "target_role": "reviewer",
    "reason": "execution_and_evidence_ready_for_review",
    "outcome": "executor_ready_for_review",
    "task_contract": {"id": "TC-…", "revision": 7, "hash": "sha256:…"},
    "role_contract": {"id": "RC-…", "revision": 2, "hash": "sha256:…"},
    "required_new_instance": true,
    "required_new_session": true
  }
}
```

Reviewer verdict 由 `verdict.submit` 追加至 `task_verdict_events`，而不是另造通用
`task_events.review.verdict`。每条记录必须有 `task_contract` 与 `role_contract` 的独立
三元组、`step_id`、snapshot、identity、attestation、lease/fencing 与 clause results。
现有 `task_verdict_events.contract_*` 继续表示 Canonical Task Envelope；新增的
Role Contract 绑定字段不得复用或重命名它。可选 `task.events` 通知只保存 `verdict_id`
引用。未持久化的聊天文本不改变派工结果。

现有 `role_contracts.step_id` 目前没有可靠绑定语义，不能被 `next_action` 直接相信。
v1 必须使用 §8.1 冻结的 append-only `task_step_role_contract_bindings`。一个可领取步骤必须
恰有一个按 revision 链推导出的有效 binding；重绑只追加更高 binding revision，不能 UPDATE
任何历史 payload。

任何 v1 判定还必须从 §8.1 的不可变 `task_workspace_bindings` 取得 task 的逻辑
`workspace_id`。它不是 active workspace，也不是可变的 `active_task_id`；request 中的
`workspace_instance_id` 必须由 authority 映射到该逻辑 workspace 后才可进入领域校验。没有
唯一 task→workspace binding 的历史任务、步骤、Role Contract 或 verdict 一律为 `UNVERIFIED`，
不得以当前进程状态补齐。

### 4.1 Verdict 写路径迁移

`verdict.submit` 落地时必须消除当前两条公开写路径，而不是仅增加第三条：

1. daemon 在 Protected Mutation 串行化点实现原生 `verdict.submit`，以 Authoritative_Clock、
   Peer/Action Identity、reviewer lease/fencing、Task/Role Contract 双绑定和当前 snapshot
   原子验证后，向 `task_verdict_events` 追加记录；
2. MCP `submit_verdict` 改为纯 daemon RPC 转发。daemon 不可达、返回 `method_not_found` 或
   未通过验证时返回稳定的 fail-closed `E_VERDICT_DAEMON_UNAVAILABLE` 或更具体错误，绝不
   调用 `db.submit_verdict`；
3. S5 `_collab_rpc_call` 的 `direct_read` 兜底只允许在显式 allowlist 中的只读 collab 查询。
   `verdict.submit`、`reveal.submit`、`evidence.append` 与 `gate.decide` 永远不在 allowlist；
4. 切换顺序固定为：原生 handler + 负向测试 → MCP 转发 → 拒绝旧兜底 → 移除或改为生产拒绝
   的 Python `db.submit_verdict` 公开写路径。旧函数只能保留为迁移测试夹具，不能成为 MCP、CLI
   或 client 可达的 production write path。

在步骤 2、3、任务 4 与 1D3B 全部完成前，`verdict.submit` 不得在 public discovery/production
client 中标为可用；1D3A 的 Internal permit 只供受控迁移验证，不能改变此规则。现存客户端时钟
SQLite 写入产生的历史记录保留审计价值，但不得作为 daemon-attested 当前 verdict 满足 Gate 的替代。

### 4.2 Verdict 枚举与兼容

v1 冻结新 Reviewer 写入只能使用 `pass` 或 `block`，避免把 `next_action` 的决策词误写回 ledger：

| 字段 | v1 新写入 | 历史兼容读取映射 |
| --- | --- | --- |
| `overall` | `pass`、`block` | `approved`→`pass`，`needs_changes`/`request_changes`/`rejected`→`block`，`unclear`/`abstain`→`UNVERIFIED` |
| `phase` | `blind_first_pass`、`post_reveal_amendment` | `PRE_VERDICT`→`blind_first_pass`，`POST_VERDICT`→`post_reveal_amendment` |

`BLOCKED` 和 `UNVERIFIED` 是 `next_action`/Gate 的派生决策，不是新的 `overall` 写值；其中 `block`、
无有效 verdict、无效 identity/attestation、旧 snapshot 或冲突 amendment 都可使派工结果为 `BLOCKED`。
历史 `request_changes` 只读映射为 `block`，从而直接回到 Executor；迁移绝不重写已有 append-only payload。
`test_p1_write_path_verdict.py` 等既有 fixture 必须保留其历史输入并新增 canonical/legacy
等价断言；任何无法无歧义映射的遗留值均为 `UNVERIFIED`，不得默认成 `pass`。

映射由 append-only `verdict_normalization_rules` registry 冻结为
`verdict-normalization/v1`，其中保存 canonical JSON 的规则 payload 与 `rules_hash`。Task
Contract 必须引用 `(normalization_version, rules_hash)`；Gate 与 `next_action` 只能使用该绑定
版本，不能读取“当前最新”规则。每个 Gate decision 和 verdict 有效投影都持久化所用 version/hash，
以便同一历史 raw payload 永远可按当时规则复算。没有绑定、hash 不匹配、registry 被撤销或 raw
值不能映射时的唯一结果是 `UNVERIFIED`，不得进入 `pass` 路径。

### 4.3 结构化 handoff 的原子与幂等边界

`task_handoff_v1` 是版本化 capability；启用后 `task.handoff` 必须带 §4 的完整结构化 envelope。
它在 daemon 的单一 Protected Mutation 串行化点、一个事务内依次验证 source actor identity、
`authorization.acting_role`、source lease/fencing、当前 step binding、Task/Role Contract
三元组与 `handoff_to`。目标角色只被记录为下一候选人，绝不因 handoff 获得 agent registration、
lease 或 claim。

`task_operation_ledger` **只**是 v1 task-domain mutation 的权威 operation/dedup ledger，
不是所有 Protected Mutation 的替代品。其 method scope 固定为 `task.create`、`task.contract_set`、
`task.claim`、`task.report`、`task.handoff`、`task.apply`、`task.close`、`lease.acquire`、
`lease.renew`（含 alias `lease.extend`）、`lease.release`、`verdict.submit`、`reveal.submit`、
`evidence.append` 与 `gate.decide`。在 dedup key 构造前 `lease.extend` 必须规范化为 canonical
method `lease.renew`，所以两个入口绝不能获得不同 authority 或重复结果。其余 Protected Mutation
（含 snapshot、workspace refresh/recover、backup/restore
及未列入 scope 的 task 方法）继续使用冻结 HTTP compatibility contract 所要求的 transport dedup；
本协议不降级、不迁移、也不声明它们与 task DB 同一事务。daemon 必须维护覆盖全部
Protected Mutation 的静态 `method → dedup_route` 表：上述方法为 `TASK_DB_LEDGER`，其余为
`HTTP_TRANSPORT_LEDGER`，未知方法 fail closed；内部路由表由 1D3A 验收，public route 由 1D3B
在任务 4 证据有效后才发布。

对 `TASK_DB_LEDGER` 方法，key 固定为 `(workspace_instance_id, method, request_id)`。ledger 同时
保存 `params_canonicalization_version`、`params_canonicalization_rules_hash`、
`canonical_params_hash`、确定性 response/error、Task/Role Contract provenance 与提交时间。HTTP
transport 只在 envelope 解析后把 key 和 payload 交给这个 authority ledger；它可以保留本地 cache
作性能优化，但 cache 对 task-domain 不拥有正确性或跨重启语义。

对首次 request，authority ledger 使用 §8.1.4 的当前 operation-params rules 计算 hash，并在一
个事务中先检查已提交 key，再执行领域校验与写入，最后同时写入 handoff/verdict 领域事件、必要的
task event 引用和 ledger result。已提交 key 的重试必须用该**已保存**的 version/rules hash 重算
incoming payload：同 hash 返回 ledger 原结果且不追加事件；不同 hash 的优先响应始终是
`E_REQUEST_ID_REUSE_MISMATCH`。未见过的 request 才进入 `task.report`/`task.handoff` 领域校验。
因此“零写入”只指 task/step/event/lease/Gate 等领域状态；authority ledger 可以持久化并重放
`E_HANDOFF_REQUIRES_TASK_HANDOFF` 这类确定性拒绝。

Executor foundation 只拥有通用 `TaskMutationExecutor`、私有 transport envelope 类型、module
declaration 与 disabled dispatch shim；它不得编辑任何领域 handler、raw transport parser、route
activation 或 ledger schema。Executor 的输入固定是不可序列化、私有字段的
`StrictParsedEnvelope`，其中包含 `(workspace_instance_id, canonical_method, request_id, params,
invocation_class)`；`invocation_class` 是私有、不可序列化枚举 `ExternalTransport` 或
`InternalValidation`，不是客户端 JSON/header/params 字段。HTTP、Named Pipe、UDS strict parser **只能**
构造 `ExternalTransport`；只有 daemon 内不经 `dispatch.rs` 的私有 validation API 能构造
`InternalValidation`。客户端无法提交或覆盖 `duplicate_keys_checked` 一类 marker。

wrapper 将同一 task-DB connection/transaction 借给 caller-supplied `apply_domain(tx)`，但 callback
必须返回以下封闭、类型化 `DomainOutcome`，禁止依据错误字符串或普通 `Result::Err` 推断类别：

```text
CommitSuccess { response }
CommitDeterministicError { stable_error: StableDomainError }
RollbackInfrastructureError { infrastructure_error: InfrastructureError }
```

`StableDomainError` 只能来自冻结的稳定错误枚举；`InfrastructureError` 只能来自连接、事务、
Authoritative_Clock、registry、I/O 或未分类内部失败枚举。Success 将领域写入和 ledger result 同时
commit。为保证 callback 已有局部写入时的选择性回滚，wrapper 在调用 `apply_domain(tx)` 前必须执行
`SAVEPOINT task_domain_callback`：Success 先 `RELEASE SAVEPOINT`，再写 ledger result 后 commit；
DeterministicError 必须 `ROLLBACK TO task_domain_callback`、`RELEASE SAVEPOINT`，确认所有 callback
领域写入已撤销后才写可重放 ledger error 并 commit outer transaction；任一 savepoint/ledger 操作失败
都转为 InfrastructureError 并回滚 outer transaction。InfrastructureError 同样回滚 outer transaction、
领域写入和 ledger result。领域 handler 的接入所有权固定为：1A=`task.create`，1B=`task.contract_set`，1C=`task.claim`，
1F=`task.apply`/`task.close`/全部 `lease.*`，任务 2=`task.report`/`task.handoff`，任务 3=
`verdict.submit`/`reveal.submit`/`evidence.append`/`gate.decide`。未列入此表的 handler 不得接入
`TASK_DB_LEDGER`。

`apply_domain` 不接收原始 connection，而只接收非 `Clone`、生命周期受 wrapper 限制的
`TaskDomainTx` 与进入 callback 前冻结的 `FrozenAuthorityInput`（registry/clock/identity/contract
recheck 结果）。`TaskDomainTx` 不暴露 commit、rollback、savepoint、原始 connection ownership、
第二写连接创建或外部持久化 I/O；callback 禁止写文件、发网络请求或向其他 DB 提交。必须的提交后
副作用在 v1 **不受支持且一律禁止**：v1 不创建 `task_domain_outbox`，没有 outbox writer、consumer
或 crash-recovery 语义。所有 v1 callback 都必须只产生可由该 task-DB outer transaction 回滚的领域
写入；任何需要 commit 后文件、网络、其他数据库或进程副作用的 handler 必须留在 v1 capability
之外，不能以“稍后执行”绕过该限制。任何尝试越过 `TaskDomainTx` API 的 handler 一律
`RollbackInfrastructureError`。

为避免共享 `dispatch.rs`/`task_collab.rs` 的编辑冲突，foundation 一次性创建
`rust_ext/src/daemon/task_loop/` 的 `mod.rs`、`types.rs`、`executor.rs`、`operation_store.rs`、`route.rs`、
`strict_transport.rs`、`preflight.rs` 与全部 method-specific module declaration，并且**仅 foundation**可修改 `dispatch.rs` 的静态
`dispatch_task_loop` shim。shim 永远调用 `route.rs`，但在 cutover 前 route 只返回 fail-closed
`E_TASK_LOOP_CAPABILITY_DISABLED`。公共 `dispatch_task_loop` route 对 `ExternalTransport` **只**接受
绑定 schema/rules fingerprint 的 `PublicPreflightPermit`；permit 缺失或 fingerprint 变化时保持 disabled。
`InternalPreflightPermit` 绝不安装到 public route，只能由私有 in-process validation API 配合
`InternalValidation` 使用；public discovery 也只接受 Public permit。其余任务只能编辑自己分配的模块：1A=`create.rs`，
1B=`contract_set.rs`，1C=`claim.rs`，1F=`lifecycle_lease.rs`，任务 2=`report_handoff.rs`，任务 3=
`verdict_evidence_gate.rs`。foundation 初始提供所有 module 的 fail-closed stub；领域任务替换自己的
stub 并调用稳定的 `apply_domain(tx)` 接口，不得修改 executor、types、dispatch 或别人的模块。foundation 独占的
`capability_control.rs` 还必须提供 workspace-keyed、daemon 内权威的 `CapabilityMutationGate`。每个 public
mutation 必须在**打开任何 authority-store 或 task-DB 写 transaction 之前**取得该 gate，并持续持有到所有
相关 transaction commit 或 rollback 完成。全局锁序冻结为
`CapabilityMutationGate → Capability Authority store transaction → task-DB transaction`；不使用的后两项可跳过，
但任何路径都不得持有 DB transaction 等待 gate，或在持有 task-DB transaction 后再取得 authority-store lock。
`revalidate_public_permit` 在 gate 内重新读取 permit 所绑定的 §7 0A 引入的 Capability Authority
id/revision/fencing/validity、evidence validity、schema/rules fingerprint、runtime binary hash 与 daemon generation。
0B 接入的 Capability Authority create/update/expiry/revoke、`invalidate_evidence`、`revoke_verifier`、
`register_attestation_revocation` 及 capability invalidation 写路径都必须按同一锁序在 workspace gate 内提交，
禁止存在绕过 gate 的写入口。因而最终复核与领域提交之间不存在可提交的撤销：撤销先取得 gate 则 mutation
读到撤销并拒绝；mutation 先取得 gate 则撤销只能在它 commit/rollback 后提交。任一 authority 不可读时 fail
closed 为 `E_CAPABILITY_AUTHORITY_UNAVAILABLE` 并回滚 outer transaction；任一值失配/撤销时清除内存 permit，
以稳定 `E_TASK_LOOP_CAPABILITY_REVOKED` 走 deterministic-error savepoint 路径。route admission 的内存 permit
检查不是授权终点，禁止用入口时的检查结果跨越 gate 内的最终 recheck 与提交。旧
`task_collab.rs` 只保留非 v1 路径，不能作为并行 v1 写入入口。

`PublicPreflightPermit` 的 promotion 是 daemon control-plane 的 Protected Mutation，不是普通 client
或 MCP capability。foundation 独占实现非 public-discovery 的 `task_loop.public_promote` control-plane API
及 §8.1.5 的 append-only `task_loop_capability_promotion_events` 权威账本；1D3B 只能以真实 registered
control-plane authority identity 调用它。该调用不使用 task lease，而要求 §7 0A 引入的 workspace-scoped
`Task_Loop_Publication_Authority` 有效 id/revision/fencing/validity；daemon 在单一串行化点重新核对该 authority、Internal permit、
任务 4 evidence 和所有 fingerprint；该 serialisation point 也必须取得并遵守 `CapabilityMutationGate`。
请求必须含 `(workspace_id, request_id)`、control-plane action identity、
Capability Authority id/revision/fencing、Internal permit fingerprint、任务 4 `evidence_id/evidence_hash`、schema
fingerprint、runtime binary hash 与 daemon generation。相同 request id 和 canonical 参数只重放既有**持久化
授权结果**；
不同参数返回 `E_REQUEST_ID_REUSE_MISMATCH`。首次 request 的确定性拒绝追加可重放审计结果；基础设施失败
回滚审计写入且不安装 permit。成功必须先在 task DB commit 完整审计 event，**仅在 commit 成功后**才安装
内存 Public permit；审计 commit 失败绝不安装。该 API 的响应固定区分
`durable_authorization=(authorized|deterministic_error)` 与仅反映**当前 daemon generation**的
`permit_installation=(installed|not_installed)`：只有两者分别为 `authorized/installed` 才表示 public capability
当前可用。audit event 只表示“已授权 publication”，不是可跨重启恢复的 permit：commit 后安装失败、调用方
丢失响应或 daemon 崩溃时，同一 request-id 只能重放 `durable_authorization`，并报告当前
`permit_installation=not_installed`，不得重新安装 permit 或返回“当前可用”。此时调用方必须用**新的** request-id
发起一次完整、fresh-validation 的 promotion；旧 event 仅作为其审计前序，不能当作安装授权。

`PublicPreflightPermit` 是私有、不可序列化的精确 event capability，至少包含
`promotion_event_id`、`workspace_id`、`request_id`、`daemon_generation`、Capability Authority
id/revision/fencing/validity、Internal permit fingerprint、任务 4 evidence id/hash、schema/rules fingerprint 与
runtime binary hash。重放 event A 时，只有内存 permit 的 `promotion_event_id`、workspace、request-id 与
generation **均**等于 A 且所有绑定仍经 gate 重检有效，才可报告 `permit_installation=installed`；event B 的
permit 绝不能使 A 的重放显示 installed。

Public permit 永不从历史 event 自动恢复：daemon restart、daemon generation/runtime/schema fingerprint
变化、Capability Authority 失效或 evidence 被撤销时必须立即清除内存 permit，重新执行 1D3A preflight 和
1D3B promotion。`capability_control.rs` 的 authoritative revalidation 是该“立即”的可执行机制；它不依赖
watcher 或 fingerprint 恰好变化。历史 promotion event 仅用于审计，不能绕过 fresh validation。

非确定性基础设施失败会回滚领域事务与 authority ledger result，因此安全重试；不会留下“业务已
提交但 authority result 未保存”的窗口。v1 的结构化 handoff **不修改** `tasks.status`；当前遗留
RPC 的 `open` 状态回退不适用于 v1。启用 capability 后，缺少 v1 envelope 的 handoff 返回
`E_HANDOFF_FIELDS_REQUIRED`，且不沿用状态回退。

并发 source actor 由 ledger transaction 内的 lease/fencing 和合同重检决定唯一结果。合同
revision/hash 在读取与写入之间发生 ABA 变化时必须以 `E_HANDOFF_CONTRACT_STALE` 拒绝，不能
复用旧读取结果。

### 4.4 `task.report` 的交接保留字段拒绝

`task_handoff_v1` 与下述 report 拒绝规则必须同一 capability release 启用。HTTP envelope/authority
ledger 先执行 request-id 冲突检查：已提交的同 key 不同 hash 必须先返回
`E_REQUEST_ID_REUSE_MISMATCH`。对首次 canonical `task.report` request，若收到任意交接保留字段——
`handoff`、`target_role`、`target_agent`、`source_role`、`handoff_reason`、
`required_new_instance`、`required_new_session` 或 `handoff_contract`——handler 必须在任何 task-domain
查询或写入之前返回 `E_HANDOFF_REQUIRES_TASK_HANDOFF`。这是领域零写入拒绝：不得更新 task/step、
追加 task/action event、创建 lease 或改变 Gate；authority ledger 可以保存该确定性错误以供相同
request-id 重放。

在该 capability 启用前，`task.report` 没有 handoff 语义，也不进行隐式转换；旧客户端只能调用
其已知字段。MCP、CLI 和 production client 在发现 `task_handoff_v1` 后必须把交接改发
`task.handoff`，并把该稳定错误原样传递给仍误用 report 的调用方。

### 4.5 失败步骤 remediation 与合法回审

任务是 Jira 式主线程，角色交付是 append-only 回复。`task.report`、`task.handoff`、
`task.next_action` 与 remediation 都必须保留原 `task_id`；任何一个入口都不得因为 BLOCKED 自动调用
`task.create_subtask`/`task.split`。子任务只表示明确独立 ownership/scope，不表示一次复审或整改轮次。

失败步骤的历史状态是不可变事实：`task.report(success=false)` 只能把当前步骤记为 `failed`，
并由 daemon 追加一个带 `remediation_of_step_id=<failed_step_id>` 的 `fix_defect` 步骤；它不得
把原步骤改成 `done`，不得用 `task.reopen`、手工 UPDATE 或新的泛化任务覆盖失败记录。自动生成的
fix_defect 仍是普通 pending step，但其父失败步骤必须在 projection 中可追溯。

`task.claim` 在存在 unresolved failed step 时必须提供显式的 `remediation_step_id`（由
`task.next_action` 返回），并按 `(task_id, remediation_step_id, request_id)` 在同一
`TASK_DB_LEDGER` 事务中精确领取该步骤。缺少该字段、指定的步骤不是该失败步骤的当前 remediation、
或试图领取后续普通 step，均稳定返回 `E_REMEDIATION_STEP_REQUIRED` /
`E_REMEDIATION_STEP_MISMATCH`，不改变任何 task/step/event/lease 状态；同 request-id 同参数只重放，
不同参数返回 `E_REQUEST_ID_REUSE_MISMATCH`。这条精确领取规则同时适用于 CLI、MCP 和 daemon RPC，
不得由客户端自行推断 step_index。

Executor 完成 remediation 后必须调用受保护的 `task.step.resolve`（或等价的唯一 daemon
mutation），追加一条不可变 resolution event，而不是修改 `task_steps.status`：

```text
task_step_resolution_events(
  resolution_event_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  failed_step_id TEXT NOT NULL REFERENCES task_steps(id),
  remediation_step_id TEXT NOT NULL REFERENCES task_steps(id),
  outcome TEXT NOT NULL CHECK(outcome = 'resolved'),
  evidence_path TEXT NOT NULL,
  evidence_hash TEXT NOT NULL,
  capture_id TEXT NOT NULL,
  identity_json TEXT NOT NULL,
  authoritative_created_at TEXT NOT NULL,
  request_id TEXT NOT NULL,
  UNIQUE(task_id, failed_step_id, remediation_step_id),
  UNIQUE(task_id, request_id)
)
```

`task.step.resolve` 必须在领域串行化点重新检查：失败步骤仍为 `failed`、remediation 步骤属于
同一 task 且已 `done`、evidence/capture 与当前 Task/Role Contract 及 workspace authority
匹配、identity/lease/fencing 有效、Authoritative_Clock 可用；任一检查失败只追加可重放的
确定性 ledger error，不写 resolution event。重复 request-id 重放同一 resolution 结果；不同
failed/remediation/evidence 参数返回 `E_REQUEST_ID_REUSE_MISMATCH`。resolution event 成功提交后，
原 failed 行仍保持原始 status/result/hash，禁止 UPDATE 或删除；resolution event 是唯一“失败已解析”
真相源。

任务进入 `review` 的条件改为：不存在 pending step，且每个历史 `failed` step 都有一条有效、
不冲突且通过 Gate/authority 校验的 resolution event。任一 failed 无 resolution、resolution 指向
未完成 remediation、证据撤销/过期、合同或 workspace binding ABA、或 resolution ledger 不可达，
均保持 `in_progress` 并返回 `BLOCKED/REVISE`（目标 Executor），不得自动进入 `review`。历史任务
没有唯一 binding 或无法回填 resolution 的，一律只读显示 `UNVERIFIED`；迁移只新增 event/schema，
不得 UPDATE 既有 failed 步骤或旧 verdict/evidence。

Reviewer 的结构化 `reviewer_blocked` handoff 是另一类原地 remediation：handler 必须从
`task_verdict_events` 读取同一 Reviewer identity 提交的最新有效 `block` verdict，要求至少一个结构化
finding，并把 source step/verdict/findings/handoff request 绑定到新 `fix_defect.result`。handoff event、
fix_defect 与 task `review→in_progress` 必须同事务提交；任一校验或写入失败则三者均不出现。相同
request-id 重放原 event/step，不同 request-id 不得为同一 verdict 复制整改。Executor 完成此类
fix_defect 后，若无 pending/in_progress 且所有历史 failed 已由 resolution event 解析，可在同一任务
重新进入 review；多轮 BLOCKED 重复上述追加过程，旧 step/event/verdict 永不更新。

### 4.6 跨 worktree task authority preflight 与统一权威（bb1b5ff9 冻结）

多 worktree/thread 并发操作同一任务时，唯一权威 daemon/DB/manifest 必须由客户端在任一
task read/claim/report 前完成只读 preflight，不得在缺少 preflight 时静默选择本地库或
猜测 endpoint。本冻结只定义 preflight 契约与错误码，不实现生产代码；实现由后续独立
implementation child 依据本冻结与 §6 验收授权进行。

1. **probe_main_authority（只读 preflight）**：客户端在任何 task-domain 调用前，先对
   当前进程内选定的 daemon endpoint 做只读探测，必须同时可解析并一致：
   - `endpoint`：Named Pipe/UDS/HTTP 中唯一选定的一条，`task.*` 一律走它；
   - `token`：同一 daemon 签发的 transport identity/token 指纹；
   - `DB fingerprint`：`task_db_path` 的权威 fingerprint（含 registry/workspace 绑定链）；
   - `workspace_instance_id`：当前 worktree 的权威实例 id（与 capture/binding 链一致）；
   - `daemon generation`：daemon 启动代次/restart epoch，供升级后的重试比对。
   探测失败、任一要素缺失或与客户端已有状态不一致时，preflight 返回
   `E_AUTHORITY_UNRESOLVED`；不得继续 claim/report，不得自动创建新 task。
2. **probe_worktree_authority（跨 worktree task existence probe）**：同一任务若在其他
   worktree/thread 已被创建，客户端必须在 preflight 中对该 `task_id` 做只读存在性探测，
   并取回该任务的权威 `workspace_instance_id`/binding/DB fingerprint。探测成功且与本地
   worktree 推导的 instance 一致 → 直接复用该权威；不一致 → 返回
   `E_TASK_AUTHORITY_MISMATCH`，禁止在本地重复创建 task 或手工同步步骤/状态。
3. **freeze_authority_preflight（fail-closed 边界）**：
   - authority 未解析或存在性探测不一致时，**禁止** local SQLite fallback、重复创建 task、
     手工 UPDATE/同步 task/step/event/lease 状态；
   - preflight 只读，不得改动任何 task/step/event/lease/evidence 行，也不得触发 workspace
     激活或写锁；
   - `task.*`（read/claim/report/handoff/next_action/step.resolve）必须绑定同一
     endpoint+token+DB fingerprint+workspace_instance_id+daemon generation；任一绑定缺失或
     与 preflight 结果不一致，稳定返回 `E_TASK_AUTHORITY_MISMATCH`，零领域写入；
   - daemon 升级/重启后重试：按旧 generation 的 preflight 结果与新 generation 比对，不一致
     必须先重新 preflight 再继续，不得沿用旧 token 或旧 fingerprint 重放写操作。
4. **report_authority_handoff（冻结白名单与验收）**：实现 child 的白名单冻结为客户端
   preflight 适配与 CLI adapter（如适用）；本设计文档冻结 §6 的 authority 验收行
   （`E_AUTHORITY_UNRESOLVED` / `E_TASK_AUTHORITY_MISMATCH` 稳定错误码、零写入、禁止
   fallback/重复创建/手工同步、升级后重新 preflight）。历史任务无权威 binding 的一律只读
   显示 `UNVERIFIED`，不回填。

## 5. `cw-task-loop` Skill 与窗口规范

该入口 Skill 放在项目可发现的 `.agents/skills/cw-task-loop/SKILL.md`，并在安装后以
`$cw-task-loop <task-id>` 调用。它不是任务 Role Contract 所要求的 skill，也绝不写入
`role_contract.skill_id`；后者只由 Executor 冻结并由 claim 校验。入口 Skill 的固定流程为：

1. 读取 AGENTS.md、适用的冻结设计和 `cw task next-action --json`；
2. 输出由响应逐字派生的角色卡：Role、Task、Step、Skill、Allowed、Forbidden、Handoff；
3. 若为 `READY/CLAIM`，仅输出目标角色新会话应执行的领取指引、合同 id 与 identity
   要求；目标角色的新会话才可显式调用现有 claim/lease 路径，且不得从聊天内容伪造 identity；
4. 若为 `READY/REVIEW`，要求创建独立 Reviewer window/session；若为 `READY/ADJUDICATE`，要求
   创建同时独立于 Executor 与 Reviewer 的 Adjudicator window/session；不得在同一窗口转换；
5. 若为 `WAITING`、`BLOCKED` 或 `COMPLETE`，只解释来自 cw 的原因，不执行写操作；对含
   `revision_hint` 的 `READY/REVISE`，逐字呈现 revision card 并交回 Executor。Skill 不得替 Executor
   修订计划，也不得让 Reviewer/Adjudicator 创建整改步骤；
6. 每次 report 或 verdict 后重新查询，而不是相信 Agent 的最终自然语言。

建议聊天窗口标题包含 `task_id + role + session_id`。每个窗口只保留一个任务角色；Adjudicator 执行
apply/close 前仍须逐项获取真实 reviewer lease。角色提示词是 fallback 文本，Task Envelope/Role Contract
始终优先。

## 6. 验收矩阵

| 场景 | 预期结果 |
| --- | --- |
| unclaimed executor step，合同完整 | READY / CLAIM / executor |
| 当前 lease 未过期 | WAITING / WAIT，无 token |
| review 无 verdict | READY / REVIEW / reviewer，新 instance/session |
| reviewer BLOCKED | 同一 task 原子追加 provenance-bound fix_defect 并 reopen；READY / REVISE / executor；旧 verdict 不改写、不创建 child |
| reviewer BLOCKED 且已有可操作 finding | `revision_hint` 只列出来源、既有合同约束和观察事实；Executor 自己修订原计划/实现、路径、验收和 capture 隔离 |
| 原计划没有 pending step | reviewer_blocked handoff 在原 task 追加 fix_defect；不创建 remediation child |
| 同一 task 连续两轮 BLOCKED→REVISE→REVIEW | 每轮各追加一个 handoff/fix_defect；task_id 不变，前一轮 step/event/verdict 全部不变 |
| 无 pending 但存在未解析 failed | 保持 in_progress/REVISE；不得伪造 review |
| 创建 child 未声明独立 ownership/scope | fail closed；BLOCKED remediation 必须回到原 task |
| unresolved failed step 存在 | `task.next_action` 只返回其精确 remediation step；不得领取后续普通 step，不得进入 review |
| `task.claim` 缺少/伪造 `remediation_step_id` | `E_REMEDIATION_STEP_REQUIRED` 或 `E_REMEDIATION_STEP_MISMATCH`；task/step/event/lease 零领域写入 |
| remediation 完成但未调用 resolution | 原 failed 仍为 failed，任务保持 in_progress；不得自动转 review |
| `task.step.resolve` 前 remediation 未 done、证据失配或 identity/fencing 过期 | 稳定 deterministic error；不追加 resolution event，不修改 failed 行 |
| `task.step.resolve` 成功 | 追加唯一 resolution event；原 failed status/result/hash 不变；只有所有 failed 均 resolved 且无 pending 才可 review |
| resolution request-id 重放/冲突 | 同参数重放同一 event/result；不同 failed/remediation/evidence 参数返回 `E_REQUEST_ID_REUSE_MISMATCH` |
| 旧 failed 步骤无可验证 resolution | 只读显示 `UNVERIFIED` 或 `BLOCKED/REVISE`；不得 UPDATE 历史 step/verdict/evidence |
| shared dirty/untracked diff 污染 capture | Executor 的修订计划声明 frozen baseline、isolated worktree 或 exact whitelist；全量 capture 拒绝执行 |
| reviewer PASS 但 child open | READY / REVISE / executor |
| reviewer PASS 且关闭前证据完整 | READY / ADJUDICATE，仍需独立 instance 和真实 lease |
| H2I 的 H1/H2 前置未关闭 | READY / REVISE / executor |
| 缺 Role Contract 或 hash 不匹配 | BLOCKED / NONE，禁止 claim |
| Skill 在同一 Executor session 请求 REVIEW 或 ADJUDICATE | 拒绝并提示新的独立 session |
| 六种 handoff outcome 与 next_role/independence 组合 | 仅接受 Executor ready→Reviewer/required、Executor blocked→user/not_applicable、Reviewer pass→Adjudicator/required、Reviewer blocked→Executor/not_required、Adjudicator accepted→complete/not_applicable、Adjudicator returned→Executor/not_required；其余组合拒绝 |
| system routing 的 WAIT/NONE/COMPLETE | 分别为 `null/null`、`null/null`、`complete/null`；不得伪造 `from_role` 或 handoff |
| READY/CLAIM routing | `executor` + 新的或已有 Executor session；`revision_hint=null` |
| task.report 与 handoff | report 请求含 handoff 字段拒绝、响应固定 `handoff=null`；仅 task.handoff 已提交 event/response 必含 event_id、outcome、request_id，缺任一项拒绝 |
| 任意查询 | 不产生 task event、lease、workspace 写入或 SQLite fallback |
| daemon 不可用时 MCP/CLI/RPC 提交 verdict/reveal/evidence/gate | 稳定 fail-closed；相关表和 task event 行数不变 |
| `cw identity revoke`（`_identity_revoke`）在 Capability Authority/gate release 后 | 不得直调 `db.register_attestation_revocation`；必须 daemon 转发或稳定 fail-closed，撤销表和 task event 零写入 |
| Capability Authority 管理 | 真实 control-plane authority 只能经 0A 冻结的 daemon control-plane `task_loop.authority.*` 或其 CLI adapter 调用；MCP/public discovery 不暴露该 mutation，缺 identity/request-id/authorization 稳定拒绝 |
| 迁移门槛后遗留 SQLite verdict 写路径 | 拒绝；无客户端时钟 fallback 或重试写入 |
| `task.report` 含任一交接保留字段（首次 canonical request） | `E_HANDOFF_REQUIRES_TASK_HANDOFF`；task/step/event/lease/Gate 零写入，但 authority operation ledger 持久化并重放该确定性错误 |
| 同一 `(workspace_instance_id, method, request_id)` 参数 hash 不同 | 优先 `E_REQUEST_ID_REUSE_MISMATCH`；不进入 `task.report` 领域校验 |
| daemon 升级后同一 task-domain request 重试 | 用 ledger 已保存的 `operation-params-c14n` version/rules hash 比较并重放原结果；不得因新规则误报 reuse mismatch |
| HTTP、Named Pipe、UDS 任一入口含 duplicate JSON key | 原始 parser 返回 `E_DUPLICATE_JSON_KEY`；不进入 dedup/dispatch，任一 ledger/task-domain 行数不变 |
| 客户端 params 提交 `duplicate_keys_checked` 或同名 provenance | 视为普通未知/受限字段，不能构造 `StrictParsedEnvelope`；route fail closed |
| 1D3A 已通过后直接调用已知 RPC（HTTP/Named Pipe/UDS） | strict parser 只能构造 `ExternalTransport`，公共 route 仍返回 `E_TASK_LOOP_CAPABILITY_DISABLED`；Internal permit 不可用 |
| handler 返回 deterministic/infrastructure failure | 只能通过 `DomainOutcome` 枚举分类；前者只写 ledger error，后者回滚 domain 与 ledger，禁止字符串分类 |
| task workspace binding 的 local/registry/capture 任一不匹配 | `E_WORKSPACE_AUTHORITY_MISMATCH`；不得写 task-domain 状态，TASK_DB_LEDGER 可持久化并重放该确定性错误 |
| `task.next_action` 的 workspace authority 不可达或 binding/capture 缺失 | `BLOCKED/NONE` + `E_WORKSPACE_AUTHORITY_UNAVAILABLE`；不评估 lease/claim |
| registry heartbeat/status/snapshot 合法变化 | stable capture identity hash 不变，既有 task binding 仍可验证；root/manifest/instance 改变则 `UNVERIFIED` |
| canonicalization rule 撤销 | 只追加 revocation row；旧 ledger/revision 按已绑定规则可重放，新 operation/new revision 被拒绝 |
| callback 先写领域行后返回 `CommitDeterministicError` | wrapper `ROLLBACK TO task_domain_callback`；领域行数不变，只新增可重放 ledger error |
| 领域 handler 经 wrapper 写入 | Success 时 domain event 与 ledger result 同 transaction；确定性拒绝仅 ledger error；基础设施失败二者均回滚 |
| 1D3A preflight 或 Internal permit fingerprint 不通过 | route 返回 `E_TASK_LOOP_CAPABILITY_DISABLED`；不得调用任何非 stub handler |
| 1D3A 通过但任务 4 未完成 | public discovery/production client 仍 disabled；不得发布 `PublicPreflightPermit` |
| 1D3B final publication | 必须复核 fresh fingerprint、1D3A Internal permit、0A/0B Capability Authority preflight 与任务 4 的旧路径拒绝证据；任一失效则不公开 capability，且 audit commit 前不得安装 Public permit |
| public promotion 缺 control-plane identity、Capability Authority、evidence/runtime/generation binding | 在已有 workspace/request-id key 下追加可重放 `deterministic_error` event；完整 canonical request 保存，无法提取的 provenance 为 NULL；不得安装 permit |
| public promotion 授权/重试/失效 | 相同 `(workspace_id, method, request_id)` 必须用 event 保存的 `operation-params` version/rules hash 重算；同 hash 只重放 durable authorization 并仅对该 `promotion_event_id` 报告当前 `permit_installation`，不同 hash 优先 `E_REQUEST_ID_REUSE_MISMATCH`；确定性拒绝持久化、基础设施失败整笔回滚；重启或 fingerprint/evidence/Capability Authority 失效即清除内存 Public permit 并重新 preflight |
| promotion audit commit、permit 安装失败或调用方丢失成功响应 | audit commit 失败绝不安装 permit；commit 后安装失败或重启后 replay 返回 `authorized/not_installed`，不得返回 capability 当前可用，也不得同 key 安装；只有新的 request-id + fresh validation 可再次安装 permit |
| promotion ruleset 升级或撤销后的同 key 重试 | 使用 event 保存的历史 version/rules hash 比较并重放；撤销只禁止首次 key，不能把旧 request 误判为 reuse mismatch |
| public mutation 在 route admission 后、最终 recheck 后、domain commit 前撤销 Capability Authority/evidence 或变更 daemon generation | `CapabilityMutationGate` 必须先于任一 authority/task DB transaction 取得并持有至 commit/rollback；撤销先提交则 final recheck 返回 `E_TASK_LOOP_CAPABILITY_REVOKED` 且回滚 callback 写入，只保存确定性 ledger error；mutation 先持 gate 则撤销必须等待其结束后才可提交；authority 不可读返回 `E_CAPABILITY_AUTHORITY_UNAVAILABLE` 并完整回滚 |
| gate 与 DB 锁顺序竞争 | 以 barrier 让 public mutation 与 0B 的 Capability Authority/evidence invalidator 并发；两者均按 `gate → authority store → task DB`，在有界时间内无死锁，最终提交顺序决定接受或拒绝，绝无 bypass 写入 |
| event A 已失效、event B 已安装 permit 后重放 A | A 返回 `authorized/not_installed`；仅 B 可报告 installed，且只限其相同 generation 和全绑定 recheck 通过 |
| callback 尝试 commit/另开写连接/外部 I/O | `TaskDomainTx` API 不可取得该能力；v1 不存在 outbox 或提交后副作用路径，callback 返回 `RollbackInfrastructureError` 且外部动作不得发生 |
| non-task Protected Mutation | 命中 `HTTP_TRANSPORT_LEDGER`；本协议不得把其持久化 transport dedup 降级为 cache，也不得声称与 task DB 同事务 |
| handoff/verdict 读取后合同 ABA | `E_HANDOFF_CONTRACT_STALE` 或对应 verdict stale 错误；零部分提交 |
| handoff 过期 lease、旧 fencing、重复或冲突 request_id | 稳定拒绝/重放；事件不重复 |
| legacy overall/phase 与未知 raw 值 | 版本化映射；未知值仅 `UNVERIFIED`，历史 payload 无 UPDATE |
| handoff/verdict 的非确定性关联写入失败 | domain event、状态、Gate 可见性与 authority operation ledger result 均不出现部分提交；确定性业务拒绝是仅 ledger 错误结果的显式例外 |
| preflight 探测 endpoint/token/DB fingerprint/workspace_instance_id/daemon generation 任一不可解析或不一致 | `E_AUTHORITY_UNRESOLVED`；不得继续 claim/report，不得自动创建 task，零领域写入 |
| 同 task 已在其他 worktree 创建且 instance 不一致 | `E_TASK_AUTHORITY_MISMATCH`；禁止本地重复创建 task 或手工同步步骤/状态 |
| authority 未解析时客户端尝试 local SQLite fallback / 重复创建 task / 手工 UPDATE 同步 | fail-closed 拒绝；task/step/event/lease 零写入 |
| daemon 升级/重启后沿用旧 generation 的 preflight 结果重试写操作 | 先重新 preflight（按新 generation 比对），不一致时拒绝并提示重新探测；不得沿用旧 token/fingerprint 重放 |
| 历史任务无权威 binding | 只读显示 `UNVERIFIED`，不回填/不伪造 binding |

## 7. 分期交付

实现前，Executor 必须先核对冻结计划中 daemon verdict、P1 verdict/gate 相关
任务的所有权；若已有任务涵盖以下任一项，必须复用或明确扩展该任务，不能重叠创建。

失败步骤 remediation 生命周期复用既有父任务 `T-1786986333084-baf7e552` 及其两个子任务：
`T-1786986359010-68abd42f` 只冻结本节 contract/migration/负向验收，
`T-1786986359010-9b5ec530` 在该 contract 经独立 Reviewer PASS 后独占 schema、daemon/RPC、CLI
和测试实现。任何 `task.next_action`、authority capture 或其他实施任务不得自行定义第二套
resolution ledger、按 step_index 跳过 remediation，或把该父任务的 failed-step 修复吸收进别的任务。

跨 worktree task authority 一致性复用既有父任务 `T-1786987073956-bb1b5ff9`：其四个阶段
（probe_main_authority / probe_worktree_authority / freeze_authority_preflight /
report_authority_handoff）只冻结 §4.6 的只读 preflight 契约、`E_AUTHORITY_UNRESOLVED` /
`E_TASK_AUTHORITY_MISMATCH` 稳定错误码与 §6 验收行；后续独立 implementation child 在该 contract
经独立 Reviewer PASS 后，才可依据冻结白名单实现客户端 preflight 适配/CLI adapter。任何任务
不得另起第二套 authority 探测、绕开 `E_TASK_AUTHORITY_MISMATCH` 重复创建 task 或手工同步状态。

1. **Contract/Binding/operation 交付父任务（不实现生产代码）**：只冻结下列 foundation、
   schema、handler integration 与 cutover 子任务的 migration、rollback/fail-closed、legacy 策略、
   非重叠白名单和验收证据；它们必须分别创建、分别领取与复审：

   - **0A Capability Authority Amendment（规划前置，不实现生产代码）**：在 1D0 之前，以 Executor 的独立
     规划工作模式任务正式修订冻结三件套 `requirements.md`、`multi-llm-contract-driven-collaboration-design.md` 与
     `tasks.md`。该修订必须定义非 P0–P4 派生的 `Task_Loop_Publication_Authority`：其 daemon-owned
     schema/migration、id/revision/fencing/validity/expiry/revoke、scope、真实 action identity、
     Authoritative Clock、API、append-only audit、与 P-stage 的关系、capability disable 默认值，以及
     `CapabilityMutationGate → authority store → task DB` 锁序和死锁验收。它还必须冻结 evidence
     invalidation writer inventory 与新的 daemon-only/fail-closed routing 边界。0A 还必须冻结唯一的
     control-plane authority 可达入口：daemon control-plane `task_loop.authority.create`、`.update`、`.expire`、`.revoke`
     （均为 Protected Mutation，非 MCP public discovery）以及只作该 RPC adapter 的
     `cw task-loop authority <create|update|expire|revoke>`；它必须为每个入口定义 identity、request-id、
     lease/fencing（如 0A 选择要求）、审计、幂等与稳定拒绝。未完成或有冲突时本协议不得把 public capability
     标为可用；
   - **1D0 Executor foundation**：在 0A 后创建 `canonicalization_rule_sets` 与其 revocation schema、
     `rust_ext/src/daemon/task_loop/{mod.rs,types.rs,executor.rs,operation_store.rs,route.rs,strict_transport.rs,preflight.rs}`
     的私有类型、interface、module declaration 和所有 fail-closed stub，以及 `dispatch.rs` 的 disabled shim；
     它还独占 `task_loop/capability_control.rs`、`task_loop_capability_promotion_events` schema 与非 public
     control-plane promotion API、promotion request-id 的唯一重放/冲突规则与每次 public mutation 的提交前
     permit authority revalidation、上述 authority control-plane route 的 fail-closed dispatch declaration，以及
     gate 私有 interface。它还必须在 `mod.rs` 预声明仅 `cfg(test)` 编译的
     `capability_control_test.rs`，并**同时创建该文件的空、可编译 test stub**（不得含测试断言或 barrier
     choreography），从而保证 1D0 自身的 Rust test build 不因 module 缺失失败；它还在
     `capability_control.rs` 提供仅该测试模块可见、不可由 production RPC/discovery 调用的 final-recheck
     barrier seam。除该通用 control plane 外，不插入 domain rule row、不创建 operation ledger、
     不启用 public route，也不得编辑领域 handler；
   - **0B Existing-authority/gate integration**：仅在 0A 与 1D0 完成后实现 Capability Authority 的
     daemon schema/API/migration、gate 适配与无 bypass preflight。独占修改
     `rust_ext/src/daemon/stage_toggle.rs`（0A 定义的 authority store/transition）、
     `db/db_task_evidence.py`（`invalidate_evidence`、`revoke_verifier`）、
     `db/db_task_identity.py`（`register_attestation_revocation`）、
     `server/tools/tools_p3_identity.py`、`cli/main.py`（精确限于 `_identity_revoke` 和 0A 冻结的
     `cw task-loop authority ...` adapter）（将上述既有直写入口改为 daemon
     转发或稳定 fail-closed），并且只能调用、不修改 `task_loop/capability_control.rs` 或 `dispatch.rs`。
     authority RPC 的 dispatch/control-plane route 只能由 1D0 修改，0B 仅实现其已冻结的 store interface。
     它必须在每个实际
     写入口于任何 DB transaction 前取得 gate，执行 §4.3 锁序，并由 preflight 枚举并拒绝所有未接入
     writer；不得编辑 task-domain handler、route、transport parser、client adapter 或 Gate decision logic；
   - **0C Authority/gate 独立测试与证据**：只新增
     `tests/test_task_loop_capability_authority.py`、`tests/test_task_loop_gate_order.py`，并只**填充**
     1D0 已创建的 `rust_ext/src/daemon/task_loop/capability_control_test.rs`。后者必须通过 1D0 预声明的 `cfg(test)`
     私有 barrier，在**真实 Rust `CapabilityMutationGate`** 的 final recheck 与 task-DB commit 之间精确
     暂停，再并发执行真实 authority/evidence revoke；Python 测试只负责真实 daemon/runtime 的 route、CLI
     fail-closed 与 writer-inventory 验收，不得用计时猜测代替 Rust barrier。覆盖 0A contract 的 migration/API、
     0B writer-inventory completeness、无 gate bypass、gate-first 锁序、revoke-vs-final-recheck 竞态与有界
     无死锁；依赖 0A、1D0、0B。不得编辑任一生产文件或 schema；其原始通过证据是 1D3A 的硬前置；
   - **1D1 Task-domain operation store**：只实现 `task_operation_ledger` migration、
     `operation_params` rule row 与 `task_loop/operation_store.rs`；依赖 1D0。不得编辑 executor、
     dispatch、route、transport parser 或领域 handler；
   - **1D2 Strict transport parser**：只实现 `task_loop/strict_transport.rs` 以及 HTTP
     `http_server.rs`、Named Pipe `server.rs`、UDS/framed `protocol.rs` 的 raw-byte strict parser
     接入，构造私有 `StrictParsedEnvelope`；依赖 1D0。不得编辑 route、operation store 或领域 handler；
   - **1A Workspace authority binding**：只实现 `task_workspace_bindings`、
     `workspace_authority_captures`、`workspace_capture` rule row，以及
     `rust_ext/src/daemon/task_loop/create.rs` 中原生 `task.create` 的同事务 binding 写入；依赖
     1D0、1D1；不得编辑 Role Contract、step binding、operation store 或 Gate；
   - **1B Role Contract lineage/c14n**：只实现 `role_contract_lineages`、
     `role_contract_revisions`、`role_contract` rule row/legacy migration 与
     `rust_ext/src/daemon/task_loop/contract_set.rs`；依赖 1D0、1D1、1A；不得编辑 task.create、
     step binding、operation store 或 Gate；
   - **1C Step binding**：只实现 `task_step_role_contract_bindings`、其 task/workspace/revision
     一致性校验、fail-closed read projection 与 `rust_ext/src/daemon/task_loop/claim.rs`；依赖
     1D0、1D1、1A、1B；不得编辑 Role Contract payload/hash、operation store 或 Gate；
   - **1E Verdict/Gate schema 与 legacy fail-closed**：只实现 `verdict_normalization_rules`、
     Task Contract normalization binding、`task_verdict_events`/Gate provenance columns，以及历史
     UNVERIFIED migration/read fixture；依赖 1D0、1A–1C。不得实现 verdict handler、handoff、
     client 路由或 Gate decision logic；
   - **1F Task lifecycle/lease wrapper integration**：只将 `task.apply`、`task.close`、
     `lease.acquire`、`lease.renew`/`lease.extend`、`lease.release` 接入 wrapper；只编辑
     `rust_ext/src/daemon/task_loop/lifecycle_lease.rs`；依赖 1D0、1D1。不得修改 wrapper、
     task.create/contract_set/claim/report/handoff/verdict handler 或任何 schema；
   - **1D3A Internal preflight**：独占 `task_loop/preflight.rs`；实现
     `task-loop-schema-preflight/v1`、只读验证所有 stub 已替换及 1A–1F/2/3 的 schema/rule/route
     条件。通过后仅经 foundation 的 API 发布绑定 fingerprint 的 `InternalPreflightPermit`，保持 public
     discovery 和 public capability disabled；它不得修改 route、领域 handler、executor、operation
     store、raw parser、transport dedup 或 schema；依赖 0A–0C、1D0–1D2、1A–1F、2、3。
   - **1D3B Final public publication（无生产代码）**：只校验 1D3A 的 permit、任务 4 的转发/旧路径
     拒绝证据与 fresh preflight fingerprint；真实 control-plane authority 必须按 §4.3 携带 Capability Authority
     id/revision/fencing、request id 与 evidence/runtime/daemon binding，三者均有效时才调用 foundation 的
     既有 promotion API 发布 `PublicPreflightPermit` 并追加审计事件。它不得编辑任何文件、重跑 schema
     migration 或绕过任务 4；依赖 0A、0B、1D3A、4。相同 request-id 的 replay、冲突、审计先提交后安装 permit
     与重启不恢复 permit 必须由 1D0 API 自动强制，1D3B 不得自行解释或补救。
2. **原生 `task.handoff`**：仅实现 §4.3/§4.4 的结构化 handoff、source 授权重检、原子/幂等
   语义和 `task.report` 保留字段零写入拒绝；只编辑
   `rust_ext/src/daemon/task_loop/report_handoff.rs`；依赖 1D0–1D2、1A–1C；
3. **原生 `verdict.submit`、ledger 与 Gate 读取**：仅实现 daemon verdict 追加、有效投影和
   Evidence Gate 的 versioned normalization 消费；只编辑
   `rust_ext/src/daemon/task_loop/verdict_evidence_gate.rs`；依赖 1D0–1D2、1B–1E、2；
4. **MCP/CLI/client mutation 路由与旧路径拒绝**：仅将 verdict/reveal/evidence/gate/handoff
   写操作（含 `server/tools/tools_collab.py`）转发到 daemon，关闭 direct_read/SQLite 写兜底；不拥有
   Capability Authority、evidence/attestation invalidation writer、`cli/main.py::_identity_revoke`、
   `cw task-loop authority ...` adapter 或 `task.next_action` CLI adapter；
   依赖任务 1D3A、2、3；
5. **`task.next_action` 交付父任务（不实现生产代码）**：只冻结 5A–5D 的接口、依赖、
   非重叠白名单和验收证据；依赖任务 1D0–1D3B、1A–1F、2–4。其子任务必须分别创建、分别领取与复审：

   - **5A Evaluator/daemon RPC**：只实现纯 `task.next_action` evaluator 与 daemon handler；
     不得编辑 CLI、Skill 或测试断言；
   - **5B CLI adapter**：只实现 `cw task next-action --json` 的 CLI/client read-only adapter 与
     help/reference；不得编辑 daemon evaluator、Skill 或测试断言；依赖 5A；
   - **5C Skill/discovery**：只实现 `.agents/skills/cw-task-loop/` 的发现入口、角色卡渲染和
     用户文档；不得调用 mutation，也不得编辑 daemon/CLI 实现或测试；
   - **5D 独立测试与证据**：只新增/维护 §6 的 E2E、并发、ABA、fencing、迁移与零写入验收，
     **不修改 0C 独占的** `tests/test_task_loop_capability_authority.py`、
     `tests/test_task_loop_gate_order.py`、
     `rust_ext/src/daemon/task_loop/capability_control_test.rs`，并保存原始证据；不得修改 5A–5C 的生产实现。

历史任务只读显示 `UNVERIFIED`，不回填或改写历史 verdict/evidence。每项都是独立任务，
并有 Role Contract、非重叠白名单、验收命令和独立 Reviewer handoff。

每一阶段都是独立任务；Skill、daemon RPC、CLI adapter 和测试不得由同一未拆分任务同时
实现。任何实现任务必须有 Role Contract、非重叠白名单、验收命令和独立 Reviewer handoff。

## 8. 冻结 schema 与剩余非阻断项

### 8.1 冻结的 task workspace、Role Contract 与 Binding Schema

#### 8.1.1 不可变 task→workspace 权威关系

v1 不向 `tasks` 或 `task_steps` 猜测性补写可变 workspace 字段，而是新增唯一、不可更新的
`task_workspace_bindings`：

```text
CREATE TABLE task_workspace_bindings (
  task_id TEXT PRIMARY KEY REFERENCES tasks(id),
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  workspace_binding_id TEXT NOT NULL UNIQUE,
  workspace_capture_id TEXT NOT NULL,
  created_by TEXT NOT NULL,
  authoritative_created_at TEXT NOT NULL,
  UNIQUE(task_id, workspace_id),
  FOREIGN KEY(workspace_capture_id, workspace_id)
    REFERENCES workspace_authority_captures(workspace_capture_id, workspace_id)
);
```

它是 task 的唯一逻辑 workspace 真相源；每个新 `task.create` 必须与这条 binding 在同一事务
写入，`task_steps` 只经 `task_id` 继承 workspace，禁止有独立 workspace 归属。这里的
`workspace_id` 是 task DB 的 `workspaces.id INTEGER`，不是 daemon registry 的同名整数，也不是
字符串 `workspace_instance_id`。

由于 task DB 与 daemon registry 是不同 SQLite 存储，v1 在 **task DB** 另持久化 append-only
`workspace_authority_captures`，不能伪造跨库 FK：

```text
CREATE TABLE workspace_authority_captures (
  workspace_capture_id TEXT PRIMARY KEY,
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  capture_revision INTEGER NOT NULL CHECK(capture_revision > 0),
  supersedes_capture_id TEXT NULL,
  daemon_workspace_id INTEGER NOT NULL,
  workspace_instance_id TEXT NOT NULL,
  capture_canonicalization_version TEXT NOT NULL,
  capture_canonicalization_rules_hash TEXT NOT NULL,
  registry_identity_payload_json TEXT NOT NULL,
  registry_identity_hash TEXT NOT NULL,
  workspace_manifest_payload_json TEXT NOT NULL,
  workspace_manifest_hash TEXT NOT NULL,
  client_view_root_hash TEXT NOT NULL,
  host_real_root_hash TEXT NOT NULL,
  created_by TEXT NOT NULL,
  authoritative_created_at TEXT NOT NULL,
  UNIQUE(workspace_id, workspace_instance_id, registry_identity_hash, capture_revision),
  UNIQUE(supersedes_capture_id),
  UNIQUE(workspace_capture_id, workspace_id)
);
```

`registry_identity_hash` 使用冻结的 `workspace-capture-c14n/v1`：canonical payload 只包含
`workspace_instance_id`、`client_view_root_hash`、`host_real_root_hash` 与
`workspace_manifest_hash`，以 UTF-8/Unicode NFC、键按 Unicode code point 排序、无额外空白的 JSON
计算 SHA-256。它**排除** registry 的 `snapshot_id`、`last_active_at`、`status`、`registered_at`、
`git_head_commit_sha`、`toolchain_fingerprint` 与自增 `daemon_workspace_id`；后者只保存作本次 registry
查找的诊断 provenance，不是稳定 workspace identity。`workspace_manifest_payload_json` 使用
`workspace-manifest-c14n/v1`，载荷固定为 task-DB `workspace_id`、workspace `name`、规范化
`root_path` hash、remote URL hash 与 manifest format version；`workspace_manifest_hash` 是其 SHA-256。
capture canonicalization 的 version/rules hash 使用 §8.1.3 registry 的独立 `workspace_capture`
domain。任一 payload/hash 无法重算时，capture 不可用。

合法的 heartbeat、activate 或 snapshot 更新只改变被排除字段，既有 capture/binding 继续可判定，
不要求重绑。需要重新登记时，daemon 仅能追加一个 payload/hash 相同的 re-attestation capture：
revision `n>1` 必须以 `supersedes_capture_id` 指向同 workspace/instance/identity hash 的 `n-1`，
否则拒绝。task binding 仍引用原 `workspace_capture_id`，运行时可接受该原 capture 或连续的同一稳定
identity re-attestation 链。任一 root、manifest 或 instance identity 改变不是“合法 liveness 更新”，旧 task 必须
`UNVERIFIED`，由真实 Executor 创建新的 task/binding，不得 UPDATE 原 binding。

任务 1D0 的 schema migration 必须先创建 `canonicalization_rule_sets`；随后任务 1A 在自己的原子
migration 中写入已验证的 `workspace_capture` rule row、创建 `workspace_authority_captures`，最后创建
`task_workspace_bindings` 并启用 foreign-key check。`task.create` 在同一 task-DB transaction 写入 binding，并要求其 `(workspace_capture_id,
workspace_id)` 指向已验证 capture；capture 由 daemon 在受保护写入前以
`workspace_instance_id` 查找当前 `daemon_workspace_id`，重算稳定 identity payload、根路径 hash 与
workspace manifest 后追加。后续 request 的 `workspace_instance_id` 必须重新经 daemon registry
验证并匹配 binding 所引用 capture 的**稳定 identity**；记录的 `daemon_workspace_id` 不参与相等判定。
任何 registry/capture/local workspace 不一致均返回 `E_WORKSPACE_AUTHORITY_MISMATCH`，不得用
`active workspace`、`active_task_id`、cwd 或客户端传入的 numeric id 补齐。删除、UPDATE 或把 task
重绑到另一 workspace 均不被 v1 支持。旧 task 仅在已有不可歧义、可验证的 workspace capture
能创建一条带 provenance 的 binding；其余旧 task 保持无 binding，所有 v1 派工、handoff、verdict
与 Gate 结果一律 `UNVERIFIED`。

#### 8.1.2 Role Contract revision 与 hash

现有 `role_contracts.contract_id` 是单行主键，不能承担“同一逻辑合同的多个 revision”的 v1
身份。因此 v1 新增 append-only `role_contract_lineages` 与 `role_contract_revisions`；旧表只作为
历史兼容来源，不能再作为 v1 授权真相源。

```text
role_contract_lineages(
  role_contract_lineage_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  workspace_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  created_by TEXT NOT NULL,
  authoritative_created_at TEXT NOT NULL,
  UNIQUE(task_id, workspace_id, role),
  FOREIGN KEY(task_id, workspace_id) REFERENCES task_workspace_bindings(task_id, workspace_id)
)

role_contract_revisions(
  role_contract_revision_id TEXT PRIMARY KEY,
  role_contract_lineage_id TEXT NOT NULL REFERENCES role_contract_lineages(role_contract_lineage_id),
  revision INTEGER NOT NULL CHECK(revision > 0),
  supersedes_revision_id TEXT NULL,
  canonical_payload_json TEXT NOT NULL,
  canonicalization_version TEXT NOT NULL,
  canonicalization_rules_hash TEXT NOT NULL,
  role_contract_hash TEXT NOT NULL,
  created_by TEXT NOT NULL,
  authoritative_created_at TEXT NOT NULL,
  UNIQUE(role_contract_lineage_id, revision),
  UNIQUE(role_contract_lineage_id, supersedes_revision_id)
)
```

revision `1` 的 `supersedes_revision_id` 为 NULL；revision `n>1` 必须只指向同 lineage 的
`n-1`，否则该 lineage 及其 binding 都是 `UNVERIFIED`。API 的 `role_contract.id` 是稳定的
`role_contract_lineage_id`，`revision_id` 是不可变 revision 身份，`revision` 与 hash 必须与该行
一致。任何 binding、handoff、verdict、Gate decision 和有效投影都持久化这四项 provenance：
`role_contract_revision_id`、`role_contract_hash`、`canonicalization_version`、
`canonicalization_rules_hash`；不得以“当前 revision”回读替换历史值。

Role Contract hash 固定为 `role-contract-c14n/v1`：payload 是 UTF-8、Unicode NFC、键按 Unicode
code point 排序、无多余空白的 canonical JSON；hash 算法固定 SHA-256，表示为 `sha256:<hex>`。
纳入 hash 的字段为 `role`、`skill_id`/`skill_version`、prompt template id/hash、allowed/forbidden
paths、commands、acceptance checks、required evidence、`handoff_to` 与 independence constraints。
路径只能是项目相对、使用正斜杠、拒绝绝对路径和 `..`；路径集合去重并排序。command、check 和
evidence 列表保留声明顺序且重复即拒绝。lineage/revision id、创建时间、创建者、`is_current` 与
其他派生显示字段不进入 hash。其 rules registry 是 §8.1.3 的持久化表，不是实现约定；未知版本、
rules hash 不匹配、payload 无法 canonicalize 或旧行缺失 provenance 均 fail closed 为
`UNVERIFIED`。

历史 `role_contracts` 只可在同时具有唯一 task workspace binding、可解析完整 payload 且可确定
revision 链时，按 `(task_id, role)` 创建有 provenance 的 lineage/revision 副本；迁移绝不 UPDATE
旧行。任一歧义（含原有空 `step_id`、缺 workspace、revision 分叉、缺 payload）不回填为当前合同，
而是保留历史行并标记相关 v1 结果 `UNVERIFIED`。

#### 8.1.3 Canonicalization rules registry

workspace capture、Role Contract 与 operation params 共用 append-only
`canonicalization_rule_sets`，其 schema 为：

```text
CREATE TABLE canonicalization_rule_sets (
  rule_set_id TEXT PRIMARY KEY,
  domain TEXT NOT NULL CHECK(domain IN ('workspace_capture', 'role_contract', 'operation_params')),
  canonicalization_version TEXT NOT NULL,
  rules_payload_json TEXT NOT NULL,
  rules_c14n_version TEXT NOT NULL,
  rules_hash TEXT NOT NULL,
  created_by TEXT NOT NULL,
  authoritative_created_at TEXT NOT NULL,
  UNIQUE(domain, canonicalization_version),
  UNIQUE(domain, rules_hash)
);

CREATE TABLE canonicalization_rule_revocations (
  revocation_id TEXT PRIMARY KEY,
  rule_set_id TEXT NOT NULL UNIQUE REFERENCES canonicalization_rule_sets(rule_set_id),
  reason TEXT NOT NULL,
  revoked_by TEXT NOT NULL,
  authoritative_revoked_at TEXT NOT NULL
);
```

`rules_hash` 固定是 `rules-c14n/v1` 对 `rules_payload_json` 的 SHA-256：UTF-8、Unicode NFC、键按
Unicode code point 排序、无额外空白的 canonical JSON，表示为 `sha256:<hex>`。`rules-c14n/v1`
是本协议文本中自举冻结的规则，不再依赖另一张 registry。任务 1D0 独占该 table 与 revocation
table 的 schema；任务 1A 只能追加 `('workspace_capture', 'workspace-capture-c14n/v1')` 初始 row，
任务 1B 只能追加
`('role_contract', 'role-contract-c14n/v1')` 初始 row，任务 1D1 只能追加
`('operation_params', 'operation-params-c14n/v1')` 初始 row，二者均不能 ALTER table。各自 migration
必须原子校验其拥有的 row；`task_handoff_v1`、`verdict.submit` 或任一 `TASK_DB_LEDGER` method 在三条
row 都存在且 hash 验证前不得启用。同 version 不同 hash、重复 payload、缺 row 或 hash 不匹配均
禁用对应 capability。rule-set row 永不 UPDATE；撤销只能向
`canonicalization_rule_revocations` 追加一条不可撤销记录，读取投影以 left join 得到 revoked 状态。
revoke 只禁止该 rule set 用于首次 operation/新 revision；历史 ledger/revision 的 read/replay 必须
仍使用其已绑定的规则，不能改写或按“最新”规则重解释。

#### 8.1.4 step→Role Contract binding 与 operation ledger

`task_step_role_contract_bindings` 是本协议唯一的 step→Role Contract 真相源；不复用目前写入
空字符串的 `role_contracts.step_id`。它的不可变 payload 是：`binding_id`（主键）、`workspace_id`、
`task_id`、`step_id`、`role_contract_lineage_id`、`role_contract_revision_id`、
`role_contract_revision`、`role_contract_hash`、`canonicalization_version`、
`canonicalization_rules_hash`、`binding_revision`、`supersedes_binding_id`、`created_by` 与
`authoritative_created_at`。它以 `(task_id, workspace_id)` 外键指向 task workspace binding，以
revision id 外键指向 Role Contract revision；写入事务必须重检三者 task/workspace/role 一致。
唯一约束为 `UNIQUE(workspace_id, task_id, step_id, binding_revision)`；revision>1 必须指向同一
step 的 `n-1`，最高连续 revision 才是 current binding。索引固定为
`(workspace_id, task_id, step_id, binding_revision DESC)` 与
`(role_contract_revision_id)`。跨 workspace、跨 task、分叉或断链一律拒绝写入；读到它们即
`UNVERIFIED`。

为落实 §4.3，新增 task-DB 权威 `task_operation_ledger`：

```text
CREATE TABLE task_operation_ledger (
  workspace_instance_id TEXT NOT NULL,
  method TEXT NOT NULL,
  request_id TEXT NOT NULL,
  params_canonicalization_version TEXT NOT NULL,
  params_canonicalization_rules_hash TEXT NOT NULL,
  canonical_params_hash TEXT NOT NULL,
  workspace_id INTEGER NULL REFERENCES workspaces(id),
  task_id TEXT NULL REFERENCES tasks(id),
  role_contract_revision_id TEXT NULL,
  role_contract_hash TEXT NULL,
  role_contract_canonicalization_version TEXT NULL,
  role_contract_canonicalization_rules_hash TEXT NULL,
  response_or_error_json TEXT NOT NULL,
  authoritative_created_at TEXT NOT NULL,
  PRIMARY KEY(workspace_instance_id, method, request_id)
);
```

`operation-params-c14n/v1` 对请求 envelope 之外的完整 method payload 计算 hash：`request_id` 与
`workspace_instance_id` 已在 key 中，不能重复进入 payload；其余字段按 method contract 形成完整
payload，递归 UTF-8/Unicode NFC 后按 RFC 8785 JSON Canonicalization Scheme 序列化，再计算
`sha256:<hex>`。首次 key 使用当前未 revoked 的 operation-params rule set；已存在 key 必须使用
该 ledger 行保存的 version/rules hash 重新 canonicalize incoming payload，不能因 daemon 升级改用
新规则。找不到绑定的 rule set、c14n 失败或 rules hash 不匹配均 fail closed，且不得把重试误写成
新的 operation。

duplicate JSON key 检测不能在已解析的 `serde_json::Value` 上事后声称完成。任务 1D2 必须在每个
生产 transport 的**原始 JSON 解析边界**先使用 strict duplicate-key parser：HTTP 在 body bytes 转成
`Value` 前，Named Pipe 与 UDS 在 frame bytes 转成 `Value` 前。检测到 duplicate key 时返回稳定
`E_DUPLICATE_JSON_KEY`，不进入 route/dedup/dispatch，也不写任一 ledger；只有带
私有 `StrictParsedEnvelope` 的 parsed payload 才可交给 `TaskMutationExecutor`。该 type 不实现客户端
JSON 反序列化，marker 也不存在于 params；直接 in-process typed call 只能走同一 crate 内受限
constructor，构造无重复键的 typed map。任务 1D2 的验收必须覆盖 HTTP、Named Pipe、UDS 三种生产
入口及同 key 正常重试；任何绕过 strict parser 的入口 fail closed。

同 key 同 hash 只读重放这行的结果；同 key 不同 hash 绝不修改该行并返回
`E_REQUEST_ID_REUSE_MISMATCH`。首次 request 的确定性领域拒绝可只提交该行错误结果；成功或
非确定性失败分别与全部 domain 写入同事务提交或回滚。该 table 是其 §4.3 method scope 内唯一
权威 operation/dedup ledger；HTTP server cache 不能覆盖或替代它。非 task-domain Protected
Mutation 的 HTTP transport ledger 仍保持冻结契约要求的持久化与恢复语义，且不受本表降级影响。

#### 8.1.5 Public promotion 权威幂等账本

`task_loop.public_promote` 不属于 `task_operation_ledger` 的 task-domain method scope，故它必须使用
独立、append-only 的 control-plane 真相源，不能借用 HTTP cache 或仅靠内存 permit：

```text
CREATE TABLE task_loop_capability_promotion_events (
  promotion_event_id TEXT PRIMARY KEY,
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  method TEXT NOT NULL CHECK(method = 'task_loop.public_promote'),
  request_id TEXT NOT NULL,
  params_canonicalization_version TEXT NOT NULL,
  params_canonicalization_rules_hash TEXT NOT NULL,
  canonical_params_hash TEXT NOT NULL,
  canonical_request_json TEXT NOT NULL,
  control_plane_action_identity_json TEXT NULL,
  control_plane_action_identity_hash TEXT NULL,
  capability_authority_id TEXT NULL,
  capability_authority_revision INTEGER NULL,
  capability_authority_fencing_counter INTEGER NULL,
  internal_permit_fingerprint TEXT NULL,
  task4_evidence_id TEXT NULL,
  task4_evidence_hash TEXT NULL,
  schema_fingerprint TEXT NULL,
  runtime_binary_hash TEXT NULL,
  daemon_generation TEXT NULL,
  outcome_kind TEXT NOT NULL CHECK(outcome_kind IN ('authorized', 'deterministic_error')),
  durable_outcome_json TEXT NOT NULL,
  authoritative_created_at TEXT NOT NULL,
  UNIQUE(workspace_id, method, request_id),
  CHECK(
    outcome_kind != 'authorized' OR (
      control_plane_action_identity_json IS NOT NULL AND control_plane_action_identity_hash IS NOT NULL AND
      capability_authority_id IS NOT NULL AND capability_authority_revision IS NOT NULL AND
      capability_authority_fencing_counter IS NOT NULL AND internal_permit_fingerprint IS NOT NULL AND
      task4_evidence_id IS NOT NULL AND task4_evidence_hash IS NOT NULL AND
      schema_fingerprint IS NOT NULL AND runtime_binary_hash IS NOT NULL AND daemon_generation IS NOT NULL
    )
  )
);
```

`capability_authority_*` 的字段语义、FK/API 与 revoked/expired 的权威判定**不由本派生协议定义**；它们只能
引用 0A 修订后的冻结三件套和 0B 已验证的 daemon authority store。0A 未完成、0B coverage preflight 不通过、
0C 未提供匹配 daemon/runtime 原始证据、
或该 authority 不可读时，任何 `authorized` promotion 都禁止写入，public route 保持 disabled。

该 table 的 canonical payload 使用已冻结且未撤销的 `operation-params-c14n/v1`；key 中的
`workspace_id`、`method`、`request_id` 不进入 payload；完整的 canonical input 则原样保存为
`canonical_request_json`，使缺 control-plane identity、Capability Authority、evidence、runtime 或 generation 等
不完整 request 仍可被审计和确定性重放。`control_plane_action_identity_hash` 使用相同 rules 对实际存在的
identity payload 计算，以便审计内容可复算。仅 `authorized` row 必须通过 DDL `CHECK` 具备全部提取
provenance；`deterministic_error` row 可保留 NULL，错误细节只能在 `durable_outcome_json` 中解释，不能伪造
缺失绑定。foundation（1D0）独占该 migration、唯一索引、`capability_control.rs` store 与 control-plane
serialization；其他任务不得直接 INSERT 或 ALTER。

promotion 在单一 task-DB transaction/daemon control-plane serialisation point 按以下不可变顺序执行：

1. 先查询 `(workspace_id, method, request_id)`。首次 key 使用当前未撤销的
   `operation-params` rule set；已存在 key 必须使用该 event 行保存的
   `params_canonicalization_version/rules_hash` 重算 incoming payload——即使该 ruleset 后来撤销或已升级。
   同 hash 只重放该 row 的 `durable_outcome_json`，并只读报告当前 generation 对该 event 的
   `permit_installation=(installed|not_installed)`，不同 hash 优先返回
   `E_REQUEST_ID_REUSE_MISMATCH`，二者都不安装 permit；
2. 首次 key 才重新验证 control-plane identity、Capability Authority id/revision/fencing/validity、Internal permit、任务 4
   evidence、schema/rules/runtime hash 与当前 daemon generation。确定性不满足时只 INSERT 一条
   `deterministic_error` row（含完整 canonical request 与可取得的 provenance）并 commit；
3. 任一数据库、Authoritative Clock 或 authority-read 基础设施失败均回滚该 transaction，既无 event 也无
   permit；
4. 成功时 INSERT 一条完整的 `authorized` row 并先 commit。只有该 commit 成功后，才由
   `capability_control.rs` 安装当前 generation 的易失 `PublicPreflightPermit`。安装成功的首个响应才返回
   `authorized/installed`；安装失败返回 `authorized/not_installed`，不得宣称 public capability 可用。
   historical `authorized` row 不能在重启、丢失响应后的普通重放或安装失败后的同 key 重试时自动安装
   permit；`not_installed` 只能由新的显式 promotion request 通过完整 fresh validation 解决。

因此 audit event 与 permit 的边界是“durable authorization 先于 volatile installation”：崩溃最多留下
可审计但未安装的授权记录，绝不留下未经审计的可用 permit，也不允许历史事件越过当代 authority 恢复
public route。

`task_verdict_events` 另增加 `step_id`、`role_contract_lineage_id`、`role_contract_revision_id`、
`role_contract_revision`、`role_contract_hash`、`canonicalization_version` 与
`canonicalization_rules_hash`；其既有 `contract_*` 仍只表示 Task Contract。Gate provenance 使用
同一组 Role Contract revision 字段，不得把 Task Contract 的 hash 混作 Role Contract hash。

`verdict_normalization_rules` 也必须是具体的 append-only registry，而非 JSON 文本约定：

```text
CREATE TABLE verdict_normalization_rules (
  verdict_rule_set_id TEXT PRIMARY KEY,
  normalization_version TEXT NOT NULL UNIQUE,
  rules_payload_json TEXT NOT NULL,
  rules_c14n_version TEXT NOT NULL,
  rules_hash TEXT NOT NULL UNIQUE,
  created_by TEXT NOT NULL,
  authoritative_created_at TEXT NOT NULL
);

CREATE TABLE verdict_normalization_rule_revocations (
  revocation_id TEXT PRIMARY KEY,
  verdict_rule_set_id TEXT NOT NULL UNIQUE
    REFERENCES verdict_normalization_rules(verdict_rule_set_id),
  reason TEXT NOT NULL,
  revoked_by TEXT NOT NULL,
  authoritative_revoked_at TEXT NOT NULL
);
```

任务 1E 以 `rules-c14n/v1` 原子插入/校验 `verdict-normalization/v1` 初始 row；撤销只追加其
revocation row。Task Contract、Gate decision 和 verdict projection 均保存该 row 的 version/hash；
首次 evaluation 禁止使用已撤销 row，历史读取只能按原 row 重放。缺 row、hash 不匹配或 revoked
时保持 `UNVERIFIED`，不以最新规则替代。

不存在跨 1D0–1D3、1A–1F 的单一 schema transaction。每个子任务只能提交自己拥有的**单调、原子** schema
migration：该 migration 内的 table/column/index/rule-row 要么全部提交、要么全部回滚，且不得删除或
UPDATE append-only 历史。所有 capability 先受由 **1D3A 独占实现**的
`task-loop-schema-preflight/v1` 联合门禁：它只读核对
0A authority contract、0B writer inventory/gate coverage、1D0–1D3、1A–1F 所有要求的 table、column、FK/index、`task_loop_capability_promotion_events` 的唯一 key、route、initial rule row、rules hash 与历史 fail-closed
投影；任一缺失、冲突、revoked 或 hash 不匹配即禁用相关 capability，`next_action` 返回
`E_ROLE_BINDING_UNAVAILABLE`。该检查只可发布 Internal permit；**1D3B** 还必须验证任务 4 已拒绝
旧 SQLite fallback，才可将同一 fresh fingerprint promote 为 Public permit。回滚是禁用 capability
的前向兼容操作，不删除 schema 或历史行。

### 8.2 仍待客户端 discovery 验证的非阻断项

1. 角色提示词/Skill 的项目安装根是仓库 `.agents/skills` 还是 Codex 用户级 skills；
   必须由实际客户端 discovery 验证，不能仅凭文件存在宣称可通过 UI 调用；
2. 任务依赖应以显式 dependency 表为准；在此之前 HTTP H1/H2/H2I 等关系只能通过冻结
   Role Contract 解析，禁止由 title 匹配推断。

> **验证结论（2026-08-19，5D 收尾）**：
> 1. 已在实际客户端（TRAE）完成 discovery 验证：项目 `.agents/skills/cw-task-loop/`
>    会被客户端自动同步到用户级 `~/.trae-cn/skills/cw-task-loop/`（SHA-256 逐字节一致），
>    Skill 工具列表暴露该 skill，并可实际调用（只读 `cw task next-action --json`，
>    不触发 mutation）。证据见 `.e2e_5d/discovery_verification.md`。
> 2. 已确认冻结 schema 无显式 dependency 表，evaluator 无 title/LIKE 匹配；
>    HTTP H1/H2/H2I 角色关系只经冻结 Role Contract（role_contract_lineages /
>    role_contract_revisions / task_step_role_contract_bindings）解析（5D 进程级证据）。
