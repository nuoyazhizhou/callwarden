//! cw_daemon——Enterprise daemon binary 入口。
//! 仅 Linux 可用（UDS + SO_PEERCRED）。
//! Windows 上编译为空 main（占位）。

fn main() {
    #[cfg(unix)]
    {
        let config = callwarden_core::daemon::DaemonConfig::default();
        eprintln!("[cw_daemon] starting with config: socket={}, max_conn={}",
            config.socket_path, config.max_connections);
        match callwarden_core::daemon::server::start_server(&config) {
            Ok(()) => eprintln!("[cw_daemon] server exited normally"),
            Err(e) => eprintln!("[cw_daemon] server error: {}", e),
        }
    }
    #[cfg(not(unix))]
    {
        eprintln!("[cw_daemon] UDS server is only available on Linux/Unix");
        std::process::exit(1);
    }
}
