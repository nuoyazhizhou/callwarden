# Call Warden Role Prompt Compiler v1 自包含冻结规范

> 状态：`RP-00_CANDIDATE`
>
> 设计责任：Planner
>
> 适用范围：Call Warden task-id-first Role Prompt Compiler v1
>
> 生成日期：2026-09-01
>
> 权威声明：本文件已经整合 R1、R2、R3、R4 及 R4 finding disposition 的最终有效语义，但在
> GATE-0、GATE-1A、GATE-1B 关闭，source clause inventory、resolution matrix、机器任务清单和独立
> Reviewer verdict 全部完成前，只是候选冻结规范，不代表生产 capability、任务状态、部署或正式 PASS 已发生。

## 0. 文档身份、历史保全与晋升规则

### 0.1 冻结输入

| 输入 | 路径 | SHA-256 |
| --- | --- | --- |
| R1 初始完整方案 | `docs/design/cw-role-prompt-compiler-v1-implementation-plan.md` | `543711EA2FEE11C1C4106825C794CD08FC7A715AA1E7E75B9DAE04D151BC5FC5` |
| R2 第一轮修订 | `docs/design/cw-role-prompt-compiler-v1-plan-amendment-r2.md` | `1B09702BFDCA57E1090D46A83D4021535FAE29F5E67C2E09E57E321CDE6DF280` |
| R3 事实裁决与收缩 | `docs/design/cw-role-prompt-compiler-v1-plan-amendment-r3.md` | `67C09BBCE67F5214F3AFB9BBF67818B2B807D1EAD3822718B4CCFB3D6AD5220C` |
| R4 最终评审裁决 | `docs/design/cw-role-prompt-compiler-v1-plan-amendment-r4.md` | `8F457BB9D89445AEABECFF7D4DB9E32D8EF010C7C1D387C0DE986602176EA084` |
| R4 finding disposition | `docs/evidence/RP-00-r4-review-findings-disposition.md` | `5C6B02455CE03436FC00015CA0B184E01158EBBC272AC3CE20335A26C85445B8` |

行数只用于显示，不参与输入身份；输入身份只由路径与 SHA-256 确定。

### 0.2 历史文件保护

R1～R4 和 finding disposition 是 append-only 的设计演进证据，不得删除、覆盖或整理成“最新版”。正式
RP-00 Contract 必须把以下文件列入 exact excluded paths：

```text
docs/design/cw-role-prompt-compiler-v1-implementation-plan.md
docs/design/cw-role-prompt-compiler-v1-plan-amendment-r2.md
docs/design/cw-role-prompt-compiler-v1-plan-amendment-r3.md
docs/design/cw-role-prompt-compiler-v1-plan-amendment-r4.md
docs/evidence/RP-00-r4-review-findings-disposition.md
```

### 0.3 候选规范晋升

本文件晋升为正式冻结规范必须同时满足：

1. GATE-0、GATE-1A、GATE-1B 均为 daemon 权威 `closed`；
2. Reviewer 人工冻结 `RP-00-source-clause-inventory.json` 的来源全集；
3. validator 证明 inventory 与 `RP-00-review-resolution-matrix.json` 一对一；
4. `role-prompt-v1-task-manifest.json` 只引用本文件的精确 hash；
5. 独立 Reviewer 对本文件、inventory、resolution matrix 和 task manifest 一并 PASS；
6. daemon 中存在 task-bound verdict、evidence 与 handoff，而不是只有聊天结论。

若晋升时正文无需修改，由 task manifest 直接 pin 当前 hash；若必须修改，不覆盖已经被 review 的候选版本，
而是生成新候选版本并追加 resolution，不静默改变已审输入。

### 0.4 关键 supersede 裁决摘要

| 历史设计 | 最终裁决 |
| --- | --- |
| R1 两个 public RPC、role/mode/preview/profile | 由单一 `task.prompt.compile(task_id, optional guard)` 取代 |
| R1/R2 通用 placeholder 与 decision template | v1 删除；静态 body + canonical appendix，decision 延后 |
| R2 12 类 escalation classifier | v1 拒绝；只编译 daemon 已有 next-action，不创造 classifier authority |
| R2 `model_requirements` | 延后到 model admission capability，不进入 v1 bundle |
| R2 `provenance_binding.source_bundle_hash` | 删除；未来 issuance ledger 直接绑定 top-level bundle hash |
| R3 三值 retry | 由四值枚举取代，新增 `after_authority_refresh` |
| R3 展开的 `bundle_hash` 输入 | 由 R4 最小闭集取代 |
| R3/R4 对全部 dispatch extras 分类 | finding disposition 收缩；RP-07 只证明 T=M 与 M 路由属于 D |
| R3 `capability_control.rs` 路径 | 修正为 `rust_ext/src/daemon/task_loop/capability_control.rs` |
| R3-specific task manifest 名称 | 统一为 `role-prompt-v1-task-manifest.json` |

历史条款的逐项 disposition 仍以正式 resolution matrix 为机器 authority；本表只供读者快速理解。

## 1. 要解决的问题

用户或后台 worker 应当只需提供一个精确 `task_id`。daemon 必须在同一个只读 authority snapshot 中确定：

- 任务及不可变 workspace binding/capture；
- 当前 `task.next_action` 结论、需要的治理角色与 step；
- Task Contract、当前 Role Contract、identity policy；
- 与 Role Contract 精确绑定的提示词模板；
- 允许范围、禁止范围、验收、证据和下一棒；
- 当前阻断、等待或完成语义。

daemon 随后返回可直接供 LLM 使用的 Role Prompt Bundle。Python CLI、MCP 与 Skill 只做 HTTP 薄客户端，
不得选择角色、拼模板、推导 workspace、读取 SQLite/PyO3 authority 或在 daemon 不可用时回落本地业务逻辑。

Prompt 只是工作指导，不是 claim、lease、report、verdict、apply 或 close 的授权物。

## 2. 目标、非目标与成功标准

### 2.1 v1 目标

1. public API 以 `task_id` 为唯一必填业务参数。
2. next-action、Contract、模板选择和 context 在单一只读 snapshot 内完成。
3. 相同 authority 输入产生确定性的 context、prompt 和 bundle hash。
4. CLI、MCP、Skill 对同一 bundle 保持字段与 hash 一致。
5. legacy Role Contract 精确可用，不改写历史模板 ID/hash。
6. BLOCKED/WAITING/COMPLETE 生成明确的 non-actionable system prompt。
7. 生产模板在 Rust 构建阶段 fail closed，runtime 不维护第二套解析器。
8. 任何输出不泄露 credential、raw lease token、Authorization 或本地 secret。
9. capability 可受控启用和回滚，回滚不恢复 Python Prompt Compiler。

### 2.2 v1 明确不实现

- public role/mode/preview/context-profile 选择；
- 通用 placeholder 模板语言；
- `decision_request_v1`、`decision.respond` 或 user route；
- `planner_governance_v1` 的创建、PLAN/replan 派工；
- model admission、`model_requirements` 或模型自动选择；
- worker 自动启动、远端 fleet 或后台调度；
- prompt issuance ledger、mutation binding 或结果归因；
- 多租户签名模板、远端 Jira 控制台；
- 通过此功能修复历史任务、Contract、binding 或 lease 数据。

### 2.3 核心不变量

1. daemon 是任务、路由、合同、模板与 capability 的唯一 authority。
2. 一个 compile 请求只观察一个 authority snapshot，不递归调用第二次 JSON-RPC。
3. `authorization.valid_for_claim` 永远为 `false`。
4. 每次 mutation 前必须重新查询 daemon authority，bundle 不能作为 fencing 票据。
5. 客户端未知字段、客户端选 role/workspace、SQLite/PyO3 fallback 一律拒绝。
6. 技术、数据、环境、Contract、binding、认证和 capability 缺口不得被编译为“请用户手工修复”。
7. v1 不创造不存在的 Planner、decision、model 或 issuance 治理事实。

## 3. 总体架构与 ownership

```text
User / background worker
        │ exact task_id
        ▼
CLI / MCP / cw-task-loop Skill                 Python thin clients
        │ HTTP JSON-RPC task.prompt.compile
        ▼
Rust daemon handler                            transport validation only
        ▼
Task Prompt domain
  ├─ one read-only transaction/snapshot
  ├─ immutable binding/capture resolver
  ├─ internal next-action evaluator
  ├─ Task/Role Contract resolver
  ├─ generated template registry
  ├─ canonical context builder
  ├─ renderer / clipping / secret guard
  └─ bundle/hash builder
        │
        ▼
RolePromptBundle valid_for_claim=false
```

| 层 | 拥有的语义 | 明确禁止 |
| --- | --- | --- |
| Rust daemon domain | authority、route、Contract、模板、canonicalization、hash、安全、错误/retry | 调用 Python 业务、递归 RPC、写任务状态 |
| Rust dispatch/HTTP | 方法注册、严格 request schema、错误传输 | route 或模板业务逻辑 |
| Python daemon client | HTTP 请求/响应、公共 transport retry | DB/PyO3、role/workspace/template 推导 |
| CLI | `llm|card|json` 本地展示 | 把 format 发给 daemon、重建 bundle |
| MCP | 单一薄工具 | 本地模板、兼容 registry 业务实现 |
| Skill | 调 daemon，原样呈现 prompt 和摘要 | 复制 production 模板、合成角色卡 |

新 domain 源文件目标少于 800 行，硬上限 1,500 行；`dispatch.rs` 和 `cli/main.py` 只允许薄注册。

## 4. Public contract

### 4.1 唯一生产 RPC

```json
{
  "method": "task.prompt.compile",
  "params": {
    "task_id": "T-...",
    "expected_workspace_instance_id": "optional-daemon-returned-CAS-guard"
  }
}
```

规则：

1. `task_id` 是唯一必填业务字段。
2. `expected_workspace_instance_id` 是可选 caller assertion；普通用户/Agent 不需要提供。
3. request 只接受上述两个字段；未知字段 fail closed。
4. 禁止字段包括 `role`、`mode`、`preview_role`、`context_profile`、`format`、identity、credential、
   role worker auth、lease token、fencing counter 和任意 DB/workspace 路径。
5. daemon 从 task immutable binding 解析真实 workspace；客户端不得选择 active workspace 或生成 synthetic ID。
6. handler 不得产生 DB 写入、lease、claim、assignment、event 或 lifecycle mutation。

### 4.2 Workspace guard

`expected_workspace_instance_id`：

- 只用于检测调用方缓存的 authority 是否过期；
- 不进入 `context_hash`、`prompt.sha256` 或 `bundle_hash`；
- mismatch 返回 `E_TASK_PROMPT_AUTHORITY_MISMATCH`；
- retry class 固定为 `after_authority_refresh`；
- 客户端必须刷新 authority 后发起新请求，不能盲重放旧请求。

### 4.3 CLI

```powershell
python C:/git_work/callwarden/cw.py task prompt T-...
python C:/git_work/callwarden/cw.py task prompt T-... --format llm
python C:/git_work/callwarden/cw.py task prompt T-... --format card
python C:/git_work/callwarden/cw.py task prompt T-... --format json
```

`--format` 只改变本地展示，不发送给 daemon、不进入任何 hash。默认格式为 `llm`。daemon unavailable 时
返回 transport error；enterprise/daemon 模式无 SQLite/PyO3/local fallback。

“客户端不得推导 workspace”在本规范中约束新 `task prompt` 路径及其 Skill/MCP 调用链。现有
`cw task next-action` 仍存在基于 cwd/project root 的 `derive_workspace_instance_id` legacy fallback；RP-06
不得复用或扩散该行为，也不在 Prompt Compiler Contract 内顺带修改另一条命令。该 legacy fallback 作为相邻的
thin-client authority defect 独立整改；在它退休前，RP-09 的只读 fallback 只有拿到 daemon 返回的 exact
workspace instance ID 才可调用 next-action，否则显示 capability/authority unavailable 并 fail closed。

### 4.4 MCP

```text
task_get_role_prompt(task_id: str) -> RolePromptBundle
```

MCP 工具不接受 role、format、workspace、credential 或 lease 参数，不复用 blind `get_role_view`。

### 4.5 Skill

`$cw-task-loop TASK_ID` 优先调用 `task.prompt.compile` 并原样输出 `prompt.text` 与结构化摘要。capability 尚未
声明时，只允许在 exact daemon-returned workspace instance 已知时回落到现有只读 `next-action` role card；
不得触发客户端 derive fallback，也不得本地渲染 production prompt。

## 5. Role Prompt Bundle v1

### 5.1 Response schema

```json
{
  "schema_version": "role_prompt_bundle_v1",
  "task_id": "T-...",
  "prompt_kind": "role_work|blocked_recovery|waiting|terminal",
  "authority": {
    "workspace_id": 1,
    "workspace_instance_id": "...",
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
  "generated_at": "display-only RFC3339 timestamp"
}
```

### 5.2 Schema semantics

- `prompt_kind=role_work` 仅表示当前 authority 可形成角色工作说明；它不授权领取。
- BLOCKED、WAITING、COMPLETE 的 `routing_state` 必须为 `non_actionable`。
- `generated_at` 只显示，不参与 hash。
- `snapshot_id` 可为 null，但 binding、capture 和 source event watermark 必须可验证。
- role work 的 body ID/hash 必须与当前 Role Contract 精确匹配。
- system prompt 的 body ID/hash 必须来自 daemon system manifest。
- v1 不包含 `executable`、`provenance_binding`、`model_requirements` 或 decision request 对象。

## 6. 路由和模板选择状态机

| daemon next-action | template authority | prompt kind | routing state |
| --- | --- | --- | --- |
| `READY/CLAIM` | 当前 Executor Role Contract | `role_work` | `action_ready` |
| `READY/REVISE` | 当前 Executor Role Contract | `role_work` | `action_ready` |
| `READY/REVIEW` | 当前 Reviewer Role Contract | `role_work` | `action_ready` |
| `READY/ADJUDICATE` | 当前 Adjudicator Role Contract | `role_work` | `action_ready` |
| `BLOCKED/*` | `cw.system.blocked_recovery.v1` | `blocked_recovery` | `non_actionable` |
| `WAITING/*` | `cw.system.waiting.v1` | `waiting` | `non_actionable` |
| `COMPLETE/*` | `cw.system.terminal.v1` | `terminal` | `non_actionable` |
| `READY/PLAN` | v1 不选择模板；留给 `planner_governance_v1` successor | 无 | hard error |
| 未知 decision/action/role | 无 | 无 | hard error |

在 `planner_governance_v1` 未声明时，`READY/PLAN` 必须返回 `E_TASK_PROMPT_UNSUPPORTED_ACTION`，不能合成 Planner
派工。v1 没有 user route；未知 user route 同样返回该错误。任何 decision-request 专用错误码和
`decision_request.md` 都不属于 v1。

### 6.1 Blocked recovery 纪律

`blocked_recovery` 只呈现 daemon 已返回的 blocking reasons 与内部恢复要求：

1. 技术、数据、环境、Contract、binding、认证和 capability 问题由内部 Planner/Executor/治理能力处理；
2. 不要求用户改库、补 Contract、改 binding、修 credential 文件或反复重试内部命令；
3. 不输出 secret、raw lease、credential、token 或恢复密钥；
4. 不伪造 Planner assignment、decision request、fix step 或 capability；
5. 没有唯一安全恢复路线时保持 non-actionable，并明确缺少的 authority 事实。

## 7. 模板引用、资产与 legacy 兼容

### 7.1 Role Contract 引用校验

1. `prompt_template_id` 缺失、非字符串或 trim 后为空：`E_TASK_PROMPT_TEMPLATE_ID_REQUIRED`。
2. `prompt_hash` 缺失或非法：`E_TASK_PROMPT_TEMPLATE_HASH_REQUIRED`。
3. 读取时允许历史 64-hex 与 `sha256:<64-hex>` wire form，比较前规范为小写 `sha256:`；其他算法、长度或字符拒绝。
4. manifest 无 exact ID/hash：`E_TASK_PROMPT_TEMPLATE_NOT_FOUND`。
5. byte content hash 不一致：`E_TASK_PROMPT_TEMPLATE_HASH_MISMATCH`。
6. manifest 的 role/action/capability 与 next-action 不匹配：`E_TASK_PROMPT_TEMPLATE_ROUTE_MISMATCH`。
7. 禁止 fallback 到默认 Executor 模板或用 current 模板冒充 legacy hash。

### 7.2 Legacy byte assets

| role | Role Contract primary ID | content SHA-256 | byte source |
| --- | --- | --- | --- |
| Executor | `cw.aprime.executor.startup.v1` | `59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6` | `deliverables/software-company/aprime_role_contracts/executor_planner_startup_v1.md` |
| Reviewer | `cw.aprime.reviewer.startup.v1` | `6415033D8F134392DE16FCA130BFB762CB6C70D9F466C770EC18A20FC4CE139E` | `deliverables/software-company/aprime_role_contracts/reviewer_startup_v1.md` |
| Adjudicator | `cw.aprime.adjudicator.startup.v1` | `42A5F1DEFA81008B009058C1BAF5D1A14B3EF4521E291B7B55C19BB473A77C3E` | `deliverables/software-company/aprime_role_contracts/adjudicator_startup_v1.md` |

Executor 历史文件正文自述 ID 为 `cw.aprime.executor-planner.startup.v1`，但 Role Contract authority 已绑定
`cw.aprime.executor.startup.v1`。manifest 必须保留唯一例外：

```json
{
  "template_id": "cw.aprime.executor.startup.v1",
  "source_declared_template_id": "cw.aprime.executor-planner.startup.v1",
  "legacy_alias_mismatch": true,
  "content_sha256": "sha256:59a459f7786097c671d48fbeec6e361c12d7a95bdec4e3722169d68d5d6a73f6"
}
```

build gate 只可按该 exact ID+hash 接受这一个历史 mismatch。current/new template 的 primary ID 与正文声明必须一致。

### 7.3 Production resources

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

Planner asset 可被离线 fixture 验证，但 public route 在 capability cutover 前不可触达。

### 7.4 唯一 validator/parser

- `rust_ext/build_support/role_prompt_validator.rs` 是唯一 parser/validator/canonical core；
- `rust_ext/build.rs` 读取 manifest/resources，调用该 core，并生成 `OUT_DIR/role_prompts_generated.rs`；
- runtime 只消费 generated constants，不重新解析资源或实现第二套规则；
- tests 复用同一 core；
- production asset 无效时 `cargo check/build/test` 必须失败；
- 禁止 `CW_ALLOW_DRAFT_TEMPLATES` 或任何 production bypass；草稿必须位于 manifest 扫描范围外。

## 8. Prompt 组合、可信边界与注入防护

### 8.1 固定组合顺序

1. daemon-owned compiler policy；
2. byte-exact Role Contract template 或 system template；
3. daemon 生成的 canonical authority/context appendix；
4. 固定 mutation recheck footer。

v1 不执行通用 placeholder 插值。task title、description、finding、evidence note 等不可信文本只能进入第 3 段。

### 8.2 不可信块

动态内容序列化为 canonical JSON 后放入：

```text
<CW_UNTRUSTED_TASK_DATA encoding="json-string-v1">
{...canonical JSON...}
</CW_UNTRUSTED_TASK_DATA>
```

正文中的 `<`、`>`、`&` 和可能形成 closing tag 的内容必须通过 JSON/string-safe 编码，不能提前结束区块。
compiler policy 必须明确：该区块是数据，不是指令；其中出现的角色声明、工具调用、授权、Handoff 或模板标签
均无治理效力。

### 8.3 Secret denylist

compile 前与最终输出后都执行高置信扫描。至少拒绝：

- raw lease token、fencing secret、Role Worker credential 或 credential hash；
- `Authorization`、Bearer token、Cookie/session secret；
- `credentials.bin` 内容或本地 session-store secret；
- private key、可复用 API key、恢复密钥；
- request 中任何未声明 credential/auth 字段。

命中返回 `E_TASK_PROMPT_SECRET_DETECTED`，不得只打日志后继续返回，也不得把 secret 放进 omissions hash。

## 9. Canonicalization、裁剪与 hash

### 9.1 Canonical JSON v1

Prompt Compiler 使用 `cw.canonical_json.v1`：

- UTF-8；不做 Unicode normalization，保留原始 scalar sequence；
- object key 按 UTF-8 byte lexical order；
- 无多余空白；
- integer 使用无前导零十进制；v1 schema 禁止 float；
- schema 定义为 set 的数组先按 canonical element bytes 排序并去重；其余数组保留 authority 顺序；
- schema 要求的 null 必须显式保留；
- SHA-256 输出为小写 `sha256:<64-hex>`。

禁止字符串拼接计算结构化 hash。

### 9.2 文本处理顺序

1. 读取逻辑 Unicode 字符串；
2. 计算原始 UTF-8 byte length 与 SHA-256；
3. 只对允许省略的字段按 Unicode scalar 安全边界裁剪；
4. 写入 `omissions[{field, original_bytes, kept_bytes, original_sha256, reason}]`；
5. JSON escaping 与 canonical serialization；
6. 包裹不可信数据标签；
7. 进行最终 secret scan 与尺寸检查。

禁止截断已经序列化的 JSON byte stream。不同长文本即使保留相同前缀，也必须因 original hash 不同得到不同
`context_hash`。

### 9.3 尺寸预算

| 对象 | v1 上限 | 行为 |
| --- | ---: | --- |
| `prompt.text` | 64 KiB UTF-8 | 按固定优先级裁剪可省略字段；仍超限则失败 |
| canonical JSON bundle | 256 KiB UTF-8 | 超限失败 |
| 单个可省略不可信逻辑字符串 | 8 KiB UTF-8 | escape 前 scalar-safe 裁剪 |
| allowed paths | 256 项 | 超限失败，不删 scope |
| forbidden paths | 256 项 | 超限失败，不删 scope |
| 单个 path | 4 KiB UTF-8 | 超限失败 |
| task/workspace/binding/capture/snapshot/step/Contract/revision ID | 256 UTF-8 bytes | 超限失败 |
| template/compiler-policy ID | 256 ASCII bytes | 超限失败 |
| `next_action` 权威文本 | 4 KiB UTF-8 | 不裁剪；超限失败 |
| reason/evidence 可选展示项数量 | 各 128 项 | 超限后的非权威尾项可省略并记录 omissions |
| revision/watermark | `0..=i64::MAX` integer | 越界失败 |
| SHA-256 | 64 hex 或规范化后的 71-byte prefixed form | 其他形式失败 |

上述 ID/path 上限是 `role_prompt_bundle_v1` 的显式新 schema 决策；不得由 Executor 临时放宽。HTTP transport 的
8 MiB body 上限不是 bundle 预算，不能替代本节更严格限制。

### 9.4 不得裁剪的字段

task/workspace/binding/capture/snapshot/step ID、Task/Role Contract ID/revision/hash、identity policy、
decision/action/required role/next action、template/compiler policy ID/hash、manifest hash、event watermark、
allowed/forbidden path scope。超限一律 `E_TASK_PROMPT_BUDGET_EXCEEDED`。

### 9.5 `context_hash` 唯一 include 集合

`context_hash` 计算对象必须且只包含：

1. schema version；
2. task ID；
3. authority 的 workspace、binding、capture、snapshot、source watermark；
4. routing 全部字段；
5. contract 全部字段；
6. template 的 body、compiler policy、manifest ID/hash；
7. canonical clipped context；
8. canonical omissions；
9. `authorization.routing_state`、`valid_for_claim=false`、`mutation_recheck_required=true`。

### 9.6 `bundle_hash` 最小闭集

`bundle_hash` 只对下列 canonical object 计算：

```json
{
  "schema_version": "role_prompt_bundle_v1",
  "task_id": "T-...",
  "prompt_kind": "...",
  "context_hash": "sha256:...",
  "prompt": {
    "sha256": "sha256:..."
  }
}
```

### 9.7 明确排除

以下字段不进入 `context_hash`、`prompt.sha256` 的动态上下文或 `bundle_hash`：

- `generated_at`；
- JSON-RPC `request_id`、HTTP trace ID；
- CLI display format；
- retry metadata；
- `bundle_hash` 自身；
- caller 的 `expected_workspace_instance_id` assertion。

其中 `prompt.sha256` 仍然按最终完整 `prompt.text` bytes 计算；“排除动态上下文”只表示不把 transport/display
字段插入 prompt。

## 10. Error 与 retry contract

### 10.1 Retry class 唯一枚举

```text
never
after_authority_refresh
after_authority_change
bounded_transient
```

每个稳定错误码必须且只能映射一个 retry class；CLI/MCP/Skill 不得重分类或无限轮询。

### 10.2 Stable errors

错误 wire shape 固定为：

```json
{
  "error": {
    "code": "E_TASK_PROMPT_...",
    "message": "human-readable, non-secret summary",
    "retry_class": "never|after_authority_refresh|after_authority_change|bounded_transient",
    "details": {
      "task_id": "T-...|null",
      "reason_codes": []
    }
  }
}
```

`details` 不得含 prompt 全文、不可信字段全文、credential、lease、Authorization、绝对用户 home 路径或
SQLite 路径。

| code | 含义 | retry class |
| --- | --- | --- |
| `E_TASK_PROMPT_TASK_ID_REQUIRED` | task_id 缺失/空/类型错误 | `never` |
| `E_TASK_PROMPT_TASK_NOT_FOUND` | 当前 authority 无该 task | `after_authority_change` |
| `E_TASK_PROMPT_AUTHORITY_MISMATCH` | optional caller guard 过期 | `after_authority_refresh` |
| `E_TASK_PROMPT_AUTHORITY_UNAVAILABLE` | 唯一 binding/capture/snapshot authority 不可解析 | `after_authority_change` |
| `E_TASK_PROMPT_CONTRACT_UNRESOLVED` | Task/Role Contract 缺失、链/hash 不可验证或 identity policy unresolved | `after_authority_change` |
| `E_TASK_PROMPT_ROLE_NOT_ELIGIBLE` | authority 没有可编译的当前角色 | `after_authority_change` |
| `E_TASK_PROMPT_TEMPLATE_ID_REQUIRED` | Role Contract 缺模板 ID | `after_authority_change` |
| `E_TASK_PROMPT_TEMPLATE_HASH_REQUIRED` | Role Contract 缺合法 prompt hash | `after_authority_change` |
| `E_TASK_PROMPT_TEMPLATE_NOT_FOUND` | production manifest 无 exact ID/hash | `never` |
| `E_TASK_PROMPT_TEMPLATE_HASH_MISMATCH` | manifest bytes 与 Contract hash 不符 | `never` |
| `E_TASK_PROMPT_TEMPLATE_ROUTE_MISMATCH` | template role/action/capability 不匹配 | `after_authority_change` |
| `E_TASK_PROMPT_TEMPLATE_INVALID` | generated manifest/template 不满足 runtime invariant | `never` |
| `E_TASK_PROMPT_UNSUPPORTED_ACTION` | 未知或 v1 未实现 route，包括 user/PLAN pre-cutover | `never` |
| `E_TASK_PROMPT_STALE_CONTEXT` | 读取期间 authority watermark 变化 | `bounded_transient` |
| `E_TASK_PROMPT_BUDGET_EXCEEDED` | 必需字段或最终 bundle 超预算 | `after_authority_change` |
| `E_TASK_PROMPT_SECRET_DETECTED` | 输入或最终输出命中 secret denylist | `never` |
| `E_TASK_PROMPT_DB_BUSY` | 只读 snapshot 暂时不可取得 | `bounded_transient` |
| `E_TASK_PROMPT_DAEMON_UNAVAILABLE` | transport 暂时不可达 | `bounded_transient` |

`bounded_transient` 使用共享 transport policy 的指数退避和有限次数；Prompt Compiler 不实现私有死循环。

## 11. Build gate、golden 与运行时能力

### 11.1 Build-time hard gate

validator 至少检查：

1. manifest/schema 可解析；
2. template ID/version/path 唯一，无目录逃逸；
3. 文件 UTF-8/LF、hash 精确；
4. role/action/capability route 完整无多义；
5. legacy exception 只匹配 exact ID+hash；
6. current/new ID 与正文声明一致；
7. system templates 不产生 action-ready/user route；
8. production 资源无 secret pattern；
9. 资源与 manifest 全部进入编译期 hash 清单；
10. runtime generated constants 与 build output 一致。

### 11.2 Composed bundle goldens

至少冻结：

- 三个 legacy role 完整 bundle；
- 一个 current role 完整 bundle；
- BLOCKED、WAITING、COMPLETE 三个 system bundle。

每个 golden 固定 compiler policy hash、body hash、context hash、prompt hash 和 bundle hash。另做同一 snapshot
连续 100 次确定性测试。golden 证明跨 build/runtime 语义；重复测试只证明单实现稳定，两者不能互相替代。

### 11.3 Capability

正式 capability 名称：`role_prompt_compiler_v1`。health/capability projection 至少返回 schema version、manifest
hash、compiler policy hash 和 enabled 状态。capability 未启用时 public RPC fail closed；Skill 只可使用既有
只读 role card fallback。

### 11.4 可观测性与 v1 审计边界

允许日志字段：method、request ID、task ID、workspace instance ID、bundle hash、template ID/version、
decision/action/required role、response bytes、latency 和稳定错误码。禁止记录 prompt 全文、description、finding、
evidence note 全文、credential、raw lease、Authorization、session-store 内容或用户 home 绝对路径。

建议指标：

```text
task_prompt_compile_total{template,result}
task_prompt_compile_duration_ms
task_prompt_bundle_bytes
task_prompt_error_total{code}
```

指标标签不得包含 task title、用户文本、绝对路径或高基数 bundle/task ID。v1 不新增 prompt event/table，也不在
服务端写 issuance ledger。调用方可保存 bundle hash 与 runtime/manifest hash 作为外部证据；服务端 append-only
issuance 由 `prompt_issuance_ledger_v1` 单独实现。

## 12. MCP route matrix SSOT reconciliation

定义：

```text
T = 实际 MCP tool registrations
M = tool migration matrix 及 generated Rust mirror
D = daemon dispatch RPC methods
```

门禁：

1. `names(T) == tool_names(M)`，tool 名唯一；
2. `route_matrix.rs == generate(M)`，禁止手改；
3. `rpc_method(M[rust_native|task_rpc])` 必须是 `D` 的子集；
4. alias 在 M 中显式标注 canonical/legacy；
5. `python_compat` 行与 Rust whitelist、Python compat registry 三向一致；
6. `D - rpc_method(M)` 不要求为空，也不在本任务全量分类；
7. verifier 从 generator/source 计算 N，不硬编码 239/241/242/243/298。

RP-07 的确定性 reconciliation：

- 从 MCP matrix 移除 `final_zero_python_authority_audit`；它是直接 daemon 发布 Gate RPC；
- 从 MCP matrix 移除 `task_cascade_close`；它是 CLI/coordinator 治理 RPC；
- 加入 `task_assignment_status -> task.assignment.status`，`READ_ONLY`；
- 加入 `task_assignment_heartbeat -> task.assignment.heartbeat`，`PROTECTED_MUTATION`；
- 加入 `task_governance_projection -> task.governance_projection.get`，`READ_ONLY`；
- 裁决并修复 `get_role_view` generator/source 与 generated mirror 的 alias/route 漂移。

当前 reconciliation 证据目标为 `N=242`；新增 `task_get_role_prompt` 后为 `N+1=243`。这些数字只用于本轮
验收记录，生产 verifier 不保留常量。

RP-07 必须新增当前脚本尚不存在的 `gen_route_matrix.py --emit-rust` 与 `--check` 能力：generator 独占
tool source 读取、canonical JSON/Rust mirror 生成和 byte-drift 自检；`--check` 只比较生成结果，不写文件。
`verify_route_matrix.py` 是只读消费侧语义 verifier，负责 T/M/D、registration、dispatch、alias、whitelist 和
compat registry 一致性，不生成或改写产物。两个脚本不得各自维护工具枚举、route 规则或总数常量。

## 13. 实施前正式 Gate

### 13.1 Gate 顺序

```text
R4 review input closed
  -> Gate task manifest freeze + independent review
  -> GATE-0 closed
  -> GATE-1A closed
  -> GATE-1B closed
  -> Prompt Compiler feature parent + RP-00
  -> RP-00 four artifacts PASS
  -> RP-01 ... RP-10
```

本候选规范可以在 Gate 前供评审，但不能据此声称 RP-00 或 feature parent 已合法创建。

### 13.2 GATE-0：legacy Epic binding attestation

- owner：Adjudicator mutation；Planner 冻结输入；独立 active registered Reviewer 提供 anchor task reviewer lease；
- idempotency key：`role-prompt-v1-gate0-epic-binding-attestation`；
- target：`T-1787203926824-9f873bfc`；
- allowed：task-bound evidence 与 daemon `attest-legacy-workspace-binding`；
- forbidden：代码改动、SQL、路径 hash、synthetic workspace、过期/伪造 lease；
- identity：Adjudicator 与 Reviewer 的 agent ID、instance ID、session ID 全部分离；
- request：真实 reviewer lease token、当前 fencing counter、evidence path/hash；
- precondition：legacy target 未绑定，anchor 有唯一 binding/capture，requested workspace exact match anchor；
- acceptance：binding/capture/instance 回读一致；重复 request 幂等；不同 request 对已绑定 task 返回 already-bound；
  无重复 event/binding/capture；不再返回 `E_WORKSPACE_AUTHORITY_UNAVAILABLE`；
- evidence 禁止包含 raw lease token。

回读命令使用 daemon 返回的 instance：

```powershell
python C:/git_work/callwarden/cw.py task next-action T-1787203926824-9f873bfc `
  --workspace-instance-id <daemon_returned_instance_id> --json
python C:/git_work/callwarden/cw.py task governance-projection T-1787203926824-9f873bfc --json
```

### 13.3 GATE-1A：parent-aware governed `task.create` daemon

- owner：Executor；独立 root bootstrap task；predecessor=GATE-0 closed；
- idempotency key：`role-prompt-v1-gate1a-parent-aware-create-daemon`；
- reuse：现有 Rust `task.create` domain，不建第二套 create business logic；
- parent branch：parent 存在、唯一 binding/capture、expected workspace exact match、Task/Role Contract、identity
  policy、steps 和 event 在同一事务成功；任何缺口整事务 rollback；
- root branch compatibility：`parent_id` 缺失或规范化为空时，request validation、事务、response schema、event 和
  error 语义与 Gate 前 bit-for-bit 一致；新增检查只在 parent 非空分支启用；
- excluded：Python client、SQLite direct path、`task.create_subtask` 新业务实现、Prompt Compiler domain、历史 reparent。

Gate manifest 冻结新增测试模块为
`rust_ext/src/daemon/task_collab_tests_parent_create.rs`，并由
`rust_ext/src/daemon/task_collab_tests.rs` 以模块名 `parent_create` 注册。精确测试函数冻结为：

- `parent_aware_task_create_root_request_response_error_parity`；
- `parent_aware_task_create_governed_parent_success`；
- `parent_aware_task_create_rejects_missing_or_unbound_parent`；
- `parent_aware_task_create_rejects_workspace_mismatch`；
- `parent_aware_task_create_rejects_missing_contract_or_role_contract`；
- `parent_aware_task_create_rejects_unresolved_identity_policy`；
- `parent_aware_task_create_rejects_unknown_field`；
- `parent_aware_task_create_rolls_back_all_rows_on_failure`。

Gate manifest 不得改名或用更宽 filter 偷换覆盖范围；若建卡前发现模块 ownership 与当前源码不兼容，必须由
Planner 修订 Gate manifest 并重新独立 review，不能交 Executor 临场决定。

验收：

```powershell
tokenslim run cargo test --manifest-path rust_ext/Cargo.toml --no-default-features parent_aware_task_create_
tokenslim run cargo check --manifest-path rust_ext/Cargo.toml --no-default-features
tokenslim run git diff --check
```

### 13.4 GATE-1B：parent-aware governed `task.create` CLI

- owner：Executor；predecessor=GATE-1A closed；
- idempotency key：`role-prompt-v1-gate1b-parent-aware-create-cli`；
- allowed：CLI parser/thin adapter、daemon client schema、Python fixtures/user guide；
- excluded：Rust create business domain、DB/PyO3 authority、Prompt Compiler domain；
- delivery：透传 parent ID、expected workspace guard、identity policy、Role Contracts、Contract envelope；
- fail closed：daemon unavailable、unknown parent、workspace mismatch、缺 Contract 均原样返回；
- forbidden：active-workspace/synthetic binding 推导、本地 DB fallback、修改已创建任务。

验收：

```powershell
tokenslim run C:\Python314\python.exe -m pytest tests/test_task_create_parent_governance.py -q
tokenslim run C:\Python314\python.exe scripts/check_client_purity.py
tokenslim run git diff --check
```

## 14. RP-00 信息保全与机器产物

正式 RP-00 交付四个文件：

1. `docs/evidence/RP-00-source-clause-inventory.json`；
2. `docs/evidence/RP-00-review-resolution-matrix.json`；
3. `docs/design/cw-role-prompt-compiler-v1-frozen-spec.md`；
4. `deliverables/software-company/role-prompt-v1-task-manifest.json`。

### 14.1 人工冻结全集

inventory 必须覆盖 R1、R2、R3、R4 和 R4 finding disposition。稳定 `clause_id` 至少覆盖：

- 含必须、禁止、不得、仅允许、应当的规范段；
- numbered/bulleted normative item；
- schema JSON Pointer 字段；
- 状态机、错误码、route、hash include/exclude、预算、任务和验收表的每一行；
- code block 中 public request/response/manifest 的每个字段。

自然语言全集完整性由独立 Reviewer 人工确认，不能伪称脚本自动证明。

### 14.2 机器一对一

validator 必须证明：

- 每个 clause ID 在 resolution matrix 恰有一行；
- disposition 只为 `retained|superseded|rejected|deferred`；
- retained/superseded 指向本规范唯一 heading 或 schema pointer；
- rejected/deferred 有理由，deferred 在适用时有 successor capability；
- 无未知、重复、空落点或 source hash mismatch；
- task manifest 只引用本规范 hash，不解释 R1～R4 overlay 优先级。

## 15. 串行微任务树

前卡权威 `closed` 后才释放后卡。Planner 是设计 owner；pre-cutover runtime cards 使用现行
Executor/Reviewer/Adjudicator 合同。

| 顺序 | 卡 | 唯一交付边界 | allowed path globs | excluded path globs |
| ---: | --- | --- | --- | --- |
| 0 | RP-00 merged frozen spec/task manifest | 四个 RP-00 产物与 coverage validator | `docs/design/cw-role-prompt-compiler-v1-frozen-spec.md`、`docs/evidence/RP-00-*`、`deliverables/software-company/role-prompt-v1-task-manifest.json`、`scripts/validate_role_prompt_spec.py`、`tests/test_role_prompt_spec_validator.py` | R1～R4/disposition exact paths、`rust_ext/src/**`、`rust_ext/resources/**`、`cli/**`、`server/**` |
| 1 | RP-01 asset compiler/build gate | validator core、build generator、负向 fixtures | `rust_ext/build.rs`、`rust_ext/build_support/**`、`rust_ext/Cargo.toml`、`rust_ext/tests/role_prompt_validator_*` | `rust_ext/resources/role_prompts/**`、daemon prompt domain、`cli/**`、`server/**` |
| 2 | RP-02 production template assets | manifest、legacy/current/system assets、asset goldens | `rust_ext/resources/role_prompts/v1/**`、`rust_ext/tests/role_prompt_assets_*` | daemon source、`cli/**`、`server/**`、`.agents/**` |
| 3 | RP-03 authority context domain | 单 snapshot 聚合 binding/next-action/Contract/watermark | `rust_ext/src/daemon/task_prompt/context*.rs`、`rust_ext/src/daemon/task_prompt/context_tests*.rs` | renderer、dispatch、`cli/**`、`server/**` |
| 4 | RP-04 renderer/hash/security | route、render、canonical、clipping、secret guard、bundle | `rust_ext/src/daemon/task_prompt/{route,render,canonical,redaction,bundle}*.rs` | dispatch、`cli/**`、`server/**` |
| 5 | RP-05 daemon RPC/capability | handler、薄 dispatch 注册、health capability | `rust_ext/src/daemon/task_prompt/handler*.rs`、`rust_ext/src/daemon/dispatch.rs`、`rust_ext/src/daemon/task_loop/capability_control.rs`、`rust_ext/src/daemon/task_prompt/rpc_tests*.rs` | `cli/**`、`server/**`、template resources |
| 6 | RP-06 CLI thin client | `cw task prompt` 与本地三格式 | `cli/task_prompt.py`、`cli/main.py`、`tests/test_task_prompt_cli*.py`、`docs/cli_reference.md` | `db/**`、`server/**`、daemon prompt domain/resources |
| 7 | RP-07 route matrix SSOT repair | generator→JSON/Rust、semantic reconciliation、N baseline | `scripts/gen_route_matrix.py`、`scripts/verify_route_matrix.py`、`deliverables/software-company/tool_migration_matrix.json`、`rust_ext/src/daemon/route_matrix.rs`、`tests/test_*route_matrix*.py` | daemon prompt domain、`server/tools/**`、`cli/**` |
| 8 | RP-08 MCP thin client | `task_get_role_prompt`、生成 N+1 matrix | `server/tools/tools_task_prompt.py`、`server/mcp_server.py`、matrix generator/JSON/Rust mirror、`tests/test_task_prompt_mcp*.py`、`docs/mcp_tools.md` | `db/**`、prompt resources/domain |
| 9 | RP-09 Skill/docs cutover | cw-task-loop 使用 daemon bundle，模板正文退出客户端 | `.agents/skills/cw-task-loop/**`、`Callwarden 无人值守循环启动模板：*`、`AGENTS.md`、`docs/agent-usage-guide.md`、`scripts/validate_template_compliance.py`、`tests/test_role_prompt_skill*.py` | `rust_ext/src/**`、`rust_ext/resources/**`、`cli/**`、`server/**` |
| 10 | RP-10 E2E/security/release Gate | fresh daemon parity、no-write/no-secret、一次 shared deploy | `tests/test_task_prompt_e2e*.py`、`rust_ext/tests/role_prompt_e2e*.rs`、`scripts/check_client_purity.py`、`scripts/refresh_shared_runtime.ps1`、`docs/evidence/RP-10-*` | `db/**`、历史 evidence、daemon prompt production source |

每卡必须展开 exact allowed/excluded paths、`forbidden_actions`、predecessor task ID、Role Contract、identity
policy、steps、验收、证据、回滚和唯一 idempotency key `role-prompt-v1-rpXX`。策略禁止项不能伪装成 path glob。

每卡最多 4 个实现步骤；超过 4 个步骤或出现第二个主要 ownership 时必须由 Planner 重拆，不能由 Executor
边做边扩 scope。

### 15.1 卡级最低验收命令

下表是 task manifest 必须展开的最低命令；manifest 还要把测试 module/function 名冻结为实际存在的精确名称。

| 卡 | 最低验收命令 |
| --- | --- |
| RP-00 | `tokenslim run C:\Python314\python.exe scripts/validate_role_prompt_spec.py --inventory docs/evidence/RP-00-source-clause-inventory.json --resolution docs/evidence/RP-00-review-resolution-matrix.json --spec docs/design/cw-role-prompt-compiler-v1-frozen-spec.md --manifest deliverables/software-company/role-prompt-v1-task-manifest.json`；`tokenslim run C:\Python314\python.exe -m pytest tests/test_role_prompt_spec_validator.py -q`；`tokenslim run git diff --check` |
| RP-01 | `tokenslim run cargo check --manifest-path rust_ext/Cargo.toml --no-default-features`；`tokenslim run cargo test --manifest-path rust_ext/Cargo.toml --no-default-features role_prompt_validator`；`tokenslim run git diff --check` |
| RP-02 | `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml --no-default-features role_prompt_assets`；legacy byte/hash probe；`tokenslim run git diff --check` |
| RP-03 | `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml --no-default-features task_prompt_context`；no-write trace；`tokenslim run git diff --check` |
| RP-04 | `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml --no-default-features task_prompt_renderer`；golden/determinism/secret/budget matrix；`tokenslim run git diff --check` |
| RP-05 | `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml --no-default-features task_prompt_rpc`；`tokenslim run cargo check --manifest-path rust_ext/Cargo.toml --no-default-features`；isolated daemon round-trip；`tokenslim run git diff --check` |
| RP-06 | `tokenslim run C:\Python314\python.exe -m pytest tests/test_task_prompt_cli.py -q`；`tokenslim run C:\Python314\python.exe scripts/check_client_purity.py`；daemon unavailable/no-fallback fixture；`tokenslim run git diff --check` |
| RP-07 | 先实现并测试本卡新增的 `--emit-rust`/`--check`；再运行 `tokenslim run C:\Python314\python.exe scripts/gen_route_matrix.py --check`；`tokenslim run C:\Python314\python.exe scripts/verify_route_matrix.py`；`tokenslim run C:\Python314\python.exe -m pytest tests/test_route_matrix_generation.py -q`；`tokenslim run git diff --check` |
| RP-08 | `tokenslim run C:\Python314\python.exe -m pytest tests/test_task_prompt_mcp.py -q`；route verifier N+1；`tokenslim run C:\Python314\python.exe scripts/check_client_purity.py`；`tokenslim run git diff --check` |
| RP-09 | `tokenslim run C:\Python314\python.exe scripts/validate_template_compliance.py --self-test`；`tokenslim run C:\Python314\python.exe -m pytest tests/test_role_prompt_skill.py -q`；`tokenslim run git diff --check` |
| RP-10 | full Rust suite；full Python suite；CLI/MCP/Skill live parity；no-write/no-secret scan；fresh runtime receipt；`tokenslim run git diff --check` |

若仓库在建卡时的真实测试文件名或 tokenslim 检测命令不同，Planner 必须在 task manifest freeze 阶段根据
`.tokenslim-context.md` 和已创建测试 module 修订命令并重新 review；Executor 不得自行把不存在的 filter 当作通过。

## 16. 测试与验收矩阵

### 16.1 正向

- task ID only 的 CLAIM、REVISE、REVIEW、ADJUDICATE 分别选择正确 Role Contract；
- BLOCKED、WAITING、COMPLETE 生成 non-actionable system prompt；
- historical Executor primary ID 可精确编译；正文 alias 不可替代 primary lookup；
- 同一 snapshot 连续 100 次三个 hash 一致；
- CLI JSON 与 MCP bundle 完全一致；card/llm 只是本地显示；
- capability health 的 manifest/policy hash 与 bundle 一致；
- route matrix reconciliation N 全绿，新增工具后 N+1 全绿。

### 16.2 Authority 负向

- task 不存在；
- binding/capture 缺失、多值或 mismatch；
- caller guard 过期；
- Contract/Role Contract 缺失、revision/hash 不可验证；
- identity policy unresolved；
- template ID missing/empty/unknown/hash mismatch/route mismatch；
- source watermark 在读取期间变化；
- unknown decision/action/role；
- Planner capability 未启用却出现 PLAN route；
- user route 在 v1 中出现。

### 16.3 安全与预算负向

- task 文本含伪 closing tag 或 prompt injection；
- Authorization/Bearer/cookie/credential/lease/private key；
- request 带未知 auth/role/workspace/format 字段；
- 多字节 Unicode clipping 边界；
- 必填 authority/Contract/scope 超限硬失败；
- 可选长文本 omissions 完整且无前缀碰撞；
- prompt 64 KiB、bundle 256 KiB、path count/length 边界；
- bundle 不能直接用于 claim/verdict/apply/close。

### 16.4 No-write/thin-client

- RPC 前后 DB fingerprint、`total_changes`、event sequence 不变；
- Python 新代码不导入 DB/SQLite/PyO3 authority；
- CLI/MCP 不读模板、不推导 role/workspace、不实现 local fallback；
- Skill 不复制 production prompt 或治理枚举；
- daemon unavailable 原样失败；
- dispatch/main 只有薄注册。

### 16.5 Regression

- 既有 `get_role_view` blind-view 语义与 golden 不变；
- root `task.create` byte parity；
- route matrix 现有 MCP tools 全覆盖，无新增 Python compat；
- full Rust/Python suite 全绿，不接受“已归因红灯”作为 release 条件；
- `git diff --check` 干净。

## 17. Evidence、VCS 与发布

### 17.1 每卡 evidence manifest

至少包含：

- task ID、step ID、predecessor、Contract/Role Contract revision/hash；
- workspace ID、instance、binding、capture、snapshot/watermark；
- allowed/excluded/dirty paths；
- commit full SHA 与 ledger entry；
- template/manifest/compiler policy/context/prompt/bundle hash；
- 正向、负向、回归命令与结果；
- secret scan、DB no-write trace；
- runtime PID/binary fingerprint/receipt（仅部署卡）；
- Reviewer verdict、Adjudicator action 与 handoff provenance。

证据不得包含 raw credential 或 lease token。

### 17.2 VCS 纪律

每卡完成并 report 后：

1. `git add <exact whitelist paths>`，禁止 `git add .`；
2. commit message 必须包含 task ID；
3. `git rev-parse HEAD` 获取完整 SHA；
4. append-only 写入 `cw_task_commit_ledger.json`；
5. commit/ledger 后再尝试 `cw refresh --all`；失败留痕，不伪造成功。

### 17.3 部署

RP-05～RP-09 只使用隔离临时 daemon 测试，不切 shared runtime。RP-10 在所有前卡 closed、全矩阵全绿、
独立 Reviewer PASS 后，才执行一次受控 `refresh_shared_runtime.ps1`，并由 Adjudicator 使用当前成功 receipt
完成最终裁决。随后必须重新跑 live HTTP/CLI/MCP/Skill round-trip。

## 18. 回滚

回滚只允许：

1. 禁用 `role_prompt_compiler_v1` capability；
2. 关闭 CLI/MCP 新入口；
3. Skill 回到既有只读 next-action role card。

禁止：

- 回落 Python Prompt Compiler 或 SQLite/PyO3 authority；
- 修改已发生的 task、Contract、lease、verdict、handoff 或 lifecycle 历史；
- 删除模板历史、evidence 或 ledger；
- 让客户端自行选择角色/模板作为临时方案。

## 19. Successor capabilities

以下只保留依赖，不进入 v1 schema 或 production manifest：

1. `decision_request_v1`：权威选项、推荐、风险、未选择后果和 `decision.respond`；
2. `planner_governance_v1`：Planner Contract 与 PLAN/replan assignment；
3. `prompt_issuance_ledger_v1`：把 top-level `bundle_hash` 绑定后续 mutation；
4. `role_worker_dispatch_v1`：按最新 authority 启动/唤醒后台 worker；
5. model admission/provider-model-instance separation；
6. 多租户签名模板、审批发布与远端任务控制台。

依赖顺序：

```text
Role Prompt Compiler v1
  -> prompt_issuance_ledger_v1
  -> role_worker_dispatch_v1 / prompt bake-off
```

issuance ledger 与 outcome attribution query 关闭前，不得发布“某模板/模型导致某结果”的 bake-off 结论。

## 20. 独立 Reviewer 必答问题

1. public request 是否真正 task-id-first，且没有客户端 role/workspace 推导？
2. daemon 是否在同一只读 snapshot 内完成 next-action、Contract、template 和 render？
3. bundle 是否明确不能替代 claim/lease/mutation authorization？
4. BLOCKED 是否只指向内部恢复，没有 user route 或伪 decision authority？
5. legacy Role Contract ID/hash 是否保持 byte-exact，唯一 alias exception 是否未扩散？
6. template ID missing/empty/unknown/hash/route mismatch 是否稳定可区分？
7. canonicalization、clipping、预算和 hash include/exclude 是否闭合、无自引用？
8. workspace guard 是否完全排除 hash，并使用 `after_authority_refresh`？
9. retry class 是否只有四值且每个错误唯一映射？
10. build/runtime 是否只有一套 validator/parser，且不存在 draft bypass？
11. composed golden 是否同时覆盖 legacy/current/system？
12. Gate cards 是否达到可建卡粒度，root-create 兼容和 GATE-0 lease/identity 是否可机器验收？
13. route 三域是否避免错误追求 T=M=D，并完成五条具体 reconciliation？
14. inventory completeness 是否由 Reviewer 人工冻结，机器只验证 resolution 一对一？
15. RP-00 后 Executor 是否只需本规范和 task manifest，不再解释 R1～R4 overlay？
16. RP-01～RP-10 是否保持单一 ownership、串行 closed gate 与 shared runtime 单次部署？

## 21. 最终原则

Role Prompt Compiler v1 只把 daemon 已经知道、已经绑定、可以验证的当前任务事实编译成 LLM 工作说明。
它不创造治理事实，不让客户端选择角色，不让 prompt 变成授权票据，也不把内部技术问题转嫁给用户。
历史文档解释“为什么这样决定”，本规范定义“最终必须实现什么”，机器任务清单定义“由谁按什么边界实现”。
