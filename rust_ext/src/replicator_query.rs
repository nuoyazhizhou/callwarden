//! Phase 1-4: Replicator 只读查询 API（PyO3 暴露层）
//!
//! 对应 Python `server/replicator.py::Replicator.get_pending_count`：
//! - `replicator_get_pending_count`——查询 pending staging log entries 数量
//!
//! 实现策略（与 Python 端一致）：
//! - staging_log 是 JSON Lines 文件（不是 SQLite 表）
//! - 通过 `daemon::staging_log::StagingLog::read_pending()` 读文件
//! - 按 workspace_id 过滤（None 时返回所有 pending 总数）
//! - 文件不存在或损坏 → 返回 0（与 Python `Replicator.get_pending_count` 一致）
//!
//! 不暴露（仍走 Python / daemon RPC）：
//! - `Replicator::replicate` / `recover`（写操作）
//! - `daemon_handle_refresh` / `daemon_handle_connect`（写操作 + session 管理）
//!
//! 设计参考：docs/design/phase1-replicator-snapshot-contract.md §3.1 / §5.1

use crate::daemon::staging_log::StagingLog;
use pyo3::prelude::*;

/// 查询 pending staging log entries 数量
///
/// 与 Python `Replicator.get_pending_count(workspace_id)` 行为一致：
/// - 文件不存在 → 返回 0（`StagingLog::new` 会创建空文件，`read_pending` 返回空 Vec）
/// - log_path 无 pending → 返回 0
/// - 有 N 个 pending → 返回 N
/// - workspace_id=None → 返回所有 pending 总数
/// - workspace_id 指定 → 仅返回该 workspace 的 pending 数量
///
/// # Errors
/// - 文件 IO 错误（极少见，StagingLog::new 内部会创建目录）→ 返回 0
///
/// # 参数
/// - `log_path`: staging log 文件路径（JSON Lines 格式）
/// - `workspace_id`: 可选 workspace_id 过滤（None 时不过滤）
#[pyfunction]
#[pyo3(signature = (log_path, workspace_id=None))]
pub fn replicator_get_pending_count(
    log_path: &str,
    workspace_id: Option<String>,
) -> usize {
    // 与 Rust daemon Replicator::get_pending_count 完全一致：
    // 1. StagingLog::new(log_path) —— 若文件不存在，创建空文件 + next_lsn=1
    // 2. read_pending() —— 读所有 status=="pending" 的 entries
    // 3. 按 workspace_id 过滤
    let log = match StagingLog::new(log_path) {
        Ok(l) => l,
        Err(_) => return 0, // 文件 IO 错误 → 返回 0（与 Python 端 read_pending 异常处理一致）
    };
    let pending = match log.read_pending() {
        Ok(e) => e,
        Err(_) => return 0,
    };
    match workspace_id {
        Some(ws_id) => pending.iter().filter(|e| e.workspace_id == ws_id).count(),
        None => pending.len(),
    }
}

/// 模块注册入口（供 lib.rs 调用）
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(replicator_get_pending_count, m)?)?;
    Ok(())
}

// ============================================
// 测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::daemon::staging_log::StagingEntry;
    use std::fs;
    use std::io::Write;
    use tempfile::tempdir;

    /// 构造一条 staging entry 的 JSON 行
    fn make_entry_json(
        lsn: i64,
        workspace_id: &str,
        file_path: &str,
        status: &str,
    ) -> String {
        format!(
            r#"{{"lsn":{},"timestamp":1.0,"workspace_id":"{}","file_path":"{}","content_hash":"h","language":"rust","parse_delta":{{}},"resolve_delta":{{}},"frontier":{{}},"metrics_update":{{}},"status":"{}","error":null}}"#,
            lsn, workspace_id, file_path, status
        )
    }

    #[test]
    fn test_file_not_exists_returns_zero() {
        // 文件不存在时 replicator_get_pending_count 应返回 0
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("nonexistent.log");
        let path_str = log_path.to_str().unwrap();
        let count = replicator_get_pending_count(path_str, None);
        assert_eq!(count, 0, "文件不存在时应返回 0");
    }

    #[test]
    fn test_empty_file_returns_zero() {
        // 空文件应返回 0
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("empty.log");
        fs::write(&log_path, "").unwrap();
        let path_str = log_path.to_str().unwrap();
        let count = replicator_get_pending_count(path_str, None);
        assert_eq!(count, 0);
    }

    #[test]
    fn test_all_pending_no_workspace_filter() {
        // 3 条 pending + 2 条 applied → 返回 3
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("mixed.log");
        let mut file = fs::File::create(&log_path).unwrap();
        writeln!(file, "{}", make_entry_json(1, "ws1", "a.rs", "pending")).unwrap();
        writeln!(file, "{}", make_entry_json(2, "ws1", "b.rs", "applied")).unwrap();
        writeln!(file, "{}", make_entry_json(3, "ws2", "c.rs", "pending")).unwrap();
        writeln!(file, "{}", make_entry_json(4, "ws2", "d.rs", "pending")).unwrap();
        writeln!(file, "{}", make_entry_json(5, "ws2", "e.rs", "failed")).unwrap();
        drop(file);

        let path_str = log_path.to_str().unwrap();
        let count = replicator_get_pending_count(path_str, None);
        // pending 总数 = 3（lsn 1, 3, 4）
        assert_eq!(count, 3);
    }

    #[test]
    fn test_workspace_filter() {
        // 多个 workspace 的 pending，按 workspace_id 过滤
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("multi_ws.log");
        let mut file = fs::File::create(&log_path).unwrap();
        writeln!(file, "{}", make_entry_json(1, "ws1", "a.rs", "pending")).unwrap();
        writeln!(file, "{}", make_entry_json(2, "ws1", "b.rs", "pending")).unwrap();
        writeln!(file, "{}", make_entry_json(3, "ws1", "c.rs", "applied")).unwrap();
        writeln!(file, "{}", make_entry_json(4, "ws2", "d.rs", "pending")).unwrap();
        writeln!(file, "{}", make_entry_json(5, "ws2", "e.rs", "pending")).unwrap();
        drop(file);

        let path_str = log_path.to_str().unwrap();
        // ws1 有 2 个 pending（lsn 1, 2），ws2 有 2 个 pending（lsn 4, 5）
        assert_eq!(replicator_get_pending_count(path_str, Some("ws1".into())), 2);
        assert_eq!(replicator_get_pending_count(path_str, Some("ws2".into())), 2);
        // ws3 不存在 → 返回 0
        assert_eq!(replicator_get_pending_count(path_str, Some("ws3".into())), 0);
    }

    #[test]
    fn test_malformed_lines_skipped() {
        // 损坏的行应被跳过（与 Python 端行为一致）
        let tmp = tempdir().unwrap();
        let log_path = tmp.path().join("malformed.log");
        let mut file = fs::File::create(&log_path).unwrap();
        writeln!(file, "{}", make_entry_json(1, "ws1", "a.rs", "pending")).unwrap();
        writeln!(file, "this is not json").unwrap();
        writeln!(file, "{}", make_entry_json(2, "ws1", "b.rs", "pending")).unwrap();
        drop(file);

        let path_str = log_path.to_str().unwrap();
        // 应返回 2（损坏行被跳过）
        assert_eq!(replicator_get_pending_count(path_str, None), 2);
    }

    #[test]
    fn test_verify_with_staging_entry_round_trip() {
        // 验证 StagingEntry::from_json_line 能正确解析我们的测试 JSON
        let json = make_entry_json(1, "ws1", "test.rs", "pending");
        let entry = StagingEntry::from_json_line(&json);
        assert!(entry.is_some(), "StagingEntry 应能解析测试 JSON");
        let entry = entry.unwrap();
        assert_eq!(entry.lsn, 1);
        assert_eq!(entry.workspace_id, "ws1");
        assert_eq!(entry.file_path, "test.rs");
        assert_eq!(entry.status, "pending");
    }
}
