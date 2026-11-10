//! Enterprise daemon 的有界 JSON-RPC 协议层（HTTPS + mTLS 单轨，ADR-002）。
//!
//! 协议格式：4 字节大端 header（payload 长度）+ UTF-8 JSON payload。
//!
//! 参考：server/daemon_protocol.py（Python 权威实现）。
//! 本模块为 Rust 等价实现，供 cw_daemon binary 与 Python daemon 互通。

use serde_json::{Map, Value};
use std::io::{self, Read, Write};

/// 4 字节大端 header（u32）
pub const HEADER_SIZE: usize = 4;

/// 默认最大消息字节数（8 MB），与 Python 一致
pub const DEFAULT_MAX_MESSAGE_BYTES: usize = 8 * 1024 * 1024;

/// IPC 帧或 JSON 请求不合法时返回的错误
#[derive(Debug, thiserror::Error)]
pub enum ProtocolError {
    #[error("连接在消息接收完成前关闭")]
    ConnectionClosed,
    #[error("消息必须是 JSON object")]
    NotJsonObject,
    #[error("非法消息长度: {0}")]
    InvalidMessageSize(u32),
    #[error("消息超过限制: {actual} > {limit}")]
    MessageTooLarge { actual: usize, limit: usize },
    #[error("JSON 解码失败: {0}")]
    JsonDecode(#[from] serde_json::Error),
    #[error("UTF-8 解码失败: {0}")]
    Utf8(#[from] std::string::FromUtf8Error),
    #[error("IO 错误: {0}")]
    Io(#[from] io::Error),
    #[error("duplicate JSON key: {0}")]
    DuplicateJsonKey(String),
    #[error("{0}")]
    Other(String),
}

/// daemon 返回的结构化远端错误
#[derive(Debug, Clone)]
pub struct DaemonRemoteError {
    pub code: String,
    pub message: String,
}

impl std::fmt::Display for DaemonRemoteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for DaemonRemoteError {}

/// 精确读取指定字节数，遇到 EOF 报 ConnectionClosed
fn recv_exact<R: Read>(reader: &mut R, size: usize) -> Result<Vec<u8>, ProtocolError> {
    let mut buf = vec![0u8; size];
    let mut read = 0;
    while read < size {
        let n = reader.read(&mut buf[read..])?;
        if n == 0 {
            return Err(ProtocolError::ConnectionClosed);
        }
        read += n;
    }
    Ok(buf)
}

/// 解析 4 字节大端 header 为 u32
fn parse_header(header: &[u8]) -> Result<u32, ProtocolError> {
    if header.len() < HEADER_SIZE {
        return Err(ProtocolError::Other(format!(
            "header 长度不足: {} < {}",
            header.len(),
            HEADER_SIZE
        )));
    }
    let mut arr = [0u8; 4];
    arr.copy_from_slice(&header[..HEADER_SIZE]);
    Ok(u32::from_be_bytes(arr))
}

/// 将消息编码为 JSON payload 字节
fn encode_message(message: &Value) -> Result<Vec<u8>, ProtocolError> {
    if !message.is_object() {
        return Err(ProtocolError::NotJsonObject);
    }
    // 与 Python 一致：ensure_ascii=False, separators=(",", ":")
    let payload = serde_json::to_vec(message)?;
    Ok(payload)
}

/// 发送单个长度分帧 JSON 对象
///
/// 协议：[4 字节大端 payload 长度][UTF-8 JSON payload]
pub fn send_message<W: Write>(
    writer: &mut W,
    message: &Value,
    max_bytes: usize,
) -> Result<(), ProtocolError> {
    if !message.is_object() {
        return Err(ProtocolError::NotJsonObject);
    }
    let payload = encode_message(message)?;
    if payload.len() > max_bytes {
        return Err(ProtocolError::MessageTooLarge {
            actual: payload.len(),
            limit: max_bytes,
        });
    }
    let len = payload.len() as u32;
    writer.write_all(&len.to_be_bytes())?;
    writer.write_all(&payload)?;
    writer.flush()?;
    Ok(())
}

/// 接收单个长度分帧 JSON 对象
pub fn recv_message<R: Read>(reader: &mut R, max_bytes: usize) -> Result<Value, ProtocolError> {
    let header = recv_exact(reader, HEADER_SIZE)?;
    let size = parse_header(&header)?;
    if size == 0 || size as usize > max_bytes {
        return Err(ProtocolError::InvalidMessageSize(size));
    }
    let payload = recv_exact(reader, size as usize)?;
    let s = String::from_utf8(payload)?;
    // 任务 1D2（Req 15/AC26）：frame bytes 先经 strict duplicate-key parser，在转成
    // `Value` 前检测重复 key；命中时 fail-closed 返回稳定 E_DUPLICATE_JSON_KEY。
    if let Err(e) = crate::daemon::task_loop::strict_transport::parse_strict_envelope(s.as_bytes())
    {
        if e.code == crate::daemon::task_loop::strict_transport::ERR_DUPLICATE_JSON_KEY {
            return Err(ProtocolError::DuplicateJsonKey(e.message));
        }
    }
    let message: Value = serde_json::from_str(&s)?;
    if !message.is_object() {
        return Err(ProtocolError::NotJsonObject);
    }
    Ok(message)
}

/// 解析 RPC 响应，远端错误转换为 DaemonRemoteError
///
/// 响应格式：
/// - 成功：`{"ok": true, "result": <data>}`
/// - 失败：`{"ok": false, "error": {"code": "...", "message": "..."}}`
pub fn parse_response(response: &Value) -> Result<Value, DaemonRemoteError> {
    let ok = response
        .get("ok")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    if ok {
        Ok(response.get("result").cloned().unwrap_or(Value::Null))
    } else {
        let error = response.get("error");
        let (code, message) = if let Some(err_obj) = error.and_then(|v| v.as_object()) {
            let code = err_obj
                .get("code")
                .and_then(|v| v.as_str())
                .unwrap_or("daemon_error")
                .to_string();
            let message = err_obj
                .get("message")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown daemon error")
                .to_string();
            (code, message)
        } else {
            (
                "daemon_error".to_string(),
                "unknown daemon error".to_string(),
            )
        };
        Err(DaemonRemoteError { code, message })
    }
}

/// 构造成功响应
pub fn make_ok_response(result: Value) -> Value {
    let mut m = Map::new();
    m.insert("ok".to_string(), Value::Bool(true));
    m.insert("result".to_string(), result);
    Value::Object(m)
}

/// 构造失败响应
pub fn make_error_response(code: &str, message: &str) -> Value {
    let mut err = Map::new();
    err.insert("code".to_string(), Value::String(code.to_string()));
    err.insert("message".to_string(), Value::String(message.to_string()));
    let mut m = Map::new();
    m.insert("ok".to_string(), Value::Bool(false));
    m.insert("error".to_string(), Value::Object(err));
    Value::Object(m)
}

// ============================================
// canonical_bytes_b64 载荷校验（Req 14.10，跨平台）
// ============================================

/// canonical_bytes_b64 默认最大载荷字节数（64 MB）
pub const DEFAULT_MAX_CANONICAL_BYTES: usize = 64 * 1024 * 1024;

/// 校验 base64 编码的 canonical bytes 载荷（Req 14.10）。
///
/// 单轨 HTTPS+mTLS 传输（ADR-002）不支持 FD 传递，客户端通过
/// `canonical_bytes_b64` 参数直接提交规范化字节。使用前必须校验：
/// 1. base64 解码成功
/// 2. 解码后尺寸不超过 `max_bytes` 配置上限
/// 3. 若请求声明了 `expected_sha256`，实际内容 SHA-256 必须匹配
///
/// 任一校验失败返回 Err(DaemonRpcError)（Structured_Reason），不得按"尽力解析"继续。
///
/// 参数：
/// - `b64_payload`: base64 编码的载荷字符串
/// - `max_bytes`: 解码后允许的最大字节数
/// - `expected_sha256`: 可选的声明摘要（hex 小写），None 时跳过摘要校验
///
/// 返回解码后的原始字节。
pub fn validate_canonical_bytes_b64(
    b64_payload: &str,
    max_bytes: usize,
    expected_sha256: Option<&str>,
) -> Result<Vec<u8>, super::dispatch::DaemonRpcError> {
    use super::dispatch::DaemonRpcError;

    // 1. base64 解码
    let bytes = base64::Engine::decode(
        &base64::engine::general_purpose::STANDARD,
        b64_payload,
    )
    .map_err(|e| {
        DaemonRpcError::new(
            "invalid_params",
            format!("canonical_bytes_b64 解码失败: {}", e),
        )
    })?;

    // 2. 尺寸校验
    if bytes.len() > max_bytes {
        return Err(DaemonRpcError::new(
            "payload_too_large",
            format!(
                "canonical_bytes 载荷超过上限: {} > {} bytes",
                bytes.len(),
                max_bytes
            ),
        ));
    }

    // 3. 摘要校验（若声明）
    if let Some(expected) = expected_sha256 {
        if !expected.is_empty() {
            use sha2::{Digest, Sha256};
            let mut hasher = Sha256::new();
            hasher.update(&bytes);
            let actual = format!("{:x}", hasher.finalize());
            if actual != expected.to_lowercase() {
                return Err(DaemonRpcError::new(
                    "digest_mismatch",
                    format!(
                        "canonical_bytes 摘要不匹配: expected={}, actual={}",
                        expected, actual
                    ),
                ));
            }
        }
    }

    Ok(bytes)
}

// ============================================
// 单元测试（跨平台，纯逻辑）
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    /// 验证长度分帧 JSON roundtrip
    #[test]
    fn test_framed_json_roundtrip() {
        let msg = serde_json::json!({
            "method": "ping",
            "params": {"workspace": "test"}
        });
        let mut buf = Vec::new();
        send_message(&mut buf, &msg, DEFAULT_MAX_MESSAGE_BYTES).unwrap();
        let mut cursor = Cursor::new(buf);
        let received = recv_message(&mut cursor, DEFAULT_MAX_MESSAGE_BYTES).unwrap();
        assert_eq!(received, msg);
    }

    /// 验证 oversized frame 拒绝
    #[test]
    fn test_oversized_frame_rejected() {
        let msg = serde_json::json!({"data": "x".repeat(100)});
        let mut buf = Vec::new();
        // 用小 max_bytes 发送应失败
        let result = send_message(&mut buf, &msg, 50);
        match result {
            Err(ProtocolError::MessageTooLarge { .. }) => (),
            _ => panic!("期望 MessageTooLarge 错误，实际: {:?}", result),
        }
    }

    /// 验证 malformed JSON 拒绝
    #[test]
    fn test_malformed_json_rejected() {
        // 构造一个非法 JSON payload
        let bad_payload = b"{not valid json";
        let mut buf = Vec::new();
        buf.extend_from_slice(&(bad_payload.len() as u32).to_be_bytes());
        buf.extend_from_slice(bad_payload);
        let mut cursor = Cursor::new(buf);
        let result = recv_message(&mut cursor, DEFAULT_MAX_MESSAGE_BYTES);
        match result {
            Err(ProtocolError::JsonDecode(_)) => (),
            _ => panic!("期望 JsonDecode 错误，实际: {:?}", result),
        }
    }

    /// 任务 1D2：duplicate JSON key 在 frame bytes 边界被拒绝（稳定 E_DUPLICATE_JSON_KEY）
    #[test]
    fn test_duplicate_json_key_rejected() {
        let payload = br#"{"method":"ping","method":"pong"}"#;
        let mut buf = Vec::new();
        buf.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        buf.extend_from_slice(payload);
        let mut cursor = Cursor::new(buf);
        let result = recv_message(&mut cursor, DEFAULT_MAX_MESSAGE_BYTES);
        match result {
            Err(ProtocolError::DuplicateJsonKey(msg)) => {
                assert!(msg.contains("method"), "msg: {}", msg);
            }
            _ => panic!("期望 DuplicateJsonKey 错误，实际: {:?}", result),
        }
    }

    /// 任务 1D2：同 key 的正常重试（不同字段名）不受影响
    #[test]
    fn test_distinct_keys_still_ok() {
        let payload = br#"{"id":"1","method":"ping","params":{"a":1,"A":2}}"#;
        let mut buf = Vec::new();
        buf.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        buf.extend_from_slice(payload);
        let mut cursor = Cursor::new(buf);
        let received = recv_message(&mut cursor, DEFAULT_MAX_MESSAGE_BYTES).unwrap();
        assert_eq!(received["method"], "ping");
        assert_eq!(received["params"]["a"], 1);
        assert_eq!(received["params"]["A"], 2);
    }

    /// 验证非 object 消息被拒绝
    #[test]
    fn test_non_object_rejected() {
        let msg = serde_json::json!([1, 2, 3]); // 数组
        let mut buf = Vec::new();
        let result = send_message(&mut buf, &msg, DEFAULT_MAX_MESSAGE_BYTES);
        match result {
            Err(ProtocolError::NotJsonObject) => (),
            _ => panic!("期望 NotJsonObject 错误"),
        }
    }

    /// 验证 size=0 被拒绝
    #[test]
    fn test_zero_size_rejected() {
        let buf = vec![0u8; 4]; // size=0
        let mut cursor = Cursor::new(buf);
        let result = recv_message(&mut cursor, DEFAULT_MAX_MESSAGE_BYTES);
        match result {
            Err(ProtocolError::InvalidMessageSize(0)) => (),
            _ => panic!("期望 InvalidMessageSize(0) 错误"),
        }
    }

    /// 验证 size 超过 max_bytes 被拒绝
    #[test]
    fn test_size_exceeds_max_rejected() {
        let mut buf = Vec::new();
        // 写入一个超过 max_bytes 的 size
        buf.extend_from_slice(&100u32.to_be_bytes());
        let mut cursor = Cursor::new(buf);
        let result = recv_message(&mut cursor, 50);
        match result {
            Err(ProtocolError::InvalidMessageSize(100)) => (),
            _ => panic!("期望 InvalidMessageSize(100) 错误"),
        }
    }

    /// 验证连接关闭时返回 ConnectionClosed
    #[test]
    fn test_connection_closed() {
        let buf = vec![]; // 空数据
        let mut cursor = Cursor::new(buf);
        let result = recv_message(&mut cursor, DEFAULT_MAX_MESSAGE_BYTES);
        match result {
            Err(ProtocolError::ConnectionClosed) => (),
            _ => panic!("期望 ConnectionClosed 错误"),
        }
    }

    /// 验证 parse_response 成功路径
    #[test]
    fn test_parse_response_ok() {
        let response = serde_json::json!({
            "ok": true,
            "result": {"count": 42}
        });
        let result = parse_response(&response).unwrap();
        assert_eq!(result["count"], 42);
    }

    /// 验证 parse_response 失败路径
    #[test]
    fn test_parse_response_error() {
        let response = serde_json::json!({
            "ok": false,
            "error": {"code": "PERMISSION_DENIED", "message": "workspace not owned"}
        });
        let result = parse_response(&response);
        match result {
            Err(e) => {
                assert_eq!(e.code, "PERMISSION_DENIED");
                assert_eq!(e.message, "workspace not owned");
            }
            _ => panic!("期望 DaemonRemoteError"),
        }
    }

    /// 验证 parse_response 缺失 error 字段的兜底
    #[test]
    fn test_parse_response_missing_error() {
        let response = serde_json::json!({"ok": false});
        let result = parse_response(&response);
        match result {
            Err(e) => {
                assert_eq!(e.code, "daemon_error");
                assert_eq!(e.message, "unknown daemon error");
            }
            _ => panic!("期望 DaemonRemoteError"),
        }
    }

    /// 验证 make_ok_response 构造正确
    #[test]
    fn test_make_ok_response() {
        let response = make_ok_response(serde_json::json!({"status": "ok"}));
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["status"], "ok");
    }

    /// 验证 make_error_response 构造正确
    #[test]
    fn test_make_error_response() {
        let response = make_error_response("INVALID_PARAMS", "missing field: method");
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "INVALID_PARAMS");
        assert_eq!(response["error"]["message"], "missing field: method");
    }

    /// 验证 UTF-8 多字节字符正确处理
    #[test]
    fn test_utf8_multibyte() {
        let msg = serde_json::json!({"message": "你好世界，Call Warden！🌍"});
        let mut buf = Vec::new();
        send_message(&mut buf, &msg, DEFAULT_MAX_MESSAGE_BYTES).unwrap();
        let mut cursor = Cursor::new(buf);
        let received = recv_message(&mut cursor, DEFAULT_MAX_MESSAGE_BYTES).unwrap();
        assert_eq!(received["message"], "你好世界，Call Warden！🌍");
    }

    /// 验证大消息（接近 max_bytes）
    #[test]
    fn test_large_message() {
        let large_data = "x".repeat(1024 * 1024); // 1 MB
        let msg = serde_json::json!({"data": large_data});
        let mut buf = Vec::new();
        send_message(&mut buf, &msg, DEFAULT_MAX_MESSAGE_BYTES).unwrap();
        let mut cursor = Cursor::new(buf);
        let received = recv_message(&mut cursor, DEFAULT_MAX_MESSAGE_BYTES).unwrap();
        assert_eq!(received["data"].as_str().unwrap().len(), 1024 * 1024);
    }

    /// 验证多消息连续发送/接收
    #[test]
    fn test_multiple_messages() {
        let msgs = vec![
            serde_json::json!({"seq": 1, "method": "ping"}),
            serde_json::json!({"seq": 2, "method": "query"}),
            serde_json::json!({"seq": 3, "method": "close"}),
        ];
        let mut buf = Vec::new();
        for msg in &msgs {
            send_message(&mut buf, msg, DEFAULT_MAX_MESSAGE_BYTES).unwrap();
        }
        let mut cursor = Cursor::new(buf);
        for expected in &msgs {
            let received = recv_message(&mut cursor, DEFAULT_MAX_MESSAGE_BYTES).unwrap();
            assert_eq!(received, *expected);
        }
    }
}

// ============================================
// canonical_bytes_b64 测试（Req 14.10，跨平台）
// ============================================

#[cfg(test)]
mod canonical_b64_tests {
    use super::*;

    #[test]
    fn test_canonical_bytes_b64_valid_payload() {
        use base64::Engine;
        let content = b"pub fn main() {}\n";
        let b64 = base64::engine::general_purpose::STANDARD.encode(content);
        let result = validate_canonical_bytes_b64(&b64, 1024, None);
        assert!(result.is_ok());
        assert_eq!(result.unwrap(), content);
    }

    #[test]
    fn test_canonical_bytes_b64_invalid_base64() {
        let result = validate_canonical_bytes_b64("not-valid-b64!!!", 1024, None);
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "invalid_params");
    }

    #[test]
    fn test_canonical_bytes_b64_exceeds_size_limit() {
        use base64::Engine;
        let content = vec![0u8; 200];
        let b64 = base64::engine::general_purpose::STANDARD.encode(&content);
        // 限制 100 字节，实际 200 字节
        let result = validate_canonical_bytes_b64(&b64, 100, None);
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "payload_too_large");
        assert!(err.message.contains("200"));
        assert!(err.message.contains("100"));
    }

    #[test]
    fn test_canonical_bytes_b64_digest_match() {
        use base64::Engine;
        use sha2::{Digest, Sha256};
        let content = b"hello world";
        let b64 = base64::engine::general_purpose::STANDARD.encode(content);
        let mut hasher = Sha256::new();
        hasher.update(content);
        let expected = format!("{:x}", hasher.finalize());

        let result = validate_canonical_bytes_b64(&b64, 1024, Some(&expected));
        assert!(result.is_ok());
        assert_eq!(result.unwrap(), content);
    }

    #[test]
    fn test_canonical_bytes_b64_digest_mismatch() {
        use base64::Engine;
        let content = b"hello world";
        let b64 = base64::engine::general_purpose::STANDARD.encode(content);
        let wrong_digest = "0000000000000000000000000000000000000000000000000000000000000000";

        let result = validate_canonical_bytes_b64(&b64, 1024, Some(wrong_digest));
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "digest_mismatch");
        assert!(err.message.contains("expected="));
        assert!(err.message.contains("actual="));
    }

    #[test]
    fn test_canonical_bytes_b64_empty_digest_skips_check() {
        use base64::Engine;
        let content = b"test";
        let b64 = base64::engine::general_purpose::STANDARD.encode(content);
        // 空字符串 digest 应跳过校验
        let result = validate_canonical_bytes_b64(&b64, 1024, Some(""));
        assert!(result.is_ok());
    }

    #[test]
    fn test_canonical_bytes_b64_case_insensitive_digest() {
        use base64::Engine;
        use sha2::{Digest, Sha256};
        let content = b"case test";
        let b64 = base64::engine::general_purpose::STANDARD.encode(content);
        let mut hasher = Sha256::new();
        hasher.update(content);
        let expected_upper = format!("{:X}", hasher.finalize());

        // 大写 digest 也应匹配（内部转小写比较）
        let result = validate_canonical_bytes_b64(&b64, 1024, Some(&expected_upper));
        assert!(result.is_ok());
    }
}
