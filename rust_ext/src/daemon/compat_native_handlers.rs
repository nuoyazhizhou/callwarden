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
use sha2::{Digest, Sha256};

use super::dispatch::{get_int_param_or, get_str_param_or, require_str_param, DaemonRpcError};
use crate::cli::status::{load_ignore_patterns, should_ignore};

fn now_ts() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 开始一次 GC 审计记录（复刻 db_gc._start_gc_audit），返回 gc_runs.id。
/// policy 以 JSON object 落 policy_json（sort_keys 对齐 Python 的 sort_keys=True）。
fn start_gc_audit(
    conn: &Connection,
    workspace_id: i64,
    operation: &str,
    dry_run: bool,
    policy: &Value,
) -> Result<i64, DaemonRpcError> {
    // serde_json 的 Map 默认不保证 key 顺序；为对齐 Python json.dumps(sort_keys=True)，
    // 这里对 object 的 key 排序后重新组装再序列化。
    let policy_json = canonical_json_sorted(policy);
    conn.execute(
        "INSERT INTO gc_runs
           (workspace_id, operation, dry_run, policy_json,
            candidate_counts, deleted_counts, backup_path, backup_size,
            started_at, completed_at, status, error, operator)
         VALUES (?1, ?2, ?3, ?4, '{}', '{}', '', 0, ?5, NULL, 'running', '', ?6)",
        rusqlite::params![
            workspace_id,
            operation,
            if dry_run { 1 } else { 0 },
            policy_json,
            now_ts(),
            "mcp"
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("start_gc_audit insert: {e}")))?;
    Ok(conn.last_insert_rowid())
}

/// 标记 GC 审计完成（复刻 db_gc._complete_gc_audit）。
fn complete_gc_audit(
    conn: &Connection,
    audit_id: i64,
    candidate_counts: &Value,
    deleted_counts: &Value,
) -> Result<(), DaemonRpcError> {
    conn.execute(
        "UPDATE gc_runs SET
           candidate_counts = ?1, deleted_counts = ?2,
           backup_path = '', backup_size = 0,
           completed_at = ?3, status = 'completed', error = ''
         WHERE id = ?4",
        rusqlite::params![
            canonical_json_sorted(candidate_counts),
            canonical_json_sorted(deleted_counts),
            now_ts(),
            audit_id
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("complete_gc_audit update: {e}")))?;
    Ok(())
}

/// 标记 GC 审计失败（复刻 db_gc._fail_gc_audit）。error 截断到 2000 字符。
fn fail_gc_audit(conn: &Connection, audit_id: i64, error: &str) {
    let mut msg = if error.is_empty() {
        "unknown error".to_string()
    } else {
        error.to_string()
    };
    if msg.chars().count() > 2000 {
        msg = msg.chars().take(2000).collect::<String>() + "...(truncated)";
    }
    // 失败路径尽力而为：审计写失败不再向上抛（避免掩盖原始错误）。
    let _ = conn.execute(
        "UPDATE gc_runs SET completed_at = ?1, status = 'failed', error = ?2 WHERE id = ?3",
        rusqlite::params![now_ts(), msg, audit_id],
    );
}

/// 将 JSON object 的 key 排序后序列化（对齐 Python json.dumps(sort_keys=True)）。
/// 非 object 直接序列化。顶层 object 的一层 key 排序足以覆盖 gc 审计的扁平 counts/policy。
fn canonical_json_sorted(v: &Value) -> String {
    match v {
        Value::Object(m) => {
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            let mut sorted = Map::new();
            for k in keys {
                sorted.insert(k.clone(), m[k].clone());
            }
            serde_json::to_string(&Value::Object(sorted)).unwrap_or_else(|_| "{}".to_string())
        }
        _ => serde_json::to_string(v).unwrap_or_else(|_| "{}".to_string()),
    }
}

/// 查询 workspace 根目录路径（复刻 cli/external.rs:workspace_root 的查询语义）。
/// 用于 gc_restore 全扫时加载 .gitignore/.callwardenignore 规则。
fn query_workspace_root(conn: &Connection, workspace_id: i64) -> Option<std::path::PathBuf> {
    conn.query_row(
        "SELECT root_path FROM workspaces WHERE id = ?1",
        rusqlite::params![workspace_id],
        |r| r.get::<_, String>(0),
    )
    .optional()
    .ok()
    .flatten()
    .filter(|s| !s.is_empty())
    .map(std::path::PathBuf::from)
}

/// gc_restore 全扫的 ignore 判断。先复用 cli/status.rs 的 should_ignore（与
/// cli/external.rs:run_gc_restore 同源的 fnmatch 语义），再补齐 Python
/// IgnoreMatcher 的目录级语义：以 '/' 结尾的 dir-only pattern（如 ".git/"、
/// "node_modules/"）应忽略该目录下**所有文件**，而 should_ignore 对 dir-only
/// pattern 仅匹配目录本身（is_dir=true）。这里对文件路径补一个"某级目录段 ==
/// dir pattern"的前缀判断，使 ".git/config" 这类归档文件在全扫时正确保持忽略。
fn path_is_ignored_for_restore(rel_path: &str, patterns: &[String]) -> bool {
    if should_ignore(rel_path, false, patterns) {
        return true;
    }
    // dir-only pattern 的目录前缀匹配：pattern "foo/" 命中 "foo/..." 任意深度。
    let segments: Vec<&str> = rel_path.split('/').collect();
    for pattern in patterns {
        if !pattern.ends_with('/') {
            continue;
        }
        let dir = pattern.trim_end_matches('/').trim_start_matches('/');
        if dir.is_empty() || dir.contains('/') {
            // 含子路径的 dir pattern（如 "a/b/"）走 should_ignore 的既有分支，这里只处理单段目录名。
            continue;
        }
        // rel_path 的任一非末段（即目录段）等于该 dir 名 → 该文件在被忽略目录下。
        if segments.len() > 1 && segments[..segments.len() - 1].iter().any(|s| *s == dir) {
            return true;
        }
    }
    false
}

/// 计算内容 hash（复刻 config.compute_content_hash）：
/// sha256(norm_newlines(content)) 的小写 hex。norm_newlines 把 \r\n 和 \r
/// 统一为 \n（与 query_compat_handlers.rs:summary_read_file_normalized 一致）。
fn compute_content_hash(content: &str) -> String {
    let normalized = content.replace("\r\n", "\n").replace('\r', "\n");
    let mut hasher = Sha256::new();
    hasher.update(normalized.as_bytes());
    let digest = hasher.finalize();
    let mut out = String::with_capacity(64);
    for b in digest {
        out.push_str(&format!("{b:02x}"));
    }
    out
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
    let force = params
        .get("force")
        .and_then(Value::as_bool)
        .unwrap_or(false);

    // 复刻 db_gc.gc_restore：
    //   - rel_paths 非空 → 仅复活指定路径（JOIN file_instances 过滤）
    //   - rel_paths 为空 → 全扫该 workspace 全部 archived_files
    //   - 非 force 时用 ignore matcher 判断是否仍被忽略；仍命中则跳过（still_ignored++）
    // ignore 判断复用 cli/status.rs 的 load_ignore_patterns + should_ignore
    //（与 cli/external.rs:run_gc_restore 同一真相源）。
    let (rows, scanned_all): (Vec<(i64, i64, String)>, usize) = if rel_paths.is_empty() {
        let mut stmt = conn
            .prepare(
                "SELECT id, file_instance_id, rel_path FROM archived_files WHERE workspace_id = ?1",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("gc_restore scan prepare: {e}")))?;
        let collected = stmt
            .query_map(rusqlite::params![workspace_id], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, String>(2)?,
                ))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("gc_restore scan: {e}")))?
            .collect::<Result<Vec<_>, rusqlite::Error>>()
            .map_err(|e| DaemonRpcError::internal_error(format!("gc_restore scan collect: {e}")))?;
        let n = collected.len();
        (collected, n)
    } else {
        let placeholders: Vec<&str> = rel_paths.iter().map(|_| "?").collect();
        let sql = format!(
            "SELECT af.id, af.file_instance_id, af.rel_path FROM archived_files af
             JOIN file_instances fi ON af.file_instance_id = fi.id
             WHERE af.workspace_id = ? AND af.rel_path IN ({})",
            placeholders.join(",")
        );
        let mut bind: Vec<rusqlite::types::Value> =
            vec![rusqlite::types::Value::Integer(workspace_id)];
        for rp in &rel_paths {
            bind.push(rusqlite::types::Value::Text(rp.clone()));
        }
        let mut stmt = conn.prepare(&sql).map_err(|e| {
            DaemonRpcError::internal_error(format!("gc_restore select prepare: {e}"))
        })?;
        let collected = stmt
            .query_map(rusqlite::params_from_iter(bind.iter()), |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, String>(2)?,
                ))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("gc_restore select: {e}")))?
            .collect::<Result<Vec<_>, rusqlite::Error>>()
            .map_err(|e| DaemonRpcError::internal_error(format!("gc_restore collect: {e}")))?;
        let n = collected.len();
        (collected, n)
    };

    // 加载 ignore 规则（非 force 时才需要）。workspace root 不可解析时降级为空规则集：
    // 空规则集下 should_ignore 恒 false，等价于 force 行为的安全侧（复活而非误判忽略）。
    let patterns = if force {
        Vec::new()
    } else {
        query_workspace_root(conn, workspace_id)
            .map(|root| load_ignore_patterns(&root))
            .unwrap_or_default()
    };

    let mut restored = 0usize;
    let mut still_ignored = 0usize;
    for (af_id, fi_id, rel_path) in rows {
        if !force && path_is_ignored_for_restore(&rel_path, &patterns) {
            still_ignored += 1;
            continue;
        }
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
        "scanned": scanned_all,
        "restored": restored,
        "still_ignored": still_ignored,
    }))
}

// ====================================================================
// gc_purge —— 复刻 db_gc.gc_purge(主库写)
// 简化:不写 gc_audit(审计表 _start_gc_audit 逻辑复杂,Rust 侧用 admin.gc_* 审计路径;
// 本方法只做物理清理,返回清理计数)。
// ====================================================================
/// `gc_purge` —— 彻底清除归档超过 older_than_days 天的文件(不可逆)。
/// 复刻 db_gc.gc_purge：全程写 gc_runs 审计（start running → complete/fail），
/// 返回值包含 audit_id 便于追溯（与 Python 一致）。
pub fn handle_gc_purge(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let older_than_days = get_int_param_or(params, "older_than_days", 30);
    let cutoff = now_ts() - (older_than_days as f64) * 86400.0;

    // 1. 开审计记录（operation=purge, dry_run=false, policy={older_than_days}）
    let audit_id = start_gc_audit(
        conn,
        workspace_id,
        "purge",
        false,
        &json!({ "older_than_days": older_than_days }),
    )?;

    // 2. 执行清理；任一步失败则 fail 审计并上抛（审计不吞原始错误）
    let result = gc_purge_inner(conn, workspace_id, cutoff, audit_id);
    match result {
        Ok(v) => Ok(v),
        Err(e) => {
            fail_gc_audit(conn, audit_id, &format!("{e:?}"));
            Err(e)
        }
    }
}

/// gc_purge 的实际清理逻辑（成功时写 complete 审计）。拆出以便统一 fail 审计。
fn gc_purge_inner(
    conn: &Connection,
    workspace_id: i64,
    cutoff: f64,
    audit_id: i64,
) -> Result<Value, DaemonRpcError> {
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
        complete_gc_audit(
            conn,
            audit_id,
            &json!({ "archived_files": 0 }),
            &json!({ "purged_files": 0, "purged_symbols": 0, "purged_calls": 0 }),
        )?;
        return Ok(json!({
            "audit_id": audit_id,
            "purged_files": 0, "purged_symbols": 0, "purged_calls": 0,
        }));
    }
    let fi_ids: Vec<i64> = rows.iter().map(|r| r.0).collect();
    let purged_symbols: i64 = rows.iter().map(|r| r.1).sum();
    let purged_calls: i64 = rows.iter().map(|r| r.2).sum();
    let candidate_files = rows.len();
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

    complete_gc_audit(
        conn,
        audit_id,
        &json!({ "archived_files": candidate_files }),
        &json!({
            "purged_files": fi_ids.len(),
            "purged_symbols": purged_symbols,
            "purged_calls": purged_calls,
        }),
    )?;

    Ok(json!({
        "audit_id": audit_id,
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

    // ---- 2. 从 git_symbol_changes 挖掘修复案例（复刻 db_defect_kb 第 2 段）----
    // 遍历 change_type='modified' 且 old/new 内容齐全的符号变更：
    //   - 定位该符号 content_hash → qualified_name（symbol_contents）
    //   - 找出相关 semgrep_findings（按 symbol_qualified 优先，否则按 content_hash）
    //   - finding.snippet 出现在 old、不出现在 new → 判定为"修复"
    //   - ensure DP-{rule_id} 模式（learned_from='git_fix'），插入 defect_fixes（去重）
    // 这里不复刻 Python 的 N+1 批量优化，改为逐行查询，语义等价、结果一致。
    let fixes_learned = build_defect_fixes(conn, now)?;

    Ok(json!({
        "patterns_built": patterns_built,
        "fixes_learned": fixes_learned,
        "categories": Value::Object(categories),
    }))
}

/// 从 git_symbol_changes 挖掘并写入 defect_fixes（复刻 db_defect_kb.build_defect_knowledge
/// 第 2 段的语义），返回新插入的 fix 数量。
fn build_defect_fixes(conn: &Connection, now: f64) -> Result<usize, DaemonRpcError> {
    // 拉取所有 modified 变更（old/new 齐全）
    let changes: Vec<(String, String, String)> = {
        let mut stmt = conn
            .prepare(
                "SELECT symbol_hash, old_content, new_content FROM git_symbol_changes
                 WHERE change_type = 'modified'
                   AND old_content IS NOT NULL AND new_content IS NOT NULL",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("defect changes prepare: {e}")))?;
        let collected = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                    r.get::<_, Option<String>>(2)?.unwrap_or_default(),
                ))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("defect changes query: {e}")))?
            .collect::<Result<Vec<_>, rusqlite::Error>>()
            .map_err(|e| DaemonRpcError::internal_error(format!("defect changes collect: {e}")))?;
        collected
    };

    // 去重键集合（pattern_id, symbol_hash, before_hash, after_hash），对齐 Python
    // dup_keys（本轮内去重）+ defect_fixes 已有记录去重。
    let mut seen: std::collections::HashSet<(String, String, String, String)> =
        std::collections::HashSet::new();
    let mut inserted = 0usize;

    for (symbol_hash, old_content, new_content) in &changes {
        // symbol_hash → qualified_name（symbol_contents.content_hash）
        let qualified_name: Option<String> = conn
            .query_row(
                "SELECT qualified_name FROM symbol_contents WHERE content_hash = ?1",
                rusqlite::params![symbol_hash],
                |r| r.get::<_, Option<String>>(0),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("defect qname: {e}")))?
            .flatten()
            .filter(|s| !s.is_empty());

        // 相关 findings：有 qualified_name 按 symbol_qualified 查；否则按 content_hash 查。
        // 两条分支字段对齐 Python（rule_id, snippet, fix）。
        let findings: Vec<(String, String, String)> = if let Some(ref qn) = qualified_name {
            let mut stmt = conn
                .prepare(
                    "SELECT rule_id, snippet, fix FROM semgrep_findings
                     WHERE symbol_qualified = ?1",
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("defect findings(q) prepare: {e}"))
                })?;
            let collected = stmt
                .query_map(rusqlite::params![qn], |r| {
                    Ok((
                        r.get::<_, Option<String>>(0)?.unwrap_or_default(),
                        r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                        r.get::<_, Option<String>>(2)?.unwrap_or_default(),
                    ))
                })
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("defect findings(q) query: {e}"))
                })?
                .collect::<Result<Vec<_>, rusqlite::Error>>()
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("defect findings(q) collect: {e}"))
                })?;
            collected
        } else {
            let mut stmt = conn
                .prepare(
                    "SELECT rule_id, snippet, fix FROM semgrep_findings
                     WHERE content_hash = ?1",
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("defect findings(h) prepare: {e}"))
                })?;
            let collected = stmt
                .query_map(rusqlite::params![symbol_hash], |r| {
                    Ok((
                        r.get::<_, Option<String>>(0)?.unwrap_or_default(),
                        r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                        r.get::<_, Option<String>>(2)?.unwrap_or_default(),
                    ))
                })
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("defect findings(h) query: {e}"))
                })?
                .collect::<Result<Vec<_>, rusqlite::Error>>()
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("defect findings(h) collect: {e}"))
                })?;
            collected
        };

        for (rule_id, snippet, fix) in findings {
            if rule_id.is_empty() {
                continue;
            }
            // snippet 在 old、不在 new → 修复（复刻 _snippet_in_content：strip 后子串匹配）
            let in_old = snippet_in_content(&snippet, old_content);
            let in_new = snippet_in_content(&snippet, new_content);
            if !in_old || in_new {
                continue;
            }
            let pattern_id = format!("DP-{rule_id}");
            // ensure DP-{rule_id}（learned_from='git_fix'），不存在才插入
            ensure_pattern_git_fix(
                conn,
                &pattern_id,
                &extract_category(&rule_id),
                // description 优先 fix，其次 snippet，再次 rule_id（对齐 Python）
                if !fix.is_empty() {
                    &fix
                } else if !snippet.is_empty() {
                    &snippet
                } else {
                    &rule_id
                },
                &rule_id,
            )?;

            let before_hash = compute_content_hash(old_content);
            let after_hash = compute_content_hash(new_content);
            let dup_key = (
                pattern_id.clone(),
                symbol_hash.clone(),
                before_hash.clone(),
                after_hash.clone(),
            );
            if seen.contains(&dup_key) {
                continue;
            }
            seen.insert(dup_key);

            // defect_fixes 已有同键记录则跳过（对齐 Python 的持久化去重）
            let exists: Option<i64> = conn
                .query_row(
                    "SELECT 1 FROM defect_fixes
                     WHERE pattern_id = ?1 AND symbol_hash = ?2
                       AND before_hash = ?3 AND after_hash = ?4",
                    rusqlite::params![pattern_id, symbol_hash, before_hash, after_hash],
                    |r| r.get(0),
                )
                .optional()
                .map_err(|e| DaemonRpcError::internal_error(format!("defect fix dup: {e}")))?;
            if exists.is_some() {
                continue;
            }

            let fix_diff = compute_unified_diff(old_content, new_content);
            conn.execute(
                "INSERT INTO defect_fixes
                   (pattern_id, symbol_hash, before_hash, after_hash, fix_diff,
                    effectiveness, created_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                rusqlite::params![
                    pattern_id,
                    symbol_hash,
                    before_hash,
                    after_hash,
                    fix_diff,
                    0.8_f64,
                    now
                ],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("defect fix insert: {e}")))?;
            inserted += 1;
        }
    }
    Ok(inserted)
}

/// 复刻 db_defect_kb._snippet_in_content：snippet.strip() 作为子串在 content 中出现。
/// snippet/content 任一为空 → false。
fn snippet_in_content(snippet: &str, content: &str) -> bool {
    if snippet.is_empty() || content.is_empty() {
        return false;
    }
    content.contains(snippet.trim())
}

/// 确保 defect_patterns 存在 git_fix 来源记录（复刻 db_defect_kb._ensure_pattern，
/// learned_from='git_fix'）。不存在才 INSERT OR IGNORE，case_count=0。
fn ensure_pattern_git_fix(
    conn: &Connection,
    pattern_id: &str,
    category: &str,
    description: &str,
    detection_rule: &str,
) -> Result<(), DaemonRpcError> {
    let exists: Option<i64> = conn
        .query_row(
            "SELECT 1 FROM defect_patterns WHERE pattern_id = ?1",
            rusqlite::params![pattern_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("ensure_pattern select: {e}")))?;
    if exists.is_some() {
        return Ok(());
    }
    // 描述截断到 500 字符（与 pattern 挖掘段一致，避免存储爆炸）
    let desc = if description.chars().count() > 500 {
        description.chars().take(500).collect::<String>() + "..."
    } else {
        description.to_string()
    };
    conn.execute(
        "INSERT OR IGNORE INTO defect_patterns
           (pattern_id, category, description, detection_rule, fix_template,
            severity, learned_from, case_count, created_at)
         VALUES (?1, ?2, ?3, ?4, '', 'info', 'git_fix', 0, ?5)",
        rusqlite::params![pattern_id, category, desc, detection_rule, now_ts()],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("ensure_pattern insert: {e}")))?;
    Ok(())
}

/// 生成 unified-diff 风格文本（存档展示用，不参与去重/匹配逻辑）。
/// 复刻 db_defect_kb._compute_diff 的意图（Python 用 difflib.unified_diff，
/// fromfile=before/tofile=after）：这里产出等价可读的 --- /+++ /@@ 结构，
/// 行级增删标记对齐 unified diff 约定。old/new 均空 → 空串。
fn compute_unified_diff(old: &str, new: &str) -> String {
    if old.is_empty() && new.is_empty() {
        return String::new();
    }
    let old_lines: Vec<&str> = old.lines().collect();
    let new_lines: Vec<&str> = new.lines().collect();
    let mut out = String::new();
    out.push_str("--- before\n");
    out.push_str("+++ after\n");
    out.push_str(&format!(
        "@@ -1,{} +1,{} @@\n",
        old_lines.len(),
        new_lines.len()
    ));
    // 简化行级 diff：公共前缀保留为上下文，剩余 old 行标 '-'、new 行标 '+'。
    let common_prefix = old_lines
        .iter()
        .zip(new_lines.iter())
        .take_while(|(a, b)| a == b)
        .count();
    for line in &old_lines[..common_prefix] {
        out.push(' ');
        out.push_str(line);
        out.push('\n');
    }
    for line in &old_lines[common_prefix..] {
        out.push('-');
        out.push_str(line);
        out.push('\n');
    }
    for line in &new_lines[common_prefix..] {
        out.push('+');
        out.push_str(line);
        out.push('\n');
    }
    out
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

// ====================================================================
// 单元测试：A 类 compat 补全（gc_restore 全扫 / gc_purge 审计 /
// build_defect_knowledge 的 fixes 关联）。使用 in-memory SQLite 构造 fixture，
// 不触碰权威库（符合 AGENTS §34）。
// ====================================================================
#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    /// 建最小 schema（仅测试用到的表/列，字段名与 db/schema.py 对齐）。
    fn setup_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "
            CREATE TABLE workspaces (id INTEGER PRIMARY KEY, root_path TEXT);
            CREATE TABLE file_instances (
                id INTEGER PRIMARY KEY, workspace_id INTEGER, status TEXT);
            CREATE TABLE archived_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT, file_instance_id INTEGER,
                workspace_id INTEGER, rel_path TEXT, abs_path TEXT,
                content_hash TEXT, symbol_count INTEGER, call_count INTEGER,
                archive_reason TEXT, archived_at REAL);
            CREATE TABLE gc_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER,
                operation TEXT NOT NULL, dry_run INTEGER NOT NULL DEFAULT 0,
                policy_json TEXT DEFAULT '', candidate_counts TEXT DEFAULT '{}',
                deleted_counts TEXT DEFAULT '{}', backup_path TEXT DEFAULT '',
                backup_size INTEGER DEFAULT 0, started_at REAL NOT NULL,
                completed_at REAL, status TEXT NOT NULL DEFAULT 'running',
                error TEXT DEFAULT '', operator TEXT DEFAULT 'cli');
            CREATE TABLE symbol_contents (
                content_hash TEXT PRIMARY KEY, qualified_name TEXT);
            CREATE TABLE semgrep_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT, rule_name TEXT,
                message TEXT, severity TEXT, snippet TEXT, fix TEXT,
                content_hash TEXT, symbol_qualified TEXT);
            CREATE TABLE git_symbol_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, commit_hash TEXT,
                symbol_hash TEXT, change_type TEXT, old_content TEXT, new_content TEXT);
            CREATE TABLE defect_patterns (
                pattern_id TEXT PRIMARY KEY, category TEXT, description TEXT,
                detection_rule TEXT, fix_template TEXT, severity TEXT,
                learned_from TEXT, case_count INTEGER, created_at REAL,
                updated_at REAL);
            CREATE TABLE defect_fixes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, pattern_id TEXT,
                symbol_hash TEXT, before_hash TEXT, after_hash TEXT,
                fix_diff TEXT, effectiveness REAL, created_at REAL);
            ",
        )
        .unwrap();
        conn
    }

    #[test]
    fn content_hash_matches_norm_newlines_sha256() {
        // 复刻 config.compute_content_hash：\r\n 和 \r 归一后 sha256。
        // 两种换行写法内容等价 → hash 必须相同。
        let a = compute_content_hash("line1\nline2\n");
        let b = compute_content_hash("line1\r\nline2\r\n");
        let c = compute_content_hash("line1\rline2\r");
        assert_eq!(a, b);
        assert_eq!(a, c);
        // 已知向量：sha256("") = e3b0c442...，空内容归一后仍空
        assert_eq!(
            compute_content_hash(""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(a.len(), 64);
    }

    #[test]
    fn snippet_in_content_strips_snippet() {
        // 复刻 _snippet_in_content：snippet.strip() 子串匹配，空返回 false。
        assert!(snippet_in_content("  foo()  ", "x = foo() + 1"));
        assert!(!snippet_in_content("bar()", "x = foo()"));
        assert!(!snippet_in_content("", "anything"));
        assert!(!snippet_in_content("x", ""));
    }

    #[test]
    fn gc_purge_writes_audit_running_then_completed() {
        let conn = setup_db();
        conn.execute(
            "INSERT INTO workspaces (id, root_path) VALUES (1, '/tmp/ws')",
            [],
        )
        .unwrap();
        // 一条很久以前归档的文件（archived_at 远早于 cutoff）
        conn.execute(
            "INSERT INTO file_instances (id, workspace_id, status) VALUES (10, 1, 'archived')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO archived_files
               (file_instance_id, workspace_id, rel_path, abs_path, content_hash,
                symbol_count, call_count, archive_reason, archived_at)
             VALUES (10, 1, 'old.py', '/tmp/ws/old.py', 'h', 3, 7, 'ignored', 1.0)",
            [],
        )
        .unwrap();

        let r = handle_gc_purge(&conn, 1, &json!({ "older_than_days": 1 })).unwrap();
        // 返回含 audit_id，清除 1 文件（3 符号 7 调用）
        let aid = r["audit_id"].as_i64().unwrap();
        assert!(aid > 0);
        assert_eq!(r["purged_files"].as_i64().unwrap(), 1);
        assert_eq!(r["purged_symbols"].as_i64().unwrap(), 3);
        assert_eq!(r["purged_calls"].as_i64().unwrap(), 7);

        // gc_runs 应有一条 completed 记录，policy/counts 落库
        let (status, op, policy, cand, del): (String, String, String, String, String) = conn
            .query_row(
                "SELECT status, operation, policy_json, candidate_counts, deleted_counts
                 FROM gc_runs WHERE id = ?1",
                rusqlite::params![aid],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
            )
            .unwrap();
        assert_eq!(status, "completed");
        assert_eq!(op, "purge");
        assert_eq!(policy, "{\"older_than_days\":1}");
        assert_eq!(cand, "{\"archived_files\":1}");
        // deleted_counts key 排序（calls < files < symbols）
        assert_eq!(
            del,
            "{\"purged_calls\":7,\"purged_files\":1,\"purged_symbols\":3}"
        );
        // 文件实例与归档记录都已物理删除
        let fi: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM file_instances WHERE id = 10",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(fi, 0);
    }

    #[test]
    fn gc_restore_full_scan_respects_ignore_and_counts() {
        let conn = setup_db();
        conn.execute(
            "INSERT INTO workspaces (id, root_path) VALUES (1, '/tmp/ws')",
            [],
        )
        .unwrap();
        // 两条归档文件：一条普通 .py（应复活），一条在 .git/ 下（默认 ignore，应 still_ignored）
        for (fid, rel) in [(1_i64, "src/app.py"), (2, ".git/config")] {
            conn.execute(
                "INSERT INTO file_instances (id, workspace_id, status) VALUES (?1, 1, 'archived')",
                rusqlite::params![fid],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO archived_files
                   (file_instance_id, workspace_id, rel_path, abs_path, content_hash,
                    symbol_count, call_count, archive_reason, archived_at)
                 VALUES (?1, 1, ?2, ?3, 'h', 0, 0, 'ignored', 1.0)",
                rusqlite::params![fid, rel, format!("/tmp/ws/{rel}")],
            )
            .unwrap();
        }

        // rel_paths 为空 → 全扫；非 force → 用默认 ignore 规则（.git 命中）
        let r = handle_gc_restore(&conn, 1, &json!({ "rel_paths": [] })).unwrap();
        assert_eq!(r["scanned"].as_i64().unwrap(), 2);
        // src/app.py 复活；.git/config 仍被忽略
        assert_eq!(r["restored"].as_i64().unwrap(), 1);
        assert_eq!(r["still_ignored"].as_i64().unwrap(), 1);
        // 复活的文件实例 status → pending
        let st: String = conn
            .query_row("SELECT status FROM file_instances WHERE id = 1", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(st, "pending");
    }

    #[test]
    fn build_defect_fixes_links_fix_from_git_change() {
        let conn = setup_db();
        let now = 1_000_000.0_f64;
        // 构造：某符号从含 unwrap() 的 old 修改为不含的 new（视为修复）
        let old = "fn f() { x.unwrap(); }";
        let new = "fn f() { x.expect(\"ok\"); }";
        conn.execute(
            "INSERT INTO git_symbol_changes
               (commit_hash, symbol_hash, change_type, old_content, new_content)
             VALUES ('c1', 'HASH_A', 'modified', ?1, ?2)",
            rusqlite::params![old, new],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbol_contents (content_hash, qualified_name) VALUES ('HASH_A', 'mod::f')",
            [],
        )
        .unwrap();
        // finding：snippet 'x.unwrap()' 在 old、不在 new → 修复命中
        conn.execute(
            "INSERT INTO semgrep_findings
               (rule_id, rule_name, message, severity, snippet, fix, content_hash, symbol_qualified)
             VALUES ('rust.lang.security.unwrap', 'no-unwrap', 'avoid unwrap', 'warning',
                     'x.unwrap()', 'use expect', 'HASH_A', 'mod::f')",
            [],
        )
        .unwrap();

        let learned = build_defect_fixes(&conn, now).unwrap();
        assert_eq!(learned, 1, "应学到 1 条修复");

        // defect_fixes 落库：pattern_id=DP-{rule_id}, effectiveness=0.8, before/after hash 正确
        let (pid, sym, before, after, eff): (String, String, String, String, f64) = conn
            .query_row(
                "SELECT pattern_id, symbol_hash, before_hash, after_hash, effectiveness
                 FROM defect_fixes LIMIT 1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
            )
            .unwrap();
        assert_eq!(pid, "DP-rust.lang.security.unwrap");
        assert_eq!(sym, "HASH_A");
        assert_eq!(before, compute_content_hash(old));
        assert_eq!(after, compute_content_hash(new));
        assert!((eff - 0.8).abs() < 1e-9);

        // ensure 了 git_fix 来源的 pattern
        let lf: String = conn
            .query_row(
                "SELECT learned_from FROM defect_patterns WHERE pattern_id = ?1",
                rusqlite::params![pid],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(lf, "git_fix");

        // 幂等：再跑一次不重复插入（持久化去重）
        let again = build_defect_fixes(&conn, now).unwrap();
        assert_eq!(again, 0, "重复运行不应再插入");
        let total: i64 = conn
            .query_row("SELECT COUNT(*) FROM defect_fixes", [], |r| r.get(0))
            .unwrap();
        assert_eq!(total, 1);
    }

    #[test]
    fn build_defect_fixes_skips_when_snippet_still_present() {
        // snippet 在 new 中仍存在 → 未修复，不应生成 fix
        let conn = setup_db();
        conn.execute(
            "INSERT INTO git_symbol_changes
               (commit_hash, symbol_hash, change_type, old_content, new_content)
             VALUES ('c1', 'HASH_B', 'modified', 'a.unwrap(); b=1', 'a.unwrap(); b=2')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbol_contents (content_hash, qualified_name) VALUES ('HASH_B', 'g')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO semgrep_findings
               (rule_id, snippet, fix, content_hash, symbol_qualified)
             VALUES ('r1', 'a.unwrap()', '', 'HASH_B', 'g')",
            [],
        )
        .unwrap();
        let learned = build_defect_fixes(&conn, 1.0).unwrap();
        assert_eq!(learned, 0);
    }
}
