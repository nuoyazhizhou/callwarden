//! cw_bridge —— Windows 侧受限 bridge，供 WSL 客户端访问 Windows cw-daemon。
//!
//! 共存契约（windows-wsl-daemon-coexistence-contract.md §4.2 / 子任务2）：
//! bridge 的职责只有：
//! 1. 从 WSL 可达的本地端点（loopback TCP）接收有界 JSON-RPC 请求；
//! 2. 用安装时生成、ACL 仅当前 Windows 用户可读的 token 做本地认证；
//! 3. 将请求原样转发到当前用户 SID 的 Named Pipe（\\.\pipe\callwarden-<sid>）；
//! 4. 将 daemon 的结构化响应原样返回；
//! 5. 记录 bridge connection、authority_id、request_id 和错误码。
//!
//! bridge 禁止：
//! - 打开或复制 callwarden.db / -wal / -shm；
//! - 自己实现 task 状态更新；
//! - 在 daemon 不可用时写本地 SQLite；
//! - 接受客户端传入的 Windows SID 作为身份事实；
//! - 把 WSL 中的任意路径直接当成 Windows workspace 路径。
//!
//! 平台：Windows-only。非 Windows 平台打印错误并退出（不冒充可用）。

use std::net::{TcpListener, TcpStream};
use std::thread;

use callwarden_core::daemon::protocol::{recv_message, send_message, DEFAULT_MAX_MESSAGE_BYTES};
use serde_json::{Map, Value};

/// 默认 bridge 监听地址（Windows 本机 loopback，WSL 场景由脚本覆盖）。
const DEFAULT_BRIDGE_ADDR: &str = "127.0.0.1:0";

/// bridge token 文件默认路径（<HOME>/.callwarden/bridge.token）。
fn default_token_path() -> std::path::PathBuf {
    let home = std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .unwrap_or_default();
    std::path::PathBuf::from(home)
        .join(".callwarden")
        .join("bridge.token")
}

/// 读取并 trim bridge token（fail-closed：读取失败或为空时拒绝所有请求）。
fn read_bridge_token() -> std::io::Result<String> {
    let path = std::env::var("CW_BRIDGE_TOKEN_FILE")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| default_token_path());
    let content = std::fs::read_to_string(&path)?;
    let trimmed = content.trim().to_string();
    if trimmed.is_empty() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "bridge token 文件为空",
        ));
    }
    Ok(trimmed)
}

/// 校验客户端认证头，并在转发前剥离 `bridge_token` 字段。
fn validate_token(request: &mut Value, expected: &str) -> Result<(), String> {
    let obj = request
        .as_object_mut()
        .ok_or_else(|| "请求必须是 JSON object".to_string())?;
    let provided = obj
        .remove("bridge_token")
        .and_then(|v| v.as_str().map(str::to_owned))
        .unwrap_or_default();
    if provided != expected {
        return Err("E_BRIDGE_AUTH_FAILED".to_string());
    }
    Ok(())
}

/// 监听地址：CW_BRIDGE_LISTEN_ADDR 优先，其次 CW_BRIDGE_ENDPOINT，
/// 否则默认 127.0.0.1 随机端口。
///
/// 统一 endpoint 规范（复审 P1）：Python/manifest 使用 `tcp://host:port`，
/// Rust `TcpListener::bind` 需要裸 `host:port`。此处剥离 `tcp://` 前缀。
fn listen_addr() -> String {
    let raw = std::env::var("CW_BRIDGE_LISTEN_ADDR")
        .or_else(|_| std::env::var("CW_BRIDGE_ENDPOINT"))
        .unwrap_or_else(|_| DEFAULT_BRIDGE_ADDR.to_string());
    strip_tcp_scheme(&raw)
}

/// 剥离 `tcp://` 前缀（兼容 `tcp://host:port` 与裸 `host:port`）。
fn strip_tcp_scheme(endpoint: &str) -> String {
    endpoint.strip_prefix("tcp://").unwrap_or(endpoint).to_string()
}

/// 生成写入 manifest 的实际 endpoint。
///
/// `CW_BRIDGE_ENDPOINT` 是客户端应连接的地址；监听地址可以通过
/// `CW_BRIDGE_LISTEN_ADDR` 单独覆盖（例如 WSL 默认网关）。
fn advertised_endpoint(actual_port: u16) -> String {
    let raw = std::env::var("CW_BRIDGE_ENDPOINT")
        .unwrap_or_else(|_| format!("127.0.0.1:{actual_port}"));
    let endpoint = strip_tcp_scheme(raw.trim());
    let endpoint = if endpoint.ends_with(":0") {
        endpoint.trim_end_matches(":0").to_string() + &format!(":{actual_port}")
    } else {
        endpoint
    };
    format!("tcp://{endpoint}")
}

/// 构建结构化错误响应（与 daemon 错误帧格式一致）。
fn error_response(id: Option<&Value>, code: &str, message: String) -> Value {
    let mut m = Map::new();
    m.insert("ok".to_string(), Value::Bool(false));
    let mut err = Map::new();
    err.insert("code".to_string(), Value::String(code.to_string()));
    err.insert("message".to_string(), Value::String(message));
    m.insert("error".to_string(), Value::Object(err));
    if let Some(id) = id {
        m.insert("id".to_string(), id.clone());
    }
    Value::Object(m)
}

/// 构建成功响应（调用方已拿到 daemon result）。
fn ok_response(id: Option<&Value>, result: Value) -> Value {
    let mut m = Map::new();
    m.insert("ok".to_string(), Value::Bool(true));
    m.insert("result".to_string(), result);
    if let Some(id) = id {
        m.insert("id".to_string(), id.clone());
    }
    Value::Object(m)
}

/// 从请求中提取 method / params。
fn extract_method_params(request: &Value) -> Result<(String, Value), String> {
    let obj = request
        .as_object()
        .ok_or_else(|| "请求必须是 JSON object".to_string())?;
    let method = obj
        .get("method")
        .and_then(|m| m.as_str())
        .ok_or_else(|| "请求缺少 method".to_string())?
        .to_string();
    let params = obj.get("params").cloned().unwrap_or_else(|| Value::Object(Map::new()));
    Ok((method, params))
}

#[cfg(windows)]
fn authority_fields(ping: &Value) -> (String, String) {
    (
        ping.get("authority_id")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string(),
        ping.get("transport")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string(),
    )
}

#[cfg(windows)]
fn audit_log(request_id: Option<&Value>, authority_id: &str, code: &str) {
    let request_id = request_id
        .map(ToString::to_string)
        .unwrap_or_else(|| "none".to_string());
    println!(
        "[cw_bridge] authority_id={} transport=windows-bridge request_id={} code={}",
        authority_id, request_id, code
    );
}

/// 转发单个请求：从客户端读帧 → 校验 token → 转发到 daemon → 返回响应帧。
#[cfg(windows)]
fn handle_client(mut client: TcpStream, token: String) -> std::io::Result<()> {
    // 1. 读取客户端请求帧
    let mut request = recv_message(&mut client, DEFAULT_MAX_MESSAGE_BYTES).map_err(|e| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!("客户端请求解析失败: {e}"),
        )
    })?;
    let request_id = request.get("id").cloned();

    // 2. 校验 bridge token（fail-closed）
    if let Err(code) = validate_token(&mut request, &token) {
        let error = error_response(
            request_id.as_ref(),
            &code,
            "bridge 认证失败".to_string(),
        );
        let _ = send_message(&mut client, &error, DEFAULT_MAX_MESSAGE_BYTES);
        audit_log(request_id.as_ref(), "unknown", &code);
        return Ok(());
    }

    // 3. 提取 method / params
    let (method, params) = match extract_method_params(&request) {
        Ok(x) => x,
        Err(msg) => {
            let error = error_response(request_id.as_ref(), "invalid_params", msg);
            let _ = send_message(&mut client, &error, DEFAULT_MAX_MESSAGE_BYTES);
            audit_log(request_id.as_ref(), "unknown", "invalid_params");
            return Ok(());
        }
    };

    // 4. 连接 Windows daemon Named Pipe 并调用
    let daemon = match windows_pipe_client() {
        Ok(c) => c,
        Err(e) => {
            // E_AUTHORITY_UNAVAILABLE：Windows daemon 不可用时 fail-closed
            let error = error_response(
                request_id.as_ref(),
                "E_AUTHORITY_UNAVAILABLE",
                format!("Windows daemon 不可用: {e}; recovery=start Windows cw-daemon; fallback=forbidden"),
            );
            let _ = send_message(&mut client, &error, DEFAULT_MAX_MESSAGE_BYTES);
            audit_log(request_id.as_ref(), "unknown", "E_AUTHORITY_UNAVAILABLE");
            return Ok(());
        }
    };

    // 在转发业务请求前固定下游 authority，避免 bridge 只认证 token 却无法
    // 证明请求落到了预期的 Windows daemon。ping 是只读调用，不触碰任务或图谱。
    let (authority_id, _daemon_transport) = match daemon.call("ping", Value::Object(Map::new())) {
        Ok(ping) => authority_fields(&ping),
        Err(e) => {
            let error = error_response(
                request_id.as_ref(),
                "E_AUTHORITY_UNAVAILABLE",
                format!("Windows daemon 握手失败: {e}; fallback=forbidden"),
            );
            let _ = send_message(&mut client, &error, DEFAULT_MAX_MESSAGE_BYTES);
            audit_log(request_id.as_ref(), "unknown", "E_AUTHORITY_UNAVAILABLE");
            return Ok(());
        }
    };

    // 5. 转发请求到 daemon
    match daemon.call(&method, params) {
        Ok(result) => {
            let response = ok_response(request_id.as_ref(), result);
            send_message(&mut client, &response, DEFAULT_MAX_MESSAGE_BYTES).map_err(|e| {
                std::io::Error::new(std::io::ErrorKind::BrokenPipe, format!("返回客户端失败: {e}"))
            })?;
            audit_log(request_id.as_ref(), &authority_id, "ok");
        }
        Err(e) => {
            // 远端业务错误保持结构化错误码（DaemonRemoteError）
            let code = match &e {
                callwarden_core::daemon::client::ClientError::Remote(remote) => {
                    remote.code.clone()
                }
                _ => "bridge_forward_failed".to_string(),
            };
            let response = error_response(request_id.as_ref(), &code, e.to_string());
            send_message(&mut client, &response, DEFAULT_MAX_MESSAGE_BYTES).map_err(|e| {
                std::io::Error::new(std::io::ErrorKind::BrokenPipe, format!("返回客户端失败: {e}"))
            })?;
            audit_log(request_id.as_ref(), &authority_id, &code);
        }
    }
    Ok(())
}

/// 创建 Windows Named Pipe RPC client（连接当前用户 SID 的管道）。
#[cfg(windows)]
fn windows_pipe_client() -> Result<
    callwarden_core::daemon::client::windows::WindowsDaemonRpcClient,
    String,
> {
    let sid = callwarden_core::daemon::transport_windows::get_current_user_sid()
        .map_err(|e| format!("获取 SID 失败: {e}"))?;
    let pipe_name = format!(r"\\.\pipe\callwarden-{}", sid);
    Ok(callwarden_core::daemon::client::windows::WindowsDaemonRpcClient::new(&pipe_name))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_validate_token_ok() {
        let mut req = serde_json::json!({"id":1,"method":"ping","params":{},"bridge_token":"secret"});
        assert!(validate_token(&mut req, "secret").is_ok());
        // 转发前剥离 bridge_token
        assert!(req.get("bridge_token").is_none());
    }

    #[test]
    fn test_validate_token_rejects_wrong() {
        let mut req = serde_json::json!({"id":1,"method":"ping","params":{},"bridge_token":"wrong"});
        assert_eq!(validate_token(&mut req, "secret"), Err("E_BRIDGE_AUTH_FAILED".to_string()));
    }

    #[test]
    fn test_error_response_structured() {
        let resp = error_response(None, "E_AUTHORITY_UNAVAILABLE", "Windows daemon 不可用".to_string());
        let obj = resp.as_object().unwrap();
        assert_eq!(obj["ok"], Value::Bool(false));
        assert_eq!(obj["error"]["code"], "E_AUTHORITY_UNAVAILABLE");
    }

    #[test]
    fn test_extract_method_params_ok() {
        let req = serde_json::json!({"id":1,"method":"workspace.register","params":{"client_view_root":"/x"}});
        let (method, params) = extract_method_params(&req).unwrap();
        assert_eq!(method, "workspace.register");
        assert_eq!(params["client_view_root"], "/x");
    }

    #[test]
    fn test_extract_method_params_missing_method() {
        let req = serde_json::json!({"id":1,"params":{}});
        assert!(extract_method_params(&req).is_err());
    }

    #[test]
    fn test_strip_tcp_scheme_removes_prefix() {
        // 复审 P1：tcp:// 前缀必须剥离，TcpListener::bind 需要裸 host:port
        assert_eq!(strip_tcp_scheme("tcp://127.0.0.1:8456"), "127.0.0.1:8456");
        assert_eq!(strip_tcp_scheme("127.0.0.1:8456"), "127.0.0.1:8456");
        assert_eq!(strip_tcp_scheme("tcp://0.0.0.0:9000"), "0.0.0.0:9000");
        assert_eq!(strip_tcp_scheme(DEFAULT_BRIDGE_ADDR), DEFAULT_BRIDGE_ADDR);
    }
}

/// 单次连接的转发流程（含连接级错误处理）。
#[cfg(windows)]
fn serve_client(client: TcpStream, token: String) {
    if let Err(e) = handle_client(client, token) {
        eprintln!("[cw_bridge] 连接处理失败: {e}");
    }
}

fn main() {
    #[cfg(windows)]
    {
        let token = match read_bridge_token() {
            Ok(t) => t,
            Err(e) => {
                eprintln!("[cw_bridge] [ERROR] bridge token 读取失败: {e}");
                std::process::exit(1);
            }
        };

        let addr = listen_addr();
        let listener = match TcpListener::bind(&addr) {
            Ok(l) => l,
            Err(e) => {
                eprintln!("[cw_bridge] [ERROR] 监听失败 {addr}: {e}");
                std::process::exit(1);
            }
        };
        let actual = listener.local_addr().expect("bridge local addr");
        println!(
            "[cw_bridge] listening on {} (WSL bridge → Windows Named Pipe), authority=windows-host",
            actual
        );

        // 端点发现（P1 修复）：将实际端口写入 manifest，供 WSL 客户端发现。
        // 优先 CW_BRIDGE_MANIFEST 环境变量，否则 <HOME>/.callwarden/bridge.manifest.json。
        write_bridge_manifest(actual.port(), &token);

        for stream in listener.incoming() {
            match stream {
                Ok(client) => {
                    let token = token.clone();
                    thread::spawn(move || serve_client(client, token));
                }
                Err(e) => eprintln!("[cw_bridge] 接受连接失败: {e}"),
            }
        }
    }
    #[cfg(not(windows))]
    {
        eprintln!(
            "[cw_bridge] cw-bridge 是 Windows-only 组件；非 Windows 平台请使用 UDS 直连本地 daemon"
        );
        std::process::exit(1);
    }
}

/// 将 bridge 端点信息写入 manifest（P1：默认随机端口的发现机制）。
///
/// manifest 路径：`CW_BRIDGE_MANIFEST` 或 `<HOME>/.callwarden/bridge.manifest.json`。
/// 内容：`{"endpoint": "tcp://<advertised-host>:<port>", "authority": "windows-host",
///        "token_file": "<token path>"}`。
/// 写入失败仅告警（不阻止 bridge 运行），但 WSL 客户端将无法自动发现端点。
fn write_bridge_manifest(port: u16, _token: &str) {
    use std::path::PathBuf;
    let path = std::env::var("CW_BRIDGE_MANIFEST")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            let home = std::env::var_os("USERPROFILE")
                .or_else(|| std::env::var_os("HOME"))
                .unwrap_or_default();
            PathBuf::from(home)
                .join(".callwarden")
                .join("bridge.manifest.json")
        });
    let token_file = std::env::var("CW_BRIDGE_TOKEN_FILE")
        .map(PathBuf::from)
        .unwrap_or_else(|_| default_token_path());
    let manifest = serde_json::json!({
        "endpoint": advertised_endpoint(port),
        "authority": "windows-host",
        "transport": "windows-bridge",
        "token_file": token_file.to_string_lossy(),
    });
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            let _ = std::fs::create_dir_all(parent);
        }
    }
    // P1 fail-closed：默认端点（127.0.0.1:0 随机端口）时，manifest 写入失败必须
    // 阻止 bridge 宣称可用（WSL 客户端无法发现随机端口）。显式 CW_BRIDGE_ENDPOINT
    // 时写入失败仅告警（端口已知，客户端可显式配置）。
    let explicit_endpoint = std::env::var("CW_BRIDGE_ENDPOINT")
        .map(|v| !v.trim().is_empty())
        .unwrap_or(false);
    match std::fs::write(&path, serde_json::to_string_pretty(&manifest).unwrap_or_default()) {
        Ok(_) => println!("[cw_bridge] manifest written to {}", path.display()),
        Err(e) => {
            if explicit_endpoint {
                eprintln!(
                    "[cw_bridge] [WARN] 无法写入 bridge manifest {}: {e}（显式 endpoint，WSL 可手动配置）",
                    path.display()
                );
            } else {
                eprintln!(
                    "[cw_bridge] [ERROR] 随机端口模式下无法写入 bridge manifest {}: {e}; \
                     拒绝启动（fail-closed，WSL 无法发现端点）",
                    path.display()
                );
                std::process::exit(1);
            }
        }
    }
}
