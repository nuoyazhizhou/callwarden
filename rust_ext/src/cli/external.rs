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
use rusqlite::params;
use serde_json::{json, Value};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use super::runtime::{CommandResult, RouteUsed, RuntimeOptions};
use super::status::{load_ignore_patterns, should_ignore};

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
