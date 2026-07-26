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
            reason: format!(
                "dirty overlay rejected (设计 §9.3): rel_path={}",
                rel_path
            ),
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
            format!(
                "parse failure (设计 §5.3 failed): cas_state={}",
                cas_state
            ),
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
        assert!(!is_dirty_overlay(
            "C:\\repo\\lib\\foo.py",
            "lib/foo.py"
        ));
        assert!(!is_dirty_overlay("/repo/README.md", "README.md"));
    }

    // ---- evaluate_generation_protection ----

    #[test]
    fn test_evaluate_protection_success() {
        let r = evaluate_generation_protection("ready_published", "/repo/src/main.rs", "src/main.rs");
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
        let r = evaluate_generation_protection(
            "stale_seq_dropped",
            "/repo/src/main.rs",
            "src/main.rs",
        );
        assert!(r.blocked);
        assert_eq!(r.parse_status, "stale");
        assert!(!r.allows_retry, "stale 状态不应允许重试");
    }

    #[test]
    fn test_evaluate_protection_dirty_overlay() {
        let r = evaluate_generation_protection(
            "ready_published",
            "/repo/.git/config",
            ".git/config",
        );
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
        let r = evaluate_generation_protection(
            "partial_published",
            "/repo/src/main.rs",
            "src/main.rs",
        );
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
