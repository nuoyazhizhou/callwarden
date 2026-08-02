//! 外部集成与运维命令 (Phase 5 P0-CLI-E5)
//!
//! 实现：
//! - `semgrep`: scan / list / stats
//! - `coverage`: import / fn / uncovered
//! - `git`: import / log / show / stats
//! - `install-agent`: 生成 AI Agent 集成配置文件
//! - `install-hook`: 安装 / 卸载 Git post-commit hook
//! - `gc`: archive / restore / status / purge / db-cleanup
//! - `doctor`: 环境诊断 / Windows Defender 排除设置

use quick_xml::events::Event;
use quick_xml::Reader;
use regex::Regex;
use rusqlite::params;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::sync::OnceLock;
use std::thread;
use std::time::{Duration, Instant};
use std::time::{SystemTime, UNIX_EPOCH};

use super::impact::query_local_impact;
use super::runtime::{CommandResult, RouteUsed, RuntimeOptions};
use super::security::bootstrap_status;
use super::stats::query_local_stats;
use super::status::{load_ignore_patterns, should_ignore};
use crate::clone_detection::detect_clones_core;
use crate::graph::GraphStore;

/// 执行外部工具并提供硬超时，避免 Semgrep/Git 进程在 CLI 中永久占用资源。
/// stdout/stderr 由独立线程消费，避免大输出填满 pipe 后造成子进程死锁。
fn run_command_with_timeout(mut command: Command, timeout: Duration) -> Result<Output, String> {
    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("failed to spawn external command: {error}"))?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "external command stdout pipe unavailable".to_string())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "external command stderr pipe unavailable".to_string())?;
    let stdout_reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = std::io::BufReader::new(stdout).read_to_end(&mut bytes);
        bytes
    });
    let stderr_reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = std::io::BufReader::new(stderr).read_to_end(&mut bytes);
        bytes
    });

    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(25)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = stdout_reader.join();
                let _ = stderr_reader.join();
                return Err(format!(
                    "external command exceeded timeout of {} seconds",
                    timeout.as_secs()
                ));
            }
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = stdout_reader.join();
                let _ = stderr_reader.join();
                return Err(format!(
                    "failed while waiting for external command: {error}"
                ));
            }
        }
    };

    let stdout = stdout_reader
        .join()
        .map_err(|_| "external command stdout reader panicked".to_string())?;
    let stderr = stderr_reader
        .join()
        .map_err(|_| "external command stderr reader panicked".to_string())?;
    Ok(Output {
        status,
        stdout,
        stderr,
    })
}

fn external_timeout(runtime: &RuntimeOptions, requested_secs: u64) -> Duration {
    let requested = if requested_secs == 0 {
        runtime.timeout.as_secs().max(1)
    } else {
        requested_secs
    };
    Duration::from_secs(requested.min(3600))
}

// ===== 1. Semgrep 子命令 =====

pub fn run_semgrep_scan(
    runtime: &RuntimeOptions,
    paths: &[PathBuf],
    config: &str,
    languages: &[String],
    timeout_secs: u64,
    save: bool,
) -> CommandResult {
    let semgrep_bin =
        std::env::var("CALLWARDEN_SEMGREP_BIN").unwrap_or_else(|_| "semgrep".to_string());

    let mut cmd = Command::new(&semgrep_bin);
    cmd.arg("scan").arg("--json");
    if !config.is_empty() {
        cmd.arg("--config").arg(config);
    }
    for lang in languages {
        cmd.arg("--lang").arg(lang);
    }
    if paths.is_empty() {
        cmd.arg(".");
    } else {
        for p in paths {
            cmd.arg(p);
        }
    }

    let output = match run_command_with_timeout(cmd, external_timeout(runtime, timeout_secs)) {
        Ok(out) => out,
        Err(err) => {
            return CommandResult::failure(
                1,
                format!("semgrep failed: {err}. Make sure semgrep is installed."),
                RouteUsed::Local,
            );
        }
    };

    let stdout_str = String::from_utf8_lossy(&output.stdout);
    let parsed_json: Value = match serde_json::from_str(&stdout_str) {
        Ok(val) => val,
        Err(_) => {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return CommandResult::failure(
                1,
                format!(
                    "semgrep returned invalid JSON (exit={}): {}",
                    output.status,
                    stderr.trim()
                ),
                RouteUsed::Local,
            );
        }
    };

    // Semgrep returns 1 when findings exist. Other non-zero exits are tool
    // failures even if a diagnostic JSON document happens to be emitted.
    if !output.status.success() && output.status.code() != Some(1) {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return CommandResult::failure(
            1,
            format!("semgrep exited with {}: {}", output.status, stderr.trim()),
            RouteUsed::Local,
        );
    }

    let results = parsed_json.get("results").and_then(|r| r.as_array());
    let findings_count = results.map(|r| r.len()).unwrap_or(0);

    if save {
        let conn = match runtime.open_local_write_db() {
            Ok(c) => c,
            Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
        };
        let ws_id = match runtime.resolve_local_workspace_id(&conn) {
            Ok(id) => id,
            Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
        };

        if let Err(error) = conn.execute_batch("BEGIN IMMEDIATE") {
            return CommandResult::failure(
                1,
                format!("cannot begin Semgrep import: {error}"),
                RouteUsed::Local,
            );
        }

        if let Some(res_list) = results {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64();
            for item in res_list {
                let rule_id = item
                    .get("check_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                let path = item.get("path").and_then(|v| v.as_str()).unwrap_or("");
                let message = item
                    .get("extra")
                    .and_then(|e| e.get("message"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let severity = item
                    .get("extra")
                    .and_then(|e| e.get("severity"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("WARNING");
                let start_line = item
                    .get("start")
                    .and_then(|s| s.get("line"))
                    .and_then(|v| v.as_i64())
                    .unwrap_or(1);
                let end_line = item
                    .get("end")
                    .and_then(|e| e.get("line"))
                    .and_then(|v| v.as_i64())
                    .unwrap_or(1);

                let normalized_path = path.replace('\\', "/").trim_start_matches("./").to_string();
                let file_row = conn.query_row(
                    "SELECT id, current_content_hash FROM file_instances
                     WHERE workspace_id = ?1 AND
                       (replace(rel_path, '\\', '/') = ?2 OR abs_path = ?3)
                     LIMIT 1",
                    params![ws_id, normalized_path, path],
                    |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)),
                );
                let (file_instance_id, content_hash) = match file_row {
                    Ok(row) => row,
                    Err(error) => {
                        let _ = conn.execute_batch("ROLLBACK");
                        return CommandResult::failure(
                            1,
                            format!("Semgrep finding path {path:?} is not indexed: {error}"),
                            RouteUsed::Local,
                        );
                    }
                };

                if let Err(error) = conn.execute(
                    "INSERT INTO semgrep_findings (
                        file_instance_id, content_hash, rule_id, rule_name, message,
                        severity, confidence, language, start_line, end_line, scanned_at
                    ) VALUES (
                        ?1, ?2, ?3, ?3, ?4, ?5, 'HIGH', '', ?6, ?7, ?8
                    )",
                    params![
                        file_instance_id,
                        content_hash,
                        rule_id,
                        message,
                        severity,
                        start_line,
                        end_line,
                        now
                    ],
                ) {
                    let _ = conn.execute_batch("ROLLBACK");
                    return CommandResult::failure(
                        1,
                        format!("failed to save Semgrep finding for {path:?}: {error}"),
                        RouteUsed::Local,
                    );
                }
            }
        }
        if let Err(error) = conn.execute_batch("COMMIT") {
            let _ = conn.execute_batch("ROLLBACK");
            return CommandResult::failure(
                1,
                format!("Semgrep import commit failed: {error}"),
                RouteUsed::Local,
            );
        }
    }

    CommandResult::success_json(
        &json!({
            "scanned": true,
            "findings_count": findings_count,
            "saved_to_db": save,
            "raw_results": parsed_json
        }),
        RouteUsed::Local,
    )
}

pub fn run_semgrep_list(runtime: &RuntimeOptions, limit: usize) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(c) => c,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };
    let ws_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };

    let sql = "
        SELECT sf.id, sf.rule_id, sf.severity, sf.message, sf.start_line, sf.end_line, fi.rel_path
        FROM semgrep_findings sf
        LEFT JOIN file_instances fi ON sf.file_instance_id = fi.id
        WHERE fi.workspace_id = ?1 OR sf.file_instance_id IS NULL
        ORDER BY sf.id DESC LIMIT ?2";

    let mut stmt = match conn.prepare(sql) {
        Ok(s) => s,
        Err(e) => {
            return CommandResult::failure(
                1,
                format!("query semgrep_findings error: {e}"),
                RouteUsed::Local,
            )
        }
    };

    let rows = stmt.query_map(params![ws_id, limit as i64], |row| {
        Ok(json!({
            "id": row.get::<_, i64>(0)?,
            "rule_id": row.get::<_, Option<String>>(1)?.unwrap_or_default(),
            "severity": row.get::<_, Option<String>>(2)?.unwrap_or_default(),
            "message": row.get::<_, Option<String>>(3)?.unwrap_or_default(),
            "start_line": row.get::<_, Option<i64>>(4)?.unwrap_or(1),
            "end_line": row.get::<_, Option<i64>>(5)?.unwrap_or(1),
            "file_path": row.get::<_, Option<String>>(6)?.unwrap_or_default(),
        }))
    });

    let list: Vec<Value> = match rows {
        Ok(r) => r.filter_map(|x| x.ok()).collect(),
        Err(_) => vec![],
    };

    CommandResult::success_json(
        &json!({ "total": list.len(), "findings": list }),
        RouteUsed::Local,
    )
}

pub fn run_semgrep_stats(runtime: &RuntimeOptions) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(c) => c,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };
    let ws_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };

    let sql = "
        SELECT severity, COUNT(*)
        FROM semgrep_findings sf
        LEFT JOIN file_instances fi ON sf.file_instance_id = fi.id
        WHERE fi.workspace_id = ?1 OR sf.file_instance_id IS NULL
        GROUP BY severity";

    let mut stmt = match conn.prepare(sql) {
        Ok(s) => s,
        Err(_) => {
            return CommandResult::success_json(&json!({"by_severity": {}}), RouteUsed::Local)
        }
    };

    let rows = stmt.query_map(params![ws_id], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    });

    let mut by_severity = serde_json::Map::new();
    if let Ok(r) = rows {
        for item in r.flatten() {
            by_severity.insert(item.0, json!(item.1));
        }
    }

    CommandResult::success_json(&json!({ "by_severity": by_severity }), RouteUsed::Local)
}

// ===== 2. Coverage 子命令 =====

fn insert_coverage_lines(
    conn: &rusqlite::Connection,
    workspace_id: i64,
    report_path: &str,
    lines: &[(i64, i64)],
    report_source: &str,
    imported_at: f64,
) -> Result<(usize, usize), String> {
    let normalized = report_path.replace('\\', "/");
    let file_instance_id: i64 = conn
        .query_row(
            "SELECT id FROM file_instances
             WHERE workspace_id = ?1
               AND replace(rel_path, '\\\\', '/') = ?2
             LIMIT 1",
            params![workspace_id, normalized],
            |row| row.get(0),
        )
        .map_err(|error| format!("coverage file {report_path:?} is not indexed: {error}"))?;

    let mut inserted = 0;
    let mut matched_symbols = 0;
    for (line_no, hit_count) in lines {
        let symbol_id: Option<i64> = conn
            .query_row(
                "SELECT id FROM symbols
                 WHERE file_instance_id = ?1 AND start_line <= ?2 AND end_line >= ?2
                 ORDER BY (end_line - start_line) ASC, id ASC LIMIT 1",
                params![file_instance_id, line_no],
                |row| row.get(0),
            )
            .ok();
        if symbol_id.is_some() {
            matched_symbols += 1;
        }
        conn.execute(
            "INSERT INTO coverage_data
                (file_instance_id, symbol_id, line_start, line_end, hit_count, report_source, imported_at)
             VALUES (?1, ?2, ?3, ?3, ?4, ?5, ?6)",
            params![file_instance_id, symbol_id, line_no, hit_count, report_source, imported_at],
        )
        .map_err(|error| format!("failed to insert coverage line {report_path}:{line_no}: {error}"))?;
        inserted += 1;
    }
    Ok((inserted, matched_symbols))
}

fn parse_cobertura(
    content: &str,
    conn: &rusqlite::Connection,
    workspace_id: i64,
    imported_at: f64,
) -> Result<(usize, usize, usize, usize), String> {
    let mut reader = Reader::from_str(content);
    reader.config_mut().trim_text(true);
    let mut current_file: Option<String> = None;
    let mut current_lines: Vec<(i64, i64)> = Vec::new();
    let mut files_total = 0;
    let mut files_matched = 0;
    let mut lines_imported = 0;
    let mut symbols_matched = 0;

    loop {
        match reader.read_event() {
            Ok(Event::Start(event)) if event.name().as_ref() == b"class" => {
                current_file = event
                    .attributes()
                    .flatten()
                    .find(|attr| attr.key.as_ref() == b"filename")
                    .and_then(|attr| attr.unescape_value().ok().map(|value| value.into_owned()));
                current_lines.clear();
            }
            Ok(Event::Empty(event)) if event.name().as_ref() == b"line" => {
                let mut number = None;
                let mut hits = None;
                for attr in event.attributes().flatten() {
                    if attr.key.as_ref() == b"number" {
                        number = attr
                            .unescape_value()
                            .ok()
                            .and_then(|v| v.parse::<i64>().ok());
                    } else if attr.key.as_ref() == b"hits" {
                        hits = attr
                            .unescape_value()
                            .ok()
                            .and_then(|v| v.parse::<i64>().ok());
                    }
                }
                if let (Some(number), Some(hits)) = (number, hits) {
                    current_lines.push((number, hits));
                }
            }
            Ok(Event::End(event)) if event.name().as_ref() == b"class" => {
                if let Some(path) = current_file.take() {
                    files_total += 1;
                    match insert_coverage_lines(
                        conn,
                        workspace_id,
                        &path,
                        &current_lines,
                        "cobertura",
                        imported_at,
                    ) {
                        Ok((inserted, matched)) => {
                            files_matched += 1;
                            lines_imported += inserted;
                            symbols_matched += matched;
                        }
                        Err(_) => {}
                    }
                }
                current_lines.clear();
            }
            Ok(Event::Eof) => break,
            Err(error) => return Err(format!("invalid Cobertura XML: {error}")),
            _ => {}
        }
    }
    Ok((files_total, files_matched, lines_imported, symbols_matched))
}

pub fn run_coverage_import(
    runtime: &RuntimeOptions,
    file_path: &Path,
    format: &str,
) -> CommandResult {
    if format != "lcov" && format != "cobertura" {
        return CommandResult::failure(
            2,
            format!("unsupported coverage format: {format}; use lcov or cobertura"),
            RouteUsed::Local,
        );
    }
    if !file_path.exists() {
        return CommandResult::failure(
            1,
            format!("Coverage file does not exist: {}", file_path.display()),
            RouteUsed::Local,
        );
    }
    let content = match fs::read_to_string(file_path) {
        Ok(c) => c,
        Err(e) => {
            return CommandResult::failure(
                1,
                format!("Failed to read coverage file: {e}"),
                RouteUsed::Local,
            )
        }
    };

    let conn = match runtime.open_local_write_db() {
        Ok(c) => c,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };
    let ws_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };

    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();

    let source = if format == "cobertura" {
        "cobertura"
    } else {
        "lcov"
    };
    if let Err(error) = conn.execute_batch("BEGIN IMMEDIATE") {
        return CommandResult::failure(
            1,
            format!("cannot begin coverage import: {error}"),
            RouteUsed::Local,
        );
    }
    if let Err(error) = conn.execute(
        "DELETE FROM coverage_data
         WHERE report_source = ?1
           AND file_instance_id IN (SELECT id FROM file_instances WHERE workspace_id = ?2)",
        params![source, ws_id],
    ) {
        let _ = conn.execute_batch("ROLLBACK");
        return CommandResult::failure(
            1,
            format!("cannot clear old coverage data: {error}"),
            RouteUsed::Local,
        );
    }

    let (files_total, files_matched, lines_imported, symbols_matched) = if format == "cobertura" {
        match parse_cobertura(&content, &conn, ws_id, now) {
            Ok(stats) => stats,
            Err(error) => {
                let _ = conn.execute_batch("ROLLBACK");
                return CommandResult::failure(1, error, RouteUsed::Local);
            }
        }
    } else {
        let mut current_sf = None;
        let mut current_lines = Vec::new();
        let mut stats = (0, 0, 0, 0);
        for raw_line in content.lines() {
            let line = raw_line.trim();
            if let Some(path) = line.strip_prefix("SF:") {
                current_sf = Some(path.to_string());
                current_lines.clear();
            } else if let Some(value) = line.strip_prefix("DA:") {
                let mut parts = value.split(',');
                if let (Some(line_no), Some(hit_count)) = (parts.next(), parts.next()) {
                    if let (Ok(line_no), Ok(hit_count)) =
                        (line_no.parse::<i64>(), hit_count.parse::<i64>())
                    {
                        current_lines.push((line_no, hit_count));
                    }
                }
            } else if line == "end_of_record" {
                if let Some(path) = current_sf.take() {
                    stats.0 += 1;
                    let inserted =
                        insert_coverage_lines(&conn, ws_id, &path, &current_lines, "lcov", now)
                            .map_err(|error| error);
                    match inserted {
                        Ok((count, matched)) => {
                            stats.1 += 1;
                            stats.2 += count;
                            stats.3 += matched;
                        }
                        Err(error) => {
                            let _ = conn.execute_batch("ROLLBACK");
                            return CommandResult::failure(1, error, RouteUsed::Local);
                        }
                    }
                }
                current_lines.clear();
            }
        }
        if let Some(path) = current_sf.take() {
            stats.0 += 1;
            match insert_coverage_lines(&conn, ws_id, &path, &current_lines, "lcov", now) {
                Ok((count, matched)) => {
                    stats.1 += 1;
                    stats.2 += count;
                    stats.3 += matched;
                }
                Err(error) => {
                    let _ = conn.execute_batch("ROLLBACK");
                    return CommandResult::failure(1, error, RouteUsed::Local);
                }
            }
        }
        stats
    };
    if let Err(error) = conn.execute_batch("COMMIT") {
        let _ = conn.execute_batch("ROLLBACK");
        return CommandResult::failure(
            1,
            format!("coverage commit failed: {error}"),
            RouteUsed::Local,
        );
    }

    CommandResult::success_json(
        &json!({
            "imported": true,
            "file": file_path.to_string_lossy(),
            "format": format,
            "files_total": files_total,
            "files_matched": files_matched,
            "lines_imported": lines_imported,
            "symbols_matched": symbols_matched
        }),
        RouteUsed::Local,
    )
}

pub fn run_coverage_fn(runtime: &RuntimeOptions, function_name: &str) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(c) => c,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };
    let ws_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };

    let sql = "
        SELECT fi.rel_path, s.name,
               SUM(CASE WHEN cd.hit_count > 0 THEN 1 ELSE 0 END),
               COUNT(cd.id),
               CASE WHEN COUNT(cd.id) = 0 THEN 0.0
                    ELSE CAST(SUM(CASE WHEN cd.hit_count > 0 THEN 1 ELSE 0 END) AS REAL) / COUNT(cd.id)
               END
        FROM symbols s
        JOIN file_instances fi ON fi.id = s.file_instance_id
        LEFT JOIN coverage_data cd ON cd.symbol_id = s.id
        WHERE fi.workspace_id = ?1 AND (s.name = ?2 OR s.qualified_name = ?2)
        GROUP BY s.id, fi.rel_path, s.name
        ORDER BY s.id DESC LIMIT 10";
    let mut stmt = match conn.prepare(sql) {
        Ok(s) => s,
        Err(_) => {
            return CommandResult::success_json(
                &json!({"function": function_name, "found": false}),
                RouteUsed::Local,
            )
        }
    };

    let rows = stmt.query_map(params![ws_id, function_name], |row| {
        Ok(json!({
            "file_path": row.get::<_, String>(0)?,
            "symbol_name": row.get::<_, String>(1)?,
            "lines_covered": row.get::<_, i64>(2)?,
            "lines_total": row.get::<_, i64>(3)?,
            "coverage_ratio": row.get::<_, f64>(4)?,
        }))
    });

    let records: Vec<Value> = match rows {
        Ok(r) => r.filter_map(|x| x.ok()).collect(),
        Err(_) => vec![],
    };

    CommandResult::success_json(
        &json!({
            "function": function_name,
            "found": !records.is_empty(),
            "coverage_records": records
        }),
        RouteUsed::Local,
    )
}

pub fn run_coverage_uncovered(runtime: &RuntimeOptions) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(c) => c,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };
    let ws_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };

    let sql = "
        SELECT s.qualified_name, fi.rel_path
        FROM symbols s
        JOIN file_instances fi ON s.file_instance_id = fi.id
        WHERE fi.workspace_id = ?1 AND s.kind IN ('fn', 'method', 'function')
          AND NOT EXISTS (
              SELECT 1 FROM coverage_data cd
              WHERE cd.symbol_id = s.id AND cd.hit_count > 0
          )
        LIMIT 50";

    let mut stmt = match conn.prepare(sql) {
        Ok(s) => s,
        Err(_) => {
            return CommandResult::success_json(
                &json!({"uncovered_functions": []}),
                RouteUsed::Local,
            )
        }
    };

    let rows = stmt.query_map(params![ws_id], |row| {
        Ok(json!({
            "qualified_name": row.get::<_, String>(0)?,
            "file_path": row.get::<_, String>(1)?,
        }))
    });

    let uncovered: Vec<Value> = match rows {
        Ok(r) => r.filter_map(|x| x.ok()).collect(),
        Err(_) => vec![],
    };

    CommandResult::success_json(
        &json!({
            "total_uncovered": uncovered.len(),
            "uncovered_functions": uncovered
        }),
        RouteUsed::Local,
    )
}

// ===== 3. Git 子命令 =====

fn workspace_root(conn: &rusqlite::Connection, workspace_id: i64) -> Result<PathBuf, String> {
    let root: String = conn
        .query_row(
            "SELECT root_path FROM workspaces WHERE id = ?1",
            params![workspace_id],
            |row| row.get(0),
        )
        .map_err(|error| format!("cannot resolve workspace root: {error}"))?;
    let path = PathBuf::from(root);
    if !path.is_dir() {
        return Err(format!(
            "workspace root is not a directory: {}",
            path.display()
        ));
    }
    Ok(path)
}

fn git_failure(output: &Output, operation: &str) -> CommandResult {
    let stderr = String::from_utf8_lossy(&output.stderr);
    CommandResult::failure(
        1,
        format!(
            "git {operation} failed (exit={}): {}",
            output.status,
            stderr.trim()
        ),
        RouteUsed::Local,
    )
}

pub fn run_git_import(runtime: &RuntimeOptions, limit: usize) -> CommandResult {
    let conn = match runtime.open_local_write_db() {
        Ok(c) => c,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };
    let ws_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };
    let root = match workspace_root(&conn, ws_id) {
        Ok(root) => root,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let mut git = Command::new("git");
    git.current_dir(&root).args([
        "log",
        &format!("-n{limit}"),
        "--pretty=format:%H%x1f%an%x1f%ae%x1f%at%x1f%s",
    ]);
    let output = match run_command_with_timeout(git, runtime.timeout) {
        Ok(out) => out,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("git import failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    if !output.status.success() {
        return git_failure(&output, "log");
    }

    if let Err(error) = conn.execute_batch("BEGIN IMMEDIATE") {
        return CommandResult::failure(
            1,
            format!("cannot begin git import: {error}"),
            RouteUsed::Local,
        );
    }

    let stdout_str = String::from_utf8_lossy(&output.stdout);
    let mut imported = 0;
    for line in stdout_str.lines() {
        let parts: Vec<&str> = line.splitn(5, '\u{1f}').collect();
        if parts.len() >= 5 {
            let hash = parts[0];
            let author = parts[1];
            let email = parts[2];
            let time_sec: f64 = parts[3].parse().unwrap_or(0.0);
            let msg = parts[4];

            if let Err(error) = conn.execute(
                "INSERT OR REPLACE INTO git_commits
                    (commit_hash, message, author, email, timestamp, workspace_id)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                params![hash, msg, author, email, time_sec, ws_id],
            ) {
                let _ = conn.execute_batch("ROLLBACK");
                return CommandResult::failure(
                    1,
                    format!("failed to store git commit {hash}: {error}"),
                    RouteUsed::Local,
                );
            }
            imported += 1;

            // Keep the file-level relation in the formal schema.  Files that are
            // not indexed in this workspace are intentionally skipped.
            let mut diff = Command::new("git");
            diff.current_dir(&root).args([
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-status",
                "-r",
                "-z",
                hash,
            ]);
            if let Ok(diff_output) = run_command_with_timeout(diff, runtime.timeout) {
                if diff_output.status.success() {
                    let fields: Vec<&[u8]> = diff_output
                        .stdout
                        .split(|byte| *byte == 0)
                        .filter(|field| !field.is_empty())
                        .collect();
                    let mut index = 0;
                    while index + 1 < fields.len() {
                        let change_type = String::from_utf8_lossy(fields[index]).to_string();
                        let path = String::from_utf8_lossy(fields[index + 1]).replace('\\', "/");
                        let file_instance_id: Option<i64> = conn.query_row(
                            "SELECT id FROM file_instances WHERE workspace_id = ?1 AND rel_path = ?2 LIMIT 1",
                            params![ws_id, path],
                            |row| row.get(0),
                        ).ok();
                        if let Some(file_instance_id) = file_instance_id {
                            if let Err(error) = conn.execute(
                                "INSERT INTO git_file_changes
                                    (commit_hash, file_instance_id, change_type)
                                 VALUES (?1, ?2, ?3)",
                                params![hash, file_instance_id, change_type],
                            ) {
                                let _ = conn.execute_batch("ROLLBACK");
                                return CommandResult::failure(
                                    1,
                                    format!("failed to store git file change {path}: {error}"),
                                    RouteUsed::Local,
                                );
                            }
                        }
                        index += if change_type.starts_with('R') || change_type.starts_with('C') {
                            3
                        } else {
                            2
                        };
                    }
                }
            }
        }
    }

    if let Err(error) = conn.execute_batch("COMMIT") {
        let _ = conn.execute_batch("ROLLBACK");
        return CommandResult::failure(
            1,
            format!("git import commit failed: {error}"),
            RouteUsed::Local,
        );
    }

    CommandResult::success_json(
        &json!({ "imported": imported, "limit": limit }),
        RouteUsed::Local,
    )
}

pub fn run_git_log(runtime: &RuntimeOptions, limit: usize) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(c) => c,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };
    let ws_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };

    let sql = "
        SELECT commit_hash, author, email, timestamp, message
        FROM git_commits
        WHERE workspace_id = ?1
        ORDER BY timestamp DESC LIMIT ?2";

    let mut stmt = match conn.prepare(sql) {
        Ok(s) => s,
        Err(_) => {
            return CommandResult::failure(
                1,
                "git_commits table not found, run `cw git import` first".to_string(),
                RouteUsed::Local,
            )
        }
    };

    let rows = stmt.query_map(params![ws_id, limit as i64], |row| {
        Ok(json!({
            "commit_hash": row.get::<_, String>(0)?,
            "author": row.get::<_, Option<String>>(1)?.unwrap_or_default(),
            "email": row.get::<_, Option<String>>(2)?.unwrap_or_default(),
            "time": row.get::<_, Option<f64>>(3)?.unwrap_or(0.0),
            "message": row.get::<_, Option<String>>(4)?.unwrap_or_default(),
        }))
    });

    let commits: Vec<Value> = match rows {
        Ok(r) => r.filter_map(|x| x.ok()).collect(),
        Err(_) => vec![],
    };

    CommandResult::success_json(
        &json!({ "total": commits.len(), "commits": commits }),
        RouteUsed::Local,
    )
}

pub fn run_git_show(runtime: &RuntimeOptions, commit_sha: &str) -> CommandResult {
    if commit_sha.len() < 7
        || commit_sha.len() > 64
        || !commit_sha
            .chars()
            .all(|character| character.is_ascii_hexdigit())
    {
        return CommandResult::failure(
            2,
            format!("invalid commit SHA: {commit_sha}"),
            RouteUsed::Local,
        );
    }
    let conn = match runtime.open_local_db() {
        Ok(c) => c,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };
    let ws_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };
    let root = match workspace_root(&conn, ws_id) {
        Ok(root) => root,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let mut git = Command::new("git");
    git.current_dir(root).args(["show", "--stat", commit_sha]);
    let output = match run_command_with_timeout(git, runtime.timeout) {
        Ok(out) => out,
        Err(error) => {
            return CommandResult::failure(1, format!("git show failed: {error}"), RouteUsed::Local)
        }
    };

    if !output.status.success() {
        return git_failure(&output, "show");
    }

    CommandResult::success_text(
        String::from_utf8_lossy(&output.stdout).to_string(),
        RouteUsed::Local,
    )
}

pub fn run_git_stats(runtime: &RuntimeOptions) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(c) => c,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };
    let ws_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };

    let mut total_commits: i64 = 0;
    let mut total_authors: i64 = 0;

    if let Err(error) = conn.query_row(
        "SELECT COUNT(*), COUNT(DISTINCT email) FROM git_commits WHERE workspace_id = ?1",
        params![ws_id],
        |row| {
            total_commits = row.get(0)?;
            total_authors = row.get(1)?;
            Ok(())
        },
    ) {
        return CommandResult::failure(
            1,
            format!("cannot read git statistics: {error}"),
            RouteUsed::Local,
        );
    }

    CommandResult::success_json(
        &json!({
            "total_commits": total_commits,
            "total_authors": total_authors
        }),
        RouteUsed::Local,
    )
}

// ===== 4. Install Agent & Hook 子命令 =====

pub fn run_install_agent(
    _runtime: &RuntimeOptions,
    agent: Option<String>,
    output_dir: Option<PathBuf>,
    force: bool,
    global: bool,
) -> CommandResult {
    let target_agent = agent.unwrap_or_else(|| "all".to_string());
    let out_dir = output_dir.unwrap_or_else(|| PathBuf::from(".callwarden/agent-integrations"));

    if !out_dir.exists() {
        if let Err(error) = fs::create_dir_all(&out_dir) {
            return CommandResult::failure(
                1,
                format!("failed to create agent output directory: {error}"),
                RouteUsed::Local,
            );
        }
    }

    let agents = if target_agent == "all" {
        vec!["claude", "codex", "cursor", "copilot", "windsurf"]
    } else {
        vec![target_agent.as_str()]
    };

    let mut generated = vec![];
    for ag in agents {
        let file_path = out_dir.join(format!("{ag}_callwarden.json"));
        if !file_path.exists() || force {
            let content = json!({
                "agent": ag,
                "version": "1.0.0",
                "mcp_servers": {
                    "callwarden": {
                        "command": "cw",
                        "args": ["server"]
                    }
                },
                "is_global": global
            });
            let serialized = match serde_json::to_string_pretty(&content) {
                Ok(value) => value,
                Err(error) => {
                    return CommandResult::failure(
                        1,
                        format!("failed to serialize agent config: {error}"),
                        RouteUsed::Local,
                    )
                }
            };
            if let Err(error) = fs::write(&file_path, serialized) {
                return CommandResult::failure(
                    1,
                    format!(
                        "failed to write agent config {}: {error}",
                        file_path.display()
                    ),
                    RouteUsed::Local,
                );
            }
            generated.push(file_path.to_string_lossy().to_string());
        }
    }

    CommandResult::success_json(
        &json!({
            "installed": true,
            "output_dir": out_dir.to_string_lossy(),
            "generated_files": generated
        }),
        RouteUsed::Local,
    )
}

pub fn run_install_hook(
    _runtime: &RuntimeOptions,
    hook_name: &str,
    task_id: &str,
    uninstall: bool,
) -> CommandResult {
    let hook_path = PathBuf::from(".git/hooks").join(hook_name);
    if uninstall {
        if hook_path.exists() {
            if let Err(error) = fs::remove_file(&hook_path) {
                return CommandResult::failure(
                    1,
                    format!("failed to remove hook {}: {error}", hook_path.display()),
                    RouteUsed::Local,
                );
            }
        }
        return CommandResult::success_json(
            &json!({ "uninstalled": true, "hook": hook_name }),
            RouteUsed::Local,
        );
    }

    let task_arg = if task_id.is_empty() {
        "--auto".to_string()
    } else {
        format!("--task-id {task_id}")
    };
    let hook_content =
        format!("#!/bin/sh\n# Call Warden post-commit hook\ncw task capture-diff {task_arg}\n");

    if let Some(parent) = hook_path.parent() {
        if let Err(error) = fs::create_dir_all(parent) {
            return CommandResult::failure(
                1,
                format!("failed to create hook directory: {error}"),
                RouteUsed::Local,
            );
        }
    }

    if let Err(e) = fs::write(&hook_path, hook_content) {
        return CommandResult::failure(
            1,
            format!("Failed to write hook file: {e}"),
            RouteUsed::Local,
        );
    }

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(meta) = fs::metadata(&hook_path) {
            let mut perms = meta.permissions();
            perms.set_mode(0o755);
            if let Err(error) = fs::set_permissions(&hook_path, perms) {
                return CommandResult::failure(
                    1,
                    format!("failed to make hook executable: {error}"),
                    RouteUsed::Local,
                );
            }
        }
    }

    CommandResult::success_json(
        &json!({
            "installed": true,
            "hook": hook_name,
            "path": hook_path.to_string_lossy(),
            "mode": if task_id.is_empty() { "auto" } else { task_id }
        }),
        RouteUsed::Local,
    )
}

// ===== 5. GC 子命令 =====

fn delete_file_graph_data(
    conn: &rusqlite::Connection,
    file_instance_id: i64,
) -> Result<(), String> {
    for sql in [
        "DELETE FROM coverage_data WHERE file_instance_id = ?1",
        "DELETE FROM calls WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id = ?1)
             OR callee_id IN (SELECT id FROM symbols WHERE file_instance_id = ?1)",
        "DELETE FROM file_symbol_versions WHERE file_version_id IN
             (SELECT id FROM file_versions WHERE file_instance_id = ?1)",
        "DELETE FROM file_versions WHERE file_instance_id = ?1",
        "DELETE FROM symbols WHERE file_instance_id = ?1",
    ] {
        conn.execute(sql, params![file_instance_id])
            .map_err(|error| {
                format!("failed to delete file {file_instance_id} graph data: {error}")
            })?;
    }
    Ok(())
}

pub fn run_gc_archive(runtime: &RuntimeOptions, force: bool, dry_run: bool) -> CommandResult {
    let conn = match runtime.open_local_write_db() {
        Ok(c) => c,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };
    let ws_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };

    let root = match workspace_root(&conn, ws_id) {
        Ok(root) => root,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let patterns = load_ignore_patterns(&root);
    let status_filter = if force {
        "status NOT IN ('archived', 'deleted')"
    } else {
        "status = 'pending'"
    };
    let sql = format!("SELECT id, rel_path, abs_path, current_content_hash, status FROM file_instances WHERE workspace_id = ?1 AND {status_filter}");
    let mut stmt = match conn.prepare(&sql) {
        Ok(stmt) => stmt,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("cannot query GC candidates: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let candidates: Vec<(i64, String, String, String, String)> =
        match stmt.query_map(params![ws_id], |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
            ))
        }) {
            Ok(rows) => match rows.collect() {
                Ok(rows) => rows,
                Err(error) => {
                    return CommandResult::failure(
                        1,
                        format!("cannot read GC candidates: {error}"),
                        RouteUsed::Local,
                    )
                }
            },
            Err(error) => {
                return CommandResult::failure(
                    1,
                    format!("cannot read GC candidates: {error}"),
                    RouteUsed::Local,
                )
            }
        };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    if !dry_run {
        if let Err(error) = conn.execute_batch("BEGIN IMMEDIATE") {
            return CommandResult::failure(
                1,
                format!("cannot begin GC archive: {error}"),
                RouteUsed::Local,
            );
        }
    }
    let mut archived = 0usize;
    let mut activated = 0usize;
    let mut skipped = 0usize;
    for (file_id, rel_path, abs_path, content_hash, status) in &candidates {
        if !should_ignore(rel_path, false, &patterns) {
            if !dry_run && status == "pending" {
                if let Err(error) = conn.execute(
                    "UPDATE file_instances SET status = 'active' WHERE id = ?1",
                    params![file_id],
                ) {
                    let _ = conn.execute_batch("ROLLBACK");
                    return CommandResult::failure(
                        1,
                        format!("failed to activate {rel_path}: {error}"),
                        RouteUsed::Local,
                    );
                }
                activated += 1;
            }
            continue;
        }
        let already_archived: bool = conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM archived_files WHERE file_instance_id = ?1)",
                params![file_id],
                |row| row.get(0),
            )
            .unwrap_or(false);
        if already_archived {
            skipped += 1;
            continue;
        }
        archived += 1;
        if !dry_run {
            let symbol_count: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM symbols WHERE file_instance_id = ?1",
                    params![file_id],
                    |row| row.get(0),
                )
                .unwrap_or(0);
            let call_count: i64 = conn.query_row("SELECT COUNT(*) FROM calls WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id = ?1)", params![file_id], |row| row.get(0)).unwrap_or(0);
            if let Err(error) = conn.execute(
                "INSERT INTO archived_files (file_instance_id, workspace_id, rel_path, abs_path, content_hash, symbol_count, call_count, archive_reason, archived_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
                params![file_id, ws_id, rel_path, abs_path, content_hash, symbol_count, call_count, "matched ignore rule", now],
            ) {
                let _ = conn.execute_batch("ROLLBACK");
                return CommandResult::failure(1, format!("failed to record archived file {rel_path}: {error}"), RouteUsed::Local);
            }
            if let Err(error) = delete_file_graph_data(&conn, *file_id) {
                let _ = conn.execute_batch("ROLLBACK");
                return CommandResult::failure(1, error, RouteUsed::Local);
            }
            if let Err(error) = conn.execute(
                "UPDATE file_instances SET status = 'archived' WHERE id = ?1",
                params![file_id],
            ) {
                let _ = conn.execute_batch("ROLLBACK");
                return CommandResult::failure(
                    1,
                    format!("failed to mark {rel_path} archived: {error}"),
                    RouteUsed::Local,
                );
            }
        }
    }
    if !dry_run {
        if let Err(error) = conn.execute_batch("COMMIT") {
            let _ = conn.execute_batch("ROLLBACK");
            return CommandResult::failure(
                1,
                format!("GC archive commit failed: {error}"),
                RouteUsed::Local,
            );
        }
    }
    CommandResult::success_json(
        &json!({
            "action": "archive", "force": force, "dry_run": dry_run, "workspace_id": ws_id,
            "scanned": candidates.len(), "archived": archived, "activated": activated, "skipped": skipped
        }),
        RouteUsed::Local,
    )
}

pub fn run_gc_restore(runtime: &RuntimeOptions, paths: &[PathBuf], force: bool) -> CommandResult {
    let conn = match runtime.open_local_write_db() {
        Ok(c) => c,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };
    let ws_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };

    let root = match workspace_root(&conn, ws_id) {
        Ok(root) => root,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let patterns = load_ignore_patterns(&root);
    let mut stmt = match conn.prepare(
        "SELECT id, file_instance_id, rel_path FROM archived_files WHERE workspace_id = ?1",
    ) {
        Ok(stmt) => stmt,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("cannot query archived files: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let rows: Vec<(i64, i64, String)> = match stmt.query_map(params![ws_id], |row| {
        Ok((row.get(0)?, row.get(1)?, row.get(2)?))
    }) {
        Ok(rows) => match rows.collect() {
            Ok(rows) => rows,
            Err(error) => {
                return CommandResult::failure(
                    1,
                    format!("cannot read archived files: {error}"),
                    RouteUsed::Local,
                )
            }
        },
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("cannot read archived files: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let requested: std::collections::HashSet<String> = paths
        .iter()
        .map(|path| path.to_string_lossy().replace('\\', "/"))
        .collect();
    if let Err(error) = conn.execute_batch("BEGIN IMMEDIATE") {
        return CommandResult::failure(
            1,
            format!("cannot begin GC restore: {error}"),
            RouteUsed::Local,
        );
    }
    let mut restored_count = 0usize;
    let mut still_ignored = 0usize;
    for (archive_id, file_id, rel_path) in &rows {
        if !requested.is_empty() && !requested.contains(rel_path) {
            continue;
        }
        if !force && should_ignore(rel_path, false, &patterns) {
            still_ignored += 1;
            continue;
        }
        if let Err(error) = conn
            .execute(
                "DELETE FROM archived_files WHERE id = ?1",
                params![archive_id],
            )
            .and_then(|_| {
                conn.execute(
                    "UPDATE file_instances SET status = 'pending' WHERE id = ?1",
                    params![file_id],
                )
            })
        {
            let _ = conn.execute_batch("ROLLBACK");
            return CommandResult::failure(
                1,
                format!("failed to restore {rel_path}: {error}"),
                RouteUsed::Local,
            );
        }
        restored_count += 1;
    }
    if let Err(error) = conn.execute_batch("COMMIT") {
        let _ = conn.execute_batch("ROLLBACK");
        return CommandResult::failure(
            1,
            format!("GC restore commit failed: {error}"),
            RouteUsed::Local,
        );
    }

    CommandResult::success_json(
        &json!({
            "scanned": rows.len(),
            "restored_count": restored_count,
            "still_ignored": still_ignored,
            "force": force
        }),
        RouteUsed::Local,
    )
}

pub fn run_gc_status(runtime: &RuntimeOptions) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(c) => c,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };
    let ws_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };

    let (active_count, archived_count, deleted_count, total_count): (i64, i64, i64, i64) =
        match conn.query_row(
            "SELECT COALESCE(SUM(status = 'active'), 0), COALESCE(SUM(status = 'archived'), 0),
                COALESCE(SUM(status = 'deleted'), 0), COUNT(*)
         FROM file_instances WHERE workspace_id = ?1",
            params![ws_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        ) {
            Ok(value) => value,
            Err(error) => {
                return CommandResult::failure(
                    1,
                    format!("cannot read GC status: {error}"),
                    RouteUsed::Local,
                )
            }
        };
    let (archived_symbols, archived_calls): (i64, i64) = conn
        .query_row(
            "SELECT COALESCE(SUM(symbol_count), 0), COALESCE(SUM(call_count), 0)
         FROM archived_files WHERE workspace_id = ?1",
            params![ws_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap_or((0, 0));

    CommandResult::success_json(
        &json!({
            "workspace_id": ws_id,
            "active_files": active_count,
            "archived_files_count": archived_count,
            "deleted_files": deleted_count,
            "archive_ratio": if total_count == 0 { 0.0 } else { archived_count as f64 / total_count as f64 },
            "archived_symbols": archived_symbols,
            "archived_calls": archived_calls
        }),
        RouteUsed::Local,
    )
}

pub fn run_gc_purge(runtime: &RuntimeOptions, dry_run: bool) -> CommandResult {
    let conn = match runtime.open_local_write_db() {
        Ok(c) => c,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };
    let ws_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };

    let purged: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM archived_files WHERE workspace_id = ?1",
            params![ws_id],
            |row| row.get(0),
        )
        .unwrap_or(0);
    if !dry_run && purged > 0 {
        if let Err(error) = conn.execute_batch("BEGIN IMMEDIATE") {
            return CommandResult::failure(
                1,
                format!("cannot begin GC purge: {error}"),
                RouteUsed::Local,
            );
        }
        let ids: Vec<i64> = match conn
            .prepare("SELECT file_instance_id FROM archived_files WHERE workspace_id = ?1")
            .and_then(|mut stmt| {
                stmt.query_map(params![ws_id], |row| row.get(0))
                    .and_then(|rows| rows.collect::<Result<Vec<i64>, _>>())
            }) {
            Ok(ids) => ids,
            Err(error) => {
                let _ = conn.execute_batch("ROLLBACK");
                return CommandResult::failure(
                    1,
                    format!("cannot read purge candidates: {error}"),
                    RouteUsed::Local,
                );
            }
        };
        for file_id in &ids {
            if let Err(error) = delete_file_graph_data(&conn, *file_id) {
                let _ = conn.execute_batch("ROLLBACK");
                return CommandResult::failure(1, error, RouteUsed::Local);
            }
        }
        if let Err(error) = conn
            .execute(
                "DELETE FROM archived_files WHERE workspace_id = ?1",
                params![ws_id],
            )
            .and_then(|_| {
                conn.execute(
                    "DELETE FROM file_instances WHERE workspace_id = ?1 AND status = 'archived'",
                    params![ws_id],
                )
            })
            .and_then(|_| conn.execute_batch("COMMIT"))
        {
            let _ = conn.execute_batch("ROLLBACK");
            return CommandResult::failure(
                1,
                format!("GC purge failed: {error}"),
                RouteUsed::Local,
            );
        }
    }

    CommandResult::success_json(
        &json!({
            "purged_count": purged,
            "dry_run": dry_run
        }),
        RouteUsed::Local,
    )
}

pub fn run_gc_db_cleanup(
    runtime: &RuntimeOptions,
    dry_run: bool,
    all_but_current: bool,
) -> CommandResult {
    let conn = match runtime.open_local_write_db() {
        Ok(c) => c,
        Err(e) => return CommandResult::failure(1, e, RouteUsed::Local),
    };

    if !dry_run {
        if let Err(error) = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE); VACUUM;") {
            return CommandResult::failure(
                1,
                format!("database cleanup failed: {error}"),
                RouteUsed::Local,
            );
        }
    }

    CommandResult::success_json(
        &json!({
            "cleaned": true,
            "dry_run": dry_run,
            "all_but_current": all_but_current
        }),
        RouteUsed::Local,
    )
}

// ===== 6. Doctor 子命令 =====

pub fn run_doctor(runtime: &RuntimeOptions, add_defender_exclusion: bool) -> CommandResult {
    let db_path = &runtime.db_path;
    let exists = db_path.exists();
    let db_size = if exists {
        fs::metadata(db_path).map(|m| m.len()).unwrap_or(0)
    } else {
        0
    };

    let conn_ok = runtime
        .open_local_db()
        .map(|conn| {
            conn.query_row(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'workspaces'",
                [],
                |_row| Ok(()),
            )
            .is_ok()
        })
        .unwrap_or(false);

    if add_defender_exclusion {
        #[cfg(target_os = "windows")]
        {
            let ps_script = format!("Add-MpPreference -ExclusionPath '{}'", db_path.display());
            let status = Command::new("powershell")
                .args(["-Command", &ps_script])
                .status();

            let success = status.map(|s| s.success()).unwrap_or(false);
            return if success {
                CommandResult::success_json(
                    &json!({ "defender_exclusion_added": true, "target_path": db_path.to_string_lossy() }),
                    RouteUsed::Local,
                )
            } else {
                CommandResult::failure(
                    1,
                    "failed to add Windows Defender exclusion".to_string(),
                    RouteUsed::Local,
                )
            };
        }

        #[cfg(not(target_os = "windows"))]
        {
            return CommandResult::failure(
                2,
                "Windows Defender exclusion is only applicable on Windows OS.".to_string(),
                RouteUsed::Local,
            );
        }
    }

    if !exists || !conn_ok {
        return CommandResult::failure(
            1,
            format!(
                "doctor failed: db_exists={}, db_connection_ok={}, db_path={}",
                exists,
                conn_ok,
                db_path.display()
            ),
            RouteUsed::Local,
        );
    }
    CommandResult::success_json(
        &json!({ "doctor": "passed", "db_path": db_path.to_string_lossy(), "db_exists": true,
                 "db_size_bytes": db_size, "db_connection_ok": true, "platform": std::env::consts::OS }),
        RouteUsed::Local,
    )
}

fn evolution_cutoff(window: &str) -> f64 {
    let value = window.trim();
    if value.len() < 2 {
        return 0.0;
    }
    let (number, unit) = value.split_at(value.len() - 1);
    let number: f64 = match number.parse() {
        Ok(number) => number,
        Err(_) => return 0.0,
    };
    let seconds = match unit {
        "d" => 86_400.0,
        "w" => 604_800.0,
        "m" => 2_592_000.0,
        "y" => 31_536_000.0,
        _ => return 0.0,
    };
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    now - number * seconds
}

pub fn run_review_report(runtime: &RuntimeOptions, symbol_hash: &str) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(conn) => conn,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let impact = match query_local_impact(&conn, workspace_id, symbol_hash, 3) {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let total = impact["total_impacted"].as_i64().unwrap_or(0);
    let scope = if total > 20 {
        "high"
    } else if total > 5 {
        "medium"
    } else {
        "low"
    };
    let mut must_test = Vec::new();
    if let Some(layers) = impact["layers"].as_array() {
        for layer in layers {
            if let Some(symbols) = layer["symbols"].as_array() {
                for symbol in symbols {
                    if symbol["visibility"].as_str() == Some("public")
                        && matches!(
                            symbol["kind"].as_str(),
                            Some("fn") | Some("function") | Some("method")
                        )
                    {
                        must_test.push(json!({"qualified_name":symbol["qualified_name"],"name":symbol["name"],"file_path":symbol["file_path"]}));
                    }
                }
            }
        }
    }
    CommandResult::success_json(
        &json!({"symbol_hash":symbol_hash,"impact_scope":scope,"risk_level":scope,"total_impacted":total,"must_test":must_test,"review_points":[],"by_layer":impact["by_layer"],"impact":impact}),
        RouteUsed::Local,
    )
}

pub fn run_churn_report(
    runtime: &RuntimeOptions,
    module_filter: &str,
    window: &str,
) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(conn) => conn,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let cutoff = evolution_cutoff(window);
    let mut sql="SELECT gfc.file_instance_id,COALESCE(gfc.lines_added,0),COALESCE(gfc.lines_deleted,0),COALESCE(gc.timestamp,0),fi.rel_path FROM git_file_changes gfc JOIN file_instances fi ON gfc.file_instance_id=fi.id LEFT JOIN git_commits gc ON gfc.commit_hash=gc.commit_hash WHERE fi.workspace_id=?1".to_string();
    if !module_filter.is_empty() {
        sql.push_str(" AND fi.module_path LIKE ?2");
    }
    if cutoff > 0.0 {
        sql.push_str(if module_filter.is_empty() {
            " AND gc.timestamp>=?2"
        } else {
            " AND gc.timestamp>=?3"
        });
    }
    let mut stmt = match conn.prepare(&sql) {
        Ok(stmt) => stmt,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("churn query failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let mut rows_data: Vec<(i64, i64, i64, f64, String)> = Vec::new();
    if module_filter.is_empty() {
        if cutoff > 0.0 {
            if let Ok(rows) = stmt.query_map(params![workspace_id, cutoff], |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                ))
            }) {
                rows_data.extend(rows.flatten());
            }
        } else if let Ok(rows) = stmt.query_map([workspace_id], |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
            ))
        }) {
            rows_data.extend(rows.flatten());
        }
    } else if cutoff > 0.0 {
        if let Ok(rows) = stmt.query_map(
            params![workspace_id, format!("{module_filter}%"), cutoff],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                ))
            },
        ) {
            rows_data.extend(rows.flatten());
        }
    } else if let Ok(rows) =
        stmt.query_map(params![workspace_id, format!("{module_filter}%")], |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
            ))
        })
    {
        rows_data.extend(rows.flatten());
    }
    let mut files: HashMap<i64, Value> = HashMap::new();
    let mut trend: HashMap<String, i64> = HashMap::new();
    let mut total = 0_i64;
    for row in rows_data {
        let churn = row.1 + row.2;
        total += churn;
        let entry=files.entry(row.0).or_insert_with(||json!({"file_instance_id":row.0,"rel_path":row.4,"change_count":0,"churned_lines":0}));
        entry["change_count"] = json!(entry["change_count"].as_i64().unwrap_or(0) + 1);
        entry["churned_lines"] = json!(entry["churned_lines"].as_i64().unwrap_or(0) + churn);
        if row.3 > 0.0 {
            let day = format!("{}", (row.3 / 86400.0).floor());
            *trend.entry(day).or_default() += churn;
        }
    }
    let current:i64=conn.query_row("SELECT COALESCE(SUM(total_lines),0) FROM file_versions WHERE is_current=1 AND file_instance_id IN (SELECT id FROM file_instances WHERE workspace_id=?1)",[workspace_id],|row|row.get(0)).unwrap_or(0);
    let mut top: Vec<Value> = files.into_values().collect();
    top.sort_by(|left, right| {
        right["churned_lines"]
            .as_i64()
            .cmp(&left["churned_lines"].as_i64())
    });
    top.truncate(10);
    let trend: Vec<Value> = trend
        .into_iter()
        .map(|(date, churned_lines)| json!({"date":date,"churned_lines":churned_lines}))
        .collect();
    CommandResult::success_json(
        &json!({"module":module_filter,"window":window,"churn_rate":if current==0{0.0}else{total as f64/current as f64},"total_churned_lines":total,"changed_files":top.len(),"total_lines_current":current,"top_churned_files":top,"trend":trend}),
        RouteUsed::Local,
    )
}

pub fn run_evolution_report(
    runtime: &RuntimeOptions,
    qualified_name: &str,
    window: &str,
    defects: bool,
) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(conn) => conn,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let cutoff = evolution_cutoff(window);
    let mut sql = "SELECT DISTINCT fv.id,fv.parsed_at,COALESCE(fv.commit_hash,''),COALESCE(gc.author,''),COALESCE(gc.message,'')
                   FROM file_symbol_versions fsv JOIN file_versions fv ON fsv.file_version_id=fv.id
                   JOIN file_instances fi ON fv.file_instance_id=fi.id LEFT JOIN git_commits gc ON fv.commit_hash=gc.commit_hash
                   WHERE fi.workspace_id=?1 AND fsv.qualified_name=?2".to_string();
    if cutoff > 0.0 {
        sql.push_str(" AND fv.parsed_at>=?3");
    }
    sql.push_str(" ORDER BY fv.parsed_at ASC");
    let mut stmt = match conn.prepare(&sql) {
        Ok(stmt) => stmt,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("evolution query failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let mut evolution_rows: Vec<(i64, f64, String, String, String)> = Vec::new();
    if cutoff > 0.0 {
        let rows = match stmt.query_map(params![workspace_id, qualified_name, cutoff], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, f64>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
            ))
        }) {
            Ok(rows) => rows,
            Err(error) => {
                return CommandResult::failure(
                    1,
                    format!("evolution rows failed: {error}"),
                    RouteUsed::Local,
                )
            }
        };
        evolution_rows.extend(rows.flatten());
    } else {
        let rows = match stmt.query_map(params![workspace_id, qualified_name], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, f64>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
            ))
        }) {
            Ok(rows) => rows,
            Err(error) => {
                return CommandResult::failure(
                    1,
                    format!("evolution rows failed: {error}"),
                    RouteUsed::Local,
                )
            }
        };
        evolution_rows.extend(rows.flatten());
    }
    let mut seen = std::collections::HashSet::new();
    let mut timeline = Vec::new();
    let mut changers = Vec::new();
    let mut timestamps = Vec::new();
    for row in evolution_rows {
        if !seen.insert(row.0) {
            continue;
        }
        timestamps.push(row.1);
        if !row.3.is_empty() && !changers.contains(&row.3) {
            changers.push(row.3.clone());
        }
        timeline
            .push(json!({"timestamp":row.1,"commit_hash":row.2,"author":row.3,"message":row.4}));
    }
    let intervals: Vec<f64> = timestamps
        .windows(2)
        .map(|pair| (pair[1] - pair[0]).max(0.0))
        .collect();
    let avg = if intervals.is_empty() {
        0.0
    } else {
        intervals.iter().sum::<f64>() / intervals.len() as f64
    };
    let mut report = json!({"qualified_name":qualified_name,"change_count":timestamps.len(),"first_seen":timestamps.first().copied().unwrap_or(0.0),"last_changed":timestamps.last().copied().unwrap_or(0.0),"changers":changers,"timeline":timeline,"intervals":intervals,"avg_interval":avg,"window":window});
    if defects {
        let mut defect_types = HashMap::new();
        let mut defect_count = 0_i64;
        if let Ok(mut defect_stmt)=conn.prepare("SELECT sf.rule_id,COUNT(*) FROM semgrep_findings sf JOIN file_instances fi ON fi.id=sf.file_instance_id WHERE fi.workspace_id=?2 AND sf.symbol_qualified=?1 GROUP BY sf.rule_id") { if let Ok(defect_rows)=defect_stmt.query_map(params![qualified_name, workspace_id],|row|Ok((row.get::<_,String>(0)?,row.get::<_,i64>(1)?))) { for row in defect_rows.flatten(){defect_count+=row.1; defect_types.insert(row.0,row.1);} } }
        report["defect_correlation"] = json!({"qualified_name":qualified_name,"change_count":timestamps.len(),"defect_count":defect_count,"defect_rate":if timestamps.is_empty(){0.0}else{defect_count as f64/timestamps.len() as f64},"defect_types":defect_types});
    }
    CommandResult::success_json(&report, RouteUsed::Local)
}

pub fn run_hotspot_report(
    runtime: &RuntimeOptions,
    module_filter: &str,
    limit: usize,
) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(conn) => conn,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let mut sql="SELECT s.symbol_hash,s.qualified_name,COALESCE(s.module_path,''),s.start_line,s.end_line,COALESCE(sc.content,''),COALESCE(fi.rel_path,'') FROM symbols s JOIN file_instances fi ON s.file_instance_id=fi.id LEFT JOIN symbol_contents sc ON s.symbol_hash=sc.content_hash WHERE fi.workspace_id=?1 AND s.kind IN ('fn','function','method') AND s.qualified_name!=''".to_string();
    if !module_filter.is_empty() {
        sql.push_str(" AND s.module_path LIKE ?2");
    }
    let mut stmt = match conn.prepare(&sql) {
        Ok(stmt) => stmt,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("hotspot query failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let mut symbol_rows: Vec<(String, String, String, i64, i64, String, String)> = Vec::new();
    if module_filter.is_empty() {
        let rows = match stmt.query_map([workspace_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, i64>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, String>(6)?,
            ))
        }) {
            Ok(rows) => rows,
            Err(error) => {
                return CommandResult::failure(
                    1,
                    format!("hotspot rows failed: {error}"),
                    RouteUsed::Local,
                )
            }
        };
        symbol_rows.extend(rows.flatten());
    } else {
        let rows = match stmt.query_map(params![workspace_id, format!("{module_filter}%")], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, i64>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, String>(6)?,
            ))
        }) {
            Ok(rows) => rows,
            Err(error) => {
                return CommandResult::failure(
                    1,
                    format!("hotspot rows failed: {error}"),
                    RouteUsed::Local,
                )
            }
        };
        symbol_rows.extend(rows.flatten());
    }
    let mut change_map = HashMap::new();
    if let Ok(mut change_stmt)=conn.prepare("SELECT fsv.symbol_hash,COUNT(DISTINCT fsv.file_version_id),COALESCE(MAX(fv.parsed_at),0) FROM file_symbol_versions fsv JOIN file_versions fv ON fsv.file_version_id=fv.id JOIN file_instances fi ON fv.file_instance_id=fi.id WHERE fi.workspace_id=?1 GROUP BY fsv.symbol_hash"){ if let Ok(change_rows)=change_stmt.query_map([workspace_id],|row|Ok((row.get::<_,String>(0)?,row.get::<_,i64>(1)?,row.get::<_,f64>(2)?))){for row in change_rows.flatten(){change_map.insert(row.0,(row.1,row.2));}} }
    let mut defect_map = HashMap::new();
    if let Ok(mut defect_stmt)=conn.prepare("SELECT sf.symbol_qualified,COUNT(*) FROM semgrep_findings sf JOIN file_instances fi ON fi.id=sf.file_instance_id WHERE fi.workspace_id=?1 AND sf.symbol_qualified!='' GROUP BY sf.symbol_qualified"){if let Ok(defect_rows)=defect_stmt.query_map([workspace_id],|row|Ok((row.get::<_,String>(0)?,row.get::<_,i64>(1)?))){for row in defect_rows.flatten(){defect_map.insert(row.0,row.1);}}}
    let mut raw:Vec<(String,Value,i64,i64,i64)>=symbol_rows.into_iter().map(|row|{let change=change_map.get(&row.0).copied().unwrap_or((0,0.0));let defect=*defect_map.get(&row.1).unwrap_or(&0);let complexity=metric_complexity(&row.5,&metric_language(Some(&row.6)));(row.1.clone(),json!({"qualified_name":row.1,"symbol_hash":row.0,"module_path":row.2,"change_count":change.0,"defect_count":defect,"complexity":complexity,"first_seen":0.0,"last_changed":change.1}),change.0,defect,complexity)}).collect();
    let max_change = raw.iter().map(|row| row.2).max().unwrap_or(1).max(1) as f64;
    let max_defect = raw.iter().map(|row| row.3).max().unwrap_or(1).max(1) as f64;
    let max_complexity = raw.iter().map(|row| row.4).max().unwrap_or(1).max(1) as f64;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    let mut results: Vec<Value> = raw
        .drain(..)
        .map(|(_, mut value, change, defect, complexity)| {
            let score = change as f64 / max_change * 0.4
                + defect as f64 / max_defect * 0.3
                + complexity as f64 / max_complexity * 0.3;
            let last = value["last_changed"].as_f64().unwrap_or(0.0);
            let label = if change > 5 && last > 0.0 && (now - last) <= 2_592_000.0 {
                "持续热点"
            } else if (3..=5).contains(&change) && last > 0.0 && (now - last) <= 604_800.0 {
                "新兴热点"
            } else {
                ""
            };
            value["hotspot_score"] = json!((score * 10000.0).round() / 10000.0);
            value["label"] = json!(label);
            value
        })
        .collect();
    results.sort_by(|left, right| {
        right["hotspot_score"]
            .as_f64()
            .partial_cmp(&left["hotspot_score"].as_f64())
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    results.truncate(limit);
    CommandResult::success_json(
        &json!({"workspace_id":workspace_id,"module":module_filter,"results":results}),
        RouteUsed::Local,
    )
}

// ===== 7. 代码度量、负责人和仓库报告 =====

fn metric_python_keyword_count(content: &str, keyword: &str) -> i64 {
    static KEYWORD_PATTERNS: OnceLock<HashMap<&'static str, Regex>> = OnceLock::new();
    let patterns = KEYWORD_PATTERNS.get_or_init(|| {
        [
            "if", "else", "for", "while", "match", "case", "catch", "try", "except", "finally",
            "when", "guard",
        ]
        .iter()
        .copied()
        .map(|keyword| {
            (
                keyword,
                Regex::new(&format!(r"\b{keyword}\b")).expect("valid complexity keyword regex"),
            )
        })
        .collect()
    });
    patterns
        .get(keyword)
        .map(|pattern| pattern.find_iter(content).count() as i64)
        .unwrap_or(0)
}

fn metric_python_boolean_count(content: &str) -> i64 {
    static BOOLEAN_OPERATORS: OnceLock<Regex> = OnceLock::new();
    BOOLEAN_OPERATORS
        .get_or_init(|| Regex::new(r"\b(&&|\|\|)\b").expect("valid complexity regex"))
        .find_iter(content)
        .count() as i64
}

fn metric_python_ternary_count(content: &str) -> i64 {
    static TERNARY: OnceLock<Regex> = OnceLock::new();
    TERNARY
        .get_or_init(|| Regex::new(r"\?\s*[^:]+\s*:").expect("valid ternary regex"))
        .find_iter(content)
        .count() as i64
}

fn metric_python_for_in_count(content: &str) -> i64 {
    static FOR_IN: OnceLock<Regex> = OnceLock::new();
    FOR_IN
        .get_or_init(|| Regex::new(r"\bfor\b.*\bin\b").expect("valid for-in regex"))
        .find_iter(content)
        .count() as i64
}

fn metric_language(path: Option<&str>) -> String {
    let extension = path
        .and_then(|value| value.rsplit_once('.').map(|(_, ext)| ext))
        .unwrap_or("")
        .to_ascii_lowercase();
    match extension.as_str() {
        "py" => "python",
        "rs" => "rust",
        "c" | "h" => "c",
        "java" => "java",
        "ts" | "tsx" => "typescript",
        "js" | "jsx" => "javascript",
        "go" => "go",
        _ => "",
    }
    .to_string()
}

fn metric_complexity(content: &str, language: &str) -> i64 {
    if content.is_empty() {
        return 1;
    }
    let keywords = [
        "if", "else", "for", "while", "match", "case", "catch", "try", "except", "finally", "when",
        "guard",
    ];
    let mut complexity = 1;
    for keyword in keywords {
        complexity += metric_python_keyword_count(content, keyword);
    }
    complexity += metric_python_boolean_count(content);
    if matches!(
        language,
        "rust" | "c" | "java" | "typescript" | "javascript" | "go"
    ) {
        complexity += metric_python_ternary_count(content);
    }
    if language == "python" {
        complexity += metric_python_for_in_count(content);
    }
    complexity
}

fn defect_category_from_rule(rule_id: &str) -> String {
    const KNOWN: [&str; 8] = [
        "security",
        "correctness",
        "best-practice",
        "best-practices",
        "performance",
        "maintainability",
        "portability",
        "accessibility",
    ];
    let parts: Vec<&str> = rule_id.split(['.', '/']).collect();
    for part in &parts {
        if KNOWN.iter().any(|known| known.eq_ignore_ascii_case(part)) {
            return part.to_ascii_lowercase();
        }
    }
    if parts.len() >= 3 {
        parts[2].to_ascii_lowercase()
    } else {
        parts
            .last()
            .copied()
            .unwrap_or("general")
            .to_ascii_lowercase()
    }
}

fn simple_unified_diff(old: &str, new: &str) -> String {
    if old == new {
        return String::new();
    }
    let old_lines: Vec<&str> = old.lines().collect();
    let new_lines: Vec<&str> = new.lines().collect();
    let mut prefix = 0usize;
    while prefix < old_lines.len()
        && prefix < new_lines.len()
        && old_lines[prefix] == new_lines[prefix]
    {
        prefix += 1;
    }
    let mut old_end = old_lines.len();
    let mut new_end = new_lines.len();
    while old_end > prefix && new_end > prefix && old_lines[old_end - 1] == new_lines[new_end - 1] {
        old_end -= 1;
        new_end -= 1;
    }
    let context_start = prefix.saturating_sub(3);
    let context_old_end = (old_end + 3).min(old_lines.len());
    let context_new_end = (new_end + 3).min(new_lines.len());
    let mut diff = format!(
        "--- before\n+++ after\n@@ -{},{} +{},{} @@\n",
        context_start + 1,
        context_old_end.saturating_sub(context_start),
        context_start + 1,
        context_new_end.saturating_sub(context_start)
    );
    for line in &old_lines[context_start..prefix] {
        diff.push(' ');
        diff.push_str(line);
        diff.push('\n');
    }
    for line in &old_lines[prefix..old_end] {
        diff.push('-');
        diff.push_str(line);
        diff.push('\n');
    }
    for line in &new_lines[prefix..new_end] {
        diff.push('+');
        diff.push_str(line);
        diff.push('\n');
    }
    let suffix_start = old_end.min(new_end);
    for line in &new_lines[suffix_start..context_new_end] {
        diff.push(' ');
        diff.push_str(line);
        diff.push('\n');
    }
    diff
}

fn defect_category(rule_id: &str) -> &str {
    for part in rule_id.split(['.', '/']) {
        match part.to_ascii_lowercase().as_str() {
            "security" | "correctness" | "best-practice" | "best-practices" | "performance"
            | "maintainability" | "portability" | "accessibility" => return part,
            _ => {}
        }
    }
    rule_id
        .split(['.', '/'])
        .nth(2)
        .or_else(|| rule_id.rsplit(['.', '/']).next())
        .unwrap_or("general")
}

pub fn run_metrics_summary(runtime: &RuntimeOptions) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(conn) => conn,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let file_count: i64 = match conn.query_row(
        "SELECT COUNT(*) FROM file_instances WHERE workspace_id=?1 AND status != 'archived'",
        [workspace_id],
        |row| row.get(0),
    ) {
        Ok(value) => value,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("metrics file count failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let function_count: i64 = match conn.query_row(
        "SELECT COUNT(*) FROM symbols s JOIN file_instances fi ON s.file_instance_id=fi.id
         WHERE fi.workspace_id=?1 AND fi.status != 'archived' AND s.kind IN ('fn','function','method')",
        [workspace_id],
        |row| row.get(0),
    ) {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, format!("metrics function count failed: {error}"), RouteUsed::Local),
    };
    let total_lines: i64 = conn
        .query_row(
            "SELECT COALESCE(SUM(total_lines),0) FROM file_instances WHERE workspace_id=?1 AND status != 'archived'",
            [workspace_id],
            |row| row.get(0),
        )
        .unwrap_or(0);
    let total_calls: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM calls c JOIN symbols s ON c.caller_id=s.id
             JOIN file_instances fi ON s.file_instance_id=fi.id
             WHERE fi.workspace_id=?1 AND fi.status != 'archived'",
            [workspace_id],
            |row| row.get(0),
        )
        .unwrap_or(0);

    let mut distribution = HashMap::from([
        ("低 (≤5)".to_string(), 0_i64),
        ("中 (6-10)".to_string(), 0_i64),
        ("高 (11-20)".to_string(), 0_i64),
        ("极高 (>20)".to_string(), 0_i64),
    ]);
    let mut total_complexity = 0_i64;
    let mut max_complexity = 0_i64;
    let mut with_content = 0_i64;
    let mut stmt = match conn.prepare(
        "SELECT sc.content, fi.rel_path FROM symbols s
         JOIN file_instances fi ON s.file_instance_id=fi.id
         LEFT JOIN symbol_contents sc ON s.symbol_hash=sc.content_hash
         WHERE fi.workspace_id=?1 AND fi.status != 'archived' AND s.kind IN ('fn','function','method')",
    ) {
        Ok(stmt) => stmt,
        Err(error) => return CommandResult::failure(1, format!("metrics complexity query failed: {error}"), RouteUsed::Local),
    };
    let rows = match stmt.query_map([workspace_id], |row| {
        Ok((
            row.get::<_, Option<String>>(0)?,
            row.get::<_, Option<String>>(1)?,
        ))
    }) {
        Ok(rows) => rows,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("metrics complexity rows failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    for row in rows.flatten() {
        let content = row.0.unwrap_or_default();
        if content.is_empty() {
            continue;
        }
        let complexity = metric_complexity(&content, &metric_language(row.1.as_deref()));
        total_complexity += complexity;
        max_complexity = max_complexity.max(complexity);
        with_content += 1;
        let bucket = if complexity <= 5 {
            "低 (≤5)"
        } else if complexity <= 10 {
            "中 (6-10)"
        } else if complexity <= 20 {
            "高 (11-20)"
        } else {
            "极高 (>20)"
        };
        *distribution.entry(bucket.to_string()).or_default() += 1;
    }
    let commented: i64 = conn
        .query_row(
            "SELECT COALESCE(SUM(CASE WHEN s.has_comment=1 THEN 1 ELSE 0 END),0)
             FROM symbols s JOIN file_instances fi ON s.file_instance_id=fi.id
             WHERE fi.workspace_id=?1 AND s.kind IN ('fn','function','method')",
            [workspace_id],
            |row| row.get(0),
        )
        .unwrap_or(0);
    let comment_coverage = if function_count == 0 {
        0.0
    } else {
        commented as f64 / function_count as f64 * 100.0
    };
    let average = if with_content == 0 {
        0.0
    } else {
        total_complexity as f64 / with_content as f64
    };
    CommandResult::success_json(
        &json!({
            "workspace_id": workspace_id,
            "file_count": file_count,
            "function_count": function_count,
            "total_lines": total_lines,
            "total_calls": total_calls,
            "avg_complexity": (average * 10.0).round() / 10.0,
            "max_complexity": max_complexity,
            "complexity_distribution": distribution,
            "comment_coverage": (comment_coverage * 10.0).round() / 10.0,
            "source": "rust-local-sqlite"
        }),
        RouteUsed::Local,
    )
}

pub fn run_complexity_report(
    runtime: &RuntimeOptions,
    limit: usize,
    module_filter: &str,
) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(conn) => conn,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let mut sql = "SELECT s.qualified_name,s.kind,s.start_line,s.end_line,s.depth,s.module_path,s.signature,sc.content,fi.rel_path
                   FROM symbols s JOIN file_instances fi ON s.file_instance_id=fi.id
                   LEFT JOIN symbol_contents sc ON s.symbol_hash=sc.content_hash
                   WHERE fi.workspace_id=?1 AND s.kind IN ('fn','function','method')".to_string();
    if !module_filter.is_empty() {
        sql.push_str(" AND s.module_path LIKE ?2");
    }
    let mut stmt = match conn.prepare(&sql) {
        Ok(stmt) => stmt,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("complexity query failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let mut records: Vec<(i64, Value)> = Vec::new();
    if module_filter.is_empty() {
        let rows = match stmt.query_map([workspace_id], |row| metric_symbol_value(row, false)) {
            Ok(rows) => rows,
            Err(error) => {
                return CommandResult::failure(
                    1,
                    format!("complexity rows failed: {error}"),
                    RouteUsed::Local,
                )
            }
        };
        for value in rows.flatten() {
            let score = value
                .get("cyclomatic_complexity")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            records.push((score, value));
        }
    } else {
        let rows = match stmt.query_map(params![workspace_id, format!("{module_filter}%")], |row| {
            metric_symbol_value(row, false)
        }) {
            Ok(rows) => rows,
            Err(error) => {
                return CommandResult::failure(
                    1,
                    format!("complexity rows failed: {error}"),
                    RouteUsed::Local,
                )
            }
        };
        for value in rows.flatten() {
            let score = value
                .get("cyclomatic_complexity")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            records.push((score, value));
        }
    }
    records.sort_by(|left, right| right.0.cmp(&left.0));
    records.truncate(limit);
    CommandResult::success_json(
        &json!({"workspace_id":workspace_id,"limit":limit,"functions":records.into_iter().map(|(_, value)| value).collect::<Vec<_>>()}),
        RouteUsed::Local,
    )
}

fn metric_symbol_value(row: &rusqlite::Row<'_>, include_content: bool) -> rusqlite::Result<Value> {
    let qualified_name: String = row.get(0)?;
    let kind: String = row.get(1)?;
    let start_line: i64 = row.get(2)?;
    let end_line: i64 = row.get(3)?;
    let depth: i64 = row.get(4)?;
    let module_path: String = row.get::<_, Option<String>>(5)?.unwrap_or_default();
    let signature: String = row.get::<_, Option<String>>(6)?.unwrap_or_default();
    let content: String = row.get::<_, Option<String>>(7)?.unwrap_or_default();
    let file_path: String = row.get::<_, Option<String>>(8)?.unwrap_or_default();
    let complexity = metric_complexity(&content, &metric_language(Some(&file_path)));
    let mut result = json!({
        "qualified_name": qualified_name,
        "kind": kind,
        "file_path": file_path,
        "start_line": start_line,
        "line_count": (end_line - start_line + 1).max(0),
        "cyclomatic_complexity": complexity,
        "depth": depth.max(0),
        "module_path": module_path,
    });
    if include_content {
        result["signature"] = json!(signature);
    }
    Ok(result)
}

pub fn run_largest_functions(
    runtime: &RuntimeOptions,
    limit: usize,
    module_filter: &str,
) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(conn) => conn,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let mut sql = "SELECT s.qualified_name,s.kind,s.start_line,s.end_line,s.depth,s.module_path,s.signature,'',fi.rel_path
                   FROM symbols s JOIN file_instances fi ON s.file_instance_id=fi.id
                   WHERE fi.workspace_id=?1 AND s.kind IN ('fn','function','method')".to_string();
    if !module_filter.is_empty() {
        sql.push_str(" AND s.module_path LIKE ?2");
    }
    let mut stmt = match conn.prepare(&sql) {
        Ok(stmt) => stmt,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("largest functions query failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let mut functions: Vec<Value> = Vec::new();
    if module_filter.is_empty() {
        let rows = match stmt.query_map([workspace_id], |row| metric_symbol_value(row, false)) {
            Ok(rows) => rows,
            Err(error) => {
                return CommandResult::failure(
                    1,
                    format!("largest functions rows failed: {error}"),
                    RouteUsed::Local,
                )
            }
        };
        functions.extend(rows.flatten());
    } else {
        let rows = match stmt.query_map(params![workspace_id, format!("{module_filter}%")], |row| {
            metric_symbol_value(row, false)
        }) {
            Ok(rows) => rows,
            Err(error) => {
                return CommandResult::failure(
                    1,
                    format!("largest functions rows failed: {error}"),
                    RouteUsed::Local,
                )
            }
        };
        functions.extend(rows.flatten());
    }
    functions.sort_by(|left, right| {
        right
            .get("line_count")
            .and_then(Value::as_i64)
            .cmp(&left.get("line_count").and_then(Value::as_i64))
    });
    functions.truncate(limit);
    CommandResult::success_json(
        &json!({"workspace_id":workspace_id,"functions":functions}),
        RouteUsed::Local,
    )
}

pub fn run_coupling_report(runtime: &RuntimeOptions, limit: usize) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(conn) => conn,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let mut stmt = match conn.prepare("SELECT s.module_path,c.callee_module,COUNT(*) FROM calls c JOIN symbols s ON c.caller_id=s.id JOIN file_instances fi ON s.file_instance_id=fi.id WHERE fi.workspace_id=?1 AND s.module_path!='' AND c.callee_module!='' AND s.module_path!=c.callee_module GROUP BY s.module_path,c.callee_module") { Ok(stmt) => stmt, Err(error) => return CommandResult::failure(1, format!("coupling query failed: {error}"), RouteUsed::Local) };
    let rows = match stmt.query_map([workspace_id], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
        ))
    }) {
        Ok(rows) => rows,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("coupling rows failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let mut afferent: HashMap<String, i64> = HashMap::new();
    let mut efferent: HashMap<String, i64> = HashMap::new();
    for row in rows.flatten() {
        *efferent.entry(row.0.clone()).or_default() += row.2;
        *afferent.entry(row.1.clone()).or_default() += row.2;
    }
    let mut module_names = std::collections::HashSet::<String>::new();
    module_names.extend(afferent.keys().cloned());
    module_names.extend(efferent.keys().cloned());
    let mut modules: Vec<Value> = module_names.into_iter().map(|module| { let aff=*afferent.get(&module).unwrap_or(&0); let eff=*efferent.get(&module).unwrap_or(&0); let total=aff+eff; json!({"module":module,"afferent":aff,"efferent":eff,"total_coupling":total,"instability":if total==0 {0.0} else {(eff as f64/total as f64*100.0).round()/100.0}}) }).collect();
    modules.sort_by(|left, right| {
        right
            .get("total_coupling")
            .and_then(Value::as_i64)
            .cmp(&left.get("total_coupling").and_then(Value::as_i64))
    });
    modules.truncate(limit);
    CommandResult::success_json(
        &json!({"workspace_id":workspace_id,"modules":modules}),
        RouteUsed::Local,
    )
}

pub fn run_coupled_functions(runtime: &RuntimeOptions, limit: usize) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(conn) => conn,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let mut fan_in: HashMap<String, i64> = HashMap::new();
    let mut incoming = match conn.prepare("SELECT callee.qualified_name,COUNT(DISTINCT c.caller_id) FROM calls c JOIN symbols caller ON c.caller_id=caller.id JOIN file_instances caller_fi ON caller.file_instance_id=caller_fi.id JOIN symbols callee ON c.callee_id=callee.id JOIN file_instances callee_fi ON callee.file_instance_id=callee_fi.id WHERE caller_fi.workspace_id=?1 AND callee_fi.workspace_id=?1 AND c.callee_id>0 AND callee.qualified_name!='' GROUP BY c.callee_id,callee.qualified_name") { Ok(stmt)=>stmt, Err(error)=>return CommandResult::failure(1, format!("coupled functions fan-in query failed: {error}"), RouteUsed::Local) };
    if let Ok(rows) = incoming.query_map([workspace_id], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    }) {
        for row in rows.flatten() {
            fan_in.insert(row.0, row.1);
        }
    }
    let mut stmt = match conn.prepare("SELECT s.qualified_name,COUNT(DISTINCT c.callee_name),s.module_path,fi.rel_path FROM symbols s JOIN calls c ON c.caller_id=s.id JOIN file_instances fi ON s.file_instance_id=fi.id WHERE fi.workspace_id=?1 AND s.qualified_name!='' GROUP BY s.qualified_name") { Ok(stmt)=>stmt, Err(error)=>return CommandResult::failure(1, format!("coupled functions query failed: {error}"), RouteUsed::Local) };
    let rows = match stmt.query_map([workspace_id], |row| { let name:String=row.get(0)?; let fan_out:i64=row.get(1)?; let module:String=row.get::<_,Option<String>>(2)?.unwrap_or_default(); let file:String=row.get::<_,Option<String>>(3)?.unwrap_or_default(); let incoming=*fan_in.get(&name).unwrap_or(&0); Ok(json!({"qualified_name":name,"file_path":file,"fan_in":incoming,"fan_out":fan_out,"total_coupling":incoming+fan_out,"module_path":module})) }) { Ok(rows)=>rows, Err(error)=>return CommandResult::failure(1, format!("coupled functions rows failed: {error}"), RouteUsed::Local) };
    let mut functions: Vec<Value> = rows.flatten().collect();
    functions.sort_by(|left, right| {
        right
            .get("total_coupling")
            .and_then(Value::as_i64)
            .cmp(&left.get("total_coupling").and_then(Value::as_i64))
    });
    functions.truncate(limit);
    CommandResult::success_json(
        &json!({"workspace_id":workspace_id,"functions":functions}),
        RouteUsed::Local,
    )
}

pub fn run_function_metrics(runtime: &RuntimeOptions, name: &str) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(conn) => conn,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let row = conn.query_row("SELECT s.id,s.qualified_name,s.kind,s.start_line,s.end_line,s.depth,s.module_path,s.signature,sc.content,fi.rel_path FROM symbols s JOIN file_instances fi ON s.file_instance_id=fi.id LEFT JOIN symbol_contents sc ON s.symbol_hash=sc.content_hash WHERE fi.workspace_id=?1 AND s.qualified_name=?2 LIMIT 1", params![workspace_id,name], |row| Ok((row.get::<_,i64>(0)?,row.get::<_,String>(1)?,row.get::<_,String>(2)?,row.get::<_,i64>(3)?,row.get::<_,i64>(4)?,row.get::<_,i64>(5)?,row.get::<_,Option<String>>(6)?.unwrap_or_default(),row.get::<_,Option<String>>(7)?.unwrap_or_default(),row.get::<_,Option<String>>(8)?.unwrap_or_default(),row.get::<_,Option<String>>(9)?.unwrap_or_default())));
    let row = match row {
        Ok(row) => row,
        Err(rusqlite::Error::QueryReturnedNoRows) => {
            return CommandResult::success_json(
                &json!({"qualified_name":name,"found":false}),
                RouteUsed::Local,
            )
        }
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("function metrics query failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let complexity = metric_complexity(&row.8, &metric_language(Some(&row.9)));
    let fan_in:i64=conn.query_row("SELECT COUNT(DISTINCT c.caller_id) FROM calls c JOIN symbols caller ON c.caller_id=caller.id JOIN file_instances fi ON caller.file_instance_id=fi.id WHERE fi.workspace_id=?1 AND c.callee_id=?2",params![workspace_id,row.0],|r|r.get(0)).unwrap_or(0);
    let fan_out:i64=conn.query_row("SELECT COUNT(DISTINCT c.callee_name) FROM calls c JOIN symbols caller ON c.caller_id=caller.id JOIN file_instances fi ON caller.file_instance_id=fi.id WHERE fi.workspace_id=?1 AND caller.qualified_name=?2",params![workspace_id,name],|r|r.get(0)).unwrap_or(0);
    let risk = if complexity <= 5 {
        "低"
    } else if complexity <= 10 {
        "中"
    } else if complexity <= 20 {
        "高"
    } else {
        "极高"
    };
    CommandResult::success_json(
        &json!({"found":true,"qualified_name":row.1,"kind":row.2,"file_path":row.9,"start_line":row.3,"end_line":row.4,"line_count":(row.4-row.3+1).max(0),"cyclomatic_complexity":complexity,"risk_level":risk,"fan_in":fan_in,"fan_out":fan_out,"depth":row.5.max(0),"module_path":row.6,"signature":row.7}),
        RouteUsed::Local,
    )
}

pub fn run_who(runtime: &RuntimeOptions, file_path: &str) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(conn) => conn,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let normalized = file_path.replace('\\', "/");
    let result = conn.query_row(
        "SELECT fi.rel_path,fo.owner,fo.source,fo.confidence,fo.last_commit_author,fo.last_commit_time,fo.last_commit_hash
         FROM file_ownership fo JOIN file_instances fi ON fo.file_instance_id=fi.id
         WHERE fi.workspace_id=?1 AND (fi.rel_path=?2 OR fi.abs_path=?3) LIMIT 1",
        params![workspace_id, normalized, file_path],
        |row| Ok(json!({
            "file_path": row.get::<_, String>(0)?,
            "owner": row.get::<_, Option<String>>(1)?.unwrap_or_default(),
            "source": row.get::<_, Option<String>>(2)?.unwrap_or_default(),
            "confidence": row.get::<_, Option<f64>>(3)?.unwrap_or(0.0),
            "last_commit_author": row.get::<_, Option<String>>(4)?.unwrap_or_default(),
            "last_commit_time": row.get::<_, Option<f64>>(5)?,
            "last_commit_hash": row.get::<_, Option<String>>(6)?.unwrap_or_default(),
        })),
    );
    match result {
        Ok(value) => {
            CommandResult::success_json(&json!({"found":true,"ownership":value}), RouteUsed::Local)
        }
        Err(rusqlite::Error::QueryReturnedNoRows) => CommandResult::success_json(
            &json!({"found":false,"file_path":file_path}),
            RouteUsed::Local,
        ),
        Err(error) => CommandResult::failure(
            1,
            format!("ownership query failed: {error}"),
            RouteUsed::Local,
        ),
    }
}

pub fn run_ownership_map(runtime: &RuntimeOptions) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(conn) => conn,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let mut stmt = match conn.prepare("SELECT COALESCE(fi.module_path,''),COALESCE(fo.owner,'(未知)') FROM file_ownership fo JOIN file_instances fi ON fo.file_instance_id=fi.id WHERE fi.workspace_id=?1") { Ok(stmt)=>stmt, Err(error)=>return CommandResult::failure(1,format!("ownership map query failed: {error}"),RouteUsed::Local) };
    let rows = match stmt.query_map([workspace_id], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    }) {
        Ok(rows) => rows,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("ownership map rows failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let mut modules: HashMap<String, HashMap<String, i64>> = HashMap::new();
    for row in rows.flatten() {
        *modules
            .entry(if row.0.is_empty() {
                "(未分类)".to_string()
            } else {
                row.0
            })
            .or_default()
            .entry(row.1)
            .or_default() += 1;
    }
    let mut records: Vec<Value> = modules.into_iter().map(|(module,owners)| { let mut owners_vec:Vec<Value>=owners.iter().map(|(name,count)|json!({"name":name,"file_count":count})).collect(); owners_vec.sort_by(|left,right| right["file_count"].as_i64().cmp(&left["file_count"].as_i64())); let file_count:i64=owners.values().sum(); let primary=owners_vec.first().and_then(|v|v["name"].as_str()).unwrap_or("(未知)"); json!({"module":module,"primary_owner":primary,"file_count":file_count,"owners":owners_vec}) }).collect();
    records.sort_by(|left, right| {
        right["file_count"]
            .as_i64()
            .cmp(&left["file_count"].as_i64())
    });
    CommandResult::success_json(
        &json!({"workspace_id":workspace_id,"modules":records}),
        RouteUsed::Local,
    )
}

pub fn run_repo_map(runtime: &RuntimeOptions, format: &str) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(conn) => conn,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(id) => id,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let mut stmt=match conn.prepare("SELECT s.module_path,c.callee_module,COUNT(*) FROM calls c JOIN symbols s ON c.caller_id=s.id JOIN file_instances fi ON s.file_instance_id=fi.id WHERE fi.workspace_id=?1 AND s.module_path!='' AND c.callee_module!='' AND s.module_path!=c.callee_module GROUP BY s.module_path,c.callee_module ORDER BY COUNT(*) DESC") { Ok(stmt)=>stmt, Err(error)=>return CommandResult::failure(1,format!("repository map query failed: {error}"),RouteUsed::Local) };
    let rows = match stmt.query_map([workspace_id], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
        ))
    }) {
        Ok(rows) => rows,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("repository map rows failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let edges: Vec<Value> = rows
        .flatten()
        .map(|row| json!({"caller":row.0,"callee":row.1,"count":row.2}))
        .collect();
    let map = if format == "mermaid" {
        let mut lines = vec!["graph TD".to_string()];
        for edge in &edges {
            let caller = edge["caller"].as_str().unwrap_or("").replace('.', "_");
            let callee = edge["callee"].as_str().unwrap_or("").replace('.', "_");
            lines.push(format!("    {caller} -->|{}| {callee}", edge["count"]));
        }
        lines.join("\n")
    } else {
        let mut lines = vec!["仓库模块依赖图:".to_string(), String::new()];
        for edge in &edges {
            lines.push(format!(
                "  {} → {} ({} 次调用)",
                edge["caller"], edge["callee"], edge["count"]
            ));
        }
        lines.join("\n")
    };
    CommandResult::success_json(
        &json!({"workspace_id":workspace_id,"format":format,"edges":edges,"map":map}),
        RouteUsed::Local,
    )
}

pub fn run_symbol_history(
    runtime: &RuntimeOptions,
    symbol_hash: &str,
    limit: usize,
) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let mut stmt = match conn.prepare("SELECT gc.commit_hash,gsc.change_type,COALESCE(gc.author,''),COALESCE(gc.message,''),COALESCE(gc.timestamp,0) FROM git_symbol_changes gsc JOIN git_commits gc ON gc.commit_hash=gsc.commit_hash WHERE gsc.symbol_hash=?1 AND gc.workspace_id=?2 ORDER BY gc.timestamp DESC LIMIT ?3") { Ok(value) => value, Err(error) => return CommandResult::failure(1, format!("symbol history query failed: {error}"), RouteUsed::Local) };
    let rows = match stmt.query_map(params![symbol_hash, workspace_id, limit as i64], |row| Ok(json!({"commit_hash":row.get::<_,String>(0)?,"change_type":row.get::<_,String>(1)?,"author":row.get::<_,String>(2)?,"message":row.get::<_,String>(3)?,"timestamp":row.get::<_,f64>(4)?}))) { Ok(value) => value, Err(error) => return CommandResult::failure(1, format!("symbol history rows failed: {error}"), RouteUsed::Local) };
    let commits: Vec<Value> = rows.flatten().collect();
    CommandResult::success_json(
        &json!({"symbol_hash":symbol_hash,"change_count":commits.len(),"commits":commits}),
        RouteUsed::Local,
    )
}

pub fn run_test_impact(runtime: &RuntimeOptions, qualified_name: &str) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let mut stmt = match conn.prepare("WITH RECURSIVE callers(id,depth) AS (SELECT s.id,0 FROM symbols s JOIN file_instances fi ON fi.id=s.file_instance_id WHERE fi.workspace_id=?1 AND s.qualified_name=?2 UNION SELECT c.caller_id,callers.depth+1 FROM calls c JOIN callers ON c.callee_id=callers.id JOIN symbols caller_s ON caller_s.id=c.caller_id JOIN file_instances caller_fi ON caller_fi.id=caller_s.file_instance_id WHERE callers.depth<64 AND caller_fi.workspace_id=?1) SELECT DISTINCT s.name,s.qualified_name,COALESCE(s.module_path,''),fi.rel_path,s.start_line FROM callers JOIN symbols s ON s.id=callers.id JOIN file_instances fi ON fi.id=s.file_instance_id WHERE fi.workspace_id=?1 AND (lower(s.name) LIKE '%test%' OR lower(s.name) LIKE '%spec%' OR lower(s.qualified_name) LIKE '%test%' OR lower(s.qualified_name) LIKE '%spec%' OR lower(COALESCE(s.module_path,'')) LIKE '%test%') ORDER BY fi.rel_path,s.start_line") { Ok(value) => value, Err(error) => return CommandResult::failure(1, format!("test impact query failed: {error}"), RouteUsed::Local) };
    let rows = match stmt.query_map(params![workspace_id, qualified_name], |row| Ok(json!({"name":row.get::<_,String>(0)?,"qualified_name":row.get::<_,String>(1)?,"module_path":row.get::<_,String>(2)?,"file_path":row.get::<_,String>(3)?,"start_line":row.get::<_,i64>(4)?}))) { Ok(value) => value, Err(error) => return CommandResult::failure(1, format!("test impact rows failed: {error}"), RouteUsed::Local) };
    let tests: Vec<Value> = rows.flatten().collect();
    CommandResult::success_json(
        &json!({"qualified_name":qualified_name,"count":tests.len(),"tests":tests}),
        RouteUsed::Local,
    )
}

pub fn run_vulnerability_blast(
    runtime: &RuntimeOptions,
    finding_id: i64,
    severity: &str,
    depth: i64,
) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let mut sql = "SELECT sf.id,sf.rule_id,COALESCE(sf.rule_name,''),sf.severity,COALESCE(sf.message,''),COALESCE(sf.symbol_qualified,''),COALESCE(sf.content_hash,''),COALESCE(fi.rel_path,'') FROM semgrep_findings sf JOIN file_instances fi ON fi.id=sf.file_instance_id WHERE fi.workspace_id=?1".to_string();
    if finding_id > 0 {
        sql.push_str(" AND sf.id=?2");
    } else if !severity.is_empty() {
        sql.push_str(" AND sf.severity=?2");
    }
    let mut stmt = match conn.prepare(&sql) {
        Ok(value) => value,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("vulnerability query failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let mut rows_data: Vec<(i64, String, String, String, String, String, String, String)> =
        Vec::new();
    if finding_id > 0 {
        let rows = match stmt.query_map(params![workspace_id, finding_id], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, String>(6)?,
                row.get::<_, String>(7)?,
            ))
        }) {
            Ok(value) => value,
            Err(error) => {
                return CommandResult::failure(
                    1,
                    format!("vulnerability rows failed: {error}"),
                    RouteUsed::Local,
                )
            }
        };
        rows_data.extend(rows.flatten());
    } else if !severity.is_empty() {
        let rows = match stmt.query_map(params![workspace_id, severity], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, String>(6)?,
                row.get::<_, String>(7)?,
            ))
        }) {
            Ok(value) => value,
            Err(error) => {
                return CommandResult::failure(
                    1,
                    format!("vulnerability rows failed: {error}"),
                    RouteUsed::Local,
                )
            }
        };
        rows_data.extend(rows.flatten());
    } else {
        let rows = match stmt.query_map(params![workspace_id], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, String>(6)?,
                row.get::<_, String>(7)?,
            ))
        }) {
            Ok(value) => value,
            Err(error) => {
                return CommandResult::failure(
                    1,
                    format!("vulnerability rows failed: {error}"),
                    RouteUsed::Local,
                )
            }
        };
        rows_data.extend(rows.flatten());
    }
    let mut findings = Vec::new();
    let mut impacted_hashes: HashSet<String> = HashSet::new();
    let mut impacted_caller_count: HashMap<String, i64> = HashMap::new();
    let mut summary_by_layer: HashMap<String, i64> = HashMap::new();
    for row in rows_data {
        let impact = if row.6.is_empty() {
            json!({"total_impacted":0,"by_layer":{}})
        } else {
            match query_local_impact(&conn, workspace_id, &row.6, depth) {
                Ok(value) => value,
                Err(error) => {
                    return CommandResult::failure(
                        1,
                        format!("vulnerability impact failed: {error}"),
                        RouteUsed::Local,
                    )
                }
            }
        };
        if let Some(layer_counts) = impact.get("by_layer").and_then(Value::as_object) {
            for (key, value) in layer_counts {
                if let Some(count) = value.as_i64() {
                    *summary_by_layer.entry(key.clone()).or_insert(0) += count;
                }
            }
        }
        if let Some(layers) = impact.get("layers").and_then(Value::as_array) {
            for layer in layers {
                if let Some(symbols) = layer.get("symbols").and_then(Value::as_array) {
                    for symbol in symbols {
                        if let Some(hash) = symbol
                            .get("symbol_hash")
                            .and_then(Value::as_str)
                            .filter(|v| !v.is_empty())
                        {
                            impacted_hashes.insert(hash.to_string());
                        }
                        if let Some(qn) = symbol
                            .get("qualified_name")
                            .and_then(Value::as_str)
                            .filter(|v| !v.is_empty())
                        {
                            *impacted_caller_count.entry(qn.to_string()).or_insert(0) += 1;
                        }
                    }
                }
            }
        }
        findings.push(json!({"finding_id":row.0,"rule_id":row.1,"rule_name":row.2,"severity":row.3,"message":row.4,"symbol_qualified":row.5,"symbol_hash":row.6,"file_path":row.7,"blast_radius":impact,"impacted_count":impact["total_impacted"]}));
    }
    let impacted = impacted_hashes.len() as i64;
    let has_error = findings.iter().any(|item| {
        matches!(
            item["severity"].as_str(),
            Some("ERROR") | Some("error") | Some("CRITICAL") | Some("critical")
        )
    });
    let risk = if findings.is_empty() {
        "none"
    } else if has_error && impacted > 10 {
        "critical"
    } else if has_error && impacted > 3 {
        "high"
    } else if findings
        .iter()
        .any(|item| matches!(item["severity"].as_str(), Some("WARN") | Some("WARNING")))
    {
        "medium"
    } else {
        "low"
    };
    let mut by_layer = json!({"code":0,"db":0,"api":0,"config":0});
    if let Some(object) = by_layer.as_object_mut() {
        for key in ["code", "db", "api", "config"] {
            if let Some(value) = summary_by_layer.get(key) {
                object.insert(key.to_string(), json!(value));
            }
        }
    }
    let mut high_risk_callers: Vec<Value> = impacted_caller_count
        .into_iter()
        .filter(|(_, count)| *count >= 2)
        .map(|(qualified_name, affected_by_count)| json!({"qualified_name":qualified_name,"affected_by_count":affected_by_count}))
        .collect();
    high_risk_callers.sort_by(|a, b| {
        b["affected_by_count"]
            .as_i64()
            .cmp(&a["affected_by_count"].as_i64())
    });
    high_risk_callers.truncate(20);
    CommandResult::success_json(
        &json!({"total_findings":findings.len(),"total_impacted_symbols":impacted,"risk_level":risk,"findings":findings,"impacted_symbols_summary":{"by_layer":by_layer,"high_risk_callers":high_risk_callers}}),
        RouteUsed::Local,
    )
}

pub fn run_fts(runtime: &RuntimeOptions, action: &str) -> CommandResult {
    let conn = match if action == "rebuild" {
        runtime.open_local_write_db()
    } else {
        runtime.open_local_db()
    } {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let exists: bool = conn
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='symbols_fts')",
            [],
            |row| row.get(0),
        )
        .unwrap_or(false);
    if !exists {
        return CommandResult::failure(
            1,
            "symbols_fts table does not exist".to_string(),
            RouteUsed::Local,
        );
    }
    if action == "rebuild" {
        if let Err(error) =
            conn.execute("INSERT INTO symbols_fts(symbols_fts) VALUES('rebuild')", [])
        {
            return CommandResult::failure(
                1,
                format!("FTS rebuild failed: {error}"),
                RouteUsed::Local,
            );
        }
    }
    let symbols: i64 = conn
        .query_row("SELECT COUNT(*) FROM symbols", [], |row| row.get(0))
        .unwrap_or(0);
    let fts_rows: i64 = conn
        .query_row("SELECT COUNT(*) FROM symbols_fts", [], |row| row.get(0))
        .unwrap_or(0);
    CommandResult::success_json(
        &json!({"action":action,"symbols_count":symbols,"fts_rows":fts_rows,"consistent":symbols==fts_rows}),
        RouteUsed::Local,
    )
}

pub fn run_clone(
    runtime: &RuntimeOptions,
    action: &str,
    file_filter: &str,
    clone_type: Option<i64>,
    min_similarity: f64,
    limit: usize,
    min_lines: usize,
    similarity: f64,
) -> CommandResult {
    let conn = match if action == "clear" {
        runtime.open_local_write_db()
    } else {
        runtime.open_local_db()
    } {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    match action {
        "clear" => {
            let deleted = match conn.execute(
                "DELETE FROM clone_pairs WHERE workspace_id=?1",
                [workspace_id],
            ) {
                Ok(value) => value,
                Err(error) => {
                    return CommandResult::failure(
                        1,
                        format!("clone clear failed: {error}"),
                        RouteUsed::Local,
                    )
                }
            };
            CommandResult::success_json(
                &json!({"deleted":deleted,"workspace_id":workspace_id}),
                RouteUsed::Local,
            )
        }
        "stats" => {
            let totals: (i64,i64,i64,i64)=conn.query_row("SELECT COUNT(*),COALESCE(SUM(clone_type=1),0),COALESCE(SUM(clone_type=2),0),COALESCE(SUM(clone_type=3),0) FROM clone_pairs WHERE workspace_id=?1",[workspace_id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?))).unwrap_or((0,0,0,0));
            let affected:(i64,i64)=conn.query_row("SELECT COUNT(DISTINCT fi.id),COUNT(DISTINCT s.id) FROM clone_pairs cp JOIN symbols s ON cp.symbol_a_id=s.id OR cp.symbol_b_id=s.id JOIN file_instances fi ON s.file_instance_id=fi.id WHERE cp.workspace_id=?1",[workspace_id],|row|Ok((row.get(0)?,row.get(1)?))).unwrap_or((0,0));
            CommandResult::success_json(
                &json!({"total":totals.0,"type1":totals.1,"type2":totals.2,"type3":totals.3,"affected_files":affected.0,"affected_symbols":affected.1}),
                RouteUsed::Local,
            )
        }
        "list" => {
            let type_filter = clone_type.unwrap_or(0);
            let mut stmt = match conn.prepare(
                "SELECT cp.clone_type, cp.similarity, cp.token_hash,
                        cp.lines_a, cp.lines_b, cp.detected_at,
                        sa.name, sa.qualified_name, sa.start_line,
                        sb.name, sb.qualified_name, sb.start_line,
                        fa.rel_path, fb.rel_path
                   FROM clone_pairs cp
                   JOIN symbols sa ON cp.symbol_a_id = sa.id
                   JOIN symbols sb ON cp.symbol_b_id = sb.id
                   JOIN file_instances fa ON sa.file_instance_id = fa.id
                   JOIN file_instances fb ON sb.file_instance_id = fb.id
                  WHERE cp.workspace_id = ?1
                    AND (?2 = 0 OR cp.clone_type = ?2)
                    AND (?3 <= 0 OR cp.similarity >= ?3)
                    AND (?4 = '' OR sa.qualified_name = ?4 OR sb.qualified_name = ?4)
                  ORDER BY cp.similarity DESC, cp.detected_at DESC
                  LIMIT ?5",
            ) {
                Ok(value) => value,
                Err(error) => {
                    return CommandResult::failure(
                        1,
                        format!("clone list query failed: {error}"),
                        RouteUsed::Local,
                    )
                }
            };
            let rows = match stmt.query_map(
                params![
                    workspace_id,
                    type_filter,
                    min_similarity,
                    file_filter,
                    limit as i64
                ],
                |row| {
                    Ok(json!({
                        "clone_type": row.get::<_, i64>(0)?,
                        "similarity": row.get::<_, f64>(1)?,
                        "token_hash": row.get::<_, String>(2)?,
                        "lines_a": row.get::<_, i64>(3)?,
                        "lines_b": row.get::<_, i64>(4)?,
                        "detected_at": row.get::<_, f64>(5)?,
                        "symbol_a_name": row.get::<_, String>(6)?,
                        "symbol_a_qualified": row.get::<_, String>(7)?,
                        "symbol_a_line": row.get::<_, i64>(8)?,
                        "symbol_b_name": row.get::<_, String>(9)?,
                        "symbol_b_qualified": row.get::<_, String>(10)?,
                        "symbol_b_line": row.get::<_, i64>(11)?,
                        "file_a": row.get::<_, String>(12)?,
                        "file_b": row.get::<_, String>(13)?
                    }))
                },
            ) {
                Ok(value) => value,
                Err(error) => {
                    return CommandResult::failure(
                        1,
                        format!("clone list rows failed: {error}"),
                        RouteUsed::Local,
                    )
                }
            };
            CommandResult::success_json(
                &json!({"clones": rows.flatten().collect::<Vec<_>>() }),
                RouteUsed::Local,
            )
        }
        "detect" => {
            let mut stmt = match conn.prepare("SELECT s.id,s.symbol_hash,COALESCE(sc.content,''),s.start_line,s.end_line,fi.rel_path FROM symbols s JOIN file_instances fi ON fi.id=s.file_instance_id LEFT JOIN symbol_contents sc ON sc.content_hash=s.symbol_hash WHERE fi.workspace_id=?1 AND s.kind IN ('fn','function','method') AND (?2='' OR fi.rel_path LIKE ?3)") { Ok(value) => value, Err(error) => return CommandResult::failure(1, format!("clone detect query failed: {error}"), RouteUsed::Local) };
            let filter = format!("%{file_filter}%");
            let rows = match stmt.query_map(params![workspace_id, file_filter, filter], |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, i64>(4)?,
                ))
            }) {
                Ok(value) => value,
                Err(error) => {
                    return CommandResult::failure(
                        1,
                        format!("clone detect rows failed: {error}"),
                        RouteUsed::Local,
                    )
                }
            };
            let mut inputs = Vec::new();
            let mut line_map: HashMap<i64, (i64, i64)> = HashMap::new();
            for row in rows.flatten() {
                let tokens: Vec<String> = row.2.split_whitespace().map(str::to_string).collect();
                if row.4.saturating_sub(row.3) + 1 < min_lines as i64 {
                    continue;
                }
                let normalized = tokens.join(" ");
                let mut hasher = Sha256::new();
                hasher.update(normalized.as_bytes());
                let token_hash = hex::encode(hasher.finalize());
                line_map.insert(row.0, (row.3, row.4));
                inputs.push((row.0, row.1, token_hash, tokens));
            }
            let (groups, stats) = detect_clones_core(inputs, similarity);
            if let Err(error) = conn.execute(
                "DELETE FROM clone_pairs WHERE workspace_id=?1",
                [workspace_id],
            ) {
                return CommandResult::failure(
                    1,
                    format!("clone detect cleanup failed: {error}"),
                    RouteUsed::Local,
                );
            }
            let detected_at = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64();
            let mut pairs = 0_i64;
            for group in &groups {
                if group.members.len() < 2 {
                    continue;
                }
                let first = group.members[0];
                for other in &group.members[1..] {
                    let a = line_map.get(&first).copied().unwrap_or((0, 0));
                    let b = line_map.get(other).copied().unwrap_or((0, 0));
                    if let Err(error)=conn.execute("INSERT OR IGNORE INTO clone_pairs(workspace_id,symbol_a_id,symbol_b_id,clone_type,similarity,token_hash,lines_a,lines_b,detected_at) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",params![workspace_id,first,other,group.clone_type as i64,group.similarity,group.token_hash,a.1-a.0+1,b.1-b.0+1,detected_at]){return CommandResult::failure(1,format!("clone detect write failed: {error}"),RouteUsed::Local);}
                    pairs += 1;
                }
            }
            CommandResult::success_json(
                &json!({"total_pairs":pairs,"scanned_symbols":stats.scanned_symbols,"skipped_symbols":stats.skipped_symbols,"total_groups":stats.total_groups,"type1_groups":stats.type1_groups,"type2_groups":stats.type2_groups,"type3_groups":stats.type3_groups,"similarity_threshold":similarity}),
                RouteUsed::Local,
            )
        }
        _ => CommandResult::failure(
            2,
            format!("unknown clone action: {action}"),
            RouteUsed::Local,
        ),
    }
}

pub fn run_defect(
    runtime: &RuntimeOptions,
    action: &str,
    category: &str,
    severity: &str,
    limit: usize,
    symbol_hash: &str,
    finding: i64,
    commit_hash: &str,
) -> CommandResult {
    let conn = match if matches!(action, "build" | "learn") {
        runtime.open_local_write_db()
    } else {
        runtime.open_local_db()
    } {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    match action {
        /* "search" => { let mut sql="SELECT pattern_id,category,description,detection_rule,severity,case_count FROM defect_patterns WHERE 1=1".to_string(); if !category.is_empty(){sql.push_str(" AND category LIKE ?1");} if !severity.is_empty(){sql.push_str(if category.is_empty(){" AND severity=?1"}else{" AND severity=?2"});} sql.push_str(" ORDER BY case_count DESC LIMIT ?"); let mut stmt=match conn.prepare(&sql){Ok(value)=>value,Err(error)=>return CommandResult::failure(1,format!("defect search failed: {error}"),RouteUsed::Local)}; let rows=if !category.is_empty()&& !severity.is_empty(){stmt.query_map(params![format!("{category}%"),severity,limit as i64],|row|Ok(json!({"pattern_id":row.get::<_,String>(0)?,"category":row.get::<_,String>(1)?,"description":row.get::<_,String>(2)?,"detection_rule":row.get::<_,String>(3)?,"severity":row.get::<_,String>(4)?,"case_count":row.get::<_,i64>(5)?})))}else if !category.is_empty(){stmt.query_map(params![format!("{category}%"),limit as i64],|row|Ok(json!({"pattern_id":row.get::<_,String>(0)?,"category":row.get::<_,String>(1)?,"description":row.get::<_,String>(2)?,"detection_rule":row.get::<_,String>(3)?,"severity":row.get::<_,String>(4)?,"case_count":row.get::<_,i64>(5)?})))}else if !severity.is_empty(){stmt.query_map(params![severity,limit as i64],|row|Ok(json!({"pattern_id":row.get::<_,String>(0)?,"category":row.get::<_,String>(1)?,"description":row.get::<_,String>(2)?,"detection_rule":row.get::<_,String>(3)?,"severity":row.get::<_,String>(4)?,"case_count":row.get::<_,i64>(5)?})))}else{stmt.query_map(params![limit as i64],|row|Ok(json!({"pattern_id":row.get::<_,String>(0)?,"category":row.get::<_,String>(1)?,"description":row.get::<_,String>(2)?,"detection_rule":row.get::<_,String>(3)?,"severity":row.get::<_,String>(4)?,"case_count":row.get::<_,i64>(5)?})))}; match rows{Ok(rows)=>CommandResult::success_json(&json!({"patterns":rows.flatten().collect::<Vec<_>>() }),RouteUsed::Local),Err(error)=>CommandResult::failure(1,format!("defect search rows failed: {error}"),RouteUsed::Local)} } */
        "search" => {
            let mut stmt = match conn.prepare(
                "SELECT pattern_id,category,description,detection_rule,severity,case_count
                   FROM defect_patterns
                  WHERE (?1='' OR category LIKE ?2)
                    AND (?3='' OR severity=?3)
                  ORDER BY case_count DESC LIMIT ?4",
            ) {
                Ok(value) => value,
                Err(error) => {
                    return CommandResult::failure(
                        1,
                        format!("defect search failed: {error}"),
                        RouteUsed::Local,
                    )
                }
            };
            let rows = match stmt.query_map(
                params![category, format!("{category}%"), severity, limit as i64],
                |row| {
                    Ok(json!({
                        "pattern_id": row.get::<_, String>(0)?,
                        "category": row.get::<_, String>(1)?,
                        "description": row.get::<_, String>(2)?,
                        "detection_rule": row.get::<_, String>(3)?,
                        "severity": row.get::<_, String>(4)?,
                        "case_count": row.get::<_, i64>(5)?
                    }))
                },
            ) {
                Ok(value) => value,
                Err(error) => {
                    return CommandResult::failure(
                        1,
                        format!("defect search rows failed: {error}"),
                        RouteUsed::Local,
                    )
                }
            };
            CommandResult::success_json(
                &json!({"patterns": rows.flatten().collect::<Vec<_>>() }),
                RouteUsed::Local,
            )
        }
        "stats" => {
            let patterns: i64 = conn
                .query_row("SELECT COUNT(*) FROM defect_patterns", [], |row| row.get(0))
                .unwrap_or(0);
            let fixes: i64 = conn
                .query_row("SELECT COUNT(*) FROM defect_fixes", [], |row| row.get(0))
                .unwrap_or(0);
            let effectiveness: f64 = conn
                .query_row(
                    "SELECT COALESCE(AVG(effectiveness),0) FROM defect_fixes",
                    [],
                    |row| row.get(0),
                )
                .unwrap_or(0.0);
            CommandResult::success_json(
                &json!({"total_patterns":patterns,"total_fixes":fixes,"avg_effectiveness":effectiveness}),
                RouteUsed::Local,
            )
        }
        "suggest" => {
            let mut stmt=match conn.prepare("SELECT df.pattern_id,COALESCE(dp.fix_template,''),df.effectiveness FROM defect_fixes df LEFT JOIN defect_patterns dp ON dp.pattern_id=df.pattern_id WHERE df.symbol_hash=?1 ORDER BY df.effectiveness DESC LIMIT 20"){Ok(value)=>value,Err(error)=>return CommandResult::failure(1,format!("defect suggest failed: {error}"),RouteUsed::Local)};
            let rows=match stmt.query_map(params![symbol_hash],|row|Ok(json!({"pattern_id":row.get::<_,Option<String>>(0)?.unwrap_or_default(),"fix_template":row.get::<_,String>(1)?,"effectiveness":row.get::<_,f64>(2)?}))){Ok(value)=>value,Err(error)=>return CommandResult::failure(1,format!("defect suggest rows failed: {error}"),RouteUsed::Local)};
            CommandResult::success_json(
                &json!({"symbol_hash":symbol_hash,"finding":finding,"suggestions":rows.flatten().collect::<Vec<_>>() }),
                RouteUsed::Local,
            )
        }
        "build" => {
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64();
            if let Err(error) = conn.execute_batch("BEGIN IMMEDIATE") {
                return CommandResult::failure(
                    1,
                    format!("defect build transaction failed: {error}"),
                    RouteUsed::Local,
                );
            }
            let mut categories: HashMap<String, i64> = HashMap::new();
            let mut stmt = match conn.prepare(
                "SELECT sf.rule_id, MAX(COALESCE(sf.rule_name,'')), MAX(COALESCE(sf.message,'')), MAX(COALESCE(sf.severity,'INFO')), COUNT(*)
                   FROM semgrep_findings sf JOIN file_instances fi ON fi.id=sf.file_instance_id
                  WHERE fi.workspace_id=?1 AND sf.rule_id!=''
                  GROUP BY sf.rule_id",
            ) {
                Ok(value) => value,
                Err(error) => {
                    let _ = conn.execute_batch("ROLLBACK");
                    return CommandResult::failure(1, format!("defect build query failed: {error}"), RouteUsed::Local);
                }
            };
            let rows = match stmt.query_map([workspace_id], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, i64>(4)?,
                ))
            }) {
                Ok(value) => value,
                Err(error) => {
                    let _ = conn.execute_batch("ROLLBACK");
                    return CommandResult::failure(
                        1,
                        format!("defect build rows failed: {error}"),
                        RouteUsed::Local,
                    );
                }
            };
            let mut built = 0_i64;
            for row in rows.flatten() {
                let category = defect_category(&row.0).to_ascii_lowercase();
                let severity = row.3.to_ascii_lowercase();
                let description = if !row.2.trim().is_empty() {
                    row.2.trim().chars().take(500).collect::<String>()
                } else if !row.1.trim().is_empty() {
                    row.1.clone()
                } else {
                    row.0.clone()
                };
                let pattern_id = format!("DP-{}", row.0);
                let existed: bool = conn
                    .query_row(
                        "SELECT EXISTS(SELECT 1 FROM defect_patterns WHERE pattern_id=?1)",
                        [&pattern_id],
                        |value| value.get(0),
                    )
                    .unwrap_or(false);
                if let Err(error) = conn.execute(
                    "INSERT INTO defect_patterns (pattern_id, category, description, detection_rule, fix_template, severity, learned_from, case_count, created_at)
                     VALUES (?1,?2,?3,?4,'',?5,'semgrep',?6,?7)
                     ON CONFLICT(pattern_id) DO UPDATE SET category=excluded.category, description=CASE WHEN defect_patterns.description='' THEN excluded.description ELSE defect_patterns.description END, severity=excluded.severity, case_count=defect_patterns.case_count+excluded.case_count",
                    params![pattern_id, category, description, row.0, severity, row.4, now],
                ) {
                    let _ = conn.execute_batch("ROLLBACK");
                    return CommandResult::failure(1, format!("defect build write failed: {error}"), RouteUsed::Local);
                }
                *categories.entry(category).or_default() += 1;
                if !existed {
                    built += 1;
                }
            }
            if let Err(error) = conn.execute_batch("COMMIT") {
                let _ = conn.execute_batch("ROLLBACK");
                return CommandResult::failure(
                    1,
                    format!("defect build commit failed: {error}"),
                    RouteUsed::Local,
                );
            }
            CommandResult::success_json(
                &json!({"patterns_built":built,"fixes_learned":0,"categories":categories}),
                RouteUsed::Local,
            )
        }
        "learn" => {
            if commit_hash.is_empty() {
                return CommandResult::failure(
                    2,
                    "commit_hash is required".to_string(),
                    RouteUsed::Local,
                );
            }
            if let Err(error) = conn.execute_batch("BEGIN IMMEDIATE") {
                return CommandResult::failure(
                    1,
                    format!("defect learn transaction failed: {error}"),
                    RouteUsed::Local,
                );
            }
            let mut change_stmt = match conn.prepare("SELECT gsc.symbol_hash, COALESCE(gsc.old_content,''), COALESCE(gsc.new_content,'') FROM git_symbol_changes gsc JOIN git_commits gc ON gc.commit_hash=gsc.commit_hash WHERE gsc.commit_hash=?1 AND gsc.change_type='modified' AND gc.workspace_id=?2") {
                Ok(value) => value,
                Err(error) => { let _ = conn.execute_batch("ROLLBACK"); return CommandResult::failure(1, format!("defect learn changes query failed: {error}"), RouteUsed::Local); }
            };
            let changes = match change_stmt.query_map(params![commit_hash, workspace_id], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            }) {
                Ok(value) => value,
                Err(error) => {
                    let _ = conn.execute_batch("ROLLBACK");
                    return CommandResult::failure(
                        1,
                        format!("defect learn changes failed: {error}"),
                        RouteUsed::Local,
                    );
                }
            };
            let mut learned_patterns = 0_i64;
            let mut learned_fixes = 0_i64;
            let mut details = Vec::new();
            for change in changes.flatten() {
                if change.1 == change.2 {
                    continue;
                }
                let mut before_hasher = Sha256::new();
                before_hasher.update(change.1.as_bytes());
                let before_hash = hex::encode(before_hasher.finalize());
                let mut after_hasher = Sha256::new();
                after_hasher.update(change.2.as_bytes());
                let after_hash = hex::encode(after_hasher.finalize());
                let fix_diff = simple_unified_diff(&change.1, &change.2);
                let qname: String = conn
                    .query_row(
                        "SELECT s.qualified_name
                           FROM symbols s
                           JOIN file_instances fi ON fi.id=s.file_instance_id
                          WHERE fi.workspace_id=?2 AND s.symbol_hash=?1 AND s.qualified_name!=''
                          ORDER BY s.id LIMIT 1",
                        params![change.0, workspace_id],
                        |row| row.get(0),
                    )
                    .unwrap_or_default();
                let mut finding_stmt = match conn.prepare("SELECT sf.rule_id, COALESCE(sf.snippet,''), COALESCE(sf.fix,''), COALESCE(sf.message,''), COALESCE(sf.severity,'INFO') FROM semgrep_findings sf JOIN file_instances fi ON fi.id=sf.file_instance_id WHERE fi.workspace_id=?2 AND sf.symbol_qualified=?1 ORDER BY sf.scanned_at DESC LIMIT 1") {
                    Ok(value) => value,
                    Err(error) => { let _ = conn.execute_batch("ROLLBACK"); return CommandResult::failure(1, format!("defect learn findings query failed: {error}"), RouteUsed::Local); }
                };
                let findings = match finding_stmt.query_map(params![qname, workspace_id], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, String>(4)?,
                    ))
                }) {
                    Ok(value) => value,
                    Err(error) => {
                        let _ = conn.execute_batch("ROLLBACK");
                        return CommandResult::failure(
                            1,
                            format!("defect learn findings failed: {error}"),
                            RouteUsed::Local,
                        );
                    }
                };
                let mut related_finding = None;
                for finding_row in findings.flatten() {
                    let snippet = finding_row.1.trim();
                    if !snippet.is_empty()
                        && (!change.1.contains(snippet) || change.2.contains(snippet))
                    {
                        continue;
                    }
                    related_finding = Some(finding_row);
                    break;
                }
                if related_finding.is_none() {
                    related_finding = conn
                        .query_row(
                            "SELECT sf.rule_id, COALESCE(sf.snippet,''), COALESCE(sf.fix,''), COALESCE(sf.message,''), COALESCE(sf.severity,'INFO')
                               FROM semgrep_findings sf
                               JOIN file_instances fi ON fi.id=sf.file_instance_id
                              WHERE fi.workspace_id=?2 AND sf.content_hash=?1
                              ORDER BY sf.scanned_at DESC LIMIT 1",
                            params![before_hash, workspace_id],
                            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?)),
                        )
                        .ok();
                }
                let now = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_secs_f64();
                let mut pattern_id = None;
                let mut related_rule_id = String::new();
                if let Some(finding_row) = related_finding {
                    related_rule_id = finding_row.0.clone();
                    let id = format!("DP-{}", finding_row.0);
                    let category = defect_category_from_rule(&finding_row.0);
                    let description = if finding_row.3.is_empty() {
                        finding_row.0.clone()
                    } else {
                        finding_row.3.clone()
                    };
                    let existed: bool = conn
                        .query_row(
                            "SELECT EXISTS(SELECT 1 FROM defect_patterns WHERE pattern_id=?1)",
                            [&id],
                            |row| row.get(0),
                        )
                        .unwrap_or(false);
                    if let Err(error) = conn.execute(
                        "INSERT OR IGNORE INTO defect_patterns (pattern_id,category,description,detection_rule,fix_template,severity,learned_from,case_count,created_at) VALUES (?1,?2,?3,?4,?5,?6,'git_fix',0,?7)",
                        params![id, category, description, finding_row.0, finding_row.2, finding_row.4.to_ascii_lowercase(), now],
                    ) {
                        let _ = conn.execute_batch("ROLLBACK");
                        return CommandResult::failure(1, format!("defect learn pattern write failed: {error}"), RouteUsed::Local);
                    }
                    conn.execute(
                        "UPDATE defect_patterns SET case_count=case_count+1 WHERE pattern_id=?1",
                        [&id],
                    )
                    .map_err(|error| error.to_string())
                    .unwrap_or(0);
                    if !existed {
                        learned_patterns += 1;
                    }
                    pattern_id = Some(id);
                }
                let duplicate = if let Some(ref id) = pattern_id {
                    conn.query_row(
                        "SELECT EXISTS(SELECT 1 FROM defect_fixes WHERE pattern_id=?1 AND symbol_hash=?2 AND before_hash=?3 AND after_hash=?4)",
                        params![id, change.0, before_hash, after_hash],
                        |row| row.get(0),
                    ).unwrap_or(false)
                } else {
                    false
                };
                if duplicate {
                    details.push(json!({"symbol_hash":change.0,"pattern_id":pattern_id.unwrap_or_default(),"status":"duplicate"}));
                    continue;
                }
                if let Err(error) = conn.execute(
                    "INSERT INTO defect_fixes (pattern_id,symbol_hash,before_hash,after_hash,fix_diff,effectiveness,created_at) VALUES (?1,?2,?3,?4,?5,1.0,?6)",
                    params![pattern_id, change.0, before_hash, after_hash, fix_diff, now],
                ) {
                    let _ = conn.execute_batch("ROLLBACK");
                    return CommandResult::failure(1, format!("defect learn fix write failed: {error}"), RouteUsed::Local);
                }
                learned_fixes += 1;
                details.push(json!({"symbol_hash":change.0,"pattern_id":pattern_id.unwrap_or_default(),"rule_id":related_rule_id,"before_hash":before_hash,"after_hash":after_hash,"status":"learned"}));
            }
            if let Err(error) = conn.execute_batch("COMMIT") {
                let _ = conn.execute_batch("ROLLBACK");
                return CommandResult::failure(
                    1,
                    format!("defect learn commit failed: {error}"),
                    RouteUsed::Local,
                );
            }
            CommandResult::success_json(
                &json!({"commit_hash":commit_hash,"learned_patterns":learned_patterns,"learned_fixes":learned_fixes,"fixes_learned":learned_fixes,"details":details}),
                RouteUsed::Local,
            )
        }
        _ => CommandResult::failure(
            2,
            format!("unknown defect action: {action}"),
            RouteUsed::Local,
        ),
    }
}

pub fn run_comment_coverage(runtime: &RuntimeOptions) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let ws = match runtime.resolve_local_workspace_id(&conn) {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let mut stmt = match conn.prepare("SELECT COALESCE(sc.content,'') FROM symbols s JOIN file_instances fi ON fi.id=s.file_instance_id LEFT JOIN symbol_contents sc ON sc.content_hash=s.symbol_hash WHERE fi.workspace_id=?1 AND s.kind IN ('fn','function','method')") { Ok(value) => value, Err(error) => return CommandResult::failure(1, format!("comment coverage query failed: {error}"), RouteUsed::Local) };
    let rows = match stmt.query_map([ws], |row| row.get::<_, String>(0)) {
        Ok(value) => value,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("comment coverage rows failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let mut total = 0_i64;
    let mut commented = 0_i64;
    for content in rows.flatten() {
        total += 1;
        if content.lines().any(|line| {
            let s = line.trim_start();
            s.starts_with("//") || s.starts_with('#') || s.starts_with("/*") || s.starts_with('*')
        }) {
            commented += 1;
        }
    }
    let rate = if total == 0 {
        0.0
    } else {
        commented as f64 / total as f64
    };
    CommandResult::success_json(
        &json!({"total_functions":total,"commented_functions":commented,"commented_rate":rate}),
        RouteUsed::Local,
    )
}

pub fn run_uncommented(runtime: &RuntimeOptions) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let ws = match runtime.resolve_local_workspace_id(&conn) {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let mut stmt=match conn.prepare("SELECT s.name,s.qualified_name,fi.rel_path,s.start_line,COALESCE(sc.content,'') FROM symbols s JOIN file_instances fi ON fi.id=s.file_instance_id LEFT JOIN symbol_contents sc ON sc.content_hash=s.symbol_hash WHERE fi.workspace_id=?1 AND s.kind IN ('fn','function','method')"){Ok(value)=>value,Err(error)=>return CommandResult::failure(1,format!("uncommented query failed: {error}"),RouteUsed::Local)};
    let rows = match stmt.query_map([ws], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, i64>(3)?,
            row.get::<_, String>(4)?,
        ))
    }) {
        Ok(value) => value,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("uncommented rows failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let symbols:Vec<Value>=rows.flatten().filter_map(|row|{let has_comment=row.4.lines().any(|line|{let s=line.trim_start();s.starts_with("//")||s.starts_with('#')||s.starts_with("/*")||s.starts_with('*')});if has_comment{None}else{Some(json!({"name":row.0,"qualified_name":row.1,"file_path":row.2,"start_line":row.3}))}}).collect();
    CommandResult::success_json(
        &json!({"count":symbols.len(),"symbols":symbols}),
        RouteUsed::Local,
    )
}

pub fn run_function_issues(runtime: &RuntimeOptions) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let ws = match runtime.resolve_local_workspace_id(&conn) {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let mut stmt=match conn.prepare("SELECT sf.rule_id,sf.severity,sf.message,COALESCE(sf.symbol_qualified,''),fi.rel_path,sf.start_line FROM semgrep_findings sf JOIN file_instances fi ON fi.id=sf.file_instance_id WHERE fi.workspace_id=?1 ORDER BY sf.start_line"){Ok(value)=>value,Err(error)=>return CommandResult::failure(1,format!("function issues query failed: {error}"),RouteUsed::Local)};
    let rows=match stmt.query_map([ws],|row|Ok(json!({"rule_id":row.get::<_,String>(0)?,"severity":row.get::<_,String>(1)?,"message":row.get::<_,String>(2)?,"symbol_qualified":row.get::<_,String>(3)?,"file_path":row.get::<_,String>(4)?,"start_line":row.get::<_,i64>(5)?}))){Ok(value)=>value,Err(error)=>return CommandResult::failure(1,format!("function issues rows failed: {error}"),RouteUsed::Local)};
    CommandResult::success_json(
        &json!({"issues":rows.flatten().collect::<Vec<_>>() }),
        RouteUsed::Local,
    )
}

pub fn run_brief(runtime: &RuntimeOptions) -> CommandResult {
    let metrics = run_metrics_summary(runtime);
    if metrics.exit_code != 0 {
        return metrics;
    }
    let conn = match runtime.open_local_db() {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let value: Value = serde_json::from_str(&metrics.stdout).unwrap_or_else(|_| json!({}));
    let mut ext_counts: HashMap<String, i64> = HashMap::new();
    if let Ok(mut stmt) = conn.prepare(
        "SELECT rel_path FROM file_instances WHERE workspace_id=?1 AND status!='archived' AND rel_path LIKE '%.%'",
    ) {
        if let Ok(rows) = stmt.query_map([workspace_id], |row| row.get::<_, String>(0)) {
            for path in rows.flatten() {
                if let Some((_, ext)) = path.rsplit_once('.') {
                    *ext_counts.entry(ext.to_ascii_lowercase()).or_default() += 1;
                }
            }
        }
    }
    let (top_ext, _) = ext_counts
        .iter()
        .max_by_key(|(_, count)| **count)
        .map(|(ext, count)| (ext.as_str(), *count))
        .unwrap_or(("", 0));
    let project_type = match top_ext {
        "rs" => "Rust",
        "py" => "Python",
        "ts" | "tsx" => "TypeScript",
        "js" | "jsx" => "JavaScript",
        "go" => "Go",
        "java" => "Java",
        "c" => "C",
        "cpp" => "C++",
        "h" => "C/C++",
        "" => "Unknown",
        _ => "Multi-language",
    };
    let mut modules = Vec::new();
    if let Ok(mut stmt) = conn.prepare(
        "SELECT s.module_path, COUNT(*) FROM symbols s JOIN file_instances fi ON fi.id=s.file_instance_id WHERE fi.workspace_id=?1 AND s.module_path!='' AND s.kind IN ('fn','function','method') GROUP BY s.module_path ORDER BY COUNT(*) DESC LIMIT 20",
    ) {
        if let Ok(rows) = stmt.query_map([workspace_id], |row| {
            Ok(json!({"module": row.get::<_, String>(0)?, "function_count": row.get::<_, i64>(1)?}))
        }) {
            modules.extend(rows.flatten());
        }
    }
    let hotspots = run_hotspot_report(runtime, "", 10);
    if hotspots.exit_code != 0 {
        return hotspots;
    }
    let hotspot_value: Value = serde_json::from_str(&hotspots.stdout).unwrap_or_else(|_| json!({}));
    let health = run_health_report(runtime);
    if health.exit_code != 0 {
        return health;
    }
    let health_value: Value = serde_json::from_str(&health.stdout).unwrap_or_else(|_| json!({}));
    CommandResult::success_json(
        &json!({
            "project_type": project_type,
            "file_count": value["file_count"],
            "function_count": value["function_count"],
            "total_lines": value["total_lines"],
            "modules": modules,
            "hot_functions": hotspot_value["results"],
            "health_score": health_value["health_score"],
            "health_level": health_value["health_level"],
            "avg_complexity": value["avg_complexity"],
            "comment_coverage": value["comment_coverage"]
        }),
        RouteUsed::Local,
    )
}

pub fn run_health_report(runtime: &RuntimeOptions) -> CommandResult {
    let conn = match runtime.open_local_db() {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let ws = match runtime.resolve_local_workspace_id(&conn) {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let mut high = 0_i64;
    let mut medium = 0_i64;
    let mut low = 0_i64;

    if let Ok(mut stmt) = conn.prepare(
        "SELECT total_lines FROM file_instances
         WHERE workspace_id=?1 AND total_lines > 0
         ORDER BY total_lines DESC LIMIT 20",
    ) {
        if let Ok(rows) = stmt.query_map([ws], |row| row.get::<_, i64>(0)) {
            for lines in rows.flatten() {
                if lines >= 2000 {
                    high += 1;
                } else if lines >= 1000 {
                    medium += 1;
                } else if lines >= 500 {
                    low += 1;
                }
            }
        }
    }

    let mut function_metrics: Vec<(i64, i64)> = Vec::new();
    if let Ok(mut stmt) = conn.prepare(
        "SELECT s.start_line,s.end_line,sc.content,fi.rel_path
         FROM symbols s
         JOIN file_instances fi ON fi.id=s.file_instance_id
         LEFT JOIN symbol_contents sc ON sc.content_hash=s.symbol_hash
         WHERE fi.workspace_id=?1
           AND s.kind IN ('fn','function','method')",
    ) {
        if let Ok(rows) = stmt.query_map([ws], |row| {
            let start = row.get::<_, i64>(0)?;
            let end = row.get::<_, i64>(1)?;
            let content = row.get::<_, Option<String>>(2)?.unwrap_or_default();
            let path = row.get::<_, Option<String>>(3)?.unwrap_or_default();
            Ok((
                metric_complexity(&content, &metric_language(Some(&path))),
                (end - start + 1).max(0),
            ))
        }) {
            function_metrics.extend(rows.flatten());
        }
    }

    let mut complexity_values: Vec<i64> = function_metrics.iter().map(|item| item.0).collect();
    complexity_values.sort_by(|left, right| right.cmp(left));
    complexity_values.truncate(30);
    for complexity in complexity_values {
        if complexity >= 30 {
            high += 1;
        } else if complexity >= 20 {
            medium += 1;
        } else if complexity >= 10 {
            low += 1;
        }
    }

    let mut line_values: Vec<i64> = function_metrics.iter().map(|item| item.1).collect();
    line_values.sort_by(|left, right| right.cmp(left));
    line_values.truncate(30);
    for lines in line_values {
        if lines >= 200 {
            high += 1;
        } else if lines >= 100 {
            medium += 1;
        } else if lines >= 50 {
            low += 1;
        }
    }

    let mut stmt = match conn.prepare(
        "SELECT s.module_path,c.callee_module,COUNT(*)
         FROM calls c
         JOIN symbols s ON c.caller_id=s.id
         JOIN file_instances fi ON s.file_instance_id=fi.id
         WHERE fi.workspace_id=?1 AND s.module_path!=''
           AND c.callee_module!='' AND s.module_path!=c.callee_module
         GROUP BY s.module_path,c.callee_module",
    ) {
        Ok(stmt) => stmt,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("health coupling query failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let coupling_rows = match stmt.query_map([ws], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
        ))
    }) {
        Ok(rows) => rows,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("health coupling rows failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let mut afferent: HashMap<String, i64> = HashMap::new();
    let mut efferent: HashMap<String, i64> = HashMap::new();
    for row in coupling_rows.flatten() {
        *efferent.entry(row.0).or_default() += row.2;
        *afferent.entry(row.1).or_default() += row.2;
    }
    let mut coupling_values: Vec<(i64, f64)> = afferent
        .keys()
        .chain(efferent.keys())
        .collect::<std::collections::HashSet<_>>()
        .into_iter()
        .map(|module| {
            let aff = *afferent.get(module).unwrap_or(&0);
            let eff = *efferent.get(module).unwrap_or(&0);
            let total = aff + eff;
            let instability = if total == 0 {
                0.0
            } else {
                (eff as f64 / total as f64 * 100.0).round() / 100.0
            };
            (total, instability)
        })
        .collect();
    coupling_values.sort_by(|left, right| right.0.cmp(&left.0));
    coupling_values.truncate(20);
    for (total, instability) in coupling_values {
        if instability >= 0.9 {
            high += 1;
        } else if instability >= 0.7 {
            medium += 1;
        } else if total >= 50 {
            low += 1;
        }
    }

    let score = (100.0 - high as f64 * 5.0 - medium as f64 * 2.0 - low as f64 * 0.5).max(0.0);
    let level = if score >= 80.0 {
        "good"
    } else if score >= 60.0 {
        "fair"
    } else if score >= 40.0 {
        "poor"
    } else {
        "bad"
    };
    CommandResult::success_json(
        &json!({
            "health_score":(score * 10.0).round() / 10.0,
            "health_level":level,
            "high_count":high,
            "medium_count":medium,
            "low_count":low,
            "high_issue_count":high,
            "medium_issue_count":medium,
            "low_issue_count":low,
            "total_issue_count":high + medium + low
        }),
        RouteUsed::Local,
    )
}

pub fn run_graph(runtime: &RuntimeOptions) -> CommandResult {
    run_repo_map(runtime, "text")
}
pub fn run_dashboard(
    runtime: &RuntimeOptions,
    full: bool,
    with_cycles: bool,
    with_evolution: bool,
    include_risks: bool,
    top: usize,
    _json: bool,
) -> CommandResult {
    let metrics = run_metrics_summary(runtime);
    if metrics.exit_code != 0 {
        return metrics;
    }
    let health = run_health_report(runtime);
    if health.exit_code != 0 {
        return health;
    }
    let metrics_value: Value = serde_json::from_str(&metrics.stdout).unwrap_or_else(|_| json!({}));
    let health_value: Value = serde_json::from_str(&health.stdout).unwrap_or_else(|_| json!({}));
    let conn = match runtime.open_local_db() {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let stats = match query_local_stats(&conn, workspace_id) {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let (workspace_name, root_path): (String, String) = conn
        .query_row(
            "SELECT COALESCE(name,''),COALESCE(root_path,'') FROM workspaces WHERE id=?1",
            [workspace_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap_or_else(|_| ("unknown".to_string(), String::new()));
    let total_calls = stats["total_calls"].as_i64().unwrap_or(0);
    let resolved_calls = stats["resolved_calls"].as_i64().unwrap_or(0);
    let commented_symbols: i64 = conn
        .query_row(
            "SELECT COALESCE(SUM(CASE WHEN s.has_comment=1 THEN 1 ELSE 0 END),0)
             FROM symbols s JOIN file_instances fi ON fi.id=s.file_instance_id
             WHERE fi.workspace_id=?1 AND fi.status!='archived'",
            [workspace_id],
            |row| row.get(0),
        )
        .unwrap_or(0);
    let mut by_language: HashMap<String, i64> = HashMap::new();
    if let Ok(mut stmt) = conn.prepare(
        "SELECT rel_path,COUNT(*) FROM file_instances
         WHERE workspace_id=?1 AND status!='archived' GROUP BY rel_path",
    ) {
        if let Ok(rows) = stmt.query_map([workspace_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        }) {
            for row in rows.flatten() {
                let language = row
                    .0
                    .rsplit_once('.')
                    .map(|(_, ext)| ext.to_ascii_lowercase())
                    .unwrap_or_else(|| "(no extension)".to_string());
                *by_language.entry(language).or_default() += row.1;
            }
        }
    }
    let mut largest_fns = Vec::new();
    if let Ok(mut stmt) = conn.prepare(
        "SELECT s.qualified_name,s.start_line,s.end_line,fi.rel_path
         FROM symbols s JOIN file_instances fi ON fi.id=s.file_instance_id
         WHERE fi.workspace_id=?1 AND s.kind IN ('fn','function','method')
         ORDER BY (s.end_line-s.start_line) DESC LIMIT ?2",
    ) {
        if let Ok(rows) = stmt.query_map(params![workspace_id, top as i64], |row| {
            let start: i64 = row.get(1)?;
            let end: i64 = row.get(2)?;
            Ok(json!({
                "qualified_name": row.get::<_, String>(0)?,
                "file_path": row.get::<_, String>(3)?,
                "start_line": start,
                "line_count": (end-start+1).max(0)
            }))
        }) {
            largest_fns.extend(rows.flatten());
        }
    }
    let mut cycles_count = Value::Null;
    if with_cycles {
        if let Ok(db_path) =
            conn.query_row("PRAGMA database_list", [], |row| row.get::<_, String>(2))
        {
            let mut store = GraphStore::new();
            if store
                .load_from_sqlite_readonly_blocking(&db_path, workspace_id)
                .is_ok()
            {
                cycles_count = json!(store.detect_cycles_rust().len());
            }
        }
    }
    let orphans_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM file_symbol_versions fsv
             JOIN file_versions fv ON fsv.file_version_id=fv.id
             JOIN file_instances fi ON fv.file_instance_id=fi.id
             JOIN symbol_contents sc ON fsv.symbol_hash=sc.content_hash
             WHERE fi.workspace_id=?1 AND fv.is_current=1 AND sc.kind='fn'
               AND fsv.qualified_name NOT IN (
                 SELECT DISTINCT cv.callee_qualified FROM call_versions cv
                 JOIN file_versions fv2 ON cv.file_version_id=fv2.id
                 JOIN file_instances fi2 ON fv2.file_instance_id=fi2.id
                 WHERE fi2.workspace_id=?1 AND fv2.is_current=1
                   AND cv.callee_qualified!='' AND cv.caller_qualified!=''
               )",
            [workspace_id],
            |row| row.get(0),
        )
        .unwrap_or(0);
    let call_graph = json!({
        "total_calls": total_calls,
        "resolved_calls": resolved_calls,
        "resolve_rate_pct": if total_calls == 0 { 0.0 } else { (resolved_calls as f64 / total_calls as f64 * 1000.0).round() / 10.0 },
        "cross_file_calls": stats["cross_file_calls"],
        "cycles_count": cycles_count,
        "orphans_count": orphans_count,
        "depth_distribution": stats["depth_distribution"]
    });
    let quality = json!({
        "avg_complexity": if full { metrics_value["avg_complexity"].clone() } else { Value::Null },
        "max_complexity": if full { metrics_value["max_complexity"].clone() } else { Value::Null },
        "complexity_distribution": if full { metrics_value["complexity_distribution"].clone() } else { Value::Null },
        "comment_coverage_pct": metrics_value["comment_coverage"],
        "uncommented_fns": metrics_value["function_count"].as_i64().unwrap_or(0).saturating_sub(commented_symbols),
        "largest_fns_top": largest_fns,
        "complexity_hotspots_top": if full {
            serde_json::from_str::<Value>(&run_complexity_report(runtime, top, "").stdout)
                .ok()
                .and_then(|value| value.get("functions").cloned())
                .unwrap_or_else(|| json!([]))
        } else {
            json!([])
        },
        "quick_mode": !full
    });
    let bootstrap = match bootstrap_status(&conn) {
        Ok(value) => value,
        Err(error) => {
            return CommandResult::failure(
                1,
                format!("dashboard bootstrap failed: {error}"),
                RouteUsed::Local,
            )
        }
    };
    let task_risk = json!({
        "task_counts": bootstrap.tasks,
        "open_findings_count": bootstrap.open_findings_count,
        "blocking_findings_count": bootstrap.blocking_findings_count,
        "pending_rule_candidates": bootstrap.pending_candidates_count,
        "recommended_action": bootstrap.recommended_next_action
    });
    let audit = json!({
        "active_rules_count": bootstrap.active_rules_count,
        "audit_broken_count": bootstrap.audit_verify.broken_count,
        "audit_verified_count": bootstrap.audit_verify.verified_count,
        "latest_scan_run": bootstrap.latest_scan_run
    });
    let evolution = if with_evolution {
        let commits = serde_json::from_str::<Value>(&run_git_log(runtime, top).stdout)
            .ok()
            .and_then(|value| value.get("commits").cloned())
            .unwrap_or_else(|| json!([]));
        let churn = serde_json::from_str::<Value>(&run_churn_report(runtime, "", "30d").stdout)
            .unwrap_or_else(|_| json!({}));
        let hotspot_top =
            serde_json::from_str::<Value>(&run_hotspot_report(runtime, "", top).stdout)
                .ok()
                .and_then(|value| value.get("results").cloned())
                .unwrap_or_else(|| json!([]));
        json!({"recent_commits":commits,"churn_30d":churn,"hotspot_top":hotspot_top})
    } else {
        Value::Null
    };
    let risks = if include_risks {
        let mut values = Vec::new();
        if full {
            if let Ok(value) =
                serde_json::from_str::<Value>(&run_complexity_report(runtime, top * 3, "").stdout)
            {
                if let Some(functions) = value.get("functions").and_then(Value::as_array) {
                    for function in functions {
                        let complexity = function["cyclomatic_complexity"].as_i64().unwrap_or(0);
                        if complexity > 20 {
                            values.push(json!({
                                "type":"high_complexity",
                                "severity":if complexity > 30 {"high"} else {"medium"},
                                "qualified_name":function["qualified_name"],
                                "file_path":function["file_path"],
                                "detail":format!("圈复杂度 {}（建议 < 20）", complexity)
                            }));
                        }
                    }
                }
            }
        }
        if let Ok(mut stmt) = conn.prepare(
            "SELECT s.qualified_name,s.start_line,s.end_line,fi.rel_path
             FROM symbols s JOIN file_instances fi ON s.file_instance_id=fi.id
             WHERE fi.workspace_id=?1 AND s.kind IN ('fn','function','method')
               AND s.start_line>0 AND s.end_line>0
               AND (s.end_line-s.start_line+1)>500 ORDER BY (s.end_line-s.start_line) DESC LIMIT ?2",
        ) {
            if let Ok(rows) = stmt.query_map(params![workspace_id, (top * 3) as i64], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?, row.get::<_, i64>(2)?, row.get::<_, String>(3)?))
            }) {
                for row in rows.flatten() {
                    let lines = row.2 - row.1 + 1;
                    values.push(json!({
                        "type":"oversized_function",
                        "severity":if lines > 1000 {"high"} else {"medium"},
                        "qualified_name":row.0,
                        "file_path":row.3,
                        "detail":format!("函数 {} 行（建议 < 500）", lines)
                    }));
                }
            }
        }
        if let Ok(mut stmt) = conn.prepare(
            "SELECT task_id,step_id,severity,message FROM task_quality_findings
             WHERE status='open' AND severity='block' LIMIT ?1",
        ) {
            if let Ok(rows) = stmt.query_map([top as i64], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                ))
            }) {
                for row in rows.flatten() {
                    values.push(json!({"type":"blocking_finding","severity":"high","task_id":row.0,"step_id":row.1,"detail":if row.3.is_empty() {"阻塞 findings 未解决".to_string()} else {row.3}}));
                }
            }
        }
        if bootstrap.audit_verify.broken_count > 0 {
            values.push(json!({"type":"broken_audit","severity":"high","detail":format!("审计链有 {} 条损坏记录（cw audit verify 查看）", bootstrap.audit_verify.broken_count)}));
        }
        if bootstrap.db_stale {
            values.push(json!({"type":"db_stale","severity":"medium","detail":"DB 滞后于 git HEAD，建议 cw --refresh-all"}));
        }
        values.truncate(top * 3);
        json!(values)
    } else {
        Value::Null
    };
    CommandResult::success_json(
        &json!({
            "overview":{"workspace_id":workspace_id,"workspace_name":workspace_name,"root_path":root_path,"db_path":runtime.db_path.to_string_lossy()},
            "code_scale":{"total_files":metrics_value["file_count"],"total_lines":metrics_value["total_lines"],"total_symbols":stats["total_symbols"],"total_function_versions":stats["total_file_symbol_links"],"by_kind":stats["by_kind"],"by_language":by_language,"commented_symbols":commented_symbols},
            "code_quality":quality,
            "call_graph":call_graph,
            "task_risk":task_risk,
            "audit":audit,
            "evolution":evolution,
            "risks":risks,
            "health":health_value
        }),
        RouteUsed::Local,
    )
}
pub fn run_rollback(
    runtime: &RuntimeOptions,
    action: &str,
    task_id: &str,
    feature_name: &str,
    phase: i64,
    production_entry: &str,
    rollback_entry: &str,
    window: &str,
    config_json: &str,
    flag: i64,
    reason: &str,
) -> CommandResult {
    let conn = match if matches!(action, "register" | "set") {
        runtime.open_local_write_db()
    } else {
        runtime.open_local_db()
    } {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let workspace_id = match runtime.resolve_local_workspace_id(&conn) {
        Ok(value) => value,
        Err(error) => return CommandResult::failure(1, error, RouteUsed::Local),
    };
    let exists: bool = conn.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='rollback_config')", [], |row| row.get(0)).unwrap_or(false);
    if !exists {
        return CommandResult::failure(
            1,
            "rollback_config table does not exist".to_string(),
            RouteUsed::Local,
        );
    }
    match action {
        "register" => {
            if task_id.is_empty()
                || feature_name.is_empty()
                || production_entry.is_empty()
                || rollback_entry.is_empty()
            {
                return CommandResult::failure(
                    2,
                    "task_id, feature, production_entry and rollback_entry are required"
                        .to_string(),
                    RouteUsed::Local,
                );
            }
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64();
            let existing: Option<(i64, i64)> = conn.query_row(
                "SELECT id, rollback_flag FROM rollback_config WHERE task_id=?1 AND (workspace_id=?2 OR workspace_id IS NULL) ORDER BY id DESC LIMIT 1",
                params![task_id, workspace_id], |row| Ok((row.get(0)?, row.get(1)?)),
            ).ok();
            if let Some((id, old_flag)) = existing {
                if let Err(error) = conn.execute(
                    "UPDATE rollback_config SET feature_name=?1, phase=?2, production_entry=?3, rollback_entry=?4, rollback_window_until=?5, config_blob=?6, updated_at=?7 WHERE id=?8",
                    params![feature_name, phase, production_entry, rollback_entry, window, config_json, now, id],
                ) {
                    return CommandResult::failure(1, format!("rollback register update failed: {error}"), RouteUsed::Local);
                }
                return CommandResult::success_json(
                    &json!({"success":true,"id":id,"action":"updated","task_id":task_id,"rollback_flag":old_flag}),
                    RouteUsed::Local,
                );
            }
            let id = match conn.execute(
                "INSERT INTO rollback_config (workspace_id, task_id, feature_name, phase, production_entry, rollback_entry, rollback_flag, rollback_window_until, config_blob, created_at, updated_at) VALUES (?1,?2,?3,?4,?5,?6,0,?7,?8,?9,?9)",
                params![workspace_id, task_id, feature_name, phase, production_entry, rollback_entry, window, config_json, now],
            ) {
                Ok(_) => conn.last_insert_rowid(),
                Err(error) => return CommandResult::failure(1, format!("rollback register failed: {error}"), RouteUsed::Local),
            };
            CommandResult::success_json(
                &json!({"success":true,"id":id,"action":"inserted","task_id":task_id,"rollback_flag":0}),
                RouteUsed::Local,
            )
        }
        "show" => {
            let row = conn.query_row(
                "SELECT id, workspace_id, task_id, feature_name, phase, production_entry, rollback_entry, rollback_flag, rollback_window_until, config_blob, created_at, updated_at FROM rollback_config WHERE task_id=?1 AND (workspace_id=?2 OR workspace_id IS NULL) ORDER BY id DESC LIMIT 1",
                params![task_id, workspace_id], |row| Ok(json!({
                    "id": row.get::<_, i64>(0)?, "workspace_id": row.get::<_, Option<i64>>(1)?, "task_id": row.get::<_, String>(2)?, "feature_name": row.get::<_, String>(3)?, "phase": row.get::<_, i64>(4)?, "production_entry": row.get::<_, String>(5)?, "rollback_entry": row.get::<_, String>(6)?, "rollback_flag": row.get::<_, i64>(7)?, "rollback_window_until": row.get::<_, Option<String>>(8)?, "config_blob": row.get::<_, String>(9)?, "created_at": row.get::<_, f64>(10)?, "updated_at": row.get::<_, f64>(11)?
                })),
            );
            match row {
                Ok(value) => CommandResult::success_json(&value, RouteUsed::Local),
                Err(rusqlite::Error::QueryReturnedNoRows) => CommandResult::failure(
                    1,
                    format!("rollback_config not found for task_id={task_id}"),
                    RouteUsed::Local,
                ),
                Err(error) => CommandResult::failure(
                    1,
                    format!("rollback show failed: {error}"),
                    RouteUsed::Local,
                ),
            }
        }
        "config" => {
            let mut stmt = match conn.prepare(
                "SELECT id, workspace_id, task_id, feature_name, phase, production_entry, rollback_entry, rollback_flag, rollback_window_until, config_blob, created_at, updated_at FROM rollback_config WHERE (workspace_id=?1 OR workspace_id IS NULL) AND (?2=0 OR phase=?2) AND (?3<0 OR rollback_flag=?3) ORDER BY phase ASC, feature_name ASC",
            ) { Ok(value) => value, Err(error) => return CommandResult::failure(1, format!("rollback config query failed: {error}"), RouteUsed::Local) };
            let rows = match stmt.query_map(params![workspace_id, phase, flag], |row| Ok(json!({
                "id": row.get::<_, i64>(0)?, "workspace_id": row.get::<_, Option<i64>>(1)?, "task_id": row.get::<_, String>(2)?, "feature_name": row.get::<_, String>(3)?, "phase": row.get::<_, i64>(4)?, "production_entry": row.get::<_, String>(5)?, "rollback_entry": row.get::<_, String>(6)?, "rollback_flag": row.get::<_, i64>(7)?, "rollback_window_until": row.get::<_, Option<String>>(8)?, "config_blob": row.get::<_, String>(9)?, "created_at": row.get::<_, f64>(10)?, "updated_at": row.get::<_, f64>(11)?
            }))) { Ok(value) => value, Err(error) => return CommandResult::failure(1, format!("rollback config rows failed: {error}"), RouteUsed::Local) };
            CommandResult::success_json(
                &json!({"configs": rows.flatten().collect::<Vec<_>>() }),
                RouteUsed::Local,
            )
        }
        "set" => {
            if flag != 0 && flag != 1 {
                return CommandResult::failure(
                    2,
                    "rollback flag must be 0 or 1".to_string(),
                    RouteUsed::Local,
                );
            }
            let old: Option<(String, i64)> = conn.query_row(
                "SELECT feature_name, rollback_flag FROM rollback_config WHERE task_id=?1 AND (workspace_id=?2 OR workspace_id IS NULL) ORDER BY id DESC LIMIT 1",
                params![task_id, workspace_id], |row| Ok((row.get(0)?, row.get(1)?)),
            ).ok();
            let Some((feature, old_flag)) = old else {
                return CommandResult::failure(
                    1,
                    format!("rollback_config not found for task_id={task_id}"),
                    RouteUsed::Local,
                );
            };
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64();
            if let Err(error) = conn.execute("UPDATE rollback_config SET rollback_flag=?1, updated_at=?2 WHERE task_id=?3 AND (workspace_id=?4 OR workspace_id IS NULL)", params![flag, now, task_id, workspace_id]) {
                return CommandResult::failure(1, format!("rollback set failed: {error}"), RouteUsed::Local);
            }
            CommandResult::success_json(
                &json!({"success":true,"task_id":task_id,"feature_name":feature,"rollback_flag":flag,"previous_flag":old_flag,"reason":reason}),
                RouteUsed::Local,
            )
        }
        "is-rolled-back" => {
            let rolled_back: i64 = conn.query_row(
                "SELECT COALESCE((SELECT rollback_flag FROM rollback_config WHERE feature_name=?1 AND (workspace_id=?2 OR workspace_id IS NULL) ORDER BY updated_at DESC LIMIT 1),0)",
                params![feature_name, workspace_id], |row| row.get(0),
            ).unwrap_or(0);
            let result = CommandResult::success_json(
                &json!({"feature_name":feature_name,"rolled_back":rolled_back == 1}),
                RouteUsed::Local,
            );
            if rolled_back == 1 {
                CommandResult {
                    exit_code: 1,
                    ..result
                }
            } else {
                result
            }
        }
        _ => CommandResult::failure(
            2,
            format!("unknown rollback action: {action}"),
            RouteUsed::Local,
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn test_runtime(db_path: PathBuf) -> RuntimeOptions {
        RuntimeOptions {
            mode: super::super::router::DaemonMode::Local,
            socket_path: PathBuf::from("unused.sock"),
            db_path,
            workspace_id: None,
            timeout: Duration::from_secs(5),
        }
    }

    #[test]
    fn metric_complexity_uses_word_boundaries() {
        assert_eq!(metric_complexity("if (x) { helper(); }", "rust"), 2);
        assert_eq!(metric_complexity("different iffy value", "rust"), 1);
        assert_eq!(metric_complexity("", "python"), 1);
    }

    #[test]
    fn defect_helpers_preserve_category_and_unified_diff_contract() {
        assert_eq!(
            defect_category_from_rule("python.lang.security.audit"),
            "security"
        );
        assert_eq!(defect_category_from_rule("custom.rule.naming"), "naming");
        let diff = simple_unified_diff("before\nkeep\n", "after\nkeep\n");
        assert!(diff.starts_with("--- before\n+++ after\n@@"));
        assert!(diff.contains("-before\n"));
        assert!(diff.contains("+after\n"));
    }

    #[test]
    fn defect_learn_writes_workspace_scoped_fix_and_returns_details() {
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("callwarden.db");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, is_active INTEGER, root_path TEXT);
             CREATE TABLE file_instances (id INTEGER PRIMARY KEY, workspace_id INTEGER, rel_path TEXT);
             CREATE TABLE symbols (id INTEGER PRIMARY KEY, file_instance_id INTEGER, symbol_hash TEXT, qualified_name TEXT);
             CREATE TABLE git_commits (commit_hash TEXT PRIMARY KEY, workspace_id INTEGER);
             CREATE TABLE git_symbol_changes (commit_hash TEXT, symbol_hash TEXT, old_content TEXT, new_content TEXT, change_type TEXT);
             CREATE TABLE symbol_contents (content_hash TEXT PRIMARY KEY, qualified_name TEXT);
             CREATE TABLE semgrep_findings (file_instance_id INTEGER, rule_id TEXT, snippet TEXT, fix TEXT, message TEXT, severity TEXT, symbol_qualified TEXT, content_hash TEXT, scanned_at REAL);
             CREATE TABLE defect_patterns (pattern_id TEXT PRIMARY KEY, category TEXT, description TEXT, detection_rule TEXT, fix_template TEXT, severity TEXT, learned_from TEXT, case_count INTEGER, created_at REAL);
             CREATE TABLE defect_fixes (id INTEGER PRIMARY KEY AUTOINCREMENT, pattern_id TEXT, symbol_hash TEXT, before_hash TEXT, after_hash TEXT, fix_diff TEXT, effectiveness REAL, created_at REAL);
             INSERT INTO workspaces VALUES (1,1,'.'),(2,0,'/other');
             INSERT INTO file_instances VALUES (10,1,'src/run.rs'),(20,2,'src/run.rs');
             INSERT INTO symbols VALUES (1,10,'sym-1','crate::run'),(2,20,'sym-1','crate::run');
             INSERT INTO git_commits VALUES ('fix-1',1);
             INSERT INTO git_symbol_changes VALUES ('fix-1','sym-1','before();','after();','modified');
             INSERT INTO symbol_contents VALUES ('sym-1','crate::run');
             INSERT INTO semgrep_findings VALUES (10,'python.lang.security.audit','before();','replace before','workspace 1 finding','ERROR','crate::run','',1.0);
             INSERT INTO semgrep_findings VALUES (20,'other.workspace.rule','before();','wrong fix','workspace 2 finding','ERROR','crate::run','',2.0);",
        )
        .unwrap();
        drop(conn);

        let runtime = test_runtime(db_path.clone());
        let result = run_defect(&runtime, "learn", "", "", 20, "", 0, "fix-1");
        assert_eq!(result.exit_code, 0, "{}", result.stderr);
        let value: Value = serde_json::from_str(&result.stdout).unwrap();
        assert_eq!(value["learned_patterns"], 1);
        assert_eq!(value["learned_fixes"], 1);
        assert_eq!(value["details"][0]["status"], "learned");
        let conn = rusqlite::Connection::open(db_path).unwrap();
        let fix_diff: String = conn
            .query_row("SELECT fix_diff FROM defect_fixes LIMIT 1", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert!(fix_diff.contains("-before();"));
        assert!(fix_diff.contains("+after();"));
    }

    #[test]
    fn hotspot_report_does_not_mix_workspace_history_or_findings() {
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("callwarden.db");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, is_active INTEGER);
             CREATE TABLE file_instances (id INTEGER PRIMARY KEY, workspace_id INTEGER, rel_path TEXT, module_path TEXT);
             CREATE TABLE symbols (id INTEGER PRIMARY KEY, file_instance_id INTEGER, symbol_hash TEXT, qualified_name TEXT, module_path TEXT, start_line INTEGER, end_line INTEGER, kind TEXT);
             CREATE TABLE symbol_contents (content_hash TEXT PRIMARY KEY, content TEXT);
             CREATE TABLE file_versions (id INTEGER PRIMARY KEY, file_instance_id INTEGER, parsed_at REAL);
             CREATE TABLE file_symbol_versions (file_version_id INTEGER, symbol_hash TEXT);
             CREATE TABLE semgrep_findings (file_instance_id INTEGER, symbol_qualified TEXT);
             INSERT INTO workspaces VALUES (1,1),(2,0);
             INSERT INTO file_instances VALUES (10,1,'src/run.rs','crate'),(20,2,'src/run.rs','crate');
             INSERT INTO symbols VALUES (1,10,'shared-hash','crate::run','crate',1,3,'fn'),(2,20,'shared-hash','crate::run','crate',1,3,'fn');
             INSERT INTO symbol_contents VALUES ('shared-hash','if value { return; }');
             INSERT INTO file_versions VALUES (100,10,10.0),(200,20,20.0);
             INSERT INTO file_symbol_versions VALUES (100,'shared-hash'),(200,'shared-hash');
             INSERT INTO semgrep_findings VALUES (20,'crate::run');",
        )
        .unwrap();
        drop(conn);

        let runtime = test_runtime(db_path);
        let value: Value = serde_json::from_str(&run_hotspot_report(&runtime, "", 10).stdout).unwrap();
        let result = &value["results"][0];
        assert_eq!(result["symbol_hash"], "shared-hash");
        assert_eq!(result["change_count"], 1);
        assert_eq!(result["defect_count"], 0);
    }

    #[test]
    fn metric_commands_are_workspace_scoped_and_real() {
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("callwarden.db");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, is_active INTEGER);
             CREATE TABLE file_instances (id INTEGER PRIMARY KEY, workspace_id INTEGER, status TEXT, total_lines INTEGER, rel_path TEXT, abs_path TEXT, module_path TEXT);
             CREATE TABLE symbols (id INTEGER PRIMARY KEY, file_instance_id INTEGER, kind TEXT, has_comment INTEGER, symbol_hash TEXT, qualified_name TEXT, name TEXT, start_line INTEGER, end_line INTEGER, depth INTEGER, module_path TEXT, signature TEXT);
             CREATE TABLE symbol_contents (content_hash TEXT PRIMARY KEY, content TEXT);
             CREATE TABLE calls (caller_id INTEGER, callee_id INTEGER, callee_name TEXT, callee_module TEXT);
             INSERT INTO workspaces VALUES (1,1),(2,0);
             INSERT INTO file_instances VALUES (1,1,'active',12,'src/a.rs','/repo/src/a.rs','crate::a'),(2,2,'active',99,'src/b.rs','/other/src/b.rs','other::b');
             INSERT INTO symbols VALUES (1,1,'fn',1,'h1','crate::a::run','run',1,4,0,'crate::a','fn run()'),(2,2,'fn',0,'h2','other::b::run','run',1,80,0,'other::b','fn run()');
             INSERT INTO symbol_contents VALUES ('h1','if (x) { helper(); }');
             INSERT INTO calls VALUES (1,0,'helper','crate::helper');",
        )
        .unwrap();
        drop(conn);

        let runtime = test_runtime(db_path);
        let metrics: Value = serde_json::from_str(&run_metrics_summary(&runtime).stdout).unwrap();
        assert_eq!(metrics["file_count"], 1);
        assert_eq!(metrics["function_count"], 1);
        assert_eq!(metrics["total_lines"], 12);
        assert_eq!(metrics["total_calls"], 1);
        assert_eq!(metrics["comment_coverage"], 100.0);

        let complexity: Value =
            serde_json::from_str(&run_complexity_report(&runtime, 30, "").stdout).unwrap();
        assert_eq!(
            complexity["functions"][0]["qualified_name"],
            "crate::a::run"
        );
        assert_eq!(complexity["functions"][0]["cyclomatic_complexity"], 2);

        let largest: Value =
            serde_json::from_str(&run_largest_functions(&runtime, 30, "").stdout).unwrap();
        assert_eq!(largest["functions"].as_array().unwrap().len(), 1);
        assert_eq!(largest["functions"][0]["line_count"], 4);

        let map: Value = serde_json::from_str(&run_repo_map(&runtime, "text").stdout).unwrap();
        assert_eq!(map["edges"].as_array().unwrap().len(), 1);
        assert_eq!(map["edges"][0]["caller"], "crate::a");
    }

    #[test]
    fn health_report_counts_python_metric_categories() {
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("callwarden.db");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, is_active INTEGER);
             CREATE TABLE file_instances (id INTEGER PRIMARY KEY, workspace_id INTEGER, status TEXT, total_lines INTEGER, rel_path TEXT);
             CREATE TABLE symbols (id INTEGER PRIMARY KEY, file_instance_id INTEGER, kind TEXT, symbol_hash TEXT, start_line INTEGER, end_line INTEGER, module_path TEXT);
             CREATE TABLE symbol_contents (content_hash TEXT PRIMARY KEY, content TEXT);
             CREATE TABLE calls (caller_id INTEGER, callee_module TEXT);
             INSERT INTO workspaces VALUES (1,1);
             INSERT INTO file_instances VALUES (1,1,'active',2500,'src/large.rs');
             INSERT INTO symbols VALUES (1,1,'fn','h1',1,250,'crate::large');
             INSERT INTO symbol_contents VALUES ('h1','');",
        )
        .unwrap();
        let content = (0..30)
            .map(|index| format!("if value_{index} {{ value_{index}(); }}"))
            .collect::<Vec<_>>()
            .join("\n");
        conn.execute(
            "UPDATE symbol_contents SET content=?1 WHERE content_hash='h1'",
            [&content],
        )
        .unwrap();
        drop(conn);

        let runtime = test_runtime(db_path);
        let value: Value = serde_json::from_str(&run_health_report(&runtime).stdout).unwrap();
        assert_eq!(value["high_issue_count"], 3);
        assert_eq!(value["medium_issue_count"], 0);
        assert_eq!(value["low_issue_count"], 0);
        assert_eq!(value["total_issue_count"], 3);
        assert_eq!(value["health_score"], 85.0);
        assert_eq!(value["health_level"], "good");
    }

    #[test]
    fn metric_complexity_matches_python_regex_boundaries() {
        // Python 的 \b&&\b/\b||\b 只匹配紧邻单词字符的运算符。
        assert_eq!(metric_complexity("if left&&right { return; }", "rust"), 3);
        assert_eq!(
            metric_complexity("if left && right || other { return; }", "rust"),
            2
        );

        // Python 只把 ? 后到下一个 : 的模式当作三元表达式，Rust 错误传播不计入。
        assert_eq!(metric_complexity("let value = fetch()?;", "rust"), 1);
        assert_eq!(metric_complexity("let value = ok ? yes : no;", "rust"), 2);

        // 普通关键词也必须使用 Python 的 Unicode-aware \b 边界。
        assert_eq!(metric_complexity("中文if中文", "rust"), 1);
        assert_eq!(metric_complexity("中文 if 中文", "rust"), 2);

        // Python 的 for-in 正则不要求前后是空格，且按行匹配列表推导式。
        assert_eq!(metric_complexity("for x in xs", "python"), 3);
        assert_eq!(metric_complexity("[x for(x) in xs]", "python"), 3);
        assert_eq!(metric_complexity("for\tx in xs", "python"), 3);
    }

    #[test]
    fn health_report_matches_python_limits_and_archived_scope() {
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("callwarden.db");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, is_active INTEGER);
             CREATE TABLE file_instances (id INTEGER PRIMARY KEY, workspace_id INTEGER, status TEXT, total_lines INTEGER, rel_path TEXT);
             CREATE TABLE symbols (id INTEGER PRIMARY KEY, file_instance_id INTEGER, kind TEXT, symbol_hash TEXT, start_line INTEGER, end_line INTEGER, module_path TEXT);
             CREATE TABLE symbol_contents (content_hash TEXT PRIMARY KEY, content TEXT);
             CREATE TABLE calls (caller_id INTEGER, callee_module TEXT);
             INSERT INTO workspaces VALUES (1,1),(2,0);
             INSERT INTO file_instances VALUES (100,2,'active',5000,'other/huge.rs');",
        )
        .unwrap();
        let complex_content = (0..30)
            .map(|index| format!("if value_{index} {{ value_{index}(); }}"))
            .collect::<Vec<_>>()
            .join("\n");
        for index in 0..21 {
            let file_id = index + 1;
            let status = if index == 20 { "archived" } else { "active" };
            conn.execute(
                "INSERT INTO file_instances VALUES (?1,1,?2,2500,?3)",
                params![file_id, status, format!("src/large_{index}.rs")],
            )
            .unwrap();
        }
        for index in 0..2 {
            let file_id = index + 1;
            let hash = format!("h{index}");
            conn.execute(
                "INSERT INTO symbols VALUES (?1,?2,'fn',?3,1,250,?4)",
                params![index + 1, file_id, hash, format!("crate::large_{index}")],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO symbol_contents VALUES (?1,?2)",
                params![hash, complex_content],
            )
            .unwrap();
        }
        conn.execute(
            "INSERT INTO symbols VALUES (3,21,'fn','h2',1,250,'crate::archived')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbol_contents VALUES ('h2',?1)",
            [&complex_content],
        )
        .unwrap();
        drop(conn);

        let runtime = test_runtime(db_path);
        let value: Value = serde_json::from_str(&run_health_report(&runtime).stdout).unwrap();
        // Python 只计 20 个大文件；3 个函数分别计入复杂度和超长函数，归档函数也会被源查询纳入。
        assert_eq!(value["high_issue_count"], 26);
        assert_eq!(value["medium_issue_count"], 0);
        assert_eq!(value["low_issue_count"], 0);
        assert_eq!(value["total_issue_count"], 26);
        assert_eq!(value["health_score"], 0.0);
    }

    #[test]
    fn health_report_limits_coupling_to_python_top_twenty() {
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("callwarden.db");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, is_active INTEGER);
             CREATE TABLE file_instances (id INTEGER PRIMARY KEY, workspace_id INTEGER, status TEXT, total_lines INTEGER, rel_path TEXT);
             CREATE TABLE symbols (id INTEGER PRIMARY KEY, file_instance_id INTEGER, kind TEXT, symbol_hash TEXT, start_line INTEGER, end_line INTEGER, module_path TEXT);
             CREATE TABLE symbol_contents (content_hash TEXT PRIMARY KEY, content TEXT);
             CREATE TABLE calls (caller_id INTEGER, callee_module TEXT);
             INSERT INTO workspaces VALUES (1,1),(2,0);
             INSERT INTO file_instances VALUES (100,2,'active',5000,'other/huge.rs');",
        )
        .unwrap();
        for index in 0..21 {
            let file_id = index + 1;
            let hash = format!("h{index}");
            conn.execute(
                "INSERT INTO file_instances VALUES (?1,1,'active',10,?2)",
                params![file_id, format!("src/caller_{index}.rs")],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO symbols VALUES (?1,?2,'fn',?3,1,1,?4)",
                params![index + 1, file_id, hash, format!("crate::caller_{index}")],
            )
            .unwrap();
            conn.execute("INSERT INTO symbol_contents VALUES (?1,'')", [&hash])
                .unwrap();
            conn.execute("INSERT INTO calls VALUES (?1,'crate::callee')", [index + 1])
                .unwrap();
        }
        drop(conn);

        let runtime = test_runtime(db_path);
        let value: Value = serde_json::from_str(&run_health_report(&runtime).stdout).unwrap();
        // 被调用模块按总耦合度排第一，因此 Python 的 20 条限制只容纳 21 个不稳定调用模块中的 19 个。
        assert_eq!(value["high_issue_count"], 19);
        assert_eq!(value["medium_issue_count"], 0);
        assert_eq!(value["low_issue_count"], 0);
    }

    #[test]
    fn clone_list_and_test_impact_preserve_workspace_and_test_predicates() {
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("callwarden.db");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, is_active INTEGER);
             CREATE TABLE file_instances (id INTEGER PRIMARY KEY, workspace_id INTEGER, status TEXT, rel_path TEXT, module_path TEXT);
             CREATE TABLE symbols (id INTEGER PRIMARY KEY, file_instance_id INTEGER, kind TEXT, name TEXT, qualified_name TEXT, module_path TEXT, start_line INTEGER, end_line INTEGER, symbol_hash TEXT);
             CREATE TABLE symbol_contents (content_hash TEXT PRIMARY KEY, content TEXT);
             CREATE TABLE calls (caller_id INTEGER, callee_id INTEGER);
             CREATE TABLE clone_pairs (workspace_id INTEGER, symbol_a_id INTEGER, symbol_b_id INTEGER, clone_type INTEGER, similarity REAL, token_hash TEXT, lines_a INTEGER, lines_b INTEGER, detected_at REAL);
             INSERT INTO workspaces VALUES (1,1),(2,0);
             INSERT INTO file_instances VALUES (1,1,'active','src/a.rs','src'),(2,1,'active','tests/spec.rs','tests'),(3,2,'active','other.rs','other');
             INSERT INTO symbols VALUES (1,1,'fn','target','crate::target','src',1,4,'h1'),(2,2,'fn','behavior_spec','tests::behavior_spec','tests',2,6,'h2'),(3,3,'fn','other_spec','other::other_spec','other',2,6,'h3');
             INSERT INTO symbol_contents VALUES ('h1','target()'),('h2','spec()'),('h3','spec()');
             INSERT INTO calls VALUES (2,1),(3,1);
             INSERT INTO clone_pairs VALUES (1,1,2,1,0.95,'t1',4,5,1.0),(2,1,3,1,0.99,'t2',4,5,1.0);",
        )
        .unwrap();
        drop(conn);

        let runtime = test_runtime(db_path);
        let clones = serde_json::from_str::<Value>(
            &run_clone(
                &runtime,
                "list",
                "tests::behavior_spec",
                Some(1),
                0.0,
                100,
                5,
                0.8,
            )
            .stdout,
        )
        .unwrap();
        assert_eq!(clones["clones"].as_array().unwrap().len(), 1);
        assert_eq!(
            clones["clones"][0]["symbol_b_qualified"],
            "tests::behavior_spec"
        );

        let impact =
            serde_json::from_str::<Value>(&run_test_impact(&runtime, "crate::target").stdout)
                .unwrap();
        assert_eq!(impact["count"], 1);
        assert_eq!(impact["tests"][0]["qualified_name"], "tests::behavior_spec");
        assert_eq!(impact["tests"][0]["module_path"], "tests");
    }
}
