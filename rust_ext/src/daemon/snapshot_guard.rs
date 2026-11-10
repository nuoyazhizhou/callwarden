//! P1-F Step 2: 失败 generation 保护 + dirty overlay 隔离
//!
//! 设计文档：docs/design/rust-only-parser-cutover-plan.md §5.3 + §9.3
//!
//! 核心职责：
//! - **失败不替换**：parse failed / unsupported / stale 状态下不替换上一代
//!   可查询 snapshot（设计 §5.3）
//! - **generation CAS**：基于 cas_state 推导是否允许 snapshot 替换
//! - **dirty overlay 隔离**：dirty overlay 路径不进入 Global CAS（设计 §9.3）
//!
//! 与现有 `cas.rs::file_generation_seen/committed` 的关系：
//! - `cas.rs` 提供两阶段 CAS 的原子更新（seen → committed）
//! - 本模块提供**显式**的失败状态判断，供 `handle_workspace_file_refresh`
//!   在调用 `merge_cas_to_codegraph` + `publish_snapshot` 之前检查
//! - 现有代码通过 `cas_state != "ready_published"` 隐式保护，本模块将其
//!   显式化为可观测的 `FailureProtectionResult`，便于 durable log 记录

use serde::{Deserialize, Serialize};

use crate::multi_lang::ParseStatus;

// ============================================
// cas_state 判定
// ============================================

/// 判断给定 cas_state 是否表示 parse 失败（设计 §5.3 `failed`）
///
/// 失败状态包括：
/// - `parse_failed`：parse_canonical_bytes 返回错误
/// - `canonicalize_failed`：从 abs_path 读取/规范化失败
/// - `publish_failed`：CAS publish 出错
/// - `cas_lookup_failed`：CAS lookup 出错
/// - `no_abs_path`：canonical_bytes 为 None 且 abs_path 为空
/// - `no_cas_conn`：cas_store 为 None（无 CAS 连接）
pub fn is_parse_failure_state(cas_state: &str) -> bool {
    matches!(
        cas_state,
        "parse_failed"
            | "canonicalize_failed"
            | "publish_failed"
            | "cas_lookup_failed"
            | "no_abs_path"
            | "no_cas_conn"
    )
}

/// 判断给定 cas_state 是否表示 unsupported（设计 §5.3 `unsupported`）
///
/// unsupported 状态：语言不被 Rust parser 支持，不发布空图谱
pub fn is_unsupported_state(cas_state: &str) -> bool {
    matches!(cas_state, "unsupported_language")
}

/// 判断给定 cas_state 是否表示 stale（设计 §5.3 `stale`）
///
/// 注意：当前 `_daemon_parse_and_publish` 不直接返回 stale 状态。
/// stale 由 `file_generation_seen` 在上游拒绝（`stale_seq_dropped`）。
/// 本函数保留接口供未来扩展（如 daemon 重启后重放时检测 stale generation）。
pub fn is_stale_state(cas_state: &str) -> bool {
    matches!(cas_state, "stale_seq_dropped" | "stale_generation")
}

/// 判断给定 cas_state 是否表示 partial（设计 §5.3 `partial`）
///
/// R6-P0-2: partial 状态发布事实到 CAS（保留符号图谱可查询），
/// 但**不替换上一代 snapshot**（保留好 snapshot 供生产查询）。
/// allows_retry=false（partial 不是致命错误，等下次文件变化自然恢复）。
pub fn is_partial_state(cas_state: &str) -> bool {
    matches!(cas_state, "partial_published")
}

/// 判断给定 cas_state 是否表示成功（设计 §5.3 `ok`）
///
/// 成功状态：CAS 已发布或缓存命中，可以替换 snapshot
///
/// 注意：partial_published 不属于 success（R6-P0-2 改为单独的 partial 状态，
/// 见 `is_partial_state`），避免坏解析替换上一代好 snapshot。
pub fn is_success_state(cas_state: &str) -> bool {
    matches!(cas_state, "ready_published" | "ready_cache_hit")
}

// ============================================
// snapshot 替换决策
// ============================================

/// 判断是否应该替换上一代 snapshot
///
/// 设计 §5.3：
/// - `ok` / `partial`：替换 snapshot（发布完整或部分 ParseFact）
/// - `failed`：不替换（保留上一代可查询 snapshot，记录失败并允许重试）
/// - `unsupported`：不替换（不发布空图谱）
/// - `stale`：不替换（generation CAS 拒绝，不覆盖新状态）
///
/// 本函数基于 `cas_state` 推导，是 `evaluate_generation_protection` 的轻量版，
/// 供热路径调用（不构造完整 `FailureProtectionResult`）。
pub fn should_replace_snapshot(cas_state: &str) -> bool {
    is_success_state(cas_state)
}

// ============================================
// dirty overlay 隔离（设计 §9.3）
// ============================================

/// 检测文件路径是否属于 dirty overlay
///
/// 设计 §9.3：dirty overlay 在重建或回滚期间不得进入 Global CAS
///
/// dirty overlay 判定规则：
/// - 路径包含 `.git/`（VCS 内部文件，非工作区源码）
/// - 路径包含 `.callwarden/`（daemon 内部文件，如 staging.log / snapshot）
/// - 路径以 `.callwarden-tmp-` 开头（daemon 临时文件）
/// - 路径以 `~` 开头（备份文件）
/// - 路径以 `.bak` / `.orig` / `.rej` 结尾（patch 残留文件）
///
/// 注意：本函数只做路径模式匹配，不检测 workspace 级别的 dirty 标记。
/// workspace 级 dirty 状态由 `workspace.rs` 维护，调用方应在调用本函数前
/// 先检查 workspace dirty 标记。
pub fn is_dirty_overlay(abs_path: &str, rel_path: &str) -> bool {
    // VCS 内部文件（.git/）
    if abs_path.contains("/.git/") || abs_path.contains("\\.git\\") {
        return true;
    }
    if rel_path.starts_with(".git/") || rel_path.contains("/.git/") {
        return true;
    }
    // daemon 内部文件（.callwarden/）
    if abs_path.contains("/.callwarden/") || abs_path.contains("\\.callwarden\\") {
        return true;
    }
    if rel_path.starts_with(".callwarden/") || rel_path.contains("/.callwarden/") {
        return true;
    }
    // daemon 临时文件（.callwarden-tmp-）
    if abs_path.contains("/.callwarden-tmp-") || abs_path.contains("\\.callwarden-tmp-") {
        return true;
    }
    // 备份文件（~ / .bak / .orig / .rej）
    if abs_path.ends_with('~') || rel_path.ends_with('~') {
        return true;
    }
    if abs_path.ends_with(".bak") || rel_path.ends_with(".bak") {
        return true;
    }
    if abs_path.ends_with(".orig") || rel_path.ends_with(".orig") {
        return true;
    }
    if abs_path.ends_with(".rej") || rel_path.ends_with(".rej") {
        return true;
    }
    false
}

// ============================================
// 失败 generation 保护结果
// ============================================

/// generation 保护评估结果
///
/// 供 daemon durable log（P1-F Step 3）记录失败原因，便于 daemon 重启后
/// 重放可重试 generation。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FailureProtectionResult {
    /// 是否阻止了 snapshot 替换（true = 保护上一代 snapshot）
    pub blocked: bool,
    /// 阻止原因（blocked=true 时非空）
    pub reason: String,
    /// 原始 cas_state（来自 `_daemon_parse_and_publish`）
    pub cas_state: String,
    /// 推导的 parse_status（"ok" / "partial" / "unsupported" / "failed" / "stale"）
    pub parse_status: String,
    /// 是否为 dirty overlay
    pub dirty_overlay: bool,
    /// 是否允许重试（仅 failed 状态允许重试，设计 §5.3）
    pub allows_retry: bool,
}

impl FailureProtectionResult {
    /// 构造成功结果（不阻止 snapshot 替换）
    pub fn ok(cas_state: &str) -> Self {
        Self {
            blocked: false,
            reason: String::new(),
            cas_state: cas_state.to_string(),
            parse_status: ParseStatus::Ok.as_str().to_string(),
            dirty_overlay: false,
            allows_retry: false,
        }
    }

    /// 构造 blocked 结果
    pub fn blocked(cas_state: &str, parse_status: ParseStatus, reason: String) -> Self {
        let allows_retry = parse_status.allows_retry();
        Self {
            blocked: true,
            reason,
            cas_state: cas_state.to_string(),
            parse_status: parse_status.as_str().to_string(),
            dirty_overlay: false,
            allows_retry,
        }
    }

    /// 转为 serde_json::Value（供 durable log 序列化）
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::to_value(self).unwrap_or_else(|_| {
            serde_json::json!({
                "blocked": true,
                "reason": "protection result serialization failed",
                "cas_state": self.cas_state,
                "parse_status": "failed",
            })
        })
    }
}

// ============================================
// 主入口：evaluate_generation_protection
// ============================================

/// 评估 generation 是否应该被保护（不替换 snapshot）
///
/// 设计 §5.3 + §9.3：
/// 1. dirty overlay 优先判断 → `Stale`（不进入 Global CAS）
/// 2. parse 失败 → `Failed`（不替换 snapshot，允许重试）
/// 3. unsupported → `Unsupported`（不发布空图谱，不重试）
/// 4. stale → `Stale`（generation CAS 拒绝，不重试）
/// 5. partial → `Partial`（已发布事实到 CAS，但不替换上一代好 snapshot）
/// 6. 成功 → 不保护（替换 snapshot）
///
/// 调用方应在 `merge_cas_to_codegraph` + `publish_snapshot` 之前调用本函数，
/// 根据 `blocked` 字段决定是否继续。
///
/// # 参数
/// - `cas_state`：来自 `_daemon_parse_and_publish` 的 cas_state 字符串
/// - `abs_path`：文件绝对路径（用于 dirty overlay 检测）
/// - `rel_path`：文件相对路径（用于 dirty overlay 检测）
pub fn evaluate_generation_protection(
    cas_state: &str,
    abs_path: &str,
    rel_path: &str,
) -> FailureProtectionResult {
    // 1. dirty overlay 优先判断（设计 §9.3）
    if is_dirty_overlay(abs_path, rel_path) {
        return FailureProtectionResult {
            blocked: true,
            reason: format!("dirty overlay rejected (设计 §9.3): rel_path={}", rel_path),
            cas_state: cas_state.to_string(),
            parse_status: ParseStatus::Stale.as_str().to_string(),
            dirty_overlay: true,
            allows_retry: false,
        };
    }

    // 2. parse 失败（设计 §5.3 failed）
    if is_parse_failure_state(cas_state) {
        return FailureProtectionResult::blocked(
            cas_state,
            ParseStatus::Failed,
            format!("parse failure (设计 §5.3 failed): cas_state={}", cas_state),
        );
    }

    // 3. unsupported（设计 §5.3 unsupported）
    if is_unsupported_state(cas_state) {
        return FailureProtectionResult::blocked(
            cas_state,
            ParseStatus::Unsupported,
            format!(
                "unsupported language (设计 §5.3 unsupported): cas_state={}",
                cas_state
            ),
        );
    }

    // 4. stale（设计 §5.3 stale）
    if is_stale_state(cas_state) {
        return FailureProtectionResult::blocked(
            cas_state,
            ParseStatus::Stale,
            format!(
                "stale generation (设计 §5.3 stale): cas_state={}",
                cas_state
            ),
        );
    }

    // 5. partial（设计 §5.3 partial）
    //
    // R6-P0-2: partial 已发布事实到 CAS，但不替换上一代 snapshot。
    // allows_retry=false（partial 不是致命错误，等下次文件变化自然恢复）。
    if is_partial_state(cas_state) {
        return FailureProtectionResult {
            blocked: true,
            reason: format!(
                "partial parse (设计 §5.3 partial): cas_state={}, 保留上一代好 snapshot",
                cas_state
            ),
            cas_state: cas_state.to_string(),
            parse_status: ParseStatus::Partial.as_str().to_string(),
            dirty_overlay: false,
            allows_retry: false,
        };
    }

    // 6. 成功（ok / ready_published / ready_cache_hit）
    FailureProtectionResult::ok(cas_state)
}

// ============================================
// 单元测试
// ============================================

// ============================================
// S2（ADR-002 §16.2）：snapshot.upload 分块组装槽位
// ============================================

use std::collections::{BTreeMap, HashMap};
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// upload slot TTL：自首块起 2h（§16.2）
const UPLOAD_SLOT_TTL: Duration = Duration::from_secs(2 * 60 * 60);
/// §16.2 单块解码后上限
pub const MAX_CHUNK_BYTES: usize = 16 * 1024 * 1024;
/// §16.2 内联解码后上限
pub const MAX_INLINE_BYTES: usize = 32 * 1024 * 1024;
/// §16.2 组装后 raw SQLite 上限
pub const MAX_ASSEMBLED_RAW_BYTES: usize = 512 * 1024 * 1024;
/// §16.2 total_chunks 上限
pub const MAX_TOTAL_CHUNKS: usize = 4096;

/// 一个分块上传会话的组装状态。
///
/// 键为 `(workspace_instance_id, upload_id)`：upload_id 客户端生成（uuid4 hex），
/// 不保证全局唯一，必须绑定 workspace 防串号。
pub struct UploadSlot {
    /// 首块声明并由后续块一致性校验
    total_chunks: usize,
    /// chunk_index → 解码后 gzip 字节（重传同 request_id 由 HTTP dedup 复用；
    /// 同 index 不同 request_id 重传覆盖写盘，保持幂等语义）
    chunks: BTreeMap<usize, Vec<u8>>,
    created_at: Instant,
}

/// 分块上传内存组装存储（daemon 内单实例，跨 worker 共享）。
///
/// 内存含量天然有界：单块 ≤ [`MAX_CHUNK_BYTES`]、块数 ≤ [`MAX_TOTAL_CHUNKS`]、
/// TTL 2h 惰性 sweep。
#[derive(Default)]
pub struct UploadSlotStore {
    slots: Mutex<HashMap<(String, String), UploadSlot>>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum SlotError {
    /// chunk_index 越界（≥ total_chunks）
    ChunkOutOfRange,
    /// total_chunks 与首块声明不一致
    TotalMismatch,
    /// commit 时缺块（块未到齐）
    MissingChunks(Vec<usize>),
    /// upload_id 首块从未到达
    UnknownUpload,
    /// 超过 2h TTL
    Expired,
    /// 单块解码后超过 16MiB 上限
    ChunkTooLarge,
    /// total_chunks 超过 4096 或 < 2
    TotalOutOfRange,
}

impl UploadSlotStore {
    pub fn new() -> Self {
        Self {
            slots: Mutex::new(HashMap::new()),
        }
    }

    /// 记录一块，返回当前已收块数 `chunks_received`。
    ///
    /// - `chunk_index` 必须 < `total_chunks`；
    /// - `total_chunks` 必须 ≥2（否则应走内联 `snapshot.upload`）且 ≤4096；
    /// - 后续块的 `total_chunks` 必须与首块一致；
    /// - 越界/不一致一律 fail-closed（不记盘，不影响既有槽位）。
    pub fn put_chunk(
        &self,
        workspace_instance_id: &str,
        upload_id: &str,
        chunk_index: usize,
        total_chunks: usize,
        chunk: Vec<u8>,
    ) -> Result<usize, SlotError> {
        if total_chunks < 2 || total_chunks > MAX_TOTAL_CHUNKS {
            return Err(SlotError::TotalOutOfRange);
        }
        if chunk_index >= total_chunks {
            return Err(SlotError::ChunkOutOfRange);
        }
        if chunk.len() > MAX_CHUNK_BYTES {
            return Err(SlotError::ChunkTooLarge);
        }
        let mut slots = self
            .slots
            .lock()
            .expect("UploadSlotStore mutex poisoned");
        let key = (workspace_instance_id.to_string(), upload_id.to_string());
        let slot = slots.entry(key).or_insert(UploadSlot {
            total_chunks,
            chunks: BTreeMap::new(),
            created_at: Instant::now(),
        });
        if slot.total_chunks != total_chunks {
            return Err(SlotError::TotalMismatch);
        }
        slot.chunks.insert(chunk_index, chunk);
        Ok(slot.chunks.len())
    }

    /// 校验块是否到齐（不消费）。commit 前的 Precondition。
    pub fn chunks_complete(
        &self,
        workspace_instance_id: &str,
        upload_id: &str,
        total_chunks: usize,
    ) -> Result<(), SlotError> {
        let mut slots = self
            .slots
            .lock()
            .expect("UploadSlotStore mutex poisoned");
        self.sweep_expired(&mut slots);
        let key = (workspace_instance_id.to_string(), upload_id.to_string());
        let slot = slots.get(&key).ok_or(SlotError::UnknownUpload)?;
        if slot.created_at.elapsed() > UPLOAD_SLOT_TTL {
            return Err(SlotError::Expired);
        }
        if slot.total_chunks != total_chunks {
            return Err(SlotError::TotalMismatch);
        }
        let missing: Vec<usize> = (0..total_chunks)
            .filter(|i| !slot.chunks.contains_key(i))
            .collect();
        if !missing.is_empty() {
            return Err(SlotError::MissingChunks(missing));
        }
        Ok(())
    }

    /// 消费 slot，按 chunk_index 拼回 gzip 流。
    ///
    /// 调用前应先用 [`UploadSlotStore::chunks_complete`] 校验（commit 处
    /// 校验失败须返回 E_SNAPSHOT_UPLOAD_CHUNK_MISSING 而非拼到一半失败）。
    pub fn take(
        &self,
        workspace_instance_id: &str,
        upload_id: &str,
        total_chunks: usize,
    ) -> Result<Vec<u8>, SlotError> {
        let mut slots = self
            .slots
            .lock()
            .expect("UploadSlotStore mutex poisoned");
        self.sweep_expired(&mut slots);
        let key = (workspace_instance_id.to_string(), upload_id.to_string());
        if !slots.contains_key(&key) {
            return Err(SlotError::UnknownUpload);
        }
        let slot = slots.remove(&key).expect("checked above");
        if slot.created_at.elapsed() > UPLOAD_SLOT_TTL {
            return Err(SlotError::Expired);
        }
        if slot.total_chunks != total_chunks {
            return Err(SlotError::TotalMismatch);
        }
        let missing: Vec<usize> = (0..total_chunks)
            .filter(|i| !slot.chunks.contains_key(i))
            .collect();
        if !missing.is_empty() {
            return Err(SlotError::MissingChunks(missing));
        }
        let mut out = Vec::new();
        for (idx, chunk) in slot.chunks {
            out.reserve(chunk.len());
            out.extend_from_slice(&chunk);
            debug_assert_eq!(out.len() % 1, 0, "chunks are raw bytes; idx {}", idx);
        }
        Ok(out)
    }

    /// 主动丢弃一个 slot（commit 失败时调用，保持「不残留」语义）。
    pub fn discard(&self, workspace_instance_id: &str, upload_id: &str) {
        let mut slots = self
            .slots
            .lock()
            .expect("UploadSlotStore mutex poisoned");
        slots.remove(&(workspace_instance_id.to_string(), upload_id.to_string()));
    }

    /// 惰性 sweep：清理过期 slot。仅在锁内调用。
    fn sweep_expired(&self, slots: &mut HashMap<(String, String), UploadSlot>) {
        slots.retain(|_, slot| slot.created_at.elapsed() <= UPLOAD_SLOT_TTL);
    }

    /// 诊断用：当前 slot 数（测试/健康检查）
    pub fn slot_count(&self) -> usize {
        self.slots
            .lock()
            .expect("UploadSlotStore mutex poisoned")
            .len()
    }
}

/// §16.1 路径形态兜底：判断字符串是否是绝对路径形态。
///
/// 规则（契约 §16.1）：
/// - Posix `/` 前缀
/// - Windows 盘符前缀（`C:\` / `C:/`）
/// - UNC `\\` 前缀
/// - Windows `\` 前缀（root-relative）
///
/// 注意：只判形态（daemon 无法解析远端路径），命中即交由调用方返回
/// E_REMOTE_PATH_FORBIDDEN，fail-closed。
pub fn looks_like_path(s: &str) -> bool {
    let s = s.trim();
    if s.is_empty() {
        return false;
    }
    if s.starts_with('/') || s.starts_with('\\') {
        return true;
    }
    // Windows 盘符前缀：单字母 + ':' + 分隔符
    let b = s.as_bytes();
    b.len() >= 3
        && b[0].is_ascii_alphabetic()
        && b[1] == b':'
        && (b[2] == b'\\' || b[2] == b'/')
}

/// §16.2 协议校验：upload_id 必须是 32 位小写 hex（uuid4形态，不带连字符）。
pub fn is_valid_upload_id(upload_id: &str) -> bool {
    upload_id.len() == 32
        && upload_id
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// §16.2 协议校验：sha256 必须是 64 位小写 hex。
pub fn is_valid_sha256_hex(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// §16.2 sha256 契约：对 raw（gunzip 后）字节计算 sha256，返回小写 hex。
pub fn sha256_hex(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(data);
    format!("{:x}", hasher.finalize())
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod s2_upload_tests {
    use super::*;

    fn chunk(i: usize, n: usize, data: Vec<u8>) -> Result<usize, SlotError> {
        let store = UploadSlotStore::new();
        store.put_chunk("ws1", "upload1", i, n, data)
    }

    #[test]
    fn test_put_chunk_basic() {
        let store = UploadSlotStore::new();
        assert_eq!(
            store
                .put_chunk("ws1", "upload1", 0, 2, b"a".to_vec())
                .unwrap(),
            1
        );
        assert_eq!(
            store
                .put_chunk("ws1", "upload1", 1, 2, b"b".to_vec())
                .unwrap(),
            2
        );
    }

    #[test]
    fn test_put_chunk_index_out_of_range() {
        assert_eq!(chunk(2, 2, b"x".to_vec()), Err(SlotError::ChunkOutOfRange));
    }

    #[test]
    fn test_put_chunk_total_mismatch() {
        let store = UploadSlotStore::new();
        store.put_chunk("ws1", "u1", 0, 3, b"a".to_vec()).unwrap();
        assert_eq!(
            store.put_chunk("ws1", "u1", 1, 2, b"b".to_vec()),
            Err(SlotError::TotalMismatch)
        );
    }

    #[test]
    fn test_put_chunk_total_out_of_range() {
        assert_eq!(
            chunk(0, 1, b"a".to_vec()),
            Err(SlotError::TotalOutOfRange)
        );
        assert_eq!(
            chunk(0, MAX_TOTAL_CHUNKS + 1, b"a".to_vec()),
            Err(SlotError::TotalOutOfRange)
        );
    }

    #[test]
    fn test_put_chunk_too_large() {
        assert_eq!(
            chunk(0, 2, vec![0u8; MAX_CHUNK_BYTES + 1]),
            Err(SlotError::ChunkTooLarge)
        );
    }

    #[test]
    fn test_assemble_roundtrip() {
        let store = UploadSlotStore::new();
        let gzip_stream = vec![0u8; 64];
        store
            .put_chunk("ws1", "u1", 0, 2, gzip_stream[..30].to_vec())
            .unwrap();
        store
            .put_chunk("ws1", "u1", 1, 2, gzip_stream[30..].to_vec())
            .unwrap();
        store.chunks_complete("ws1", "u1", 2).unwrap();
        let out = store.take("ws1", "u1", 2).unwrap();
        assert_eq!(out, gzip_stream);
        // take 消费 slot
        assert_eq!(
            store.chunks_complete("ws1", "u1", 2),
            Err(SlotError::UnknownUpload)
        );
    }

    #[test]
    fn test_chunk_order_irrelevant() {
        let store = UploadSlotStore::new();
        store.put_chunk("ws1", "u1", 1, 2, b"second".to_vec()).unwrap();
        store.put_chunk("ws1", "u1", 0, 2, b"first".to_vec()).unwrap();
        assert_eq!(store.take("ws1", "u1", 2).unwrap(), b"firstsecond");
    }

    #[test]
    fn test_missing_chunks() {
        let store = UploadSlotStore::new();
        store.put_chunk("ws1", "u1", 0, 3, b"a".to_vec()).unwrap();
        assert_eq!(
            store.chunks_complete("ws1", "u1", 3),
            Err(SlotError::MissingChunks(vec![1, 2]))
        );
    }

    #[test]
    fn test_unknown_upload() {
        let store = UploadSlotStore::new();
        assert_eq!(
            store.chunks_complete("ws1", "ghost", 2),
            Err(SlotError::UnknownUpload)
        );
    }

    #[test]
    fn test_workspace_isolation() {
        let store = UploadSlotStore::new();
        store.put_chunk("ws1", "u1", 0, 2, b"a".to_vec()).unwrap();
        // 同 upload_id 不同 workspace：互不串号
        assert_eq!(
            store.chunks_complete("ws2", "u1", 2),
            Err(SlotError::UnknownUpload)
        );
    }

    #[test]
    fn test_discard() {
        let store = UploadSlotStore::new();
        store.put_chunk("ws1", "u1", 0, 2, b"a".to_vec()).unwrap();
        store.discard("ws1", "u1");
        assert_eq!(store.slot_count(), 0);
    }

    #[test]
    fn test_looks_like_path() {
        assert!(looks_like_path("/etc/passwd"));
        assert!(looks_like_path("/home/u/x.py"));
        assert!(looks_like_path("C:\\Users\\u\\x.py"));
        assert!(looks_like_path("D:/repo/x.py"));
        assert!(looks_like_path("\\\\server\\share\\x"));
        assert!(looks_like_path("\\repo\\x"));
        assert!(!looks_like_path("src/main.rs"));
        assert!(!looks_like_path("x.py"));
        assert!(!looks_like_path(""));
        assert!(!looks_like_path("relative\\path"));
    }

    #[test]
    fn test_is_valid_upload_id() {
        assert!(is_valid_upload_id("0123456789abcdef0123456789abcdef"));
        assert!(!is_valid_upload_id("0123456789abcdef0123456789abcde")); // 31
        assert!(!is_valid_upload_id("0123456789abcdef0123456789abcdef0")); // 33
        assert!(!is_valid_upload_id("0123456789ABCDEF0123456789abcdef")); // uppercase
        assert!(!is_valid_upload_id("01234567-89ab-cdef-0123-456789abcdef")); // dashes
    }

    #[test]
    fn test_is_valid_sha256_hex() {
        assert!(is_valid_sha256_hex(&"a".repeat(64)));
        assert!(!is_valid_sha256_hex(&"a".repeat(63)));
        assert!(!is_valid_sha256_hex(&"A".repeat(64)));
        assert!(!is_valid_sha256_hex(&"g".repeat(64)));
    }

    #[test]
    fn test_sha256_hex_known_value() {
        // sha256("hello") = 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824
        assert_eq!(
            sha256_hex(b"hello"),
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ---- is_parse_failure_state ----

    #[test]
    fn test_is_parse_failure_state() {
        assert!(is_parse_failure_state("parse_failed"));
        assert!(is_parse_failure_state("canonicalize_failed"));
        assert!(is_parse_failure_state("publish_failed"));
        assert!(is_parse_failure_state("cas_lookup_failed"));
        assert!(is_parse_failure_state("no_abs_path"));
        assert!(is_parse_failure_state("no_cas_conn"));
        assert!(!is_parse_failure_state("ready_published"));
        assert!(!is_parse_failure_state("ready_cache_hit"));
        assert!(!is_parse_failure_state("unsupported_language"));
        assert!(!is_parse_failure_state(""));
    }

    // ---- is_unsupported_state ----

    #[test]
    fn test_is_unsupported_state() {
        assert!(is_unsupported_state("unsupported_language"));
        assert!(!is_unsupported_state("parse_failed"));
        assert!(!is_unsupported_state("ready_published"));
    }

    // ---- is_stale_state ----

    #[test]
    fn test_is_stale_state() {
        assert!(is_stale_state("stale_seq_dropped"));
        assert!(is_stale_state("stale_generation"));
        assert!(!is_stale_state("ready_published"));
    }

    // ---- is_success_state ----

    #[test]
    fn test_is_success_state() {
        assert!(is_success_state("ready_published"));
        assert!(is_success_state("ready_cache_hit"));
        assert!(!is_success_state("parse_failed"));
        assert!(!is_success_state("unsupported_language"));
        // R6-P0-2: partial 不属于 success（不替换 snapshot）
        assert!(!is_success_state("partial_published"));
    }

    // ---- is_partial_state ----

    #[test]
    fn test_is_partial_state() {
        assert!(is_partial_state("partial_published"));
        assert!(!is_partial_state("ready_published"));
        assert!(!is_partial_state("parse_failed"));
        assert!(!is_partial_state("ready_cache_hit"));
    }

    // ---- should_replace_snapshot ----

    #[test]
    fn test_should_replace_snapshot_success() {
        assert!(should_replace_snapshot("ready_published"));
        assert!(should_replace_snapshot("ready_cache_hit"));
    }

    #[test]
    fn test_should_replace_snapshot_failure() {
        assert!(!should_replace_snapshot("parse_failed"));
        assert!(!should_replace_snapshot("canonicalize_failed"));
        assert!(!should_replace_snapshot("publish_failed"));
        assert!(!should_replace_snapshot("unsupported_language"));
        assert!(!should_replace_snapshot("stale_seq_dropped"));
        assert!(!should_replace_snapshot("no_cas_conn"));
        // R6-P0-2: partial 不替换 snapshot
        assert!(!should_replace_snapshot("partial_published"));
    }

    // ---- is_dirty_overlay ----

    #[test]
    fn test_is_dirty_overlay_git() {
        assert!(is_dirty_overlay("/repo/.git/config", ".git/config"));
        assert!(is_dirty_overlay("/repo/sub/.git/HEAD", "sub/.git/HEAD"));
        assert!(is_dirty_overlay("C:\\repo\\.git\\config", ".git/config"));
    }

    #[test]
    fn test_is_dirty_overlay_callwarden() {
        assert!(is_dirty_overlay(
            "/home/u/.callwarden/callwarden.db",
            ".callwarden/callwarden.db"
        ));
        assert!(is_dirty_overlay(
            "/tmp/.callwarden-tmp-abc123",
            ".callwarden-tmp-abc123"
        ));
    }

    #[test]
    fn test_is_dirty_overlay_backup_files() {
        assert!(is_dirty_overlay("/repo/file.rs~", "file.rs~"));
        assert!(is_dirty_overlay("/repo/file.bak", "file.bak"));
        assert!(is_dirty_overlay("/repo/file.orig", "file.orig"));
        assert!(is_dirty_overlay("/repo/file.rej", "file.rej"));
    }

    #[test]
    fn test_is_dirty_overlay_clean_paths() {
        assert!(!is_dirty_overlay("/repo/src/main.rs", "src/main.rs"));
        assert!(!is_dirty_overlay("C:\\repo\\lib\\foo.py", "lib/foo.py"));
        assert!(!is_dirty_overlay("/repo/README.md", "README.md"));
    }

    // ---- evaluate_generation_protection ----

    #[test]
    fn test_evaluate_protection_success() {
        let r =
            evaluate_generation_protection("ready_published", "/repo/src/main.rs", "src/main.rs");
        assert!(!r.blocked);
        assert_eq!(r.parse_status, "ok");
        assert!(!r.dirty_overlay);
    }

    #[test]
    fn test_evaluate_protection_parse_failed() {
        let r = evaluate_generation_protection("parse_failed", "/repo/src/main.rs", "src/main.rs");
        assert!(r.blocked);
        assert_eq!(r.parse_status, "failed");
        assert!(r.allows_retry, "failed 状态应允许重试");
        assert!(!r.dirty_overlay);
    }

    #[test]
    fn test_evaluate_protection_unsupported() {
        let r = evaluate_generation_protection(
            "unsupported_language",
            "/repo/unknown.xyz",
            "unknown.xyz",
        );
        assert!(r.blocked);
        assert_eq!(r.parse_status, "unsupported");
        assert!(!r.allows_retry, "unsupported 状态不应允许重试");
    }

    #[test]
    fn test_evaluate_protection_stale() {
        let r =
            evaluate_generation_protection("stale_seq_dropped", "/repo/src/main.rs", "src/main.rs");
        assert!(r.blocked);
        assert_eq!(r.parse_status, "stale");
        assert!(!r.allows_retry, "stale 状态不应允许重试");
    }

    #[test]
    fn test_evaluate_protection_dirty_overlay() {
        let r =
            evaluate_generation_protection("ready_published", "/repo/.git/config", ".git/config");
        assert!(r.blocked);
        assert_eq!(r.parse_status, "stale");
        assert!(r.dirty_overlay);
        assert!(!r.allows_retry);
    }

    #[test]
    fn test_evaluate_protection_dirty_overlay_takes_priority_over_success() {
        // 即使 cas_state 是 ready_published，dirty overlay 仍然阻止
        let r = evaluate_generation_protection(
            "ready_published",
            "/repo/.callwarden/callwarden.db",
            ".callwarden/callwarden.db",
        );
        assert!(r.blocked);
        assert!(r.dirty_overlay);
    }

    // ---- FailureProtectionResult 序列化 ----

    #[test]
    fn test_failure_protection_result_serialize() {
        let r = FailureProtectionResult::blocked(
            "parse_failed",
            ParseStatus::Failed,
            "test reason".to_string(),
        );
        let json = r.to_json();
        assert_eq!(json["blocked"], true);
        assert_eq!(json["cas_state"], "parse_failed");
        assert_eq!(json["parse_status"], "failed");
        assert_eq!(json["allows_retry"], true);
    }

    #[test]
    fn test_failure_protection_result_ok_serialize() {
        let r = FailureProtectionResult::ok("ready_published");
        let json = r.to_json();
        assert_eq!(json["blocked"], false);
        assert_eq!(json["parse_status"], "ok");
    }

    // ---- R6-P0-2: partial_published 状态保护 ----

    #[test]
    fn test_evaluate_protection_partial_published() {
        let r =
            evaluate_generation_protection("partial_published", "/repo/src/main.rs", "src/main.rs");
        assert!(r.blocked, "partial 应阻止替换 snapshot");
        assert_eq!(r.parse_status, "partial");
        assert!(!r.allows_retry, "partial 不重试（等下次文件变化）");
        assert!(!r.dirty_overlay);
    }

    #[test]
    fn test_evaluate_protection_partial_takes_priority_over_success_only_check() {
        // partial_published 不应被识别为 success（保留好 snapshot）
        assert!(!is_success_state("partial_published"));
        assert!(is_partial_state("partial_published"));
    }
}
