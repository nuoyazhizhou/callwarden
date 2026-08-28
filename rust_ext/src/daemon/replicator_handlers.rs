//! Replicator authority handlers（SRV-014）。
//!
//! rollback_config 查询和 refresh 路由均由 Rust daemon 负责；Python
//! `server/replicator.py` 只保留 JSON/RPC 参数适配。

use std::path::PathBuf;

use rusqlite::Connection;
use serde_json::{json, Value};

use super::{get_str_param, DaemonRpcError, DaemonStateExt, PeerCredential};

const RUST_CAS_WRITE_FEATURE: &str = "rust_cas_write";
const RUST_REPLICATOR_QUERY_FEATURE: &str = "rust_replicator_snapshot_query";

fn default_callwarden_dir() -> PathBuf {
    let home = std::env::var("CALLWARDEN_HOME")
        .ok()
        .filter(|v| !v.is_empty())
        .or_else(|| std::env::var("USERPROFILE").ok())
        .or_else(|| std::env::var("HOME").ok())
        .unwrap_or_default();
    PathBuf::from(home).join(".callwarden")
}

fn default_db_path() -> PathBuf {
    if let Ok(value) = std::env::var("CALLWARDEN_DB") {
        if !value.is_empty() {
            return PathBuf::from(value);
        }
    }
    default_callwarden_dir().join("callwarden.db")
}

fn query_rollback_flag(db_path: &PathBuf, feature: &str) -> Value {
    let connection = match Connection::open(db_path) {
        Ok(connection) => connection,
        Err(_) => return json!({"rolled_back": false, "reason": "db_open_failed"}),
    };
    let flag: i64 = connection
        .query_row(
            "SELECT COALESCE((SELECT rollback_flag FROM rollback_config \
             WHERE feature_name = ?1 ORDER BY updated_at DESC LIMIT 1), 0)",
            rusqlite::params![feature],
            |row| row.get(0),
        )
        .unwrap_or(0);
    json!({"rolled_back": flag == 1, "source": "rust"})
}

/// Rust CAS write rollback authority（与 Python 原 feature 名逐字一致）。
pub fn handle_is_rust_cas_write_rolled_back(params: &Value) -> Result<Value, DaemonRpcError> {
    let db_path = match get_str_param(params, "db_path") {
        Some(path) if !path.is_empty() => PathBuf::from(path),
        _ => default_db_path(),
    };
    Ok(query_rollback_flag(&db_path, RUST_CAS_WRITE_FEATURE))
}

/// Rust replicator query rollback authority（与 Python 原 feature 名逐字一致）。
pub fn handle_is_rust_replicator_query_rolled_back(
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let db_path = match get_str_param(params, "db_path") {
        Some(path) if !path.is_empty() => PathBuf::from(path),
        _ => default_db_path(),
    };
    Ok(query_rollback_flag(&db_path, RUST_REPLICATOR_QUERY_FEATURE))
}

/// 将 replicator refresh RPC 接入现有 Rust workspace refresh 主链。
///
/// `handle_workspace_file_refresh` 承担 peer/owned-workspace ACL、canonical
/// bytes/FD 校验、session epoch、CAS、merge、manifest 和 replicate；此 wrapper
/// 只提供 SRV-014 的方法名，不重新实现或绕过这些控制。
pub fn handle_daemon_handle_refresh<S: DaemonStateExt>(
    state: &mut S,
    peer: PeerCredential,
    params: &Value,
    received_fds: &[i32],
) -> Result<Value, DaemonRpcError> {
    state.handle_workspace_file_refresh(peer, params, received_fds)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn temp_db(tag: &str) -> (Connection, PathBuf) {
        let directory = std::env::temp_dir().join(format!(
            "srv014_{}_{}",
            tag,
            std::process::id()
        ));
        let _ = std::fs::create_dir_all(&directory);
        let path = directory.join(format!("{tag}.db"));
        let _ = std::fs::remove_file(&path);
        let connection = Connection::open(&path).unwrap();
        (connection, path)
    }

    fn seed_rollback_config(connection: &Connection) {
        connection
            .execute_batch(
                "CREATE TABLE rollback_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feature_name TEXT NOT NULL,
                    rollback_flag INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                );",
            )
            .unwrap();
    }

    #[test]
    fn cas_write_rollback_flag_set() {
        let (connection, path) = temp_db("cas_write");
        seed_rollback_config(&connection);
        connection
            .execute(
                "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES (?1, 1, 100.0)",
                rusqlite::params![RUST_CAS_WRITE_FEATURE],
            )
            .unwrap();
        drop(connection);
        let result = handle_is_rust_cas_write_rolled_back(
            &json!({"db_path": path.to_string_lossy()}),
        )
        .unwrap();
        assert_eq!(result["rolled_back"], true);
    }

    #[test]
    fn replicator_query_flag_set_and_cas_is_ignored() {
        let (connection, path) = temp_db("replicator_query");
        seed_rollback_config(&connection);
        connection
            .execute(
                "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES (?1, 1, 100.0)",
                rusqlite::params![RUST_REPLICATOR_QUERY_FEATURE],
            )
            .unwrap();
        drop(connection);
        let query = handle_is_rust_replicator_query_rolled_back(
            &json!({"db_path": path.to_string_lossy()}),
        )
        .unwrap();
        assert_eq!(query["rolled_back"], true);
        let cas = handle_is_rust_cas_write_rolled_back(
            &json!({"db_path": path.to_string_lossy()}),
        )
        .unwrap();
        assert_eq!(cas["rolled_back"], false);
    }

    #[test]
    fn missing_table_and_directory_are_fail_soft() {
        let (_connection, empty_path) = temp_db("missing_table");
        let result = handle_is_rust_cas_write_rolled_back(
            &json!({"db_path": empty_path.to_string_lossy()}),
        )
        .unwrap();
        assert_eq!(result["rolled_back"], false);

        let directory = std::env::temp_dir().join(format!("srv014_dir_{}", std::process::id()));
        let _ = std::fs::create_dir_all(&directory);
        let result = handle_is_rust_replicator_query_rolled_back(
            &json!({"db_path": directory.to_string_lossy()}),
        )
        .unwrap();
        assert_eq!(result["rolled_back"], false);
        assert_eq!(result["reason"], "db_open_failed");
    }
}
