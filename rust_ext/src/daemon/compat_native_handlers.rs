//! compat_native_handlers.rs —— A 类 compat 连锁修复:8 个此前无 daemon 原生
//! handler 的裸名方法迁 rust_native（T-1791357540076-1201cbe0）。
//!
//! 这些方法曾依赖已下线的 Python compat worker（COMPAT_ROUTE_WHITELIST 清零后
//! 恒 method_not_found）。SQL/语义逐条复刻 Python db 层真相源:
//!   - gc_status / gc_restore / gc_purge          ← db/db_gc.py
//!   - get_fts_status                             ← db/db_build.py
//!   - list_destructive_operations                ← db/db_git.py
//!   - get_rollback_config / is_feature_rolled_back ← db/db_rollback_config.py
//!   - build_defect_knowledge                     ← db/db_defect_kb.py
//!
//! 库归属:
//!   - 主库(codegraph,workspace_id 过滤):gc_* / list_destructive_operations /
//!     build_defect_knowledge / get_fts_status(symbols/symbols_fts 全局但同主库)
//!   - 任务库(task DB,全局无 workspace):get_rollback_config / is_feature_rolled_back
//!
//! 写方法(gc_restore/gc_purge/build_defect_knowledge)经调用方 SerializationPoint
//! 串行化;只读方法无副作用。

use rusqlite::{Connection, OptionalExtension};
use serde_json::{json, Map, Value};

use super::dispatch::{get_int_param_or, get_str_param_or, require_str_param, DaemonRpcError};

fn now_ts() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

// ====================================================================
// gc_status —— 复刻 db_gc.gc_status(主库,workspace_id 过滤)
// ====================================================================
/// `gc_status` —— GC 状态统计（活跃/归档/删除文件数 + 归档符号/调用 + 最近归档）。
pub fn handle_gc_status(
    conn: &Connection,
    workspace_id: i64,
    _params: &Value,
) -> Result<Value, DaemonRpcError> {
    let (active, archived, deleted, total): (i64, i64, i64, i64) = conn
        .query_row(
            "SELECT
                COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'archived' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'deleted' THEN 1 ELSE 0 END), 0),
                COUNT(*)
             FROM file_instances WHERE workspace_id = ?1",
            rusqlite::params![workspace_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_status counts: {e}")))?;

    let (archived_symbols, archived_calls): (i64, i64) = conn
        .query_row(
            "SELECT COALESCE(SUM(symbol_count), 0), COALESCE(SUM(call_count), 0)
             FROM archived_files WHERE workspace_id = ?1",
            rusqlite::params![workspace_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap_or((0, 0));

    let mut stmt = conn
        .prepare(
            "SELECT rel_path, archive_reason, archived_at, symbol_count
             FROM archived_files WHERE workspace_id = ?1
             ORDER BY archived_at DESC LIMIT 10",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_status recent prepare: {e}")))?;
    let recent: Vec<Value> = stmt
        .query_map(rusqlite::params![workspace_id], |r| {
            Ok(json!({
                "rel_path": r.get::<_, String>(0)?,
                "archive_reason": r.get::<_, Option<String>>(1)?,
                "archived_at": r.get::<_, Option<f64>>(2)?,
                "symbol_count": r.get::<_, Option<i64>>(3)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_status recent query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_status recent collect: {e}")))?;

    let archive_ratio = if total > 0 {
        archived as f64 / total as f64
    } else {
        0.0
    };
    Ok(json!({
        "active_files": active,
        "archived_files": archived,
        "archived_symbols": archived_symbols,
        "archived_calls": archived_calls,
        "deleted_files": deleted,
        "archive_ratio": archive_ratio,
        "recent_archives": recent,
    }))
}

// ====================================================================
// gc_restore —— 复刻 db_gc.gc_restore(主库写)
// 简化:仅支持 rel_paths 指定复活(全扫 ignore 匹配依赖 Python ignore_spec,
// 不在 Rust 复刻;force 对指定路径生效)。rel_paths 为空时返回 scanned=0。
// ====================================================================
/// `gc_restore` —— 复活归档文件(指定 rel_paths)。status→pending,待下次 build 重解析。
pub fn handle_gc_restore(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let rel_paths: Vec<String> = params
        .get("rel_paths")
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|x| x.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default();

    if rel_paths.is_empty() {
        // 全扫 ignore 匹配依赖 Python ignore matcher,Rust 侧不复刻;
        // 返回明确结果而非静默空(与 Python 全扫语义区分)。
        return Ok(json!({
            "scanned": 0,
            "restored": 0,
            "still_ignored": 0,
            "note": "Rust 原生 gc_restore 仅支持显式 rel_paths;全扫复活请指定路径列表",
        }));
    }

    let placeholders: Vec<&str> = rel_paths.iter().map(|_| "?").collect();
    let sql = format!(
        "SELECT af.id, af.file_instance_id, af.rel_path FROM archived_files af
         WHERE af.workspace_id = ? AND af.rel_path IN ({})",
        placeholders.join(",")
    );
    let mut bind: Vec<rusqlite::types::Value> = vec![rusqlite::types::Value::Integer(workspace_id)];
    for rp in &rel_paths {
        bind.push(rusqlite::types::Value::Text(rp.clone()));
    }
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_restore select prepare: {e}")))?;
    let rows: Vec<(i64, i64)> = stmt
        .query_map(rusqlite::params_from_iter(bind.iter()), |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_restore select: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_restore collect: {e}")))?;

    let scanned = rows.len();
    let mut restored = 0usize;
    for (af_id, fi_id) in rows {
        conn.execute(
            "DELETE FROM archived_files WHERE id = ?1",
            rusqlite::params![af_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_restore delete: {e}")))?;
        conn.execute(
            "UPDATE file_instances SET status = 'pending' WHERE id = ?1 AND workspace_id = ?2",
            rusqlite::params![fi_id, workspace_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_restore update: {e}")))?;
        restored += 1;
    }
    Ok(json!({
        "scanned": scanned,
        "restored": restored,
        "still_ignored": 0,
    }))
}

// ====================================================================
// gc_purge —— 复刻 db_gc.gc_purge(主库写)
// 简化:不写 gc_audit(审计表 _start_gc_audit 逻辑复杂,Rust 侧用 admin.gc_* 审计路径;
// 本方法只做物理清理,返回清理计数)。
// ====================================================================
/// `gc_purge` —— 彻底清除归档超过 older_than_days 天的文件(不可逆)。
pub fn handle_gc_purge(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let older_than_days = get_int_param_or(params, "older_than_days", 30);
    let cutoff = now_ts() - (older_than_days as f64) * 86400.0;

    let mut stmt = conn
        .prepare(
            "SELECT file_instance_id, symbol_count, call_count FROM archived_files
             WHERE workspace_id = ?1 AND archived_at < ?2",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_purge select prepare: {e}")))?;
    let rows: Vec<(i64, i64, i64)> = stmt
        .query_map(rusqlite::params![workspace_id, cutoff], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, Option<i64>>(1)?.unwrap_or(0),
                r.get::<_, Option<i64>>(2)?.unwrap_or(0),
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_purge select: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_purge collect: {e}")))?;

    if rows.is_empty() {
        return Ok(json!({
            "purged_files": 0, "purged_symbols": 0, "purged_calls": 0,
        }));
    }
    let fi_ids: Vec<i64> = rows.iter().map(|r| r.0).collect();
    let purged_symbols: i64 = rows.iter().map(|r| r.1).sum();
    let purged_calls: i64 = rows.iter().map(|r| r.2).sum();
    let placeholders: Vec<&str> = fi_ids.iter().map(|_| "?").collect();
    let ph = placeholders.join(",");
    let bind: Vec<rusqlite::types::Value> = fi_ids
        .iter()
        .map(|&i| rusqlite::types::Value::Integer(i))
        .collect();
    conn.execute(
        &format!("DELETE FROM archived_files WHERE file_instance_id IN ({ph})"),
        rusqlite::params_from_iter(bind.iter()),
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("gc_purge del archived: {e}")))?;
    conn.execute(
        &format!("DELETE FROM file_instances WHERE id IN ({ph})"),
        rusqlite::params_from_iter(bind.iter()),
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("gc_purge del fi: {e}")))?;

    Ok(json!({
        "purged_files": fi_ids.len(),
        "purged_symbols": purged_symbols,
        "purged_calls": purged_calls,
    }))
}

// ====================================================================
// get_fts_status —— 复刻 db_build.get_fts_status(主库,全局 symbols/symbols_fts)
// ====================================================================
/// `get_fts_status` —— FTS5 索引状态(存在性/行数一致性/触发器)。
pub fn handle_get_fts_status(conn: &Connection, _params: &Value) -> Result<Value, DaemonRpcError> {
    let exists: bool = conn
        .query_row(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='symbols_fts'",
            [],
            |_| Ok(true),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("fts exists: {e}")))?
        .unwrap_or(false);
    if !exists {
        return Ok(json!({
            "exists": false, "symbols_count": 0, "fts_rows": 0,
            "triggers": [], "consistent": false,
        }));
    }
    let symbols_count: i64 = conn
        .query_row("SELECT COUNT(*) FROM symbols", [], |r| r.get(0))
        .unwrap_or(0);
    let fts_rows: i64 = conn
        .query_row("SELECT COUNT(*) FROM symbols_fts", [], |r| r.get(0))
        .unwrap_or(0);
    let mut stmt = conn
        .prepare(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'symbols_fts_%'",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("fts triggers prepare: {e}")))?;
    let triggers: Vec<String> = stmt
        .query_map([], |r| r.get::<_, String>(0))
        .map_err(|e| DaemonRpcError::internal_error(format!("fts triggers query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("fts triggers collect: {e}")))?;
    Ok(json!({
        "exists": true,
        "symbols_count": symbols_count,
        "fts_rows": fts_rows,
        "triggers": triggers,
        "consistent": fts_rows == symbols_count,
    }))
}

// ====================================================================
// list_destructive_operations —— 复刻 db_git.list_destructive_operations(主库)
// ====================================================================
/// `list_destructive_operations` —— 破坏性 git 操作历史(workspace_id 过滤,时间倒序)。
pub fn handle_list_destructive_operations(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let limit = get_int_param_or(params, "limit", 20).max(0);
    let operation_type = get_str_param_or(params, "operation_type", "");

    let col_names: Vec<String> = {
        let stmt = conn
            .prepare("SELECT * FROM destructive_operations LIMIT 0")
            .map_err(|e| DaemonRpcError::internal_error(format!("destructive cols: {e}")))?;
        stmt.column_names().iter().map(|s| s.to_string()).collect()
    };

    let row_to_json = move |row: &rusqlite::Row<'_>| -> rusqlite::Result<Value> {
        let mut m = Map::new();
        for (i, name) in col_names.iter().enumerate() {
            let v = row.get_ref(i)?;
            let jv = match v {
                rusqlite::types::ValueRef::Null => Value::Null,
                rusqlite::types::ValueRef::Integer(n) => Value::Number(n.into()),
                rusqlite::types::ValueRef::Real(f) => serde_json::Number::from_f64(f)
                    .map(Value::Number)
                    .unwrap_or(Value::Null),
                rusqlite::types::ValueRef::Text(t) => {
                    Value::String(String::from_utf8_lossy(t).to_string())
                }
                rusqlite::types::ValueRef::Blob(_) => Value::Null,
            };
            m.insert(name.clone(), jv);
        }
        Ok(Value::Object(m))
    };

    // 规则36：query_map 结果（MappedRows 借用 Statement）必须先绑定为局部变量，
    // 不能作块尾临时值直接返回，否则 Statement 先于 MappedRows 析构触发 E0597。
    let rows: Vec<Value> = if operation_type.is_empty() {
        let mut stmt = conn
            .prepare(
                "SELECT * FROM destructive_operations WHERE workspace_id = ?1
                 ORDER BY created_at DESC LIMIT ?2",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("destructive prepare: {e}")))?;
        let collected = stmt
            .query_map(rusqlite::params![workspace_id, limit], row_to_json)
            .map_err(|e| DaemonRpcError::internal_error(format!("destructive query: {e}")))?
            .collect::<Result<Vec<_>, rusqlite::Error>>()
            .map_err(|e| DaemonRpcError::internal_error(format!("destructive collect: {e}")))?;
        collected
    } else {
        let mut stmt = conn
            .prepare(
                "SELECT * FROM destructive_operations WHERE workspace_id = ?1 AND operation_type = ?2
                 ORDER BY created_at DESC LIMIT ?3",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("destructive prepare: {e}")))?;
        let collected = stmt
            .query_map(
                rusqlite::params![workspace_id, operation_type, limit],
                row_to_json,
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("destructive query: {e}")))?
            .collect::<Result<Vec<_>, rusqlite::Error>>()
            .map_err(|e| DaemonRpcError::internal_error(format!("destructive collect: {e}")))?;
        collected
    };
    Ok(Value::Array(rows))
}

// ====================================================================
// get_rollback_config / is_feature_rolled_back —— 复刻 db_rollback_config.py
// (任务库,全局无 workspace 过滤)
// ====================================================================
/// `get_rollback_config` —— 查询单任务回滚配置(task 库,config_blob 反序列化)。
pub fn handle_get_rollback_config(
    conn: &Connection,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let task_id = require_str_param(params, "task_id")?;
    let col_names: Vec<String> = {
        let stmt = conn
            .prepare("SELECT * FROM rollback_config LIMIT 0")
            .map_err(|e| DaemonRpcError::internal_error(format!("rollback cols: {e}")))?;
        stmt.column_names().iter().map(|s| s.to_string()).collect()
    };
    let mut stmt = conn
        .prepare("SELECT * FROM rollback_config WHERE task_id = ?1")
        .map_err(|e| DaemonRpcError::internal_error(format!("rollback prepare: {e}")))?;
    let row = stmt
        .query_row(rusqlite::params![task_id], |row| {
            let mut m = Map::new();
            for (i, name) in col_names.iter().enumerate() {
                let v = row.get_ref(i)?;
                let jv = match v {
                    rusqlite::types::ValueRef::Null => Value::Null,
                    rusqlite::types::ValueRef::Integer(n) => Value::Number(n.into()),
                    rusqlite::types::ValueRef::Real(f) => serde_json::Number::from_f64(f)
                        .map(Value::Number)
                        .unwrap_or(Value::Null),
                    rusqlite::types::ValueRef::Text(t) => {
                        Value::String(String::from_utf8_lossy(t).to_string())
                    }
                    rusqlite::types::ValueRef::Blob(_) => Value::Null,
                };
                m.insert(name.clone(), jv);
            }
            Ok(Value::Object(m))
        })
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("rollback query: {e}")))?;
    match row {
        Some(mut v) => {
            // config_blob 反序列化(与 Python 一致)
            if let Some(blob) = v.get("config_blob").and_then(Value::as_str) {
                if let Ok(parsed) = serde_json::from_str::<Value>(blob) {
                    if let Value::Object(ref mut m) = v {
                        m.insert("config_blob".into(), parsed);
                    }
                }
            }
            Ok(v)
        }
        None => Ok(Value::Null),
    }
}

/// `is_feature_rolled_back` —— 功能是否已回滚(task 库,最新 updated_at 的 rollback_flag==1)。
pub fn handle_is_feature_rolled_back(
    conn: &Connection,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let feature_name = get_str_param_or(params, "feature_name", "");
    if feature_name.is_empty() {
        return Ok(json!(false));
    }
    let flag: Option<i64> = conn
        .query_row(
            "SELECT rollback_flag FROM rollback_config WHERE feature_name = ?1
             ORDER BY updated_at DESC LIMIT 1",
            rusqlite::params![feature_name],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("is_rolled_back query: {e}")))?;
    Ok(json!(flag == Some(1)))
}

// ====================================================================
// build_defect_knowledge —— 复刻 db_defect_kb.build_defect_knowledge(主库写)
// 简化:只做 pattern 挖掘(按 rule_id 分组 → defect_patterns upsert),不做
// defect_fixes 关联(需 git_symbol_changes 复杂关联,Python 侧亦为增量;
// 返回 fixes_learned=0 并注明)。
// ====================================================================
/// `build_defect_knowledge` —— 从 semgrep_findings 挖掘缺陷模式(按 rule_id 分组)。
pub fn handle_build_defect_knowledge(
    conn: &Connection,
    _workspace_id: i64,
    _params: &Value,
) -> Result<Value, DaemonRpcError> {
    let now = now_ts();
    let mut stmt = conn
        .prepare(
            "SELECT rule_id, MAX(rule_name), MAX(message), MAX(severity), COUNT(*)
             FROM semgrep_findings
             WHERE rule_id IS NOT NULL AND rule_id != ''
             GROUP BY rule_id",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("defect group prepare: {e}")))?;
    let rows: Vec<(String, Option<String>, Option<String>, Option<String>, i64)> = stmt
        .query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, Option<String>>(1)?,
                r.get::<_, Option<String>>(2)?,
                r.get::<_, Option<String>>(3)?,
                r.get::<_, i64>(4)?,
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("defect group query: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("defect group collect: {e}")))?;

    let mut patterns_built = 0usize;
    let mut categories: Map<String, Value> = Map::new();
    for (rule_id, rule_name, message, severity, case_count) in rows {
        let category = extract_category(&rule_id);
        let severity = normalize_severity(severity.as_deref().unwrap_or("info"));
        let mut description = message
            .filter(|m| !m.trim().is_empty())
            .or(rule_name)
            .unwrap_or_else(|| rule_id.clone());
        if description.chars().count() > 500 {
            description = description.chars().take(500).collect::<String>() + "...";
        }
        let pattern_id = format!("DP-{rule_id}");
        let cat_count = categories
            .get(&category)
            .and_then(Value::as_i64)
            .unwrap_or(0);
        categories.insert(category.clone(), Value::Number((cat_count + 1).into()));

        // upsert defect_patterns
        let existing: Option<String> = conn
            .query_row(
                "SELECT pattern_id FROM defect_patterns WHERE pattern_id = ?1",
                rusqlite::params![pattern_id],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("defect existing: {e}")))?;
        if existing.is_some() {
            conn.execute(
                "UPDATE defect_patterns SET case_count = ?1, severity = ?2,
                 description = ?3, updated_at = ?4 WHERE pattern_id = ?5",
                rusqlite::params![case_count, severity, description, now, pattern_id],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("defect update: {e}")))?;
        } else {
            conn.execute(
                "INSERT INTO defect_patterns
                 (pattern_id, category, description, detection_rule, severity, case_count, created_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                rusqlite::params![pattern_id, category, description, rule_id, severity, case_count, now, now],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("defect insert: {e}")))?;
            patterns_built += 1;
        }
    }
    Ok(json!({
        "patterns_built": patterns_built,
        "fixes_learned": 0,
        "categories": Value::Object(categories),
        "note": "Rust 原生实现:仅 pattern 挖掘;defect_fixes 关联(git_symbol_changes)未复刻",
    }))
}

/// 已知缺陷类别关键词(复刻 db_defect_kb._CATEGORY_KEYWORDS)。
const CATEGORY_KEYWORDS: &[&str] = &[
    "security",
    "correctness",
    "best-practice",
    "best-practices",
    "performance",
    "maintainability",
    "portability",
    "accessibility",
];

/// 复刻 db_defect_kb._extract_category:
/// 1. 按 [./] 分隔 → 优先匹配已知类别关键词;2. 否则取第 3 段;3. 否则最后一段。
/// 空 rule_id → "general"。
fn extract_category(rule_id: &str) -> String {
    if rule_id.is_empty() {
        return "general".to_string();
    }
    let parts: Vec<&str> = rule_id.split(['.', '/']).collect();
    for part in &parts {
        let pl = part.to_lowercase();
        if CATEGORY_KEYWORDS.contains(&pl.as_str()) {
            return pl;
        }
    }
    if parts.len() >= 3 {
        return parts[2].to_lowercase();
    }
    parts
        .last()
        .map(|s| s.to_lowercase())
        .unwrap_or_else(|| "general".to_string())
}

/// 复刻 db_defect_kb._normalize_severity:sev.lower().strip(),空 → "info"。
fn normalize_severity(sev: &str) -> String {
    let trimmed = sev.trim();
    if trimmed.is_empty() {
        return "info".to_string();
    }
    trimmed.to_lowercase()
}
