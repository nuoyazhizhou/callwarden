//! `cw symbol` 的兼容输出。

use serde_json::{Map, Value};

/// 按 Python `cw symbol` 的默认中文输出格式渲染完整符号详情。
pub fn format_symbol_output(value: &Value, requested_name: &str) -> Result<String, String> {
    if value.is_null() {
        return Ok(format!(
            "未找到符号: {requested_name}\n提示: 用 --search 搜索符号名称"
        ));
    }
    let detail = value
        .as_object()
        .ok_or_else(|| "symbol result must be a JSON object or null".to_string())?;
    let qualified_name = json_str(detail, "qualified_name")?;
    let kind = json_str(detail, "kind")?;
    let depth = json_i64(detail, "depth")?;
    let file_path = json_str(detail, "file_path")?;
    let start_line = json_i64(detail, "start_line")?;
    let end_line = json_i64(detail, "end_line")?;
    let signature = detail
        .get("signature")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .chars()
        .take(100)
        .collect::<String>();
    let has_comment = json_boolish(detail, "has_comment");

    let mut lines = vec![
        "符号详情".to_string(),
        format!("  名称: {qualified_name}"),
        format!("  类型: {kind}"),
        format!("  深度: {depth}"),
        format!("  文件: {file_path}:{start_line}-{end_line}"),
        if signature.is_empty() {
            "  签名: (无)".to_string()
        } else {
            format!("  签名: {signature}")
        },
        if has_comment {
            "  注释: 有".to_string()
        } else {
            "  注释: 无".to_string()
        },
    ];

    if let Some(comment) = detail.get("comment_content").and_then(Value::as_str) {
        if !comment.is_empty() {
            lines.push("  注释内容:".to_string());
            lines.extend(
                comment
                    .split('\n')
                    .take(10)
                    .map(|line| format!("    {line}")),
            );
        }
    }

    let calls_out = json_array(detail, "calls_out")?;
    lines.push(String::new());
    lines.push(format!("调用的函数（{} 个）:", calls_out.len()));
    if calls_out.is_empty() {
        lines.push("  (无)".to_string());
    } else {
        for call in calls_out.iter().take(20) {
            let call = call
                .as_object()
                .ok_or_else(|| "calls_out item must be a JSON object".to_string())?;
            let target = json_str(call, "target_name")?;
            let call_line = json_optional_i64(call, "call_line");
            let line_info = if call_line > 0 {
                format!(" (line {call_line})")
            } else {
                String::new()
            };
            lines.push(format!("  → {target}{line_info}"));
        }
        if calls_out.len() > 20 {
            lines.push(format!("  ... 还有 {} 个", calls_out.len() - 20));
        }
    }

    let called_by = json_array(detail, "called_by")?;
    lines.push(String::new());
    lines.push(format!("被谁调用（{} 个）:", called_by.len()));
    if called_by.is_empty() {
        lines.push("  (无)".to_string());
    } else {
        for call in called_by.iter().take(20) {
            let call = call
                .as_object()
                .ok_or_else(|| "called_by item must be a JSON object".to_string())?;
            let caller = json_str(call, "caller_name")?;
            let call_line = json_optional_i64(call, "call_line");
            let line_info = if call_line > 0 {
                format!(" (line {call_line})")
            } else {
                String::new()
            };
            lines.push(format!("  ← {caller}{line_info}"));
        }
        if called_by.len() > 20 {
            lines.push(format!("  ... 还有 {} 个", called_by.len() - 20));
        }
    }

    let issues = detail
        .get("issues")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let issues_total = json_optional_i64(detail, "issues_total").max(0) as usize;
    if !issues.is_empty() {
        lines.push(String::new());
        lines.push(format!(
            "Issues ({} of {issues_total}, use 'cw issues {requested_name}' for full):",
            issues.len()
        ));
        for issue in issues {
            let issue = issue
                .as_object()
                .ok_or_else(|| "issue item must be a JSON object".to_string())?;
            let source = optional_str(issue, "source", "?");
            let severity = optional_str(issue, "severity", "?").to_uppercase();
            let rule_id = optional_str(issue, "rule_id", "?");
            let message = optional_str(issue, "message", "");
            let start_line = json_optional_i64(issue, "start_line");
            let line_info = if start_line > 0 {
                format!(" L{start_line}")
            } else {
                String::new()
            };
            lines.push(format!(
                "  [{source}] [{severity}] {rule_id}{line_info}: {message}"
            ));
        }
    } else if issues_total > 0 {
        lines.push(String::new());
        lines.push(format!(
            "Issues: {issues_total} total (filtered, use 'cw issues {requested_name} --include-info')"
        ));
    }
    Ok(lines.join("\n"))
}

fn json_str<'a>(object: &'a Map<String, Value>, key: &str) -> Result<&'a str, String> {
    object
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("symbol result field {key:?} must be a string"))
}

fn optional_str<'a>(object: &'a Map<String, Value>, key: &str, default: &'a str) -> &'a str {
    object.get(key).and_then(Value::as_str).unwrap_or(default)
}

fn json_i64(object: &Map<String, Value>, key: &str) -> Result<i64, String> {
    object
        .get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("symbol result field {key:?} must be an integer"))
}

fn json_optional_i64(object: &Map<String, Value>, key: &str) -> i64 {
    object.get(key).and_then(Value::as_i64).unwrap_or_default()
}

fn json_boolish(object: &Map<String, Value>, key: &str) -> bool {
    match object.get(key) {
        Some(Value::Bool(value)) => *value,
        Some(Value::Number(value)) => value.as_i64().unwrap_or_default() != 0,
        _ => false,
    }
}

fn json_array<'a>(object: &'a Map<String, Value>, key: &str) -> Result<&'a Vec<Value>, String> {
    object
        .get(key)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("symbol result field {key:?} must be an array"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn formats_not_found_like_python() {
        assert_eq!(
            format_symbol_output(&Value::Null, "a.missing").unwrap(),
            "未找到符号: a.missing\n提示: 用 --search 搜索符号名称"
        );
    }

    #[test]
    fn formats_full_detail_calls_comments_and_issues() {
        let detail = json!({
            "qualified_name": "a.alpha",
            "kind": "fn",
            "depth": 0,
            "file_path": "a.py",
            "start_line": 1,
            "end_line": 5,
            "signature": "alpha()",
            "has_comment": true,
            "comment_content": "first\nsecond",
            "calls_out": [{"target_name": "a.beta", "call_line": 3}],
            "called_by": [{"caller_name": "a.beta", "call_line": 8}],
            "issues": [{
                "source": "semgrep",
                "severity": "ERROR",
                "rule_id": "python.eval",
                "message": "avoid eval",
                "start_line": 2
            }],
            "issues_total": 1
        });
        let output = format_symbol_output(&detail, "a.alpha").unwrap();
        assert!(output.contains("  名称: a.alpha"));
        assert!(output.contains("    first\n    second"));
        assert!(output.contains("  → a.beta (line 3)"));
        assert!(output.contains("  ← a.beta (line 8)"));
        assert!(output.contains("[semgrep] [ERROR] python.eval L2: avoid eval"));
    }
}
