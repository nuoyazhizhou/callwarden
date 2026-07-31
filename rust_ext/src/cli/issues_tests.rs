//! `cw issues` 与 `cw tests` 只读模式的查询和兼容输出。

use std::mem::MaybeUninit;

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Map, Value};

/// 查询符号的全部 Semgrep 与 Guardrail 问题。
pub fn query_local_issues(
    conn: &Connection,
    workspace_id: i64,
    qualified_name: &str,
    include_info: bool,
) -> Result<Value, String> {
    let symbol = conn
        .query_row(
            "
            SELECT fi.id, fi.rel_path, fsv.start_line, fsv.end_line, fsv.symbol_hash
            FROM file_symbol_versions fsv
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ?1
              AND fi.status != 'archived'
              AND fv.is_current = 1
              AND fsv.is_deleted = 0
              AND fsv.qualified_name = ?2
            LIMIT 1
            ",
            params![workspace_id, qualified_name],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, i64>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, String>(4)?,
                ))
            },
        )
        .optional()
        .map_err(|error| format!("cannot query issue symbol: {error}"))?;
    let Some((file_instance_id, file_path, start_line, end_line, symbol_hash)) = symbol else {
        return Ok(Value::Array(Vec::new()));
    };

    let mut issues = Vec::new();
    let mut semgrep_sql = "
        SELECT rule_id, rule_name, severity, confidence, message,
               start_line, end_line, snippet, fix
        FROM semgrep_findings
        WHERE file_instance_id = ?1
          AND (symbol_qualified = ?2 OR symbol_qualified = ''
               OR (start_line BETWEEN ?3 AND ?4 AND end_line BETWEEN ?3 AND ?4))
    "
    .to_string();
    if !include_info {
        semgrep_sql.push_str(" AND severity != 'INFO' AND severity != 'UNKNOWN'");
    }
    semgrep_sql.push_str(
        "
        ORDER BY CASE severity WHEN 'ERROR' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END,
                 start_line
        ",
    );
    if let Ok(mut stmt) = conn.prepare(&semgrep_sql) {
        if let Ok(rows) = stmt.query_map(
            params![file_instance_id, qualified_name, start_line, end_line],
            |row| {
                Ok(json!({
                    "rule_id": row.get::<_, String>(0)?,
                    "rule_name": row.get::<_, String>(1)?,
                    "severity": row.get::<_, String>(2)?,
                    "confidence": row.get::<_, String>(3)?,
                    "message": row.get::<_, String>(4)?,
                    "start_line": row.get::<_, i64>(5)?,
                    "end_line": row.get::<_, i64>(6)?,
                    "snippet": row.get::<_, String>(7)?,
                    "fix": row.get::<_, String>(8)?,
                    "source": "semgrep",
                }))
            },
        ) {
            issues.extend(rows.filter_map(Result::ok));
        }
    }

    let mut guard_sql = "
        SELECT gf.rule_id, gr.category, gf.severity, gf.message,
               gf.status, gf.detected_at
        FROM guardrail_findings gf
        JOIN guardrail_rules gr ON gf.rule_id = gr.rule_id
        WHERE gf.file_path = ?1 AND gf.symbol_hash = ?2
    "
    .to_string();
    if !include_info {
        guard_sql.push_str(" AND gf.severity != 'info'");
    }
    guard_sql.push_str(
        "
        ORDER BY CASE gf.severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END
        ",
    );
    if let Ok(mut stmt) = conn.prepare(&guard_sql) {
        if let Ok(rows) = stmt.query_map(params![file_path, symbol_hash], |row| {
            Ok(json!({
                "rule_id": row.get::<_, String>(0)?,
                "rule_name": row.get::<_, String>(1)?,
                "severity": row.get::<_, String>(2)?,
                "message": row.get::<_, String>(3)?,
                "status": row.get::<_, String>(4)?,
                "detected_at": row.get::<_, f64>(5)?,
                "start_line": 0,
                "end_line": 0,
                "source": "guardrail",
            }))
        }) {
            issues.extend(rows.filter_map(Result::ok));
        }
    }
    Ok(Value::Array(issues))
}

/// 查询被测符号关联的测试函数。
pub fn query_local_test_cases(
    conn: &Connection,
    workspace_id: i64,
    qualified_name: &str,
) -> Result<Value, String> {
    let mut stmt = conn
        .prepare(
            "
            SELECT tcr.test_fn_id, tcr.match_method, tcr.confidence,
                   s.name, s.qualified_name, s.start_line, fi.rel_path
            FROM test_case_relations tcr
            JOIN symbols s ON tcr.test_fn_id = s.id
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE tcr.workspace_id = ?1
              AND tcr.tested_fn_id = (
                SELECT s2.id FROM symbols s2
                JOIN file_instances fi2 ON s2.file_instance_id = fi2.id
                WHERE fi2.workspace_id = ?1 AND s2.qualified_name = ?2
                LIMIT 1
              )
            ORDER BY CASE tcr.confidence WHEN 'high' THEN 0 WHEN 'mid' THEN 1 ELSE 2 END
            ",
        )
        .map_err(|error| format!("cannot prepare test case query: {error}"))?;
    let rows = stmt
        .query_map(params![workspace_id, qualified_name], |row| {
            Ok(json!({
                "test_fn_id": row.get::<_, i64>(0)?,
                "match_method": row.get::<_, String>(1)?,
                "confidence": row.get::<_, String>(2)?,
                "test_name": row.get::<_, String>(3)?,
                "test_qualified_name": row.get::<_, String>(4)?,
                "test_start_line": row.get::<_, i64>(5)?,
                "test_file": row.get::<_, String>(6)?,
            }))
        })
        .map_err(|error| format!("cannot query test cases: {error}"))?;
    collect_values(rows, "test case")
}

/// 反向查询测试函数覆盖的生产函数。
pub fn query_local_tested_functions(
    conn: &Connection,
    workspace_id: i64,
    qualified_name: &str,
) -> Result<Value, String> {
    let mut stmt = conn
        .prepare(
            "
            SELECT tcr.tested_fn_id, tcr.match_method, tcr.confidence,
                   s.name, s.qualified_name, s.start_line, s.end_line, fi.rel_path
            FROM test_case_relations tcr
            JOIN symbols s ON tcr.tested_fn_id = s.id
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE tcr.workspace_id = ?1
              AND tcr.test_fn_id = (
                SELECT s2.id FROM symbols s2
                JOIN file_instances fi2 ON s2.file_instance_id = fi2.id
                WHERE fi2.workspace_id = ?1 AND s2.qualified_name = ?2
                LIMIT 1
              )
            ORDER BY CASE tcr.confidence WHEN 'high' THEN 0 WHEN 'mid' THEN 1 ELSE 2 END
            ",
        )
        .map_err(|error| format!("cannot prepare tested function query: {error}"))?;
    let rows = stmt
        .query_map(params![workspace_id, qualified_name], |row| {
            Ok(json!({
                "tested_fn_id": row.get::<_, i64>(0)?,
                "match_method": row.get::<_, String>(1)?,
                "confidence": row.get::<_, String>(2)?,
                "tested_name": row.get::<_, String>(3)?,
                "tested_qualified_name": row.get::<_, String>(4)?,
                "tested_start_line": row.get::<_, i64>(5)?,
                "tested_end_line": row.get::<_, i64>(6)?,
                "tested_file": row.get::<_, String>(7)?,
            }))
        })
        .map_err(|error| format!("cannot query tested functions: {error}"))?;
    collect_values(rows, "tested function")
}

/// 查询关联测试的运行稳定性。
pub fn query_local_test_stability(
    conn: &Connection,
    workspace_id: i64,
    qualified_name: &str,
    limit: usize,
) -> Result<Value, String> {
    let tested_fn_id = conn
        .query_row(
            "
            SELECT s.id FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?1 AND s.qualified_name = ?2
            LIMIT 1
            ",
            params![workspace_id, qualified_name],
            |row| row.get::<_, i64>(0),
        )
        .optional()
        .map_err(|error| format!("cannot query tested symbol: {error}"))?;
    let Some(tested_fn_id) = tested_fn_id else {
        return Ok(empty_stability());
    };

    let limit =
        i64::try_from(limit).map_err(|_| format!("test history limit is too large: {limit}"))?;
    let mut stmt = conn
        .prepare(
            "
            SELECT tr.status, tr.duration_ms, tr.error_message, tr.error_type,
                   tr.run_at, tr.test_name, tr.ci_run_id
            FROM test_runs tr
            WHERE tr.workspace_id = ?1 AND tr.test_fn_id IN (
                SELECT test_fn_id FROM test_case_relations
                WHERE workspace_id = ?1 AND tested_fn_id = ?2
            )
            ORDER BY tr.run_at DESC
            LIMIT ?3
            ",
        )
        .map_err(|error| format!("cannot prepare test stability query: {error}"))?;
    let rows = stmt
        .query_map(params![workspace_id, tested_fn_id, limit], |row| {
            Ok(TestRun {
                status: row.get(0)?,
                duration_ms: row.get(1)?,
                error_message: row.get(2)?,
                error_type: row.get(3)?,
                run_at: row.get(4)?,
                test_name: row.get(5)?,
            })
        })
        .map_err(|error| format!("cannot query test stability: {error}"))?;
    let runs = rows
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("cannot read test run: {error}"))?;
    if runs.is_empty() {
        return Ok(empty_stability());
    }

    let total = runs.len();
    let passed = runs.iter().filter(|run| run.status == "passed").count();
    let avg_duration = runs.iter().map(|run| run.duration_ms).sum::<f64>() / total as f64;
    let recent_failures = runs
        .iter()
        .filter(|run| run.status == "failed" || run.status == "error")
        .take(10)
        .map(|run| {
            json!({
                "test_name": run.test_name,
                "error_type": run.error_type,
                "error_message": truncate_chars(&run.error_message, 200),
                "run_at": run.run_at,
            })
        })
        .collect::<Vec<_>>();

    let mut by_test = Map::new();
    for run in &runs {
        let stats = by_test
            .entry(run.test_name.clone())
            .or_insert_with(|| json!({"total": 0, "passed": 0, "failed": 0}));
        let stats = stats
            .as_object_mut()
            .expect("test stability stats are objects");
        increment_json_count(stats, "total");
        if run.status == "passed" {
            increment_json_count(stats, "passed");
        } else if run.status == "failed" || run.status == "error" {
            increment_json_count(stats, "failed");
        }
    }

    let pass_rate = round_to(passed as f64 / total as f64, 3);
    Ok(json!({
        "total_runs": total,
        "pass_rate": pass_rate,
        "avg_duration_ms": round_to(avg_duration, 1),
        "recent_failures": recent_failures,
        "by_test": by_test,
    }))
}

/// 格式化 `cw issues` 的人类可读输出。
pub fn format_issues_output(
    issues: &Value,
    qualified_name: &str,
    include_info: bool,
) -> Result<String, String> {
    let issues = require_array(issues, "issues")?;
    if issues.is_empty() {
        return Ok(format!(
            "No issues found for: {qualified_name}\n\
             （可能原因：1. 未运行 semgrep 扫描；2. 符号无 WARNING+ 问题；3. 符号不存在）"
        ));
    }

    let mut by_source = vec![("semgrep".to_string(), 0), ("guardrail".to_string(), 0)];
    let mut by_severity = Vec::new();
    for issue in issues {
        let issue = require_object(issue, "issue")?;
        increment_ordered_count(&mut by_source, &string_field(issue, "source", "?"));
        increment_ordered_count(
            &mut by_severity,
            &string_field(issue, "severity", "?").to_uppercase(),
        );
    }
    sort_counts_desc(&mut by_source);
    sort_counts_desc(&mut by_severity);
    let info_note = if include_info { " (+INFO)" } else { "" };
    let mut lines = vec![
        format!(
            "Issues for {qualified_name}: {} total{info_note}",
            issues.len()
        ),
        format!("  by severity: {}", display_counts(&by_severity)),
        format!("  by source:   {}", display_counts(&by_source)),
        String::new(),
    ];

    for (index, issue) in issues.iter().enumerate() {
        let issue = require_object(issue, "issue")?;
        let source = string_field(issue, "source", "?");
        let severity = string_field(issue, "severity", "?").to_uppercase();
        let rule_id = string_field(issue, "rule_id", "?");
        let rule_name = string_field(issue, "rule_name", "");
        let message = string_field(issue, "message", "");
        let start_line = integer_field(issue, "start_line", 0);
        let end_line = integer_field(issue, "end_line", 0);
        let confidence = string_field(issue, "confidence", "");
        let status = string_field(issue, "status", "");
        let snippet = string_field(issue, "snippet", "");
        let fix = string_field(issue, "fix", "");

        let line_range = if start_line == end_line || end_line == 0 {
            format!("L{start_line}")
        } else {
            format!("L{start_line}-{end_line}")
        };
        let line_info = if start_line == 0 {
            String::new()
        } else {
            format!(" {line_range}")
        };
        let confidence_info = if confidence.is_empty() || confidence == "UNKNOWN" {
            String::new()
        } else {
            format!(" conf={confidence}")
        };
        let status_info = if status.is_empty() {
            String::new()
        } else {
            format!(" [{status}]")
        };
        lines.push(format!(
            "[{}] [{source}] [{severity}] {rule_id}{line_info}{confidence_info}{status_info}",
            index + 1
        ));
        if !rule_name.is_empty() {
            lines.push(format!("    rule: {rule_name}"));
        }
        if !message.is_empty() {
            lines.push(format!("    msg:  {message}"));
        }
        if !snippet.is_empty() {
            let snippet_lines = snippet.trim().split('\n').collect::<Vec<_>>();
            for line in snippet_lines.iter().take(3) {
                lines.push(format!("    code: {line}"));
            }
            if snippet_lines.len() > 3 {
                lines.push(format!(
                    "    code: ... ({} more lines)",
                    snippet_lines.len() - 3
                ));
            }
        }
        if !fix.is_empty() {
            lines.push(format!("    fix:  {fix}"));
        }
        lines.push(String::new());
    }
    lines.push(format!("Total: {} issues", issues.len()));
    Ok(lines.join("\n"))
}

/// 格式化正向测试关联。
pub fn format_test_cases_output(cases: &Value, qualified_name: &str) -> Result<String, String> {
    let cases = require_array(cases, "test cases")?;
    if cases.is_empty() {
        return Ok(format!(
            "No test cases found for: {qualified_name}\n\
             （可能原因：1. 未运行 cw tests --build；2. 此函数无测试；3. 符号不存在）\n\
             提示：运行 'cw tests --build' 重建测试关联表"
        ));
    }

    let mut by_confidence = Vec::new();
    let mut by_method = Vec::new();
    for case in cases {
        let case = require_object(case, "test case")?;
        increment_ordered_count(&mut by_confidence, &string_field(case, "confidence", "?"));
        increment_ordered_count(&mut by_method, &string_field(case, "match_method", "?"));
    }
    sort_counts_desc(&mut by_confidence);
    sort_counts_desc(&mut by_method);
    let mut lines = vec![
        format!("Test cases for {qualified_name}: {} total", cases.len()),
        format!("  by confidence: {}", display_counts(&by_confidence)),
        format!("  by method:     {}", display_counts(&by_method)),
        String::new(),
    ];
    for (index, case) in cases.iter().enumerate() {
        let case = require_object(case, "test case")?;
        let method = string_field(case, "match_method", "?");
        let confidence = string_field(case, "confidence", "?");
        let test_qualified = string_field(case, "test_qualified_name", "?");
        let test_file = string_field(case, "test_file", "");
        let test_line = integer_field(case, "test_start_line", 0);
        lines.push(format!(
            "[{}] [{method}] [{confidence}] {test_qualified}",
            index + 1
        ));
        if !test_file.is_empty() {
            lines.push(format!("    file: {test_file}:{test_line}"));
        }
    }
    lines.push(String::new());
    lines.push(format!("Total: {} test cases", cases.len()));
    Ok(lines.join("\n"))
}

/// 格式化反向测试关联。
pub fn format_tested_functions_output(
    functions: &Value,
    qualified_name: &str,
) -> Result<String, String> {
    let functions = require_array(functions, "tested functions")?;
    if functions.is_empty() {
        return Ok(format!(
            "No tested functions found for: {qualified_name}\n\
             （可能原因：1. 未运行 cw tests --build；2. 此 test_fn 未关联到任何函数；3. 符号不存在）"
        ));
    }
    let mut lines = vec![
        format!(
            "Tested functions for {qualified_name}: {} total",
            functions.len()
        ),
        String::new(),
    ];
    for (index, function) in functions.iter().enumerate() {
        let function = require_object(function, "tested function")?;
        let method = string_field(function, "match_method", "?");
        let confidence = string_field(function, "confidence", "?");
        let tested_qualified = string_field(function, "tested_qualified_name", "?");
        let tested_file = string_field(function, "tested_file", "");
        let line = integer_field(function, "tested_start_line", 0);
        lines.push(format!(
            "[{}] [{method}] [{confidence}] {tested_qualified}",
            index + 1
        ));
        if !tested_file.is_empty() {
            lines.push(format!("    file: {tested_file}:{line}"));
        }
    }
    lines.push(String::new());
    lines.push(format!("Total: {} tested functions", functions.len()));
    Ok(lines.join("\n"))
}

/// 格式化测试稳定性历史。
pub fn format_test_stability_output(
    stability: &Value,
    qualified_name: &str,
) -> Result<String, String> {
    let stability = require_object(stability, "test stability")?;
    let total_runs = integer_field(stability, "total_runs", 0);
    let mut lines = vec![
        format!("Test stability for {qualified_name}:"),
        format!("  Total runs:   {total_runs}"),
    ];
    if total_runs == 0 {
        lines.push("  (no test runs found)".to_string());
        lines.push("  提示：先运行 'cw tests --import <junit.xml>' 导入 CI 测试结果".to_string());
        return Ok(lines.join("\n"));
    }

    let pass_rate = number_field(stability, "pass_rate", 0.0);
    let avg_duration = number_field(stability, "avg_duration_ms", 0.0);
    lines.push(format!("  Pass rate:    {:.1}%", pass_rate * 100.0));
    lines.push(format!("  Avg duration: {avg_duration:.1} ms"));

    let failures = stability
        .get("recent_failures")
        .and_then(Value::as_array)
        .ok_or_else(|| "recent_failures must be an array".to_string())?;
    if !failures.is_empty() {
        lines.push(String::new());
        lines.push(format!("Recent failures (top {}):", failures.len()));
        for failure in failures {
            let failure = require_object(failure, "recent failure")?;
            let test_name = string_field(failure, "test_name", "");
            let error_type = {
                let value = string_field(failure, "error_type", "");
                if value.is_empty() {
                    "?".to_string()
                } else {
                    value
                }
            };
            let run_at = number_field(failure, "run_at", 0.0);
            lines.push(format!(
                "  - {test_name} [{error_type}] @ {}",
                format_local_timestamp(run_at)?
            ));
            let message = string_field(failure, "error_message", "");
            if !message.is_empty() {
                lines.push(format!("    {}", truncate_chars(&message, 100)));
            }
        }
    }

    let by_test = stability
        .get("by_test")
        .and_then(Value::as_object)
        .ok_or_else(|| "by_test must be an object".to_string())?;
    if !by_test.is_empty() {
        let mut tests = by_test.iter().collect::<Vec<_>>();
        tests.sort_by(|left, right| {
            let left_stats = left.1.as_object().expect("by_test values are objects");
            let right_stats = right.1.as_object().expect("by_test values are objects");
            let left_total = integer_field(left_stats, "total", 0);
            let right_total = integer_field(right_stats, "total", 0);
            let left_passed = integer_field(left_stats, "passed", 0);
            let right_passed = integer_field(right_stats, "passed", 0);
            let left_rate = if left_total == 0 {
                1.0
            } else {
                left_passed as f64 / left_total as f64
            };
            let right_rate = if right_total == 0 {
                1.0
            } else {
                right_passed as f64 / right_total as f64
            };
            left_rate
                .total_cmp(&right_rate)
                .then_with(|| left_total.cmp(&right_total))
        });
        lines.push(String::new());
        lines.push("By test (pass/total):".to_string());
        for (name, stats) in tests {
            let stats = require_object(stats, "by_test stats")?;
            let total = integer_field(stats, "total", 0);
            let passed = integer_field(stats, "passed", 0);
            let failed = integer_field(stats, "failed", 0);
            let rate = if total == 0 {
                0.0
            } else {
                passed as f64 * 100.0 / total as f64
            };
            let failed_text = if failed == 0 {
                String::new()
            } else {
                format!(", {failed} failed")
            };
            lines.push(format!(
                "  {name}: {passed}/{total} ({rate:.0}%{failed_text})"
            ));
        }
    }
    Ok(lines.join("\n"))
}

fn collect_values(
    rows: rusqlite::MappedRows<'_, impl FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<Value>>,
    label: &str,
) -> Result<Value, String> {
    rows.collect::<Result<Vec<_>, _>>()
        .map(Value::Array)
        .map_err(|error| format!("cannot read {label}: {error}"))
}

fn empty_stability() -> Value {
    json!({
        "total_runs": 0,
        "pass_rate": 0.0,
        "avg_duration_ms": 0,
        "recent_failures": [],
        "by_test": {},
    })
}

#[derive(Debug)]
struct TestRun {
    status: String,
    duration_ms: f64,
    error_message: String,
    error_type: String,
    run_at: f64,
    test_name: String,
}

fn increment_json_count(stats: &mut Map<String, Value>, key: &str) {
    let next = stats.get(key).and_then(Value::as_i64).unwrap_or(0) + 1;
    stats.insert(key.to_string(), Value::from(next));
}

fn round_to(value: f64, digits: u32) -> f64 {
    let factor = 10_f64.powi(digits as i32);
    (value * factor).round() / factor
}

fn require_array<'a>(value: &'a Value, label: &str) -> Result<&'a Vec<Value>, String> {
    value
        .as_array()
        .ok_or_else(|| format!("{label} must be an array"))
}

fn require_object<'a>(value: &'a Value, label: &str) -> Result<&'a Map<String, Value>, String> {
    value
        .as_object()
        .ok_or_else(|| format!("{label} must be an object"))
}

fn string_field(object: &Map<String, Value>, key: &str, default: &str) -> String {
    object
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or(default)
        .to_string()
}

fn integer_field(object: &Map<String, Value>, key: &str, default: i64) -> i64 {
    object.get(key).and_then(Value::as_i64).unwrap_or(default)
}

fn number_field(object: &Map<String, Value>, key: &str, default: f64) -> f64 {
    object.get(key).and_then(Value::as_f64).unwrap_or(default)
}

fn increment_ordered_count(counts: &mut Vec<(String, usize)>, key: &str) {
    if let Some((_, count)) = counts.iter_mut().find(|(item, _)| item == key) {
        *count += 1;
    } else {
        counts.push((key.to_string(), 1));
    }
}

fn sort_counts_desc(counts: &mut [(String, usize)]) {
    counts.sort_by(|left, right| right.1.cmp(&left.1));
}

fn display_counts(counts: &[(String, usize)]) -> String {
    counts
        .iter()
        .map(|(key, count)| format!("{key}:{count}"))
        .collect::<Vec<_>>()
        .join(", ")
}

fn truncate_chars(value: &str, limit: usize) -> String {
    value.chars().take(limit).collect()
}

fn format_local_timestamp(timestamp: f64) -> Result<String, String> {
    let timestamp = timestamp as libc::time_t;
    let mut local = MaybeUninit::<libc::tm>::uninit();

    #[cfg(windows)]
    {
        // C 运行库按当前进程时区填充 tm，复现 Python time.localtime。
        let result = unsafe { libc::localtime_s(local.as_mut_ptr(), &timestamp) };
        if result != 0 {
            return Err(format!("cannot convert local timestamp: errno {result}"));
        }
    }

    #[cfg(unix)]
    {
        // localtime_r 使用调用方缓冲区，避免多线程下共享静态 tm。
        let result = unsafe { libc::localtime_r(&timestamp, local.as_mut_ptr()) };
        if result.is_null() {
            return Err("cannot convert local timestamp".to_string());
        }
    }

    #[cfg(not(any(windows, unix)))]
    {
        return Err("local timestamp formatting is unsupported on this platform".to_string());
    }

    // 上述平台分支成功后已初始化全部 tm 字段。
    let local = unsafe { local.assume_init() };
    Ok(format!(
        "{:04}-{:02}-{:02} {:02}:{:02}",
        local.tm_year + 1900,
        local.tm_mon + 1,
        local.tm_mday,
        local.tm_hour,
        local.tm_min
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn issues_output_preserves_summary_and_detail_layout() {
        let issues = json!([
            {
                "source": "semgrep",
                "severity": "ERROR",
                "rule_id": "python.eval",
                "rule_name": "eval use",
                "message": "avoid eval",
                "start_line": 2,
                "end_line": 2,
                "confidence": "HIGH",
                "snippet": "eval(x)",
                "fix": "use parser"
            },
            {
                "source": "guardrail",
                "severity": "warn",
                "rule_id": "guard.db",
                "rule_name": "db_safety",
                "message": "unsafe SQL",
                "status": "open",
                "start_line": 0,
                "end_line": 0
            }
        ]);
        let output = format_issues_output(&issues, "a.alpha", false).unwrap();
        assert!(output.contains("by severity: ERROR:1, WARN:1"));
        assert!(output.contains("[1] [semgrep] [ERROR] python.eval L2 conf=HIGH"));
        assert!(output.contains("[2] [guardrail] [WARN] guard.db [open]"));
    }

    #[test]
    fn empty_test_outputs_keep_python_hints() {
        assert!(format_test_cases_output(&json!([]), "a.missing")
            .unwrap()
            .contains("cw tests --build"));
        assert!(format_tested_functions_output(&json!([]), "a.test_missing")
            .unwrap()
            .contains("No tested functions"));
        assert!(
            format_test_stability_output(&empty_stability(), "a.missing")
                .unwrap()
                .contains("cw tests --import")
        );
    }

    #[test]
    fn test_history_orders_unstable_tests_first() {
        let stability = json!({
            "total_runs": 3,
            "pass_rate": 0.667,
            "avg_duration_ms": 12.3,
            "recent_failures": [],
            "by_test": {
                "test_stable": {"total": 1, "passed": 1, "failed": 0},
                "test_flaky": {"total": 2, "passed": 1, "failed": 1}
            }
        });
        let output = format_test_stability_output(&stability, "a.alpha").unwrap();
        assert!(output.find("test_flaky").unwrap() < output.find("test_stable").unwrap());
        assert!(output.contains("Pass rate:    66.7%"));
    }
}
