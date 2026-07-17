//! StagingLog —— 持久化 staging log（append-only + JSON Lines，崩溃安全）。
//!
//! 对应 Python：`server/staging_log.py`（StagingEntry + StagingLog）
//!
//! 跨平台：纯文件 + serde_json，Windows 可完整验收。
//!
//! 设计：
//! - Append-only：新 entry 追加到文件末尾
//! - JSON Lines：每行一条 entry，崩溃安全（部分写入的行会被跳过）
//! - LSN：单调递增的 log sequence number
//! - Truncate：Replicator 应用后可截断已应用的 entries
//!
//! 不变量：
//! - 崩溃后部分写入的行（JSON 解析失败）会被跳过
//! - next_lsn 在初始化时从现有 log 恢复（取 max_lsn + 1）

use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::Path;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

// ============================================
// StagingEntry
// ============================================

/// 单条 staging 记录（对应 Python StagingEntry dataclass）
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StagingEntry {
    /// log sequence number（单调递增，由 StagingLog::append 分配）
    pub lsn: i64,
    /// 时间戳（epoch seconds）
    pub timestamp: f64,
    /// workspace ID
    pub workspace_id: String,
    /// 变更文件路径
    pub file_path: String,
    /// 文件内容 SHA-256
    pub content_hash: String,
    /// 语言 ID
    pub language: String,
    /// 序列化的 ParseDelta（JSON object）
    #[serde(default)]
    pub parse_delta: Map<String, Value>,
    /// 序列化的 ResolveDelta（JSON object）
    #[serde(default)]
    pub resolve_delta: Map<String, Value>,
    /// 序列化的 AffectedFrontier（JSON object）
    #[serde(default)]
    pub frontier: Map<String, Value>,
    /// 序列化的 LocalMetricsUpdate（JSON object）
    #[serde(default)]
    pub metrics_update: Map<String, Value>,
    /// pending / applied / failed
    #[serde(default = "default_status")]
    pub status: String,
    /// 失败原因（status=failed 时）
    #[serde(default)]
    pub error: Option<String>,
}

fn default_status() -> String {
    "pending".to_string()
}

impl StagingEntry {
    /// 创建新的 StagingEntry（lsn=0，由 log.append 分配实际 lsn）
    pub fn new(
        workspace_id: &str,
        file_path: &str,
        content_hash: &str,
        language: &str,
    ) -> Self {
        Self {
            lsn: 0,
            timestamp: now_ts(),
            workspace_id: workspace_id.to_string(),
            file_path: file_path.to_string(),
            content_hash: content_hash.to_string(),
            language: language.to_string(),
            parse_delta: Map::new(),
            resolve_delta: Map::new(),
            frontier: Map::new(),
            metrics_update: Map::new(),
            status: "pending".to_string(),
            error: None,
        }
    }

    /// 序列化为 JSON line（单行 JSON，ensure_ascii=false）
    pub fn to_json_line(&self) -> String {
        serde_json::to_string(self).unwrap_or_else(|_| "{}".to_string())
    }

    /// 从 JSON line 反序列化（解析失败返回 None，对应 Python 的 except json.JSONDecodeError）
    pub fn from_json_line(line: &str) -> Option<Self> {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            return None;
        }
        serde_json::from_str(trimmed).ok()
    }

    /// 变更摘要
    pub fn summary(&self) -> String {
        format!(
            "StagingEntry(lsn={}, {}, {}, status={})",
            self.lsn, self.file_path, self.language, self.status
        )
    }
}

fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

// ============================================
// StagingLog
// ============================================

/// 持久化 staging log，记录 delta 变更。
///
/// - Append-only：新 entry 追加到文件末尾
/// - JSON Lines：每行一条 entry，崩溃安全（部分写入的行会被跳过）
/// - LSN：单调递增的 log sequence number
/// - Truncate：Replicator 应用后可截断已应用的 entries
///
/// 对应 Python `server/staging_log.py:StagingLog`。
/// 内部用 `std::sync::Mutex` 保护文件句柄 + next_lsn。
pub struct StagingLog {
    log_path: String,
    inner: Mutex<StagingLogInner>,
}

struct StagingLogInner {
    next_lsn: i64,
}

impl StagingLog {
    /// 初始化 staging log。
    ///
    /// 如果 log 文件已存在，从末尾恢复 next_lsn（取 max_lsn + 1）。
    /// 如果 log 文件不存在，next_lsn 从 1 开始。
    pub fn new(log_path: &str) -> std::io::Result<Self> {
        // 确保目录存在
        if let Some(parent) = Path::new(log_path).parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }

        let next_lsn = recover_next_lsn(log_path)?;

        Ok(Self {
            log_path: log_path.to_string(),
            inner: Mutex::new(StagingLogInner { next_lsn }),
        })
    }

    /// 追加一条 staging entry。
    ///
    /// 自动分配 lsn（单调递增），写入文件并 fsync（崩溃安全）。
    /// 返回分配的 LSN。
    pub fn append(&self, entry: &mut StagingEntry) -> std::io::Result<i64> {
        let mut inner = self.inner.lock().unwrap();
        entry.lsn = inner.next_lsn;
        if entry.timestamp == 0.0 {
            entry.timestamp = now_ts();
        }
        inner.next_lsn += 1;

        let line = entry.to_json_line() + "\n";
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.log_path)?;
        file.write_all(line.as_bytes())?;
        file.flush()?;
        // fsync 保证崩溃安全（对应 Python os.fsync）
        use std::io::Write;
        file.sync_all()?;

        Ok(entry.lsn)
    }

    /// 读取从 since_lsn 开始的所有 entries（不包含 since_lsn）。
    ///
    /// 损坏的行（JSON 解析失败）会被跳过。
    pub fn read(&self, since_lsn: i64) -> std::io::Result<Vec<StagingEntry>> {
        let _inner = self.inner.lock().unwrap();
        let mut entries = Vec::new();

        if !Path::new(&self.log_path).exists() {
            return Ok(entries);
        }

        let file = File::open(&self.log_path)?;
        let reader = BufReader::new(file);
        for line in reader.lines() {
            let line = match line {
                Ok(l) => l,
                Err(_) => continue, // IO 错误跳过
            };
            if let Some(entry) = StagingEntry::from_json_line(&line) {
                if entry.lsn > since_lsn {
                    entries.push(entry);
                }
            }
            // 损坏的行跳过（对应 Python 的 except json.JSONDecodeError）
        }

        Ok(entries)
    }

    /// 读取所有 status=pending 的 entries
    pub fn read_pending(&self) -> std::io::Result<Vec<StagingEntry>> {
        let entries = self.read(0)?;
        Ok(entries
            .into_iter()
            .filter(|e| e.status == "pending")
            .collect())
    }

    /// 批量标记多个 LSN 为 applied——单次文件重写。
    ///
    /// 对应 Python StagingLog.mark_applied_batch（修复 T-1783952125417-7a09）。
    /// 减少 mark_applied 逐条重写整个文件的开销。
    pub fn mark_applied_batch(&self, lsns: &[i64]) -> std::io::Result<()> {
        if lsns.is_empty() {
            return Ok(());
        }
        let target_lsns: std::collections::HashSet<i64> = lsns.iter().copied().collect();
        let _inner = self.inner.lock().unwrap();

        let mut entries = Vec::new();
        if Path::new(&self.log_path).exists() {
            let file = File::open(&self.log_path)?;
            let reader = BufReader::new(file);
            for line in reader.lines() {
                let line = match line {
                    Ok(l) => l,
                    Err(_) => continue,
                };
                if let Some(mut entry) = StagingEntry::from_json_line(&line) {
                    if target_lsns.contains(&entry.lsn) {
                        entry.status = "applied".to_string();
                    }
                    entries.push(entry);
                }
            }
        }

        self.rewrite(&entries)
    }

    /// 标记指定 LSN 的 entry 为 failed
    pub fn mark_failed(&self, lsn: i64, error: &str) -> std::io::Result<()> {
        let _inner = self.inner.lock().unwrap();

        let mut entries = Vec::new();
        if Path::new(&self.log_path).exists() {
            let file = File::open(&self.log_path)?;
            let reader = BufReader::new(file);
            for line in reader.lines() {
                let line = match line {
                    Ok(l) => l,
                    Err(_) => continue,
                };
                if let Some(mut entry) = StagingEntry::from_json_line(&line) {
                    if entry.lsn == lsn {
                        entry.status = "failed".to_string();
                        entry.error = Some(error.to_string());
                    }
                    entries.push(entry);
                }
            }
        }

        self.rewrite(&entries)
    }

    /// 截断 log，删除所有 LSN <= up_to_lsn 的 entries。
    pub fn truncate(&self, up_to_lsn: i64) -> std::io::Result<()> {
        let _inner = self.inner.lock().unwrap();

        let mut entries = Vec::new();
        if Path::new(&self.log_path).exists() {
            let file = File::open(&self.log_path)?;
            let reader = BufReader::new(file);
            for line in reader.lines() {
                let line = match line {
                    Ok(l) => l,
                    Err(_) => continue,
                };
                if let Some(entry) = StagingEntry::from_json_line(&line) {
                    if entry.lsn > up_to_lsn {
                        entries.push(entry);
                    }
                }
            }
        }

        self.rewrite(&entries)
    }

    /// 压缩 log，删除所有 status=applied 的 entries。
    ///
    /// 与 truncate 不同，compact_applied 按 status 过滤而非 LSN，
    /// 避免误删其他 workspace 的 pending entries。
    pub fn compact_applied(&self, workspace_id: Option<&str>) -> std::io::Result<()> {
        let _inner = self.inner.lock().unwrap();

        let mut entries = Vec::new();
        if Path::new(&self.log_path).exists() {
            let file = File::open(&self.log_path)?;
            let reader = BufReader::new(file);
            for line in reader.lines() {
                let line = match line {
                    Ok(l) => l,
                    Err(_) => continue,
                };
                if let Some(entry) = StagingEntry::from_json_line(&line) {
                    // 保留非 applied 的，或不是目标 workspace 的 applied
                    if entry.status == "applied" {
                        match workspace_id {
                            Some(ws_id) if entry.workspace_id == ws_id => continue,
                            None => continue,
                            _ => {}
                        }
                    }
                    entries.push(entry);
                }
            }
        }

        self.rewrite(&entries)
    }

    /// 重写整个 log 文件（原子替换：tmp + fsync + rename）
    fn rewrite(&self, entries: &[StagingEntry]) -> std::io::Result<()> {
        let tmp_path = format!("{}.tmp", self.log_path);
        {
            let file = OpenOptions::new()
                .create(true)
                .write(true)
                .truncate(true)
                .open(&tmp_path)?;
            let mut writer = BufWriter::new(file);
            for entry in entries {
                writer.write_all(entry.to_json_line().as_bytes())?;
                writer.write_all(b"\n")?;
            }
            writer.flush()?;
            let file = writer.into_inner()?;
            file.sync_all()?;
        }

        // 原子替换（对应 Python os.replace）
        std::fs::rename(&tmp_path, &self.log_path)?;
        Ok(())
    }

    /// 返回 log 统计信息
    pub fn stats(&self) -> std::io::Result<StagingLogStats> {
        let entries = self.read(0)?;
        let pending = entries.iter().filter(|e| e.status == "pending").count();
        let applied = entries.iter().filter(|e| e.status == "applied").count();
        let failed = entries.iter().filter(|e| e.status == "failed").count();

        let next_lsn = self.inner.lock().unwrap().next_lsn;

        Ok(StagingLogStats {
            total_entries: entries.len(),
            pending,
            applied,
            failed,
            next_lsn,
            log_path: self.log_path.clone(),
        })
    }

    /// 获取下一个 LSN（用于测试）
    pub fn next_lsn(&self) -> i64 {
        self.inner.lock().unwrap().next_lsn
    }

    /// log 文件路径
    pub fn log_path(&self) -> &str {
        &self.log_path
    }
}

/// 从现有 log 文件恢复 next_lsn（取 max_lsn + 1）
fn recover_next_lsn(log_path: &str) -> std::io::Result<i64> {
    if !Path::new(log_path).exists() {
        return Ok(1);
    }

    let file = File::open(log_path)?;
    let reader = BufReader::new(file);
    let mut max_lsn: i64 = 0;
    for line in reader.lines() {
        let line = match line {
            Ok(l) => l,
            Err(_) => continue,
        };
        if let Some(entry) = StagingEntry::from_json_line(&line) {
            if entry.lsn > max_lsn {
                max_lsn = entry.lsn;
            }
        }
    }

    Ok(max_lsn + 1)
}

/// StagingLog 统计信息
#[derive(Debug, Clone)]
pub struct StagingLogStats {
    pub total_entries: usize,
    pub pending: usize,
    pub applied: usize,
    pub failed: usize,
    pub next_lsn: i64,
    pub log_path: String,
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;

    fn make_log() -> (tempfile::TempDir, StagingLog) {
        let tmp = tempfile::tempdir().unwrap();
        let log_path = tmp.path().join("staging.log");
        let log_path_str = log_path.to_str().unwrap().to_string();
        let log = StagingLog::new(&log_path_str).unwrap();
        (tmp, log)
    }

    fn make_entry(workspace_id: &str, file_path: &str) -> StagingEntry {
        StagingEntry::new(workspace_id, file_path, "hash123", "rust")
    }

    // ---- StagingEntry 测试 ----

    #[test]
    fn test_entry_new_initializes_defaults() {
        let entry = StagingEntry::new("ws1", "src/main.rs", "hash123", "rust");
        assert_eq!(entry.lsn, 0);
        assert_eq!(entry.workspace_id, "ws1");
        assert_eq!(entry.file_path, "src/main.rs");
        assert_eq!(entry.content_hash, "hash123");
        assert_eq!(entry.language, "rust");
        assert_eq!(entry.status, "pending");
        assert!(entry.error.is_none());
        assert!(entry.timestamp > 0.0);
    }

    #[test]
    fn test_entry_json_roundtrip() {
        let mut entry = StagingEntry::new("ws1", "src/main.rs", "hash123", "rust");
        entry.lsn = 42;
        entry.status = "applied".to_string();
        entry.error = Some("test error".to_string());

        let json = entry.to_json_line();
        let parsed = StagingEntry::from_json_line(&json).unwrap();

        assert_eq!(parsed.lsn, 42);
        assert_eq!(parsed.workspace_id, "ws1");
        assert_eq!(parsed.file_path, "src/main.rs");
        assert_eq!(parsed.status, "applied");
        assert_eq!(parsed.error, Some("test error".to_string()));
    }

    #[test]
    fn test_entry_from_json_line_empty_returns_none() {
        assert_eq!(StagingEntry::from_json_line(""), None);
        assert_eq!(StagingEntry::from_json_line("   "), None);
    }

    #[test]
    fn test_entry_from_json_line_invalid_json_returns_none() {
        assert_eq!(StagingEntry::from_json_line("not json"), None);
        assert_eq!(StagingEntry::from_json_line("{invalid"), None);
    }

    #[test]
    fn test_entry_from_json_line_partial_fields() {
        // 最小有效 JSON：只有必填字段
        let json = r#"{"lsn":1,"timestamp":1.0,"workspace_id":"ws","file_path":"f","content_hash":"h","language":"l"}"#;
        let entry = StagingEntry::from_json_line(json).unwrap();
        assert_eq!(entry.lsn, 1);
        assert_eq!(entry.workspace_id, "ws");
        // 缺省字段应使用默认值
        assert_eq!(entry.status, "pending");
        assert!(entry.error.is_none());
    }

    #[test]
    fn test_entry_summary() {
        let entry = StagingEntry::new("ws1", "src/main.rs", "hash123", "rust");
        let summary = entry.summary();
        assert!(summary.contains("src/main.rs"));
        assert!(summary.contains("rust"));
        assert!(summary.contains("pending"));
    }

    // ---- StagingLog 基础测试 ----

    #[test]
    fn test_log_new_creates_parent_dir() {
        let tmp = tempfile::tempdir().unwrap();
        let log_path = tmp.path().join("nested").join("staging.log");
        let log = StagingLog::new(log_path.to_str().unwrap()).unwrap();
        assert_eq!(log.next_lsn(), 1);
        assert!(log_path.exists() || log_path.parent().unwrap().exists());
    }

    #[test]
    fn test_log_append_assigns_lsn_sequentially() {
        let (_tmp, log) = make_log();
        assert_eq!(log.next_lsn(), 1);

        let mut e1 = make_entry("ws1", "file1.rs");
        let lsn1 = log.append(&mut e1).unwrap();
        assert_eq!(lsn1, 1);
        assert_eq!(e1.lsn, 1);
        assert_eq!(log.next_lsn(), 2);

        let mut e2 = make_entry("ws1", "file2.rs");
        let lsn2 = log.append(&mut e2).unwrap();
        assert_eq!(lsn2, 2);
        assert_eq!(e2.lsn, 2);
        assert_eq!(log.next_lsn(), 3);
    }

    #[test]
    fn test_log_read_returns_entries_in_lsn_order() {
        let (_tmp, log) = make_log();

        let mut e1 = make_entry("ws1", "file1.rs");
        let mut e2 = make_entry("ws1", "file2.rs");
        let mut e3 = make_entry("ws1", "file3.rs");
        log.append(&mut e1).unwrap();
        log.append(&mut e2).unwrap();
        log.append(&mut e3).unwrap();

        let entries = log.read(0).unwrap();
        assert_eq!(entries.len(), 3);
        assert_eq!(entries[0].lsn, 1);
        assert_eq!(entries[1].lsn, 2);
        assert_eq!(entries[2].lsn, 3);
    }

    #[test]
    fn test_log_read_with_since_lsn_filter() {
        let (_tmp, log) = make_log();

        for i in 0..5 {
            let mut e = make_entry("ws1", &format!("file{}.rs", i));
            log.append(&mut e).unwrap();
        }

        let entries = log.read(2).unwrap();
        assert_eq!(entries.len(), 3); // lsn 3, 4, 5
        assert_eq!(entries[0].lsn, 3);
        assert_eq!(entries[1].lsn, 4);
        assert_eq!(entries[2].lsn, 5);
    }

    #[test]
    fn test_log_read_pending_filters_by_status() {
        let (_tmp, log) = make_log();

        let mut e1 = make_entry("ws1", "file1.rs");
        let mut e2 = make_entry("ws1", "file2.rs");
        log.append(&mut e1).unwrap();
        log.append(&mut e2).unwrap();

        // mark e1 as applied
        log.mark_applied_batch(&[1]).unwrap();

        let pending = log.read_pending().unwrap();
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].lsn, 2);
        assert_eq!(pending[0].status, "pending");
    }

    #[test]
    fn test_log_read_empty_when_no_file() {
        let tmp = tempfile::tempdir().unwrap();
        let log_path = tmp.path().join("nonexistent.log");
        let log = StagingLog::new(log_path.to_str().unwrap()).unwrap();

        let entries = log.read(0).unwrap();
        assert!(entries.is_empty());
    }

    // ---- mark_applied_batch 测试 ----

    #[test]
    fn test_mark_applied_batch_updates_status() {
        let (_tmp, log) = make_log();

        for i in 0..5 {
            let mut e = make_entry("ws1", &format!("file{}.rs", i));
            log.append(&mut e).unwrap();
        }

        log.mark_applied_batch(&[1, 3, 5]).unwrap();

        let entries = log.read(0).unwrap();
        assert_eq!(entries.len(), 5);
        assert_eq!(entries[0].status, "applied"); // lsn=1
        assert_eq!(entries[1].status, "pending"); // lsn=2
        assert_eq!(entries[2].status, "applied"); // lsn=3
        assert_eq!(entries[3].status, "pending"); // lsn=4
        assert_eq!(entries[4].status, "applied"); // lsn=5
    }

    #[test]
    fn test_mark_applied_batch_empty_lsns_noop() {
        let (_tmp, log) = make_log();

        let mut e = make_entry("ws1", "file1.rs");
        log.append(&mut e).unwrap();

        // 空 lsns 应该是 no-op
        log.mark_applied_batch(&[]).unwrap();

        let entries = log.read(0).unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].status, "pending");
    }

    // ---- mark_failed 测试 ----

    #[test]
    fn test_mark_failed_sets_error() {
        let (_tmp, log) = make_log();

        let mut e = make_entry("ws1", "file1.rs");
        log.append(&mut e).unwrap();

        log.mark_failed(1, "parse failed: syntax error").unwrap();

        let entries = log.read(0).unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].status, "failed");
        assert_eq!(entries[0].error, Some("parse failed: syntax error".to_string()));
    }

    // ---- truncate 测试 ----

    #[test]
    fn test_truncate_removes_old_entries() {
        let (_tmp, log) = make_log();

        for i in 0..5 {
            let mut e = make_entry("ws1", &format!("file{}.rs", i));
            log.append(&mut e).unwrap();
        }

        log.truncate(3).unwrap();

        let entries = log.read(0).unwrap();
        assert_eq!(entries.len(), 2); // lsn 4, 5
        assert_eq!(entries[0].lsn, 4);
        assert_eq!(entries[1].lsn, 5);
    }

    // ---- compact_applied 测试 ----

    #[test]
    fn test_compact_applied_removes_all_applied() {
        let (_tmp, log) = make_log();

        for i in 0..5 {
            let mut e = make_entry("ws1", &format!("file{}.rs", i));
            log.append(&mut e).unwrap();
        }
        log.mark_applied_batch(&[1, 3]).unwrap();

        log.compact_applied(None).unwrap();

        let entries = log.read(0).unwrap();
        assert_eq!(entries.len(), 3); // lsn 2, 4, 5
        assert_eq!(entries[0].lsn, 2);
        assert_eq!(entries[1].lsn, 4);
    }

    #[test]
    fn test_compact_applied_filters_by_workspace() {
        let (_tmp, log) = make_log();

        // ws1 的 entries
        let mut e1 = make_entry("ws1", "file1.rs");
        let mut e2 = make_entry("ws1", "file2.rs");
        log.append(&mut e1).unwrap();
        log.append(&mut e2).unwrap();
        // ws2 的 entries
        let mut e3 = make_entry("ws2", "file3.rs");
        let mut e4 = make_entry("ws2", "file4.rs");
        log.append(&mut e3).unwrap();
        log.append(&mut e4).unwrap();

        // mark ws1 的 entry 为 applied
        log.mark_applied_batch(&[1]).unwrap();
        // mark ws2 的 entry 为 applied
        log.mark_applied_batch(&[3]).unwrap();

        // 只 compact ws1 的 applied
        log.compact_applied(Some("ws1")).unwrap();

        let entries = log.read(0).unwrap();
        assert_eq!(entries.len(), 3);
        // ws1 的 applied 应该被删除（lsn=1）
        // ws2 的 applied 应该保留（lsn=3）
        let lsns: Vec<i64> = entries.iter().map(|e| e.lsn).collect();
        assert!(lsns.contains(&2));
        assert!(lsns.contains(&3));
        assert!(lsns.contains(&4));
        assert!(!lsns.contains(&1));
    }

    // ---- next_lsn 恢复测试 ----

    #[test]
    fn test_next_lsn_recovers_from_existing_log() {
        let tmp = tempfile::tempdir().unwrap();
        let log_path = tmp.path().join("staging.log");
        let log_path_str = log_path.to_str().unwrap().to_string();

        // 第一次打开，写入 3 条
        {
            let log = StagingLog::new(&log_path_str).unwrap();
            assert_eq!(log.next_lsn(), 1);
            for i in 0..3 {
                let mut e = make_entry("ws1", &format!("file{}.rs", i));
                log.append(&mut e).unwrap();
            }
        }

        // 第二次打开，next_lsn 应该恢复为 4
        {
            let log = StagingLog::new(&log_path_str).unwrap();
            assert_eq!(log.next_lsn(), 4);
        }
    }

    #[test]
    fn test_next_lsn_starts_from_1_for_empty_log() {
        let tmp = tempfile::tempdir().unwrap();
        let log_path = tmp.path().join("staging.log");
        let log = StagingLog::new(log_path.to_str().unwrap()).unwrap();
        assert_eq!(log.next_lsn(), 1);
    }

    // ---- 崩溃安全测试 ----

    #[test]
    fn test_corrupted_lines_are_skipped() {
        let tmp = tempfile::tempdir().unwrap();
        let log_path = tmp.path().join("staging.log");
        let log_path_str = log_path.to_str().unwrap().to_string();

        // 写入：有效 entry + 损坏行 + 有效 entry
        {
            let log = StagingLog::new(&log_path_str).unwrap();
            let mut e1 = make_entry("ws1", "file1.rs");
            log.append(&mut e1).unwrap();
        }
        // 手动追加损坏行 + 有效 entry
        {
            use std::io::Write;
            let mut f = OpenOptions::new()
                .append(true)
                .open(&log_path_str)
                .unwrap();
            writeln!(f, "this is corrupted json line").unwrap();
            writeln!(f, "{{invalid").unwrap();
            let valid_entry = StagingEntry::new("ws1", "file2.rs", "hash123", "rust");
            let mut valid_entry = valid_entry;
            valid_entry.lsn = 100; // 手动设置 lsn
            writeln!(f, "{}", valid_entry.to_json_line()).unwrap();
        }

        // 重新打开 log，损坏行应该被跳过
        let log = StagingLog::new(&log_path_str).unwrap();
        let entries = log.read(0).unwrap();
        // 应该有 2 条有效 entries（lsn=1 + lsn=100），损坏行被跳过
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].lsn, 1);
        assert_eq!(entries[1].lsn, 100);
        // next_lsn 应该恢复为 101（max_lsn + 1）
        assert_eq!(log.next_lsn(), 101);
    }

    // ---- stats 测试 ----

    #[test]
    fn test_stats_returns_correct_counts() {
        let (_tmp, log) = make_log();

        // 3 pending + 2 applied + 1 failed
        for i in 0..6 {
            let mut e = make_entry("ws1", &format!("file{}.rs", i));
            log.append(&mut e).unwrap();
        }
        log.mark_applied_batch(&[1, 2]).unwrap();
        log.mark_failed(3, "test error").unwrap();

        let stats = log.stats().unwrap();
        assert_eq!(stats.total_entries, 6);
        assert_eq!(stats.pending, 3); // lsn 4, 5, 6
        assert_eq!(stats.applied, 2); // lsn 1, 2
        assert_eq!(stats.failed, 1); // lsn 3
        assert_eq!(stats.next_lsn, 7);
    }

    // ---- 跨 workspace 隔离测试 ----

    #[test]
    fn test_read_pending_filters_by_workspace_in_replicator() {
        let (_tmp, log) = make_log();

        // ws1 的 entries
        let mut e1 = make_entry("ws1", "file1.rs");
        let mut e2 = make_entry("ws1", "file2.rs");
        log.append(&mut e1).unwrap();
        log.append(&mut e2).unwrap();
        // ws2 的 entries
        let mut e3 = make_entry("ws2", "file3.rs");
        let mut e4 = make_entry("ws2", "file4.rs");
        log.append(&mut e3).unwrap();
        log.append(&mut e4).unwrap();

        // read_pending 返回所有，replicator 自行过滤
        let all_pending = log.read_pending().unwrap();
        assert_eq!(all_pending.len(), 4);

        // 模拟 replicator 过滤
        let ws1_pending: Vec<_> = all_pending
            .iter()
            .filter(|e| e.workspace_id == "ws1")
            .collect();
        assert_eq!(ws1_pending.len(), 2);
    }

    // ---- JSON Lines 格式验证 ----

    #[test]
    fn test_each_entry_is_one_line() {
        let (_tmp, log) = make_log();

        // entry 的 content 中包含换行符，JSON 序列化后应该是单行
        let mut e = StagingEntry::new("ws1", "file1.rs", "hash123", "rust");
        e.parse_delta.insert(
            "multiline".to_string(),
            Value::String("line1\nline2\nline3".to_string()),
        );
        log.append(&mut e).unwrap();

        // 读取文件，确认每条 entry 占一行
        let content = std::fs::read_to_string(log.log_path()).unwrap();
        let lines: Vec<&str> = content.lines().collect();
        assert_eq!(lines.len(), 1, "应该只有一行（JSON 序列化转义换行符）");
    }
}
