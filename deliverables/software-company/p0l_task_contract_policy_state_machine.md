# P0-L Step0：Task Contract Identity-Policy 与 Pre-Claim 状态机映射

**任务**：`T-1787801315246-e3e3a08c`（P0-L：Role Worker Task Contract policy / preclaim enforcement remediation）  
**步骤**：`S-1787801315285-f68a24c8`（map_task_contract_policy_and_preclaim_gap）  
**性质**：只读源码勘察 + 临时库迁移探针；未修改任何生产源码、权威库或任务状态。  
**勘察基线**：`master` HEAD（工作树存在其他并行会话的未提交改动，本文件锚点全部取自已提交源码与只读探针）。

---

## 1. Source-to-RPC Transaction Map

### 1.1 路由层（`rust_ext/src/daemon/dispatch.rs`）

| RPC 方法 | 路由入口 | 性质 | 当前 policy 认知 |
|---|---|---|---|
| `task.create` | `state.handle_task_create`（route 表 L2518 附近） | 写，单事务 | **无**（不解析任何 policy 参数） |
| `task.claim` | `state.handle_task_claim`（route 表，紧随 task.create） | 写，先只读门禁后事务 | **无**（仅 legacy ActionIdentity + contract_claim） |
| `task.next_action` | `handle_task_next_action`（dispatch.rs L1205-1223）→ `task_loop::next_action::evaluate_next_action`，经 `with_conn` 只读 | **纯只读**，无 mutation | **无**（只投影 contract，不表达 policy 要求） |
| `task.contract_bootstrap` | `state.handle_task_contract_bootstrap`（task_collab.rs L2430-2541） | 写，预检后单事务 | **无**（强制 legacy identity + adjudicator + reviewer lease） |
| `task.contract_revise` | `state.handle_task_contract_revise`（task_collab.rs L2543-2673） | 写，预检后单事务 | **无**（同上 + `expected_previous_hash`） |
| `verdict.submit` | `store.handle_verdict_submit`（dispatch.rs L1564；task_collab.rs L4414+） | 受保护写（`is_protected_mutation`，dispatch.rs L3528 测试锚点） | 仍为 legacy reviewer identity + reviewer lease（L4500-4510） |

`identity_policy` / `role_worker_v1` 字符串在全 `rust_ext/src` 中仅出现于
`task_loop/role_worker_test.rs` L191 注释（"由调用方按 identity_policy 决定 legacy / role_worker_v1 路径"）——
**该"调用方"目前不存在**，即 policy 路由完全未实现（gap 实证，与任务草案 §1 一致）。

### 1.2 `task.create` 事务内序列（task_collab.rs `handle_task_create` L1526-1646）

```text
dedup 检查
→ 解析 title / description / parent_id / workspace_id(必填>0) / workspace_instance_id / steps / role_contracts
   ※ 不解析 task_contract_envelope，不解析 identity_policy（见第 2 节必答问题 2）
→ BEGIN tx
  → INSERT tasks (status='open')
  → bind_task_to_workspace（不可变 task→workspace binding，同事务）
  → INSERT task_events ('none'→'open', reason='created')
  → insert_task_steps（每步 pending；任一非法即整事务回滚，L973-991）
  → 若 role_contracts 非空：
      insert_role_contracts（legacy 三角色合同，revision=1）
      → task_create_contract_envelope(task_id,title,description,steps) 生成 **固定 generic** envelope（L827-872）
      → bootstrap_task_governance_contracts（同事务）：
          task_contract_revisions rev1 + 三角色 role_contract_lineages/revisions rev1
          + 所有 pending/in_progress step 的 executor step binding
→ COMMIT
→ 返回 {task_id, governance_projection, workspace_binding_id, workspace_capture_id, ...}
```

关键点：generic envelope 的字段集（objective/interfaces/allowed_edit_scope/acceptance_clauses/
risks/rollback/dependencies/handoff/source）**没有 `identity_policy` 槽位**；
`bootstrap_task_governance_contracts` 把整个 envelope c14n 后存入
`task_contract_revisions.envelope_payload`（task_contract_bootstrap.rs L116-125），
因此 persisted contract 中同样无 policy 字段。

### 1.3 current contract 读取链

| 对象 | 选择规则 | 锚点 |
|---|---|---|
| legacy role contract（claim 门禁用） | `role_contracts WHERE task_id=? AND role=? AND is_current=1 ORDER BY revision DESC LIMIT 1` | task_collab.rs `get_current_role_contract` L892-899 |
| 现代 Task Contract current revision（revise 链用） | `task_contract_revisions WHERE task_id=? ORDER BY revision DESC LIMIT 1` | task_contract_revise.rs L92-98 |
| next_action 投影 | `RoleContractProjection`（binding 链 + revision 行 + c14n payload 一致才 Some） | next_action.rs L64-65 |
| 合同存在性（claim fail-closed） | `COUNT(*) FROM role_contracts WHERE task_id=?` | task_collab.rs `task_has_contracts` L875-884 |

### 1.4 `task.claim` pre-write 门禁序列（task_collab.rs L1648-1758）

```text
parse_action_identity（identity 可选；非空必须含 agent_id/session_id/model_id/role，否则 E_IDENTITY_INCOMPLETE，L117-152）
若 identity 存在：
  1) agent_registrations 查注册：无→E_IDENTITY_UNREGISTERED；非 active→E_IDENTITY_INACTIVE
  2) instance 一致性：注册 instance 非空且不符→E_IDENTITY_INSTANCE_MISMATCH
  3) session 一致性：identity.session_id 必须等于 claim 的 agent_session_id→E_IDENTITY_SESSION_MISMATCH
  4) check_role_independence（instance/session 不得承载冲突角色，L724）
  5) 若 current role contract 存在：contract_claim 必须 skill_id/skill_version/prompt_hash 一致
     →E_CONTRACT_SKILL_MISMATCH / E_CONTRACT_VERSION_MISMATCH / E_CONTRACT_PROMPT_MISMATCH
否则若任务已有合同 → E_IDENTITY_REQUIRED（fail-closed）
※ 全链路没有 role_worker_auth 解析/校验；role string + 注册记录即构成授权
→ BEGIN tx → 状态检查（仅 open/in_progress 可 claim）→ remediation 检查 → step/lease 写入
```

### 1.5 `task.next_action`（只读 evaluator，next_action.rs）

- 严格只读：不激活 workspace、不建 lease、不写 event、不更新 task_steps（L4）。
- 按 12 条规则产出决策：`READY/CLAIM`（executor，L949-953）、`READY/REVIEW`（reviewer）、
  `READY/ADJUDICATE`（adjudicator）、`READY/REVISE`、`WAITING`（active lease 持有者）、
  `BLOCKED/NONE`、`COMPLETE`（L912-916）。
- 返回体含 `task_contract`（id/revision/hash）与 `role_contract` 投影，
  **但无 `identity_policy`、无 `claim_requirements`、无 `requires_role_worker_auth` 字段**。
- A″ 实测（`aprime2_contract_dispatch_observation.json`）：parent/G0 generic contract rev1
  存在，`blocking_conditions=[]`，`identity_policy_present_in_returned_contract=false` —— 
  generic 合同被当作无条件可领取。

### 1.6 `task.contract_bootstrap` / `task.contract_revise` 门禁序列

```text
两者共同预检（只读，先于事务）：
  完整 legacy ActionIdentity（E_IDENTITY_INCOMPLETE）
  role 必须为 adjudicator → E_TASK_CONTRACT_BOOTSTRAP_ROLE_REQUIRED
  evidence_path/evidence_hash + lease token + fencing_counter 必填
  OperationStore dedupe（受保护写串行化）
  task_bound_workspace_id + binding instance 校验 → E_WORKSPACE_AUTHORITY_MISMATCH
  verify_registered_identity（task_supersede.rs L117）
  validate_reviewer_lease_for_adjudication（task_collab.rs L6530-6603，见第 3 节）
    → 陈旧 fencing 映射为 E_TASK_CONTRACT_BOOTSTRAP_FENCED
bootstrap 专属：no_governance_projection 四表全空校验 → 任一存在即 E_TASK_CONTRACT_BOOTSTRAP_NOT_EMPTY（L128-140）
revise 专属：expected_previous_hash 必填且等于 current hash；agent_instance_id 必填（P0-G §3）
→ BEGIN tx → bootstrap_task_governance_contracts / append_task_contract_revision → COMMIT
```

`append_task_contract_revision`（task_contract_revise.rs L87-160）为**哈希链追加**：
`revision = current+1`、`supersedes_revision = current`、`supersedes_contract_hash = current_hash`、
`contract_id` 不可变；INSERT-only，从不 UPDATE/DELETE 历史行——这正是 A″ 逐卡修订必须走的路径。

---

## 2. 必答问题

### Q1：current contract 如何精确选择，如何拒绝 zero/multiple/malformed policy？

**现状**（见 1.3）：
- legacy 层：`(task_id, role, is_current=1) ORDER BY revision DESC LIMIT 1` —— 取"最新 current"，
  不校验是否唯一；
- 现代层：`task_contract_revisions WHERE task_id=? ORDER BY revision DESC LIMIT 1`（revise 链读头）。

**policy 维度当前完全没有拒绝逻辑**（因为 policy 字段不存在）。P0-L step1 的处置设计：
- 引入 typed `IdentityPolicy` parser：合法枚举**仅** `legacy_identity_v1` / `role_worker_v1`；
- **zero**（缺失）与 **unknown**：`E_TASK_IDENTITY_POLICY_REQUIRED`，fail-closed；
- **multiple**（envelope 出现多个 policy 字段/数组）与 **malformed**（非字符串、含 secret 键）：同上拒绝；
- **mismatch**（caller 声明 policy 与持久化 canonical policy 不一致）：`E_TASK_IDENTITY_POLICY_MISMATCH`；
- 选择仍沿用"单任务单 current"语义：policy 从 current revision 的 canonical envelope 中精确读出，
  revision 链断裂（非连续/分叉）即按现有 `ERR_REVISE_CONFLICT` 类语义拒绝，绝不猜测。

### Q2：`task.create` 忽略哪个 caller envelope 字段，如何做到 caller/persisted policy 完全一致？

`handle_task_create`（L1526-1646）**不解析任何合同 envelope 参数**：参数解析清单只有
title / description / parent_id / workspace_id / workspace_instance_id / steps / role_contracts。
合同由内部 `task_create_contract_envelope(task_id, title, description, steps)`（L827-872）
现场生成——caller 即使传入 `task_contract_envelope`（含 policy）也会被整体忽略。

一致化设计（step1 实施）：
1. `task.create` 新增必填（对有 role_contracts 的任务）参数：调用方提供的 **canonical Task Contract envelope**，
   其中必须含 `identity_policy`；
2. daemon 侧用 typed parser 验证 policy 合法性（Q1 枚举）；
3. 持久化时使用 **调用方提供的 envelope 本身**（c14n + sha256 与任务草案合同哈希语义一致），
   替换固定 generic 生成；`task_create_contract_envelope` 仅保留为无合同任务的历史兼容路径或直接退役；
4. **exact match 校验**：事务内回读将写入的 canonical payload，与输入 policy 逐字段比对，
   不一致 → `E_TASK_IDENTITY_POLICY_MISMATCH`，同一事务回滚 tasks/binding/events/steps/contracts
   （现有单事务结构已保证原子回滚，无需新增补偿）；
5. A″ 实测的失败回执（`aprime2_task_creation_receipt.json`，generic projection 拒绝无 pending step 的
   bootstrap）证明该事务原子性已生效，policy 失败复用同一回滚通道即可。

### Q3：reviewer lease 与 adjudicator Role Worker proof 如何分离？

**现状（legacy）**：`validate_reviewer_lease_for_adjudication`（L6530-6603）已实现三重分离：
- 只读 `task_leases WHERE task_id=? AND role='reviewer' AND status='active'`，以
  **token hash 比对**（`sha256(token)==token_hash`，L6558）+ fencing counter + 过期校验证明持有；
- adjudicator 与 reviewer lease holder 的 **agent_id / agent_instance_id / session_id 三不等**
  （L6584-6601：`E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_AGENT/SAME_INSTANCE/SAME_SESSION`）。
- 即 adjudicator 需要有效 token 才能证明"独立 review 已发生"，但 lease holder 身份被强校验为不同主体。

**P0-L role_worker_v1 分支的分离强化**（step2 实施）：
1. reviewer proof 改为"已由 daemon 记录在案的 reviewer **role worker** proof"：reviewer worker
   经自己的 `role_worker_auth`（credential 只经 enroll response 返回一次，DB 仅存 SHA-256，
   role_worker.rs L98-109）先行通过 `validate_and_record`（expected_role="reviewer"）留下
   无秘密 provenance；
2. adjudicator worker 经 `validate_and_record(expected_role="adjudicator")` 授权；
   **`role_worker_id` 不得等于 reviewer proof 的 worker**——`validate_and_record` 已内置同任务
   跨角色冲突检查（L326-332，`E_ROLE_WORKER_SEPARATION_VIOLATION`），P0-L 再加显式
   reviewer/adjudicator worker 不等校验；
3. **raw reviewer token/credential 永不转移给 adjudicator**：adjudicator 分支不要求、不接收
   reviewer 的 lease raw token 作为自身凭据；lease/fencing 保留为事务串行化与独立审查证据
   （`E_LEASE_FENCING_STALE` 语义不变，L6564-6569）；
4. 所有分支拒绝 provider/account/model/session token 字段（`runtime_contains_secret` 递归扫描
   token/secret/password/cookie/credential，L50-60），错误与日志不落 raw credential。

### Q4：older 显式 `legacy_identity_v1` 行为如何保持？pre-P0-L missing policy 如何处置？

**显式 `legacy_identity_v1`**：完全走现有路径，逐字节保持——`parse_action_identity` →
agent_registrations 四项校验 → `check_role_independence` → contract_claim skill/prompt hash 匹配
（1.4 序列）；**不接受** `role_worker_auth` 作为该路径的任何降级或替代输入
（step3 负矩阵必须证明"legacy 路由上带 role_worker_auth 被拒绝/忽略，绝不隐式使用"）。

**pre-P0-L missing policy ≠ legacy default**：
- 历史任务（含 A″ parent/G0、P0-L 自身）的 current contract 无 policy 字段；
- 处置语义定义为 `policy_missing_legacy_migration_required`：`task.next_action` 返回
  **policy blocker**（而非 unqualified CLAIM），`task.claim` pre-write 拒绝；
- 唯一出路是**逐任务、显式、append-only** 的修订流程：经新的 role-worker-aware
  `task.contract_revise` 分支追加携带 `identity_policy` 的 revision，并在修订记录中写入
  `policy_migration_reason` 与被取代的 old contract hash（哈希链天然保留旧值，见 1.6）；
- **禁止**：在 bootstrap/claim 中静默默认 legacy；批量脚本回填历史卡；删除或改写既有
  generic revision。

### Q5：A″ parent/G0 如何在 P0-L 后逐卡追加 policy revision？为何不是批量/placeholder bootstrap？

1. **前提门禁**：P0-L 代码独立 Reviewer PASS → 独立 Adjudicator 复核 → 受控
   `refresh_shared_runtime.ps1` 部署 → live binary/source/schema/commit 收敛证据齐备（任务草案 §4 step6）。
2. **逐卡路径**：独立 Reviewer/Adjudicator 使用**各自**已登记的 role worker credential，
   对 `T-1787800241076-0a1c1824`（A″ parent）与 `T-1787800241077-e7fd7231`（A″-G0）
   **各发起一次** `task.contract_revise`：
   - `expected_previous_hash` = 该卡 current generic revision hash（observation 中分别为
     `sha256:245996d0…` / `sha256:16745cd8…`）；
   - 新 revision envelope 携带 `identity_policy=role_worker_v1` + `policy_migration_reason`；
   - `append_task_contract_revision` 强制 `revision=current+1`、`supersedes_contract_hash=current hash`，
     INSERT-only（L142-148）——generic revision 1 原样保留，历史不可改写。
3. **为什么不是批量/placeholder**：
   - `task.contract_bootstrap` 对已有投影的任务直接 `E_TASK_CONTRACT_BOOTSTRAP_NOT_EMPTY`
     （no_governance_projection，bootstrap.rs L128-140），A″ 两卡已有 rev1，**只能走 revise**；
   - 每次 revise 都是独立请求、独立授权证据（各自 worker provenance + lease/fencing）、
     独立哈希链锚点，无法合并为一次批量写；
   - A″-01…A″-37 不存在（observation `no_implementation_microtasks_created=true`），
     P0-L 明确禁止批量预建/批量回填；后续每张卡创建时即带显式 policy（Q2），不再需要迁移修订。

---

## 3. P0-K Boundary（只回归、不触碰）

| 事实 | 锚点 |
|---|---|
| `role_worker.rs` 域完整：enroll（CSPRNG 32B 一次性 credential，仅存 hash）/revoke/status/`validate_and_record` | role_worker.rs L111-340 |
| 错误码常量齐备（E_ROLE_WORKER_AUTH_REQUIRED 等 8 个） | role_worker.rs L15-22 |
| **生产调用方为零**：`parse_role_worker_auth` / `validate_and_record` 仅被本模块测试与 `role_worker_test.rs` 调用（全源码 grep 证实） | 见 1.1 备注 |
| `verdict.submit` 仍为 legacy reviewer identity + reviewer lease 门禁 | task_collab.rs L4414+，L4500-4510 |
| P0-L 对 verdict.submit/task.apply/task.close 只做回归测试，不改主逻辑 | 任务草案 §3 冻结边界 |

**新发现（schema 缺口）**：`role_workers` / `role_worker_instances` / `role_runtime_provenance`
三张表的 DDL **不存在于任何 canonical schema 源**：
- Rust canonical 迁移 `migrate_connection`（sqlite_query.rs L818-891）经 `include_str!`
  嵌入 `db/schema.py` 的 `SCHEMA_SQL`（sqlite_query.rs L18-19）+ 固定 rule seed，无 role worker 表；
- `db/schema.py`（SCHEMA_VERSION=60，L2099）grep 无 `role_worker` 任何字样；
- `db/db_base.py` / 全 Python 层同样没有；
- **运行时探针**（`.tmp_p0l_schema_probe.py`，对全新临时库执行已部署
  `callwarden_core.sqlite_migrate_schema`）：`migrated_version=60`，role_* 表仅
  `role_contract_lineages / role_contract_revisions / role_contracts`，
  `role_workers: MISSING`，`role_worker_instances: MISSING`，`role_runtime_provenance: MISSING`。

**含义**：P0-K 的 role_worker 测试（断言 `migrate_connection==60` 后直接 INSERT role_workers）
在全新库上无法通过；role worker 域目前是"逻辑先行、schema 未落地"。
**P0-L step1 必须包含最小 v61 schema 支持**：在 `db/schema.py` SCHEMA_SQL 增补三表
（幂等 `CREATE TABLE IF NOT EXISTS`）+ `RUST_SCHEMA_VERSION` 镜像 + `db/db_base.py`
Python compatibility parity（仅 schema 兼容，不写授权逻辑）——这落在本任务 allowed_paths 内
（`db/schema.py`、`db/db_base.py`、`rust_ext/src/sqlite_query.rs`）。

## 4. Legacy Compatibility 摘要

| 场景 | P0-L 后行为 |
|---|---|
| 显式 `legacy_identity_v1` 新卡 | create/next_action/claim 全链路保持现状；拒绝 role_worker_auth 输入 |
| 无 role_contracts 的普通任务 | 不受影响（无合同即无 policy 门禁，沿用 `task_has_contracts` 分支） |
| pre-P0-L 有合同无 policy 的历史卡 | `policy_missing_legacy_migration_required`：next_action blocker、claim 拒绝；仅逐卡 append-only 修订可解 |
| lease / fencing | 语义完全保留（时序/并发/独立审查证明），不以 runtime session 替代 |

## 5. Worker / Lease Separation 摘要

- 授权锚点：`role_worker_id` + `role_instance_id` + daemon 签发的一次性 credential（仅存哈希）。
- 非授权事实（仅 append-only 审计）：provider / account / model / agent / session；
  runtime payload 禁 secret（递归扫描，`E_RUNTIME_PROVENANCE_SECRET_FORBIDDEN`）。
- 同任务跨角色：`role_runtime_provenance` 冲突查询（role_worker.rs L326-332）+ 显式
  reviewer/adjudicator worker 不等 → 双重阻断同一 worker 身兼治理角色。
- lease 不消失：reviewer lease/fencing 继续作为"独立审查已发生"的时序证据；
  adjudicator 永远不以持有 reviewer raw token 作为自己的授权。

## 6. A″ / G0 Impact 摘要

- 现状（observation 实证）：generic rev1 contract + `blocking_conditions=[]` →
  文本宣称的"先 bootstrap 再 claim"无 daemon 强制力。
- P0-L 后：policy_missing → next_action blocker + claim fail-closed，A″ parent/G0 回到
  **visible-but-not-claimable** 的正确状态；独立三角色按 Q5 逐卡修订后方可领取。
- G0/parent 的既有失败回执与 observation 证据保留原样（append-only），不删除不覆盖。

## 7. 本任务受限自举例外（引用记录）

P0-L 自身即在"修复对象尚未存在"的 bootstrap 悖论下创建：任务描述已记录用户直接授权的
一次性例外（generic projection、六步 + 三份固定 prompt-hash 合同、executor 不得
apply/close/部署、独立 reviewer/adjudicator 不得借用 executor worker、P0-L 收尾前须经
新 policy-aware 流程追加自身 `role_worker_v1` revision）。本例外仅限本任务，不得外推。

## 8. 勘察证据索引

| 证据 | 位置 |
|---|---|
| create/claim/bootstrap/revise/lease 源码锚点 | `rust_ext/src/daemon/task_collab.rs` L117-152、L724、L827-944、L1526-1758、L2430-2673、L6530-6603 |
| next_action 只读 evaluator | `rust_ext/src/daemon/task_loop/next_action.rs` L4、L64-65、L629、L912-1138 |
| bootstrap/revise 域 | `rust_ext/src/daemon/task_loop/task_contract_bootstrap.rs` L128-216；`task_contract_revise.rs` L87-160 |
| role worker 域 | `rust_ext/src/daemon/task_loop/role_worker.rs` L15-340；`role_worker_test.rs` L188-193 |
| canonical 迁移 | `rust_ext/src/sqlite_query.rs` L16-32、L818-891；`db/schema.py` L2099 |
| schema 缺口运行时探针 | `.tmp_p0l_schema_probe.py`（fresh migrate → 三表 MISSING） |
| A″ 实测 observation | `deliverables/software-company/aprime2_contract_dispatch_observation.json`、`aprime2_role_worker_contract_bootstrap_blocked_20260827.md` |
| 任务草案 | `deliverables/software-company/p0l_role_worker_task_contract_claim_enforcement_task_draft_20260827.md` |
