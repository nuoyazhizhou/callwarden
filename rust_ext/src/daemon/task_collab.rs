//! Task 协同 RPC 模块（multi-llm-contract-collaboration D0/P1）。
//!
//! 提供 Task 状态机与协同 RPC 的 Rust daemon 端逻辑：
//! - `agent.register`
//! - `agent.heartbeat`
//! - `task.create`
//! - `task.claim`
//! - `task.work_next`
//! - `task.report`
//! - `task.handoff`
//! - `task.status`
//! - `task.events`
//! - `task.wait`
//!
//! 状态机直接整合 Call Warden 权威原生表（`tasks`、`task_steps`、`task_events`、`agent_registrations`），
//! 不建立旁路 `daemon_tasks` 表。

use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection};
use serde_json::{Map, Value};

use super::dispatch::{DaemonRpcError, PeerCredential};

// ============================================
// DDL
// ============================================

const TASK_COLLAB_DDL: &str = r#"
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    creator TEXT NOT NULL DEFAULT 'agent',
    status TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    applied_at REAL,
    closed_at REAL,
    parent_id TEXT DEFAULT '',
    depth INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_steps (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    action TEXT NOT NULL,
    target_file TEXT DEFAULT '',
    target_symbol TEXT DEFAULT '',
    check_items TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT DEFAULT '',
    created_at REAL NOT NULL,
    completed_at REAL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS task_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    workspace_id TEXT DEFAULT '',
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    reason_code TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    actor_identity TEXT NOT NULL,
    agent_session_id TEXT DEFAULT '',
    role TEXT DEFAULT '',
    contract_hash TEXT DEFAULT '',
    snapshot_id TEXT DEFAULT '',
    monotonic_seq INTEGER NOT NULL,
    authoritative_timestamp REAL NOT NULL,
    evidence_path TEXT DEFAULT '',
    evidence_hash TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS agent_registrations (
    agent_id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    owner_key TEXT NOT NULL,
    capabilities TEXT DEFAULT '[]',
    registered_at REAL NOT NULL,
    last_heartbeat REAL NOT NULL,
    status TEXT DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_task_steps_task ON task_steps(task_id);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id);
"#;

fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn rand_val() -> u32 {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    (ts & 0xffffffff) as u32
}

fn generate_task_id() -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    format!("T-{}-{:08x}", now, rand_val())
}

// ============================================
// TaskCollabStore
// ============================================

pub struct TaskCollabStore {
    conn: Arc<Mutex<Connection>>,
    seq_counter: Arc<Mutex<i64>>,
}

impl TaskCollabStore {
    pub fn new<P: AsRef<Path>>(db_path: P) -> Result<Self, DaemonRpcError> {
        let conn = Connection::open(&db_path).map_err(|e| {
            DaemonRpcError::internal_error(format!("无法打开 Task DB {}: {}", db_path.as_ref().display(), e))
        })?;

        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;").ok();

        conn.execute_batch(TASK_COLLAB_DDL).map_err(|e| {
            DaemonRpcError::internal_error(format!("初始化 Task DDL 失败: {}", e))
        })?;

        let max_seq: i64 = conn
            .query_row("SELECT COALESCE(MAX(monotonic_seq), 0) FROM task_events", [], |r| r.get(0))
            .unwrap_or(0);

        Ok(Self {
            conn: Arc::new(Mutex::new(conn)),
            seq_counter: Arc::new(Mutex::new(max_seq)),
        })
    }

    pub fn from_connection(conn: Connection) -> Result<Self, DaemonRpcError> {
        conn.execute_batch(TASK_COLLAB_DDL).map_err(|e| {
            DaemonRpcError::internal_error(format!("初始化 Task DDL 失败: {}", e))
        })?;

        let max_seq: i64 = conn
            .query_row("SELECT COALESCE(MAX(monotonic_seq), 0) FROM task_events", [], |r| r.get(0))
            .unwrap_or(0);

        Ok(Self {
            conn: Arc::new(Mutex::new(conn)),
            seq_counter: Arc::new(Mutex::new(max_seq)),
        })
    }

    fn next_seq(&self) -> i64 {
        let mut seq = self.seq_counter.lock().unwrap();
        *seq += 1;
        *seq
    }

    /// 获取任务当前在 task_events 表中的最新声明者与 agent_session_id
    fn get_task_claim_info(&self, conn: &Connection, task_id: &str) -> (Option<String>, Option<String>) {
        let res: Result<(String, String), _> = conn.query_row(
            "SELECT actor_identity, agent_session_id FROM task_events 
             WHERE task_id = ?1 AND to_status = 'in_progress' 
             ORDER BY monotonic_seq DESC LIMIT 1",
            params![task_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        );
        match res {
            Ok((actor, session)) => (Some(actor), Some(session)),
            Err(_) => (None, None),
        }
    }

    pub fn handle_agent_register(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let agent_name = params
            .get("agent_name")
            .and_then(|v| v.as_str())
            .unwrap_or("cw-agent");
        let capabilities = params
            .get("capabilities")
            .map(|v| v.to_string())
            .unwrap_or_else(|| "[]".to_string());

        let owner_key = peer.owner_key();
        let agent_id = format!("agent-{}", owner_key);
        let ts = now_ts();

        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT OR REPLACE INTO agent_registrations 
             (agent_id, agent_name, owner_key, capabilities, registered_at, last_heartbeat, status)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'active')",
            params![agent_id, agent_name, owner_key, capabilities, ts, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("agent_register 失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("agent_id".to_string(), Value::String(agent_id));
        res.insert("status".to_string(), Value::String("registered".to_string()));
        res.insert("owner_key".to_string(), Value::String(owner_key));
        Ok(Value::Object(res))
    }

    pub fn handle_agent_heartbeat(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let agent_id = params
            .get("agent_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_else(|| format!("agent-{}", peer.owner_key()));

        let ts = now_ts();
        let conn = self.conn.lock().unwrap();
        let updated = conn
            .execute(
                "UPDATE agent_registrations SET last_heartbeat = ?1 WHERE agent_id = ?2",
                params![ts, agent_id],
            )
            .unwrap_or(0);

        let mut res = Map::new();
        res.insert("status".to_string(), Value::String("ok".to_string()));
        res.insert("timestamp".to_string(), Value::Number(serde_json::Number::from_f64(ts).unwrap()));
        res.insert("acknowledged".to_string(), Value::Bool(updated > 0));
        Ok(Value::Object(res))
    }

    pub fn handle_task_create(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let title = params
            .get("title")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 title"))?;
        let description = params
            .get("description")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let parent_id = params
            .get("parent_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let workspace_id = params
            .get("workspace_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");

        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_else(generate_task_id);

        let ts = now_ts();
        let seq = self.next_seq();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        tx.execute(
            "INSERT INTO tasks 
             (id, title, description, creator, status, created_at, updated_at, parent_id)
             VALUES (?1, ?2, ?3, ?4, 'open', ?5, ?6, ?7)",
            params![task_id, title, description, peer.owner_key(), ts, ts, parent_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_create 失败: {}", e)))?;

        tx.execute(
            "INSERT INTO task_events 
             (task_id, workspace_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'none', 'open', 'created', ?3, ?4, ?5, ?6)",
            params![task_id, workspace_id, title, peer.owner_key(), seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 task_create 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id));
        res.insert("status".to_string(), Value::String("open".to_string()));
        res.insert("title".to_string(), Value::String(title.to_string()));
        Ok(Value::Object(res))
    }

    pub fn handle_task_claim(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let owner_key = peer.owner_key();
        let agent_session_id = params
            .get("agent_session_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_else(|| owner_key.clone());

        let ts = now_ts();
        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        // 检查 task 当前状态
        let current_status: String = tx
            .query_row(
                "SELECT status FROM tasks WHERE id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)))?;

        if current_status != "open" && current_status != "in_progress" {
            return Err(DaemonRpcError::new(
                "task_conflict",
                format!("Task {} 处于不可 claim 状态 ({})", task_id, current_status),
            ));
        }

        // 检查是否已被其他 agent claim
        let (_claimed_actor, claimed_session) = self.get_task_claim_info(&tx, task_id);
        if let Some(existing_session) = claimed_session {
            if current_status == "in_progress" && existing_session != agent_session_id {
                return Err(DaemonRpcError::new(
                    "task_conflict",
                    format!("Task {} 已被 agent {} 抢占", task_id, existing_session),
                ));
            }
        }

        let updated = tx
            .execute(
                "UPDATE tasks SET status = 'in_progress', updated_at = ?1 WHERE id = ?2 AND status IN ('open', 'in_progress')",
                params![ts, task_id],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("task_claim 失败: {}", e)))?;

        if updated == 0 {
            return Err(DaemonRpcError::new(
                "task_conflict",
                format!("Task {} 抢占失败", task_id),
            ));
        }

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events 
             (task_id, from_status, to_status, reason_code, reason, actor_identity, agent_session_id, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'in_progress', 'claimed', 'task claimed by agent', ?3, ?4, ?5, ?6)",
            params![task_id, current_status, owner_key, agent_session_id, seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 task_claim 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String("in_progress".to_string()));
        res.insert("claimed_by".to_string(), Value::String(agent_session_id));
        Ok(Value::Object(res))
    }

    pub fn handle_task_work_next(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;

        let conn = self.conn.lock().unwrap();
        let status: String = conn
            .query_row(
                "SELECT status FROM tasks WHERE id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)))?;

        let (_claimed_actor, claimed_session) = self.get_task_claim_info(&conn, task_id);
        let claimed_by = claimed_session.unwrap_or_default();

        let mut stmt = conn
            .prepare("SELECT id, step_index, action, target_file, status FROM task_steps WHERE task_id = ?1 ORDER BY step_index ASC")
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 task_steps 失败: {}", e)))?;

        let steps_iter = stmt
            .query_map(params![task_id], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, String>(4)?,
                ))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("映射 task_steps 失败: {}", e)))?;

        let mut steps = Vec::new();
        let mut next_step_id = None;
        for s in steps_iter.flatten() {
            if next_step_id.is_none() && (s.4 == "pending" || s.4 == "in_progress") {
                next_step_id = Some(s.0.clone());
            }
            let mut sm = Map::new();
            sm.insert("step_id".to_string(), Value::String(s.0));
            sm.insert("step_index".to_string(), Value::Number(s.1.into()));
            sm.insert("action".to_string(), Value::String(s.2));
            sm.insert("target_file".to_string(), Value::String(s.3));
            sm.insert("status".to_string(), Value::String(s.4));
            steps.push(Value::Object(sm));
        }

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String(status));
        res.insert("claimed_by".to_string(), Value::String(claimed_by));
        res.insert("next_step_id".to_string(), next_step_id.map(Value::String).unwrap_or(Value::Null));
        res.insert("steps".to_string(), Value::Array(steps));
        Ok(Value::Object(res))
    }

    pub fn handle_task_report(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let summary = params
            .get("summary")
            .and_then(|v| v.as_str())
            .unwrap_or("report submitted");
        let evidence_path = params
            .get("evidence_path")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let evidence_hash = params
            .get("evidence_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("");

        let owner_key = peer.owner_key();
        let agent_session_id = params
            .get("agent_session_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_else(|| owner_key.clone());

        let ts = now_ts();
        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        // 校验 claim 所有者 (P1 修复: 只有 claim 对应的 agent 才能 report)
        let (claimed_actor, claimed_session) = self.get_task_claim_info(&tx, task_id);
        if let Some(c_actor) = claimed_actor {
            if c_actor != owner_key {
                if let Some(c_sess) = claimed_session {
                    if c_sess != agent_session_id {
                        return Err(DaemonRpcError::permission_denied(format!(
                            "只有 claim 该任务的 agent ({}) 才能提交 report，当前为 {}",
                            c_actor, owner_key
                        )));
                    }
                }
            }
        }

        let updated = tx
            .execute(
                "UPDATE tasks SET status = 'review', updated_at = ?1 WHERE id = ?2",
                params![ts, task_id],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("task_report 失败: {}", e)))?;

        if updated == 0 {
            return Err(DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)));
        }

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events 
             (task_id, from_status, to_status, reason_code, reason, actor_identity, agent_session_id, monotonic_seq, authoritative_timestamp, evidence_path, evidence_hash)
             VALUES (?1, 'in_progress', 'review', 'reported', ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            params![task_id, summary, owner_key, agent_session_id, seq, ts, evidence_path, evidence_hash],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 task_report 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String("review".to_string()));
        Ok(Value::Object(res))
    }

    pub fn handle_task_handoff(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let target_agent = params
            .get("target_agent")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let reason = params
            .get("reason")
            .and_then(|v| v.as_str())
            .unwrap_or("handoff requested");

        let owner_key = peer.owner_key();
        let ts = now_ts();
        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        // 校验 handoff 权限 (P1 修复: 校验 creator 或 current claimer)
        let (creator, _status): (String, String) = tx
            .query_row("SELECT creator, status FROM tasks WHERE id = ?1", params![task_id], |r| Ok((r.get(0)?, r.get(1)?)))
            .map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)))?;

        let (claimed_actor, _) = self.get_task_claim_info(&tx, task_id);
        let is_authorized = creator == owner_key || claimed_actor.as_deref() == Some(&owner_key) || owner_key == "root";
        if !is_authorized {
            return Err(DaemonRpcError::permission_denied(format!(
                "没有对任务 {} 执行 handoff 的权限",
                task_id
            )));
        }

        let updated = tx
            .execute(
                "UPDATE tasks SET status = 'open', updated_at = ?1 WHERE id = ?2",
                params![ts, task_id],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("task_handoff 失败: {}", e)))?;

        if updated == 0 {
            return Err(DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)));
        }

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events 
             (task_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, 'in_progress', 'open', 'handoff', ?2, ?3, ?4, ?5)",
            params![task_id, reason, owner_key, seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 task_handoff 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String("open".to_string()));
        res.insert("target_agent".to_string(), Value::String(target_agent.to_string()));
        Ok(Value::Object(res))
    }

    pub fn handle_task_status(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;

        let conn = self.conn.lock().unwrap();
        let row = conn
            .query_row(
                "SELECT id, title, description, parent_id, status, creator, created_at, updated_at 
                 FROM tasks WHERE id = ?1",
                params![task_id],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                        r.get::<_, String>(3)?,
                        r.get::<_, String>(4)?,
                        r.get::<_, String>(5)?,
                        r.get::<_, f64>(6)?,
                        r.get::<_, f64>(7)?,
                    ))
                },
            )
            .map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)))?;

        let (_claimed_actor, claimed_session) = self.get_task_claim_info(&conn, task_id);

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(row.0));
        res.insert("title".to_string(), Value::String(row.1));
        res.insert("description".to_string(), Value::String(row.2));
        res.insert("parent_id".to_string(), Value::String(row.3));
        res.insert("status".to_string(), Value::String(row.4));
        res.insert("creator".to_string(), Value::String(row.5));
        res.insert("claimed_by".to_string(), Value::String(claimed_session.unwrap_or_default()));
        res.insert("created_at".to_string(), Value::Number(serde_json::Number::from_f64(row.6).unwrap()));
        res.insert("updated_at".to_string(), Value::Number(serde_json::Number::from_f64(row.7).unwrap()));
        Ok(Value::Object(res))
    }

    pub fn handle_task_events(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;

        let conn = self.conn.lock().unwrap();
        let mut stmt = conn
            .prepare(
                "SELECT event_id, task_id, workspace_id, from_status, to_status, reason_code, reason, actor_identity, agent_session_id, role, contract_hash, snapshot_id, monotonic_seq, authoritative_timestamp, evidence_path, evidence_hash 
                 FROM task_events WHERE task_id = ?1 ORDER BY monotonic_seq ASC",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 events 失败: {}", e)))?;

        let rows = stmt
            .query_map(params![task_id], |r| {
                let mut m = Map::new();
                m.insert("event_id".to_string(), Value::Number(r.get::<_, i64>(0)?.into()));
                m.insert("task_id".to_string(), Value::String(r.get(1)?));
                m.insert("workspace_id".to_string(), Value::String(r.get(2)?));
                m.insert("from_status".to_string(), Value::String(r.get(3)?));
                m.insert("to_status".to_string(), Value::String(r.get(4)?));
                m.insert("reason_code".to_string(), Value::String(r.get(5)?));
                m.insert("reason".to_string(), Value::String(r.get(6)?));
                m.insert("actor_identity".to_string(), Value::String(r.get(7)?));
                m.insert("agent_session_id".to_string(), Value::String(r.get(8)?));
                m.insert("role".to_string(), Value::String(r.get(9)?));
                m.insert("contract_hash".to_string(), Value::String(r.get(10)?));
                m.insert("snapshot_id".to_string(), Value::String(r.get(11)?));
                m.insert("monotonic_seq".to_string(), Value::Number(r.get::<_, i64>(12)?.into()));
                m.insert("authoritative_timestamp".to_string(), Value::Number(serde_json::Number::from_f64(r.get(13)?).unwrap()));
                m.insert("evidence_path".to_string(), Value::String(r.get(14)?));
                m.insert("evidence_hash".to_string(), Value::String(r.get(15)?));
                Ok(Value::Object(m))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("映射 events 失败: {}", e)))?;

        let mut events = Vec::new();
        for r in rows.flatten() {
            events.push(r);
        }

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("events".to_string(), Value::Array(events));
        Ok(Value::Object(res))
    }

    pub fn handle_task_wait(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let timeout_secs = params
            .get("timeout_seconds")
            .and_then(|v| v.as_f64())
            .unwrap_or(2.0);

        let target_status = params
            .get("target_status")
            .and_then(|v| v.as_str())
            .unwrap_or("review");

        let deadline = SystemTime::now() + Duration::from_secs_f64(timeout_secs);
        let mut final_status = String::new();

        loop {
            {
                let conn = self.conn.lock().unwrap();
                let status: Result<String, _> = conn.query_row(
                    "SELECT status FROM tasks WHERE id = ?1",
                    params![task_id],
                    |r| r.get(0),
                );
                if let Ok(st) = status {
                    final_status = st.clone();
                    if st == target_status || st == "closed" || st == "applied" || st == "review" {
                        let mut res = Map::new();
                        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
                        res.insert("status".to_string(), Value::String(st));
                        res.insert("ready".to_string(), Value::Bool(true));
                        return Ok(Value::Object(res));
                    }
                } else {
                    return Err(DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)));
                }
            }

            if SystemTime::now() >= deadline {
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String(final_status));
        res.insert("ready".to_string(), Value::Bool(false));
        Ok(Value::Object(res))
    }
}

// ============================================
// Tests
// ============================================

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_db() -> (tempfile::TempDir, std::path::PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test_collab.db");
        (dir, db_path)
    }

    #[test]
    fn test_task_collab_full_lifecycle() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);

        // 1. Agent Register
        let reg_params = serde_json::json!({
            "agent_name": "agent-alpha",
            "capabilities": ["code", "review"]
        });
        let reg_res = store.handle_agent_register(peer.clone(), &reg_params).unwrap();
        assert_eq!(reg_res["status"], "registered");

        // 2. Task Create
        let create_params = serde_json::json!({
            "title": "Fix memory leak in parser",
            "description": "Investigate tree-sitter memory allocation",
            "task_id": "T-TEST-001"
        });
        let create_res = store.handle_task_create(peer.clone(), &create_params).unwrap();
        assert_eq!(create_res["task_id"], "T-TEST-001");
        assert_eq!(create_res["status"], "open");

        // 3. Task Claim
        let claim_params = serde_json::json!({
            "task_id": "T-TEST-001",
            "agent_session_id": "session-123"
        });
        let claim_res = store.handle_task_claim(peer.clone(), &claim_params).unwrap();
        assert_eq!(claim_res["status"], "in_progress");
        assert_eq!(claim_res["claimed_by"], "session-123");

        // Concurrent Claim Conflict Test
        let peer2 = PeerCredential::new_unix(1001, 1001, 5678);
        let claim2_params = serde_json::json!({
            "task_id": "T-TEST-001",
            "agent_session_id": "session-456"
        });
        let claim2_err = store.handle_task_claim(peer2.clone(), &claim2_params).unwrap_err();
        assert_eq!(claim2_err.code, "task_conflict");

        // 4. Task Report (by unauthorized peer -> expect permission_denied)
        let report_params = serde_json::json!({
            "task_id": "T-TEST-001",
            "summary": "Fixed memory leak",
            "agent_session_id": "session-456"
        });
        let report_err = store.handle_task_report(peer2.clone(), &report_params).unwrap_err();
        assert_eq!(report_err.code, "permission_denied");

        // Task Report (by authorized peer)
        let report_valid_params = serde_json::json!({
            "task_id": "T-TEST-001",
            "summary": "Fixed memory leak",
            "agent_session_id": "session-123"
        });
        let report_res = store.handle_task_report(peer.clone(), &report_valid_params).unwrap();
        assert_eq!(report_res["status"], "review");

        // 5. Task Events
        let events_params = serde_json::json!({ "task_id": "T-TEST-001" });
        let events_res = store.handle_task_events(peer.clone(), &events_params).unwrap();
        let events = events_res["events"].as_array().unwrap();
        assert_eq!(events.len(), 3); // created, claimed, reported
    }
}
