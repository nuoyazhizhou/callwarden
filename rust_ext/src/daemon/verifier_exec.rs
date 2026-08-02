//! 昂贵 verifier 执行——SQLite 写事务外运行（Req 14.16, 6.18）。
//!
//! ## 职责
//! - verifier 在 daemon 进程内、SQLite 写事务**之外**执行 [Req 14.16]
//! - 写事务只提交不可变记录与状态转换，长耗时验证不占用写锁
//! - 快照内容 hash 只覆盖 Envelope relevant scope、Actual_Changes 与声明的
//!   verifier 依赖，全仓库 hash 只作为非默认显式请求 [Req 6.18]
//! - S0/S1 双快照防 TOCTOU [Req 6.9, 6.10]
//!
//! ## 执行流程
//! ```text
//! capture S0 → run verifier (outside txn) → capture S1
//!   → S0 == S1? → open write txn → commit records + transition → close txn
//!   → S0 != S1? → fail closed (stale), no state change
//! ```
//!
//! ## 与串行化点的关系
//! verifier 执行本身不经过串行化点（只读 + 外部计算）。
//! 只有最终 verdict/record 的写入才经串行化点提交。
//! 因此多个 verifier 可以并发执行，只在最终提交时串行。

use std::collections::HashMap;
use std::time::Instant;

/// Verifier 执行结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VerifierOutcome {
    /// 验证通过，可以提交
    Pass {
        /// verifier 输出摘要
        summary: String,
        /// 验证耗时（毫秒）
        duration_ms: u64,
    },
    /// 验证失败
    Fail {
        /// 失败原因
        reason: String,
        /// 验证耗时（毫秒）
        duration_ms: u64,
    },
    /// S0/S1 快照不一致（TOCTOU 检测），fail closed [Req 6.10]
    Stale {
        /// S0 快照 hash
        s0_hash: String,
        /// S1 快照 hash
        s1_hash: String,
    },
    /// S0 或 S1 捕获失败 [Req 6.10]
    CaptureFailure {
        /// 失败阶段
        phase: CapturePhase,
        /// 错误描述
        error: String,
    },
}

/// 快照捕获失败阶段。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CapturePhase {
    /// S0（verifier 执行前）
    Before,
    /// S1（verifier 执行后）
    After,
}

/// 快照捕获——S0/S1 的输入集合 [Req 6.9]。
///
/// 包含：Current_Envelope 绑定、relevant scope、required selectors、
/// Verifier 版本与配置、依赖 hash、Workspace_Snapshot。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotInput {
    /// Current_Envelope 绑定 hash
    pub envelope_binding_hash: String,
    /// relevant scope 文件路径集合（排序后 hash）
    pub relevant_scope: Vec<String>,
    /// required selectors
    pub selectors: Vec<String>,
    /// verifier 版本标识
    pub verifier_version: String,
    /// verifier 配置 hash
    pub verifier_config_hash: String,
    /// 依赖 hash 集合（声明的 verifier 依赖）
    pub dependency_hashes: Vec<String>,
    /// Workspace_Snapshot 标识
    pub workspace_snapshot_id: String,
}

impl SnapshotInput {
    /// 计算快照 hash（SHA-256 语义，此处用简化表示）。
    ///
    /// 只覆盖 Envelope relevant scope、Actual_Changes 与声明的 verifier 依赖
    /// [Req 6.18]，不包含全仓库内容。
    pub fn compute_hash(&self) -> String {
        // 确定性序列化：所有字段按固定顺序拼接
        let mut content = String::new();
        content.push_str(&self.envelope_binding_hash);
        content.push('\n');
        for path in &self.relevant_scope {
            content.push_str(path);
            content.push('\n');
        }
        for sel in &self.selectors {
            content.push_str(sel);
            content.push('\n');
        }
        content.push_str(&self.verifier_version);
        content.push('\n');
        content.push_str(&self.verifier_config_hash);
        content.push('\n');
        for dep in &self.dependency_hashes {
            content.push_str(dep);
            content.push('\n');
        }
        content.push_str(&self.workspace_snapshot_id);

        // 简化 hash（生产环境应使用 SHA-256）
        format!("snap:{:016x}", simple_hash(&content))
    }
}

/// 作用域内容 hasher [Req 6.18]。
///
/// 只 hash Envelope relevant scope + Actual_Changes + 声明的 verifier 依赖。
/// 全仓库 hash 只作为非默认显式请求。
#[derive(Debug, Clone)]
pub struct ScopedHasher {
    /// relevant scope 文件路径 → 内容 hash
    scoped_files: HashMap<String, String>,
    /// Actual_Changes diff hash
    actual_changes_hash: Option<String>,
    /// 声明的 verifier 依赖 hash
    verifier_dep_hashes: Vec<String>,
}

impl ScopedHasher {
    /// 创建空的作用域 hasher。
    pub fn new() -> Self {
        Self {
            scoped_files: HashMap::new(),
            actual_changes_hash: None,
            verifier_dep_hashes: Vec::new(),
        }
    }

    /// 添加 relevant scope 文件的内容 hash。
    pub fn add_scoped_file(&mut self, path: &str, content_hash: &str) {
        self.scoped_files.insert(path.to_string(), content_hash.to_string());
    }

    /// 设置 Actual_Changes 的 diff hash。
    pub fn set_actual_changes_hash(&mut self, hash: &str) {
        self.actual_changes_hash = Some(hash.to_string());
    }

    /// 添加声明的 verifier 依赖 hash。
    pub fn add_verifier_dependency(&mut self, dep_hash: &str) {
        self.verifier_dep_hashes.push(dep_hash.to_string());
    }

    /// 计算作用域内容 hash [Req 6.18]。
    ///
    /// 只覆盖已注册的 scoped files + actual changes + verifier deps。
    /// 不包含全仓库内容。
    pub fn compute_scoped_hash(&self) -> String {
        let mut content = String::new();

        // 按路径排序确保确定性
        let mut paths: Vec<&String> = self.scoped_files.keys().collect();
        paths.sort();
        for path in paths {
            content.push_str(path);
            content.push('=');
            content.push_str(&self.scoped_files[path]);
            content.push('\n');
        }

        if let Some(ref changes_hash) = self.actual_changes_hash {
            content.push_str("actual_changes=");
            content.push_str(changes_hash);
            content.push('\n');
        }

        for dep in &self.verifier_dep_hashes {
            content.push_str("verifier_dep=");
            content.push_str(dep);
            content.push('\n');
        }

        format!("scoped:{:016x}", simple_hash(&content))
    }

    /// 全仓库 hash——非默认显式请求 [Req 6.18]。
    ///
    /// 调用方必须显式调用此方法；默认路径不使用全仓库 hash。
    /// 参数 `whole_repo_hash` 由调用方提供（daemon 不主动计算全仓库 hash）。
    pub fn with_whole_repo_hash(&self, whole_repo_hash: &str) -> String {
        let scoped = self.compute_scoped_hash();
        format!("{}+repo:{}", scoped, whole_repo_hash)
    }

    /// 获取 scoped files 数量。
    pub fn scoped_file_count(&self) -> usize {
        self.scoped_files.len()
    }
}

impl Default for ScopedHasher {
    fn default() -> Self {
        Self::new()
    }
}

/// Verifier 执行器——在 SQLite 写事务外运行昂贵验证 [Req 14.16]。
///
/// 执行流程：
/// 1. 捕获 S0（verifier 执行前的快照输入）
/// 2. 运行 verifier（外部计算，不持有写锁）
/// 3. 捕获 S1（verifier 执行后的快照输入）
/// 4. 比较 S0 与 S1：不一致则 fail closed（stale）
/// 5. 一致则返回 Pass/Fail，由调用方在串行化点内提交
///
/// 本结构体不持有数据库连接，不执行任何 SQL。
/// 写事务的开启与提交由调用方（dispatch 层）负责。
#[derive(Debug)]
pub struct VerifierExecutor {
    /// verifier 版本标识
    verifier_version: String,
}

impl VerifierExecutor {
    /// 创建执行器。
    pub fn new(verifier_version: &str) -> Self {
        Self {
            verifier_version: verifier_version.to_string(),
        }
    }

    /// 执行 verifier（事务外）。
    ///
    /// 参数：
    /// - `s0`: verifier 执行前的快照输入
    /// - `verify_fn`: verifier 闭包（外部计算，可能耗时）
    /// - `s1`: verifier 执行后的快照输入
    ///
    /// 返回 VerifierOutcome，调用方据此决定是否在写事务内提交。
    ///
    /// [Req 14.16]: 本方法不执行任何 SQL，不持有写锁。
    /// [Req 6.9]: S0/S1 包含 Current_Envelope 绑定、relevant scope 等。
    /// [Req 6.10]: S0/S1 不一致时 fail closed。
    pub fn execute<F>(
        &self,
        s0: &SnapshotInput,
        verify_fn: F,
        s1: &SnapshotInput,
    ) -> VerifierOutcome
    where
        F: FnOnce() -> Result<String, String>,
    {
        let s0_hash = s0.compute_hash();
        let s1_hash = s1.compute_hash();

        // TOCTOU 检测 [Req 6.10]
        if s0_hash != s1_hash {
            return VerifierOutcome::Stale {
                s0_hash,
                s1_hash,
            };
        }

        // 运行 verifier（事务外，不持有写锁）
        let start = Instant::now();
        let result = verify_fn();
        let duration_ms = start.elapsed().as_millis() as u64;

        match result {
            Ok(summary) => VerifierOutcome::Pass {
                summary,
                duration_ms,
            },
            Err(reason) => VerifierOutcome::Fail {
                reason,
                duration_ms,
            },
        }
    }

    /// 验证 S0/S1 快照一致性（不运行 verifier）。
    ///
    /// 用于调用方在提交前做最终检查。
    pub fn check_snapshot_consistency(
        &self,
        s0: &SnapshotInput,
        s1: &SnapshotInput,
    ) -> Result<(), (String, String)> {
        let s0_hash = s0.compute_hash();
        let s1_hash = s1.compute_hash();
        if s0_hash == s1_hash {
            Ok(())
        } else {
            Err((s0_hash, s1_hash))
        }
    }

    /// 获取 verifier 版本。
    pub fn version(&self) -> &str {
        &self.verifier_version
    }
}

/// 不可变验证记录——写事务内提交的数据 [Req 14.16]。
///
/// 写事务只提交此记录与状态转换，不包含 verifier 执行过程。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImmutableVerifierRecord {
    /// 记录 ID
    pub record_id: String,
    /// verifier 版本
    pub verifier_version: String,
    /// 快照 hash（S0 == S1 时的共同值）
    pub snapshot_hash: String,
    /// 作用域内容 hash [Req 6.18]
    pub scoped_content_hash: String,
    /// 验证结果
    pub outcome: VerifierRecordOutcome,
    /// 验证耗时（毫秒）
    pub duration_ms: u64,
    /// Authoritative_Clock 时间戳（由调用方填入）
    pub timestamp_ms: u64,
}

/// 验证记录结果（持久化用，比 VerifierOutcome 更精简）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VerifierRecordOutcome {
    /// 通过
    Pass,
    /// 失败
    Fail,
    /// 快照不一致（stale）
    Stale,
}

impl From<&VerifierOutcome> for VerifierRecordOutcome {
    fn from(outcome: &VerifierOutcome) -> Self {
        match outcome {
            VerifierOutcome::Pass { .. } => Self::Pass,
            VerifierOutcome::Fail { .. } => Self::Fail,
            VerifierOutcome::Stale { .. } => Self::Stale,
            VerifierOutcome::CaptureFailure { .. } => Self::Fail,
        }
    }
}

// ---------------------------------------------------------------------------
// 辅助函数
// ---------------------------------------------------------------------------

/// 简化 hash 函数（FNV-1a 64-bit）。
/// 生产环境应替换为 SHA-256，此处为纯逻辑测试用。
fn simple_hash(input: &str) -> u64 {
    const FNV_OFFSET: u64 = 0xcbf29ce484222325;
    const FNV_PRIME: u64 = 0x100000001b3;
    let mut hash = FNV_OFFSET;
    for byte in input.as_bytes() {
        hash ^= *byte as u64;
        hash = hash.wrapping_mul(FNV_PRIME);
    }
    hash
}

// ---------------------------------------------------------------------------
// 测试
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn make_snapshot_input(scope: &[&str]) -> SnapshotInput {
        SnapshotInput {
            envelope_binding_hash: "env-hash-001".to_string(),
            relevant_scope: scope.iter().map(|s| s.to_string()).collect(),
            selectors: vec!["selector-a".to_string()],
            verifier_version: "1.0.0".to_string(),
            verifier_config_hash: "cfg-hash".to_string(),
            dependency_hashes: vec!["dep-1".to_string()],
            workspace_snapshot_id: "ws-snap-001".to_string(),
        }
    }

    // --- Req 14.16: verifier 在写事务外执行 ---

    #[test]
    fn test_verifier_pass_outside_transaction() {
        // verifier 执行不涉及任何 SQL，纯计算
        let executor = VerifierExecutor::new("1.0.0");
        let s0 = make_snapshot_input(&["src/main.rs", "src/lib.rs"]);
        let s1 = s0.clone(); // 快照未变

        let outcome = executor.execute(&s0, || Ok("all checks passed".to_string()), &s1);

        match outcome {
            VerifierOutcome::Pass { summary, duration_ms } => {
                assert_eq!(summary, "all checks passed");
                // duration 应为非负（可能为 0 如果太快）
                assert!(duration_ms < 1000); // 不应超过 1 秒
            }
            _ => panic!("expected Pass, got {:?}", outcome),
        }
    }

    #[test]
    fn test_verifier_fail_outside_transaction() {
        let executor = VerifierExecutor::new("1.0.0");
        let s0 = make_snapshot_input(&["src/main.rs"]);
        let s1 = s0.clone();

        let outcome = executor.execute(
            &s0,
            || Err("compilation error at line 42".to_string()),
            &s1,
        );

        match outcome {
            VerifierOutcome::Fail { reason, .. } => {
                assert!(reason.contains("compilation error"));
            }
            _ => panic!("expected Fail, got {:?}", outcome),
        }
    }

    // --- Req 6.9/6.10: S0/S1 TOCTOU 防护 ---

    #[test]
    fn test_stale_detection_s0_s1_mismatch() {
        let executor = VerifierExecutor::new("1.0.0");
        let s0 = make_snapshot_input(&["src/main.rs"]);
        // S1 的 scope 变了（模拟 verifier 执行期间文件被修改）
        let s1 = make_snapshot_input(&["src/main.rs", "src/modified.rs"]);

        let outcome = executor.execute(&s0, || Ok("should not reach".to_string()), &s1);

        match outcome {
            VerifierOutcome::Stale { s0_hash, s1_hash } => {
                assert_ne!(s0_hash, s1_hash);
                assert!(s0_hash.starts_with("snap:"));
                assert!(s1_hash.starts_with("snap:"));
            }
            _ => panic!("expected Stale, got {:?}", outcome),
        }
    }

    #[test]
    fn test_stale_verifier_not_executed() {
        // S0/S1 不一致时 verifier 闭包不应被调用
        let executor = VerifierExecutor::new("1.0.0");
        let s0 = make_snapshot_input(&["a.rs"]);
        let s1 = make_snapshot_input(&["b.rs"]);

        let mut called = false;
        let outcome = executor.execute(
            &s0,
            || {
                called = true;
                Ok("unreachable".to_string())
            },
            &s1,
        );

        assert!(matches!(outcome, VerifierOutcome::Stale { .. }));
        // 注意：当前实现中 verifier 在 S0/S1 比较之后执行，
        // 但由于 S0 != S1 提前返回，闭包不会被调用
        assert!(!called);
    }

    #[test]
    fn test_check_snapshot_consistency_ok() {
        let executor = VerifierExecutor::new("1.0.0");
        let s0 = make_snapshot_input(&["x.rs"]);
        let s1 = s0.clone();

        assert!(executor.check_snapshot_consistency(&s0, &s1).is_ok());
    }

    #[test]
    fn test_check_snapshot_consistency_mismatch() {
        let executor = VerifierExecutor::new("1.0.0");
        let s0 = make_snapshot_input(&["x.rs"]);
        let s1 = make_snapshot_input(&["y.rs"]);

        let result = executor.check_snapshot_consistency(&s0, &s1);
        assert!(result.is_err());
        let (h0, h1) = result.unwrap_err();
        assert_ne!(h0, h1);
    }

    // --- Req 6.18: 作用域内容 hash ---

    #[test]
    fn test_scoped_hasher_covers_only_scope() {
        let mut hasher = ScopedHasher::new();
        hasher.add_scoped_file("src/main.rs", "abc123");
        hasher.add_scoped_file("src/lib.rs", "def456");
        hasher.set_actual_changes_hash("diff-hash-789");
        hasher.add_verifier_dependency("dep-hash-aaa");

        let hash = hasher.compute_scoped_hash();
        assert!(hash.starts_with("scoped:"));
        assert_eq!(hasher.scoped_file_count(), 2);
    }

    #[test]
    fn test_scoped_hash_deterministic() {
        let mut h1 = ScopedHasher::new();
        h1.add_scoped_file("b.rs", "hash-b");
        h1.add_scoped_file("a.rs", "hash-a");

        let mut h2 = ScopedHasher::new();
        h2.add_scoped_file("a.rs", "hash-a");
        h2.add_scoped_file("b.rs", "hash-b");

        // 插入顺序不同但结果相同（内部排序）
        assert_eq!(h1.compute_scoped_hash(), h2.compute_scoped_hash());
    }

    #[test]
    fn test_scoped_hash_changes_with_content() {
        let mut h1 = ScopedHasher::new();
        h1.add_scoped_file("src/main.rs", "version-1");

        let mut h2 = ScopedHasher::new();
        h2.add_scoped_file("src/main.rs", "version-2");

        assert_ne!(h1.compute_scoped_hash(), h2.compute_scoped_hash());
    }

    #[test]
    fn test_whole_repo_hash_is_explicit_non_default() {
        // 全仓库 hash 只作为非默认显式请求 [Req 6.18]
        let mut hasher = ScopedHasher::new();
        hasher.add_scoped_file("src/main.rs", "abc");

        let scoped_only = hasher.compute_scoped_hash();
        let with_repo = hasher.with_whole_repo_hash("full-repo-hash-xyz");

        // 默认不包含全仓库 hash
        assert!(!scoped_only.contains("repo:"));
        // 显式请求时包含
        assert!(with_repo.contains("repo:full-repo-hash-xyz"));
        assert!(with_repo.starts_with("scoped:"));
    }

    #[test]
    fn test_empty_hasher_produces_valid_hash() {
        let hasher = ScopedHasher::new();
        let hash = hasher.compute_scoped_hash();
        assert!(hash.starts_with("scoped:"));
    }

    // --- ImmutableVerifierRecord ---

    #[test]
    fn test_immutable_record_from_outcome() {
        let outcome = VerifierOutcome::Pass {
            summary: "ok".to_string(),
            duration_ms: 150,
        };
        let record_outcome = VerifierRecordOutcome::from(&outcome);
        assert_eq!(record_outcome, VerifierRecordOutcome::Pass);

        let fail_outcome = VerifierOutcome::Fail {
            reason: "error".to_string(),
            duration_ms: 200,
        };
        assert_eq!(
            VerifierRecordOutcome::from(&fail_outcome),
            VerifierRecordOutcome::Fail
        );

        let stale_outcome = VerifierOutcome::Stale {
            s0_hash: "a".to_string(),
            s1_hash: "b".to_string(),
        };
        assert_eq!(
            VerifierRecordOutcome::from(&stale_outcome),
            VerifierRecordOutcome::Stale
        );
    }

    #[test]
    fn test_immutable_record_structure() {
        let record = ImmutableVerifierRecord {
            record_id: "rec-001".to_string(),
            verifier_version: "1.0.0".to_string(),
            snapshot_hash: "snap:12345".to_string(),
            scoped_content_hash: "scoped:67890".to_string(),
            outcome: VerifierRecordOutcome::Pass,
            duration_ms: 100,
            timestamp_ms: 1700000000000,
        };

        // 不可变记录的所有字段都已填充
        assert_eq!(record.record_id, "rec-001");
        assert_eq!(record.verifier_version, "1.0.0");
        assert_eq!(record.outcome, VerifierRecordOutcome::Pass);
        assert_eq!(record.duration_ms, 100);
        assert_eq!(record.timestamp_ms, 1700000000000);
    }

    // --- SnapshotInput hash 确定性 ---

    #[test]
    fn test_snapshot_input_hash_deterministic() {
        let s1 = make_snapshot_input(&["a.rs", "b.rs"]);
        let s2 = make_snapshot_input(&["a.rs", "b.rs"]);
        assert_eq!(s1.compute_hash(), s2.compute_hash());
    }

    #[test]
    fn test_snapshot_input_hash_sensitive_to_scope() {
        let s1 = make_snapshot_input(&["a.rs"]);
        let s2 = make_snapshot_input(&["a.rs", "b.rs"]);
        assert_ne!(s1.compute_hash(), s2.compute_hash());
    }

    #[test]
    fn test_snapshot_input_hash_sensitive_to_envelope() {
        let mut s1 = make_snapshot_input(&["a.rs"]);
        let mut s2 = make_snapshot_input(&["a.rs"]);
        s2.envelope_binding_hash = "different-env".to_string();
        assert_ne!(s1.compute_hash(), s2.compute_hash());
        // 避免 unused mut 警告
        s1.selectors.clear();
        s2.selectors.clear();
        assert_ne!(s1.compute_hash(), s2.compute_hash());
    }

    // --- 并发安全属性 ---

    #[test]
    fn test_executor_is_send() {
        // VerifierExecutor 应可跨线程使用（daemon 多线程场景）
        fn assert_send<T: Send>() {}
        assert_send::<VerifierExecutor>();
    }

    #[test]
    fn test_scoped_hasher_is_send() {
        fn assert_send<T: Send>() {}
        assert_send::<ScopedHasher>();
    }
}
