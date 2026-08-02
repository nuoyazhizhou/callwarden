//! 只读命令识别（Phase 5-1 A.3）
//!
//! 对齐 Python `cli/main.py`：
//! - `is_readonly_command(cmd, sub_argv)`：子命令模式只读判断
//! - `is_readonly_args(write_flags_set)`：flag 模式只读判断
//!
//! 设计原则：所有读操作都不应该被锁住，只有写操作才需要锁。
//! 未知命令默认为写（fail-safe），避免漏判导致数据不一致。
//!
//! 契约：docs/design/phase5-1-cli-config-contract.md §3.3

// ============================================================
// 只读 action 集合（对齐 Python `_READONLY_*_ACTIONS`）
// ============================================================

/// task list/show/findings 只读；create/next/report/apply/close 等是写
///
/// 对齐 Python `_READONLY_TASK_ACTIONS` (L63)
pub const READONLY_TASK_ACTIONS: &[&str] = &["list", "show", "status-tree", "findings"];

/// rule list/candidate/applicable/extract 只读；sync/insert-block 是写
///
/// 对齐 Python `_READONLY_RULE_ACTIONS` (L64)
pub const READONLY_RULE_ACTIONS: &[&str] = &["list", "candidate", "applicable", "extract"];

/// audit verify/keys 只读（查询 audit_chain/audit_key_rotations 表）
/// audit rotate-key 是写（INSERT/UPDATE audit_key_rotations）
///
/// 对齐 Python `_READONLY_AUDIT_ACTIONS` (L67)
pub const READONLY_AUDIT_ACTIONS: &[&str] = &["verify", "keys"];

/// bootstrap status 只读（汇总查询，不写数据库）
///
/// 对齐 Python `_READONLY_BOOTSTRAP_ACTIONS` (L69)
pub const READONLY_BOOTSTRAP_ACTIONS: &[&str] = &["status"];

/// clone list/stats 只读（查询 clone_pairs 表）；clone detect/clear 写
///
/// 对齐 Python `_READONLY_CLONE_ACTIONS` (L71)
pub const READONLY_CLONE_ACTIONS: &[&str] = &["list", "stats"];

/// workspace list 只读；register/set/delete 写
///
/// 对齐 Python `_READONLY_WORKSPACE_ACTIONS` (L74)
pub const READONLY_WORKSPACE_ACTIONS: &[&str] = &["list"];

/// git log/show/stats/check-task/destructive-log 只读；git import/check-push 写
///
/// 对齐 Python `_READONLY_GIT_ACTIONS` (L78-79)
pub const READONLY_GIT_ACTIONS: &[&str] =
    &["log", "show", "stats", "check-task", "destructive-log"];

/// semgrep list/stats 只读；semgrep scan 视为写（含 --save 选项）
///
/// 对齐 Python `_READONLY_SEMGREP_ACTIONS` (L81)
pub const READONLY_SEMGREP_ACTIONS: &[&str] = &["list", "stats"];

/// coverage fn/uncovered 只读；coverage import 写
///
/// 对齐 Python `_READONLY_COVERAGE_ACTIONS` (L83)
pub const READONLY_COVERAGE_ACTIONS: &[&str] = &["fn", "uncovered"];

/// fts status 只读（查询 FTS5 状态）；fts rebuild 写（重建索引）
///
/// 对齐 Python `_READONLY_FTS_ACTIONS` (L85)
pub const READONLY_FTS_ACTIONS: &[&str] = &["status"];

/// graph build-from-c 只读（不写 DB，仅 parse + 内存构 CSR）
///
/// 对齐 Python `_READONLY_GRAPH_ACTIONS` (L88)
pub const READONLY_GRAPH_ACTIONS: &[&str] = &["build-from-c"];

/// config explain/paths 只读（只读 TOML + 打印路径）
///
/// 对齐 Python `_READONLY_CONFIG_ACTIONS` (L90)
pub const READONLY_CONFIG_ACTIONS: &[&str] = &["explain", "paths"];

/// rollback config/show/is-rolled-back 只读；register/set 写
///
/// 对齐 Python `_READONLY_ROLLBACK_ACTIONS` (L92)
pub const READONLY_ROLLBACK_ACTIONS: &[&str] = &["config", "show", "is-rolled-back"];

/// defect stats/list/show 只读；import/add 是写
///
/// 对齐 Python `_is_readonly_command` 中 `cmd == "defect"` 分支 (L1126-1127)
pub const READONLY_DEFECT_ACTIONS: &[&str] = &["stats", "list", "show"];

/// gc list/inspect/db-cleanup 只读；archive/import 是写
/// db-cleanup 默认 dry-run（只读报告），--apply 时才删除
///
/// 对齐 Python `_is_readonly_command` 中 `cmd == "gc"` 分支 (L1128-1131)
pub const READONLY_GC_ACTIONS: &[&str] = &["list", "inspect", "db-cleanup"];

// ============================================================
// 始终只读的子命令集合
// ============================================================

/// 分析/查询类子命令，始终只读（不写数据库）
///
/// 对齐 Python `_is_readonly_command` L1118-1124
pub const ALWAYS_READONLY_ANALYSIS_CMDS: &[&str] = &[
    "doctor",
    "check-gate",
    "test-impact",
    "hotspot",
    "churn",
    "evolution",
    "impact",
    "review",
    "vuln-blast",
    "symbol-history",
    "guardrail",
];

/// 查询/分析类子命令，始终只读
///
/// 对齐 Python `_is_readonly_command` L1145-1152
pub const ALWAYS_READONLY_QUERY_CMDS: &[&str] = &[
    "search",
    "grep",
    "symbol",
    "file",
    "query",
    "callers",
    "callees",
    "call-chain",
    "topo",
    "metrics",
    "complexity",
    "coupling",
    "comment-coverage",
    "uncommented",
    "function-issues",
    "largest-fns",
    "coupled-fns",
    "fn-metrics",
    "who",
    "ownership-map",
    "brief",
    "map",
    "stats",
    "status",
    "health-report",
    "dashboard",
];

// ============================================================
// 写 flag 集合（flag 模式只读判断用）
// ============================================================

/// 写 flag 集合：设置这些 flag 的命令需要写数据库，必须激活 workspace
///
/// 不在此集合内的 flag 命令均为只读。
///
/// 对齐 Python `_WRITE_FLAGS` (L98-103)
pub const WRITE_FLAGS: &[&str] = &[
    "refresh_all",
    "refresh",
    "watch",
    "register_workspace",
    "set_workspace",
    "delete_workspace",
    "restore_comment",
    "restore_all_comments",
    "coverage_import",
];

// ============================================================
// 只读命令识别函数
// ============================================================

/// 判断子命令是否为只读命令（不修改数据库）。
///
/// 只读命令可以跳过 workspace 注册/激活写操作，在 MCP Server 持有写锁时也能立即返回。
/// 设计原则：所有读操作都不应该被锁住，只有写操作才需要锁。
/// 未知命令默认为写（fail-safe），避免漏判导致数据不一致。
///
/// 对齐 Python `cli/main.py:_is_readonly_command()` (L1098-1180)
///
/// # 参数
/// - `cmd`: 子命令关键字（如 "task"）
/// - `sub_argv`: 子命令参数（不含子命令关键字本身）
///
/// # 返回
/// `true` 表示只读命令，可跳过 workspace 写操作
pub fn is_readonly_command(cmd: &str, sub_argv: &[String]) -> bool {
    let action = sub_argv.first().map(|s| s.as_str()).unwrap_or("");

    match cmd {
        "task" => contains(READONLY_TASK_ACTIONS, action),
        "rule" => contains(READONLY_RULE_ACTIONS, action),
        "audit" => contains(READONLY_AUDIT_ACTIONS, action),
        "bootstrap" => contains(READONLY_BOOTSTRAP_ACTIONS, action),
        "clone" => contains(READONLY_CLONE_ACTIONS, action),
        "workspace" => contains(READONLY_WORKSPACE_ACTIONS, action),
        "git" => contains(READONLY_GIT_ACTIONS, action),
        "semgrep" => contains(READONLY_SEMGREP_ACTIONS, action),
        "coverage" => contains(READONLY_COVERAGE_ACTIONS, action),
        "fts" => contains(READONLY_FTS_ACTIONS, action),
        "graph" => contains(READONLY_GRAPH_ACTIONS, action),
        "config" => contains(READONLY_CONFIG_ACTIONS, action),
        "rollback" => contains(READONLY_ROLLBACK_ACTIONS, action),
        "defect" => contains(READONLY_DEFECT_ACTIONS, action),
        "gc" => contains(READONLY_GC_ACTIONS, action),
        "tests" => {
            // tests --build / tests --import 写；其他（含 --history、--reverse）只读
            // 对齐 Python L1141-1143
            !(sub_argv.iter().any(|a| a == "--build") || sub_argv.iter().any(|a| a == "--import"))
        }
        // 分析/查询类子命令，始终只读
        cmd if contains(ALWAYS_READONLY_ANALYSIS_CMDS, cmd) => true,
        cmd if contains(ALWAYS_READONLY_QUERY_CMDS, cmd) => true,
        // refresh 始终是写操作
        "refresh" => false,
        // 未知命令默认为写（fail-safe）
        _ => false,
    }
}

/// 判断 flag 模式命令是否为只读（不修改数据库）。
///
/// 设计原则：不在 `WRITE_FLAGS` 集合内的 flag 命令均为只读。
///
/// 对齐 Python `cli/main.py:_is_readonly_args()` (L1183-1198)
///
/// # 参数
/// - `write_flags_set`: 已设置的 write flag 名称列表（从 argparse args 对象提取）
///
/// # 返回
/// `true` 表示只读命令（无任何 write flag 被设置）
pub fn is_readonly_args(write_flags_set: &[String]) -> bool {
    // 如果任何 write flag 被设置，则为写命令
    for flag in write_flags_set {
        if contains(WRITE_FLAGS, flag) {
            return false;
        }
    }
    true
}

/// 辅助函数：检查切片是否包含指定字符串。
fn contains(slice: &[&str], item: &str) -> bool {
    slice.iter().any(|s| *s == item)
}

// ============================================================
// PyO3 暴露（供 Python wire-production 调用）
// ============================================================

use pyo3::prelude::*;

/// Python 暴露的 is_readonly_command
///
/// 对齐 Python `cli/main.py:_is_readonly_command()`
#[pyfunction]
pub fn is_readonly_command_py(cmd: &str, sub_argv: Vec<String>) -> bool {
    is_readonly_command(cmd, &sub_argv)
}

/// Python 暴露的 is_readonly_args
///
/// 对齐 Python `cli/main.py:_is_readonly_args()`
///
/// 参数 `write_flags_set`：从 argparse args 对象提取的已设置 write flag 名称列表。
/// 返回 True 表示无任何 write flag 被设置（只读命令）。
#[pyfunction]
pub fn is_readonly_args_py(write_flags_set: Vec<String>) -> bool {
    is_readonly_args(&write_flags_set)
}

// ============================================================
// 单元测试（对齐契约 D5 测试矩阵）
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn test_d5_1_task_list_readonly() {
        assert!(is_readonly_command("task", &argv(&["list"])));
    }

    #[test]
    fn task_status_tree_is_readonly() {
        assert!(is_readonly_command(
            "task",
            &argv(&["status-tree", "task-1"]),
        ));
    }

    #[test]
    fn test_d5_2_task_create_write() {
        assert!(!is_readonly_command("task", &argv(&["create"])));
    }

    #[test]
    fn test_d5_3_search_always_readonly() {
        assert!(is_readonly_command("search", &argv(&[])));
    }

    #[test]
    fn test_d5_4_refresh_always_write() {
        assert!(!is_readonly_command("refresh", &argv(&["--all"])));
    }

    #[test]
    fn test_d5_5_audit_verify_readonly() {
        assert!(is_readonly_command("audit", &argv(&["verify"])));
    }

    #[test]
    fn test_d5_6_audit_rotate_key_write() {
        assert!(!is_readonly_command("audit", &argv(&["rotate-key"])));
    }

    #[test]
    fn test_d5_7_tests_history_readonly() {
        assert!(is_readonly_command("tests", &argv(&["--history"])));
    }

    #[test]
    fn test_d5_8_tests_build_write() {
        assert!(!is_readonly_command("tests", &argv(&["--build"])));
    }

    #[test]
    fn test_d5_9_rollback_config_readonly() {
        assert!(is_readonly_command("rollback", &argv(&["config"])));
    }

    #[test]
    fn test_d5_10_rollback_register_write() {
        assert!(!is_readonly_command("rollback", &argv(&["register"])));
    }

    #[test]
    fn test_d5_11_unknown_cmd_fail_safe_write() {
        assert!(!is_readonly_command("unknown_cmd", &argv(&[])));
    }

    #[test]
    fn test_always_readonly_analysis_cmds() {
        for cmd in ALWAYS_READONLY_ANALYSIS_CMDS {
            assert!(
                is_readonly_command(cmd, &argv(&[])),
                "{} should be readonly",
                cmd
            );
        }
    }

    #[test]
    fn test_always_readonly_query_cmds() {
        for cmd in ALWAYS_READONLY_QUERY_CMDS {
            assert!(
                is_readonly_command(cmd, &argv(&[])),
                "{} should be readonly",
                cmd
            );
        }
    }

    #[test]
    fn test_tests_import_write() {
        assert!(!is_readonly_command("tests", &argv(&["--import"])));
    }

    #[test]
    fn test_defect_stats_readonly() {
        assert!(is_readonly_command("defect", &argv(&["stats"])));
    }

    #[test]
    fn test_defect_import_write() {
        assert!(!is_readonly_command("defect", &argv(&["import"])));
    }

    #[test]
    fn test_gc_list_readonly() {
        assert!(is_readonly_command("gc", &argv(&["list"])));
    }

    #[test]
    fn test_gc_archive_write() {
        assert!(!is_readonly_command("gc", &argv(&["archive"])));
    }

    #[test]
    fn test_is_readonly_args_no_flags() {
        assert!(is_readonly_args(&[]));
    }

    #[test]
    fn test_is_readonly_args_with_write_flag() {
        assert!(!is_readonly_args(&["refresh_all".to_string()]));
    }

    #[test]
    fn test_is_readonly_args_with_non_write_flag() {
        // 非 write flag 不影响只读判断
        assert!(is_readonly_args(&["verbose".to_string()]));
    }

    #[test]
    fn test_is_readonly_args_mixed_flags() {
        // 混合 flag，含 write flag 则为写
        assert!(!is_readonly_args(&[
            "verbose".to_string(),
            "refresh".to_string()
        ]));
    }
}
