//! Phase 1-1: SQLite 只读查询 API
//!
//! 设计原则（见 docs/design/phase1-sqlite-contract.md §4）：
//! - 只读连接（`SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_URI`，非 `immutable=1`）
//! - 不激活 workspace，不持有写锁
//! - WAL checkpoint（PASSIVE）后读取，确保数据一致（AGENTS.md 规则 7）
//! - busy_timeout=5000（与 Python 端一致，AGENTS.md 规则 6）
//! - 短连接：每次调用新建 + 关闭，避免与 Python 长连接撞锁
//!
//! 不在本模块实现（保留 Python 主导）：
//! - schema migration（`_migrate_schema`）
//! - 表创建、索引管理
//! - workspace 激活

use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;
use rusqlite::OpenFlags;

/// 查询 SQLite 数据库的 schema_version
///
/// 与 Python 端 `db_base.py:_get_current_version` 行为一致：
/// - 空数据库 / 不存在 schema_version 表 / 表为空 → 返回 0
/// - 正常表 → 返回 `MAX(version)`
///
/// # Errors
/// - `PyValueError`: db_path 为空
/// - `PyIOError`: 数据库无法打开（路径不存在 / 权限 / 损坏）或 busy_timeout 超时
#[pyfunction]
pub fn sqlite_query_schema_version(db_path: &str) -> PyResult<i64> {
    if db_path.is_empty() {
        return Err(PyValueError::new_err("db_path 不能为空"));
    }

    // 只读 + URI 模式（支持 file: URI）；不用 immutable=1（会跳过 WAL，读到旧数据）
    let conn = rusqlite::Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    )
    .map_err(|e| PyIOError::new_err(format!("打开数据库失败: {}", e)))?;

    // busy_timeout=5000：与 Python 端一致（AGENTS.md 规则 6）
    conn.busy_timeout(std::time::Duration::from_secs(5))
        .map_err(|e| PyIOError::new_err(format!("设置 busy_timeout 失败: {}", e)))?;

    // WAL checkpoint（PASSIVE）：确保 WAL 已 flush 到主库
    // AGENTS.md 规则 7：immutable=1/只读连接可能读到旧数据，需先 checkpoint
    // PASSIVE 不阻塞写连接，但若 MCP Server 正在写，可能读到 checkpoint 前的状态
    // —— 这与 Python 端 sqlite3.connect 行为一致，可接受
    // 用 execute_batch 避免 rusqlite 0.31 pragma_query 的签名差异
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");

    // 查询 MAX(version)
    // - 表不存在 → query_row 返回 Err → ok() → None → unwrap_or(0)
    // - 表为空 → MAX 返回 NULL → row.get(0) 返回 None → flatten → None → unwrap_or(0)
    // - 有记录 → MAX 返回 i64 → unwrap
    let v: Option<i64> = conn
        .query_row(
            "SELECT MAX(version) FROM schema_version",
            [],
            |row| row.get::<_, Option<i64>>(0),
        )
        .ok()
        .flatten();
    Ok(v.unwrap_or(0))
}

/// 模块注册入口（供 lib.rs 调用）
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sqlite_query_schema_version, m)?)?;
    Ok(())
}
