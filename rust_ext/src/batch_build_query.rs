//! Phase 2-2: 批量 symbols 写入 PyO3 暴露层
//!
//! 对应 Python `db_build.py::_save_symbols_for_version`：
//! - `batch_save_symbols` —— 批量写入 symbols + symbol_contents + file_symbol_versions
//!
//! 设计原则（见 docs/design/phase2-2-batch-save-symbols-contract.md §5）：
//! - codegraph_db_path 用读写连接（默认 OpenFlags）
//! - busy_timeout=5000（写锁冲突最多等 5 秒）
//! - PRAGMA foreign_keys = ON
//! - 短连接：每次调用新建 + 关闭
//! - 失败不抛异常，返回 dict {"success": False, "error": str(e)}（与 Python 行为一致）
//!
//! 行为契约（B1-B6，见契约文档 §4.1）：
//! - B1: 单文件 N 个 symbols（无 comment）→ 批量 INSERT
//! - B2: 含 has_comment=1 → INSERT OR IGNORE + UPDATE comment_content
//! - B3: 重复调用（幂等性）→ DELETE + INSERT，数量不变
//! - B4: 已有旧 symbols（替换语义）→ DELETE 旧 + INSERT 新
//! - B5: ON CONFLICT 更新 → ON CONFLICT(file_instance_id, name, start_line) DO UPDATE
//! - B6: 空 symbols 列表 → 直接返回，不执行 DELETE 也不 INSERT（与 Python 一致）
//!
//! 注意：契约文档 §4.1 B6 原描述为"仍 DELETE 旧 symbols + calls"，但 Python 真相源
//! `_save_symbols_for_version` 在 `all_symbols` 为空时直接 return（不 DELETE）。
//! Rust 实现以 Python 真相源为准，空列表时直接返回空结果，不执行任何 SQL。

use pyo3::exceptions::PyIOError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::Bound;
use rusqlite::params;

/// 单个 symbol 信息（从 Python dict 提取）
struct SymbolInfo {
    content_hash: String,
    name: String,
    kind: String,
    qualified_name: String,
    visibility: String,
    start_line: i64,
    end_line: i64,
    start_col: i64,
    end_col: i64,
    signature: String,
    has_comment: i64,
    comment_content: String,
    module_path: String,
}

/// 批量写入结果
struct BatchSaveResult {
    symbol_contents_inserted: usize,
    symbol_contents_comment_updated: usize,
    symbols_inserted: usize,
    file_symbol_versions_inserted: usize,
    old_calls_deleted: usize,
    old_symbols_deleted: usize,
}

impl Default for BatchSaveResult {
    fn default() -> Self {
        Self {
            symbol_contents_inserted: 0,
            symbol_contents_comment_updated: 0,
            symbols_inserted: 0,
            file_symbol_versions_inserted: 0,
            old_calls_deleted: 0,
            old_symbols_deleted: 0,
        }
    }
}

/// 打开读写连接（CodeGraph DB 用）
///
/// - 默认 OpenFlags（READWRITE + CREATE）
/// - busy_timeout=5000（与 cas_merge_query 一致）
/// - PRAGMA 与 Python db_base.py 完全对齐，避免两个 SQLite 实例
///   对 WAL 文件操作不一致导致 database disk image is malformed
fn open_readwrite(db_path: &str) -> PyResult<rusqlite::Connection> {
    let conn = rusqlite::Connection::open(db_path)
        .map_err(|e| PyIOError::new_err(format!("打开 CodeGraph 数据库失败: {}", e)))?;
    // busy_timeout 必须在任何 SQL 之前设置
    conn.busy_timeout(std::time::Duration::from_secs(5))
        .map_err(|e| PyIOError::new_err(format!("设置 busy_timeout 失败: {}", e)))?;
    // WAL 模式是数据库持久化设置，但显式设置确保一致性
    // synchronous=NORMAL：WAL 模式下仅在 checkpoint 时 fsync（与 Python 一致）
    // foreign_keys=OFF：入库期间关闭外键检查（与 Python db_base.py:2178 一致）
    // wal_checkpoint(PASSIVE)：确保 Rust 连接能读到 Python 写入的最新 WAL 数据
    conn.pragma_update(None, "journal_mode", "WAL")
        .map_err(|e| PyIOError::new_err(format!("设置 WAL 模式失败: {}", e)))?;
    conn.pragma_update(None, "synchronous", "NORMAL")
        .map_err(|e| PyIOError::new_err(format!("设置 synchronous 失败: {}", e)))?;
    conn.pragma_update(None, "foreign_keys", "OFF")
        .map_err(|e| PyIOError::new_err(format!("设置 foreign_keys 失败: {}", e)))?;
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
    Ok(conn)
}

/// 从 Python dict 提取 SymbolInfo
///
/// 字段提取顺序（与 Python `_save_symbols_for_version` 对齐）：
/// - content_hash: str（必填，Python 端补算后传入）
/// - name: str
/// - kind: str
/// - qualified_name: str
/// - visibility: str（默认 "private"）
/// - start_line: int
/// - end_line: int
/// - start_col: int（默认 0）
/// - end_col: int（默认 0）
/// - signature: str（默认 ""）
/// - has_comment: int（0 或 1）
/// - comment_content: str（默认 ""）
/// - module_path: str（默认 ""）
fn extract_symbol_info(dict: &Bound<'_, PyDict>) -> PyResult<SymbolInfo> {
    let get_str = |key: &str| -> PyResult<String> {
        let val = dict
            .get_item(key)?
            .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(key.to_string()))?;
        val.extract::<String>()
    };

    let get_str_or = |key: &str, default: &str| -> PyResult<String> {
        match dict.get_item(key)? {
            Some(val) => val.extract::<String>(),
            None => Ok(default.to_string()),
        }
    };

    let get_i64 = |key: &str| -> PyResult<i64> {
        let val = dict
            .get_item(key)?
            .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(key.to_string()))?;
        val.extract::<i64>()
    };

    let get_i64_or = |key: &str, default: i64| -> PyResult<i64> {
        match dict.get_item(key)? {
            Some(val) => val.extract::<i64>(),
            None => Ok(default),
        }
    };

    Ok(SymbolInfo {
        content_hash: get_str("content_hash")?,
        name: get_str("name")?,
        kind: get_str("kind")?,
        qualified_name: get_str("qualified_name")?,
        visibility: get_str_or("visibility", "private")?,
        start_line: get_i64("start_line")?,
        end_line: get_i64("end_line")?,
        start_col: get_i64_or("start_col", 0)?,
        end_col: get_i64_or("end_col", 0)?,
        signature: get_str_or("signature", "")?,
        has_comment: get_i64_or("has_comment", 0)?,
        comment_content: get_str_or("comment_content", "")?,
        module_path: get_str_or("module_path", "")?,
    })
}

/// 批量写入 symbols + symbol_contents + file_symbol_versions
///
/// 与 Python `_save_symbols_for_version` 行为一致：
/// 1. 批量 INSERT OR IGNORE INTO symbol_contents
/// 2. 批量 UPDATE symbol_contents SET has_comment=1, comment_content=?（仅 has_comment=1）
/// 3. DELETE FROM calls WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id=?)
/// 4. DELETE FROM symbols WHERE file_instance_id=?
/// 5. 批量 INSERT INTO symbols ... ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET ...
/// 6. 批量 INSERT INTO file_symbol_versions
///
/// 注意：调用方负责 BEGIN IMMEDIATE / COMMIT 事务边界
fn batch_save_symbols_inner(
    conn: &rusqlite::Connection,
    file_instance_id: i64,
    file_version_id: i64,
    symbols: &[SymbolInfo],
) -> Result<BatchSaveResult, rusqlite::Error> {
    let mut result = BatchSaveResult::default();

    if symbols.is_empty() {
        // 与 Python 一致：空列表时直接返回，不执行 DELETE 也不 INSERT
        // Python _save_symbols_for_version 在 all_symbols 为空时直接 return（不 DELETE）
        return Ok(result);
    }

    // 1. 批量 INSERT OR IGNORE INTO symbol_contents
    //    用循环 execute 累计 changes（与 Python executemany 行为等价）
    //    注意：content 字段传 ''（与 Python 一致，symbol_contents.content 不在
    //    _save_symbols_for_version 中写入，由其他路径补全）
    result.symbol_contents_inserted = 0;
    for s in symbols {
        let affected = conn.execute(
            "INSERT OR IGNORE INTO symbol_contents \
             (content_hash, name, kind, content, signature, has_comment, comment_content, qualified_name) \
             VALUES (?1, ?2, ?3, '', ?4, ?5, ?6, ?7)",
            params![
                &s.content_hash,
                &s.name,
                &s.kind,
                &s.signature,
                s.has_comment,
                &s.comment_content,
                &s.qualified_name,
            ],
        )?;
        result.symbol_contents_inserted += affected;
    }

    // 2. 批量 UPDATE symbol_contents SET has_comment=1, comment_content=?
    //    仅 has_comment=1 的符号
    result.symbol_contents_comment_updated = 0;
    for s in symbols {
        if s.has_comment != 0 {
            let affected = conn.execute(
                "UPDATE symbol_contents SET has_comment = 1, comment_content = ?1 \
                 WHERE content_hash = ?2 AND has_comment = 0",
                params![&s.comment_content, &s.content_hash],
            )?;
            result.symbol_contents_comment_updated += affected;
        }
    }

    // 3. DELETE FROM calls WHERE caller_id IN (旧 symbols)
    result.old_calls_deleted = conn.execute(
        "DELETE FROM calls WHERE caller_id IN \
         (SELECT id FROM symbols WHERE file_instance_id = ?1)",
        params![file_instance_id],
    )?;

    // 4. DELETE FROM symbols WHERE file_instance_id = ?
    result.old_symbols_deleted = conn.execute(
        "DELETE FROM symbols WHERE file_instance_id = ?1",
        params![file_instance_id],
    )?;

    // 5. 批量 INSERT INTO symbols ... ON CONFLICT DO UPDATE
    result.symbols_inserted = 0;
    {
        let sql = "INSERT INTO symbols \
            (file_instance_id, symbol_hash, name, kind, visibility, start_line, end_line, \
             start_col, end_col, signature, has_comment, comment_status, module_path, qualified_name) \
            VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, 'pending', ?12, ?13) \
            ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET \
                symbol_hash = excluded.symbol_hash, \
                kind = excluded.kind, \
                visibility = excluded.visibility, \
                end_line = excluded.end_line, \
                start_col = excluded.start_col, \
                end_col = excluded.end_col, \
                signature = excluded.signature, \
                has_comment = excluded.has_comment, \
                module_path = excluded.module_path, \
                qualified_name = excluded.qualified_name";
        for s in symbols {
            let affected = conn.execute(
                sql,
                params![
                    file_instance_id,
                    &s.content_hash,
                    &s.name,
                    &s.kind,
                    &s.visibility,
                    s.start_line,
                    s.end_line,
                    s.start_col,
                    s.end_col,
                    &s.signature,
                    s.has_comment,
                    &s.module_path,
                    &s.qualified_name,
                ],
            )?;
            result.symbols_inserted += affected;
        }
    }

    // 6. 批量 INSERT INTO file_symbol_versions
    result.file_symbol_versions_inserted = 0;
    {
        let sql = "INSERT INTO file_symbol_versions \
            (file_version_id, symbol_hash, qualified_name, start_line, end_line, module_path, depth, is_deleted) \
            VALUES (?1, ?2, ?3, ?4, ?5, ?6, -1, 0)";
        for s in symbols {
            let affected = conn.execute(
                sql,
                params![
                    file_version_id,
                    &s.content_hash,
                    &s.qualified_name,
                    s.start_line,
                    s.end_line,
                    &s.module_path,
                ],
            )?;
            result.file_symbol_versions_inserted += affected;
        }
    }

    Ok(result)
}

/// 批量写入 symbols + symbol_contents + file_symbol_versions
///
/// 与 Python `db_build.py:_save_symbols_for_version` 行为一致：
/// - INSERT OR IGNORE INTO symbol_contents（按 content_hash 去重）
/// - UPDATE symbol_contents SET has_comment=1, comment_content=? （仅 has_comment 符号）
/// - DELETE FROM calls WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id=?)
/// - DELETE FROM symbols WHERE file_instance_id=?
/// - INSERT INTO symbols ... ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET ...
/// - INSERT INTO file_symbol_versions
///
/// 事务边界：BEGIN IMMEDIATE → 全部 SQL → COMMIT
/// 失败：返回 dict {"success": False, "error": str(e)}，不抛异常
#[pyfunction]
#[pyo3(signature = (codegraph_db_path, workspace_id, file_instance_id, file_version_id, symbols))]
pub fn batch_save_symbols<'py>(
    py: Python<'py>,
    codegraph_db_path: &str,
    workspace_id: i64,
    file_instance_id: i64,
    file_version_id: i64,
    symbols: Vec<Bound<'py, PyDict>>,
) -> PyResult<Bound<'py, PyDict>> {
    // workspace_id 当前仅作为 metadata（不写入，不校验），保留参数以匹配 Python 调用方签名
    let _ = workspace_id;

    let dict = PyDict::new(py);
    dict.set_item("success", false)?;
    dict.set_item("symbol_contents_inserted", 0usize)?;
    dict.set_item("symbol_contents_comment_updated", 0usize)?;
    dict.set_item("symbols_inserted", 0usize)?;
    dict.set_item("file_symbol_versions_inserted", 0usize)?;
    dict.set_item("old_calls_deleted", 0usize)?;
    dict.set_item("old_symbols_deleted", 0usize)?;
    dict.set_item("error", ())?;

    // 1. 提取 SymbolInfo（在 GIL 内，需要访问 PyDict）
    let symbols_info: Vec<SymbolInfo> = match symbols
        .iter()
        .map(|d| extract_symbol_info(d))
        .collect::<PyResult<Vec<_>>>()
    {
        Ok(v) => v,
        Err(e) => {
            dict.set_item("success", false)?;
            dict.set_item("error", format!("提取 symbol 字段失败: {}", e))?;
            return Ok(dict);
        }
    };

    // 2. 释放 GIL 做文件 IO + SQL 操作
    let result = py.detach(|| -> Result<BatchSaveResult, String> {
        let conn = open_readwrite(codegraph_db_path)
            .map_err(|e| format!("打开 CodeGraph 数据库失败: {}", e))?;

        // BEGIN IMMEDIATE 事务
        conn.execute_batch("BEGIN IMMEDIATE;")
            .map_err(|e| format!("BEGIN IMMEDIATE 失败: {}", e))?;

        let inner_result =
            batch_save_symbols_inner(&conn, file_instance_id, file_version_id, &symbols_info);

        match inner_result {
            Ok(r) => {
                conn.execute_batch("COMMIT;")
                    .map_err(|e| format!("COMMIT 失败: {}", e))?;
                Ok(r)
            }
            Err(e) => {
                // 失败时 ROLLBACK
                let _ = conn.execute_batch("ROLLBACK;");
                Err(format!("batch_save_symbols 失败: {}", e))
            }
        }
    });

    match result {
        Ok(r) => {
            dict.set_item("success", true)?;
            dict.set_item("symbol_contents_inserted", r.symbol_contents_inserted)?;
            dict.set_item(
                "symbol_contents_comment_updated",
                r.symbol_contents_comment_updated,
            )?;
            dict.set_item("symbols_inserted", r.symbols_inserted)?;
            dict.set_item(
                "file_symbol_versions_inserted",
                r.file_symbol_versions_inserted,
            )?;
            dict.set_item("old_calls_deleted", r.old_calls_deleted)?;
            dict.set_item("old_symbols_deleted", r.old_symbols_deleted)?;
            dict.set_item("error", ())?;
        }
        Err(e) => {
            dict.set_item("success", false)?;
            dict.set_item("error", e)?;
        }
    }

    Ok(dict)
}

/// 模块注册入口（供 lib.rs 调用）
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(batch_save_symbols, m)?)?;
    Ok(())
}
