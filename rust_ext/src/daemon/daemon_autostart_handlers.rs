//! daemon autostart 面 handler（SRV-005：server daemon autostart Python authority → Rust daemon）。
//!
//! 对应 `server/daemon_autostart.py` 中三个直接执行 socket connect 的 Python authority 函数：
//! - `_try_connect_tcp`：TCP bridge 连通探测（`tcp://host:port` 或裸 `host:port`）；
//! - `_try_connect_unix`：Unix Domain Socket 连通探测；
//! - `try_http_connect`：HTTP endpoint 短超时连通探针（urlparse 等价解析 + TCP connect）。
//!
//! RPC 无法传递 socket 对象，下沉后统一为「connect + 立即关闭」的**探测语义**：
//! 成功返回 `connectable=true`，失败返回 `connectable=false` + 稳定 `error` 字段
//! （对齐 Python 返回 None/False 的 fail-soft 语义，不抛传输异常）。
//! 全部 handler 只读、无 DB、无写锁；endpoint 缺失返回 `invalid_params`（stable errors），
//! endpoint 格式非法/连接失败返回 `connectable=false`（对齐 Python 返回 None 不抛异常）。
//!
//! 注意：Python 客户端连接 daemon 自身的 transport 连接（`try_connect` 返回 socket
//! 供后续帧通信）无法经 RPC 替代——transport bootstrap 保留在 Python 侧，
//! 本组 handler 承载健康探针/bridge 探测 authority。

use std::net::ToSocketAddrs;
use std::time::Duration;

use serde_json::{json, Value};

use super::dispatch::{require_str_param, DaemonRpcError};

/// TCP/UDS 探测默认超时（秒），对齐 Python `CONNECT_TIMEOUT = 1.0`。
const DEFAULT_CONNECT_TIMEOUT_SECS: f64 = 1.0;
/// HTTP 探针默认超时（秒），对齐 Python `try_http_connect(timeout=2.0)`。
const DEFAULT_HTTP_TIMEOUT_SECS: f64 = 2.0;

/// 提取可选正浮点超时参数（缺失/非数字/非正数使用默认值，对齐 Python 默认参数语义）。
fn get_timeout(params: &Value, default_secs: f64) -> Duration {
    let secs = params
        .get("timeout")
        .and_then(|v| v.as_f64())
        .filter(|t| t.is_finite() && *t > 0.0)
        .unwrap_or(default_secs);
    Duration::from_secs_f64(secs)
}

/// 解析 TCP endpoint：`tcp://host:port` 或裸 `host:port`（对齐 Python
/// `_try_connect_tcp`：removeprefix("tcp://") + rpartition(":") + port.isdigit 校验）。
/// 格式非法返回 None（对齐 Python 返回 None）。
fn parse_tcp_endpoint(endpoint: &str) -> Option<(String, u16)> {
    let host_port = endpoint.strip_prefix("tcp://").unwrap_or(endpoint);
    let (host, port_str) = host_port.rsplit_once(':')?;
    if host.is_empty() {
        return None;
    }
    // Python `port.isdigit()`：纯数字且 int() 可容纳
    let port: u16 = port_str.parse().ok()?;
    if !port_str.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    Some((host.to_string(), port))
}

/// 将 host:port 解析为 SocketAddr（对齐 Python socket.connect 的 DNS 解析语义；
/// 解析失败返回 None）。
fn resolve_addr(host: &str, port: u16) -> Option<std::net::SocketAddr> {
    (host, port).to_socket_addrs().ok()?.next()
}

/// TCP bridge 连通探测（对应 Python `_try_connect_tcp`）。
///
/// params:
/// - `endpoint`（必填）：`tcp://host:port` 或裸 `host:port`；
/// - `timeout`（可选，秒，默认 1.0）。
///
/// 返回 `{"connectable": bool, "host": str, "port": int, "error": str|null}`。
pub fn handle_try_connect_tcp(params: &Value) -> Result<Value, DaemonRpcError> {
    let endpoint = require_str_param(params, "endpoint")?;
    let timeout = get_timeout(params, DEFAULT_CONNECT_TIMEOUT_SECS);

    let Some((host, port)) = parse_tcp_endpoint(endpoint) else {
        // 对齐 Python：格式非法返回 None（不抛异常），下沉为 connectable=false
        return Ok(json!({
            "connectable": false,
            "endpoint": endpoint,
            "error": "invalid tcp endpoint: expect tcp://host:port or host:port",
        }));
    };

    let result = resolve_addr(&host, port)
        .and_then(|addr| std::net::TcpStream::connect_timeout(&addr, timeout).ok());
    let connectable = result.is_some();
    // 探测语义：连接成功立即关闭（对齐 SRV-004 open_readonly_conn 探测模式）
    drop(result);

    Ok(json!({
        "connectable": connectable,
        "host": host,
        "port": port,
        "error": if connectable { Value::Null } else { json!("connect failed or unreachable") },
    }))
}

/// Unix Domain Socket 连通探测（对应 Python `_try_connect_unix`）。
///
/// params:
/// - `endpoint`（必填）：UDS 路径；
/// - `timeout`（可选，秒，默认 1.0）。
///
/// 返回 `{"connectable": bool, "endpoint": str, "error": str|null}`。
/// 非 unix 平台对齐 Python `not hasattr(socket, "AF_UNIX")` → 返回 connectable=false。
pub fn handle_try_connect_unix(params: &Value) -> Result<Value, DaemonRpcError> {
    let endpoint = require_str_param(params, "endpoint")?;
    let timeout = get_timeout(params, DEFAULT_CONNECT_TIMEOUT_SECS);

    #[cfg(unix)]
    {
        use std::os::unix::net::{SocketAddr as UnixSockAddr, UnixStream};
        let result = UnixSockAddr::from_path(std::path::Path::new(endpoint))
            .ok()
            .and_then(|addr| UnixStream::connect_timeout(&addr, timeout).ok());
        let connectable = result.is_some();
        drop(result);
        Ok(json!({
            "connectable": connectable,
            "endpoint": endpoint,
            "error": if connectable { Value::Null } else { json!("connect failed or unreachable") },
        }))
    }
    #[cfg(not(unix))]
    {
        let _ = timeout;
        // 对齐 Python：无 AF_UNIX 直接返回 None（不抛异常）
        Ok(json!({
            "connectable": false,
            "endpoint": endpoint,
            "error": "AF_UNIX not available on this platform",
        }))
    }
}

/// 解析 HTTP endpoint（对齐 Python `urlparse(endpoint).hostname or "127.0.0.1"`、
/// `.port or 80`）。仅提取 host/port 做 TCP 级探针，格式非法返回 None。
fn parse_http_endpoint(endpoint: &str) -> Option<(String, u16)> {
    // 等价 urlparse：scheme://netloc/path；无 scheme 时整体按 netloc 解析
    let after_scheme = endpoint
        .split_once("://")
        .map(|(_, rest)| rest)
        .unwrap_or(endpoint);
    let authority = after_scheme.split(['/', '?', '#']).next().unwrap_or("");
    // 去 userinfo（user:pass@host）
    let hostport = authority.rsplit_once('@').map(|(_, h)| h).unwrap_or(authority);
    // IPv6 字面量 [::1]:port
    let (host, port_str) = if let Some(bracket_end) = hostport.find(']') {
        let host = &hostport[..=bracket_end];
        let rest = &hostport[bracket_end + 1..];
        let port = rest.strip_prefix(':');
        (host.trim_matches(['[', ']']), port)
    } else {
        match hostport.rsplit_once(':') {
            Some((h, p)) => (h, Some(p)),
            None => (hostport, None),
        }
    };
    let host = if host.is_empty() { "127.0.0.1" } else { host };
    let port: u16 = match port_str {
        Some(p) => p.parse().ok()?,
        None => 80,
    };
    Some((host.to_string(), port))
}

/// HTTP endpoint 短超时连通探针（对应 Python `try_http_connect`：
/// urlparse → host 默认 127.0.0.1、port 默认 80 → TCP connect + close → bool）。
///
/// params:
/// - `endpoint`（必填）：HTTP endpoint（如 `http://127.0.0.1:8456`）；
/// - `timeout`（可选，秒，默认 2.0）。
///
/// 返回 `{"connectable": bool}`（对齐 Python 返回 bool）。
pub fn handle_try_http_connect(params: &Value) -> Result<Value, DaemonRpcError> {
    let endpoint = require_str_param(params, "endpoint")?;
    let timeout = get_timeout(params, DEFAULT_HTTP_TIMEOUT_SECS);

    let Some((host, port)) = parse_http_endpoint(endpoint) else {
        // 对齐 Python fail-soft：探针失败返回 False（urlparse 端口非法等价不可达）
        return Ok(json!({ "connectable": false }));
    };

    let connectable = resolve_addr(&host, port)
        .and_then(|addr| std::net::TcpStream::connect_timeout(&addr, timeout).ok())
        .is_some();
    Ok(json!({ "connectable": connectable }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// 起一个本地临时 TcpListener，返回其端口（用于成功路径测试）。
    fn temp_listener() -> (std::net::TcpListener, u16) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        (listener, port)
    }

    // ---------- parse_tcp_endpoint ----------

    #[test]
    fn parse_tcp_endpoint_variants() {
        assert_eq!(
            parse_tcp_endpoint("tcp://127.0.0.1:8456"),
            Some(("127.0.0.1".to_string(), 8456))
        );
        assert_eq!(
            parse_tcp_endpoint("127.0.0.1:8456"),
            Some(("127.0.0.1".to_string(), 8456))
        );
        assert_eq!(parse_tcp_endpoint("tcp://:8456"), None); // 空 host
        assert_eq!(parse_tcp_endpoint("tcp://host:abc"), None); // 非数字端口
        assert_eq!(parse_tcp_endpoint("no-port"), None); // 无冒号
    }

    // ---------- handle_try_connect_tcp ----------

    #[test]
    fn try_connect_tcp_success_and_fail() {
        let (_listener, port) = temp_listener();
        let ok = handle_try_connect_tcp(&json!({ "endpoint": format!("tcp://127.0.0.1:{port}") }))
            .unwrap();
        assert_eq!(ok.get("connectable"), Some(&json!(true)));
        assert_eq!(ok.get("port"), Some(&json!(port)));
        assert_eq!(ok.get("error"), Some(&Value::Null));

        drop(_listener);
        // 释放后不可达（连接被拒）
        let fail = handle_try_connect_tcp(
            &json!({ "endpoint": format!("tcp://127.0.0.1:{port}"), "timeout": 0.5 }),
        )
        .unwrap();
        assert_eq!(fail.get("connectable"), Some(&json!(false)));
        assert!(fail.get("error").unwrap().is_string());
    }

    #[test]
    fn try_connect_tcp_invalid_endpoint_is_fail_soft() {
        let r = handle_try_connect_tcp(&json!({ "endpoint": "tcp://host:abc" })).unwrap();
        assert_eq!(r.get("connectable"), Some(&json!(false)));
        assert!(r.get("error").unwrap().is_string());
    }

    #[test]
    fn try_connect_tcp_missing_endpoint_is_invalid_params() {
        let err = handle_try_connect_tcp(&json!({})).unwrap_err();
        assert!(err.message.contains("endpoint"));
    }

    // ---------- handle_try_connect_unix ----------

    #[test]
    fn try_connect_unix_unreachable_or_platform_unavailable() {
        let r = handle_try_connect_unix(&json!({
            "endpoint": "/nonexistent-callwarden-test.sock",
            "timeout": 0.5
        }))
        .unwrap();
        // unix：路径不存在连接失败；非 unix：AF_UNIX 不可用。两者均 fail-soft false。
        assert_eq!(r.get("connectable"), Some(&json!(false)));
        assert!(r.get("error").unwrap().is_string());
    }

    #[test]
    fn try_connect_unix_missing_endpoint_is_invalid_params() {
        let err = handle_try_connect_unix(&json!({})).unwrap_err();
        assert!(err.message.contains("endpoint"));
    }

    // ---------- parse_http_endpoint ----------

    #[test]
    fn parse_http_endpoint_variants() {
        assert_eq!(
            parse_http_endpoint("http://127.0.0.1:8456/health"),
            Some(("127.0.0.1".to_string(), 8456))
        );
        assert_eq!(
            parse_http_endpoint("http://localhost"),
            Some(("localhost".to_string(), 80))
        );
        assert_eq!(
            parse_http_endpoint("http://"),
            Some(("127.0.0.1".to_string(), 80)) // host 空 → 默认 127.0.0.1
        );
        assert_eq!(
            parse_http_endpoint("http://user:pass@10.0.0.1:9999/x"),
            Some(("10.0.0.1".to_string(), 9999))
        );
        assert_eq!(parse_http_endpoint("http://host:abc"), None); // 端口非数字
    }

    // ---------- handle_try_http_connect ----------

    #[test]
    fn try_http_connect_success_and_fail() {
        let (_listener, port) = temp_listener();
        let ok = handle_try_http_connect(&json!({ "endpoint": format!("http://127.0.0.1:{port}") }))
            .unwrap();
        assert_eq!(ok.get("connectable"), Some(&json!(true)));

        drop(_listener);
        let fail = handle_try_http_connect(
            &json!({ "endpoint": format!("http://127.0.0.1:{port}"), "timeout": 0.5 }),
        )
        .unwrap();
        assert_eq!(fail.get("connectable"), Some(&json!(false)));
    }

    #[test]
    fn try_http_connect_missing_endpoint_is_invalid_params() {
        let err = handle_try_http_connect(&json!({})).unwrap_err();
        assert!(err.message.contains("endpoint"));
    }
}
