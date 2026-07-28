//! Phase 3-4-1: StagingLog PyO3 暴露层（读写 API）
//!
//! 对应 Python `server/staging_log.py:StagingLog` 全部方法。
//!
//! 实现策略（与 `replicator_query.rs` 一致的无状态函数模式）：
//! - 每个 PyO3 函数接收 `log_path` 参数，内部 `StagingLog::new` 打开 + 操作 + 丢弃
//! - `StagingLog::new` 从文件恢复 `next_lsn`（取 max_lsn + 1），无需跨调用保持状态
//! - 复杂数据（entry / entries / stats）用 JSON string 传递，Python 侧 `json.loads/dumps`
//!
//! 暴露的 9 个 API：
//! 1. `staging_log_append(log_path, entry_json) -> i64`
//! 2. `staging_log_read(log_path, since_lsn) -> String`（JSON array）
//! 3. `staging_log_read_pending(log_path) -> String`（JSON array）
//! 4. `staging_log_mark_applied_batch(log_path, lsns) -> bool`
//! 5. `staging_log_mark_failed(log_path, lsn, error) -> bool`
//! 6. `staging_log_truncate(log_path, up_to_lsn) -> bool`
//! 7. `staging_log_compact_applied(log_path, workspace_id=None) -> bool`
//! 8. `staging_log_stats(log_path) -> String`（JSON object）
//! 9. `staging_log_next_lsn(log_path) -> i64`
//!
//! 设计参考：docs/design/migration-manifest.md §2.2（daemon/staging_log.rs 已实现未暴露）
//! 对齐 Python：server/staging_log.py:StagingLog（append/read/read_pending/mark_applied/
//!             mark_applied_batch/mark_failed/truncate/compact_applied/stats）

use crate::daemon::staging_log::{StagingEntry, StagingLog};
use pyo3::prelude::*;
use pyo3::exceptions::{PyIOError, PyValueError};
use serde_json;

/// 打开 StagingLog，失败时返回 PyIOError
fn open_log(log_path: &str) -> PyResult<StagingLog> {
    StagingLog::new(log_path).map_err(|e| {
        PyIOError::new_err(format!("open staging log failed: {}", e))
    })
}

/// 追加一条 staging entry，返回分配的 LSN
///
/// 与 Python `StagingLog.append(entry)` 行为一致：
/// - 自动分配 lsn（单调递增）
/// - 写入文件并 fsync（崩溃安全）
/// - entry_json 应包含 StagingEntry 的所有字段（lsn/timestamp 可为 0，由 log.append 填充）
///
/// # 参数
/// - `log_path`: staging log 文件路径（JSON Lines 格式）
/// - `entry_json`: StagingEntry 的 JSON 序列化字符串
///
/// # 返回
/// 分配的 LSN（i64）
#[pyfunction]
pub fn staging_log_append(log_path: &str, entry_json: &str) -> PyResult<i64> {
    let mut entry: StagingEntry = serde_json::from_str(entry_json).map_err(|e| {
        PyValueError::new_err(format!("invalid entry JSON: {}", e))
    })?;
    let log = open_log(log_path)?;
    log.append(&mut entry).map_err(|e| {
        PyIOError::new_err(format!("append failed: {}", e))
    })?;
    Ok(entry.lsn)
}

/// 读取从 since_lsn 开始的所有 entries（不包含 since_lsn）
///
/// 与 Python `StagingLog.read(since_lsn)` 行为一致：
/// - 文件不存在 → 返回 "[]"
/// - 损坏的行（JSON 解析失败）会被跳过
/// - 按 LSN 升序返回
///
/// # 返回
/// JSON array string（每个元素是一个 StagingEntry dict）
#[pyfunction]
pub fn staging_log_read(log_path: &str, since_lsn: i64) -> PyResult<String> {
    let log = open_log(log_path)?;
    let entries = log.read(since_lsn).map_err(|e| {
        PyIOError::new_err(format!("read failed: {}", e))
    })?;
    serde_json::to_string(&entries).map_err(|e| {
        PyValueError::new_err(format!("serialize entries failed: {}", e))
    })
}

/// 读取所有 status=pending 的 entries
///
/// 与 Python `StagingLog.read_pending()` 行为一致。
///
/// # 返回
/// JSON array string
#[pyfunction]
pub fn staging_log_read_pending(log_path: &str) -> PyResult<String> {
    let log = open_log(log_path)?;
    let entries = log.read_pending().map_err(|e| {
        PyIOError::new_err(format!("read_pending failed: {}", e))
    })?;
    serde_json::to_string(&entries).map_err(|e| {
        PyValueError::new_err(format!("serialize entries failed: {}", e))
    })
}

/// 批量标记多个 LSN 为 applied（单次文件重写）
///
/// 与 Python `StagingLog.mark_applied_batch(lsns)` 行为一致：
/// - 空列表 → 空操作，返回 true
/// - 重写整个文件（原子替换：tmp + fsync + rename）
///
/// # 返回
/// true 表示成功（失败抛 PyIOError）
#[pyfunction]
pub fn staging_log_mark_applied_batch(log_path: &str, lsns: Vec<i64>) -> PyResult<bool> {
    let log = open_log(log_path)?;
    log.mark_applied_batch(&lsns).map_err(|e| {
        PyIOError::new_err(format!("mark_applied_batch failed: {}", e))
    })?;
    Ok(true)
}

/// 标记指定 LSN 的 entry 为 failed
///
/// 与 Python `StagingLog.mark_failed(lsn, error)` 行为一致。
#[pyfunction]
pub fn staging_log_mark_failed(log_path: &str, lsn: i64, error: &str) -> PyResult<bool> {
    let log = open_log(log_path)?;
    log.mark_failed(lsn, error).map_err(|e| {
        PyIOError::new_err(format!("mark_failed failed: {}", e))
    })?;
    Ok(true)
}

/// 截断 log，删除所有 LSN <= up_to_lsn 的 entries
///
/// 与 Python `StagingLog.truncate(up_to_lsn)` 行为一致。
#[pyfunction]
pub fn staging_log_truncate(log_path: &str, up_to_lsn: i64) -> PyResult<bool> {
    let log = open_log(log_path)?;
    log.truncate(up_to_lsn).map_err(|e| {
        PyIOError::new_err(format!("truncate failed: {}", e))
    })?;
    Ok(true)
}

/// 压缩 log，删除所有 status=applied 的 entries
///
/// 与 Python `StagingLog.compact_applied(workspace_id)` 行为一致：
/// - workspace_id=None → 删除所有 applied entries
/// - workspace_id 指定 → 只删除该 workspace 的 applied entries
/// - 保留非 applied 和其他 workspace 的 applied
#[pyfunction]
#[pyo3(signature = (log_path, workspace_id=None))]
pub fn staging_log_compact_applied(
    log_path: &str,
    workspace_id: Option<String>,
) -> PyResult<bool> {
    let log = open_log(log_path)?;
    log.compact_applied(workspace_id.as_deref()).map_err(|e| {
        PyIOError::new_err(format!("compact_applied failed: {}", e))
    })?;
    Ok(true)
}

/// 返回 log 统计信息
///
/// 与 Python `StagingLog.stats()` 行为一致：
/// - total_entries / pending / applied / failed / next_lsn / log_path
///
/// # 返回
/// JSON object string
#[pyfunction]
pub fn staging_log_stats(log_path: &str) -> PyResult<String> {
    let log = open_log(log_path)?;
    let stats = log.stats().map_err(|e| {
        PyIOError::new_err(format!("stats failed: {}", e))
    })?;
    // StagingLogStats 未实现 Serialize，手动构造 JSON
    let json = serde_json::json!({
        "total_entries": stats.total_entries,
        "pending": stats.pending,
        "applied": stats.applied,
        "failed": stats.failed,
        "next_lsn": stats.next_lsn,
        "log_path": stats.log_path,
    });
    serde_json::to_string(&json).map_err(|e| {
        PyValueError::new_err(format!("serialize stats failed: {}", e))
    })
}

/// 获取下一个 LSN（不追加 entry）
///
/// 与 Python `StagingLog._next_lsn` 属性一致（用于测试 / 调试）。
#[pyfunction]
pub fn staging_log_next_lsn(log_path: &str) -> PyResult<i64> {
    let log = open_log(log_path)?;
    Ok(log.next_lsn())
}

/// 模块注册入口（供 lib.rs 调用）
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(staging_log_append, m)?)?;
    m.add_function(wrap_pyfunction!(staging_log_read, m)?)?;
    m.add_function(wrap_pyfunction!(staging_log_read_pending, m)?)?;
    m.add_function(wrap_pyfunction!(staging_log_mark_applied_batch, m)?)?;
    m.add_function(wrap_pyfunction!(staging_log_mark_failed, m)?)?;
    m.add_function(wrap_pyfunction!(staging_log_truncate, m)?)?;
    m.add_function(wrap_pyfunction!(staging_log_compact_applied, m)?)?;
    m.add_function(wrap_pyfunction!(staging_log_stats, m)?)?;
    m.add_function(wrap_pyfunction!(staging_log_next_lsn, m)?)?;
    Ok(())
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    /// 构造一条 StagingEntry 的 JSON
    fn make_entry_json(workspace_id: &str, file_path: &str) -> String {
        serde_json::json!({
            "lsn": 0,
            "timestamp": 0.0,
            "workspace_id": workspace_id,
            "file_path": file_path,
            "content_hash": "abc123",
            "language": "rust",
            "parse_delta": {},
            "resolve_delta": {},
            "frontier": {},
            "metrics_update": {},
            "status": "pending",
            "error": null
        }).to_string()
    }

    #[test]
    fn test_append_and_read_round_trip() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("test.log");
        let path_str = log_path.to_str().unwrap();

        // append 3 条
        let lsn1 = staging_log_append(path_str, &make_entry_json("ws1", "a.rs")).unwrap();
        let lsn2 = staging_log_append(path_str, &make_entry_json("ws1", "b.rs")).unwrap();
        let lsn3 = staging_log_append(path_str, &make_entry_json("ws1", "c.rs")).unwrap();

        assert_eq!(lsn1, 1);
        assert_eq!(lsn2, 2);
        assert_eq!(lsn3, 3);

        // read all
        let json = staging_log_read(path_str, 0).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 3);
        assert_eq!(entries[0]["lsn"], 1);
        assert_eq!(entries[0]["file_path"], "a.rs");
        assert_eq!(entries[2]["lsn"], 3);

        // read since_lsn=1（不包含 lsn=1）
        let json = staging_log_read(path_str, 1).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0]["lsn"], 2);
    }

    #[test]
    fn test_read_pending() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("pending.log");
        let path_str = log_path.to_str().unwrap();

        staging_log_append(path_str, &make_entry_json("ws1", "a.rs")).unwrap();
        staging_log_append(path_str, &make_entry_json("ws1", "b.rs")).unwrap();

        let json = staging_log_read_pending(path_str).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 2);
        assert!(entries.iter().all(|e| e["status"] == "pending"));
    }

    #[test]
    fn test_mark_applied_batch() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("applied.log");
        let path_str = log_path.to_str().unwrap();

        let lsn1 = staging_log_append(path_str, &make_entry_json("ws1", "a.rs")).unwrap();
        let lsn2 = staging_log_append(path_str, &make_entry_json("ws1", "b.rs")).unwrap();
        let _lsn3 = staging_log_append(path_str, &make_entry_json("ws1", "c.rs")).unwrap();

        // 标记 lsn1 和 lsn2 为 applied
        staging_log_mark_applied_batch(path_str, vec![lsn1, lsn2]).unwrap();

        // pending 应只剩 1 条
        let json = staging_log_read_pending(path_str).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0]["lsn"], 3);
    }

    #[test]
    fn test_compact_applied() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("compact.log");
        let path_str = log_path.to_str().unwrap();

        let lsn1 = staging_log_append(path_str, &make_entry_json("ws1", "a.rs")).unwrap();
        let _lsn2 = staging_log_append(path_str, &make_entry_json("ws1", "b.rs")).unwrap();

        staging_log_mark_applied_batch(path_str, vec![lsn1]).unwrap();

        // compact 后应只剩 pending（lsn2）
        staging_log_compact_applied(path_str, None).unwrap();

        let json = staging_log_read(path_str, 0).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0]["lsn"], 2);
        assert_eq!(entries[0]["status"], "pending");
    }

    #[test]
    fn test_stats() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("stats.log");
        let path_str = log_path.to_str().unwrap();

        staging_log_append(path_str, &make_entry_json("ws1", "a.rs")).unwrap();
        staging_log_append(path_str, &make_entry_json("ws1", "b.rs")).unwrap();

        let json = staging_log_stats(path_str).unwrap();
        let stats: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(stats["total_entries"], 2);
        assert_eq!(stats["pending"], 2);
        assert_eq!(stats["applied"], 0);
        assert_eq!(stats["next_lsn"], 3);
    }

    #[test]
    fn test_next_lsn_recovery() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("recover.log");
        let path_str = log_path.to_str().unwrap();

        // append 2 条
        staging_log_append(path_str, &make_entry_json("ws1", "a.rs")).unwrap();
        staging_log_append(path_str, &make_entry_json("ws1", "b.rs")).unwrap();

        // 重新打开（模拟新进程），next_lsn 应恢复为 3
        let next = staging_log_next_lsn(path_str).unwrap();
        assert_eq!(next, 3);
    }

    #[test]
    fn test_file_not_exists_read_returns_empty() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("nonexistent.log");
        let path_str = log_path.to_str().unwrap();

        // 文件不存在时 read 应返回 "[]"（StagingLog::new 会创建空文件）
        let json = staging_log_read(path_str, 0).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 0);
    }
}
