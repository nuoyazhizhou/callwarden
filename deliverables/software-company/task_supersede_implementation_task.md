# task.supersede 前置实现计划

## P0：实现 daemon-native `task.supersede` append-only 任务替代谱系

**目的。** 为重建、拆分或被新工作线程替代的历史任务提供可查询、可重放、不可篡改的替代关系；禁止以改描述、伪造 verdict、修改 `closed_at` 或滥用 `task.handoff` 表达 supersede。该任务只实现能力，**不执行**对 S2-original/S2-rebuilt 的实际 supersede，也不创建 A′ 恢复父任务、CLI-01 或 MCP 卡。

**调用前置。** successor 必须已经是同一 workspace 中真实存在的任务，因此 A′ 迁移的实际物理顺序应是：先创建新恢复父任务，再由独立 Adjudicator 对每个旧 S2 单独调用 `cw task supersede`。不得接受自由文本 successor ID 或尚未创建的未来任务作为关系目标。

**公开接口。** 新增 JSON-RPC `task.supersede` 与 CLI `cw task supersede <predecessor_task_id> --successor <successor_task_id> --reason-code <code> --reason <text> --request-id <uuid> --evidence-path <path> --evidence-hash <sha256> --agent-id <id> --session-id <id> --model-id <id> --role adjudicator --lease-token <token> --fencing-counter <n>`。新增只读 projection `task.supersedence.list`，并在 `cw task show` 中展示 incoming/outgoing supersedence links。Python 客户端必须将写入口经 `route_task_write` fail-closed 路由到 daemon，不得 local SQLite fallback。

**数据与不变量。** `db/schema.py` 新增 schema v59 的 append-only `task_supersedence_events` 表和索引。每行至少包含不可变 `supersedence_id`、workspace identity、predecessor task、successor task、reason_code、reason、actor identity、agent/session/model/role、request_id、reviewer lease/fencing provenance、authoritative timestamp、evidence path/hash。数据库约束必须拒绝 self-edge，并保证同一 workspace 内每个 predecessor 最多一个有效 outgoing supersedence edge；多个陈旧任务可指向同一 successor。所有成功写必须同一事务追加对应的 `task_events` 审计事实，`reason_code=task_superseded`，并保持 source task 的 `status`、`applied_at`、`closed_at`、既有 step、evidence、verdict 和 event 完全不变。

**授权与一致性。** 这是治理关系写，不是 Executor 的计划写。RPC 仅接受完整、已注册、peer-bound的 `adjudicator` identity，并要求 predecessor 的有效 reviewer lease 与当前 fencing counter；必须在同一 daemon serial writer/SQLite transaction 内完成：workspace authority、source/successor 存在性、同 workspace、角色、lease/fencing、无自环、无重复 outgoing、无间接环检查、append-only relation event、task_events 审计行、`task_operation_ledger` durable result。将 `task.supersede` 纳入 `task_loop/operation_store.rs::TASK_DB_LEDGER_METHODS`；重放必须返回原结果且不追加任何 relation/event，request_id 参数不一致返回 `E_REQUEST_ID_REUSE_MISMATCH`。

**环检测与错误码。** 使用递归 CTE 或等价事务内图遍历，在插入 predecessor→successor 前证明 successor 不能经既有 supersedence 链抵达 predecessor。至少定义并测试：`E_SUPERSEDE_TASK_NOT_FOUND`、`E_SUPERSEDE_CROSS_WORKSPACE`、`E_SUPERSEDE_SELF_REFERENCE`、`E_SUPERSEDE_ALREADY_EXISTS`、`E_SUPERSEDE_CYCLE`、`E_SUPERSEDE_ROLE_REQUIRED`、`E_SUPERSEDE_LEASE_REQUIRED`、`E_SUPERSEDE_FENCED` 和 operation-ledger 既有 fail-closed 错误。确定性拒绝必须写 durable ledger error，但不得写 supersedence/task_event 行。

**精确文件与函数白名单。**

Python / schema 范围：`db/schema.py`（新增 v59 表、索引、版本说明）；`db/db_base.py`（无损 v58→v59 迁移与 schema checksum 路径）；`cli/main.py`（task parser 中新增 `supersede`、`task.supersedence.list`/`task show` projection 与 daemon-only adapter）；`server/daemon_client.py`（`task.supersede`/`task.supersedence.list` 的 route policy 和 fail-closed read/write mapping）。

Rust 范围：`rust_ext/src/daemon/task_loop/operation_store.rs`（将 `task.supersede` 纳入 durable ledger scope，并补 unit tests）；`rust_ext/src/daemon/task_collab.rs`（新增 `TaskCollabStore::handle_task_supersede`、supersedence projection helper、同事务 validation/append/dedupe 实现）；`rust_ext/src/daemon/dispatch.rs`（新增 `DaemonState::handle_task_supersede` 和 `task.supersede`/`task.supersedence.list` 分发分支）；`rust_ext/src/daemon/http_server.rs`（capability registry 与 HTTP method exposure）。仅在现有 module 边界确实不适配时允许新建 `rust_ext/src/daemon/task_supersedence.rs`，并须在任务证据中说明为何没有放入 `task_collab.rs`。

测试范围：新增 `tests/test_task_supersede_cli.py` 与 `tests/test_task_supersede_daemon.py`；扩展 `rust_ext/src/daemon/task_loop/operation_store.rs` 内测试和与 TaskCollabStore 相邻的 Rust test module。覆盖 success、same request replay、request mismatch、self/cross-workspace/duplicate/cycle、身份/lease/fencing 拒绝、schema migration、source 状态与历史 evidence/verdict 不变、daemon unavailable no-local-fallback、CLI→HTTP→Rust round-trip 及 list/show projection。

**明确排除。** 不修改旧 S1、S2、S3 的状态或描述；不执行 supersede；不创建 A′ 父任务/CLI-01/MCP-001；不改 Task Envelope、lease 模型、verdict 模型或通用三角色协议；不做 PyO3/db retirement；不修改生产代码之外的无关文件。

**验收。** 实施 Executor 报告后，独立 Reviewer 必须仅判定 PASS/BLOCKED；PASS 后由不同 session 的 Adjudicator 使用真实 reviewer lease 对任务 apply/close。通过后，该能力才可作为 A′ Phase 0 的正式收口路径。

- schema-v59-and-append-only-supersedence-event @ db/schema.py
- lossless-v58-to-v59-migration-and-historical-data-preservation @ db/db_base.py
- durable-ledger-method-scope-and-replay-tests @ rust_ext/src/daemon/task_loop/operation_store.rs
- governance-handler-validation-cycle-check-and-projection @ rust_ext/src/daemon/task_collab.rs
- rpc-dispatch-and-capability-registry @ rust_ext/src/daemon/dispatch.rs
- http-method-exposure-and-capability-matrix @ rust_ext/src/daemon/http_server.rs
- cli-parser-daemon-only-adapter-and-show-projection @ cli/main.py
- client-fail-closed-route-policy @ server/daemon_client.py
- daemon-cli-and-negative-regression-tests @ tests/test_task_supersede_daemon.py
- cli-argument-and-projection-regression-tests @ tests/test_task_supersede_cli.py

Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 先核验设计是否保持 append-only 谱系、durable ledger、adjudicator-only 授权与无循环不变量；通过后再由 implementer 领取本任务实施。
  reason: 该任务是 A′ Phase 0 的产品能力前置，未经独立审查不得用它收口任何历史 S2。
  independence_requirement: required
