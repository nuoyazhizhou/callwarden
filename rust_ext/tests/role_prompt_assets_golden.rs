//! RP-02 asset goldens for the production role prompt resources tree
//! (frozen spec §7.3, §11.1 items 3/4/9, §7.2 legacy byte probes).
//!
//! Frozen authority: docs/design/cw-role-prompt-compiler-v1-frozen-spec.md
//! SHA-256 95298729F3357CDBE76D8F8E91F12067B54D2661D6E80ABFF561A2E2A8C86CB7.
//!
//! These tests run the sole validator core against the REAL production tree
//! under `rust_ext/resources/role_prompts/v1/`, proving:
//! - the tree validates fail-closed (manifest present, hashes exact);
//! - legacy bytes are byte-exact against the frozen §7.2 hashes;
//! - no undeclared file lurks in the assets root (build.rs comment contract).

#[path = "../build_support/role_prompt_validator.rs"]
mod role_prompt_validator;

use role_prompt_validator::{
    validate_assets_tree, LEGACY_EXCEPTION_BODY_ID, LEGACY_EXCEPTION_CONTENT_SHA256,
    LEGACY_EXCEPTION_TEMPLATE_ID,
};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

/// The frozen §7.2 legacy byte hashes (lowercase 64-hex).
const FROZEN_LEGACY: [(&str, &str, &str); 3] = [
    (
        "cw.aprime.executor.startup.v1",
        "legacy/executor_planner_startup_v1.md",
        "59a459f7786097c671d48fbeec6e361c12d7a95bdec4e3722169d68d5d6a73f6",
    ),
    (
        "cw.aprime.reviewer.startup.v1",
        "legacy/reviewer_startup_v1.md",
        "6415033d8f134392de16fca130bfb762cb6c70d9f466c770ec18a20fc4ce139e",
    ),
    (
        "cw.aprime.adjudicator.startup.v1",
        "legacy/adjudicator_startup_v1.md",
        "42a5f1defa81008b009058c1baf5d1a14b3ef4521e291b7b55c19bb473a77c3e",
    ),
];

fn assets_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join(role_prompt_validator::ASSETS_SUBDIR)
}

fn sha256_file(p: &Path) -> String {
    let b = fs::read(p).expect("read asset file");
    format!("{}", hex::encode(Sha256::digest(&b)))
}

fn walk_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).expect("read_dir") {
        let entry = entry.expect("dir entry");
        let path = entry.path();
        if path.is_dir() {
            walk_files(&path, out);
        } else {
            out.push(path);
        }
    }
}

#[test]
fn role_prompt_assets_prod_tree_validates_fail_closed() {
    let out = validate_assets_tree(&assets_root()).expect("production tree must validate");
    assert!(out.manifest_present, "manifest must be present post-RP-02");
    assert_eq!(out.templates.len(), 10);

    let roles = out.templates.iter().filter(|t| t.kind == "role").count();
    let systems = out.templates.iter().filter(|t| t.kind == "system").count();
    assert_eq!(roles, 7);
    assert_eq!(systems, 3);

    // §11.1(4) route-coverage semantics (SR-02 Option B): exactly the 4
    // action-ready routes covered, each owned once.
    let mut covered: BTreeSet<String> = BTreeSet::new();
    for t in &out.templates {
        for r in &t.routes {
            assert!(covered.insert(r.clone()), "route {r:?} owned twice");
        }
    }
    assert_eq!(
        covered,
        BTreeSet::from([
            "READY/CLAIM".to_string(),
            "READY/REVISE".to_string(),
            "READY/REVIEW".to_string(),
            "READY/ADJUDICATE".to_string(),
        ])
    );

    // Compiler policy must be declared and hashed (normalized wire form).
    let policy_sha = out
        .compiler_policy_sha256
        .clone()
        .expect("compiler policy declared");
    assert_eq!(
        policy_sha.strip_prefix("sha256:").map(|s| s.len()),
        Some(64)
    );

    // Manifest hash must be stable across runs (byte golden).
    let again = validate_assets_tree(&assets_root()).expect("second run must validate");
    assert_eq!(out.manifest_sha256, again.manifest_sha256);
    assert_eq!(out.compiler_policy_sha256, again.compiler_policy_sha256);
}

#[test]
fn role_prompt_assets_legacy_bytes_match_frozen_spec() {
    let root = assets_root();
    for (id, rel, want) in FROZEN_LEGACY {
        let got = sha256_file(&root.join(rel));
        assert_eq!(got, want, "legacy asset {id} ({rel}) drifted from frozen §7.2 hash");
    }
}

#[test]
fn role_prompt_assets_manifest_declares_frozen_legacy_exception() {
    let out = validate_assets_tree(&assets_root()).expect("tree must validate");
    let exec = out
        .templates
        .iter()
        .find(|t| t.template_id == LEGACY_EXCEPTION_TEMPLATE_ID)
        .expect("legacy executor template declared");
    assert!(exec.legacy_alias_mismatch);
    assert_eq!(
        exec.source_declared_template_id.as_deref(),
        Some(LEGACY_EXCEPTION_BODY_ID)
    );
    assert_eq!(exec.content_sha256, format!("sha256:{LEGACY_EXCEPTION_CONTENT_SHA256}"));
    // The other two legacy templates must NOT carry the alias flag.
    for t in &out.templates {
        if t.template_id != LEGACY_EXCEPTION_TEMPLATE_ID {
            assert!(!t.legacy_alias_mismatch, "{} must not claim alias flag", t.template_id);
        }
    }
}

#[test]
fn role_prompt_assets_no_undeclared_files_in_tree() {
    let root = assets_root();
    let out = validate_assets_tree(&root).expect("tree must validate");
    let declared: BTreeSet<String> = out.templates.iter().map(|t| t.path.clone()).collect();

    let mut files = Vec::new();
    walk_files(&root, &mut files);
    let mut extra = 0;
    for f in files {
        let rel = f
            .strip_prefix(&root)
            .expect("under root")
            .to_string_lossy()
            .replace('\\', "/");
        if rel == role_prompt_validator::MANIFEST_FILE_NAME {
            continue;
        }
        if rel == "compiler_policy.md" {
            continue; // declared via the compiler_policy object, not templates
        }
        assert!(
            declared.contains(&rel),
            "undeclared stray file in assets root: {rel} (add it to manifest.json or remove it)"
        );
        extra += 1;
    }
    // 10 template assets + compiler policy = 11 non-manifest files.
    assert_eq!(declared.len() + 1, extra + 1);
}
