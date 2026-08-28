//! daemon server 面 handler（SRV-008：server daemon_server Python authority → Rust daemon）。
//!
//! 对应 `server/daemon_server.py` 的 6 个 Python 权威符号：
//! - `_is_rust_acl_rolled_back`：读权威库 `rollback_config`，判断
//!   `rust_daemon_acl_path_budget` feature 是否已回滚（60s 缓存由 Python 薄客户端侧维持）。
//! - `_is_rust_health_rolled_back`：同上，feature=`rust_daemon_health_check`。
//! - `get_registry_conn`（模块级）/ `_registry_conn`（实例方法）：原实现返回
//!   `sqlite3.Connection`。RPC 无法传递连接对象，下沉为权威元信息探测
//!   （registry.db 路径 + 存在性 + schema 就绪状态，Rust 权威执行 schema 探测），
//!   对齐 SRV-006 `handle_get_db`（RPC 无法传递 CodeGraphDB 实例 → 路径元信息）先例。
//! - `_get_workspace_resources`：原实现懒初始化 per-workspace CAS/StagingLog/Replicator
//!   进程内对象。RPC 无法传递这些对象，下沉为权威元信息探测（资源路径映射 + 存在性）。
//! - `dispatch`：原实现是 Python daemon 服务端 RPC 路由器。路由权威已由 Rust
//!   `dispatch_rpc` 承担（生产主链），handler 下沉为路由权威声明（manifest 语义）。
//!
//! 错误语义与 Python 对齐（fail-soft 只读探测）：rollback 探测对库不可打开 /
//! 表缺失 / 查询失败一律返回 `{"rolled_back": false, "reason": ...}`，绝不抛错
//!（对齐 Python `except Exception: value = False`），与 SRV-003/SRV-007 先例一致。

use std::path::PathBuf;

use rusqlite::Connection;
use serde_json::{json, Value};

use super::dispatch::{get_str_param, DaemonRpcError};

/// ACL feature 名称（对齐 Python `_is_rust_acl_rolled_back` 查询条件）。
const RUST_DAEMON_ACL_FEATURE: &str = "rust_daemon_acl_path_budget";
/// Health feature 名称（对齐 Python `_is_rust_health_rolled_back` 查询条件）。
const RUST_DAEMON_HEALTH_FEATURE: &str = "rust_daemon_health_check";

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

/// 默认 registry.db 路径（对齐 Python `config.DAEMON_REGISTRY_DB`：
/// `CW_DAEMON_DATA_ROOT` → `CALLWARDEN_DIR/daemon`；`CALLWARDEN_DAEMON_REGISTRY_DB`
/// 为测试/多库场景的显式覆盖）。
fn default_registry_db_path() -> PathBuf {
    if let Ok(v) = std::env::var("CALLWARDEN_DAEMON_REGISTRY_DB") {
        if !v.is_empty() {
            return PathBuf::from(v);
        }
    }
    if let Ok(v) = std::env::var("CW_DAEMON_DATA_ROOT") {
        if !v.is_empty() {
            return PathBuf::from(v).join("registry.db");
        }
    }
    default_callwarden_dir().join("daemon").join("registry.db")
}

/// rollback_config 只读探测（共享实现）：
/// `WHERE feature_name = ? ORDER BY updated_at DESC LIMIT 1`，行缺失/NULL → 未回滚。
/// fail-soft：库不可打开 / 表缺失 / 查询失败 → `{"rolled_back": false, "reason": ...}`。
fn query_rollback_flag(db_path: &PathBuf, feature: &str) -> Value {
    let conn = match Connection::open(db_path) {
        Ok(c) => c,
        Err(_) => {
            return json!({"rolled_back": false, "reason": "db_open_failed"});
        }
    };
    let value: i64 = conn
        .query_row(
            "SELECT COALESCE((SELECT rollback_flag FROM rollback_config \
             WHERE feature_name = ?1 ORDER BY updated_at DESC LIMIT 1), 0)",
            rusqlite::params![feature],
            |row| row.get(0),
        )
        .unwrap_or(0);
    json!({"rolled_back": value == 1})
}

/// `mcp.daemon_server.is_rust_acl_rolled_back` —— 读 daemon 权威
/// `rollback_config`，判断 `rust_daemon_acl_path_budget` feature 是否已回滚
///（对齐 Python `_is_rust_acl_rolled_back` 的 SQL 语义）。
/// `db_path` 参数可选（缺省用户级单库），供测试与多库场景显式指定。
pub fn handle_is_rust_acl_rolled_back(params: &Value) -> Result<Value, DaemonRpcError> {
    let db_path = match get_str_param(params, "db_path") {
        Some(p) if !p.is_empty() => PathBuf::from(p),
        _ => default_db_path(),
    };
    Ok(query_rollback_flag(&db_path, RUST_DAEMON_ACL_FEATURE))
}

/// `mcp.daemon_server.is_rust_health_rolled_back` —— 读 daemon 权威
/// `rollback_config`，判断 `rust_daemon_health_check` feature 是否已回滚
///（对齐 Python `_is_rust_health_rolled_back` 的 SQL 语义）。
pub fn handle_is_rust_health_rolled_back(params: &Value) -> Result<Value, DaemonRpcError> {
    let db_path = match get_str_param(params, "db_path") {
        Some(p) if !p.is_empty() => PathBuf::from(p),
        _ => default_db_path(),
    };
    Ok(query_rollback_flag(&db_path, RUST_DAEMON_HEALTH_FEATURE))
}

/// registry 连接权威元信息探测（`get_registry_conn` / `_registry_conn` 共享）。
/// RPC 无法传递 sqlite3.Connection；Rust 权威打开 registry.db 探测 schema 就绪
///（`daemon_workspaces` 表存在性），返回归一化元信息。
/// fail-soft：库不可打开 → `{"registry_db": ..., "exists": false,
/// "schema_ready": false, "reason": "db_open_failed"}`，绝不抛错。
fn registry_conn_meta(registry_db: &PathBuf, source: &str) -> Value {
    let exists = registry_db.exists();
    let mut schema_ready = false;
    let mut reason: Option<&str> = None;
    match Connection::open(registry_db) {
        Ok(conn) => {
            schema_ready = conn
                .query_row(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='daemon_workspaces'",
                    [],
                    |row| row.get::<_, i64>(0),
                )
                .map(|n| n > 0)
                .unwrap_or(false);
        }
        Err(_) => {
            reason = Some("db_open_failed");
        }
    }
    let mut out = json!({
        "registry_db": registry_db.to_string_lossy(),
        "exists": exists,
        "schema_ready": schema_ready,
        "source": source,
    });
    if let Some(r) = reason {
        out["reason"] = json!(r);
    }
    out
}

/// `mcp.daemon_server.get_registry_conn` —— 模块级 `get_registry_conn` 下沉
///（权威元信息探测，对齐 SRV-006 `handle_get_db` 先例）。
/// `registry_db` 参数可选（缺省 `config.DAEMON_REGISTRY_DB` 语义路径）。
pub fn handle_get_registry_conn(params: &Value) -> Result<Value, DaemonRpcError> {
    let registry_db = match get_str_param(params, "registry_db") {
        Some(p) if !p.is_empty() => PathBuf::from(p),
        _ => default_registry_db_path(),
    };
    Ok(registry_conn_meta(&registry_db, "module"))
}

/// `mcp.daemon_server.registry_conn` —— `EnterpriseDaemonService._registry_conn`
/// 下沉（权威元信息探测，source=instance 区分实例方法语义）。
pub fn handle_registry_conn(params: &Value) -> Result<Value, DaemonRpcError> {
    let registry_db = match get_str_param(params, "registry_db") {
        Some(p) if !p.is_empty() => PathBuf::from(p),
        _ => default_registry_db_path(),
    };
    Ok(registry_conn_meta(&registry_db, "instance"))
}

/// `mcp.daemon_server.get_workspace_resources` —— `_get_workspace_resources` 下沉。
/// 原实现懒初始化 per-workspace 进程内对象（cas_conn/ws_conn/StagingLog/Replicator），
/// RPC 无法传递；下沉为权威元信息探测：workspace 资源目录路径映射 + 存在性。
/// fail-closed：`workspace_instance_id` 缺失/为空 → `invalid_params`
///（对齐 Python dispatch `缺少 workspace_instance_id` 语义）。
pub fn handle_get_workspace_resources(params: &Value) -> Result<Value, DaemonRpcError> {
    let workspace_id = get_str_param(params, "workspace_instance_id")
        .filter(|s| !s.is_empty())
        .ok_or_else(|| DaemonRpcError::invalid_params("缺少 workspace_instance_id".to_string()))?;
    let data_root = match get_str_param(params, "data_root") {
        Some(p) if !p.is_empty() => PathBuf::from(p),
        _ => {
            // 对齐 Python `EnterpriseDaemonService._data_root`：
            // registry_db 所在目录 + "enterprise"（默认 ~/.callwarden/daemon/enterprise）
            if let Ok(v) = std::env::var("CW_DAEMON_DATA_ROOT") {
                if !v.is_empty() {
                    PathBuf::from(v).join("enterprise")
                } else {
                    default_callwarden_dir().join("daemon").join("enterprise")
                }
            } else {
                default_callwarden_dir().join("daemon").join("enterprise")
            }
        }
    };
    let ws_dir = data_root.join(&workspace_id);
    let cas_db = ws_dir.join("cas.db");
    let ws_db = ws_dir.join("workspace.db");
    let staging_log = ws_dir.join("staging.log");
    Ok(json!({
        "workspace_instance_id": workspace_id,
        "ws_dir": ws_dir.to_string_lossy(),
        "cas_db_path": cas_db.to_string_lossy(),
        "ws_db_path": ws_db.to_string_lossy(),
        "staging_log_path": staging_log.to_string_lossy(),
        "cas_db_exists": cas_db.exists(),
        "ws_db_exists": ws_db.exists(),
        "staging_log_exists": staging_log.exists(),
    }))
}

/// `mcp.daemon_server.dispatch` —— Python `EnterpriseDaemonService.dispatch`
/// 路由权威声明。生产 RPC 路由权威已由 Rust `dispatch_rpc` 承担；
/// 本 handler 返回路由权威 manifest（只读声明，不执行任何业务路由）。
pub fn handle_dispatch(_params: &Value) -> Result<Value, DaemonRpcError> {
    Ok(json!({
        "authority": "rust_dispatch",
        "python_dispatch_role": "compat_fallback",
        "admin_only_enforced": true,
        "methods": [
            "ping", "workspace.register", "workspace.list", "workspace.connect",
            "health", "schema.version", "backup", "restore", "gc.snapshots",
            "metrics.snapshot", "metrics.prometheus",
            "mount.register", "mount.list", "mount.delete",
            "gc.cas", "workspace.status", "workspace.file.refresh",
            "workspace.recover", "snapshot.publish", "workspace.refresh",
            "query.stats", "query.symbol", "query.search", "query.callers",
            "query.callees", "query.call_chain_down", "query.topological_order",
            "query.detect_cycles",
        ],
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn tmp_db(tag: &str) -> (Connection, PathBuf) {
        let dir = std::env::temp_dir().join(format!("srv008_{}_{}", tag, std::process::id()));
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
    fn test_acl_flag_set() {
        let (conn, path) = tmp_db("acl_flag_set");
        seed_rollback_config(&conn);
        conn.execute(
            "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES (?1, 1, 100.0)",
            rusqlite::params![RUST_DAEMON_ACL_FEATURE],
        )
        .unwrap();
        drop(conn);
        let res =
            handle_is_rust_acl_rolled_back(&json!({"db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["rolled_back"], true);
    }

    #[test]
    fn test_acl_flag_unset_and_other_feature_ignored() {
        let (conn, path) = tmp_db("acl_flag_unset");
        seed_rollback_config(&conn);
        // 其他 feature 置 1 不得影响 acl 判定
        conn.execute(
            "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES ('other_feature', 1, 100.0)",
            [],
        )
        .unwrap();
        drop(conn);
        let res =
            handle_is_rust_acl_rolled_back(&json!({"db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["rolled_back"], false);
    }

    #[test]
    fn test_acl_table_missing_fail_soft() {
        let (_conn, path) = tmp_db("acl_table_missing");
        let res =
            handle_is_rust_acl_rolled_back(&json!({"db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["rolled_back"], false);
    }

    #[test]
    fn test_health_flag_set() {
        let (conn, path) = tmp_db("health_flag_set");
        seed_rollback_config(&conn);
        conn.execute(
            "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES (?1, 1, 100.0)",
            rusqlite::params![RUST_DAEMON_HEALTH_FEATURE],
        )
        .unwrap();
        drop(conn);
        let res =
            handle_is_rust_health_rolled_back(&json!({"db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["rolled_back"], true);
    }

    #[test]
    fn test_health_latest_row_wins() {
        // ORDER BY updated_at DESC LIMIT 1：最新行生效（先回滚后恢复 → 未回滚）
        let (conn, path) = tmp_db("health_latest");
        seed_rollback_config(&conn);
        conn.execute(
            "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES (?1, 1, 100.0)",
            rusqlite::params![RUST_DAEMON_HEALTH_FEATURE],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) VALUES (?1, 0, 200.0)",
            rusqlite::params![RUST_DAEMON_HEALTH_FEATURE],
        )
        .unwrap();
        drop(conn);
        let res =
            handle_is_rust_health_rolled_back(&json!({"db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["rolled_back"], false);
    }

    #[test]
    fn test_get_registry_conn_schema_ready() {
        let (conn, path) = tmp_db("reg_ready");
        conn.execute_batch("CREATE TABLE daemon_workspaces (workspace_id INTEGER PRIMARY KEY);")
            .unwrap();
        drop(conn);
        let res =
            handle_get_registry_conn(&json!({"registry_db": path.to_string_lossy()})).unwrap();
        assert_eq!(res["exists"], true);
        assert_eq!(res["schema_ready"], true);
        assert_eq!(res["source"], "module");
    }

    #[test]
    fn test_get_registry_conn_schema_missing() {
        let (conn, path) = tmp_db("reg_missing");
        drop(conn);
        let res =
            handle_get_registry_conn(&json!({"registry_db": path.to_string_lossy()})).unwrap();
        assert_eq!(res["exists"], true);
        assert_eq!(res["schema_ready"], false);
    }

    #[test]
    fn test_registry_conn_instance_source() {
        let (conn, path) = tmp_db("reg_instance");
        drop(conn);
        let res = handle_registry_conn(&json!({"registry_db": path.to_string_lossy()})).unwrap();
        assert_eq!(res["source"], "instance");
    }

    #[test]
    fn test_get_workspace_resources_meta() {
        let dir = std::env::temp_dir().join(format!("srv008_wsres_{}", std::process::id()));
        let ws_dir = dir.join("ws-abc");
        let _ = std::fs::create_dir_all(&ws_dir);
        std::fs::write(ws_dir.join("cas.db"), b"").unwrap();
        let res = handle_get_workspace_resources(&json!({
            "workspace_instance_id": "ws-abc",
            "data_root": dir.to_string_lossy(),
        }))
        .unwrap();
        assert_eq!(res["cas_db_exists"], true);
        assert_eq!(res["ws_db_exists"], false);
        assert_eq!(res["staging_log_exists"], false);
    }

    #[test]
    fn test_get_workspace_resources_missing_id() {
        let err = handle_get_workspace_resources(&json!({})).unwrap_err();
        assert_eq!(err.code, "invalid_params");
    }

    #[test]
    fn test_dispatch_manifest() {
        let res = handle_dispatch(&json!({})).unwrap();
        assert_eq!(res["authority"], "rust_dispatch");
        assert_eq!(res["admin_only_enforced"], true);
        assert!(res["methods"].as_array().unwrap().len() >= 20);
    }
}
