//! Enterprise daemon 模块——UDS server + SO_PEERCRED + workspace registry。
//! Linux 特性用 #[cfg(unix)] 条件编译，Windows 上编译为空模块。

/// JSON-RPC 协议层（跨平台，纯逻辑）
pub mod protocol;

/// JSON-RPC dispatch 路由表 + 基础方法（跨平台，纯逻辑）
pub mod dispatch;

#[cfg(unix)]
pub mod server;
#[cfg(unix)]
pub mod peercred;

/// daemon schema 版本号（与 db/schema.py:SCHEMA_VERSION 保持同步）
/// 用于 schema.version RPC 方法返回，以及 daemon 启动时 schema 兼容性检查。
/// 更新 schema 时记得同步修改。
pub const SCHEMA_VERSION: u32 = 37;

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
