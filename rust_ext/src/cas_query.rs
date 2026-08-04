//! Phase 1-2: CAS 只读查询 API（PyO3 暴露层）
//!
//! 对应 Python `db/db_cas.py` 的只读查询方法：
//! - `compute_cas_key_v1` / `compute_symbol_content_hash` — 纯函数（无副作用）
//! - `cas_lookup`（→ `cas_global_lookup`）— 只命中 state='ready'
//! - `cas_get_cas_state`（→ `cas_global_get_state`）— 不过滤 state
//! - `count_cas_files`（→ `cas_global_count_files`）— 统计行数
//! - `get_file_generation`（→ `cas_local_get_file_generation`）— Local 引用层
//!
//! 设计原则（见 docs/design/phase1-cas-contract.md §5）：
//! - 只读连接（`SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_URI`，非 `immutable=1`）
//! - **不执行 WAL checkpoint(PASSIVE)**（T-1785831377543-8d626745）：只读连接经
//!   WAL + -shm 总能读到最新已提交数据，checkpoint 冗余且 Windows + WAL 下
//!   register 写事务后会无限阻塞（SQLite 内部 sleep 循环不受 busy_timeout 控制）
//! - busy_timeout=5000（与 Phase 1-1 一致）
//! - open 有界超时 8s + 全局降级标记：超时后本次进程后续只读短连接快速失败，
//!   由 Python 侧降级到主连接查询，不挂死
//! - 短连接：每次调用新建 + 关闭
//! - 纯函数不访问数据库
//!
//! 不暴露（仍走 Python）：
//! - `cas_publish` / `cas_pin` / `cas_gc` / `file_generation_seen/committed/uncommit`
//! - `merge_cas_to_codegraph`

use pyo3::exceptions::PyIOError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use rusqlite::OpenFlags;
use std::sync::atomic::{AtomicBool, Ordering};

/// 只读连接全局降级标记（T-1785831377543-8d626745）
///
/// `open_readonly_bounded` 有界超时（8s）触发后置位：本次进程后续所有
/// 只读短连接直接快速失败，由 Python 侧降级到主连接查询，避免反复卡死。
/// 一次性闩锁（不自动复位）：修复后正常路径不会触发，触发即视为环境异常。
pub(crate) static READONLY_DEGRADED: AtomicBool = AtomicBool::new(false);

/// 只读连接打开超时阈值（秒）
const READONLY_OPEN_TIMEOUT_SECS: u64 = 8;

// 注意：函数返回类型用 Option<Bound<'py, PyAny>> 而非 Option<PyObject>，
// 与 graph.rs 的 get_symbol 一致（pyo3 0.29 推荐用法）

/// 计算 CAS key（SHA-256 hex，与 Python compute_cas_key_v1 完全一致）
///
/// 输入字段拼接顺序：`content_hash|language|parser_version|callwarden_version|
/// extraction_config_version|abi_version|input_abi_version`
#[pyfunction]
pub fn compute_cas_key_v1(
    content_hash: &str,
    language: &str,
    parser_version: &str,
    callwarden_version: &str,
    extraction_config_version: &str,
    abi_version: &str,
    input_abi_version: &str,
) -> String {
    crate::daemon::cas::compute_cas_key_v1(
        content_hash,
        language,
        parser_version,
        callwarden_version,
        extraction_config_version,
        abi_version,
        input_abi_version,
    )
}

/// 计算符号正文 content_hash（SHA-256 hex，与 Python compute_symbol_content_hash 一致）
#[pyfunction]
pub fn compute_symbol_content_hash(content: &str) -> String {
    crate::daemon::cas::compute_symbol_content_hash(content)
}

/// 打开只读连接的辅助函数（内部使用）
///
/// 与 sqlite_query.rs 相同的策略：
/// - SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_URI（非 immutable=1，避免读到旧数据）
/// - busy_timeout=5000
/// - **不执行 PRAGMA wal_checkpoint(PASSIVE)**（T-1785831377543-8d626745）：
///   只读连接经 WAL + -shm 总能读到最新已提交数据，checkpoint 冗余；
///   且 Windows + WAL 模式下 register 写事务后 checkpoint 会进入
///   SQLite 内部 walIndexLock/recovery 的 sleep 循环，不受 busy_timeout 控制，
///   导致 refresh-all 无限阻塞（规则 32 hook 看门狗兜底）。
/// - **8s 有界超时**：open 在后台线程执行，超时则返回错误并置位全局降级标记
///   `READONLY_DEGRADED`，本次进程后续只读短连接快速失败，由 Python 侧降级
///   到主连接查询，不再挂死。
pub(crate) fn open_readonly_bounded(
    db_path: &str,
) -> PyResult<rusqlite::Connection> {
    // 全局降级标记已置位：快速失败，避免反复卡死
    if READONLY_DEGRADED.load(Ordering::Relaxed) {
        return Err(PyIOError::new_err(
            "只读连接已进入全局降级模式（此前 open 超时），请由 Python 主连接查询",
        ));
    }

    // 后台线程执行 open + busy_timeout，避免 SQLite 内部 sleep 循环阻塞主线程
    let db_path_owned = db_path.to_string();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let result = (|| -> rusqlite::Result<rusqlite::Connection> {
            let conn = rusqlite::Connection::open_with_flags(
                &db_path_owned,
                OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
            )?;
            conn.busy_timeout(std::time::Duration::from_secs(5))?;
            Ok(conn)
        })();
        let _ = tx.send(result);
    });

    match rx.recv_timeout(std::time::Duration::from_secs(READONLY_OPEN_TIMEOUT_SECS)) {
        Ok(Ok(conn)) => Ok(conn),
        Ok(Err(e)) => {
            Err(PyIOError::new_err(format!("打开数据库失败: {}", e)))
        }
        Err(_) => {
            // recv_timeout 超时：open 可能卡在 SQLite 内部锁循环
            READONLY_DEGRADED.store(true, Ordering::Relaxed);
            Err(PyIOError::new_err(format!(
                "打开只读数据库超时（>{}s），已进入全局降级模式",
                READONLY_OPEN_TIMEOUT_SECS
            )))
        }
    }
}

/// 打开只读连接的辅助函数（内部使用，兼容旧调用签名）
fn open_readonly(db_path: &str) -> PyResult<rusqlite::Connection> {
    open_readonly_bounded(db_path)
}

/// 查询 cas_file_cache 表（只命中 state='ready'）
///
/// 与 Python `db_cas.cas_lookup(conn, cas_key)` 行为一致：
/// - state='ready' → 返回 dict（含 12 个字段）
/// - state='building' / 'partial' / 不存在 → 返回 None
#[pyfunction]
pub fn cas_global_lookup<'py>(
    py: Python<'py>,
    db_path: &str,
    cas_key: &str,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let conn = open_readonly(db_path)?;
    let mut stmt = conn
        .prepare(
            "SELECT cas_key, content_hash, language, file_size, total_lines,
                parser_version, callwarden_version, extraction_config_version,
                abi_version, input_abi_version, state, parsed_at
         FROM cas_file_cache WHERE cas_key = ?1 AND state = 'ready'",
        )
        .map_err(|e| PyIOError::new_err(format!("prepare 失败: {}", e)))?;
    let mut rows = stmt
        .query(rusqlite::params![cas_key])
        .map_err(|e| PyIOError::new_err(format!("query 失败: {}", e)))?;

    if let Some(row) = rows
        .next()
        .map_err(|e| PyIOError::new_err(format!("fetch 失败: {}", e)))?
    {
        let dict = PyDict::new(py);
        dict.set_item(
            "cas_key",
            row.get::<_, String>(0)
                .map_err(|e| PyIOError::new_err(format!("字段读取失败 cas_key: {}", e)))?,
        )?;
        dict.set_item(
            "content_hash",
            row.get::<_, String>(1)
                .map_err(|e| PyIOError::new_err(format!("字段读取失败 content_hash: {}", e)))?,
        )?;
        dict.set_item(
            "language",
            row.get::<_, String>(2)
                .map_err(|e| PyIOError::new_err(format!("字段读取失败 language: {}", e)))?,
        )?;
        dict.set_item(
            "file_size",
            row.get::<_, i64>(3)
                .map_err(|e| PyIOError::new_err(format!("字段读取失败 file_size: {}", e)))?,
        )?;
        dict.set_item(
            "total_lines",
            row.get::<_, i64>(4)
                .map_err(|e| PyIOError::new_err(format!("字段读取失败 total_lines: {}", e)))?,
        )?;
        dict.set_item(
            "parser_version",
            row.get::<_, String>(5)
                .map_err(|e| PyIOError::new_err(format!("字段读取失败 parser_version: {}", e)))?,
        )?;
        dict.set_item(
            "callwarden_version",
            row.get::<_, String>(6).map_err(|e| {
                PyIOError::new_err(format!("字段读取失败 callwarden_version: {}", e))
            })?,
        )?;
        dict.set_item(
            "extraction_config_version",
            row.get::<_, String>(7).map_err(|e| {
                PyIOError::new_err(format!("字段读取失败 extraction_config_version: {}", e))
            })?,
        )?;
        dict.set_item(
            "abi_version",
            row.get::<_, String>(8)
                .map_err(|e| PyIOError::new_err(format!("字段读取失败 abi_version: {}", e)))?,
        )?;
        dict.set_item(
            "input_abi_version",
            row.get::<_, String>(9).map_err(|e| {
                PyIOError::new_err(format!("字段读取失败 input_abi_version: {}", e))
            })?,
        )?;
        dict.set_item(
            "state",
            row.get::<_, String>(10)
                .map_err(|e| PyIOError::new_err(format!("字段读取失败 state: {}", e)))?,
        )?;
        dict.set_item(
            "parsed_at",
            row.get::<_, f64>(11)
                .map_err(|e| PyIOError::new_err(format!("字段读取失败 parsed_at: {}", e)))?,
        )?;
        Ok(Some(dict.into_any()))
    } else {
        Ok(None)
    }
}

/// 查询 cas_file_cache.state（不经过 state 过滤）
///
/// 与 Python 路径行为对齐：
/// - 行存在 → 返回 state 字符串（'ready' / 'building' / 'partial'）
/// - 行不存在 → 返回 None
#[pyfunction]
pub fn cas_global_get_state(db_path: &str, cas_key: &str) -> PyResult<Option<String>> {
    let conn = open_readonly(db_path)?;
    let mut stmt = conn
        .prepare("SELECT state FROM cas_file_cache WHERE cas_key = ?1")
        .map_err(|e| PyIOError::new_err(format!("prepare 失败: {}", e)))?;
    let mut rows = stmt
        .query(rusqlite::params![cas_key])
        .map_err(|e| PyIOError::new_err(format!("query 失败: {}", e)))?;
    if let Some(row) = rows
        .next()
        .map_err(|e| PyIOError::new_err(format!("fetch 失败: {}", e)))?
    {
        Ok(Some(row.get::<_, String>(0).map_err(|e| {
            PyIOError::new_err(format!("字段读取失败: {}", e))
        })?))
    } else {
        Ok(None)
    }
}

/// 统计 cas_file_cache 行数（含所有 state）
///
/// 与 Python 路径行为对齐：
/// - 表存在 → 返回 COUNT(*)
/// - 表不存在 → 返回 0（与 Phase 1-1 sqlite_query_schema_version 一致）
#[pyfunction]
pub fn cas_global_count_files(db_path: &str) -> PyResult<i64> {
    let conn = open_readonly(db_path)?;
    let count: Option<i64> = conn
        .query_row("SELECT COUNT(*) FROM cas_file_cache", [], |row| row.get(0))
        .ok();
    Ok(count.unwrap_or(0))
}

/// 查询 file_generations 表（Local 引用层）
///
/// 与 Python `db_cas.file_generation_*`（内部使用的查询逻辑）行为一致：
/// - 行存在 → 返回 dict（含 7 个字段）
/// - 不存在 → 返回 None
#[pyfunction]
pub fn cas_local_get_file_generation<'py>(
    py: Python<'py>,
    db_path: &str,
    workspace_id: i64,
    rel_path: &str,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let conn = open_readonly(db_path)?;
    let mut stmt = conn
        .prepare(
            "SELECT workspace_id, rel_path, latest_session_id, latest_session_epoch,
                    latest_seq, latest_seen_generation, latest_committed_generation
             FROM file_generations WHERE workspace_id = ?1 AND rel_path = ?2",
        )
        .map_err(|e| PyIOError::new_err(format!("prepare 失败: {}", e)))?;
    let mut rows = stmt
        .query(rusqlite::params![workspace_id, rel_path])
        .map_err(|e| PyIOError::new_err(format!("query 失败: {}", e)))?;

    if let Some(row) = rows
        .next()
        .map_err(|e| PyIOError::new_err(format!("fetch 失败: {}", e)))?
    {
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
            "latest_session_id",
            row.get::<_, String>(2).map_err(|e| {
                PyIOError::new_err(format!("字段读取失败 latest_session_id: {}", e))
            })?,
        )?;
        dict.set_item(
            "latest_session_epoch",
            row.get::<_, i64>(3).map_err(|e| {
                PyIOError::new_err(format!("字段读取失败 latest_session_epoch: {}", e))
            })?,
        )?;
        dict.set_item(
            "latest_seq",
            row.get::<_, i64>(4)
                .map_err(|e| PyIOError::new_err(format!("字段读取失败 latest_seq: {}", e)))?,
        )?;
        dict.set_item(
            "latest_seen_generation",
            row.get::<_, String>(5).map_err(|e| {
                PyIOError::new_err(format!("字段读取失败 latest_seen_generation: {}", e))
            })?,
        )?;
        dict.set_item(
            "latest_committed_generation",
            row.get::<_, String>(6).map_err(|e| {
                PyIOError::new_err(format!("字段读取失败 latest_committed_generation: {}", e))
            })?,
        )?;
        Ok(Some(dict.into_any()))
    } else {
        Ok(None)
    }
}

/// 模块注册入口（供 lib.rs 调用）
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(compute_cas_key_v1, m)?)?;
    m.add_function(wrap_pyfunction!(compute_symbol_content_hash, m)?)?;
    m.add_function(wrap_pyfunction!(cas_global_lookup, m)?)?;
    m.add_function(wrap_pyfunction!(cas_global_get_state, m)?)?;
    m.add_function(wrap_pyfunction!(cas_global_count_files, m)?)?;
    m.add_function(wrap_pyfunction!(cas_local_get_file_generation, m)?)?;
    Ok(())
}
