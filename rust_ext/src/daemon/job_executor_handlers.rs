//! job executor 面 handler（SRV-011：server job executor Python authority → Rust daemon）。
//!
//! 对应 `server/job_executor.py::JobExecutor.start`（唯一指定的 Python
//! direct authority 函数，L193 `sqlite3.connect`）：
//! - `handle_start`：承接 `JobExecutor.start` 的权威初始化形态——打开/创建
//!   jobs DB、设置批次10 完整 PRAGMA 集（对齐 Python start 内 7 项 PRAGMA）、
//!   初始化 jobs schema（`JOBS_SCHEMA_DDL`：jobs 表 + 4 索引，逐字对齐
//!   `db/db_jobs.py`），幂等可重复调用。
//!
//! 权威归属说明：生产链重任务执行权威原已由 Rust `job_runner.rs`
//!（task_rpc：`task.job_submit` + `task.wait_for_job`，18 个长任务批次）
//! 承担，不依赖 Python 双实现；本卡将 Python `JobExecutor.start` 的
//! daemon RPC 形态下沉，消除 Python sqlite3.connect 残留的权威接缝。
//!
//! 返回形态对齐 SRV-009 init：`{db_path, exists, schema_ready, jobs_table,
//! index_count, source:"rust"}`。错误语义（fail-soft）：连接/初始化失败
//! 归一化为 `reason` 字段（missing_db_path/db_open_failed/schema_init_failed），
//! 绝不抛错，与 SRV-003~010 先例一致。

use std::path::PathBuf;

use rusqlite::Connection;
use serde_json::{json, Value};

use super::dispatch::{get_str_param, DaemonRpcError};

/// jobs schema DDL：逐字对齐 `db/db_jobs.py::JOBS_SCHEMA_DDL`
///（jobs 表 + 4 索引）。
const JOBS_SCHEMA_DDL: &str = r#"
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    workspace_id INTEGER NOT NULL,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    progress REAL DEFAULT 0.0,
    message TEXT DEFAULT '',
    params TEXT DEFAULT '{}',
    result_summary TEXT DEFAULT '{}',
    error TEXT DEFAULT '',
    cancel_requested INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL DEFAULT 0,
    finished_at REAL DEFAULT 0,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_jobs_workspace ON jobs(workspace_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_type ON jobs(workspace_id, job_type);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"#;

/// 批次10 PRAGMA 集：逐字对齐 Python `JobExecutor.start` 内 7 项 PRAGMA
///（WAL + busy_timeout + synchronous + wal_autocheckpoint + cache_size
/// + mmap_size + temp_store）。
const START_PRAGMAS: &str = "PRAGMA journal_mode=WAL;\
                             PRAGMA busy_timeout=5000;\
                             PRAGMA synchronous=NORMAL;\
                             PRAGMA wal_autocheckpoint=1000;\
                             PRAGMA cache_size=-262144;\
                             PRAGMA mmap_size=268435456;\
                             PRAGMA temp_store=MEMORY;";

/// `mcp.job_executor.start` —— `JobExecutor.start` 权威初始化形态下沉。
/// 打开/创建 jobs DB + 批次10 PRAGMA 集 + jobs schema（幂等 DDL）。
/// 归一化：缺 db_path → reason=missing_db_path；连接失败 →
/// reason=db_open_failed；DDL 失败 → reason=schema_init_failed；
/// 就绪 → schema_ready=true + jobs_table/index_count 核验，绝不抛错。
pub fn handle_start(params: &Value) -> Result<Value, DaemonRpcError> {
    let db_path = match get_str_param(params, "db_path") {
        Some(p) if !p.is_empty() => PathBuf::from(p),
        _ => {
            return Ok(json!({
                "schema_ready": false,
                "reason": "missing_db_path",
                "source": "rust",
            }));
        }
    };

    // 缺父目录则创建（对齐 SRV-009 init 语义）
    if let Some(parent) = db_path.parent() {
        if !parent.as_os_str().is_empty() {
            let _ = std::fs::create_dir_all(parent);
        }
    }

    let conn = match Connection::open(&db_path) {
        Ok(c) => c,
        Err(e) => {
            return Ok(json!({
                "db_path": db_path.to_string_lossy(),
                "exists": false,
                "schema_ready": false,
                "reason": "db_open_failed",
                "error": format!("{}", e),
                "source": "rust",
            }));
        }
    };

    // 批次10 PRAGMA 集（对齐 Python start）
    if let Err(e) = conn.execute_batch(START_PRAGMAS) {
        return Ok(json!({
            "db_path": db_path.to_string_lossy(),
            "exists": true,
            "schema_ready": false,
            "reason": "schema_init_failed",
            "error": format!("pragma failed: {}", e),
            "source": "rust",
        }));
    }

    // jobs schema（幂等 DDL：CREATE TABLE/INDEX IF NOT EXISTS）
    if let Err(e) = conn.execute_batch(JOBS_SCHEMA_DDL) {
        return Ok(json!({
            "db_path": db_path.to_string_lossy(),
            "exists": true,
            "schema_ready": false,
            "reason": "schema_init_failed",
            "error": format!("ddl failed: {}", e),
            "source": "rust",
        }));
    }

    // 权威核验：jobs 表 + 4 索引真实存在
    let jobs_table: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='jobs'",
            [],
            |row| row.get(0),
        )
        .unwrap_or(0);
    let index_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' \
             AND name IN ('idx_jobs_workspace','idx_jobs_status',\
                          'idx_jobs_type','idx_jobs_created')",
            [],
            |row| row.get(0),
        )
        .unwrap_or(0);

    Ok(json!({
        "db_path": db_path.to_string_lossy(),
        "exists": true,
        "schema_ready": jobs_table > 0,
        "jobs_table": jobs_table > 0,
        "index_count": index_count,
        "source": "rust",
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    fn tmp_path(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("srv011_{}_{}", tag, std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        dir.join(format!("{tag}.db"))
    }

    #[test]
    fn test_start_new_db_schema_ready() {
        let path = tmp_path("new_db");
        let _ = std::fs::remove_file(&path);
        let res = handle_start(&json!({"db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["schema_ready"], true);
        assert_eq!(res["jobs_table"], true);
        assert_eq!(res["index_count"], 4);
        assert_eq!(res["source"], "rust");
        // 权威核验：直查 sqlite_master
        let conn = Connection::open(&path).unwrap();
        let n: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' \
                 AND name='jobs'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n, 1);
    }

    #[test]
    fn test_start_idempotent() {
        let path = tmp_path("idempotent");
        let _ = std::fs::remove_file(&path);
        let r1 = handle_start(&json!({"db_path": path.to_string_lossy()})).unwrap();
        let r2 = handle_start(&json!({"db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(r1["schema_ready"], true);
        assert_eq!(r2["schema_ready"], true);
        assert_eq!(r2["index_count"], 4);
    }

    #[test]
    fn test_start_preserves_existing_data() {
        // 幂等 DDL 不破坏既有 jobs 数据
        let path = tmp_path("preserve");
        let _ = std::fs::remove_file(&path);
        handle_start(&json!({"db_path": path.to_string_lossy()})).unwrap();
        let conn = Connection::open(&path).unwrap();
        // jobs FK 引用 workspaces；生产库中 workspaces 由 schema 基线先建，
        // 测试补最小 workspaces 表以覆盖 FK 场景
        conn.execute(
            "CREATE TABLE IF NOT EXISTS workspaces (id INTEGER PRIMARY KEY)",
            [],
        )
        .unwrap();
        conn.execute("INSERT INTO workspaces (id) VALUES (1)", [])
            .unwrap();
        conn.execute(
            "INSERT INTO jobs (job_id, workspace_id, job_type, status, created_at) \
             VALUES ('J-1', 1, 'clone_detect', 'pending', 1.0)",
            [],
        )
        .unwrap();
        drop(conn);
        let res = handle_start(&json!({"db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["schema_ready"], true);
        let conn = Connection::open(&path).unwrap();
        let n: i64 = conn
            .query_row("SELECT COUNT(*) FROM jobs", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 1);
    }

    #[test]
    fn test_start_missing_db_path_fail_soft() {
        let res = handle_start(&json!({})).unwrap();
        assert_eq!(res["schema_ready"], false);
        assert_eq!(res["reason"], "missing_db_path");
    }

    #[test]
    fn test_start_dir_path_fail_soft() {
        // db_path 指向目录 → sqlite open 失败 → fail-soft 归一化
        let dir = std::env::temp_dir().join(format!("srv011_dirpath_{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let res = handle_start(&json!({"db_path": dir.to_string_lossy()})).unwrap();
        assert_eq!(res["schema_ready"], false);
        assert_eq!(res["reason"], "db_open_failed");
    }

    #[test]
    fn test_start_wal_mode_applied() {
        let path = tmp_path("wal_mode");
        let _ = std::fs::remove_file(&path);
        handle_start(&json!({"db_path": path.to_string_lossy()})).unwrap();
        let conn = Connection::open(&path).unwrap();
        let mode: String = conn
            .query_row("PRAGMA journal_mode", [], |r| r.get(0))
            .unwrap();
        assert_eq!(mode.to_lowercase(), "wal");
    }

    #[test]
    fn test_start_creates_parent_dir() {
        let base = std::env::temp_dir().join(format!("srv011_parent_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let path = base.join("nested").join("jobs.db");
        let res = handle_start(&json!({"db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["schema_ready"], true);
        assert!(Path::new(&path).is_file());
    }
}
