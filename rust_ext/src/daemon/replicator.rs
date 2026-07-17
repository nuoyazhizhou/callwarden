//! Replicator —— Session 管理 + daemon_handle_connect + daemon_handle_refresh + Replicator。
//!
//! 对应 Python `server/replicator.py`：
//! - SESSION_SCHEMA_DDL + init_session_schema
//! - daemon_handle_connect：session epoch CAS，旧 session 永久失效
//! - daemon_handle_refresh：完整管道（session epoch 校验 + 两阶段 CAS）
//! - Replicator 类（replicate / recover / get_pending_count）
//!
//! 跨平台：rusqlite + CasStore + StagingLog，Windows 可完整验收。
//!
//! R5 阶段不接入 SnapshotManagerService（依赖 R6），用 `SnapshotPublisher` trait
//! 钩子抽象，R6 实现真正的 SnapshotManager 后注入即可。

use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection};
use serde_json::{Map, Value};

use super::cas::{CasPublishError, CasStore};
use super::staging_log::{StagingEntry, StagingLog};

// ============================================
// Session 管理 schema（与 Python replicator.py:SESSION_SCHEMA_DDL 一致）
// ============================================

pub const SESSION_SCHEMA_DDL: &str = r#"
CREATE TABLE IF NOT EXISTS agent_sessions (
    workspace_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    session_epoch INTEGER NOT NULL,
    activated_at INTEGER NOT NULL,
    revoked_at INTEGER,
    peer_uid INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, session_id)
);

CREATE TABLE IF NOT EXISTS workspace_active_session (
    workspace_id INTEGER PRIMARY KEY,
    active_session_id TEXT NOT NULL,
    active_session_epoch INTEGER NOT NULL
);
"#;

// file_generations DDL 从 cas.rs 复用（CAS_SCHEMA_DDL 已包含）
// 这里不需要重复定义，init_session_schema 假设 CasStore 已经初始化过 file_generations

/// 初始化 session 管理 schema（agent_sessions + workspace_active_session）
///
/// 注意：调用方需先初始化 CAS schema（含 file_generations 表），再调用此函数。
/// 对应 Python replicator.py:init_session_schema
pub fn init_session_schema(conn: &Connection) -> Result<(), rusqlite::Error> {
    conn.execute_batch("PRAGMA busy_timeout=5000;")?;
    conn.execute_batch(SESSION_SCHEMA_DDL)?;
    Ok(())
}

fn now_unix() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

// ============================================
// Session epoch CAS（对应 Python daemon_handle_connect）
// ============================================

/// Session epoch / generation CAS 协议错误
#[derive(Debug, Clone)]
pub struct ProtocolError {
    pub message: String,
}

impl ProtocolError {
    pub fn new(msg: impl Into<String>) -> Self {
        Self {
            message: msg.into(),
        }
    }
}

impl std::fmt::Display for ProtocolError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl std::error::Error for ProtocolError {}

/// agent 连接握手——daemon 分配单调 epoch，旧 session 永久失效。
///
/// 规范：watcher-generation-state-machine.md §4.1
/// 对应 Python replicator.py:daemon_handle_connect
///
/// 完整流程：
/// 1. 撤销同一 workspace 所有旧 active session（UPDATE revoked_at）
/// 2. 分配 new_epoch = MAX(all session_epoch) + 1
/// 3. INSERT agent_sessions
/// 4. INSERT OR REPLACE workspace_active_session
/// 5. UPDATE file_generations SET latest_seq=0（新 session seq 从 1 开始）
///
/// 返回：`{"session_epoch": N}`
pub fn daemon_handle_connect(
    peer_uid: u32,
    workspace_id: i64,
    requested_session_id: &str,
    ws_conn: &Mutex<Connection>,
) -> Result<Value, ProtocolError> {
    let now = now_unix();
    let conn = ws_conn.lock().unwrap();
    conn.execute_batch("BEGIN IMMEDIATE").map_err(|e| {
        ProtocolError::new(format!("BEGIN IMMEDIATE 失败: {}", e))
    })?;
    let result = daemon_handle_connect_inner(
        &conn,
        peer_uid,
        workspace_id,
        requested_session_id,
        now,
    );
    match result {
        Ok(value) => {
            if let Err(e) = conn.execute_batch("COMMIT") {
                let _ = conn.execute_batch("ROLLBACK");
                return Err(ProtocolError::new(format!("COMMIT 失败: {}", e)));
            }
            Ok(value)
        }
        Err(e) => {
            let _ = conn.execute_batch("ROLLBACK");
            Err(e)
        }
    }
}

fn daemon_handle_connect_inner(
    conn: &Connection,
    peer_uid: u32,
    workspace_id: i64,
    requested_session_id: &str,
    now: i64,
) -> Result<Value, ProtocolError> {
    // 1. 撤销同一 workspace 所有旧 active session
    conn.execute(
        "UPDATE agent_sessions SET revoked_at = ?1
         WHERE workspace_id = ?2 AND revoked_at IS NULL",
        params![now, workspace_id],
    )
    .map_err(|e| ProtocolError::new(format!("撤销旧 session 失败: {}", e)))?;

    // 2. 分配 new_epoch = MAX(all session_epoch) + 1
    let new_epoch: i64 = conn
        .query_row(
            "SELECT COALESCE(MAX(session_epoch), 0) + 1 FROM agent_sessions WHERE workspace_id = ?1",
            params![workspace_id],
            |row| row.get(0),
        )
        .map_err(|e| ProtocolError::new(format!("查询 next_epoch 失败: {}", e)))?;

    // 3. INSERT agent_sessions
    conn.execute(
        "INSERT INTO agent_sessions (workspace_id, session_id, session_epoch,
         activated_at, revoked_at, peer_uid) VALUES (?1, ?2, ?3, ?4, NULL, ?5)",
        params![workspace_id, requested_session_id, new_epoch, now, peer_uid],
    )
    .map_err(|e| ProtocolError::new(format!("INSERT agent_sessions 失败: {}", e)))?;

    // 4. INSERT OR REPLACE workspace_active_session
    conn.execute(
        "INSERT OR REPLACE INTO workspace_active_session
         (workspace_id, active_session_id, active_session_epoch) VALUES (?1, ?2, ?3)",
        params![workspace_id, requested_session_id, new_epoch],
    )
    .map_err(|e| ProtocolError::new(format!("INSERT workspace_active_session 失败: {}", e)))?;

    // 5. UPDATE file_generations SET latest_seq=0（新 session seq 从 1 开始）
    conn.execute(
        "UPDATE file_generations SET latest_session_id = ?1,
         latest_session_epoch = ?2, latest_seq = 0,
         latest_seen_generation = '' WHERE workspace_id = ?3",
        params![requested_session_id, new_epoch, workspace_id],
    )
    .map_err(|e| ProtocolError::new(format!("UPDATE file_generations 失败: {}", e)))?;

    let mut m = Map::new();
    m.insert(
        "session_epoch".to_string(),
        Value::Number(new_epoch.into()),
    );
    Ok(Value::Object(m))
}

// ============================================
// daemon_handle_refresh 完整管道
// ============================================

/// refresh 消息（对应 Python msg dict）
#[derive(Debug, Clone)]
pub struct RefreshMessage {
    pub rel_path: String,
    pub agent_session_id: String,
    pub monotonic_seq: i64,
    pub session_epoch: i64,
    /// 可选：客户端提供的 abs_path（仅在 canonical_bytes 为 None 时使用）
    pub abs_path: Option<String>,
}

/// refresh 处理结果
#[derive(Debug, Clone)]
pub struct RefreshResult {
    pub status: String, // "committed" / "stale_seq_dropped" / "no_cas"
    pub generation: String,
    /// CAS publish 结果（若发生）
    pub cas_result: Option<Value>,
}

/// 处理 agent refresh 消息——session epoch 校验 + 两阶段 CAS。
///
/// 规范：watcher-generation-state-machine.md §4.3
/// 对应 Python replicator.py:daemon_handle_refresh
///
/// 完整管道：
/// 1. session epoch 校验（拒绝 stale session）
/// 2. CAS 第一阶段（seen）—— 原子更新 latest_seen_generation
/// 3. CAS 第二阶段（committed）—— 条件更新 latest_committed_generation
///
/// 注意：R5 阶段不做 daemon 侧 parse + CAS publish（依赖 R6 接入 callwarden_core
/// 的 parse_canonical_bytes_py）。本函数只做 session epoch + file_generation 两阶段
/// CAS。完整的 parse + publish 管道由 R6 实现（或上层调用方在调用本函数后自行处理）。
///
/// 参数：
/// - workspace_id: workspace ID
/// - msg: refresh 消息
/// - ws_conn: workspace 数据库连接（含 agent_sessions / workspace_active_session /
///   file_generations 表）
/// - cas_store: CAS store（用于两阶段 CAS，若为 None 则跳过 CAS 部分）
pub fn daemon_handle_refresh(
    workspace_id: i64,
    msg: &RefreshMessage,
    ws_conn: &Mutex<Connection>,
    cas_store: Option<&CasStore>,
) -> Result<RefreshResult, ProtocolError> {
    // 1. 校验 session epoch——只能匹配当前 active epoch
    let active_session = {
        let conn = ws_conn.lock().unwrap();
        conn.query_row(
            "SELECT active_session_id, active_session_epoch
             FROM workspace_active_session WHERE workspace_id = ?1",
            params![workspace_id],
            |row| {
                Ok(ActiveSession {
                    session_id: row.get(0)?,
                    session_epoch: row.get(1)?,
                })
            },
        )
        .optional()
        .map_err(|e| ProtocolError::new(format!("查询 active session 失败: {}", e)))?
    };
    let active = active_session.ok_or_else(|| {
        ProtocolError::new(format!("no active session for workspace {}", workspace_id))
    })?;
    if msg.agent_session_id != active.session_id
        || msg.session_epoch != active.session_epoch
    {
        return Err(ProtocolError::new(format!(
            "stale session rejected: incoming={}:{} active={}:{}",
            msg.agent_session_id, msg.session_epoch,
            active.session_id, active.session_epoch
        )));
    }

    let incoming_gen = format!("{}:{}", msg.session_epoch, msg.monotonic_seq);

    // 2. CAS 第一阶段（seen）—— 原子更新 latest_seen_generation
    let seen_result = cas_store
        .map(|s| s.file_generation_seen(workspace_id, &msg.rel_path, &msg.agent_session_id,
                                         msg.session_epoch, msg.monotonic_seq))
        .transpose()
        .map_err(|e| ProtocolError::new(format!("file_generation_seen 失败: {}", e)))?;

    match seen_result {
        Some(seen) if !seen => {
            // stale seq 直接丢弃，不报错
            return Ok(RefreshResult {
                status: "stale_seq_dropped".to_string(),
                generation: String::new(),
                cas_result: None,
            });
        }
        _ => {}
    }

    // 3. CAS 第二阶段（committed）—— 条件更新 latest_committed_generation
    // 注意：R5 阶段不做 daemon 侧 parse + CAS publish，跳过这一步。
    // 完整管道（含 parse + publish）由 R6 实现。
    let committed = cas_store
        .map(|s| s.file_generation_committed(workspace_id, &msg.rel_path,
                                              msg.session_epoch, msg.monotonic_seq))
        .transpose()
        .map_err(|e| ProtocolError::new(format!("file_generation_committed 失败: {}", e)))?;

    if committed == Some(false) {
        return Err(ProtocolError::new(format!(
            "stale manifest commit for {}",
            msg.rel_path
        )));
    }

    Ok(RefreshResult {
        status: "committed".to_string(),
        generation: incoming_gen,
        cas_result: None,
    })
}

/// active session 查询结果
#[derive(Debug, Clone)]
struct ActiveSession {
    session_id: String,
    session_epoch: i64,
}

// ============================================
// SnapshotPublisher trait（R6 扩展点）
// ============================================

/// Snapshot 发布器 trait（对应 Python SnapshotManagerService）
///
/// R5 阶段无实现，Replicator 用 `None` 调用方跳过 snapshot 发布。
/// R6 实现真正的 SnapshotManager 后注入。
pub trait SnapshotPublisher: Send + Sync {
    /// 发布新 generation snapshot
    fn publish_snapshot(
        &self,
        workspace_instance_id: &str,
        db_path: &str,
        build_context_hash: &str,
    ) -> Result<PublishResult, String>;
}

/// publish_snapshot 返回结果
#[derive(Debug, Clone, Default)]
pub struct PublishResult {
    pub generation: i64,
}

// ============================================
// Replicator
// ============================================

/// 单次 replication 的结果（对应 Python ReplicationResult）
#[derive(Debug, Clone, Default)]
pub struct ReplicationResult {
    pub success: bool,
    pub workspace_id: String,
    pub generation: i64,
    pub applied_lsns: Vec<i64>,
    pub pending_count: usize,
    pub applied_count: usize,
    pub error: Option<String>,
    pub duration_ms: f64,
}

impl ReplicationResult {
    pub fn summary(&self) -> String {
        let status = if self.success { "ok" } else { "failed" };
        format!(
            "ReplicationResult({}, ws={}, gen={}, {}/{}) applied",
            status,
            self.workspace_id,
            self.generation,
            self.applied_count,
            self.pending_count
        )
    }
}

/// Replicator——合并 staging log 中的 delta 并发布新 generation。
///
/// 对应 Python `server/replicator.py:Replicator`。
/// Replicator 是单线程的（由 Coordinator 调用），不需要内部锁。
pub struct Replicator<'a> {
    pub staging_log: &'a StagingLog,
    /// 可选的 snapshot publisher（None 时只更新 log，不发布 snapshot）
    pub snapshot_publisher: Option<&'a dyn SnapshotPublisher>,
}

impl<'a> Replicator<'a> {
    pub fn new(staging_log: &'a StagingLog) -> Self {
        Self {
            staging_log,
            snapshot_publisher: None,
        }
    }

    pub fn with_snapshot_publisher(
        mut self,
        publisher: &'a dyn SnapshotPublisher,
    ) -> Self {
        self.snapshot_publisher = Some(publisher);
        self
    }

    /// 执行一次 replication：读取 pending → 发布新 generation → 标记 applied。
    ///
    /// 参数：
    /// - workspace_id: workspace 实例 ID
    /// - db_path: SQLite 数据库路径（用于 publish_snapshot）
    /// - build_context_hash: build context 哈希
    pub fn replicate(
        &self,
        workspace_id: &str,
        db_path: &str,
        build_context_hash: &str,
    ) -> ReplicationResult {
        let start_time = SystemTime::now();
        let mut result = ReplicationResult {
            success: true,
            workspace_id: workspace_id.to_string(),
            ..Default::default()
        };

        // 1. 读取 pending entries
        let all_pending = match self.staging_log.read_pending() {
            Ok(e) => e,
            Err(e) => {
                result.success = false;
                result.error = Some(format!("read_pending failed: {}", e));
                result.duration_ms = elapsed_ms(&start_time);
                return result;
            }
        };
        // 过滤当前 workspace 的 entries
        let pending: Vec<StagingEntry> = all_pending
            .into_iter()
            .filter(|e| e.workspace_id == workspace_id)
            .collect();
        result.pending_count = pending.len();

        if pending.is_empty() {
            result.duration_ms = elapsed_ms(&start_time);
            return result;
        }

        // 2. 合并 delta（简单汇总，对应 Python _merge_deltas）
        let _merged = self.merge_deltas(&pending);

        // 3. 发布新 generation
        if let Some(publisher) = self.snapshot_publisher {
            if !db_path.is_empty() {
                match publisher.publish_snapshot(workspace_id, db_path, build_context_hash) {
                    Ok(pub_result) => {
                        result.generation = pub_result.generation;
                    }
                    Err(e) => {
                        result.success = false;
                        result.error = Some(format!("publish failed: {}", e));
                        // 标记 entries 为 failed
                        for entry in &pending {
                            let _ = self.staging_log.mark_failed(entry.lsn, &e);
                        }
                        result.duration_ms = elapsed_ms(&start_time);
                        return result;
                    }
                }
            }
        }

        // 4. 批量标记 entries 为 applied（单次文件重写）
        result.applied_lsns = pending.iter().map(|e| e.lsn).collect();
        if !result.applied_lsns.is_empty() {
            if let Err(e) = self.staging_log.mark_applied_batch(&result.applied_lsns) {
                result.success = false;
                result.error = Some(format!("mark_applied_batch failed: {}", e));
                result.duration_ms = elapsed_ms(&start_time);
                return result;
            }
        }
        result.applied_count = result.applied_lsns.len();

        // 5. 压缩已应用的 entries（按 status 而非 LSN，避免误删其他 workspace）
        if !result.applied_lsns.is_empty() {
            let _ = self.staging_log.compact_applied(Some(workspace_id));
        }

        result.duration_ms = elapsed_ms(&start_time);
        result
    }

    /// 从 crash 恢复：读取所有 pending entries 并重新 replication。
    ///
    /// 在 daemon 启动时调用。
    pub fn recover(
        &self,
        workspace_id: &str,
        db_path: &str,
    ) -> ReplicationResult {
        self.replicate(workspace_id, db_path, "")
    }

    /// 获取 pending entries 数量。
    ///
    /// 参数：workspace_id 如果指定，只返回该 workspace 的 pending 数量
    pub fn get_pending_count(&self, workspace_id: Option<&str>) -> usize {
        let pending = match self.staging_log.read_pending() {
            Ok(e) => e,
            Err(_) => return 0,
        };
        match workspace_id {
            Some(ws_id) => pending.iter().filter(|e| e.workspace_id == ws_id).count(),
            None => pending.len(),
        }
    }

    /// 合并多个 staging entries 的 delta（对应 Python _merge_deltas）
    ///
    /// 目前是简单汇总，实际可做更复杂的 merge（如冲突检测、去重等）。
    fn merge_deltas(&self, entries: &[StagingEntry]) -> MergedDelta {
        let mut merged = MergedDelta::default();
        for entry in entries {
            merged.files.push(MergedFile {
                file_path: entry.file_path.clone(),
                content_hash: entry.content_hash.clone(),
                language: entry.language.clone(),
            });
            // parse_delta.symbol_delta.added/removed/changed 数量
            if let Some(symbol_delta) = entry.parse_delta.get("symbol_delta") {
                if let Some(added) = symbol_delta.get("added").and_then(|v| v.as_array()) {
                    merged.total_added_symbols += added.len();
                }
                if let Some(removed) = symbol_delta.get("removed").and_then(|v| v.as_array()) {
                    merged.total_removed_symbols += removed.len();
                }
                if let Some(changed) = symbol_delta.get("changed").and_then(|v| v.as_array()) {
                    merged.total_changed_symbols += changed.len();
                }
            }
            // resolve_delta.added/removed 数量
            if let Some(resolve_delta) = entry.resolve_delta.get("added").and_then(|v| v.as_array()) {
                merged.total_added_edges += resolve_delta.len();
            }
            if let Some(resolve_delta) = entry.resolve_delta.get("removed").and_then(|v| v.as_array()) {
                merged.total_removed_edges += resolve_delta.len();
            }
        }
        merged
    }
}

fn elapsed_ms(start: &SystemTime) -> f64 {
    start.elapsed().map(|d| d.as_secs_f64() * 1000.0).unwrap_or(0.0)
}

/// 合并后的 delta summary（对应 Python _merge_deltas 返回的 dict）
#[derive(Debug, Default)]
struct MergedDelta {
    files: Vec<MergedFile>,
    total_added_symbols: usize,
    total_removed_symbols: usize,
    total_changed_symbols: usize,
    total_added_edges: usize,
    total_removed_edges: usize,
}

#[derive(Debug)]
struct MergedFile {
    file_path: String,
    content_hash: String,
    language: String,
}

// ============================================
// SessionStore：封装 ws_conn（agent_sessions + workspace_active_session + file_generations）
// ============================================

/// Workspace session 数据库（封装 agent_sessions + workspace_active_session + file_generations）
///
/// 对应 Python EnterpriseDaemonService._get_workspace_resources 中的 ws_conn。
/// R5 阶段用 CasStore.open_in_memory() 或 CasStore.open(path) 初始化（已含 file_generations），
/// 然后调用 init_session_schema 补充 agent_sessions + workspace_active_session。
pub struct SessionStore {
    conn: Mutex<Connection>,
}

impl SessionStore {
    /// 打开指定路径的 session DB（不存在则创建并初始化 schema）
    ///
    /// schema 包含：
    /// - file_generations（来自 CAS schema）
    /// - agent_sessions + workspace_active_session（来自 SESSION_SCHEMA_DDL）
    pub fn open(db_path: &str) -> Result<Self, rusqlite::Error> {
        if let Some(parent) = std::path::Path::new(db_path).parent() {
            if !parent.as_os_str().is_empty() {
                let _ = std::fs::create_dir_all(parent);
            }
        }
        let conn = Connection::open(db_path)?;
        Self::init_conn(&conn)?;
        Ok(Self {
            conn: Mutex::new(conn),
        })
    }

    /// 内存数据库（测试用）
    pub fn open_in_memory() -> Result<Self, rusqlite::Error> {
        let conn = Connection::open_in_memory()?;
        Self::init_conn(&conn)?;
        Ok(Self {
            conn: Mutex::new(conn),
        })
    }

    fn init_conn(conn: &Connection) -> Result<(), rusqlite::Error> {
        conn.execute_batch("PRAGMA busy_timeout=5000;")?;
        conn.execute_batch("PRAGMA journal_mode=WAL;")?;
        // 初始化 file_generations 表（与 CasStore schema 一致）
        conn.execute_batch(super::cas::CAS_SCHEMA_DDL)?;
        // 初始化 session 表
        init_session_schema(conn)?;
        Ok(())
    }

    /// 获取内部连接的 Mutex 引用（供 daemon_handle_connect / daemon_handle_refresh 使用）
    pub fn conn(&self) -> &Mutex<Connection> {
        &self.conn
    }
}

// ============================================
// 辅助 trait：将 query_row 的"无行"情况转为 Option
// ============================================

/// rusqlite Optional 扩展（等价于 Python 的 fetchone() 返回 None）
///
/// 使用：`conn.query_row(sql, params, |row| ...).optional()`
trait OptionalRow {
    type Item;
    fn optional(self) -> Result<Option<Self::Item>, rusqlite::Error>;
}

impl<T> OptionalRow for Result<T, rusqlite::Error> {
    type Item = T;
    fn optional(self) -> Result<Option<Self::Item>, rusqlite::Error> {
        match self {
            Ok(value) => Ok(Some(value)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e),
        }
    }
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    fn make_session_store() -> SessionStore {
        SessionStore::open_in_memory().unwrap()
    }

    // ---- init_session_schema 测试 ----

    #[test]
    fn test_session_store_open_in_memory_initializes_schema() {
        let store = make_session_store();
        // 表应该存在（查询不报错即表示表存在）
        let conn = store.conn.lock().unwrap();
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM agent_sessions", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 0);
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM workspace_active_session", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 0);
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM file_generations", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 0);
    }

    // ---- daemon_handle_connect 测试 ----

    #[test]
    fn test_daemon_handle_connect_assigns_epoch_1_for_first_session() {
        let store = make_session_store();
        let result = daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();
        assert_eq!(result["session_epoch"], 1);
    }

    #[test]
    fn test_daemon_handle_connect_assigns_increasing_epoch() {
        let store = make_session_store();

        let r1 = daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();
        assert_eq!(r1["session_epoch"], 1);

        let r2 = daemon_handle_connect(1000, 1, "session-2", store.conn()).unwrap();
        assert_eq!(r2["session_epoch"], 2);

        let r3 = daemon_handle_connect(1000, 1, "session-3", store.conn()).unwrap();
        assert_eq!(r3["session_epoch"], 3);
    }

    #[test]
    fn test_daemon_handle_connect_revokes_old_sessions() {
        let store = make_session_store();

        daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();
        daemon_handle_connect(1000, 1, "session-2", store.conn()).unwrap();

        // session-1 应该被 revoked（revoked_at IS NOT NULL）
        let conn = store.conn.lock().unwrap();
        let revoked_at: Option<i64> = conn
            .query_row(
                "SELECT revoked_at FROM agent_sessions
                 WHERE workspace_id = 1 AND session_id = 'session-1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(revoked_at.is_some(), "session-1 应该被 revoked");
    }

    #[test]
    fn test_daemon_handle_connect_updates_workspace_active_session() {
        let store = make_session_store();

        daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();
        {
            let conn = store.conn.lock().unwrap();
            let (sid, epoch): (String, i64) = conn
                .query_row(
                    "SELECT active_session_id, active_session_epoch
                     FROM workspace_active_session WHERE workspace_id = 1",
                    [],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .unwrap();
            assert_eq!(sid, "session-1");
            assert_eq!(epoch, 1);
        }

        daemon_handle_connect(1000, 1, "session-2", store.conn()).unwrap();
        {
            let conn = store.conn.lock().unwrap();
            let (sid, epoch): (String, i64) = conn
                .query_row(
                    "SELECT active_session_id, active_session_epoch
                     FROM workspace_active_session WHERE workspace_id = 1",
                    [],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .unwrap();
            assert_eq!(sid, "session-2");
            assert_eq!(epoch, 2);
        }
    }

    #[test]
    fn test_daemon_handle_connect_resets_file_generations_seq() {
        let store = make_session_store();
        let conn = store.conn.lock().unwrap();

        // 先插入一行 file_generations（模拟之前有活动）
        conn.execute(
            "INSERT INTO file_generations
             (workspace_id, rel_path, latest_session_id, latest_session_epoch,
              latest_seq, latest_seen_generation, latest_committed_generation)
             VALUES (1, 'src/main.rs', 'old-session', 0, 100, '0:100', '0:100')",
            [],
        )
        .unwrap();
        drop(conn);

        daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();

        // file_generations 的 latest_seq 应该被重置为 0，latest_seen_generation 应该为空
        let conn = store.conn.lock().unwrap();
        let (latest_seq, latest_seen_gen): (i64, String) = conn
            .query_row(
                "SELECT latest_seq, latest_seen_generation FROM file_generations
                 WHERE workspace_id = 1 AND rel_path = 'src/main.rs'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(latest_seq, 0, "latest_seq 应该被重置为 0");
        assert!(latest_seen_gen.is_empty(), "latest_seen_generation 应该被清空");
    }

    // ---- daemon_handle_refresh 测试 ----

    fn make_msg(seq: i64, session_id: &str, epoch: i64) -> RefreshMessage {
        RefreshMessage {
            rel_path: "src/main.rs".to_string(),
            agent_session_id: session_id.to_string(),
            monotonic_seq: seq,
            session_epoch: epoch,
            abs_path: None,
        }
    }

    #[test]
    fn test_daemon_handle_refresh_rejects_when_no_active_session() {
        let store = make_session_store();
        let cas_store = super::super::cas::CasStore::open_in_memory().unwrap();

        let msg = make_msg(1, "session-1", 1);
        let result = daemon_handle_refresh(1, &msg, store.conn(), Some(&cas_store));
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.message.contains("no active session"));
    }

    #[test]
    fn test_daemon_handle_refresh_rejects_stale_session() {
        let store = make_session_store();
        let cas_store = super::super::cas::CasStore::open_in_memory().unwrap();

        // 先 connect 分配 epoch=1
        daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();
        // 再 connect 分配 epoch=2（session-1 被 revoked）
        daemon_handle_connect(1000, 1, "session-2", store.conn()).unwrap();

        // 用旧的 session-1 + epoch=1 refresh 应该被拒绝
        let msg = make_msg(1, "session-1", 1);
        let result = daemon_handle_refresh(1, &msg, store.conn(), Some(&cas_store));
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.message.contains("stale session rejected"));
    }

    #[test]
    fn test_daemon_handle_refresh_first_time_seen_committed() {
        let store = make_session_store();
        let cas_store = super::super::cas::CasStore::open_in_memory().unwrap();

        daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();

        // 首次 refresh（epoch=1, seq=1）
        let msg = make_msg(1, "session-1", 1);
        let result = daemon_handle_refresh(1, &msg, store.conn(), Some(&cas_store)).unwrap();
        assert_eq!(result.status, "committed");
        assert_eq!(result.generation, "1:1");
    }

    #[test]
    fn test_daemon_handle_refresh_stale_seq_dropped() {
        let store = make_session_store();
        let cas_store = super::super::cas::CasStore::open_in_memory().unwrap();

        daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();

        // 先 refresh seq=5
        let msg5 = make_msg(5, "session-1", 1);
        daemon_handle_refresh(1, &msg5, store.conn(), Some(&cas_store)).unwrap();

        // 再 refresh seq=3 应该被丢弃（stale seq）
        let msg3 = make_msg(3, "session-1", 1);
        let result = daemon_handle_refresh(1, &msg3, store.conn(), Some(&cas_store)).unwrap();
        assert_eq!(result.status, "stale_seq_dropped");
    }

    #[test]
    fn test_daemon_handle_refresh_accepts_newer_seq() {
        let store = make_session_store();
        let cas_store = super::super::cas::CasStore::open_in_memory().unwrap();

        daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();

        // seq=1
        let msg1 = make_msg(1, "session-1", 1);
        let r1 = daemon_handle_refresh(1, &msg1, store.conn(), Some(&cas_store)).unwrap();
        assert_eq!(r1.status, "committed");
        assert_eq!(r1.generation, "1:1");

        // seq=10
        let msg10 = make_msg(10, "session-1", 1);
        let r10 = daemon_handle_refresh(1, &msg10, store.conn(), Some(&cas_store)).unwrap();
        assert_eq!(r10.status, "committed");
        assert_eq!(r10.generation, "1:10");
    }

    #[test]
    fn test_daemon_handle_refresh_without_cas_store() {
        let store = make_session_store();
        // cas_store=None 时，跳过两阶段 CAS，只做 session epoch 校验
        daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();

        let msg = make_msg(1, "session-1", 1);
        let result = daemon_handle_refresh(1, &msg, store.conn(), None).unwrap();
        assert_eq!(result.status, "committed");
    }

    // ---- Replicator 测试 ----

    fn make_staging_log() -> (tempfile::TempDir, StagingLog) {
        let tmp = tempfile::tempdir().unwrap();
        let log_path = tmp.path().join("staging.log");
        let log = StagingLog::new(log_path.to_str().unwrap()).unwrap();
        (tmp, log)
    }

    #[test]
    fn test_replicator_replicate_no_pending() {
        let (_tmp, log) = make_staging_log();
        let replicator = Replicator::new(&log);

        let result = replicator.replicate("ws1", "", "");
        assert!(result.success);
        assert_eq!(result.workspace_id, "ws1");
        assert_eq!(result.pending_count, 0);
        assert_eq!(result.applied_count, 0);
    }

    #[test]
    fn test_replicator_replicate_applies_pending() {
        let (_tmp, log) = make_staging_log();
        let replicator = Replicator::new(&log);

        // 写入 3 条 pending entries for ws1
        for i in 0..3 {
            let mut e = StagingEntry::new(
                "ws1",
                &format!("file{}.rs", i),
                &format!("hash{}", i),
                "rust",
            );
            log.append(&mut e).unwrap();
        }
        // 写入 1 条 pending entry for ws2
        {
            let mut e = StagingEntry::new("ws2", "file_ws2.rs", "hash_ws2", "rust");
            log.append(&mut e).unwrap();
        }

        let result = replicator.replicate("ws1", "", "");
        assert!(result.success);
        assert_eq!(result.pending_count, 3);
        assert_eq!(result.applied_count, 3);
        assert_eq!(result.applied_lsns.len(), 3);

        // ws1 的 pending 应该都被标记为 applied 并 compact 掉
        let remaining = log.read(0).unwrap();
        assert_eq!(remaining.len(), 1, "应该只剩 ws2 的 1 条 entry");
        assert_eq!(remaining[0].workspace_id, "ws2");
    }

    #[test]
    fn test_replicator_get_pending_count() {
        let (_tmp, log) = make_staging_log();
        let replicator = Replicator::new(&log);

        for i in 0..5 {
            let workspace_id = if i < 3 { "ws1" } else { "ws2" };
            let mut e = StagingEntry::new(
                workspace_id,
                &format!("file{}.rs", i),
                &format!("hash{}", i),
                "rust",
            );
            log.append(&mut e).unwrap();
        }

        // 全量 pending
        assert_eq!(replicator.get_pending_count(None), 5);
        // 按 workspace 过滤
        assert_eq!(replicator.get_pending_count(Some("ws1")), 3);
        assert_eq!(replicator.get_pending_count(Some("ws2")), 2);
    }

    #[test]
    fn test_replicator_recover_calls_replicate() {
        let (_tmp, log) = make_staging_log();
        let replicator = Replicator::new(&log);

        // 写入 2 条 pending entries
        for i in 0..2 {
            let mut e = StagingEntry::new(
                "ws1",
                &format!("file{}.rs", i),
                &format!("hash{}", i),
                "rust",
            );
            log.append(&mut e).unwrap();
        }

        // recover 应该和 replicate 行为一致
        let result = replicator.recover("ws1", "");
        assert!(result.success);
        assert_eq!(result.applied_count, 2);
    }

    // ---- MockSnapshotPublisher 测试 ----

    struct MockSnapshotPublisher {
        call_count: Mutex<i32>,
    }

    impl MockSnapshotPublisher {
        fn new() -> Self {
            Self {
                call_count: Mutex::new(0),
            }
        }

        fn call_count(&self) -> i32 {
            *self.call_count.lock().unwrap()
        }
    }

    impl SnapshotPublisher for MockSnapshotPublisher {
        fn publish_snapshot(
            &self,
            _workspace_instance_id: &str,
            _db_path: &str,
            _build_context_hash: &str,
        ) -> Result<PublishResult, String> {
            *self.call_count.lock().unwrap() += 1;
            Ok(PublishResult { generation: 42 })
        }
    }

    #[test]
    fn test_replicator_with_snapshot_publisher_calls_publish() {
        let (_tmp, log) = make_staging_log();
        let publisher = MockSnapshotPublisher::new();

        // 写入 1 条 pending
        {
            let mut e = StagingEntry::new("ws1", "file1.rs", "hash1", "rust");
            log.append(&mut e).unwrap();
        }

        let replicator = Replicator::new(&log).with_snapshot_publisher(&publisher);
        let result = replicator.replicate("ws1", "/path/to/db", "ctx-hash");

        assert!(result.success);
        assert_eq!(result.generation, 42);
        assert_eq!(publisher.call_count(), 1);
    }

    #[test]
    fn test_replicator_publisher_failure_marks_entries_failed() {
        let (_tmp, log) = make_staging_log();
        struct FailingPublisher;
        impl SnapshotPublisher for FailingPublisher {
            fn publish_snapshot(
                &self,
                _workspace_instance_id: &str,
                _db_path: &str,
                _build_context_hash: &str,
            ) -> Result<PublishResult, String> {
                Err("simulated publish failure".to_string())
            }
        }
        let publisher = FailingPublisher;

        // 写入 1 条 pending
        {
            let mut e = StagingEntry::new("ws1", "file1.rs", "hash1", "rust");
            log.append(&mut e).unwrap();
        }

        let replicator = Replicator::new(&log).with_snapshot_publisher(&publisher);
        let result = replicator.replicate("ws1", "/path/to/db", "");

        assert!(!result.success);
        assert!(result.error.as_ref().unwrap().contains("publish failure"));

        // entry 应该被标记为 failed
        let entries = log.read(0).unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].status, "failed");
        assert!(entries[0].error.as_ref().unwrap().contains("publish failure"));
    }

    // ---- ReplicationResult.summary 测试 ----

    #[test]
    fn test_replication_result_summary_ok() {
        let r = ReplicationResult {
            success: true,
            workspace_id: "ws1".to_string(),
            generation: 5,
            applied_count: 3,
            pending_count: 3,
            ..Default::default()
        };
        let s = r.summary();
        assert!(s.contains("ok"));
        assert!(s.contains("ws1"));
        assert!(s.contains("gen=5"));
        assert!(s.contains("3/3"));
    }

    #[test]
    fn test_replication_result_summary_failed() {
        let r = ReplicationResult {
            success: false,
            workspace_id: "ws1".to_string(),
            ..Default::default()
        };
        let s = r.summary();
        assert!(s.contains("failed"));
    }

    // ---- Arc 共享测试 ----

    #[test]
    fn test_session_store_send_sync() {
        // SessionStore 必须是 Send + Sync（用于跨线程共享）
        fn assert_send_sync<T: Send + Sync>() {}
        assert_send_sync::<SessionStore>();
    }

    #[test]
    fn test_replicator_with_arc_session_store() {
        // 模拟实际使用场景：SessionStore 包装在 Arc 中跨线程共享
        let store = Arc::new(make_session_store());
        let store_clone = store.clone();

        daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();

        // 在另一个 "线程" 中查询
        let _ = std::thread::spawn(move || {
            let conn = store_clone.conn.lock().unwrap();
            let count: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM agent_sessions WHERE workspace_id = 1",
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(count, 1);
        })
        .join();
    }
}
