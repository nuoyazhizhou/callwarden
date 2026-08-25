//! Rust SQLite schema/version/transaction boundary.
//!
//! The authoritative DDL remains `db/schema.py`.  It is embedded at compile
//! time so the released Rust binary does not need a Python runtime or a source
//! checkout.  The migration runner deliberately fails closed: it never writes
//! schema version 52 unless all DDL, compatibility columns, indexes, and the
//! operation_params rule seed have committed in one SQLite transaction.

use std::path::Path;
use std::time::Duration;

use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;
use rusqlite::{Connection, OpenFlags, OptionalExtension};

pub const RUST_SCHEMA_VERSION: i64 = 60;

const EMBEDDED_SCHEMA_SOURCE: &str =
    include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../db/schema.py"));

fn schema_sql_block() -> Result<&'static str, String> {
    let marker = "SCHEMA_SQL = \"\"\"";
    let start = EMBEDDED_SCHEMA_SOURCE
        .find(marker)
        .ok_or_else(|| "db/schema.py no longer contains SCHEMA_SQL marker".to_string())?
        + marker.len();
    let end = EMBEDDED_SCHEMA_SOURCE[start..]
        .find("\n\"\"\"")
        .ok_or_else(|| "db/schema.py SCHEMA_SQL block is unterminated".to_string())?
        + start;
    Ok(&EMBEDDED_SCHEMA_SOURCE[start..end])
}

fn statement_end(source: &str, start: usize) -> Option<usize> {
    let bytes = source.as_bytes();
    let mut depth = 0usize;
    let mut single_quote = false;
    let mut index = start;
    while index < bytes.len() {
        match bytes[index] {
            b'\'' => {
                if single_quote && bytes.get(index + 1) == Some(&b'\'') {
                    index += 1;
                } else {
                    single_quote = !single_quote;
                }
            }
            b'(' if !single_quote => depth += 1,
            b')' if !single_quote && depth > 0 => depth -= 1,
            b';' if !single_quote && depth == 0 => return Some(index + 1),
            _ => {}
        }
        index += 1;
    }
    None
}

fn ddl_statements_precise(source: &str, prefixes: &[&str]) -> Vec<String> {
    let mut statements = Vec::new();
    for (start, _) in source.match_indices("CREATE ") {
        if start > 0 && source.as_bytes()[start - 1] != b'\n' {
            continue;
        }
        let line = &source[start..];
        if prefixes.iter().any(|prefix| line.starts_with(prefix)) {
            if let Some(end) = statement_end(source, start) {
                statements.push(source[start..end].to_string());
            }
        }
    }
    statements
}

fn execute_existing_schema(conn: &Connection, schema: &str) -> Result<(), String> {
    for statement in ddl_statements_precise(schema, &["CREATE TABLE", "CREATE VIRTUAL TABLE"]) {
        conn.execute_batch(&statement)
            .map_err(|error| format!("schema table DDL failed: {error}"))?;
    }

    // Columns introduced after the original table definitions.  Keeping this
    // list explicit makes old databases fail visibly instead of being stamped
    // as current while a production query still lacks a required column.
    for (table, column, definition) in [
        ("tasks", "applied_at", "REAL"),
        ("file_versions", "ast_cache", "BLOB"),
        ("workspaces", "active_task_id", "TEXT DEFAULT ''"),
        (
            "task_symbol_changes",
            "source_commit_hash",
            "TEXT DEFAULT ''",
        ),
        ("git_file_changes", "lines_added", "INTEGER DEFAULT 0"),
        ("git_file_changes", "lines_deleted", "INTEGER DEFAULT 0"),
        ("semgrep_findings", "scan_id", "INTEGER"),
        ("guardrail_findings", "workspace_id", "INTEGER NOT NULL DEFAULT 0"),
        ("agent_registrations", "agent_instance_id", "TEXT DEFAULT ''"),
        ("agent_registrations", "client_id", "TEXT DEFAULT ''"),
        ("agent_registrations", "provider", "TEXT DEFAULT ''"),
        ("agent_registrations", "model_id", "TEXT DEFAULT ''"),
        ("agent_registrations", "model_mode", "TEXT DEFAULT ''"),
        ("agent_registrations", "system_fingerprint", "TEXT DEFAULT ''"),
        ("agent_registrations", "runtime_hash", "TEXT DEFAULT ''"),
        ("agent_registrations", "session_id", "TEXT DEFAULT ''"),
        ("agent_registrations", "role", "TEXT DEFAULT ''"),
        ("task_contract_revisions", "normalization_version", "TEXT DEFAULT ''"),
        ("task_contract_revisions", "normalization_rules_hash", "TEXT DEFAULT ''"),
        ("task_verdict_events", "step_id", "TEXT DEFAULT ''"),
        ("task_verdict_events", "role_contract_lineage_id", "TEXT DEFAULT ''"),
        ("task_verdict_events", "role_contract_revision_id", "TEXT DEFAULT ''"),
        ("task_verdict_events", "role_contract_revision", "INTEGER DEFAULT 0"),
        ("task_verdict_events", "role_contract_hash", "TEXT DEFAULT ''"),
        ("task_verdict_events", "canonicalization_version", "TEXT DEFAULT ''"),
        ("task_verdict_events", "canonicalization_rules_hash", "TEXT DEFAULT ''"),
        ("task_verdict_events", "normalization_version", "TEXT DEFAULT ''"),
        ("task_verdict_events", "normalization_rules_hash", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "step_id", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "role_contract_lineage_id", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "role_contract_revision_id", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "role_contract_revision", "INTEGER DEFAULT 0"),
        ("task_gate_decisions", "role_contract_hash", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "canonicalization_version", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "canonicalization_rules_hash", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "normalization_version", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "normalization_rules_hash", "TEXT DEFAULT ''"),
    ] {
        let present = conn
            .prepare(&format!("PRAGMA table_info({table})"))
            .and_then(|mut statement| {
                statement
                    .query_map([], |row| row.get::<_, String>(1))?
                    .collect::<Result<Vec<_>, _>>()
            })
            .map_err(|error| format!("cannot inspect {table}: {error}"))?
            .iter()
            .any(|name| name == column);
        if !present {
            conn.execute_batch(&format!(
                "ALTER TABLE {table} ADD COLUMN {column} {definition}"
            ))
            .map_err(|error| format!("cannot add {table}.{column}: {error}"))?;
        }
    }

    // Indexes are executed after compatibility columns are present.  Trigger
    // recreation is intentionally left to the existing schema; missing index
    // or uniqueness errors remain fatal and prevent version publication.
    for statement in ddl_statements_precise(schema, &["CREATE INDEX", "CREATE UNIQUE INDEX"]) {
        conn.execute_batch(&statement)
            .map_err(|error| format!("schema index DDL failed: {error}"))?;
    }

    // v48（W2.3 P1-1）：guardrail_findings 旧数据（无 workspace 归属）必须
    // fail-closed。workspace_id=0 且 status='open' 的旧行置为 'orphaned'，
    // 禁止静默归入当前 workspace。幂等：重复执行时不再有 open 的 0 归属行。
    let has_guardrail = conn
        .prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='guardrail_findings'")
        .and_then(|mut statement| statement.exists([]))
        .unwrap_or(false);
    if has_guardrail {
        conn.execute(
            "UPDATE guardrail_findings SET status='orphaned'
             WHERE workspace_id = 0 AND status = 'open'",
            [],
        )
        .map_err(|error| format!("cannot orphan unowned guardrail findings: {error}"))?;
    }
    Ok(())
}

/// v52（1D1）：operation_params 初始 rule row 幂等播种。
///
/// 与 Python `_migrate_v51_to_v52` 完全一致：已存在则原子校验 payload 与 hash，
/// 任何差异都 fail closed（禁止静默改写或重解释）。payload 与 hash 由
/// `_gen_rules_seed.py` 冻结生成，operation_store 运行时查找的常量必须一致。
/// pub(crate)：storage.rs StorageService 迁移复用同一播种逻辑（单点真相源）。
pub(crate) fn seed_operation_params_rule(conn: &Connection) -> Result<(), String> {
    const RULES_PAYLOAD: &str = "{\"canonicalization_version\":\"operation-params-c14n/v1\",\"description\":\"Task-domain operation payload canonicalization for task_operation_ledger dedup\",\"excluded_keys\":[\"request_id\",\"workspace_instance_id\"],\"hash_algorithm\":\"sha256\",\"hash_prefix\":\"sha256:\",\"key_ordering\":\"unicode_code_point\",\"normalization\":\"unicode_nfc\",\"serialization\":\"rfc8785_json_canonicalization_scheme\"}";
    const RULES_HASH: &str =
        "sha256:8b7a902fd1463c8a79a6b687161b6be471ad62877d4920f4e874e11bbb2e495c";

    let exists = conn
        .prepare(
            "SELECT rules_payload_json, rules_hash FROM canonicalization_rule_sets \
             WHERE domain = 'operation_params' AND canonicalization_version = 'operation-params-c14n/v1'",
        )
        .and_then(|mut statement| statement.query_row([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        }));

    match exists {
        Err(rusqlite::Error::QueryReturnedNoRows) => {
            conn.execute(
                "INSERT INTO canonicalization_rule_sets \
                 (rule_set_id, domain, canonicalization_version, rules_payload_json, \
                  rules_c14n_version, rules_hash, created_by, authoritative_created_at) \
                 VALUES ('operation-params-c14n/v1', 'operation_params', 'operation-params-c14n/v1', \
                         ?1, 'rules-c14n/v1', ?2, 'migration-v51-to-v52', \
                         strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                rusqlite::params![RULES_PAYLOAD, RULES_HASH],
            )
            .map_err(|error| format!("cannot seed operation_params rule row: {error}"))?;
            Ok(())
        }
        Err(error) => Err(format!("cannot inspect operation_params rule row: {error}")),
        Ok((payload, hash)) => {
                if payload != RULES_PAYLOAD || hash != RULES_HASH {
                    return Err(format!(
                        "operation-params-c14n/v1 rule row mismatch \
                         (payload={} hash={} expected_payload={} expected_hash={})",
                        &payload[..payload.len().min(120)],
                        &hash[..hash.len().min(120)],
                        &RULES_PAYLOAD[..RULES_PAYLOAD.len().min(120)],
                        &RULES_HASH[..RULES_HASH.len().min(120)]
                    ));
                }
                Ok(())
            }
        }
}

/// 1A：幂等播种 `('workspace_capture', 'workspace-capture-c14n/v1')` 初始 rule row
/// （cw-role-handoff-task-loop.md §8.1.3 / §8.1.1）。1A 独占该 domain row；本函数与
/// schema v53 绑定，失败则整个 migration 回滚（fail closed）。
///
/// 校验规则：已存在 row 的 payload/rules_hash 必须与冻结常量一致，否则拒绝迁移。
/// rules_hash 固定是 `rules-c14n/v1` 对 `rules_payload_json` 的 SHA-256。
pub(crate) fn seed_workspace_capture_rule(conn: &Connection) -> Result<(), String> {
    const RULES_PAYLOAD: &str = "{\"canonicalization_version\":\"workspace-capture-c14n/v1\",\"description\":\"Task workspace authority capture canonicalization for immutable task_workspace_bindings\",\"hash_algorithm\":\"sha256\",\"hash_prefix\":\"sha256:\",\"key_ordering\":\"unicode_code_point\",\"normalization\":\"unicode_nfc\",\"serialization\":\"rfc8785_json_canonicalization_scheme\",\"payload_fields\":[\"workspace_instance_id\",\"client_view_root_hash\",\"host_real_root_hash\",\"workspace_manifest_hash\"]}";
    let rules_hash = rules_c14n_hash_v53(RULES_PAYLOAD);

    let exists = conn
        .prepare(
            "SELECT rules_payload_json, rules_hash FROM canonicalization_rule_sets \
             WHERE domain = 'workspace_capture' AND canonicalization_version = 'workspace-capture-c14n/v1'",
        )
        .and_then(|mut statement| statement.query_row([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        }));

    match exists {
        Err(rusqlite::Error::QueryReturnedNoRows) => {
            conn.execute(
                "INSERT INTO canonicalization_rule_sets \
                 (rule_set_id, domain, canonicalization_version, rules_payload_json, \
                  rules_c14n_version, rules_hash, created_by, authoritative_created_at) \
                 VALUES ('workspace-capture-c14n/v1', 'workspace_capture', 'workspace-capture-c14n/v1', \
                         ?1, 'rules-c14n/v1', ?2, 'migration-v52-to-v53', \
                         strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                rusqlite::params![RULES_PAYLOAD, rules_hash],
            )
            .map_err(|error| format!("cannot seed workspace_capture rule row: {error}"))?;
            Ok(())
        }
        Err(error) => Err(format!("cannot inspect workspace_capture rule row: {error}")),
        Ok((payload, hash)) => {
            if payload != RULES_PAYLOAD || hash != rules_hash {
                return Err(format!(
                    "workspace-capture-c14n/v1 rule row mismatch \
                     (payload={} hash={} expected_payload={} expected_hash={})",
                    &payload[..payload.len().min(120)],
                    &hash[..hash.len().min(120)],
                    &RULES_PAYLOAD[..RULES_PAYLOAD.len().min(120)],
                    &rules_hash[..rules_hash.len().min(120)]
                ));
            }
            Ok(())
        }
    }
}

/// `rules-c14n/v1` 对规则载荷做 UTF-8、Unicode NFC、键按 code point 排序、无额外空白
/// 的 canonical JSON 后取 SHA-256，表示为 `sha256:<hex>`（§8.1.3 自举冻结）。
/// operation_store.rs 内部有等价的私有实现；此处为 schema 种子独立保留一份以维持
/// foundation/领域所有权边界（SQLite 迁移属 1A 自己的原子 migration）。
fn rules_c14n_hash_v53(payload: &str) -> String {
    use sha2::{Digest, Sha256};
    use std::collections::BTreeMap;
    fn c14n(value: &serde_json::Value) -> serde_json::Value {
        match value {
            serde_json::Value::Object(map) => {
                let sorted: BTreeMap<String, serde_json::Value> = map
                    .iter()
                    .map(|(k, v)| (k.to_owned(), c14n(v)))
                    .collect();
                let mut out = serde_json::Map::new();
                for (k, v) in sorted {
                    out.insert(k, v);
                }
                serde_json::Value::Object(out)
            }
            serde_json::Value::Array(arr) => {
                serde_json::Value::Array(arr.iter().map(c14n).collect())
            }
            other => other.clone(),
        }
    }
    let value: serde_json::Value = serde_json::from_str(payload).unwrap_or_default();
    let canonical = c14n(&value);
    let bytes = serde_json::to_vec(&canonical).unwrap_or_default();
    let digest = Sha256::digest(&bytes);
    format!("sha256:{}", hex::encode(digest))
}

/// 1B：幂等播种 `('role_contract', 'role-contract-c14n/v1')` 初始 rule row
/// （cw-role-handoff-task-loop.md §8.1.2/§8.1.3）。1B 独占该 domain row；本函数与
/// schema v55 绑定，失败则整个 migration 回滚（fail closed）。
///
/// 校验规则：已存在 row 的 payload/rules_hash 必须与冻结常量一致，否则拒绝迁移。
/// rules_hash 固定是 `rules-c14n/v1` 对 `rules_payload_json` 的 SHA-256。
/// `payload_fields` 与 `task_loop/contract_set.rs` 的 `role-contract-c14n/v1` hash
/// 纳入字段（§8.1.2）完全一致：role、skill_id、skill_version、prompt_template_id、
/// prompt_hash、allowed/forbidden paths、commands、acceptance checks、required
/// evidence、handoff_to 与 independence。
pub(crate) fn seed_role_contract_rule(conn: &Connection) -> Result<(), String> {
    const RULES_PAYLOAD: &str = "{\"canonicalization_version\":\"role-contract-c14n/v1\",\"description\":\"Role Contract lineage revision canonicalization for immutable role_contract_revisions\",\"hash_algorithm\":\"sha256\",\"hash_prefix\":\"sha256:\",\"key_ordering\":\"unicode_code_point\",\"normalization\":\"unicode_nfc\",\"payload_fields\":[\"acceptance_checks\",\"allowed_paths\",\"commands\",\"forbidden_paths\",\"handoff_to\",\"independence\",\"prompt_hash\",\"prompt_template_id\",\"required_evidence\",\"role\",\"skill_id\",\"skill_version\"],\"serialization\":\"rfc8785_json_canonicalization_scheme\"}";
    let rules_hash = rules_c14n_hash_v53(RULES_PAYLOAD);

    let exists = conn
        .prepare(
            "SELECT rules_payload_json, rules_hash FROM canonicalization_rule_sets \
             WHERE domain = 'role_contract' AND canonicalization_version = 'role-contract-c14n/v1'",
        )
        .and_then(|mut statement| statement.query_row([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        }));

    match exists {
        Err(rusqlite::Error::QueryReturnedNoRows) => {
            conn.execute(
                "INSERT INTO canonicalization_rule_sets \
                 (rule_set_id, domain, canonicalization_version, rules_payload_json, \
                  rules_c14n_version, rules_hash, created_by, authoritative_created_at) \
                 VALUES ('role-contract-c14n/v1', 'role_contract', 'role-contract-c14n/v1', \
                         ?1, 'rules-c14n/v1', ?2, 'migration-v54-to-v55', \
                         strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                rusqlite::params![RULES_PAYLOAD, rules_hash],
            )
            .map_err(|error| format!("cannot seed role_contract rule row: {error}"))?;
            Ok(())
        }
        Err(error) => Err(format!("cannot inspect role_contract rule row: {error}")),
        Ok((payload, hash)) => {
            if payload != RULES_PAYLOAD || hash != rules_hash {
                return Err(format!(
                    "role-contract-c14n/v1 rule row mismatch \
                     (payload={} hash={} expected_payload={} expected_hash={})",
                    &payload[..payload.len().min(120)],
                    &hash[..hash.len().min(120)],
                    &RULES_PAYLOAD[..RULES_PAYLOAD.len().min(120)],
                    &rules_hash[..rules_hash.len().min(120)]
                ));
            }
            Ok(())
        }
    }
}

/// 1E：幂等播种 `verdict-normalization/v1` 初始 rule row
/// （cw-role-handoff-task-loop.md §4.2/§8.1.3、§1101-1127）。1E 独占该 registry row；
/// 本函数与 schema v57 绑定，失败则整个 migration 回滚（fail closed）。
///
/// 校验规则：已存在 row 的 payload/rules_hash 必须与冻结常量一致，否则拒绝迁移。
/// rules_hash 固定是 `rules-c14n/v1` 对 `rules_payload_json` 的 SHA-256。Task Contract、
/// Gate decision 与 verdict projection 均引用该 row 的 `(normalization_version,
/// rules_hash)`；缺 row、hash 不匹配或 revoked 时保持 UNVERIFIED，不得以"最新"规则
/// 替代或进入 pass 路径。
pub(crate) fn seed_verdict_normalization_rule(conn: &Connection) -> Result<(), String> {
    const RULES_PAYLOAD: &str = "{\"canonicalization_version\":\"verdict-normalization/v1\",\"description\":\"Reviewer verdict overall/phase normalization for task_verdict_events and Evidence Gate projection\",\"hash_algorithm\":\"sha256\",\"hash_prefix\":\"sha256:\",\"key_ordering\":\"unicode_code_point\",\"normalization\":\"unicode_nfc\",\"overall_map\":{\"abstain\":\"UNVERIFIED\",\"approved\":\"pass\",\"needs_changes\":\"block\",\"rejected\":\"block\",\"request_changes\":\"block\",\"unclear\":\"UNVERIFIED\"},\"phase_map\":{\"POST_VERDICT\":\"post_reveal_amendment\",\"PRE_VERDICT\":\"blind_first_pass\"},\"serialization\":\"rfc8785_json_canonicalization_scheme\"}";
    let rules_hash = rules_c14n_hash_v53(RULES_PAYLOAD);

    let exists = conn
        .prepare(
            "SELECT rules_payload_json, rules_hash FROM verdict_normalization_rules \
             WHERE normalization_version = 'verdict-normalization/v1'",
        )
        .and_then(|mut statement| {
            statement.query_row([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
        });

    match exists {
        Err(rusqlite::Error::QueryReturnedNoRows) => {
            conn.execute(
                "INSERT INTO verdict_normalization_rules \
                 (verdict_rule_set_id, normalization_version, rules_payload_json, \
                  rules_c14n_version, rules_hash, created_by, authoritative_created_at) \
                 VALUES ('verdict-normalization/v1', 'verdict-normalization/v1', \
                         ?1, 'rules-c14n/v1', ?2, 'migration-v56-to-v57', \
                         strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                rusqlite::params![RULES_PAYLOAD, rules_hash],
            )
            .map_err(|error| format!("cannot seed verdict-normalization rule row: {error}"))?;
            Ok(())
        }
        Err(error) => Err(format!("cannot inspect verdict-normalization rule row: {error}")),
        Ok((payload, hash)) => {
            if payload != RULES_PAYLOAD || hash != rules_hash {
                return Err(format!(
                    "verdict-normalization/v1 rule row mismatch \
                     (payload={} hash={} expected_payload={} expected_hash={})",
                    &payload[..payload.len().min(120)],
                    &hash[..hash.len().min(120)],
                    &RULES_PAYLOAD[..RULES_PAYLOAD.len().min(120)],
                    &rules_hash[..rules_hash.len().min(120)]
                ));
            }
            Ok(())
        }
    }
}

/// 读取 `role_contract` rule row 的 rules_hash（1B legacy 回填写 provenance 使用）。
/// 缺 row 或不可读一律 fail closed（§8.1.3，capability 未就绪）。
fn role_contract_rules_hash(conn: &Connection) -> Result<String, String> {
    conn.query_row(
        "SELECT rules_hash FROM canonicalization_rule_sets \
         WHERE domain = 'role_contract' AND canonicalization_version = 'role-contract-c14n/v1'",
        [],
        |row| row.get::<_, String>(0),
    )
    .map_err(|e| format!("role_contract rule row 不可用（capability 未就绪）: {e}"))
}

/// 1B：历史 `role_contracts` → lineage/revision 的回填（cw-role-handoff-task-loop.md
/// §8.1.2）。旧行只可在**同时**满足以下条件时按 `(task_id, role)` 创建有 provenance 的
/// lineage/revision 副本；迁移绝不 UPDATE 旧行：
///   1. 唯一 task→workspace binding（task_workspace_bindings 该 task 恰一行）；
///   2. 可解析完整 payload（所有 JSON 字段可解析、role 非空、路径/列表满足 c14n 规则）；
///   3. 可确定 revision 链（revision 恰好 1..n 连续且无分叉）；
///   4. 非歧义（原有空 step_id 等歧义不回填，保留历史行并标记相关 v1 结果 UNVERIFIED）。
pub(crate) fn migrate_role_contract_legacy(conn: &Connection) -> Result<(), String> {
    let rules_hash = role_contract_rules_hash(conn)?;

    let mut stmt = conn
        .prepare(
            "SELECT task_id, role, revision, step_id, skill_id, skill_version, \
                    prompt_template_id, prompt_hash, allowed_paths, forbidden_paths, \
                    commands, acceptance_checks, required_evidence, handoff_to, \
                    independence, created_by, created_at \
             FROM role_contracts ORDER BY task_id ASC, role ASC, revision ASC",
        )
        .map_err(|e| format!("cannot read legacy role_contracts: {e}"))?;

    let rows = stmt
        .query_map([], |row| {
            Ok(LegacyContractRow {
                task_id: row.get(0)?,
                role: row.get(1)?,
                revision: row.get(2)?,
                step_id: row.get(3)?,
                skill_id: row.get(4)?,
                skill_version: row.get(5)?,
                prompt_template_id: row.get(6)?,
                prompt_hash: row.get(7)?,
                allowed_paths: row.get(8)?,
                forbidden_paths: row.get(9)?,
                commands: row.get(10)?,
                acceptance_checks: row.get(11)?,
                required_evidence: row.get(12)?,
                handoff_to: row.get(13)?,
                independence: row.get(14)?,
                created_by: row.get(15)?,
                created_at: row.get(16)?,
            })
        })
        .map_err(|e| format!("cannot map legacy role_contracts: {e}"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| format!("cannot collect legacy role_contracts: {e}"))?;

    // 按 (task_id, role) 升序分组（SQL 已按该序排列）。
    let mut groups: Vec<Vec<LegacyContractRow>> = Vec::new();
    for row in rows {
        match groups.last_mut() {
            Some(last)
                if last.first().map(|r| &r.task_id) == Some(&row.task_id)
                    && last.first().map(|r| &r.role) == Some(&row.role) =>
            {
                last.push(row);
            }
            _ => groups.push(vec![row]),
        }
    }

    for group in groups {
        if let Err(reason) = backfill_group(conn, &rules_hash, &group) {
            // §8.1.2：任一歧义不回填，保留历史行（相关 v1 结果 UNVERIFIED）。
            eprintln!(
                "callwarden-core: role contract legacy backfill skipped \
                 (task_id={} role={}): {reason}",
                group[0].task_id, group[0].role
            );
        }
    }
    Ok(())
}

/// legacy 回填单组（task_id, role）：全部条件满足才写 lineage + revisions。
fn backfill_group(
    conn: &Connection,
    rules_hash: &str,
    rows: &[LegacyContractRow],
) -> Result<(), String> {
    // 1. 唯一 task→workspace binding。
    let binding: Option<(i64, String)> = conn
        .query_row(
            "SELECT workspace_id, workspace_capture_id FROM task_workspace_bindings \
             WHERE task_id = ?1",
            [&rows[0].task_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(|e| format!("workspace binding 查询失败: {e}"))?;
    let (workspace_id, workspace_capture_id) = binding.ok_or("缺 task workspace binding")?;

    // 2. 原有空 step_id 属于歧义（§8.1.2），不回填。
    if rows.iter().any(|r| r.step_id.trim().is_empty()) {
        return Err("原有空 step_id（歧义）".to_string());
    }

    // 3. revision 链必须恰好 1..n 连续（无重复、无缺失、无分叉）。
    let expected: Vec<i64> = (1..=rows.len() as i64).collect();
    let actual: Vec<i64> = rows.iter().map(|r| r.revision).collect();
    if actual != expected {
        return Err(format!(
            "revision 链不连续（期望 {expected:?} 实际 {actual:?}）"
        ));
    }

    // 4. 预解析 payload（可解析完整 payload + 路径/列表 c14n 规则），任一失败即整组跳过。
    let mut canonical_payloads = Vec::with_capacity(rows.len());
    let mut hashes = Vec::with_capacity(rows.len());
    for row in rows {
        let payload = legacy_canonical_payload(row)?;
        let canonical = c14n_legacy(&payload);
        let bytes = serde_json::to_vec(&canonical)
            .map_err(|e| format!("c14n 序列化失败: {e}"))?;
        hashes.push(format!("sha256:{}", hex::encode(sha256_bytes(&bytes))));
        canonical_payloads.push(payload.to_string());
    }

    // 5. 写入 lineage（task, workspace, role 唯一）。
    let lineage_id = format!("rcl-{}-{}", rows[0].task_id, rows[0].role);
    conn.execute(
        "INSERT INTO role_contract_lineages \
         (role_contract_lineage_id, task_id, workspace_id, role, \
          created_by, authoritative_created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        rusqlite::params![
            lineage_id,
            rows[0].task_id,
            workspace_id,
            rows[0].role,
            rows[0].created_by,
            legacy_ts(rows[0].created_at),
        ],
    )
    .map_err(|e| format!("写入 role_contract_lineages 失败: {e}"))?;

    // 6. 写入全部 revision（revision 1 supersedes=NULL；n>1 指向同组 n-1）。
    for (i, row) in rows.iter().enumerate() {
        let revision_id = format!("rcr-{}-{}-r{}", row.task_id, row.role, row.revision);
        let supersedes = if i == 0 {
            None
        } else {
            Some(format!("rcr-{}-{}-r{}", row.task_id, row.role, rows[i - 1].revision))
        };
        conn.execute(
            "INSERT INTO role_contract_revisions \
             (role_contract_revision_id, role_contract_lineage_id, revision, \
              supersedes_revision_id, canonical_payload_json, canonicalization_version, \
              canonicalization_rules_hash, role_contract_hash, created_by, \
              authoritative_created_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
            rusqlite::params![
                revision_id,
                lineage_id,
                row.revision,
                supersedes,
                canonical_payloads[i],
                ROLE_CONTRACT_C14N_VERSION,
                rules_hash,
                hashes[i],
                row.created_by,
                legacy_ts(row.created_at),
            ],
        )
        .map_err(|e| format!("写入 role_contract_revisions 失败: {e}"))?;
    }
    let _ = workspace_capture_id;
    Ok(())
}

/// role-contract-c14n/v1 冻结 version（§8.1.2；与 task_loop/contract_set.rs 常量一致）。
const ROLE_CONTRACT_C14N_VERSION: &str = "role-contract-c14n/v1";

/// legacy 行 → role-contract-c14n/v1 canonical payload（§8.1.2）。路径字段去重排序、
/// 拒绝绝对路径/`..`/反斜杠；command/check/evidence 列表保序且重复即拒绝。
fn legacy_canonical_payload(row: &LegacyContractRow) -> Result<serde_json::Value, String> {
    if row.role.trim().is_empty() {
        return Err("role 为空".to_string());
    }
    let allowed = parse_paths(&row.allowed_paths, "allowed_paths")?;
    let forbidden = parse_paths(&row.forbidden_paths, "forbidden_paths")?;
    let commands = parse_ordered_list(&row.commands, "commands")?;
    let checks = parse_ordered_list(&row.acceptance_checks, "acceptance_checks")?;
    let evidence = parse_ordered_list(&row.required_evidence, "required_evidence")?;
    let independence: serde_json::Value = serde_json::from_str(&row.independence)
        .map_err(|e| format!("independence 不可解析: {e}"))?;
    if !independence.is_object() {
        return Err("independence 必须是 JSON object".to_string());
    }
    Ok(serde_json::json!({
        "role": row.role,
        "skill_id": row.skill_id,
        "skill_version": row.skill_version,
        "prompt_template_id": row.prompt_template_id,
        "prompt_hash": row.prompt_hash,
        "allowed_paths": allowed,
        "forbidden_paths": forbidden,
        "commands": commands,
        "acceptance_checks": checks,
        "required_evidence": evidence,
        "handoff_to": row.handoff_to,
        "independence": independence,
    }))
}

/// 路径集合解析：必须是 JSON array of strings；每条路径项目相对、正斜杠、无 `..`；
/// 集合去重并排序（§8.1.2）。
fn parse_paths(raw: &str, field: &str) -> Result<Vec<String>, String> {
    let value: serde_json::Value =
        serde_json::from_str(raw).map_err(|e| format!("{field} 不可解析: {e}"))?;
    let arr = value
        .as_array()
        .ok_or_else(|| format!("{field} 必须是 JSON array"))?;
    let mut out: Vec<String> = Vec::new();
    for item in arr {
        let path = item
            .as_str()
            .ok_or_else(|| format!("{field} 元素必须是字符串"))?;
        validate_relative_path(path).map_err(|e| format!("{field}: {e}"))?;
        if !out.contains(&path.to_string()) {
            out.push(path.to_string());
        }
    }
    out.sort();
    Ok(out)
}

/// 保序列表解析：JSON array of strings，重复元素即拒绝（§8.1.2）。
fn parse_ordered_list(raw: &str, field: &str) -> Result<Vec<String>, String> {
    let value: serde_json::Value =
        serde_json::from_str(raw).map_err(|e| format!("{field} 不可解析: {e}"))?;
    let arr = value
        .as_array()
        .ok_or_else(|| format!("{field} 必须是 JSON array"))?;
    let mut out: Vec<String> = Vec::new();
    for item in arr {
        let s = item
            .as_str()
            .ok_or_else(|| format!("{field} 元素必须是字符串"))?
            .to_string();
        if out.contains(&s) {
            return Err(format!("{field} 存在重复元素（保序且重复即拒绝）"));
        }
        out.push(s);
    }
    Ok(out)
}

/// 路径只能项目相对、正斜杠、拒绝绝对路径与 `..`（§8.1.2）。
fn validate_relative_path(path: &str) -> Result<(), String> {
    if path.is_empty() {
        return Err("路径不能为空".to_string());
    }
    if path.starts_with('/') || path.starts_with('\\') {
        return Err(format!("绝对路径拒绝: {path}"));
    }
    if path.contains('\\') {
        return Err(format!("路径必须使用正斜杠（拒绝反斜杠）: {path}"));
    }
    if path.split('/').any(|seg| seg == "..") {
        return Err(format!("拒绝 `..` 路径段: {path}"));
    }
    Ok(())
}

/// 递归 canonical 化 Value：字符串 NFC、对象键 NFC + 按 code point 排序。
fn c14n_legacy(value: &serde_json::Value) -> serde_json::Value {
    use unicode_normalization::UnicodeNormalization;
    match value {
        serde_json::Value::Object(map) => {
            let mut sorted: std::collections::BTreeMap<String, serde_json::Value> =
                std::collections::BTreeMap::new();
            for (k, v) in map {
                let key = k.nfc().collect::<String>();
                sorted.insert(key, c14n_legacy(v));
            }
            let mut out = serde_json::Map::new();
            for (k, v) in sorted {
                out.insert(k, v);
            }
            serde_json::Value::Object(out)
        }
        serde_json::Value::Array(arr) => {
            serde_json::Value::Array(arr.iter().map(c14n_legacy).collect())
        }
        serde_json::Value::String(s) => serde_json::Value::String(s.nfc().collect::<String>()),
        other => other.clone(),
    }
}

/// 旧行 created_at（REAL 秒）转权威时间文本（微秒精度）。
fn legacy_ts(created_at: f64) -> String {
    format!("{created_at:.6}")
}

/// legacy 行结构（role_contracts 旧表）。
struct LegacyContractRow {
    task_id: String,
    role: String,
    revision: i64,
    step_id: String,
    skill_id: String,
    skill_version: String,
    prompt_template_id: String,
    prompt_hash: String,
    allowed_paths: String,
    forbidden_paths: String,
    commands: String,
    acceptance_checks: String,
    required_evidence: String,
    handoff_to: String,
    independence: String,
    created_by: String,
    created_at: f64,
}

/// SHA-256 摘要（hex 编码由调用方添加 `sha256:` 前缀）。
fn sha256_bytes(bytes: &[u8]) -> Vec<u8> {
    use sha2::{Digest, Sha256};
    Sha256::digest(bytes).to_vec()
}

/// 读取当前 schema 版本（只读，无写锁）。
///
/// pub(crate)：供 TaskCollabStore 迁移后校验实际版本。
pub(crate) fn current_schema_version(conn: &Connection) -> Result<i64, rusqlite::Error> {
    conn.query_row(
        "SELECT COALESCE(MAX(version), 0) FROM schema_version",
        [],
        |row| row.get(0),
    )
}

/// v59（P0-H，T-1787277487109-758e56d0）：task_supersede 双表 v59 列幂等补齐。
///
/// 针对「版本已被抢先打标为 59（或 ≤58 走迁移）但既有 supersede 基础表仍缺
/// v59 列」的库：`CREATE TABLE IF NOT EXISTS` 不会 ALTER 既有表，必须在迁移/
/// 短路路径显式补列（无损 ALTER ADD COLUMN，不动历史行；与 Python
/// `db_base._migrate_v58_to_v59` 的补列语义一致）。全新库由 schema_sql_block
/// 建齐，本函数对缺表/满列库均为 no-op。
fn ensure_supersede_v59_compat(conn: &Connection) -> Result<(), String> {
    const RELATION_V59_COLUMNS: &[(&str, &str)] = &[
        ("workspace_id", "INTEGER NOT NULL DEFAULT 0"),
        ("supersedence_id", "TEXT NOT NULL DEFAULT ''"),
        ("reason_code", "TEXT NOT NULL DEFAULT 'governance_supersede'"),
        ("actor_agent_id", "TEXT NOT NULL DEFAULT ''"),
        ("actor_session_id", "TEXT NOT NULL DEFAULT ''"),
        ("actor_model_id", "TEXT NOT NULL DEFAULT ''"),
        ("actor_role", "TEXT NOT NULL DEFAULT ''"),
        ("request_id", "TEXT NOT NULL DEFAULT ''"),
        ("lease_id", "TEXT NOT NULL DEFAULT ''"),
        ("fencing_counter", "INTEGER NOT NULL DEFAULT -1"),
        ("evidence_path", "TEXT NOT NULL DEFAULT ''"),
        ("evidence_hash", "TEXT NOT NULL DEFAULT ''"),
        ("authoritative_timestamp", "REAL NOT NULL DEFAULT 0"),
    ];
    for table in ["task_supersede_relations", "task_supersede_events"] {
        let exists: bool = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?1",
                rusqlite::params![table],
                |r| r.get::<_, i64>(0).map(|v| v > 0),
            )
            .map_err(|e| format!("cannot inspect {table}: {e}"))?;
        if !exists {
            continue; // 全新库由 schema_sql_block 建齐
        }
        let existing: Vec<String> = {
            let mut stmt = conn
                .prepare(&format!("PRAGMA table_info({table})"))
                .map_err(|e| format!("cannot inspect {table}: {e}"))?;
            let rows = stmt
                .query_map([], |r| r.get::<_, String>(1))
                .map_err(|e| format!("cannot inspect {table}: {e}"))?;
            let mut out = Vec::new();
            for row in rows {
                out.push(row.map_err(|e| format!("cannot inspect {table}: {e}"))?);
            }
            out
        };
        for (column, ddl) in RELATION_V59_COLUMNS {
            if !existing.iter().any(|c| c == column) {
                conn.execute_batch(&format!("ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                    .map_err(|e| format!("cannot add {table}.{column}: {e}"))?;
            }
        }
    }
    Ok(())
}

/// 事务化官方 schema 迁移（与 Python `_migrate_schema` 等价，幂等）。
///
/// pub(crate)：供 daemon TaskCollabStore 等组件在打开权威库后调用，
/// 确保任务表与 schema_version 审计由同一条正式迁移路径管理。
pub(crate) fn migrate_connection(conn: &Connection) -> Result<i64, String> {
    conn.busy_timeout(Duration::from_secs(5))
        .map_err(|error| format!("cannot set SQLite busy_timeout: {error}"))?;
    conn.execute_batch("PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;")
        .map_err(|error| format!("cannot configure SQLite: {error}"))?;
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at REAL NOT NULL,
            description TEXT DEFAULT ''
        )",
    )
    .map_err(|error| format!("cannot create schema_version: {error}"))?;

    let current = current_schema_version(conn)
        .map_err(|error| format!("cannot read schema version: {error}"))?;

    // P0-H（T-1787277487109-758e56d0）：v59 supersede 列幂等自愈。
    // 短路检查（current >= RUST_SCHEMA_VERSION）之前无条件执行：防止"版本已被
    // 抢先打标为 59 但 supersede 表仍缺 v59 列"的库（CREATE TABLE IF NOT EXISTS
    // 不 ALTER 既有基础表）在短路路径静默通过后，由 validate_supersede_schema
    // fail-closed 拒绝服务（v49 同类教训：短路路径必须做列级校验/补齐）。
    // 无损 ALTER ADD COLUMN，不动历史行；与 Python `_migrate_v58_to_v59` 一致。
    ensure_supersede_v59_compat(conn).map_err(|error| format!("supersede v59 compat failed: {error}"))?;

    if current >= RUST_SCHEMA_VERSION {
        return Ok(current);
    }

    conn.execute_batch("BEGIN IMMEDIATE")
        .map_err(|error| format!("cannot begin schema migration: {error}"))?;
    let result = (|| {
        let schema = schema_sql_block()?;
        if current == 0 {
            conn.execute_batch(schema)
                .map_err(|error| format!("initial schema DDL failed: {error}"))?;
        } else {
            execute_existing_schema(conn, schema)?;
        }
        // v52（1D1）：operation_params 初始 rule row 幂等播种（失败则整个迁移回滚）。
        seed_operation_params_rule(conn)?;
        // v53（1A）：workspace_capture 初始 rule row 幂等播种（失败则整个迁移回滚）。
        seed_workspace_capture_rule(conn)?;
        // v55（1B）：role_contract 初始 rule row 幂等播种（失败则整个迁移回滚）。
        seed_role_contract_rule(conn)?;
        // v55（1B）：历史 role_contracts → lineage/revision 回填（歧义组跳过，保留旧行）。
        migrate_role_contract_legacy(conn)?;
        // v57（1E）：verdict-normalization/v1 初始 rule row 幂等播种（失败则整个迁移回滚）。
        seed_verdict_normalization_rule(conn)?;
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_secs_f64())
            .unwrap_or(0.0);
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version, applied_at, description)
             VALUES (?1, ?2, ?3)",
            rusqlite::params![RUST_SCHEMA_VERSION, now, "Rust schema migration to v59"],
        )
        .map_err(|error| format!("cannot publish schema version 59: {error}"))?;
        Ok::<(), String>(())
    })();

    match result {
        Ok(()) => {
            conn.execute_batch("COMMIT")
                .map_err(|error| format!("schema migration commit failed: {error}"))?;
            Ok(RUST_SCHEMA_VERSION)
        }
        Err(error) => {
            let _ = conn.execute_batch("ROLLBACK");
            Err(error)
        }
    }
}

/// Read the authoritative schema version without taking a write lock.
#[pyfunction]
pub fn sqlite_query_schema_version(db_path: &str) -> PyResult<i64> {
    if db_path.is_empty() {
        return Err(PyValueError::new_err("db_path 不能为空"));
    }
    let conn = Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    )
    .map_err(|error| PyIOError::new_err(format!("打开数据库失败: {error}")))?;
    conn.busy_timeout(Duration::from_secs(5))
        .map_err(|error| PyIOError::new_err(format!("设置 busy_timeout 失败: {error}")))?;
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
    Ok(current_schema_version(&conn).unwrap_or(0))
}

/// Run the Rust schema migration transaction and return the committed version.
#[pyfunction]
pub fn sqlite_migrate_schema(db_path: &str) -> PyResult<i64> {
    if db_path.is_empty() {
        return Err(PyValueError::new_err("db_path 不能为空"));
    }
    if let Some(parent) = Path::new(db_path).parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)
                .map_err(|error| PyIOError::new_err(format!("创建数据库目录失败: {error}")))?;
        }
    }
    let conn = Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_CREATE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| PyIOError::new_err(format!("打开数据库失败: {error}")))?;
    migrate_connection(&conn)
        .map_err(|error| PyIOError::new_err(format!("Rust schema migration failed: {error}")))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sqlite_query_schema_version, m)?)?;
    m.add_function(wrap_pyfunction!(sqlite_migrate_schema, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fresh_database_is_created_at_v53() {
        let conn = Connection::open_in_memory().unwrap();
        assert_eq!(migrate_connection(&conn).unwrap(), RUST_SCHEMA_VERSION);
        assert_eq!(current_schema_version(&conn).unwrap(), RUST_SCHEMA_VERSION);
        let workspaces: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'workspaces'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let rollback_config: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'rollback_config'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(workspaces, 1);
        assert_eq!(rollback_config, 1);
        // v52：三张表存在且 operation_params 初始 rule row 已播种
        for table in [
            "canonicalization_rule_sets",
            "canonicalization_rule_revocations",
            "task_operation_ledger",
        ] {
            let present: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name = ?1",
                    [table],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(present, 1, "missing table: {table}");
        }
        let rule_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM canonicalization_rule_sets \
                 WHERE domain = 'operation_params' AND canonicalization_version = 'operation-params-c14n/v1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(rule_count, 1, "operation_params seed row missing");
        // v53（1A）：两表存在且 workspace_capture 初始 rule row 已播种
        for table in ["workspace_authority_captures", "task_workspace_bindings"] {
            let present: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name = ?1",
                    [table],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(present, 1, "missing table: {table}");
        }
        let capture_rule_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM canonicalization_rule_sets \
                 WHERE domain = 'workspace_capture' AND canonicalization_version = 'workspace-capture-c14n/v1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(capture_rule_count, 1, "workspace_capture seed row missing");
        // v54（1D3B）：promotion 权威账本表存在
        let promotion_table: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'task_loop_capability_promotion_events'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(promotion_table, 1, "promotion events table missing");
    }

    #[test]
    fn migration_is_idempotent_after_v53() {
        let conn = Connection::open_in_memory().unwrap();
        assert_eq!(migrate_connection(&conn).unwrap(), RUST_SCHEMA_VERSION);
        assert_eq!(migrate_connection(&conn).unwrap(), RUST_SCHEMA_VERSION);
        assert_eq!(current_schema_version(&conn).unwrap(), RUST_SCHEMA_VERSION);
        // 二次迁移不得重复播种
        let rule_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM canonicalization_rule_sets \
                 WHERE domain = 'operation_params' AND canonicalization_version = 'operation-params-c14n/v1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(rule_count, 1);
        let capture_rule_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM canonicalization_rule_sets \
                 WHERE domain = 'workspace_capture' AND canonicalization_version = 'workspace-capture-c14n/v1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(capture_rule_count, 1);
        // v55（1B）：role_contract 初始 rule row 已播种
        let role_contract_rule_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM canonicalization_rule_sets \
                 WHERE domain = 'role_contract' AND canonicalization_version = 'role-contract-c14n/v1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(role_contract_rule_count, 1, "role_contract seed row missing");
    }

    #[test]
    fn fresh_database_has_role_contract_lineage_tables() {
        let conn = Connection::open_in_memory().unwrap();
        assert_eq!(migrate_connection(&conn).unwrap(), RUST_SCHEMA_VERSION);
        for table in ["role_contract_lineages", "role_contract_revisions"] {
            let present: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name = ?1",
                    [table],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(present, 1, "missing table: {table}");
        }
    }

    // v57（1E）：fresh DB 具备 verdict_normalization registry + provenance 列；
    // 历史无绑定记录（缺 provenance / normalization 绑定）保持 UNVERIFIED，不回填改写。
    #[test]
    fn fresh_database_has_verdict_normalization_schema() {
        let conn = Connection::open_in_memory().unwrap();
        assert_eq!(migrate_connection(&conn).unwrap(), RUST_SCHEMA_VERSION);
        for table in [
            "verdict_normalization_rules",
            "verdict_normalization_rule_revocations",
        ] {
            let present: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name = ?1",
                    [table],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(present, 1, "missing table: {table}");
        }
        // verdict-normalization/v1 初始 rule row 已播种（1E 独占）。
        let (payload, rules_hash, c14n_version): (String, String, String) = conn
            .query_row(
                "SELECT rules_payload_json, rules_hash, rules_c14n_version \
                 FROM verdict_normalization_rules WHERE normalization_version = 'verdict-normalization/v1'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(c14n_version, "rules-c14n/v1");
        // payload 重算 hash 必须一致（承诺可复算）。
        assert_eq!(rules_hash, rules_c14n_hash_v53(&payload));
        // rules_payload_json 是合法 canonical JSON 且含 overall/phase 映射。
        let value: serde_json::Value = serde_json::from_str(&payload).unwrap();
        assert_eq!(value["overall_map"]["approved"], "pass");
        assert_eq!(value["overall_map"]["rejected"], "block");
        assert_eq!(value["overall_map"]["abstain"], "UNVERIFIED");
        assert_eq!(value["phase_map"]["PRE_VERDICT"], "blind_first_pass");
        // 撤销表初始为空，且 UNIQUE(verdict_rule_set_id) 防重复撤销。
        let revocations: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM verdict_normalization_rule_revocations",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(revocations, 0);
    }

    #[test]
    fn supersede_v59_compat_backfills_columns_on_stale_versioned_db() {
        // 模拟"版本已被抢先打标为 59 但 supersede 基础表仍缺 v59 列"的既有库
        // （CREATE TABLE IF NOT EXISTS 不 ALTER 既有表；短路路径必须自愈补列，
        // 否则 validate_supersede_schema 会 fail-closed 拒绝服务）。
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, description TEXT DEFAULT '');
             INSERT INTO schema_version (version, applied_at, description) VALUES (59, 1.0, 'stale 59 stamp');
             CREATE TABLE task_supersede_relations (
                 superseded_task_id TEXT NOT NULL,
                 superseding_task_id TEXT NOT NULL,
                 reason TEXT DEFAULT '',
                 actor TEXT NOT NULL,
                 created_at REAL NOT NULL,
                 PRIMARY KEY (superseded_task_id, superseding_task_id)
             );
             CREATE TABLE task_supersede_events (
                 event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                 superseded_task_id TEXT NOT NULL,
                 superseding_task_id TEXT NOT NULL,
                 reason TEXT DEFAULT '',
                 actor TEXT NOT NULL,
                 monotonic_seq INTEGER NOT NULL,
                 authoritative_timestamp REAL NOT NULL
             );
             INSERT INTO task_supersede_relations
                 (superseded_task_id, superseding_task_id, reason, actor, created_at)
                 VALUES ('OLD','NEW','legacy','implementer-workbuddy-v1', 1.0);
            ",
        )
        .unwrap();
        // migrate_connection 短路路径（current==59）必须补列后返回，不抛错
        assert_eq!(migrate_connection(&conn).unwrap(), RUST_SCHEMA_VERSION);
        let cols: Vec<String> = {
            let mut stmt = conn
                .prepare("PRAGMA table_info(task_supersede_relations)")
                .unwrap();
            stmt.query_map([], |r| r.get::<_, String>(1))
                .unwrap()
                .collect::<Result<Vec<_>, _>>()
                .unwrap()
        };
        assert!(cols.contains(&"workspace_id".to_string()), "relations 缺 workspace_id");
        assert!(cols.contains(&"supersedence_id".to_string()));
        assert!(cols.contains(&"evidence_hash".to_string()));
        // 历史行保留（无损 ALTER）
        let (old, new, ws): (String, String, i64) = conn
            .query_row(
                "SELECT superseded_task_id, superseding_task_id, workspace_id \
                 FROM task_supersede_relations",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .unwrap();
        assert_eq!((old.as_str(), new.as_str(), ws), ("OLD", "NEW", 0));
    }

    #[test]
    fn legacy_verdict_gate_contract_unbound_is_untouched() {
        let conn = Connection::open_in_memory().unwrap();
        assert_eq!(migrate_connection(&conn).unwrap(), RUST_SCHEMA_VERSION);
        // 写入一条 legacy Task Contract revision（无 normalization 绑定）与一条
        // legacy verdict（Role Contract provenance 列全空），模拟 v57 之前的既有数据。
        conn.execute(
            "INSERT INTO task_contract_revisions
             (contract_id, revision, contract_hash, profile, task_id, workspace_id,
              envelope_payload, created_at, created_by)
             VALUES ('tc-legacy', 1, 'sha256:tc', 'review', 't1', 1, '{}', 0.0, 'legacy')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO task_verdict_events
             (verdict_id, task_id, contract_id, contract_revision, contract_hash,
              phase, overall, submitted_at)
             VALUES ('V-legacy', 't1', 'tc-legacy', 1, 'sha256:tc',
                     'blind_first_pass', 'request_changes', 0.0)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO task_gate_decisions
             (decision_id, task_id, contract_id, contract_revision, contract_hash,
              decision, decision_time)
             VALUES ('D-legacy', 't1', 'tc-legacy', 1, 'sha256:tc', 'pass', 0.0)",
            [],
        )
        .unwrap();
        // 迁移幂等重跑不改变既有行（append-only，不回填改写历史）。
        assert_eq!(migrate_connection(&conn).unwrap(), RUST_SCHEMA_VERSION);
        let verdict_rcr: i64 = conn
            .query_row(
                "SELECT role_contract_revision FROM task_verdict_events WHERE verdict_id = 'V-legacy'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        // v1 新列保持缺省（0 / 空），由读投影派生 UNVERIFIED；绝不回填。
        assert_eq!(verdict_rcr, 0);
        let normalize_verdict: String = conn
            .query_row(
                "SELECT normalization_version FROM task_verdict_events WHERE verdict_id = 'V-legacy'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(normalize_verdict.is_empty(), "legacy verdict 不得回填 normalization 绑定");
        let gate_rcr: i64 = conn
            .query_row(
                "SELECT role_contract_revision FROM task_gate_decisions WHERE decision_id = 'D-legacy'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(gate_rcr, 0);
        // task_verdict_events.gate 无绑定 → 读投影保持 UNVERIFIED（此处验证列已补且未改写）。
        let overall: String = conn
            .query_row(
                "SELECT overall FROM task_verdict_events WHERE verdict_id = 'V-legacy'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(overall, "request_changes", "历史 overall payload 不得改写");
    }

    /// 构造 legacy 场景：旧 role_contracts 行 + task_workspace_bindings（migration 前）。
    /// 返回 (conn, task_id, role)。插入后调用方可执行 migrate_connection 再断言。
    fn legacy_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE task_workspace_bindings (
                task_id TEXT PRIMARY KEY,
                workspace_id INTEGER NOT NULL,
                workspace_binding_id TEXT NOT NULL UNIQUE,
                workspace_capture_id TEXT NOT NULL,
                created_by TEXT NOT NULL,
                authoritative_created_at TEXT NOT NULL,
                UNIQUE(task_id, workspace_id)
            );
            CREATE TABLE role_contracts (
                contract_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                step_id TEXT DEFAULT '',
                role TEXT NOT NULL,
                skill_id TEXT DEFAULT '',
                skill_version TEXT DEFAULT '',
                prompt_template_id TEXT DEFAULT '',
                prompt_hash TEXT DEFAULT '',
                allowed_paths TEXT DEFAULT '[]',
                forbidden_paths TEXT DEFAULT '[]',
                commands TEXT DEFAULT '[]',
                acceptance_checks TEXT DEFAULT '[]',
                required_evidence TEXT DEFAULT '[]',
                handoff_to TEXT DEFAULT '',
                independence TEXT DEFAULT '{}',
                revision INTEGER DEFAULT 1,
                is_current INTEGER DEFAULT 1,
                created_at REAL NOT NULL,
                created_by TEXT DEFAULT ''
            );
            INSERT INTO task_workspace_bindings
                (task_id, workspace_id, workspace_binding_id, workspace_capture_id,
                 created_by, authoritative_created_at)
            VALUES ('t1', 1, 'wb-1', 'wc-1', 'test', '2026-01-01T00:00:00.000000Z');",
        )
        .unwrap();
        conn
    }

    fn insert_legacy_row(
        conn: &Connection,
        task_id: &str,
        role: &str,
        revision: i64,
        step_id: &str,
        created_at: f64,
    ) {
        conn.execute(
            "INSERT INTO role_contracts
             (contract_id, task_id, step_id, role, skill_id, skill_version,
              prompt_template_id, prompt_hash, allowed_paths, forbidden_paths,
              commands, acceptance_checks, required_evidence, handoff_to,
              independence, revision, is_current, created_at, created_by)
             VALUES (?1, ?2, ?3, ?4, 'skill-1', '1.0', 'pt-1', 'ph-1',
                     '[\"src/\", \"docs/\"]', '[\"target/\"]',
                     '[\"echo\"]', '[\"pass\"]', '[\"log\"]', 'human',
                     '{\"max_tokens\": 100}', ?5, 1, ?6, 'legacy-user')",
            rusqlite::params![
                format!("{task_id}-{role}-r{revision}"),
                task_id,
                step_id,
                role,
                revision,
                created_at
            ],
        )
        .unwrap();
    }

    #[test]
    fn legacy_migration_backfills_lineage_and_revisions() {
        let conn = legacy_db();
        insert_legacy_row(&conn, "t1", "coder", 1, "step-1", 1000.0);
        insert_legacy_row(&conn, "t1", "coder", 2, "step-2", 1001.0);
        insert_legacy_row(&conn, "t1", "reviewer", 1, "step-3", 2000.0);

        assert_eq!(migrate_connection(&conn).unwrap(), RUST_SCHEMA_VERSION);

        let lineage_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM role_contract_lineages", [], |r| r.get(0))
            .unwrap();
        assert_eq!(lineage_count, 2, "期望 2 个 lineage（coder/reviewer）");

        // coder lineage：revision 1 supersedes=NULL；revision 2 指向 revision 1。
        let revisions: Vec<(i64, Option<String>, String, String)> = conn
            .prepare(
                "SELECT revision, supersedes_revision_id, canonicalization_version, role_contract_hash \
                 FROM role_contract_revisions \
                 WHERE role_contract_lineage_id = 'rcl-t1-coder' ORDER BY revision ASC",
            )
            .unwrap()
            .query_map([], |row| {
                Ok((
                    row.get(0)?,
                    row.get::<_, Option<String>>(1)?,
                    row.get(2)?,
                    row.get(3)?,
                ))
            })
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(revisions.len(), 2);
        assert_eq!(revisions[0].0, 1);
        assert_eq!(revisions[0].1, None, "revision 1 的 supersedes 必须为 NULL");
        assert_eq!(revisions[0].2, ROLE_CONTRACT_C14N_VERSION);
        assert!(revisions[0].3.starts_with("sha256:"));
        assert_eq!(revisions[1].0, 2);
        assert_eq!(
            revisions[1].1.as_deref(),
            Some("rcr-t1-coder-r1"),
            "revision 2 必须指向同 lineage 的 revision 1"
        );
        // hash 确定性：两次读取一致（revision 行不可变）。
        let hash1: String = conn
            .query_row(
                "SELECT role_contract_hash FROM role_contract_revisions \
                 WHERE role_contract_revision_id = 'rcr-t1-coder-r1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let hash1_again: String = conn
            .query_row(
                "SELECT role_contract_hash FROM role_contract_revisions \
                 WHERE role_contract_revision_id = 'rcr-t1-coder-r1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(hash1, hash1_again);
        // lineage 携带 workspace binding 归属（唯一 binding → 精确 workspace）。
        let (task_id, workspace_id, role): (String, i64, String) = conn
            .query_row(
                "SELECT task_id, workspace_id, role FROM role_contract_lineages \
                 WHERE role_contract_lineage_id = 'rcl-t1-coder'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!((task_id.as_str(), workspace_id, role.as_str()), ("t1", 1, "coder"));
    }

    #[test]
    fn legacy_migration_skips_ambiguous_groups() {
        let conn = legacy_db();
        // 歧义 1：空 step_id → 整组跳过（revision 1 有值、2 为空）。
        insert_legacy_row(&conn, "t1", "skipped-empty-step", 1, "", 1000.0);
        insert_legacy_row(&conn, "t1", "skipped-empty-step", 2, "step-2", 1001.0);
        // 歧义 2：revision 链不连续（1、3 缺 2）→ 跳过。
        insert_legacy_row(&conn, "t1", "skipped-gap", 1, "step-1", 1000.0);
        insert_legacy_row(&conn, "t1", "skipped-gap", 3, "step-3", 1002.0);
        // 歧义 3：绝对路径 → payload 不可解析 → 跳过。
        conn.execute(
            "INSERT INTO role_contracts
             (contract_id, task_id, step_id, role, allowed_paths, revision, created_at)
             VALUES ('skip-abs', 't1', 'step-1', 'skipped-abs-path',
                     '[\"/etc\"]', 1, 3000.0)",
            [],
        )
        .unwrap();

        assert_eq!(migrate_connection(&conn).unwrap(), RUST_SCHEMA_VERSION);

        let lineage_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM role_contract_lineages", [], |r| r.get(0))
            .unwrap();
        assert_eq!(lineage_count, 0, "歧义组一律不回填");
        // 旧行保留（迁移绝不 UPDATE/DELETE 历史行）。
        let legacy_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM role_contracts", [], |r| r.get(0))
            .unwrap();
        assert_eq!(legacy_count, 5);
        // role_contract rule row 在跳过歧义组后仍正常播种。
        let rule_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM canonicalization_rule_sets \
                 WHERE domain = 'role_contract' AND canonicalization_version = 'role-contract-c14n/v1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(rule_count, 1);
    }

    #[test]
    fn legacy_migration_skips_unbound_task() {
        let conn = legacy_db();
        // t2 无 task_workspace_bindings 行 → 无法确定 workspace → 跳过。
        insert_legacy_row(&conn, "t2", "coder", 1, "step-1", 1000.0);
        assert_eq!(migrate_connection(&conn).unwrap(), RUST_SCHEMA_VERSION);
        let lineage_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM role_contract_lineages", [], |r| r.get(0))
            .unwrap();
        assert_eq!(lineage_count, 0);
    }

    #[test]
    fn seed_rejects_mismatched_rule_row() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS canonicalization_rule_sets (
                rule_set_id TEXT PRIMARY KEY,
                domain TEXT NOT NULL CHECK(domain IN ('workspace_capture', 'role_contract', 'operation_params')),
                canonicalization_version TEXT NOT NULL,
                rules_payload_json TEXT NOT NULL,
                rules_c14n_version TEXT NOT NULL,
                rules_hash TEXT NOT NULL,
                created_by TEXT NOT NULL,
                authoritative_created_at TEXT NOT NULL,
                UNIQUE(domain, canonicalization_version),
                UNIQUE(domain, rules_hash)
            )",
        )
        .unwrap();
        // 预置一个 hash 不一致的 rule row，migrate_connection 必须 fail closed
        conn.execute(
            "INSERT INTO canonicalization_rule_sets \
             (rule_set_id, domain, canonicalization_version, rules_payload_json, \
              rules_c14n_version, rules_hash, created_by, authoritative_created_at) \
             VALUES ('operation-params-c14n/v1', 'operation_params', 'operation-params-c14n/v1', \
                     '{\"tampered\":true}', 'rules-c14n/v1', 'sha256:deadbeef', 'test', 't')",
            [],
        )
        .unwrap();
        let error = migrate_connection(&conn).unwrap_err();
        assert!(
            error.contains("rule row mismatch"),
            "expected fail-closed mismatch, got: {error}"
        );
    }

    #[test]
    fn seed_rejects_mismatched_verdict_normalization_rule_row() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS verdict_normalization_rules (
                verdict_rule_set_id TEXT PRIMARY KEY,
                normalization_version TEXT NOT NULL UNIQUE,
                rules_payload_json TEXT NOT NULL,
                rules_c14n_version TEXT NOT NULL,
                rules_hash TEXT NOT NULL UNIQUE,
                created_by TEXT NOT NULL,
                authoritative_created_at TEXT NOT NULL
            )",
        )
        .unwrap();
        // 预置一个 hash 不一致的 verdict-normalization rule row，migrate 必须 fail closed。
        conn.execute(
            "INSERT INTO verdict_normalization_rules \
             (verdict_rule_set_id, normalization_version, rules_payload_json, \
              rules_c14n_version, rules_hash, created_by, authoritative_created_at) \
             VALUES ('verdict-normalization/v1', 'verdict-normalization/v1', \
                     '{\"tampered\":true}', 'rules-c14n/v1', 'sha256:deadbeef', 'test', 't')",
            [],
        )
        .unwrap();
        let error = migrate_connection(&conn).unwrap_err();
        assert!(
            error.contains("verdict-normalization/v1 rule row mismatch"),
            "expected fail-closed mismatch, got: {error}"
        );
    }
}
