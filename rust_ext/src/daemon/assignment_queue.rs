//! Durable task assignment projection backed by the existing append-only task ledger.
//!
//! This is the daemon-side bridge for the durable handoff/assignment contract.  It
//! deliberately uses `task_events` instead of a second mutable queue table: every
//! queue mutation is an immutable, task-bound event and the current assignment is
//! reconstructed from that event stream.  A later schema migration may normalize
//! this projection without changing the event contract.

use std::collections::BTreeMap;

use rusqlite::{params, Connection, Transaction};
use serde_json::{json, Value};

use super::dispatch::DaemonRpcError;
use crate::canonicalize::sha256_hex;

pub(crate) const STALE_AFTER_SECS: f64 = 15.0 * 60.0;

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct AssignmentProjection {
    pub assignment_id: String,
    pub task_id: String,
    pub step_id: Option<String>,
    pub role: String,
    pub status: String,
    pub holder_agent_id: String,
    pub holder_session_id: String,
    pub holder_model_id: String,
    pub queued_at: f64,
    pub claimed_at: Option<f64>,
    pub last_heartbeat_at: Option<f64>,
    pub source_request_id: String,
    pub source_event_id: Option<i64>,
    pub last_event_id: i64,
}

impl AssignmentProjection {
    pub(crate) fn is_active(&self) -> bool {
        matches!(self.status.as_str(), "queued" | "claimed")
    }

    pub(crate) fn as_value(&self) -> Value {
        json!({
            "assignment_id": self.assignment_id,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "role": self.role,
            "status": self.status,
            "holder_agent_id": self.holder_agent_id,
            "holder_session_id": self.holder_session_id,
            "holder_model_id": self.holder_model_id,
            "queued_at": self.queued_at,
            "claimed_at": self.claimed_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "source_request_id": self.source_request_id,
            "source_event_id": self.source_event_id,
            "last_event_id": self.last_event_id,
        })
    }
}

fn field<'a>(reason: &'a Value, name: &str) -> &'a str {
    reason.get(name).and_then(Value::as_str).unwrap_or("")
}

fn optional_field(reason: &Value, name: &str) -> Option<String> {
    let value = field(reason, name).trim();
    (!value.is_empty()).then(|| value.to_string())
}

fn number_field(reason: &Value, name: &str) -> Option<f64> {
    reason.get(name).and_then(Value::as_f64)
}

fn assignment_id(task_id: &str, step_id: Option<&str>, role: &str, source_request_id: &str) -> String {
    let raw = format!(
        "{}:{}:{}:{}",
        task_id,
        step_id.unwrap_or("task-level"),
        role,
        source_request_id
    );
    // Keep the identifier deterministic without depending on another database
    // column or a client-generated opaque identifier.
    let digest = sha256_hex(raw.as_bytes());
    format!("A-{}", &digest[..24])
}

fn append_event(
    tx: &Transaction<'_>,
    task_id: &str,
    reason_code: &str,
    reason: &Value,
    actor_identity: &str,
    actor_session_id: &str,
    role: &str,
    seq: i64,
    ts: f64,
) -> Result<i64, DaemonRpcError> {
    let reason_json = serde_json::to_string(reason)
        .map_err(|e| DaemonRpcError::internal_error(format!("序列化 assignment 事件失败: {e}")))?;
    tx.execute(
        "INSERT INTO task_events
         (task_id, from_status, to_status, reason_code, reason, actor_identity,
          agent_session_id, role, monotonic_seq, authoritative_timestamp)
         VALUES (?1, 'assignment', 'assignment', ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
        params![task_id, reason_code, reason_json, actor_identity, actor_session_id, role, seq, ts],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("追加 assignment 事件失败: {e}")))?;
    Ok(tx.last_insert_rowid())
}

/// Rebuild the assignment projection from task_events.  Unknown/malformed
/// assignment events are ignored so historical ledgers remain readable; known
/// events with a missing assignment id are not allowed to become queue state.
pub(crate) fn project_task_assignments(
    conn: &Connection,
    task_id: &str,
) -> Result<Vec<AssignmentProjection>, DaemonRpcError> {
    let mut stmt = conn
        .prepare(
            "SELECT event_id, reason_code, reason, authoritative_timestamp
             FROM task_events
             WHERE task_id = ?1
               AND reason_code IN ('assignment_queued', 'assignment_claimed',
                                   'assignment_heartbeat', 'assignment_takeover',
                                   'assignment_stale', 'assignment_released',
                                   'assignment_completed')
             ORDER BY event_id ASC",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("查询 assignment 事件失败: {e}")))?;
    let rows = stmt
        .query_map(params![task_id], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, f64>(3)?,
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("读取 assignment 事件失败: {e}")))?;

    let mut assignments: BTreeMap<String, AssignmentProjection> = BTreeMap::new();
    for row in rows {
        let (event_id, reason_code, raw_reason, event_ts) = row
            .map_err(|e| DaemonRpcError::internal_error(format!("映射 assignment 事件失败: {e}")))?;
        let reason = serde_json::from_str::<Value>(&raw_reason).unwrap_or(Value::Null);
        let id = field(&reason, "assignment_id").trim();
        if id.is_empty() {
            continue;
        }
        if reason_code == "assignment_queued" {
            let role = field(&reason, "role").trim();
            if role.is_empty() {
                continue;
            }
            assignments.insert(
                id.to_string(),
                AssignmentProjection {
                    assignment_id: id.to_string(),
                    task_id: task_id.to_string(),
                    step_id: optional_field(&reason, "step_id"),
                    role: role.to_string(),
                    status: "queued".to_string(),
                    holder_agent_id: String::new(),
                    holder_session_id: String::new(),
                    holder_model_id: String::new(),
                    queued_at: number_field(&reason, "queued_at").unwrap_or(event_ts),
                    claimed_at: None,
                    last_heartbeat_at: None,
                    source_request_id: field(&reason, "source_request_id").to_string(),
                    source_event_id: reason.get("source_event_id").and_then(Value::as_i64),
                    last_event_id: event_id,
                },
            );
            continue;
        }

        let Some(assignment) = assignments.get_mut(id) else {
            // A claim can be the first event for tasks created before the queue
            // bridge.  It is safe to materialize that claim as a projection row.
            if reason_code != "assignment_claimed" {
                continue;
            }
            let role = field(&reason, "role").trim();
            if role.is_empty() {
                continue;
            }
            assignments.insert(
                id.to_string(),
                AssignmentProjection {
                    assignment_id: id.to_string(),
                    task_id: task_id.to_string(),
                    step_id: optional_field(&reason, "step_id"),
                    role: role.to_string(),
                    status: "claimed".to_string(),
                    holder_agent_id: field(&reason, "holder_agent_id").to_string(),
                    holder_session_id: field(&reason, "holder_session_id").to_string(),
                    holder_model_id: field(&reason, "holder_model_id").to_string(),
                    queued_at: number_field(&reason, "queued_at").unwrap_or(event_ts),
                    claimed_at: Some(number_field(&reason, "claimed_at").unwrap_or(event_ts)),
                    last_heartbeat_at: Some(
                        number_field(&reason, "last_heartbeat_at").unwrap_or(event_ts),
                    ),
                    source_request_id: field(&reason, "source_request_id").to_string(),
                    source_event_id: reason.get("source_event_id").and_then(Value::as_i64),
                    last_event_id: event_id,
                },
            );
            continue;
        };

        assignment.last_event_id = event_id;
        match reason_code.as_str() {
            "assignment_claimed" => {
                assignment.status = "claimed".to_string();
                assignment.holder_agent_id = field(&reason, "holder_agent_id").to_string();
                assignment.holder_session_id = field(&reason, "holder_session_id").to_string();
                assignment.holder_model_id = field(&reason, "holder_model_id").to_string();
                assignment.claimed_at = Some(number_field(&reason, "claimed_at").unwrap_or(event_ts));
                assignment.last_heartbeat_at = Some(
                    number_field(&reason, "last_heartbeat_at").unwrap_or(event_ts),
                );
            }
            "assignment_heartbeat" => {
                if assignment.is_active() {
                    assignment.status = "claimed".to_string();
                    assignment.last_heartbeat_at = Some(
                        number_field(&reason, "last_heartbeat_at").unwrap_or(event_ts),
                    );
                }
            }
            "assignment_takeover" => {
                // The following assignment_claimed event carries the new holder;
                // retaining this event in the ledger provides the audit trail.
            }
            "assignment_stale" => assignment.status = "stale".to_string(),
            "assignment_released" => assignment.status = "released".to_string(),
            "assignment_completed" => assignment.status = "completed".to_string(),
            _ => {}
        }
    }
    Ok(assignments.into_values().collect())
}

pub(crate) fn current_assignment(
    conn: &Connection,
    task_id: &str,
    step_id: Option<&str>,
    role: Option<&str>,
) -> Result<Option<AssignmentProjection>, DaemonRpcError> {
    Ok(project_task_assignments(conn, task_id)?
        .into_iter()
        .filter(|item| {
            item.is_active()
                && step_id.map_or(true, |expected| item.step_id.as_deref() == Some(expected))
                && role.map_or(true, |expected| item.role == expected)
        })
        // assignment_id 是稳定标识符，不代表时间顺序；最新 task event 才是
        // daemon 投影中“当前负责者”的权威排序依据。
        .max_by_key(|item| item.last_event_id))
}

pub(crate) fn queue_assignment(
    tx: &Transaction<'_>,
    task_id: &str,
    step_id: Option<&str>,
    role: &str,
    source_request_id: &str,
    source_event_id: Option<i64>,
    actor_identity: &str,
    actor_session_id: &str,
    seq: i64,
    ts: f64,
) -> Result<Option<String>, DaemonRpcError> {
    if !matches!(role, "executor" | "reviewer" | "adjudicator") {
        return Ok(None);
    }
    if let Some(existing) = current_assignment(tx, task_id, step_id, Some(role))? {
        return Ok(Some(existing.assignment_id));
    }
    let id = assignment_id(task_id, step_id, role, source_request_id);
    let reason = json!({
        "assignment_id": id,
        "task_id": task_id,
        "step_id": step_id,
        "role": role,
        "status": "queued",
        "queued_at": ts,
        "source_request_id": source_request_id,
        "source_event_id": source_event_id,
    });
    append_event(tx, task_id, "assignment_queued", &reason, actor_identity, actor_session_id, role, seq, ts)?;
    Ok(Some(id))
}

pub(crate) fn claim_assignment(
    tx: &Transaction<'_>,
    task_id: &str,
    step_id: Option<&str>,
    role: &str,
    holder_agent_id: &str,
    holder_session_id: &str,
    holder_model_id: &str,
    source_request_id: &str,
    actor_identity: &str,
    recovered: bool,
    seq: i64,
    ts: f64,
) -> Result<String, DaemonRpcError> {
    let existing = current_assignment(tx, task_id, step_id, Some(role))?;
    let id = existing
        .as_ref()
        .map(|item| item.assignment_id.clone())
        .unwrap_or_else(|| assignment_id(task_id, step_id, role, source_request_id));
    if let Some(previous) = existing {
        if previous.status == "claimed" && previous.holder_session_id == holder_session_id {
            return Ok(id);
        }
        if previous.status == "claimed" && !recovered {
            return Err(DaemonRpcError::new(
                "task_conflict",
                "assignment 已由同角色的其他 session 持有",
            ));
        }
        if recovered {
            let takeover = json!({
                "assignment_id": id,
                "task_id": task_id,
                "step_id": step_id,
                "role": role,
                "status": "takeover",
                "old_holder_agent_id": previous.holder_agent_id,
                "old_holder_session_id": previous.holder_session_id,
                "new_holder_agent_id": holder_agent_id,
                "new_holder_session_id": holder_session_id,
                "takeover_at": ts,
                "source_request_id": source_request_id,
            });
            append_event(tx, task_id, "assignment_takeover", &takeover, actor_identity, holder_session_id, role, seq, ts)?;
        }
    }
    let claimed = json!({
        "assignment_id": id,
        "task_id": task_id,
        "step_id": step_id,
        "role": role,
        "status": "claimed",
        "holder_agent_id": holder_agent_id,
        "holder_session_id": holder_session_id,
        "holder_model_id": holder_model_id,
        "claimed_at": ts,
        "last_heartbeat_at": ts,
        "source_request_id": source_request_id,
    });
    append_event(tx, task_id, "assignment_claimed", &claimed, actor_identity, holder_session_id, role, seq, ts)?;
    Ok(id)
}

pub(crate) fn complete_assignments(
    tx: &Transaction<'_>,
    task_id: &str,
    step_id: Option<&str>,
    role: Option<&str>,
    actor_identity: &str,
    actor_session_id: &str,
    seq: i64,
    ts: f64,
) -> Result<Vec<String>, DaemonRpcError> {
    let active: Vec<_> = project_task_assignments(tx, task_id)?
        .into_iter()
        .filter(|item| {
            item.is_active()
                && step_id.map_or(true, |expected| item.step_id.as_deref() == Some(expected))
                && role.map_or(true, |expected| item.role == expected)
        })
        .collect();
    let mut completed = Vec::new();
    for item in active {
        let reason = json!({
            "assignment_id": item.assignment_id,
            "task_id": task_id,
            "step_id": item.step_id,
            "role": item.role,
            "status": "completed",
            "completed_at": ts,
        });
        append_event(tx, task_id, "assignment_completed", &reason, actor_identity, actor_session_id, &item.role, seq, ts)?;
        completed.push(item.assignment_id);
    }
    Ok(completed)
}

pub(crate) fn heartbeat_assignment(
    tx: &Transaction<'_>,
    task_id: &str,
    assignment_id: &str,
    holder_agent_id: &str,
    holder_session_id: &str,
    holder_model_id: &str,
    actor_identity: &str,
    request_id: &str,
    seq: i64,
    ts: f64,
) -> Result<AssignmentProjection, DaemonRpcError> {
    let current = project_task_assignments(tx, task_id)?
        .into_iter()
        .find(|item| item.assignment_id == assignment_id && item.is_active())
        .ok_or_else(|| DaemonRpcError::new("E_ASSIGNMENT_NOT_ACTIVE", "assignment 不存在或已结束"))?;
    if current.status != "claimed"
        || current.holder_session_id != holder_session_id
        || (!current.holder_agent_id.is_empty() && current.holder_agent_id != holder_agent_id)
    {
        return Err(DaemonRpcError::new(
            "E_ASSIGNMENT_HOLDER_MISMATCH",
            "heartbeat 只能由当前 assignment holder 的同一 session 提交",
        ));
    }
    let last = current.last_heartbeat_at.or(current.claimed_at).unwrap_or(current.queued_at);
    if ts - last > STALE_AFTER_SECS {
        return Err(DaemonRpcError::new(
            "E_ASSIGNMENT_STALE",
            "assignment 已超过 heartbeat stale 窗口，请由同角色 claim 接管",
        ));
    }
    let reason = json!({
        "assignment_id": assignment_id,
        "task_id": task_id,
        "step_id": current.step_id,
        "role": current.role,
        "status": "claimed",
        "holder_agent_id": holder_agent_id,
        "holder_session_id": holder_session_id,
        "holder_model_id": holder_model_id,
        "last_heartbeat_at": ts,
        "request_id": request_id,
    });
    append_event(tx, task_id, "assignment_heartbeat", &reason, actor_identity, holder_session_id, &current.role, seq, ts)?;
    project_task_assignments(tx, task_id)?
        .into_iter()
        .find(|item| item.assignment_id == assignment_id)
        .ok_or_else(|| DaemonRpcError::internal_error("heartbeat 后无法重建 assignment 投影"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE task_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL, from_status TEXT, to_status TEXT,
                reason_code TEXT, reason TEXT, actor_identity TEXT,
                agent_session_id TEXT, role TEXT, monotonic_seq INTEGER,
                authoritative_timestamp REAL
            );",
        )
        .unwrap();
        conn
    }

    #[test]
    fn queue_claim_heartbeat_and_complete_are_replayable_events() {
        let mut conn = conn();
        let tx = conn.transaction().unwrap();
        let id = queue_assignment(&tx, "T-1", Some("S-1"), "reviewer", "req-1", Some(7), "actor", "sess-r", 1, 10.0)
            .unwrap()
            .unwrap();
        claim_assignment(&tx, "T-1", Some("S-1"), "reviewer", "agent-r", "sess-r", "m", "req-2", "actor", false, 2, 11.0).unwrap();
        let hb = heartbeat_assignment(&tx, "T-1", &id, "agent-r", "sess-r", "m", "actor", "req-3", 3, 12.0).unwrap();
        assert_eq!(hb.last_heartbeat_at, Some(12.0));
        complete_assignments(&tx, "T-1", Some("S-1"), Some("reviewer"), "actor", "sess-r", 4, 13.0).unwrap();
        tx.commit().unwrap();
        let projection = project_task_assignments(&conn, "T-1").unwrap();
        assert_eq!(projection[0].status, "completed");
        assert_eq!(projection[0].last_heartbeat_at, Some(12.0));
    }

    #[test]
    fn stale_takeover_is_same_assignment_and_keeps_audit_event() {
        let mut conn = conn();
        let tx = conn.transaction().unwrap();
        let id = queue_assignment(&tx, "T-2", None, "executor", "req-1", None, "a", "old", 1, 10.0).unwrap().unwrap();
        claim_assignment(&tx, "T-2", None, "executor", "old-agent", "old", "m", "req-2", "a", false, 2, 11.0).unwrap();
        claim_assignment(&tx, "T-2", None, "executor", "new-agent", "new", "m", "req-3", "a", true, 3, 20.0).unwrap();
        tx.commit().unwrap();
        let projection = project_task_assignments(&conn, "T-2").unwrap();
        assert_eq!(projection[0].assignment_id, id);
        assert_eq!(projection[0].holder_session_id, "new");
        let takeover_count: i64 = conn.query_row("SELECT COUNT(*) FROM task_events WHERE reason_code='assignment_takeover'", [], |r| r.get(0)).unwrap();
        assert_eq!(takeover_count, 1);
    }

    #[test]
    fn replayed_queue_and_same_holder_claim_are_idempotent() {
        let mut conn = conn();
        let tx = conn.transaction().unwrap();
        let first = queue_assignment(
            &tx, "T-replay", Some("S-replay"), "reviewer", "req-queue", None,
            "actor", "session", 1, 10.0,
        ).unwrap().unwrap();
        let replayed_queue = queue_assignment(
            &tx, "T-replay", Some("S-replay"), "reviewer", "req-queue", None,
            "actor", "session", 2, 11.0,
        ).unwrap().unwrap();
        assert_eq!(replayed_queue, first);

        let first_claim = claim_assignment(
            &tx, "T-replay", Some("S-replay"), "reviewer", "agent", "session", "m",
            "req-claim", "actor", false, 3, 12.0,
        ).unwrap();
        let replayed_claim = claim_assignment(
            &tx, "T-replay", Some("S-replay"), "reviewer", "agent", "session", "m",
            "req-claim-replay", "actor", false, 4, 13.0,
        ).unwrap();
        assert_eq!(replayed_claim, first_claim);
        tx.commit().unwrap();

        let queued_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM task_events WHERE task_id='T-replay' AND reason_code='assignment_queued'",
            [], |r| r.get(0),
        ).unwrap();
        let claimed_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM task_events WHERE task_id='T-replay' AND reason_code='assignment_claimed'",
            [], |r| r.get(0),
        ).unwrap();
        assert_eq!(queued_count, 1, "重复 queue 不得复制 assignment");
        assert_eq!(claimed_count, 1, "同 holder 重放 claim 不得复制事件");
    }

    #[test]
    fn second_same_role_holder_is_rejected_until_stale_takeover() {
        let mut conn = conn();
        let tx = conn.transaction().unwrap();
        queue_assignment(
            &tx, "T-conflict", None, "executor", "req-queue", None,
            "actor", "old-session", 1, 10.0,
        ).unwrap();
        claim_assignment(
            &tx, "T-conflict", None, "executor", "old-agent", "old-session", "m",
            "req-old", "actor", false, 2, 11.0,
        ).unwrap();

        let conflict = claim_assignment(
            &tx, "T-conflict", None, "executor", "new-agent", "new-session", "m",
            "req-new", "actor", false, 3, 12.0,
        ).unwrap_err();
        assert_eq!(conflict.code, "task_conflict");

        let id = claim_assignment(
            &tx, "T-conflict", None, "executor", "new-agent", "new-session", "m",
            "req-new", "actor", true, 4, 13.0,
        ).unwrap();
        tx.commit().unwrap();
        let projection = project_task_assignments(&conn, "T-conflict").unwrap();
        assert_eq!(projection[0].assignment_id, id);
        assert_eq!(projection[0].holder_session_id, "new-session");
        let takeover_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM task_events WHERE task_id='T-conflict' AND reason_code='assignment_takeover'",
            [], |r| r.get(0),
        ).unwrap();
        assert_eq!(takeover_count, 1, "接管必须留下不可变审计事件");
    }

    #[test]
    fn heartbeat_rejects_assignment_after_stale_timeout() {
        let mut conn = conn();
        let tx = conn.transaction().unwrap();
        let id = queue_assignment(
            &tx, "T-timeout", None, "executor", "req-queue", None,
            "actor", "session", 1, 10.0,
        ).unwrap().unwrap();
        claim_assignment(
            &tx, "T-timeout", None, "executor", "agent", "session", "m",
            "req-claim", "actor", false, 2, 11.0,
        ).unwrap();
        let stale = heartbeat_assignment(
            &tx, "T-timeout", &id, "agent", "session", "m", "actor", "req-hb",
            3, 11.0 + STALE_AFTER_SECS + 1.0,
        ).unwrap_err();
        assert_eq!(stale.code, "E_ASSIGNMENT_STALE");
        tx.commit().unwrap();
        let heartbeat_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM task_events WHERE task_id='T-timeout' AND reason_code='assignment_heartbeat'",
            [], |r| r.get(0),
        ).unwrap();
        assert_eq!(heartbeat_count, 0, "stale heartbeat 不得写入伪续租事件");
    }

    #[test]
    fn current_assignment_follows_event_order_not_assignment_id_order() {
        let mut conn = conn();
        let tx = conn.transaction().unwrap();
        queue_assignment(
            &tx, "T-current", Some("S-review"), "reviewer", "req-review", None,
            "actor", "reviewer-session", 1, 10.0,
        ).unwrap();
        let executor_id = queue_assignment(
            &tx, "T-current", Some("S-execute"), "executor", "req-execute", None,
            "actor", "executor-session", 2, 11.0,
        ).unwrap().unwrap();
        claim_assignment(
            &tx, "T-current", Some("S-execute"), "executor", "executor-agent", "executor-session", "m",
            "req-claim", "actor", false, 3, 12.0,
        ).unwrap();
        tx.commit().unwrap();

        let current = current_assignment(&conn, "T-current", None, None).unwrap().unwrap();
        assert_eq!(current.assignment_id, executor_id);
        assert_eq!(current.role, "executor");
        assert_eq!(current.status, "claimed");
    }
}
