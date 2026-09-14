//! RP-01 positive fixtures for the sole role prompt validator core
//! (frozen spec §7.4 "tests 复用同一 core", §11.1).
//!
//! Frozen authority: docs/design/cw-role-prompt-compiler-v1-frozen-spec.md
//! SHA-256 95298729F3357CDBE76D8F8E91F12067B54D2661D6E80ABFF561A2E2A8C86CB7.

#[path = "../build_support/role_prompt_validator.rs"]
mod role_prompt_validator;

use role_prompt_validator::*;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};

fn sha256_file(p: &Path) -> String {
    let b = fs::read(p).expect("read fixture file");
    format!("sha256:{}", hex::encode(Sha256::digest(&b)))
}

fn sha256_bytes(b: &[u8]) -> String {
    format!("sha256:{}", hex::encode(Sha256::digest(b)))
}

/// Fixture builder: writes manifest.json under a temp assets root and returns
/// (root, manifest_bytes).
struct Fixture {
    root: PathBuf,
}

#[derive(Clone)]
struct Entry {
    template_id: &'static str,
    kind: &'static str,
    role: Option<&'static str>,
    routes: Vec<&'static str>,
    path: &'static str,
    body: &'static str,
    source_declared: Option<&'static str>,
    legacy: bool,
    omit_hash: bool,
}

impl Default for Entry {
    fn default() -> Self {
        Self {
            template_id: "cw.aprime.executor.startup.v1",
            kind: "role",
            role: Some("executor"),
            routes: vec!["READY/CLAIM"],
            path: "legacy/executor.md",
            body: "# executor\n",
            source_declared: None,
            legacy: false,
            omit_hash: false,
        }
    }
}

impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "rp01_pos_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).expect("mkdir");
        Self { root }
    }

    fn write_entries(&self, entries: &[Entry], extra_manifest: Option<Value>) {
        let mut templates = Vec::new();
        for e in entries {
            let path_abs = self.root.join(e.path);
            fs::create_dir_all(path_abs.parent().unwrap()).expect("mkdir asset");
            fs::write(&path_abs, e.body).expect("write asset");
            let mut t = json!({
                "template_id": e.template_id,
                "kind": e.kind,
                "routes": e.routes,
                "path": e.path,
            });
            if let Some(r) = e.role {
                t["role"] = json!(r);
            }
            if !e.omit_hash {
                t["content_sha256"] = json!(sha256_bytes(e.body.as_bytes()));
            }
            if let Some(sd) = e.source_declared {
                t["source_declared_template_id"] = json!(sd);
            }
            if e.legacy {
                t["legacy_alias_mismatch"] = json!(true);
            }
            templates.push(t);
        }
        let mut manifest = json!({
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "templates": templates,
        });
        if let Some(x) = extra_manifest {
            manifest = x;
        }
        fs::write(self.root.join(MANIFEST_FILE_NAME), serde_json::to_vec_pretty(&manifest).unwrap())
            .expect("write manifest");
    }
}

#[test]
fn role_prompt_validator_pos_valid_manifest_with_role_and_system_templates_passes() {
    let f = Fixture::new();
    f.write_entries(
        &[
            Entry {
                template_id: "cw.aprime.executor.startup.v1",
                routes: vec!["READY/CLAIM", "READY/REVISE"],
                ..Default::default()
            },
            Entry {
                template_id: "cw.system.waiting.v1",
                kind: "system",
                role: None,
                routes: vec![],
                path: "system/waiting.md",
                body: "# waiting\n",
                ..Default::default()
            },
        ],
        None,
    );
    let out = validate_assets_tree(&f.root).expect("must validate");
    assert!(out.manifest_present);
    assert_eq!(out.templates.len(), 2);
    assert!(out.manifest_sha256.starts_with("sha256:"));
    assert_eq!(out.manifest_sha256[7..].len(), 64);
    assert_eq!(out.templates[0].routes, vec!["READY/CLAIM", "READY/REVISE"]);
    assert_eq!(out.templates[1].kind, "system");
}

#[test]
fn role_prompt_validator_pos_absent_manifest_yields_absent_marker_not_error() {
    let f = Fixture::new();
    let out = validate_assets_tree(&f.root).expect("absent must be Ok");
    assert!(!out.manifest_present);
    assert!(out.templates.is_empty());
}

#[test]
fn role_prompt_validator_pos_compiler_policy_hash_is_validated_and_reported() {
    let f = Fixture::new();
    let policy_body = b"# compiler policy\n";
    fs::write(f.root.join("compiler_policy.md"), policy_body).expect("write policy");
    f.write_entries(
        &[Entry {
            template_id: "cw.system.terminal.v1",
            kind: "system",
            role: None,
            routes: vec![],
            path: "system/terminal.md",
            body: "# terminal\n",
            ..Default::default()
        }],
        Some(json!({
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "compiler_policy": {
                "path": "compiler_policy.md",
                "content_sha256": sha256_bytes(policy_body),
            },
            "templates": [],
        })),
    );
    // templates empty on purpose: expect the empty-array issue, which proves
    // the policy was still parsed; then re-run with a template to assert hash.
    let err = validate_assets_tree(&f.root).unwrap_err();
    assert!(err.iter().any(|i| i.code == "E_ASSET_MANIFEST_INVALID"));

    // Now the full positive: policy + one system template.
    let f2 = Fixture::new();
    fs::write(f2.root.join("compiler_policy.md"), policy_body).expect("write policy");
    f2.write_entries(
        &[Entry {
            template_id: "cw.system.terminal.v1",
            kind: "system",
            role: None,
            routes: vec![],
            path: "system/terminal.md",
            body: "# terminal\n",
            ..Default::default()
        }],
        Some(json!({
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "compiler_policy": {
                "path": "compiler_policy.md",
                "content_sha256": sha256_bytes(policy_body),
            },
            "templates": serde_json::Value::Null,
        })),
    );
    // rebuild templates array properly (write_entries wrote Null above because
    // extra_manifest replaced the whole object): rewrite manifest cleanly.
    let manifest = json!({
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "compiler_policy": {
            "path": "compiler_policy.md",
            "content_sha256": sha256_bytes(policy_body),
        },
        "templates": [{
            "template_id": "cw.system.terminal.v1",
            "kind": "system",
            "routes": [],
            "path": "system/terminal.md",
            "content_sha256": sha256_bytes(b"# terminal\n"),
        }],
    });
    fs::write(f2.root.join(MANIFEST_FILE_NAME), serde_json::to_vec_pretty(&manifest).unwrap())
        .expect("rewrite manifest");
    let out = validate_assets_tree(&f2.root).expect("must validate");
    assert_eq!(out.compiler_policy_sha256.as_deref(), Some(sha256_bytes(policy_body).as_str()));
}

#[test]
fn role_prompt_validator_pos_frozen_legacy_alias_exception_is_accepted_byte_exactly() {
    // The single historical mismatch from frozen spec §7.2 must be accepted
    // only when ID + hash + declared body id match the frozen triple.
    let repo_root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../deliverables/software-company/aprime_role_contracts");
    let legacy_path = repo_root.join("executor_planner_startup_v1.md");
    let legacy_bytes = fs::read(&legacy_path).expect("legacy historical file must exist");

    let f = Fixture::new();
    let rel = "legacy/executor_planner_startup_v1.md";
    fs::create_dir_all(f.root.join("legacy")).expect("mkdir");
    fs::write(f.root.join(rel), &legacy_bytes).expect("copy legacy bytes");
    let manifest = json!({
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "templates": [{
            "template_id": LEGACY_EXCEPTION_TEMPLATE_ID,
            "kind": "role",
            "role": "executor",
            "routes": ["READY/CLAIM", "READY/REVISE"],
            "path": rel,
            "content_sha256": sha256_file(&legacy_path),
            "source_declared_template_id": LEGACY_EXCEPTION_BODY_ID,
            "legacy_alias_mismatch": true,
        }],
    });
    fs::write(f.root.join(MANIFEST_FILE_NAME), serde_json::to_vec_pretty(&manifest).unwrap())
        .expect("write manifest");
    let out = validate_assets_tree(&f.root).expect("frozen legacy exception must pass");
    assert_eq!(out.templates.len(), 1);
    assert!(out.templates[0].legacy_alias_mismatch);
    assert_eq!(
        out.templates[0].content_sha256,
        format!("sha256:{}", LEGACY_EXCEPTION_CONTENT_SHA256)
    );
}

#[test]
fn role_prompt_validator_pos_hash_accepts_both_wire_forms() {
    let f = Fixture::new();
    let body = "# both forms\n";
    let hex_hash = {
        let d = Sha256::digest(body.as_bytes());
        hex::encode(d)
    };
    f.write_entries(
        &[Entry {
            template_id: "cw.aprime.reviewer.startup.v1",
            body,
            omit_hash: true,
            ..Default::default()
        }],
        Some(json!({
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "templates": [{
                "template_id": "cw.aprime.reviewer.startup.v1",
                "kind": "role",
                "role": "reviewer",
                "routes": ["READY/REVIEW"],
                "path": "legacy/executor.md",
                "content_sha256": hex_hash,
            }],
        })),
    );
    let out = validate_assets_tree(&f.root).expect("64hex wire form must be accepted");
    assert_eq!(out.templates[0].content_sha256, format!("sha256:{hex_hash}"));
}

// --- SR-02 Option B regression coverage (2026-09-06) ---

#[test]
fn role_prompt_validator_pos_role_template_with_empty_routes_is_accepted() {
    // SR-02: role-kind templates may declare routes:[]. Routelessness is the
    // legacy/planner posture (frozen spec §7.1 exact-ID+hash selection;
    // §6 READY/PLAN hard error in v1).
    let f = Fixture::new();
    f.write_entries(
        &[Entry {
            template_id: "cw.aprime.planner.startup.v1",
            role: Some("planner"),
            routes: vec![],
            path: "current/planner_v1.md",
            body: "# planner\n",
            ..Default::default()
        }],
        None,
    );
    let out = validate_assets_tree(&f.root).expect("role template with routes:[] must validate");
    assert_eq!(out.templates.len(), 1);
    assert!(out.templates[0].routes.is_empty());
    assert_eq!(out.templates[0].role.as_deref(), Some("planner"));
}

#[test]
fn role_prompt_validator_pos_frozen_sec7_3_tree_shape_validates() {
    // The full frozen §7.3 production tree shape: 7 role templates (3 legacy
    // routeless + current executor CLAIM+REVISE / reviewer REVIEW /
    // adjudicator ADJUDICATE / planner routeless) + 3 system templates.
    // Before SR-02 this shape was unsatisfiable (per-template >=1-route
    // mandate vs 4 legal routes; §6 READY/PLAN hard error in v1).
    // Note: the legacy alias exception (exact ID+hash triple) has dedicated
    // coverage elsewhere; this fixture focuses on route shape, so legacy
    // entries use their primary IDs without the alias flag.
    let legacy = [
        ("cw.aprime.executor.startup.v1", "executor", "legacy/executor_planner_startup_v1.md", "# legacy executor\n"),
        ("cw.aprime.reviewer.startup.v1", "reviewer", "legacy/reviewer_startup_v1.md", "# legacy reviewer\n"),
        ("cw.aprime.adjudicator.startup.v1", "adjudicator", "legacy/adjudicator_startup_v1.md", "# legacy adjudicator\n"),
    ];
    let mut entries = Vec::new();
    for (id, role, path, body) in legacy {
        entries.push(Entry {
            template_id: id,
            role: Some(role),
            routes: vec![],
            path,
            body,
            ..Default::default()
        });
    }
    let current = [
        ("cw.aprime.executor.startup.v4", "executor", vec!["READY/CLAIM", "READY/REVISE"], "current/executor_v4.md"),
        ("cw.aprime.reviewer.startup.v4", "reviewer", vec!["READY/REVIEW"], "current/reviewer_v4.md"),
        ("cw.aprime.adjudicator.startup.v4", "adjudicator", vec!["READY/ADJUDICATE"], "current/adjudicator_v4.md"),
        ("cw.aprime.planner.startup.v1", "planner", vec![], "current/planner_v1.md"),
    ];
    for (id, role, routes, path) in current {
        entries.push(Entry {
            template_id: id,
            role: Some(role),
            routes,
            path,
            body: "# current\n",
            ..Default::default()
        });
    }
    for (id, path) in [
        ("cw.system.blocked_recovery.v1", "system/blocked_recovery.md"),
        ("cw.system.waiting.v1", "system/waiting.md"),
        ("cw.system.terminal.v1", "system/terminal.md"),
    ] {
        entries.push(Entry {
            template_id: id,
            kind: "system",
            role: None,
            routes: vec![],
            path,
            body: "# system\n",
            ..Default::default()
        });
    }
    let f = Fixture::new();
    f.write_entries(&entries, None);
    let out = validate_assets_tree(&f.root).expect("frozen §7.3 tree shape must validate");
    assert_eq!(out.templates.len(), 10);
    let role_count = out.templates.iter().filter(|t| t.kind == "role").count();
    let system_count = out.templates.iter().filter(|t| t.kind == "system").count();
    assert_eq!(role_count, 7);
    assert_eq!(system_count, 3);
    // Route coverage: all 4 action-ready routes owned, unambiguously.
    let mut covered = std::collections::BTreeSet::new();
    for t in &out.templates {
        for r in &t.routes {
            assert!(covered.insert(r.clone()), "route {r:?} owned twice");
        }
    }
    assert_eq!(covered.len(), 4);
}
