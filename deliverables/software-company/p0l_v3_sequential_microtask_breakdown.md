# P0-L v3：Role Worker 身份策略 fail-closed 修复与串行微任务拆分

**文档状态：** 设计蓝图 / 任务拆分基线  
**版本：** v3  ���**归属主任务：** `T-1787801315246-e3e3a08c`  
**上级 Epic：** `T-1787203926824-9f873bfc`  
**作者：** Manus AI  ���**落盘日期：** 2026-08-29  ���**实施状态：** 尚未创建 P0-L.1～P0-L.11；本文不等同于代码实现通过或任务已入库。

> 本文是已评审通过的 P0-L v3 **替代蓝图**。它不是对当前 P0-L 实现的 PASS，也不授权绕过 Reviewer、Adjudicator、lease/fencing、workspace binding 或 daemon authority。

## 1. 目标与问题定义

P0-L 的目标是在 `task.create → contract policy/revision → lease.acquire → contract bootstrap/revise → task.next_action → task.claim` 全链路建立不可绕过的 Role Worker 身份策略。稳定的 CallWarden 本地 Role Worker 与其受保护的本地 credential 是角色授权锚点；provider account、provider token、model、agent、runtime session 等字段只能作为可变的追加式 provenance，不能取代稳定角色授权，也不能因为账号切换而使合法角色失效。

本修复解决三类问题。第一，generic contract 或旧 revision 不能在缺少 `identity_policy` 时意外进入可领取状态。第二，legacy lease、raw reviewer token 和 runtime role string 不能升级为 `role_worker_v1` 权限。第三，Python CLI/MCP、Rust daemon 的两个 lease 实现以及 bootstrap/revise/claim/next_action 之间不能各自维护互相矛盾的身份语义。

最终行为必须满足以下安全不变量：

| 不变量 | 要求 |
|---|---|
| 角色授权锚点 | 由 daemon 服务端验证稳定 Role Worker、本地 role-session、worker-instance/session 绑定；角色字符串本身不得赋权。 |
| 角色分离 | 同一 task 的 executor、reviewer、adjudicator 必须满足任务级 separation；provider、model、session 的变化仅记录 provenance，不自动构成冒充。 |
| 客户端边界 | Python 仅是 HTTP/MCP/CLI thin client；不直接访问 SQLite、CAS 或执行业务授权。 |
| 秘密处理 | raw credential、raw lease token、provider token、cookie、密码不得进入 CLI 参数、shell history、HTTP 日志、DB、evidence 或聊天。 |
| 追加式历史 | 不修改、删除或重写 P0-J/P0-J-D/P0-K 及既有 P0-L 证据；所有 verdict、repair、revision、lease 和 audit 事件 append-only。 |
| fail-closed | policy 缺失、损坏、未知、workspace 不匹配、session 过期、holder 不一致、fencing 过期时，必须拒绝而不能隐式降级。 |

## 2. 合法实施顺序

### 2.1 现有 P0-L 的前置修复顺序

当前主任务 `T-1787801315246-e3e3a08c` 已有历史 revision，不能把它伪装成“无现代 projection”再走普通 `task.contract_bootstrap`。合法顺序如下：

```text
独立 Reviewer 以 opaque local role-session handle 提交 reviewer_blocked
→ daemon 在同一 P0-L 主任务内、同一事务追加唯一 P0-L.0 fix_defect step
→ Executor 领取并完成 P0-L.0（只做 v3 anchor/planning，不做 11 张卡的生产实现）
→ 独立 Reviewer PASS
→ Adjudicator 独立复审并 apply/close P0-L.0
→ Planner 才能按顺序创建 P0-L.1
→ 每一张后续卡完成、独立 review PASS、adjudicator apply/close 后，才创建下一张
```

P0-L.0 **不是 child task**，而是由正式 `reviewer_blocked` 触发的同一主任务 remediation step。P0-L.1～P0-L.11 才是 P0-L 主任务的直接子任务；不得挂在 P0-L.0 下面，也不得直接挂到 Epic 下。

### 2.2 创建规则

不得预先批量创建 P0-L.1～P0-L.11。每次只允许在前一张卡已经 `closed`、对应 evidence/verdict/lease/fencing 记录完整、运行时版本已受控发布并核验后创建下一张。这样可以避免 `task.create` 本身仍有 policy 缺口时，把一整棵不可领取或可绕过的任务树写入 authority。

所有后续卡必须具有显式 Role Contract 和 `identity_policy=role_worker_v1`。任务标题、role 字符串、CLI 参数或客户端自报字段都不能代替 daemon 的 policy revision、worker mapping、workspace binding 和 lease proof。

## 3. 总体依赖图

```mermaid
flowchart LR
  A[已有 P0-L reviewer_blocked] --> B[P0-L.0 同任务 fix_defect anchor]
  B --> C[P0-L.1 v61 holder/session schema]
  C --> D[P0-L.2 migration parity]
  D --> E[P0-L.3 shared policy resolver/exception domain]
  E --> F[P0-L.4 role-worker session lifecycle]
  F --> G[P0-L.5 worker reviewer lease acquire]
  G --> H[P0-L.6 legacy/worker proof validators]
  H --> I[P0-L.7 exact P0-L policy repair]
  I --> J[P0-L.8 bootstrap/revise routing]
  J --> K[P0-L.9 next_action/claim projection]
  K --> L[P0-L.10 opaque CLI/MCP handle]
  L --> M[P0-L.11 cross-layer release gate]
```

| 顺序 | 卡片 | 直接前置 | 主要产物 | 不得提前做的事项 |
|---:|---|---|---|---|
| 0 | P0-L.0 | 正式 reviewer_blocked | v3 anchor、repair exception 定义、证据/发布地图 | 不实现 schema、handler 或 CLI。 |
| 1 | P0-L.1 | P0-L.0 closed | v61 schema DDL、CHECK、FK/index、Rust/Python schema 草案 | 不改变 lease 授权语义。 |
| 2 | P0-L.2 | .1 closed | v60→v61 forward/backfill/idempotence/failure parity | 不把 migration 与业务授权混合。 |
| 3 | P0-L.3 | .2 closed | 共享 policy resolver、exception domain、use ledger | 不直接开放 exception RPC。 |
| 4 | P0-L.4 | .3 closed | session create/heartbeat/revoke/retire/gc lifecycle | 不让 stale recovery 只依赖 worker status。 |
| 5 | P0-L.5 | .4 closed | worker-first reviewer lease acquire | 不复用 legacy `agent_id` 表示 worker。 |
| 6 | P0-L.6 | .5 closed | 分离 legacy/worker reviewer-proof validators | worker proof 必须拒绝 legacy holder。 |
| 7 | P0-L.7 | .6 closed | P0-L 精确一次 policy repair n+1 revision | 不把 P0-L 当作普通 bootstrap。 |
| 8 | P0-L.8 | .7 closed | bootstrap/revise 互斥路由 | 不允许 raw token 或 policy 降级。 |
| 9 | P0-L.9 | .8 closed | next_action/claim 一致 projection | 不让 claim 与 next_action 分叉。 |
| 10 | P0-L.10 | .9 closed | CLI/MCP opaque handle interface | 不提前修改 CLI/MCP surface。 |
| 11 | P0-L.11 | .10 closed | 跨层 release/security gate | 不以“归因给旧失败”替代全套绿灯。 |

## 4. 所有卡片的共同完成条件

每一张 P0-L 子任务都必须同时满足以下条件，缺一不可：

1. 前置卡已经由 Adjudicator 正式 `apply/close`，而不是只有聊天中的“完成”或文本 Handoff。
2. 生产代码、测试、evidence 和运行时版本均与当前 task/workspace binding 对齐；不得把隔离 worktree 未部署状态冒充 live 完成。
3. 测试覆盖成功、拒绝、重放、并发、stale/fencing、migration forward、idempotence 和 failure rollback（适用者必须全部覆盖）。
4. legacy parity 测试通过，且 legacy 路径不能升级 worker 权限或绕过现代 policy。
5. no-secret scan 通过：不得出现 raw worker credential、raw lease token、provider token、密码、cookie 或其可逆包装进入日志、DB、evidence、CLI 参数或测试 fixture。
6. 独立 Reviewer 使用独立 Role Worker、独立 instance/session 和正确 lease/fencing 完成复审并持久化 `reviewer_pass` 或 `reviewer_blocked`。
7. Adjudicator 独立复审 Reviewer 结果；只有 Adjudicator 才能进行受权限约束的 `apply/close`。
8. 受控 deployment 仅通过项目规定的 runtime refresh 入口，部署前动态读取 manifest/PID/health，部署后核验 live source revision、schema、health、smoke 和 rollback 证据。
9. 不允许以“全量测试失败是并行会话旧问题”为 release 替代；必须真正运行规定的完整 gate，所有 release blocking failure 均需修复。

## 5. P0-L.0：受限 bootstrap anchor / fix_defect step

**性质与所有权。** P0-L.0 不是独立 child task，而是正式 `reviewer_blocked` 在 `T-1787801315246-e3e3a08c` 内追加的唯一 `fix_defect` step。它必须具有 source verdict、reason、evidence path/hash、request id、exception marker、workspace binding 和 append-only audit。

**目标。** P0-L.0 只冻结 v3 状态机和实施地图，不编写 v61 schema、lease handler、policy resolver、CLI/MCP surface 或后续卡的生产实现。它应明确：P0-L 精确 task-id exception 的签发来源、使用次数、hash binding、审计字段、common lease domain 所有权、任务状态迁移、发布顺序和回滚策略。

**验收。** 测试必须证明正常客户端无法创建该 step；普通 `task.create`、`contract-revise`、rollback 和客户端自报参数无法触发 exception；同一 request id 重放不会产生第二个 verdict 或第二个 step；不同 task、不同 workspace、不同 revision 和不同 exception marker 全部拒绝。

**完成后。** 独立 Reviewer PASS、Adjudicator apply/close 后才允许 Planner 创建 P0-L.1。

## 6. P0-L.1：v61 显式 lease-holder schema

**目标。** 在 canonical schema 和 Rust schema migration 中建立清晰的 worker holder 表达，不能继续复用 `task_leases.agent_id` 代表 Role Worker。

**主要文件/范围。** 重点包括 `db/schema.py`、`rust_ext/src/sqlite_query.rs` 及对应 schema/migration tests。只处理 DDL、约束、索引和兼容性定义；业务授权与 lease handler 留给后续卡。

**必须新增或明确的字段。** `task_leases` 至少包含 `holder_kind`、`role_worker_id`、`role_instance_id`、`role_session_id` 和 `holder_binding_version`。应建立 `role_worker_sessions`，保存 worker、instance、session、workspace/binding、状态、创建/心跳/撤销/退休时间等非秘密 metadata；不得保存 raw credential。

**约束要求。** worker holder 字段必须 all-or-none；legacy holder 必须 all-empty；`holder_kind` 与字段组合必须有 CHECK；active worker holder 必须有唯一索引；active worker session 的 worker/instance/session 组合必须具备唯一性，并通过 FK 或 daemon 逻辑校验绑定关系。约束必须拒绝混合 holder、空 holder、跨 workspace holder 和重复 active session。

**验收。** DDL migration test、CHECK violation test、active uniqueness test、FK/逻辑 binding test、旧 schema read compatibility test、以及 schema introspection test 全部通过。不得在此卡引入 raw token 新字段。

## 7. P0-L.2：v60 → v61 migration parity

**目标。** 对 Rust migration 与 Python compatibility migration 建立相同的 v60→v61 行为；Python 仅负责 migration compatibility，不加入授权或业务决策。

**主要文件/范围。** `rust_ext/src/sqlite_query.rs`、`db/db_base.py` 及 migration fixtures/tests。需要处理 forward migration、既有 legacy lease backfill、CHECK/index 建立、`role_worker_sessions` 初始状态与异常中止。

**backfill 规则。** 既有 lease 只能按真实可验证的历史 holder 语义填充；不能把 `agent_id` 字符串猜测成 `role_worker_id`，不能伪造 instance/session。无法安全 backfill 的记录必须进入明确的 legacy/unknown 状态并由后续 policy resolver fail-closed，而不是静默升级。

**验收。** fresh v61、v60 forward、重复执行 idempotence、部分失败恢复、旧数据 backfill、unknown legacy、并发 migration lock、Rust/Python parity 和 rollback safety 测试全部通过。migration 不能删除旧 append-only 记录或重写历史 evidence。

## 8. P0-L.3：共享 policy resolver、exception domain 与 use ledger

**目标。** 建立唯一的 policy resolution domain，消除 `task_collab.rs` 与其他路径各自判断 policy 的分叉。建议将共享逻辑放入 `rust_ext/src/daemon/task_loop/policy_resolver.rs`、`policy_exception.rs` 或项目实际等价模块。

**解析状态。** 至少区分 `resolved_role_worker_v1`、`resolved_legacy_identity_v1`、`unresolved`、`unknown`、`invalid`。缺失、损坏、未知和不匹配必须是机器可观察的 blocking state，不能隐式按 legacy 继续。

**exception domain。** exception 必须包含精确 `task_id`、exception kind、发行来源、workspace/task binding、预期 revision、cutoff/历史条件（如适用）、max uses、当前 use ledger、创建和消费 request id、source verdict/evidence hash。P0-L exception 只能由受控 migration/break-glass authority 签发，不能由普通客户端字段自行产生。

**验收。** resolver matrix、policy revision hash chain、exact task match、wrong workspace/revision、expired/consumed exception、double-spend、request replay、unknown policy fail-closed 和 audit ledger append-only 测试全部通过。

## 9. P0-L.4：Role Worker session 生命周期

**目标。** 实现完整的 `role_worker_sessions` authoritative lifecycle：`session.create`、`session.heartbeat`、`session.revoke`、`session.retire/gc`。session 必须绑定 stable worker、instance、session、workspace/task binding 和生命周期状态。

**主要文件/范围。** `rust_ext/src/daemon/task_loop/role_worker.rs` 及新的 session domain/RPC；必要的 dispatch registration、schema tests 和 runtime metadata tests。session store 的本地 credential 仍由 ACL 目录保护；daemon 不应把 credential 内容写入 DB 或日志。

**状态要求。** 至少定义 active、stale、revoked、retired 等状态及状态转移。heartbeat 必须受 worker/session 身份约束；revoke 必须 append audit；GC 只能回收已满足 stale/retired 条件的 metadata，不得回收仍被 active lease 引用的 session。stale recovery 不能只检查 `role_workers.status=active`。

**验收。** create/heartbeat/revoke/retire/GC 生命周期、重复请求幂等、跨 worker/instance/session 拒绝、workspace mismatch、stale recovery、active lease protection、revoke 后 lease 行为、并发 heartbeat/revoke 和 no-secret scan 全部通过。

## 10. P0-L.5：worker-first reviewer `lease.acquire`

**目标。** 让 reviewer lease 以显式 worker holder 获取；legacy `agent_id/session_id/model_id` 只能表示旧 holder/provenance，不能冒充 worker holder。

**主要文件/范围。** 统一后的 `lease_holder.rs`、`lease_policy.rs`、`task_collab.rs` lease handler、`task_loop/lifecycle_lease.rs` adapter 以及对应 RPC/tests。此卡开始前不得保留两套互相独立的 lease 语义。

**授权要求。** daemon 从 opaque local role-session handle 解引用并验证 stable reviewer worker、worker-instance、role-session、workspace binding、task role separation 和当前 policy；lease holder 字段必须写入 v61 显式列。raw lease token 不能作为 reviewer identity，也不得进入日志/evidence/chat。

**并发与 fencing。** acquire/renew/release/recovery 必须共享同一 fencing domain；active uniqueness、expired lease、stale session、renew race、旧 fencing counter 和同 task executor/reviewer/adjudicator separation 必须服务端判定。

**验收。** worker acquire success、legacy holder cannot upgrade、wrong role、wrong worker/session/instance、workspace mismatch、stale recovery、expired lease、fencing race、duplicate acquire、renew/release idempotence 和 concurrency tests 全部通过。

## 11. P0-L.6：legacy/worker reviewer-proof validators

**目标。** 明确分离 legacy proof 与 worker proof validator，禁止一个宽松 validator 同时接受两类 holder。

**主要文件/范围。** reviewer proof validation domain、`task.contract_revise`、verdict/evidence gate、reviewer lease proof consumers 及其 tests。应新增清晰的 proof kind/holder kind 判定，不把 raw reviewer token 交给 adjudicator 作为证明。

**验证规则。** worker proof 必须引用服务端保存的 reviewer lease/session/fencing proof，并拒绝 legacy holder；legacy proof 只有在历史 cutoff、历史治理记录和有效 daemon exception marker 同时存在时才允许。role string/runtime identity alone 不得通过。

**验收。** worker proof success、legacy success only under exception、worker-with-legacy-holder reject、raw token reject、wrong fencing reject、wrong task/workspace/revision reject、same-task role separation、proof replay/idempotence 和 audit provenance tests 全部通过。

## 12. P0-L.7：P0-L 精确 `contract_policy_repair`

**目标。** 对当前已有 revision 但缺失/损坏 `identity_policy` 的 P0-L，追加一次精确的 n+1 hash-linked policy repair revision。不得伪装为无 projection 后走 bootstrap。

**主要文件/范围。** contract policy revision domain、`task.contract_revise`/policy repair internal path、revision hash-chain tests 和 P0-L exception ledger。只允许 target task `T-1787801315246-e3e3a08c`、精确当前 revision、精确 workspace binding、精确 exception kind。

**修复内容。** 新 revision 必须写入 `identity_policy=role_worker_v1`、适用角色、worker/session proof requirements、同 task separation、lease/fencing requirements、source exception id/use、previous revision hash 和新 revision hash。不得修改旧 revision，不得伪造历史“从未存在 policy”。

**一次性与幂等。** exception max uses=1；同一 request id 重放返回同一结果或明确已消费错误，不产生第二个 revision；不同 request、不同 task、不同 workspace、不同旧 revision 和不同 evidence hash 均拒绝。

**验收。** n+1 hash chain、exact-match、one-use、replay、concurrent double-spend、wrong policy、wrong lease/proof、legacy bypass rejection 和 append-only audit tests 全部通过。

## 13. P0-L.8：bootstrap/revise policy routing

**目标。** 固化 bootstrap 与 policy repair 的互斥状态机，并让所有 contract mutation 使用同一个 policy resolver。

**路由状态机。**

```text
无现代 projection
    → 合法 task.contract_bootstrap
已有 revision 且 policy 缺失/损坏
    → 仅合法 contract_policy_repair
已有有效 policy revision
    → 仅合法 contract.revise（按 policy 和 lease/proof）
未知/不一致/跨 binding
    → BLOCKED，不降级
```

**主要文件/范围。** `task_collab.rs` 的 bootstrap/revise handlers、policy resolver、worker proof validator、contract revision tests、dispatch route tests。

**客户端边界。** 客户端不能通过参数选择 bootstrap/repair、注入 exception、传 raw credential、传 raw lease token 或选择任意 historical legacy 模式。daemon 必须从数据库和 server-side exception ledger 决定路由。

**验收。** empty projection bootstrap、existing missing-policy repair、valid-policy revise、bootstrap-vs-repair mutual exclusion、raw-input rejection、wrong binding/revision、unknown policy BLOCKED、concurrent route race 和 idempotence tests 全部通过。

## 14. P0-L.9：`task.next_action` / `task.claim` 一致治理 projection

**目标。** 让只读 next_action 与实际 claim 使用同一 policy resolution 和 requirements model，消除“next_action 可领取但 claim 才失败”或反向分叉。

**主要文件/范围。** `rust_ext/src/daemon/dispatch.rs`、`task_collab.rs` claim handler、policy resolver、claim requirements projection 和 route tests。

**机器状态。** 对 unresolved、unknown、invalid、missing policy、missing worker session、wrong holder、stale lease、missing fencing 等情况，next_action 必须返回 `BLOCKED` 或等价机器状态，并带 `resolve_identity_policy`、`adjudicator`、blocking arrays 和明确 claim requirements；不能返回可领取 action。

**claim 要求。** claim 必须重新在服务端原子校验，而不是信任 next_action 快照；worker-first branch 从 DB/session mapping 得到角色，runtime identity 仅作 provenance。不同角色、不同 task binding、旧 fencing 和 stale session 必须拒绝。

**验收。** next_action/claim positive matrix、missing/unknown policy、worker session expired、legacy mismatch、same-task separation、TOCTOU race、requirements 与实际错误一致、重复 claim/fencing 和 read-only projection tests 全部通过。

## 15. P0-L.10：CLI/MCP opaque local role-session handle

**目标。** 将 CLI/MCP 接口收敛为 opaque local role-session handle；客户端不能接收或传递 `--role-worker-auth`、raw credential 或 raw lease token。

**主要文件/范围。** `server/daemon_client.py`、`cli/main.py`、MCP tool wrappers、role-session local loader tests。Python 仍只做 HTTP transport、参数投影和结果展示，不实现授权、不直接访问 SQLite/CAS。

**接口要求。** CLI/MCP 只接受不可逆 opaque handle；daemon 或受保护本地 loader 根据当前用户 ACL 读取对应 session metadata/credential，仅在内存中完成服务端验证。credential 内容不得写入 shell history、命令回显、日志、JSON evidence 或聊天。lease token 若历史协议仍存在，只能由受保护 server-side mechanism 管理，不得成为 CLI/MCP 的身份参数。

**兼容要求。** 对旧 raw 参数必须 fail-closed 并返回迁移提示，而不是静默接受；正常用户不需要该能力时不得强制配置 role-session；账号切换产生的新 runtime/session provenance 不得被误判成冒充，只要稳定 worker authorization 和任务角色 policy 仍有效。

**验收。** opaque handle success、missing/invalid handle、ACL denial、raw credential rejection、raw lease token rejection、shell/log redaction、MCP/CLI HTTP-only route、no SQLite import/open、provider-token scan、账号/session 切换 provenance 和 backward-compatibility rejection tests 全部通过。

## 16. P0-L.11：跨层 migration/security regression gate

**目标。** 对 Rust daemon、schema migration、Role Worker session、lease/fencing、contract policy、next_action/claim、Python thin client、CLI/MCP 和部署 provenance 做最终发布门禁。

**范围。** 全部 P0-L 相关 Rust/Python 测试、跨层 HTTP round-trip、migration matrix、concurrency/fencing、no-secret scan、dependency/source scan、CLI/MCP route scan、live smoke 和 rollback evidence。不得把既有全量失败简单标为“并行会话引入”而放行；必须有可复现的基线、修复和重新通过记录。

**最低 release matrix。**

| 类别 | 最低要求 |
|---|---|
| Migration | v60→v61 forward、repeat/idempotence、legacy backfill、failure recovery、Rust/Python parity 全绿。 |
| Identity | worker-first、legacy strict exception、unknown/missing policy、worker/session lifecycle 全绿。 |
| Lease | holder all-or-none、active uniqueness、fencing race、stale recovery、same-task separation 全绿。 |
| Contract | bootstrap/repair/revise mutual exclusion、n+1 hash chain、one-use exception、replay protection 全绿。 |
| Projection | next_action 与 claim 同一 resolution，所有 blocking 状态机器一致。 |
| Client boundary | Python/CLI/MCP 无 direct SQLite/CAS、无 raw credential/token 参数、HTTP-only 通过。 |
| Security | no-secret scan、敏感日志扫描、raw token/credential fixture scan、ACL/session 文件检查通过。 |
| Runtime | controlled refresh、manifest/PID/health、live source/schema、smoke、rollback provenance 全部可核验。 |

**完成定义。** P0-L.11 通过后，独立 Reviewer PASS、Adjudicator apply/close，且运行实例与证据 hash 一致，P0-L 才可整体关闭。P0-L 关闭前不得创建或实施 A″-01～A″-37。

## 17. 后续任务创建与 A″ 解锁规则

P0-L.1～P0-L.11 必须由 Planner 按本文顺序逐张挂载到 `T-1787801315246-e3e3a08c`。每张卡都应注明 predecessor task/step、workspace binding、`identity_policy=role_worker_v1`、精确白名单、禁止范围、测试命令、required evidence、Reviewer handoff 和 successor rule。

P0-L 完整关闭且 runtime convergence 已独立核验后，才允许对 A″ parent `T-1787800241076-0a1c1824` 和 G0 `T-1787801315246-e3e3a08c`（如其实际 ID/谱系仍如此）追加合法 `role_worker_v1` policy revision。A″-01～A″-37 仍须满足各自 predecessor、独立 review/adjudication、A′ 关闭、`python_compat=0`、旧 S3 disposition、无 direct SQLite/CAS 和 release gate；不能因为本文已落盘而预建。

## 18. 当前状态声明

本文仅完成设计文档落盘。它没有：

- 创建 P0-L.1～P0-L.11 任务；
- 提交真实 `reviewer_blocked`；
- 创建或关闭 P0-L.0；
- 修改生产代码或 schema；
- 更新 live daemon；
- 修改 P0-J、P0-J-D、P0-K 或 A″ 历史；
- 授权任何绕过门禁的导入脚本。

当前如果要继续实施，必须先动态读取最新 manifest/PID/health 和 P0-L 状态，不得信任旧报告或旧 endpoint。后续任何 daemon 写入都必须经过真实独立 Role Worker、正确 workspace binding、lease/fencing 和 append-only authority。

## References

[1]: [P0-L v3 原始替代蓝图附件](file:///home/ubuntu/upload/pasted_content_2.txt)
[2]: [P0-L v2 整改方案（历史，不是 v3 权威）](file:///C:/git_work/callwarden/.qoder/plans/P0-L_bootstrap_lease_worker-first_整改_b2867e21.md)
[3]: [P0-L step 5 review packet（历史证据，不替代当前 verdict）](file:///C:/git_work/callwarden/deliverables/software-company/p0l_step5_review_packet_20260828.md)
[4]: [P0-L v3 拆分正式文档](file:///C:/git_work/callwarden/deliverables/software-company/p0l_v3_sequential_microtask_breakdown.md)
