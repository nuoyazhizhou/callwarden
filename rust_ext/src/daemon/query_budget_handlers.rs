//! Query budget rollback authority handler（SRV-013）。
//!
//! `server/query_budget.py` 的 rollback 探测只保留 daemon RPC 薄适配；
//! `rollback_config` 的数据库读取和 feature 判断由 Rust daemon 执行。

use std::path::PathBuf;

use rusqlite::Connection;
use serde_json::{json, Value};

use super::{get_str_param, DaemonRpcError};

const RUST_DAEMON_ACL_PATH_BUDGET_FEATURE: &str = "rust_daemon_acl_path_budget";

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

/// 读取 query budget 的 rollback authority。
///
/// 与 Python 原实现保持同一条查询语义：最新 `updated_at` 行的
/// `rollback_flag == 1` 才表示已回滚。数据库不可打开、表缺失或查询失败
/// 都 fail-soft 为未回滚；客户端不得因此回退到 Python SQLite。
pub fn handle_is_rust_budget_rolled_back(params: &Value) -> Result<Value, DaemonRpcError> {
    let db_path = match get_str_param(params, "db_path") {
        Some(path) if !path.is_empty() => PathBuf::from(path),
        _ => default_db_path(),
    };
    let connection = match Connection::open(&db_path) {
        Ok(connection) => connection,
        Err(_) => return Ok(json!({"rolled_back": false, "reason": "db_open_failed"})),
    };
    let flag: i64 = connection
        .query_row(
            "SELECT COALESCE((SELECT rollback_flag FROM rollback_config \
             WHERE feature_name = ?1 ORDER BY updated_at DESC LIMIT 1), 0)",
            rusqlite::params![RUST_DAEMON_ACL_PATH_BUDGET_FEATURE],
            |row| row.get(0),
        )
        .unwrap_or(0);
    Ok(json!({"rolled_back": flag == 1, "source": "rust"}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn temp_db(tag: &str) -> (Connection, PathBuf) {
        let directory = std::env::temp_dir().join(format!(
            "srv013_{}_{}",
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
    fn budget_rollback_flag_set() {
        let (connection, path) = temp_db("flag_set");
        seed_rollback_config(&connection);
        connection
            .execute(
                "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES (?1, 1, 100.0)",
                rusqlite::params![RUST_DAEMON_ACL_PATH_BUDGET_FEATURE],
            )
            .unwrap();
        drop(connection);
        let result = handle_is_rust_budget_rolled_back(
            &json!({"db_path": path.to_string_lossy()}),
        )
        .unwrap();
        assert_eq!(result["rolled_back"], true);
        assert_eq!(result["source"], "rust");
    }

    #[test]
    fn budget_other_feature_is_ignored() {
        let (connection, path) = temp_db("other_feature");
        seed_rollback_config(&connection);
        connection
            .execute(
                "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES ('other_feature', 1, 100.0)",
                [],
            )
            .unwrap();
        drop(connection);
        let result = handle_is_rust_budget_rolled_back(
            &json!({"db_path": path.to_string_lossy()}),
        )
        .unwrap();
        assert_eq!(result["rolled_back"], false);
    }

    #[test]
    fn budget_latest_row_wins() {
        let (connection, path) = temp_db("latest");
        seed_rollback_config(&connection);
        for (flag, timestamp) in [(1, 100.0), (0, 200.0)] {
            connection
                .execute(
                    "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES (?1, ?2, ?3)",
                    rusqlite::params![RUST_DAEMON_ACL_PATH_BUDGET_FEATURE, flag, timestamp],
                )
                .unwrap();
        }
        drop(connection);
        let result = handle_is_rust_budget_rolled_back(
            &json!({"db_path": path.to_string_lossy()}),
        )
        .unwrap();
        assert_eq!(result["rolled_back"], false);
    }

    #[test]
    fn budget_missing_table_and_directory_fail_soft() {
        let (_connection, missing_table_path) = temp_db("missing_table");
        let result = handle_is_rust_budget_rolled_back(
            &json!({"db_path": missing_table_path.to_string_lossy()}),
        )
        .unwrap();
        assert_eq!(result["rolled_back"], false);

        let directory = std::env::temp_dir().join(format!("srv013_dir_{}", std::process::id()));
        let _ = std::fs::create_dir_all(&directory);
        let result = handle_is_rust_budget_rolled_back(
            &json!({"db_path": directory.to_string_lossy()}),
        )
        .unwrap();
        assert_eq!(result["rolled_back"], false);
        assert_eq!(result["reason"], "db_open_failed");
    }
}
