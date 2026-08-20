//! CAS 写入 PyO3 facade。
//!
//! Python 的 legacy replicator 仍需要保留 API 兼容，但不应继续复制 CAS 的
//! building -> payload -> ready 事务。此 facade 将 parse result DTO 转换为
//! daemon::cas::CasPublishInput，并复用唯一的 Rust CasStore 实现。

use pyo3::exceptions::{PyIOError, PyKeyError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rusqlite::{params, Connection};
use std::thread;
use std::time::Duration;

use crate::daemon::cas::{
    CasImportInput, CasPublishError, CasPublishInput, CasRawCallInput, CasStore, CasSymbolInput,
};

/// 打开只含 file_generations 语义的短连接（不向目标 DB 注入 CAS_SCHEMA_DDL）。
///
/// 供 Python daemon（legacy replicator）对 workspace DB 的 file_generations
/// 两阶段写复用 CasStore inner 逻辑；目标 DB 的 file_generations 表
/// 由 Python `init_session_schema` 或本函数幂等创建。
fn open_file_generations_conn(db_path: &str) -> Result<Connection, CasPublishError> {
    let conn = Connection::open(db_path)
        .map_err(|e| CasPublishError::Sqlite(e))?;
    conn.busy_timeout(Duration::from_secs(5))
        .map_err(|e| CasPublishError::Sqlite(e))?;
    // WAL 模式：与 MCP/CLI 长连接并发安全（只读连接总能读到最新已提交数据）
    let _ = conn.execute_batch("PRAGMA journal_mode=WAL;");
    CasStore::ensure_file_generations_table(&conn)?;
    Ok(conn)
}

fn required_str(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    dict.get_item(key)?
        .ok_or_else(|| PyKeyError::new_err(key.to_string()))?
        .extract::<String>()
}

fn optional_str(dict: &Bound<'_, PyDict>, key: &str, default: &str) -> PyResult<String> {
    match dict.get_item(key)? {
        Some(value) if !value.is_none() => value.extract::<String>(),
        _ => Ok(default.to_string()),
    }
}

fn optional_i64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<i64>> {
    match dict.get_item(key)? {
        Some(value) if !value.is_none() => value.extract::<i64>().map(Some),
        _ => Ok(None),
    }
}

fn optional_bool(dict: &Bound<'_, PyDict>, key: &str, default: bool) -> PyResult<bool> {
    match dict.get_item(key)? {
        Some(value) if !value.is_none() => value.extract::<bool>(),
        _ => Ok(default),
    }
}

fn required_i64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<i64> {
    dict.get_item(key)?
        .ok_or_else(|| PyKeyError::new_err(key.to_string()))?
        .extract::<i64>()
}

fn first_i64(dict: &Bound<'_, PyDict>, keys: &[&str], default: i64) -> PyResult<i64> {
    for key in keys {
        if let Some(value) = dict.get_item(key)? {
            if !value.is_none() {
                return value.extract::<i64>();
            }
        }
    }
    Ok(default)
}

fn first_optional_i64(dict: &Bound<'_, PyDict>, keys: &[&str]) -> PyResult<Option<i64>> {
    for key in keys {
        if let Some(value) = dict.get_item(key)? {
            if !value.is_none() {
                return value.extract::<i64>().map(Some);
            }
        }
    }
    Ok(None)
}

fn parse_input_from_dict(parse_result: &Bound<'_, PyDict>) -> PyResult<CasPublishInput> {
    let symbols_obj = parse_result
        .get_item("symbols")?
        .ok_or_else(|| PyKeyError::new_err("symbols"))?;
    let symbols = symbols_obj
        .cast::<PyList>()
        .map_err(|_| PyTypeError::new_err("parse_result.symbols 必须是 list[dict]"))?;
    let mut symbol_inputs = Vec::with_capacity(symbols.len());
    for item in symbols.iter() {
        let dict = item
            .cast::<PyDict>()
            .map_err(|_| PyTypeError::new_err("symbols 的元素必须是 dict"))?;
        let local_id = first_i64(dict, &["local_id", "local_symbol_id"], 0)?;
        let local_id = if local_id > 0 {
            local_id
        } else {
            // 旧 Python parse DTO 没有 local_id 时按稳定列表顺序补 1-based ID。
            (symbol_inputs.len() + 1) as i64
        };
        symbol_inputs.push(CasSymbolInput {
            local_symbol_id: local_id,
            name: optional_str(dict, "name", "")?,
            qualified_name: optional_str(dict, "qualified_name", "")?,
            parent_id: first_optional_i64(dict, &["lexical_parent_local_id", "parent_id"])?,
            kind: optional_str(dict, "kind", "function")?,
            start_line: first_i64(dict, &["start_line"], 0)?,
            end_line: first_i64(dict, &["end_line"], 0)?,
            start_col: first_i64(dict, &["start_col"], 0)?,
            end_col: first_i64(dict, &["end_col"], 0)?,
            start_byte: first_i64(dict, &["byte_start", "start_byte"], 0)?,
            end_byte: first_i64(dict, &["byte_end", "end_byte"], 0)?,
            visibility: optional_str(dict, "visibility", "private")?,
            signature: optional_str(dict, "signature", "")?,
            has_comment: optional_bool(dict, "has_comment", false)?,
            depth: first_i64(dict, &["depth"], -1)?,
            content: optional_str(dict, "content", "")?,
        });
    }

    let calls_obj = parse_result
        .get_item("raw_calls")?
        .or_else(|| parse_result.get_item("calls").ok().flatten())
        .ok_or_else(|| PyKeyError::new_err("raw_calls"))?;
    let calls = calls_obj
        .cast::<PyList>()
        .map_err(|_| PyTypeError::new_err("parse_result.raw_calls 必须是 list[dict]"))?;
    let mut raw_calls = Vec::with_capacity(calls.len());
    for item in calls.iter() {
        let dict = item
            .cast::<PyDict>()
            .map_err(|_| PyTypeError::new_err("raw_calls 的元素必须是 dict"))?;
        raw_calls.push(CasRawCallInput {
            caller_id: first_optional_i64(dict, &["caller_local_id", "caller_id"])?,
            caller_name: optional_str(dict, "caller_name", "")?,
            callee_name: optional_str(dict, "callee_name", "")?,
            line: first_i64(dict, &["call_line", "line"], 0)?,
            ordinal: first_i64(dict, &["ordinal", "call_ordinal"], 0)?,
        });
    }

    let imports = match parse_result.get_item("imports")? {
        Some(value) if !value.is_none() => {
            let list = value
                .cast::<PyList>()
                .map_err(|_| PyTypeError::new_err("parse_result.imports 必须是 list"))?;
            let mut result = Vec::with_capacity(list.len());
            for item in list.iter() {
                if let Ok(path) = item.extract::<String>() {
                    result.push(CasImportInput {
                        path,
                        kind: "import".to_string(),
                    });
                } else {
                    let dict = item
                        .cast::<PyDict>()
                        .map_err(|_| PyTypeError::new_err("imports 的元素必须是 string 或 dict"))?;
                    result.push(CasImportInput {
                        path: optional_str(dict, "path", "")?,
                        kind: optional_str(dict, "kind", "import")?,
                    });
                }
            }
            result
        }
        _ => Vec::new(),
    };

    Ok(CasPublishInput {
        file_size: first_i64(parse_result, &["file_size"], 0)?,
        total_lines: first_i64(parse_result, &["total_lines"], 0)?,
        symbols: symbol_inputs,
        raw_calls,
        imports,
    })
}

/// 复用 Rust CasStore 发布 CAS，并在成功后写 pending ref。
#[pyfunction]
#[pyo3(signature = (db_path, cas_key, content_hash, language, parse_result, workspace_id=0, max_retries=3, parser_version="0.1.0", callwarden_version="0.2.0", extraction_config_version="v1", abi_version="v1", input_abi_version="v1", final_state="ready"))]
#[allow(clippy::too_many_arguments)]
pub fn cas_publish_with_retry(
    db_path: &str,
    cas_key: &str,
    content_hash: &str,
    language: &str,
    parse_result: &Bound<'_, PyDict>,
    workspace_id: i64,
    max_retries: u32,
    parser_version: &str,
    callwarden_version: &str,
    extraction_config_version: &str,
    abi_version: &str,
    input_abi_version: &str,
    final_state: &str,
) -> PyResult<()> {
    let input = parse_input_from_dict(parse_result)?;
    let store = CasStore::open(db_path)
        .map_err(|e| PyIOError::new_err(format!("打开 CAS 数据库失败: {}", e)))?;
    let attempts = max_retries.max(1);
    let mut last_error = None;
    for attempt in 0..attempts {
        let result = if final_state == "ready" {
            store.publish(
                cas_key,
                content_hash,
                language,
                &input,
                parser_version,
                callwarden_version,
                extraction_config_version,
                abi_version,
                input_abi_version,
            )
        } else {
            store.publish_with_status(
                cas_key,
                content_hash,
                language,
                &input,
                parser_version,
                callwarden_version,
                extraction_config_version,
                abi_version,
                input_abi_version,
                final_state,
            )
        };
        match result {
            Ok(()) => {
                if workspace_id != 0 {
                    store
                        .pin(cas_key, workspace_id, 3600.0)
                        .map_err(|e| PyIOError::new_err(format!("CAS pin 失败: {}", e)))?;
                }
                return Ok(());
            }
            Err(error) => {
                if let CasPublishError::Sqlite(ref sqlite_error) = error {
                    if attempt + 1 < attempts && sqlite_error.to_string().contains("locked") {
                        last_error = Some(error.to_string());
                        thread::sleep(Duration::from_millis(100 * u64::from(attempt + 1)));
                        if store.lookup(cas_key).ok().flatten().is_some() {
                            if workspace_id != 0 {
                                store.pin(cas_key, workspace_id, 3600.0).map_err(|e| {
                                    PyIOError::new_err(format!("CAS pin 失败: {}", e))
                                })?;
                            }
                            return Ok(());
                        }
                        continue;
                    }
                }
                return Err(PyIOError::new_err(format!("CAS publish 失败: {}", error)));
            }
        }
    }
    Err(PyIOError::new_err(format!(
        "CAS publish 重试耗尽: {}",
        last_error.unwrap_or_else(|| "unknown error".to_string())
    )))
}

/// 为已命中的 ready CAS 写入 pending ref，不重新发布 payload。
#[pyfunction]
pub fn cas_pin(db_path: &str, cas_key: &str, workspace_id: i64, ttl_seconds: f64) -> PyResult<()> {
    let store = CasStore::open(db_path)
        .map_err(|e| PyIOError::new_err(format!("打开 CAS 数据库失败: {}", e)))?;
    store
        .pin(cas_key, workspace_id, ttl_seconds)
        .map_err(|e| PyIOError::new_err(format!("CAS pin 失败: {}", e)))
}

/// Local CAS 两阶段第一阶段 seen：原子记录代际，stale 时返回 false。
///
/// 对应 Python `db/db_cas.py::file_generation_seen` 的 Rust 语义
/// （CasStore::file_generation_seen_inner，BEGIN IMMEDIATE 单事务）。
/// 使用短连接直接操作目标 DB 的 file_generations 表（不注入 CAS_SCHEMA_DDL）。
#[pyfunction]
#[pyo3(signature = (db_path, workspace_id, rel_path, session_id, epoch, seq))]
pub fn cas_file_generation_seen(
    db_path: &str,
    workspace_id: i64,
    rel_path: &str,
    session_id: &str,
    epoch: i64,
    seq: i64,
) -> PyResult<bool> {
    let incoming_gen = format!("{}:{}", epoch, seq);
    let conn = open_file_generations_conn(db_path)
        .map_err(|e| PyIOError::new_err(format!("打开数据库失败: {}", e)))?;
    conn.execute_batch("BEGIN IMMEDIATE")
        .map_err(|e| PyIOError::new_err(format!("BEGIN IMMEDIATE 失败: {}", e)))?;
    let result = CasStore::file_generation_seen_inner(
        &conn,
        workspace_id,
        rel_path,
        session_id,
        epoch,
        seq,
        &incoming_gen,
    );
    match result {
        Ok(seen) => {
            conn.execute_batch("COMMIT")
                .map_err(|e| PyIOError::new_err(format!("COMMIT 失败: {}", e)))?;
            Ok(seen)
        }
        Err(e) => {
            let _ = conn.execute_batch("ROLLBACK");
            Err(PyIOError::new_err(format!("file_generation_seen 失败: {}", e)))
        }
    }
}

/// Local CAS 两阶段第二阶段 committed：条件 UPDATE 确认 manifest 已提交。
///
/// 返回 true=committed 更新成功，false=stale（其他 handler 已覆盖 seen）。
#[pyfunction]
#[pyo3(signature = (db_path, workspace_id, rel_path, epoch, seq))]
pub fn cas_file_generation_committed(
    db_path: &str,
    workspace_id: i64,
    rel_path: &str,
    epoch: i64,
    seq: i64,
) -> PyResult<bool> {
    let incoming_gen = format!("{}:{}", epoch, seq);
    let conn = open_file_generations_conn(db_path)
        .map_err(|e| PyIOError::new_err(format!("打开数据库失败: {}", e)))?;
    conn.execute_batch("BEGIN IMMEDIATE")
        .map_err(|e| PyIOError::new_err(format!("BEGIN IMMEDIATE 失败: {}", e)))?;
    let result = CasStore::file_generation_committed_inner(
        &conn,
        workspace_id,
        rel_path,
        epoch,
        seq,
        &incoming_gen,
    );
    match result {
        Ok(committed) => {
            conn.execute_batch("COMMIT")
                .map_err(|e| PyIOError::new_err(format!("COMMIT 失败: {}", e)))?;
            Ok(committed)
        }
        Err(e) => {
            let _ = conn.execute_batch("ROLLBACK");
            Err(PyIOError::new_err(format!("file_generation_committed 失败: {}", e)))
        }
    }
}

/// Local CAS 回滚 committed：仅清除 latest_committed_generation 匹配的行，
/// 让同 seq 重试时 stale 检查不会拒绝。
#[pyfunction]
#[pyo3(signature = (db_path, workspace_id, rel_path))]
pub fn cas_file_generation_uncommit(
    db_path: &str,
    workspace_id: i64,
    rel_path: &str,
) -> PyResult<bool> {
    let conn = open_file_generations_conn(db_path)
        .map_err(|e| PyIOError::new_err(format!("打开数据库失败: {}", e)))?;
    conn.execute_batch("BEGIN IMMEDIATE")
        .map_err(|e| PyIOError::new_err(format!("BEGIN IMMEDIATE 失败: {}", e)))?;
    let result = CasStore::file_generation_uncommit_inner(&conn, workspace_id, rel_path);
    match result {
        Ok(uncommitted) => {
            conn.execute_batch("COMMIT")
                .map_err(|e| PyIOError::new_err(format!("COMMIT 失败: {}", e)))?;
            Ok(uncommitted)
        }
        Err(e) => {
            let _ = conn.execute_batch("ROLLBACK");
            Err(PyIOError::new_err(format!("file_generation_uncommit 失败: {}", e)))
        }
    }
}

/// 会话级重置：新 session 建立后把 workspace 所有 file_generations 的
/// 归属 session 更新并清零 seq/seen（新 session 的 seq 从 1 开始）。
#[pyfunction]
#[pyo3(signature = (db_path, workspace_id, session_id, epoch))]
pub fn cas_file_generation_reset(
    db_path: &str,
    workspace_id: i64,
    session_id: &str,
    epoch: i64,
) -> PyResult<()> {
    let conn = open_file_generations_conn(db_path)
        .map_err(|e| PyIOError::new_err(format!("打开数据库失败: {}", e)))?;
    conn.execute_batch("BEGIN IMMEDIATE")
        .map_err(|e| PyIOError::new_err(format!("BEGIN IMMEDIATE 失败: {}", e)))?;
    let result = CasStore::file_generation_reset_inner(&conn, workspace_id, session_id, epoch);
    match result {
        Ok(()) => {
            conn.execute_batch("COMMIT")
                .map_err(|e| PyIOError::new_err(format!("COMMIT 失败: {}", e)))?;
            Ok(())
        }
        Err(e) => {
            let _ = conn.execute_batch("ROLLBACK");
            Err(PyIOError::new_err(format!("file_generation_reset 失败: {}", e)))
        }
    }
}

/// Global CAS 垃圾回收：回收超过 grace_days 未被引用的文件块。
///
/// 对应 Python `db/db_cas.py::cas_gc` 的 Rust 语义（CasStore::gc_unreferenced，
/// flock + BEGIN IMMEDIATE）。返回回收的条目数。
#[pyfunction]
#[pyo3(signature = (db_path, grace_days=30))]
pub fn cas_gc(db_path: &str, grace_days: u32) -> PyResult<u64> {
    let store = CasStore::open(db_path)
        .map_err(|e| PyIOError::new_err(format!("打开 CAS 数据库失败: {}", e)))?;
    store
        .gc_unreferenced(grace_days)
        .map_err(|e| PyIOError::new_err(format!("CAS gc 失败: {}", e)))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(cas_publish_with_retry, m)?)?;
    m.add_function(wrap_pyfunction!(cas_pin, m)?)?;
    m.add_function(wrap_pyfunction!(cas_file_generation_seen, m)?)?;
    m.add_function(wrap_pyfunction!(cas_file_generation_committed, m)?)?;
    m.add_function(wrap_pyfunction!(cas_file_generation_uncommit, m)?)?;
    m.add_function(wrap_pyfunction!(cas_file_generation_reset, m)?)?;
    m.add_function(wrap_pyfunction!(cas_gc, m)?)?;
    Ok(())
}
