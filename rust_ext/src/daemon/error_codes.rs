//! 稳定错误码目录 + 双语 message key（Req 1.12, 5.14–5.15, 7.16, 10.12, 14.30, 14.36）。
//!
//! ## 职责
//! - 每个 Structured_Reason 携带一个稳定错误码和一个 i18n message key
//! - 文案变化不改变错误码
//! - 覆盖 D0 已知拒绝路径 + 警告码 + 跨类操作 + Independence_Policy + Revocation_Mode
//!
//! ## 设计原则
//! - 错误码是稳定标识符（`E_` 前缀 = 错误，`W_` 前缀 = 警告）
//! - message key 是 i18n 查找键（`error.*` / `warning.*`）
//! - 错误码与 message key 一一对应，但文案内容可变
//! - 警告码非阻断（不改变操作的接受或拒绝语义）

/// 错误码条目——稳定码 + i18n message key。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ErrorCodeEntry {
    /// 稳定错误码（文案变化不改变此值）
    pub code: &'static str,
    /// i18n message key（zh_CN 与 en_US 均可解析）
    pub message_key: &'static str,
    /// 是否为警告码（非阻断）
    pub is_warning: bool,
    /// 简短描述（开发参考，不面向用户）
    pub description: &'static str,
}

// ---------------------------------------------------------------------------
// D0 已知拒绝路径错误码
// ---------------------------------------------------------------------------

/// SID 不匹配（Windows 命名管道对端 SID 与预期不符）。
pub const E_SID_MISMATCH: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_SID_MISMATCH",
    message_key: "error.sid_mismatch",
    is_warning: false,
    description: "Windows named pipe peer SID does not match expected owner",
};

/// Peer_Credential 不可获取（SO_PEERCRED / ImpersonateNamedPipeClient 失败）。
pub const E_PEER_CREDENTIAL_UNAVAILABLE: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_PEER_CREDENTIAL_UNAVAILABLE",
    message_key: "error.peer_credential_unavailable",
    is_warning: false,
    description: "Cannot obtain peer credential from transport",
};

/// 载荷尺寸不符（消息超过 DEFAULT_MAX_MESSAGE_BYTES）。
pub const E_PAYLOAD_SIZE_EXCEEDED: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_PAYLOAD_SIZE_EXCEEDED",
    message_key: "error.payload_size_exceeded",
    is_warning: false,
    description: "Message payload exceeds maximum allowed size",
};

/// 载荷摘要不符（CAS 四阶段中 digest 校验失败）。
pub const E_PAYLOAD_DIGEST_MISMATCH: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_PAYLOAD_DIGEST_MISMATCH",
    message_key: "error.payload_digest_mismatch",
    is_warning: false,
    description: "Content digest does not match declared hash",
};

/// 请求超时（串行化点等待超时）。
pub const E_REQUEST_TIMEOUT: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_REQUEST_TIMEOUT",
    message_key: "error.request_timeout",
    is_warning: false,
    description: "Serialization point wait timeout exceeded",
};

/// Attestation 签发失败（HMAC 计算或密钥不可用）。
pub const E_ATTESTATION_ISSUANCE_FAILED: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_ATTESTATION_ISSUANCE_FAILED",
    message_key: "error.attestation_issuance_failed",
    is_warning: false,
    description: "Attestation HMAC signing failed",
};

/// Attestation 越窗（expires_at 已过期）。
pub const E_ATTESTATION_EXPIRED: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_ATTESTATION_EXPIRED",
    message_key: "error.attestation_expired",
    is_warning: false,
    description: "Attestation validity window has expired",
};

/// Attestation 校验失败（签名不匹配或绑定字段不一致）。
pub const E_ATTESTATION_INVALID: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_ATTESTATION_INVALID",
    message_key: "error.attestation_invalid",
    is_warning: false,
    description: "Attestation signature or binding validation failed",
};

/// Stage_Toggle 前置缺失（启用高阶段时前置阶段未启用）。
pub const E_STAGE_TOGGLE_PREREQUISITE_MISSING: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_STAGE_TOGGLE_PREREQUISITE_MISSING",
    message_key: "error.stage_toggle_prerequisite_missing",
    is_warning: false,
    description: "Cannot enable stage: prerequisite stage not enabled",
};

/// Degraded_Mode 下 Governance_Write 被拒。
pub const E_GOVERNANCE_WRITE_DEGRADED: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_GOVERNANCE_WRITE_DEGRADED",
    message_key: "error.governance_write_degraded",
    is_warning: false,
    description: "Governance write rejected in Degraded_Mode (daemon unavailable)",
};

// ---------------------------------------------------------------------------
// 警告码（非阻断）
// ---------------------------------------------------------------------------

/// 空 scope 发布警告 [Req 7.15]。
/// 警告码非阻断：不改变操作的接受或拒绝语义。
pub const W_EMPTY_SCOPE_PUBLISH: ErrorCodeEntry = ErrorCodeEntry {
    code: "W_EMPTY_SCOPE_PUBLISH",
    message_key: "warning.empty_scope_publish",
    is_warning: true,
    description: "Envelope published with empty relevant scope",
};

// ---------------------------------------------------------------------------
// 跨类操作组成部分错误码 [Req 14.35, 14.37]
// ---------------------------------------------------------------------------

/// 跨类操作部分执行：索引组成部分成功，治理组成部分被拒。
pub const E_CROSS_CLASS_PARTIAL_REJECTION: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_CROSS_CLASS_PARTIAL_REJECTION",
    message_key: "error.cross_class_partial_rejection",
    is_warning: false,
    description: "Cross-class operation: index component executed, governance component rejected",
};

// ---------------------------------------------------------------------------
// Independence_Policy 相关码
// ---------------------------------------------------------------------------

/// high_risk profile 拒绝 solo 审核。
pub const E_INDEPENDENCE_POLICY_SOLO_REJECTED: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_INDEPENDENCE_POLICY_SOLO_REJECTED",
    message_key: "error.independence_policy_solo_rejected",
    is_warning: false,
    description: "high_risk profile does not allow solo review",
};

/// 独立审核按政策豁免（标识码，不表述为"独立性已证明"）。
pub const INDEPENDENCE_EXEMPTION_BY_POLICY: ErrorCodeEntry = ErrorCodeEntry {
    code: "I_INDEPENDENCE_EXEMPTION_BY_POLICY",
    message_key: "info.independence_exemption_by_policy",
    is_warning: false, // 信息码，非错误非警告
    description: "Independent review exempted by policy (not a proof of independence)",
};

// ---------------------------------------------------------------------------
// Revocation_Mode 相关码 [Req 8.2, 8.7]
// ---------------------------------------------------------------------------

/// Attestation 撤销请求缺少 Revocation_Mode。
/// message 必须提示显式指定 compromised 或 rotated，不得暗示系统会取默认值。
pub const E_REVOCATION_MODE_MISSING: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_REVOCATION_MODE_MISSING",
    message_key: "error.revocation_mode_missing",
    is_warning: false,
    description: "Revocation request must explicitly specify mode: compromised or rotated",
};

// ---------------------------------------------------------------------------
// 收敛架构错误码（T01，cw-rust-client-convergence-design.md §8.6）
// ---------------------------------------------------------------------------

/// 工具显式废弃（路由矩阵 target_backend=declared_unavailable，仍占路由数）。
/// 客户端收到后不得重试本地实现；结构化 code=E_TOOL_DEPRECATED。
pub const E_TOOL_DEPRECATED: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_TOOL_DEPRECATED",
    message_key: "error.tool_deprecated",
    is_warning: false,
    description: "Tool explicitly deprecated in convergence architecture; no local fallback",
};

/// daemon 模式废弃（local/legacy 仅 CW_TEST_MODE=1 下可用）。
/// 生产环境显式配置 local/legacy 一律视为配置错误（fail-closed）。
pub const E_MODE_DEPRECATED: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_MODE_DEPRECATED",
    message_key: "error.mode_deprecated",
    is_warning: false,
    description: "Daemon mode local/legacy deprecated outside CW_TEST_MODE=1",
};

/// 工具迁移待定（路由矩阵已登记但 handler 尚未落地）。
/// 与 E_TOOL_DEPRECATED 不同：本码表示路由存在但实现未完成，客户端应
/// 稍后重试而非认为工具永久废弃。
pub const E_TOOL_MIGRATION_PENDING: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_TOOL_MIGRATION_PENDING",
    message_key: "error.tool_migration_pending",
    is_warning: false,
    description: "Tool route registered in matrix but native handler not yet landed",
};

/// HTTP daemon 不可用（fail-closed：绝不回退本地 SQLite/CodeGraphDB）。
pub const E_HTTP_DAEMON_UNAVAILABLE: ErrorCodeEntry = ErrorCodeEntry {
    code: "E_HTTP_DAEMON_UNAVAILABLE",
    message_key: "error.http_daemon_unavailable",
    is_warning: false,
    description: "HTTP daemon unavailable; client must fail closed, no local fallback",
};

// ---------------------------------------------------------------------------
// 错误码目录（全量）
// ---------------------------------------------------------------------------

/// 所有已发布的错误码/警告码/信息码目录。
pub const ERROR_CODE_DIRECTORY: &[&ErrorCodeEntry] = &[
    // D0 拒绝路径
    &E_SID_MISMATCH,
    &E_PEER_CREDENTIAL_UNAVAILABLE,
    &E_PAYLOAD_SIZE_EXCEEDED,
    &E_PAYLOAD_DIGEST_MISMATCH,
    &E_REQUEST_TIMEOUT,
    &E_ATTESTATION_ISSUANCE_FAILED,
    &E_ATTESTATION_EXPIRED,
    &E_ATTESTATION_INVALID,
    &E_STAGE_TOGGLE_PREREQUISITE_MISSING,
    &E_GOVERNANCE_WRITE_DEGRADED,
    // 警告码
    &W_EMPTY_SCOPE_PUBLISH,
    // 跨类操作
    &E_CROSS_CLASS_PARTIAL_REJECTION,
    // Independence_Policy
    &E_INDEPENDENCE_POLICY_SOLO_REJECTED,
    &INDEPENDENCE_EXEMPTION_BY_POLICY,
    // Revocation_Mode
    &E_REVOCATION_MODE_MISSING,
    // 收敛架构（T01）
    &E_TOOL_DEPRECATED,
    &E_MODE_DEPRECATED,
    &E_TOOL_MIGRATION_PENDING,
    &E_HTTP_DAEMON_UNAVAILABLE,
];

/// 按错误码查找条目。
pub fn lookup_by_code(code: &str) -> Option<&'static ErrorCodeEntry> {
    ERROR_CODE_DIRECTORY.iter().find(|e| e.code == code).copied()
}

/// 按 message key 查找条目。
pub fn lookup_by_message_key(key: &str) -> Option<&'static ErrorCodeEntry> {
    ERROR_CODE_DIRECTORY.iter().find(|e| e.message_key == key).copied()
}

/// 获取所有错误码（不含警告和信息码）。
pub fn error_codes_only() -> Vec<&'static ErrorCodeEntry> {
    ERROR_CODE_DIRECTORY
        .iter()
        .filter(|e| !e.is_warning && !e.code.starts_with('I'))
        .copied()
        .collect()
}

/// 获取所有警告码。
pub fn warning_codes_only() -> Vec<&'static ErrorCodeEntry> {
    ERROR_CODE_DIRECTORY
        .iter()
        .filter(|e| e.is_warning)
        .copied()
        .collect()
}

// ---------------------------------------------------------------------------
// 测试
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_all_codes_have_e_or_w_or_i_prefix() {
        for entry in ERROR_CODE_DIRECTORY {
            assert!(
                entry.code.starts_with('E')
                    || entry.code.starts_with('W')
                    || entry.code.starts_with('I'),
                "code {} must start with E/W/I",
                entry.code
            );
        }
    }

    #[test]
    fn test_all_message_keys_have_prefix() {
        for entry in ERROR_CODE_DIRECTORY {
            assert!(
                entry.message_key.starts_with("error.")
                    || entry.message_key.starts_with("warning.")
                    || entry.message_key.starts_with("info."),
                "message_key {} must start with error./warning./info.",
                entry.message_key
            );
        }
    }

    #[test]
    fn test_codes_are_unique() {
        let mut codes: Vec<&str> = ERROR_CODE_DIRECTORY.iter().map(|e| e.code).collect();
        codes.sort();
        let len = codes.len();
        codes.dedup();
        assert_eq!(codes.len(), len, "duplicate codes found");
    }

    #[test]
    fn test_message_keys_are_unique() {
        let mut keys: Vec<&str> = ERROR_CODE_DIRECTORY.iter().map(|e| e.message_key).collect();
        keys.sort();
        let len = keys.len();
        keys.dedup();
        assert_eq!(keys.len(), len, "duplicate message keys found");
    }

    #[test]
    fn test_lookup_by_code() {
        let entry = lookup_by_code("E_SID_MISMATCH").unwrap();
        assert_eq!(entry.message_key, "error.sid_mismatch");
        assert!(!entry.is_warning);

        assert!(lookup_by_code("E_NONEXISTENT").is_none());
    }

    #[test]
    fn test_lookup_by_message_key() {
        let entry = lookup_by_message_key("error.request_timeout").unwrap();
        assert_eq!(entry.code, "E_REQUEST_TIMEOUT");

        assert!(lookup_by_message_key("error.nonexistent").is_none());
    }

    #[test]
    fn test_warning_code_is_non_blocking() {
        let entry = lookup_by_code("W_EMPTY_SCOPE_PUBLISH").unwrap();
        assert!(entry.is_warning);
        assert!(entry.message_key.starts_with("warning."));
    }

    #[test]
    fn test_error_codes_only_excludes_warnings() {
        let errors = error_codes_only();
        for e in &errors {
            assert!(!e.is_warning);
            assert!(!e.code.starts_with('I'));
        }
        // 应包含主要错误码
        assert!(errors.iter().any(|e| e.code == "E_SID_MISMATCH"));
        assert!(errors.iter().any(|e| e.code == "E_GOVERNANCE_WRITE_DEGRADED"));
    }

    #[test]
    fn test_warning_codes_only() {
        let warnings = warning_codes_only();
        assert_eq!(warnings.len(), 1);
        assert_eq!(warnings[0].code, "W_EMPTY_SCOPE_PUBLISH");
    }

    #[test]
    fn test_independence_exemption_not_proof() {
        // 豁免标识码不得表述为"独立性已证明"
        let entry = lookup_by_code("I_INDEPENDENCE_EXEMPTION_BY_POLICY").unwrap();
        assert!(!entry.description.contains("proven"));
        assert!(!entry.description.contains("proved"));
        assert!(entry.description.contains("exempted"));
    }

    #[test]
    fn test_revocation_mode_missing_message_requirement() {
        // message 必须提示显式指定 compromised 或 rotated
        let entry = lookup_by_code("E_REVOCATION_MODE_MISSING").unwrap();
        assert!(entry.description.contains("compromised"));
        assert!(entry.description.contains("rotated"));
        // 不得暗示系统会取默认值
        assert!(!entry.description.contains("default"));
    }

    #[test]
    fn test_governance_degraded_code_matches_python() {
        // 与 server/degraded_mode.py 中的 code 一致
        let entry = lookup_by_code("E_GOVERNANCE_WRITE_DEGRADED").unwrap();
        assert_eq!(entry.code, "E_GOVERNANCE_WRITE_DEGRADED");
        assert_eq!(entry.message_key, "error.governance_write_degraded");
    }

    #[test]
    fn test_directory_covers_d0_rejection_paths() {
        // D0 已知拒绝路径全覆盖
        let d0_codes = [
            "E_SID_MISMATCH",
            "E_PEER_CREDENTIAL_UNAVAILABLE",
            "E_PAYLOAD_SIZE_EXCEEDED",
            "E_PAYLOAD_DIGEST_MISMATCH",
            "E_REQUEST_TIMEOUT",
            "E_ATTESTATION_ISSUANCE_FAILED",
            "E_ATTESTATION_EXPIRED",
            "E_ATTESTATION_INVALID",
            "E_STAGE_TOGGLE_PREREQUISITE_MISSING",
            "E_GOVERNANCE_WRITE_DEGRADED",
        ];
        for code in &d0_codes {
            assert!(
                lookup_by_code(code).is_some(),
                "D0 rejection path {} not in directory",
                code
            );
        }
    }

    #[test]
    fn test_cross_class_code_exists() {
        let entry = lookup_by_code("E_CROSS_CLASS_PARTIAL_REJECTION").unwrap();
        assert!(!entry.is_warning);
    }

    #[test]
    fn test_total_directory_size() {
        // 当前目录共 19 个条目（15 个既有 + 4 个收敛架构 T01 新增）
        assert_eq!(ERROR_CODE_DIRECTORY.len(), 19);
    }

    #[test]
    fn test_convergence_codes_present() {
        for code in [
            "E_TOOL_DEPRECATED",
            "E_MODE_DEPRECATED",
            "E_TOOL_MIGRATION_PENDING",
            "E_HTTP_DAEMON_UNAVAILABLE",
        ] {
            assert!(lookup_by_code(code).is_some(), "收敛架构错误码 {code} 缺失");
        }
        assert_eq!(lookup_by_code("E_TOOL_DEPRECATED").unwrap().message_key, "error.tool_deprecated");
    }
}
