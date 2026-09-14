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

use std::path::{Path, PathBuf};

use rusqlite::{Connection, OptionalExtension};
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

// ====================================================================
// P0-COMPAT-v3（T-1788963103216-cb818938）：tools_query 组（8 方法）。
// SQL/语义复刻 Python 真相源：db/db_query.py（get_symbol_history /
// get_recent_changes / export_module_graph）、db/db_comment.py
// （get_comment_from_version）、analyzers/call_chain.py（get_call_chain_up）、
// analyzers/issues.py（get_function_issues / get_issue_summary + 语言规则表）、
// analyzers/coverage.py（get_test_coverage）。
// ====================================================================

use std::collections::HashSet;

/// 解析 Python `_parse_since` 时间串（"1h"、"30m"、"1d"、"2h30m"）→ 秒数。
fn parse_since_seconds(since: &str) -> i64 {
    let mut total: i64 = 0;
    let mut num_buf = String::new();
    for ch in since.chars() {
        if ch.is_ascii_digit() {
            num_buf.push(ch);
        } else {
            let num: i64 = num_buf.parse().unwrap_or(0);
            num_buf.clear();
            match ch {
                'd' => total += num * 86400,
                'h' => total += num * 3600,
                'm' => total += num * 60,
                's' => total += num,
                _ => {}
            }
        }
    }
    total
}

/// file_versions 行 + rel_path → JSON（Python `dict(row)` 等价；ast_cache BLOB
/// 以 null 输出保证 JSON 可序列化）。
fn fv_row_to_json(row: &rusqlite::Row<'_>) -> Result<Value, rusqlite::Error> {
    Ok(json!({
        "id": row.get::<_, i64>(0)?,
        "file_instance_id": row.get::<_, i64>(1)?,
        "version_num": row.get::<_, i64>(2)?,
        "content_hash": row.get::<_, String>(3)?,
        "mtime": row.get::<_, f64>(4)?,
        "total_lines": row.get::<_, i64>(5)?,
        "parsed_at": row.get::<_, f64>(6)?,
        "is_current": row.get::<_, i64>(7)?,
        "is_deleted": row.get::<_, i64>(8)?,
        "commit_hash": row.get::<_, String>(9)?,
        "ast_cache": Value::Null,
        "rel_path": row.get::<_, String>(10)?,
    }))
}

const FV_SELECT: &str = "SELECT fv.id, fv.file_instance_id, fv.version_num, fv.content_hash, \
         fv.mtime, fv.total_lines, fv.parsed_at, fv.is_current, fv.is_deleted, fv.commit_hash, \
         fi.rel_path \
         FROM file_versions fv \
         JOIN file_instances fi ON fv.file_instance_id = fi.id";

/// `get_symbol_history` —— 符号全部历史版本（复刻 db_query.get_symbol_history）。
pub fn handle_get_symbol_history(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let qualified_name = get_str_param_or(params, "qualified_name", "");
    let sql = "SELECT fsv.symbol_hash, fsv.qualified_name, fsv.start_line, fsv.end_line, \
               fsv.module_path, fsv.is_deleted, fv.version_num, fv.parsed_at, fv.mtime, \
               fv.is_current, fi.rel_path as file_path \
               FROM file_symbol_versions fsv \
               JOIN file_versions fv ON fsv.file_version_id = fv.id \
               JOIN file_instances fi ON fv.file_instance_id = fi.id \
               WHERE fsv.qualified_name = ?1 AND fi.workspace_id = ?2 AND fi.status != 'archived' \
               ORDER BY fv.parsed_at DESC";
    let mut stmt = conn
        .prepare(sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol_history prepare: {e}")))?;
    let rows = stmt
        .query_map(rusqlite::params![qualified_name, workspace_id], |row| {
            Ok(json!({
                "symbol_hash": row.get::<_, String>(0)?,
                "qualified_name": row.get::<_, String>(1)?,
                "start_line": row.get::<_, i64>(2)?,
                "end_line": row.get::<_, i64>(3)?,
                "module_path": row.get::<_, String>(4)?,
                "is_deleted": row.get::<_, i64>(5)?,
                "version_num": row.get::<_, i64>(6)?,
                "parsed_at": row.get::<_, f64>(7)?,
                "mtime": row.get::<_, f64>(8)?,
                "is_current": row.get::<_, i64>(9)?,
                "file_path": row.get::<_, String>(10)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol_history query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol_history query: {e}")))?;
    Ok(Value::Array(rows))
}

/// `get_recent_changes` —— 近期变化的文件和函数（复刻 db_query.get_recent_changes）。
pub fn handle_get_recent_changes(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let since = get_str_param_or(params, "since", "1d");
    let seconds = parse_since_seconds(&since);
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let cutoff = now - seconds as f64;

    let sql = format!("{FV_SELECT} WHERE fi.workspace_id = ?1 AND fv.parsed_at > ?2 \
               ORDER BY fv.parsed_at DESC");
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("recent_changes prepare: {e}")))?;
    let changed_files: Vec<Value> = stmt
        .query_map(rusqlite::params![workspace_id, cutoff], fv_row_to_json)
        .map_err(|e| DaemonRpcError::internal_error(format!("recent_changes query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("recent_changes query: {e}")))?;

    let mut changed_functions: Vec<Value> = Vec::new();
    for fv in &changed_files {
        let obj = fv.as_object().unwrap();
        let fv_id = obj.get("id").and_then(Value::as_i64).unwrap_or(0);
        let fi_id = obj.get("file_instance_id").and_then(Value::as_i64).unwrap_or(0);
        let version_num = obj.get("version_num").and_then(Value::as_i64).unwrap_or(0);
        let rel_path = obj
            .get("rel_path")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let parsed_at = obj.get("parsed_at").and_then(Value::as_f64).unwrap_or(0.0);
        if version_num <= 1 {
            continue;
        }

        // 上一版本 hash 集
        let mut prev_stmt = conn
            .prepare(
                "SELECT qualified_name, symbol_hash FROM file_symbol_versions \
                 WHERE file_version_id = (SELECT id FROM file_versions \
                 WHERE file_instance_id = ?1 AND version_num = ?2)",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("recent_changes prepare: {e}")))?;
        let prev_rows: Vec<(String, String)> = prev_stmt
            .query_map(rusqlite::params![fi_id, version_num - 1], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("recent_changes query: {e}")))?
            .collect::<Result<Vec<_>, rusqlite::Error>>()
            .map_err(|e| DaemonRpcError::internal_error(format!("recent_changes query: {e}")))?;

        // 当前版本 hash/行号/删除标记
        let mut curr_stmt = conn
            .prepare(
                "SELECT qualified_name, symbol_hash, start_line, is_deleted \
                 FROM file_symbol_versions WHERE file_version_id = ?1",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("recent_changes prepare: {e}")))?;
        let curr_rows: Vec<(String, String, i64, i64)> = curr_stmt
            .query_map(rusqlite::params![fv_id], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, i64>(2)?,
                    row.get::<_, i64>(3)?,
                ))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("recent_changes query: {e}")))?
            .collect::<Result<Vec<_>, rusqlite::Error>>()
            .map_err(|e| DaemonRpcError::internal_error(format!("recent_changes query: {e}")))?;

        let mut all_names: Vec<String> = Vec::new();
        for (n, _) in &prev_rows {
            if !all_names.contains(n) {
                all_names.push(n.clone());
            }
        }
        for (n, _, _, _) in &curr_rows {
            if !all_names.contains(n) {
                all_names.push(n.clone());
            }
        }

        for name in all_names {
            let prev_h = prev_rows
                .iter()
                .find(|(n, _)| n == &name)
                .map(|(_, h)| h.clone());
            let curr_info = curr_rows.iter().find(|(n, _, _, _)| n == &name);
            let curr_h = curr_info.map(|(_, h, _, _)| h.clone());
            let is_deleted = curr_info.map(|(_, _, _, d)| *d).unwrap_or(0);

            if prev_h != curr_h {
                let change_type = if prev_h.is_none() && is_deleted == 0 {
                    "新增"
                } else if is_deleted != 0 || curr_h.is_none() {
                    "删除"
                } else {
                    "修改"
                };
                let line = curr_info.map(|(_, _, l, _)| *l).unwrap_or(0);
                changed_functions.push(json!({
                    "qualified_name": name,
                    "file_path": rel_path,
                    "change_type": change_type,
                    "version": version_num,
                    "parsed_at": parsed_at,
                    "line": line,
                    "prev_hash": prev_h.unwrap_or_default(),
                    "curr_hash": curr_h.unwrap_or_default(),
                }));
            }
        }
    }

    Ok(json!({
        "changed_files": changed_files,
        "changed_functions": changed_functions,
        "since_seconds": seconds,
    }))
}

/// `get_impact` —— 向上调用链 BFS（复刻 analyzers/call_chain.get_call_chain_up）。
pub fn handle_get_impact(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let qualified_name = get_str_param_or(params, "qualified_name", "").to_string();
    let max_depth = get_int_param_or(params, "max_depth", 10).max(0);

    // visited 保序（Python set 无序，这里按发现序输出保证确定性）
    let mut visited: Vec<String> = vec![qualified_name.clone()];
    let mut levels: Vec<Value> = Vec::new();
    let mut current_level: Vec<String> = vec![qualified_name.clone()];

    for depth in 0..max_depth {
        let mut next_level: Vec<String> = Vec::new();
        let mut level_callers: Vec<Value> = Vec::new();

        // 500 一批分块（与 Python 相同的 IN 子句占位符上限策略）
        let callee_list: Vec<&String> = current_level.iter().filter(|c| !c.is_empty()).collect();
        for chunk in callee_list.chunks(500) {
            let placeholders: Vec<String> = (0..chunk.len()).map(|i| format!("?{}", i + 2)).collect();
            let sql = format!(
                "SELECT DISTINCT cv.caller_qualified as caller_name, \
                        cv.callee_qualified as callee_name \
                 FROM call_versions cv \
                 JOIN file_versions fv ON cv.file_version_id = fv.id \
                 JOIN file_instances fi ON fv.file_instance_id = fi.id \
                 WHERE fi.workspace_id = ?1 AND fv.is_current = 1 \
                   AND cv.callee_qualified IN ({}) AND cv.caller_qualified != ''",
                placeholders.join(",")
            );
            let mut stmt = conn.prepare(&sql).map_err(|e| {
                DaemonRpcError::internal_error(format!("call_chain_up prepare: {e}"))
            })?;
            let mut bind: Vec<rusqlite::types::Value> =
                vec![rusqlite::types::Value::Integer(workspace_id)];
            for c in chunk {
                bind.push(rusqlite::types::Value::Text((*c).clone()));
            }
            let rows = stmt
                .query_map(rusqlite::params_from_iter(bind.iter()), |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                    ))
                })
                .map_err(|e| DaemonRpcError::internal_error(format!("call_chain_up query: {e}")))?;
            for r in rows {
                let (caller, callee) = r.map_err(|e| {
                    DaemonRpcError::internal_error(format!("call_chain_up row: {e}"))
                })?;
                if !caller.is_empty() && !visited.contains(&caller) {
                    visited.push(caller.clone());
                    next_level.push(caller.clone());
                    level_callers.push(json!({
                        "caller": caller,
                        "target": callee,
                        "depth": depth + 1,
                    }));
                }
            }
        }

        if level_callers.is_empty() {
            break;
        }
        levels.push(json!({
            "depth": depth + 1,
            "count": level_callers.len(),
            "callers": level_callers,
        }));
        current_level = next_level;
        if current_level.is_empty() {
            break;
        }
    }

    let all_upstream: Vec<&String> = visited.iter().skip(1).collect();
    Ok(json!({
        "start": qualified_name,
        "max_depth_reached": levels.len(),
        "total_upstream": visited.len() - 1,
        "levels": levels,
        "all_upstream": all_upstream,
    }))
}

/// `get_comment_from_version` —— 从历史版本获取注释（复刻 db_comment.get_comment_from_version）。
pub fn handle_get_comment_from_version(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let spec = get_str_param_or(params, "spec", "");
    if !spec.contains('@') {
        return Ok(Value::Null);
    }
    let parts: Vec<&str> = spec.split('@').collect();
    let qualified_name = parts[0];
    let version_ref = parts[1];

    // 复用 get_symbol_history 查询
    let history = handle_get_symbol_history(
        conn,
        workspace_id,
        &json!({ "qualified_name": qualified_name }),
    )?;

    let history_arr = history.as_array().cloned().unwrap_or_default();
    let mut target: Option<&Value> = None;
    if let Some(num_txt) = version_ref.strip_prefix('v') {
        if let Ok(version_num) = num_txt.parse::<i64>() {
            target = history_arr
                .iter()
                .find(|h| h.get("version_num").and_then(Value::as_i64) == Some(version_num));
        }
    }
    if target.is_none() {
        target = history_arr.iter().find(|h| {
            h.get("symbol_hash")
                .and_then(Value::as_str)
                .map(|h| h.starts_with(version_ref))
                .unwrap_or(false)
        });
    }
    let target = match target {
        Some(t) => t,
        None => return Ok(Value::Null),
    };
    let symbol_hash = target.get("symbol_hash").and_then(Value::as_str).unwrap_or("");

    let mut stmt = conn
        .prepare("SELECT content, has_comment, comment_content FROM symbol_contents \
                  WHERE content_hash = ?1")
        .map_err(|e| DaemonRpcError::internal_error(format!("comment_from_version prepare: {e}")))?;
    let content = stmt
        .query_row(rusqlite::params![symbol_hash], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, String>(2)?,
            ))
        })
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("comment_from_version query: {e}")))?;
    let (full_content, has_comment, comment_content) = match content {
        Some(v) => v,
        None => return Ok(Value::Null),
    };

    Ok(json!({
        "qualified_name": qualified_name,
        "version_num": target.get("version_num").cloned().unwrap_or(Value::Null),
        "symbol_hash": symbol_hash,
        "file_path": target.get("file_path").cloned().unwrap_or(Value::Null),
        "start_line": target.get("start_line").cloned().unwrap_or(Value::Null),
        "end_line": target.get("end_line").cloned().unwrap_or(Value::Null),
        "comment_content": comment_content,
        "has_comment": has_comment,
        "full_content": full_content,
    }))
}

// ---- 缺陷检测规则表（复刻 analyzers/issues.py；每条 = key/label/severity/pattern/desc）----

struct IssueRule {
    key: &'static str,
    label: &'static str,
    severity: &'static str,
    pattern: &'static str,
    desc: &'static str,
}

const COMMON_ISSUE_RULES: &[IssueRule] = &[
    IssueRule { key: "todo_fixme", label: "TODO/FIXME", severity: "warn",
        pattern: r"\b(TODO|FIXME|HACK|XXX|WORKAROUND)\b",
        desc: "代码中包含 TODO/FIXME/HACK 等未完成标记" },
    IssueRule { key: "hardcoded_path", label: "硬编码路径", severity: "warn",
        pattern: r#""(?:src|docs|scripts|tests|target|\.cargo|\.git|/tmp/|/usr/|/etc/|C://|/home/)[//w/-/.//]*""#,
        desc: "硬编码了文件路径，建议使用配置文件或环境变量" },
    IssueRule { key: "hardcoded_url", label: "硬编码 URL", severity: "info",
        pattern: r#""https?://[^\s"]+""#,
        desc: "硬编码了 URL，建议使用配置文件" },
    IssueRule { key: "magic_number", label: "魔法数字", severity: "info",
        pattern: r"\b\d{4,}\b",
        desc: "硬编码的大数字常量（4 位以上），建议定义为具名常量" },
];

const RUST_ISSUE_RULES: &[IssueRule] = &[
    IssueRule { key: "unwrap_call", label: "unwrap 调用", severity: "danger",
        pattern: r"\.unwrap\(\)", desc: "使用 unwrap()，可能导致 panic" },
    IssueRule { key: "expect_call", label: "expect 调用", severity: "warn",
        pattern: r"\.expect\(", desc: "使用 expect()，可能导致 panic" },
    IssueRule { key: "panic_macro", label: "panic!/unimplemented!", severity: "danger",
        pattern: r"\bpanic!\(|\bunimplemented!\(|\btodo!\(\)",
        desc: "包含 panic!/unimplemented!/todo!() 占位代码" },
    IssueRule { key: "unsafe_block", label: "unsafe 块", severity: "warn",
        pattern: r"\bunsafe\s*\{|\bunsafe\s+fn\b|\bunsafe\s+impl\b",
        desc: "包含 unsafe 代码块" },
    IssueRule { key: "clone_heavy", label: "频繁 clone", severity: "info",
        pattern: r"\.clone\(\)", desc: "频繁使用 clone()，可能影响性能" },
];

const TYPESCRIPT_ISSUE_RULES: &[IssueRule] = &[
    IssueRule { key: "any_type", label: "any 类型", severity: "warn",
        pattern: r":\s*any\b|\bas\s+any\b", desc: "使用 any 类型，失去类型安全" },
    IssueRule { key: "console_log", label: "console.log", severity: "info",
        pattern: r"console\.(log|error|warn|debug)\(",
        desc: "包含 console 调试输出，建议移除" },
    IssueRule { key: "ts_ignore", label: "@ts-ignore", severity: "warn",
        pattern: r"@ts-ignore|@ts-nocheck", desc: "使用 @ts-ignore 跳过类型检查" },
    IssueRule { key: "non_null_assertion", label: "非空断言", severity: "warn",
        pattern: r"\w!\s*[.\(\)]", desc: "使用非空断言操作符 (!)，可能导致运行时错误" },
];

const PYTHON_ISSUE_RULES: &[IssueRule] = &[
    IssueRule { key: "bare_except", label: "裸 except", severity: "danger",
        pattern: r"except\s*:", desc: "使用裸 except，会捕获所有异常包括 SystemExit" },
    IssueRule { key: "print_debug", label: "print 调试", severity: "info",
        pattern: r"\bprint\(", desc: "包含 print 调试输出，建议使用 logging" },
    IssueRule { key: "broad_except", label: "宽泛异常", severity: "warn",
        pattern: r"except\s+Exception\s*:", desc: "捕获 Exception 过于宽泛，建议精确捕获" },
    IssueRule { key: "mutable_default", label: "可变默认参数", severity: "warn",
        pattern: r"def\s+\w+\([^)]*=\s*(\[\]|\{\})",
        desc: "使用可变对象作为默认参数，可能导致意外行为" },
];

const KOTLIN_ISSUE_RULES: &[IssueRule] = &[
    IssueRule { key: "!!_operator", label: "!! 非空断言", severity: "warn",
        pattern: r"\w\s*!!", desc: "使用 !! 非空断言，可能导致 NullPointerException" },
    IssueRule { key: "println_debug", label: "println 调试", severity: "info",
        pattern: r"\bprintln\(", desc: "包含 println 调试输出" },
    IssueRule { key: "unsafe_cast", label: "不安全类型转换", severity: "warn",
        pattern: r"\bas\s+\w", desc: "使用不安全的类型转换，建议用安全转换 as?" },
];

const GO_ISSUE_RULES: &[IssueRule] = &[
    IssueRule { key: "nil_check_missing", label: "缺少 nil 检查", severity: "warn",
        pattern: r"\.([A-Z]\w*)\(", desc: "可能缺少 nil 检查（调用方法前未判断 nil）" },
    IssueRule { key: "err_ignored", label: "忽略错误返回", severity: "danger",
        pattern: r"_\s*=\s*\w+\.\w+\(", desc: "使用 _ 忽略了错误返回值，建议处理错误" },
    IssueRule { key: "fmt_print_debug", label: "fmt.Print 调试", severity: "info",
        pattern: r"fmt\.Print(ln|f)?\(", desc: "包含 fmt.Print 调试输出，建议使用日志库" },
    IssueRule { key: "panic_call", label: "panic 调用", severity: "danger",
        pattern: r"\bpanic\(", desc: "使用 panic()，可能导致程序崩溃" },
    IssueRule { key: "unsafe_import", label: "unsafe 包", severity: "warn",
        pattern: r#""unsafe""#, desc: "导入了 unsafe 包，可能存在内存安全风险" },
];

const JAVA_ISSUE_RULES: &[IssueRule] = &[
    IssueRule { key: "print_stack_trace", label: "printStackTrace", severity: "warn",
        pattern: r"\.printStackTrace\(", desc: "使用 printStackTrace()，建议使用日志框架" },
    IssueRule { key: "system_out_print", label: "System.out 调试", severity: "info",
        pattern: r"System\.out\.print(ln)?\(", desc: "包含 System.out 调试输出，建议使用日志框架" },
    IssueRule { key: "empty_catch", label: "空 catch 块", severity: "warn",
        pattern: r"catch\s*\([^)]+\)\s*\{[\s;]*\}", desc: "空的 catch 块，异常被静默吞掉" },
    IssueRule { key: "raw_type", label: "原始类型", severity: "warn",
        pattern: r"\b(List|Map|Set|Collection|Iterator|Comparable)\s*[^<\w]",
        desc: "使用原始类型（未指定泛型参数），失去类型安全" },
    IssueRule { key: "magic_suppress", label: "@SuppressWarnings", severity: "info",
        pattern: r"@SuppressWarnings", desc: "使用 @SuppressWarnings 抑制警告" },
];

const C_CPP_ISSUE_RULES: &[IssueRule] = &[
    IssueRule { key: "printf_debug", label: "printf 调试", severity: "info",
        pattern: r"\bprintf\(|\bfprintf\(", desc: "包含 printf/fprintf 调试输出" },
    IssueRule { key: "unsafe_malloc", label: "malloc 不检查", severity: "danger",
        pattern: r"=\s*malloc\s*\(", desc: "调用 malloc 后未检查返回值是否为 NULL" },
    IssueRule { key: "unsafe_free", label: "野指针风险", severity: "warn",
        pattern: r"free\s*\([^)]+\);", desc: "free 后未置空指针，可能导致野指针" },
    IssueRule { key: "goto_statement", label: "goto 语句", severity: "warn",
        pattern: r"\bgoto\s+\w+", desc: "使用 goto 语句，影响代码可读性" },
    IssueRule { key: "char_buffer", label: "char 数组溢出", severity: "warn",
        pattern: r"char\s+\w+\s*\[\d+\]", desc: "固定大小 char 数组，可能存在缓冲区溢出风险" },
    IssueRule { key: "void_star", label: "void* 指针", severity: "warn",
        pattern: r"void\s*\*", desc: "使用 void* 指针，失去类型安全" },
    IssueRule { key: "define_macro", label: "宏定义", severity: "info",
        pattern: r"#define\s+\w+", desc: "使用宏定义，建议用 const/constexpr 替代" },
];

/// 复刻 `LANGUAGE_RULES_MAP.get(language, [])`（语言 → 专用规则）。
fn rules_for_language(language: &str) -> &'static [IssueRule] {
    match language {
        "rust" => RUST_ISSUE_RULES,
        "typescript" | "javascript" => TYPESCRIPT_ISSUE_RULES,
        "python" => PYTHON_ISSUE_RULES,
        "kotlin" => KOTLIN_ISSUE_RULES,
        "go" => GO_ISSUE_RULES,
        "java" => JAVA_ISSUE_RULES,
        "c" | "cpp" => C_CPP_ISSUE_RULES,
        _ => &[],
    }
}

/// 复刻 `_get_all_issue_rules`（通用 + 各语言专用，按 key 首现去重；
/// 语言遍历序 = Python dict 插入序：rust/typescript/python/kotlin/go/java/c/cpp）。
fn all_issue_rules() -> Vec<&'static IssueRule> {
    let mut seen: HashSet<&str> = HashSet::new();
    let mut out: Vec<&'static IssueRule> = Vec::new();
    for r in COMMON_ISSUE_RULES {
        if seen.insert(r.key) {
            out.push(r);
        }
    }
    for rules in [
        RUST_ISSUE_RULES,
        TYPESCRIPT_ISSUE_RULES,
        PYTHON_ISSUE_RULES,
        KOTLIN_ISSUE_RULES,
        GO_ISSUE_RULES,
        JAVA_ISSUE_RULES,
        C_CPP_ISSUE_RULES,
    ] {
        for r in rules {
            if seen.insert(r.key) {
                out.push(r);
            }
        }
    }
    out
}

/// 复刻 `_detect_language_from_module_path`（扩展名优先 → "::" → 兜底 rust）。
fn detect_language_from_module_path(module_path: &str) -> &'static str {
    if module_path.is_empty() {
        return "rust";
    }
    let mp = module_path.to_lowercase();
    if mp.contains(".py") {
        return "python";
    }
    if mp.contains(".go") {
        return "go";
    }
    if mp.contains(".java") {
        return "java";
    }
    if mp.contains(".tsx") || mp.contains(".ts") {
        return "typescript";
    }
    if mp.contains(".jsx") || mp.contains(".js") {
        return "javascript";
    }
    if mp.contains(".cpp") || mp.contains(".cc") || mp.contains(".cxx") {
        return "cpp";
    }
    if mp.contains(".kt") {
        return "kotlin";
    }
    if mp.contains(".rs") {
        return "rust";
    }
    if mp.contains(".c") {
        return "c";
    }
    if mp.contains("::") {
        return "rust";
    }
    "rust"
}

fn compile_rules<'a>(rules: &[&'a IssueRule]) -> Vec<(&'a IssueRule, regex::Regex)> {
    rules
        .iter()
        .filter_map(|r| {
            regex::RegexBuilder::new(r.pattern)
                .multi_line(true)
                .build()
                .ok()
                .map(|re| (*r, re))
        })
        .collect()
}

/// 对单个函数内容跑一组规则，返回命中的 issues 数组（missing_comment 特判）。
fn eval_rules_for_content(
    compiled: &[(&IssueRule, regex::Regex)],
    content: &str,
    has_comment: i64,
    issue_filter: &str,
) -> Vec<Value> {
    let mut issues: Vec<Value> = Vec::new();
    for (rule, re) in compiled {
        if !issue_filter.is_empty() && rule.key != issue_filter {
            continue;
        }
        if rule.key == "missing_comment" {
            if has_comment == 0 {
                let line_count = content.matches('\n').count() as i64;
                if line_count >= 3 {
                    issues.push(json!({
                        "type": rule.key, "label": rule.label,
                        "severity": rule.severity, "count": 1,
                        "description": rule.desc,
                    }));
                }
            }
        } else {
            let count = re.find_iter(content).count() as i64;
            if count > 0 {
                issues.push(json!({
                    "type": rule.key, "label": rule.label,
                    "severity": rule.severity, "count": count,
                    "description": rule.desc,
                }));
            }
        }
    }
    issues
}

const ISSUES_SELECT: &str = "SELECT DISTINCT fsv.qualified_name, fsv.module_path, sc.content, \
         sc.has_comment, sc.name \
         FROM file_symbol_versions fsv \
         JOIN file_versions fv ON fsv.file_version_id = fv.id \
         JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash \
         JOIN file_instances fi ON fv.file_instance_id = fi.id \
         WHERE fv.is_current = 1 AND sc.kind = 'fn' AND fi.workspace_id = ?1";

/// `find_issues` —— 函数缺陷检测（复刻 analyzers/issues.get_function_issues；
/// Python 真相源未按 workspace 过滤（单库时代遗留），此处补 fi.workspace_id
/// 过滤防多 workspace 泄漏——one-sqlite-per-host 下 Python 行为本身是缺陷）。
pub fn handle_find_issues(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let qualified_name = get_str_param_or(params, "qualified_name", "");
    let module_filter = get_str_param_or(params, "module_filter", "");
    let issue_filter = get_str_param_or(params, "issue_type", "");
    let limit = get_int_param_or(params, "limit", 30).max(0);

    let mut sql = String::from(ISSUES_SELECT);
    if !qualified_name.is_empty() {
        sql.push_str(" AND fsv.qualified_name = ?2");
    }
    if !module_filter.is_empty() {
        sql.push_str(" AND fsv.module_path LIKE ?3 ESCAPE '\\'");
    }
    if qualified_name.is_empty() {
        sql.push_str(" AND fsv.module_path NOT LIKE '%::tests' AND sc.name NOT LIKE 'test_%'");
    }
    let fetch_limit = limit * 5;
    sql.push_str(&format!(" LIMIT {fetch_limit}"));

    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("find_issues prepare: {e}")))?;
    let pattern = format!("%{}%", module_filter.replace('\\', "\\\\").replace('%', "\\%"));
    type IssueRow = (String, String, String, i64, String);
    let map_row = |row: &rusqlite::Row<'_>| -> rusqlite::Result<IssueRow> {
        Ok((
            row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?,
        ))
    };
    let rows: Vec<IssueRow> = if !qualified_name.is_empty() && !module_filter.is_empty() {
        stmt.query_map(
            rusqlite::params![workspace_id, qualified_name, pattern],
            map_row,
        )
    } else if !qualified_name.is_empty() {
        stmt.query_map(rusqlite::params![workspace_id, qualified_name], map_row)
    } else if !module_filter.is_empty() {
        stmt.query_map(rusqlite::params![workspace_id, pattern], map_row)
    } else {
        stmt.query_map(rusqlite::params![workspace_id], map_row)
    }
    .map_err(|e| DaemonRpcError::internal_error(format!("find_issues query: {e}")))?
    .collect::<Result<Vec<_>, rusqlite::Error>>()
    .map_err(|e| DaemonRpcError::internal_error(format!("find_issues query: {e}")))?;

    // 按行语言惰性编译规则（同一语言复用缓存）
    let mut rule_cache: std::collections::HashMap<&'static str, Vec<(&'static IssueRule, regex::Regex)>> =
        std::collections::HashMap::new();

    let mut results: Vec<Value> = Vec::new();
    for (qn, module_path, content, has_comment, name) in rows {
        let lang = detect_language_from_module_path(&module_path);
        let compiled = rule_cache
            .entry(lang)
            .or_insert_with(|| compile_rules(&rules_for_language(lang).iter().collect::<Vec<_>>()));
        let issues = eval_rules_for_content(compiled, &content, has_comment, &issue_filter);
        if !issues.is_empty() {
            results.push(json!({
                "qualified_name": qn,
                "module_path": module_path,
                "name": name,
                "issue_count": issues.len(),
                "issues": issues,
            }));
        }
    }

    results.sort_by(|a, b| {
        let ca = a.get("issue_count").and_then(Value::as_i64).unwrap_or(0);
        let cb = b.get("issue_count").and_then(Value::as_i64).unwrap_or(0);
        cb.cmp(&ca)
    });
    results.truncate(limit as usize);
    Ok(Value::Array(results))
}

/// `get_issue_summary` —— 缺陷类型汇总（复刻 analyzers/issues.get_issue_summary）。
pub fn handle_get_issue_summary(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let module_filter = get_str_param_or(params, "module_filter", "");

    let mut sql = String::from(ISSUES_SELECT);
    sql.push_str(" AND fsv.module_path NOT LIKE '%::tests' AND sc.name NOT LIKE 'test_%'");
    if !module_filter.is_empty() {
        sql.push_str(" AND fsv.module_path LIKE ?2 ESCAPE '\\'");
    }

    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("issue_summary prepare: {e}")))?;
    let pattern = format!("%{}%", module_filter.replace('\\', "\\\\").replace('%', "\\%"));
    type SummaryRow = (String, String, String, i64);
    let map_row = |row: &rusqlite::Row<'_>| -> rusqlite::Result<SummaryRow> {
        Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
    };
    let rows: Vec<SummaryRow> = if !module_filter.is_empty() {
        stmt.query_map(rusqlite::params![workspace_id, pattern], map_row)
    } else {
        stmt.query_map(rusqlite::params![workspace_id], map_row)
    }
    .map_err(|e| DaemonRpcError::internal_error(format!("issue_summary query: {e}")))?
    .collect::<Result<Vec<_>, rusqlite::Error>>()
    .map_err(|e| DaemonRpcError::internal_error(format!("issue_summary query: {e}")))?;

    // 统计基座：全部规则全集（key 去重，保持 Python 插入序）
    let all_rules = all_issue_rules();
    let mut stats: Vec<(String, i64, i64)> = Vec::new(); // (key, function_count, total_occurrences)
    let mut stat_meta: Vec<(&str, &str, &str, &str)> = Vec::new(); // (key,label,severity,desc)
    for r in &all_rules {
        stats.push((r.key.to_string(), 0, 0));
        stat_meta.push((r.key, r.label, r.severity, r.desc));
    }

    let mut rule_cache: std::collections::HashMap<&'static str, Vec<(&'static IssueRule, regex::Regex)>> =
        std::collections::HashMap::new();

    let mut total_functions: i64 = 0;
    let mut functions_with_issues: i64 = 0;

    for (_qn, module_path, content, has_comment) in rows {
        total_functions += 1;
        let lang = detect_language_from_module_path(&module_path);
        let compiled = rule_cache
            .entry(lang)
            .or_insert_with(|| compile_rules(&rules_for_language(lang).iter().collect::<Vec<_>>()));
        let mut has_any_issue = false;
        for (rule, re) in compiled.iter() {
            let idx = match stats.iter().position(|(k, _, _)| k == rule.key) {
                Some(i) => i,
                None => continue,
            };
            if rule.key == "missing_comment" {
                if has_comment == 0 {
                    let line_count = content.matches('\n').count() as i64;
                    if line_count >= 3 {
                        stats[idx].1 += 1;
                        stats[idx].2 += 1;
                        has_any_issue = true;
                    }
                }
            } else {
                let count = re.find_iter(&content).count() as i64;
                if count > 0 {
                    stats[idx].1 += 1;
                    stats[idx].2 += count;
                    has_any_issue = true;
                }
            }
        }
        if has_any_issue {
            functions_with_issues += 1;
        }
    }

    let mut issue_list: Vec<Value> = Vec::new();
    for (i, (key, fc, toc)) in stats.iter().enumerate() {
        let (label, severity, desc, _d) = stat_meta[i];
        let ratio = if total_functions > 0 {
            (*fc as f64 / total_functions as f64 * 100.0 * 10.0).round() / 10.0
        } else {
            0.0
        };
        issue_list.push(json!({
            "type": key,
            "label": label,
            "severity": severity,
            "description": desc,
            "function_count": fc,
            "total_occurrences": toc,
            "ratio": ratio,
        }));
    }
    issue_list.sort_by(|a, b| {
        let ca = a.get("function_count").and_then(Value::as_i64).unwrap_or(0);
        let cb = b.get("function_count").and_then(Value::as_i64).unwrap_or(0);
        cb.cmp(&ca)
    });

    let issue_free = total_functions - functions_with_issues;
    let free_ratio = if total_functions > 0 {
        (issue_free as f64 / total_functions as f64 * 100.0 * 10.0).round() / 10.0
    } else {
        0.0
    };
    Ok(json!({
        "total_functions": total_functions,
        "functions_with_issues": functions_with_issues,
        "issue_free_functions": issue_free,
        "issue_free_ratio": free_ratio,
        "issues": issue_list,
    }))
}

/// `get_test_coverage` —— 测试函数分布统计（复刻 analyzers/coverage.get_test_coverage）。
pub fn handle_get_test_coverage(
    conn: &Connection,
    workspace_id: i64,
    _params: &Value,
) -> Result<Value, DaemonRpcError> {
    let total_fns: i64 = conn
        .query_row(
            "SELECT COUNT(DISTINCT fsv.qualified_name) as count \
             FROM file_symbol_versions fsv \
             JOIN file_versions fv ON fsv.file_version_id = fv.id \
             JOIN file_instances fi ON fv.file_instance_id = fi.id \
             JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash \
             WHERE fi.workspace_id = ?1 AND fv.is_current = 1 AND sc.kind = 'fn'",
            rusqlite::params![workspace_id],
            |r| r.get(0),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("test_coverage query: {e}")))?;

    let test_fns: i64 = conn
        .query_row(
            "SELECT COUNT(DISTINCT fsv.qualified_name) as count \
             FROM file_symbol_versions fsv \
             JOIN file_versions fv ON fsv.file_version_id = fv.id \
             JOIN file_instances fi ON fv.file_instance_id = fi.id \
             JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash \
             WHERE fi.workspace_id = ?1 AND fv.is_current = 1 AND sc.kind = 'fn' \
               AND (fsv.module_path LIKE '%::tests' OR sc.name LIKE 'test_%')",
            rusqlite::params![workspace_id],
            |r| r.get(0),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("test_coverage query: {e}")))?;

    // 按模块统计（Python 原语义：test_count = 模块内全部 fn 去重数，
    // test_count2 = 命中 tests 模块/前缀的行数和，取 max 后过滤 >0）
    let sql_by_module = "SELECT fsv.module_path, \
                COUNT(DISTINCT fsv.qualified_name) as test_count, \
                SUM(CASE WHEN fsv.module_path LIKE '%::tests' OR sc.name LIKE 'test_%' \
                    THEN 1 ELSE 0 END) as test_count2 \
         FROM file_symbol_versions fsv \
         JOIN file_versions fv ON fsv.file_version_id = fv.id \
         JOIN file_instances fi ON fv.file_instance_id = fi.id \
         JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash \
         WHERE fi.workspace_id = ?1 AND fv.is_current = 1 AND sc.kind = 'fn' \
         GROUP BY fsv.module_path \
         HAVING test_count > 0 OR test_count2 > 0 \
         ORDER BY test_count2 DESC, test_count DESC";
    let mut stmt = conn
        .prepare(sql_by_module)
        .map_err(|e| DaemonRpcError::internal_error(format!("test_coverage prepare: {e}")))?;
    let module_rows: Vec<(String, i64, i64)> = stmt
        .query_map(rusqlite::params![workspace_id], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("test_coverage query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("test_coverage query: {e}")))?;

    let mut test_by_module: Vec<Value> = Vec::new();
    for (module_path, tc, tc2) in module_rows {
        let count = tc.max(tc2);
        if count > 0 {
            let m = if module_path.is_empty() {
                "(unknown)".to_string()
            } else {
                module_path
            };
            test_by_module.push(json!({ "module": m, "test_count": count }));
        }
    }
    let modules_with_tests = test_by_module.len() as i64;

    let total_modules: i64 = conn
        .query_row(
            "SELECT COUNT(DISTINCT fsv.module_path) as count \
             FROM file_symbol_versions fsv \
             JOIN file_versions fv ON fsv.file_version_id = fv.id \
             JOIN file_instances fi ON fv.file_instance_id = fi.id \
             JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash \
             WHERE fi.workspace_id = ?1 AND fv.is_current = 1 AND sc.kind = 'fn' \
               AND fsv.module_path != ''",
            rusqlite::params![workspace_id],
            |r| r.get(0),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("test_coverage query: {e}")))?;

    let test_ratio = if total_fns > 0 {
        (test_fns as f64 / total_fns as f64 * 100.0 * 100.0).round() / 100.0
    } else {
        0.0
    };
    let module_coverage = if total_modules > 0 {
        (modules_with_tests as f64 / total_modules as f64 * 100.0 * 100.0).round() / 100.0
    } else {
        0.0
    };
    Ok(json!({
        "total_functions": total_fns,
        "test_functions": test_fns,
        "test_ratio": test_ratio,
        "total_modules": total_modules,
        "modules_with_tests": modules_with_tests,
        "module_coverage": module_coverage,
        "test_by_module": test_by_module,
    }))
}

/// 从限定名提取顶级模块（前 2-3 段；复刻 db_query.export_module_graph.get_top_module）。
fn get_top_module(name: &str) -> String {
    let parts: Vec<&str> = name.split("::").collect();
    if parts.len() >= 3 {
        parts[..3].join("::")
    } else if parts.len() >= 2 {
        parts[..2].join("::")
    } else {
        name.to_string()
    }
}

fn safe_module_name(mod_name: &str) -> String {
    mod_name.replace("::", "_").replace('-', "_")
}

/// `export_module_graph` —— 导出模块依赖图（复刻 db_query.export_module_graph）。
pub fn handle_export_module_graph(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let format = get_str_param_or(params, "format", "mermaid");
    if format != "mermaid" && format != "dot" {
        return Err(DaemonRpcError::invalid_params(format!(
            "不支持的格式: {format}"
        )));
    }

    let sql = "SELECT cv.caller_qualified, cv.callee_qualified, COUNT(*) as call_count \
         FROM call_versions cv \
         JOIN file_versions fv ON cv.file_version_id = fv.id \
         JOIN file_instances fi ON fv.file_instance_id = fi.id \
         WHERE fi.workspace_id = ?1 AND fv.is_current = 1 \
           AND cv.caller_qualified != '' AND cv.callee_qualified != '' \
           AND cv.caller_qualified LIKE '%::%' AND cv.callee_qualified LIKE '%::%' \
         GROUP BY cv.caller_qualified, cv.callee_qualified";
    let mut stmt = conn
        .prepare(sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("export_module_graph prepare: {e}")))?;
    let rows: Vec<(String, String, i64)> = stmt
        .query_map(rusqlite::params![workspace_id], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("export_module_graph query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("export_module_graph query: {e}")))?;

    // 聚合：插入序保持行序（Python dict 语义），稳定排序后与 Python 等价
    let mut edges: Vec<(String, String, i64)> = Vec::new(); // (caller_mod, callee_mod, call_count)
    let mut all_modules: Vec<String> = Vec::new();
    for (caller, callee, count) in rows {
        let caller_mod = get_top_module(&caller);
        let callee_mod = get_top_module(&callee);
        if caller_mod == callee_mod {
            continue;
        }
        if !all_modules.contains(&caller_mod) {
            all_modules.push(caller_mod.clone());
        }
        if !all_modules.contains(&callee_mod) {
            all_modules.push(callee_mod.clone());
        }
        match edges.iter_mut().find(|(c, e, _)| c == &caller_mod && e == &callee_mod) {
            Some(e) => e.2 += count,
            None => edges.push((caller_mod, callee_mod, count)),
        }
    }

    let mut sorted_modules = all_modules.clone();
    sorted_modules.sort();

    let mut sorted_edges = edges;
    sorted_edges.sort_by(|a, b| b.2.cmp(&a.2)); // call_count desc（稳定序保持插入序）

    let content = if format == "mermaid" {
        let mut lines: Vec<String> = vec!["flowchart TD".to_string()];
        for m in &sorted_modules {
            lines.push(format!("    {}[\"{}\"]", safe_module_name(m), m));
        }
        lines.push(String::new());
        for (caller, callee, count) in &sorted_edges {
            let cs = safe_module_name(caller);
            let es = safe_module_name(callee);
            let weight = (*count).min(10);
            let arrow = if weight < 5 {
                "-->".to_string()
            } else {
                format!("-- \"{}\" -->", count)
            };
            lines.push(format!("    {}{}{}", cs, arrow, es));
        }
        format!("{}\n", lines.join("\n"))
    } else {
        let mut lines: Vec<String> = vec![
            "digraph module_dependencies {".to_string(),
            "    rankdir=LR;".to_string(),
            "    node [shape=box, style=filled, fillcolor=lightblue];".to_string(),
            String::new(),
        ];
        for m in &sorted_modules {
            lines.push(format!("    {} [label=\"{}\"];", safe_module_name(m), m));
        }
        lines.push(String::new());
        for (caller, callee, count) in &sorted_edges {
            let cs = safe_module_name(caller);
            let es = safe_module_name(callee);
            let penwidth = ((*count as f64 / 10.0).min(5.0)).max(1.0);
            lines.push(format!(
                "    {} -> {} [label=\"{}\", penwidth={:.1}];",
                cs, es, count, penwidth
            ));
        }
        lines.push("}".to_string());
        format!("{}\n", lines.join("\n"))
    };

    Ok(Value::String(content))
}

// ============================================
// P0-COMPAT-v3（T-1788963104058-fdb2e848）：tools_task 组（8）
// ============================================
// 复刻 Python 真相源：db_task_attribution.get_symbol_change_tasks /
// db_audit_chain.verify_audit_chain + list_signing_keys /
// db_bootstrap.bootstrap_status / db_clone_detection.list_clones /
// db_clone_groups.list_clone_groups + get_clone_group_detail /
// db_tasks.task_plan_template（i18n 模板）。
// conn = workspace 快照只读连接（daemon_wb_migration 主机级单库模型下
// 含治理表，与 W4-1 get_commit_tasks 同源）。

/// 局部 f64 参数helper（dispatch.rs 目前只有 str/int 变体）。
fn get_f64_param_or(params: &Value, key: &str, default: f64) -> f64 {
    params
        .get(key)
        .and_then(Value::as_f64)
        .unwrap_or(default)
}

/// `get_symbol_change_tasks` —— 反查符号版本/符号名由哪些任务改变过。
///
/// 复刻 db_task_attribution.get_symbol_change_tasks：无 workspace 过滤
/// （Python 真相源为全局语义，task_symbol_changes.task_id 全局唯一）；
/// 两条件同时传为 AND；无条件返回空数组。
pub fn handle_get_symbol_change_tasks(
    conn: &Connection,
    _workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let symbol_hash = get_str_param_or(params, "symbol_hash", "");
    let qualified_name = get_str_param_or(params, "qualified_name", "");
    let limit = get_int_param_or(params, "limit", 50);

    let mut clauses: Vec<&str> = Vec::new();
    if !symbol_hash.is_empty() {
        clauses.push("(symbol_hash_before = ?1 OR symbol_hash_after = ?1)");
    }
    if !qualified_name.is_empty() {
        clauses.push("qualified_name = ?2");
    }
    if clauses.is_empty() {
        return Ok(Value::Array(Vec::new()));
    }
    let sql = format!(
        "SELECT id, workspace_id, task_id, step_id, edit_audit_id, change_audit_id, \
         file_path, qualified_name, symbol_name, symbol_hash_before, symbol_hash_after, \
         change_type, source, source_commit_hash, metadata, created_at \
         FROM task_symbol_changes WHERE {} ORDER BY created_at DESC, id DESC LIMIT ?3",
        clauses.join(" AND ")
    );
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol_change_tasks prepare: {e}")))?;
    let rows = stmt
        .query_map(
            rusqlite::params![symbol_hash, qualified_name, limit],
            |row| {
                Ok(json!({
                    "id": row.get::<_, i64>(0)?,
                    "workspace_id": row.get::<_, Option<i64>>(1)?,
                    "task_id": row.get::<_, String>(2)?,
                    "step_id": row.get::<_, Option<String>>(3)?,
                    "edit_audit_id": row.get::<_, Option<String>>(4)?,
                    "change_audit_id": row.get::<_, Option<String>>(5)?,
                    "file_path": row.get::<_, Option<String>>(6)?,
                    "qualified_name": row.get::<_, Option<String>>(7)?,
                    "symbol_name": row.get::<_, Option<String>>(8)?,
                    "symbol_hash_before": row.get::<_, Option<String>>(9)?,
                    "symbol_hash_after": row.get::<_, Option<String>>(10)?,
                    "change_type": row.get::<_, Option<String>>(11)?,
                    "source": row.get::<_, Option<String>>(12)?,
                    "source_commit_hash": row.get::<_, Option<String>>(13)?,
                    "metadata": row.get::<_, Option<String>>(14)?,
                    "created_at": row.get::<_, Option<f64>>(15)?,
                }))
            },
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol_change_tasks query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol_change_tasks query: {e}")))?;
    Ok(Value::Array(rows))
}

/// `audit_verify_chain` —— 审计签名链验证。
///
/// 复用 cli::security::verify_audit_chain（签名重算/链连续性/首条
/// prev_signature 语义已对齐 Python db_audit_chain）。limit<=0 语义对齐
/// Python 直传 SQL：limit=0 → LIMIT 0 空结果（零计数 dict）；limit<0 →
/// fail-closed invalid_params（SQLite 负 LIMIT = 无上限，拒绝复刻）。
/// 未知 signing_key_id 的 reason 名为 signing_key_missing（Python 记
/// signature_mismatch）；常态链（local/SHA-256）行为一致。
pub fn handle_audit_verify_chain(
    conn: &Connection,
    _workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let table_name = get_str_param_or(params, "table_name", "");
    let limit = get_int_param_or(params, "limit", 1000);
    if limit < 0 {
        return Err(DaemonRpcError::invalid_params(format!(
            "audit_verify_chain limit must be positive (got {limit})"
        )));
    }
    if limit == 0 {
        let security_level = crate::cli::security::active_audit_key(conn)
            .map(|(_, _, level)| level)
            .unwrap_or_default();
        return Ok(json!({
            "table_name": table_name,
            "total_count": 0,
            "verified_count": 0,
            "broken_count": 0,
            "broken_records": [],
            "security_level": security_level,
        }));
    }
    let result = crate::cli::security::verify_audit_chain(conn, &table_name, limit)
        .map_err(DaemonRpcError::internal_error)?;
    serde_json::to_value(result)
        .map_err(|e| DaemonRpcError::internal_error(format!("audit_verify_chain serialize: {e}")))
}

/// `list_audit_signing_keys` —— 列出签名密钥轮换记录（不含 key_secret）。
///
/// 复刻 db_audit_chain.list_signing_keys：按 rotated_at 倒序。
pub fn handle_list_audit_signing_keys(
    conn: &Connection,
    _workspace_id: i64,
    _params: &Value,
) -> Result<Value, DaemonRpcError> {
    let sql = "SELECT key_id, rotated_at, is_active FROM audit_key_rotations \
               ORDER BY rotated_at DESC";
    let mut stmt = conn
        .prepare(sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("list_signing_keys prepare: {e}")))?;
    let rows = stmt
        .query_map([], |row| {
            Ok(json!({
                "key_id": row.get::<_, String>(0)?,
                "rotated_at": row.get::<_, f64>(1)?,
                "is_active": row.get::<_, i64>(2)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("list_signing_keys query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("list_signing_keys query: {e}")))?;
    Ok(Value::Array(rows))
}

/// `bootstrap_status` —— 自举健康状态摘要。
///
/// 复刻 db_bootstrap.bootstrap_status：db_stale（最近 scan_run git_head vs
/// 当前 HEAD）+ active_rules/pending_candidates 计数 + open/blocking
/// findings（按 workspace 过滤）+ audit_verify（limit=500）+ tasks 状态
/// 分组 + recommended_next_action。Python 各段 try/except pass → 0 值，
/// 此处以 unwrap_or(0) 等价复刻。
///
/// Q9-VCS（2026-09-17）：`reported_head` 为客户端 agent 经 refresh 报文上报的
/// git head（registry `daemon_workspaces.git_head_commit_sha`）。非空时
/// 直接作为当前 HEAD，daemon 不再 spawn git；为空（旧客户端 / 未探测到）
/// 时才回退到 `git rev-parse HEAD`（向后兼容）。
pub fn handle_bootstrap_status(
    conn: &Connection,
    workspace_id: i64,
    _params: &Value,
    reported_head: &str,
) -> Result<Value, DaemonRpcError> {
    // 1. 最近一次 scan_run（workspace 过滤）
    let latest_scan: Option<(i64, Option<String>, Option<f64>, Option<String>)> = conn
        .query_row(
            "SELECT id, git_head, started_at, status FROM workspace_scan_runs \
             WHERE workspace_id = ?1 ORDER BY started_at DESC LIMIT 1",
            rusqlite::params![workspace_id],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                ))
            },
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("bootstrap scan_run: {e}")))?;

    // Q9-VCS（2026-09-17）：当前 HEAD 优先取客户端 agent 上报的 head_sha
    // （跨平台统一：Linux/macOS/Windows 的 cw-agent 都经 probe_vcs_info
    // 探测并随 refresh 报文上报，daemon 不再自行 spawn git）。
    // 仅在客户端未上报（旧客户端 / 非 git 仓库）时回退到 daemon 自行
    // `git rev-parse HEAD`（向后兼容）。
    let mut current_head = String::new();
    if !reported_head.is_empty() {
        current_head = reported_head.to_string();
    } else {
        let workspace_root: Option<String> = conn
            .query_row(
                "SELECT root_path FROM workspaces WHERE id = ?1",
                rusqlite::params![workspace_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("bootstrap workspace: {e}")))?;
        if let Some(root) = &workspace_root {
            if std::path::Path::new(root).join(".git").is_dir() {
                if let Ok(output) = std::process::Command::new("git")
                    .args(["rev-parse", "HEAD"])
                    .current_dir(root)
                    .output()
                {
                    if output.status.success() {
                        current_head = String::from_utf8_lossy(&output.stdout)
                            .trim_end_matches(['\r', '\n'])
                            .to_string();
                    }
                }
            }
        }
    }
    let scan_head = latest_scan
        .as_ref()
        .and_then(|(_, git_head, _, _)| git_head.clone())
        .unwrap_or_default();
    let db_stale = !scan_head.is_empty() && !current_head.is_empty() && scan_head != current_head;

    // 2-3. 规则计数
    let count_scalar = |sql: &str| -> i64 { conn.query_row(sql, [], |r| r.get(0)).unwrap_or(0) };
    let active_rules_count =
        count_scalar("SELECT COUNT(*) FROM agent_rules WHERE status = 'active'");
    let pending_candidates_count =
        count_scalar("SELECT COUNT(*) FROM agent_rule_candidates WHERE status = 'pending'");

    // 4-5. quality findings 计数（workspace 过滤）
    let open_findings_count = conn
        .query_row(
            "SELECT COUNT(*) FROM task_quality_findings \
             WHERE status = 'open' AND workspace_id = ?1",
            rusqlite::params![workspace_id],
            |r| r.get::<_, i64>(0),
        )
        .unwrap_or(0);
    let blocking_findings_count = conn
        .query_row(
            "SELECT COUNT(*) FROM task_quality_findings \
             WHERE status = 'open' AND severity = 'block' AND workspace_id = ?1",
            rusqlite::params![workspace_id],
            |r| r.get::<_, i64>(0),
        )
        .unwrap_or(0);

    // 6. audit_chain 验证（limit=500；Python 失败 → {"error": ...}，字段取 0 值）
    let audit_verify = match crate::cli::security::verify_audit_chain(conn, "", 500) {
        Ok(v) => json!({
            "total_count": v.total_count,
            "verified_count": v.verified_count,
            "broken_count": v.broken_count,
            "security_level": v.security_level,
        }),
        Err(e) => json!({ "error": e, "total_count": 0, "verified_count": 0,
                          "broken_count": 0, "security_level": "" }),
    };
    let broken_count = audit_verify
        .get("broken_count")
        .and_then(Value::as_i64)
        .unwrap_or(0);

    // 7. tasks 状态分组（全局，无 workspace 维度——Python 注释明示）
    let mut task_counts = json!({
        "open": 0, "in_progress": 0, "review": 0, "applied": 0
    });
    if let Ok(mut stmt) = conn.prepare("SELECT status, COUNT(*) FROM tasks GROUP BY status") {
        if let Ok(rows) =
            stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))
        {
            for row in rows.flatten() {
                if let Some(slot) = task_counts.get_mut(&row.0) {
                    *slot = Value::from(row.1);
                }
            }
        }
    }

    // 8. 推荐下一条命令（分支顺序与 Python 一致）
    let recommended = if db_stale {
        "cw --refresh-all"
    } else if blocking_findings_count > 0 {
        "cw task findings <task_id>  # 有阻塞发现需修复"
    } else if pending_candidates_count > 0 {
        "cw rule candidate  # 有待审核的候选规则"
    } else if broken_count > 0 {
        "cw audit verify  # 审计链有损坏记录"
    } else if task_counts
        .get("review")
        .and_then(Value::as_i64)
        .unwrap_or(0)
        > 0
    {
        "cw task apply <task_id>  # 有任务待审核"
    } else {
        "cw task list  # 一切正常，查看任务列表"
    };

    let latest_scan_json = latest_scan.map(|(id, git_head, started_at, status)| {
        json!({
            "id": id,
            "git_head": git_head.unwrap_or_default(),
            "started_at": started_at.unwrap_or(0.0),
            "status": status.unwrap_or_default(),
        })
    });

    Ok(json!({
        "db_stale": db_stale,
        "current_head": current_head,
        "active_rules_count": active_rules_count,
        "pending_candidates_count": pending_candidates_count,
        "open_findings_count": open_findings_count,
        "blocking_findings_count": blocking_findings_count,
        "audit_verify": audit_verify,
        "latest_scan_run": latest_scan_json,
        "tasks": task_counts,
        "recommended_next_action": recommended,
    }))
}

/// `list_clones` —— 列出克隆对（含符号与文件信息，相似度降序）。
///
/// 复刻 db_clone_detection.list_clones：clone_type 仅 1/2/3 生效（0=全部）、
/// min_similarity>0 过滤、symbol_id>0 过滤（a 或 b 侧）。
pub fn handle_list_clones(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let clone_type = get_int_param_or(params, "clone_type", 0);
    let min_similarity = get_f64_param_or(params, "min_similarity", 0.0);
    let limit = get_int_param_or(params, "limit", 100);
    let symbol_id = get_int_param_or(params, "symbol_id", 0);

    let mut where_clauses = vec!["cp.workspace_id = ?1".to_string()];
    if (1..=3).contains(&clone_type) {
        where_clauses.push("cp.clone_type = ?2".to_string());
    }
    if min_similarity > 0.0 {
        where_clauses.push("cp.similarity >= ?3".to_string());
    }
    if symbol_id > 0 {
        where_clauses.push("(cp.symbol_a_id = ?4 OR cp.symbol_b_id = ?4)".to_string());
    }
    let sql = format!(
        "SELECT cp.clone_type, cp.similarity, cp.token_hash, cp.lines_a, cp.lines_b, \
         cp.detected_at, sa.name as symbol_a_name, sa.qualified_name as symbol_a_qualified, \
         sa.start_line as symbol_a_line, sb.name as symbol_b_name, \
         sb.qualified_name as symbol_b_qualified, sb.start_line as symbol_b_line, \
         fa.rel_path as file_a, fb.rel_path as file_b \
         FROM clone_pairs cp \
         JOIN symbols sa ON cp.symbol_a_id = sa.id \
         JOIN symbols sb ON cp.symbol_b_id = sb.id \
         JOIN file_instances fa ON sa.file_instance_id = fa.id \
         JOIN file_instances fb ON sb.file_instance_id = fb.id \
         WHERE {} ORDER BY cp.similarity DESC, cp.detected_at DESC LIMIT ?5",
        where_clauses.join(" AND ")
    );

    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("list_clones prepare: {e}")))?;
    let rows = stmt
        .query_map(
            rusqlite::params![workspace_id, clone_type, min_similarity, symbol_id, limit],
            |row| {
                Ok(json!({
                    "clone_type": row.get::<_, i64>(0)?,
                    "similarity": row.get::<_, f64>(1)?,
                    "token_hash": row.get::<_, Option<String>>(2)?,
                    "lines_a": row.get::<_, Option<i64>>(3)?,
                    "lines_b": row.get::<_, Option<i64>>(4)?,
                    "detected_at": row.get::<_, Option<f64>>(5)?,
                    "symbol_a_name": row.get::<_, Option<String>>(6)?,
                    "symbol_a_qualified": row.get::<_, Option<String>>(7)?,
                    "symbol_a_line": row.get::<_, Option<i64>>(8)?,
                    "symbol_b_name": row.get::<_, Option<String>>(9)?,
                    "symbol_b_qualified": row.get::<_, Option<String>>(10)?,
                    "symbol_b_line": row.get::<_, Option<i64>>(11)?,
                    "file_a": row.get::<_, Option<String>>(12)?,
                    "file_b": row.get::<_, Option<String>>(13)?,
                }))
            },
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("list_clones query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("list_clones query: {e}")))?;
    Ok(Value::Array(rows))
}

/// `list_clone_groups` —— 列出 clone groups（相似度降序，次序 member_count 降序）。
///
/// 复刻 db_clone_groups.list_clone_groups；输出 = CloneGroup.to_dict()
/// （asdict 全 9 字段）。
pub fn handle_list_clone_groups(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let clone_type = get_int_param_or(params, "clone_type", 0);
    let min_similarity = get_f64_param_or(params, "min_similarity", 0.0);
    let limit = get_int_param_or(params, "limit", 100);

    let mut sql = "SELECT id, workspace_id, group_hash, clone_type, token_hash, \
                   similarity, representative_symbol_id, member_count, created_at \
                   FROM clone_groups WHERE workspace_id = ?1"
        .to_string();
    if (1..=3).contains(&clone_type) {
        sql.push_str(" AND clone_type = ?2");
    }
    if min_similarity > 0.0 {
        sql.push_str(" AND similarity >= ?3");
    }
    sql.push_str(" ORDER BY similarity DESC, member_count DESC LIMIT ?4");

    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("list_clone_groups prepare: {e}")))?;
    let rows = stmt
        .query_map(
            rusqlite::params![workspace_id, clone_type, min_similarity, limit],
            clone_group_row_to_json,
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("list_clone_groups query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("list_clone_groups query: {e}")))?;
    Ok(Value::Array(rows))
}

/// clone_groups 行 → CloneGroup.to_dict() 等价 JSON。
fn clone_group_row_to_json(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "id": row.get::<_, i64>(0)?,
        "workspace_id": row.get::<_, i64>(1)?,
        "group_hash": row.get::<_, Option<String>>(2)?,
        "clone_type": row.get::<_, i64>(3)?,
        "token_hash": row.get::<_, Option<String>>(4)?,
        "similarity": row.get::<_, f64>(5)?,
        "representative_symbol_id": row.get::<_, Option<i64>>(6)?,
        "member_count": row.get::<_, i64>(7)?,
        "created_at": row.get::<_, Option<f64>>(8)?,
    }))
}

/// `get_clone_group_detail` —— clone group 详情（含成员符号）。
///
/// 复刻 db_clone_groups.get_clone_group_detail：按 group_id 直查
/// （Python 无 workspace 过滤，保持一致）；成员经 clone_group_members
/// JOIN symbols JOIN file_instances，按 symbol_id 排序。未找到 →
/// {"error": "group not found: <id>"}。
pub fn handle_get_clone_group_detail(
    conn: &Connection,
    _workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let group_id = get_int_param_or(params, "group_id", 0);
    let members_limit = get_int_param_or(params, "members_limit", 100);

    let group_row = conn
        .query_row(
            "SELECT id, workspace_id, group_hash, clone_type, token_hash, similarity, \
             representative_symbol_id, member_count, created_at \
             FROM clone_groups WHERE id = ?1",
            rusqlite::params![group_id],
            clone_group_row_to_json,
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("clone_group_detail: {e}")))?;
    let Some(group) = group_row else {
        return Ok(json!({ "error": format!("group not found: {group_id}") }));
    };

    let mut stmt = conn
        .prepare(
            "SELECT m.symbol_id, s.name, s.qualified_name, s.start_line, \
             fi.rel_path as file_path \
             FROM clone_group_members m \
             JOIN symbols s ON m.symbol_id = s.id \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE m.group_id = ?1 ORDER BY m.symbol_id LIMIT ?2",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("clone_group_members prepare: {e}")))?;
    let members = stmt
        .query_map(rusqlite::params![group_id, members_limit], |row| {
            Ok(json!({
                "symbol_id": row.get::<_, i64>(0)?,
                "name": row.get::<_, Option<String>>(1)?,
                "qualified_name": row.get::<_, Option<String>>(2)?,
                "start_line": row.get::<_, Option<i64>>(3)?,
                "file_path": row.get::<_, Option<String>>(4)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("clone_group_members query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("clone_group_members query: {e}")))?;

    Ok(json!({ "group": group, "members": members }))
}

/// `task_plan_template` —— task_create_from_plan 标准格式模板。
///
/// 复刻 db_tasks.task_plan_template（i18n）。locale 解析复刻
/// i18n._detect_system_language 的环境变量链（CALLWARDEN_LANG →
/// LC_ALL/LC_MESSAGES/LANG，zh-CN/zh 归一化），回退 en_US 默认模板；
/// Python locale.getdefaultlocale() 分支未复刻（本机实测返回 en 默认，
/// 与此处回退一致）。
pub fn handle_task_plan_template(
    _conn: &Connection,
    _workspace_id: i64,
    _params: &Value,
) -> Result<Value, DaemonRpcError> {
    Ok(Value::String(detect_plan_template_locale().to_string()))
}

/// 环境变量链探测模板语言（zh_CN → 中文模板，否则英文默认）。
fn detect_plan_template_locale() -> &'static str {
    const TEMPLATE_EN: &str = "# {Root task title}\n{Root task description (plain text)}\n\n## {Subtask 1 title}\n{Subtask 1 description (optional)}\n\n- {Step 1 description}\n- {Step 2 description}\n- [ ] {Incomplete step (checkbox format)}\n- [x] {Completed step}\n\n### {Step group title (optional)}\n- {Grouped step}\n\n## {Subtask 2 title}\n1. {Ordered list step}\n2. {Ordered list step}\n\n## {Subtask 3 title (default step will be added when no steps exist)}\n\nFormat notes:/n- # heading -> root task description\n- ## heading -> subtask (created automatically)\n- ### heading -> step group\n- - / * / + -> unordered list item (recognized as step)\n- 1. / 2. / 3. -> ordered list item (recognized as step)\n- [ ] / [x] / [X] -> checkbox variants (marker is removed)\n- ``` or ~~~ -> code block (content is not parsed)\n- trailing # in headings is cleaned automatically (for example, ## Title ## -> Title)\n";
    const TEMPLATE_ZH: &str = "# {根任务标题}\n{根任务描述（普通文本）}\n\n## {子任务1标题}\n{子任务1描述（可选）}\n\n- {步骤1描述}\n- {步骤2描述}\n- [ ] {未完成步骤（checkbox格式）}\n- [x] {已完成步骤}\n\n### {步骤分组标题（可选）}\n- {分组内步骤}\n\n## {子任务2标题}\n1. {有序列表步骤}\n2. {有序列表步骤}\n\n## {子任务3标题（无步骤会自动补默认）}\n\n格式说明：\n- # 一级标题 → 根任务描述\n- ## 二级标题 → 子任务（自动创建）\n- ### 三级标题 → 步骤分组\n- - / * / + → 无序列表项（自动识别为步骤）\n- 1. / 2. / 3. → 有序列表项（自动识别为步骤）\n- [ ] / [x] / [X] → checkbox 变体（自动去除标记）\n- ``` 或 ~~~ → 代码块（内容不解析）\n- 标题末尾 # 字符自动清理（如 ## Title ## → Title）\n";

    let mut lang = std::env::var("CALLWARDEN_LANG").unwrap_or_default();
    if lang.trim().is_empty() {
        for env_var in ["LC_ALL", "LC_MESSAGES", "LANG"] {
            let val = std::env::var(env_var).unwrap_or_default();
            if !val.trim().is_empty() {
                lang = val;
                break;
            }
        }
    }
    let normalized = lang.trim().to_string().replace('-', "_");
    let lang_code = normalized.split('.').next().unwrap_or("").to_string();
    if lang_code == "zh_CN" || lang_code == "zh" {
        TEMPLATE_ZH
    } else {
        TEMPLATE_EN
    }
}

// ============================================================
// P0-COMPAT-v3（T-1788963104879-2e9e6270）：tools_semantic 组（5）
// ============================================================
// 复刻 server/tools/tools_semantic.py 的 5 个只读 compat handler：
// - get_symbol_commit_history → db/db_git.py::get_symbol_commit_history
// - parse_codeowners          → db/db_ownership.py::parse_codeowners
// - get_project_dependencies  → db/db_external.py（多语言 manifest 解析）
// - find_similar_functions / semantic_search → db/db_vector.py
// 语义向量可用性策略见 handle_semantic_search 文档注释。

/// 解析 workspace 根路径（workspaces.root_path）。
///
/// parse_codeowners / get_project_dependencies 需要真实文件系统路径；
/// 复刻 tools_semantic._bind_readonly_db 里的 `SELECT root_path FROM workspaces`。
fn workspace_root_of(conn: &Connection, workspace_id: i64) -> Option<String> {
    conn.query_row(
        "SELECT root_path FROM workspaces WHERE id = ?1",
        rusqlite::params![workspace_id],
        |row| row.get::<_, Option<String>>(0),
    )
    .ok()
    .flatten()
}

/// 执行查询并把结果集按列名序列化为 JSON 对象数组（`SELECT gc.*` 动态列场景）。
fn query_rows_to_json(
    conn: &Connection,
    sql: &str,
    params: &[&dyn rusqlite::ToSql],
) -> Result<Value, DaemonRpcError> {
    let mut stmt = conn
        .prepare(sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("query prepare: {e}")))?;
    let names: Vec<String> = stmt
        .column_names()
        .into_iter()
        .map(|s| s.to_string())
        .collect();
    let rows = stmt
        .query_map(params, |row| {
            let mut map = Map::new();
            for (i, name) in names.iter().enumerate() {
                let v = match row.get_ref(i)? {
                    rusqlite::types::ValueRef::Null => Value::Null,
                    rusqlite::types::ValueRef::Integer(n) => json!(n),
                    rusqlite::types::ValueRef::Real(f) => json!(f),
                    rusqlite::types::ValueRef::Text(t) => {
                        Value::String(String::from_utf8_lossy(t).to_string())
                    }
                    rusqlite::types::ValueRef::Blob(b) => {
                        Value::String(format!("<blob {} bytes>", b.len()))
                    }
                };
                map.insert(name.clone(), v);
            }
            Ok(Value::Object(map))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("query collect: {e}")))?;
    Ok(Value::Array(rows))
}

/// `get_symbol_commit_history` —— 符号的 Git 变更历史。
///
/// 复刻 db/db_git.py::get_symbol_commit_history：`git_symbol_changes` join
/// `git_commits`，按 commit 时间倒序，LIMIT 由参数控制。
///
/// 与 Python 的偏离（fail-closed，已在上批 v3 卡统一）：`limit` 为负时 Python
/// 会把 LIMIT 负值解释为"不限行数"，本实现返回 invalid_params，避免全表扫描。
pub fn handle_get_symbol_commit_history(
    conn: &Connection,
    _workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let symbol_hash = get_str_param_or(params, "symbol_hash", "");
    let limit = get_int_param_or(params, "limit", 20);
    if limit < 0 {
        return Err(DaemonRpcError::invalid_params(
            "limit 不能为负数（Python 负 LIMIT 语义为不限制行数，此处 fail-closed）",
        ));
    }
    if symbol_hash.trim().is_empty() {
        return Ok(Value::Array(vec![]));
    }
    let limit = limit.min(MAX_RESULT_ROWS);
    query_rows_to_json(
        conn,
        "SELECT gc.*, gsc.change_type \
         FROM git_symbol_changes gsc \
         JOIN git_commits gc ON gsc.commit_hash = gc.commit_hash \
         WHERE gsc.symbol_hash = ?1 \
         ORDER BY gc.timestamp DESC \
         LIMIT ?2",
        rusqlite::params![symbol_hash, limit],
    )
}

/// CODEOWNERS 候选路径（复刻 db_ownership._CODEOWNERS_CANDIDATES）。
const CODEOWNERS_CANDIDATES: [&str; 3] = [".github/CODEOWNERS", "docs/CODEOWNERS", "CODEOWNERS"];

/// `parse_codeowners` —— 解析 CODEOWNERS 规则（只读，不写库）。
///
/// 复刻 db/db_ownership.py::parse_codeowners：空行/注释行跳过，行内注释按
/// " #" 切分，owner 只保留含 `@` 的 token，无 owner 的规则也保留。
pub fn handle_parse_codeowners(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let file_path = get_str_param_or(params, "file_path", "");
    let resolved: Option<PathBuf> = if file_path.trim().is_empty() {
        workspace_root_of(conn, workspace_id).and_then(|root| {
            CODEOWNERS_CANDIDATES
                .iter()
                .map(|rel| Path::new(&root).join(rel))
                .find(|p| p.is_file())
        })
    } else {
        Some(PathBuf::from(file_path))
    };
    let Some(path) = resolved else {
        return Ok(Value::Array(vec![]));
    };
    let Ok(content) = std::fs::read_to_string(&path) else {
        return Ok(Value::Array(vec![]));
    };
    Ok(Value::Array(parse_codeowners_rules(&content)))
}

/// CODEOWNERS 文本 → 规则数组（[{pattern, owners}]）。
fn parse_codeowners_rules(content: &str) -> Vec<Value> {
    let mut rules = Vec::new();
    for raw_line in content.lines() {
        let mut line = raw_line.trim().to_string();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some(idx) = line.find(" #") {
            if idx > 0 {
                line = line[..idx].trim().to_string();
            }
        }
        let parts: Vec<String> = line.split_whitespace().map(|s| s.to_string()).collect();
        let Some(pattern) = parts.first() else {
            continue;
        };
        if pattern.is_empty() {
            continue;
        }
        let owners: Vec<Value> = parts[1..]
            .iter()
            .filter(|p| p.contains('@'))
            .map(|p| Value::String(p.to_string()))
            .collect();
        rules.push(json!({ "pattern": pattern, "owners": owners }));
    }
    rules
}

/// 语言 → manifest 候选文件（顺序敏感，复刻 db_external.LANG_PACKAGE_MANAGERS
/// 的 dict 顺序与 manifest_files 列表）。
const LANG_MANIFESTS: [(&str, &[&str]); 13] = [
    ("python", &["requirements.txt", "pyproject.toml", "setup.py"]),
    ("rust", &["Cargo.toml"]),
    ("java", &["pom.xml", "build.gradle"]),
    ("scala", &["build.sbt", "pom.xml", "build.gradle"]),
    ("go", &["go.mod"]),
    ("typescript", &["package.json"]),
    ("javascript", &["package.json"]),
    ("ruby", &["Gemfile", "*.gemspec"]),
    ("php", &["composer.json"]),
    ("swift", &["Package.swift"]),
    ("kotlin", &["build.gradle.kts", "build.gradle"]),
    ("csharp", &["*.csproj", "packages.config"]),
    ("elixir", &["mix.exs"]),
];

/// `get_project_dependencies` —— 项目直接依赖清单（多语言，不展开传递依赖）。
///
/// 复刻 db/db_external.py::get_project_dependencies：
/// - languages 为空 → 按 LANG_MANIFESTS 顺序检测存在的 manifest；
/// - 每种语言取第一个命中的 manifest 解析（Python 同款：命中即 return）；
/// - 结果形状 {语言: {包名: 版本约束}}。
pub fn handle_get_project_dependencies(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let root = workspace_root_of(conn, workspace_id).unwrap_or_default();
    let languages: Vec<String> = match params.get("languages").and_then(Value::as_array) {
        Some(arr) => arr
            .iter()
            .filter_map(|v| v.as_str().map(|s| s.to_string()))
            .collect(),
        None => detect_project_languages(&root),
    };
    let mut out = Map::new();
    for lang in languages {
        out.insert(lang.clone(), deps_for_lang(&root, &lang));
    }
    Ok(Value::Object(out))
}

/// 检测项目语言（复刻 db_external._detect_project_languages）。
fn detect_project_languages(root: &str) -> Vec<String> {
    if root.is_empty() {
        return Vec::new();
    }
    let base = Path::new(root);
    let mut detected = Vec::new();
    for (lang, manifests) in LANG_MANIFESTS.iter() {
        if manifests.iter().any(|m| match_manifest(base, m).is_some()) {
            detected.push(lang.to_string());
        }
    }
    detected
}

/// manifest 匹配：支持 `*.ext` 通配符（取字典序第一个匹配，Python glob 同序）。
fn match_manifest(base: &Path, manifest: &str) -> Option<PathBuf> {
    if let Some(ext) = manifest.strip_prefix("*.") {
        let dir = std::fs::read_dir(base).ok()?;
        let mut hits: Vec<PathBuf> = dir
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| {
                p.extension()
                    .and_then(|e| e.to_str())
                    .map(|e| e == ext)
                    .unwrap_or(false)
            })
            .collect();
        hits.sort();
        return hits.into_iter().next();
    }
    let p = base.join(manifest);
    if p.exists() {
        Some(p)
    } else {
        None
    }
}

/// 单语言依赖解析（复刻 db_external._get_project_dependencies_for_lang）。
fn deps_for_lang(root: &str, lang: &str) -> Value {
    let base = Path::new(root);
    let mut deps = Map::new();
    let Some((_, manifests)) = LANG_MANIFESTS.iter().find(|(l, _)| *l == lang) else {
        return Value::Object(deps);
    };
    for manifest in *manifests {
        let Some(path) = match_manifest(base, manifest) else {
            continue;
        };
        match lang {
            "python" => parse_python_deps(base, &mut deps),
            "rust" => parse_rust_manifest(&path, &mut deps),
            "go" => parse_go_manifest(&path, &mut deps),
            "typescript" | "javascript" => parse_json_section(&path, "dependencies", &mut deps),
            "php" => parse_json_section(&path, "require", &mut deps),
            "java" | "scala" | "kotlin" => parse_java_manifest(&path, &mut deps),
            "ruby" => parse_ruby_manifest(&path, &mut deps),
            "swift" => parse_swift_manifest(&path, &mut deps),
            "csharp" => parse_csharp_manifest(&path, &mut deps),
            "elixir" => parse_elixir_manifest(&path, &mut deps),
            _ => {}
        }
        break; // 命中即返回（对齐 Python 的 return parser(path)）
    }
    Value::Object(deps)
}

/// 依赖规格字符串 `name==1.0` / `name>=1.0` → 小写包名 + 版本约束。
fn insert_dep_spec(spec: &str, out: &mut Map<String, Value>) {
    let dep = spec.trim();
    if dep.is_empty() {
        return;
    }
    let parts: Vec<&str> = dep.split("==").collect();
    let (name, version) = if parts.len() >= 2 {
        (parts[0].trim().to_lowercase(), parts[1].trim().to_string())
    } else {
        (dep.to_lowercase(), String::new())
    };
    if !name.is_empty() {
        out.insert(name, Value::String(version));
    }
}

/// Python 依赖：requirements.txt → pyproject.toml → setup.py（累加，复刻
/// db_external._get_python_project_dependencies）。
fn parse_python_deps(base: &Path, out: &mut Map<String, Value>) {
    let req = base.join("requirements.txt");
    if req.exists() {
        if let Ok(content) = std::fs::read_to_string(&req) {
            for line in content.lines() {
                let line = line.trim();
                if line.is_empty() || line.starts_with('#') {
                    continue;
                }
                insert_dep_spec(line, out);
            }
        }
    }
    let pyproject = base.join("pyproject.toml");
    if pyproject.exists() {
        if let Some(doc) = load_toml(&pyproject) {
            for section in [&["project", "dependencies"][..], &["tool", "poetry", "dependencies"][..]] {
                let mut cur = Some(&doc);
                for key in section {
                    cur = cur.and_then(|v| v.get(key));
                }
                if let Some(toml::Value::Array(items)) = cur {
                    for item in items {
                        if let toml::Value::String(s) = item {
                            insert_dep_spec(s, out);
                        }
                    }
                }
            }
        }
    }
    let setup = base.join("setup.py");
    if setup.exists() {
        if let Ok(content) = std::fs::read_to_string(&setup) {
            if let Some(start) = content.find("install_requires") {
                if let Some(br_start) = content[start..].find('[') {
                    let rest = &content[start + br_start + 1..];
                    if let Some(br_end) = rest.find(']') {
                        for dep in rest[..br_end].split(',') {
                            let dep = dep.trim().trim_matches('\'').trim_matches('"').trim();
                            if !dep.is_empty() {
                                insert_dep_spec(dep, out);
                            }
                        }
                    }
                }
            }
        }
    }
}

fn load_toml(path: &Path) -> Option<toml::Value> {
    let content = std::fs::read_to_string(path).ok()?;
    toml::from_str::<toml::Value>(&content).ok()
}

/// Cargo.toml：字符串 spec 或 table 的 version 字段（复刻 _parse_rust_manifest）。
fn parse_rust_manifest(path: &Path, out: &mut Map<String, Value>) {
    let Some(doc) = load_toml(path) else {
        return;
    };
    let Some(toml::Value::Table(table)) = doc.get("dependencies") else {
        return;
    };
    for (name, spec) in table {
        let version = match spec {
            toml::Value::String(s) => s.clone(),
            toml::Value::Table(t) => t
                .get("version")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            _ => String::new(),
        };
        out.insert(name.clone(), Value::String(version));
    }
}

/// go.mod：require 块 + 单行 require（跳过 // indirect，复刻 _parse_go_manifest）。
fn parse_go_manifest(path: &Path, out: &mut Map<String, Value>) {
    let Ok(content) = std::fs::read_to_string(path) else {
        return;
    };
    if let Some(start) = content.find("require") {
        if let Some(rel) = content[start..].find('(') {
            let rest = &content[start + rel + 1..];
            if let Some(end) = rest.find(')') {
                for line in rest[..end].lines() {
                    let line = line.trim();
                    if line.is_empty() || line.starts_with("//") || line.contains("// indirect") {
                        continue;
                    }
                    let parts: Vec<&str> = line.split_whitespace().collect();
                    if parts.len() >= 2 {
                        out.insert(parts[0].to_string(), Value::String(parts[1].to_string()));
                    }
                }
            }
        }
    }
    for line in content.lines() {
        let line = line.trim();
        if let Some(rest) = line.strip_prefix("require ") {
            if rest.starts_with('(') || line.contains("// indirect") {
                continue;
            }
            let parts: Vec<&str> = rest.split_whitespace().collect();
            if parts.len() >= 2 {
                out.insert(
                    parts[0].to_string(),
                    Value::String(parts[1].trim_end_matches("//").to_string()),
                );
            }
        }
    }
}

/// package.json / composer.json：取指定 section（复刻 _parse_package_json /
/// _parse_php_manifest；版本号一律 str()）。
fn parse_json_section(path: &Path, section: &str, out: &mut Map<String, Value>) {
    let Ok(content) = std::fs::read_to_string(path) else {
        return;
    };
    let Ok(doc) = serde_json::from_str::<Value>(&content) else {
        return;
    };
    let Some(Value::Object(map)) = doc.get(section) else {
        return;
    };
    for (name, version) in map {
        let v = match version {
            Value::String(s) => s.clone(),
            Value::Number(n) => n.to_string(),
            Value::Null => String::new(),
            other => other.to_string(),
        };
        out.insert(name.clone(), Value::String(v));
    }
}

/// pom.xml（groupId:artifactId → version）+ build.gradle（简化的
/// `group:artifact:version` 声明）（简化复刻 _parse_java_manifest）。
fn parse_java_manifest(path: &Path, out: &mut Map<String, Value>) {
    let Ok(content) = std::fs::read_to_string(path) else {
        return;
    };
    let is_xml = path
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| e == "xml")
        .unwrap_or(false);
    if is_xml {
        // 逐 <dependency> 块提取 groupId / artifactId / version
        for block in content.split("<dependency>").skip(1) {
            let block = block.split("</dependency>").next().unwrap_or("");
            let group = xml_tag_text(block, "groupId").unwrap_or_default();
            let artifact = xml_tag_text(block, "artifactId").unwrap_or_default();
            let version = xml_tag_text(block, "version")
                .or_else(|| xml_tag_text(block, "versionRange"))
                .unwrap_or_default();
            if !artifact.is_empty() {
                let key = if group.is_empty() {
                    artifact.clone()
                } else {
                    format!("{group}:{artifact}")
                };
                out.insert(key, Value::String(version));
            }
        }
        return;
    }
    // build.gradle / build.gradle.kts / build.sbt：`config 'group:artifact:version'`
    for line in content.lines() {
        let line = line.trim();
        if !line.starts_with("implementation")
            && !line.starts_with("api")
            && !line.starts_with("compile")
            && !line.starts_with("testImplementation")
            && !line.contains("libraryDependencies")
        {
            continue;
        }
        for token in line.split(|c| c == '\'' || c == '"').skip(1).step_by(2) {
            if token.contains(':') {
                let parts: Vec<&str> = token.split(':').collect();
                if parts.len() >= 2 {
                    let key = if parts.len() >= 3 && !parts[0].is_empty() {
                        format!("{}:{}", parts[0], parts[1])
                    } else {
                        parts[..2].join(":")
                    };
                    let version = parts.get(2).filter(|v| parts.len() > 3).unwrap_or(&"");
                    out.insert(key, Value::String(version.to_string()));
                }
            }
        }
    }
}

/// 提取 XML 标签文本（极简，仅用于 pom 依赖块）。
fn xml_tag_text(block: &str, tag: &str) -> Option<String> {
    let open = format!("<{tag}>");
    let close = format!("</{tag}>");
    let start = block.find(&open)? + open.len();
    let rest = &block[start..];
    let end = rest.find(&close)?;
    Some(rest[..end].trim().to_string())
}

/// Gemfile：`gem 'name', 'version'`（复刻 _parse_ruby_manifest）。
fn parse_ruby_manifest(path: &Path, out: &mut Map<String, Value>) {
    let Ok(content) = std::fs::read_to_string(path) else {
        return;
    };
    for line in content.lines() {
        let line = line.trim();
        let Some(rest) = line.strip_prefix("gem ") else {
            continue;
        };
        let tokens: Vec<String> = rest
            .split(|c| c == '\'' || c == '"')
            .skip(1)
            .step_by(2)
            .map(|s| s.to_string())
            .collect();
        if let Some(name) = tokens.first() {
            out.insert(
                name.clone(),
                Value::String(tokens.get(1).cloned().unwrap_or_default()),
            );
        }
    }
}

/// Package.swift：`.package(url: "...", from: "1.0.0")`（简化复刻 _parse_swift_manifest）。
fn parse_swift_manifest(path: &Path, out: &mut Map<String, Value>) {
    let Ok(content) = std::fs::read_to_string(path) else {
        return;
    };
    let Ok(re) = regex::Regex::new(r#"\.package\([^)]*url:\s*"([^"]+)"[^)]*\)"#) else {
        return;
    };
    let Ok(ver_re) = regex::Regex::new(
        r#"(?:from:\s*"([^"]+)"|\.exact\("([^"]+)"\)|version:\s*"([^"]+)"|upToNextMajor\(from:\s*"([^"]+)"\))"#,
    ) else {
        return;
    };
    for cap in re.captures_iter(&content) {
        let url = cap[1].to_string();
        let m = ver_re.captures(&cap[0]);
        let version = m
            .as_ref()
            .map(|c| {
                c.iter()
                    .skip(1)
                    .flatten()
                    .map(|x| x.as_str().to_string())
                    .next()
                    .unwrap_or_default()
            })
            .unwrap_or_default();
        let name = url
            .trim_end_matches('/')
            .split('/')
            .next_back()
            .unwrap_or("")
            .replace(".git", "");
        if !name.is_empty() {
            out.insert(name, Value::String(version));
        }
    }
}

/// *.csproj：`<PackageReference Include="X" Version="1.0" />`（简化复刻
/// _parse_csharp_manifest）。
fn parse_csharp_manifest(path: &Path, out: &mut Map<String, Value>) {
    let Ok(content) = std::fs::read_to_string(path) else {
        return;
    };
    let Ok(re) = regex::Regex::new(
        r#"<PackageReference\s+Include="([^"]+)"\s+Version="([^"]*)""#,
    ) else {
        return;
    };
    for cap in re.captures_iter(&content) {
        out.insert(cap[1].to_string(), Value::String(cap[2].to_string()));
    }
}

/// mix.exs：`{:name, "~> 1.0"}`（复刻 _parse_elixir_manifest）。
fn parse_elixir_manifest(path: &Path, out: &mut Map<String, Value>) {
    let Ok(content) = std::fs::read_to_string(path) else {
        return;
    };
    let Ok(re) = regex::Regex::new(r#"\{:(\w+),\s*"([^"]+)""#) else {
        return;
    };
    for cap in re.captures_iter(&content) {
        out.insert(cap[1].to_string(), Value::String(cap[2].to_string()));
    }
}

// ------------------------------------------------------------
// 语义向量：embedding 加载 / cosine TopK
// ------------------------------------------------------------

/// 加载全部 embedding（复刻 db_vector._load_all_embeddings）。
///
/// BLOB 为 float32 小端序列（numpy.float32.tobytes）；长度非 4 倍数的行跳过
/// （对齐 Python try/except continue）。
fn load_embeddings(conn: &Connection) -> Result<Vec<(String, Vec<f32>)>, DaemonRpcError> {
    let mut stmt = conn
        .prepare("SELECT symbol_hash, embedding FROM symbol_embeddings")
        .map_err(|e| DaemonRpcError::internal_error(format!("embeddings prepare: {e}")))?;
    let rows = stmt
        .query_map([], |row| {
            let hash: String = row.get(0)?;
            let blob: Vec<u8> = row.get(1)?;
            Ok((hash, blob))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("embeddings query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("embeddings collect: {e}")))?;
    Ok(rows
        .into_iter()
        .map(|(hash, blob)| (hash, decode_f32_blob(&blob)))
        .collect())
}

/// float32 小端 BLOB → Vec<f32>（长度非 4 倍数返回空向量）。
fn decode_f32_blob(blob: &[u8]) -> Vec<f32> {
    if blob.is_empty() || blob.len() % 4 != 0 {
        return Vec::new();
    }
    blob.chunks_exact(4)
        .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
        .collect()
}

/// 余弦相似度 TopK（复刻 vector_topk_core 语义：阈值过滤 + 相似度降序 +
/// 同分按 symbol_hash 升序 tiebreaker）。
fn cosine_topk(
    query: &[f32],
    items: &[(String, Vec<f32>)],
    threshold: f32,
    top_k: usize,
) -> Vec<(String, f32)> {
    if query.is_empty() || items.is_empty() || top_k == 0 {
        return Vec::new();
    }
    let q_norm: f32 = query.iter().map(|x| x * x).sum::<f32>().sqrt();
    if q_norm == 0.0 {
        return Vec::new();
    }
    let mut scored: Vec<(String, f32)> = items
        .iter()
        .map(|(hash, vec)| {
            let mut dot = 0.0f32;
            let mut norm_sq = 0.0f32;
            for (q, v) in query.iter().zip(vec.iter()) {
                dot += q * v;
                norm_sq += v * v;
            }
            let sim = if norm_sq > 0.0 {
                dot / (q_norm * norm_sq.sqrt())
            } else {
                0.0
            };
            (hash.clone(), sim)
        })
        .filter(|(_, sim)| *sim >= threshold)
        .collect();
    scored.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });
    scored.truncate(top_k);
    scored
}

/// 按 symbol_hash 批量取符号元信息 → 结果数组（复刻 db_vector 的 info_map 拼装）。
fn symbol_results(
    conn: &Connection,
    workspace_id: i64,
    top: &[(String, f32)],
) -> Result<Value, DaemonRpcError> {
    if top.is_empty() {
        return Ok(Value::Array(vec![]));
    }
    let placeholders = std::iter::repeat_n("?", top.len())
        .collect::<Vec<_>>()
        .join(",");
    let sql = format!(
        "SELECT s.symbol_hash, s.qualified_name, s.kind, \
                s.start_line, s.end_line, fi.rel_path, \
                ( \
                    SELECT ss.summary FROM symbol_summaries ss \
                    WHERE ss.symbol_hash = s.symbol_hash \
                      AND ss.is_current = 1 \
                    ORDER BY ss.version DESC LIMIT 1 \
                ) as summary \
         FROM symbols s \
         JOIN file_instances fi ON s.file_instance_id = fi.id \
         WHERE fi.workspace_id = ?1 \
           AND s.symbol_hash IN ({placeholders}) \
         GROUP BY s.symbol_hash"
    );
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol_info prepare: {e}")))?;
    let mut params: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
    params.push(Box::new(workspace_id));
    for (hash, _) in top {
        params.push(Box::new(hash.clone()));
    }
    let refs: Vec<&dyn rusqlite::ToSql> = params.iter().map(|p| p.as_ref()).collect();
    let info = stmt
        .query_map(refs.as_slice(), |row| {
            let hash: String = row.get(0)?;
            let qualified_name: Option<String> = row.get(1)?;
            let start_line: Option<i64> = row.get(3)?;
            let rel_path: Option<String> = row.get(5)?;
            let summary: Option<String> = row.get(6)?;
            Ok((
                hash,
                qualified_name.unwrap_or_default(),
                rel_path.unwrap_or_default(),
                start_line.unwrap_or(0),
                summary.unwrap_or_default(),
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol_info query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol_info collect: {e}")))?;
    let map: std::collections::HashMap<String, (String, String, i64, String)> = info
        .into_iter()
        .map(|(h, q, p, l, s)| (h, (q, p, l, s)))
        .collect();
    let mut results = Vec::new();
    for (hash, sim) in top {
        let Some((qualified_name, file_path, start_line, summary)) = map.get(hash) else {
            continue;
        };
        let rounded = (*sim as f64 * 10000.0).round() / 10000.0;
        results.push(json!({
            "qualified_name": qualified_name,
            "file_path": file_path,
            "start_line": start_line,
            "similarity": rounded,
            "summary": summary,
        }));
    }
    Ok(Value::Array(results))
}

/// `find_similar_functions` —— 与目标函数语义相似的函数列表。
///
/// 复刻 db/db_vector.py::find_similar_functions：目标函数缺失/无 embedding →
/// 空列表；候选集排除自身；阈值过滤 + TopK。
pub fn handle_find_similar_functions(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let qualified_name = get_str_param_or(params, "qualified_name", "");
    let threshold = get_f64_param_or(params, "threshold", 0.8) as f32;
    let top_k = get_int_param_or(params, "top_k", 20);
    if qualified_name.trim().is_empty() || top_k <= 0 {
        return Ok(Value::Array(vec![]));
    }
    let target_hash: Option<String> = conn
        .query_row(
            "SELECT s.symbol_hash \
             FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND fi.status != 'archived' AND s.qualified_name = ?2 \
             LIMIT 1",
            rusqlite::params![workspace_id, qualified_name],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("target symbol query: {e}")))?;
    let Some(target_hash) = target_hash else {
        return Ok(Value::Array(vec![]));
    };
    let blob: Option<Vec<u8>> = conn
        .query_row(
            "SELECT embedding FROM symbol_embeddings WHERE symbol_hash = ?1",
            rusqlite::params![target_hash],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("target embedding query: {e}")))?;
    let Some(blob) = blob else {
        return Ok(Value::Array(vec![]));
    };
    let target_vec = decode_f32_blob(&blob);
    if target_vec.is_empty() {
        return Ok(Value::Array(vec![]));
    }
    let all = load_embeddings(conn)?;
    let filtered: Vec<(String, Vec<f32>)> = all
        .into_iter()
        .filter(|(h, _)| h != &target_hash)
        .collect();
    let top = cosine_topk(&target_vec, &filtered, threshold, top_k as usize);
    symbol_results(conn, workspace_id, &top)
}

/// `semantic_search` —— 自然语言语义搜索。
///
/// 复刻 db/db_vector.py::semantic_search。查询向量需要嵌入模型
/// （sentence-transformers jina-embeddings-v2-base-code 或 ollama
/// nomic-embed-text），Rust daemon 不携带任何模型运行时，因此沿用 Python 的
/// 降级契约：**嵌入后端不可用时返回空列表**（Python `_get_embedder() is None`
/// 分支同形）。
///
/// fail-closed 护栏：只有库内 embedding 全部由 daemon 自产的确定性嵌入
/// （model_version = `hash-v1`，见 job_runner::exec_embed）时，才用同款
/// sha256 近似向量复算 query 向量并执行检索；若库内是外部模型的向量，
/// Rust 侧不会用自造向量冒充语义相似度，直接返回空列表。
pub fn handle_semantic_search(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let query = get_str_param_or(params, "query", "");
    let top_k = get_int_param_or(params, "top_k", 5);
    if query.trim().is_empty() || top_k <= 0 {
        return Ok(Value::Array(vec![]));
    }
    let embeddings = load_embeddings(conn)?;
    if embeddings.is_empty() {
        return Ok(Value::Array(vec![]));
    }
    let mut stmt = conn
        .prepare("SELECT DISTINCT model_version FROM symbol_embeddings")
        .map_err(|e| DaemonRpcError::internal_error(format!("model_version prepare: {e}")))?;
    let versions = stmt
        .query_map([], |row| row.get::<_, Option<String>>(0))
        .map_err(|e| DaemonRpcError::internal_error(format!("model_version query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("model_version collect: {e}")))?;
    let deterministic_only = !versions.is_empty()
        && versions
            .iter()
            .all(|v| v.as_deref().unwrap_or("") == DETERMINISTIC_EMBED_MODEL);
    if !deterministic_only {
        return Ok(Value::Array(vec![]));
    }
    let query_vec = deterministic_embedding(&query);
    let top = cosine_topk(&query_vec, &embeddings, 0.0, top_k as usize);
    symbol_results(conn, workspace_id, &top)
}

/// daemon 确定性嵌入的 model_version（与 job_runner::exec_embed 对齐）。
const DETERMINISTIC_EMBED_MODEL: &str = "hash-v1";

/// 确定性近似嵌入（复刻 job_runner::exec_embed：sha256 前 16 字节 / 255）。
fn deterministic_embedding(text: &str) -> Vec<f32> {
    let digest = crate::daemon::fs_handlers::sha256_hex(text.as_bytes());
    let bytes = digest.as_bytes();
    (0..16)
        .map(|i| bytes.get(i).copied().unwrap_or(0) as f32 / 255.0)
        .collect()
}

// ============================================================
// tools_semantic 组单元测试（临时 DB，覆盖纯函数与 handler 行为）
// ============================================================

#[cfg(test)]
mod semantic_tests {
    use super::*;
    use std::collections::HashSet;

    fn temp_db() -> Connection {
        let conn = Connection::open_in_memory().expect("memory db");
        conn.execute_batch(
            "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, root_path TEXT);
             CREATE TABLE file_instances (id INTEGER PRIMARY KEY, workspace_id INTEGER, rel_path TEXT, status TEXT);
             CREATE TABLE symbols (id INTEGER PRIMARY KEY, symbol_hash TEXT, qualified_name TEXT,
                 kind TEXT, start_line INTEGER, end_line INTEGER, file_instance_id INTEGER);
             CREATE TABLE symbol_embeddings (symbol_hash TEXT PRIMARY KEY, embedding BLOB,
                 model_version TEXT, dim INTEGER, embedded_at INTEGER);
             CREATE TABLE symbol_summaries (id INTEGER PRIMARY KEY, symbol_hash TEXT, summary TEXT,
                 model TEXT, version INTEGER, is_current INTEGER, created_at INTEGER);
             CREATE TABLE git_commits (id INTEGER PRIMARY KEY, commit_hash TEXT, message TEXT,
                 author TEXT, email TEXT, timestamp INTEGER, workspace_id INTEGER);
             CREATE TABLE git_symbol_changes (id INTEGER PRIMARY KEY, commit_hash TEXT,
                 symbol_hash TEXT, change_type TEXT, old_content TEXT, new_content TEXT);",
        )
        .expect("schema");
        conn
    }

    fn f32_blob(v: &[f32]) -> Vec<u8> {
        v.iter().flat_map(|x| x.to_le_bytes()).collect()
    }

    fn seed_symbol(conn: &Connection, hash: &str, name: &str) {
        conn.execute(
            "INSERT INTO file_instances (id, workspace_id, rel_path, status) VALUES (1, 1, 'src/a.py', 'active')
             ON CONFLICT(id) DO NOTHING",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbols (symbol_hash, qualified_name, kind, start_line, end_line, file_instance_id)
             VALUES (?1, ?2, 'function', 10, 20, 1)",
            rusqlite::params![hash, name],
        )
        .unwrap();
    }

    #[test]
    fn codeowners_rules_respect_comments_and_owners() {
        let rules = parse_codeowners_rules(
            "# 顶层注释\n\n*.py @alice @bob  # 行内注释\n/docs/ @team\nC\n",
        );
        assert_eq!(rules.len(), 3);
        assert_eq!(rules[0]["pattern"], json!("*.py"));
        assert_eq!(rules[0]["owners"], json!(["@alice", "@bob"]));
        assert_eq!(rules[1]["pattern"], json!("/docs/"));
        assert_eq!(rules[2]["owners"], json!([]));
    }

    #[test]
    fn cosine_topk_filters_threshold_and_ties_by_hash() {
        let items = vec![
            ("b".to_string(), vec![1.0f32, 0.0]),
            ("a".to_string(), vec![1.0f32, 0.0]),
            ("c".to_string(), vec![0.0f32, 1.0]),
        ];
        let top = cosine_topk(&[1.0, 0.0], &items, 0.5, 3);
        assert_eq!(top.len(), 2);
        assert_eq!(top[0].0, "a"); // 同分按 hash 升序
        assert_eq!(top[1].0, "b");
        let none = cosine_topk(&[1.0, 0.0], &items, 1.5, 3);
        assert!(none.is_empty());
    }

    #[test]
    fn project_dependencies_detects_python_and_node() {
        let dir = std::env::temp_dir().join(format!("cw_sem_deps_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("pyproject.toml"),
            "[project]\nname='x'\ndependencies = [\n  # comment ignored\n  \"numpy>=1.24\",\n  \"mcp==1.28.0\",\n]\n",
        )
        .unwrap();
        std::fs::write(
            dir.join("package.json"),
            r#"{"dependencies": {"left-pad": "1.3.0"}}"#,
        )
        .unwrap();
        let langs: HashSet<String> = detect_project_languages(dir.to_str().unwrap())
            .into_iter()
            .collect();
        assert!(langs.contains("python"));
        assert!(langs.contains("typescript"));
        assert!(langs.contains("javascript"));
        let py = deps_for_lang(dir.to_str().unwrap(), "python");
        assert_eq!(py["numpy>=1.24"], json!(""));
        assert_eq!(py["mcp"], json!("1.28.0"));
        let ts = deps_for_lang(dir.to_str().unwrap(), "typescript");
        assert_eq!(ts["left-pad"], json!("1.3.0"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn symbol_commit_history_rejects_negative_limit_and_empty_hash() {
        let conn = temp_db();
        conn.execute(
            "INSERT INTO git_commits (id, commit_hash, message, author, email, timestamp, workspace_id)
             VALUES (1, 'abc', 'msg', 'a', 'a@b', 100, 1)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO git_symbol_changes (id, commit_hash, symbol_hash, change_type)
             VALUES (1, 'abc', 'h1', 'modified')",
            [],
        )
        .unwrap();
        let ok = handle_get_symbol_commit_history(
            &conn,
            1,
            &json!({ "symbol_hash": "h1", "limit": 5 }),
        )
        .unwrap();
        assert_eq!(ok.as_array().unwrap().len(), 1);
        assert_eq!(ok[0]["commit_hash"], json!("abc"));
        assert_eq!(ok[0]["change_type"], json!("modified"));
        let empty =
            handle_get_symbol_commit_history(&conn, 1, &json!({ "symbol_hash": "" })).unwrap();
        assert_eq!(empty, json!([]));
        let err = handle_get_symbol_commit_history(
            &conn,
            1,
            &json!({ "symbol_hash": "h1", "limit": -1 }),
        )
        .unwrap_err();
        assert_eq!(err.code, "invalid_params");
    }

    #[test]
    fn semantic_search_is_empty_without_embeddings_or_external_model() {
        let conn = temp_db();
        // 1) 无 embedding → 空列表
        let empty = handle_semantic_search(&conn, 1, &json!({ "query": "auth" })).unwrap();
        assert_eq!(empty, json!([]));
        // 2) 外部模型 embedding（非 hash-v1）→ fail-closed 空列表
        seed_symbol(&conn, "h1", "mod::a");
        conn.execute(
            "INSERT INTO symbol_embeddings (symbol_hash, embedding, model_version, dim, embedded_at)
             VALUES ('h1', ?1, 'jina-v2', 2, 1)",
            rusqlite::params![f32_blob(&[1.0f32, 0.0])],
        )
        .unwrap();
        let closed = handle_semantic_search(&conn, 1, &json!({ "query": "auth" })).unwrap();
        assert_eq!(closed, json!([]));
        // 3) hash-v1 确定性向量 → 走真实检索路径
        conn.execute(
            "UPDATE symbol_embeddings SET model_version = 'hash-v1' WHERE symbol_hash = 'h1'",
            [],
        )
        .unwrap();
        let hits = handle_semantic_search(&conn, 1, &json!({ "query": "auth", "top_k": 3 })).unwrap();
        assert_eq!(hits.as_array().unwrap().len(), 1);
        assert_eq!(hits[0]["qualified_name"], json!("mod::a"));
        assert!(hits[0]["similarity"].as_f64().unwrap().abs() <= 1.0);
    }

    #[test]
    fn find_similar_functions_needs_target_and_embedding() {
        let conn = temp_db();
        let missing =
            handle_find_similar_functions(&conn, 1, &json!({ "qualified_name": "nope" })).unwrap();
        assert_eq!(missing, json!([]));
        seed_symbol(&conn, "h1", "mod::a");
        // 有 symbol 无 embedding → 空
        let no_emb =
            handle_find_similar_functions(&conn, 1, &json!({ "qualified_name": "mod::a" })).unwrap();
        assert_eq!(no_emb, json!([]));
        // 注入 target + 候选 embedding
        conn.execute(
            "INSERT INTO symbol_embeddings (symbol_hash, embedding, model_version, dim, embedded_at)
             VALUES ('h1', ?1, 'hash-v1', 2, 1)",
            rusqlite::params![f32_blob(&[1.0f32, 0.0])],
        )
        .unwrap();
        seed_symbol(&conn, "h2", "mod::b");
        conn.execute(
            "INSERT INTO symbol_embeddings (symbol_hash, embedding, model_version, dim, embedded_at)
             VALUES ('h2', ?1, 'hash-v1', 2, 1)",
            rusqlite::params![f32_blob(&[0.9f32, 0.1])],
        )
        .unwrap();
        let hits = handle_find_similar_functions(
            &conn,
            1,
            &json!({ "qualified_name": "mod::a", "threshold": 0.5, "top_k": 5 }),
        )
        .unwrap();
        assert_eq!(hits.as_array().unwrap().len(), 1);
        assert_eq!(hits[0]["qualified_name"], json!("mod::b"));
    }

    #[test]
    fn parse_codeowners_handler_reads_workspace_default_path() {
        let dir = std::env::temp_dir().join(format!("cw_sem_co_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join(".github")).unwrap();
        std::fs::write(
            dir.join(".github/CODEOWNERS"),
            "# owners\n*.rs @rust-team\n",
        )
        .unwrap();
        let conn = temp_db();
        conn.execute(
            "INSERT INTO workspaces (id, root_path) VALUES (1, ?1)",
            rusqlite::params![dir.to_str().unwrap()],
        )
        .unwrap();
        let rules = handle_parse_codeowners(&conn, 1, &json!({})).unwrap();
        assert_eq!(rules.as_array().unwrap().len(), 1);
        assert_eq!(rules[0]["pattern"], json!("*.rs"));
        let explicit =
            handle_parse_codeowners(&conn, 1, &json!({ "file_path": dir.join("nope").to_str().unwrap() }))
                .unwrap();
        assert_eq!(explicit, json!([]));
        let _ = std::fs::remove_dir_all(&dir);
    }
}

// ============================================
// tools_security 组（P0-COMPAT-v3 T-1788963105720-60bfc80c）
// 15 方法：list_branches / merge_preview / get_edit_history /
// find_shared_symbols / cross_repo_impact / cross_repo_summary /
// lsp_hover / lsp_definition / lsp_references / lsp_diagnostics /
// lsp_completion / lsp_check_available / rule_candidate_list /
// rule_list / get_applicable_rules。
// SQL 语义复刻自 db/db_branch.py、db/db_edit.py、db/db_cross_repo.py、
// db/db_impact.py、db/db_agent_rules.py、db/db_lsp.py 与
// server/tools/tools_security.py worker handler。
// ============================================

use std::collections::{BTreeMap, BTreeSet, HashMap};

/// 查 workspace 名 → id（`WHERE name = ? LIMIT 1`，无大小写折叠；
/// 复刻 db_cross_repo._find_workspace_id_by_name）。
fn security_workspace_id_by_name(conn: &Connection, name: &str) -> Option<i64> {
    conn.query_row(
        "SELECT id FROM workspaces WHERE name = ?1 LIMIT 1",
        [name],
        |r| r.get::<_, i64>(0),
    )
    .optional()
    .unwrap_or(None)
}

/// 查 workspace root_path（get_edit_history 的相对路径解析依赖）。
fn security_workspace_root(conn: &Connection, workspace_id: i64) -> Option<String> {
    conn.query_row(
        "SELECT root_path FROM workspaces WHERE id = ?1",
        [workspace_id],
        |r| r.get::<_, String>(0),
    )
    .optional()
    .unwrap_or(None)
}

/// list_branches（复刻 db_branch.list_branch_workspaces：全部 workspace 按 id
/// 升序 + symbol_count 相关子查询）。
pub fn handle_list_branches(
    conn: &Connection,
    _workspace_id: i64,
    _params: &Value,
) -> Result<Value, DaemonRpcError> {
    let mut stmt = conn
        .prepare(
            "SELECT w.id, w.name, w.root_path, w.created_at, w.is_active, \
             (SELECT COUNT(*) FROM symbols s \
              JOIN file_instances fi ON s.file_instance_id = fi.id \
              WHERE fi.workspace_id = w.id) as symbol_count \
             FROM workspaces w ORDER BY w.id ASC",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
    let rows = stmt
        .query_map([], |r| {
            Ok(json!({
                "id": r.get::<_, i64>(0)?,
                "name": r.get::<_, String>(1)?,
                "root_path": r.get::<_, String>(2)?,
                "created_at": r.get::<_, f64>(3)?,
                "is_active": r.get::<_, i64>(4)?,
                "symbol_count": r.get::<_, i64>(5)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row.map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?);
    }
    Ok(Value::Array(out))
}

/// cross_layer_impact 的 db/api/config 三层正则提取（复刻 db_impact.py
/// Python 全路径；code 层由调用方负责 SQL 查询）。返回计数（by_layer 用）。
fn security_cross_layer_counts(
    source_name: &str,
    content: &str,
) -> (usize, usize, usize) {
    // DB 层：SQL 表名提取（FROM/UPDATE/INSERT INTO/DELETE FROM），BTreeSet 保序去重
    let mut table_names = BTreeSet::new();
    for pat in [
        r"(?i)\bFROM\s+(\w+)",
        r"(?i)\bUPDATE\s+(\w+)",
        r"(?i)\bINSERT\s+INTO\s+(\w+)",
        r"(?i)\bDELETE\s+FROM\s+(\w+)",
    ] {
        if let Ok(re) = regex::Regex::new(pat) {
            for cap in re.captures_iter(content) {
                if let Some(m) = cap.get(1) {
                    table_names.insert(m.as_str().to_string());
                }
            }
        }
    }
    let db_count = table_names.len();

    // API 层：函数名关键词 / HTTP 注解 / 路由装饰器（命中与否 → 0 或 1 条）
    let name_lower = source_name.to_lowercase();
    let is_api_name =
        name_lower.contains("route") || name_lower.contains("handler") || name_lower.contains("endpoint");
    let http_annotation = regex::Regex::new(r"(?i)#\[(?:get|post|put|delete|patch|head|options)\s*\(")
        .ok()
        .map(|re| re.is_match(content))
        .unwrap_or(false);
    let route_decorator = regex::Regex::new(r"(?i)@\w+\.(?:route|get|post|put|delete|patch)\s*\(")
        .ok()
        .map(|re| re.is_match(content))
        .unwrap_or(false);
    let api_count =
        usize::from(is_api_name || http_annotation || route_decorator);

    // 配置层：配置项引用提取
    let mut config_keys = BTreeSet::new();
    for pat in [
        r#"env::var\s*\(\s*['"]([^'"]+)['"]\s*\)"#,
        r#"std::env::var\s*\(\s*['"]([^'"]+)['"]\s*\)"#,
        r#"config\.get\s*\(\s*['"]([^'"]+)['"]\s*\)"#,
    ] {
        if let Ok(re) = regex::Regex::new(pat) {
            for cap in re.captures_iter(content) {
                if let Some(m) = cap.get(1) {
                    config_keys.insert(m.as_str().to_string());
                }
            }
        }
    }
    (db_count, api_count, config_keys.len())
}

/// blast_radius 的 SQL BFS 版（复刻 db_impact.blast_radius Python 全路径：
/// calls 反向逐层 BFS + cross_layer_impact 计数）。返回
/// (source_qn, layers 数组, total_impacted, by_layer)。
/// 源符号不存在 → None（调用方按 Python 语义返回空结构）。
fn security_blast_radius_sql(
    conn: &Connection,
    workspace_id: i64,
    symbol_hash: &str,
    depth: i64,
) -> Option<(String, Value, i64, Value)> {
    let row = conn
        .query_row(
            "SELECT s.id, s.symbol_hash, s.qualified_name, s.name, s.module_path, \
             s.visibility, s.kind, fi.rel_path \
             FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND s.symbol_hash = ?2 LIMIT 1",
            rusqlite::params![workspace_id, symbol_hash],
            |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, Option<String>>(2)?,
                    r.get::<_, Option<String>>(3)?,
                    r.get::<_, Option<String>>(4)?,
                    r.get::<_, Option<String>>(5)?,
                    r.get::<_, Option<String>>(6)?,
                    r.get::<_, Option<String>>(7)?,
                ))
            },
        )
        .optional()
        .ok()??;

    let source_qn = row.2.clone().unwrap_or_default();
    let layer0 = json!({
        "depth": 0,
        "symbols": [{
            "symbol_hash": row.1,
            "qualified_name": row.2,
            "name": row.3,
            "module_path": row.4,
            "file_path": row.7,
            "visibility": row.5,
            "kind": row.6,
        }],
    });

    let mut layers: Vec<Value> = vec![layer0];
    let mut visited_qn: HashSet<String> = HashSet::new();
    if !source_qn.is_empty() {
        visited_qn.insert(source_qn.clone());
    }
    let mut visited_hash: HashSet<String> = HashSet::new();
    visited_hash.insert(symbol_hash.to_string());
    let mut current_batch: Vec<i64> = vec![row.0];
    let mut total_impacted: i64 = 1;

    for d in 1..=depth {
        if current_batch.is_empty() {
            break;
        }
        // 复刻 Python f-string IN (?,?...) 动态占位符
        let placeholders: Vec<String> = current_batch.iter().map(|_| "?".to_string()).collect();
        let sql = format!(
            "SELECT DISTINCT s.id, s.symbol_hash, s.qualified_name, s.name, s.module_path, \
             s.visibility, s.kind, fi.rel_path \
             FROM calls c JOIN symbols s ON c.caller_id = s.id \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ? AND c.callee_id > 0 AND c.callee_id IN ({})",
            placeholders.join(",")
        );
        let mut stmt = match conn.prepare(&sql) {
            Ok(s) => s,
            Err(_) => break,
        };
        let mut bind_params: Vec<Box<dyn rusqlite::ToSql>> = vec![Box::new(workspace_id)];
        for id in &current_batch {
            bind_params.push(Box::new(*id));
        }
        let mut rows = match stmt.query(rusqlite::params_from_iter(bind_params.iter().map(|b| b.as_ref()))) {
            Ok(rows) => rows,
            Err(_) => break,
        };
        let mut next_batch: Vec<i64> = Vec::new();
        let mut layer_symbols: Vec<Value> = Vec::new();
        while let Ok(Some(r)) = rows.next() {
            let id: i64 = r.get(0).unwrap_or(0);
            let sh: String = r.get::<_, Option<String>>(1).unwrap_or(None).unwrap_or_default();
            let qn: String = r.get::<_, Option<String>>(2).unwrap_or(None).unwrap_or_default();
            let key = if qn.is_empty() { sh.clone() } else { qn.clone() };
            if visited_qn.contains(&key) || visited_hash.contains(&sh) {
                continue;
            }
            visited_qn.insert(key);
            visited_hash.insert(sh.clone());
            layer_symbols.push(json!({
                "symbol_hash": sh,
                "qualified_name": qn,
                "name": r.get::<_, Option<String>>(3).unwrap_or(None),
                "module_path": r.get::<_, Option<String>>(4).unwrap_or(None),
                "file_path": r.get::<_, Option<String>>(7).unwrap_or(None),
                "visibility": r.get::<_, Option<String>>(5).unwrap_or(None),
                "kind": r.get::<_, Option<String>>(6).unwrap_or(None),
            }));
            if !qn.is_empty() {
                next_batch.push(id);
            }
        }
        if !layer_symbols.is_empty() {
            total_impacted += layer_symbols.len() as i64;
            layers.push(json!({"depth": d, "symbols": layer_symbols}));
        }
        current_batch = next_batch;
    }

    // cross_layer_impact 计数（db/api/config 三层正则 + code 层 = 反向调用方 DISTINCT 数）
    let content: String = conn
        .query_row(
            "SELECT sc.content FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             LEFT JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash \
             WHERE fi.workspace_id = ?1 AND s.symbol_hash = ?2 LIMIT 1",
            rusqlite::params![workspace_id, symbol_hash],
            |r| r.get::<_, Option<String>>(0),
        )
        .optional()
        .ok()
        .flatten()
        .unwrap_or(None)
        .unwrap_or_default();
    let (db_n, api_n, config_n) = security_cross_layer_counts(
        &row.3.unwrap_or_default(),
        &content,
    );
    let code_n: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM calls c \
             JOIN symbols s ON c.caller_id = s.id \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND c.callee_id > 0 AND c.callee_id = ?2",
            rusqlite::params![workspace_id, row.0],
            |r| r.get(0),
        )
        .unwrap_or(0);

    Some((
        source_qn,
        Value::Array(layers),
        total_impacted,
        json!({"code": code_n, "db": db_n, "api": api_n, "config": config_n}),
    ))
}

/// merge_preview（复刻 tools_security._h_merge_preview 只读等价版：
/// diff_branches + 逐符号 blast_radius 聚合 + 风险分级）。
pub fn handle_merge_preview(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let source_branch = get_str_param_or(params, "source_branch", "");
    let target_branch = get_str_param_or(params, "target_branch", "");
    let diff = crate::daemon::snapshot_state::query_local_diff_branches(
        conn,
        &source_branch,
        &target_branch,
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("diff_branches 失败: {e}")))?;
    if diff.get("error").is_some() {
        return Ok(diff);
    }

    // 收集 target 侧需要分析的符号 hash（added.symbol_hash + modified.target_hash）
    let mut target_hashes: Vec<String> = Vec::new();
    if let Some(added) = diff.get("added").and_then(|v| v.as_array()) {
        for item in added {
            if let Some(h) = item.get("symbol_hash").and_then(|v| v.as_str()) {
                if !h.is_empty() {
                    target_hashes.push(h.to_string());
                }
            }
        }
    }
    if let Some(modified) = diff.get("modified").and_then(|v| v.as_array()) {
        for item in modified {
            if let Some(h) = item.get("target_hash").and_then(|v| v.as_str()) {
                if !h.is_empty() {
                    target_hashes.push(h.to_string());
                }
            }
        }
    }

    let mut seen: HashSet<String> = HashSet::new();
    let mut all_impacted: HashSet<String> = HashSet::new();
    let mut impact_layers: Vec<Value> = Vec::new();

    for symbol_hash in target_hashes {
        if !seen.insert(symbol_hash.clone()) {
            continue;
        }
        // Python worker 通道 blast_radius 异常时 continue（单符号失败不中断）；
        // Rust 侧同款容错：单符号失败跳过。
        let (source_symbol, _layers, total_impacted, by_layer) =
            match security_blast_radius_sql(conn, workspace_id, &symbol_hash, 3) {
                Some(v) => v,
                None => continue,
            };
        // all_impacted 收集 layers 里的 symbol_hash（与 Python 一致遍历 layers[].symbols[]）
        if let Some(layers) = _layers.as_array() {
            for layer in layers {
                if let Some(syms) = layer.get("symbols").and_then(|v| v.as_array()) {
                    for sym in syms {
                        if let Some(h) = sym.get("symbol_hash").and_then(|v| v.as_str()) {
                            if !h.is_empty() {
                                all_impacted.insert(h.to_string());
                            }
                        }
                    }
                }
            }
        }
        impact_layers.push(json!({
            "source_symbol": source_symbol,
            "source_hash": symbol_hash,
            "total_impacted": total_impacted,
            "by_layer": by_layer,
        }));
    }

    let affected_count = all_impacted.len() as i64;
    let risk_level = if affected_count > 20 {
        "high"
    } else if affected_count > 5 {
        "medium"
    } else {
        "low"
    };

    Ok(json!({
        "affected_symbols": affected_count,
        "impact_layers": impact_layers,
        "risk_level": risk_level,
    }))
}

/// get_edit_history（复刻 db_edit.get_edit_history：file_path 相对路径匹配 +
/// created_at 倒序 LIMIT）。Python _resolve_rel_path：绝对路径 →
/// relpath(workspace_root)；Rust 复刻前缀剥离 + 分隔符归一。
pub fn handle_get_edit_history(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let file_path = get_str_param_or(params, "file_path", "");
    let limit = get_int_param_or(params, "limit", 20);
    const COLS: &str = "id, file_path, operation, file_hash_before, file_hash_after, \
                        symbol_hash, agent_task_id, diff_summary, status, created_at, \
                        applied_at, reverted_at";
    let mut out: Vec<Value> = Vec::new();
    if !file_path.is_empty() {
        let mut rel_path = file_path.replace('\\', "/");
        if let Some(root) = security_workspace_root(conn, workspace_id) {
            let root_norm = root.replace('\\', "/");
            let root_trimmed = root_norm.trim_end_matches('/');
            if rel_path.starts_with(root_trimmed) {
                let stripped = &rel_path[root_trimmed.len()..];
                rel_path = stripped.trim_start_matches('/').to_string();
            }
        }
        let mut stmt = conn
            .prepare(&format!(
                "SELECT {COLS} FROM file_edit_audit WHERE file_path = ?1 \
                 ORDER BY created_at DESC LIMIT ?2"
            ))
            .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
        let rows = stmt
            .query_map([rel_path, limit.to_string()], security_edit_row_to_json)
            .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
        for row in rows {
            out.push(row.map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?);
        }
    } else {
        let mut stmt = conn
            .prepare(&format!(
                "SELECT {COLS} FROM file_edit_audit ORDER BY created_at DESC LIMIT ?1"
            ))
            .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
        let rows = stmt
            .query_map([limit], security_edit_row_to_json)
            .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
        for row in rows {
            out.push(row.map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?);
        }
    }
    Ok(Value::Array(out))
}

fn security_edit_row_to_json(r: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "id": r.get::<_, String>(0)?,
        "file_path": r.get::<_, String>(1)?,
        "operation": r.get::<_, String>(2)?,
        "file_hash_before": r.get::<_, Option<String>>(3)?,
        "file_hash_after": r.get::<_, Option<String>>(4)?,
        "symbol_hash": r.get::<_, Option<String>>(5)?,
        "agent_task_id": r.get::<_, Option<String>>(6)?,
        "diff_summary": r.get::<_, Option<String>>(7)?,
        "status": r.get::<_, String>(8)?,
        "created_at": r.get::<_, f64>(9)?,
        "applied_at": r.get::<_, Option<f64>>(10)?,
        "reverted_at": r.get::<_, Option<f64>>(11)?,
    }))
}

/// find_shared_symbols 核心（复刻 db_cross_repo.find_shared_symbols：
/// symbol_contents content_hash 分组 + 跨 workspace 配对）。供
/// handle_find_shared_symbols 与 handle_cross_repo_summary 复用。
fn security_find_shared_symbols(
    conn: &Connection,
    workspace_a: &str,
    workspace_b: &str,
) -> Value {
    // workspace_a 不存在 → 空结果（与 Python 一致）
    if !workspace_a.is_empty() && security_workspace_id_by_name(conn, workspace_a).is_none() {
        return json!({"total_shared": 0, "shared_symbols": []});
    }
    // workspace_b 指定但不存在 → Python 里 ws_b_id=None，所有配对被过滤 → 空
    let ws_b_id: Option<i64> = if workspace_b.is_empty() {
        None
    } else {
        security_workspace_id_by_name(conn, workspace_b)
    };

    let sql = if workspace_a.is_empty() {
        "SELECT sc.content_hash, s.qualified_name, fi.rel_path, w.id as ws_id, w.name as ws_name \
         FROM symbols s \
         JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash \
         JOIN file_instances fi ON s.file_instance_id = fi.id \
         JOIN workspaces w ON fi.workspace_id = w.id \
         WHERE s.kind = 'fn' ORDER BY sc.content_hash"
            .to_string()
    } else {
        format!(
            "SELECT sc.content_hash, s.qualified_name, fi.rel_path, w.id as ws_id, w.name as ws_name \
             FROM symbols s \
             JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             JOIN workspaces w ON fi.workspace_id = w.id \
             WHERE s.kind = 'fn' AND fi.workspace_id = {} ORDER BY sc.content_hash",
            security_workspace_id_by_name(conn, workspace_a).unwrap_or(0)
        )
    };

    let mut stmt = match conn.prepare(&sql) {
        Ok(s) => s,
        Err(_) => return json!({"total_shared": 0, "shared_symbols": []}),
    };
    let rows = match stmt.query_map([], |r| {
        Ok((
            r.get::<_, String>("content_hash")?,
            r.get::<_, Option<String>>("qualified_name")?,
            r.get::<_, Option<String>>("rel_path")?,
            r.get::<_, i64>("ws_id")?,
            r.get::<_, String>("ws_name")?,
        ))
    }) {
        Ok(rows) => rows,
        Err(_) => return json!({"total_shared": 0, "shared_symbols": []}),
    };
    let mut by_hash: HashMap<String, Vec<(i64, String, String, String, String)>> = HashMap::new();
    for row in rows.flatten() {
        by_hash.entry(row.0).or_default().push((row.3, row.4, row.1.unwrap_or_default(), row.2.unwrap_or_default(), String::new()));
    }

    // Python defaultdict 保插入序（content_hash 排序后首现序）；输出顺序对齐：
    // HashMap 无序 → 用 BTreeMap 按 content_hash 排序（Python 按 content_hash ORDER BY 分组序一致）
    let mut ordered: BTreeMap<String, Vec<(i64, String, String, String)>> = BTreeMap::new();
    for (h, mut syms) in by_hash {
        // 保留 Python 行序（同 hash 内按查询行序）
        let mut pairs: Vec<(i64, String, String, String)> = syms
            .drain(..)
            .map(|(ws_id, ws_name, qn, rel, _)| (ws_id, ws_name, qn, rel))
            .collect();
        ordered.insert(h, pairs);
    }

    let mut shared: Vec<Value> = Vec::new();
    for (content_hash, syms) in &ordered {
        if syms.len() < 2 {
            continue;
        }
        let ws_ids: HashSet<i64> = syms.iter().map(|s| s.0).collect();
        if ws_ids.len() < 2 {
            continue;
        }
        for i in 0..syms.len() {
            for j in (i + 1)..syms.len() {
                if syms[i].0 == syms[j].0 {
                    continue;
                }
                if let Some(b_id) = ws_b_id {
                    if syms[j].0 != b_id {
                        continue;
                    }
                }
                shared.push(json!({
                    "content_hash": content_hash,
                    "workspace_a": syms[i].1,
                    "workspace_b": syms[j].1,
                    "qualified_name_a": syms[i].2,
                    "qualified_name_b": syms[j].2,
                    "file_a": syms[i].3,
                    "file_b": syms[j].3,
                }));
            }
        }
    }

    let total = shared.len() as i64;
    json!({"total_shared": total, "shared_symbols": shared})
}

pub fn handle_find_shared_symbols(
    conn: &Connection,
    _workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let ws_a = get_str_param_or(params, "workspace_a", "");
    let ws_b = get_str_param_or(params, "workspace_b", "");
    Ok(security_find_shared_symbols(conn, &ws_a, &ws_b))
}

/// cross_repo_impact（复刻 db_cross_repo.cross_repo_impact：源符号定位 +
/// cross_repo_deps 反向依赖（哪些仓库依赖了我）+ local blast_radius 计数 +
/// 风险分级）。
pub fn handle_cross_repo_impact(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let symbol_hash = get_str_param_or(params, "symbol_hash", "");
    let depth = get_int_param_or(params, "depth", 2);

    let source = conn
        .query_row(
            "SELECT s.symbol_hash, s.qualified_name, w.name as ws_name \
             FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             JOIN workspaces w ON fi.workspace_id = w.id \
             WHERE s.symbol_hash = ?1 LIMIT 1",
            [&symbol_hash],
            |r| {
                Ok((
                    r.get::<_, Option<String>>(0)?,
                    r.get::<_, Option<String>>(1)?,
                    r.get::<_, String>(2)?,
                ))
            },
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
    let (source_qn, source_ws) = match source {
        Some((_h, qn, ws)) => (qn.unwrap_or_default(), ws),
        None => {
            return Ok(json!({
                "source_symbol": "",
                "source_workspace": "",
                "impacted_repos": [],
                "total_impacted_repos": 0,
                "risk_level": "none",
            }))
        }
    };

    // 反向依赖：cross_repo_deps 语义 = source 依赖 target；变更 target（被依赖方）
    // 影响所有依赖它的 source 仓库（P1-2 方向修正）
    let mut stmt = conn
        .prepare(
            "SELECT DISTINCT crd.source_workspace_id, w.name as source_ws_name, \
             crd.dependency_type, crd.confidence, crd.source_symbol_hash \
             FROM cross_repo_deps crd \
             JOIN workspaces w ON crd.source_workspace_id = w.id \
             WHERE crd.target_symbol_hash = ?1",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
    let rows = stmt
        .query_map([&symbol_hash], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, Option<String>>(2)?,
                r.get::<_, f64>(3)?,
                r.get::<_, Option<String>>(4)?,
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
    // 按 ws_name 聚合（首个 dependency_type/confidence 保留）
    let mut order: Vec<String> = Vec::new();
    let mut impacted: BTreeMap<String, (String, f64, Vec<String>)> = BTreeMap::new();
    for dep in rows.flatten() {
        let entry = impacted
            .entry(dep.1.clone())
            .or_insert_with(|| {
                order.push(dep.1.clone());
                (dep.2.clone().unwrap_or_default(), dep.3, Vec::new())
            });
        if let Some(sh) = dep.4 {
            if !sh.is_empty() {
                entry.2.push(sh);
            }
        }
    }

    // local blast_radius（Python worker 通道因 GraphStore 缺失恒异常 → 0；
    // Rust 实现完整 SQL BFS，行为改进已在 evidence 声明）
    let local_impacted_count = match security_blast_radius_sql(
        conn,
        workspace_id,
        &symbol_hash,
        depth,
    ) {
        Some((_, _, total, _)) => total,
        None => 0,
    };

    let impacted_list: Vec<Value> = order
        .iter()
        .map(|name| {
            let (dtype, conf, syms) = &impacted[name];
            json!({
                "workspace": name,
                "impacted_symbols": syms,
                "dependency_type": dtype,
                "confidence": conf,
            })
        })
        .collect();
    let total = impacted_list.len() as i64;
    let risk_level = if total > 3 {
        "high"
    } else if total > 1 {
        "medium"
    } else {
        "low"
    };

    Ok(json!({
        "source_symbol": source_qn,
        "source_workspace": source_ws,
        "local_impacted_count": local_impacted_count,
        "impacted_repos": impacted_list,
        "total_impacted_repos": total,
        "risk_level": risk_level,
    }))
}

/// cross_repo_summary（复刻 db_cross_repo.cross_repo_summary：repos 计数 +
/// cross_repo_deps 分组统计 + find_shared_symbols 全扫）。
pub fn handle_cross_repo_summary(
    conn: &Connection,
    _workspace_id: i64,
    _params: &Value,
) -> Result<Value, DaemonRpcError> {
    let mut stmt = conn
        .prepare(
            "SELECT w.id, w.name, w.root_path, w.created_at, \
             COUNT(DISTINCT s.id) as symbol_count \
             FROM workspaces w \
             LEFT JOIN file_instances fi ON fi.workspace_id = w.id \
             LEFT JOIN symbols s ON s.file_instance_id = fi.id \
             GROUP BY w.id ORDER BY w.created_at",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
    let rows = stmt
        .query_map([], |r| {
            Ok(json!({
                "id": r.get::<_, i64>(0)?,
                "name": r.get::<_, String>(1)?,
                "root_path": r.get::<_, String>(2)?,
                "created_at": r.get::<_, f64>(3)?,
                "symbol_count": r.get::<_, i64>(4)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
    let mut repos = Vec::new();
    for row in rows {
        repos.push(row.map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?);
    }

    let mut stmt = conn
        .prepare("SELECT dependency_type, COUNT(*) FROM cross_repo_deps GROUP BY dependency_type")
        .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
    let rows = stmt
        .query_map([], |r| {
            Ok((
                r.get::<_, Option<String>>(0)?,
                r.get::<_, i64>(1)?,
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
    let mut deps_by_type = serde_json::Map::new();
    for (dt, cnt) in rows.flatten() {
        deps_by_type.insert(dt.unwrap_or_default(), json!(cnt));
    }

    let total_deps: i64 = conn
        .query_row("SELECT COUNT(*) FROM cross_repo_deps", [], |r| r.get(0))
        .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;

    let shared = security_find_shared_symbols(conn, "", "");
    let total_shared = shared
        .get("total_shared")
        .and_then(|v| v.as_i64())
        .unwrap_or(0);

    Ok(json!({
        "total_repos": repos.len(),
        "repos": repos,
        "total_cross_deps": total_deps,
        "total_shared_symbols": total_shared,
        "deps_by_type": deps_by_type,
    }))
}

// ============================================
// LSP 组（6 方法）
// ============================================
//
// Python 真相源 db_lsp.py 通过 subprocess 启动 LSP 服务器（pylsp /
// typescript-language-server / gopls / rust-analyzer）做即时查询，未安装
// 时 fail-soft 返回 available=False 空结构。Rust daemon 不实现 LSP 子进程
// 客户端（长驻进程池属独立能力域），恒返回同款 fail-soft 空结构——与
// 「LSP 未安装」环境（本机实测 4 服务器均未安装）行为逐字段一致；
// lsp_check_available 仍按 where/which 实测探测，保真可用性语义。

fn security_lsp_validate_path(file_path: &str) -> bool {
    // SEC-002：拒绝空路径 / shell 元字符 / 目录遍历（失败路径返回的也是
    // available=False 空结构，校验只为语义完整性）
    if file_path.is_empty() {
        return false;
    }
    if file_path
        .chars()
        .any(|c| [';', '|', '&', '$', '`', '(', ')', '\n', '\r'].contains(&c))
    {
        return false;
    }
    !file_path.split(['/', '\\']).any(|seg| seg == "..")
}

pub fn handle_lsp_hover(
    _conn: &Connection,
    _workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let file_path = get_str_param_or(params, "file_path", "");
    let line = get_int_param_or(params, "line", 0);
    let character = get_int_param_or(params, "character", 0);
    let _ = security_lsp_validate_path(&file_path);
    Ok(json!({
        "file_path": file_path,
        "line": line,
        "character": character,
        "contents": "",
        "available": false,
    }))
}

pub fn handle_lsp_definition(
    _conn: &Connection,
    _workspace_id: i64,
    _params: &Value,
) -> Result<Value, DaemonRpcError> {
    Ok(json!({"definitions": [], "available": false}))
}

pub fn handle_lsp_references(
    _conn: &Connection,
    _workspace_id: i64,
    _params: &Value,
) -> Result<Value, DaemonRpcError> {
    Ok(json!({"references": [], "total": 0, "available": false}))
}

pub fn handle_lsp_diagnostics(
    _conn: &Connection,
    _workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let file_path = get_str_param_or(params, "file_path", "");
    let _ = security_lsp_validate_path(&file_path);
    Ok(json!({
        "file_path": file_path,
        "diagnostics": [],
        "total": 0,
        "available": false,
    }))
}

pub fn handle_lsp_completion(
    _conn: &Connection,
    _workspace_id: i64,
    _params: &Value,
) -> Result<Value, DaemonRpcError> {
    Ok(json!({"completions": [], "total": 0, "available": false}))
}

/// lsp_check_available：where/which 实测探测（复刻 db_lsp.lsp_check_available，
/// timeout 5s 内的探测；探测失败/未安装 → false）。
pub fn handle_lsp_check_available(
    _conn: &Connection,
    _workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    const SERVERS: [(&str, &str); 4] = [
        ("python", "pylsp"),
        ("typescript", "typescript-language-server"),
        ("go", "gopls"),
        ("rust", "rust-analyzer"),
    ];
    let language = get_str_param_or(params, "language", "");
    let which_cmd = if cfg!(windows) { "where" } else { "which" };
    let mut available = serde_json::Map::new();
    let mut total = 0i64;
    for (lang, command) in SERVERS {
        if !language.is_empty() && language != lang {
            continue;
        }
        let found = std::process::Command::new(which_cmd)
            .arg(command)
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false);
        available.insert(lang.to_string(), json!(found));
        if found {
            total += 1;
        }
    }
    // 未知语言：Python 里 available={lang: False}；Rust 同款
    if !language.is_empty() && !available.contains_key(&language) {
        available.insert(language.clone(), json!(false));
    }
    Ok(json!({
        "available_servers": available,
        "total_available": total,
    }))
}

// ============================================
// agent rules 组（3 方法）
// ============================================

fn security_deserialize_json_object(raw: &str) -> Value {
    if raw.is_empty() {
        return json!({});
    }
    match serde_json::from_str::<Value>(raw) {
        Ok(v @ Value::Object(_)) => v,
        _ => json!({}),
    }
}

/// scope_json / evidence_json 反序列化 + 规则行投影（复刻 _row_to_candidate）。
fn security_row_to_candidate(r: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    let scope_json: String = r.get::<_, Option<String>>(3)?.unwrap_or_default();
    let evidence_json: String = r.get::<_, Option<String>>(6)?.unwrap_or_default();
    Ok(json!({
        "id": r.get::<_, String>(0)?,
        "title": r.get::<_, String>(1)?,
        "rule_text": r.get::<_, String>(2)?,
        "scope": security_deserialize_json_object(&scope_json),
        "severity": r.get::<_, Option<String>>(4)?.unwrap_or_else(|| "info".to_string()),
        "source": r.get::<_, Option<String>>(5)?.unwrap_or_else(|| "manual".to_string()),
        "evidence": security_deserialize_json_object(&evidence_json),
        "confidence": r.get::<_, f64>(7)?,
        "status": r.get::<_, Option<String>>(8)?.unwrap_or_else(|| "pending".to_string()),
        "created_at": r.get::<_, f64>(9)?,
        "reviewed_at": r.get::<_, Option<f64>>(10)?,
        "reviewer": r.get::<_, Option<String>>(11)?.unwrap_or_default(),
        "linked_rule_id": r.get::<_, Option<String>>(12)?.unwrap_or_default(),
    }))
}

/// 复刻 _row_to_rule。
fn security_row_to_rule(r: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    let scope_json: String = r.get::<_, Option<String>>(3)?.unwrap_or_default();
    let evidence_json: String = r.get::<_, Option<String>>(7)?.unwrap_or_default();
    Ok(json!({
        "id": r.get::<_, String>(0)?,
        "title": r.get::<_, String>(1)?,
        "rule_text": r.get::<_, String>(2)?,
        "scope": security_deserialize_json_object(&scope_json),
        "severity": r.get::<_, Option<String>>(4)?.unwrap_or_else(|| "info".to_string()),
        "status": r.get::<_, Option<String>>(5)?.unwrap_or_else(|| "active".to_string()),
        "source_candidate_id": r.get::<_, Option<String>>(6)?.unwrap_or_default(),
        "evidence": security_deserialize_json_object(&evidence_json),
        "created_at": r.get::<_, f64>(8)?,
        "updated_at": r.get::<_, f64>(9)?,
        "synced_to_agents_md": r.get::<_, i64>(10)? != 0,
        "sync_hash": r.get::<_, Option<String>>(11)?.unwrap_or_default(),
    }))
}

/// rule_candidate_list（复刻 db_agent_rules.rule_candidate_list：
/// status 过滤（空串不过滤）+ created_at 倒序 + limit<=0 空列表）。
pub fn handle_rule_candidate_list(
    conn: &Connection,
    _workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let status = get_str_param_or(params, "status", "pending");
    let limit = get_int_param_or(params, "limit", 50);
    if limit <= 0 {
        return Ok(json!({"candidates": [], "count": 0}));
    }
    const COLS: &str = "id, title, rule_text, scope_json, severity, source, evidence_json, \
                        confidence, status, created_at, reviewed_at, reviewer, linked_rule_id";
    let mut out: Vec<Value> = Vec::new();
    if !status.is_empty() {
        let mut stmt = conn
            .prepare(&format!(
                "SELECT {COLS} FROM agent_rule_candidates WHERE status = ?1 \
                 ORDER BY created_at DESC LIMIT ?2"
            ))
            .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
        let rows = stmt
            .query_map([status, limit.to_string()], security_row_to_candidate)
            .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
        for row in rows {
            out.push(row.map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?);
        }
    } else {
        let mut stmt = conn
            .prepare(&format!(
                "SELECT {COLS} FROM agent_rule_candidates ORDER BY created_at DESC LIMIT ?1"
            ))
            .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
        let rows = stmt
            .query_map([limit], security_row_to_candidate)
            .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
        for row in rows {
            out.push(row.map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?);
        }
    }
    let count = out.len() as i64;
    Ok(json!({"candidates": out, "count": count}))
}

/// rule_list（复刻 db_agent_rules.rule_list：severity CASE 排序 + updated_at 倒序）。
pub fn handle_rule_list(
    conn: &Connection,
    _workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let status = get_str_param_or(params, "status", "active");
    let limit = get_int_param_or(params, "limit", 100);
    if limit <= 0 {
        return Ok(json!({"rules": [], "count": 0}));
    }
    const COLS: &str = "id, title, rule_text, scope_json, severity, status, \
                        source_candidate_id, evidence_json, created_at, updated_at, \
                        synced_to_agents_md, sync_hash";
    const ORDER: &str = "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1 \
                         WHEN 'warning' THEN 2 WHEN 'info' THEN 3 ELSE 4 END, updated_at DESC";
    let mut out: Vec<Value> = Vec::new();
    if !status.is_empty() {
        let mut stmt = conn
            .prepare(&format!(
                "SELECT {COLS} FROM agent_rules WHERE status = ?1 {ORDER} LIMIT ?2"
            ))
            .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
        let rows = stmt
            .query_map([status, limit.to_string()], security_row_to_rule)
            .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
        for row in rows {
            out.push(row.map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?);
        }
    } else {
        let mut stmt = conn
            .prepare(&format!(
                "SELECT {COLS} FROM agent_rules {ORDER} LIMIT ?1"
            ))
            .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
        let rows = stmt
            .query_map([limit], security_row_to_rule)
            .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
        for row in rows {
            out.push(row.map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?);
        }
    }
    let count = out.len() as i64;
    Ok(json!({"rules": out, "count": count}))
}

/// Python fnmatch 单段翻译（* ? [seq]）→ regex；复刻 _match_scope 的
/// file_patterns glob 语义。pattern 无效时回退字面匹配（fnmatch 对
/// 非法 pattern 宽容，Python fnmatch.translate 不抛错）。
fn security_fnmatch_to_regex(pattern: &str) -> Option<regex::Regex> {
    let mut re = String::from("^");
    let mut chars = pattern.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '*' => re.push_str(".*"),
            '?' => re.push('.'),
            '[' => {
                // [seq] / [!seq] 转译
                let mut seq = String::from("[");
                let mut closed = false;
                while let Some(&nc) = chars.peek() {
                    chars.next();
                    if nc == ']' {
                        closed = true;
                        break;
                    }
                    seq.push(nc);
                }
                if !closed {
                    // 未闭合 → Python translate 视为字面 '['
                    re.push_str("\\[");
                } else if seq.is_empty() {
                    re.push_str("\\[\\]");
                } else {
                    if seq.starts_with('!') {
                        seq = format!("^{}", &seq[1..]);
                    }
                    re.push('[');
                    re.push_str(&seq);
                    re.push(']');
                }
            }
            c => {
                if regex::escape(&c.to_string()).len() > 1 {
                    re.push_str(&regex::escape(&c.to_string()));
                } else {
                    re.push(c);
                }
            }
        }
    }
    re.push('$');
    regex::Regex::new(&re).ok()
}

/// _match_scope 复刻：返回 (命中标签列表, 是否匹配)。
/// scope 字段间 AND，字段内 OR；缺失上下文字段视为不命中。
fn security_match_scope(scope: &Value, params_ctx: &Value) -> (Vec<String>, bool) {
    let mut labels: Vec<String> = Vec::new();

    let arr = |v: &Value, key: &str| -> Vec<String> {
        v.get(key)
            .and_then(|x| x.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|i| i.as_str().map(|s| s.to_string()))
                    .collect()
            })
            .unwrap_or_default()
    };
    // 1. languages
    let scope_langs = arr(scope, "languages");
    if !scope_langs.is_empty() {
        let ctx_lang = params_ctx
            .get("language")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_lowercase();
        if ctx_lang.is_empty() || !scope_langs.iter().any(|s| s.to_lowercase() == ctx_lang) {
            return (vec![], false);
        }
        labels.push(format!("language:{ctx_lang}"));
    }

    // 2. file_patterns（glob）
    let scope_patterns = arr(scope, "file_patterns");
    if !scope_patterns.is_empty() {
        let ctx_file = params_ctx
            .get("file_path")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if ctx_file.is_empty() {
            return (vec![], false);
        }
        // Python fnmatch.fnmatch 在 Windows 上经 os.path.normcase 把
        // pattern 与 name 都转小写（大小写不敏感）；POSIX 保持原样。
        let norm = |s: &str| -> String {
            if cfg!(windows) { s.to_lowercase() } else { s.to_string() }
        };
        let hit = scope_patterns.iter().any(|pat| {
            security_fnmatch_to_regex(&norm(pat))
                .map(|re| re.is_match(&norm(ctx_file)))
                .unwrap_or(false)
        });
        if !hit {
            return (vec![], false);
        }
        labels.push(format!("file:{ctx_file}"));
    }

    // 3. symbol_kinds
    let scope_kinds = arr(scope, "symbol_kinds");
    if !scope_kinds.is_empty() {
        let ctx_kind = params_ctx
            .get("symbol_kind")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_lowercase();
        if ctx_kind.is_empty() || !scope_kinds.iter().any(|s| s.to_lowercase() == ctx_kind) {
            return (vec![], false);
        }
        labels.push(format!("symbol_kind:{ctx_kind}"));
    }

    // 4. actions
    let scope_actions = arr(scope, "actions");
    if !scope_actions.is_empty() {
        let ctx_action = params_ctx
            .get("action")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_lowercase();
        if ctx_action.is_empty() || !scope_actions.iter().any(|s| s.to_lowercase() == ctx_action) {
            return (vec![], false);
        }
        labels.push(format!("action:{ctx_action}"));
    }

    // 5. finding_types
    let scope_findings = arr(scope, "finding_types");
    if !scope_findings.is_empty() {
        let ctx_ftype = params_ctx
            .get("finding_type")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_lowercase();
        if ctx_ftype.is_empty() || !scope_findings.iter().any(|s| s.to_lowercase() == ctx_ftype) {
            return (vec![], false);
        }
        labels.push(format!("finding_type:{ctx_ftype}"));
    }

    // 6. module_prefixes（前缀匹配）
    let scope_prefixes = arr(scope, "module_prefixes");
    if !scope_prefixes.is_empty() {
        let ctx_module = params_ctx
            .get("module_prefix")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if ctx_module.is_empty() || !scope_prefixes.iter().any(|p| ctx_module.starts_with(p.as_str())) {
            return (vec![], false);
        }
        labels.push(format!("module:{ctx_module}"));
    }

    (labels, true)
}

/// get_applicable_rules（复刻 db_agent_rules.get_applicable_rules：active 规则
/// 全查 LIMIT 500 → 内存 scope 匹配 → severity/精度/updated_at 排序 → limit）。
pub fn handle_get_applicable_rules(
    conn: &Connection,
    _workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let limit = get_int_param_or(params, "limit", 10);
    // Python handler 默认 {}（params.get("context", {})）；缺省空对象
    let context = params.get("context").cloned().unwrap_or_else(|| json!({}));
    if limit <= 0 {
        return Ok(json!({"rules": [], "count": 0}));
    }

    const COLS: &str = "id, title, rule_text, scope_json, severity, status, \
                        source_candidate_id, evidence_json, created_at, updated_at, \
                        synced_to_agents_md, sync_hash";
    let mut stmt = conn
        .prepare(&format!(
            "SELECT {COLS} FROM agent_rules WHERE status = 'active' \
             ORDER BY updated_at DESC LIMIT 500"
        ))
        .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;
    let rows = stmt
        .query_map([], security_row_to_rule)
        .map_err(|e| DaemonRpcError::internal_error(format!("security 组 SQL 失败: {e}")))?;

    // SEVERITY_ORDER = {"critical": 4, "error": 3, "warning": 2, "info": 1}（缺省 0）
    let severity_order = |sev: &str| -> i64 {
        match sev {
            "critical" => 4,
            "error" => 3,
            "warning" => 2,
            "info" => 1,
            _ => 0,
        }
    };

    struct Matched {
        rule: Value,
        order_key: (std::cmp::Reverse<i64>, std::cmp::Reverse<usize>),
        updated_at: f64,
    }
    let mut matched: Vec<Matched> = Vec::new();
    for rule in rows.flatten() {
        let scope = rule.get("scope").cloned().unwrap_or_else(|| json!({}));
        let scope_empty = scope.as_object().map(|o| o.is_empty()).unwrap_or(true);
        let (labels, ok) = if scope_empty {
            (vec!["global".to_string()], true)
        } else {
            security_match_scope(&scope, &context)
        };
        if !ok {
            continue;
        }
        let mut rule = rule;
        rule.as_object_mut()
            .unwrap()
            .insert("matched_scope".to_string(), json!(labels.clone()));
        let sev = rule
            .get("severity")
            .and_then(|v| v.as_str())
            .unwrap_or("info")
            .to_string();
        let precision = labels.len();
        let updated_at = rule.get("updated_at").and_then(|v| v.as_f64()).unwrap_or(0.0);
        matched.push(Matched {
            rule,
            order_key: (
                std::cmp::Reverse(severity_order(&sev)),
                std::cmp::Reverse(precision),
            ),
            updated_at,
        });
    }
    matched.sort_by(|a, b| {
        a.order_key
            .cmp(&b.order_key)
            .then(b.updated_at.partial_cmp(&a.updated_at).unwrap_or(std::cmp::Ordering::Equal))
    });

    let out: Vec<Value> = matched.into_iter().take(limit as usize).map(|m| m.rule).collect();
    let count = out.len() as i64;
    Ok(json!({"rules": out, "count": count}))
}

// ============================================================
// P0-COMPAT-v3（T-1788963106520-907544c8）：tools_summary 组（19）
// 复刻 server/tools/tools_summary.py → db 层 Python 真相源：
// - db/db_summary.py：get_summary / project_brief / repo_map
// - db/db_coverage.py：test_impact_selection
// - db/db_ownership.py：who_to_ask / get_ownership_map
// - db/db_guardrail.py：guardrail_scan / guardrail_check_edit / guardrail_list_rules
// - db/db_impact.py / db_vector.py / db_token_savings.py / db_evolution.py /
//   db_defect_kb.py：其余 10 方法
// 写一面基线（2026-09-10 mode=ro 探针实证）：
// - guardrail_scan / guardrail_list_rules：_init_builtin_rules 首行 INSERT →
//   OperationalError("attempt to write a readonly database")，恒 error；
//   Rust fail-closed 同语义（写面不落只读快照连接）。
// - defect_learn：无变更 → {"learned_patterns":0,...}；有 qualifying 变更 →
//   INSERT → readonly error。
// - ask_codebase：embedder 不可用 → keyword_fallback（live 实证 fallback_used）。
// i18n：t() 在部署机解析为 zh_CN（i18n/zh_CN.json 键值，2026-09-10 抽取）。
// ============================================================

/// summary_detect_language —— 复刻 config.detect_language_from_path
/// （LANGUAGE_CONFIG 首个命中扩展名的语言；.h 归 c，config 字典序即优先级）。
fn summary_detect_language(rel_path: &str) -> String {
    let ext = match rel_path.rfind('.') {
        Some(idx) => rel_path[idx..].to_ascii_lowercase(),
        None => return String::new(),
    };
    const CONFIG: &[(&str, &[&str])] = &[
        ("rust", &[".rs"]),
        ("typescript", &[".ts", ".tsx"]),
        ("javascript", &[".js", ".jsx", ".mjs", ".cjs"]),
        ("python", &[".py"]),
        ("kotlin", &[".kt", ".kts"]),
        ("go", &[".go"]),
        ("java", &[".java"]),
        ("c", &[".c", ".h"]),
        ("cpp", &[".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h"]),
        ("csharp", &[".cs"]),
        ("ruby", &[".rb"]),
        ("php", &[".php"]),
        ("swift", &[".swift"]),
        ("scala", &[".scala", ".sc"]),
        ("hcl", &[".tf", ".hcl"]),
        ("elixir", &[".ex", ".exs"]),
    ];
    for (lang, exts) in CONFIG {
        if exts.contains(&ext.as_str()) {
            return (*lang).to_string();
        }
    }
    String::new()
}

/// summary_cyclomatic_complexity —— 复刻 db_metrics._compute_cyclomatic_complexity。
fn summary_cyclomatic_complexity(content: &str, language: &str) -> i64 {
    if content.is_empty() {
        return 1;
    }
    let mut complexity: i64 = 1;
    let keyword_patterns = [
        r"\bif\b", r"\belse\b", r"\bfor\b", r"\bwhile\b", r"\bmatch\b", r"\bcase\b",
        r"\bcatch\b", r"\b&&\b", r"\b\|\|\b", r"\btry\b", r"\bexcept\b", r"\bfinally\b",
        r"\bwhen\b", r"\bguard\b",
    ];
    for pat in keyword_patterns {
        if let Ok(re) = regex::Regex::new(pat) {
            complexity += re.find_iter(content).count() as i64;
        }
    }
    if matches!(language, "rust" | "c" | "java" | "typescript" | "javascript" | "go") {
        if let Ok(re) = regex::Regex::new(r"\?\s*[^:]+\s*:") {
            complexity += re.find_iter(content).count() as i64;
        }
    }
    if language == "python" {
        if let Ok(re) = regex::Regex::new(r"\bfor\b.*\bin\b") {
            complexity += re.find_iter(content).count() as i64;
        }
    }
    complexity
}

/// summary_complexity_hotspots —— 复刻 db_metrics.get_complexity_hotspots。
fn summary_complexity_hotspots(
    conn: &Connection,
    workspace_id: i64,
    limit: i64,
    module_filter: &str,
) -> Result<Vec<Value>, DaemonRpcError> {
    let sql = if module_filter.is_empty() {
        "SELECT s.qualified_name, s.start_line, s.end_line, s.depth, s.module_path, \
         sc.content, fi.rel_path \
         FROM symbols s \
         JOIN file_instances fi ON s.file_instance_id = fi.id \
         LEFT JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash \
         WHERE fi.workspace_id = ?1 AND s.kind IN ('fn','function','method')"
            .to_string()
    } else {
        format!(
            "{} AND s.module_path LIKE ?2",
            "SELECT s.qualified_name, s.start_line, s.end_line, s.depth, s.module_path, \
             sc.content, fi.rel_path \
             FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             LEFT JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash \
             WHERE fi.workspace_id = ?1 AND s.kind IN ('fn','function','method')"
        )
    };
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("hotspots prepare: {e}")))?;
    let map_row = |r: &rusqlite::Row<'_>| -> rusqlite::Result<(String, Option<i64>, Option<i64>, i64, Option<String>, Option<String>, Option<String>)> {
        Ok((
            r.get(0)?,
            r.get(1)?,
            r.get(2)?,
            r.get::<_, Option<i64>>(3)?.unwrap_or(0),
            r.get(4)?,
            r.get(5)?,
            r.get(6)?,
        ))
    };
    let mut rows = if module_filter.is_empty() {
        stmt.query_map([workspace_id], map_row)
            .map_err(|e| DaemonRpcError::internal_error(format!("hotspots query: {e}")))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| DaemonRpcError::internal_error(format!("hotspots collect: {e}")))?
    } else {
        let like = format!("{module_filter}%");
        stmt.query_map(rusqlite::params![workspace_id, like], map_row)
            .map_err(|e| DaemonRpcError::internal_error(format!("hotspots query: {e}")))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| DaemonRpcError::internal_error(format!("hotspots collect: {e}")))?
    };
    let mut results: Vec<Value> = rows
        .into_iter()
        .map(|(qn, start_line, end_line, depth, module_path, content, rel_path)| {
            let content = content.clone().unwrap_or_default();
            let lang = rel_path
                .as_deref()
                .map(summary_detect_language)
                .unwrap_or_default();
            let complexity = summary_cyclomatic_complexity(&content, &lang);
            let line_count = match (start_line, end_line) {
                (Some(s), Some(e)) if s != 0 && e != 0 => e - s + 1,
                _ => 0,
            };
            json!({
                "qualified_name": qn,
                "file_path": rel_path,
                "start_line": start_line,
                "line_count": line_count,
                "cyclomatic_complexity": complexity,
                "depth": if depth >= 0 { depth } else { 0 },
                "module_path": module_path,
            })
        })
        .collect();
    // Python sort 稳定（复杂度降序，同分保持行序）；Rust sort_by 同为稳定排序
    results.sort_by(|a, b| {
        let ca = a["cyclomatic_complexity"].as_i64().unwrap_or(0);
        let cb = b["cyclomatic_complexity"].as_i64().unwrap_or(0);
        cb.cmp(&ca)
    });
    Ok(results
        .into_iter()
        .take(limit.max(0) as usize)
        .collect())
}

/// summary_metrics_summary —— 复刻 db_metrics.get_code_metrics_summary。
///
/// 8 字段 legacy 契约（file_count / function_count / total_lines / total_calls /
/// avg_complexity / max_complexity / complexity_distribution / comment_coverage）。
/// `query.metrics_summary` 与 `project_brief` 共用本实现，保证两处对客户端暴露
/// 同一契约（避免 metrics_handlers 出现第二套字段集）。
pub(crate) fn summary_metrics_summary(
    conn: &Connection,
    workspace_id: i64,
) -> Result<Value, DaemonRpcError> {
    let file_count = scalar_i64(
        conn,
        "SELECT COUNT(*) FROM file_instances WHERE workspace_id = ? AND status != 'archived'",
        workspace_id,
    )?;
    let function_count = scalar_i64(
        conn,
        "SELECT COUNT(*) FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id \
         WHERE fi.workspace_id = ? AND fi.status != 'archived' AND s.kind IN ('fn','function','method')",
        workspace_id,
    )?;
    let total_lines: i64 = conn
        .query_row(
            "SELECT COALESCE(SUM(fi.total_lines), 0) FROM file_instances fi \
             WHERE fi.workspace_id = ?1 AND fi.status != 'archived'",
            [workspace_id],
            |r| r.get(0),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("total_lines: {e}")))?;

    let mut stmt = conn
        .prepare(
            "SELECT sc.content, fi.rel_path \
             FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             LEFT JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash \
             WHERE fi.workspace_id = ?1 AND fi.status != 'archived' AND s.kind IN ('fn','function','method')",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("complexity scan prepare: {e}")))?;
    let rows: Vec<(Option<String>, Option<String>)> = stmt
        .query_map([workspace_id], |r| Ok((r.get(0)?, r.get(1)?)))
        .map_err(|e| DaemonRpcError::internal_error(format!("complexity scan: {e}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("complexity collect: {e}")))?;

    let mut bucket_low: i64 = 0;
    let mut bucket_mid: i64 = 0;
    let mut bucket_high: i64 = 0;
    let mut bucket_extreme: i64 = 0;
    let mut total_complexity: i64 = 0;
    let mut max_complexity: i64 = 0;
    let mut with_content: i64 = 0;
    for (content, rel_path) in &rows {
        let content = content.clone().unwrap_or_default();
        if content.is_empty() {
            continue;
        }
        let lang = rel_path
            .as_deref()
            .map(summary_detect_language)
            .unwrap_or_default();
        let complexity = summary_cyclomatic_complexity(&content, &lang);
        total_complexity += complexity;
        if complexity > max_complexity {
            max_complexity = complexity;
        }
        with_content += 1;
        if complexity <= 5 {
            bucket_low += 1;
        } else if complexity <= 10 {
            bucket_mid += 1;
        } else if complexity <= 20 {
            bucket_high += 1;
        } else {
            bucket_extreme += 1;
        }
    }
    let avg_complexity = if with_content > 0 {
        total_complexity as f64 / with_content as f64
    } else {
        0.0
    };
    let total_calls = scalar_i64(
        conn,
        "SELECT COUNT(*) FROM calls c JOIN symbols s ON c.caller_id = s.id \
         JOIN file_instances fi ON s.file_instance_id = fi.id \
         WHERE fi.workspace_id = ? AND fi.status != 'archived'",
        workspace_id,
    )?;
    let (comment_total, commented): (i64, i64) = conn
        .query_row(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN s.has_comment = 1 THEN 1 ELSE 0 END), 0) \
             FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND s.kind IN ('fn','function','method')",
            [workspace_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("comment coverage: {e}")))?;
    let comment_coverage = if comment_total > 0 {
        commented as f64 / comment_total as f64 * 100.0
    } else {
        0.0
    };
    Ok(json!({
        "file_count": file_count,
        "function_count": function_count,
        "total_lines": total_lines,
        "total_calls": total_calls,
        "avg_complexity": (avg_complexity * 10.0).round() / 10.0,
        "max_complexity": max_complexity,
        "complexity_distribution": {
            "低 (≤5)": bucket_low,
            "中 (6-10)": bucket_mid,
            "高 (11-20)": bucket_high,
            "极高 (>20)": bucket_extreme,
        },
        "comment_coverage": (comment_coverage * 10.0).round() / 10.0,
    }))
}

/// summary_coupling_analysis —— 复刻 db_metrics.get_coupling_analysis。
fn summary_coupling_analysis(
    conn: &Connection,
    workspace_id: i64,
    limit: i64,
) -> Result<Vec<Value>, DaemonRpcError> {
    let mut stmt = conn
        .prepare(
            "SELECT s_caller.module_path, c.callee_module, COUNT(*) \
             FROM calls c \
             JOIN symbols s_caller ON c.caller_id = s_caller.id \
             JOIN file_instances fi ON s_caller.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 \
               AND s_caller.module_path != '' AND c.callee_module != '' \
               AND s_caller.module_path != c.callee_module \
             GROUP BY s_caller.module_path, c.callee_module",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("coupling prepare: {e}")))?;
    let rows: Vec<(String, String, i64)> = stmt
        .query_map([workspace_id], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, i64>(2)?))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("coupling query: {e}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("coupling collect: {e}")))?;

    // afferent/efferent 按首见顺序累积（Python defaultdict 插入序）
    let mut order: Vec<String> = Vec::new();
    let mut afferent: HashMap<String, i64> = HashMap::new();
    let mut efferent: HashMap<String, i64> = HashMap::new();
    for (caller_mod, callee_mod, cnt) in rows {
        *efferent.entry(caller_mod.clone()).or_insert(0) += cnt;
        *afferent.entry(callee_mod.clone()).or_insert(0) += cnt;
        if !order.contains(&caller_mod) {
            order.push(caller_mod);
        }
        if !order.contains(&callee_mod) {
            order.push(callee_mod);
        }
    }
    let mut results: Vec<Value> = order
        .iter()
        .map(|m| {
            let aff = afferent.get(m).copied().unwrap_or(0);
            let eff = efferent.get(m).copied().unwrap_or(0);
            let total = aff + eff;
            let instability = if total > 0 { eff as f64 / total as f64 } else { 0.0 };
            json!({
                "module": m,
                "afferent": aff,
                "efferent": eff,
                "total_coupling": total,
                "instability": (instability * 100.0).round() / 100.0,
            })
        })
        .collect();
    results.sort_by(|a, b| {
        let ta = a["total_coupling"].as_i64().unwrap_or(0);
        let tb = b["total_coupling"].as_i64().unwrap_or(0);
        tb.cmp(&ta)
    });
    Ok(results.into_iter().take(limit.max(0) as usize).collect())
}

/// summary_largest_functions —— 复刻 db_metrics.get_largest_functions
///（health check 只用 line_count/qualified_name/start_line/depth/file_path）。
fn summary_largest_functions(
    conn: &Connection,
    workspace_id: i64,
    limit: i64,
) -> Result<Vec<Value>, DaemonRpcError> {
    let mut stmt = conn
        .prepare(
            "SELECT s.qualified_name, s.start_line, s.end_line, s.depth, fi.rel_path \
             FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND s.kind IN ('fn','function','method')",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("largest prepare: {e}")))?;
    let mut results: Vec<Value> = stmt
        .query_map([workspace_id], |r| {
            let qn: String = r.get(0)?;
            let start_line: Option<i64> = r.get(1)?;
            let end_line: Option<i64> = r.get(2)?;
            let depth: i64 = r.get::<_, Option<i64>>(3)?.unwrap_or(0);
            let rel_path: Option<String> = r.get(4)?;
            let line_count = match (start_line, end_line) {
                (Some(s), Some(e)) if s != 0 && e != 0 => e - s + 1,
                _ => 0,
            };
            Ok(json!({
                "qualified_name": qn,
                "file_path": rel_path,
                "start_line": start_line,
                "line_count": line_count,
                "depth": if depth >= 0 { depth } else { 0 },
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("largest query: {e}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("largest collect: {e}")))?;
    results.sort_by(|a, b| {
        let la = a["line_count"].as_i64().unwrap_or(0);
        let lb = b["line_count"].as_i64().unwrap_or(0);
        lb.cmp(&la)
    });
    Ok(results.into_iter().take(limit.max(0) as usize).collect())
}

/// summary_health_check —— 复刻 db_metrics.get_code_health_check。
/// severity 过滤语义与 Python 一致（"all" 不过滤；其余按 item.severity 匹配）。
fn summary_health_check(
    conn: &Connection,
    workspace_id: i64,
    severity: &str,
) -> Result<Value, DaemonRpcError> {
    // 1. 大文件
    let mut stmt = conn
        .prepare(
            "SELECT rel_path, total_lines FROM file_instances \
             WHERE workspace_id = ?1 AND total_lines > 0 ORDER BY total_lines DESC LIMIT 20",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("health files prepare: {e}")))?;
    let file_rows: Vec<(Option<String>, i64)> = stmt
        .query_map([workspace_id], |r| Ok((r.get(0)?, r.get(1)?)))
        .map_err(|e| DaemonRpcError::internal_error(format!("health files: {e}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("health files collect: {e}")))?;
    let mut large_files: Vec<Value> = Vec::new();
    for (rel_path, lines) in file_rows {
        let (sev, advice) = if lines >= 2000 {
            ("high", "严重过大，强烈建议按功能拆分为多个模块文件")
        } else if lines >= 1000 {
            ("medium", "文件较大，建议考虑拆分")
        } else if lines >= 500 {
            ("low", "可考虑按职责拆分")
        } else {
            continue;
        };
        large_files.push(json!({
            "file_path": rel_path, "total_lines": lines,
            "severity": sev, "advice": advice,
        }));
    }

    // 2. 复杂函数
    let hotspots = summary_complexity_hotspots(conn, workspace_id, 30, "")?;
    let mut complex_functions: Vec<Value> = Vec::new();
    for fn_v in &hotspots {
        let comp = fn_v["cyclomatic_complexity"].as_i64().unwrap_or(0);
        let (sev, advice) = if comp >= 30 {
            ("high", "极复杂函数，必须重构拆分，否则 AI 难以正确理解和修改")
        } else if comp >= 20 {
            ("medium", "复杂度高，建议拆分为多个小函数")
        } else if comp >= 10 {
            ("low", "复杂度中等，可考虑优化")
        } else {
            continue;
        };
        complex_functions.push(json!({
            "qualified_name": fn_v["qualified_name"],
            "file_path": fn_v["file_path"],
            "start_line": fn_v["start_line"],
            "line_count": fn_v["line_count"],
            "cyclomatic_complexity": comp,
            "depth": fn_v["depth"],
            "severity": sev, "advice": advice,
        }));
    }

    // 3. 超长函数
    let largest = summary_largest_functions(conn, workspace_id, 30)?;
    let mut long_functions: Vec<Value> = Vec::new();
    for fn_v in &largest {
        let lines = fn_v["line_count"].as_i64().unwrap_or(0);
        let (sev, advice) = if lines >= 200 {
            ("high", "函数过长，强烈建议拆分，AI 读取和修改都容易出问题")
        } else if lines >= 100 {
            ("medium", "函数较长，建议拆分")
        } else if lines >= 50 {
            ("low", "可考虑拆分")
        } else {
            continue;
        };
        long_functions.push(json!({
            "qualified_name": fn_v["qualified_name"],
            "file_path": fn_v["file_path"],
            "start_line": fn_v["start_line"],
            "line_count": lines,
            "depth": fn_v["depth"],
            "severity": sev, "advice": advice,
        }));
    }

    // 4. 高耦合模块
    let coupling = summary_coupling_analysis(conn, workspace_id, 20)?;
    let mut high_coupling: Vec<Value> = Vec::new();
    for m in &coupling {
        let inst = m["instability"].as_f64().unwrap_or(0.0);
        let total = m["total_coupling"].as_i64().unwrap_or(0);
        let (sev, advice) = if inst >= 0.9 {
            ("high", "极度不稳定，严重依赖外部模块，修改影响范围大")
        } else if inst >= 0.7 {
            ("medium", "不稳定，依赖较多外部模块")
        } else if total >= 50 {
            ("low", "耦合度较高")
        } else {
            continue;
        };
        let mut item = m.clone();
        item["severity"] = json!(sev);
        item["advice"] = json!(advice);
        high_coupling.push(item);
    }

    let mut issues: Vec<Value> = vec![
        json!({"category": "large_files", "title": "过大文件", "count": large_files.len(), "items": large_files}),
        json!({"category": "complex_functions", "title": "复杂函数", "count": complex_functions.len(), "items": complex_functions}),
        json!({"category": "long_functions", "title": "超长函数", "count": long_functions.len(), "items": long_functions}),
        json!({"category": "high_coupling", "title": "高耦合模块", "count": high_coupling.len(), "items": high_coupling}),
    ];

    // severity 过滤
    if severity != "all" {
        let mut filtered: Vec<Value> = Vec::new();
        for cat in issues {
            let items: Vec<Value> = cat["items"]
                .as_array()
                .cloned()
                .unwrap_or_default()
                .into_iter()
                .filter(|it| it["severity"].as_str() == Some(severity))
                .collect();
            if !items.is_empty() {
                filtered.push(json!({
                    "category": cat["category"], "title": cat["title"],
                    "count": items.len(), "items": items,
                }));
            }
        }
        issues = filtered;
    }

    let mut high_count = 0i64;
    let mut medium_count = 0i64;
    let mut low_count = 0i64;
    for cat in &issues {
        for it in cat["items"].as_array().cloned().unwrap_or_default() {
            match it["severity"].as_str() {
                Some("high") => high_count += 1,
                Some("medium") => medium_count += 1,
                Some("low") => low_count += 1,
                _ => {}
            }
        }
    }
    let health_score = (100.0 - high_count as f64 * 5.0 - medium_count as f64 * 2.0
        - low_count as f64 * 0.5)
        .max(0.0);
    let health_level = if health_score >= 80.0 {
        "良好"
    } else if health_score >= 60.0 {
        "一般"
    } else if health_score >= 40.0 {
        "较差"
    } else {
        "很差"
    };
    let agent_guidance = "AI Agent 修改代码前必读：\n1. 优先修改小文件，大文件修改前先考虑拆分\n2. 复杂函数修改前先理解完整逻辑，或先拆分成小函数再修改\n3. 高耦合模块修改后务必验证所有调用点\n4. 如果一个函数超过 200 行或复杂度 >30，建议先重构再修改";
    Ok(json!({
        "health_score": (health_score * 10.0).round() / 10.0,
        "health_level": health_level,
        "high_issue_count": high_count,
        "medium_issue_count": medium_count,
        "low_issue_count": low_count,
        "total_issue_count": high_count + medium_count + low_count,
        "issues": issues,
        "agent_guidance": agent_guidance,
    }))
}

/// summary_cross_layer_full —— 复刻 db_impact.cross_layer_impact（含 code 层 SQL
/// 与 db/api/config 三层正则；返回完整列表而非计数）。
/// 源符号不存在 → None。
#[allow(clippy::type_complexity)]
fn summary_cross_layer_full(
    conn: &Connection,
    workspace_id: i64,
    symbol_hash: &str,
) -> Result<Option<(Vec<Value>, Vec<Value>, Vec<Value>, Vec<Value>)>, DaemonRpcError> {
    let row = conn
        .query_row(
            "SELECT s.id, s.qualified_name, s.name, sc.content \
             FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             LEFT JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash \
             WHERE fi.workspace_id = ?1 AND s.symbol_hash = ?2 LIMIT 1",
            rusqlite::params![workspace_id, symbol_hash],
            |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, Option<String>>(1)?,
                    r.get::<_, Option<String>>(2)?,
                    r.get::<_, Option<String>>(3)?,
                ))
            },
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("cross_layer source: {e}")))?;
    let (symbol_id, source_qn_o, source_name_o, content_o) = match row {
        Some(v) => v,
        None => return Ok(None),
    };
    let source_qn = source_qn_o.unwrap_or_default();
    let source_name = source_name_o.unwrap_or_default();
    let content = content_o.unwrap_or_default();

    // code 层：反向调用方
    let mut stmt = conn
        .prepare(
            "SELECT DISTINCT s.qualified_name, s.name, s.module_path, s.visibility, s.kind, fi.rel_path \
             FROM calls c \
             JOIN symbols s ON c.caller_id = s.id \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND c.callee_id > 0 AND c.callee_id = ?2",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("cross_layer code prepare: {e}")))?;
    let code_layer: Vec<Value> = stmt
        .query_map(rusqlite::params![workspace_id, symbol_id], |r| {
            Ok(json!({
                "qualified_name": r.get::<_, Option<String>>(0)?,
                "name": r.get::<_, Option<String>>(1)?,
                "module_path": r.get::<_, Option<String>>(2)?,
                "visibility": r.get::<_, Option<String>>(3)?,
                "kind": r.get::<_, Option<String>>(4)?,
                "file_path": r.get::<_, Option<String>>(5)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("cross_layer code: {e}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("cross_layer code collect: {e}")))?;

    // db 层：SQL 表名提取（BTreeSet 排序去重）
    let mut table_names = BTreeSet::new();
    for pat in [
        r"(?i)\bFROM\s+(\w+)",
        r"(?i)\bUPDATE\s+(\w+)",
        r"(?i)\bINSERT\s+INTO\s+(\w+)",
        r"(?i)\bDELETE\s+FROM\s+(\w+)",
    ] {
        if let Ok(re) = regex::Regex::new(pat) {
            for cap in re.captures_iter(&content) {
                if let Some(m) = cap.get(1) {
                    table_names.insert(m.as_str().to_string());
                }
            }
        }
    }
    let db_layer: Vec<Value> = table_names
        .iter()
        .map(|t| json!({"table": t, "source": source_qn}))
        .collect();

    // api 层：函数名关键词 / HTTP 注解 / 路由装饰器
    let name_lower = source_name.to_lowercase();
    let is_api_name = name_lower.contains("route")
        || name_lower.contains("handler")
        || name_lower.contains("endpoint");
    let http_annotation = regex::Regex::new(r"(?i)#\[(?:get|post|put|delete|patch|head|options)\s*\(")
        .ok()
        .map(|re| re.is_match(&content))
        .unwrap_or(false);
    let route_decorator = regex::Regex::new(r"(?i)@\w+\.(?:route|get|post|put|delete|patch)\s*\(")
        .ok()
        .map(|re| re.is_match(&content))
        .unwrap_or(false);
    let mut api_layer: Vec<Value> = Vec::new();
    if is_api_name || http_annotation || route_decorator {
        let mut reasons: Vec<&str> = Vec::new();
        if is_api_name {
            reasons.push("function_name_keyword");
        }
        if http_annotation {
            reasons.push("http_method_annotation");
        }
        if route_decorator {
            reasons.push("route_decorator");
        }
        api_layer.push(json!({
            "symbol": source_qn, "name": source_name,
            "reason": reasons.join(","),
        }));
    }

    // config 层：配置项引用提取
    let mut config_keys = BTreeSet::new();
    for pat in [
        r#"env::var\s*\(\s*['"]([^'"]+)['"]\s*\)"#,
        r#"std::env::var\s*\(\s*['"]([^'"]+)['"]\s*\)"#,
        r#"config\.get\s*\(\s*['"]([^'"]+)['"]\s*\)"#,
    ] {
        if let Ok(re) = regex::Regex::new(pat) {
            for cap in re.captures_iter(&content) {
                if let Some(m) = cap.get(1) {
                    config_keys.insert(m.as_str().to_string());
                }
            }
        }
    }
    let config_layer: Vec<Value> = config_keys
        .iter()
        .map(|k| json!({"config_key": k, "source": source_qn}))
        .collect();

    Ok(Some((code_layer, db_layer, api_layer, config_layer)))
}

/// get_summary —— 复刻 db_summary.get_summary。
pub fn handle_summary_get_summary(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let qualified_name = get_str_param_or(params, "qualified_name", "");
    let row = conn
        .query_row(
            "SELECT ss.summary, ss.model, ss.version, s.qualified_name \
             FROM symbol_summaries ss \
             JOIN symbols s ON ss.symbol_hash = s.symbol_hash \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND s.qualified_name = ?2 AND ss.is_current = 1",
            rusqlite::params![workspace_id, qualified_name],
            |r| {
                Ok((
                    r.get::<_, Option<String>>(0)?,
                    r.get::<_, Option<String>>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, Option<String>>(3)?,
                ))
            },
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("get_summary: {e}")))?;
    match row {
        None => Ok(Value::Null),
        Some((summary, model, version, qn)) => Ok(json!({
            "qualified_name": qn, "summary": summary,
            "model": model, "version": version,
        })),
    }
}

/// project_brief —— 复刻 db_summary.project_brief。
pub fn handle_summary_project_brief(
    conn: &Connection,
    workspace_id: i64,
    _params: &Value,
) -> Result<Value, DaemonRpcError> {
    let metrics = summary_metrics_summary(conn, workspace_id)?;
    let health = summary_health_check(conn, workspace_id, "high")?;

    // 项目类型：按扩展名分布
    let mut stmt = conn
        .prepare(
            "SELECT rel_path FROM file_instances \
             WHERE workspace_id = ?1 AND status != 'archived' AND rel_path LIKE '%.%'",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("brief ext prepare: {e}")))?;
    let rel_paths: Vec<String> = stmt
        .query_map([workspace_id], |r| r.get::<_, String>(0))
        .map_err(|e| DaemonRpcError::internal_error(format!("brief ext: {e}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("brief ext collect: {e}")))?;
    let mut ext_counts: BTreeMap<String, i64> = BTreeMap::new();
    for rp in &rel_paths {
        let ext = rp.rsplit('.').next().unwrap_or("").to_lowercase();
        *ext_counts.entry(ext).or_insert(0) += 1;
    }
    let project_type = if !ext_counts.is_empty() {
        let (top_ext, _) = ext_counts
            .iter()
            .max_by(|a, b| a.1.cmp(b.1).then_with(|| b.0.cmp(a.0)))
            .map(|(k, v)| (k.clone(), *v))
            .unwrap_or_default();
        // Python max(ext_counts.items(), key=lambda x: x[1]) 取首个最大（插入序）；
        // BTreeMap 已失序，同频平局取字典序最小近似（实测主扩展名频次唯一）。
        let _ = &top_ext;
        match top_ext.as_str() {
            "rs" => "Rust".to_string(),
            "py" => "Python".to_string(),
            "ts" | "tsx" => "TypeScript".to_string(),
            "js" | "jsx" => "JavaScript".to_string(),
            "go" => "Go".to_string(),
            "java" => "Java".to_string(),
            "c" => "C".to_string(),
            "cpp" => "C++".to_string(),
            "h" => "C/C++".to_string(),
            other => return_section_other(other),
        }
    } else {
        "Unknown".to_string()
    };

    // 模块列表（函数数降序，前 20）
    let mut stmt = conn
        .prepare(
            "SELECT s.module_path, COUNT(*) as fn_count \
             FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND s.module_path != '' \
               AND s.kind IN ('fn','function','method') \
             GROUP BY s.module_path ORDER BY fn_count DESC LIMIT 20",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("brief modules prepare: {e}")))?;
    let modules: Vec<Value> = stmt
        .query_map([workspace_id], |r| {
            Ok(json!({
                "module": r.get::<_, String>(0)?,
                "function_count": r.get::<_, i64>(1)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("brief modules: {e}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("brief modules collect: {e}")))?;

    // 热点函数（前 10）
    let hotspots = summary_complexity_hotspots(conn, workspace_id, 10, "")?;

    // 文件数：优先 metrics，回退 get_status().files.tracked（= current_files）
    let mut file_count = metrics["file_count"].as_i64().unwrap_or(0);
    if file_count == 0 {
        file_count = conn
            .query_row(
                "SELECT COALESCE(SUM(CASE WHEN fv.is_current = 1 THEN 1 ELSE 0 END), 0) \
                 FROM file_versions fv \
                 JOIN file_instances fi ON fv.file_instance_id = fi.id \
                 WHERE fi.workspace_id = ?1 AND fi.status != 'archived'",
                [workspace_id],
                |r| r.get::<_, i64>(0),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("brief status fallback: {e}")))?
            .unwrap_or(0);
    }

    Ok(json!({
        "project_type": project_type,
        "file_count": file_count,
        "function_count": metrics["function_count"],
        "total_lines": metrics["total_lines"],
        "modules": modules,
        "hot_functions": hotspots,
        "health_score": health["health_score"],
        "health_level": health["health_level"],
        "avg_complexity": metrics["avg_complexity"],
        "comment_coverage": metrics["comment_coverage"],
    }))
}

/// project_type 未映射扩展名的回退（拆出避免 match 臂类型问题）。
fn return_section_other(ext: &str) -> String {
    format!("Multi-language (.{ext})")
}

/// repo_map —— 复刻 db_summary.repo_map（模块间跨模块调用依赖图）。
pub fn handle_summary_repo_map(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let format = get_str_param_or(params, "format", "text");
    let mut stmt = conn
        .prepare(
            "SELECT s_caller.module_path, c.callee_module, COUNT(*) as cnt \
             FROM calls c \
             JOIN symbols s_caller ON c.caller_id = s_caller.id \
             JOIN file_instances fi ON s_caller.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 \
               AND s_caller.module_path != '' AND c.callee_module != '' \
               AND s_caller.module_path != c.callee_module \
             GROUP BY s_caller.module_path, c.callee_module ORDER BY cnt DESC",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("repo_map prepare: {e}")))?;
    let edges: Vec<(String, String, i64)> = stmt
        .query_map([workspace_id], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, i64>(2)?))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("repo_map: {e}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("repo_map collect: {e}")))?;
    let out = if format == "mermaid" {
        let mut lines = vec!["graph TD".to_string()];
        for (caller, callee, cnt) in &edges {
            lines.push(format!(
                "    {} -->|{}| {}",
                caller.replace('.', "_"),
                cnt,
                callee.replace('.', "_")
            ));
        }
        lines.join("\n")
    } else {
        let mut lines = vec!["仓库模块依赖图:".to_string(), String::new()];
        for (caller, callee, cnt) in &edges {
            lines.push(format!("  {} → {} ({} 次调用)", caller, callee, cnt));
        }
        lines.join("\n")
    };
    Ok(json!(out))
}

/// test_impact_selection —— 复刻 db_coverage.test_impact_selection
///（反向调用图 BFS + 测试函数筛选）。
pub fn handle_summary_test_impact_selection(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let qualified_name = get_str_param_or(params, "qualified_name", "");
    let target: Option<i64> = conn
        .query_row(
            "SELECT s.id FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND s.qualified_name = ?2 LIMIT 1",
            rusqlite::params![workspace_id, qualified_name],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("tis target: {e}")))?;
    let target_id = match target {
        Some(id) => id,
        None => return Ok(json!([])),
    };

    let mut visited: std::collections::HashSet<i64> = std::collections::HashSet::new();
    let mut queue: Vec<i64> = vec![target_id];
    // all_callers：Python dict 插入序 → Vec<(key, Value)> + HashSet 键查重
    let mut all_callers: Vec<Value> = Vec::new();
    let mut seen_qn: std::collections::HashSet<String> = std::collections::HashSet::new();

    while !queue.is_empty() {
        let current_batch: Vec<i64> = queue
            .iter()
            .copied()
            .filter(|id| !visited.contains(id))
            .collect();
        if current_batch.is_empty() {
            break;
        }
        let placeholders: Vec<&str> = current_batch.iter().map(|_| "?").collect();
        let sql = format!(
            "SELECT DISTINCT s.id, s.qualified_name, s.name, s.module_path, \
             s.start_line, fi.rel_path \
             FROM calls c \
             JOIN symbols s ON c.caller_id = s.id \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ? AND c.callee_id > 0 AND c.callee_id IN ({})",
            placeholders.join(",")
        );
        let mut stmt2 = conn
            .prepare(&sql)
            .map_err(|e| DaemonRpcError::internal_error(format!("tis bfs prepare: {e}")))?;
        let iter = stmt2.query_map(rusqlite::params_from_iter(current_batch.iter()), |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, Option<String>>(1)?,
                r.get::<_, Option<String>>(2)?,
                r.get::<_, Option<String>>(3)?,
                r.get::<_, Option<i64>>(4)?,
                r.get::<_, Option<String>>(5)?,
            ))
        });
        let iter = match iter {
            Ok(it) => it,
            Err(e) => {
                return Err(DaemonRpcError::internal_error(format!("tis bfs: {e}")));
            }
        };
        let mut next_queue: Vec<i64> = Vec::new();
        for row in iter {
            let (id, qn_o, name_o, module_o, start_line, rel_path) = row
                .map_err(|e| DaemonRpcError::internal_error(format!("tis bfs row: {e}")))?;
            let caller_qn = qn_o.clone().unwrap_or_default();
            if !caller_qn.is_empty() && !visited.contains(&id) && seen_qn.insert(caller_qn.clone()) {
                all_callers.push(json!({
                    "qualified_name": caller_qn,
                    "name": name_o,
                    "module_path": module_o,
                    "start_line": start_line,
                    "file_path": rel_path,
                }));
                next_queue.push(id);
            }
        }
        for id in current_batch {
            visited.insert(id);
        }
        queue = next_queue;
    }

    // 测试函数筛选：名称/限定名含 test|spec，或 module_path 含 test（小写）
    let is_test = |c: &Value| -> bool {
        let name = c["name"].as_str().unwrap_or("").to_lowercase();
        let qn = c["qualified_name"].as_str().unwrap_or("").to_lowercase();
        let module = c["module_path"].as_str().unwrap_or("").to_lowercase();
        name.contains("test")
            || name.contains("spec")
            || qn.contains("test")
            || qn.contains("spec")
            || module.contains("test")
    };
    Ok(json!(all_callers
        .into_iter()
        .filter(|c| is_test(c))
        .collect::<Vec<_>>()))
}

/// who_to_ask —— 复刻 db_ownership.who_to_ask（CODEOWNERS + git blame 归属）。
pub fn handle_summary_who_to_ask(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let file_path = get_str_param_or(params, "file_path", "");
    let is_abs = std::path::Path::new(file_path.as_str()).is_absolute();
    let (match_col, match_val) = if is_abs {
        ("fi.abs_path", file_path.replace('\\', "/"))
    } else {
        ("fi.rel_path", file_path.replace('\\', "/"))
    };
    let sql = format!(
        "SELECT fo.owner, fo.source, fo.confidence, \
         fo.last_commit_hash, fo.last_commit_author, fo.last_commit_time, fi.rel_path \
         FROM file_ownership fo \
         JOIN file_instances fi ON fo.file_instance_id = fi.id \
         WHERE fi.workspace_id = ?1 AND {match_col} = ?2 LIMIT 1"
    );
    let row = conn
        .query_row(
            &sql,
            rusqlite::params![workspace_id, match_val],
            |r| {
                Ok((
                    r.get::<_, Option<String>>(0)?,
                    r.get::<_, Option<String>>(1)?,
                    r.get::<_, Option<f64>>(2)?,
                    r.get::<_, Option<String>>(3)?,
                    r.get::<_, Option<String>>(4)?,
                    r.get::<_, Option<f64>>(5)?,
                    r.get::<_, Option<String>>(6)?,
                ))
            },
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("who_to_ask: {e}")))?;
    match row {
        None => Ok(Value::Null),
        Some((owner, source, confidence, last_hash, last_author, last_time, rel_path)) => {
            Ok(json!({
                "file_path": rel_path,
                "owner": owner,
                "source": source,
                "confidence": confidence,
                "last_commit_author": last_author.unwrap_or_default(),
                "last_commit_time": last_time,
                "last_commit_hash": last_hash.unwrap_or_default(),
            }))
        }
    }
}

/// get_ownership_map —— 复刻 db_ownership.get_ownership_map（模块负责人分布）。
pub fn handle_summary_ownership_map(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let module_filter = get_str_param_or(params, "module_filter", "");
    let sql = if module_filter.is_empty() {
        "SELECT fi.module_path, fo.owner FROM file_ownership fo \
         JOIN file_instances fi ON fo.file_instance_id = fi.id \
         WHERE fi.workspace_id = ?1"
            .to_string()
    } else {
        format!(
            "{} AND fi.module_path LIKE ?2",
            "SELECT fi.module_path, fo.owner FROM file_ownership fo \
             JOIN file_instances fi ON fo.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1"
        )
    };
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("ownership prepare: {e}")))?;
    let rows: Vec<(Option<String>, Option<String>)> = if module_filter.is_empty() {
        stmt.query_map([workspace_id], |r| {
            Ok((r.get::<_, Option<String>>(0)?, r.get::<_, Option<String>>(1)?))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("ownership: {e}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("ownership collect: {e}")))?
    } else {
        let like = format!("{module_filter}%");
        stmt.query_map(rusqlite::params![workspace_id, like], |r| {
            Ok((r.get::<_, Option<String>>(0)?, r.get::<_, Option<String>>(1)?))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("ownership: {e}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("ownership collect: {e}")))?
    };
    // module -> (owner -> count)，Python defaultdict 嵌套插入序 → Vec 保序
    let mut module_order: Vec<String> = Vec::new();
    let mut module_owners: HashMap<String, Vec<(String, i64)>> = HashMap::new();
    let mut module_total: HashMap<String, i64> = HashMap::new();
    for (module_path, owner) in rows {
        let module = module_path
            .filter(|m| !m.is_empty())
            .unwrap_or_else(|| "(未分类)".to_string());
        let owner = owner
            .filter(|o| !o.is_empty())
            .unwrap_or_else(|| "(未知)".to_string());
        if !module_order.contains(&module) {
            module_order.push(module.clone());
        }
        let entry = module_owners.entry(module.clone()).or_default();
        match entry.iter_mut().find(|(n, _)| *n == owner) {
            Some((_, cnt)) => *cnt += 1,
            None => entry.push((owner, 1)),
        }
        *module_total.entry(module).or_insert(0) += 1;
    }
    let mut results: Vec<(i64, Value)> = module_order
        .iter()
        .map(|module| {
            let owners = module_owners.get(module).cloned().unwrap_or_default();
            // 主负责人：文件数最多（Python sorted 降序稳定 → 平局保插入序）
            let mut sorted_owners = owners.clone();
            sorted_owners.sort_by(|a, b| b.1.cmp(&a.1));
            let primary = sorted_owners
                .first()
                .map(|(n, _)| n.clone())
                .unwrap_or_else(|| "(未知)".to_string());
            let owners_list: Vec<Value> = sorted_owners
                .into_iter()
                .map(|(name, cnt)| json!({"name": name, "file_count": cnt}))
                .collect();
            let total = module_total.get(module).copied().unwrap_or(0);
            let item = json!({
                "module": module,
                "primary_owner": primary,
                "file_count": total,
                "owners": owners_list,
            });
            (total, item)
        })
        .collect();
    results.sort_by(|a, b| b.0.cmp(&a.0));
    Ok(json!(results.into_iter().map(|(_, v)| v).collect::<Vec<_>>()))
}

// ---- guardrail 组 ----
// 基线（2026-09-10 mode=ro 探针实证）：scan_guardrails / guardrail_list_rules
// 首行 _init_builtin_rules 走 INSERT OR IGNORE → OperationalError
// "attempt to write a readonly database"，MCP 面恒 error。Rust 原生侧按
// fail-closed 同语义返回内部错误（写面拒绝落只读快照连接，不实际写库）。

/// guardrail_scan（写面 fail-closed，对齐 Python worker readonly error）。
pub fn handle_summary_guardrail_scan(
    _conn: &Connection,
    _workspace_id: i64,
    _params: &Value,
) -> Result<Value, DaemonRpcError> {
    Err(DaemonRpcError::internal_error(
        "guardrail_scan is write-face (_init_builtin_rules INSERT); \
         read-only snapshot connection rejects it \
         (parity: python worker OperationalError attempt to write a readonly database)"
            .to_string(),
    ))
}

/// guardrail_list_rules（写面 fail-closed，同上）。
pub fn handle_summary_guardrail_list_rules(
    _conn: &Connection,
    _workspace_id: i64,
    _params: &Value,
) -> Result<Value, DaemonRpcError> {
    Err(DaemonRpcError::internal_error(
        "guardrail_list_rules is write-face (_init_builtin_rules INSERT); \
         read-only snapshot connection rejects it \
         (parity: python worker OperationalError attempt to write a readonly database)"
            .to_string(),
    ))
}

/// summary_read_file_normalized —— 复刻 config.read_file_normalized
///（BOM 检测 + UTF-8 优先 + norm_newlines）。失败返回 None。
fn summary_read_file_normalized(path: &str) -> Option<String> {
    let raw = std::fs::read(path).ok()?;
    let text = if raw.starts_with(&[0xEF, 0xBB, 0xBF]) {
        String::from_utf8_lossy(&raw[3..]).into_owned()
    } else if raw.starts_with(&[0xFF, 0xFE]) {
        let utf16: Vec<u16> = raw[2..]
            .chunks_exact(2)
            .map(|c| u16::from_le_bytes([c[0], c[1]]))
            .collect();
        String::from_utf16_lossy(&utf16)
    } else if raw.starts_with(&[0xFE, 0xFF]) {
        let utf16: Vec<u16> = raw[2..]
            .chunks_exact(2)
            .map(|c| u16::from_be_bytes([c[0], c[1]]))
            .collect();
        String::from_utf16_lossy(&utf16)
    } else {
        String::from_utf8_lossy(&raw).into_owned()
    };
    Some(text.replace("\r\n", "\n").replace('\r', "\n"))
}

/// summary_extract_function_blocks —— 复刻 db_guardrail._extract_function_blocks
///（花括号语言 fn/func/function + Python def 缩进启发式）。
fn summary_extract_function_blocks(content: &str) -> Vec<(String, String, i64, i64)> {
    let mut blocks: Vec<(String, String, i64, i64)> = Vec::new();
    if let Ok(brace_re) =
        regex::Regex::new(r"\b(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?(?:fn|func|function)\s+(\w+)")
    {
        for cap in brace_re.captures_iter(content) {
            let m = match cap.get(0) {
                Some(m) => m,
                None => continue,
            };
            let name = cap.get(1).map(|g| g.as_str().to_string()).unwrap_or_default();
            let start_line = content[..m.start()].matches('\n').count() as i64 + 1;
            let brace_start = match content[m.end()..].find('{') {
                Some(off) => m.end() + off,
                None => continue,
            };
            let bytes = content.as_bytes();
            let mut depth: i64 = 1;
            let mut i = brace_start + 1;
            while i < bytes.len() && depth > 0 {
                match bytes[i] {
                    b'{' => depth += 1,
                    b'}' => depth -= 1,
                    _ => {}
                }
                i += 1;
            }
            if depth == 0 {
                let body = content[brace_start + 1..i - 1].to_string();
                let end_line = content[..i].matches('\n').count() as i64 + 1;
                blocks.push((name, body, start_line, end_line));
            }
        }
    }
    if let Ok(def_re) = regex::Regex::new(r"\bdef\s+(\w+)\s*\(") {
        for cap in def_re.captures_iter(content) {
            let m = match cap.get(0) {
                Some(m) => m,
                None => continue,
            };
            let name = cap.get(1).map(|g| g.as_str().to_string()).unwrap_or_default();
            let start_line = content[..m.start()].matches('\n').count() as i64 + 1;
            let colon_pos = match content[m.end()..].find(':') {
                Some(off) => m.end() + off,
                None => continue,
            };
            let body_start = match content[colon_pos..].find('\n') {
                Some(off) => colon_pos + off + 1,
                None => continue,
            };
            let rest = &content[body_start..];
            let body_end = regex::Regex::new(r"\n(?:def |class )")
                .ok()
                .and_then(|re| re.find(rest))
                .map(|mm| body_start + mm.start())
                .unwrap_or(content.len());
            let body = content[body_start..body_end].to_string();
            let end_line = content[..body_start + body.len()].matches('\n').count() as i64 + 1;
            blocks.push((name, body, start_line, end_line));
        }
    }
    blocks
}

/// summary_detect_db_safety —— 复刻 db_guardrail._detect_db_safety。
fn summary_detect_db_safety(content: &str, file_path: &str) -> Vec<Value> {
    let mut findings: Vec<Value> = Vec::new();
    if let Ok(re) = regex::Regex::new(r"(?i)\bALTER\s+TABLE\b") {
        for m in re.find_iter(content) {
            let line = content[..m.start()].matches('\n').count() as i64 + 1;
            findings.push(json!({
                "rule_id": "GR-builtin-db-1",
                "severity": "warn",
                "message": format!("检测到 ALTER TABLE 语句（第 {line} 行）"),
                "symbol_hash": "",
            }));
        }
    }
    if let Ok(re) = regex::Regex::new(r"(?i)\bDROP\s+(TABLE|COLUMN)\b") {
        for m in re.find_iter(content) {
            let line = content[..m.start()].matches('\n').count() as i64 + 1;
            let statement = m.as_str().to_uppercase();
            findings.push(json!({
                "rule_id": "GR-builtin-db-2",
                "severity": "block",
                "message": format!("检测到 {statement} 语句（第 {line} 行）"),
                "symbol_hash": "",
            }));
        }
    }
    if let Ok(re) = regex::Regex::new(
        r"(?i)VARCHAR\s*\(\s*(\d+)\s*\)\s*(?:→|->)\s*VARCHAR\s*\(\s*(\d+)\s*\)",
    ) {
        for cap in re.captures_iter(content) {
            let m = match cap.get(0) {
                Some(m) => m,
                None => continue,
            };
            let old_len: i64 = cap.get(1).and_then(|g| g.as_str().parse().ok()).unwrap_or(0);
            let new_len: i64 = cap.get(2).and_then(|g| g.as_str().parse().ok()).unwrap_or(0);
            if new_len < old_len {
                let line = content[..m.start()].matches('\n').count() as i64 + 1;
                findings.push(json!({
                    "rule_id": "GR-builtin-db-3",
                    "severity": "block",
                    "message": format!("VARCHAR 长度缩减：{old_len} → {new_len}（第 {line} 行）"),
                    "symbol_hash": "",
                }));
            }
        }
    }
    if file_path.to_lowercase().ends_with(".sql") && !file_path.contains("migrations/") {
        findings.push(json!({
            "rule_id": "GR-builtin-db-1",
            "severity": "warn",
            "message": "SQL 文件不在 migrations/ 目录下（迁移脚本缺失风险）",
            "symbol_hash": "",
        }));
    }
    findings
}

/// summary_detect_api_compat —— 复刻 db_guardrail._detect_api_compat。
fn summary_detect_api_compat(content: &str, _file_path: &str) -> Vec<Value> {
    let mut findings: Vec<Value> = Vec::new();
    if let Ok(re) = regex::Regex::new(r"(?i)#\s*BREAKING\s+CHANGE") {
        for m in re.find_iter(content) {
            let line = content[..m.start()].matches('\n').count() as i64 + 1;
            let line_end = content[m.start()..]
                .find('\n')
                .map(|off| m.start() + off)
                .unwrap_or(content.len());
            let context = content[m.start()..line_end].trim().chars().take(80).collect::<String>();
            findings.push(json!({
                "rule_id": "GR-builtin-api-1",
                "severity": "block",
                "message": format!("检测到 BREAKING CHANGE 标记：{context}（第 {line} 行）"),
                "symbol_hash": "",
            }));
        }
    }
    if let Ok(re) = regex::Regex::new(r"(?i)//\s*REMOVED\s+PARAM") {
        for m in re.find_iter(content) {
            let line = content[..m.start()].matches('\n').count() as i64 + 1;
            findings.push(json!({
                "rule_id": "GR-builtin-api-2",
                "severity": "block",
                "message": format!("检测到参数删除标记 // REMOVED PARAM（第 {line} 行）"),
                "symbol_hash": "",
            }));
        }
    }
    if let Ok(re) = regex::Regex::new(r"(?i)//\s*REMOVED\s+FIELD") {
        for m in re.find_iter(content) {
            let line = content[..m.start()].matches('\n').count() as i64 + 1;
            findings.push(json!({
                "rule_id": "GR-builtin-api-3",
                "severity": "block",
                "message": format!("检测到字段删除标记 // REMOVED FIELD（第 {line} 行）"),
                "symbol_hash": "",
            }));
        }
    }
    findings
}

/// summary_detect_incident_readiness —— 复刻 db_guardrail._detect_incident_readiness。
fn summary_detect_incident_readiness(content: &str, _file_path: &str) -> Vec<Value> {
    let mut findings: Vec<Value> = Vec::new();
    let mut blocks = summary_extract_function_blocks(content);
    if blocks.is_empty() {
        let total_lines = content.matches('\n').count() as i64 + 1;
        blocks.push(("<file>".to_string(), content.to_string(), 1, total_lines));
    }
    let err_re = regex::Regex::new(r"\b(?:try|catch|unwrap|expect)\b|\?|Result").ok();
    let log_re = regex::Regex::new(
        r"\blog::|tracing::|println!|print!|eprintln!|warn!|info!|error!|debug!",
    )
    .ok();
    let write_re1 = regex::Regex::new(r"(?i)\b(?:INSERT|UPDATE|DELETE|CREATE|DROP)\b").ok();
    let write_re2 = regex::Regex::new(r"\.(?:write|save|push|insert|update|delete)\s*\(").ok();
    let safety_re =
        regex::Regex::new(r"(?i)\b(?:rollback|transaction|begin|undo|abort|commit)\b").ok();
    for (name, body, start_line, end_line) in blocks {
        if body.trim().chars().count() < 10 {
            continue;
        }
        let location = format!("函数 {name}（第 {start_line}-{end_line} 行）");
        let has_err = err_re.as_ref().map(|re| re.is_match(&body)).unwrap_or(false);
        if !has_err {
            findings.push(json!({
                "rule_id": "GR-builtin-inc-1",
                "severity": "warn",
                "message": format!("{location}缺少错误处理（无 try/catch/unwrap/expect/?/Result）"),
                "symbol_hash": "",
            }));
        }
        let has_log = log_re.as_ref().map(|re| re.is_match(&body)).unwrap_or(false);
        if !has_log {
            findings.push(json!({
                "rule_id": "GR-builtin-inc-2",
                "severity": "info",
                "message": format!("{location}缺少日志（无 log::/tracing::/println!/print!）"),
                "symbol_hash": "",
            }));
        }
        let has_write = write_re1
            .as_ref()
            .map(|re| re.is_match(&body))
            .unwrap_or(false)
            || write_re2
                .as_ref()
                .map(|re| re.is_match(&body))
                .unwrap_or(false);
        let has_safety = safety_re
            .as_ref()
            .map(|re| re.is_match(&body))
            .unwrap_or(false);
        if has_write && !has_safety {
            findings.push(json!({
                "rule_id": "GR-builtin-inc-3",
                "severity": "warn",
                "message": format!("{location}有写操作但无事务/回滚逻辑"),
                "symbol_hash": "",
            }));
        }
    }
    findings
}

/// guardrail_check_edit —— 复刻 db_guardrail.check_before_edit（只读，无持久化）。
pub fn handle_summary_guardrail_check_edit(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let file_path = get_str_param_or(params, "file_path", "")
        .replace('\\', "/")
        .trim()
        .to_string();
    let proposed = get_str_param_or(params, "proposed_change", "");
    let content: Option<String> = if !proposed.is_empty() {
        Some(proposed)
    } else {
        // 策略1：file_instances rel_path → abs_path
        let abs: Option<String> = conn
            .query_row(
                "SELECT abs_path FROM file_instances \
                 WHERE workspace_id = ?1 AND rel_path = ?2 AND status != 'archived' LIMIT 1",
                rusqlite::params![workspace_id, file_path],
                |r| r.get::<_, Option<String>>(0),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("check_edit lookup: {e}")))?
            .flatten();
        let mut found = abs.as_deref().and_then(summary_read_file_normalized);
        // 策略2：作为绝对路径直接读取
        if found.is_none() {
            found = summary_read_file_normalized(&file_path);
        }
        // 策略3：workspace_root 拼接
        if found.is_none() {
            if let Some(root) = security_workspace_root(conn, workspace_id) {
                let joined = format!("{}/{}", root.trim_end_matches('/'), file_path);
                found = summary_read_file_normalized(&joined);
            }
        }
        found
    };
    let content = match content {
        Some(c) => c,
        None => {
            return Ok(json!({
                "decision": "pass", "findings": [],
                "message": "文件不存在或无法读取，跳过检查",
            }));
        }
    };
    let mut findings: Vec<Value> = Vec::new();
    findings.extend(summary_detect_db_safety(&content, &file_path));
    findings.extend(summary_detect_api_compat(&content, &file_path));
    findings.extend(summary_detect_incident_readiness(&content, &file_path));
    for f in findings.iter_mut() {
        f["file_path"] = json!(file_path);
    }
    let block_count = findings
        .iter()
        .filter(|f| f["severity"] == "block")
        .count();
    let warn_count = findings
        .iter()
        .filter(|f| f["severity"] == "warn")
        .count();
    let (decision, message) = if block_count > 0 {
        ("block", format!("检测到 {block_count} 个阻断级问题，禁止编辑"))
    } else if warn_count > 0 {
        ("warn", format!("检测到 {warn_count} 个警告级问题，建议审查后编辑"))
    } else {
        ("pass", "未检测到安全问题，可以编辑".to_string())
    };
    Ok(json!({"decision": decision, "findings": findings, "message": message}))
}

/// blast_radius —— 复刻 db_impact.blast_radius（Python 全路径 SQL BFS；
/// 复用 security_blast_radius_sql + summary_cross_layer_full 计数）。
pub fn handle_summary_blast_radius(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let symbol_hash = get_str_param_or(params, "symbol_hash", "");
    let depth = get_int_param_or(params, "depth", 3);
    match security_blast_radius_sql(conn, workspace_id, &symbol_hash, depth) {
        Some((source_qn, layers, total_impacted, by_layer)) => Ok(json!({
            "source_symbol": source_qn,
            "source_hash": symbol_hash,
            "depth": depth,
            "layers": layers,
            "total_impacted": total_impacted,
            "by_layer": by_layer,
        })),
        None => Ok(json!({
            "source_symbol": "",
            "source_hash": symbol_hash,
            "depth": depth,
            "layers": [],
            "total_impacted": 0,
            "by_layer": {"code": 0, "db": 0, "api": 0, "config": 0},
        })),
    }
}

/// cross_layer_impact —— 复刻 db_impact.cross_layer_impact（完整四层列表）。
pub fn handle_summary_cross_layer_impact(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let symbol_hash = get_str_param_or(params, "symbol_hash", "");
    match summary_cross_layer_full(conn, workspace_id, &symbol_hash)? {
        None => Ok(json!({"code": [], "db": [], "api": [], "config": []})),
        Some((code, db, api, config)) => {
            Ok(json!({"code": code, "db": db, "api": api, "config": config}))
        }
    }
}

// ---- ask_codebase / token_savings / vuln / clone / review_readiness ----

/// summary_keyword_fallback_search —— 复刻 db_vector._keyword_fallback_search。
fn summary_keyword_fallback_search(
    conn: &Connection,
    workspace_id: i64,
    query: &str,
    top_k: i64,
) -> Result<Vec<Value>, DaemonRpcError> {
    let normalized = query.replace(',', " ").replace('.', " ");
    let keywords: Vec<&str> = normalized
        .split_whitespace()
        .filter(|w| w.chars().count() > 2)
        .take(5)
        .collect();
    if keywords.is_empty() {
        return Ok(Vec::new());
    }
    let mut results: Vec<Value> = Vec::new();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    for kw in keywords {
        let like = format!("%{kw}%");
        let mut stmt = conn
            .prepare(
                "SELECT s.symbol_hash, s.qualified_name, s.start_line, s.end_line, fi.rel_path, \
                 (SELECT ss.summary FROM symbol_summaries ss \
                  WHERE ss.symbol_hash = s.symbol_hash AND ss.is_current = 1 \
                  ORDER BY ss.version DESC LIMIT 1) as summary \
                 FROM symbols s \
                 JOIN file_instances fi ON s.file_instance_id = fi.id \
                 WHERE fi.workspace_id = ?1 AND s.kind = 'fn' \
                   AND (s.qualified_name LIKE ?2 OR s.name LIKE ?2) LIMIT ?3",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("kw fallback prepare: {e}")))?;
        let rows = stmt
            .query_map(
                rusqlite::params![workspace_id, like, top_k.max(0)],
                |r| {
                    Ok((
                        r.get::<_, Option<String>>(0)?,
                        r.get::<_, Option<String>>(1)?,
                        r.get::<_, Option<i64>>(2)?,
                        r.get::<_, Option<i64>>(3)?,
                        r.get::<_, Option<String>>(4)?,
                        r.get::<_, Option<String>>(5)?,
                    ))
                },
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("kw fallback: {e}")))?;
        for row in rows {
            let (hash, qn, start_line, _end_line, rel_path, summary) = row
                .map_err(|e| DaemonRpcError::internal_error(format!("kw fallback row: {e}")))?;
            let hash_s = hash.unwrap_or_default();
            if hash_s.is_empty() || !seen.insert(hash_s) {
                continue;
            }
            results.push(json!({
                "qualified_name": qn,
                "file_path": rel_path,
                "start_line": start_line,
                "similarity": 0.5,
                "summary": summary.unwrap_or_default(),
            }));
            if results.len() as i64 >= top_k {
                return Ok(results);
            }
        }
    }
    Ok(results)
}

/// summary_lookup_symbol_hash —— 复刻 db_vector._lookup_symbol_hash_by_qualified_name。
fn summary_lookup_symbol_hash(
    conn: &Connection,
    workspace_id: i64,
    qualified_name: &str,
) -> Option<String> {
    conn.query_row(
        "SELECT s.symbol_hash FROM symbols s \
         JOIN file_instances fi ON s.file_instance_id = fi.id \
         WHERE fi.workspace_id = ?1 AND fi.status != 'archived' AND s.qualified_name = ?2 \
         LIMIT 1",
        rusqlite::params![workspace_id, qualified_name],
        |r| r.get::<_, Option<String>>(0),
    )
    .optional()
    .ok()
    .flatten()
    .flatten()
}

/// summary_build_rag_block —— 复刻 db_vector._build_rag_block。
fn summary_build_rag_block(
    conn: &Connection,
    workspace_id: i64,
    symbol_hash: &str,
    meta: &Value,
    role: &str,
) -> Result<Option<Value>, DaemonRpcError> {
    let row = conn
        .query_row(
            "SELECT sc.content, sc.has_comment, s.start_line, s.end_line \
             FROM symbols s \
             JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND s.symbol_hash = ?2 LIMIT 1",
            rusqlite::params![workspace_id, symbol_hash],
            |r| {
                Ok((
                    r.get::<_, Option<String>>(0)?,
                    r.get::<_, Option<i64>>(1)?,
                    r.get::<_, Option<i64>>(2)?,
                    r.get::<_, Option<i64>>(3)?,
                ))
            },
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("rag block: {e}")))?;
    let (content, has_comment, row_start, _row_end) = match row {
        Some(v) => v,
        None => return Ok(None),
    };
    let start_line = meta["start_line"].as_i64().filter(|v| *v != 0).or(row_start).unwrap_or(0);
    Ok(Some(json!({
        "role": role,
        "qualified_name": meta["qualified_name"],
        "file_path": meta["file_path"],
        "start_line": start_line,
        "similarity": meta["similarity"],
        "summary": meta["summary"],
        "code": content.unwrap_or_default(),
        "has_comment": has_comment.unwrap_or(0) != 0,
    })))
}

/// summary_get_callees —— 复刻 db_vector._get_callees_for_symbol。
fn summary_get_callees(
    conn: &Connection,
    workspace_id: i64,
    symbol_hash: &str,
    limit: i64,
) -> Result<Vec<Value>, DaemonRpcError> {
    let mut stmt = conn
        .prepare(
            "SELECT s.symbol_hash, s.qualified_name, fi.rel_path \
             FROM calls c \
             JOIN symbols s ON c.callee_id = s.id \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 \
               AND c.caller_id = (SELECT id FROM symbols WHERE symbol_hash = ?2 LIMIT 1) \
             LIMIT ?3",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("callees prepare: {e}")))?;
    let rows = stmt
        .query_map(rusqlite::params![workspace_id, symbol_hash, limit.max(0)], |r| {
            Ok((
                r.get::<_, Option<String>>(0)?,
                r.get::<_, Option<String>>(1)?,
                r.get::<_, Option<String>>(2)?,
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("callees: {e}")))?;
    let mut out = Vec::new();
    for row in rows {
        let (hash, qn, rel_path) = row
            .map_err(|e| DaemonRpcError::internal_error(format!("callees row: {e}")))?;
        out.push(json!({
            "symbol_hash": hash.unwrap_or_default(),
            "qualified_name": qn.unwrap_or_default(),
            "rel_path": rel_path.unwrap_or_default(),
        }));
    }
    Ok(out)
}

/// ask_codebase —— 复刻 db_vector.ask_codebase。
/// 部署机 embedder 不可用（2026-09-10 live 实证）→ 恒走 keyword_fallback 路径，
/// metadata.has_vector_index=false（与 Python 基线一致）。
pub fn handle_summary_ask_codebase(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let question = get_str_param_or(params, "question", "");
    let top_k = get_int_param_or(params, "top_k", 5);
    let include_callers = get_int_param_or(params, "include_callers", 2);
    let include_callees = get_int_param_or(params, "include_callees", 1);
    let max_tokens = get_int_param_or(params, "max_tokens", 4000);

    let fallback_used = "keyword_fallback";
    let seeds = summary_keyword_fallback_search(conn, workspace_id, &question, top_k)?;
    if seeds.is_empty() {
        return Ok(json!({
            "question": question,
            "seed_functions": [],
            "context_blocks": [],
            "rag_context": "",
            "estimated_tokens": 0,
            "truncated": false,
            "metadata": {
                "total_functions_included": 0,
                "has_vector_index": false,
                "fallback_used": "no_results",
            },
        }));
    }

    let mut context_blocks: Vec<Value> = Vec::new();
    let mut included: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut total_chars: usize = 0;
    let max_chars = (max_tokens.max(0) as usize).saturating_mul(4);
    let mut truncated = false;

    'seeds: for seed in &seeds {
        if total_chars > max_chars {
            truncated = true;
            break;
        }
        let qn = seed["qualified_name"].as_str().unwrap_or("");
        let symbol_hash = match summary_lookup_symbol_hash(conn, workspace_id, qn) {
            Some(h) => h,
            None => continue,
        };
        if !included.insert(symbol_hash.clone()) {
            continue;
        }
        if let Some(block) =
            summary_build_rag_block(conn, workspace_id, &symbol_hash, seed, "seed")?
        {
            total_chars += block["code"].as_str().map(|s| s.chars().count()).unwrap_or(0)
                + block["summary"].as_str().map(|s| s.chars().count()).unwrap_or(0);
            context_blocks.push(block);
        }
        // 调用方上下文（blast_radius depth=1）
        if include_callers > 0 {
            if let Some((_src, layers, _total, _by)) =
                security_blast_radius_sql(conn, workspace_id, &symbol_hash, 1)
            {
                let mut callers: Vec<Value> = Vec::new();
                if let Value::Array(ls) = &layers {
                    for layer in ls {
                        if let Some(syms) = layer["symbols"].as_array() {
                            callers.extend(syms.clone());
                        }
                    }
                }
                for caller in callers.iter().take(include_callers.max(0) as usize) {
                    if total_chars > max_chars {
                        truncated = true;
                        break 'seeds;
                    }
                    let caller_hash = caller["symbol_hash"].as_str().unwrap_or("");
                    if !caller_hash.is_empty() && !included.contains(caller_hash) {
                        included.insert(caller_hash.to_string());
                        let meta = json!({
                            "qualified_name": caller["qualified_name"],
                            "file_path": caller["file_path"],
                            "start_line": 0,
                            "similarity": 0.0,
                            "summary": "",
                        });
                        if let Some(cblock) = summary_build_rag_block(
                            conn, workspace_id, caller_hash, &meta, "caller",
                        )? {
                            total_chars +=
                                cblock["code"].as_str().map(|s| s.chars().count()).unwrap_or(0);
                            context_blocks.push(cblock);
                        }
                    }
                }
            }
        }
        // 被调用方上下文
        if include_callees > 0 {
            let callees =
                summary_get_callees(conn, workspace_id, &symbol_hash, include_callees)?;
            for callee in callees {
                if total_chars > max_chars {
                    truncated = true;
                    break 'seeds;
                }
                let callee_hash = callee["symbol_hash"].as_str().unwrap_or("");
                if !callee_hash.is_empty() && !included.contains(callee_hash) {
                    included.insert(callee_hash.to_string());
                    let meta = json!({
                        "qualified_name": callee["qualified_name"],
                        "file_path": callee["rel_path"],
                        "start_line": 0,
                        "similarity": 0.0,
                        "summary": "",
                    });
                    if let Some(cblock) = summary_build_rag_block(
                        conn, workspace_id, callee_hash, &meta, "callee",
                    )? {
                        total_chars +=
                            cblock["code"].as_str().map(|s| s.chars().count()).unwrap_or(0);
                        context_blocks.push(cblock);
                    }
                }
            }
        }
    }

    // 拼接 RAG 上下文（zh_CN i18n 键值）
    let mut lines: Vec<String> = Vec::new();
    lines.push(format!("# 问题\n{question}\n"));
    lines.push("# 相关代码上下文\n".to_string());
    for block in &context_blocks {
        let role_label = match block["role"].as_str().unwrap_or("") {
            "seed" => "种子",
            "caller" => "调用方",
            "callee" => "被调用方",
            _ => "相关",
        };
        let qn = block["qualified_name"].as_str().unwrap_or("unknown");
        let fp = block["file_path"].as_str().unwrap_or("");
        let sl = block["start_line"].as_i64().unwrap_or(0);
        let sim = block["similarity"].as_f64().unwrap_or(0.0);
        let summary = block["summary"].as_str().unwrap_or("");
        let code = block["code"].as_str().unwrap_or("");
        lines.push(format!("## [{role_label}] {qn} ({fp}:{sl}) — similarity: {sim:.4}"));
        if !summary.is_empty() {
            lines.push(format!("摘要: {summary}"));
        }
        if !code.is_empty() {
            lines.push(format!("```\n{code}\n```"));
        }
        lines.push(String::new());
    }
    let rag_context = lines.join("\n");
    let estimated_tokens = rag_context.chars().count() / 4;

    Ok(json!({
        "question": question,
        "seed_functions": seeds,
        "context_blocks": context_blocks,
        "rag_context": rag_context,
        "estimated_tokens": estimated_tokens,
        "truncated": truncated,
        "metadata": {
            "total_functions_included": included.len(),
            "has_vector_index": false,
            "fallback_used": fallback_used,
        },
    }))
}

/// summary_parse_time_window_seconds —— 复刻 db_token_savings._parse_time_window_seconds。
fn summary_parse_time_window_seconds(time_window: &str) -> i64 {
    let w = time_window.trim().to_lowercase();
    if w.is_empty() {
        return 0;
    }
    let (num, unit) = match w.split_last_char() {
        Some(v) => v,
        None => return 0,
    };
    let n: i64 = match num.parse() {
        Ok(v) => v,
        Err(_) => return 0,
    };
    match unit {
        'd' => n * 86400,
        'w' => n * 86400 * 7,
        'm' => n * 86400 * 30,
        'y' => n * 86400 * 365,
        _ => 0,
    }
}

trait SplitLastChar {
    fn split_last_char(&self) -> Option<(&str, char)>;
}

impl SplitLastChar for str {
    fn split_last_char(&self) -> Option<(&str, char)> {
        let c = self.chars().last()?;
        Some((&self[..self.len() - c.len_utf8()], c))
    }
}

/// get_token_savings_report —— 复刻 db_token_savings.get_token_savings_report。
pub fn handle_summary_token_savings_report(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    // get_total_savings（无 operation_filter 路径）
    let time_window = get_str_param_or(params, "time_window", "30d");
    let seconds = summary_parse_time_window_seconds(&time_window);
    let (total_saved, total_operations, avg_savings_pct): (i64, i64, f64) = if seconds > 0 {
        let cutoff = summary_now_unix() - seconds as f64;
        conn.query_row(
            "SELECT COALESCE(SUM(tokens_saved), 0), COUNT(*), COALESCE(AVG(savings_pct), 0) \
             FROM token_savings_ledger WHERE created_at >= ?1",
            [cutoff],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("token savings: {e}")))?
        .unwrap_or((0, 0, 0.0))
    } else {
        conn.query_row(
            "SELECT COALESCE(SUM(tokens_saved), 0), COUNT(*), COALESCE(AVG(savings_pct), 0) \
             FROM token_savings_ledger",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("token savings: {e}")))?
        .unwrap_or((0, 0, 0.0))
    };

    // 按操作类型分组
    let mut stmt = conn
        .prepare(if seconds > 0 {
            let cutoff = summary_now_unix() - seconds as f64;
            let _ = cutoff;
            "SELECT operation, COALESCE(SUM(tokens_saved), 0), COUNT(*), COALESCE(AVG(savings_pct), 0) \
             FROM token_savings_ledger WHERE created_at >= ?1 GROUP BY operation ORDER BY 2 DESC"
        } else {
            "SELECT operation, COALESCE(SUM(tokens_saved), 0), COUNT(*), COALESCE(AVG(savings_pct), 0) \
             FROM token_savings_ledger GROUP BY operation ORDER BY 2 DESC"
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("token group prepare: {e}")))?;
    let by_operation = if seconds > 0 {
        let cutoff = summary_now_unix() - seconds as f64;
        let rows: Vec<(Option<String>, i64, i64, f64)> = stmt
            .query_map([cutoff], |r| {
                Ok((
                    r.get::<_, Option<String>>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, f64>(3)?,
                ))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("token group: {e}")))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| DaemonRpcError::internal_error(format!("token group collect: {e}")))?;
        summary_by_operation(rows)?
    } else {
        let rows: Vec<(Option<String>, i64, i64, f64)> = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, Option<String>>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, f64>(3)?,
                ))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("token group: {e}")))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| DaemonRpcError::internal_error(format!("token group collect: {e}")))?;
        summary_by_operation(rows)?
    };

    // 每日趋势（空窗口回退 365 天，对齐 Python 86400*365）
    let trend_seconds = if time_window.is_empty() {
        86400 * 365
    } else if seconds > 0 {
        seconds
    } else {
        0
    };
    let mut daily_trend: Vec<Value> = Vec::new();
    if trend_seconds > 0 {
        let cutoff = summary_now_unix() - trend_seconds as f64;
        let mut stmt = conn
            .prepare(
                "SELECT DATE(created_at, 'unixepoch', 'localtime') as date, \
                 SUM(tokens_saved) as daily_saved, COUNT(*) as daily_ops \
                 FROM token_savings_ledger WHERE created_at >= ?1 \
                 GROUP BY DATE(created_at, 'unixepoch', 'localtime') ORDER BY date ASC",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("trend prepare: {e}")))?;
        let rows = stmt
            .query_map([cutoff], |r| {
                Ok((
                    r.get::<_, Option<String>>(0)?,
                    r.get::<_, Option<i64>>(1)?,
                    r.get::<_, i64>(2)?,
                ))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("trend: {e}")))?;
        for row in rows {
            let (date, saved, ops) = row
                .map_err(|e| DaemonRpcError::internal_error(format!("trend row: {e}")))?;
            daily_trend.push(json!({
                "date": date.unwrap_or_default(),
                "saved": saved.unwrap_or(0),
                "ops": ops,
            }));
        }
    }

    let headline = if total_saved >= 1_000_000 {
        format!("已为你节省 {:.1}M tokens", total_saved as f64 / 1_000_000.0)
    } else if total_saved >= 1000 {
        format!("已为你节省 {:.1}K tokens", total_saved as f64 / 1000.0)
    } else {
        format!("已为你节省 {total_saved} tokens")
    };

    Ok(json!({
        "time_window": time_window,
        "total_saved": total_saved,
        "total_operations": total_operations,
        "avg_savings_pct": (avg_savings_pct * 100.0).round() / 100.0,
        "by_operation": by_operation,
        "daily_trend": daily_trend,
        "headline": headline,
    }))
}

fn summary_by_operation(
    rows: Vec<(Option<String>, i64, i64, f64)>,
) -> Result<Value, DaemonRpcError> {
    let mut by_op = Map::new();
    for (op, saved, count, avg_pct) in rows {
        by_op.insert(
            op.unwrap_or_default(),
            json!({
                "total_saved": saved,
                "op_count": count,
                "avg_savings_pct": (avg_pct * 100.0).round() / 100.0,
            }),
        );
    }
    Ok(Value::Object(by_op))
}

/// summary_now_unix —— 当前 Unix 时间戳（秒，浮点，对齐 time.time()）。
fn summary_now_unix() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// get_vulnerability_blast_radius —— 复刻 db_impact.get_vulnerability_blast_radius
///（Semgrep findings × blast_radius 反向传播）。
pub fn handle_summary_vulnerability_blast_radius(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let finding_id = get_int_param_or(params, "finding_id", 0);
    let severity_filter = get_str_param_or(params, "severity_filter", "");
    let depth = get_int_param_or(params, "depth", 3);

    let sql = if finding_id > 0 {
        "SELECT sf.id, sf.rule_id, sf.rule_name, sf.severity, sf.message, \
         sf.start_line, sf.end_line, sf.symbol_id, sf.symbol_qualified, \
         sf.content_hash, fi.rel_path \
         FROM semgrep_findings sf \
         LEFT JOIN file_instances fi ON sf.file_instance_id = fi.id \
         WHERE sf.id = ?1"
            .to_string()
    } else if !severity_filter.is_empty() {
        "SELECT sf.id, sf.rule_id, sf.rule_name, sf.severity, sf.message, \
         sf.start_line, sf.end_line, sf.symbol_id, sf.symbol_qualified, \
         sf.content_hash, fi.rel_path \
         FROM semgrep_findings sf \
         LEFT JOIN file_instances fi ON sf.file_instance_id = fi.id \
         WHERE sf.severity = ?1 ORDER BY sf.severity DESC, sf.id ASC"
            .to_string()
    } else {
        "SELECT sf.id, sf.rule_id, sf.rule_name, sf.severity, sf.message, \
         sf.start_line, sf.end_line, sf.symbol_id, sf.symbol_qualified, \
         sf.content_hash, fi.rel_path \
         FROM semgrep_findings sf \
         LEFT JOIN file_instances fi ON sf.file_instance_id = fi.id \
         ORDER BY sf.severity DESC, sf.id ASC"
            .to_string()
    };
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("vuln prepare: {e}")))?;
    let map = |r: &rusqlite::Row<'_>| -> rusqlite::Result<(
        i64, Option<String>, Option<String>, Option<String>, Option<String>,
        Option<i64>, Option<i64>, Option<i64>, Option<String>, Option<String>, Option<String>,
    )> {
        Ok((
            r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?,
            r.get(5)?, r.get(6)?, r.get(7)?, r.get(8)?, r.get(9)?, r.get(10)?,
        ))
    };
    let findings_rows = if finding_id > 0 {
        stmt.query_map([finding_id], map)
    } else if !severity_filter.is_empty() {
        stmt.query_map([severity_filter], map)
    } else {
        stmt.query_map([], map)
    }
    .map_err(|e| DaemonRpcError::internal_error(format!("vuln query: {e}")))?
    .collect::<Result<Vec<_>, _>>()
    .map_err(|e| DaemonRpcError::internal_error(format!("vuln collect: {e}")))?;
    if findings_rows.is_empty() {
        return Ok(json!({
            "total_findings": 0,
            "total_impacted_symbols": 0,
            "risk_level": "none",
            "findings": [],
            "impacted_symbols_summary": {
                "by_layer": {"code": 0, "db": 0, "api": 0, "config": 0},
                "high_risk_callers": [],
            },
        }));
    }

    let mut findings_results: Vec<Value> = Vec::new();
    let mut all_impacted_hashes: std::collections::HashSet<String> = std::collections::HashSet::new();
    // qualified_name -> 被多少漏洞影响（插入序保留供平局稳定）
    let mut caller_order: Vec<String> = Vec::new();
    let mut caller_count: HashMap<String, i64> = HashMap::new();

    for row in findings_rows {
        let (id, rule_id, rule_name, severity, message, start_line, _end_line,
             symbol_id, symbol_qualified_o, content_hash_o, rel_path) = row;
        let mut symbol_hash = content_hash_o.unwrap_or_default();
        let symbol_qualified = symbol_qualified_o.unwrap_or_default();
        if symbol_hash.is_empty() {
            if let Some(sid) = symbol_id {
                let h: Option<String> = conn
                    .query_row(
                        "SELECT symbol_hash FROM symbols WHERE id = ?1",
                        [sid],
                        |r| r.get(0),
                    )
                    .optional()
                    .map_err(|e| DaemonRpcError::internal_error(format!("vuln sym: {e}")))?
                    .flatten();
                if let Some(h) = h {
                    symbol_hash = h;
                }
            }
        }
        let (br, impacted_count): (Value, i64) = if !symbol_hash.is_empty() {
            match security_blast_radius_sql(conn, workspace_id, &symbol_hash, depth) {
                Some((src, layers, total, by_layer)) => (
                    json!({
                        "source_symbol": src,
                        "source_hash": symbol_hash,
                        "depth": depth,
                        "layers": layers,
                        "total_impacted": total,
                        "by_layer": by_layer,
                    }),
                    total,
                ),
                None => (
                    json!({
                        "source_symbol": "",
                        "source_hash": symbol_hash,
                        "depth": depth,
                        "layers": [],
                        "total_impacted": 0,
                        "by_layer": {"code": 0, "db": 0, "api": 0, "config": 0},
                    }),
                    0,
                ),
            }
        } else {
            (
                json!({
                    "layers": [], "total_impacted": 0,
                    "by_layer": {"code": 0, "db": 0, "api": 0, "config": 0},
                }),
                0,
            )
        };
        // 收集受影响符号（全局去重 + 调用方计数）
        if let Some(layers) = br["layers"].as_array() {
            for layer in layers {
                if let Some(syms) = layer["symbols"].as_array() {
                    for sym in syms {
                        let h = sym["symbol_hash"].as_str().unwrap_or("");
                        let qn = sym["qualified_name"].as_str().unwrap_or("");
                        if !h.is_empty() {
                            all_impacted_hashes.insert(h.to_string());
                        }
                        if !qn.is_empty() {
                            if !caller_order.iter().any(|x| x == qn) {
                                caller_order.push(qn.to_string());
                            }
                            *caller_count.entry(qn.to_string()).or_insert(0) += 1;
                        }
                    }
                }
            }
        }
        findings_results.push(json!({
            "finding_id": id,
            "rule_id": rule_id,
            "rule_name": rule_name,
            "severity": severity,
            "message": message,
            "file_path": rel_path.unwrap_or_default(),
            "start_line": start_line,
            "symbol_qualified": symbol_qualified,
            "symbol_hash": symbol_hash,
            "blast_radius": br,
            "impacted_count": impacted_count,
        }));
    }

    let has_error = findings_results.iter().any(|f| {
        matches!(
            f["severity"].as_str().unwrap_or(""),
            "ERROR" | "error" | "CRITICAL" | "critical"
        )
    });
    let total_impacted = all_impacted_hashes.len() as i64;
    let risk_level = if has_error && total_impacted > 10 {
        "critical"
    } else if has_error && total_impacted > 3 {
        "high"
    } else if findings_results.iter().any(|f| {
        matches!(f["severity"].as_str().unwrap_or(""), "WARN" | "warn" | "WARNING")
    }) {
        "medium"
    } else {
        "low"
    };

    let mut high_risk: Vec<(i64, Value)> = caller_order
        .iter()
        .filter_map(|qn| {
            let cnt = caller_count.get(qn).copied().unwrap_or(0);
            if cnt >= 2 {
                Some((cnt, json!({"qualified_name": qn, "affected_by_count": cnt})))
            } else {
                None
            }
        })
        .collect();
    high_risk.sort_by(|a, b| b.0.cmp(&a.0));
    let high_risk_callers: Vec<Value> =
        high_risk.into_iter().take(20).map(|(_, v)| v).collect();

    let mut summary_by_layer = json!({"code": 0, "db": 0, "api": 0, "config": 0});
    for f in &findings_results {
        for k in ["code", "db", "api", "config"] {
            let v = summary_by_layer[k].as_i64().unwrap_or(0)
                + f["blast_radius"]["by_layer"][k].as_i64().unwrap_or(0);
            summary_by_layer[k] = json!(v);
        }
    }

    Ok(json!({
        "total_findings": findings_results.len(),
        "total_impacted_symbols": total_impacted,
        "risk_level": risk_level,
        "findings": findings_results,
        "impacted_symbols_summary": {
            "by_layer": summary_by_layer,
            "high_risk_callers": high_risk_callers,
        },
    }))
}

/// get_clone_aware_impact —— 复刻 db_impact.get_clone_aware_impact
///（源符号 + clone_pairs 联动影响分析）。
pub fn handle_summary_clone_aware_impact(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let qualified_name = get_str_param_or(params, "qualified_name", "");
    let depth = get_int_param_or(params, "depth", 3);

    let row = conn
        .query_row(
            "SELECT s.id, s.symbol_hash, s.qualified_name, s.name, s.kind, fi.rel_path \
             FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND s.qualified_name = ?2 LIMIT 1",
            rusqlite::params![workspace_id, qualified_name],
            |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, Option<String>>(1)?,
                    r.get::<_, Option<String>>(2)?,
                    r.get::<_, Option<String>>(3)?,
                    r.get::<_, Option<String>>(4)?,
                    r.get::<_, Option<String>>(5)?,
                ))
            },
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("clone source: {e}")))?;
    let (symbol_id, symbol_hash_o, qn_o, name_o, kind_o, rel_path) = match row {
        Some(v) => v,
        None => {
            return Ok(json!({
                "source_symbol": "",
                "error": format!("符号不存在: {qualified_name}"),
            }));
        }
    };
    let symbol_hash = symbol_hash_o.unwrap_or_default();

    let br_value = |hash: &str| -> Result<Value, DaemonRpcError> {
        Ok(match security_blast_radius_sql(conn, workspace_id, hash, depth) {
            Some((src, layers, total, by_layer)) => json!({
                "source_symbol": src, "source_hash": hash, "depth": depth,
                "layers": layers, "total_impacted": total, "by_layer": by_layer,
            }),
            None => json!({
                "source_symbol": "", "source_hash": hash, "depth": depth,
                "layers": [], "total_impacted": 0,
                "by_layer": {"code": 0, "db": 0, "api": 0, "config": 0},
            }),
        })
    };
    let original_radius = br_value(&symbol_hash)?;

    // list_clones(symbol_id, limit=50)（clone_type=0 / min_similarity=0 → 无过滤）
    let mut stmt = conn
        .prepare(
            "SELECT cp.clone_type, cp.similarity, sa.qualified_name as symbol_a_qualified, \
             sb.qualified_name as symbol_b_qualified, fa.rel_path as file_a, \
             fb.rel_path as file_b, sa.start_line as symbol_a_line, \
             sb.start_line as symbol_b_line \
             FROM clone_pairs cp \
             JOIN symbols sa ON cp.symbol_a_id = sa.id \
             JOIN symbols sb ON cp.symbol_b_id = sb.id \
             JOIN file_instances fa ON sa.file_instance_id = fa.id \
             JOIN file_instances fb ON sb.file_instance_id = fb.id \
             WHERE cp.workspace_id = ?1 AND (cp.symbol_a_id = ?2 OR cp.symbol_b_id = ?2) \
             ORDER BY cp.similarity DESC, cp.detected_at DESC LIMIT 50",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("clones prepare: {e}")))?;
    let clone_rows = stmt
        .query_map(rusqlite::params![workspace_id, symbol_id], |r| {
            Ok((
                r.get::<_, Option<i64>>(0)?,
                r.get::<_, Option<f64>>(1)?,
                r.get::<_, Option<String>>(2)?,
                r.get::<_, Option<String>>(3)?,
                r.get::<_, Option<String>>(4)?,
                r.get::<_, Option<String>>(5)?,
                r.get::<_, Option<i64>>(6)?,
                r.get::<_, Option<i64>>(7)?,
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("clones: {e}")))?;
    let mut clone_infos: Vec<Value> = Vec::new();
    let mut clone_impacts: Vec<Value> = Vec::new();
    for row in clone_rows {
        let (clone_type, similarity, a_qn, b_qn, file_a, file_b, a_line, b_line) = row
            .map_err(|e| DaemonRpcError::internal_error(format!("clones row: {e}")))?;
        let a_qn = a_qn.unwrap_or_default();
        let b_qn = b_qn.unwrap_or_default();
        let (clone_qn, clone_file, clone_line) = if a_qn == qualified_name {
            (b_qn, file_b.unwrap_or_default(), b_line.unwrap_or(0))
        } else {
            (a_qn, file_a.unwrap_or_default(), a_line.unwrap_or(0))
        };
        clone_infos.push(json!({
            "qualified_name": clone_qn,
            "file": clone_file,
            "line": clone_line,
            "similarity": similarity.unwrap_or(0.0),
            "clone_type": clone_type.unwrap_or(0),
        }));
        if !clone_qn.is_empty() {
            let clone_hash: Option<String> = conn
                .query_row(
                    "SELECT s.symbol_hash FROM symbols s \
                     JOIN file_instances fi ON s.file_instance_id = fi.id \
                     WHERE fi.workspace_id = ?1 AND s.qualified_name = ?2 LIMIT 1",
                    rusqlite::params![workspace_id, clone_qn],
                    |r| r.get(0),
                )
                .optional()
                .map_err(|e| DaemonRpcError::internal_error(format!("clone hash: {e}")))?
                .flatten();
            if let Some(ch) = clone_hash {
                clone_impacts.push(json!({
                    "clone_symbol": clone_qn,
                    "blast_radius": br_value(&ch)?,
                }));
            }
        }
    }

    // 合并去重（qualified_name 集合，丢弃空串）
    let mut all_impacted: std::collections::HashSet<String> = std::collections::HashSet::new();
    let collect_layers = |radius: &Value, set: &mut std::collections::HashSet<String>| {
        if let Some(layers) = radius["layers"].as_array() {
            for layer in layers {
                if let Some(syms) = layer["symbols"].as_array() {
                    for sym in syms {
                        match sym {
                            Value::String(s) => {
                                set.insert(s.clone());
                            }
                            Value::Object(_) => {
                                let qn = sym["qualified_name"].as_str().unwrap_or("");
                                set.insert(qn.to_string());
                            }
                            _ => {}
                        }
                    }
                }
            }
        }
    };
    collect_layers(&original_radius, &mut all_impacted);
    for ci in &clone_impacts {
        collect_layers(&ci["blast_radius"], &mut all_impacted);
    }
    all_impacted.remove("");

    Ok(json!({
        "source_symbol": {
            "qualified_name": qn_o,
            "name": name_o,
            "kind": kind_o,
            "file": rel_path,
            "symbol_hash": symbol_hash,
        },
        "original_blast_radius": original_radius,
        "clones": clone_infos,
        "clone_blast_radii": clone_impacts,
        "total_impacted_with_clones": all_impacted.len(),
    }))
}

/// review_readiness —— 复刻 db_impact.review_readiness_report
///（blast_radius + cross_layer_impact + 覆盖率合成）。
pub fn handle_summary_review_readiness(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let symbol_hash = get_str_param_or(params, "symbol_hash", "");
    let depth = 3;
    let (blast, cross): (Value, (Vec<Value>, Vec<Value>, Vec<Value>, Vec<Value>)) = {
        let blast = match security_blast_radius_sql(conn, workspace_id, &symbol_hash, depth) {
            Some((src, layers, total, by_layer)) => json!({
                "source_symbol": src, "source_hash": symbol_hash, "depth": depth,
                "layers": layers, "total_impacted": total, "by_layer": by_layer,
            }),
            None => json!({
                "source_symbol": "", "source_hash": symbol_hash, "depth": depth,
                "layers": [], "total_impacted": 0,
                "by_layer": {"code": 0, "db": 0, "api": 0, "config": 0},
            }),
        };
        let cross = match summary_cross_layer_full(conn, workspace_id, &symbol_hash)? {
            None => (Vec::new(), Vec::new(), Vec::new(), Vec::new()),
            Some(v) => v,
        };
        (blast, cross)
    };
    let (_code_layer, db_layer, api_layer, _config_layer) = cross;
    let total = blast["total_impacted"].as_i64().unwrap_or(0);

    let scope = if total > 20 {
        "high"
    } else if total > 5 {
        "medium"
    } else {
        "low"
    };

    // 必测项：受影响 public 函数（去重）
    let mut must_test: Vec<Value> = Vec::new();
    let mut seen_test: std::collections::HashSet<String> = std::collections::HashSet::new();
    if let Some(layers) = blast["layers"].as_array() {
        for layer in layers {
            if let Some(syms) = layer["symbols"].as_array() {
                for sym in syms {
                    let vis = sym["visibility"].as_str().unwrap_or("").to_lowercase();
                    let kind = sym["kind"].as_str().unwrap_or("").to_lowercase();
                    let qn = sym["qualified_name"].as_str().unwrap_or("");
                    if vis == "public"
                        && matches!(kind.as_str(), "fn" | "function" | "method")
                        && !qn.is_empty()
                        && seen_test.insert(qn.to_string())
                    {
                        must_test.push(json!({
                            "qualified_name": qn,
                            "name": sym["name"],
                            "file_path": sym["file_path"],
                        }));
                    }
                }
            }
        }
    }

    // 人工审查点：DB / API 层
    let mut review_points: Vec<Value> = Vec::new();
    for item in &db_layer {
        let tbl = item["table"].as_str().unwrap_or("");
        review_points.push(json!({
            "layer": "db", "target": tbl,
            "source": item["source"],
            "message": format!("DB 表受影响: {tbl}"),
        }));
    }
    for item in &api_layer {
        let sym_name = item["name"].as_str().unwrap_or("");
        review_points.push(json!({
            "layer": "api", "target": item["symbol"],
            "source": sym_name,
            "message": format!("API 端点受影响: {sym_name}"),
        }));
    }

    let mut report = json!({
        "impact_scope": scope,
        "risk_level": scope,
        "total_impacted": total,
        "must_test": must_test,
        "review_points": review_points,
        "by_layer": blast["by_layer"],
    });

    // 覆盖率（源符号 get_coverage_for_symbol）
    let source_qn = blast["source_symbol"].as_str().unwrap_or("");
    if !source_qn.is_empty() {
        if let Some(cov) = summary_coverage_for_symbol(conn, workspace_id, source_qn)? {
            report["coverage"] = json!({
                "qualified_name": cov["qualified_name"],
                "coverage_pct": cov["coverage_pct"],
                "covered_lines": cov["covered_lines"],
                "tracked_lines": cov["tracked_lines"],
            });
        }
    }
    Ok(report)
}

/// summary_coverage_for_symbol —— review_readiness 覆盖率附带
///（复刻 db_coverage.get_coverage_for_symbol 字段子集）。
fn summary_coverage_for_symbol(
    conn: &Connection,
    workspace_id: i64,
    qualified_name: &str,
) -> Result<Option<Value>, DaemonRpcError> {
    let row = conn
        .query_row(
            "SELECT s.id, s.start_line, s.end_line FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND s.qualified_name = ?2 LIMIT 1",
            rusqlite::params![workspace_id, qualified_name],
            |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?, r.get::<_, i64>(2)?)),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("coverage sym: {e}")))?;
    let (symbol_id, start_line, end_line) = match row {
        Some(v) => v,
        None => return Ok(None),
    };
    let tracked: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM coverage_data \
             WHERE symbol_id = ?1 AND line_start >= ?2 AND line_end <= ?3",
            rusqlite::params![symbol_id, start_line, end_line],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("coverage tracked: {e}")))?
        .unwrap_or(0);
    let covered: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM coverage_data \
             WHERE symbol_id = ?1 AND line_start >= ?2 AND line_end <= ?3 AND hit_count > 0",
            rusqlite::params![symbol_id, start_line, end_line],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("coverage covered: {e}")))?
        .unwrap_or(0);
    let total_lines = end_line - start_line + 1;
    let pct = if tracked > 0 { covered as f64 / tracked as f64 * 100.0 } else { 0.0 };
    Ok(Some(json!({
        "qualified_name": qualified_name,
        "coverage_pct": pct,
        "covered_lines": covered,
        "tracked_lines": tracked,
        "total_lines": total_lines,
    })))
}

// ---- evolution / hotspot / defect_learn ----

/// summary_parse_time_window —— 复刻 db_evolution._parse_time_window
///（d/w/m/y = 86400/604800/2592000/31536000；返回截止时间戳，0 = 无限制）。
fn summary_parse_time_window(time_window: &str) -> f64 {
    let tw = time_window.trim();
    if tw.is_empty() {
        return 0.0;
    }
    let re = match regex::Regex::new(r"^\s*(\d+)\s*([dwmy])\s*$") {
        Ok(re) => re,
        Err(_) => return 0.0,
    };
    let cap = match re.captures(tw) {
        Some(c) => c,
        None => return 0.0,
    };
    let num: i64 = match cap.get(1).and_then(|g| g.as_str().parse().ok()) {
        Some(v) => v,
        None => return 0.0,
    };
    let seconds: i64 = match cap.get(2).map(|g| g.as_str()) {
        Some("d") => 86400,
        Some("w") => 7 * 86400,
        Some("m") => 30 * 86400,
        Some("y") => 365 * 86400,
        _ => return 0.0,
    };
    if seconds <= 0 {
        return 0.0;
    }
    summary_now_unix() - (num * seconds) as f64
}

/// summary_change_distribution —— 复刻 db_evolution._compute_change_distribution
///（按天/周/月；%W 周编号经 SQLite strftime 与 Python time.strftime 对齐）。
fn summary_change_distribution(
    conn: &Connection,
    timestamps: &[f64],
) -> Result<Value, DaemonRpcError> {
    let mut daily: BTreeMap<String, i64> = BTreeMap::new();
    let mut weekly: BTreeMap<String, i64> = BTreeMap::new();
    let mut monthly: BTreeMap<String, i64> = BTreeMap::new();
    for ts in timestamps {
        let d: String = conn
            .query_row(
                "SELECT strftime('%Y-%m-%d', ?1, 'unixepoch', 'localtime')",
                [ts],
                |r| r.get(0),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("dist daily: {e}")))?;
        let w: String = conn
            .query_row(
                "SELECT strftime('%Y-W%W', ?1, 'unixepoch', 'localtime')",
                [ts],
                |r| r.get(0),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("dist weekly: {e}")))?;
        let m: String = conn
            .query_row(
                "SELECT strftime('%Y-%m', ?1, 'unixepoch', 'localtime')",
                [ts],
                |r| r.get(0),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("dist monthly: {e}")))?;
        *daily.entry(d).or_insert(0) += 1;
        *weekly.entry(w).or_insert(0) += 1;
        *monthly.entry(m).or_insert(0) += 1;
    }
    Ok(json!({"daily": daily, "weekly": weekly, "monthly": monthly}))
}

/// evolution_frequency —— 复刻 db_evolution.function_change_frequency。
pub fn handle_summary_evolution_frequency(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let qualified_name = get_str_param_or(params, "qualified_name", "");
    let time_window = get_str_param_or(params, "time_window", "");
    let cutoff = summary_parse_time_window(&time_window);

    let sql = if cutoff > 0.0 {
        "SELECT fv.id as fv_id, fv.parsed_at, fv.commit_hash, gc.author, gc.message \
         FROM file_symbol_versions fsv \
         JOIN file_versions fv ON fsv.file_version_id = fv.id \
         JOIN file_instances fi ON fv.file_instance_id = fi.id \
         LEFT JOIN git_commits gc ON fv.commit_hash = gc.commit_hash \
         WHERE fi.workspace_id = ?1 AND fsv.qualified_name = ?2 AND fv.parsed_at >= ?3 \
         ORDER BY fv.parsed_at ASC"
            .to_string()
    } else {
        "SELECT fv.id as fv_id, fv.parsed_at, fv.commit_hash, gc.author, gc.message \
         FROM file_symbol_versions fsv \
         JOIN file_versions fv ON fsv.file_version_id = fv.id \
         JOIN file_instances fi ON fv.file_instance_id = fi.id \
         LEFT JOIN git_commits gc ON fv.commit_hash = gc.commit_hash \
         WHERE fi.workspace_id = ?1 AND fsv.qualified_name = ?2 \
         ORDER BY fv.parsed_at ASC"
            .to_string()
    };
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("evolution prepare: {e}")))?;
    let map = |r: &rusqlite::Row<'_>| -> rusqlite::Result<(i64, f64, Option<String>, Option<String>, Option<String>)> {
        Ok((
            r.get(0)?,
            r.get::<_, Option<f64>>(1)?.unwrap_or(0.0),
            r.get(2)?,
            r.get(3)?,
            r.get(4)?,
        ))
    };
    let rows: Vec<(i64, f64, Option<String>, Option<String>, Option<String>)> = if cutoff > 0.0 {
        stmt.query_map(rusqlite::params![workspace_id, qualified_name, cutoff], map)
    } else {
        stmt.query_map(rusqlite::params![workspace_id, qualified_name], map)
    }
    .map_err(|e| DaemonRpcError::internal_error(format!("evolution query: {e}")))?
    .collect::<Result<Vec<_>, _>>()
    .map_err(|e| DaemonRpcError::internal_error(format!("evolution collect: {e}")))?;

    // 去重同一 file_version_id（保序）
    let mut seen_fv: std::collections::HashSet<i64> = std::collections::HashSet::new();
    let mut timeline: Vec<Value> = Vec::new();
    let mut changers: Vec<String> = Vec::new();
    let mut timestamps: Vec<f64> = Vec::new();
    for (fv_id, parsed_at, commit_hash, author_o, message_o) in rows {
        if !seen_fv.insert(fv_id) {
            continue;
        }
        timestamps.push(parsed_at);
        let author = author_o.unwrap_or_default();
        if !author.is_empty() && !changers.iter().any(|c| c == &author) {
            changers.push(author.clone());
        }
        timeline.push(json!({
            "timestamp": parsed_at,
            "commit_hash": commit_hash.unwrap_or_default(),
            "author": author,
            "message": message_o.unwrap_or_default(),
        }));
    }
    let change_count = timestamps.len() as i64;
    let first_seen = timestamps.first().copied().unwrap_or(0.0);
    let last_changed = timestamps.last().copied().unwrap_or(0.0);
    let intervals: Vec<f64> = timestamps
        .windows(2)
        .map(|w| (w[1] - w[0]).max(0.0))
        .collect();
    let avg_interval = if intervals.is_empty() {
        0.0
    } else {
        intervals.iter().sum::<f64>() / intervals.len() as f64
    };
    let distribution = summary_change_distribution(conn, &timestamps)?;

    Ok(json!({
        "qualified_name": qualified_name,
        "change_count": change_count,
        "first_seen": first_seen,
        "last_changed": last_changed,
        "changers": changers,
        "timeline": timeline,
        "intervals": intervals,
        "avg_interval": avg_interval,
        "distribution": distribution,
    }))
}

/// hotspot_evolution —— 复刻 db_evolution.hotspot_evolution（→ _compute_hotspot_scores）。
pub fn handle_summary_hotspot_evolution(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let module_filter = get_str_param_or(params, "module_filter", "");
    let now = summary_now_unix();

    let base_sql = "SELECT s.symbol_hash, s.qualified_name, s.module_path, \
         s.start_line, s.end_line, sc.content, fi.rel_path \
         FROM symbols s \
         JOIN file_instances fi ON s.file_instance_id = fi.id \
         LEFT JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash \
         WHERE fi.workspace_id = ?1 AND s.kind IN ('fn','function','method') \
           AND s.qualified_name != ''";
    let sql = if module_filter.is_empty() {
        base_sql.to_string()
    } else {
        format!("{base_sql} AND s.module_path LIKE ?2")
    };
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("hotspot prepare: {e}")))?;
    let map = |r: &rusqlite::Row<'_>| -> rusqlite::Result<(String, String, Option<String>, i64, i64, Option<String>, Option<String>)> {
        Ok((
            r.get(0)?,
            r.get(1)?,
            r.get(2)?,
            r.get::<_, Option<i64>>(3)?.unwrap_or(0),
            r.get::<_, Option<i64>>(4)?.unwrap_or(0),
            r.get(5)?,
            r.get(6)?,
        ))
    };
    let symbols: Vec<_> = if module_filter.is_empty() {
        stmt.query_map([workspace_id], map)
    } else {
        let like = format!("{module_filter}%");
        stmt.query_map(rusqlite::params![workspace_id, like], map)
    }
    .map_err(|e| DaemonRpcError::internal_error(format!("hotspot query: {e}")))?
    .collect::<Result<Vec<_>, _>>()
    .map_err(|e| DaemonRpcError::internal_error(format!("hotspot collect: {e}")))?;
    if symbols.is_empty() {
        return Ok(json!([]));
    }

    // 批量预取变更次数（全局，无 workspace 过滤——对齐 Python）
    let mut stmt = conn
        .prepare(
            "SELECT fsv.symbol_hash, COUNT(DISTINCT fsv.file_version_id) as cnt, \
             MIN(fv.parsed_at) as first_seen, MAX(fv.parsed_at) as last_changed \
             FROM file_symbol_versions fsv \
             JOIN file_versions fv ON fsv.file_version_id = fv.id \
             GROUP BY fsv.symbol_hash",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("hotspot change prepare: {e}")))?;
    let mut change_map: HashMap<String, (i64, f64, f64)> = HashMap::new();
    let rows = stmt
        .query_map([], |r| {
            Ok((
                r.get::<_, Option<String>>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, Option<f64>>(2)?.unwrap_or(0.0),
                r.get::<_, Option<f64>>(3)?.unwrap_or(0.0),
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("hotspot change: {e}")))?;
    for row in rows {
        let (hash, cnt, first, last) = row
            .map_err(|e| DaemonRpcError::internal_error(format!("hotspot change row: {e}")))?;
        if let Some(h) = hash {
            change_map.insert(h, (cnt, first, last));
        }
    }

    // 批量预取缺陷数（全局，按 symbol_qualified）
    let mut stmt = conn
        .prepare(
            "SELECT symbol_qualified, COUNT(*) as cnt FROM semgrep_findings \
             WHERE symbol_qualified != '' GROUP BY symbol_qualified",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("hotspot defect prepare: {e}")))?;
    let mut defect_map: HashMap<String, i64> = HashMap::new();
    let rows = stmt
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))
        .map_err(|e| DaemonRpcError::internal_error(format!("hotspot defect: {e}")))?;
    for row in rows {
        let (qn, cnt) = row
            .map_err(|e| DaemonRpcError::internal_error(format!("hotspot defect row: {e}")))?;
        defect_map.insert(qn, cnt);
    }

    // 第一轮：原始指标 + 归一化最大值
    let mut raw_list: Vec<(String, String, String, i64, i64, i64, f64, f64)> = Vec::new();
    let mut max_change: i64 = 1;
    let mut max_defect: i64 = 1;
    let mut max_complexity: i64 = 1;
    for (hash, qn, module_o, start_line, end_line, content_o, rel_path_o) in &symbols {
        let (change_count, first_seen, last_changed) = change_map
            .get(hash)
            .copied()
            .unwrap_or((0, 0.0, 0.0));
        let defect_count = defect_map.get(qn).copied().unwrap_or(0);
        let content = content_o.clone().unwrap_or_default();
        let complexity = if !content.is_empty() {
            let lang = rel_path_o
                .as_deref()
                .map(summary_detect_language)
                .unwrap_or_default();
            summary_cyclomatic_complexity(&content, &lang)
        } else if *start_line != 0 && *end_line != 0 && end_line >= start_line {
            end_line - start_line + 1
        } else {
            1
        };
        max_change = max_change.max(change_count);
        max_defect = max_defect.max(defect_count);
        max_complexity = max_complexity.max(complexity);
        raw_list.push((
            hash.clone(),
            qn.clone(),
            module_o.clone().unwrap_or_default(),
            change_count,
            defect_count,
            complexity,
            first_seen,
            last_changed,
        ));
    }

    // 第二轮：归一化评分 + 热点标注
    let mut results: Vec<Value> = raw_list
        .iter()
        .map(|(hash, qn, module, change_count, defect_count, complexity, first_seen, last_changed)| {
            let change_freq = if max_change > 0 {
                *change_count as f64 / max_change as f64
            } else {
                0.0
            };
            let defect_density = if max_defect > 0 {
                *defect_count as f64 / max_defect as f64
            } else {
                0.0
            };
            let cyclo_norm = if max_complexity > 0 {
                *complexity as f64 / max_complexity as f64
            } else {
                0.0
            };
            let hotspot_score = change_freq * 0.4 + defect_density * 0.3 + cyclo_norm * 0.3;
            let days_since = if *last_changed > 0.0 {
                (now - last_changed) / 86400.0
            } else {
                f64::INFINITY
            };
            let label = if *change_count > 5 && days_since <= 30.0 {
                "持续热点"
            } else if (3..=5).contains(change_count) && days_since <= 7.0 {
                "新兴热点"
            } else {
                ""
            };
            json!({
                "qualified_name": qn,
                "symbol_hash": hash,
                "module_path": module,
                "hotspot_score": (hotspot_score * 10000.0).round() / 10000.0,
                "change_count": change_count,
                "defect_count": defect_count,
                "complexity": complexity,
                "first_seen": first_seen,
                "last_changed": last_changed,
                "label": if label.is_empty() { Value::Null } else { json!(label) },
            })
        })
        .collect();
    results.sort_by(|a, b| {
        let sa = a["hotspot_score"].as_f64().unwrap_or(0.0);
        let sb = b["hotspot_score"].as_f64().unwrap_or(0.0);
        sb.partial_cmp(&sa).unwrap_or(std::cmp::Ordering::Equal)
    });
    Ok(json!(results))
}

/// defect_learn —— 复刻 db_defect_kb.learn_defect_from_fix。
/// 只读连接语义：无 qualifying 变更 → 零结果（与 Python 一致）；有 qualifying
/// 变更时 Python 必然走到 INSERT（defect_fixes / defect_patterns）→ worker ro
/// 连接 OperationalError。Rust 同语义 fail-closed（写面不落只读连接）。
pub fn handle_summary_defect_learn(
    conn: &Connection,
    _workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let fix_commit_hash = get_str_param_or(params, "fix_commit_hash", "");
    let mut stmt = conn
        .prepare(
            "SELECT symbol_hash, old_content, new_content \
             FROM git_symbol_changes \
             WHERE commit_hash = ?1 AND change_type = 'modified'",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("defect learn prepare: {e}")))?;
    let rows = stmt
        .query_map([fix_commit_hash], |r| {
            Ok((
                r.get::<_, Option<String>>(0)?,
                r.get::<_, Option<String>>(1)?,
                r.get::<_, Option<String>>(2)?,
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("defect learn: {e}")))?;
    let mut has_qualifying = false;
    for row in rows {
        let (_hash, old_c, new_c) = row
            .map_err(|e| DaemonRpcError::internal_error(format!("defect learn row: {e}")))?;
        if old_c.unwrap_or_default() != new_c.unwrap_or_default() {
            has_qualifying = true;
            break;
        }
    }
    if has_qualifying {
        return Err(DaemonRpcError::internal_error(
            "defect_learn is write-face (INSERT defect_fixes/defect_patterns); \
             read-only snapshot connection rejects it \
             (parity: python worker OperationalError attempt to write a readonly database)"
                .to_string(),
        ));
    }
    Ok(json!({
        "learned_patterns": 0,
        "learned_fixes": 0,
        "details": [],
    }))
}

#[cfg(test)]
mod bootstrap_tests {
    use super::*;

    /// 构造 bootstrap_status 所需的最小库表。
    ///
    /// `workspaces.root_path` 指向一个无 `.git` 的临时目录，使回退分支
    /// （旧客户端）也不会 spawn git —— 从而测试只验证 reported_head 逻辑。
    fn bootstrap_db(scan_head: Option<&str>) -> Connection {
        let conn = Connection::open_in_memory().expect("memory db");
        conn.execute_batch(
            "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, root_path TEXT);
             CREATE TABLE workspace_scan_runs (id INTEGER PRIMARY KEY, workspace_id INTEGER,
                 git_head TEXT, started_at REAL, status TEXT);
             CREATE TABLE agent_rules (status TEXT);
             CREATE TABLE agent_rule_candidates (status TEXT);
             CREATE TABLE task_quality_findings (status TEXT, severity TEXT, workspace_id INTEGER);
             CREATE TABLE tasks (status TEXT);",
        )
        .expect("schema");
        if let Some(head) = scan_head {
            conn.execute(
                "INSERT INTO workspace_scan_runs (workspace_id, git_head, started_at, status) \
                 VALUES (1, ?1, 1.0, 'done')",
                rusqlite::params![head],
            )
            .unwrap();
        }
        let non_git_root = std::env::temp_dir().join("cw_q9_bootstrap_nogit");
        conn.execute(
            "INSERT INTO workspaces (id, root_path) VALUES (1, ?1)",
            rusqlite::params![non_git_root.to_str().unwrap()],
        )
        .unwrap();
        conn
    }

    #[test]
    fn bootstrap_status_uses_reported_head_without_spawning_git() {
        // Q9-VCS：客户端上报 reported_head 时 daemon 直接采用，不再 spawn git
        let conn = bootstrap_db(Some("scan-aaaaaaaa"));
        let res = handle_bootstrap_status(&conn, 1, &json!({}), "reported-head-1234").unwrap();
        assert_eq!(res["current_head"], "reported-head-1234");
        // scan head 与 reported head 不同 → db_stale=true
        assert_eq!(res["db_stale"], true);
    }

    #[test]
    fn bootstrap_reported_head_not_stale_when_matches_scan_head() {
        let conn = bootstrap_db(Some("same-head"));
        let res = handle_bootstrap_status(&conn, 1, &json!({}), "same-head").unwrap();
        assert_eq!(res["current_head"], "same-head");
        assert_eq!(res["db_stale"], false);
    }

    #[test]
    fn bootstrap_status_empty_reported_head_on_non_git_workspace_stays_empty() {
        // 旧客户端未上报 + workspace 非 git 仓库 → current_head 为空，
        // db_stale 恒为 false（与既有 !current_head.is_empty() 语义一致）
        let conn = bootstrap_db(Some("scan-aaaaaaaa"));
        let res = handle_bootstrap_status(&conn, 1, &json!({}), "").unwrap();
        assert_eq!(res["current_head"], "");
        assert_eq!(res["db_stale"], false);
    }
}
