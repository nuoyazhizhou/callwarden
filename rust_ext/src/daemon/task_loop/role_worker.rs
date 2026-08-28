//! P0-J：CW 本地 Role Worker 授权与可变运行时 provenance。
//!
//! `role_worker_id`、`role_instance_id` 和 daemon 签发的 credential 是授权锚点；
//! provider/account/model/runtime session 只是 append-only 审计事实，绝不参与
//! 角色授权比较，也不得保存 raw token。

use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde_json::{json, Value};

use crate::canonicalize::sha256_hex;
use crate::daemon::dispatch::DaemonRpcError;

pub const ERR_AUTH_REQUIRED: &str = "E_ROLE_WORKER_AUTH_REQUIRED";
pub const ERR_CREDENTIAL_INVALID: &str = "E_ROLE_WORKER_CREDENTIAL_INVALID";
pub const ERR_ROLE_MISMATCH: &str = "E_ROLE_WORKER_ROLE_MISMATCH";
pub const ERR_INSTANCE_INVALID: &str = "E_ROLE_WORKER_INSTANCE_INVALID";
pub const ERR_SEPARATION: &str = "E_ROLE_WORKER_SEPARATION_VIOLATION";
pub const ERR_RUNTIME_SECRET: &str = "E_RUNTIME_PROVENANCE_SECRET_FORBIDDEN";
pub const ERR_WORKER_NOT_FOUND: &str = "E_ROLE_WORKER_NOT_FOUND";
pub const ERR_WORKER_REVOKED: &str = "E_ROLE_WORKER_REVOKED";

// ===== P0-L：Task Contract identity policy（typed 解析，fail-closed）=====

/// 唯一合法的 identity policy 枚举；其他取值一律拒绝，绝不静默降级。
pub const POLICY_LEGACY_IDENTITY_V1: &str = "legacy_identity_v1";
pub const POLICY_ROLE_WORKER_V1: &str = "role_worker_v1";
pub const ERR_POLICY_REQUIRED: &str = "E_TASK_IDENTITY_POLICY_REQUIRED";
pub const ERR_POLICY_MISMATCH: &str = "E_TASK_IDENTITY_POLICY_MISMATCH";

/// 从 canonical Task Contract envelope 解析 `identity_policy`。
///
/// fail-closed 语义（P0-L step1 决策）：
/// - missing / malformed / multiple policy 槽位 → `E_TASK_IDENTITY_POLICY_REQUIRED`；
/// - unknown 取值 → `E_TASK_IDENTITY_POLICY_MISMATCH`。
/// policy 是后续 create→bootstrap/revise→next_action→claim 的唯一授权路由锚点，
/// 绝不把 provider/account/model/agent/session 重新当作授权依据。
pub fn parse_identity_policy(envelope: &Value) -> Result<String, DaemonRpcError> {
    let object = envelope.as_object().ok_or_else(|| {
        DaemonRpcError::new(ERR_POLICY_REQUIRED, "Task Contract envelope 必须是 JSON object")
    })?;
    let mut slots: Vec<(&str, &Value)> = Vec::new();
    for key in ["identity_policy", "identity_policies"] {
        if let Some(value) = object.get(key) {
            slots.push((key, value));
        }
    }
    if slots.is_empty() {
        return Err(DaemonRpcError::new(
            ERR_POLICY_REQUIRED,
            "Task Contract envelope 缺少 identity_policy（zero policy fail-closed）",
        ));
    }
    if slots.len() > 1 {
        return Err(DaemonRpcError::new(
            ERR_POLICY_REQUIRED,
            "Task Contract envelope 同时声明多个 identity policy 槽位（multiple policy fail-closed）",
        ));
    }
    // identity_policies 作为唯一槽位时只接受恰好一个元素，保持单 policy 语义。
    let declared: &Value = match slots[0] {
        ("identity_policies", Value::Array(items)) => {
            if items.len() != 1 {
                return Err(DaemonRpcError::new(
                    ERR_POLICY_REQUIRED,
                    format!("identity_policies 必须恰好含 1 个 policy，实际 {} 个（multiple/zero fail-closed）", items.len()),
                ));
            }
            &items[0]
        }
        (_, value) => value,
    };
    let policy = declared
        .as_str()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            DaemonRpcError::new(
                ERR_POLICY_REQUIRED,
                "identity_policy 必须是非空字符串（malformed fail-closed）",
            )
        })?;
    match policy {
        POLICY_LEGACY_IDENTITY_V1 | POLICY_ROLE_WORKER_V1 => Ok(policy.to_string()),
        other => Err(DaemonRpcError::new(
            ERR_POLICY_MISMATCH,
            format!(
                "identity_policy {other} 未知；仅支持 {POLICY_LEGACY_IDENTITY_V1} / {POLICY_ROLE_WORKER_V1}"
            ),
        )),
    }
}

/// 从已持久化的 envelope payload JSON 中只读提取 `identity_policy`。
///
/// 用于事务内回读校验（caller/persisted exact match）与 current policy 投影；
/// 缺失/非法 JSON/空值一律返回 None，由调用方按 fail-closed 语义处置。
pub fn policy_from_envelope_payload(payload_json: &str) -> Option<String> {
    let value: Value = serde_json::from_str(payload_json).ok()?;
    value
        .get("identity_policy")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|policy| !policy.is_empty())
        .map(ToOwned::to_owned)
}

#[derive(Clone, Debug)]
pub struct RoleWorkerAuth {
    pub role_worker_id: String,
    pub role_instance_id: String,
    pub role_session_id: String,
    pub credential: String,
    pub runtime: Value,
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_secs_f64())
        .unwrap_or(0.0)
}

fn non_empty(object: &serde_json::Map<String, Value>, name: &str) -> Result<String, DaemonRpcError> {
    object
        .get(name)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
        .ok_or_else(|| DaemonRpcError::new(ERR_AUTH_REQUIRED, format!("role_worker_auth 缺少非空字段 {name}")))
}

fn runtime_contains_secret(value: &Value) -> bool {
    match value {
        Value::Object(values) => values.iter().any(|(key, child)| {
            let key = key.to_ascii_lowercase();
            key.contains("token") || key.contains("secret") || key.contains("password")
                || key.contains("cookie") || key.contains("credential") || runtime_contains_secret(child)
        }),
        Value::Array(values) => values.iter().any(runtime_contains_secret),
        _ => false,
    }
}

pub fn parse_role_worker_auth(params: &Value) -> Result<Option<RoleWorkerAuth>, DaemonRpcError> {
    let Some(raw) = params.get("role_worker_auth") else { return Ok(None); };
    if raw.is_null() { return Ok(None); }
    let value = if let Some(text) = raw.as_str() {
        serde_json::from_str::<Value>(text).map_err(|_| {
            DaemonRpcError::new(ERR_AUTH_REQUIRED, "role_worker_auth 必须是 JSON object")
        })?
    } else {
        raw.clone()
    };
    let object = value.as_object().ok_or_else(|| {
        DaemonRpcError::new(ERR_AUTH_REQUIRED, "role_worker_auth 必须是 JSON object")
    })?;
    let runtime = object.get("runtime").cloned().unwrap_or_else(|| json!({}));
    if !runtime.is_object() {
        return Err(DaemonRpcError::new(ERR_AUTH_REQUIRED, "role_worker_auth.runtime 必须是 JSON object"));
    }
    if runtime_contains_secret(&runtime) {
        return Err(DaemonRpcError::new(
            ERR_RUNTIME_SECRET,
            "runtime provenance 禁止包含 token/secret/password/cookie/credential",
        ));
    }
    Ok(Some(RoleWorkerAuth {
        role_worker_id: non_empty(object, "role_worker_id")?,
        role_instance_id: non_empty(object, "role_instance_id")?,
        role_session_id: non_empty(object, "role_session_id")?,
        credential: non_empty(object, "credential")?,
        runtime,
    }))
}

fn event_id(prefix: &str, material: &str) -> String {
    format!("{}-{}", prefix, &sha256_hex(material.as_bytes())[..24])
}

/// 以 OS CSPRNG 签发一次性 Role Worker credential。
///
/// credential 的安全性不得依赖 wall clock、PID、worker ID 或可观察 hash；这些值只可
/// 作为审计字段，绝不可构成密钥材料。32 byte entropy 以 hex 编码后只经 enrollment
/// success response 返回一次，authority 数据库仅保存其 SHA-256 hash。
fn credential_material() -> Result<String, DaemonRpcError> {
    let mut entropy = [0_u8; 32];
    getrandom::fill(&mut entropy).map_err(|error| {
        DaemonRpcError::internal_error(format!("OS CSPRNG 签发 role worker credential 失败: {error}"))
    })?;
    Ok(hex::encode(entropy))
}

pub fn enroll_role_worker(
    tx: &Transaction<'_>,
    owner_key: &str,
    params: &Value,
    workspace_id: i64,
) -> Result<Value, DaemonRpcError> {
    let object = params.as_object().ok_or_else(|| {
        DaemonRpcError::invalid_params("role_worker.enroll 参数必须是 JSON object")
    })?;
    let role_worker_id = non_empty(object, "role_worker_id")?;
    let role_instance_id = non_empty(object, "role_instance_id")?;
    let role = non_empty(object, "role")?;
    if !matches!(role.as_str(), "executor" | "reviewer" | "adjudicator") {
        return Err(DaemonRpcError::new(ERR_ROLE_MISMATCH, "role_worker.enroll role 必须是 executor/reviewer/adjudicator"));
    }
    let runtime = object.get("runtime").cloned().unwrap_or_else(|| json!({}));
    if !runtime.is_object() || runtime_contains_secret(&runtime) {
        return Err(DaemonRpcError::new(ERR_RUNTIME_SECRET, "enrollment runtime 必须为无秘密 JSON object"));
    }
    let duplicate: Option<String> = tx.query_row(
        "SELECT role FROM role_workers WHERE role_worker_id=?1",
        [&role_worker_id], |row| row.get(0),
    ).optional().map_err(|error| DaemonRpcError::internal_error(format!("查询 role worker 失败: {error}")))?;
    if duplicate.is_some() {
        return Err(DaemonRpcError::new("E_ROLE_WORKER_ALREADY_EXISTS", "role_worker_id 已存在；禁止覆盖或重签发"));
    }
    let raw_credential = credential_material()?;
    let credential_hash = sha256_hex(raw_credential.as_bytes());
    let ts = now_secs();
    tx.execute(
        "INSERT INTO role_workers (role_worker_id,owner_key,role,credential_hash,status,created_at,revoked_at,revocation_reason) VALUES (?1,?2,?3,?4,'active',?5,0,'')",
        params![role_worker_id, owner_key, role, credential_hash, ts],
    ).map_err(|error| DaemonRpcError::internal_error(format!("创建 role worker 失败: {error}")))?;
    tx.execute(
        "INSERT INTO role_worker_instances (role_instance_id,role_worker_id,owner_key,status,created_at,retired_at) VALUES (?1,?2,?3,'active',?4,0)",
        params![role_instance_id, role_worker_id, owner_key, ts],
    ).map_err(|error| DaemonRpcError::internal_error(format!("创建 role worker instance 失败: {error}")))?;
    let payload = serde_json::to_string(&runtime).map_err(|error| DaemonRpcError::internal_error(format!("runtime provenance 序列化失败: {error}")))?;
    tx.execute(
        "INSERT INTO role_runtime_provenance (event_id,workspace_id,task_id,action_type,role_worker_id,role_instance_id,role_session_id,runtime_payload_json,recorded_at) VALUES (?1,?2,'','role_worker.enroll',?3,?4,'enrollment',?5,?6)",
        params![event_id("RRP", &format!("enroll:{role_worker_id}:{role_instance_id}:{ts}")), workspace_id, role_worker_id, role_instance_id, payload, ts],
    ).map_err(|error| DaemonRpcError::internal_error(format!("记录 enrollment provenance 失败: {error}")))?;
    Ok(json!({
        "ok": true,
        "role_worker_id": role_worker_id,
        "role_instance_id": role_instance_id,
        "role": role,
        "credential": raw_credential,
        "credential_delivery": "response_once",
        "recorded_at": ts,
    }))
}


/// Revoke a local Role Worker append-only: credentials are never reissued and
/// runtime provenance records only a constrained non-secret reason code.
pub fn revoke_role_worker(
    tx: &Transaction<'_>,
    owner_key: &str,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let object = params.as_object().ok_or_else(|| {
        DaemonRpcError::invalid_params("role_worker.revoke 参数必须是 JSON object")
    })?;
    let role_worker_id = non_empty(object, "role_worker_id")?;
    let reason_code = non_empty(object, "reason_code")?;
    let workspace_id = object.get("workspace_id").and_then(Value::as_i64).filter(|value| *value > 0)
        .ok_or_else(|| DaemonRpcError::invalid_params("role_worker.revoke 必须携带 workspace_id > 0"))?;
    let workspace_exists: bool = tx.query_row(
        "SELECT COUNT(*) FROM workspaces WHERE id=?1", [workspace_id], |row| row.get::<_, i64>(0).map(|value| value == 1),
    ).map_err(|error| DaemonRpcError::internal_error(format!("校验 role worker revoke workspace 失败: {error}")))?;
    if !workspace_exists {
        return Err(DaemonRpcError::new("E_ROLE_WORKER_WORKSPACE_NOT_FOUND", "role_worker.revoke workspace 不存在"));
    }
    if reason_code.len() > 80
        || !reason_code
            .chars()
            .all(|value| value.is_ascii_alphanumeric() || matches!(value, '_' | '-' | '.'))
    {
        return Err(DaemonRpcError::new(
            ERR_RUNTIME_SECRET,
            "role_worker.revoke reason_code 必须是长度 ≤80 的无秘密 ASCII code",
        ));
    }
    let row: Option<(String, String)> = tx
        .query_row(
            "SELECT owner_key,status FROM role_workers WHERE role_worker_id=?1",
            [&role_worker_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(|error| DaemonRpcError::internal_error(format!("读取 role worker revoke 状态失败: {error}")))?;
    let Some((worker_owner, status)) = row else {
        return Err(DaemonRpcError::new(ERR_WORKER_NOT_FOUND, "role worker 不存在"));
    };
    if worker_owner != owner_key {
        return Err(DaemonRpcError::new(ERR_CREDENTIAL_INVALID, "role worker 不属于当前 local peer"));
    }
    if status == "revoked" {
        return Err(DaemonRpcError::new(ERR_WORKER_REVOKED, "role worker 已撤销；禁止重复或恢复"));
    }
    if status != "active" {
        return Err(DaemonRpcError::new(ERR_CREDENTIAL_INVALID, "role worker 非 active，禁止撤销状态改写"));
    }
    let active_instance_id: String = tx.query_row(
        "SELECT role_instance_id FROM role_worker_instances WHERE role_worker_id=?1 AND owner_key=?2 AND status='active' ORDER BY created_at ASC LIMIT 1",
        params![role_worker_id, owner_key], |row| row.get(0),
    ).map_err(|error| DaemonRpcError::new(ERR_INSTANCE_INVALID, format!("role worker 缺少 active instance，拒绝撤销: {error}")))?;
    let ts = now_secs();
    tx.execute(
        "UPDATE role_workers SET status='revoked',revoked_at=?1,revocation_reason=?2 WHERE role_worker_id=?3 AND owner_key=?4 AND status='active'",
        params![ts, reason_code, role_worker_id, owner_key],
    ).map_err(|error| DaemonRpcError::internal_error(format!("撤销 role worker 失败: {error}")))?;
    tx.execute(
        "INSERT INTO role_runtime_provenance (event_id,workspace_id,task_id,action_type,role_worker_id,role_instance_id,role_session_id,runtime_payload_json,recorded_at) \
         VALUES (?1,?2,'','role_worker.revoke',?3,?4,'revocation',?5,?6)",
        params![
            event_id("RRP", &format!("revoke:{role_worker_id}:{ts}")),
            workspace_id,
            role_worker_id,
            active_instance_id,
            serde_json::to_string(&json!({"reason_code": reason_code})).map_err(|error| DaemonRpcError::internal_error(format!("序列化 revocation provenance 失败: {error}")))?,
            ts,
        ],
    ).map_err(|error| DaemonRpcError::internal_error(format!("记录 role worker revoke provenance 失败: {error}")))?;
    Ok(json!({
        "ok": true,
        "role_worker_id": role_worker_id,
        "status": "revoked",
        "revoked_at": ts,
        "reason_code": reason_code,
    }))
}

/// Owner-scoped, credential-free Role Worker lifecycle state. It intentionally
/// omits credential_hash and all runtime payloads, which may only be audited by
/// separate authority methods.
pub fn role_worker_status(
    conn: &Connection,
    owner_key: &str,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let object = params.as_object().ok_or_else(|| {
        DaemonRpcError::invalid_params("role_worker.status 参数必须是 JSON object")
    })?;
    let role_worker_id = non_empty(object, "role_worker_id")?;
    let worker: Option<(String, String, f64, f64, String)> = conn
        .query_row(
            "SELECT role,status,created_at,revoked_at,revocation_reason FROM role_workers WHERE role_worker_id=?1 AND owner_key=?2",
            params![role_worker_id, owner_key],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?)),
        )
        .optional()
        .map_err(|error| DaemonRpcError::internal_error(format!("读取 role worker status 失败: {error}")))?;
    let Some((role, status, created_at, revoked_at, revocation_reason)) = worker else {
        return Err(DaemonRpcError::new(ERR_WORKER_NOT_FOUND, "role worker 不存在或不属于当前 local peer"));
    };
    let mut statement = conn
        .prepare("SELECT role_instance_id,status,created_at,retired_at FROM role_worker_instances WHERE role_worker_id=?1 AND owner_key=?2 ORDER BY created_at ASC")
        .map_err(|error| DaemonRpcError::internal_error(format!("查询 role worker instances 失败: {error}")))?;
    let instances = statement
        .query_map(params![role_worker_id, owner_key], |row| {
            Ok(json!({
                "role_instance_id": row.get::<_, String>(0)?,
                "status": row.get::<_, String>(1)?,
                "created_at": row.get::<_, f64>(2)?,
                "retired_at": row.get::<_, f64>(3)?,
            }))
        })
        .map_err(|error| DaemonRpcError::internal_error(format!("读取 role worker instances 失败: {error}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| DaemonRpcError::internal_error(format!("解码 role worker instances 失败: {error}")))?;
    Ok(json!({
        "ok": true,
        "role_worker_id": role_worker_id,
        "role": role,
        "status": status,
        "created_at": created_at,
        "revoked_at": revoked_at,
        "revocation_reason": revocation_reason,
        "instances": instances,
    }))
}

pub fn validate_and_record(
    tx: &Transaction<'_>,
    auth: &RoleWorkerAuth,
    owner_key: &str,
    workspace_id: i64,
    task_id: &str,
    action_type: &str,
    expected_role: &str,
) -> Result<(), DaemonRpcError> {
    validate_and_record_inner(
        tx,
        auth,
        owner_key,
        workspace_id,
        task_id,
        action_type,
        Some(expected_role),
    )
    .map(|_| ())
}

/// P0-L R1：worker-first 授权校验。
///
/// 与 `validate_and_record` 的唯一区别：调用方不供应 expected_role——稳定
/// Role Worker 在 authority 库中登记的角色（credential 校验通过后从 DB 读取）
/// 是唯一角色锚点，直接作为有效角色返回，供下游合同/事件归属使用。
/// runtime identity 不参与本函数的任何授权判定（P0-L R1：worker-first，
/// runtime identity 仅 provenance）。
pub fn validate_and_record_worker_first(
    tx: &Transaction<'_>,
    auth: &RoleWorkerAuth,
    owner_key: &str,
    workspace_id: i64,
    task_id: &str,
    action_type: &str,
) -> Result<String, DaemonRpcError> {
    validate_and_record_inner(
        tx,
        auth,
        owner_key,
        workspace_id,
        task_id,
        action_type,
        None,
    )
}

fn validate_and_record_inner(
    tx: &Transaction<'_>,
    auth: &RoleWorkerAuth,
    owner_key: &str,
    workspace_id: i64,
    task_id: &str,
    action_type: &str,
    expected_role: Option<&str>,
) -> Result<String, DaemonRpcError> {
    let row: Option<(String, String, String, String)> = tx.query_row(
        "SELECT w.owner_key,w.role,w.credential_hash,w.status FROM role_workers w WHERE w.role_worker_id=?1",
        [&auth.role_worker_id], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
    ).optional().map_err(|error| DaemonRpcError::internal_error(format!("读取 role worker 失败: {error}")))?;
    let Some((worker_owner, worker_role, credential_hash, status)) = row else {
        return Err(DaemonRpcError::new(ERR_CREDENTIAL_INVALID, "role worker 未登记"));
    };
    if worker_owner != owner_key || status != "active" || credential_hash != sha256_hex(auth.credential.as_bytes()) {
        return Err(DaemonRpcError::new(ERR_CREDENTIAL_INVALID, "role worker credential、owner 或状态无效"));
    }
    if let Some(expected) = expected_role {
        if worker_role != expected {
            return Err(DaemonRpcError::new(ERR_ROLE_MISMATCH, format!("role worker 已绑定 role={}，不可用于 {}", worker_role, expected)));
        }
    }
    let instance: Option<(String, String)> = tx.query_row(
        "SELECT role_worker_id,status FROM role_worker_instances WHERE role_instance_id=?1 AND owner_key=?2",
        params![auth.role_instance_id, owner_key], |row| Ok((row.get(0)?, row.get(1)?)),
    ).optional().map_err(|error| DaemonRpcError::internal_error(format!("读取 role worker instance 失败: {error}")))?;
    if instance.as_ref().map(|value| value.0.as_str()) != Some(auth.role_worker_id.as_str())
        || instance.as_ref().map(|value| value.1.as_str()) != Some("active")
    {
        return Err(DaemonRpcError::new(ERR_INSTANCE_INVALID, "role worker instance 不存在、已退役或不属于该 worker"));
    }
    let conflict: Option<String> = tx.query_row(
        "SELECT p.role_worker_id FROM role_runtime_provenance p JOIN role_workers w ON w.role_worker_id=p.role_worker_id WHERE p.task_id=?1 AND w.role<>?2 AND p.role_worker_id=?3 ORDER BY p.recorded_at DESC LIMIT 1",
        params![task_id, worker_role, auth.role_worker_id], |row| row.get(0),
    ).optional().map_err(|error| DaemonRpcError::internal_error(format!("校验 role worker 分离失败: {error}")))?;
    if conflict.is_some() {
        return Err(DaemonRpcError::new(ERR_SEPARATION, "同一 role worker 已在该任务上承担冲突治理角色"));
    }
    let payload = serde_json::to_string(&auth.runtime).map_err(|error| DaemonRpcError::internal_error(format!("runtime provenance 序列化失败: {error}")))?;
    let ts = now_secs();
    tx.execute(
        "INSERT INTO role_runtime_provenance (event_id,workspace_id,task_id,action_type,role_worker_id,role_instance_id,role_session_id,runtime_payload_json,recorded_at) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
        params![event_id("RRP", &format!("{action_type}:{task_id}:{}:{}:{ts}", auth.role_worker_id, auth.role_session_id)), workspace_id, task_id, action_type, auth.role_worker_id, auth.role_instance_id, auth.role_session_id, payload, ts],
    ).map_err(|error| DaemonRpcError::internal_error(format!("记录 runtime provenance 失败: {error}")))?;
    Ok(worker_role)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sqlite_query::migrate_connection;

    fn db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        assert_eq!(migrate_connection(&conn).unwrap(), 60);
        conn.execute(
            "INSERT INTO workspaces (id,name,root_path,created_at,is_active) VALUES (1,'role-worker-test','/role-worker-test',0,1)",
            [],
        ).unwrap();
        conn
    }

    fn enrollment(worker: &str, instance: &str, role: &str, runtime: Value) -> Value {
        json!({
            "role_worker_id": worker,
            "role_instance_id": instance,
            "role": role,
            "runtime": runtime,
        })
    }

    fn enroll(conn: &mut Connection, worker: &str, instance: &str, role: &str, runtime: Value) -> Value {
        let tx = conn.transaction().unwrap();
        let response = enroll_role_worker(&tx, "owner-A", &enrollment(worker, instance, role, runtime), 1).unwrap();
        tx.commit().unwrap();
        response
    }

    fn auth(worker: &str, instance: &str, session: &str, credential: &str, runtime: Value) -> RoleWorkerAuth {
        RoleWorkerAuth {
            role_worker_id: worker.to_string(),
            role_instance_id: instance.to_string(),
            role_session_id: session.to_string(),
            credential: credential.to_string(),
            runtime,
        }
    }

    #[test]
    fn enrollment_uses_csprng_and_persists_hash_only() {
        let mut conn = db();
        let first = enroll(&mut conn, "rw-exec-a", "rwi-exec-a", "executor", json!({"provider":"p1"}));
        let second = enroll(&mut conn, "rw-exec-b", "rwi-exec-b", "executor", json!({"provider":"p1"}));
        let credential_a = first["credential"].as_str().unwrap();
        let credential_b = second["credential"].as_str().unwrap();
        assert_eq!(credential_a.len(), 64);
        assert_eq!(credential_b.len(), 64);
        assert_ne!(credential_a, credential_b);
        let stored: String = conn.query_row("SELECT credential_hash FROM role_workers WHERE role_worker_id='rw-exec-a'", [], |row| row.get(0)).unwrap();
        assert_eq!(stored, sha256_hex(credential_a.as_bytes()));
        assert_ne!(stored, credential_a);
        let provenance: String = conn.query_row("SELECT runtime_payload_json FROM role_runtime_provenance WHERE role_worker_id='rw-exec-a'", [], |row| row.get(0)).unwrap();
        assert!(!provenance.contains(credential_a));
    }

    #[test]
    fn rejects_wrong_credential_and_cross_role_impersonation() {
        let mut conn = db();
        let response = enroll(&mut conn, "rw-exec", "rwi-exec", "executor", json!({}));
        let credential = response["credential"].as_str().unwrap();
        let tx = conn.transaction().unwrap();
        let wrong = validate_and_record(&tx, &auth("rw-exec", "rwi-exec", "s1", "wrong", json!({})), "owner-A", 1, "T1", "task.claim", "executor").unwrap_err();
        assert_eq!(wrong.code, ERR_CREDENTIAL_INVALID);
        let impersonation = validate_and_record(&tx, &auth("rw-exec", "rwi-exec", "s1", credential, json!({})), "owner-A", 1, "T1", "task.claim", "reviewer").unwrap_err();
        assert_eq!(impersonation.code, ERR_ROLE_MISMATCH);
    }

    #[test]
    fn runtime_changes_are_accepted_and_recorded_append_only() {
        let mut conn = db();
        let response = enroll(&mut conn, "rw-exec", "rwi-exec", "executor", json!({"provider":"vendor-a","model_id":"m1"}));
        let credential = response["credential"].as_str().unwrap();
        for (session, runtime) in [
            ("role-session-1", json!({"provider":"vendor-a","account":"acct-one","model_id":"m1","agent":"agent-a"})),
            ("role-session-2", json!({"provider":"vendor-b","account":"acct-two","model_id":"m2","agent":"agent-b"})),
        ] {
            let tx = conn.transaction().unwrap();
            validate_and_record(&tx, &auth("rw-exec", "rwi-exec", session, credential, runtime), "owner-A", 1, "T-runtime", "task.claim", "executor").unwrap();
            tx.commit().unwrap();
        }
        let count: i64 = conn.query_row("SELECT COUNT(*) FROM role_runtime_provenance WHERE task_id='T-runtime' AND action_type='task.claim'", [], |row| row.get(0)).unwrap();
        assert_eq!(count, 2);
        let payloads: Vec<String> = conn.prepare("SELECT runtime_payload_json FROM role_runtime_provenance WHERE task_id='T-runtime' ORDER BY recorded_at ASC").unwrap()
            .query_map([], |row| row.get(0)).unwrap().collect::<Result<Vec<_>, _>>().unwrap();
        assert!(payloads.iter().any(|value| value.contains("vendor-a")));
        assert!(payloads.iter().any(|value| value.contains("vendor-b")));
    }

    #[test]
    fn rejects_runtime_secrets_before_any_authorization_write() {
        let malformed = parse_role_worker_auth(&json!({
            "role_worker_auth": {
                "role_worker_id":"rw","role_instance_id":"ri","role_session_id":"rs","credential":"c",
                "runtime":{"provider":"ok","api_token":"must-not-store"}
            }
        })).unwrap_err();
        assert_eq!(malformed.code, ERR_RUNTIME_SECRET);
    }

    #[test]
    fn revoked_worker_is_rejected_and_status_omits_credential_hash() {
        let mut conn = db();
        let response = enroll(&mut conn, "rw-exec", "rwi-exec", "executor", json!({}));
        let credential = response["credential"].as_str().unwrap();
        let tx = conn.transaction().unwrap();
        let revoked = revoke_role_worker(&tx, "owner-A", &json!({"role_worker_id":"rw-exec","workspace_id":1,"reason_code":"COMPROMISE_SUSPECTED"})).unwrap();
        assert_eq!(revoked["status"], "revoked");
        tx.commit().unwrap();
        let status = role_worker_status(&conn, "owner-A", &json!({"role_worker_id":"rw-exec"})).unwrap();
        assert_eq!(status["status"], "revoked");
        assert!(status.get("credential_hash").is_none());
        let tx = conn.transaction().unwrap();
        let err = validate_and_record(&tx, &auth("rw-exec", "rwi-exec", "s-after-revoke", credential, json!({})), "owner-A", 1, "T2", "task.claim", "executor").unwrap_err();
        assert_eq!(err.code, ERR_CREDENTIAL_INVALID);
    }

    // ===== P0-L step1：identity policy 解析 fail-closed 矩阵 =====

    #[test]
    fn identity_policy_accepts_only_canonical_enum() {
        assert_eq!(
            parse_identity_policy(&json!({"identity_policy": "legacy_identity_v1"})).unwrap(),
            POLICY_LEGACY_IDENTITY_V1
        );
        assert_eq!(
            parse_identity_policy(&json!({"identity_policy": "role_worker_v1"})).unwrap(),
            POLICY_ROLE_WORKER_V1
        );
        // identity_policies 单元素数组等价单 policy。
        assert_eq!(
            parse_identity_policy(&json!({"identity_policies": ["role_worker_v1"]})).unwrap(),
            POLICY_ROLE_WORKER_V1
        );
    }

    #[test]
    fn identity_policy_zero_malformed_multiple_are_required_errors() {
        // zero：无 policy 槽位。
        let zero = parse_identity_policy(&json!({"contract_id": "TC-x"})).unwrap_err();
        assert_eq!(zero.code, ERR_POLICY_REQUIRED);
        // malformed：非字符串/空字符串。
        let malformed = parse_identity_policy(&json!({"identity_policy": 42})).unwrap_err();
        assert_eq!(malformed.code, ERR_POLICY_REQUIRED);
        let empty = parse_identity_policy(&json!({"identity_policy": "  "})).unwrap_err();
        assert_eq!(empty.code, ERR_POLICY_REQUIRED);
        // multiple：双槽位。
        let multiple = parse_identity_policy(&json!({
            "identity_policy": "legacy_identity_v1", "identity_policies": ["role_worker_v1"]
        })).unwrap_err();
        assert_eq!(multiple.code, ERR_POLICY_REQUIRED);
        // multiple：数组超过一个元素。
        let two = parse_identity_policy(&json!({"identity_policies": ["legacy_identity_v1", "role_worker_v1"]})).unwrap_err();
        assert_eq!(two.code, ERR_POLICY_REQUIRED);
        // malformed envelope：非 object。
        let non_object = parse_identity_policy(&json!(["legacy_identity_v1"])).unwrap_err();
        assert_eq!(non_object.code, ERR_POLICY_REQUIRED);
    }

    #[test]
    fn identity_policy_unknown_value_is_mismatch_error() {
        let err = parse_identity_policy(&json!({"identity_policy": "role_worker_v2"})).unwrap_err();
        assert_eq!(err.code, ERR_POLICY_MISMATCH);
    }

    #[test]
    fn policy_from_envelope_payload_roundtrip_and_absence() {
        let payload = serde_json::to_string(&json!({"identity_policy": "role_worker_v1", "revision": 1})).unwrap();
        assert_eq!(policy_from_envelope_payload(&payload).as_deref(), Some(POLICY_ROLE_WORKER_V1));
        // 无 policy / 非法 JSON / 空值均返回 None，交由调用方 fail-closed。
        assert!(policy_from_envelope_payload("{}").is_none());
        assert!(policy_from_envelope_payload("not-json").is_none());
        assert!(policy_from_envelope_payload(r#"{"identity_policy": ""}"#).is_none());
    }
}

