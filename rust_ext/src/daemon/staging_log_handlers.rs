//! StagingLog rollback authority（SRV-018）。
//!
//! Python 端只保留缓存和 RPC 适配；`rollback_config` 的读取与 feature
//! 判断由 Rust daemon 执行。只读探测失败时按既有 fail-soft 合同返回
//! `rolled_back=false`，客户端不得回退到本地 SQLite。

use std::path::PathBuf;

use rusqlite::Connection;
use serde_json::{json, Value};

use super::{get_str_param, DaemonRpcError};

const RUST_STAGING_LOG_FEATURE: &str = "rust_staging_log";

fn default_callwarden_dir() -> PathBuf {
    let home = std::env::var("CALLWARDEN_HOME")
        .ok()
        .filter(|value| !value.is_empty())
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

/// 查询 `rust_staging_log` 最新 rollback flag。
///
/// 与 Python 原实现一致：数据库打不开、表不存在、查询错误或记录缺失
/// 都返回未回滚，不把只读探测失败升级为 staging log 写入失败。
pub fn handle_is_rust_staging_log_rolled_back(
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let db_path = match get_str_param(params, "db_path") {
        Some(path) if !path.is_empty() => PathBuf::from(path),
        _ => default_db_path(),
    };
    let conn = match Connection::open(&db_path) {
        Ok(connection) => connection,
        Err(_) => return Ok(json!({"rolled_back": false, "reason": "db_open_failed", "source": "rust"})),
    };
    let flag: i64 = conn
        .query_row(
            "SELECT COALESCE((SELECT rollback_flag FROM rollback_config \
             WHERE feature_name = ?1 ORDER BY updated_at DESC LIMIT 1), 0)",
            rusqlite::params![RUST_STAGING_LOG_FEATURE],
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
        let dir = std::env::temp_dir().join(format!("srv018_{}_{}", tag, std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join(format!("{tag}.db"));
        let _ = std::fs::remove_file(&path);
        (Connection::open(&path).unwrap(), path)
    }

    fn seed_rollback_config(conn: &Connection) {
        conn.execute_batch(
            "CREATE TABLE rollback_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feature_name TEXT NOT NULL,
                rollback_flag INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            )",
        )
        .unwrap();
    }

    #[test]
    fn staging_log_rollback_flag_set() {
        let (conn, path) = temp_db("set");
        seed_rollback_config(&conn);
        conn.execute(
            "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES (?1, 1, 10.0)",
            rusqlite::params![RUST_STAGING_LOG_FEATURE],
        )
        .unwrap();
        drop(conn);
        let result = handle_is_rust_staging_log_rolled_back(
            &json!({"db_path": path.to_string_lossy()}),
        )
        .unwrap();
        assert_eq!(result["rolled_back"], true);
        assert_eq!(result["source"], "rust");
        let _ = std::fs::remove_dir_all(path.parent().unwrap());
    }

    #[test]
    fn latest_rollback_row_wins() {
        let (conn, path) = temp_db("latest");
        seed_rollback_config(&conn);
        for (flag, timestamp) in [(1, 10.0), (0, 20.0)] {
            conn.execute(
                "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES (?1, ?2, ?3)",
                rusqlite::params![RUST_STAGING_LOG_FEATURE, flag, timestamp],
            )
            .unwrap();
        }
        drop(conn);
        let result = handle_is_rust_staging_log_rolled_back(
            &json!({"db_path": path.to_string_lossy()}),
        )
        .unwrap();
        assert_eq!(result["rolled_back"], false);
        let _ = std::fs::remove_dir_all(path.parent().unwrap());
    }

    #[test]
    fn missing_table_fails_soft_without_error() {
        let (conn, path) = temp_db("missing-table");
        drop(conn);
        let result = handle_is_rust_staging_log_rolled_back(
            &json!({"db_path": path.to_string_lossy()}),
        )
        .unwrap();
        assert_eq!(result["rolled_back"], false);
        let _ = std::fs::remove_dir_all(path.parent().unwrap());
    }

    #[test]
    fn directory_open_fails_soft_with_stable_reason() {
        let dir = std::env::temp_dir().join(format!("srv018_dir_{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let result = handle_is_rust_staging_log_rolled_back(
            &json!({"db_path": dir.to_string_lossy()}),
        )
        .unwrap();
        assert_eq!(result["rolled_back"], false);
        assert_eq!(result["reason"], "db_open_failed");
        let _ = std::fs::remove_dir_all(dir);
    }
}
