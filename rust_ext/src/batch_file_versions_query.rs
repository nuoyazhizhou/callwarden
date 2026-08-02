//! Phase 2-4: 批量文件历史版本写入 PyO3 暴露层
//!
//! 对应 Python `db_build.py::_save_file_version`：
//! - `batch_save_file_versions` —— 批量写入 file_versions + file_instances + file_contents + ast_cache
//!
//! 设计原则（见 docs/design/phase2-4-batch-save-file-versions-contract.md §5）：
//! - codegraph_db_path 用读写连接（默认 OpenFlags）
//! - busy_timeout=5000（写锁冲突最多等 5 秒）
//! - BEGIN IMMEDIATE → 全部 SQL → COMMIT/ROLLBACK
//! - 失败不抛异常，返回 dict {"success": False, "error": str(e)}
//!
//! 行为契约（V1-V12，见契约文档 §3）：
//! - 两分支：短路（content_hash 相同）+ 新版本（content_hash 变化或首次）
//! - is_current toggle：新版本 INSERT 前将旧版本 is_current=0
//! - ast_cache：JSON 元数据写入 BLOB 字段（v28+，v27 降级跳过）
//! - file_contents：INSERT OR IGNORE 去重
//! - file_instances：UPDATE current_content_hash + last_parsed + total_lines + mtime
//!
//! 不在范围（契约 §1）：
//! - _compute_and_apply_symbol_diff（Python 回调，Rust 返回 prev_version_id）
//! - _get_head_commit_cached（Python 预计算 commit_hash 传入）
//! - detect_language_from_path（Python 预计算 language 传入）
//! - os.path.getmtime（Python 预计算 mtime 传入）

use pyo3::exceptions::PyIOError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::Bound;
use rusqlite::params;
use serde::Serialize;

// ===========================================================================
// 数据结构
// ===========================================================================

/// 从 Python dict 提取的单个文件版本信息
struct FileVersionInfo {
    file_instance_id: i64,
    content_hash: String,
    mtime: f64,
    total_lines: i64,
    parsed_at: f64,
    language: String,
    commit_hash: String,
    /// ast_cache 元数据（None 表示不写 ast_cache）
    ast_cache_metadata: Option<AstCacheMetadata>,
}

/// ast_cache JSON 元数据（对应 Python `_update_ast_cache` 中的 metadata dict）
#[derive(Serialize)]
struct AstCacheMetadata {
    content_hash: String,
    file_content_hash: String,
    parsed_at: f64,
    incremental: bool,
    changed_ranges_count: i64,
    language: String,
}

/// 查询到的当前版本信息（对应 Python `_get_file_version` 返回值）
struct LatestVersion {
    id: i64,
    version_num: i64,
    content_hash: String,
}

/// 单个文件的保存结果
struct SaveResult {
    file_instance_id: i64,
    file_version_id: i64,
    is_new_version: bool,
    /// 新建版本时的前一版本 id（用于 Python 回调 _compute_and_apply_symbol_diff）
    prev_version_id: Option<i64>,
}

/// 批量保存结果汇总
struct BatchSaveVersionsResult {
    files_processed: usize,
    new_versions: usize,
    short_circuited: usize,
    results: Vec<SaveResult>,
}

impl Default for BatchSaveVersionsResult {
    fn default() -> Self {
        Self {
            files_processed: 0,
            new_versions: 0,
            short_circuited: 0,
            results: Vec::new(),
        }
    }
}

// ===========================================================================
// 工具函数
// ===========================================================================

/// 打开读写连接（与 batch_build_query.rs 一致）
fn open_readwrite(db_path: &str) -> PyResult<rusqlite::Connection> {
    let conn = rusqlite::Connection::open(db_path)
        .map_err(|e| PyIOError::new_err(format!("打开 CodeGraph 数据库失败: {}", e)))?;
    conn.busy_timeout(std::time::Duration::from_secs(5))
        .map_err(|e| PyIOError::new_err(format!("设置 busy_timeout 失败: {}", e)))?;
    Ok(conn)
}

/// 从 Python dict 提取 str 字段（必填）
fn get_str(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    let val = dict
        .get_item(key)?
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(key.to_string()))?;
    val.extract::<String>()
}

/// 从 Python dict 提取 str 字段（可选，缺失返回默认值）
fn get_str_or(dict: &Bound<'_, PyDict>, key: &str, default: &str) -> PyResult<String> {
    match dict.get_item(key)? {
        Some(val) => val.extract::<String>(),
        None => Ok(default.to_string()),
    }
}

/// 从 Python dict 提取 i64 字段（必填）
fn get_i64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<i64> {
    let val = dict
        .get_item(key)?
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(key.to_string()))?;
    val.extract::<i64>()
}

/// 从 Python dict 提取 i64 字段（可选，缺失返回默认值）
fn get_i64_or(dict: &Bound<'_, PyDict>, key: &str, default: i64) -> PyResult<i64> {
    match dict.get_item(key)? {
        Some(val) => val.extract::<i64>(),
        None => Ok(default),
    }
}

/// 从 Python dict 提取 f64 字段（必填）
fn get_f64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<f64> {
    let val = dict
        .get_item(key)?
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(key.to_string()))?;
    val.extract::<f64>()
}

/// 从 Python dict 提取 f64 字段（可选，缺失返回默认值）
fn get_f64_or(dict: &Bound<'_, PyDict>, key: &str, default: f64) -> PyResult<f64> {
    match dict.get_item(key)? {
        Some(val) => val.extract::<f64>(),
        None => Ok(default),
    }
}

/// 从 Python dict 提取 bool 字段（可选，缺失返回默认值）
fn get_bool_or(dict: &Bound<'_, PyDict>, key: &str, default: bool) -> PyResult<bool> {
    match dict.get_item(key)? {
        Some(val) => {
            // Python bool 是 int 子类，直接 extract::<bool>()
            val.extract::<bool>()
        }
        None => Ok(default),
    }
}

/// 从 Python dict 提取 AstCacheMetadata（可选，字段缺失或为 None 时返回 None）
fn extract_ast_cache_metadata(dict: &Bound<'_, PyDict>) -> PyResult<Option<AstCacheMetadata>> {
    let metadata_dict = match dict.get_item("ast_cache_metadata")? {
        Some(val) if !val.is_none() => val.extract::<Bound<'_, PyDict>>()?,
        _ => return Ok(None),
    };

    Ok(Some(AstCacheMetadata {
        content_hash: get_str(&metadata_dict, "content_hash")?,
        file_content_hash: get_str_or(&metadata_dict, "file_content_hash", "")?,
        parsed_at: get_f64_or(&metadata_dict, "parsed_at", 0.0)?,
        incremental: get_bool_or(&metadata_dict, "incremental", false)?,
        changed_ranges_count: get_i64_or(&metadata_dict, "changed_ranges_count", 0)?,
        language: get_str_or(&metadata_dict, "language", "")?,
    }))
}

/// 从 Python dict 提取 FileVersionInfo
fn extract_file_version_info(dict: &Bound<'_, PyDict>) -> PyResult<FileVersionInfo> {
    let ast_cache_metadata = extract_ast_cache_metadata(dict)?;

    Ok(FileVersionInfo {
        file_instance_id: get_i64(dict, "file_instance_id")?,
        content_hash: get_str(dict, "content_hash")?,
        mtime: get_f64(dict, "mtime")?,
        total_lines: get_i64_or(dict, "total_lines", 0)?,
        parsed_at: get_f64(dict, "parsed_at")?,
        language: get_str_or(dict, "language", "")?,
        commit_hash: get_str_or(dict, "commit_hash", "")?,
        ast_cache_metadata,
    })
}

/// 检测 file_versions 表是否有 ast_cache 字段（v28+）
///
/// 对应 Python `_update_ast_cache` 的 try/except 降级逻辑：
/// v27 库没有 ast_cache 字段，sqlite3.OperationalError 被静默吞掉
fn check_ast_cache_column_exists(conn: &rusqlite::Connection) -> Result<bool, rusqlite::Error> {
    let mut stmt = conn.prepare("PRAGMA table_info(file_versions)")?;
    let column_iter = stmt.query_map([], |row| row.get::<_, String>(1))?;
    for col in column_iter {
        let col_name = col?;
        if col_name == "ast_cache" {
            return Ok(true);
        }
    }
    Ok(false)
}

/// 查询当前 is_current=1 的版本（对应 Python `_get_file_version`）
///
/// SQL: `WHERE file_instance_id=? AND is_current=1 ORDER BY version_num DESC LIMIT 1`
fn get_latest_version(
    conn: &rusqlite::Connection,
    file_instance_id: i64,
) -> Result<Option<LatestVersion>, rusqlite::Error> {
    let mut stmt = conn.prepare(
        "SELECT id, version_num, content_hash FROM file_versions \
         WHERE file_instance_id = ?1 AND is_current = 1 \
         ORDER BY version_num DESC LIMIT 1",
    )?;
    let mut rows = stmt.query(params![file_instance_id])?;
    if let Some(row) = rows.next()? {
        Ok(Some(LatestVersion {
            id: row.get(0)?,
            version_num: row.get(1)?,
            content_hash: row.get(2)?,
        }))
    } else {
        Ok(None)
    }
}

/// 写入 ast_cache 元数据（JSON 序列化为 bytes）
///
/// 对应 Python `_update_ast_cache`：
/// ```python
/// metadata = {...}
/// self.conn.execute(
///     "UPDATE file_versions SET ast_cache = ? WHERE id = ?",
///     (json.dumps(metadata).encode("utf-8"), file_version_id),
/// )
/// ```
fn update_ast_cache(
    conn: &rusqlite::Connection,
    file_version_id: i64,
    metadata: &AstCacheMetadata,
) -> Result<(), rusqlite::Error> {
    let json_bytes = serde_json::to_vec(metadata)
        .map_err(|e| rusqlite::Error::ToSqlConversionFailure(Box::new(e)))?;
    conn.execute(
        "UPDATE file_versions SET ast_cache = ?1 WHERE id = ?2",
        params![json_bytes, file_version_id],
    )?;
    Ok(())
}

/// 单个文件的版本写入（对应 Python `_save_file_version` 核心逻辑）
///
/// 两分支：
/// - 分支 A（短路）：content_hash 与 latest 相同 → UPDATE mtime+commit_hash + UPDATE file_instances + ast_cache
/// - 分支 B（新版本）：UPDATE is_current=0 + INSERT 新版本 + UPDATE file_instances + ast_cache
///
/// 返回 SaveResult（file_version_id + is_new_version + prev_version_id）
fn save_single_file_version(
    conn: &rusqlite::Connection,
    info: &FileVersionInfo,
    ast_cache_exists: bool,
) -> Result<SaveResult, rusqlite::Error> {
    // 1. INSERT OR IGNORE INTO file_contents（确保有记录）
    conn.execute(
        "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) \
         VALUES (?1, ?2, ?3, ?4)",
        params![
            &info.content_hash,
            &info.language,
            info.total_lines,
            info.parsed_at,
        ],
    )?;

    // 2. 查询当前 is_current=1 的版本
    let latest = get_latest_version(conn, info.file_instance_id)?;

    // 分支 A：短路（content_hash 与 latest 相同）
    if let Some(ref latest) = latest {
        if latest.content_hash == info.content_hash {
            // A.1 UPDATE file_versions SET mtime=?, commit_hash=? WHERE id=latest.id
            conn.execute(
                "UPDATE file_versions SET mtime = ?1, commit_hash = ?2 WHERE id = ?3",
                params![info.mtime, &info.commit_hash, latest.id],
            )?;

            // A.2 UPDATE file_instances SET current_content_hash=?, last_parsed=?, total_lines=?, mtime=?
            conn.execute(
                "UPDATE file_instances SET current_content_hash = ?1, last_parsed = ?2, \
                 total_lines = ?3, mtime = ?4 WHERE id = ?5",
                params![
                    &info.content_hash,
                    info.parsed_at,
                    info.total_lines,
                    info.mtime,
                    info.file_instance_id,
                ],
            )?;

            // A.3 更新 ast_cache 元数据（即使内容未变，记录 parsed_at 用于跨进程缓存判断）
            if ast_cache_exists {
                if let Some(ref metadata) = info.ast_cache_metadata {
                    update_ast_cache(conn, latest.id, metadata)?;
                }
            }

            return Ok(SaveResult {
                file_instance_id: info.file_instance_id,
                file_version_id: latest.id,
                is_new_version: false,
                prev_version_id: None,
            });
        }
    }

    // 分支 B：新版本
    let prev_version_id = latest.as_ref().map(|l| l.id);

    // B.1 UPDATE 旧版本 is_current=0（如有 latest）
    let version_num = if let Some(ref latest) = latest {
        conn.execute(
            "UPDATE file_versions SET is_current = 0 WHERE id = ?1",
            params![latest.id],
        )?;
        latest.version_num + 1
    } else {
        1
    };

    // B.2 INSERT 新版本（is_current=1, is_deleted=0）
    let cur = conn.execute(
        "INSERT INTO file_versions \
         (file_instance_id, version_num, content_hash, mtime, total_lines, parsed_at, \
          is_current, is_deleted, commit_hash) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, 1, 0, ?7)",
        params![
            info.file_instance_id,
            version_num,
            &info.content_hash,
            info.mtime,
            info.total_lines,
            info.parsed_at,
            &info.commit_hash,
        ],
    )?;
    let _ = cur; // 抑制未使用变量警告
    let new_version_id = conn.last_insert_rowid();

    // B.3 UPDATE file_instances SET current_content_hash=?, last_parsed=?, total_lines=?, mtime=?
    conn.execute(
        "UPDATE file_instances SET current_content_hash = ?1, last_parsed = ?2, \
         total_lines = ?3, mtime = ?4 WHERE id = ?5",
        params![
            &info.content_hash,
            info.parsed_at,
            info.total_lines,
            info.mtime,
            info.file_instance_id,
        ],
    )?;

    // B.4 写入 ast_cache 元数据
    if ast_cache_exists {
        if let Some(ref metadata) = info.ast_cache_metadata {
            update_ast_cache(conn, new_version_id, metadata)?;
        }
    }

    // 注意：_compute_and_apply_symbol_diff 不在范围，由 Python 回调
    // Rust 返回 prev_version_id，Python 调用方负责调用 _compute_and_apply_symbol_diff

    Ok(SaveResult {
        file_instance_id: info.file_instance_id,
        file_version_id: new_version_id,
        is_new_version: true,
        prev_version_id,
    })
}

// ===========================================================================
// PyO3 入口函数
// ===========================================================================

/// 批量写入文件历史版本
///
/// 与 Python `db_build.py:_save_file_version` 行为一致：
/// - 分支 A（短路）：content_hash 与 latest 相同 → UPDATE mtime+commit_hash + UPDATE file_instances + ast_cache
/// - 分支 B（新版本）：UPDATE is_current=0 + INSERT 新版本 + UPDATE file_instances + ast_cache
///
/// 事务边界：BEGIN IMMEDIATE → 全部 SQL → COMMIT
/// 失败：返回 dict {"success": False, "error": str(e)}，不抛异常
///
/// 不在范围（由 Python 调用方处理）：
/// - _compute_and_apply_symbol_diff（Rust 返回 prev_version_id，Python 回调）
/// - _get_head_commit_cached（Python 预计算 commit_hash 传入）
/// - detect_language_from_path（Python 预计算 language 传入）
/// - os.path.getmtime（Python 预计算 mtime 传入）
#[pyfunction]
#[pyo3(signature = (codegraph_db_path, file_results))]
pub fn batch_save_file_versions<'py>(
    py: Python<'py>,
    codegraph_db_path: &str,
    file_results: Vec<Bound<'py, PyDict>>,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("success", false)?;
    dict.set_item("files_processed", 0usize)?;
    dict.set_item("new_versions", 0usize)?;
    dict.set_item("short_circuited", 0usize)?;
    dict.set_item("results", Vec::<Bound<'py, PyDict>>::new())?;
    dict.set_item("error", ())?;

    // 1. 提取 FileVersionInfo（在 GIL 内，需要访问 PyDict）
    let infos: Vec<FileVersionInfo> = match file_results
        .iter()
        .map(|d| extract_file_version_info(d))
        .collect::<PyResult<Vec<_>>>()
    {
        Ok(v) => v,
        Err(e) => {
            dict.set_item("success", false)?;
            dict.set_item("error", format!("提取 file_version 字段失败: {}", e))?;
            return Ok(dict);
        }
    };

    // 2. 打开 DB 连接
    let conn = match open_readwrite(codegraph_db_path) {
        Ok(c) => c,
        Err(e) => {
            dict.set_item("error", format!("{}", e))?;
            return Ok(dict);
        }
    };

    // 3. 检测 ast_cache 字段是否存在（v28+）
    let ast_cache_exists = match check_ast_cache_column_exists(&conn) {
        Ok(exists) => exists,
        Err(e) => {
            dict.set_item("error", format!("检测 ast_cache 字段失败: {}", e))?;
            return Ok(dict);
        }
    };

    // 4. BEGIN IMMEDIATE → 处理所有文件 → COMMIT/ROLLBACK
    let tx_result: Result<BatchSaveVersionsResult, rusqlite::Error> =
        conn.execute_batch("BEGIN IMMEDIATE").and_then(|_| {
            let mut result = BatchSaveVersionsResult::default();

            for info in &infos {
                let save_result = save_single_file_version(&conn, info, ast_cache_exists)?;
                if save_result.is_new_version {
                    result.new_versions += 1;
                } else {
                    result.short_circuited += 1;
                }
                result.files_processed += 1;
                result.results.push(save_result);
            }

            conn.execute_batch("COMMIT")?;
            Ok(result)
        });

    match tx_result {
        Ok(result) => {
            // 构建 results 列表（回到 GIL 内构建 PyDict）
            let results_list: Vec<Bound<'py, PyDict>> = result
                .results
                .iter()
                .map(|r| {
                    let d = PyDict::new(py);
                    d.set_item("file_instance_id", r.file_instance_id).unwrap();
                    d.set_item("file_version_id", r.file_version_id).unwrap();
                    d.set_item("is_new_version", r.is_new_version).unwrap();
                    d.set_item("prev_version_id", r.prev_version_id).unwrap();
                    d
                })
                .collect();

            dict.set_item("success", true)?;
            dict.set_item("files_processed", result.files_processed)?;
            dict.set_item("new_versions", result.new_versions)?;
            dict.set_item("short_circuited", result.short_circuited)?;
            dict.set_item("results", results_list)?;
            dict.set_item("error", ())?;
        }
        Err(e) => {
            // ROLLBACK（忽略 ROLLBACK 自身的错误）
            let _ = conn.execute_batch("ROLLBACK");
            dict.set_item("success", false)?;
            dict.set_item("error", format!("数据库错误: {}", e))?;
        }
    }

    Ok(dict)
}
