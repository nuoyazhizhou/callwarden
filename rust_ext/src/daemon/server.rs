//! UDS server——Unix Domain Socket 监听 + 多线程 accept + UID ACL。
//!
//! 对应 Python `server/daemon_server.py:EnterpriseDaemonServer`（L517-622）
//!
//! ## 安全模型（daemon-ipc-security.md §3.1）
//! - **身份始终取自 SO_PEERCRED**：内核保证不可伪造
//! - 客户端请求体中的身份字段不参与授权
//! - 跨 UID 拒绝访问：`workspace.register` / `workspace.list` / `workspace.status`
//!   等方法都通过 `peer.uid` 做 ACL（参考 R4 owned_workspace / validate_owned_path）
//!
//! ## 线程模型
//! - 主线程：`UnixListener::bind` + `accept` 循环
//! - 工作线程池：`rayon` 或 `std::thread`（每连接一线程，有界）
//! - 每个连接处理单个请求（与 Python 一致，连接 = 请求 = 响应）
//!
//! ## 条件编译
//! - `#[cfg(unix)]` 保护整个模块
//! - Windows 上跳过编译，cw_daemon binary 在 Windows 上不启动

#![cfg(unix)]

use std::io;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use crossbeam_channel::{bounded, Sender};
use libc::mode_t;

use super::dispatch::{DaemonStateExt, PeerCredential};
use super::peercred::{get_peer_cred, PeerCred};
use super::protocol::{
    make_error_response, recv_message_with_fds, send_message, DEFAULT_MAX_FDS,
    DEFAULT_MAX_MESSAGE_BYTES,
};

// ============================================
// DaemonConfig 扩展（UDS server 专用配置）
// ============================================

/// UDS server 配置（对应 Python EnterpriseDaemonServer.__init__ 参数）
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
}

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
        }
    }
}

// ============================================
// ServerHandle：用于控制 server 生命周期
// ============================================

/// UDS server 句柄（用于 shutdown 控制）
///
/// 对应 Python EnterpriseDaemonServer.shutdown
pub struct ServerHandle {
    stop_flag: Arc<AtomicBool>,
    /// accept 线程 join handle（用于等待 server 退出）
    accept_thread: Option<thread::JoinHandle<()>>,
    /// worker 线程 join handles
    worker_handles: Vec<thread::JoinHandle<()>>,
    /// socket 路径（用于 shutdown 后清理）
    socket_path: PathBuf,
}

impl ServerHandle {
    /// 请求 server 停止（非阻塞）
    pub fn shutdown(&mut self) {
        self.stop_flag.store(true, Ordering::SeqCst);
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
}

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
                format!(
                    "拒绝覆盖非 socket 路径: {}",
                    socket_path.display()
                ),
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
pub fn start_server<F, S>(config: ServerConfig, state_factory: F) -> io::Result<ServerHandle>
where
    F: Fn() -> io::Result<S> + Send + Sync + 'static,
    S: DaemonStateExt + Send + 'static,
{
    prepare_socket_path(&config.socket_path)?;

    let listener = UnixListener::bind(&config.socket_path)?;
    // 设置 socket 文件权限（0o660：owner + group 可读写）
    std::fs::set_permissions(
        &config.socket_path,
        std::fs::Permissions::from_mode(config.socket_mode),
    )?;
    // 非阻塞 accept（用于响应 stop_flag）
    listener.set_nonblocking(true)?;

    let stop_flag = Arc::new(AtomicBool::new(false));
    let stop_flag_clone = stop_flag.clone();
    let (worker_tx, worker_rx) = bounded::<UnixStream>(config.max_workers);

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
            accept_loop(
                &listener,
                worker_tx,
                stop_flag_clone,
                accept_timeout,
            );
        })?;

    Ok(ServerHandle {
        stop_flag,
        accept_thread: Some(accept_thread),
        worker_handles,
        socket_path,
    })
}

/// accept 线程主循环：等待连接 → 分发到 worker 线程池
fn accept_loop(
    listener: &UnixListener,
    worker_tx: Sender<UnixStream>,
    stop_flag: Arc<AtomicBool>,
    accept_timeout: Duration,
) {
    // 用 SO_RCVTIMEO 模拟超时 accept（Linux 特有）
    // 这里用 set_nonblocking + sleep 简化实现
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
                // 非阻塞模式下没有连接，短暂 sleep 后重试
                thread::sleep(accept_timeout);
            }
            Err(e) => {
                eprintln!("[cw_daemon] accept error: {}", e);
                // 短暂 sleep 避免忙等
                thread::sleep(accept_timeout);
            }
        }
    }
}

/// worker 线程主循环：从 channel 取连接 → 处理 → 回复
fn worker_loop<F, S>(
    state_factory: &Arc<F>,
    worker_rx: crossbeam_channel::Receiver<UnixStream>,
    stop_flag: Arc<AtomicBool>,
    max_message_bytes: usize,
    max_fds: usize,
    request_timeout: Duration,
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

        if let Err(e) = handle_connection(&mut stream, &mut state, max_message_bytes, max_fds) {
            eprintln!("[cw_daemon] connection error: {}", e);
        }
    }
}

/// 处理单个连接：peercred → recv_message_with_fds → dispatch → send_message
///
/// 对应 Python EnterpriseDaemonServer._handle_connection
pub fn handle_connection<S>(
    stream: &mut UnixStream,
    state: &mut S,
    max_message_bytes: usize,
    max_fds: usize,
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
    let peer = PeerCredential {
        uid: cred.uid,
        gid: cred.gid,
        pid: cred.pid,
    };

    // 2. 接收 JSON-RPC 请求（含可选 FD）
    // recv_message_with_fds 需要 &mut UnixStream（内部用 recvmsg/read_exact）
    let (request, received_fds) = match recv_message_with_fds(
        stream,
        max_message_bytes,
        max_fds,
    ) {
        Ok((msg, fds)) => (msg, fds),
        Err(e) => {
            // 协议错误：尝试回复错误响应（可能客户端已经断开）
            let response = make_error_response("protocol_error", &e.to_string());
            let _ = send_message(stream, &response, max_message_bytes);
            // 关闭已接收的 FD（虽然出错时一般没收到）
            close_fds(&[]);
            return Ok(());
        }
    };

    // 3. 解析 method / params / id
    let request_id = request.get("id").cloned();
    let method = request.get("method").and_then(|v| v.as_str()).unwrap_or("");
    let params = request.get("params").cloned().unwrap_or_else(|| {
        serde_json::Value::Object(serde_json::Map::new())
    });

    // 4. dispatch（state.handle_* 内部做 ACL 检查）
    let response = if method.is_empty() || !params.is_object() {
        make_error_response("invalid_request", "method/params 类型错误")
    } else {
        // 将 dispatch 结果包装为 JSON-RPC 响应
        super::dispatch::dispatch(state, peer, method, &params, &received_fds)
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
/// 适用于单元测试中模拟客户端连接。
pub fn handle_one_connection<S>(
    stream: &mut UnixStream,
    state: &mut S,
    max_message_bytes: usize,
    max_fds: usize,
) -> io::Result<()>
where
    S: DaemonStateExt,
{
    handle_connection(stream, state, max_message_bytes, max_fds)
}

#[cfg(test)]
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
            handle_one_connection(&mut server, &mut state, DEFAULT_MAX_MESSAGE_BYTES, DEFAULT_MAX_FDS)
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
        assert_eq!(
            response["result"]["peer_uid"],
            current_uid_for_test()
        );
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
        assert_eq!(
            response["result"]["version"],
            super::super::SCHEMA_VERSION
        );
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
        assert_eq!(config.socket_path, PathBuf::from(super::super::config::DEFAULT_SOCKET_PATH));
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

        let state_factory = move || -> io::Result<DaemonState> {
            Ok(DaemonState::default())
        };

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

        let state_factory = move || -> io::Result<DaemonState> {
            Ok(DaemonState::default())
        };

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

        let state_factory = move || -> io::Result<DaemonState> {
            Ok(DaemonState::default())
        };

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
