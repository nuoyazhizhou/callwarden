//! 度量/状态面 handler（T02-metrics 批次，9 个工具）。
//!
//! 对应 `tool_migration_matrix.json` 中 target_backend=rust_native、
//! batch=T02-metrics 的 9 个纯本地 SQL 工具：get_status / get_code_metrics_summary /
//! get_complexity_hotspots / get_coupling_analysis / get_function_metrics /
//! get_largest_functions / get_most_coupled_functions / get_code_health_check /
//! get_symbol_content_by_hash。
//!
//! 数据源：workspace codegraph DB（snapshot query 只读连接，由
//! SnapshotDaemonState::open_query_connection 提供）。所有查询受
//! QueryBudget（limit 上限）约束；本模块不接收客户端 SQL 片段（Q4 否决通用
//! SQL RPC 的工程落地）。

use rusqlite::Connection;
use serde_json::{json, Map, Value};

use super::dispatch::{get_int_param_or, get_str_param_or, DaemonRpcError};

/// 查询结果行数上限（QueryBudget 常量，防止 BFS/DFS 指数爆炸）。
const MAX_RESULT_ROWS: i64 = 500;

/// `query.status` —— 工作区状态（含基本计数）。
pub fn handle_status(
    conn: &Connection,
    workspace_id: i64,
    workspace_instance_id: &str,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let symbol_count = scalar_i64(
        conn,
        "SELECT COUNT(*) FROM symbols s JOIN file_instances fi ON fi.id = s.file_instance_id WHERE fi.workspace_id = ?1",
        workspace_id,
    )?;
    let call_count = scalar_i64(
        conn,
        "SELECT COUNT(*) FROM calls c JOIN symbols s ON s.id = c.caller_id JOIN file_instances fi ON fi.id = s.file_instance_id WHERE fi.workspace_id = ?1",
        workspace_id,
    )?;
    let file_count = scalar_i64(
        conn,
        "SELECT COUNT(*) FROM file_instances WHERE workspace_id = ?1 AND status != 'archived'",
        workspace_id,
    )?;
    let _ = params;
    let mut m = Map::new();
    m.insert("workspace_instance_id".into(), Value::String(workspace_instance_id.to_string()));
    m.insert("status".into(), Value::String("ok".to_string()));
    m.insert("schema_version".into(), Value::Number(super::SCHEMA_VERSION.into()));
    m.insert("symbol_count".into(), Value::Number(symbol_count.into()));
    m.insert("call_count".into(), Value::Number(call_count.into()));
    m.insert("file_count".into(), Value::Number(file_count.into()));
    Ok(Value::Object(m))
}

/// `query.metrics_summary` —— 代码度量摘要。
pub fn handle_metrics_summary(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let _ = params;
    let symbols = scalar_i64(
        conn,
        "SELECT COUNT(*) FROM symbols s JOIN file_instances fi ON fi.id = s.file_instance_id WHERE fi.workspace_id = ?1",
        workspace_id,
    )?;
    let calls = scalar_i64(
        conn,
        "SELECT COUNT(*) FROM calls c JOIN symbols s ON s.id = c.caller_id JOIN file_instances fi ON fi.id = s.file_instance_id WHERE fi.workspace_id = ?1",
        workspace_id,
    )?;
    let files = scalar_i64(
        conn,
        "SELECT COUNT(*) FROM file_instances WHERE workspace_id = ?1 AND status != 'archived'",
        workspace_id,
    )?;
    let commented = scalar_i64(
        conn,
        "SELECT COUNT(DISTINCT s.symbol_hash) FROM symbols s JOIN file_instances fi ON fi.id = s.file_instance_id WHERE fi.workspace_id = ?1 AND s.has_comment = 1",
        workspace_id,
    )?;
    let functions = scalar_i64(
        conn,
        "SELECT COUNT(*) FROM symbols s JOIN file_instances fi ON fi.id = s.file_instance_id WHERE fi.workspace_id = ?1 AND s.kind IN ('fn','test_fn','func','function','method')",
        workspace_id,
    )?;
    let avg_depth = scalar_f64(
        conn,
        "SELECT AVG(s.depth) FROM symbols s JOIN file_instances fi ON fi.id = s.file_instance_id WHERE fi.workspace_id = ?1 AND s.depth >= 0",
        workspace_id,
    )?;
    let comment_rate = if symbols > 0 { commented as f64 / symbols as f64 } else { 0.0 };
    Ok(json!({
        "symbols": symbols,
        "calls": calls,
        "files": files,
        "commented_symbols": commented,
        "functions": functions,
        "avg_depth": avg_depth,
        "comment_coverage": (comment_rate * 10000.0).round() / 10000.0,
    }))
}

/// `query.complexity_hotspots` —— 复杂度热点（行跨度 Top N）。
pub fn handle_complexity_hotspots(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let limit = get_int_param_or(params, "limit", 20).clamp(1, MAX_RESULT_ROWS);
    let module_filter = get_str_param_or(params, "module_filter", "");
    let mut sql = String::from(
        "SELECT s.qualified_name, s.name, s.kind, s.start_line, s.end_line, fi.rel_path,
                (s.end_line - s.start_line + 1) AS span
         FROM symbols s JOIN file_instances fi ON fi.id = s.file_instance_id
         WHERE fi.workspace_id = ?1 AND s.kind IN ('fn','test_fn','func','function','method')
           AND s.start_line > 0 AND s.end_line >= s.start_line",
    );
    if !module_filter.is_empty() {
        sql.push_str(" AND s.module_path LIKE ?2 ESCAPE '\\'");
    }
    sql.push_str(" ORDER BY span DESC LIMIT ?3");
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("complexity prepare: {e}")))?;
    let rows: Result<Vec<Value>, rusqlite::Error> = if module_filter.is_empty() {
        stmt.query_map(rusqlite::params![workspace_id, limit], |row| {
            Ok(json!({
                "qualified_name": row.get::<_, String>(0)?,
                "name": row.get::<_, String>(1)?,
                "kind": row.get::<_, String>(2)?,
                "start_line": row.get::<_, i64>(3)?,
                "end_line": row.get::<_, i64>(4)?,
                "file_path": row.get::<_, String>(5)?,
                "line_span": row.get::<_, i64>(6)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("complexity query: {e}")))?
        .collect()
    } else {
        let pattern = format!("%{}%", module_filter.replace('\\', "\\\\").replace('%', "\\%"));
        stmt.query_map(rusqlite::params![workspace_id, pattern, limit], |row| {
            Ok(json!({
                "qualified_name": row.get::<_, String>(0)?,
                "name": row.get::<_, String>(1)?,
                "kind": row.get::<_, String>(2)?,
                "start_line": row.get::<_, i64>(3)?,
                "end_line": row.get::<_, i64>(4)?,
                "file_path": row.get::<_, String>(5)?,
                "line_span": row.get::<_, i64>(6)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("complexity query: {e}")))?
        .collect()
    };
    rows.map(Value::Array)
        .map_err(|e| DaemonRpcError::internal_error(format!("complexity query: {e}")))
}

/// `query.coupling_analysis` —— 模块耦合分析（跨模块调用 Top N）。
pub fn handle_coupling_analysis(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let limit = get_int_param_or(params, "limit", 30).clamp(1, MAX_RESULT_ROWS);
    let mut stmt = conn
        .prepare(
            "SELECT c.caller_module, c.callee_module, COUNT(*) AS cnt,
                    COUNT(DISTINCT c.caller_id) AS unique_callers,
                    COUNT(DISTINCT c.callee_id) AS unique_callees
             FROM calls c
             JOIN symbols s ON s.id = c.caller_id
             JOIN file_instances fi ON fi.id = s.file_instance_id
             WHERE fi.workspace_id = ?1 AND c.caller_module != '' AND c.callee_module != ''
               AND c.caller_module != c.callee_module
             GROUP BY c.caller_module, c.callee_module
             ORDER BY cnt DESC LIMIT ?2",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("coupling prepare: {e}")))?;
    let rows = stmt
        .query_map(rusqlite::params![workspace_id, limit], |row| {
            Ok(json!({
                "caller_module": row.get::<_, String>(0)?,
                "callee_module": row.get::<_, String>(1)?,
                "call_count": row.get::<_, i64>(2)?,
                "unique_caller_count": row.get::<_, i64>(3)?,
                "unique_callee_count": row.get::<_, i64>(4)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("coupling query: {e}")))?
        .collect::<Result<Vec<Value>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("coupling query: {e}")))?;
    Ok(Value::Array(rows))
}

/// `query.function_metrics` —— 单函数度量。
pub fn handle_function_metrics(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let qualified_name = super::dispatch::require_str_param(params, "qualified_name")?;
    let mut stmt = conn
        .prepare(
            "SELECT s.id, s.name, s.kind, s.qualified_name, s.signature, s.start_line, s.end_line,
                    s.has_comment, s.depth, fi.rel_path
             FROM symbols s JOIN file_instances fi ON fi.id = s.file_instance_id
             WHERE fi.workspace_id = ?1 AND s.qualified_name = ?2
             ORDER BY s.start_line ASC LIMIT 1",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("function_metrics prepare: {e}")))?;
    let mut rows = stmt
        .query_map(rusqlite::params![workspace_id, qualified_name], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, i64>(5)?,
                row.get::<_, i64>(6)?,
                row.get::<_, i64>(7)?,
                row.get::<_, i64>(8)?,
                row.get::<_, String>(9)?,
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("function_metrics query: {e}")))?;
    if let Some(row) = rows.next() {
        let (id, name, kind, qname, signature, start_line, end_line, has_comment, depth, rel_path) =
            row.map_err(|e| DaemonRpcError::internal_error(format!("function_metrics row: {e}")))?;
        let call_count = scalar_i64(
            conn,
            "SELECT COUNT(*) FROM calls WHERE caller_id = ?1",
            id,
        )?;
        let callee_count = scalar_i64(
            conn,
            "SELECT COUNT(DISTINCT callee_id) FROM calls WHERE caller_id = ?1 AND callee_id > 0",
            id,
        )?;
        return Ok(json!({
            "id": id,
            "name": name,
            "kind": kind,
            "qualified_name": qname,
            "signature": signature,
            "start_line": start_line,
            "end_line": end_line,
            "line_span": end_line - start_line + 1,
            "has_comment": has_comment != 0,
            "depth": depth,
            "file_path": rel_path,
            "call_count": call_count,
            "unique_callee_count": callee_count,
        }));
    }
    Ok(Value::Null)
}

/// `query.largest_functions` —— 最大函数列表（行跨度 Top N）。
pub fn handle_largest_functions(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let limit = get_int_param_or(params, "limit", 20).clamp(1, MAX_RESULT_ROWS);
    let module_filter = get_str_param_or(params, "module_filter", "");
    let mut sql = String::from(
        "SELECT s.qualified_name, s.name, fi.rel_path, s.start_line, s.end_line,
                (s.end_line - s.start_line + 1) AS span
         FROM symbols s JOIN file_instances fi ON fi.id = s.file_instance_id
         WHERE fi.workspace_id = ?1 AND s.kind IN ('fn','test_fn','func','function','method')
           AND s.start_line > 0 AND s.end_line >= s.start_line",
    );
    if !module_filter.is_empty() {
        sql.push_str(" AND s.module_path LIKE ?2 ESCAPE '\\'");
    }
    sql.push_str(" ORDER BY span DESC LIMIT ?3");
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("largest prepare: {e}")))?;
    let rows: Result<Vec<Value>, rusqlite::Error> = if module_filter.is_empty() {
        stmt.query_map(rusqlite::params![workspace_id, limit], |row| {
            Ok(json!({
                "qualified_name": row.get::<_, String>(0)?,
                "name": row.get::<_, String>(1)?,
                "file_path": row.get::<_, String>(2)?,
                "start_line": row.get::<_, i64>(3)?,
                "end_line": row.get::<_, i64>(4)?,
                "line_span": row.get::<_, i64>(5)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("largest query: {e}")))?
        .collect()
    } else {
        let pattern = format!("%{}%", module_filter.replace('\\', "\\\\").replace('%', "\\%"));
        stmt.query_map(rusqlite::params![workspace_id, pattern, limit], |row| {
            Ok(json!({
                "qualified_name": row.get::<_, String>(0)?,
                "name": row.get::<_, String>(1)?,
                "file_path": row.get::<_, String>(2)?,
                "start_line": row.get::<_, i64>(3)?,
                "end_line": row.get::<_, i64>(4)?,
                "line_span": row.get::<_, i64>(5)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("largest query: {e}")))?
        .collect()
    };
    rows.map(Value::Array)
        .map_err(|e| DaemonRpcError::internal_error(format!("largest query: {e}")))
}

/// `query.most_coupled_functions` —— 耦合最深的函数（被调用次数 Top N）。
pub fn handle_most_coupled_functions(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let limit = get_int_param_or(params, "limit", 20).clamp(1, MAX_RESULT_ROWS);
    let mut stmt = conn
        .prepare(
            "SELECT s.qualified_name, s.name, fi.rel_path, COUNT(c.id) AS incoming
             FROM calls c
             JOIN symbols s ON s.id = c.callee_id
             JOIN file_instances fi ON fi.id = s.file_instance_id
             WHERE fi.workspace_id = ?1 AND c.callee_id > 0
             GROUP BY s.id
             ORDER BY incoming DESC LIMIT ?2",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("most_coupled prepare: {e}")))?;
    let rows = stmt
        .query_map(rusqlite::params![workspace_id, limit], |row| {
            Ok(json!({
                "qualified_name": row.get::<_, String>(0)?,
                "name": row.get::<_, String>(1)?,
                "file_path": row.get::<_, String>(2)?,
                "incoming_call_count": row.get::<_, i64>(3)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("most_coupled query: {e}")))?
        .collect::<Result<Vec<Value>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("most_coupled query: {e}")))?;
    Ok(Value::Array(rows))
}

/// `query.code_health` —— 代码健康检查（按 severity 聚合问题）。
pub fn handle_code_health(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let severity = get_str_param_or(params, "severity", "all");
    let max_span = 80i64;
    let long_functions = scalar_i64_pair(
        conn,
        "SELECT COUNT(*) FROM symbols s JOIN file_instances fi ON fi.id = s.file_instance_id
         WHERE fi.workspace_id = ?1 AND s.kind IN ('fn','test_fn','func','function','method')
           AND (s.end_line - s.start_line + 1) > ?2",
        workspace_id,
        max_span,
    )?;
    let uncommented = scalar_i64(
        conn,
        "SELECT COUNT(*) FROM symbols s JOIN file_instances fi ON fi.id = s.file_instance_id
         WHERE fi.workspace_id = ?1 AND s.kind IN ('fn','test_fn','func','function','method') AND s.has_comment = 0",
        workspace_id,
    )?;
    let dangling_calls = scalar_i64(
        conn,
        "SELECT COUNT(*) FROM calls c JOIN symbols s ON s.id = c.caller_id
         JOIN file_instances fi ON fi.id = s.file_instance_id
         WHERE fi.workspace_id = ?1 AND c.callee_id = 0",
        workspace_id,
    )?;
    let issues = match severity.as_str() {
        "warning" => vec![
            json!({"severity": "warning", "code": "UNCOMMENTED_FUNCTIONS", "count": uncommented}),
            json!({"severity": "warning", "code": "DANGLING_CALLS", "count": dangling_calls}),
        ],
        "error" => vec![
            json!({"severity": "error", "code": "LONG_FUNCTIONS", "count": long_functions}),
        ],
        _ => vec![
            json!({"severity": "error", "code": "LONG_FUNCTIONS", "count": long_functions}),
            json!({"severity": "warning", "code": "UNCOMMENTED_FUNCTIONS", "count": uncommented}),
            json!({"severity": "warning", "code": "DANGLING_CALLS", "count": dangling_calls}),
        ],
    };
    let total: i64 = issues.iter().map(|i| i["count"].as_i64().unwrap_or(0)).sum();
    Ok(json!({ "severity": severity, "total_issues": total, "issues": issues }))
}

/// `query.symbol_content_by_hash` —— 按 content_hash 查询符号内容。
pub fn handle_symbol_content_by_hash(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let content_hash = super::dispatch::require_str_param(params, "content_hash")?;
    let mut stmt = conn
        .prepare(
            "SELECT sc.content, s.name, s.qualified_name, s.kind, fi.rel_path
             FROM symbol_contents sc
             JOIN symbols s ON s.symbol_hash = sc.content_hash
             JOIN file_instances fi ON fi.id = s.file_instance_id
             WHERE fi.workspace_id = ?1 AND sc.content_hash = ?2
             ORDER BY s.start_line ASC LIMIT 1",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol_content_by_hash prepare: {e}")))?;
    let mut rows = stmt
        .query_map(rusqlite::params![workspace_id, content_hash], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol_content_by_hash query: {e}")))?;
    if let Some(row) = rows.next() {
        let (content, name, qname, kind, rel_path) =
            row.map_err(|e| DaemonRpcError::internal_error(format!("row: {e}")))?;
        return Ok(json!({
            "content_hash": content_hash,
            "content": content,
            "name": name,
            "qualified_name": qname,
            "kind": kind,
            "file_path": rel_path,
        }));
    }
    Ok(Value::Null)
}

/// 辅助：查询单个 i64 标量（单 workspace 参数）。
fn scalar_i64(conn: &Connection, sql: &str, workspace_id: i64) -> Result<i64, DaemonRpcError> {
    conn.query_row(sql, rusqlite::params![workspace_id], |row| row.get::<_, i64>(0))
        .map_err(|e| DaemonRpcError::internal_error(format!("scalar query: {e}")))
}

/// 辅助：查询单个 i64 标量（双参数）。
fn scalar_i64_pair(
    conn: &Connection,
    sql: &str,
    a: i64,
    b: i64,
) -> Result<i64, DaemonRpcError> {
    conn.query_row(sql, rusqlite::params![a, b], |row| row.get::<_, i64>(0))
        .map_err(|e| DaemonRpcError::internal_error(format!("scalar query: {e}")))
}

/// 辅助：查询单个 f64 标量。
fn scalar_f64(conn: &Connection, sql: &str, workspace_id: i64) -> Result<f64, DaemonRpcError> {
    conn.query_row(sql, rusqlite::params![workspace_id], |row| row.get::<_, f64>(0))
        .map_err(|e| DaemonRpcError::internal_error(format!("scalar f64 query: {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_max_result_rows_sane() {
        assert!(MAX_RESULT_ROWS >= 20);
        assert!(MAX_RESULT_ROWS <= 1000);
    }
}
