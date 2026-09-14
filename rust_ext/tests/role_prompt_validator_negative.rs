//! RP-01 negative fixtures: every fail-closed path of the sole role prompt
//! validator core (frozen spec §11.1 items 1-8, §7.2, §8.3, §9.3 budgets).
//!
//! Frozen authority: docs/design/cw-role-prompt-compiler-v1-frozen-spec.md
//! SHA-256 95298729F3357CDBE76D8F8E91F12067B54D2661D6E80ABFF561A2E2A8C86CB7.

#[path = "../build_support/role_prompt_validator.rs"]
mod role_prompt_validator;

use role_prompt_validator::validate_assets_tree;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::PathBuf;

struct Neg {
    root: PathBuf,
}

impl Neg {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "rp01_neg_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).expect("mkdir");
        Self { root }
    }

    /// Merges `template_override` onto the canonical base template, writes the
    /// asset at `asset_path`, and writes a single-template manifest. When the
    /// override (or the merge result) declares no content_sha256, the real
    /// SHA-256 of the written asset bytes is filled in.
    fn build(&self, asset_path: &str, asset_bytes: &[u8], template_override: Value) {
        let abs = self.root.join(asset_path);
        fs::create_dir_all(abs.parent().unwrap()).expect("mkdir asset");
        fs::write(&abs, asset_bytes).expect("write asset");
        let mut t = BASE_TEMPLATE();
        let override_declares_path = template_override.get("path").is_some();
        if let Some(ov) = template_override.as_object() {
            let to = t.as_object_mut().unwrap();
            for (k, v) in ov {
                to.insert(k.clone(), v.clone());
            }
        }
        // Respect an explicit (possibly invalid) path override; otherwise the
        // manifest must point at the asset actually written for this fixture.
        if !override_declares_path {
            t["path"] = json!(asset_path);
        }
        if t.get("content_sha256").is_none() {
            t["content_sha256"] =
                json!(format!("sha256:{}", hex::encode(Sha256::digest(asset_bytes))));
        }
        let manifest = json!({
            "schema_version": role_prompt_validator::MANIFEST_SCHEMA_VERSION,
            "templates": [t],
        });
        fs::write(self.root.join("manifest.json"), serde_json::to_vec_pretty(&manifest).unwrap())
            .expect("write manifest");
    }

    /// Raw manifest control for tests that need top-level fields the merge
    /// helper cannot express (unknown fields, wrong schema version).
    fn build_raw(&self, manifest: Value) {
        fs::write(self.root.join("manifest.json"), serde_json::to_vec_pretty(&manifest).unwrap())
            .expect("write manifest");
    }

    fn codes(&self) -> Vec<String> {
        match validate_assets_tree(&self.root) {
            Ok(_) => panic!("expected validation failure"),
            Err(issues) => issues.iter().map(|i| i.code.to_string()).collect(),
        }
    }
}

const BASE_TEMPLATE: fn() -> Value = || {
    json!({
        "template_id": "cw.aprime.executor.startup.v1",
        "kind": "role",
        "role": "executor",
        "routes": ["READY/CLAIM"],
        "path": "legacy/executor.md",
    })
};

#[test]
fn role_prompt_validator_neg_rejects_declared_hash_mismatch() {
    let n = Neg::new();
    n.build(
        "legacy/executor.md",
        b"# executor\n",
        json!({ "content_sha256": format!("sha256:{}", "0".repeat(64)) }),
    );
    let codes = n.codes();
    assert!(codes.contains(&"E_ASSET_HASH_MISMATCH".to_string()), "{codes:?}");
}

#[test]
fn role_prompt_validator_neg_rejects_bad_hash_wire_form() {
    let n = Neg::new();
    n.build("legacy/executor.md", b"# executor\n", json!({ "content_sha256": "md5:abcdef" }));
    let codes = n.codes();
    assert!(codes.contains(&"E_ASSET_MANIFEST_INVALID".to_string()), "{codes:?}");
}

#[test]
fn role_prompt_validator_neg_rejects_duplicate_template_id() {
    let n = Neg::new();
    let body = b"# dup\n";
    fs::write(n.root.join("a.md"), body).unwrap();
    fs::write(n.root.join("b.md"), body).unwrap();
    let mk = |p: &str| {
        json!({
            "template_id": "cw.aprime.executor.startup.v1",
            "kind": "role",
            "role": "executor",
            "routes": ["READY/CLAIM"],
            "path": p,
            "content_sha256": format!("sha256:{}", hex::encode(Sha256::digest(body))),
        })
    };
    let manifest = json!({
        "schema_version": role_prompt_validator::MANIFEST_SCHEMA_VERSION,
        "templates": [mk("a.md"), mk("b.md")],
    });
    fs::write(n.root.join("manifest.json"), serde_json::to_vec_pretty(&manifest).unwrap()).unwrap();
    let codes = n.codes();
    assert!(codes.contains(&"E_ASSET_ID_DUPLICATE".to_string()), "{codes:?}");
}

#[test]
fn role_prompt_validator_neg_rejects_duplicate_path() {
    let n = Neg::new();
    let body = b"# dup\n";
    fs::write(n.root.join("a.md"), body).unwrap();
    let mk = |id: &str| {
        json!({
            "template_id": id,
            "kind": "role",
            "role": "executor",
            "routes": ["READY/CLAIM"],
            "path": "a.md",
            "content_sha256": format!("sha256:{}", hex::encode(Sha256::digest(body))),
        })
    };
    let manifest = json!({
        "schema_version": role_prompt_validator::MANIFEST_SCHEMA_VERSION,
        "templates": [mk("cw.a.v1"), mk("cw.b.v1")],
    });
    fs::write(n.root.join("manifest.json"), serde_json::to_vec_pretty(&manifest).unwrap()).unwrap();
    let codes = n.codes();
    assert!(codes.contains(&"E_ASSET_PATH_DUPLICATE".to_string()), "{codes:?}");
}

#[test]
fn role_prompt_validator_neg_rejects_directory_escape() {
    let n = Neg::new();
    n.build("legacy/executor.md", b"# x\n", json!({ "path": "../outside.md" }));
    let codes = n.codes();
    assert!(codes.contains(&"E_ASSET_PATH_INVALID".to_string()), "{codes:?}");
}

#[test]
fn role_prompt_validator_neg_rejects_windows_style_or_absolute_path() {
    for bad in ["C:/x.md", "/abs/x.md", "a\\b.md"] {
        let n = Neg::new();
        n.build("legacy/executor.md", b"# x\n", json!({ "path": bad }));
        let codes = n.codes();
        assert!(codes.contains(&"E_ASSET_PATH_INVALID".to_string()), "{bad}: {codes:?}");
    }
}

#[test]
fn role_prompt_validator_neg_rejects_crlf_line_endings() {
    let n = Neg::new();
    n.build("legacy/executor.md", b"# title\r\nbody\r\n", json!({}));
    let codes = n.codes();
    assert!(codes.contains(&"E_ASSET_BYTES_INVALID".to_string()), "{codes:?}");
}

#[test]
fn role_prompt_validator_neg_rejects_non_utf8_asset() {
    let n = Neg::new();
    n.build("legacy/executor.md", &[0xff, 0xfe, 0x00, 0x01], json!({}));
    let codes = n.codes();
    assert!(codes.contains(&"E_ASSET_BYTES_INVALID".to_string()), "{codes:?}");
}

#[test]
fn role_prompt_validator_neg_rejects_ambiguous_route() {
    let n = Neg::new();
    let body = b"# dup route\n";
    fs::write(n.root.join("a.md"), body).unwrap();
    fs::write(n.root.join("b.md"), body).unwrap();
    let mk = |id: &str, p: &str| {
        json!({
            "template_id": id,
            "kind": "role",
            "role": "executor",
            "routes": ["READY/CLAIM"],
            "path": p,
            "content_sha256": format!("sha256:{}", hex::encode(Sha256::digest(body))),
        })
    };
    let manifest = json!({
        "schema_version": role_prompt_validator::MANIFEST_SCHEMA_VERSION,
        "templates": [mk("cw.a.v1", "a.md"), mk("cw.b.v1", "b.md")],
    });
    fs::write(n.root.join("manifest.json"), serde_json::to_vec_pretty(&manifest).unwrap()).unwrap();
    let codes = n.codes();
    assert!(codes.contains(&"E_ASSET_ROUTE_AMBIGUOUS".to_string()), "{codes:?}");
}

#[test]
fn role_prompt_validator_neg_rejects_system_template_with_action_route() {
    let n = Neg::new();
    n.build(
        "system/waiting.md",
        b"# waiting\n",
        json!({ "template_id": "cw.system.waiting.v1", "kind": "system", "role": Value::Null, "routes": ["READY/CLAIM"] }),
    );
    let codes = n.codes();
    assert!(codes.contains(&"E_ASSET_ROUTE_INVALID".to_string()), "{codes:?}");
}

// SR-02 Option B: the former `neg_rejects_role_template_without_route`
// fixture asserted the removed per-template >=1-route mandate. Role-kind
// templates with routes:[] are now accepted (see the SR-02 rationale comment
// in build_support/role_prompt_validator.rs); positive coverage lives in
// role_prompt_validator_positive.rs (empty-routes acceptance + the frozen
// §7.3 tree shape).

#[test]
fn role_prompt_validator_neg_rejects_unknown_route() {
    let n = Neg::new();
    n.build("legacy/executor.md", b"# x\n", json!({ "routes": ["READY/PLAN"] }));
    let codes = n.codes();
    assert!(codes.contains(&"E_ASSET_ROUTE_INVALID".to_string()), "{codes:?}");
}

#[test]
fn role_prompt_validator_neg_rejects_legacy_exception_outside_frozen_triple() {
    // legacy_alias_mismatch=true on a template that is NOT the frozen executor
    // ID+hash pair must fail closed (§7.2 "build gate 只可按该 exact ID+hash 接受").
    let n = Neg::new();
    n.build(
        "legacy/reviewer.md",
        b"# reviewer\n",
        json!({
            "template_id": "cw.aprime.reviewer.startup.v1",
            "kind": "role",
            "role": "reviewer",
            "routes": ["READY/REVIEW"],
            "source_declared_template_id": "cw.aprime.something.else.v1",
            "legacy_alias_mismatch": true,
        }),
    );
    let codes = n.codes();
    assert!(codes.contains(&"E_ASSET_LEGACY_EXCEPTION_VIOLATED".to_string()), "{codes:?}");
}

#[test]
fn role_prompt_validator_neg_rejects_source_declared_mismatch_without_legacy_flag() {
    let n = Neg::new();
    n.build(
        "legacy/executor.md",
        b"# x\n",
        json!({ "source_declared_template_id": "cw.other.v1" }),
    );
    let codes = n.codes();
    assert!(codes.contains(&"E_ASSET_BODY_ID_MISMATCH".to_string()), "{codes:?}");
}

#[test]
fn role_prompt_validator_neg_rejects_body_self_declared_id_mismatch() {
    let n = Neg::new();
    n.build(
        "current/executor_v4.md",
        b"template_id: cw.aprime.wrong.self.v1\nbody\n",
        json!({}),
    );
    let codes = n.codes();
    assert!(codes.contains(&"E_ASSET_BODY_ID_MISMATCH".to_string()), "{codes:?}");
}

#[test]
fn role_prompt_validator_neg_rejects_secret_patterns() {
    for secret_body in [
        &b"-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"[..],
        b"Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9\n",
        b"set-cookie: session=deadbeefdeadbeefdeadbeef\n",
        b"see credentials.bin for the raw lease\n",
    ] {
        let n = Neg::new();
        n.build("legacy/executor.md", secret_body, json!({}));
        let codes = n.codes();
        assert!(codes.contains(&"E_ASSET_SECRET_DETECTED".to_string()), "{codes:?}");
    }
}

#[test]
fn role_prompt_validator_neg_rejects_unknown_manifest_and_template_fields() {
    let n = Neg::new();
    fs::create_dir_all(n.root.join("legacy")).unwrap();
    fs::write(n.root.join("legacy/executor.md"), b"# x\n").unwrap();
    n.build_raw(json!({
        "schema_version": role_prompt_validator::MANIFEST_SCHEMA_VERSION,
        "bypass_flag": true,
        "templates": [{ "draft": true }],
    }));
    let codes = n.codes();
    assert!(
        codes.iter().filter(|c| *c == "E_ASSET_MANIFEST_INVALID").count() >= 2,
        "{codes:?}"
    );
}

#[test]
fn role_prompt_validator_neg_rejects_wrong_schema_version() {
    let n = Neg::new();
    fs::create_dir_all(n.root.join("legacy")).unwrap();
    fs::write(n.root.join("legacy/executor.md"), b"# x\n").unwrap();
    n.build_raw(json!({
        "schema_version": "role_prompt_assets_v0",
        "templates": [],
    }));
    let codes = n.codes();
    assert!(codes.contains(&"E_ASSET_MANIFEST_INVALID".to_string()), "{codes:?}");
}

#[test]
fn role_prompt_validator_neg_rejects_empty_templates_array_when_manifest_declared() {
    let n = Neg::new();
    let manifest = json!({
        "schema_version": role_prompt_validator::MANIFEST_SCHEMA_VERSION,
        "templates": [],
    });
    fs::write(n.root.join("manifest.json"), serde_json::to_vec_pretty(&manifest).unwrap()).unwrap();
    let codes = n.codes();
    assert!(codes.contains(&"E_ASSET_MANIFEST_INVALID".to_string()), "{codes:?}");
}

#[test]
fn role_prompt_validator_neg_rejects_oversized_or_bad_charset_template_id() {
    for bad_id in ["cw/bad id with spaces", &"x".repeat(300)[..]] {
        let n = Neg::new();
        n.build("legacy/executor.md", b"# x\n", json!({ "template_id": bad_id }));
        let codes = n.codes();
        assert!(codes.contains(&"E_ASSET_ID_INVALID".to_string()), "{bad_id}: {codes:?}");
    }
}

#[test]
fn role_prompt_validator_neg_rejects_missing_asset_file() {
    let n = Neg::new();
    let manifest = json!({
        "schema_version": role_prompt_validator::MANIFEST_SCHEMA_VERSION,
        "templates": [{
            "template_id": "cw.aprime.executor.startup.v1",
            "kind": "role",
            "role": "executor",
            "routes": ["READY/CLAIM"],
            "path": "legacy/missing.md",
            "content_sha256": format!("sha256:{}", "1".repeat(64)),
        }],
    });
    fs::write(n.root.join("manifest.json"), serde_json::to_vec_pretty(&manifest).unwrap()).unwrap();
    let codes = n.codes();
    assert!(codes.contains(&"E_ASSET_FILE_UNREADABLE".to_string()), "{codes:?}");
}
