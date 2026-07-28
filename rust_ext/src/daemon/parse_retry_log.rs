//! P1-F Step 3: Parse 失败 durable log + daemon 重启重放
//!
//! 设计文档：docs/design/rust-only-parser-cutover-plan.md §8 Phase 4
//!
//! 核心职责：
//! - **durable log**：记录 failed / partial / unsupported / stale 状态的 parse 失败
//! - **重启恢复**：daemon 重启后读取 pending entries
//! - **只重放可重试**：仅 `allows_retry=true`（即 `failed` 状态）的 generation 才重放
//!
//! 设计 §8 Phase 4：
//! - 失败 generation 不替换上一代 snapshot
//! - durable log 记录 failed/partial/retry 状态
//! - daemon 重启后只重放可重试 generation
//! - stale session/generation CAS 继续拒绝旧结果
//!
//! 与 `staging_log.rs` 的区别：
//! - `staging_log` 记录成功 parse 后的 staging entries（pending/applied/failed）
//! - `parse_retry_log` 记录 parse 阶段的失败，用于 daemon 重启后重试
//! - 两者独立，互不干扰

use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

// ============================================
// ParseFailureEntry
// ============================================

/// Parse 失败日志条目
///
/// 记录一次 parse 失败的完整上下文，供 daemon 重启后重放。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ParseFailureEntry {
    /// log sequence number（单调递增，由 ParseRetryLog::append 分配）
    pub lsn: i64,
    /// 时间戳（epoch seconds）
    #[serde(default)]
    pub timestamp: f64,
    /// workspace ID
    pub workspace_id: String,
    /// 文件相对路径
    pub rel_path: String,
    /// 文件绝对路径（用于重试时读取）
    #[serde(default)]
    pub abs_path: String,
    /// generation 标识（session_epoch:monotonic_seq）
    #[serde(default)]
    pub generation: String,
    /// 语言 ID
    #[serde(default)]
    pub language: String,
    /// parse 状态（"failed" / "partial" / "unsupported" / "stale"）
    pub parse_status: String,
    /// cas_state（来自 `_daemon_parse_and_publish`）
    #[serde(default)]
    pub cas_state: String,
    /// 失败原因
    #[serde(default)]
    pub reason: String,
    /// 是否允许重试（仅 `failed` 状态允许，设计 §5.3）
    #[serde(default)]
    pub allows_retry: bool,
    /// 重试次数
    #[serde(default)]
    pub retry_count: u32,
    /// 最后重试时间（epoch seconds）
    #[serde(default)]
    pub last_retry_at: Option<f64>,
    /// 条目状态：pending（待重试）/ applied（已重试成功）/ exhausted（重试次数耗尽）/ permanent（不可重试）
    #[serde(default = "default_entry_status")]
    pub status: String,
}

fn default_entry_status() -> String {
    "pending".to_string()
}

impl ParseFailureEntry {
    /// 创建新的 ParseFailureEntry（lsn=0，由 log.append 分配实际 lsn）
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        workspace_id: &str,
        rel_path: &str,
        abs_path: &str,
        generation: &str,
        language: &str,
        parse_status: &str,
        cas_state: &str,
        reason: &str,
        allows_retry: bool,
    ) -> Self {
        Self {
            lsn: 0,
            timestamp: 0.0, // 由 append 填充
            workspace_id: workspace_id.to_string(),
            rel_path: rel_path.to_string(),
            abs_path: abs_path.to_string(),
            generation: generation.to_string(),
            language: language.to_string(),
            parse_status: parse_status.to_string(),
            cas_state: cas_state.to_string(),
            reason: reason.to_string(),
            allows_retry,
            retry_count: 0,
            last_retry_at: None,
            status: if allows_retry {
                "pending".to_string()
            } else {
                "permanent".to_string()
            },
        }
    }

    /// 序列化为 JSON line
    pub fn to_json_line(&self) -> String {
        serde_json::to_string(self).unwrap_or_else(|_| "{}".to_string())
    }

    /// 从 JSON line 反序列化（解析失败返回 None）
    pub fn from_json_line(line: &str) -> Option<Self> {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            return None;
        }
        serde_json::from_str(trimmed).ok()
    }

    /// 是否仍可重试（pending + allows_retry + retry_count < max）
    pub fn is_retryable(&self, max_retry: u32) -> bool {
        self.allows_retry && self.status == "pending" && self.retry_count < max_retry
    }
}

fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

// ============================================
// ParseRetryLog
// ============================================

/// 持久化 parse 失败 log，记录可重试的 generation。
///
/// - Append-only：新 entry 追加到文件末尾
/// - JSON Lines：每行一条 entry，崩溃安全（部分写入的行会被跳过）
/// - LSN：单调递增的 log sequence number
/// - Rewrite：状态变更（applied/exhausted/increment_retry）需要重写整个文件
///
/// 对应设计 §8 Phase 4 durable log。
pub struct ParseRetryLog {
    log_path: String,
    inner: Mutex<ParseRetryLogInner>,
}

struct ParseRetryLogInner {
    next_lsn: i64,
}

impl ParseRetryLog {
    /// 初始化 parse retry log。
    ///
    /// 如果 log 文件已存在，从末尾恢复 next_lsn（取 max_lsn + 1）。
    pub fn new(log_path: &str) -> std::io::Result<Self> {
        if let Some(parent) = Path::new(log_path).parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        let next_lsn = recover_next_lsn(log_path)?;
        Ok(Self {
            log_path: log_path.to_string(),
            inner: Mutex::new(ParseRetryLogInner { next_lsn }),
        })
    }

    /// 追加一条 parse 失败 entry。
    ///
    /// 自动分配 lsn（单调递增），写入文件并 fsync（崩溃安全）。
    pub fn append(&self, entry: &mut ParseFailureEntry) -> std::io::Result<i64> {
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
        file.sync_all()?; // 崩溃安全

        Ok(entry.lsn)
    }

    /// 读取所有 entries（不包含 since_lsn）
    pub fn read(&self, since_lsn: i64) -> std::io::Result<Vec<ParseFailureEntry>> {
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
                Err(_) => continue,
            };
            if let Some(entry) = ParseFailureEntry::from_json_line(&line) {
                if entry.lsn > since_lsn {
                    entries.push(entry);
                }
            }
        }
        Ok(entries)
    }

    /// 读取所有 pending entries（供 daemon 重启重放）
    pub fn read_pending(&self) -> std::io::Result<Vec<ParseFailureEntry>> {
        let entries = self.read(0)?;
        Ok(entries
            .into_iter()
            .filter(|e| e.status == "pending")
            .collect())
    }

    /// 读取所有可重试 entries（pending + allows_retry + retry_count < max_retry）
    ///
    /// 设计 §8 Phase 4：daemon 重启后只重放可重试 generation
    pub fn read_retryable(&self, max_retry: u32) -> std::io::Result<Vec<ParseFailureEntry>> {
        let entries = self.read_pending()?;
        Ok(entries
            .into_iter()
            .filter(|e| e.is_retryable(max_retry))
            .collect())
    }

    /// 标记 entry 为 applied（重试成功）
    pub fn mark_applied(&self, lsn: i64) -> std::io::Result<()> {
        self.update_entry_status(lsn, "applied")
    }

    /// 标记 entry 为 exhausted（重试次数耗尽）
    pub fn mark_exhausted(&self, lsn: i64) -> std::io::Result<()> {
        self.update_entry_status(lsn, "exhausted")
    }

    /// 增加 retry_count 并更新 last_retry_at（重试前调用）
    pub fn increment_retry(&self, lsn: i64) -> std::io::Result<()> {
        let _inner = self.inner.lock().unwrap();
        let mut entries = self.read_all_entries()?;
        let now = now_ts();
        for entry in entries.iter_mut() {
            if entry.lsn == lsn {
                entry.retry_count += 1;
                entry.last_retry_at = Some(now);
                break;
            }
        }
        self.rewrite(&entries)
    }

    /// 压缩 log，删除所有 status=applied/exhausted/permanent 的 entries。
    ///
    /// 只保留 status=pending 的 entries，减少 log 文件大小。
    pub fn compact(&self) -> std::io::Result<usize> {
        let _inner = self.inner.lock().unwrap();
        let entries = self.read_all_entries()?;
        let total = entries.len();
        let kept: Vec<ParseFailureEntry> = entries
            .into_iter()
            .filter(|e| e.status == "pending")
            .collect();
        let removed = total - kept.len();
        self.rewrite(&kept)?;
        Ok(removed)
    }

    /// 获取下一个 LSN（用于测试 / PyO3 暴露）
    pub fn next_lsn(&self) -> i64 {
        self.inner.lock().unwrap().next_lsn
    }

    /// log 文件路径
    pub fn log_path(&self) -> &str {
        &self.log_path
    }

    /// 更新指定 lsn 的 entry 状态
    fn update_entry_status(&self, lsn: i64, new_status: &str) -> std::io::Result<()> {
        let _inner = self.inner.lock().unwrap();
        let mut entries = self.read_all_entries()?;
        for entry in entries.iter_mut() {
            if entry.lsn == lsn {
                entry.status = new_status.to_string();
                break;
            }
        }
        self.rewrite(&entries)
    }

    /// 读取所有 entries（内部辅助函数）
    fn read_all_entries(&self) -> std::io::Result<Vec<ParseFailureEntry>> {
        let mut entries = Vec::new();
        if !Path::new(&self.log_path).exists() {
            return Ok(entries);
        }
        let file = File::open(&self.log_path)?;
        let reader = BufReader::new(file);
        for line in reader.lines() {
            let line = match line {
                Ok(l) => l,
                Err(_) => continue,
            };
            if let Some(entry) = ParseFailureEntry::from_json_line(&line) {
                entries.push(entry);
            }
        }
        Ok(entries)
    }

    /// 重写整个 log 文件
    fn rewrite(&self, entries: &[ParseFailureEntry]) -> std::io::Result<()> {
        let mut file = OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .open(&self.log_path)?;
        for entry in entries {
            let line = entry.to_json_line() + "\n";
            file.write_all(line.as_bytes())?;
        }
        file.flush()?;
        file.sync_all()?;
        Ok(())
    }
}

/// 从现有 log 恢复 next_lsn
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
        if let Some(entry) = ParseFailureEntry::from_json_line(&line) {
            if entry.lsn > max_lsn {
                max_lsn = entry.lsn;
            }
        }
    }
    Ok(max_lsn + 1)
}

// ============================================
// ReplayConfig + daemon 重启重放
// ============================================

/// daemon 重启重放策略
#[derive(Debug, Clone)]
pub struct ReplayConfig {
    /// 最大重试次数（超过则标记 exhausted）
    pub max_retry: u32,
    /// 重试间隔（秒，0 = 立即重试）
    pub retry_interval: f64,
}

impl Default for ReplayConfig {
    fn default() -> Self {
        Self {
            max_retry: 3,
            retry_interval: 0.0,
        }
    }
}

/// daemon 重启后重放可重试 generation
///
/// 设计 §8 Phase 4：daemon 重启后只重放可重试 generation
///
/// 返回待重试的 entries 列表（调用方负责实际重试，重试成功后调用 `mark_applied`，
/// 重试次数耗尽调用 `mark_exhausted`）。
///
/// # 参数
/// - `log`：parse retry log
/// - `config`：重放策略
pub fn replay_pending(log: &ParseRetryLog, config: &ReplayConfig) -> std::io::Result<Vec<ParseFailureEntry>> {
    log.read_retryable(config.max_retry)
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::env;

    fn temp_log_path(name: &str) -> String {
        let mut p = env::temp_dir();
        p.push(format!("callwarden_test_parse_retry_{}.log", name));
        let s = p.to_string_lossy().into_owned();
        // 测试前删除旧文件
        let _ = std::fs::remove_file(&s);
        s
    }

    #[test]
    fn test_append_and_read() {
        let path = temp_log_path("append_read");
        let log = ParseRetryLog::new(&path).unwrap();
        let mut e1 = ParseFailureEntry::new(
            "ws1", "src/main.rs", "/repo/src/main.rs", "1:5", "rust",
            "failed", "parse_failed", "test error", true,
        );
        let lsn1 = log.append(&mut e1).unwrap();
        assert_eq!(lsn1, 1);
        assert!(e1.timestamp > 0.0);

        let mut e2 = ParseFailureEntry::new(
            "ws1", "src/lib.rs", "/repo/src/lib.rs", "1:6", "rust",
            "failed", "parse_failed", "test error 2", true,
        );
        let lsn2 = log.append(&mut e2).unwrap();
        assert_eq!(lsn2, 2);

        let entries = log.read(0).unwrap();
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].rel_path, "src/main.rs");
        assert_eq!(entries[1].rel_path, "src/lib.rs");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn test_read_pending() {
        let path = temp_log_log("pending");
        let log = ParseRetryLog::new(&path).unwrap();
        let mut e1 = ParseFailureEntry::new(
            "ws1", "a.rs", "/a.rs", "1:1", "rust",
            "failed", "parse_failed", "err", true,
        );
        log.append(&mut e1).unwrap();

        let mut e2 = ParseFailureEntry::new(
            "ws1", "b.rs", "/b.rs", "1:2", "rust",
            "unsupported", "unsupported_language", "no lang", false,
        );
        log.append(&mut e2).unwrap();

        let pending = log.read_pending().unwrap();
        // e1 是 pending（allows_retry=true），e2 是 permanent（allows_retry=false）
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].rel_path, "a.rs");

        let _ = std::fs::remove_file(&path);
    }

    fn temp_log_log(name: &str) -> String {
        temp_log_path(name)
    }

    #[test]
    fn test_read_retryable() {
        let path = temp_log_path("retryable");
        let log = ParseRetryLog::new(&path).unwrap();

        // 3 个 pending + allows_retry entries
        for i in 0..3 {
            let mut e = ParseFailureEntry::new(
                "ws1", &format!("f{}.rs", i), &format!("/f{}.rs", i),
                &format!("1:{}", i), "rust",
                "failed", "parse_failed", "err", true,
            );
            log.append(&mut e).unwrap();
        }

        let retryable = log.read_retryable(3).unwrap();
        assert_eq!(retryable.len(), 3);

        // 增加 retry_count 到 3，应该不再可重试
        log.increment_retry(1).unwrap();
        log.increment_retry(1).unwrap();
        log.increment_retry(1).unwrap();
        let retryable = log.read_retryable(3).unwrap();
        assert_eq!(retryable.len(), 2, "retry_count=3 应该不再可重试");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn test_mark_applied() {
        let path = temp_log_path("applied");
        let log = ParseRetryLog::new(&path).unwrap();
        let mut e = ParseFailureEntry::new(
            "ws1", "a.rs", "/a.rs", "1:1", "rust",
            "failed", "parse_failed", "err", true,
        );
        let lsn = log.append(&mut e).unwrap();

        log.mark_applied(lsn).unwrap();
        let pending = log.read_pending().unwrap();
        assert_eq!(pending.len(), 0, "applied entry 不应在 pending 中");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn test_mark_exhausted() {
        let path = temp_log_path("exhausted");
        let log = ParseRetryLog::new(&path).unwrap();
        let mut e = ParseFailureEntry::new(
            "ws1", "a.rs", "/a.rs", "1:1", "rust",
            "failed", "parse_failed", "err", true,
        );
        let lsn = log.append(&mut e).unwrap();

        log.mark_exhausted(lsn).unwrap();
        let pending = log.read_pending().unwrap();
        assert_eq!(pending.len(), 0, "exhausted entry 不应在 pending 中");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn test_increment_retry() {
        let path = temp_log_path("increment");
        let log = ParseRetryLog::new(&path).unwrap();
        let mut e = ParseFailureEntry::new(
            "ws1", "a.rs", "/a.rs", "1:1", "rust",
            "failed", "parse_failed", "err", true,
        );
        let lsn = log.append(&mut e).unwrap();
        assert_eq!(e.retry_count, 0);

        log.increment_retry(lsn).unwrap();
        let entries = log.read(0).unwrap();
        assert_eq!(entries[0].retry_count, 1);
        assert!(entries[0].last_retry_at.is_some());

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn test_compact() {
        let path = temp_log_path("compact");
        let log = ParseRetryLog::new(&path).unwrap();

        // 添加 3 个 entries
        let mut e1 = ParseFailureEntry::new(
            "ws1", "a.rs", "/a.rs", "1:1", "rust",
            "failed", "parse_failed", "err", true,
        );
        let lsn1 = log.append(&mut e1).unwrap();

        let mut e2 = ParseFailureEntry::new(
            "ws1", "b.rs", "/b.rs", "1:2", "rust",
            "failed", "parse_failed", "err", true,
        );
        let lsn2 = log.append(&mut e2).unwrap();

        let mut e3 = ParseFailureEntry::new(
            "ws1", "c.rs", "/c.rs", "1:3", "rust",
            "failed", "parse_failed", "err", true,
        );
        let _lsn3 = log.append(&mut e3).unwrap();

        // 标记 e1 和 e2 为 applied
        log.mark_applied(lsn1).unwrap();
        log.mark_applied(lsn2).unwrap();

        // compact 应该删除 2 个 applied，保留 1 个 pending
        let removed = log.compact().unwrap();
        assert_eq!(removed, 2);
        let remaining = log.read(0).unwrap();
        assert_eq!(remaining.len(), 1);
        assert_eq!(remaining[0].rel_path, "c.rs");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn test_recover_next_lsn() {
        let path = temp_log_path("recover");
        let log1 = ParseRetryLog::new(&path).unwrap();
        let mut e = ParseFailureEntry::new(
            "ws1", "a.rs", "/a.rs", "1:1", "rust",
            "failed", "parse_failed", "err", true,
        );
        let lsn = log1.append(&mut e).unwrap();
        assert_eq!(lsn, 1);

        // 重新打开，next_lsn 应该恢复为 2
        let log2 = ParseRetryLog::new(&path).unwrap();
        let mut e2 = ParseFailureEntry::new(
            "ws1", "b.rs", "/b.rs", "1:2", "rust",
            "failed", "parse_failed", "err", true,
        );
        let lsn2 = log2.append(&mut e2).unwrap();
        assert_eq!(lsn2, 2);

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn test_replay_pending() {
        let path = temp_log_path("replay");
        let log = ParseRetryLog::new(&path).unwrap();

        // 添加 1 个可重试 + 1 个不可重试
        let mut e1 = ParseFailureEntry::new(
            "ws1", "a.rs", "/a.rs", "1:1", "rust",
            "failed", "parse_failed", "err", true,
        );
        log.append(&mut e1).unwrap();

        let mut e2 = ParseFailureEntry::new(
            "ws1", "b.rs", "/b.rs", "1:2", "rust",
            "unsupported", "unsupported_language", "no lang", false,
        );
        log.append(&mut e2).unwrap();

        let config = ReplayConfig::default();
        let replayable = replay_pending(&log, &config).unwrap();
        assert_eq!(replayable.len(), 1, "只应重放可重试的 failed entry");
        assert_eq!(replayable[0].rel_path, "a.rs");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn test_is_retryable() {
        let mut e = ParseFailureEntry::new(
            "ws1", "a.rs", "/a.rs", "1:1", "rust",
            "failed", "parse_failed", "err", true,
        );
        assert!(e.is_retryable(3));

        e.retry_count = 3;
        assert!(!e.is_retryable(3), "retry_count=max 时不可重试");
        assert!(e.is_retryable(5), "retry_count < 新 max 时仍可重试");

        e.allows_retry = false;
        assert!(!e.is_retryable(10), "allows_retry=false 时不可重试");

        e.allows_retry = true;
        e.status = "applied".to_string();
        assert!(!e.is_retryable(10), "status=applied 时不可重试");
    }

    #[test]
    fn test_permanent_status_for_non_retryable() {
        let e = ParseFailureEntry::new(
            "ws1", "a.rs", "/a.rs", "1:1", "rust",
            "unsupported", "unsupported_language", "no lang", false,
        );
        assert_eq!(e.status, "permanent", "allows_retry=false 时初始 status 应为 permanent");
    }
}
