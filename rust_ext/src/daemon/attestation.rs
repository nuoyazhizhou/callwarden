//! Daemon 侧 Attestation 签发与校验（Req 14.13）。
//!
//! ## 职责
//! - 由 Daemon 基于 Peer_Credential 派生的 Identity 与 Authoritative_Clock 签发 Attestation
//! - 绑定：Identity、record_id（verdict_id 或 evidence_id）、View_Manifest hash、Contract_Hash、有效期窗口
//! - 校验：issuer 必须为 Daemon、签名有效、绑定字段匹配、签发时间在有效期窗口内
//! - 拒绝客户端自签 Attestation 作为授权输入
//!
//! ## 签名算法
//! HMAC-SHA256，密钥为 daemon 启动时生成（或从配置加载）的 signing_key。
//! 签名覆盖所有绑定字段的规范化字节序列。
//!
//! ## 校验失败语义（Req 10.9）
//! 自签、issuer 非 daemon、绑定/签名校验失败、签发时间越窗 → fail closed，
//! 关联 verdict/Evidence 判为 invalid，保持 pre-request 任务状态。
//!
//! ## 撤销衔接（Req 10.10–10.18）
//! 撤销由 Evidence_Ledger 追加 Attestation_Revocation_Record 实现，
//! 本模块仅负责签发与校验；撤销派生逻辑在 gate 层实现（P3 阶段）。

use hmac::{Hmac, Mac};
use sha2::Sha256;

use super::clock::AuthoritativeClock;
use super::dispatch::DaemonRpcError;
use super::peercred::PeerIdentity;

type HmacSha256 = Hmac<Sha256>;

/// Attestation 签发者标识（固定为 "daemon"）。
///
/// 校验时 issuer 必须等于此值，否则视为客户端自签（Req 10.9 fail closed）。
pub const ATTESTATION_ISSUER_DAEMON: &str = "daemon";

/// 默认 Attestation 有效期窗口（毫秒）：24 小时。
///
/// 签发时间 + 此窗口 = 过期时间。校验时 Authoritative_Clock 当前时间
/// 必须落在 [issued_at, expires_at] 内。
pub const DEFAULT_VALIDITY_WINDOW_MS: u64 = 24 * 60 * 60 * 1000;

/// Daemon 签发的 Attestation 凭证（Req 14.13）。
///
/// 绑定 Peer_Identity 派生的 Identity、记录标识、View_Manifest hash、
/// Contract_Hash 与有效期窗口。签名覆盖所有绑定字段。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Attestation {
    /// 签发者标识（必须为 "daemon"）
    pub issuer: String,
    /// Peer_Identity 派生的 Identity（owner_key 格式：Unix="uid:gid"，Windows="sid"）
    pub identity: String,
    /// 记录标识（verdict_id 或 evidence_id）
    pub record_id: String,
    /// View_Manifest hash（SHA-256 hex）
    pub view_manifest_hash: String,
    /// Contract_Hash（SHA-256 hex）
    pub contract_hash: String,
    /// 签发时间（Authoritative_Clock 毫秒时间戳）
    pub issued_at: u64,
    /// 过期时间（issued_at + validity_window）
    pub expires_at: u64,
    /// HMAC-SHA256 签名（hex 编码）
    pub signature: String,
}

/// Attestation 签发器（Daemon 持有，不对外暴露 signing_key）。
///
/// daemon 启动时创建，通过 Arc 共享给需要签发 Attestation 的 handler。
pub struct AttestationIssuer {
    /// HMAC-SHA256 签名密钥
    signing_key: Vec<u8>,
    /// 权威时钟引用
    clock: std::sync::Arc<AuthoritativeClock>,
    /// 有效期窗口（毫秒）
    validity_window_ms: u64,
}

impl AttestationIssuer {
    /// 创建 Attestation 签发器。
    ///
    /// - `signing_key`：HMAC-SHA256 密钥（daemon 启动时生成或从安全配置加载）
    /// - `clock`：权威时钟（Req 14.11）
    /// - `validity_window_ms`：有效期窗口（毫秒），None 使用默认 24h
    pub fn new(
        signing_key: Vec<u8>,
        clock: std::sync::Arc<AuthoritativeClock>,
        validity_window_ms: Option<u64>,
    ) -> Self {
        Self {
            signing_key,
            clock,
            validity_window_ms: validity_window_ms.unwrap_or(DEFAULT_VALIDITY_WINDOW_MS),
        }
    }

    /// 生成随机 signing_key（32 字节，用于 daemon 首次启动）。
    pub fn generate_signing_key() -> Vec<u8> {
        // 使用 getrandom 或 fallback 到时间戳混合
        // D0 阶段使用简单的时间+pid 混合（P3 阶段替换为 CSPRNG）
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default();
        let pid = std::process::id();
        let mut key = Vec::with_capacity(32);
        // 混合时间戳和 pid 生成 32 字节
        let seed = now.as_nanos() ^ (pid as u128) << 64;
        for i in 0..32u8 {
            key.push(((seed >> (i % 16) * 8) & 0xFF) as u8 ^ i.wrapping_mul(37));
        }
        key
    }

    /// 签发 Attestation（Req 14.13）。
    ///
    /// 从 Peer_Identity 派生 Identity，绑定 record_id、View_Manifest hash、
    /// Contract_Hash，使用 Authoritative_Clock 记录签发时间，
    /// 以 daemon signing_key 签名。
    ///
    /// 拒绝客户端自签：本方法只由 daemon handler 调用，
    /// 客户端提交的 Attestation 在校验侧被拒绝（Req 10.9）。
    pub fn issue(
        &self,
        identity: &PeerIdentity,
        record_id: &str,
        view_manifest_hash: &str,
        contract_hash: &str,
    ) -> Attestation {
        let issued_at = self.clock.now_millis();
        let expires_at = issued_at + self.validity_window_ms;
        let identity_str = identity.owner_key();

        let signature = self.compute_signature(
            ATTESTATION_ISSUER_DAEMON,
            &identity_str,
            record_id,
            view_manifest_hash,
            contract_hash,
            issued_at,
            expires_at,
        );

        Attestation {
            issuer: ATTESTATION_ISSUER_DAEMON.to_string(),
            identity: identity_str,
            record_id: record_id.to_string(),
            view_manifest_hash: view_manifest_hash.to_string(),
            contract_hash: contract_hash.to_string(),
            issued_at,
            expires_at,
            signature,
        }
    }

    /// 校验 Attestation（Req 10.8, 10.9）。
    ///
    /// 校验项：
    /// 1. issuer 必须为 "daemon"（拒绝客户端自签）
    /// 2. 签名有效（HMAC-SHA256 验证）
    /// 3. 绑定字段匹配（identity、record_id、view_manifest_hash、contract_hash）
    /// 4. 签发时间在有效期窗口内（issued_at <= now <= expires_at）
    ///
    /// 任一校验失败返回 Structured_Reason（fail closed）。
    pub fn validate(
        &self,
        attestation: &Attestation,
        expected_identity: &str,
        expected_record_id: &str,
        expected_view_manifest_hash: &str,
        expected_contract_hash: &str,
    ) -> Result<(), DaemonRpcError> {
        // 1. issuer 校验（Req 10.9：issuer 非 daemon → fail closed）
        if attestation.issuer != ATTESTATION_ISSUER_DAEMON {
            return Err(DaemonRpcError::new(
                "attestation_invalid",
                format!(
                    "Attestation issuer 必须为 \"{}\"，实际为 \"{}\"（拒绝客户端自签）",
                    ATTESTATION_ISSUER_DAEMON, attestation.issuer
                ),
            ));
        }

        // 2. 签名校验（HMAC-SHA256）
        let expected_sig = self.compute_signature(
            &attestation.issuer,
            &attestation.identity,
            &attestation.record_id,
            &attestation.view_manifest_hash,
            &attestation.contract_hash,
            attestation.issued_at,
            attestation.expires_at,
        );
        if !constant_time_eq(expected_sig.as_bytes(), attestation.signature.as_bytes()) {
            return Err(DaemonRpcError::new(
                "attestation_invalid",
                "Attestation 签名校验失败（签名不匹配或密钥不一致）",
            ));
        }

        // 3. 绑定字段匹配
        if attestation.identity != expected_identity {
            return Err(DaemonRpcError::new(
                "attestation_invalid",
                format!(
                    "Attestation Identity 不匹配：期望 \"{}\"，实际 \"{}\"",
                    expected_identity, attestation.identity
                ),
            ));
        }
        if attestation.record_id != expected_record_id {
            return Err(DaemonRpcError::new(
                "attestation_invalid",
                format!(
                    "Attestation record_id 不匹配：期望 \"{}\"，实际 \"{}\"",
                    expected_record_id, attestation.record_id
                ),
            ));
        }
        if attestation.view_manifest_hash != expected_view_manifest_hash {
            return Err(DaemonRpcError::new(
                "attestation_invalid",
                "Attestation View_Manifest hash 不匹配",
            ));
        }
        if attestation.contract_hash != expected_contract_hash {
            return Err(DaemonRpcError::new(
                "attestation_invalid",
                "Attestation Contract_Hash 不匹配",
            ));
        }

        // 4. 有效期窗口校验（Authoritative_Clock 当前时间必须在窗口内）
        let now = self.clock.now_millis();
        if now < attestation.issued_at {
            return Err(DaemonRpcError::new(
                "attestation_invalid",
                format!(
                    "Attestation 签发时间 {} 晚于当前权威时间 {}（时钟异常）",
                    attestation.issued_at, now
                ),
            ));
        }
        if now > attestation.expires_at {
            return Err(DaemonRpcError::new(
                "attestation_expired",
                format!(
                    "Attestation 已过期：签发时间 {}，过期时间 {}，当前权威时间 {}",
                    attestation.issued_at, attestation.expires_at, now
                ),
            ));
        }

        Ok(())
    }

    /// 计算 HMAC-SHA256 签名（覆盖所有绑定字段的规范化字节序列）。
    ///
    /// 规范化格式：各字段以 '\n' 分隔，时间戳为十进制字符串。
    fn compute_signature(
        &self,
        issuer: &str,
        identity: &str,
        record_id: &str,
        view_manifest_hash: &str,
        contract_hash: &str,
        issued_at: u64,
        expires_at: u64,
    ) -> String {
        let canonical = format!(
            "{}\n{}\n{}\n{}\n{}\n{}\n{}",
            issuer, identity, record_id, view_manifest_hash, contract_hash, issued_at, expires_at
        );

        let mut mac = HmacSha256::new_from_slice(&self.signing_key).expect("HMAC 接受任意长度密钥");
        mac.update(canonical.as_bytes());
        let result = mac.finalize();
        hex_encode(result.into_bytes().as_slice())
    }
}

/// 常量时间比较（防止时序攻击）。
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

/// 十六进制编码（小写）。
fn hex_encode(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

// ============================================
// 记录有效性判定（Req 14.31, 10.8, 10.9）
// ============================================

/// 记录 Attestation 有效性判定结果。
///
/// 由 gate 层（P1 4.4/4.5）与 Degraded_Mode 降级写入路径（3.28）消费。
/// 无有效 Attestation 的记录不满足任何 Blocking_Clause。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecordValidity {
    /// 记录持有有效的 daemon 签发 Attestation
    Valid,
    /// 记录无 Attestation 或 Attestation 校验失败
    Invalid {
        /// 无效原因码
        reason: RecordInvalidReason,
    },
}

/// 记录无效原因分类。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecordInvalidReason {
    /// 无 Attestation（Degraded_Mode 直连写入、绕过 CLI 直接开库写入）
    NoAttestation,
    /// issuer 非 daemon（客户端自签）
    IssuerNotDaemon,
    /// 签名校验失败
    SignatureMismatch,
    /// 绑定字段不匹配
    BindingMismatch,
    /// 签发时间越窗（过期）
    Expired,
}

/// 判定记录是否持有有效 Attestation [Req 14.31, 10.8, 10.9]。
///
/// 不实现任何物理写屏障——安全性由 Attestation 校验承担，
/// 而不是靠阻止别人开库。
///
/// 参数：
/// - `attestation`: 记录附带的 Attestation（None = 无 Attestation）
/// - `issuer`: 用于签名校验的 AttestationIssuer
/// - `expected_identity`: 期望的 Identity
/// - `expected_record_id`: 期望的 record_id
/// - `expected_view_manifest_hash`: 期望的 View_Manifest hash
/// - `expected_contract_hash`: 期望的 Contract_Hash
///
/// 返回 RecordValidity::Valid 或 RecordValidity::Invalid { reason }。
pub fn check_record_attestation(
    attestation: Option<&Attestation>,
    issuer: &AttestationIssuer,
    expected_identity: &str,
    expected_record_id: &str,
    expected_view_manifest_hash: &str,
    expected_contract_hash: &str,
) -> RecordValidity {
    // 无 Attestation → invalid [Req 14.31]
    let att = match attestation {
        Some(a) => a,
        None => {
            return RecordValidity::Invalid {
                reason: RecordInvalidReason::NoAttestation,
            }
        }
    };

    // issuer 非 daemon → invalid [Req 10.9]
    if att.issuer != ATTESTATION_ISSUER_DAEMON {
        return RecordValidity::Invalid {
            reason: RecordInvalidReason::IssuerNotDaemon,
        };
    }

    // 签名校验
    let expected_sig = issuer.compute_signature(
        &att.issuer,
        &att.identity,
        &att.record_id,
        &att.view_manifest_hash,
        &att.contract_hash,
        att.issued_at,
        att.expires_at,
    );
    if !constant_time_eq(expected_sig.as_bytes(), att.signature.as_bytes()) {
        return RecordValidity::Invalid {
            reason: RecordInvalidReason::SignatureMismatch,
        };
    }

    // 绑定字段匹配
    if att.identity != expected_identity
        || att.record_id != expected_record_id
        || att.view_manifest_hash != expected_view_manifest_hash
        || att.contract_hash != expected_contract_hash
    {
        return RecordValidity::Invalid {
            reason: RecordInvalidReason::BindingMismatch,
        };
    }

    // 时间窗口校验
    let now = issuer.clock.now_millis();
    if now < att.issued_at || now > att.expires_at {
        return RecordValidity::Invalid {
            reason: RecordInvalidReason::Expired,
        };
    }

    RecordValidity::Valid
}

/// 快速判定：记录是否无有效 Attestation（便捷方法）。
///
/// 用于 gate 层快速过滤：返回 true 表示记录无效（不满足 Blocking_Clause）。
pub fn is_record_unattested(
    attestation: Option<&Attestation>,
    issuer: &AttestationIssuer,
    expected_identity: &str,
    expected_record_id: &str,
    expected_view_manifest_hash: &str,
    expected_contract_hash: &str,
) -> bool {
    !matches!(
        check_record_attestation(
            attestation,
            issuer,
            expected_identity,
            expected_record_id,
            expected_view_manifest_hash,
            expected_contract_hash,
        ),
        RecordValidity::Valid
    )
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    fn make_issuer() -> AttestationIssuer {
        let key = b"test-signing-key-32-bytes-long!!".to_vec();
        let clock = Arc::new(AuthoritativeClock::new());
        AttestationIssuer::new(key, clock, None)
    }

    fn make_identity() -> PeerIdentity {
        PeerIdentity::Unix {
            uid: 1000,
            gid: 1000,
        }
    }

    #[test]
    fn test_issue_attestation_basic() {
        let issuer = make_issuer();
        let identity = make_identity();

        let att = issuer.issue(&identity, "verdict-001", "vmhash-abc", "contract-hash-xyz");

        assert_eq!(att.issuer, "daemon");
        assert_eq!(att.identity, "1000");
        assert_eq!(att.record_id, "verdict-001");
        assert_eq!(att.view_manifest_hash, "vmhash-abc");
        assert_eq!(att.contract_hash, "contract-hash-xyz");
        assert!(att.issued_at > 0);
        assert_eq!(att.expires_at, att.issued_at + DEFAULT_VALIDITY_WINDOW_MS);
        assert!(!att.signature.is_empty());
        // 签名为 64 字符 hex（SHA-256 = 32 bytes = 64 hex chars）
        assert_eq!(att.signature.len(), 64);
    }

    #[test]
    fn test_validate_attestation_success() {
        let issuer = make_issuer();
        let identity = make_identity();

        let att = issuer.issue(&identity, "verdict-001", "vmhash-abc", "contract-hash-xyz");

        let result = issuer.validate(
            &att,
            "1000",
            "verdict-001",
            "vmhash-abc",
            "contract-hash-xyz",
        );
        assert!(result.is_ok());
    }

    #[test]
    fn test_validate_rejects_wrong_issuer() {
        let issuer = make_issuer();
        let identity = make_identity();

        let mut att = issuer.issue(&identity, "verdict-001", "vmhash-abc", "contract-hash-xyz");
        att.issuer = "client-self-signed".to_string();

        let result = issuer.validate(
            &att,
            "1000",
            "verdict-001",
            "vmhash-abc",
            "contract-hash-xyz",
        );
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "attestation_invalid");
        assert!(err.message.contains("issuer"));
    }

    #[test]
    fn test_validate_rejects_tampered_signature() {
        let issuer = make_issuer();
        let identity = make_identity();

        let mut att = issuer.issue(&identity, "verdict-001", "vmhash-abc", "contract-hash-xyz");
        // 篡改签名
        att.signature =
            "0000000000000000000000000000000000000000000000000000000000000000".to_string();

        let result = issuer.validate(
            &att,
            "1000",
            "verdict-001",
            "vmhash-abc",
            "contract-hash-xyz",
        );
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "attestation_invalid");
        assert!(err.message.contains("签名"));
    }

    #[test]
    fn test_validate_rejects_identity_mismatch() {
        let issuer = make_issuer();
        let identity = make_identity();

        let att = issuer.issue(&identity, "verdict-001", "vmhash-abc", "contract-hash-xyz");

        // 期望不同的 identity
        let result = issuer.validate(
            &att,
            "2000",
            "verdict-001",
            "vmhash-abc",
            "contract-hash-xyz",
        );
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "attestation_invalid");
        assert!(err.message.contains("Identity"));
    }

    #[test]
    fn test_validate_rejects_record_id_mismatch() {
        let issuer = make_issuer();
        let identity = make_identity();

        let att = issuer.issue(&identity, "verdict-001", "vmhash-abc", "contract-hash-xyz");

        let result = issuer.validate(
            &att,
            "1000",
            "verdict-999",
            "vmhash-abc",
            "contract-hash-xyz",
        );
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "attestation_invalid");
        assert!(err.message.contains("record_id"));
    }

    #[test]
    fn test_validate_rejects_view_manifest_hash_mismatch() {
        let issuer = make_issuer();
        let identity = make_identity();

        let att = issuer.issue(&identity, "verdict-001", "vmhash-abc", "contract-hash-xyz");

        let result = issuer.validate(
            &att,
            "1000",
            "verdict-001",
            "wrong-hash",
            "contract-hash-xyz",
        );
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "attestation_invalid");
        assert!(err.message.contains("View_Manifest"));
    }

    #[test]
    fn test_validate_rejects_contract_hash_mismatch() {
        let issuer = make_issuer();
        let identity = make_identity();

        let att = issuer.issue(&identity, "verdict-001", "vmhash-abc", "contract-hash-xyz");

        let result = issuer.validate(
            &att,
            "1000",
            "verdict-001",
            "vmhash-abc",
            "wrong-contract-hash",
        );
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "attestation_invalid");
        assert!(err.message.contains("Contract_Hash"));
    }

    #[test]
    fn test_validate_rejects_expired_attestation() {
        let key = b"test-signing-key-32-bytes-long!!".to_vec();
        let clock = Arc::new(AuthoritativeClock::new());
        // 极短有效期：1ms
        let issuer = AttestationIssuer::new(key, clock, Some(1));
        let identity = make_identity();

        let att = issuer.issue(&identity, "verdict-001", "vmhash-abc", "contract-hash-xyz");

        // 等待超过有效期
        std::thread::sleep(std::time::Duration::from_millis(5));

        let result = issuer.validate(
            &att,
            "1000",
            "verdict-001",
            "vmhash-abc",
            "contract-hash-xyz",
        );
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "attestation_expired");
        assert!(err.message.contains("过期"));
    }

    #[test]
    fn test_validate_rejects_different_signing_key() {
        let issuer_a = make_issuer();
        let identity = make_identity();

        let att = issuer_a.issue(&identity, "verdict-001", "vmhash-abc", "contract-hash-xyz");

        // 用不同密钥的 issuer 校验
        let key_b = b"different-key-32-bytes-long!!!!!".to_vec();
        let clock_b = Arc::new(AuthoritativeClock::new());
        let issuer_b = AttestationIssuer::new(key_b, clock_b, None);

        let result = issuer_b.validate(
            &att,
            "1000",
            "verdict-001",
            "vmhash-abc",
            "contract-hash-xyz",
        );
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "attestation_invalid");
        assert!(err.message.contains("签名"));
    }

    #[test]
    fn test_windows_identity_attestation() {
        let issuer = make_issuer();
        let identity = PeerIdentity::Windows {
            sid: "S-1-5-21-1234567890-1000".to_string(),
        };

        let att = issuer.issue(&identity, "evidence-042", "vmhash-win", "contract-win");

        assert_eq!(att.identity, "S-1-5-21-1234567890-1000");

        let result = issuer.validate(
            &att,
            "S-1-5-21-1234567890-1000",
            "evidence-042",
            "vmhash-win",
            "contract-win",
        );
        assert!(result.is_ok());
    }

    #[test]
    fn test_constant_time_eq_basic() {
        assert!(constant_time_eq(b"hello", b"hello"));
        assert!(!constant_time_eq(b"hello", b"world"));
        assert!(!constant_time_eq(b"hello", b"hell"));
        assert!(constant_time_eq(b"", b""));
    }

    #[test]
    fn test_hex_encode() {
        assert_eq!(hex_encode(&[0x00, 0xff, 0xab]), "00ffab");
        assert_eq!(hex_encode(&[]), "");
    }

    #[test]
    fn test_generate_signing_key_length() {
        let key = AttestationIssuer::generate_signing_key();
        assert_eq!(key.len(), 32);
    }

    #[test]
    fn test_custom_validity_window() {
        let key = b"test-signing-key-32-bytes-long!!".to_vec();
        let clock = Arc::new(AuthoritativeClock::new());
        let issuer = AttestationIssuer::new(key, clock, Some(3600_000)); // 1 hour
        let identity = make_identity();

        let att = issuer.issue(&identity, "verdict-001", "vmhash", "contract");
        assert_eq!(att.expires_at - att.issued_at, 3600_000);
    }

    // ============================================
    // check_record_attestation / is_record_unattested 测试
    // ============================================

    #[test]
    fn test_check_record_no_attestation() {
        let issuer = make_issuer();
        let result = check_record_attestation(
            None,
            &issuer,
            "1000",
            "verdict-001",
            "vmhash",
            "contract",
        );
        assert_eq!(
            result,
            RecordValidity::Invalid {
                reason: RecordInvalidReason::NoAttestation,
            }
        );
    }

    #[test]
    fn test_check_record_issuer_not_daemon() {
        let issuer = make_issuer();
        let identity = make_identity();
        let mut att = issuer.issue(&identity, "verdict-001", "vmhash", "contract");
        att.issuer = "rogue-client".to_string();

        let result = check_record_attestation(
            Some(&att),
            &issuer,
            "1000",
            "verdict-001",
            "vmhash",
            "contract",
        );
        assert_eq!(
            result,
            RecordValidity::Invalid {
                reason: RecordInvalidReason::IssuerNotDaemon,
            }
        );
    }

    #[test]
    fn test_check_record_signature_mismatch() {
        let issuer = make_issuer();
        let identity = make_identity();
        let mut att = issuer.issue(&identity, "verdict-001", "vmhash", "contract");
        att.signature =
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef".to_string();

        let result = check_record_attestation(
            Some(&att),
            &issuer,
            "1000",
            "verdict-001",
            "vmhash",
            "contract",
        );
        assert_eq!(
            result,
            RecordValidity::Invalid {
                reason: RecordInvalidReason::SignatureMismatch,
            }
        );
    }

    #[test]
    fn test_check_record_binding_mismatch_identity() {
        let issuer = make_issuer();
        let identity = make_identity();
        let att = issuer.issue(&identity, "verdict-001", "vmhash", "contract");

        // 期望 identity 不匹配
        let result = check_record_attestation(
            Some(&att),
            &issuer,
            "9999",
            "verdict-001",
            "vmhash",
            "contract",
        );
        assert_eq!(
            result,
            RecordValidity::Invalid {
                reason: RecordInvalidReason::BindingMismatch,
            }
        );
    }

    #[test]
    fn test_check_record_binding_mismatch_record_id() {
        let issuer = make_issuer();
        let identity = make_identity();
        let att = issuer.issue(&identity, "verdict-001", "vmhash", "contract");

        let result = check_record_attestation(
            Some(&att),
            &issuer,
            "1000",
            "verdict-different",
            "vmhash",
            "contract",
        );
        assert_eq!(
            result,
            RecordValidity::Invalid {
                reason: RecordInvalidReason::BindingMismatch,
            }
        );
    }

    #[test]
    fn test_check_record_binding_mismatch_view_manifest() {
        let issuer = make_issuer();
        let identity = make_identity();
        let att = issuer.issue(&identity, "verdict-001", "vmhash", "contract");

        let result = check_record_attestation(
            Some(&att),
            &issuer,
            "1000",
            "verdict-001",
            "wrong-vmhash",
            "contract",
        );
        assert_eq!(
            result,
            RecordValidity::Invalid {
                reason: RecordInvalidReason::BindingMismatch,
            }
        );
    }

    #[test]
    fn test_check_record_binding_mismatch_contract_hash() {
        let issuer = make_issuer();
        let identity = make_identity();
        let att = issuer.issue(&identity, "verdict-001", "vmhash", "contract");

        let result = check_record_attestation(
            Some(&att),
            &issuer,
            "1000",
            "verdict-001",
            "vmhash",
            "wrong-contract",
        );
        assert_eq!(
            result,
            RecordValidity::Invalid {
                reason: RecordInvalidReason::BindingMismatch,
            }
        );
    }

    #[test]
    fn test_check_record_expired() {
        let key = b"test-signing-key-32-bytes-long!!".to_vec();
        let clock = Arc::new(AuthoritativeClock::new());
        let issuer = AttestationIssuer::new(key, clock, Some(1)); // 1ms 有效期
        let identity = make_identity();
        let att = issuer.issue(&identity, "verdict-001", "vmhash", "contract");

        std::thread::sleep(std::time::Duration::from_millis(5));

        let result = check_record_attestation(
            Some(&att),
            &issuer,
            "1000",
            "verdict-001",
            "vmhash",
            "contract",
        );
        assert_eq!(
            result,
            RecordValidity::Invalid {
                reason: RecordInvalidReason::Expired,
            }
        );
    }

    #[test]
    fn test_check_record_valid() {
        let issuer = make_issuer();
        let identity = make_identity();
        let att = issuer.issue(&identity, "verdict-001", "vmhash-abc", "contract-xyz");

        let result = check_record_attestation(
            Some(&att),
            &issuer,
            "1000",
            "verdict-001",
            "vmhash-abc",
            "contract-xyz",
        );
        assert_eq!(result, RecordValidity::Valid);
    }

    #[test]
    fn test_is_record_unattested_true_for_none() {
        let issuer = make_issuer();
        assert!(is_record_unattested(
            None,
            &issuer,
            "1000",
            "verdict-001",
            "vmhash",
            "contract",
        ));
    }

    #[test]
    fn test_is_record_unattested_false_for_valid() {
        let issuer = make_issuer();
        let identity = make_identity();
        let att = issuer.issue(&identity, "verdict-001", "vmhash-abc", "contract-xyz");

        assert!(!is_record_unattested(
            Some(&att),
            &issuer,
            "1000",
            "verdict-001",
            "vmhash-abc",
            "contract-xyz",
        ));
    }

    #[test]
    fn test_is_record_unattested_true_for_tampered() {
        let issuer = make_issuer();
        let identity = make_identity();
        let mut att = issuer.issue(&identity, "verdict-001", "vmhash", "contract");
        att.signature =
            "0000000000000000000000000000000000000000000000000000000000000000".to_string();

        assert!(is_record_unattested(
            Some(&att),
            &issuer,
            "1000",
            "verdict-001",
            "vmhash",
            "contract",
        ));
    }
}
