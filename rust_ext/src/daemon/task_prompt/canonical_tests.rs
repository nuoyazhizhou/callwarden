//! canonical.rs 单元测试（测试名统一携带 `task_prompt_renderer` 验收
//! filter token，spec §15.1 RP-04 最低验收命令）。

use serde_json::{json, Value};

use crate::daemon::task_prompt::canonical::{
    canonical_json, normalize_sha256_form, sha256_hex, sha256_prefixed, valid_sha256_form,
};

#[test]
fn task_prompt_renderer_canonical_sorts_object_keys_by_utf8_bytes() {
    let value = json!({
        "b": 1,
        "A": 2,
        "a": 3,
        "中文": 4,
        "Zz": 5,
    });
    let bytes = canonical_json(&value).expect("canonical ok");
    let text = String::from_utf8(bytes).expect("utf-8");
    // UTF-8 byte lexical order：'A'(65) < 'Z'(90) < 'a'(97) < 中文(E4B8AD)。
    // 键序由小到大；"b"(98) 在中文(E4..) 之后。
    assert_eq!(text, r#"{"A":2,"Zz":5,"a":3,"b":1,"中文":4}"#);
}

#[test]
fn task_prompt_renderer_canonical_is_compact_and_deterministic() {
    let value = json!({ "k": [1, 2, null], "nested": { "z": true, "a": null } });
    let first = canonical_json(&value).expect("canonical ok");
    let second = canonical_json(&value).expect("canonical ok");
    assert_eq!(first, second);
    let text = String::from_utf8(first).expect("utf-8");
    assert!(!text.contains(' '));
    // null 显式保留。
    assert!(text.contains("null"));
    // 数组保留 authority 顺序（1,2 不重排）。
    assert!(text.contains("[1,2,null]"));
    assert_eq!(text, r#"{"k":[1,2,null],"nested":{"a":null,"z":true}}"#);
}

#[test]
fn task_prompt_renderer_canonical_rejects_floats() {
    let value: Value =
        serde_json::from_str(r#"{"ok": 1, "bad": 1.5}"#).expect("parse with float");
    let err = canonical_json(&value).expect_err("float must be rejected");
    assert!(err.contains("float"), "error mentions float: {err}");
}

#[test]
fn task_prompt_renderer_canonical_preserves_integers_exactly() {
    let value = json!({ "watermark": 123, "zero": 0, "big": 9223372036854775807i64 });
    let text = String::from_utf8(canonical_json(&value).expect("ok")).expect("utf-8");
    assert_eq!(text, r#"{"big":9223372036854775807,"watermark":123,"zero":0}"#);
}

#[test]
fn task_prompt_renderer_sha256_forms_normalize_case_and_prefix() {
    let lower = "59a459f7786097c671d48fbeec6e361c12d7a95bdec4e3722169d68d5d6a73f6";
    let upper = lower.to_ascii_uppercase();
    let expected = format!("sha256:{lower}");
    assert_eq!(normalize_sha256_form(lower).as_deref(), Some(expected.as_str()));
    assert_eq!(
        normalize_sha256_form(&format!("sha256:{upper}")).as_deref(),
        Some(expected.as_str())
    );
    assert_eq!(normalize_sha256_form(&upper).as_deref(), Some(expected.as_str()));
    assert!(valid_sha256_form(lower));
    assert!(valid_sha256_form(&format!("sha256:{upper}")));
}

#[test]
fn task_prompt_renderer_sha256_forms_reject_illegal_wire_forms() {
    assert!(!valid_sha256_form(""));
    assert!(!valid_sha256_form("sha256:"));
    // 63/65 hex、非 hex 字符、md5 形式、其他算法前缀。
    assert!(!valid_sha256_form("59a459f7"));
    assert!(!valid_sha256_form(&"a".repeat(65)));
    assert!(!valid_sha256_form(&format!("md5:{}", "a".repeat(32))));
    assert!(!valid_sha256_form(&format!("sha256:{}", "g".repeat(64))));
    assert!(!valid_sha256_form("sha256:59a4 extra"));
}

#[test]
fn task_prompt_renderer_sha256_output_is_lowercase_prefixed() {
    let hex = sha256_hex(b"abc");
    assert_eq!(hex.len(), 64);
    assert!(hex.bytes().all(|b| b.is_ascii_hexdigit())
        && hex.bytes().all(|b| !b.is_ascii_uppercase()));
    let known = sha256_hex(b"abc");
    assert_eq!(
        known,
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
    assert_eq!(sha256_prefixed(b"abc"), format!("sha256:{known}"));
}
