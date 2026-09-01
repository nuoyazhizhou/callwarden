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

pub(crate) fn assignment_id_for(
    task_id: &str,
    step_id: Option<&str>,
    role: &str,
    source_request_id: &str,
) -> String {
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
        // The SQL predicate binds the event row to `task_id`, but the
        // immutable payload is also part of the assignment provenance.  Do
        // not project a malformed/cross-task payload as current queue state.
        if field(&reason, "task_id").trim() != task_id {
            continue;
        }
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
                && item.step_id.as_deref().map_or(true, |step| {
                    assignment_step_belongs_to_task(conn, task_id, step).unwrap_or(false)
                })
                && step_id.map_or(true, |expected| item.step_id.as_deref() == Some(expected))
                && role.map_or(true, |expected| item.role == expected)
        })
        // assignment_id 是稳定标识符，不代表时间顺序；最新 task event 才是
        // daemon 投影中“当前负责者”的权威排序依据。
        .max_by_key(|item| item.last_event_id))
}

/// Reject an assignment whose step belongs to another task.  The tiny
/// in-memory assignment tests intentionally omit the authority task schema;
/// production task stores always contain `tasks` and `task_steps`, so the
/// compatibility branch is never used by daemon routing.
fn assignment_step_belongs_to_task(
    conn: &Connection,
    task_id: &str,
    step_id: &str,
) -> Result<bool, rusqlite::Error> {
    let has_task_schema: bool = conn.query_row(
        "SELECT EXISTS(
             SELECT 1 FROM sqlite_master
             WHERE type = 'table' AND name IN ('tasks', 'task_steps')
         )",
        [],
        |row| row.get(0),
    )?;
    if !has_task_schema {
        return Ok(true);
    }
    let bound: bool = conn.query_row(
        "SELECT EXISTS(
             SELECT 1 FROM task_steps
             WHERE id = ?1 AND task_id = ?2
         )",
        params![step_id, task_id],
        |row| row.get(0),
    )?;
    Ok(bound)
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
    let id = assignment_id_for(task_id, step_id, role, source_request_id);
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
        .unwrap_or_else(|| assignment_id_for(task_id, step_id, role, source_request_id));
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

/// 漂移 #1 修复（含闭环）：Rust `task.claim` 走事件投影（`task_events`），但 Python
/// 治理层（`db/db_task_leases.py` 的 `assignment_show` / `has_active_assignment`）读取的是
/// 物理表 `task_assignments` 来判断「任务是否有 active assignment」。原 claim 路径从不
/// 写该表，导致它恒为零行 → 任何以该表为权威的网关判定都会 fail-open（误判为「无
/// active assignment」）。此处把事件投影规范化地回写到 `task_assignments`，使两套
/// 派工视图保持最终一致。
///
/// `status` 必须与事件投影对齐：claim 时传 `"active"`，step 完成（report success）时
/// 传 `"completed"`。否则物理表会残留 `active` 孤儿行（与事件投影的 `completed` 冲突），
/// 反而制造新的漂移。因此本函数同时承担「claim 写入」与「完成闭环」两职责。
///
/// `INSERT OR REPLACE` 兼容 claim 重入 / takeover；物理表 `assignment_id` 为 TEXT UNIQUE，
/// 与事件投影的 `A-<sha256>` 同源。
///
/// 该函数属于漂移 #1 的「契约自洽」补偿写，不替代事件投影的权威地位——事件投影
/// 仍是 assignment 状态的唯一真相来源，本写仅用于消除 Python 侧物理表与事件流的漂移。
/// 物理 `task_assignments` 行的 `assignment_id` 必须是「每个 (task, step, role) 一个」的
/// 规范 id，与触发本次写入的 `source_request_id` 无关——否则 claim 写（active）与
/// report 完成写（completed）会派生出两个不同的 `assignment_id`，`INSERT OR REPLACE`
/// 落成两行，永远无法把孤儿 `active` 行收敛为 `completed`，反而制造新的漂移。
/// 因此物理行 id 在 `persist_claimed_assignment` 内部用固定 source 派生，调用方传入的
/// 事件流 id 一律不采用。
const PHYSICAL_ASSIGNMENT_ROW_SOURCE: &str = "physical-task-assignments-row";

pub(crate) fn persist_claimed_assignment(
    tx: &Transaction<'_>,
    workspace_id: i64,
    task_id: &str,
    step_id: Option<&str>,
    role: &str,
    holder_agent_id: &str,
    holder_session_id: &str,
    holder_model_id: &str,
    status: &str,
    ts: f64,
) -> Result<(), DaemonRpcError> {
    // 物理行 id 规范派生：与事件流 id（source 相关）解耦，保证 claim(active) 与
    // report(completed) 命中同一行，从而收敛孤儿 active 行。
    let assignment_id = assignment_id_for(task_id, step_id, role, PHYSICAL_ASSIGNMENT_ROW_SOURCE);
    // 物理表可能尚未随 schema 迁移建立（极旧库）；补偿写失败不应阻断主流程，
    // 但必须显式记录而非静默吞掉，便于审计追踪漂移收敛情况。
    let result = tx.execute(
        "INSERT OR REPLACE INTO task_assignments
         (workspace_id, assignment_id, task_id, role, agent_id, session_id, model_id, status, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        rusqlite::params![
            workspace_id,
            assignment_id,
            task_id,
            role,
            holder_agent_id,
            holder_session_id,
            holder_model_id,
            status,
            ts
        ],
    );
    match result {
        Ok(_) => Ok(()),
        Err(e) => {
            let msg = e.to_string();
            // 表不存在属于可接受的旧库状态（Python 侧会自行建表）；其余错误上抛以便
            // 调用方感知真实的写入失败，避免静默漂移。
            if msg.contains("no such table") {
                Ok(())
            } else {
                Err(DaemonRpcError::internal_error(format!(
                    "回写 task_assignments 失败（漂移 #1 补偿写）: {e}"
                )))
            }
        }
    }
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
    fn claim_persists_normalized_row_into_task_assignments() {
        // 漂移 #1 回归：Rust `task.claim` 走事件投影，但 Python 治理层
        // （`db/db_task_leases.py`）读物理表 `task_assignments` 判「有无 active
        // assignment」。修复后 `persist_claimed_assignment` 必须在 claim 同事务内
        // 把规范化行回写该表，否则会 fail-open。
        let mut conn = conn();
        conn.execute_batch(
            "CREATE TABLE task_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                assignment_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL,
                role TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL
            );",
        )
        .unwrap();
        let tx = conn.unchecked_transaction().unwrap();
        queue_assignment(
            &tx, "T-persist", Some("S-persist"), "executor", "req-queue", None,
            "actor", "exec-session", 1, 10.0,
        )
        .unwrap();
        claim_assignment(
            &tx, "T-persist", Some("S-persist"), "executor", "exec-agent", "exec-session", "exec-model",
            "req-claim", "actor", false, 2, 11.0,
        )
        .unwrap();
        persist_claimed_assignment(
            &tx,
            7,
            "T-persist",
            Some("S-persist"),
            "executor",
            "exec-agent",
            "exec-session",
            "exec-model",
            "active",
            11.0,
        )
        .unwrap();
        tx.commit().unwrap();

        let expected_physical_id =
            assignment_id_for("T-persist", Some("S-persist"), "executor", PHYSICAL_ASSIGNMENT_ROW_SOURCE);
        let row: (String, String, String, String, String) = conn
            .query_row(
                "SELECT assignment_id, task_id, role, agent_id, status
                 FROM task_assignments WHERE task_id = 'T-persist'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
            )
            .unwrap();
        assert_eq!(row.0, expected_physical_id, "回写的 assignment_id 必须是规范物理行 id");
        assert_eq!(row.1, "T-persist");
        assert_eq!(row.2, "executor");
        assert_eq!(row.3, "exec-agent");
        assert_eq!(row.4, "active", "回写行必须是 active 状态");

        // 事件投影仍为真源：回写不应改变投影内容。事件流 id 由 queue 时的
        // source_request_id 派生（claim 复用已排队 id），与物理行规范 id 解耦，
        // 这里独立校验投影一致。
        let expected_event_id =
            assignment_id_for("T-persist", Some("S-persist"), "executor", "req-queue");
        let projection = project_task_assignments(&conn, "T-persist").unwrap();
        assert_eq!(projection[0].assignment_id, expected_event_id);
        assert_eq!(projection[0].status, "claimed");
    }

    #[test]
    fn completion_finalizes_task_assignments_row_to_completed() {
        // 漂移 #1 闭环回归：claim 写入 active 行后，step 完成必须把物理
        // `task_assignments` 行收敛为 `completed`，否则残留孤儿 active 行会与
        // 事件投影的 `completed` 冲突，制造新的「误判有 active assignment」漂移。
        let mut conn = conn();
        conn.execute_batch(
            "CREATE TABLE task_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                assignment_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL,
                role TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL
            );",
        )
        .unwrap();
        let tx = conn.unchecked_transaction().unwrap();
        queue_assignment(
            &tx, "T-finalize", Some("S-finalize"), "executor", "req-queue", None,
            "actor", "exec-session", 1, 10.0,
        )
        .unwrap();
        claim_assignment(
            &tx, "T-finalize", Some("S-finalize"), "executor", "exec-agent", "exec-session", "exec-model",
            "req-claim", "actor", false, 2, 11.0,
        )
        .unwrap();
        persist_claimed_assignment(
            &tx, 7, "T-finalize", Some("S-finalize"),
            "executor", "exec-agent", "exec-session", "exec-model", "active", 11.0,
        )
        .unwrap();

        // 模拟 step 完成：同一 (task, step, role) 复用规范物理行 id 写 completed，
        // 必须命中同一行（INSERT OR REPLACE），不得复制出行。
        persist_claimed_assignment(
            &tx, 7, "T-finalize", Some("S-finalize"),
            "executor", "exec-agent", "exec-session", "exec-model", "completed", 12.0,
        )
        .unwrap();
        tx.commit().unwrap();

        let (status,): (String,) = conn
            .query_row(
                "SELECT status FROM task_assignments WHERE task_id = 'T-finalize'",
                [],
                |r| Ok((r.get(0)?,)),
            )
            .unwrap();
        assert_eq!(status, "completed", "完成补偿写必须把行收敛为 completed");

        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_assignments WHERE task_id = 'T-finalize'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1, "active 与 completed 必须是同一行的更新，不得复制");
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

    #[test]
    fn projection_ignores_cross_task_assignment_payload() {
        let conn = conn();
        let tx = conn.unchecked_transaction().unwrap();
        append_event(
            &tx,
            "T-outer",
            "assignment_queued",
            &json!({
                "assignment_id": "A-cross-task",
                "task_id": "T-other",
                "step_id": "S-other",
                "role": "reviewer",
                "status": "queued",
                "source_request_id": "req-cross-task"
            }),
            "actor",
            "session",
            "reviewer",
            1,
            10.0,
        )
        .unwrap();
        tx.commit().unwrap();

        assert!(project_task_assignments(&conn, "T-outer").unwrap().is_empty());
    }

    #[test]
    fn current_assignment_rejects_step_bound_to_another_task() {
        let conn = conn();
        conn.execute_batch(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY);
             CREATE TABLE task_steps (id TEXT PRIMARY KEY, task_id TEXT NOT NULL);
             INSERT INTO tasks (id) VALUES ('T-bound'), ('T-other');
             INSERT INTO task_steps (id, task_id) VALUES ('S-good', 'T-bound'), ('S-other', 'T-other');",
        )
        .unwrap();
        let tx = conn.unchecked_transaction().unwrap();
        queue_assignment(
            &tx,
            "T-bound",
            Some("S-other"),
            "reviewer",
            "req-other",
            None,
            "actor",
            "session",
            1,
            10.0,
        )
        .unwrap();
        queue_assignment(
            &tx,
            "T-bound",
            Some("S-good"),
            "reviewer",
            "req-good",
            None,
            "actor",
            "session",
            2,
            11.0,
        )
        .unwrap();
        tx.commit().unwrap();

        let current = current_assignment(&conn, "T-bound", None, Some("reviewer"))
            .unwrap()
            .unwrap();
        assert_eq!(current.step_id.as_deref(), Some("S-good"));
    }
}
