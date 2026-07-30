# Phase 5-2 Slice 1 契约：Rust UDS Client + cw-client ping

**Task ID**: `T-1785251742819-94f16bee`（Phase 5-2 Slice 1）
**状态**: contract
**日期**: 2026-07-28
**依赖**: Phase 4（daemon 服务端 + 协议层）、Phase 5-1 A（clap 骨架）

## 1. 范围

Phase 5-2 Slice 1 是 Rust client/agent 迁移的最小闭环：实现 Rust UDS RPC Client + `cw-client ping` 命令，验证端到端 RPC 通信。

**涉及**：
- **C.1 跨平台协议层**：`build_request` / `parse_rpc_response`（纯逻辑，Windows 可测）
- **C.2 Unix UDS Client**：`UnixDaemonRpcClient` struct + `call(method, params)` 方法（`#[cfg(unix)]`）
- **C.3 PyO3 暴露**：`build_request_py` / `parse_rpc_response_py` / `daemon_client_call_py`
- **C.4 cw-client binary**：clap 骨架 + `ping` 子命令
- **C.5 差分测试**：D1 跨平台协议层（Windows 可测）+ D2 UDS 端到端（仅 Linux）

**不涉及**（留给后续 Slice）：
- SCM_RIGHTS FD 传递（Slice 4）
- 31 个 RPC 方法的完整 CLI 包装（Slice 3/5）
- cw-agent watcher + session（Slice 6）
- wire-production 路由整合（Slice 7）
- cw_cli binary 数据源接入（Slice 2）
- SQL fallback 路径

## 2. Python 真相源

| 文件 | 行号 | 函数/类 | 迁移方式 |
|---|---|---|---|
| `server/daemon_client.py` | - | `UnixDaemonRpcClient` | Rust `UnixDaemonRpcClient`（`#[cfg(unix)]`） |
| `server/daemon_client.py` | - | `UnixDaemonRpcClient.call()` | Rust `UnixDaemonRpcClient::call()` |
| `server/daemon_protocol.py` | - | `send_message` / `recv_message` | 复用 `rust_ext/src/daemon/protocol.rs` |
| `server/daemon_protocol.py` | - | `parse_response` | 复用 `rust_ext/src/daemon/protocol.rs:parse_response` |
| `cli/daemon_commands.py` | - | `cmd_ping` | Rust `cw-client ping` 子命令 |

### Python 行为详解

```python
class UnixDaemonRpcClient:
    def __init__(self, socket_path, timeout=30, max_message_bytes=8*1024*1024):
        self.socket_path = socket_path
        self.timeout = timeout
        self.max_message_bytes = max_message_bytes

    def call(self, method, params=None):
        # 每次请求建立新 UDS 连接
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        try:
            request = {"method": method, "params": params or {}}
            send_message(sock, request, self.max_message_bytes)
            response = recv_message(sock, self.max_message_bytes)
            return parse_response(response)
        finally:
            sock.close()
```

**关键行为**：
1. **无状态连接**：每次 `call()` 建立新 UDS 连接，请求完成后关闭
2. **请求格式**：`{"method": "...", "params": {...}}`
3. **响应格式**：`{"ok": true, "result": ...}` 或 `{"ok": false, "error": {"code": "...", "message": "..."}}`
4. **超时**：默认 30 秒
5. **最大消息**：默认 8MB

## 3. API 契约

### 3.1 跨平台协议层（C.1）

```rust
use serde_json::{Map, Value};

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
    // 直接复用 protocol.rs 的 parse_response
    crate::daemon::protocol::parse_response(response)
}
```

### 3.2 Unix UDS Client（C.2）

```rust
#[cfg(unix)]
pub mod unix {
    use super::*;
    use std::os::unix::net::UnixStream;
    use std::time::Duration;
    use crate::daemon::protocol::{send_message, recv_message, DEFAULT_MAX_MESSAGE_BYTES};

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
        pub fn new(socket_path: &str) -> Self {
            Self {
                socket_path: socket_path.to_string(),
                timeout: Duration::from_secs(30),
                max_message_bytes: DEFAULT_MAX_MESSAGE_BYTES,
            }
        }

        pub fn with_timeout(mut self, timeout: Duration) -> Self {
            self.timeout = timeout;
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
        /// 6. 关闭连接
        pub fn call(&self, method: &str, params: Value) -> Result<Value, ClientError> {
            // 1. 建立 UDS 连接
            let mut stream = UnixStream::connect(&self.socket_path)
                .map_err(|e| ClientError::ConnectFailed {
                    path: self.socket_path.clone(),
                    source: e,
                })?;

            // 2. 设置超时
            stream.set_read_timeout(Some(self.timeout))
                .map_err(|e| ClientError::SetTimeout(e))?;
            stream.set_write_timeout(Some(self.timeout))
                .map_err(|e| ClientError::SetTimeout(e))?;

            // 3. 发送请求
            let request = build_request(method, params);
            send_message(&mut stream, &request, self.max_message_bytes)
                .map_err(ClientError::Protocol)?;

            // 4. 接收响应
            let response = recv_message(&mut stream, self.max_message_bytes)
                .map_err(ClientError::Protocol)?;

            // 5. 解析响应
            let result = parse_rpc_response(&response)
                .map_err(ClientError::Remote)?;

            // 6. 连接自动关闭（UnixStream Drop）
            Ok(result)
        }

        /// 便捷方法：ping
        pub fn ping(&self) -> Result<Value, ClientError> {
            self.call("ping", Value::Object(Map::new()))
        }
    }
}

/// Client 错误类型
#[derive(Debug, thiserror::Error)]
pub enum ClientError {
    #[error("UDS 连接失败 (path={path}): {source}")]
    ConnectFailed { path: String, source: std::io::Error },
    #[error("设置超时失败: {0}")]
    SetTimeout(std::io::Error),
    #[error("协议错误: {0}")]
    Protocol(#[from] crate::daemon::protocol::ProtocolError),
    #[error("远端错误: {0}")]
    Remote(#[from] crate::daemon::protocol::DaemonRemoteError),
}
```

### 3.3 PyO3 暴露（C.3）

```rust
/// Python 暴露的 build_request。
///
/// 返回 JSON 字符串，便于 Python 端验证请求格式。
#[pyfunction]
pub fn build_request_py(method: &str, params_json: &str) -> String {
    let params: Value = serde_json::from_str(params_json).unwrap_or(Value::Null);
    let request = build_request(method, params);
    serde_json::to_string(&request).unwrap_or_default()
}

/// Python 暴露的 parse_rpc_response。
///
/// 返回 (ok, result_or_error_json) 元组：
/// - ok=true: result_or_error_json 是 result 的 JSON 字符串
/// - ok=false: result_or_error_json 是 error 的 JSON 字符串
#[pyfunction]
pub fn parse_rpc_response_py(response_json: &str) -> (bool, String) {
    let response: Value = match serde_json::from_str(response_json) {
        Ok(v) => v,
        Err(_) => return (false, r#"{"code":"parse_error","message":"invalid JSON"}"#.to_string()),
    };
    match parse_rpc_response(&response) {
        Ok(result) => (true, serde_json::to_string(&result).unwrap_or_default()),
        Err(e) => (false, serde_json::to_string(&serde_json::json!({
            "code": e.code,
            "message": e.message
        })).unwrap_or_default()),
    }
}

/// Python 暴露的 daemon_client_call（仅 Unix）。
///
/// 返回 (exit_code, result_json, stderr)：
/// - exit_code=0: result_json 包含 RPC 结果
/// - exit_code=1: stderr 包含错误信息
#[cfg(unix)]
#[pyfunction]
pub fn daemon_client_call_py(
    socket_path: &str,
    method: &str,
    params_json: &str,
    timeout_secs: Option<u64>,
) -> (i32, String, String) {
    let params: Value = serde_json::from_str(params_json).unwrap_or(Value::Object(Default::default()));
    let mut client = unix::UnixDaemonRpcClient::new(socket_path);
    if let Some(secs) = timeout_secs {
        client = client.with_timeout(Duration::from_secs(secs));
    }
    match client.call(method, params) {
        Ok(result) => (0, serde_json::to_string(&result).unwrap_or_default(), String::new()),
        Err(e) => (1, String::new(), format!("{}", e)),
    }
}
```

### 3.4 cw-client binary（C.4）

```rust
// rust_ext/src/bin/cw_client.rs
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "cw-client", version, about = "Call Warden daemon RPC client")]
struct Cli {
    /// daemon socket 路径
    #[arg(long, default_value = "/tmp/callwarden_daemon.sock", global = true)]
    socket: String,

    /// 超时（秒）
    #[arg(long, default_value_t = 30, global = true)]
    timeout: u64,

    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Subcommand)]
enum Commands {
    /// ping daemon（测试连接）
    Ping,
}

fn main() {
    let cli = Cli::parse();
    match cli.command {
        Some(Commands::Ping) => {
            #[cfg(unix)]
            {
                let client = callwarden_core::daemon::client::unix::UnixDaemonRpcClient::new(&cli.socket)
                    .with_timeout(std::time::Duration::from_secs(cli.timeout));
                match client.ping() {
                    Ok(result) => {
                        println!("{}", serde_json::to_string_pretty(&result).unwrap_or_default());
                        std::process::exit(0);
                    }
                    Err(e) => {
                        eprintln!("cw-client ping: {}", e);
                        std::process::exit(1);
                    }
                }
            }
            #[cfg(not(unix))]
            {
                eprintln!("cw-client: UDS not available on this platform (Linux only)");
                std::process::exit(2);
            }
        }
        None => {
            Cli::parse_from(["cw-client", "--help"]);
        }
    }
}
```

## 4. 差分测试矩阵

### D1: 跨平台协议层（Windows 可测）

| 场景 | 输入 | 期望输出 |
|---|---|---|
| D1.1 build_request ping | `("ping", {})` | `{"method":"ping","params":{}}` |
| D1.2 build_request query | `("query", {"ws_id":"abc","type":"stats"})` | `{"method":"query","params":{"ws_id":"abc","type":"stats"}}` |
| D1.3 parse_rpc_response 成功 | `{"ok":true,"result":{"pong":true}}` | `Ok({"pong":true})` |
| D1.4 parse_rpc_response 失败 | `{"ok":false,"error":{"code":"not_found","message":"workspace not found"}}` | `Err(DaemonRemoteError{code:"not_found",...})` |
| D1.5 parse_rpc_response 缺 result | `{"ok":true}` | `Ok(Null)` |
| D1.6 parse_rpc_response 缺 error | `{"ok":false}` | `Err(DaemonRemoteError{code:"daemon_error",...})` |

### D2: UDS 端到端（仅 Linux）

| 场景 | 前置条件 | 期望 |
|---|---|---|
| D2.1 ping 成功 | daemon 已启动 | exit 0，stdout `{"pong": true, ...}` |
| D2.2 daemon 未启动 | 无 daemon | exit 1，stderr "UDS 连接失败" |
| D2.3 超时 | daemon 挂起 | exit 1，stderr "超时" |

## 5. 实现计划

1. **创建 `rust_ext/src/daemon/client.rs`**：跨平台协议层 + Unix UDS Client + PyO3 暴露 + 单元测试
2. **修改 `rust_ext/src/daemon/mod.rs`**：声明 `pub mod client;`
3. **修改 `rust_ext/src/lib.rs`**：注册 PyO3 函数
4. **创建 `rust_ext/src/bin/cw_client.rs`**：cw-client binary
5. **修改 `rust_ext/Cargo.toml`**：新增 cw-client binary target
6. **创建 `tests/test_phase5_2_slice1_client_diff.py`**：D1 跨平台差分测试
7. **更新 `migration-manifest.md`**：添加 §36 Phase 5-2 Slice 1

## 6. 预期差异

1. **无状态连接**：Python 和 Rust 都是无状态（每次 call 建立新连接），行为一致。

2. **错误类型**：Python 抛 `DaemonError` / `ConnectionRefusedError`；Rust 返回 `ClientError` 枚举。通过 PyO3 转换为 `(exit_code, stdout, stderr)` 三元组。

3. **超时单位**：Python 用秒（float）；Rust 用 `Duration`（支持纳秒精度）。PyO3 接口接受 `timeout_secs: Option<u64>`。

4. **平台门禁**：Python 运行时检查 `sys.platform != "linux"` exit 2；Rust 编译时 `#[cfg(unix)]` + 运行时 `#[cfg(not(unix))]` exit 2。

## 7. 验收标准

1. **D1 跨平台差分测试全部通过**（Windows 可测）
2. **Rust 单元测试全部通过**（跨平台部分）
3. **`cargo build --lib` 编译通过**（Windows + Linux）
4. **`cargo build --bin cw-client` 编译通过**（Linux，Windows 上 cw-client binary 不编译 UDS 部分）
5. **PyO3 暴露 3 个函数**：`build_request_py` / `parse_rpc_response_py` / `daemon_client_call_py`（后者仅 Unix）
6. **migration-manifest.md §36 Review 清单完整**

## 8. 与后续 Slice 的关系

| Slice | 交付物 | Slice 1 关系 |
|---|---|---|
| 1（本阶段） | UDS Client + cw-client ping | **本阶段** |
| 2 | cw stats 数据源接入 | 用 Slice 1 的 client 连接 daemon |
| 3 | 5 个核心查询子命令 | 复用 Slice 1 的 client + 协议层 |
| 4 | snapshot.publish + SCM_RIGHTS | 扩展 Slice 1 的 client 支持 FD 传递 |
| 5 | 剩余 25 个子命令 | 复用 Slice 1 的 client |
| 6 | cw-agent watcher + session | 新增 agent 模块 |
| 7 | wire-production 路由整合 | 整合所有 Slice |
