//! cw CLI binary（Phase 5-1 A.2）
//!
//! clap 命令树对齐 Python `cli/main.py:_SUBCOMMANDS` 的 59 个子命令。
//! 命令按迁移清单逐项接入真实 Rust 业务逻辑，未迁移项仍显式失败。
//!
//! 契约：docs/design/phase5-1-cli-config-contract.md §3.2

use std::path::PathBuf;
use std::time::Instant;

use callwarden_core::cli::config::{check_role_supported, load_config, ConfigEntry, PlatformPaths};
use callwarden_core::cli::file_query::{
    format_file_symbols_output, format_symbol_location_output, query_local_file_symbols,
    query_local_symbol_location,
};
use callwarden_core::cli::graph_query::{
    format_callees_output, format_callers_output, normalize_callees, normalize_callers,
    query_local_callees, query_local_callers, resolve_name_filter,
};
use callwarden_core::cli::graph_traversal::{
    format_call_chain_output, format_topological_output, normalize_enterprise_call_chain,
    normalize_enterprise_topological_order, query_local_call_chain,
    query_local_topological_order, MAX_CALL_CHAIN_DEPTH,
};
use callwarden_core::cli::grep::{query_local_grep, GrepOptions};
use callwarden_core::cli::impact::{
    format_impact_output, query_local_impact, MAX_IMPACT_DEPTH,
};
use callwarden_core::cli::issues_tests::{
    format_issues_output, format_test_cases_output, format_test_stability_output,
    format_tested_functions_output, query_local_issues, query_local_test_cases,
    query_local_test_stability, query_local_tested_functions,
};
use callwarden_core::cli::refresh::{
    build_enterprise_refresh_params, connect_params, enterprise_batch_result,
    format_refresh_output, new_cli_session_id, prepare_enterprise_file, refresh_local_paths,
};
use callwarden_core::cli::router::DaemonMode;
use callwarden_core::cli::runtime::{CommandResult, RouteUsed, RuntimeOptions};
use callwarden_core::cli::search::{
    format_search_output, normalize_search_results, query_local_search,
};
use callwarden_core::cli::stats::query_local_stats;
use callwarden_core::cli::status::{combine_enterprise_status, query_local_status};
use callwarden_core::cli::symbol::format_symbol_output;
use callwarden_core::symbol_query::query_symbol_detail;
use clap::{Parser, Subcommand, ValueEnum};

/// Call Warden — 代码知识图谱工具
#[derive(Parser)]
#[command(name = "cw", version, about = "Call Warden CLI", long_about = None)]
struct Cli {
    /// 数据源模式：local / enterprise / auto
    #[arg(long, value_enum, global = true)]
    mode: Option<ModeArg>,

    /// daemon UDS 路径
    #[arg(long, global = true)]
    socket: Option<PathBuf>,

    /// 本地 SQLite 路径
    #[arg(long, global = true)]
    db: Option<PathBuf>,

    /// workspace ID；未提供时 local 模式解析唯一 active workspace
    #[arg(long, global = true)]
    workspace_id: Option<String>,

    /// daemon RPC 超时秒数
    #[arg(long, default_value_t = 30, global = true)]
    timeout: u64,

    /// 子命令
    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Clone, Copy, Debug, ValueEnum)]
enum ModeArg {
    Local,
    Enterprise,
    Auto,
}

impl From<ModeArg> for DaemonMode {
    fn from(value: ModeArg) -> Self {
        match value {
            ModeArg::Local => DaemonMode::Local,
            ModeArg::Enterprise => DaemonMode::Enterprise,
            ModeArg::Auto => DaemonMode::Auto,
        }
    }
}

/// 59 个子命令枚举（对齐 Python `cli/main.py:_SUBCOMMANDS`）
///
/// 变体名使用 PascalCase，通过 `rename_all = "kebab-case"` 自动转换为
/// 命令行中的 kebab-case 名称（如 `VulnBlast` → `vuln-blast`）。
#[derive(Subcommand)]
#[command(rename_all = "kebab-case")]
enum Commands {
    // ===== 代码守护者架构（四大支柱）=====
    /// 安全护栏扫描
    Guardrail,
    /// 变更影响分析
    Impact {
        /// 源符号 hash
        symbol_hash: String,
        /// 最大反向 BFS 遍历深度
        #[arg(long, default_value_t = 3, allow_hyphen_values = true)]
        depth: i64,
    },
    /// 代码审查就绪度
    Review,
    /// 代码演化智能
    Evolution,
    /// 热点排名
    Hotspot,
    /// 变更频率分析
    Churn,
    /// 缺陷知识库
    Defect,
    /// 任务编排
    Task,
    /// 漏洞影响半径
    VulnBlast,
    /// 符号历史
    SymbolHistory,
    /// 检查门禁
    CheckGate,
    /// 测试影响选择
    TestImpact,

    // ===== 运维 =====
    /// 垃圾回收
    Gc,
    /// 健康检查
    Doctor,
    /// 安装 Agent
    InstallAgent,
    /// 安装 Hook
    InstallHook,
    /// 规则管理
    Rule,
    /// 审计链
    Audit,
    /// 引导状态
    Bootstrap,
    /// 克隆检测
    Clone,
    /// FTS5 全文搜索
    Fts,

    // ===== 8 大类 subcommand =====
    /// workspace 管理
    Workspace,
    /// 刷新数据库
    Refresh {
        /// 全仓刷新尚未迁移，本阶段显式拒绝
        #[arg(long)]
        all: bool,
        /// 强制全量重建尚未迁移，只能与 --all 一起使用
        #[arg(long)]
        force: bool,
        /// 要增量刷新的文件路径
        paths: Vec<PathBuf>,
    },
    /// 统计信息
    Stats,
    /// 状态信息
    Status,
    /// 搜索符号
    Search {
        /// 搜索关键词
        query: String,
        /// 符号类型过滤
        #[arg(long)]
        kind: Option<String>,
        /// 最大结果数
        #[arg(long, default_value_t = 50)]
        limit: usize,
    },
    /// 带符号上下文的文本搜索
    Grep {
        /// 搜索模式；多个模式为同一行 AND 语义
        #[arg(required = true, num_args = 1..)]
        patterns: Vec<String>,
        /// 将所有模式视为固定字符串
        #[arg(long)]
        fixed: bool,
        /// 符号过滤后返回的最大匹配数
        #[arg(long, default_value_t = 200)]
        limit: usize,
        /// 搜索路径；默认使用 workspace 根目录
        #[arg(long)]
        path: Option<PathBuf>,
        /// 保留不属于任何符号的匹配
        #[arg(long)]
        include_all: bool,
        /// 仅保留指定符号类型
        #[arg(long)]
        kind: Option<String>,
    },
    /// 符号查询
    Symbol {
        /// 完整限定名
        name: String,
    },
    /// 文件读取
    File {
        /// 文件路径
        path: String,
    },
    /// 查询
    Query {
        /// 符号短名
        name: String,
        /// 文件路径
        file: String,
    },
    /// 符号级静态检查问题
    Issues {
        /// 符号限定名
        qualified_name: String,
        /// 包含 INFO 级别问题
        #[arg(long)]
        include_info: bool,
    },
    /// 测试关联与运行历史
    Tests {
        /// 符号限定名
        qualified_name: Option<String>,
        /// 反向查询测试函数覆盖的生产函数
        #[arg(long)]
        reverse: bool,
        /// 写模式：重建测试关联，当前 Rust 只读阶段拒绝
        #[arg(long)]
        build: bool,
        /// 与 --build 配合的强制重建参数
        #[arg(long)]
        force: bool,
        /// 查询测试运行稳定性
        #[arg(long)]
        history: bool,
        /// 写模式：导入 JUnit XML，当前 Rust 只读阶段拒绝
        #[arg(long = "import")]
        import_file: Option<String>,
        /// CI 运行 ID
        #[arg(long, default_value = "")]
        ci_run_id: String,
        /// CI 运行 URL
        #[arg(long, default_value = "")]
        ci_url: String,
        /// history 最多读取的运行记录数
        #[arg(long, default_value_t = 50)]
        limit: usize,
    },
    /// 调用指定符号的函数
    Callers {
        /// 符号名称或限定名
        name: String,
        /// 完整限定名，精确匹配且不降级
        #[arg(long)]
        qualified: Option<String>,
    },
    /// 指定符号调用的函数
    Callees {
        /// 符号名称或限定名
        name: String,
        /// 完整限定名，精确匹配且不降级
        #[arg(long)]
        qualified: Option<String>,
    },
    /// 调用链
    CallChain {
        /// 起始符号限定名
        name: String,
        /// 最大向下遍历深度
        #[arg(long, default_value_t = 10)]
        depth: i64,
    },
    /// 拓扑排序
    Topo {
        /// 最多返回的函数数；负数表示不限制
        #[arg(long, default_value_t = 50)]
        limit: i64,
    },
    /// 指标
    Metrics,
    /// 复杂度
    Complexity,
    /// 耦合分析
    Coupling,
    /// 注释覆盖率
    CommentCoverage,
    /// 未注释符号
    Uncommented,
    /// 函数问题
    FunctionIssues,
    /// 最大函数
    LargestFns,
    /// 耦合函数
    CoupledFns,
    /// 函数指标
    FnMetrics,
    /// Git 集成
    Git,
    /// Semgrep 集成
    Semgrep,
    /// 覆盖率
    Coverage,
    /// 负责人
    Who,
    /// 所有权映射
    OwnershipMap,
    /// 项目简报
    Brief,
    /// 仓库地图
    Map,
    /// 健康报告
    HealthReport,

    // ===== L5/N4 =====
    /// 构建上下文
    BuildContext,
    /// 工具链指纹
    Toolchain,
    /// 图存储
    Graph,
    /// 配置管理
    Config {
        #[command(subcommand)]
        action: ConfigAction,
    },

    // ===== 驾驶舱 =====
    /// 项目综合状态驾驶舱
    Dashboard,

    // ===== 回滚 =====
    /// 迁移回滚配置
    Rollback,
}

#[derive(Subcommand)]
enum ConfigAction {
    /// 输出每个配置值及其来源
    Explain,
    /// 输出当前平台的配置和数据路径
    Paths,
    /// 检查当前平台是否支持指定安装角色
    CheckRole {
        #[arg(value_enum)]
        role: RoleArg,
    },
}

#[derive(Clone, Copy, Debug, ValueEnum)]
enum RoleArg {
    Local,
    Client,
    Agent,
    Daemon,
    All,
}

impl RoleArg {
    fn as_str(self) -> &'static str {
        match self {
            Self::Local => "local",
            Self::Client => "client",
            Self::Agent => "agent",
            Self::Daemon => "daemon",
            Self::All => "all",
        }
    }
}

/// 子命令的通用参数（骨架阶段仅接收任意位置参数，不深入解析）
#[derive(Parser)]
struct SubcommandArgs {
    /// 位置参数（透传给后续业务逻辑）
    #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
    args: Vec<String>,
}

fn main() {
    let cli = Cli::parse();
    let runtime = RuntimeOptions::from_overrides(
        cli.mode.map(Into::into),
        cli.socket,
        cli.db,
        cli.workspace_id,
        cli.timeout,
    );

    match cli.command {
        Some(cmd) => {
            // Phase 5-1 C: stats 子命令业务逻辑已迁移到 lib
            // 其他子命令仍返回 "not implemented"（Phase 5-1 C 扩展阶段逐命令迁移）
            match cmd {
                Commands::Stats => {
                    emit_result(run_stats(&runtime));
                }
                Commands::Status => {
                    emit_result(run_status(&runtime));
                }
                Commands::Config { action } => {
                    emit_result(run_config(action));
                }
                Commands::Search { query, kind, limit } => {
                    emit_result(run_search(&runtime, &query, kind.as_deref(), limit));
                }
                Commands::Symbol { name } => {
                    emit_result(run_symbol(&runtime, &name));
                }
                Commands::File { path } => {
                    emit_result(run_file(&runtime, &path));
                }
                Commands::Query { name, file } => {
                    emit_result(run_query(&runtime, &name, &file));
                }
                Commands::Grep {
                    patterns,
                    fixed,
                    limit,
                    path,
                    include_all,
                    kind,
                } => {
                    emit_result(run_grep(
                        &runtime,
                        GrepOptions {
                            patterns,
                            fixed,
                            limit,
                            path,
                            include_all,
                            kind,
                        },
                    ));
                }
                Commands::Issues {
                    qualified_name,
                    include_info,
                } => {
                    emit_result(run_issues(&runtime, &qualified_name, include_info));
                }
                Commands::Tests {
                    qualified_name,
                    reverse,
                    build,
                    force,
                    history,
                    import_file,
                    ci_run_id,
                    ci_url,
                    limit,
                } => {
                    emit_result(run_tests(
                        &runtime,
                        qualified_name.as_deref(),
                        reverse,
                        build,
                        force,
                        history,
                        import_file.as_deref(),
                        &ci_run_id,
                        &ci_url,
                        limit,
                    ));
                }
                Commands::Callers { name, qualified } => {
                    emit_result(run_callers(&runtime, &name, qualified.as_deref()));
                }
                Commands::Callees { name, qualified } => {
                    emit_result(run_callees(&runtime, &name, qualified.as_deref()));
                }
                Commands::CallChain { name, depth } => {
                    emit_result(run_call_chain(&runtime, &name, depth));
                }
                Commands::Topo { limit } => {
                    emit_result(run_topo(&runtime, limit));
                }
                Commands::Impact { symbol_hash, depth } => {
                    emit_result(run_impact(&runtime, &symbol_hash, depth));
                }
                Commands::Refresh { all, force, paths } => {
                    emit_result(run_refresh(&runtime, all, force, &paths));
                }
                _ => {
                    // 骨架阶段：其他子命令返回 "not implemented"
                    // Phase 5-1 C 扩展阶段将逐命令迁移业务逻辑
                    let cmd_name = command_name(&cmd);
                    eprintln!(
                        "cw {}: not implemented (Phase 5-1 skeleton — subcommand parsed successfully)",
                        cmd_name
                    );
                    std::process::exit(1);
                }
            }
        }
        None => {
            // 无子命令时打印 help
            Cli::parse_from(["cw", "--help"]);
        }
    }
}

fn run_refresh(
    runtime: &RuntimeOptions,
    refresh_all: bool,
    force: bool,
    paths: &[PathBuf],
) -> CommandResult {
    if force && !refresh_all {
        return CommandResult::failure(
            1,
            "--force is only valid together with --all".to_string(),
            RouteUsed::None,
        );
    }
    if refresh_all {
        return CommandResult::failure(
            1,
            "cw refresh --all/--force is not migrated yet; use explicit file paths".to_string(),
            RouteUsed::None,
        );
    }
    if paths.is_empty() {
        return CommandResult::failure(
            1,
            "cw refresh requires at least one file path".to_string(),
            RouteUsed::None,
        );
    }

    runtime.execute_write_with(
        || {
            let conn = runtime.open_local_write_db()?;
            let workspace_id = runtime.resolve_local_workspace_id(&conn)?;
            let result = refresh_local_paths(&conn, &runtime.db_path, workspace_id, paths)?;
            Ok(format_refresh_output(&result))
        },
        || run_enterprise_refresh(runtime, paths),
    )
}

fn run_enterprise_refresh(runtime: &RuntimeOptions, paths: &[PathBuf]) -> Result<String, String> {
    let workspace_id = runtime.workspace_id.as_deref().ok_or_else(|| {
        "enterprise refresh requires --workspace-id <workspace_instance_id>".to_string()
    })?;
    let session_id = new_cli_session_id();
    let connect = runtime.daemon_call(
        "workspace.connect",
        connect_params(workspace_id, &session_id),
    )?;
    let session_epoch = connect
        .get("session_epoch")
        .and_then(serde_json::Value::as_u64)
        .ok_or_else(|| "workspace.connect response is missing session_epoch".to_string())?;
    let started = Instant::now();
    let mut responses = Vec::with_capacity(paths.len());

    for (index, path) in paths.iter().enumerate() {
        let response = prepare_enterprise_file(path).and_then(|(rel_path, canonical_bytes)| {
            let params = build_enterprise_refresh_params(
                workspace_id,
                &rel_path,
                &session_id,
                session_epoch,
                index as u64 + 1,
                &canonical_bytes,
            );
            let value = runtime.daemon_call("workspace.file.refresh", params)?;
            let status = value
                .get("status")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("");
            if status != "committed" {
                return Err(format!(
                    "daemon rejected refresh for {rel_path}: status={status}"
                ));
            }
            Ok(value)
        });
        responses.push(response);
    }

    Ok(format_refresh_output(&enterprise_batch_result(
        paths,
        responses,
        started.elapsed().as_secs_f64(),
    )))
}

fn run_stats(runtime: &RuntimeOptions) -> CommandResult {
    runtime.execute_read_with(
        || {
            let conn = runtime.open_local_db()?;
            let workspace_id = runtime.resolve_local_workspace_id(&conn)?;
            query_local_stats(&conn, workspace_id)
        },
        || {
            let workspace_id = runtime.workspace_id.as_deref().ok_or_else(|| {
                "enterprise stats requires --workspace-id <workspace_instance_id>".to_string()
            })?;
            let (method, params) = callwarden_core::daemon::client::build_query_request(
                workspace_id,
                "stats",
                "",
                None,
                None,
                None,
                None,
            )
            .map_err(|error| format!("cannot build stats RPC: {error}"))?;
            runtime.daemon_call(&method, params)
        },
    )
}

fn run_status(runtime: &RuntimeOptions) -> CommandResult {
    runtime.execute_read_with(
        || {
            let conn = runtime.open_local_db()?;
            let workspace_id = runtime.resolve_local_workspace_id(&conn)?;
            query_local_status(&conn, workspace_id, &runtime.db_path)
        },
        || {
            let workspace_id = runtime.workspace_id.as_deref().ok_or_else(|| {
                "enterprise status requires --workspace-id <workspace_instance_id>".to_string()
            })?;
            query_enterprise_status(workspace_id, |method, params| {
                runtime.daemon_call(method, params)
            })
        },
    )
}

fn run_search(
    runtime: &RuntimeOptions,
    query: &str,
    kind: Option<&str>,
    limit: usize,
) -> CommandResult {
    let result = runtime.execute_read_with(
        || {
            let conn = runtime.open_local_db()?;
            let workspace_id = runtime.resolve_local_workspace_id(&conn)?;
            query_local_search(&conn, workspace_id, query, kind, limit)
        },
        || {
            let workspace_id = runtime.workspace_id.as_deref().ok_or_else(|| {
                "enterprise search requires --workspace-id <workspace_instance_id>".to_string()
            })?;
            query_enterprise_search(workspace_id, query, kind, limit, |method, params| {
                runtime.daemon_call(method, params)
            })
        },
    );
    format_search_result(result, query, kind, limit)
}

fn query_enterprise_search<F>(
    workspace_id: &str,
    query: &str,
    kind: Option<&str>,
    limit: usize,
    mut call: F,
) -> Result<serde_json::Value, String>
where
    F: FnMut(&str, serde_json::Value) -> Result<serde_json::Value, String>,
{
    let rpc_limit =
        u32::try_from(limit).map_err(|_| format!("search limit is too large: {limit}"))?;
    let (method, params) = callwarden_core::daemon::client::build_query_request(
        workspace_id,
        "search",
        query,
        None,
        kind,
        Some(rpc_limit),
        None,
    )
    .map_err(|error| format!("cannot build search RPC: {error}"))?;
    normalize_search_results(call(&method, params)?)
}

fn format_search_result(
    mut result: CommandResult,
    query: &str,
    kind: Option<&str>,
    limit: usize,
) -> CommandResult {
    if result.exit_code != 0 {
        return result;
    }
    let parsed = match serde_json::from_str::<serde_json::Value>(&result.stdout) {
        Ok(value) => value,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("cannot decode search result: {error}"),
                result.route,
            );
        }
    };
    match format_search_output(&parsed, query, kind, limit) {
        Ok(stdout) => {
            result.stdout = stdout;
            result
        }
        Err(error) => CommandResult::failure(1, error, result.route),
    }
}

fn run_symbol(runtime: &RuntimeOptions, qualified_name: &str) -> CommandResult {
    let result = runtime.execute_read_with(
        || {
            let conn = runtime.open_local_db()?;
            let workspace_id = runtime.resolve_local_workspace_id(&conn)?;
            query_symbol_detail(&conn, workspace_id, qualified_name)
        },
        || {
            let workspace_id = runtime.workspace_id.as_deref().ok_or_else(|| {
                "enterprise symbol requires --workspace-id <workspace_instance_id>".to_string()
            })?;
            query_enterprise_symbol(workspace_id, qualified_name, |method, params| {
                runtime.daemon_call(method, params)
            })
        },
    );
    format_symbol_result(result, qualified_name)
}

fn query_enterprise_symbol<F>(
    workspace_id: &str,
    qualified_name: &str,
    mut call: F,
) -> Result<serde_json::Value, String>
where
    F: FnMut(&str, serde_json::Value) -> Result<serde_json::Value, String>,
{
    let (method, params) = callwarden_core::daemon::client::build_query_request(
        workspace_id,
        "symbol",
        qualified_name,
        None,
        None,
        None,
        None,
    )
    .map_err(|error| format!("cannot build symbol RPC: {error}"))?;
    call(&method, params)
}

fn format_symbol_result(mut result: CommandResult, qualified_name: &str) -> CommandResult {
    if result.exit_code != 0 {
        return result;
    }
    let parsed = match serde_json::from_str::<serde_json::Value>(&result.stdout) {
        Ok(value) => value,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("cannot decode symbol result: {error}"),
                result.route,
            );
        }
    };
    match format_symbol_output(&parsed, qualified_name) {
        Ok(stdout) => {
            result.stdout = stdout;
            result
        }
        Err(error) => CommandResult::failure(1, error, result.route),
    }
}

fn run_file(runtime: &RuntimeOptions, file_path: &str) -> CommandResult {
    let result = runtime.execute_read_with(
        || {
            let conn = runtime.open_local_db()?;
            let workspace_id = runtime.resolve_local_workspace_id(&conn)?;
            query_local_file_symbols(&conn, workspace_id, file_path)
        },
        || Err("enterprise file query is not implemented by the daemon protocol".to_string()),
    );
    format_file_result(result, file_path)
}

fn format_file_result(mut result: CommandResult, file_path: &str) -> CommandResult {
    if result.exit_code != 0 {
        return result;
    }
    let parsed = match serde_json::from_str::<serde_json::Value>(&result.stdout) {
        Ok(value) => value,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("cannot decode file symbol result: {error}"),
                result.route,
            );
        }
    };
    match format_file_symbols_output(&parsed, file_path) {
        Ok(stdout) => {
            result.stdout = stdout;
            result
        }
        Err(error) => CommandResult::failure(1, error, result.route),
    }
}

fn run_query(runtime: &RuntimeOptions, name: &str, file_path: &str) -> CommandResult {
    let result = runtime.execute_read_with(
        || {
            let conn = runtime.open_local_db()?;
            let workspace_id = runtime.resolve_local_workspace_id(&conn)?;
            query_local_symbol_location(&conn, workspace_id, name, file_path)
        },
        || {
            Err(
                "enterprise symbol location query is not implemented by the daemon protocol"
                    .to_string(),
            )
        },
    );
    format_query_result(result, name)
}

fn format_query_result(mut result: CommandResult, name: &str) -> CommandResult {
    if result.exit_code != 0 {
        return result;
    }
    let parsed = match serde_json::from_str::<serde_json::Value>(&result.stdout) {
        Ok(value) => value,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("cannot decode symbol location result: {error}"),
                result.route,
            );
        }
    };
    match format_symbol_location_output(&parsed, name) {
        Ok(stdout) => {
            result.stdout = stdout;
            result
        }
        Err(error) => CommandResult::failure(1, error, result.route),
    }
}

fn run_grep(runtime: &RuntimeOptions, options: GrepOptions) -> CommandResult {
    let result = runtime.execute_read_with(
        || {
            let conn = runtime.open_local_db()?;
            let workspace_id = runtime.resolve_local_workspace_id(&conn)?;
            query_local_grep(&conn, workspace_id, &options)
        },
        || Err("enterprise grep is not implemented by the daemon protocol".to_string()),
    );
    format_grep_result(result)
}

fn format_grep_result(mut result: CommandResult) -> CommandResult {
    if result.exit_code != 0 {
        return result;
    }
    let parsed = match serde_json::from_str::<serde_json::Value>(&result.stdout) {
        Ok(value) => value,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("cannot decode grep result: {error}"),
                result.route,
            );
        }
    };
    match parsed.as_str() {
        Some(stdout) => {
            result.stdout = stdout.to_string();
            result
        }
        None => CommandResult::failure(
            1,
            "grep result must be a JSON string".to_string(),
            result.route,
        ),
    }
}

fn run_issues(runtime: &RuntimeOptions, qualified_name: &str, include_info: bool) -> CommandResult {
    let result = runtime.execute_read_with(
        || {
            let conn = runtime.open_local_db()?;
            let workspace_id = runtime.resolve_local_workspace_id(&conn)?;
            query_local_issues(&conn, workspace_id, qualified_name, include_info)
        },
        || Err("enterprise issues query is not implemented by the daemon protocol".to_string()),
    );
    format_read_result(result, |value| {
        format_issues_output(value, qualified_name, include_info)
    })
}

#[allow(clippy::too_many_arguments)]
fn run_tests(
    runtime: &RuntimeOptions,
    qualified_name: Option<&str>,
    reverse: bool,
    build: bool,
    _force: bool,
    history: bool,
    import_file: Option<&str>,
    _ci_run_id: &str,
    _ci_url: &str,
    limit: usize,
) -> CommandResult {
    if import_file.is_some() || build {
        return CommandResult::failure(
            2,
            "Rust cw tests write modes --build/--import are not migrated; use the Python CLI until the write-command phase"
                .to_string(),
            RouteUsed::None,
        );
    }
    let Some(qualified_name) = qualified_name else {
        let message = if history {
            "Error: qualified_name required with --history"
        } else {
            "Error: qualified_name required (or use --build/--import/--history)"
        };
        return text_result(0, message.to_string());
    };

    let result = runtime.execute_read_with(
        || {
            let conn = runtime.open_local_db()?;
            let workspace_id = runtime.resolve_local_workspace_id(&conn)?;
            if history {
                query_local_test_stability(&conn, workspace_id, qualified_name, limit)
            } else if reverse {
                query_local_tested_functions(&conn, workspace_id, qualified_name)
            } else {
                query_local_test_cases(&conn, workspace_id, qualified_name)
            }
        },
        || Err("enterprise tests query is not implemented by the daemon protocol".to_string()),
    );
    format_read_result(result, |value| {
        if history {
            format_test_stability_output(value, qualified_name)
        } else if reverse {
            format_tested_functions_output(value, qualified_name)
        } else {
            format_test_cases_output(value, qualified_name)
        }
    })
}

fn run_callers(
    runtime: &RuntimeOptions,
    requested_name: &str,
    qualified_name: Option<&str>,
) -> CommandResult {
    let result = runtime.execute_read_with(
        || {
            let conn = runtime.open_local_db()?;
            let workspace_id = runtime.resolve_local_workspace_id(&conn)?;
            query_local_callers(&conn, workspace_id, requested_name, qualified_name)
        },
        || {
            let workspace_id = runtime.workspace_id.as_deref().ok_or_else(|| {
                "enterprise callers requires --workspace-id <workspace_instance_id>".to_string()
            })?;
            query_enterprise_graph(
                workspace_id,
                "callers",
                requested_name,
                qualified_name,
                |method, params| runtime.daemon_call(method, params),
            )
        },
    );
    format_read_result(result, |value| format_callers_output(value, requested_name))
}

fn run_callees(
    runtime: &RuntimeOptions,
    requested_name: &str,
    qualified_name: Option<&str>,
) -> CommandResult {
    let result = runtime.execute_read_with(
        || {
            let conn = runtime.open_local_db()?;
            let workspace_id = runtime.resolve_local_workspace_id(&conn)?;
            query_local_callees(&conn, workspace_id, requested_name, qualified_name)
        },
        || {
            let workspace_id = runtime.workspace_id.as_deref().ok_or_else(|| {
                "enterprise callees requires --workspace-id <workspace_instance_id>".to_string()
            })?;
            query_enterprise_graph(
                workspace_id,
                "callees",
                requested_name,
                qualified_name,
                |method, params| runtime.daemon_call(method, params),
            )
        },
    );
    format_read_result(result, |value| format_callees_output(value, requested_name))
}

fn run_call_chain(
    runtime: &RuntimeOptions,
    qualified_name: &str,
    requested_depth: i64,
) -> CommandResult {
    let result = runtime.execute_read_with(
        || {
            let conn = runtime.open_local_db()?;
            let workspace_id = runtime.resolve_local_workspace_id(&conn)?;
            query_local_call_chain(&conn, workspace_id, qualified_name, requested_depth)
        },
        || {
            let workspace_id = runtime.workspace_id.as_deref().ok_or_else(|| {
                "enterprise call-chain requires --workspace-id <workspace_instance_id>".to_string()
            })?;
            query_enterprise_call_chain(
                workspace_id,
                qualified_name,
                requested_depth,
                |method, params| runtime.daemon_call(method, params),
            )
        },
    );
    format_read_result(result, format_call_chain_output)
}

fn run_impact(runtime: &RuntimeOptions, symbol_hash: &str, requested_depth: i64) -> CommandResult {
    let result = runtime.execute_read_with(
        || {
            let conn = runtime.open_local_db()?;
            let workspace_id = runtime.resolve_local_workspace_id(&conn)?;
            query_local_impact(&conn, workspace_id, symbol_hash, requested_depth)
        },
        || {
            let workspace_id = runtime.workspace_id.as_deref().ok_or_else(|| {
                "enterprise impact requires --workspace-id <workspace_instance_id>".to_string()
            })?;
            query_enterprise_impact(
                workspace_id,
                symbol_hash,
                requested_depth,
                |method, params| runtime.daemon_call(method, params),
            )
        },
    );
    format_read_result(result, format_impact_output)
}

fn query_enterprise_impact<F>(
    workspace_id: &str,
    symbol_hash: &str,
    requested_depth: i64,
    mut call: F,
) -> Result<serde_json::Value, String>
where
    F: FnMut(&str, serde_json::Value) -> Result<serde_json::Value, String>,
{
    let bounded_depth = requested_depth.max(0).min(MAX_IMPACT_DEPTH as i64) as u32;
    let (method, params) = callwarden_core::daemon::client::build_query_request(
        workspace_id,
        "impact",
        symbol_hash,
        None,
        None,
        None,
        Some(bounded_depth),
    )
    .map_err(|error| format!("cannot build impact RPC: {error}"))?;
    call(&method, params)
}

fn query_enterprise_call_chain<F>(
    workspace_id: &str,
    qualified_name: &str,
    requested_depth: i64,
    mut call: F,
) -> Result<serde_json::Value, String>
where
    F: FnMut(&str, serde_json::Value) -> Result<serde_json::Value, String>,
{
    let bounded_depth = requested_depth.max(0).min(MAX_CALL_CHAIN_DEPTH as i64) as u32;
    let (method, params) = callwarden_core::daemon::client::build_query_request(
        workspace_id,
        "call_chain_down",
        qualified_name,
        None,
        None,
        None,
        Some(bounded_depth),
    )
    .map_err(|error| format!("cannot build call-chain RPC: {error}"))?;
    normalize_enterprise_call_chain(call(&method, params)?, qualified_name, requested_depth)
}

fn run_topo(runtime: &RuntimeOptions, limit: i64) -> CommandResult {
    let result = runtime.execute_read_with(
        || {
            let conn = runtime.open_local_db()?;
            let workspace_id = runtime.resolve_local_workspace_id(&conn)?;
            query_local_topological_order(&conn, workspace_id, limit)
        },
        || {
            let workspace_id = runtime.workspace_id.as_deref().ok_or_else(|| {
                "enterprise topo requires --workspace-id <workspace_instance_id>".to_string()
            })?;
            query_enterprise_topological_order(workspace_id, limit, |method, params| {
                runtime.daemon_call(method, params)
            })
        },
    );
    format_read_result(result, format_topological_output)
}

fn query_enterprise_topological_order<F>(
    workspace_id: &str,
    limit: i64,
    mut call: F,
) -> Result<serde_json::Value, String>
where
    F: FnMut(&str, serde_json::Value) -> Result<serde_json::Value, String>,
{
    let rpc_limit = if limit < 0 {
        u32::MAX
    } else {
        u32::try_from(limit).map_err(|_| format!("topo limit is too large: {limit}"))?
    };
    let (method, mut params) = callwarden_core::daemon::client::build_query_request(
        workspace_id,
        "topological_order",
        "",
        None,
        None,
        Some(rpc_limit),
        None,
    )
    .map_err(|error| format!("cannot build topo RPC: {error}"))?;
    params
        .as_object_mut()
        .ok_or_else(|| "topo RPC params must be a JSON object".to_string())?
        .insert("detail".to_string(), serde_json::Value::Bool(true));
    normalize_enterprise_topological_order(call(&method, params)?)
}

fn query_enterprise_graph<F>(
    workspace_id: &str,
    query_type: &str,
    requested_name: &str,
    qualified_name: Option<&str>,
    mut call: F,
) -> Result<serde_json::Value, String>
where
    F: FnMut(&str, serde_json::Value) -> Result<serde_json::Value, String>,
{
    let (short_name, effective_qname, auto_qname) =
        resolve_name_filter(requested_name, qualified_name);
    let query_once =
        |name: &str, qname: Option<&str>, call: &mut F| -> Result<serde_json::Value, String> {
            let (method, params) = callwarden_core::daemon::client::build_query_request(
                workspace_id,
                query_type,
                name,
                qname,
                None,
                None,
                None,
            )
            .map_err(|error| format!("cannot build {query_type} RPC: {error}"))?;
            call(&method, params)
        };

    let mut value = query_once(short_name, effective_qname, &mut call)?;
    if auto_qname && matches!(value.as_array(), Some(rows) if rows.is_empty()) {
        value = query_once(short_name, None, &mut call)?;
    }
    match query_type {
        "callers" => normalize_callers(value),
        "callees" => normalize_callees(value),
        _ => Err(format!("unsupported graph query type: {query_type}")),
    }
}

fn format_read_result<F>(mut result: CommandResult, formatter: F) -> CommandResult
where
    F: FnOnce(&serde_json::Value) -> Result<String, String>,
{
    if result.exit_code != 0 {
        return result;
    }
    let parsed = match serde_json::from_str::<serde_json::Value>(&result.stdout) {
        Ok(value) => value,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("cannot decode read query result: {error}"),
                result.route,
            );
        }
    };
    match formatter(&parsed) {
        Ok(stdout) => {
            result.stdout = stdout;
            result
        }
        Err(error) => CommandResult::failure(1, error, result.route),
    }
}

fn query_enterprise_status<F>(workspace_id: &str, mut call: F) -> Result<serde_json::Value, String>
where
    F: FnMut(&str, serde_json::Value) -> Result<serde_json::Value, String>,
{
    let (status_method, status_params) =
        callwarden_core::daemon::client::build_simple_request("status", Some(workspace_id))
            .map_err(|error| format!("cannot build workspace status RPC: {error}"))?;
    let workspace_registry = call(&status_method, status_params)?;

    let (stats_method, stats_params) = callwarden_core::daemon::client::build_query_request(
        workspace_id,
        "stats",
        "",
        None,
        None,
        None,
        None,
    )
    .map_err(|error| format!("cannot build status stats RPC: {error}"))?;
    let graph_stats = call(&stats_method, stats_params)?;
    Ok(combine_enterprise_status(workspace_registry, graph_stats))
}

fn run_config(action: ConfigAction) -> CommandResult {
    match action {
        ConfigAction::Explain => {
            let entries = load_config(None, "CW_").explain();
            text_result(0, format_config_explain(&entries))
        }
        ConfigAction::Paths => {
            let paths = PlatformPaths::detect();
            text_result(0, format_config_paths(&paths, python_platform_name()))
        }
        ConfigAction::CheckRole { role } => {
            let role = role.as_str();
            let platform = python_platform_name();
            let supported = check_role_supported(role, Some(platform));
            text_result(
                if supported { 0 } else { 1 },
                format_role_support(role, platform, supported),
            )
        }
    }
}

fn format_config_explain(entries: &[ConfigEntry]) -> String {
    let mut lines = vec![
        "# N4 分层配置（来源：callwarden_core::cli::config）".to_string(),
        "# 优先级：CLI > env(CW_*) > user_config > system_config > default".to_string(),
        String::new(),
        format!("{:<30} {:<40} {}", "Key", "Value", "Source"),
        format!(
            "{:<30} {:<40} {}",
            "-".repeat(30),
            "-".repeat(40),
            "-".repeat(20)
        ),
    ];
    for entry in entries {
        let value = truncate_config_value(&entry.value);
        lines.push(format!("{:<30} {:<40} {}", entry.key, value, entry.source));
    }
    lines.push(String::new());
    lines.push(format!("共 {} 个配置项", entries.len()));
    lines.join("\n")
}

fn format_config_paths(paths: &PlatformPaths, platform: &str) -> String {
    let mut lines = vec![
        "# N4 PlatformPaths（来源：callwarden_core::cli::config）".to_string(),
        format!("# 平台：{platform}"),
        String::new(),
        format!("{:<20} {}", "Name", "Path"),
        format!("{:<20} {}", "-".repeat(20), "-".repeat(60)),
        format!("{:<20} {}", "system_config", paths.system_config.display()),
        format!("{:<20} {}", "user_config", paths.user_config.display()),
        format!("{:<20} {}", "system_data", paths.system_data.display()),
        format!("{:<20} {}", "user_data", paths.user_data.display()),
    ];
    if let Some(runtime) = &paths.runtime {
        lines.push(format!("{:<20} {}", "runtime", runtime.display()));
    }
    lines.extend([
        String::new(),
        "提示：".to_string(),
        format!(
            "  - 系统配置文件：{}（需 root/admin 写入）",
            paths.system_config.display()
        ),
        format!(
            "  - 用户配置文件：{}（普通用户写入）",
            paths.user_config.display()
        ),
        format!(
            "  - 数据目录：{}（数据库等持久化数据）",
            paths.user_data.display()
        ),
    ]);
    lines.join("\n")
}

fn format_role_support(role: &str, platform: &str, supported: bool) -> String {
    if supported {
        return format!("角色 '{role}' 在当前平台 {platform} 上 ✅ 支持");
    }
    [
        format!("角色 '{role}' 在当前平台 {platform} 上 ❌ 不支持"),
        "提示：".to_string(),
        "  - Windows/macOS 仅支持 local/client 角色".to_string(),
        "  - Linux 才支持 agent/daemon/all 角色（需 SO_PEERCRED + SCM_RIGHTS + UDS）".to_string(),
    ]
    .join("\n")
}

fn truncate_config_value(value: &str) -> String {
    if value.chars().count() <= 38 {
        return value.to_string();
    }
    value.chars().take(35).collect::<String>() + "..."
}

fn python_platform_name() -> &'static str {
    match std::env::consts::OS {
        "windows" => "win32",
        "macos" => "darwin",
        platform => platform,
    }
}

fn text_result(exit_code: i32, stdout: String) -> CommandResult {
    CommandResult {
        exit_code,
        stdout,
        stderr: String::new(),
        route: RouteUsed::None,
    }
}

fn emit_result(result: CommandResult) {
    if !result.stdout.is_empty() {
        println!("{}", result.stdout);
    }
    if !result.stderr.is_empty() {
        eprintln!("{}", result.stderr);
    }
    if result.exit_code != 0 {
        std::process::exit(result.exit_code);
    }
}

/// 获取子命令的命令行名称（kebab-case）
fn command_name(cmd: &Commands) -> &'static str {
    use Commands::*;
    match cmd {
        Guardrail => "guardrail",
        Impact { .. } => "impact",
        Review => "review",
        Evolution => "evolution",
        Hotspot => "hotspot",
        Churn => "churn",
        Defect => "defect",
        Task => "task",
        VulnBlast => "vuln-blast",
        SymbolHistory => "symbol-history",
        CheckGate => "check-gate",
        TestImpact => "test-impact",
        Gc => "gc",
        Doctor => "doctor",
        InstallAgent => "install-agent",
        InstallHook => "install-hook",
        Rule => "rule",
        Audit => "audit",
        Bootstrap => "bootstrap",
        Clone => "clone",
        Fts => "fts",
        Workspace => "workspace",
        Refresh { .. } => "refresh",
        Stats => "stats",
        Status => "status",
        Search { .. } => "search",
        Grep { .. } => "grep",
        Symbol { .. } => "symbol",
        File { .. } => "file",
        Query { .. } => "query",
        Issues { .. } => "issues",
        Tests { .. } => "tests",
        Callers { .. } => "callers",
        Callees { .. } => "callees",
        CallChain { .. } => "call-chain",
        Topo { .. } => "topo",
        Metrics => "metrics",
        Complexity => "complexity",
        Coupling => "coupling",
        CommentCoverage => "comment-coverage",
        Uncommented => "uncommented",
        FunctionIssues => "function-issues",
        LargestFns => "largest-fns",
        CoupledFns => "coupled-fns",
        FnMetrics => "fn-metrics",
        Git => "git",
        Semgrep => "semgrep",
        Coverage => "coverage",
        Who => "who",
        OwnershipMap => "ownership-map",
        Brief => "brief",
        Map => "map",
        HealthReport => "health-report",
        BuildContext => "build-context",
        Toolchain => "toolchain",
        Graph => "graph",
        Config { .. } => "config",
        Dashboard => "dashboard",
        Rollback => "rollback",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_command_count() {
        // 验证 59 个子命令
        // 通过 command_name 的 match 分支数间接验证
        // 每个 Commands 变体都有对应的 command_name 分支
        let variants: Vec<Commands> = vec![
            Commands::Guardrail,
            Commands::Impact {
                symbol_hash: "sym-a".to_string(),
                depth: 3,
            },
            Commands::Review,
            Commands::Evolution,
            Commands::Hotspot,
            Commands::Churn,
            Commands::Defect,
            Commands::Task,
            Commands::VulnBlast,
            Commands::SymbolHistory,
            Commands::CheckGate,
            Commands::TestImpact,
            Commands::Gc,
            Commands::Doctor,
            Commands::InstallAgent,
            Commands::InstallHook,
            Commands::Rule,
            Commands::Audit,
            Commands::Bootstrap,
            Commands::Clone,
            Commands::Fts,
            Commands::Workspace,
            Commands::Refresh {
                all: false,
                force: false,
                paths: vec!["src/lib.rs".into()],
            },
            Commands::Stats,
            Commands::Status,
            Commands::Search {
                query: "alpha".to_string(),
                kind: None,
                limit: 50,
            },
            Commands::Grep {
                patterns: vec!["needle".to_string()],
                fixed: false,
                limit: 200,
                path: None,
                include_all: false,
                kind: None,
            },
            Commands::Symbol {
                name: "a.alpha".to_string(),
            },
            Commands::File {
                path: "a.py".to_string(),
            },
            Commands::Query {
                name: "alpha".to_string(),
                file: "a.py".to_string(),
            },
            Commands::Issues {
                qualified_name: "a.alpha".to_string(),
                include_info: false,
            },
            Commands::Tests {
                qualified_name: Some("a.alpha".to_string()),
                reverse: false,
                build: false,
                force: false,
                history: false,
                import_file: None,
                ci_run_id: String::new(),
                ci_url: String::new(),
                limit: 50,
            },
            Commands::Callers {
                name: "beta".to_string(),
                qualified: None,
            },
            Commands::Callees {
                name: "alpha".to_string(),
                qualified: None,
            },
            Commands::CallChain {
                name: "a.alpha".to_string(),
                depth: 10,
            },
            Commands::Topo { limit: 50 },
            Commands::Metrics,
            Commands::Complexity,
            Commands::Coupling,
            Commands::CommentCoverage,
            Commands::Uncommented,
            Commands::FunctionIssues,
            Commands::LargestFns,
            Commands::CoupledFns,
            Commands::FnMetrics,
            Commands::Git,
            Commands::Semgrep,
            Commands::Coverage,
            Commands::Who,
            Commands::OwnershipMap,
            Commands::Brief,
            Commands::Map,
            Commands::HealthReport,
            Commands::BuildContext,
            Commands::Toolchain,
            Commands::Graph,
            Commands::Config {
                action: ConfigAction::Explain,
            },
            Commands::Dashboard,
            Commands::Rollback,
        ];
        assert_eq!(variants.len(), 59, "应有 59 个子命令");
    }

    #[test]
    fn test_command_name_kebab_case() {
        // 验证 kebab-case 转换
        assert_eq!(command_name(&Commands::VulnBlast), "vuln-blast");
        assert_eq!(command_name(&Commands::SymbolHistory), "symbol-history");
        assert_eq!(command_name(&Commands::CheckGate), "check-gate");
        assert_eq!(command_name(&Commands::TestImpact), "test-impact");
        assert_eq!(command_name(&Commands::InstallAgent), "install-agent");
        assert_eq!(command_name(&Commands::InstallHook), "install-hook");
        assert_eq!(
            command_name(&Commands::CallChain {
                name: "a.alpha".to_string(),
                depth: 10,
            }),
            "call-chain"
        );
        assert_eq!(command_name(&Commands::CommentCoverage), "comment-coverage");
        assert_eq!(command_name(&Commands::FunctionIssues), "function-issues");
        assert_eq!(command_name(&Commands::LargestFns), "largest-fns");
        assert_eq!(command_name(&Commands::CoupledFns), "coupled-fns");
        assert_eq!(command_name(&Commands::FnMetrics), "fn-metrics");
        assert_eq!(command_name(&Commands::OwnershipMap), "ownership-map");
        assert_eq!(command_name(&Commands::HealthReport), "health-report");
        assert_eq!(command_name(&Commands::BuildContext), "build-context");
    }

    #[test]
    fn test_command_name_simple() {
        // 验证简单名称（无连字符）
        assert_eq!(command_name(&Commands::Guardrail), "guardrail");
        assert_eq!(command_name(&Commands::Task), "task");
        assert_eq!(command_name(&Commands::Gc), "gc");
        assert_eq!(
            command_name(&Commands::Search {
                query: "alpha".to_string(),
                kind: None,
                limit: 50,
            }),
            "search"
        );
        assert_eq!(
            command_name(&Commands::Config {
                action: ConfigAction::Explain,
            }),
            "config"
        );
        assert_eq!(command_name(&Commands::Rollback), "rollback");
    }

    #[test]
    fn enterprise_status_calls_registry_then_snapshot_stats() {
        let mut methods = Vec::new();
        let result = query_enterprise_status("ws-1", |method, params| {
            methods.push(method.to_string());
            assert_eq!(params["workspace_instance_id"], "ws-1");
            match method {
                "workspace.status" => Ok(serde_json::json!({"status": "active"})),
                "query.stats" => Ok(serde_json::json!({"symbol_count": 7})),
                _ => panic!("unexpected method: {method}"),
            }
        })
        .unwrap();

        assert_eq!(methods, vec!["workspace.status", "query.stats"]);
        assert_eq!(result["workspace_registry"]["status"], "active");
        assert_eq!(result["graph_stats"]["symbol_count"], 7);
    }

    #[test]
    fn enterprise_status_fails_when_snapshot_stats_fail() {
        let error = query_enterprise_status("ws-1", |method, _params| match method {
            "workspace.status" => Ok(serde_json::json!({"status": "active"})),
            "query.stats" => Err("snapshot_not_ready".to_string()),
            _ => panic!("unexpected method: {method}"),
        })
        .unwrap_err();

        assert_eq!(error, "snapshot_not_ready");
    }

    #[test]
    fn enterprise_search_builds_rpc_and_normalizes_snapshot_fields() {
        let result = query_enterprise_search("ws-1", "alpha", Some("fn"), 7, |method, params| {
            assert_eq!(method, "query.search");
            assert_eq!(params["workspace_instance_id"], "ws-1");
            assert_eq!(params["query"], "alpha");
            assert_eq!(params["kind"], "fn");
            assert_eq!(params["limit"], 7);
            Ok(serde_json::json!([{
                "qualified_name": "a.alpha",
                "kind": "fn",
                "depth": 0,
                "start_line": 1,
                "file_rel_path": "a.py"
            }]))
        })
        .unwrap();

        assert_eq!(result[0]["file_path"], "a.py");
        assert_eq!(result[0]["signature"], "");
        assert_eq!(result[0]["has_comment"], false);
    }

    #[test]
    fn enterprise_symbol_builds_rpc_and_preserves_full_detail() {
        let result = query_enterprise_symbol("ws-1", "a.alpha", |method, params| {
            assert_eq!(method, "query.symbol");
            assert_eq!(params["workspace_instance_id"], "ws-1");
            assert_eq!(params["qualified_name"], "a.alpha");
            Ok(serde_json::json!({
                "qualified_name": "a.alpha",
                "kind": "fn",
                "depth": 0,
                "file_path": "a.py",
                "start_line": 1,
                "end_line": 2,
                "signature": "alpha()",
                "has_comment": true,
                "comment_content": "docs",
                "calls_out": [{"target_name": "a.beta", "call_line": 2}],
                "called_by": [],
                "issues": [],
                "issues_total": 0
            }))
        })
        .unwrap();

        assert_eq!(result["qualified_name"], "a.alpha");
        assert_eq!(result["calls_out"][0]["target_name"], "a.beta");
    }

    #[test]
    fn enterprise_callers_auto_qname_falls_back_once() {
        let mut calls = 0;
        let result = query_enterprise_graph("ws-1", "callers", "a.beta", None, |method, params| {
            calls += 1;
            assert_eq!(method, "query.callers");
            assert_eq!(params["workspace_instance_id"], "ws-1");
            assert_eq!(params["callee_name"], "beta");
            if calls == 1 {
                assert_eq!(params["qualified_name"], "a.beta");
                Ok(serde_json::json!([]))
            } else {
                assert!(params.get("qualified_name").is_none());
                Ok(serde_json::json!([{
                    "caller_name": "alpha",
                    "caller_file": "a.py",
                    "call_line": 3,
                    "is_cross_file": false
                }]))
            }
        })
        .unwrap();

        assert_eq!(calls, 2);
        assert_eq!(result[0]["caller_name"], "alpha");
        assert_eq!(result[0]["call_line"], 3);
    }

    #[test]
    fn enterprise_call_chain_builds_bounded_rpc_and_normalizes_levels() {
        let result = query_enterprise_call_chain("ws-1", "a.alpha", 500, |method, params| {
            assert_eq!(method, "query.call_chain_down");
            assert_eq!(params["workspace_instance_id"], "ws-1");
            assert_eq!(params["qualified_name"], "a.alpha");
            assert_eq!(params["max_depth"], MAX_CALL_CHAIN_DEPTH);
            Ok(serde_json::json!([{
                "depth": 0,
                "caller_name": "alpha",
                "caller_qualified": "a.alpha",
                "callee_name": "beta",
                "callee_qualified": "a.beta",
                "callee_id": 2
            }]))
        })
        .unwrap();

        assert_eq!(result["total_downstream"], 1);
        assert_eq!(result["levels"][0]["depth"], 1);
        assert_eq!(result["levels"][0]["callees"][0]["callee"], "a.beta");
    }

    #[test]
    fn enterprise_impact_builds_bounded_rpc() {
        let result = query_enterprise_impact("ws-1", "hash-a", 500, |method, params| {
            assert_eq!(method, "query.impact");
            assert_eq!(params["workspace_instance_id"], "ws-1");
            assert_eq!(params["symbol_hash"], "hash-a");
            assert_eq!(params["depth"], MAX_IMPACT_DEPTH);
            Ok(serde_json::json!({
                "source_symbol": "a.alpha",
                "source_hash": "hash-a",
                "depth": 500,
                "layers": [],
                "total_impacted": 1,
                "by_layer": {"code": 0, "db": 0, "api": 0, "config": 0}
            }))
        })
        .unwrap();
        assert_eq!(result["source_symbol"], "a.alpha");
        assert_eq!(result["depth"], 500);
    }

    #[test]
    fn enterprise_topo_requests_details_and_preserves_python_fields() {
        let result = query_enterprise_topological_order("ws-1", 7, |method, params| {
            assert_eq!(method, "query.topological_order");
            assert_eq!(params["workspace_instance_id"], "ws-1");
            assert_eq!(params["limit"], 7);
            assert_eq!(params["detail"], true);
            Ok(serde_json::json!([{
                "qualified_name": "a.alpha",
                "name": "alpha",
                "path": "a.py",
                "start_line": 1,
                "depth": 0
            }]))
        })
        .unwrap();

        assert_eq!(result[0]["qualified_name"], "a.alpha");
        assert_eq!(result[0]["path"], "a.py");
    }

    #[test]
    fn parses_call_chain_and_topo_arguments() {
        let chain =
            Cli::try_parse_from(["cw", "call-chain", "a.alpha", "--depth", "3"]).unwrap();
        assert!(matches!(
            chain.command,
            Some(Commands::CallChain { name, depth }) if name == "a.alpha" && depth == 3
        ));

        let topo = Cli::try_parse_from(["cw", "topo", "--limit", "12"]).unwrap();
        assert!(matches!(
            topo.command,
            Some(Commands::Topo { limit }) if limit == 12
        ));

        let impact = Cli::try_parse_from(["cw", "impact", "hash-a", "--depth", "4"]).unwrap();
        assert!(matches!(
            impact.command,
            Some(Commands::Impact { symbol_hash, depth })
                if symbol_hash == "hash-a" && depth == 4
        ));
    }

    #[test]
    fn parses_refresh_paths_and_full_flags() {
        let paths = Cli::try_parse_from(["cw", "refresh", "src/a.rs", "src/b.rs"]).unwrap();
        assert!(matches!(
            paths.command,
            Some(Commands::Refresh { all: false, force: false, paths })
                if paths == vec![PathBuf::from("src/a.rs"), PathBuf::from("src/b.rs")]
        ));

        let full = Cli::try_parse_from(["cw", "refresh", "--all", "--force"]).unwrap();
        assert!(matches!(
            full.command,
            Some(Commands::Refresh { all: true, force: true, paths }) if paths.is_empty()
        ));
    }

    #[test]
    fn refresh_full_mode_stays_fail_closed() {
        let runtime = RuntimeOptions::from_overrides(
            Some(DaemonMode::Local),
            None,
            Some(PathBuf::from("unused.db")),
            None,
            1,
        );
        let result = run_refresh(&runtime, true, false, &[]);
        assert_eq!(result.exit_code, 1);
        assert!(result.stderr.contains("not migrated yet"));
    }

    #[test]
    fn parses_symbol_qualified_name() {
        let cli = Cli::try_parse_from(["cw", "symbol", "a.alpha"]).unwrap();
        assert!(matches!(
            cli.command,
            Some(Commands::Symbol { name }) if name == "a.alpha"
        ));
    }

    #[test]
    fn parses_file_and_query_arguments() {
        let file = Cli::try_parse_from(["cw", "file", "src/a.py"]).unwrap();
        assert!(matches!(
            file.command,
            Some(Commands::File { path }) if path == "src/a.py"
        ));

        let query = Cli::try_parse_from(["cw", "query", "alpha", "src/a.py"]).unwrap();
        assert!(matches!(
            query.command,
            Some(Commands::Query { name, file })
                if name == "alpha" && file == "src/a.py"
        ));
    }

    #[test]
    fn parses_callers_and_callees_arguments() {
        let callers =
            Cli::try_parse_from(["cw", "callers", "beta", "--qualified", "a.beta"]).unwrap();
        assert!(matches!(
            callers.command,
            Some(Commands::Callers {
                name,
                qualified: Some(qualified),
            }) if name == "beta" && qualified == "a.beta"
        ));

        let callees = Cli::try_parse_from(["cw", "callees", "a.alpha"]).unwrap();
        assert!(matches!(
            callees.command,
            Some(Commands::Callees {
                name,
                qualified: None,
            }) if name == "a.alpha"
        ));
    }

    #[test]
    fn parses_all_grep_options() {
        let cli = Cli::try_parse_from([
            "cw",
            "grep",
            "import",
            "time",
            "--fixed",
            "--limit",
            "7",
            "--path",
            "src",
            "--include-all",
            "--kind",
            "fn",
        ])
        .unwrap();
        assert!(matches!(
            cli.command,
            Some(Commands::Grep {
                patterns,
                fixed: true,
                limit: 7,
                path: Some(path),
                include_all: true,
                kind: Some(kind),
            }) if patterns == ["import", "time"]
                && path == PathBuf::from("src")
                && kind == "fn"
        ));
    }

    #[test]
    fn parses_issues_and_tests_read_options() {
        let issues = Cli::try_parse_from(["cw", "issues", "a.alpha", "--include-info"]).unwrap();
        assert!(matches!(
            issues.command,
            Some(Commands::Issues {
                qualified_name,
                include_info: true,
            }) if qualified_name == "a.alpha"
        ));

        let tests = Cli::try_parse_from([
            "cw",
            "tests",
            "a.alpha",
            "--reverse",
            "--history",
            "--limit",
            "7",
        ])
        .unwrap();
        assert!(matches!(
            tests.command,
            Some(Commands::Tests {
                qualified_name: Some(qualified_name),
                reverse: true,
                history: true,
                limit: 7,
                ..
            }) if qualified_name == "a.alpha"
        ));
    }

    #[test]
    fn tests_write_modes_fail_closed() {
        let runtime = RuntimeOptions::from_overrides(Some(DaemonMode::Local), None, None, None, 30);
        let result = run_tests(&runtime, None, false, true, false, false, None, "", "", 50);
        assert_eq!(result.exit_code, 2);
        assert!(result.stderr.contains("write modes"));
    }

    #[test]
    fn parses_global_runtime_options_before_command() {
        let cli = Cli::try_parse_from([
            "cw",
            "--mode",
            "local",
            "--db",
            "/tmp/callwarden.db",
            "--workspace-id",
            "17",
            "--timeout",
            "9",
            "stats",
        ])
        .unwrap();
        assert!(matches!(cli.mode, Some(ModeArg::Local)));
        assert_eq!(cli.db, Some(PathBuf::from("/tmp/callwarden.db")));
        assert_eq!(cli.workspace_id.as_deref(), Some("17"));
        assert_eq!(cli.timeout, 9);
        assert!(matches!(cli.command, Some(Commands::Stats)));
    }

    #[test]
    fn parses_global_runtime_options_after_command() {
        let cli = Cli::try_parse_from([
            "cw",
            "stats",
            "--mode",
            "enterprise",
            "--socket",
            "/tmp/callwarden.sock",
        ])
        .unwrap();
        assert!(matches!(cli.mode, Some(ModeArg::Enterprise)));
        assert_eq!(cli.socket, Some(PathBuf::from("/tmp/callwarden.sock")));
        assert!(matches!(cli.command, Some(Commands::Stats)));
    }
}
