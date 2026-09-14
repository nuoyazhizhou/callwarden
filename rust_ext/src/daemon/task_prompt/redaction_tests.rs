//! redaction.rs 单元测试（§8.3 secret denylist；测试名携带
//! `task_prompt_renderer` 验收 filter token）。

use crate::daemon::task_prompt::redaction::{
    ensure_clean, non_secret_hex_refs, normalize_bare_sha256_refs, scan_text,
    KNOWN_NON_SECRET_HEX_REFS,
};

#[test]
fn task_prompt_renderer_secret_scan_rejects_pem_private_key() {
    let text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----";
    let findings = scan_text(text);
    assert_eq!(findings.len(), 1);
    assert_eq!(findings[0].reason_code, "private_key_pem");
    assert!(ensure_clean(text).is_err());
}

#[test]
fn task_prompt_renderer_secret_scan_rejects_bearer_tokens() {
    let text = "use header Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.token.part";
    let findings = scan_text(text);
    assert!(
        findings.iter().any(|f| f.reason_code == "bearer_token"),
        "expected bearer_token, got {:?}",
        findings
    );
}

#[test]
fn task_prompt_renderer_secret_scan_rejects_authorization_value_forms() {
    for text in [
        r#"{"authorization": "Bearerxyz123456"}"#,
        "authorization=sk-live-abcdef0123456789",
        r#""Authorization":"Basic YWxhZGRpbg==""#,
    ] {
        assert!(
            scan_text(text).iter().any(|f| f.reason_code == "authorization_header"),
            "expected authorization_header for {text}"
        );
    }
}

#[test]
fn task_prompt_renderer_secret_scan_allows_bundle_authorization_object_key() {
    // bundle 授权投影对象键不是 HTTP Authorization 头（spec §9.3 误报边界）。
    let text = r#"{"authorization":{"routing_state":"action_ready","valid_for_claim":false}}"#;
    assert!(
        scan_text(text).is_empty(),
        "authorization object key must not trigger denylist"
    );
}

#[test]
fn task_prompt_renderer_secret_scan_rejects_credential_store_and_cookies() {
    assert!(scan_text("load secrets from credentials.bin now")
        .iter()
        .any(|f| f.reason_code == "local_credential_store"));
    assert!(scan_text("Set-Cookie: sessionid=abc123; HttpOnly")
        .iter()
        .any(|f| f.reason_code == "cookie_session_secret"));
}

#[test]
fn task_prompt_renderer_secret_scan_rejects_lease_and_fencing_assignments() {
    for text in [
        r#"{"lease_token":"raw-lease-token-value-123"}"#,
        "fencing_token = 00000042",
        r#"{"fencing-secret": "abc123"}"#,
    ] {
        assert!(
            scan_text(text).iter().any(|f| f.reason_code == "lease_fencing_secret"),
            "expected lease_fencing_secret for {text}"
        );
    }
}

#[test]
fn task_prompt_renderer_secret_scan_rejects_reusable_api_keys() {
    for text in [
        "key = sk-proj-abcdefghijklmnopqrstuvwx",
        "ghp_0123456789abcdefghijklmnopqrstuvwxyz0",
        "AKIAIOSFODNN7EXAMPLE",
        "xoxb-123456789012-abcdefghijklmnop",
    ] {
        assert!(
            scan_text(text).iter().any(|f| f.reason_code == "reusable_api_key"),
            "expected reusable_api_key for {text}"
        );
    }
}

#[test]
fn task_prompt_renderer_secret_scan_rejects_bare_64hex_credential_hash() {
    let hex = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    assert!(
        scan_text(hex).iter().any(|f| f.reason_code == "bare_credential_hash"),
        "bare 64-hex must be flagged"
    );
    // sha256: 前缀形式是唯一合法 hash wire form（spec §9.3），不命中。
    assert!(scan_text(&format!("sha256:{hex}")).is_empty());
}

#[test]
fn task_prompt_renderer_secret_scan_passes_authority_data_and_templates() {
    // contract/prompt hash（sha256: 前缀）、task id、路由字段均为合法数据面。
    let data = r#"{"task_id":"T-1788696869654-76a02598","role_contract_hash":"sha256:59a459f7786097c671d48fbeec6e361c12d7a95bdec4e3722169d68d5d6a73f6","routing":{"decision":"READY","action":"CLAIM"},"source_event_watermark":123}"#;
    assert!(scan_text(data).is_empty(), "authority data must pass: {:?}", scan_text(data));
}

#[test]
fn task_prompt_renderer_secret_scan_findings_carry_no_secret_content() {
    // reason codes 只含类别名，不含命中内容（§10.2 details 纪律）。
    let secret = "Bearer super-secret-token-value-123456";
    let findings = scan_text(secret);
    assert!(!findings.is_empty());
    for f in findings {
        let debug = format!("{:?}", f.reason_code);
        assert!(!debug.contains("super-secret"), "finding leaked content: {debug}");
    }
    let err = ensure_clean(secret).expect_err("must fail");
    assert!(err.starts_with("E_TASK_PROMPT_SECRET_DETECTED|"));
    assert!(!err.contains("super-secret"), "error leaked secret: {err}");
}

// ---------------------------------------------------------------------------
// CR14（review §5.7）：豁免表归一化 + manifest 派生 + 覆盖测试
// ---------------------------------------------------------------------------

#[test]
fn task_prompt_renderer_secret_scan_exempts_known_hex_refs_case_insensitively() {
    // 大写/小写/混合引用均豁免（CR14：此前仅大写恰好有效）。
    for hex in KNOWN_NON_SECRET_HEX_REFS {
        let lower = hex.to_ascii_lowercase();
        assert!(scan_text(hex).is_empty(), "uppercase exempt failed: {hex}");
        assert!(scan_text(&lower).is_empty(), "lowercase exempt failed: {lower}");
        assert!(
            scan_text(&format!("authority sha {}", &lower)).is_empty(),
            "embedded lowercase exempt failed"
        );
    }
}

#[test]
fn task_prompt_renderer_secret_scan_exempts_manifest_content_hashes() {
    // manifest 派生：任一 content_sha256 的裸 hex 核心也豁免（新资产免改码）。
    let set = non_secret_hex_refs();
    assert!(
        set.len() > KNOWN_NON_SECRET_HEX_REFS.len(),
        "manifest must contribute entries beyond the hardcoded one"
    );
    let sample = &set[KNOWN_NON_SECRET_HEX_REFS.len()];
    let lower = sample.to_ascii_lowercase();
    assert!(scan_text(&lower).is_empty(), "manifest-derived exempt failed: {lower}");
    // sha256: 前缀形态本就不命中，双保险。
    assert!(scan_text(&format!("sha256:{lower}")).is_empty());
}

#[test]
fn task_prompt_renderer_secret_scan_still_rejects_unexempted_bare_hex() {
    // 非豁免裸 64-hex 仍拦（归一化不放宽 denylist）。
    let hex = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210";
    assert!(
        scan_text(hex).iter().any(|f| f.reason_code == "bare_credential_hash"),
        "unexempted bare hex must still be flagged"
    );
}

#[test]
fn task_prompt_renderer_secret_scan_compiler_policy_body_passes() {
    // compiler_policy.md 正文自述冻结 spec SHA（裸 hex 豁免主场景）。
    let policy = include_str!("../../../resources/role_prompts/v1/compiler_policy.md");
    assert!(
        scan_text(policy).is_empty(),
        "compiler_policy.md body must pass scan: {:?}",
        scan_text(policy).iter().map(|f| f.reason_code).collect::<Vec<_>>()
    );
}

// ---------------------------------------------------------------------------
// CR4 本体（review §5.7 重定性）：写入侧 hash 规范化 normalize_bare_sha256_refs
// ---------------------------------------------------------------------------

#[test]
fn task_prompt_renderer_normalize_adds_prefix_to_bare_hex() {
    let hex = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    let out = normalize_bare_sha256_refs(&format!("manifest sha {} done", hex));
    assert_eq!(out, format!("manifest sha sha256:{} done", hex));
    // 规范化后的文本必须过扫描（写入侧 ↔ 扫描器 对偶锚点）
    assert!(scan_text(&out).is_empty());
}

#[test]
fn task_prompt_renderer_normalize_preserves_prefixed_and_longer_runs() {
    let hex = "0123456789ABCDEF0123456789abcdef0123456789abcdef0123456789abcdef";
    // 已带前缀：不重复补写（hex 原样保留，大小写不改动）
    assert_eq!(
        normalize_bare_sha256_refs(&format!("sha256:{}", hex)),
        format!("sha256:{}", hex)
    );
    // 65+ hex 串 / 128-hex：属更长 run，不命中
    let long65 = format!("{}a", hex);
    assert_eq!(normalize_bare_sha256_refs(&long65), long65);
    let hex2 = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210";
    let pair = format!("{}{}", hex, hex2);
    assert_eq!(normalize_bare_sha256_refs(&pair), pair);
}

#[test]
fn task_prompt_renderer_normalize_adjacent_hashes_all_prefixed() {
    // 相邻 hash（单字符分隔）：regex replace_all 会漏第二个，手写扫描必须全覆盖
    let h1 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    let h2 = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210";
    let out = normalize_bare_sha256_refs(&format!("{} and {}", h1, h2));
    assert_eq!(out, format!("sha256:{} and sha256:{}", h1, h2));
    assert!(scan_text(&out).is_empty());
}

#[test]
fn task_prompt_renderer_normalize_keeps_structured_semantics_untouched() {
    // 非 hash 内容零改动（含短 hex、普通词、请求 id）
    let text = "request_id=req-abc123 step_id=s1 lease ref aabbccdd";
    assert_eq!(normalize_bare_sha256_refs(text), text);
}
