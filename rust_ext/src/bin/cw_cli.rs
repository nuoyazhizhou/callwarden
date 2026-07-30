//! cw CLI binary（Phase 5-1 A.2）
//!
//! clap 命令树骨架，对齐 Python `cli/main.py:_SUBCOMMANDS` 的 59 个子命令。
//! 本阶段仅实现骨架（命令解析 + "not implemented" 错误），不实现业务逻辑。
//!
//! 契约：docs/design/phase5-1-cli-config-contract.md §3.2

use std::path::PathBuf;

use callwarden_core::cli::router::DaemonMode;
use callwarden_core::cli::runtime::RuntimeOptions;
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
    workspace_id: Option<i64>,

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
    Search,
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
    Config,

    // ===== 驾驶舱 =====
    /// 项目综合状态驾驶舱
    Dashboard,

    // ===== 回滚 =====
    /// 迁移回滚配置
    Rollback,
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
                    let _ = runtime;
                    // 业务逻辑已在 `callwarden_core::cli::stats::stats_command_run` 实现
                    // 但 cw_cli binary 无数据库连接，无法获取 stats_json
                    // wire-production 阶段通过 daemon client（Phase 5-2）或直接 SQL 接入
                    eprintln!("cw stats: data source not available in standalone mode");
                    eprintln!("  (Phase 5-1 C: business logic implemented in lib, awaiting data source wiring)");
                    eprintln!(
                        "  Use 'python cw.py stats' for now, or wait for Phase 5-2 daemon client."
                    );
                    std::process::exit(1);
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
        Search => "search",
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
        Config => "config",
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
            Commands::Search,
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
            Commands::Config,
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
        assert_eq!(command_name(&Commands::Search), "search");
        assert_eq!(command_name(&Commands::Config), "config");
        assert_eq!(command_name(&Commands::Rollback), "rollback");
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
        assert_eq!(cli.workspace_id, Some(17));
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
