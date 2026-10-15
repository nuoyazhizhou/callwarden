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

// `encode_wide` 是 Windows 专属扩展 trait，仅 Windows 目标需要。
#[cfg(windows)]
use std::os::windows::ffi::OsStrExt;

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
/// 对齐 Python `config.py:is_daemon_available()`
///
/// - Linux → `socket_path.exists()`
/// - macOS → false（UDS 不可用，且未实现 Named Pipe）
/// - Windows → `windows_endpoint_available(socket_path)`（尊重传入端点，
///   见该函数的三分支与残留局限说明）
///
/// # 参数
/// - `socket_path`: daemon socket 路径（或命名管道路径）
/// - `platform`: 平台名（`std::env::consts::OS` 或测试指定值）
pub fn is_daemon_available(socket_path: &Path, platform: &str) -> bool {
    match platform {
        // Linux：UDS socket 文件存在即认为 daemon 可用
        "linux" => socket_path.exists(),
        // macOS：UDS 不可用，且未实现 Named Pipe，返回 false
        "macos" => false,
        // Windows：按传入端点判定（TP-11G #48 / F-ROUTER-01）
        "windows" => windows_endpoint_available(socket_path),
        // 未知平台 fail-closed
        _ => false,
    }
}

/// Windows 端点可用性判定（TP-11G #48 / F-ROUTER-01 修复）。
///
/// 三分支，fail-closed 优先：
/// 1. `socket_path` 为命名管道形态（`\\.\pipe\...`）→ 探测**该管道**：
///    尊重运维显式配置的端点，测试也可注入隔离管道；
/// 2. `socket_path` 等于 `DEFAULT_DAEMON_SOCKET`（未显式配置——注意该默认值
///    是 Unix 式路径 `/run/callwarden/callwarden.sock`，在 Windows 上永不
///    存在）→ 退化为探测本用户默认管道（保留既有生产行为，见下）；
/// 3. 其他显式路径（UDS 式文件路径，Windows 不支持）→ **false**（fail-closed：
///    该端点在当前平台不可能存在）。
///
/// 为何分支 2 保留环境派生探测：Windows 上 daemon 数据通道默认走 HTTP
/// loopback（`runtime.rs` 对 `\\.\pipe\` 前缀的特判即为此），管道探测只是
/// 「本用户是否有 daemon 在跑」的活性代理。若分支 2 也判 false，Windows
/// 生产上 Enterprise 将恒 Unavailable、路由全断。
///
/// 残留局限（**未闭环，另行立项**）：未显式配置时仍无法识别「默认管道属于
/// 另一个实例」的跨实例混淆；彻底消除需改为探测真实数据通道（HTTP
/// endpoint），而非旁路管道活性。
#[cfg(windows)]
fn windows_endpoint_available(socket_path: &Path) -> bool {
    let raw = socket_path.to_string_lossy();
    // 前缀匹配对 ASCII 大小写不敏感（TP-48 复核信息项）：Win32 管道名本身
    // 不区分大小写，运维配置 `\\.\PIPE\foo` 与 `\\.\pipe\foo` 语义等价，都应
    // 进分支 1。原先大写形态静默落分支 3 → false，是「忽略显式端点」缺陷的
    // 残留形态。探测时仍传原始形态（WaitNamedPipeW 大小写不敏感）。
    let normalized = raw.replace('\\', "/").to_ascii_lowercase();
    if normalized.starts_with("//./pipe/") {
        return wait_named_pipe(&raw);
    }
    if socket_path == Path::new(DEFAULT_DAEMON_SOCKET) {
        return windows_named_pipe_available();
    }
    false
}

#[cfg(not(windows))]
fn windows_endpoint_available(_socket_path: &Path) -> bool {
    false
}

/// Windows 下探测 daemon 命名管道是否可用。
///
/// 管道名与 daemon 绑定一致：`\\.\pipe\callwarden-<user-sid>`
/// （见 `daemon/transport_windows.rs:get_current_user_sid` + 管道名派生）。
#[cfg(windows)]
fn windows_named_pipe_available() -> bool {
    let sid = match crate::daemon::transport_windows::get_current_user_sid() {
        Ok(s) => s,
        Err(_) => return false,
    };
    wait_named_pipe(&format!(r"\\.\pipe\callwarden-{}", sid))
}

#[cfg(not(windows))]
fn windows_named_pipe_available() -> bool {
    false
}

/// 以 `WaitNamedPipeW` 探测指定管道：返回非 0 表示存在可用实例。
///
/// 超时统一 **5000ms**，与 `cw_client.rs` / `cw_daemon.rs` /
/// `daemon/client.rs` 三处探针一致（TP-11G 关屿 N3）。
/// Win32 语义纠正：`NMPWAIT_USE_DEFAULT_WAIT = 0` 表示「等待服务端
/// `CreateNamedPipe` 时声明的默认超时」，**并非**「立即返回」；原注释
/// 「timeout=0：立即返回不阻塞」属误导性契约。
#[cfg(windows)]
fn wait_named_pipe(pipe_name: &str) -> bool {
    let wide: Vec<u16> = std::path::Path::new(pipe_name)
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect();
    unsafe { windows_sys::Win32::System::Pipes::WaitNamedPipeW(wide.as_ptr(), 5000) != 0 }
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
    fn test_d3_3_windows_probes_named_pipe() {
        // TP-11G #48（F-ROUTER-01）：Windows 分支必须**尊重传入端点**。
        // 原实现无条件探测本用户默认管道、丢弃 socket_path，导致：
        //   - Enterprise fail-closed 可被未认证方满足；
        //   - 测试隔离失效（探针打到真实生产 daemon）；
        //   - 与 Linux 分支语义不对称。
        // 本用例原先把该缺陷行为写成期望契约（断言「任意路径 == 默认管道探测」），
        // 现按新契约改写。
        let explicit = Path::new("C:\\callwarden\\socket.sock");
        assert!(
            !is_daemon_available(explicit, "windows"),
            "显式配置的非管道端点在 Windows 上必须 fail-closed（不再旁路到默认管道）"
        );
        assert_eq!(
            is_daemon_available(Path::new(DEFAULT_DAEMON_SOCKET), "windows"),
            windows_named_pipe_available(),
            "未显式配置（Unix 默认路径）时保留默认管道探测，保 Windows 生产行为"
        );
    }

    // ============================================================
    // TP-48 N-a（何序复核）：分支 1（显式管道路径）真实注入测试。
    // 4 个原失败用例全走分支 3（fail-closed），分支 1 此前零覆盖。
    // 起真实管道服务端验证「探测的是注入管道本身」，Q11 注入终态落地。
    // 仅 Windows 编译；Linux 上不存在（不计 skip）。
    // ============================================================

    /// 起一次性命名管道服务端（默认安全属性即可——测试不需要 daemon 的
    /// SDDL 隔离，只要名字存在且处于 listening 态，WaitNamedPipeW 即命中）。
    #[cfg(windows)]
    fn spawn_pipe_server(pipe_name: &str) -> windows_sys::Win32::Foundation::HANDLE {
        use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_FIRST_PIPE_INSTANCE, PIPE_ACCESS_DUPLEX,
        };
        use windows_sys::Win32::System::Pipes::{CreateNamedPipeW, PIPE_TYPE_BYTE, PIPE_WAIT};

        let wide: Vec<u16> = pipe_name.encode_utf16().chain(Some(0)).collect();
        let handle = unsafe {
            CreateNamedPipeW(
                wide.as_ptr(),
                PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
                PIPE_TYPE_BYTE | PIPE_WAIT,
                1, // 单实例：本测试独占
                1024,
                1024,
                0, // 默认超时
                std::ptr::null(),
            )
        };
        assert_ne!(
            handle, INVALID_HANDLE_VALUE,
            "CreateNamedPipeW 失败: {pipe_name}"
        );
        handle
    }

    #[cfg(windows)]
    fn unique_pipe_name(tag: &str) -> String {
        use std::time::{SystemTime, UNIX_EPOCH};
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        format!(
            r"\\.\pipe\callwarden-test-{}-{}-{}",
            tag,
            std::process::id(),
            nanos
        )
    }

    #[cfg(windows)]
    #[test]
    fn test_tp48_branch1_explicit_pipe_probed_not_bypassed() {
        use windows_sys::Win32::Foundation::CloseHandle;

        let pipe_name = unique_pipe_name("b1");
        let server = spawn_pipe_server(&pipe_name);

        // 分支 1：显式管道路径 → 探测**该管道**（listening 实例在）→ true。
        assert!(
            is_daemon_available(Path::new(&pipe_name), "windows"),
            "显式管道路径必须探测该管道本身"
        );

        // 大写前缀形态 `\\.\PIPE\` 也必须走分支 1（大小写不敏感匹配）→ true。
        // 若静默落分支 3（fail-closed）会得到 false，本断言即失败。
        let upper = pipe_name.replacen(r"\\.\pipe\", r"\\.\PIPE\", 1);
        assert!(
            is_daemon_available(Path::new(&upper), "windows"),
            "大写 PIPE 前缀必须被识别为管道路径（Win32 管道名大小写不敏感）"
        );

        // 服务端销毁 → 管道名消失 → false。若实现仍旁路默认管道（旧缺陷），
        // 本机真实 daemon 会让它得到 true，本断言即失败——同时证明密闭。
        unsafe {
            CloseHandle(server);
        }
        assert!(
            !is_daemon_available(Path::new(&pipe_name), "windows"),
            "管道销毁后必须不可用（证明探测的是注入管道而非默认管道）"
        );
    }

    #[cfg(windows)]
    #[test]
    fn test_tp48_branch1_nonexistent_pipe_fails_closed() {
        // 从未存在的管道名 → ERROR_FILE_NOT_FOUND → 立即 false（不触发 5s 等待）。
        let missing = unique_pipe_name("missing");
        assert!(!is_daemon_available(Path::new(&missing), "windows"));
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
        assert_eq!(daemon_socket_path(), PathBuf::from(DEFAULT_DAEMON_SOCKET));
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
