# Call Warden Role Prompt Compiler v1 方案评审裁决 R4

> 状态：Planner append-only 评审裁决稿，待独立 Reviewer 审查。
> 目标：裁决针对 R3 的多份评审意见，关闭争议后直接进入 RP-00 merged frozen spec；不再继续创建 R5/R6 overlay。
> 非目标：本稿不修改生产代码、daemon capability、任务状态、Role Contract 或 runtime。

## 1. 冻结输入

本轮只裁决以下精确输入：

| source | SHA-256 | lines | 用途 |
| --- | --- | ---: | --- |
| R1 `cw-role-prompt-compiler-v1-implementation-plan.md` | `543711EA2FEE11C1C4106825C794CD08FC7A715AA1E7E75B9DAE04D151BC5FC5` | 1321 | 初始完整方案 |
| R2 `cw-role-prompt-compiler-v1-plan-amendment-r2.md` | `1B09702BFDCA57E1090D46A83D4021535FAE29F5E67C2E09E57E321CDE6DF280` | 384 | 第一轮评审修订 |
| R3 `cw-role-prompt-compiler-v1-plan-amendment-r3.md` | `67C09BBCE67F5214F3AFB9BBF67818B2B807D1EAD3822718B4CCFB3D6AD5220C` | 645 | 事实裁决与合并门禁 |

R1/R2/R3 均保持不可改写。R4 只追加裁决；R4 通过后，RP-00 生成一份自包含的
`cw-role-prompt-compiler-v1-frozen-spec.md`，后续 Executor 只引用 frozen spec hash。

## 2. 最初问题与方案评价标准

最终要解决的问题不是“把模板搬进 Rust”本身，而是：用户或后台 worker 只提供精确 `task_id`，daemon 在同一
authority snapshot 中确定当前任务、角色、Contract、允许动作、证据与下一棒，返回可直接供 LLM 使用的工作提示；
Python CLI/MCP/Skill 只做 HTTP 薄客户端，不选择角色、不拼模板、不读数据库，prompt 也不能替代 claim、lease 或
mutation authorization。

因此裁决按以下优先级，而不是按 Reviewer 人数投票：

1. 不破坏现有 daemon authority、历史 Role Contract 与 append-only 证据；
2. public API 保持 task-id-first、single-snapshot、read-only、thin-client；
3. 只实现当前有权威数据源和消费者的能力，未来能力显式 successor；
4. hash、route、预算和任务合同必须可机械验证；
5. 历史文档可追溯，实施规范必须单一、自包含。

## 3. 评审意见去重与相互矛盾

附件中的第一份评审全文重复出现两次，只计为一个独立意见。实质意见集合为 A、B 两组。

### 3.1 相互矛盾一：R3 hash 漂移是否阻断

- 评审 A 使用了更早聊天中的 `98C59182...`/615 行；随后用户要求增加 R1/R2/R3 无损合并门禁，R3 被明确修订并
  重新公布为 `67C09BBC...`/645 行。
- 当前 R3 仍是未提交的 Planner 设计稿，没有绑定 daemon report/verdict，也没有以旧 hash 形成正式 evidence event。

裁决：**旧评审输入已过期，不构成当前设计 blocker，也不需要伪造 correction evidence。** 正式 Reviewer 只能
审查 §1 的冻结 hash。如果未来已有 daemon event 绑定旧 hash，才需要 append-only correction；本轮没有该事实。

### 3.2 相互矛盾二：legacy Executor template ID 以谁为准

历史模板正文首部自述：

```text
cw.aprime.executor-planner.startup.v1
```

但当前 Rust/CLI 默认合同、历史 probe 和真实 Role Contract 一致绑定：

```text
cw.aprime.executor.startup.v1
hash=59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6
```

评审 B 建议把 manifest primary ID 改为正文自述 ID。该建议会使现有 Role Contract 无法 exact lookup，违背“历史
合同不改写”的首要不变量。

裁决：**拒绝改写 primary ID。** legacy manifest primary key 必须使用 Role Contract 已绑定的
`cw.aprime.executor.startup.v1`；正文保持 byte-exact，不修改其自述。manifest 对该唯一历史例外增加：

```json
{
  "template_id": "cw.aprime.executor.startup.v1",
  "source_declared_template_id": "cw.aprime.executor-planner.startup.v1",
  "legacy_alias_mismatch": true,
  "content_sha256": "sha256:59a459f7786097c671d48fbeec6e361c12d7a95bdec4e3722169d68d5d6a73f6"
}
```

build gate 只能按该 exact ID+hash 接受这一条例外；current/new template 的 primary ID 与正文声明必须一致。
测试必须证明：按历史 Role Contract ID 可编译；擅自换成正文自述 ID 会对历史合同 fail closed。

### 3.3 相互矛盾三：decision request 错误码是否属于 v1

评审 A 认为 `E_TASK_PROMPT_DECISION_REQUEST_REQUIRED` 是 v1 防止技术问题升级用户的硬门禁；评审 B 指出
v1 route matrix 没有 user route，错误码不可达。

裁决：**评审 B 正确。** v1 防止技术问题交用户的机制是 BLOCKED→`blocked_recovery`、
`routing_state=non_actionable` 和不存在 user template；不需要一个不可达错误码。

- v1 production manifest、error table 和测试删除 `E_TASK_PROMPT_DECISION_REQUEST_REQUIRED`；
- v1 收到未知 user route 时返回 `E_TASK_PROMPT_UNSUPPORTED_ACTION`；
- 该错误码和 `decision_request.md` 只在 `decision_request_v1` successor 中重新设计，不提前占位。

## 4. 采纳并收紧的意见

### 4.1 `bundle_hash` 使用最小闭集

R3 `context_hash` 已绑定 authority、routing、Contract、template、watermark、omissions 与 authorization。再次把这些
字段平铺进 `bundle_hash` 不会产生自引用，但制造两处同步定义，没有收益。

frozen spec 将 `bundle_hash` 唯一输入收敛为：

```text
schema_version
task_id
prompt_kind
context_hash
prompt.sha256
```

所有字段使用版本化 canonical JSON object 计算，不使用字符串拼接。`bundle_hash` 自身、时间、request/trace、display、
retry metadata 均排除。

### 4.2 optional workspace guard 的语义闭合

`expected_workspace_instance_id` 是 caller CAS assertion，不是 daemon authority 数据：

- 不进入 `context_hash`、`prompt.sha256` 或 `bundle_hash`；
- mismatch 返回 `E_TASK_PROMPT_AUTHORITY_MISMATCH`；
- `retry_class=after_authority_refresh`：客户端必须重新读取 authority 后发新请求，禁止原请求盲重试；
- 不填 guard 时 daemon 仍只从 task immutable binding 解析 workspace，客户端不得自行推导。

`after_authority_refresh` 与 `after_authority_change` 分开：前者表示 caller assertion 过期，后者表示 task/Contract/
binding 本身必须发生权威变化。

### 4.3 尺寸预算不能留给实现者猜

RP-00 必须把 R1 的下列值保留进 frozen spec：

| 对象 | v1 上限 | 处理 |
| --- | ---: | --- |
| `prompt.text` UTF-8 bytes | 64 KiB | 可省略字段按固定优先级裁剪；仍超限则失败 |
| canonical JSON bundle UTF-8 bytes | 256 KiB | 超限失败 |
| 单个可省略的不可信逻辑字符串 | 8 KiB | JSON escape 前按 Unicode scalar 安全边界裁剪 |
| allowed/forbidden paths | 各 256 项 | 超限失败，不静默删 Contract scope |

RP-00 还必须逐字段冻结所有不可截断 ID/hash/revision/watermark 的数值上限；上限必须来自现有 daemon schema/
transport 约束或显式新 schema 决策，不能由 Executor 自行发明。

### 4.4 composed-bundle golden fixture

RP-02/RP-04 至少提供：

- 三个 legacy role 的完整 composed bundle golden；
- 一个 current role golden；
- BLOCKED/WAITING/COMPLETE 三个 system golden；
- 每个 golden 固定 compiler policy hash、body hash、context hash、prompt hash 和 bundle hash。

百次确定性测试只证明同一实现稳定；golden 才证明 build-time/runtime、legacy/current 和跨实现组合语义一致。

### 4.5 RP-00 历史文件机械保护

RP-00 的 write allowlist 可以包含 frozen spec 新文件，但 exact excluded paths 必须包含：

```text
docs/design/cw-role-prompt-compiler-v1-implementation-plan.md
docs/design/cw-role-prompt-compiler-v1-plan-amendment-r2.md
docs/design/cw-role-prompt-compiler-v1-plan-amendment-r3.md
docs/design/cw-role-prompt-compiler-v1-plan-amendment-r4.md
```

读取这些文件不受影响；任何写入都由 Contract path gate 拒绝。历史保护不能只依赖自然语言 forbidden action。

## 5. RP-00 coverage 门禁改为“人工冻结全集 + 机器一对一”

R3 所写“每条规范性条款必须且只能映射一次”目标正确，但自然语言中的“条款”不能自动可靠识别。宣称纯机器能证明
2350+ 行自然语言无遗漏是不真实的。

RP-00 改为两层门禁：

### 5.1 Reviewer 冻结 source clause inventory

RP-00 先生成 `docs/evidence/RP-00-source-clause-inventory.json`。稳定 `clause_id` 至少覆盖：

- 编号标题下含 MUST/必须/禁止/不得/仅允许/应当的规范段；
- numbered/bulleted normative item；
- schema 的 JSON Pointer 字段；
- 状态机、错误码、route、hash include/exclude、预算、任务和验收表中的每一行；
- code block 中定义 public request/response 或 manifest schema 的每个字段。

独立 Reviewer 负责确认 inventory 对 R1/R2/R3/R4 的人工完整性。自然语言全集完整性不能伪称由脚本证明。

### 5.2 机器验证 resolution 一对一

inventory 冻结后，validator 必须证明：

- 每个 `clause_id` 在 resolution matrix 中恰有一行；
- disposition 只能为 `retained|superseded|rejected|deferred`；
- retained/superseded 指向 frozen spec 的唯一 heading/schema pointer；
- rejected/deferred 有理由，deferred 在适用时有 successor capability；
- 不存在未知 clause ID、重复 ID、空落点或 source hash 不匹配；
- task manifest 只引用 frozen spec hash，不引用 overlay 优先级。

RP-00 的三个主要产物因此扩展为四个：inventory、resolution matrix、merged frozen spec、machine task manifest。

## 6. GATE-0/1A/1B 必须是正式治理卡

R3 的前置方向正确，但 GATE-1A/1B 缺少可直接建卡的 Contract 字段。由于 R3 又规定三道 Gate 关闭后才创建
RP-00，Gate 合同不能推迟到 RP-00 才补。R4 通过后必须先生成并独立审查
`deliverables/software-company/role-prompt-v1-gate-task-manifest.json`；以下 card envelope 是该 manifest 的
规范输入。Gate 不能以自然语言“先修一下”执行。

### 6.1 GATE-0 legacy Epic binding attestation

- owner：Adjudicator mutation；Planner 只冻结输入，独立 Reviewer 提供真实 reviewer lease；
- idempotency key：`role-prompt-v1-gate0-epic-binding-attestation`；
- allowed：task-bound evidence 与 daemon `attest-legacy-workspace-binding`；
- forbidden：代码改动、SQL、路径 hash、synthetic workspace、过期 lease；
- acceptance：Epic binding/capture 回读一致，`next-action` 不再返回 authority unavailable；
- evidence：task/anchor/workspace/instance/binding/capture/request/lease fencing/result hash，不含 raw lease token。

### 6.2 GATE-1A parent-aware governed create daemon

- owner：Executor；独立 root bootstrap task；predecessor=GATE-0 closed；
- idempotency key：`role-prompt-v1-gate1a-parent-aware-create-daemon`；
- allowed：现有 Rust `task.create` domain、dispatch 极薄注册、定向 Rust tests；
- excluded：Python client、SQLite direct path、`task.create_subtask` 新业务实现、Prompt Compiler domain；
- acceptance commands：
  - `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml --no-default-features parent_aware_task_create`；
  - `tokenslim run cargo check --manifest-path rust_ext/Cargo.toml --no-default-features`；
  - `tokenslim run git diff --check`；
- required evidence：parent/binding/Contract/policy 原子成功，以及 unknown parent、unbound parent、workspace mismatch、
  missing Contract、unresolved policy、unknown field、transaction rollback 负向矩阵；
- rollback：不迁移历史 parent；关闭新 parent-aware route/capability 后旧 root create 保持原语义。

### 6.3 GATE-1B parent-aware governed create CLI

- owner：Executor；predecessor=GATE-1A closed；
- idempotency key：`role-prompt-v1-gate1b-parent-aware-create-cli`；
- allowed：CLI parser/thin adapter、daemon client schema、Python fixtures/user guide；
- excluded：Rust task.create business domain、DB/PyO3 authority、Prompt Compiler domain；
- acceptance commands：
  - `tokenslim run C:\Python314\python.exe -m pytest tests/test_task_create_parent_governance.py -q`；
  - `tokenslim run C:\Python314\python.exe scripts/check_client_purity.py`；
  - `tokenslim run git diff --check`；
- required evidence：CLI 参数到 HTTP request 的 byte-level fixture、daemon success/error 原样返回、daemon unavailable
  无 fallback、无 active-workspace/synthetic binding 推导；
- rollback：移除 CLI 新参数入口，不回落本地 DB，不改已创建任务。

Gate task manifest 必须为三张卡展开精确 allowed/excluded path glob、Role Contract、identity policy、step、
predecessor、acceptance、evidence 与 Reviewer/Adjudicator 闭环，并绑定 R4 hash。该 manifest 经独立 Reviewer
PASS 后才允许创建/执行 Gate；RP-00 不再追认或改写已经执行的 Gate 合同。

## 7. route matrix 的正确三域模型

评审指出 verifier 当前打印 matrix=241、MCP registration=242、dispatch=298。要求三者数字相等是错误的：
dispatch 包含 CLI、governance、内部 alias 和非 MCP RPC，本来就是 MCP 工具 surface 的超集。

RP-07 必须冻结三个集合：

```text
T = MCP tool registrations
M = tool migration matrix / generated Rust mirror
D = daemon dispatch RPC methods
```

门禁为：

1. `names(T) == tool_names(M)`，逐项 module/tool name 唯一；
2. `route_matrix.rs == generate(M)`，禁止手改；
3. 对 `rust_native|task_rpc` 行，`rpc_method(M) ⊆ D`，且 alias 必须在 M 中显式声明 canonical/legacy；
4. 对 `python_compat` 行，M、Rust whitelist、Python compat registry 三向一致；
5. `D - rpc_method(M)` 不要求为空，但每个额外 dispatch method 必须由 daemon capability/operation registry 分类为
   `internal|cli|governance|alias|non_mcp_public`，不能成为未登记的 MCP 工具；
6. verifier 不硬编码 239/241/242/298；权威基线 `N=|T|=|M|` 由生成源产生，新工具后为 `N+1`。

因此评审关于“必须解释 298”的方向成立，但不能把所有 dispatch RPC 强塞进 MCP tool matrix。

## 8. R3 其余核心设计维持

以下不因本轮评审改变：

- public production RPC 仍只有 `task.prompt.compile(task_id, optional expected guard)`；
- daemon 在一个只读 authority snapshot 中完成 next-action/context/render；
- CLI/MCP/Skill 不读取模板、不选角色、不读数据库、不实现 Python fallback；
- prompt 永不授权 claim/lease/verdict/apply/close；
- v1 不实现 public preview、通用 placeholder、model admission、decision request、automatic dispatch 或 issuance ledger；
- GATE-0/1A/1B 关闭后才创建 Prompt Compiler feature parent；RP 前卡 closed 才释放后卡；
- shared runtime 只在 RP-10 最终 Gate 一次性部署。

## 9. 最终顺序、RP-00 交付与停止版本叠加

R4 经独立 Reviewer 通过后，不再编写 R5/R6。正确顺序是：

```text
R4 PASS
  → Gate task manifest freeze + independent review
  → GATE-0 closed
  → GATE-1A closed
  → GATE-1B closed
  → 创建 Prompt Compiler feature parent 与 RP-00
  → RP-00 merged frozen spec + RP task manifest PASS
  → RP-01 ... RP-10
```

RP-00 直接生成：

1. `RP-00-source-clause-inventory.json`；
2. `RP-00-review-resolution-matrix.json`；
3. `cw-role-prompt-compiler-v1-frozen-spec.md`；
4. `role-prompt-v1-task-manifest.json`。

frozen spec 必须吸收 R1～R4 的最终有效语义，包括本稿的 legacy ID exception、最小 bundle hash、workspace guard、
预算、golden、历史保护、正式 Gate cards 和 route 三域模型。后续 Contract 只 pin frozen spec hash；R1～R4 只用于
审计“为什么这样决定”，不再由 Executor 运行时 overlay。

## 10. Reviewer 必答问题

1. 是否确认旧 R3 hash 评审已被当前冻结 hash取代，且当前没有 daemon event 需要 correction？
2. 是否确认 historical Role Contract ID 比模板正文自述更有 authority，且 exact legacy exception 不扩散到新模板？
3. 是否确认 v1 无 user route，因此 decision-request 错误码应移至 successor？
4. bundle hash 最小闭集是否完整绑定 context 与 prompt，且没有重复规则？
5. optional workspace guard 是否完全排除 hash，并有独立 refresh retry 语义？
6. 预算和 composed golden 是否足以锁定跨实现行为？
7. inventory completeness 是否由 Reviewer 负责、机器只验证冻结 inventory 的一对一 resolution？
8. GATE-0/1A/1B 是否达到正式建卡粒度？
9. route 三域是否正确区分 MCP tool 与 daemon RPC，避免错误追求 241=242=298？
10. RP-00 后是否只剩一个 frozen spec 实施单源，不再增加 overlay？

---

R4 的最终原则是：**评审负责发现矛盾，但 Planner 必须回到真实 authority 决定谁对；历史合同不能为迎合模板文字而
改写，未来能力不能为显得完整而提前伪造，实施者最终只能面对一份可追溯、可机器验证的冻结规范。**
