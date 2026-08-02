//! 平台无关的 daemon 传输抽象层。
//!
//! D0 3.1（Req 14.1–14.4, 14.18–14.21）：将当前只在 Unix 编译的监听、接受连接与
//! 请求循环抽象为平台无关的 listener / acceptor / connection 抽象：
//! - Unix 侧绑定 Unix domain socket（owner + 0660）
//! - Windows 侧绑定命名管道 `\\.\pipe\callwarden-<user-sid>`（SDDL 仅授权 owner SID）
//!
//! ## 端点负向约束（Req 14.20, 14.21）
//! 实现与配置层面不得存在 Windows AF_UNIX 端点、监听 TCP 端口或本机 HTTPS
//! 协同 RPC 入口；OS 不为这些连接提供 Peer_Credential，Requirement 14.5
//! 在其上无法成立。
//!
//! ## 实例保活（Req 14.19，硬要求）
//! Windows 命名管道预创建 ≥2 个实例，并在服务每个已接受连接之前补建替换实例，
//! 消除两次 accept 之间的 pipe-busy / 端点缺失竞态窗口。
//! 这不是优化项，顺序颠倒即重新引入竞态。

use std::io;

// ============================================
// 对端身份（跨平台统一表示）
// ============================================

/// 由操作系统内核提供的连接对端凭证派生的身份。
///
/// Peer_Identity **只**由 OS Peer_Credential 派生（Req 14.5）。
/// 客户端自报的 agent 名、session 名、请求体身份字段一律不参与授权判定。
///
/// | 平台    | Peer_Credential 来源       | Peer_Identity 组成        |
/// |---------|---------------------------|--------------------------|
/// | Linux   | SO_PEERCRED               | UID + GID（pid 可作审计）|
/// | macOS   | LOCAL_PEERCRED            | UID + GID（无 pid）      |
/// | Windows | 命名管道对端访问令牌 SID  | 对端令牌 SID             |
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransportPeerIdentity {
    /// Unix 对端凭证（Linux SO_PEERCRED / macOS LOCAL_PEERCRED）
    Unix {
        uid: u32,
        gid: u32,
        /// Linux 有 pid；macOS 无 pid（值为 0，不参与授权）
        pid: i32,
    },
    /// Windows 命名管道对端访问令牌 SID（SDDL 字符串格式，如 S-1-5-21-...）
    Windows {
        /// 对端令牌 SID（SDDL 字符串）
        sid: String,
        /// 对端进程 PID（审计元数据，不参与授权判定）
        pid: u32,
    },
}

impl TransportPeerIdentity {
    /// 返回用于 ACL 比较的 owner 标识符。
    /// Unix: uid 字符串；Windows: SID 字符串。
    pub fn owner_key(&self) -> String {
        match self {
            Self::Unix { uid, .. } => uid.to_string(),
            Self::Windows { sid, .. } => sid.clone(),
        }
    }
}

// ============================================
// 传输连接 trait
// ============================================

/// 平台无关的已建立连接。
///
/// 每个连接处理单个请求（与 Python daemon 一致：连接 = 请求 = 响应）。
pub trait TransportConnection: Send {
    /// 读取一个完整消息帧（长度前缀 + JSON 载荷）。
    /// 返回原始 JSON 字节。
    fn recv_message(&mut self, max_bytes: usize) -> io::Result<Vec<u8>>;

    /// 写入一个完整消息帧。
    fn send_message(&mut self, data: &[u8]) -> io::Result<()>;

    /// 获取对端身份（由 OS 内核保证不可伪造）。
    fn peer_identity(&self) -> io::Result<TransportPeerIdentity>;

    /// 设置读写超时。
    fn set_timeout(&mut self, timeout: std::time::Duration) -> io::Result<()>;
}

// ============================================
// 传输监听器 trait
// ============================================

/// 平台无关的监听器。
///
/// ## 实例保活（Windows 命名管道，Req 14.19）
/// Windows 实现必须在 accept 返回之前补建替换管道实例，
/// 保证任意两次 accept 之间不存在端点缺失窗口。
/// Unix 实现无此约束（监听 socket 在 accept 后仍然存在）。
pub trait TransportListener: Send {
    /// 阻塞等待并接受一个连接。
    ///
    /// Windows 实现：在返回 conn 之前已补建替换管道实例（先补建、后服务）。
    fn accept(&mut self) -> io::Result<Box<dyn TransportConnection>>;

    /// 请求停止监听（非阻塞，打破 accept 阻塞）。
    fn shutdown(&self) -> io::Result<()>;

    /// 返回端点描述（用于日志和错误消息）。
    fn endpoint_description(&self) -> String;
}

// ============================================
// 传输配置
// ============================================

/// 平台无关的传输配置。
#[derive(Debug, Clone)]
pub struct TransportConfig {
    /// 最大消息字节数（默认 8 MB）
    pub max_message_bytes: usize,
    /// 工作线程数（默认 16）
    pub max_workers: usize,
    /// 单请求超时（默认 30 秒）
    pub request_timeout: std::time::Duration,
    /// accept 循环超时（用于响应 shutdown 信号，默认 200ms）
    pub accept_timeout: std::time::Duration,
}

impl Default for TransportConfig {
    fn default() -> Self {
        Self {
            max_message_bytes: 8 * 1024 * 1024,
            max_workers: 16,
            request_timeout: std::time::Duration::from_secs(30),
            accept_timeout: std::time::Duration::from_millis(200),
        }
    }
}

// ============================================
// 平台工厂函数
// ============================================

/// 创建平台对应的传输监听器。
///
/// - Unix: 绑定 Unix domain socket（owner + 0660）
/// - Windows: 绑定命名管道 `\\.\pipe\callwarden-<user-sid>`（SDDL 仅授权 owner）
///
/// 端点负向约束（Req 14.20, 14.21）：
/// 不暴露 TCP 端口、HTTPS 端点或 Windows AF_UNIX。
#[cfg(unix)]
pub fn create_listener(
    socket_path: &std::path::Path,
    config: &TransportConfig,
) -> io::Result<Box<dyn TransportListener>> {
    use super::server::{ServerConfig, UnixTransportListener};

    let server_config = ServerConfig {
        socket_path: socket_path.to_path_buf(),
        max_message_bytes: config.max_message_bytes,
        max_fds: super::protocol::DEFAULT_MAX_FDS,
        max_workers: config.max_workers,
        request_timeout: config.request_timeout,
        socket_mode: 0o660,
        accept_timeout: config.accept_timeout,
        socket_group: None,
    };
    let listener = UnixTransportListener::bind(&server_config)?;
    Ok(Box::new(listener))
}

/// Windows 平台工厂：创建命名管道监听器。
///
/// 管道名由 owner user SID 派生：`\\.\pipe\callwarden-<user-sid>`
/// SDDL 只授权 owner SID 的 connect 与读写（可选附加 local administrators），
/// 其他 SID 一律不授权，使访问范围等价 Unix socket 的 owner + 0660（Req 14.18）。
#[cfg(windows)]
pub fn create_listener(config: &TransportConfig) -> io::Result<Box<dyn TransportListener>> {
    use super::transport_windows::NamedPipeListener;

    let listener = NamedPipeListener::bind(config)?;
    Ok(Box::new(listener))
}
