//! P1-F Step 5: kill -9 恢复 E2E + stale session/generation 拒绝
//!
//! 设计文档：docs/design/rust-only-parser-cutover-plan.md §8 Phase 4 完成门
//!
//! 跨平台集成测试（不依赖 Unix UDS，Windows 可完整验收）：
//! - **kill -9 恢复**：ParseRetryLog 文件持久化 + 重启重放可重试 generation
//! - **stale 不覆盖**：snapshot_guard 阻止 failed/stale/unsupported 替换上一代 snapshot
//! - **失败文件可定位**：ParserMetrics FailureLabel 记录 workspace/file/generation/language
//!
//! 测试策略：
//! - 使用 tempfile 创建临时日志文件，模拟 daemon 崩溃后重启
//! - 通过 evaluate_generation_protection 验证 snapshot 保护决策
//! - 通过 ParserMetrics.record_parse 验证失败标签可观测性

use callwarden_core::daemon::parse_retry_log::{
    ParseFailureEntry, ParseRetryLog, ReplayConfig, replay_pending,
};
use callwarden_core::daemon::parser_metrics::{FailureLabel, ParserDoctor, ParserMetrics};
use callwarden_core::daemon::snapshot_guard::{
    evaluate_generation_protection, should_replace_snapshot,
};

use std::fs;

// ============================================
// 辅助函数
// ============================================

fn temp_log_path(name: &str) -> String {
    let mut p = std::env::temp_dir();
    p.push(format!("callwarden_p1f_e2e_{}.log", name));
    let s = p.to_string_lossy().into_owned();
    let _ = std::fs::remove_file(&s);
    s
}

fn make_failure_entry(rel_path: &str, generation: &str, allows_retry: bool) -> ParseFailureEntry {
    ParseFailureEntry::new(
        "ws1",
        rel_path,
        &format!("/repo/{}", rel_path),
        generation,
        "rust",
        if allows_retry { "failed" } else { "unsupported" },
        if allows_retry { "parse_failed" } else { "unsupported_language" },
        if allows_retry { "parse error" } else { "no grammar" },
        allows_retry,
    )
}

// ============================================
// E2E 测试 1: kill -9 恢复
// ============================================

/// 场景：daemon 正在处理 parse，写入 durable log 后被 kill -9。
/// 重启后读取 durable log，重放可重试的 failed generation。
#[test]
fn test_kill9_recovery_replays_retryable_generations() {
    let path = temp_log_path("kill9_recovery");
    // Phase 1: daemon 运行中，记录 3 个失败
    {
        let log = ParseRetryLog::new(&path).unwrap();
        // 2 个可重试（failed）
        let mut e1 = make_failure_entry("src/main.rs", "1:5", true);
        log.append(&mut e1).unwrap();
        let mut e2 = make_failure_entry("src/lib.rs", "1:6", true);
        log.append(&mut e2).unwrap();
        // 1 个不可重试（unsupported）
        let mut e3 = make_failure_entry("unknown.xyz", "1:7", false);
        log.append(&mut e3).unwrap();
    }
    // 模拟 kill -9：进程突然终止，log 文件保留在磁盘上

    // Phase 2: daemon 重启，读取 durable log 重放
    {
        let log = ParseRetryLog::new(&path).unwrap();
        let config = ReplayConfig::default();
        let replayable = replay_pending(&log, &config).unwrap();

        // 只重放可重试的 2 个 failed entry，跳过 unsupported
        assert_eq!(replayable.len(), 2, "kill -9 后应只重放可重试的 failed entries");
        assert_eq!(replayable[0].rel_path, "src/main.rs");
        assert_eq!(replayable[1].rel_path, "src/lib.rs");
        // unsupported entry 不在重放列表中
        for e in &replayable {
            assert!(e.allows_retry, "重放的 entry 必须允许重试");
            assert_eq!(e.parse_status, "failed");
        }
    }
    let _ = fs::remove_file(&path);
}

/// 场景：kill -9 后重放，重试成功 → 标记 applied，下次重放不再包含
#[test]
fn test_kill9_recovery_mark_applied_after_retry() {
    let path = temp_log_path("kill9_applied");
    {
        let log = ParseRetryLog::new(&path).unwrap();
        let mut e = make_failure_entry("src/main.rs", "1:5", true);
        let lsn = log.append(&mut e).unwrap();
        assert_eq!(lsn, 1);
    }
    // 重启后重放
    {
        let log = ParseRetryLog::new(&path).unwrap();
        let config = ReplayConfig::default();
        let replayable = replay_pending(&log, &config).unwrap();
        assert_eq!(replayable.len(), 1);

        // 模拟重试成功 → 标记 applied
        log.mark_applied(replayable[0].lsn).unwrap();
    }
    // 再次重启，applied entry 不应再被重放
    {
        let log = ParseRetryLog::new(&path).unwrap();
        let config = ReplayConfig::default();
        let replayable = replay_pending(&log, &config).unwrap();
        assert_eq!(replayable.len(), 0, "applied entry 不应再被重放");
    }
    let _ = fs::remove_file(&path);
}

/// 场景：kill -9 后重放，重试次数耗尽 → 标记 exhausted，不再重放
#[test]
fn test_kill9_recovery_exhausted_after_max_retry() {
    let path = temp_log_path("kill9_exhausted");
    {
        let log = ParseRetryLog::new(&path).unwrap();
        let mut e = make_failure_entry("src/main.rs", "1:5", true);
        log.append(&mut e).unwrap();
    }
    // 重启后重放，重试 max_retry=3 次
    {
        let log = ParseRetryLog::new(&path).unwrap();
        // 重试 3 次（达到上限）
        for _ in 0..3 {
            log.increment_retry(1).unwrap();
        }
        let config = ReplayConfig {
            max_retry: 3,
            retry_interval: 0.0,
        };
        let replayable = replay_pending(&log, &config).unwrap();
        assert_eq!(
            replayable.len(),
            0,
            "retry_count 达到 max_retry 后不应再重放"
        );

        // 标记 exhausted
        log.mark_exhausted(1).unwrap();
    }
    let _ = fs::remove_file(&path);
}

/// 场景：kill -9 发生在 append 中途（部分写入的行）→ 损坏行被跳过，完整行仍可重放
#[test]
fn test_kill9_recovery_skips_corrupted_lines() {
    let path = temp_log_path("kill9_corrupt");
    {
        let log = ParseRetryLog::new(&path).unwrap();
        let mut e1 = make_failure_entry("src/main.rs", "1:5", true);
        log.append(&mut e1).unwrap();
    }
    // 模拟部分写入的损坏行（kill -9 在 write 中途）
    {
        let mut file = std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap();
        use std::io::Write;
        file.write_all(b"{\"lsn\":2,\"rel_path\":\"src/lib.rs\",\"parse_status\":\"failed\",\"allows_retry\":true,\"workspace_id\":\"ws1\"\n")
            .unwrap(); // 缺少闭合 }，JSON 解析失败
    }
    // 重启后重放：损坏行被跳过，只重放完整的 entry
    {
        let log = ParseRetryLog::new(&path).unwrap();
        let config = ReplayConfig::default();
        let replayable = replay_pending(&log, &config).unwrap();
        assert_eq!(
            replayable.len(),
            1,
            "损坏行应被跳过，只重放完整 entry"
        );
        assert_eq!(replayable[0].rel_path, "src/main.rs");
    }
    let _ = fs::remove_file(&path);
}

// ============================================
// E2E 测试 2: stale 不覆盖
// ============================================

/// 场景：parse failed → snapshot_guard 阻止替换上一代 snapshot
#[test]
fn test_stale_does_not_overwrite_on_parse_failure() {
    let result = evaluate_generation_protection(
        "parse_failed",
        "/repo/src/main.rs",
        "src/main.rs",
    );
    assert!(result.blocked, "parse failed 必须阻止 snapshot 替换");
    assert_eq!(result.parse_status, "failed");
    assert!(result.allows_retry, "failed 状态应允许重试");
    assert!(!result.dirty_overlay);

    // should_replace_snapshot 应返回 false
    assert!(!should_replace_snapshot("parse_failed"));
    assert!(!should_replace_snapshot("canonicalize_failed"));
    assert!(!should_replace_snapshot("publish_failed"));
}

/// 场景：unsupported language → 不发布空图谱，不替换 snapshot
#[test]
fn test_stale_does_not_overwrite_on_unsupported() {
    let result = evaluate_generation_protection(
        "unsupported_language",
        "/repo/unknown.xyz",
        "unknown.xyz",
    );
    assert!(result.blocked, "unsupported 必须阻止 snapshot 替换");
    assert_eq!(result.parse_status, "unsupported");
    assert!(!result.allows_retry, "unsupported 状态不应允许重试");
}

/// 场景：stale generation → generation CAS 拒绝，不替换 snapshot
#[test]
fn test_stale_does_not_overwrite_on_stale_generation() {
    let result = evaluate_generation_protection(
        "stale_seq_dropped",
        "/repo/src/main.rs",
        "src/main.rs",
    );
    assert!(result.blocked, "stale 必须阻止 snapshot 替换");
    assert_eq!(result.parse_status, "stale");
    assert!(!result.allows_retry, "stale 状态不应允许重试");
}

/// 场景：dirty overlay 路径 → 即使 cas_state=ready_published 也被阻止
#[test]
fn test_stale_does_not_overwrite_on_dirty_overlay() {
    // .git/ 内部文件
    let r1 = evaluate_generation_protection("ready_published", "/repo/.git/config", ".git/config");
    assert!(r1.blocked);
    assert!(r1.dirty_overlay);
    assert_eq!(r1.parse_status, "stale");

    // .callwarden/ 内部文件
    let r2 = evaluate_generation_protection(
        "ready_published",
        "/home/u/.callwarden/callwarden.db",
        ".callwarden/callwarden.db",
    );
    assert!(r2.blocked);
    assert!(r2.dirty_overlay);

    // 备份文件
    let r3 = evaluate_generation_protection("ready_published", "/repo/file.rs~", "file.rs~");
    assert!(r3.blocked);
    assert!(r3.dirty_overlay);
}

/// 场景：成功状态 → 允许替换 snapshot
#[test]
fn test_success_overwrites_snapshot() {
    let r = evaluate_generation_protection(
        "ready_published",
        "/repo/src/main.rs",
        "src/main.rs",
    );
    assert!(!r.blocked, "成功状态不应阻止 snapshot 替换");
    assert_eq!(r.parse_status, "ok");
    assert!(should_replace_snapshot("ready_published"));
    assert!(should_replace_snapshot("ready_cache_hit"));
}

// ============================================
// E2E 测试 3: 失败文件可定位
// ============================================

/// 场景：parse 失败 → ParserMetrics 记录 FailureLabel，可定位到
/// workspace/file/generation/language
#[test]
fn test_failed_file_locatable_via_metrics() {
    let metrics = ParserMetrics::new();
    let label = FailureLabel {
        timestamp: 0.0,
        workspace_id: "ws1".to_string(),
        rel_path: "src/main.rs".to_string(),
        generation: "1:5".to_string(),
        language: "rust".to_string(),
        parse_status: "failed".to_string(),
        reason: "parse_failed".to_string(),
    };
    metrics.record_parse("failed", 5.0, 512, Some(label));

    let snap = metrics.snapshot();
    assert_eq!(snap["parse_total"], 1);
    assert_eq!(snap["parse_failed"], 1);
    assert_eq!(snap["recent_failures_count"], 1);

    // 验证失败可定位到 workspace/file/generation/language
    let failure = &snap["recent_failures"][0];
    assert_eq!(failure["workspace_id"], "ws1");
    assert_eq!(failure["rel_path"], "src/main.rs");
    assert_eq!(failure["generation"], "1:5");
    assert_eq!(failure["language"], "rust");
    assert_eq!(failure["parse_status"], "failed");
    assert_eq!(failure["reason"], "parse_failed");
}

/// 场景：多个文件失败 → 每个失败都可定位（bounded 队列保留最近 N 个）
#[test]
fn test_multiple_failed_files_locatable() {
    let metrics = ParserMetrics::with_max_recent_failures(10);
    for i in 0..5 {
        let label = FailureLabel {
            timestamp: 0.0,
            workspace_id: format!("ws{}", i),
            rel_path: format!("src/f{}.rs", i),
            generation: format!("1:{}", i),
            language: "rust".to_string(),
            parse_status: "failed".to_string(),
            reason: format!("error {}", i),
        };
        metrics.record_parse("failed", 1.0, 100, Some(label));
    }
    let snap = metrics.snapshot();
    assert_eq!(snap["parse_failed"], 5);
    assert_eq!(snap["recent_failures_count"], 5);

    // 每个失败都可定位
    for i in 0..5 {
        let failure = &snap["recent_failures"][i];
        assert_eq!(failure["workspace_id"], format!("ws{}", i));
        assert_eq!(failure["rel_path"], format!("src/f{}.rs", i));
        assert_eq!(failure["generation"], format!("1:{}", i));
    }
}

/// 场景：不同失败状态（partial/unsupported/failed）都可定位
#[test]
fn test_all_failure_statuses_locatable() {
    let metrics = ParserMetrics::new();
    let statuses = ["partial", "failed", "unsupported", "stale"];
    for (i, status) in statuses.iter().enumerate() {
        let label = FailureLabel {
            timestamp: 0.0,
            workspace_id: "ws1".to_string(),
            rel_path: format!("f{}.rs", i),
            generation: format!("1:{}", i),
            language: "rust".to_string(),
            parse_status: status.to_string(),
            reason: format!("{} reason", status),
        };
        metrics.record_parse(status, 1.0, 100, Some(label));
    }
    let snap = metrics.snapshot();
    // 所有非 ok 状态都应记录到 recent_failures
    assert_eq!(snap["recent_failures_count"], 4, "所有非 ok 状态都应可定位");
}

/// 场景：ParserDoctor 自检通过 → Rust grammar/ABI 可用
#[test]
fn test_doctor_self_check_passes() {
    let doctor = ParserDoctor::new();
    let report = doctor.run_check();
    // 至少有 4 个检查项
    assert!(report.checks.len() >= 4, "doctor 应至少有 4 个检查项");
    // core_version 非空
    assert!(!report.core_version.is_empty(), "core_version 应非空");
    // 如果 Rust grammar 可用，整体状态应为 healthy
    if !report.supported_languages.is_empty() {
        assert_eq!(
            report.status, "healthy",
            "Rust grammar 可用时 doctor 应为 healthy"
        );
    }
}

// ============================================
// E2E 测试 4: 完整 kill -9 恢复链路
// ============================================

/// 场景：完整的 kill -9 恢复链路：
/// 1. daemon 处理 refresh 时 parse 失败
/// 2. snapshot_guard 评估为 blocked (failed)
/// 3. durable log 记录失败 entry
/// 4. kill -9 发生
/// 5. daemon 重启，replay_pending 重放可重试 entry
/// 6. ParserMetrics 记录失败，可定位
#[test]
fn test_full_kill9_recovery_chain() {
    let log_path = temp_log_path("full_chain");
    let metrics = ParserMetrics::new();

    // Phase 1: daemon 运行中，处理 refresh 时 parse 失败
    let cas_state = "parse_failed";
    let abs_path = "/repo/src/main.rs";
    let rel_path = "src/main.rs";

    // Step 1: snapshot_guard 评估
    let protection = evaluate_generation_protection(cas_state, abs_path, rel_path);
    assert!(protection.blocked);
    assert_eq!(protection.parse_status, "failed");
    assert!(protection.allows_retry);

    // Step 2: durable log 记录失败
    {
        let log = ParseRetryLog::new(&log_path).unwrap();
        let mut entry = ParseFailureEntry::new(
            "ws1",
            rel_path,
            abs_path,
            "1:5",
            "rust",
            "failed",
            cas_state,
            "parse error",
            true,
        );
        let lsn = log.append(&mut entry).unwrap();
        assert_eq!(lsn, 1);

        // Step 3: ParserMetrics 记录失败，可定位
        let label = FailureLabel {
            timestamp: 0.0,
            workspace_id: "ws1".to_string(),
            rel_path: rel_path.to_string(),
            generation: "1:5".to_string(),
            language: "rust".to_string(),
            parse_status: "failed".to_string(),
            reason: cas_state.to_string(),
        };
        metrics.record_parse("failed", 5.0, 512, Some(label));
    }

    // Phase 2: kill -9 发生（进程终止，log 文件持久化）

    // Phase 3: daemon 重启，重放
    {
        let log = ParseRetryLog::new(&log_path).unwrap();
        let config = ReplayConfig::default();
        let replayable = replay_pending(&log, &config).unwrap();
        assert_eq!(replayable.len(), 1, "应重放 1 个可重试 entry");

        // 验证重放的 entry 上下文完整
        let e = &replayable[0];
        assert_eq!(e.workspace_id, "ws1");
        assert_eq!(e.rel_path, rel_path);
        assert_eq!(e.abs_path, abs_path);
        assert_eq!(e.generation, "1:5");
        assert_eq!(e.language, "rust");
        assert_eq!(e.parse_status, "failed");
        assert!(e.allows_retry);

        // 模拟重试成功
        log.mark_applied(e.lsn).unwrap();
    }

    // 验证 ParserMetrics 仍记录失败（可定位）
    let snap = metrics.snapshot();
    assert_eq!(snap["parse_failed"], 1);
    assert_eq!(snap["recent_failures_count"], 1);
    assert_eq!(snap["recent_failures"][0]["rel_path"], rel_path);
    assert_eq!(snap["recent_failures"][0]["workspace_id"], "ws1");

    let _ = fs::remove_file(&log_path);
}

/// 场景：kill -9 恢复后，不重试 unsupported 状态（永久拒绝）
#[test]
fn test_kill9_recovery_skips_unsupported_permanent() {
    let path = temp_log_path("skip_unsupported");
    {
        let log = ParseRetryLog::new(&path).unwrap();
        // unsupported entry（allows_retry=false → status=permanent）
        let mut e = make_failure_entry("unknown.xyz", "1:1", false);
        log.append(&mut e).unwrap();
    }
    // 重启后重放
    {
        let log = ParseRetryLog::new(&path).unwrap();
        let config = ReplayConfig::default();
        let replayable = replay_pending(&log, &config).unwrap();
        assert_eq!(
            replayable.len(),
            0,
            "unsupported (permanent) 不应被重放"
        );
    }
    let _ = fs::remove_file(&path);
}
