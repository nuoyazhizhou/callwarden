//! Phase 2-6-1: 增量构建 PyO3 暴露层
//!
//! 对应 Python `db_build.py` 的两个增量构建函数：
//! - `compute_and_apply_symbol_diff` —— 符号 diff 计算 + 应用 is_deleted=1 删除标记（写）
//! - `load_file_result_from_db` —— 从 DB 加载已解析文件结果（只读，全量构建 _from_db 短路用）
//!
//! 设计原则（见 docs/design/phase2-6-1-incremental-build-contract.md）：
//! - `compute_and_apply_symbol_diff`：读写连接 + BEGIN IMMEDIATE 事务 + busy_timeout=5000
//! - `load_file_result_from_db`：只读连接，**不执行 WAL checkpoint(PASSIVE)**
//!   （T-1785831377543-8d626745：Windows + WAL 下 register 写事务后 checkpoint 会
//!   无限阻塞；只读连接经 WAL/-shm 总能读到最新已提交数据，checkpoint 冗余）+
//!   8s 有界超时 + 全局降级标记（超时后本次进程后续只读连接快速失败，Python 侧
//!   `_load_file_result_from_db_python` 用主连接降级查询，不挂死）
//! - 短连接：每次调用新建 + 关闭
//! - 失败不抛异常，返回 dict {"success": false, "error": str(e)}（与 Phase 2-2 一致）
//!
//! 行为契约（D1-D10 + L1-L7，见契约文档 §3）：
//! - D1-D10: compute_and_apply_symbol_diff 的符号 diff 场景
//! - L1-L7: load_file_result_from_db 的 DB 加载场景

use pyo3::exceptions::PyIOError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList};
use pyo3::Bound;
use rusqlite::params;

/// 打开读写连接（CodeGraph DB 用，写操作）
///
/// 与 batch_build_query.rs 一致：
/// - 默认 OpenFlags（READWRITE + CREATE）
/// - busy_timeout=5000
/// - WAL checkpoint 前置（确保读到最新数据）
/// - foreign_keys=OFF（与 Python 生产环境 db_base.py L2178 一致：
///   入库期间关闭外键检查，避免每次 INSERT 触发引用完整性校验；
///   rusqlite bundled 默认启用 FK，需显式关闭以对齐 Python 行为）
fn open_readwrite(db_path: &str) -> PyResult<rusqlite::Connection> {
    let conn = rusqlite::Connection::open(db_path)
        .map_err(|e| PyIOError::new_err(format!("打开 CodeGraph 数据库失败: {}", e)))?;
    conn.busy_timeout(std::time::Duration::from_secs(5))
        .map_err(|e| PyIOError::new_err(format!("设置 busy_timeout 失败: {}", e)))?;
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
    // 显式关闭 FK 检查（与 Python db_base.py L2178 一致）
    // 注意：pragma_update(None, "foreign_keys", "OFF") 传字符串会被 SQLite 解释为
    // 非空字符串（truthy=1），FK 仍启用。必须用 execute_batch 传裸 OFF 关键字，
    // 或用 pragma_update 传整数 0。此处用 execute_batch 与 Python 行为完全对齐。
    conn.execute_batch("PRAGMA foreign_keys = OFF;")
        .map_err(|e| PyIOError::new_err(format!("关闭 foreign_keys 失败: {}", e)))?;
    Ok(conn)
}

/// 打开只读连接（CodeGraph DB 用，只读查询）
///
/// 与 cas_query.rs 一致，并共享其有界超时 + 全局降级保护
/// （T-1785831377543-8d626745）：
/// - SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_URI（非 immutable=1，避免读到旧数据）
/// - busy_timeout=5000
/// - **不执行 PRAGMA wal_checkpoint(PASSIVE)**：只读连接经 WAL/-shm 总能读到
///   最新已提交数据；Windows + WAL 下 register 写事务后 checkpoint 会进入
///   SQLite 内部 sleep 循环不受 busy_timeout 控制，导致 refresh-all 无限阻塞。
/// - 8s 有界超时，超时置位全局降级标记，本次进程后续只读连接快速失败；
///   Python 侧 `_load_file_result_from_db_python` 用主连接降级查询，不挂死。
fn open_readonly(db_path: &str) -> PyResult<rusqlite::Connection> {
    crate::cas_query::open_readonly_bounded(db_path)
}

/// 将 rusqlite::Error 转换为 PyIOError（辅助函数）
fn to_pyerr(e: rusqlite::Error, ctx: &str) -> PyErr {
    PyIOError::new_err(format!("{}: {}", ctx, e))
}

/// 计算符号 diff 并应用 is_deleted=1 删除标记
///
/// 对应 Python `db_build.py::_compute_and_apply_symbol_diff`（3169-3209 行）。
///
/// 逻辑：
/// 1. 查询 prev_version_id 的所有符号（symbol_hash + qualified_name）
/// 2. 查询 curr_version_id 的所有符号（id + symbol_hash + qualified_name）
/// 3. 找出删除的符号：prev 有但 curr 没有的 qualified_name
/// 4. 对每个删除的符号，从 prev_version 查询位置信息（start_line, end_line, module_path, depth）
/// 5. INSERT 到 curr_version，标记 is_deleted=1
///
/// 返回 dict：
/// {
///     "success": bool,
///     "removed_count": usize,
///     "removed_names": Vec<String>,
///     "error": Option<String>,
/// }
#[pyfunction]
pub fn compute_and_apply_symbol_diff<'py>(
    py: Python<'py>,
    codegraph_db_path: &str,
    prev_version_id: i64,
    curr_version_id: i64,
) -> PyResult<Bound<'py, PyDict>> {
    let result_dict = PyDict::new(py);

    // 打开读写连接
    let conn = match open_readwrite(codegraph_db_path) {
        Ok(c) => c,
        Err(e) => {
            result_dict.set_item("success", false)?;
            result_dict.set_item("removed_count", 0usize)?;
            result_dict.set_item("removed_names", Vec::<String>::new())?;
            result_dict.set_item("error", format!("打开数据库失败: {}", e))?;
            return Ok(result_dict);
        }
    };

    // BEGIN IMMEDIATE 事务
    if let Err(e) = conn.execute_batch("BEGIN IMMEDIATE;") {
        result_dict.set_item("success", false)?;
        result_dict.set_item("removed_count", 0usize)?;
        result_dict.set_item("removed_names", Vec::<String>::new())?;
        result_dict.set_item("error", format!("BEGIN IMMEDIATE 失败: {}", e))?;
        return Ok(result_dict);
    }

    match compute_symbol_diff_inner(&conn, prev_version_id, curr_version_id) {
        Ok((removed_count, removed_names)) => {
            // COMMIT
            if let Err(e) = conn.execute_batch("COMMIT;") {
                result_dict.set_item("success", false)?;
                result_dict.set_item("removed_count", 0usize)?;
                result_dict.set_item("removed_names", Vec::<String>::new())?;
                result_dict.set_item("error", format!("COMMIT 失败: {}", e))?;
                return Ok(result_dict);
            }
            result_dict.set_item("success", true)?;
            result_dict.set_item("removed_count", removed_count)?;
            result_dict.set_item("removed_names", removed_names)?;
            Ok(result_dict)
        }
        Err(e) => {
            // ROLLBACK
            let _ = conn.execute_batch("ROLLBACK;");
            result_dict.set_item("success", false)?;
            result_dict.set_item("removed_count", 0usize)?;
            result_dict.set_item("removed_names", Vec::<String>::new())?;
            result_dict.set_item("error", format!("{}", e))?;
            Ok(result_dict)
        }
    }
}

/// compute_and_apply_symbol_diff 内部逻辑（事务内执行）
fn compute_symbol_diff_inner(
    conn: &rusqlite::Connection,
    prev_version_id: i64,
    curr_version_id: i64,
) -> PyResult<(usize, Vec<String>)> {
    use std::collections::{HashMap, HashSet};

    // 步骤 1：查询 prev_version 的所有符号（symbol_hash + qualified_name）
    let mut prev_symbols: HashMap<String, String> = HashMap::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT symbol_hash, qualified_name FROM file_symbol_versions WHERE file_version_id = ?",
            )
            .map_err(|e| to_pyerr(e, "查询 prev 符号失败"))?;
        let rows = stmt
            .query_map(params![prev_version_id], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
            .map_err(|e| to_pyerr(e, "查询 prev 符号 rows 失败"))?;
        for row in rows {
            let (symbol_hash, qualified_name) =
                row.map_err(|e| to_pyerr(e, "读取 prev 符号行失败"))?;
            prev_symbols.insert(qualified_name, symbol_hash);
        }
    }

    // 步骤 2：查询 curr_version 的所有符号（qualified_name 集合）
    let mut curr_names: HashSet<String> = HashSet::new();
    {
        let mut stmt = conn
            .prepare("SELECT qualified_name FROM file_symbol_versions WHERE file_version_id = ?")
            .map_err(|e| to_pyerr(e, "查询 curr 符号失败"))?;
        let rows = stmt
            .query_map(params![curr_version_id], |row| row.get::<_, String>(0))
            .map_err(|e| to_pyerr(e, "查询 curr 符号 rows 失败"))?;
        for row in rows {
            let name = row.map_err(|e| to_pyerr(e, "读取 curr 符号行失败"))?;
            curr_names.insert(name);
        }
    }

    // 步骤 3：找出删除的符号（prev 有但 curr 没有的 qualified_name）
    let removed_names: Vec<String> = prev_symbols
        .keys()
        .filter(|name| !curr_names.contains(*name))
        .cloned()
        .collect();

    // 步骤 4 + 5：对每个删除的符号，查询位置信息并 INSERT is_deleted=1
    let mut removed_count: usize = 0;
    for name in &removed_names {
        let symbol_hash = &prev_symbols[name];

        // 查询 prev_version 中的位置信息
        let maybe_row: rusqlite::Result<(i64, i64, String, i64)> = conn.query_row(
            "SELECT start_line, end_line, module_path, depth FROM file_symbol_versions WHERE file_version_id = ? AND qualified_name = ?",
            params![prev_version_id, name],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        );
        let (start_line, end_line, module_path, depth) = match maybe_row {
            Ok(v) => v,
            Err(rusqlite::Error::QueryReturnedNoRows) => continue, // prev 中无此符号（理论上不会发生）
            Err(e) => return Err(to_pyerr(e, "查询 prev 位置信息失败")),
        };

        // INSERT is_deleted=1 标记记录到 curr_version
        conn.execute(
            "INSERT INTO file_symbol_versions
             (file_version_id, symbol_hash, qualified_name, start_line, end_line, module_path, depth, is_deleted)
             VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            params![
                curr_version_id,
                symbol_hash,
                name,
                start_line,
                end_line,
                module_path,
                depth
            ],
        )
        .map_err(|e| to_pyerr(e, "INSERT is_deleted=1 失败"))?;
        removed_count += 1;
    }

    Ok((removed_count, removed_names))
}

/// 从 DB 加载已解析文件结果（增量构建 _from_db 短路用）
///
/// 对应 Python `db_build.py::_load_file_result_from_db`（1936-1993 行）。
///
/// 组装 file_versions + file_symbol_versions + symbol_contents + calls 的完整结果 dict。
/// 返回 None 表示文件版本不存在或查询失败。
#[pyfunction]
pub fn load_file_result_from_db<'py>(
    py: Python<'py>,
    codegraph_db_path: &str,
    file_instance_id: i64,
    file_version_id: i64,
    rel_path: String,
    abs_path: String,
    module_path: String,
) -> PyResult<Option<Bound<'py, PyDict>>> {
    // 打开只读连接
    let conn = match open_readonly(codegraph_db_path) {
        Ok(c) => c,
        Err(_) => return Ok(None),
    };

    // 步骤 1：查询 file_versions 获取 content_hash + total_lines
    let fv_row: rusqlite::Result<(String, i64)> = conn.query_row(
        "SELECT content_hash, total_lines FROM file_versions WHERE id = ?",
        params![file_version_id],
        |row| Ok((row.get(0)?, row.get(1)?)),
    );
    let (content_hash, total_lines) = match fv_row {
        Ok(v) => v,
        Err(rusqlite::Error::QueryReturnedNoRows) => return Ok(None), // 版本不存在
        Err(_) => return Ok(None),
    };

    // 步骤 2：查询 file_symbol_versions JOIN symbol_contents
    let mut symbols_list: Vec<Bound<'py, PyDict>> = Vec::new();
    {
        let mut stmt = match conn.prepare(
            "SELECT sv.id, sv.symbol_hash, sv.qualified_name, sv.start_line, sv.end_line,
                    sv.module_path, sv.depth, sv.is_deleted,
                    sc.name, sc.kind, sc.content, sc.signature, sc.has_comment,
                    sc.comment_content as doc_comment
             FROM file_symbol_versions sv
             JOIN symbol_contents sc ON sv.symbol_hash = sc.content_hash
             WHERE sv.file_version_id = ? AND sv.is_deleted = 0",
        ) {
            Ok(s) => s,
            Err(_) => return Ok(None),
        };
        let rows = match stmt.query_map(params![file_version_id], |row| {
            Ok((
                row.get::<_, i64>(0)?,             // sv.id
                row.get::<_, String>(1)?,          // sv.symbol_hash
                row.get::<_, String>(2)?,          // sv.qualified_name
                row.get::<_, i64>(3)?,             // sv.start_line
                row.get::<_, i64>(4)?,             // sv.end_line
                row.get::<_, String>(5)?,          // sv.module_path
                row.get::<_, i64>(6)?,             // sv.depth
                row.get::<_, i64>(7)?,             // sv.is_deleted
                row.get::<_, String>(8)?,          // sc.name
                row.get::<_, String>(9)?,          // sc.kind
                row.get::<_, String>(10)?,         // sc.content
                row.get::<_, String>(11)?,         // sc.signature
                row.get::<_, i64>(12)?,            // sc.has_comment
                row.get::<_, Option<String>>(13)?, // doc_comment
            ))
        }) {
            Ok(r) => r,
            Err(_) => return Ok(None),
        };
        for row in rows {
            if let Ok((
                id,
                symbol_hash,
                qualified_name,
                start_line,
                end_line,
                sym_module_path,
                depth,
                is_deleted,
                name,
                kind,
                content,
                signature,
                has_comment,
                doc_comment,
            )) = row
            {
                let sym_dict = PyDict::new(py);
                sym_dict.set_item("id", id)?;
                sym_dict.set_item("symbol_hash", symbol_hash)?;
                sym_dict.set_item("qualified_name", qualified_name)?;
                sym_dict.set_item("start_line", start_line)?;
                sym_dict.set_item("end_line", end_line)?;
                sym_dict.set_item("module_path", sym_module_path)?;
                sym_dict.set_item("depth", depth)?;
                sym_dict.set_item("is_deleted", is_deleted)?;
                sym_dict.set_item("name", name)?;
                sym_dict.set_item("kind", kind)?;
                sym_dict.set_item("content", content)?;
                sym_dict.set_item("signature", signature)?;
                sym_dict.set_item("has_comment", has_comment)?;
                sym_dict.set_item("doc_comment", doc_comment.unwrap_or_default())?;
                // Python 路径添加 calls=[] 和 issues=[]
                let empty_calls = PyList::empty(py);
                sym_dict.set_item("calls", empty_calls)?;
                let empty_issues = PyList::empty(py);
                sym_dict.set_item("issues", empty_issues)?;
                symbols_list.push(sym_dict);
            }
        }
    }

    // 步骤 3：查询 calls JOIN symbols（通过 file_instance_id 关联）
    let mut raw_calls_list: Vec<Bound<'py, PyDict>> = Vec::new();
    {
        let mut stmt = match conn.prepare(
            "SELECT c.caller_name, c.caller_module, c.callee_name, c.callee_module,
                    c.callee_qualified, c.callee_file, c.callee_id, c.call_line, c.is_cross_file
             FROM calls c
             JOIN symbols s ON c.caller_id = s.id
             WHERE s.file_instance_id = ?",
        ) {
            Ok(s) => s,
            Err(_) => return Ok(None),
        };
        let rows = match stmt.query_map(params![file_instance_id], |row| {
            Ok((
                row.get::<_, String>(0)?, // caller_name
                row.get::<_, String>(1)?, // caller_module
                row.get::<_, String>(2)?, // callee_name
                row.get::<_, String>(3)?, // callee_module
                row.get::<_, String>(4)?, // callee_qualified
                row.get::<_, String>(5)?, // callee_file
                row.get::<_, i64>(6)?,    // callee_id
                row.get::<_, i64>(7)?,    // call_line
                row.get::<_, i64>(8)?,    // is_cross_file
            ))
        }) {
            Ok(r) => r,
            Err(_) => return Ok(None),
        };
        for row in rows {
            if let Ok((
                caller_name,
                caller_module,
                callee_name,
                callee_module,
                callee_qualified,
                callee_file,
                callee_id,
                call_line,
                is_cross_file,
            )) = row
            {
                let call_dict = PyDict::new(py);
                call_dict.set_item("caller_name", caller_name)?;
                call_dict.set_item("caller_module", caller_module)?;
                call_dict.set_item("callee_name", callee_name)?;
                call_dict.set_item("callee_module", callee_module)?;
                call_dict.set_item("callee_qualified", callee_qualified)?;
                call_dict.set_item("callee_file", callee_file)?;
                call_dict.set_item("callee_id", callee_id)?;
                call_dict.set_item("call_line", call_line)?;
                call_dict.set_item("is_cross_file", is_cross_file)?;
                raw_calls_list.push(call_dict);
            }
        }
    }

    // 组装结果 dict
    let result_dict = PyDict::new(py);
    result_dict.set_item("abs_path", abs_path)?;
    result_dict.set_item("rel_path", rel_path)?;
    result_dict.set_item("module_path", module_path)?;
    result_dict.set_item("file_instance_id", file_instance_id)?;
    result_dict.set_item("file_version_id", file_version_id)?;

    // PyList::new 在 pyo3 0.29 中返回 Result，需要 ? 解包
    let symbols_pylist = PyList::new(py, symbols_list.iter())?;
    result_dict.set_item("symbols", symbols_pylist)?;

    let raw_calls_pylist = PyList::new(py, raw_calls_list.iter())?;
    result_dict.set_item("raw_calls", raw_calls_pylist)?;

    let empty_imports = PyList::empty(py);
    result_dict.set_item("imports", empty_imports)?;
    result_dict.set_item("content_hash", content_hash)?;
    result_dict.set_item("total_lines", total_lines)?;
    let empty_inline_modules = PyList::empty(py);
    result_dict.set_item("inline_modules", empty_inline_modules)?;
    result_dict.set_item("_from_db", PyBool::new(py, true))?;

    Ok(Some(result_dict))
}
