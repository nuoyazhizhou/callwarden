//! cw CLI binary（Phase 5-1 A.2）
//!
//! clap 命令树对齐 Python `cli/main.py:_SUBCOMMANDS` 的 59 个子命令。
//! 命令按迁移清单逐项接入真实 Rust 业务逻辑，未迁移项仍显式失败。
//!
//! 契约：docs/design/phase5-1-cli-config-contract.md §3.2

use std::path::PathBuf;

use callwarden_core::cli::config::{check_role_supported, load_config, ConfigEntry, PlatformPaths};
use callwarden_core::cli::router::DaemonMode;
use callwarden_core::cli::runtime::{CommandResult, RouteUsed, RuntimeOptions};
use callwarden_core::cli::search::{
    format_search_output, normalize_search_results, query_local_search,
};
use callwarden_core::cli::stats::query_local_stats;
use callwarden_core::cli::status::{combine_enterprise_status, query_local_status};
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
    Impact,
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
    Refresh,
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
    /// grep 搜索
    Grep,
    /// 符号查询
    Symbol,
    /// 文件读取
    File,
    /// 查询
    Query,
    /// 符号问题
    Issues,
    /// 测试用例
    Tests,
    /// 调用者
    Callers,
    /// 被调用者
    Callees,
    /// 调用链
    CallChain,
    /// 拓扑排序
    Topo,
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
        Impact => "impact",
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
        Refresh => "refresh",
        Stats => "stats",
        Status => "status",
        Search { .. } => "search",
        Grep => "grep",
        Symbol => "symbol",
        File => "file",
        Query => "query",
        Issues => "issues",
        Tests => "tests",
        Callers => "callers",
        Callees => "callees",
        CallChain => "call-chain",
        Topo => "topo",
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
            Commands::Impact,
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
            Commands::Refresh,
            Commands::Stats,
            Commands::Status,
            Commands::Search {
                query: "alpha".to_string(),
                kind: None,
                limit: 50,
            },
            Commands::Grep,
            Commands::Symbol,
            Commands::File,
            Commands::Query,
            Commands::Issues,
            Commands::Tests,
            Commands::Callers,
            Commands::Callees,
            Commands::CallChain,
            Commands::Topo,
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
        assert_eq!(command_name(&Commands::CallChain), "call-chain");
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
