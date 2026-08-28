//! health check 面 handler（SRV-010：server health check Python authority → Rust daemon）。
//!
//! 对应 `server/health_check.py` 的 4 个 direct authority（sqlite3.connect）函数：
//! - `handle_check_db_registry`：承接 `HealthChecker._check_db_registry`——
//!   registry DB 连通性 + `daemon_workspaces` 表存在性检查（只读）。
//! - `handle_recover_workspace_registry`：承接 `RecoveryHandler._recover_workspace_registry`——
//!   验证表结构、统计 active workspace、更新 `last_active_at`（写，恢复语义）。
//! - `handle_recover_cas_db`：承接 `RecoveryHandler._recover_cas_db`——
//!   CAS DB 可访问性探测（只读，缺库视为首次启动 healthy）。
//! - `handle_recover_stale_jobs`：承接 `RecoveryHandler._recover_stale_jobs`——
//!   将 registry `jobs` 表 running 状态 job 标记为 failed（写，daemon 重启恢复语义）。
//!
//! 权威归属说明：生产链健康检查权威原已由 Rust `health.rs`（G14：
//! `HealthChecker::check_all` / `RecoveryHandler::recover`，经
//! `callwarden_core.health_check_all` PyO3 短路 daemon_server 生产路径）承担；
//! 本卡将上述 4 个 Python direct authority 函数的 daemon RPC 形态下沉，
//! 消除 Python sqlite3.connect 残留的权威接缝。
//!
//! 返回形态对齐 Python：`{name, status, message, details}` + `source:"rust"`。
//! 错误语义（fail-soft 对齐 Python）：DB 缺失/表缺失/连接失败一律归一化为
//! healthy/degraded/unhealthy 状态返回，绝不抛错，与 SRV-003~009 先例一致。

use std::path::{Path, PathBuf};

use rusqlite::Connection;
use serde_json::{json, Value};

use super::dispatch::{get_str_param, DaemonRpcError};

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

/// 默认权威 registry.db 路径：`CALLWARDEN_DAEMON_REGISTRY_DB` /
/// `CW_DAEMON_REGISTRY_DB` 显式覆盖 → `CW_DAEMON_DATA_ROOT/registry.db` →
/// `~/.callwarden/daemon/registry.db`（目录语义对齐 SRV-008/009）。
fn default_registry_db_path() -> PathBuf {
    for key in ["CALLWARDEN_DAEMON_REGISTRY_DB", "CW_DAEMON_REGISTRY_DB"] {
        if let Ok(v) = std::env::var(key) {
            if !v.is_empty() {
                return PathBuf::from(v);
            }
        }
    }
    if let Ok(v) = std::env::var("CW_DAEMON_DATA_ROOT") {
        if !v.is_empty() {
            return PathBuf::from(v).join("registry.db");
        }
    }
    default_callwarden_dir().join("daemon").join("registry.db")
}

/// 默认权威 cas.db 路径：`CALLWARDEN_CAS_DB` 显式覆盖 →
/// `CW_DAEMON_DATA_ROOT/cas.db` → `~/.callwarden/daemon/cas.db`
///（对齐 Python `DaemonConfig.cas_db_path` = data_root/cas.db 语义）。
fn default_cas_db_path() -> PathBuf {
    if let Ok(v) = std::env::var("CALLWARDEN_CAS_DB") {
        if !v.is_empty() {
            return PathBuf::from(v);
        }
    }
    if let Ok(v) = std::env::var("CW_DAEMON_DATA_ROOT") {
        if !v.is_empty() {
            return PathBuf::from(v).join("cas.db");
        }
    }
    default_callwarden_dir().join("daemon").join("cas.db")
}

/// 列出已打开连接的表名（sqlite_master）。
fn list_tables(conn: &Connection) -> Vec<String> {
    conn.prepare("SELECT name FROM sqlite_master WHERE type='table'")
        .ok()
        .and_then(|mut stmt| {
            stmt.query_map([], |row| row.get::<_, String>(0))
                .ok()
                .map(|rows| rows.filter_map(|r| r.ok()).collect())
        })
        .unwrap_or_default()
}

fn registry_db_param(params: &Value) -> PathBuf {
    match get_str_param(params, "registry_db_path") {
        Some(p) if !p.is_empty() => PathBuf::from(p),
        _ => default_registry_db_path(),
    }
}

/// `mcp.health_check.check_db_registry` —— `HealthChecker._check_db_registry` 下沉。
/// 只读检查 registry DB 连通性 + `daemon_workspaces` 表存在性。
/// 归一化：文件缺失/连接失败 → unhealthy；缺表 → degraded；就绪 → healthy。
pub fn handle_check_db_registry(params: &Value) -> Result<Value, DaemonRpcError> {
    let db_path = registry_db_param(params);

    if !Path::new(&db_path).is_file() {
        return Ok(json!({
            "name": "db_registry",
            "status": "unhealthy",
            "message": format!("registry DB not found: {}", db_path.to_string_lossy()),
            "details": {},
            "source": "rust",
        }));
    }

    let conn = match Connection::open(&db_path) {
        Ok(c) => c,
        Err(e) => {
            return Ok(json!({
                "name": "db_registry",
                "status": "unhealthy",
                "message": format!("DB error: {}", e),
                "details": {},
                "source": "rust",
            }));
        }
    };
    let _ = conn.execute_batch("PRAGMA busy_timeout=5000");
    let tables = list_tables(&conn);
    if !tables.iter().any(|t| t == "daemon_workspaces") {
        return Ok(json!({
            "name": "db_registry",
            "status": "degraded",
            "message": "daemon_workspaces table missing",
            "details": {"tables": tables},
            "source": "rust",
        }));
    }
    Ok(json!({
        "name": "db_registry",
        "status": "healthy",
        "message": format!("OK ({} tables)", tables.len()),
        "details": {"tables": tables},
        "source": "rust",
    }))
}

/// `mcp.health_check.recover_workspace_registry` ——
/// `RecoveryHandler._recover_workspace_registry` 下沉（写，恢复语义）。
/// 验证 `daemon_workspaces` 表、统计 active workspace、更新 `last_active_at`
/// 标记 daemon 已恢复。归一化：库缺失 → degraded（首次注册时创建）；
/// 缺表 → unhealthy；就绪 → healthy + `details.active_workspaces`。
pub fn handle_recover_workspace_registry(params: &Value) -> Result<Value, DaemonRpcError> {
    let db_path = registry_db_param(params);

    if !Path::new(&db_path).is_file() {
        return Ok(json!({
            "name": "workspace_registry",
            "status": "degraded",
            "message": "registry DB not found, will be created on first register",
            "source": "rust",
        }));
    }

    let conn = match Connection::open(&db_path) {
        Ok(c) => c,
        Err(e) => {
            return Ok(json!({
                "name": "workspace_registry",
                "status": "unhealthy",
                "message": format!("DB error: {}", e),
                "source": "rust",
            }));
        }
    };
    let _ = conn.execute_batch("PRAGMA busy_timeout=5000");
    let tables = list_tables(&conn);
    if !tables.iter().any(|t| t == "daemon_workspaces") {
        return Ok(json!({
            "name": "workspace_registry",
            "status": "unhealthy",
            "message": "daemon_workspaces table missing",
            "source": "rust",
        }));
    }
    let count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM daemon_workspaces WHERE status='active'",
            [],
            |row| row.get(0),
        )
        .unwrap_or(0);
    // 对齐 Python：UPDATE daemon_workspaces SET last_active_at = ? WHERE status='active'
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let _ = conn.execute(
        "UPDATE daemon_workspaces SET last_active_at = ? WHERE status = 'active'",
        rusqlite::params![now],
    );
    Ok(json!({
        "name": "workspace_registry",
        "status": "healthy",
        "message": format!("recovered {} active workspaces", count),
        "details": {"active_workspaces": count},
        "source": "rust",
    }))
}

/// `mcp.health_check.recover_cas_db` —— `RecoveryHandler._recover_cas_db` 下沉。
/// CAS DB 可访问性探测（只读短连接）。归一化对齐 Python：文件缺失 →
/// healthy（首次启动，首次使用时创建）；可访问 → healthy；连接失败 → unhealthy。
pub fn handle_recover_cas_db(params: &Value) -> Result<Value, DaemonRpcError> {
    let db_path = match get_str_param(params, "cas_db_path") {
        Some(p) if !p.is_empty() => PathBuf::from(p),
        _ => default_cas_db_path(),
    };

    if !Path::new(&db_path).is_file() {
        // CAS DB 不存在是正常的（首次启动）
        return Ok(json!({
            "name": "cas_db",
            "status": "healthy",
            "message": "CAS DB not found, will be created on first use",
            "source": "rust",
        }));
    }

    match Connection::open_with_flags(&db_path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY) {
        Ok(conn) => match conn.execute_batch("SELECT 1") {
            Ok(_) => Ok(json!({
                "name": "cas_db",
                "status": "healthy",
                "message": "CAS DB accessible",
                "source": "rust",
            })),
            Err(e) => Ok(json!({
                "name": "cas_db",
                "status": "unhealthy",
                "message": format!("CAS DB error: {}", e),
                "source": "rust",
            })),
        },
        Err(e) => Ok(json!({
            "name": "cas_db",
            "status": "unhealthy",
            "message": format!("CAS DB error: {}", e),
            "source": "rust",
        })),
    }
}

/// `mcp.health_check.recover_stale_jobs` —— `RecoveryHandler._recover_stale_jobs` 下沉
///（写，daemon 重启恢复语义）。将 registry `jobs` 表 running 状态 job 标记为
/// failed（daemon 重启后不会再完成）。归一化：registry 缺失 / 无 jobs 表 →
/// healthy（无 stale 可清理）；清理成功 → healthy + `details.stale_jobs_cleaned`；
/// 异常 → degraded，绝不抛错。
pub fn handle_recover_stale_jobs(params: &Value) -> Result<Value, DaemonRpcError> {
    let db_path = registry_db_param(params);

    if !Path::new(&db_path).is_file() {
        return Ok(json!({
            "name": "stale_jobs",
            "status": "healthy",
            "message": "no registry DB, no stale jobs",
            "source": "rust",
        }));
    }

    let conn = match Connection::open(&db_path) {
        Ok(c) => c,
        Err(e) => {
            return Ok(json!({
                "name": "stale_jobs",
                "status": "degraded",
                "message": format!("stale job cleanup error: {}", e),
                "source": "rust",
            }));
        }
    };
    let _ = conn.execute_batch("PRAGMA busy_timeout=5000");
    let has_jobs_table = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='jobs'",
            [],
            |row| row.get::<_, i64>(0),
        )
        .unwrap_or(0)
        > 0;
    if !has_jobs_table {
        return Ok(json!({
            "name": "stale_jobs",
            "status": "healthy",
            "message": "no jobs table, no stale jobs",
            "source": "rust",
        }));
    }
    let stale_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM jobs WHERE status = 'running'",
            [],
            |row| row.get(0),
        )
        .unwrap_or(0);
    if stale_count > 0 {
        // 对齐 Python：标记为 failed（daemon 重启中断）
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        let res = conn.execute(
            "UPDATE jobs SET status = 'failed', \
             error = 'daemon restarted, job interrupted', finished_at = ? \
             WHERE status = 'running'",
            rusqlite::params![now],
        );
        if res.is_err() {
            return Ok(json!({
                "name": "stale_jobs",
                "status": "degraded",
                "message": "stale job cleanup error: update failed",
                "source": "rust",
            }));
        }
    }
    Ok(json!({
        "name": "stale_jobs",
        "status": "healthy",
        "message": format!("cleaned {} stale jobs", stale_count),
        "details": {"stale_jobs_cleaned": stale_count},
        "source": "rust",
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_db(tag: &str) -> (PathBuf, PathBuf) {
        let dir = std::env::temp_dir().join(format!("srv010_{}_{}", tag, std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join(format!("{tag}.db"));
        let _ = std::fs::remove_file(&path);
        (dir, path)
    }

    fn make_registry_with_workspaces(path: &Path) {
        let conn = Connection::open(path).unwrap();
        conn.execute_batch(
            "CREATE TABLE daemon_workspaces (
                workspace_id INTEGER PRIMARY KEY,
                workspace_instance_id TEXT,
                status TEXT DEFAULT 'active',
                last_active_at REAL
             );
             INSERT INTO daemon_workspaces
                 (workspace_id, workspace_instance_id, status, last_active_at)
             VALUES (1, 'ws-1', 'active', 0.0),
                    (2, 'ws-2', 'archived', 0.0);",
        )
        .unwrap();
    }

    #[test]
    fn test_check_db_registry_healthy() {
        let (_dir, path) = tmp_db("check_ok");
        make_registry_with_workspaces(&path);
        let res =
            handle_check_db_registry(&json!({"registry_db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["status"], "healthy");
        assert_eq!(res["name"], "db_registry");
        assert_eq!(res["source"], "rust");
        assert!(res["message"].as_str().unwrap().contains("tables"));
    }

    #[test]
    fn test_check_db_registry_missing_file_unhealthy() {
        let (dir, path) = tmp_db("check_missing");
        let _ = dir;
        let res =
            handle_check_db_registry(&json!({"registry_db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["status"], "unhealthy");
        assert!(res["message"].as_str().unwrap().contains("not found"));
    }

    #[test]
    fn test_check_db_registry_missing_table_degraded() {
        let (_dir, path) = tmp_db("check_notable");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch("CREATE TABLE other_table (id INTEGER);")
            .unwrap();
        drop(conn);
        let res =
            handle_check_db_registry(&json!({"registry_db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["status"], "degraded");
        assert!(res["message"]
            .as_str()
            .unwrap()
            .contains("daemon_workspaces"));
    }

    #[test]
    fn test_recover_workspace_registry_updates_last_active_at() {
        let (_dir, path) = tmp_db("ws_recover");
        make_registry_with_workspaces(&path);
        let res =
            handle_recover_workspace_registry(&json!({"registry_db_path": path.to_string_lossy()}))
                .unwrap();
        assert_eq!(res["status"], "healthy");
        assert_eq!(res["details"]["active_workspaces"], 1);
        // 权威核验：active workspace 的 last_active_at 已更新
        let conn = Connection::open(&path).unwrap();
        let lat: f64 = conn
            .query_row(
                "SELECT last_active_at FROM daemon_workspaces WHERE workspace_id=1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(lat > 0.0);
        // archived workspace 不受影响
        let lat2: f64 = conn
            .query_row(
                "SELECT last_active_at FROM daemon_workspaces WHERE workspace_id=2",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(lat2, 0.0);
    }

    #[test]
    fn test_recover_workspace_registry_missing_degraded() {
        let (_dir, path) = tmp_db("ws_missing");
        let res =
            handle_recover_workspace_registry(&json!({"registry_db_path": path.to_string_lossy()}))
                .unwrap();
        assert_eq!(res["status"], "degraded");
    }

    #[test]
    fn test_recover_workspace_registry_missing_table_unhealthy() {
        let (_dir, path) = tmp_db("ws_notable");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch("CREATE TABLE other_table (id INTEGER);")
            .unwrap();
        drop(conn);
        let res =
            handle_recover_workspace_registry(&json!({"registry_db_path": path.to_string_lossy()}))
                .unwrap();
        assert_eq!(res["status"], "unhealthy");
    }

    #[test]
    fn test_recover_cas_db_missing_is_healthy_first_use() {
        // 对齐 Python：CAS DB 不存在是正常的（首次启动）
        let (_dir, path) = tmp_db("cas_missing");
        let res = handle_recover_cas_db(&json!({"cas_db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["status"], "healthy");
        assert!(res["message"].as_str().unwrap().contains("first use"));
    }

    #[test]
    fn test_recover_cas_db_accessible() {
        let (_dir, path) = tmp_db("cas_ok");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch("CREATE TABLE cas_entries (id INTEGER);")
            .unwrap();
        drop(conn);
        let res = handle_recover_cas_db(&json!({"cas_db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["status"], "healthy");
        assert_eq!(res["message"], "CAS DB accessible");
    }

    #[test]
    fn test_recover_stale_jobs_cleans_running() {
        let (_dir, path) = tmp_db("stale_clean");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch(
            "CREATE TABLE daemon_workspaces (workspace_id INTEGER PRIMARY KEY, status TEXT);
             CREATE TABLE jobs (job_id TEXT PRIMARY KEY, status TEXT, error TEXT, finished_at REAL);
             INSERT INTO jobs VALUES ('J-1', 'running', NULL, NULL),
                                     ('J-2', 'running', NULL, NULL),
                                     ('J-3', 'completed', NULL, 12345.0);",
        )
        .unwrap();
        drop(conn);
        let res = handle_recover_stale_jobs(&json!({"registry_db_path": path.to_string_lossy()}))
            .unwrap();
        assert_eq!(res["status"], "healthy");
        assert_eq!(res["details"]["stale_jobs_cleaned"], 2);
        // 权威核验：running → failed，completed 不受影响
        let conn = Connection::open(&path).unwrap();
        let s1: String = conn
            .query_row("SELECT status FROM jobs WHERE job_id='J-1'", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(s1, "failed");
        let err1: String = conn
            .query_row("SELECT error FROM jobs WHERE job_id='J-1'", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert!(err1.contains("daemon restarted"));
        let s3: String = conn
            .query_row("SELECT status FROM jobs WHERE job_id='J-3'", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(s3, "completed");
    }

    #[test]
    fn test_recover_stale_jobs_no_jobs_table() {
        let (_dir, path) = tmp_db("stale_notable");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch("CREATE TABLE daemon_workspaces (id INTEGER);")
            .unwrap();
        drop(conn);
        let res = handle_recover_stale_jobs(&json!({"registry_db_path": path.to_string_lossy()}))
            .unwrap();
        assert_eq!(res["status"], "healthy");
        assert!(res["message"].as_str().unwrap().contains("no jobs table"));
    }
}
