//! Phase 1-3: workspace manifest 只读查询 API（PyO3 暴露层）
//!
//! 对应 Python `db/db_workspace_manifest.py` 的只读查询方法：
//! - `manifest_get`（→ Python `get_manifest`）—— 查询单个文件 manifest
//! - `manifest_list`（→ Python `list_manifests`）—— 列出 workspace 所有 manifest
//! - `manifest_count`（→ Python 等价 `len(list_manifests(...))`）—— 统计行数
//! - `snapshot_get_files`（→ Python `get_snapshot_files`）—— 查询 snapshot 文件列表
//! - `manifest_verify_raw_hash`（→ Python `verify_raw_hash`）—— 校验 raw_hash
//!
//! 设计原则（见 docs/design/phase1-manifest-contract.md §5）：
//! - 只读连接（`SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_URI`，非 `immutable=1`）
//! - WAL checkpoint(PASSIVE) 后读取（AGENTS.md 规则 7）
//! - busy_timeout=5000（与 Phase 1-1 / 1-2 一致，AGENTS.md 规则 6）
//! - 短连接：每次调用新建 + 关闭
//!
//! 写操作也通过本模块暴露，作为 Python 兼容入口的 Rust 生产实现：
//! - `manifest_init_schema`：幂等创建 manifest 表和索引
//! - `manifest_upsert`：单条 manifest 原子 UPSERT
//! - `manifest_link_to_snapshot`：单条 snapshot 映射原子 UPSERT
//!
//! Rust `cas_merge::upsert_manifest` 仍是 daemon 内部实现，不通过 PyO3 暴露。

use pyo3::exceptions::PyIOError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::types::PyList;
use rusqlite::OpenFlags;

const MANIFEST_SCHEMA_DDL: &str = "
CREATE TABLE IF NOT EXISTS workspace_manifests (
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    cas_key TEXT,
    raw_hash TEXT,
    source_encoding TEXT DEFAULT 'utf-8',
    bom_kind TEXT DEFAULT 'none',
    newline_style TEXT DEFAULT 'lf',
    file_size INTEGER DEFAULT 0,
    mtime_ns INTEGER DEFAULT 0,
    is_dirty INTEGER DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, rel_path)
);
CREATE TABLE IF NOT EXISTS workspace_snapshot_map (
    snapshot_id TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    cas_key TEXT,
    PRIMARY KEY (snapshot_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_manifests_hash ON workspace_manifests(content_hash);
CREATE INDEX IF NOT EXISTS idx_manifests_cas ON workspace_manifests(cas_key);
CREATE INDEX IF NOT EXISTS idx_manifests_dirty ON workspace_manifests(workspace_id, is_dirty);
";

/// 打开只读连接的辅助函数（内部使用）
///
/// 与 sqlite_query.rs / cas_query.rs 完全相同策略：
/// - SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_URI（非 immutable=1，避免读到旧数据）
/// - busy_timeout=5000
/// - PRAGMA wal_checkpoint(PASSIVE) 确保 WAL 已 flush
fn open_readonly(db_path: &str) -> PyResult<rusqlite::Connection> {
    let conn = rusqlite::Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    )
    .map_err(|e| PyIOError::new_err(format!("打开数据库失败: {}", e)))?;
    conn.busy_timeout(std::time::Duration::from_secs(5))
        .map_err(|e| PyIOError::new_err(format!("设置 busy_timeout 失败: {}", e)))?;
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
    Ok(conn)
}

/// 打开用于短写事务的连接。
fn open_readwrite(db_path: &str) -> PyResult<rusqlite::Connection> {
    let conn = rusqlite::Connection::open(db_path)
        .map_err(|e| PyIOError::new_err(format!("打开数据库失败: {}", e)))?;
    conn.busy_timeout(std::time::Duration::from_secs(5))
        .map_err(|e| PyIOError::new_err(format!("设置 busy_timeout 失败: {}", e)))?;
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
        .map_err(|e| PyIOError::new_err(format!("设置写入模式失败: {}", e)))?;
    Ok(conn)
}

fn sqlite_write_error(operation: &str, error: rusqlite::Error) -> PyErr {
    PyIOError::new_err(format!("{}失败: {}", operation, error))
}

/// 幂等创建 manifest 相关 schema。
#[pyfunction]
pub fn manifest_init_schema(db_path: &str) -> PyResult<()> {
    let conn = open_readwrite(db_path)?;
    conn.execute_batch("BEGIN IMMEDIATE;")
        .map_err(|e| sqlite_write_error("开始 manifest schema 事务", e))?;
    if let Err(error) = conn.execute_batch(MANIFEST_SCHEMA_DDL) {
        let _ = conn.execute_batch("ROLLBACK;");
        return Err(sqlite_write_error("初始化 manifest schema", error));
    }
    conn.execute_batch("COMMIT;")
        .map_err(|e| sqlite_write_error("提交 manifest schema 事务", e))?;
    Ok(())
}

/// 原子写入 workspace manifest。
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn manifest_upsert(
    db_path: &str,
    workspace_id: i64,
    rel_path: &str,
    content_hash: &str,
    cas_key: &str,
    raw_hash: &str,
    source_encoding: &str,
    bom_kind: &str,
    newline_style: &str,
    file_size: i64,
    mtime_ns: i64,
    is_dirty: bool,
) -> PyResult<()> {
    let conn = open_readwrite(db_path)?;
    conn.execute_batch("BEGIN IMMEDIATE;")
        .map_err(|e| sqlite_write_error("开始 manifest 写入事务", e))?;
    let result = conn.execute(
        "INSERT OR REPLACE INTO workspace_manifests
         (workspace_id, rel_path, content_hash, cas_key, raw_hash,
          source_encoding, bom_kind, newline_style, file_size, mtime_ns,
          is_dirty, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
        rusqlite::params![
            workspace_id,
            rel_path,
            content_hash,
            cas_key,
            raw_hash,
            source_encoding,
            bom_kind,
            newline_style,
            file_size,
            mtime_ns,
            i64::from(is_dirty),
            unix_timestamp(),
        ],
    );
    if let Err(error) = result {
        let _ = conn.execute_batch("ROLLBACK;");
        return Err(sqlite_write_error("写入 manifest", error));
    }
    conn.execute_batch("COMMIT;")
        .map_err(|e| sqlite_write_error("提交 manifest 写入事务", e))?;
    Ok(())
}

/// 原子写入 snapshot 到文件的映射。
#[pyfunction]
pub fn manifest_link_to_snapshot(
    db_path: &str,
    snapshot_id: &str,
    rel_path: &str,
    content_hash: &str,
    cas_key: &str,
) -> PyResult<()> {
    let conn = open_readwrite(db_path)?;
    conn.execute_batch("BEGIN IMMEDIATE;")
        .map_err(|e| sqlite_write_error("开始 snapshot 映射事务", e))?;
    let result = conn.execute(
        "INSERT OR REPLACE INTO workspace_snapshot_map
         (snapshot_id, rel_path, content_hash, cas_key)
         VALUES (?1, ?2, ?3, ?4)",
        rusqlite::params![snapshot_id, rel_path, content_hash, cas_key],
    );
    if let Err(error) = result {
        let _ = conn.execute_batch("ROLLBACK;");
        return Err(sqlite_write_error("写入 snapshot 映射", error));
    }
    conn.execute_batch("COMMIT;")
        .map_err(|e| sqlite_write_error("提交 snapshot 映射事务", e))?;
    Ok(())
}

fn unix_timestamp() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

/// 从一行结果构建 manifest dict（12 字段，与 Python SELECT * 顺序一致）
///
/// 字段顺序对齐 db_workspace_manifest.py:upsert_manifest 的 INSERT 列序：
/// workspace_id / rel_path / content_hash / cas_key / raw_hash /
/// source_encoding / bom_kind / newline_style / file_size / mtime_ns /
/// is_dirty / updated_at
fn manifest_row_to_dict<'py>(
    py: Python<'py>,
    row: &rusqlite::Row<'_>,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item(
        "workspace_id",
        row.get::<_, i64>(0)
            .map_err(|e| PyIOError::new_err(format!("字段读取失败 workspace_id: {}", e)))?,
    )?;
    dict.set_item(
        "rel_path",
        row.get::<_, String>(1)
            .map_err(|e| PyIOError::new_err(format!("字段读取失败 rel_path: {}", e)))?,
    )?;
    dict.set_item(
        "content_hash",
        row.get::<_, String>(2)
            .map_err(|e| PyIOError::new_err(format!("字段读取失败 content_hash: {}", e)))?,
    )?;
    // cas_key / raw_hash 允许 NULL，使用 Option<String>
    dict.set_item(
        "cas_key",
        row.get::<_, Option<String>>(3)
            .map_err(|e| PyIOError::new_err(format!("字段读取失败 cas_key: {}", e)))?
            .unwrap_or_default(),
    )?;
    dict.set_item(
        "raw_hash",
        row.get::<_, Option<String>>(4)
            .map_err(|e| PyIOError::new_err(format!("字段读取失败 raw_hash: {}", e)))?
            .unwrap_or_default(),
    )?;
    dict.set_item(
        "source_encoding",
        row.get::<_, String>(5)
            .map_err(|e| PyIOError::new_err(format!("字段读取失败 source_encoding: {}", e)))?,
    )?;
    dict.set_item(
        "bom_kind",
        row.get::<_, String>(6)
            .map_err(|e| PyIOError::new_err(format!("字段读取失败 bom_kind: {}", e)))?,
    )?;
    dict.set_item(
        "newline_style",
        row.get::<_, String>(7)
            .map_err(|e| PyIOError::new_err(format!("字段读取失败 newline_style: {}", e)))?,
    )?;
    dict.set_item(
        "file_size",
        row.get::<_, i64>(8)
            .map_err(|e| PyIOError::new_err(format!("字段读取失败 file_size: {}", e)))?,
    )?;
    dict.set_item(
        "mtime_ns",
        row.get::<_, i64>(9)
            .map_err(|e| PyIOError::new_err(format!("字段读取失败 mtime_ns: {}", e)))?,
    )?;
    dict.set_item(
        "is_dirty",
        row.get::<_, i64>(10)
            .map_err(|e| PyIOError::new_err(format!("字段读取失败 is_dirty: {}", e)))?,
    )?;
    dict.set_item(
        "updated_at",
        row.get::<_, f64>(11)
            .map_err(|e| PyIOError::new_err(format!("字段读取失败 updated_at: {}", e)))?,
    )?;
    Ok(dict)
}

/// 查询单个文件 manifest
///
/// 与 Python `db_workspace_manifest.get_manifest(conn, workspace_id, rel_path)` 行为一致：
/// - 行存在 → 返回 dict（含 12 字段）
/// - 行不存在 → 返回 None
///
/// # Errors
/// - `PyIOError`: 数据库无法打开（路径不存在 / 权限 / 损坏）或表不存在
#[pyfunction]
pub fn manifest_get<'py>(
    py: Python<'py>,
    db_path: &str,
    workspace_id: i64,
    rel_path: &str,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let conn = open_readonly(db_path)?;
    let mut stmt = conn
        .prepare(
            "SELECT workspace_id, rel_path, content_hash, cas_key, raw_hash, \
                    source_encoding, bom_kind, newline_style, file_size, mtime_ns, \
                    is_dirty, updated_at \
             FROM workspace_manifests \
             WHERE workspace_id = ?1 AND rel_path = ?2",
        )
        .map_err(|e| PyIOError::new_err(format!("prepare 失败: {}", e)))?;
    let mut rows = stmt
        .query(rusqlite::params![workspace_id, rel_path])
        .map_err(|e| PyIOError::new_err(format!("query 失败: {}", e)))?;

    if let Some(row) = rows
        .next()
        .map_err(|e| PyIOError::new_err(format!("fetch 失败: {}", e)))?
    {
        let dict = manifest_row_to_dict(py, &row)?;
        Ok(Some(dict.into_any()))
    } else {
        Ok(None)
    }
}

/// 列出 workspace 的所有 manifest
///
/// 与 Python `db_workspace_manifest.list_manifests(conn, workspace_id, dirty_only)` 行为一致：
/// - dirty_only=True → 只返回 is_dirty=1 的行
/// - dirty_only=False → 返回所有行
/// - 空表或无行 → 返回空列表 []
///
/// # Errors
/// - `PyIOError`: 数据库无法打开或表不存在
#[pyfunction]
pub fn manifest_list<'py>(
    py: Python<'py>,
    db_path: &str,
    workspace_id: i64,
    dirty_only: bool,
) -> PyResult<Bound<'py, PyList>> {
    let conn = open_readonly(db_path)?;
    let sql = if dirty_only {
        "SELECT workspace_id, rel_path, content_hash, cas_key, raw_hash, \
                source_encoding, bom_kind, newline_style, file_size, mtime_ns, \
                is_dirty, updated_at \
         FROM workspace_manifests \
         WHERE workspace_id = ?1 AND is_dirty = 1"
    } else {
        "SELECT workspace_id, rel_path, content_hash, cas_key, raw_hash, \
                source_encoding, bom_kind, newline_style, file_size, mtime_ns, \
                is_dirty, updated_at \
         FROM workspace_manifests \
         WHERE workspace_id = ?1"
    };
    let mut stmt = conn
        .prepare(sql)
        .map_err(|e| PyIOError::new_err(format!("prepare 失败: {}", e)))?;
    let rows = stmt
        .query_map(rusqlite::params![workspace_id], |row| {
            // 这里用 placeholder 占位（无法在闭包内直接构造 PyDict，因 Python token 未传）
            // 改用 query_map 收集 row → Vec 后再转 PyDict
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, Option<String>>(3)?.unwrap_or_default(),
                row.get::<_, Option<String>>(4)?.unwrap_or_default(),
                row.get::<_, String>(5)?,
                row.get::<_, String>(6)?,
                row.get::<_, String>(7)?,
                row.get::<_, i64>(8)?,
                row.get::<_, i64>(9)?,
                row.get::<_, i64>(10)?,
                row.get::<_, f64>(11)?,
            ))
        })
        .map_err(|e| PyIOError::new_err(format!("query 失败: {}", e)))?;

    let list = PyList::empty(py);
    for row in rows {
        let r = row.map_err(|e| PyIOError::new_err(format!("row 读取失败: {}", e)))?;
        let dict = PyDict::new(py);
        dict.set_item("workspace_id", r.0)?;
        dict.set_item("rel_path", r.1)?;
        dict.set_item("content_hash", r.2)?;
        dict.set_item("cas_key", r.3)?;
        dict.set_item("raw_hash", r.4)?;
        dict.set_item("source_encoding", r.5)?;
        dict.set_item("bom_kind", r.6)?;
        dict.set_item("newline_style", r.7)?;
        dict.set_item("file_size", r.8)?;
        dict.set_item("mtime_ns", r.9)?;
        dict.set_item("is_dirty", r.10)?;
        dict.set_item("updated_at", r.11)?;
        list.append(dict)?;
    }
    Ok(list)
}

/// 统计 workspace manifest 行数
///
/// Python 端无直接对应方法，但行为等价于 `len(list_manifests(...))`。
/// 用于快速计数场景，避免序列化全表。
/// - 表不存在 → 返回 0（与 Phase 1-2 cas_global_count_files 行为一致）
/// - dirty_only=True → COUNT WHERE is_dirty=1
///
/// # Errors
/// - `PyIOError`: 数据库无法打开（表不存在时返回 0 而非抛错）
#[pyfunction]
pub fn manifest_count(db_path: &str, workspace_id: i64, dirty_only: bool) -> PyResult<i64> {
    let conn = open_readonly(db_path)?;
    let sql = if dirty_only {
        "SELECT COUNT(*) FROM workspace_manifests WHERE workspace_id = ?1 AND is_dirty = 1"
    } else {
        "SELECT COUNT(*) FROM workspace_manifests WHERE workspace_id = ?1"
    };
    // 表不存在时返回 0（与 cas_global_count_files 行为一致）
    let count: Option<i64> = conn
        .query_row(sql, rusqlite::params![workspace_id], |row| row.get(0))
        .ok();
    Ok(count.unwrap_or(0))
}

/// 查询 snapshot 的所有文件
///
/// 与 Python `db_workspace_manifest.get_snapshot_files(conn, snapshot_id)` 行为一致：
/// - snapshot 存在 → 返回 list[dict]（每 dict 含 snapshot_id / rel_path / content_hash / cas_key）
/// - snapshot 不存在 → 返回空列表 []
///
/// # Errors
/// - `PyIOError`: 数据库无法打开或表不存在
#[pyfunction]
pub fn snapshot_get_files<'py>(
    py: Python<'py>,
    db_path: &str,
    snapshot_id: &str,
) -> PyResult<Bound<'py, PyList>> {
    let conn = open_readonly(db_path)?;
    let mut stmt = conn
        .prepare(
            "SELECT snapshot_id, rel_path, content_hash, cas_key \
             FROM workspace_snapshot_map \
             WHERE snapshot_id = ?1",
        )
        .map_err(|e| PyIOError::new_err(format!("prepare 失败: {}", e)))?;
    let rows = stmt
        .query_map(rusqlite::params![snapshot_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, Option<String>>(3)?.unwrap_or_default(),
            ))
        })
        .map_err(|e| PyIOError::new_err(format!("query 失败: {}", e)))?;

    let list = PyList::empty(py);
    for row in rows {
        let r = row.map_err(|e| PyIOError::new_err(format!("row 读取失败: {}", e)))?;
        let dict = PyDict::new(py);
        dict.set_item("snapshot_id", r.0)?;
        dict.set_item("rel_path", r.1)?;
        dict.set_item("content_hash", r.2)?;
        dict.set_item("cas_key", r.3)?;
        list.append(dict)?;
    }
    Ok(list)
}

/// 校验磁盘文件 raw_hash 与 manifest 记录一致
///
/// 与 Python `db_workspace_manifest.verify_raw_hash(conn, workspace_id, rel_path, expected_raw_hash)` 行为一致：
/// - manifest 不存在 → 返回 False
/// - manifest 存在但 raw_hash 不匹配 → 返回 False
/// - manifest 存在且 raw_hash 匹配 → 返回 True
#[pyfunction]
pub fn manifest_verify_raw_hash(
    db_path: &str,
    workspace_id: i64,
    rel_path: &str,
    expected_raw_hash: &str,
) -> PyResult<bool> {
    let conn = open_readonly(db_path)?;
    // 单次查询直接拿到 raw_hash，避免二次查询
    let stored: Option<String> = conn
        .query_row(
            "SELECT raw_hash FROM workspace_manifests \
             WHERE workspace_id = ?1 AND rel_path = ?2",
            rusqlite::params![workspace_id, rel_path],
            |row| row.get::<_, Option<String>>(0),
        )
        .ok()
        .flatten();
    match stored {
        // None 表示行不存在或字段为 NULL
        None => Ok(false),
        Some(actual) => Ok(actual == expected_raw_hash),
    }
}

/// 模块注册入口（供 lib.rs 调用）
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(manifest_init_schema, m)?)?;
    m.add_function(wrap_pyfunction!(manifest_upsert, m)?)?;
    m.add_function(wrap_pyfunction!(manifest_link_to_snapshot, m)?)?;
    m.add_function(wrap_pyfunction!(manifest_get, m)?)?;
    m.add_function(wrap_pyfunction!(manifest_list, m)?)?;
    m.add_function(wrap_pyfunction!(manifest_count, m)?)?;
    m.add_function(wrap_pyfunction!(snapshot_get_files, m)?)?;
    m.add_function(wrap_pyfunction!(manifest_verify_raw_hash, m)?)?;
    Ok(())
}

// ============================================
// 测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::params;
    use rusqlite::Connection;

    /// 创建 manifest schema（与 db_workspace_manifest.py:MANIFEST_SCHEMA_DDL 对齐）
    fn make_manifest_schema(conn: &Connection) {
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS workspace_manifests (\
                workspace_id INTEGER NOT NULL,\
                rel_path TEXT NOT NULL,\
                content_hash TEXT NOT NULL,\
                cas_key TEXT,\
                raw_hash TEXT,\
                source_encoding TEXT DEFAULT 'utf-8',\
                bom_kind TEXT DEFAULT 'none',\
                newline_style TEXT DEFAULT 'lf',\
                file_size INTEGER DEFAULT 0,\
                mtime_ns INTEGER DEFAULT 0,\
                is_dirty INTEGER DEFAULT 0,\
                updated_at REAL NOT NULL,\
                PRIMARY KEY (workspace_id, rel_path)\
             );\
             CREATE TABLE IF NOT EXISTS workspace_snapshot_map (\
                snapshot_id TEXT NOT NULL,\
                rel_path TEXT NOT NULL,\
                content_hash TEXT NOT NULL,\
                cas_key TEXT,\
                PRIMARY KEY (snapshot_id, rel_path)\
             );\
             CREATE INDEX IF NOT EXISTS idx_manifests_hash ON workspace_manifests(content_hash);\
             CREATE INDEX IF NOT EXISTS idx_manifests_cas ON workspace_manifests(cas_key);\
             CREATE INDEX IF NOT EXISTS idx_manifests_dirty ON workspace_manifests(workspace_id, is_dirty);",
        )
        .unwrap();
    }

    /// 插入测试 manifest 行
    fn seed_manifest(
        conn: &Connection,
        workspace_id: i64,
        rel_path: &str,
        content_hash: &str,
        cas_key: &str,
        raw_hash: &str,
        is_dirty: i64,
    ) {
        conn.execute(
            "INSERT OR REPLACE INTO workspace_manifests \
             (workspace_id, rel_path, content_hash, cas_key, raw_hash, \
             source_encoding, bom_kind, newline_style, file_size, mtime_ns, \
             is_dirty, updated_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, 'utf-8', 'none', 'lf', 100, 0, ?6, 1000.0)",
            params![
                workspace_id,
                rel_path,
                content_hash,
                cas_key,
                raw_hash,
                is_dirty
            ],
        )
        .unwrap();
    }

    /// 插入测试 snapshot 行
    fn seed_snapshot(
        conn: &Connection,
        snapshot_id: &str,
        rel_path: &str,
        content_hash: &str,
        cas_key: &str,
    ) {
        conn.execute(
            "INSERT OR REPLACE INTO workspace_snapshot_map \
             (snapshot_id, rel_path, content_hash, cas_key) \
             VALUES (?1, ?2, ?3, ?4)",
            params![snapshot_id, rel_path, content_hash, cas_key],
        )
        .unwrap();
    }

    #[test]
    fn test_manifest_get_row_to_dict_complete() {
        // manifest_row_to_dict 应正确读取所有 12 字段
        let conn = Connection::open_in_memory().unwrap();
        make_manifest_schema(&conn);
        seed_manifest(&conn, 1, "src/main.py", "hash1", "ck1", "raw1", 1);

        // 通过直接 SQL 查询验证（不通过 PyO3，因 Python token 在测试中不可用）
        let row = conn
            .query_row(
                "SELECT workspace_id, rel_path, content_hash, cas_key, raw_hash, \
                 source_encoding, bom_kind, newline_style, file_size, mtime_ns, \
                 is_dirty, updated_at \
                 FROM workspace_manifests WHERE workspace_id = 1 AND rel_path = 'src/main.py'",
                [],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, Option<String>>(3)?.unwrap_or_default(),
                        row.get::<_, Option<String>>(4)?.unwrap_or_default(),
                        row.get::<_, String>(5)?,
                        row.get::<_, String>(6)?,
                        row.get::<_, String>(7)?,
                        row.get::<_, i64>(8)?,
                        row.get::<_, i64>(9)?,
                        row.get::<_, i64>(10)?,
                        row.get::<_, f64>(11)?,
                    ))
                },
            )
            .unwrap();
        assert_eq!(row.0, 1);
        assert_eq!(row.1, "src/main.py");
        assert_eq!(row.2, "hash1");
        assert_eq!(row.3, "ck1");
        assert_eq!(row.4, "raw1");
        assert_eq!(row.5, "utf-8");
        assert_eq!(row.6, "none");
        assert_eq!(row.7, "lf");
        assert_eq!(row.8, 100);
        assert_eq!(row.9, 0);
        assert_eq!(row.10, 1);
        assert_eq!(row.11, 1000.0);
    }

    #[test]
    fn test_manifest_get_with_null_fields() {
        // cas_key / raw_hash 可为 NULL，应被读取为空字符串
        let conn = Connection::open_in_memory().unwrap();
        make_manifest_schema(&conn);
        conn.execute(
            "INSERT INTO workspace_manifests \
             (workspace_id, rel_path, content_hash, cas_key, raw_hash, \
             source_encoding, bom_kind, newline_style, file_size, mtime_ns, \
             is_dirty, updated_at) \
             VALUES (1, 'a.py', 'hash1', NULL, NULL, 'utf-8', 'none', 'lf', 0, 0, 0, 0.0)",
            [],
        )
        .unwrap();

        // 直接 SQL 验证 NULL 处理（与 manifest_row_to_dict 中 unwrap_or_default 一致）
        let row = conn
            .query_row(
                "SELECT cas_key, raw_hash FROM workspace_manifests WHERE workspace_id = 1 AND rel_path = 'a.py'",
                [],
                |row| {
                    Ok((
                        row.get::<_, Option<String>>(0)?.unwrap_or_default(),
                        row.get::<_, Option<String>>(1)?.unwrap_or_default(),
                    ))
                },
            )
            .unwrap();
        assert_eq!(row.0, "", "NULL cas_key 应被读取为空字符串");
        assert_eq!(row.1, "", "NULL raw_hash 应被读取为空字符串");
    }

    #[test]
    fn test_manifest_count_dirty_only() {
        let conn = Connection::open_in_memory().unwrap();
        make_manifest_schema(&conn);
        // 3 个 dirty + 2 个 clean
        seed_manifest(&conn, 1, "a.py", "h1", "ck1", "r1", 1);
        seed_manifest(&conn, 1, "b.py", "h2", "ck2", "r2", 1);
        seed_manifest(&conn, 1, "c.py", "h3", "ck3", "r3", 1);
        seed_manifest(&conn, 1, "d.py", "h4", "ck4", "r4", 0);
        seed_manifest(&conn, 1, "e.py", "h5", "ck5", "r5", 0);

        // 模拟 manifest_count 的 SQL 逻辑
        let all_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM workspace_manifests WHERE workspace_id = 1",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(all_count, 5);

        let dirty_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM workspace_manifests WHERE workspace_id = 1 AND is_dirty = 1",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(dirty_count, 3);
    }

    #[test]
    fn test_manifest_count_table_not_exists() {
        // 表不存在时 manifest_count 应返回 0（与 cas_global_count_files 一致）
        let conn = Connection::open_in_memory().unwrap();
        // 不创建 workspace_manifests 表
        let count: Option<i64> = conn
            .query_row(
                "SELECT COUNT(*) FROM workspace_manifests WHERE workspace_id = 1",
                [],
                |row| row.get(0),
            )
            .ok();
        assert_eq!(
            count, None,
            "表不存在时 query_row 返回 Err，应被转为 None → 0"
        );
    }

    #[test]
    fn test_snapshot_get_files_empty() {
        // snapshot 不存在 → 返回空列表
        let conn = Connection::open_in_memory().unwrap();
        make_manifest_schema(&conn);
        let mut stmt = conn
            .prepare("SELECT snapshot_id, rel_path, content_hash, cas_key FROM workspace_snapshot_map WHERE snapshot_id = ?1")
            .unwrap();
        let rows = stmt.query_map(["nonexistent"], |_| Ok(())).unwrap();
        let count = rows.count();
        assert_eq!(count, 0, "snapshot 不存在应返回 0 行");
    }

    #[test]
    fn test_snapshot_get_files_with_data() {
        let conn = Connection::open_in_memory().unwrap();
        make_manifest_schema(&conn);
        seed_snapshot(&conn, "snap1", "a.py", "hash1", "ck1");
        seed_snapshot(&conn, "snap1", "b.py", "hash2", "ck2");

        let mut stmt = conn
            .prepare("SELECT snapshot_id, rel_path, content_hash, cas_key FROM workspace_snapshot_map WHERE snapshot_id = ?1")
            .unwrap();
        let rows: Vec<(String, String, String, String)> = stmt
            .query_map(["snap1"], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, Option<String>>(3)?.unwrap_or_default(),
                ))
            })
            .unwrap()
            .filter_map(|r| r.ok())
            .collect();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].0, "snap1");
    }

    #[test]
    fn test_verify_raw_hash_match() {
        let conn = Connection::open_in_memory().unwrap();
        make_manifest_schema(&conn);
        seed_manifest(&conn, 1, "a.py", "hash1", "ck1", "raw1", 0);

        // 模拟 manifest_verify_raw_hash 的逻辑
        let stored: Option<String> = conn
            .query_row(
                "SELECT raw_hash FROM workspace_manifests WHERE workspace_id = 1 AND rel_path = 'a.py'",
                [],
                |row| row.get::<_, Option<String>>(0),
            )
            .ok()
            .flatten();
        assert_eq!(stored, Some("raw1".to_string()));
        assert_eq!(stored.as_deref(), Some("raw1"));
        assert_eq!(stored.as_deref() == Some("raw1"), true);
    }

    #[test]
    fn test_verify_raw_hash_mismatch() {
        let conn = Connection::open_in_memory().unwrap();
        make_manifest_schema(&conn);
        seed_manifest(&conn, 1, "a.py", "hash1", "ck1", "raw1", 0);

        let stored: Option<String> = conn
            .query_row(
                "SELECT raw_hash FROM workspace_manifests WHERE workspace_id = 1 AND rel_path = 'a.py'",
                [],
                |row| row.get::<_, Option<String>>(0),
            )
            .ok()
            .flatten();
        // 不匹配：stored=Some("raw1"), expected="different"
        assert_ne!(stored.as_deref(), Some("different"));
    }

    #[test]
    fn test_verify_raw_hash_not_found() {
        let conn = Connection::open_in_memory().unwrap();
        make_manifest_schema(&conn);
        // 不插入任何数据
        let stored: Option<String> = conn
            .query_row(
                "SELECT raw_hash FROM workspace_manifests WHERE workspace_id = 1 AND rel_path = 'nonexistent.py'",
                [],
                |row| row.get::<_, Option<String>>(0),
            )
            .ok()
            .flatten();
        assert_eq!(stored, None, "manifest 不存在应返回 None → false");
    }

    #[test]
    fn test_verify_raw_hash_null_field() {
        // raw_hash 字段为 NULL → stored 应被读取为 None → 返回 false
        let conn = Connection::open_in_memory().unwrap();
        make_manifest_schema(&conn);
        conn.execute(
            "INSERT INTO workspace_manifests \
             (workspace_id, rel_path, content_hash, cas_key, raw_hash, \
             source_encoding, bom_kind, newline_style, file_size, mtime_ns, \
             is_dirty, updated_at) \
             VALUES (1, 'a.py', 'hash1', 'ck1', NULL, 'utf-8', 'none', 'lf', 0, 0, 0, 0.0)",
            [],
        )
        .unwrap();

        let stored: Option<String> = conn
            .query_row(
                "SELECT raw_hash FROM workspace_manifests WHERE workspace_id = 1 AND rel_path = 'a.py'",
                [],
                |row| row.get::<_, Option<String>>(0),
            )
            .ok()
            .flatten();
        // NULL 字段被读取为 None
        assert_eq!(stored, None, "NULL raw_hash 应被读取为 None → false");
    }

    #[test]
    fn test_verify_raw_hash_empty_string_matches() {
        // raw_hash 为空字符串且 expected 也为空 → True
        let conn = Connection::open_in_memory().unwrap();
        make_manifest_schema(&conn);
        conn.execute(
            "INSERT INTO workspace_manifests \
             (workspace_id, rel_path, content_hash, cas_key, raw_hash, \
             source_encoding, bom_kind, newline_style, file_size, mtime_ns, \
             is_dirty, updated_at) \
             VALUES (1, 'a.py', 'hash1', 'ck1', '', 'utf-8', 'none', 'lf', 0, 0, 0, 0.0)",
            [],
        )
        .unwrap();

        let stored: Option<String> = conn
            .query_row(
                "SELECT raw_hash FROM workspace_manifests WHERE workspace_id = 1 AND rel_path = 'a.py'",
                [],
                |row| row.get::<_, Option<String>>(0),
            )
            .ok()
            .flatten();
        // 注意：'' 不是 NULL，会被读取为 Some("")
        assert_eq!(stored, Some("".to_string()));
        // 与空字符串 expected 比较 → True
        assert_eq!(stored.as_deref() == Some(""), true);
    }
}
