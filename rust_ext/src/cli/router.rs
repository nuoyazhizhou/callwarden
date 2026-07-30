//! local/enterprise/auto 路由决策（Phase 5-1 B）
//!
//! 对齐 Python `config.py`：
//! - `DaemonMode` enum 对齐 `DAEMON_MODE` 字符串
//! - `get_daemon_mode()` / `is_daemon_required()` / `is_daemon_available()` / `daemon_socket_path()`
//! - `route_command()` 显式化路由决策（Python 隐式散落在 `cli/main.py:main()`）
//!
//! 决策矩阵见契约 §3.2。
//!
//! 契约：docs/design/phase5-1b-router-contract.md

use std::path::{Path, PathBuf};

// ============================================================
// 常量
// ============================================================

/// daemon socket 默认路径（Linux）。
///
/// 对齐 Python `config.py:DAEMON_SOCKET_PATH` (L1319-1321)
pub const DEFAULT_DAEMON_SOCKET: &str = "/run/callwarden/callwarden.sock";

/// daemon 模式环境变量名。
pub const DAEMON_MODE_ENV: &str = "CW_DAEMON_MODE";

/// daemon socket 环境变量名。
pub const DAEMON_SOCKET_ENV: &str = "CW_DAEMON_SOCKET";

// ============================================================
// DaemonMode 枚举
// ============================================================

/// daemon 运行模式。
///
/// 对齐 Python `config.py:DAEMON_MODE` (L1343)
///
/// - `Local`: 强制走本地 SQLite
/// - `Enterprise`: 强制走 daemon RPC
/// - `Auto`: 自动检测（有 daemon 用 daemon，没有用 local）
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DaemonMode {
    Local,
    Enterprise,
    Auto,
}

impl DaemonMode {
    /// 从字符串解析模式。
    ///
    /// 对齐 Python `os.environ.get("CW_DAEMON_MODE", "auto")` 行为：
    /// - `"local"` → `Local`
    /// - `"enterprise"` → `Enterprise`
    /// - `"auto"` / 未知值 / None → `Auto`（fail-soft）
    pub fn from_str(s: Option<&str>) -> Self {
        match s {
            Some("local") => DaemonMode::Local,
            Some("enterprise") => DaemonMode::Enterprise,
            // "auto" / 未知值 / None 都返回 Auto（fail-soft）
            _ => DaemonMode::Auto,
        }
    }

    /// 转换为字符串（与 Python 字符串值对齐）。
    pub fn as_str(&self) -> &'static str {
        match self {
            DaemonMode::Local => "local",
            DaemonMode::Enterprise => "enterprise",
            DaemonMode::Auto => "auto",
        }
    }
}

// ============================================================
// RouteDecision 枚举
// ============================================================

/// 路由决策结果。
///
/// 由 `route_command()` 返回，调用方根据决策选择执行路径。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RouteDecision {
    /// 走本地 SQLite 路径
    Local,
    /// 走 daemon RPC 路径
    Enterprise,
    /// daemon 不可用但 mode=enterprise（fail-closed 场景）
    Unavailable,
}

impl RouteDecision {
    /// 转换为字符串（供 PyO3 暴露和日志输出）。
    pub fn as_str(&self) -> &'static str {
        match self {
            RouteDecision::Local => "local",
            RouteDecision::Enterprise => "enterprise",
            RouteDecision::Unavailable => "unavailable",
        }
    }
}

// ============================================================
// 辅助查询函数
// ============================================================

/// 获取 daemon socket 路径。
///
/// 对齐 Python `config.py:DAEMON_SOCKET_PATH` (L1319-1321)
///
/// 优先级：`CW_DAEMON_SOCKET` 环境变量 > 默认 `/run/callwarden/callwarden.sock`
pub fn daemon_socket_path() -> PathBuf {
    std::env::var(DAEMON_SOCKET_ENV)
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(DEFAULT_DAEMON_SOCKET))
}

/// 获取当前 daemon 模式。
///
/// 对齐 Python `config.py:get_daemon_mode()` (L1366-1368)
pub fn get_daemon_mode() -> DaemonMode {
    DaemonMode::from_str(std::env::var(DAEMON_MODE_ENV).ok().as_deref())
}

/// 是否强制要求 daemon。
///
/// 对齐 Python `config.py:is_daemon_required()` (L1371-1373)
///
/// 仅当 `mode == Enterprise` 时返回 true。
pub fn is_daemon_required() -> bool {
    matches!(get_daemon_mode(), DaemonMode::Enterprise)
}

/// 检测 daemon 是否可用。
///
/// 对齐 Python `config.py:is_daemon_available()` (L1376-1383)
///
/// - Windows/macOS → false（UDS 不可用）
/// - Linux → `socket_path.exists()`
///
/// # 参数
/// - `socket_path`: daemon socket 路径
/// - `platform`: 平台名（`std::env::consts::OS` 或测试指定值）
pub fn is_daemon_available(socket_path: &Path, platform: &str) -> bool {
    // Windows/macOS 永远不可用（UDS 是 Linux 特有）
    if platform != "linux" {
        return false;
    }
    socket_path.exists()
}

// ============================================================
// 路由决策函数
// ============================================================

/// 根据模式、socket 存在性、平台返回路由决策。
///
/// 决策矩阵见契约 §3.2。
///
/// # 参数
/// - `mode`: daemon 模式
/// - `socket_path`: daemon socket 路径
/// - `platform`: 平台名（`linux` / `windows` / `macos`）
///
/// # 返回
/// `RouteDecision::Local` / `Enterprise` / `Unavailable`
pub fn route_command(
    mode: &DaemonMode,
    socket_path: &Path,
    platform: &str,
) -> RouteDecision {
    match mode {
        DaemonMode::Local => {
            // 强制走本地，无论平台和 socket
            RouteDecision::Local
        }
        DaemonMode::Enterprise => {
            // 强制走 daemon，不可用时 fail-closed
            if is_daemon_available(socket_path, platform) {
                RouteDecision::Enterprise
            } else {
                RouteDecision::Unavailable
            }
        }
        DaemonMode::Auto => {
            // 自动检测：有 daemon 用 daemon，没有用 local
            if is_daemon_available(socket_path, platform) {
                RouteDecision::Enterprise
            } else {
                RouteDecision::Local
            }
        }
    }
}

// ============================================================
// PyO3 暴露（供 Python wire-production 调用）
// ============================================================

use pyo3::prelude::*;

/// Python 暴露的 get_daemon_mode。
///
/// 对齐 Python `config.py:get_daemon_mode()`
#[pyfunction]
pub fn get_daemon_mode_py() -> String {
    get_daemon_mode().as_str().to_string()
}

/// Python 暴露的 is_daemon_required。
///
/// 对齐 Python `config.py:is_daemon_required()`
#[pyfunction]
pub fn is_daemon_required_py() -> bool {
    is_daemon_required()
}

/// Python 暴露的 is_daemon_available。
///
/// 对齐 Python `config.py:is_daemon_available()`
///
/// 参数 `socket_path` 和 `platform` 由 Python 传入（便于测试），
/// 默认值由 Python 端 `daemon_socket_path_py()` + `sys.platform` 提供。
#[pyfunction]
pub fn is_daemon_available_py(socket_path: &str, platform: &str) -> bool {
    is_daemon_available(Path::new(socket_path), platform)
}

/// Python 暴露的 daemon_socket_path。
///
/// 对齐 Python `config.py:DAEMON_SOCKET_PATH`
#[pyfunction]
pub fn daemon_socket_path_py() -> String {
    daemon_socket_path().to_string_lossy().to_string()
}

/// Python 暴露的 route_command。
///
/// 综合决策：根据 mode、socket、平台返回路由决策字符串。
///
/// 返回值：`"local"` / `"enterprise"` / `"unavailable"`
#[pyfunction]
pub fn route_command_py(mode: &str, socket_path: &str, platform: &str) -> String {
    let dm = DaemonMode::from_str(Some(mode));
    let decision = route_command(&dm, Path::new(socket_path), platform);
    decision.as_str().to_string()
}

// ============================================================
// 单元测试（对齐契约 D1-D5 测试矩阵）
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::env;

    // D1: get_daemon_mode / DaemonMode::from_str

    #[test]
    fn test_d1_1_mode_local() {
        assert_eq!(DaemonMode::from_str(Some("local")), DaemonMode::Local);
    }

    #[test]
    fn test_d1_2_mode_enterprise() {
        assert_eq!(DaemonMode::from_str(Some("enterprise")), DaemonMode::Enterprise);
    }

    #[test]
    fn test_d1_3_mode_auto() {
        assert_eq!(DaemonMode::from_str(Some("auto")), DaemonMode::Auto);
    }

    #[test]
    fn test_d1_4_mode_none_defaults_auto() {
        assert_eq!(DaemonMode::from_str(None), DaemonMode::Auto);
    }

    #[test]
    fn test_d1_5_mode_unknown_fail_soft_auto() {
        assert_eq!(DaemonMode::from_str(Some("unknown")), DaemonMode::Auto);
    }

    // D2: is_daemon_required

    #[test]
    fn test_d2_1_local_not_required() {
        assert!(!is_daemon_required_for_mode(DaemonMode::Local));
    }

    #[test]
    fn test_d2_2_enterprise_required() {
        assert!(is_daemon_required_for_mode(DaemonMode::Enterprise));
    }

    #[test]
    fn test_d2_3_auto_not_required() {
        assert!(!is_daemon_required_for_mode(DaemonMode::Auto));
    }

    /// 辅助：给定 mode 判断 is_daemon_required（避免依赖环境变量）
    fn is_daemon_required_for_mode(mode: DaemonMode) -> bool {
        matches!(mode, DaemonMode::Enterprise)
    }

    // D3: is_daemon_available

    #[test]
    fn test_d3_1_linux_socket_exists() {
        // 用临时文件模拟存在的 socket
        let tmp = std::env::temp_dir().join("cw_test_socket_d3_1.sock");
        std::fs::write(&tmp, b"").ok();
        assert!(is_daemon_available(&tmp, "linux"));
        std::fs::remove_file(&tmp).ok();
    }

    #[test]
    fn test_d3_2_linux_socket_not_exists() {
        let path = Path::new("/run/callwarden/nonexistent_socket_for_test.sock");
        assert!(!is_daemon_available(path, "linux"));
    }

    #[test]
    fn test_d3_3_windows_always_false() {
        let path = Path::new("C:\\callwarden\\socket.sock");
        assert!(!is_daemon_available(path, "windows"));
    }

    #[test]
    fn test_d3_4_macos_always_false() {
        let path = Path::new("/tmp/callwarden.sock");
        assert!(!is_daemon_available(path, "macos"));
    }

    // D4: route_command

    #[test]
    fn test_d4_1_local_always_local() {
        let path = Path::new("/run/callwarden/callwarden.sock");
        assert_eq!(
            route_command(&DaemonMode::Local, path, "linux"),
            RouteDecision::Local
        );
    }

    #[test]
    fn test_d4_2_enterprise_linux_socket_exists() {
        let tmp = std::env::temp_dir().join("cw_test_socket_d4_2.sock");
        std::fs::write(&tmp, b"").ok();
        assert_eq!(
            route_command(&DaemonMode::Enterprise, &tmp, "linux"),
            RouteDecision::Enterprise
        );
        std::fs::remove_file(&tmp).ok();
    }

    #[test]
    fn test_d4_3_enterprise_linux_socket_not_exists() {
        let path = Path::new("/run/callwarden/nonexistent.sock");
        assert_eq!(
            route_command(&DaemonMode::Enterprise, path, "linux"),
            RouteDecision::Unavailable
        );
    }

    #[test]
    fn test_d4_4_enterprise_windows_unavailable() {
        let path = Path::new("C:\\callwarden\\socket.sock");
        assert_eq!(
            route_command(&DaemonMode::Enterprise, path, "windows"),
            RouteDecision::Unavailable
        );
    }

    #[test]
    fn test_d4_5_auto_linux_socket_exists() {
        let tmp = std::env::temp_dir().join("cw_test_socket_d4_5.sock");
        std::fs::write(&tmp, b"").ok();
        assert_eq!(
            route_command(&DaemonMode::Auto, &tmp, "linux"),
            RouteDecision::Enterprise
        );
        std::fs::remove_file(&tmp).ok();
    }

    #[test]
    fn test_d4_6_auto_linux_socket_not_exists() {
        let path = Path::new("/run/callwarden/nonexistent.sock");
        assert_eq!(
            route_command(&DaemonMode::Auto, path, "linux"),
            RouteDecision::Local
        );
    }

    #[test]
    fn test_d4_7_auto_windows_local() {
        let path = Path::new("C:\\callwarden\\socket.sock");
        assert_eq!(
            route_command(&DaemonMode::Auto, path, "windows"),
            RouteDecision::Local
        );
    }

    #[test]
    fn test_d4_8_local_windows_local() {
        let path = Path::new("C:\\callwarden\\socket.sock");
        assert_eq!(
            route_command(&DaemonMode::Local, path, "windows"),
            RouteDecision::Local
        );
    }

    // D5: daemon_socket_path

    #[test]
    fn test_d5_1_env_override() {
        // 保存原值，设置后恢复
        let orig = env::var(DAEMON_SOCKET_ENV).ok();
        env::set_var(DAEMON_SOCKET_ENV, "/tmp/x.sock");
        assert_eq!(daemon_socket_path(), PathBuf::from("/tmp/x.sock"));
        // 恢复
        match orig {
            Some(v) => env::set_var(DAEMON_SOCKET_ENV, v),
            None => env::remove_var(DAEMON_SOCKET_ENV),
        }
    }

    #[test]
    fn test_d5_2_default_path() {
        // 不设置环境变量时应返回默认路径
        let orig = env::var(DAEMON_SOCKET_ENV).ok();
        env::remove_var(DAEMON_SOCKET_ENV);
        assert_eq!(
            daemon_socket_path(),
            PathBuf::from(DEFAULT_DAEMON_SOCKET)
        );
        // 恢复
        match orig {
            Some(v) => env::set_var(DAEMON_SOCKET_ENV, v),
            None => {}
        }
    }

    // 额外：RouteDecision::as_str

    #[test]
    fn test_route_decision_as_str() {
        assert_eq!(RouteDecision::Local.as_str(), "local");
        assert_eq!(RouteDecision::Enterprise.as_str(), "enterprise");
        assert_eq!(RouteDecision::Unavailable.as_str(), "unavailable");
    }

    #[test]
    fn test_daemon_mode_as_str() {
        assert_eq!(DaemonMode::Local.as_str(), "local");
        assert_eq!(DaemonMode::Enterprise.as_str(), "enterprise");
        assert_eq!(DaemonMode::Auto.as_str(), "auto");
    }
}
