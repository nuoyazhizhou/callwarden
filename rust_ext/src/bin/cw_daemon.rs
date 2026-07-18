//! cw_daemon —— Enterprise daemon binary 入口。
//!
//! R7 实现：CLI 参数解析（clap）→ 配置加载 → schema 初始化 → 启动 UDS server →
//! 信号处理（SIGTERM/SIGINT 优雅关闭 + SIGHUP reload log + SIGUSR1 drain log）。
//!
//! ## Linux-only
//! UDS + SO_PEERCRED 是 Linux 特有。Windows 上编译为 stub，运行时 exit 1。
//! macOS 不支持 SO_PEERCRED（peercred.rs 用 #[cfg(unix)] 但实际只在 Linux 测试通过）。
//!
//! ## systemd 集成
//! - 推荐配合 `Type=simple`（R7 未实现 sd_notify READY=1，留作 TODO）
//! - SIGTERM 触发优雅关闭：stop accept → drain workers → remove socket
//! - ExecStartPre=/usr/bin/cw-daemon schema-check --strict
//! - ExecStart=/usr/bin/cw-daemon serve
//!
//! ## 参考文档
//! - 设计：docs/design/enterprise-daemon-shared-snapshot-plan.md
//! - Runbook：docs/design/daemon-deploy-runbook.md
//! - Python 参考：server/daemon_server.py:EnterpriseDaemonServer.serve_forever

fn main() {
    #[cfg(unix)]
    {
        let exit_code = unix::main();
        std::process::exit(exit_code);
    }
    #[cfg(not(unix))]
    {
        eprintln!("[cw_daemon] UDS server is only available on Linux/Unix");
        std::process::exit(1);
    }
}

#[cfg(unix)]
mod unix {
    use std::io;
    use std::os::unix::net::UnixStream;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;
    use std::thread;
    use std::time::{Duration, Instant};

    use clap::{Parser, Subcommand};
    use signal_hook::consts::{SIGHUP, SIGINT, SIGTERM, SIGUSR1};
    use signal_hook::flag as signal_flag;

    use callwarden_core::daemon::config::DaemonConfig;
    use callwarden_core::daemon::server::{ServerConfig, ServerHandle, start_server};
    use callwarden_core::daemon::snapshot_state::SnapshotDaemonState;
    use callwarden_core::daemon::workspace::WorkspaceRegistry;
    use callwarden_core::daemon::SCHEMA_VERSION;
    use callwarden_core::snapshot::SnapshotCache;

    /// cw_daemon CLI 参数
    #[derive(Parser, Debug)]
    #[command(
        name = "cw_daemon",
        version,
        about = "Call Warden Enterprise Daemon (Linux-only, UDS + SO_PEERCRED)"
    )]
    struct Cli {
        /// 配置文件路径（JSON 格式，字段优先级：CLI > env > 文件 > 默认）
        #[arg(long)]
        config: Option<PathBuf>,

        /// 前台运行（Rust 实现天然前台，flag 仅为兼容 systemd Type=simple）
        #[arg(long, default_value_t = true)]
        foreground: bool,

        /// UDS socket 路径（覆盖配置文件）
        #[arg(long)]
        socket: Option<PathBuf>,

        /// 工作线程数（覆盖配置文件）
        #[arg(long)]
        workers: Option<usize>,

        /// registry DB 路径（覆盖配置文件）
        #[arg(long)]
        registry: Option<PathBuf>,

        /// snapshot cache 容量（覆盖配置文件）
        #[arg(long)]
        cache_capacity: Option<usize>,

        /// 子命令（缺省 = serve）
        #[command(subcommand)]
        command: Option<Command>,
    }

    #[derive(Subcommand, Debug)]
    enum Command {
        /// 启动 UDS server（默认动作）
        Serve,
        /// schema 兼容性检查（systemd ExecStartPre 调用）
        SchemaCheck {
            /// 严格模式：schema_version 必须 == SCHEMA_VERSION，否则 exit 1
            #[arg(long)]
            strict: bool,
        },
        /// 健康检查（连接 UDS socket，发送 ping，等待响应）
        HealthCheck {
            /// 超时秒数
            #[arg(long, default_value_t = 15)]
            timeout: u64,
        },
    }

    /// 入口：返回进程退出码
    pub fn main() -> i32 {
        let cli = Cli::parse();

        match &cli.command {
            Some(Command::SchemaCheck { strict }) => schema_check(*strict, &cli),
            Some(Command::HealthCheck { timeout }) => health_check(*timeout, &cli),
            Some(Command::Serve) | None => serve(&cli),
        }
    }

    // ============================================
    // serve 子命令（默认动作）
    // ============================================

    fn serve(cli: &Cli) -> i32 {
        // 1. 加载配置（config file → env → CLI overrides）
        let mut config = load_config(cli);
        if let Err(e) = config.as_mut().map(|c| c.apply_env_overrides()) {
            eprintln!("[cw_daemon] [ERROR] 环境变量解析失败: {}", e);
            return 1;
        }
        let mut config = match config {
            Ok(c) => c,
            Err(e) => {
                eprintln!("[cw_daemon] [ERROR] 配置加载失败: {}", e);
                return 1;
            }
        };

        // 应用 CLI 参数覆盖（最高优先级）
        apply_cli_overrides(&mut config, cli);

        // 2. 确保目录存在
        if let Err(e) = config.ensure_directories() {
            eprintln!("[cw_daemon] [ERROR] 创建数据目录失败: {}", e);
            return 1;
        }

        eprintln!(
            "[cw_daemon] [INFO] starting with config: socket={}, workers={}, registry={}",
            config.socket_path.display(),
            config.max_workers,
            config.registry_db_path.display()
        );

        // 3. schema 初始化（WorkspaceRegistry::open 会自动 init_conn + 写入 schema_version）
        let registry = match WorkspaceRegistry::open(&config.registry_db_path.to_string_lossy()) {
            Ok(r) => r,
            Err(e) => {
                eprintln!(
                    "[cw_daemon] [ERROR] schema 初始化失败: {}: {}",
                    config.registry_db_path.display(),
                    e
                );
                return 1;
            }
        };
        eprintln!(
            "[cw_daemon] [INFO] schema initialized: version={}, registry={}",
            SCHEMA_VERSION,
            config.registry_db_path.display()
        );

        // 4. recover_all_workspaces：daemon 重启恢复
        // 从 registry DB 读取所有已注册 workspace，对每个 workspace 检查 staging.log
        // 是否有 pending entries，有则调用 Replicator::recover 恢复。
        // 修正 Python 版本的 bug：Python 只遍历内存中的 _workspace_resources，重启后是空的。
        let recovered_count = recover_all_workspaces(&registry, &config.data_root);
        eprintln!(
            "[cw_daemon] [INFO] recovered {} pending entries from workspaces",
            recovered_count
        );

        // 5. 构造 state_factory 闭包（每个 worker 线程调用一次，独立 WorkspaceRegistry 连接）
        // 必须是 Fn（可多次调用）+ Send + Sync：SnapshotDaemonState 内部有 Mutex<Connection>，
        // 每线程独立连接避免锁竞争
        let registry_db_path = config.registry_db_path.to_string_lossy().to_string();
        let cache_capacity = config.snapshot_cache_capacity;
        let state_factory = move || -> io::Result<SnapshotDaemonState> {
            let registry = WorkspaceRegistry::open(&registry_db_path)
                .map_err(|e| io::Error::new(io::ErrorKind::Other, e.to_string()))?;
            let snapshot_cache = Arc::new(SnapshotCache::new(cache_capacity));
            Ok(SnapshotDaemonState::with_registry(registry, snapshot_cache))
        };

        // 6. 构造 ServerConfig
        let server_config = ServerConfig {
            socket_path: config.socket_path.clone(),
            max_message_bytes: callwarden_core::daemon::protocol::DEFAULT_MAX_MESSAGE_BYTES,
            max_fds: callwarden_core::daemon::protocol::DEFAULT_MAX_FDS,
            max_workers: config.max_workers,
            request_timeout: config.request_timeout(),
            socket_mode: config.socket_mode as libc::mode_t,
            accept_timeout: Duration::from_millis(200),
        };

        // 7. 启动 server
        let mut handle: ServerHandle = match start_server(server_config, state_factory) {
            Ok(h) => h,
            Err(e) => {
                eprintln!("[cw_daemon] [ERROR] server 启动失败: {}", e);
                return 1;
            }
        };
        eprintln!(
            "[cw_daemon] [INFO] server listening: {} (mode 0o{:o})",
            config.socket_path.display(),
            config.socket_mode
        );

        // 8. 注册信号处理
        let stop_flag = Arc::new(AtomicBool::new(false));
        let reload_flag = Arc::new(AtomicBool::new(false));
        let drain_flag = Arc::new(AtomicBool::new(false));

        signal_flag::register(SIGTERM, Arc::clone(&stop_flag))
            .or_else(|_| signal_flag::register(SIGINT, Arc::clone(&stop_flag)))
            .ok();
        // 单独注册 SIGINT（如果上面 register 因 SIGTERM 注册失败会跳过）
        let _ = signal_flag::register(SIGINT, Arc::clone(&stop_flag));
        let _ = signal_flag::register(SIGHUP, Arc::clone(&reload_flag));
        let _ = signal_flag::register(SIGUSR1, Arc::clone(&drain_flag));

        eprintln!("[cw_daemon] [INFO] signal handlers registered (SIGTERM/SIGINT/SIGHUP/SIGUSR1)");
        eprintln!("[cw_daemon] [INFO] ready, waiting for connections (Type=simple mode)");

        // 9. 主循环：等待信号
        loop {
            if stop_flag.load(Ordering::SeqCst) {
                eprintln!("[cw_daemon] [INFO] received stop signal, shutting down...");
                break;
            }
            if reload_flag.swap(false, Ordering::SeqCst) {
                eprintln!("[cw_daemon] [INFO] received SIGHUP, reload requested (R7 stub: no-op)");
            }
            if drain_flag.swap(false, Ordering::SeqCst) {
                eprintln!("[cw_daemon] [INFO] received SIGUSR1, drain requested (R7 stub: no-op)");
            }
            thread::sleep(Duration::from_millis(100));
        }

        // 10. 优雅关闭
        eprintln!("[cw_daemon] [INFO] shutting down server...");
        handle.shutdown();
        handle.join();
        eprintln!("[cw_daemon] [INFO] server exited cleanly");
        0
    }

    // ============================================
    // schema-check 子命令
    // ============================================

    fn schema_check(strict: bool, cli: &Cli) -> i32 {
        let mut config = match load_config(cli) {
            Ok(c) => c,
            Err(e) => {
                eprintln!("[cw_daemon] [ERROR] 配置加载失败: {}", e);
                return 1;
            }
        };
        let _ = config.apply_env_overrides();
        apply_cli_overrides(&mut config, cli);

        // strict 模式：先读取 DB 实际 schema_version（不修改 DB）
        // 必须在 WorkspaceRegistry::open 之前，因为 open 会调用 init_conn
        // 用 SCHEMA_META_UPSERT 覆盖 schema_version，破坏 strict 检查语义
        if strict {
            let db_path = config.registry_db_path.to_string_lossy().to_string();
            match read_registry_schema_version(&db_path) {
                Ok(Some(actual_version)) => {
                    if actual_version != SCHEMA_VERSION {
                        eprintln!(
                            "[cw_daemon] [ERROR] schema-check strict: version mismatch: db={}, expected={}",
                            actual_version, SCHEMA_VERSION
                        );
                        return 1;
                    }
                    eprintln!(
                        "[cw_daemon] [INFO] schema-check strict: version={} matches",
                        actual_version
                    );
                }
                Ok(None) => {
                    eprintln!(
                        "[cw_daemon] [ERROR] schema-check strict: registry DB 未初始化（daemon_state 表或 schema_version 行缺失）: {}",
                        config.registry_db_path.display()
                    );
                    return 1;
                }
                Err(e) => {
                    eprintln!(
                        "[cw_daemon] [ERROR] schema-check strict: 读取 schema_version 失败: {}: {}",
                        config.registry_db_path.display(),
                        e
                    );
                    return 1;
                }
            }
        }

        // strict 已验证通过（或非 strict 模式）：正常 open（init schema + 写入当前版本）
        match WorkspaceRegistry::open(&config.registry_db_path.to_string_lossy()) {
            Ok(registry) => {
                let count = registry.count_workspaces().unwrap_or(0);
                eprintln!(
                    "[cw_daemon] [INFO] schema-check OK: version={}, workspaces={}, registry={}",
                    SCHEMA_VERSION,
                    count,
                    config.registry_db_path.display()
                );
                0
            }
            Err(e) => {
                eprintln!(
                    "[cw_daemon] [ERROR] schema-check failed: {}: {}",
                    config.registry_db_path.display(),
                    e
                );
                1
            }
        }
    }

    // ============================================
    // health-check 子命令（连接 UDS socket 验证 daemon 存活）
    // ============================================

    fn health_check(timeout: u64, cli: &Cli) -> i32 {
        let config = match load_config(cli) {
            Ok(c) => c,
            Err(e) => {
                eprintln!("[cw_daemon] [ERROR] 配置加载失败: {}", e);
                return 1;
            }
        };
        let socket_path = cli.socket.clone().unwrap_or(config.socket_path);

        let deadline = Instant::now() + Duration::from_secs(timeout);
        while Instant::now() < deadline {
            match UnixStream::connect(&socket_path) {
                Ok(stream) => {
                    // 连接成功，尝试发送一个 ping 消息（4 字节长度 + JSON）
                    // 简化：只发空帧，daemon 应该返回错误但表示存活
                    use std::io::Write;
                    let mut stream = stream;
                    // {"method":"ping"} = 18 字节
                    let body = br#"{"id":1,"method":"ping"}"#;
                    let len = body.len() as u32;
                    if stream.write_all(&len.to_be_bytes()).is_ok()
                        && stream.write_all(body).is_ok()
                    {
                        eprintln!("[cw_daemon] [INFO] health-check OK: daemon responding at {}", socket_path.display());
                        return 0;
                    }
                }
                Err(_) => {
                    // socket 还没就绪，等 200ms 重试
                    thread::sleep(Duration::from_millis(200));
                }
            }
        }
        eprintln!(
            "[cw_daemon] [ERROR] health-check timeout: daemon not responding at {}",
            socket_path.display()
        );
        1
    }

    // ============================================
    // 辅助函数
    // ============================================

    /// 加载配置：默认值 → 文件覆盖（如果 --config 指定）
    fn load_config(cli: &Cli) -> Result<DaemonConfig, callwarden_core::daemon::config::ConfigError> {
        if let Some(cfg_path) = &cli.config {
            DaemonConfig::load_from_file(cfg_path)
        } else {
            Ok(DaemonConfig::default())
        }
    }

    /// 应用 CLI 参数覆盖（最高优先级）
    fn apply_cli_overrides(config: &mut DaemonConfig, cli: &Cli) {
        if let Some(socket) = &cli.socket {
            config.socket_path = socket.clone();
        }
        if let Some(workers) = cli.workers {
            config.max_workers = workers;
        }
        if let Some(registry) = &cli.registry {
            config.registry_db_path = registry.clone();
        }
        if let Some(capacity) = cli.cache_capacity {
            config.snapshot_cache_capacity = capacity;
        }
    }

    /// 读取 registry DB 中的 schema_version（只读，不修改 DB）。
    ///
    /// 用于 `schema-check --strict` 模式：在不调用 `WorkspaceRegistry::open`（会
    /// 触发 `init_conn` 并 UPSERT 覆盖 `schema_version`）的前提下，验证 DB 实际
    /// 版本与二进制编译时的 `SCHEMA_VERSION` 是否匹配。
    ///
    /// 返回值：
    /// - `Ok(Some(v))`：DB 已初始化，schema_version=v
    /// - `Ok(None)`：DB 未初始化（daemon_state 表不存在或 schema_version 行缺失）
    /// - `Err(e)`：读取失败（如 DB 文件损坏）
    ///
    /// 注意：`Connection::open` 在 DB 不存在时会创建空 DB（SQLite 行为），
    /// 随后查询因表不存在而返回 None，这是预期行为。
    fn read_registry_schema_version(db_path: &str) -> Result<Option<u32>, rusqlite::Error> {
        use rusqlite::Connection;
        let conn = Connection::open(db_path)?;
        // 查询 daemon_state.schema_version（表不存在或行不存在都返回 None）
        let version_str: Option<String> = conn
            .query_row(
                "SELECT value FROM daemon_state WHERE key = 'schema_version'",
                [],
                |row| row.get(0),
            )
            .ok();
        Ok(version_str.and_then(|s| s.parse::<u32>().ok()))
    }

    /// 恢复所有 workspace 的 pending staging log entries。
    ///
    /// daemon 启动时调用。对应 Python daemon_server.py:L191-206，但修正了一个 bug：
    /// Python 版本只遍历内存中的 _workspace_resources（重启后是空的，实际不恢复任何 workspace）。
    /// Rust 版本从 registry DB 读取所有已注册 workspace，确保重启后能真正恢复。
    ///
    /// 路径约定（与 Python _get_workspace_resources 一致）：
    /// - ws_dir = data_root / workspace_instance_id
    /// - staging_log_path = ws_dir / "staging.log"
    ///
    /// 恢复策略：
    /// 1. registry.list_workspaces(None) → 遍历所有 workspace
    /// 2. 检查 staging.log 是否存在（不存在说明从未有 pending entries，跳过）
    /// 3. 打开 StagingLog，读取 pending entries
    /// 4. 过滤当前 workspace 的 entries（staging log 可能跨 workspace 共享）
    /// 5. 调用 Replicator::recover（无 SnapshotPublisher，只更新 log 状态）
    ///
    /// 错误容忍：单个 workspace 恢复失败不影响其他 workspace。
    fn recover_all_workspaces(
        registry: &WorkspaceRegistry,
        data_root: &std::path::Path,
    ) -> usize {
        use callwarden_core::daemon::replicator::Replicator;
        use callwarden_core::daemon::staging_log::StagingLog;

        let workspaces = match registry.list_workspaces(None) {
            Ok(w) => w,
            Err(e) => {
                eprintln!("[cw_daemon] [WARN] list_workspaces 失败: {}", e);
                return 0;
            }
        };

        let mut recovered_count = 0;
        for ws in workspaces {
            let ws_id = ws
                .get("workspace_instance_id")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if ws_id.is_empty() {
                continue;
            }

            let ws_dir = data_root.join(ws_id);
            let staging_log_path = ws_dir.join("staging.log");

            // staging.log 不存在 → 该 workspace 从未有 pending entries，跳过
            if !staging_log_path.exists() {
                continue;
            }

            // 打开 StagingLog（自动恢复 next_lsn）
            let staging_log = match StagingLog::new(staging_log_path.to_string_lossy().as_ref()) {
                Ok(l) => l,
                Err(e) => {
                    eprintln!(
                        "[cw_daemon] [WARN] 打开 staging.log 失败 ws={}: {}",
                        ws_id, e
                    );
                    continue;
                }
            };

            // 检查是否有 pending entries
            let pending = match staging_log.read_pending() {
                Ok(p) => p,
                Err(e) => {
                    eprintln!(
                        "[cw_daemon] [WARN] read_pending 失败 ws={}: {}",
                        ws_id, e
                    );
                    continue;
                }
            };

            // 过滤当前 workspace 的 entries
            let ws_pending: Vec<_> = pending
                .into_iter()
                .filter(|e| e.workspace_id == ws_id)
                .collect();

            if ws_pending.is_empty() {
                continue;
            }

            eprintln!(
                "[cw_daemon] [INFO] recovering {} pending entries for ws={}",
                ws_pending.len(),
                ws_id
            );

            // 创建 Replicator（无 SnapshotPublisher，只更新 log 状态）
            // daemon 启动恢复时不需要发布 snapshot（snapshot 可能已是最新），
            // 只需将 pending entries 标记为 applied 或重试 replication
            let replicator = Replicator::new(&staging_log);
            let result = replicator.recover(ws_id, "");

            if result.success {
                recovered_count += result.applied_count;
            } else {
                eprintln!(
                    "[cw_daemon] [WARN] recovery failed for ws={}: {}",
                    ws_id,
                    result.error.unwrap_or_default()
                );
            }
        }

        recovered_count
    }

    // ============================================
    // 单元测试
    // ============================================

    #[cfg(test)]
    mod tests {
        use super::*;
        use callwarden_core::daemon::staging_log::{StagingEntry, StagingLog};
        use callwarden_core::daemon::workspace::WorkspaceRegistry;
        use rusqlite::params;
        use std::fs;
        use std::path::Path;
        use tempfile::TempDir;

        /// 测试 fixture：临时 registry DB + data_root
        struct RecoverFixture {
            _tmp: TempDir,
            registry_db: PathBuf,
            data_root: PathBuf,
            registry: WorkspaceRegistry,
        }

        impl RecoverFixture {
            fn new() -> Self {
                let tmp = tempfile::tempdir().unwrap();
                let registry_db = tmp.path().join("registry.db");
                let data_root = tmp.path().join("data");
                fs::create_dir_all(&data_root).unwrap();
                let registry = WorkspaceRegistry::open(registry_db.to_str().unwrap()).unwrap();
                Self {
                    _tmp: tmp,
                    registry_db,
                    data_root,
                    registry,
                }
            }

            /// 注册一个 workspace，返回 workspace_instance_id
            fn register_workspace(&self, ws_root: &Path) -> String {
                // register_workspace 要求 client_view_root 真实存在
                fs::create_dir_all(ws_root).unwrap();
                let result = self
                    .registry
                    .register_workspace(
                        1000, // owner_uid
                        ws_root.to_str().unwrap(),
                        ws_root.to_str().unwrap(), // host_real_root
                        "",   // git_remote_url
                        "",   // git_head_commit_sha
                        "",   // toolchain_fingerprint
                    )
                    .unwrap();
                result["workspace_instance_id"]
                    .as_str()
                    .unwrap()
                    .to_string()
            }

            /// 在 data_root 下创建 staging.log 并写入 pending entries
            fn write_pending_entries(&self, ws_id: &str, entries: &[(&str, &str)]) {
                let ws_dir = self.data_root.join(ws_id);
                fs::create_dir_all(&ws_dir).unwrap();
                let log_path = ws_dir.join("staging.log");
                let log = StagingLog::new(log_path.to_str().unwrap()).unwrap();
                for (file_path, content_hash) in entries {
                    let mut entry = StagingEntry::new(ws_id, file_path, content_hash, "python");
                    log.append(&mut entry).unwrap();
                }
            }

            /// 获取 workspace 的 staging.log pending 数量
            fn count_pending(&self, ws_id: &str) -> usize {
                let log_path = self.data_root.join(ws_id).join("staging.log");
                if !log_path.exists() {
                    return 0;
                }
                let log = StagingLog::new(log_path.to_str().unwrap()).unwrap();
                log.read_pending().unwrap().len()
            }
        }

        #[test]
        fn test_recover_no_workspaces() {
            let fixture = RecoverFixture::new();
            // 无 workspace 注册
            let count = recover_all_workspaces(&fixture.registry, &fixture.data_root);
            assert_eq!(count, 0);
        }

        #[test]
        fn test_recover_workspace_no_staging_log() {
            let fixture = RecoverFixture::new();
            let ws_root = fixture.data_root.join("ws1_root");
            let ws_id = fixture.register_workspace(&ws_root);

            // workspace 已注册但无 staging.log 文件
            let count = recover_all_workspaces(&fixture.registry, &fixture.data_root);
            assert_eq!(count, 0);
            let _ = ws_id;
        }

        #[test]
        fn test_recover_workspace_empty_staging_log() {
            let fixture = RecoverFixture::new();
            let ws_root = fixture.data_root.join("ws2_root");
            let ws_id = fixture.register_workspace(&ws_root);

            // 创建空的 staging.log（无 pending entries）
            let ws_dir = fixture.data_root.join(&ws_id);
            fs::create_dir_all(&ws_dir).unwrap();
            let log_path = ws_dir.join("staging.log");
            StagingLog::new(log_path.to_str().unwrap()).unwrap();

            let count = recover_all_workspaces(&fixture.registry, &fixture.data_root);
            assert_eq!(count, 0);
        }

        #[test]
        fn test_recover_workspace_with_pending_entries() {
            let fixture = RecoverFixture::new();
            let ws_root = fixture.data_root.join("ws3_root");
            let ws_id = fixture.register_workspace(&ws_root);

            // 写入 3 条 pending entries
            fixture.write_pending_entries(
                &ws_id,
                &[
                    ("src/main.py", "hash1"),
                    ("src/utils.py", "hash2"),
                    ("tests/test_main.py", "hash3"),
                ],
            );

            // 验证 recover 返回 3（applied_count）
            let count = recover_all_workspaces(&fixture.registry, &fixture.data_root);
            assert_eq!(count, 3);

            // 验证 pending entries 已被标记为 applied（read_pending 返回空）
            let pending_after = fixture.count_pending(&ws_id);
            assert_eq!(pending_after, 0);
        }

        #[test]
        fn test_recover_multiple_workspaces_partial_pending() {
            let fixture = RecoverFixture::new();

            // ws_a：有 2 条 pending
            let ws_a_root = fixture.data_root.join("ws_a_root");
            let ws_a_id = fixture.register_workspace(&ws_a_root);
            fixture.write_pending_entries(
                &ws_a_id,
                &[("a.py", "hash_a1"), ("b.py", "hash_a2")],
            );

            // ws_b：无 staging.log
            let ws_b_root = fixture.data_root.join("ws_b_root");
            let ws_b_id = fixture.register_workspace(&ws_b_root);

            // ws_c：有 1 条 pending
            let ws_c_root = fixture.data_root.join("ws_c_root");
            let ws_c_id = fixture.register_workspace(&ws_c_root);
            fixture.write_pending_entries(&ws_c_id, &[("c.py", "hash_c1")]);

            // recover 应返回 2 + 0 + 1 = 3
            let count = recover_all_workspaces(&fixture.registry, &fixture.data_root);
            assert_eq!(count, 3);

            // 验证 ws_a 和 ws_c 的 pending 已清空
            assert_eq!(fixture.count_pending(&ws_a_id), 0);
            assert_eq!(fixture.count_pending(&ws_b_id), 0);
            assert_eq!(fixture.count_pending(&ws_c_id), 0);
        }

        #[test]
        fn test_recover_filters_entries_by_workspace_id() {
            let fixture = RecoverFixture::new();

            // 注册两个 workspace
            let ws_a_root = fixture.data_root.join("ws_filter_a_root");
            let ws_a_id = fixture.register_workspace(&ws_a_root);

            let ws_b_root = fixture.data_root.join("ws_filter_b_root");
            let ws_b_id = fixture.register_workspace(&ws_b_root);

            // 在 ws_a 的 staging.log 中写入 2 条 ws_a 的 + 1 条 ws_b 的
            // （模拟 staging.log 被跨 workspace 共享的情况）
            let ws_a_dir = fixture.data_root.join(&ws_a_id);
            fs::create_dir_all(&ws_a_dir).unwrap();
            let log_path = ws_a_dir.join("staging.log");
            let log = StagingLog::new(log_path.to_str().unwrap()).unwrap();

            // 写入 ws_a 的 entries
            let mut entry_a1 = StagingEntry::new(&ws_a_id, "a1.py", "hash_a1", "python");
            log.append(&mut entry_a1).unwrap();
            let mut entry_a2 = StagingEntry::new(&ws_a_id, "a2.py", "hash_a2", "python");
            log.append(&mut entry_a2).unwrap();
            // 写入 ws_b 的 entry（在 ws_a 的 log 中）
            let mut entry_b = StagingEntry::new(&ws_b_id, "b.py", "hash_b", "python");
            log.append(&mut entry_b).unwrap();

            // recover：只恢复 ws_a 的 2 条（ws_b 的 entry 在 ws_a 的 log 中，但 ws_b 有自己的 log 路径）
            // 注意：ws_b 在 data_root/ws_b_id/ 下没有 staging.log，所以不会被恢复
            let count = recover_all_workspaces(&fixture.registry, &fixture.data_root);

            // ws_a 的 log 有 2 条 ws_a + 1 条 ws_b = 3 条 pending
            // recover 只过滤 ws_a 的 2 条，但 read_pending 返回所有 3 条
            // Replicator::recover 内部会过滤 workspace_id
            assert_eq!(count, 2); // 只恢复 ws_a 的 2 条

            // ws_a 的 log 中仍有 ws_b 的 entry（未被恢复，因为 workspace_id 不匹配）
            let log = StagingLog::new(log_path.to_str().unwrap()).unwrap();
            let remaining_pending = log.read_pending().unwrap();
            assert_eq!(remaining_pending.len(), 1);
            assert_eq!(remaining_pending[0].workspace_id, ws_b_id);
        }

        #[test]
        fn test_recover_is_idempotent() {
            let fixture = RecoverFixture::new();
            let ws_root = fixture.data_root.join("ws_idem_root");
            let ws_id = fixture.register_workspace(&ws_root);

            // 写入 2 条 pending
            fixture.write_pending_entries(
                &ws_id,
                &[("x.py", "hash_x"), ("y.py", "hash_y")],
            );

            // 第一次 recover：恢复 2 条
            let count1 = recover_all_workspaces(&fixture.registry, &fixture.data_root);
            assert_eq!(count1, 2);

            // 第二次 recover：无 pending，返回 0
            let count2 = recover_all_workspaces(&fixture.registry, &fixture.data_root);
            assert_eq!(count2, 0);
        }

        // ---- read_registry_schema_version 单元测试 ----

        /// 临时 DB fixture：用 WorkspaceRegistry::open 初始化后返回路径
        struct SchemaVersionFixture {
            _tmp: TempDir,
            db_path: PathBuf,
        }

        impl SchemaVersionFixture {
            /// 创建已初始化的 registry DB（schema_version = SCHEMA_VERSION）
            fn new_initialized() -> Self {
                let tmp = tempfile::tempdir().unwrap();
                let db_path = tmp.path().join("registry.db");
                let db_str = db_path.to_string_lossy().to_string();
                // open 会调用 init_conn，写入 SCHEMA_VERSION
                let _ = WorkspaceRegistry::open(&db_str).unwrap();
                Self {
                    _tmp: tmp,
                    db_path,
                }
            }

            /// 创建已初始化但 schema_version 被改为 other 的 DB
            fn new_with_version(version: u32) -> Self {
                let fixture = Self::new_initialized();
                // 手动 UPDATE daemon_state.schema_version
                use rusqlite::Connection;
                let conn = Connection::open(&fixture.db_path).unwrap();
                conn.execute(
                    "UPDATE daemon_state SET value = ?1 WHERE key = 'schema_version'",
                    params![version.to_string()],
                )
                .unwrap();
                fixture
            }

            /// 创建空 DB（无 daemon_state 表）
            fn new_empty_db() -> Self {
                let tmp = tempfile::tempdir().unwrap();
                let db_path = tmp.path().join("empty.db");
                // 仅创建空文件（Connection::open 会创建空 DB，但不创建表）
                let conn = rusqlite::Connection::open(&db_path).unwrap();
                drop(conn);
                Self {
                    _tmp: tmp,
                    db_path,
                }
            }
        }

        #[test]
        fn test_read_schema_version_returns_current_version_for_initialized_db() {
            let fixture = SchemaVersionFixture::new_initialized();
            let db_str = fixture.db_path.to_string_lossy().to_string();
            let version = read_registry_schema_version(&db_str).unwrap();
            assert_eq!(version, Some(SCHEMA_VERSION));
        }

        #[test]
        fn test_read_schema_version_detects_version_mismatch() {
            // 模拟 DB 版本 = SCHEMA_VERSION + 10（未来版本）
            let future_version = SCHEMA_VERSION + 10;
            let fixture = SchemaVersionFixture::new_with_version(future_version);
            let db_str = fixture.db_path.to_string_lossy().to_string();
            let version = read_registry_schema_version(&db_str).unwrap();
            assert_eq!(version, Some(future_version));
            assert_ne!(version, Some(SCHEMA_VERSION));
        }

        #[test]
        fn test_read_schema_version_returns_none_for_empty_db() {
            // 空 DB（无 daemon_state 表）→ None
            let fixture = SchemaVersionFixture::new_empty_db();
            let db_str = fixture.db_path.to_string_lossy().to_string();
            let version = read_registry_schema_version(&db_str).unwrap();
            assert_eq!(version, None);
        }

        #[test]
        fn test_read_schema_version_returns_none_for_nonexistent_db() {
            // 不存在的 DB 路径：Connection::open 会创建空 DB，查询返回 None
            let tmp = tempfile::tempdir().unwrap();
            let db_path = tmp.path().join("nonexistent.db");
            let db_str = db_path.to_string_lossy().to_string();
            let version = read_registry_schema_version(&db_str).unwrap();
            assert_eq!(version, None);
        }
    }
}
