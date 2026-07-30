//! R7: cw_daemon binary 配置加载。
//!
//! 对应 Python `server/daemon_config.py:DaemonConfig`（L103-319）的 Rust 最小子集，
//! 仅覆盖启动所需字段。完整 Python 版还包含 tcp/resources/security/jobs 等子配置，
//! 留给后续任务按需补齐。
//!
//! ## 字段优先级（高 → 低）
//! 1. CLI 参数（--socket / --workers / --registry）
//! 2. 环境变量（CW_DAEMON_SOCKET / CW_DAEMON_REGISTRY_DB / CW_DAEMON_DATA_ROOT）
//! 3. 配置文件（--config 指定的 JSON）
//! 4. 内置默认值

use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// 默认 UDS socket 路径（systemd RuntimeDirectory=callwarden → /run/callwarden/）
///
/// 注意：与 release/linux/deb/daemon.{preinst,postinst,prerm} 一致，
/// 不使用旧版 /var/run/callwarden.sock（server.rs:ServerConfig 默认值需同步修正）
pub const DEFAULT_SOCKET_PATH: &str = "/run/callwarden/callwarden.sock";

/// 默认数据根目录（systemd StateDirectory=callwarden → /var/lib/callwarden/）
pub const DEFAULT_DATA_ROOT: &str = "/var/lib/callwarden";

/// 默认 registry DB 路径（<data_root>/registry.db）
pub fn default_registry_db_path() -> PathBuf {
    PathBuf::from(format!("{}/registry.db", DEFAULT_DATA_ROOT))
}

/// 默认 snapshot cache 容量（最多同时缓存的 workspace 数）
pub const DEFAULT_SNAPSHOT_CACHE_CAPACITY: usize = 32;

/// cw_daemon 配置（最小集，仅覆盖启动所需字段）
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DaemonConfig {
    /// UDS socket 路径
    pub socket_path: PathBuf,
    /// registry DB 路径（daemon_workspaces + daemon_state + container_mount_mappings 表）
    pub registry_db_path: PathBuf,
    /// 数据根目录（CAS / staging_log / snapshot 等存储根）
    pub data_root: PathBuf,
    /// 工作线程数
    pub max_workers: usize,
    /// 单请求超时
    pub request_timeout_secs: u64,
    /// socket 文件权限（八进制，如 0o660）
    pub socket_mode: u32,
    /// snapshot cache 容量
    pub snapshot_cache_capacity: usize,
    /// G11: CodeGraph DB 路径模板（用于 Replicator 发布 snapshot）
    ///
    /// 占位符 `{workspace_instance_id}` 在运行时按当前 workspace 替换。
    /// P0-2 修复（2026-07-22 完整复审）：默认值改为非空，启用 save-to-query 数据链。
    /// daemon 首次打开 CodeGraph DB 时自动创建目录。
    pub codegraph_db_path_template: String,
    /// P0-3 修复：socket 文件组名（用于多用户 UDS 访问）
    ///
    /// 非空时，socket bind 后 chown 到该组。空表示不 chown（使用进程 GID）。
    /// 典型值：`callwarden-clients`（Linux 多用户安装）
    #[serde(default)]
    pub socket_group: String,
}

impl Default for DaemonConfig {
    fn default() -> Self {
        Self {
            socket_path: PathBuf::from(DEFAULT_SOCKET_PATH),
            registry_db_path: default_registry_db_path(),
            data_root: PathBuf::from(DEFAULT_DATA_ROOT),
            max_workers: 16,
            request_timeout_secs: 30,
            socket_mode: 0o660,
            snapshot_cache_capacity: DEFAULT_SNAPSHOT_CACHE_CAPACITY,
            // P0-2 修复：默认启用 CodeGraph 发布（save-to-query 数据链闭合）
            codegraph_db_path_template: format!(
                "{}/workspaces/{{workspace_instance_id}}/codegraph.db",
                DEFAULT_DATA_ROOT
            ),
            // P0-3 修复：默认 socket 组为 callwarden-clients（多用户 UDS 访问）
            socket_group: String::from("callwarden-clients"),
        }
    }
}

/// 配置加载错误
#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("配置文件读取失败: {path}: {source}")]
    FileRead {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("配置文件解析失败: {path}: {source}")]
    Parse {
        path: PathBuf,
        #[source]
        source: serde_json::Error,
    },
    #[error("环境变量 {name} 解析失败: {reason}")]
    EnvVar { name: String, reason: String },
}

impl DaemonConfig {
    /// 从 JSON 配置文件加载（对应 Python DaemonConfig.load_from_file）
    ///
    /// 文件格式示例：
    /// ```json
    /// {
    ///   "socket_path": "/run/callwarden/callwarden.sock",
    ///   "registry_db_path": "/var/lib/callwarden/registry.db",
    ///   "data_root": "/var/lib/callwarden",
    ///   "max_workers": 16,
    ///   "request_timeout_secs": 30,
    ///   "socket_mode": 432,
    ///   "snapshot_cache_capacity": 32
    /// }
    /// ```
    pub fn load_from_file(path: &Path) -> Result<Self, ConfigError> {
        let content = std::fs::read_to_string(path).map_err(|e| ConfigError::FileRead {
            path: path.to_path_buf(),
            source: e,
        })?;
        let cfg: Self = serde_json::from_str(&content).map_err(|e| ConfigError::Parse {
            path: path.to_path_buf(),
            source: e,
        })?;
        Ok(cfg)
    }

    /// 应用环境变量覆盖（仅在字段仍为默认值时覆盖，与 CLI 参数互斥）
    ///
    /// - `CW_DAEMON_SOCKET` → socket_path
    /// - `CW_DAEMON_REGISTRY_DB` → registry_db_path
    /// - `CW_DAEMON_DATA_ROOT` → data_root
    /// - `CW_DAEMON_WORKERS` → max_workers
    /// - `CW_DAEMON_CODEGRAPH_DB_TEMPLATE` → codegraph_db_path_template（G11）
    pub fn apply_env_overrides(&mut self) -> Result<(), ConfigError> {
        if let Ok(v) = std::env::var("CW_DAEMON_SOCKET") {
            if !v.is_empty() {
                self.socket_path = PathBuf::from(v);
            }
        }
        if let Ok(v) = std::env::var("CW_DAEMON_REGISTRY_DB") {
            if !v.is_empty() {
                self.registry_db_path = PathBuf::from(v);
            }
        }
        if let Ok(v) = std::env::var("CW_DAEMON_DATA_ROOT") {
            if !v.is_empty() {
                self.data_root = PathBuf::from(v);
                // 如果 registry_db_path 仍是默认值，跟随 data_root 重新计算
                if self.registry_db_path == default_registry_db_path() {
                    self.registry_db_path = self.data_root.join("registry.db");
                }
            }
        }
        if let Ok(v) = std::env::var("CW_DAEMON_WORKERS") {
            if !v.is_empty() {
                self.max_workers = v.parse::<usize>().map_err(|e| ConfigError::EnvVar {
                    name: "CW_DAEMON_WORKERS".to_string(),
                    reason: e.to_string(),
                })?;
            }
        }
        // G11: CodeGraph DB 路径模板（用于 Replicator 发布 snapshot）
        if let Ok(v) = std::env::var("CW_DAEMON_CODEGRAPH_DB_TEMPLATE") {
            if !v.is_empty() {
                self.codegraph_db_path_template = v;
            }
        }
        Ok(())
    }

    /// G11: 解析 workspace 的 CodeGraph DB 路径
    ///
    /// 模板中的 `{workspace_instance_id}` 占位符替换为实际 workspace ID。
    /// 模板为空时返回空字符串（调用方应判断空值跳过 snapshot 发布）。
    pub fn resolve_codegraph_db_path(&self, workspace_instance_id: &str) -> String {
        if self.codegraph_db_path_template.is_empty() {
            return String::new();
        }
        self.codegraph_db_path_template
            .replace("{workspace_instance_id}", workspace_instance_id)
    }

    /// 确保 data_root 和 registry_db_path 的父目录存在（不报错，启动时再检查）
    pub fn ensure_directories(&self) -> std::io::Result<()> {
        std::fs::create_dir_all(&self.data_root)?;
        if let Some(parent) = self.registry_db_path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        if let Some(parent) = self.socket_path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        Ok(())
    }

    /// request_timeout 转换为 Duration（便利方法）
    pub fn request_timeout(&self) -> Duration {
        Duration::from_secs(self.request_timeout_secs)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsString;
    use std::sync::{Mutex, MutexGuard};

    const DAEMON_ENV_KEYS: [&str; 5] = [
        "CW_DAEMON_SOCKET",
        "CW_DAEMON_REGISTRY_DB",
        "CW_DAEMON_DATA_ROOT",
        "CW_DAEMON_WORKERS",
        "CW_DAEMON_CODEGRAPH_DB_TEMPLATE",
    ];
    static DAEMON_ENV_LOCK: Mutex<()> = Mutex::new(());

    struct IsolatedDaemonEnv {
        saved: Vec<(&'static str, Option<OsString>)>,
        _lock: MutexGuard<'static, ()>,
    }

    impl IsolatedDaemonEnv {
        fn new() -> Self {
            let lock = DAEMON_ENV_LOCK
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            let saved = DAEMON_ENV_KEYS
                .iter()
                .map(|key| (*key, std::env::var_os(key)))
                .collect();
            for key in DAEMON_ENV_KEYS {
                std::env::remove_var(key);
            }
            Self { saved, _lock: lock }
        }

        fn set(&self, key: &'static str, value: &str) {
            std::env::set_var(key, value);
        }
    }

    impl Drop for IsolatedDaemonEnv {
        fn drop(&mut self) {
            for (key, value) in self.saved.drain(..) {
                match value {
                    Some(value) => std::env::set_var(key, value),
                    None => std::env::remove_var(key),
                }
            }
        }
    }

    #[test]
    fn test_default_config_paths() {
        let cfg = DaemonConfig::default();
        assert_eq!(cfg.socket_path, PathBuf::from(DEFAULT_SOCKET_PATH));
        assert_eq!(cfg.data_root, PathBuf::from(DEFAULT_DATA_ROOT));
        assert_eq!(
            cfg.registry_db_path,
            PathBuf::from("/var/lib/callwarden/registry.db")
        );
        assert_eq!(cfg.max_workers, 16);
        assert_eq!(cfg.socket_mode, 0o660);
    }

    #[test]
    fn test_load_from_file_roundtrip() {
        let tmp = tempfile::tempdir().unwrap();
        let cfg_path = tmp.path().join("daemon.json");
        let original = DaemonConfig {
            socket_path: PathBuf::from("/tmp/test.sock"),
            registry_db_path: PathBuf::from("/tmp/registry.db"),
            data_root: PathBuf::from("/tmp/data"),
            max_workers: 8,
            request_timeout_secs: 15,
            socket_mode: 0o600,
            snapshot_cache_capacity: 4,
            codegraph_db_path_template: String::from(
                "/var/lib/callwarden/{workspace_instance_id}/codegraph.db",
            ),
            socket_group: String::from("callwarden-clients"),
        };
        let json = serde_json::to_string_pretty(&original).unwrap();
        std::fs::write(&cfg_path, json).unwrap();
        let loaded = DaemonConfig::load_from_file(&cfg_path).unwrap();
        assert_eq!(loaded.socket_path, original.socket_path);
        assert_eq!(loaded.max_workers, 8);
        assert_eq!(loaded.socket_mode, 0o600);
        assert_eq!(
            loaded.codegraph_db_path_template,
            original.codegraph_db_path_template
        );
    }

    #[test]
    fn test_resolve_codegraph_db_path_empty_template() {
        // P0-2 修复后：默认模板不再为空，需显式构造空模板验证 resolve 行为
        let mut cfg = DaemonConfig::default();
        cfg.codegraph_db_path_template = String::new();
        assert_eq!(cfg.resolve_codegraph_db_path("ws-123"), "");
    }

    #[test]
    fn test_resolve_codegraph_db_path_placeholder_substitution() {
        let mut cfg = DaemonConfig::default();
        cfg.codegraph_db_path_template =
            "/var/lib/callwarden/{workspace_instance_id}/codegraph.db".to_string();
        assert_eq!(
            cfg.resolve_codegraph_db_path("ws-abc-123"),
            "/var/lib/callwarden/ws-abc-123/codegraph.db"
        );
    }

    #[test]
    fn test_apply_env_overrides_codegraph_db_template() {
        let env = IsolatedDaemonEnv::new();
        env.set(
            "CW_DAEMON_CODEGRAPH_DB_TEMPLATE",
            "/tmp/ws_{workspace_instance_id}/cg.db",
        );
        let mut cfg = DaemonConfig::default();
        cfg.apply_env_overrides().unwrap();
        assert_eq!(
            cfg.codegraph_db_path_template,
            "/tmp/ws_{workspace_instance_id}/cg.db"
        );
        assert_eq!(cfg.resolve_codegraph_db_path("xyz"), "/tmp/ws_xyz/cg.db");
    }

    #[test]
    fn test_apply_env_overrides_socket() {
        let env = IsolatedDaemonEnv::new();
        env.set("CW_DAEMON_SOCKET", "/tmp/env_override.sock");
        let mut cfg = DaemonConfig::default();
        cfg.apply_env_overrides().unwrap();
        assert_eq!(cfg.socket_path, PathBuf::from("/tmp/env_override.sock"));
    }

    #[test]
    fn test_apply_env_overrides_workers_invalid() {
        let env = IsolatedDaemonEnv::new();
        env.set("CW_DAEMON_WORKERS", "not-a-number");
        let mut cfg = DaemonConfig::default();
        let result = cfg.apply_env_overrides();
        assert!(result.is_err());
    }

    #[test]
    fn test_apply_env_overrides_data_root_updates_registry() {
        let env = IsolatedDaemonEnv::new();
        env.set("CW_DAEMON_DATA_ROOT", "/tmp/custom_data");
        let mut cfg = DaemonConfig::default();
        cfg.apply_env_overrides().unwrap();
        assert_eq!(cfg.data_root, PathBuf::from("/tmp/custom_data"));
        // registry_db_path 应跟随 data_root
        assert_eq!(
            cfg.registry_db_path,
            PathBuf::from("/tmp/custom_data/registry.db")
        );
    }

    #[test]
    fn test_ensure_directories_creates_data_root() {
        let tmp = tempfile::tempdir().unwrap();
        let cfg = DaemonConfig {
            socket_path: tmp.path().join("subdir/socket.sock"),
            registry_db_path: tmp.path().join("regdir/registry.db"),
            data_root: tmp.path().join("dataroot"),
            ..DaemonConfig::default()
        };
        cfg.ensure_directories().unwrap();
        assert!(tmp.path().join("dataroot").exists());
        assert!(tmp.path().join("regdir").exists());
        assert!(tmp.path().join("subdir").exists());
    }
}
