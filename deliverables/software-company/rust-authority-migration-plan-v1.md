# Call Warden Rust Authority Migration Plan v1

状态：`DRAFT_BLOCKED_ON_GRAPH_AUTHORITY`

目标：把 Call Warden 收敛为 Python CLI/MCP HTTP thin client + Rust daemon 业务与数据 authority。Python 不再直接打开 SQLite、执行 DB 业务逻辑、调用 PyO3 业务入口或保留 Python parser/分析器作为生产 authority。

本计划不是立即批量建卡清单。它是分层导入蓝图：先挂载主干，再逐个主干挂分支，最后按一个函数族或一条调用链挂叶子。每个叶子最多一个 ownership、一个 daemon/API 边界和一个独立验收目标；超过 5 个实现步骤、同时跨 schema/daemon/client 或超过约 5 个函数时必须继续拆分。

## A0 [Gate/graph-authority]：知识图谱与 authority 基线恢复

这是所有后续导入的前置主干，不是 P0-L 的隐式依赖。当前 `cw refresh --all` 仍从 `cli/main.py` 通过 `RpcDBProxy` 调用不存在的 `build_full_graph` RPC；live capability 只有 `snapshot.publish`，运行实例也落后于当前源码。目标是让 Planner 能用 daemon 真实读取本仓库并获得可复核 snapshot。

- 只允许 `rust_ext/src/daemon/`、`rust_ext/src/bin/cw_daemon.rs`、`server/daemon_client.py`、`cli/main.py`、`tests/` 和对应设计/证据文件。
- 不允许用 Python `db.build_full_graph`、本地 SQLite fallback、直接 SQL 或人工伪造 snapshot。
- 叶子顺序：daemon refresh-plan/文件 refresh round-trip；Rust 全量/增量构建 authority；thin CLI `refresh --all`；snapshot publish/status；runtime manifest 与 fresh snapshot 证据。
- 验收必须包含真实 daemon round-trip、空/脏/删除/force 负向矩阵、运行 PID/二进制/commit/schema 指纹一致，且 `cw status` 不再返回 `snapshot_not_ready`。
- 完成后才允许用图谱生成下面各主干的精确 symbol/caller/callee 清单；本 Gate 不得顺手迁移业务工具。

## A1 [Build/Parse]：解析、构建与图谱写入 Rust authority

把 `db/db_build.py`、`db/rust_parser_facade.py`、`parsers/`、`analyzers/call_chain.py`、`analyzers/resolved_edges_engine.py` 中仍承担生产解析、文件版本、symbols/calls、深度、CAS merge 的逻辑迁到 Rust daemon。Python 只保留参数/结果适配。

- 分支：canonical input 与语言 dispatch；文件注册/版本与 tombstone；symbols 批量写；calls/resolution/depth；CAS/manifest/FTS/index 发布；watcher 增量与重试。
- 每个叶子只覆盖一个函数族或一条从 RPC 到事务提交的调用链；禁止把 parser、schema、CLI 和 MCP 同卡实现。
- Rust 事务必须定义唯一 authority、幂等 request、失败回滚、partial/unsupported 可观测状态；Python 旧路径只能作为差分 fixture，不得继续作为生产 fallback。
- 验收：Python/Rust 结果差分、重复 refresh unchanged、force、删除 tombstone、解析失败不发布空 snapshot、并发 agent 不互相覆盖。

## A2 [Read/Analysis]：查询、分析与报告 Rust handlers

把 `db/db_query.py`、`db/db_metrics.py`、`db/db_impact.py`、`db/db_dashboard.py`、`db/db_summary.py`、`db/db_coverage.py`、`db/db_tests.py`、`db/db_evolution.py`、`db/db_git.py`、`db/db_cross_repo.py`、`db/db_token_savings.py`、`db/db_ownership.py`、`db/db_defect_kb.py` 及其 analyzer 业务迁为 Rust read handlers。

- 分支按调用链而不是按“所有 query 一张卡”划分：符号/文件/调用链；指标/复杂度/覆盖率；影响/演化/缺陷；Git/跨仓库；摘要/ownership/token savings；语义/向量/clone 查询。
- 当前 route matrix 的 78 个 `python_compat` 必须逐项变成 `rust_native` 或有明确永久保留理由；`123 RustNative + 78 PythonCompat + 41 TaskRpc` 与旧文档宣称的 239 工具数必须在 Gate 中对账，不能直接把数字当完成证据。
- 每个叶子要求成功、空结果、越权 workspace、未发布 snapshot、坏参数和 daemon unavailable 负向测试；结果 schema 由 Rust 定义，Python 不做业务重算。

## A3 [Write/Admin]：编辑、规则、审计与维护写 authority

把 `db/db_edit.py`、`db/db_comment.py`、`db/db_guardrail.py`、`db/db_agent_rules.py`、`db/db_rollback_config.py`、`db/db_audit_chain.py`、`db/db_gc.py`、`db/db_clone_detection.py`、`db/db_clone_groups.py`、`db/db_vector.py`、`db/db_toolchain.py`、`db/db_jobs.py`、`db/db_external.py` 中仍有 DB/业务决策的路径迁入 Rust daemon。

- 分支：编辑/注释/审计；guardrail/rule；GC/clone/vector；toolchain/build-context/jobs；backup/restore/migrate/external integration。
- 每个叶子只绑定一个 protected mutation family，明确 serialization point、owner ACL、幂等与 rollback；读面不得暗中写库。
- Python 只负责 HTTP 参数、用户输出和本机非业务配置；任何 `conn.execute`、事务、状态机和结果聚合都必须在 Rust。
- 验收：正向 round-trip、权限拒绝、重复 request replay、事务失败回滚、并发写锁、旧 Python 入口退出后无行为漂移。

## A4 [Governance]：任务、合同、证据与角色生命周期 Rust authority

把 `db/db_tasks.py`、`db/db_task_contracts.py`、`db/db_task_dependencies.py`、`db/db_task_evidence.py`、`db/db_task_gate.py`、`db/db_task_identity.py`、`db/db_task_leases.py`、`db/db_task_quality.py`、`db/db_task_reviews.py`、`db/db_task_attribution.py`、`db/task_snapshot.py` 的残余 Python authority 与治理状态机收敛到 Rust daemon。

- 分支：task create/split/binding；contract/revision/policy；assignment/claim/heartbeat/recovery；report/handoff/verdict/evidence；review/adjudication/apply/close；finding/remediation/Planner replan。
- P0-L、旧 A″、历史 task reconciliation 是治理数据和 capability 的独立修复线，不能阻塞 A1/A2/A3 的业务迁移；但所有新叶子必须经过当前 daemon 可验证的 binding、Contract、identity policy 和 evidence gate。
- `task.create`/`task.split` 必须原子写 task、workspace binding、四角色 contracts、identity policy、steps 和依赖；任何缺失 fail-closed，禁止 Python/SQL 补写。
- 验收：状态投影与事件一致、同一 request 幂等、stale claim 可由同角色接管、BLOCKED 自动产生 provenance-bound remediation、Planner/Executor 往复可持久化、历史数据只追加修复。

## A5 [Transport/Client]：HTTP thin-client、session 与旧 PyO3/IPC 退役

在 A0 至 A4 的 successor 已可用后，收敛 `server/daemon_client.py`、`server/daemon_protocol.py`、`server/agent_protocol.py`、`server/agent_session.py`、`server/agent_watcher.py`、`server/daemon_autostart.py`、`server/daemon_config.py`、`server/ipc_transport.py`、`server/replicator.py`、`server/snapshot_manager.py`、`server/compat_registry.py` 与 `rust_ext/src/daemon/client.rs` 的边界。

- 分支：HTTP request/response codec；workspace/session/refresh transport；MCP tool adapters；CLI adapters；opaque local session handle；legacy UDS/PyO3 call-site retirement。
- Python 只做 HTTP/MCP/CLI adapter，不传 raw credential/token，不打开业务 DB，不把 provider/account/model/session 当授权锚点。
- 每个叶子只退役一个 PyO3 export 或一个不可拆 request/response pair；必须先有 successor、caller 清零证明和 ABI/错误 parity。
- 验收：239/当前 route manifest 对账、HTTP 正负 round-trip、daemon unavailable fail-closed、无 legacy fallback、secret scan、`check_client_purity` 和 import graph 通过。

## A6 [Retirement/Release]：兼容窗口收口与最终删除

只有 A1–A5 的对应叶子全部独立通过后，才清理 `db/` 中无调用者的模块、`server/compat_worker.py`/compat registry 过渡窗口、旧 PyO3 exports、旧 IPC framing 和陈旧文档/矩阵。

- 分支：逐模块 caller-zero 证明；compat route retire；`db/` 删除批次；文档/route matrix/packaging 更新；全量 release gate。
- 禁止以静态 `check_client_purity=0` 代替 daemon round-trip；禁止一次删除整个 `db/`；每批必须有 task-bound manifest、删除清单、回滚点和 fresh runtime evidence。
- 最终验收：Python 仅剩 thin client/transport/config；Rust daemon 是唯一业务与 DB authority；全量测试、迁移矩阵、运行时指纹、图谱 snapshot 和 provenance ledger 一致。

### 导入顺序与建卡规则

1. 先修 A0 的 Graph Authority Gate，并通过 daemon 真实发布 snapshot；同时由受支持治理路径补齐 Epic `T-1787203926824-9f873bfc` 的不可变 workspace binding。当前该 Epic `next-action` 已明确返回 `E_WORKSPACE_AUTHORITY_UNAVAILABLE`，因此现在不能合法 `task.split`。
2. A0 完成后只在 Epic 下创建上述 6 个主干；每次创建必须由 daemon 原子写完整 Contract、binding、identity policy 和至少一个 step。现有 A″ parent/G0 保留历史事实，不复制同名任务；是否 supersede 由后续正式治理事件决定。
3. 主干稳定后逐个 `task.split` 分支；分支完成 review/adjudication 或满足明确 predecessor 后，再逐个生成叶子。不得预建整棵 200+ 卡树。
4. 每张叶子必须带：`task_id` placeholder、title、description、role contracts、identity policy、allowed/excluded paths、predecessor、acceptance commands、positive/negative tests、evidence manifest、rollback、idempotency key、binding requirements、successor rule。
5. P0-L 等治理修复只在影响 task creation/claim/review authority 时作为 A0/A4 的明确依赖；普通业务迁移不得因为历史治理卡未 close 而事实停工。若某个治理能力缺失，Planner 将其拆成独立 capability leaf，由 Executor 实现，不要求采购方手工改库。

### 当前审计事实与禁止假设

- 代码图谱当前无 published snapshot；`cw refresh --all` 实测失败：daemon `method_not_found: build_full_graph`。
- live daemon health 为 schema 60、registry `http-mvp-cap-registry-v1`，运行 binary 与当前 HEAD 不一致；部署必须在 source fix 后重新产生 task-bound provenance。
- 纯度脚本当前通过，只能说明 `server/tools`、`cw.py`、`cli` 的直接禁用模式为零，不能证明 `db/` 已退休或 daemon 已承接全部业务。
- 不以历史 CLI/MCP “closed” 记录替代源码 caller audit、route capability、真实 RPC、snapshot、测试和 Git attribution。
