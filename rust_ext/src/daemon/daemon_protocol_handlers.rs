//! daemon protocol 面 handler（SRV-007：server daemon_protocol Python authority → Rust daemon）。
//!
//! 对应 `server/daemon_protocol.py` 中唯一直接 open SQLite 的 Python 权威符号：
//! - `_is_rust_protocol_rolled_back`：读权威库 `rollback_config`，判断
//!   `rust_daemon_protocol` feature 是否已回滚（60s 缓存由 Python 薄客户端侧维持）。
//!
//! 错误语义与 Python 对齐（fail-soft 只读探测）：Python 原实现对任何异常
//! `except → False`（视为未回滚）。下沉后 handler 对库不可打开 / 表缺失 /
//! 查询失败一律返回 `{"rolled_back": false, "reason": ...}`，绝不抛错，
//! 与 SRV-003 `mcp.backup_restore.is_rust_backup_rolled_back` 先例一致。

use std::path::PathBuf;

use rusqlite::Connection;
use serde_json::{json, Value};

use super::dispatch::{get_str_param, DaemonRpcError};

/// feature 名称（对齐 Python `_is_rust_protocol_rolled_back` 查询条件）。
const RUST_DAEMON_PROTOCOL_FEATURE: &str = "rust_daemon_protocol";

/// 默认 `~/.callwarden` 目录（对齐 Python `config.CALLWARDEN_DIR`）。
fn default_callwarden_dir() -> PathBuf {
    let home = std::env::var("CALLWARDEN_HOME")
        .ok()
        .filter(|v| !v.is_empty())
        .or_else(|| std::env::var("USERPROFILE").ok())
        .or_else(|| std::env::var("HOME").ok())
        .unwrap_or_default();
    PathBuf::from(home).join(".callwarden")
}

/// 默认用户级单库路径（对齐 Python `config.DB_PATH`：
/// `CALLWARDEN_DB` 环境变量 → `~/.callwarden/callwarden.db`）。
fn default_db_path() -> PathBuf {
    if let Ok(v) = std::env::var("CALLWARDEN_DB") {
        if !v.is_empty() {
            return PathBuf::from(v);
        }
    }
    default_callwarden_dir().join("callwarden.db")
}

/// `mcp.daemon_protocol.is_rust_protocol_rolled_back` —— 读 daemon 权威
/// `rollback_config`，判断 `rust_daemon_protocol` feature 是否已回滚
/// （对齐 Python `_is_rust_protocol_rolled_back` 的 SQL 语义：
/// `WHERE feature_name = ? ORDER BY updated_at DESC LIMIT 1`，行缺失/NULL → 未回滚）。
///
/// fail-soft：库不可打开 / 表缺失 / 查询失败 → `{"rolled_back": false, "reason": ...}`
/// （对齐 Python `except Exception: value = False`，只读探测绝不抛错）。
/// `db_path` 参数可选（缺省用户级单库），供测试与多库场景显式指定。
pub fn handle_is_rust_protocol_rolled_back(params: &Value) -> Result<Value, DaemonRpcError> {
    let db_path = match get_str_param(params, "db_path") {
        Some(p) if !p.is_empty() => PathBuf::from(p),
        _ => default_db_path(),
    };
    let conn = match Connection::open(&db_path) {
        Ok(c) => c,
        Err(_) => {
            return Ok(json!({"rolled_back": false, "reason": "db_open_failed"}));
        }
    };
    let value: i64 = conn
        .query_row(
            "SELECT COALESCE((SELECT rollback_flag FROM rollback_config \
             WHERE feature_name = ?1 ORDER BY updated_at DESC LIMIT 1), 0)",
            rusqlite::params![RUST_DAEMON_PROTOCOL_FEATURE],
            |row| row.get(0),
        )
        .unwrap_or(0);
    Ok(json!({"rolled_back": value == 1}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn tmp_db(tag: &str) -> (Connection, PathBuf) {
        let dir = std::env::temp_dir().join(format!("srv007_{}_{}", tag, std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join(format!("{tag}.db"));
        let _ = std::fs::remove_file(&path);
        let conn = Connection::open(&path).unwrap();
        (conn, path)
    }

    fn seed_rollback_config(conn: &Connection) {
        conn.execute_batch(
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
    fn test_rolled_back_flag_set() {
        let (conn, path) = tmp_db("flag_set");
        seed_rollback_config(&conn);
        conn.execute(
            "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES (?1, 1, 100.0)",
            rusqlite::params![RUST_DAEMON_PROTOCOL_FEATURE],
        )
        .unwrap();
        drop(conn);
        let res = handle_is_rust_protocol_rolled_back(&json!({"db_path": path.to_string_lossy()}))
            .unwrap();
        assert_eq!(res["rolled_back"], true);
    }

    #[test]
    fn test_rolled_back_flag_unset() {
        let (conn, path) = tmp_db("flag_unset");
        seed_rollback_config(&conn);
        conn.execute(
            "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES (?1, 0, 100.0)",
            rusqlite::params![RUST_DAEMON_PROTOCOL_FEATURE],
        )
        .unwrap();
        drop(conn);
        let res = handle_is_rust_protocol_rolled_back(&json!({"db_path": path.to_string_lossy()}))
            .unwrap();
        assert_eq!(res["rolled_back"], false);
    }

    #[test]
    fn test_latest_row_wins() {
        // ORDER BY updated_at DESC LIMIT 1：最新行生效（先回滚后恢复 → 未回滚）
        let (conn, path) = tmp_db("latest_wins");
        seed_rollback_config(&conn);
        conn.execute(
            "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES (?1, 1, 100.0)",
            rusqlite::params![RUST_DAEMON_PROTOCOL_FEATURE],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES (?1, 0, 200.0)",
            rusqlite::params![RUST_DAEMON_PROTOCOL_FEATURE],
        )
        .unwrap();
        drop(conn);
        let res = handle_is_rust_protocol_rolled_back(&json!({"db_path": path.to_string_lossy()}))
            .unwrap();
        assert_eq!(res["rolled_back"], false);
    }

    #[test]
    fn test_row_missing_fail_soft() {
        let (conn, path) = tmp_db("row_missing");
        seed_rollback_config(&conn);
        drop(conn);
        let res = handle_is_rust_protocol_rolled_back(&json!({"db_path": path.to_string_lossy()}))
            .unwrap();
        assert_eq!(res["rolled_back"], false);
    }

    #[test]
    fn test_table_missing_fail_soft() {
        let (_conn, path) = tmp_db("table_missing");
        let res = handle_is_rust_protocol_rolled_back(&json!({"db_path": path.to_string_lossy()}))
            .unwrap();
        assert_eq!(res["rolled_back"], false);
    }

    #[test]
    fn test_other_feature_ignored() {
        let (conn, path) = tmp_db("other_feature");
        seed_rollback_config(&conn);
        conn.execute(
            "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES ('other_feature', 1, 100.0)",
            [],
        )
        .unwrap();
        drop(conn);
        let res = handle_is_rust_protocol_rolled_back(&json!({"db_path": path.to_string_lossy()}))
            .unwrap();
        assert_eq!(res["rolled_back"], false);
    }
}
