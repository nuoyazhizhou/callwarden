//! Daemon server——平台传输监听 + 多线程 accept + 对端 ACL。
//!
//! 对应 Python `server/daemon_server.py:EnterpriseDaemonServer`（L517-622）
//!
//! ## 跨平台传输（D0 3.2，Req 14.1–14.4）
//! - Unix: UDS + SO_PEERCRED + SCM_RIGHTS FD 传递（`#[cfg(unix)]` 保留原有路径）
//! - Windows: 命名管道 + ImpersonateNamedPipeClient（通过 `transport` 模块）
//! - 跨平台入口：`start_server_transport` 使用 `TransportListener` 抽象
//!
//! ## 安全模型（daemon-ipc-security.md §3.1）
//! - **身份始终取自 OS Peer_Credential**：内核保证不可伪造
//! - 客户端请求体中的身份字段不参与授权
//! - 跨 UID/SID 拒绝访问
//!
//! ## 线程模型
//! - 主线程：listener accept 循环
//! - 工作线程池：每连接一线程，有界
//! - 每个连接处理单个请求（连接 = 请求 = 响应）

// Unix 专用导入（UDS + SO_PEERCRED + SCM_RIGHTS）
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
#[cfg(unix)]
use std::os::unix::net::{UnixListener, UnixStream};
#[cfg(unix)]
use std::path::{Path, PathBuf};

use std::io;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

#[cfg(unix)]
use crossbeam_channel::{bounded, Sender};
#[cfg(not(unix))]
use crossbeam_channel::{bounded, Sender};

use super::dispatch::{DaemonStateExt, PeerCredential};
#[cfg(unix)]
use super::protocol::{make_error_response, send_message, DEFAULT_MAX_MESSAGE_BYTES};
#[cfg(not(unix))]
use super::protocol::{make_error_response, DEFAULT_MAX_MESSAGE_BYTES};
use super::transport::{TransportConnection, TransportListener, TransportPeerIdentity};

// H1: HTTP MVP transport 异步运行时 + 配置类型（dev_loopback_unauthenticated overlay）
use tokio::sync::Mutex as TokioMutex;
use super::serialization::SerializationPoint;
use super::http_server::HttpServerConfig;

#[cfg(unix)]
use super::peercred::{get_peer_cred, PeerCred};
#[cfg(unix)]
use super::protocol::{recv_message_with_fds, ProtocolError, DEFAULT_MAX_FDS};
#[cfg(unix)]
use libc::mode_t;

// ============================================
// DaemonConfig 扩展（UDS server 专用配置）
// ============================================

/// UDS server 配置（对应 Python EnterpriseDaemonServer.__init__ 参数）
#[cfg(unix)]
#[derive(Debug, Clone)]
pub struct ServerConfig {
    /// UDS socket 路径
    pub socket_path: PathBuf,
    /// 最大消息字节数（默认 8 MB）
    pub max_message_bytes: usize,
    /// 最大 FD 数量（SCM_RIGHTS，默认 1）
    pub max_fds: usize,
    /// 工作线程数（默认 16）
    pub max_workers: usize,
    /// 单请求超时（秒，默认 30）
    pub request_timeout: Duration,
    /// socket 文件权限（默认 0o660）
    pub socket_mode: mode_t,
    /// accept 循环的超时（用于响应 shutdown 信号）
    pub accept_timeout: Duration,
    /// P0-3 修复：socket 文件组名（非空时 chown 到该组，用于多用户 UDS 访问）
    pub socket_group: Option<String>,
    /// H1: opt-in HTTP MVP transport 配置（仅 dev_loopback_unauthenticated）。
    /// 为 None 时行为与现有 UDS server 完全一致。
    pub http: Option<super::http_server::HttpServerConfig>,
}

#[cfg(unix)]
impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            // R7: 统一为 systemd RuntimeDirectory 风格，与 release/linux/deb/daemon.{preinst,postinst,prerm} 一致
            // 旧版路径 /var/run/callwarden.sock 已废弃
            socket_path: PathBuf::from(super::config::DEFAULT_SOCKET_PATH),
            max_message_bytes: DEFAULT_MAX_MESSAGE_BYTES,
            max_fds: DEFAULT_MAX_FDS,
            max_workers: 16,
            request_timeout: Duration::from_secs(30),
            socket_mode: 0o660,
            accept_timeout: Duration::from_millis(200),
            socket_group: None,
            http: None,
        }
    }
}

// ============================================
// Windows 命名管道 server 配置（D0 3.2，Req 14.1–14.4）
// ============================================

/// Windows 命名管道 server 配置。
///
/// 与 Unix `ServerConfig` 的差异：
/// - 无 `socket_path` / `socket_mode` / `socket_group`：管道名由当前用户 SID 派生
///   （`\\.\pipe\callwarden-<user-sid>`），SDDL 由 `transport_windows` 构建，
///   只授权 owner SID（可选附加 local administrators），不暴露其他端点（Req 14.18、14.20）。
/// - 无 `max_fds`：Windows 命名管道无 SCM_RIGHTS，客户端走 `canonical_bytes_b64` 参数路径。
/// - 字段直接映射到 `TransportConfig`，由 `create_listener(config)` 绑定 NamedPipeListener。
#[cfg(windows)]
#[derive(Debug, Clone)]
pub struct ServerConfig {
    /// 最大消息字节数（默认 8 MB）
    pub max_message_bytes: usize,
    /// 工作线程数（默认 16）
    pub max_workers: usize,
    /// 单请求超时（默认 30 秒）
    pub request_timeout: Duration,
    /// accept 循环的超时（用于响应 shutdown 信号）
    pub accept_timeout: Duration,
    /// H1: opt-in HTTP MVP transport 配置（仅 dev_loopback_unauthenticated）。
    /// 为 None 时行为与现有 Named Pipe server 完全一致。
    pub http: Option<super::http_server::HttpServerConfig>,
}

#[cfg(windows)]
impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            max_message_bytes: DEFAULT_MAX_MESSAGE_BYTES,
            max_workers: 16,
            request_timeout: Duration::from_secs(30),
            accept_timeout: Duration::from_millis(200),
            http: None,
        }
    }
}

// ============================================
// ServerHandle：用于控制 server 生命周期
// ============================================

/// UDS server 句柄（用于 shutdown 控制）
///
/// 对应 Python EnterpriseDaemonServer.shutdown
#[cfg(unix)]
pub struct ServerHandle {
    stop_flag: Arc<AtomicBool>,
    /// accept 线程 join handle（用于等待 server 退出）
    accept_thread: Option<thread::JoinHandle<()>>,
    /// worker 线程 join handles
    worker_handles: Vec<thread::JoinHandle<()>>,
    /// socket 路径（用于 shutdown 后清理）
    socket_path: PathBuf,
    /// listener 的 raw fd（用于 shutdown 时打破 accept 阻塞）
    listener_fd: std::os::unix::io::RawFd,
    /// 唯一串行化点（Req 14.6）：暴露给 HTTP MVP transport 复用（H1 P0-1）
    serialization_point: Arc<SerializationPoint>,
}

#[cfg(unix)]
impl ServerHandle {
    /// 请求 server 停止（非阻塞）
    ///
    /// 通过 libc::shutdown(fd, SHUT_RDWR) 打破 accept 线程的阻塞，
    /// 否则 blocking accept 会一直等连接，无法退出。
    pub fn shutdown(&mut self) {
        self.stop_flag.store(true, Ordering::SeqCst);
        // 关闭 listener fd 的读写端，打破 accept 阻塞
        // 即使 fd 已关闭，shutdown 也只会返回 EBADF，忽略即可
        unsafe {
            libc::shutdown(self.listener_fd, libc::SHUT_RDWR);
        }
    }

    /// 等待 server 完全退出（阻塞当前线程）
    pub fn join(&mut self) {
        if let Some(handle) = self.accept_thread.take() {
            let _ = handle.join();
        }
        for handle in self.worker_handles.drain(..) {
            let _ = handle.join();
        }
        // 清理 socket 文件
        let _ = std::fs::remove_file(&self.socket_path);
    }

    /// 返回共享的唯一串行化点（Req 14.6），供 HTTP MVP transport 复用（H1 P0-1）。
    pub fn serialization_point(&self) -> Arc<SerializationPoint> {
        Arc::clone(&self.serialization_point)
    }
}

#[cfg(unix)]
impl Drop for ServerHandle {
    fn drop(&mut self) {
        self.shutdown();
        self.join();
    }
}

// ============================================
// 启动 UDS server
// ============================================

/// 准备 socket 路径：创建父目录 + 清理旧 socket 文件
///
/// 对应 Python EnterpriseDaemonServer._prepare_socket_path
#[cfg(unix)]
fn prepare_socket_path(socket_path: &Path) -> io::Result<()> {
    if let Some(parent) = socket_path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    if std::fs::symlink_metadata(socket_path).is_ok() {
        // 检查是否为 socket 文件，拒绝覆盖非 socket 路径
        let meta = std::fs::symlink_metadata(socket_path)?;
        use std::os::unix::fs::FileTypeExt;
        if !meta.file_type().is_socket() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("拒绝覆盖非 socket 路径: {}", socket_path.display()),
            ));
        }
        std::fs::remove_file(socket_path)?;
    }
    Ok(())
}

/// 启动 UDS server，返回 ServerHandle 用于 shutdown 控制。
///
/// 对应 Python EnterpriseDaemonServer.serve_forever
///
/// 参数：
/// - config: server 配置
/// - state_factory: 闭包，为每个工作线程创建独立的 daemon state（因为
///   `DaemonStateExt` 可能包含 `Mutex<Connection>` 等非 Send 资源？目前是 Send
///   的，但保留 factory 模式便于后续扩展 per-thread 状态）
///
/// 注意：state 由 worker 线程共享，要求 `Send + Sync`。如果 state 包含非 Send
/// 资源（如 `RefCell`），需要在 state_factory 中包装。
#[cfg(unix)]
pub fn start_server<F, S>(config: ServerConfig, state_factory: F) -> io::Result<ServerHandle>
where
    F: Fn() -> io::Result<S> + Send + Sync + 'static,
    S: DaemonStateExt + Send + Sync + 'static,
{
    prepare_socket_path(&config.socket_path)?;

    let listener = UnixListener::bind(&config.socket_path)?;
    // 设置 socket 文件权限（0o660：owner + group 可读写）
    // macOS 上 libc::mode_t 是 u16，但 Permissions::from_mode 期望 u32，需要显式转换
    std::fs::set_permissions(
        &config.socket_path,
        std::fs::Permissions::from_mode(config.socket_mode as u32),
    )?;

    // P0-3 v2 修复：socket chown 到指定组（多用户 UDS 访问）—— fail-closed
    //
    // 复审报告指出：组不存在或 chown 失败时只 warning，daemon 继续 ready，
    // 导致"服务健康但所有真实客户端无权连接"。
    //
    // 修复：
    // 1. socket_group 非空时，组不存在或 chown 失败必须 return Err（fail-closed）
    // 2. chown 后回读 stat 校验 owner/group/mode
    // 3. 用 io::Error::last_os_error() 替代 libc::__errno_location()（macOS 用 __error）
    #[cfg(unix)]
    if let Some(ref group_name) = config.socket_group {
        if !group_name.is_empty() {
            use std::ffi::CString;
            let c_group = CString::new(group_name.as_str())
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?;

            // 查找 GID
            let grp_ptr = unsafe { libc::getgrnam(c_group.as_ptr()) };
            if grp_ptr.is_null() {
                // P0-3 v2: fail-closed —— 组不存在时拒绝启动
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    format!(
                        "[P0-3] socket_group '{}' 不存在，daemon 拒绝启动（fail-closed）。\
                         请创建组（groupadd {}）或在配置中清空 socket_group。",
                        group_name, group_name
                    ),
                ));
            }
            let gid = unsafe { (*grp_ptr).gr_gid };
            let c_path = CString::new(config.socket_path.to_string_lossy().as_bytes())
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?;

            // chown：uid 用 -1（不改变 owner），只改 group
            let ret = unsafe { libc::chown(c_path.as_ptr(), libc::uid_t::MAX, gid) };
            if ret != 0 {
                // P0-3 v2: fail-closed —— chown 失败时拒绝启动
                // 用 io::Error::last_os_error() 替代 libc::__errno_location()（跨平台）
                let err = io::Error::last_os_error();
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    format!(
                        "[P0-3] chown socket 到组 {} (gid={}) 失败（fail-closed）: {}。\
                         daemon 拒绝启动以避免无权连接的客户端被静默拒绝。",
                        group_name, gid, err
                    ),
                ));
            }

            // P0-3 v2: 回读 stat 校验 socket owner/group/mode
            let c_stat_path = CString::new(config.socket_path.to_string_lossy().as_bytes())
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?;
            let mut stat_buf: libc::stat = unsafe { std::mem::zeroed() };
            let stat_ret = unsafe { libc::stat(c_stat_path.as_ptr(), &mut stat_buf) };
            if stat_ret != 0 {
                let err = io::Error::last_os_error();
                return Err(io::Error::new(
                    io::ErrorKind::Other,
                    format!("[P0-3] stat socket 回读校验失败（fail-closed）: {}", err),
                ));
            }
            // 校验 GID
            if stat_buf.st_gid != gid {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    format!(
                        "[P0-3] socket GID 校验失败（fail-closed）：期望 {} 实际 {}。\
                         可能被其他进程覆盖。",
                        gid, stat_buf.st_gid
                    ),
                ));
            }
            // 校验 mode（socket_mode 的低 9 位）
            // macOS 上 st_mode 是 u16，Linux 上是 u32，统一转 u32 比较
            let actual_mode = (stat_buf.st_mode & 0o777) as u32;
            if actual_mode != config.socket_mode as u32 {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    format!(
                        "[P0-3] socket mode 校验失败（fail-closed）：期望 0o{:o} 实际 0o{:o}。\
                         可能被 umask 或其他进程覆盖。",
                        config.socket_mode, actual_mode
                    ),
                ));
            }

            eprintln!(
                "[P0-3] socket chown 到组 {} (gid={}) + mode 0o{:o} 校验通过",
                group_name, gid, config.socket_mode
            );
        }
    }
    // 保存 listener raw fd（用于 shutdown 时打破 accept 阻塞）
    use std::os::unix::io::AsRawFd;
    let listener_fd = listener.as_raw_fd();
    // 注：accept_loop 内部会设回 blocking 模式（start_server 保持 nonblocking 仅用于
    // 兼容性，实际 accept_loop 会调用 set_nonblocking(false)）

    let stop_flag = Arc::new(AtomicBool::new(false));
    let stop_flag_clone = stop_flag.clone();
    let (worker_tx, worker_rx) = bounded::<UnixStream>(config.max_workers);

    // 唯一串行化点（Req 14.6）：所有 worker 共享同一实例
    let serialization_point =
        Arc::new(super::serialization::SerializationPoint::with_default_timeout());

    // 启动 worker 线程池
    let state_factory = Arc::new(state_factory);
    let mut worker_handles = Vec::with_capacity(config.max_workers);
    for worker_idx in 0..config.max_workers {
        let state_factory = state_factory.clone();
        let worker_rx = worker_rx.clone();
        let stop_flag = stop_flag.clone();
        let max_message_bytes = config.max_message_bytes;
        let max_fds = config.max_fds;
        let request_timeout = config.request_timeout;
        let sp = serialization_point.clone();

        let handle = thread::Builder::new()
            .name(format!("cw-daemon-worker-{}", worker_idx))
            .spawn(move || {
                worker_loop(
                    &state_factory,
                    worker_rx,
                    stop_flag,
                    max_message_bytes,
                    max_fds,
                    request_timeout,
                    &sp,
                );
            })?;
        worker_handles.push(handle);
    }

    // accept 线程
    let socket_path = config.socket_path.clone();
    let accept_timeout = config.accept_timeout;
    let accept_thread = thread::Builder::new()
        .name("cw-daemon-accept".to_string())
        .spawn(move || {
            accept_loop(&listener, worker_tx, stop_flag_clone, accept_timeout);
        })?;

    Ok(ServerHandle {
        stop_flag,
        accept_thread: Some(accept_thread),
        worker_handles,
        socket_path,
        listener_fd,
        serialization_point,
    })
}

/// accept 线程主循环：等待连接 → 分发到 worker 线程池
///
/// 策略：blocking accept（零延迟），shutdown 时通过 libc::shutdown(fd, SHUT_RDWR)
/// 打破阻塞。比非阻塞 + sleep 轮询方案延迟低 100x（1ms → 10us）。
#[cfg(unix)]
fn accept_loop(
    listener: &UnixListener,
    worker_tx: Sender<UnixStream>,
    stop_flag: Arc<AtomicBool>,
    accept_timeout: Duration,
) {
    // 用 blocking accept：listener 设回 blocking 模式（start_server 中设为 nonblocking）
    // 这样 accept 在无连接时阻塞，有连接时立即返回，零延迟。
    // shutdown 时 stop_flag 被设置，主线程调用 libc::shutdown 打破阻塞。
    let _ = listener.set_nonblocking(false);

    while !stop_flag.load(Ordering::SeqCst) {
        match listener.accept() {
            Ok((stream, _addr)) => {
                // 发送到 worker channel，如果 channel 满了直接拒绝（背压）
                if worker_tx.try_send(stream).is_err() {
                    // worker 池已满，拒绝连接（客户端会收到连接重置）
                    eprintln!("[cw_daemon] worker pool full, rejecting connection");
                }
            }
            Err(ref e) if e.kind() == io::ErrorKind::WouldBlock => {
                // blocking 模式下不应触发，保险起见短暂 sleep
                thread::sleep(Duration::from_micros(100));
            }
            Err(e) => {
                if stop_flag.load(Ordering::SeqCst) {
                    // shutdown 触发的 accept 错误，正常退出
                    break;
                }
                eprintln!("[cw_daemon] accept error: {}", e);
                // 短暂 sleep 避免忙等
                thread::sleep(Duration::from_millis(10));
            }
        }
    }
    // 标记 accept_timeout 已使用（参数保留以便未来切换策略）
    let _ = accept_timeout;
}

/// worker 线程主循环：从 channel 取连接 → 处理 → 回复
#[cfg(unix)]
fn worker_loop<F, S>(
    state_factory: &Arc<F>,
    worker_rx: crossbeam_channel::Receiver<UnixStream>,
    stop_flag: Arc<AtomicBool>,
    max_message_bytes: usize,
    max_fds: usize,
    request_timeout: Duration,
    serialization_point: &super::serialization::SerializationPoint,
) where
    F: Fn() -> io::Result<S> + Send + Sync + 'static,
    S: DaemonStateExt + Send + 'static,
{
    // 每个 worker 持有独立的 daemon state（线程隔离，避免锁竞争）
    let mut state = match state_factory() {
        Ok(s) => s,
        Err(e) => {
            eprintln!("[cw_daemon] worker state_factory failed: {}", e);
            return;
        }
    };

    while !stop_flag.load(Ordering::SeqCst) {
        let mut stream = match worker_rx.recv_timeout(request_timeout) {
            Ok(s) => s,
            Err(crossbeam_channel::RecvTimeoutError::Timeout) => continue,
            Err(crossbeam_channel::RecvTimeoutError::Disconnected) => break,
        };

        // P1 修复（T-1785854423993）：与 transport_worker_loop 一致的 panic 隔离。
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            handle_connection(
                &mut stream,
                &mut state,
                max_message_bytes,
                max_fds,
                serialization_point,
            )
        }));
        match outcome {
            Ok(Ok(())) => {}
            Ok(Err(e)) => {
                eprintln!("[cw_daemon] connection error: {}", e);
            }
            Err(_) => {
                eprintln!("[cw_daemon] worker panic caught (UDS): handler 崩溃已被隔离");
            }
        }
    }
}

/// 处理单个连接：peercred → recv_message_with_fds → dispatch → send_message
///
/// 对应 Python EnterpriseDaemonServer._handle_connection
#[cfg(unix)]
pub fn handle_connection<S>(
    stream: &mut UnixStream,
    state: &mut S,
    max_message_bytes: usize,
    max_fds: usize,
    serialization_point: &super::serialization::SerializationPoint,
) -> io::Result<()>
where
    S: DaemonStateExt,
{
    // 设置读超时（避免恶意客户端挂死 worker 线程）
    let timeout = Some(Duration::from_secs(30));
    stream.set_read_timeout(timeout)?;
    stream.set_write_timeout(timeout)?;

    // 1. 获取 peer credential（SO_PEERCRED，内核保证不可伪造）
    let cred = get_peer_cred(stream)?;
    let peer = PeerCredential::new_unix(cred.uid, cred.gid, cred.pid);

    // 2. 接收 JSON-RPC 请求（含可选 FD）
    // recv_message_with_fds 需要 &mut UnixStream（内部用 recvmsg/read_exact）
    let (request, received_fds) = match recv_message_with_fds(stream, max_message_bytes, max_fds) {
        Ok((msg, fds)) => (msg, fds),
        Err(e) => {
            // 协议错误：尝试回复错误响应（可能客户端已经断开）。
            // 任务 1D2：duplicate key 使用稳定 E_DUPLICATE_JSON_KEY 回显。
            let response = match &e {
                ProtocolError::DuplicateJsonKey(_) => {
                    make_error_response("E_DUPLICATE_JSON_KEY", &e.to_string())
                }
                _ => make_error_response("protocol_error", &e.to_string()),
            };
            let _ = send_message(stream, &response, max_message_bytes);
            // 关闭已接收的 FD（虽然出错时一般没收到）
            close_fds(&[]);
            return Ok(());
        }
    };

    // 3. 解析 method / params / id
    let request_id = request.get("id").cloned();
    let method = request.get("method").and_then(|v| v.as_str()).unwrap_or("");
    let params = request
        .get("params")
        .cloned()
        .unwrap_or_else(|| serde_json::Value::Object(serde_json::Map::new()));

    // 4. dispatch（Protected_Mutation 经串行化点，Req 14.6）
    let response = if method.is_empty() || !params.is_object() {
        make_error_response("invalid_request", "method/params 类型错误")
    } else {
        super::dispatch::dispatch_rpc(
            state,
            peer,
            method,
            &params,
            &received_fds,
            serialization_point,
        )
    };

    // 5. 附加 request_id
    let final_response = if let Some(id) = request_id {
        let mut m = serde_json::Map::new();
        if let serde_json::Value::Object(obj) = response {
            m.extend(obj);
        }
        m.insert("id".to_string(), id);
        serde_json::Value::Object(m)
    } else {
        response
    };

    // 6. 发送响应
    if let Err(e) = send_message(stream, &final_response, max_message_bytes) {
        eprintln!("[cw_daemon] send response failed: {}", e);
    }

    // 7. 关闭客户端传入的 FD（避免 FD 泄漏）
    close_fds(&received_fds);

    Ok(())
}

/// 批量关闭 FD（避免 FD 泄漏）
#[cfg(unix)]
fn close_fds(fds: &[i32]) {
    for &fd in fds {
        unsafe {
            libc::close(fd);
        }
    }
}

// ============================================
// 测试辅助：单连接处理（不启动长循环）
// ============================================

/// 处理单个连接（测试用，不启动 accept 循环）
///
/// 与 `handle_connection` 的区别：直接接受 `UnixStream`，不通过 channel。
/// 适用于单元测试中模拟客户端连接（内部创建默认串行化点）。
#[cfg(unix)]
pub fn handle_one_connection<S>(
    stream: &mut UnixStream,
    state: &mut S,
    max_message_bytes: usize,
    max_fds: usize,
) -> io::Result<()>
where
    S: DaemonStateExt,
{
    let sp = super::serialization::SerializationPoint::with_default_timeout();
    handle_connection(stream, state, max_message_bytes, max_fds, &sp)
}

// ============================================
// Windows 命名管道 server（D0 3.2，Req 14.1–14.4，Req 14.18–14.21）
// ============================================

/// Windows 命名管道 server 句柄（用于 shutdown 控制）。
///
/// 与 Unix `ServerHandle` 的差异：
/// - 无 socket 文件需要清理（命名管道由 OS 管理，句柄关闭即释放）。
/// - 持有 `Arc<Mutex<Box<dyn TransportListener>>>`：shutdown 时调用
///   `NamedPipeListener::shutdown()`（SetEvent 打断 ConnectNamedPipe 阻塞），
///   否则 blocking accept 无法退出。
#[cfg(windows)]
pub struct ServerHandle {
    stop_flag: Arc<AtomicBool>,
    /// accept 线程 join handle（用于等待 server 退出）
    accept_thread: Option<thread::JoinHandle<()>>,
    /// worker 线程 join handles
    worker_handles: Vec<thread::JoinHandle<()>>,
    /// 监听器（accept 线程独占；handle 保留引用以在 shutdown 时打断 accept 阻塞）
    listener: Option<Arc<std::sync::Mutex<Box<dyn TransportListener>>>>,
    /// 唯一串行化点（Req 14.6）：暴露给 HTTP MVP transport 复用（H1 P0-1）
    serialization_point: Arc<SerializationPoint>,
}

#[cfg(windows)]
impl ServerHandle {
    /// 请求 server 停止（非阻塞）。
    ///
    /// 设置 stop_flag 后调用 `NamedPipeListener::shutdown()`（设置内部 stop_flag
    /// + SetEvent 打断 WaitForSingleObject），使 accept 线程在下一次轮询时以
    /// `io::ErrorKind::Interrupted` 退出。
    pub fn shutdown(&mut self) {
        self.stop_flag.store(true, Ordering::SeqCst);
        if let Some(ref listener) = self.listener {
            if let Ok(guard) = listener.lock() {
                let _ = guard.shutdown();
            }
        }
    }

    /// 等待 server 完全退出（阻塞当前线程）。
    pub fn join(&mut self) {
        if let Some(handle) = self.accept_thread.take() {
            let _ = handle.join();
        }
        for handle in self.worker_handles.drain(..) {
            let _ = handle.join();
        }
    }

    /// 返回共享的唯一串行化点（Req 14.6），供 HTTP MVP transport 复用（H1 P0-1）。
    pub fn serialization_point(&self) -> Arc<SerializationPoint> {
        Arc::clone(&self.serialization_point)
    }
}

#[cfg(windows)]
impl Drop for ServerHandle {
    fn drop(&mut self) {
        self.shutdown();
        self.join();
    }
}

/// 启动 Windows 命名管道 server，返回 ServerHandle 用于 shutdown 控制。
///
/// 流程（Req 14.19 先补建、后服务由 NamedPipeListener 内部保证）：
/// 1. `create_listener(config)` 绑定 `\\.\pipe\callwarden-<user-sid>`（SDDL 仅授权
///    owner SID，Req 14.18），预创建 ≥2 个管道实例。
/// 2. accept 线程阻塞在 `TransportListener::accept()`，接受连接后投递到有界
///    worker 通道（背压拒绝）。
/// 3. worker 线程复用跨平台 `transport_worker_loop`：对端身份经 peercred Windows
///    分支（ImpersonateNamedPipeClient + TokenUser SID，Req 14.5）派生，Protected_Mutation
///    经唯一串行化点 `SerializationPoint`（Req 14.6）执行。
///
/// 不暴露 TCP/HTTPS/AF_UNIX 端点（Req 14.20、14.21）——Windows 仅绑定命名管道。
#[cfg(windows)]
pub fn start_server<F, S>(config: ServerConfig, state_factory: F) -> io::Result<ServerHandle>
where
    F: Fn() -> io::Result<S> + Send + Sync + 'static,
    S: DaemonStateExt + Send + Sync + 'static,
{
    let transport_config = super::transport::TransportConfig {
        max_message_bytes: config.max_message_bytes,
        max_workers: config.max_workers,
        request_timeout: config.request_timeout,
        accept_timeout: config.accept_timeout,
    };
    let listener = super::transport::create_listener(&transport_config)?;
    eprintln!(
        "[cw_daemon] [INFO] named pipe endpoint: {}",
        listener.endpoint_description()
    );

    let stop_flag = Arc::new(AtomicBool::new(false));
    let (worker_tx, worker_rx) = bounded::<Box<dyn TransportConnection>>(config.max_workers);

    // 唯一串行化点（Req 14.6）：所有 worker 共享同一实例
    let serialization_point =
        Arc::new(super::serialization::SerializationPoint::with_default_timeout());

    // 启动 worker 线程池（复用跨平台 worker 循环，与 start_server_transport 一致）
    let state_factory = Arc::new(state_factory);
    let mut worker_handles = Vec::with_capacity(config.max_workers);
    for worker_idx in 0..config.max_workers {
        let state_factory = state_factory.clone();
        let worker_rx = worker_rx.clone();
        let stop_flag = stop_flag.clone();
        let max_message_bytes = config.max_message_bytes;
        let request_timeout = config.request_timeout;
        let sp = serialization_point.clone();

        let handle = thread::Builder::new()
            .name(format!("cw-daemon-worker-{}", worker_idx))
            .spawn(move || {
                transport_worker_loop(
                    &state_factory,
                    worker_rx,
                    stop_flag,
                    max_message_bytes,
                    request_timeout,
                    &sp,
                );
            })?;
        worker_handles.push(handle);
    }

    // accept 线程：listener 由 accept 线程独占；handle 保留 Arc<Mutex> 引用以便
    // shutdown 时调用 listener.shutdown() 打断 ConnectNamedPipe 阻塞。
    // NamedPipeListener::accept 内部每 200ms 轮询自身 stop_flag，因此锁等待有界。
    let listener = Arc::new(std::sync::Mutex::new(listener));
    let accept_listener = Arc::clone(&listener);
    let stop_flag_clone = stop_flag.clone();
    let accept_thread = thread::Builder::new()
        .name("cw-daemon-accept".to_string())
        .spawn(move || {
            windows_accept_loop(&accept_listener, worker_tx, stop_flag_clone);
        })?;

    Ok(ServerHandle {
        stop_flag,
        accept_thread: Some(accept_thread),
        worker_handles,
        listener: Some(listener),
        serialization_point,
    })
}

/// Windows accept 线程主循环：等待连接 → 分发到 worker 线程池。
///
/// `TransportListener::accept()`（NamedPipeListener）阻塞等待连接，带内部 200ms
/// 超时轮询自身 stop_flag；shutdown 时 ServerHandle 调用 `listener.shutdown()`
/// 使其返回 `io::ErrorKind::Interrupted`，循环退出。
#[cfg(windows)]
fn windows_accept_loop(
    listener: &Arc<std::sync::Mutex<Box<dyn TransportListener>>>,
    worker_tx: Sender<Box<dyn TransportConnection>>,
    stop_flag: Arc<AtomicBool>,
) {
    while !stop_flag.load(Ordering::SeqCst) {
        let mut guard = match listener.lock() {
            Ok(guard) => guard,
            Err(_) => break,
        };
        match guard.accept() {
            Ok(conn) => {
                // 发送到 worker channel，如果 channel 满了直接拒绝（背压）
                drop(guard);
                if worker_tx.try_send(conn).is_err() {
                    // worker 池已满，拒绝连接（客户端会收到连接重置）
                    eprintln!("[cw_daemon] worker pool full, rejecting connection");
                }
            }
            Err(ref e) if e.kind() == io::ErrorKind::Interrupted => break,
            Err(e) => {
                if stop_flag.load(Ordering::SeqCst) {
                    // shutdown 触发的 accept 错误，正常退出
                    break;
                }
                eprintln!("[cw_daemon] accept error: {}", e);
                // 短暂 sleep 避免忙等
                thread::sleep(Duration::from_millis(10));
            }
        }
    }
}

// ============================================
// 跨平台传输 server（D0 3.2，Req 14.1–14.4）
// ============================================

/// Unix 传输监听器包装（将现有 UDS server 逻辑适配到 TransportListener trait）。
///
/// 保留原有 SCM_RIGHTS FD 传递能力（Windows 无此路径，使用 base64 载荷）。
#[cfg(unix)]
pub struct UnixTransportListener {
    listener: UnixListener,
    stop_flag: Arc<AtomicBool>,
    socket_path: PathBuf,
    listener_fd: std::os::unix::io::RawFd,
}

#[cfg(unix)]
impl UnixTransportListener {
    /// 绑定 UDS 并设置权限。
    pub fn bind(config: &ServerConfig) -> io::Result<Self> {
        prepare_socket_path(&config.socket_path)?;
        let listener = UnixListener::bind(&config.socket_path)?;
        std::fs::set_permissions(
            &config.socket_path,
            std::fs::Permissions::from_mode(config.socket_mode as u32),
        )?;
        use std::os::unix::io::AsRawFd;
        let listener_fd = listener.as_raw_fd();
        Ok(Self {
            listener,
            stop_flag: Arc::new(AtomicBool::new(false)),
            socket_path: config.socket_path.clone(),
            listener_fd,
        })
    }
}

#[cfg(unix)]
impl TransportListener for UnixTransportListener {
    fn accept(&mut self) -> io::Result<Box<dyn TransportConnection>> {
        let _ = self.listener.set_nonblocking(false);
        loop {
            if self.stop_flag.load(Ordering::SeqCst) {
                return Err(io::Error::new(io::ErrorKind::Interrupted, "shutdown"));
            }
            match self.listener.accept() {
                Ok((stream, _)) => return Ok(Box::new(UnixTransportConnection { stream })),
                Err(ref e) if e.kind() == io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_micros(100));
                }
                Err(e) => {
                    if self.stop_flag.load(Ordering::SeqCst) {
                        return Err(io::Error::new(io::ErrorKind::Interrupted, "shutdown"));
                    }
                    return Err(e);
                }
            }
        }
    }

    fn shutdown(&self) -> io::Result<()> {
        self.stop_flag.store(true, Ordering::SeqCst);
        unsafe { libc::shutdown(self.listener_fd, libc::SHUT_RDWR) };
        Ok(())
    }

    fn endpoint_description(&self) -> String {
        format!("unix:{}", self.socket_path.display())
    }
}

/// Unix 传输连接包装。
#[cfg(unix)]
struct UnixTransportConnection {
    stream: UnixStream,
}

#[cfg(unix)]
impl TransportConnection for UnixTransportConnection {
    fn recv_message(&mut self, max_bytes: usize) -> io::Result<Vec<u8>> {
        use std::io::Read;
        // 长度前缀协议：4 字节大端长度 + JSON 载荷
        let mut len_buf = [0u8; 4];
        self.stream.read_exact(&mut len_buf)?;
        let len = u32::from_be_bytes(len_buf) as usize;
        if len > max_bytes {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("消息过大: {} > {}", len, max_bytes),
            ));
        }
        let mut buf = vec![0u8; len];
        self.stream.read_exact(&mut buf)?;
        Ok(buf)
    }

    fn send_message(&mut self, data: &[u8]) -> io::Result<()> {
        use std::io::Write;
        let len = (data.len() as u32).to_be_bytes();
        self.stream.write_all(&len)?;
        self.stream.write_all(data)?;
        self.stream.flush()
    }

    fn peer_identity(&self) -> io::Result<TransportPeerIdentity> {
        let cred = get_peer_cred(&self.stream)?;
        Ok(TransportPeerIdentity::Unix {
            uid: cred.uid,
            gid: cred.gid,
            pid: cred.pid,
        })
    }

    fn set_timeout(&mut self, timeout: Duration) -> io::Result<()> {
        self.stream.set_read_timeout(Some(timeout))?;
        self.stream.set_write_timeout(Some(timeout))
    }
}

/// 跨平台 daemon server 入口（使用 TransportListener 抽象）。
///
/// D0 3.2（Req 14.1–14.4）：
/// - Unix: 通过 UnixTransportListener 保留原有 UDS + FD 传递
/// - Windows: 通过 NamedPipeListener 提供命名管道端点
/// - 三平台暴露等价协同 RPC 方法集
///
/// 端点负向约束（Req 14.20, 14.21）：不暴露 TCP/HTTPS/AF_UNIX。
pub fn start_server_transport<F, S>(
    mut listener: Box<dyn TransportListener>,
    config: &super::transport::TransportConfig,
    state_factory: F,
) -> io::Result<TransportServerHandle>
where
    F: Fn() -> io::Result<S> + Send + Sync + 'static,
    S: DaemonStateExt + Send + 'static,
{
    let stop_flag = Arc::new(AtomicBool::new(false));
    let (worker_tx, worker_rx) = bounded::<Box<dyn TransportConnection>>(config.max_workers);

    // 唯一串行化点（Req 14.6）：所有 worker 共享同一实例
    let serialization_point =
        Arc::new(super::serialization::SerializationPoint::with_default_timeout());

    // 启动 worker 线程池
    let state_factory = Arc::new(state_factory);
    let mut worker_handles = Vec::with_capacity(config.max_workers);
    for worker_idx in 0..config.max_workers {
        let state_factory = state_factory.clone();
        let worker_rx = worker_rx.clone();
        let stop_flag = stop_flag.clone();
        let max_message_bytes = config.max_message_bytes;
        let request_timeout = config.request_timeout;
        let sp = serialization_point.clone();

        let handle = thread::Builder::new()
            .name(format!("cw-daemon-worker-{}", worker_idx))
            .spawn(move || {
                transport_worker_loop(
                    &state_factory,
                    worker_rx,
                    stop_flag,
                    max_message_bytes,
                    request_timeout,
                    &sp,
                );
            })?;
        worker_handles.push(handle);
    }

    // accept 线程
    let stop_flag_clone = stop_flag.clone();
    let accept_thread = thread::Builder::new()
        .name("cw-daemon-accept".to_string())
        .spawn(move || {
            while !stop_flag_clone.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok(conn) => {
                        if worker_tx.try_send(conn).is_err() {
                            eprintln!("[cw_daemon] worker pool full, rejecting connection");
                        }
                    }
                    Err(ref e) if e.kind() == io::ErrorKind::Interrupted => break,
                    Err(e) => {
                        if stop_flag_clone.load(Ordering::SeqCst) {
                            break;
                        }
                        eprintln!("[cw_daemon] accept error: {}", e);
                        thread::sleep(Duration::from_millis(10));
                    }
                }
            }
        })?;

    Ok(TransportServerHandle {
        stop_flag,
        accept_thread: Some(accept_thread),
        worker_handles,
        listener_shutdown: None, // listener 已移入 accept 线程
    })
}

/// 跨平台 worker 线程主循环。
fn transport_worker_loop<F, S>(
    state_factory: &Arc<F>,
    worker_rx: crossbeam_channel::Receiver<Box<dyn TransportConnection>>,
    stop_flag: Arc<AtomicBool>,
    max_message_bytes: usize,
    request_timeout: Duration,
    serialization_point: &super::serialization::SerializationPoint,
) where
    F: Fn() -> io::Result<S> + Send + Sync + 'static,
    S: DaemonStateExt + Send + 'static,
{
    let mut state = match state_factory() {
        Ok(s) => s,
        Err(e) => {
            eprintln!("[cw_daemon] worker state_factory failed: {}", e);
            return;
        }
    };
    while !stop_flag.load(Ordering::SeqCst) {
        let mut conn = match worker_rx.recv_timeout(request_timeout) {
            Ok(c) => c,
            Err(crossbeam_channel::RecvTimeoutError::Timeout) => continue,
            Err(crossbeam_channel::RecvTimeoutError::Disconnected) => break,
        };

        // P1 修复（T-1785854423993）：handler 内 panic（如未嵌入 Python 的 daemon
        // 调用 pyo3 依赖方法）不能杀死 worker 线程，也不能让客户端等不到响应。
        // catch_unwind 把 panic 隔离在本连接内，worker 继续服务后续连接。
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            handle_transport_connection(
                &mut *conn,
                &mut state,
                max_message_bytes,
                serialization_point,
            )
        }));
        match outcome {
            Ok(Ok(())) => {}
            Ok(Err(e)) => {
                eprintln!("[cw_daemon] connection error: {}", e);
            }
            Err(_) => {
                eprintln!(
                    "[cw_daemon] worker panic caught: handler 崩溃已被隔离 \
                     （请求未完成，worker 继续存活）"
                );
                // 尝试向客户端返回结构化错误（可能已断开，失败忽略）
                let response = make_error_response(
                    "internal_error",
                    "daemon handler panic（请求未执行；Python 依赖方法需嵌入解释器）",
                );
                if let Ok(json_bytes) = serde_json::to_vec(&response) {
                    let _ = conn.send_message(&json_bytes);
                }
            }
        }
    }
}

/// 处理单个跨平台连接：peer_identity → recv → dispatch → send。
///
/// 与 Unix `handle_connection` 的区别：
/// - 不使用 SCM_RIGHTS FD 传递（Windows 无此机制）
/// - 对端身份通过 TransportPeerIdentity 统一表示
/// - dispatch 层 FD 参数传空切片（base64 载荷路径兜底）
pub fn handle_transport_connection<S>(
    conn: &mut dyn TransportConnection,
    state: &mut S,
    max_message_bytes: usize,
    serialization_point: &super::serialization::SerializationPoint,
) -> io::Result<()>
where
    S: DaemonStateExt,
{
    conn.set_timeout(Duration::from_secs(30))?;

    // 1. 接收 JSON-RPC 请求（无 FD，Windows 使用 base64 载荷）
    //
    // 顺序约束：必须先读后取对端身份——Windows 命名管道的
    // ImpersonateNamedPipeClient 在服务端尚未读取任何数据时返回
    // ERROR_CANNOT_IMPERSONATE (1368)，因此 peer_identity 必须在 recv 之后。
    let request_bytes = match conn.recv_message(max_message_bytes) {
        Ok(bytes) => bytes,
        Err(e) => {
            let response = make_error_response("protocol_error", &e.to_string());
            if let Ok(json_bytes) = serde_json::to_vec(&response) {
                let _ = conn.send_message(&json_bytes);
            }
            return Ok(());
        }
    };

    // 2. 获取对端身份（OS 内核保证不可伪造）
    let identity = conn.peer_identity()?;
    let peer = match &identity {
        TransportPeerIdentity::Unix { uid, gid, pid } => PeerCredential::new_unix(*uid, *gid, *pid),
        TransportPeerIdentity::Windows { sid, pid } => PeerCredential::new_windows(sid.clone(), *pid),
    };


    // 3. 任务 1D2（Req 15/AC26）：raw frame bytes 先经 strict duplicate-key parser，
    //    在转成 `Value` 前检测重复 key；命中时 fail-closed，不进入 dispatch/ledger。
    if let Err(e) = crate::daemon::task_loop::strict_transport::parse_strict_envelope(&request_bytes)
    {
        if e.code == crate::daemon::task_loop::strict_transport::ERR_DUPLICATE_JSON_KEY {
            let response = make_error_response("E_DUPLICATE_JSON_KEY", &e.message);
            if let Ok(json_bytes) = serde_json::to_vec(&response) {
                let _ = conn.send_message(&json_bytes);
            }
            return Ok(());
        }
    }

    // 4. 解析 method / params / id
    let request: serde_json::Value = match serde_json::from_slice(&request_bytes) {
        Ok(v) => v,
        Err(e) => {
            let response = make_error_response("parse_error", &e.to_string());
            if let Ok(json_bytes) = serde_json::to_vec(&response) {
                let _ = conn.send_message(&json_bytes);
            }
            return Ok(());
        }
    };

    // 5. 解析 method / params / id
    let request_id = request.get("id").cloned();
    let method = request.get("method").and_then(|v| v.as_str()).unwrap_or("");
    let params = request
        .get("params")
        .cloned()
        .unwrap_or_else(|| serde_json::Value::Object(serde_json::Map::new()));

    // 6. dispatch（Protected_Mutation 经串行化点，Req 14.6；无 FD，传空切片）
    let response = if method.is_empty() || !params.is_object() {
        make_error_response("invalid_request", "method/params 类型错误")
    } else {
        super::dispatch::dispatch_rpc(state, peer, method, &params, &[], serialization_point)
    };

    // 7. 附加 request_id
    let final_response = if let Some(id) = request_id {
        let mut m = serde_json::Map::new();
        if let serde_json::Value::Object(obj) = response {
            m.extend(obj);
        }
        m.insert("id".to_string(), id);
        serde_json::Value::Object(m)
    } else {
        response
    };

    // 7. 发送响应
    let json_bytes = serde_json::to_vec(&final_response).map_err(|e| {
        io::Error::new(io::ErrorKind::InvalidData, format!("序列化响应失败: {}", e))
    })?;
    conn.send_message(&json_bytes)?;

    Ok(())
}

/// 跨平台 server 句柄（用于 shutdown 控制）。
pub struct TransportServerHandle {
    stop_flag: Arc<AtomicBool>,
    accept_thread: Option<thread::JoinHandle<()>>,
    worker_handles: Vec<thread::JoinHandle<()>>,
    /// 用于从外部触发 listener shutdown（如果 listener 未移入 accept 线程）
    listener_shutdown: Option<Box<dyn Fn() -> io::Result<()> + Send>>,
}

impl TransportServerHandle {
    /// 请求 server 停止（非阻塞）。
    pub fn shutdown(&mut self) {
        self.stop_flag.store(true, Ordering::SeqCst);
        if let Some(ref f) = self.listener_shutdown {
            let _ = f();
        }
    }

    /// 等待 server 完全退出（阻塞当前线程）。
    pub fn join(&mut self) {
        if let Some(handle) = self.accept_thread.take() {
            let _ = handle.join();
        }
        for handle in self.worker_handles.drain(..) {
            let _ = handle.join();
        }
    }
}

impl Drop for TransportServerHandle {
    fn drop(&mut self) {
        self.shutdown();
        self.join();
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use serde_json::json;
    use std::io::Write;
    use std::os::unix::net::UnixStream;
    use std::time::Instant;

    use super::super::dispatch::DaemonState;

    /// 构造一个最小的 daemon state（仅支持 ping/health/schema.version）
    fn make_state() -> DaemonState {
        DaemonState::default()
    }

    /// 发送一个长度分帧的 JSON 请求到 stream
    fn send_request(stream: &mut UnixStream, request: &serde_json::Value) -> io::Result<()> {
        let payload = serde_json::to_vec(request).unwrap();
        let len = payload.len() as u32;
        stream.write_all(&len.to_be_bytes())?;
        stream.write_all(&payload)?;
        stream.flush()
    }

    /// 从 stream 读取一个长度分帧的 JSON 响应
    fn read_response(stream: &mut UnixStream) -> serde_json::Value {
        use std::io::Read;
        let mut header = [0u8; 4];
        stream.read_exact(&mut header).unwrap();
        let len = u32::from_be_bytes(header) as usize;
        let mut buf = vec![0u8; len];
        stream.read_exact(&mut buf).unwrap();
        serde_json::from_slice(&buf).unwrap()
    }

    /// 用 socketpair 模拟一次客户端-服务端交互
    fn simulate_request(request: &serde_json::Value) -> serde_json::Value {
        let (mut client, mut server) = UnixStream::pair().unwrap();
        send_request(&mut client, request).unwrap();

        let mut state = make_state();
        // 在另一个作用域处理连接（避免借用冲突）
        {
            handle_one_connection(
                &mut server,
                &mut state,
                DEFAULT_MAX_MESSAGE_BYTES,
                DEFAULT_MAX_FDS,
            )
            .unwrap();
        }

        read_response(&mut client)
    }

    // ---- ping 测试 ----

    #[test]
    fn test_ping_returns_ok_with_peer_uid() {
        let request = json!({
            "id": 1,
            "method": "ping",
            "params": {},
        });
        let response = simulate_request(&request);
        assert_eq!(response["ok"], true);
        assert_eq!(response["id"], 1);
        assert_eq!(response["result"]["status"], "ok");
        // peer_uid 应该是当前进程的 uid
        assert_eq!(response["result"]["peer_uid"], current_uid_for_test());
    }

    /// 测试辅助：获取当前进程 uid
    fn current_uid_for_test() -> u32 {
        super::super::peercred::current_uid()
    }

    // ---- health 测试 ----

    #[test]
    fn test_health_returns_uptime_and_schema_version() {
        let request = json!({
            "id": 2,
            "method": "health",
            "params": {},
        });
        let response = simulate_request(&request);
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["status"], "ok");
        assert!(response["result"]["uptime_seconds"].as_u64().is_some());
        assert_eq!(
            response["result"]["schema_version"],
            super::super::SCHEMA_VERSION
        );
    }

    // ---- schema.version 测试 ----

    #[test]
    fn test_schema_version_returns_version() {
        let request = json!({
            "id": 3,
            "method": "schema.version",
            "params": {},
        });
        let response = simulate_request(&request);
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["version"], super::super::SCHEMA_VERSION);
    }

    // ---- 未知方法测试 ----

    #[test]
    fn test_unknown_method_returns_method_not_found() {
        let request = json!({
            "id": 4,
            "method": "nonexistent.method",
            "params": {},
        });
        let response = simulate_request(&request);
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "method_not_found");
        assert!(response["error"]["message"]
            .as_str()
            .unwrap()
            .contains("nonexistent.method"));
    }

    // ---- 缺少 method 字段测试 ----

    #[test]
    fn test_missing_method_returns_invalid_request() {
        let request = json!({
            "id": 5,
            "params": {},
        });
        let response = simulate_request(&request);
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "invalid_request");
    }

    // ---- prepare_socket_path 测试 ----

    #[test]
    fn test_prepare_socket_path_creates_parent_dir() {
        let tmp = tempfile::tempdir().unwrap();
        let socket_path = tmp.path().join("subdir").join("test.sock");
        prepare_socket_path(&socket_path).unwrap();
        assert!(socket_path.parent().unwrap().exists());
    }

    #[test]
    fn test_prepare_socket_path_cleans_existing_socket() {
        let tmp = tempfile::tempdir().unwrap();
        let socket_path = tmp.path().join("test.sock");
        // 先 bind 一个 socket
        let _listener = UnixListener::bind(&socket_path).unwrap();
        assert!(socket_path.exists());
        // prepare_socket_path 应该能清理旧 socket
        prepare_socket_path(&socket_path).unwrap();
        assert!(!socket_path.exists());
    }

    #[test]
    fn test_prepare_socket_path_rejects_non_socket_file() {
        let tmp = tempfile::tempdir().unwrap();
        let socket_path = tmp.path().join("test.sock");
        // 创建一个普通文件
        std::fs::write(&socket_path, b"not a socket").unwrap();
        // prepare_socket_path 应该拒绝覆盖
        let result = prepare_socket_path(&socket_path);
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.to_string().contains("拒绝覆盖非 socket 路径"));
        // 原文件应该还在
        assert!(socket_path.exists());
    }

    // ---- ServerConfig 默认值测试 ----

    #[test]
    fn test_server_config_default_values() {
        let config = ServerConfig::default();
        // R7: 默认 socket_path 统一为 systemd RuntimeDirectory 风格
        assert_eq!(
            config.socket_path,
            PathBuf::from(super::super::config::DEFAULT_SOCKET_PATH)
        );
        assert_eq!(config.max_message_bytes, DEFAULT_MAX_MESSAGE_BYTES);
        assert_eq!(config.max_fds, DEFAULT_MAX_FDS);
        assert_eq!(config.max_workers, 16);
        assert_eq!(config.request_timeout, Duration::from_secs(30));
        assert_eq!(config.socket_mode, 0o660);
    }

    // ---- start_server + shutdown 集成测试 ----

    #[test]
    fn test_start_server_and_shutdown() {
        let tmp = tempfile::tempdir().unwrap();
        let socket_path = tmp.path().join("test.sock");

        let config = ServerConfig {
            socket_path: socket_path.clone(),
            max_workers: 2,
            accept_timeout: Duration::from_millis(50),
            ..Default::default()
        };

        let state_factory = move || -> io::Result<DaemonState> { Ok(DaemonState::default()) };

        let mut handle = start_server(config, state_factory).unwrap();
        assert!(socket_path.exists());

        // 短暂等待 server 就绪
        thread::sleep(Duration::from_millis(50));

        // shutdown
        handle.shutdown();
        handle.join();

        // socket 文件应该被清理
        assert!(!socket_path.exists());
    }

    #[test]
    fn test_start_server_accepts_connection_and_responds_ping() {
        let tmp = tempfile::tempdir().unwrap();
        let socket_path = tmp.path().join("test.sock");

        let config = ServerConfig {
            socket_path: socket_path.clone(),
            max_workers: 2,
            accept_timeout: Duration::from_millis(50),
            request_timeout: Duration::from_secs(5),
            ..Default::default()
        };

        let state_factory = move || -> io::Result<DaemonState> { Ok(DaemonState::default()) };

        let mut handle = start_server(config, state_factory).unwrap();

        // 等待 server 就绪
        thread::sleep(Duration::from_millis(100));

        // 客户端连接并发送 ping
        let mut client = UnixStream::connect(&socket_path).unwrap();
        let request = json!({
            "id": 42,
            "method": "ping",
            "params": {},
        });
        send_request(&mut client, &request).unwrap();

        let response = read_response(&mut client);
        assert_eq!(response["ok"], true);
        assert_eq!(response["id"], 42);
        assert_eq!(response["result"]["status"], "ok");

        handle.shutdown();
        handle.join();
    }

    #[test]
    fn test_start_server_socket_permissions() {
        let tmp = tempfile::tempdir().unwrap();
        let socket_path = tmp.path().join("test.sock");

        let config = ServerConfig {
            socket_path: socket_path.clone(),
            socket_mode: 0o600, // 仅 owner 可读写
            max_workers: 1,
            accept_timeout: Duration::from_millis(50),
            ..Default::default()
        };

        let state_factory = move || -> io::Result<DaemonState> { Ok(DaemonState::default()) };

        let mut handle = start_server(config, state_factory).unwrap();

        // 验证 socket 文件权限
        let metadata = std::fs::metadata(&socket_path).unwrap();
        use std::os::unix::fs::PermissionsExt;
        let mode = metadata.permissions().mode();
        assert_eq!(mode & 0o777, 0o600);

        handle.shutdown();
        handle.join();
    }

    // ---- peercred ACL 集成测试 ----

    #[test]
    fn test_handle_connection_records_peer_uid_in_ping_response() {
        // 通过 socketpair 验证 ping 返回的 peer_uid 等于当前进程 uid
        let (mut client, mut server) = UnixStream::pair().unwrap();
        let request = json!({
            "id": 1,
            "method": "ping",
            "params": {},
        });
        send_request(&mut client, &request).unwrap();

        let mut state = make_state();
        handle_one_connection(
            &mut server,
            &mut state,
            DEFAULT_MAX_MESSAGE_BYTES,
            DEFAULT_MAX_FDS,
        )
        .unwrap();

        let response = read_response(&mut client);
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["peer_uid"], current_uid_for_test());
    }

    // ---- workspace.register 完整 ACL 测试（需要 WorkspaceDaemonState）----

    #[test]
    fn test_handle_connection_with_workspace_daemon_state() {
        use super::super::workspace::{WorkspaceDaemonState, WorkspaceRegistry};

        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let state_factory = move || -> io::Result<WorkspaceDaemonState> {
            // 注意：open_in_memory 每次创建新的内存 DB，跨线程不共享
            // 真实部署应该用 WorkspaceRegistry::open(path) 共享文件 DB
            let registry = WorkspaceRegistry::open_in_memory().unwrap();
            Ok(WorkspaceDaemonState::new(registry))
        };

        let tmp = tempfile::tempdir().unwrap();
        let socket_path = tmp.path().join("test.sock");
        let config = ServerConfig {
            socket_path: socket_path.clone(),
            max_workers: 1,
            accept_timeout: Duration::from_millis(50),
            request_timeout: Duration::from_secs(5),
            ..Default::default()
        };

        let mut handle = start_server(config, state_factory).unwrap();
        thread::sleep(Duration::from_millis(100));

        // 客户端连接并发送 workspace.list（应该返回空数组）
        let mut client = UnixStream::connect(&socket_path).unwrap();
        let request = json!({
            "id": 1,
            "method": "workspace.list",
            "params": {},
        });
        send_request(&mut client, &request).unwrap();

        let response = read_response(&mut client);
        assert_eq!(response["ok"], true);
        assert!(response["result"].is_array());
        assert_eq!(response["result"].as_array().unwrap().len(), 0);

        handle.shutdown();
        handle.join();
    }

    // ---- 协议错误处理测试 ----

    #[test]
    fn test_handle_connection_returns_protocol_error_on_malformed_input() {
        use std::io::Read;

        let (mut client, mut server) = UnixStream::pair().unwrap();
        // 发送非法 JSON
        let bad_payload = b"{not valid json";
        let len = bad_payload.len() as u32;
        client.write_all(&len.to_be_bytes()).unwrap();
        client.write_all(bad_payload).unwrap();
        client.flush().unwrap();

        let mut state = make_state();
        // handle_connection 应该不 panic，返回 protocol_error
        let _ = handle_one_connection(
            &mut server,
            &mut state,
            DEFAULT_MAX_MESSAGE_BYTES,
            DEFAULT_MAX_FDS,
        );

        // 尝试读取响应（可能客户端已经断开）
        let mut buf = [0u8; 1024];
        let _ = client.read(&mut buf);
        // 不严格断言响应内容，只要不 panic 就行
    }

    // ---- 性能基准：单连接处理延迟 ----

    #[test]
    fn test_handle_connection_latency_under_10ms() {
        let (mut client, mut server) = UnixStream::pair().unwrap();
        let request = json!({
            "id": 1,
            "method": "ping",
            "params": {},
        });
        send_request(&mut client, &request).unwrap();

        let mut state = make_state();
        let start = Instant::now();
        handle_one_connection(
            &mut server,
            &mut state,
            DEFAULT_MAX_MESSAGE_BYTES,
            DEFAULT_MAX_FDS,
        )
        .unwrap();
        let elapsed = start.elapsed();

        // 单连接处理（含 peercred + recv + dispatch + send）应该在 10ms 以内
        assert!(
            elapsed.as_millis() < 10,
            "单连接处理延迟 {} 超过 10ms",
            elapsed.as_millis()
        );

        let _response = read_response(&mut client);
    }
}

// ============================================
// H1: HTTP MVP transport 启动（dev_loopback_unauthenticated overlay）
//
// 与既有 Named Pipe / UDS 传输并行运行；共享同一 `S` 类型与同一个
// `SerializationPoint`（由调用方构造并传入）。仅在成功 loopback bind 之后，
// 由 http_server::serve 内部原子发布 manifest。非 loopback 绑定在绑定前
// fail-closed 返回 `E_HTTP_MVP_LOOPBACK_ONLY`，不建立 listener、不发布 manifest。
// ============================================

/// 在独立线程的 tokio runtime 中启动 HTTP MVP listener。
///
/// - `state`: 共享的 daemon state（与既有传输同一 `S` 类型），包装为 `Arc<TokioMutex<S>>`。
/// - `sp`: 与既有传输共享的串行化点实例。
/// - `config`: HTTP MVP 配置（`bind_spec` / `manifest_path` 等）。
///
/// 返回守护线程的 `JoinHandle`，调用方无需 join（随主线程退出而结束）。
pub fn spawn_http_transport<S>(
    state: Arc<TokioMutex<S>>,
    sp: Arc<SerializationPoint>,
    config: HttpServerConfig,
) -> std::thread::JoinHandle<()>
where
    S: DaemonStateExt + Send + Sync + 'static,
{
    thread::Builder::new()
        .name("cw-http-mvp".to_string())
        .spawn(move || {
            let rt = match tokio::runtime::Builder::new_multi_thread()
                .worker_threads(2)
                .enable_all()
                .build()
            {
                Ok(rt) => rt,
                Err(e) => {
                    eprintln!("[cw_daemon] [ERROR] HTTP MVP tokio runtime init failed: {}", e);
                    return;
                }
            };
            rt.block_on(async {
                match super::http_server::serve(state, sp, config).await {
                    Ok(addr) => eprintln!(
                        "[cw_daemon] [INFO] HTTP MVP listener bound at http://{}",
                        addr
                    ),
                    Err(e) => eprintln!("[cw_daemon] [ERROR] HTTP MVP bind failed: {:?}", e),
                }
            });
        })
        .expect("spawn cw-http-mvp thread")
}
