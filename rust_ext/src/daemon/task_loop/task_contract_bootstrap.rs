//! P0-C: 为已绑定但缺失 v1 governance projection 的历史任务追加初始合同。
//!
//! 该模块严格只允许「完全缺失」的 bootstrap：一旦已存在任一 Task Contract、
//! Role Contract lineage/revision 或 step binding，就拒绝而非更新、拼接或回填。
//! 成功路径在调用者持有的同一 SQLite transaction 内追加 Task Envelope revision 1、
//! executor/reviewer/adjudicator 三条 Role Contract lineage/revision 1 和所有
//! pending/in_progress step 的 executor binding。

use rusqlite::{params, OptionalExtension, Transaction};
use serde_json::Value;
use sha2::{Digest, Sha256};

use super::contract_set::ROLE_CONTRACT_C14N_VERSION;
use crate::daemon::dispatch::DaemonRpcError;

pub const ERR_BOOTSTRAP_INVALID: &str = "E_TASK_CONTRACT_BOOTSTRAP_INVALID";
pub const ERR_BOOTSTRAP_NOT_EMPTY: &str = "E_TASK_CONTRACT_BOOTSTRAP_NOT_EMPTY";
pub const ERR_BOOTSTRAP_ROLE_SOURCE: &str = "E_TASK_CONTRACT_BOOTSTRAP_ROLE_SOURCE";

/// 受权历史任务 allowlist（role_contracts=0 治理清洗）。
///
/// 这些任务三层治理数据（workspace binding / Task Contract / Role Contract /
/// step binding）全部缺失，且无 legacy role_contracts 可派生。allowlist 模式
/// 允许以默认三角色模板建立 Role Contract（payload 标注
/// `source_provenance=allowlist_default_template:v1`，空字段=未证明，不伪造）。
/// 任何非 allowlist 任务保持 governance_blocked（ERR_BOOTSTRAP_ROLE_SOURCE）。
/// 仅由用户/治理明确授权；不得由客户端参数扩展。
pub const BOOTSTRAP_ROLE_ALLOWLIST: &[&str] = &["T-1787203937193-0993d120"];

#[derive(Debug)]
pub struct BootstrapInput {
    pub task_id: String,
    pub envelope: Value,
    pub created_by: String,
    /// "legacy"（默认，要求任务已有 current legacy role_contracts）或
    /// "allowlist"（任务必须在 BOOTSTRAP_ROLE_ALLOWLIST 中，用默认模板派生）。
    pub role_contract_source: String,
}

fn deterministic(code: &str, message: impl Into<String>) -> DaemonRpcError {
    DaemonRpcError::new(code, message.into())
}

fn c14n(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = serde_json::Map::new();
            let mut keys: Vec<String> = map.keys().cloned().collect();
            keys.sort();
            for key in keys {
                out.insert(key.clone(), c14n(map.get(&key).unwrap()));
            }
            Value::Object(out)
        }
        Value::Array(items) => {
            let mut normalized: Vec<Value> = items.iter().map(c14n).collect();
            normalized.sort_by(|a, b| {
                serde_json::to_string(a).unwrap_or_default()
                    .cmp(&serde_json::to_string(b).unwrap_or_default())
            });
            Value::Array(normalized)
        }
        Value::String(s) => Value::String(s.replace('\\', "/")),
        other => other.clone(),
    }
}

fn hash_value(value: &Value) -> Result<String, DaemonRpcError> {
    let bytes = serde_json::to_vec(&c14n(value))
        .map_err(|e| DaemonRpcError::internal_error(format!("contract canonical json 序列化失败: {e}")))?;
    Ok(format!("sha256:{}", hex::encode(Sha256::digest(bytes))))
}

fn parse_string_list(raw: &str, field: &str) -> Result<Vec<String>, DaemonRpcError> {
    let trimmed = raw.trim();
    if trimmed.is_empty() { return Ok(Vec::new()); }
    let values = if trimmed.starts_with('[') {
        let parsed: Value = serde_json::from_str(trimmed).map_err(|_| {
            deterministic(ERR_BOOTSTRAP_ROLE_SOURCE, format!("legacy role contract {field} 不是合法 JSON string array"))
        })?;
        parsed.as_array().ok_or_else(|| deterministic(ERR_BOOTSTRAP_ROLE_SOURCE, format!("legacy role contract {field} 必须是 string array")))?
            .iter().map(|v| v.as_str().map(|s| s.to_string()).ok_or_else(|| deterministic(ERR_BOOTSTRAP_ROLE_SOURCE, format!("legacy role contract {field} 元素必须为 string"))))
            .collect::<Result<Vec<_>, _>>()?
    } else {
        vec![trimmed.to_string()]
    };
    for item in &values {
        if item.contains("..") || item.starts_with('/') || item.starts_with('\\') {
            return Err(deterministic(ERR_BOOTSTRAP_ROLE_SOURCE, format!("legacy role contract {field} 包含非法路径语义")));
        }
    }
    let mut unique = values;
    unique.sort();
    unique.dedup();
    Ok(unique)
}

fn role_rules_hash(tx: &Transaction<'_>) -> Result<String, DaemonRpcError> {
    tx.query_row(
        "SELECT rules_hash FROM canonicalization_rule_sets WHERE domain='role_contract' AND canonicalization_version=?1",
        [ROLE_CONTRACT_C14N_VERSION], |row| row.get(0),
    ).map_err(|e| deterministic(ERR_BOOTSTRAP_INVALID, format!("role contract canonicalization rule 不可用: {e}")))
}

fn normalization_rules(tx: &Transaction<'_>) -> Result<(String, String), DaemonRpcError> {
    tx.query_row(
        "SELECT r.normalization_version, r.rules_hash \
         FROM verdict_normalization_rules r \
         LEFT JOIN verdict_normalization_rule_revocations v ON v.verdict_rule_set_id=r.verdict_rule_set_id \
         WHERE v.verdict_rule_set_id IS NULL \
         ORDER BY r.authoritative_created_at DESC LIMIT 1",
        [], |row| Ok((row.get(0)?, row.get(1)?)),
    ).map_err(|e| deterministic(ERR_BOOTSTRAP_INVALID, format!("verdict normalization rule 不可用: {e}")))
}

fn task_contract_payload(input: &BootstrapInput) -> Result<(String, String, String, String), DaemonRpcError> {
    let map = input.envelope.as_object().ok_or_else(|| deterministic(ERR_BOOTSTRAP_INVALID, "envelope 必须是 JSON object"))?;
    let contract_id = map.get("contract_id").and_then(|v| v.as_str()).unwrap_or("").trim();
    let profile = map.get("profile").and_then(|v| v.as_str()).unwrap_or("").trim();
    let revision = map.get("revision").and_then(|v| v.as_i64()).unwrap_or(0);
    if contract_id.is_empty() || revision != 1 || !matches!(profile, "research"|"design"|"code_change"|"high_risk"|"review") {
        return Err(deterministic(ERR_BOOTSTRAP_INVALID, "envelope 必须含非空 contract_id、合法 profile 且 revision=1"));
    }
    for key in ["objective", "interfaces", "allowed_edit_scope", "acceptance_clauses", "risks", "rollback", "dependencies"] {
        if !map.contains_key(key) {
            return Err(deterministic(ERR_BOOTSTRAP_INVALID, format!("envelope 缺少字段: {key}")));
        }
    }
    let mut canonical = input.envelope.clone();
    let object = canonical.as_object_mut().unwrap();
    object.remove("contract_hash");
    object.remove("created_at");
    object.remove("created_by");
    let hash = hash_value(&canonical)?;
    let payload = c14n(&canonical);
    let payload_json = serde_json::to_string(&payload)
        .map_err(|e| DaemonRpcError::internal_error(format!("Task Envelope 序列化失败: {e}")))?;
    Ok((contract_id.to_string(), profile.to_string(), hash, payload_json))
}

fn no_governance_projection(tx: &Transaction<'_>, task_id: &str) -> Result<(), DaemonRpcError> {
    for (table, sql) in [
        ("task_contract_revisions", "SELECT EXISTS(SELECT 1 FROM task_contract_revisions WHERE task_id=?1)"),
        ("role_contract_lineages", "SELECT EXISTS(SELECT 1 FROM role_contract_lineages WHERE task_id=?1)"),
        ("role_contract_revisions", "SELECT EXISTS(SELECT 1 FROM role_contract_revisions r JOIN role_contract_lineages l ON l.role_contract_lineage_id=r.role_contract_lineage_id WHERE l.task_id=?1)"),
        ("task_step_role_contract_bindings", "SELECT EXISTS(SELECT 1 FROM task_step_role_contract_bindings WHERE task_id=?1)"),
    ] {
        let exists: bool = tx.query_row(sql, [task_id], |row| row.get(0))
            .map_err(|e| DaemonRpcError::internal_error(format!("{table} projection 查询失败: {e}")))?;
        if exists { return Err(deterministic(ERR_BOOTSTRAP_NOT_EMPTY, format!("task {task_id} 已存在 {table}，bootstrap 只允许完全缺失的任务"))); }
    }
    Ok(())
}

/// 在调用方已经完成 authority / identity / reviewer lease / evidence 预检的 transaction 中执行。
pub fn bootstrap_task_governance_contracts(
    tx: &Transaction<'_>,
    input: &BootstrapInput,
    workspace_id: i64,
) -> Result<Value, DaemonRpcError> {
    no_governance_projection(tx, &input.task_id)?;
    let task_exists: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM tasks WHERE id=?1)", [&input.task_id], |r| r.get(0))
        .map_err(|e| DaemonRpcError::internal_error(format!("task existence 查询失败: {e}")))?;
    if !task_exists { return Err(deterministic(ERR_BOOTSTRAP_INVALID, format!("task {} 不存在", input.task_id))); }
    let (contract_id, profile, contract_hash, envelope_payload) = task_contract_payload(input)?;
    let (normalization_version, normalization_rules_hash) = normalization_rules(tx)?;
    tx.execute(
        "INSERT INTO task_contract_revisions (contract_id,revision,contract_hash,profile,task_id,workspace_id,envelope_payload,created_at,created_by,normalization_version,normalization_rules_hash) VALUES (?1,1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
        params![contract_id, contract_hash, profile, input.task_id, workspace_id, envelope_payload, crate::daemon::task_collab::task_now_ts(), input.created_by, normalization_version, normalization_rules_hash],
    ).map_err(|e| DaemonRpcError::internal_error(format!("Task Contract revision 写入失败: {e}")))?;

    let rules_hash = role_rules_hash(tx)?;
    let mut executor_revision_id = String::new();
    let mut role_result = Vec::new();
    let allowlisted = input.role_contract_source == "allowlist"
        && BOOTSTRAP_ROLE_ALLOWLIST.contains(&input.task_id.as_str());
    for role in ["executor", "reviewer", "adjudicator"] {
        let legacy: Option<(String,String,String,String,String,String,String,String,String,String,String)> = tx.query_row(
            "SELECT skill_id,skill_version,prompt_template_id,prompt_hash,allowed_paths,forbidden_paths,commands,acceptance_checks,required_evidence,handoff_to,independence FROM role_contracts WHERE task_id=?1 AND role=?2 AND is_current=1 ORDER BY revision DESC LIMIT 1",
            params![input.task_id, role], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?,r.get(8)?,r.get(9)?,r.get(10)?)),
        ).optional().map_err(|e| DaemonRpcError::internal_error(format!("legacy role contract 读取失败: {e}")))?;
        // allowlist 模式：role_contracts=0 的历史任务用默认三角色模板派生
        // （空字段=未证明，payload 标注 source_provenance，不伪造历史证据）。
        let (skill_id,skill_version,prompt_template_id,prompt_hash,allowed_paths,forbidden_paths,commands,acceptance_checks,required_evidence,handoff_to,independence) = match legacy {
            Some(v) => v,
            None if allowlisted => (
                String::new(), String::new(), String::new(), String::new(),
                String::new(), String::new(), String::new(), String::new(),
                String::new(), String::new(), String::new(),
            ),
            None => return Err(deterministic(ERR_BOOTSTRAP_ROLE_SOURCE, format!("task 缺少 current legacy role contract: {role}"))),
        };
        let mut payload = serde_json::json!({
            "role": role, "skill_id": skill_id, "skill_version": skill_version,
            "prompt_template_id": prompt_template_id, "prompt_hash": prompt_hash,
            "allowed_paths": parse_string_list(&allowed_paths, "allowed_paths")?,
            "forbidden_paths": parse_string_list(&forbidden_paths, "forbidden_paths")?,
            "commands": parse_string_list(&commands, "commands")?,
            "acceptance_checks": parse_string_list(&acceptance_checks, "acceptance_checks")?,
            "required_evidence": parse_string_list(&required_evidence, "required_evidence")?,
            "handoff_to": handoff_to,
            "independence": serde_json::from_str::<Value>(&independence).unwrap_or_else(|_| serde_json::json!({"mode": independence})),
        });
        if allowlisted {
            // 可审计来源标注：allowlist 默认模板派生，非历史证据。
            payload["source_provenance"] = serde_json::json!("allowlist_default_template:v1");
        }
        let canonical_payload = c14n(&payload);
        let role_hash = hash_value(&canonical_payload)?;
        let lineage_id = format!("rcl-{}-{}", input.task_id, role);
        let revision_id = format!("rcr-{}-{}-r1", input.task_id, role);
        tx.execute("INSERT INTO role_contract_lineages (role_contract_lineage_id,task_id,workspace_id,role,created_by,authoritative_created_at) VALUES (?1,?2,?3,?4,?5,?6)",
            params![lineage_id,input.task_id,workspace_id,role,input.created_by,crate::daemon::task_collab::task_now_ts()])
            .map_err(|e| DaemonRpcError::internal_error(format!("Role Contract lineage 写入失败: {e}")))?;
        tx.execute("INSERT INTO role_contract_revisions (role_contract_revision_id,role_contract_lineage_id,revision,supersedes_revision_id,canonical_payload_json,canonicalization_version,canonicalization_rules_hash,role_contract_hash,created_by,authoritative_created_at) VALUES (?1,?2,1,NULL,?3,?4,?5,?6,?7,?8)",
            params![revision_id,lineage_id,serde_json::to_string(&canonical_payload).unwrap_or_default(),ROLE_CONTRACT_C14N_VERSION,rules_hash,role_hash,input.created_by,crate::daemon::task_collab::task_now_ts()])
            .map_err(|e| DaemonRpcError::internal_error(format!("Role Contract revision 写入失败: {e}")))?;
        if role == "executor" { executor_revision_id = revision_id.clone(); }
        role_result.push(serde_json::json!({"role":role,"lineage_id":lineage_id,"revision_id":revision_id,"hash":role_hash}));
    }

    let (executor_lineage_id, executor_hash): (String,String) = tx.query_row(
        "SELECT l.role_contract_lineage_id,r.role_contract_hash FROM role_contract_lineages l JOIN role_contract_revisions r ON r.role_contract_lineage_id=l.role_contract_lineage_id WHERE l.task_id=?1 AND l.workspace_id=?2 AND l.role='executor' AND r.revision=1",
        params![input.task_id,workspace_id], |r| Ok((r.get(0)?,r.get(1)?)),
    ).map_err(|e| DaemonRpcError::internal_error(format!("executor bootstrap revision 查询失败: {e}")))?;
    let mut stmt = tx.prepare("SELECT id FROM task_steps WHERE task_id=?1 AND status IN ('pending','in_progress') ORDER BY step_index")
        .map_err(|e| DaemonRpcError::internal_error(format!("task step 查询失败: {e}")))?;
    let steps: Vec<String> = stmt.query_map([&input.task_id], |r| r.get(0))
        .map_err(|e| DaemonRpcError::internal_error(format!("task step 遍历失败: {e}")))?
        .collect::<Result<Vec<_>,_>>().map_err(|e| DaemonRpcError::internal_error(format!("task step 读取失败: {e}")))?;
    let mut binding_ids = Vec::new();
    for step_id in steps {
        let binding_id = format!("sb-{}-{}-r1", input.task_id, step_id);
        tx.execute("INSERT INTO task_step_role_contract_bindings (binding_id,workspace_id,task_id,step_id,role_contract_lineage_id,role_contract_revision_id,role_contract_revision,role_contract_hash,canonicalization_version,canonicalization_rules_hash,binding_revision,supersedes_binding_id,created_by,authoritative_created_at) VALUES (?1,?2,?3,?4,?5,?6,1,?7,?8,?9,1,NULL,?10,?11)",
            params![binding_id,workspace_id,input.task_id,step_id,executor_lineage_id,executor_revision_id,executor_hash,ROLE_CONTRACT_C14N_VERSION,rules_hash,input.created_by,crate::daemon::task_collab::task_now_ts()])
            .map_err(|e| DaemonRpcError::internal_error(format!("step Role Contract binding 写入失败: {e}")))?;
        binding_ids.push(binding_id);
    }
    Ok(serde_json::json!({
        "ok": true, "task_id": input.task_id, "workspace_id": workspace_id,
        "contract_id": contract_id, "contract_revision": 1, "contract_hash": contract_hash,
        "role_contracts": role_result, "step_binding_ids": binding_ids,
    }))
}

/// 为新追加的 remediation step 绑定当前 Executor Role Contract。
///
/// remediation 不是普通步骤的旁路类型：它同样必须拥有唯一、可验证的
/// `task_step_role_contract_bindings`。该函数只接受任务自身不可变 workspace binding
/// 和任务自己的 executor lineage/revision；任一治理事实缺失或 binding 链异常都拒绝，
/// 由调用方的外层事务回滚 step、assignment 和状态变更。
pub fn bind_step_to_executor_role_contract(
    tx: &Transaction<'_>,
    task_id: &str,
    step_id: &str,
    created_by: &str,
) -> Result<String, DaemonRpcError> {
    let workspace_id: i64 = tx
        .query_row(
            "SELECT workspace_id FROM task_workspace_bindings WHERE task_id = ?1",
            [task_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("task workspace binding 查询失败: {e}")))?
        .ok_or_else(|| {
            deterministic(
                "E_TASK_STEP_ROLE_CONTRACT_BINDING_REQUIRED",
                format!("task {task_id} 缺少 workspace binding，不能创建 remediation binding"),
            )
        })?;

    let step_owned: i64 = tx
        .query_row(
            "SELECT COUNT(*) FROM task_steps WHERE id = ?1 AND task_id = ?2",
            params![step_id, task_id],
            |row| row.get(0),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("remediation step 归属查询失败: {e}")))?;
    if step_owned != 1 {
        return Err(deterministic(
            "E_TASK_STEP_ROLE_CONTRACT_BINDING_REQUIRED",
            format!("step {step_id} 不属于 task {task_id}，不能创建 remediation binding"),
        ));
    }

    let (lineage_id, revision_id, revision, role_hash, c14n_version, rules_hash): (
        String,
        String,
        i64,
        String,
        String,
        String,
    ) = tx
        .query_row(
            "SELECT l.role_contract_lineage_id, r.role_contract_revision_id, r.revision,
                    r.role_contract_hash, r.canonicalization_version,
                    r.canonicalization_rules_hash
             FROM role_contract_lineages l
             JOIN role_contract_revisions r
               ON r.role_contract_lineage_id = l.role_contract_lineage_id
             WHERE l.task_id = ?1 AND l.workspace_id = ?2 AND l.role = 'executor'
             ORDER BY r.revision DESC LIMIT 1",
            params![task_id, workspace_id],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                ))
            },
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("Executor Role Contract 查询失败: {e}")))?
        .ok_or_else(|| {
            deterministic(
                "E_TASK_STEP_ROLE_CONTRACT_BINDING_REQUIRED",
                format!("task {task_id} 缺少当前 Executor Role Contract，不能创建 remediation binding"),
            )
        })?;

    let (revision_count, max_revision): (i64, i64) = tx
        .query_row(
            "SELECT COUNT(*), COALESCE(MAX(revision), 0)
             FROM role_contract_revisions WHERE role_contract_lineage_id = ?1",
            [&lineage_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("Executor Role Contract revision 链查询失败: {e}")))?;
    if revision_count != max_revision || revision != max_revision {
        return Err(deterministic(
            "E_TASK_STEP_ROLE_CONTRACT_BINDING_REQUIRED",
            format!("task {task_id} 的 Executor Role Contract revision 链不连续，不能创建 remediation binding"),
        ));
    }

    let (binding_count, max_binding): (i64, i64) = tx
        .query_row(
            "SELECT COUNT(*), COALESCE(MAX(binding_revision), 0)
             FROM task_step_role_contract_bindings
             WHERE workspace_id = ?1 AND task_id = ?2 AND step_id = ?3",
            params![workspace_id, task_id, step_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("remediation binding 链查询失败: {e}")))?;
    if binding_count != max_binding {
        return Err(deterministic(
            "E_TASK_STEP_ROLE_CONTRACT_BINDING_REQUIRED",
            format!("step {step_id} 的 Role Contract binding 链不连续，拒绝追加"),
        ));
    }
    if max_binding > 0 {
        let (binding_id, bound_revision_id, bound_hash): (String, String, String) = tx
            .query_row(
                "SELECT binding_id, role_contract_revision_id, role_contract_hash
                 FROM task_step_role_contract_bindings
                 WHERE workspace_id = ?1 AND task_id = ?2 AND step_id = ?3
                 ORDER BY binding_revision DESC LIMIT 1",
                params![workspace_id, task_id, step_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("current remediation binding 查询失败: {e}")))?;
        if bound_revision_id == revision_id && bound_hash == role_hash {
            return Ok(binding_id);
        }
        return Err(deterministic(
            "E_TASK_STEP_ROLE_CONTRACT_BINDING_REQUIRED",
            format!("step {step_id} 已绑定其他 Executor Role Contract，拒绝覆盖历史 binding"),
        ));
    }

    let binding_id = format!("sb-{task_id}-{step_id}-r1");
    tx.execute(
        "INSERT INTO task_step_role_contract_bindings
         (binding_id, workspace_id, task_id, step_id,
          role_contract_lineage_id, role_contract_revision_id, role_contract_revision,
          role_contract_hash, canonicalization_version, canonicalization_rules_hash,
          binding_revision, supersedes_binding_id, created_by, authoritative_created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, 1, NULL, ?11, ?12)",
        params![
            binding_id,
            workspace_id,
            task_id,
            step_id,
            lineage_id,
            revision_id,
            revision,
            role_hash,
            c14n_version,
            rules_hash,
            created_by,
            crate::daemon::task_collab::task_now_ts(),
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("写入 remediation Role Contract binding 失败: {e}")))?;
    Ok(binding_id)
}
