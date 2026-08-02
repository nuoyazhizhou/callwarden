//! Python/Rust 差分对照基线数据结构（Phase 0 子任务 3 Step 2）
//!
//! 本模块定义差分对照的数据结构和性能基线常量。
//! 对应真相源：docs/design/differential-harness-contract.md
//!
//! 设计原则：
//! - 数据结构对齐 Python baseline.json 结构
//! - 性能基线常量集中定义，用于回归检测
//! - 不实现复杂业务逻辑，只提供数据结构
//! - 后续 Phase 1+ 可通过 PyO3 暴露给 Python harness

use std::collections::HashMap;
use std::fmt;

// ============================================
// 性能基线常量（对应契约 §4）
// ============================================

/// 单文件 parse P50 目标（ms）
pub const PARSE_P50_TARGET_MS: f64 = 100.0;

/// 单文件 parse P95 目标（ms）
pub const PARSE_P95_TARGET_MS: f64 = 200.0;

/// GraphStore 加载 1M 符号 P50 目标（ms）
pub const GRAPHSTORE_LOAD_P50_TARGET_MS: f64 = 5000.0;

/// GraphStore 加载 1M 符号 P95 目标（ms）
pub const GRAPHSTORE_LOAD_P95_TARGET_MS: f64 = 10000.0;

/// GraphStore get_callers P50 目标（ms）
pub const GET_CALLERS_P50_TARGET_MS: f64 = 1.0;

/// GraphStore get_callers P95 目标（ms）
pub const GET_CALLERS_P95_TARGET_MS: f64 = 5.0;

/// watcher 单文件更新 P95 目标（ms）
pub const WATCHER_UPDATE_P95_TARGET_MS: f64 = 3000.0;

/// 性能 P50 回归阈值（1.5x）
pub const PERF_P50_REGRESSION_THRESHOLD: f64 = 1.5;

/// 性能 P95 回归阈值（2.0x）
pub const PERF_P95_REGRESSION_THRESHOLD: f64 = 2.0;

/// 内存 RSS 回归阈值（1.5x）
pub const RSS_REGRESSION_THRESHOLD: f64 = 1.5;

/// 二进制体积回归阈值（1.2x）
pub const BINARY_SIZE_REGRESSION_THRESHOLD: f64 = 1.2;

// ============================================
// Phase 0 完成门
// ============================================

/// Phase 0 4 个完成门名称
pub const PHASE0_GATE_TYPESCRIPT: &str = "tests_expose_typescript_gap";
pub const PHASE0_GATE_PHP: &str = "tests_expose_php_gap";
pub const PHASE0_GATE_SCALA: &str = "tests_expose_scala_gap";
pub const PHASE0_GATE_HCL: &str = "tests_expose_hcl_gap";

// ============================================
// 单语言能力快照
// ============================================

/// 单语言的 Python/Rust 双实现能力快照
/// 对应 baseline.json 的 language_capability.<lang>
#[derive(Debug, Clone, PartialEq)]
pub struct LanguageCapability {
    pub language: String,
    pub rust_supported: bool,
    pub python_parser_available: bool,
    pub sample_path: String,
    pub symbols_count_py: u32,
    pub symbols_count_rs: u32,
    pub kinds_py: Vec<String>,
    pub kinds_rs: Vec<String>,
    pub signature_present_py: bool,
    pub signature_present_rs: bool,
    pub visibility_present_py: bool,
    pub visibility_present_rs: bool,
    pub calls_count_py: u32,
    pub calls_count_rs: u32,
    pub imports_count_py: u32,
    pub imports_count_rs: u32,
    pub references_present_py: bool,
    pub references_present_rs: bool,
    pub rust_module_path: bool,
    pub python_module_path: bool,
    pub known_symbol_diffs_count: u32,
    pub known_call_diffs_count: u32,
    pub known_symbol_diffs_reason: String,
    pub known_call_diffs_reason: String,
    pub gaps: Vec<String>,
}

impl LanguageCapability {
    /// 创建默认能力快照（所有字段为空/false）
    pub fn new(language: &str) -> Self {
        Self {
            language: language.to_string(),
            rust_supported: false,
            python_parser_available: false,
            sample_path: String::new(),
            symbols_count_py: 0,
            symbols_count_rs: 0,
            kinds_py: Vec::new(),
            kinds_rs: Vec::new(),
            signature_present_py: false,
            signature_present_rs: false,
            visibility_present_py: false,
            visibility_present_rs: false,
            calls_count_py: 0,
            calls_count_rs: 0,
            imports_count_py: 0,
            imports_count_rs: 0,
            references_present_py: false,
            references_present_rs: false,
            rust_module_path: false,
            python_module_path: false,
            known_symbol_diffs_count: 0,
            known_call_diffs_count: 0,
            known_symbol_diffs_reason: String::new(),
            known_call_diffs_reason: String::new(),
            gaps: Vec::new(),
        }
    }

    /// 符号数差异（绝对值）
    pub fn symbol_count_diff(&self) -> i64 {
        self.symbols_count_py as i64 - self.symbols_count_rs as i64
    }

    /// 调用数差异（绝对值）
    pub fn call_count_diff(&self) -> i64 {
        self.calls_count_py as i64 - self.calls_count_rs as i64
    }

    /// import 数差异（绝对值）
    pub fn import_count_diff(&self) -> i64 {
        self.imports_count_py as i64 - self.imports_count_rs as i64
    }

    /// 是否存在任何差异（排除已知差异后）
    pub fn has_diff(&self) -> bool {
        self.symbol_count_diff() != 0
            || self.call_count_diff() != 0
            || self.import_count_diff() != 0
            || !self.gaps.is_empty()
    }

    /// 是否存在已知差异
    pub fn has_known_diffs(&self) -> bool {
        self.known_symbol_diffs_count > 0 || self.known_call_diffs_count > 0
    }
}

// ============================================
// 性能基线指标
// ============================================

/// 性能基线指标快照
/// 对应 baseline.json 的 performance_baseline
#[derive(Debug, Clone, PartialEq)]
pub struct PerformanceBaseline {
    pub parse_p50_ms: f64,
    pub parse_p95_ms: f64,
    pub graphstore_load_p50_ms: f64,
    pub graphstore_load_p95_ms: f64,
    pub get_callers_p50_ms: f64,
    pub get_callers_p95_ms: f64,
    pub watcher_update_p95_ms: f64,
    pub build_full_graph_p50_ms: f64,
    pub build_full_graph_p95_ms: f64,
}

impl Default for PerformanceBaseline {
    fn default() -> Self {
        Self {
            parse_p50_ms: 0.0,
            parse_p95_ms: 0.0,
            graphstore_load_p50_ms: 0.0,
            graphstore_load_p95_ms: 0.0,
            get_callers_p50_ms: 0.0,
            get_callers_p95_ms: 0.0,
            watcher_update_p95_ms: 0.0,
            build_full_graph_p50_ms: 0.0,
            build_full_graph_p95_ms: 0.0,
        }
    }
}

impl PerformanceBaseline {
    /// 验证性能指标是否满足目标
    pub fn verify_targets(&self) -> Vec<PerformanceViolation> {
        let mut violations = Vec::new();

        macro_rules! check {
            ($field:ident, $target:expr, $name:expr) => {
                if self.$field > $target {
                    violations.push(PerformanceViolation {
                        metric: $name.to_string(),
                        value: self.$field,
                        target: $target,
                        ratio: self.$field / $target,
                    });
                }
            };
        }

        check!(parse_p50_ms, PARSE_P50_TARGET_MS, "parse_p50_ms");
        check!(parse_p95_ms, PARSE_P95_TARGET_MS, "parse_p95_ms");
        check!(
            graphstore_load_p50_ms,
            GRAPHSTORE_LOAD_P50_TARGET_MS,
            "graphstore_load_p50_ms"
        );
        check!(
            graphstore_load_p95_ms,
            GRAPHSTORE_LOAD_P95_TARGET_MS,
            "graphstore_load_p95_ms"
        );
        check!(
            get_callers_p50_ms,
            GET_CALLERS_P50_TARGET_MS,
            "get_callers_p50_ms"
        );
        check!(
            get_callers_p95_ms,
            GET_CALLERS_P95_TARGET_MS,
            "get_callers_p95_ms"
        );
        check!(
            watcher_update_p95_ms,
            WATCHER_UPDATE_P95_TARGET_MS,
            "watcher_update_p95_ms"
        );

        violations
    }
}

/// 性能违规项
#[derive(Debug, Clone, PartialEq)]
pub struct PerformanceViolation {
    pub metric: String,
    pub value: f64,
    pub target: f64,
    pub ratio: f64,
}

impl fmt::Display for PerformanceViolation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}: {:.2}ms > target {:.2}ms (ratio {:.2})",
            self.metric, self.value, self.target, self.ratio
        )
    }
}

// ============================================
// 回归检测结果
// ============================================

/// 性能回归检测项
#[derive(Debug, Clone, PartialEq)]
pub struct Regression {
    pub metric: String,
    pub baseline_value: f64,
    pub current_value: f64,
    pub ratio: f64,
    pub threshold: f64,
    pub is_regression: bool,
}

impl Regression {
    /// 检测是否回归
    pub fn detect(metric: &str, baseline: f64, current: f64, threshold: f64) -> Self {
        let ratio = if baseline > 0.0 {
            current / baseline
        } else {
            if current > 0.0 {
                f64::INFINITY
            } else {
                1.0
            }
        };
        Self {
            metric: metric.to_string(),
            baseline_value: baseline,
            current_value: current,
            ratio,
            threshold,
            is_regression: ratio > threshold,
        }
    }
}

/// 基线验证结果
/// 对应契约 §2.4 BaselineVerification
#[derive(Debug, Clone)]
pub struct BaselineVerification {
    pub baseline_commit: String,
    pub current_commit: String,
    pub is_consistent: bool,
    pub has_performance_regression: bool,
    pub regressions: Vec<Regression>,
    pub new_gaps: Vec<String>,
    pub fixed_gaps: Vec<String>,
}

impl BaselineVerification {
    pub fn new(baseline_commit: &str, current_commit: &str) -> Self {
        Self {
            baseline_commit: baseline_commit.to_string(),
            current_commit: current_commit.to_string(),
            is_consistent: true,
            has_performance_regression: false,
            regressions: Vec::new(),
            new_gaps: Vec::new(),
            fixed_gaps: Vec::new(),
        }
    }

    /// 添加性能回归检测项
    pub fn add_regression(&mut self, reg: Regression) {
        if reg.is_regression {
            self.has_performance_regression = true;
            self.is_consistent = false;
        }
        self.regressions.push(reg);
    }

    /// 添加新发现的缺口
    pub fn add_new_gap(&mut self, gap: String) {
        self.new_gaps.push(gap);
        self.is_consistent = false;
    }

    /// 添加已修复的缺口
    pub fn add_fixed_gap(&mut self, gap: String) {
        self.fixed_gaps.push(gap);
    }
}

// ============================================
// 已知差异
// ============================================

/// 已知差异声明
/// 对应契约 §6.2
#[derive(Debug, Clone, PartialEq)]
pub struct KnownDiff {
    pub parser: String, // "rust" or "python"
    pub field: String,  // e.g. "signature"
    pub description: String,
    pub phase: String, // e.g. "Phase 2.7"
    pub reason: String,
    pub fix_commit: Option<String>,
}

impl KnownDiff {
    pub fn new(parser: &str, field: &str, description: &str, phase: &str) -> Self {
        Self {
            parser: parser.to_string(),
            field: field.to_string(),
            description: description.to_string(),
            phase: phase.to_string(),
            reason: String::new(),
            fix_commit: None,
        }
    }

    /// 是否已修复
    pub fn is_fixed(&self) -> bool {
        self.fix_commit.is_some()
    }
}

// ============================================
// 基线快照
// ============================================

/// 完整的基线快照
/// 对应 baseline.json 顶层结构
#[derive(Debug, Clone)]
pub struct BaselineSnapshot {
    pub generated_at: String,
    pub commit_sha: String,
    pub platform: HashMap<String, String>,
    pub language_capability: HashMap<String, LanguageCapability>,
    pub phase0_completion_gates: HashMap<String, bool>,
    pub performance_baseline: PerformanceBaseline,
}

impl BaselineSnapshot {
    pub fn new(commit_sha: &str) -> Self {
        let mut gates = HashMap::new();
        gates.insert(PHASE0_GATE_TYPESCRIPT.to_string(), true);
        gates.insert(PHASE0_GATE_PHP.to_string(), true);
        gates.insert(PHASE0_GATE_SCALA.to_string(), true);
        gates.insert(PHASE0_GATE_HCL.to_string(), true);

        Self {
            generated_at: String::new(),
            commit_sha: commit_sha.to_string(),
            platform: HashMap::new(),
            language_capability: HashMap::new(),
            phase0_completion_gates: gates,
            performance_baseline: PerformanceBaseline::default(),
        }
    }

    /// 检查所有 Phase 0 完成门是否通过（gate=True 表示暴露了缺口）
    pub fn phase0_gates_exposed(&self) -> bool {
        self.phase0_completion_gates.values().all(|v| *v)
    }

    /// 获取指定语言的能力快照
    pub fn get_capability(&self, lang: &str) -> Option<&LanguageCapability> {
        self.language_capability.get(lang)
    }

    /// 验证 commit_sha 是否与当前一致
    pub fn verify_commit(&self, current_sha: &str) -> bool {
        self.commit_sha == current_sha
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_language_capability_default() {
        let cap = LanguageCapability::new("rust");
        assert_eq!(cap.language, "rust");
        assert!(!cap.rust_supported);
        assert_eq!(cap.symbols_count_py, 0);
        assert_eq!(cap.symbols_count_rs, 0);
        assert!(!cap.has_diff());
    }

    #[test]
    fn test_language_capability_diff() {
        let mut cap = LanguageCapability::new("rust");
        cap.symbols_count_py = 10;
        cap.symbols_count_rs = 8;
        assert_eq!(cap.symbol_count_diff(), 2);
        assert!(cap.has_diff());
    }

    #[test]
    fn test_language_capability_known_diffs() {
        let mut cap = LanguageCapability::new("rust");
        cap.known_symbol_diffs_count = 2;
        assert!(cap.has_known_diffs());
        // 已知差异不视为实际差异
        assert!(!cap.has_diff());
    }

    #[test]
    fn test_performance_baseline_verify_targets_pass() {
        let baseline = PerformanceBaseline {
            parse_p50_ms: 50.0,
            parse_p95_ms: 100.0,
            graphstore_load_p50_ms: 3000.0,
            graphstore_load_p95_ms: 8000.0,
            get_callers_p50_ms: 0.5,
            get_callers_p95_ms: 3.0,
            watcher_update_p95_ms: 2000.0,
            build_full_graph_p50_ms: 0.0,
            build_full_graph_p95_ms: 0.0,
        };
        let violations = baseline.verify_targets();
        assert!(violations.is_empty());
    }

    #[test]
    fn test_performance_baseline_verify_targets_fail() {
        let baseline = PerformanceBaseline {
            parse_p50_ms: 150.0, // > 100
            parse_p95_ms: 100.0,
            graphstore_load_p50_ms: 3000.0,
            graphstore_load_p95_ms: 8000.0,
            get_callers_p50_ms: 0.5,
            get_callers_p95_ms: 3.0,
            watcher_update_p95_ms: 2000.0,
            build_full_graph_p50_ms: 0.0,
            build_full_graph_p95_ms: 0.0,
        };
        let violations = baseline.verify_targets();
        assert_eq!(violations.len(), 1);
        assert_eq!(violations[0].metric, "parse_p50_ms");
        assert_eq!(violations[0].value, 150.0);
        assert_eq!(violations[0].target, 100.0);
        assert!((violations[0].ratio - 1.5).abs() < 0.01);
    }

    #[test]
    fn test_regression_detect_no_regression() {
        let reg = Regression::detect("parse_p50", 100.0, 120.0, 1.5);
        assert!(!reg.is_regression);
        assert!((reg.ratio - 1.2).abs() < 0.01);
    }

    #[test]
    fn test_regression_detect_regression() {
        let reg = Regression::detect("parse_p50", 100.0, 200.0, 1.5);
        assert!(reg.is_regression);
        assert!((reg.ratio - 2.0).abs() < 0.01);
    }

    #[test]
    fn test_regression_detect_zero_baseline() {
        let reg = Regression::detect("parse_p50", 0.0, 100.0, 1.5);
        assert!(reg.is_regression);
        assert!(reg.ratio.is_infinite());
    }

    #[test]
    fn test_regression_detect_zero_both() {
        let reg = Regression::detect("parse_p50", 0.0, 0.0, 1.5);
        assert!(!reg.is_regression);
        assert!((reg.ratio - 1.0).abs() < 0.01);
    }

    #[test]
    fn test_baseline_verification_new() {
        let ver = BaselineVerification::new("abc123", "def456");
        assert!(ver.is_consistent);
        assert!(!ver.has_performance_regression);
        assert!(ver.regressions.is_empty());
    }

    #[test]
    fn test_baseline_verification_add_regression() {
        let mut ver = BaselineVerification::new("abc123", "def456");
        let reg = Regression::detect("parse_p50", 100.0, 200.0, 1.5);
        ver.add_regression(reg);
        assert!(ver.has_performance_regression);
        assert!(!ver.is_consistent);
        assert_eq!(ver.regressions.len(), 1);
    }

    #[test]
    fn test_baseline_verification_add_new_gap() {
        let mut ver = BaselineVerification::new("abc123", "def456");
        ver.add_new_gap("new gap".to_string());
        assert!(!ver.is_consistent);
        assert_eq!(ver.new_gaps.len(), 1);
    }

    #[test]
    fn test_known_diff_new() {
        let diff = KnownDiff::new("rust", "signature", "not implemented", "Phase 2.7");
        assert_eq!(diff.parser, "rust");
        assert_eq!(diff.field, "signature");
        assert!(!diff.is_fixed());
    }

    #[test]
    fn test_known_diff_fixed() {
        let mut diff = KnownDiff::new("rust", "signature", "not implemented", "Phase 2.7");
        diff.fix_commit = Some("abc123".to_string());
        assert!(diff.is_fixed());
    }

    #[test]
    fn test_baseline_snapshot_new() {
        let snap = BaselineSnapshot::new("abc123");
        assert_eq!(snap.commit_sha, "abc123");
        assert!(snap.phase0_gates_exposed());
        assert!(snap.verify_commit("abc123"));
        assert!(!snap.verify_commit("def456"));
    }

    #[test]
    fn test_performance_target_constants() {
        assert!(PARSE_P50_TARGET_MS > 0.0);
        assert!(PARSE_P95_TARGET_MS > PARSE_P50_TARGET_MS);
        assert!(GRAPHSTORE_LOAD_P50_TARGET_MS > PARSE_P50_TARGET_MS);
        assert!(GET_CALLERS_P50_TARGET_MS < PARSE_P50_TARGET_MS);
        assert!(WATCHER_UPDATE_P95_TARGET_MS > PARSE_P95_TARGET_MS);
    }

    #[test]
    fn test_regression_threshold_constants() {
        assert!(PERF_P50_REGRESSION_THRESHOLD > 1.0);
        assert!(PERF_P95_REGRESSION_THRESHOLD > PERF_P50_REGRESSION_THRESHOLD);
        assert!(RSS_REGRESSION_THRESHOLD > 1.0);
        assert!(BINARY_SIZE_REGRESSION_THRESHOLD > 1.0);
    }

    #[test]
    fn test_phase0_gate_constants() {
        assert_eq!(PHASE0_GATE_TYPESCRIPT, "tests_expose_typescript_gap");
        assert_eq!(PHASE0_GATE_PHP, "tests_expose_php_gap");
        assert_eq!(PHASE0_GATE_SCALA, "tests_expose_scala_gap");
        assert_eq!(PHASE0_GATE_HCL, "tests_expose_hcl_gap");
    }
}
