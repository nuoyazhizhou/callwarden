//! cw CLI binary（Phase 5-1 A.2）
//!
//! clap 命令树对齐 Python `cli/main.py:_SUBCOMMANDS` 的 59 个子命令。
//! 命令按迁移清单逐项接入真实 Rust 业务逻辑，未迁移项仍显式失败。
//!
//! 契约：docs/design/phase5-1-cli-config-contract.md §3.2

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Instant;

use callwarden_core::cli::build_context::{
    compute_resolved_edges, compute_resolved_edges_for_external_context, import_compile_commands,
    prepare_toolchain_registration, AggregatedBuildContext, ResolvedEdgesResult,
    ToolchainRegistration,
};
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
    normalize_enterprise_topological_order, query_local_call_chain, query_local_topological_order,
    MAX_CALL_CHAIN_DEPTH,
};
use callwarden_core::cli::grep::{query_local_grep, GrepOptions};
use callwarden_core::cli::impact::{format_impact_output, query_local_impact, MAX_IMPACT_DEPTH};
use callwarden_core::cli::issues_tests::{
    format_issues_output, format_test_cases_output, format_test_stability_output,
    format_tested_functions_output, query_local_issues, query_local_test_cases,
    query_local_test_stability, query_local_tested_functions,
};
use callwarden_core::cli::refresh::{
    build_enterprise_delete_params, build_enterprise_refresh_params,
    build_enterprise_refresh_plan_params, connect_params, enterprise_batch_result,
    format_full_refresh_output, format_refresh_output, new_cli_session_id, prepare_enterprise_file,
    prepare_enterprise_manifest, refresh_full_workspace, refresh_local_paths, FullRefreshResult,
    RefreshFileResult,
};
use callwarden_core::cli::router::DaemonMode;
use callwarden_core::cli::runtime::{CommandResult, RouteUsed, RuntimeOptions};
use callwarden_core::cli::search::{
    format_search_output, normalize_search_results, query_local_search,
};
use callwarden_core::cli::stats::query_local_stats;
use callwarden_core::cli::status::{combine_enterprise_status, query_local_status};
use callwarden_core::cli::symbol::format_symbol_output;
use callwarden_core::cli::task::{
    apply_task, capture_task_diff, capture_task_diff_auto, claim_next_task_step, close_task,
    create_task, format_task_apply, format_task_capture, format_task_claim, format_task_close,
    format_task_completion_review, format_task_create, format_task_finding_resolution,
    format_task_findings, format_task_list, format_task_reopen, format_task_report,
    format_task_rollback, format_task_show, format_task_split, query_task_detail,
    query_task_findings, query_task_links, query_task_list, reopen_task, report_task_step,
    resolve_task_finding, review_task_completion, rollback_task, split_task_from_plan,
    TaskListOptions, TaskStepInput,
};
use callwarden_core::cli::workspace::{
    activate_local_workspace, format_activate_result, format_register_success,
    format_remove_result, format_workspace_list, get_local_workspace, list_local_workspaces,
    register_local_workspace, remove_local_workspace, workspace_record_json, WorkspaceRecord,
};
use callwarden_core::daemon::toolchain::{ResolvedEdgeInput, ToolchainStore};
use callwarden_core::symbol_query::query_symbol_detail;
use clap::{Parser, Subcommand, ValueEnum};
use serde_json::Value;

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
    #[arg(long = "workspace-id", global = true)]
    enterprise_workspace_id: Option<String>,

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
    Task {
        #[command(subcommand)]
        action: TaskAction,
    },
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
    Workspace {
        #[command(subcommand)]
        action: WorkspaceAction,
    },
    /// 刷新数据库
    Refresh {
        /// 全仓刷新
        #[arg(long)]
        all: bool,
        /// 强制重新解析全部扫描文件，只能与 --all 一起使用
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
    BuildContext {
        #[command(subcommand)]
        action: BuildContextAction,
    },
    /// 工具链指纹
    Toolchain {
        #[command(subcommand)]
        action: ToolchainAction,
    },
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

#[derive(Subcommand)]
enum WorkspaceAction {
    /// 列出当前数据源可见的工作区
    List,
    /// 注册工作区
    Register {
        name: String,
        root: PathBuf,
        #[arg(long, default_value = "")]
        description: String,
        #[arg(long, default_value = "")]
        git_remote_url: String,
        #[arg(long, default_value = "")]
        git_head_commit_sha: String,
        #[arg(long, default_value = "")]
        toolchain_fingerprint: String,
    },
    /// 查询工作区状态；省略标识时使用全局 --workspace-id 或本地 active workspace
    Status { id_or_name: Option<String> },
    /// 激活工作区（兼容 Python `workspace set`）
    #[command(alias = "set")]
    Activate { id_or_name: String },
    /// 移除工作区（兼容 Python `workspace delete`）
    #[command(alias = "delete")]
    Remove { id_or_name: String },
}

#[derive(Subcommand)]
enum TaskAction {
    /// 创建任务和步骤
    Create {
        #[arg(long)]
        title: String,
        #[arg(long, default_value = "")]
        desc: String,
        /// JSON 步骤数组
        #[arg(long, default_value = "")]
        steps: String,
    },
    /// 原子领取任务树中的下一个步骤
    Next { task_id: String },
    /// 回报步骤执行结果
    Report {
        task_id: String,
        step_id: String,
        #[arg(long, default_value = "")]
        result: String,
        #[arg(long)]
        fail: bool,
    },
    /// 记录步骤或 change_audit 范围的回滚意图
    Rollback { task_id: String, step_id: String },
    /// 捕获工作区真实 diff 到任务审计证据
    CaptureDiff {
        task_id: Option<String>,
        #[arg(long, default_value = "")]
        step_id: String,
        #[arg(long, default_value = "")]
        base: String,
        #[arg(long)]
        dry_run: bool,
        #[arg(long)]
        auto: bool,
        #[arg(long)]
        skip_quality_review: bool,
        #[arg(long, default_value = "")]
        source_commit_hash: String,
    },
    /// 汇总任务当前 open findings 并给出完成决策
    CompletionReview {
        task_id: String,
        #[arg(long, default_value = "")]
        step_id: String,
    },
    /// 原子解决或豁免一条任务 finding
    ResolveFinding {
        finding_id: i64,
        #[arg(long, default_value = "fixed")]
        resolution: String,
        #[arg(long = "by", default_value = "agent")]
        resolved_by: String,
    },
    /// 独立 reviewer 审核通过叶子任务
    Apply {
        task_id: String,
        #[arg(long, default_value = "reviewer")]
        reviewer: String,
    },
    /// 独立 reviewer 关闭已 applied 叶子任务
    Close {
        task_id: String,
        #[arg(long, default_value = "reviewer")]
        reviewer: String,
    },
    /// 从 Markdown 计划原子拆分父子任务树
    Split {
        task_id: String,
        #[arg(long)]
        plan: PathBuf,
    },
    /// 重新打开 review/applied/closed 任务
    Reopen {
        task_id: String,
        #[arg(long, default_value = "reviewer")]
        reviewer: String,
        #[arg(long, default_value = "")]
        reason: String,
    },
    /// 列出任务
    List {
        /// 只显示自身或后代存在阻塞 finding 的任务
        #[arg(long)]
        blocked: bool,
        /// 最大查询数量
        #[arg(long, default_value_t = 200)]
        limit: usize,
        /// 任务状态过滤
        #[arg(long, default_value = "")]
        status: String,
        /// 扁平展示，不按父子层级缩进
        #[arg(long)]
        flat: bool,
    },
    /// 查看任务详情，默认递归展示子任务
    Show {
        task_id: String,
        /// 只展示当前任务
        #[arg(long)]
        flat: bool,
    },
    /// 查看任务状态树
    StatusTree { task_id: String },
    /// 查看任务质量发现
    Findings {
        task_id: String,
        /// open/resolved/wontfix/all
        #[arg(long, default_value = "open")]
        status: String,
        /// info/warn/error/block
        #[arg(long, default_value = "")]
        severity: String,
    },
}

#[derive(Subcommand)]
enum ToolchainAction {
    /// 注册工具链
    Register {
        name: String,
        compiler_path: PathBuf,
        #[arg(long)]
        sysroot: Option<PathBuf>,
        #[arg(long, default_value = "")]
        description: String,
        #[arg(long)]
        no_probe: bool,
    },
    /// 列出全部工具链
    List,
    /// 显示工具链详情
    Show { name_or_id: String },
    /// 删除工具链
    Delete { name_or_id: String },
    /// 绑定工具链到 workspace
    Bind {
        workspace_id: i64,
        toolchain_name: String,
        #[arg(long, default_value = "")]
        build_context_hash: String,
    },
    /// 列出 workspace 绑定的工具链
    ListBound {
        workspace_id: i64,
        #[arg(long, default_value = "")]
        build_context_hash: String,
    },
}

#[derive(Subcommand)]
enum BuildContextAction {
    /// 注册 build context
    Register {
        workspace_id: i64,
        name: String,
        #[arg(long, num_args = 0..)]
        flags: Vec<String>,
        #[arg(long, num_args = 0..)]
        defines: Vec<String>,
        #[arg(long, num_args = 0..)]
        includes: Vec<String>,
        #[arg(long)]
        activate: bool,
    },
    /// 列出 workspace 的 build context
    List { workspace_id: i64 },
    /// 显示 build context
    Show { workspace_id: i64, hash: String },
    /// 激活 build context
    Activate { workspace_id: i64, hash: String },
    /// 删除 build context
    Delete { workspace_id: i64, hash: String },
    /// 从 compile_commands.json 导入
    ImportCompileCommands {
        file: PathBuf,
        workspace_id: i64,
        #[arg(long, default_value = "imported")]
        name: String,
        #[arg(long)]
        activate: bool,
        #[arg(long)]
        workspace_root: Option<PathBuf>,
    },
    /// 重新计算 resolved edge 缓存
    Resolve { workspace_id: i64, hash: String },
    /// 查询 resolved edge
    Edges {
        workspace_id: i64,
        hash: String,
        #[arg(long)]
        caller: Option<i64>,
        #[arg(long, default_value_t = 50)]
        limit: usize,
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
        cli.enterprise_workspace_id,
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
                Commands::Workspace { action } => {
                    emit_result(run_workspace(&runtime, action));
                }
                Commands::Toolchain { action } => {
                    emit_result(run_toolchain(&runtime, action));
                }
                Commands::BuildContext { action } => {
                    emit_result(run_build_context(&runtime, action));
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
                Commands::Task { action } => {
                    emit_result(run_task(&runtime, action));
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

fn run_task(runtime: &RuntimeOptions, action: TaskAction) -> CommandResult {
    let result = (|| -> Result<String, String> {
        // task 是 per-UID 编排元数据；即使代码查询使用 enterprise，也固定读取本地库。
        let zh_cn = use_zh_cn();
        match action {
            TaskAction::Create { title, desc, steps } => {
                let steps = if steps.is_empty() {
                    Vec::new()
                } else {
                    serde_json::from_str::<Vec<TaskStepInput>>(&steps)
                        .map_err(|error| format!("invalid task steps JSON: {error}"))?
                };
                let mut conn = runtime.open_local_write_db()?;
                let result = create_task(&mut conn, &title, &desc, steps, "agent", None)?;
                Ok(format_task_create(&result, zh_cn))
            }
            TaskAction::Next { task_id } => {
                let mut conn = runtime.open_local_write_db()?;
                let step = claim_next_task_step(&mut conn, &task_id)?;
                Ok(format_task_claim(&task_id, step.as_ref(), zh_cn))
            }
            TaskAction::Report {
                task_id,
                step_id,
                result,
                fail,
            } => {
                let mut conn = runtime.open_local_write_db()?;
                let report = report_task_step(&mut conn, &task_id, &step_id, &result, !fail)?;
                Ok(format_task_report(&report, &result, zh_cn))
            }
            TaskAction::Rollback { task_id, step_id } => {
                let mut conn = runtime.open_local_write_db()?;
                let result = rollback_task(&mut conn, &task_id, &step_id, "")?;
                Ok(format_task_rollback(&result, zh_cn))
            }
            TaskAction::CaptureDiff {
                task_id,
                step_id,
                base,
                dry_run,
                auto,
                skip_quality_review,
                source_commit_hash,
            } => {
                let mut conn = runtime.open_local_write_db()?;
                if auto {
                    if task_id.is_some() {
                        return Err("task_id cannot be combined with --auto".to_string());
                    }
                    let result = capture_task_diff_auto(&mut conn, Path::new(""));
                    return Ok(format_task_capture(&result, zh_cn));
                }
                let task_id = task_id.ok_or_else(|| {
                    "task_id is required for capture-diff unless --auto is used".to_string()
                })?;
                let result = capture_task_diff(
                    &mut conn,
                    &task_id,
                    &step_id,
                    Path::new(""),
                    &base,
                    dry_run,
                    &source_commit_hash,
                    skip_quality_review,
                )?;
                Ok(format_task_capture(&result, zh_cn))
            }
            TaskAction::CompletionReview { task_id, step_id } => {
                let conn = runtime.open_local_db()?;
                let result = review_task_completion(&conn, &task_id, &step_id)?;
                Ok(format_task_completion_review(&result, zh_cn))
            }
            TaskAction::ResolveFinding {
                finding_id,
                resolution,
                resolved_by,
            } => {
                let mut conn = runtime.open_local_write_db()?;
                let result =
                    resolve_task_finding(&mut conn, finding_id, &resolution, &resolved_by)?;
                Ok(format_task_finding_resolution(&result, zh_cn))
            }
            TaskAction::Apply { task_id, reviewer } => {
                let mut conn = runtime.open_local_write_db()?;
                let result = apply_task(&mut conn, &task_id, &reviewer)?;
                Ok(format_task_apply(&result, zh_cn))
            }
            TaskAction::Close { task_id, reviewer } => {
                let mut conn = runtime.open_local_write_db()?;
                let result = close_task(&mut conn, &task_id, &reviewer)?;
                Ok(format_task_close(&result, zh_cn))
            }
            TaskAction::Split { task_id, plan } => {
                let plan_markdown = std::fs::read_to_string(&plan).map_err(|error| {
                    format!("cannot read task split plan {}: {error}", plan.display())
                })?;
                let mut conn = runtime.open_local_write_db()?;
                let result = split_task_from_plan(&mut conn, &task_id, &plan_markdown)?;
                Ok(format_task_split(&result, zh_cn))
            }
            TaskAction::Reopen {
                task_id,
                reviewer,
                reason,
            } => {
                let mut conn = runtime.open_local_write_db()?;
                let result = reopen_task(&mut conn, &task_id, &reviewer, &reason)?;
                Ok(format_task_reopen(&result, zh_cn))
            }
            TaskAction::List {
                blocked,
                limit,
                status,
                flat,
            } => {
                let conn = runtime.open_local_db()?;
                let status = (!status.is_empty()).then_some(status);
                let options = TaskListOptions {
                    blocked,
                    limit,
                    status: status.clone(),
                    flat,
                };
                let tasks = query_task_list(&conn, status.as_deref(), limit)?;
                format_task_list(&tasks, &options, zh_cn)
            }
            TaskAction::Show { task_id, flat } => {
                let conn = runtime.open_local_db()?;
                let detail = query_task_detail(&conn, &task_id, !flat)?;
                let links = query_task_links(&conn, &task_id)?;
                Ok(format_task_show(
                    &task_id,
                    detail.as_ref(),
                    &links,
                    flat,
                    zh_cn,
                ))
            }
            TaskAction::StatusTree { task_id } => {
                let conn = runtime.open_local_db()?;
                let detail = query_task_detail(&conn, &task_id, true)?;
                let links = query_task_links(&conn, &task_id)?;
                Ok(format_task_show(
                    &task_id,
                    detail.as_ref(),
                    &links,
                    false,
                    zh_cn,
                ))
            }
            TaskAction::Findings {
                task_id,
                status,
                severity,
            } => {
                let conn = runtime.open_local_db()?;
                let findings = query_task_findings(&conn, &task_id, &status, &severity)?;
                Ok(format_task_findings(&task_id, &findings, zh_cn))
            }
        }
    })();
    match result {
        Ok(output) => CommandResult::success_text(output, RouteUsed::Local),
        Err(error) => CommandResult::failure(1, error, RouteUsed::Local),
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
        if !paths.is_empty() {
            return CommandResult::failure(
                1,
                "cw refresh --all does not accept explicit file paths".to_string(),
                RouteUsed::None,
            );
        }
        return runtime.execute_write_with(
            || {
                let conn = runtime.open_local_write_db()?;
                let workspace_id = runtime.resolve_local_workspace_id(&conn)?;
                let result = refresh_full_workspace(&conn, &runtime.db_path, workspace_id, force)?;
                Ok(format_full_refresh_output(&result))
            },
            || run_enterprise_full_refresh(runtime, force),
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

fn run_enterprise_full_refresh(runtime: &RuntimeOptions, force: bool) -> Result<String, String> {
    let workspace_id = runtime.workspace_id.as_deref().ok_or_else(|| {
        "enterprise refresh requires --workspace-id <workspace_instance_id>".to_string()
    })?;
    let started = Instant::now();
    let manifest = prepare_enterprise_manifest()?;
    let manifest_paths = manifest
        .iter()
        .map(|entry| (entry.rel_path.clone(), entry.absolute_path.clone()))
        .collect::<HashMap<_, _>>();
    let session_id = new_cli_session_id();
    let connect = runtime.daemon_call(
        "workspace.connect",
        connect_params(workspace_id, &session_id),
    )?;
    let session_epoch = connect
        .get("session_epoch")
        .and_then(serde_json::Value::as_u64)
        .ok_or_else(|| "workspace.connect response is missing session_epoch".to_string())?;
    const MANIFEST_CHUNK_FILES: usize = 5_000;
    let plan_id = format!("{session_id}-plan");
    let ranges = if manifest.is_empty() {
        vec![(0usize, 0usize)]
    } else {
        (0..manifest.len())
            .step_by(MANIFEST_CHUNK_FILES)
            .map(|start| (start, (start + MANIFEST_CHUNK_FILES).min(manifest.len())))
            .collect::<Vec<_>>()
    };
    let mut scanned = 0usize;
    let mut unchanged = 0usize;
    let mut refresh_paths = Vec::new();
    let mut delete_paths = Vec::new();
    for (chunk_index, (start, end)) in ranges.iter().copied().enumerate() {
        let complete = chunk_index + 1 == ranges.len();
        let plan = runtime.daemon_call(
            "workspace.refresh.plan",
            build_enterprise_refresh_plan_params(
                workspace_id,
                &manifest[start..end],
                force,
                &plan_id,
                chunk_index == 0,
                complete,
            ),
        )?;
        scanned = plan
            .get("scanned")
            .and_then(Value::as_u64)
            .ok_or_else(|| "workspace.refresh.plan response is missing scanned".to_string())?
            as usize;
        unchanged = plan
            .get("unchanged")
            .and_then(Value::as_u64)
            .ok_or_else(|| "workspace.refresh.plan response is missing unchanged".to_string())?
            as usize;
        refresh_paths.extend(
            plan.get("refresh_paths")
                .and_then(Value::as_array)
                .ok_or_else(|| {
                    "workspace.refresh.plan response is missing refresh_paths".to_string()
                })?
                .iter()
                .cloned(),
        );
        if complete {
            delete_paths.extend(
                plan.get("delete_paths")
                    .and_then(Value::as_array)
                    .ok_or_else(|| {
                        "workspace.refresh.plan response is missing delete_paths".to_string()
                    })?
                    .iter()
                    .cloned(),
            );
        }
    }

    let mut refreshed = 0usize;
    let mut deleted = 0usize;
    let mut failed = Vec::new();
    let mut seq = 0u64;
    for value in &refresh_paths {
        let rel_path = value.as_str().ok_or_else(|| {
            "workspace.refresh.plan returned a non-string refresh path".to_string()
        })?;
        let absolute_path = manifest_paths.get(rel_path).ok_or_else(|| {
            format!(
                "workspace.refresh.plan returned a path outside the submitted manifest: {rel_path}"
            )
        })?;
        seq += 1;
        let result = prepare_enterprise_file(absolute_path).and_then(
            |(prepared_rel_path, canonical_bytes)| {
                if prepared_rel_path != rel_path {
                    return Err(format!(
                        "manifest path changed during refresh: planned={rel_path} current={prepared_rel_path}"
                    ));
                }
                let response = runtime.daemon_call(
                    "workspace.file.refresh",
                    build_enterprise_refresh_params(
                        workspace_id,
                        rel_path,
                        &session_id,
                        session_epoch,
                        seq,
                        &canonical_bytes,
                    ),
                )?;
                let status = response.get("status").and_then(Value::as_str).unwrap_or("");
                if status != "committed" {
                    return Err(format!("daemon rejected refresh: status={status}"));
                }
                Ok(())
            },
        );
        match result {
            Ok(()) => refreshed += 1,
            Err(error) => failed.push(RefreshFileResult {
                input_path: rel_path.to_string(),
                success: false,
                status: "failed".to_string(),
                error: Some(error),
            }),
        }
    }
    for value in &delete_paths {
        let rel_path = value.as_str().ok_or_else(|| {
            "workspace.refresh.plan returned a non-string delete path".to_string()
        })?;
        seq += 1;
        let result = runtime.daemon_call(
            "workspace.file.delete",
            build_enterprise_delete_params(workspace_id, rel_path, &session_id, session_epoch, seq),
        );
        match result {
            Ok(response) if response.get("status").and_then(Value::as_str) == Some("deleted") => {
                deleted += 1;
            }
            Ok(response) => failed.push(RefreshFileResult {
                input_path: rel_path.to_string(),
                success: false,
                status: "failed".to_string(),
                error: Some(format!(
                    "daemon rejected delete: status={}",
                    response.get("status").and_then(Value::as_str).unwrap_or("")
                )),
            }),
            Err(error) => failed.push(RefreshFileResult {
                input_path: rel_path.to_string(),
                success: false,
                status: "failed".to_string(),
                error: Some(error),
            }),
        }
    }

    Ok(format_full_refresh_output(&FullRefreshResult {
        scanned,
        refreshed,
        unchanged,
        deleted,
        failed,
        elapsed_seconds: started.elapsed().as_secs_f64(),
        force,
    }))
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

fn run_toolchain(runtime: &RuntimeOptions, action: ToolchainAction) -> CommandResult {
    match action {
        ToolchainAction::Register {
            name,
            compiler_path,
            sysroot,
            description,
            no_probe,
        } => {
            let registration = match prepare_toolchain_registration(
                &name,
                &compiler_path,
                sysroot
                    .as_deref()
                    .unwrap_or_else(|| std::path::Path::new("")),
                &description,
                no_probe,
            ) {
                Ok(value) => value,
                Err(error) => return CommandResult::failure(1, error, RouteUsed::None),
            };
            let local_registration = registration.clone();
            let enterprise_registration = registration.clone();
            runtime.execute_write_with(
                || {
                    let store = open_local_toolchain_store(runtime, true)?;
                    let value = register_toolchain_in_store(&store, &local_registration)?;
                    format_toolchain_registered(&value)
                },
                || {
                    let value = runtime.daemon_call(
                        "toolchain.register",
                        toolchain_register_params(&enterprise_registration),
                    )?;
                    format_toolchain_registered(&value)
                },
            )
        }
        ToolchainAction::List => execute_workspace_read(
            runtime,
            || {
                let store = open_local_toolchain_store(runtime, false)?;
                let values = store.list_toolchains().map_err(toolchain_sql_error)?;
                format_toolchain_list(&Value::Array(values))
            },
            || {
                let value = runtime.daemon_call("toolchain.list", serde_json::json!({}))?;
                format_toolchain_list(&value)
            },
        ),
        ToolchainAction::Show { name_or_id } => {
            let local_name = name_or_id.clone();
            let enterprise_name = name_or_id.clone();
            execute_workspace_read(
                runtime,
                || {
                    let store = open_local_toolchain_store(runtime, false)?;
                    let value = store
                        .get_toolchain(&local_name)
                        .map_err(toolchain_sql_error)?
                        .ok_or_else(|| format!("Toolchain not found: {local_name}"))?;
                    format_toolchain_show(&value)
                },
                || {
                    let value = runtime.daemon_call(
                        "toolchain.get",
                        serde_json::json!({"name_or_id": enterprise_name}),
                    )?;
                    format_toolchain_show(&value)
                },
            )
        }
        ToolchainAction::Delete { name_or_id } => {
            let local_name = name_or_id.clone();
            let enterprise_name = name_or_id.clone();
            runtime.execute_write_with(
                || {
                    let store = open_local_toolchain_store(runtime, true)?;
                    let deleted = store
                        .delete_toolchain(&local_name)
                        .map_err(toolchain_sql_error)?;
                    if deleted == 0 {
                        Err(format!("Toolchain not found: {local_name}"))
                    } else {
                        Ok(format!("Toolchain deleted: {local_name}"))
                    }
                },
                || {
                    let value = runtime.daemon_call(
                        "toolchain.delete",
                        serde_json::json!({"name_or_id": enterprise_name}),
                    )?;
                    if value.get("deleted").and_then(Value::as_u64).unwrap_or(0) == 0 {
                        Err(format!("Toolchain not found: {enterprise_name}"))
                    } else {
                        Ok(format!("Toolchain deleted: {enterprise_name}"))
                    }
                },
            )
        }
        ToolchainAction::Bind {
            workspace_id,
            toolchain_name,
            build_context_hash,
        } => {
            let local_name = toolchain_name.clone();
            let enterprise_name = toolchain_name.clone();
            let local_hash = build_context_hash.clone();
            let enterprise_hash = build_context_hash.clone();
            runtime.execute_write_with(
                || {
                    let store = open_local_toolchain_store(runtime, true)?;
                    let toolchain = store
                        .get_toolchain(&local_name)
                        .map_err(toolchain_sql_error)?
                        .ok_or_else(|| format!("Toolchain not found: {local_name}"))?;
                    let id = require_json_i64(&toolchain, "id")?;
                    store
                        .bind_toolchain_to_workspace(workspace_id, id, &local_hash)
                        .map_err(toolchain_sql_error)?;
                    Ok(format!(
                        "Toolchain '{}' bound to workspace {}",
                        json_string(&toolchain, "name"),
                        workspace_id
                    ))
                },
                || {
                    let toolchain = runtime.daemon_call(
                        "toolchain.get",
                        serde_json::json!({"name_or_id": enterprise_name}),
                    )?;
                    let toolchain_id = require_json_i64(&toolchain, "id")?;
                    runtime.daemon_call(
                        "toolchain.bind",
                        serde_json::json!({
                            "workspace_id": workspace_id.to_string(),
                            "toolchain_id": toolchain_id.to_string(),
                            "build_context_hash": enterprise_hash,
                        }),
                    )?;
                    Ok(format!(
                        "Toolchain '{}' bound to workspace {}",
                        json_string(&toolchain, "name"),
                        workspace_id
                    ))
                },
            )
        }
        ToolchainAction::ListBound {
            workspace_id,
            build_context_hash,
        } => {
            let local_hash = build_context_hash.clone();
            let enterprise_hash = build_context_hash.clone();
            execute_workspace_read(
                runtime,
                || {
                    let store = open_local_toolchain_store(runtime, false)?;
                    let values = store
                        .get_workspace_toolchains(workspace_id, Some(&local_hash))
                        .map_err(toolchain_sql_error)?;
                    format_bound_toolchains(workspace_id, &Value::Array(values))
                },
                || {
                    let value = runtime.daemon_call(
                        "toolchain.list_bound",
                        serde_json::json!({
                            "workspace_id": workspace_id.to_string(),
                            "build_context_hash": enterprise_hash,
                        }),
                    )?;
                    format_bound_toolchains(workspace_id, &value)
                },
            )
        }
    }
}

fn run_build_context(runtime: &RuntimeOptions, action: BuildContextAction) -> CommandResult {
    match action {
        BuildContextAction::Register {
            workspace_id,
            name,
            flags,
            defines,
            includes,
            activate,
        } => {
            let defines = match parse_cli_defines(&defines) {
                Ok(value) => value,
                Err(error) => return CommandResult::failure(1, error, RouteUsed::None),
            };
            execute_build_context_register(
                runtime,
                workspace_id,
                name,
                flags,
                defines,
                includes,
                activate,
                None,
            )
        }
        BuildContextAction::List { workspace_id } => execute_workspace_read(
            runtime,
            || {
                let store = open_local_toolchain_store(runtime, false)?;
                let values = store
                    .list_build_contexts(workspace_id)
                    .map_err(toolchain_sql_error)?;
                format_build_context_list(workspace_id, &Value::Array(values))
            },
            || {
                let value = runtime.daemon_call(
                    "build_context.list",
                    serde_json::json!({"workspace_id": workspace_id.to_string()}),
                )?;
                format_build_context_list(workspace_id, &value)
            },
        ),
        BuildContextAction::Show { workspace_id, hash } => {
            let local_hash = hash.clone();
            let enterprise_hash = hash.clone();
            execute_workspace_read(
                runtime,
                || {
                    let store = open_local_toolchain_store(runtime, false)?;
                    let context = store
                        .get_build_context(workspace_id, &local_hash)
                        .map_err(toolchain_sql_error)?
                        .ok_or_else(|| format!("Build context not found: {local_hash}"))?;
                    let full_hash = json_string(&context, "build_context_hash");
                    let count = store
                        .count_resolved_edges(workspace_id, &full_hash)
                        .map_err(toolchain_sql_error)?;
                    format_build_context_show(&context, count)
                },
                || {
                    let context = runtime.daemon_call(
                        "build_context.get",
                        serde_json::json!({
                            "workspace_id": workspace_id.to_string(),
                            "build_context_hash": enterprise_hash,
                        }),
                    )?;
                    if context.is_null() {
                        return Err(format!("Build context not found: {enterprise_hash}"));
                    }
                    let full_hash = json_string(&context, "build_context_hash");
                    let count = runtime.daemon_call(
                        "resolved_edges.count",
                        serde_json::json!({
                            "workspace_id": workspace_id.to_string(),
                            "build_context_hash": full_hash,
                        }),
                    )?;
                    format_build_context_show(
                        &context,
                        count.get("count").and_then(Value::as_i64).unwrap_or(0),
                    )
                },
            )
        }
        BuildContextAction::Activate { workspace_id, hash } => {
            execute_build_context_mutation(runtime, workspace_id, hash, true)
        }
        BuildContextAction::Delete { workspace_id, hash } => {
            execute_build_context_mutation(runtime, workspace_id, hash, false)
        }
        BuildContextAction::ImportCompileCommands {
            file,
            workspace_id,
            name,
            activate,
            workspace_root,
        } => {
            let root = workspace_root
                .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
            let aggregate = match import_compile_commands(&file, &root) {
                Ok(value) => value,
                Err(error) => return CommandResult::failure(1, error, RouteUsed::None),
            };
            let defines = aggregate
                .defines
                .iter()
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect();
            execute_build_context_register(
                runtime,
                workspace_id,
                name,
                aggregate.compile_flags.clone(),
                defines,
                aggregate.include_paths.clone(),
                activate,
                Some(aggregate),
            )
        }
        BuildContextAction::Resolve { workspace_id, hash } => {
            let local_hash = hash.clone();
            let enterprise_hash = hash.clone();
            runtime.execute_write_with(
                || resolve_local_build_context(runtime, workspace_id, &local_hash),
                || resolve_enterprise_build_context(runtime, workspace_id, &enterprise_hash),
            )
        }
        BuildContextAction::Edges {
            workspace_id,
            hash,
            caller,
            limit,
        } => {
            let local_hash = hash.clone();
            let enterprise_hash = hash.clone();
            execute_workspace_read(
                runtime,
                || {
                    let store = open_local_toolchain_store(runtime, false)?;
                    let context = store
                        .get_build_context(workspace_id, &local_hash)
                        .map_err(toolchain_sql_error)?
                        .ok_or_else(|| format!("Build context not found: {local_hash}"))?;
                    let full_hash = json_string(&context, "build_context_hash");
                    let edges = store
                        .get_resolved_edges(workspace_id, &full_hash, caller, Some(limit))
                        .map_err(toolchain_sql_error)?;
                    format_resolved_edges(&Value::Array(edges))
                },
                || {
                    let context = runtime.daemon_call(
                        "build_context.get",
                        serde_json::json!({
                            "workspace_id": workspace_id.to_string(),
                            "build_context_hash": enterprise_hash,
                        }),
                    )?;
                    if context.is_null() {
                        return Err(format!("Build context not found: {enterprise_hash}"));
                    }
                    let value = runtime.daemon_call(
                        "resolved_edges.get",
                        serde_json::json!({
                            "workspace_id": workspace_id.to_string(),
                            "build_context_hash": json_string(&context, "build_context_hash"),
                            "caller_symbol_id": caller,
                            "limit": limit,
                        }),
                    )?;
                    format_resolved_edges(&value)
                },
            )
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn execute_build_context_register(
    runtime: &RuntimeOptions,
    workspace_id: i64,
    name: String,
    flags: Vec<String>,
    defines: Vec<(String, String)>,
    includes: Vec<String>,
    activate: bool,
    imported: Option<AggregatedBuildContext>,
) -> CommandResult {
    let local_name = name.clone();
    let enterprise_name = name.clone();
    let local_flags = flags.clone();
    let enterprise_flags = flags;
    let local_defines = defines.clone();
    let enterprise_defines = defines;
    let local_includes = includes.clone();
    let enterprise_includes = includes;
    let local_imported = imported.clone();
    let enterprise_imported = imported;
    runtime.execute_write_with(
        || {
            let store = open_local_toolchain_store(runtime, true)?;
            let context = store
                .register_build_context(
                    workspace_id,
                    &local_name,
                    &local_flags,
                    &local_defines,
                    &local_includes,
                    activate,
                )
                .map_err(toolchain_sql_error)?;
            format_build_context_registered(&context, local_imported.as_ref())
        },
        || {
            let context = runtime.daemon_call(
                "build_context.register",
                serde_json::json!({
                    "workspace_id": workspace_id.to_string(),
                    "name": enterprise_name,
                    "compile_flags": enterprise_flags,
                    "defines": pairs_to_json_object(&enterprise_defines),
                    "include_paths": enterprise_includes,
                    "set_active": activate,
                }),
            )?;
            format_build_context_registered(&context, enterprise_imported.as_ref())
        },
    )
}

fn execute_build_context_mutation(
    runtime: &RuntimeOptions,
    workspace_id: i64,
    hash: String,
    activate: bool,
) -> CommandResult {
    let local_hash = hash.clone();
    let enterprise_hash = hash.clone();
    runtime.execute_write_with(
        || {
            let store = open_local_toolchain_store(runtime, true)?;
            let context = store
                .get_build_context(workspace_id, &local_hash)
                .map_err(toolchain_sql_error)?
                .ok_or_else(|| format!("Build context not found: {local_hash}"))?;
            let full_hash = json_string(&context, "build_context_hash");
            let name = json_string(&context, "name");
            if activate {
                if !store
                    .set_active_build_context(workspace_id, &full_hash)
                    .map_err(toolchain_sql_error)?
                {
                    return Err("Failed to activate".to_string());
                }
                Ok(format!("Activated: {} ({})", name, short_hash(&full_hash)))
            } else {
                if store
                    .delete_build_context(workspace_id, &full_hash)
                    .map_err(toolchain_sql_error)?
                    == 0
                {
                    return Err("Failed to delete".to_string());
                }
                Ok(format!("Deleted: {} ({})", name, short_hash(&full_hash)))
            }
        },
        || {
            let context = runtime.daemon_call(
                "build_context.get",
                serde_json::json!({
                    "workspace_id": workspace_id.to_string(),
                    "build_context_hash": enterprise_hash,
                }),
            )?;
            if context.is_null() {
                return Err(format!("Build context not found: {enterprise_hash}"));
            }
            let full_hash = json_string(&context, "build_context_hash");
            let name = json_string(&context, "name");
            let method = if activate {
                "build_context.set_active"
            } else {
                "build_context.delete"
            };
            let value = runtime.daemon_call(
                method,
                serde_json::json!({
                    "workspace_id": workspace_id.to_string(),
                    "build_context_hash": full_hash,
                }),
            )?;
            if activate && !value.get("ok").and_then(Value::as_bool).unwrap_or(false) {
                return Err("Failed to activate".to_string());
            }
            if !activate && value.get("deleted").and_then(Value::as_u64).unwrap_or(0) == 0 {
                return Err("Failed to delete".to_string());
            }
            Ok(if activate {
                format!("Activated: {} ({})", name, short_hash(&full_hash))
            } else {
                format!("Deleted: {} ({})", name, short_hash(&full_hash))
            })
        },
    )
}

fn resolve_local_build_context(
    runtime: &RuntimeOptions,
    workspace_id: i64,
    hash: &str,
) -> Result<String, String> {
    let store = open_local_toolchain_store(runtime, true)?;
    let context = store
        .get_build_context(workspace_id, hash)
        .map_err(toolchain_sql_error)?
        .ok_or_else(|| format!("Build context not found: {hash}"))?;
    let full_hash = json_string(&context, "build_context_hash");
    let result = {
        let conn = store.conn();
        compute_resolved_edges(&conn, workspace_id, &full_hash)?
    };
    let (deleted, inserted) = store
        .replace_resolved_edges(workspace_id, &full_hash, &result.edges)
        .map_err(toolchain_sql_error)?;
    format_resolve_result(&context, &result, deleted, inserted)
}

fn resolve_enterprise_build_context(
    runtime: &RuntimeOptions,
    workspace_id: i64,
    hash: &str,
) -> Result<String, String> {
    let context = runtime.daemon_call(
        "build_context.get",
        serde_json::json!({
            "workspace_id": workspace_id.to_string(),
            "build_context_hash": hash,
        }),
    )?;
    if context.is_null() {
        return Err(format!("Build context not found: {hash}"));
    }
    let full_hash = json_string(&context, "build_context_hash");
    let conn = runtime.open_local_db().map_err(|error| {
        format!(
            "enterprise resolve requires the mounted local workspace database for symbol facts: {error}"
        )
    })?;
    let toolchain = runtime.daemon_call(
        "toolchain.resolve",
        serde_json::json!({
            "workspace_id": workspace_id.to_string(),
            "build_context_hash": full_hash,
        }),
    )?;
    let result = compute_resolved_edges_for_external_context(
        &conn,
        workspace_id,
        &context,
        (!toolchain.is_null()).then_some(&toolchain),
    )
    .map_err(|error| {
        format!("enterprise resolve requires a matching local symbol snapshot: {error}")
    })?;
    let response = runtime.daemon_call(
        "resolved_edges.replace",
        serde_json::json!({
            "workspace_id": workspace_id.to_string(),
            "build_context_hash": full_hash,
            "edges": resolved_edges_json(&result.edges),
        }),
    )?;
    format_resolve_result(
        &context,
        &result,
        response.get("deleted").and_then(Value::as_u64).unwrap_or(0),
        response
            .get("inserted")
            .and_then(Value::as_u64)
            .unwrap_or(0) as usize,
    )
}

fn open_local_toolchain_store(
    runtime: &RuntimeOptions,
    writable: bool,
) -> Result<ToolchainStore, String> {
    let path = runtime.db_path.to_string_lossy();
    if writable {
        ToolchainStore::open(&path)
    } else {
        ToolchainStore::open_read_only(&path)
    }
    .map_err(toolchain_sql_error)
}

fn register_toolchain_in_store(
    store: &ToolchainStore,
    registration: &ToolchainRegistration,
) -> Result<Value, String> {
    let mut macros = registration
        .predefined_macros
        .iter()
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect::<Vec<_>>();
    macros.sort();
    store
        .register_toolchain(
            &registration.name,
            &registration.compiler_path,
            &registration.compiler_type,
            &registration.version,
            &registration.target_triple,
            &registration.sysroot,
            &registration.include_dirs,
            &macros,
            &registration.fingerprint,
            &registration.description,
        )
        .map_err(toolchain_sql_error)
}

fn toolchain_register_params(registration: &ToolchainRegistration) -> Value {
    serde_json::json!({
        "name": registration.name,
        "compiler_path": registration.compiler_path,
        "compiler_type": registration.compiler_type,
        "version": registration.version,
        "target_triple": registration.target_triple,
        "sysroot": registration.sysroot,
        "include_dirs": registration.include_dirs,
        "predefined_macros": registration.predefined_macros,
        "fingerprint": registration.fingerprint,
        "description": registration.description,
    })
}

fn parse_cli_defines(values: &[String]) -> Result<Vec<(String, String)>, String> {
    let mut result = Vec::<(String, String)>::new();
    let mut positions = std::collections::HashMap::<String, usize>::new();
    for value in values {
        let (name, value) = value
            .split_once('=')
            .map(|(name, value)| (name, value))
            .unwrap_or((value.as_str(), ""));
        if name.is_empty() {
            return Err("build-context define name must not be empty".to_string());
        }
        if let Some(index) = positions.get(name).copied() {
            result[index].1 = value.to_string();
        } else {
            positions.insert(name.to_string(), result.len());
            result.push((name.to_string(), value.to_string()));
        }
    }
    Ok(result)
}

fn pairs_to_json_object(values: &[(String, String)]) -> Value {
    Value::Object(
        values
            .iter()
            .map(|(key, value)| (key.clone(), Value::String(value.clone())))
            .collect(),
    )
}

fn resolved_edges_json(edges: &[ResolvedEdgeInput]) -> Vec<Value> {
    edges
        .iter()
        .map(|edge| {
            serde_json::json!({
                "caller_symbol_id": edge.caller_symbol_id,
                "callee_symbol_id": edge.callee_symbol_id,
                "callee_name": edge.callee_name,
                "callee_file": edge.callee_file,
                "call_line": edge.call_line,
                "resolution_method": edge.resolution_method,
            })
        })
        .collect()
}

fn format_toolchain_registered(value: &Value) -> Result<String, String> {
    let includes = json_array_len(value, "include_dirs");
    let macros = json_object_len(value, "predefined_macros");
    let mut lines = vec![
        format!(
            "Toolchain registered: Toolchain(id={}, name={}, type={}, version={}, target={})",
            require_json_i64(value, "id")?,
            json_string(value, "name"),
            json_string(value, "compiler_type"),
            json_string(value, "version"),
            json_string(value, "target_triple")
        ),
        format!("  fingerprint: {}", json_string(value, "fingerprint")),
    ];
    if includes > 0 {
        let preview = value["include_dirs"]
            .as_array()
            .into_iter()
            .flatten()
            .take(3)
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join(", ");
        lines.push(format!("  include_dirs ({includes}): {preview}..."));
    }
    if macros > 0 {
        lines.push(format!("  predefined_macros: {macros} macros"));
    }
    Ok(lines.join("\n"))
}

fn format_toolchain_list(value: &Value) -> Result<String, String> {
    let rows = value
        .as_array()
        .ok_or_else(|| "toolchain list returned a non-array result".to_string())?;
    if rows.is_empty() {
        return Ok("No toolchains registered.".to_string());
    }
    let mut lines = vec![
        format!(
            "{:<5} {:<20} {:<20} {:<30} {:<25}",
            "ID", "Name", "Type", "Version", "Target"
        ),
        "-".repeat(100),
    ];
    for row in rows {
        lines.push(format!(
            "{:<5} {:<20} {:<20} {:<30} {:<25}",
            row["id"].as_i64().unwrap_or(0),
            truncate(&json_string(row, "name"), 20),
            truncate(&json_string(row, "compiler_type"), 20),
            truncate(&json_string(row, "version"), 30),
            truncate(&json_string(row, "target_triple"), 25),
        ));
    }
    Ok(lines.join("\n"))
}

fn format_toolchain_show(value: &Value) -> Result<String, String> {
    let includes = value["include_dirs"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let mut lines = vec![
        format!("Toolchain: {}", json_string(value, "name")),
        format!("  ID: {}", require_json_i64(value, "id")?),
        format!("  Compiler: {}", json_string(value, "compiler_path")),
        format!("  Type: {}", json_string(value, "compiler_type")),
        format!("  Version: {}", json_string(value, "version")),
        format!("  Target: {}", json_string(value, "target_triple")),
        format!(
            "  Sysroot: {}",
            nonempty_or(&json_string(value, "sysroot"), "(none)")
        ),
        format!("  Fingerprint: {}", json_string(value, "fingerprint")),
        format!("  Include dirs ({}):", includes.len()),
    ];
    lines.extend(
        includes
            .iter()
            .take(10)
            .filter_map(Value::as_str)
            .map(|path| format!("    {path}")),
    );
    if includes.len() > 10 {
        lines.push(format!("    ... and {} more", includes.len() - 10));
    }
    lines.push(format!(
        "  Predefined macros: {}",
        json_object_len(value, "predefined_macros")
    ));
    lines.push(format!(
        "  Description: {}",
        nonempty_or(&json_string(value, "description"), "(none)")
    ));
    Ok(lines.join("\n"))
}

fn format_bound_toolchains(workspace_id: i64, value: &Value) -> Result<String, String> {
    let rows = value
        .as_array()
        .ok_or_else(|| "toolchain.list_bound returned a non-array result".to_string())?;
    if rows.is_empty() {
        return Ok(format!("No toolchains bound to workspace {workspace_id}"));
    }
    Ok(rows
        .iter()
        .map(|row| {
            format!(
                "  Toolchain(id={}, name={}, type={}, version={}, target={})",
                row["id"].as_i64().unwrap_or(0),
                json_string(row, "name"),
                json_string(row, "compiler_type"),
                json_string(row, "version"),
                json_string(row, "target_triple")
            )
        })
        .collect::<Vec<_>>()
        .join("\n"))
}

fn format_build_context_registered(
    context: &Value,
    imported: Option<&AggregatedBuildContext>,
) -> Result<String, String> {
    let mut lines = Vec::new();
    if let Some(imported) = imported {
        lines.extend([
            format!("Imported {} compile entries:", imported.file_count),
            format!(
                "  compiler: {}",
                nonempty_or(&imported.compiler_path, "(not detected)")
            ),
            format!("  defines: {}", imported.defines.len()),
            format!("  include_paths: {}", imported.include_paths.len()),
            format!("  compile_flags: {}", imported.compile_flags.len()),
        ]);
    }
    lines.extend([
        format!("Build context registered: {}", json_string(context, "name")),
        format!("  hash: {}", json_string(context, "build_context_hash")),
    ]);
    if imported.is_none() {
        lines.extend([
            format!(
                "  flags: {}",
                python_string_list_repr(&context["compile_flags"])
            ),
            format!("  defines: {} macros", json_object_len(context, "defines")),
            format!(
                "  includes: {} paths",
                json_array_len(context, "include_paths")
            ),
        ]);
    }
    if context["is_active"].as_bool().unwrap_or(false) {
        lines.push("  (set as active)".to_string());
    }
    if let Some(imported) = imported {
        if !imported.compiler_path.is_empty() {
            lines.push(format!(
                "\n  Hint: Detected compiler '{}'",
                imported.compiler_path
            ));
            lines.push(format!(
                "  Run: cw toolchain register auto_{} {}",
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_secs(),
                imported.compiler_path
            ));
        }
    }
    Ok(lines.join("\n"))
}

fn format_build_context_list(workspace_id: i64, value: &Value) -> Result<String, String> {
    let rows = value
        .as_array()
        .ok_or_else(|| "build_context.list returned a non-array result".to_string())?;
    if rows.is_empty() {
        return Ok(format!("No build contexts for workspace {workspace_id}"));
    }
    let mut lines = vec![
        format!("Build contexts for workspace {workspace_id}:"),
        format!(
            "{:<20} {:<8} {:<20} {:<8} {:<8}",
            "Name", "Active", "Hash", "Defines", "Includes"
        ),
        "-".repeat(80),
    ];
    for row in rows {
        lines.push(format!(
            "{:<20} {:<8} {:<20} {:<8} {:<8}",
            truncate(&json_string(row, "name"), 20),
            if row["is_active"].as_bool().unwrap_or(false) {
                "✓"
            } else {
                ""
            },
            short_hash(&json_string(row, "build_context_hash")),
            json_object_len(row, "defines"),
            json_array_len(row, "include_paths"),
        ));
    }
    Ok(lines.join("\n"))
}

fn format_build_context_show(context: &Value, edge_count: i64) -> Result<String, String> {
    let flags = context["compile_flags"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let defines = context["defines"].as_object().cloned().unwrap_or_default();
    let includes = context["include_paths"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let mut lines = vec![
        format!("Build Context: {}", json_string(context, "name")),
        format!("  Hash: {}", json_string(context, "build_context_hash")),
        format!(
            "  Active: {}",
            if context["is_active"].as_bool().unwrap_or(false) {
                "yes"
            } else {
                "no"
            }
        ),
        format!("  Compile flags ({}):", flags.len()),
    ];
    lines.extend(
        flags
            .iter()
            .filter_map(Value::as_str)
            .map(|flag| format!("    {flag}")),
    );
    lines.push(format!("  Defines ({}):", defines.len()));
    lines.extend(
        defines
            .iter()
            .take(20)
            .map(|(key, value)| format!("    {}={}", key, value.as_str().unwrap_or(""))),
    );
    if defines.len() > 20 {
        lines.push(format!("    ... and {} more", defines.len() - 20));
    }
    lines.push(format!("  Include paths ({}):", includes.len()));
    lines.extend(
        includes
            .iter()
            .take(10)
            .filter_map(Value::as_str)
            .map(|path| format!("    {path}")),
    );
    if includes.len() > 10 {
        lines.push(format!("    ... and {} more", includes.len() - 10));
    }
    lines.push(format!("  Resolved edges: {edge_count}"));
    Ok(lines.join("\n"))
}

fn format_resolved_edges(value: &Value) -> Result<String, String> {
    let rows = value
        .as_array()
        .ok_or_else(|| "resolved_edges.get returned a non-array result".to_string())?;
    if rows.is_empty() {
        return Ok("No resolved edges found".to_string());
    }
    let mut lines = vec![
        format!("Resolved edges ({} shown):", rows.len()),
        format!(
            "{:<10} {:<10} {:<30} {:<20} {:<6} {:<15}",
            "Caller", "Callee", "Callee Name", "File", "Line", "Method"
        ),
        "-".repeat(95),
    ];
    for row in rows {
        lines.push(format!(
            "{:<10} {:<10} {:<30} {:<20} {:<6} {:<15}",
            row["caller_symbol_id"].as_i64().unwrap_or(0),
            row["callee_symbol_id"].as_i64().unwrap_or(0),
            truncate(&json_string(row, "callee_name"), 30),
            truncate(&json_string(row, "callee_file"), 20),
            row["call_line"].as_i64().unwrap_or(0),
            truncate(&json_string(row, "resolution_method"), 15),
        ));
    }
    Ok(lines.join("\n"))
}

fn format_resolve_result(
    context: &Value,
    result: &ResolvedEdgesResult,
    deleted: u64,
    inserted: usize,
) -> Result<String, String> {
    let mut lines = vec![
        format!(
            "Resolved edges computed for: {}",
            json_string(context, "name")
        ),
        format!("  source: {}", result.source),
        format!("  computed: {} edges", result.edges.len()),
    ];
    if result.skipped > 0 {
        lines.push(format!("  skipped (caller unmapped): {}", result.skipped));
    }
    lines.extend([
        format!("  deleted old: {deleted}"),
        format!("  stored: {inserted}"),
    ]);
    Ok(lines.join("\n"))
}

fn require_json_i64(value: &Value, field: &str) -> Result<i64, String> {
    value
        .get(field)
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("response is missing integer field {field}"))
}

fn json_string(value: &Value, field: &str) -> String {
    value
        .get(field)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

fn json_array_len(value: &Value, field: &str) -> usize {
    value
        .get(field)
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0)
}

fn json_object_len(value: &Value, field: &str) -> usize {
    value
        .get(field)
        .and_then(Value::as_object)
        .map(serde_json::Map::len)
        .unwrap_or(0)
}

fn short_hash(value: &str) -> &str {
    value.get(..16).unwrap_or(value)
}

fn nonempty_or<'a>(value: &'a str, fallback: &'a str) -> &'a str {
    if value.is_empty() {
        fallback
    } else {
        value
    }
}

fn truncate(value: &str, max_chars: usize) -> String {
    value.chars().take(max_chars).collect()
}

fn python_string_list_repr(value: &Value) -> String {
    let Some(values) = value.as_array() else {
        return "[]".to_string();
    };
    let items = values
        .iter()
        .filter_map(Value::as_str)
        .map(|item| format!("'{}'", item.replace('\\', "\\\\").replace('\'', "\\'")))
        .collect::<Vec<_>>();
    format!("[{}]", items.join(", "))
}

fn toolchain_sql_error(error: rusqlite::Error) -> String {
    format!("toolchain SQLite operation failed: {error}")
}

fn run_workspace(runtime: &RuntimeOptions, action: WorkspaceAction) -> CommandResult {
    match action {
        WorkspaceAction::List => execute_workspace_read(
            runtime,
            || {
                let conn = runtime.open_local_db()?;
                let rows = list_local_workspaces(&conn)?;
                Ok(format_workspace_list(&rows, use_zh_cn()))
            },
            || {
                let value = runtime.daemon_call("workspace.list", serde_json::json!({}))?;
                format_enterprise_workspace_list(&value)
            },
        ),
        WorkspaceAction::Status { id_or_name } => {
            let enterprise_id = id_or_name.clone().or_else(|| runtime.workspace_id.clone());
            execute_workspace_read(
                runtime,
                || {
                    let conn = runtime.open_local_db()?;
                    let identifier = match id_or_name.as_deref() {
                        Some(value) => value.to_string(),
                        None => runtime.resolve_local_workspace_id(&conn)?.to_string(),
                    };
                    let workspace = get_local_workspace(&conn, &identifier)?
                        .ok_or_else(|| format!("workspace {identifier:?} not found"))?;
                    serde_json::to_string_pretty(&workspace_record_json(&workspace))
                        .map_err(|error| format!("cannot serialize workspace status: {error}"))
                },
                || {
                    let identifier = enterprise_id.as_deref().ok_or_else(|| {
                        "enterprise workspace status requires ID or --workspace-id".to_string()
                    })?;
                    let value = runtime.daemon_call(
                        "workspace.status",
                        serde_json::json!({"workspace_instance_id": identifier}),
                    )?;
                    serde_json::to_string_pretty(&value)
                        .map_err(|error| format!("cannot serialize workspace status: {error}"))
                },
            )
        }
        WorkspaceAction::Register {
            name,
            root,
            description,
            git_remote_url,
            git_head_commit_sha,
            toolchain_fingerprint,
        } => runtime.execute_write_with(
            || {
                let mut conn = runtime.open_local_write_db()?;
                let mut workspace =
                    register_local_workspace(&mut conn, &name, &root, &description)?;
                // Python 成功消息回显用户参数，数据库内仍保存规范化路径。
                workspace.root_path = root.to_string_lossy().to_string();
                Ok(format_register_success(&workspace, use_zh_cn()))
            },
            || {
                let root = root.to_string_lossy().to_string();
                let value = runtime.daemon_call(
                    "workspace.register",
                    serde_json::json!({
                        "client_view_root": root,
                        "git_remote_url": git_remote_url,
                        "git_head_commit_sha": git_head_commit_sha,
                        "toolchain_fingerprint": toolchain_fingerprint,
                    }),
                )?;
                let id = value
                    .get("workspace_id")
                    .and_then(|item| item.as_i64())
                    .unwrap_or_default();
                let normalized_root = value
                    .get("client_view_root")
                    .and_then(|item| item.as_str())
                    .unwrap_or(&root);
                let workspace = WorkspaceRecord {
                    id,
                    name: name.clone(),
                    root_path: normalized_root.to_string(),
                    is_active: true,
                    description: description.clone(),
                };
                Ok(format_register_success(&workspace, use_zh_cn()))
            },
        ),
        WorkspaceAction::Activate { id_or_name } => runtime.execute_write_with(
            || {
                let mut conn = runtime.open_local_write_db()?;
                let workspace = activate_local_workspace(&mut conn, &id_or_name)?;
                Ok(format_activate_result(
                    workspace.as_ref(),
                    &id_or_name,
                    use_zh_cn(),
                ))
            },
            || {
                let value = runtime.daemon_call(
                    "workspace.activate",
                    serde_json::json!({"workspace_instance_id": id_or_name}),
                )?;
                let root = value
                    .get("client_view_root")
                    .and_then(|item| item.as_str())
                    .unwrap_or("");
                Ok(if use_zh_cn() {
                    format!("已激活企业工作区: {} ({})", id_or_name, root)
                } else {
                    format!("Enterprise workspace activated: {} ({})", id_or_name, root)
                })
            },
        ),
        WorkspaceAction::Remove { id_or_name } => runtime.execute_write_with(
            || {
                let mut conn = runtime.open_local_write_db()?;
                let workspace = remove_local_workspace(&mut conn, &id_or_name)?;
                Ok(format_remove_result(
                    workspace.as_ref(),
                    &id_or_name,
                    use_zh_cn(),
                ))
            },
            || {
                runtime.daemon_call(
                    "workspace.remove",
                    serde_json::json!({"workspace_instance_id": id_or_name}),
                )?;
                Ok(if use_zh_cn() {
                    format!("企业工作区 '{}' 已归档", id_or_name)
                } else {
                    format!("Enterprise workspace '{}' archived", id_or_name)
                })
            },
        ),
    }
}

fn execute_workspace_read<L, E>(runtime: &RuntimeOptions, local: L, enterprise: E) -> CommandResult
where
    L: FnOnce() -> Result<String, String>,
    E: FnOnce() -> Result<String, String>,
{
    match runtime.mode {
        DaemonMode::Local => workspace_text_result(local(), RouteUsed::Local),
        DaemonMode::Enterprise => {
            if !runtime.daemon_available() {
                return CommandResult::failure(
                    2,
                    format!(
                        "enterprise daemon is unavailable at {}",
                        runtime.socket_path.display()
                    ),
                    RouteUsed::None,
                );
            }
            workspace_text_result(enterprise(), RouteUsed::Enterprise)
        }
        DaemonMode::Auto if runtime.daemon_available() => match enterprise() {
            Ok(stdout) => CommandResult::success_text(stdout, RouteUsed::Enterprise),
            Err(enterprise_error) => {
                let mut result = workspace_text_result(local(), RouteUsed::Local);
                if result.exit_code == 0 {
                    result.stderr = format!(
                        "warning: daemon query failed; used local database: {enterprise_error}"
                    );
                }
                result
            }
        },
        DaemonMode::Auto => workspace_text_result(local(), RouteUsed::Local),
    }
}

fn workspace_text_result(result: Result<String, String>, route: RouteUsed) -> CommandResult {
    match result {
        Ok(stdout) => CommandResult::success_text(stdout, route),
        Err(error) => CommandResult::failure(1, error, route),
    }
}

fn format_enterprise_workspace_list(value: &serde_json::Value) -> Result<String, String> {
    let rows = value
        .as_array()
        .ok_or_else(|| "workspace.list returned a non-array result".to_string())?;
    let mut lines = vec![if use_zh_cn() {
        format!("企业工作区列表（共 {} 个）:", rows.len())
    } else {
        format!("Enterprise workspaces ({} total):", rows.len())
    }];
    for row in rows {
        let id = row
            .get("workspace_instance_id")
            .and_then(|item| item.as_str())
            .unwrap_or("<unknown>");
        let status = row
            .get("status")
            .and_then(|item| item.as_str())
            .unwrap_or("unknown");
        let root = row
            .get("client_view_root")
            .and_then(|item| item.as_str())
            .unwrap_or("");
        lines.push(format!("[{}] {} [{}]", row["workspace_id"], id, status));
        lines.push(if use_zh_cn() {
            format!("路径: {root}")
        } else {
            format!("Path: {root}")
        });
    }
    Ok(lines.join("\n"))
}

fn use_zh_cn() -> bool {
    std::env::var("CALLWARDEN_LANG")
        .map(|value| value.to_ascii_lowercase().starts_with("zh"))
        .unwrap_or(false)
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
        Task { .. } => "task",
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
        Workspace { .. } => "workspace",
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
        BuildContext { .. } => "build-context",
        Toolchain { .. } => "toolchain",
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
            Commands::Task {
                action: TaskAction::List {
                    blocked: false,
                    limit: 200,
                    status: String::new(),
                    flat: false,
                },
            },
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
            Commands::Workspace {
                action: WorkspaceAction::List,
            },
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
            Commands::BuildContext {
                action: BuildContextAction::List { workspace_id: 1 },
            },
            Commands::Toolchain {
                action: ToolchainAction::List,
            },
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
        assert_eq!(
            command_name(&Commands::BuildContext {
                action: BuildContextAction::List { workspace_id: 1 },
            }),
            "build-context"
        );
    }

    #[test]
    fn test_command_name_simple() {
        // 验证简单名称（无连字符）
        assert_eq!(command_name(&Commands::Guardrail), "guardrail");
        assert_eq!(
            command_name(&Commands::Task {
                action: TaskAction::StatusTree {
                    task_id: "task-1".to_string(),
                },
            }),
            "task"
        );
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
        let chain = Cli::try_parse_from(["cw", "call-chain", "a.alpha", "--depth", "3"]).unwrap();
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
    fn parses_toolchain_and_build_context_actions() {
        let toolchain = Cli::try_parse_from([
            "cw",
            "toolchain",
            "register",
            "gcc-arm",
            "/opt/gcc/bin/gcc",
            "--sysroot",
            "/opt/gcc/sysroot",
            "--no-probe",
        ])
        .unwrap();
        assert!(matches!(
            toolchain.command,
            Some(Commands::Toolchain {
                action: ToolchainAction::Register {
                    name,
                    no_probe: true,
                    ..
                }
            }) if name == "gcc-arm"
        ));

        let context = Cli::try_parse_from([
            "cw",
            "build-context",
            "register",
            "7",
            "debug",
            "--flags=-O2",
            "--defines",
            "DEBUG=1",
            "BOARD=A98",
            "--includes",
            "include",
            "--activate",
        ])
        .unwrap();
        assert!(matches!(
            context.command,
            Some(Commands::BuildContext {
                action: BuildContextAction::Register {
                    workspace_id: 7,
                    name,
                    flags,
                    defines,
                    includes,
                    activate: true,
                }
            }) if name == "debug"
                && flags == ["-O2"]
                && defines == ["DEBUG=1", "BOARD=A98"]
                && includes == ["include"]
        ));
    }

    #[test]
    fn refresh_full_mode_rejects_conflicting_arguments() {
        let runtime = RuntimeOptions::from_overrides(
            Some(DaemonMode::Local),
            None,
            Some(PathBuf::from("unused.db")),
            None,
            1,
        );
        let explicit_path = run_refresh(&runtime, true, false, &[PathBuf::from("src/lib.rs")]);
        assert_eq!(explicit_path.exit_code, 1);
        assert!(explicit_path.stderr.contains("does not accept explicit"));

        let force_without_all = run_refresh(&runtime, false, true, &[PathBuf::from("src/lib.rs")]);
        assert_eq!(force_without_all.exit_code, 1);
        assert!(force_without_all.stderr.contains("only valid together"));
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
        assert_eq!(cli.enterprise_workspace_id.as_deref(), Some("17"));
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
