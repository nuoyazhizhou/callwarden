//! Enterprise daemon 模块——跨平台传输 + Peer_Credential + workspace registry。
//! D0 3.2：移除 Unix-only 编译门，Windows 通过命名管道提供等价能力。

/// JSON-RPC 协议层（跨平台，纯逻辑）
pub mod protocol;

/// JSON-RPC dispatch 路由表 + 基础方法（跨平台，纯逻辑）
pub mod dispatch;

/// Task 协同 RPC 模块（multi-llm-contract-collaboration D0/P1）
pub mod task_collab;

/// task_loop_public 能力 foundation（1D0：模块声明 / foundation 私有类型 /
/// fail-closed stubs / 禁用 shim，cutover 前能力禁用）
pub mod task_loop;


/// 平台无关传输抽象（D0 3.1：TransportListener / TransportConnection trait）
/// Unix: UDS + SO_PEERCRED；Windows: 命名管道 + ImpersonateNamedPipeClient
pub mod transport;

/// Windows 命名管道传输实现（D0 3.1：SDDL + 实例保活 + 对端 SID）
#[cfg(windows)]
pub mod transport_windows;

/// Daemon RPC Client（Phase 5-2 Slice 1）
/// 跨平台协议层 + Unix UDS Client（`#[cfg(unix)]`）
/// 契约：docs/design/phase5-2-slice1-daemon-client-contract.md
pub mod client;

/// Workspace registry + UID ACL（跨平台，rusqlite 数据层）
/// R4：实现 workspace.register / list / status + 路径校验 + owned_workspace ACL
pub mod workspace;

/// CAS（Content-Addressable Storage）+ file_generations 两阶段 CAS（跨平台）
/// R5：实现 compute_cas_key_v1 + cas_publish 四阶段原子发布 + cas_pin + cas_gc +
///     file_generation_seen / file_generation_committed
pub mod cas;

/// G1 Layer 2: Toolchain DB（独立 toolchain.db，与 CasStore / WorkspaceRegistry 对称）
/// 实现 toolchain CRUD + build_context CRUD + resolved_edges CRUD + workspace 绑定 + ATTACH
pub mod toolchain;

/// StagingLog——持久化 staging log（append-only + JSON Lines，崩溃安全，跨平台）
/// R5：实现 StagingEntry + StagingLog（append/read/read_pending/mark_applied_batch/
///     mark_failed/truncate/compact_applied）
pub mod staging_log;

/// Replicator——Session 管理 + daemon_handle_connect + daemon_handle_refresh +
/// Replicator（跨平台，rusqlite + CasStore + StagingLog）
/// R5：实现 SESSION_SCHEMA_DDL + daemon_handle_connect（session epoch CAS）+
///     daemon_handle_refresh（两阶段 CAS）+ Replicator（replicate / recover /
///     get_pending_count）+ SnapshotPublisher trait（R6 扩展点）
pub mod replicator;

/// CAS → CodeGraph DB merge（P0-2 子问题1 修复，2026-07-22）
/// 把 CAS DB 中的解析结果（cas_symbols/cas_raw_calls）merge 到 CodeGraph DB
/// 主表（file_instances/symbols/calls），对应 Python db_cas_merge.py
pub mod cas_merge;

/// 对端凭证 → Peer_Identity 派生（三平台：Linux SO_PEERCRED / macOS LOCAL_PEERCRED / Windows 命名管道 SID）
/// D0 3.3：内部按 cfg 分流，跨平台 PeerIdentity 枚举始终可用
pub mod peercred;

/// Authoritative_Clock —— daemon 进程唯一权威时间源（D0 3.8，Req 14.11, 14.12）
/// 单调不回退，供 Lease/verdict/Evidence/Attestation/gate decision 使用
pub mod clock;

/// Protected_Mutation 串行化点（D0 3.7，Req 14.6, 14.7, 14.14）
/// daemon 进程内唯一写操作入口，带请求超时
pub mod serialization;

/// Daemon 侧 Attestation 签发与校验（D0 3.9，Req 14.13）
/// 基于 Peer_Credential 派生 Identity + Authoritative_Clock 签发，HMAC-SHA256 签名
pub mod attestation;

/// 并发 gate 判定隔离（D0 3.10，Req 14.15）
/// 每个 gate 判定绑定独立 Gate_Snapshot，未提交中间态互不可见
pub mod gate_session;

/// Stage_Toggle 存储与前置阶段校验（D0 3.12，Req 13.11–13.21）
/// P0–P4 三级作用域 + 前置阶段校验 + Independence_Policy 存取
pub mod stage_toggle;

/// Daemon server（跨平台：Unix UDS + Windows 命名管道，内部 cfg 分流）
pub mod server;

/// H1 HTTP MVP transport（Profile v1: dev_loopback_unauthenticated）
/// 冻结栈：axum 0.8 + hyper 1 + tokio 1，仅 HTTP/1.1，loopback-only
pub mod http_server;

/// H3 Python compatibility worker adapter
/// daemon 管理 worker 生命周期 + 帧协议 IPC + 兼容写锁
/// 契约：docs/design/http-daemon-mvp-compatibility-contract.md §3.3
pub mod compat_adapter;

/// R6: SnapshotDaemonState —— 集成 SnapshotCache 的 daemon state 实现
/// 实现 snapshot.publish / gc.snapshots / query.* handler
/// 跨平台：query.* 和 gc.snapshots 纯逻辑，snapshot.publish 的 FD 模式仅 Unix
pub mod snapshot_state;

/// G14: Health Check + Recovery Handler
/// 实现 HealthChecker（4 项检查）+ RecoveryHandler（4 步恢复）
/// 跨平台：rusqlite + fs2 + std，内存检查仅 Linux（读 /proc/self/status）
pub mod health;

/// R7: cw_daemon binary 配置加载（DaemonConfig + 环境变量覆盖 + 文件加载）
pub mod config;

/// G10/G20: memfd / FD 读取四重校验（FD 类型 + 大小预检 + 容量上限 + 摘要比对）
/// 仅 Unix 编译（Windows 无 FD 路径）
#[cfg(unix)]
pub mod memfd;

/// G29: QueryBudget + BudgetTracker——查询预算控制（max_depth + max_nodes + timeout）
/// 防止 BFS/DFS 在大型代码库中指数爆炸
pub mod budget;

/// P1-F Step 2: 失败 generation 保护 + dirty overlay 隔离（设计 §5.3 + §9.3）
/// 提供 `evaluate_generation_protection` / `should_replace_snapshot` /
/// `is_dirty_overlay`，供 daemon 在 publish_snapshot 前显式检查失败状态
pub mod snapshot_guard;

/// P1-F Step 3: Parse 失败 durable log + daemon 重启重放（设计 §8 Phase 4）
/// 提供 `ParseRetryLog`（append-only + JSON Lines + 崩溃安全）记录 failed/partial/
/// unsupported/stale 状态，`replay_pending` 只重放 allows_retry=true 的 generation
pub mod parse_retry_log;

/// 3.11: 昂贵 verifier 执行——SQLite 写事务外运行（Req 14.16, 6.18）
/// 提供 `VerifierExecutor`（S0/S1 TOCTOU 防护 + 事务外执行）和
/// `ScopedHasher`（作用域内容 hash，不默认全仓库）
pub mod verifier_exec;

/// 3.13: 稳定错误码目录 + 双语 message key（Req 1.12, 5.14–5.15, 7.16, 10.12, 14.30, 14.36）
/// 覆盖 D0 拒绝路径 + 警告码 + 跨类操作 + Independence_Policy + Revocation_Mode
pub mod error_codes;

/// P1-F Step 4: Parser metrics + doctor 自检（设计 §8 Phase 4）
/// 提供 `ParserMetrics`（AtomicU64 计数器 + 有界 recent_failures）和
/// `ParserDoctor`（Rust grammar/ABI 自检），任何 parse 失败可定位到
/// workspace/file/generation/language
pub mod parser_metrics;

/// 3.14: 路由矩阵——239 工具单一真相源 Rust 镜像（T01 收敛架构）
/// 数据来自 deliverables/software-company/tool_migration_matrix.json
/// （由 scripts/gen_route_matrix.py --emit-json 生成，禁止手改）。
pub mod route_matrix;

/// 3.15: 文件/构建面 handler（workspace.build_graph / workspace.file.* 家族）
/// 对应 18 个纯本地 SQL 工具中的文件面 9 个（T02-fs 批次）。
pub mod fs_handlers;

/// 3.16: 度量/状态面 handler（query.code_health / query.metrics_summary 等）
/// 对应 18 个纯本地 SQL 工具中的度量面 9 个（T02-metrics 批次）。
pub mod metrics_handlers;

/// 3.20: MCP common 面 handler（SRV-001：server mcp common Python authority → Rust daemon）
/// 对应 `server/_mcp_common.py::_get_db_path_for_daemon` 的 Rust 下沉
///（`mcp.common.get_db_path_for_daemon` 返回 daemon 权威任务库路径）。
pub mod _mcp_common_handlers;

/// 3.21: audit log 面 handler（SRV-002：server audit log Python authority → Rust daemon）
/// 对应 `server/audit_log.py::AuditLogger` 的 SQLite 权威下沉
///（`mcp.audit_log.{get_conn,init_db,append,query,count,clear,get_stats}`）。
pub mod audit_log_handlers;

/// 3.17: 异步长任务 job 状态机（task.job_submit / task.job_cancel / job 执行器）
/// 对应 61 个拒止工具中的异步长任务组 18 个（T02-job 批次，target_backend=task_rpc）。
pub mod job_runner;

/// 3.18: GC/审计/运维 handler（admin.gc_* / admin.audit_* / admin.branch_* 等）
/// 对应 61 个拒止工具中的 GC/审计/运维/协同写面（T02-admin 批次）。
pub mod admin_handlers;

/// 3.19: 编辑/提案/规则写面 handler（edit.propose* / rule.* / gate.* / summary.generate）
/// 对应 61 个拒止工具中的编辑/规则写面（T02-edit 批次，经 CAS + SerializationPoint）。
pub mod edit_handlers;

/// daemon schema 版本号（与 db/schema.py:SCHEMA_VERSION 保持同步）
/// 用于 schema.version RPC 方法返回，以及 daemon 启动时 schema 兼容性检查。
/// 更新 schema 时记得同步修改。
pub const SCHEMA_VERSION: u32 = 51;
