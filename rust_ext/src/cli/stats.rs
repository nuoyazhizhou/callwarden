//! stats 子命令业务逻辑（Phase 5-1 C）
//!
//! 对齐 Python `cli/main.py:_handle_stats()` (L6623-6636)：
//! - 接收已序列化的 stats JSON 字符串（由调用方提供，数据查询层不迁移）
//! - 用 `json_dumps_pretty` 格式化为带 2 空格缩进、非 ASCII 不转义的 JSON
//! - 返回 `StatsResult`（exit_code + stdout + stderr）
//!
//! 契约：docs/design/phase5-1c-stats-vertical-slice-contract.md §3

use pyo3::prelude::*;

use super::output::json_dumps_pretty;

/// stats 子命令执行结果。
///
/// 对齐 Python `_handle_stats` 的返回值（True/False → exit_code）和副作用（print → stdout/stderr）。
pub struct StatsResult {
    /// exit code：0 成功，1 失败
    pub exit_code: i32,
    /// stdout 输出内容（已格式化的 JSON，或空字符串）
    pub stdout: String,
    /// stderr 输出内容（错误信息，或空字符串）
    pub stderr: String,
}

/// 执行 stats 子命令业务逻辑。
///
/// 对齐 Python `cli/main.py:_handle_stats()` (L6623-6636)
///
/// 参数：
/// - `stats_json`: 已序列化的 stats 数据（JSON 字符串），由调用方提供。
///   生产环境：Python `db.get_stats()` 的结果通过 `json.dumps(stats)` 序列化后传入。
///   测试环境：直接传入测试 JSON 字符串。
///
/// 行为：
/// 1. 解析 `stats_json`（若无效，输出错误到 stderr，返回 exit 1）
/// 2. 用 `json_dumps_pretty` 重新格式化为带 2 空格缩进的 JSON
/// 3. 输出到 stdout
/// 4. 返回 exit code 0
///
/// 返回：`StatsResult`
///
/// 错误处理：
/// - 空字符串输入 → exit 1，stderr "error: invalid JSON input for stats command"
/// - 损坏 JSON 输入 → exit 1，stderr "error: invalid JSON input for stats command"
/// - 字面量 `null` → exit 0，stdout "null"（合法 JSON）
pub fn stats_command_run(stats_json: &str) -> StatsResult {
    // 复用 Phase 5-3 的 json_dumps_pretty（对齐 json.dumps(indent=2, ensure_ascii=False)）
    let pretty = json_dumps_pretty(stats_json);

    // json_dumps_pretty 对无效 JSON 返回 "null"
    // 需区分：字面量 "null" 输入（合法）vs 无效 JSON 输入（错误）
    if pretty == "null" && stats_json.trim() != "null" {
        return StatsResult {
            exit_code: 1,
            stdout: String::new(),
            stderr: "error: invalid JSON input for stats command".to_string(),
        };
    }

    StatsResult {
        exit_code: 0,
        stdout: pretty,
        stderr: String::new(),
    }
}

/// Python 暴露的 stats_command_run。
///
/// 对齐 Python `cli/main.py:_handle_stats()` (L6623-6636)
///
/// 返回 `(exit_code, stdout, stderr)` 三元组，便于 Python 调用方处理。
///
/// 用法（wire-production 阶段）：
/// ```python
/// import json
/// from callwarden_core import stats_command_run_py
///
/// # Python 端保持数据查询层
/// stats = db.get_stats()
/// stats_json = json.dumps(stats)
///
/// # Rust 端处理业务逻辑（参数解析 + 输出格式化）
/// exit_code, stdout, stderr = stats_command_run_py(stats_json)
/// if stdout:
///     print(stdout)
/// if stderr:
///     print(stderr, file=sys.stderr)
/// sys.exit(exit_code)
/// ```
#[pyfunction]
pub fn stats_command_run_py(stats_json: &str) -> (i32, String, String) {
    let result = stats_command_run(stats_json);
    (result.exit_code, result.stdout, result.stderr)
}

#[cfg(test)]
mod tests {
    use super::*;

    // ============================================
    // D1: 有效 JSON 输入
    // ============================================

    #[test]
    fn test_d1_1_simple_object() {
        let result = stats_command_run(r#"{"a": 1}"#);
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout, "{\n  \"a\": 1\n}");
        assert_eq!(result.stderr, "");
    }

    #[test]
    fn test_d1_2_nested_object() {
        let result = stats_command_run(r#"{"outer": {"inner": "v"}}"#);
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout, "{\n  \"outer\": {\n    \"inner\": \"v\"\n  }\n}");
        assert_eq!(result.stderr, "");
    }

    #[test]
    fn test_d1_3_array() {
        let result = stats_command_run("[1, 2, 3]");
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout, "[\n  1,\n  2,\n  3\n]");
        assert_eq!(result.stderr, "");
    }

    #[test]
    fn test_d1_4_chinese() {
        let result = stats_command_run(r#"{"name": "中文"}"#);
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout, "{\n  \"name\": \"中文\"\n}");
        assert_eq!(result.stderr, "");
    }

    #[test]
    fn test_d1_5_emoji() {
        let result = stats_command_run(r#"{"emoji": "🎉"}"#);
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout, "{\n  \"emoji\": \"🎉\"\n}");
        assert_eq!(result.stderr, "");
    }

    #[test]
    fn test_d1_6_empty_object() {
        let result = stats_command_run("{}");
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout, "{}");
        assert_eq!(result.stderr, "");
    }

    #[test]
    fn test_d1_7_empty_array() {
        let result = stats_command_run("[]");
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout, "[]");
        assert_eq!(result.stderr, "");
    }

    #[test]
    fn test_d1_8_null_literal() {
        let result = stats_command_run("null");
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout, "null");
        assert_eq!(result.stderr, "");
    }

    #[test]
    fn test_d1_9_number() {
        let result = stats_command_run("42");
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout, "42");
        assert_eq!(result.stderr, "");
    }

    #[test]
    fn test_d1_10_string() {
        let result = stats_command_run("\"hello\"");
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout, "\"hello\"");
        assert_eq!(result.stderr, "");
    }

    // ============================================
    // D2: 无效 JSON 输入
    // ============================================

    #[test]
    fn test_d2_1_empty_string() {
        let result = stats_command_run("");
        assert_eq!(result.exit_code, 1);
        assert_eq!(result.stdout, "");
        assert!(!result.stderr.is_empty());
    }

    #[test]
    fn test_d2_2_broken_json() {
        let result = stats_command_run("{invalid}");
        assert_eq!(result.exit_code, 1);
        assert_eq!(result.stdout, "");
        assert!(!result.stderr.is_empty());
    }

    #[test]
    fn test_d2_3_incomplete_json() {
        let result = stats_command_run(r#"{"a":"#);
        assert_eq!(result.exit_code, 1);
        assert_eq!(result.stdout, "");
        assert!(!result.stderr.is_empty());
    }

    // ============================================
    // D3: 真实 stats 数据样例（对齐 db.get_stats() 输出）
    // ============================================

    #[test]
    fn test_d3_1_real_stats_sample() {
        // 模拟 db.get_stats() 返回的真实数据结构
        let stats_json = r#"{"total_files": 100, "total_symbols": 5000, "by_kind": {"fn": 3000, "struct": 200}}"#;
        let result = stats_command_run(stats_json);
        assert_eq!(result.exit_code, 0);
        assert!(result.stdout.contains("\"total_files\": 100"));
        assert!(result.stdout.contains("\"total_symbols\": 5000"));
        assert!(result.stdout.contains("\"fn\": 3000"));
        assert!(result.stdout.contains("\"struct\": 200"));
    }

    #[test]
    fn test_d3_2_full_stats_structure() {
        // 完整的 db.get_stats() 输出结构（所有字段）
        let stats_json = r#"{
            "total_files": 150,
            "unique_symbol_contents": 8000,
            "total_symbols": 12000,
            "commented": 5000,
            "total_calls": 45000,
            "cross_file_calls": 12000,
            "resolved_calls": 38000,
            "total_file_versions": 200,
            "current_files": 150,
            "multi_version_files": 50,
            "total_file_symbol_links": 18000,
            "total_call_versions": 60000,
            "by_kind": {"fn": 8000, "struct": 1500, "enum": 500},
            "depth_distribution": {"0": 1000, "1": 3000, "2": 2500}
        }"#;
        let result = stats_command_run(stats_json);
        assert_eq!(result.exit_code, 0);
        // 验证关键字段都出现在输出中
        assert!(result.stdout.contains("\"total_files\": 150"));
        assert!(result.stdout.contains("\"total_symbols\": 12000"));
        assert!(result.stdout.contains("\"commented\": 5000"));
        assert!(result.stdout.contains("\"total_calls\": 45000"));
        assert!(result.stdout.contains("\"resolved_calls\": 38000"));
        assert!(result.stdout.contains("\"by_kind\":"));
        assert!(result.stdout.contains("\"depth_distribution\":"));
    }

    #[test]
    fn test_d3_3_whitespace_padding() {
        // 输入带前后空格（对齐 Python json.dumps 输出后被传入的场景）
        let result = stats_command_run("  {\"a\": 1}  ");
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout, "{\n  \"a\": 1\n}");
    }

    #[test]
    fn test_d3_4_compact_json_input() {
        // 紧凑 JSON 输入（无空格），输出应为 pretty 格式
        let result = stats_command_run(r#"{"a":1,"b":2}"#);
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout, "{\n  \"a\": 1,\n  \"b\": 2\n}");
    }

    #[test]
    fn test_d3_5_idempotent_pretty_input() {
        // pretty 格式输入，输出应保持一致（幂等性）
        let pretty_input = "{\n  \"a\": 1,\n  \"b\": 2\n}";
        let result = stats_command_run(pretty_input);
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout, pretty_input);
    }

    // ============================================
    // D4: StatsResult 字段完整性
    // ============================================

    #[test]
    fn test_d4_1_success_result_fields() {
        let result = stats_command_run(r#"{"a": 1}"#);
        assert_eq!(result.exit_code, 0);
        assert!(!result.stdout.is_empty());
        assert_eq!(result.stderr, "");
    }

    #[test]
    fn test_d4_2_error_result_fields() {
        let result = stats_command_run("invalid");
        assert_eq!(result.exit_code, 1);
        assert_eq!(result.stdout, "");
        assert!(!result.stderr.is_empty());
    }
}
