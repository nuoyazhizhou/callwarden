//! cw-client binary（Phase 5-2 Slice 1 + Slice 2）
//!
//! Call Warden daemon RPC client。通过 UDS 连接 daemon，执行 RPC 调用。
//!
//! 子命令：
//! - `ping`：测试 daemon 连接（Slice 1）
//! - `query`：查询共享 snapshot（Slice 2，支持 8 种 query 类型）
//!
//! 跨平台编译：
//! - Unix（Linux/macOS）：UDS client 可用，子命令连接 daemon
//! - Windows：UDS 不可用，子命令返回平台提示（exit 2）
//!
//! 契约：
//! - Slice 1: docs/design/phase5-2-slice1-daemon-client-contract.md §3.4
//! - Slice 2: query 参数构建对齐 cli/daemon_commands.py:run_daemon_command

use clap::{Parser, Subcommand};

/// Call Warden — daemon RPC client
#[derive(Parser)]
#[command(
    name = "cw-client",
    version,
    about = "Call Warden daemon RPC client (UDS)"
)]
struct Cli {
    /// daemon socket 路径（默认 /tmp/callwarden_daemon.sock）
    #[arg(long, default_value = "/tmp/callwarden_daemon.sock", global = true)]
    socket: String,

    /// RPC 超时（秒）
    #[arg(long, default_value_t = 30, global = true)]
    timeout: u64,

    /// 子命令
    #[command(subcommand)]
    command: Option<Commands>,
}

/// cw-client 子命令
#[derive(Subcommand)]
enum Commands {
    /// ping daemon（测试连接）
    Ping,

    /// 查询共享 snapshot（支持 stats/symbol/search/callers/callees/...）
    Query {
        /// workspace instance ID
        workspace_id: String,

        /// 查询类型
        #[arg(value_enum)]
        query_type: QueryType,

        /// 主查询值（symbol 的 qualified_name / search 的 query / callers 的 callee_name 等）
        #[arg(default_value = "")]
        value: String,

        /// callers/callees 的限定名过滤
        #[arg(long)]
        qualified_name: Option<String>,

        /// search 的符号类型过滤
        #[arg(long)]
        kind: Option<String>,

        /// search/topological_order 的结果限制
        #[arg(long, default_value_t = 20)]
        limit: u32,

        /// call_chain_down/detect_cycles 的最大深度
        #[arg(long, default_value_t = 10)]
        max_depth: u32,
    },

    /// 列出当前 UID 的 workspace（无参数 RPC）
    List,

    /// 查询 workspace 和 snapshot 状态
    Status {
        /// workspace instance ID
        workspace_id: String,
    },

    /// 检查 daemon 健康状态（无参数 RPC）
    Health,

    /// 查询 registry DB schema 版本（无参数 RPC）
    SchemaVersion,

    /// 查看 daemon 模式（本地处理，不走 RPC）
    Mode {
        /// 设置 daemon 模式（仅打印提示，实际需设置环境变量 CW_DAEMON_MODE）
        #[arg(long, value_enum)]
        set: Option<ModeValue>,
    },

    /// 注册当前 UID 的 workspace
    Register {
        /// workspace 根路径
        root: String,
        /// git remote URL
        #[arg(long, default_value = "")]
        git_remote: String,
        /// git head commit SHA
        #[arg(long, default_value = "")]
        git_head: String,
        /// toolchain fingerprint
        #[arg(long, default_value = "")]
        toolchain: String,
    },

    /// 备份 registry DB
    Backup {
        /// 备份输出路径
        #[arg(long)]
        output: String,
    },

    /// 从备份恢复 registry DB
    Restore {
        /// 备份文件路径
        #[arg(long)]
        from: String,
    },

    /// GC CAS 存储（清理未引用 content）
    GcCas {
        /// workspace instance ID
        workspace_id: String,
        /// 清理 grace_days 天前的未引用 content
        #[arg(long, default_value_t = 7)]
        grace_days: u32,
    },

    /// GC 快照（保留最近 N 个）
    GcSnapshots {
        /// 每个 workspace 保留的快照数量
        #[arg(long, default_value_t = 3)]
        keep_last: u32,
    },

    /// 查询 daemon 内 SnapshotCache 统计
    SnapshotStats,

    /// 列出 daemon 已知的所有 workspace snapshot
    SnapshotList,

    /// 驱逐指定 workspace 的 snapshot 缓存
    SnapshotEvict {
        /// workspace instance ID
        workspace_id: String,
    },

    /// 容器挂载映射管理
    Mount {
        #[command(subcommand)]
        mount_action: MountAction,
    },

    /// 通用 RPC 调用（method + JSON params，用于 task.* 等任意 daemon 方法）
    Rpc {
        /// daemon method 名（如 task.create / task.claim / task.status）
        method: String,
        /// JSON 参数对象（如 {"title":"..."}）
        #[arg(default_value = "{}")]
        params: String,
    },

    /// 发布已刷新 DB 为共享 snapshot（含 SCM_RIGHTS FD 传递，Unix-only）
    Publish {
        /// workspace instance ID
        workspace_id: String,
        /// 本地 DB 路径（将作为 FD 传给 daemon）
        db_path: String,
        /// build context hash（可选）
        #[arg(long, default_value = "")]
        build_context: String,
        /// 跳过 WAL checkpoint（默认执行 PRAGMA busy_timeout=5000; wal_checkpoint(PASSIVE)，C4/S8 统一）
        #[arg(long)]
        skip_checkpoint: bool,
    },
}

/// 支持的查询类型（对齐 Python daemon_commands.py 的 choices）
#[derive(Clone, Debug, clap::ValueEnum)]
enum QueryType {
    /// 统计信息
    Stats,
    /// 符号查询
    Symbol,
    /// 搜索符号
    Search,
    /// 调用者查询
    Callers,
    /// 被调用者查询
    Callees,
    /// 调用链（向下）
    CallChainDown,
    /// 拓扑排序
    TopologicalOrder,
    /// 循环检测
    DetectCycles,
}

impl QueryType {
    fn as_str(&self) -> &'static str {
        match self {
            QueryType::Stats => "stats",
            QueryType::Symbol => "symbol",
            QueryType::Search => "search",
            QueryType::Callers => "callers",
            QueryType::Callees => "callees",
            QueryType::CallChainDown => "call_chain_down",
            QueryType::TopologicalOrder => "topological_order",
            QueryType::DetectCycles => "detect_cycles",
        }
    }
}

/// daemon 模式（对齐 Python choices=["auto", "enterprise", "local"]）
#[derive(Clone, Debug, clap::ValueEnum)]
enum ModeValue {
    /// 自动选择
    Auto,
    /// 强制企业 daemon 模式
    Enterprise,
    /// 强制本地模式
    Local,
}

impl ModeValue {
    fn as_str(&self) -> &'static str {
        match self {
            ModeValue::Auto => "auto",
            ModeValue::Enterprise => "enterprise",
            ModeValue::Local => "local",
        }
    }
}

/// mount 子命令（对齐 Python daemon_commands.py mount_action）
#[derive(Clone, Debug, clap::Subcommand)]
enum MountAction {
    /// 注册/更新容器挂载映射
    Register {
        /// 容器标识（如 ubuntu_2204）
        container_id: String,
        /// 容器内路径前缀
        container_path: String,
        /// 宿主机真实路径
        host_path: String,
        /// 映射类型（bind/volume/smb）
        #[arg(long, default_value = "bind")]
        r#type: String,
    },

    /// 列出容器挂载映射
    List {
        /// 按 container_id 过滤（缺省列出全部）
        #[arg(long)]
        container_id: Option<String>,
    },

    /// 删除容器挂载映射
    Delete {
        /// 容器标识
        container_id: String,
        /// 容器内路径前缀
        container_path: String,
    },
}

fn main() {
    let cli = Cli::parse();
    match cli.command {
        Some(Commands::Ping) => run_ping(&cli.socket, cli.timeout),
        Some(Commands::Query {
            workspace_id,
            query_type,
            value,
            qualified_name,
            kind,
            limit,
            max_depth,
        }) => {
            run_query(
                &cli.socket,
                cli.timeout,
                &workspace_id,
                query_type.as_str(),
                &value,
                qualified_name.as_deref(),
                kind.as_deref(),
                Some(limit),
                Some(max_depth),
            );
        }
        Some(Commands::List) => run_simple(&cli.socket, cli.timeout, "list", None),
        Some(Commands::Status { workspace_id }) => {
            run_simple(&cli.socket, cli.timeout, "status", Some(&workspace_id))
        }
        Some(Commands::Health) => run_simple(&cli.socket, cli.timeout, "health", None),
        Some(Commands::SchemaVersion) => {
            run_simple(&cli.socket, cli.timeout, "schema-version", None)
        }
        Some(Commands::Mode { set }) => run_mode(set),
        Some(Commands::Register {
            root,
            git_remote,
            git_head,
            toolchain,
        }) => {
            let abs_root = callwarden_core::daemon::client::to_abspath(&root);
            let params = serde_json::json!({
                "client_view_root": abs_root,
                "git_remote_url": git_remote,
                "git_head_commit_sha": git_head,
                "toolchain_fingerprint": toolchain,
            });
            run_rpc_action(&cli.socket, cli.timeout, "register", &params);
        }
        Some(Commands::Backup { output }) => {
            let abs_output = callwarden_core::daemon::client::to_abspath(&output);
            let params = serde_json::json!({ "output_path": abs_output });
            run_rpc_action(&cli.socket, cli.timeout, "backup", &params);
        }
        Some(Commands::Restore { from }) => {
            let abs_from = callwarden_core::daemon::client::to_abspath(&from);
            let params = serde_json::json!({ "source_path": abs_from });
            run_rpc_action(&cli.socket, cli.timeout, "restore", &params);
        }
        Some(Commands::GcCas {
            workspace_id,
            grace_days,
        }) => {
            let params = serde_json::json!({
                "workspace_instance_id": workspace_id,
                "grace_days": grace_days,
            });
            run_rpc_action(&cli.socket, cli.timeout, "gc-cas", &params);
        }
        Some(Commands::GcSnapshots { keep_last }) => {
            let params = serde_json::json!({ "keep_last": keep_last });
            run_rpc_action(&cli.socket, cli.timeout, "gc-snapshots", &params);
        }
        Some(Commands::SnapshotStats) => {
            run_rpc_action(&cli.socket, cli.timeout, "snapshot-stats", &serde_json::json!({}));
        }
        Some(Commands::SnapshotList) => {
            run_rpc_action(&cli.socket, cli.timeout, "snapshot-list", &serde_json::json!({}));
        }
        Some(Commands::SnapshotEvict { workspace_id }) => {
            let params = serde_json::json!({ "workspace_instance_id": workspace_id });
            run_rpc_action(&cli.socket, cli.timeout, "snapshot-evict", &params);
        }
        Some(Commands::Mount { mount_action }) => {
            run_mount(&cli.socket, cli.timeout, mount_action);
        }
        Some(Commands::Rpc { method, params }) => {
            let params_val: serde_json::Value = match serde_json::from_str(&params) {
                Ok(v) => v,
                Err(e) => {
                    eprintln!("cw-client rpc: 参数不是合法 JSON: {}", e);
                    std::process::exit(1);
                }
            };
            #[cfg(unix)]
            {
                run_rpc_unix(&cli.socket, cli.timeout, &method, &params_val, "rpc")
            }
            #[cfg(not(unix))]
            {
                run_rpc_windows(&cli.socket, cli.timeout, &method, &params_val, "rpc")
            }
        }
        Some(Commands::Publish {
            workspace_id,
            db_path,
            build_context,
            skip_checkpoint,
        }) => {
            run_publish(
                &cli.socket,
                cli.timeout,
                &workspace_id,
                &db_path,
                &build_context,
                skip_checkpoint,
            );
        }
        None => {
            Cli::parse_from(["cw-client", "--help"]);
        }
    }
}

/// 执行 ping 子命令。
fn run_ping(socket: &str, timeout_secs: u64) {
    #[cfg(unix)]
    {
        run_ping_unix(socket, timeout_secs)
    }
    #[cfg(not(unix))]
    {
        run_rpc_windows(socket, timeout_secs, "ping", &serde_json::json!({}), "ping");
    }
}

/// 执行 query 子命令。
fn run_query(
    socket: &str,
    timeout_secs: u64,
    workspace_id: &str,
    query_type: &str,
    value: &str,
    qualified_name: Option<&str>,
    kind: Option<&str>,
    limit: Option<u32>,
    max_depth: Option<u32>,
) {
    // 构建查询参数（跨平台逻辑）
    let (method, params) =
        match callwarden_core::daemon::client::build_query_request(
            workspace_id,
            query_type,
            value,
            qualified_name,
            kind,
            limit,
            max_depth,
        ) {
            Ok((m, p)) => (m, p),
            Err(e) => {
                eprintln!("cw-client query: {}", e);
                std::process::exit(1);
            }
        };

    #[cfg(unix)]
    {
        run_query_unix(socket, timeout_secs, &method, &params)
    }
    #[cfg(not(unix))]
    {
        run_rpc_windows(socket, timeout_secs, &method, &params, "query");
    }
}

/// 执行简单 RPC 子命令（list/status/health/schema-version）。
fn run_simple(
    socket: &str,
    timeout_secs: u64,
    action: &str,
    workspace_id: Option<&str>,
) {
    // 构建参数（跨平台逻辑）
    let (method, params) =
        match callwarden_core::daemon::client::build_simple_request(action, workspace_id) {
            Ok((m, p)) => (m, p),
            Err(e) => {
                eprintln!("cw-client {}: {}", action, e);
                std::process::exit(1);
            }
        };

    #[cfg(unix)]
    {
        run_rpc_unix(socket, timeout_secs, &method, &params, action)
    }
    #[cfg(not(unix))]
    {
        run_rpc_windows(socket, timeout_secs, &method, &params, action)
    }
}

/// 执行 mode 子命令（本地处理，不走 RPC）。
///
/// 对齐 Python cli/daemon_commands.py:run_daemon_command 的 mode 分支：
/// - --set 仅打印提示（实际需设置环境变量 CW_DAEMON_MODE）
/// - 无 --set 时输出当前模式信息
fn run_mode(set: Option<ModeValue>) {
    // 读取环境变量 CW_DAEMON_MODE（对齐 Python get_daemon_mode）
    let current_mode = std::env::var("CW_DAEMON_MODE").unwrap_or_else(|_| "auto".to_string());
    let mode = set.as_ref().map(|v| v.as_str().to_string()).unwrap_or_else(|| current_mode.clone());

    // 输出 JSON（对齐 Python _print_json 输出格式）
    let info = serde_json::json!({
        "mode": mode,
        "available": true,  // 简化：Rust 端不做完整 is_daemon_available 检查
        "required": false,  // 简化：Rust 端不做完整 is_daemon_required 检查
        "socket": std::env::var("CW_DAEMON_SOCKET")
            .unwrap_or_else(|_| "/tmp/callwarden_daemon.sock".to_string()),
    });
    let pretty = serde_json::to_string_pretty(&info).unwrap_or_else(|_| format!("{}", info));
    println!("{}", pretty);

    if let Some(v) = set {
        // 对齐 Python：--set 时打印提示
        eprintln!("请设置环境变量 CW_DAEMON_MODE={}", v.as_str());
    }
}

/// 执行剩余 RPC 子命令（register/backup/restore/gc/snapshot）。
///
/// 通用参数构建 + UDS/Pipe 调用，复用 build_rpc_request 做 method 映射。
fn run_rpc_action(
    socket: &str,
    timeout_secs: u64,
    action: &str,
    params: &serde_json::Value,
) {
    // 构建 method 和参数（跨平台逻辑）
    let params_json = serde_json::to_string(params).unwrap_or_else(|_| "{}".to_string());
    let (method, final_params) =
        match callwarden_core::daemon::client::build_rpc_request(action, &params_json) {
            Ok((m, p)) => (m, p),
            Err(e) => {
                eprintln!("cw-client {}: {}", action, e);
                std::process::exit(1);
            }
        };

    #[cfg(unix)]
    {
        run_rpc_unix(socket, timeout_secs, &method, &final_params, action)
    }
    #[cfg(not(unix))]
    {
        run_rpc_windows(socket, timeout_secs, &method, &final_params, action)
    }
}

/// 执行 mount 子命令组（mount register/list/delete）。
fn run_mount(
    socket: &str,
    timeout_secs: u64,
    mount_action: MountAction,
) {
    let (action, params) = match mount_action {
        MountAction::Register {
            container_id,
            container_path,
            host_path,
            r#type,
        } => {
            let abs_host = callwarden_core::daemon::client::to_abspath(&host_path);
            let p = serde_json::json!({
                "container_id": container_id,
                "container_path": container_path,
                "host_path": abs_host,
                "mapping_type": r#type,
            });
            ("mount-register", p)
        }
        MountAction::List { container_id } => {
            let mut p = serde_json::Map::new();
            if let Some(cid) = container_id {
                p.insert("container_id".to_string(), serde_json::Value::String(cid));
            }
            ("mount-list", serde_json::Value::Object(p))
        }
        MountAction::Delete {
            container_id,
            container_path,
        } => {
            let p = serde_json::json!({
                "container_id": container_id,
                "container_path": container_path,
            });
            ("mount-delete", p)
        }
    };
    run_rpc_action(socket, timeout_secs, action, &params);
}

/// 执行 publish 子命令（snapshot.publish + SCM_RIGHTS FD 传递）。
///
/// 对齐 Python `UnixDaemonRpcClient.publish_snapshot`：
/// 1. WAL checkpoint（可选，默认执行 PRAGMA busy_timeout=5000; wal_checkpoint(PASSIVE)，C4/S8 统一）
/// 2. 通过 SCM_RIGHTS 将 db_path 的只读 FD 传给 daemon
///
/// Windows 上：UDS 不可用，仅输出参数构建结果（用于验证）
fn run_publish(
    socket: &str,
    timeout_secs: u64,
    workspace_id: &str,
    db_path: &str,
    build_context: &str,
    skip_checkpoint: bool,
) {
    let abs_db_path = callwarden_core::daemon::client::to_abspath(db_path);

    // 构建参数（跨平台逻辑，Windows 也可验证）
    let (method, params) = callwarden_core::daemon::client::build_publish_params(
        workspace_id,
        build_context,
    );

    #[cfg(unix)]
    {
        run_publish_unix(
            socket,
            timeout_secs,
            &method,
            &params,
            workspace_id,
            &abs_db_path,
            build_context,
            skip_checkpoint,
        )
    }
    #[cfg(not(unix))]
    {
        let _ = skip_checkpoint; // Windows 分支不执行本地 WAL checkpoint（服务端自行处理）
        // Windows：无 SCM_RIGHTS FD 传递，改用 db_path 参数（服务端 publish handler
        // 在无 FD 时回退到 db_path 参数并自行 WAL checkpoint）。
        let mut params_with_path = params.clone();
        if let Some(obj) = params_with_path.as_object_mut() {
            obj.insert("db_path".to_string(), serde_json::Value::String(abs_db_path.clone()));
        }
        run_rpc_windows(socket, timeout_secs, &method, &params_with_path, "publish")
    }
}

/// 执行 Windows Named Pipe RPC（cw-client Windows 分支统一入口）。
///
/// 对齐 cw_daemon `health-check` 的管道客户端实现与 daemon 协议：
/// 1. `WaitNamedPipeW` 等待 daemon 管道实例可用
/// 2. `CreateFileW` 打开 `\\.\pipe\callwarden-<sid>`
/// 3. 发送 [4 字节大端长度][JSON 请求] 帧
/// 4. 读取 [4 字节大端长度][JSON 响应] 帧
/// 5. `parse_rpc_response` 解析并打印结果
#[cfg(not(unix))]
fn run_rpc_windows(
    socket: &str,
    timeout_secs: u64,
    method: &str,
    params: &serde_json::Value,
    action: &str,
) {
    use std::os::windows::ffi::OsStrExt;
    use std::time::Duration;
    use windows_sys::Win32::Foundation::{
        CloseHandle, GetLastError, GENERIC_READ, GENERIC_WRITE, HANDLE, INVALID_HANDLE_VALUE,
    };
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, ReadFile, WriteFile, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING,
    };
    use windows_sys::Win32::System::Pipes::WaitNamedPipeW;

    // ERROR_ACCESS_DENIED（Named Pipe SDDL 拒绝其他用户）
    const ERROR_ACCESS_DENIED: u32 = 5;

    let deadline = std::time::Instant::now() + Duration::from_secs(timeout_secs);

    // 1. 构建请求帧：{method, params} → [4 字节大端长度][JSON]
    let request = callwarden_core::daemon::client::build_request(method, params.clone());
    let body = serde_json::to_vec(&request).unwrap_or_else(|e| {
        eprintln!("cw-client {}: 请求序列化失败: {}", action, e);
        std::process::exit(1);
    });
    let mut frame = Vec::with_capacity(4 + body.len());
    frame.extend_from_slice(&(body.len() as u32).to_be_bytes());
    frame.extend_from_slice(&body);

    // 2. 宽字符管道名
    let wide: Vec<u16> = std::path::Path::new(socket)
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect();

    // 3. WaitNamedPipeW 等待管道实例可用（daemon 未启动时管道不存在）
    loop {
        if unsafe { WaitNamedPipeW(wide.as_ptr(), 5000) } != 0 {
            break;
        }
        let last_error = unsafe { GetLastError() };
        // P1 ACL：SDDL 拒绝其他用户时立即上报真实 Win32 错误码（ERROR_ACCESS_DENIED=5）
        if last_error == ERROR_ACCESS_DENIED {
            eprintln!(
                "cw-client {}: Named Pipe 访问被拒绝 (Win32 error {}) 用户无权限连接管道: {}",
                action, last_error, socket
            );
            std::process::exit(1);
        }
        if std::time::Instant::now() >= deadline {
            eprintln!(
                "cw-client {}: daemon 未响应 (timeout {}s, last Win32 error {}): {}",
                action, timeout_secs, last_error, socket
            );
            std::process::exit(1);
        }
        std::thread::sleep(Duration::from_millis(200));
    }

    // 4. CreateFileW 打开管道
    let handle: HANDLE = unsafe {
        CreateFileW(
            wide.as_ptr(),
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            std::ptr::null(),
            OPEN_EXISTING,
            0,
            std::ptr::null_mut(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        let last_error = unsafe { GetLastError() };
        eprintln!(
            "cw-client {}: CreateFileW 失败 (Win32 error {}): {}",
            action, last_error, socket
        );
        std::process::exit(1);
    }

    // 5. 发送请求帧
    let mut written: u32 = 0;
    let ok_write = unsafe {
        WriteFile(
            handle,
            frame.as_ptr(),
            frame.len() as u32,
            &mut written,
            std::ptr::null_mut(),
        )
    };
    if ok_write == 0 {
        unsafe { CloseHandle(handle) };
        eprintln!("cw-client {}: WriteFile 失败: {}", action, socket);
        std::process::exit(1);
    }

    // 6. 读取 4 字节长度前缀（ReadFile 可能短读，循环补足）
    let mut header = [0u8; 4];
    let mut header_read: u32 = 0;
    while header_read < 4 {
        let mut n: u32 = 0;
        let ptr = unsafe { header.as_mut_ptr().add(header_read as usize) };
        let ok = unsafe {
            ReadFile(
                handle,
                ptr,
                4 - header_read,
                &mut n,
                std::ptr::null_mut(),
            )
        };
        if ok == 0 || n == 0 {
            unsafe { CloseHandle(handle) };
            eprintln!("cw-client {}: 读取响应长度失败: {}", action, socket);
            std::process::exit(1);
        }
        header_read += n;
    }
    let resp_len = u32::from_be_bytes(header) as usize;
    let mut resp = vec![0u8; resp_len];
    let mut resp_read: u32 = 0;
    while resp_read < resp_len as u32 {
        let mut n: u32 = 0;
        let ptr = unsafe { resp.as_mut_ptr().add(resp_read as usize) };
        let ok = unsafe {
            ReadFile(
                handle,
                ptr,
                (resp_len as u32) - resp_read,
                &mut n,
                std::ptr::null_mut(),
            )
        };
        if ok == 0 || n == 0 {
            unsafe { CloseHandle(handle) };
            eprintln!("cw-client {}: 读取响应体失败: {}", action, socket);
            std::process::exit(1);
        }
        resp_read += n;
    }
    unsafe { CloseHandle(handle) };

    // 7. 解析并打印结果
    let parsed: serde_json::Value = match serde_json::from_slice(&resp) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("cw-client {}: 响应 JSON 解析失败: {}: {}", action, e, socket);
            std::process::exit(1);
        }
    };
    match callwarden_core::daemon::client::parse_rpc_response(&parsed) {
        Ok(result) => {
            let pretty = serde_json::to_string_pretty(&result).unwrap_or_else(|_| {
                format!("{}", result)
            });
            println!("{}", pretty);
            std::process::exit(0);
        }
        Err(e) => {
            eprintln!("cw-client {}: {}", action, e);
            std::process::exit(1);
        }
    }
}

#[cfg(unix)]
fn run_ping_unix(socket: &str, timeout_secs: u64) {
    use callwarden_core::daemon::client::unix::UnixDaemonRpcClient;
    use std::time::Duration;

    let client = UnixDaemonRpcClient::new(socket).with_timeout(Duration::from_secs(timeout_secs));
    match client.ping() {
        Ok(result) => {
            let pretty = serde_json::to_string_pretty(&result).unwrap_or_else(|_| {
                format!("{}", result)
            });
            println!("{}", pretty);
            std::process::exit(0);
        }
        Err(e) => {
            eprintln!("cw-client ping: {}", e);
            std::process::exit(1);
        }
    }
}

#[cfg(unix)]
fn run_query_unix(
    socket: &str,
    timeout_secs: u64,
    method: &str,
    params: &serde_json::Value,
) {
    use callwarden_core::daemon::client::unix::UnixDaemonRpcClient;
    use std::time::Duration;

    let client = UnixDaemonRpcClient::new(socket).with_timeout(Duration::from_secs(timeout_secs));
    match client.call(method, params.clone()) {
        Ok(result) => {
            let pretty = serde_json::to_string_pretty(&result).unwrap_or_else(|_| {
                format!("{}", result)
            });
            println!("{}", pretty);
            std::process::exit(0);
        }
        Err(e) => {
            eprintln!("cw-client query: {}", e);
            std::process::exit(1);
        }
    }
}

#[cfg(unix)]
fn run_rpc_unix(
    socket: &str,
    timeout_secs: u64,
    method: &str,
    params: &serde_json::Value,
    action: &str,
) {
    use callwarden_core::daemon::client::unix::UnixDaemonRpcClient;
    use std::time::Duration;

    let client = UnixDaemonRpcClient::new(socket).with_timeout(Duration::from_secs(timeout_secs));
    match client.call(method, params.clone()) {
        Ok(result) => {
            let pretty = serde_json::to_string_pretty(&result).unwrap_or_else(|_| {
                format!("{}", result)
            });
            println!("{}", pretty);
            std::process::exit(0);
        }
        Err(e) => {
            eprintln!("cw-client {}: {}", action, e);
            std::process::exit(1);
        }
    }
}

#[cfg(unix)]
fn run_publish_unix(
    socket: &str,
    timeout_secs: u64,
    method: &str,
    params: &serde_json::Value,
    workspace_id: &str,
    db_path: &str,
    _build_context: &str,
    skip_checkpoint: bool,
) {
    use callwarden_core::daemon::client::unix::UnixDaemonRpcClient;
    use std::time::Duration;

    // 1. WAL checkpoint（可选）
    if !skip_checkpoint {
        match wal_checkpoint(db_path) {
            Ok(()) => eprintln!("✓ WAL checkpoint 完成: {}", db_path),
            Err(e) => {
                eprintln!("cw-client publish: WAL checkpoint 失败: {}", e);
                std::process::exit(1);
            }
        }
    }

    // 2. 通过 SCM_RIGHTS 发布 snapshot
    let client = UnixDaemonRpcClient::new(socket).with_timeout(Duration::from_secs(timeout_secs));
    let _ = method; // method 固定为 "snapshot.publish"
    let _ = params;  // publish_snapshot 内部会重新构建参数
    match client.publish_snapshot(workspace_id, db_path, _build_context) {
        Ok(result) => {
            let pretty = serde_json::to_string_pretty(&result).unwrap_or_else(|_| {
                format!("{}", result)
            });
            println!("{}", pretty);
            std::process::exit(0);
        }
        Err(e) => {
            eprintln!("cw-client publish: {}", e);
            std::process::exit(1);
        }
    }
}

#[cfg(unix)]
fn wal_checkpoint(db_path: &str) -> Result<(), Box<dyn std::error::Error>> {
    use rusqlite::Connection;
    let conn = Connection::open(db_path)?;
    // C4/S8 统一：PASSIVE 双保险——busy_timeout 等待后 PASSIVE checkpoint，
    // busy 时不 fail-fast，剩余 WAL 页由 daemon/内核后续 PASSIVE checkpoint 兜底。
    conn.execute_batch("PRAGMA busy_timeout=5000; PRAGMA wal_checkpoint(PASSIVE);")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_cli_parse_ping() {
        let cli = Cli::parse_from(["cw-client", "ping"]);
        assert!(matches!(cli.command, Some(Commands::Ping)));
    }

    #[test]
    fn test_cli_parse_with_socket() {
        let cli = Cli::parse_from(["cw-client", "--socket", "/tmp/custom.sock", "ping"]);
        assert_eq!(cli.socket, "/tmp/custom.sock");
        assert!(matches!(cli.command, Some(Commands::Ping)));
    }

    #[test]
    fn test_cli_parse_with_timeout() {
        let cli = Cli::parse_from(["cw-client", "--timeout", "60", "ping"]);
        assert_eq!(cli.timeout, 60);
        assert!(matches!(cli.command, Some(Commands::Ping)));
    }

    #[test]
    fn test_cli_default_socket() {
        let cli = Cli::parse_from(["cw-client", "ping"]);
        assert_eq!(cli.socket, "/tmp/callwarden_daemon.sock");
    }

    #[test]
    fn test_cli_default_timeout() {
        let cli = Cli::parse_from(["cw-client", "ping"]);
        assert_eq!(cli.timeout, 30);
    }

    #[test]
    fn test_cli_global_args_before_subcommand() {
        let cli = Cli::parse_from(["cw-client", "--socket", "/tmp/x.sock", "ping"]);
        assert_eq!(cli.socket, "/tmp/x.sock");
    }

    #[test]
    fn test_cli_no_subcommand() {
        let cli = Cli::parse_from(["cw-client"]);
        assert!(cli.command.is_none());
    }

    // ===== Phase 5-2 Slice 2: query 子命令测试 =====

    #[test]
    fn test_cli_parse_query_stats() {
        let cli = Cli::parse_from(["cw-client", "query", "ws-1", "stats"]);
        match cli.command {
            Some(Commands::Query {
                workspace_id,
                query_type,
                value,
                ..
            }) => {
                assert_eq!(workspace_id, "ws-1");
                assert!(matches!(query_type, QueryType::Stats));
                assert_eq!(value, "");
            }
            _ => panic!("期望 Query 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_query_symbol_with_value() {
        let cli = Cli::parse_from([
            "cw-client",
            "query",
            "ws-1",
            "symbol",
            "module::func",
        ]);
        match cli.command {
            Some(Commands::Query {
                workspace_id,
                query_type,
                value,
                ..
            }) => {
                assert_eq!(workspace_id, "ws-1");
                assert!(matches!(query_type, QueryType::Symbol));
                assert_eq!(value, "module::func");
            }
            _ => panic!("期望 Query 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_query_search_with_options() {
        let cli = Cli::parse_from([
            "cw-client",
            "query",
            "ws-1",
            "search",
            "foo",
            "--kind",
            "function",
            "--limit",
            "50",
        ]);
        match cli.command {
            Some(Commands::Query {
                value,
                kind,
                limit,
                ..
            }) => {
                assert_eq!(value, "foo");
                assert_eq!(kind.as_deref(), Some("function"));
                assert_eq!(limit, 50);
            }
            _ => panic!("期望 Query 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_query_callers_with_qualified_name() {
        let cli = Cli::parse_from([
            "cw-client",
            "query",
            "ws-1",
            "callers",
            "callee_func",
            "--qualified-name",
            "module::caller",
        ]);
        match cli.command {
            Some(Commands::Query {
                value,
                qualified_name,
                ..
            }) => {
                assert_eq!(value, "callee_func");
                assert_eq!(qualified_name.as_deref(), Some("module::caller"));
            }
            _ => panic!("期望 Query 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_query_call_chain_down_with_max_depth() {
        let cli = Cli::parse_from([
            "cw-client",
            "query",
            "ws-1",
            "call-chain-down",
            "module::func",
            "--max-depth",
            "5",
        ]);
        match cli.command {
            Some(Commands::Query {
                query_type,
                value,
                max_depth,
                ..
            }) => {
                assert!(matches!(query_type, QueryType::CallChainDown));
                assert_eq!(value, "module::func");
                assert_eq!(max_depth, 5);
            }
            _ => panic!("期望 Query 子命令"),
        }
    }

    #[test]
    fn test_query_type_as_str() {
        assert_eq!(QueryType::Stats.as_str(), "stats");
        assert_eq!(QueryType::Symbol.as_str(), "symbol");
        assert_eq!(QueryType::Search.as_str(), "search");
        assert_eq!(QueryType::Callers.as_str(), "callers");
        assert_eq!(QueryType::Callees.as_str(), "callees");
        assert_eq!(QueryType::CallChainDown.as_str(), "call_chain_down");
        assert_eq!(QueryType::TopologicalOrder.as_str(), "topological_order");
        assert_eq!(QueryType::DetectCycles.as_str(), "detect_cycles");
    }

    // ===== Phase 5-2 Slice 3: 核心子命令测试 =====

    #[test]
    fn test_cli_parse_list() {
        let cli = Cli::parse_from(["cw-client", "list"]);
        assert!(matches!(cli.command, Some(Commands::List)));
    }

    #[test]
    fn test_cli_parse_status_with_workspace_id() {
        let cli = Cli::parse_from(["cw-client", "status", "ws-abc"]);
        match cli.command {
            Some(Commands::Status { workspace_id }) => {
                assert_eq!(workspace_id, "ws-abc");
            }
            _ => panic!("期望 Status 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_health() {
        let cli = Cli::parse_from(["cw-client", "health"]);
        assert!(matches!(cli.command, Some(Commands::Health)));
    }

    #[test]
    fn test_cli_parse_schema_version() {
        let cli = Cli::parse_from(["cw-client", "schema-version"]);
        assert!(matches!(cli.command, Some(Commands::SchemaVersion)));
    }

    #[test]
    fn test_cli_parse_mode_no_set() {
        let cli = Cli::parse_from(["cw-client", "mode"]);
        match cli.command {
            Some(Commands::Mode { set }) => assert!(set.is_none()),
            _ => panic!("期望 Mode 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_mode_with_set_enterprise() {
        let cli = Cli::parse_from(["cw-client", "mode", "--set", "enterprise"]);
        match cli.command {
            Some(Commands::Mode { set }) => {
                assert!(matches!(set, Some(ModeValue::Enterprise)));
            }
            _ => panic!("期望 Mode 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_mode_with_set_local() {
        let cli = Cli::parse_from(["cw-client", "mode", "--set", "local"]);
        match cli.command {
            Some(Commands::Mode { set }) => {
                assert!(matches!(set, Some(ModeValue::Local)));
            }
            _ => panic!("期望 Mode 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_mode_with_set_auto() {
        let cli = Cli::parse_from(["cw-client", "mode", "--set", "auto"]);
        match cli.command {
            Some(Commands::Mode { set }) => {
                assert!(matches!(set, Some(ModeValue::Auto)));
            }
            _ => panic!("期望 Mode 子命令"),
        }
    }

    #[test]
    fn test_mode_value_as_str() {
        assert_eq!(ModeValue::Auto.as_str(), "auto");
        assert_eq!(ModeValue::Enterprise.as_str(), "enterprise");
        assert_eq!(ModeValue::Local.as_str(), "local");
    }

    #[test]
    fn test_cli_parse_status_with_socket_global() {
        // 全局参数 --socket 在子命令之前
        let cli = Cli::parse_from([
            "cw-client",
            "--socket",
            "/tmp/custom.sock",
            "status",
            "ws-1",
        ]);
        assert_eq!(cli.socket, "/tmp/custom.sock");
        match cli.command {
            Some(Commands::Status { workspace_id }) => assert_eq!(workspace_id, "ws-1"),
            _ => panic!("期望 Status 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_list_with_timeout_global() {
        let cli = Cli::parse_from(["cw-client", "--timeout", "60", "list"]);
        assert_eq!(cli.timeout, 60);
        assert!(matches!(cli.command, Some(Commands::List)));
    }

    // ===== Phase 5-2 Slice 5: 剩余子命令测试 =====

    #[test]
    fn test_cli_parse_register() {
        let cli = Cli::parse_from([
            "cw-client",
            "register",
            "/tmp/project",
            "--git-remote",
            "https://github.com/x/y.git",
            "--git-head",
            "abc123",
            "--toolchain",
            "gcc-11",
        ]);
        match cli.command {
            Some(Commands::Register {
                root,
                git_remote,
                git_head,
                toolchain,
            }) => {
                assert_eq!(root, "/tmp/project");
                assert_eq!(git_remote, "https://github.com/x/y.git");
                assert_eq!(git_head, "abc123");
                assert_eq!(toolchain, "gcc-11");
            }
            _ => panic!("期望 Register 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_register_defaults() {
        let cli = Cli::parse_from(["cw-client", "register", "/tmp/p"]);
        match cli.command {
            Some(Commands::Register {
                root,
                git_remote,
                git_head,
                toolchain,
            }) => {
                assert_eq!(root, "/tmp/p");
                assert_eq!(git_remote, "");
                assert_eq!(git_head, "");
                assert_eq!(toolchain, "");
            }
            _ => panic!("期望 Register 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_backup() {
        let cli = Cli::parse_from(["cw-client", "backup", "--output", "/tmp/bak.db"]);
        match cli.command {
            Some(Commands::Backup { output }) => {
                assert_eq!(output, "/tmp/bak.db");
            }
            _ => panic!("期望 Backup 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_restore() {
        let cli = Cli::parse_from(["cw-client", "restore", "--from", "/tmp/bak.db"]);
        match cli.command {
            Some(Commands::Restore { from }) => {
                assert_eq!(from, "/tmp/bak.db");
            }
            _ => panic!("期望 Restore 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_gc_cas() {
        let cli = Cli::parse_from(["cw-client", "gc-cas", "ws-1", "--grace-days", "14"]);
        match cli.command {
            Some(Commands::GcCas {
                workspace_id,
                grace_days,
            }) => {
                assert_eq!(workspace_id, "ws-1");
                assert_eq!(grace_days, 14);
            }
            _ => panic!("期望 GcCas 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_gc_cas_default_grace_days() {
        let cli = Cli::parse_from(["cw-client", "gc-cas", "ws-1"]);
        match cli.command {
            Some(Commands::GcCas { grace_days, .. }) => {
                assert_eq!(grace_days, 7);
            }
            _ => panic!("期望 GcCas 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_gc_snapshots() {
        let cli = Cli::parse_from(["cw-client", "gc-snapshots", "--keep-last", "5"]);
        match cli.command {
            Some(Commands::GcSnapshots { keep_last }) => {
                assert_eq!(keep_last, 5);
            }
            _ => panic!("期望 GcSnapshots 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_gc_snapshots_default() {
        let cli = Cli::parse_from(["cw-client", "gc-snapshots"]);
        match cli.command {
            Some(Commands::GcSnapshots { keep_last }) => {
                assert_eq!(keep_last, 3);
            }
            _ => panic!("期望 GcSnapshots 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_snapshot_stats() {
        let cli = Cli::parse_from(["cw-client", "snapshot-stats"]);
        assert!(matches!(cli.command, Some(Commands::SnapshotStats)));
    }

    #[test]
    fn test_cli_parse_snapshot_list() {
        let cli = Cli::parse_from(["cw-client", "snapshot-list"]);
        assert!(matches!(cli.command, Some(Commands::SnapshotList)));
    }

    #[test]
    fn test_cli_parse_snapshot_evict() {
        let cli = Cli::parse_from(["cw-client", "snapshot-evict", "ws-1"]);
        match cli.command {
            Some(Commands::SnapshotEvict { workspace_id }) => {
                assert_eq!(workspace_id, "ws-1");
            }
            _ => panic!("期望 SnapshotEvict 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_mount_register() {
        let cli = Cli::parse_from([
            "cw-client",
            "mount",
            "register",
            "ubuntu_2204",
            "/mnt/code",
            "/tmp/host-code",
        ]);
        match cli.command {
            Some(Commands::Mount { mount_action }) => {
                match mount_action {
                    MountAction::Register {
                        container_id,
                        container_path,
                        host_path,
                        r#type,
                    } => {
                        assert_eq!(container_id, "ubuntu_2204");
                        assert_eq!(container_path, "/mnt/code");
                        assert_eq!(host_path, "/tmp/host-code");
                        assert_eq!(r#type, "bind"); // 默认 bind
                    }
                    _ => panic!("期望 MountAction::Register"),
                }
            }
            _ => panic!("期望 Mount 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_mount_register_with_type() {
        let cli = Cli::parse_from([
            "cw-client",
            "mount",
            "register",
            "ubuntu",
            "/mnt",
            "/tmp",
            "--type",
            "volume",
        ]);
        match cli.command {
            Some(Commands::Mount { mount_action }) => {
                match mount_action {
                    MountAction::Register { r#type, .. } => {
                        assert_eq!(r#type, "volume");
                    }
                    _ => panic!("期望 MountAction::Register"),
                }
            }
            _ => panic!("期望 Mount 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_mount_list_no_filter() {
        let cli = Cli::parse_from(["cw-client", "mount", "list"]);
        match cli.command {
            Some(Commands::Mount { mount_action }) => {
                match mount_action {
                    MountAction::List { container_id } => {
                        assert!(container_id.is_none());
                    }
                    _ => panic!("期望 MountAction::List"),
                }
            }
            _ => panic!("期望 Mount 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_mount_list_with_container_id() {
        let cli = Cli::parse_from([
            "cw-client",
            "mount",
            "list",
            "--container-id",
            "ubuntu",
        ]);
        match cli.command {
            Some(Commands::Mount { mount_action }) => {
                match mount_action {
                    MountAction::List { container_id } => {
                        assert_eq!(container_id.as_deref(), Some("ubuntu"));
                    }
                    _ => panic!("期望 MountAction::List"),
                }
            }
            _ => panic!("期望 Mount 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_mount_delete() {
        let cli = Cli::parse_from(["cw-client", "mount", "delete", "ubuntu", "/mnt"]);
        match cli.command {
            Some(Commands::Mount { mount_action }) => {
                match mount_action {
                    MountAction::Delete {
                        container_id,
                        container_path,
                    } => {
                        assert_eq!(container_id, "ubuntu");
                        assert_eq!(container_path, "/mnt");
                    }
                    _ => panic!("期望 MountAction::Delete"),
                }
            }
            _ => panic!("期望 Mount 子命令"),
        }
    }

    // ===== Phase 5-2 Slice 4: publish 子命令测试 =====

    #[test]
    fn test_cli_parse_publish_basic() {
        let cli = Cli::parse_from(["cw-client", "publish", "ws-1", "/tmp/db.sqlite"]);
        match cli.command {
            Some(Commands::Publish {
                workspace_id,
                db_path,
                build_context,
                skip_checkpoint,
            }) => {
                assert_eq!(workspace_id, "ws-1");
                assert_eq!(db_path, "/tmp/db.sqlite");
                assert_eq!(build_context, "");
                assert!(!skip_checkpoint);
            }
            _ => panic!("期望 Publish 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_publish_with_build_context() {
        let cli = Cli::parse_from([
            "cw-client",
            "publish",
            "ws-1",
            "/tmp/db.sqlite",
            "--build-context",
            "ctx-hash-abc",
        ]);
        match cli.command {
            Some(Commands::Publish { build_context, .. }) => {
                assert_eq!(build_context, "ctx-hash-abc");
            }
            _ => panic!("期望 Publish 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_publish_skip_checkpoint() {
        let cli = Cli::parse_from([
            "cw-client",
            "publish",
            "ws-1",
            "/tmp/db.sqlite",
            "--skip-checkpoint",
        ]);
        match cli.command {
            Some(Commands::Publish { skip_checkpoint, .. }) => {
                assert!(skip_checkpoint);
            }
            _ => panic!("期望 Publish 子命令"),
        }
    }

    #[test]
    fn test_cli_parse_publish_with_all_options() {
        let cli = Cli::parse_from([
            "cw-client",
            "--socket",
            "/tmp/custom.sock",
            "publish",
            "ws-1",
            "/tmp/db.sqlite",
            "--build-context",
            "ctx",
            "--skip-checkpoint",
        ]);
        assert_eq!(cli.socket, "/tmp/custom.sock");
        match cli.command {
            Some(Commands::Publish {
                workspace_id,
                db_path,
                build_context,
                skip_checkpoint,
            }) => {
                assert_eq!(workspace_id, "ws-1");
                assert_eq!(db_path, "/tmp/db.sqlite");
                assert_eq!(build_context, "ctx");
                assert!(skip_checkpoint);
            }
            _ => panic!("期望 Publish 子命令"),
        }
    }
}
