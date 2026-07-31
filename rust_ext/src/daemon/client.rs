//! Daemon RPC Client（Phase 5-2 Slice 1）
//!
//! 对齐 Python `server/daemon_client.py:UnixDaemonRpcClient`：
//! - 跨平台协议层（build_request / parse_rpc_response）：纯逻辑，Windows 可测
//! - Unix UDS Client（UnixDaemonRpcClient）：`#[cfg(unix)]`，Linux 端到端可用
//!
//! 每次调用建立新 UDS 连接（无状态），请求完成后关闭。
//!
//! 契约：docs/design/phase5-2-slice1-daemon-client-contract.md §3

use pyo3::prelude::*;
use serde_json::{Map, Value};

use super::protocol::{parse_response, DaemonRemoteError, DEFAULT_MAX_MESSAGE_BYTES};

// ============================================
// 跨平台协议层（Windows 可测）
// ============================================

/// 构建 RPC 请求 JSON。
///
/// 对齐 Python `{"method": "...", "params": {...}}`
///
/// 参数：
/// - `method`: RPC 方法名（如 "ping"）
/// - `params`: 参数 JSON（已解析的 serde_json::Value，通常是 Object）
///
/// 返回：`{"method": "...", "params": {...}}`
pub fn build_request(method: &str, params: Value) -> Value {
    let mut m = Map::new();
    m.insert("method".to_string(), Value::String(method.to_string()));
    m.insert("params".to_string(), params);
    Value::Object(m)
}

/// 解析 RPC 响应，远端错误转换为 DaemonRemoteError。
///
/// 对齐 Python `parse_response(response)`。
/// 复用 `rust_ext/src/daemon/protocol.rs:parse_response`。
pub fn parse_rpc_response(response: &Value) -> Result<Value, DaemonRemoteError> {
    parse_response(response)
}

// ============================================
// Client 错误类型
// ============================================

/// Client 错误类型。
///
/// 对齐 Python 的 `ConnectionRefusedError` / `socket.timeout` / `DaemonError`。
#[derive(Debug, thiserror::Error)]
pub enum ClientError {
    #[error("UDS 连接失败 (path={path}): {source}")]
    ConnectFailed {
        path: String,
        source: std::io::Error,
    },
    #[error("设置超时失败: {0}")]
    SetTimeout(std::io::Error),
    #[error("协议错误: {0}")]
    Protocol(#[from] super::protocol::ProtocolError),
    #[error("远端错误: {0}")]
    Remote(#[from] super::protocol::DaemonRemoteError),
}

// ============================================
// Unix UDS Client（仅 Unix 编译）
// ============================================

#[cfg(unix)]
pub mod unix {
    use super::*;
    use crate::daemon::protocol::{recv_message, send_message};
    use std::os::unix::net::UnixStream;
    use std::time::Duration;

    /// Rust UDS RPC Client。
    ///
    /// 对齐 Python `server/daemon_client.py:UnixDaemonRpcClient`
    ///
    /// 每次调用建立新连接（无状态），请求完成后关闭。
    #[derive(Debug, Clone)]
    pub struct UnixDaemonRpcClient {
        pub socket_path: String,
        pub timeout: Duration,
        pub max_message_bytes: usize,
    }

    impl UnixDaemonRpcClient {
        /// 创建新 client，默认超时 30 秒，最大消息 8MB。
        pub fn new(socket_path: &str) -> Self {
            Self {
                socket_path: socket_path.to_string(),
                timeout: Duration::from_secs(30),
                max_message_bytes: DEFAULT_MAX_MESSAGE_BYTES,
            }
        }

        /// 设置超时（builder 模式）。
        pub fn with_timeout(mut self, timeout: Duration) -> Self {
            self.timeout = timeout;
            self
        }

        /// 设置最大消息字节数（builder 模式）。
        pub fn with_max_message_bytes(mut self, max_bytes: usize) -> Self {
            self.max_message_bytes = max_bytes;
            self
        }

        /// 调用 RPC 方法。
        ///
        /// 对齐 Python `UnixDaemonRpcClient.call(method, params)`
        ///
        /// 流程：
        /// 1. 建立 UDS 连接
        /// 2. 设置超时
        /// 3. 发送请求（build_request + send_message）
        /// 4. 接收响应（recv_message）
        /// 5. 解析响应（parse_response）
        /// 6. 关闭连接（UnixStream Drop 自动关闭）
        pub fn call(&self, method: &str, params: Value) -> Result<Value, ClientError> {
            // 1. 建立 UDS 连接
            let mut stream = UnixStream::connect(&self.socket_path).map_err(|e| {
                ClientError::ConnectFailed {
                    path: self.socket_path.clone(),
                    source: e,
                }
            })?;

            // 2. 设置超时
            stream
                .set_read_timeout(Some(self.timeout))
                .map_err(ClientError::SetTimeout)?;
            stream
                .set_write_timeout(Some(self.timeout))
                .map_err(ClientError::SetTimeout)?;

            // 3. 发送请求
            let request = build_request(method, params);
            send_message(&mut stream, &request, self.max_message_bytes)
                .map_err(ClientError::Protocol)?;

            // 4. 接收响应
            let response = recv_message(&mut stream, self.max_message_bytes)
                .map_err(ClientError::Protocol)?;

            // 5. 解析响应
            let result = parse_rpc_response(&response).map_err(ClientError::Remote)?;

            // 6. 连接自动关闭（UnixStream Drop）
            Ok(result)
        }

        /// 便捷方法：ping daemon。
        ///
        /// 对齐 Python `client.call("ping", {})`
        pub fn ping(&self) -> Result<Value, ClientError> {
            self.call("ping", Value::Object(Map::new()))
        }

        /// 调用带 FD 的 RPC 方法（SCM_RIGHTS 传递）。
        ///
        /// 对齐 Python `UnixDaemonRpcClient.call_with_fd(method, params, fd)`
        ///
        /// 流程：
        /// 1. 建立 UDS 连接
        /// 2. 设置超时
        /// 3. send_message_with_fds 发送请求 + FD
        /// 4. recv_message 接收响应
        /// 5. parse_response 解析响应
        pub fn call_with_fd(
            &self,
            method: &str,
            params: Value,
            fd: std::os::unix::io::RawFd,
        ) -> Result<Value, ClientError> {
            use crate::daemon::protocol::send_message_with_fds;

            // 1. 建立 UDS 连接
            let mut stream = UnixStream::connect(&self.socket_path).map_err(|e| {
                ClientError::ConnectFailed {
                    path: self.socket_path.clone(),
                    source: e,
                }
            })?;

            // 2. 设置超时
            stream
                .set_read_timeout(Some(self.timeout))
                .map_err(ClientError::SetTimeout)?;
            stream
                .set_write_timeout(Some(self.timeout))
                .map_err(ClientError::SetTimeout)?;

            // 3. 构建 RPC 请求（包含 id 字段，对齐 Python）
            let mut request = Map::new();
            request.insert("method".to_string(), Value::String(method.to_string()));
            request.insert("params".to_string(), params);
            request.insert(
                "id".to_string(),
                Value::Number(serde_json::Number::from(1)),
            );

            // 4. send_message_with_fds 发送请求 + FD
            send_message_with_fds(
                &mut stream,
                &Value::Object(request),
                &[fd],
                self.max_message_bytes,
            )
            .map_err(ClientError::Protocol)?;

            // 5. 接收响应
            let response = recv_message(&mut stream, self.max_message_bytes)
                .map_err(ClientError::Protocol)?;

            // 6. 解析响应
            let result = parse_rpc_response(&response).map_err(ClientError::Remote)?;

            Ok(result)
        }

        /// 发布 snapshot 给 daemon（含 WAL checkpoint + FD 传递）。
        ///
        /// 对齐 Python `UnixDaemonRpcClient.publish_snapshot(workspace_instance_id, db_path, build_context_hash)`
        ///
        /// 流程：
        /// 1. 打开 db_path（O_RDONLY）
        /// 2. RPC 调用 `snapshot.publish` 方法，通过 SCM_RIGHTS 传递 db 文件 FD
        /// 3. 关闭 FD（finally 语义）
        ///
        /// **注意**：此方法不做 WAL checkpoint。Python 端在调用前做 checkpoint，
        /// Rust 端将其拆分为独立的 `wal_checkpoint` 函数，由调用方按需执行。
        pub fn publish_snapshot(
            &self,
            workspace_instance_id: &str,
            db_path: &str,
            build_context_hash: &str,
        ) -> Result<Value, ClientError> {
            use std::os::unix::io::RawFd;

            // 1. 打开 db_path（O_RDONLY）
            // arm64 Linux 上 c_char 是 u8，x86_64 上是 i8；用 libc::c_char 兼容
            let fd: RawFd = unsafe { libc::open(db_path.as_ptr() as *const libc::c_char, libc::O_RDONLY) };
            if fd < 0 {
                return Err(ClientError::ConnectFailed {
                    path: db_path.to_string(),
                    source: std::io::Error::last_os_error(),
                });
            }

            // 2. 构建 RPC 参数
            let mut params = Map::new();
            params.insert(
                "workspace_instance_id".to_string(),
                Value::String(workspace_instance_id.to_string()),
            );
            params.insert(
                "build_context_hash".to_string(),
                Value::String(build_context_hash.to_string()),
            );

            // 3. call_with_fd 发送请求 + FD（finally 语义关闭 fd）
            let result = self.call_with_fd("snapshot.publish", Value::Object(params), fd);

            // 4. 关闭 FD（finally 语义）
            unsafe { libc::close(fd) };

            result
        }
    }
}

// ============================================
// PyO3 暴露
// ============================================

/// Python 暴露的 build_request。
///
/// 返回 JSON 字符串，便于 Python 端验证请求格式。
///
/// 对齐 Python `{"method": "...", "params": {...}}`
#[pyfunction]
pub fn build_request_py(method: &str, params_json: &str) -> String {
    let params: Value = serde_json::from_str(params_json).unwrap_or(Value::Null);
    let request = build_request(method, params);
    serde_json::to_string(&request).unwrap_or_default()
}

/// Python 暴露的 parse_rpc_response。
///
/// 返回 `(ok, result_or_error_json)` 元组：
/// - `ok=true`: `result_or_error_json` 是 result 的 JSON 字符串
/// - `ok=false`: `result_or_error_json` 是 error 的 JSON 字符串 `{"code":"...","message":"..."}`
///
/// 对齐 Python `parse_response(response)`
#[pyfunction]
pub fn parse_rpc_response_py(response_json: &str) -> (bool, String) {
    let response: Value = match serde_json::from_str(response_json) {
        Ok(v) => v,
        Err(_) => {
            return (
                false,
                r#"{"code":"parse_error","message":"invalid JSON"}"#.to_string(),
            )
        }
    };
    match parse_rpc_response(&response) {
        Ok(result) => (
            true,
            serde_json::to_string(&result).unwrap_or_default(),
        ),
        Err(e) => (
            false,
            serde_json::to_string(&serde_json::json!({
                "code": e.code,
                "message": e.message
            }))
            .unwrap_or_default(),
        ),
    }
}

/// Python 暴露的 daemon_client_call（仅 Unix 编译）。
///
/// 对齐 Python `UnixDaemonRpcClient.call(method, params)`。
///
/// 参数：
/// - `socket_path`: daemon UDS socket 路径
/// - `method`: RPC 方法名
/// - `params_json`: 参数 JSON 字符串
/// - `timeout_secs`: 可选超时（秒），None 用默认 30 秒
///
/// 返回 `(exit_code, result_json, stderr)`：
/// - `exit_code=0`: `result_json` 包含 RPC 结果
/// - `exit_code=1`: `stderr` 包含错误信息
///
/// 契约：docs/design/phase5-2-slice1-daemon-client-contract.md §3.3
#[cfg(unix)]
#[pyfunction]
pub fn daemon_client_call_py(
    socket_path: &str,
    method: &str,
    params_json: &str,
    timeout_secs: Option<u64>,
) -> (i32, String, String) {
    use std::time::Duration;

    let params: Value = serde_json::from_str(params_json)
        .unwrap_or_else(|_| Value::Object(Map::new()));
    let mut client = unix::UnixDaemonRpcClient::new(socket_path);
    if let Some(secs) = timeout_secs {
        client = client.with_timeout(Duration::from_secs(secs));
    }
    match client.call(method, params) {
        Ok(result) => (
            0,
            serde_json::to_string(&result).unwrap_or_default(),
            String::new(),
        ),
        Err(e) => (1, String::new(), format!("{}", e)),
    }
}

// ============================================
// Phase 5-2 Slice 2: query RPC 参数构建（跨平台，纯逻辑）
// ============================================

/// 支持的 query 类型
pub const QUERY_TYPES: &[&str] = &[
    "stats",
    "symbol",
    "search",
    "callers",
    "callees",
    "call_chain_down",
    "impact",
    "topological_order",
    "detect_cycles",
];

/// query 参数构建错误
#[derive(Debug, thiserror::Error)]
pub enum QueryError {
    #[error("不支持的 query 类型: {0}")]
    UnknownQueryType(String),
}

/// 构建 query RPC 请求参数。
///
/// 对齐 Python `cli/daemon_commands.py:run_daemon_command` 的 query 分支 (L574-592)。
///
/// 参数：
/// - `workspace_id`: workspace instance ID
/// - `query_type`: 查询类型（stats/symbol/search/callers/callees/...）
/// - `value`: 主查询值（symbol 的 qualified_name / search 的 query / callers 的 callee_name 等）
/// - `qualified_name`: 可选，callers/callees 的限定名过滤
/// - `kind`: 可选，search 的符号类型过滤
/// - `limit`: 可选，search/topological_order 的结果限制（默认 20）
/// - `max_depth`: 可选，call_chain_down/detect_cycles 的最大深度（默认 10）
///
/// 返回 `(method, params)` 元组：
/// - `method`: `"query.{query_type}"`
/// - `params`: RPC 参数 JSON
pub fn build_query_request(
    workspace_id: &str,
    query_type: &str,
    value: &str,
    qualified_name: Option<&str>,
    kind: Option<&str>,
    limit: Option<u32>,
    max_depth: Option<u32>,
) -> Result<(String, Value), QueryError> {
    if !QUERY_TYPES.contains(&query_type) {
        return Err(QueryError::UnknownQueryType(query_type.to_string()));
    }

    let method = format!("query.{}", query_type);
    let mut params = Map::new();
    params.insert(
        "workspace_instance_id".to_string(),
        Value::String(workspace_id.to_string()),
    );

    match query_type {
        "stats" => {
            // 无额外参数，只有 workspace_instance_id
        }
        "symbol" => {
            params.insert(
                "qualified_name".to_string(),
                Value::String(value.to_string()),
            );
        }
        "search" => {
            params.insert("query".to_string(), Value::String(value.to_string()));
            if let Some(k) = kind {
                if !k.is_empty() {
                    params.insert("kind".to_string(), Value::String(k.to_string()));
                }
            }
            params.insert(
                "limit".to_string(),
                Value::Number(serde_json::Number::from(limit.unwrap_or(20))),
            );
        }
        "callers" => {
            params.insert(
                "callee_name".to_string(),
                Value::String(value.to_string()),
            );
            if let Some(qn) = qualified_name {
                if !qn.is_empty() {
                    params.insert(
                        "qualified_name".to_string(),
                        Value::String(qn.to_string()),
                    );
                }
            }
        }
        "callees" => {
            params.insert(
                "caller_name".to_string(),
                Value::String(value.to_string()),
            );
            if let Some(qn) = qualified_name {
                if !qn.is_empty() {
                    params.insert(
                        "qualified_name".to_string(),
                        Value::String(qn.to_string()),
                    );
                }
            }
        }
        "call_chain_down" => {
            params.insert(
                "qualified_name".to_string(),
                Value::String(value.to_string()),
            );
            params.insert(
                "max_depth".to_string(),
                Value::Number(serde_json::Number::from(max_depth.unwrap_or(10))),
            );
        }
        "impact" => {
            params.insert(
                "symbol_hash".to_string(),
                Value::String(value.to_string()),
            );
            params.insert(
                "depth".to_string(),
                Value::Number(serde_json::Number::from(max_depth.unwrap_or(3))),
            );
        }
        "topological_order" => {
            params.insert(
                "limit".to_string(),
                Value::Number(serde_json::Number::from(limit.unwrap_or(20))),
            );
        }
        "detect_cycles" => {
            params.insert(
                "max_depth".to_string(),
                Value::Number(serde_json::Number::from(max_depth.unwrap_or(10))),
            );
        }
        _ => unreachable!(),
    }

    Ok((method, Value::Object(params)))
}

/// Python 暴露的 build_query_request。
///
/// 返回 `(method, params_json)` 元组：
/// - `method`: RPC 方法名（如 "query.stats"）
/// - `params_json`: 参数 JSON 字符串
///
/// 错误时返回 `("ERROR", error_message)`。
#[pyfunction]
pub fn build_query_request_py(
    workspace_id: &str,
    query_type: &str,
    value: &str,
    qualified_name: Option<&str>,
    kind: Option<&str>,
    limit: Option<u32>,
    max_depth: Option<u32>,
) -> (String, String) {
    match build_query_request(
        workspace_id,
        query_type,
        value,
        qualified_name,
        kind,
        limit,
        max_depth,
    ) {
        Ok((method, params)) => (
            method,
            serde_json::to_string(&params).unwrap_or_default(),
        ),
        Err(e) => ("ERROR".to_string(), format!("{}", e)),
    }
}

// ============================================
// Phase 5-2 Slice 3: 简单 RPC 命令参数构建（跨平台，纯逻辑）
// ============================================

/// 支持的简单 action（无嵌套子命令，参数简单）
/// 对齐 cli/daemon_commands.py 的 list/status/health/schema-version 分支
pub const SIMPLE_ACTIONS: &[&str] = &["list", "status", "health", "schema-version"];

/// 简单命令参数构建错误
#[derive(Debug, thiserror::Error)]
pub enum SimpleError {
    #[error("不支持的 action: {0}")]
    UnknownAction(String),
    #[error("status 命令需要 workspace_id 参数")]
    MissingWorkspaceId,
}

/// 构建 4 个简单 RPC 命令的请求参数。
///
/// 对齐 Python `cli/daemon_commands.py:run_daemon_command` 的简单命令分支：
/// - `list` → `workspace.list`（无 params，Python `client.call("workspace.list")` 内部转 `{}`）
/// - `status` → `workspace.status`（params: `{"workspace_instance_id": workspace_id}`）
/// - `health` → `health`（params: `{}`）
/// - `schema-version` → `schema.version`（params: `{}`）
///
/// 参数：
/// - `action`: 命令名（list/status/health/schema-version）
/// - `workspace_id`: 可选，仅 status 需要
///
/// 返回 `(method, params)` 元组：
/// - `method`: RPC 方法名（如 "workspace.list"）
/// - `params`: RPC 参数 JSON（至少是 `{}`）
pub fn build_simple_request(
    action: &str,
    workspace_id: Option<&str>,
) -> Result<(String, Value), SimpleError> {
    if !SIMPLE_ACTIONS.contains(&action) {
        return Err(SimpleError::UnknownAction(action.to_string()));
    }

    let (method, params) = match action {
        "list" => ("workspace.list".to_string(), Value::Object(Map::new())),
        "status" => {
            let ws = workspace_id.ok_or(SimpleError::MissingWorkspaceId)?;
            let mut p = Map::new();
            p.insert(
                "workspace_instance_id".to_string(),
                Value::String(ws.to_string()),
            );
            ("workspace.status".to_string(), Value::Object(p))
        }
        "health" => ("health".to_string(), Value::Object(Map::new())),
        "schema-version" => ("schema.version".to_string(), Value::Object(Map::new())),
        _ => unreachable!(),
    };

    Ok((method, params))
}

/// Python 暴露的 build_simple_request。
///
/// 返回 `(method, params_json)` 元组：
/// - `method`: RPC 方法名（如 "workspace.list"）
/// - `params_json`: 参数 JSON 字符串
///
/// 错误时返回 `("ERROR", error_message)`。
#[pyfunction]
pub fn build_simple_request_py(action: &str, workspace_id: Option<&str>) -> (String, String) {
    match build_simple_request(action, workspace_id) {
        Ok((method, params)) => (
            method,
            serde_json::to_string(&params).unwrap_or_default(),
        ),
        Err(e) => ("ERROR".to_string(), format!("{}", e)),
    }
}

// ============================================
// Phase 5-2 Slice 5: 剩余 RPC 命令参数构建（跨平台，纯逻辑）
// ============================================

/// 支持的 RPC 命令（register/backup/restore/gc/snapshot/mount）
/// 对齐 cli/daemon_commands.py 的对应分支
pub const RPC_ACTIONS: &[&str] = &[
    "register",
    "activate",
    "remove",
    "backup",
    "restore",
    "gc-cas",
    "gc-snapshots",
    "snapshot-stats",
    "snapshot-list",
    "snapshot-evict",
    "mount-register",
    "mount-list",
    "mount-delete",
];

/// RPC 命令参数构建错误
#[derive(Debug, thiserror::Error)]
pub enum RpcError {
    #[error("不支持的 RPC action: {0}")]
    UnknownAction(String),
    #[error("缺少必需参数: {0}")]
    MissingParam(&'static str),
}

/// 构建 workspace 生命周期及其余 11 个 RPC 命令的请求参数。
///
/// 对齐 Python `cli/daemon_commands.py:run_daemon_command` 的对应分支：
/// - `register` → `workspace.register`（4 参数）
/// - `backup` → `backup`（1 abspath 参数）
/// - `restore` → `restore`（1 abspath 参数）
/// - `gc-cas` → `gc.cas`（2 参数）
/// - `gc-snapshots` → `gc.snapshots`（1 参数）
/// - `snapshot-stats` → `snapshot.stats`（无参数）
/// - `snapshot-list` → `snapshot.list_workspaces`（无参数）
/// - `snapshot-evict` → `snapshot.evict`（1 参数）
/// - `mount-register` → `mount.register`（4 参数，host_path 需 abspath）
/// - `mount-list` → `mount.list`（可选 container_id）
/// - `mount-delete` → `mount.delete`（2 参数）
///
/// 参数：
/// - `action`: 命令名（见 RPC_ACTIONS）
/// - `params`: 已序列化的参数 JSON 字符串（由 CLI 层构建）
///
/// 返回 `(method, params)` 元组。
pub fn build_rpc_request(
    action: &str,
    params_json: &str,
) -> Result<(String, Value), RpcError> {
    if !RPC_ACTIONS.contains(&action) {
        return Err(RpcError::UnknownAction(action.to_string()));
    }

    // 解析参数 JSON（CLI 层已构建好参数结构）
    let params: Value = serde_json::from_str(params_json).map_err(|_| {
        RpcError::MissingParam("params_json 解析失败")
    })?;

    let method = match action {
        "register" => "workspace.register",
        "activate" => "workspace.activate",
        "remove" => "workspace.remove",
        "backup" => "backup",
        "restore" => "restore",
        "gc-cas" => "gc.cas",
        "gc-snapshots" => "gc.snapshots",
        "snapshot-stats" => "snapshot.stats",
        "snapshot-list" => "snapshot.list_workspaces",
        "snapshot-evict" => "snapshot.evict",
        "mount-register" => "mount.register",
        "mount-list" => "mount.list",
        "mount-delete" => "mount.delete",
        _ => unreachable!(),
    };

    Ok((method.to_string(), params))
}

/// Python 暴露的 build_rpc_request。
///
/// 返回 `(method, params_json)` 元组，错误时返回 `("ERROR", error_message)`。
#[pyfunction]
pub fn build_rpc_request_py(action: &str, params_json: &str) -> (String, String) {
    match build_rpc_request(action, params_json) {
        Ok((method, params)) => (
            method,
            serde_json::to_string(&params).unwrap_or_default(),
        ),
        Err(e) => ("ERROR".to_string(), format!("{}", e)),
    }
}

/// 构建 snapshot.publish RPC 请求参数（跨平台纯逻辑）。
///
/// 对齐 Python `UnixDaemonRpcClient.publish_snapshot` 的参数构建部分：
/// ```python
/// params = {
///     "workspace_instance_id": workspace_instance_id,
///     "build_context_hash": build_context_hash,
/// }
/// ```
///
/// FD 打开和 SCM_RIGHTS 传递是 Unix-only 副作用，不在本函数中处理。
/// 本函数仅构建 RPC 参数，便于差分测试验证参数结构对齐。
pub fn build_publish_params(
    workspace_instance_id: &str,
    build_context_hash: &str,
) -> (String, Value) {
    let mut params = Map::new();
    params.insert(
        "workspace_instance_id".to_string(),
        Value::String(workspace_instance_id.to_string()),
    );
    params.insert(
        "build_context_hash".to_string(),
        Value::String(build_context_hash.to_string()),
    );
    (
        "snapshot.publish".to_string(),
        Value::Object(params),
    )
}

/// Python 暴露的 build_publish_params。
///
/// 返回 `(method, params_json)` 元组。
#[pyfunction]
pub fn build_publish_params_py(
    workspace_instance_id: &str,
    build_context_hash: &str,
) -> (String, String) {
    let (method, params) = build_publish_params(workspace_instance_id, build_context_hash);
    (
        method,
        serde_json::to_string(&params).unwrap_or_default(),
    )
}

// ============================================
// Phase 5-2 Slice 6: agent session + watcher 参数构建（跨平台，纯逻辑）
// ============================================

/// 构建 workspace.connect RPC 请求参数（agent 握手）。
///
/// 对齐 Python `server/agent_protocol.py:user_agent_connect` 的参数构建 (L121-124)：
/// ```python
/// result = daemon_rpc_client.call("workspace.connect", {
///     "workspace_instance_id": workspace_instance_id,
///     "agent_session_id": agent_session.session_id,
/// })
/// ```
///
/// 参数：
/// - `workspace_instance_id`: workspace 标识符（16 位 hex）
/// - `agent_session_id`: agent session UUID（格式 `agent-{hex[:12]}`）
///
/// 返回 `(method, params)` 元组。
pub fn build_connect_params(
    workspace_instance_id: &str,
    agent_session_id: &str,
) -> (String, Value) {
    let mut params = Map::new();
    params.insert(
        "workspace_instance_id".to_string(),
        Value::String(workspace_instance_id.to_string()),
    );
    params.insert(
        "agent_session_id".to_string(),
        Value::String(agent_session_id.to_string()),
    );
    ("workspace.connect".to_string(), Value::Object(params))
}

/// 构建 workspace.file.refresh RPC 请求参数（文件变更通知）。
///
/// 对齐 Python `server/agent_protocol.py:build_refresh_message` (L200-206)：
/// ```python
/// return {
///     "workspace_instance_id": workspace_instance_id,
///     "rel_path": rel_path,
///     "agent_session_id": agent_session.session_id,
///     "session_epoch": epoch,
///     "monotonic_seq": seq,
/// }
/// ```
///
/// 参数：
/// - `workspace_instance_id`: workspace 标识符
/// - `rel_path`: 文件相对路径（相对于 workspace 根目录）
/// - `agent_session_id`: agent session UUID
/// - `session_epoch`: daemon 分配的 epoch（≥1）
/// - `monotonic_seq`: agent 本地单调递增 seq
///
/// 返回 `(method, params)` 元组。
pub fn build_refresh_params(
    workspace_instance_id: &str,
    rel_path: &str,
    agent_session_id: &str,
    session_epoch: u64,
    monotonic_seq: u64,
) -> (String, Value) {
    let mut params = Map::new();
    params.insert(
        "workspace_instance_id".to_string(),
        Value::String(workspace_instance_id.to_string()),
    );
    params.insert(
        "rel_path".to_string(),
        Value::String(rel_path.to_string()),
    );
    params.insert(
        "agent_session_id".to_string(),
        Value::String(agent_session_id.to_string()),
    );
    params.insert(
        "session_epoch".to_string(),
        Value::Number(serde_json::Number::from(session_epoch)),
    );
    params.insert(
        "monotonic_seq".to_string(),
        Value::Number(serde_json::Number::from(monotonic_seq)),
    );
    (
        "workspace.file.refresh".to_string(),
        Value::Object(params),
    )
}

/// 构建 agent ping RPC 请求参数。
///
/// 对齐 Python `server/agent_protocol.py:user_agent_ping` (L358)：
/// `daemon_rpc_client.call("ping", {})`
pub fn build_agent_ping_params() -> (String, Value) {
    ("ping".to_string(), Value::Object(Map::new()))
}

/// Agent session 状态（跨平台纯逻辑，非线程安全）。
///
/// 对齐 Python `server/agent_session.py:AgentSession` 的核心状态管理：
/// - session_id: agent 唯一标识（格式 `agent-{hex[:12]}`）
/// - per-workspace epoch + seq_counter
///
/// 注意：Python 版用 RLock 保证线程安全，Rust 版用于差分测试和参数构建，
/// 不需要线程安全（生产环境的 watcher 循环是单线程的）。
#[derive(Debug, Clone)]
pub struct AgentSession {
    /// agent session 唯一标识
    pub session_id: String,
    /// per-workspace 状态：workspace_instance_id → (epoch, seq_counter)
    workspaces: std::collections::HashMap<String, WorkspaceState>,
}

/// per-workspace 状态
#[derive(Debug, Clone)]
struct WorkspaceState {
    /// daemon 分配的 epoch（≥1，0 表示未协商）
    epoch: u64,
    /// 本地单调递增 seq（每次 next_seq +1）
    seq_counter: u64,
}

impl AgentSession {
    /// 创建新的 AgentSession（内存中，不持久化）。
    ///
    /// 对齐 Python `AgentSession.create_in_memory(session_id)` (L111)
    pub fn new(session_id: String) -> Self {
        Self {
            session_id,
            workspaces: std::collections::HashMap::new(),
        }
    }

    /// 生成新的 session_id（格式 `agent-{hex[:12]}`）。
    ///
    /// 对齐 Python `f"agent-{uuid4().hex[:12]}"` (L102)
    pub fn generate_session_id() -> String {
        // 简化版：用时间戳 + 进程 ID 生成伪 UUID
        // 生产环境应使用真正的 UUID 库
        // 对齐 Python agent-{uuid4().hex[:12]}：固定 12 hex chars（48 bits）
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let pid = std::process::id();
        // 掩码到 48 位确保 {:012x} 恰好输出 12 字符
        let mixed = ((ts as u64) ^ (pid as u64)) & 0xFFFF_FFFF_FFFF;
        format!("agent-{:012x}", mixed)
    }

    /// 注册 workspace（初始化 epoch=0, seq=0）。
    ///
    /// 对齐 Python `AgentSession.register_workspace()` (L136)
    pub fn register_workspace(&mut self, workspace_instance_id: &str) {
        self.workspaces
            .entry(workspace_instance_id.to_string())
            .or_insert_with(|| WorkspaceState {
                epoch: 0,
                seq_counter: 0,
            });
    }

    /// 获取 workspace 的 epoch。
    ///
    /// 对齐 Python `AgentSession.get_epoch()` (L147)
    pub fn get_epoch(&self, workspace_instance_id: &str) -> u64 {
        self.workspaces
            .get(workspace_instance_id)
            .map(|ws| ws.epoch)
            .unwrap_or(0)
    }

    /// 设置 workspace 的 epoch（重置 seq_counter=0）。
    ///
    /// 对齐 Python `AgentSession.set_epoch()` (L153)
    pub fn set_epoch(&mut self, workspace_instance_id: &str, epoch: u64) {
        let ws = self
            .workspaces
            .entry(workspace_instance_id.to_string())
            .or_insert_with(|| WorkspaceState {
                epoch: 0,
                seq_counter: 0,
            });
        ws.epoch = epoch;
        ws.seq_counter = 0;
    }

    /// 分配下一个 monotonic_seq（+1）。
    ///
    /// 对齐 Python `AgentSession.next_seq()` (L167)
    /// 返回分配的 seq 值（≥1）。
    pub fn next_seq(&mut self, workspace_instance_id: &str) -> u64 {
        let ws = self
            .workspaces
            .entry(workspace_instance_id.to_string())
            .or_insert_with(|| WorkspaceState {
                epoch: 0,
                seq_counter: 0,
            });
        ws.seq_counter += 1;
        ws.seq_counter
    }

    /// 检查 workspace 是否已激活（epoch ≥ 1）。
    ///
    /// 对齐 Python `AgentSession.is_active()` (L209)
    pub fn is_active(&self, workspace_instance_id: &str) -> bool {
        self.workspaces
            .get(workspace_instance_id)
            .map(|ws| ws.epoch >= 1)
            .unwrap_or(false)
    }
}

/// Python 暴露的 build_connect_params。
#[pyfunction]
pub fn build_connect_params_py(
    workspace_instance_id: &str,
    agent_session_id: &str,
) -> (String, String) {
    let (method, params) = build_connect_params(workspace_instance_id, agent_session_id);
    (
        method,
        serde_json::to_string(&params).unwrap_or_default(),
    )
}

/// Python 暴露的 build_refresh_params。
#[pyfunction]
pub fn build_refresh_params_py(
    workspace_instance_id: &str,
    rel_path: &str,
    agent_session_id: &str,
    session_epoch: u64,
    monotonic_seq: u64,
) -> (String, String) {
    let (method, params) = build_refresh_params(
        workspace_instance_id,
        rel_path,
        agent_session_id,
        session_epoch,
        monotonic_seq,
    );
    (
        method,
        serde_json::to_string(&params).unwrap_or_default(),
    )
}

/// 将路径转为绝对路径（跨平台，对齐 Python os.path.abspath）。
///
/// Windows 上用 std::fs::canonicalize 不可用时不影响逻辑，
/// 这里仅做简单的 current_dir + 路径拼接。
pub fn to_abspath(path: &str) -> String {
    // 对齐 Python os.path.abspath：如果是相对路径，拼接 current_dir
    if std::path::Path::new(path).is_absolute() {
        path.to_string()
    } else {
        match std::env::current_dir() {
            Ok(cwd) => cwd.join(path).to_string_lossy().to_string(),
            Err(_) => path.to_string(),
        }
    }
}

// ============================================
// 单元测试（跨平台，纯逻辑）
// ============================================

#[cfg(test)]
mod tests {
    use super::*;

    // ============================================
    // D1: 跨平台协议层
    // ============================================

    #[test]
    fn test_d1_1_build_request_ping() {
        let request = build_request("ping", Value::Object(Map::new()));
        assert_eq!(request["method"], "ping");
        assert_eq!(request["params"], Value::Object(Map::new()));
    }

    #[test]
    fn test_d1_2_build_request_query() {
        let params = serde_json::json!({"ws_id": "abc", "type": "stats"});
        let request = build_request("query", params.clone());
        assert_eq!(request["method"], "query");
        assert_eq!(request["params"], params);
    }

    #[test]
    fn test_d1_3_parse_rpc_response_success() {
        let response = serde_json::json!({"ok": true, "result": {"pong": true}});
        let result = parse_rpc_response(&response).unwrap();
        assert_eq!(result, serde_json::json!({"pong": true}));
    }

    #[test]
    fn test_d1_4_parse_rpc_response_error() {
        let response = serde_json::json!({
            "ok": false,
            "error": {"code": "not_found", "message": "workspace not found"}
        });
        let err = parse_rpc_response(&response).unwrap_err();
        assert_eq!(err.code, "not_found");
        assert_eq!(err.message, "workspace not found");
    }

    #[test]
    fn test_d1_5_parse_rpc_response_missing_result() {
        let response = serde_json::json!({"ok": true});
        let result = parse_rpc_response(&response).unwrap();
        assert_eq!(result, Value::Null);
    }

    #[test]
    fn test_d1_6_parse_rpc_response_missing_error() {
        let response = serde_json::json!({"ok": false});
        let err = parse_rpc_response(&response).unwrap_err();
        assert_eq!(err.code, "daemon_error");
        assert_eq!(err.message, "unknown daemon error");
    }

    // ============================================
    // D2: build_request 边界
    // ============================================

    #[test]
    fn test_d2_1_build_request_empty_method() {
        let request = build_request("", Value::Object(Map::new()));
        assert_eq!(request["method"], "");
    }

    #[test]
    fn test_d2_2_build_request_null_params() {
        let request = build_request("ping", Value::Null);
        assert_eq!(request["method"], "ping");
        assert_eq!(request["params"], Value::Null);
    }

    #[test]
    fn test_d2_3_build_request_array_params() {
        let params = serde_json::json!([1, 2, 3]);
        let request = build_request("batch", params.clone());
        assert_eq!(request["method"], "batch");
        assert_eq!(request["params"], params);
    }

    #[test]
    fn test_d2_4_build_request_string_params() {
        let request = build_request("echo", Value::String("hello".to_string()));
        assert_eq!(request["method"], "echo");
        assert_eq!(request["params"], "hello");
    }

    // ============================================
    // D3: parse_rpc_response 边界
    // ============================================

    #[test]
    fn test_d3_1_parse_response_ok_not_bool() {
        // ok 字段不是 bool，应视为 false
        let response = serde_json::json!({"ok": "true", "result": 42});
        let err = parse_rpc_response(&response).unwrap_err();
        assert_eq!(err.code, "daemon_error");
    }

    #[test]
    fn test_d3_2_parse_response_ok_missing() {
        // 缺少 ok 字段，应视为 false
        let response = serde_json::json!({"result": 42});
        let err = parse_rpc_response(&response).unwrap_err();
        assert_eq!(err.code, "daemon_error");
    }

    #[test]
    fn test_d3_3_parse_response_error_partial() {
        // error 对象部分缺失
        let response = serde_json::json!({"ok": false, "error": {"code": "err"}});
        let err = parse_rpc_response(&response).unwrap_err();
        assert_eq!(err.code, "err");
        assert_eq!(err.message, "unknown daemon error");
    }

    #[test]
    fn test_d3_4_parse_response_error_not_object() {
        // error 字段不是 object
        let response = serde_json::json!({"ok": false, "error": "string error"});
        let err = parse_rpc_response(&response).unwrap_err();
        assert_eq!(err.code, "daemon_error");
        assert_eq!(err.message, "unknown daemon error");
    }

    // ============================================
    // D4: PyO3 暴露逻辑验证（通过直接调用 Rust 函数）
    // ============================================

    #[test]
    fn test_d4_1_build_request_py_logic() {
        // 模拟 build_request_py 的逻辑
        let params = Value::Object(Map::new());
        let request = build_request("ping", params);
        let json_str = serde_json::to_string(&request).unwrap();
        assert_eq!(json_str, r#"{"method":"ping","params":{}}"#);
    }

    #[test]
    fn test_d4_2_parse_rpc_response_py_logic_success() {
        // 模拟 parse_rpc_response_py 的成功路径
        let response = serde_json::json!({"ok": true, "result": {"pong": true}});
        let result = parse_rpc_response(&response).unwrap();
        let (ok, json_str) = (true, serde_json::to_string(&result).unwrap());
        assert!(ok);
        assert_eq!(json_str, r#"{"pong":true}"#);
    }

    #[test]
    fn test_d4_3_parse_rpc_response_py_logic_error() {
        // 模拟 parse_rpc_response_py 的失败路径
        let response = serde_json::json!({
            "ok": false,
            "error": {"code": "timeout", "message": "request timed out"}
        });
        let err = parse_rpc_response(&response).unwrap_err();
        let (ok, json_str) = (
            false,
            serde_json::to_string(&serde_json::json!({
                "code": err.code,
                "message": err.message
            }))
            .unwrap(),
        );
        assert!(!ok);
        assert!(json_str.contains("timeout"));
        assert!(json_str.contains("request timed out"));
    }

    #[test]
    fn test_d4_4_parse_rpc_response_py_logic_invalid_json() {
        // 模拟 parse_rpc_response_py 的无效 JSON 输入
        let response_str = "not valid json";
        let response: Value = match serde_json::from_str(response_str) {
            Ok(v) => v,
            Err(_) => Value::Null,
        };
        // 无效 JSON 应返回 false + parse_error
        let result = parse_rpc_response(&response);
        // Value::Null 不是 object，parse_response 会返回 Err
        assert!(result.is_err());
    }

    // ============================================
    // D5: ClientError 类型验证
    // ============================================

    #[test]
    fn test_d5_1_client_error_display() {
        let err = ClientError::SetTimeout(std::io::Error::new(
            std::io::ErrorKind::Other,
            "test",
        ));
        let msg = format!("{}", err);
        assert!(msg.contains("设置超时失败"));
    }

    #[test]
    fn test_d5_2_client_error_from_protocol() {
        let protocol_err = super::super::protocol::ProtocolError::ConnectionClosed;
        let client_err: ClientError = protocol_err.into();
        let msg = format!("{}", client_err);
        assert!(msg.contains("协议错误"));
    }

    #[test]
    fn test_d5_3_client_error_from_remote() {
        let remote_err = DaemonRemoteError {
            code: "test_code".to_string(),
            message: "test message".to_string(),
        };
        let client_err: ClientError = remote_err.into();
        let msg = format!("{}", client_err);
        assert!(msg.contains("远端错误"));
        assert!(msg.contains("test_code"));
    }

    // ============================================
    // Unix 专属测试（仅 Linux/macOS 编译）
    // ============================================

    #[cfg(unix)]
    #[test]
    fn test_unix_client_new_default() {
        use super::unix::UnixDaemonRpcClient;
        let client = UnixDaemonRpcClient::new("/tmp/test.sock");
        assert_eq!(client.socket_path, "/tmp/test.sock");
        assert_eq!(client.timeout, std::time::Duration::from_secs(30));
        assert_eq!(client.max_message_bytes, DEFAULT_MAX_MESSAGE_BYTES);
    }

    #[cfg(unix)]
    #[test]
    fn test_unix_client_with_timeout() {
        use super::unix::UnixDaemonRpcClient;
        let client = UnixDaemonRpcClient::new("/tmp/test.sock")
            .with_timeout(std::time::Duration::from_secs(60));
        assert_eq!(client.timeout, std::time::Duration::from_secs(60));
    }

    #[cfg(unix)]
    #[test]
    fn test_unix_client_with_max_message_bytes() {
        use super::unix::UnixDaemonRpcClient;
        let client = UnixDaemonRpcClient::new("/tmp/test.sock")
            .with_max_message_bytes(1024);
        assert_eq!(client.max_message_bytes, 1024);
    }

    #[cfg(unix)]
    #[test]
    fn test_unix_client_connect_failed() {
        use super::unix::UnixDaemonRpcClient;
        // 连接不存在的 socket 应返回 ConnectFailed 错误
        let client = UnixDaemonRpcClient::new("/tmp/nonexistent_callwarden_test.sock");
        let result = client.ping();
        assert!(result.is_err());
        match result.unwrap_err() {
            ClientError::ConnectFailed { path, .. } => {
                assert_eq!(path, "/tmp/nonexistent_callwarden_test.sock");
            }
            other => panic!("期望 ConnectFailed，实际: {:?}", other),
        }
    }

    // ============================================
    // D6: build_query_request 参数构建（Phase 5-2 Slice 2）
    // ============================================

    #[test]
    fn test_d6_1_query_stats() {
        let (method, params) =
            build_query_request("ws-1", "stats", "", None, None, None, None).unwrap();
        assert_eq!(method, "query.stats");
        assert_eq!(params["workspace_instance_id"], "ws-1");
        // stats 无额外参数
        assert_eq!(params.as_object().unwrap().len(), 1);
    }

    #[test]
    fn test_d6_2_query_symbol() {
        let (method, params) = build_query_request(
            "ws-1",
            "symbol",
            "module::func",
            None,
            None,
            None,
            None,
        )
        .unwrap();
        assert_eq!(method, "query.symbol");
        assert_eq!(params["qualified_name"], "module::func");
        assert_eq!(params.as_object().unwrap().len(), 2);
    }

    #[test]
    fn test_d6_3_query_search_default_limit() {
        let (method, params) =
            build_query_request("ws-1", "search", "foo", None, None, None, None).unwrap();
        assert_eq!(method, "query.search");
        assert_eq!(params["query"], "foo");
        assert_eq!(params["limit"], 20); // 默认
        assert_eq!(params.as_object().unwrap().len(), 3); // ws_id + query + limit
    }

    #[test]
    fn test_d6_4_query_search_with_kind_and_limit() {
        let (method, params) = build_query_request(
            "ws-1",
            "search",
            "foo",
            None,
            Some("function"),
            Some(50),
            None,
        )
        .unwrap();
        assert_eq!(method, "query.search");
        assert_eq!(params["query"], "foo");
        assert_eq!(params["kind"], "function");
        assert_eq!(params["limit"], 50);
    }

    #[test]
    fn test_d6_5_query_callers() {
        let (method, params) = build_query_request(
            "ws-1",
            "callers",
            "callee_func",
            Some("module::caller"),
            None,
            None,
            None,
        )
        .unwrap();
        assert_eq!(method, "query.callers");
        assert_eq!(params["callee_name"], "callee_func");
        assert_eq!(params["qualified_name"], "module::caller");
    }

    #[test]
    fn test_d6_6_query_callees() {
        let (method, params) = build_query_request(
            "ws-1",
            "callees",
            "caller_func",
            Some("module::callee"),
            None,
            None,
            None,
        )
        .unwrap();
        assert_eq!(method, "query.callees");
        assert_eq!(params["caller_name"], "caller_func");
        assert_eq!(params["qualified_name"], "module::callee");
    }

    #[test]
    fn test_d6_7_query_call_chain_down_default_depth() {
        let (method, params) = build_query_request(
            "ws-1",
            "call_chain_down",
            "module::func",
            None,
            None,
            None,
            None,
        )
        .unwrap();
        assert_eq!(method, "query.call_chain_down");
        assert_eq!(params["qualified_name"], "module::func");
        assert_eq!(params["max_depth"], 10); // 默认
    }

    #[test]
    fn test_d6_8_query_topological_order_default_limit() {
        let (method, params) = build_query_request(
            "ws-1",
            "topological_order",
            "",
            None,
            None,
            None,
            None,
        )
        .unwrap();
        assert_eq!(method, "query.topological_order");
        assert_eq!(params["limit"], 20); // 默认
    }

    #[test]
    fn test_d6_8a_query_impact_default_depth() {
        let (method, params) =
            build_query_request("ws-1", "impact", "hash-a", None, None, None, None).unwrap();
        assert_eq!(method, "query.impact");
        assert_eq!(params["symbol_hash"], "hash-a");
        assert_eq!(params["depth"], 3);
    }

    #[test]
    fn test_d6_9_query_detect_cycles_default_depth() {
        let (method, params) = build_query_request(
            "ws-1",
            "detect_cycles",
            "",
            None,
            None,
            None,
            None,
        )
        .unwrap();
        assert_eq!(method, "query.detect_cycles");
        assert_eq!(params["max_depth"], 10); // 默认
    }

    #[test]
    fn test_d6_10_query_unknown_type() {
        let result = build_query_request("ws-1", "unknown", "", None, None, None, None);
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(matches!(err, QueryError::UnknownQueryType(_)));
    }

    #[test]
    fn test_d6_11_query_callers_no_qualified_name() {
        // qualified_name=None 不添加到 params
        let (_, params) = build_query_request(
            "ws-1",
            "callers",
            "callee_func",
            None,
            None,
            None,
            None,
        )
        .unwrap();
        assert_eq!(params["callee_name"], "callee_func");
        assert!(params.get("qualified_name").is_none());
    }

    #[test]
    fn test_d6_12_query_search_empty_kind() {
        // kind="" 不添加到 params（空字符串过滤）
        let (_, params) =
            build_query_request("ws-1", "search", "foo", None, Some(""), None, None).unwrap();
        assert_eq!(params["query"], "foo");
        assert_eq!(params["limit"], 20);
        assert!(params.get("kind").is_none());
    }

    #[test]
    fn test_d6_13_query_types_count() {
        // 验证支持 9 种 query 类型
        assert_eq!(QUERY_TYPES.len(), 9);
    }

    // ============================================
    // D7: 简单命令参数构建（Slice 3）
    // ============================================

    #[test]
    fn test_d7_1_list_action() {
        let (method, params) = build_simple_request("list", None).unwrap();
        assert_eq!(method, "workspace.list");
        assert_eq!(params, Value::Object(Map::new()));
    }

    #[test]
    fn test_d7_2_status_action_with_workspace_id() {
        let (method, params) =
            build_simple_request("status", Some("ws-abc-123")).unwrap();
        assert_eq!(method, "workspace.status");
        assert_eq!(
            params["workspace_instance_id"],
            Value::String("ws-abc-123".to_string())
        );
    }

    #[test]
    fn test_d7_3_status_action_missing_workspace_id() {
        let result = build_simple_request("status", None);
        assert!(matches!(result, Err(SimpleError::MissingWorkspaceId)));
    }

    #[test]
    fn test_d7_4_health_action() {
        let (method, params) = build_simple_request("health", None).unwrap();
        assert_eq!(method, "health");
        assert_eq!(params, Value::Object(Map::new()));
    }

    #[test]
    fn test_d7_5_schema_version_action() {
        let (method, params) = build_simple_request("schema-version", None).unwrap();
        // RPC method 是 schema.version（点号，不是下划线）
        assert_eq!(method, "schema.version");
        assert_eq!(params, Value::Object(Map::new()));
    }

    #[test]
    fn test_d7_6_unknown_action() {
        let result = build_simple_request("unknown-cmd", None);
        assert!(matches!(result, Err(SimpleError::UnknownAction(_))));
    }

    #[test]
    fn test_d7_7_simple_actions_count() {
        // 验证支持 4 个简单 action
        assert_eq!(SIMPLE_ACTIONS.len(), 4);
        assert!(SIMPLE_ACTIONS.contains(&"list"));
        assert!(SIMPLE_ACTIONS.contains(&"status"));
        assert!(SIMPLE_ACTIONS.contains(&"health"));
        assert!(SIMPLE_ACTIONS.contains(&"schema-version"));
    }

    #[test]
    fn test_d7_8_status_ignores_none_workspace_id_when_not_status() {
        // list/health/schema-version 忽略 workspace_id 参数
        let (m1, _) = build_simple_request("list", Some("ignored")).unwrap();
        assert_eq!(m1, "workspace.list");
        let (m2, _) = build_simple_request("health", Some("ignored")).unwrap();
        assert_eq!(m2, "health");
        let (m3, _) = build_simple_request("schema-version", Some("ignored")).unwrap();
        assert_eq!(m3, "schema.version");
    }

    #[test]
    fn test_d7_9_build_simple_request_py_success() {
        let (method, params_json) = build_simple_request_py("status", Some("ws-1"));
        assert_eq!(method, "workspace.status");
        assert!(params_json.contains("workspace_instance_id"));
        assert!(params_json.contains("ws-1"));
    }

    #[test]
    fn test_d7_10_build_simple_request_py_error() {
        let (method, err_msg) = build_simple_request_py("unknown", None);
        assert_eq!(method, "ERROR");
        assert!(err_msg.contains("不支持的 action"));
    }

    #[test]
    fn test_d7_11_status_empty_workspace_id_string() {
        // 空字符串 workspace_id 仍发送（Python 也会传递空字符串）
        let (method, params) = build_simple_request("status", Some("")).unwrap();
        assert_eq!(method, "workspace.status");
        assert_eq!(params["workspace_instance_id"], Value::String("".to_string()));
    }

    // ============================================
    // D8: 剩余 RPC 命令参数构建（Slice 5）
    // ============================================

    #[test]
    fn test_d8_1_register_method_mapping() {
        let params = r#"{"client_view_root":"/tmp","git_remote_url":"","git_head_commit_sha":"abc","toolchain_fingerprint":""}"#;
        let (method, _) = build_rpc_request("register", params).unwrap();
        assert_eq!(method, "workspace.register");
    }

    #[test]
    fn test_d8_workspace_lifecycle_write_mappings() {
        let params = r#"{"workspace_instance_id":"ws-1"}"#;
        let (activate_method, activate_params) =
            build_rpc_request("activate", params).unwrap();
        let (remove_method, remove_params) = build_rpc_request("remove", params).unwrap();
        assert_eq!(activate_method, "workspace.activate");
        assert_eq!(remove_method, "workspace.remove");
        assert_eq!(activate_params["workspace_instance_id"], "ws-1");
        assert_eq!(remove_params["workspace_instance_id"], "ws-1");
    }

    #[test]
    fn test_d8_2_backup_method_mapping() {
        let params = r#"{"output_path":"/tmp/backup.db"}"#;
        let (method, p) = build_rpc_request("backup", params).unwrap();
        assert_eq!(method, "backup");
        assert_eq!(p["output_path"], "/tmp/backup.db");
    }

    #[test]
    fn test_d8_3_restore_method_mapping() {
        let params = r#"{"source_path":"/tmp/backup.db"}"#;
        let (method, _) = build_rpc_request("restore", params).unwrap();
        assert_eq!(method, "restore");
    }

    #[test]
    fn test_d8_4_gc_cas_method_mapping() {
        let params = r#"{"workspace_instance_id":"ws-1","grace_days":7}"#;
        let (method, p) = build_rpc_request("gc-cas", params).unwrap();
        assert_eq!(method, "gc.cas");
        assert_eq!(p["workspace_instance_id"], "ws-1");
        assert_eq!(p["grace_days"], 7);
    }

    #[test]
    fn test_d8_5_gc_snapshots_method_mapping() {
        let params = r#"{"keep_last":3}"#;
        let (method, p) = build_rpc_request("gc-snapshots", params).unwrap();
        assert_eq!(method, "gc.snapshots");
        assert_eq!(p["keep_last"], 3);
    }

    #[test]
    fn test_d8_6_snapshot_stats_method_mapping() {
        let (method, _) = build_rpc_request("snapshot-stats", "{}").unwrap();
        assert_eq!(method, "snapshot.stats");
    }

    #[test]
    fn test_d8_7_snapshot_list_method_mapping() {
        let (method, _) = build_rpc_request("snapshot-list", "{}").unwrap();
        assert_eq!(method, "snapshot.list_workspaces");
    }

    #[test]
    fn test_d8_8_snapshot_evict_method_mapping() {
        let params = r#"{"workspace_instance_id":"ws-1"}"#;
        let (method, p) = build_rpc_request("snapshot-evict", params).unwrap();
        assert_eq!(method, "snapshot.evict");
        assert_eq!(p["workspace_instance_id"], "ws-1");
    }

    #[test]
    fn test_d8_9_mount_register_method_mapping() {
        let params = r#"{"container_id":"ubuntu","container_path":"/mnt","host_path":"/tmp","mapping_type":"bind"}"#;
        let (method, p) = build_rpc_request("mount-register", params).unwrap();
        assert_eq!(method, "mount.register");
        assert_eq!(p["container_id"], "ubuntu");
        assert_eq!(p["mapping_type"], "bind");
    }

    #[test]
    fn test_d8_10_mount_list_method_mapping() {
        let (method, p) = build_rpc_request("mount-list", "{}").unwrap();
        assert_eq!(method, "mount.list");
        assert!(p.as_object().unwrap().is_empty());
    }

    #[test]
    fn test_d8_11_mount_delete_method_mapping() {
        let params = r#"{"container_id":"ubuntu","container_path":"/mnt"}"#;
        let (method, p) = build_rpc_request("mount-delete", params).unwrap();
        assert_eq!(method, "mount.delete");
        assert_eq!(p["container_id"], "ubuntu");
    }

    #[test]
    fn test_d8_12_unknown_action_error() {
        let result = build_rpc_request("unknown", "{}");
        assert!(matches!(result, Err(RpcError::UnknownAction(_))));
    }

    #[test]
    fn test_d8_13_invalid_json_error() {
        let result = build_rpc_request("backup", "not json");
        assert!(matches!(result, Err(RpcError::MissingParam(_))));
    }

    #[test]
    fn test_d8_14_rpc_actions_count() {
        assert_eq!(RPC_ACTIONS.len(), 13);
    }

    #[test]
    fn test_d8_15_build_rpc_request_py_success() {
        let (method, params_json) = build_rpc_request_py("backup", r#"{"output_path":"/tmp/b"}"#);
        assert_eq!(method, "backup");
        assert!(params_json.contains("output_path"));
    }

    #[test]
    fn test_d8_16_build_rpc_request_py_error() {
        let (method, err) = build_rpc_request_py("unknown", "{}");
        assert_eq!(method, "ERROR");
        assert!(err.contains("不支持的 RPC action"));
    }

    #[test]
    fn test_d8_17_to_abspath_absolute() {
        // 绝对路径直接返回
        let path = if cfg!(windows) { "C:\\tmp\\test.db" } else { "/tmp/test.db" };
        assert_eq!(to_abspath(path), path);
    }

    #[test]
    fn test_d8_18_to_abspath_relative() {
        // 相对路径拼接 current_dir
        let result = to_abspath("test.db");
        // 应包含 test.db 且是绝对路径
        assert!(result.ends_with("test.db"));
        assert!(std::path::Path::new(&result).is_absolute());
    }

    // ============================================
    // D9: publish 参数构建（Slice 4）
    // ============================================

    #[test]
    fn test_d9_1_build_publish_params_basic() {
        let (method, params) = build_publish_params("ws-abc-123", "");
        assert_eq!(method, "snapshot.publish");
        assert_eq!(params["workspace_instance_id"], "ws-abc-123");
        assert_eq!(params["build_context_hash"], "");
    }

    #[test]
    fn test_d9_2_build_publish_params_with_context() {
        let (method, params) = build_publish_params("ws-1", "ctx-hash-xyz");
        assert_eq!(method, "snapshot.publish");
        assert_eq!(params["workspace_instance_id"], "ws-1");
        assert_eq!(params["build_context_hash"], "ctx-hash-xyz");
    }

    #[test]
    fn test_d9_3_build_publish_params_empty_workspace() {
        let (method, params) = build_publish_params("", "");
        assert_eq!(method, "snapshot.publish");
        assert_eq!(params["workspace_instance_id"], "");
    }

    #[test]
    fn test_d9_4_build_publish_params_py_returns_tuple() {
        let (method, params_json) = build_publish_params_py("ws-1", "ctx");
        assert_eq!(method, "snapshot.publish");
        assert!(params_json.contains("workspace_instance_id"));
        assert!(params_json.contains("ws-1"));
        assert!(params_json.contains("build_context_hash"));
        assert!(params_json.contains("ctx"));
    }

    #[test]
    fn test_d9_5_build_publish_params_only_two_fields() {
        // 验证 params 只有 2 个字段（workspace_instance_id + build_context_hash）
        let (_, params) = build_publish_params("ws-1", "ctx");
        let obj = params.as_object().unwrap();
        assert_eq!(obj.len(), 2);
        assert!(obj.contains_key("workspace_instance_id"));
        assert!(obj.contains_key("build_context_hash"));
    }

    // ============================================
    // D10: agent session + watcher 参数构建（Slice 6）
    // ============================================

    #[test]
    fn test_d10_1_build_connect_params() {
        let (method, params) = build_connect_params("ws-abc-123", "agent-deadbeef1234");
        assert_eq!(method, "workspace.connect");
        assert_eq!(params["workspace_instance_id"], "ws-abc-123");
        assert_eq!(params["agent_session_id"], "agent-deadbeef1234");
    }

    #[test]
    fn test_d10_2_build_connect_params_has_two_fields() {
        let (_, params) = build_connect_params("ws-1", "agent-abc");
        let obj = params.as_object().unwrap();
        assert_eq!(obj.len(), 2);
        assert!(obj.contains_key("workspace_instance_id"));
        assert!(obj.contains_key("agent_session_id"));
    }

    #[test]
    fn test_d10_3_build_refresh_params() {
        let (method, params) = build_refresh_params(
            "ws-1",
            "src/main.rs",
            "agent-abc",
            42,
            7,
        );
        assert_eq!(method, "workspace.file.refresh");
        assert_eq!(params["workspace_instance_id"], "ws-1");
        assert_eq!(params["rel_path"], "src/main.rs");
        assert_eq!(params["agent_session_id"], "agent-abc");
        assert_eq!(params["session_epoch"], 42);
        assert_eq!(params["monotonic_seq"], 7);
    }

    #[test]
    fn test_d10_4_build_refresh_params_has_five_fields() {
        let (_, params) = build_refresh_params("ws", "p", "a", 1, 1);
        let obj = params.as_object().unwrap();
        assert_eq!(obj.len(), 5);
    }

    #[test]
    fn test_d10_5_build_agent_ping_params() {
        let (method, params) = build_agent_ping_params();
        assert_eq!(method, "ping");
        assert_eq!(params, Value::Object(Map::new()));
    }

    #[test]
    fn test_d10_6_agent_session_new() {
        let session = AgentSession::new("agent-test123".to_string());
        assert_eq!(session.session_id, "agent-test123");
        assert!(!session.is_active("ws-1"));
    }

    #[test]
    fn test_d10_7_agent_session_register_and_epoch() {
        let mut session = AgentSession::new("agent-test".to_string());
        session.register_workspace("ws-1");
        assert_eq!(session.get_epoch("ws-1"), 0);
        assert!(!session.is_active("ws-1"));

        session.set_epoch("ws-1", 5);
        assert_eq!(session.get_epoch("ws-1"), 5);
        assert!(session.is_active("ws-1"));
    }

    #[test]
    fn test_d10_8_agent_session_next_seq() {
        let mut session = AgentSession::new("agent-test".to_string());
        session.register_workspace("ws-1");

        assert_eq!(session.next_seq("ws-1"), 1);
        assert_eq!(session.next_seq("ws-1"), 2);
        assert_eq!(session.next_seq("ws-1"), 3);
    }

    #[test]
    fn test_d10_9_agent_session_set_epoch_resets_seq() {
        let mut session = AgentSession::new("agent-test".to_string());
        session.register_workspace("ws-1");
        session.next_seq("ws-1");
        session.next_seq("ws-1");
        assert_eq!(session.next_seq("ws-1"), 3);

        // set_epoch 重置 seq_counter
        session.set_epoch("ws-1", 10);
        assert_eq!(session.next_seq("ws-1"), 1);
    }

    #[test]
    fn test_d10_10_agent_session_generate_id_format() {
        let id = AgentSession::generate_session_id();
        assert!(id.starts_with("agent-"));
        assert_eq!(id.len(), 18); // "agent-" (6) + 12 hex chars
    }

    #[test]
    fn test_d10_11_agent_session_multiple_workspaces() {
        let mut session = AgentSession::new("agent-test".to_string());
        session.register_workspace("ws-1");
        session.register_workspace("ws-2");

        session.set_epoch("ws-1", 1);
        session.set_epoch("ws-2", 2);

        assert_eq!(session.get_epoch("ws-1"), 1);
        assert_eq!(session.get_epoch("ws-2"), 2);
        assert!(session.is_active("ws-1"));
        assert!(session.is_active("ws-2"));
    }

    #[test]
    fn test_d10_12_build_connect_params_py() {
        let (method, params_json) =
            build_connect_params_py("ws-1", "agent-abc");
        assert_eq!(method, "workspace.connect");
        assert!(params_json.contains("workspace_instance_id"));
        assert!(params_json.contains("agent_session_id"));
    }

    #[test]
    fn test_d10_13_build_refresh_params_py() {
        let (method, params_json) =
            build_refresh_params_py("ws-1", "src/main.rs", "agent-abc", 5, 10);
        assert_eq!(method, "workspace.file.refresh");
        assert!(params_json.contains("session_epoch"));
        assert!(params_json.contains("monotonic_seq"));
        assert!(params_json.contains("src/main.rs"));
    }

    #[test]
    fn test_d10_14_agent_session_next_seq_auto_registers() {
        // next_seq 在 workspace 未注册时自动注册
        let mut session = AgentSession::new("agent-test".to_string());
        assert_eq!(session.next_seq("ws-auto"), 1);
        assert!(!session.is_active("ws-auto")); // epoch 仍为 0
    }
}
