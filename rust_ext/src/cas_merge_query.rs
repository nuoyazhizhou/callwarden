//! Phase 2-1: CAS→CodeGraph Merge PyO3 暴露层
//!
//! 对应 Python `db/db_cas_merge.py::merge_cas_to_codegraph`：
//! - `cas_merge_to_codegraph` —— CAS→CodeGraph DB 合并主入口（写操作）
//! - `cas_merge_init_schema` —— 幂等 schema 初始化
//!
//! 设计原则（见 docs/design/phase2-1-cas-merge-py暴露-contract.md §5）：
//! - cas_db_path 用只读连接（SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_URI）
//! - codegraph_db_path 用读写连接（默认 OpenFlags）
//! - 两端均 busy_timeout=5000（写锁冲突最多等 5 秒）
//! - 只读连接先 PRAGMA wal_checkpoint(PASSIVE) 确保 WAL 已 flush
//! - 短连接：每次调用新建 + 关闭
//! - 失败不抛异常，返回 dict {"success": False, "error": str(e)}（与 Python 行为一致）
//!
//! 行为契约（M1-M8 + S1-S2，见契约文档 §4）：
//! - M1: CAS miss → {"success": False, "error": "cas_miss"}
//! - M2: fresh CodeGraph DB → 自动 init_codegraph_schema 后合并
//! - M3: CAS hit + 已有 DB → 成功合并
//! - M4: 重复 merge 同一 cas_key → 幂等替换
//! - M5: workspace 级回扫 pass（resolve_unresolved_calls_in_workspace）
//! - M6: workspace 不存在 → INSERT OR IGNORE 自动创建
//! - M7: file_size 来自 cas_file_cache.file_size（字节）
//! - M8: ORDER BY s.id ASC LIMIT 1 稳定 resolve
//! - S1: fresh DB → init_codegraph_schema 幂等建表
//! - S2: 已有 schema → 幂等，不报错

use pyo3::exceptions::PyIOError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use rusqlite::OpenFlags;

/// 打开只读连接（CAS DB 用）
///
/// 与 cas_query.rs::open_readonly 一致：
/// - SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_URI（非 immutable=1，避免读到旧数据）
/// - busy_timeout=5000
/// - PRAGMA wal_checkpoint(PASSIVE) 确保 WAL 已 flush
fn open_readonly(db_path: &str) -> PyResult<rusqlite::Connection> {
    let conn = rusqlite::Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    )
    .map_err(|e| PyIOError::new_err(format!("打开 CAS 数据库失败: {}", e)))?;
    conn.busy_timeout(std::time::Duration::from_secs(5))
        .map_err(|e| PyIOError::new_err(format!("设置 busy_timeout 失败: {}", e)))?;
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
    Ok(conn)
}

/// 打开读写连接（CodeGraph DB 用）
///
/// - 默认 OpenFlags（READWRITE + CREATE）
/// - busy_timeout=5000（与 Python db_cas_merge.py 一致）
/// - PRAGMA 与 Python db_base.py 完全对齐，避免两个 SQLite 实例
///   对 WAL 文件操作不一致导致 database disk image is malformed
fn open_readwrite(db_path: &str) -> PyResult<rusqlite::Connection> {
    let conn = rusqlite::Connection::open(db_path)
        .map_err(|e| PyIOError::new_err(format!("打开 CodeGraph 数据库失败: {}", e)))?;
    conn.busy_timeout(std::time::Duration::from_secs(5))
        .map_err(|e| PyIOError::new_err(format!("设置 busy_timeout 失败: {}", e)))?;
    conn.pragma_update(None, "journal_mode", "WAL")
        .map_err(|e| PyIOError::new_err(format!("设置 WAL 模式失败: {}", e)))?;
    conn.pragma_update(None, "synchronous", "NORMAL")
        .map_err(|e| PyIOError::new_err(format!("设置 synchronous 失败: {}", e)))?;
    conn.pragma_update(None, "foreign_keys", "OFF")
        .map_err(|e| PyIOError::new_err(format!("设置 foreign_keys 失败: {}", e)))?;
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
    Ok(conn)
}

/// CAS→CodeGraph DB 合并主入口
///
/// 与 Python `db_cas_merge.merge_cas_to_codegraph` 行为一致：
/// - 从 cas_db_path 读取 cas_key 对应的 ParseResult（symbols + calls + imports）
/// - 写入 codegraph_db_path：
///   - INSERT OR IGNORE workspaces
///   - UPSERT file_contents + file_instances（status='parsed'）
///   - DELETE 旧 symbols + calls WHERE file_instance_id
///   - INSERT OR IGNORE symbol_contents
///   - INSERT symbols
///   - INSERT calls（含 4 策略 caller_id resolve + ORDER BY s.id ASC LIMIT 1）
///   - UPSERT workspace_manifests（is_dirty=1, file_size 来自 cas_file_cache）
/// - workspace 级回扫：resolve_unresolved_calls_in_workspace
/// - 事务边界：BEGIN IMMEDIATE → 全部 SQL → COMMIT
/// - 失败：返回 dict {"success": False, "error": str(e)}，不抛异常
///
/// Args:
///     cas_db_path: CAS 数据库路径
///     codegraph_db_path: CodeGraph 数据库路径
///     cas_key: CAS key
///     workspace_id: 数字 workspace_id
///     rel_path: 文件相对路径
///     abs_path: 文件绝对路径
///     content_hash: 文件内容 SHA-256
///     language: 语言 ID
///     workspace_root_path: workspace 根路径（用于 workspaces.root_path）
///
/// Returns:
///     {
///         "success": True/False,
///         "symbols_inserted": usize,
///         "calls_inserted": usize,
///         "calls_resolved": usize,  # callee_id != 0 的 calls 数
///         "error": Optional[str],
///     }
#[pyfunction]
#[pyo3(signature = (cas_db_path, codegraph_db_path, cas_key, workspace_id, rel_path, abs_path, content_hash, language, workspace_root_path=""))]
pub fn cas_merge_to_codegraph<'py>(
    py: Python<'py>,
    cas_db_path: &str,
    codegraph_db_path: &str,
    cas_key: &str,
    workspace_id: i64,
    rel_path: &str,
    abs_path: &str,
    content_hash: &str,
    language: &str,
    workspace_root_path: &str,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("success", false)?;
    dict.set_item("symbols_inserted", 0usize)?;
    dict.set_item("calls_inserted", 0usize)?;
    dict.set_item("calls_resolved", 0usize)?;
    dict.set_item("error", ())?;

    // 1. 打开 cas_conn（只读）+ codegraph_conn（读写）
    //    释放 GIL 做文件 IO + SQL 操作
    let result = py.detach(|| -> Result<crate::daemon::cas_merge::MergeResult, String> {
        let cas_conn = open_readonly(cas_db_path)
            .map_err(|e| format!("打开 CAS 数据库失败: {}", e))?;
        let codegraph_conn = open_readwrite(codegraph_db_path)
            .map_err(|e| format!("打开 CodeGraph 数据库失败: {}", e))?;

        // 2. fresh DB 场景：先 init_codegraph_schema（幂等，CREATE IF NOT EXISTS）
        //    与 Python db_base.init_schema 行为一致
        crate::daemon::cas_merge::init_codegraph_schema(&codegraph_conn)
            .map_err(|e| format!("init_codegraph_schema 失败: {}", e))?;

        // 3. 调用 merge_cas_to_codegraph
        //    内部已完成 BEGIN IMMEDIATE → 全部 SQL → COMMIT
        //    失败时内部已 ROLLBACK
        let merge_result = crate::daemon::cas_merge::merge_cas_to_codegraph(
            &cas_conn,
            &codegraph_conn,
            cas_key,
            workspace_id,
            rel_path,
            abs_path,
            content_hash,
            language,
            workspace_root_path,
        )?;

        Ok(merge_result)
    });

    match result {
        Ok(merge_result) => {
            // 4. 查询 calls_resolved（callee_id != 0 的 calls 数）
            //    仅统计本次 merge 的 file_instance 的 calls
            //    释放 GIL 做只读查询
            let calls_resolved: usize = if merge_result.file_instance_id > 0 {
                py.detach(|| -> usize {
                    let conn = match open_readwrite(codegraph_db_path) {
                        Ok(c) => c,
                        Err(_) => return 0,
                    };
                    let count: Option<i64> = conn
                        .query_row(
                            "SELECT COUNT(*) FROM calls \
                             WHERE caller_id IN \
                             (SELECT id FROM symbols WHERE file_instance_id = ?1) \
                             AND callee_id > 0",
                            rusqlite::params![merge_result.file_instance_id],
                            |row| row.get(0),
                        )
                        .ok();
                    count.unwrap_or(0) as usize
                })
            } else {
                0
            };

            dict.set_item("success", true)?;
            dict.set_item("symbols_inserted", merge_result.symbols_inserted)?;
            dict.set_item("calls_inserted", merge_result.calls_inserted)?;
            dict.set_item("calls_resolved", calls_resolved)?;
            dict.set_item("error", ())?;
        }
        Err(e) => {
            dict.set_item("success", false)?;
            dict.set_item("symbols_inserted", 0usize)?;
            dict.set_item("calls_inserted", 0usize)?;
            dict.set_item("calls_resolved", 0usize)?;
            dict.set_item("error", e)?;
        }
    }

    Ok(dict)
}

/// 幂等初始化 CodeGraph DB schema
///
/// 与 Python `db_base.init_schema` 行为一致（核心表子集）：
/// - CREATE TABLE IF NOT EXISTS（workspaces / file_instances / symbols / calls /
///   symbol_contents / file_contents + 索引 + FTS5 触发器）
/// - 不修改 schema_version（保持 Python 端管理）
///
/// Args:
///     codegraph_db_path: CodeGraph 数据库路径
///
/// Returns:
///     True 表示成功初始化（或已存在）
///     False 表示 schema 初始化失败（db_path 不可写等）
#[pyfunction]
pub fn cas_merge_init_schema(py: Python<'_>, codegraph_db_path: &str) -> bool {
    // 释放 GIL 做文件 IO + SQL
    let result = py.detach(|| -> Result<(), String> {
        let conn = open_readwrite(codegraph_db_path)
            .map_err(|e| format!("打开 CodeGraph 数据库失败: {}", e))?;
        crate::daemon::cas_merge::init_codegraph_schema(&conn)
            .map_err(|e| format!("init_codegraph_schema 失败: {}", e))?;
        Ok(())
    });
    result.is_ok()
}

/// 模块注册入口（供 lib.rs 调用）
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(cas_merge_to_codegraph, m)?)?;
    m.add_function(wrap_pyfunction!(cas_merge_init_schema, m)?)?;
    Ok(())
}
