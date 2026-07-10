//! Enterprise daemon 模块——UDS server + SO_PEERCRED + workspace registry。
//! Linux 特性用 #[cfg(unix)] 条件编译，Windows 上编译为空模块。

#[cfg(unix)]
pub mod server;
#[cfg(unix)]
pub mod peercred;

/// daemon 配置
pub struct DaemonConfig {
    pub socket_path: String,
    pub max_connections: usize,
    pub workspace_root: String,
}

impl Default for DaemonConfig {
    fn default() -> Self {
        Self {
            socket_path: "/var/run/callwarden.sock".to_string(),
            max_connections: 64,
            workspace_root: "/var/lib/callwarden".to_string(),
        }
    }
}
