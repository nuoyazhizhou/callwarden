//! 兼容输出层（Phase 5-3）
//!
//! 对齐 Python `cli/console.py`：
//! - ANSI 颜色码（14 种颜色/样式）
//! - 颜色检测（NO_COLOR / TTY / FORCE_COLOR 三层判定）
//! - 彩色打印（colorize / cprint / success / error / warning / info / dim / bold）
//! - 格式化工具（format_duration / format_size）
//! - JSON 输出（json_dumps_pretty，对齐 `json.dumps(indent=2, ensure_ascii=False)`）
//!
//! 契约：docs/design/phase5-3-output-layer-contract.md

use serde_json::Value;
use std::collections::HashMap;
use std::io::IsTerminal;

// ============================================================
// ANSI 颜色码常量（对齐 Python `_COLORS` L17-33）
// ============================================================

pub const COLOR_RESET: &str = "\u{1b}[0m";
pub const COLOR_BOLD: &str = "\u{1b}[1m";
pub const COLOR_DIM: &str = "\u{1b}[2m";
pub const COLOR_RED: &str = "\u{1b}[31m";
pub const COLOR_GREEN: &str = "\u{1b}[32m";
pub const COLOR_YELLOW: &str = "\u{1b}[33m";
pub const COLOR_BLUE: &str = "\u{1b}[34m";
pub const COLOR_MAGENTA: &str = "\u{1b}[35m";
pub const COLOR_CYAN: &str = "\u{1b}[36m";
pub const COLOR_WHITE: &str = "\u{1b}[37m";
pub const COLOR_BRIGHT_RED: &str = "\u{1b}[91m";
pub const COLOR_BRIGHT_GREEN: &str = "\u{1b}[92m";
pub const COLOR_BRIGHT_YELLOW: &str = "\u{1b}[93m";
pub const COLOR_BRIGHT_BLUE: &str = "\u{1b}[94m";
pub const COLOR_BRIGHT_CYAN: &str = "\u{1b}[96m";

/// 颜色名 → ANSI 码映射（对齐 Python `_COLORS` 字典）。
///
/// 返回 `Option<&str>`：未知颜色返回 None。
pub fn color_code(color: &str) -> Option<&'static str> {
    match color {
        "reset" => Some(COLOR_RESET),
        "bold" => Some(COLOR_BOLD),
        "dim" => Some(COLOR_DIM),
        "red" => Some(COLOR_RED),
        "green" => Some(COLOR_GREEN),
        "yellow" => Some(COLOR_YELLOW),
        "blue" => Some(COLOR_BLUE),
        "magenta" => Some(COLOR_MAGENTA),
        "cyan" => Some(COLOR_CYAN),
        "white" => Some(COLOR_WHITE),
        "bright_red" => Some(COLOR_BRIGHT_RED),
        "bright_green" => Some(COLOR_BRIGHT_GREEN),
        "bright_yellow" => Some(COLOR_BRIGHT_YELLOW),
        "bright_blue" => Some(COLOR_BRIGHT_BLUE),
        "bright_cyan" => Some(COLOR_BRIGHT_CYAN),
        _ => None,
    }
}

// ============================================================
// 颜色检测（对齐 Python `should_use_color()` L84-112）
// ============================================================

/// 判定是否启用彩色输出。
///
/// 对齐 Python `cli/console.py:should_use_color()` (L84-112)
///
/// 判定规则（按优先级）：
/// 1. `NO_COLOR` 环境变量存在 → false
/// 2. stdout 非 TTY → false
/// 3. `FORCE_COLOR` 环境变量存在 → true
/// 4. 否则按 VT 模式启用结果决定
///
/// # 参数
/// - `no_color`: `NO_COLOR` 环境变量是否存在
/// - `is_tty`: stdout 是否为 TTY
/// - `force_color`: `FORCE_COLOR` 环境变量是否存在
/// - `vt_enabled`: VT 模式是否已启用（Windows 10+ 默认 true）
pub fn should_use_color(no_color: bool, is_tty: bool, force_color: bool, vt_enabled: bool) -> bool {
    if no_color {
        return false;
    }
    if !is_tty {
        return false;
    }
    if force_color {
        return true;
    }
    vt_enabled
}

/// 从环境变量和 stdout 检测是否启用彩色输出（生产代码用）。
///
/// 自动检测：
/// - `NO_COLOR` 环境变量存在 → false
/// - stdout 非 TTY → false
/// - `FORCE_COLOR` 环境变量存在 → true
/// - 否则假设 VT 已启用（Windows 10+ / Linux / macOS 默认支持 ANSI）
pub fn should_use_color_auto() -> bool {
    let no_color = std::env::var_os("NO_COLOR").is_some();
    let is_tty = std::io::stdout().is_terminal();
    let force_color = std::env::var_os("FORCE_COLOR").is_some();
    // 假设 VT 已启用（Windows 10+ / Linux / macOS 默认支持 ANSI）
    should_use_color(no_color, is_tty, force_color, true)
}

// ============================================================
// 彩色打印（对齐 Python `colorize` / `cprint` L115-146）
// ============================================================

/// 为文本添加 ANSI 颜色转义序列。
///
/// 对齐 Python `cli/console.py:colorize()` (L115-130)
///
/// `use_color=false` 时直接返回原文本。
/// 未知颜色码不套码（返回原文本）。
pub fn colorize(text: &str, color: &str, use_color: bool) -> String {
    if !use_color {
        return text.to_string();
    }
    match color_code(color) {
        Some(code) => format!("{}{}{}", code, text, COLOR_RESET),
        None => text.to_string(),
    }
}

/// 返回格式化后的字符串（不直接打印，便于组合）。
///
/// 对齐 Python `cli/console.py:cprint()` (L133-146)
///
/// `bold=true` 时先套 bold，再套 color。
/// 返回字符串由调用方决定打印时机（与 Python 直接 `print()` 不同，便于测试）。
pub fn cprint(text: &str, color: Option<&str>, bold: bool, use_color: bool) -> String {
    let mut result = text.to_string();
    if bold {
        result = colorize(&result, "bold", use_color);
    }
    if let Some(c) = color {
        result = colorize(&result, c, use_color);
    }
    result
}

// ============================================================
// 预定义消息函数（对齐 Python success/error/warning/info/dim/bold）
// ============================================================

/// 生成成功消息（绿色 ✓ 前缀）。
///
/// 对齐 Python `cli/console.py:success()` (L149-158)
pub fn success(msg: &str, use_color: bool) -> String {
    colorize(&format!("\u{2713} {}", msg), "green", use_color)
}

/// 生成错误消息（红色 ✗ 前缀）。
///
/// 对齐 Python `cli/console.py:error()` (L161-170)
pub fn error(msg: &str, use_color: bool) -> String {
    colorize(&format!("\u{2717} {}", msg), "red", use_color)
}

/// 生成警告消息（黄色 ⚠ 前缀）。
///
/// 对齐 Python `cli/console.py:warning()` (L173-182)
pub fn warning(msg: &str, use_color: bool) -> String {
    colorize(&format!("\u{26a0} {}", msg), "yellow", use_color)
}

/// 生成信息消息（蓝色 ℹ 前缀）。
///
/// 对齐 Python `cli/console.py:info()` (L185-194)
pub fn info(msg: &str, use_color: bool) -> String {
    colorize(&format!("\u{2139} {}", msg), "blue", use_color)
}

/// 生成 dim 消息（暗色文本，无前缀）。
///
/// 对齐 Python `cli/console.py:dim()` (L197-206)
pub fn dim(msg: &str, use_color: bool) -> String {
    colorize(msg, "dim", use_color)
}

/// 生成 bold 消息（加粗文本，无前缀）。
///
/// 对齐 Python `cli/console.py:bold()` (L209-220)
pub fn bold(msg: &str, use_color: bool) -> String {
    colorize(msg, "bold", use_color)
}

// ============================================================
// 格式化工具（对齐 Python `format_duration` / `format_size`）
// ============================================================

/// 将秒数格式化为人类可读的时长字符串。
///
/// 对齐 Python `cli/console.py:format_duration()` (L274-297)
///
/// 规则：
/// - `< 0.001s` → `{ms:.1f}ms`
/// - `< 1s` → `{ms:.0f}ms`
/// - `< 60s` → `{s:.1f}s`
/// - `< 60m` → `{m}m{s:.0f}s`
/// - `>= 60m` → `{h}h{m}m`
pub fn format_duration(seconds: f64) -> String {
    if seconds < 0.001 {
        return format!("{:.1}ms", seconds * 1000.0);
    }
    if seconds < 1.0 {
        return format!("{:.0}ms", seconds * 1000.0);
    }
    if seconds < 60.0 {
        return format!("{:.1}s", seconds);
    }
    let total_m = (seconds / 60.0) as i64;
    let s = seconds % 60.0;
    if total_m < 60 {
        return format!("{}m{:.0}s", total_m, s);
    }
    let h = total_m / 60;
    let m = total_m % 60;
    format!("{}h{}m", h, m)
}

/// 将字节数格式化为人类可读的大小字符串。
///
/// 对齐 Python `cli/console.py:format_size()` (L300-313)
///
/// 规则：
/// - `< 1024` → `{n} B`
/// - `< 1024*1024` → `{kb:.1f} KB`
/// - `>= 1024*1024` → `{mb:.1f} MB`
pub fn format_size(n: u64) -> String {
    if n < 1024 {
        return format!("{} B", n);
    }
    if n < 1024 * 1024 {
        return format!("{:.1} KB", n as f64 / 1024.0);
    }
    format!("{:.1} MB", n as f64 / 1024.0 / 1024.0)
}

// ============================================================
// JSON 输出（对齐 Python `json.dumps(data, indent=2, ensure_ascii=False)`）
// ============================================================

/// 将 JSON 字符串重新序列化为带缩进、非 ASCII 转义禁用的格式。
///
/// 对齐 Python `json.dumps(data, indent=2, ensure_ascii=False)`
///
/// # 实现
/// 使用 `serde_json::Value` 解析后，手动序列化以禁用非 ASCII 转义。
/// `serde_json::to_string_pretty` 默认会对非 ASCII 字符转义（如 `\u4e2d`），
/// 本函数通过自定义 `Formatter` 禁用转义。
///
/// # 参数
/// - `json_str`: 已序列化的 JSON 字符串（紧凑或带空格均可）
///
/// # 返回
/// 带缩进（2 空格）+ 非 ASCII 字符不转义的 JSON 字符串。
/// 解析失败时返回 `"null"`。
pub fn json_dumps_pretty(json_str: &str) -> String {
    let v: Value = match serde_json::from_str(json_str) {
        Ok(val) => val,
        Err(_) => return "null".to_string(),
    };
    json_value_to_pretty_string(&v)
}

/// 将 `serde_json::Value` 序列化为带缩进、非 ASCII 转义禁用的字符串。
///
/// 对齐 Python `json.dumps(data, indent=2, ensure_ascii=False)`
pub fn json_value_to_pretty_string(v: &Value) -> String {
    let mut buf = Vec::new();
    let formatter = serde_json::ser::PrettyFormatter::with_indent(b"  ");
    let mut ser = serde_json::Serializer::with_formatter(&mut buf, formatter);
    // 使用 serialize 保持非 ASCII 字符（serde_json 默认不转义非 ASCII）
    // 注意：serde_json 的 Serializer 默认对非 ASCII 字符不转义（与 Python ensure_ascii=False 一致）
    serde::Serialize::serialize(v, &mut ser).unwrap_or(());
    String::from_utf8_lossy(&buf).to_string()
}

// ============================================================
// PyO3 暴露（供 Python wire-production 调用）
// ============================================================

use pyo3::prelude::*;

/// Python 暴露的 should_use_color。
///
/// 对齐 Python `cli/console.py:should_use_color()`
#[pyfunction]
#[pyo3(signature = (no_color, is_tty, force_color, vt_enabled))]
pub fn should_use_color_py(
    no_color: bool,
    is_tty: bool,
    force_color: bool,
    vt_enabled: bool,
) -> bool {
    should_use_color(no_color, is_tty, force_color, vt_enabled)
}

/// Python 暴露的 should_use_color_auto（从环境变量自动检测）。
#[pyfunction]
pub fn should_use_color_auto_py() -> bool {
    should_use_color_auto()
}

/// Python 暴露的 colorize。
///
/// 对齐 Python `cli/console.py:colorize()`
#[pyfunction]
pub fn colorize_py(text: &str, color: &str, use_color: bool) -> String {
    colorize(text, color, use_color)
}

/// Python 暴露的 cprint。
///
/// 对齐 Python `cli/console.py:cprint()`
///
/// 注意：Rust 版返回字符串（不直接打印），由调用方决定打印时机。
#[pyfunction]
#[pyo3(signature = (text, color=None, bold=false, use_color=false))]
pub fn cprint_py(text: &str, color: Option<&str>, bold: bool, use_color: bool) -> String {
    cprint(text, color, bold, use_color)
}

/// Python 暴露的 success。
#[pyfunction]
pub fn success_py(msg: &str, use_color: bool) -> String {
    success(msg, use_color)
}

/// Python 暴露的 error。
#[pyfunction]
pub fn error_py(msg: &str, use_color: bool) -> String {
    error(msg, use_color)
}

/// Python 暴露的 warning。
#[pyfunction]
pub fn warning_py(msg: &str, use_color: bool) -> String {
    warning(msg, use_color)
}

/// Python 暴露的 info。
#[pyfunction]
pub fn info_py(msg: &str, use_color: bool) -> String {
    info(msg, use_color)
}

/// Python 暴露的 dim。
#[pyfunction]
pub fn dim_py(msg: &str, use_color: bool) -> String {
    dim(msg, use_color)
}

/// Python 暴露的 bold。
#[pyfunction]
pub fn bold_py(msg: &str, use_color: bool) -> String {
    bold(msg, use_color)
}

/// Python 暴露的 format_duration。
#[pyfunction]
pub fn format_duration_py(seconds: f64) -> String {
    format_duration(seconds)
}

/// Python 暴露的 format_size。
#[pyfunction]
pub fn format_size_py(n: u64) -> String {
    format_size(n)
}

/// Python 暴露的 json_dumps_pretty。
///
/// 对齐 Python `json.dumps(data, indent=2, ensure_ascii=False)`
#[pyfunction]
pub fn json_dumps_pretty_py(json_str: &str) -> String {
    json_dumps_pretty(json_str)
}

// ============================================================
// 单元测试（对齐契约 D1-D6 测试矩阵）
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;

    // D1: colorize

    #[test]
    fn test_d1_1_colorize_red_with_color() {
        let result = colorize("hello", "red", true);
        assert_eq!(result, format!("{}hello{}", COLOR_RED, COLOR_RESET));
    }

    #[test]
    fn test_d1_2_colorize_red_without_color() {
        let result = colorize("hello", "red", false);
        assert_eq!(result, "hello");
    }

    #[test]
    fn test_d1_3_colorize_unknown_color() {
        let result = colorize("hello", "unknown", true);
        assert_eq!(result, "hello");
    }

    #[test]
    fn test_d1_4_colorize_empty_text() {
        let result = colorize("", "red", true);
        assert_eq!(result, format!("{}{}", COLOR_RED, COLOR_RESET));
    }

    // D2: should_use_color

    #[test]
    fn test_d2_1_no_color_env_disables() {
        assert!(!should_use_color(true, true, true, true));
    }

    #[test]
    fn test_d2_2_non_tty_disables() {
        assert!(!should_use_color(false, false, true, true));
    }

    #[test]
    fn test_d2_3_force_color_enables() {
        assert!(should_use_color(false, true, true, false));
    }

    #[test]
    fn test_d2_4_vt_enabled_enables() {
        assert!(should_use_color(false, true, false, true));
    }

    #[test]
    fn test_d2_5_vt_disabled_disables() {
        assert!(!should_use_color(false, true, false, false));
    }

    // D3: 预定义消息函数

    #[test]
    fn test_d3_1_success_with_color() {
        let result = success("done", true);
        assert_eq!(
            result,
            format!("{}\u{2713} done{}", COLOR_GREEN, COLOR_RESET)
        );
    }

    #[test]
    fn test_d3_2_error_with_color() {
        let result = error("fail", true);
        assert_eq!(result, format!("{}\u{2717} fail{}", COLOR_RED, COLOR_RESET));
    }

    #[test]
    fn test_d3_3_warning_with_color() {
        let result = warning("warn", true);
        assert_eq!(
            result,
            format!("{}\u{26a0} warn{}", COLOR_YELLOW, COLOR_RESET)
        );
    }

    #[test]
    fn test_d3_4_info_with_color() {
        let result = info("info", true);
        assert_eq!(
            result,
            format!("{}\u{2139} info{}", COLOR_BLUE, COLOR_RESET)
        );
    }

    #[test]
    fn test_d3_5_dim_with_color() {
        let result = dim("dim", true);
        assert_eq!(result, format!("{}dim{}", COLOR_DIM, COLOR_RESET));
    }

    #[test]
    fn test_d3_6_bold_with_color() {
        let result = bold("bold", true);
        assert_eq!(result, format!("{}bold{}", COLOR_BOLD, COLOR_RESET));
    }

    #[test]
    fn test_d3_7_success_without_color() {
        let result = success("done", false);
        assert_eq!(result, "\u{2713} done");
    }

    // D4: format_duration

    #[test]
    fn test_d4_1_very_small_ms() {
        assert_eq!(format_duration(0.0005), "0.5ms");
    }

    #[test]
    fn test_d4_2_ms_range() {
        assert_eq!(format_duration(0.12), "120ms");
    }

    #[test]
    fn test_d4_3_seconds_range() {
        assert_eq!(format_duration(3.5), "3.5s");
    }

    #[test]
    fn test_d4_4_minutes_range() {
        assert_eq!(format_duration(150.0), "2m30s");
    }

    #[test]
    fn test_d4_5_hours_range() {
        // 3900s = 65m = 1h5m
        assert_eq!(format_duration(3900.0), "1h5m");
    }

    #[test]
    fn test_d4_6_zero() {
        assert_eq!(format_duration(0.0), "0.0ms");
    }

    // D5: format_size

    #[test]
    fn test_d5_1_zero_bytes() {
        assert_eq!(format_size(0), "0 B");
    }

    #[test]
    fn test_d5_2_small_bytes() {
        assert_eq!(format_size(512), "512 B");
    }

    #[test]
    fn test_d5_3_just_below_kb() {
        assert_eq!(format_size(1023), "1023 B");
    }

    #[test]
    fn test_d5_4_exactly_1kb() {
        assert_eq!(format_size(1024), "1.0 KB");
    }

    #[test]
    fn test_d5_5_1_5_kb() {
        assert_eq!(format_size(1536), "1.5 KB");
    }

    #[test]
    fn test_d5_6_exactly_1mb() {
        assert_eq!(format_size(1048576), "1.0 MB");
    }

    #[test]
    fn test_d5_7_1_5_mb() {
        assert_eq!(format_size(1572864), "1.5 MB");
    }

    // D6: json_dumps_pretty

    #[test]
    fn test_d6_1_simple_object() {
        let input = r#"{"a":1,"b":2}"#;
        let result = json_dumps_pretty(input);
        let expected = "{\n  \"a\": 1,\n  \"b\": 2\n}";
        assert_eq!(result, expected);
    }

    #[test]
    fn test_d6_2_non_ascii_not_escaped() {
        let input = r#"{"name":"中文"}"#;
        let result = json_dumps_pretty(input);
        // 非 ASCII 字符（中文）不应被转义为 \uXXXX
        assert!(
            !result.contains("\\u"),
            "Non-ASCII should not be escaped: {}",
            result
        );
        assert!(
            result.contains("中文"),
            "Chinese characters should be preserved: {}",
            result
        );
    }

    #[test]
    fn test_d6_3_array() {
        let input = r#"[1,2,3]"#;
        let result = json_dumps_pretty(input);
        let expected = "[\n  1,\n  2,\n  3\n]";
        assert_eq!(result, expected);
    }

    #[test]
    fn test_d6_4_invalid_json_returns_null() {
        let result = json_dumps_pretty("not valid json");
        assert_eq!(result, "null");
    }

    #[test]
    fn test_d6_5_nested_object() {
        let input = r#"{"outer":{"inner":"value"}}"#;
        let result = json_dumps_pretty(input);
        let expected = "{\n  \"outer\": {\n    \"inner\": \"value\"\n  }\n}";
        assert_eq!(result, expected);
    }

    // 额外：color_code 映射

    #[test]
    fn test_color_code_all_known() {
        let colors = [
            "reset",
            "bold",
            "dim",
            "red",
            "green",
            "yellow",
            "blue",
            "magenta",
            "cyan",
            "white",
            "bright_red",
            "bright_green",
            "bright_yellow",
            "bright_blue",
            "bright_cyan",
        ];
        for c in &colors {
            assert!(
                color_code(c).is_some(),
                "color_code({}) should return Some",
                c
            );
        }
    }

    #[test]
    fn test_color_code_unknown() {
        assert!(color_code("unknown").is_none());
        assert!(color_code("").is_none());
    }

    // 额外：cprint 组合

    #[test]
    fn test_cprint_bold_and_color() {
        let result = cprint("text", Some("red"), true, true);
        // bold 先套，再套 red
        let expected = format!(
            "{}{}text{}{}",
            COLOR_RED, COLOR_BOLD, COLOR_RESET, COLOR_RESET
        );
        assert_eq!(result, expected);
    }

    #[test]
    fn test_cprint_no_color_no_bold() {
        let result = cprint("text", None, false, false);
        assert_eq!(result, "text");
    }
}
