# Call Warden Role Prompt Compiler v1 方案修订 R3

> 状态：Planner 事实裁决稿，待独立 Reviewer 审查。
> 基线：R1 `cw-role-prompt-compiler-v1-implementation-plan.md` + R2
> `cw-role-prompt-compiler-v1-plan-amendment-r2.md`。
> 修订方式：append-only。R1/R2 保留历史；与本稿冲突时以本稿为准。
> 本稿只冻结方案和任务边界，不代表生产代码、task state、capability 或正式 verdict 已发生变化。
> 本稿也不是 Executor 的最终多文档拼装入口。R3 经独立 Reviewer 通过后，RP-00 必须生成一份
> 自包含的 merged frozen spec；该规范通过前不得释放任何生产实现卡。

## 1. Planner 最终判断

R1 的根本方向正确：模板属于 daemon 版本化资源，角色和下一动作只由 daemon authority 决定，
Python CLI/MCP/Skill 只能做 HTTP 薄客户端，Prompt Compiler 不替代 claim、lease、verdict、apply 或 close。

R2 试图补齐“技术问题不得推给用户”的规则，但引入了过多尚无 authority 的新概念：

- 12 类 `escalation.classification` 与不存在的 `governance_classifier`；
- 当前不可达的 `decision_request.md`；
- 本期没有消费者的 `model_requirements`；
- 包含自身 hash 的 `provenance_binding.source_bundle_hash`；
- public preview/mode/profile 造成的授权与 hash 组合爆炸。

这些问题不应继续靠 R4、R5 小补丁堆叠。R3 直接收缩 v1：

> **调用方只提交精确 `task_id`；daemon 在同一只读 authority snapshot 内解析 task binding、
> `task.next_action`、Role Contract 和模板，返回一个永远不能代替 claim 的 Role Prompt Bundle。**

public API 不再接受 role、mode、preview、context profile 或客户端推导出的 workspace instance。
这样才真正实现用户最初要解决的问题：只说“处理 `T-...`”，系统就能告诉 Agent 当前是谁、要做什么、
允许做什么、完成后交给谁，而不是继续要求用户手工拼角色提示词。

## 2. 已核验的当前事实

以下结论来自当前源码和 live daemon，而不是来自 Reviewer 文本。

### 2.1 Epic authority 仍未闭合

- 当前 daemon 注册 authority：`workspace_id=1087`、
  `workspace_instance_id=4baea3ff12c2ea5c`、root=`C:\git_work\callwarden`。
- 对 Epic `T-1787203926824-9f873bfc` 执行 `task.next-action` 返回
  `E_WORKSPACE_AUTHORITY_UNAVAILABLE`：Epic 没有不可变 workspace binding。
- 因此“re-attest 到 `b9515f7c28f5d0f0`”不是可采纳结论。该值是某种客户端路径 hash，
  既不是 live daemon 返回值，也不能替代正式 workspace authority capture。
- 正确前置是：使用 daemon 当前返回的 authority、一个已绑定 anchor task、真实 reviewer lease 和
  `task attest-legacy-workspace-binding` 完成正式 attestation；不得硬编码任何路径 hash。

### 2.2 当前 `task.split` 不能承担增量微任务导入

- public 参数只有 `task_id`、`subtasks`/`plan_file` 和 `identity_policy`；R1 envelope 中的
  `idempotency_key`、`binding_requirements`、`contract_defaults`、`child_release_policy`
  不是 RPC 字段。
- 每次调用都从 `<parent>-sub-1` 开始生成 ID。Epic 已存在
  `T-1787203926824-9f873bfc-sub-1`，再次增量 split 会发生 ID 冲突。
- CLI 仍保留 `db.task_split(...)` local fallback；enterprise/daemon 任务不得使用该路径。
- split 能原子写 binding、legacy 三角色合同和 Task Contract，但默认 policy 仍是
  `legacy_identity_v1`，不能据此声称它已支持任意新任务合同。

### 2.3 已有子任务创建入口不完整

- daemon `task.create` 已支持 `parent_id`、显式 workspace、Role Contracts、Task Contract bootstrap
  与 `identity_policy`；这是应复用的正式创建 domain。
- 当前 CLI `cw task create` 没有 `--parent`、`--workspace-instance-id` 或完整 Contract envelope 参数。
- daemon/MCP `task.create_subtask` 只创建 task、binding、event 和 steps；没有创建 Role Contracts、
  Task Contract revision、identity policy 或 step bindings，不能用于治理任务树。
- 因此，在导入 Prompt Compiler 微任务前，必须先补“parent-aware governed task.create”薄客户端能力，
  不能直连 SQLite，也不能用残缺的 `task.create_subtask`。

### 2.4 `get_role_view` 不能复用为 Prompt Compiler

现有 `get_role_view`/`role_view.get` 是 blind Role_View：

- 由调用方传 role，缺省为 legacy `implementer`；
- 只按 allowlist 过滤最新 Contract envelope；
- 未知 task 也可返回空投影；
- 已有 golden parity 和兼容契约。

Prompt Compiler 的语义是“daemon 根据当前 next action 选择角色并生成执行提示词”。把它塞进
`get_role_view` 会改变已有工具语义、继续允许客户端选角色，并让未知 task 的空投影混入治理流程。
R3 明确保留 `get_role_view`，新增独立 RPC/MCP 工具。

### 2.5 route matrix 当前已经漂移

- `scripts/gen_route_matrix.py` 没有 `--emit-rust`/`--check-rust`。
- `scripts/verify_route_matrix.py` 把总数硬编码为 239。
- 当前 `tool_migration_matrix.json` 实际声明/包含 241 项，MCP 注册提取为 242 项；live verifier 返回
  4 errors + 3 warnings，而不是“239/239 全绿”。其中至少包括 3 个 registered-but-unlisted 与
  2 个 matrix-but-registration-unresolved 条目，必须逐项裁决，不能只改数字让测试变绿。
- `gen_route_matrix.py` 仍把 `get_role_view` 标成 Python compat，而
  `route_matrix.rs` 已标成 Rust native。
- 因此新 MCP 工具不能直接手改进 Rust mirror；必须先恢复单源生成与语义验证。

### 2.6 历史 Role Contract 已冻结 legacy prompt hash

当前默认三角色合同不是抽象模板引用，而是精确绑定了历史文件的 SHA-256：

| role | template ID | 已核验 SHA-256 | byte source |
| --- | --- | --- | --- |
| Executor | `cw.aprime.executor.startup.v1` | `59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6` | `deliverables/software-company/aprime_role_contracts/executor_planner_startup_v1.md` |
| Reviewer | `cw.aprime.reviewer.startup.v1` | `6415033D8F134392DE16FCA130BFB762CB6C70D9F466C770EC18A20FC4CE139E` | `deliverables/software-company/aprime_role_contracts/reviewer_startup_v1.md` |
| Adjudicator | `cw.aprime.adjudicator.startup.v1` | `42A5F1DEFA81008B009058C1BAF5D1A14B3EF4521E291B7B55C19BB473A77C3E` | `deliverables/software-company/aprime_role_contracts/adjudicator_startup_v1.md` |

当前 v4 模板 hash 与上述值不同。Prompt Compiler 不得用新模板正文冒充旧 ID/hash；否则几乎所有历史
Role Contract 都会失真。R3 要求 manifest 同时保留 byte-exact legacy assets，并把当前 daemon 编译策略、
合同模板和动态 context 分开哈希。新模板只能由新建或 append-only Contract revision 显式引用。

## 3. 多份评审意见裁决

### 3.1 采纳

| finding | 裁决 | R3 处理 |
| --- | --- | --- |
| 文本应在 JSON escaping 前截断 | 采纳 | 在 Unicode 逻辑字符串层裁剪，再 escape/serialize；记录原始 hash 与省略字节数 |
| `prompt_template_id` 缺失/空串/未知未区分 | 采纳 | 缺失或空串统一 `E_TASK_PROMPT_TEMPLATE_ID_REQUIRED`；未知非空值为 `E_TASK_PROMPT_TEMPLATE_NOT_FOUND` |
| R2 `source_bundle_hash` 自引用 | 采纳 | 删除整个 v1 `provenance_binding`；top-level `bundle_hash` 是唯一 bundle 标识 |
| R2 classifier 没有 daemon 落点 | 采纳 | v1 删除 classifier 和 12 类枚举；只编译 next-action 的权威结果 |
| build/runtime 双解析会漂移 | 采纳 | build-time validator 生成 Rust 常量；runtime 只消费生成物，不再解析第二遍 |
| `task.split` schema 与 envelope 不符 | 采纳 | 先补 parent-aware governed create；R1 YAML 禁止直接导入 |
| hash 输入/排除集合不闭合 | 采纳 | §7 给出唯一 include/exclude 清单，禁止使用“等” |
| oversized 字段没有分级 | 采纳 | authority/Contract 标识不截断、超限硬失败；可选不可信文本确定性裁剪 |
| RP-04/05 与 RP-08 部署语义冲突 | 采纳 | RP-05～RP-09 只用隔离临时 daemon；共享 runtime 只在最终 Gate 部署 |
| RP-08 excluded paths 不是路径 | 采纳 | 路径只写 glob；策略迁到 `forbidden_actions` |
| RP-00 Planner pre-cutover 无原生合同 | 采纳 | RP-00 由 Executor docs-only Contract 执行，Planner 只承担设计责任 |
| incremental child create 未验证 | 采纳 | 列为 feature task tree 的硬前置，不再假设 split 可增量 |
| route matrix 生成源缺失 | 采纳 | 单独微任务恢复 generator→JSON/Rust 双产物和 verifier 语义门禁 |

### 3.2 改写后采纳

| finding | 原建议问题 | R3 裁决 |
| --- | --- | --- |
| preview 需额外防越权字段 | 仍保留了不必要的 public preview 攻击面 | v1 删除 public preview/mode；模板设计预览只在离线 fixture/test 工具中发生 |
| hard error 一律 terminal | DB busy、daemon unavailable、stale read 等是暂态 | 返回 `retry_class`；禁止盲重试，但允许明确、有限次重试 |
| decision request 模板必须含选项/推荐/后果 | 原则正确，但 `decision_request_v1` 尚未实现，v1 没有权威数据源 | v1 不发布可达 decision 模板；要求留给 successor capability |
| RP-01 跨 ownership，应继续加说明或拆卡 | 单纯按文件语言划 owner 也会过度拆分 | 拆成“资产编译器/build gate”和“production templates”两卡，均保持单一语义 owner |
| provenance 字段排除 hash 可解决自引用 | 仍留下无消费者的占位对象 | v1 直接删除该对象；未来 issuance ledger 绑定 top-level bundle hash |
| waiting 与 decision route 要定优先级 | 建立在 v1 不可达 decision route 上 | v1 只有 WAITING→waiting；future capability 再追加明确优先级 |

### 3.3 拒绝或延后

| finding | 裁决 | 原因 |
| --- | --- | --- |
| 用 `CW_ALLOW_DRAFT_TEMPLATES=1` 绕过 build gate | 拒绝 | 环境变量可泄漏到 CI/release，破坏 fail-closed；draft 资源应放在 production manifest 外 |
| v1 冻结 `model_requirements` | 延后 | 当前没有 worker model admission/dispatcher authority；提前冻结只会制造未执行却看似受控的字段 |
| CLI 自行用 project-root hash 推导 workspace instance | 拒绝 | live authority 为 `4ba...`，与建议 hash 不一致；客户端推导会重演 phantom workspace |
| Planner/任意四角色 public preview | 拒绝 | 生产 API 的目标是当前权威派工，不是模板 IDE；离线 golden fixture 足够 |
| 把所有 blocked reason 映射到 12 类 escalation | 拒绝 v1 | Prompt Compiler 不应创造新的治理 classifier；分类应由未来 decision/governance capability 产出 |
| 复用 `get_role_view` 以避免新增 MCP tool | 拒绝 | 破坏现有 blind view 契约并继续让客户端选择 role |
| “R1+R2 可 PASS，仅需小修” | 拒绝 | 自引用 hash、Epic 无 binding、创建入口和 route matrix 均是实施阻断，不是 minor refinement |

## 4. R3 public contract

### 4.1 唯一 RPC

```json
{
  "method": "task.prompt.compile",
  "params": {
    "task_id": "T-...",
    "expected_workspace_instance_id": "optional-daemon-returned-guard"
  }
}
```

规则：

1. `task_id` 是唯一必填业务参数。
2. `expected_workspace_instance_id` 只用于调用方已有 authority 时做 CAS guard；普通用户/Agent 不填。
3. 禁止参数：`role`、`mode`、`preview_role`、`context_profile`、`format`、identity、credential、
   lease token、fencing counter。
4. daemon 从 task immutable binding 解析 workspace；没有唯一 binding/capture 时 fail closed。
5. daemon 在同一只读 transaction/snapshot 中调用内部 next-action evaluator 和 Prompt Context builder；
   禁止从 handler 递归发送第二次 JSON-RPC。
6. 该 RPC 零数据库写入、零 lease、零 claim、零 assignment mutation。

### 4.2 CLI

```powershell
python C:/git_work/callwarden/cw.py task prompt T-...
python C:/git_work/callwarden/cw.py task prompt T-... --format json
python C:/git_work/callwarden/cw.py task prompt T-... --format card
```

`--format` 只影响本地展示，不发送给 daemon、不进入 hash。CLI 不计算 workspace、不选择 role、
不加载模板、不降级到 SQLite。

### 4.3 MCP

```text
task_get_role_prompt(task_id: str) -> RolePromptBundle
```

这是一个新增工具，不复用 `get_role_view`。RP-07 必须先逐项 reconciliation 得到权威基线 `N`；
引入后总数必须恰为 `N+1`。这是受控功能新增，不是覆盖率回归。route matrix 的期望总数必须由
单一生成源导出，禁止继续在 verifier 中硬编码 239、241、242 或其他常量。

### 4.4 Skill

`$cw-task-loop TASK_ID` 调用 `task.prompt.compile`，然后原样输出 `prompt.text` 和结构化摘要。
capability 未声明时可回落到现有**只读** next-action role card，但不得在 Skill 本地渲染生产提示词。

## 5. v1 Role Prompt Bundle

```json
{
  "schema_version": "role_prompt_bundle_v1",
  "task_id": "T-...",
  "prompt_kind": "role_work|blocked_recovery|waiting|terminal",
  "authority": {
    "workspace_id": 1087,
    "workspace_instance_id": "4ba...",
    "workspace_binding_id": "B-...",
    "workspace_capture_id": "C-...",
    "snapshot_id": null,
    "source_event_watermark": 123
  },
  "routing": {
    "decision": "READY|BLOCKED|WAITING|COMPLETE",
    "action": "CLAIM|REVISE|REVIEW|ADJUDICATE|WAIT|NONE",
    "required_role": "executor|reviewer|adjudicator|null",
    "next_action": "...",
    "step_id": "S-...|null"
  },
  "contract": {
    "task_contract_id": "TC-...|null",
    "task_contract_revision": 1,
    "task_contract_hash": "sha256:...|null",
    "role_contract_revision_id": "RCR-...|null",
    "role_contract_hash": "sha256:...|null",
    "role_contract_prompt_template_id": "...|null",
    "role_contract_prompt_hash": "sha256:...|null",
    "identity_policy_status": "resolved|unresolved|not_applicable"
  },
  "template": {
    "source": "role_contract|system",
    "body_template_id": "...",
    "body_template_hash": "sha256:...",
    "compiler_policy_id": "cw.role_prompt.compiler_policy.v1",
    "compiler_policy_hash": "sha256:...",
    "manifest_hash": "sha256:..."
  },
  "authorization": {
    "routing_state": "action_ready|non_actionable",
    "valid_for_claim": false,
    "mutation_recheck_required": true
  },
  "omissions": [],
  "context_hash": "sha256:...",
  "prompt": {
    "text": "...",
    "sha256": "sha256:..."
  },
  "bundle_hash": "sha256:...",
  "generated_at": "display-only"
}
```

重要语义：

- `valid_for_claim` 永远是 `false`。Prompt 可以指导 Agent 调用 claim，但绝不是 claim 票据。
- 即使 `routing_state=action_ready`，Agent 仍须重新调用 `next-action` 并按 daemon 返回值领取。
- v1 没有 `executable=true`，避免把“可读的工作提示词”误当成 mutation 授权。
- v1 没有 `provenance_binding` 和 `model_requirements`。
- `generated_at` 只用于显示，不参与任何 hash。
- role work 的 `body_template_*` 必须与 Role Contract 的 prompt reference 精确一致；system prompt 则绑定
  exact system template ID/hash。daemon 的 compiler policy 只能增加
  fail-closed 约束和安全 context，不能扩大合同 scope。

## 6. 模板选择状态机

| next-action authority | 模板来源 | prompt kind | 结果 |
| --- | --- | --- | --- |
| `READY/CLAIM`、`READY/REVISE` | 当前 Executor Role Contract 的 `prompt_template_id` | `role_work` | action-ready |
| `READY/REVIEW` | 当前 Reviewer Role Contract 的 `prompt_template_id` | `role_work` | action-ready |
| `READY/ADJUDICATE` | 当前 Adjudicator Role Contract 的 `prompt_template_id` | `role_work` | action-ready |
| `BLOCKED/*` | daemon system manifest 的 `cw.system.blocked_recovery.v1` | `blocked_recovery` | non-actionable |
| `WAITING/*` | daemon system manifest 的 `cw.system.waiting.v1` | `waiting` | non-actionable |
| `COMPLETE/*` | daemon system manifest 的 `cw.system.terminal.v1` | `terminal` | non-actionable |
| `READY/PLAN` | 仅当 `planner_governance_v1` live 且 Planner Role Contract 已绑定 | `role_work` | future-enabled |
| 未知 decision/action 组合 | 无 | 无 | `E_TASK_PROMPT_UNSUPPORTED_ACTION` |

Role Contract template ID 规则：

1. 字段不存在、不是字符串或 trim 后为空：`E_TASK_PROMPT_TEMPLATE_ID_REQUIRED`。
2. `prompt_hash` 缺失或不是合法 SHA-256：`E_TASK_PROMPT_TEMPLATE_HASH_REQUIRED`。历史
   64-hex 与 `sha256:<64-hex>` 两种 wire form 均可读取，比较前统一规范为小写 `sha256:` 形式；
   其他算法、长度或字符集一律拒绝。
3. 非空但 manifest 无 exact ID/hash：`E_TASK_PROMPT_TEMPLATE_NOT_FOUND`。
4. manifest 中 byte content hash 与 Role Contract `prompt_hash` 不一致：
   `E_TASK_PROMPT_TEMPLATE_HASH_MISMATCH`。
5. manifest entry 的 role/action/capability 与 next-action 不匹配：
   `E_TASK_PROMPT_TEMPLATE_ROUTE_MISMATCH`。
6. 不允许 fallback 到默认 executor 模板，也不允许把 v4 正文伪装成 legacy v1 hash。

### 6.1 prompt 组合顺序

v1 不把 task description 等不可信文本插值进 Role Contract 模板正文。最终 prompt 按固定顺序组成：

1. daemon-owned `compiler_policy`：角色边界、authority/staleness、prompt 不构成 claim 授权；
2. byte-exact Role Contract template（role work）或 system template（blocked/waiting/terminal）；
3. daemon 生成的 canonical authority/context appendix；
4. 固定 mutation recheck footer。

动态数据只进入第 3 段的受标记 JSON block。v1 不实现通用 placeholder engine，因此 Reviewer 提出的
decision placeholder 完整性问题留给 `decision_request_v1`，不会成为当前 production parser 的隐式分支。

### 6.2 blocked 不发明人类升级分类

v1 的 `blocked_recovery` 固定表达当前权威 blocking reasons 和内部恢复纪律：

- 技术、数据、环境、Contract、binding、auth/capability 问题不得交给用户手工修复；
- 提示词要求内部角色定位根因并形成唯一安全恢复任务；
- 不输出 credential、token、secret、原始 lease 或恢复密钥；
- 不伪造 Planner assignment，也不声称 `planner_governance_v1` 已启用。

若未来 next-action 指向 `user`，但 daemon 没有 persisted `decision_request_v1` 事件、选项、推荐、风险和
后果，compiler 返回 `E_TASK_PROMPT_DECISION_REQUEST_REQUIRED`，不得根据聊天文本自行生成选择题。
`decision_request.md` 留给 successor capability，不进入 v1 production manifest。

## 7. 确定性、截断与 hash

### 7.1 文本处理顺序

1. 读取逻辑 Unicode 字符串。
2. 计算原始 UTF-8 byte length 与 SHA-256。
3. 仅对允许省略的字段按 UTF-8/Unicode scalar 安全边界裁剪。
4. 记录 `omissions[{field, original_bytes, kept_bytes, original_sha256}]`。
5. 再进行 JSON escaping 与 serialization。
6. 最后包裹 `<CW_UNTRUSTED_TASK_DATA encoding="json-string-v1">`。

禁止截断已经序列化的 JSON byte stream。

### 7.2 不得截断的字段

task/workspace/binding/capture/snapshot/step ID、Contract/Role Contract ID/revision/hash、identity policy、
decision/action/role/next_action、template ID/version/hash、event watermark。任何字段超出 schema 上限均硬失败。

### 7.3 可确定性裁剪的字段

title、description、blocking reason 展示文本、evidence note 摘要、非权威说明文字。
原始内容的 hash 必须进入 omissions，使两个不同长文本不会因相同前缀得到相同 bundle。

### 7.4 `context_hash` include

唯一包含集合：

- `schema_version`；
- task ID；
- authority 的 workspace/binding/capture/snapshot/watermark；
- routing 全部字段；
- Contract 全部字段；
- 模板 ID/version/hash/manifest hash；
- Role Contract prompt ID/hash 与 compiler policy ID/hash；
- canonicalized clipped context；
- canonicalized omissions；
- `authorization.valid_for_claim=false` 与 `mutation_recheck_required=true`。

### 7.5 `bundle_hash` include

- `schema_version`；
- `task_id`；
- `prompt_kind`；
- `context_hash`；
- `prompt.sha256`；
- body template ID/hash、compiler policy ID/hash、manifest hash；
- source event watermark；
- canonical omissions。

### 7.6 明确排除

- `generated_at`；
- JSON-RPC `request_id`；
- HTTP/transport trace ID；
- CLI display format；
- retry metadata；
- `bundle_hash` 自身。

禁止使用“时间字段等”这类开放式表述。

## 8. 模板资产与 build gate

### 8.1 资源位置

```text
rust_ext/resources/role_prompts/v1/manifest.json
rust_ext/resources/role_prompts/v1/compiler_policy.md
rust_ext/resources/role_prompts/v1/legacy/executor_planner_startup_v1.md
rust_ext/resources/role_prompts/v1/legacy/reviewer_startup_v1.md
rust_ext/resources/role_prompts/v1/legacy/adjudicator_startup_v1.md
rust_ext/resources/role_prompts/v1/current/executor_v4.md
rust_ext/resources/role_prompts/v1/current/reviewer_v4.md
rust_ext/resources/role_prompts/v1/current/adjudicator_v4.md
rust_ext/resources/role_prompts/v1/current/planner_v1.md
rust_ext/resources/role_prompts/v1/system/blocked_recovery.md
rust_ext/resources/role_prompts/v1/system/waiting.md
rust_ext/resources/role_prompts/v1/system/terminal.md
```

legacy 三文件必须与 §2.6 的 historical byte source/hash 完全一致。current 模板使用新 ID/hash；只有
Role Contract 明确引用时才可使用。Planner current 模板可进入编译资产，但 production route 必须受
`planner_governance_v1` 门禁；当前只能由离线 fixture 验证，不可通过 public preview RPC 触达。

RP-09 cutover 后，仓库根目录的 v4 启动文档和 Skill 不再复制 production prompt 全文，只保留入口说明、
template ID/hash 查询方法和 capability fallback；production manifest 是 runtime 模板单源。

### 8.2 唯一 parser/validator

- `rust_ext/build_support/role_prompt_validator.rs` 提供无 IO 的 parser/validator/canonical 函数。
- `build.rs` 读取 production manifest/模板，调用该函数并生成 `OUT_DIR/role_prompts_generated.rs`。
- runtime 只 `include!` 生成常量，不再次解析文件或实现第二套 validator。
- tests 通过 `#[path]` 或独立小 crate 复用同一 validator core。

### 8.3 无开发逃生舱

production manifest 中任何模板错误都使 `cargo check/build/test` 失败。禁止
`CW_ALLOW_DRAFT_TEMPLATES` 一类绕过变量。草稿必须放在 manifest 不扫描的目录，或保持 schema 合法。

## 9. Error 与 retry contract

每个错误包含稳定 code 和 `retry_class`：

| retry class | 语义 | 示例 |
| --- | --- | --- |
| `never` | 同一输入自动重试没有意义 | invalid params、unknown template、secret detected、unsupported action |
| `after_authority_change` | 仅当 task/Contract/binding/evidence 已发生权威变化后重试 | missing binding、unresolved policy、budget/required field failure |
| `bounded_transient` | 客户端可指数退避，最多有限次数 | DB busy、daemon unavailable、read snapshot conflict |

CLI/MCP/Skill 禁止无限轮询；`bounded_transient` 的默认上限由客户端公共 transport policy 决定，
Prompt Compiler 不单独实现重试循环。

## 10. route matrix 单源修复

新增 MCP 工具前必须先完成：

1. `gen_route_matrix.py` 成为 tool route 声明的唯一生成源；
2. 增加 Rust mirror emit，并对当前 JSON/Rust 做一次语义 diff；
3. `verify_route_matrix.py` 保留语义门禁，不再复制固定 `EXPECTED_TOTAL=239`；
4. byte drift 由 generator 的 `--check` 负责，semantic route/dispatch/whitelist/registration 由 verifier 负责；
5. 对当前 matrix=241、registered=242 的差异逐项裁决，建立权威基线 `N`；
6. 再新增 `task_get_role_prompt`，生成并验证恰好 `N+1` 项产物；
7. 不手改 `route_matrix.rs`，不新建第三套 checker。

## 11. 实施前硬前置

### GATE-0：Epic immutable binding attestation

- **类型**：治理数据修复，不改生产代码。
- **目标**：为 `T-1787203926824-9f873bfc` 建立 daemon 认可的 immutable binding/capture。
- **输入**：执行时 daemon 返回的 workspace authority、已绑定 anchor task、真实 reviewer lease、
  task-bound evidence hash。
- **禁止**：硬编码 `b951...`/`c433...`、路径 hash 推导、直连 SQLite、复用过期 lease。
- **验收**：Epic `next-action` 不再返回 `E_WORKSPACE_AUTHORITY_UNAVAILABLE`，binding/capture 可回读。

### GATE-1A：parent-aware governed task.create daemon hardening

- **类型**：独立 root bootstrap 任务；完成后 Prompt Compiler feature parent 才挂入 Epic。
- **复用**：现有 Rust `task.create` domain，不新写第二套 task creation business logic。
- **交付**：`parent_id` 非空时必须验证 parent 存在、读取其唯一 immutable binding、继承 workspace；
  caller workspace 只能作为 expected guard 且必须 exact match；Role Contracts、Contract envelope 和
  identity policy 缺失时整事务 fail closed。
- **负向**：parent 不存在/未绑定、Contract 缺失、policy unresolved、unknown field、local fallback 全部失败。
- **禁止**：新建 task creation domain、使用 `task.create_subtask` 残缺路径、Python DB、对已有任务强行 reparent。

### GATE-1B：parent-aware governed task.create CLI parity

- **类型**：GATE-1A closed 后的 Python thin-client 任务。
- **交付**：CLI 透传 `parent_id`、expected workspace guard、identity policy、Role Contracts、Contract envelope；
  响应原样展示 daemon 创建的 binding/Contract projection。
- **负向**：daemon unavailable、unknown parent、workspace mismatch、缺 Contract 均原样 fail closed；
  enterprise/daemon 模式不得调用 local DB fallback。
- **禁止**：客户端补 binding、选择 active workspace、生成 synthetic `ws-1`、直接修改 parent_id。

GATE-0、GATE-1A 与 GATE-1B 都关闭后，才创建 Prompt Compiler feature parent 和 RP 卡。R1/R2 里的 `task.split`
导入 envelope 作废，不得直接执行。

## 12. 串行微任务

所有卡使用 Executor/Reviewer/Adjudicator 现行三角色治理合同。Planner 是设计 owner，不作为 pre-cutover
runtime Role Contract。前卡 closed 才释放后卡。

| 顺序 | 卡 | 单一交付边界 | `allowed_path_globs` | `excluded_path_globs` |
| ---: | --- | --- | --- | --- |
| 0 | RP-00 merged frozen spec / task manifest freeze | 合并 R1/R2/R3 为自包含冻结规范，生成逐条 resolution matrix、来源 hash 清单和机器导入 manifest | `docs/design/cw-role-prompt-compiler-v1-*`、`docs/evidence/RP-00-*`、`deliverables/software-company/role-prompt-v1-r3-task-manifest.json` | `rust_ext/src/**`、`rust_ext/resources/**`、`cli/**`、`server/**` |
| 1 | RP-01 asset compiler/build gate | validator core、build.rs 生成器、负向 fixtures | `rust_ext/build.rs`、`rust_ext/build_support/**`、`rust_ext/Cargo.toml`、`rust_ext/tests/role_prompt_validator_*` | `rust_ext/resources/role_prompts/**`、`rust_ext/src/daemon/task_prompt/**`、`cli/**`、`server/**` |
| 2 | RP-02 production template assets | manifest、legacy/current/system 模板、golden fixtures | `rust_ext/resources/role_prompts/v1/**`、`rust_ext/tests/role_prompt_assets_*` | `rust_ext/src/daemon/**`、`cli/**`、`server/**`、`.agents/**` |
| 3 | RP-03 authority context domain | 单 read snapshot 聚合 binding/next-action/Contract/watermark | `rust_ext/src/daemon/task_prompt/context*.rs`、`rust_ext/src/daemon/task_prompt/context_tests*.rs` | `rust_ext/src/daemon/task_prompt/render*.rs`、`rust_ext/src/daemon/dispatch.rs`、`cli/**`、`server/**` |
| 4 | RP-04 renderer/hash/security | route、render、canonical、truncation、secret guard、bundle | `rust_ext/src/daemon/task_prompt/route*.rs`、`rust_ext/src/daemon/task_prompt/render*.rs`、`rust_ext/src/daemon/task_prompt/canonical*.rs`、`rust_ext/src/daemon/task_prompt/redaction*.rs`、`rust_ext/src/daemon/task_prompt/bundle*.rs` | `rust_ext/src/daemon/dispatch.rs`、`cli/**`、`server/**` |
| 5 | RP-05 daemon RPC/capability | `task.prompt.compile` handler、dispatch 极薄注册、health capability | `rust_ext/src/daemon/task_prompt/handler*.rs`、`rust_ext/src/daemon/dispatch.rs`、`rust_ext/src/daemon/capability_control.rs`、`rust_ext/src/daemon/task_prompt/rpc_tests*.rs` | `cli/**`、`server/**`、`rust_ext/resources/role_prompts/**` |
| 6 | RP-06 CLI thin client | `cw task prompt TASK_ID` 与三种本地显示 | `cli/task_prompt.py`、`cli/main.py`、`tests/test_task_prompt_cli*.py`、`docs/user-guide.md` | `db/**`、`server/**`、`rust_ext/src/daemon/task_prompt/**`、`rust_ext/resources/role_prompts/**` |
| 7 | RP-07 route matrix SSOT repair | generator→JSON/Rust 双产物、semantic verifier reconciliation 出权威基线 `N` | `scripts/gen_route_matrix.py`、`scripts/verify_route_matrix.py`、`deliverables/software-company/tool_migration_matrix.json`、`rust_ext/src/daemon/route_matrix.rs`、`tests/test_*route_matrix*.py` | `rust_ext/src/daemon/task_prompt/**`、`server/tools/**`、`cli/**` |
| 8 | RP-08 MCP thin client | 新 `task_get_role_prompt`、生成 `N+1` 项 matrix | `server/tools/tools_task_prompt.py`、`server/mcp_server.py`、`scripts/gen_route_matrix.py`、`deliverables/software-company/tool_migration_matrix.json`、`rust_ext/src/daemon/route_matrix.rs`、`tests/test_task_prompt_mcp*.py` | `db/**`、`rust_ext/resources/role_prompts/**`、`rust_ext/src/daemon/task_prompt/**` |
| 9 | RP-09 Skill/docs cutover | cw-task-loop 默认使用 daemon bundle；capability 缺失只回落 role card | `.agents/skills/cw-task-loop/**`、`Callwarden 无人值守循环启动模板：*`、`AGENTS.md`、`scripts/validate_template_compliance.py`、`tests/test_role_prompt_skill*.py` | `rust_ext/src/**`、`rust_ext/resources/**`、`cli/**`、`server/**` |
| 10 | RP-10 E2E/security/release Gate | fresh daemon、CLI/MCP/Skill parity、no-write/no-secret、部署 receipt | `tests/test_task_prompt_e2e*.py`、`rust_ext/tests/role_prompt_e2e*.rs`、`scripts/check_client_purity.py`、`scripts/refresh_shared_runtime.ps1`、`docs/evidence/RP-10-*` | `db/**`、`docs/evidence/*-historical-*`、`rust_ext/src/daemon/task_prompt/**` |

策略性禁止项（如“不得改历史 evidence”“不得新增业务功能”“不得直连数据库”）必须写入每卡
`forbidden_actions`，不能伪装成 path glob。上表是 path 约束，RP-00 机器 manifest 必须逐项原样展开。

### 12.1 每卡共同合同

- 唯一 idempotency key：`role-prompt-v1-r3-rpXX`。
- 任务描述必须引用 R3 hash、predecessor task ID 和 exact allowed/excluded paths。
- 正向、负向、回归测试均为验收必需，不允许“已归因红灯”代替全绿。
- source-only build 不等于部署；RP-10 前不切 shared runtime。
- evidence 必须含 task/step/Contract/binding/commit/template/bundle hash 和命令结果。
- 新 domain 文件目标 <800 行，硬上限 1,500 行；`dispatch.rs`/`cli/main.py` 只允许薄注册。

### 12.2 RP-00 合并冻结与信息保全门禁

R1、R2、R3 是 append-only 的设计与评审历史，三者都不得删除、覆盖或在原文件内“整理成最新版本”。
RP-00 必须另外生成以下三类交付物：

1. `docs/design/cw-role-prompt-compiler-v1-frozen-spec.md`：面向实施的唯一规范。它必须自包含，Executor
   无需再阅读 R1/R2/R3 才能获得完整 schema、错误码、hash、模板路由、安全、任务、验收和回滚语义；
2. `docs/evidence/RP-00-review-resolution-matrix.*`：逐条覆盖 R1/R2/R3 的规范性条款，记录来源文件、章节、
   source hash、处置 `retained|superseded|rejected|deferred`、理由和 frozen spec 落点；
3. `deliverables/software-company/role-prompt-v1-r3-task-manifest.json`：只能从 merged frozen spec 生成的
   机器可导入清单，不得从三份历史文档临时拼接。

RP-00 的 fail-closed 验收如下：

- R1/R2/R3 每条规范性条款必须且只能映射一次；存在未映射、重复映射、冲突未裁决或无落点条款即失败；
- resolution matrix 必须记录三份来源文件的完整 SHA-256，frozen spec 与 task manifest 也必须各自有 SHA-256；
- `retained`/`superseded` 条款必须能追溯到 frozen spec 精确章节；`rejected`/`deferred` 必须保留理由和
  successor capability（适用时），不得静默丢弃；
- task manifest 中每张卡只能引用 frozen spec hash、自己的 predecessor 和精确 Contract，不得要求
  Executor 在运行时自行解释 R1/R2/R3 的优先级；
- merged frozen spec、resolution matrix 和 task manifest 必须由独立 Reviewer 一并 PASS，RP-01 才可释放。

因此，“合并”是新增权威产物而不是改写历史：R1/R2/R3 负责说明设计如何演进，merged frozen spec
负责告诉实现者最终必须做什么，resolution matrix 负责证明信息没有丢失。

## 13. 关键验收矩阵

### 13.1 正向

- task ID only：CLAIM/REVISE/REVIEW/ADJUDICATE 各生成正确角色模板；
- BLOCKED/WAITING/COMPLETE 生成 non-actionable system prompt；
- 相同 authority snapshot 连续 100 次 `context_hash`/`prompt.sha256`/`bundle_hash` 完全一致；
- CLI JSON 与 MCP bundle 字段、hash 完全一致；card/llm 只是同 bundle 的本地显示；
- capability health 中 manifest hash 与实际 bundle 一致；
- route baseline reconciliation 得到 `N` 且零漂移后新增工具，生成/验证 `N+1` 项全绿。

### 13.2 authority 负向

- task 不存在；
- task 没有唯一 immutable binding/capture；
- caller guard 与 task binding 不一致；
- Contract/Role Contract 缺失、hash 不可验证、identity policy unresolved；
- missing/empty/unknown/mismatched prompt template ID；
- stale source event watermark；
- unknown decision/action/role；
- planner capability 未启用却出现 PLAN route。

### 13.3 安全负向

- task title/description/evidence 内含伪 closing tag；
- Authorization header、Bearer token、credential、lease token、private key 特征；
- JSON escape 前后边界与多字节 Unicode 裁剪；
- 必填 authority 字段超限必须硬失败；
- 可选长文本裁剪后 omissions hash 正确且 bundle 不碰撞；
- public RPC 传 role/mode/preview/profile/format/credential/lease 等未知字段必须拒绝；
- 同一 prompt 不能直接作为 claim、verdict、apply 或 close 授权。

### 13.4 no-write / thin-client

- RPC 前后 task DB fingerprint/`total_changes`/event sequence 不变；
- Python 新代码不导入 DB/SQLite/PyO3 authority；
- CLI/MCP 不读取模板、不推导 role/workspace、不实现 fallback；
- Skill 不复制模板正文或 role-protocol 枚举。

## 14. 发布与回滚

发布前必须：

1. GATE-0、GATE-1A、GATE-1B、RP-00～RP-10 全部 closed，且 RP-00 merged frozen spec、
   resolution matrix、task manifest 的 hash 与独立 Reviewer verdict 一致；
2. fresh isolated daemon 全矩阵全绿；
3. 独立 Reviewer PASS；
4. Adjudicator 基于当前 runtime receipt 裁决；
5. 通过 `refresh_shared_runtime.ps1` 一次性切换 shared runtime；
6. live HTTP/CLI/MCP/Skill round-trip 再验收。

回滚只禁用 `role_prompt_compiler_v1` capability 和客户端入口，Skill 回到现有只读 next-action role card。
不得回落 Python Prompt business logic，不修改 task/Contract/lease/verdict/lifecycle 历史。

## 15. Successor capabilities

以下明确不在 v1：

- `decision_request_v1`：权威选项、推荐、风险、后果和 `decision.respond`；
- `planner_governance_v1`：Planner Role Contract 与 PLAN/replan assignment；
- `role_worker_dispatch_v1`：根据最新 authority 自动启动/唤醒 worker；
- `prompt_issuance_ledger_v1`：把 top-level `bundle_hash` 绑定到后续 mutation；
- model admission/`model_requirements`；
- 多租户模板签名、审批发布、远端 Jira 控制台和 worker fleet。

依赖顺序：

```text
Role Prompt Compiler v1
  → prompt_issuance_ledger_v1
  → role_worker_dispatch_v1 / bake-off
```

没有 issuance ledger 前不得开展“哪个提示词导致了哪个 mutation”的归因实验。

## 16. 对 R1/R2 的 supersede 映射

| 旧条款 | R3 处置 |
| --- | --- |
| R1 §4.2 两 RPC、mode/preview/profile | 由 R3 §4 单 RPC 取代 |
| R1 §5 bundle/executable | 由 R3 §5 的永不授权 claim bundle 取代 |
| R1 §6 模板选择 | 由 R3 §6 状态机取代 |
| R1 §7.1 placeholder language | v1 删除通用 placeholder；改为 trusted static body + canonical context appendix |
| R1 §7.2/§7.3 untrusted/secret | 保留原则，由 R3 §6.1、§7、§13.3 收紧 |
| R1 §7.4 budget | 由 R3 §7 分级裁剪/硬失败取代 |
| R1 §8 hard errors | 由 R3 §9 retry class 取代 |
| R1 §9 CLI/MCP | 由 R3 §4.2～§4.4 取代 |
| R1 §12/§21 task.split 导入 | 由 R3 §11/§12 取代 |
| R2 §3 escalation classifier/decision template | v1 删除；future decision capability 见 §15 |
| R2 §4 build gate | 由 R3 §8 的单 parser/generated runtime 取代 |
| R2 §5 model_requirements | v1 删除，延后到 model admission capability |
| R2 §6 provenance_binding | v1 删除；未来 ledger 直接绑定 top-level bundle hash |
| R2 §7/§10 实施顺序 | 由 R3 §11～§14 取代 |

## 17. Reviewer 必答问题

1. public request 是否真正只要求 task ID，且没有客户端角色/workspace 推导？
2. daemon 是否在同一只读 snapshot 内完成 next-action/context/render？
3. bundle 是否明确不能替代 claim/lease/mutation authorization？
4. blocked 是否保持内部恢复，而没有发明不存在的 classifier/decision authority？
5. template ID 缺失、空串、未知和 route mismatch 是否稳定可区分？
6. hash include/exclude 是否无自引用、无开放式“等”？
7. build/runtime 是否确实只有一套 parser/validator？
8. 是否拒绝 draft bypass 环境变量？
9. route matrix 是否先逐项 reconciliation 出权威基线 `N`，再受控扩到 `N+1`？
10. GATE-0 是否使用 live daemon authority，而不是路径 hash？
11. GATE-1A/1B 是否复用并硬化 Rust `task.create`，没有 DB/task.create_subtask 旁路？
12. RP 卡是否保持单一主要 ownership、可串行独立验收？
13. RP-00 是否逐条覆盖 R1/R2/R3，并生成自包含 frozen spec、resolution matrix 和仅引用 frozen spec hash
    的机器 manifest，确保 Executor 不需要自行拼接历史文档？

---

R3 的最终原则是：**Prompt Compiler 只把 daemon 已经知道的权威任务状态编译成角色工作说明，
不创造新治理事实，不让客户端选角色，不让 prompt 变成授权票据。**
