//! JSON-RPC dispatch 路由表 + 基础方法实现。
//!
//! 参考：server/daemon_server.py:EnterpriseDaemonService.dispatch（L121-514）。
//! 本模块负责：
//! - 解析 RPC 请求（method + params + received_fds）
//! - 路由到对应 handler
//! - 基础方法实现：ping / health / schema.version
//! - 错误码体系：INVALID_PARAMS / METHOD_NOT_FOUND / INTERNAL_ERROR / PERMISSION_DENIED
//!
//! 高级方法（workspace.*/snapshot.*/query.*/gc.*/backup/restore）在 R4-R6 实现，
//! 本模块提供 trait 钩子供后续扩展。

use super::protocol::{make_error_response, make_ok_response};
use serde_json::{Map, Value};
use std::time::Instant;

/// peer credential（来自 SO_PEERCRED）
#[derive(Debug, Clone, Copy)]
pub struct PeerCredential {
    pub uid: u32,
    pub gid: u32,
    pub pid: i32,
}

/// daemon 运行状态（基础方法用，高级方法由 DaemonStateExt trait 扩展）
pub struct DaemonState {
    /// daemon 启动时间（用于计算 uptime）
    pub start_time: Instant,
    /// schema 版本号（与 db/schema.py:SCHEMA_VERSION 保持同步）
    pub schema_version: u32,
    /// daemon 进程 PID
    pub pid: u32,
}

impl Default for DaemonState {
    fn default() -> Self {
        Self {
            start_time: Instant::now(),
            schema_version: super::SCHEMA_VERSION,
            pid: std::process::id(),
        }
    }
}

/// daemon RPC 错误（对应 Python DaemonRpcError）
#[derive(Debug, Clone)]
pub struct DaemonRpcError {
    pub code: String,
    pub message: String,
}

impl DaemonRpcError {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_string(),
            message: message.into(),
        }
    }

    pub fn invalid_params(msg: impl Into<String>) -> Self {
        Self::new("invalid_params", msg)
    }

    pub fn method_not_found(method: &str) -> Self {
        Self::new("method_not_found", format!("未知方法: {}", method))
    }

    pub fn internal_error(msg: impl Into<String>) -> Self {
        Self::new("internal_error", msg)
    }

    pub fn permission_denied(msg: impl Into<String>) -> Self {
        Self::new("permission_denied", msg)
    }

    pub fn workspace_not_found(workspace_id: &str) -> Self {
        Self::new("workspace_not_found", workspace_id.to_string())
    }

    pub fn workspace_forbidden(msg: impl Into<String>) -> Self {
        Self::new("workspace_forbidden", msg)
    }

    pub fn workspace_archived(workspace_id: &str) -> Self {
        Self::new("workspace_archived", workspace_id.to_string())
    }
}

impl std::fmt::Display for DaemonRpcError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for DaemonRpcError {}

/// daemon 状态扩展 trait（高级方法 handler 由 R4-R6 实现）
///
/// 默认实现返回 method_not_found，避免 R3 阶段编译失败。
/// R4-R6 实现 DaemonState 后，重写对应方法即可接入路由。
///
/// 基础方法（ping/health/schema.version）也在 trait 中提供默认实现，
/// 因为 dispatch_inner 接受泛型 S: DaemonStateExt，统一通过 state.method() 调用。
pub trait DaemonStateExt {
    /// 返回基础 DaemonState（用于获取 pid / start_time / schema_version）
    fn daemon_state(&self) -> &DaemonState;

    // ---- 基础方法（R3 默认实现）----

    fn handle_ping(&mut self, peer: PeerCredential) -> Result<Value, DaemonRpcError> {
        let state = self.daemon_state();
        let mut m = Map::new();
        m.insert("status".to_string(), Value::String("ok".to_string()));
        m.insert("peer_uid".to_string(), Value::Number(peer.uid.into()));
        m.insert("pid".to_string(), Value::Number(state.pid.into()));
        Ok(Value::Object(m))
    }

    fn handle_health(&mut self, _peer: PeerCredential) -> Result<Value, DaemonRpcError> {
        let state = self.daemon_state();
        let uptime = state.start_time.elapsed().as_secs();
        let mut m = Map::new();
        m.insert("status".to_string(), Value::String("ok".to_string()));
        m.insert("pid".to_string(), Value::Number(state.pid.into()));
        m.insert("uptime_seconds".to_string(), Value::Number(uptime.into()));
        m.insert(
            "schema_version".to_string(),
            Value::Number(state.schema_version.into()),
        );
        m.insert("workspace_count".to_string(), Value::Number(0u32.into()));
        Ok(Value::Object(m))
    }

    fn handle_schema_version(&mut self, _peer: PeerCredential) -> Result<Value, DaemonRpcError> {
        let state = self.daemon_state();
        let mut m = Map::new();
        m.insert(
            "version".to_string(),
            Value::Number(state.schema_version.into()),
        );
        Ok(Value::Object(m))
    }

    // ---- 高级方法（R4-R6 实现，默认 method_not_found）----

    fn handle_workspace_register(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.register"))
    }

    fn handle_workspace_list(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.list"))
    }

    fn handle_workspace_status(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.status"))
    }

    fn handle_workspace_activate(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.activate"))
    }

    fn handle_workspace_remove(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.remove"))
    }

    fn handle_workspace_connect(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.connect"))
    }

    fn handle_workspace_file_refresh(
        &mut self,
        peer: PeerCredential,
        params: &Value,
        received_fds: &[i32],
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params, received_fds);
        Err(DaemonRpcError::method_not_found("workspace.file.refresh"))
    }

    fn handle_workspace_refresh_plan(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.refresh.plan"))
    }

    fn handle_workspace_file_delete(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.file.delete"))
    }

    fn handle_workspace_recover(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.recover"))
    }

    fn handle_snapshot_publish(
        &mut self,
        peer: PeerCredential,
        params: &Value,
        received_fds: &[i32],
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params, received_fds);
        Err(DaemonRpcError::method_not_found("snapshot.publish"))
    }

    fn handle_gc_snapshots(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("gc.snapshots"))
    }

    fn handle_gc_cas(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("gc.cas"))
    }

    fn handle_query_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.stats"))
    }

    fn handle_query_symbol(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.symbol"))
    }

    fn handle_query_search(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.search"))
    }

    fn handle_query_callers(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.callers"))
    }

    fn handle_query_callees(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.callees"))
    }

    // ---- 高级查询方法（G7-T4：Python snapshot_manager.py:305-373 对应）----
    // 默认实现返回 method_not_found，由 SnapshotDaemonState 覆盖

    fn handle_query_call_chain_down(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.call_chain_down"))
    }

    fn handle_query_impact(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.impact"))
    }

    fn handle_query_topological_order(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.topological_order"))
    }

    fn handle_query_detect_cycles(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.detect_cycles"))
    }

    // ---- Snapshot 管理方法（G7-T5：Python snapshot_manager.py:list/evict/stats 对应）----
    // 默认实现返回 method_not_found，由 SnapshotDaemonState 覆盖

    fn handle_snapshot_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("snapshot.stats"))
    }

    fn handle_snapshot_list_workspaces(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("snapshot.list_workspaces"))
    }

    fn handle_snapshot_evict(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("snapshot.evict"))
    }

    fn handle_backup(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("backup"))
    }

    fn handle_restore(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("restore"))
    }

    // ---- Mount Mapping 管理（G4 实现）----
    // 默认实现返回 method_not_found，由 WorkspaceDaemonState 覆盖

    fn handle_mount_register(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("mount.register"))
    }

    fn handle_mount_list(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("mount.list"))
    }

    fn handle_mount_delete(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("mount.delete"))
    }

    // ---- Toolchain 管理（G1 Layer 2 实现）----
    // 默认实现返回 method_not_found，由 WorkspaceDaemonState 覆盖

    fn handle_toolchain_register(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("toolchain.register"))
    }

    fn handle_toolchain_list(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("toolchain.list"))
    }

    fn handle_toolchain_get(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("toolchain.get"))
    }

    fn handle_toolchain_delete(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("toolchain.delete"))
    }

    fn handle_toolchain_bind(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("toolchain.bind"))
    }

    fn handle_toolchain_resolve(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("toolchain.resolve"))
    }

    fn handle_toolchain_list_bound(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("toolchain.list_bound"))
    }

    // ---- Build Context 管理（G1 Layer 2 实现）----

    fn handle_build_context_register(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("build_context.register"))
    }

    fn handle_build_context_list(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("build_context.list"))
    }

    fn handle_build_context_get(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("build_context.get"))
    }

    fn handle_build_context_set_active(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("build_context.set_active"))
    }

    fn handle_build_context_delete(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("build_context.delete"))
    }

    // ---- Resolved Edges（G1 Layer 2 实现）----

    fn handle_resolved_edges_store(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("resolved_edges.store"))
    }

    fn handle_resolved_edges_get(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("resolved_edges.get"))
    }

    fn handle_resolved_edges_count(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("resolved_edges.count"))
    }

    fn handle_resolved_edges_replace(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("resolved_edges.replace"))
    }
}

/// daemon state 扩展的默认实现（所有高级方法返回 method_not_found）
impl DaemonStateExt for DaemonState {
    fn daemon_state(&self) -> &DaemonState {
        self
    }
}

/// 执行单个 RPC 请求，返回 JSON-RPC 响应
///
/// 参数：
/// - state: daemon 状态（实现 DaemonStateExt trait）
/// - peer: peer credential（来自 SO_PEERCRED）
/// - method: RPC 方法名
/// - params: 参数（JSON object）
/// - received_fds: 附加的 FD 列表（来自 SCM_RIGHTS）
///
/// 返回：JSON-RPC 响应（{ok:true, result} 或 {ok:false, error:{code,message}}）
pub fn dispatch<S: DaemonStateExt>(
    state: &mut S,
    peer: PeerCredential,
    method: &str,
    params: &Value,
    received_fds: &[i32],
) -> Value {
    let result = dispatch_inner(state, peer, method, params, received_fds);
    match result {
        Ok(value) => make_ok_response(value),
        Err(err) => make_error_response(&err.code, &err.message),
    }
}

/// 需要管理员权限的运维方法（修改全局配置 / 资源回收 / 数据库备份还原）。
///
/// 授权规则：`peer.uid == 0`（root）或 `peer.uid == current_uid()`（daemon 进程自己）。
/// workspace.file.refresh / workspace.register 等已经通过 owned_workspace / validate_owned_path
/// 做了 per-workspace UID ACL，不重复检查；只读方法（list/get/query/stats）允许任意已连接 peer。
pub const ADMIN_ONLY_METHODS: &[&str] = &[
    // 数据库备份 / 还原
    "backup",
    "restore",
    // 资源回收（CAS / snapshots / evict）
    "gc.cas",
    "gc.snapshots",
    "snapshot.evict",
    // Mount Mapping 写操作（register / delete）
    "mount.register",
    "mount.delete",
    // Mount Mapping 读操作（P0-2 整改 2026-07-21）
    // mount.list 暴露全局 host_path 映射，container_mount_mappings 表无 owner_uid 列，
    // 无法按 UID 过滤；改为 admin-only 避免普通用户枚举宿主机路径。
    "mount.list",
    // Toolchain 配置变更（register / delete / bind）
    "toolchain.register",
    "toolchain.delete",
    "toolchain.bind",
    // Build Context 属于 workspace 资源，由 handler 做 owner ACL。
];

/// 返回 daemon 进程自己的 uid（Unix: getuid；Windows: 与测试 current_uid() 一致）
///
/// P1-1 修复（2026-07-22 完整复审）：Windows 上没有真正的 Unix UID 概念，
/// 原 `0` 与 workspace.rs 测试中的 `current_uid()=1000` 不一致，导致 admin-only
/// 方法（backup/restore/gc.cas/mount.*）的测试 peer 在 Windows 上永远不是 admin。
/// 改为返回 1000，与测试 `current_uid()` 对齐，使 `make_owner_peer()` 在 Windows
/// 上也通过 `is_admin` 检查（`peer.uid == current_daemon_uid()`）。
pub fn current_daemon_uid() -> u32 {
    #[cfg(unix)]
    {
        // SAFETY: getuid() 是无副作用 syscall，永远安全
        unsafe { libc::getuid() }
    }
    #[cfg(not(unix))]
    {
        // Windows：与 workspace.rs tests::current_uid() 保持一致（1000）
        1000
    }
}

/// 判断 peer 是否为管理员（root 或 daemon 进程自己）
pub fn is_admin(peer: PeerCredential) -> bool {
    peer.uid == 0 || peer.uid == current_daemon_uid()
}

/// dispatch 内部实现（返回 Result<Value, DaemonRpcError>）
fn dispatch_inner<S: DaemonStateExt>(
    state: &mut S,
    peer: PeerCredential,
    method: &str,
    params: &Value,
    received_fds: &[i32],
) -> Result<Value, DaemonRpcError> {
    // 管理员方法授权检查（fail-closed：未授权直接拒绝，不进入 handler）
    //
    // Phase 4-2 implement 状态：
    // - ADMIN_ONLY_METHODS 覆盖全局运维与 toolchain 写操作
    // - build_context/resolved_edges 是 workspace 资源，由 handler 做 owner ACL
    // - is_admin 判定已实现（uid==0 或 uid==current_daemon_uid()）
    // - workspace owner 校验在 workspace.rs:owned_workspace/owned_workspace_by_id 内
    // - 路径安全在 workspace.rs:validate_owned_path 内（canonicalize + owner_uid）
    // - QueryBudget 在 budget.rs 内（max_nodes/timeout_ms）
    //
    // TODO(audit): ACL 拒绝事件应记录到 audit_log（迁移自 Python server/audit_log.py）。
    //   当前仅返回 permission_denied 错误，未持久化审计记录。
    //   迁移后应在下面 ACL 拒绝分支调用 audit_logger.record(
    //     event_type="ACL_DENIED", actor_uid=peer.uid, method=method, result="denied")
    //   admin-only 方法执行成功/失败也应记录（event_type="ADMIN_OP", result=ok/fail）。
    if ADMIN_ONLY_METHODS.contains(&method) && !is_admin(peer) {
        return Err(DaemonRpcError::permission_denied(format!(
            "方法 {} 需要管理员权限（root 或 daemon uid），当前 peer.uid={}",
            method, peer.uid
        )));
    }
    match method {
        // ---- 基础方法（R3 默认实现）----
        "ping" => state.handle_ping(peer),
        "health" => state.handle_health(peer),
        "schema.version" => state.handle_schema_version(peer),

        // ---- Workspace 管理（R4 实现）----
        "workspace.register" => state.handle_workspace_register(peer, params),
        "workspace.list" => state.handle_workspace_list(peer, params),
        "workspace.status" => state.handle_workspace_status(peer, params),
        "workspace.activate" => state.handle_workspace_activate(peer, params),
        "workspace.remove" => state.handle_workspace_remove(peer, params),
        "workspace.connect" => state.handle_workspace_connect(peer, params),
        "workspace.refresh.plan" => state.handle_workspace_refresh_plan(peer, params),
        "workspace.file.refresh" => state.handle_workspace_file_refresh(peer, params, received_fds),
        "workspace.file.delete" => state.handle_workspace_file_delete(peer, params),
        "workspace.recover" => state.handle_workspace_recover(peer, params),

        // ---- Snapshot 管理（R6 实现）----
        "snapshot.publish" => state.handle_snapshot_publish(peer, params, received_fds),
        "gc.snapshots" => state.handle_gc_snapshots(peer, params),

        // ---- CAS GC（R6 实现）----
        "gc.cas" => state.handle_gc_cas(peer, params),

        // ---- 查询方法（R6 实现）----
        "query.stats" => state.handle_query_stats(peer, params),
        "query.symbol" => state.handle_query_symbol(peer, params),
        "query.search" => state.handle_query_search(peer, params),
        "query.callers" => state.handle_query_callers(peer, params),
        "query.callees" => state.handle_query_callees(peer, params),

        // ---- 高级查询方法（G7-T4 实现）----
        "query.call_chain_down" => state.handle_query_call_chain_down(peer, params),
        "query.impact" => state.handle_query_impact(peer, params),
        "query.topological_order" => state.handle_query_topological_order(peer, params),
        "query.detect_cycles" => state.handle_query_detect_cycles(peer, params),

        // ---- Snapshot 管理方法（G7-T5 实现）----
        "snapshot.stats" => state.handle_snapshot_stats(peer, params),
        "snapshot.list_workspaces" => state.handle_snapshot_list_workspaces(peer, params),
        "snapshot.evict" => state.handle_snapshot_evict(peer, params),

        // ---- 运维方法（R6 实现）----
        "backup" => state.handle_backup(peer, params),
        "restore" => state.handle_restore(peer, params),

        // ---- Mount Mapping 管理（G4 实现）----
        "mount.register" => state.handle_mount_register(peer, params),
        "mount.list" => state.handle_mount_list(peer, params),
        "mount.delete" => state.handle_mount_delete(peer, params),

        // ---- Toolchain 管理（G1 Layer 2 实现）----
        "toolchain.register" => state.handle_toolchain_register(peer, params),
        "toolchain.list" => state.handle_toolchain_list(peer, params),
        "toolchain.get" => state.handle_toolchain_get(peer, params),
        "toolchain.delete" => state.handle_toolchain_delete(peer, params),
        "toolchain.bind" => state.handle_toolchain_bind(peer, params),
        "toolchain.resolve" => state.handle_toolchain_resolve(peer, params),
        "toolchain.list_bound" => state.handle_toolchain_list_bound(peer, params),

        // ---- Build Context 管理（G1 Layer 2 实现）----
        "build_context.register" => state.handle_build_context_register(peer, params),
        "build_context.list" => state.handle_build_context_list(peer, params),
        "build_context.get" => state.handle_build_context_get(peer, params),
        "build_context.set_active" => state.handle_build_context_set_active(peer, params),
        "build_context.delete" => state.handle_build_context_delete(peer, params),

        // ---- Resolved Edges（G1 Layer 2 实现）----
        "resolved_edges.store" => state.handle_resolved_edges_store(peer, params),
        "resolved_edges.get" => state.handle_resolved_edges_get(peer, params),
        "resolved_edges.count" => state.handle_resolved_edges_count(peer, params),
        "resolved_edges.replace" => state.handle_resolved_edges_replace(peer, params),

        // ---- 未知方法 ----
        _ => Err(DaemonRpcError::method_not_found(method)),
    }
}

// ============================================
// 参数解析工具函数
// ============================================

/// 从 params 提取字符串字段（缺失或非字符串返回 None）
pub fn get_str_param<'a>(params: &'a Value, key: &str) -> Option<&'a str> {
    params.get(key).and_then(|v| v.as_str())
}

/// 从 params 提取必填字符串字段（缺失返回 invalid_params 错误）
pub fn require_str_param<'a>(params: &'a Value, key: &str) -> Result<&'a str, DaemonRpcError> {
    get_str_param(params, key)
        .ok_or_else(|| DaemonRpcError::invalid_params(format!("缺少字段: {}", key)))
}

/// 从 params 提取可选字符串字段（缺失返回默认值）
pub fn get_str_param_or(params: &Value, key: &str, default: &str) -> String {
    get_str_param(params, key).unwrap_or(default).to_string()
}

/// 从 params 提取整数字段（缺失或非整数返回 None）
///
/// G12 批次8（2026-07-21）：修复 query 字段错配——
/// Python daemon_client.py 传 int 类型（如 `"limit": 50`），
/// 原 Rust daemon 用 `get_str_param + parse` 只接受字符串，数字被忽略。
/// 本函数支持 JSON 数字（i64/u64）和字符串两种形式，兼容旧客户端。
pub fn get_int_param(params: &Value, key: &str) -> Option<i64> {
    let v = params.get(key)?;
    // 优先 JSON 数字（Python client 默认传 int）
    if let Some(n) = v.as_i64() {
        return Some(n);
    }
    // 兼容字符串形式（旧客户端或 curl 手动调用）
    if let Some(s) = v.as_str() {
        return s.parse::<i64>().ok();
    }
    None
}

/// 从 params 提取可选整数字段（缺失或非整数返回默认值）
///
/// 与 `get_int_param` 配套，提供默认值回退。
pub fn get_int_param_or(params: &Value, key: &str, default: i64) -> i64 {
    get_int_param(params, key).unwrap_or(default)
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn make_peer() -> PeerCredential {
        // P1-1 修复：返回明确非 admin 的 uid（既非 0 也非 current_daemon_uid()）
        // 避免与 current_daemon_uid() 碰撞（Windows 上两者都是 1000）
        let non_admin_uid = current_daemon_uid().wrapping_add(1);
        PeerCredential {
            uid: non_admin_uid,
            gid: 1000,
            pid: 12345,
        }
    }

    fn make_state() -> DaemonState {
        DaemonState::default()
    }

    // ---- 基础方法测试 ----

    #[test]
    fn test_ping_returns_ok_with_peer_uid() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({});
        let response = dispatch(&mut state, peer, "ping", &params, &[]);

        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["status"], "ok");
        assert_eq!(
            response["result"]["peer_uid"],
            current_daemon_uid().wrapping_add(1)
        );
        assert_eq!(response["result"]["pid"], state.pid);
    }

    #[test]
    fn test_health_returns_uptime_and_schema_version() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({});
        let response = dispatch(&mut state, peer, "health", &params, &[]);

        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["status"], "ok");
        assert_eq!(
            response["result"]["schema_version"],
            super::super::SCHEMA_VERSION
        );
        assert_eq!(response["result"]["workspace_count"], 0);
        // uptime_seconds 应该 >= 0
        let uptime = response["result"]["uptime_seconds"].as_u64().unwrap();
        assert!(uptime < 5); // 刚启动，应该 < 5 秒
    }

    #[test]
    fn test_schema_version_returns_version() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({});
        let response = dispatch(&mut state, peer, "schema.version", &params, &[]);

        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["version"], super::super::SCHEMA_VERSION);
    }

    // ---- 未知方法测试 ----

    #[test]
    fn test_unknown_method_returns_method_not_found() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({});
        let response = dispatch(&mut state, peer, "nonexistent.method", &params, &[]);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "method_not_found");
        assert!(response["error"]["message"]
            .as_str()
            .unwrap()
            .contains("nonexistent.method"));
    }

    // ---- 高级方法（默认返回 method_not_found）----

    #[test]
    fn test_workspace_register_default_unimplemented() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({"client_view_root": "/tmp/test"});
        let response = dispatch(&mut state, peer, "workspace.register", &params, &[]);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "method_not_found");
    }

    #[test]
    fn test_snapshot_publish_default_unimplemented() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({"workspace_instance_id": "123"});
        let response = dispatch(&mut state, peer, "snapshot.publish", &params, &[0]);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "method_not_found");
    }

    #[test]
    fn test_query_stats_default_unimplemented() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({"workspace_instance_id": "123"});
        let response = dispatch(&mut state, peer, "query.stats", &params, &[]);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "method_not_found");
    }

    // ---- 参数解析工具测试 ----

    #[test]
    fn test_get_str_param_present() {
        let params = json!({"key": "value"});
        assert_eq!(get_str_param(&params, "key"), Some("value"));
    }

    #[test]
    fn test_get_str_param_missing() {
        let params = json!({"other": "value"});
        assert_eq!(get_str_param(&params, "key"), None);
    }

    #[test]
    fn test_get_str_param_non_string() {
        let params = json!({"key": 123});
        assert_eq!(get_str_param(&params, "key"), None);
    }

    #[test]
    fn test_require_str_param_present() {
        let params = json!({"key": "value"});
        assert_eq!(require_str_param(&params, "key").unwrap(), "value");
    }

    #[test]
    fn test_require_str_param_missing_returns_invalid_params() {
        let params = json!({});
        let result = require_str_param(&params, "key");
        match result {
            Err(e) => {
                assert_eq!(e.code, "invalid_params");
                assert!(e.message.contains("key"));
            }
            _ => panic!("期望 invalid_params 错误"),
        }
    }

    #[test]
    fn test_get_str_param_or_present() {
        let params = json!({"key": "value"});
        assert_eq!(get_str_param_or(&params, "key", "default"), "value");
    }

    #[test]
    fn test_get_str_param_or_missing_returns_default() {
        let params = json!({});
        assert_eq!(get_str_param_or(&params, "key", "default"), "default");
    }

    // ---- get_int_param / get_int_param_or 测试（G12 批次8）----

    #[test]
    fn test_get_int_param_json_number() {
        // Python client 默认传 JSON 数字
        let params = json!({"limit": 50, "max_depth": 5});
        assert_eq!(get_int_param(&params, "limit"), Some(50));
        assert_eq!(get_int_param(&params, "max_depth"), Some(5));
    }

    #[test]
    fn test_get_int_param_string_form() {
        // 旧客户端或 curl 手动调用传字符串
        let params = json!({"limit": "50", "max_depth": "5"});
        assert_eq!(get_int_param(&params, "limit"), Some(50));
        assert_eq!(get_int_param(&params, "max_depth"), Some(5));
    }

    #[test]
    fn test_get_int_param_missing_returns_none() {
        let params = json!({});
        assert_eq!(get_int_param(&params, "limit"), None);
    }

    #[test]
    fn test_get_int_param_non_numeric_string_returns_none() {
        let params = json!({"limit": "abc"});
        assert_eq!(get_int_param(&params, "limit"), None);
    }

    #[test]
    fn test_get_int_param_or_json_number() {
        let params = json!({"limit": 50});
        assert_eq!(get_int_param_or(&params, "limit", 20), 50);
    }

    #[test]
    fn test_get_int_param_or_missing_returns_default() {
        let params = json!({});
        assert_eq!(get_int_param_or(&params, "limit", 20), 20);
    }

    #[test]
    fn test_get_int_param_or_non_numeric_returns_default() {
        let params = json!({"limit": "abc"});
        assert_eq!(get_int_param_or(&params, "limit", 20), 20);
    }

    // ---- DaemonRpcError 构造器测试 ----

    #[test]
    fn test_daemon_rpc_error_invalid_params() {
        let err = DaemonRpcError::invalid_params("missing field");
        assert_eq!(err.code, "invalid_params");
        assert_eq!(err.message, "missing field");
    }

    #[test]
    fn test_daemon_rpc_error_method_not_found() {
        let err = DaemonRpcError::method_not_found("unknown.method");
        assert_eq!(err.code, "method_not_found");
        assert!(err.message.contains("unknown.method"));
    }

    #[test]
    fn test_daemon_rpc_error_internal_error() {
        let err = DaemonRpcError::internal_error("panic");
        assert_eq!(err.code, "internal_error");
        assert_eq!(err.message, "panic");
    }

    #[test]
    fn test_daemon_rpc_error_permission_denied() {
        let err = DaemonRpcError::permission_denied("not owner");
        assert_eq!(err.code, "permission_denied");
        assert_eq!(err.message, "not owner");
    }

    #[test]
    fn test_daemon_rpc_error_workspace_not_found() {
        let err = DaemonRpcError::workspace_not_found("ws_123");
        assert_eq!(err.code, "workspace_not_found");
        assert_eq!(err.message, "ws_123");
    }

    #[test]
    fn test_daemon_rpc_error_workspace_forbidden() {
        let err = DaemonRpcError::workspace_forbidden("not your ws");
        assert_eq!(err.code, "workspace_forbidden");
        assert_eq!(err.message, "not your ws");
    }

    #[test]
    fn test_daemon_rpc_error_workspace_archived() {
        let err = DaemonRpcError::workspace_archived("ws_456");
        assert_eq!(err.code, "workspace_archived");
        assert_eq!(err.message, "ws_456");
    }

    #[test]
    fn test_daemon_rpc_error_display() {
        let err = DaemonRpcError::new("custom_code", "custom message");
        let s = format!("{}", err);
        assert_eq!(s, "custom_code: custom message");
    }

    // ---- PeerCredential 测试 ----

    fn make_root_peer() -> PeerCredential {
        PeerCredential {
            uid: 0,
            gid: 0,
            pid: 1,
        }
    }

    /// 非管理员 peer 调用 admin-only 方法应返回 permission_denied
    #[test]
    fn test_admin_only_method_denied_for_non_admin() {
        let mut state = make_state();
        let peer = make_peer(); // uid=1000，非 root 且非 daemon 自己
        let params = json!({"output_path": "/tmp/x.db"});

        // backup 是 admin-only 方法
        let response = dispatch(&mut state, peer, "backup", &params, &[]);
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "permission_denied");

        // 验证多个 admin-only 方法都被拒绝
        for method in &[
            "restore",
            "gc.cas",
            "gc.snapshots",
            "snapshot.evict",
            "mount.register",
            "mount.delete",
            "toolchain.register",
            "toolchain.delete",
            "toolchain.bind",
        ] {
            let response = dispatch(&mut state, make_peer(), method, &params, &[]);
            assert_eq!(
                response["error"]["code"], "permission_denied",
                "方法 {} 应被 permission_denied 拒绝",
                method
            );
        }
    }

    /// root (uid=0) 调用 admin-only 方法应通过授权检查（进入 handler 后由 handler 返回结果）
    #[test]
    fn test_admin_only_method_allowed_for_root() {
        let mut state = make_state();
        let peer = make_root_peer();
        let params = json!({});

        // backup 通过授权检查后，默认 DaemonState 的 handle_backup 返回 method_not_found
        // 这里只验证授权检查未拒绝（不是 permission_denied）
        let response = dispatch(&mut state, peer, "backup", &params, &[]);
        assert_ne!(
            response["error"]["code"], "permission_denied",
            "root 调用 backup 不应被 permission_denied"
        );
    }

    /// 只读方法（list/get/query/stats）不应被授权检查拦截
    #[test]
    fn test_readonly_methods_not_blocked_by_admin_check() {
        let mut state = make_state();
        let peer = make_peer(); // 非管理员

        // 只读方法应正常路由（不会被 permission_denied 拦截）
        // 注意：mount.list 自 P0-2 整改起改为 admin-only（暴露全局 host_path），
        // 不再属于只读方法集，已从此列表移除。
        for method in &[
            "workspace.list",
            "workspace.status",
            "toolchain.list",
            "toolchain.get",
            "toolchain.list_bound",
            "build_context.list",
            "build_context.get",
            "build_context.register",
            "build_context.set_active",
            "build_context.delete",
            "resolved_edges.replace",
            "query.stats",
            "query.symbol",
            "snapshot.stats",
            "snapshot.list_workspaces",
        ] {
            let params = json!({});
            let response = dispatch(&mut state, peer, method, &params, &[]);
            assert_ne!(
                response["error"]["code"], "permission_denied",
                "只读方法 {} 不应被 permission_denied 拦截",
                method
            );
        }
    }

    #[test]
    fn test_peer_credential_clone_copy() {
        let peer1 = PeerCredential {
            uid: 100,
            gid: 200,
            pid: 300,
        };
        let peer2 = peer1; // Copy
        assert_eq!(peer1.uid, peer2.uid);
        assert_eq!(peer1.gid, peer2.gid);
        assert_eq!(peer1.pid, peer2.pid);
    }

    // ---- DaemonState 默认值测试 ----

    #[test]
    fn test_daemon_state_default_pid_nonzero() {
        let state = DaemonState::default();
        assert!(state.pid > 0); // 进程 ID 应该 > 0
    }

    #[test]
    fn test_daemon_state_default_schema_version() {
        let state = DaemonState::default();
        assert_eq!(state.schema_version, super::super::SCHEMA_VERSION);
    }

    // ---- 完整 dispatch 链路测试（验证路由分发正确）----

    #[test]
    fn test_dispatch_ping_routes_correctly() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({});
        let response = dispatch(&mut state, peer, "ping", &params, &[]);

        // 验证路由到 handle_ping（而非其他 handler）
        assert_eq!(response["ok"], true);
        assert_eq!(
            response["result"]["peer_uid"],
            current_daemon_uid().wrapping_add(1)
        );
    }

    #[test]
    fn test_dispatch_all_known_methods_route_without_panic() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({});

        // 所有已知方法都应该路由成功（不 panic），即使是默认 method_not_found
        let methods = vec![
            "ping",
            "health",
            "schema.version",
            "workspace.register",
            "workspace.list",
            "workspace.status",
            "workspace.activate",
            "workspace.remove",
            "workspace.connect",
            "workspace.refresh.plan",
            "workspace.file.refresh",
            "workspace.file.delete",
            "workspace.recover",
            "snapshot.publish",
            "gc.snapshots",
            "gc.cas",
            "query.stats",
            "query.symbol",
            "query.search",
            "query.callers",
            "query.callees",
            "backup",
            "restore",
        ];

        for method in methods {
            let response = dispatch(&mut state, peer, method, &params, &[]);
            // 所有方法都应该返回有效的 JSON-RPC 响应（ok=true 或 ok=false）
            assert!(
                response.get("ok").is_some(),
                "方法 {} 的响应缺少 ok 字段",
                method
            );
        }
    }

    /// 自定义 DaemonState mock，用于测试 DaemonStateExt trait 扩展机制
    struct MockState {
        base: DaemonState,
        workspace_count: u32,
    }

    impl DaemonStateExt for MockState {
        fn daemon_state(&self) -> &DaemonState {
            &self.base
        }

        fn handle_workspace_list(
            &mut self,
            _peer: PeerCredential,
            _params: &Value,
        ) -> Result<Value, DaemonRpcError> {
            let mut m = Map::new();
            m.insert(
                "count".to_string(),
                Value::Number(self.workspace_count.into()),
            );
            Ok(Value::Object(m))
        }
    }

    #[test]
    fn test_daemon_state_ext_trait_extension_works() {
        let mut state = MockState {
            base: DaemonState::default(),
            workspace_count: 5,
        };
        let peer = make_peer();
        let params = json!({});

        // workspace.list 在 MockState 中被重写，返回自定义数据
        let response = dispatch(&mut state, peer, "workspace.list", &params, &[]);
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["count"], 5);

        // ping 走基础 handler（DaemonState 默认实现）
        let response = dispatch(&mut state, peer, "ping", &params, &[]);
        assert_eq!(response["ok"], true);
        assert_eq!(
            response["result"]["peer_uid"],
            current_daemon_uid().wrapping_add(1)
        );
    }
}
