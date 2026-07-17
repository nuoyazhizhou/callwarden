//! Enterprise daemon 模块——UDS server + SO_PEERCRED + workspace registry。
//! Linux 特性用 #[cfg(unix)] 条件编译，Windows 上编译为空模块。

/// JSON-RPC 协议层（跨平台，纯逻辑）
pub mod protocol;

/// JSON-RPC dispatch 路由表 + 基础方法（跨平台，纯逻辑）
pub mod dispatch;

/// Workspace registry + UID ACL（跨平台，rusqlite 数据层）
/// R4：实现 workspace.register / list / status + 路径校验 + owned_workspace ACL
pub mod workspace;

/// CAS（Content-Addressable Storage）+ file_generations 两阶段 CAS（跨平台）
/// R5：实现 compute_cas_key_v1 + cas_publish 四阶段原子发布 + cas_pin + cas_gc +
///     file_generation_seen / file_generation_committed
pub mod cas;

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

#[cfg(unix)]
pub mod server;
#[cfg(unix)]
pub mod peercred;

/// R6: SnapshotDaemonState —— 集成 SnapshotCache 的 daemon state 实现
/// 实现 snapshot.publish / gc.snapshots / query.* handler
/// 跨平台：query.* 和 gc.snapshots 纯逻辑，snapshot.publish 的 FD 模式仅 Unix
pub mod snapshot_state;

/// daemon schema 版本号（与 db/schema.py:SCHEMA_VERSION 保持同步）
/// 用于 schema.version RPC 方法返回，以及 daemon 启动时 schema 兼容性检查。
/// 更新 schema 时记得同步修改。
pub const SCHEMA_VERSION: u32 = 37;
