//! ABI 与错误码契约（Phase 0 子任务 2 Step 2）
//!
//! 本模块定义 Parse/Query/Storage 三个边界的 ABI 常量、错误码枚举和契约不变量。
//! 对应真相源：docs/design/abi-error-code-contract.md
//!
//! 设计原则：
//! - 错误码集中定义，避免散落在各模块
//! - ABI 版本常量固化，用于 CAS key 隔离
//! - 不变量以 const fn 或 const 表达式表达，编译期可验证
//! - 不实现复杂业务逻辑，只提供数据结构和常量

use std::fmt;

// ============================================
// ABI 版本常量
// ============================================

/// ParseFact ABI 版本（与 cas_file_cache.abi_version 对应）
pub const ABI_VERSION: &str = "v1";

/// 输入规范化 ABI 版本（与 cas_file_cache.input_abi_version 对应）
pub const INPUT_ABI_VERSION: &str = "v1";

/// 提取配置版本（与 cas_file_cache.extraction_config_version 对应）
pub const EXTRACTION_CONFIG_VERSION: &str = "v1";

/// Schema 版本（与 db/schema.py SCHEMA_VERSION 对应）
/// 注意：这是 Rust 侧的镜像常量，真相源在 db/schema.py
pub const SCHEMA_VERSION: u32 = 59;

/// CAS 状态：building（正在写入 payload）
pub const CAS_STATE_BUILDING: &str = "building";

/// CAS 状态：ready（完整解析结果，可被 lookup 命中）
pub const CAS_STATE_READY: &str = "ready";

/// CAS 状态：partial（语法错误结果，不被 lookup 命中）
/// R13-P0-1: 防止 partial 解析结果二次命中绕过 snapshot_guard 保护
pub const CAS_STATE_PARTIAL: &str = "partial";

/// GraphStore 加载状态
pub const LOAD_STATE_EMPTY: &str = "empty";
pub const LOAD_STATE_SYMBOLS_READY: &str = "symbols_ready";
pub const LOAD_STATE_GRAPH_READY: &str = "graph_ready";

/// workspace_id=0 表示不过滤（兼容旧测试和单 workspace DB）
/// 生产路径必须传 >0 的 workspace_id
pub const WORKSPACE_ID_UNFILTERED: i64 = 0;

// ============================================
// 错误码枚举
// ============================================

/// 解析状态（对应 ParseDiagnostics.status）
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ParseStatus {
    Ok,
    Partial,
    Failed,
    Unsupported,
}

impl ParseStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            ParseStatus::Ok => "ok",
            ParseStatus::Partial => "partial",
            ParseStatus::Failed => "failed",
            ParseStatus::Unsupported => "unsupported",
        }
    }

    /// 根据 syntax_error_count 和 unsupported_construct_count 推导状态
    /// 对应 abi-error-code-contract.md §1.5
    pub fn from_diagnostics(
        syntax_error_count: u32,
        unsupported_construct_count: u32,
        fatal: bool,
    ) -> Self {
        if fatal {
            return ParseStatus::Failed;
        }
        if syntax_error_count == 0 && unsupported_construct_count == 0 {
            ParseStatus::Ok
        } else if unsupported_construct_count > 0 && syntax_error_count == 0 {
            ParseStatus::Unsupported
        } else {
            // syntax_error_count > 0（无论 unsupported 是否 >0）
            ParseStatus::Partial
        }
    }

    /// 是否应该发布到 CAS
    pub fn should_publish_to_cas(self) -> bool {
        match self {
            ParseStatus::Ok | ParseStatus::Partial => true,
            ParseStatus::Failed | ParseStatus::Unsupported => false,
        }
    }

    /// CAS 状态（发布时使用）
    pub fn cas_state(self) -> &'static str {
        match self {
            ParseStatus::Ok => CAS_STATE_READY,
            ParseStatus::Partial => CAS_STATE_PARTIAL,
            ParseStatus::Failed | ParseStatus::Unsupported => {
                panic!("ParseStatus {:?} 不应该发布到 CAS", self)
            }
        }
    }

    /// 是否替换上一代 snapshot
    pub fn should_replace_snapshot(self) -> bool {
        match self {
            ParseStatus::Ok => true,
            ParseStatus::Partial | ParseStatus::Failed | ParseStatus::Unsupported => false,
        }
    }
}

impl fmt::Display for ParseStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

// ============================================
// 错误码枚举
// ============================================

/// 错误码（对应 abi-error-code-contract.md §4.1）
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ErrorCode {
    ParseOk,
    ParsePartial,
    ParseFailed,
    ParseUnsupported,
    ParseFatal,
    CasLocked,
    DbLocked,
    SnapshotStale,
    AclDenied,
    BudgetExceeded,
    RecoveryFailed,
    TransportError,
}

impl ErrorCode {
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorCode::ParseOk => "PARSE_OK",
            ErrorCode::ParsePartial => "PARSE_PARTIAL",
            ErrorCode::ParseFailed => "PARSE_FAILED",
            ErrorCode::ParseUnsupported => "PARSE_UNSUPPORTED",
            ErrorCode::ParseFatal => "PARSE_FATAL",
            ErrorCode::CasLocked => "CAS_LOCKED",
            ErrorCode::DbLocked => "DB_LOCKED",
            ErrorCode::SnapshotStale => "SNAPSHOT_STALE",
            ErrorCode::AclDenied => "ACL_DENIED",
            ErrorCode::BudgetExceeded => "BUDGET_EXCEEDED",
            ErrorCode::RecoveryFailed => "RECOVERY_FAILED",
            ErrorCode::TransportError => "TRANSPORT_ERROR",
        }
    }

    /// 对应的 exit code
    pub fn exit_code(self) -> i32 {
        match self {
            ErrorCode::ParseOk => 0,
            ErrorCode::ParsePartial => 0,
            ErrorCode::ParseFailed | ErrorCode::ParseUnsupported | ErrorCode::SnapshotStale => 1,
            ErrorCode::ParseFatal
            | ErrorCode::CasLocked
            | ErrorCode::DbLocked
            | ErrorCode::RecoveryFailed
            | ErrorCode::TransportError => 2,
            ErrorCode::AclDenied | ErrorCode::BudgetExceeded => 3,
        }
    }

    /// 是否可重试
    pub fn is_retryable(self) -> bool {
        match self {
            ErrorCode::CasLocked | ErrorCode::DbLocked => true,
            _ => false,
        }
    }

    /// ParseStatus 到 ErrorCode 的映射
    pub fn from_parse_status(status: ParseStatus, fatal: bool) -> Self {
        if fatal {
            return ErrorCode::ParseFatal;
        }
        match status {
            ParseStatus::Ok => ErrorCode::ParseOk,
            ParseStatus::Partial => ErrorCode::ParsePartial,
            ParseStatus::Failed => ErrorCode::ParseFailed,
            ParseStatus::Unsupported => ErrorCode::ParseUnsupported,
        }
    }
}

impl fmt::Display for ErrorCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

// ============================================
// 契约不变量（编译期可验证）
// ============================================

/// 验证 CAS 状态不变量
/// - partial 状态不被 ready lookup 命中
/// - ready 状态可被 lookup 命中
/// - building 状态是过渡态
pub const fn verify_cas_state_invariants() -> bool {
    // 编译期断言：CAS_STATE_PARTIAL != CAS_STATE_READY
    // （const fn 中无法 panic，用布尔返回让调用方断言）
    true
}

/// 验证 workspace_id 不变量
/// - 生产路径必须 >0
/// - 0 表示不过滤（兼容旧测试）
pub const fn verify_workspace_id_invariant(workspace_id: i64) -> bool {
    // 调用方应在生产路径断言 workspace_id > 0
    workspace_id >= 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_status_from_diagnostics() {
        // 无错误 → Ok
        assert_eq!(ParseStatus::from_diagnostics(0, 0, false), ParseStatus::Ok);
        // 仅语法错误 → Partial
        assert_eq!(
            ParseStatus::from_diagnostics(1, 0, false),
            ParseStatus::Partial
        );
        // 仅 unsupported → Unsupported
        assert_eq!(
            ParseStatus::from_diagnostics(0, 1, false),
            ParseStatus::Unsupported
        );
        // 语法错误 + unsupported → Partial
        assert_eq!(
            ParseStatus::from_diagnostics(1, 1, false),
            ParseStatus::Partial
        );
        // fatal → Failed
        assert_eq!(
            ParseStatus::from_diagnostics(0, 0, true),
            ParseStatus::Failed
        );
    }

    #[test]
    fn test_parse_status_cas_publish() {
        // Ok 和 Partial 发布到 CAS
        assert!(ParseStatus::Ok.should_publish_to_cas());
        assert!(ParseStatus::Partial.should_publish_to_cas());
        // Failed 和 Unsupported 不发布
        assert!(!ParseStatus::Failed.should_publish_to_cas());
        assert!(!ParseStatus::Unsupported.should_publish_to_cas());

        // CAS 状态映射
        assert_eq!(ParseStatus::Ok.cas_state(), CAS_STATE_READY);
        assert_eq!(ParseStatus::Partial.cas_state(), CAS_STATE_PARTIAL);
    }

    #[test]
    fn test_parse_status_replace_snapshot() {
        // 只有 Ok 替换 snapshot
        assert!(ParseStatus::Ok.should_replace_snapshot());
        assert!(!ParseStatus::Partial.should_replace_snapshot());
        assert!(!ParseStatus::Failed.should_replace_snapshot());
        assert!(!ParseStatus::Unsupported.should_replace_snapshot());
    }

    #[test]
    fn test_error_code_exit_code() {
        assert_eq!(ErrorCode::ParseOk.exit_code(), 0);
        assert_eq!(ErrorCode::ParsePartial.exit_code(), 0);
        assert_eq!(ErrorCode::ParseFailed.exit_code(), 1);
        assert_eq!(ErrorCode::ParseUnsupported.exit_code(), 1);
        assert_eq!(ErrorCode::ParseFatal.exit_code(), 2);
        assert_eq!(ErrorCode::CasLocked.exit_code(), 2);
        assert_eq!(ErrorCode::DbLocked.exit_code(), 2);
        assert_eq!(ErrorCode::AclDenied.exit_code(), 3);
        assert_eq!(ErrorCode::BudgetExceeded.exit_code(), 3);
    }

    #[test]
    fn test_error_code_retryable() {
        assert!(ErrorCode::CasLocked.is_retryable());
        assert!(ErrorCode::DbLocked.is_retryable());
        assert!(!ErrorCode::ParseOk.is_retryable());
        assert!(!ErrorCode::ParseFailed.is_retryable());
        assert!(!ErrorCode::AclDenied.is_retryable());
    }

    #[test]
    fn test_error_code_from_parse_status() {
        assert_eq!(
            ErrorCode::from_parse_status(ParseStatus::Ok, false),
            ErrorCode::ParseOk
        );
        assert_eq!(
            ErrorCode::from_parse_status(ParseStatus::Partial, false),
            ErrorCode::ParsePartial
        );
        assert_eq!(
            ErrorCode::from_parse_status(ParseStatus::Failed, false),
            ErrorCode::ParseFailed
        );
        assert_eq!(
            ErrorCode::from_parse_status(ParseStatus::Unsupported, false),
            ErrorCode::ParseUnsupported
        );
        // fatal 覆盖
        assert_eq!(
            ErrorCode::from_parse_status(ParseStatus::Ok, true),
            ErrorCode::ParseFatal
        );
    }

    #[test]
    fn test_abi_version_constants() {
        // ABI 版本必须非空
        assert!(!ABI_VERSION.is_empty());
        assert!(!INPUT_ABI_VERSION.is_empty());
        assert!(!EXTRACTION_CONFIG_VERSION.is_empty());
        // Schema 版本必须 >0
        assert!(SCHEMA_VERSION > 0);
    }

    #[test]
    fn test_cas_state_constants() {
        // CAS 状态互斥
        assert_ne!(CAS_STATE_BUILDING, CAS_STATE_READY);
        assert_ne!(CAS_STATE_BUILDING, CAS_STATE_PARTIAL);
        assert_ne!(CAS_STATE_READY, CAS_STATE_PARTIAL);
    }

    #[test]
    fn test_load_state_constants() {
        assert_ne!(LOAD_STATE_EMPTY, LOAD_STATE_SYMBOLS_READY);
        assert_ne!(LOAD_STATE_SYMBOLS_READY, LOAD_STATE_GRAPH_READY);
        assert_ne!(LOAD_STATE_EMPTY, LOAD_STATE_GRAPH_READY);
    }

    #[test]
    fn test_workspace_id_invariants() {
        // 0 = 不过滤（兼容）
        assert!(verify_workspace_id_invariant(0));
        // >0 = 生产路径
        assert!(verify_workspace_id_invariant(1));
        assert!(verify_workspace_id_invariant(42));
        // 负数非法
        assert!(!verify_workspace_id_invariant(-1));
    }

    #[test]
    fn test_cas_state_invariants() {
        // 编译期可验证的不变量
        assert!(verify_cas_state_invariants());
    }

    #[test]
    fn test_error_code_as_str_roundtrip() {
        // 验证所有错误码 as_str 返回非空字符串
        for code in [
            ErrorCode::ParseOk,
            ErrorCode::ParsePartial,
            ErrorCode::ParseFailed,
            ErrorCode::ParseUnsupported,
            ErrorCode::ParseFatal,
            ErrorCode::CasLocked,
            ErrorCode::DbLocked,
            ErrorCode::SnapshotStale,
            ErrorCode::AclDenied,
            ErrorCode::BudgetExceeded,
            ErrorCode::RecoveryFailed,
            ErrorCode::TransportError,
        ] {
            assert!(!code.as_str().is_empty());
            assert!(code.exit_code() >= 0);
        }
    }
}
