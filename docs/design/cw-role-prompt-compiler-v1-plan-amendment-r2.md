# Call Warden Role Prompt Compiler v1 方案修订 R2

> 状态：`DRAFT_FOR_INDEPENDENT_REVIEW`
> 修订日期：2026-08-31
> 修订角色：Planner
> 规划锚点：`T-1787203926824-9f873bfc`
> 基线文件：`docs/design/cw-role-prompt-compiler-v1-implementation-plan.md`
> 基线 SHA-256：`543711EA2FEE11C1C4106825C794CD08FC7A715AA1E7E75B9DAE04D151BC5FC5`
> 修订方式：append-only overlay；不得改写 R1

## 1. 修订结论

独立评审给出 `CONDITIONAL PASS`。本修订接受并闭合 1 个 blocking 与 3 个 advisory：

| finding | 结论 | R2 处置 |
| --- | --- | --- |
| BLOCKING-1：`blocked_recovery.md` 没有升级分型契约 | 接受 | 新增强类型 escalation schema；内部技术恢复与人类 decision request 分成两个模板；未知类别 fail closed |
| A1：validator 可能成为可选脚本 | 接受并加强 | 生产模板由 Rust build-time validator 强制校验；任何 production template 错误使 `cargo check/build/test` 失败 |
| A2：缺少 `model_requirements` | 接受 | 在 R1 schema 冻结前加入可空、版本化字段；v1 只传播和哈希，不虚假声称已调度或已强制执行 |
| A3：bake-off 缺 prompt provenance 前置 | 接受 | 明确 `RP-08 → prompt issuance/source bundle binding → bake-off` 的硬依赖，不允许提前做归因实验 |

除本文件明确 supersede 的条款外，R1 其余内容继续有效。

## 2. 规范性优先级

实施与评审按以下顺序解释：

1. 仓库 `AGENTS.md` 与 `.agents/skills/cw-task-loop/references/role-protocol.md`；
2. 本 R2 amendment；
3. R1 实施方案；
4. 聊天、评审摘要和其他非权威说明。

发生冲突时，R2 只覆盖本文件列出的章节和任务合同，不扩张 Prompt Compiler v1 的生产 scope。

## 3. BLOCKING-1：升级分型成为 schema 与模板硬契约

### 3.1 分类枚举

Prompt Context 新增强类型字段：

```json
{
  "escalation": {
    "schema_version": "task_escalation_v1",
    "classification": "DECISION|EXTERNAL_FACT|ACCEPTANCE|SENSITIVE_AUTHORIZATION|TECHNICAL|DATA_REPAIR|ENV_FAULT|CONTRACT_GAP|BINDING_GAP|AUTH_RECOVERY|CAPABILITY_GAP|UNRESOLVED",
    "human_routable": false,
    "owner_route": "planner|executor|reviewer|adjudicator|internal_governance|user|unresolved",
    "decision_request_required": false,
    "recovery_action": "...",
    "reason_codes": [],
    "source": "daemon_next_action|task_contract|role_contract|governance_classifier"
  }
}
```

分类集合固定如下：

#### 人类可路由 allowlist

| classification | 含义 | 必须满足 |
| --- | --- | --- |
| `DECISION` | 两条以上安全路线，需要采购方选择业务、成本或架构取舍 | 至少 A/B 两项、推荐项、风险、未选择后果、自由文本入口 |
| `EXTERNAL_FACT` | 只有采购方掌握且系统无法自行取得的外部事实 | 明确缺少的事实及取得后系统自动执行的下一步 |
| `ACCEPTANCE` | 业务验收、UAT 或合同约定签收 | 明确验收对象、判定标准和不通过后的内部整改路线 |
| `SENSITIVE_AUTHORIZATION` | 不可逆或高风险权限动作需要明确授权 | 明确授权范围、时效、影响、回滚/不可回滚事实 |

`SENSITIVE_AUTHORIZATION` 来自现行共享角色协议对“敏感授权”的明确允许，不得被泛化为普通技术确认。

#### 禁止路由人类 denylist

| classification | 内部责任 |
| --- | --- |
| `TECHNICAL` | Executor 或内部技术 owner 诊断、修复、验证 |
| `DATA_REPAIR` | Planner 冻结安全清洗方案，Executor/治理维护能力执行 |
| `ENV_FAULT` | Executor/平台能力修复环境、部署、路径、锁或 runtime |
| `CONTRACT_GAP` | Planner/治理维护路径追加或修复 Contract capability |
| `BINDING_GAP` | Planner/治理维护路径恢复 workspace/task binding |
| `AUTH_RECOVERY` | 内部认证恢复实现；若动作本身需要敏感授权，另建 `SENSITIVE_AUTHORIZATION` decision，而不是把技术操作交给用户 |
| `CAPABILITY_GAP` | Planner 安排最小 daemon/CLI/MCP capability 实现 |
| `UNRESOLVED` | fail closed 到 internal governance classifier，不得默认 user |

### 3.2 不变量

1. `human_routable=true` 当且仅当 classification 属于人类 allowlist。
2. `owner_route=user` 当且仅当 `human_routable=true` 且 decision request 结构完整。
3. denylist 与 `UNRESOLVED` 必须满足：
   - `human_routable=false`；
   - `owner_route != user`；
   - `decision_request_required=false`；
   - `recovery_action` 指向内部角色或 capability 修复。
4. 客户端、Skill 和模板正文不能改变 classification、human_routable 或 owner route。
5. 任务标题、description、聊天 Handoff 和 evidence note 不能作为分类 authority。
6. 无法形成唯一分类时使用 `UNRESOLVED`，禁止以“方便处理”为由默认升级用户。

### 3.3 模板拆分

R1 的单一 `blocked_recovery.md` 定义被以下规则收紧：

- `blocked_recovery.md`：只服务 denylist/`UNRESOLVED`，输出内部 owner、根因、复现证据、唯一安全恢复路径和后续验证；禁止要求用户改库、补 Contract、修环境、重试命令或提供内部 credential。
- 新增 `decision_request.md`：只服务人类 allowlist，输出结构化选择题/事实请求/验收/敏感授权；不得夹带技术修复操作。

模板选择矩阵增量：

| context | template | executable | next route |
| --- | --- | --- | --- |
| blocked + denylist/UNRESOLVED | `blocked_recovery.md` | false | internal owner，绝不 user |
| blocked/waiting + human allowlist | `decision_request.md` | false | user/控制台；仅表达请求，不伪造 mutation |

在 `decision_request_v1` capability 未启用时，`decision_request.md` 只能生成
`persistence_status=protocol_reserved` 的非执行型提示词包；客户端不得伪造已落库 decision event。

### 3.4 Template manifest 强制字段

`manifest.json` 增加：

```json
{
  "escalation_policy": {
    "schema_version": "task_escalation_v1",
    "human_allowlist": [
      "DECISION",
      "EXTERNAL_FACT",
      "ACCEPTANCE",
      "SENSITIVE_AUTHORIZATION"
    ],
    "human_denylist": [
      "TECHNICAL",
      "DATA_REPAIR",
      "ENV_FAULT",
      "CONTRACT_GAP",
      "BINDING_GAP",
      "AUTH_RECOVERY",
      "CAPABILITY_GAP",
      "UNRESOLVED"
    ],
    "template_routes": {
      "internal": "blocked_recovery.md",
      "human": "decision_request.md"
    }
  }
}
```

manifest 缺类别、重复类别、allow/deny 交集、未覆盖枚举、denylist 指向 human 模板或
`UNRESOLVED → user` 时，build-time validator 必须失败。

## 4. A1：validator 升级为构建期硬门禁

### 4.1 唯一权威实现

R1 中的 Python validator 不再是唯一或最终门禁。RP-01 必须新增：

```text
rust_ext/build.rs
rust_ext/build/role_prompt_validator.rs
rust_ext/build/role_prompt_validator_tests.rs（或等价测试入口）
```

约束：

- `build.rs` 在每次 `cargo check/build/test` 时验证 production manifest 与模板资源；
- 对 `rust_ext/resources/role_prompts/**` 输出 `cargo:rerun-if-changed`；
- validator 失败时 build script 返回非零，使编译直接失败；
- validator domain 置于独立小文件，目标 <500 行、硬上限 800 行；
- Python 脚本可保留为本地诊断/fixture 工具，但不得拥有不同的 schema 语义，也不得成为 release 唯一依据；
- runtime loader 仍须验证嵌入 manifest/hash，防止 build-time 与 runtime 解释漂移。

### 4.2 构建期检查项

至少包括：

1. manifest/schema JSON 可解析；
2. template ID、版本、路径唯一且无目录逃逸；
3. 模板文件存在、UTF-8/LF、hash 精确；
4. placeholder 只使用声明集合，必填 placeholder 齐全；
5. role/action/template 映射完整且无多义；
6. escalation allow/deny 集合互斥、完整、固定；
7. denylist 不能映射 `decision_request.md`/user route；
8. `blocked_recovery.md` 与 `decision_request.md` 的用途不能互换；
9. production 模板不含 credential/lease token/Authorization/任意文件 include；
10. 每个模板和 manifest 均进入编译期 hash 清单。

### 4.3 负向测试

Rust test fixtures 必须覆盖：

- 删除 `TECHNICAL` 分类；
- 同一分类同时进入 allow/deny；
- `UNRESOLVED` 映射 user；
- denylist 映射 `decision_request.md`；
- `blocked_recovery.md` 缺内部 recovery placeholder；
- `decision_request.md` 缺选项/推荐/自由文本 placeholder；
- template hash 漂移；
- 路径逃逸；
- 未知/缺失 placeholder；
- secret pattern。

### 4.4 RP-01 合同增量

RP-01 allowed paths 增加：

```text
rust_ext/build.rs
rust_ext/build/role_prompt_validator.rs
rust_ext/Cargo.toml（仅 build-dependencies，若确有必要）
rust_ext/resources/role_prompts/v1/decision_request.md
```

RP-01 acceptance commands 至少为：

```powershell
tokenslim run cargo check --manifest-path rust_ext/Cargo.toml --no-default-features
tokenslim run cargo test --manifest-path rust_ext/Cargo.toml --no-default-features role_prompt_validator
tokenslim run C:\Python314\python.exe scripts/validate_role_prompt_templates.py --self-test
tokenslim run git diff --check
```

其中前两条是 release blocking；Python self-test 只提供诊断与交叉验证。

## 5. A2：在 v1 schema 预留 `model_requirements`

### 5.1 Bundle 字段

Role Prompt Context/Bundle 新增可空对象：

```json
{
  "model_requirements": {
    "schema_version": "model_requirements_v1",
    "source": "role_contract|task_contract|unspecified",
    "provider_allowlist": [],
    "model_allowlist": [],
    "minimum_context_window_tokens": null,
    "minimum_reasoning_level": null,
    "required_capabilities": [],
    "separation": {
      "distinct_provider_from_roles": [],
      "distinct_model_from_roles": [],
      "distinct_instance_from_roles": []
    },
    "declaration_status": "absent|present",
    "enforcement_status": "not_applicable_v1|not_enforced_v1|satisfied|unsatisfied|unresolved",
    "model_admission_required": false
  }
}
```

### 5.2 v1 语义

- 字段来源只能是 Task/Role Contract；客户端和模板不能补默认模型。
- Contract 未声明时返回 `source=unspecified`、`declaration_status=absent`、
  `enforcement_status=not_applicable_v1`，而不是省略字段；不存在的要求不会阻断已有任务。
- Contract 已声明要求但 v1 尚无 worker/model admission verifier 时，返回 `declaration_status=present`、
  `enforcement_status=not_enforced_v1`、`model_admission_required=true`。
- v1 Prompt Compiler 只传播、canonicalize 和 hash；不选择模型、不启动 worker、不声称 separation 已满足。
- 若现有 next-action 已能证明 instance/session independence，可在 provenance 中单独报告；不能冒充 provider/model separation。
- `model_requirements`（包括明确的 unspecified 状态）进入 `context_hash` 和 `bundle_hash`。
- Prompt bundle 始终 `valid_for_claim=false`；声明了 model requirement 时，后续 worker dispatcher 启用前必须把
  `not_enforced_v1` 转换为可验证的 preclaim gate，不能只消费自然语言模板。

### 5.3 RP-00/RP-03 增量

RP-00 冻结 `model_requirements_v1` schema 与 canonicalization。RP-03 增加下列负向测试：

- 客户端传入 Contract 未声明的 model allowlist；
- 模板擅自指定模型；
- separation 字段排序导致 hash 不稳定；
- 未声明要求没有稳定映射为 `not_applicable_v1`；
- 已声明要求没有稳定映射为 `not_enforced_v1 + model_admission_required=true`；
- `not_enforced_v1` 被误显示为已满足。

`satisfied|unsatisfied|unresolved` 是后续 admission verifier 使用的协议保留值；v1 不得自行生成
`satisfied`。Role eligibility 的 `executable` 与 model admission 状态分开表达，任何状态都不能把 prompt bundle
变成可直接 claim 的授权物。

## 6. A3：Prompt provenance 与 bake-off 硬依赖

### 6.1 v1 输出占位

Bundle 新增：

```json
{
  "provenance_binding": {
    "source_bundle_hash": "sha256:<current bundle_hash>",
    "mutation_binding_status": "not_supported_v1",
    "issuance_ledger_status": "not_supported_v1"
  }
}
```

该字段只陈述能力状态，不允许 v1 客户端把 hash 塞进未支持的 mutation 参数或本地伪造 ledger。

### 6.2 Successor Gate

路线图硬依赖改为：

```text
RP-08 role_prompt_v1 release gate
    ↓
RP-S1 prompt_issuance_ledger_v1 + source_bundle_hash mutation binding
    ↓
RP-S2 prompt/agent/outcome attribution query
    ↓
Prompt bake-off / A-B test / model comparison
```

禁止在 RP-S1、RP-S2 closed 前发布任何声称可归因到 prompt/template/model 的 bake-off 结论。此前只允许测试
Prompt Compiler 的确定性、安全性和 transport 一致性，不能把任务结果归因于某个提示词。

### 6.3 RP-08 增量

RP-08 evidence manifest 必须写明：

- `mutation_binding_status=not_supported_v1`；
- `issuance_ledger_status=not_supported_v1`；
- bake-off 状态为 `blocked_by_successor_gate`；
- RP-S1/RP-S2 的 successor task placeholder 与创建条件。

RP-08 close 只代表 Prompt Compiler v1 可发布，不代表 prompt attribution/bake-off 已具备。

## 7. 对 R1 的精确 supersede 映射

| R1 位置 | R2 处理 |
| --- | --- |
| §4.1 模板资源清单 | 增加 `decision_request.md`；`blocked_recovery.md` 收紧为 internal-only |
| §5.1 顶层响应 | 增加 `escalation`、`model_requirements`、`provenance_binding` |
| §5.3 Hash | 三个新对象的稳定表示进入 context/bundle hash |
| §5.4 Staleness | 保留；补充 mutation binding 当前为 `not_supported_v1` |
| §6 BLOCKED 模板选择 | 由 escalation allow/deny 分类决定 internal recovery 或 human decision request |
| §7 模板语言 | 增加 escalation 分类/路由不可由模板改写的约束 |
| §8 错误与阻断 | `UNRESOLVED` 是 internal blocked，不得默认 user |
| §12.1 manifest defaults | 增加 escalation/model/provenance schema 与 build-time validator gate |
| RP-00 | 增加三个 schema 与 bake-off successor gate 冻结 |
| RP-01 | 增加 `decision_request.md`、Rust build-time validator 和负向矩阵 |
| RP-03 | 增加 escalation/model/provenance canonicalization 与安全测试 |
| RP-08 | 增加 successor gate 状态证据；不得声称具备 attribution |
| §20 后续能力 | `prompt_issuance_ledger_v1/source_bundle_hash` 明确排在 bake-off 前 |

R1 其他目标、非目标、RPC 边界、只读语义、文件规模、CLI/MCP/Skill 薄客户端、secret/injection/staleness、
串行 RP-00～RP-08 和 rollback 条款保持不变。

## 8. 修订后发布门禁增量

在 R1 §16 的 12 项 Gate 基础上，新增：

1. escalation 枚举 allow/deny 全覆盖且无交集；
2. denylist/UNRESOLVED 无任何 user route；
3. human route 必须有结构化 decision request 所需字段；
4. production template 错误会使 `cargo check` 失败；
5. build-time validator 与 runtime loader 对 manifest/hash 结论一致；
6. `model_requirements` 明确存在且 hash 稳定；
7. model requirement 的 absent/present 与 not-applicable/not-enforced 状态准确、稳定且不虚报 satisfied；
8. RP-08 evidence 明确记录 attribution/bake-off 尚不可用；
9. RP-S1/RP-S2 未关闭时，任何 bake-off 发布 Gate 必须失败。

## 9. 独立复审清单

Reviewer 应重点验证：

1. 技术、数据、环境、Contract、binding、认证恢复和 capability 缺口是否都不能升级用户；
2. `SENSITIVE_AUTHORIZATION` 是否被严格限制为授权，而非让用户执行技术操作；
3. `UNRESOLVED` 是否 fail closed 到 internal governance；
4. `blocked_recovery.md` 是否绝不产生用户操作指令；
5. `decision_request.md` 是否具备选项、推荐、影响、未选择后果和自由文本入口；
6. build-time validator 是否确实在普通 `cargo check` 中运行并能阻止编译；
7. Python validator 是否只是诊断，且没有不同的 schema 解释；
8. `model_requirements` 缺失/unsatisfied/unresolved 是否不会被伪装成满足；
9. bundle hash 是否绑定 escalation/model/provenance 的 canonical 状态；
10. bake-off 是否被 RP-S1/RP-S2 硬阻断，而非只写成建议。

## 10. 实施顺序修订

1. 独立 Reviewer 先复审 R1 + R2；
2. 复审 PASS 后，RP-00 冻结合并后的规范，不改写 R1/R2 历史；
3. RP-01 在任何 production template 编写完成前先建立 build-time validator 骨架；
4. validator 能阻止无效模板编译后，再提交全部 v1 模板资产；
5. RP-02～RP-08 继续按 R1 串行；
6. RP-08 closed 后只允许进入 RP-S1，不允许直接进入 bake-off；
7. RP-S1、RP-S2 均 closed 后，Planner 才能冻结 bake-off 任务合同。

---

R2 的核心裁决是：**“是否需要人类”不是模板措辞，而是 daemon 的强类型分类与 allowlist；生产模板是否合法也不是可选脚本意见，而是 Rust 构建是否成功的前置条件。**
