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

use std::time::Instant;
use serde_json::{Map, Value};
use super::protocol::{make_error_response, make_ok_response};

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
        m.insert(
            "uptime_seconds".to_string(),
            Value::Number(uptime.into()),
        );
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

/// dispatch 内部实现（返回 Result<Value, DaemonRpcError>）
fn dispatch_inner<S: DaemonStateExt>(
    state: &mut S,
    peer: PeerCredential,
    method: &str,
    params: &Value,
    received_fds: &[i32],
) -> Result<Value, DaemonRpcError> {
    match method {
        // ---- 基础方法（R3 默认实现）----
        "ping" => state.handle_ping(peer),
        "health" => state.handle_health(peer),
        "schema.version" => state.handle_schema_version(peer),

        // ---- Workspace 管理（R4 实现）----
        "workspace.register" => state.handle_workspace_register(peer, params),
        "workspace.list" => state.handle_workspace_list(peer, params),
        "workspace.status" => state.handle_workspace_status(peer, params),
        "workspace.connect" => state.handle_workspace_connect(peer, params),
        "workspace.file.refresh" => state.handle_workspace_file_refresh(peer, params, received_fds),
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
pub fn require_str_param<'a>(
    params: &'a Value,
    key: &str,
) -> Result<&'a str, DaemonRpcError> {
    get_str_param(params, key)
        .ok_or_else(|| DaemonRpcError::invalid_params(format!("缺少字段: {}", key)))
}

/// 从 params 提取可选字符串字段（缺失返回默认值）
pub fn get_str_param_or(params: &Value, key: &str, default: &str) -> String {
    get_str_param(params, key)
        .unwrap_or(default)
        .to_string()
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn make_peer() -> PeerCredential {
        PeerCredential {
            uid: 1000,
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
        assert_eq!(response["result"]["peer_uid"], 1000);
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
        assert_eq!(
            response["result"]["version"],
            super::super::SCHEMA_VERSION
        );
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
        assert_eq!(response["result"]["peer_uid"], 1000);
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
            "workspace.connect",
            "workspace.file.refresh",
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
        assert_eq!(response["result"]["peer_uid"], 1000);
    }
}
