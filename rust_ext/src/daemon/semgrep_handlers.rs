//! CLI-061（T-1787322798303-8bd1779c）：`cw semgrep` 专用 handler。
//!
//! 复刻 Python `analyzers/issues.py` 的 run_semgrep / run_semgrep_and_save /
//! scan_semgrep_incremental / get_semgrep_summary 语义，使 CLI 成为 HTTP thin
//! client、Rust daemon 成为唯一 authority：
//! - run_semgrep：执行 semgrep CLI 并返回结构化 findings（不落库）；
//! - run_semgrep_and_save：执行并落库 semgrep_scans/semgrep_findings；
//! - scan_semgrep_incremental：git diff 变更文件 + 扫描 + 清理 stale；
//! - get_semgrep_summary：聚合 by_severity / by_language / top_rules。

use rusqlite::{Connection, OptionalExtension};
use serde_json::{json, Map, Value};

use super::dispatch::{
    get_int_param_or, get_str_param_or, require_str_param, DaemonRpcError,
};

/// 解析 bool 参数（与 edit_handlers 保持一致）。
fn get_bool_param_or(params: &Value, key: &str, default: bool) -> bool {
    match params.get(key) {
        Some(v) if v.is_boolean() => v.as_bool().unwrap_or(default),
        Some(v) if v.is_string() => match v.as_str() {
            Some("true") | Some("1") => true,
            _ => default,
        },
        _ => default,
    }
}

fn now_ts() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn workspace_root(conn: &Connection, workspace_id: i64) -> Result<std::path::PathBuf, DaemonRpcError> {
    conn.query_row(
        "SELECT root_path FROM workspaces WHERE id = ?1",
        rusqlite::params![workspace_id],
        |row| row.get::<_, String>(0),
    )
    .map(std::path::PathBuf::from)
    .map_err(|e| DaemonRpcError::internal_error(format!("查询 workspace root 失败: {e}")))
}

/// 从 semgrep JSON results 条目提取结构化 finding（与 Python run_semgrep 一致）。
fn finding_from_item(item: &Value, path: &str) -> Value {
    let check_id = item.get("check_id").and_then(Value::as_str).unwrap_or("");
    let extra = item.get("extra").unwrap_or(&Value::Null);
    let start = item.get("start").unwrap_or(&Value::Null);
    let end = item.get("end").unwrap_or(&Value::Null);
    let language = detect_language_from_path(path);
    json!({
        "rule_id": check_id,
        "rule_name": check_id.rsplit('.').next().unwrap_or(""),
        "message": extra.get("message").and_then(Value::as_str).unwrap_or(""),
        "severity": extra.get("severity").and_then(Value::as_str).unwrap_or("INFO"),
        "confidence": extra.get("confidence").and_then(Value::as_str).unwrap_or("UNKNOWN"),
        "path": path,
        "start_line": start.get("line").and_then(Value::as_i64).unwrap_or(0),
        "end_line": end.get("line").and_then(Value::as_i64).unwrap_or(0),
        "snippet": extra.get("lines").and_then(Value::as_str).unwrap_or(""),
        "language": language,
        "fix": extra.get("fix").and_then(Value::as_str).unwrap_or(""),
        "references": extra.get("references").cloned().unwrap_or(Value::Array(vec![])),
    })
}

/// 从路径推断语言（与 Python _detect_language_from_path 等价）。
fn detect_language_from_path(path: &str) -> String {
    let ext = std::path::Path::new(path)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_lowercase();
    match ext.as_str() {
        "rs" => "rust".to_string(),
        "ts" | "tsx" => "typescript".to_string(),
        "js" | "jsx" => "javascript".to_string(),
        "py" => "python".to_string(),
        "kt" | "kts" => "kotlin".to_string(),
        "go" => "go".to_string(),
        "java" => "java".to_string(),
        "c" | "h" => "c".to_string(),
        "cpp" | "hpp" | "cc" | "hh" => "cpp".to_string(),
        _ => "".to_string(),
    }
}

/// 查找 semgrep 可执行文件（PATH 探测）。
fn find_semgrep_cli() -> Option<String> {
    let candidates = ["semgrep", "semgrep.exe"];
    if let Some(paths) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&paths) {
            for name in candidates {
                let full = dir.join(name);
                if full.is_file() {
                    return Some(full.to_string_lossy().to_string());
                }
            }
        }
    }
    None
}

/// 语言 → 文件扩展名映射（与 Python ext_map 一致）。
fn language_exts(lang: &str) -> Vec<String> {
    match lang.to_lowercase().as_str() {
        "rust" => vec!["*.rs".to_string()],
        "typescript" => vec!["*.ts".to_string(), "*.tsx".to_string()],
        "javascript" => vec!["*.js".to_string(), "*.jsx".to_string()],
        "python" => vec!["*.py".to_string()],
        "kotlin" => vec!["*.kt".to_string(), "*.kts".to_string()],
        "go" => vec!["*.go".to_string()],
        "java" => vec!["*.java".to_string()],
        "c" => vec!["*.c".to_string(), "*.h".to_string()],
        "cpp" => vec!["*.cpp".to_string(), "*.hpp".to_string(), "*.cc".to_string(), "*.hh".to_string()],
        _ => vec![],
    }
}

/// 执行 semgrep CLI，返回解析后的 JSON 输出。
fn run_semgrep_cli(
    root: &std::path::Path,
    config: &str,
    languages: &[Value],
    target_paths: &[String],
    timeout: i64,
) -> Result<Value, String> {
    let semgrep = find_semgrep_cli().ok_or_else(|| {
        "Semgrep CLI not found. Please run: pip install semgrep".to_string()
    })?;
    let mut cmd = std::process::Command::new(&semgrep);
    cmd.arg("--config")
        .arg(config)
        .arg("--json")
        .arg("--quiet")
        .current_dir(root);
    for lang in languages {
        if let Some(l) = lang.as_str() {
            for ext in language_exts(l) {
                cmd.arg("--include").arg(ext);
            }
        }
    }
    for tp in target_paths {
        cmd.arg(tp);
    }
    // semgrep 可能不存在于子进程 PATH，注入当前 PATH
    if let Ok(path) = std::env::var("PATH") {
        cmd.env("PATH", path);
    }
    let output = cmd
        .output()
        .map_err(|e| format!("semgrep 执行失败: {e}"))?;
    if !output.status.success() && output.stdout.is_empty() {
        return Err(format!(
            "Semgrep 返回码 {}: {}",
            output.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&output.stderr).chars().take(500).collect::<String>()
        ));
    }
    serde_json::from_slice(&output.stdout)
        .map_err(|e| format!("semgrep JSON 解析失败: {e}"))
}

/// `run_semgrep` —— 执行 semgrep 扫描并返回结构化 findings（不落库）。
pub fn handle_run_semgrep(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let root = workspace_root(conn, workspace_id)?;
    let config = get_str_param_or(params, "config", "p/default");
    let languages = params.get("languages").and_then(Value::as_array).cloned().unwrap_or_default();
    let timeout = get_int_param_or(params, "timeout", 300);
    let target_paths: Vec<String> = match params.get("target_paths") {
        Some(v) if v.is_array() => v
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|x| x.as_str().map(String::from))
            .collect(),
        Some(v) if v.is_string() => vec![v.as_str().unwrap_or("").to_string()],
        _ => vec![".".to_string()],
    };

    let data = match run_semgrep_cli(&root, &config, &languages, &target_paths, timeout) {
        Ok(d) => d,
        Err(e) => {
            return Ok(json!({
                "success": false,
                "error": e,
                "results": [],
                "total_findings": 0,
                "severity_counts": {},
            }))
        }
    };

    let mut findings: Vec<Value> = Vec::new();
    if let Some(results) = data.get("results").and_then(Value::as_array) {
        for item in results {
            let path = item.get("path").and_then(Value::as_str).unwrap_or("");
            findings.push(finding_from_item(item, path));
        }
    }
    let mut severity_counts = Map::new();
    for f in &findings {
        let sev = f.get("severity").and_then(Value::as_str).unwrap_or("INFO").to_string();
        let count = severity_counts.get(&sev).and_then(Value::as_i64).unwrap_or(0) + 1;
        severity_counts.insert(sev, Value::Number(serde_json::Number::from(count)));
    }
    let paths_scanned = data
        .get("paths")
        .and_then(|p| p.get("scanned"))
        .cloned()
        .unwrap_or(Value::Array(vec![]));
    let errors = data.get("errors").cloned().unwrap_or(Value::Array(vec![]));

    Ok(json!({
        "success": true,
        "total_findings": findings.len(),
        "severity_counts": Value::Object(severity_counts),
        "results": findings,
        "paths_scanned": paths_scanned,
        "errors": errors,
    }))
}

/// `run_semgrep_and_save` —— 执行 semgrep 扫描并落库。
pub fn handle_run_semgrep_and_save(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    // 复用 run_semgrep 的执行逻辑
    let run = handle_run_semgrep(conn, workspace_id, params)?;
    if run.get("success").and_then(Value::as_bool).unwrap_or(false) {
        let findings = run.get("results").and_then(Value::as_array).cloned().unwrap_or_default();
        let saved = save_findings(conn, workspace_id, findings, "full", "p/default")?;
        return Ok(json!({
            "success": true,
            "total_findings": run.get("total_findings").cloned().unwrap_or(Value::Null),
            "saved_findings": saved,
        }));
    }
    Ok(run)
}

/// 落库 findings 到 semgrep_scans + semgrep_findings（与 Python save_semgrep_findings 对齐）。
fn save_findings(
    conn: &Connection,
    workspace_id: i64,
    findings: Vec<Value>,
    scan_type: &str,
    config: &str,
) -> Result<i64, DaemonRpcError> {
    let scan_id = conn
        .execute(
            "INSERT INTO semgrep_scans (scan_type, config, workspace_id, started_at, status)
             VALUES (?1, ?2, ?3, ?4, 'completed')",
            rusqlite::params![scan_type, config, workspace_id, now_ts()],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("semgrep_scans insert: {e}")))?;
    let scan_id = conn.last_insert_rowid();
    let mut inserted = 0i64;
    for f in &findings {
        let path = f.get("path").and_then(Value::as_str).unwrap_or("");
        let rel = path.trim_start_matches('/');
        let file_instance_id: Option<i64> = conn
            .query_row(
                "SELECT id FROM file_instances WHERE workspace_id = ?1 AND rel_path = ?2 LIMIT 1",
                rusqlite::params![workspace_id, rel],
                |row| row.get(0),
            )
            .ok();
        let file_instance_id = file_instance_id.unwrap_or(0);
        let rule_id = f.get("rule_id").and_then(Value::as_str).unwrap_or("unknown");
        let message = f.get("message").and_then(Value::as_str).unwrap_or("");
        let severity = f.get("severity").and_then(Value::as_str).unwrap_or("INFO").to_uppercase();
        let start_line = f.get("start_line").and_then(Value::as_i64).unwrap_or(0);
        let end_line = f.get("end_line").and_then(Value::as_i64).unwrap_or(start_line);
        let language = f.get("language").and_then(Value::as_str).unwrap_or("");
        let content_hash = crate::daemon::fs_handlers::sha256_hex(
            std::fs::read(std::path::Path::new(path)).unwrap_or_default().as_slice(),
        );
        let res = conn.execute(
            "INSERT OR IGNORE INTO semgrep_findings
               (file_instance_id, content_hash, rule_id, rule_name, message, severity, confidence,
                language, start_line, end_line, snippet, symbol_id, symbol_qualified, scanned_at, scan_id)
             VALUES (?1, ?2, ?3, '', ?4, ?5, 'UNKNOWN', ?6, ?7, ?8, '', 0, '', ?9, ?10)",
            rusqlite::params![
                file_instance_id,
                content_hash,
                rule_id,
                message,
                severity,
                language,
                start_line,
                end_line,
                now_ts(),
                scan_id
            ],
        );
        if res.is_ok() && res.unwrap() > 0 {
            inserted += 1;
        }
    }
    conn.execute(
        "UPDATE semgrep_scans SET completed_at = ?1, total_findings = ?2 WHERE id = ?3",
        rusqlite::params![now_ts(), inserted, scan_id],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("semgrep_scans update: {e}")))?;
    Ok(inserted)
}

/// `scan_semgrep_incremental` —— 增量扫描：git diff 变更文件 + 扫描 + 清理 stale。
pub fn handle_scan_semgrep_incremental(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let root = workspace_root(conn, workspace_id)?;
    let base_branch = get_str_param_or(params, "base_branch", "main");
    let head = get_str_param_or(params, "head", "HEAD");
    let config = get_str_param_or(params, "config", "p/default");
    let languages = params.get("languages").and_then(Value::as_array).cloned().unwrap_or_default();
    let timeout = get_int_param_or(params, "timeout", 300);

    // git diff --name-only base...head 取变更文件
    let changed = git_diff_changed(&root, &base_branch, &head);
    let changed_files = changed.len() as i64;
    if changed.is_empty() {
        return Ok(json!({
            "success": true,
            "changed_files": 0,
            "scanned_files": 0,
            "saved_findings": 0,
            "total_findings": 0,
            "stale_file_ids": [],
        }));
    }
    let data = match run_semgrep_cli(&root, &config, &languages, &changed, timeout) {
        Ok(d) => d,
        Err(e) => {
            return Ok(json!({
                "success": false,
                "error": e,
                "changed_files": changed_files,
                "scanned_files": 0,
                "saved_findings": 0,
                "total_findings": 0,
                "stale_file_ids": [],
            }))
        }
    };
    let mut findings: Vec<Value> = Vec::new();
    if let Some(results) = data.get("results").and_then(Value::as_array) {
        for item in results {
            let path = item.get("path").and_then(Value::as_str).unwrap_or("");
            findings.push(finding_from_item(item, path));
        }
    }
    // 清理 stale：变更文件的旧 findings 先删
    let mut stale_file_ids: Vec<i64> = Vec::new();
    for rel in &changed {
        if let Some(fid) = conn
            .query_row(
                "SELECT id FROM file_instances WHERE workspace_id = ?1 AND rel_path = ?2 LIMIT 1",
                rusqlite::params![workspace_id, rel],
                |row| row.get::<_, i64>(0),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("stale fid query: {e}")))?
        {
            stale_file_ids.push(fid);
        }
    }
    if !stale_file_ids.is_empty() {
        let placeholders = stale_file_ids.iter().map(|_| "?").collect::<Vec<_>>().join(",");
        let sql = format!(
            "DELETE FROM semgrep_findings WHERE file_instance_id IN ({placeholders})"
        );
        let binds: Vec<rusqlite::types::Value> =
            stale_file_ids.iter().map(|x| rusqlite::types::Value::Integer(*x)).collect();
        conn.execute(&sql, rusqlite::params_from_iter(binds.iter()))
            .map_err(|e| DaemonRpcError::internal_error(format!("stale cleanup: {e}")))?;
    }
    let saved = save_findings(conn, workspace_id, findings.clone(), "incremental", &config)?;
    Ok(json!({
        "success": true,
        "changed_files": changed_files,
        "scanned_files": changed.len(),
        "saved_findings": saved,
        "total_findings": findings.len(),
        "stale_file_ids": stale_file_ids,
    }))
}

/// git diff --name-only base...head。
fn git_diff_changed(root: &std::path::Path, base: &str, head: &str) -> Vec<String> {
    let mut cmd = std::process::Command::new("git");
    cmd.arg("diff").arg("--name-only").arg(format!("{base}...{head}")).current_dir(root);
    let output = match cmd.output() {
        Ok(o) => o,
        Err(_) => return vec![],
    };
    if !output.status.success() {
        return vec![];
    }
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(|l| l.trim().to_string())
        .filter(|l| !l.is_empty())
        .collect()
}

/// `get_semgrep_summary` —— 执行 semgrep 快速扫描并聚合统计。
pub fn handle_get_semgrep_summary(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    // 复用 run_semgrep 执行
    let run = handle_run_semgrep(conn, workspace_id, params)?;
    if !run.get("success").and_then(Value::as_bool).unwrap_or(false) {
        return Ok(run);
    }
    let findings = run.get("results").and_then(Value::as_array).cloned().unwrap_or_default();
    let mut by_severity = Map::new();
    let mut by_language = Map::new();
    let mut by_rule: std::collections::HashMap<String, (i64, String, String)> =
        std::collections::HashMap::new();
    for f in &findings {
        let sev = f.get("severity").and_then(Value::as_str).unwrap_or("INFO").to_string();
        let lang = f.get("language").and_then(Value::as_str).unwrap_or("").to_string();
        let rule = f.get("rule_id").and_then(Value::as_str).unwrap_or("").to_string();
        let msg = f.get("message").and_then(Value::as_str).unwrap_or("").to_string();
        let count = by_severity.get(&sev).and_then(Value::as_i64).unwrap_or(0) + 1;
        by_severity.insert(sev.clone(), Value::Number(serde_json::Number::from(count)));
        if !lang.is_empty() {
            let lc = by_language.get(&lang).and_then(Value::as_i64).unwrap_or(0) + 1;
            by_language.insert(lang, Value::Number(serde_json::Number::from(lc)));
        }
        let entry = by_rule.entry(rule.clone()).or_insert_with(|| (0, msg, sev.clone()));
        entry.0 += 1;
    }
    let mut top_rules: Vec<(String, i64, String, String)> = by_rule
        .into_iter()
        .map(|(rule, (count, msg, sev))| (rule, count, msg, sev))
        .collect();
    top_rules.sort_by(|a, b| b.1.cmp(&a.1));
    // CLI 期望 [(rule_id, {severity,count,message}), ...]（Python sorted(items) 元组对），
    // JSON 表示为首元素 rule_id、次元素 stats dict 的嵌套数组。
    let top_rules_json: Vec<Value> = top_rules
        .iter()
        .take(10)
        .map(|(rule, count, msg, sev)| {
            json!([rule, {"severity": sev, "count": count, "message": msg}])
        })
        .collect();
    Ok(json!({
        "success": true,
        "total_findings": findings.len(),
        "by_severity": Value::Object(by_severity),
        "by_language": Value::Object(by_language),
        "top_rules": top_rules_json,
        "errors": run.get("errors").cloned().unwrap_or(Value::Array(vec![])),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detect_language_from_path_extensions() {
        assert_eq!(detect_language_from_path("a.rs"), "rust");
        assert_eq!(detect_language_from_path("a.ts"), "typescript");
        assert_eq!(detect_language_from_path("a.py"), "python");
        assert_eq!(detect_language_from_path("a.java"), "java");
        assert_eq!(detect_language_from_path("a.cpp"), "cpp");
        assert_eq!(detect_language_from_path("noext"), "");
    }

    #[test]
    fn finding_from_item_maps_fields() {
        let item = json!({
            "check_id": "rules.no-eval",
            "path": "a.py",
            "start": {"line": 3},
            "end": {"line": 5},
            "extra": {
                "message": "avoid eval",
                "severity": "ERROR",
                "confidence": "HIGH",
                "lines": "eval(x)",
                "fix": "use literal"
            }
        });
        let f = finding_from_item(&item, "a.py");
        assert_eq!(f["rule_id"], "rules.no-eval");
        assert_eq!(f["rule_name"], "no-eval");
        assert_eq!(f["message"], "avoid eval");
        assert_eq!(f["severity"], "ERROR");
        assert_eq!(f["language"], "python");
        assert_eq!(f["start_line"], 3);
        assert_eq!(f["fix"], "use literal");
    }

    #[test]
    fn language_exts_map() {
        assert_eq!(language_exts("rust"), vec!["*.rs"]);
        assert_eq!(language_exts("typescript"), vec!["*.ts", "*.tsx"]);
        assert!(language_exts("unknown").is_empty());
    }

    #[test]
    fn get_bool_param_or_variants() {
        assert_eq!(get_bool_param_or(&json!({"dry_run": true}), "dry_run", false), true);
        assert_eq!(get_bool_param_or(&json!({"dry_run": "true"}), "dry_run", false), true);
        assert_eq!(get_bool_param_or(&json!({"dry_run": 1}), "dry_run", false), false);
        assert_eq!(get_bool_param_or(&json!({}), "dry_run", true), true);
    }
}
