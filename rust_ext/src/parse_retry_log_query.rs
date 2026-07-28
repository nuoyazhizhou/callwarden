//! Phase 3-4-2: ParseRetryLog PyO3 暴露层（读写 API）
//!
//! 对应 Rust `daemon::parse_retry_log::ParseRetryLog` 全部方法。
//! Python 端目前无对应实现（ParseRetryLog 仅在 Rust daemon 内部使用），
//! 本暴露层使 Python 端可以查询/操作 parse retry log，用于：
//! - daemon 重启后重放可重试 generation（与 cw_daemon.rs 中
//!   `recover_all_workspaces_legacy` 对齐）
//! - Python 端测试 / 调试 parse retry 行为
//! - Phase 3-4 wire-production 接入（若未来 Python 端需要）
//!
//! 实现策略（与 `staging_log_query.rs` 一致的无状态函数模式）：
//! - 每个 PyO3 函数接收 `log_path` 参数，内部 `ParseRetryLog::new` 打开 + 操作 + 丢弃
//! - `ParseRetryLog::new` 从文件恢复 `next_lsn`（取 max_lsn + 1），无需跨调用保持状态
//! - 复杂数据（entry / entries）用 JSON string 传递，Python 侧 `json.loads/dumps`
//!
//! 暴露的 9 个 API：
//! 1. `parse_retry_log_append(log_path, entry_json) -> i64`
//! 2. `parse_retry_log_read(log_path, since_lsn) -> String`（JSON array）
//! 3. `parse_retry_log_read_pending(log_path) -> String`（JSON array）
//! 4. `parse_retry_log_read_retryable(log_path, max_retry) -> String`（JSON array）
//! 5. `parse_retry_log_mark_applied(log_path, lsn) -> bool`
//! 6. `parse_retry_log_mark_exhausted(log_path, lsn) -> bool`
//! 7. `parse_retry_log_increment_retry(log_path, lsn) -> bool`
//! 8. `parse_retry_log_compact(log_path) -> usize`（返回删除的条数）
//! 9. `parse_retry_log_next_lsn(log_path) -> i64`
//!
//! 设计参考：docs/design/migration-manifest.md §2.2（daemon/parse_retry_log.rs 已实现未暴露）

use crate::daemon::parse_retry_log::{ParseFailureEntry, ParseRetryLog};
use pyo3::prelude::*;
use pyo3::exceptions::{PyIOError, PyValueError};
use serde_json;

/// 打开 ParseRetryLog，失败时返回 PyIOError
fn open_log(log_path: &str) -> PyResult<ParseRetryLog> {
    ParseRetryLog::new(log_path).map_err(|e| {
        PyIOError::new_err(format!("open parse retry log failed: {}", e))
    })
}

/// 追加一条 parse 失败 entry，返回分配的 LSN
///
/// 与 Rust `ParseRetryLog::append(entry)` 行为一致：
/// - 自动分配 lsn（单调递增）
/// - 写入文件并 fsync（崩溃安全）
/// - entry_json 应包含 ParseFailureEntry 的所有字段（lsn/timestamp 可为 0，由 log.append 填充）
/// - allows_retry=true 的 entry 初始 status="pending"，allows_retry=false 初始 status="permanent"
///
/// # 参数
/// - `log_path`: parse retry log 文件路径（JSON Lines 格式）
/// - `entry_json`: ParseFailureEntry 的 JSON 序列化字符串
///
/// # 返回
/// 分配的 LSN（i64）
#[pyfunction]
pub fn parse_retry_log_append(log_path: &str, entry_json: &str) -> PyResult<i64> {
    let mut entry: ParseFailureEntry = serde_json::from_str(entry_json).map_err(|e| {
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
/// 与 Rust `ParseRetryLog::read(since_lsn)` 行为一致：
/// - 文件不存在 → 返回 "[]"
/// - 损坏的行（JSON 解析失败）会被跳过
/// - 按 LSN 升序返回
///
/// # 返回
/// JSON array string（每个元素是一个 ParseFailureEntry dict）
#[pyfunction]
pub fn parse_retry_log_read(log_path: &str, since_lsn: i64) -> PyResult<String> {
    let log = open_log(log_path)?;
    let entries = log.read(since_lsn).map_err(|e| {
        PyIOError::new_err(format!("read failed: {}", e))
    })?;
    serde_json::to_string(&entries).map_err(|e| {
        PyValueError::new_err(format!("serialize entries failed: {}", e))
    })
}

/// 读取所有 status=pending 的 entries（供 daemon 重启重放）
///
/// 与 Rust `ParseRetryLog::read_pending()` 行为一致。
///
/// # 返回
/// JSON array string
#[pyfunction]
pub fn parse_retry_log_read_pending(log_path: &str) -> PyResult<String> {
    let log = open_log(log_path)?;
    let entries = log.read_pending().map_err(|e| {
        PyIOError::new_err(format!("read_pending failed: {}", e))
    })?;
    serde_json::to_string(&entries).map_err(|e| {
        PyValueError::new_err(format!("serialize entries failed: {}", e))
    })
}

/// 读取所有可重试 entries（pending + allows_retry + retry_count < max_retry）
///
/// 与 Rust `ParseRetryLog::read_retryable(max_retry)` 行为一致。
/// 设计 §8 Phase 4：daemon 重启后只重放可重试 generation。
///
/// # 参数
/// - `log_path`: parse retry log 文件路径
/// - `max_retry`: 最大重试次数（超过则不返回）
///
/// # 返回
/// JSON array string
#[pyfunction]
pub fn parse_retry_log_read_retryable(log_path: &str, max_retry: u32) -> PyResult<String> {
    let log = open_log(log_path)?;
    let entries = log.read_retryable(max_retry).map_err(|e| {
        PyIOError::new_err(format!("read_retryable failed: {}", e))
    })?;
    serde_json::to_string(&entries).map_err(|e| {
        PyValueError::new_err(format!("serialize entries failed: {}", e))
    })
}

/// 标记指定 LSN 的 entry 为 applied（重试成功）
///
/// 与 Rust `ParseRetryLog::mark_applied(lsn)` 行为一致：
/// - 重写整个 log 文件（原子替换：truncate + write + fsync）
/// - 未找到指定 lsn → 无操作（不报错）
#[pyfunction]
pub fn parse_retry_log_mark_applied(log_path: &str, lsn: i64) -> PyResult<bool> {
    let log = open_log(log_path)?;
    log.mark_applied(lsn).map_err(|e| {
        PyIOError::new_err(format!("mark_applied failed: {}", e))
    })?;
    Ok(true)
}

/// 标记指定 LSN 的 entry 为 exhausted（重试次数耗尽）
///
/// 与 Rust `ParseRetryLog::mark_exhausted(lsn)` 行为一致。
#[pyfunction]
pub fn parse_retry_log_mark_exhausted(log_path: &str, lsn: i64) -> PyResult<bool> {
    let log = open_log(log_path)?;
    log.mark_exhausted(lsn).map_err(|e| {
        PyIOError::new_err(format!("mark_exhausted failed: {}", e))
    })?;
    Ok(true)
}

/// 增加 retry_count 并更新 last_retry_at（重试前调用）
///
/// 与 Rust `ParseRetryLog::increment_retry(lsn)` 行为一致：
/// - retry_count += 1
/// - last_retry_at = now()
/// - 重写整个 log 文件
#[pyfunction]
pub fn parse_retry_log_increment_retry(log_path: &str, lsn: i64) -> PyResult<bool> {
    let log = open_log(log_path)?;
    log.increment_retry(lsn).map_err(|e| {
        PyIOError::new_err(format!("increment_retry failed: {}", e))
    })?;
    Ok(true)
}

/// 压缩 log，删除所有 status != "pending" 的 entries（applied/exhausted/permanent）
///
/// 与 Rust `ParseRetryLog::compact()` 行为一致：
/// - 只保留 status=pending 的 entries
/// - 重写整个 log 文件
///
/// # 返回
/// 删除的条目数
#[pyfunction]
pub fn parse_retry_log_compact(log_path: &str) -> PyResult<usize> {
    let log = open_log(log_path)?;
    log.compact().map_err(|e| {
        PyIOError::new_err(format!("compact failed: {}", e))
    })
}

/// 获取下一个 LSN（不追加 entry）
///
/// 与 Rust `ParseRetryLog::next_lsn()` 行为一致（用于测试 / 调试）。
/// 打开 log 时从现有文件恢复 next_lsn（取 max_lsn + 1），文件不存在时返回 1。
#[pyfunction]
pub fn parse_retry_log_next_lsn(log_path: &str) -> PyResult<i64> {
    let log = open_log(log_path)?;
    Ok(log.next_lsn())
}

/// 模块注册入口（供 lib.rs 调用）
#[allow(dead_code)]
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parse_retry_log_append, m)?)?;
    m.add_function(wrap_pyfunction!(parse_retry_log_read, m)?)?;
    m.add_function(wrap_pyfunction!(parse_retry_log_read_pending, m)?)?;
    m.add_function(wrap_pyfunction!(parse_retry_log_read_retryable, m)?)?;
    m.add_function(wrap_pyfunction!(parse_retry_log_mark_applied, m)?)?;
    m.add_function(wrap_pyfunction!(parse_retry_log_mark_exhausted, m)?)?;
    m.add_function(wrap_pyfunction!(parse_retry_log_increment_retry, m)?)?;
    m.add_function(wrap_pyfunction!(parse_retry_log_compact, m)?)?;
    m.add_function(wrap_pyfunction!(parse_retry_log_next_lsn, m)?)?;
    Ok(())
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    /// 构造一条 ParseFailureEntry 的 JSON（allows_retry=true → status=pending）
    fn make_entry_json(workspace_id: &str, rel_path: &str, allows_retry: bool) -> String {
        serde_json::json!({
            "lsn": 0,
            "timestamp": 0.0,
            "workspace_id": workspace_id,
            "rel_path": rel_path,
            "abs_path": format!("/repo/{}", rel_path),
            "generation": "1:1",
            "language": "rust",
            "parse_status": "failed",
            "cas_state": "parse_failed",
            "reason": "test error",
            "allows_retry": allows_retry,
            "retry_count": 0,
            "last_retry_at": null,
            "status": if allows_retry { "pending" } else { "permanent" }
        }).to_string()
    }

    #[test]
    fn test_append_and_read_round_trip() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("test.log");
        let path_str = log_path.to_str().unwrap();

        // append 3 条（allows_retry=true）
        let lsn1 = parse_retry_log_append(path_str, &make_entry_json("ws1", "a.rs", true)).unwrap();
        let lsn2 = parse_retry_log_append(path_str, &make_entry_json("ws1", "b.rs", true)).unwrap();
        let lsn3 = parse_retry_log_append(path_str, &make_entry_json("ws1", "c.rs", true)).unwrap();

        assert_eq!(lsn1, 1);
        assert_eq!(lsn2, 2);
        assert_eq!(lsn3, 3);

        // read all
        let json = parse_retry_log_read(path_str, 0).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 3);
        assert_eq!(entries[0]["lsn"], 1);
        assert_eq!(entries[0]["rel_path"], "a.rs");
        assert_eq!(entries[2]["lsn"], 3);

        // read since_lsn=1（不包含 lsn=1）
        let json = parse_retry_log_read(path_str, 1).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0]["lsn"], 2);
    }

    #[test]
    fn test_read_pending() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("pending.log");
        let path_str = log_path.to_str().unwrap();

        parse_retry_log_append(path_str, &make_entry_json("ws1", "a.rs", true)).unwrap();
        parse_retry_log_append(path_str, &make_entry_json("ws1", "b.rs", true)).unwrap();
        // allows_retry=false → status=permanent，不在 pending 中
        parse_retry_log_append(path_str, &make_entry_json("ws1", "c.rs", false)).unwrap();

        let json = parse_retry_log_read_pending(path_str).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 2); // 只有 2 个 pending
        assert!(entries.iter().all(|e| e["status"] == "pending"));
    }

    #[test]
    fn test_read_retryable() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("retryable.log");
        let path_str = log_path.to_str().unwrap();

        let lsn1 = parse_retry_log_append(path_str, &make_entry_json("ws1", "a.rs", true)).unwrap();
        let lsn2 = parse_retry_log_append(path_str, &make_entry_json("ws1", "b.rs", true)).unwrap();

        // max_retry=3，初始 retry_count=0，都应可重试
        let json = parse_retry_log_read_retryable(path_str, 3).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 2);

        // increment lsn1 两次 → retry_count=2，max_retry=3 仍可重试
        parse_retry_log_increment_retry(path_str, lsn1).unwrap();
        parse_retry_log_increment_retry(path_str, lsn1).unwrap();

        let json = parse_retry_log_read_retryable(path_str, 3).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 2); // retry_count=2 < max_retry=3

        // increment lsn1 第三次 → retry_count=3，max_retry=3 不可重试
        parse_retry_log_increment_retry(path_str, lsn1).unwrap();
        let json = parse_retry_log_read_retryable(path_str, 3).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 1); // 只剩 lsn2
        assert_eq!(entries[0]["lsn"], lsn2);
    }

    #[test]
    fn test_mark_applied() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("applied.log");
        let path_str = log_path.to_str().unwrap();

        let lsn1 = parse_retry_log_append(path_str, &make_entry_json("ws1", "a.rs", true)).unwrap();
        let _lsn2 = parse_retry_log_append(path_str, &make_entry_json("ws1", "b.rs", true)).unwrap();

        // 标记 lsn1 为 applied
        parse_retry_log_mark_applied(path_str, lsn1).unwrap();

        // pending 应只剩 1 条
        let json = parse_retry_log_read_pending(path_str).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0]["lsn"], 2);
    }

    #[test]
    fn test_mark_exhausted() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("exhausted.log");
        let path_str = log_path.to_str().unwrap();

        let lsn1 = parse_retry_log_append(path_str, &make_entry_json("ws1", "a.rs", true)).unwrap();

        // 标记为 exhausted
        parse_retry_log_mark_exhausted(path_str, lsn1).unwrap();

        // pending 应为空
        let json = parse_retry_log_read_pending(path_str).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 0);

        // read all 应包含 1 条 status=exhausted
        let json = parse_retry_log_read(path_str, 0).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0]["status"], "exhausted");
    }

    #[test]
    fn test_increment_retry() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("increment.log");
        let path_str = log_path.to_str().unwrap();

        let lsn1 = parse_retry_log_append(path_str, &make_entry_json("ws1", "a.rs", true)).unwrap();

        // increment 两次
        parse_retry_log_increment_retry(path_str, lsn1).unwrap();
        parse_retry_log_increment_retry(path_str, lsn1).unwrap();

        let json = parse_retry_log_read(path_str, 0).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries[0]["retry_count"], 2);
        assert!(entries[0]["last_retry_at"].is_number());
    }

    #[test]
    fn test_compact() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("compact.log");
        let path_str = log_path.to_str().unwrap();

        let lsn1 = parse_retry_log_append(path_str, &make_entry_json("ws1", "a.rs", true)).unwrap();
        let _lsn2 = parse_retry_log_append(path_str, &make_entry_json("ws1", "b.rs", true)).unwrap();
        // allows_retry=false → status=permanent
        let _lsn3 = parse_retry_log_append(path_str, &make_entry_json("ws1", "c.rs", false)).unwrap();

        // 标记 lsn1 为 applied
        parse_retry_log_mark_applied(path_str, lsn1).unwrap();

        // compact：删除 applied（lsn1）和 permanent（lsn3），只保留 pending（lsn2）
        let removed = parse_retry_log_compact(path_str).unwrap();
        assert_eq!(removed, 2);

        let json = parse_retry_log_read(path_str, 0).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0]["lsn"], 2);
        assert_eq!(entries[0]["status"], "pending");
    }

    #[test]
    fn test_next_lsn_recovery() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("recover.log");
        let path_str = log_path.to_str().unwrap();

        // append 2 条
        parse_retry_log_append(path_str, &make_entry_json("ws1", "a.rs", true)).unwrap();
        parse_retry_log_append(path_str, &make_entry_json("ws1", "b.rs", true)).unwrap();

        // 重新打开（模拟新进程），next_lsn 应恢复为 3
        let next = parse_retry_log_next_lsn(path_str).unwrap();
        assert_eq!(next, 3);
    }

    #[test]
    fn test_file_not_exists_read_returns_empty() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("nonexistent.log");
        let path_str = log_path.to_str().unwrap();

        // 文件不存在时 read 应返回 "[]"（ParseRetryLog::new 会创建空文件）
        let json = parse_retry_log_read(path_str, 0).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 0);
    }

    #[test]
    fn test_permanent_not_in_retryable() {
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("perm.log");
        let path_str = log_path.to_str().unwrap();

        // allows_retry=false → status=permanent，不在 retryable 中
        parse_retry_log_append(path_str, &make_entry_json("ws1", "a.rs", false)).unwrap();
        parse_retry_log_append(path_str, &make_entry_json("ws1", "b.rs", true)).unwrap();

        let json = parse_retry_log_read_retryable(path_str, 3).unwrap();
        let entries: Vec<serde_json::Value> = serde_json::from_str(&json).unwrap();
        assert_eq!(entries.len(), 1); // 只有 b.rs
        assert_eq!(entries[0]["rel_path"], "b.rs");
    }
}
