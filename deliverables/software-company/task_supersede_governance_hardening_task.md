# task.supersede governance hardening / promotion 计划

## P0-H：将既有 task.supersede 基础实现提升为 A′ 三角色治理权威能力

**与已关闭基础任务的边界。** 本任务以 `T-1787203926824-9f873bfc-sub-1` 已实现的 relation/event、基础 CLI、存在性检查和全图环检测为输入，**不**重开、不删改、不重复实现其已经覆盖的公开名词。新增内容仅限该基础能力缺失的 authority、durability、provenance 与 promotion 门禁。实现完成前，任何 Agent 均不得用 `cw task supersede` 收口 S2-original/S2-rebuilt。

**目标。** 使 `task.supersede` 成为 daemon-native、workspace-bound、append-only、durably idempotent、Adjudicator-only 的治理 mutation。成功后，A′ Phase 0 才可将两个陈旧 S2 指向已存在的 A′ 恢复父任务；这个 promotion 不等于自动执行 supersede。

**行为契约。** 保持现有 CLI 位置 `cw task supersede <old> <new>` 和只读 `cw task superseded <id>`；新增 `--request-id`、`--evidence-path`、`--evidence-hash`、`--lease-token`、`--fencing-counter`，并将 `--role` 严格限定为 `adjudicator`。所有 mutation 参数经 `route_task_write("task.supersede", ...)`；daemon 不可用、HTTP authority manifest 缺失、identity/lease/provenance 缺失时 fail-closed，绝不 fallback 至 local SQLite。

**数据与 schema v59。** 将 `task_supersede_relations` / `task_supersede_events` 从 `task_supersede.rs::SUPERSEDE_SCHEMA_SQL` 的启动期临时 DDL 纳入 checksummed `db/schema.py`；版本从 v58 升至 v59，并在 `db/db_base.py` 补无损迁移与必备列/索引校验。最终关系/事件必须保存 workspace_id 或 workspace_instance_id、supersedence_id、predecessor、successor、reason_code、reason、actor agent/session/model/role、request_id、lease_id/fencing_counter、authoritative timestamp、evidence path/hash。relation table 对同 workspace 的 predecessor 设置唯一 outgoing edge；允许多个 predecessor 指向同一 successor；禁止 self edge。不得 UPDATE/DELETE 历史 relation/event，亦不得改动旧任务 status、description、applied_at、closed_at、step、evidence 或 verdict。

**一致性与 authority。** 在 `task_loop/operation_store.rs::TASK_DB_LEDGER_METHODS` 加入 `task.supersede`，在 `TaskCollabStore::handle_task_supersede` 中使用 `OperationStore::dedupe` 与 `record_result`，使 relation event、权威 `task_events` 审计行和 ledger result 同一 SQLite transaction 成功提交；同 request_id/同 canonical 参数只重放结果，不再追加任何行；同 key/异参数返回 `E_REQUEST_ID_REUSE_MISMATCH`；确定性拒绝只写可重放 ledger error，不写 relation/task_event。不得继续使用 `TaskCollabStore::check_dedup/save_dedup` 作为该方法唯一幂等机制。

写入前必须验证：predecessor/successor 都存在、归属同一 workspace authority binding、successor 已存在、请求 peer 与完整 registered identity 一致、role=adjudicator、source task 对应 reviewer lease 有效、fencing counter 为当前值、不存在 outgoing relation、无间接环、证据 manifest/hash 完整。所有失败必须返回稳定 code，至少含 `E_SUPERSEDE_TASK_NOT_FOUND`、`E_SUPERSEDE_CROSS_WORKSPACE`、`E_SUPERSEDE_SELF_REFERENCE`、`E_SUPERSEDE_ALREADY_EXISTS`、`E_SUPERSEDE_CYCLE`、`E_SUPERSEDE_IDENTITY_REQUIRED`、`E_SUPERSEDE_ROLE_REQUIRED`、`E_SUPERSEDE_LEASE_REQUIRED`、`E_SUPERSEDE_FENCED`、`E_SUPERSEDE_EVIDENCE_REQUIRED`。

**精确文件与函数白名单。**

- `db/schema.py`：v59 schema、`task_supersede_relations`、`task_supersede_events`、workspace/provenance 列、单 outgoing unique index、task_events reason-code 合同。
- `db/db_base.py`：v58→v59 无损 migration、历史数据库列/索引校验；不得以 runtime DDL 掩盖迁移。
- `rust_ext/src/daemon/task_supersede.rs`：移除 `ensure_supersede_schema` 作为权威 schema 来源；扩展 `TaskCollabStore::handle_task_supersede` 为 authority/lease/fencing/ledger/atomic event 实现；扩展 `handle_task_superseded_by` 为 workspace-scoped projection。
- `rust_ext/src/daemon/task_loop/operation_store.rs`：扩展 `TASK_DB_LEDGER_METHODS`、method scope regression、同 request replay、mismatch 和 durable error tests。
- `rust_ext/src/daemon/task_collab.rs`：仅补 task_events append helper、identity/assignment/lease/fencing 验证复用点；不得重写 report/handoff/verdict 逻辑。
- `rust_ext/src/daemon/dispatch.rs`：保留/加固 `DaemonState::handle_task_supersede`、`task.superseded_by` 及 capability promotion gate；确保所有 HTTP/pipe 请求进入现有 serial writer。
- `rust_ext/src/daemon/http_server.rs`：更新 task.supersede capability，不宣传为 enabled，直到 promotion verifier PASS。
- `cli/main.py`：`supersede` parser 完整凭证、严格 role、daemon-only adapter、结构化错误/JSON 输出；`superseded` read projection 输出 workspace/provenance。
- `server/daemon_client.py`：`task.supersede` write policy 与 `task.superseded_by` read policy，不允许 local fallback。
- `tests/test_task_supersede.py`（扩展或拆为 schema/daemon cases）、`tests/test_task_supersede_cli.py`（新建）、`tests/test_task_supersede_daemon.py`（新建）：覆盖迁移、重启重放、并发唯一、workspace/identity/role/lease/fencing/evidence 拒绝、环、历史字段不变、CLI→HTTP→Rust round-trip、daemon unavailable fail-closed、show/list projection。

**验收门禁。** 运行相关 Python 与 Rust 测试，并提供实际 runtime 的 daemon endpoint/manifest/capability evidence。独立 Reviewer 只能 PASS/BLOCKED；PASS 后，不同 session 的 Adjudicator 在 reviewer lease 规则下 apply/close。只有 Adjudicator accepted 后，`task.supersede` 才可列入 A′ Phase 0 allowed capability。

**明确排除。** 不执行任何 S2 supersede；不创建 A′ 恢复父任务、角色 prompt、CLI-01 或 MCP 卡；不改变旧 S1/S2/S3 状态；不改 Task Envelope、一般 lease 语义或 retirement 计划；不重做基础任务已有的公开命令、基础环检测或关系查询。

- migrate-authority-schema-v58-to-v59 @ db/schema.py
- preserve-existing-databases-and-verify-v59-columns-indexes @ db/db_base.py
- add-durable-supersede-method-to-operation-ledger @ rust_ext/src/daemon/task_loop/operation_store.rs
- bind-supersede-to-authority-identity-lease-fencing-and-task-events @ rust_ext/src/daemon/task_supersede.rs
- reuse-existing-task-identity-and-lease-validation-without-changing-other-mutations @ rust_ext/src/daemon/task_collab.rs
- promote-rpc-through-serial-dispatch-and-capability-gate @ rust_ext/src/daemon/dispatch.rs
- expose-authority-gated-http-capability @ rust_ext/src/daemon/http_server.rs
- add-cli-credentials-daemon-only-route-and-read-projection @ cli/main.py
- enforce-python-client-fail-closed-routing @ server/daemon_client.py
- prove-durable-governance-and-cli-http-roundtrip @ tests/test_task_supersede_daemon.py
- prove-cli-validation-migration-and-no-fallback @ tests/test_task_supersede_cli.py

Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立核验本任务与已关闭基础 task.supersede 实现没有 scope 重叠，且所有 durability、workspace、adjudicator lease/fencing、schema v59 与 promotion 门禁均可测试。
  reason: 只有升级后的 capability 才可作为 A′ Phase 0 对两个旧 S2 进行 append-only 收口的治理依据。
  independence_requirement: required
