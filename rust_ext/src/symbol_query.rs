//! 完整符号详情的只读 SQLite 查询。
//!
//! local CLI 与 enterprise snapshot RPC 共用本模块，避免 GraphStore 基本字段
//! 被误当作完整 `cw symbol` 契约。

use rusqlite::{params, Connection, OptionalExtension, Row};
use serde_json::{json, Value};

/// 查询当前 workspace 中的完整符号详情。
///
/// 返回 `null` 表示符号不存在；成功对象包含基本信息、调用关系和前五条
/// WARNING+ 静态检查问题。
pub fn query_symbol_detail(
    conn: &Connection,
    workspace_id: i64,
    qualified_name: &str,
) -> Result<Value, String> {
    let row = conn
        .query_row(
            "
            SELECT DISTINCT
                fsv.qualified_name,
                fsv.module_path,
                fsv.start_line,
                fsv.end_line,
                fsv.depth,
                sc.name,
                sc.kind,
                COALESCE(sc.signature, ''),
                COALESCE(sc.has_comment, 0),
                COALESCE(sc.comment_content, ''),
                sc.content_hash,
                fi.rel_path,
                fi.abs_path,
                fi.id
            FROM file_symbol_versions fsv
            JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
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
            symbol_row,
        )
        .optional()
        .map_err(|error| format!("cannot query symbol detail: {error}"))?;

    let Some(mut detail) = row else {
        return Ok(Value::Null);
    };
    let object = detail
        .as_object_mut()
        .ok_or_else(|| "symbol detail row must be an object".to_string())?;
    let file_instance_id = object
        .remove("_file_instance_id")
        .and_then(|value| value.as_i64())
        .ok_or_else(|| "symbol detail is missing file_instance_id".to_string())?;
    let symbol_hash = object
        .get("content_hash")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let file_path = object
        .get("file_path")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();

    object.insert(
        "calls_out".to_string(),
        Value::Array(query_calls_out(conn, workspace_id, qualified_name)?),
    );
    object.insert(
        "called_by".to_string(),
        Value::Array(query_called_by(conn, workspace_id, qualified_name)?),
    );

    let issues = query_symbol_issues(
        conn,
        file_instance_id,
        qualified_name,
        object
            .get("start_line")
            .and_then(Value::as_i64)
            .unwrap_or_default(),
        object
            .get("end_line")
            .and_then(Value::as_i64)
            .unwrap_or_default(),
        &file_path,
        &symbol_hash,
    );
    object.insert(
        "issues_total".to_string(),
        Value::Number((issues.len() as u64).into()),
    );
    object.insert(
        "issues".to_string(),
        Value::Array(issues.into_iter().take(5).collect()),
    );
    Ok(detail)
}

fn symbol_row(row: &Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "qualified_name": row.get::<_, String>(0)?,
        "module_path": row.get::<_, String>(1)?,
        "start_line": row.get::<_, i64>(2)?,
        "end_line": row.get::<_, i64>(3)?,
        "depth": row.get::<_, i64>(4)?,
        "name": row.get::<_, String>(5)?,
        "kind": row.get::<_, String>(6)?,
        "signature": row.get::<_, String>(7)?,
        "has_comment": row.get::<_, i64>(8)? != 0,
        "comment_content": row.get::<_, String>(9)?,
        "content_hash": row.get::<_, String>(10)?,
        "file_path": row.get::<_, String>(11)?,
        "abs_path": row.get::<_, String>(12)?,
        "_file_instance_id": row.get::<_, i64>(13)?,
    }))
}

fn query_calls_out(
    conn: &Connection,
    workspace_id: i64,
    qualified_name: &str,
) -> Result<Vec<Value>, String> {
    let mut stmt = conn
        .prepare(
            "
            SELECT DISTINCT
                COALESCE(NULLIF(cv.callee_qualified, ''), cv.callee_name),
                COALESCE(cv.callee_module, ''),
                COALESCE(cv.callee_file, ''),
                COALESCE(cv.call_line, 0)
            FROM call_versions cv
            JOIN file_versions fv ON cv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ?1
              AND fv.is_current = 1
              AND cv.caller_qualified = ?2
            ORDER BY cv.callee_qualified
            ",
        )
        .map_err(|error| format!("cannot prepare outgoing symbol calls: {error}"))?;
    let rows = stmt
        .query_map(params![workspace_id, qualified_name], |row| {
            Ok(json!({
                "target_name": row.get::<_, String>(0)?,
                "target_module": row.get::<_, String>(1)?,
                "target_file": row.get::<_, String>(2)?,
                "call_line": row.get::<_, i64>(3)?,
            }))
        })
        .map_err(|error| format!("cannot query outgoing symbol calls: {error}"))?;
    collect_rows(rows, "outgoing symbol call")
}

fn query_called_by(
    conn: &Connection,
    workspace_id: i64,
    qualified_name: &str,
) -> Result<Vec<Value>, String> {
    let mut stmt = conn
        .prepare(
            "
            SELECT DISTINCT
                cv.caller_qualified,
                COALESCE(cv.caller_hash, ''),
                COALESCE(cv.call_line, 0),
                fi.rel_path
            FROM call_versions cv
            JOIN file_versions fv ON cv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ?1
              AND fv.is_current = 1
              AND cv.callee_qualified = ?2
              AND cv.caller_qualified != ''
            ORDER BY cv.caller_qualified
            ",
        )
        .map_err(|error| format!("cannot prepare incoming symbol calls: {error}"))?;
    let rows = stmt
        .query_map(params![workspace_id, qualified_name], |row| {
            Ok(json!({
                "caller_name": row.get::<_, String>(0)?,
                "caller_hash": row.get::<_, String>(1)?,
                "call_line": row.get::<_, i64>(2)?,
                "caller_file": row.get::<_, String>(3)?,
            }))
        })
        .map_err(|error| format!("cannot query incoming symbol calls: {error}"))?;
    collect_rows(rows, "incoming symbol call")
}

fn collect_rows<T>(
    rows: rusqlite::MappedRows<'_, impl FnMut(&Row<'_>) -> rusqlite::Result<T>>,
    label: &str,
) -> Result<Vec<T>, String> {
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("cannot read {label}: {error}"))
}

fn query_symbol_issues(
    conn: &Connection,
    file_instance_id: i64,
    qualified_name: &str,
    start_line: i64,
    end_line: i64,
    file_path: &str,
    symbol_hash: &str,
) -> Vec<Value> {
    let mut issues = Vec::new();
    if let Ok(mut stmt) = conn.prepare(
        "
        SELECT rule_id, rule_name, severity, confidence, message,
               start_line, end_line, snippet, fix
        FROM semgrep_findings
        WHERE file_instance_id = ?1
          AND (symbol_qualified = ?2 OR symbol_qualified = ''
               OR (start_line BETWEEN ?3 AND ?4 AND end_line BETWEEN ?3 AND ?4))
          AND severity != 'INFO'
          AND severity != 'UNKNOWN'
        ORDER BY CASE severity WHEN 'ERROR' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END,
                 start_line
        ",
    ) {
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

    if let Ok(mut stmt) = conn.prepare(
        "
        SELECT gf.rule_id, gr.category, gf.severity, gf.message,
               gf.status, gf.detected_at
        FROM guardrail_findings gf
        JOIN guardrail_rules gr ON gf.rule_id = gr.rule_id
        WHERE gf.file_path = ?1
          AND gf.symbol_hash = ?2
          AND gf.severity != 'info'
        ORDER BY CASE gf.severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END
        ",
    ) {
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
    issues
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "
            CREATE TABLE file_instances (
                id INTEGER PRIMARY KEY,
                workspace_id INTEGER NOT NULL,
                rel_path TEXT NOT NULL,
                abs_path TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE file_versions (
                id INTEGER PRIMARY KEY,
                file_instance_id INTEGER NOT NULL,
                is_current INTEGER NOT NULL
            );
            CREATE TABLE symbol_contents (
                content_hash TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                signature TEXT,
                has_comment INTEGER,
                comment_content TEXT
            );
            CREATE TABLE file_symbol_versions (
                file_version_id INTEGER NOT NULL,
                symbol_hash TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                module_path TEXT,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                depth INTEGER NOT NULL,
                is_deleted INTEGER NOT NULL
            );
            CREATE TABLE call_versions (
                file_version_id INTEGER NOT NULL,
                caller_qualified TEXT NOT NULL,
                caller_hash TEXT,
                callee_name TEXT NOT NULL,
                callee_module TEXT,
                callee_qualified TEXT,
                callee_file TEXT,
                call_line INTEGER
            );
            CREATE TABLE semgrep_findings (
                file_instance_id INTEGER NOT NULL,
                rule_id TEXT NOT NULL,
                rule_name TEXT,
                severity TEXT,
                confidence TEXT,
                message TEXT,
                start_line INTEGER,
                end_line INTEGER,
                snippet TEXT,
                fix TEXT,
                symbol_qualified TEXT
            );
            CREATE TABLE guardrail_rules (
                rule_id TEXT PRIMARY KEY,
                category TEXT NOT NULL
            );
            CREATE TABLE guardrail_findings (
                rule_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                symbol_hash TEXT,
                severity TEXT,
                status TEXT,
                message TEXT,
                detected_at REAL
            );
            INSERT INTO file_instances VALUES (1, 7, 'a.py', '/repo/a.py', 'active');
            INSERT INTO file_versions VALUES (10, 1, 1);
            INSERT INTO symbol_contents VALUES
                ('hash-alpha', 'alpha', 'fn', 'alpha()', 1, 'alpha docs'),
                ('hash-beta', 'beta', 'fn', 'beta()', 0, '');
            INSERT INTO file_symbol_versions VALUES
                (10, 'hash-alpha', 'a.alpha', 'a', 1, 5, 0, 0),
                (10, 'hash-beta', 'a.beta', 'a', 7, 9, 0, 0);
            INSERT INTO call_versions VALUES
                (10, 'a.alpha', 'hash-alpha', 'beta', 'a', 'a.beta', 'a.py', 3),
                (10, 'a.beta', 'hash-beta', 'alpha', 'a', 'a.alpha', 'a.py', 8);
            INSERT INTO semgrep_findings VALUES
                (1, 'python.eval', 'eval use', 'ERROR', 'HIGH', 'avoid eval',
                 2, 2, 'eval(x)', 'use parser', 'a.alpha');
            INSERT INTO guardrail_rules VALUES ('guard.db', 'db_safety');
            INSERT INTO guardrail_findings VALUES
                ('guard.db', 'a.py', 'hash-alpha', 'warn', 'open', 'unsafe SQL', 1.0);
            ",
        )
        .unwrap();
        conn
    }

    #[test]
    fn returns_full_symbol_detail() {
        let conn = fixture();
        let detail = query_symbol_detail(&conn, 7, "a.alpha").unwrap();
        assert_eq!(detail["qualified_name"], "a.alpha");
        assert_eq!(detail["signature"], "alpha()");
        assert_eq!(detail["has_comment"], true);
        assert_eq!(detail["calls_out"][0]["target_name"], "a.beta");
        assert_eq!(detail["called_by"][0]["caller_name"], "a.beta");
        assert_eq!(detail["issues_total"], 2);
        assert_eq!(detail["issues"][0]["source"], "semgrep");
        assert_eq!(detail["issues"][1]["source"], "guardrail");
    }

    #[test]
    fn missing_or_foreign_workspace_symbol_returns_null() {
        let conn = fixture();
        assert!(query_symbol_detail(&conn, 7, "missing").unwrap().is_null());
        assert!(query_symbol_detail(&conn, 8, "a.alpha").unwrap().is_null());
    }
}
