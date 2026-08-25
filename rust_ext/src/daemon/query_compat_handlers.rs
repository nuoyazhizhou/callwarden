//! 查询面 compat → Rust native handler（S2 / P0-compat 批次 1）。
//!
//! 对应 `tool_migration_matrix.json` 中 target_backend=python_compat 的
//! 6 个纯 SQL 只读工具：get_top_callers / get_orphan_symbols /
//! get_deepest_functions / get_comment_coverage / get_call_heatmap /
//! find_uncovered_functions。
//!
//! 数据源：workspace codegraph DB（snapshot query 只读连接，由
//! SnapshotDaemonState::open_query_connection 提供）。SQL 复刻自
//! analyzers/call_chain.py、analyzers/coverage.py、db/db_coverage.py 的
//! 等价实现（versioned 表：file_symbol_versions / file_versions /
//! file_instances / symbol_contents / call_versions）。
//! 所有查询受 QueryBudget（limit 上限）约束；本模块不接收客户端 SQL 片段。

use rusqlite::Connection;
use serde_json::{json, Map, Value};

use crate::daemon::dispatch::{
    get_int_param_or, get_str_param, get_str_param_or, DaemonRpcError,
};

/// 查询结果行数上限（QueryBudget 常量，防止 BFS/DFS 指数爆炸）。
const MAX_RESULT_ROWS: i64 = 500;

fn scalar_i64(conn: &Connection, sql: &str, workspace_id: i64) -> Result<i64, DaemonRpcError> {
    conn.query_row(sql, rusqlite::params![workspace_id], |row| row.get(0))
        .map_err(|e| DaemonRpcError::internal_error(format!("scalar_i64 查询失败: {e}")))
}

/// `get_top_callers` —— 被调用次数最多的函数排行。
///
/// 复刻 analyzers/call_chain.py `get_top_callers`：按被调用次数降序。
/// 注意：Python 签名接受 `kind` 但 SQL 未按 kind 过滤，本实现保持一致。
pub fn handle_get_top_callers(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let limit = get_int_param_or(params, "limit", 20).clamp(1, MAX_RESULT_ROWS);
    let module_filter = get_str_param_or(params, "module_filter", "");
    let mut sql = String::from(
        "SELECT cv.callee_qualified AS qualified_name, \
                COUNT(DISTINCT cv.caller_qualified) AS caller_count, \
                COUNT(*) AS call_count \
         FROM call_versions cv \
         JOIN file_versions fv ON cv.file_version_id = fv.id \
         JOIN file_instances fi ON fv.file_instance_id = fi.id \
         WHERE fi.workspace_id = ?1 AND fv.is_current = 1 \
           AND cv.callee_qualified != '' AND cv.caller_qualified != ''",
    );
    if !module_filter.is_empty() {
        sql.push_str(" AND cv.callee_qualified LIKE ?2 ESCAPE '\\'");
    }
    // limit 是已 clamp 的整数，直接内联为字面量避免 rusqlite 占位符跳号
    // （空 filter 时 SQL 只有 ?1，若再用 ?3 会报 wrong number of parameters）。
    sql.push_str(&format!(" GROUP BY cv.callee_qualified ORDER BY caller_count DESC LIMIT {limit}"));
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("top_callers prepare: {e}")))?;
    let rows: Result<Vec<Value>, rusqlite::Error> = if module_filter.is_empty() {
        stmt.query_map(rusqlite::params![workspace_id], |row: &rusqlite::Row<'_>| {
            Ok(json!({
                "qualified_name": row.get::<_, String>(0)?,
                "caller_count": row.get::<_, i64>(1)?,
                "call_count": row.get::<_, i64>(2)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("top_callers query: {e}")))?
        .collect()
    } else {
        let pattern = format!("%{}%", module_filter.replace('\\', "\\\\").replace('%', "\\%"));
        stmt.query_map(rusqlite::params![workspace_id, pattern], |row: &rusqlite::Row<'_>| {
            Ok(json!({
                "qualified_name": row.get::<_, String>(0)?,
                "caller_count": row.get::<_, i64>(1)?,
                "call_count": row.get::<_, i64>(2)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("top_callers query: {e}")))?
        .collect()
    };
    rows.map(Value::Array)
        .map_err(|e| DaemonRpcError::internal_error(format!("top_callers query: {e}")))
}

/// `get_orphan_symbols` —— 未被任何函数调用的孤立符号。
///
/// 复刻 analyzers/call_chain.py `get_orphan_symbols`。
pub fn handle_get_orphan_symbols(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let kind = get_str_param_or(params, "kind", "fn");
    let limit = get_int_param_or(params, "limit", 100).clamp(1, MAX_RESULT_ROWS);
    let module_filter = get_str_param_or(params, "module_filter", "");
    let mut sql = String::from(
        "SELECT DISTINCT fsv.qualified_name, fsv.module_path, sc.name, sc.kind \
         FROM file_symbol_versions fsv \
         JOIN file_versions fv ON fsv.file_version_id = fv.id \
         JOIN file_instances fi ON fv.file_instance_id = fi.id \
         JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash \
         WHERE fi.workspace_id = ?1 AND fv.is_current = 1 AND sc.kind = ?2 \
           AND fsv.qualified_name NOT IN ( \
               SELECT DISTINCT cv.callee_qualified \
               FROM call_versions cv \
               JOIN file_versions fv2 ON cv.file_version_id = fv2.id \
               JOIN file_instances fi2 ON fv2.file_instance_id = fi2.id \
               WHERE fi2.workspace_id = ?1 AND fv2.is_current = 1 \
                 AND cv.callee_qualified != '' AND cv.caller_qualified != '')",
    );
    if !module_filter.is_empty() {
        sql.push_str(" AND fsv.module_path LIKE ?3 ESCAPE '\\'");
    }
    sql.push_str(&format!(" ORDER BY fsv.module_path, fsv.qualified_name LIMIT {limit}"));
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("orphan_symbols prepare: {e}")))?;
    let rows: Result<Vec<Value>, rusqlite::Error> = if module_filter.is_empty() {
        stmt.query_map(rusqlite::params![workspace_id, kind], |row: &rusqlite::Row<'_>| {
            Ok(json!({
                "qualified_name": row.get::<_, String>(0)?,
                "module_path": row.get::<_, String>(1)?,
                "name": row.get::<_, String>(2)?,
                "kind": row.get::<_, String>(3)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("orphan_symbols query: {e}")))?
        .collect()
    } else {
        let pattern = format!("%{}%", module_filter.replace('\\', "\\\\").replace('%', "\\%"));
        stmt.query_map(rusqlite::params![workspace_id, kind, pattern], |row: &rusqlite::Row<'_>| {
            Ok(json!({
                "qualified_name": row.get::<_, String>(0)?,
                "module_path": row.get::<_, String>(1)?,
                "name": row.get::<_, String>(2)?,
                "kind": row.get::<_, String>(3)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("orphan_symbols query: {e}")))?
        .collect()
    };
    rows.map(Value::Array)
        .map_err(|e| DaemonRpcError::internal_error(format!("orphan_symbols query: {e}")))
}

/// `get_deepest_functions` —— 调用深度最深的函数排行。
///
/// 复刻 analyzers/call_chain.py `get_deepest_functions`。
pub fn handle_get_deepest_functions(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let limit = get_int_param_or(params, "limit", 20).clamp(1, MAX_RESULT_ROWS);
    let module_filter = get_str_param_or(params, "module_filter", "");
    let kind = get_str_param_or(params, "kind", "fn");
    let mut sql = String::from(
        "SELECT DISTINCT fsv.qualified_name, fsv.module_path, fsv.depth, sc.kind \
         FROM file_symbol_versions fsv \
         JOIN file_versions fv ON fsv.file_version_id = fv.id \
         JOIN file_instances fi ON fv.file_instance_id = fi.id \
         JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash \
         WHERE fi.workspace_id = ?1 AND fv.is_current = 1 AND sc.kind = ?2 \
           AND fsv.depth >= 0",
    );
    if !module_filter.is_empty() {
        sql.push_str(" AND fsv.module_path LIKE ?3 ESCAPE '\\'");
    }
    sql.push_str(&format!(" ORDER BY fsv.depth DESC, fsv.qualified_name LIMIT {limit}"));
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("deepest_functions prepare: {e}")))?;
    let rows: Result<Vec<Value>, rusqlite::Error> = if module_filter.is_empty() {
        stmt.query_map(rusqlite::params![workspace_id, kind], |row: &rusqlite::Row<'_>| {
            Ok(json!({
                "qualified_name": row.get::<_, String>(0)?,
                "module_path": row.get::<_, String>(1)?,
                "depth": row.get::<_, i64>(2)?,
                "kind": row.get::<_, String>(3)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("deepest_functions query: {e}")))?
        .collect()
    } else {
        let pattern = format!("%{}%", module_filter.replace('\\', "\\\\").replace('%', "\\%"));
        stmt.query_map(rusqlite::params![workspace_id, kind, pattern], |row: &rusqlite::Row<'_>| {
            Ok(json!({
                "qualified_name": row.get::<_, String>(0)?,
                "module_path": row.get::<_, String>(1)?,
                "depth": row.get::<_, i64>(2)?,
                "kind": row.get::<_, String>(3)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("deepest_functions query: {e}")))?
        .collect()
    };
    rows.map(Value::Array)
        .map_err(|e| DaemonRpcError::internal_error(format!("deepest_functions query: {e}")))
}

/// `get_comment_coverage` —— 注释覆盖率统计。
///
/// 复刻 analyzers/coverage.py `get_comment_coverage`。返回
/// {total, commented, coverage, by_kind, by_module|by_file}。
pub fn handle_get_comment_coverage(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let group_by = get_str_param_or(params, "group_by", "module");

    // 1. 按 kind × has_comment 聚合
    let mut stmt = conn
        .prepare(
            "SELECT sc.kind, sc.has_comment, \
                    COUNT(DISTINCT fsv.qualified_name || '@' || fi.rel_path) AS cnt \
             FROM file_symbol_versions fsv \
             JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash \
             JOIN file_versions fv ON fsv.file_version_id = fv.id \
             JOIN file_instances fi ON fv.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND fv.is_current = 1 \
               AND (fsv.is_deleted = 0 OR fsv.is_deleted IS NULL) \
             GROUP BY sc.kind, sc.has_comment ORDER BY sc.kind, sc.has_comment",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("comment_coverage prepare: {e}")))?;
    let rows = stmt
        .query_map(rusqlite::params![workspace_id], |row: &rusqlite::Row<'_>| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, i64>(2)?,
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("comment_coverage query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("comment_coverage query: {e}")))?;

    let mut by_kind: Map<String, Value> = Map::new();
    let mut total_all: i64 = 0;
    let mut total_commented: i64 = 0;
    for (kind, has_comment, cnt) in &rows {
        let entry = by_kind
            .entry(kind.clone())
            .or_insert_with(|| {
                let mut m = Map::new();
                m.insert("total".to_string(), Value::Number(0.into()));
                m.insert("commented".to_string(), Value::Number(0.into()));
                Value::Object(m)
            });
        if let Value::Object(obj) = entry {
            let t = obj.get("total").and_then(Value::as_i64).unwrap_or(0);
            let c = obj.get("commented").and_then(Value::as_i64).unwrap_or(0);
            obj.insert("total".to_string(), Value::Number((t + cnt).into()));
            if *has_comment != 0 {
                obj.insert("commented".to_string(), Value::Number((c + cnt).into()));
            }
        }
        total_all += cnt;
        if *has_comment != 0 {
            total_commented += cnt;
        }
    }

    let mut result = Map::new();
    result.insert("total".to_string(), Value::Number(total_all.into()));
    result.insert("commented".to_string(), Value::Number(total_commented.into()));
    let coverage = if total_all > 0 {
        (total_commented as f64 / total_all as f64 * 10000.0).round() / 100.0
    } else {
        0.0
    };
    result.insert("coverage".to_string(), Value::Number(serde_json::Number::from_f64(coverage).unwrap_or(0.into())));
    result.insert("by_kind".to_string(), Value::Object(by_kind));

    // 2. module/file 分组（Python 仅在 group_by in (module, file) 时附加）
    if group_by == "module" || group_by == "file" {
        let mut module_stmt = conn
            .prepare(
                "SELECT fsv.module_path, fi.rel_path, sc.kind, sc.has_comment, \
                        COUNT(DISTINCT fsv.qualified_name || '@' || fi.rel_path) AS cnt \
                 FROM file_symbol_versions fsv \
                 JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash \
                 JOIN file_versions fv ON fsv.file_version_id = fv.id \
                 JOIN file_instances fi ON fv.file_instance_id = fi.id \
                 WHERE fi.workspace_id = ?1 AND fv.is_current = 1 \
                   AND (fsv.is_deleted = 0 OR fsv.is_deleted IS NULL) \
                 GROUP BY fsv.module_path, fi.rel_path, sc.kind, sc.has_comment \
                 ORDER BY fsv.module_path",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("comment_coverage prepare: {e}")))?;
        let module_rows = module_stmt
            .query_map(rusqlite::params![workspace_id], |row: &rusqlite::Row<'_>| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, i64>(4)?,
                ))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("comment_coverage query: {e}")))?
            .collect::<Result<Vec<_>, rusqlite::Error>>()
            .map_err(|e| DaemonRpcError::internal_error(format!("comment_coverage query: {e}")))?;

        let mut modules: Map<String, Value> = Map::new();
        for (module_path, rel_path, kind, has_comment, cnt) in &module_rows {
            let key = if group_by == "file" { rel_path.clone() } else { module_path.clone() };
            let key = if key.is_empty() { rel_path.clone() } else { key };
            let entry = modules.entry(key).or_insert_with(|| {
                let mut m = Map::new();
                m.insert("total".to_string(), Value::Number(0.into()));
                m.insert("commented".to_string(), Value::Number(0.into()));
                let mut bk = Map::new();
                m.insert("by_kind".to_string(), Value::Object(bk));
                Value::Object(m)
            });
            if let Value::Object(obj) = entry {
                let t = obj.get("total").and_then(Value::as_i64).unwrap_or(0);
                let c = obj.get("commented").and_then(Value::as_i64).unwrap_or(0);
                obj.insert("total".to_string(), Value::Number((t + cnt).into()));
                if *has_comment != 0 {
                    obj.insert("commented".to_string(), Value::Number((c + cnt).into()));
                }
                let kind_entry = obj
                    .get_mut("by_kind")
                    .and_then(Value::as_object_mut)
                    .and_then(|bk| {
                        // bk 是 &mut Map，entry 返回 Entry 而非 Option；
                        // 手动包装为 Option<&mut Value>。
                        Some(bk.entry(kind.clone()).or_insert_with(|| {
                            let mut m = Map::new();
                            m.insert("total".to_string(), Value::Number(0.into()));
                            m.insert("commented".to_string(), Value::Number(0.into()));
                            Value::Object(m)
                        }))
                    });
                if let Some(kv) = kind_entry {
                    if let Value::Object(km) = kv {
                        let kt = km.get("total").and_then(Value::as_i64).unwrap_or(0);
                        let kc = km.get("commented").and_then(Value::as_i64).unwrap_or(0);
                        km.insert("total".to_string(), Value::Number((kt + cnt).into()));
                        if *has_comment != 0 {
                            km.insert("commented".to_string(), Value::Number((kc + cnt).into()));
                        }
                    }
                }
            }
        }
        // 计算每组覆盖率
        for (_k, v) in modules.iter_mut() {
            if let Value::Object(obj) = v {
                let t = obj.get("total").and_then(Value::as_i64).unwrap_or(0);
                let c = obj.get("commented").and_then(Value::as_i64).unwrap_or(0);
                let cv = if t > 0 { (c as f64 / t as f64 * 10000.0).round() / 100.0 } else { 0.0 };
                obj.insert("coverage".to_string(),
                           Value::Number(serde_json::Number::from_f64(cv).unwrap_or(0.into())));
            }
        }
        if group_by == "module" {
            result.insert("by_module".to_string(), Value::Object(modules));
        } else {
            result.insert("by_file".to_string(), Value::Object(modules));
        }
    }

    Ok(Value::Object(result))
}

/// `get_call_heatmap` —— 函数调用频率热力图。
///
/// 复刻 analyzers/coverage.py `get_call_heatmap`。group_by ∈ {module, file}，
/// 其他值返回空数组（Python 仅处理这两个分支）。
pub fn handle_get_call_heatmap(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let group_by = get_str_param_or(params, "group_by", "module");
    let top_n = get_int_param_or(params, "top_n", 20).clamp(1, MAX_RESULT_ROWS);
    if group_by != "module" && group_by != "file" {
        return Ok(Value::Array(vec![]));
    }

    let sql = if group_by == "module" {
        String::from(
            "SELECT fsv.module_path AS group_key, \
                    COUNT(*) AS total_calls_in, \
                    COUNT(DISTINCT cv.caller_qualified) AS unique_callers, \
                    COUNT(DISTINCT cv.callee_qualified) AS unique_callees \
             FROM call_versions cv \
             JOIN file_versions fv ON cv.file_version_id = fv.id \
             JOIN file_instances fi ON fv.file_instance_id = fi.id \
             JOIN file_symbol_versions fsv ON fsv.qualified_name = cv.callee_qualified \
                  AND fsv.file_version_id = fv.id \
             WHERE fi.workspace_id = ?1 AND fv.is_current = 1 \
               AND cv.callee_qualified != '' AND fsv.module_path != '' \
             GROUP BY fsv.module_path ORDER BY total_calls_in DESC LIMIT ?2",
        )
    } else {
        String::from(
            "SELECT fi.rel_path AS group_key, \
                    COUNT(*) AS total_calls_in, \
                    COUNT(DISTINCT cv.caller_qualified) AS unique_callers, \
                    COUNT(DISTINCT cv.callee_qualified) AS unique_callees \
             FROM call_versions cv \
             JOIN file_versions fv ON cv.file_version_id = fv.id \
             JOIN file_symbol_versions fsv ON fsv.qualified_name = cv.callee_qualified \
                  AND fsv.file_version_id = fv.id \
             JOIN file_instances fi ON fv.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND fv.is_current = 1 \
               AND cv.callee_qualified != '' \
             GROUP BY fi.rel_path ORDER BY total_calls_in DESC LIMIT ?2",
        )
    };

    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("call_heatmap prepare: {e}")))?;
    // Python 取 top_n*2 再截断 top_n（保持等价输出上限）
    let fetch_limit = (top_n * 2).clamp(1, MAX_RESULT_ROWS);
    let rows: Vec<Value> = stmt
        .query_map(rusqlite::params![workspace_id, fetch_limit], |row: &rusqlite::Row<'_>| {
            Ok(json!({
                "group": row.get::<_, String>(0)?,
                "total_calls": row.get::<_, i64>(1)?,
                "unique_callers": row.get::<_, i64>(2)?,
                "unique_callees": row.get::<_, i64>(3)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("call_heatmap query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("call_heatmap query: {e}")))?;

    let truncated: Vec<Value> = rows.into_iter().take(top_n as usize).collect();
    Ok(Value::Array(truncated))
}

/// `find_uncovered_functions` —— 覆盖率低于阈值的函数。
///
/// 复刻 db/db_coverage.py `find_uncovered_functions`。数据源使用 snapshot
/// 同构主表（symbols/file_instances/coverage_data；与 metrics_handlers 同款）。
pub fn handle_find_uncovered_functions(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let module_filter = get_str_param_or(params, "module_filter", "");
    let threshold = get_int_param_or(params, "threshold", 50).clamp(0, 100);
    let mut sql = String::from(
        "SELECT s.id, s.qualified_name, s.start_line, s.end_line, s.module_path, fi.rel_path, \
                COUNT(cd.id) AS tracked_lines, \
                COALESCE(SUM(CASE WHEN cd.hit_count > 0 THEN 1 ELSE 0 END), 0) AS covered_lines \
         FROM symbols s \
         JOIN file_instances fi ON s.file_instance_id = fi.id \
         LEFT JOIN coverage_data cd ON cd.symbol_id = s.id \
         WHERE fi.workspace_id = ?1 \
           AND s.kind IN ('fn','function','method')",
    );
    if !module_filter.is_empty() {
        sql.push_str(" AND s.module_path LIKE ?2 ESCAPE '\\'");
    }
    sql.push_str(" GROUP BY s.id HAVING tracked_lines > 0");
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("uncovered prepare: {e}")))?;
    let rows: Result<
        Vec<(String, String, String, i64, i64, i64, i64)>,
        rusqlite::Error,
    > = if module_filter.is_empty() {
        stmt.query_map(rusqlite::params![workspace_id], |row: &rusqlite::Row<'_>| {
            Ok((
                row.get::<_, String>(1)?,
                row.get::<_, String>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, i64>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, i64>(6)?,
                row.get::<_, i64>(7)?,
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("uncovered query: {e}")))?
        .collect()
    } else {
        let pattern = format!("%{}%", module_filter.replace('\\', "\\\\").replace('%', "\\%"));
        stmt.query_map(rusqlite::params![workspace_id, pattern], |row: &rusqlite::Row<'_>| {
            Ok((
                row.get::<_, String>(1)?,
                row.get::<_, String>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, i64>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, i64>(6)?,
                row.get::<_, i64>(7)?,
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("uncovered query: {e}")))?
        .collect()
    };
    let rows = rows.map_err(|e| DaemonRpcError::internal_error(format!("uncovered query: {e}")))?;

    let mut out: Vec<Value> = Vec::new();
    for (qualified_name, module_path, file_path, start_line, end_line, tracked, covered) in rows {
        let pct = if tracked > 0 {
            (covered as f64 / tracked as f64) * 100.0
        } else {
            0.0
        };
        if pct < threshold as f64 {
            out.push(json!({
                "qualified_name": qualified_name,
                "file_path": file_path,
                "module_path": module_path,
                "start_line": start_line,
                "end_line": end_line,
                "tracked_lines": tracked,
                "covered_lines": covered,
                "coverage_pct": (pct * 10.0).round() / 10.0,
            }));
        }
    }
    out.sort_by(|a, b| {
        let pa = a.get("coverage_pct").and_then(Value::as_f64).unwrap_or(0.0);
        let pb = b.get("coverage_pct").and_then(Value::as_f64).unwrap_or(0.0);
        pa.partial_cmp(&pb).unwrap_or(std::cmp::Ordering::Equal)
    });
    Ok(Value::Array(out))
}

/// 便捷辅助：scalar_i64（模块内复用，与 metrics_handlers 等价但独立实现）。
#[allow(dead_code)]
fn _scalar_i64(conn: &Connection, sql: &str, workspace_id: i64) -> Result<i64, DaemonRpcError> {
    scalar_i64(conn, sql, workspace_id)
}

// ====================================================================
// S2（P0-compat 批次 2）：toolchain 组（list_toolchains / get_toolchain /
// get_workspace_toolchains）。数据源为权威 task DB（用户级 callwarden.db，
// 经 SnapshotDaemonState::open_task_db_readonly 提供只读连接），与 Python
// compat worker 的 ctx.conn 同源。SQL 复刻 db/db_toolchain.py。
// ====================================================================

/// 把 toolchains 行转换为 JSON 对象（复刻 db_toolchain._row_to_toolchain.to_dict）。
fn toolchain_row_to_json(row: &rusqlite::Row<'_>) -> Result<Value, rusqlite::Error> {
    let include_dirs_raw: String = row.get(7)?;
    let macros_raw: String = row.get(8)?;
    let include_dirs = serde_json::from_str::<Vec<Value>>(&include_dirs_raw).unwrap_or_default();
    let predefined_macros =
        serde_json::from_str::<Map<String, Value>>(&macros_raw).unwrap_or_default();
    let created_at: f64 = row.get(10)?;
    let updated_at: f64 = row.get(11)?;
    let description: String = row.get(12)?;
    Ok(json!({
        "id": row.get::<_, i64>(0)?,
        "name": row.get::<_, String>(1)?,
        "compiler_path": row.get::<_, String>(2)?,
        "compiler_type": row.get::<_, String>(3)?,
        "version": row.get::<_, String>(4)?,
        "target_triple": row.get::<_, String>(5)?,
        "sysroot": row.get::<_, String>(6)?,
        "include_dirs": include_dirs,
        "predefined_macros": predefined_macros,
        "fingerprint": row.get::<_, String>(9)?,
        "created_at": created_at,
        "updated_at": updated_at,
        "description": description,
    }))
}

/// `list_toolchains` —— 列出所有工具链。
///
/// 复刻 db/db_toolchain.py `list_toolchains`（SELECT * FROM toolchains ORDER BY created_at）。
pub fn handle_list_toolchains(
    conn: &Connection,
    _params: &Value,
) -> Result<Value, DaemonRpcError> {
    let mut stmt = conn
        .prepare("SELECT * FROM toolchains ORDER BY created_at")
        .map_err(|e| DaemonRpcError::internal_error(format!("list_toolchains prepare: {e}")))?;
    let rows = stmt
        .query_map([], toolchain_row_to_json)
        .map_err(|e| DaemonRpcError::internal_error(format!("list_toolchains query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("list_toolchains query: {e}")))?;
    Ok(Value::Array(rows))
}

/// `get_toolchain` —— 按 name 或 id 查询工具链。
///
/// 复刻 db/db_toolchain.py `get_toolchain`。
pub fn handle_get_toolchain(
    conn: &Connection,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let name_or_id = get_str_param_or(params, "name_or_id", "");
    // 兼容整数 id 与字符串 name（Python 按 isinstance 分派）
    let sql = if name_or_id.parse::<i64>().is_ok() {
        "SELECT * FROM toolchains WHERE id = ?1"
    } else {
        "SELECT * FROM toolchains WHERE name = ?1"
    };
    let mut stmt = conn
        .prepare(sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("get_toolchain prepare: {e}")))?;
    let mut rows_iter = stmt
        .query(rusqlite::params![name_or_id])
        .map_err(|e| DaemonRpcError::internal_error(format!("get_toolchain query: {e}")))?;
    let mut out: Vec<Value> = Vec::new();
    while let Some(row) = rows_iter
        .next()
        .map_err(|e| DaemonRpcError::internal_error(format!("get_toolchain query: {e}")))?
    {
        out.push(
            toolchain_row_to_json(&row)
                .map_err(|e| DaemonRpcError::internal_error(format!("get_toolchain row: {e}")))?,
        );
    }
    match out.into_iter().next() {
        Some(v) => Ok(v),
        None => Ok(Value::Null), // Python 返回 None（工具链不存在）
    }
}

/// `get_workspace_toolchains` —— 获取 workspace 绑定的工具链列表。
///
/// 复刻 db/db_toolchain.py `get_workspace_toolchains`。workspace_id 为任务库
/// workspaces.id（Python 侧语义；build_context_hash 可选，None 返回全部绑定）。
pub fn handle_get_workspace_toolchains(
    conn: &Connection,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let workspace_id = get_int_param_or(params, "workspace_id", 0);
    let build_context_hash = get_str_param(params, "build_context_hash");
    let (sql, bind): (String, Vec<rusqlite::types::Value>) = if let Some(bch) = build_context_hash {
        (
            "SELECT t.* FROM toolchains t \
             JOIN workspace_toolchains wt ON t.id = wt.toolchain_id \
             WHERE wt.workspace_id = ?1 AND wt.build_context_hash = ?2"
                .to_string(),
            vec![
                rusqlite::types::Value::Integer(workspace_id),
                rusqlite::types::Value::Text(bch.to_string()),
            ],
        )
    } else {
        (
            "SELECT t.* FROM toolchains t \
             JOIN workspace_toolchains wt ON t.id = wt.toolchain_id \
             WHERE wt.workspace_id = ?1"
                .to_string(),
            vec![rusqlite::types::Value::Integer(workspace_id)],
        )
    };
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("workspace_toolchains prepare: {e}")))?;
    let mut rows_iter = stmt
        .query(rusqlite::params_from_iter(bind.iter()))
        .map_err(|e| DaemonRpcError::internal_error(format!("workspace_toolchains query: {e}")))?;
    let mut out: Vec<Value> = Vec::new();
    while let Some(row) = rows_iter
        .next()
        .map_err(|e| DaemonRpcError::internal_error(format!("workspace_toolchains query: {e}")))?
    {
        out.push(toolchain_row_to_json(&row).map_err(|e| {
            DaemonRpcError::internal_error(format!("workspace_toolchains row: {e}"))
        })?);
    }
    Ok(Value::Array(out))
}
