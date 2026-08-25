//! P0-G：`task.contract_revise` 的 append-only Task Contract revision domain。
//!
//! 该域只追加 `task_contract_revisions` 的 n+1，不更新/删除历史 revision，
//! 并强制新 envelope 显式锚定当前 revision/hash，防止批量机械合同被静默覆盖。

use rusqlite::{params, OptionalExtension, Transaction};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::daemon::dispatch::DaemonRpcError;

pub const ERR_REVISE_INVALID: &str = "E_TASK_CONTRACT_REVISE_INVALID";
pub const ERR_REVISE_NOT_FOUND: &str = "E_TASK_CONTRACT_REVISE_NOT_FOUND";
pub const ERR_REVISE_CONFLICT: &str = "E_TASK_CONTRACT_REVISE_CONFLICT";

#[derive(Debug)]
pub struct ContractReviseInput {
    pub task_id: String,
    pub envelope: Value,
    pub expected_previous_hash: String,
    pub created_by: String,
}

fn reject(message: impl Into<String>) -> DaemonRpcError {
    DaemonRpcError::new(ERR_REVISE_INVALID, message.into())
}

fn c14n(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = serde_json::Map::new();
            let mut keys: Vec<String> = map.keys().cloned().collect();
            keys.sort();
            for key in keys {
                out.insert(key.clone(), c14n(map.get(&key).expect("sorted key exists")));
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(items.iter().map(c14n).collect()),
        Value::String(s) => Value::String(s.replace('\\', "/")),
        other => other.clone(),
    }
}

fn hash_value(value: &Value) -> Result<String, DaemonRpcError> {
    let bytes = serde_json::to_vec(&c14n(value))
        .map_err(|e| DaemonRpcError::internal_error(format!("Task Contract canonical JSON 序列化失败: {e}")))?;
    Ok(format!("sha256:{}", hex::encode(Sha256::digest(bytes))))
}

fn require_string(map: &serde_json::Map<String, Value>, key: &str) -> Result<String, DaemonRpcError> {
    map.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .map(ToOwned::to_owned)
        .ok_or_else(|| reject(format!("revision envelope 缺少非空字符串字段: {key}")))
}

fn require_string_array(map: &serde_json::Map<String, Value>, key: &str) -> Result<(), DaemonRpcError> {
    let array = map.get(key).and_then(Value::as_array)
        .ok_or_else(|| reject(format!("revision envelope.{key} 必须是 JSON array")))?;
    if array.is_empty() {
        return Err(reject(format!("revision envelope.{key} 不得为空")));
    }
    for item in array {
        if item.as_str().map(str::trim).filter(|v| !v.is_empty()).is_none() {
            return Err(reject(format!("revision envelope.{key} 必须仅含非空字符串")));
        }
    }
    Ok(())
}

fn normalization_rules(tx: &Transaction<'_>) -> Result<(String, String), DaemonRpcError> {
    tx.query_row(
        "SELECT r.normalization_version, r.rules_hash \
         FROM verdict_normalization_rules r \
         LEFT JOIN verdict_normalization_rule_revocations v ON v.verdict_rule_set_id=r.verdict_rule_set_id \
         WHERE v.verdict_rule_set_id IS NULL \
         ORDER BY r.authoritative_created_at DESC LIMIT 1",
        [],
        |row| Ok((row.get(0)?, row.get(1)?)),
    ).map_err(|e| DaemonRpcError::new(ERR_REVISE_INVALID, format!("verdict normalization rule 不可用: {e}")))
}

/// 在已经完成 workspace/identity/reviewer lease 预检的同一事务中追加 revision。
pub fn append_task_contract_revision(
    tx: &Transaction<'_>,
    input: &ContractReviseInput,
    workspace_id: i64,
) -> Result<Value, DaemonRpcError> {
    let (contract_id, current_revision, current_hash): (String, i64, String) = tx.query_row(
        "SELECT contract_id, revision, contract_hash FROM task_contract_revisions \
         WHERE task_id=?1 ORDER BY revision DESC LIMIT 1",
        [&input.task_id],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
    ).optional().map_err(|e| DaemonRpcError::internal_error(format!("current Task Contract 查询失败: {e}")))?
        .ok_or_else(|| DaemonRpcError::new(ERR_REVISE_NOT_FOUND, format!("task {} 没有可修订的 Task Contract", input.task_id)))?;

    if input.expected_previous_hash != current_hash {
        return Err(DaemonRpcError::new(
            ERR_REVISE_CONFLICT,
            format!("expected_previous_hash 与当前 contract hash 不一致: expected={}, current={}", input.expected_previous_hash, current_hash),
        ));
    }
    let map = input.envelope.as_object().ok_or_else(|| reject("revision envelope 必须是 JSON object"))?;
    if require_string(map, "contract_id")? != contract_id {
        return Err(DaemonRpcError::new(ERR_REVISE_CONFLICT, "revision envelope.contract_id 不得改变既有 lineage"));
    }
    let revision = map.get("revision").and_then(Value::as_i64)
        .ok_or_else(|| reject("revision envelope.revision 必须是整数"))?;
    if revision != current_revision + 1 {
        return Err(DaemonRpcError::new(ERR_REVISE_CONFLICT, format!("revision 必须为当前 revision+1（expected={}）", current_revision + 1)));
    }
    let supersedes_revision = map.get("supersedes_revision").and_then(Value::as_i64)
        .ok_or_else(|| reject("revision envelope.supersedes_revision 必须是整数"))?;
    if supersedes_revision != current_revision {
        return Err(DaemonRpcError::new(ERR_REVISE_CONFLICT, "supersedes_revision 与当前 revision 不一致"));
    }
    if require_string(map, "supersedes_contract_hash")? != current_hash {
        return Err(DaemonRpcError::new(ERR_REVISE_CONFLICT, "supersedes_contract_hash 与当前 hash 不一致"));
    }
    let profile = require_string(map, "profile")?;
    if !matches!(profile.as_str(), "research" | "design" | "code_change" | "high_risk" | "review") {
        return Err(reject("revision envelope.profile 非法"));
    }
    require_string(map, "objective")?;
    require_string(map, "source_provenance")?;
    for key in ["interfaces", "allowed_edit_scope", "acceptance_clauses", "risks", "rollback", "dependencies"] {
        require_string_array(map, key)?;
    }

    let mut canonical = input.envelope.clone();
    let object = canonical.as_object_mut().expect("validated object");
    object.remove("contract_hash");
    object.remove("created_at");
    object.remove("created_by");
    let contract_hash = hash_value(&canonical)?;
    let payload = serde_json::to_string(&c14n(&canonical))
        .map_err(|e| DaemonRpcError::internal_error(format!("revision envelope 序列化失败: {e}")))?;
    let (normalization_version, normalization_rules_hash) = normalization_rules(tx)?;
    tx.execute(
        "INSERT INTO task_contract_revisions \
         (contract_id,revision,contract_hash,profile,task_id,workspace_id,envelope_payload,created_at,created_by,normalization_version,normalization_rules_hash) \
         VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",
        params![contract_id, revision, contract_hash, profile, input.task_id, workspace_id, payload,
            crate::daemon::task_collab::task_now_ts(), input.created_by, normalization_version, normalization_rules_hash],
    ).map_err(|e| DaemonRpcError::internal_error(format!("Task Contract revision append 失败: {e}")))?;

    Ok(serde_json::json!({
        "ok": true,
        "task_id": input.task_id,
        "contract_id": contract_id,
        "previous_revision": current_revision,
        "previous_contract_hash": current_hash,
        "revision": revision,
        "contract_hash": contract_hash,
        "workspace_id": workspace_id,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn c14n_preserves_ordered_clause_arrays() {
        let input = serde_json::json!({"dependencies":["b","a"],"objective":"x"});
        assert_eq!(c14n(&input)["dependencies"], serde_json::json!(["b","a"]));
    }
}
