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
//! - 只读连接**不执行 PRAGMA wal_checkpoint(PASSIVE)**
//!   （T-1785831377543-8d626745：Windows + WAL 下 register 写事务后 checkpoint 会
//!   无限阻塞；只读连接经 WAL/-shm 总能读到最新已提交数据）+ 8s 有界超时 +
//!   全局降级标记（超时后本次进程后续只读连接快速失败）
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

use pyo3::prelude::*;
use pyo3::types::PyDict;

/// 打开只读连接（CAS DB 用）
///
/// 与 cas_query.rs::open_readonly 一致，并共享其有界超时 + 全局降级保护
/// （T-1785831377543-8d626745）：
/// - SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_URI（非 immutable=1，避免读到旧数据）
/// - busy_timeout=5000
/// - **不执行 PRAGMA wal_checkpoint(PASSIVE)**：只读连接经 WAL/-shm 总能读到
///   最新已提交数据；Windows + WAL 下 register 写事务后 checkpoint 会进入
///   SQLite 内部 sleep 循环不受 busy_timeout 控制，导致无限阻塞。
/// - 8s 有界超时，超时置位全局降级标记，本次进程后续只读连接快速失败。
fn open_readonly(db_path: &str) -> PyResult<rusqlite::Connection> {
    crate::cas_query::open_readonly_bounded(db_path)
}

// CI-DEBT 深水区：merge 写路径原 `open_readwrite` 每次调用建短连接，与同进程
// Python 主连接构成双 SQLite 实例；POSIX 上 fcntl 锁按进程分配无法互斥 WAL-index，
// 短连接 drop 的 checkpoint 与 Python autocheckpoint 交错可产生撕裂页
//（database disk image is malformed，CI ubuntu 独有）。现改用进程级池连接
//（`crate::conn_pool::with_pooled_conn`）。
//
// 注意：pool 的 Mutex 仅在单次 `with_pooled_conn` 调用期间持有。下方三处
// open_readwrite 调用分属**顺序**的独立 `py.detach` 块，非嵌套，故不会重入自锁。
// 只读连接保持短连接（`open_readonly`）：只读经 WAL + `-shm` 读到最新已提交数据，
// 不做 checkpoint（见 AGENTS.md 规则 32），不参与撕裂页成因。

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

    // 1. 打开 cas_conn（只读，短连接）
    //    codegraph_conn 取自进程级池（见 crate::conn_pool）
    //    释放 GIL 做文件 IO + SQL 操作
    let result = py.detach(
        || -> Result<crate::daemon::cas_merge::MergeResult, String> {
            let cas_conn =
                open_readonly(cas_db_path).map_err(|e| format!("打开 CAS 数据库失败: {}", e))?;

            // 2. fresh DB 场景：先 init_codegraph_schema（幂等，CREATE IF NOT EXISTS）
            //    与 Python db_base.init_schema 行为一致
            // 3. 调用 merge_cas_to_codegraph
            //    内部已完成 BEGIN IMMEDIATE → 全部 SQL → COMMIT
            //    失败时内部已 ROLLBACK
            crate::conn_pool::with_pooled_conn("cas_merge", codegraph_db_path, |codegraph_conn| {
                crate::daemon::cas_merge::init_codegraph_schema(codegraph_conn)
                    .map_err(|e| format!("init_codegraph_schema 失败: {}", e))?;

                crate::daemon::cas_merge::merge_cas_to_codegraph(
                    &cas_conn,
                    codegraph_conn,
                    cas_key,
                    workspace_id,
                    rel_path,
                    abs_path,
                    content_hash,
                    language,
                    workspace_root_path,
                )
            })
            .map_err(|e| format!("打开 CodeGraph 数据库失败: {}", e))?
        },
    );

    match result {
        Ok(merge_result) => {
            // 4. 查询 calls_resolved（callee_id != 0 的 calls 数）
            //    仅统计本次 merge 的 file_instance 的 calls
            //    释放 GIL 做只读查询
            let calls_resolved: usize = if merge_result.file_instance_id > 0 {
                py.detach(|| -> usize {
                    crate::conn_pool::with_pooled_conn("cas_merge", codegraph_db_path, |conn| {
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
                    .unwrap_or(0)
                })
            } else {
                0
            };

            dict.set_item("success", true)?;
            dict.set_item("file_instance_id", merge_result.file_instance_id)?;
            dict.set_item("merge_status", merge_result.merge_status.clone())?;
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
        crate::conn_pool::with_pooled_conn("cas_merge", codegraph_db_path, |conn| {
            crate::daemon::cas_merge::init_codegraph_schema(conn)
                .map_err(|e| format!("init_codegraph_schema 失败: {}", e))?;
            Ok(())
        })
        .map_err(|e| format!("打开 CodeGraph 数据库失败: {}", e))?
    });
    result.is_ok()
}

/// 模块注册入口（供 lib.rs 调用）
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(cas_merge_to_codegraph, m)?)?;
    m.add_function(wrap_pyfunction!(cas_merge_init_schema, m)?)?;
    Ok(())
}
