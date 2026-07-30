//! 分层配置加载器（Phase 5-1 A.1）
//!
//! 对齐 Python `release/config_loader.py`：
//! - `PlatformPaths`: 平台特定配置/数据目录
//! - `ConfigValue` / `Config`: 带来源追踪的配置
//! - `load_config()`: TOML + env + CLI 三层优先级
//! - `config_explain()`: 输出来源，隐藏 secret
//! - `check_role_supported()` / `fail_closed_unsupported()`: 平台×角色矩阵
//!
//! 契约：docs/design/phase5-1-cli-config-contract.md §3.1

use std::collections::HashMap;
use std::path::PathBuf;
use std::process::exit;

// ============================================================
// 平台路径规范
// ============================================================

/// 平台特定的配置和数据目录。
///
/// 对齐 Python `release/config_loader.py:PlatformPaths` (L29-66)
#[derive(Debug, Clone)]
pub struct PlatformPaths {
    pub system_config: PathBuf,
    pub user_config: PathBuf,
    pub system_data: PathBuf,
    pub user_data: PathBuf,
    /// Linux only: /run/callwarden
    pub runtime: Option<PathBuf>,
}

impl PlatformPaths {
    /// 根据当前平台返回标准路径。
    ///
    /// 对齐 Python `PlatformPaths::detect()` (L38-66)
    pub fn detect() -> Self {
        Self::detect_for_platform(std::env::consts::OS)
    }

    /// 按指定平台名检测路径（供测试和 PyO3 暴露使用）。
    ///
    /// `platform` 参数对齐 Python `sys.platform`：
    /// - `"linux"` → Linux 路径
    /// - `"windows"` → Windows 路径（Python `win32`）
    /// - `"macos"` → macOS 路径（Python `darwin`）
    pub fn detect_for_platform(platform: &str) -> Self {
        let home = dirs_home();
        match platform {
            "windows" | "win32" => {
                let program_data = std::env::var("ProgramData")
                    .unwrap_or_else(|_| r"C:\ProgramData".to_string());
                let local_app_data = std::env::var("LOCALAPPDATA")
                    .unwrap_or_else(|_| {
                        format!("{}\\AppData\\Local", home.display())
                    });
                let program_data = PathBuf::from(program_data);
                let local_app_data = PathBuf::from(local_app_data);
                Self {
                    system_config: program_data.join("CallWarden").join("config.toml"),
                    user_config: local_app_data.join("CallWarden").join("config.toml"),
                    system_data: program_data.join("CallWarden").join("data"),
                    user_data: local_app_data.join("CallWarden").join("data"),
                    runtime: None,
                }
            }
            "macos" | "darwin" => {
                let lib_support = PathBuf::from("/Library/Application Support/CallWarden");
                let user_support = home
                    .join("Library")
                    .join("Application Support")
                    .join("CallWarden");
                Self {
                    system_config: lib_support.join("config.toml"),
                    user_config: user_support.join("config.toml"),
                    system_data: lib_support.join("data"),
                    user_data: user_support.join("data"),
                    runtime: None,
                }
            }
            _ => {
                // Linux / Unix-like
                let xdg_config = std::env::var("XDG_CONFIG_HOME")
                    .map(PathBuf::from)
                    .unwrap_or_else(|_| home.join(".config"));
                let xdg_state = std::env::var("XDG_STATE_HOME")
                    .map(PathBuf::from)
                    .unwrap_or_else(|_| home.join(".local").join("state"));
                Self {
                    system_config: PathBuf::from("/etc/callwarden/config.toml"),
                    user_config: xdg_config.join("callwarden").join("config.toml"),
                    system_data: PathBuf::from("/var/lib/callwarden"),
                    user_data: xdg_state.join("callwarden"),
                    runtime: Some(PathBuf::from("/run/callwarden")),
                }
            }
        }
    }
}

/// 获取 home 目录（跨平台）。
fn dirs_home() -> PathBuf {
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            // Windows: %USERPROFILE%
            std::env::var("USERPROFILE")
                .map(PathBuf::from)
                .unwrap_or_else(|_| PathBuf::from("."))
        })
}

// ============================================================
// 分层配置加载器
// ============================================================

/// 带来源追踪的配置值。
///
/// 对齐 Python `release/config_loader.py:ConfigValue` (L73-77)
#[derive(Debug, Clone)]
pub struct ConfigValue {
    pub value: String,
    pub source: String, // "cli" / "env" / "user_config" / "system_config" / "default"
}

/// 分层配置。
///
/// 对齐 Python `release/config_loader.py:Config` (L80-106)
#[derive(Debug, Clone, Default)]
pub struct Config {
    pub values: HashMap<String, ConfigValue>,
}

/// Secret 字段名片段（explain 时隐藏值）。
///
/// 对齐 Python `Config::SECRET_KEYS` (L86)
const SECRET_KEYS: &[&str] = &["token", "secret", "password", "api_key", "private_key"];

impl Config {
    /// 获取配置值。
    pub fn get(&self, key: &str, default: &str) -> String {
        self.values
            .get(key)
            .map(|cv| cv.value.clone())
            .unwrap_or_else(|| default.to_string())
    }

    /// 输出每个有效值的来源（隐藏 secret）。
    ///
    /// 对齐 Python `Config::explain()` (L95-106)
    /// 按 key 字母排序，secret 字段值显示为 `***`。
    pub fn explain(&self) -> Vec<ConfigEntry> {
        let mut result: Vec<ConfigEntry> = self
            .values
            .iter()
            .map(|(key, cv)| {
                let is_secret = SECRET_KEYS
                    .iter()
                    .any(|s| key.to_lowercase().contains(s));
                let display_value = if is_secret {
                    "***".to_string()
                } else {
                    cv.value.clone()
                };
                ConfigEntry {
                    key: key.clone(),
                    value: display_value,
                    source: cv.source.clone(),
                }
            })
            .collect();
        result.sort_by(|a, b| a.key.cmp(&b.key));
        result
    }
}

/// config_explain 返回的单条记录。
#[derive(Debug, Clone)]
pub struct ConfigEntry {
    pub key: String,
    pub value: String,
    pub source: String,
}

/// 按优先级加载配置。
///
/// 对齐 Python `release/config_loader.py:load_config()` (L109-148)
///
/// 优先级：CLI 参数 > 环境变量 > 用户配置 > 系统配置 > 默认值
pub fn load_config(
    cli_overrides: Option<&HashMap<String, String>>,
    env_prefix: &str,
) -> Config {
    let mut config = Config::default();
    let paths = PlatformPaths::detect();
    let is_linux = std::env::consts::OS == "linux";

    // 1. 默认值
    let defaults: HashMap<String, String> = if is_linux {
        let mut m = HashMap::new();
        m.insert("daemon_socket".to_string(), "/run/callwarden/callwarden.sock".to_string());
        m
    } else {
        HashMap::new()
    };
    let mut defaults = defaults;
    defaults.insert("log_level".to_string(), "info".to_string());
    defaults.insert("max_workers".to_string(), "16".to_string());
    defaults.insert("watcher_debounce_ms".to_string(), "250".to_string());
    defaults.insert("cas_grace_days".to_string(), "7".to_string());
    for (key, value) in &defaults {
        config.values.insert(
            key.clone(),
            ConfigValue {
                value: value.clone(),
                source: "default".to_string(),
            },
        );
    }

    // 2. 系统配置
    load_toml_into(&mut config, &paths.system_config, "system_config");

    // 3. 用户配置
    load_toml_into(&mut config, &paths.user_config, "user_config");

    // 4. 环境变量（CW_ 前缀）
    for (env_key, env_value) in std::env::vars() {
        if env_key.starts_with(env_prefix) {
            let config_key = env_key[env_prefix.len()..].to_lowercase();
            config.values.insert(
                config_key,
                ConfigValue {
                    value: env_value,
                    source: "env".to_string(),
                },
            );
        }
    }

    // 5. CLI 参数
    if let Some(overrides) = cli_overrides {
        for (key, value) in overrides {
            config.values.insert(
                key.clone(),
                ConfigValue {
                    value: value.clone(),
                    source: "cli".to_string(),
                },
            );
        }
    }

    config
}

/// 从 TOML 文件加载配置到 Config 对象。
///
/// 对齐 Python `_load_toml_into()` (L151-162)
/// 配置文件损坏时跳过（静默忽略）。
fn load_toml_into(config: &mut Config, path: &PathBuf, source: &str) {
    if !path.exists() {
        return;
    }
    let content = match std::fs::read_to_string(path) {
        Ok(c) => c,
        Err(_) => return,
    };
    match toml::from_str::<toml::Value>(&content) {
        Ok(data) => {
            flatten_toml(&data, config, source, "");
        }
        Err(_) => {
            // 配置文件损坏时跳过（与 Python 行为一致）
        }
    }
}

/// 将嵌套 TOML dict 扁平化为 dot-separated key。
///
/// 对齐 Python `_flatten_toml()` (L165-172)
fn flatten_toml(data: &toml::Value, config: &mut Config, source: &str, prefix: &str) {
    if let Some(table) = data.as_table() {
        for (key, value) in table {
            let full_key = if prefix.is_empty() {
                key.clone()
            } else {
                format!("{}.{}", prefix, key)
            };
            match value {
                toml::Value::Table(_) => {
                    flatten_toml(value, config, source, &full_key);
                }
                _ => {
                    config.values.insert(
                        full_key,
                        ConfigValue {
                            value: toml_value_to_string(value),
                            source: source.to_string(),
                        },
                    );
                }
            }
        }
    }
}

/// 将 toml::Value 转换为字符串（与 Python TOML 解析后的字符串表示对齐）。
fn toml_value_to_string(value: &toml::Value) -> String {
    match value {
        toml::Value::String(s) => s.clone(),
        toml::Value::Integer(i) => i.to_string(),
        toml::Value::Float(f) => f.to_string(),
        toml::Value::Boolean(b) => b.to_string(),
        toml::Value::Datetime(dt) => dt.to_string(),
        _ => value.to_string(),
    }
}

// ============================================================
// 角色化安装检查
// ============================================================

/// 支持的角色集合。
///
/// 对齐 Python `SUPPORTED_ROLES` (L179)
pub const SUPPORTED_ROLES: &[&str] = &["local", "client", "agent", "daemon", "all"];

/// 平台×角色支持矩阵。
///
/// 对齐 Python `PLATFORM_ROLE_SUPPORT` (L181-185)
///
/// 返回 (platform, 支持的角色列表)。
pub fn platform_role_support(platform: &str) -> Vec<&'static str> {
    match platform {
        "win32" | "windows" => vec!["local", "client"],
        "darwin" | "macos" => vec!["local", "client"],
        "linux" => vec!["local", "client", "agent", "daemon", "all"],
        _ => vec![],
    }
}

/// 检查当前平台是否支持指定角色。
///
/// 对齐 Python `check_role_supported()` (L188-192)
pub fn check_role_supported(role: &str, platform: Option<&str>) -> bool {
    let plat = platform.unwrap_or_else(|| std::env::consts::OS);
    let supported = platform_role_support(plat);
    supported.contains(&role)
}

/// 如果角色不受平台支持，fail-closed 退出。
///
/// 对齐 Python `fail_closed_unsupported()` (L195-206)
pub fn fail_closed_unsupported(role: &str, platform: Option<&str>) {
    if !check_role_supported(role, platform) {
        let plat = platform.unwrap_or_else(|| std::env::consts::OS);
        let supported = platform_role_support(plat);
        let supported_str = supported
            .iter()
            .map(|s| s.to_string())
            .collect::<Vec<_>>()
            .join(", ");
        eprintln!(
            "ERROR: Role '{}' is not supported on {}.\n\
             Supported roles: {}\n\
             Enterprise daemon/agent requires Linux with SO_PEERCRED, SCM_RIGHTS, and UDS.",
            role, plat, supported_str
        );
        exit(2);
    }
}

// ============================================================
// PyO3 暴露（供 Python wire-production 调用）
// ============================================================

use pyo3::prelude::*;
use pyo3::types::PyDict;

/// Python 暴露的 PlatformPaths（dict 形式）
///
/// 对齐 Python `release/config_loader.py:PlatformPaths.detect()`
#[pyfunction]
pub fn platform_paths_detect(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let paths = PlatformPaths::detect();
    let dict = PyDict::new(py);
    dict.set_item(
        "system_config",
        paths.system_config.to_string_lossy().to_string(),
    )?;
    dict.set_item(
        "user_config",
        paths.user_config.to_string_lossy().to_string(),
    )?;
    dict.set_item(
        "system_data",
        paths.system_data.to_string_lossy().to_string(),
    )?;
    dict.set_item("user_data", paths.user_data.to_string_lossy().to_string())?;
    dict.set_item(
        "runtime",
        paths
            .runtime
            .map(|p| p.to_string_lossy().to_string())
            .unwrap_or_default(),
    )?;
    Ok(dict)
}

/// Python 暴露的 load_config
///
/// 对齐 Python `release/config_loader.py:load_config()`
#[pyfunction]
#[pyo3(signature = (cli_overrides=None, env_prefix="CW_"))]
pub fn load_config_py<'py>(
    py: Python<'py>,
    cli_overrides: Option<Bound<'py, PyDict>>,
    env_prefix: &str,
) -> PyResult<Bound<'py, PyDict>> {
    let cli_map: Option<HashMap<String, String>> = if let Some(d) = cli_overrides {
        let mut m = HashMap::new();
        for (key, value) in d.iter() {
            let key_str: String = key.extract()?;
            let value_str: String = value.extract()?;
            m.insert(key_str, value_str);
        }
        Some(m)
    } else {
        None
    };
    let config = load_config(cli_map.as_ref(), env_prefix);
    let dict = PyDict::new(py);
    for (key, cv) in &config.values {
        let inner = PyDict::new(py);
        inner.set_item("value", &cv.value)?;
        inner.set_item("source", &cv.source)?;
        dict.set_item(key, inner)?;
    }
    Ok(dict)
}

/// Python 暴露的 config_explain
///
/// 对齐 Python `release/config_loader.py:Config.explain()`
#[pyfunction]
pub fn config_explain_py(py: Python<'_>) -> PyResult<Vec<Bound<'_, PyDict>>> {
    let config = load_config(None, "CW_");
    let mut result = Vec::new();
    for entry in config.explain() {
        let dict = PyDict::new(py);
        dict.set_item("key", &entry.key)?;
        dict.set_item("value", &entry.value)?;
        dict.set_item("source", &entry.source)?;
        result.push(dict);
    }
    Ok(result)
}

/// Python 暴露的 check_role_supported
///
/// 对齐 Python `release/config_loader.py:check_role_supported()`
#[pyfunction]
pub fn check_role_supported_py(role: &str, platform: Option<&str>) -> bool {
    check_role_supported(role, platform)
}
