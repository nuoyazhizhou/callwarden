//! Enterprise daemon 的有界 UDS JSON-RPC 协议层。
//!
//! 协议格式：4 字节大端 header（payload 长度）+ UTF-8 JSON payload。
//! 附加能力：SCM_RIGHTS 文件描述符传递（仅 Unix）。
//!
//! 参考：server/daemon_protocol.py（Python 权威实现）。
//! 本模块为 Rust 等价实现，供 cw_daemon binary 与 Python daemon 互通。

use serde_json::{Map, Value};
use std::io::{self, Read, Write};

// P1-3（2026-07-21）：修复既有导入错误
// _recv_msg_with_fd / call_with_fd 在 #[cfg(unix)] 函数签名中使用 RawFd，
// 但原 use 只在 mod unix 内部，外层函数无法访问。这里在文件顶层补导入。
#[cfg(unix)]
use std::os::unix::io::RawFd;

/// 4 字节大端 header（u32）
pub const HEADER_SIZE: usize = 4;

/// 默认最大消息字节数（8 MB），与 Python 一致
pub const DEFAULT_MAX_MESSAGE_BYTES: usize = 8 * 1024 * 1024;

/// 默认最大 FD 数量（SCM_RIGHTS），与 Python 一致
pub const DEFAULT_MAX_FDS: usize = 1;

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
    #[error("FD 数量必须在 1..={max} 之间")]
    InvalidFdCount { max: usize },
    #[error("收到过多 FD: {actual} > {max}")]
    TooManyFds { actual: usize, max: usize },
    #[error("SCM_RIGHTS ancillary data 被截断")]
    AncillaryTruncated,
    #[error("当前平台不支持 SCM_RIGHTS")]
    ScmRightsUnsupported,
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
// SCM_RIGHTS FD 传递（仅 Unix）
// ============================================

#[cfg(unix)]
mod unix {
    use super::*;
    use libc::{
        c_void, iovec, msghdr, recvmsg, sendmsg, CMSG_DATA, CMSG_FIRSTHDR, CMSG_LEN, CMSG_SPACE,
        SCM_RIGHTS, SOL_SOCKET,
    };
    use std::os::unix::io::{AsRawFd, RawFd};
    use std::os::unix::net::UnixStream;

    /// 发送 JSON 帧并附带少量 SCM_RIGHTS 文件描述符
    ///
    /// 协议：与 send_message 相同，但通过 SCM_RIGHTS ancillary data 携带 FD
    pub fn send_message_with_fds(
        sock: &mut UnixStream,
        message: &Value,
        fds: &[RawFd],
        max_bytes: usize,
    ) -> Result<(), ProtocolError> {
        if fds.is_empty() || fds.len() > DEFAULT_MAX_FDS {
            return Err(ProtocolError::InvalidFdCount {
                max: DEFAULT_MAX_FDS,
            });
        }
        let payload = encode_message(message)?;
        if payload.len() > max_bytes {
            return Err(ProtocolError::MessageTooLarge {
                actual: payload.len(),
                limit: max_bytes,
            });
        }
        let mut frame = Vec::with_capacity(HEADER_SIZE + payload.len());
        frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        frame.extend_from_slice(&payload);

        let fd_count = fds.len();
        let cmsg_space =
            unsafe { CMSG_SPACE((fd_count * std::mem::size_of::<RawFd>()) as u32) } as usize;

        // 构建 iov
        let mut iov = iovec {
            iov_base: frame.as_mut_ptr() as *mut c_void,
            iov_len: frame.len(),
        };

        // 构建 cmsg buffer
        let mut cmsg_buf = vec![0u8; cmsg_space];
        let mut msg: msghdr = unsafe { std::mem::zeroed() };
        msg.msg_iov = &mut iov;
        msg.msg_iovlen = 1;
        msg.msg_control = cmsg_buf.as_mut_ptr() as *mut c_void;
        // macOS 上 msg_controllen 是 socklen_t (u32)，Linux 上是 size_t (usize)，用 as _ 适配
        msg.msg_controllen = cmsg_space as _;

        // 填充 cmsg
        unsafe {
            let cmsg = CMSG_FIRSTHDR(&msg);
            if cmsg.is_null() {
                return Err(ProtocolError::Other("CMSG_FIRSTHDR 返回 null".to_string()));
            }
            (*cmsg).cmsg_level = SOL_SOCKET;
            (*cmsg).cmsg_type = SCM_RIGHTS;
            // cmsg_len: Linux 上为 size_t (usize)，macOS 上为 socklen_t (u32)
            // 用 as _ 让编译器根据目标平台自动推导类型
            (*cmsg).cmsg_len = CMSG_LEN((fd_count * std::mem::size_of::<RawFd>()) as u32) as _;
            let data_ptr = CMSG_DATA(cmsg) as *mut RawFd;
            for (i, &fd) in fds.iter().enumerate() {
                *data_ptr.add(i) = fd;
            }
        }

        let ret = unsafe { sendmsg(sock.as_raw_fd(), &msg, 0) };
        if ret < 0 {
            return Err(ProtocolError::Io(io::Error::last_os_error()));
        }
        // 如果 sendmsg 没发完所有数据，补发剩余
        let sent = ret as usize;
        if sent < frame.len() {
            sock.write_all(&frame[sent..])?;
        }
        Ok(())
    }

    /// 接收 JSON 帧及首包携带的 SCM_RIGHTS FD
    ///
    /// 返回 (message, received_fds)
    pub fn recv_message_with_fds(
        sock: &mut UnixStream,
        max_bytes: usize,
        max_fds: usize,
    ) -> Result<(Value, Vec<RawFd>), ProtocolError> {
        let fd_size = std::mem::size_of::<RawFd>();
        let cmsg_space = unsafe { CMSG_SPACE((max_fds * fd_size) as u32) } as usize;

        // 先用 recvmsg 接收 header + ancillary data
        let mut header_buf = [0u8; HEADER_SIZE];
        let mut iov = iovec {
            iov_base: header_buf.as_mut_ptr() as *mut c_void,
            iov_len: HEADER_SIZE,
        };
        let mut cmsg_buf = vec![0u8; cmsg_space];
        let mut msg: msghdr = unsafe { std::mem::zeroed() };
        msg.msg_iov = &mut iov;
        msg.msg_iovlen = 1;
        msg.msg_control = cmsg_buf.as_mut_ptr() as *mut c_void;
        // macOS 上 msg_controllen 是 socklen_t (u32)，Linux 上是 size_t (usize)，用 as _ 适配
        msg.msg_controllen = cmsg_space as _;

        let mut received_fds: Vec<RawFd> = Vec::new();
        let ret = unsafe { recvmsg(sock.as_raw_fd(), &mut msg, 0) };
        if ret < 0 {
            return Err(ProtocolError::Io(io::Error::last_os_error()));
        }
        let n = ret as usize;
        if n == 0 {
            return Err(ProtocolError::ConnectionClosed);
        }
        // 检查 MSG_CTRUNC
        if msg.msg_flags & libc::MSG_CTRUNC != 0 {
            return Err(ProtocolError::AncillaryTruncated);
        }

        // 提取 FDs
        unsafe {
            let mut cmsg = CMSG_FIRSTHDR(&msg);
            while !cmsg.is_null() {
                if (*cmsg).cmsg_level == SOL_SOCKET && (*cmsg).cmsg_type == SCM_RIGHTS {
                    let data_ptr = CMSG_DATA(cmsg) as *const RawFd;
                    // 计算 cmsg 数据部分能容纳多少个 RawFd
                    let cmsg_data_len =
                        (*cmsg).cmsg_len as usize - (CMSG_DATA(cmsg) as usize - cmsg as usize);
                    let fd_count = cmsg_data_len / fd_size;
                    for i in 0..fd_count {
                        received_fds.push(*data_ptr.add(i));
                    }
                }
                cmsg = libc::CMSG_NXTHDR(&msg, cmsg);
                if cmsg.is_null() {
                    break;
                }
            }
        }
        if received_fds.len() > max_fds {
            // 关闭已接收的 FD
            for fd in &received_fds {
                unsafe { libc::close(*fd) };
            }
            return Err(ProtocolError::TooManyFds {
                actual: received_fds.len(),
                max: max_fds,
            });
        }

        // header 可能未完全接收（n < HEADER_SIZE），补读
        let mut header = header_buf[..n].to_vec();
        if header.len() < HEADER_SIZE {
            let mut remaining = vec![0u8; HEADER_SIZE - header.len()];
            sock.read_exact(&mut remaining)?;
            header.extend_from_slice(&remaining);
        }
        let size = parse_header(&header)?;
        if size == 0 || size as usize > max_bytes {
            // 关闭已接收的 FD
            for fd in &received_fds {
                unsafe { libc::close(*fd) };
            }
            return Err(ProtocolError::InvalidMessageSize(size));
        }

        // 接收 payload
        let mut payload = vec![0u8; size as usize];
        sock.read_exact(&mut payload)?;
        let s = String::from_utf8(payload)?;
        // 任务 1D2（Req 15/AC26）：frame bytes 先经 strict duplicate-key parser。
        if let Err(e) =
            crate::daemon::task_loop::strict_transport::parse_strict_envelope(s.as_bytes())
        {
            if e.code == crate::daemon::task_loop::strict_transport::ERR_DUPLICATE_JSON_KEY {
                // 关闭已接收的 FD
                for fd in &received_fds {
                    unsafe { libc::close(*fd) };
                }
                return Err(ProtocolError::DuplicateJsonKey(e.message));
            }
        }
        let message: Value = serde_json::from_str(&s)?;
        if !message.is_object() {
            // 关闭已接收的 FD
            for fd in &received_fds {
                unsafe { libc::close(*fd) };
            }
            return Err(ProtocolError::NotJsonObject);
        }
        Ok((message, received_fds))
    }
}

#[cfg(unix)]
pub use unix::{recv_message_with_fds, send_message_with_fds};

// ============================================
// G21/G22: 命名统一包装
//
// 规范 daemon-ipc-security.md 中使用了简短命名：
// - `send_msg`：对应 `send_message`
// - `_recv_msg_with_fd`：对应 `recv_message_with_fds`（复数 fds）
// - `call_with_fd`：请求-响应组合（send_msg + _recv_msg_with_fd）
//
// 现有代码已实现完整功能，仅函数名与规范文档不一致。这里新增简短别名，
// 让按规范文档查阅代码的开发者能快速找到对应实现，避免命名困惑。
//
// 别名是 zero-cost：直接 re-export，不引入额外间接调用。
// ============================================

/// G21: `send_msg` 别名，对应规范文档中的简短命名
///
/// 等价于 [`send_message`]，详见原函数文档。
pub fn send_msg<W: Write>(
    writer: &mut W,
    message: &Value,
    max_bytes: usize,
) -> Result<(), ProtocolError> {
    send_message(writer, message, max_bytes)
}

/// G22: `_recv_msg_with_fd` 别名（单数 fd 命名，实际仍接收多个 FD）
///
/// 等价于 [`recv_message_with_fds`]（仅在 Unix 下可用）。
/// 函数名前导下划线表示这是规范文档使用的"内部命名"，与生产代码命名
/// （复数 fds）保持区分。功能完全相同。
#[cfg(unix)]
pub fn _recv_msg_with_fd(
    sock: &mut std::os::unix::net::UnixStream,
    max_bytes: usize,
    max_fds: usize,
) -> Result<(Value, Vec<RawFd>), ProtocolError> {
    recv_message_with_fds(sock, max_bytes, max_fds)
}

/// G21/G22: `call_with_fd` 组合——发送请求 + 接收带 FD 的响应
///
/// 便利函数：封装 send_msg + _recv_msg_with_fd 的请求-响应模式。
/// 适用于 daemon 客户端调用 "send FD → 接收处理结果" 的场景。
///
/// 参数：
/// - `sock`: UDS socket（已连接）
/// - `request`: 请求 JSON
/// - `max_bytes`: 单条消息字节上限
/// - `max_fds`: FD 数量上限
///
/// 返回 (response, received_fds)
#[cfg(unix)]
pub fn call_with_fd(
    sock: &mut std::os::unix::net::UnixStream,
    request: &Value,
    max_bytes: usize,
    max_fds: usize,
) -> Result<(Value, Vec<RawFd>), ProtocolError> {
    send_msg(sock, request, max_bytes)?;
    _recv_msg_with_fd(sock, max_bytes, max_fds)
}

// ============================================
// canonical_bytes_b64 载荷校验（Req 14.10，跨平台）
// ============================================

/// canonical_bytes_b64 默认最大载荷字节数（64 MB，与 FD 路径 DEFAULT_MAX_FD_READ_BYTES 对齐）
pub const DEFAULT_MAX_CANONICAL_BYTES: usize = 64 * 1024 * 1024;

/// 校验 base64 编码的 canonical bytes 载荷（Req 14.10）。
///
/// Windows 上 SCM_RIGHTS FD 传输不可用，客户端通过 `canonical_bytes_b64` 参数
/// 直接提交规范化字节。使用前必须校验：
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
// Unix 专属测试（SCM_RIGHTS FD 传递）
// ============================================

#[cfg(all(unix, test))]
mod unix_tests {
    use super::*;
    use std::os::unix::net::UnixStream;
    use std::thread;

    /// 验证 SCM_RIGHTS FD 传递 roundtrip
    #[test]
    fn test_scm_rights_fd_passing() {
        let (mut sock_a, mut sock_b) = UnixStream::pair().unwrap();

        // 创建临时文件并打开 FD
        let temp_dir = tempfile::tempdir().unwrap();
        let temp_path = temp_dir.path().join("test_fd.txt");
        std::fs::write(&temp_path, b"hello fd").unwrap();
        let file = std::fs::File::open(&temp_path).unwrap();
        let raw_fd = std::os::unix::io::AsRawFd::as_raw_fd(&file);

        // 发送端：发 JSON + FD
        let msg = serde_json::json!({"method": "snapshot.publish", "fd_attached": true});
        let send_result =
            send_message_with_fds(&mut sock_a, &msg, &[raw_fd], DEFAULT_MAX_MESSAGE_BYTES);
        if send_result.is_err() {
            // 某些环境（如无权限的沙箱）可能失败，跳过
            eprintln!("跳过 SCM_RIGHTS 测试：sendmsg 失败");
            return;
        }

        // 接收端：收 JSON + FD
        let (received_msg, received_fds) =
            recv_message_with_fds(&mut sock_b, DEFAULT_MAX_MESSAGE_BYTES, DEFAULT_MAX_FDS).unwrap();
        assert_eq!(received_msg, msg);
        assert_eq!(received_fds.len(), 1);

        // 验证接收到的 FD 可读
        let mut received_file = unsafe { std::fs::File::from_raw_fd(received_fds[0]) };
        let mut content = String::new();
        use std::os::unix::io::FromRawFd;
        received_file.read_to_string(&mut content).unwrap();
        assert_eq!(content, "hello fd");
    }

    /// 验证空 FD 列表被拒绝
    #[test]
    fn test_empty_fds_rejected() {
        let (mut sock_a, _sock_b) = UnixStream::pair().unwrap();
        let msg = serde_json::json!({"method": "test"});
        let result = send_message_with_fds(&mut sock_a, &msg, &[], DEFAULT_MAX_MESSAGE_BYTES);
        match result {
            Err(ProtocolError::InvalidFdCount { .. }) => (),
            _ => panic!("期望 InvalidFdCount 错误"),
        }
    }

    /// 验证过多 FD 被拒绝
    #[test]
    fn test_too_many_fds_rejected() {
        let (mut sock_a, _sock_b) = UnixStream::pair().unwrap();
        let msg = serde_json::json!({"method": "test"});
        let fds = [0, 1, 2]; // 3 个 FD，超过 DEFAULT_MAX_FDS=1
        let result = send_message_with_fds(&mut sock_a, &msg, &fds, DEFAULT_MAX_MESSAGE_BYTES);
        match result {
            Err(ProtocolError::InvalidFdCount { .. }) => (),
            _ => panic!("期望 InvalidFdCount 错误"),
        }
    }

    // ---- D0 3.6: validate_canonical_bytes_b64 测试（Req 14.10）----

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
