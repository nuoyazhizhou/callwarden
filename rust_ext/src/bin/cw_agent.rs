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
        run_start_unix(socket, timeout_secs, root, workspace_id, &mut session, debounce_ms)
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
    let (method, params) = callwarden_core::daemon::client::build_connect_params(
        workspace_id,
        &session.session_id,
    );
    match client.call(&method, params) {
        Ok(result) => {
            let epoch = result.get("session_epoch")
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
    eprintln!("cw-agent: starting watcher on {} (debounce={}ms)", root, debounce_ms);
    eprintln!("cw-agent: watcher loop not yet implemented (stub)");
    eprintln!("cw-agent: would use DebouncedFileWatcher from callwarden_core::watcher");

    // TODO: 实现完整的 watcher 循环（需要 notify + crossbeam_channel）
    // 当前仅作为骨架，等待后续完善

    // 清理 PID 文件
    let _ = std::fs::remove_file(&pid_file);
    eprintln!("cw-agent: stopped");
}

#[cfg(unix)]
fn stop_unix(pid: u32) {
    let ret = unsafe { libc::kill(pid as i32, libc::SIGTERM) };
    if ret == 0 {
        println!("Sent SIGTERM to PID {}", pid);
        let pid_file = agent_pid_file();
        let _ = std::fs::remove_file(&pid_file);
    } else {
        eprintln!("Failed to send signal to PID {}: {}", pid, std::io::Error::last_os_error());
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
}
