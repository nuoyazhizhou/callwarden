//! Phase 2-6-3: 批量文件注册 PyO3 暴露层
//!
//! 对应 Python `db_build.py::_build_multi_lang` register 阶段：
//! - `batch_register_files` —— 批量注册 file_instances + 查询最新 file_versions
//!
//! 设计原则（见 docs/design/phase2-6-3-batch-register-contract.md §2）：
//! - 单一读写连接处理全部文件（连接复用）
//! - 预处理 4 条 SQL 语句，N 次执行（减少 prepare 开销）
//! - BEGIN IMMEDIATE → 全部 SQL → COMMIT（单事务批量化）
//! - busy_timeout=5000（写锁冲突最多等 5 秒）
//! - foreign_keys=OFF（与 Python db_base.py L2178 一致）
//! - 失败不抛异常，返回 dict {"success": False, "error": str(e)}（fail-soft）
//!
//! 行为契约（R1-R6 / V1-V4 / T1-T5，见契约文档 §3）：
//! - 两分支：UPDATE 现有 file_instance / INSERT 新 file_instance
//! - skip_version_lookup=True 时跳过 file_versions 查询（对应 force=True）
//! - version 查询用 is_current=1 过滤（有索引，与 Python ORDER BY version_num DESC 等价）
//!
//! 不在范围（由 Python 调用方处理）：
//! - detect_language_from_path / RustParserFacade.supports_language（Python 预过滤）
//! - _infer_module_path_generic（Python 预计算 module_path 传入）
//! - os.path.getmtime（Python 预计算 mtime 传入）
//! - norm_path（Python 预计算 rel_path/abs_path 传入）
//! - _load_file_result_from_db（已在 Phase 2-6-1 通过 PyO3 暴露）

use pyo3::exceptions::PyIOError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::Bound;
use rusqlite::params;

// ===========================================================================
// 数据结构
// ===========================================================================

/// 从 Python dict 提取的单个文件注册信息
struct FileInfo {
    rel_path: String,
    abs_path: String,
    module_path: String,
    mtime: f64,
}

/// 单个文件的注册结果
struct RegisterResult {
    rel_path: String,
    file_instance_id: i64,
    /// None 表示无版本或 skip_version_lookup=True
    version_id: Option<i64>,
    version_mtime: Option<f64>,
    version_content_hash: Option<String>,
    version_total_lines: Option<i64>,
}

// ===========================================================================
// 工具函数
// ===========================================================================

/// 打开读写连接（与 incremental_build_query.rs 一致）
///
/// 关键：foreign_keys=OFF，与 Python db_base.py L2178 对齐。
/// 原因：INSERT file_instances 时 current_content_hash='' 不是 file_contents 的真实 FK，
/// 若开启 FK 检查会触发 IntegrityError。
fn open_readwrite(db_path: &str) -> PyResult<rusqlite::Connection> {
    let conn = rusqlite::Connection::open(db_path)
        .map_err(|e| PyIOError::new_err(format!("打开 CodeGraph 数据库失败: {}", e)))?;
    conn.busy_timeout(std::time::Duration::from_secs(5))
        .map_err(|e| PyIOError::new_err(format!("设置 busy_timeout 失败: {}", e)))?;
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
    // 显式关闭 FK 检查（与 Python db_base.py L2178 一致）
    conn.execute_batch("PRAGMA foreign_keys = OFF;")
        .map_err(|e| PyIOError::new_err(format!("关闭 foreign_keys 失败: {}", e)))?;
    Ok(conn)
}

/// 从 Python dict 提取 str 字段（必填）
fn get_str(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    let val = dict
        .get_item(key)?
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(key.to_string()))?;
    val.extract::<String>()
}

/// 从 Python dict 提取 f64 字段（必填）
fn get_f64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<f64> {
    let val = dict
        .get_item(key)?
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(key.to_string()))?;
    val.extract::<f64>()
}

/// 从 Python dict 提取 FileInfo
fn extract_file_info(dict: &Bound<'_, PyDict>) -> PyResult<FileInfo> {
    Ok(FileInfo {
        rel_path: get_str(dict, "rel_path")?,
        abs_path: get_str(dict, "abs_path")?,
        module_path: get_str(dict, "module_path")?,
        mtime: get_f64(dict, "mtime")?,
    })
}

// ===========================================================================
// 核心逻辑
// ===========================================================================

/// 批量注册文件到 file_instances 表，并查询最新 file_versions
///
/// 对应 Python `_build_multi_lang` register 阶段的 `_register_file_db` + `_get_file_version` 循环。
///
/// 事务边界：BEGIN IMMEDIATE → 全部 SQL → COMMIT/ROLLBACK
/// 失败：返回 dict {"success": False, "error": str(e)}，不抛异常
///
/// 不在范围（由 Python 调用方处理）：
/// - detect_language_from_path / _infer_module_path_generic（Python 预计算）
/// - os.path.getmtime（Python 预计算）
/// - _load_file_result_from_db（Phase 2-6-1 已暴露）
#[pyfunction]
#[pyo3(signature = (codegraph_db_path, workspace_id, files, skip_version_lookup=false))]
pub fn batch_register_files<'py>(
    py: Python<'py>,
    codegraph_db_path: &str,
    workspace_id: i64,
    files: Vec<Bound<'py, PyDict>>,
    skip_version_lookup: bool,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("success", false)?;
    dict.set_item("files_processed", 0usize)?;
    dict.set_item("results", Vec::<Bound<'py, PyDict>>::new())?;
    dict.set_item("error", ())?;

    // 1. 提取 FileInfo（在 GIL 内，需要访问 PyDict）
    let infos: Vec<FileInfo> = match files
        .iter()
        .map(|d| extract_file_info(d))
        .collect::<PyResult<Vec<_>>>()
    {
        Ok(v) => v,
        Err(e) => {
            dict.set_item("error", format!("提取 file 字段失败: {}", e))?;
            return Ok(dict);
        }
    };

    // 空列表快速返回
    if infos.is_empty() {
        dict.set_item("success", true)?;
        dict.set_item("files_processed", 0usize)?;
        return Ok(dict);
    }

    // 2. 打开 DB 连接
    let conn = match open_readwrite(codegraph_db_path) {
        Ok(c) => c,
        Err(e) => {
            dict.set_item("error", format!("{}", e))?;
            return Ok(dict);
        }
    };

    // 3. BEGIN IMMEDIATE → 预处理语句 → 循环执行 → COMMIT/ROLLBACK
    let tx_result: Result<Vec<RegisterResult>, rusqlite::Error> = conn
        .execute_batch("BEGIN IMMEDIATE")
        .and_then(|_| {
            // 预处理 4 条 SQL 语句
            let mut stmt_select_instance = conn.prepare(
                "SELECT id FROM file_instances WHERE workspace_id = ?1 AND rel_path = ?2",
            )?;
            let mut stmt_update_instance = conn.prepare(
                "UPDATE file_instances SET mtime = ?1, module_path = ?2, status = 'pending' WHERE id = ?3",
            )?;
            let mut stmt_insert_instance = conn.prepare(
                "INSERT INTO file_instances \
                 (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path) \
                 VALUES (?1, ?2, ?3, '', ?4, 0, 0, 'pending', ?5)",
            )?;
            // file_versions 查询（可选）
            let mut stmt_select_version = if !skip_version_lookup {
                Some(conn.prepare(
                    "SELECT id, mtime, content_hash, total_lines FROM file_versions \
                     WHERE file_instance_id = ?1 AND is_current = 1 \
                     ORDER BY version_num DESC LIMIT 1",
                )?)
            } else {
                None
            };

            let mut results = Vec::with_capacity(infos.len());

            for info in &infos {
                // 步骤 1：查询现有 file_instance
                let existing_id: Option<i64> = {
                    let mut rows = stmt_select_instance.query(params![workspace_id, &info.rel_path])?;
                    if let Some(row) = rows.next()? {
                        Some(row.get(0)?)
                    } else {
                        None
                    }
                };

                // 步骤 2：UPDATE 或 INSERT
                let file_instance_id = if let Some(id) = existing_id {
                    // 已存在 → UPDATE
                    stmt_update_instance.execute(params![info.mtime, &info.module_path, id])?;
                    id
                } else {
                    // 不存在 → INSERT
                    stmt_insert_instance.execute(params![
                        workspace_id,
                        &info.rel_path,
                        &info.abs_path,
                        info.mtime,
                        &info.module_path,
                    ])?;
                    conn.last_insert_rowid()
                };

                // 步骤 3：查询最新版本（skip_version_lookup=False 时）
                let (version_id, version_mtime, version_content_hash, version_total_lines) =
                    if let Some(ref mut stmt) = stmt_select_version {
                        let mut rows = stmt.query(params![file_instance_id])?;
                        if let Some(row) = rows.next()? {
                            (
                                Some(row.get::<_, i64>(0)?),
                                Some(row.get::<_, f64>(1)?),
                                Some(row.get::<_, String>(2)?),
                                Some(row.get::<_, i64>(3)?),
                            )
                        } else {
                            (None, None, None, None)
                        }
                    } else {
                        (None, None, None, None)
                    };

                results.push(RegisterResult {
                    rel_path: info.rel_path.clone(),
                    file_instance_id,
                    version_id,
                    version_mtime,
                    version_content_hash,
                    version_total_lines,
                });
            }

            conn.execute_batch("COMMIT")?;
            Ok(results)
        });

    // 4. 错误处理（ROLLBACK 在 drop 时自动执行）
    match tx_result {
        Ok(results) => {
            // 构建 results 列表（回到 GIL 内构建 PyDict）
            let results_list: Vec<Bound<'py, PyDict>> = results
                .iter()
                .map(|r| {
                    let d = PyDict::new(py);
                    d.set_item("rel_path", &r.rel_path).unwrap();
                    d.set_item("file_instance_id", r.file_instance_id).unwrap();
                    // Option<i64> → Option<i64>，Python 端 None 表示无版本
                    d.set_item("version_id", r.version_id).unwrap();
                    d.set_item("version_mtime", r.version_mtime).unwrap();
                    d.set_item("version_content_hash", r.version_content_hash.clone())
                        .unwrap();
                    d.set_item("version_total_lines", r.version_total_lines).unwrap();
                    d
                })
                .collect();

            dict.set_item("success", true)?;
            dict.set_item("files_processed", results.len())?;
            dict.set_item("results", results_list)?;
            dict.set_item("error", ())?;
        }
        Err(e) => {
            // 事务在 drop 时自动 ROLLBACK
            dict.set_item("error", format!("批量注册失败: {}", e))?;
        }
    }

    Ok(dict)
}
