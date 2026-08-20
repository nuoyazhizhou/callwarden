//! Rust `cw rule/guardrail/check-gate/audit/bootstrap` 安全链。
//!
//! 这些事实属于当前 UID 的本地数据库。即使代码查询使用 enterprise daemon，
//! 规则审核、编辑门禁和用户审计链仍固定留在本地，避免形成两个可写真相源。

use std::collections::HashMap;
use std::fs;
use std::fs::File;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};

use regex::{Regex, RegexBuilder};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::Serialize;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::canonicalize::canonicalize_source;
use crate::daemon::replicator::detect_language_from_path;
use crate::multi_lang::{GenericParser, LangConfig};

static SECURITY_ID_COUNTER: AtomicU64 = AtomicU64::new(0);

const RULE_STATUS_ACTIVE: &str = "active";
const CANDIDATE_PENDING: &str = "pending";
const CANDIDATE_ACCEPTED: &str = "accepted";
const CANDIDATE_REJECTED: &str = "rejected";
const MARKER_START: &str = "<!-- CALLWARDEN_RULES_START -->";
const MARKER_END: &str = "<!-- CALLWARDEN_RULES_END -->";
const MARKER_HINT: &str = "<!-- 自动同步区域，请通过 cw rule sync 更新，不要手改 -->";

#[derive(Debug, Clone)]
pub struct RuleCandidateInput {
    pub title: String,
    pub rule_text: String,
    pub scope: Value,
    pub severity: String,
    pub source: String,
    pub evidence: Value,
    pub confidence: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct RuleCandidate {
    pub id: String,
    pub title: String,
    pub rule_text: String,
    pub scope: Value,
    pub severity: String,
    pub source: String,
    pub evidence: Value,
    pub confidence: f64,
    pub status: String,
    pub created_at: f64,
    pub reviewed_at: Option<f64>,
    pub reviewer: String,
    pub linked_rule_id: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct AgentRule {
    pub id: String,
    pub title: String,
    pub rule_text: String,
    pub scope: Value,
    pub severity: String,
    pub status: String,
    pub source_candidate_id: String,
    pub evidence: Value,
    pub created_at: f64,
    pub updated_at: f64,
    pub synced_to_agents_md: bool,
    pub sync_hash: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub matched_scope: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct RuleSyncResult {
    pub success: bool,
    pub dry_run: bool,
    pub target_path: String,
    pub rule_count: usize,
    pub rule_ids: Vec<String>,
    pub before_hash: String,
    pub after_hash: String,
    pub preview: String,
    pub error: String,
    pub suggested_block: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct RuleSeedItem {
    pub id: String,
    pub title: String,
    pub action: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct RuleSeedResult {
    pub dry_run: bool,
    pub total: usize,
    pub created: usize,
    pub updated: usize,
    pub skipped: usize,
    pub rules: Vec<RuleSeedItem>,
}

#[derive(Debug, Clone, Serialize)]
pub struct RuleCleanupResult {
    pub success: bool,
    pub dry_run: bool,
    pub deleted_count: i64,
    pub remaining_count: i64,
    pub total_before: i64,
    pub older_than_days: i64,
    pub keep_latest: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct GuardrailRule {
    pub rule_id: String,
    pub category: String,
    pub severity: String,
    pub pattern: String,
    pub action: String,
    pub description: String,
    pub is_builtin: bool,
    pub created_at: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct GuardrailFinding {
    pub id: i64,
    pub rule_id: String,
    pub file_path: String,
    pub symbol_hash: String,
    pub severity: String,
    pub status: String,
    pub message: String,
    pub detected_at: f64,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub finding_type: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub check: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub line: Option<u32>,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub raw_severity: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct GuardrailScanResult {
    pub scanned_files: usize,
    pub findings: Vec<GuardrailFinding>,
}

#[derive(Debug, Clone, Serialize)]
pub struct CheckGateResult {
    pub task_id: String,
    pub step_id: String,
    pub passed: bool,
    pub checks_run: Vec<String>,
    pub findings: Vec<GuardrailFinding>,
    pub fix_required: bool,
    pub summary: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct GateResolveResult {
    pub task_id: String,
    pub resolved_count: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct AuditBrokenRecord {
    pub id: i64,
    pub table_name: String,
    pub record_id: String,
    pub reasons: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct AuditVerifyResult {
    pub table_name: String,
    pub total_count: usize,
    pub verified_count: usize,
    pub broken_count: usize,
    pub broken_records: Vec<AuditBrokenRecord>,
    pub security_level: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct AuditKeyInfo {
    pub key_id: String,
    pub rotated_at: f64,
    pub is_active: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct AuditRotateResult {
    pub success: bool,
    pub key_id: String,
    pub rotated_at: f64,
    pub previous_key_id: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct BootstrapLatestScan {
    pub id: i64,
    pub git_head: String,
    pub started_at: f64,
    pub status: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct BootstrapStatus {
    pub db_stale: bool,
    pub current_head: String,
    pub active_rules_count: i64,
    pub pending_candidates_count: i64,
    pub open_findings_count: i64,
    pub blocking_findings_count: i64,
    pub audit_verify: AuditVerifyResult,
    pub latest_scan_run: Option<BootstrapLatestScan>,
    pub tasks: HashMap<String, i64>,
    pub recommended_next_action: String,
}

const BUILTIN_GUARDRAIL_RULES: [(&str, &str, &str, &str, &str, &str); 9] = [
    (
        "GR-builtin-db-1",
        "db_safety",
        "warn",
        r"\bALTER\s+TABLE\b",
        "warn",
        "Detect ALTER TABLE statements (schema change risk)",
    ),
    (
        "GR-builtin-db-2",
        "db_safety",
        "block",
        r"\bDROP\s+(TABLE|COLUMN)\b",
        "block",
        "Detect DROP TABLE / DROP COLUMN statements (data loss risk)",
    ),
    (
        "GR-builtin-db-3",
        "db_safety",
        "block",
        r"VARCHAR\s*\(\s*(\d+)\s*\)\s*(?:→|->)\s*VARCHAR\s*\(\s*(\d+)\s*\)",
        "block",
        "Detect VARCHAR length shrinkage (data truncation risk)",
    ),
    (
        "GR-builtin-api-1",
        "api_compat",
        "block",
        r"#\s*BREAKING\s+CHANGE",
        "block",
        "Detect BREAKING CHANGE markers (including reduced pub fn visibility)",
    ),
    (
        "GR-builtin-api-2",
        "api_compat",
        "block",
        r"//\s*REMOVED\s+PARAM",
        "block",
        "Detect removed function parameters (caller compatibility break)",
    ),
    (
        "GR-builtin-api-3",
        "api_compat",
        "block",
        r"//\s*REMOVED\s+FIELD",
        "block",
        "Detect removed pub struct fields (struct compatibility break)",
    ),
    (
        "GR-builtin-inc-1",
        "incident",
        "warn",
        r"fn\s+\w+.*\{[^}]*(?:try|catch|unwrap|expect|\?|Result)",
        "warn",
        "Detect functions missing error handling (no try/catch/unwrap/expect/?/Result)",
    ),
    (
        "GR-builtin-inc-2",
        "incident",
        "info",
        r"fn\s+\w+.*\{[^}]*(?:log::|tracing::|println!|print!)",
        "warn",
        "Detect functions missing logging (no log::/tracing::/println!/print!)",
    ),
    (
        "GR-builtin-inc-3",
        "incident",
        "warn",
        r"(?:INSERT|UPDATE|DELETE|write|save|commit).*(?:rollback|transaction|begin|undo)",
        "warn",
        "Detect write operations without transaction/rollback logic",
    ),
];

pub fn list_guardrail_rules(
    conn: &mut Connection,
    category: &str,
) -> Result<Vec<GuardrailRule>, String> {
    ensure_builtin_guardrail_rules(conn)?;
    let mut sql =
        "SELECT rule_id,category,severity,pattern,action,description,is_builtin,created_at
                   FROM guardrail_rules"
            .to_string();
    if !category.trim().is_empty() {
        sql.push_str(" WHERE category=?1");
    }
    sql.push_str(" ORDER BY is_builtin DESC,created_at ASC");
    let mut statement = conn
        .prepare(&sql)
        .map_err(|error| format!("cannot prepare guardrail rule query: {error}"))?;
    let map_row = |row: &rusqlite::Row<'_>| {
        Ok(GuardrailRule {
            rule_id: row.get(0)?,
            category: row.get(1)?,
            severity: row.get(2)?,
            pattern: row.get(3)?,
            action: row.get(4)?,
            description: row.get(5)?,
            is_builtin: row.get::<_, i64>(6)? != 0,
            created_at: row.get(7)?,
        })
    };
    let rows = if category.trim().is_empty() {
        statement.query_map([], map_row)
    } else {
        statement.query_map(params![category.trim()], map_row)
    }
    .map_err(|error| format!("cannot query guardrail rules: {error}"))?;
    rows.collect::<rusqlite::Result<Vec<_>>>()
        .map_err(|error| format!("cannot decode guardrail rule: {error}"))
}

pub fn scan_guardrails(
    conn: &mut Connection,
    file_filter: &str,
    category: &str,
) -> Result<GuardrailScanResult, String> {
    validate_guardrail_category(category)?;
    let (workspace_id, workspace_root) = active_workspace(conn)?;
    let normalized_filter = normalize_safe_prefix(file_filter)?;
    let root = fs::canonicalize(&workspace_root).map_err(|error| {
        format!(
            "cannot canonicalize active workspace {}: {error}",
            workspace_root.display()
        )
    })?;
    let mut statement = conn
        .prepare(
            "SELECT rel_path,abs_path FROM file_instances
             WHERE workspace_id=?1 AND status!='archived' AND (?2='' OR rel_path LIKE ?3)
             ORDER BY rel_path",
        )
        .map_err(|error| format!("cannot prepare guardrail file query: {error}"))?;
    let prefix = format!("{normalized_filter}%");
    let files = statement
        .query_map(params![workspace_id, normalized_filter, prefix], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(|error| format!("cannot query guardrail files: {error}"))?
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(|error| format!("cannot decode guardrail file: {error}"))?;
    drop(statement);

    let mut pending = Vec::new();
    for (rel_path, abs_path) in &files {
        let safe_path = validate_indexed_file(&root, Path::new(abs_path), rel_path)?;
        let canonical = canonicalize_source(&safe_path.to_string_lossy()).map_err(|error| {
            format!(
                "cannot read guardrail evidence {}: {error}",
                safe_path.display()
            )
        })?;
        let content = String::from_utf8(canonical.canonical_bytes).map_err(|error| {
            format!("canonical guardrail evidence is not UTF-8 for {rel_path}: {error}")
        })?;
        pending.extend(detect_guardrail_findings(&content, rel_path, category)?);
    }

    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot begin guardrail scan transaction: {error}"))?;
    insert_builtin_guardrail_rules(&tx)?;
    let now = now_epoch()?;
    let mut inserted = Vec::new();
    for finding in pending {
        if let Some(finding) = insert_guardrail_finding(&tx, workspace_id, &finding, now, true)? {
            inserted.push(finding);
        }
    }
    tx.commit()
        .map_err(|error| format!("cannot commit guardrail scan: {error}"))?;
    Ok(GuardrailScanResult {
        scanned_files: files.len(),
        findings: inserted,
    })
}

pub fn run_check_gate(
    conn: &mut Connection,
    task_id: &str,
    step_id: &str,
    semgrep_timeout_secs: u64,
) -> Result<CheckGateResult, String> {
    require_task(conn, task_id)?;
    let (workspace_id, workspace_root) = active_workspace(conn)?;
    let changed_files = task_changed_files(conn, task_id)?;
    if changed_files.is_empty() {
        return Err(format!(
            "task {task_id} has no change_audit evidence; check gate fails closed"
        ));
    }
    let root = fs::canonicalize(&workspace_root).map_err(|error| {
        format!(
            "cannot canonicalize active workspace {}: {error}",
            workspace_root.display()
        )
    })?;
    let mut resolved_files = Vec::with_capacity(changed_files.len());
    let mut pending = Vec::new();
    let mut syntax_ran = false;
    for rel_path in &changed_files {
        let abs_path = resolve_workspace_evidence_path(conn, workspace_id, &root, rel_path)?;
        let canonical = canonicalize_source(&abs_path.to_string_lossy()).map_err(|error| {
            format!(
                "cannot read check-gate evidence {}: {error}",
                abs_path.display()
            )
        })?;
        let language = detect_language_from_path(rel_path);
        if !language.is_empty() {
            syntax_ran = true;
            let config = LangConfig::get(&language).ok_or_else(|| {
                format!("Rust parser registry missing supported language {language}")
            })?;
            let parser = GenericParser::new(std::sync::Arc::new(config));
            let result = parser.parse_canonical_bytes(
                &canonical.canonical_bytes,
                &abs_path.to_string_lossy(),
                "",
                &canonical.content_hash,
            );
            if let Some(error) = result.error.or(result.diagnostics.fatal_parse_error) {
                pending.push(PendingFinding::gate(
                    "syntax",
                    rel_path,
                    "error",
                    "gate_syntax_error",
                    format!("syntax error: {error}"),
                    None,
                    "ERROR",
                ));
            } else if result.diagnostics.syntax_error_count > 0 {
                pending.push(PendingFinding::gate(
                    "syntax",
                    rel_path,
                    "error",
                    "gate_syntax_error",
                    format!(
                        "syntax error: {} occurrence(s)",
                        result.diagnostics.syntax_error_count
                    ),
                    None,
                    "ERROR",
                ));
            } else if result.diagnostics.unsupported_construct_count > 0 {
                pending.push(PendingFinding::gate(
                    "syntax",
                    rel_path,
                    "warn",
                    "gate_syntax_warn",
                    format!(
                        "unsupported syntax construct: {} occurrence(s)",
                        result.diagnostics.unsupported_construct_count
                    ),
                    None,
                    "WARNING",
                ));
            }
        }
        resolved_files.push((rel_path.clone(), abs_path));
    }

    let semgrep_findings = run_semgrep_batch(&root, &resolved_files, semgrep_timeout_secs)
        .unwrap_or_else(|error| {
            vec![PendingFinding::gate(
                "semgrep",
                "<check-gate>",
                "error",
                "gate_semgrep_error",
                format!("Semgrep check failed closed: {error}"),
                None,
                "ERROR",
            )]
        });
    pending.extend(semgrep_findings);

    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot begin check-gate transaction: {error}"))?;
    let now = now_epoch()?;
    let mut inserted = Vec::new();
    for finding in pending {
        ensure_gate_rule(&tx, &finding, now)?;
        if let Some(finding) = insert_guardrail_finding(&tx, workspace_id, &finding, now, false)? {
            inserted.push(finding);
        }
    }
    tx.commit()
        .map_err(|error| format!("cannot commit check-gate findings: {error}"))?;

    let mut checks_run = Vec::new();
    if syntax_ran {
        checks_run.push("syntax".to_string());
    }
    checks_run.push("semgrep".to_string());
    let passed = !inserted
        .iter()
        .any(|finding| matches!(finding.severity.as_str(), "error" | "block"));
    let summary = checks_run
        .iter()
        .map(|check| {
            let failed = inserted.iter().any(|finding| {
                finding.check == *check && matches!(finding.severity.as_str(), "error" | "block")
            });
            format!("{check}:{}", if failed { "FAIL" } else { "pass" })
        })
        .collect::<Vec<_>>()
        .join(", ");
    Ok(CheckGateResult {
        task_id: task_id.to_string(),
        step_id: step_id.to_string(),
        passed,
        checks_run,
        findings: inserted,
        fix_required: !passed,
        summary: format!(
            "check {}: {summary}",
            if passed { "passed" } else { "failed" }
        ),
    })
}

pub fn resolve_gate_findings(
    conn: &mut Connection,
    task_id: &str,
) -> Result<GateResolveResult, String> {
    require_task(conn, task_id)?;
    let files = task_changed_files(conn, task_id)?;
    if files.is_empty() {
        return Err(format!(
            "task {task_id} has no change_audit evidence; resolve refused"
        ));
    }
    let (workspace_id, _) = active_workspace(conn)?;
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot begin gate resolve transaction: {error}"))?;
    let now = now_epoch()?;
    let mut resolved_count = 0_i64;
    for file in files {
        resolved_count += tx
            .execute(
                "UPDATE guardrail_findings SET status='resolved',resolved_at=?1
                 WHERE workspace_id=?2 AND file_path=?3 AND status='open' AND rule_id IN
                   (SELECT rule_id FROM guardrail_rules WHERE category='check_gate')",
                params![now, workspace_id, file],
            )
            .map_err(|error| format!("cannot resolve gate findings for {file}: {error}"))?
            as i64;
    }
    tx.commit()
        .map_err(|error| format!("cannot commit gate resolution: {error}"))?;
    Ok(GateResolveResult {
        task_id: task_id.to_string(),
        resolved_count,
    })
}

pub fn verify_audit_chain(
    conn: &Connection,
    table_name: &str,
    limit: i64,
) -> Result<AuditVerifyResult, String> {
    if limit <= 0 {
        return Err("audit verify limit must be positive".to_string());
    }
    let security_level = active_audit_key(conn)?.2;
    let mut sql = "SELECT id,table_name,record_id,payload_hash,prev_signature,
                          record_signature,signing_key_id
                   FROM audit_chain"
        .to_string();
    if table_name.trim().is_empty() {
        sql.push_str(" ORDER BY id ASC LIMIT ?1");
    } else {
        sql.push_str(" WHERE table_name=?1 ORDER BY id ASC LIMIT ?2");
    }
    let mut statement = conn
        .prepare(&sql)
        .map_err(|error| format!("cannot prepare audit verification: {error}"))?;
    let decode = |row: &rusqlite::Row<'_>| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, String>(4)?,
            row.get::<_, String>(5)?,
            row.get::<_, String>(6)?,
        ))
    };
    let records = if table_name.trim().is_empty() {
        statement.query_map(params![limit], decode)
    } else {
        statement.query_map(params![table_name.trim(), limit], decode)
    }
    .map_err(|error| format!("cannot query audit chain: {error}"))?
    .collect::<rusqlite::Result<Vec<_>>>()
    .map_err(|error| format!("cannot decode audit chain: {error}"))?;

    let mut previous = HashMap::<String, String>::new();
    let mut broken_records = Vec::new();
    let mut verified_count = 0_usize;
    for (id, row_table, record_id, payload_hash, prev_signature, signature, key_id) in &records {
        let expected_prev = previous.get(row_table).cloned().unwrap_or_default();
        let mut reasons = Vec::new();
        if prev_signature != &expected_prev {
            reasons.push("chain_broken".to_string());
        }
        if expected_prev.is_empty() && !prev_signature.is_empty() {
            reasons.push("first_prev_not_empty".to_string());
        }
        let key = lookup_audit_key(conn, key_id)?;
        let recomputed = if key_id == "local" {
            crate::daemon_query::audit_compute_signature(prev_signature, payload_hash, None)
        } else if let Some(key) = key {
            crate::daemon_query::audit_compute_signature(prev_signature, payload_hash, Some(&key))
        } else {
            reasons.push("signing_key_missing".to_string());
            String::new()
        };
        if !recomputed.is_empty() && recomputed != *signature {
            reasons.push("signature_mismatch".to_string());
        }
        if reasons.is_empty() {
            verified_count += 1;
        } else {
            broken_records.push(AuditBrokenRecord {
                id: *id,
                table_name: row_table.clone(),
                record_id: record_id.clone(),
                reasons,
            });
        }
        previous.insert(row_table.clone(), signature.clone());
    }
    Ok(AuditVerifyResult {
        table_name: table_name.trim().to_string(),
        total_count: records.len(),
        verified_count,
        broken_count: broken_records.len(),
        broken_records,
        security_level,
    })
}

pub fn rotate_audit_key(
    conn: &mut Connection,
    key_id: &str,
    secret: &str,
) -> Result<AuditRotateResult, String> {
    let key_id = key_id.trim();
    if key_id.is_empty() {
        return Err("new_key_id is required".to_string());
    }
    if secret.is_empty() {
        return Err("new_key_secret is required".to_string());
    }
    if key_id == "local" || key_id == "hmac" {
        return Err("key_id local/hmac is reserved for legacy audit records".to_string());
    }
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot begin audit key rotation: {error}"))?;
    let active = query_active_rotation_keys(&tx)?;
    if active.len() > 1 {
        return Err("multiple active audit keys; rotation fails closed".to_string());
    }
    let previous_key_id = active.first().map(|key| key.0.clone()).unwrap_or_default();
    let now = now_epoch()?;
    tx.execute(
        "UPDATE audit_key_rotations SET is_active=0 WHERE is_active=1",
        [],
    )
    .map_err(|error| format!("cannot deactivate old audit key: {error}"))?;
    tx.execute(
        "INSERT INTO audit_key_rotations(key_id,key_secret,rotated_at,is_active)
         VALUES (?1,?2,?3,1)
         ON CONFLICT(key_id) DO UPDATE SET
           key_secret=excluded.key_secret,rotated_at=excluded.rotated_at,is_active=1",
        params![key_id, secret, now],
    )
    .map_err(|error| format!("cannot activate audit key {key_id}: {error}"))?;
    tx.commit()
        .map_err(|error| format!("cannot commit audit key rotation: {error}"))?;
    Ok(AuditRotateResult {
        success: true,
        key_id: key_id.to_string(),
        rotated_at: now,
        previous_key_id,
    })
}

pub fn list_audit_keys(conn: &Connection) -> Result<Vec<AuditKeyInfo>, String> {
    let mut statement = conn
        .prepare(
            "SELECT key_id,rotated_at,is_active FROM audit_key_rotations
             ORDER BY rotated_at DESC,key_id ASC",
        )
        .map_err(|error| format!("cannot prepare audit key query: {error}"))?;
    let rows = statement
        .query_map([], |row| {
            Ok(AuditKeyInfo {
                key_id: row.get(0)?,
                rotated_at: row.get(1)?,
                is_active: row.get::<_, i64>(2)? != 0,
            })
        })
        .map_err(|error| format!("cannot query audit keys: {error}"))?
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(|error| format!("cannot decode audit key: {error}"))?;
    Ok(rows)
}

pub fn generate_audit_secret() -> Result<String, String> {
    let first = generate_security_id("audit-key")?;
    let second = generate_security_id("audit-key")?;
    Ok(sha256_hex(format!("{first}:{second}").as_bytes()))
}

pub fn bootstrap_status(conn: &Connection) -> Result<BootstrapStatus, String> {
    let (workspace_id, workspace_root) = active_workspace(conn)?;
    let latest_scan_run = conn
        .query_row(
            "SELECT id,git_head,started_at,status FROM workspace_scan_runs
             WHERE workspace_id=?1 ORDER BY started_at DESC,id DESC LIMIT 1",
            params![workspace_id],
            |row| {
                Ok(BootstrapLatestScan {
                    id: row.get(0)?,
                    git_head: row.get(1)?,
                    started_at: row.get(2)?,
                    status: row.get(3)?,
                })
            },
        )
        .optional()
        .map_err(|error| format!("cannot query latest workspace scan: {error}"))?;
    let current_head = current_git_head(&workspace_root)?;
    let db_stale = latest_scan_run.as_ref().is_some_and(|scan| {
        !current_head.is_empty() && !scan.git_head.is_empty() && scan.git_head != current_head
    });
    let active_rules_count = count_where(conn, "agent_rules", "status='active'")?;
    let pending_candidates_count = count_where(conn, "agent_rule_candidates", "status='pending'")?;
    let open_findings_count = count_where(conn, "task_quality_findings", "status='open'")?;
    let blocking_findings_count = count_where(
        conn,
        "task_quality_findings",
        "status='open' AND severity='block'",
    )?;
    let audit_verify = verify_audit_chain(conn, "", 500)?;
    let mut tasks = HashMap::from([
        ("open".to_string(), 0_i64),
        ("in_progress".to_string(), 0_i64),
        ("review".to_string(), 0_i64),
        ("applied".to_string(), 0_i64),
    ]);
    let mut statement = conn
        .prepare("SELECT status,COUNT(*) FROM tasks GROUP BY status")
        .map_err(|error| format!("cannot prepare bootstrap task counts: {error}"))?;
    let counts = statement
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })
        .map_err(|error| format!("cannot query bootstrap task counts: {error}"))?
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(|error| format!("cannot decode bootstrap task counts: {error}"))?;
    for (status, count) in counts {
        if let Some(slot) = tasks.get_mut(&status) {
            *slot = count;
        }
    }
    let recommended_next_action = if db_stale {
        "cw --refresh-all"
    } else if blocking_findings_count > 0 {
        "cw task findings <task_id>  # 有阻塞发现需修复"
    } else if pending_candidates_count > 0 {
        "cw rule candidate  # 有待审核的候选规则"
    } else if audit_verify.broken_count > 0 {
        "cw audit verify  # 审计链有损坏记录"
    } else if tasks.get("review").copied().unwrap_or(0) > 0 {
        "cw task apply <task_id>  # 有任务待审核"
    } else {
        "cw task list  # 一切正常，查看任务列表"
    }
    .to_string();
    Ok(BootstrapStatus {
        db_stale,
        current_head,
        active_rules_count,
        pending_candidates_count,
        open_findings_count,
        blocking_findings_count,
        audit_verify,
        latest_scan_run,
        tasks,
        recommended_next_action,
    })
}

pub fn create_rule_candidate(
    conn: &mut Connection,
    input: &RuleCandidateInput,
) -> Result<String, String> {
    let title = input.title.trim();
    let rule_text = input.rule_text.trim();
    if title.is_empty() {
        return Err("rule candidate title is required".to_string());
    }
    if rule_text.is_empty() {
        return Err("rule candidate text is required".to_string());
    }
    require_json_object(&input.scope, "scope")?;
    require_json_object(&input.evidence, "evidence")?;
    let id = generate_security_id("ARC")?;
    let now = now_epoch()?;
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot begin candidate transaction: {error}"))?;
    tx.execute(
        "INSERT INTO agent_rule_candidates
         (id,title,rule_text,scope_json,severity,source,evidence_json,confidence,
          status,created_at,reviewed_at,reviewer,linked_rule_id)
         VALUES (?1,?2,?3,?4,?5,?6,?7,?8,'pending',?9,NULL,'','')",
        params![
            id,
            title,
            rule_text,
            compact_json(&input.scope)?,
            normalize_rule_severity(&input.severity),
            nonempty_or(&input.source, "manual"),
            compact_json(&input.evidence)?,
            input.confidence.clamp(0.0, 1.0),
            now,
        ],
    )
    .map_err(|error| format!("cannot insert rule candidate: {error}"))?;
    tx.commit()
        .map_err(|error| format!("cannot commit rule candidate: {error}"))?;
    Ok(id)
}

pub fn list_rule_candidates(
    conn: &Connection,
    status: &str,
    limit: i64,
) -> Result<Vec<RuleCandidate>, String> {
    if limit <= 0 {
        return Ok(Vec::new());
    }
    let mut sql = "SELECT id,title,rule_text,scope_json,severity,source,evidence_json,
                          confidence,status,created_at,reviewed_at,reviewer,linked_rule_id
                   FROM agent_rule_candidates"
        .to_string();
    if !status.is_empty() {
        sql.push_str(" WHERE status = ?1 ORDER BY created_at DESC LIMIT ?2");
        let mut stmt = conn
            .prepare(&sql)
            .map_err(|error| format!("cannot prepare candidate query: {error}"))?;
        let rows = stmt
            .query_map(params![status, limit], row_to_candidate)
            .map_err(|error| format!("cannot query candidates: {error}"))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| format!("cannot read candidates: {error}"))?;
        return Ok(rows);
    }
    sql.push_str(" ORDER BY created_at DESC LIMIT ?1");
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|error| format!("cannot prepare candidate query: {error}"))?;
    let rows = stmt
        .query_map(params![limit], row_to_candidate)
        .map_err(|error| format!("cannot query candidates: {error}"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("cannot read candidates: {error}"))?;
    Ok(rows)
}

pub fn accept_rule_candidate(
    conn: &mut Connection,
    candidate_id: &str,
    reviewer: &str,
) -> Result<String, String> {
    if candidate_id.trim().is_empty() {
        return Err("candidate_id is required".to_string());
    }
    if reviewer.trim().is_empty() {
        return Err("reviewer is required".to_string());
    }
    let now = now_epoch()?;
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot begin candidate review: {error}"))?;
    let row = tx
        .query_row(
            "SELECT title,rule_text,scope_json,severity,evidence_json,status,
                    COALESCE(linked_rule_id,'')
             FROM agent_rule_candidates WHERE id = ?1",
            params![candidate_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, String>(6)?,
                ))
            },
        )
        .optional()
        .map_err(|error| format!("cannot query candidate: {error}"))?
        .ok_or_else(|| format!("candidate not found: {candidate_id}"))?;
    if row.5 == CANDIDATE_REJECTED {
        return Err(format!("candidate already rejected: {candidate_id}"));
    }
    if row.5 == CANDIDATE_ACCEPTED && !row.6.is_empty() {
        let linked_exists = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM agent_rules WHERE id = ?1)",
                params![row.6],
                |value| value.get::<_, i64>(0),
            )
            .map_err(|error| format!("cannot validate linked rule: {error}"))?;
        if linked_exists != 0 {
            tx.commit()
                .map_err(|error| format!("cannot finish idempotent review: {error}"))?;
            return Ok(row.6);
        }
    } else if row.5 != CANDIDATE_PENDING {
        return Err(format!("candidate has invalid status {}", row.5));
    }
    let rule_id = generate_security_id("AR")?;
    tx.execute(
        "INSERT INTO agent_rules
         (id,title,rule_text,scope_json,severity,status,source_candidate_id,
          evidence_json,created_at,updated_at,synced_to_agents_md,sync_hash)
         VALUES (?1,?2,?3,?4,?5,'active',?6,?7,?8,?8,0,'')",
        params![
            rule_id,
            row.0,
            row.1,
            row.2,
            row.3,
            candidate_id,
            row.4,
            now
        ],
    )
    .map_err(|error| format!("cannot create active rule: {error}"))?;
    let changed = tx
        .execute(
            "UPDATE agent_rule_candidates
             SET status='accepted',reviewed_at=?1,reviewer=?2,linked_rule_id=?3
             WHERE id=?4 AND status IN ('pending','accepted')",
            params![now, reviewer.trim(), rule_id, candidate_id],
        )
        .map_err(|error| format!("cannot update candidate: {error}"))?;
    if changed != 1 {
        return Err("candidate review lost a concurrent update".to_string());
    }
    tx.commit()
        .map_err(|error| format!("cannot commit candidate review: {error}"))?;
    Ok(rule_id)
}

pub fn reject_rule_candidate(
    conn: &mut Connection,
    candidate_id: &str,
    reviewer: &str,
    reason: &str,
) -> Result<bool, String> {
    if candidate_id.trim().is_empty() {
        return Err("candidate_id is required".to_string());
    }
    if reviewer.trim().is_empty() {
        return Err("reviewer is required".to_string());
    }
    let now = now_epoch()?;
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot begin candidate rejection: {error}"))?;
    let row = tx
        .query_row(
            "SELECT status,COALESCE(evidence_json,'{}') FROM agent_rule_candidates WHERE id=?1",
            params![candidate_id],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()
        .map_err(|error| format!("cannot query candidate: {error}"))?
        .ok_or_else(|| format!("candidate not found: {candidate_id}"))?;
    if row.0 == CANDIDATE_ACCEPTED {
        return Err(format!("candidate already accepted: {candidate_id}"));
    }
    if row.0 == CANDIDATE_REJECTED {
        tx.commit()
            .map_err(|error| format!("cannot finish idempotent rejection: {error}"))?;
        return Ok(true);
    }
    if row.0 != CANDIDATE_PENDING {
        return Err(format!("candidate has invalid status {}", row.0));
    }
    let mut evidence = parse_json_object(&row.1);
    if !reason.is_empty() {
        evidence.insert(
            "reject_reason".to_string(),
            Value::String(reason.to_string()),
        );
    }
    let changed = tx
        .execute(
            "UPDATE agent_rule_candidates
             SET status='rejected',reviewed_at=?1,reviewer=?2,evidence_json=?3
             WHERE id=?4 AND status='pending'",
            params![
                now,
                reviewer.trim(),
                compact_json(&Value::Object(evidence))?,
                candidate_id
            ],
        )
        .map_err(|error| format!("cannot reject candidate: {error}"))?;
    if changed != 1 {
        return Err("candidate rejection lost a concurrent update".to_string());
    }
    tx.commit()
        .map_err(|error| format!("cannot commit candidate rejection: {error}"))?;
    Ok(true)
}

pub fn list_agent_rules(
    conn: &Connection,
    status: &str,
    limit: i64,
) -> Result<Vec<AgentRule>, String> {
    if limit <= 0 {
        return Ok(Vec::new());
    }
    let select = "SELECT id,title,rule_text,scope_json,severity,status,
                         source_candidate_id,evidence_json,created_at,updated_at,
                         synced_to_agents_md,sync_hash FROM agent_rules";
    if status.is_empty() {
        let sql = format!(
            "{select} ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1 \
             WHEN 'warning' THEN 2 WHEN 'info' THEN 3 ELSE 4 END,updated_at DESC LIMIT ?1"
        );
        let mut stmt = conn
            .prepare(&sql)
            .map_err(|error| format!("cannot prepare rules query: {error}"))?;
        return stmt
            .query_map(params![limit], row_to_rule)
            .map_err(|error| format!("cannot query rules: {error}"))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| format!("cannot read rules: {error}"));
    }
    let sql = format!(
        "{select} WHERE status=?1 ORDER BY CASE severity WHEN 'critical' THEN 0 \
         WHEN 'error' THEN 1 WHEN 'warning' THEN 2 WHEN 'info' THEN 3 ELSE 4 END, \
         updated_at DESC LIMIT ?2"
    );
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|error| format!("cannot prepare rules query: {error}"))?;
    let rows = stmt
        .query_map(params![status, limit], row_to_rule)
        .map_err(|error| format!("cannot query rules: {error}"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("cannot read rules: {error}"))?;
    Ok(rows)
}

pub fn applicable_agent_rules(
    conn: &Connection,
    context: &Value,
    limit: i64,
) -> Result<Vec<AgentRule>, String> {
    require_json_object(context, "context")?;
    if limit <= 0 {
        return Ok(Vec::new());
    }
    let mut rules = list_agent_rules(conn, RULE_STATUS_ACTIVE, 500)?
        .into_iter()
        .filter_map(|mut rule| {
            match_scope(rule.scope.as_object(), context.as_object().unwrap())
                .transpose()
                .map(|result| {
                    result.map(|labels| {
                        rule.matched_scope = labels;
                        rule
                    })
                })
        })
        .collect::<Result<Vec<_>, _>>()?;
    rules.sort_by(|left, right| {
        severity_rank(&right.severity)
            .cmp(&severity_rank(&left.severity))
            .then_with(|| right.matched_scope.len().cmp(&left.matched_scope.len()))
            .then_with(|| {
                right
                    .updated_at
                    .partial_cmp(&left.updated_at)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
    });
    rules.truncate(limit as usize);
    Ok(rules)
}

pub fn sync_agent_rules(
    conn: &mut Connection,
    target: &Path,
    dry_run: bool,
    actor: &str,
) -> Result<RuleSyncResult, String> {
    if actor.trim().is_empty() {
        return Err("sync actor is required".to_string());
    }
    let (root, target_path) = resolve_safe_workspace_target(conn, target)?;
    let content = read_utf8_or_empty(&target_path)?;
    let before_hash = sha256_hex(content.as_bytes());
    let Some(start) = content.find(MARKER_START) else {
        return Ok(missing_marker_result(target, dry_run, &before_hash));
    };
    let Some(end) = content.find(MARKER_END) else {
        return Ok(missing_marker_result(target, dry_run, &before_hash));
    };
    if end < start {
        return Ok(missing_marker_result(target, dry_run, &before_hash));
    }
    let rules = list_agent_rules(conn, RULE_STATUS_ACTIVE, 500)?;
    let rule_ids = rules.iter().map(|rule| rule.id.clone()).collect::<Vec<_>>();
    let mut marker = vec![MARKER_START.to_string(), MARKER_HINT.to_string()];
    marker.extend(rules.iter().map(|rule| {
        format!(
            "- [{}] **{}** (severity: {}): {}",
            rule.id, rule.title, rule.severity, rule.rule_text
        )
    }));
    marker.push(MARKER_END.to_string());
    let marker = marker.join("\n");
    let new_content = format!(
        "{}{}{}",
        &content[..start],
        marker,
        &content[end + MARKER_END.len()..]
    );
    let after_hash = sha256_hex(new_content.as_bytes());
    let result = RuleSyncResult {
        success: true,
        dry_run,
        target_path: target.to_string_lossy().to_string(),
        rule_count: rule_ids.len(),
        rule_ids: rule_ids.clone(),
        before_hash,
        after_hash: after_hash.clone(),
        preview: if dry_run { marker } else { String::new() },
        error: String::new(),
        suggested_block: String::new(),
    };
    if dry_run {
        return Ok(result);
    }
    let old_content = content.clone();
    write_workspace_file(&root, &target_path, new_content.as_bytes())?;
    let tx_result = (|| {
        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| format!("cannot begin rule sync transaction: {error}"))?;
        let log_id = generate_security_id("ARSL")?;
        tx.execute(
            "INSERT INTO agent_rule_sync_log
             (id,target_path,rule_ids_json,before_hash,after_hash,dry_run,created_at,actor)
             VALUES (?1,?2,?3,?4,?5,0,?6,?7)",
            params![
                log_id,
                target.to_string_lossy(),
                compact_json(&json!(rule_ids))?,
                result.before_hash,
                after_hash,
                now_epoch()?,
                actor.trim(),
            ],
        )
        .map_err(|error| format!("cannot insert rule sync audit: {error}"))?;
        if !rule_ids.is_empty() {
            tx.execute(
                "UPDATE agent_rules SET synced_to_agents_md=1,sync_hash=?1
                 WHERE status='active'",
                params![after_hash],
            )
            .map_err(|error| format!("cannot mark synced rules: {error}"))?;
        }
        tx.commit()
            .map_err(|error| format!("cannot commit rule sync: {error}"))
    })();
    if let Err(error) = tx_result {
        let _ = write_workspace_file(&root, &target_path, old_content.as_bytes());
        return Err(error);
    }
    Ok(result)
}

pub fn insert_rule_marker_block(
    conn: &mut Connection,
    target: &Path,
    actor: &str,
) -> Result<bool, String> {
    if actor.trim().is_empty() {
        return Err("sync actor is required".to_string());
    }
    let (root, target_path) = resolve_safe_workspace_target(conn, target)?;
    let content = read_utf8_or_empty(&target_path)?;
    if content.contains(MARKER_START) || content.contains(MARKER_END) {
        return Ok(false);
    }
    let block =
        format!("\n\n## Call Warden 自动沉淀规则\n\n{MARKER_START}\n{MARKER_HINT}\n{MARKER_END}\n");
    let new_content = format!("{content}{block}");
    let before_hash = sha256_hex(content.as_bytes());
    let after_hash = sha256_hex(new_content.as_bytes());
    write_workspace_file(&root, &target_path, new_content.as_bytes())?;
    let tx_result = (|| {
        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| format!("cannot begin marker transaction: {error}"))?;
        tx.execute(
            "INSERT INTO agent_rule_sync_log
             (id,target_path,rule_ids_json,before_hash,after_hash,dry_run,created_at,actor)
             VALUES (?1,?2,'[]',?3,?4,1,?5,?6)",
            params![
                generate_security_id("ARSL")?,
                target.to_string_lossy(),
                before_hash,
                after_hash,
                now_epoch()?,
                actor.trim(),
            ],
        )
        .map_err(|error| format!("cannot audit marker insertion: {error}"))?;
        tx.commit()
            .map_err(|error| format!("cannot commit marker insertion: {error}"))
    })();
    if let Err(error) = tx_result {
        let _ = write_workspace_file(&root, &target_path, content.as_bytes());
        return Err(error);
    }
    Ok(true)
}

pub fn cleanup_rule_sync_log(
    conn: &mut Connection,
    older_than_days: i64,
    keep_latest: i64,
    dry_run: bool,
) -> Result<RuleCleanupResult, String> {
    if older_than_days < 0 || keep_latest < 0 {
        return Err("older-than and keep-latest must be non-negative".to_string());
    }
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot begin sync-log cleanup: {error}"))?;
    let total = tx
        .query_row("SELECT COUNT(*) FROM agent_rule_sync_log", [], |row| {
            row.get(0)
        })
        .map_err(|error| format!("cannot count sync logs: {error}"))?;
    let cutoff = now_epoch()? - older_than_days as f64 * 86_400.0;
    let threshold = if total <= keep_latest || keep_latest == 0 {
        None
    } else {
        tx.query_row(
            "SELECT created_at FROM agent_rule_sync_log
             ORDER BY created_at DESC LIMIT 1 OFFSET ?1",
            params![keep_latest - 1],
            |row| row.get::<_, f64>(0),
        )
        .optional()
        .map_err(|error| format!("cannot query sync-log threshold: {error}"))?
    };
    let candidate_count = if let Some(threshold) = threshold {
        tx.query_row(
            "SELECT COUNT(*) FROM agent_rule_sync_log
             WHERE created_at < ?1 AND created_at < ?2",
            params![cutoff, threshold],
            |row| row.get::<_, i64>(0),
        )
    } else {
        tx.query_row(
            "SELECT COUNT(*) FROM agent_rule_sync_log WHERE created_at < ?1",
            params![cutoff],
            |row| row.get::<_, i64>(0),
        )
    }
    .map_err(|error| format!("cannot count cleanup candidates: {error}"))?;
    let deleted = if dry_run {
        candidate_count
    } else if let Some(threshold) = threshold {
        tx.execute(
            "DELETE FROM agent_rule_sync_log WHERE created_at < ?1 AND created_at < ?2",
            params![cutoff, threshold],
        )
        .map_err(|error| format!("cannot delete sync logs: {error}"))? as i64
    } else {
        tx.execute(
            "DELETE FROM agent_rule_sync_log WHERE created_at < ?1",
            params![cutoff],
        )
        .map_err(|error| format!("cannot delete sync logs: {error}"))? as i64
    };
    let remaining = if dry_run { total } else { total - deleted };
    tx.commit()
        .map_err(|error| format!("cannot commit sync-log cleanup: {error}"))?;
    Ok(RuleCleanupResult {
        success: true,
        dry_run,
        deleted_count: deleted,
        remaining_count: remaining,
        total_before: total,
        older_than_days,
        keep_latest,
    })
}

pub fn extract_rule_candidates(
    conn: &mut Connection,
    task_id: &str,
    min_occurrences: i64,
) -> Result<Vec<String>, String> {
    let threshold = min_occurrences.max(1);
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot begin rule extraction: {error}"))?;
    let groups = if task_id.is_empty() {
        let mut stmt = tx
            .prepare(
                "SELECT finding_type,severity,source,COUNT(*),MIN(message),GROUP_CONCAT(id)
                 FROM task_quality_findings
                 GROUP BY finding_type,severity,source HAVING COUNT(*) >= ?1
                 ORDER BY COUNT(*) DESC",
            )
            .map_err(|error| format!("cannot prepare finding extraction: {error}"))?;
        let rows = stmt
            .query_map(params![threshold], extraction_row)
            .map_err(|error| format!("cannot query finding groups: {error}"))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| format!("cannot read finding groups: {error}"))?;
        rows
    } else {
        let mut stmt = tx
            .prepare(
                "SELECT finding_type,severity,source,COUNT(*),MIN(message),GROUP_CONCAT(id)
                 FROM task_quality_findings WHERE task_id=?1
                 GROUP BY finding_type,severity,source HAVING COUNT(*) >= ?2
                 ORDER BY COUNT(*) DESC",
            )
            .map_err(|error| format!("cannot prepare task finding extraction: {error}"))?;
        let rows = stmt
            .query_map(params![task_id, threshold], extraction_row)
            .map_err(|error| format!("cannot query task finding groups: {error}"))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| format!("cannot read task finding groups: {error}"))?;
        rows
    };
    let mut created = Vec::new();
    for (finding_type, severity, _source, occurrences, sample, finding_ids) in groups {
        let title = format!("自动沉淀: {finding_type} ({severity})");
        let exists = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM agent_rule_candidates
                 WHERE title=?1 AND status='pending')",
                params![title],
                |row| row.get::<_, i64>(0),
            )
            .map_err(|error| format!("cannot deduplicate extracted candidate: {error}"))?;
        if exists != 0 {
            continue;
        }
        let id = generate_security_id("ARC")?;
        let ids = finding_ids
            .split(',')
            .filter_map(|value| value.parse::<i64>().ok())
            .take(10)
            .collect::<Vec<_>>();
        let evidence = json!({
            "source": "task_quality_findings",
            "finding_ids": ids,
            "occurrences": occurrences,
            "task_id": task_id,
            "sample_message": sample,
        });
        let rule_severity = match severity.as_str() {
            "error" | "block" => "error",
            "warn" | "warning" => "warning",
            _ => "info",
        };
        tx.execute(
            "INSERT INTO agent_rule_candidates
             (id,title,rule_text,scope_json,severity,source,evidence_json,confidence,
              status,created_at,reviewed_at,reviewer,linked_rule_id)
             VALUES (?1,?2,?3,?4,?5,'auto_quality_findings',?6,?7,'pending',?8,NULL,'','')",
            params![
                id,
                title,
                format!(
                    "在任务执行中重复出现 {finding_type} 类型问题（{occurrences} 次）。样例: {sample}"
                ),
                compact_json(&json!({"finding_types":[finding_type]}))?,
                rule_severity,
                compact_json(&evidence)?,
                (occurrences as f64 / 10.0).min(1.0),
                now_epoch()?,
            ],
        )
        .map_err(|error| format!("cannot insert extracted candidate: {error}"))?;
        created.push(id);
    }
    tx.commit()
        .map_err(|error| format!("cannot commit rule extraction: {error}"))?;
    Ok(created)
}

pub fn seed_bootstrap_rules(
    conn: &mut Connection,
    dry_run: bool,
) -> Result<RuleSeedResult, String> {
    let seeds = bootstrap_seed_rules();
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot begin bootstrap seed: {error}"))?;
    let mut rules = Vec::new();
    let mut created = 0;
    let mut updated = 0;
    let mut skipped = 0;
    let now = now_epoch()?;
    for seed in &seeds {
        let existing = tx
            .query_row(
                "SELECT rule_text,scope_json,severity,evidence_json FROM agent_rules WHERE id=?1",
                params![seed.id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                    ))
                },
            )
            .optional()
            .map_err(|error| format!("cannot query bootstrap rule {}: {error}", seed.id))?;
        let scope = compact_json(&seed.scope)?;
        let evidence = compact_json(&seed.evidence)?;
        let action = match existing {
            None => {
                created += 1;
                if !dry_run {
                    tx.execute(
                        "INSERT INTO agent_rules
                         (id,title,rule_text,scope_json,severity,status,source_candidate_id,
                          evidence_json,created_at,updated_at,synced_to_agents_md,sync_hash)
                         VALUES (?1,?2,?3,?4,?5,'active','',?6,?7,?7,0,'')",
                        params![
                            seed.id,
                            seed.title,
                            seed.rule_text,
                            scope,
                            seed.severity,
                            evidence,
                            now,
                        ],
                    )
                    .map_err(|error| {
                        format!("cannot create bootstrap rule {}: {error}", seed.id)
                    })?;
                }
                "create"
            }
            Some(existing)
                if existing.0 != seed.rule_text
                    || existing.1 != scope
                    || existing.2 != seed.severity
                    || existing.3 != evidence =>
            {
                updated += 1;
                if !dry_run {
                    tx.execute(
                        "UPDATE agent_rules SET title=?1,rule_text=?2,scope_json=?3,
                         severity=?4,evidence_json=?5,updated_at=?6,status='active' WHERE id=?7",
                        params![
                            seed.title,
                            seed.rule_text,
                            scope,
                            seed.severity,
                            evidence,
                            now,
                            seed.id,
                        ],
                    )
                    .map_err(|error| {
                        format!("cannot update bootstrap rule {}: {error}", seed.id)
                    })?;
                }
                "update"
            }
            Some(_) => {
                skipped += 1;
                "skip"
            }
        };
        rules.push(RuleSeedItem {
            id: seed.id.to_string(),
            title: seed.title.to_string(),
            action: action.to_string(),
        });
    }
    tx.commit()
        .map_err(|error| format!("cannot finish bootstrap seed: {error}"))?;
    Ok(RuleSeedResult {
        dry_run,
        total: seeds.len(),
        created,
        updated,
        skipped,
        rules,
    })
}

struct BootstrapSeed {
    id: &'static str,
    title: &'static str,
    rule_text: &'static str,
    scope: Value,
    severity: &'static str,
    evidence: Value,
}

fn bootstrap_seed_rules() -> Vec<BootstrapSeed> {
    vec![
        BootstrapSeed {
            id: "AR-bootstrap-i18n",
            title: "用户可见输出必须使用 i18n key",
            rule_text: "所有用户可见的 CLI/MCP 输出（提示、错误、状态、摘要）必须通过 i18n.t() 获取，禁止硬编码中文/英文字符串。新增输出时必须同时更新 i18n/zh_CN.json 与 i18n/en_US.json。",
            scope: json!({}),
            severity: "warning",
            evidence: json!({"source":"bootstrap-seed","plan":"bootstrap-closure-plan.md"}),
        },
        BootstrapSeed {
            id: "AR-bootstrap-refresh-before-commit",
            title: "提交前必须刷新代码图谱",
            rule_text: "每次 git commit 之前必须运行 cw --refresh-all 或批量刷新所有修改文件，确保数据库中的符号/调用关系与代码同步。禁止提交后数据库滞后。",
            scope: json!({"actions":["commit"]}),
            severity: "critical",
            evidence: json!({"source":"bootstrap-seed","plan":"bootstrap-closure-plan.md"}),
        },
        BootstrapSeed {
            id: "AR-bootstrap-task-split",
            title: "大任务必须通过 Call Warden task 拆分并推进",
            rule_text: "当任务涉及 3 个以上文件或 5 个以上步骤时，必须使用 task_split 拆分为父子任务树，通过 task_next_step 逐步执行，避免遗漏和遗忘。",
            scope: json!({"actions":["task_create","task_next_step"]}),
            severity: "warning",
            evidence: json!({"source":"bootstrap-seed","plan":"bootstrap-closure-plan.md"}),
        },
        BootstrapSeed {
            id: "AR-bootstrap-completion-review",
            title: "任务完成后必须运行 completion review",
            rule_text: "任务完成后必须运行 run_task_completion_review，blocking finding 未解决不得 apply/close。blocking findings 必须先修复或显式 wontfix 后才能推进状态。",
            scope: json!({"actions":["task_apply","task_close"]}),
            severity: "critical",
            evidence: json!({"source":"bootstrap-seed","plan":"bootstrap-closure-plan.md"}),
        },
        BootstrapSeed {
            id: "AR-bootstrap-capture-diff",
            title: "外部编辑完成后必须运行 task capture-diff",
            rule_text: "外部 Agent 完成文件编辑后，必须运行 cw task capture-diff 把真实改动归因到 task/change/symbol/audit 闭环，触发质量审查与签名链。",
            scope: json!({"actions":["task_capture_diff"]}),
            severity: "warning",
            evidence: json!({"source":"bootstrap-seed","plan":"bootstrap-closure-plan.md"}),
        },
    ]
}

fn extraction_row(
    row: &rusqlite::Row<'_>,
) -> rusqlite::Result<(String, String, String, i64, String, String)> {
    Ok((
        row.get::<_, Option<String>>(0)?
            .unwrap_or_else(|| "unknown".to_string()),
        row.get::<_, Option<String>>(1)?
            .unwrap_or_else(|| "info".to_string()),
        row.get::<_, Option<String>>(2)?
            .unwrap_or_else(|| "task_quality".to_string()),
        row.get(3)?,
        row.get::<_, Option<String>>(4)?.unwrap_or_default(),
        row.get::<_, Option<String>>(5)?.unwrap_or_default(),
    ))
}

fn row_to_candidate(row: &rusqlite::Row<'_>) -> rusqlite::Result<RuleCandidate> {
    Ok(RuleCandidate {
        id: row.get(0)?,
        title: row.get(1)?,
        rule_text: row.get(2)?,
        scope: parse_json_value(&row.get::<_, String>(3)?, json!({})),
        severity: row.get(4)?,
        source: row.get(5)?,
        evidence: parse_json_value(&row.get::<_, String>(6)?, json!({})),
        confidence: row.get(7)?,
        status: row.get(8)?,
        created_at: row.get(9)?,
        reviewed_at: row.get(10)?,
        reviewer: row.get(11)?,
        linked_rule_id: row.get(12)?,
    })
}

fn row_to_rule(row: &rusqlite::Row<'_>) -> rusqlite::Result<AgentRule> {
    Ok(AgentRule {
        id: row.get(0)?,
        title: row.get(1)?,
        rule_text: row.get(2)?,
        scope: parse_json_value(&row.get::<_, String>(3)?, json!({})),
        severity: row.get(4)?,
        status: row.get(5)?,
        source_candidate_id: row.get(6)?,
        evidence: parse_json_value(&row.get::<_, String>(7)?, json!({})),
        created_at: row.get(8)?,
        updated_at: row.get(9)?,
        synced_to_agents_md: row.get::<_, i64>(10)? != 0,
        sync_hash: row.get(11)?,
        matched_scope: Vec::new(),
    })
}

fn match_scope(
    scope: Option<&Map<String, Value>>,
    context: &Map<String, Value>,
) -> Result<Option<Vec<String>>, String> {
    let Some(scope) = scope else {
        return Ok(Some(vec!["global".to_string()]));
    };
    if scope.is_empty() {
        return Ok(Some(vec!["global".to_string()]));
    }
    let mut labels = Vec::new();
    for (scope_key, context_key, label) in [
        ("languages", "language", "language"),
        ("symbol_kinds", "symbol_kind", "symbol_kind"),
        ("actions", "action", "action"),
        ("finding_types", "finding_type", "finding_type"),
    ] {
        let values = string_array(scope.get(scope_key), scope_key)?;
        if !values.is_empty() {
            let actual = context
                .get(context_key)
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_lowercase();
            if actual.is_empty() || !values.iter().any(|item| item.to_lowercase() == actual) {
                return Ok(None);
            }
            labels.push(format!("{label}:{actual}"));
        }
    }
    let patterns = string_array(scope.get("file_patterns"), "file_patterns")?;
    if !patterns.is_empty() {
        let actual = context
            .get("file_path")
            .and_then(Value::as_str)
            .unwrap_or("");
        if actual.is_empty() || !patterns.iter().any(|pattern| glob_match(pattern, actual)) {
            return Ok(None);
        }
        labels.push(format!("file:{actual}"));
    }
    let prefixes = string_array(scope.get("module_prefixes"), "module_prefixes")?;
    if !prefixes.is_empty() {
        let actual = context
            .get("module_prefix")
            .and_then(Value::as_str)
            .unwrap_or("");
        if actual.is_empty() || !prefixes.iter().any(|prefix| actual.starts_with(prefix)) {
            return Ok(None);
        }
        labels.push(format!("module:{actual}"));
    }
    Ok(Some(labels))
}

fn glob_match(pattern: &str, value: &str) -> bool {
    let mut regex = String::from("^");
    let mut chars = pattern.chars().peekable();
    while let Some(ch) = chars.next() {
        match ch {
            '*' => regex.push_str(".*"),
            '?' => regex.push('.'),
            _ => regex.push_str(&regex::escape(&ch.to_string())),
        }
    }
    regex.push('$');
    Regex::new(&regex)
        .map(|compiled| compiled.is_match(value))
        .unwrap_or(false)
}

fn resolve_safe_workspace_target(
    conn: &Connection,
    target: &Path,
) -> Result<(PathBuf, PathBuf), String> {
    if target.as_os_str().is_empty() {
        return Err("target path is required".to_string());
    }
    if target
        .components()
        .any(|component| matches!(component, Component::ParentDir))
    {
        return Err("target path must not contain '..'".to_string());
    }
    let roots = {
        let mut stmt = conn
            .prepare("SELECT root_path FROM workspaces WHERE is_active=1 ORDER BY id LIMIT 2")
            .map_err(|error| format!("cannot query active workspace root: {error}"))?;
        let roots = stmt
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(|error| format!("cannot query active workspace root: {error}"))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| format!("cannot read active workspace root: {error}"))?;
        roots
    };
    let [root] = roots.as_slice() else {
        return Err("exactly one active workspace is required".to_string());
    };
    let root = fs::canonicalize(root)
        .map_err(|error| format!("cannot canonicalize workspace root {root}: {error}"))?;
    let candidate = if target.is_absolute() {
        target.to_path_buf()
    } else {
        root.join(target)
    };
    let parent = candidate
        .parent()
        .ok_or_else(|| "target path has no parent".to_string())?;
    let parent = fs::canonicalize(parent).map_err(|error| {
        format!(
            "cannot canonicalize target parent {}: {error}",
            parent.display()
        )
    })?;
    if !parent.starts_with(&root) {
        return Err("target path escapes the active workspace".to_string());
    }
    let file_name = candidate
        .file_name()
        .ok_or_else(|| "target path has no file name".to_string())?;
    let resolved = parent.join(file_name);
    if resolved.exists() {
        let canonical = fs::canonicalize(&resolved).map_err(|error| {
            format!("cannot canonicalize target {}: {error}", resolved.display())
        })?;
        if !canonical.starts_with(&root) {
            return Err("target symlink escapes the active workspace".to_string());
        }
        return Ok((root, canonical));
    }
    Ok((root, resolved))
}

fn write_workspace_file(root: &Path, target: &Path, bytes: &[u8]) -> Result<(), String> {
    if !target.starts_with(root) {
        return Err("target path escapes the active workspace".to_string());
    }
    fs::write(target, bytes)
        .map_err(|error| format!("cannot write rule target {}: {error}", target.display()))
}

fn read_utf8_or_empty(path: &Path) -> Result<String, String> {
    if !path.exists() {
        return Ok(String::new());
    }
    fs::read_to_string(path)
        .map_err(|error| format!("cannot read UTF-8 rule target {}: {error}", path.display()))
}

#[derive(Debug, Clone)]
struct PendingFinding {
    rule_id: String,
    file_path: String,
    symbol_hash: String,
    severity: String,
    message: String,
    finding_type: String,
    check: String,
    line: Option<u32>,
    raw_severity: String,
}

impl PendingFinding {
    fn guardrail(rule_id: &str, file_path: &str, severity: &str, message: String) -> Self {
        Self {
            rule_id: rule_id.to_string(),
            file_path: file_path.to_string(),
            symbol_hash: String::new(),
            severity: severity.to_string(),
            message,
            finding_type: String::new(),
            check: String::new(),
            line: None,
            raw_severity: String::new(),
        }
    }

    fn gate(
        check: &str,
        file_path: &str,
        severity: &str,
        rule_id: &str,
        message: String,
        line: Option<u32>,
        raw_severity: &str,
    ) -> Self {
        Self {
            rule_id: rule_id.to_string(),
            file_path: file_path.to_string(),
            symbol_hash: String::new(),
            severity: severity.to_string(),
            message,
            finding_type: check.to_string(),
            check: check.to_string(),
            line,
            raw_severity: raw_severity.to_string(),
        }
    }
}

fn ensure_builtin_guardrail_rules(conn: &mut Connection) -> Result<(), String> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot begin builtin guardrail transaction: {error}"))?;
    insert_builtin_guardrail_rules(&tx)?;
    tx.commit()
        .map_err(|error| format!("cannot commit builtin guardrail rules: {error}"))
}

fn insert_builtin_guardrail_rules(tx: &rusqlite::Transaction<'_>) -> Result<(), String> {
    let now = now_epoch()?;
    for (rule_id, category, severity, pattern, action, description) in BUILTIN_GUARDRAIL_RULES {
        tx.execute(
            "INSERT OR IGNORE INTO guardrail_rules
             (rule_id,category,severity,pattern,action,description,is_builtin,created_at)
             VALUES (?1,?2,?3,?4,?5,?6,1,?7)",
            params![
                rule_id,
                category,
                severity,
                pattern,
                action,
                description,
                now
            ],
        )
        .map_err(|error| format!("cannot initialize guardrail rule {rule_id}: {error}"))?;
    }
    Ok(())
}

fn validate_guardrail_category(category: &str) -> Result<(), String> {
    if category.trim().is_empty()
        || matches!(category.trim(), "db_safety" | "api_compat" | "incident")
    {
        Ok(())
    } else {
        Err(format!(
            "invalid guardrail category {:?}; expected db_safety/api_compat/incident",
            category
        ))
    }
}

fn active_workspace(conn: &Connection) -> Result<(i64, PathBuf), String> {
    let mut statement = conn
        .prepare("SELECT id,root_path FROM workspaces WHERE is_active=1 ORDER BY id")
        .map_err(|error| format!("cannot query active workspace: {error}"))?;
    let rows = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                PathBuf::from(row.get::<_, String>(1)?),
            ))
        })
        .map_err(|error| format!("cannot query active workspace: {error}"))?
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(|error| format!("cannot decode active workspace: {error}"))?;
    match rows.as_slice() {
        [workspace] => Ok(workspace.clone()),
        [] => Err("no active workspace; security command fails closed".to_string()),
        _ => Err("multiple active workspaces; security command fails closed".to_string()),
    }
}

fn normalize_safe_prefix(value: &str) -> Result<String, String> {
    let normalized = value.trim().replace('\\', "/");
    if normalized.starts_with('/')
        || normalized.split('/').any(|part| part == "..")
        || Path::new(&normalized).is_absolute()
    {
        return Err(format!("unsafe workspace-relative path: {value}"));
    }
    Ok(normalized)
}

fn validate_indexed_file(root: &Path, path: &Path, rel_path: &str) -> Result<PathBuf, String> {
    let canonical = fs::canonicalize(path)
        .map_err(|error| format!("indexed file evidence is missing {rel_path}: {error}"))?;
    if !canonical.is_file() {
        return Err(format!(
            "indexed evidence is not a file: {}",
            canonical.display()
        ));
    }
    if !canonical.starts_with(root) {
        return Err(format!(
            "indexed file escapes active workspace: {}",
            canonical.display()
        ));
    }
    Ok(canonical)
}

fn require_task(conn: &Connection, task_id: &str) -> Result<(), String> {
    if task_id.trim().is_empty() {
        return Err("task_id is required".to_string());
    }
    let exists = conn
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM tasks WHERE id=?1)",
            params![task_id],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|error| format!("cannot verify task {task_id}: {error}"))?;
    if exists == 0 {
        Err(format!("task not found: {task_id}"))
    } else {
        Ok(())
    }
}

fn task_changed_files(conn: &Connection, task_id: &str) -> Result<Vec<String>, String> {
    let mut statement = conn
        .prepare(
            "SELECT DISTINCT file_path FROM change_audit
             WHERE task_id=?1 AND file_path!='' ORDER BY file_path",
        )
        .map_err(|error| format!("cannot prepare task change query: {error}"))?;
    let rows = statement
        .query_map(params![task_id], |row| row.get::<_, String>(0))
        .map_err(|error| format!("cannot query task changes: {error}"))?
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(|error| format!("cannot decode task change evidence: {error}"))?;
    Ok(rows)
}

fn resolve_workspace_evidence_path(
    conn: &Connection,
    workspace_id: i64,
    root: &Path,
    file_path: &str,
) -> Result<PathBuf, String> {
    let normalized = normalize_safe_prefix(file_path)?;
    if normalized.is_empty() {
        return Err("empty file path in change_audit evidence".to_string());
    }
    let indexed = conn
        .query_row(
            "SELECT abs_path FROM file_instances
             WHERE workspace_id=?1 AND rel_path=?2 AND status!='archived'",
            params![workspace_id, normalized],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(|error| format!("cannot resolve change evidence {file_path}: {error}"))?;
    let candidate = indexed
        .map(PathBuf::from)
        .unwrap_or_else(|| root.join(&normalized));
    validate_indexed_file(root, &candidate, &normalized)
}

fn detect_guardrail_findings(
    content: &str,
    file_path: &str,
    category: &str,
) -> Result<Vec<PendingFinding>, String> {
    let mut findings = Vec::new();
    if category.is_empty() || category == "db_safety" {
        let alter = case_insensitive_regex(r"\bALTER\s+TABLE\b")?;
        for found in alter.find_iter(content) {
            findings.push(PendingFinding::guardrail(
                "GR-builtin-db-1",
                file_path,
                "warn",
                format!(
                    "Detected ALTER TABLE statement (line {})",
                    line_at(content, found.start())
                ),
            ));
        }
        let drop_re = case_insensitive_regex(r"\bDROP\s+(TABLE|COLUMN)\b")?;
        for found in drop_re.find_iter(content) {
            findings.push(PendingFinding::guardrail(
                "GR-builtin-db-2",
                file_path,
                "block",
                format!(
                    "Detected {} statement (line {})",
                    found.as_str().to_uppercase(),
                    line_at(content, found.start())
                ),
            ));
        }
        let varchar = case_insensitive_regex(
            r"VARCHAR\s*\(\s*(\d+)\s*\)\s*(?:→|->)\s*VARCHAR\s*\(\s*(\d+)\s*\)",
        )?;
        for captures in varchar.captures_iter(content) {
            let old_len = captures[1].parse::<u64>().unwrap_or(0);
            let new_len = captures[2].parse::<u64>().unwrap_or(0);
            if new_len < old_len {
                let start = captures.get(0).map(|value| value.start()).unwrap_or(0);
                findings.push(PendingFinding::guardrail(
                    "GR-builtin-db-3",
                    file_path,
                    "block",
                    format!(
                        "VARCHAR length shrank: {old_len} -> {new_len} (line {})",
                        line_at(content, start)
                    ),
                ));
            }
        }
        if file_path.to_lowercase().ends_with(".sql")
            && !file_path.replace('\\', "/").contains("migrations/")
        {
            findings.push(PendingFinding::guardrail(
                "GR-builtin-db-1",
                file_path,
                "warn",
                "SQL file is not under migrations/ (migration script missing risk)".to_string(),
            ));
        }
    }
    if category.is_empty() || category == "api_compat" {
        for (pattern, rule_id, description) in [
            (
                r"#\s*BREAKING\s+CHANGE",
                "GR-builtin-api-1",
                "Detected BREAKING CHANGE marker",
            ),
            (
                r"//\s*REMOVED\s+PARAM",
                "GR-builtin-api-2",
                "Detected parameter removal marker // REMOVED PARAM",
            ),
            (
                r"//\s*REMOVED\s+FIELD",
                "GR-builtin-api-3",
                "Detected field removal marker // REMOVED FIELD",
            ),
        ] {
            let matcher = case_insensitive_regex(pattern)?;
            for found in matcher.find_iter(content) {
                findings.push(PendingFinding::guardrail(
                    rule_id,
                    file_path,
                    "block",
                    format!("{description} (line {})", line_at(content, found.start())),
                ));
            }
        }
    }
    if category.is_empty() || category == "incident" {
        let blocks = extract_function_blocks(content)?;
        let blocks = if blocks.is_empty() {
            vec![(
                "<file>".to_string(),
                content.to_string(),
                1_u32,
                content.lines().count().max(1) as u32,
            )]
        } else {
            blocks
        };
        let error_handling = Regex::new(r"\b(?:try|catch|unwrap|expect)\b|\?|Result")
            .map_err(|error| format!("invalid incident regex: {error}"))?;
        let logging =
            Regex::new(r"\blog::|tracing::|println!|print!|eprintln!|warn!|info!|error!|debug!")
                .map_err(|error| format!("invalid logging regex: {error}"))?;
        let write_word = case_insensitive_regex(r"\b(?:INSERT|UPDATE|DELETE|CREATE|DROP)\b")?;
        let write_call = Regex::new(r"\.(?:write|save|push|insert|update|delete)\s*\(")
            .map_err(|error| format!("invalid write regex: {error}"))?;
        let safety =
            case_insensitive_regex(r"\b(?:rollback|transaction|begin|undo|abort|commit)\b")?;
        for (name, body, start, end) in blocks {
            if body.trim().len() < 10 {
                continue;
            }
            let location = format!("Function {name} (lines {start}-{end})");
            if !error_handling.is_match(&body) {
                findings.push(PendingFinding::guardrail(
                    "GR-builtin-inc-1",
                    file_path,
                    "warn",
                    format!(
                        "{location} is missing error handling (no try/catch/unwrap/expect/?/Result)"
                    ),
                ));
            }
            if !logging.is_match(&body) {
                findings.push(PendingFinding::guardrail(
                    "GR-builtin-inc-2",
                    file_path,
                    "info",
                    format!("{location} is missing logging (no log::/tracing::/println!/print!)"),
                ));
            }
            if (write_word.is_match(&body) || write_call.is_match(&body)) && !safety.is_match(&body)
            {
                findings.push(PendingFinding::guardrail(
                    "GR-builtin-inc-3",
                    file_path,
                    "warn",
                    format!("{location} has write operations without transaction/rollback logic"),
                ));
            }
        }
    }
    Ok(findings)
}

fn extract_function_blocks(content: &str) -> Result<Vec<(String, String, u32, u32)>, String> {
    let matcher =
        Regex::new(r"\b(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?(?:fn|func|function)\s+(\w+)")
            .map_err(|error| format!("invalid function regex: {error}"))?;
    let mut blocks = Vec::new();
    for captures in matcher.captures_iter(content) {
        let (Some(full), Some(name)) = (captures.get(0), captures.get(1)) else {
            continue;
        };
        let Some(relative_brace) = content[full.end()..].find('{') else {
            continue;
        };
        let brace_start = full.end() + relative_brace;
        let mut depth = 1_i64;
        for (offset, byte) in content.as_bytes()[brace_start + 1..].iter().enumerate() {
            match byte {
                b'{' => depth += 1,
                b'}' => depth -= 1,
                _ => {}
            }
            if depth == 0 {
                let end = brace_start + 2 + offset;
                blocks.push((
                    name.as_str().to_string(),
                    content[brace_start + 1..end - 1].to_string(),
                    line_at(content, full.start()),
                    line_at(content, end),
                ));
                break;
            }
        }
    }
    Ok(blocks)
}

fn case_insensitive_regex(pattern: &str) -> Result<Regex, String> {
    RegexBuilder::new(pattern)
        .case_insensitive(true)
        .build()
        .map_err(|error| format!("invalid guardrail regex {pattern:?}: {error}"))
}

fn line_at(content: &str, byte_offset: usize) -> u32 {
    content.as_bytes()[..byte_offset.min(content.len())]
        .iter()
        .filter(|byte| **byte == b'\n')
        .count() as u32
        + 1
}

fn insert_guardrail_finding(
    tx: &rusqlite::Transaction<'_>,
    workspace_id: i64,
    finding: &PendingFinding,
    now: f64,
    deduplicate: bool,
) -> Result<Option<GuardrailFinding>, String> {
    if deduplicate {
        let exists = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM guardrail_findings
                 WHERE workspace_id=?1 AND rule_id=?2 AND file_path=?3 AND message=?4 AND status='open')",
                params![workspace_id, finding.rule_id, finding.file_path, finding.message],
                |row| row.get::<_, i64>(0),
            )
            .map_err(|error| format!("cannot deduplicate guardrail finding: {error}"))?;
        if exists != 0 {
            return Ok(None);
        }
    }
    tx.execute(
        "INSERT INTO guardrail_findings
         (workspace_id,rule_id,file_path,symbol_hash,severity,status,message,detected_at)
         VALUES (?1,?2,?3,?4,?5,'open',?6,?7)",
        params![
            workspace_id,
            finding.rule_id,
            finding.file_path,
            finding.symbol_hash,
            finding.severity,
            finding.message,
            now,
        ],
    )
    .map_err(|error| format!("cannot insert guardrail finding: {error}"))?;
    Ok(Some(GuardrailFinding {
        id: tx.last_insert_rowid(),
        rule_id: finding.rule_id.clone(),
        file_path: finding.file_path.clone(),
        symbol_hash: finding.symbol_hash.clone(),
        severity: finding.severity.clone(),
        status: "open".to_string(),
        message: finding.message.clone(),
        detected_at: now,
        finding_type: finding.finding_type.clone(),
        check: finding.check.clone(),
        line: finding.line,
        raw_severity: finding.raw_severity.clone(),
    }))
}

fn ensure_gate_rule(
    tx: &rusqlite::Transaction<'_>,
    finding: &PendingFinding,
    now: f64,
) -> Result<(), String> {
    tx.execute(
        "INSERT OR IGNORE INTO guardrail_rules
         (rule_id,category,severity,pattern,action,description,is_builtin,created_at)
         VALUES (?1,'check_gate',?2,'*','require_review',?3,1,?4)",
        params![
            finding.rule_id,
            finding.severity,
            format!("check gate: {} - {}", finding.check, finding.message),
            now,
        ],
    )
    .map_err(|error| format!("cannot ensure check-gate rule {}: {error}", finding.rule_id))?;
    Ok(())
}

fn run_semgrep_batch(
    workspace_root: &Path,
    files: &[(String, PathBuf)],
    timeout_secs: u64,
) -> Result<Vec<PendingFinding>, String> {
    if files.is_empty() {
        return Err("Semgrep has no file evidence to scan".to_string());
    }
    let executable =
        std::env::var("CALLWARDEN_SEMGREP_BIN").unwrap_or_else(|_| "semgrep".to_string());
    let token = generate_security_id("semgrep")?;
    let stdout_path = std::env::temp_dir().join(format!("{token}.json"));
    let stderr_path = std::env::temp_dir().join(format!("{token}.stderr"));
    let stdout = File::create(&stdout_path)
        .map_err(|error| format!("cannot create Semgrep output file: {error}"))?;
    let stderr = File::create(&stderr_path)
        .map_err(|error| format!("cannot create Semgrep error file: {error}"))?;
    let mut command = Command::new(&executable);
    command
        .current_dir(workspace_root)
        .args(["--config", "p/default", "--json", "--quiet"])
        .args(files.iter().map(|(_, path)| path))
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));
    let mut child = command
        .spawn()
        .map_err(|error| format!("cannot start Semgrep executable {executable:?}: {error}"))?;
    let deadline = std::time::Instant::now() + Duration::from_secs(timeout_secs.max(1));
    let status = loop {
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("cannot poll Semgrep: {error}"))?
        {
            break status;
        }
        if std::time::Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            let stderr = fs::read_to_string(&stderr_path).unwrap_or_default();
            let _ = fs::remove_file(&stdout_path);
            let _ = fs::remove_file(&stderr_path);
            return Err(format!(
                "Semgrep timed out after {}s{}",
                timeout_secs.max(1),
                if stderr.trim().is_empty() {
                    String::new()
                } else {
                    format!(": {}", stderr.trim())
                }
            ));
        }
        thread::sleep(Duration::from_millis(50));
    };
    let stdout = fs::read_to_string(&stdout_path)
        .map_err(|error| format!("cannot read Semgrep JSON: {error}"))?;
    let stderr = fs::read_to_string(&stderr_path).unwrap_or_default();
    let _ = fs::remove_file(&stdout_path);
    let _ = fs::remove_file(&stderr_path);
    if !status.success() {
        return Err(format!(
            "Semgrep exited with {}{}",
            status
                .code()
                .map_or_else(|| "signal".to_string(), |code| code.to_string()),
            if stderr.trim().is_empty() {
                String::new()
            } else {
                format!(": {}", stderr.trim())
            }
        ));
    }
    let payload: Value =
        serde_json::from_str(&stdout).map_err(|error| format!("invalid Semgrep JSON: {error}"))?;
    parse_semgrep_payload(workspace_root, files, &payload)
}

fn parse_semgrep_payload(
    workspace_root: &Path,
    files: &[(String, PathBuf)],
    payload: &Value,
) -> Result<Vec<PendingFinding>, String> {
    if payload
        .get("errors")
        .and_then(Value::as_array)
        .is_some_and(|errors| !errors.is_empty())
    {
        return Err(format!("Semgrep reported errors: {}", payload["errors"]));
    }
    let results = payload
        .get("results")
        .and_then(Value::as_array)
        .ok_or_else(|| "Semgrep JSON has no results array".to_string())?;
    let mut findings = Vec::new();
    for result in results {
        let raw_path = result.get("path").and_then(Value::as_str).unwrap_or("");
        let rel_path = map_semgrep_path(workspace_root, files, raw_path)?;
        let rule_id = result
            .get("check_id")
            .and_then(Value::as_str)
            .unwrap_or("gate_semgrep_warning");
        let extra = result.get("extra").and_then(Value::as_object);
        let raw_severity = extra
            .and_then(|value| value.get("severity"))
            .and_then(Value::as_str)
            .unwrap_or("WARNING")
            .to_uppercase();
        let severity = match raw_severity.as_str() {
            "ERROR" | "BLOCK" => "error",
            "INFO" => "info",
            _ => "warn",
        };
        let message = extra
            .and_then(|value| value.get("message"))
            .and_then(Value::as_str)
            .unwrap_or("Semgrep finding")
            .to_string();
        let line = result
            .pointer("/start/line")
            .and_then(Value::as_u64)
            .and_then(|line| u32::try_from(line).ok());
        findings.push(PendingFinding::gate(
            "semgrep",
            &rel_path,
            severity,
            rule_id,
            message,
            line,
            &raw_severity,
        ));
    }
    Ok(findings)
}

fn map_semgrep_path(
    workspace_root: &Path,
    files: &[(String, PathBuf)],
    raw_path: &str,
) -> Result<String, String> {
    if raw_path.trim().is_empty() {
        return Err("Semgrep finding has no path".to_string());
    }
    let reported = PathBuf::from(raw_path);
    let candidate = if reported.is_absolute() {
        reported
    } else {
        workspace_root.join(reported)
    };
    let normalized = fs::canonicalize(&candidate)
        .map_err(|error| format!("cannot resolve Semgrep finding path {raw_path}: {error}"))?;
    for (rel_path, abs_path) in files {
        if fs::canonicalize(abs_path).ok().as_deref() == Some(normalized.as_path()) {
            return Ok(rel_path.clone());
        }
    }
    Err(format!(
        "Semgrep returned finding outside checked evidence: {raw_path}"
    ))
}

fn query_active_rotation_keys(conn: &Connection) -> Result<Vec<(String, Vec<u8>)>, String> {
    let mut statement = conn
        .prepare(
            "SELECT key_id,key_secret FROM audit_key_rotations
             WHERE is_active=1 ORDER BY rotated_at DESC,id DESC",
        )
        .map_err(|error| format!("cannot prepare active audit key query: {error}"))?;
    let rows = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?.into_bytes(),
            ))
        })
        .map_err(|error| format!("cannot query active audit keys: {error}"))?
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(|error| format!("cannot decode active audit key: {error}"))?;
    Ok(rows)
}

fn active_audit_key(conn: &Connection) -> Result<(String, Option<Vec<u8>>, String), String> {
    let active = query_active_rotation_keys(conn)?;
    if active.len() > 1 {
        return Err("multiple active audit keys; audit operation fails closed".to_string());
    }
    if let Some((key_id, key)) = active.into_iter().next() {
        return Ok((key_id, Some(key), "hmac".to_string()));
    }
    if let Some(key) = legacy_audit_key()? {
        return Ok(("hmac".to_string(), Some(key), "hmac".to_string()));
    }
    Ok(("local".to_string(), None, "hash_only".to_string()))
}

fn lookup_audit_key(conn: &Connection, key_id: &str) -> Result<Option<Vec<u8>>, String> {
    if key_id == "local" {
        return Ok(None);
    }
    let stored = conn
        .query_row(
            "SELECT key_secret FROM audit_key_rotations WHERE key_id=?1 LIMIT 1",
            params![key_id],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(|error| format!("cannot look up audit key {key_id}: {error}"))?;
    if let Some(secret) = stored {
        return Ok(Some(secret.into_bytes()));
    }
    if key_id == "hmac" {
        return legacy_audit_key();
    }
    Ok(None)
}

fn legacy_audit_key() -> Result<Option<Vec<u8>>, String> {
    if let Ok(value) = std::env::var("CALLWARDEN_AUDIT_HMAC_KEY") {
        if !value.is_empty() {
            return Ok(Some(value.into_bytes()));
        }
    }
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from);
    let Some(home) = home else {
        return Ok(None);
    };
    let path = home.join(".callwarden").join("audit.key");
    if !path.exists() {
        return Ok(None);
    }
    let bytes = fs::read(&path)
        .map_err(|error| format!("cannot read audit key {}: {error}", path.display()))?;
    let start = bytes.iter().position(|byte| !byte.is_ascii_whitespace());
    let end = bytes.iter().rposition(|byte| !byte.is_ascii_whitespace());
    match (start, end) {
        (Some(start), Some(end)) => Ok(Some(bytes[start..=end].to_vec())),
        _ => Ok(None),
    }
}

fn current_git_head(workspace_root: &Path) -> Result<String, String> {
    if !workspace_root.join(".git").exists() {
        return Ok(String::new());
    }
    let output = Command::new("git")
        .args(["-C", &workspace_root.to_string_lossy(), "rev-parse", "HEAD"])
        .output()
        .map_err(|error| format!("cannot run git rev-parse for bootstrap status: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "cannot resolve git HEAD for bootstrap status: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(String::from_utf8(output.stdout)
        .map_err(|error| format!("git HEAD is not UTF-8: {error}"))?
        .trim()
        .to_string())
}

fn count_where(conn: &Connection, table: &str, predicate: &str) -> Result<i64, String> {
    let sql = format!("SELECT COUNT(*) FROM {table} WHERE {predicate}");
    conn.query_row(&sql, [], |row| row.get::<_, i64>(0))
        .map_err(|error| format!("cannot query bootstrap count from {table}: {error}"))
}

fn missing_marker_result(target: &Path, dry_run: bool, before_hash: &str) -> RuleSyncResult {
    let suggested_block =
        format!("\n\n## Call Warden 自动沉淀规则\n\n{MARKER_START}\n{MARKER_HINT}\n{MARKER_END}\n");
    RuleSyncResult {
        success: false,
        dry_run,
        target_path: target.to_string_lossy().to_string(),
        rule_count: 0,
        rule_ids: Vec::new(),
        before_hash: before_hash.to_string(),
        after_hash: String::new(),
        preview: String::new(),
        error: format!(
            "Marker block not found in {}. Insert the block first via --insert-block or manually.",
            target.display()
        ),
        suggested_block,
    }
}

fn string_array(value: Option<&Value>, name: &str) -> Result<Vec<String>, String> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let array = value
        .as_array()
        .ok_or_else(|| format!("scope.{name} must be an array"))?;
    array
        .iter()
        .map(|item| {
            item.as_str()
                .map(ToString::to_string)
                .ok_or_else(|| format!("scope.{name} values must be strings"))
        })
        .collect()
}

fn compact_json(value: &Value) -> Result<String, String> {
    match value {
        Value::Array(items) => {
            let rendered = items
                .iter()
                .map(compact_json)
                .collect::<Result<Vec<_>, _>>()?;
            Ok(format!("[{}]", rendered.join(", ")))
        }
        Value::Object(entries) => {
            let rendered = entries
                .iter()
                .map(|(key, value)| {
                    let key = serde_json::to_string(key)
                        .map_err(|error| format!("cannot serialize JSON key: {error}"))?;
                    Ok(format!("{key}: {}", compact_json(value)?))
                })
                .collect::<Result<Vec<_>, String>>()?;
            Ok(format!("{{{}}}", rendered.join(", ")))
        }
        _ => {
            serde_json::to_string(value).map_err(|error| format!("cannot serialize JSON: {error}"))
        }
    }
}

fn parse_json_value(raw: &str, fallback: Value) -> Value {
    serde_json::from_str(raw).unwrap_or(fallback)
}

fn parse_json_object(raw: &str) -> Map<String, Value> {
    serde_json::from_str::<Value>(raw)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .unwrap_or_default()
}

fn require_json_object(value: &Value, name: &str) -> Result<(), String> {
    if value.is_object() {
        Ok(())
    } else {
        Err(format!("{name} must be a JSON object"))
    }
}

fn normalize_rule_severity(value: &str) -> String {
    match value.to_lowercase().as_str() {
        "critical" | "error" | "warning" | "info" => value.to_lowercase(),
        _ => "info".to_string(),
    }
}

fn severity_rank(value: &str) -> u8 {
    match value {
        "critical" => 4,
        "error" => 3,
        "warning" => 2,
        "info" => 1,
        _ => 0,
    }
}

fn nonempty_or<'a>(value: &'a str, fallback: &'a str) -> &'a str {
    if value.trim().is_empty() {
        fallback
    } else {
        value
    }
}

fn generate_security_id(prefix: &str) -> Result<String, String> {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| format!("system clock is before UNIX_EPOCH: {error}"))?
        .as_millis();
    let counter = SECURITY_ID_COUNTER.fetch_add(1, Ordering::Relaxed);
    let entropy = format!("{prefix}:{millis}:{}:{counter}", std::process::id());
    let digest = sha256_hex(entropy.as_bytes());
    Ok(format!("{prefix}-{millis}-{}", &digest[..8]))
}

fn now_epoch() -> Result<f64, String> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .map_err(|error| format!("system clock is before UNIX_EPOCH: {error}"))
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn fixture() -> (tempfile::TempDir, Connection) {
        let dir = tempdir().unwrap();
        fs::write(
            dir.path().join("AGENTS.md"),
            format!("manual\n{MARKER_START}\n{MARKER_HINT}\n{MARKER_END}\ntail\n"),
        )
        .unwrap();
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE workspaces(id INTEGER PRIMARY KEY,root_path TEXT,is_active INTEGER);
             CREATE TABLE file_instances(
               id INTEGER PRIMARY KEY,workspace_id INTEGER,rel_path TEXT,abs_path TEXT,status TEXT);
             CREATE TABLE guardrail_rules(
               rule_id TEXT PRIMARY KEY,category TEXT,severity TEXT,pattern TEXT,action TEXT,
               description TEXT,is_builtin INTEGER,created_at REAL);
             CREATE TABLE guardrail_findings(
               id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,rule_id TEXT,file_path TEXT,
               symbol_hash TEXT,severity TEXT,status TEXT,message TEXT,detected_at REAL,resolved_at REAL);
             CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT DEFAULT 'open');
             CREATE TABLE change_audit(
               id TEXT PRIMARY KEY,task_id TEXT,step_id TEXT,file_path TEXT);
             CREATE TABLE task_quality_findings(
               id TEXT PRIMARY KEY,task_id TEXT,status TEXT,severity TEXT,
               finding_type TEXT,source TEXT,message TEXT);
             CREATE TABLE audit_chain(
               id INTEGER PRIMARY KEY AUTOINCREMENT,table_name TEXT,record_id TEXT,
               operation TEXT,payload_hash TEXT,prev_signature TEXT,record_signature TEXT,
               signing_key_id TEXT,signed_at REAL);
             CREATE TABLE audit_key_rotations(
               id INTEGER PRIMARY KEY AUTOINCREMENT,key_id TEXT UNIQUE,key_secret TEXT,
               rotated_at REAL,is_active INTEGER);
             CREATE TABLE workspace_scan_runs(
               id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,git_head TEXT,
               started_at REAL,status TEXT);
             CREATE TABLE agent_rule_candidates(
               id TEXT PRIMARY KEY,title TEXT,rule_text TEXT,scope_json TEXT,severity TEXT,
               source TEXT,evidence_json TEXT,confidence REAL,status TEXT,created_at REAL,
               reviewed_at REAL,reviewer TEXT,linked_rule_id TEXT);
             CREATE TABLE agent_rules(
               id TEXT PRIMARY KEY,title TEXT,rule_text TEXT,scope_json TEXT,severity TEXT,
               status TEXT,source_candidate_id TEXT,evidence_json TEXT,created_at REAL,
               updated_at REAL,synced_to_agents_md INTEGER,sync_hash TEXT);
             CREATE TABLE agent_rule_sync_log(
               id TEXT PRIMARY KEY,target_path TEXT,rule_ids_json TEXT,before_hash TEXT,
               after_hash TEXT,dry_run INTEGER,created_at REAL,actor TEXT);",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO workspaces VALUES (1,?1,1)",
            params![dir.path().to_string_lossy().to_string()],
        )
        .unwrap();
        (dir, conn)
    }

    #[test]
    fn candidate_review_is_atomic_and_idempotent() {
        let (_dir, mut conn) = fixture();
        let id = create_rule_candidate(
            &mut conn,
            &RuleCandidateInput {
                title: "No raw SQL".to_string(),
                rule_text: "Use transactions".to_string(),
                scope: json!({"actions":["edit"]}),
                severity: "critical".to_string(),
                source: "manual".to_string(),
                evidence: json!({}),
                confidence: 2.0,
            },
        )
        .unwrap();
        let rule_id = accept_rule_candidate(&mut conn, &id, "reviewer").unwrap();
        assert_eq!(
            accept_rule_candidate(&mut conn, &id, "reviewer").unwrap(),
            rule_id
        );
        assert!(reject_rule_candidate(&mut conn, &id, "reviewer", "late").is_err());
        assert_eq!(list_agent_rules(&conn, "active", 10).unwrap().len(), 1);
    }

    #[test]
    fn applicable_rules_use_and_across_scope_fields() {
        let (_dir, conn) = fixture();
        conn.execute(
            "INSERT INTO agent_rules VALUES
             ('r1','Scoped','text',?1,'critical','active','','{}',1,2,0,'')",
            params![r#"{"languages":["rust"],"actions":["edit"],"file_patterns":["src/*.rs"]}"#],
        )
        .unwrap();
        let matched = applicable_agent_rules(
            &conn,
            &json!({"language":"rust","action":"edit","file_path":"src/lib.rs"}),
            5,
        )
        .unwrap();
        assert_eq!(matched.len(), 1);
        assert!(applicable_agent_rules(
            &conn,
            &json!({"language":"rust","action":"review","file_path":"src/lib.rs"}),
            5
        )
        .unwrap()
        .is_empty());
    }

    #[test]
    fn sync_dry_run_is_read_only_and_rejects_path_escape() {
        let (dir, mut conn) = fixture();
        conn.execute(
            "INSERT INTO agent_rules VALUES
             ('r1','Rule','text','{}','warning','active','','{}',1,2,0,'')",
            [],
        )
        .unwrap();
        let before = fs::read(dir.path().join("AGENTS.md")).unwrap();
        let result = sync_agent_rules(&mut conn, Path::new("AGENTS.md"), true, "agent").unwrap();
        assert!(result.success);
        assert_eq!(before, fs::read(dir.path().join("AGENTS.md")).unwrap());
        assert_eq!(
            conn.query_row("SELECT COUNT(*) FROM agent_rule_sync_log", [], |row| row
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert!(sync_agent_rules(&mut conn, Path::new("../AGENTS.md"), false, "agent").is_err());
    }

    #[test]
    fn rule_lifecycle_sync_extract_seed_and_cleanup_match_python_contract() {
        let (dir, mut conn) = fixture();
        conn.execute(
            "INSERT INTO agent_rules VALUES
             ('r1','Rule','text','{}','warning','active','','{}',1,2,0,'')",
            [],
        )
        .unwrap();

        let synced =
            sync_agent_rules(&mut conn, Path::new("AGENTS.md"), false, "reviewer").unwrap();
        assert!(synced.success);
        assert!(!synced.dry_run);
        assert!(fs::read_to_string(dir.path().join("AGENTS.md"))
            .unwrap()
            .contains("- [r1] **Rule** (severity: warning): text"));
        assert_eq!(
            conn.query_row(
                "SELECT synced_to_agents_md FROM agent_rules WHERE id='r1'",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap(),
            1
        );

        fs::write(dir.path().join("SECOND.md"), "manual\n").unwrap();
        assert!(insert_rule_marker_block(&mut conn, Path::new("SECOND.md"), "reviewer").unwrap());
        assert!(!insert_rule_marker_block(&mut conn, Path::new("SECOND.md"), "reviewer").unwrap());

        conn.execute_batch(
            "INSERT INTO task_quality_findings
             (id,task_id,status,severity,finding_type,source,message) VALUES
             ('1','task-1','open','warn','missing_test','review','sample'),
             ('2','task-1','open','warn','missing_test','review','sample');",
        )
        .unwrap();
        let extracted = extract_rule_candidates(&mut conn, "task-1", 2).unwrap();
        assert_eq!(extracted.len(), 1);
        assert!(extract_rule_candidates(&mut conn, "task-1", 2)
            .unwrap()
            .is_empty());
        let evidence = conn
            .query_row(
                "SELECT evidence_json FROM agent_rule_candidates WHERE id=?1",
                params![extracted[0]],
                |row| row.get::<_, String>(0),
            )
            .unwrap();
        assert_eq!(
            evidence,
            r#"{"source": "task_quality_findings", "finding_ids": [1, 2], "occurrences": 2, "task_id": "task-1", "sample_message": "sample"}"#
        );

        let before_seed = conn
            .query_row("SELECT COUNT(*) FROM agent_rules", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap();
        let preview = seed_bootstrap_rules(&mut conn, true).unwrap();
        assert_eq!(preview.created, 5);
        assert_eq!(
            conn.query_row("SELECT COUNT(*) FROM agent_rules", [], |row| row
                .get::<_, i64>(0))
                .unwrap(),
            before_seed
        );
        let applied = seed_bootstrap_rules(&mut conn, false).unwrap();
        assert_eq!(applied.created, 5);
        assert_eq!(seed_bootstrap_rules(&mut conn, false).unwrap().skipped, 5);
        assert_eq!(
            conn.query_row(
                "SELECT rule_text FROM agent_rules WHERE id='AR-bootstrap-i18n'",
                [],
                |row| row.get::<_, String>(0),
            )
            .unwrap(),
            "所有用户可见的 CLI/MCP 输出（提示、错误、状态、摘要）必须通过 i18n.t() 获取，禁止硬编码中文/英文字符串。新增输出时必须同时更新 i18n/zh_CN.json 与 i18n/en_US.json。"
        );

        conn.execute(
            "INSERT INTO agent_rule_sync_log VALUES
             ('old-1','AGENTS.md','[]','','',0,1,'agent')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO agent_rule_sync_log VALUES
             ('old-2','AGENTS.md','[]','','',0,2,'agent')",
            [],
        )
        .unwrap();
        let preview_cleanup = cleanup_rule_sync_log(&mut conn, 1, 2, true).unwrap();
        assert_eq!(preview_cleanup.deleted_count, 2);
        assert_eq!(
            conn.query_row("SELECT COUNT(*) FROM agent_rule_sync_log", [], |row| row
                .get::<_, i64>(0))
                .unwrap(),
            4
        );
        let applied_cleanup = cleanup_rule_sync_log(&mut conn, 1, 2, false).unwrap();
        assert_eq!(applied_cleanup.deleted_count, 2);
        assert_eq!(applied_cleanup.remaining_count, 2);
    }

    #[test]
    fn guardrail_scan_is_atomic_filtered_and_deduplicated() {
        let (dir, mut conn) = fixture();
        let file = dir.path().join("danger.sql");
        fs::write(
            &file,
            "ALTER TABLE users ADD COLUMN x INT;\nDROP TABLE audit;\n",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO file_instances VALUES (1,1,'danger.sql',?1,'ready')",
            params![file.to_string_lossy().to_string()],
        )
        .unwrap();

        let first = scan_guardrails(&mut conn, "danger", "db_safety").unwrap();
        assert_eq!(first.scanned_files, 1);
        assert_eq!(first.findings.len(), 3);
        assert!(first
            .findings
            .iter()
            .any(|finding| finding.severity == "block"));
        assert_eq!(
            list_guardrail_rules(&mut conn, "").unwrap().len(),
            BUILTIN_GUARDRAIL_RULES.len()
        );

        let second = scan_guardrails(&mut conn, "danger", "db_safety").unwrap();
        assert!(second.findings.is_empty());
        assert!(scan_guardrails(&mut conn, "../", "").is_err());
        assert!(scan_guardrails(&mut conn, "", "unknown").is_err());
    }

    #[test]
    fn check_gate_rejects_missing_task_and_change_evidence() {
        let (_dir, mut conn) = fixture();
        assert!(run_check_gate(&mut conn, "missing", "", 1).is_err());
        conn.execute("INSERT INTO tasks(id) VALUES ('task-1')", [])
            .unwrap();
        let error = run_check_gate(&mut conn, "task-1", "", 1).unwrap_err();
        assert!(error.contains("no change_audit evidence"));
        assert!(resolve_gate_findings(&mut conn, "task-1").is_err());
    }

    #[test]
    fn semgrep_payload_is_scoped_and_errors_fail_closed() {
        let (dir, _conn) = fixture();
        let file = dir.path().join("sample.py");
        fs::write(&file, "print('x')\n").unwrap();
        let files = vec![("sample.py".to_string(), file.clone())];
        let payload = json!({
            "results": [{
                "check_id": "python.demo",
                "path": file.to_string_lossy(),
                "start": {"line": 1},
                "extra": {"severity": "ERROR", "message": "demo"}
            }],
            "errors": []
        });
        let findings = parse_semgrep_payload(dir.path(), &files, &payload).unwrap();
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].file_path, "sample.py");
        assert_eq!(findings[0].severity, "error");

        assert!(parse_semgrep_payload(
            dir.path(),
            &files,
            &json!({"results": [], "errors": [{"message": "bad config"}]})
        )
        .is_err());
        assert!(parse_semgrep_payload(
            dir.path(),
            &files,
            &json!({"results": [{"path": "outside.py", "extra": {}}], "errors": []})
        )
        .is_err());
    }

    #[test]
    fn audit_verify_detects_tampering_and_missing_keys() {
        let (_dir, conn) = fixture();
        let first_hash = sha256_hex(b"first");
        let first_signature = crate::daemon_query::audit_compute_signature("", &first_hash, None);
        let second_hash = sha256_hex(b"second");
        let second_signature =
            crate::daemon_query::audit_compute_signature(&first_signature, &second_hash, None);
        conn.execute(
            "INSERT INTO audit_chain
             (table_name,record_id,operation,payload_hash,prev_signature,record_signature,
              signing_key_id,signed_at)
             VALUES ('events','1','insert',?1,'',?2,'local',1),
                    ('events','2','insert',?3,?2,?4,'local',2)",
            params![first_hash, first_signature, second_hash, second_signature],
        )
        .unwrap();
        let verified = verify_audit_chain(&conn, "events", 100).unwrap();
        assert_eq!(verified.verified_count, 2);
        assert_eq!(verified.broken_count, 0);

        conn.execute(
            "UPDATE audit_chain SET record_signature='tampered' WHERE id=1",
            [],
        )
        .unwrap();
        let broken = verify_audit_chain(&conn, "events", 100).unwrap();
        assert!(broken.broken_count >= 1);
        assert!(broken
            .broken_records
            .iter()
            .any(|record| record.reasons.contains(&"signature_mismatch".to_string())));

        conn.execute(
            "UPDATE audit_chain SET signing_key_id='deleted-key' WHERE id=2",
            [],
        )
        .unwrap();
        let missing = verify_audit_chain(&conn, "events", 100).unwrap();
        assert!(missing
            .broken_records
            .iter()
            .any(|record| record.reasons.contains(&"signing_key_missing".to_string())));
    }

    #[test]
    fn audit_key_rotation_preserves_old_key_verification() {
        let (_dir, mut conn) = fixture();
        rotate_audit_key(&mut conn, "key-1", "secret-1").unwrap();
        let hash1 = sha256_hex(b"one");
        let signature1 =
            crate::daemon_query::audit_compute_signature("", &hash1, Some(b"secret-1"));
        conn.execute(
            "INSERT INTO audit_chain
             (table_name,record_id,operation,payload_hash,prev_signature,record_signature,
              signing_key_id,signed_at)
             VALUES ('events','1','insert',?1,'',?2,'key-1',1)",
            params![hash1, signature1],
        )
        .unwrap();
        let rotation = rotate_audit_key(&mut conn, "key-2", "secret-2").unwrap();
        assert_eq!(rotation.previous_key_id, "key-1");
        let hash2 = sha256_hex(b"two");
        let signature2 =
            crate::daemon_query::audit_compute_signature(&signature1, &hash2, Some(b"secret-2"));
        conn.execute(
            "INSERT INTO audit_chain
             (table_name,record_id,operation,payload_hash,prev_signature,record_signature,
              signing_key_id,signed_at)
             VALUES ('events','2','insert',?1,?2,?3,'key-2',2)",
            params![hash2, signature1, signature2],
        )
        .unwrap();
        let verified = verify_audit_chain(&conn, "events", 100).unwrap();
        assert_eq!(verified.verified_count, 2);
        let keys = list_audit_keys(&conn).unwrap();
        assert_eq!(keys.len(), 2);
        assert_eq!(keys.iter().filter(|key| key.is_active).count(), 1);
        assert_eq!(keys[0].key_id, "key-2");
        assert!(rotate_audit_key(&mut conn, "local", "secret").is_err());
    }

    #[test]
    fn bootstrap_status_propagates_counts_and_recommendation() {
        let (_dir, conn) = fixture();
        conn.execute(
            "INSERT INTO agent_rules VALUES
             ('active','Rule','text','{}','warning','active','','{}',1,2,0,'')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO agent_rule_candidates VALUES
             ('candidate','Rule','text','{}','warning','manual','{}',1,'pending',1,NULL,'','')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO task_quality_findings
             (id,task_id,status,severity,finding_type,source,message)
             VALUES ('finding','','open','block','quality','test','blocking')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO tasks(id,status) VALUES ('task-review','review')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO workspace_scan_runs(workspace_id,git_head,started_at,status)
             VALUES (1,'head',1,'completed')",
            [],
        )
        .unwrap();
        let status = bootstrap_status(&conn).unwrap();
        assert_eq!(status.active_rules_count, 1);
        assert_eq!(status.pending_candidates_count, 1);
        assert_eq!(status.blocking_findings_count, 1);
        assert_eq!(status.tasks["review"], 1);
        assert!(status.recommended_next_action.contains("findings"));
    }
}
