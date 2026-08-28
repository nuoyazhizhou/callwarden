//! Snapshot GC authority handlers（SRV-016）。
//!
//! GC 的 SQLite 扫描、删除和 VACUUM 均由 Rust daemon 执行；Python 只负责
//! 将 GCPolicy/路径/键序列化为 RPC 参数并格式化返回值。

use std::path::PathBuf;

use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Value};

use super::{get_str_param, DaemonRpcError};

fn default_callwarden_dir() -> PathBuf {
    let home = std::env::var("CALLWARDEN_HOME")
        .ok()
        .filter(|value| !value.is_empty())
        .or_else(|| std::env::var("USERPROFILE").ok())
        .or_else(|| std::env::var("HOME").ok())
        .unwrap_or_default();
    PathBuf::from(home).join(".callwarden")
}

fn registry_path(params: &Value) -> PathBuf {
    get_str_param(params, "registry_db_path")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var("CW_DAEMON_REGISTRY_DB").ok().map(PathBuf::from))
        .unwrap_or_else(|| default_callwarden_dir().join("daemon").join("registry.db"))
}

fn audit_path(params: &Value) -> Option<PathBuf> {
    get_str_param(params, "audit_db_path")
        .or_else(|| get_str_param(params, "audit_log_path"))
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var("CW_DAEMON_AUDIT_DB").ok().map(PathBuf::from))
}

fn max_age(params: &Value) -> f64 {
    params
        .get("max_age_seconds")
        .and_then(Value::as_f64)
        .unwrap_or(7.0 * 24.0 * 3600.0)
}

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_secs_f64())
        .unwrap_or(0.0)
}

fn open_rw(path: &PathBuf) -> Result<Connection, DaemonRpcError> {
    Connection::open(path)
        .map_err(|error| DaemonRpcError::internal_error(format!("GC DB open failed: {error}")))
}

fn open_ro(path: &PathBuf) -> Result<Connection, DaemonRpcError> {
    Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|error| DaemonRpcError::internal_error(format!("GC DB open failed: {error}")))
}

fn has_table(conn: &Connection, name: &str) -> bool {
    conn.query_row(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?1",
        rusqlite::params![name],
        |row| row.get::<_, i64>(0),
    )
    .unwrap_or(0)
        > 0
}

fn item(item_type: &str, key: impl Into<String>, size: i64, reason: &str, metadata: Value) -> Value {
    json!({
        "item_type": item_type,
        "key": key.into(),
        "size_bytes": size,
        "reason": reason,
        "metadata": metadata
    })
}

/// 查询 registry 中仍被 workspace 引用的 snapshot ids。
pub fn handle_get_registered_snapshot_ids(params: &Value) -> Result<Value, DaemonRpcError> {
    let path = registry_path(params);
    if !path.is_file() {
        return Ok(json!([]));
    }
    let conn = open_ro(&path)?;
    if !has_table(&conn, "daemon_workspaces") {
        return Ok(json!([]));
    }
    let mut stmt = conn
        .prepare("SELECT DISTINCT snapshot_id FROM daemon_workspaces WHERE snapshot_id IS NOT NULL AND snapshot_id != ''")
        .map_err(|error| DaemonRpcError::internal_error(format!("snapshot ids prepare failed: {error}")))?;
    let values = stmt
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|error| DaemonRpcError::internal_error(format!("snapshot ids query failed: {error}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| DaemonRpcError::internal_error(format!("snapshot ids row failed: {error}")))?;
    Ok(json!(values))
}

/// 扫描 registry backup_history 中已删除或已过期记录。
pub fn handle_scan_expired_backup_history(params: &Value) -> Result<Value, DaemonRpcError> {
    let path = registry_path(params);
    if !path.is_file() {
        return Ok(json!([]));
    }
    let conn = open_ro(&path)?;
    if !has_table(&conn, "backup_history") {
        return Ok(json!([]));
    }
    let cutoff = now() - max_age(params);
    let mut stmt = conn
        .prepare("SELECT backup_id, created_at, deleted_at, total_size_bytes FROM backup_history WHERE deleted_at > 0 OR (deleted_at = 0 AND created_at < ?1)")
        .map_err(|error| DaemonRpcError::internal_error(format!("backup history prepare failed: {error}")))?;
    let rows = stmt
        .query_map(rusqlite::params![cutoff], |row| {
            let deleted_at: f64 = row.get(2)?;
            let created_at: f64 = row.get(1)?;
            let reason = if deleted_at > 0.0 { "deleted" } else { "expired" };
            Ok(item(
                "backup_history",
                row.get::<_, String>(0)?,
                row.get::<_, i64>(3).unwrap_or(0),
                reason,
                json!({"created_at": created_at, "deleted_at": deleted_at}),
            ))
        })
        .map_err(|error| DaemonRpcError::internal_error(format!("backup history query failed: {error}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| DaemonRpcError::internal_error(format!("backup history row failed: {error}")))?;
    Ok(json!(rows))
}

/// 扫描 schema_migrations_log 中超过保留窗口的旧记录。
pub fn handle_scan_expired_migrations_log(params: &Value) -> Result<Value, DaemonRpcError> {
    let path = registry_path(params);
    if !path.is_file() {
        return Ok(json!([]));
    }
    let conn = open_ro(&path)?;
    if !has_table(&conn, "schema_migrations_log") {
        return Ok(json!([]));
    }
    let retention = params
        .get("retention_count")
        .and_then(Value::as_i64)
        .unwrap_or(3)
        .max(1);
    let keep = (retention * 10).max(50);
    let total: i64 = conn
        .query_row("SELECT COUNT(*) FROM schema_migrations_log", [], |row| row.get(0))
        .unwrap_or(0);
    if total <= keep {
        return Ok(json!([]));
    }
    let mut stmt = conn
        .prepare("SELECT id, db_name, from_version, to_version, applied_at FROM schema_migrations_log ORDER BY applied_at ASC LIMIT ?1")
        .map_err(|error| DaemonRpcError::internal_error(format!("migration log prepare failed: {error}")))?;
    let rows = stmt
        .query_map(rusqlite::params![total - keep], |row| {
            Ok(item(
                "migration_log",
                row.get::<_, i64>(0)?.to_string(),
                0,
                "old_log",
                json!({
                    "db_name": row.get::<_, String>(1)?,
                    "from_version": row.get::<_, i64>(2)?,
                    "to_version": row.get::<_, i64>(3)?,
                    "applied_at": row.get::<_, f64>(4)?
                }),
            ))
        })
        .map_err(|error| DaemonRpcError::internal_error(format!("migration log query failed: {error}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| DaemonRpcError::internal_error(format!("migration log row failed: {error}")))?;
    Ok(json!(rows))
}

/// 扫描 audit.db 中已过期的审计记录。
pub fn handle_scan_expired_audit_logs(params: &Value) -> Result<Value, DaemonRpcError> {
    let Some(path) = audit_path(params) else { return Ok(json!([])); };
    if !path.is_file() {
        return Ok(json!([]));
    }
    let conn = open_ro(&path)?;
    if !has_table(&conn, "audit_log") {
        return Ok(json!([]));
    }
    let cutoff = now() - max_age(params);
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM audit_log WHERE timestamp < ?1", rusqlite::params![cutoff], |row| row.get(0))
        .unwrap_or(0);
    if count == 0 {
        return Ok(json!([]));
    }
    Ok(json!([item("audit_log", "expired_batch", 0, "expired", json!({"count": count, "cutoff": cutoff}))]))
}

/// 扫描过期 archived workspace，供 SnapshotCache 驱逐。
pub fn handle_scan_orphaned_workspaces(params: &Value) -> Result<Value, DaemonRpcError> {
    let path = registry_path(params);
    if !path.is_file() {
        return Ok(json!([]));
    }
    let conn = open_ro(&path)?;
    if !has_table(&conn, "daemon_workspaces") {
        return Ok(json!([]));
    }
    let cutoff = now() - max_age(params);
    let mut stmt = conn
        .prepare("SELECT workspace_instance_id, owner_uid, last_active_at FROM daemon_workspaces WHERE status = 'archived' AND last_active_at <= ?1")
        .map_err(|error| DaemonRpcError::internal_error(format!("orphan workspace prepare failed: {error}")))?;
    let rows = stmt
        .query_map(rusqlite::params![cutoff], |row| {
            Ok(item(
                "workspace_cache",
                row.get::<_, String>(0)?,
                0,
                "unregistered",
                json!({"owner_uid": row.get::<_, i64>(1)?, "last_active_at": row.get::<_, f64>(2)?}),
            ))
        })
        .map_err(|error| DaemonRpcError::internal_error(format!("orphan workspace query failed: {error}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| DaemonRpcError::internal_error(format!("orphan workspace row failed: {error}")))?;
    Ok(json!(rows))
}

pub fn handle_delete_backup_history_record(params: &Value) -> Result<Value, DaemonRpcError> {
    let backup_id = get_str_param(params, "backup_id")
        .ok_or_else(|| DaemonRpcError::invalid_params("missing backup_id"))?;
    let conn = open_rw(&registry_path(params))?;
    let deleted = conn
        .execute("DELETE FROM backup_history WHERE backup_id = ?1", rusqlite::params![backup_id])
        .map_err(|error| DaemonRpcError::internal_error(format!("backup history delete failed: {error}")))?;
    Ok(json!({"deleted": deleted, "source": "rust"}))
}

pub fn handle_delete_migration_log_record(params: &Value) -> Result<Value, DaemonRpcError> {
    let log_id = params
        .get("log_id")
        .and_then(Value::as_i64)
        .ok_or_else(|| DaemonRpcError::invalid_params("missing numeric log_id"))?;
    let conn = open_rw(&registry_path(params))?;
    let deleted = conn
        .execute("DELETE FROM schema_migrations_log WHERE id = ?1", rusqlite::params![log_id])
        .map_err(|error| DaemonRpcError::internal_error(format!("migration log delete failed: {error}")))?;
    Ok(json!({"deleted": deleted, "source": "rust"}))
}

pub fn handle_delete_expired_audit_logs(params: &Value) -> Result<Value, DaemonRpcError> {
    let Some(path) = audit_path(params) else { return Ok(json!({"deleted": 0, "source": "rust"})); };
    let cutoff = params.get("cutoff").and_then(Value::as_f64).unwrap_or_else(|| now() - max_age(params));
    if !path.is_file() {
        return Ok(json!({"deleted": 0, "source": "rust"}));
    }
    let conn = open_rw(&path)?;
    let deleted = conn
        .execute("DELETE FROM audit_log WHERE timestamp < ?1", rusqlite::params![cutoff])
        .map_err(|error| DaemonRpcError::internal_error(format!("audit log delete failed: {error}")))?;
    Ok(json!({"deleted": deleted, "source": "rust"}))
}

pub fn handle_vacuum_databases(params: &Value) -> Result<Value, DaemonRpcError> {
    let mut paths = vec![registry_path(params)];
    if let Some(path) = audit_path(params) {
        if path.is_file() {
            paths.push(path);
        }
    }
    let mut vacuumed = Vec::new();
    let mut failed = Vec::new();
    for path in paths {
        match open_rw(&path).and_then(|conn| {
            conn.execute_batch("VACUUM")
                .map_err(|error| DaemonRpcError::internal_error(format!("VACUUM failed: {error}")))
                .map(|_| ())
        }) {
            Ok(()) => vacuumed.push(path.to_string_lossy().to_string()),
            Err(error) => failed.push(json!({"path": path.to_string_lossy(), "error": error.to_string()})),
        }
    }
    Ok(json!({"vacuumed": vacuumed, "failed": failed, "source": "rust"}))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_db(tag: &str) -> PathBuf {
        let directory = std::env::temp_dir().join(format!("srv016_{tag}_{}", std::process::id()));
        let _ = std::fs::create_dir_all(&directory);
        let path = directory.join(format!("{tag}.db"));
        let _ = std::fs::remove_file(&path);
        path
    }

    #[test]
    fn registered_snapshot_ids_and_orphans_are_read_only() {
        let path = temp_db("registry");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch("CREATE TABLE daemon_workspaces (workspace_instance_id TEXT, snapshot_id TEXT, owner_uid INTEGER, status TEXT, last_active_at REAL); INSERT INTO daemon_workspaces VALUES ('ws-1','snap-1',1,'active',0),('ws-2',NULL,2,'archived',0);").unwrap();
        drop(conn);
        let params = json!({"registry_db_path": path.to_string_lossy(), "max_age_seconds": 0});
        assert_eq!(handle_get_registered_snapshot_ids(&params).unwrap(), json!(["snap-1"]));
        assert_eq!(handle_scan_orphaned_workspaces(&params).unwrap().as_array().unwrap().len(), 1);
    }

    #[test]
    fn delete_and_vacuum_use_rust_owned_db() {
        let path = temp_db("delete");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch("CREATE TABLE backup_history (backup_id TEXT PRIMARY KEY); INSERT INTO backup_history VALUES ('b-1');").unwrap();
        drop(conn);
        let params = json!({"registry_db_path": path.to_string_lossy(), "backup_id": "b-1"});
        assert_eq!(handle_delete_backup_history_record(&params).unwrap()["deleted"], 1);
        assert_eq!(handle_vacuum_databases(&params).unwrap()["failed"], json!([]));
    }

    #[test]
    fn invalid_delete_is_stable() {
        let error = handle_delete_migration_log_record(&json!({})).unwrap_err();
        assert_eq!(error.code, "invalid_params");
    }
}
