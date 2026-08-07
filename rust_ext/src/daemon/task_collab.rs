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
//! 不建立旁路 `daemon_tasks` 表。schema 由 `crate::sqlite_query::migrate_connection`
//! 官方事务化迁移管理（与 Python `_migrate_schema` 同一版本审计，SCHEMA_VERSION=47），
//! 本模块只做只读校验，不再内嵌旁路 DDL。

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection};
use serde_json::{Map, Value};

use super::dispatch::{DaemonRpcError, PeerCredential};
use crate::sqlite_query::{current_schema_version, migrate_connection, RUST_SCHEMA_VERSION};

// ============================================
// 官方迁移后必须存在的 Task 协同权威表（只读校验清单）
// ============================================

const TASK_COLLAB_TABLES: [&str; 4] = ["tasks", "task_steps", "task_events", "agent_registrations"];

/// daemon 实际读写依赖的列（官方 v47 schema 权威清单，db/schema.py）。
/// 迁移后只读校验这些列存在，防止历史库缺列导致 daemon 查询失败；
/// 该清单不含旁路扩展列（tasks.claimed_by/claimed_at/workspace_id、task_steps.step_number），
/// 因为这些列 daemon 从不读写。
const TASK_COLLAB_COLUMNS: &[(&str, &[&str])] = &[
    (
        "tasks",
        &["id", "title", "description", "creator", "status", "created_at", "updated_at", "parent_id"],
    ),
    (
        "task_steps",
        &["id", "step_index", "action", "target_file", "target_symbol", "check_items", "status", "result", "created_at", "completed_at"],
    ),
    (
        "task_events",
        &["task_id", "workspace_id", "from_status", "to_status", "reason_code", "reason", "actor_identity", "agent_session_id", "role", "contract_hash", "snapshot_id", "monotonic_seq", "authoritative_timestamp", "evidence_path", "evidence_hash"],
    ),
    (
        "agent_registrations",
        &["agent_id", "agent_name", "owner_key", "capabilities", "registered_at", "last_heartbeat", "status"],
    ),
];

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

fn parse_subtasks_from_plan_text(plan_text: &str) -> Vec<(String, String)> {
    let mut items = Vec::new();
    for line in plan_text.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("## ") {
            let title = trimmed.trim_start_matches('#').trim().to_string();
            if !title.is_empty() {
                items.push((title, String::new()));
            }
        } else if trimmed.starts_with("- ") || trimmed.starts_with("* ") || trimmed.starts_with("+ ") {
            let title = trimmed[2..].trim().to_string();
            if !title.is_empty() && items.len() < 20 {
                items.push((title, String::new()));
            }
        }
    }
    if items.is_empty() {
        items.push(("Task Execution Step".to_string(), String::new()));
    }
    items
}

// ============================================
// TaskCollabStore
// ============================================

pub struct TaskCollabStore {
    conn: Arc<Mutex<Connection>>,
    seq_counter: Arc<Mutex<i64>>,
    dedup_cache: Arc<Mutex<HashMap<String, Value>>>,
}

impl TaskCollabStore {
    pub fn new<P: AsRef<Path>>(db_path: P) -> Result<Self, DaemonRpcError> {
        let conn = Connection::open(&db_path).map_err(|e| {
            DaemonRpcError::internal_error(format!("无法打开 Task DB {}: {}", db_path.as_ref().display(), e))
        })?;

        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;").ok();

        Self::migrate_and_verify(&conn)?;

        let max_seq: i64 = conn
            .query_row("SELECT COALESCE(MAX(monotonic_seq), 0) FROM task_events", [], |r| r.get(0))
            .unwrap_or(0);

        Ok(Self {
            conn: Arc::new(Mutex::new(conn)),
            seq_counter: Arc::new(Mutex::new(max_seq)),
            dedup_cache: Arc::new(Mutex::new(HashMap::new())),
        })
    }

    pub fn check_dedup(&self, params: &Value) -> Option<Value> {
        if let Some(req_id) = params.get("request_id").and_then(|v| v.as_str()) {
            if !req_id.is_empty() {
                let cache = self.dedup_cache.lock().unwrap();
                if let Some(res) = cache.get(req_id) {
                    let val: Value = res.clone();
                    return Some(val);
                }
            }
        }
        None
    }

    pub fn save_dedup(&self, params: &Value, result: &Value) {
        if let Some(req_id) = params.get("request_id").and_then(|v| v.as_str()) {
            if !req_id.is_empty() {
                let mut cache = self.dedup_cache.lock().unwrap();
                if cache.len() > 1000 {
                    cache.clear();
                }
                cache.insert(req_id.to_string(), result.clone());
            }
        }
    }

    pub fn from_connection(conn: Connection) -> Result<Self, DaemonRpcError> {
        Self::migrate_and_verify(&conn)?;

        let max_seq: i64 = conn
            .query_row("SELECT COALESCE(MAX(monotonic_seq), 0) FROM task_events", [], |r| r.get(0))
            .unwrap_or(0);

        Ok(Self {
            conn: Arc::new(Mutex::new(conn)),
            seq_counter: Arc::new(Mutex::new(max_seq)),
            dedup_cache: Arc::new(Mutex::new(HashMap::new())),
        })
    }

    /// 执行官方 schema migration 并做只读校验（不含任何旁路 DDL）：
    /// 1. `migrate_connection` 事务化迁移到当前 SCHEMA_VERSION（47，权威 db/schema.py），错误传播
    /// 2. 读取实际 schema version 并与编译期常量 `RUST_SCHEMA_VERSION` 比较，不一致即拒绝服务
    /// 3. 只读校验 4 张 Task 权威表存在（不建表、不写库）
    /// 4. 只读校验 daemon 实际读写的列存在（PRAGMA table_info，替代旧旁路 ALTER 的职责）
    fn migrate_and_verify(conn: &Connection) -> Result<(), DaemonRpcError> {
        // 1. 官方事务化迁移：错误必须传播，禁止 `let _ =` 吞错（吞错会掩盖迁移失败继续服务）
        migrate_connection(conn)
            .map_err(|e| DaemonRpcError::internal_error(format!("官方 schema migration 失败: {}", e)))?;

        // 2. 读取实际 schema 版本并与编译期常量比较（fail-closed：版本不符拒绝服务）
        let actual = current_schema_version(conn)
            .map_err(|e| DaemonRpcError::internal_error(format!("读取 schema_version 失败: {}", e)))?;
        if actual != RUST_SCHEMA_VERSION {
            return Err(DaemonRpcError::internal_error(format!(
                "Task DB schema version 不匹配: 实际 {}, 期望 {}",
                actual, RUST_SCHEMA_VERSION
            )));
        }

        // 3. 只读校验 4 张权威表存在（官方 migration 必须已建齐）
        for table in TASK_COLLAB_TABLES {
            let present: bool = conn
                .query_row(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?1",
                    params![table],
                    |r| r.get::<_, i64>(0).map(|v| v > 0),
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("校验权威表 {} 失败: {}", table, e))
                })?;
            if !present {
                return Err(DaemonRpcError::internal_error(format!(
                    "Task DB 缺少权威表 {}（官方 migration 未建齐 schema）",
                    table
                )));
            }
        }

        // 4. 只读校验 daemon 实际读写列存在（PRAGMA table_info 只读，无写锁）。
        //    官方 v47 schema 已含全部所需列（含 task_events 的 role/contract_hash/snapshot_id/
        //    evidence_path/evidence_hash 与 task_steps.step_index/completed_at），
        //    此处仅做 fail-closed 校验，防止旧库缺列导致 daemon 查询报 "no such column"。
        for (table, required_cols) in TASK_COLLAB_COLUMNS {
            let mut existing: Vec<String> = Vec::new();
            {
                let mut stmt = conn
                    .prepare(&format!("PRAGMA table_info({})", table))
                    .map_err(|e| {
                        DaemonRpcError::internal_error(format!("读取表 {} 结构失败: {}", table, e))
                    })?;
                let rows = stmt
                    .query_map([], |r| r.get::<_, String>(1))
                    .map_err(|e| {
                        DaemonRpcError::internal_error(format!("读取表 {} 列失败: {}", table, e))
                    })?;
                for row in rows {
                    existing.push(row.map_err(|e| {
                        DaemonRpcError::internal_error(format!("读取表 {} 列失败: {}", table, e))
                    })?);
                }
            }
            for col in *required_cols {
                if !existing.iter().any(|c| c == col) {
                    return Err(DaemonRpcError::internal_error(format!(
                        "Task DB 表 {} 缺少列 {}（官方 migration 未补齐 daemon 所需 schema）",
                        table, col
                    )));
                }
            }
        }
        Ok(())
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
        if let Some(cached) = self.check_dedup(params) {
            return Ok(cached);
        }

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
        res.insert("monotonic_seq".to_string(), Value::Number(serde_json::Number::from(seq)));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_claim(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
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
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
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
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
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
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
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

    pub fn handle_task_list(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let status_filter = params.get("status").and_then(|v| v.as_str());
        let limit = params.get("limit").and_then(|v| v.as_u64()).unwrap_or(100) as usize;
        let parent_filter = params.get("parent_id").and_then(|v| v.as_str());

        let conn = self.conn.lock().unwrap();
        let mut query = String::from(
            "SELECT id, title, description, parent_id, status, creator, created_at, updated_at
             FROM tasks WHERE 1=1"
        );
        let mut status_val = String::new();
        let mut parent_val = String::new();
        let mut params_vec: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();

        if let Some(st) = status_filter {
            if !st.is_empty() {
                query.push_str(" AND status = ?");
                status_val = st.to_string();
                params_vec.push(Box::new(status_val.clone()));
            }
        }
        if let Some(pid) = parent_filter {
            query.push_str(" AND parent_id = ?");
            parent_val = pid.to_string();
            params_vec.push(Box::new(parent_val.clone()));
        }

        query.push_str(" ORDER BY created_at DESC LIMIT ?");
        let limit_i64 = limit as i64;
        params_vec.push(Box::new(limit_i64));

        let mut stmt = conn.prepare(&query).map_err(|e| {
            DaemonRpcError::internal_error(format!("prepare task_list 失败: {}", e))
        })?;

        let params_refs: Vec<&dyn rusqlite::ToSql> = params_vec.iter().map(|p| p.as_ref()).collect();

        let rows = stmt.query_map(params_refs.as_slice(), |r| {
            let mut m = Map::new();
            m.insert("task_id".to_string(), Value::String(r.get(0)?));
            m.insert("title".to_string(), Value::String(r.get(1)?));
            m.insert("description".to_string(), Value::String(r.get(2)?));
            m.insert("parent_id".to_string(), Value::String(r.get(3)?));
            m.insert("status".to_string(), Value::String(r.get(4)?));
            m.insert("creator".to_string(), Value::String(r.get(5)?));
            m.insert("created_at".to_string(), Value::Number(serde_json::Number::from_f64(r.get(6)?).unwrap_or(serde_json::Number::from(0))));
            m.insert("updated_at".to_string(), Value::Number(serde_json::Number::from_f64(r.get(7)?).unwrap_or(serde_json::Number::from(0))));
            Ok(Value::Object(m))
        }).map_err(|e| DaemonRpcError::internal_error(format!("query task_list 失败: {}", e)))?;

        let mut tasks = Vec::new();
        for r in rows.flatten() {
            tasks.push(r);
        }

        let mut res = Map::new();
        res.insert("tasks".to_string(), Value::Array(tasks));
        Ok(Value::Object(res))
    }

    pub fn handle_task_rollback(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let reason = params.get("reason").and_then(|v| v.as_str()).unwrap_or("rollback requested");
        let owner_key = peer.owner_key();
        let ts = now_ts();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        let current_status: String = tx
            .query_row("SELECT status FROM tasks WHERE id = ?1", params![task_id], |r| r.get(0))
            .map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)))?;

        tx.execute(
            "UPDATE tasks SET status = 'reverted', updated_at = ?1 WHERE id = ?2",
            params![ts, task_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_rollback 失败: {}", e)))?;

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'reverted', 'rollback', ?3, ?4, ?5, ?6)",
            params![task_id, current_status, reason, owner_key, seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 task_rollback 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String("reverted".to_string()));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_reopen(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let reason = params.get("reason").and_then(|v| v.as_str()).unwrap_or("reopen requested");
        let reviewer = params.get("reviewer").and_then(|v| v.as_str()).unwrap_or("reviewer");
        let owner_key = peer.owner_key();
        let ts = now_ts();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        let current_status: String = tx
            .query_row("SELECT status FROM tasks WHERE id = ?1", params![task_id], |r| r.get(0))
            .map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)))?;

        tx.execute(
            "UPDATE tasks SET status = 'in_progress', updated_at = ?1 WHERE id = ?2",
            params![ts, task_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_reopen 失败: {}", e)))?;

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'in_progress', 'reopened', ?3, ?4, ?5, ?6)",
            params![task_id, current_status, reason, owner_key, seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 task_reopen 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String("in_progress".to_string()));
        res.insert("previous_status".to_string(), Value::String(current_status));
        res.insert("reopened_at".to_string(), Value::Number(serde_json::Number::from_f64(ts).unwrap()));
        res.insert("reviewer".to_string(), Value::String(reviewer.to_string()));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_apply(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let reviewer = params.get("reviewer").and_then(|v| v.as_str()).unwrap_or("reviewer");
        let owner_key = peer.owner_key();
        let ts = now_ts();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        let current_status: String = tx
            .query_row("SELECT status FROM tasks WHERE id = ?1", params![task_id], |r| r.get(0))
            .map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)))?;

        tx.execute(
            "UPDATE tasks SET status = 'applied', updated_at = ?1 WHERE id = ?2",
            params![ts, task_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_apply 失败: {}", e)))?;

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'applied', 'applied', 'task applied', ?3, ?4, ?5)",
            params![task_id, current_status, owner_key, seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 task_apply 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String("applied".to_string()));
        res.insert("applied_at".to_string(), Value::Number(serde_json::Number::from_f64(ts).unwrap()));
        res.insert("reviewer".to_string(), Value::String(reviewer.to_string()));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_close(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let reviewer = params.get("reviewer").and_then(|v| v.as_str()).unwrap_or("reviewer");
        let owner_key = peer.owner_key();
        let ts = now_ts();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        let current_status: String = tx
            .query_row("SELECT status FROM tasks WHERE id = ?1", params![task_id], |r| r.get(0))
            .map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)))?;

        tx.execute(
            "UPDATE tasks SET status = 'closed', updated_at = ?1 WHERE id = ?2",
            params![ts, task_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_close 失败: {}", e)))?;

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'closed', 'closed', 'task closed', ?3, ?4, ?5)",
            params![task_id, current_status, owner_key, seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 task_close 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String("closed".to_string()));
        res.insert("closed_at".to_string(), Value::Number(serde_json::Number::from_f64(ts).unwrap()));
        res.insert("reviewer".to_string(), Value::String(reviewer.to_string()));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_capture_diff(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let step_id = params.get("step_id").and_then(|v| v.as_str()).unwrap_or("");
        let base = params.get("base").and_then(|v| v.as_str()).unwrap_or("HEAD");
        let file_path = params.get("file_path").and_then(|v| v.as_str()).unwrap_or("");
        let owner_key = peer.owner_key();
        let ts = now_ts();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, 'in_progress', 'in_progress', 'diff_captured', ?2, ?3, ?4, ?5)",
            params![task_id, format!("base={}", base), owner_key, seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        if !file_path.is_empty() {
            tx.execute(
                "CREATE TABLE IF NOT EXISTS change_audit (
                    id TEXT PRIMARY KEY, task_id TEXT, file_path TEXT, symbols_json TEXT, created_at REAL
                )",
                [],
            ).ok();
            let audit_id = format!("audit-{}-{}", task_id, seq);
            tx.execute(
                "INSERT OR REPLACE INTO change_audit (id, task_id, file_path, symbols_json, created_at) VALUES (?1, ?2, ?3, '[]', ?4)",
                params![audit_id, task_id, file_path, ts],
            ).ok();
        }

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 task_capture_diff 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("step_id".to_string(), Value::String(step_id.to_string()));
        res.insert("success".to_string(), Value::Bool(true));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_split(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
        let task_id = params.get("task_id").and_then(|v| v.as_str()).unwrap_or("");
        let plan_file = params.get("plan_file").and_then(|v| v.as_str()).unwrap_or("");
        let subtasks_param = params.get("subtasks").and_then(|v| v.as_array());

        let ts = now_ts();
        let mut created_subtasks = Vec::new();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        if let Some(sub_defs) = subtasks_param {
            for (idx, sub_def) in sub_defs.iter().enumerate() {
                let st_title = sub_def.get("title").and_then(|v| v.as_str()).unwrap_or_else(|| "subtask");
                let st_desc = sub_def.get("description").and_then(|v| v.as_str()).unwrap_or("");
                let sub_id = format!("{}-sub-{}", task_id, idx + 1);

                tx.execute(
                    "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at, parent_id)
                     VALUES (?1, ?2, ?3, ?4, 'open', ?5, ?6, ?7)",
                    params![sub_id, st_title, st_desc, peer.owner_key(), ts, ts, task_id],
                ).map_err(|e| DaemonRpcError::internal_error(format!("子任务创建失败: {}", e)))?;

                created_subtasks.push(sub_id);
            }
        } else {
            let plan_text = if !plan_file.is_empty() {
                std::fs::read_to_string(plan_file).unwrap_or_default()
            } else {
                String::new()
            };

            let parsed = parse_subtasks_from_plan_text(&plan_text);
            for (idx, (st_title, st_desc)) in parsed.into_iter().enumerate() {
                let sub_id = format!("{}-sub-{}", task_id, idx + 1);
                tx.execute(
                    "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at, parent_id)
                     VALUES (?1, ?2, ?3, ?4, 'open', ?5, ?6, ?7)",
                    params![sub_id, st_title, st_desc, peer.owner_key(), ts, ts, task_id],
                ).ok();
                created_subtasks.push(sub_id);
            }
        }

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events 
             (task_id, workspace_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, '', 'in_progress', 'in_progress', 'split', ?2, ?3, ?4, ?5)",
            params![task_id, plan_file, peer.owner_key(), seq, ts],
        ).ok();

        tx.commit().map_err(|e| DaemonRpcError::internal_error(format!("提交 task_split 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String("split".to_string()));
        res.insert("subtask_count".to_string(), Value::Number(serde_json::Number::from(created_subtasks.len())));
        res.insert("subtasks".to_string(), Value::Array(created_subtasks.into_iter().map(Value::String).collect()));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_create_from_plan(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
        let title = params.get("title").and_then(|v| v.as_str()).unwrap_or("Root Plan Task");
        let plan_file = params.get("plan_file").and_then(|v| v.as_str()).unwrap_or("");
        let root_task_id = generate_task_id();
        let ts = now_ts();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        tx.execute(
            "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at, parent_id)
             VALUES (?1, ?2, ?3, ?4, 'open', ?5, ?6, '')",
            params![root_task_id, title, plan_file, peer.owner_key(), ts, ts],
        ).map_err(|e| DaemonRpcError::internal_error(format!("建根任务失败: {}", e)))?;

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events (task_id, workspace_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, '', 'none', 'open', 'created_from_plan', ?2, ?3, ?4, ?5)",
            params![root_task_id, plan_file, peer.owner_key(), seq, ts],
        ).ok();

        tx.commit().map_err(|e| DaemonRpcError::internal_error(format!("提交 task_create_from_plan 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("root_task_id".to_string(), Value::String(root_task_id));
        res.insert("plan_file".to_string(), Value::String(plan_file.to_string()));
        res.insert("created".to_string(), Value::Bool(true));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_completion_review(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = peer;
        let task_id = params.get("task_id").and_then(|v| v.as_str()).unwrap_or("");
        let conn = self.conn.lock().unwrap();

        let mut findings = Vec::new();
        let mut has_block = false;

        let mut stmt = conn.prepare(
            "SELECT id, finding_type, severity, message, status FROM task_quality_findings WHERE task_id = ?1 AND status != 'resolved'"
        ).map_err(|e| DaemonRpcError::internal_error(format!("查询 findings 失败: {}", e)))?;

        let rows = stmt.query_map(params![task_id], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1).unwrap_or_default(),
                row.get::<_, String>(2).unwrap_or_default(),
                row.get::<_, String>(3).unwrap_or_default(),
                row.get::<_, String>(4).unwrap_or_default(),
            ))
        }).ok();

        if let Some(rows_iter) = rows {
            for item in rows_iter.flatten() {
                if item.2 == "error" || item.2 == "block" {
                    has_block = true;
                }
                let mut obj = Map::new();
                obj.insert("id".to_string(), Value::Number(serde_json::Number::from(item.0)));
                obj.insert("finding_type".to_string(), Value::String(item.1));
                obj.insert("severity".to_string(), Value::String(item.2));
                obj.insert("message".to_string(), Value::String(item.3));
                obj.insert("status".to_string(), Value::String(item.4));
                findings.push(Value::Object(obj));
            }
        }

        let decision = if has_block { "block" } else { "pass" };
        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("decision".to_string(), Value::String(decision.to_string()));
        res.insert("findings".to_string(), Value::Array(findings));
        Ok(Value::Object(res))
    }

    pub fn handle_task_resolve_quality_finding(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
        let finding_id = params.get("finding_id").and_then(|v| v.as_i64()).unwrap_or(0);
        let resolution = params.get("resolution").and_then(|v| v.as_str()).unwrap_or("fixed");

        let conn = self.conn.lock().unwrap();
        let updated = conn.execute(
            "UPDATE task_quality_findings SET status = ?1 WHERE id = ?2",
            params![resolution, finding_id],
        ).map_err(|e| DaemonRpcError::internal_error(format!("更新 finding 状态失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("finding_id".to_string(), Value::Number(serde_json::Number::from(finding_id)));
        res.insert("status".to_string(), Value::String(resolution.to_string()));
        res.insert("updated".to_string(), Value::Bool(updated > 0));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_create_subtask(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
        let parent_id = params.get("parent_task_id").and_then(|v| v.as_str()).unwrap_or("");
        let title = params.get("title").and_then(|v| v.as_str()).unwrap_or("subtask");
        let task_id = generate_task_id();
        let ts = now_ts();
        let seq = self.next_seq();

        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO tasks (id, title, creator, status, created_at, updated_at, parent_id) VALUES (?1, ?2, ?3, 'open', ?4, ?5, ?6)",
            params![task_id, title, peer.owner_key(), ts, ts, parent_id],
        ).map_err(|e| DaemonRpcError::internal_error(format!("子任务写入失败: {}", e)))?;

        conn.execute(
            "INSERT INTO task_events (task_id, workspace_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp) VALUES (?1, '', 'none', 'open', 'subtask_created', ?2, ?3, ?4, ?5)",
            params![task_id, title, peer.owner_key(), seq, ts],
        ).ok();

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id));
        res.insert("parent_id".to_string(), Value::String(parent_id.to_string()));
        res.insert("status".to_string(), Value::String("open".to_string()));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_status_tree(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = peer;
        let task_id = params.get("task_id").and_then(|v| v.as_str()).unwrap_or("");
        if task_id.is_empty() {
            return Err(DaemonRpcError::invalid_params("缺少 task_id"));
        }
        let conn = self.conn.lock().unwrap();
        let node = build_task_tree_node(&conn, task_id);
        if node.is_null() {
            return Err(DaemonRpcError::new(
                "task_not_found",
                format!("任务不存在: {}", task_id),
            ));
        }
        Ok(node)
    }

    pub fn handle_task_record_symbol_change(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
        let _ = peer;
        let task_id = params.get("task_id").and_then(|v| v.as_str()).unwrap_or("");
        let file_path = params.get("file_path").and_then(|v| v.as_str()).unwrap_or("");
        let symbol_hash = params.get("symbol_hash").and_then(|v| v.as_str()).unwrap_or("");
        let symbol_hash_before = params.get("symbol_hash_before").and_then(|v| v.as_str()).unwrap_or("");
        let hash_after = if !symbol_hash.is_empty() { symbol_hash } else { symbol_hash_before };
        let change_type = params.get("change_type").and_then(|v| v.as_str()).unwrap_or("modified");
        let ts = now_ts();

        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO task_symbol_changes (task_id, file_path, symbol_hash_after, symbol_hash_before, change_type, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![task_id, file_path, hash_after, symbol_hash_before, change_type, ts],
        ).map_err(|e| DaemonRpcError::internal_error(format!("写入 task_symbol_changes 失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("file_path".to_string(), Value::String(file_path.to_string()));
        res.insert("recorded".to_string(), Value::Bool(true));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_link_edit_audit_symbols(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
        let _ = peer;
        let task_id = params.get("task_id").and_then(|v| v.as_str()).unwrap_or("");
        let file_path = params.get("file_path").and_then(|v| v.as_str()).unwrap_or("");
        let symbols = params.get("symbol_hashes").map(|v| v.to_string()).unwrap_or_else(|| "[]".to_string());
        let ts = now_ts();
        let audit_id = format!("audit-{}-{}", task_id, now_ts() as i64);

        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT OR REPLACE INTO change_audit (id, task_id, file_path, symbols_json, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![audit_id, task_id, file_path, symbols, ts],
        ).map_err(|e| DaemonRpcError::internal_error(format!("写入 change_audit 失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("audit_id".to_string(), Value::String(audit_id));
        res.insert("linked".to_string(), Value::Bool(true));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_get_symbol_changes(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = peer;
        let task_id = params.get("task_id").and_then(|v| v.as_str()).unwrap_or("");
        let conn = self.conn.lock().unwrap();

        let mut changes = Vec::new();
        let mut stmt = conn.prepare(
            "SELECT file_path, symbol_hash_after, symbol_hash_before, change_type, created_at FROM task_symbol_changes WHERE task_id = ?1"
        ).ok();

        if let Some(ref mut st) = stmt {
            let rows = st.query_map(params![task_id], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, f64>(4)?,
                ))
            }).ok();

            if let Some(iter) = rows {
                for item in iter.flatten() {
                    let mut obj = Map::new();
                    obj.insert("file_path".to_string(), Value::String(item.0));
                    obj.insert("symbol_hash".to_string(), Value::String(item.1));
                    obj.insert("symbol_hash_before".to_string(), Value::String(item.2));
                    obj.insert("change_type".to_string(), Value::String(item.3));
                    obj.insert("created_at".to_string(), Value::Number(serde_json::Number::from_f64(item.4).unwrap()));
                    changes.push(Value::Object(obj));
                }
            }
        }

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("changes".to_string(), Value::Array(changes));
        Ok(Value::Object(res))
    }

    pub fn handle_task_quality_findings(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params.get("task_id").and_then(|v| v.as_str()).unwrap_or("");
        let status = params.get("status").and_then(|v| v.as_str()).unwrap_or("");
        let severity = params.get("severity").and_then(|v| v.as_str()).unwrap_or("");

        let mut sql = String::from(
            "SELECT id, task_id, step_id, finding_type, severity, message, details, status, created_at, resolved_at
             FROM task_quality_findings WHERE task_id = ?1",
        );
        let mut bind: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
        bind.push(Box::new(task_id.to_string()));
        if !status.is_empty() && status != "all" {
            sql.push_str(" AND status = ?");
            bind.push(Box::new(status.to_string()));
        }
        if !severity.is_empty() {
            sql.push_str(" AND severity = ?");
            bind.push(Box::new(severity.to_string()));
        }
        sql.push_str(" ORDER BY created_at ASC");

        let conn = self.conn.lock().unwrap();
        let mut stmt = conn
            .prepare(&sql)
            .map_err(|e| DaemonRpcError::internal_error(format!("prepare quality_findings 失败: {}", e)))?;
        let bind_refs: Vec<&dyn rusqlite::ToSql> = bind.iter().map(|b| b.as_ref()).collect();

        let rows = stmt
            .query_map(bind_refs.as_slice(), |r| {
                let mut m = Map::new();
                m.insert("id".to_string(), Value::Number(r.get::<_, i64>(0)?.into()));
                m.insert("task_id".to_string(), Value::String(r.get(1)?));
                m.insert("step_id".to_string(), Value::Number(r.get::<_, i64>(2)?.into()));
                m.insert("finding_type".to_string(), Value::String(r.get(3)?));
                m.insert("severity".to_string(), Value::String(r.get(4)?));
                m.insert("message".to_string(), Value::String(r.get(5)?));
                m.insert("details".to_string(), Value::String(r.get(6)?));
                m.insert("status".to_string(), Value::String(r.get(7)?));
                m.insert("created_at".to_string(), Value::Number(serde_json::Number::from_f64(r.get(8)?).unwrap()));
                m.insert("resolved_at".to_string(), Value::Number(serde_json::Number::from_f64(r.get(9)?).unwrap()));
                Ok(Value::Object(m))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 quality_findings 失败: {}", e)))?;

        let mut findings = Vec::new();
        for r in rows.flatten() {
            findings.push(r);
        }

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("findings".to_string(), Value::Array(findings));
        Ok(Value::Object(res))
    }

    pub fn handle_task_has_blocking_findings(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params.get("task_id").and_then(|v| v.as_str()).unwrap_or("");
        let conn = self.conn.lock().unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_quality_findings
                 WHERE task_id = ?1 AND status = 'open' AND severity IN ('error', 'block')",
                params![task_id],
                |r| r.get(0),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 has_blocking_findings 失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("has_blocking".to_string(), Value::Bool(count > 0));
        Ok(Value::Object(res))
    }

    pub fn handle_task_get_commits(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params.get("task_id").and_then(|v| v.as_str()).unwrap_or("");
        let conn = self.conn.lock().unwrap();

        let mut commits = Vec::new();
        if let Ok(mut stmt) = conn.prepare(
            "SELECT tsc.source_commit_hash,
                    COUNT(*) AS change_count,
                    MIN(tsc.created_at) AS first_change_at,
                    MAX(tsc.created_at) AS last_change_at,
                    COALESCE(gc.author, '') AS commit_author,
                    COALESCE(gc.message, '') AS commit_message,
                    COALESCE(gc.timestamp, 0) AS commit_timestamp
             FROM task_symbol_changes tsc
             LEFT JOIN git_commits gc ON tsc.source_commit_hash = gc.commit_hash
             WHERE tsc.task_id = ?1 AND tsc.source_commit_hash != ''
             GROUP BY tsc.source_commit_hash
             ORDER BY last_change_at DESC",
        ) {
            if let Ok(rows) = stmt.query_map(params![task_id], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, f64>(2)?,
                    r.get::<_, f64>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, String>(5)?,
                    r.get::<_, f64>(6)?,
                ))
            }) {
                for item in rows.flatten() {
                    let msg = item.5.clone();
                    let subject = msg.lines().next().unwrap_or("").to_string();
                    let mut obj = Map::new();
                    obj.insert("source_commit_hash".to_string(), Value::String(item.0));
                    obj.insert("change_count".to_string(), Value::Number(serde_json::Number::from(item.1)));
                    obj.insert("first_change_at".to_string(), Value::Number(serde_json::Number::from_f64(item.2).unwrap()));
                    obj.insert("last_change_at".to_string(), Value::Number(serde_json::Number::from_f64(item.3).unwrap()));
                    obj.insert("commit_author".to_string(), Value::String(item.4));
                    obj.insert("commit_message".to_string(), Value::String(item.5));
                    obj.insert("commit_timestamp".to_string(), Value::Number(serde_json::Number::from_f64(item.6).unwrap()));
                    obj.insert("commit_subject".to_string(), Value::String(subject));
                    commits.push(Value::Object(obj));
                }
            }
        }

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("commits".to_string(), Value::Array(commits));
        Ok(Value::Object(res))
    }
}

/// 递归构建任务树节点（与本地 db.task_status_tree 返回结构对齐）。
/// 返回 Value::Null 表示任务不存在。
fn build_task_tree_node(conn: &Connection, task_id: &str) -> Value {
    let row = conn
        .query_row(
            "SELECT id, title, description, status, COALESCE(creator, ''), COALESCE(depth, 0), COALESCE(sort_order, 0),
                    COALESCE(created_at, 0), COALESCE(updated_at, 0), COALESCE(closed_at, 0)
             FROM tasks WHERE id = ?1",
            params![task_id],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, i64>(5)?,
                    r.get::<_, i64>(6)?,
                    r.get::<_, f64>(7)?,
                    r.get::<_, f64>(8)?,
                    r.get::<_, f64>(9)?,
                ))
            },
        )
        .ok();
    let Some(row) = row else {
        return Value::Null;
    };

    // 认领信息（与 handle_task_status 一致：取最近一次进入 in_progress 的 session）
    let claimed_by = conn
        .query_row(
            "SELECT agent_session_id FROM task_events
             WHERE task_id = ?1 AND to_status = 'in_progress'
             ORDER BY monotonic_seq DESC LIMIT 1",
            params![task_id],
            |r| r.get::<_, String>(0),
        )
        .unwrap_or_default();

    // 自身步骤
    let mut steps = Vec::new();
    let mut step_total = 0i64;
    let mut step_done = 0i64;
    if let Ok(mut stmt) = conn.prepare(
        "SELECT id, step_index, action, target_file, target_symbol, check_items, status, result, created_at, completed_at
         FROM task_steps WHERE task_id = ?1 ORDER BY step_index ASC",
    ) {
        if let Ok(rows) = stmt.query_map(params![task_id], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
                r.get::<_, String>(4)?,
                r.get::<_, String>(5)?,
                r.get::<_, String>(6)?,
                r.get::<_, String>(7)?,
                r.get::<_, f64>(8)?,
                r.get::<_, f64>(9)?,
            ))
        }) {
            for s in rows.flatten() {
                step_total += 1;
                if s.6 == "done" || s.6 == "skipped" {
                    step_done += 1;
                }
                let mut m = Map::new();
                m.insert("step_id".to_string(), Value::Number(serde_json::Number::from(s.0)));
                m.insert("step_index".to_string(), Value::Number(serde_json::Number::from(s.1)));
                m.insert("action".to_string(), Value::String(s.2));
                m.insert("target_file".to_string(), Value::String(s.3));
                m.insert("target_symbol".to_string(), Value::String(s.4));
                m.insert("check_items".to_string(), Value::String(s.5));
                m.insert("status".to_string(), Value::String(s.6));
                m.insert("result".to_string(), Value::String(s.7));
                m.insert("created_at".to_string(), Value::Number(serde_json::Number::from_f64(s.8).unwrap()));
                m.insert("completed_at".to_string(), Value::Number(serde_json::Number::from_f64(s.9).unwrap()));
                steps.push(Value::Object(m));
            }
        }
    }

    // 直接子任务 ID 列表（先收集再递归，避免 stmt 借用 conn）
    let mut child_ids: Vec<String> = Vec::new();
    if let Ok(mut stmt) = conn.prepare("SELECT id FROM tasks WHERE parent_id = ?1 ORDER BY sort_order ASC") {
        if let Ok(rows) = stmt.query_map(params![task_id], |r| r.get::<_, String>(0)) {
            for cid in rows.flatten() {
                child_ids.push(cid);
            }
        }
    }

    // 递归子任务 + 进度累加
    let mut subtasks = Vec::new();
    let mut total = step_total;
    let mut done = step_done;
    for cid in &child_ids {
        let sub = build_task_tree_node(conn, cid);
        if sub.is_null() {
            continue;
        }
        if let Some(obj) = sub.as_object() {
            if let Some(pr) = obj.get("progress") {
                total += pr.get("total").and_then(|v| v.as_i64()).unwrap_or(0);
                done += pr.get("done").and_then(|v| v.as_i64()).unwrap_or(0);
            }
        }
        subtasks.push(sub);
    }

    let progress = if total > 0 { done as f64 / total as f64 } else { 0.0 };

    let mut res = Map::new();
    res.insert("task_id".to_string(), Value::String(row.0));
    res.insert("title".to_string(), Value::String(row.1));
    res.insert("description".to_string(), Value::String(row.2));
    res.insert("status".to_string(), Value::String(row.3));
    res.insert("creator".to_string(), Value::String(row.4));
    res.insert("claimed_by".to_string(), Value::String(claimed_by));
    res.insert("depth".to_string(), Value::Number(serde_json::Number::from(row.5)));
    res.insert("sort_order".to_string(), Value::Number(serde_json::Number::from(row.6)));
    res.insert("created_at".to_string(), Value::Number(serde_json::Number::from_f64(row.7).unwrap()));
    res.insert("updated_at".to_string(), Value::Number(serde_json::Number::from_f64(row.8).unwrap()));
    res.insert("closed_at".to_string(), Value::Number(serde_json::Number::from_f64(row.9).unwrap()));
    let mut prog = Map::new();
    prog.insert("total".to_string(), Value::Number(serde_json::Number::from(total)));
    prog.insert("done".to_string(), Value::Number(serde_json::Number::from(done)));
    prog.insert("progress".to_string(), Value::Number(serde_json::Number::from_f64(progress).unwrap()));
    res.insert("progress".to_string(), Value::Object(prog));
    res.insert("steps".to_string(), Value::Array(steps));
    res.insert("subtasks".to_string(), Value::Array(subtasks));
    Value::Object(res)
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

    #[test]
    fn test_task_collab_migrates_v46_db_to_v47() {
        // P1 修复：v46 旧库（无 task_events/agent_registrations、schema_version=46）
        // 打开后必须走官方 migration 升级到 v47 并补齐权威任务表，完整 task RPC 可用
        let (_dir, db_path) = temp_db();

        // 1. 先建一个 v47 库，再人为降级为 v46（模拟旧版库形态）
        {
            let store = TaskCollabStore::new(&db_path).unwrap();
            drop(store);
        }
        {
            let conn = Connection::open(&db_path).unwrap();
            conn.execute_batch(
                "DROP TABLE IF EXISTS task_events;
                 DROP TABLE IF EXISTS agent_registrations;
                 DROP INDEX IF EXISTS idx_task_events_task;
                 UPDATE schema_version SET version = 46 WHERE version >= 47;",
            )
            .unwrap();
            let v: i64 = conn
                .query_row("SELECT COALESCE(MAX(version),0) FROM schema_version", [], |r| r.get(0))
                .unwrap();
            assert_eq!(v, 46);
        }

        // 2. 用真实 store 打开迁移后的库，验证完整 task RPC 可用
        let store = TaskCollabStore::new(&db_path).unwrap();

        // 3. 校验实际 schema version == 47（不再依赖编译时常量，读真实 schema_version 表）
        let conn = Connection::open(&db_path).unwrap();
        let v: i64 = conn
            .query_row("SELECT COALESCE(MAX(version),0) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, RUST_SCHEMA_VERSION);
        // 4. 权威任务表已被官方 migration 补齐
        for table in TASK_COLLAB_TABLES {
            let present: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?1",
                    params![table],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(present, 1, "权威表 {} 未补齐", table);
        }

        // 5. 完整 task RPC 可用（创建 → 查询）
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let create_params = serde_json::json!({
            "title": "upgrade from v46",
            "task_id": "T-V46-001"
        });
        let create_res = store.handle_task_create(peer.clone(), &create_params).unwrap();
        assert_eq!(create_res["status"], "open");
        let events_params = serde_json::json!({ "task_id": "T-V46-001" });
        let events_res = store.handle_task_events(peer, &events_params).unwrap();
        assert_eq!(events_res["events"].as_array().unwrap().len(), 1);
    }
}
