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
/// 优先级：显式注入 `endpoint` > `CW_DAEMON_SOCKET` 环境变量 >
/// 默认 `/run/callwarden/callwarden.sock`
///
/// TP-11G 2a 尾批参数化（注入优先于全局状态变异）：此前测试为验证
/// env 覆盖直接改写全局环境，与 router 并行测试竞态（test_d5_1 间歇
/// FAIL）；参数化后测试直传 endpoint，env 断言改只读一致性检查。
pub fn daemon_socket_path(endpoint: Option<&str>) -> PathBuf {
    match endpoint {
        Some(e) => PathBuf::from(e),
        None => std::env::var(DAEMON_SOCKET_ENV)
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from(DEFAULT_DAEMON_SOCKET)),
    }
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
/// 对齐 Python `config.py:is_daemon_available()` 的**单轨形态门禁**：
/// ADR-002 单轨 HTTPS+mTLS 下，endpoint 为 http(s):// URL 才可能可用；
/// socket / Named Pipe 路径一律 fail-closed false（传输已删除，存在性
/// 不再代表可用，亦覆盖 TP-49「未 arm 占位者」假阳性方向）。
///
/// 本路由层为**轻量决策层**（无 TLS 客户端依赖，hyper 仅 server 特性）：
/// 对 http(s) 端点做 TCP 可达性探针（2s 短超时）。协议级探针（mTLS
/// 握手 + ping 应答）由 Python 消费层 `config.is_daemon_available` 承担——
/// `is_daemon_available_py` 无生产调用方，仅服务于路由契约 diff 测试
/// （两侧 TCP 语义对齐）。
///
/// # 参数
/// - `socket_path`: daemon 端点（单轨语义下为 http(s):// URL；保留旧参数
///   名以兼容 PyO3 签名与 diff 测试注入）
/// - `platform`: 平台名（`std::env::consts::OS` 或测试指定值）
pub fn is_daemon_available(socket_path: &Path, platform: &str) -> bool {
    match platform {
        // 单轨：linux / windows 统一 http(s) 端点形态门禁 + TCP 可达性探针
        "linux" | "windows" => {
            let endpoint = socket_path.to_string_lossy();
            if !endpoint.starts_with("https://") && !endpoint.starts_with("http://") {
                return false;
            }
            tcp_reachable(&endpoint)
        }
        // macOS：单轨 HTTPS 之外无本地传输，返回 false
        "macos" => false,
        // 未知平台 fail-closed
        _ => false,
    }
}

/// 对 http(s) endpoint 做 TCP 可达性探针（2s 短超时）。
///
/// 解析 `host:port`（无端口补 https 默认 443）；解析失败 / 连接失败 =
/// 不可用。仅可达性判定——TLS/HTTP 应答由 Python 协议级探针承担。
fn tcp_reachable(endpoint: &str) -> bool {
    use std::net::{TcpStream, ToSocketAddrs};
    use std::time::Duration;

    let host_port = endpoint
        .trim_start_matches("https://")
        .trim_start_matches("http://");
    let addr = match host_port.rsplit(':').next() {
        Some(port) if port.parse::<u16>().is_ok() => host_port.to_string(),
        _ => format!("{host_port}:443"),
    };
    match addr.to_socket_addrs() {
        Ok(mut it) => match it.next() {
            Some(socket_addr) => {
                TcpStream::connect_timeout(&socket_addr, Duration::from_secs(2)).is_ok()
            }
            None => false,
        },
        Err(_) => false,
    }
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
pub fn route_command(mode: &DaemonMode, socket_path: &Path, platform: &str) -> RouteDecision {
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
///
/// **边界校验（TP-49 假阳性卡）**：`platform` 与当前 OS 不一致时 eprintln
/// 告警；其中**破坏性组合**（非 linux 实机喂 linux 语义 + 管道形态路径）
/// 直接拒绝返回 false。历史依据：早期 linux 分支为 `socket_path.exists()`，
/// 对 Windows 管道名「接触即破坏」（属性查询打开消耗 armed 实例，与返回值
/// 无关，实证见台账 §A4/A5）。ADR-002 单轨后 linux 分支已改为 http(s)
/// 形态门禁 + TCP 探针，不再接触文件系统；本 guard 作为**纵深防御**保留——
/// 管道形态端点在任何平台语义下都不应进入探针路径。其余不一致组合
/// （macos/windows 语义在异机恒 false）保留模拟，维持 diff 测试的显式
/// 平台注入能力。**不做自动纠正**：纠正会摧毁跨平台语义模拟（测试依赖）
/// 并掩盖调用方 bug。
#[pyfunction]
pub fn is_daemon_available_py(socket_path: &str, platform: &str) -> bool {
    let host = std::env::consts::OS;
    if platform != host {
        eprintln!(
            "[callwarden] WARNING: is_daemon_available_py platform mismatch: got '{platform}', host is '{host}'"
        );
        if platform == "linux"
            && host != "linux"
            && (socket_path.replace('\\', "/").to_ascii_lowercase()
                .starts_with("//./pipe/")
                || socket_path.replace('\\', "/").to_ascii_lowercase()
                    .starts_with("//?/pipe/"))
        {
            eprintln!(
                "[callwarden] REFUSED: linux availability semantics on non-linux host against a pipe-shaped endpoint — contact destroys pipe instances"
            );
            return false;
        }
    }
    is_daemon_available(Path::new(socket_path), platform)
}

/// Python 暴露的 daemon_socket_path。
///
/// 对齐 Python `config.py:DAEMON_SOCKET_PATH`
#[pyfunction]
pub fn daemon_socket_path_py() -> String {
    daemon_socket_path(None).to_string_lossy().to_string()
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
        assert_eq!(
            DaemonMode::from_str(Some("enterprise")),
            DaemonMode::Enterprise
        );
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
    fn test_d3_1_linux_socket_path_fail_closed() {
        // ADR-002 单轨：socket 路径形态一律 fail-closed——旧 socket 文件
        // 残留不代表 daemon 可用（UDS 传输已删除）。
        let tmp = std::env::temp_dir().join("cw_test_socket_d3_1.sock");
        std::fs::write(&tmp, b"").ok();
        assert!(!is_daemon_available(&tmp, "linux"));
        std::fs::remove_file(&tmp).ok();
    }

    #[test]
    fn test_d3_2_linux_socket_not_exists() {
        let path = Path::new("/run/callwarden/nonexistent_socket_for_test.sock");
        assert!(!is_daemon_available(path, "linux"));
    }

    #[test]
    fn test_d3_3_windows_socket_path_fail_closed() {
        // 历史（TP-11G #48 / F-ROUTER-01）：Windows 分支曾无条件探测本用户
        // 默认管道、丢弃 socket_path，导致 Enterprise fail-closed 可被未认证
        // 方满足、测试隔离失效。ADR-002 单轨后语义收敛：非 http(s) 端点在
        // Windows（及 linux）一律 fail-closed，与 Python 镜像对齐。
        let explicit = Path::new("C:\\callwarden\\socket.sock");
        assert!(
            !is_daemon_available(explicit, "windows"),
            "非 http(s) 端点在 Windows 上必须 fail-closed"
        );
        assert!(
            !is_daemon_available(Path::new(DEFAULT_DAEMON_SOCKET), "windows"),
            "socket 默认路径在 Windows 上必须 fail-closed"
        );
    }

    #[test]
    fn test_d3_5_https_unreachable_endpoint_false() {
        // 单轨：http(s) 端点形态合法但端口无监听 → TCP 可达性探针失败 →
        // false。端口 1（tcpmux）在测试机确定无监听；两侧 TCP 语义一致。
        assert!(!is_daemon_available(
            Path::new("https://127.0.0.1:1"),
            "linux"
        ));
        assert!(!is_daemon_available(
            Path::new("https://127.0.0.1:1"),
            "windows"
        ));
    }

    #[cfg(windows)]
    #[test]
    fn test_tp49_pywrapper_refuses_verbatim_pipe_semantics() {
        // 关屿验收 P2：verbatim 前缀 `\\?\pipe\` 归一后为 `//?/pipe/`，
        // 原 matcher 只认 `//./pipe/` → verbatim 形态绕过 guard 落入
        // linux exists()（属性类打开，对管道同样接触即破坏——语义推导
        // 待 tp11g 实测验证，guard 先行封死）。扩展 matcher 后：
        // verbatim 管道形态 + linux 语义 → 拒绝 false。
        let verbatim = r"\\?\pipe\callwarden-isotest-verbatim-tp49";
        assert!(!is_daemon_available_py(verbatim, "linux"));
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
    fn test_d4_2_enterprise_linux_socket_path_fail_closed() {
        // ADR-002 单轨：socket 路径形态 fail-closed → Enterprise 模式
        // 无法证实 daemon 可用 → Unavailable（不回退本地）。
        let tmp = std::env::temp_dir().join("cw_test_socket_d4_2.sock");
        std::fs::write(&tmp, b"").ok();
        assert_eq!(
            route_command(&DaemonMode::Enterprise, &tmp, "linux"),
            RouteDecision::Unavailable
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
    fn test_d4_5_auto_linux_socket_path_falls_back_local() {
        // ADR-002 单轨：socket 路径形态 fail-closed → auto 模式回退 Local。
        let tmp = std::env::temp_dir().join("cw_test_socket_d4_5.sock");
        std::fs::write(&tmp, b"").ok();
        assert_eq!(
            route_command(&DaemonMode::Auto, &tmp, "linux"),
            RouteDecision::Local
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
    //
    // TP-11G 2a 尾批（P2 竞态修复）：原 test_d5_1/test_d5_2 直接改写全局
    // `CW_DAEMON_SOCKET`，与 router 并行测试竞态（cli::router 单独 3 跑
    // 1 FAIL）。参数化注入后测试直传 endpoint，env 断言改**只读一致性**
    // 检查——不再有任何全局状态变异。

    #[test]
    fn test_d5_1_endpoint_injection() {
        // 注入优先于全局状态变异：Some(endpoint) 恒生效，与 env/默认值无关
        assert_eq!(
            daemon_socket_path(Some("/tmp/x.sock")),
            PathBuf::from("/tmp/x.sock")
        );
        // env 一致性（只读）：注入 None 时，env 已设则遵循之，未设则默认
        match env::var(DAEMON_SOCKET_ENV) {
            Ok(v) => assert_eq!(daemon_socket_path(None), PathBuf::from(v)),
            Err(_) => assert_eq!(
                daemon_socket_path(None),
                PathBuf::from(DEFAULT_DAEMON_SOCKET)
            ),
        }
    }

    #[test]
    fn test_d5_2_default_path() {
        // 只读断言：env 未设时注入 None 应得默认端点；
        // env 已设的场景由 test_d5_1 的一致性断言覆盖（本用例不变异全局状态）
        if env::var(DAEMON_SOCKET_ENV).is_err() {
            assert_eq!(
                daemon_socket_path(None),
                PathBuf::from(DEFAULT_DAEMON_SOCKET)
            );
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
