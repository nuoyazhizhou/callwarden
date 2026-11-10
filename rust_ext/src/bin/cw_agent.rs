//! cw-agent binary（Phase 5-2 Slice 6）
//!
//! Call Warden agent：文件监控 + session 管理（Rust legacy RPC 客户端已退役）。
//!
//! ADR-002：唯一 daemon RPC 传输是 HTTPS+mTLS。Rust `cw-agent start` 为
//! fail-closed 占位，需使用 Python `cw-agent` 执行实际 agent 工作。
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
///
/// ADR-002（单轨 HTTPS+mTLS）退役了 Rust legacy RPC 客户端（UDS / 命名管道），
/// 且本仓库不提供 Rust HTTPS 客户端。因此 `cw-agent start` 为 fail-closed：
/// 明确报错并以非 0 退出码结束，不回退到任何 socket / pipe 通道。
fn run_start(
    socket: &str,
    timeout_secs: u64,
    root: &str,
    workspace_id: &str,
    session_id: Option<&str>,
    debounce_ms: u64,
) {
    let _ = (socket, timeout_secs, root, workspace_id, session_id, debounce_ms);
    eprintln!(
        "cw-agent: Rust legacy RPC 客户端已按 ADR-002 退役；请使用 Python `cw-agent`（经 HTTPS+mTLS 访问 daemon）"
    );
    std::process::exit(1);
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
                    stop_windows(pid)
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
                    let alive = is_process_alive_windows(pid);
                    println!("Process alive: {}", alive);
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

/// Windows 终止 agent 进程（taskkill /PID /T /F，无 POSIX 信号）。
#[cfg(not(unix))]
fn stop_windows(pid: u32) {
    let status = std::process::Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .status();
    match status {
        Ok(s) if s.success() => {
            println!("Sent terminate to PID {}", pid);
            let pid_file = agent_pid_file();
            let _ = std::fs::remove_file(&pid_file);
        }
        Ok(s) => {
            eprintln!("taskkill failed for PID {}: {}", pid, s);
            std::process::exit(1);
        }
        Err(e) => {
            eprintln!("failed to run taskkill: {}", e);
            std::process::exit(1);
        }
    }
}

/// Windows 进程存活检查（OpenProcess + GetExitCodeProcess == STILL_ACTIVE）。
#[cfg(not(unix))]
fn is_process_alive_windows(pid: u32) -> bool {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Threading::{
        GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };

    let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if handle.is_null() {
        return false;
    }
    let mut code: u32 = 0;
    let ok = unsafe { GetExitCodeProcess(handle, &mut code) };
    unsafe { CloseHandle(handle) };
    ok != 0 && code == 259 // STILL_ACTIVE
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
