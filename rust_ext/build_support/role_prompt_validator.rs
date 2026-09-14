//! Role Prompt Compiler v1 — the sole parser/validator/canonical core for
//! production prompt assets (frozen spec §7.4, §11.1).
//!
//! Frozen authority: docs/design/cw-role-prompt-compiler-v1-frozen-spec.md
//! SHA-256 95298729F3357CDBE76D8F8E91F12067B54D2661D6E80ABFF561A2E2A8C86CB7.
//!
//! This module is included by:
//! - `rust_ext/build.rs` (build-time hard gate + `OUT_DIR/role_prompts_generated.rs`)
//! - `rust_ext/tests/role_prompt_validator_*.rs` (positive + negative fixtures)
//! via `#[path]` so that exactly one implementation exists (no second rule set).
//!
//! Semantics (fail-closed):
//! - manifest absent (pre-RP-02 state) → `ValidationOutput { manifest_present: false }`
//!   so the build gate passes only while no production assets are declared;
//! - manifest present but invalid → hard error list; build MUST abort;
//! - no bypass flag exists (spec forbids `CW_ALLOW_DRAFT_TEMPLATES` or any
//!   production bypass; drafts must live outside the manifest scan root).

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::Value;
use sha2::{Digest, Sha256};

/// Fixed schema version of the production asset manifest.
pub const MANIFEST_SCHEMA_VERSION: &str = "role_prompt_assets_v1";

/// Resource scan root, relative to the crate manifest dir (frozen spec §7.3).
pub const ASSETS_SUBDIR: &str = "resources/role_prompts/v1";

/// Manifest file name inside the assets root.
pub const MANIFEST_FILE_NAME: &str = "manifest.json";

/// The one and only legacy alias mismatch accepted by the build gate
/// (frozen spec §7.2: Executor historical body self-declares the planner id).
pub const LEGACY_EXCEPTION_TEMPLATE_ID: &str = "cw.aprime.executor.startup.v1";
pub const LEGACY_EXCEPTION_BODY_ID: &str = "cw.aprime.executor-planner.startup.v1";
pub const LEGACY_EXCEPTION_CONTENT_SHA256: &str =
    "59a459f7786097c671d48fbeec6e361c12d7a95bdec4e3722169d68d5d6a73f6";

/// Action-ready routes a role template may claim (frozen spec §6).
pub const ALLOWED_ROUTES: [&str; 4] = [
    "READY/CLAIM",
    "READY/REVISE",
    "READY/REVIEW",
    "READY/ADJUDICATE",
];

/// ID budget (frozen spec §9.3): template/compiler-policy ID ≤ 256 ASCII bytes.
const MAX_ID_BYTES: usize = 256;
/// Single path budget (frozen spec §9.3): ≤ 4 KiB UTF-8.
const MAX_PATH_BYTES: usize = 4096;

/// One fail-closed validation failure. `code` is a stable, human-scannable tag.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidationIssue {
    pub code: &'static str,
    pub message: String,
}

impl ValidationIssue {
    fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self { code, message: message.into() }
    }
}

impl std::fmt::Display for ValidationIssue {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

/// A validated template asset.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidatedTemplate {
    pub template_id: String,
    pub kind: String,
    pub role: Option<String>,
    pub routes: Vec<String>,
    /// Manifest-relative path with forward slashes.
    pub path: String,
    /// Normalized lowercase `sha256:<64hex>`.
    pub content_sha256: String,
    pub source_declared_template_id: Option<String>,
    pub legacy_alias_mismatch: bool,
}

/// Successful validation result (manifest present) or the absent-marker result.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidationOutput {
    /// False when the assets root/manifest does not exist yet (pre-RP-02).
    pub manifest_present: bool,
    /// Lowercase 64-hex SHA-256 of the raw manifest bytes.
    pub manifest_sha256: String,
    /// Lowercase 64-hex SHA-256 of the raw compiler policy bytes, if declared.
    pub compiler_policy_sha256: Option<String>,
    /// Templates in manifest declaration order.
    pub templates: Vec<ValidatedTemplate>,
}

impl ValidationOutput {
    fn absent() -> Self {
        Self {
            manifest_present: false,
            manifest_sha256: String::new(),
            compiler_policy_sha256: None,
            templates: Vec::new(),
        }
    }
}

/// Normalize a declared hash: accept `64hex` or `sha256:<64hex>`, lowercase.
/// Returns `Err` with a stable issue for any other algorithm/length/charset.
fn normalize_sha256(raw: &Value, field: &str) -> Result<String, ValidationIssue> {
    let s = raw
        .as_str()
        .ok_or_else(|| ValidationIssue::new("E_ASSET_MANIFEST_INVALID", format!("{field} must be a string")))?;
    let lowered = s.strip_prefix("sha256:").unwrap_or(s).to_ascii_lowercase();
    if lowered.len() != 64 || !lowered.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(ValidationIssue::new(
            "E_ASSET_MANIFEST_INVALID",
            format!("{field} is not a sha-256 digest (64 hex or sha256: prefixed): {s:?}"),
        ));
    }
    Ok(format!("sha256:{lowered}"))
}

fn sha256_hex(bytes: &[u8]) -> String {
    let d = Sha256::digest(bytes);
    format!("sha256:{}", hex::encode(d))
}

/// Reject path forms that escape the assets root or violate the frozen
/// convention: relative, forward slashes, no `..` component, no absolute or
/// Windows-specific forms.
fn check_relative_asset_path(raw: &str) -> Result<(), ValidationIssue> {
    if raw.is_empty() {
        return Err(ValidationIssue::new("E_ASSET_PATH_INVALID", "path is empty"));
    }
    if raw.len() > MAX_PATH_BYTES {
        return Err(ValidationIssue::new(
            "E_ASSET_PATH_INVALID",
            format!("path exceeds {MAX_PATH_BYTES} UTF-8 bytes budget"),
        ));
    }
    if raw.starts_with('/') || raw.starts_with('\\') || raw.contains(':') || raw.contains('\\') {
        return Err(ValidationIssue::new(
            "E_ASSET_PATH_INVALID",
            format!("path must be relative with forward slashes: {raw:?}"),
        ));
    }
    if raw.split('/').any(|c| c == ".." || c.is_empty()) {
        return Err(ValidationIssue::new(
            "E_ASSET_PATH_INVALID",
            format!("path contains empty or `..` component (directory escape): {raw:?}"),
        ));
    }
    Ok(())
}

fn check_template_id(raw: &str) -> Result<(), ValidationIssue> {
    if raw.is_empty() {
        return Err(ValidationIssue::new("E_ASSET_ID_INVALID", "template_id is empty"));
    }
    if raw.len() > MAX_ID_BYTES || !raw.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-' | b'_' | b'/')) {
        return Err(ValidationIssue::new(
            "E_ASSET_ID_INVALID",
            format!("template_id must be non-empty ASCII (alnum . _ / -) within {MAX_ID_BYTES} bytes: {raw:?}"),
        ));
    }
    Ok(())
}

/// High-confidence secret denylist scan (frozen spec §8.3 subset that is
/// unambiguous in prose assets). Deliberately avoids 64-hex raw-token matching,
/// which would false-positive on documented digests.
fn scan_secrets(rel: &str, bytes: &[u8]) -> Option<ValidationIssue> {
    let hay = String::from_utf8_lossy(bytes);
    let lowered = hay.to_ascii_lowercase();
    const NEEDLES: [&str; 8] = [
        "-----begin",
        "authorization:",
        "set-cookie:",
        "aws_secret_access_key",
        "credentials.bin",
        "fencing_secret",
        "lease_token:",
        "private_key:",
    ];
    for n in NEEDLES {
        if lowered.contains(n) {
            return Some(ValidationIssue::new(
                "E_ASSET_SECRET_DETECTED",
                format!("{rel} hits secret denylist pattern {n:?}"),
            ));
        }
    }
    if lowered.contains("bearer ") && lowered.matches("bearer ").count() > 0 {
        // `Bearer <token>` prose mention with a literal space-delimited value is
        // the exact credential wire form; bare word "bearer" alone is allowed.
        for (idx, _) in lowered.match_indices("bearer ") {
            let tail = &hay[idx + 7..];
            let token: String = tail
                .chars()
                .take_while(|c| !c.is_whitespace())
                .collect::<String>()
                .trim_matches(|c: char| ".,;:)\"'`".contains(c))
                .to_string();
            if token.len() >= 16 {
                return Some(ValidationIssue::new(
                    "E_ASSET_SECRET_DETECTED",
                    format!("{rel} hits secret denylist pattern 'Bearer <token>'"),
                ));
            }
        }
    }
    None
}

/// Scan body text for a self-declared template id line and enforce the
/// primary/declared consistency rule (frozen spec §7.2, §11.1 item 6).
/// Recognized declaration forms (line-anchored, high confidence only):
/// - `template_id: X` / `template id: X`
/// - `**模板标识：** \`X\`` (the frozen historical Executor file form)
fn check_body_declared_id(
    rel: &str,
    body: &str,
    primary_id: &str,
    legacy_exception: bool,
) -> Option<ValidationIssue> {
    for line in body.lines() {
        let trimmed = line.trim();
        // Strip markdown bold markers around the declaration label.
        let bare = trimmed.trim_start_matches('*').trim_start();
        let lower = bare.to_ascii_lowercase();
        let declared: Option<String> = if (lower.starts_with("template_id:") || lower.starts_with("template id:"))
            && !lower.contains("source_declared")
        {
            bare.split_once(':')
                .map(|(_, v)| v.trim().trim_matches(|c| c == '"' || c == '\'' || c == '`').to_string())
        } else if bare.starts_with("模板标识") {
            // `模板标识：** \`X\`` → take the first back-ticked token after the label.
            let after_label = bare.trim_start_matches("模板标识")
                .trim_start_matches(|c: char| c == '：' || c == ':' || c == '*' || c == ' ');
            let token: String = after_label
                .trim()
                .trim_matches(|c: char| c == '`' || c == '"' || c == '\'' || c == '*')
                .trim()
                .to_string();
            Some(token)
        } else {
            None
        };
        let Some(declared) = declared else { continue };
        if declared.is_empty() {
            continue;
        }
        if declared == primary_id || (legacy_exception && declared == LEGACY_EXCEPTION_BODY_ID) {
            return None;
        }
        return Some(ValidationIssue::new(
            "E_ASSET_BODY_ID_MISMATCH",
            format!("{rel} body self-declares template id {declared:?} but manifest primary id is {primary_id:?}"),
        ));
    }
    None
}

fn string_field(obj: &Value, key: &str, ctx: &str) -> Result<String, ValidationIssue> {
    obj.get(key)
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
        .ok_or_else(|| {
            ValidationIssue::new("E_ASSET_MANIFEST_INVALID", format!("{ctx}: missing string field {key:?}"))
        })
}

/// Validate the production assets tree rooted at `base` (…/resources/role_prompts/v1).
///
/// Returns the absent-marker output when `base/manifest.json` does not exist.
/// Any present-but-invalid state returns the full issue list (fail-closed).
pub fn validate_assets_tree(base: &Path) -> Result<ValidationOutput, Vec<ValidationIssue>> {
    let manifest_path: PathBuf = base.join(MANIFEST_FILE_NAME);
    if !manifest_path.exists() {
        // Pre-RP-02 state: no production assets declared; gate stays open only
        // in the sense that nothing is declared. Any file placed inside the
        // assets root without a manifest entry is caught once RP-02 lands.
        return Ok(ValidationOutput::absent());
    }

    let mut issues: Vec<ValidationIssue> = Vec::new();

    let manifest_bytes = match fs::read(&manifest_path) {
        Ok(b) => b,
        Err(e) => {
            return Err(vec![ValidationIssue::new(
                "E_ASSET_MANIFEST_UNREADABLE",
                format!("manifest.json unreadable: {e}"),
            )])
        }
    };
    let manifest_sha256 = sha256_hex(&manifest_bytes);
    let manifest: Value = match serde_json::from_slice(&manifest_bytes) {
        Ok(v) => v,
        Err(e) => {
            return Err(vec![ValidationIssue::new(
                "E_ASSET_MANIFEST_INVALID",
                format!("manifest.json is not valid JSON: {e}"),
            )])
        }
    };

    // §11.1 item 1: schema resolvable + exact schema_version + closed field set.
    let obj = match manifest.as_object() {
        // §11.1 item 2 (partial): template ids/paths unique — the duplicate scan
        // below covers uniqueness; closed-field checks live here and per-entry.
        Some(o) => o,
        None => {
            return Err(vec![ValidationIssue::new(
                "E_ASSET_MANIFEST_INVALID",
                "manifest.json root must be a JSON object",
            )])
        }
    };
    for key in obj.keys() {
        if !matches!(key.as_str(), "schema_version" | "compiler_policy" | "templates") {
            issues.push(ValidationIssue::new(
                "E_ASSET_MANIFEST_INVALID",
                format!("manifest.json has unknown top-level field {key:?}"),
            ));
        }
    }
    match obj.get("schema_version").and_then(|v| v.as_str()) {
        Some(v) if v == MANIFEST_SCHEMA_VERSION => {}
        other => issues.push(ValidationIssue::new(
            "E_ASSET_MANIFEST_INVALID",
            format!(
                "schema_version must be {MANIFEST_SCHEMA_VERSION:?}, got {:?}",
                other.map(|s| s.to_string())
            ),
        )),
    }

    // compiler_policy: optional-but-if-present must be a closed object with a
    // real readable+hashed file.
    let mut policy_sha: Option<String> = None;
    if let Some(p) = obj.get("compiler_policy") {
        match p.as_object() {
            Some(po) => {
                for key in po.keys() {
                    if !matches!(key.as_str(), "path" | "content_sha256") {
                        issues.push(ValidationIssue::new(
                            "E_ASSET_MANIFEST_INVALID",
                            format!("compiler_policy has unknown field {key:?}"),
                        ));
                    }
                }
                let path = po.get("path").and_then(|v| v.as_str()).unwrap_or("");
                let declared = po.get("content_sha256");
                if path.is_empty() {
                    issues.push(ValidationIssue::new(
                        "E_ASSET_PATH_INVALID",
                        "compiler_policy.path missing or empty",
                    ));
                } else if let Err(i) = check_relative_asset_path(path) {
                    issues.push(i);
                } else if let Some(declared) = declared {
                    match fs::read(base.join(path)) {
                        Ok(bytes) => {
                            if let Some(i) = scan_secrets(path, &bytes) {
                                issues.push(i);
                            }
                            match normalize_sha256(declared, "compiler_policy.content_sha256") {
                                Ok(norm) => {
                                    let actual = sha256_hex(&bytes);
                                    if actual != norm {
                                        issues.push(ValidationIssue::new(
                                            "E_ASSET_HASH_MISMATCH",
                                            format!("compiler_policy {path}: declared {norm} but actual {actual}"),
                                        ));
                                    }
                                    policy_sha = Some(actual);
                                }
                                Err(i) => issues.push(i),
                            }
                        }
                        Err(e) => issues.push(ValidationIssue::new(
                            "E_ASSET_FILE_UNREADABLE",
                            format!("compiler_policy {path} unreadable: {e}"),
                        )),
                    }
                }
            }
            None => issues.push(ValidationIssue::new(
                "E_ASSET_MANIFEST_INVALID",
                "compiler_policy must be an object",
            )),
        }
    }

    // templates array.
    let templates_json = match obj.get("templates").and_then(|v| v.as_array()) {
        Some(a) => a,
        None => {
            issues.push(ValidationIssue::new(
                "E_ASSET_MANIFEST_INVALID",
                "manifest.json must contain a `templates` array",
            ));
            return Err(issues);
        }
    };
    if templates_json.is_empty() {
        issues.push(ValidationIssue::new(
            "E_ASSET_MANIFEST_INVALID",
            "templates array must not be empty when the manifest is declared",
        ));
    }

    let mut ids: BTreeSet<String> = BTreeSet::new();
    let mut paths: BTreeSet<String> = BTreeSet::new();
    let mut route_owner: std::collections::BTreeMap<String, String> = Default::default();
    let mut validated: Vec<ValidatedTemplate> = Vec::new();

    for (idx, t) in templates_json.iter().enumerate() {
        let ctx = format!("templates[{idx}]");
        let to = match t.as_object() {
            Some(o) => o,
            None => {
                issues.push(ValidationIssue::new(
                    "E_ASSET_MANIFEST_INVALID",
                    format!("{ctx} must be an object"),
                ));
                continue;
            }
        };
        for key in to.keys() {
            if !matches!(
                key.as_str(),
                "template_id"
                    | "kind"
                    | "role"
                    | "routes"
                    | "path"
                    | "content_sha256"
                    | "source_declared_template_id"
                    | "legacy_alias_mismatch"
            ) {
                issues.push(ValidationIssue::new(
                    "E_ASSET_MANIFEST_INVALID",
                    format!("{ctx} has unknown field {key:?}"),
                ));
            }
        }

        let template_id = string_field(t, "template_id", &ctx);
        let kind = string_field(t, "kind", &ctx);
        let path = string_field(t, "path", &ctx);
        let (template_id, kind, path) = match (template_id, kind, path) {
            (Ok(a), Ok(b), Ok(c)) => (a, b, c),
            _ => continue, // field errors already recorded
        };

        if let Err(i) = check_template_id(&template_id) {
            issues.push(ValidationIssue::new(i.code, format!("{ctx}: {}", i.message)));
        }
        if !ids.insert(template_id.clone()) {
            issues.push(ValidationIssue::new(
                "E_ASSET_ID_DUPLICATE",
                format!("{ctx}: duplicate template_id {template_id:?}"),
            ));
        }
        if let Err(i) = check_relative_asset_path(&path) {
            issues.push(ValidationIssue::new(i.code, format!("{ctx}: {}", i.message)));
        }
        if !paths.insert(path.clone()) {
            issues.push(ValidationIssue::new(
                "E_ASSET_PATH_DUPLICATE",
                format!("{ctx}: duplicate path {path:?}"),
            ));
        }

        let is_role = kind == "role";
        let is_system = kind == "system";
        if !is_role && !is_system {
            issues.push(ValidationIssue::new(
                "E_ASSET_MANIFEST_INVALID",
                format!("{ctx}: kind must be \"role\" or \"system\", got {kind:?}"),
            ));
        }

        // role field: required for role kind, forbidden for system kind.
        let role = t.get("role").and_then(|v| v.as_str()).map(|s| s.to_string());
        if is_role {
            match role.as_deref() {
                Some(r) if matches!(r, "executor" | "reviewer" | "adjudicator" | "planner") => {}
                Some(r) => issues.push(ValidationIssue::new(
                    "E_ASSET_MANIFEST_INVALID",
                    format!("{ctx}: unknown role {r:?}"),
                )),
                None => issues.push(ValidationIssue::new(
                    "E_ASSET_MANIFEST_INVALID",
                    format!("{ctx}: role templates require a `role` field"),
                )),
            }
        } else if role.is_some() {
            issues.push(ValidationIssue::new(
                "E_ASSET_MANIFEST_INVALID",
                format!("{ctx}: system templates must not declare a `role`"),
            ));
        }

        // routes: role ⇒ ≥1 action-ready route; system ⇒ none (§6, §11.1 item 7).
        let mut routes: Vec<String> = Vec::new();
        match t.get("routes").and_then(|v| v.as_array()) {
            Some(rs) => {
                for r in rs {
                    match r.as_str() {
                        Some(rv) if ALLOWED_ROUTES.contains(&rv) => routes.push(rv.to_string()),
                        Some(rv) => issues.push(ValidationIssue::new(
                            "E_ASSET_ROUTE_INVALID",
                            format!("{ctx}: route {rv:?} is not an action-ready route"),
                        )),
                        None => issues.push(ValidationIssue::new(
                            "E_ASSET_ROUTE_INVALID",
                            format!("{ctx}: routes must be strings"),
                        )),
                    }
                }
            }
            None => issues.push(ValidationIssue::new(
                "E_ASSET_MANIFEST_INVALID",
                format!("{ctx}: routes must be an array"),
            )),
        }
        // SR-02 (Option B, user-adjudicated 2026-09-06): role-kind templates
        // MAY declare routes:[]. The frozen spec §11.1(4) "route 完整无多义"
        // is read as route-coverage semantics: global uniqueness below keeps
        // 无多义 (each route has exactly one owning template), and "完整" is
        // the production tree's job (current executor/reviewer/adjudicator
        // cover all 4 routes). The former per-template ">=1 route" mandate
        // made the frozen §7.3 tree (7 role templates incl. routeless
        // legacy x3 and planner_v1) unsatisfiable: §6 hard-errors READY/PLAN
        // in v1, and legacy templates are selected by exact ID+hash per
        // §7.1 rather than by route. System templates still must not claim
        // action-ready routes (§11.1(7)).
        if is_system && !routes.is_empty() {
            issues.push(ValidationIssue::new(
                "E_ASSET_ROUTE_INVALID",
                format!("{ctx}: system templates must not produce action-ready routes"),
            ));
        }
        for r in &routes {
            match route_owner.get(r) {
                Some(prev) => issues.push(ValidationIssue::new(
                    "E_ASSET_ROUTE_AMBIGUOUS",
                    format!("route {r:?} claimed by both {prev:?} and {template_id:?}"),
                )),
                None => {
                    route_owner.insert(r.clone(), template_id.clone());
                }
            }
        }

        // content hash: declared form + real bytes.
        let declared = t.get("content_sha256");
        let norm_declared = match declared {
            Some(d) => match normalize_sha256(d, &format!("{ctx}.content_sha256")) {
                Ok(n) => Some(n),
                Err(i) => {
                    issues.push(i);
                    None
                }
            },
            None => {
                issues.push(ValidationIssue::new(
                    "E_ASSET_MANIFEST_INVALID",
                    format!("{ctx}: missing content_sha256"),
                ));
                None
            }
        };

        let file_path = base.join(&path);
        let body_bytes = match fs::read(&file_path) {
            Ok(b) => Some(b),
            Err(e) => {
                issues.push(ValidationIssue::new(
                    "E_ASSET_FILE_UNREADABLE",
                    format!("{ctx}: asset {path:?} unreadable: {e}"),
                ));
                None
            }
        };

        if let (Some(norm), Some(bytes)) = (norm_declared.clone(), body_bytes.as_ref()) {
            let actual = sha256_hex(bytes);
            if actual != norm {
                issues.push(ValidationIssue::new(
                    "E_ASSET_HASH_MISMATCH",
                    format!("{ctx}: asset {path:?} declared {norm} but actual {actual}"),
                ));
            }
            // §11.1 item 3: UTF-8 + LF only.
            match std::str::from_utf8(bytes) {
                Ok(text) => {
                    if text.contains('\r') {
                        issues.push(ValidationIssue::new(
                            "E_ASSET_BYTES_INVALID",
                            format!("{ctx}: asset {path:?} must use LF line endings (CR found)"),
                        ));
                    }
                    if let Some(i) = check_body_declared_id(
                        &path,
                        text,
                        &template_id,
                        template_id == LEGACY_EXCEPTION_TEMPLATE_ID
                            && norm == format!("sha256:{LEGACY_EXCEPTION_CONTENT_SHA256}"),
                    ) {
                        issues.push(i);
                    }
                }
                Err(_) => issues.push(ValidationIssue::new(
                    "E_ASSET_BYTES_INVALID",
                    format!("{ctx}: asset {path:?} is not valid UTF-8"),
                )),
            }
            if let Some(i) = scan_secrets(&path, bytes) {
                issues.push(ValidationIssue::new(i.code, format!("{ctx}: {}", i.message)));
            }
        }

        // §7.2 legacy alias exception: exact ID + exact hash + declared body id.
        let legacy_flag = match t.get("legacy_alias_mismatch") {
            None | Some(Value::Bool(false)) => false,
            Some(Value::Bool(true)) => true,
            Some(_) => {
                issues.push(ValidationIssue::new(
                    "E_ASSET_MANIFEST_INVALID",
                    format!("{ctx}: legacy_alias_mismatch must be a bool"),
                ));
                false
            }
        };
        let source_declared = match t.get("source_declared_template_id") {
            None => None,
            Some(Value::String(s)) => Some(s.clone()),
            Some(_) => {
                issues.push(ValidationIssue::new(
                    "E_ASSET_MANIFEST_INVALID",
                    format!("{ctx}: source_declared_template_id must be a string"),
                ));
                None
            }
        };
        if legacy_flag {
            let id_ok = template_id == LEGACY_EXCEPTION_TEMPLATE_ID;
            let hash_ok = norm_declared.as_deref() == Some(&format!("sha256:{LEGACY_EXCEPTION_CONTENT_SHA256}"));
            let body_ok = source_declared.as_deref() == Some(LEGACY_EXCEPTION_BODY_ID);
            if !(id_ok && hash_ok && body_ok) {
                issues.push(ValidationIssue::new(
                    "E_ASSET_LEGACY_EXCEPTION_VIOLATED",
                    format!(
                        "{ctx}: legacy_alias_mismatch=true allowed only for exact ID {:?} + hash sha256:{} + source_declared_template_id {:?} (id_ok={id_ok} hash_ok={hash_ok} body_ok={body_ok})",
                        LEGACY_EXCEPTION_TEMPLATE_ID, LEGACY_EXCEPTION_CONTENT_SHA256, LEGACY_EXCEPTION_BODY_ID
                    ),
                ));
            }
        } else if let Some(sd) = &source_declared {
            if sd != &template_id {
                issues.push(ValidationIssue::new(
                    "E_ASSET_BODY_ID_MISMATCH",
                    format!("{ctx}: source_declared_template_id {sd:?} != primary id {template_id:?} without the frozen legacy exception"),
                ));
            }
        }

        validated.push(ValidatedTemplate {
            template_id,
            kind: kind.clone(),
            role: role.clone(),
            routes,
            path,
            content_sha256: norm_declared.unwrap_or_default(),
            source_declared_template_id: source_declared,
            legacy_alias_mismatch: legacy_flag,
        });
    }

    if !issues.is_empty() {
        return Err(issues);
    }

    Ok(ValidationOutput {
        manifest_present: true,
        manifest_sha256,
        compiler_policy_sha256: policy_sha,
        templates: validated,
    })
}
