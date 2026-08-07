//! Rust SQLite schema/version/transaction boundary.
//!
//! The authoritative DDL remains `db/schema.py`.  It is embedded at compile
//! time so the released Rust binary does not need a Python runtime or a source
//! checkout.  The migration runner deliberately fails closed: it never writes
//! schema version 47 unless all DDL, compatibility columns, and indexes have
//! committed in one SQLite transaction.

use std::path::Path;
use std::time::Duration;

use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;
use rusqlite::{Connection, OpenFlags};

pub const RUST_SCHEMA_VERSION: i64 = 47;

const EMBEDDED_SCHEMA_SOURCE: &str =
    include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../db/schema.py"));

fn schema_sql_block() -> Result<&'static str, String> {
    let marker = "SCHEMA_SQL = \"\"\"";
    let start = EMBEDDED_SCHEMA_SOURCE
        .find(marker)
        .ok_or_else(|| "db/schema.py no longer contains SCHEMA_SQL marker".to_string())?
        + marker.len();
    let end = EMBEDDED_SCHEMA_SOURCE[start..]
        .find("\n\"\"\"")
        .ok_or_else(|| "db/schema.py SCHEMA_SQL block is unterminated".to_string())?
        + start;
    Ok(&EMBEDDED_SCHEMA_SOURCE[start..end])
}

fn statement_end(source: &str, start: usize) -> Option<usize> {
    let bytes = source.as_bytes();
    let mut depth = 0usize;
    let mut single_quote = false;
    let mut index = start;
    while index < bytes.len() {
        match bytes[index] {
            b'\'' => {
                if single_quote && bytes.get(index + 1) == Some(&b'\'') {
                    index += 1;
                } else {
                    single_quote = !single_quote;
                }
            }
            b'(' if !single_quote => depth += 1,
            b')' if !single_quote && depth > 0 => depth -= 1,
            b';' if !single_quote && depth == 0 => return Some(index + 1),
            _ => {}
        }
        index += 1;
    }
    None
}

fn ddl_statements_precise(source: &str, prefixes: &[&str]) -> Vec<String> {
    let mut statements = Vec::new();
    for (start, _) in source.match_indices("CREATE ") {
        if start > 0 && source.as_bytes()[start - 1] != b'\n' {
            continue;
        }
        let line = &source[start..];
        if prefixes.iter().any(|prefix| line.starts_with(prefix)) {
            if let Some(end) = statement_end(source, start) {
                statements.push(source[start..end].to_string());
            }
        }
    }
    statements
}

fn execute_existing_schema(conn: &Connection, schema: &str) -> Result<(), String> {
    for statement in ddl_statements_precise(schema, &["CREATE TABLE", "CREATE VIRTUAL TABLE"]) {
        conn.execute_batch(&statement)
            .map_err(|error| format!("schema table DDL failed: {error}"))?;
    }

    // Columns introduced after the original table definitions.  Keeping this
    // list explicit makes old databases fail visibly instead of being stamped
    // as current while a production query still lacks a required column.
    for (table, column, definition) in [
        ("tasks", "applied_at", "REAL"),
        ("file_versions", "ast_cache", "BLOB"),
        ("workspaces", "active_task_id", "TEXT DEFAULT ''"),
        (
            "task_symbol_changes",
            "source_commit_hash",
            "TEXT DEFAULT ''",
        ),
        ("git_file_changes", "lines_added", "INTEGER DEFAULT 0"),
        ("git_file_changes", "lines_deleted", "INTEGER DEFAULT 0"),
        ("semgrep_findings", "scan_id", "INTEGER"),
    ] {
        let present = conn
            .prepare(&format!("PRAGMA table_info({table})"))
            .and_then(|mut statement| {
                statement
                    .query_map([], |row| row.get::<_, String>(1))?
                    .collect::<Result<Vec<_>, _>>()
            })
            .map_err(|error| format!("cannot inspect {table}: {error}"))?
            .iter()
            .any(|name| name == column);
        if !present {
            conn.execute_batch(&format!(
                "ALTER TABLE {table} ADD COLUMN {column} {definition}"
            ))
            .map_err(|error| format!("cannot add {table}.{column}: {error}"))?;
        }
    }

    // Indexes are executed after compatibility columns are present.  Trigger
    // recreation is intentionally left to the existing schema; missing index
    // or uniqueness errors remain fatal and prevent version publication.
    for statement in ddl_statements_precise(schema, &["CREATE INDEX", "CREATE UNIQUE INDEX"]) {
        conn.execute_batch(&statement)
            .map_err(|error| format!("schema index DDL failed: {error}"))?;
    }
    Ok(())
}

/// 读取当前 schema 版本（只读，无写锁）。
///
/// pub(crate)：供 TaskCollabStore 迁移后校验实际版本。
pub(crate) fn current_schema_version(conn: &Connection) -> Result<i64, rusqlite::Error> {
    conn.query_row(
        "SELECT COALESCE(MAX(version), 0) FROM schema_version",
        [],
        |row| row.get(0),
    )
}

/// 事务化官方 schema 迁移（与 Python `_migrate_schema` 等价，幂等）。
///
/// pub(crate)：供 daemon TaskCollabStore 等组件在打开权威库后调用，
/// 确保任务表与 schema_version 审计由同一条正式迁移路径管理。
pub(crate) fn migrate_connection(conn: &Connection) -> Result<i64, String> {
    conn.busy_timeout(Duration::from_secs(5))
        .map_err(|error| format!("cannot set SQLite busy_timeout: {error}"))?;
    conn.execute_batch("PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;")
        .map_err(|error| format!("cannot configure SQLite: {error}"))?;
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at REAL NOT NULL,
            description TEXT DEFAULT ''
        )",
    )
    .map_err(|error| format!("cannot create schema_version: {error}"))?;

    let current = current_schema_version(conn)
        .map_err(|error| format!("cannot read schema version: {error}"))?;
    if current >= RUST_SCHEMA_VERSION {
        return Ok(current);
    }

    conn.execute_batch("BEGIN IMMEDIATE")
        .map_err(|error| format!("cannot begin schema migration: {error}"))?;
    let result = (|| {
        let schema = schema_sql_block()?;
        if current == 0 {
            conn.execute_batch(schema)
                .map_err(|error| format!("initial schema DDL failed: {error}"))?;
        } else {
            execute_existing_schema(conn, schema)?;
        }
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_secs_f64())
            .unwrap_or(0.0);
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version, applied_at, description)
             VALUES (?1, ?2, ?3)",
            rusqlite::params![RUST_SCHEMA_VERSION, now, "Rust schema migration to v47"],
        )
        .map_err(|error| format!("cannot publish schema version 47: {error}"))?;
        Ok::<(), String>(())
    })();

    match result {
        Ok(()) => {
            conn.execute_batch("COMMIT")
                .map_err(|error| format!("schema migration commit failed: {error}"))?;
            Ok(RUST_SCHEMA_VERSION)
        }
        Err(error) => {
            let _ = conn.execute_batch("ROLLBACK");
            Err(error)
        }
    }
}

/// Read the authoritative schema version without taking a write lock.
#[pyfunction]
pub fn sqlite_query_schema_version(db_path: &str) -> PyResult<i64> {
    if db_path.is_empty() {
        return Err(PyValueError::new_err("db_path 不能为空"));
    }
    let conn = Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    )
    .map_err(|error| PyIOError::new_err(format!("打开数据库失败: {error}")))?;
    conn.busy_timeout(Duration::from_secs(5))
        .map_err(|error| PyIOError::new_err(format!("设置 busy_timeout 失败: {error}")))?;
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
    Ok(current_schema_version(&conn).unwrap_or(0))
}

/// Run the Rust schema migration transaction and return the committed version.
#[pyfunction]
pub fn sqlite_migrate_schema(db_path: &str) -> PyResult<i64> {
    if db_path.is_empty() {
        return Err(PyValueError::new_err("db_path 不能为空"));
    }
    if let Some(parent) = Path::new(db_path).parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)
                .map_err(|error| PyIOError::new_err(format!("创建数据库目录失败: {error}")))?;
        }
    }
    let conn = Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_CREATE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| PyIOError::new_err(format!("打开数据库失败: {error}")))?;
    migrate_connection(&conn)
        .map_err(|error| PyIOError::new_err(format!("Rust schema migration failed: {error}")))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sqlite_query_schema_version, m)?)?;
    m.add_function(wrap_pyfunction!(sqlite_migrate_schema, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fresh_database_is_created_at_v47() {
        let conn = Connection::open_in_memory().unwrap();
        assert_eq!(migrate_connection(&conn).unwrap(), RUST_SCHEMA_VERSION);
        assert_eq!(current_schema_version(&conn).unwrap(), RUST_SCHEMA_VERSION);
        let workspaces: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'workspaces'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let rollback_config: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'rollback_config'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(workspaces, 1);
        assert_eq!(rollback_config, 1);
    }

    #[test]
    fn migration_is_idempotent_after_v47() {
        let conn = Connection::open_in_memory().unwrap();
        assert_eq!(migrate_connection(&conn).unwrap(), RUST_SCHEMA_VERSION);
        assert_eq!(migrate_connection(&conn).unwrap(), RUST_SCHEMA_VERSION);
        assert_eq!(current_schema_version(&conn).unwrap(), RUST_SCHEMA_VERSION);
    }
}
