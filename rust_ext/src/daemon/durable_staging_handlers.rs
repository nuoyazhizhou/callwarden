//! durable staging 面 handler（SRV-009：server durable staging Python authority → Rust daemon）。
//!
//! 对应 `server/durable_staging.py` 的 `DurableStagingLog`（SQLite WAL 状态机）：
//! - `handle_init`：承接 `DurableStagingLog.__init__` 的权威初始化契约——
//!   打开权威 staging.db、应用批次10 完整 PRAGMA 集（busy_timeout/WAL/
//!   synchronous/wal_autocheckpoint/cache_size/mmap_size/temp_store）、
//!   建立 `staging_entries` schema（表 + 2 索引），返回归一化元信息。
//! - `handle_stats`：`DurableStagingLog.stats` 的只读统计探测
//!   （4 状态计数 + max_lsn），以只读连接短连接执行。
//!
//! 权威归属说明：生产链 staging 权威由 Rust `staging_log.rs` 承担
//!（per-workspace JSONL StagingLog，workspace.rs/replicator.rs 使用）；
//! Python `DurableStagingLog` 为零生产调用方的 compat/test-only 组件，
//! 本卡将其 SQLite WAL 形态的权威初始化/统计下沉至 Rust daemon，
//! 消除 Python direct authority（sqlite3.connect）残留。
//!
//! 错误语义（fail-soft 对齐 Python）：库不可打开 / 表缺失 / 查询失败
//! 一律返回归一化降级结果（schema_ready=false 或全零计数 + reason），
//! 绝不抛错，与 SRV-003/SRV-007/SRV-008 先例一致。

use std::path::PathBuf;

use rusqlite::Connection;
use serde_json::{json, Value};

use super::dispatch::{get_str_param, DaemonRpcError};

/// `staging_entries` schema（逐字对齐 Python `STAGING_SCHEMA_DDL`）。
const STAGING_SCHEMA_DDL: &str = "
CREATE TABLE IF NOT EXISTS staging_entries (
    lsn INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    session_epoch INTEGER NOT NULL,
    monotonic_seq INTEGER NOT NULL,
    event_kind TEXT NOT NULL,
    content_hash TEXT DEFAULT '',
    delta_blob BLOB NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    applied_generation INTEGER DEFAULT 0,
    error TEXT DEFAULT NULL,
    UNIQUE(workspace_id, rel_path, session_epoch, monotonic_seq)
);
CREATE INDEX IF NOT EXISTS idx_staging_state
    ON staging_entries(state);
CREATE INDEX IF NOT EXISTS idx_staging_workspace
    ON staging_entries(workspace_id, state);
";

/// PRAGMA 集（对齐 Python `DurableStagingLog.__init__` 批次10 补全集）。
const STAGING_PRAGMAS: &[&str] = &[
    "PRAGMA busy_timeout=5000",
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA wal_autocheckpoint=1000",
    "PRAGMA cache_size=-262144",
    "PRAGMA mmap_size=268435456",
    "PRAGMA temp_store=MEMORY",
];

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

/// 默认权威 staging.db 路径：`CALLWARDEN_STAGING_DB` 显式覆盖 →
/// `CW_DAEMON_DATA_ROOT/staging.db` → `~/.callwarden/daemon/staging.db`
///（目录语义对齐 SRV-008 `default_registry_db_path`）。
fn default_staging_db_path() -> PathBuf {
    if let Ok(v) = std::env::var("CALLWARDEN_STAGING_DB") {
        if !v.is_empty() {
            return PathBuf::from(v);
        }
    }
    if let Ok(v) = std::env::var("CW_DAEMON_DATA_ROOT") {
        if !v.is_empty() {
            return PathBuf::from(v).join("staging.db");
        }
    }
    default_callwarden_dir().join("daemon").join("staging.db")
}

/// `mcp.durable_staging.init` —— `DurableStagingLog.__init__` 权威初始化下沉。
/// Rust 权威打开 staging.db（缺父目录则创建）、应用完整 PRAGMA 集、
/// 建立 `staging_entries` schema，返回归一化元信息
/// `{db_path, exists, schema_ready, source}`。
/// fail-soft：库不可打开 / schema 建立失败 → `schema_ready=false + reason`，绝不抛错。
/// `db_path` 参数可选（缺省用户级 daemon 数据目录），供测试与多库场景显式指定。
pub fn handle_init(params: &Value) -> Result<Value, DaemonRpcError> {
    let db_path = match get_str_param(params, "db_path") {
        Some(p) if !p.is_empty() => PathBuf::from(p),
        _ => default_staging_db_path(),
    };
    // 对齐 Python `os.makedirs(os.path.dirname(db_path), exist_ok=True)`
    if let Some(parent) = db_path.parent() {
        if !parent.as_os_str().is_empty() {
            let _ = std::fs::create_dir_all(parent);
        }
    }
    let conn = match Connection::open(&db_path) {
        Ok(c) => c,
        Err(_) => {
            return Ok(json!({
                "db_path": db_path.to_string_lossy(),
                "exists": db_path.exists(),
                "schema_ready": false,
                "source": "rust",
                "reason": "db_open_failed",
            }));
        }
    };
    for pragma in STAGING_PRAGMAS {
        let _ = conn.execute_batch(pragma);
    }
    let schema_ok = conn.execute_batch(STAGING_SCHEMA_DDL).is_ok();
    let mut out = json!({
        "db_path": db_path.to_string_lossy(),
        "exists": db_path.exists(),
        "schema_ready": schema_ok,
        "source": "rust",
    });
    if !schema_ok {
        out["reason"] = json!("schema_init_failed");
    }
    Ok(out)
}

/// `mcp.durable_staging.stats` —— `DurableStagingLog.stats` 只读统计探测。
/// 以只读短连接读权威 staging.db：4 状态计数 + total + max_lsn。
/// fail-soft：库不可打开（含文件缺失）→ 全零计数 + `reason=db_open_failed`；
/// 表缺失 → 全零计数 + `reason=table_missing`；绝不抛错。
pub fn handle_stats(params: &Value) -> Result<Value, DaemonRpcError> {
    let db_path = match get_str_param(params, "db_path") {
        Some(p) if !p.is_empty() => PathBuf::from(p),
        _ => default_staging_db_path(),
    };
    let zero = |reason: &str| {
        json!({
            "counts": {"pending": 0, "applying": 0, "applied": 0, "failed": 0},
            "total": 0,
            "max_lsn": 0,
            "db_path": db_path.to_string_lossy(),
            "reason": reason,
        })
    };
    let conn =
        match Connection::open_with_flags(&db_path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY) {
            Ok(c) => c,
            Err(_) => return Ok(zero("db_open_failed")),
        };
    let mut counts = serde_json::Map::new();
    let mut total: i64 = 0;
    for state in ["pending", "applying", "applied", "failed"] {
        let n: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM staging_entries WHERE state = ?1",
                rusqlite::params![state],
                |row| row.get(0),
            )
            .map_err(|_| ())
            .unwrap_or_else(|_| return_i64_neg());
        if n < 0 {
            return Ok(zero("table_missing"));
        }
        counts.insert(state.to_string(), json!(n));
        total += n;
    }
    let max_lsn: i64 = conn
        .query_row(
            "SELECT COALESCE(MAX(lsn), 0) FROM staging_entries",
            [],
            |row| row.get(0),
        )
        .unwrap_or(0);
    Ok(json!({
        "counts": counts,
        "total": total,
        "max_lsn": max_lsn,
        "db_path": db_path.to_string_lossy(),
    }))
}

/// 哨兵值：查询失败（表缺失等）时返回 -1 以触发 fail-soft 分支。
fn return_i64_neg() -> i64 {
    -1
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn tmp_path(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("srv009_{}_{}", tag, std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join(format!("{tag}.db"));
        let _ = std::fs::remove_file(&path);
        path
    }

    #[test]
    fn test_init_creates_schema_and_ready() {
        let path = tmp_path("init_ready");
        let res = handle_init(&json!({"db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["schema_ready"], true);
        assert_eq!(res["exists"], true);
        assert_eq!(res["source"], "rust");
        // 权威核验：staging_entries 表与 2 索引真实建立
        let conn = Connection::open(&path).unwrap();
        let tables: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='staging_entries' AND type='table'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(tables, 1);
        let indexes: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'idx_staging_%' AND type='index'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(indexes, 2);
    }

    #[test]
    fn test_init_idempotent() {
        let path = tmp_path("init_idem");
        let r1 = handle_init(&json!({"db_path": path.to_string_lossy()})).unwrap();
        let r2 = handle_init(&json!({"db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(r1["schema_ready"], true);
        assert_eq!(r2["schema_ready"], true);
    }

    #[test]
    fn test_init_applies_wal_mode() {
        // PRAGMA journal_mode=WAL 对齐 Python __init__ 批次10 PRAGMA 集
        let path = tmp_path("init_wal");
        handle_init(&json!({"db_path": path.to_string_lossy()})).unwrap();
        let conn = Connection::open(&path).unwrap();
        let mode: String = conn
            .query_row("PRAGMA journal_mode", [], |r| r.get(0))
            .unwrap();
        assert_eq!(mode.to_lowercase(), "wal");
    }

    #[test]
    fn test_init_creates_parent_dir() {
        // 对齐 Python os.makedirs(dirname, exist_ok=True)
        let dir = std::env::temp_dir().join(format!("srv009_parent_{}", std::process::id()));
        let nested = dir.join("deep").join("staging.db");
        let _ = std::fs::remove_dir_all(&dir);
        let res = handle_init(&json!({"db_path": nested.to_string_lossy()})).unwrap();
        assert_eq!(res["schema_ready"], true);
        assert!(nested.exists());
    }

    #[test]
    fn test_init_open_failed_fail_soft() {
        // db_path 指向已存在目录 → sqlite open 失败 → fail-soft 不抛错
        let dir = std::env::temp_dir().join(format!("srv009_openfail_{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let res = handle_init(&json!({"db_path": dir.to_string_lossy()})).unwrap();
        assert_eq!(res["schema_ready"], false);
        assert_eq!(res["reason"], "db_open_failed");
    }

    fn seed_entry(conn: &Connection, seq: i64, state: &str) {
        conn.execute(
            "INSERT INTO staging_entries (workspace_id, rel_path, session_epoch, \
             monotonic_seq, event_kind, delta_blob, state, created_at) \
             VALUES (?1, 'a.txt', 1, ?2, 'modify', X'00', ?3, 100.0)",
            rusqlite::params!["ws-1", seq, state],
        )
        .unwrap();
    }

    fn init_and_open(path: &PathBuf) -> Connection {
        handle_init(&json!({"db_path": path.to_string_lossy()})).unwrap();
        Connection::open(path).unwrap()
    }

    #[test]
    fn test_stats_empty() {
        let path = tmp_path("stats_empty");
        init_and_open(&path);
        let res = handle_stats(&json!({"db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["total"], 0);
        assert_eq!(res["max_lsn"], 0);
        assert!(res.get("reason").is_none());
    }

    #[test]
    fn test_stats_counts_and_max_lsn() {
        let path = tmp_path("stats_counts");
        let conn = init_and_open(&path);
        seed_entry(&conn, 1, "pending");
        seed_entry(&conn, 2, "pending");
        seed_entry(&conn, 3, "applied");
        drop(conn);
        let res = handle_stats(&json!({"db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["counts"]["pending"], 2);
        assert_eq!(res["counts"]["applied"], 1);
        assert_eq!(res["counts"]["applying"], 0);
        assert_eq!(res["total"], 3);
        assert!(res["max_lsn"].as_i64().unwrap() >= 3);
    }

    #[test]
    fn test_stats_db_missing_fail_soft() {
        // 只读连接对缺失文件 open 失败 → 全零计数 + reason，绝不抛错
        let dir = std::env::temp_dir().join(format!("srv009_missing_{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("no_such.db");
        let res = handle_stats(&json!({"db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["total"], 0);
        assert_eq!(res["reason"], "db_open_failed");
    }

    #[test]
    fn test_stats_table_missing_fail_soft() {
        let path = tmp_path("stats_notable");
        // 有库无表
        let conn = Connection::open(&path).unwrap();
        drop(conn);
        let res = handle_stats(&json!({"db_path": path.to_string_lossy()})).unwrap();
        assert_eq!(res["total"], 0);
        assert_eq!(res["reason"], "table_missing");
    }
}
