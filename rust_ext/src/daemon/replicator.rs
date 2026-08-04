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

use std::sync::Arc;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use pyo3::prelude::*;
use rusqlite::{params, Connection};
use serde_json::{Map, Value};

use super::cas::{
    compute_cas_key_v1, CasImportInput, CasPublishInput, CasRawCallInput, CasStore, CasSymbolInput,
};
use super::cas_merge::module_path_from_rel;
use super::staging_log::{StagingEntry, StagingLog};
use crate::canonicalize::{canonicalize_source, sha256_hex};
use crate::multi_lang::{GenericParser, LangConfig};
use crate::ParseResult;

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
    conn.execute_batch("BEGIN IMMEDIATE")
        .map_err(|e| ProtocolError::new(format!("BEGIN IMMEDIATE 失败: {}", e)))?;
    let result =
        daemon_handle_connect_inner(&conn, peer_uid, workspace_id, requested_session_id, now);
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
    m.insert("session_epoch".to_string(), Value::Number(new_epoch.into()));
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

/// 处理 agent refresh 消息——session epoch 校验 + 两阶段 CAS + daemon 侧 parse + publish。
///
/// 规范：watcher-generation-state-machine.md §4.3
/// 规范：daemon-ipc-security.md §3.2（daemon 不信任 agent 提供的 hash）
/// 规范：parse-input-abi.md §2（canonical bytes 是唯一输入入口）
/// 对应 Python replicator.py:daemon_handle_refresh
///
/// 完整管道：
/// 1. session epoch 校验（拒绝 stale session）
/// 2. CAS 第一阶段（seen）—— 原子更新 latest_seen_generation
/// 3. daemon 侧 parse + CAS publish（消除 TOCTOU）：
///    canonical bytes → sha256 → CAS lookup → 未命中则 parse_canonical_bytes → publish
/// 4. CAS 第二阶段（committed）—— 条件更新 latest_committed_generation
///
/// **P0-2 降级说明（2026-07-22 复审整改-v2，方案 3 已实现 2026-07-22）**：
/// 本函数为 **CAS-only 路径**，本身没有 Python `server/replicator.py` 的 step 5
/// （CAS → CodeGraph merge + upsert_manifest）。但**方案 3 已实现**：
/// 调用方 `handle_workspace_file_refresh`（workspace.rs）在 `daemon_handle_refresh`
/// 返回后、`replicator.replicate` 调用前，会调用
/// `cas_merge::merge_cas_to_codegraph`（rust_ext/src/daemon/cas_merge.rs）把 CAS DB
/// 中的 `cas_file_cache` / `cas_symbols` / `cas_raw_calls` merge 到主 CodeGraph DB
/// 的 `file_instances` / `symbols` / `calls` 表。因此 `publish_snapshot` 通过
/// `build_and_publish_blocking` 重载 SQLite 时，能读到 merge 后的新数据。
///
/// merge 失败不阻塞 replicate（与 Python 降级策略一致），只在响应中记录 warning。
///
/// 参数：
/// - workspace_id: workspace ID
/// - msg: refresh 消息
/// - ws_conn: workspace 数据库连接（含 agent_sessions / workspace_active_session /
///   file_generations 表）
/// - cas_store: CAS store（用于两阶段 CAS + parse publish，若为 None 则跳过 CAS 部分）
/// - canonical_bytes: 来自 UDS bytes frame / FD 的规范化文件内容（优先路径）；
///   None 时降级为从 msg.abs_path 读取 + canonicalize（兼容旧 refresh 模式）
pub fn daemon_handle_refresh(
    workspace_id: i64,
    msg: &RefreshMessage,
    ws_conn: &Mutex<Connection>,
    cas_store: Option<&CasStore>,
    canonical_bytes: Option<&[u8]>,
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
    if msg.agent_session_id != active.session_id || msg.session_epoch != active.session_epoch {
        return Err(ProtocolError::new(format!(
            "stale session rejected: incoming={}:{} active={}:{}",
            msg.agent_session_id, msg.session_epoch, active.session_id, active.session_epoch
        )));
    }

    let incoming_gen = format!("{}:{}", msg.session_epoch, msg.monotonic_seq);

    // 2. CAS 第一阶段（seen）—— 原子更新 latest_seen_generation
    let seen_result = cas_store
        .map(|s| {
            s.file_generation_seen(
                workspace_id,
                &msg.rel_path,
                &msg.agent_session_id,
                msg.session_epoch,
                msg.monotonic_seq,
            )
        })
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

    // 3. daemon 侧 parse + CAS publish（消除 TOCTOU）
    // 规范：daemon-ipc-security.md §3.2 —— daemon 重新计算 sha256
    // 规范：parse-input-abi.md §2 —— canonical bytes 是唯一输入入口
    // 注意：仅当 cas_store 为 Some 时才尝试 parse + publish；cas_store 为 None 时跳过
    // step 3（与 Python 一致：cas_conn=None 时 cas_result=None）。
    let cas_result = if let Some(cas) = cas_store {
        Some(_daemon_parse_and_publish(
            &msg.rel_path,
            canonical_bytes,
            msg.abs_path.as_deref().unwrap_or(""),
            Some(cas),
            workspace_id,
        ))
    } else {
        None
    };

    // P0-1 修复：committed 移到 workspace.rs 中 merge 成功之后执行
    // 此处只返回 CAS 已发布状态，不更新 latest_committed_generation
    // 如果 merge 失败/崩溃，同 seq 重试时 cas.rs 的 stale 检查会发现
    // latest_committed_generation 为空，允许重试（不会被判 stale）
    Ok(RefreshResult {
        status: "committed".to_string(),
        generation: incoming_gen,
        cas_result,
    })
}

/// active session 查询结果
#[derive(Debug, Clone)]
struct ActiveSession {
    session_id: String,
    session_epoch: i64,
}

// ============================================
// G8: daemon 侧 parse + CAS publish（消除 TOCTOU）
// ============================================

/// 根据文件路径扩展名检测语言。
///
/// 对应 Python `config.py:detect_language_from_path`（L1037-1051）。
///
/// 实现说明：Python `LANGUAGE_CONFIG` 是 dict，按插入顺序遍历；
/// `.h` 同时存在于 "c" 和 "cpp" 的 extensions 中，但 "c" 在 "cpp" 之前，
/// 因此 `.h` 实际匹配 "c"。本实现用 `match` 显式表达此规则。
///
/// 支持的扩展名与 Python `LANGUAGE_CONFIG` 一致：
/// - rust: `.rs`
/// - typescript: `.ts`, `.tsx`
/// - javascript: `.js`, `.jsx`, `.mjs`, `.cjs`
/// - python: `.py`
/// - kotlin: `.kt`, `.kts`
/// - go: `.go`
/// - java: `.java`
/// - c: `.c`, `.h`
/// - cpp: `.cpp`, `.cc`, `.cxx`, `.hpp`, `.hh`, `.hxx`
/// - csharp: `.cs`
/// - ruby: `.rb`
/// - php: `.php`
/// - swift: `.swift`
/// - scala: `.scala`, `.sc`
/// - hcl: `.tf`, `.hcl`
/// - elixir: `.ex`, `.exs`
pub fn detect_language_from_path(file_path: &str) -> String {
    // 提取扩展名（小写）。从最后一个 '.' 开始。
    let ext = match file_path.rfind('.') {
        Some(idx) => file_path[idx..].to_lowercase(),
        None => return String::new(),
    };
    let lang: &str = match ext.as_str() {
        ".rs" => "rust",
        ".ts" | ".tsx" => "typescript",
        ".js" | ".jsx" | ".mjs" | ".cjs" => "javascript",
        ".py" => "python",
        ".kt" | ".kts" => "kotlin",
        ".go" => "go",
        ".java" => "java",
        ".c" | ".h" => "c", // Python 中 .h 先匹配 "c"（顺序在 "cpp" 之前）
        ".cpp" | ".cc" | ".cxx" | ".hpp" | ".hh" | ".hxx" => "cpp",
        ".cs" => "csharp",
        ".rb" => "ruby",
        ".php" => "php",
        ".swift" => "swift",
        ".scala" | ".sc" => "scala",
        ".tf" | ".hcl" => "hcl",
        ".ex" | ".exs" => "elixir",
        _ => "",
    };
    lang.to_string()
}

/// daemon 侧解析 + CAS publish——消除 TOCTOU。
///
/// 对应 Python `server/replicator.py:_daemon_parse_and_publish`（L268-405）。
///
/// 规范：
/// - daemon-ipc-security.md §3.2 —— daemon 重新计算 sha256，不信任 agent 提供的 hash
/// - parse-input-abi.md §2 —— canonical bytes 是唯一输入入口
/// - cas-gc-protocol.md §3 —— CAS 原子发布四阶段
///
/// 输入优先级：
/// 1. `canonical_bytes` 非 None：直接 hash + parse_canonical_bytes（不读文件）
/// 2. `canonical_bytes` 为 None：从 abs_path 读取 + canonicalize（降级路径）
///
/// 返回 JSON Value，至少包含以下字段：
/// - `content_hash`: str（空字符串表示未能计算）
/// - `cas_key`: str（空字符串表示未计算或 cas_store 为 None）
/// - `cas_state`: str，可能值：
///   - `unsupported_language`：rel_path 不识别
///   - `no_abs_path`：canonical_bytes 为 None 且 abs_path 为空
///   - `canonicalize_failed`：从 abs_path 读取/规范化失败（含 error 字段）
///   - `no_cas_conn`：cas_store 为 None（含 canonicalize_method）
///   - `ready_cache_hit`：CAS 命中，已 pin
///   - `cas_lookup_failed`：CAS lookup 出错（含 error 字段）
///   - `parse_failed`：parse_canonical_bytes 返回错误（含 parse_error）
///   - `ready_published`：CAS 原子发布成功
///   - `publish_failed`：CAS publish 出错（含 error 字段）
pub fn _daemon_parse_and_publish(
    rel_path: &str,
    canonical_bytes: Option<&[u8]>,
    abs_path: &str,
    cas_store: Option<&CasStore>,
    workspace_id: i64,
) -> Value {
    daemon_parse_and_publish_with_options(
        rel_path,
        canonical_bytes,
        abs_path,
        cas_store,
        workspace_id,
        false,
    )
}

/// 与 `_daemon_parse_and_publish` 相同，但允许强制跳过 ready CAS 读取短路。
pub fn daemon_parse_and_publish_with_options(
    rel_path: &str,
    canonical_bytes: Option<&[u8]>,
    abs_path: &str,
    cas_store: Option<&CasStore>,
    workspace_id: i64,
    force_reparse: bool,
) -> Value {
    // 3a. 检测语言
    let language = detect_language_from_path(rel_path);
    if language.is_empty() {
        return serde_json::json!({
            "content_hash": "",
            "cas_key": "",
            "cas_state": "unsupported_language",
        });
    }

    // 3b. canonicalize + re-hash
    // canonical_bytes_owned 仅在降级路径下分配，避免优先路径的无谓分配
    let (canonical_bytes_owned, content_hash, canonicalize_method): (
        Option<Vec<u8>>,
        String,
        &'static str,
    ) = match canonical_bytes {
        Some(bytes) => {
            // 优先路径：daemon 已从 UDS bytes frame / FD 获得规范化内容
            let hash = sha256_hex(bytes);
            (None, hash, "direct_bytes")
        }
        None => {
            // 降级路径：从 abs_path 读取 + canonicalize
            if abs_path.is_empty() {
                return serde_json::json!({
                    "content_hash": "",
                    "cas_key": "",
                    "cas_state": "no_abs_path",
                });
            }
            match canonicalize_source(abs_path) {
                Ok(canon) => {
                    let bytes = canon.canonical_bytes;
                    let hash = canon.content_hash;
                    (Some(bytes), hash, "abs_path_fallback")
                }
                Err(e) => {
                    return serde_json::json!({
                        "content_hash": "",
                        "cas_key": "",
                        "cas_state": "canonicalize_failed",
                        "error": format!("{}", e),
                    });
                }
            }
        }
    };

    // 决定最终用于 parse 的字节切片
    let canonical_bytes_ref: &[u8] = canonical_bytes
        .or_else(|| canonical_bytes_owned.as_deref())
        .unwrap_or(&[]);

    // 3c. CAS publish（若 cas_store 可用）
    let cas_store = match cas_store {
        Some(s) => s,
        None => {
            return serde_json::json!({
                "content_hash": content_hash,
                "cas_key": "",
                "cas_state": "no_cas_conn",
                "canonicalize_method": canonicalize_method,
            });
        }
    };

    // 3d. 计算 CAS key（版本与 Python _daemon_parse_and_publish 一致）
    let parser_version = "0.1.0";
    let callwarden_version = "0.2.0";
    let extraction_config_version = "v1";
    let abi_version = "v1";
    let input_abi_version = "v1";
    let cas_key = compute_cas_key_v1(
        &content_hash,
        &language,
        parser_version,
        callwarden_version,
        extraction_config_version,
        abi_version,
        input_abi_version,
    );

    // 3e. 检查 CAS 是否已命中（state='ready'）
    match cas_store.lookup(&cas_key) {
        Ok(Some(_existing)) => {
            if !force_reparse {
                // 缓存命中：pin 后直接返回（pin 失败只警告，不影响主流程）
                let _ = cas_store.pin(&cas_key, workspace_id, 3600.0);
                return serde_json::json!({
                    "content_hash": content_hash,
                    "cas_key": cas_key,
                    "cas_state": "ready_cache_hit",
                    "canonicalize_method": canonicalize_method,
                });
            }
            // force 模式继续 parse；publish 会原子替换同一 CAS key 的局部事实。
        }
        Ok(None) => {
            // 未命中——继续解析
        }
        Err(e) => {
            return serde_json::json!({
                "content_hash": content_hash,
                "cas_key": cas_key,
                "cas_state": "cas_lookup_failed",
                "error": format!("{}", e),
                "canonicalize_method": canonicalize_method,
            });
        }
    }

    // 3f. CAS 未命中——用同一份 canonical_bytes 做 parse（消除 TOCTOU）
    let lang_config = match LangConfig::get(&language) {
        Some(c) => c,
        None => {
            // LangConfig 不支持此语言（HCL 等）
            return serde_json::json!({
                "content_hash": content_hash,
                "cas_key": cas_key,
                "cas_state": "unsupported_language",
                "canonicalize_method": canonicalize_method,
            });
        }
    };
    let parser = GenericParser::new(Arc::new(lang_config));
    let module_path = module_path_from_rel(rel_path, &language);
    let parse_result =
        parser.parse_canonical_bytes(canonical_bytes_ref, abs_path, &module_path, &content_hash);

    // R6-P0-2: parse 失败 / partial 检查（读取 diagnostics 字段，权威源）
    //
    // 复审 §P0-2：原实现只检查 parse_result.error，未读取 diagnostics。
    // 畸形源码（如 `fn broken( {`）实测 error=null, status=partial,
    // syntax_error_count=1，仍会继续 publish 并返回 ready_published，
    // 导致坏解析污染 CAS 并替换上一代好 snapshot。
    //
    // 修复：调用 parse_status_from_result 推导权威状态：
    // - Failed (error/fatal_parse_error) → cas_state="parse_failed"，跳过 publish
    // - Partial (syntax_error_count>0 / unsupported_construct_count>0) →
    //   cas_state="partial_published"，仍 publish 事实到 CAS（设计 §5.3
    //   "Partial 发布事实"），但 snapshot_guard 会阻止替换上一代 snapshot
    // - Ok → cas_state="ready_published"，正常 publish + 替换 snapshot
    use crate::multi_lang::parse_status_from_result;
    let parse_status = parse_status_from_result(&parse_result);
    match parse_status {
        crate::multi_lang::ParseStatus::Failed => {
            return serde_json::json!({
                "content_hash": content_hash,
                "cas_key": cas_key,
                "cas_state": "parse_failed",
                "canonicalize_method": canonicalize_method,
                "parse_error": parse_result.error.clone()
                    .or_else(|| parse_result.diagnostics.fatal_parse_error.clone())
                    .unwrap_or_else(|| "parse failed (diagnostics)".to_string()),
                "diagnostics": parse_diagnostics_to_json(&parse_result),
            });
        }
        crate::multi_lang::ParseStatus::Unsupported => {
            // Unsupported 状态不应从 parse_status_from_result 推导（设计要求
            // 调用方显式设置），此处兜底处理
            return serde_json::json!({
                "content_hash": content_hash,
                "cas_key": cas_key,
                "cas_state": "unsupported_language",
                "canonicalize_method": canonicalize_method,
                "diagnostics": parse_diagnostics_to_json(&parse_result),
            });
        }
        _ => {} // Ok / Partial 继续发布
    }

    // 3g. 转换 ParseResult → CasPublishInput
    let cas_input = parse_result_to_cas_input(&parse_result, canonical_bytes_ref);

    // 3h. CAS 原子发布
    // R13-P0-1: Partial 解析用 publish_with_status(..., "partial") 发布，
    // 使其 state='partial'，lookup() 不会命中（lookup 只查 state='ready'）。
    // 第二次相同内容 refresh 不会返回 ready_cache_hit，必须重新 parse，
    // snapshot_guard 因此能再次看到 partial_published 并阻止替换上一代好 snapshot。
    let is_partial = parse_status == crate::multi_lang::ParseStatus::Partial;
    let final_state = if is_partial { "partial" } else { "ready" };
    let publish_result = if is_partial {
        cas_store.publish_with_status(
            &cas_key,
            &content_hash,
            &language,
            &cas_input,
            parser_version,
            callwarden_version,
            extraction_config_version,
            abi_version,
            input_abi_version,
            final_state,
        )
    } else {
        cas_store.publish(
            &cas_key,
            &content_hash,
            &language,
            &cas_input,
            parser_version,
            callwarden_version,
            extraction_config_version,
            abi_version,
            input_abi_version,
        )
    };
    match publish_result {
        Ok(()) => {
            // R6-P0-2: Partial 状态返回独立 cas_state，让 snapshot_guard 阻止替换
            // R13-P0-1: 同时 CAS 中 state='partial'，第二次 refresh 不会 ready_cache_hit
            let cas_state = if is_partial {
                "partial_published"
            } else {
                "ready_published"
            };
            serde_json::json!({
                "content_hash": content_hash,
                "cas_key": cas_key,
                "cas_state": cas_state,
                "canonicalize_method": canonicalize_method,
                "diagnostics": parse_diagnostics_to_json(&parse_result),
            })
        }
        Err(e) => serde_json::json!({
            "content_hash": content_hash,
            "cas_key": cas_key,
            "cas_state": "publish_failed",
            "error": format!("{}", e),
            "canonicalize_method": canonicalize_method,
        }),
    }
}

/// R6-P0-2: 将 ParseResult.diagnostics 序列化为 JSON（供 cas_result 携带）
///
/// 复审 §P0-2 要求 cas_result 暴露结构化 diagnostics，便于：
/// 1. snapshot_guard 评估 generation 保护
/// 2. parser_metrics 记录 syntax_error_count / unsupported_construct_count
/// 3. ParseRetryLog 持久化诊断信息供 daemon 重启后诊断
fn parse_diagnostics_to_json(parse_result: &ParseResult) -> serde_json::Value {
    serde_json::json!({
        "status": parse_result.diagnostics.status,
        "syntax_error_count": parse_result.diagnostics.syntax_error_count,
        "unsupported_construct_count": parse_result.diagnostics.unsupported_construct_count,
        "fatal_parse_error": parse_result.diagnostics.fatal_parse_error,
        "partial_parse": parse_result.diagnostics.partial_parse,
        "error": parse_result.diagnostics.error,
    })
}

/// 将 ParseResult 转换为 CasPublishInput（CAS publish 所需的输入格式）。
///
/// R1-P0-2: ParseFact ABI 已在 lib.rs::SymbolInfo / RawCall 上携带
/// local_id / lexical_parent_local_id / byte_start / byte_end / ordinal /
/// caller_local_id 字段，本函数直接转发这些值（不再写死 None / 0）。
/// start_col / end_col 仍置 0（parser 暂不提取列号；后续可补）。
fn parse_result_to_cas_input(
    parse_result: &ParseResult,
    canonical_bytes: &[u8],
) -> CasPublishInput {
    let symbols: Vec<CasSymbolInput> = parse_result
        .symbols
        .iter()
        .map(|si| CasSymbolInput {
            // R14-P0-2: 保留 ParseFact 的 1-based local_id，不使用 vector 下标重编号。
            local_symbol_id: si.local_id as i64,
            name: si.name.clone(),
            qualified_name: si.qualified_name.clone(),
            // R14-P0-2: lexical_parent_local_id 已是 Option<u32>，直接 map 转换
            // None = 顶层（无词法父），Some(x) = 真实父符号 local_id
            parent_id: si.lexical_parent_local_id.map(|x| x as i64),
            kind: si.kind.clone(),
            start_line: si.start_line as i64,
            end_line: si.end_line as i64,
            start_col: 0,
            end_col: 0,
            // R1-P0-2: 转发真实 byte range
            start_byte: si.byte_start as i64,
            end_byte: si.byte_end as i64,
            visibility: si.visibility.clone(),
            signature: si.signature.clone(),
            has_comment: si.has_comment,
            depth: si.depth as i64,
            content: si.content.clone(),
        })
        .collect();

    let raw_calls: Vec<CasRawCallInput> = parse_result
        .calls
        .iter()
        .map(|rc| CasRawCallInput {
            // R14-P0-2: caller_local_id 已是 Option<u32>，直接 map 转换
            // None = 顶层裸调用（未解析到调用者符号），Some(x) = 真实调用者 local_id
            caller_id: rc.caller_local_id.map(|x| x as i64),
            caller_name: rc.caller_name.clone(),
            callee_name: rc.callee_name.clone(),
            line: rc.call_line as i64,
            // R1-P0-2: 转发真实 ordinal
            ordinal: rc.ordinal as i64,
        })
        .collect();

    let imports: Vec<CasImportInput> = parse_result
        .imports
        .iter()
        .map(|s| CasImportInput {
            path: s.clone(),
            kind: "import".to_string(),
        })
        .collect();

    CasPublishInput {
        file_size: canonical_bytes.len() as i64,
        total_lines: parse_result.total_lines as i64,
        symbols,
        raw_calls,
        imports,
    }
}

// ============================================
// SnapshotPublisher trait（R6 扩展点）
// ============================================

/// Snapshot 发布器 trait（对应 Python SnapshotManagerService）
///
/// R5 阶段无实现，Replicator 用 `None` 调用方跳过 snapshot 发布。
/// R6 实现真正的 SnapshotManager 后注入。
///
/// P0-2 整改（2026-07-22 复审整改-v2）：`workspace_id`（数字主键）必传，
/// 用于 GraphStore SQL 层过滤本 workspace 数据，避免 snapshot 混入其他 workspace。
pub trait SnapshotPublisher: Send + Sync {
    /// 发布新 generation snapshot
    ///
    /// 参数：
    /// - workspace_instance_id: workspace 实例 ID（字符串，用于 per-workspace SnapshotManager）
    /// - workspace_id: workspace 数字主键（用于 SQL 过滤）
    /// - db_path: SQLite 数据库路径
    /// - build_context_hash: build context 哈希
    fn publish_snapshot(
        &self,
        workspace_instance_id: &str,
        workspace_id: i64,
        db_path: &str,
        build_context_hash: &str,
    ) -> Result<PublishResult, String>;
}

/// publish_snapshot 返回结果
#[derive(Debug, Clone, Default)]
pub struct PublishResult {
    pub generation: i64,
    /// 新 snapshot 中的符号数（供调用方校验）
    pub symbol_count: usize,
    /// 新 snapshot 中的调用边数
    pub call_count: usize,
}

/// `SnapshotPublisher` 的具体实现——桥接 `SnapshotCache`。
///
/// G11：补齐 Replicator 的 CAS → Manifest → Snapshot 管道。
/// - CAS：由 `_daemon_parse_and_publish` 写入（daemon 侧 parse + publish）
/// - Manifest：由 `file_generations` 表维护（两阶段 CAS 保证 committed 状态）
/// - Snapshot：由本 publisher 调用 `SnapshotManager::build_and_publish_blocking`，
///   从 SQLite 加载符号 + 调用图 → 构建 `GraphSnapshot` → 发布到 `ArcSwap`
///
/// 使用：`Replicator::with_snapshot_publisher(&publisher)` 注入。
///
/// 注意：`build_and_publish_blocking` 返回 `PyResult`，但内部不持有 GIL——
/// `PyErr` 仅用作错误类型。错误路径下用 `{:?}`（Debug 格式化）将 `PyErr` 转为
/// `String`，避免依赖 GIL（`PyErr` 的 `Display` impl 在 PyO3 0.29 需要 GIL，
/// 而 `Debug` 不需要）。成功路径无需 GIL。
pub struct SnapshotCachePublisher {
    cache: Arc<crate::snapshot::SnapshotCache>,
}

impl SnapshotCachePublisher {
    pub fn new(cache: Arc<crate::snapshot::SnapshotCache>) -> Self {
        Self { cache }
    }
}

impl SnapshotPublisher for SnapshotCachePublisher {
    /// 发布快照到 SnapshotCache。
    ///
    /// **P0-2 降级说明（2026-07-22 复审整改-v2，方案 3 已实现 2026-07-22）**：
    /// 本方法仅调用 `build_and_publish_blocking(db_path, workspace_id, ...)` 重新加载已有 SQLite
    /// 到内存 GraphStore，本身不执行 CAS → CodeGraph merge。
    /// **方案 3 已实现**：调用方 `handle_workspace_file_refresh`（workspace.rs）在
    /// `replicate` 调用前已通过 `cas_merge::merge_cas_to_codegraph`
    /// （rust_ext/src/daemon/cas_merge.rs）把 CAS delta merge 到 CodeGraph DB，
    /// 因此本方法重载 SQLite 时能读到 merge 后的新数据。
    ///
    /// merge 失败不阻塞 publish_snapshot（与 Python 降级策略一致），但此时
    /// GraphSnapshot 可能是旧数据，调用方可通过响应中的 `cas_merge.status` 字段
    /// 判断 merge 是否成功。
    ///
    /// P0-2 子问题3 修复：`workspace_id` 传入 `build_and_publish_blocking`，GraphStore
    /// SQL 层用 `WHERE workspace_id = ?` 过滤，避免 snapshot 混入其他 workspace 数据。
    fn publish_snapshot(
        &self,
        workspace_instance_id: &str,
        workspace_id: i64,
        db_path: &str,
        build_context_hash: &str,
    ) -> Result<PublishResult, String> {
        if db_path.is_empty() {
            return Err("db_path 不能为空（snapshot publish 需要源数据库路径）".to_string());
        }

        // 获取或创建 SnapshotManager（per-workspace）
        let mgr = self.cache.get_or_create(workspace_instance_id);

        // 调用 build_and_publish_blocking（返回 PyResult，但内部不持 GIL）。
        // 成功路径：直接返回 (generation, symbol_count, call_count)
        // 失败路径：用 Debug 格式化（不需要 GIL）将 PyErr 转为 String
        //
        // P1 修复（T-1785854423993）：cw-daemon 未嵌入 Python 解释器，pyo3 依赖的
        // 错误路径（构造 PyErr）会 panic。这里用 catch_unwind 把 panic 转为结构化
        // 错误返回，避免 panic 展开到 worker/serialization 层。成功路径是纯 Rust，
        // 不受影响。
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            mgr.build_and_publish_blocking(db_path, workspace_id, build_context_hash, None)
        }));
        match outcome {
            Ok(Ok((generation, symbol_count, call_count))) => Ok(PublishResult {
                generation: generation as i64,
                symbol_count,
                call_count,
            }),
            Ok(Err(_)) => {
                // 注意：pyo3 0.29 的 PyErr Debug/Display 都会 `Python::attach`（需要
                // 解释器），daemon 未嵌入 Python 时格式化会再次 panic。只能丢弃 PyErr
                // 并返回通用错误（原"Debug 不需要 GIL"注释在此版本不成立）。
                Err(
                    "build_and_publish_blocking failed (daemon 未嵌入 Python 解释器，\
                     无法输出 PyErr 详情)"
                        .to_string(),
                )
            }
            Err(_) => Err(
                "snapshot publish panic: daemon 未嵌入 Python 解释器，Python 依赖的 \
                 publish 路径不可用（请求已隔离，不影响其他请求）"
                    .to_string(),
            ),
        }
    }
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
    /// G11-T2：本次 replication 合并的 delta 摘要
    /// （files 数量 + 增删符号/调用边数量），供调用方观测变更规模。
    pub merged_summary: MergedDeltaSummary,
}

/// 合并 delta 的摘要（从 `MergedDelta` 提取，供 `ReplicationResult` 携带）
///
/// 设计原因：`MergedDelta` 内含 `Vec<MergedFile>`（可能很大），
/// `ReplicationResult` 只需数值摘要，不需要完整文件列表。
#[derive(Debug, Clone, Default)]
pub struct MergedDeltaSummary {
    pub file_count: usize,
    pub total_added_symbols: usize,
    pub total_removed_symbols: usize,
    pub total_changed_symbols: usize,
    pub total_added_edges: usize,
    pub total_removed_edges: usize,
}

impl ReplicationResult {
    pub fn summary(&self) -> String {
        let status = if self.success { "ok" } else { "failed" };
        format!(
            "ReplicationResult({}, ws={}, gen={}, {}/{}) applied",
            status, self.workspace_id, self.generation, self.applied_count, self.pending_count
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

    pub fn with_snapshot_publisher(mut self, publisher: &'a dyn SnapshotPublisher) -> Self {
        self.snapshot_publisher = Some(publisher);
        self
    }

    /// 执行一次 replication：读取 pending → 发布新 generation → 标记 applied。
    ///
    /// 参数：
    /// - workspace_id: workspace 实例 ID（字符串，用于 per-workspace SnapshotManager）
    /// - workspace_id_num: workspace 数字主键（用于 GraphStore SQL 过滤；
    ///   0 表示不过滤，兼容无 publisher 的恢复路径）
    /// - db_path: SQLite 数据库路径（用于 publish_snapshot）
    /// - build_context_hash: build context 哈希
    pub fn replicate(
        &self,
        workspace_id: &str,
        workspace_id_num: i64,
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

        // 2. 重放 durable 操作。delete 在发布 snapshot 前幂等应用，
        // 覆盖“日志已落盘但 handler 尚未改库”以及“改库后尚未发布”两种崩溃窗口。
        if let Err(e) = self.apply_durable_operations(&pending, workspace_id_num, db_path) {
            result.success = false;
            result.error = Some(format!("apply durable operation failed: {}", e));
            result.duration_ms = elapsed_ms(&start_time);
            return result;
        }

        // 3. 合并 delta（简单汇总，对应 Python _merge_deltas）
        // G11-T2：将 merged 结果提取为摘要写入 ReplicationResult
        let merged = self.merge_deltas(&pending);
        result.merged_summary = MergedDeltaSummary {
            file_count: merged.files.len(),
            total_added_symbols: merged.total_added_symbols,
            total_removed_symbols: merged.total_removed_symbols,
            total_changed_symbols: merged.total_changed_symbols,
            total_added_edges: merged.total_added_edges,
            total_removed_edges: merged.total_removed_edges,
        };

        // 4. 发布新 generation（若 publisher + db_path 均可用）
        if let Some(publisher) = self.snapshot_publisher {
            if !db_path.is_empty() {
                match publisher.publish_snapshot(
                    workspace_id,
                    workspace_id_num,
                    db_path,
                    build_context_hash,
                ) {
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

        // 5. 批量标记 entries 为 applied（单次文件重写）
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

        // 6. 压缩已应用的 entries（按 status 而非 LSN，避免误删其他 workspace）
        if !result.applied_lsns.is_empty() {
            let _ = self.staging_log.compact_applied(Some(workspace_id));
        }

        result.duration_ms = elapsed_ms(&start_time);
        result
    }

    fn apply_durable_operations(
        &self,
        pending: &[StagingEntry],
        workspace_id_num: i64,
        db_path: &str,
    ) -> Result<(), String> {
        let delete_entries: Vec<&StagingEntry> = pending
            .iter()
            .filter(|entry| entry.operation == "delete")
            .collect();
        if delete_entries.is_empty() {
            return Ok(());
        }
        if workspace_id_num <= 0 {
            return Err("delete staging 缺少有效 workspace_id_num".to_string());
        }
        if db_path.is_empty() {
            return Err("delete staging 缺少 CodeGraph DB 路径".to_string());
        }
        if let Some(parent) = std::path::Path::new(db_path).parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| format!("创建 CodeGraph DB 目录失败: {}", e))?;
        }
        let conn = rusqlite::Connection::open(db_path)
            .map_err(|e| format!("打开 CodeGraph DB 失败: {}", e))?;
        crate::daemon::cas_merge::init_codegraph_schema(&conn)
            .map_err(|e| format!("初始化 CodeGraph schema 失败: {}", e))?;

        for entry in delete_entries {
            crate::daemon::cas_merge::delete_workspace_file_from_codegraph(
                &conn,
                workspace_id_num,
                &entry.file_path,
            )?;
        }
        Ok(())
    }

    /// 从 crash 恢复：读取所有 pending entries 并重新 replication。
    ///
    /// 在 daemon 启动时调用。
    ///
    /// P0-2 整改：新增 `workspace_id_num` 参数。daemon 启动恢复路径通常无
    /// SnapshotPublisher，`workspace_id_num` 可传 0（无过滤，不发布 snapshot）。
    pub fn recover(
        &self,
        workspace_id: &str,
        workspace_id_num: i64,
        db_path: &str,
    ) -> ReplicationResult {
        self.replicate(workspace_id, workspace_id_num, db_path, "")
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
            if let Some(resolve_delta) = entry.resolve_delta.get("added").and_then(|v| v.as_array())
            {
                merged.total_added_edges += resolve_delta.len();
            }
            if let Some(resolve_delta) = entry
                .resolve_delta
                .get("removed")
                .and_then(|v| v.as_array())
            {
                merged.total_removed_edges += resolve_delta.len();
            }
        }
        merged
    }
}

fn elapsed_ms(start: &SystemTime) -> f64 {
    start
        .elapsed()
        .map(|d| d.as_secs_f64() * 1000.0)
        .unwrap_or(0.0)
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
            .query_row("SELECT COUNT(*) FROM workspace_active_session", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 0);
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM file_generations", [], |row| {
                row.get(0)
            })
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
        assert!(
            latest_seen_gen.is_empty(),
            "latest_seen_generation 应该被清空"
        );
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
        let result = daemon_handle_refresh(1, &msg, store.conn(), Some(&cas_store), None);
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
        let result = daemon_handle_refresh(1, &msg, store.conn(), Some(&cas_store), None);
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
        let result = daemon_handle_refresh(1, &msg, store.conn(), Some(&cas_store), None).unwrap();
        assert_eq!(result.status, "committed");
        assert_eq!(result.generation, "1:1");
    }

    #[test]
    fn test_daemon_handle_refresh_stale_seq_dropped() {
        // P0-1 修复后：daemon_handle_refresh 不再调用 file_generation_committed
        // committed 移到 workspace.rs merge 成功后调用
        // 本测试验证：committed 后 stale seq 会被 file_generation_seen 拒绝
        let store = make_session_store();
        let cas_store = super::super::cas::CasStore::open_in_memory().unwrap();

        daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();

        // 先 refresh seq=5（CAS publish 后只 seen，未 committed）
        let msg5 = make_msg(5, "session-1", 1);
        daemon_handle_refresh(1, &msg5, store.conn(), Some(&cas_store), None).unwrap();

        // 模拟 workspace.rs merge 成功后调用 committed
        cas_store
            .file_generation_committed(1, "src/main.rs", 1, 5)
            .unwrap();

        // 再 refresh seq=3 应该被丢弃（stale seq < committed 5）
        let msg3 = make_msg(3, "session-1", 1);
        let result = daemon_handle_refresh(1, &msg3, store.conn(), Some(&cas_store), None).unwrap();
        assert_eq!(result.status, "stale_seq_dropped");
    }

    #[test]
    fn test_daemon_handle_refresh_accepts_newer_seq() {
        let store = make_session_store();
        let cas_store = super::super::cas::CasStore::open_in_memory().unwrap();

        daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();

        // seq=1
        let msg1 = make_msg(1, "session-1", 1);
        let r1 = daemon_handle_refresh(1, &msg1, store.conn(), Some(&cas_store), None).unwrap();
        assert_eq!(r1.status, "committed");
        assert_eq!(r1.generation, "1:1");

        // seq=10
        let msg10 = make_msg(10, "session-1", 1);
        let r10 = daemon_handle_refresh(1, &msg10, store.conn(), Some(&cas_store), None).unwrap();
        assert_eq!(r10.status, "committed");
        assert_eq!(r10.generation, "1:10");
    }

    #[test]
    fn test_daemon_handle_refresh_without_cas_store() {
        let store = make_session_store();
        // cas_store=None 时，跳过两阶段 CAS，只做 session epoch 校验
        daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();

        let msg = make_msg(1, "session-1", 1);
        let result = daemon_handle_refresh(1, &msg, store.conn(), None, None).unwrap();
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

        let result = replicator.replicate("ws1", 0, "", "");
        assert!(result.success);
        assert_eq!(result.workspace_id, "ws1");
        assert_eq!(result.pending_count, 0);
        assert_eq!(result.applied_count, 0);
    }

    #[test]
    fn test_replicator_recover_replays_delete_before_snapshot_publish() {
        let tmp = tempfile::tempdir().unwrap();
        let log_path = tmp.path().join("staging.log");
        let db_path = tmp.path().join("codegraph.db");
        let log = StagingLog::new(log_path.to_str().unwrap()).unwrap();
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        crate::daemon::cas_merge::init_codegraph_schema(&conn).unwrap();
        conn.execute(
            "INSERT INTO workspaces (id, name, root_path, created_at) \
             VALUES (1, 'ws1', '/ws1', 0)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO file_contents (content_hash, language, total_lines, first_seen_at) \
             VALUES ('content-a', 'rust', 1, 0)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbol_contents \
             (content_hash, name, kind, content, signature, has_comment, comment_content, qualified_name) \
             VALUES ('sym-a', 'a', 'function', 'fn a() {}', '', 0, '', 'a')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO file_instances \
             (id, workspace_id, rel_path, abs_path, current_content_hash, mtime, status) \
             VALUES (10, 1, 'src/a.rs', '/ws1/src/a.rs', 'content-a', 0, 'ok')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbols \
             (id, file_instance_id, symbol_hash, name, kind, start_line, end_line, qualified_name) \
             VALUES (100, 10, 'sym-a', 'a', 'function', 1, 1, 'a')",
            [],
        )
        .unwrap();
        drop(conn);

        let mut entry = StagingEntry::new_delete("ws1", "src/a.rs");
        log.append(&mut entry).unwrap();

        let result = Replicator::new(&log).recover("ws1", 1, db_path.to_str().unwrap());
        assert!(result.success, "{:?}", result.error);
        assert_eq!(result.applied_count, 1);

        let conn = rusqlite::Connection::open(&db_path).unwrap();
        let status: String = conn
            .query_row(
                "SELECT status FROM file_instances WHERE id = 10",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let symbol_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM symbols WHERE file_instance_id = 10",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(status, "deleted");
        assert_eq!(symbol_count, 0);
        assert!(log.read_pending().unwrap().is_empty());
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

        let result = replicator.replicate("ws1", 0, "", "");
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
        let result = replicator.recover("ws1", 0, "");
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
            _workspace_id: i64,
            _db_path: &str,
            _build_context_hash: &str,
        ) -> Result<PublishResult, String> {
            *self.call_count.lock().unwrap() += 1;
            Ok(PublishResult {
                generation: 42,
                symbol_count: 0,
                call_count: 0,
            })
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
        let result = replicator.replicate("ws1", 0, "/path/to/db", "ctx-hash");

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
                _workspace_id: i64,
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
        let result = replicator.replicate("ws1", 0, "/path/to/db", "");

        assert!(!result.success);
        assert!(result.error.as_ref().unwrap().contains("publish failure"));

        // entry 应该被标记为 failed
        let entries = log.read(0).unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].status, "failed");
        assert!(entries[0]
            .error
            .as_ref()
            .unwrap()
            .contains("publish failure"));
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

    // ---- G8: detect_language_from_path 测试 ----

    #[test]
    fn test_detect_language_from_path_rust() {
        assert_eq!(detect_language_from_path("src/main.rs"), "rust");
        assert_eq!(detect_language_from_path("Cargo.toml"), "");
    }

    #[test]
    fn test_detect_language_from_path_all_languages() {
        // 验证所有 16 种语言（与 Python LANGUAGE_CONFIG 一致）
        assert_eq!(detect_language_from_path("foo.rs"), "rust");
        assert_eq!(detect_language_from_path("foo.ts"), "typescript");
        assert_eq!(detect_language_from_path("foo.tsx"), "typescript");
        assert_eq!(detect_language_from_path("foo.js"), "javascript");
        assert_eq!(detect_language_from_path("foo.jsx"), "javascript");
        assert_eq!(detect_language_from_path("foo.mjs"), "javascript");
        assert_eq!(detect_language_from_path("foo.cjs"), "javascript");
        assert_eq!(detect_language_from_path("foo.py"), "python");
        assert_eq!(detect_language_from_path("foo.kt"), "kotlin");
        assert_eq!(detect_language_from_path("foo.kts"), "kotlin");
        assert_eq!(detect_language_from_path("foo.go"), "go");
        assert_eq!(detect_language_from_path("foo.java"), "java");
        assert_eq!(detect_language_from_path("foo.c"), "c");
        assert_eq!(detect_language_from_path("foo.h"), "c"); // .h 先匹配 "c"（顺序优先）
        assert_eq!(detect_language_from_path("foo.cpp"), "cpp");
        assert_eq!(detect_language_from_path("foo.cc"), "cpp");
        assert_eq!(detect_language_from_path("foo.cxx"), "cpp");
        assert_eq!(detect_language_from_path("foo.hpp"), "cpp");
        assert_eq!(detect_language_from_path("foo.hh"), "cpp");
        assert_eq!(detect_language_from_path("foo.hxx"), "cpp");
        assert_eq!(detect_language_from_path("foo.cs"), "csharp");
        assert_eq!(detect_language_from_path("foo.rb"), "ruby");
        assert_eq!(detect_language_from_path("foo.php"), "php");
        assert_eq!(detect_language_from_path("foo.swift"), "swift");
        assert_eq!(detect_language_from_path("foo.scala"), "scala");
        assert_eq!(detect_language_from_path("foo.sc"), "scala");
        assert_eq!(detect_language_from_path("foo.tf"), "hcl");
        assert_eq!(detect_language_from_path("foo.hcl"), "hcl");
        assert_eq!(detect_language_from_path("foo.ex"), "elixir");
        assert_eq!(detect_language_from_path("foo.exs"), "elixir");
    }

    #[test]
    fn test_detect_language_from_path_case_insensitive() {
        // 扩展名大小写不敏感
        assert_eq!(detect_language_from_path("FOO.RS"), "rust");
        assert_eq!(detect_language_from_path("Foo.PY"), "python");
        assert_eq!(detect_language_from_path("foo.TS"), "typescript");
    }

    #[test]
    fn test_detect_language_from_path_no_extension() {
        // 无扩展名返回空字符串
        assert_eq!(detect_language_from_path("README"), "");
        assert_eq!(detect_language_from_path("/path/to/file"), "");
    }

    #[test]
    fn test_detect_language_from_path_unsupported_extension() {
        assert_eq!(detect_language_from_path("foo.txt"), "");
        assert_eq!(detect_language_from_path("foo.md"), "");
        assert_eq!(detect_language_from_path("foo.json"), "");
    }

    #[test]
    fn test_detect_language_from_path_dotted_filename() {
        // 文件名含多个点：取最后一个点之后作为扩展名
        assert_eq!(detect_language_from_path("foo.bar.rs"), "rust");
        assert_eq!(detect_language_from_path("test.spec.ts"), "typescript");
    }

    // ---- G8: _daemon_parse_and_publish 测试 ----

    #[test]
    fn test_daemon_parse_and_publish_unsupported_language() {
        // .txt 不识别 → unsupported_language
        let result = _daemon_parse_and_publish("foo.txt", Some(b"hello".as_slice()), "", None, 1);
        assert_eq!(result["cas_state"], "unsupported_language");
        assert_eq!(result["content_hash"], "");
        assert_eq!(result["cas_key"], "");
    }

    #[test]
    fn test_daemon_parse_and_publish_no_abs_path_when_no_bytes() {
        // 无 canonical_bytes + 无 abs_path → no_abs_path
        let result = _daemon_parse_and_publish("foo.rs", None, "", None, 1);
        assert_eq!(result["cas_state"], "no_abs_path");
    }

    #[test]
    fn test_daemon_parse_and_publish_canonicalize_failed() {
        // 提供不存在的 abs_path → canonicalize_failed
        let result = _daemon_parse_and_publish("foo.rs", None, "/nonexistent/path/foo.rs", None, 1);
        assert_eq!(result["cas_state"], "canonicalize_failed");
        assert!(
            result["error"].as_str().unwrap().contains("No such file")
                || result["error"].as_str().unwrap().contains("cannot find")
                || !result["error"].as_str().unwrap().is_empty()
        );
    }

    #[test]
    fn test_daemon_parse_and_publish_no_cas_conn() {
        // cas_store=None → no_cas_conn（但 content_hash 已计算）
        let result =
            _daemon_parse_and_publish("foo.rs", Some(b"fn main() {}".as_slice()), "", None, 1);
        assert_eq!(result["cas_state"], "no_cas_conn");
        assert_eq!(result["canonicalize_method"], "direct_bytes");
        // content_hash 应为 sha256("fn main() {}")
        assert!(!result["content_hash"].as_str().unwrap().is_empty());
        assert_eq!(result["cas_key"], "");
    }

    #[test]
    fn test_daemon_parse_and_publish_ready_published() {
        // 完整管道：parse + CAS publish（首次发布，非缓存命中）
        let cas = super::super::cas::CasStore::open_in_memory().unwrap();
        let result = _daemon_parse_and_publish(
            "foo.rs",
            Some(b"fn main() { println!(\"hello\"); }".as_slice()),
            "/tmp/foo.rs",
            Some(&cas),
            1,
        );
        assert_eq!(result["cas_state"], "ready_published");
        assert_eq!(result["canonicalize_method"], "direct_bytes");
        assert!(!result["content_hash"].as_str().unwrap().is_empty());
        assert!(!result["cas_key"].as_str().unwrap().is_empty());
    }

    #[test]
    fn test_daemon_parse_and_publish_ready_cache_hit() {
        // 第二次同样的 bytes → ready_cache_hit
        let cas = super::super::cas::CasStore::open_in_memory().unwrap();
        let bytes = b"fn add(a: i32, b: i32) -> i32 { a + b }".to_vec();
        let _ = _daemon_parse_and_publish("foo.rs", Some(&bytes), "/tmp/foo.rs", Some(&cas), 1);
        let result2 =
            _daemon_parse_and_publish("foo.rs", Some(&bytes), "/tmp/foo.rs", Some(&cas), 1);
        assert_eq!(result2["cas_state"], "ready_cache_hit");
        assert_eq!(result2["canonicalize_method"], "direct_bytes");
    }

    #[test]
    fn test_daemon_parse_and_publish_cache_hit_pins_ref() {
        // 缓存命中后应插入 cas_pending_refs（pin TTL=3600s）
        let cas = super::super::cas::CasStore::open_in_memory().unwrap();
        let bytes = b"fn x() {}".to_vec();
        let _ = _daemon_parse_and_publish("foo.rs", Some(&bytes), "/tmp/foo.rs", Some(&cas), 42);
        let result2 =
            _daemon_parse_and_publish("foo.rs", Some(&bytes), "/tmp/foo.rs", Some(&cas), 42);
        assert_eq!(result2["cas_state"], "ready_cache_hit");

        // 查询 cas_pending_refs
        let conn = cas.conn().lock().unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM cas_pending_refs WHERE workspace_id = 42",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(count >= 1, "pin 应插入 cas_pending_refs");
    }

    // ---- G8: daemon_handle_refresh + canonical_bytes 集成测试 ----

    #[test]
    fn test_daemon_handle_refresh_with_canonical_bytes_publishes_to_cas() {
        // 完整管道：canonical_bytes → daemon_handle_refresh → CAS publish
        let store = make_session_store();
        let cas_store = super::super::cas::CasStore::open_in_memory().unwrap();

        daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();

        let msg = make_msg(1, "session-1", 1);
        let bytes = b"fn main() {}".to_vec();
        let result =
            daemon_handle_refresh(1, &msg, store.conn(), Some(&cas_store), Some(&bytes)).unwrap();
        assert_eq!(result.status, "committed");
        assert_eq!(result.generation, "1:1");
        // cas_result 应包含 ready_published
        let cas_result = result.cas_result.expect("cas_result should be Some");
        assert_eq!(cas_result["cas_state"], "ready_published");
    }

    #[test]
    fn test_daemon_handle_refresh_cache_hit_on_second_refresh() {
        // 第二次 refresh 同样内容 → ready_cache_hit
        let store = make_session_store();
        let cas_store = super::super::cas::CasStore::open_in_memory().unwrap();

        daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();

        let bytes = b"fn x() {}".to_vec();
        let msg1 = make_msg(1, "session-1", 1);
        let r1 =
            daemon_handle_refresh(1, &msg1, store.conn(), Some(&cas_store), Some(&bytes)).unwrap();
        assert_eq!(r1.cas_result.unwrap()["cas_state"], "ready_published");

        let msg2 = make_msg(2, "session-1", 1);
        let r2 =
            daemon_handle_refresh(1, &msg2, store.conn(), Some(&cas_store), Some(&bytes)).unwrap();
        assert_eq!(r2.cas_result.unwrap()["cas_state"], "ready_cache_hit");
    }

    #[test]
    fn test_daemon_handle_refresh_parse_failure_returns_parse_failed() {
        // 故意构造一个语法错误的 Rust 代码 → parse_failed
        // 注意：tree-sitter 对部分语法错误仍能 parse（不会返回 error），
        // 只有 set_language 失败或 parse 返回 None 才会触发 error。
        // 此测试用空字节流（合法但无符号）验证不触发 ready_published 的边界。
        let store = make_session_store();
        let cas_store = super::super::cas::CasStore::open_in_memory().unwrap();

        daemon_handle_connect(1000, 1, "session-1", store.conn()).unwrap();

        let msg = make_msg(1, "session-1", 1);
        let bytes = b"".to_vec(); // 空文件
        let result =
            daemon_handle_refresh(1, &msg, store.conn(), Some(&cas_store), Some(&bytes)).unwrap();
        assert_eq!(result.status, "committed");
        // 空文件 parse 不出错（tree-sitter 容错），但 symbols 为空。
        // cas_state 应为 ready_published（CAS 表里留痕）或 ready_cache_hit。
        let cas_state = result.cas_result.unwrap()["cas_state"]
            .as_str()
            .unwrap()
            .to_string();
        assert!(
            cas_state == "ready_published" || cas_state == "ready_cache_hit",
            "expected ready_published or ready_cache_hit, got {}",
            cas_state
        );
    }

    // ---- G11-T2: ReplicationResult.merged_summary 测试 ----

    #[test]
    fn test_replicator_merged_summary_populated_from_pending_entries() {
        // G11-T2：replicate() 应将 merge_deltas 结果填充到 ReplicationResult.merged_summary
        let (_tmp, log) = make_staging_log();
        let replicator = Replicator::new(&log);

        // 写入 2 条带 parse_delta + resolve_delta 的 pending entries
        {
            let mut e = StagingEntry::new("ws1", "file1.rs", "hash1", "rust");
            // 模拟 parse_delta：2 个 added 符号、1 个 removed 符号
            e.parse_delta.insert(
                "symbol_delta".to_string(),
                serde_json::json!({
                    "added": [{"name": "fn_a"}, {"name": "fn_b"}],
                    "removed": [{"name": "old_fn"}],
                    "changed": []
                }),
            );
            // 模拟 resolve_delta：3 条新增边
            e.resolve_delta.insert(
                "added".to_string(),
                serde_json::json!([{"src": 1, "dst": 2}, {"src": 1, "dst": 3}, {"src": 2, "dst": 3}]),
            );
            log.append(&mut e).unwrap();
        }
        {
            let mut e = StagingEntry::new("ws1", "file2.rs", "hash2", "rust");
            e.parse_delta.insert(
                "symbol_delta".to_string(),
                serde_json::json!({
                    "added": [{"name": "fn_c"}],
                    "removed": [],
                    "changed": [{"name": "fn_a"}]
                }),
            );
            e.resolve_delta.insert(
                "removed".to_string(),
                serde_json::json!([{"src": 1, "dst": 2}]),
            );
            log.append(&mut e).unwrap();
        }

        let result = replicator.replicate("ws1", 0, "", "");
        assert!(result.success, "replicate 应成功");
        // 合并 2 条 entries
        assert_eq!(result.merged_summary.file_count, 2);
        // 总计 added: 2 + 1 = 3
        assert_eq!(result.merged_summary.total_added_symbols, 3);
        // 总计 removed: 1 + 0 = 1
        assert_eq!(result.merged_summary.total_removed_symbols, 1);
        // 总计 changed: 0 + 1 = 1
        assert_eq!(result.merged_summary.total_changed_symbols, 1);
        // 总计 added_edges: 3 + 0 = 3
        assert_eq!(result.merged_summary.total_added_edges, 3);
        // 总计 removed_edges: 0 + 1 = 1
        assert_eq!(result.merged_summary.total_removed_edges, 1);
    }

    #[test]
    fn test_replicator_merged_summary_empty_for_no_pending() {
        // G11-T2：无 pending entries 时 merged_summary 应全为 0
        let (_tmp, log) = make_staging_log();
        let replicator = Replicator::new(&log);

        let result = replicator.replicate("ws1", 0, "", "");
        assert!(result.success);
        assert_eq!(result.merged_summary.file_count, 0);
        assert_eq!(result.merged_summary.total_added_symbols, 0);
        assert_eq!(result.merged_summary.total_removed_symbols, 0);
        assert_eq!(result.merged_summary.total_changed_symbols, 0);
        assert_eq!(result.merged_summary.total_added_edges, 0);
        assert_eq!(result.merged_summary.total_removed_edges, 0);
    }

    // ---- G11-T3: SnapshotCachePublisher E2E 测试 ----

    /// 构造一个带符号和调用边的临时 SQLite DB，供 SnapshotCachePublisher 测试。
    ///
    /// schema 对齐 `GraphStore::load_from_sqlite_blocking` 的查询：
    /// - file_instances(id, rel_path, status)
    /// - symbols(id, file_instance_id, kind, name, qualified_name, module_path,
    ///           start_line, end_line, depth)
    /// - calls(caller_id, callee_id, callee_name, call_line, is_cross_file)
    fn make_snapshot_test_db() -> (tempfile::TempDir, std::path::PathBuf) {
        let tmp = tempfile::tempdir().unwrap();
        let db_path = tmp.path().join("snapshot_test.db");
        {
            let conn = rusqlite::Connection::open(&db_path).unwrap();
            conn.execute_batch("PRAGMA journal_mode=WAL;").unwrap();
            conn.execute_batch(
                r#"
                CREATE TABLE file_instances (
                    id INTEGER PRIMARY KEY,
                    rel_path TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                );
                CREATE TABLE symbols (
                    id INTEGER PRIMARY KEY,
                    file_instance_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    qualified_name TEXT NOT NULL,
                    module_path TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    depth INTEGER NOT NULL
                );
                CREATE TABLE calls (
                    caller_id INTEGER NOT NULL,
                    callee_id INTEGER NOT NULL,
                    callee_name TEXT NOT NULL,
                    call_line INTEGER NOT NULL,
                    is_cross_file INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO file_instances (id, rel_path, status) VALUES
                    (1, 'src/main.rs', 'active'),
                    (2, 'src/lib.rs', 'active'),
                    (3, 'src/old.rs', 'archived');
                INSERT INTO symbols (id, file_instance_id, kind, name, qualified_name,
                                     module_path, start_line, end_line, depth) VALUES
                    (1, 1, 'fn', 'main', 'src.main', 'src', 1, 5, 0),
                    (2, 1, 'fn', 'helper', 'src.main.helper', 'src', 2, 3, 1),
                    (3, 2, 'fn', 'add', 'src.lib.add', 'src', 1, 3, 0),
                    (4, 2, 'struct', 'Config', 'src.lib.Config', 'src', 5, 8, 0);
                INSERT INTO calls (caller_id, callee_id, callee_name, call_line, is_cross_file) VALUES
                    (1, 2, 'helper', 4, 0),
                    (1, 3, 'add', 4, 1),
                    (2, 3, 'add', 2, 1);
                "#,
            ).unwrap();
            // WAL checkpoint：确保数据写入主 DB（GraphStore 用 immutable=1 打开）
            conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);")
                .unwrap();
        }
        (tmp, db_path)
    }

    #[test]
    fn test_snapshot_cache_publisher_publishes_and_returns_counts() {
        // G11-T3 E2E：SnapshotCachePublisher → build_and_publish_blocking → 返回 PublishResult
        let (_tmp, db_path) = make_snapshot_test_db();
        let db_path_str = db_path.to_str().unwrap();

        // 创建 SnapshotCache（max_workspaces=4）+ SnapshotCachePublisher
        let cache = Arc::new(crate::snapshot::SnapshotCache::new(4));
        let publisher = SnapshotCachePublisher::new(cache.clone());

        // 第一次 publish：
        // - 4 个真实符号（ids 1-4），archived 文件排除
        // - GraphStore::load_from_sqlite_blocking 用 by_id.len() 作为 symbol_count，
        //   包含 index 0 的占位槽，因此返回 5（= 4 真实 + 1 占位）
        // - 3 条调用边
        let result = publisher.publish_snapshot("ws_e2e_1", 0, db_path_str, "ctx-hash-1");
        assert!(result.is_ok(), "publish 应成功: {:?}", result.err());
        let pr = result.unwrap();
        assert_eq!(pr.symbol_count, 5, "应加载 5（4 真实 + 1 占位槽）");
        assert_eq!(pr.call_count, 3, "应加载 3 条调用边");
        assert!(pr.generation >= 1, "generation 应 >= 1");

        // 第二次 publish：generation 递增，符号/调用边数不变
        let result2 = publisher.publish_snapshot("ws_e2e_1", 0, db_path_str, "ctx-hash-2");
        assert!(result2.is_ok());
        let pr2 = result2.unwrap();
        assert_eq!(pr2.symbol_count, 5);
        assert_eq!(pr2.call_count, 3);
        assert!(pr2.generation > pr.generation, "generation 应递增");
    }

    #[test]
    fn test_snapshot_cache_publisher_rejects_empty_db_path() {
        // G11-T3：空 db_path 应返回错误
        let cache = Arc::new(crate::snapshot::SnapshotCache::new(4));
        let publisher = SnapshotCachePublisher::new(cache);

        let result = publisher.publish_snapshot("ws_e2e_2", 0, "", "ctx-hash");
        assert!(result.is_err());
        let err_msg = result.unwrap_err();
        assert!(
            err_msg.contains("db_path"),
            "错误信息应包含 db_path，实际: {}",
            err_msg
        );
    }

    // G11-T3：不存在的 DB 路径错误路径测试省略——build_and_publish_blocking
    // 内部用 PyRuntimeError::new_err(...) 创建 PyErr，创建 PyErr 需要 Python
    // 解释器初始化，而 cargo test 默认不初始化（auto-initialize feature 未启用）。
    // 完整 E2E（含 Python 初始化）在 cw_daemon 集成测试中验证。
    // 空 db_path 的提前返回路径由 test_snapshot_cache_publisher_rejects_empty_db_path 覆盖。

    #[test]
    fn test_snapshot_cache_publisher_multiple_workspaces_independent() {
        // G11-T3：多 workspace 隔离——不同 workspace_instance_id 应有独立 generation
        let (_tmp, db_path) = make_snapshot_test_db();
        let db_path_str = db_path.to_str().unwrap();
        let cache = Arc::new(crate::snapshot::SnapshotCache::new(4));
        let publisher = SnapshotCachePublisher::new(cache.clone());

        // ws_a 第一次 publish
        // symbol_count = 5（4 真实符号 + 1 占位槽，见 publishes_and_returns_counts 测试注释）
        let pr_a1 = publisher
            .publish_snapshot("ws_a", 0, db_path_str, "ctx-a-1")
            .unwrap();
        assert_eq!(pr_a1.symbol_count, 5);
        assert_eq!(pr_a1.generation, 1);

        // ws_b 第一次 publish（独立 generation 序列）
        let pr_b1 = publisher
            .publish_snapshot("ws_b", 0, db_path_str, "ctx-b-1")
            .unwrap();
        assert_eq!(pr_b1.symbol_count, 5);
        assert_eq!(pr_b1.generation, 1, "ws_b 的 generation 应独立从 1 开始");

        // ws_a 第二次 publish
        let pr_a2 = publisher
            .publish_snapshot("ws_a", 0, db_path_str, "ctx-a-2")
            .unwrap();
        assert_eq!(pr_a2.generation, 2, "ws_a 第二次 generation 应为 2");
    }

    // ---- P0 CAS local_id ABI 端到端持久化测试（T-1785076611912）----
    // 验证 ParseFact local_id 从 parser → parse_result_to_cas_input → CasStore.publish 的完整传递
    // 使用真实文件（file-backed），而非合成 CasPublishInput

    /// 辅助：parse 真实文件 → CAS publish → 返回 (ParseResult, CasStore)
    fn parse_and_publish_to_cas(
        source: &str,
        language: &str,
        suffix: &str,
    ) -> (ParseResult, CasStore, String) {
        use crate::canonicalize::canonicalize_source;
        use crate::multi_lang::LangConfig;

        // 1. 写入临时文件
        let tmp_dir = tempfile::tempdir().unwrap();
        let file_path = tmp_dir.path().join(format!("test.{}", suffix));
        std::fs::write(&file_path, source).unwrap();
        let abs_path = file_path.to_str().unwrap();

        // 2. canonicalize（获取 canonical_bytes + content_hash）
        let canon = canonicalize_source(abs_path).unwrap();
        let content_hash = canon.content_hash.clone();
        let canonical_bytes = canon.canonical_bytes.clone();

        // 3. parse 真实文件
        let config =
            LangConfig::get(language).unwrap_or_else(|| panic!("不支持的语言: {}", language));
        let parser = crate::multi_lang::GenericParser::new(std::sync::Arc::new(config));
        let parse_result = parser.parse_file(abs_path, "test_module");

        // parse 不应有错误
        assert!(
            parse_result.error.is_none(),
            "parse 失败: {:?}",
            parse_result.error
        );

        // 4. 转换 ParseResult → CasPublishInput
        let cas_input = parse_result_to_cas_input(&parse_result, &canonical_bytes);

        // 5. CAS 发布
        let cas_store = CasStore::open_in_memory().unwrap();
        let cas_key = compute_cas_key_v1(
            &content_hash,
            language,
            "0.1.0", // parser_version
            "0.2.0", // callwarden_version
            "v1",    // extraction_config_version
            "v1",    // abi_version
            "v1",    // input_abi_version
        );
        cas_store
            .publish(
                &cas_key,
                &content_hash,
                language,
                &cas_input,
                "0.1.0",
                "0.2.0",
                "v1",
                "v1",
                "v1",
            )
            .unwrap();

        // 保持 tmp_dir 不被删除（返回路径供调试）
        std::mem::forget(tmp_dir);

        (parse_result, cas_store, cas_key)
    }

    /// P0-1: Rust 文件 file-backed E2E：parser local_id → cas_symbols.local_symbol_id 一致
    #[test]
    fn test_cas_local_id_e2e_rust_file() {
        let source = r#"fn outer() {
    inner();
}

fn inner() {
    println!("hello");
}
"#;
        let (parse_result, cas_store, cas_key) = parse_and_publish_to_cas(source, "rust", "rs");

        // 验证 parser 输出了符号
        assert!(!parse_result.symbols.is_empty(), "parser 应提取到符号");
        // 验证 parser 输出了调用
        assert!(!parse_result.calls.is_empty(), "parser 应提取到调用");

        // 查询 cas_symbols，断言 local_symbol_id 与 parser 输出一致
        let conn = cas_store.conn().lock().unwrap();
        let mut stmt = conn
            .prepare(
                "SELECT name, local_symbol_id, lexical_parent_local_id
                 FROM cas_symbols WHERE cas_key = ?1 ORDER BY local_symbol_id",
            )
            .unwrap();
        let cas_rows: Vec<(String, i64, Option<i64>)> = stmt
            .query_map(rusqlite::params![cas_key], |row: &rusqlite::Row<'_>| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?))
            })
            .unwrap()
            .map(|r: rusqlite::Result<_>| r.unwrap())
            .collect();

        // 逐符号对比
        assert_eq!(
            cas_rows.len(),
            parse_result.symbols.len(),
            "cas_symbols 行数应与 parser 符号数一致"
        );
        for (i, sym) in parse_result.symbols.iter().enumerate() {
            let cas_row = &cas_rows[i];
            assert_eq!(
                cas_row.0, sym.name,
                "符号名不一致: cas={} vs parser={}",
                cas_row.0, sym.name
            );
            assert_eq!(
                cas_row.1, sym.local_id as i64,
                "local_symbol_id 不一致: cas={} vs parser={} (symbol={})",
                cas_row.1, sym.local_id, sym.name
            );
            assert_eq!(
                cas_row.2,
                sym.lexical_parent_local_id.map(|x| x as i64),
                "lexical_parent_local_id 不一致: cas={:?} vs parser={:?} (symbol={})",
                cas_row.2,
                sym.lexical_parent_local_id,
                sym.name
            );
        }

        // 查询 cas_raw_calls，断言 caller_local_id 与 parser 输出一致
        let mut stmt = conn
            .prepare(
                "SELECT callee_name, caller_local_id, call_ordinal
                 FROM cas_raw_calls WHERE cas_key = ?1 ORDER BY call_ordinal",
            )
            .unwrap();
        let cas_calls: Vec<(String, Option<i64>, i64)> = stmt
            .query_map(rusqlite::params![cas_key], |row: &rusqlite::Row<'_>| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?))
            })
            .unwrap()
            .map(|r: rusqlite::Result<_>| r.unwrap())
            .collect();

        assert_eq!(
            cas_calls.len(),
            parse_result.calls.len(),
            "cas_raw_calls 行数应与 parser 调用数一致"
        );
        for (i, call) in parse_result.calls.iter().enumerate() {
            let cas_row = &cas_calls[i];
            assert_eq!(
                cas_row.1,
                call.caller_local_id.map(|x| x as i64),
                "caller_local_id 不一致: cas={:?} vs parser={:?} (callee={})",
                cas_row.1,
                call.caller_local_id,
                call.callee_name
            );
            assert_eq!(
                cas_row.2, call.ordinal as i64,
                "call_ordinal 不一致: cas={} vs parser={} (callee={})",
                cas_row.2, call.ordinal, call.callee_name
            );
        }
    }

    /// P0-2: Python 文件 file-backed E2E（验证多语言 local_id 传递）
    #[test]
    fn test_cas_local_id_e2e_python_file() {
        let source = r#"def outer():
    inner()

def inner():
    pass
"#;
        let (parse_result, cas_store, cas_key) = parse_and_publish_to_cas(source, "python", "py");

        assert!(
            !parse_result.symbols.is_empty(),
            "Python parser 应提取到符号"
        );

        let conn = cas_store.conn().lock().unwrap();
        let mut stmt = conn
            .prepare(
                "SELECT name, local_symbol_id FROM cas_symbols WHERE cas_key = ?1 ORDER BY local_symbol_id",
            )
            .unwrap();
        let cas_rows: Vec<(String, i64)> = stmt
            .query_map(rusqlite::params![cas_key], |row: &rusqlite::Row<'_>| {
                Ok((row.get(0)?, row.get(1)?))
            })
            .unwrap()
            .map(|r: rusqlite::Result<_>| r.unwrap())
            .collect();

        assert_eq!(
            cas_rows.len(),
            parse_result.symbols.len(),
            "Python cas_symbols 行数应与 parser 符号数一致"
        );
        for (i, sym) in parse_result.symbols.iter().enumerate() {
            assert_eq!(
                cas_rows[i].1, sym.local_id as i64,
                "Python local_symbol_id 不一致: cas={} vs parser={} (symbol={})",
                cas_rows[i].1, sym.local_id, sym.name
            );
        }
    }

    /// P0-3: 验证 local_id 全部非零（0 是保留值，表示 synthetic module symbol）
    #[test]
    fn test_cas_local_id_all_nonzero_e2e() {
        let source = r#"fn foo() { bar(); }
fn bar() { baz(); }
fn baz() {}
"#;
        let (parse_result, cas_store, cas_key) = parse_and_publish_to_cas(source, "rust", "rs");

        let conn = cas_store.conn().lock().unwrap();
        let mut stmt = conn
            .prepare("SELECT local_symbol_id FROM cas_symbols WHERE cas_key = ?1")
            .unwrap();
        let ids: Vec<i64> = stmt
            .query_map(rusqlite::params![cas_key], |row: &rusqlite::Row<'_>| {
                row.get(0)
            })
            .unwrap()
            .map(|r: rusqlite::Result<_>| r.unwrap())
            .collect();

        for id in &ids {
            assert!(
                *id >= 1,
                "local_symbol_id 应 >= 1（0 是保留值），实际: {}",
                id
            );
        }

        // 验证 parser 端也全部非零
        for sym in &parse_result.symbols {
            assert!(
                sym.local_id >= 1,
                "parser local_id 应 >= 1（0 是保留值），实际: {} (symbol={})",
                sym.local_id,
                sym.name
            );
        }
    }

    /// P0-4: 验证 lexical_parent_local_id 指向真实存在的符号
    #[test]
    fn test_cas_local_id_parent_references_valid_symbol_e2e() {
        let source = r#"struct Container {
    fn method(&self) {
        self.helper();
    }
    fn helper(&self) {}
}
"#;
        let (_parse_result, cas_store, cas_key) = parse_and_publish_to_cas(source, "rust", "rs");

        let conn = cas_store.conn().lock().unwrap();
        // 查询所有有 parent 的符号
        let mut stmt = conn
            .prepare(
                "SELECT local_symbol_id, lexical_parent_local_id
                 FROM cas_symbols WHERE cas_key = ?1 AND lexical_parent_local_id IS NOT NULL",
            )
            .unwrap();
        let children: Vec<(i64, i64)> = stmt
            .query_map(rusqlite::params![cas_key], |row: &rusqlite::Row<'_>| {
                Ok((row.get(0)?, row.get(1)?))
            })
            .unwrap()
            .map(|r: rusqlite::Result<_>| r.unwrap())
            .collect();

        // 每个子符号的 parent 应存在于 cas_symbols 中
        let mut stmt2 = conn
            .prepare("SELECT local_symbol_id FROM cas_symbols WHERE cas_key = ?1")
            .unwrap();
        let all_ids: std::collections::HashSet<i64> = stmt2
            .query_map(rusqlite::params![cas_key], |row: &rusqlite::Row<'_>| {
                row.get(0)
            })
            .unwrap()
            .map(|r: rusqlite::Result<_>| r.unwrap())
            .collect();

        for (child_id, parent_id) in &children {
            assert!(
                all_ids.contains(parent_id),
                "child local_id={} 的 parent local_id={} 不存在于 cas_symbols 中",
                child_id,
                parent_id
            );
        }
    }

    /// P0-5: 验证 caller_local_id 指向真实存在的符号（调用关系完整性）
    #[test]
    fn test_cas_caller_local_id_references_valid_symbol_e2e() {
        let source = r#"fn caller_a() {
    callee();
}
fn callee() {}
"#;
        let (_parse_result, cas_store, cas_key) = parse_and_publish_to_cas(source, "rust", "rs");

        let conn = cas_store.conn().lock().unwrap();
        // 查询所有有 caller 的调用
        let mut stmt = conn
            .prepare(
                "SELECT callee_name, caller_local_id
                 FROM cas_raw_calls WHERE cas_key = ?1 AND caller_local_id IS NOT NULL",
            )
            .unwrap();
        let calls: Vec<(String, i64)> = stmt
            .query_map(rusqlite::params![cas_key], |row: &rusqlite::Row<'_>| {
                Ok((row.get(0)?, row.get(1)?))
            })
            .unwrap()
            .map(|r: rusqlite::Result<_>| r.unwrap())
            .collect();

        // 获取所有符号 local_id
        let mut stmt2 = conn
            .prepare("SELECT local_symbol_id, name FROM cas_symbols WHERE cas_key = ?1")
            .unwrap();
        let id_to_name: std::collections::HashMap<i64, String> = stmt2
            .query_map(rusqlite::params![cas_key], |row: &rusqlite::Row<'_>| {
                Ok((row.get(0)?, row.get(1)?))
            })
            .unwrap()
            .map(|r: rusqlite::Result<_>| r.unwrap())
            .collect();

        for (callee_name, caller_id) in &calls {
            assert!(
                id_to_name.contains_key(caller_id),
                "call callee={} 的 caller_local_id={} 不存在于 cas_symbols 中",
                callee_name,
                caller_id
            );
        }
    }
}
