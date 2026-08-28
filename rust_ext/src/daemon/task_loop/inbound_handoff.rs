//! 任务 T-1787912195064：`task.next_action` 的 inbound_handoff + work_order 只读投影。
//!
//! 背景：`report_handoff.rs` 已把结构化交接写入 `task_events`
//! （`reason_code='handoff_structured'`，`reason` 为完整 envelope JSON），但
//! `next_action.rs` 从不查询它，导致上一棒的 next_action/reason/证据对下一棒不可见。
//!
//! 本模块从 append-only `task_events` 派生两个**只读**字段：
//! - `inbound_handoff`：最近一条 `handoff_structured` 事件的 envelope 摘要；
//! - `work_order`：objective / 合同路径 / 验收 / 证据 / 历史失败与历史交接摘要。
//!
//! 硬约束（冻结计划 §6.3）：
//! 1. 纯只读：本模块不执行任何 `INSERT`/`UPDATE`/`DELETE`；
//! 2. 不改写既有响应字段（decision/action/routing/next_session/blocking_reasons/
//!    workflow_status/lifecycle_status 保持现值）；
//! 3. `matches_current_routing=false` 时只暴露事实，不得改写 routing；
//! 4. 不新增 `workflow_status` 枚举值；
//! 5. 缺失或损坏时 fail-soft（no_handoff / unparsable_handoff），与实施前行为一致。

use rusqlite::{Connection, OptionalExtension};
use serde_json::{Map, Value};

use crate::daemon::dispatch::DaemonRpcError;

/// 只读投影的截断上限（冻结计划 §6.2：prior_attempts / prior_handoffs 上限 20 条）。
const MAX_PROJECTION_ITEMS: usize = 20;

/// `handoff_structured` 事件的固定 reason_code。
const REASON_CODE_HANDOFF_STRUCTURED: &str = "handoff_structured";

/// 基础设施失败归类（与 next_action.rs 对齐，E_TASK_DB_TRANSACTION）。
fn infra_error(message: &str) -> DaemonRpcError {
    DaemonRpcError::new("E_TASK_DB_TRANSACTION", message.to_string())
}

/// 从 `task_events` 读取该 task 的最近一条 `handoff_structured` 事件
/// （按 `monotonic_seq` 降序，仅取最新）。
fn latest_handoff_event(
    conn: &Connection,
    task_id: &str,
) -> Result<Option<(i64, f64, String)>, DaemonRpcError> {
    conn.query_row(
        "SELECT monotonic_seq, authoritative_timestamp, reason FROM task_events \
         WHERE task_id = ?1 AND reason_code = ?2 \
         ORDER BY monotonic_seq DESC LIMIT 1",
        rusqlite::params![task_id, REASON_CODE_HANDOFF_STRUCTURED],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
    )
    .optional()
    .map_err(|e| infra_error(&format!("handoff_structured 事件读取失败: {e}")))
}

/// 从 `task_events` 读取该 task 的全部 `handoff_structured` 事件摘要
/// （按 `monotonic_seq` 升序；上限由调用方截断）。
fn all_handoff_events(
    conn: &Connection,
    task_id: &str,
) -> Result<Vec<(i64, f64, String)>, DaemonRpcError> {
    let mut stmt = conn
        .prepare(
            "SELECT monotonic_seq, authoritative_timestamp, reason FROM task_events \
             WHERE task_id = ?1 AND reason_code = ?2 \
             ORDER BY monotonic_seq ASC",
        )
        .map_err(|e| infra_error(&format!("handoff_structured 事件遍历准备失败: {e}")))?;
    let rows = stmt
        .query_map(
            rusqlite::params![task_id, REASON_CODE_HANDOFF_STRUCTURED],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .map_err(|e| infra_error(&format!("handoff_structured 事件遍历失败: {e}")))?;
    let mut out = Vec::new();
    for row in rows {
        let (seq, ts, reason) =
            row.map_err(|e| infra_error(&format!("handoff_structured 事件行读取失败: {e}")))?;
        out.push((seq, ts, reason));
    }
    Ok(out)
}

/// 从 `task_steps` 读取该 task 的全部 failed step（按 `step_index` 升序；
/// 上限由调用方截断）。result 为原始字符串，不回写。
fn failed_steps(conn: &Connection, task_id: &str) -> Result<Vec<Value>, DaemonRpcError> {
    let mut stmt = conn
        .prepare(
            "SELECT id, step_index, action, result FROM task_steps \
             WHERE task_id = ?1 AND status = 'failed' ORDER BY step_index ASC",
        )
        .map_err(|e| infra_error(&format!("failed steps 查询准备失败: {e}")))?;
    let rows = stmt
        .query_map([task_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
            ))
        })
        .map_err(|e| infra_error(&format!("failed steps 查询失败: {e}")))?;
    let mut out = Vec::new();
    for row in rows {
        let (step_id, step_index, action, result) =
            row.map_err(|e| infra_error(&format!("failed step 行读取失败: {e}")))?;
        out.push(serde_json::json!({
            "step_id": step_id,
            "step_index": step_index,
            "action": action,
            "status": "failed",
            "result": result,
        }));
    }
    Ok(out)
}

/// 读取 `tasks.title`；缺失时返回空串（不编造）。
fn task_title(conn: &Connection, task_id: &str) -> Result<String, DaemonRpcError> {
    let title: Option<String> = conn
        .query_row("SELECT title FROM tasks WHERE id = ?1", [task_id], |row| {
            row.get(0)
        })
        .optional()
        .map_err(|e| infra_error(&format!("tasks.title 读取失败: {e}")))?;
    Ok(title.unwrap_or_default())
}

/// 解析 envelope 的 source_role/target_role 的运行时角色映射
/// （与 next_action.rs::runtime_role 保持一致）。
fn runtime_role(acting_role: &str) -> &'static str {
    match acting_role {
        "planner" | "implementer" | "tester" | "evidence" | "executor" => "executor",
        "reviewer" | "independent_reviewer" => "reviewer",
        "adjudicator" => "adjudicator",
        _ => "",
    }
}

/// 当前 step 的 action 文本（`task_steps.action`）；step 不存在 → None（不编造）。
fn current_step_action(
    conn: &Connection,
    task_id: &str,
    step_id: Option<&str>,
) -> Result<Option<String>, DaemonRpcError> {
    let Some(step_id) = step_id else {
        return Ok(None);
    };
    let action: Option<String> = conn
        .query_row(
            "SELECT action FROM task_steps WHERE id = ?1 AND task_id = ?2",
            rusqlite::params![step_id, task_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| infra_error(&format!("task_steps.action 读取失败: {e}")))?;
    Ok(action)
}

/// 读取当前 step 绑定的 role_contract canonical payload（step 无 binding → None）。
fn step_role_contract_payload(
    conn: &Connection,
    task_id: &str,
    step_id: &str,
) -> Result<Option<Value>, DaemonRpcError> {
    let revision_id: Option<String> = conn
        .query_row(
            "SELECT role_contract_revision_id FROM task_step_role_contract_bindings \
             WHERE task_id = ?1 AND step_id = ?2 \
             ORDER BY binding_revision DESC LIMIT 1",
            rusqlite::params![task_id, step_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| infra_error(&format!("step role_contract binding 读取失败: {e}")))?;
    let Some(revision_id) = revision_id else {
        return Ok(None);
    };
    let payload_json: Option<String> = conn
        .query_row(
            "SELECT canonical_payload_json FROM role_contract_revisions \
             WHERE role_contract_revision_id = ?1",
            [&revision_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| infra_error(&format!("role_contract_revision payload 读取失败: {e}")))?;
    Ok(payload_json.and_then(|p| serde_json::from_str(&p).ok()))
}

/// 按角色 lineage 读取当前 revision 的 canonical payload（review/adjudicate 语义；
/// lineage 缺失/断链 → None，fail-soft 为空数组）。
fn lineage_role_contract_payload(
    conn: &Connection,
    task_id: &str,
    role: &str,
) -> Result<Option<Value>, DaemonRpcError> {
    let lineage_id: Option<String> = conn
        .query_row(
            "SELECT role_contract_lineage_id FROM role_contract_lineages \
             WHERE task_id = ?1 AND role = ?2",
            rusqlite::params![task_id, role],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| infra_error(&format!("role_contract_lineage 读取失败: {e}")))?;
    let Some(lineage_id) = lineage_id else {
        return Ok(None);
    };
    let revision_id: Option<String> = conn
        .query_row(
            "SELECT role_contract_revision_id FROM role_contract_revisions \
             WHERE role_contract_lineage_id = ?1 ORDER BY revision DESC LIMIT 1",
            [&lineage_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| infra_error(&format!("role_contract 当前 revision 读取失败: {e}")))?;
    let Some(revision_id) = revision_id else {
        return Ok(None);
    };
    let payload_json: Option<String> = conn
        .query_row(
            "SELECT canonical_payload_json FROM role_contract_revisions \
             WHERE role_contract_revision_id = ?1",
            [&revision_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| infra_error(&format!("role_contract payload 读取失败: {e}")))?;
    Ok(payload_json.and_then(|p| serde_json::from_str(&p).ok()))
}

/// 从 `task_events.reason` 解析 envelope；解析失败 → None（fail-soft）。
fn parse_envelope(reason: &str) -> Option<Value> {
    serde_json::from_str::<Value>(reason).ok()
}

/// 从 envelope JSON 提取字符串字段；缺失/类型不符 → None。
fn env_str(envelope: &Value, key: &str) -> Option<String> {
    envelope
        .get(key)
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
}

/// 投影 `inbound_handoff` 字段（冻结计划 §6.1）。
///
/// - 无 handoff 事件 → `{"diagnosis": "no_handoff"}`；
/// - envelope 非法 JSON → `{"diagnosis": "unparsable_handoff", "handoff_event_id": ...}`；
/// - `matches_current_routing` = envelope.target_role 的 runtime 映射 ==
///   `routing.next_role` 的 runtime 映射；false 时**不改写 routing**。
pub fn project_inbound_handoff(
    conn: &Connection,
    task_id: &str,
    routing_next_role: Option<&str>,
) -> Result<Value, DaemonRpcError> {
    let Some((seq, ts, reason)) = latest_handoff_event(conn, task_id)? else {
        return Ok(serde_json::json!({ "diagnosis": "no_handoff" }));
    };
    let Some(envelope) = parse_envelope(&reason) else {
        let event_id_hint = extract_handoff_event_id_hint(&reason);
        return Ok(serde_json::json!({
            "diagnosis": "unparsable_handoff",
            "handoff_event_id": event_id_hint,
        }));
    };
    let target_role = env_str(&envelope, "target_role").unwrap_or_default();
    let matches = !target_role.is_empty()
        && routing_next_role.is_some()
        && runtime_role(&target_role) == runtime_role(routing_next_role.unwrap_or(""));
    Ok(serde_json::json!({
        "handoff_event_id": env_str(&envelope, "handoff_event_id"),
        "from_role": env_str(&envelope, "source_role"),
        "target_role": env_str(&envelope, "target_role"),
        "outcome": env_str(&envelope, "outcome"),
        "reason": env_str(&envelope, "reason"),
        "request_id": env_str(&envelope, "request_id"),
        "step_id": env_str(&envelope, "step_id"),
        "monotonic_seq": seq,
        "authoritative_timestamp": ts,
        "matches_current_routing": matches,
    }))
}

/// 在 next_action 响应对象上附加 `inbound_handoff` + `work_order` 两个只读字段
/// （T-1787912195064 接线入口；不改写任何既有字段，routing 逐字保持）。
pub fn attach_projection(
    conn: &Connection,
    task_id: &str,
    value: &mut Value,
) -> Result<(), DaemonRpcError> {
    let Value::Object(m) = value else {
        return Ok(());
    };
    let next_role = m
        .get("routing")
        .and_then(|r| r.get("next_role"))
        .and_then(Value::as_str);
    let step = m.get("step_id").and_then(Value::as_str);
    let hint = match m.get("action").and_then(Value::as_str) {
        Some("REVIEW") => Some("review"),
        Some("ADJUDICATE") => Some("adjudicate"),
        _ => None,
    };
    let ih = project_inbound_handoff(conn, task_id, next_role)?;
    let wo = project_work_order(conn, task_id, step, hint)?;
    m.insert("inbound_handoff".to_string(), ih);
    m.insert("work_order".to_string(), wo);
    Ok(())
}

/// envelope 非法 JSON 时尽力提取 handoff_event_id（`he-...` 模式），
/// 提取不到 → None（fail-soft，不 panic）。
fn extract_handoff_event_id_hint(reason: &str) -> Option<String> {
    let quote_start = reason.find("he-")?;
    let rest = &reason[quote_start..];
    let end = rest.find('"')?;
    Some(rest[..end].to_string())
}

/// 投影 `work_order` 字段（冻结计划 §6.2）。
///
/// - `objective`：当前 step 的 action；无 step 时为 review/adjudicate 语义文本；
/// - `allowed_paths`/`excluded_paths`/`acceptance_checks`/`required_evidence`/
///   `commands`：来自当前 role_contract canonical payload（step binding 优先，
///   review/adjudicate 语义按角色 lineage），缺失一律空数组；
/// - `prior_attempts`：failed steps（含 result），上限 20，按 step_index 升序；
/// - `prior_handoffs`：全部 handoff_structured 摘要，上限 20，按 monotonic_seq 升序，
///   超限截断并附 `"truncated": true`。
pub fn project_work_order(
    conn: &Connection,
    task_id: &str,
    current_step_id: Option<&str>,
    review_action_hint: Option<&str>,
) -> Result<Value, DaemonRpcError> {
    let objective = current_step_action(conn, task_id, current_step_id)?.or_else(|| {
        review_action_hint.map(|h| match h {
            "review" => "review_current_step".to_string(),
            "adjudicate" => "adjudicate_current_verdict".to_string(),
            _ => h.to_string(),
        })
    });

    // role_contract canonical payload：step binding 优先；无 step 时按
    // review/adjudicate 角色 lineage 兜底（只读，缺失即空数组）。
    let role_contract = match current_step_id {
        Some(step_id) => step_role_contract_payload(conn, task_id, step_id)?,
        None => match review_action_hint {
            Some("review") => lineage_role_contract_payload(conn, task_id, "reviewer")?,
            Some("adjudicate") => lineage_role_contract_payload(conn, task_id, "adjudicator")?,
            _ => None,
        },
    };

    let str_list = |key: &str| -> Vec<String> {
        role_contract
            .as_ref()
            .and_then(|rc| rc.get(key))
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|i| i.as_str())
                    .map(|s| s.to_string())
                    .collect()
            })
            .unwrap_or_default()
    };
    let mut prior_attempts = failed_steps(conn, task_id)?;
    let mut prior_handoffs_raw = all_handoff_events(conn, task_id)?;
    let attempts_truncated = prior_attempts.len() > MAX_PROJECTION_ITEMS;
    let handoffs_truncated = prior_handoffs_raw.len() > MAX_PROJECTION_ITEMS;
    prior_attempts.truncate(MAX_PROJECTION_ITEMS);
    prior_handoffs_raw.truncate(MAX_PROJECTION_ITEMS);
    let prior_handoffs: Vec<Value> = prior_handoffs_raw
        .iter()
        .filter_map(|(seq, _ts, reason)| {
            let envelope = parse_envelope(reason)?;
            Some(serde_json::json!({
                "handoff_event_id": env_str(&envelope, "handoff_event_id"),
                "outcome": env_str(&envelope, "outcome"),
                "reason": env_str(&envelope, "reason"),
                "monotonic_seq": seq,
            }))
        })
        .collect();

    let mut wo = Map::new();
    wo.insert(
        "objective".to_string(),
        objective.map(Value::String).unwrap_or(Value::Null),
    );
    wo.insert(
        "task_title".to_string(),
        Value::String(task_title(conn, task_id)?),
    );
    wo.insert(
        "allowed_paths".to_string(),
        serde_json::json!(str_list("allowed_paths")),
    );
    wo.insert(
        "excluded_paths".to_string(),
        serde_json::json!(str_list("forbidden_paths")),
    );
    wo.insert(
        "acceptance_checks".to_string(),
        serde_json::json!(str_list("acceptance_checks")),
    );
    wo.insert(
        "required_evidence".to_string(),
        serde_json::json!(str_list("required_evidence")),
    );
    wo.insert(
        "commands".to_string(),
        serde_json::json!(str_list("commands")),
    );
    wo.insert(
        "prior_attempts".to_string(),
        serde_json::json!(prior_attempts),
    );
    wo.insert(
        "prior_handoffs".to_string(),
        serde_json::json!(prior_handoffs),
    );
    if attempts_truncated || handoffs_truncated {
        wo.insert("truncated".to_string(), Value::Bool(true));
    }
    Ok(Value::Object(wo))
}
