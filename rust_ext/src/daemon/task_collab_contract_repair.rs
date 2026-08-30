//! P0-L contract repair helpers.

use super::*;

/// 将历史 task.create envelope 适配为 revision append domain 所要求的稳定形状。
pub(super) fn normalize_p0l_repair_envelope(
    payload: &str,
    contract_id: &str,
    revision: i64,
    previous_hash: &str,
    task_id: &str,
) -> Result<Value, DaemonRpcError> {
    let mut envelope: Value = serde_json::from_str(payload).map_err(|error| {
        DaemonRpcError::new(
            "E_P0L_POLICY_REPAIR_CONTRACT_INVALID",
            format!("P0-L 当前 Task Contract envelope 不是合法 JSON: {error}"),
        )
    })?;
    let object = envelope.as_object_mut().ok_or_else(|| {
        DaemonRpcError::new(
            "E_P0L_POLICY_REPAIR_CONTRACT_INVALID",
            "P0-L 当前 Task Contract envelope 必须是 JSON object",
        )
    })?;
    let mut legacy = serde_json::Map::new();
    let preserve = |legacy: &mut serde_json::Map<String, Value>, key: &str, value: Value| {
        if !value.is_null() {
            legacy.insert(key.to_string(), value);
        }
    };

    if object.get("contract_id").and_then(Value::as_str).is_none() {
        object.insert(
            "contract_id".to_string(),
            Value::String(contract_id.to_string()),
        );
    }
    if object.get("profile").and_then(Value::as_str).is_none() {
        if let Some(value) = object.remove("profile") {
            preserve(&mut legacy, "profile", value);
        }
        object.insert(
            "profile".to_string(),
            Value::String("code_change".to_string()),
        );
    }

    let objective = object.remove("objective");
    match objective {
        Some(Value::String(value)) if !value.trim().is_empty() => {
            object.insert("objective".to_string(), Value::String(value));
        }
        Some(value) => {
            preserve(&mut legacy, "objective", value.clone());
            let text = value
                .get("statement")
                .and_then(Value::as_str)
                .filter(|item| !item.trim().is_empty())
                .map(ToOwned::to_owned)
                .unwrap_or_else(|| value.to_string());
            object.insert("objective".to_string(), Value::String(text));
        }
        None => {
            object.insert(
                "objective".to_string(),
                Value::String(format!("P0-L identity policy repair for {task_id}")),
            );
        }
    };

    if object
        .get("source_provenance")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .is_none()
    {
        if let Some(value) = object.remove("source_provenance") {
            preserve(&mut legacy, "source_provenance", value);
        }
        let source = object.remove("source");
        if let Some(value) = source {
            preserve(&mut legacy, "source", value.clone());
            object.insert(
                "source_provenance".to_string(),
                Value::String(value.to_string()),
            );
        } else {
            object.insert(
                "source_provenance".to_string(),
                Value::String(format!("p0l_identity_policy_repair:{task_id}")),
            );
        }
    }

    for key in [
        "interfaces",
        "allowed_edit_scope",
        "acceptance_clauses",
        "risks",
        "rollback",
        "dependencies",
    ] {
        let value = object.remove(key);
        let converted = match value {
            Some(Value::Array(items)) if !items.is_empty() => items
                .into_iter()
                .map(|item| match item {
                    Value::String(text) if !text.trim().is_empty() => Value::String(text),
                    other => Value::String(other.to_string()),
                })
                .collect(),
            Some(value) => {
                preserve(&mut legacy, key, value.clone());
                vec![Value::String(value.to_string())]
            }
            None => vec![Value::String(format!(
                "{key}: not declared in historical revision"
            ))],
        };
        object.insert(key.to_string(), Value::Array(converted));
    }
    if !legacy.is_empty() {
        object.insert("legacy_contract_fields".to_string(), Value::Object(legacy));
    }
    object.insert("revision".to_string(), Value::Number(revision.into()));
    object.insert(
        "supersedes_revision".to_string(),
        Value::Number((revision - 1).into()),
    );
    object.insert(
        "supersedes_contract_hash".to_string(),
        Value::String(previous_hash.to_string()),
    );
    object.insert(
        "identity_policy".to_string(),
        Value::String(POLICY_ROLE_WORKER_V1.to_string()),
    );
    object.insert(
        "repair_provenance".to_string(),
        serde_json::json!({"kind": "p0l_identity_policy_v1", "task_id": task_id}),
    );
    Ok(envelope)
}
