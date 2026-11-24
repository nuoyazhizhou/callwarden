//! SessionEventLog —— durable session log（append-only JSONL + 文件轮转 + 回放）。
//!
//! 对标 GitNexus 的 durable session log：把 agent 会话的关键事件
//! （注册 / 激活 / 切换 / 结束）追加写入可回放日志，进程重启后可通过
//! [`SessionEventLog::replay`] 重建会话视图。
//!
//! 设计：
//! - Append-only：新事件追加到当前活动文件末尾，写完 flush + fsync（崩溃安全）。
//! - JSON Lines：每行一条事件；部分写入 / 损坏的行在回放时被跳过。
//! - 磁盘空间上限：活动文件超过 `max_file_bytes` 时轮转。
//! - 轮转：先把活动文件 rename 为 `session.log.<seq>`（旧文件先保留），再新建活动文件；
//!   只有旋转文件数量超过 `max_rotated_files` 时才删除最旧的（0 表示不删除）。
//! - 回放：按 seq 升序重放所有旋转文件 + 活动文件，得到每个 workspace 的活动会话视图。
//!
//! 与 `replicator::SessionStore`（SQLite 会话表）互补：SQLite 是当前态权威，
//! 本模块提供可回放的事件流，用于重启恢复与审计。

use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

/// 活动日志文件默认上限：8 MiB。
pub const DEFAULT_MAX_FILE_BYTES: u64 = 8 * 1024 * 1024;
/// 默认保留的旋转文件数量上限（0 表示不删除）。
pub const DEFAULT_MAX_ROTATED_FILES: usize = 8;

/// 会话事件类型。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SessionEventKind {
    /// 会话注册（分配到 epoch，但尚未激活）。
    Register,
    /// 会话激活（成为 workspace 的活动会话）。
    Activate,
    /// 会话切换（活动会话由旧 session 切换到新 session）。
    Switch,
    /// 会话结束（主动结束或被撤销）。
    End,
}

/// 单条持久化会话事件。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionEvent {
    /// 单调递增的事件序号（由 [`SessionEventLog::append`] 分配）。
    pub seq: u64,
    /// 事件时间戳（Unix epoch 毫秒）。
    pub ts_ms: i64,
    /// workspace 实例标识（daemon 侧的 workspace_instance_id）。
    pub workspace_id: String,
    /// agent 会话标识。
    pub session_id: String,
    /// 会话 epoch（daemon 分配，单调递增）。
    pub session_epoch: i64,
    /// 事件类型。
    pub kind: SessionEventKind,
    /// 可选补充信息（例如切换来源 session）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

impl SessionEvent {
    /// 创建一条事件（seq 由 append 分配，传 0 即可）。
    pub fn new(
        workspace_id: impl Into<String>,
        session_id: impl Into<String>,
        session_epoch: i64,
        kind: SessionEventKind,
    ) -> Self {
        Self {
            seq: 0,
            ts_ms: now_ms(),
            workspace_id: workspace_id.into(),
            session_id: session_id.into(),
            session_epoch,
            kind,
            detail: None,
        }
    }

    /// 附加 detail 字段（builder 风格）。
    pub fn with_detail(mut self, detail: impl Into<String>) -> Self {
        self.detail = Some(detail.into());
        self
    }

    fn from_json_line(line: &str) -> Option<Self> {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            return None;
        }
        serde_json::from_str(trimmed).ok()
    }

    fn to_json_line(&self) -> String {
        // serde_json 默认转义换行符，保证一条事件占一行。
        serde_json::to_string(self).unwrap_or_else(|_| "{}".to_string())
    }
}

/// 回放得到的活动会话视图。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ActiveSessionView {
    pub session_id: String,
    pub session_epoch: i64,
    /// 该会话成为活动会话的事件 seq。
    pub since_seq: u64,
}

/// 回放得到的单个会话状态。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionRecordView {
    pub session_epoch: i64,
    /// 是否已结束（被 End 事件或以被切换的方式撤销）。
    pub ended: bool,
}

/// 由事件日志回放得到的会话视图。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SessionView {
    /// workspace_id -> 当前活动会话。
    pub active: BTreeMap<String, ActiveSessionView>,
    /// workspace_id -> session_id -> 会话状态。
    pub sessions: BTreeMap<String, BTreeMap<String, SessionRecordView>>,
    /// 已回放的最大事件 seq（无事件时为 0）。
    pub last_seq: u64,
    /// 最后一条事件的时间戳（无事件时为 0）。
    pub last_ts_ms: i64,
}

impl SessionView {
    /// 重放一条事件，更新视图。
    fn apply(&mut self, event: &SessionEvent) {
        let workspace = event.workspace_id.clone();
        match event.kind {
            SessionEventKind::Register => {
                self.sessions
                    .entry(workspace)
                    .or_default()
                    .entry(event.session_id.clone())
                    .or_insert(SessionRecordView {
                        session_epoch: event.session_epoch,
                        ended: false,
                    });
            }
            SessionEventKind::Activate | SessionEventKind::Switch => {
                // 旧的活动会话被新会话取代，视为已结束（与 daemon_handle_connect 的
                // “撤销旧 session”语义一致）。
                if let Some(prev) = self.active.get(&workspace).cloned() {
                    if prev.session_id != event.session_id {
                        let records = self.sessions.entry(workspace.clone()).or_default();
                        if let Some(record) = records.get_mut(&prev.session_id) {
                            record.ended = true;
                        }
                    }
                }
                self.sessions
                    .entry(workspace.clone())
                    .or_default()
                    .insert(
                        event.session_id.clone(),
                        SessionRecordView {
                            session_epoch: event.session_epoch,
                            ended: false,
                        },
                    );
                self.active.insert(
                    workspace,
                    ActiveSessionView {
                        session_id: event.session_id.clone(),
                        session_epoch: event.session_epoch,
                        since_seq: event.seq,
                    },
                );
            }
            SessionEventKind::End => {
                let records = self.sessions.entry(workspace.clone()).or_default();
                if let Some(record) = records.get_mut(&event.session_id) {
                    record.ended = true;
                }
                let is_active = self
                    .active
                    .get(&workspace)
                    .map(|a| a.session_id == event.session_id)
                    .unwrap_or(false);
                if is_active {
                    self.active.remove(&workspace);
                }
            }
        }
        if event.seq > self.last_seq {
            self.last_seq = event.seq;
        }
        if event.ts_ms > self.last_ts_ms {
            self.last_ts_ms = event.ts_ms;
        }
    }
}

// ============================================
// Admission barrier（并发会话准入控制）
// ============================================

/// 准入拒绝的稳定错误码（同一 workspace 活动会话数达到阈值）。
pub const E_SESSION_ADMISSION_LIMIT: &str = "E_SESSION_ADMISSION_LIMIT";

/// 默认同一 workspace 同时活动会话数上限。
pub const DEFAULT_MAX_ACTIVE_SESSIONS: usize = 8;

/// 配置活动会话数上限的环境变量名。
pub const ENV_MAX_ACTIVE_SESSIONS: &str = "CALLWARDEN_SESSION_MAX_ACTIVE_SESSIONS";

/// 准入拒绝的结构化错误（携带稳定错误码 + 上下文）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionAdmissionError {
    /// 稳定错误码，恒为 [`E_SESSION_ADMISSION_LIMIT`]。
    pub code: &'static str,
    pub workspace_id: String,
    /// 拒绝时该 workspace 已有的活动会话数。
    pub active_sessions: usize,
    /// 配置的上限。
    pub limit: usize,
}

impl SessionAdmissionError {
    /// 序列化为结构化 JSON（供 RPC 响应使用）。
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "code": self.code,
            "workspace_id": self.workspace_id,
            "active_sessions": self.active_sessions,
            "limit": self.limit,
        })
    }
}

impl std::fmt::Display for SessionAdmissionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{}: workspace {} has {} active sessions (limit {})",
            self.code, self.workspace_id, self.active_sessions, self.limit
        )
    }
}

impl std::error::Error for SessionAdmissionError {}

/// 会话准入屏障：同一 workspace 的活动会话数达到阈值时拒绝新会话。
///
/// 活动会话数取自 [`SessionView`] 中 `ended == false` 的会话记录；
/// 阈值由 [`SessionAdmissionLimiter::from_env`] 从环境变量读取
/// （`CALLWARDEN_SESSION_MAX_ACTIVE_SESSIONS`，0 表示不限制）。
pub struct SessionAdmissionLimiter {
    limit: usize,
}

impl SessionAdmissionLimiter {
    /// 使用显式上限创建（0 表示不限制）。
    pub fn new(limit: usize) -> Self {
        Self { limit }
    }

    /// 从默认环境变量读取上限；未配置或非法值回退到
    /// [`DEFAULT_MAX_ACTIVE_SESSIONS`]。
    pub fn from_env() -> Self {
        Self::from_env_key(ENV_MAX_ACTIVE_SESSIONS)
    }

    /// 从指定环境变量读取上限（便于测试与多实例隔离）。
    pub fn from_env_key(key: &str) -> Self {
        let limit = parse_env_u64(key)
            .map(|v| v as usize)
            .unwrap_or(DEFAULT_MAX_ACTIVE_SESSIONS);
        Self::new(limit)
    }

    /// 配置的上限（0 表示不限制）。
    pub fn limit(&self) -> usize {
        self.limit
    }

    /// 依据回放视图检查是否允许为 workspace 再接纳一个会话。
    ///
    /// 注意：调用时机应在写入新会话的 Activate 事件之前，
    /// 因此 `view` 中的活动会话数即“已被占用”的并发额度。
    pub fn check(
        &self,
        view: &SessionView,
        workspace_id: &str,
    ) -> Result<(), SessionAdmissionError> {
        let active = view
            .sessions
            .get(workspace_id)
            .map(|records| records.values().filter(|record| !record.ended).count())
            .unwrap_or(0);
        self.check_active_count(workspace_id, active)
    }

    /// 依据显式活动会话数检查准入（供不持有 [`SessionView`] 的调用方使用）。
    pub fn check_active_count(
        &self,
        workspace_id: &str,
        active_sessions: usize,
    ) -> Result<(), SessionAdmissionError> {
        if self.limit == 0 || active_sessions < self.limit {
            return Ok(());
        }
        Err(SessionAdmissionError {
            code: E_SESSION_ADMISSION_LIMIT,
            workspace_id: workspace_id.to_string(),
            active_sessions,
            limit: self.limit,
        })
    }
}

/// 持久化会话事件日志。
pub struct SessionEventLog {
    log_path: PathBuf,
    max_file_bytes: u64,
    max_rotated_files: usize,
    inner: Mutex<LogInner>,
}

struct LogInner {
    next_seq: u64,
}

impl SessionEventLog {
    /// 使用默认上限打开（或创建）事件日志。
    pub fn new(log_path: impl AsRef<Path>) -> std::io::Result<Self> {
        Self::with_limits(
            log_path,
            DEFAULT_MAX_FILE_BYTES,
            DEFAULT_MAX_ROTATED_FILES,
        )
    }

    /// 使用显式上限打开（或创建）事件日志。
    ///
    /// `max_file_bytes` 是活动文件轮转阈值；`max_rotated_files` 是保留的旋转文件数量
    /// 上限（0 表示永不删除）。
    pub fn with_limits(
        log_path: impl AsRef<Path>,
        max_file_bytes: u64,
        max_rotated_files: usize,
    ) -> std::io::Result<Self> {
        let log_path = log_path.as_ref().to_path_buf();
        let mut next_seq = 1;
        for (path, _rotated_seq) in list_log_files(&log_path)? {
            for event in read_events_from(&path)? {
                if event.seq >= next_seq {
                    next_seq = event.seq + 1;
                }
            }
        }
        Ok(Self {
            log_path,
            max_file_bytes,
            max_rotated_files,
            inner: Mutex::new(LogInner { next_seq }),
        })
    }

    /// 追加一条事件，返回分配的 seq。
    ///
    /// 写入后执行 flush + fsync，保证崩溃安全。
    pub fn append(&self, event: &mut SessionEvent) -> std::io::Result<u64> {
        let mut inner = self.inner.lock().unwrap();
        event.seq = inner.next_seq;
        if event.ts_ms == 0 {
            event.ts_ms = now_ms();
        }
        inner.next_seq += 1;

        let line = event.to_json_line() + "\n";
        self.maybe_rotate(inner.next_seq - 1, line.len() as u64)?;

        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.log_path)?;
        file.write_all(line.as_bytes())?;
        file.flush()?;
        file.sync_all()?;

        Ok(event.seq)
    }

    /// 读取所有事件（旋转文件 + 活动文件，按 seq 升序）。
    pub fn read_all(&self) -> std::io::Result<Vec<SessionEvent>> {
        let mut events = Vec::new();
        for (path, _rotated_seq) in list_log_files(&self.log_path)? {
            events.extend(read_events_from(&path)?);
        }
        events.sort_by_key(|e| e.seq);
        Ok(events)
    }

    /// 回放全部事件，重建会话视图。
    pub fn replay(&self) -> std::io::Result<SessionView> {
        let mut view = SessionView::default();
        for event in self.read_all()? {
            view.apply(&event);
        }
        Ok(view)
    }

    /// 当前活动文件路径。
    pub fn log_path(&self) -> &Path {
        &self.log_path
    }

    /// 当前已保留的旋转文件（按轮转顺序升序）。
    pub fn rotated_files(&self) -> std::io::Result<Vec<PathBuf>> {
        Ok(list_rotated_files(&self.log_path)?
            .into_iter()
            .map(|(path, _seq)| path)
            .collect())
    }

    /// 下一个将被分配的 seq。
    pub fn next_seq(&self) -> u64 {
        self.inner.lock().unwrap().next_seq
    }

    /// 活动文件当前大小（不存在时为 0）。
    pub fn current_size_bytes(&self) -> std::io::Result<u64> {
        match std::fs::metadata(&self.log_path) {
            Ok(meta) => Ok(meta.len()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(0),
            Err(e) => Err(e),
        }
    }

    /// 若活动文件写入下一行后会超过上限，则先轮转（保留旧文件）。
    fn maybe_rotate(&self, event_seq: u64, incoming_bytes: u64) -> std::io::Result<()> {
        let current = self.current_size_bytes()?;
        if current == 0 || current + incoming_bytes <= self.max_file_bytes {
            return Ok(());
        }
        // 旧文件先 rename 保留，再新建活动文件。
        let rotated = rotated_path(&self.log_path, event_seq);
        std::fs::rename(&self.log_path, &rotated)?;

        if self.max_rotated_files > 0 {
            let rotated_files = list_rotated_files(&self.log_path)?;
            if rotated_files.len() > self.max_rotated_files {
                let remove_count = rotated_files.len() - self.max_rotated_files;
                for (path, _seq) in rotated_files.into_iter().take(remove_count) {
                    // 删除失败不应阻塞写入；残留文件会在下次轮转时再次尝试清理。
                    let _ = std::fs::remove_file(path);
                }
            }
        }
        Ok(())
    }
}

/// 从环境变量读取活动文件大小上限（`CALLWARDEN_SESSION_LOG_MAX_BYTES`）。
pub fn max_file_bytes_from_env() -> u64 {
    parse_env_u64("CALLWARDEN_SESSION_LOG_MAX_BYTES").unwrap_or(DEFAULT_MAX_FILE_BYTES)
}

/// 从环境变量读取旋转文件保留数量（`CALLWARDEN_SESSION_LOG_MAX_FILES`，0 表示不删除）。
pub fn max_rotated_files_from_env() -> usize {
    parse_env_u64("CALLWARDEN_SESSION_LOG_MAX_FILES")
        .map(|v| v as usize)
        .unwrap_or(DEFAULT_MAX_ROTATED_FILES)
}

/// workspace 目录下的默认事件日志路径。
pub fn default_log_path(workspace_dir: impl AsRef<Path>) -> PathBuf {
    workspace_dir.as_ref().join("session_events.log")
}

fn parse_env_u64(key: &str) -> Option<u64> {
    std::env::var(key)
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
}

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn rotated_path(log_path: &Path, seq: u64) -> PathBuf {
    let mut file_name = log_path
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| "session.log".to_string());
    file_name.push_str(&format!(".{}", seq));
    log_path.with_file_name(file_name)
}

/// 解析旋转文件名后缀（`session.log.<seq>`）。
fn rotated_seq(log_path: &Path, candidate: &Path) -> Option<u64> {
    let base = log_path.file_name()?.to_string_lossy().to_string();
    let name = candidate.file_name()?.to_string_lossy().to_string();
    let suffix = name.strip_prefix(&format!("{}.", base))?;
    suffix.parse::<u64>().ok()
}

/// 列出旋转文件（按轮转 seq 升序）。
fn list_rotated_files(log_path: &Path) -> std::io::Result<Vec<(PathBuf, u64)>> {
    let parent = log_path.parent().unwrap_or_else(|| Path::new("."));
    let mut files = Vec::new();
    let entries = match std::fs::read_dir(parent) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(files),
        Err(e) => return Err(e),
    };
    for entry in entries {
        let entry = entry?;
        let path = entry.path();
        if let Some(seq) = rotated_seq(log_path, &path) {
            files.push((path, seq));
        }
    }
    files.sort_by_key(|(_, seq)| *seq);
    Ok(files)
}

/// 列出所有日志文件：旋转文件（升序）在前，活动文件（若存在）在后。
fn list_log_files(log_path: &Path) -> std::io::Result<Vec<(PathBuf, Option<u64>)>> {
    let mut files: Vec<(PathBuf, Option<u64>)> = list_rotated_files(log_path)?
        .into_iter()
        .map(|(path, seq)| (path, Some(seq)))
        .collect();
    if log_path.exists() {
        files.push((log_path.to_path_buf(), None));
    }
    Ok(files)
}

fn read_events_from(path: &Path) -> std::io::Result<Vec<SessionEvent>> {
    let file = File::open(path)?;
    let reader = BufReader::new(file);
    let mut events = Vec::new();
    for line in reader.lines() {
        let line = match line {
            Ok(line) => line,
            Err(_) => continue,
        };
        // 损坏 / 部分写入的行直接跳过（崩溃安全）。
        if let Some(event) = SessionEvent::from_json_line(&line) {
            events.push(event);
        }
    }
    Ok(events)
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;

    fn make_log(dir: &Path) -> SessionEventLog {
        SessionEventLog::new(dir.join("session_events.log")).unwrap()
    }

    fn event(kind: SessionEventKind, session: &str, epoch: i64) -> SessionEvent {
        SessionEvent::new("ws-1", session, epoch, kind)
    }

    #[test]
    fn test_append_assigns_monotonic_seq_and_persists() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("session_events.log");
        {
            let log = SessionEventLog::new(&path).unwrap();
            let mut e1 = event(SessionEventKind::Register, "s1", 1);
            let mut e2 = event(SessionEventKind::Activate, "s1", 1);
            assert_eq!(log.append(&mut e1).unwrap(), 1);
            assert_eq!(log.append(&mut e2).unwrap(), 2);
            assert_eq!(log.next_seq(), 3);
        }
        // 重新打开后 next_seq 从日志恢复
        let log = SessionEventLog::new(&path).unwrap();
        assert_eq!(log.next_seq(), 3);
        let events = log.read_all().unwrap();
        assert_eq!(events.len(), 2);
        assert_eq!(events[0].seq, 1);
        assert_eq!(events[1].seq, 2);
        assert_eq!(events[1].kind, SessionEventKind::Activate);
    }

    #[test]
    fn test_replay_rebuilds_active_session_view() {
        let tmp = tempfile::tempdir().unwrap();
        let log = make_log(tmp.path());

        let mut register = event(SessionEventKind::Register, "s1", 1);
        let mut activate = event(SessionEventKind::Activate, "s1", 1);
        let mut switch = event(SessionEventKind::Switch, "s2", 2);
        log.append(&mut register).unwrap();
        log.append(&mut activate).unwrap();
        log.append(&mut switch).unwrap();

        let view = log.replay().unwrap();
        let active = view.active.get("ws-1").expect("active session missing");
        assert_eq!(active.session_id, "s2");
        assert_eq!(active.session_epoch, 2);
        assert_eq!(active.since_seq, 3);

        let sessions = view.sessions.get("ws-1").unwrap();
        assert!(sessions.get("s1").unwrap().ended);
        assert!(!sessions.get("s2").unwrap().ended);
        assert_eq!(view.last_seq, 3);
    }

    #[test]
    fn test_replay_end_clears_active_only_for_matching_session() {
        let tmp = tempfile::tempdir().unwrap();
        let log = make_log(tmp.path());

        let mut activate = event(SessionEventKind::Activate, "s1", 1);
        let mut end_other = event(SessionEventKind::End, "s2", 2);
        log.append(&mut activate).unwrap();
        log.append(&mut end_other).unwrap();

        let view = log.replay().unwrap();
        // 结束其他会话不影响当前活动会话
        assert_eq!(view.active.get("ws-1").unwrap().session_id, "s1");

        let mut end_s1 = event(SessionEventKind::End, "s1", 1);
        log.append(&mut end_s1).unwrap();
        let view = log.replay().unwrap();
        assert!(view.active.get("ws-1").is_none());
        assert!(view.sessions.get("ws-1").unwrap().get("s1").unwrap().ended);
    }

    #[test]
    fn test_rotation_keeps_old_files_and_replays_all_events() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("session_events.log");
        // 阈值很小：每条事件都会触发轮转；保留上限 0 = 不删除
        let log = SessionEventLog::with_limits(&path, 1, 0).unwrap();

        for i in 0..5 {
            let mut e = event(SessionEventKind::Activate, &format!("s{}", i), i);
            log.append(&mut e).unwrap();
        }

        let rotated = log.rotated_files().unwrap();
        // 每次写入前活动文件非空即轮转：第 1 条写入后，后续 4 条各轮转一次
        assert_eq!(rotated.len(), 4);
        // 活动文件仍存在（最后一条事件）
        assert!(log.current_size_bytes().unwrap() > 0);

        // 轮转后所有事件仍可回放
        let events = log.read_all().unwrap();
        assert_eq!(events.len(), 5);
        assert_eq!(events.first().unwrap().seq, 1);
        assert_eq!(events.last().unwrap().seq, 5);

        let view = log.replay().unwrap();
        assert_eq!(view.active.get("ws-1").unwrap().session_id, "s4");
    }

    #[test]
    fn test_rotation_prunes_only_when_over_retention() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("session_events.log");
        // 阈值 1 字节，保留 2 个旋转文件
        let log = SessionEventLog::with_limits(&path, 1, 2).unwrap();

        for i in 0..6 {
            let mut e = event(SessionEventKind::Activate, &format!("s{}", i), i);
            log.append(&mut e).unwrap();
        }

        let rotated = log.rotated_files().unwrap();
        assert_eq!(rotated.len(), 2);
        // 保留的是最近的旋转文件
        let events = log.read_all().unwrap();
        assert!(events.len() >= 2);
        let last = events.last().unwrap();
        assert_eq!(last.session_id, "s5");
    }

    #[test]
    fn test_corrupted_lines_are_skipped() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("session_events.log");
        {
            let log = SessionEventLog::new(&path).unwrap();
            let mut e = event(SessionEventKind::Activate, "s1", 1);
            log.append(&mut e).unwrap();
        }
        {
            let mut file = OpenOptions::new().append(true).open(&path).unwrap();
            writeln!(file, "this is not json").unwrap();
            writeln!(file, "{{invalid").unwrap();
        }
        let log = SessionEventLog::new(&path).unwrap();
        let events = log.read_all().unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].seq, 1);
        assert_eq!(log.next_seq(), 2);
    }

    #[test]
    fn test_event_json_roundtrip_preserves_fields() {
        let e = SessionEvent::new("ws", "s1", 3, SessionEventKind::Switch)
            .with_detail("from=s0");
        let line = e.to_json_line();
        let parsed = SessionEvent::from_json_line(&line).unwrap();
        assert_eq!(parsed, e);
        assert!(line.contains("\"switch\""));
    }

    #[test]
    fn test_default_log_path_uses_workspace_dir() {
        let path = default_log_path("C:/data/ws-1");
        assert!(path.ends_with("session_events.log"));
    }

    // ---- admission barrier 测试 ----

    fn view_with_active(workspace: &str, active: usize, ended: usize) -> SessionView {
        let mut view = SessionView::default();
        let mut records = BTreeMap::new();
        for i in 0..active {
            records.insert(
                format!("s{}", i),
                SessionRecordView {
                    session_epoch: i as i64 + 1,
                    ended: false,
                },
            );
        }
        for i in 0..ended {
            records.insert(
                format!("ended{}", i),
                SessionRecordView {
                    session_epoch: 100 + i as i64,
                    ended: true,
                },
            );
        }
        view.sessions.insert(workspace.to_string(), records);
        view
    }

    #[test]
    fn test_admission_allows_below_limit() {
        let limiter = SessionAdmissionLimiter::new(3);
        let view = view_with_active("ws-1", 2, 5);
        assert!(limiter.check(&view, "ws-1").is_ok());
        // 未出现过的 workspace 视为 0 个活动会话
        assert!(limiter.check(&view, "ws-unknown").is_ok());
    }

    #[test]
    fn test_admission_rejects_at_limit_with_stable_code() {
        let limiter = SessionAdmissionLimiter::new(2);
        let view = view_with_active("ws-1", 2, 3);
        let err = limiter.check(&view, "ws-1").unwrap_err();
        assert_eq!(err.code, E_SESSION_ADMISSION_LIMIT);
        assert_eq!(err.active_sessions, 2);
        assert_eq!(err.limit, 2);
        assert_eq!(err.workspace_id, "ws-1");

        let json = err.to_json();
        assert_eq!(json["code"], E_SESSION_ADMISSION_LIMIT);
        assert_eq!(json["active_sessions"], 2);
        assert_eq!(json["limit"], 2);
    }

    #[test]
    fn test_admission_ignores_ended_sessions() {
        let limiter = SessionAdmissionLimiter::new(2);
        // 2 个已结束 + 1 个活动 -> 允许
        let view = view_with_active("ws-1", 1, 2);
        assert!(limiter.check(&view, "ws-1").is_ok());
    }

    #[test]
    fn test_admission_zero_limit_is_unlimited() {
        let limiter = SessionAdmissionLimiter::new(0);
        let view = view_with_active("ws-1", 10_000, 0);
        assert!(limiter.check(&view, "ws-1").is_ok());
    }

    #[test]
    fn test_admission_limit_from_env_key() {
        let key = "CALLWARDEN_TEST_SESSION_MAX_ACTIVE_UNIQUE";
        std::env::remove_var(key);
        // 未配置 -> 默认值
        assert_eq!(
            SessionAdmissionLimiter::from_env_key(key).limit(),
            DEFAULT_MAX_ACTIVE_SESSIONS
        );
        // 配置合法值 -> 生效
        std::env::set_var(key, "2");
        assert_eq!(SessionAdmissionLimiter::from_env_key(key).limit(), 2);
        // 非法值 -> 回退默认
        std::env::set_var(key, "not-a-number");
        assert_eq!(
            SessionAdmissionLimiter::from_env_key(key).limit(),
            DEFAULT_MAX_ACTIVE_SESSIONS
        );
        std::env::remove_var(key);
    }

    #[test]
    fn test_admission_limit_from_default_env_var() {
        // 生产入口 from_env() 读真实环境变量 CALLWARDEN_SESSION_MAX_ACTIVE_SESSIONS：
        // daemon 侧 workspace.connect 的准入阈值由此 env 可调。
        // 本测试进程内没有其他用例设置该键（其余用例走 override seam 或独立键），
        // 置位/复位窗口内无并发读者。
        let key = ENV_MAX_ACTIVE_SESSIONS;
        std::env::remove_var(key);
        assert_eq!(
            SessionAdmissionLimiter::from_env().limit(),
            DEFAULT_MAX_ACTIVE_SESSIONS
        );
        std::env::set_var(key, "3");
        assert_eq!(SessionAdmissionLimiter::from_env().limit(), 3);
        std::env::set_var(key, "0");
        assert_eq!(
            SessionAdmissionLimiter::from_env().limit(),
            0,
            "0 表示不限制"
        );
        std::env::remove_var(key);
    }

    #[test]
    fn test_admission_check_active_count() {
        let limiter = SessionAdmissionLimiter::new(1);
        assert!(limiter.check_active_count("ws-1", 0).is_ok());
        let err = limiter.check_active_count("ws-1", 1).unwrap_err();
        assert_eq!(err.code, E_SESSION_ADMISSION_LIMIT);
        assert!(err.to_string().contains(E_SESSION_ADMISSION_LIMIT));
    }

    #[test]
    fn test_admission_concurrent_over_limit_is_rejected() {
        use std::sync::{Arc, Barrier, Mutex};

        // 上限 3，已有 2 个活动会话：并发尝试 8 个新会话，只应放行 1 个。
        let limiter = Arc::new(SessionAdmissionLimiter::new(3));
        let view = Arc::new(Mutex::new(view_with_active("ws-1", 2, 0)));
        let barrier = Arc::new(Barrier::new(8));

        let mut handles = Vec::new();
        for i in 0..8 {
            let limiter = Arc::clone(&limiter);
            let view = Arc::clone(&view);
            let barrier = Arc::clone(&barrier);
            handles.push(std::thread::spawn(move || -> Result<bool, String> {
                barrier.wait();
                // 持有同一把锁，使“检查 + 登记”成为原子临界区。
                let mut guard = view.lock().unwrap();
                if limiter.check(&guard, "ws-1").is_ok() {
                    let records = guard
                        .sessions
                        .get_mut("ws-1")
                        .ok_or_else(|| "ws-1 records missing".to_string())?;
                    records.insert(
                        format!("new-{}", i),
                        SessionRecordView {
                            session_epoch: 1000 + i,
                            ended: false,
                        },
                    );
                    Ok(true)
                } else {
                    Ok(false)
                }
            }));
        }

        let results: Vec<bool> = handles
            .into_iter()
            .map(|handle| handle.join().expect("thread panicked").expect("admission error"))
            .collect();
        let admitted = results.iter().filter(|admitted| **admitted).count();
        let rejected = results.len() - admitted;
        assert_eq!(admitted, 1, "并发超限应只放行剩余额度");
        assert_eq!(rejected, 7);

        let active = view
            .lock()
            .unwrap()
            .sessions
            .get("ws-1")
            .unwrap()
            .values()
            .filter(|record| !record.ended)
            .count();
        assert_eq!(active, 3);
    }

    #[test]
    fn test_restart_replay_view_is_consistent() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("session_events.log");

        // 进程 1：写入事件（阈值很小，中途发生轮转）
        let view_before = {
            let log = SessionEventLog::with_limits(&path, 64, 0).unwrap();
            let mut register = event(SessionEventKind::Register, "s1", 1);
            let mut activate = event(SessionEventKind::Activate, "s1", 1);
            let mut switch = event(SessionEventKind::Switch, "s2", 2);
            let mut register3 = event(SessionEventKind::Register, "s3", 3);
            let mut activate3 = event(SessionEventKind::Activate, "s3", 3);
            for e in [
                &mut register,
                &mut activate,
                &mut switch,
                &mut register3,
                &mut activate3,
            ] {
                log.append(e).unwrap();
            }
            let view = log.replay().unwrap();
            assert!(!log.rotated_files().unwrap().is_empty(), "应发生轮转");
            view
        };

        // 进程 2：重启后从同一路径恢复
        let log = SessionEventLog::new(&path).unwrap();
        let view_after = log.replay().unwrap();

        assert_eq!(view_before, view_after, "重启前后会话视图必须一致");
        let active = view_after
            .active
            .get("ws-1")
            .expect("重启后活动会话丢失");
        assert_eq!(active.session_id, "s3");
        assert_eq!(active.session_epoch, 3);
        let sessions = view_after.sessions.get("ws-1").unwrap();
        assert!(sessions.get("s1").unwrap().ended);
        assert!(sessions.get("s2").unwrap().ended);
        assert!(!sessions.get("s3").unwrap().ended);
        // 重启后继续追加不会与旧 seq 冲突
        assert_eq!(log.next_seq(), view_after.last_seq + 1);
    }
}
