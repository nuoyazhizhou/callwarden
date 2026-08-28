//! Schema migrator authority handlers（SRV-015）。
//!
//! `server/schema_migrator.py` 的数据库连接、版本查询、迁移和 schema 校验
//! 全部在此由 Rust daemon 执行。Python 侧只保留 RPC 参数/结果适配。

use std::path::{Path, PathBuf};

use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Value};

use super::{get_str_param, DaemonRpcError};

const REGISTRY_MIGRATION_SET: &str = "registry";
const AUDIT_MIGRATION_SET: &str = "audit";

fn default_callwarden_dir() -> PathBuf {
    let home = std::env::var("CALLWARDEN_HOME")
        .ok()
        .filter(|value| !value.is_empty())
        .or_else(|| std::env::var("USERPROFILE").ok())
        .or_else(|| std::env::var("HOME").ok())
        .unwrap_or_default();
    PathBuf::from(home).join(".callwarden")
}

fn default_db_path(migration_set: &str) -> PathBuf {
    let explicit_key: [&str; 2] = match migration_set {
        AUDIT_MIGRATION_SET => ["CALLWARDEN_DAEMON_AUDIT_DB", "CW_DAEMON_AUDIT_DB"],
        _ => [
            "CALLWARDEN_DAEMON_REGISTRY_DB",
            "CW_DAEMON_REGISTRY_DB",
        ],
    };
    for key in explicit_key {
        if let Ok(value) = std::env::var(key) {
            if !value.is_empty() {
                return PathBuf::from(value);
            }
        }
    }
    if let Ok(root) = std::env::var("CW_DAEMON_DATA_ROOT") {
        if !root.is_empty() {
            return PathBuf::from(root).join(format!("{migration_set}.db"));
        }
    }
    default_callwarden_dir()
        .join("daemon")
        .join(format!("{migration_set}.db"))
}

fn migration_set(params: &Value) -> Result<&str, DaemonRpcError> {
    let value = get_str_param(params, "migration_set")
        .or_else(|| get_str_param(params, "db_name"))
        .unwrap_or(REGISTRY_MIGRATION_SET);
    match value {
        REGISTRY_MIGRATION_SET | AUDIT_MIGRATION_SET => Ok(value),
        other => Err(DaemonRpcError::invalid_params(format!(
            "unsupported migration_set: {other}"
        ))),
    }
}

fn db_path(params: &Value, set: &str) -> PathBuf {
    get_str_param(params, "db_path")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| default_db_path(set))
}

fn open_readonly(path: &Path) -> Result<Connection, rusqlite::Error> {
    Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
}

fn current_version(conn: &Connection) -> i64 {
    conn.query_row(
        "SELECT COALESCE(MAX(version), 0) FROM schema_version",
        [],
        |row| row.get::<_, i64>(0),
    )
    .unwrap_or(0)
}

fn schema_version_ddl(conn: &Connection) -> Result<(), rusqlite::Error> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at REAL NOT NULL,
            description TEXT DEFAULT ''
        );",
    )
}

fn apply_registry_migration(conn: &Connection, version: i64) -> Result<&'static str, rusqlite::Error> {
    match version {
        1 => conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS daemon_workspaces (
                workspace_id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_instance_id TEXT NOT NULL UNIQUE,
                snapshot_id TEXT,
                owner_uid INTEGER NOT NULL,
                git_remote_url TEXT DEFAULT '',
                git_head_commit_sha TEXT DEFAULT '',
                client_view_root TEXT NOT NULL,
                host_real_root TEXT NOT NULL,
                toolchain_fingerprint TEXT DEFAULT '',
                registered_at REAL NOT NULL,
                last_active_at REAL NOT NULL,
                status TEXT DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS container_mount_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                container_id TEXT NOT NULL,
                container_path TEXT NOT NULL,
                host_path TEXT NOT NULL,
                mapping_type TEXT DEFAULT 'bind',
                UNIQUE(container_id, container_path)
            );
            CREATE TABLE IF NOT EXISTS daemon_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_workspaces_owner ON daemon_workspaces(owner_uid);
            CREATE INDEX IF NOT EXISTS idx_workspaces_snapshot ON daemon_workspaces(snapshot_id);
            CREATE INDEX IF NOT EXISTS idx_workspaces_status ON daemon_workspaces(status);",
        )
        .map(|_| "初始 schema: daemon_workspaces + container_mount_mappings + daemon_state"),
        2 => conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS backup_history (
                backup_id TEXT PRIMARY KEY,
                backup_type TEXT NOT NULL,
                created_at REAL NOT NULL,
                file_count INTEGER DEFAULT 0,
                total_size_bytes INTEGER DEFAULT 0,
                checksum TEXT DEFAULT '',
                deleted_at REAL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_backup_history_created ON backup_history(created_at);
            CREATE INDEX IF NOT EXISTS idx_backup_history_type ON backup_history(backup_type);",
        )
        .map(|_| "新增 backup_history 表"),
        3 => conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_migrations_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                db_name TEXT NOT NULL,
                from_version INTEGER NOT NULL,
                to_version INTEGER NOT NULL,
                applied_at REAL NOT NULL,
                duration_ms INTEGER DEFAULT 0,
                status TEXT DEFAULT 'success',
                error TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_migrations_log_db ON schema_migrations_log(db_name);
            CREATE INDEX IF NOT EXISTS idx_migrations_log_applied ON schema_migrations_log(applied_at);",
        )
        .map(|_| "新增 schema_migrations_log 表"),
        _ => Err(rusqlite::Error::InvalidParameterName(format!(
            "registry migration v{version}"
        ))),
    }
}

fn apply_audit_migration(conn: &Connection, version: i64) -> Result<&'static str, rusqlite::Error> {
    match version {
        1 => conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS audit_log (
                event_id TEXT PRIMARY KEY,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                actor_uid INTEGER NOT NULL,
                actor_role TEXT DEFAULT '',
                action TEXT NOT NULL,
                target TEXT DEFAULT '',
                result TEXT DEFAULT '',
                details TEXT DEFAULT '{}',
                client_ip TEXT DEFAULT ''
            );",
        )
        .map(|_| "初始 schema: audit_log"),
        2 => conn.execute_batch(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);
             CREATE INDEX IF NOT EXISTS idx_audit_log_event_type ON audit_log(event_type);
             CREATE INDEX IF NOT EXISTS idx_audit_log_actor_uid ON audit_log(actor_uid);
             CREATE INDEX IF NOT EXISTS idx_audit_log_result ON audit_log(result);",
        )
        .map(|_| "新增 timestamp/event_type/actor_uid/result 索引"),
        _ => Err(rusqlite::Error::InvalidParameterName(format!(
            "audit migration v{version}"
        ))),
    }
}

fn target_version(set: &str) -> i64 {
    match set {
        AUDIT_MIGRATION_SET => 2,
        _ => 3,
    }
}

/// 应用 Rust daemon 管理的 registry/audit schema migrations。
pub fn handle_apply_migrations(params: &Value) -> Result<Value, DaemonRpcError> {
    let set = migration_set(params)?;
    let path = db_path(params, set);
    let conn = Connection::open(&path).map_err(|error| {
        DaemonRpcError::internal_error(format!("schema migration open failed: {error}"))
    })?;
    let _ = conn.execute_batch("PRAGMA busy_timeout=5000");
    schema_version_ddl(&conn).map_err(|error| {
        DaemonRpcError::internal_error(format!("schema_version init failed: {error}"))
    })?;
    let from_version = current_version(&conn);
    let target = target_version(set);
    let mut applied = Vec::new();
    let mut to_version = from_version;
    let mut failed = Value::Null;
    let mut error_value = Value::Null;

    for version in (from_version + 1)..=target {
        let result = (|| -> Result<&'static str, rusqlite::Error> {
            conn.execute_batch("BEGIN")?;
            let description = match set {
                AUDIT_MIGRATION_SET => apply_audit_migration(&conn, version),
                _ => apply_registry_migration(&conn, version),
            }?;
            let applied_at = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|value| value.as_secs_f64())
                .unwrap_or(0.0);
            conn.execute(
                "INSERT INTO schema_version (version, applied_at, description) VALUES (?1, ?2, ?3)",
                rusqlite::params![version, applied_at, description],
            )?;
            conn.execute_batch("COMMIT")?;
            Ok(description)
        })();
        match result {
            Ok(_) => {
                applied.push(version);
                to_version = version;
            }
            Err(err) => {
                let _ = conn.execute_batch("ROLLBACK");
                failed = json!(version);
                error_value = json!(format!("{}: {}", err.sqlite_error_code().map(|c| format!("{c:?}")).unwrap_or_else(|| "sqlite".to_string()), err));
                break;
            }
        }
    }
    let status = if !failed.is_null() {
        "failed"
    } else if applied.is_empty() {
        "up_to_date"
    } else {
        "migrated"
    };
    Ok(json!({
        "db_path": path.to_string_lossy(),
        "from_version": from_version,
        "to_version": to_version,
        "applied": applied,
        "skipped": [],
        "failed": failed,
        "error": error_value,
        "status": status,
        "source": "rust"
    }))
}

/// 只读获取当前 schema version；缺库/缺表返回 0，不创建文件。
pub fn handle_get_current_version(params: &Value) -> Result<Value, DaemonRpcError> {
    let set = migration_set(params)?;
    let path = db_path(params, set);
    if !path.is_file() {
        return Ok(json!(0));
    }
    let conn = open_readonly(&path).map_err(|error| {
        DaemonRpcError::internal_error(format!("schema version open failed: {error}"))
    })?;
    Ok(json!(current_version(&conn)))
}

/// 只读获取 schema migration history；缺库/缺表返回空数组。
pub fn handle_get_migration_history(params: &Value) -> Result<Value, DaemonRpcError> {
    let set = migration_set(params)?;
    let path = db_path(params, set);
    if !path.is_file() {
        return Ok(json!([]));
    }
    let conn = open_readonly(&path).map_err(|error| {
        DaemonRpcError::internal_error(format!("migration history open failed: {error}"))
    })?;
    let mut statement = match conn.prepare(
        "SELECT version, applied_at, description FROM schema_version ORDER BY version ASC",
    ) {
        Ok(statement) => statement,
        Err(_) => return Ok(json!([])),
    };
    let rows = statement
        .query_map([], |row| {
            Ok::<Value, rusqlite::Error>(json!({
                "version": row.get::<_, i64>(0)?,
                "applied_at": row.get::<_, f64>(1)?,
                "description": row.get::<_, String>(2)?,
            }))
        })
        .map_err(|error| DaemonRpcError::internal_error(format!("migration history query failed: {error}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| DaemonRpcError::internal_error(format!("migration history row failed: {error}")))?;
    Ok(json!(rows))
}

fn string_list(params: &Value, key: &str) -> Result<Vec<String>, DaemonRpcError> {
    let value = params
        .get(key)
        .and_then(Value::as_array)
        .ok_or_else(|| DaemonRpcError::invalid_params(format!("{key} must be an array")))?;
    value
        .iter()
        .map(|item| {
            item.as_str()
                .map(str::to_string)
                .ok_or_else(|| DaemonRpcError::invalid_params(format!("{key} must contain strings")))
        })
        .collect()
}

/// 只读校验 schema 表和索引，不触发本地写入。
pub fn handle_validate_schema(params: &Value) -> Result<Value, DaemonRpcError> {
    let set = migration_set(params)?;
    let path = db_path(params, set);
    let expected_tables = string_list(params, "expected_tables")?;
    let expected_indexes = match params.get("expected_indexes") {
        None | Some(Value::Null) => Vec::new(),
        Some(_) => string_list(params, "expected_indexes")?,
    };
    if !path.is_file() {
        return Ok(json!({
            "valid": false,
            "missing_tables": expected_tables,
            "missing_indexes": expected_indexes,
            "current_version": 0,
            "source": "rust"
        }));
    }
    let conn = open_readonly(&path).map_err(|error| {
        DaemonRpcError::internal_error(format!("schema validation open failed: {error}"))
    })?;
    let existing_tables = object_names(&conn, "table")?;
    let existing_indexes = object_names(&conn, "index")?;
    let missing_tables: Vec<_> = expected_tables
        .into_iter()
        .filter(|name| !existing_tables.iter().any(|item| item == name))
        .collect();
    let missing_indexes: Vec<_> = expected_indexes
        .into_iter()
        .filter(|name| !existing_indexes.iter().any(|item| item == name))
        .collect();
    Ok(json!({
        "valid": missing_tables.is_empty() && missing_indexes.is_empty(),
        "missing_tables": missing_tables,
        "missing_indexes": missing_indexes,
        "current_version": current_version(&conn),
        "source": "rust"
    }))
}

fn object_names(conn: &Connection, object_type: &str) -> Result<Vec<String>, DaemonRpcError> {
    let mut statement = conn
        .prepare("SELECT name FROM sqlite_master WHERE type = ?1")
        .map_err(|error| DaemonRpcError::internal_error(format!("schema object prepare failed: {error}")))?;
    let rows = statement
        .query_map(rusqlite::params![object_type], |row| row.get::<_, String>(0))
        .map_err(|error| DaemonRpcError::internal_error(format!("schema object query failed: {error}")))?
        .collect::<Result<Vec<String>, _>>();
    rows.map_err(|error| DaemonRpcError::internal_error(format!("schema object row failed: {error}")))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_db(tag: &str) -> PathBuf {
        let directory = std::env::temp_dir().join(format!("srv015_{tag}_{}", std::process::id()));
        let _ = std::fs::create_dir_all(&directory);
        let path = directory.join(format!("{tag}.db"));
        let _ = std::fs::remove_file(&path);
        path
    }

    #[test]
    fn registry_migration_is_idempotent_and_has_history() {
        let path = temp_db("registry");
        let params = json!({"db_path": path.to_string_lossy(), "migration_set": "registry"});
        let first = handle_apply_migrations(&params).unwrap();
        assert_eq!(first["status"], "migrated");
        assert_eq!(first["applied"], json!([1, 2, 3]));
        let second = handle_apply_migrations(&params).unwrap();
        assert_eq!(second["status"], "up_to_date");
        assert_eq!(handle_get_current_version(&params).unwrap(), json!(3));
        assert_eq!(handle_get_migration_history(&params).unwrap().as_array().unwrap().len(), 3);
    }

    #[test]
    fn audit_validation_and_invalid_set_are_stable() {
        let path = temp_db("audit");
        let params = json!({"db_path": path.to_string_lossy(), "migration_set": "audit"});
        handle_apply_migrations(&params).unwrap();
        let validation = handle_validate_schema(&json!({
            "db_path": path.to_string_lossy(),
            "migration_set": "audit",
            "expected_tables": ["audit_log", "schema_version"],
            "expected_indexes": ["idx_audit_log_timestamp"]
        }))
        .unwrap();
        assert_eq!(validation["valid"], true);
        let error = handle_apply_migrations(&json!({"migration_set": "unknown"})).unwrap_err();
        assert_eq!(error.code, "invalid_params");
    }

    #[test]
    fn missing_database_is_read_only_and_reports_missing_schema() {
        let path = temp_db("missing");
        let params = json!({"db_path": path.to_string_lossy()});
        assert_eq!(handle_get_current_version(&params).unwrap(), json!(0));
        assert_eq!(handle_get_migration_history(&params).unwrap(), json!([]));
        let result = handle_validate_schema(&json!({
            "db_path": path.to_string_lossy(),
            "expected_tables": ["daemon_workspaces"]
        }))
        .unwrap();
        assert_eq!(result["valid"], false);
        assert!(!path.exists());
    }
}
