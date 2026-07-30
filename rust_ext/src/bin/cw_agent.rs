//! cw-agent binary（Phase 5-2 Slice 6）
//!
//! Call Warden agent：文件监控 + session 管理 + UDS RPC 通知 daemon。
//!
//! 子命令：
//! - `start`：启动 agent（握手 + 文件监控循环）
//! - `stop`：停止运行中的 agent
//! - `status`：查询 agent 运行状态
//!
//! 跨平台编译：
//! - Unix（Linux/macOS）：watcher 循环可用，完整功能
//! - Windows：watcher 不可用，返回平台提示（exit 2）
//!
//! 契约：
//! - 对齐 Python cli/main.py:run_agent_mode (L12023+)
//! - 对齐 Python server/agent_watcher.py:AgentWatcher
//! - 对齐 Python server/agent_session.py:AgentSession
//! - 对齐 Python server/agent_protocol.py:user_agent_connect / build_refresh_message

use clap::{Parser, Subcommand};
use std::path::PathBuf;

/// Call Warden — agent watcher + session
#[derive(Parser)]
#[command(
    name = "cw-agent",
    version,
    about = "Call Warden agent (file watcher + session management)"
)]
struct Cli {
    /// daemon socket 路径
    #[arg(long, default_value = "/tmp/callwarden_daemon.sock", global = true)]
    socket: String,

    /// RPC 超时（秒）
    #[arg(long, default_value_t = 30, global = true)]
    timeout: u64,

    /// 子命令
    #[command(subcommand)]
    command: Option<Commands>,
}

/// cw-agent 子命令
#[derive(Subcommand)]
enum Commands {
    /// 启动 agent（握手 + 文件监控循环）
    Start {
        /// 监控目录（workspace 根路径）
        root: String,

        /// workspace instance ID（16 位 hex）
        #[arg(long)]
        workspace_id: String,

        /// agent session ID（不指定则自动生成）
        #[arg(long)]
        session_id: Option<String>,

        /// 防抖时间（毫秒，默认 1000）
        #[arg(long, default_value_t = 1000)]
        debounce_ms: u64,
    },

    /// 停止运行中的 agent
    Stop,

    /// 查询 agent 运行状态
    Status,
}

fn main() {
    let cli = Cli::parse();
    match cli.command {
        Some(Commands::Start {
            root,
            workspace_id,
            session_id,
            debounce_ms,
        }) => {
            run_start(
                &cli.socket,
                cli.timeout,
                &root,
                &workspace_id,
                session_id.as_deref(),
                debounce_ms,
            );
        }
        Some(Commands::Stop) => run_stop(),
        Some(Commands::Status) => run_status(),
        None => {
            Cli::parse_from(["cw-agent", "--help"]);
        }
    }
}

/// 执行 start 子命令。
fn run_start(
    socket: &str,
    timeout_secs: u64,
    root: &str,
    workspace_id: &str,
    session_id: Option<&str>,
    debounce_ms: u64,
) {
    // 生成或使用指定的 session_id
    let sid = session_id
        .map(|s| s.to_string())
        .unwrap_or_else(callwarden_core::daemon::client::AgentSession::generate_session_id);

    // 创建 AgentSession
    let mut session = callwarden_core::daemon::client::AgentSession::new(sid.clone());
    session.register_workspace(workspace_id);

    eprintln!("cw-agent: session_id={}", sid);
    eprintln!("cw-agent: workspace_id={}", workspace_id);
    eprintln!("cw-agent: watch_dir={}", root);
    eprintln!("cw-agent: debounce_ms={}", debounce_ms);

    // 构建 connect 参数（跨平台逻辑，Windows 也可验证）
    let (connect_method, connect_params) =
        callwarden_core::daemon::client::build_connect_params(workspace_id, &sid);
    eprintln!(
        "cw-agent: would call RPC: {} with params: {}",
        connect_method, connect_params
    );

    #[cfg(unix)]
    {
        run_start_unix(
            socket,
            timeout_secs,
            root,
            workspace_id,
            &mut session,
            debounce_ms,
        )
    }
    #[cfg(not(unix))]
    {
        let _ = (socket, timeout_secs);
        eprintln!("cw-agent: watcher not available on this platform (Linux/macOS only)");
        std::process::exit(2);
    }
}

/// 执行 stop 子命令。
fn run_stop() {
    let pid_file = agent_pid_file();
    match std::fs::read_to_string(&pid_file) {
        Ok(pid_str) => {
            let pid: u32 = pid_str.trim().parse().unwrap_or(0);
            if pid > 0 {
                eprintln!("cw-agent: would send SIGTERM to PID {}", pid);
                #[cfg(unix)]
                {
                    stop_unix(pid)
                }
                #[cfg(not(unix))]
                {
                    eprintln!("cw-agent: signal sending not available on Windows");
                    std::process::exit(2);
                }
            } else {
                eprintln!("cw-agent: invalid PID in {}", pid_file.display());
                std::process::exit(1);
            }
        }
        Err(_) => {
            eprintln!("cw-agent: no running agent (PID file not found)");
            std::process::exit(1);
        }
    }
}

/// 执行 status 子命令。
fn run_status() {
    let pid_file = agent_pid_file();
    match std::fs::read_to_string(&pid_file) {
        Ok(pid_str) => {
            let pid: u32 = pid_str.trim().parse().unwrap_or(0);
            if pid > 0 {
                println!("Agent running: PID {}", pid);
                println!("PID file: {}", pid_file.display());

                // 检查进程是否存活
                #[cfg(unix)]
                {
                    let alive = unsafe { libc::kill(pid as i32, 0) } == 0;
                    println!("Process alive: {}", alive);
                }
                #[cfg(not(unix))]
                {
                    println!("Process alive: unknown (Windows)");
                }
            } else {
                println!("Agent not running (invalid PID)");
            }
        }
        Err(_) => {
            println!("Agent not running (PID file not found)");
        }
    }
}

/// 获取 agent PID 文件路径。
///
/// 对齐 Python `~/.callwarden/agent.pid` (cli/main.py L12072)
fn agent_pid_file() -> PathBuf {
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home).join(".callwarden").join("agent.pid")
}

#[cfg(unix)]
fn run_start_unix(
    socket: &str,
    timeout_secs: u64,
    root: &str,
    workspace_id: &str,
    session: &mut callwarden_core::daemon::client::AgentSession,
    debounce_ms: u64,
) {
    use callwarden_core::daemon::client::unix::UnixDaemonRpcClient;
    use callwarden_core::watcher::{DebouncedFileWatcher, FileEventKind};
    use signal_hook::consts::{SIGINT, SIGTERM};
    use signal_hook::flag as signal_flag;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;
    use std::time::Duration;

    let client = UnixDaemonRpcClient::new(socket).with_timeout(Duration::from_secs(timeout_secs));

    // 1. ping daemon
    eprintln!("cw-agent: pinging daemon...");
    match client.ping() {
        Ok(_) => eprintln!("cw-agent: daemon reachable"),
        Err(e) => {
            eprintln!("cw-agent: daemon unreachable: {}", e);
            std::process::exit(1);
        }
    }

    // 2. 握手 workspace.connect
    eprintln!("cw-agent: connecting to workspace {}...", workspace_id);
    let (method, params) =
        callwarden_core::daemon::client::build_connect_params(workspace_id, &session.session_id);
    match client.call(&method, params) {
        Ok(result) => {
            let epoch = result
                .get("session_epoch")
                .and_then(|v| v.as_u64())
                .unwrap_or(0);
            if epoch < 1 {
                eprintln!("cw-agent: invalid session_epoch: {}", epoch);
                std::process::exit(1);
            }
            session.set_epoch(workspace_id, epoch);
            eprintln!("cw-agent: handshake success, epoch={}", epoch);
        }
        Err(e) => {
            eprintln!("cw-agent: handshake failed: {}", e);
            std::process::exit(1);
        }
    }

    // 3. 写 PID 文件
    let pid = std::process::id();
    let pid_file = agent_pid_file();
    if let Some(parent) = pid_file.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Err(e) = std::fs::write(&pid_file, pid.to_string()) {
        eprintln!("cw-agent: failed to write PID file: {}", e);
    }

    // 4. 启动 watcher 循环
    eprintln!(
        "cw-agent: starting watcher on {} (debounce={}ms)",
        root, debounce_ms
    );
    let watcher = DebouncedFileWatcher::new(
        root,
        callwarden_core::watcher::default_supported_extensions(),
        debounce_ms,
    );
    if let Err(e) = watcher.start() {
        eprintln!("cw-agent: failed to start watcher: {}", e);
        let _ = std::fs::remove_file(&pid_file);
        std::process::exit(1);
    }

    let stop_flag = Arc::new(AtomicBool::new(false));
    if let Err(e) = signal_flag::register(SIGTERM, Arc::clone(&stop_flag)) {
        eprintln!("cw-agent: failed to register SIGTERM handler: {}", e);
        watcher.stop();
        let _ = std::fs::remove_file(&pid_file);
        std::process::exit(1);
    }
    if let Err(e) = signal_flag::register(SIGINT, Arc::clone(&stop_flag)) {
        eprintln!("cw-agent: failed to register SIGINT handler: {}", e);
        watcher.stop();
        let _ = std::fs::remove_file(&pid_file);
        std::process::exit(1);
    }

    while !stop_flag.load(Ordering::Relaxed) {
        for event in watcher.poll_events() {
            match event.kind {
                FileEventKind::Created | FileEventKind::Modified => {
                    if let Err(e) = refresh_path(&client, session, workspace_id, root, &event.path)
                    {
                        eprintln!("cw-agent: refresh failed {}: {}", event.path.display(), e);
                    }
                }
                FileEventKind::Removed => {
                    if let Err(e) = delete_path(&client, session, workspace_id, root, &event.path) {
                        eprintln!("cw-agent: delete failed {}: {}", event.path.display(), e);
                    }
                }
                FileEventKind::Renamed => {
                    if let Some(from_path) = event.from_path.as_ref() {
                        if let Err(e) = delete_path(&client, session, workspace_id, root, from_path)
                        {
                            eprintln!(
                                "cw-agent: rename delete failed {}: {}",
                                from_path.display(),
                                e
                            );
                        }
                    }
                    if let Some(to_path) = event.to_path.as_ref() {
                        if to_path.is_file() {
                            if let Err(e) =
                                refresh_path(&client, session, workspace_id, root, to_path)
                            {
                                eprintln!(
                                    "cw-agent: rename refresh failed {}: {}",
                                    to_path.display(),
                                    e
                                );
                            }
                        }
                    }
                }
            }
        }
        std::thread::sleep(Duration::from_millis(50));
    }

    // 信号到达后，先处理已经越过 debounce 窗口的事件，再停止底层 watcher。
    for event in watcher.flush() {
        if matches!(event.kind, FileEventKind::Created | FileEventKind::Modified)
            && event.path.is_file()
        {
            if let Err(e) = refresh_path(&client, session, workspace_id, root, &event.path) {
                eprintln!(
                    "cw-agent: final refresh failed {}: {}",
                    event.path.display(),
                    e
                );
            }
        }
    }
    watcher.stop();

    // 清理 PID 文件
    let _ = std::fs::remove_file(&pid_file);
    eprintln!("cw-agent: stopped");
}

#[cfg(unix)]
trait AgentRpcClient {
    fn call(
        &self,
        method: &str,
        params: serde_json::Value,
    ) -> Result<serde_json::Value, callwarden_core::daemon::client::ClientError>;

    fn call_with_fd(
        &self,
        method: &str,
        params: serde_json::Value,
        fd: std::os::fd::RawFd,
    ) -> Result<serde_json::Value, callwarden_core::daemon::client::ClientError>;
}

#[cfg(unix)]
impl AgentRpcClient for callwarden_core::daemon::client::unix::UnixDaemonRpcClient {
    fn call(
        &self,
        method: &str,
        params: serde_json::Value,
    ) -> Result<serde_json::Value, callwarden_core::daemon::client::ClientError> {
        callwarden_core::daemon::client::unix::UnixDaemonRpcClient::call(self, method, params)
    }

    fn call_with_fd(
        &self,
        method: &str,
        params: serde_json::Value,
        fd: std::os::fd::RawFd,
    ) -> Result<serde_json::Value, callwarden_core::daemon::client::ClientError> {
        callwarden_core::daemon::client::unix::UnixDaemonRpcClient::call_with_fd(
            self, method, params, fd,
        )
    }
}

#[cfg(unix)]
fn relative_path(root: &str, path: &std::path::Path) -> Result<String, String> {
    let root_path = std::path::Path::new(root);
    let rel = path
        .strip_prefix(root_path)
        .map_err(|_| format!("path 不在 workspace root 内: {}", path.display()))?;
    Ok(rel.to_string_lossy().replace('\\', "/"))
}

#[cfg(unix)]
fn reconnect_session<C: AgentRpcClient>(
    client: &C,
    session: &mut callwarden_core::daemon::client::AgentSession,
    workspace_id: &str,
) -> Result<(), String> {
    let (method, params) =
        callwarden_core::daemon::client::build_connect_params(workspace_id, &session.session_id);
    let result = client.call(&method, params).map_err(|e| e.to_string())?;
    let epoch = result
        .get("session_epoch")
        .and_then(|v| v.as_u64())
        .ok_or_else(|| "workspace.connect 响应缺少有效 session_epoch".to_string())?;
    if epoch < 1 {
        return Err(format!(
            "workspace.connect 返回无效 session_epoch={}",
            epoch
        ));
    }
    session.set_epoch(workspace_id, epoch);
    Ok(())
}

#[cfg(unix)]
fn refresh_path<C: AgentRpcClient>(
    client: &C,
    session: &mut callwarden_core::daemon::client::AgentSession,
    workspace_id: &str,
    root: &str,
    path: &std::path::Path,
) -> Result<serde_json::Value, String> {
    use callwarden_core::daemon::client::ClientError;

    let rel_path = relative_path(root, path)?;
    let canonical = callwarden_core::canonicalize::canonicalize_source(&path.to_string_lossy())
        .map_err(|e| format!("canonicalize 失败: {}", e))?;

    let send_once = |client: &C,
                     session: &mut callwarden_core::daemon::client::AgentSession|
     -> Result<serde_json::Value, ClientError> {
        let seq = session.next_seq(workspace_id);
        let epoch = session.get_epoch(workspace_id);
        let (method, mut params) = callwarden_core::daemon::client::build_refresh_params(
            workspace_id,
            &rel_path,
            &session.session_id,
            epoch,
            seq,
        );
        let map = params
            .as_object_mut()
            .expect("build_refresh_params 必须返回 object");
        map.insert(
            "canonical_len".to_string(),
            serde_json::json!(canonical.canonical_bytes.len()),
        );
        map.insert(
            "content_hash".to_string(),
            serde_json::Value::String(canonical.content_hash.clone()),
        );

        // JSON payload 默认上限 8MB；hex 会放大为 2 倍，3MB 阈值留出协议余量。
        if canonical.canonical_bytes.len() <= 3 * 1024 * 1024 {
            map.insert(
                "canonical_bytes_hex".to_string(),
                serde_json::Value::String(hex::encode(&canonical.canonical_bytes)),
            );
            client.call(&method, params)
        } else {
            send_large_refresh(client, &method, params, &canonical.canonical_bytes, seq)
        }
    };

    match send_once(client, session) {
        Ok(value) => Ok(value),
        Err(ClientError::Remote(remote))
            if remote.code == "session_not_active" || remote.code == "stale_session" =>
        {
            reconnect_session(client, session, workspace_id)?;
            send_once(client, session).map_err(|e| e.to_string())
        }
        Err(e) => Err(e.to_string()),
    }
}

#[cfg(unix)]
fn send_large_refresh<C: AgentRpcClient>(
    client: &C,
    method: &str,
    params: serde_json::Value,
    canonical_bytes: &[u8],
    seq: u64,
) -> Result<serde_json::Value, callwarden_core::daemon::client::ClientError> {
    use std::io::{Seek, SeekFrom, Write};
    use std::os::fd::AsRawFd;

    let tmp_path = std::env::temp_dir().join(format!(
        "callwarden-agent-{}-{}-{}.canonical",
        std::process::id(),
        seq,
        canonical_bytes.len()
    ));
    let mut file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(&tmp_path)
        .map_err(
            |e| callwarden_core::daemon::client::ClientError::ConnectFailed {
                path: tmp_path.to_string_lossy().to_string(),
                source: e,
            },
        )?;
    if let Err(e) = file
        .write_all(canonical_bytes)
        .and_then(|_| file.sync_all())
        .and_then(|_| file.seek(SeekFrom::Start(0)).map(|_| ()))
    {
        let _ = std::fs::remove_file(&tmp_path);
        return Err(
            callwarden_core::daemon::client::ClientError::ConnectFailed {
                path: tmp_path.to_string_lossy().to_string(),
                source: e,
            },
        );
    }
    let result = client.call_with_fd(method, params, file.as_raw_fd());
    drop(file);
    let _ = std::fs::remove_file(&tmp_path);
    result
}

#[cfg(unix)]
fn delete_path<C: AgentRpcClient>(
    client: &C,
    session: &mut callwarden_core::daemon::client::AgentSession,
    workspace_id: &str,
    root: &str,
    path: &std::path::Path,
) -> Result<serde_json::Value, String> {
    use callwarden_core::daemon::client::ClientError;

    let rel_path = relative_path(root, path)?;
    let send_once = |client: &C,
                     session: &mut callwarden_core::daemon::client::AgentSession|
     -> Result<serde_json::Value, ClientError> {
        let monotonic_seq = session.next_seq(workspace_id);
        client.call(
            "workspace.file.delete",
            serde_json::json!({
                "workspace_instance_id": workspace_id,
                "rel_path": rel_path,
                "agent_session_id": session.session_id,
                "session_epoch": session.get_epoch(workspace_id),
                "monotonic_seq": monotonic_seq,
            }),
        )
    };
    match send_once(client, session) {
        Ok(value) => Ok(value),
        Err(ClientError::Remote(remote))
            if remote.code == "session_not_active" || remote.code == "stale_session" =>
        {
            reconnect_session(client, session, workspace_id)?;
            send_once(client, session).map_err(|e| e.to_string())
        }
        Err(e) => Err(e.to_string()),
    }
}

#[cfg(unix)]
fn stop_unix(pid: u32) {
    let ret = unsafe { libc::kill(pid as i32, libc::SIGTERM) };
    if ret == 0 {
        println!("Sent SIGTERM to PID {}", pid);
        let pid_file = agent_pid_file();
        let _ = std::fs::remove_file(&pid_file);
    } else {
        eprintln!(
            "Failed to send signal to PID {}: {}",
            pid,
            std::io::Error::last_os_error()
        );
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[derive(Default)]
    struct MockRpcClient {
        calls: std::sync::Mutex<Vec<(String, serde_json::Value)>>,
        fd_payloads: std::sync::Mutex<Vec<Vec<u8>>>,
    }

    #[cfg(unix)]
    impl AgentRpcClient for MockRpcClient {
        fn call(
            &self,
            method: &str,
            params: serde_json::Value,
        ) -> Result<serde_json::Value, callwarden_core::daemon::client::ClientError> {
            self.calls
                .lock()
                .expect("calls mutex")
                .push((method.to_string(), params));
            Ok(serde_json::json!({"status": "committed"}))
        }

        fn call_with_fd(
            &self,
            method: &str,
            params: serde_json::Value,
            fd: std::os::fd::RawFd,
        ) -> Result<serde_json::Value, callwarden_core::daemon::client::ClientError> {
            use std::io::Read;
            use std::os::fd::FromRawFd;

            self.calls
                .lock()
                .expect("calls mutex")
                .push((method.to_string(), params));
            let duplicated = unsafe { libc::dup(fd) };
            assert!(duplicated >= 0);
            let mut file = unsafe { std::fs::File::from_raw_fd(duplicated) };
            let mut payload = Vec::new();
            file.read_to_end(&mut payload).expect("read duplicated fd");
            self.fd_payloads
                .lock()
                .expect("fd payload mutex")
                .push(payload);
            Ok(serde_json::json!({"status": "committed"}))
        }
    }

    #[cfg(unix)]
    fn active_session() -> callwarden_core::daemon::client::AgentSession {
        let mut session =
            callwarden_core::daemon::client::AgentSession::new("agent-test".to_string());
        session.register_workspace("ws-test");
        session.set_epoch("ws-test", 3);
        session
    }

    #[test]
    fn test_cli_parse_start() {
        let cli = Cli::parse_from([
            "cw-agent",
            "start",
            "/tmp/project",
            "--workspace-id",
            "ws-abc123",
        ]);
        match cli.command {
            Some(Commands::Start {
                root,
                workspace_id,
                session_id,
                debounce_ms,
            }) => {
                assert_eq!(root, "/tmp/project");
                assert_eq!(workspace_id, "ws-abc123");
                assert!(session_id.is_none());
                assert_eq!(debounce_ms, 1000);
            }
            _ => panic!("期望 Start 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_start_with_all_options() {
        let cli = Cli::parse_from([
            "cw-agent",
            "start",
            "/tmp/p",
            "--workspace-id",
            "ws-1",
            "--session-id",
            "agent-custom",
            "--debounce-ms",
            "500",
        ]);
        match cli.command {
            Some(Commands::Start {
                root,
                workspace_id,
                session_id,
                debounce_ms,
            }) => {
                assert_eq!(root, "/tmp/p");
                assert_eq!(workspace_id, "ws-1");
                assert_eq!(session_id.as_deref(), Some("agent-custom"));
                assert_eq!(debounce_ms, 500);
            }
            _ => panic!("期望 Start 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_stop() {
        let cli = Cli::parse_from(["cw-agent", "stop"]);
        assert!(matches!(cli.command, Some(Commands::Stop)));
    }

    #[test]
    fn test_cli_parse_status() {
        let cli = Cli::parse_from(["cw-agent", "status"]);
        assert!(matches!(cli.command, Some(Commands::Status)));
    }

    #[test]
    fn test_cli_parse_no_subcommand() {
        let cli = Cli::parse_from(["cw-agent"]);
        assert!(cli.command.is_none());
    }

    #[test]
    fn test_cli_default_socket() {
        let cli = Cli::parse_from(["cw-agent", "status"]);
        assert_eq!(cli.socket, "/tmp/callwarden_daemon.sock");
    }

    #[test]
    fn test_cli_default_timeout() {
        let cli = Cli::parse_from(["cw-agent", "status"]);
        assert_eq!(cli.timeout, 30);
    }

    #[test]
    fn test_cli_global_args_before_subcommand() {
        let cli = Cli::parse_from([
            "cw-agent",
            "--socket",
            "/tmp/custom.sock",
            "--timeout",
            "60",
            "status",
        ]);
        assert_eq!(cli.socket, "/tmp/custom.sock");
        assert_eq!(cli.timeout, 60);
        assert!(matches!(cli.command, Some(Commands::Status)));
    }

    #[test]
    fn test_agent_pid_file_path() {
        let pid_file = agent_pid_file();
        // 应包含 .callwarden 和 agent.pid
        assert!(pid_file.to_string_lossy().contains(".callwarden"));
        assert!(pid_file.to_string_lossy().ends_with("agent.pid"));
    }

    #[cfg(unix)]
    #[test]
    fn test_refresh_path_sends_canonical_bytes_and_generation() {
        let dir = tempfile::tempdir().expect("tempdir");
        let source = dir.path().join("sample.rs");
        std::fs::write(&source, b"fn sample() {}\r\n").expect("write source");
        let client = MockRpcClient::default();
        let mut session = active_session();

        refresh_path(
            &client,
            &mut session,
            "ws-test",
            &dir.path().to_string_lossy(),
            &source,
        )
        .expect("refresh");

        let calls = client.calls.lock().expect("calls mutex");
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "workspace.file.refresh");
        assert_eq!(calls[0].1["rel_path"], "sample.rs");
        assert_eq!(calls[0].1["session_epoch"], 3);
        assert_eq!(calls[0].1["monotonic_seq"], 1);
        let canonical_hex = calls[0].1["canonical_bytes_hex"]
            .as_str()
            .expect("canonical bytes hex");
        let canonical = hex::decode(canonical_hex).expect("decode canonical bytes");
        assert_eq!(canonical, b"fn sample() {}\n");
    }

    #[cfg(unix)]
    #[test]
    fn test_large_refresh_fd_starts_at_byte_zero() {
        let dir = tempfile::tempdir().expect("tempdir");
        let source = dir.path().join("large.rs");
        let content = vec![b'x'; 3 * 1024 * 1024 + 1];
        std::fs::write(&source, &content).expect("write source");
        let client = MockRpcClient::default();
        let mut session = active_session();

        refresh_path(
            &client,
            &mut session,
            "ws-test",
            &dir.path().to_string_lossy(),
            &source,
        )
        .expect("refresh");

        let payloads = client.fd_payloads.lock().expect("fd payload mutex");
        assert_eq!(payloads.len(), 1);
        assert_eq!(payloads[0], content);
    }

    #[cfg(unix)]
    #[test]
    fn test_delete_path_sends_workspace_scoped_request() {
        let dir = tempfile::tempdir().expect("tempdir");
        let source = dir.path().join("removed.rs");
        let client = MockRpcClient::default();
        let mut session = active_session();

        delete_path(
            &client,
            &mut session,
            "ws-test",
            &dir.path().to_string_lossy(),
            &source,
        )
        .expect("delete");

        let calls = client.calls.lock().expect("calls mutex");
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "workspace.file.delete");
        assert_eq!(calls[0].1["workspace_instance_id"], "ws-test");
        assert_eq!(calls[0].1["rel_path"], "removed.rs");
        assert_eq!(calls[0].1["session_epoch"], 3);
        assert_eq!(calls[0].1["monotonic_seq"], 1);
    }
}
