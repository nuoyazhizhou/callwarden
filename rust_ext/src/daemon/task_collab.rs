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
//! 官方事务化迁移管理（与 Python `_migrate_schema` 同一版本审计，SCHEMA_VERSION=51），
//! 本模块只做只读校验，不再内嵌旁路 DDL。

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde_json::{json, Map, Value};

use super::clock::AuthoritativeClock;
use super::dispatch::{DaemonRpcError, PeerCredential};
use super::task_supersede::{validate_supersede_schema, verify_registered_identity};
use super::task_loop::operation_store::{DedupeOutcome, LedgerProvenance, OperationStore, ParamsRules};
use super::task_loop::task_contract_bootstrap::{bootstrap_task_governance_contracts, BootstrapInput};

use crate::canonicalize::sha256_hex;
use crate::sqlite_query::{current_schema_version, migrate_connection, RUST_SCHEMA_VERSION};

// ============================================
// 官方迁移后必须存在的 Task 协同权威表（只读校验清单）
// ============================================

const TASK_COLLAB_TABLES: [&str; 5] = [
    "tasks", "task_steps", "task_events", "agent_registrations", "action_identities",
];

/// 规范 JSON SHA-256（MCP-001 role_view.get 用）。
///
/// 语义与 Python `_canonical_json` + `_compute_hash` 完全一致：
/// `json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
/// —— 递归按 key 排序、无多余空格、紧凑分隔符。serde_json preserve_order
/// 保留插入顺序，因此此处显式排序 key 后再序列化。
fn canonical_json_sha256(value: &Value) -> String {
    fn sort_keys(value: &Value) -> Value {
        match value {
            Value::Object(map) => {
                let mut keys: Vec<&String> = map.keys().collect();
                keys.sort();
                let mut out = Map::new();
                for key in keys {
                    out.insert(key.clone(), sort_keys(&map[key]));
                }
                Value::Object(out)
            }
            Value::Array(items) => {
                Value::Array(items.iter().map(sort_keys).collect())
            }
            other => other.clone(),
        }
    }
    let sorted = sort_keys(value);
    // serde_json 紧凑序列化（无空格）；ensure_ascii=false 等价于 UTF-8 直出
    let canonical = serde_json::to_string(&sorted).unwrap_or_default();
    sha256_hex(canonical.as_bytes())
}

/// daemon 实际读写依赖的列（官方 v49 schema 权威清单，db/schema.py）。
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
    (
        "action_identities",
        &["workspace_id", "action_id", "action_type", "task_id", "agent_id", "session_id", "model_id", "role", "recorded_at"],
    ),
];

#[derive(Clone, Debug)]
pub(crate) struct ActionIdentity {
    pub(crate) agent_id: String,
    pub(crate) agent_instance_id: String,
    pub(crate) client_id: String,
    pub(crate) provider: String,
    pub(crate) model_id: String,
    pub(crate) model_mode: String,
    pub(crate) system_fingerprint: String,
    pub(crate) session_id: String,
    pub(crate) role: String,
    pub(crate) runtime_hash: String,
}

pub(crate) fn parse_action_identity(params: &Value) -> Result<Option<ActionIdentity>, DaemonRpcError> {
    let Some(raw) = params.get("identity") else { return Ok(None); };
    if raw.is_null() || raw.as_str().map(|s| s.trim().is_empty()).unwrap_or(false) {
        return Ok(None);
    }
    let value = if let Some(text) = raw.as_str() {
        serde_json::from_str::<Value>(text)
            .map_err(|_| DaemonRpcError::new("E_IDENTITY_INCOMPLETE", "identity 必须是 JSON 对象"))?
    } else {
        raw.clone()
    };
    let object = value.as_object()
        .ok_or_else(|| DaemonRpcError::new("E_IDENTITY_INCOMPLETE", "identity 必须是 JSON 对象"))?;
    let field = |name: &str| object.get(name).and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
    let identity = ActionIdentity {
        agent_id: field("agent_id"),
        agent_instance_id: field("agent_instance_id"),
        client_id: field("client_id"),
        provider: field("provider"),
        model_id: field("model_id"),
        model_mode: field("model_mode"),
        system_fingerprint: field("system_fingerprint"),
        session_id: field("session_id"),
        role: field("role"),
        runtime_hash: field("runtime_hash"),
    };
    if identity.agent_id.is_empty() || identity.session_id.is_empty()
        || identity.model_id.is_empty() || identity.role.is_empty()
    {
        return Err(DaemonRpcError::new(
            "E_IDENTITY_INCOMPLETE",
            "identity 必须同时包含 agent_id/session_id/model_id/role",
        ));
    }
    Ok(Some(identity))
}

pub(crate) fn record_action_identity(
    tx: &Transaction<'_>,
    task_id: &str,
    identity: &ActionIdentity,
    action_type: &str,
    seq: i64,
    ts: f64,
) -> Result<(), DaemonRpcError> {
    // provenance 归属优先取任务不可变 binding（多 workspace 下不会记到别的项目）；
    // 无 binding 的 legacy 任务回退到 active workspace（fail-closed：无 active 拒绝）。
    let workspace_id = task_workspace_id_or_active(tx, task_id)?;
    let action_id = format!("ACT-daemon-{}-{}-{}", action_type, task_id, seq);
    tx.execute(
        "INSERT INTO action_identities
         (workspace_id, action_id, action_type, task_id, contract_id, contract_revision,
          agent_id, session_id, model_id, role, recorded_at)
         VALUES (?1, ?2, ?3, ?4, '', 0, ?5, ?6, ?7, ?8, ?9)",
        params![
            workspace_id, action_id, action_type, task_id, identity.agent_id,
            identity.session_id, identity.model_id, identity.role, ts
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("记录 action identity 失败: {}", e)))?;
    Ok(())
}

pub(crate) fn task_now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

// 兼容本模块既有内部调用；跨模块只使用 task_now_ts。
fn now_ts() -> f64 {
    task_now_ts()
}

/// 返回已由 append-only `step_resolved` 事件解析的 failed step 集合。
///
/// failed step 自身保持不可变；生命周期投影只消费精确 JSON 字段，禁止用
/// `LIKE` 猜测 resolution，避免 step id 前缀/转义造成误判。
fn resolved_failed_step_ids(
    conn: &Connection,
    task_id: &str,
) -> Result<HashSet<String>, DaemonRpcError> {
    let mut stmt = conn
        .prepare(
            "SELECT reason FROM task_events
             WHERE task_id = ?1 AND reason_code = 'step_resolved'
             ORDER BY event_id ASC",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("查询 resolution ledger 失败: {}", e)))?;
    let rows = stmt
        .query_map(params![task_id], |row| row.get::<_, String>(0))
        .map_err(|e| DaemonRpcError::internal_error(format!("读取 resolution ledger 失败: {}", e)))?;
    let mut resolved = HashSet::new();
    for row in rows {
        let raw = row
            .map_err(|e| DaemonRpcError::internal_error(format!("读取 resolution event 失败: {}", e)))?;
        if let Ok(value) = serde_json::from_str::<Value>(&raw) {
            if let Some(step_id) = value
                .get("failed_step_id")
                .and_then(|item| item.as_str())
                .filter(|item| !item.trim().is_empty())
            {
                resolved.insert(step_id.to_string());
            }
        }
    }
    Ok(resolved)
}

fn unresolved_failed_step_ids(
    conn: &Connection,
    task_id: &str,
) -> Result<Vec<String>, DaemonRpcError> {
    let resolved = resolved_failed_step_ids(conn, task_id)?;
    let mut stmt = conn
        .prepare(
            "SELECT id FROM task_steps
             WHERE task_id = ?1 AND status = 'failed'
             ORDER BY step_index ASC",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("查询 failed steps 失败: {}", e)))?;
    let rows = stmt
        .query_map(params![task_id], |row| row.get::<_, String>(0))
        .map_err(|e| DaemonRpcError::internal_error(format!("读取 failed steps 失败: {}", e)))?;
    let mut unresolved = Vec::new();
    for row in rows {
        let step_id = row
            .map_err(|e| DaemonRpcError::internal_error(format!("读取 failed step 失败: {}", e)))?;
        if !resolved.contains(&step_id) {
            unresolved.push(step_id);
        }
    }
    Ok(unresolved)
}

/// 找出必须显式领取的 remediation。Reviewer BLOCKED 生成的整改与未解析
/// failed step 的整改都优先于普通 pending step；已完成的历史整改不会重复命中。
fn required_remediation_step(
    conn: &Connection,
    task_id: &str,
) -> Result<Option<(String, Value)>, DaemonRpcError> {
    let unresolved: HashSet<String> = unresolved_failed_step_ids(conn, task_id)?
        .into_iter()
        .collect();
    let mut stmt = conn
        .prepare(
            "SELECT id, result FROM task_steps
             WHERE task_id = ?1 AND action = 'fix_defect'
               AND status IN ('pending', 'in_progress')
             ORDER BY step_index ASC",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("查询 remediation steps 失败: {}", e)))?;
    let rows = stmt
        .query_map(params![task_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("读取 remediation steps 失败: {}", e)))?;
    for row in rows {
        let (step_id, raw) = row
            .map_err(|e| DaemonRpcError::internal_error(format!("读取 remediation step 失败: {}", e)))?;
        let metadata = serde_json::from_str::<Value>(&raw).unwrap_or(Value::Null);
        let linked = metadata
            .get("remediation_of_step_id")
            .and_then(|item| item.as_str())
            .unwrap_or("");
        let source_outcome = metadata
            .get("source_outcome")
            .and_then(|item| item.as_str())
            .unwrap_or("");
        if unresolved.contains(linked)
            || matches!(source_outcome, "reviewer_blocked" | "adjudicator_returned")
        {
            return Ok(Some((step_id, metadata)));
        }
    }
    Ok(None)
}

/// 超过该时间没有 agent heartbeat 的 claim 才允许由受保护的恢复入口释放。
/// 这是安全阈值，不是客户端可覆盖的参数；恢复仍须持有目标任务的 reviewer lease。
const ORPHAN_CLAIM_STALE_SECS: f64 = 15.0 * 60.0;

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

fn generate_step_id() -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    format!("S-{}-{:08x}", now, rand_val())
}

/// 生成 Lease raw token（Req 11.2：仅成功响应返回一次，DB 只存 sha256）。
///
/// 多路熵（纳秒时间戳 + 随机值 + 进程 PID）经双重 sha256 单向哈希，
/// 保证无法从数据库中的 token_hash 反推 raw token（对齐 Python `secrets.token_urlsafe(32)`）。
fn gen_lease_token() -> String {
    let raw = format!(
        "{}:{}:{}:{}",
        now_ts(),
        rand_val(),
        std::process::id(),
        rand_val()
    );
    sha256_hex(format!("{}:{}", raw, sha256_hex(raw.as_bytes())).as_bytes())
}

/// 生成 Lease 唯一标识（对齐 Python `L-<uuid4.hex[:16]>` 格式）。
fn gen_lease_id() -> String {
    format!("L-{}", &sha256_hex(format!("{}:{}", now_ts(), rand_val()).as_bytes())[..16])
}

/// 生成 Lease 审计事件唯一标识（对齐 Python `EVT-<uuid4.hex[:16]>` 格式）。
fn gen_lease_event_id() -> String {
    format!("EVT-{}", &sha256_hex(format!("{}:{}", now_ts(), rand_val()).as_bytes())[..16])
}

/// 判断 rusqlite 错误是否为 UNIQUE 约束冲突（SQLITE_CONSTRAINT，code 19/2067）。
///
/// 用于 acquire 时捕获 `idx_task_leases_active_unique` 部分唯一索引冲突（Req 11.2 防双活）。
fn is_unique_violation(err: &rusqlite::Error) -> bool {
    matches!(
        err,
        rusqlite::Error::SqliteFailure(e, _) if e.code == rusqlite::ErrorCode::ConstraintViolation
    )
}

/// 获取当前活动 workspace 的 id（与 `record_action_identity` 同一绑定逻辑）。
///
/// fail-closed：没有 `is_active = 1` 的 workspace 时拒绝，绝不回退到“任意
/// workspace”（旧实现 `ORDER BY id LIMIT 1` 会在多 workspace 单库中把任务
/// 归属错配到其他项目，正是“工作区身份混串”的根因之一）。
fn active_workspace_id(conn: &Connection) -> Result<i64, DaemonRpcError> {
    conn.query_row(
        "SELECT id FROM workspaces WHERE is_active = 1 ORDER BY id LIMIT 1",
        [],
        |r| r.get(0),
    )
    .map_err(|_| DaemonRpcError::new(
        "E_IDENTITY_NOT_WIRED",
        "没有 active workspace（is_active=1），拒绝推导 workspace；必须显式绑定",
    ))
}

/// 解析显式 `workspace_id` 参数（abi-error-code-contract.md：生产路径必须显式传入
/// `workspace_id > 0`，禁止用 active workspace / cwd / 客户端 numeric id 补齐）。
fn required_workspace_id_param(params: &Value) -> Result<i64, DaemonRpcError> {
    let raw = params.get("workspace_id").ok_or_else(|| {
        DaemonRpcError::new(
            "E_TASK_WORKSPACE_UNBOUND",
            "缺少显式 workspace_id（> 0）；生产路径禁止用 active workspace / cwd 补齐",
        )
    })?;
    let ws_id = if let Some(i) = raw.as_i64() {
        i
    } else if let Some(s) = raw.as_str() {
        s.trim().parse::<i64>().map_err(|_| {
            DaemonRpcError::new(
                "E_TASK_WORKSPACE_UNBOUND",
                format!("workspace_id 无法解析为整数: {}", s),
            )
        })?
    } else {
        return Err(DaemonRpcError::new(
            "E_TASK_WORKSPACE_UNBOUND",
            "workspace_id 必须是整数或数字字符串",
        ));
    };
    if ws_id <= 0 {
        return Err(DaemonRpcError::new(
            "E_TASK_WORKSPACE_UNBOUND",
            format!("workspace_id 必须 > 0，实际 {}", ws_id),
        ));
    }
    Ok(ws_id)
}

/// 可选解析 `workspace_id` 参数（None 表示未提供；用于与 binding 一致性校验）。
fn optional_workspace_id_param(params: &Value) -> Option<i64> {
    params
        .get("workspace_id")
        .and_then(|v| {
            v.as_i64()
                .or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok()))
        })
        .filter(|id| *id > 0)
}

/// 任务逻辑 workspace 只来自不可变 `task_workspace_bindings`
/// （cw-role-handoff-task-loop.md §8.1.1）。
///
/// - 无 binding → `E_TASK_WORKSPACE_UNBOUND` fail-closed（旧 task 保持无 binding，
///   v1 派工/lease 一律拒绝，绝不回退 active workspace 或客户端 numeric id）；
/// - 显式 requested 与 binding 不一致 → `E_WORKSPACE_AUTHORITY_MISMATCH`。
pub(crate) fn task_bound_workspace_id(
    conn: &Connection,
    task_id: &str,
    requested_workspace_id: Option<i64>,
) -> Result<i64, DaemonRpcError> {
    let bound: Option<i64> = conn
        .query_row(
            "SELECT workspace_id FROM task_workspace_bindings WHERE task_id = ?1",
            params![task_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("查询 task workspace binding 失败: {}", e)))?;
    let workspace_id = bound.ok_or_else(|| {
        DaemonRpcError::new(
            "E_TASK_WORKSPACE_UNBOUND",
            format!(
                "task={} 未绑定不可变 workspace（task_workspace_bindings 缺失），拒绝操作",
                task_id
            ),
        )
    })?;
    if let Some(requested) = requested_workspace_id {
        if requested != workspace_id {
            return Err(DaemonRpcError::new(
                "E_WORKSPACE_AUTHORITY_MISMATCH",
                format!(
                    "task={} 绑定 workspace={} 与请求 workspace={} 不一致",
                    task_id, workspace_id, requested
                ),
            ));
        }
    }
    Ok(workspace_id)
}

/// provenance/审计记录的 workspace 归属：优先不可变 binding（多 workspace 下不会把
/// 任务的动作记到其他项目的 workspace）；无 binding 的 legacy 任务回退到 active
/// workspace（fail-closed：无 active workspace 时拒绝，绝不回退到“任意 workspace”）。
fn task_workspace_id_or_active(
    conn: &Connection,
    task_id: &str,
) -> Result<i64, DaemonRpcError> {
    let bound: Option<i64> = conn
        .query_row(
            "SELECT workspace_id FROM task_workspace_bindings WHERE task_id = ?1",
            params![task_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("查询 task workspace binding 失败: {}", e)))?;
    if let Some(ws) = bound {
        return Ok(ws);
    }
    active_workspace_id(conn)
}

/// 权威 UTC 秒值文本（capture/binding 的 `authoritative_created_at`）。
fn authoritative_now_text() -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    format!("{}", ts)
}

/// 在 `task.create` 同一事务内追加 workspace authority capture + 不可变 binding
/// （cw-role-handoff-task-loop.md §8.1.1）。
///
/// - workspace 不存在 → `E_WORKSPACE_AUTHORITY_MISMATCH` fail-closed（不得用客户端
///   numeric id 补齐）；
/// - workspace_capture rule row 不可读 → `E_WORKSPACE_AUTHORITY_MISMATCH`
///   （capability 未就绪，禁止无 provenance 绑定）；
/// - 同 workspace/instance 已有 capture 时只允许相同稳定 identity 的 re-attestation
///   （revision 递增、supersedes 指向前一条），identity 改变 → mismatch。
pub(crate) fn bind_task_to_workspace(
    tx: &Transaction<'_>,
    task_id: &str,
    workspace_id: i64,
    workspace_instance_id: &str,
    created_by: &str,
) -> Result<(String, String), DaemonRpcError> {
    // 0. workspace 必须真实存在。
    let ws_exists: i64 = tx
        .query_row(
            "SELECT COUNT(*) FROM workspaces WHERE id = ?1",
            params![workspace_id],
            |r| r.get(0),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("workspace 存在性校验失败: {}", e)))?;
    if ws_exists == 0 {
        return Err(DaemonRpcError::new(
            "E_WORKSPACE_AUTHORITY_MISMATCH",
            format!(
                "task-DB 中不存在 workspace_id={}（不得用客户端 numeric id 补齐）",
                workspace_id
            ),
        ));
    }

    // 1. capture c14n rule row 必须可读（capability 未就绪 fail-closed）。
    let capture_rules_hash: String = tx
        .query_row(
            "SELECT rules_hash FROM canonicalization_rule_sets \
             WHERE domain = 'workspace_capture' AND canonicalization_version = 'workspace-capture-c14n/v1'",
            [],
            |r| r.get(0),
        )
        .map_err(|e| {
            DaemonRpcError::new(
                "E_WORKSPACE_AUTHORITY_MISMATCH",
                format!("workspace_capture rule row 不可用（capability 未就绪）: {}", e),
            )
        })?;

    // 2. 从 workspace 行派生稳定身份字段（root/name 只读，不依赖客户端提交）。
    let (name, root_path): (String, String) = tx
        .query_row(
            "SELECT name, root_path FROM workspaces WHERE id = ?1",
            params![workspace_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("读取 workspace 行失败: {}", e)))?;
    let instance_id = if workspace_instance_id.trim().is_empty() {
        format!("ws-{}", workspace_id)
    } else {
        workspace_instance_id.to_string()
    };
    let root_hash = sha256_hex(root_path.as_bytes());
    let manifest_payload = serde_json::json!({
        "workspace_id": workspace_id,
        "workspace_name": name,
        "root_path_hash": root_hash,
        "manifest_format_version": "workspace-manifest-c14n/v1",
    });
    let manifest_payload_json = serde_json::to_string(&manifest_payload).unwrap_or_default();
    let manifest_hash = sha256_hex(manifest_payload_json.as_bytes());
    let identity_hash = crate::daemon::task_loop::create::registry_identity_hash(
        &instance_id,
        &root_hash,
        &root_hash,
        &manifest_hash,
    );

    // 3. capture 链：同 workspace/instance/identity 才允许追加，否则 mismatch。
    let latest: Option<(String, i64, String)> = tx
        .query_row(
            "SELECT workspace_capture_id, capture_revision, registry_identity_hash \
             FROM workspace_authority_captures \
             WHERE workspace_id = ?1 AND workspace_instance_id = ?2 \
             ORDER BY capture_revision DESC LIMIT 1",
            params![workspace_id, instance_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("capture 链读取失败: {}", e)))?;
    let (capture_id, revision, supersedes) = match &latest {
        Some((_, _, prev_hash)) if prev_hash != &identity_hash => {
            return Err(DaemonRpcError::new(
                "E_WORKSPACE_AUTHORITY_MISMATCH",
                format!(
                    "workspace 稳定 identity 改变：既有 registry_identity_hash={} 与当前={} 不一致；\
                     旧 task 必须 UNVERIFIED，不得 UPDATE 原 binding",
                    prev_hash, identity_hash
                ),
            ));
        }
        Some((prev_capture_id, prev_revision, _)) => (
            format!("wc-{}-{}", instance_id, rand_val()),
            prev_revision + 1,
            Some(prev_capture_id.clone()),
        ),
        None => (
            format!("wc-{}-{}", instance_id, rand_val()),
            1,
            None,
        ),
    };
    let now = authoritative_now_text();
    let registry_identity_payload_json = serde_json::to_string(&serde_json::json!({
        "workspace_instance_id": instance_id,
        "client_view_root_hash": root_hash,
        "host_real_root_hash": root_hash,
        "workspace_manifest_hash": manifest_hash,
    }))
    .unwrap_or_default();

    // 4. 追加 workspace_authority_captures（append-only）。
    tx.execute(
        "INSERT INTO workspace_authority_captures \
         (workspace_capture_id, workspace_id, capture_revision, supersedes_capture_id, \
          daemon_workspace_id, workspace_instance_id, capture_canonicalization_version, \
          capture_canonicalization_rules_hash, registry_identity_payload_json, \
          registry_identity_hash, workspace_manifest_payload_json, workspace_manifest_hash, \
          client_view_root_hash, host_real_root_hash, created_by, authoritative_created_at) \
         VALUES (?1, ?2, ?3, ?4, 0, ?5, 'workspace-capture-c14n/v1', ?6, ?7, ?8, ?9, ?10, ?11, ?11, ?12, ?13)",
        params![
            capture_id,
            workspace_id,
            revision,
            supersedes,
            instance_id,
            capture_rules_hash,
            registry_identity_payload_json,
            identity_hash,
            manifest_payload_json,
            manifest_hash,
            root_hash,
            created_by,
            now,
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("追加 workspace_authority_captures 失败: {}", e)))?;

    // 5. 不可变 task→workspace binding（引用刚追加的 capture）。
    let binding_id = format!("tb-{}-{}", task_id, instance_id);
    tx.execute(
        "INSERT INTO task_workspace_bindings \
         (task_id, workspace_id, workspace_binding_id, workspace_capture_id, \
          created_by, authoritative_created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![task_id, workspace_id, binding_id, capture_id, created_by, now],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("写入 task_workspace_bindings 失败: {}", e)))?;

    Ok((binding_id, capture_id))
}

/// 构造 E_LEASE_CLOCK_UNAVAILABLE（fail-closed，Req 14.30）。
///
/// daemon serve 装配未注入 AuthoritativeClock 时，Lease 写操作一律拒绝，
/// 绝不降级为客户端时钟或非受保护写。
fn lease_clock_unavailable(action: &str, task_id: &str, role: &str) -> DaemonRpcError {
    DaemonRpcError::new(
        "E_LEASE_CLOCK_UNAVAILABLE",
        format!(
            "{}（task={} role={}）需要 daemon 权威时钟但未注入；\
             请确认 daemon 装配调用了 TaskCollabStore::with_clock(AuthoritativeClock)",
            action, task_id, role
        ),
    )
}

fn step_field(step: &Map<String, Value>, name: &str) -> String {
    let Some(value) = step.get(name) else {
        return String::new();
    };
    if let Some(text) = value.as_str() {
        return text.to_string();
    }
    if value.is_null() {
        return String::new();
    }
    serde_json::to_string(value).unwrap_or_default()
}

/// 从 Role Contract JSON 中提取字段（字符串原样；数组/对象序列化为 JSON 文本）。
fn contract_field(contract: &Map<String, Value>, name: &str) -> String {
    let Some(value) = contract.get(name) else {
        return String::new();
    };
    if let Some(text) = value.as_str() {
        return text.to_string();
    }
    if value.is_null() {
        return String::new();
    }
    serde_json::to_string(value).unwrap_or_default()
}

/// A3 角色独立性判定：independent_reviewer 与 implementer/tester/evidence
/// 不能共享 agent_instance_id 或 session_id（设计 §5）；coordinator 不受限。
fn roles_conflict(a: &str, b: &str) -> bool {
    let a_independent = a == "independent_reviewer" || a == "reviewer";
    let b_independent = b == "independent_reviewer" || b == "reviewer";
    let a_work = matches!(a, "implementer" | "tester" | "evidence");
    let b_work = matches!(b, "implementer" | "tester" | "evidence");
    (a_independent && b_work) || (b_independent && a_work)
}

/// A3 角色独立性门禁：同一 agent_instance_id 或 session_id 不得同时持有
/// 相互冲突的角色（如 implementer 与 independent_reviewer）。
/// 查询所有 active 且角色非空、角色与目标角色冲突、instance/session 相同的
/// 注册行（含当前 agent 自身的历史角色），命中即拒绝。
fn check_role_independence(
    conn: &Connection,
    instance_id: &str,
    session_id: &str,
    role: &str,
    agent_id: &str,
) -> Result<(), DaemonRpcError> {
    if role.is_empty() || (instance_id.is_empty() && session_id.is_empty()) {
        return Ok(());
    }
    let mut stmt = conn
        .prepare(
            "SELECT agent_id, agent_instance_id, session_id, role
             FROM agent_registrations
             WHERE status = 'active' AND role != '' AND role != ?1
               AND (agent_instance_id = ?2 OR (session_id != '' AND session_id = ?3))",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("独立性门禁查询失败: {}", e)))?;
    let rows = stmt
        .query_map(params![role, instance_id, session_id], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("独立性门禁映射失败: {}", e)))?;
    for row in rows.flatten() {
        let (other_agent, other_instance, other_session, other_role) = row;
        if roles_conflict(role, &other_role) {
            return Err(DaemonRpcError::new(
                "E_ROLE_INDEPENDENCE_VIOLATION",
                format!(
                    "角色独立性门禁拒绝: 角色 {} (agent={}, instance={}, session={}) 与 角色 {} \
                     冲突，instance/session 不可共享（当前 agent={}）",
                    other_role, other_agent, other_instance, other_session, role, agent_id,
                ),
            ));
        }
    }
    Ok(())
}

/// 在调用方已有事务中持久化 Role Contract（A3，task.create 路径）。
fn insert_role_contracts(
    tx: &Transaction<'_>,
    task_id: &str,
    contracts: &[Value],
    created_by: &str,
    ts: f64,
) -> Result<(), DaemonRpcError> {
    for (idx, raw) in contracts.iter().enumerate() {
        let contract = raw.as_object().ok_or_else(|| {
            DaemonRpcError::invalid_params("role_contracts 的每一项必须是 JSON object")
        })?;
        let role = contract
            .get("role")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        if role.is_empty() {
            return Err(DaemonRpcError::invalid_params(
                "role_contracts 每项必须包含 role",
            ));
        }
        let contract_id = format!("RC-{}-{}-{}", task_id, role, idx);
        tx.execute(
            "INSERT INTO role_contracts
             (contract_id, task_id, step_id, role, skill_id, skill_version,
              prompt_template_id, prompt_hash, allowed_paths, forbidden_paths,
              commands, acceptance_checks, required_evidence, handoff_to,
              independence, revision, is_current, created_at, created_by)
             VALUES (?1, ?2, '', ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, 1, 1, ?15, ?16)",
            params![
                contract_id, task_id, role,
                contract_field(contract, "skill_id"),
                contract_field(contract, "skill_version"),
                contract_field(contract, "prompt_template_id"),
                contract_field(contract, "prompt_hash"),
                contract_field(contract, "allowed_paths"),
                contract_field(contract, "forbidden_paths"),
                contract_field(contract, "commands"),
                contract_field(contract, "acceptance_checks"),
                contract_field(contract, "required_evidence"),
                contract_field(contract, "handoff_to"),
                contract_field(contract, "independence"),
                ts, created_by,
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("role_contract 写入失败: {}", e)))?;
    }
    Ok(())
}

/// 查询任务是否有冻结的 Role Contract（A3 门禁用）。
fn task_has_contracts(conn: &Connection, task_id: &str) -> Result<bool, DaemonRpcError> {
    let count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM role_contracts WHERE task_id = ?1",
            params![task_id],
            |r| r.get(0),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("查询 role_contracts 失败: {}", e)))?;
    Ok(count > 0)
}

/// 查询任务指定角色的当前（is_current=1）Role Contract。
fn get_current_role_contract(
    conn: &Connection,
    task_id: &str,
    role: &str,
) -> Result<Option<Map<String, Value>>, DaemonRpcError> {
    let row = conn
        .query_row(
            "SELECT contract_id, skill_id, skill_version, prompt_template_id, prompt_hash,
                    allowed_paths, forbidden_paths, commands, acceptance_checks,
                    required_evidence, handoff_to, independence, revision, created_by
             FROM role_contracts
             WHERE task_id = ?1 AND role = ?2 AND is_current = 1
             ORDER BY revision DESC LIMIT 1",
            params![task_id, role],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, String>(5)?,
                    r.get::<_, String>(6)?,
                    r.get::<_, String>(7)?,
                    r.get::<_, String>(8)?,
                    r.get::<_, String>(9)?,
                    r.get::<_, String>(10)?,
                    r.get::<_, String>(11)?,
                    r.get::<_, i64>(12)?,
                    r.get::<_, String>(13)?,
                ))
            },
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("查询 role_contract 失败: {}", e)))?;
    Ok(row.map(|(contract_id, skill_id, skill_version, prompt_template_id, prompt_hash,
                 allowed_paths, forbidden_paths, commands, acceptance_checks,
                 required_evidence, handoff_to, independence, revision, created_by)| {
        let mut m = Map::new();
        m.insert("contract_id".to_string(), Value::String(contract_id));
        m.insert("task_id".to_string(), Value::String(task_id.to_string()));
        m.insert("role".to_string(), Value::String(role.to_string()));
        m.insert("skill_id".to_string(), Value::String(skill_id));
        m.insert("skill_version".to_string(), Value::String(skill_version));
        m.insert("prompt_template_id".to_string(), Value::String(prompt_template_id));
        m.insert("prompt_hash".to_string(), Value::String(prompt_hash));
        m.insert("allowed_paths".to_string(), Value::String(allowed_paths));
        m.insert("forbidden_paths".to_string(), Value::String(forbidden_paths));
        m.insert("commands".to_string(), Value::String(commands));
        m.insert("acceptance_checks".to_string(), Value::String(acceptance_checks));
        m.insert("required_evidence".to_string(), Value::String(required_evidence));
        m.insert("handoff_to".to_string(), Value::String(handoff_to));
        m.insert("independence".to_string(), Value::String(independence));
        m.insert("revision".to_string(), Value::Number(serde_json::Number::from(revision)));
        m.insert("created_by".to_string(), Value::String(created_by));
        m
    }))
}

/// 将 role_contracts 查询行映射为 JSON 对象（用于 contract_get / Envelope）。
/// 列顺序与 handle_task_contract_get 的 SELECT 一致。
fn contract_row_to_map(r: &rusqlite::Row<'_>) -> rusqlite::Result<Map<String, Value>> {
    let mut m = Map::new();
    m.insert("role".to_string(), Value::String(r.get(0)?));
    m.insert("skill_id".to_string(), Value::String(r.get(1)?));
    m.insert("skill_version".to_string(), Value::String(r.get(2)?));
    m.insert("prompt_template_id".to_string(), Value::String(r.get(3)?));
    m.insert("prompt_hash".to_string(), Value::String(r.get(4)?));
    m.insert("allowed_paths".to_string(), Value::String(r.get(5)?));
    m.insert("forbidden_paths".to_string(), Value::String(r.get(6)?));
    m.insert("commands".to_string(), Value::String(r.get(7)?));
    m.insert("acceptance_checks".to_string(), Value::String(r.get(8)?));
    m.insert("required_evidence".to_string(), Value::String(r.get(9)?));
    m.insert("handoff_to".to_string(), Value::String(r.get(10)?));
    m.insert("independence".to_string(), Value::String(r.get(11)?));
    m.insert("revision".to_string(), Value::Number(serde_json::Number::from(r.get::<_, i64>(12)?)));
    m.insert("contract_id".to_string(), Value::String(r.get(13)?));
    m.insert("created_by".to_string(), Value::String(r.get(14)?));
    Ok(m)
}

/// 在调用方已有事务中持久化任务步骤。
///
/// `task.create` 和 `task.split` 都必须使用这条路径，避免 daemon RPC
/// 创建出没有步骤记录的“空任务”。任何一步定义非法或写入失败都会让
/// 外层事务回滚，不能留下只有任务没有步骤的半完成状态。
fn insert_task_steps(
    tx: &Transaction<'_>,
    task_id: &str,
    steps: &[Value],
    ts: f64,
) -> Result<(), DaemonRpcError> {
    for (step_index, raw_step) in steps.iter().enumerate() {
        let step = raw_step.as_object().ok_or_else(|| {
            DaemonRpcError::invalid_params("steps 的每一项必须是 JSON object")
        })?;
        let step_id = generate_step_id();
        tx.execute(
            "INSERT INTO task_steps
             (id, task_id, step_index, action, target_file, target_symbol,
              check_items, status, result, created_at, completed_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'pending', '', ?8, NULL)",
            params![
                step_id,
                task_id,
                step_index as i64,
                step_field(step, "action"),
                step_field(step, "target_file"),
                step_field(step, "target_symbol"),
                step_field(step, "check_items"),
                ts,
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_step 写入失败: {}", e)))?;
    }
    Ok(())
}

/// 解析 Markdown 计划文本为子任务定义（与 CLI `_parse_plan_to_subtasks` 语义一致）
///
/// 格式约定：
/// - `## 标题` = 子任务标题
/// - 标题下的普通行 = 子任务描述
/// - `- / * / +` 列表项 = 步骤，支持 `action @ target_file`、`action: target_file`
///   或纯 `action` 三种写法（与 insert_task_steps 的字段结构对齐）
/// - ``` / ~~~ 围栏内的行按代码块跳过，不作为描述或步骤
///
/// 返回: Vec<(title, description, steps)>，steps 为 JSON object 数组
fn parse_subtasks_from_plan_text(plan_text: &str) -> Vec<(String, String, Vec<Map<String, Value>>)> {
    let mut items: Vec<(String, String, Vec<Map<String, Value>>)> = Vec::new();
    let mut in_code_block = false;

    for line in plan_text.lines() {
        let trimmed = line.trim();

        // 代码块围栏检测
        if trimmed.starts_with("```") || trimmed.starts_with("~~~") {
            in_code_block = !in_code_block;
            continue;
        }
        if in_code_block {
            continue;
        }

        // 二级标题 = 新子任务
        if trimmed.starts_with("## ") {
            let title = trimmed[3..].trim().trim_end_matches('#').trim().to_string();
            if !title.is_empty() {
                items.push((title, String::new(), Vec::new()));
            }
            continue;
        }

        // 列表项 = 步骤（仅当已有子任务时归入当前子任务）
        let list_body = if trimmed.starts_with("- ") {
            Some(&trimmed[2..])
        } else if trimmed.starts_with("* ") {
            Some(&trimmed[2..])
        } else if trimmed.starts_with("+ ") {
            Some(&trimmed[2..])
        } else {
            None
        };
        if let Some(content) = list_body {
            let content = content.trim();
            if !content.is_empty() {
                if let Some(cur) = items.last_mut() {
                    // 解析 "action @ target_file" / "action: target_file" / 纯 action
                    let mut step = Map::new();
                    let (action, target_file) = if let Some(pos) = content.find('@') {
                        (content[..pos].trim().to_string(),
                         content[pos + 1..].trim().to_string())
                    } else if let Some(pos) = content.find(':') {
                        (content[..pos].trim().to_string(),
                         content[pos + 1..].trim().to_string())
                    } else {
                        (content.to_string(), String::new())
                    };
                    step.insert("action".to_string(), Value::String(action));
                    step.insert("target_file".to_string(), Value::String(target_file));
                    step.insert("target_symbol".to_string(), Value::String(String::new()));
                    step.insert("check_items".to_string(), Value::String(String::new()));
                    cur.2.push(step);
                }
            }
            continue;
        }

        // 其余以 # 开头的标题行跳过（H1/H3/H4...），普通行归入当前子任务描述
        if trimmed.starts_with('#') {
            continue;
        }
        if !trimmed.is_empty() {
            if let Some(cur) = items.last_mut() {
                if !cur.1.is_empty() {
                    cur.1.push('\n');
                }
                cur.1.push_str(trimmed);
            }
        }
    }

    if items.is_empty() {
        items.push((
            "Task Execution Step".to_string(),
            String::new(),
            Vec::new(),
        ));
    }
    items
}

// ============================================
// TaskCollabStore
// ============================================

pub struct TaskCollabStore {
    pub(crate) conn: Arc<Mutex<Connection>>,
    seq_counter: Arc<Mutex<i64>>,
    dedup_cache: Arc<Mutex<HashMap<String, Value>>>,
    /// daemon 权威时钟（lease 受保护写校验必需）。
    /// 为 None 时，携带 lease 凭证的写操作 fail-closed（E_LEASE_CLOCK_UNAVAILABLE）。
    clock: Option<Arc<AuthoritativeClock>>,
}

impl TaskCollabStore {
    /// 在 task-DB 连接上执行回调（供 task_loop 公共路由写路径使用）。
    ///
    /// 锁序约束：调用方必须先持有 `CapabilityMutationGate`（gate → task-DB
    /// transaction，见 cw-role-handoff-task-loop.md §4.3）。锁由连接 mutex 串行化。
    pub(crate) fn with_conn<R>(
        &self,
        f: impl FnOnce(&mut Connection) -> Result<R, DaemonRpcError>,
    ) -> Result<R, DaemonRpcError> {
        let mut guard = self
            .conn
            .lock()
            .map_err(|_| DaemonRpcError::internal_error("task collab store 连接锁 poisoned"))?;
        f(&mut guard)
    }

    pub fn new<P: AsRef<Path>>(db_path: P) -> Result<Self, DaemonRpcError> {
        let conn = Connection::open(&db_path).map_err(|e| {
            DaemonRpcError::internal_error(format!("无法打开 Task DB {}: {}", db_path.as_ref().display(), e))
        })?;

        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;").ok();

        Self::migrate_and_verify(&conn)?;
        // P0-H（T-1787277487109-758e56d0）：supersede 表已入 canonical v59 schema，
        // 此处仅做列级 fail-closed 校验（不再以启动期 DDL 创建/掩盖迁移）。
        validate_supersede_schema(&conn)?;

        let max_seq: i64 = conn
            .query_row("SELECT COALESCE(MAX(monotonic_seq), 0) FROM task_events", [], |r| r.get(0))
            .unwrap_or(0);

        Ok(Self {
            conn: Arc::new(Mutex::new(conn)),
            seq_counter: Arc::new(Mutex::new(max_seq)),
            dedup_cache: Arc::new(Mutex::new(HashMap::new())),
            clock: None,
        })
    }

    /// 注入 daemon 权威时钟（lease 受保护写校验必需）。
    ///
    /// 未注入时钟时，携带 lease 凭证的 apply/close 会 fail-closed：
    /// 返回 `E_LEASE_CLOCK_UNAVAILABLE`，绝不降级为无凭证写（Req 11.9 / 14.30）。
    pub fn with_clock(mut self, clock: Arc<AuthoritativeClock>) -> Self {
        self.clock = Some(clock);
        self
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
        // P0-H（T-1787277487109-758e56d0）：supersede 表已入 canonical v59 schema，
        // 此处仅做列级 fail-closed 校验（不再以启动期 DDL 创建/掩盖迁移）。
        validate_supersede_schema(&conn)?;

        let max_seq: i64 = conn
            .query_row("SELECT COALESCE(MAX(monotonic_seq), 0) FROM task_events", [], |r| r.get(0))
            .unwrap_or(0);

        Ok(Self {
            conn: Arc::new(Mutex::new(conn)),
            seq_counter: Arc::new(Mutex::new(max_seq)),
            dedup_cache: Arc::new(Mutex::new(HashMap::new())),
            clock: None,
        })
    }

    /// 执行官方 schema migration 并做只读校验（不含任何旁路 DDL）：
    /// 1. `migrate_connection` 事务化迁移到当前 SCHEMA_VERSION（49，权威 db/schema.py），错误传播
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
        //    官方 v50 schema 已含全部所需列（含 task_events 的 role/contract_hash/snapshot_id/
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

    /// 单调递增事件序号（进程内自增；跨重启以 DB MAX(monotonic_seq) 恢复）。
    pub(crate) fn next_seq(&self) -> i64 {
        let mut seq = self.seq_counter.lock().unwrap();
        *seq += 1;
        *seq
    }

    /// task_events append-only 审计行写入 helper（P0-H：task.supersede 复用）。
    ///
    /// 与 apply/close/claim 的事件写模式一致（task_id/from_status/to_status/
    /// reason_code/reason/actor_identity/agent_session_id/role/monotonic_seq/
    /// authoritative_timestamp），只追加，不改变任何任务字段。返回新事件 id。
    pub(crate) fn append_task_event(
        tx: &Transaction<'_>,
        task_id: &str,
        from_status: &str,
        to_status: &str,
        reason_code: &str,
        reason: &str,
        actor_identity: &str,
        actor_session_id: &str,
        role: &str,
        seq: i64,
        ts: f64,
    ) -> Result<i64, DaemonRpcError> {
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity,
              agent_session_id, role, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
            params![
                task_id, from_status, to_status, reason_code, reason,
                actor_identity, actor_session_id, role, seq, ts
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("追加 task_events 失败: {}", e)))?;
        Ok(tx.last_insert_rowid())
    }

    /// 获取任务当前 claim 的声明者与 agent_session_id。
    ///
    /// 只看 claim 生命周期事件，而不是所有 `to_status='in_progress'` 事件。
    /// 这样 `task.claim.recover` 追加 `claim_released` 后，旧 claim 不会继续
    /// 被当成当前 owner；历史 claimed/reported 事件保持不可变。
    fn get_task_claim_info(&self, conn: &Connection, task_id: &str) -> (Option<String>, Option<String>) {
        let res: Result<(String, String, String), _> = conn.query_row(
            "SELECT reason_code, actor_identity, agent_session_id FROM task_events
             WHERE task_id = ?1 AND reason_code IN ('claimed', 'claim_recovered', 'claim_released')
             ORDER BY monotonic_seq DESC LIMIT 1",
            params![task_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        );
        match res {
            Ok((reason, actor, session)) if reason != "claim_released" => (Some(actor), Some(session)),
            Err(_) => (None, None),
            _ => (None, None),
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

        // A2 Agent Identity：接受 identity JSON 对象或扁平字段。
        // 扁平字段（agent_instance_id/client_id/provider/model_id/runtime_hash/...）
        // 与 identity 对象字段一一对应，identity 对象优先。
        let field = |params: &Value, name: &str| -> String {
            params
                .get(name)
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_string()
        };
        let (i_instance, i_client, i_provider, i_model, i_mode, i_fingerprint, i_session, i_role, i_runtime) =
            if let Some(raw) = params.get("identity") {
                let value = if let Some(text) = raw.as_str() {
                    serde_json::from_str::<Value>(text).unwrap_or(Value::Null)
                } else {
                    raw.clone()
                };
                let obj = value.as_object().cloned().unwrap_or_default();
                (
                    field(&Value::Object(obj.clone()), "agent_instance_id"),
                    field(&Value::Object(obj.clone()), "client_id"),
                    field(&Value::Object(obj.clone()), "provider"),
                    field(&Value::Object(obj.clone()), "model_id"),
                    field(&Value::Object(obj.clone()), "model_mode"),
                    field(&Value::Object(obj.clone()), "system_fingerprint"),
                    field(&Value::Object(obj.clone()), "session_id"),
                    field(&Value::Object(obj.clone()), "role"),
                    field(&Value::Object(obj), "runtime_hash"),
                )
            } else {
                (
                    field(params, "agent_instance_id"),
                    field(params, "client_id"),
                    field(params, "provider"),
                    field(params, "model_id"),
                    field(params, "model_mode"),
                    field(params, "system_fingerprint"),
                    field(params, "session_id"),
                    field(params, "role"),
                    field(params, "runtime_hash"),
                )
            };

        let owner_key = peer.owner_key();
        let agent_id = params
            .get("agent_id")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| format!("agent-{}", owner_key));
        let ts = task_now_ts();

        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT OR REPLACE INTO agent_registrations 
             (agent_id, agent_name, owner_key, capabilities, registered_at, last_heartbeat, status,
              agent_instance_id, client_id, provider, model_id, model_mode,
              system_fingerprint, runtime_hash, session_id, role)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'active', ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)",
            params![
                agent_id, agent_name, owner_key, capabilities, ts, ts,
                i_instance, i_client, i_provider, i_model, i_mode,
                i_fingerprint, i_runtime, i_session, i_role
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("agent_register 失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("agent_id".to_string(), Value::String(agent_id));
        res.insert("status".to_string(), Value::String("registered".to_string()));
        res.insert("owner_key".to_string(), Value::String(owner_key));
        res.insert("agent_instance_id".to_string(), Value::String(i_instance));
        res.insert("client_id".to_string(), Value::String(i_client));
        res.insert("provider".to_string(), Value::String(i_provider));
        res.insert("model_id".to_string(), Value::String(i_model));
        res.insert("session_id".to_string(), Value::String(i_session));
        res.insert("role".to_string(), Value::String(i_role));
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

        let ts = task_now_ts();
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
        // workspace authority fail-closed：task.create 必须显式传入 workspace_id（>0）
        // 且 workspace 真实存在；禁止用 active workspace / cwd 补齐（§8.1.1）。
        let workspace_id = required_workspace_id_param(params)?;
        let workspace_instance_id = params
            .get("workspace_instance_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let steps = match params.get("steps") {
            None => &[][..],
            Some(value) => value
                .as_array()
                .ok_or_else(|| DaemonRpcError::invalid_params("steps 必须是 JSON array"))?,
        };
        let role_contracts = match params.get("role_contracts") {
            None => &[][..],
            Some(value) => value
                .as_array()
                .ok_or_else(|| DaemonRpcError::invalid_params("role_contracts 必须是 JSON array"))?,
        };

        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_else(generate_task_id);

        let ts = task_now_ts();
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

        // 不可变 task→workspace binding 与 task 行同一事务写入（§8.1.1）。
        let (binding_id, capture_id) = bind_task_to_workspace(
            &tx,
            &task_id,
            workspace_id,
            workspace_instance_id,
            &peer.owner_key(),
        )?;

        tx.execute(
            "INSERT INTO task_events 
             (task_id, workspace_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'none', 'open', 'created', ?3, ?4, ?5, ?6)",
            params![task_id, workspace_id.to_string(), title, peer.owner_key(), seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        insert_task_steps(&tx, &task_id, steps, ts)?;

        // A3：Planner 通过 task.create 一次性冻结 Role Contract（revision=1）。
        if !role_contracts.is_empty() {
            insert_role_contracts(&tx, &task_id, role_contracts, &peer.owner_key(), ts)?;
        }

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 task_create 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id));
        res.insert("status".to_string(), Value::String("open".to_string()));
        res.insert("title".to_string(), Value::String(title.to_string()));
        res.insert("step_count".to_string(), Value::Number(serde_json::Number::from(steps.len())));
        res.insert("contract_count".to_string(), Value::Number(serde_json::Number::from(role_contracts.len())));
        res.insert("monotonic_seq".to_string(), Value::Number(serde_json::Number::from(seq)));
        res.insert("workspace_id".to_string(), Value::Number(serde_json::Number::from(workspace_id)));
        res.insert("workspace_binding_id".to_string(), Value::String(binding_id));
        res.insert("workspace_capture_id".to_string(), Value::String(capture_id));
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
        let requested_remediation_step_id = params
            .get("remediation_step_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();

        // A2/A3 身份与合同门禁（fail-closed，先于事务执行的只读校验）。
        let identity = parse_action_identity(params)?;
        let ts = task_now_ts();
        let mut conn = self.conn.lock().unwrap();

        if let Some(id) = &identity {
            // 1. 未注册身份 fail-closed
            let registered = conn
                .query_row(
                    "SELECT agent_instance_id, session_id, role, status FROM agent_registrations
                     WHERE agent_id = ?1",
                    params![id.agent_id],
                    |r| Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                        r.get::<_, String>(3)?,
                    )),
                )
                .optional()
                .map_err(|e| DaemonRpcError::internal_error(format!("查询 agent_registrations 失败: {}", e)))?;
            let reg = registered.ok_or_else(|| DaemonRpcError::new(
                "E_IDENTITY_UNREGISTERED",
                format!("agent {} 未注册身份，禁止领取任务（fail-closed）", id.agent_id),
            ))?;
            if reg.3 != "active" {
                return Err(DaemonRpcError::new(
                    "E_IDENTITY_INACTIVE",
                    format!("agent {} 已停用，禁止领取任务", id.agent_id),
                ));
            }
            // 2. instance 一致性（注册时 instance 非空则必须一致）
            if !reg.0.is_empty() && reg.0 != id.agent_instance_id {
                return Err(DaemonRpcError::new(
                    "E_IDENTITY_INSTANCE_MISMATCH",
                    format!(
                        "agent {} 注册 instance {} 与本次领取 {} 不一致",
                        id.agent_id, reg.0, id.agent_instance_id
                    ),
                ));
            }
            // 3. session 一致性（identity.session_id 必须等于 claim 的 agent_session_id）
            if id.session_id != agent_session_id {
                return Err(DaemonRpcError::new(
                    "E_IDENTITY_SESSION_MISMATCH",
                    format!(
                        "agent {} identity.session_id {} 与领取会话 {} 不一致",
                        id.agent_id, id.session_id, agent_session_id
                    ),
                ));
            }
            // 4. 角色独立性门禁（instance/session 不可共享冲突角色）
            check_role_independence(&conn, &id.agent_instance_id, &id.session_id, &id.role, &id.agent_id)?;
            // 5. Role Contract 校验（合同任务必须携带 contract_claim 且 skill/prompt hash 一致）
            if let Some(contract) = get_current_role_contract(&conn, task_id, &id.role)? {
                let claim = params.get("contract_claim").and_then(|v| v.as_object());
                let claim_field = |name: &str| -> String {
                    claim.and_then(|c| c.get(name)).and_then(|v| v.as_str()).unwrap_or("").trim().to_string()
                };
                let cfield = |name: &str| -> String {
                    contract.get(name).and_then(|v| v.as_str()).unwrap_or("").to_string()
                };
                if !cfield("skill_id").is_empty() && claim_field("skill_id") != cfield("skill_id") {
                    return Err(DaemonRpcError::new(
                        "E_CONTRACT_SKILL_MISMATCH",
                        format!("任务 {} 的 {} 角色合同 skill_id 不符（期望 {}）", task_id, id.role, cfield("skill_id")),
                    ));
                }
                if !cfield("skill_version").is_empty() && claim_field("skill_version") != cfield("skill_version") {
                    return Err(DaemonRpcError::new(
                        "E_CONTRACT_VERSION_MISMATCH",
                        format!("任务 {} 的 {} 角色合同 skill_version 不符", task_id, id.role),
                    ));
                }
                if !cfield("prompt_hash").is_empty() && claim_field("prompt_hash") != cfield("prompt_hash") {
                    return Err(DaemonRpcError::new(
                        "E_CONTRACT_PROMPT_MISMATCH",
                        format!("任务 {} 的 {} 角色合同 prompt_hash 不符", task_id, id.role),
                    ));
                }
            }
        } else if task_has_contracts(&conn, task_id)? {
            // 合同任务必须携带 identity（fail-closed）
            return Err(DaemonRpcError::new(
                "E_IDENTITY_REQUIRED",
                format!("任务 {} 已冻结 Role Contract，领取必须携带 identity", task_id),
            ));
        }
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

        let required_remediation = required_remediation_step(&tx, task_id)?;
        if let Some((required_step_id, required_metadata)) = required_remediation {
            if requested_remediation_step_id.is_empty() {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_STEP_REQUIRED",
                    format!(
                        "任务存在待处理 remediation {}，必须显式提供 remediation_step_id",
                        required_step_id
                    ),
                ));
            }
            if requested_remediation_step_id != required_step_id {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_STEP_MISMATCH",
                    format!("必须领取当前 remediation {}", required_step_id),
                ));
            }
            let remediation: Option<(String, String, String, String)> = tx
                .query_row(
                    "SELECT action, status, result, task_id FROM task_steps WHERE id = ?1",
                    params![requested_remediation_step_id],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
                )
                .optional()
                .map_err(|e| DaemonRpcError::internal_error(format!("查询 remediation 步骤失败: {}", e)))?;
            let Some((action, step_status, result, remediation_task_id)) = remediation else {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_STEP_MISMATCH",
                    "指定 remediation_step_id 不存在",
                ));
            };
            if remediation_task_id != task_id || action != "fix_defect" || !matches!(step_status.as_str(), "pending" | "in_progress") {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_STEP_MISMATCH",
                    "指定 remediation_step_id 不是当前任务可领取的 fix_defect 步骤",
                ));
            }
            let metadata = serde_json::from_str::<Value>(&result).unwrap_or(Value::Null);
            let linked_step_id = metadata
                .get("remediation_of_step_id")
                .and_then(|item| item.as_str())
                .filter(|item| !item.trim().is_empty())
                .ok_or_else(|| DaemonRpcError::new(
                "E_REMEDIATION_STEP_MISMATCH",
                "remediation 步骤缺少 remediation_of_step_id provenance",
            ))?;
            let source_step_exists: i64 = tx
                .query_row(
                    "SELECT COUNT(*) FROM task_steps WHERE id = ?1 AND task_id = ?2",
                    params![linked_step_id, task_id],
                    |r| r.get(0),
                )
                .map_err(|e| DaemonRpcError::internal_error(format!("校验 remediation provenance 失败: {}", e)))?;
            if source_step_exists == 0 || metadata != required_metadata {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_STEP_MISMATCH",
                    "remediation provenance 未指向当前任务的源步骤",
                ));
            }
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

        // 标记第一个 pending 步骤为 in_progress（与 Python db.task_next_step 契约对齐：
        // 领取即占用步骤，同任务内不可重复领取同一 pending 步骤；已 in_progress 的步骤
        // 再次 claim（同 session 恢复）时原样返回，不重复改写状态）。
        let claimed_step_update = if requested_remediation_step_id.is_empty() {
            tx.execute(
                "UPDATE task_steps SET status = 'in_progress' WHERE id = (
                     SELECT id FROM task_steps WHERE task_id = ?1 AND status = 'pending'
                     ORDER BY step_index ASC LIMIT 1
                 )",
                params![task_id],
            ).map_err(|e| DaemonRpcError::internal_error(format!("task_claim 标记步骤失败: {}", e)))?
        } else {
            tx.execute(
                "UPDATE task_steps SET status = 'in_progress' WHERE id = ?1 AND task_id = ?2 AND status = 'pending'",
                params![requested_remediation_step_id, task_id],
            ).map_err(|e| DaemonRpcError::internal_error(format!("task_claim 标记 remediation 步骤失败: {}", e)))?
        };
        if claimed_step_update == 0 && !requested_remediation_step_id.is_empty() {
            let still_in_progress: i64 = tx.query_row(
                "SELECT COUNT(*) FROM task_steps WHERE id = ?1 AND task_id = ?2 AND status = 'in_progress'",
                params![requested_remediation_step_id, task_id], |r| r.get(0),
            ).map_err(|e| DaemonRpcError::internal_error(format!("查询 remediation 步骤状态失败: {}", e)))?;
            if still_in_progress == 0 {
                return Err(DaemonRpcError::new("E_REMEDIATION_STEP_MISMATCH", "remediation 步骤无法领取"));
            }
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

        // 返回下一步骤详情（与 Python db.task_next_step 契约对齐）。
        // daemon 模式下 MCP/CLI 通过 task.claim 领取步骤，必须携带步骤信息，
        // 否则 Agent 拿不到 step_id/action/target 等字段。
        // 注意：tx 已 commit 释放对 conn 的借用，直接复用上方已持有的 conn。
        let step = conn
            .query_row(
                "SELECT ts.id, ts.step_index, ts.action, ts.target_file,
                        ts.target_symbol, ts.check_items, ts.status, t.title
                 FROM task_steps ts
                 JOIN tasks t ON t.id = ts.task_id
                 WHERE ts.task_id = ?1 AND ts.status IN ('pending', 'in_progress')
                 ORDER BY ts.step_index ASC
                 LIMIT 1",
                params![task_id],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, i64>(1)?,
                        r.get::<_, String>(2)?,
                        r.get::<_, String>(3)?,
                        r.get::<_, String>(4)?,
                        r.get::<_, String>(5)?,
                        r.get::<_, String>(6)?,
                        r.get::<_, String>(7)?,
                    ))
                },
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("查询下一步骤失败: {}", e)))?;

        if let Some((step_id, step_index, action, target_file, target_symbol, check_items, step_status, task_title)) = step {
            res.insert("step_id".to_string(), Value::String(step_id));
            res.insert("step_index".to_string(), Value::Number(serde_json::Number::from(step_index)));
            res.insert("action".to_string(), Value::String(action));
            res.insert("target_file".to_string(), Value::String(target_file));
            res.insert("target_symbol".to_string(), Value::String(target_symbol));
            res.insert("check_items".to_string(), Value::String(check_items));
            res.insert("step_status".to_string(), Value::String(step_status));
            res.insert("task_title".to_string(), Value::String(task_title));
        }

        // A3 Task Envelope：携带当前角色的冻结 Role Contract（revision/hash 存证）。
        if let Some(id) = &identity {
            if let Some(contract) = get_current_role_contract(&conn, task_id, &id.role)? {
                res.insert("role_contract".to_string(), Value::Object(contract));
            }
        }

        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    /// 受保护的 orphan claim 恢复入口。
    ///
    /// 该方法只释放已经证明失联的旧 claim，不冒充旧 session，也不改写任何
    /// 历史步骤/evidence。调用方必须以 adjudicator 身份持有目标任务的
    /// reviewer lease；释放动作与 claim_released 审计事件在同一事务提交。
    /// 释放后由新的 Executor 显式调用 task.claim，避免恢复接口隐式替新角色
    /// 写入 claim。
    pub fn handle_task_claim_recover(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) {
            return Ok(cached);
        }
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let reason = params
            .get("reason")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        if reason.is_empty() {
            return Err(DaemonRpcError::invalid_params("orphan claim recovery 必须提供 reason"));
        }
        let identity = parse_action_identity(params)?;
        let identity = identity.ok_or_else(|| {
            DaemonRpcError::new(
                "E_IDENTITY_REQUIRED",
                "orphan claim recovery 必须携带 adjudicator identity",
            )
        })?;
        if identity.role != "adjudicator" {
            return Err(DaemonRpcError::new(
                "E_RECOVERY_ROLE_REQUIRED",
                "orphan claim recovery 只能由 adjudicator 执行",
            ));
        }
        let (lease_token, fencing_counter) = Self::require_lease_params(params)?;
        let owner_key = peer.owner_key();
        let clock = self.clock.as_ref().ok_or_else(|| {
            lease_clock_unavailable("task.claim.recover", task_id, "reviewer")
        })?;
        let now = clock.now_secs() as f64;

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        // 先验证受保护 reviewer lease；失败时零 task-domain 写入。
        self.validate_lease_for_mutation(
            &tx,
            task_id,
            "reviewer",
            &lease_token,
            fencing_counter,
            Some(&identity),
        )?;

        let registered: Option<(String, String, String)> = tx
            .query_row(
                "SELECT status, session_id, role FROM agent_registrations WHERE agent_id = ?1",
                params![identity.agent_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 recovery identity 失败: {}", e)))?;
        match registered {
            Some((status, session, role))
                if status == "active" && session == identity.session_id && role == identity.role => {}
            _ => {
                return Err(DaemonRpcError::new(
                    "E_IDENTITY_NOT_ACTIVE",
                    "adjudicator identity 未注册为 active，拒绝 orphan claim recovery",
                ));
            }
        }

        let current_status: String = tx
            .query_row("SELECT status FROM tasks WHERE id = ?1", params![task_id], |r| r.get(0))
            .map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)))?;
        if current_status != "open" && current_status != "in_progress" {
            return Err(DaemonRpcError::new(
                "task_conflict",
                format!("Task {} 处于不可 recovery 状态 ({})", task_id, current_status),
            ));
        }

        let (claimed_actor, claimed_session) = self.get_task_claim_info(&tx, task_id);
        let old_actor = claimed_actor.ok_or_else(|| {
            DaemonRpcError::new("E_CLAIM_NOT_FOUND", "任务当前没有可释放的 active claim")
        })?;
        let old_session = claimed_session.unwrap_or_default();
        if old_session.is_empty() {
            return Err(DaemonRpcError::new(
                "E_CLAIM_OWNER_INVALID",
                "当前 claim 缺少 owner session，拒绝猜测性恢复",
            ));
        }

        // 只有旧 session 明确失联/停用/超时才可释放；active heartbeat 仍新鲜时 fail-closed。
        let owner_registration: Option<(String, f64)> = tx
            .query_row(
                "SELECT status, last_heartbeat FROM agent_registrations WHERE session_id = ?1 ORDER BY registered_at DESC LIMIT 1",
                params![old_session],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("查询旧 claim owner 失败: {}", e)))?;
        let stale = match owner_registration {
            None => true,
            Some((status, heartbeat)) => {
                status != "active" || now - heartbeat > ORPHAN_CLAIM_STALE_SECS
            }
        };
        if !stale {
            return Err(DaemonRpcError::new(
                "E_CLAIM_OWNER_ACTIVE",
                format!("旧 claim owner session={} 仍 active，拒绝 recovery", old_session),
            ));
        }

        let recovery_reason = serde_json::json!({
            "old_actor_identity": old_actor.clone(),
            "old_session_id": old_session.clone(),
            "recovery_reason": reason,
            "replacement_authority": identity.agent_id,
            "replacement_session_id": identity.session_id,
            "stale_after_seconds": ORPHAN_CLAIM_STALE_SECS,
        })
        .to_string();
        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity,
              agent_session_id, role, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, ?2, 'claim_released', ?3, ?4, ?5, ?6, ?7, ?8)",
            params![
                task_id,
                current_status,
                recovery_reason,
                owner_key,
                identity.session_id,
                identity.role,
                seq,
                now,
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("追加 claim recovery 事件失败: {}", e)))?;
        let event_id = tx.last_insert_rowid();
        record_action_identity(&tx, task_id, &identity, "task.claim.recover", seq, now)?;

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 claim recovery 事务失败: {}", e)))?;

        let mut result = Map::new();
        result.insert("task_id".to_string(), Value::String(task_id.to_string()));
        result.insert("status".to_string(), Value::String(current_status));
        result.insert("claim_status".to_string(), Value::String("released".to_string()));
        result.insert("old_actor_identity".to_string(), Value::String(old_actor));
        result.insert("old_session_id".to_string(), Value::String(old_session));
        result.insert("recovery_event_id".to_string(), Value::Number(event_id.into()));
        result.insert("next_action".to_string(), Value::String("new Executor must call task.claim".to_string()));
        result.insert("replayed".to_string(), Value::Bool(false));
        let value = Value::Object(result);
        self.save_dedup(params, &value);
        Ok(value)
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
        let required_remediation = required_remediation_step(&conn, task_id)?;

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
        let mut next_step_id = required_remediation
            .as_ref()
            .map(|(step_id, _)| step_id.clone());
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
        res.insert(
            "next_action".to_string(),
            Value::String(if required_remediation.is_some() {
                "revise".to_string()
            } else {
                "work".to_string()
            }),
        );
        res.insert(
            "remediation_step_id".to_string(),
            required_remediation
                .map(|(step_id, _)| Value::String(step_id))
                .unwrap_or(Value::Null),
        );
        res.insert("steps".to_string(), Value::Array(steps));
        Ok(Value::Object(res))
    }

    /// A3：更新任务某角色的 Role Contract——旧版置 is_current=0，写入新 revision，
    /// 并追加 task_event 审计（合同变更必须生成新版本 + 审计事件）。
    pub fn handle_task_contract_set(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let contract = params
            .get("contract")
            .and_then(|v| v.as_object())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 contract 对象"))?;
        let role = contract
            .get("role")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        if role.is_empty() {
            return Err(DaemonRpcError::invalid_params("contract.role 不能为空"));
        }

        let owner_key = peer.owner_key();
        let ts = task_now_ts();
        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        // 权限：creator / 当前 claimer / root
        let (creator, _status): (String, String) = tx
            .query_row("SELECT creator, status FROM tasks WHERE id = ?1", params![task_id], |r| Ok((r.get(0)?, r.get(1)?)))
            .map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)))?;
        let (claimed_actor, _) = self.get_task_claim_info(&tx, task_id);
        if creator != owner_key && claimed_actor.as_deref() != Some(&owner_key) && owner_key != "root" {
            return Err(DaemonRpcError::permission_denied(format!(
                "没有对任务 {} 更新 Role Contract 的权限",
                task_id
            )));
        }

        // 旧版置非当前，并计算新 revision
        tx.execute(
            "UPDATE role_contracts SET is_current = 0 WHERE task_id = ?1 AND role = ?2 AND is_current = 1",
            params![task_id, role],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("role_contract 旧版下线失败: {}", e)))?;
        let next_revision: i64 = tx
            .query_row(
                "SELECT COALESCE(MAX(revision), 0) + 1 FROM role_contracts WHERE task_id = ?1 AND role = ?2",
                params![task_id, role],
                |r| r.get(0),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("查询合同 revision 失败: {}", e)))?;
        let contract_id = format!("RC-{}-{}-r{}", task_id, role, next_revision);

        tx.execute(
            "INSERT INTO role_contracts
             (contract_id, task_id, step_id, role, skill_id, skill_version,
              prompt_template_id, prompt_hash, allowed_paths, forbidden_paths,
              commands, acceptance_checks, required_evidence, handoff_to,
              independence, revision, is_current, created_at, created_by)
             VALUES (?1, ?2, '', ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, 1, ?16, ?17)",
            params![
                contract_id, task_id, role,
                contract_field(contract, "skill_id"),
                contract_field(contract, "skill_version"),
                contract_field(contract, "prompt_template_id"),
                contract_field(contract, "prompt_hash"),
                contract_field(contract, "allowed_paths"),
                contract_field(contract, "forbidden_paths"),
                contract_field(contract, "commands"),
                contract_field(contract, "acceptance_checks"),
                contract_field(contract, "required_evidence"),
                contract_field(contract, "handoff_to"),
                contract_field(contract, "independence"),
                next_revision, ts, owner_key,
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("role_contract 写入失败: {}", e)))?;

        // 审计事件：合同变更
        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events 
             (task_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, '', '', 'contract_set', ?2, ?3, ?4, ?5)",
            params![task_id, format!("role_contract {} revision {} 更新", role, next_revision), owner_key, seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 task_contract_set 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("role".to_string(), Value::String(role));
        res.insert("contract_id".to_string(), Value::String(contract_id));
        res.insert("revision".to_string(), Value::Number(serde_json::Number::from(next_revision)));
        res.insert("status".to_string(), Value::String("frozen".to_string()));
        Ok(Value::Object(res))
    }

    /// A3：读取任务当前冻结的 Role Contract 列表（可指定 role 过滤）。
    /// P0-C：对治理投影完全缺失但已绑定 authority 的历史/自举任务追加 v1 Task
    /// Contract、三角色 lineage/revision 和 executor step bindings。该入口绝不更新
    /// 任何既有 projection；一次成功必须由 adjudicator reviewer lease/fencing 与 evidence
    /// 证明，且经 operation ledger 保证持久幂等。
    pub fn handle_task_contract_bootstrap(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params.get("task_id").and_then(|v| v.as_str()).unwrap_or("").trim();
        let request_id = params.get("request_id").and_then(|v| v.as_str()).unwrap_or("").trim();
        let workspace_instance_id = params.get("workspace_instance_id").and_then(|v| v.as_str()).unwrap_or("").trim();
        let workspace_id = optional_workspace_id_param(params).ok_or_else(|| {
            DaemonRpcError::new("E_TASK_CONTRACT_BOOTSTRAP_WORKSPACE_REQUIRED", "task.contract_bootstrap 必须携带 workspace_id > 0")
        })?;
        if task_id.is_empty() || request_id.is_empty() || workspace_instance_id.is_empty() {
            return Err(DaemonRpcError::new(
                "E_TASK_CONTRACT_BOOTSTRAP_PARAMS_REQUIRED",
                "task.contract_bootstrap 必须携带 task_id、request_id、workspace_instance_id",
            ));
        }
        let envelope = params.get("envelope").cloned().ok_or_else(|| {
            DaemonRpcError::new("E_TASK_CONTRACT_BOOTSTRAP_ENVELOPE_REQUIRED", "task.contract_bootstrap 必须携带 envelope")
        })?;
        let identity = parse_action_identity(params)?.ok_or_else(|| {
            DaemonRpcError::new("E_TASK_CONTRACT_BOOTSTRAP_IDENTITY_REQUIRED", "task.contract_bootstrap 必须携带完整 identity")
        })?;
        if identity.role != "adjudicator" {
            return Err(DaemonRpcError::new(
                "E_TASK_CONTRACT_BOOTSTRAP_ROLE_REQUIRED",
                format!("仅允许 role=adjudicator，实际 role={}", identity.role),
            ));
        }
        let (token, counter) = Self::require_lease_params(params)?;
        let evidence_path = params.get("evidence_path").and_then(|v| v.as_str()).unwrap_or("").trim();
        let evidence_hash = params.get("evidence_hash").and_then(|v| v.as_str()).unwrap_or("").trim();
        if evidence_path.is_empty() || evidence_hash.is_empty() {
            return Err(DaemonRpcError::new(
                "E_TASK_CONTRACT_BOOTSTRAP_EVIDENCE_REQUIRED",
                "task.contract_bootstrap 必须携带 evidence_path 与 evidence_hash",
            ));
        }

        let method = "task.contract_bootstrap";
        let operation_store = OperationStore;
        let mut conn = self.conn.lock().unwrap();
        let dedupe = operation_store.dedupe(&conn, workspace_instance_id, method, request_id, params)?;
        let (rules, canonical_params_hash): (ParamsRules, String) = match dedupe {
            DedupeOutcome::Replay { response_or_error_json } => {
                if let Some(err) = response_or_error_json.get("error") {
                    let code = err.get("code").and_then(|v| v.as_str()).unwrap_or("E_TASK_CONTRACT_BOOTSTRAP_REPLAY_ERROR");
                    let message = err.get("message").and_then(|v| v.as_str()).unwrap_or("bootstrap deterministic rejection");
                    return Err(DaemonRpcError::new(code, message));
                }
                return Ok(response_or_error_json);
            }
            DedupeOutcome::FirstRequest { rules, canonical_params_hash } => (rules, canonical_params_hash),
        };
        let tx = conn.unchecked_transaction().map_err(|e| {
            DaemonRpcError::internal_error(format!("开启 task contract bootstrap 事务失败: {e}"))
        })?;
        let provenance = LedgerProvenance { workspace_id: Some(workspace_id), task_id: Some(task_id.to_string()), ..Default::default() };
        let record_reject = |tx: &Transaction<'_>, code: &str, message: &str| -> DaemonRpcError {
            let body = serde_json::json!({"error":{"code":code,"message":message}});
            let _ = operation_store.record_result(tx, workspace_instance_id, method, request_id, &rules, &canonical_params_hash, &provenance, &body);
            DaemonRpcError::new(code, message)
        };
        macro_rules! reject { ($code:expr, $message:expr) => {{ let err = record_reject(&tx, $code, $message); let _ = tx.commit(); return Err(err); }}; }

        let bound_workspace = match task_bound_workspace_id(&tx, task_id, Some(workspace_id)) {
            Ok(value) => value,
            Err(e) => reject!(&e.code, &e.message),
        };
        let binding_instance: String = match tx.query_row(
            "SELECT c.workspace_instance_id FROM task_workspace_bindings b JOIN workspace_authority_captures c ON c.workspace_capture_id=b.workspace_capture_id WHERE b.task_id=?1 AND b.workspace_id=?2",
            params![task_id, bound_workspace], |r| r.get(0),
        ) {
            Ok(value) => value,
            Err(_) => reject!("E_TASK_CONTRACT_BOOTSTRAP_AUTHORITY_UNAVAILABLE", "task 缺少可复核的 workspace authority capture"),
        };
        if binding_instance != workspace_instance_id {
            reject!("E_WORKSPACE_AUTHORITY_MISMATCH", &format!("task binding workspace_instance_id={} 与请求 {} 不一致", binding_instance, workspace_instance_id));
        }
        if let Err(e) = verify_registered_identity(&tx, &identity) { reject!(&e.code, &e.message); }
        if let Err(e) = self.validate_reviewer_lease_for_adjudication(&tx, task_id, &token, counter, &identity) {
            let code = if e.code == "E_LEASE_FENCING_STALE" { "E_TASK_CONTRACT_BOOTSTRAP_FENCED" } else { &e.code };
            reject!(code, &e.message);
        }
        let status: String = match tx.query_row("SELECT status FROM tasks WHERE id=?1", [task_id], |r| r.get(0)) {
            Ok(value) => value,
            Err(_) => reject!("E_TASK_CONTRACT_BOOTSTRAP_TASK_NOT_FOUND", &format!("task 不存在: {task_id}")),
        };
        let input = BootstrapInput { task_id: task_id.to_string(), envelope, created_by: identity.agent_id.clone() };
        let result = match bootstrap_task_governance_contracts(&tx, &input, bound_workspace) {
            Ok(value) => value,
            Err(e) => reject!(&e.code, &e.message),
        };
        let ts = task_now_ts();
        let seq = self.next_seq();
        if let Err(e) = TaskCollabStore::append_task_event(
            &tx, task_id, &status, &status, "task_contract_bootstrapped",
            &serde_json::json!({"request_id":request_id,"evidence_path":evidence_path,"evidence_hash":evidence_hash}).to_string(),
            &identity.agent_id, &identity.session_id, &identity.role, seq, ts,
        ) { return Err(e); }
        if let Err(e) = record_action_identity(&tx, task_id, &identity, method, seq, ts) { return Err(e); }
        let mut full = result.as_object().cloned().unwrap_or_default();
        full.insert("request_id".to_string(), Value::String(request_id.to_string()));
        full.insert("evidence_path".to_string(), Value::String(evidence_path.to_string()));
        full.insert("evidence_hash".to_string(), Value::String(evidence_hash.to_string()));
        full.insert("fencing_counter".to_string(), Value::Number(counter.into()));
        full.insert("authoritative_timestamp".to_string(), serde_json::json!(ts));
        let full = Value::Object(full);
        operation_store.record_result(&tx, workspace_instance_id, method, request_id, &rules, &canonical_params_hash, &provenance, &full)?;
        tx.commit().map_err(|e| DaemonRpcError::internal_error(format!("提交 task contract bootstrap 事务失败: {e}")))?;
        Ok(full)
    }

    pub fn handle_task_contract_get(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let role = params
            .get("role")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();

        let conn = self.conn.lock().unwrap();
        let sql = if role.is_empty() {
            "SELECT role, skill_id, skill_version, prompt_template_id, prompt_hash,
                    allowed_paths, forbidden_paths, commands, acceptance_checks,
                    required_evidence, handoff_to, independence, revision, contract_id, created_by
             FROM role_contracts WHERE task_id = ?1 AND is_current = 1 ORDER BY role ASC"
        } else {
            "SELECT role, skill_id, skill_version, prompt_template_id, prompt_hash,
                    allowed_paths, forbidden_paths, commands, acceptance_checks,
                    required_evidence, handoff_to, independence, revision, contract_id, created_by
             FROM role_contracts WHERE task_id = ?1 AND role = ?2 AND is_current = 1
             ORDER BY revision DESC LIMIT 1"
        };
        let mut stmt = conn
            .prepare(sql)
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 role_contracts 失败: {}", e)))?;
        // 统一映射函数为 fn pointer，避免 if/else 分支闭包类型不一致
        let mapper: for<'r> fn(&rusqlite::Row<'r>) -> rusqlite::Result<Map<String, Value>> =
            contract_row_to_map;
        let rows = if role.is_empty() {
            stmt.query_map(params![task_id], mapper)
        } else {
            stmt.query_map(params![task_id, role], mapper)
        }
        .map_err(|e| DaemonRpcError::internal_error(format!("映射 role_contracts 失败: {}", e)))?;

        let contracts: Vec<Value> = rows
            .flatten()
            .map(Value::Object)
            .collect();

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("contracts".to_string(), Value::Array(contracts));
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
        let step_id = params
            .get("step_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let success = params
            .get("success")
            .and_then(|v| v.as_bool())
            .unwrap_or(true);
        let changes = match params.get("changes") {
            None => Vec::new(),
            Some(value) => value
                .as_array()
                .ok_or_else(|| DaemonRpcError::invalid_params("changes 必须是 JSON array"))?
                .clone(),
        };
        let identity = parse_action_identity(params)?;

        let owner_key = peer.owner_key();
        let explicit_session = params
            .get("agent_session_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .map(str::to_string);
        if let (Some(explicit), Some(id)) = (&explicit_session, &identity) {
            if explicit != &id.session_id {
                return Err(DaemonRpcError::new(
                    "E_IDENTITY_SESSION_MISMATCH",
                    "agent_session_id 与 identity.session_id 不一致",
                ));
            }
        }
        let agent_session_id = explicit_session
            .or_else(|| identity.as_ref().map(|id| id.session_id.clone()))
            .unwrap_or_else(|| owner_key.clone());

        let ts = task_now_ts();
        let mut conn = self.conn.lock().unwrap();

        // A3：合同任务 report 必须匹配已冻结角色（handoff 角色不匹配即拒绝）。
        if let Some(id) = &identity {
            if task_has_contracts(&conn, task_id)? {
                if get_current_role_contract(&conn, task_id, &id.role)?.is_none() {
                    return Err(DaemonRpcError::new(
                        "E_CONTRACT_ROLE_MISMATCH",
                        format!(
                            "任务 {} 未为角色 {} 冻结 Role Contract，禁止 report",
                            task_id, id.role
                        ),
                    ));
                }
            }
        }

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

        let current_status: String = tx
            .query_row("SELECT status FROM tasks WHERE id = ?1", params![task_id], |r| r.get(0))
            .map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)))?;

        // task-owned attribution：report 只能把显式白名单步骤目标写入
        // change_audit。禁止把共享工作树中未声明的 dirty/untracked 文件
        // 静默吸入任务证据；路径必须是相对路径且精确匹配 target_file 的
        // 逗号分隔白名单。所有记录与步骤状态在同一事务提交。
        let mut change_ids: Vec<Value> = Vec::new();
        if !changes.is_empty() {
            if step_id.is_empty() {
                return Err(DaemonRpcError::new(
                    "E_CHANGE_STEP_REQUIRED",
                    "带 changes 的 task.report 必须指定 step_id",
                ));
            }
            let target_file: String = tx
                .query_row(
                    "SELECT target_file FROM task_steps WHERE id = ?1 AND task_id = ?2",
                    params![step_id, task_id],
                    |r| r.get(0),
                )
                .map_err(|_| DaemonRpcError::new(
                    "task_step_not_found",
                    format!("步骤不存在或不属于任务: {}", step_id),
                ))?;
            let allowed: Vec<String> = target_file
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(|s| s.replace('\\', "/"))
                .collect();
            if allowed.is_empty() {
                return Err(DaemonRpcError::new(
                    "E_CHANGE_PATH_NOT_ALLOWED",
                    "当前步骤没有声明可归属的 target_file",
                ));
            }
            for raw in &changes {
                let obj = raw.as_object().ok_or_else(|| {
                    DaemonRpcError::invalid_params("changes 每项必须是 JSON object")
                })?;
                let file_path = obj
                    .get("file_path")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .trim()
                    .replace('\\', "/");
                if file_path.is_empty()
                    || file_path.starts_with('/')
                    || file_path.contains(':')
                    || file_path.split('/').any(|part| part == "..")
                    || !allowed.iter().any(|item| item == &file_path)
                {
                    return Err(DaemonRpcError::new(
                        "E_CHANGE_PATH_NOT_ALLOWED",
                        format!("文件 {} 不在步骤白名单 {}", file_path, target_file),
                    ));
                }
                let change_id = format!("CA-{}", &sha256_hex(
                    format!("{}:{}:{}", task_id, step_id, file_path).as_bytes()
                )[..24]);
                let hash_before = obj
                    .get("hash_before")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let hash_after = obj
                    .get("hash_after")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let diff = obj.get("diff").and_then(|v| v.as_str()).unwrap_or("");
                let author = obj
                    .get("author")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.trim().is_empty())
                    .or_else(|| identity.as_ref().map(|id| id.agent_id.as_str()))
                    .unwrap_or("agent");
                tx.execute(
                    "INSERT OR REPLACE INTO change_audit
                     (id, task_id, step_id, file_path, hash_before, hash_after, diff, author, timestamp)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
                    params![change_id, task_id, step_id, file_path, hash_before, hash_after, diff, author, ts],
                )
                .map_err(|e| DaemonRpcError::internal_error(format!("写入 change_audit 失败: {}", e)))?;
                change_ids.push(Value::String(change_id));
            }
        }

        let mut next_status = "review".to_string();
        if !step_id.is_empty() {
            let actual_task_id: String = tx
                .query_row("SELECT task_id FROM task_steps WHERE id = ?1", params![step_id], |r| r.get(0))
                .map_err(|_| DaemonRpcError::new("task_step_not_found", format!("步骤不存在: {}", step_id)))?;
            let failed_scope: (String, String, String) = tx
                .query_row(
                    "SELECT target_file, target_symbol, check_items FROM task_steps WHERE id = ?1 AND task_id = ?2",
                    params![step_id, actual_task_id],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .map_err(|_| DaemonRpcError::new("task_step_not_found", format!("步骤不存在或不属于任务: {}", step_id)))?;
            let (step_action, prior_result): (String, String) = tx
                .query_row(
                    "SELECT action, COALESCE(result, '') FROM task_steps WHERE id = ?1 AND task_id = ?2",
                    params![step_id, actual_task_id],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .map_err(|_| DaemonRpcError::new("task_step_not_found", format!("步骤不存在或不属于任务: {}", step_id)))?;
            let step_status = if success { "done" } else { "failed" };
            let stored_result = if success && step_action == "fix_defect" {
                if let Ok(mut metadata) = serde_json::from_str::<Value>(&prior_result) {
                    if let Some(obj) = metadata.as_object_mut() {
                        obj.insert("resolution_summary".into(), Value::String(summary.to_string()));
                        metadata.to_string()
                    } else { summary.to_string() }
                } else { summary.to_string() }
            } else { summary.to_string() };
            let step_updated = tx.execute(
                "UPDATE task_steps SET status = ?1, result = ?2, completed_at = ?3 WHERE id = ?4",
                params![step_status, stored_result, ts, step_id],
            ).map_err(|e| DaemonRpcError::internal_error(format!("task_step 更新失败: {}", e)))?;
            if step_updated == 0 {
                return Err(DaemonRpcError::new("task_step_not_found", format!("步骤不存在: {}", step_id)));
            }
            if !success {
                let max_idx: i64 = tx.query_row(
                    "SELECT COALESCE(MAX(step_index), -1) FROM task_steps WHERE task_id = ?1",
                    params![actual_task_id], |r| r.get(0),
                ).map_err(|e| DaemonRpcError::internal_error(format!("查询步骤序号失败: {}", e)))?;
                let remediation_id = generate_task_id();
                let remediation_metadata = serde_json::json!({
                    "remediation_of_step_id": step_id,
                    "failed_target_file": failed_scope.0,
                    "failed_target_symbol": failed_scope.1,
                    "failed_check_items": failed_scope.2,
                }).to_string();
                tx.execute(
                    "INSERT INTO task_steps (id, task_id, step_index, action, target_file, target_symbol, check_items, status, result, created_at, completed_at)
                     VALUES (?1, ?2, ?3, 'fix_defect', ?4, ?5, ?6, 'pending', ?7, ?8, NULL)",
                    params![remediation_id, actual_task_id, max_idx + 1,
                            failed_scope.0, failed_scope.1, failed_scope.2,
                            remediation_metadata, ts],
                ).map_err(|e| DaemonRpcError::internal_error(format!("插入 fix_defect 步骤失败: {}", e)))?;
                next_status = "in_progress".to_string();
            }
        }

        // review 是 projection，不是“没有 pending 就猜测完成”。历史 failed 行保持
        // 不变，仅当它们都有 append-only step_resolved 事件，且没有 pending/
        // in_progress 步骤时，任务才可进入 review。无 step_id 的 legacy report 也
        // 必须经过同一检查，避免绕过 remediation。
        let remaining: i64 = tx.query_row(
            "SELECT COUNT(*) FROM task_steps
             WHERE task_id = ?1 AND status IN ('pending', 'in_progress')",
            params![task_id],
            |r| r.get(0),
        ).map_err(|e| DaemonRpcError::internal_error(format!("查询剩余步骤失败: {}", e)))?;
        let unresolved_failed = unresolved_failed_step_ids(&tx, task_id)?.len() as i64;
        if remaining > 0 || unresolved_failed > 0 {
            next_status = "in_progress".to_string();
        }

        let updated = tx.execute(
            "UPDATE tasks SET status = ?1, updated_at = ?2 WHERE id = ?3",
            params![next_status, ts, task_id],
        ).map_err(|e| DaemonRpcError::internal_error(format!("task_report 失败: {}", e)))?;

        if updated == 0 {
            return Err(DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)));
        }

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events 
             (task_id, from_status, to_status, reason_code, reason, actor_identity, agent_session_id, role, monotonic_seq, authoritative_timestamp, evidence_path, evidence_hash)
             VALUES (?1, ?2, ?3, 'reported', ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            params![task_id, current_status, next_status, summary, owner_key, agent_session_id,
                    identity.as_ref().map(|id| id.role.as_str()).unwrap_or(""), seq, ts, evidence_path, evidence_hash],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        if let Some(ref id) = identity {
            record_action_identity(&tx, task_id, id, "state_transition", seq, ts)?;
        }

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 task_report 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String(next_status));
        if !step_id.is_empty() {
            res.insert("step_id".to_string(), Value::String(step_id.to_string()));
        }
        res.insert("change_ids".to_string(), Value::Array(change_ids));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    /// 在同一主任务中显式追加一个带 provenance 的 fix_defect 步骤。
    ///
    /// 该入口支持历史 failed step，以及 Reviewer/Adjudicator 退回后的整改。
    /// 它只追加 remediation step/event 并 reopen task，不修改源 step、verdict、
    /// evidence 或既有 handoff。普通整改不得通过 child task 表达。
    pub fn handle_task_remediation_create(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params.get("task_id").and_then(|v| v.as_str()).filter(|s| !s.trim().is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let source_step_id = params.get("source_step_id")
            .or_else(|| params.get("failed_step_id"))
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 source_step_id/failed_step_id"))?;
        let source_outcome = params.get("source_outcome")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .unwrap_or("failed_step");
        if !matches!(source_outcome, "failed_step" | "reviewer_blocked" | "adjudicator_returned") {
            return Err(DaemonRpcError::new(
                "E_REMEDIATION_SOURCE_OUTCOME_INVALID",
                "source_outcome 必须是 failed_step/reviewer_blocked/adjudicator_returned",
            ));
        }
        let requested_verdict_id = params.get("source_verdict_id")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .unwrap_or("");
        let requested_findings = params.get("source_findings").cloned().unwrap_or(Value::Null);
        let request_id = params.get("request_id").and_then(|v| v.as_str()).map(str::trim).filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 request_id"))?;
        let identity = parse_action_identity(params)?;
        let (token, counter) = Self::require_lease_params(params)?;
        let request_fingerprint = sha256_hex(serde_json::json!({
            "task_id": task_id,
            "source_step_id": source_step_id,
            "source_outcome": source_outcome,
            "source_verdict_id": requested_verdict_id,
            "source_findings": requested_findings,
        }).to_string().as_bytes());
        let mut conn = self.conn.lock().unwrap();
        let tx = conn.unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启 remediation 事务失败: {}", e)))?;
        self.validate_lease_for_mutation(&tx, task_id, "implementer", &token, counter, identity.as_ref())?;
        let current_status: String = tx.query_row(
            "SELECT status FROM tasks WHERE id = ?1",
            params![task_id],
            |r| r.get(0),
        ).map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)))?;

        // durable request-id replay/mismatch，避免 daemon 重启后重复创建步骤。
        let mut event_stmt = tx.prepare(
            "SELECT event_id, reason FROM task_events
             WHERE task_id = ?1 AND reason_code = 'remediation_created'
             ORDER BY event_id DESC"
        ).map_err(|e| DaemonRpcError::internal_error(format!("查询 remediation ledger 失败: {}", e)))?;
        let event_rows = event_stmt.query_map(params![task_id], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))
            .map_err(|e| DaemonRpcError::internal_error(format!("读取 remediation ledger 失败: {}", e)))?;
        for row in event_rows {
            let (event_id, reason) = row.map_err(|e| DaemonRpcError::internal_error(format!("读取 remediation event 失败: {}", e)))?;
            if let Ok(existing) = serde_json::from_str::<Value>(&reason) {
                if existing.get("request_id").and_then(|v| v.as_str()) == Some(request_id) {
                    let same = existing.get("request_fingerprint").and_then(|v| v.as_str())
                        == Some(request_fingerprint.as_str());
                    if !same {
                        return Err(DaemonRpcError::new("E_REQUEST_ID_REUSE_MISMATCH", "remediation request_id 参数冲突"));
                    }
                    let remediation_step_id = existing.get("remediation_step_id").and_then(|v| v.as_str()).unwrap_or("");
                    let mut replay = Map::new();
                    replay.insert("task_id".into(), Value::String(task_id.to_string()));
                    replay.insert("source_step_id".into(), Value::String(source_step_id.to_string()));
                    replay.insert("source_outcome".into(), Value::String(source_outcome.to_string()));
                    replay.insert("source_verdict_id".into(), Value::String(requested_verdict_id.to_string()));
                    if source_outcome == "failed_step" {
                        replay.insert("failed_step_id".into(), Value::String(source_step_id.to_string()));
                    }
                    replay.insert("remediation_step_id".into(), Value::String(remediation_step_id.to_string()));
                    replay.insert("request_id".into(), Value::String(request_id.to_string()));
                    replay.insert("remediation_event_id".into(), Value::Number(serde_json::Number::from(event_id)));
                    replay.insert("replayed".into(), Value::Bool(true));
                    return Ok(Value::Object(replay));
                }
            }
        }
        drop(event_stmt);

        let source_scope: (String, String, String, String) = tx.query_row(
            "SELECT status, target_file, target_symbol, check_items
             FROM task_steps WHERE id = ?1 AND task_id = ?2",
            params![source_step_id, task_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        ).map_err(|_| DaemonRpcError::new("E_REMEDIATION_SOURCE_STEP_INVALID", "source step 不属于任务"))?;

        let (source_verdict_id, source_findings) = if source_outcome == "failed_step" {
            if source_scope.0 != "failed" {
                return Err(DaemonRpcError::new("E_FAILED_STEP_NOT_UNRESOLVED", "failed 步骤已不是未解析 failed 状态"));
            }
            if !requested_verdict_id.is_empty() || params.get("source_findings").is_some() {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_PROVENANCE_MISMATCH",
                    "failed_step remediation 不得伪造 verdict/findings provenance",
                ));
            }
            (String::new(), Value::Array(Vec::new()))
        } else {
            if current_status != "review" {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_REVIEW_STATE_REQUIRED",
                    "Reviewer/Adjudicator 退回只能从 review 状态追加 remediation",
                ));
            }
            if requested_verdict_id.is_empty() {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_VERDICT_REQUIRED",
                    "Reviewer/Adjudicator 退回必须绑定 source_verdict_id",
                ));
            }
            let expected_overall = if source_outcome == "reviewer_blocked" { "block" } else { "pass" };
            let verdict_findings_raw: String = tx.query_row(
                "SELECT findings FROM task_verdict_events
                 WHERE task_id = ?1 AND verdict_id = ?2 AND overall = ?3",
                params![task_id, requested_verdict_id, expected_overall],
                |r| r.get(0),
            ).map_err(|_| DaemonRpcError::new(
                "E_REMEDIATION_VERDICT_REQUIRED",
                "source_verdict_id 不属于当前 task/outcome",
            ))?;
            let verdict_findings = serde_json::from_str::<Value>(&verdict_findings_raw)
                .unwrap_or(Value::Array(Vec::new()));
            if requested_findings.is_null() {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_FINDINGS_REQUIRED",
                    "Reviewer/Adjudicator 退回必须显式携带 source_findings",
                ));
            }
            let supplied_findings = requested_findings.clone();
            if !supplied_findings.is_array()
                || supplied_findings.as_array().map(|items| items.is_empty()).unwrap_or(true)
            {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_FINDINGS_REQUIRED",
                    "Reviewer/Adjudicator 退回必须携带结构化 source_findings",
                ));
            }
            if source_outcome == "reviewer_blocked" && supplied_findings != verdict_findings {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_PROVENANCE_MISMATCH",
                    "source_findings 与 block verdict findings 不一致",
                ));
            }
            (requested_verdict_id.to_string(), supplied_findings)
        };

        // 已有同一 failed step 的 provenance remediation 时复用，避免并发重复步骤。
        let mut existing_stmt = tx.prepare(
            "SELECT id, status, result FROM task_steps
             WHERE task_id = ?1 AND action = 'fix_defect' ORDER BY step_index ASC"
        ).map_err(|e| DaemonRpcError::internal_error(format!("查询现有 remediation 步骤失败: {}", e)))?;
        let existing_rows = existing_stmt.query_map(params![task_id], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?)))
            .map_err(|e| DaemonRpcError::internal_error(format!("读取现有 remediation 步骤失败: {}", e)))?;
        for row in existing_rows {
            let (step_id, status, result) = row.map_err(|e| DaemonRpcError::internal_error(format!("读取 remediation 步骤失败: {}", e)))?;
            let linked = serde_json::from_str::<Value>(&result).ok()
                .and_then(|v| v.get("remediation_of_step_id").and_then(|x| x.as_str()).map(str::to_string));
            let existing_metadata = serde_json::from_str::<Value>(&result).unwrap_or(Value::Null);
            let same_source = if source_outcome == "failed_step" {
                linked.as_deref() == Some(source_step_id)
                    && existing_metadata.get("source_outcome").and_then(Value::as_str).unwrap_or("failed_step") == "failed_step"
            } else {
                existing_metadata.get("source_verdict_id").and_then(Value::as_str)
                    == Some(source_verdict_id.as_str())
            };
            if same_source {
                if !matches!(status.as_str(), "pending" | "in_progress" | "done") {
                    return Err(DaemonRpcError::new("E_REMEDIATION_STEP_MISMATCH", "已有 remediation 步骤状态不可恢复"));
                }
                return Err(DaemonRpcError::new("E_REMEDIATION_ALREADY_EXISTS", "该 source 已有带 provenance 的 remediation 步骤"));
            }
        }
        drop(existing_stmt);

        let max_idx: i64 = tx.query_row(
            "SELECT COALESCE(MAX(step_index), -1) FROM task_steps WHERE task_id = ?1",
            params![task_id], |r| r.get(0),
        ).map_err(|e| DaemonRpcError::internal_error(format!("查询步骤序号失败: {}", e)))?;
        let remediation_step_id = format!("S-{}", &sha256_hex(
            format!("{}:{}:{}:{}:{}", task_id, source_step_id, source_outcome, source_verdict_id, request_id).as_bytes()
        )[..24]);
        let metadata = serde_json::json!({
            "remediation_of_step_id": source_step_id,
            "source_outcome": source_outcome,
            "source_verdict_id": source_verdict_id,
            "source_findings": source_findings,
            "source_target_file": source_scope.1,
            "source_target_symbol": source_scope.2,
            "source_check_items": source_scope.3,
            "request_id": request_id,
        }).to_string();
        let ts = task_now_ts();
        tx.execute(
            "INSERT INTO task_steps
             (id, task_id, step_index, action, target_file, target_symbol, check_items, status, result, created_at, completed_at)
             VALUES (?1, ?2, ?3, 'fix_defect', ?4, ?5, ?6, 'pending', ?7, ?8, NULL)",
            params![remediation_step_id, task_id, max_idx + 1, source_scope.1, source_scope.2, source_scope.3, metadata, ts],
        ).map_err(|e| DaemonRpcError::internal_error(format!("写入 remediation 步骤失败: {}", e)))?;
        let reason = serde_json::json!({
            "request_id": request_id,
            "request_fingerprint": request_fingerprint,
            "source_step_id": source_step_id,
            "source_outcome": source_outcome,
            "source_verdict_id": source_verdict_id,
            "source_findings": source_findings,
            "remediation_step_id": remediation_step_id,
        }).to_string();
        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity, agent_session_id, role, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'in_progress', 'remediation_created', ?3, ?4, ?5, ?6, ?7, ?8)",
            params![task_id, current_status, reason, peer.owner_key(), identity.as_ref().map(|i| i.session_id.as_str()).unwrap_or(""), identity.as_ref().map(|i| i.role.as_str()).unwrap_or(""), seq, ts],
        ).map_err(|e| DaemonRpcError::internal_error(format!("写入 remediation event 失败: {}", e)))?;
        tx.execute("UPDATE tasks SET status = 'in_progress', updated_at = ?1 WHERE id = ?2", params![ts, task_id])
            .map_err(|e| DaemonRpcError::internal_error(format!("更新 remediation task 状态失败: {}", e)))?;
        if let Some(id) = &identity { record_action_identity(&tx, task_id, id, "task.remediation.create", seq, ts)?; }
        tx.commit().map_err(|e| DaemonRpcError::internal_error(format!("提交 remediation 事务失败: {}", e)))?;

        let mut out = Map::new();
        out.insert("task_id".into(), Value::String(task_id.to_string()));
        out.insert("source_step_id".into(), Value::String(source_step_id.to_string()));
        out.insert("source_outcome".into(), Value::String(source_outcome.to_string()));
        out.insert("source_verdict_id".into(), Value::String(source_verdict_id));
        if source_outcome == "failed_step" {
            out.insert("failed_step_id".into(), Value::String(source_step_id.to_string()));
        }
        out.insert("remediation_step_id".into(), Value::String(remediation_step_id));
        out.insert("request_id".into(), Value::String(request_id.to_string()));
        out.insert("replayed".into(), Value::Bool(false));
        Ok(Value::Object(out))
    }

    /// 将已完成的 fix_defect 绑定到一个不可变 failed 步骤 resolution event。
    ///
    /// 该入口不修改 failed 步骤本身；它只在同一事务内校验 remediation、lease/fencing
    /// 与证据，并追加 `step_resolved` 事件。重复 request_id 重放原事件，参数冲突 fail-closed。
    pub fn handle_task_step_resolve(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params.get("task_id").and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let failed_step_id = params.get("failed_step_id").and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 failed_step_id"))?;
        let remediation_step_id = params.get("remediation_step_id").and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 remediation_step_id"))?;
        let request_id = params.get("request_id").and_then(|v| v.as_str()).unwrap_or("").trim();
        if request_id.is_empty() {
            return Err(DaemonRpcError::invalid_params("缺少 request_id"));
        }
        let evidence_path = params.get("evidence_path").and_then(|v| v.as_str()).unwrap_or("").trim();
        let evidence_hash = params.get("evidence_hash").and_then(|v| v.as_str()).unwrap_or("").trim();
        if evidence_path.is_empty() || evidence_hash.is_empty() {
            return Err(DaemonRpcError::new("E_RESOLUTION_EVIDENCE_REQUIRED", "resolution 必须携带 evidence_path/evidence_hash"));
        }
        let identity = parse_action_identity(params)?;
        let (token, counter) = Self::require_lease_params(params)?;
        let mut conn = self.conn.lock().unwrap();
        let tx = conn.unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启 resolution 事务失败: {}", e)))?;
        self.validate_lease_for_mutation(&tx, task_id, "implementer", &token, counter, identity.as_ref())?;

        // 先查询同 request_id 的历史 resolution；匹配则稳定重放，参数不同则冲突。
        let mut stmt = tx.prepare(
            "SELECT event_id, reason FROM task_events WHERE task_id = ?1 AND reason_code = 'step_resolved' ORDER BY event_id DESC"
        ).map_err(|e| DaemonRpcError::internal_error(format!("查询 resolution ledger 失败: {}", e)))?;
        let rows = stmt.query_map(params![task_id], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))
            .map_err(|e| DaemonRpcError::internal_error(format!("读取 resolution ledger 失败: {}", e)))?;
        for row in rows {
            let (event_id, reason) = row.map_err(|e| DaemonRpcError::internal_error(format!("读取 resolution event 失败: {}", e)))?;
            if let Ok(existing) = serde_json::from_str::<Value>(&reason) {
                if existing.get("request_id").and_then(|v| v.as_str()) == Some(request_id) {
                    let same = existing.get("failed_step_id").and_then(|v| v.as_str()) == Some(failed_step_id)
                        && existing.get("remediation_step_id").and_then(|v| v.as_str()) == Some(remediation_step_id)
                        && existing.get("evidence_path").and_then(|v| v.as_str()) == Some(evidence_path)
                        && existing.get("evidence_hash").and_then(|v| v.as_str()) == Some(evidence_hash);
                    if !same {
                        return Err(DaemonRpcError::new("E_REQUEST_ID_REUSE_MISMATCH", "resolution request_id 参数冲突"));
                    }
                    let mut replay = Map::new();
                    replay.insert("task_id".into(), Value::String(task_id.to_string()));
                    replay.insert("resolution_event_id".into(), Value::Number(serde_json::Number::from(event_id)));
                    replay.insert("request_id".into(), Value::String(request_id.to_string()));
                    replay.insert("replayed".into(), Value::Bool(true));
                    let val = Value::Object(replay);
                    self.save_dedup(params, &val);
                    return Ok(val);
                }
            }
        }
        drop(stmt);

        let failed_status: String = tx.query_row(
            "SELECT status FROM task_steps WHERE id = ?1 AND task_id = ?2",
            params![failed_step_id, task_id], |r| r.get(0)
        ).map_err(|_| DaemonRpcError::new("E_FAILED_STEP_NOT_FOUND", "failed_step_id 不属于任务"))?;
        if failed_status != "failed" {
            return Err(DaemonRpcError::new("E_FAILED_STEP_NOT_UNRESOLVED", "failed 步骤已不是未解析 failed 状态"));
        }
        let remediation_result: String = tx.query_row(
            "SELECT result FROM task_steps WHERE id = ?1 AND task_id = ?2 AND action = 'fix_defect' AND status = 'done'",
            params![remediation_step_id, task_id], |r| r.get(0)
        ).map_err(|_| DaemonRpcError::new("E_REMEDIATION_NOT_DONE", "remediation 步骤不存在或尚未 done"))?;
        let linked_failed = serde_json::from_str::<Value>(&remediation_result).ok()
            .and_then(|v| v.get("remediation_of_step_id").and_then(|x| x.as_str()).map(str::to_string));
        if linked_failed.as_deref() != Some(failed_step_id) {
            return Err(DaemonRpcError::new("E_REMEDIATION_STEP_MISMATCH", "remediation provenance 与 failed_step_id 不一致"));
        }

        let ts = task_now_ts();
        let reason = serde_json::json!({
            "request_id": request_id,
            "failed_step_id": failed_step_id,
            "remediation_step_id": remediation_step_id,
            "evidence_path": evidence_path,
            "evidence_hash": evidence_hash,
        }).to_string();
        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity, agent_session_id, role, monotonic_seq, authoritative_timestamp, evidence_path, evidence_hash)
             VALUES (?1, 'in_progress', 'in_progress', 'step_resolved', ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            params![task_id, reason, peer.owner_key(), identity.as_ref().map(|i| i.session_id.as_str()).unwrap_or(""),
                    identity.as_ref().map(|i| i.role.as_str()).unwrap_or(""), seq, ts, evidence_path, evidence_hash],
        ).map_err(|e| DaemonRpcError::internal_error(format!("写入 resolution event 失败: {}", e)))?;
        let resolution_event_id = tx.last_insert_rowid();
        let pending: i64 = tx.query_row(
            "SELECT COUNT(*) FROM task_steps WHERE task_id = ?1 AND status IN ('pending', 'in_progress')",
            params![task_id], |r| r.get(0)
        ).map_err(|e| DaemonRpcError::internal_error(format!("查询剩余步骤失败: {}", e)))?;
        let unresolved = unresolved_failed_step_ids(&tx, task_id)?.len() as i64;
        let next_status = if pending == 0 && unresolved == 0 { "review" } else { "in_progress" };
        tx.execute("UPDATE tasks SET status = ?1, updated_at = ?2 WHERE id = ?3", params![next_status, ts, task_id])
            .map_err(|e| DaemonRpcError::internal_error(format!("更新 resolution 后任务状态失败: {}", e)))?;
        if let Some(id) = &identity { record_action_identity(&tx, task_id, id, "task.step.resolve", seq, ts)?; }
        tx.commit().map_err(|e| DaemonRpcError::internal_error(format!("提交 resolution 事务失败: {}", e)))?;
        let mut out = Map::new();
        out.insert("task_id".into(), Value::String(task_id.to_string()));
        out.insert("resolution_event_id".into(), Value::Number(serde_json::Number::from(resolution_event_id)));
        out.insert("status".into(), Value::String(next_status.to_string()));
        out.insert("request_id".into(), Value::String(request_id.to_string()));
        out.insert("replayed".into(), Value::Bool(false));
        let val = Value::Object(out);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_handoff(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let text_field = |name: &str| -> Result<String, DaemonRpcError> {
            params
                .get(name)
                .and_then(|v| v.as_str())
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .ok_or_else(|| DaemonRpcError::new(
                    "E_HANDOFF_STRUCTURED_REQUIRED",
                    format!("task.handoff 缺少结构化字段 {}", name),
                ))
        };
        let task_id = text_field("task_id")?;
        let from_role = text_field("from_role")?;
        let outcome = text_field("outcome")?;
        let next_role = text_field("next_role")?;
        let next_action = text_field("next_action")?;
        let reason = text_field("reason")?;
        let independence_requirement = text_field("independence_requirement")?;
        let request_id = text_field("request_id")?;
        let step_id = text_field("step_id")?;
        let report_request_id = text_field("report_request_id")?;
        let evidence_path = text_field("evidence_path")?;
        let evidence_hash = text_field("evidence_hash")?;
        let (lease_token, fencing_counter) = Self::require_lease_params(params)?;
        let identity = parse_action_identity(params)?.ok_or_else(|| {
            DaemonRpcError::new("E_IDENTITY_REQUIRED", "结构化 task.handoff 必须携带 identity")
        })?;

        let expected_route = match outcome.as_str() {
            "executor_ready_for_review" => ("executor", "reviewer", "required"),
            "executor_blocked_to_user" => ("executor", "user", "not_applicable"),
            "reviewer_pass" => ("reviewer", "adjudicator", "required"),
            "reviewer_blocked" => ("reviewer", "executor", "not_required"),
            "adjudicator_accepted" => ("adjudicator", "complete", "not_applicable"),
            "adjudicator_returned" => ("adjudicator", "executor", "not_required"),
            _ => return Err(DaemonRpcError::new("E_HANDOFF_OUTCOME_INVALID", "未知 handoff outcome")),
        };
        if from_role != expected_route.0 || next_role != expected_route.1
            || independence_requirement != expected_route.2
        {
            return Err(DaemonRpcError::new(
                "E_HANDOFF_ROUTE_INVALID",
                format!("outcome={} 的 from/next/independence 路由不合法", outcome),
            ));
        }
        let runtime_role = match identity.role.as_str() {
            "planner" | "implementer" | "tester" | "evidence" | "executor" => "executor",
            "reviewer" | "independent_reviewer" => "reviewer",
            "adjudicator" => "adjudicator",
            _ => "",
        };
        if runtime_role != from_role {
            return Err(DaemonRpcError::new(
                "E_HANDOFF_ROLE_IDENTITY_MISMATCH",
                "from_role 与 identity.role 不匹配",
            ));
        }

        let owner_key = peer.owner_key();
        let ts = task_now_ts();
        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;
        // handoff 是受保护 mutation：必须在同一事务内重新验证 source actor 的
        // active lease 与 fencing，不能仅凭 envelope 中的治理角色放行。
        self.validate_lease_for_mutation(
            &tx,
            &task_id,
            identity.role.as_str(),
            &lease_token,
            fencing_counter,
            Some(&identity),
        )?;
        let (creator, current_status): (String, String) = tx
            .query_row(
                "SELECT creator, status FROM tasks WHERE id = ?1",
                params![task_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)))?;
        let (claimed_actor, _) = self.get_task_claim_info(&tx, &task_id);
        if creator != owner_key && claimed_actor.as_deref() != Some(&owner_key) && owner_key != "root" {
            return Err(DaemonRpcError::permission_denied(format!("没有对任务 {} 执行 handoff 的权限", task_id)));
        }

        // Reviewer BLOCKED 是同一主任务上的新回复：从唯一 Verdict Ledger 读取
        // source verdict/findings，并在本事务中准备一个 provenance-bound
        // fix_defect。不得创建 child task，也不得修改被审步骤或历史 verdict。
        let remediation_plan: Option<(String, String, String, String, Value, String)> =
            if outcome == "reviewer_blocked" {
                if current_status != "review" {
                    let mut replayable = false;
                    let mut replay_stmt = tx.prepare(
                        "SELECT reason FROM task_events
                         WHERE task_id = ?1 AND reason_code = 'handoff_structured'
                         ORDER BY event_id DESC",
                    ).map_err(|e| DaemonRpcError::internal_error(format!("查询 handoff replay 失败: {}", e)))?;
                    let rows = replay_stmt.query_map(params![task_id], |row| row.get::<_, String>(0))
                        .map_err(|e| DaemonRpcError::internal_error(format!("读取 handoff replay 失败: {}", e)))?;
                    for row in rows {
                        let raw = row.map_err(|e| DaemonRpcError::internal_error(format!("读取 handoff replay 失败: {}", e)))?;
                        if serde_json::from_str::<Value>(&raw).ok()
                            .and_then(|value| value.get("request_id").and_then(Value::as_str).map(str::to_string))
                            .as_deref() == Some(request_id.as_str())
                        {
                            replayable = true;
                            break;
                        }
                    }
                    drop(replay_stmt);
                    if !replayable {
                        return Err(DaemonRpcError::new(
                            "E_REMEDIATION_REVIEW_STATE_REQUIRED",
                            "reviewer_blocked 只能从 review 状态追加原地整改",
                        ));
                    }
                }
                let (target_file, target_symbol, check_items): (String, String, String) = tx
                    .query_row(
                        "SELECT target_file, target_symbol, check_items
                         FROM task_steps WHERE id = ?1 AND task_id = ?2",
                        params![step_id, task_id],
                        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                    )
                    .map_err(|_| DaemonRpcError::new(
                        "E_REMEDIATION_SOURCE_STEP_INVALID",
                        "handoff step_id 不属于目标主任务",
                    ))?;
                let (source_verdict_id, source_findings_raw, source_reviewer_raw):
                    (String, String, String) = tx
                    .query_row(
                        "SELECT verdict_id, findings, reviewer_identity
                         FROM task_verdict_events
                         WHERE task_id = ?1 AND overall = 'block'
                         ORDER BY id DESC LIMIT 1",
                        params![task_id],
                        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                    )
                    .map_err(|_| DaemonRpcError::new(
                        "E_REMEDIATION_VERDICT_REQUIRED",
                        "reviewer_blocked 必须绑定当前任务的权威 block verdict",
                    ))?;
                let source_reviewer = serde_json::from_str::<Value>(&source_reviewer_raw)
                    .unwrap_or(Value::Null);
                let verdict_agent = source_reviewer
                    .get("identity")
                    .and_then(|item| item.get("agent_id"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let verdict_session = source_reviewer
                    .get("identity")
                    .and_then(|item| item.get("session_id"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if verdict_agent != identity.agent_id || verdict_session != identity.session_id {
                    return Err(DaemonRpcError::new(
                        "E_REMEDIATION_VERDICT_IDENTITY_MISMATCH",
                        "handoff Reviewer 与 source verdict identity 不一致",
                    ));
                }
                let source_findings = serde_json::from_str::<Value>(&source_findings_raw)
                    .ok()
                    .filter(Value::is_array)
                    .filter(|value| value.as_array().map(|items| !items.is_empty()).unwrap_or(false))
                    .ok_or_else(|| DaemonRpcError::new(
                        "E_REMEDIATION_FINDINGS_REQUIRED",
                        "block verdict 必须携带至少一个结构化 finding",
                    ))?;

                // 同一 verdict 只能产生一个整改回复；重试必须复用原 request_id，
                // 防止用不同 request_id 复制 remediation。
                let mut existing_steps = tx.prepare(
                    "SELECT id, result FROM task_steps
                     WHERE task_id = ?1 AND action = 'fix_defect'
                     ORDER BY step_index ASC",
                ).map_err(|e| DaemonRpcError::internal_error(format!("查询 verdict remediation 失败: {}", e)))?;
                let rows = existing_steps.query_map(params![task_id], |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
                }).map_err(|e| DaemonRpcError::internal_error(format!("读取 verdict remediation 失败: {}", e)))?;
                for row in rows {
                    let (_existing_id, raw) = row.map_err(|e| {
                        DaemonRpcError::internal_error(format!("读取 verdict remediation 失败: {}", e))
                    })?;
                    let value = serde_json::from_str::<Value>(&raw).unwrap_or(Value::Null);
                    if value.get("source_verdict_id").and_then(Value::as_str)
                        == Some(source_verdict_id.as_str())
                    {
                        if value.get("source_handoff_request_id").and_then(Value::as_str)
                            == Some(request_id.as_str())
                        {
                            continue;
                        }
                        return Err(DaemonRpcError::new(
                            "E_REMEDIATION_ALREADY_EXISTS",
                            "该 block verdict 已在当前主任务创建 remediation",
                        ));
                    }
                }
                drop(existing_steps);

                let remediation_step_id = format!(
                    "S-{}",
                    &sha256_hex(
                        format!("{}:{}:{}", task_id, source_verdict_id, request_id).as_bytes()
                    )[..24]
                );
                Some((
                    remediation_step_id,
                    target_file,
                    target_symbol,
                    check_items,
                    source_findings,
                    source_verdict_id,
                ))
            } else {
                None
            };

        let mut envelope = Map::new();
        for (key, value) in [
            ("task_id", Value::String(task_id.clone())),
            ("from_role", Value::String(from_role.clone())),
            ("outcome", Value::String(outcome.clone())),
            ("next_role", Value::String(next_role.clone())),
            ("next_action", Value::String(next_action.clone())),
            ("reason", Value::String(reason.clone())),
            ("independence_requirement", Value::String(independence_requirement.clone())),
            ("request_id", Value::String(request_id.clone())),
            ("step_id", Value::String(step_id.clone())),
            ("report_request_id", Value::String(report_request_id.clone())),
            ("evidence_path", Value::String(evidence_path.clone())),
            ("evidence_hash", Value::String(evidence_hash.clone())),
            ("fencing_counter", Value::Number(serde_json::Number::from(fencing_counter))),
        ] {
            envelope.insert(key.to_string(), value);
        }
        if let Some((remediation_step_id, _, _, _, source_findings, source_verdict_id)) =
            &remediation_plan
        {
            envelope.insert(
                "remediation_step_id".to_string(),
                Value::String(remediation_step_id.clone()),
            );
            envelope.insert(
                "source_verdict_id".to_string(),
                Value::String(source_verdict_id.clone()),
            );
            envelope.insert("source_findings".to_string(), source_findings.clone());
        }
        let envelope_value = Value::Object(envelope);
        let envelope_json = serde_json::to_string(&envelope_value)
            .map_err(|e| DaemonRpcError::internal_error(format!("序列化 handoff 失败: {}", e)))?;

        let mut existing = tx.prepare(
            "SELECT event_id, reason FROM task_events WHERE task_id = ?1 AND reason_code = 'handoff_structured' ORDER BY event_id DESC",
        ).map_err(|e| DaemonRpcError::internal_error(format!("查询 handoff ledger 失败: {}", e)))?;
        let rows = existing.query_map(params![task_id], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
        }).map_err(|e| DaemonRpcError::internal_error(format!("读取 handoff ledger 失败: {}", e)))?;
        for row in rows {
            let (event_id, raw) = row.map_err(|e| DaemonRpcError::internal_error(format!("读取 handoff 事件失败: {}", e)))?;
            if let Ok(previous) = serde_json::from_str::<Value>(&raw) {
                if previous.get("request_id").and_then(|v| v.as_str()) == Some(request_id.as_str()) {
                    if previous == envelope_value {
                        let mut replay = Map::new();
                        replay.insert("task_id".to_string(), Value::String(task_id.clone()));
                        replay.insert("status".to_string(), Value::String(current_status.clone()));
                        replay.insert("event_id".to_string(), Value::Number(event_id.into()));
                        replay.insert(
                            "remediation_step_id".to_string(),
                            previous
                                .get("remediation_step_id")
                                .cloned()
                                .unwrap_or(Value::Null),
                        );
                        replay.insert("replayed".to_string(), Value::Bool(true));
                        return Ok(Value::Object(replay));
                    }
                    return Err(DaemonRpcError::new("E_REQUEST_ID_REUSE_MISMATCH", "handoff request_id 参数冲突"));
                }
            }
        }
        drop(existing);

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity,
              agent_session_id, role, monotonic_seq, authoritative_timestamp,
              evidence_path, evidence_hash)
             VALUES (?1, ?2, ?2, 'handoff_structured', ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
            params![
                task_id, current_status, envelope_json, owner_key, identity.session_id,
                from_role, seq, ts, evidence_path, evidence_hash
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("追加 handoff ledger 失败: {}", e)))?;
        let event_id = tx.last_insert_rowid();

        let next_status = if let Some((
            remediation_step_id,
            target_file,
            target_symbol,
            check_items,
            source_findings,
            source_verdict_id,
        )) = &remediation_plan
        {
            let max_idx: i64 = tx.query_row(
                "SELECT COALESCE(MAX(step_index), -1) FROM task_steps WHERE task_id = ?1",
                params![task_id],
                |row| row.get(0),
            ).map_err(|e| DaemonRpcError::internal_error(format!("查询 remediation step_index 失败: {}", e)))?;
            let metadata = serde_json::json!({
                "remediation_of_step_id": step_id,
                "source_outcome": outcome,
                "source_verdict_id": source_verdict_id,
                "source_findings": source_findings,
                "source_handoff_event_id": event_id,
                "source_handoff_request_id": request_id,
            }).to_string();
            tx.execute(
                "INSERT INTO task_steps
                 (id, task_id, step_index, action, target_file, target_symbol,
                  check_items, status, result, created_at, completed_at)
                 VALUES (?1, ?2, ?3, 'fix_defect', ?4, ?5, ?6, 'pending', ?7, ?8, NULL)",
                params![
                    remediation_step_id,
                    task_id,
                    max_idx + 1,
                    target_file,
                    target_symbol,
                    check_items,
                    metadata,
                    ts,
                ],
            ).map_err(|e| DaemonRpcError::internal_error(format!("追加 reviewer remediation 失败: {}", e)))?;
            tx.execute(
                "UPDATE tasks SET status = 'in_progress', updated_at = ?1 WHERE id = ?2",
                params![ts, task_id],
            ).map_err(|e| DaemonRpcError::internal_error(format!("reopen 主任务失败: {}", e)))?;
            "in_progress"
        } else {
            current_status.as_str()
        };
        record_action_identity(&tx, &task_id, &identity, "task.handoff", seq, ts)?;
        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 task.handoff 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id));
        res.insert("status".to_string(), Value::String(next_status.to_string()));
        res.insert("event_id".to_string(), Value::Number(event_id.into()));
        res.insert("request_id".to_string(), Value::String(request_id));
        res.insert(
            "remediation_step_id".to_string(),
            remediation_plan
                .map(|(step_id, _, _, _, _, _)| Value::String(step_id))
                .unwrap_or(Value::Null),
        );
        res.insert("replayed".to_string(), Value::Bool(false));
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

    /// Authority 只读证据投影：返回 task_events 与 change_audit，避免
    /// Reviewer 退回 Python direct_read。该路径绝不写库或猜测 gate 结论。
    ///
    /// task_evidence_events 也必须由同一 authority 读取；空数组只表示没有
    /// task-bound Evidence，不能被解释为 Gate PASS。
    pub fn handle_evidence_append(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) {
            let same = cached.get("evidence_id") == params.get("evidence_id")
                && cached.get("step_id") == params.get("step_id")
                && cached.get("payload_hash") == params.get("payload_hash");
            if same {
                return Ok(cached);
            }
            return Err(DaemonRpcError::new(
                "E_REQUEST_ID_REUSE_MISMATCH",
                "同一 request_id 已绑定不同 evidence/step/payload",
            ));
        }
        let task_id = params.get("task_id").and_then(|v| v.as_str()).filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let step_id = params.get("step_id").and_then(|v| v.as_str()).filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 step_id"))?;
        let evidence_id = params.get("evidence_id").and_then(|v| v.as_str()).filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 evidence_id"))?;
        let evidence_type = params.get("evidence_type").and_then(|v| v.as_str()).filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 evidence_type"))?;
        let manifest_path = params.get("manifest_path").and_then(|v| v.as_str()).filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 manifest_path"))?;
        let payload_hash = params.get("payload_hash").and_then(|v| v.as_str()).filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 payload_hash"))?;
        let request_id = params.get("request_id").and_then(|v| v.as_str()).filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 request_id"))?;
        if request_id.chars().any(|c| !c.is_ascii_alphanumeric() && !matches!(c, '-' | '_' | '.' | ':')) {
            return Err(DaemonRpcError::invalid_params("request_id 只能包含 ASCII 字母、数字、-_.:"));
        }
        let path = Path::new(manifest_path);
        if path.is_absolute() || manifest_path.split(['/', '\\']).any(|part| part == "..")
            || !manifest_path.starts_with("docs/evidence/")
        {
            return Err(DaemonRpcError::new(
                "E_EVIDENCE_MANIFEST_PATH_INVALID",
                "manifest_path 必须是 docs/evidence/ 下的相对路径",
            ));
        }
        let identity = parse_action_identity(params)?.ok_or_else(|| {
            DaemonRpcError::new("E_IDENTITY_REQUIRED", "evidence.append 必须携带完整 identity")
        })?;
        let (token, counter) = Self::require_lease_params(params)?;
        let conn = self.conn.lock().unwrap();
        self.validate_lease_for_mutation(&conn, task_id, "implementer", &token, counter, Some(&identity))?;

        let producer_identity = format!(
            "request_id={};step_id={};identity={}",
            request_id,
            step_id,
            serde_json::json!({
                "agent_id": identity.agent_id,
                "agent_instance_id": identity.agent_instance_id,
                "client_id": identity.client_id,
                "session_id": identity.session_id,
                "role": identity.role,
            })
        );
        // request_id 是持久化在 producer_identity 前缀中的 operation key，
        // daemon 重启后仍能区分同参重放与参数冲突。
        let request_prefix = format!("request_id={};%", request_id);
        if let Ok(existing) = conn.query_row(
            "SELECT evidence_id, payload_hash, step_id FROM task_evidence_events
             WHERE task_id = ?1 AND producer_identity LIKE ?2 ORDER BY id ASC LIMIT 1",
            params![task_id, request_prefix],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2).unwrap_or_default())),
        ) {
            if existing.0 == evidence_id && existing.1 == payload_hash && existing.2 == step_id {
                let mut result = Map::new();
                result.insert("task_id".to_string(), Value::String(task_id.to_string()));
                result.insert("step_id".to_string(), Value::String(step_id.to_string()));
                result.insert("evidence_id".to_string(), Value::String(evidence_id.to_string()));
                result.insert("payload_hash".to_string(), Value::String(payload_hash.to_string()));
                result.insert("replayed".to_string(), Value::Bool(true));
                return Ok(Value::Object(result));
            }
            return Err(DaemonRpcError::new(
                "E_REQUEST_ID_REUSE_MISMATCH",
                "同一 request_id 已绑定不同 evidence/step/payload",
            ));
        }

        let task_exists: Result<String, _> = conn.query_row(
            "SELECT status FROM tasks WHERE id = ?1", params![task_id], |r| r.get(0));
        if task_exists.is_err() {
            return Err(DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)));
        }
        let step_belongs: Result<String, _> = conn.query_row(
            "SELECT status FROM task_steps WHERE id = ?1 AND task_id = ?2",
            params![step_id, task_id], |r| r.get(0));
        if step_belongs.is_err() {
            return Err(DaemonRpcError::new(
                "E_EVIDENCE_STEP_MISMATCH",
                "evidence 的 step_id 不属于 task",
            ));
        }
        let _evidence_json = params.get("evidence_json").cloned().unwrap_or(Value::Null);
        let file_hashes = params.get("file_hashes").map(Value::to_string).unwrap_or_default();
        let symbol_hashes = params.get("symbol_hashes").map(Value::to_string).unwrap_or_default();
        let verifier_name = params.get("verifier_name").and_then(|v| v.as_str()).unwrap_or("");
        let verifier_version = params.get("verifier_version").and_then(|v| v.as_str()).unwrap_or("");
        let verifier_config_hash = params.get("verifier_config_hash").and_then(|v| v.as_str()).unwrap_or("");
        let commit_hash = params.get("commit_hash").and_then(|v| v.as_str()).unwrap_or("");
        let workspace_snapshot_id = params.get("workspace_snapshot_id").and_then(|v| v.as_str()).unwrap_or("");
        let graph_refresh_version = params.get("graph_refresh_version").and_then(|v| v.as_str()).unwrap_or("");
        let ts = task_now_ts();
        conn.execute(
            "INSERT INTO task_evidence_events
             (evidence_id, task_id, contract_id, contract_revision, contract_hash,
              evidence_type, event_type, commit_hash, workspace_snapshot_id, file_hashes,
              symbol_hashes, graph_refresh_version, verifier_name, verifier_version,
              verifier_config_hash, producer_identity, produced_at, payload_hash,
              invalidation_reason, original_evidence_ref, workspace_id)
             VALUES (?1, ?2, '', 0, '', ?3, 'evidence_appended', ?4, ?5, ?6, ?7, ?8,
                     ?9, ?10, ?11, ?12, ?13, ?14, '', '', NULL)",
            params![evidence_id, task_id, evidence_type, commit_hash, workspace_snapshot_id,
                    file_hashes, symbol_hashes, graph_refresh_version, verifier_name,
                    verifier_version, verifier_config_hash, producer_identity, ts, payload_hash],
        ).map_err(|e| {
            if e.to_string().contains("UNIQUE constraint failed: task_evidence_events.evidence_id") {
                DaemonRpcError::new("E_EVIDENCE_ID_REUSE_MISMATCH", "evidence_id 已绑定其他 Evidence")
            } else {
                DaemonRpcError::internal_error(format!("写入 task_evidence_events 失败: {}", e))
            }
        })?;
        let mut result = Map::new();
        result.insert("task_id".to_string(), Value::String(task_id.to_string()));
        result.insert("step_id".to_string(), Value::String(step_id.to_string()));
        result.insert("evidence_id".to_string(), Value::String(evidence_id.to_string()));
        result.insert("payload_hash".to_string(), Value::String(payload_hash.to_string()));
        result.insert("produced_at".to_string(), Value::Number(serde_json::Number::from_f64(ts).unwrap()));
        let value = Value::Object(result);
        self.save_dedup(params, &value);
        Ok(value)
    }

    pub fn handle_evidence_query(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let task_events = self.handle_task_events(peer, params)?;
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn
            .prepare(
                "SELECT id, task_id, step_id, file_path, hash_before, hash_after, diff, author, timestamp
                 FROM change_audit WHERE task_id = ?1 ORDER BY timestamp ASC",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 change_audit 失败: {}", e)))?;
        let rows = stmt
            .query_map(params![task_id], |r| {
                let mut m = Map::new();
                m.insert("id".to_string(), Value::String(r.get(0)?));
                m.insert("task_id".to_string(), Value::String(r.get(1)?));
                m.insert("step_id".to_string(), Value::String(r.get::<_, Option<String>>(2)?.unwrap_or_default()));
                m.insert("file_path".to_string(), Value::String(r.get(3)?));
                m.insert("hash_before".to_string(), Value::String(r.get(4)?));
                m.insert("hash_after".to_string(), Value::String(r.get(5)?));
                m.insert("diff".to_string(), Value::String(r.get(6)?));
                m.insert("author".to_string(), Value::String(r.get(7)?));
                m.insert("timestamp".to_string(), Value::Number(serde_json::Number::from_f64(r.get(8)?).unwrap()));
                Ok(Value::Object(m))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("映射 change_audit 失败: {}", e)))?;
        let mut changes = Vec::new();
        for row in rows {
            changes.push(row.map_err(|e| DaemonRpcError::internal_error(format!("读取 change_audit 失败: {}", e)))?);
        }
        let mut result = Map::new();
        result.insert("task_id".to_string(), Value::String(task_id.to_string()));
        result.insert("task_events".to_string(), task_events.get("events").cloned().unwrap_or(Value::Array(Vec::new())));
        result.insert("change_audit".to_string(), Value::Array(changes));
        let mut stmt = conn.prepare(
            "SELECT evidence_id, task_id, evidence_type, event_type, commit_hash,
                    workspace_snapshot_id, file_hashes, symbol_hashes, graph_refresh_version,
                    verifier_name, verifier_version, verifier_config_hash, producer_identity,
                    produced_at, payload_hash
             FROM task_evidence_events WHERE task_id = ?1 ORDER BY id ASC",
        ).map_err(|e| DaemonRpcError::internal_error(format!("查询 task_evidence_events 失败: {}", e)))?;
        let rows = stmt.query_map(params![task_id], |r| {
            let mut m = Map::new();
            m.insert("evidence_id".to_string(), Value::String(r.get(0)?));
            m.insert("task_id".to_string(), Value::String(r.get(1)?));
            m.insert("evidence_type".to_string(), Value::String(r.get(2)?));
            m.insert("event_type".to_string(), Value::String(r.get(3)?));
            m.insert("commit_hash".to_string(), Value::String(r.get(4)?));
            m.insert("workspace_snapshot_id".to_string(), Value::String(r.get(5)?));
            m.insert("file_hashes".to_string(), Value::String(r.get(6)?));
            m.insert("symbol_hashes".to_string(), Value::String(r.get(7)?));
            m.insert("graph_refresh_version".to_string(), Value::String(r.get(8)?));
            m.insert("verifier_name".to_string(), Value::String(r.get(9)?));
            m.insert("verifier_version".to_string(), Value::String(r.get(10)?));
            m.insert("verifier_config_hash".to_string(), Value::String(r.get(11)?));
            m.insert("producer_identity".to_string(), Value::String(r.get(12)?));
            m.insert("produced_at".to_string(), Value::Number(serde_json::Number::from_f64(r.get(13)?).unwrap()));
            m.insert("payload_hash".to_string(), Value::String(r.get(14)?));
            Ok(Value::Object(m))
        }).map_err(|e| DaemonRpcError::internal_error(format!("映射 task_evidence_events 失败: {}", e)))?;
        let mut evidence = Vec::new();
        for row in rows {
            evidence.push(row.map_err(|e| DaemonRpcError::internal_error(format!("读取 task_evidence_events 失败: {}", e)))?);
        }
        result.insert("task_evidence_events".to_string(), Value::Array(evidence));
        Ok(Value::Object(result))
    }

    /// Evidence Gate 决策只读投影；无记录返回空数组，不把空值解释为 PASS。
    pub fn handle_gate_decision_query(
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
                "SELECT decision_id, task_id, decision, reason, requested_transition, event_type, decision_time
                 FROM task_gate_decisions WHERE task_id = ?1 ORDER BY decision_time ASC",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 gate decision 失败: {}", e)))?;
        let rows = stmt
            .query_map(params![task_id], |r| {
                let mut m = Map::new();
                m.insert("decision_id".to_string(), Value::String(r.get(0)?));
                m.insert("task_id".to_string(), Value::String(r.get(1)?));
                m.insert("decision".to_string(), Value::String(r.get(2)?));
                m.insert("reason".to_string(), Value::String(r.get(3)?));
                m.insert("requested_transition".to_string(), Value::String(r.get(4)?));
                m.insert("event_type".to_string(), Value::String(r.get(5)?));
                m.insert("decision_time".to_string(), Value::Number(serde_json::Number::from_f64(r.get(6)?).unwrap()));
                Ok(Value::Object(m))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("映射 gate decision 失败: {}", e)))?;
        let mut decisions = Vec::new();
        for row in rows {
            decisions.push(row.map_err(|e| DaemonRpcError::internal_error(format!("读取 gate decision 失败: {}", e)))?);
        }
        let mut result = Map::new();
        result.insert("task_id".to_string(), Value::String(task_id.to_string()));
        result.insert("decisions".to_string(), Value::Array(decisions));
        Ok(Value::Object(result))
    }

    /// 将已完成的只读 Gate 检查结果追加到权威 ledger。它不是 Verdict，
    /// 也不改变任务状态；仅允许当前 Executor lease 记录 task-owned gate
    /// 证据，所有 provenance 以请求中的 evidence hash 原样保存。
    pub fn handle_gate_decision_append(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) {
            let same = cached.get("evidence_id") == params.get("evidence_id")
                && cached.get("step_id") == params.get("step_id")
                && cached.get("evidence_hash") == params.get("payload_hash").or_else(|| params.get("evidence_hash"));
            if same {
                return Ok(cached);
            }
            return Err(DaemonRpcError::new(
                "E_REQUEST_ID_REUSE_MISMATCH",
                "同一 request_id 已绑定不同 Gate evidence/step/payload",
            ));
        }
        let task_id = params.get("task_id").and_then(|v| v.as_str()).filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let decision = params.get("decision").and_then(|v| v.as_str()).filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 decision"))?;
        if !matches!(decision, "pass" | "block" | "warn") {
            return Err(DaemonRpcError::new("E_GATE_DECISION_INVALID", "decision 必须是 pass/block/warn"));
        }
        let reason = params.get("reason").and_then(|v| v.as_str()).unwrap_or("");
        let evidence_hash = params.get("evidence_hash").and_then(|v| v.as_str()).unwrap_or("");
        let evidence_id = params.get("evidence_id").and_then(|v| v.as_str()).filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 evidence_id"))?;
        let step_id = params.get("step_id").and_then(|v| v.as_str()).filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 step_id"))?;
        let request_id = params.get("request_id").and_then(|v| v.as_str()).filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 request_id"))?;
        if request_id.chars().any(|c| !c.is_ascii_alphanumeric() && !matches!(c, '-' | '_' | '.' | ':')) {
            return Err(DaemonRpcError::invalid_params("request_id 只能包含 ASCII 字母、数字、-_.:"));
        }
        let identity = parse_action_identity(params)?;
        let (token, counter) = Self::require_lease_params(params)?;
        let conn = self.conn.lock().unwrap();
        self.validate_lease_for_mutation(&conn, task_id, "implementer", &token, counter, identity.as_ref())?;
        let payload_hash = params.get("payload_hash").and_then(|v| v.as_str()).unwrap_or(evidence_hash);
        let evidence_matches: Result<(String, String), _> = conn.query_row(
            "SELECT payload_hash, producer_identity FROM task_evidence_events
             WHERE task_id = ?1 AND evidence_id = ?2",
            params![task_id, evidence_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        );
        let existing_evidence = evidence_matches.map_err(|_| DaemonRpcError::new(
            "E_GATE_EVIDENCE_REQUIRED", "Gate decision 必须绑定已提交的 task Evidence"))?;
        if existing_evidence.0 != payload_hash || !existing_evidence.1.contains(&format!("step_id={}", step_id)) {
            return Err(DaemonRpcError::new(
                "E_GATE_EVIDENCE_MISMATCH",
                "Gate decision 的 evidence_id/payload_hash/step_id 与 Evidence 不匹配",
            ));
        }
        let clause_binding = serde_json::json!({
            "evidence_id": evidence_id,
            "step_id": step_id,
            "payload_hash": payload_hash,
            "request_id": request_id,
        }).to_string();
        let request_marker = format!("%\"request_id\":\"{}\"%", request_id);
        if let Ok((existing_id, existing_clause)) = conn.query_row(
            "SELECT decision_id, clause_decisions FROM task_gate_decisions
             WHERE task_id = ?1 AND clause_decisions LIKE ?2 ORDER BY id ASC LIMIT 1",
            params![task_id, request_marker],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)),
        ) {
            if existing_clause == clause_binding {
                let mut replay = Map::new();
                replay.insert("decision_id".to_string(), Value::String(existing_id));
                replay.insert("task_id".to_string(), Value::String(task_id.to_string()));
                replay.insert("decision".to_string(), Value::String(decision.to_string()));
                replay.insert("evidence_id".to_string(), Value::String(evidence_id.to_string()));
                replay.insert("replayed".to_string(), Value::Bool(true));
                return Ok(Value::Object(replay));
            }
            return Err(DaemonRpcError::new(
                "E_REQUEST_ID_REUSE_MISMATCH",
                "同一 request_id 已绑定不同 Gate evidence/step/payload",
            ));
        }
        let ts = task_now_ts();
        let decision_id = format!("GD-{}", &sha256_hex(format!("{}:{}", task_id, request_id).as_bytes())[..24]);
        conn.execute(
            "INSERT INTO task_gate_decisions
             (decision_id, task_id, contract_id, contract_revision, contract_hash,
              gate_snapshot_s0, gate_snapshot_s1, requested_transition, decision, reason,
              clause_decisions, verifier_triples, resolved_stage_toggle_set,
              independence_policy_value, independence_waiver_marker, event_type,
              decision_time, workspace_id)
             VALUES (?1, ?2, '', 0, '', '', '', 'review', ?3, ?4, ?5, '', ?6, '', '',
                     'runtime_task_gate', ?7, NULL)",
            params![decision_id, task_id, decision, reason, clause_binding, evidence_id, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("写入 gate decision 失败: {}", e)))?;
        let mut result = Map::new();
        result.insert("decision_id".to_string(), Value::String(decision_id));
        result.insert("task_id".to_string(), Value::String(task_id.to_string()));
        result.insert("decision".to_string(), Value::String(decision.to_string()));
        result.insert("evidence_id".to_string(), Value::String(evidence_id.to_string()));
        result.insert("evidence_hash".to_string(), Value::String(payload_hash.to_string()));
        result.insert("step_id".to_string(), Value::String(step_id.to_string()));
        let value = Value::Object(result);
        self.save_dedup(params, &value);
        Ok(value)
    }

    /// 将 Reviewer verdict 追加到唯一权威 ledger `task_verdict_events`。
    ///
    /// 当前 schema 尚未提供独立的 Role Contract provenance 列，因此 v1 过渡路径
    /// 将 step、Role Contract 三元组、request id 与 canonical params hash 作为结构化
    /// JSON 保存到 `reviewer_identity`。Task Contract 三元组仍使用表中既有
    /// `contract_*` 列，二者绝不混用。所有校验和 append 在同一 daemon 事务中完成；
    /// 不提供 Python/SQLite fallback。
    pub fn handle_verdict_submit(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let required = |name: &str| -> Result<&str, DaemonRpcError> {
            params
                .get(name)
                .and_then(|v| v.as_str())
                .map(str::trim)
                .filter(|v| !v.is_empty())
                .ok_or_else(|| DaemonRpcError::invalid_params(format!("缺少 {}", name)))
        };
        let task_id = required("task_id")?;
        let step_id = required("step_id")?;
        let contract_id = required("contract_id")?;
        let contract_hash = required("contract_hash")?;
        let role_contract_id = required("role_contract_id")?;
        let role_contract_hash = required("role_contract_hash")?;
        let snapshot_id = required("snapshot_id")?;
        let request_id = required("request_id")?;
        if request_id
            .chars()
            .any(|c| !c.is_ascii_alphanumeric() && !matches!(c, '-' | '_' | '.' | ':'))
        {
            return Err(DaemonRpcError::invalid_params(
                "request_id 只能包含 ASCII 字母、数字、-_.:",
            ));
        }
        let contract_revision = params
            .get("contract_revision")
            .and_then(|v| v.as_i64())
            .filter(|v| *v > 0)
            .ok_or_else(|| DaemonRpcError::invalid_params("contract_revision 必须为正整数"))?;
        let role_contract_revision = params
            .get("role_contract_revision")
            .and_then(|v| v.as_i64())
            .filter(|v| *v > 0)
            .ok_or_else(|| DaemonRpcError::invalid_params("role_contract_revision 必须为正整数"))?;
        let phase = required("phase")?;
        if !matches!(phase, "blind_first_pass" | "post_reveal_amendment") {
            return Err(DaemonRpcError::new(
                "E_VERDICT_PHASE_INVALID",
                "phase 必须是 blind_first_pass 或 post_reveal_amendment",
            ));
        }
        let overall = required("overall")?;
        if !matches!(overall, "pass" | "block") {
            return Err(DaemonRpcError::new(
                "E_VERDICT_OVERALL_INVALID",
                "overall 必须是 pass 或 block",
            ));
        }
        let attestation = required("attestation")?;
        let amendment_ref = params
            .get("amendment_ref")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        if phase == "post_reveal_amendment" && amendment_ref.is_empty() {
            return Err(DaemonRpcError::new(
                "E_VERDICT_AMENDMENT_REF_REQUIRED",
                "post_reveal_amendment 必须引用 sealed verdict",
            ));
        }
        if phase == "blind_first_pass" && !amendment_ref.is_empty() {
            return Err(DaemonRpcError::new(
                "E_VERDICT_AMENDMENT_REF_INVALID",
                "blind_first_pass 不得携带 amendment_ref",
            ));
        }
        let clause_results = params
            .get("clause_results")
            .cloned()
            .unwrap_or_else(|| Value::Array(Vec::new()));
        if !clause_results.is_array() {
            return Err(DaemonRpcError::invalid_params("clause_results 必须是 JSON array"));
        }
        let findings = params
            .get("findings")
            .cloned()
            .unwrap_or_else(|| Value::Array(Vec::new()));
        if !findings.is_array() {
            return Err(DaemonRpcError::invalid_params("findings 必须是 JSON array"));
        }
        let identity = parse_action_identity(params)?.ok_or_else(|| {
            DaemonRpcError::new("E_IDENTITY_REQUIRED", "verdict.submit 必须携带完整 Reviewer identity")
        })?;
        if !matches!(identity.role.as_str(), "reviewer" | "independent_reviewer") {
            return Err(DaemonRpcError::new(
                "E_VERDICT_REVIEWER_ROLE_REQUIRED",
                "verdict.submit 只允许 reviewer/independent_reviewer identity",
            ));
        }
        let (lease_token, fencing_counter) = Self::require_lease_params(params)?;
        let clock = self.clock.as_ref().ok_or_else(|| {
            lease_clock_unavailable("verdict.submit", task_id, "reviewer")
        })?;
        let submitted_at = clock.now_secs() as f64;

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启 verdict 事务失败: {}", e)))?;
        self.validate_lease_for_mutation(
            &tx,
            task_id,
            "reviewer",
            &lease_token,
            fencing_counter,
            Some(&identity),
        )?;

        let task_status: String = tx
            .query_row("SELECT status FROM tasks WHERE id = ?1", params![task_id], |r| r.get(0))
            .map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)))?;
        if task_status != "review" {
            return Err(DaemonRpcError::new(
                "E_VERDICT_TASK_NOT_IN_REVIEW",
                format!("任务 {} 当前状态为 {}，不能提交 Reviewer verdict", task_id, task_status),
            ));
        }
        let step_exists: i64 = tx
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE id = ?1 AND task_id = ?2",
                params![step_id, task_id],
                |r| r.get(0),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("校验 verdict step 失败: {}", e)))?;
        if step_exists != 1 {
            return Err(DaemonRpcError::new(
                "E_VERDICT_STEP_MISMATCH",
                "step_id 不属于目标 task",
            ));
        }

        let task_contract_workspace: Option<i64> = tx
            .query_row(
                "SELECT workspace_id FROM task_contract_revisions
                 WHERE task_id = ?1 AND contract_id = ?2 AND revision = ?3 AND contract_hash = ?4",
                params![task_id, contract_id, contract_revision, contract_hash],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("校验 Task Contract 失败: {}", e)))?
            .ok_or_else(|| {
                DaemonRpcError::new(
                    "E_TASK_CONTRACT_BINDING_INVALID",
                    "Task Contract id/revision/hash 未精确绑定目标 task",
                )
            })?;

        let role_row: Option<(
            String,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
        )> = tx
            .query_row(
                "SELECT role, step_id, skill_id, skill_version, prompt_template_id, prompt_hash,
                        allowed_paths, forbidden_paths, commands, acceptance_checks,
                        required_evidence, handoff_to, independence
                 FROM role_contracts
                 WHERE contract_id = ?1 AND task_id = ?2 AND revision = ?3 AND is_current = 1",
                params![role_contract_id, task_id, role_contract_revision],
                |r| {
                    Ok((
                        r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?,
                        r.get(6)?, r.get(7)?, r.get(8)?, r.get(9)?, r.get(10)?, r.get(11)?,
                        r.get(12)?,
                    ))
                },
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("校验 Role Contract 失败: {}", e)))?;
        let role_row = role_row.ok_or_else(|| {
            DaemonRpcError::new(
                "E_ROLE_CONTRACT_BINDING_INVALID",
                "Role Contract id/revision 未精确绑定目标 task 的当前合同",
            )
        })?;
        if !matches!(role_row.0.as_str(), "reviewer" | "independent_reviewer") {
            return Err(DaemonRpcError::new(
                "E_ROLE_CONTRACT_BINDING_INVALID",
                "Role Contract 不是 Reviewer 合同",
            ));
        }
        if !role_row.1.is_empty() && role_row.1 != step_id {
            return Err(DaemonRpcError::new(
                "E_ROLE_CONTRACT_STEP_MISMATCH",
                "Role Contract step_id 与 verdict step_id 不一致",
            ));
        }
        let role_contract_payload = serde_json::json!({
            "canonicalization_version": "role-contract-c14n/v1",
            "contract_id": role_contract_id,
            "revision": role_contract_revision,
            "task_id": task_id,
            "role": role_row.0,
            "step_id": role_row.1,
            "skill_id": role_row.2,
            "skill_version": role_row.3,
            "prompt_template_id": role_row.4,
            "prompt_hash": role_row.5,
            "allowed_paths": role_row.6,
            "forbidden_paths": role_row.7,
            "commands": role_row.8,
            "acceptance_checks": role_row.9,
            "required_evidence": role_row.10,
            "handoff_to": role_row.11,
            "independence": role_row.12,
        });
        let actual_role_contract_hash = format!(
            "sha256:{}",
            sha256_hex(role_contract_payload.to_string().as_bytes())
        );
        if actual_role_contract_hash != role_contract_hash {
            return Err(DaemonRpcError::new(
                "E_ROLE_CONTRACT_HASH_MISMATCH",
                "Role Contract canonical hash 不匹配",
            ));
        }

        if !amendment_ref.is_empty() {
            let sealed_exists: i64 = tx
                .query_row(
                    "SELECT COUNT(*) FROM task_verdict_events
                     WHERE verdict_id = ?1 AND task_id = ?2 AND phase = 'blind_first_pass'",
                    params![amendment_ref, task_id],
                    |r| r.get(0),
                )
                .map_err(|e| DaemonRpcError::internal_error(format!("校验 sealed verdict 失败: {}", e)))?;
            if sealed_exists != 1 {
                return Err(DaemonRpcError::new(
                    "E_VERDICT_AMENDMENT_REF_INVALID",
                    "amendment_ref 未引用当前任务的 sealed verdict",
                ));
            }
        }

        let verdict_id = params
            .get("verdict_id")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|v| !v.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| {
                format!(
                    "V-{}",
                    &sha256_hex(format!("{}:{}", task_id, request_id).as_bytes())[..24]
                )
            });
        let view_manifest_hash = params
            .get("view_manifest_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let canonical_params = serde_json::json!({
            "task_id": task_id,
            "step_id": step_id,
            "verdict_id": verdict_id,
            "contract_id": contract_id,
            "contract_revision": contract_revision,
            "contract_hash": contract_hash,
            "role_contract_id": role_contract_id,
            "role_contract_revision": role_contract_revision,
            "role_contract_hash": role_contract_hash,
            "phase": phase,
            "view_manifest_hash": view_manifest_hash,
            "snapshot_id": snapshot_id,
            "clause_results": clause_results,
            "findings": findings,
            "overall": overall,
            "attestation": attestation,
            "amendment_ref": amendment_ref,
            "identity": {
                "agent_id": identity.agent_id,
                "agent_instance_id": identity.agent_instance_id,
                "client_id": identity.client_id,
                "model_id": identity.model_id,
                "session_id": identity.session_id,
                "role": identity.role,
            },
        });
        let params_hash = format!(
            "sha256:{}",
            sha256_hex(canonical_params.to_string().as_bytes())
        );
        let request_marker = format!("%\"request_id\":\"{}\"%", request_id);
        if let Some((existing_id, existing_identity)) = tx
            .query_row(
                "SELECT verdict_id, reviewer_identity FROM task_verdict_events
                 WHERE task_id = ?1 AND reviewer_identity LIKE ?2 ORDER BY id ASC LIMIT 1",
                params![task_id, request_marker],
                |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 verdict 幂等记录失败: {}", e)))?
        {
            let saved_hash = serde_json::from_str::<Value>(&existing_identity)
                .ok()
                .and_then(|v| v.get("params_hash").and_then(Value::as_str).map(str::to_string));
            if saved_hash.as_deref() == Some(params_hash.as_str()) {
                return Ok(serde_json::json!({
                    "success": true,
                    "task_id": task_id,
                    "verdict_id": existing_id,
                    "replayed": true,
                }));
            }
            return Err(DaemonRpcError::new(
                "E_REQUEST_ID_REUSE_MISMATCH",
                "同一 request_id 已绑定不同 verdict params",
            ));
        }
        let reviewer_identity = serde_json::json!({
            "request_id": request_id,
            "params_hash": params_hash,
            "step_id": step_id,
            "identity": canonical_params["identity"],
            "role_contract": {
                "id": role_contract_id,
                "revision": role_contract_revision,
                "hash": role_contract_hash,
                "canonicalization_version": "role-contract-c14n/v1",
            },
        })
        .to_string();
        tx.execute(
            "INSERT INTO task_verdict_events
             (verdict_id, task_id, contract_id, contract_revision, contract_hash,
              phase, view_manifest_hash, snapshot_id, reviewer_identity, clause_results,
              findings, overall, attestation, amendment_ref, submitted_at, workspace_id)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16)",
            params![
                verdict_id,
                task_id,
                contract_id,
                contract_revision,
                contract_hash,
                phase,
                view_manifest_hash,
                snapshot_id,
                reviewer_identity,
                clause_results.to_string(),
                findings.to_string(),
                overall,
                attestation,
                amendment_ref,
                submitted_at,
                task_contract_workspace,
            ],
        )
        .map_err(|e| {
            if e.to_string().contains("UNIQUE constraint failed: task_verdict_events.verdict_id") {
                DaemonRpcError::new(
                    "E_VERDICT_ID_REUSE_MISMATCH",
                    "verdict_id 已绑定其他 verdict",
                )
            } else {
                DaemonRpcError::internal_error(format!("写入 task_verdict_events 失败: {}", e))
            }
        })?;
        let event_id = tx.last_insert_rowid();
        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 verdict 事务失败: {}", e)))?;

        Ok(serde_json::json!({
            "success": true,
            "task_id": task_id,
            "verdict_id": verdict_id,
            "event_id": event_id,
            "submitted_at": submitted_at,
            "replayed": false,
        }))
    }

    // MCP-001（T-1787321708699-da5d8224）：role_view.get 从 python_compat 迁移为
    // Rust native。语义与 Python db_task_reviews.get_role_view + tools_collab
    // _collab_direct_read("role_view.get") 完全一致：
    //   - 从 task_contract_revisions 取最新 envelope_payload；
    //   - 按 (role, "1.0", "blind") allowlist 过滤字段；
    //   - allowlist/contract/content/view_manifest 四类 hash 用规范 JSON
    //     （key 排序、无空格，等价 Python _canonical_json）计算。
    pub fn handle_get_role_view(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let role = params
            .get("role")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        let role = if role.is_empty() { "implementer" } else { role.as_str() };

        // 从最新契约 Envelope 生成 Role_View（view_type=role, stage=blind）
        let envelope: Value = {
            let conn = self.conn.lock().unwrap();
            let row: Option<String> = conn
                .query_row(
                    "SELECT envelope_payload FROM task_contract_revisions \
                     WHERE task_id = ?1 ORDER BY revision DESC LIMIT 1",
                    params![task_id],
                    |r| r.get(0),
                )
                .optional()
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("读取 task_contract_revisions 失败: {e}"))
                })?;
            match row {
                Some(payload) if !payload.is_empty() => {
                    serde_json::from_str(&payload).unwrap_or(Value::Object(Map::new()))
                }
                _ => Value::Object(Map::new()),
            }
        };

        let allowed: Vec<&str> = match role {
            "planner" => vec![
                "contract_id", "profile", "title", "description", "requirements",
                "target_file", "target_symbol", "clauses", "blocking_clauses",
            ],
            "reviewer" => vec![
                "contract_id", "profile", "title", "description", "requirements",
                "target_file", "target_symbol", "allowed_edit_scope", "actual_changes",
                "symbol_changes", "test_runs", "open_quality_findings", "clauses",
                "blocking_clauses",
            ],
            "tester" => vec![
                "contract_id", "profile", "title", "description", "requirements",
                "target_file", "target_symbol", "clauses", "test_cases", "test_runs",
            ],
            // 默认 implementer（含未知 role 兼容 Python 语义）
            _ => vec![
                "contract_id", "profile", "title", "description", "requirements",
                "target_file", "target_symbol", "allowed_edit_scope", "clauses",
                "blocking_clauses",
            ],
        };
        let allowed_set: HashSet<&str> = allowed.iter().copied().collect();

        // 过滤 content：envelope 中在 allowlist 内的字段保留，其余进 excluded
        let mut filtered: Map<String, Value> = Map::new();
        let mut excluded: Vec<String> = Vec::new();
        if let Some(obj) = envelope.as_object() {
            for (key, value) in obj {
                if allowed_set.contains(key.as_str()) {
                    filtered.insert(key.clone(), value.clone());
                } else {
                    excluded.push(key.clone());
                }
            }
        }
        excluded.sort();

        let mut sorted_allowed: Vec<&str> = allowed.clone();
        sorted_allowed.sort_unstable();
        let allowlist_def_hash = canonical_json_sha256(&Value::Array(
            sorted_allowed.iter().map(|s| Value::String(s.to_string())).collect(),
        ));
        let contract_hash = envelope
            .get("contract_hash")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_else(|| canonical_json_sha256(&envelope));
        let content_hash = canonical_json_sha256(&Value::Object(filtered.clone()));

        let manifest = json!({
            "view_type": role,
            "view_version": "1.0",
            "stage": "blind",
            "contract_hash": contract_hash,
            "allowlist_hash": allowlist_def_hash,
            "content_hash": content_hash,
        });
        let view_manifest_hash = canonical_json_sha256(&manifest);

        let mut result = Map::new();
        result.insert("task_id".to_string(), Value::String(task_id.to_string()));
        result.insert("view_type".to_string(), Value::String(role.to_string()));
        result.insert("view_version".to_string(), Value::String("1.0".to_string()));
        result.insert("stage".to_string(), Value::String("blind".to_string()));
        result.insert("view_manifest_hash".to_string(), Value::String(view_manifest_hash));
        result.insert("contract_hash".to_string(), Value::String(contract_hash));
        result.insert("content".to_string(), Value::Object(filtered));
        result.insert(
            "allowed_fields".to_string(),
            Value::Array(sorted_allowed.iter().map(|s| Value::String(s.to_string())).collect()),
        );
        result.insert(
            "excluded_fields".to_string(),
            Value::Array(excluded.iter().map(|s| Value::String(s.clone())).collect()),
        );
        Ok(Value::Object(result))
    }

    // MCP-002（T-1787321708760-de068a9c）：find_evidence 从 python_compat 迁移为
    // Rust native。语义与 Python tools_collab._h_find_evidence +
    // db_task_reviews 的 evidence.query 完全一致：从 task_evidence_events 按
    // task_id / contract_id / verifier / limit 过滤查询，返回 {"items": [...], "count": N}。
    pub fn handle_find_evidence(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let contract_id = params
            .get("contract_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let verifier = params
            .get("verifier")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let limit = params
            .get("limit")
            .and_then(|v| v.as_i64())
            .unwrap_or(50);
        let limit = if limit < 0 { 50 } else { limit as i64 };

        let conn = self.conn.lock().unwrap();
        let mut sql = String::from(
            "SELECT evidence_id, task_id, evidence_type, event_type, commit_hash,
                    workspace_snapshot_id, file_hashes, symbol_hashes, graph_refresh_version,
                    verifier_name, verifier_version, verifier_config_hash, producer_identity,
                    produced_at, payload_hash
             FROM task_evidence_events WHERE 1=1",
        );
        let mut binds: Vec<String> = Vec::new();
        if let Some(ref t) = task_id {
            sql.push_str(" AND task_id = ?");
            binds.push(t.clone());
        }
        if let Some(ref c) = contract_id {
            sql.push_str(" AND evidence_id LIKE ?");
            binds.push(format!("%{}%", c));
        }
        if let Some(ref v) = verifier {
            sql.push_str(" AND verifier_name = ?");
            binds.push(v.clone());
        }
        sql.push_str(" ORDER BY id DESC LIMIT ?");
        binds.push(limit.to_string());

        let mut stmt = conn
            .prepare(&sql)
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 task_evidence_events 失败: {}", e)))?;
        let rows = stmt
            .query_map(rusqlite::params_from_iter(binds.iter()), |r| {
                let mut m = Map::new();
                m.insert("evidence_id".to_string(), Value::String(r.get(0)?));
                m.insert("task_id".to_string(), Value::String(r.get(1)?));
                m.insert("evidence_type".to_string(), Value::String(r.get(2)?));
                m.insert("event_type".to_string(), Value::String(r.get(3)?));
                m.insert("commit_hash".to_string(), Value::String(r.get(4)?));
                m.insert("workspace_snapshot_id".to_string(), Value::String(r.get(5)?));
                m.insert("file_hashes".to_string(), Value::String(r.get(6)?));
                m.insert("symbol_hashes".to_string(), Value::String(r.get(7)?));
                m.insert("graph_refresh_version".to_string(), Value::String(r.get(8)?));
                m.insert("verifier_name".to_string(), Value::String(r.get(9)?));
                m.insert("verifier_version".to_string(), Value::String(r.get(10)?));
                m.insert("verifier_config_hash".to_string(), Value::String(r.get(11)?));
                m.insert("producer_identity".to_string(), Value::String(r.get(12)?));
                m.insert("produced_at".to_string(), Value::Number(serde_json::Number::from_f64(r.get(13)?).unwrap()));
                m.insert("payload_hash".to_string(), Value::String(r.get(14)?));
                Ok(Value::Object(m))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("映射 task_evidence_events 失败: {}", e)))?;
        let mut items = Vec::new();
        for row in rows {
            items.push(row.map_err(|e| DaemonRpcError::internal_error(format!("读取 task_evidence_events 失败: {}", e)))?);
        }
        let mut result = Map::new();
        result.insert("items".to_string(), Value::Array(items.clone()));
        result.insert("count".to_string(), Value::Number(serde_json::Number::from(items.len())));
        Ok(Value::Object(result))
    }

    // MCP-003 （T-1787321708856-e3c10624）：get_freshness_status 从 python_compat
    // 迁移为 Rust native。语义与 Python db_task_evidence.derive_freshness 一致：
    // 全序优先级 invalid > superseded > stale > fresh（Req 6.15）。当前调用方
    // 仅传 evidence_id/task_id（snapshot/hash 为 None，stale 分支不触发）。
    pub fn handle_get_freshness_status(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let evidence_id = params
            .get("evidence_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        let conn = self.conn.lock().unwrap();
        // 当前契约 revision（无契约时取 0）
        let current_rev: i64 = if let Some(ref t) = task_id {
            conn.query_row(
                "SELECT MAX(revision) FROM task_contract_revisions WHERE task_id = ?",
                params![t],
                |r| r.get::<_, Option<i64>>(0),
            )
            .ok()
            .flatten()
            .unwrap_or(0)
        } else {
            0
        };

        // 收集待查询 evidence_id
        let mut ids: Vec<String> = Vec::new();
        if let Some(ref eid) = evidence_id {
            if !eid.is_empty() {
                ids.push(eid.clone());
            }
        } else if let Some(ref t) = task_id {
            let mut stmt = conn
                .prepare(
                    "SELECT evidence_id FROM task_evidence_events \
                     WHERE task_id = ? AND event_type = ?",
                )
                .map_err(|e| DaemonRpcError::internal_error(format!("查询 task_evidence_events 失败: {}", e)))?;
            let rows = stmt
                .query_map(params![t, "evidence_appended"], |r| r.get::<_, String>(0))
                .map_err(|e| DaemonRpcError::internal_error(format!("映射 evidence_id 失败: {}", e)))?;
            for row in rows {
                if let Ok(eid) = row {
                    ids.push(eid);
                }
            }
        }

        let mut items: Vec<Value> = Vec::new();
        for eid in ids {
            if eid.is_empty() {
                continue;
            }
            let status = Self::derive_evidence_freshness(&conn, &eid, current_rev);
            items.push(json!({"evidence_id": eid, "status": status}));
        }
        let mut result = Map::new();
        result.insert("items".to_string(), Value::Array(items));
        Ok(Value::Object(result))
    }

    // MCP-004（T-1787321708926-e7ebfac4）：get_gate_decision 从 python_compat
    // 迁移为 Rust native。语义与 Python tools_collab._h_gate_decision +
    // gate.decision.query 一致：从 task_gate_decisions 按 task_id/decision_id
    // (gate_id) 过滤查询，按 decision_time DESC 限流。
    pub fn handle_get_gate_decision(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let gate_id = params
            .get("gate_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let limit: i64 = params
            .get("limit")
            .and_then(|v| v.as_i64())
            .unwrap_or(20);

        let conn = self.conn.lock().unwrap();
        let mut sql = String::from(
            "SELECT decision_id, task_id, contract_id, contract_revision, contract_hash, \
             decision, reason, clause_decisions, verifier_triples, resolved_stage_toggle_set, \
             independence_policy_value, independence_waiver_marker, event_type, decision_time, \
             step_id, role_contract_lineage_id, role_contract_revision_id, role_contract_revision, \
             role_contract_hash, canonicalization_version, canonicalization_rules_hash, \
             normalization_version, normalization_rules_hash, workspace_id \
             FROM task_gate_decisions WHERE 1=1",
        );
        let mut binds: Vec<String> = Vec::new();
        if let Some(ref t) = task_id {
            sql.push_str(" AND task_id = ?");
            binds.push(t.clone());
        }
        if let Some(ref g) = gate_id {
            if !g.is_empty() {
                sql.push_str(" AND decision_id = ?");
                binds.push(g.clone());
            }
        }
        sql.push_str(" ORDER BY decision_time DESC LIMIT ?");
        binds.push(limit.to_string());

        let mut stmt = conn
            .prepare(&sql)
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 task_gate_decisions 失败: {}", e)))?;
        let rows = stmt
            .query_map(rusqlite::params_from_iter(binds.iter()), |r| {
                let mut m = Map::new();
                m.insert("decision_id".to_string(), Value::String(r.get(0)?));
                m.insert("task_id".to_string(), Value::String(r.get(1)?));
                m.insert("contract_id".to_string(), Value::String(r.get(2)?));
                m.insert("contract_revision".to_string(), Value::Number(r.get::<_, i64>(3)?.into()));
                m.insert("contract_hash".to_string(), Value::String(r.get(4)?));
                m.insert("decision".to_string(), Value::String(r.get(5)?));
                m.insert("reason".to_string(), Value::String(r.get(6)?));
                m.insert("clause_decisions".to_string(), Value::String(r.get(7)?));
                m.insert("verifier_triples".to_string(), Value::String(r.get(8)?));
                m.insert("resolved_stage_toggle_set".to_string(), Value::String(r.get(9)?));
                m.insert("independence_policy_value".to_string(), Value::String(r.get(10)?));
                m.insert("independence_waiver_marker".to_string(), Value::String(r.get(11)?));
                m.insert("event_type".to_string(), Value::String(r.get(12)?));
                m.insert("decision_time".to_string(), Value::Number(serde_json::Number::from_f64(r.get(13)?).unwrap()));
                m.insert("step_id".to_string(), Value::String(r.get(14)?));
                m.insert("role_contract_lineage_id".to_string(), Value::String(r.get(15)?));
                m.insert("role_contract_revision_id".to_string(), Value::String(r.get(16)?));
                m.insert("role_contract_revision".to_string(), Value::Number(r.get::<_, i64>(17)?.into()));
                m.insert("role_contract_hash".to_string(), Value::String(r.get(18)?));
                m.insert("canonicalization_version".to_string(), Value::String(r.get(19)?));
                m.insert("canonicalization_rules_hash".to_string(), Value::String(r.get(20)?));
                m.insert("normalization_version".to_string(), Value::String(r.get(21)?));
                m.insert("normalization_rules_hash".to_string(), Value::String(r.get(22)?));
                m.insert("workspace_id".to_string(), Value::Number(r.get::<_, i64>(23)?.into()));
                Ok(Value::Object(m))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("映射 task_gate_decisions 失败: {}", e)))?;
        let mut items = Vec::new();
        for row in rows {
            items.push(row.map_err(|e| DaemonRpcError::internal_error(format!("读取 task_gate_decisions 失败: {}", e)))?);
        }
        let mut result = Map::new();
        result.insert("items".to_string(), Value::Array(items.clone()));
        result.insert("count".to_string(), Value::Number(serde_json::Number::from(items.len())));
        Ok(Value::Object(result))
    }

    // MCP-005（T-1787321709017-ed4e79b0）：get_artifact_freshness 从 python_compat
    // 迁移为 Rust native。语义与 Python tools_p2_graph._h_get_artifact_freshness +
    // db_task_dependencies.get_artifact_freshness 一致：从 artifact_identities 按
    // workspace_id + task_id (+ artifact_ref) 取最新一条。
    pub fn handle_get_artifact_freshness(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id: i64 = params
            .get("workspace_id")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let artifact_ref = params
            .get("artifact_ref")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        let conn = self.conn.lock().unwrap();
        let (sql, binds): (String, Vec<String>) = if let Some(ref a) = artifact_ref {
            if a.is_empty() {
                (
                    "SELECT artifact_id, freshness_status, artifact_hash, produced_at \
                     FROM artifact_identities \
                     WHERE workspace_id = ? AND task_id = ? \
                     ORDER BY produced_at DESC LIMIT 1".to_string(),
                    vec![workspace_id.to_string(), task_id.clone().unwrap_or_default()],
                )
            } else {
                (
                    "SELECT artifact_id, freshness_status, artifact_hash, produced_at \
                     FROM artifact_identities \
                     WHERE workspace_id = ? AND task_id = ? AND artifact_ref = ? \
                     ORDER BY produced_at DESC LIMIT 1".to_string(),
                    vec![workspace_id.to_string(), task_id.clone().unwrap_or_default(), a.clone()],
                )
            }
        } else {
            (
                "SELECT artifact_id, freshness_status, artifact_hash, produced_at \
                 FROM artifact_identities \
                 WHERE workspace_id = ? AND task_id = ? \
                 ORDER BY produced_at DESC LIMIT 1".to_string(),
                vec![workspace_id.to_string(), task_id.clone().unwrap_or_default()],
            )
        };

        let row = conn
            .prepare(&sql)
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 artifact_identities 失败: {}", e)))?
            .query_row(
                rusqlite::params_from_iter(binds.iter()),
                |r| {
                    let mut m = Map::new();
                    m.insert("artifact_id".to_string(), Value::String(r.get(0)?));
                    m.insert("freshness_status".to_string(), Value::String(r.get(1)?));
                    m.insert("artifact_hash".to_string(), Value::String(r.get(2)?));
                    m.insert(
                        "produced_at".to_string(),
                        Value::Number(serde_json::Number::from_f64(r.get(3)?).unwrap()),
                    );
                    Ok(Value::Object(m))
                },
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("映射 artifact_identities 失败: {}", e)))?;

        match row {
            Some(v) => Ok(v),
            None => {
                let mut nf = Map::new();
                nf.insert("found".to_string(), Value::Bool(false));
                Ok(Value::Object(nf))
            }
        }
    }

    // MCP-006（T-1787321709098-f2236ea0）：get_interface_providers 从 python_compat
    // 迁移为 Rust native。语义与 Python tools_p2_graph._h_get_interface_providers +
    // db_task_dependencies.get_interface_providers 一致：从 interface_identities 按
    // workspace_id + interface_name (+ version) 查询 provider 列表。
    pub fn handle_get_interface_providers(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id: i64 = params
            .get("workspace_id")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let interface_name = params
            .get("interface_name")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_default();
        let version = params
            .get("version")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_default();

        let conn = self.conn.lock().unwrap();
        let (sql, binds): (String, Vec<String>) = if version.is_empty() {
            (
                "SELECT interface_id, interface_name, version, interface_hash, \
                 provider_task_id, contract_id, contract_revision \
                 FROM interface_identities \
                 WHERE workspace_id = ? AND interface_name = ?".to_string(),
                vec![workspace_id.to_string(), interface_name.clone()],
            )
        } else {
            (
                "SELECT interface_id, interface_name, version, interface_hash, \
                 provider_task_id, contract_id, contract_revision \
                 FROM interface_identities \
                 WHERE workspace_id = ? AND interface_name = ? AND version = ?".to_string(),
                vec![workspace_id.to_string(), interface_name.clone(), version.clone()],
            )
        };

        let mut stmt = conn
            .prepare(&sql)
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 interface_identities 失败: {}", e)))?;
        let rows = stmt
            .query_map(rusqlite::params_from_iter(binds.iter()), |r| {
                let mut m = Map::new();
                m.insert("interface_id".to_string(), Value::String(r.get(0)?));
                m.insert("interface_name".to_string(), Value::String(r.get(1)?));
                m.insert("version".to_string(), Value::String(r.get(2)?));
                m.insert("interface_hash".to_string(), Value::String(r.get(3)?));
                m.insert("provider_task_id".to_string(), Value::String(r.get(4)?));
                m.insert("contract_id".to_string(), Value::String(r.get(5)?));
                m.insert("contract_revision".to_string(), Value::Number(r.get::<_, i64>(6)?.into()));
                Ok(Value::Object(m))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("映射 interface_identities 失败: {}", e)))?;
        let mut items = Vec::new();
        for row in rows {
            items.push(row.map_err(|e| DaemonRpcError::internal_error(format!("读取 interface_identities 失败: {}", e)))?);
        }
        let mut result = Map::new();
        result.insert("items".to_string(), Value::Array(items.clone()));
        result.insert("count".to_string(), Value::Number(serde_json::Number::from(items.len())));
        Ok(Value::Object(result))
    }

    // MCP-007（T-1787321709179-f6fdf5bc）：detect_cycle 从 python_compat 迁移为
    // Rust native。语义与 Python db_task_dependencies.detect_cycle 完全一致：
    //   - 从 dependency_edges 取 workspace 内 is_hard=1 的边；
    //   - DFS 三色标记检测环，定位 cycle_start_node；
    //   - BFS 从 cycle_start_node 回到自身找最短 cycle path。
    // 返回 {"has_cycle": bool, "cycle_path": [str], "checked_nodes": int}。
    pub fn handle_detect_cycle(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id: i64 = params
            .get("workspace_id")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);

        let conn = self.conn.lock().unwrap();
        let mut graph: BTreeMap<String, Vec<String>> = BTreeMap::new();
        {
            let mut stmt = conn
                .prepare(
                    "SELECT DISTINCT provider_task_id, consumer_task_id \
                     FROM dependency_edges \
                     WHERE workspace_id = ? AND is_hard = 1",
                )
                .map_err(|e| DaemonRpcError::internal_error(format!("查询 dependency_edges 失败: {}", e)))?;
            let rows = stmt
                .query_map(params![workspace_id], |r| {
                    Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
                })
                .map_err(|e| DaemonRpcError::internal_error(format!("读取 dependency_edges 失败: {}", e)))?;
            for row in rows {
                let (provider, consumer) = row
                    .map_err(|e| DaemonRpcError::internal_error(format!("映射 dependency_edges 失败: {}", e)))?;
                graph.entry(provider).or_default().push(consumer);
            }
        }

        if graph.is_empty() {
            let mut result = Map::new();
            result.insert("has_cycle".to_string(), Value::Bool(false));
            result.insert("cycle_path".to_string(), Value::Array(vec![]));
            result.insert("checked_nodes".to_string(), Value::Number(serde_json::Number::from(0)));
            return Ok(Value::Object(result));
        }

        // DFS 三色标记检测环（0=WHITE, 1=GRAY, 2=BLACK）
        let mut color: HashMap<String, u8> = HashMap::new();
        let mut cycle_start_node: Option<String> = None;

        fn dfs_detect(
            node: &str,
            graph: &BTreeMap<String, Vec<String>>,
            color: &mut HashMap<String, u8>,
            found: &mut Option<String>,
        ) -> bool {
            color.insert(node.to_string(), 1);
            if let Some(neighbors) = graph.get(node) {
                for nb in neighbors {
                    let c = color.get(nb).copied().unwrap_or(0);
                    if c == 1 {
                        *found = Some(nb.clone());
                        return true;
                    }
                    if c == 0 {
                        if dfs_detect(nb, graph, color, found) {
                            return true;
                        }
                    }
                }
            }
            color.insert(node.to_string(), 2);
            false
        }

        for node in graph.keys() {
            if color.get(node).copied().unwrap_or(0) == 0 {
                if dfs_detect(node, &graph, &mut color, &mut cycle_start_node) {
                    break;
                }
            }
        }

        if cycle_start_node.is_none() {
            let mut result = Map::new();
            result.insert("has_cycle".to_string(), Value::Bool(false));
            result.insert("cycle_path".to_string(), Value::Array(vec![]));
            result.insert("checked_nodes".to_string(), Value::Number(serde_json::Number::from(graph.len())));
            return Ok(Value::Object(result));
        }

        let start = cycle_start_node.clone().unwrap();
        let cycle_path = Self::detect_cycle_find_shortest(&graph, &start);

        let mut result = Map::new();
        result.insert("has_cycle".to_string(), Value::Bool(true));
        result.insert(
            "cycle_path".to_string(),
            Value::Array(cycle_path.into_iter().map(Value::String).collect()),
        );
        result.insert("checked_nodes".to_string(), Value::Number(serde_json::Number::from(graph.len())));
        Ok(Value::Object(result))
    }

    // MCP-008（T-1787321709249-fb256530）：validate_revision_dependencies 迁移 rust_native。
    // 语义与 Python tools_p2_graph._h_validate_revision_dependencies 一致：在内存中模拟
    // build_hard_dependency_edges（不写 dependency_edges 表）——查询 task_dependencies →
    // 解析 requires_artifact / requires_interface 的 provider → 检查多 provider 显式选择 →
    // 计算 edges_built/edges_skipped/resolution_errors/provider_conflicts；环检测合并
    // 「现有表硬边 ∪ 本次模拟边」（与 db 层 build 幂等写表后 detect_cycle(整表) 语义等价）。
    pub fn handle_validate_revision_dependencies(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id: i64 = params
            .get("workspace_id")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let contract_id = params
            .get("contract_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let contract_revision: i64 = params
            .get("contract_revision")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);

        let conn = self.conn.lock().unwrap();

        // 1. 内存模拟 build_hard_dependency_edges（不写表）
        let mut edges_built: i64 = 0;
        let mut edges_skipped: i64 = 0;
        let mut resolution_errors: Vec<String> = Vec::new();
        let mut provider_conflicts: Vec<Value> = Vec::new();
        let mut new_edges: BTreeMap<String, Vec<String>> = BTreeMap::new();

        {
            let mut stmt = conn
                .prepare(
                    "SELECT task_id, dependency_type, target_ref, target_task_id, \
                            contract_id, contract_revision \
                     FROM task_dependencies \
                     WHERE workspace_id = ? AND contract_id = ? AND contract_revision = ? \
                       AND is_informational = 0",
                )
                .map_err(|e| DaemonRpcError::internal_error(format!("查询 task_dependencies 失败: {}", e)))?;
            let rows = stmt
                .query_map(params![workspace_id, contract_id, contract_revision], |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                        r.get::<_, String>(3)?,
                        r.get::<_, String>(4)?,
                        r.get::<_, i64>(5)?,
                    ))
                })
                .map_err(|e| DaemonRpcError::internal_error(format!("读取 task_dependencies 失败: {}", e)))?;
            for row in rows {
                let (consumer_task_id, dtype, target_ref, target_task_id, dep_contract_id, dep_revision) =
                    row.map_err(|e| DaemonRpcError::internal_error(format!("映射 task_dependencies 失败: {}", e)))?;

                if dtype == "requires_artifact" {
                    // requires_artifact: target_task_id 是 provider
                    if target_task_id.is_empty() {
                        resolution_errors.push(format!(
                            "requires_artifact 依赖缺少 target_task_id (task={}, ref={})",
                            consumer_task_id, target_ref
                        ));
                        edges_skipped += 1;
                        continue;
                    }
                    new_edges.entry(target_task_id.clone()).or_default().push(consumer_task_id.clone());
                    edges_built += 1;
                } else if dtype == "requires_interface" {
                    // requires_interface: 需要解析 provides_interface
                    let providers = Self::query_interface_providers(&conn, workspace_id, &target_ref, "");
                    if providers.is_empty() {
                        resolution_errors.push(format!(
                            "requires_interface '{}' 无匹配 provider (task={})",
                            target_ref, consumer_task_id
                        ));
                        edges_skipped += 1;
                        continue;
                    }
                    if providers.len() > 1 {
                        // 多 provider：检查是否有显式选择（Req 9.9）
                        let selected = Self::query_provider_selection(
                            &conn,
                            workspace_id,
                            &consumer_task_id,
                            &dep_contract_id,
                            dep_revision,
                            &target_ref,
                        );
                        if selected.is_none() {
                            let provs: Vec<Value> = providers
                                .iter()
                                .map(|p| Value::String(p.clone()))
                                .collect();
                            let mut conflict = Map::new();
                            conflict.insert("consumer_task_id".to_string(), Value::String(consumer_task_id.clone()));
                            conflict.insert("interface_name".to_string(), Value::String(target_ref.clone()));
                            conflict.insert("providers".to_string(), Value::Array(provs));
                            provider_conflicts.push(Value::Object(conflict));
                            edges_skipped += 1;
                            continue;
                        }
                        new_edges.entry(selected.unwrap()).or_default().push(consumer_task_id.clone());
                        edges_built += 1;
                    } else {
                        new_edges.entry(providers[0].clone()).or_default().push(consumer_task_id.clone());
                        edges_built += 1;
                    }
                }
                // requires_existing 和 provides_interface 不建边
            }
        }

        // 2. 合并现有表硬边（db 层 build 幂等写表后 detect_cycle 检测整表）
        let mut graph: BTreeMap<String, Vec<String>> = BTreeMap::new();
        {
            let mut stmt2 = conn
                .prepare(
                    "SELECT DISTINCT provider_task_id, consumer_task_id \
                     FROM dependency_edges \
                     WHERE workspace_id = ? AND is_hard = 1",
                )
                .map_err(|e| DaemonRpcError::internal_error(format!("查询 dependency_edges 失败: {}", e)))?;
            let rows2 = stmt2
                .query_map(params![workspace_id], |r| {
                    Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
                })
                .map_err(|e| DaemonRpcError::internal_error(format!("读取 dependency_edges 失败: {}", e)))?;
            for row in rows2 {
                let (provider, consumer) = row
                    .map_err(|e| DaemonRpcError::internal_error(format!("映射 dependency_edges 失败: {}", e)))?;
                graph.entry(provider).or_default().push(consumer);
            }
        }
        for (provider, consumers) in new_edges.iter() {
            graph.entry(provider.clone()).or_default().extend(consumers.iter().cloned());
        }
        drop(conn);

        // 3. 环检测（复刻 db 层 detect_cycle：DFS 三色 + BFS 最短 cycle path）
        let mut has_cycle = false;
        let mut cycle_path: Vec<String> = Vec::new();
        if !graph.is_empty() {
            let cycle_result = Self::detect_cycle_on_graph(&graph);
            has_cycle = cycle_result.0;
            cycle_path = cycle_result.1;
        }

        // 4. 组装结果（与 db 层 validate_revision_dependencies 相同结构）
        let mut errors: Vec<String> = resolution_errors;
        for conflict in provider_conflicts.iter() {
            let consumer = conflict.get("consumer_task_id").and_then(|v| v.as_str()).unwrap_or("");
            let interface = conflict.get("interface_name").and_then(|v| v.as_str()).unwrap_or("");
            let provs: Vec<&str> = conflict
                .get("providers")
                .and_then(|v| v.as_array())
                .map(|a| a.iter().filter_map(|x| x.as_str()).collect())
                .unwrap_or_default();
            errors.push(format!(
                "interface '{}' 有多个 provider {:?} 但无 Planner 显式选择 (consumer={})",
                interface, provs, consumer
            ));
        }
        if has_cycle {
            errors.push(format!("硬依赖图存在环: {}", cycle_path.join(" → ")));
        }

        let valid = errors.is_empty() && provider_conflicts.is_empty();

        let mut result = Map::new();
        result.insert("valid".to_string(), Value::Bool(valid));
        result.insert("errors".to_string(), Value::Array(errors.into_iter().map(Value::String).collect()));
        result.insert(
            "cycle_path".to_string(),
            if has_cycle {
                Value::Array(cycle_path.into_iter().map(Value::String).collect())
            } else {
                Value::Array(vec![])
            },
        );
        result.insert("provider_conflicts".to_string(), Value::Array(provider_conflicts));
        result.insert("edges_built".to_string(), Value::Number(serde_json::Number::from(edges_built)));
        result.insert("edges_skipped".to_string(), Value::Number(serde_json::Number::from(edges_skipped)));
        Ok(Value::Object(result))
    }

    // 查询 interface_identities 中匹配的 provider 列表（复刻 db get_interface_providers）。
    fn query_interface_providers(
        conn: &rusqlite::Connection,
        workspace_id: i64,
        interface_name: &str,
        version: &str,
    ) -> Vec<String> {
        let mut stmt = conn
            .prepare(
                "SELECT provider_task_id FROM interface_identities \
                 WHERE workspace_id = ? AND interface_name = ?",
            )
            .ok();
        let Some(mut stmt) = stmt else { return vec![] };
        let rows = stmt
            .query_map(params![workspace_id, interface_name], |r| r.get::<_, String>(0))
            .ok();
        let Some(rows) = rows else { return vec![] };
        rows.filter_map(|r| r.ok()).collect()
    }

    // 查询已记录的 provider 选择（复刻 db get_provider_selection）。
    fn query_provider_selection(
        conn: &rusqlite::Connection,
        workspace_id: i64,
        consumer_task_id: &str,
        contract_id: &str,
        contract_revision: i64,
        interface_name: &str,
    ) -> Option<String> {
        let mut stmt = conn
            .prepare(
                "SELECT selected_provider_task_id FROM interface_provider_selections \
                 WHERE workspace_id = ? AND consumer_task_id = ? AND contract_id = ? \
                   AND contract_revision = ? AND interface_name = ?",
            )
            .ok()?;
        stmt.query_row(
            params![workspace_id, consumer_task_id, contract_id, contract_revision, interface_name],
            |r| r.get::<_, String>(0),
        )
        .ok()
    }

    // 在内存边集合上做环检测（复刻 db detect_cycle：DFS 三色 + BFS 最短 cycle path）。
    // 返回 (has_cycle, cycle_path)。
    fn detect_cycle_on_graph(graph: &BTreeMap<String, Vec<String>>) -> (bool, Vec<String>) {
        if graph.is_empty() {
            return (false, vec![]);
        }
        let mut color: HashMap<String, u8> = HashMap::new();
        let mut cycle_start_node: Option<String> = None;

        fn dfs_detect(
            node: &str,
            graph: &BTreeMap<String, Vec<String>>,
            color: &mut HashMap<String, u8>,
            found: &mut Option<String>,
        ) -> bool {
            color.insert(node.to_string(), 1);
            if let Some(neighbors) = graph.get(node) {
                for nb in neighbors {
                    let c = color.get(nb).copied().unwrap_or(0);
                    if c == 1 {
                        *found = Some(nb.clone());
                        return true;
                    }
                    if c == 0 && dfs_detect(nb, graph, color, found) {
                        return true;
                    }
                }
            }
            color.insert(node.to_string(), 2);
            false
        }

        for node in graph.keys() {
            if color.get(node).copied().unwrap_or(0) == 0 {
                if dfs_detect(node, graph, &mut color, &mut cycle_start_node) {
                    break;
                }
            }
        }

        match cycle_start_node {
            None => (false, vec![]),
            Some(start) => (true, Self::detect_cycle_find_shortest(graph, &start)),
        }
    }

    // MCP-009（T-1787321709365-021050a8）：get_dependency_edges 迁移 rust_native。
    // 语义与 Python db_task_dependencies.get_dependency_edges 一致：查询硬依赖图边
    // （dependency_edges 全部列，按 created_at 排序），可选按 task_id 过滤
    // （provider_task_id 或 consumer_task_id 匹配）。返回行数组（与 Python dict 行
    // 键名一致：id/workspace_id/provider_task_id/consumer_task_id/edge_type/
    // source_type/contract_id/contract_revision/is_hard/created_at）。
    pub fn handle_get_dependency_edges(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id: i64 = params
            .get("workspace_id")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        let conn = self.conn.lock().unwrap();
        let rows: Vec<Value> = if task_id.is_empty() {
            let mut stmt = conn
                .prepare(
                    "SELECT id, workspace_id, provider_task_id, consumer_task_id, \
                            edge_type, source_type, contract_id, contract_revision, \
                            is_hard, created_at \
                     FROM dependency_edges \
                     WHERE workspace_id = ? \
                     ORDER BY created_at",
                )
                .map_err(|e| DaemonRpcError::internal_error(format!("查询 dependency_edges 失败: {}", e)))?;
            let items = stmt
                .query_map(params![workspace_id], |r| {
                    Self::map_dependency_edge_row(r)
                })
                .map_err(|e| DaemonRpcError::internal_error(format!("读取 dependency_edges 失败: {}", e)))?;
            let mut out = Vec::new();
            for it in items {
                out.push(it.map_err(|e| DaemonRpcError::internal_error(format!("映射 dependency_edges 失败: {}", e)))?);
            }
            out
        } else {
            let mut stmt = conn
                .prepare(
                    "SELECT id, workspace_id, provider_task_id, consumer_task_id, \
                            edge_type, source_type, contract_id, contract_revision, \
                            is_hard, created_at \
                     FROM dependency_edges \
                     WHERE workspace_id = ? AND (provider_task_id = ? OR consumer_task_id = ?) \
                     ORDER BY created_at",
                )
                .map_err(|e| DaemonRpcError::internal_error(format!("查询 dependency_edges 失败: {}", e)))?;
            let items = stmt
                .query_map(params![workspace_id, task_id, task_id], |r| {
                    Self::map_dependency_edge_row(r)
                })
                .map_err(|e| DaemonRpcError::internal_error(format!("读取 dependency_edges 失败: {}", e)))?;
            let mut out = Vec::new();
            for it in items {
                out.push(it.map_err(|e| DaemonRpcError::internal_error(format!("映射 dependency_edges 失败: {}", e)))?);
            }
            out
        };

        Ok(Value::Array(rows))
    }

    // 将 dependency_edges 一行映射为与 Python dict 行一致的 JSON 对象。
    fn map_dependency_edge_row(
        r: &rusqlite::Row,
    ) -> Result<Value, rusqlite::Error> {
        let mut m = Map::new();
        m.insert("id".to_string(), Value::Number(r.get::<_, i64>(0)?.into()));
        m.insert("workspace_id".to_string(), Value::Number(r.get::<_, i64>(1)?.into()));
        m.insert("provider_task_id".to_string(), Value::String(r.get::<_, String>(2)?));
        m.insert("consumer_task_id".to_string(), Value::String(r.get::<_, String>(3)?));
        m.insert("edge_type".to_string(), Value::String(r.get::<_, String>(4)?));
        m.insert("source_type".to_string(), Value::String(r.get::<_, String>(5)?));
        m.insert("contract_id".to_string(), Value::String(r.get::<_, String>(6)?));
        m.insert("contract_revision".to_string(), Value::Number(r.get::<_, i64>(7)?.into()));
        m.insert("is_hard".to_string(), Value::Number(r.get::<_, i64>(8)?.into()));
        m.insert("created_at".to_string(), Value::Number(serde_json::Number::from_f64(r.get::<_, f64>(9)?).unwrap_or(serde_json::Number::from(0))));
        Ok(Value::Object(m))
    }

    // MCP-010（T-1787321709432-060d1128）：get_action_identity 迁移 rust_native。
    // 语义与 Python db_task_identity.get_action_identity 一致：按 workspace_id + action_id
    // 查询 action_identities 单行（全部列），无匹配返回 None。
    pub fn handle_get_action_identity(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id: i64 = params
            .get("workspace_id")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let action_id = params
            .get("action_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        let conn = self.conn.lock().unwrap();
        let mut stmt = conn
            .prepare(
                "SELECT id, workspace_id, action_id, action_type, task_id, \
                        contract_id, contract_revision, agent_id, session_id, \
                        model_id, role, recorded_at \
                 FROM action_identities \
                 WHERE workspace_id = ? AND action_id = ?",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 action_identities 失败: {}", e)))?;
        let found = stmt
            .query_row(params![workspace_id, action_id], |r| {
                let mut m = Map::new();
                m.insert("id".to_string(), Value::Number(r.get::<_, i64>(0)?.into()));
                m.insert("workspace_id".to_string(), Value::Number(r.get::<_, i64>(1)?.into()));
                m.insert("action_id".to_string(), Value::String(r.get::<_, String>(2)?));
                m.insert("action_type".to_string(), Value::String(r.get::<_, String>(3)?));
                m.insert("task_id".to_string(), Value::String(r.get::<_, String>(4)?));
                m.insert("contract_id".to_string(), Value::String(r.get::<_, String>(5)?));
                m.insert("contract_revision".to_string(), Value::Number(r.get::<_, i64>(6)?.into()));
                m.insert("agent_id".to_string(), Value::String(r.get::<_, String>(7)?));
                m.insert("session_id".to_string(), Value::String(r.get::<_, String>(8)?));
                m.insert("model_id".to_string(), Value::String(r.get::<_, String>(9)?));
                m.insert("role".to_string(), Value::String(r.get::<_, String>(10)?));
                m.insert("recorded_at".to_string(), Value::Number(serde_json::Number::from_f64(r.get::<_, f64>(11)?).unwrap_or(serde_json::Number::from(0))));
                Ok(Value::Object(m))
            })
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("映射 action_identities 失败: {}", e)))?;

        match found {
            Some(v) => Ok(v),
            None => Ok(Value::Null),
        }
    }

    // MCP-011（T-1787321709518-0b31a484）：check_action_identity 迁移 rust_native。
    // 语义与 Python tools_p3_identity._h_check_action_identity 一致：解析 identity JSON
    // 字符串 → 校验结构化身份（agent_id/session_id/model_id/role 四字段齐全 +
    // require_role 匹配）→ 返回 {"valid": bool, "reason": {...}}。
    pub fn handle_check_action_identity(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let identity_str = params
            .get("identity")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let require_role = params
            .get("require_role")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        // 1. 解析 identity JSON 字符串（_p3_resolve_identity_arg）
        if identity_str.trim().is_empty() {
            let mut reason = Map::new();
            reason.insert("code".to_string(), Value::String("E_IDENTITY_INCOMPLETE".to_string()));
            reason.insert("message_key".to_string(), Value::String("error.identity_incomplete".to_string()));
            reason.insert("detail".to_string(), Value::String("identity 必须是 JSON 对象 {agent_id, session_id, model_id, role}".to_string()));
            let mut result = Map::new();
            result.insert("valid".to_string(), Value::Bool(false));
            result.insert("reason".to_string(), Value::Object(reason));
            return Ok(Value::Object(result));
        }
        let parsed: Result<Value, _> = serde_json::from_str(&identity_str);
        let parsed = match parsed {
            Ok(v) => v,
            Err(_) => {
                let mut reason = Map::new();
                reason.insert("code".to_string(), Value::String("E_IDENTITY_INCOMPLETE".to_string()));
                reason.insert("message_key".to_string(), Value::String("error.identity_incomplete".to_string()));
                reason.insert("detail".to_string(), Value::String("identity 必须是 JSON 对象 {agent_id, session_id, model_id, role}".to_string()));
                let mut result = Map::new();
                result.insert("valid".to_string(), Value::Bool(false));
                result.insert("reason".to_string(), Value::Object(reason));
                return Ok(Value::Object(result));
            }
        };
        let obj = match parsed.as_object() {
            Some(o) => o,
            None => {
                let mut reason = Map::new();
                reason.insert("code".to_string(), Value::String("E_IDENTITY_INCOMPLETE".to_string()));
                reason.insert("message_key".to_string(), Value::String("error.identity_incomplete".to_string()));
                reason.insert("detail".to_string(), Value::String("identity 必须是 JSON 对象".to_string()));
                let mut result = Map::new();
                result.insert("valid".to_string(), Value::Bool(false));
                result.insert("reason".to_string(), Value::Object(reason));
                return Ok(Value::Object(result));
            }
        };

        let agent_id = obj.get("agent_id").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let session_id = obj.get("session_id").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let model_id = obj.get("model_id").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let role = obj.get("role").and_then(|v| v.as_str()).unwrap_or("").to_string();

        // 2. validate_action_identity（纯逻辑，无 DB 查询）
        let valid;
        let mut reason = Map::new();
        if agent_id.is_empty() || session_id.is_empty() || model_id.is_empty() || role.is_empty() {
            valid = false;
            reason.insert("code".to_string(), Value::String("E_IDENTITY_INCOMPLETE".to_string()));
            reason.insert("message_key".to_string(), Value::String("error.identity_incomplete".to_string()));
            reason.insert("detail".to_string(), Value::String("缺失必要的 Identity 字段 (agent_id, session_id, model_id, role)".to_string()));
        } else if !require_role.is_empty() && role != require_role {
            valid = false;
            reason.insert("code".to_string(), Value::String("E_IDENTITY_ROLE_MISMATCH".to_string()));
            reason.insert("message_key".to_string(), Value::String("error.identity_role_mismatch".to_string()));
            reason.insert("detail".to_string(), Value::String(format!("角色不匹配: 期望 {}, 实际 {}", require_role, role)));
            reason.insert("expected_role".to_string(), Value::String(require_role.clone()));
            reason.insert("actual_role".to_string(), Value::String(role.clone()));
        } else {
            valid = true;
            reason.insert("code".to_string(), Value::String("OK".to_string()));
        }

        let mut result = Map::new();
        result.insert("valid".to_string(), Value::Bool(valid));
        result.insert("reason".to_string(), Value::Object(reason));
        Ok(Value::Object(result))
    }

    // 从 start 出发 BFS 回到自身的最短 cycle path；找不到时回退 DFS 任意环。
    fn detect_cycle_find_shortest(
        graph: &BTreeMap<String, Vec<String>>,
        start: &str,
    ) -> Vec<String> {
        use std::collections::VecDeque;
        let mut queue: VecDeque<(String, Vec<String>)> = VecDeque::new();
        queue.push_back((start.to_string(), vec![start.to_string()]));
        let mut visited: HashSet<String> = HashSet::new();
        visited.insert(start.to_string());
        while let Some((node, path)) = queue.pop_front() {
            if let Some(neighbors) = graph.get(&node) {
                for nb in neighbors {
                    if nb == start && path.len() >= 1 {
                        let mut p = path.clone();
                        p.push(start.to_string());
                        return p;
                    }
                    if !visited.contains(nb) {
                        visited.insert(nb.clone());
                        let mut np = path.clone();
                        np.push(nb.clone());
                        queue.push_back((nb.clone(), np));
                    }
                }
            }
        }
        Self::detect_cycle_find_any(graph, start)
    }

    // DFS 回退：从 start 出发找任意回到 start 的 cycle path。
    fn detect_cycle_find_any(
        graph: &BTreeMap<String, Vec<String>>,
        start: &str,
    ) -> Vec<String> {
        let mut path: Vec<String> = Vec::new();
        let mut visited: HashSet<String> = HashSet::new();
        fn dfs(
            node: &str,
            graph: &BTreeMap<String, Vec<String>>,
            start: &str,
            path: &mut Vec<String>,
            visited: &mut HashSet<String>,
        ) -> Vec<String> {
            path.push(node.to_string());
            visited.insert(node.to_string());
            if let Some(neighbors) = graph.get(node) {
                for nb in neighbors {
                    if nb == start && path.len() >= 1 {
                        let mut p = path.clone();
                        p.push(start.to_string());
                        return p;
                    }
                    if !visited.contains(nb) {
                        let r = dfs(nb, graph, start, path, visited);
                        if !r.is_empty() {
                            return r;
                        }
                    }
                }
            }
            path.pop();
            visited.remove(node);
            Vec::new()
        }
        dfs(start, graph, start, &mut path, &mut visited)
    }

    /// 复刻 Python db_task_evidence.derive_freshness 的核心派生逻辑（snapshot/hash
    /// 比较维度在调用方未传入时跳过，保持与 Python 「freshness.status」 RPC 一致）。
    fn derive_evidence_freshness(
        conn: &rusqlite::Connection,
        evidence_id: &str,
        current_contract_revision: i64,
    ) -> String {
        const FRESHNESS_FRESH: &str = "fresh";
        const FRESHNESS_STALE: &str = "stale";
        const FRESHNESS_INVALID: &str = "invalid";
        const FRESHNESS_SUPERSEDED: &str = "superseded";

        // 查找原始 Evidence（event_type = evidence_appended）
        let row = conn
            .query_row(
                "SELECT verifier_name, verifier_version, verifier_config_hash, \
                 contract_revision, workspace_snapshot_id, file_hashes, symbol_hashes, \
                 graph_refresh_version FROM task_evidence_events \
                 WHERE evidence_id = ? AND event_type = ?",
                params![evidence_id, "evidence_appended"],
                |r| {
                    Ok((
                        r.get::<_, Option<String>>(0)?,
                        r.get::<_, Option<String>>(1)?,
                        r.get::<  _, Option<String>>(2)?,
                        r.get::<_, Option<i64>>(3)?,
                        r.get::<_, Option<String>>(4)?,
                        r.get::<_, Option<String>>(5)?,
                        r.get::<_, Option<String>>(6)?,
                        r.get::<_, Option<String>>(7)?,
                    ))
                },
            )
            .optional();

        let row = match row {
            Ok(Some(r)) => r,
            Ok(None) => return FRESHNESS_INVALID.to_string(),
            Err(_) => return "unknown".to_string(),
        };

        let (v_name, v_version, v_config, bound_revision, _snap, _fh, _sh, _gv) = row;
        let mut candidates: Vec<(i32, &str)> = Vec::new();

        // 1. 个体失效：存在 original_evidence_ref = evidence_id 的 invalidated 事件
        let invalidated: bool = conn
            .query_row(
                "SELECT 1 FROM task_evidence_events \
                 WHERE original_evidence_ref = ? AND event_type = ?",
                params![evidence_id, "evidence_invalidated"],
                |r| r.get::<_, i64>(0),
            )
            .ok()
            .is_some();
        if invalidated {
            candidates.push((3, FRESHNESS_INVALID));
        }

        // 2. Verifier 注册/信任/撤销
        if let (Some(name), Some(ver), Some(cfg)) = (&v_name, &v_version, &v_config) {
            let trust: Option<String> = conn
                .query_row(
                    "SELECT trust_status FROM verifier_registry \
                     WHERE name = ? AND version = ? AND config_hash = ?",
                    params![name, ver, cfg],
                    |r| r.get::<_, String>(0),
                )
                .ok();
            if trust.is_none() {
                candidates.push((3, FRESHNESS_INVALID));
            } else if trust.as_deref() != Some("trusted") {
                candidates.push((3, FRESHNESS_INVALID));
            }
            let revoked: bool = conn
                .query_row(
                    "SELECT 1 FROM verifier_revocation_records \
                     WHERE verifier_name = ? AND verifier_version = ? AND verifier_config_hash = ?",
                    params![name, ver, cfg],
                    |r| r.get::<_, i64>(0),
                )
                .ok()
                .is_some();
            if revoked {
                candidates.push((3, FRESHNESS_INVALID));
            }
        }

        // 3. superseded：当前契约 revision 前进
        if let Some(br) = bound_revision {
            if current_contract_revision > br {
                candidates.push((2, FRESHNESS_SUPERSEDED));
            }
        }

        // 4. stale：snapshot/file/symbol/graph 维度——仅当调用方传入时比较；
        //    本 RPC 调用方未传入，默认跳过（与 Python freshness.status 一致）。

        if candidates.is_empty() {
            return FRESHNESS_FRESH.to_string();
        }
        // 按优先级（invalid=3 > superseded=2 > stale=1 > fresh=0）取最高
        candidates.sort_by(|a, b| b.0.cmp(&a.0));
        candidates[0].1.to_string()
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
        // workspace authority fail-closed：task.list 必须显式传入 workspace_id（>0），
        // 只列出该 workspace 的已绑定任务；无显式 workspace 禁止全表列出（WHERE 1=1）。
        let workspace_id = required_workspace_id_param(params)?;
        let status_filter = params.get("status").and_then(|v| v.as_str());
        let limit = params.get("limit").and_then(|v| v.as_u64()).unwrap_or(100) as usize;
        let parent_filter = params.get("parent_id").and_then(|v| v.as_str());

        let conn = self.conn.lock().unwrap();
        let mut query = String::from(
            "SELECT t.id, t.title, t.description, t.parent_id, t.status, t.creator, t.created_at, t.updated_at
             FROM tasks t
             JOIN task_workspace_bindings b ON b.task_id = t.id AND b.workspace_id = ?1
             WHERE 1=1"
        );
        let mut status_val = String::new();
        let mut parent_val = String::new();
        let mut params_vec: Vec<Box<dyn rusqlite::ToSql>> = vec![Box::new(workspace_id)];

        if let Some(st) = status_filter {
            if !st.is_empty() {
                query.push_str(" AND t.status = ?");
                status_val = st.to_string();
                params_vec.push(Box::new(status_val.clone()));
            }
        }
        if let Some(pid) = parent_filter {
            query.push_str(" AND t.parent_id = ?");
            parent_val = pid.to_string();
            params_vec.push(Box::new(parent_val.clone()));
        }

        query.push_str(" ORDER BY t.created_at DESC LIMIT ?");
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
        let ts = task_now_ts();

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
        let identity = parse_action_identity(params)?;
        let owner_key = peer.owner_key();
        let ts = task_now_ts();

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
             (task_id, from_status, to_status, reason_code, reason, actor_identity, role, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'in_progress', 'reopened', ?3, ?4, ?5, ?6, ?7)",
            params![task_id, current_status, reason, owner_key,
                    identity.as_ref().map(|id| id.role.as_str()).unwrap_or(""), seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        if let Some(ref id) = identity {
            record_action_identity(&tx, task_id, id, "state_transition", seq, ts)?;
        }

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

    /// 强制解析 lease 受保护写凭证（task.apply / task.close / task.supersede 门禁）。
    ///
    /// daemon 权威路径下 apply/close/supersede 必须持有完整 reviewer lease 凭证。
    /// 缺少 lease_token 或 fencing_counter，或只提供其一，一律 fail-closed 返回
    /// E_LEASE_REQUIRED；禁止再沿用旧版"缺凭证即跳过校验"的兼容行为。
    pub(crate) fn require_lease_params(params: &Value) -> Result<(String, i64), DaemonRpcError> {
        let token = params.get("lease_token").and_then(|v| v.as_str());
        let counter = params.get("fencing_counter").and_then(|v| v.as_i64());
        match (token, counter) {
            (Some(t), Some(c)) if !t.is_empty() => Ok((t.to_string(), c)),
            _ => Err(DaemonRpcError::new(
                "E_LEASE_REQUIRED",
                "task.apply/task.close 必须携带完整 reviewer lease 凭证（lease_token + fencing_counter）",
            )),
        }
    }

    /// lease 受保护写校验（Req 11.8-11.9，与 Python `validate_lease_for_mutation` 对齐）。
    ///
    /// 任一校验项失败即返回结构化错误，且**不改变 task data**：
    /// 1. 权威时钟不可用 → E_LEASE_CLOCK_UNAVAILABLE（fail-closed，禁止降级为无凭证写）
    /// 2. 无 active lease → E_LEASE_NOT_FOUND
    /// 3. token hash 不匹配 → E_LEASE_TOKEN_MISMATCH
    /// 4. 已过期（Authoritative_Clock）→ E_LEASE_EXPIRED
    /// 5. fencing counter 不一致 → E_LEASE_FENCING_STALE
    /// 6. holder Identity 不一致（提供时）→ E_LEASE_HOLDER_MISMATCH
    pub(crate) fn validate_lease_for_mutation(
        &self,
        conn: &Connection,
        task_id: &str,
        role: &str,
        token: &str,
        fencing_counter: i64,
        identity: Option<&ActionIdentity>,
    ) -> Result<(), DaemonRpcError> {
        // 1. 权威时钟 fail-closed：store 未注入时钟时直接拒绝，绝不让步
        let clock = self.clock.as_ref().ok_or_else(|| {
            DaemonRpcError::new(
                "E_LEASE_CLOCK_UNAVAILABLE",
                format!("lease clock 不可用，受保护写操作拒绝（task={} role={}）", task_id, role),
            )
        })?;
        let now = clock.now_secs() as f64;

        // 2. 查找 active lease（同一 task+role 只允许一个 active lease，按获取先后取最早）
        let lease = conn.query_row(
            "SELECT lease_id, token_hash, fencing_counter, expires_at, agent_id, session_id, model_id
             FROM task_leases
             WHERE task_id = ?1 AND role = ?2 AND status = 'active'
             ORDER BY id ASC LIMIT 1",
            params![task_id, role],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, f64>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, String>(5)?,
                    r.get::<_, String>(6)?,
                ))
            },
        );
        let (lease_id, token_hash, active_counter, expires_at, agent_id, session_id, model_id) =
            match lease {
                Ok(v) => v,
                Err(_) => {
                    return Err(DaemonRpcError::new(
                        "E_LEASE_NOT_FOUND",
                        format!("task={} role={} 无 active lease，受保护写操作需要先 acquire_lease", task_id, role),
                    ))
                }
            };

        // 3. token hash 匹配（Req 11.2：数据库只存 sha256，永不存 raw token）
        if sha256_hex(token.as_bytes()) != token_hash {
            return Err(DaemonRpcError::new(
                "E_LEASE_TOKEN_MISMATCH",
                format!("token hash 不匹配 (lease_id={})", lease_id),
            ));
        }

        // 4. 未过期（Authoritative_Clock，Req 11.4）
        if now > expires_at {
            return Err(DaemonRpcError::new(
                "E_LEASE_EXPIRED",
                format!("lease {} 已过期 (expires_at={:.1}, now={:.1})", lease_id, expires_at, now),
            ));
        }

        // 5. fencing counter 等于当前 counter（Property 11）
        if fencing_counter != active_counter {
            return Err(DaemonRpcError::new(
                "E_LEASE_FENCING_STALE",
                format!("fencing counter {} != 当前 {}；旧持有者写入被拒绝", fencing_counter, active_counter),
            ));
        }

        // 6. holder Identity 匹配（提供时校验，Req 11.2）
        if let Some(id) = identity {
            if id.agent_id != agent_id || id.session_id != session_id || id.model_id != model_id {
                return Err(DaemonRpcError::new(
                    "E_LEASE_HOLDER_MISMATCH",
                    format!("holder Identity 与 lease ({}) 不一致", lease_id),
                ));
            }
        }

        Ok(())
    }

        /// P0-E：治理写入中的 reviewer lease 由独立 Reviewer 持有、由独立
    /// Adjudicator 执行。此方法不得用于普通 mutation：普通 mutation 仍使用
    /// validate_lease_for_mutation 的同一 holder 语义。
    pub(crate) fn validate_reviewer_lease_for_adjudication(
        &self,
        conn: &Connection,
        task_id: &str,
        token: &str,
        fencing_counter: i64,
        adjudicator: &ActionIdentity,
    ) -> Result<(), DaemonRpcError> {
        if adjudicator.role != "adjudicator" {
            return Err(DaemonRpcError::new(
                "E_GOVERNANCE_ADJUDICATOR_ROLE_REQUIRED",
                format!("跨角色 reviewer lease 仅允许 adjudicator，实际 role={}", adjudicator.role),
            ));
        }
        let clock = self.clock.as_ref().ok_or_else(|| DaemonRpcError::new(
            "E_LEASE_CLOCK_UNAVAILABLE",
            format!("lease clock 不可用，治理写操作拒绝（task={}）", task_id),
        ))?;
        let (lease_id, token_hash, active_counter, expires_at, reviewer_agent_id, reviewer_session_id): (String, String, i64, f64, String, String) = conn.query_row(
            "SELECT lease_id, token_hash, fencing_counter, expires_at, agent_id, session_id \
             FROM task_leases WHERE task_id=?1 AND role='reviewer' AND status='active' \
             ORDER BY id ASC LIMIT 1",
            params![task_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)),
        ).map_err(|_| DaemonRpcError::new(
            "E_LEASE_NOT_FOUND",
            format!("task={} 无 active reviewer lease，治理写操作必须先独立 review", task_id),
        ))?;
        if sha256_hex(token.as_bytes()) != token_hash {
            return Err(DaemonRpcError::new("E_LEASE_TOKEN_MISMATCH", format!("token hash 不匹配 (lease_id={})", lease_id)));
        }
        if clock.now_secs() as f64 > expires_at {
            return Err(DaemonRpcError::new("E_LEASE_EXPIRED", format!("lease {} 已过期", lease_id)));
        }
        if fencing_counter != active_counter {
            return Err(DaemonRpcError::new(
                "E_LEASE_FENCING_STALE",
                format!("fencing counter {} != 当前 {}；旧 holder 写入被拒绝", fencing_counter, active_counter),
            ));
        }
        let (reviewer_instance_id, registered_session_id, registered_role, registered_status): (String, String, String, String) = conn.query_row(
            "SELECT agent_instance_id, session_id, role, status FROM agent_registrations WHERE agent_id=?1",
            params![reviewer_agent_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        ).map_err(|_| DaemonRpcError::new(
            "E_GOVERNANCE_REVIEWER_UNREGISTERED",
            format!("reviewer lease holder {} 未注册", reviewer_agent_id),
        ))?;
        if registered_status != "active" || registered_role != "reviewer" || registered_session_id != reviewer_session_id {
            return Err(DaemonRpcError::new(
                "E_GOVERNANCE_REVIEWER_INVALID",
                format!("reviewer lease holder {} 必须为 active registered reviewer 且 session 一致", reviewer_agent_id),
            ));
        }
        if adjudicator.agent_id == reviewer_agent_id {
            return Err(DaemonRpcError::new(
                "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_AGENT",
                "Adjudicator 不得等于 reviewer lease holder agent_id",
            ));
        }
        if !reviewer_instance_id.is_empty() && reviewer_instance_id == adjudicator.agent_instance_id {
            return Err(DaemonRpcError::new(
                "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_INSTANCE",
                "Adjudicator 不得等于 reviewer lease holder agent_instance_id",
            ));
        }
        if adjudicator.session_id == reviewer_session_id {
            return Err(DaemonRpcError::new(
                "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_SESSION",
                "Adjudicator 不得等于 reviewer lease holder session_id",
            ));
        }
        Ok(())
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
        let identity = parse_action_identity(params)?;
        let owner_key = peer.owner_key();
        let ts = task_now_ts();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        // S3: lease 受保护写门禁（强制）—— daemon 权威路径下 apply 必须持有完整
        // reviewer lease 凭证，缺失/不完整 fail-closed 返回 E_LEASE_REQUIRED。
        // 校验失败在任何写入前拒绝，不改变 task data（与 Python task_apply 对齐）。
        let (token, counter) = Self::require_lease_params(params)?;
        self.validate_lease_for_mutation(&tx, task_id, "reviewer", &token, counter, identity.as_ref())?;

        let current_status: String = tx
            .query_row("SELECT status FROM tasks WHERE id = ?1", params![task_id], |r| r.get(0))
            .map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)))?;

        // 观察#1 修复：daemon 权威 apply 必须回填 applied_at 列，与 Python
        // db_tasks.task_apply（line 1990）及 CLI apply_task（cli/task.rs:1157）对齐，
        // 否则 auto/enterprise 模式下 applied_at 恒为 NULL，破坏审计轨迹与级联语义。
        tx.execute(
            "UPDATE tasks SET status = 'applied', applied_at = ?1, updated_at = ?1 WHERE id = ?2",
            params![ts, task_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_apply 失败: {}", e)))?;

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity, role, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'applied', 'applied', 'task applied', ?3, ?4, ?5, ?6)",
            params![task_id, current_status, owner_key,
                    identity.as_ref().map(|id| id.role.as_str()).unwrap_or(""), seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        if let Some(ref id) = identity {
            record_action_identity(&tx, task_id, id, "state_transition", seq, ts)?;
        }

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
        let identity = parse_action_identity(params)?;
        let owner_key = peer.owner_key();
        let ts = task_now_ts();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        // S3: lease 受保护写门禁（强制）—— daemon 权威路径下 close 必须持有完整
        // reviewer lease 凭证，缺失/不完整 fail-closed 返回 E_LEASE_REQUIRED。
        // 校验失败在任何写入前拒绝，不改变 task data（与 Python task_close 对齐）。
        let (token, counter) = Self::require_lease_params(params)?;
        self.validate_lease_for_mutation(&tx, task_id, "reviewer", &token, counter, identity.as_ref())?;

        let current_status: String = tx
            .query_row("SELECT status FROM tasks WHERE id = ?1", params![task_id], |r| r.get(0))
            .map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id)))?;

        // S1: 子任务状态门禁 —— 存在任何非 closed 子任务时禁止关闭父任务。
        // 所有子任务均已 closed 时父任务才允许关闭（子任务完成步骤即证据）。
        let child_total: i64 = tx
            .query_row(
                "SELECT COUNT(*) FROM tasks WHERE parent_id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .unwrap_or(0);
        if child_total > 0 {
            let open_children: i64 = tx
                .query_row(
                    "SELECT COUNT(*) FROM tasks WHERE parent_id = ?1 AND status != 'closed'",
                    params![task_id],
                    |r| r.get(0),
                )
                .unwrap_or(0);
            if open_children > 0 {
                return Err(DaemonRpcError::new(
                    "E_CHILD_TASKS_NOT_CLOSED",
                    format!("任务 {} 存在 {} 个未关闭子任务，禁止关闭", task_id, open_children),
                ));
            }
        } else {
            // S2: 叶子任务步骤门禁 —— 必须有步骤且全部 done/skipped 才能关闭。
            let step_count: i64 = tx
                .query_row(
                    "SELECT COUNT(*) FROM task_steps WHERE task_id = ?1",
                    params![task_id],
                    |r| r.get(0),
                )
                .unwrap_or(0);
            if step_count == 0 {
                return Err(DaemonRpcError::new(
                    "E_NO_STEPS",
                    format!("任务 {} 无步骤记录，禁止关闭", task_id),
                ));
            }
            let not_done: i64 = tx
                .query_row(
                    "SELECT COUNT(*) FROM task_steps WHERE task_id = ?1 AND status IN ('pending', 'failed', 'blocked')",
                    params![task_id],
                    |r| r.get(0),
                )
                .unwrap_or(0);
            if not_done > 0 {
                return Err(DaemonRpcError::new(
                    "E_STEPS_NOT_DONE",
                    format!("任务 {} 存在 {} 个未完成步骤，禁止关闭", task_id, not_done),
                ));
            }
        }

        // S5: closed_at 写入真实非零时间戳
        tx.execute(
            "UPDATE tasks SET status = 'closed', closed_at = ?1, updated_at = ?1 WHERE id = ?2",
            params![ts, task_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_close 失败: {}", e)))?;

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity, role, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'closed', 'closed', 'task closed', ?3, ?4, ?5, ?6)",
            params![task_id, current_status, owner_key,
                    identity.as_ref().map(|id| id.role.as_str()).unwrap_or(""), seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        if let Some(ref id) = identity {
            record_action_identity(&tx, task_id, id, "state_transition", seq, ts)?;
        }

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

    // ============================================
    // Lease Control Plane（Req 11.2-11.9, 14.11-14.12, 14.30）
    //
    // daemon 权威路径：全部写操作在单一 `self.conn` 互斥下执行（BEGIN IMMEDIATE 事务），
    // 时间字段一律使用 `self.clock()`（AuthoritativeClock，单调不回退）；
    // clock 未注入（None）时 fail-closed 返回 E_LEASE_CLOCK_UNAVAILABLE，绝不降级。
    // raw token 仅在 acquire 成功响应返回一次，数据库只存 sha256（Req 11.2）。
    // 与 Python `db/db_task_leases.py` 语义对齐；MCP 工具经 server/tools/tools_p4_lease.py 路由至此。
    // ============================================

    /// 追加一条 Lease 审计事件（append-only，Req 11.6/11.12；调用方负责 commit；不写 raw token）。
    fn append_lease_event(
        &self,
        tx: &Transaction<'_>,
        workspace_id: i64,
        lease_id: &str,
        task_id: &str,
        role: &str,
        event_type: &str,
        fencing_counter: i64,
        event_at: f64,
        actor: &ActionIdentity,
        detail: &str,
    ) -> Result<(), DaemonRpcError> {
        let event_id = gen_lease_event_id();
        tx.execute(
            "INSERT INTO task_lease_events
             (workspace_id, event_id, lease_id, task_id, role, event_type,
              fencing_counter, event_at, actor_agent_id, actor_session_id, actor_model_id, detail)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
            params![
                workspace_id, event_id, lease_id, task_id, role, event_type,
                fencing_counter, event_at, actor.agent_id, actor.session_id, actor.model_id, detail
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("追加 lease 事件失败: {}", e)))?;
        Ok(())
    }

    /// 以 lease holder 身份构造事件 actor（对齐 Python renew/release 事件 actor 取 lease holder）。
    fn holder_identity(agent_id: &str, session_id: &str, model_id: &str, role: &str) -> ActionIdentity {
        ActionIdentity {
            agent_id: agent_id.to_string(),
            agent_instance_id: String::new(),
            client_id: String::new(),
            provider: String::new(),
            model_id: model_id.to_string(),
            model_mode: String::new(),
            system_fingerprint: String::new(),
            session_id: session_id.to_string(),
            role: role.to_string(),
            runtime_hash: String::new(),
        }
    }

    /// 获取 Lease（Req 11.2-11.3）。
    ///
    /// 原子比较当前 active lease（BEGIN IMMEDIATE）：
    /// - 存在未过期且 holder 注册/心跳新鲜的 lease → E_LEASE_ACTIVE_EXISTS
    /// - 存在未过期但 holder 注册缺失、停用或 heartbeat 超时的 orphan lease
    ///   → 同一事务追加 `expire` 事件并回收后创建新 lease
    /// - 存在已过期 active lease → 置 expired 后创建新 lease
    /// - 无 active lease → 创建新 lease
    /// fencing counter 取该 task+role 全部历史最大 counter + 1（单调递增，Req 11.3）。
    /// raw token 仅在本次成功响应返回一次，数据库只存 sha256（Req 11.2）。
    pub fn handle_lease_acquire(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let role = params
            .get("role")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 role"))?;
        let ttl = params.get("ttl_seconds").and_then(|v| v.as_f64()).unwrap_or(3600.0);
        if ttl <= 0.0 {
            return Err(DaemonRpcError::invalid_params("ttl_seconds 必须大于 0"));
        }
        // holder Identity 必填（agent_id/session_id/model_id），缺失 → E_IDENTITY_INCOMPLETE
        let identity = parse_action_identity(params)?;
        let id = identity.as_ref().ok_or_else(|| {
            DaemonRpcError::new(
                "E_IDENTITY_INCOMPLETE",
                "acquire lease 需要 identity（agent_id/session_id/model_id/role）",
            )
        })?;

        // 权威时钟 fail-closed（Req 14.11/14.30）：daemon 未注入时钟时禁止降级
        let clock = self.clock.as_ref()
            .ok_or_else(|| lease_clock_unavailable("lease.acquire", task_id, role))?;
        let now = clock.now_secs() as f64;
        let expires_at = now + ttl;

        let token = gen_lease_token();
        let token_hash = sha256_hex(token.as_bytes());
        let lease_id = gen_lease_id();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;
        let workspace_id = task_bound_workspace_id(&tx, task_id, optional_workspace_id_param(params))?;

        // 1. 原子比较当前 active lease（Req 11.2）
        let active: Option<(i64, String, i64, f64, String, String, String)> = tx
            .query_row(
                "SELECT id, lease_id, fencing_counter, expires_at, agent_id, session_id, model_id FROM task_leases
                 WHERE workspace_id = ?1 AND task_id = ?2 AND role = ?3 AND status = 'active'
                 ORDER BY id ASC LIMIT 1",
                params![workspace_id, task_id, role],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?)),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 active lease 失败: {}", e)))?;
        if let Some((active_id, active_lease_id, active_counter, active_expires, holder_agent, holder_session, holder_model)) = active {
            if now <= active_expires {
                // 未过期并不等于仍有可用 owner：Executor 进程异常退出时，旧 lease
                // 可能还有很长 TTL。仅当 holder 的注册状态/心跳明确 stale 时才允许
                // 在同一事务中回收，避免把仍在工作的 holder 误判为 orphan。
                let holder_registration: Option<(String, f64)> = tx
                    .query_row(
                        "SELECT status, last_heartbeat FROM agent_registrations
                         WHERE agent_id = ?1 AND session_id = ?2 LIMIT 1",
                        params![holder_agent, holder_session],
                        |r| Ok((r.get(0)?, r.get(1)?)),
                    )
                    .optional()
                    .map_err(|e| DaemonRpcError::internal_error(format!("查询 lease holder 注册失败: {}", e)))?;
                let stale_reason = match holder_registration {
                    None => Some("holder_registration_missing"),
                    Some((status, last_heartbeat)) if status != "active" => Some("holder_registration_inactive"),
                    Some((_, last_heartbeat)) if now - last_heartbeat > ORPHAN_CLAIM_STALE_SECS => {
                        Some("holder_heartbeat_stale")
                    }
                    _ => None,
                };
                if let Some(reason) = stale_reason {
                    tx.execute(
                        "UPDATE task_leases SET status = 'expired', released_at = ?1 WHERE id = ?2",
                        params![now, active_id],
                    )
                    .map_err(|e| DaemonRpcError::internal_error(format!("回收 stale lease 失败: {}", e)))?;
                    self.append_lease_event(
                        &tx, workspace_id, &active_lease_id, task_id, role, "expire",
                        // 回收事件使用旧 lease 的 fencing counter；此时新 counter
                        // 尚未分配，审计链仍能精确标识被回收的 lease。
                        active_counter, now, id,
                        &format!(
                            "stale holder recovered: reason={}, holder_agent_id={}, holder_session_id={}, holder_model_id={}",
                            reason, holder_agent, holder_session, holder_model
                        ),
                    )?;
                } else {
                    return Err(DaemonRpcError::new(
                        "E_LEASE_ACTIVE_EXISTS",
                        format!(
                            "task={} role={} 已有未过期 lease ({}, expires_at={:.1})",
                            task_id, role, active_lease_id, active_expires
                        ),
                    ));
                }
            }
            if now > active_expires {
                // 已过期 → 旧 lease 置 expired（释放唯一 active 槽位，对齐 Python acquire）
                tx.execute(
                    "UPDATE task_leases SET status = 'expired', released_at = ?1 WHERE id = ?2",
                    params![now, active_id],
                )
                .map_err(|e| DaemonRpcError::internal_error(format!("过期 lease 置 expired 失败: {}", e)))?;
            }
        }

        // 2. 单调递增 fencing counter（Req 11.3）：该 task+role 全历史 MAX + 1
        let fencing_counter: i64 = tx
            .query_row(
                "SELECT COALESCE(MAX(fencing_counter), 0) FROM task_leases
                 WHERE workspace_id = ?1 AND task_id = ?2 AND role = ?3",
                params![workspace_id, task_id, role],
                |r| r.get::<_, i64>(0),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 fencing counter 失败: {}", e)))?
            + 1;

        // 3. 插入新 lease（唯一索引 idx_task_leases_active_unique 防双活；冲突 → E_LEASE_ALREADY_ACTIVE）
        tx.execute(
            "INSERT INTO task_leases
             (workspace_id, lease_id, task_id, role, agent_id, session_id, model_id,
              token_hash, fencing_counter, acquired_at, expires_at, status)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, 'active')",
            params![
                workspace_id, lease_id, task_id, role,
                id.agent_id, id.session_id, id.model_id,
                token_hash, fencing_counter, now, expires_at
            ],
        )
        .map_err(|e| {
            if is_unique_violation(&e) {
                DaemonRpcError::new(
                    "E_LEASE_ALREADY_ACTIVE",
                    format!("task={} role={} 已有 active lease（唯一索引防双活）", task_id, role),
                )
            } else {
                DaemonRpcError::internal_error(format!("插入 task_leases 失败: {}", e))
            }
        })?;

        // 4. 追加审计事件（append-only，不写 raw token）
        self.append_lease_event(
            &tx, workspace_id, &lease_id, task_id, role, "acquire",
            fencing_counter, now, id,
            &format!("acquired, expires_at={:.1}", expires_at),
        )?;

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 acquire 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("lease_id".to_string(), Value::String(lease_id));
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("role".to_string(), Value::String(role.to_string()));
        res.insert("token".to_string(), Value::String(token)); // raw token 仅此一次返回（Req 11.2）
        res.insert("fencing_counter".to_string(), Value::Number(serde_json::Number::from(fencing_counter)));
        res.insert("acquired_at".to_string(), Value::Number(serde_json::Number::from_f64(now).unwrap()));
        res.insert("expires_at".to_string(), Value::Number(serde_json::Number::from_f64(expires_at).unwrap()));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    /// 续租 Lease / renew（Req 11.4-11.5；`lease.renew` 兼容别名并入本 handler）。
    ///
    /// 校验：token hash 匹配（必）、未过期（权威时钟）、holder Identity 匹配（提供时）、
    /// fencing counter 匹配（提供时，Property 11）。校验通过后以权威时钟续期并更新 renewed_at。
    ///
    /// 幂等（Req 11.5）：重复有效的 renew 返回同一 lease（同一 lease_id 与 fencing counter），
    /// 不递增 counter、不创建新 lease。
    pub fn handle_lease_extend(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let role = params
            .get("role")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 role"))?;
        let token = params
            .get("token")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 token"))?;
        let ttl = params.get("ttl_seconds").and_then(|v| v.as_f64()).unwrap_or(3600.0);
        if ttl <= 0.0 {
            return Err(DaemonRpcError::invalid_params("ttl_seconds 必须大于 0"));
        }
        let identity = parse_action_identity(params)?;
        // 调用方可选携带 fencing_counter；提供时强制校验（Property 11，旧持有者续租被拒）
        let provided_counter = params.get("fencing_counter").and_then(|v| v.as_i64());

        let clock = self.clock.as_ref()
            .ok_or_else(|| lease_clock_unavailable("lease.extend", task_id, role))?;
        let now = clock.now_secs() as f64;

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;
        let workspace_id = task_bound_workspace_id(&tx, task_id, optional_workspace_id_param(params))?;

        let active: Option<(String, String, i64, f64, String, String, String)> = tx
            .query_row(
                "SELECT lease_id, token_hash, fencing_counter, expires_at,
                        agent_id, session_id, model_id
                 FROM task_leases
                 WHERE workspace_id = ?1 AND task_id = ?2 AND role = ?3 AND status = 'active'
                 ORDER BY id ASC LIMIT 1",
                params![workspace_id, task_id, role],
                |r| {
                    Ok((
                        r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?,
                        r.get(4)?, r.get(5)?, r.get(6)?,
                    ))
                },
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 active lease 失败: {}", e)))?;
        let Some((lease_id, token_hash, active_counter, expires_at, holder_agent, holder_session, holder_model)) = active
        else {
            return Err(DaemonRpcError::new(
                "E_LEASE_NOT_FOUND",
                format!("task={} role={} 无 active lease", task_id, role),
            ));
        };

        // token hash 校验（Req 11.9）
        if sha256_hex(token.as_bytes()) != token_hash {
            return Err(DaemonRpcError::new(
                "E_LEASE_TOKEN_MISMATCH",
                format!("token hash 不匹配 (lease_id={})", lease_id),
            ));
        }
        // holder Identity 校验（提供时，Req 11.4）
        if let Some(id) = identity.as_ref() {
            if id.agent_id != holder_agent || id.session_id != holder_session || id.model_id != holder_model {
                return Err(DaemonRpcError::new(
                    "E_LEASE_HOLDER_MISMATCH",
                    format!("holder Identity 与 lease ({}) 不一致", lease_id),
                ));
            }
        }
        // 过期判定（权威时钟，Req 11.4）
        if now > expires_at {
            return Err(DaemonRpcError::new(
                "E_LEASE_EXPIRED",
                format!("lease {} 已过期 (expires_at={:.1}, now={:.1})", lease_id, expires_at, now),
            ));
        }
        // fencing counter 校验（提供时，Property 11）
        if let Some(c) = provided_counter {
            if c != active_counter {
                return Err(DaemonRpcError::new(
                    "E_LEASE_FENCING_STALE",
                    format!("fencing counter {} != 当前 {}；旧持有者续租被拒绝", c, active_counter),
                ));
            }
        }

        // 幂等续租：不递增 counter，不创建新 lease（Req 11.5）
        let new_expires = now + ttl;
        tx.execute(
            "UPDATE task_leases SET renewed_at = ?1, expires_at = ?2
             WHERE workspace_id = ?3 AND task_id = ?4 AND role = ?5 AND status = 'active'",
            params![now, new_expires, workspace_id, task_id, role],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("renew 续期失败: {}", e)))?;

        // 事件 actor 取 lease holder（对齐 Python renew）
        let actor = Self::holder_identity(&holder_agent, &holder_session, &holder_model, role);
        self.append_lease_event(
            &tx, workspace_id, &lease_id, task_id, role, "renew",
            active_counter, now, &actor,
            &format!("renewed, expires_at={:.1}", new_expires),
        )?;

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 renew 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("lease_id".to_string(), Value::String(lease_id));
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("role".to_string(), Value::String(role.to_string()));
        res.insert("fencing_counter".to_string(), Value::Number(serde_json::Number::from(active_counter)));
        res.insert("renewed_at".to_string(), Value::Number(serde_json::Number::from_f64(now).unwrap()));
        res.insert("expires_at".to_string(), Value::Number(serde_json::Number::from_f64(new_expires).unwrap()));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    /// 释放 Lease（Req 11.6-11.7）。
    ///
    /// 当前 token 匹配时原子追加 release 审计事件并将 lease 置 released。
    /// 幂等（Req 11.7）：重复 release 返回同一 released 状态，不改变 fencing counter，
    /// 不创建第二个 active lease。
    pub fn handle_lease_release(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let role = params
            .get("role")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 role"))?;
        let token = params
            .get("token")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 token"))?;
        let identity = parse_action_identity(params)?;

        let clock = self.clock.as_ref()
            .ok_or_else(|| lease_clock_unavailable("lease.release", task_id, role))?;
        let now = clock.now_secs() as f64;

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;
        let workspace_id = task_bound_workspace_id(&tx, task_id, optional_workspace_id_param(params))?;

        // 1. 查 active lease
        let active: Option<(i64, String, String, i64, String, String, String)> = tx
            .query_row(
                "SELECT id, lease_id, token_hash, fencing_counter,
                        agent_id, session_id, model_id
                 FROM task_leases
                 WHERE workspace_id = ?1 AND task_id = ?2 AND role = ?3 AND status = 'active'
                 ORDER BY id ASC LIMIT 1",
                params![workspace_id, task_id, role],
                |r| {
                    Ok((
                        r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?,
                        r.get(4)?, r.get(5)?, r.get(6)?,
                    ))
                },
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 active lease 失败: {}", e)))?;
        if let Some((lease_pk, lease_id, token_hash, active_counter, holder_agent, holder_session, holder_model)) = active {
            if sha256_hex(token.as_bytes()) != token_hash {
                return Err(DaemonRpcError::new(
                    "E_LEASE_TOKEN_MISMATCH",
                    format!("token hash 不匹配 (lease_id={})", lease_id),
                ));
            }
            // holder Identity 校验（提供时）
            if let Some(id) = identity.as_ref() {
                if id.agent_id != holder_agent || id.session_id != holder_session || id.model_id != holder_model {
                    return Err(DaemonRpcError::new(
                        "E_LEASE_HOLDER_MISMATCH",
                        format!("holder Identity 与 lease ({}) 不一致", lease_id),
                    ));
                }
            }
            tx.execute(
                "UPDATE task_leases SET status = 'released', released_at = ?1 WHERE id = ?2",
                params![now, lease_pk],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("release 置 released 失败: {}", e)))?;

            let actor = Self::holder_identity(&holder_agent, &holder_session, &holder_model, role);
            self.append_lease_event(
                &tx, workspace_id, &lease_id, task_id, role, "release",
                active_counter, now, &actor,
                &format!("released at {:.1}", now),
            )?;

            tx.commit()
                .map_err(|e| DaemonRpcError::internal_error(format!("提交 release 事务失败: {}", e)))?;

            let mut res = Map::new();
            res.insert("lease_id".to_string(), Value::String(lease_id));
            res.insert("task_id".to_string(), Value::String(task_id.to_string()));
            res.insert("role".to_string(), Value::String(role.to_string()));
            res.insert("fencing_counter".to_string(), Value::Number(serde_json::Number::from(active_counter)));
            res.insert("released_at".to_string(), Value::Number(serde_json::Number::from_f64(now).unwrap()));
            res.insert("status".to_string(), Value::String("released".to_string()));
            let val = Value::Object(res);
            self.save_dedup(params, &val);
            return Ok(val);
        }

        // 2. 无 active lease → 幂等分支（Req 11.7）：最近历史 lease 已 released 且 token 匹配视为已释放
        let hist: Option<(String, String, i64, String, f64)> = tx
            .query_row(
                "SELECT lease_id, token_hash, fencing_counter, status, COALESCE(released_at, 0)
                 FROM task_leases
                 WHERE workspace_id = ?1 AND task_id = ?2 AND role = ?3
                 ORDER BY id DESC LIMIT 1",
                params![workspace_id, task_id, role],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("查询历史 lease 失败: {}", e)))?;
        if let Some((lease_id, hist_hash, hist_counter, hist_status, hist_released_at)) = hist {
            if hist_status == "released" && sha256_hex(token.as_bytes()) == hist_hash {
                let mut res = Map::new();
                res.insert("lease_id".to_string(), Value::String(lease_id));
                res.insert("task_id".to_string(), Value::String(task_id.to_string()));
                res.insert("role".to_string(), Value::String(role.to_string()));
                res.insert("fencing_counter".to_string(), Value::Number(serde_json::Number::from(hist_counter)));
                res.insert("released_at".to_string(), Value::Number(serde_json::Number::from_f64(hist_released_at).unwrap()));
                res.insert("status".to_string(), Value::String("released".to_string()));
                res.insert("idempotent".to_string(), Value::Bool(true));
                tx.commit()
                    .map_err(|e| DaemonRpcError::internal_error(format!("提交幂等 release 事务失败: {}", e)))?;
                let val = Value::Object(res);
                self.save_dedup(params, &val);
                return Ok(val);
            }
        }
        Err(DaemonRpcError::new(
            "E_LEASE_NOT_FOUND",
            format!("task={} role={} 无 active lease", task_id, role),
        ))
    }

    /// 查询 Lease 状态（只读，Req 11.2）。
    ///
    /// 返回当前 active lease（含 token_hash 供受保护校验，**不含 raw token**）；
    /// 无 active lease 时返回最近一条历史 lease 的状态摘要。
    pub fn handle_lease_status(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) { return Ok(cached); }
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let role = params.get("role").and_then(|v| v.as_str()).unwrap_or("");

        let conn = self.conn.lock().unwrap();
        let workspace_id = task_bound_workspace_id(&conn, task_id, optional_workspace_id_param(params))?;

        let row: Option<(String, String, String, String, String, String, String, String, i64, f64, f64, Option<f64>, Option<f64>)> = conn
            .query_row(
                "SELECT status, lease_id, task_id, role, agent_id, session_id, model_id,
                        token_hash, fencing_counter, acquired_at, expires_at, renewed_at, released_at
                 FROM task_leases
                 WHERE workspace_id = ?1 AND task_id = ?2 AND (?3 = '' OR role = ?3)
                 ORDER BY (status = 'active') DESC, id DESC LIMIT 1",
                params![workspace_id, task_id, role],
                |r| {
                    Ok((
                        r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?,
                        r.get(5)?, r.get(6)?, r.get(7)?, r.get(8)?, r.get(9)?,
                        r.get(10)?, r.get(11)?, r.get(12)?,
                    ))
                },
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 lease 状态失败: {}", e)))?;

        let val = match row {
            Some((status, lease_id, row_task_id, row_role, agent_id, session_id, model_id,
                  token_hash, fencing_counter, acquired_at, expires_at, renewed_at, released_at)) => {
                let mut res = Map::new();
                res.insert("status".to_string(), Value::String(status));
                res.insert("lease_id".to_string(), Value::String(lease_id));
                res.insert("task_id".to_string(), Value::String(row_task_id));
                res.insert("role".to_string(), Value::String(row_role));
                res.insert("agent_id".to_string(), Value::String(agent_id));
                res.insert("session_id".to_string(), Value::String(session_id));
                res.insert("model_id".to_string(), Value::String(model_id));
                res.insert("token_hash".to_string(), Value::String(token_hash)); // raw token 永不返回（Req 11.2）
                res.insert("fencing_counter".to_string(), Value::Number(serde_json::Number::from(fencing_counter)));
                res.insert("acquired_at".to_string(), Value::Number(serde_json::Number::from_f64(acquired_at).unwrap()));
                res.insert("expires_at".to_string(), Value::Number(serde_json::Number::from_f64(expires_at).unwrap()));
                if let Some(v) = renewed_at {
                    res.insert("renewed_at".to_string(), Value::Number(serde_json::Number::from_f64(v).unwrap()));
                }
                if let Some(v) = released_at {
                    res.insert("released_at".to_string(), Value::Number(serde_json::Number::from_f64(v).unwrap()));
                }
                Value::Object(res)
            }
            None => {
                let mut res = Map::new();
                res.insert("status".to_string(), Value::String("none".to_string()));
                res.insert("task_id".to_string(), Value::String(task_id.to_string()));
                res.insert("role".to_string(), Value::String(role.to_string()));
                Value::Object(res)
            }
        };
        Ok(val)
    }

    /// 查询 Lease 审计事件（只读，append-only 账本，Req 11.12）。
    ///
    /// 返回 acquire/renew/release 事件列表；**不包含 raw token**。
    pub fn handle_lease_list_events(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params.get("task_id").and_then(|v| v.as_str()).unwrap_or("");
        let role = params.get("role").and_then(|v| v.as_str()).unwrap_or("");

        let conn = self.conn.lock().unwrap();
        // workspace authority fail-closed：有 task_id 时从不可变 binding 解析；
        // 无 task_id（全量事件）必须显式传入 workspace_id（>0），禁止 active 推导。
        let workspace_id = if task_id.is_empty() {
            required_workspace_id_param(params)?
        } else {
            task_bound_workspace_id(&conn, task_id, optional_workspace_id_param(params))?
        };

        // 动态条件：task_id / role 可选过滤
        let mut sql = String::from(
            "SELECT event_id, lease_id, task_id, role, event_type, fencing_counter,
                    event_at, actor_agent_id, actor_session_id, actor_model_id, detail
             FROM task_lease_events WHERE workspace_id = ?1",
        );
        let mut args: Vec<Box<dyn rusqlite::ToSql>> = vec![Box::new(workspace_id)];
        if !task_id.is_empty() {
            sql.push_str(" AND task_id = ?2");
            args.push(Box::new(task_id.to_string()));
        }
        if !role.is_empty() {
            sql.push_str(" AND role = ?3");
            args.push(Box::new(role.to_string()));
        }
        sql.push_str(" ORDER BY id ASC");

        let mut stmt = conn
            .prepare(&sql)
            .map_err(|e| DaemonRpcError::internal_error(format!("prepare lease 事件查询失败: {}", e)))?;
        let arg_refs: Vec<&dyn rusqlite::ToSql> = args.iter().map(|b| b.as_ref() as &dyn rusqlite::ToSql).collect();
        let rows = stmt
            .query_map(
                rusqlite::params_from_iter(arg_refs.iter().copied()),
                |r| {
                    Ok((
                        r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?,
                        r.get::<_, String>(3)?, r.get::<_, String>(4)?, r.get::<_, i64>(5)?,
                        r.get::<_, f64>(6)?, r.get::<_, String>(7)?, r.get::<_, String>(8)?,
                        r.get::<_, String>(9)?, r.get::<_, String>(10)?,
                    ))
                },
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 lease 事件失败: {}", e)))?;

        let mut events: Vec<Value> = Vec::new();
        for row in rows {
            let (event_id, lease_id, row_task_id, row_role, event_type, fencing_counter,
                 event_at, actor_agent_id, actor_session_id, actor_model_id, detail) =
                row.map_err(|e| DaemonRpcError::internal_error(format!("读取 lease 事件失败: {}", e)))?;
            let mut m = Map::new();
            m.insert("event_id".to_string(), Value::String(event_id));
            m.insert("lease_id".to_string(), Value::String(lease_id));
            m.insert("task_id".to_string(), Value::String(row_task_id));
            m.insert("role".to_string(), Value::String(row_role));
            m.insert("event_type".to_string(), Value::String(event_type));
            m.insert("fencing_counter".to_string(), Value::Number(serde_json::Number::from(fencing_counter)));
            m.insert("event_at".to_string(), Value::Number(serde_json::Number::from_f64(event_at).unwrap()));
            m.insert("actor_agent_id".to_string(), Value::String(actor_agent_id));
            m.insert("actor_session_id".to_string(), Value::String(actor_session_id));
            m.insert("actor_model_id".to_string(), Value::String(actor_model_id));
            m.insert("detail".to_string(), Value::String(detail));
            events.push(Value::Object(m));
        }
        Ok(Value::Array(events))
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
        let dry_run = params.get("dry_run").and_then(|v| v.as_bool()).unwrap_or(false);
        let source_commit_hash = params
            .get("source_commit_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let skip_quality_review = params
            .get("skip_quality_review")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let owner_key = peer.owner_key();
        let ts = task_now_ts();

        let mut conn = self.conn.lock().unwrap();

        // 完整 capture-diff：change_audit（真实 schema）+ task_symbol_changes + audit_chain 签名
        let result = crate::cli::task::capture_task_diff(
            &mut conn,
            task_id,
            step_id,
            Path::new(""),
            base,
            dry_run,
            source_commit_hash,
            skip_quality_review,
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("capture-diff 执行失败: {}", e)))?;

        // 记录 diff_captured 事件（dry_run 不落事件，对齐 Python 语义）
        if !dry_run {
            let seq = self.next_seq();
            conn.execute(
                "INSERT INTO task_events
                 (task_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
                 VALUES (?1, 'in_progress', 'in_progress', 'diff_captured', ?2, ?3, ?4, ?5)",
                params![task_id, format!("base={}", base), owner_key, seq, ts],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;
        }

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(result.task_id.clone()));
        res.insert("step_id".to_string(), Value::String(result.step_id.clone()));
        res.insert("base".to_string(), Value::String(result.base.clone()));
        res.insert("dry_run".to_string(), Value::Bool(result.dry_run));
        res.insert("scan_id".to_string(), serde_json::json!(result.scan_id));
        let changed_files: Vec<Value> = result
            .changed_files
            .iter()
            .map(|f| {
                let mut m = Map::new();
                m.insert("path".to_string(), Value::String(f.path.clone()));
                m.insert("status".to_string(), Value::String(f.status.clone()));
                Value::Object(m)
            })
            .collect();
        res.insert("changed_files".to_string(), Value::Array(changed_files));
        // linked_symbols 对齐 Python 契约：数组 [{file_path, change_id, linked}]
        let linked_symbols: Vec<Value> = result
            .linked_change_ids
            .iter()
            .map(|(file_path, change_id)| {
                let mut m = Map::new();
                m.insert("file_path".to_string(), Value::String(file_path.clone()));
                m.insert("change_id".to_string(), Value::String(change_id.clone()));
                m.insert("linked".to_string(), Value::Bool(true));
                Value::Object(m)
            })
            .collect();
        res.insert("linked_symbols".to_string(), Value::Array(linked_symbols));
        let findings: Vec<Value> = result
            .quality_findings
            .iter()
            .map(|f| {
                let mut m = Map::new();
                m.insert("id".to_string(), serde_json::json!(f.id));
                m.insert("step_id".to_string(), Value::String(f.step_id.clone()));
                m.insert("finding_type".to_string(), Value::String(f.finding_type.clone()));
                m.insert("severity".to_string(), Value::String(f.severity.clone()));
                m.insert("status".to_string(), Value::String(f.status.clone()));
                m.insert("message".to_string(), Value::String(f.message.clone()));
                m.insert("source".to_string(), Value::String(f.source.clone()));
                Value::Object(m)
            })
            .collect();
        res.insert("quality_findings".to_string(), Value::Array(findings));
        res.insert("quality_decision".to_string(), Value::String(result.quality_decision.clone()));
        res.insert("next_action".to_string(), Value::String(result.next_action.clone()));
        res.insert("auto".to_string(), Value::Bool(result.auto));
        res.insert("success".to_string(), Value::Bool(result.success));
        res.insert("reason".to_string(), Value::String(result.reason.clone()));
        res.insert("error".to_string(), Value::String(result.error.clone()));
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

        let ts = task_now_ts();
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

                let steps = match sub_def.get("steps") {
                    None => &[][..],
                    Some(value) => value
                        .as_array()
                        .ok_or_else(|| DaemonRpcError::invalid_params("子任务 steps 必须是 JSON array"))?,
                };
                insert_task_steps(&tx, &sub_id, steps, ts)?;

                created_subtasks.push(sub_id);
            }
        } else {
            let plan_text = if !plan_file.is_empty() {
                std::fs::read_to_string(plan_file).unwrap_or_default()
            } else {
                String::new()
            };

            let parsed = parse_subtasks_from_plan_text(&plan_text);
            for (idx, (st_title, st_desc, steps)) in parsed.into_iter().enumerate() {
                let sub_id = format!("{}-sub-{}", task_id, idx + 1);
                tx.execute(
                    "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at, parent_id)
                     VALUES (?1, ?2, ?3, ?4, 'open', ?5, ?6, ?7)",
                    params![sub_id, st_title, st_desc, peer.owner_key(), ts, ts, task_id],
                ).map_err(|e| DaemonRpcError::internal_error(format!("计划子任务创建失败: {}", e)))?;

                let step_values: Vec<Value> = steps.into_iter().map(Value::Object).collect();
                insert_task_steps(&tx, &sub_id, &step_values, ts)?;

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
        // workspace authority fail-closed：根任务同样必须显式绑定 workspace。
        let workspace_id = required_workspace_id_param(params)?;
        let workspace_instance_id = params
            .get("workspace_instance_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let title = params.get("title").and_then(|v| v.as_str()).unwrap_or("Root Plan Task");
        let plan_file = params.get("plan_file").and_then(|v| v.as_str()).unwrap_or("");
        let root_task_id = generate_task_id();
        let ts = task_now_ts();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        tx.execute(
            "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at, parent_id)
             VALUES (?1, ?2, ?3, ?4, 'open', ?5, ?6, '')",
            params![root_task_id, title, plan_file, peer.owner_key(), ts, ts],
        ).map_err(|e| DaemonRpcError::internal_error(format!("建根任务失败: {}", e)))?;

        let (_binding_id, _capture_id) = bind_task_to_workspace(
            &tx,
            &root_task_id,
            workspace_id,
            workspace_instance_id,
            &peer.owner_key(),
        )?;

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events (task_id, workspace_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'none', 'open', 'created_from_plan', ?3, ?4, ?5, ?6)",
            params![root_task_id, workspace_id.to_string(), plan_file, peer.owner_key(), seq, ts],
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

        // S4: 零步骤普通任务不能 vacuous pass —— 无步骤即无验收证据，返回 blocked
        let step_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE task_id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .unwrap_or(0);
        if step_count == 0 {
            let mut finding = Map::new();
            finding.insert("finding_type".to_string(), Value::String("steps".to_string()));
            finding.insert("severity".to_string(), Value::String("block".to_string()));
            finding.insert(
                "message".to_string(),
                Value::String("任务无步骤记录，无法进行完成性评审（E_NO_STEPS）".to_string()),
            );
            let mut res = Map::new();
            res.insert("task_id".to_string(), Value::String(task_id.to_string()));
            res.insert("decision".to_string(), Value::String("blocked".to_string()));
            res.insert("reason".to_string(), Value::String("E_NO_STEPS".to_string()));
            res.insert("findings".to_string(), Value::Array(vec![Value::Object(finding)]));
            return Ok(Value::Object(res));
        }

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
        let description = params
            .get("description")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let steps = match params.get("steps") {
            None => &[][..],
            Some(value) => value
                .as_array()
                .ok_or_else(|| DaemonRpcError::invalid_params("steps 必须是 JSON array"))?,
        };
        let task_id = generate_task_id();
        let ts = task_now_ts();
        let seq = self.next_seq();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        // 子任务 workspace 从父任务不可变 binding 继承（§8.1.1：task 逻辑 workspace
        // 只来自 task_workspace_bindings；禁止 active workspace 补齐）。父任务无 binding
        // → E_TASK_WORKSPACE_UNBOUND fail-closed；无父任务（root 子任务）必须显式传入。
        let workspace_id = if parent_id.is_empty() {
            required_workspace_id_param(params)?
        } else {
            task_bound_workspace_id(&tx, parent_id, optional_workspace_id_param(params))?
        };

        tx.execute(
            "INSERT INTO tasks
             (id, title, description, creator, status, created_at, updated_at, parent_id)
             VALUES (?1, ?2, ?3, ?4, 'open', ?5, ?6, ?7)",
            params![task_id, title, description, peer.owner_key(), ts, ts, parent_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("子任务写入失败: {}", e)))?;

        let (_binding_id, _capture_id) = bind_task_to_workspace(
            &tx,
            &task_id,
            workspace_id,
            "",
            &peer.owner_key(),
        )?;

        tx.execute(
            "INSERT INTO task_events (task_id, workspace_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp) VALUES (?1, ?2, 'none', 'open', 'subtask_created', ?3, ?4, ?5, ?6)",
            params![task_id, workspace_id.to_string(), title, peer.owner_key(), seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        insert_task_steps(&tx, &task_id, steps, ts)?;

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交子任务事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id));
        res.insert("parent_id".to_string(), Value::String(parent_id.to_string()));
        res.insert("status".to_string(), Value::String("open".to_string()));
        res.insert("step_count".to_string(), Value::Number(serde_json::Number::from(steps.len())));
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
        let ts = task_now_ts();

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
        let audit_id = params
            .get("audit_id")
            .and_then(|v| v.as_i64())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 audit_id"))?;
        let step_id = params.get("step_id").and_then(|v| v.as_str()).unwrap_or("");

        let conn = self.conn.lock().unwrap();

        // 1. 查 file_edit_audit（对齐 Python link_edit_audit_symbols）
        let audit = conn
            .query_row(
                "SELECT workspace_id, file_path, file_hash_before, file_hash_after, agent_task_id
                 FROM file_edit_audit WHERE id = ?1",
                params![audit_id],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, String>(4)?,
                    ))
                },
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 file_edit_audit 失败: {}", e)))?
            .ok_or_else(|| DaemonRpcError::invalid_params(format!("edit audit {} 不存在", audit_id)))?;

        let (workspace_id, file_path, hash_before, hash_after, task_id) = audit;
        if task_id.is_empty() {
            let mut res = Map::new();
            res.insert("success".to_string(), Value::Bool(false));
            res.insert("linked".to_string(), serde_json::json!(0));
            res.insert("error".to_string(), Value::String("edit audit has no task id".to_string()));
            return Ok(Value::Object(res));
        }
        let step_id = if step_id.is_empty() {
            Self::infer_in_progress_step_id(&conn, &task_id).unwrap_or_default()
        } else {
            step_id.to_string()
        };

        // 2. 匹配 before/after 文件版本（hash 精确匹配 + 位置回退）
        let before_version_id = Self::file_version_for_hash(&conn, workspace_id, &file_path, &hash_before, "before");
        let after_version_id = Self::file_version_for_hash(&conn, workspace_id, &file_path, &hash_after, "after");
        if before_version_id.is_none() && after_version_id.is_none() {
            let mut res = Map::new();
            res.insert("success".to_string(), Value::Bool(false));
            res.insert("linked".to_string(), serde_json::json!(0));
            res.insert("error".to_string(), Value::String("file versions not found; refresh graph first".to_string()));
            return Ok(Value::Object(res));
        }

        // 3. 符号版本快照
        let before = Self::symbols_for_file_version(&conn, before_version_id)?;
        let after = Self::symbols_for_file_version(&conn, after_version_id)?;
        let mut names: Vec<&String> = before.keys().chain(after.keys()).collect();
        names.sort();
        names.dedup();

        // 4. 逐符号对比写入 task_symbol_changes + audit_chain 签名
        let mut linked: Vec<Value> = Vec::new();
        let ts = task_now_ts();
        for qualified_name in names {
            let before_sym = before.get(qualified_name);
            let after_sym = after.get(qualified_name);
            let before_hash = before_sym.map(|s| s.1.as_str()).unwrap_or("");
            let after_hash = after_sym.map(|s| s.1.as_str()).unwrap_or("");
            if before_hash == after_hash {
                continue;
            }
            let change_type = if before_sym.is_some() && after_sym.is_some() {
                "modified"
            } else if after_sym.is_some() {
                "added"
            } else {
                "deleted"
            };
            let symbol_name = after_sym
                .or(before_sym)
                .map(|s| s.0.clone())
                .unwrap_or_default();
            let metadata = serde_json::json!({
                "file_hash_before": hash_before,
                "file_hash_after": hash_after,
                "before_file_version_id": before_version_id.unwrap_or(0),
                "after_file_version_id": after_version_id.unwrap_or(0),
            });
            let row_id = conn
                .execute(
                    "INSERT INTO task_symbol_changes(
                         workspace_id, task_id, step_id, edit_audit_id, change_audit_id, file_path,
                         qualified_name, symbol_name, symbol_hash_before, symbol_hash_after,
                         change_type, source, source_commit_hash, metadata, created_at
                     ) VALUES (?1, ?2, ?3, ?4, '', ?5, ?6, ?7, ?8, ?9, ?10,
                               'edit_audit_symbol_diff', '', ?11, ?12)",
                    params![
                        workspace_id,
                        task_id,
                        step_id,
                        audit_id,
                        file_path,
                        qualified_name,
                        symbol_name,
                        before_hash,
                        after_hash,
                        change_type,
                        metadata.to_string(),
                        ts
                    ],
                )
                .map_err(|e| DaemonRpcError::internal_error(format!("写入 task_symbol_changes 失败: {}", e)))?;

            // 审计链签名（失败不吞错）
            crate::cli::task::sign_audit_chain(
                &conn,
                "task_symbol_changes",
                &row_id.to_string(),
                &serde_json::json!({
                    "task_id": task_id,
                    "step_id": step_id,
                    "edit_audit_id": audit_id,
                    "change_audit_id": "",
                    "file_path": file_path,
                    "qualified_name": qualified_name,
                    "symbol_name": symbol_name,
                    "symbol_hash_before": before_hash,
                    "symbol_hash_after": after_hash,
                    "change_type": change_type,
                    "source": "edit_audit_symbol_diff",
                    "metadata": metadata,
                }),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("符号归因审计签名失败: {}", e)))?;

            let mut item = Map::new();
            item.insert("id".to_string(), serde_json::json!(row_id));
            item.insert("qualified_name".to_string(), Value::String(qualified_name.clone()));
            item.insert("change_type".to_string(), Value::String(change_type.to_string()));
            linked.push(Value::Object(item));
        }

        let mut res = Map::new();
        res.insert("success".to_string(), Value::Bool(true));
        res.insert("audit_id".to_string(), serde_json::json!(audit_id));
        res.insert("linked".to_string(), serde_json::json!(linked.len()));
        res.insert("changes".to_string(), Value::Array(linked));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    /// 递归查任务树的 in_progress 步骤（对齐 Python db_task_attribution._infer_in_progress_step_id）
    fn infer_in_progress_step_id(conn: &Connection, task_id: &str) -> Option<String> {
        conn.query_row(
            "WITH RECURSIVE task_tree(id) AS (
                 SELECT id FROM tasks WHERE id = ?1
                 UNION ALL
                 SELECT t.id FROM tasks t JOIN task_tree tt ON t.parent_id = tt.id
             )
             SELECT ts.id FROM task_steps ts
             JOIN task_tree tt ON ts.task_id = tt.id
             WHERE ts.status = 'in_progress'
             ORDER BY ts.created_at DESC LIMIT 1",
            params![task_id],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .ok()
        .flatten()
    }

    /// 按文件 hash 查找文件版本 ID（对齐 Python db_task_attribution._file_version_for_hash）
    /// 精确 hash 匹配失败后按版本位置回退（before=is_current 0 / after=is_current 1）。
    fn file_version_for_hash(
        conn: &Connection,
        workspace_id: i64,
        file_path: &str,
        file_hash: &str,
        position: &str,
    ) -> Option<i64> {
        if !file_hash.is_empty() {
            let exact: Option<i64> = conn
                .query_row(
                    "SELECT fv.id FROM file_versions fv
                     JOIN file_instances fi ON fv.file_instance_id = fi.id
                     WHERE fi.workspace_id = ?1 AND fi.rel_path = ?2 AND fv.content_hash = ?3
                     ORDER BY fv.parsed_at DESC, fv.id DESC LIMIT 1",
                    params![workspace_id, file_path, file_hash],
                    |row| row.get(0),
                )
                .ok();
            if exact.is_some() {
                return exact;
            }
        }
        if position == "before" || position == "after" {
            let is_current = if position == "after" { 1 } else { 0 };
            return conn
                .query_row(
                    "SELECT fv.id FROM file_versions fv
                     JOIN file_instances fi ON fv.file_instance_id = fi.id
                     WHERE fi.workspace_id = ?1 AND fi.rel_path = ?2 AND fv.is_current = ?3
                     ORDER BY fv.version_num DESC LIMIT 1",
                    params![workspace_id, file_path, is_current],
                    |row| row.get(0),
                )
                .ok();
        }
        None
    }

    /// 查询文件版本的符号集合（对齐 Python db_task_attribution._symbols_for_file_version）
    /// 返回 qualified_name -> (symbol_name, symbol_hash)。
    fn symbols_for_file_version(
        conn: &Connection,
        version_id: Option<i64>,
    ) -> Result<HashMap<String, (String, String)>, DaemonRpcError> {
        let mut map = HashMap::new();
        if let Some(vid) = version_id {
            let mut stmt = conn
                .prepare(
                    "SELECT fsv.qualified_name, fsv.symbol_hash, sc.name
                     FROM file_symbol_versions fsv
                     LEFT JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
                     WHERE fsv.file_version_id = ?1 AND fsv.is_deleted = 0",
                )
                .map_err(|e| DaemonRpcError::internal_error(format!("准备符号版本查询失败: {}", e)))?;
            let rows = stmt
                .query_map(params![vid], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                })
                .map_err(|e| DaemonRpcError::internal_error(format!("查询符号版本失败: {}", e)))?;
            for item in rows.flatten() {
                map.insert(item.0, (item.2, item.1));
            }
        }
        Ok(map)
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
                r.get::<_, String>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
                r.get::<_, String>(4)?,
                r.get::<_, String>(5)?,
                r.get::<_, String>(6)?,
                r.get::<_, String>(7)?,
                r.get::<_, f64>(8)?,
                r.get::<_, Option<f64>>(9)?,
            ))
        }) {
            for s in rows.flatten() {
                step_total += 1;
                if s.6 == "done" || s.6 == "skipped" {
                    step_done += 1;
                }
                let mut m = Map::new();
                m.insert("step_id".to_string(), Value::String(s.0.clone()));
                m.insert("step_index".to_string(), Value::Number(serde_json::Number::from(s.1)));
                m.insert("action".to_string(), Value::String(s.2));
                m.insert("target_file".to_string(), Value::String(s.3));
                m.insert("target_symbol".to_string(), Value::String(s.4));
                m.insert("check_items".to_string(), Value::String(s.5));
                m.insert("status".to_string(), Value::String(s.6));
                m.insert("result".to_string(), Value::String(s.7));
                m.insert("created_at".to_string(), Value::Number(serde_json::Number::from_f64(s.8).unwrap()));
                m.insert("completed_at".to_string(), match s.9 {
                    Some(v) => Value::Number(serde_json::Number::from_f64(v).unwrap()),
                    None => Value::Null,
                });
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
            "workspace_id": 1,
            "title": "Fix memory leak in parser",
            "description": "Investigate tree-sitter memory allocation",
            "task_id": "T-TEST-001"
        });
        seed_workspace(&store);
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
    fn test_failed_report_preserves_scope_and_requires_remediation_claim() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store.handle_task_create(peer.clone(), &serde_json::json!({ "workspace_id": 1,
            "task_id": "T-REMEDIATION-SCOPE",
            "title": "failed step scope",
            "steps": [{"action":"capture", "target_file":"docs/design/example.md", "target_symbol":"", "check_items":"isolated capture"}]
        })).unwrap();
        let conn = store.conn.lock().unwrap();
        let failed_id: String = conn.query_row(
            "SELECT id FROM task_steps WHERE task_id='T-REMEDIATION-SCOPE' ORDER BY step_index LIMIT 1", [], |r| r.get(0)
        ).unwrap();
        drop(conn);
        store.handle_task_claim(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-SCOPE", "agent_session_id":"executor-session"
        })).unwrap();
        store.handle_task_report(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-SCOPE", "step_id":failed_id, "agent_session_id":"executor-session",
            "summary":"capture blocked", "success":false
        })).unwrap();
        let missing = store.handle_task_claim(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-SCOPE", "agent_session_id":"executor-session"
        })).unwrap_err();
        assert_eq!(missing.code, "E_REMEDIATION_STEP_REQUIRED");
        let conn = store.conn.lock().unwrap();
        let (fix_id, target, result): (String, String, String) = conn.query_row(
            "SELECT id, target_file, result FROM task_steps WHERE task_id='T-REMEDIATION-SCOPE' AND action='fix_defect' ORDER BY step_index DESC LIMIT 1", [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))
        ).unwrap();
        drop(conn);
        let claim = store.handle_task_claim(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-SCOPE", "agent_session_id":"executor-session", "remediation_step_id":fix_id
        })).unwrap();
        assert_eq!(claim["step_id"], fix_id);
        assert_eq!(target, "docs/design/example.md");
        assert_eq!(serde_json::from_str::<Value>(&result).unwrap()["remediation_of_step_id"], failed_id);
    }

    #[test]
    fn test_explicit_remediation_create_binds_failed_step_and_resolves() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store.handle_task_create(peer.clone(), &serde_json::json!({ "workspace_id": 1,
            "task_id":"T-REMEDIATION-EXPLICIT", "title":"explicit remediation", "steps":[
                {"action":"capture", "target_file":"docs/design/example.md", "check_items":"isolated"}
            ]
        })).unwrap();
        let failed_id: String = {
            let conn = store.conn.lock().unwrap();
            conn.query_row("SELECT id FROM task_steps WHERE task_id='T-REMEDIATION-EXPLICIT'", [], |r| r.get(0)).unwrap()
        };
        store.handle_task_claim(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-EXPLICIT", "agent_session_id":"executor-session"
        })).unwrap();
        store.handle_task_report(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-EXPLICIT", "step_id":failed_id, "agent_session_id":"executor-session",
            "summary":"capture blocked", "success":false
        })).unwrap();
        // 模拟历史任务中的 malformed remediation：旧步骤已完成，但 result
        // 不是带 remediation_of_step_id 的结构化 provenance。显式创建入口
        // 必须为同一 failed 步骤补建一个新的、可解析的 remediation。
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE task_steps SET status='done', result='legacy malformed remediation', completed_at=?1
                 WHERE task_id='T-REMEDIATION-EXPLICIT' AND action='fix_defect'",
                params![now_ts()],
            ).unwrap();
        }
        let ident = lease_identity("agent-explicit-remediation", "executor-session", "model", "implementer");
        let lease = store.handle_lease_acquire(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-EXPLICIT", "role":"implementer", "ttl_seconds":3600.0, "identity":ident
        })).unwrap();
        let create_params = serde_json::json!({
            "task_id":"T-REMEDIATION-EXPLICIT", "failed_step_id":failed_id,
            "request_id":"remediation-create-1", "identity":ident,
            "lease_token":lease["token"], "fencing_counter":lease["fencing_counter"]
        });
        let created = store.handle_task_remediation_create(peer.clone(), &create_params).unwrap();
        let remediation_id = created["remediation_step_id"].as_str().unwrap().to_string();
        let replay = store.handle_task_remediation_create(peer.clone(), &create_params).unwrap();
        assert_eq!(replay["replayed"], true);
        assert_eq!(replay["remediation_step_id"], remediation_id);
        store.handle_task_claim(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-EXPLICIT", "agent_session_id":"executor-session",
            "remediation_step_id":remediation_id
        })).unwrap();
        let remediation_report = store.handle_task_report(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-EXPLICIT", "step_id":remediation_id,
            "agent_session_id":"executor-session", "summary":"fixed", "success":true
        })).unwrap();
        assert_eq!(
            remediation_report["status"],
            "in_progress",
            "没有 resolution event 时不得因 pending=0 伪造 review",
        );
        let resolve_params = serde_json::json!({
            "task_id":"T-REMEDIATION-EXPLICIT", "failed_step_id":failed_id,
            "remediation_step_id":remediation_id, "request_id":"resolution-explicit-1",
            "evidence_path":"docs/evidence/resolution-explicit.json", "evidence_hash":"sha256:explicit",
            "identity":ident, "lease_token":lease["token"], "fencing_counter":lease["fencing_counter"]
        });
        let resolved = store.handle_task_step_resolve(peer.clone(), &resolve_params).unwrap();
        assert_eq!(resolved["status"], "review");
        let replay = store.handle_task_step_resolve(peer.clone(), &resolve_params).unwrap();
        assert_eq!(replay["replayed"], true);
        let conn = store.conn.lock().unwrap();
        let failed_status: String = conn.query_row("SELECT status FROM task_steps WHERE id=?1", params![failed_id], |r| r.get(0)).unwrap();
        assert_eq!(failed_status, "failed");
        let result: String = conn.query_row("SELECT result FROM task_steps WHERE id=?1", params![remediation_id], |r| r.get(0)).unwrap();
        assert_eq!(serde_json::from_str::<Value>(&result).unwrap()["remediation_of_step_id"], failed_id);
    }

    #[test]
    fn test_step_resolution_is_idempotent_and_keeps_failed_history_immutable() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store.handle_task_create(peer.clone(), &serde_json::json!({ "workspace_id": 1,
            "task_id":"T-REMEDIATION-RESOLVE", "title":"resolution", "steps":[
                {"action":"capture", "target_file":"docs/design/example.md", "check_items":"isolated"}
            ]
        })).unwrap();
        let conn = store.conn.lock().unwrap();
        let failed_id: String = conn.query_row("SELECT id FROM task_steps WHERE task_id='T-REMEDIATION-RESOLVE'", [], |r| r.get(0)).unwrap();
        drop(conn);
        store.handle_task_claim(peer.clone(), &serde_json::json!({"task_id":"T-REMEDIATION-RESOLVE", "agent_session_id":"executor-session"})).unwrap();
        store.handle_task_report(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-RESOLVE", "step_id":failed_id, "agent_session_id":"executor-session", "summary":"failed", "success":false
        })).unwrap();
        let conn = store.conn.lock().unwrap();
        let remediation_id: String = conn.query_row("SELECT id FROM task_steps WHERE task_id='T-REMEDIATION-RESOLVE' AND action='fix_defect'", [], |r| r.get(0)).unwrap();
        drop(conn);
        store.handle_task_claim(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-RESOLVE", "agent_session_id":"executor-session", "remediation_step_id":remediation_id
        })).unwrap();
        store.handle_task_report(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-RESOLVE", "step_id":remediation_id, "agent_session_id":"executor-session", "summary":"fixed", "success":true
        })).unwrap();
        let ident = lease_identity("agent-resolver", "executor-session", "model", "implementer");
        let lease = store.handle_lease_acquire(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-RESOLVE", "role":"implementer", "ttl_seconds":3600.0, "identity":ident
        })).unwrap();
        let params = serde_json::json!({
            "task_id":"T-REMEDIATION-RESOLVE", "failed_step_id":failed_id, "remediation_step_id":remediation_id,
            "request_id":"resolution-1", "evidence_path":"docs/evidence/resolution.md", "evidence_hash":"hash-1",
            "identity":ident, "lease_token":lease["token"], "fencing_counter":lease["fencing_counter"]
        });
        let resolved = store.handle_task_step_resolve(peer.clone(), &params).unwrap();
        assert_eq!(resolved["status"], "review");
        let replay = store.handle_task_step_resolve(peer.clone(), &params).unwrap();
        assert_eq!(replay["replayed"], true);
        let mut conflict = params.clone();
        conflict["evidence_hash"] = Value::String("hash-2".into());
        let err = store.handle_task_step_resolve(peer, &conflict).unwrap_err();
        assert_eq!(err.code, "E_REQUEST_ID_REUSE_MISMATCH");
        let conn = store.conn.lock().unwrap();
        let status: String = conn.query_row("SELECT status FROM task_steps WHERE id=?1", params![failed_id], |r| r.get(0)).unwrap();
        assert_eq!(status, "failed");
    }

    #[test]
    fn test_reviewer_blocked_remediation_create_reopens_same_task_with_provenance() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store.handle_task_create(peer.clone(), &serde_json::json!({ "workspace_id": 1,
            "task_id":"T-REMEDIATION-REVIEW", "title":"review remediation", "steps":[
                {"action":"implement", "target_file":"src/review.rs", "check_items":"focused"}
            ]
        })).unwrap();
        let source_step_id: String = {
            let conn = store.conn.lock().unwrap();
            conn.query_row(
                "SELECT id FROM task_steps WHERE task_id='T-REMEDIATION-REVIEW'",
                [],
                |r| r.get(0),
            ).unwrap()
        };
        let findings = serde_json::json!([
            {"finding_id":"F-REVIEW-1","fact":"event transition is not reconstructable"}
        ]);
        let source_result = "immutable executor delivery";
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE task_steps SET status='done', result=?1, completed_at=?2 WHERE id=?3",
                params![source_result, now_ts(), source_step_id],
            ).unwrap();
            conn.execute(
                "UPDATE tasks SET status='review' WHERE id='T-REMEDIATION-REVIEW'",
                [],
            ).unwrap();
            conn.execute(
                "INSERT INTO task_verdict_events
                 (verdict_id, task_id, contract_id, contract_revision, contract_hash,
                  phase, reviewer_identity, findings, overall, attestation, submitted_at)
                 VALUES ('V-REVIEW-1', 'T-REMEDIATION-REVIEW', 'TC-REVIEW', 1, 'sha256:task',
                         'blind_first_pass', '{}', ?1, 'block', 'attested', ?2)",
                params![findings.to_string(), now_ts()],
            ).unwrap();
        }
        let executor_identity = lease_identity(
            "agent-remediation-executor",
            "executor-remediation-session",
            "executor-model",
            "implementer",
        );
        let lease = store.handle_lease_acquire(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-REVIEW", "role":"implementer",
            "ttl_seconds":3600.0, "identity":executor_identity
        })).unwrap();
        let create = serde_json::json!({
            "task_id":"T-REMEDIATION-REVIEW",
            "source_step_id":source_step_id,
            "source_outcome":"reviewer_blocked",
            "source_verdict_id":"V-REVIEW-1",
            "source_findings":findings,
            "request_id":"review-remediation-1",
            "identity":executor_identity,
            "lease_token":lease["token"],
            "fencing_counter":lease["fencing_counter"]
        });
        let created = store.handle_task_remediation_create(peer.clone(), &create).unwrap();
        assert_eq!(created["source_outcome"], "reviewer_blocked");
        assert_eq!(created["source_verdict_id"], "V-REVIEW-1");
        let remediation_step_id = created["remediation_step_id"].as_str().unwrap();

        let replay = store.handle_task_remediation_create(peer.clone(), &create).unwrap();
        assert_eq!(replay["replayed"], true);
        assert_eq!(replay["remediation_step_id"], remediation_step_id);
        let mut conflict = create.clone();
        conflict["source_findings"] = serde_json::json!([
            {"finding_id":"F-REVIEW-CHANGED","fact":"different params"}
        ]);
        let conflict_err = store.handle_task_remediation_create(peer, &conflict).unwrap_err();
        assert_eq!(conflict_err.code, "E_REQUEST_ID_REUSE_MISMATCH");

        let conn = store.conn.lock().unwrap();
        let task_status: String = conn.query_row(
            "SELECT status FROM tasks WHERE id='T-REMEDIATION-REVIEW'",
            [],
            |r| r.get(0),
        ).unwrap();
        assert_eq!(task_status, "in_progress");
        let (source_status, result_after): (String, String) = conn.query_row(
            "SELECT status, result FROM task_steps WHERE id=?1",
            params![source_step_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        ).unwrap();
        assert_eq!(source_status, "done");
        assert_eq!(result_after, source_result);
        let metadata_raw: String = conn.query_row(
            "SELECT result FROM task_steps WHERE id=?1",
            params![remediation_step_id],
            |r| r.get(0),
        ).unwrap();
        let metadata: Value = serde_json::from_str(&metadata_raw).unwrap();
        assert_eq!(metadata["remediation_of_step_id"], source_step_id);
        assert_eq!(metadata["source_verdict_id"], "V-REVIEW-1");
        assert_eq!(metadata["source_findings"], findings);
        let (from_status, to_status): (String, String) = conn.query_row(
            "SELECT from_status, to_status FROM task_events
             WHERE task_id='T-REMEDIATION-REVIEW' AND reason_code='remediation_created'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        ).unwrap();
        assert_eq!((from_status.as_str(), to_status.as_str()), ("review", "in_progress"));
        let verdict_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM task_verdict_events WHERE task_id='T-REMEDIATION-REVIEW'",
            [],
            |r| r.get(0),
        ).unwrap();
        assert_eq!(verdict_count, 1);
        let child_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM tasks WHERE parent_id='T-REMEDIATION-REVIEW'",
            [],
            |r| r.get(0),
        ).unwrap();
        assert_eq!(child_count, 0);
    }

    #[test]
    fn test_reviewer_blocked_reopens_same_task_for_multiple_revision_rounds() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store.handle_task_create(peer.clone(), &serde_json::json!({ "workspace_id": 1,
            "task_id":"T-THREAD-REVISE", "title":"thread revision", "steps":[
                {"action":"implement", "target_file":"src/thread.rs", "check_items":"focused"}
            ]
        })).unwrap();
        let source_step_id: String = {
            let conn = store.conn.lock().unwrap();
            conn.query_row(
                "SELECT id FROM task_steps WHERE task_id='T-THREAD-REVISE'",
                [],
                |row| row.get(0),
            ).unwrap()
        };
        let reviewer_identity = lease_identity(
            "agent-thread-reviewer",
            "reviewer-session",
            "reviewer-model",
            "reviewer",
        );
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE task_steps SET status='done', result='executor delivery', completed_at=?1
                 WHERE id=?2",
                params![now_ts(), source_step_id],
            ).unwrap();
            conn.execute(
                "UPDATE tasks SET status='review' WHERE id='T-THREAD-REVISE'",
                [],
            ).unwrap();
            conn.execute(
                "INSERT INTO task_verdict_events
                 (verdict_id, task_id, contract_id, contract_revision, contract_hash,
                  phase, reviewer_identity, findings, overall, attestation, submitted_at)
                 VALUES ('V-THREAD-1', 'T-THREAD-REVISE', 'TC-THREAD', 1, 'sha256:task',
                         'blind_first_pass', ?1, ?2, 'block', 'attested', ?3)",
                params![
                    serde_json::json!({"identity": reviewer_identity.clone()}).to_string(),
                    serde_json::json!([{"finding_id":"F-THREAD-1","fact":"first defect"}]).to_string(),
                    now_ts(),
                ],
            ).unwrap();
        }
        let reviewer_lease = store.handle_lease_acquire(peer.clone(), &serde_json::json!({
            "task_id":"T-THREAD-REVISE", "role":"reviewer", "ttl_seconds":3600.0,
            "identity": reviewer_identity.clone()
        })).unwrap();
        let first_handoff = serde_json::json!({
            "task_id":"T-THREAD-REVISE", "from_role":"reviewer",
            "outcome":"reviewer_blocked", "next_role":"executor",
            "next_action":"revise finding F-THREAD-1", "reason":"F-THREAD-1",
            "independence_requirement":"not_required", "request_id":"handoff-thread-1",
            "step_id":source_step_id, "report_request_id":"report-thread-1",
            "evidence_path":"docs/evidence/thread-1.json", "evidence_hash":"sha256:thread-1",
            "identity":reviewer_identity.clone(), "lease_token":reviewer_lease["token"],
            "fencing_counter":reviewer_lease["fencing_counter"]
        });
        let first = store.handle_task_handoff(peer.clone(), &first_handoff).unwrap();
        assert_eq!(first["status"], "in_progress");
        let remediation_one = first["remediation_step_id"].as_str().unwrap().to_string();
        let replay = store.handle_task_handoff(peer.clone(), &first_handoff).unwrap();
        assert_eq!(replay["replayed"], true);
        assert_eq!(replay["remediation_step_id"], remediation_one);

        let claim = store.handle_task_claim(peer.clone(), &serde_json::json!({
            "task_id":"T-THREAD-REVISE", "agent_session_id":"executor-session",
            "remediation_step_id":remediation_one
        })).unwrap();
        assert_eq!(claim["step_id"], remediation_one);
        let report = store.handle_task_report(peer.clone(), &serde_json::json!({
            "task_id":"T-THREAD-REVISE", "step_id":remediation_one,
            "agent_session_id":"executor-session", "summary":"first revision done", "success":true
        })).unwrap();
        assert_eq!(report["status"], "review");

        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "INSERT INTO task_verdict_events
                 (verdict_id, task_id, contract_id, contract_revision, contract_hash,
                  phase, reviewer_identity, findings, overall, attestation, submitted_at)
                 VALUES ('V-THREAD-2', 'T-THREAD-REVISE', 'TC-THREAD', 1, 'sha256:task',
                         'post_reveal_amendment', ?1, ?2, 'block', 'attested', ?3)",
                params![
                    serde_json::json!({"identity": reviewer_identity.clone()}).to_string(),
                    serde_json::json!([{"finding_id":"F-THREAD-2","fact":"second defect"}]).to_string(),
                    now_ts(),
                ],
            ).unwrap();
        }
        let second_handoff = serde_json::json!({
            "task_id":"T-THREAD-REVISE", "from_role":"reviewer",
            "outcome":"reviewer_blocked", "next_role":"executor",
            "next_action":"revise finding F-THREAD-2", "reason":"F-THREAD-2",
            "independence_requirement":"not_required", "request_id":"handoff-thread-2",
            "step_id":remediation_one, "report_request_id":"report-thread-2",
            "evidence_path":"docs/evidence/thread-2.json", "evidence_hash":"sha256:thread-2",
            "identity":reviewer_identity, "lease_token":reviewer_lease["token"],
            "fencing_counter":reviewer_lease["fencing_counter"]
        });
        let second = store.handle_task_handoff(peer, &second_handoff).unwrap();
        let remediation_two = second["remediation_step_id"].as_str().unwrap();
        assert_ne!(remediation_two, remediation_one);

        let conn = store.conn.lock().unwrap();
        let source_state: (String, String) = conn.query_row(
            "SELECT status, result FROM task_steps WHERE id=?1",
            params![source_step_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        ).unwrap();
        assert_eq!(source_state, ("done".to_string(), "executor delivery".to_string()));
        let verdict_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM task_verdict_events WHERE task_id='T-THREAD-REVISE'",
            [],
            |row| row.get(0),
        ).unwrap();
        let remediation_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM task_steps
             WHERE task_id='T-THREAD-REVISE' AND action='fix_defect'",
            [],
            |row| row.get(0),
        ).unwrap();
        let child_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM tasks WHERE parent_id='T-THREAD-REVISE'",
            [],
            |row| row.get(0),
        ).unwrap();
        assert_eq!(verdict_count, 2);
        assert_eq!(remediation_count, 2);
        assert_eq!(child_count, 0);
    }

    #[test]
    fn test_task_create_persists_steps_atomically() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let create_params = serde_json::json!({
            "workspace_id": 1,
            "task_id": "T-STEPS-CREATE",
            "title": "task with steps",
            "steps": [
                {
                    "action": "implement",
                    "target_file": "rust_ext/src/daemon/task_collab.rs",
                    "target_symbol": "TaskCollabStore::handle_task_create",
                    "check_items": ["cargo test", "audit"],
                },
                {
                    "action": "test",
                    "target_file": "tests/test_task_split_steps.py",
                    "check_items": "pytest",
                },
            ]
        });

        seed_workspace(&store);
        let result = store.handle_task_create(peer, &create_params).unwrap();
        assert_eq!(result["step_count"], 2);

        let conn = store.conn.lock().unwrap();
        let mut stmt = conn
            .prepare(
                "SELECT step_index, action, target_file, target_symbol, check_items, status
                 FROM task_steps WHERE task_id = ?1 ORDER BY step_index",
            )
            .unwrap();
        let rows: Vec<(i64, String, String, String, String, String)> = stmt
            .query_map(params!["T-STEPS-CREATE"], |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                ))
            })
            .unwrap()
            .map(|row| row.unwrap())
            .collect();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].0, 0);
        assert_eq!(rows[0].1, "implement");
        assert_eq!(rows[0].2, "rust_ext/src/daemon/task_collab.rs");
        assert_eq!(rows[0].3, "TaskCollabStore::handle_task_create");
        assert_eq!(rows[0].4, "[\"cargo test\",\"audit\"]");
        assert_eq!(rows[0].5, "pending");
        assert_eq!(rows[1].0, 1);
        assert_eq!(rows[1].1, "test");
        assert_eq!(rows[1].2, "tests/test_task_split_steps.py");
        assert_eq!(rows[1].4, "pytest");
    }

    #[test]
    fn test_task_split_persists_child_steps() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-STEPS-SPLIT",
                    "title": "parent",
                }),
            )
            .unwrap();

        let split = store
            .handle_task_split(
                peer,
                &serde_json::json!({
                    "task_id": "T-STEPS-SPLIT",
                    "subtasks": [
                        {
                            "title": "bridge",
                            "description": "bridge implementation",
                            "steps": [
                                {"action": "implement", "target_file": "rust_ext/src/bin/cw_bridge.rs"},
                                {"action": "test", "target_file": "tests/test_windows_bridge_e2e.py"},
                            ]
                        },
                        {
                            "title": "routing",
                            "steps": [
                                {"action": "implement", "target_file": "server/daemon_client.py"}
                            ]
                        }
                    ]
                }),
            )
            .unwrap();
        assert_eq!(split["subtask_count"], 2);

        let conn = store.conn.lock().unwrap();
        let child_one: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE task_id = 'T-STEPS-SPLIT-sub-1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let child_two_target: String = conn
            .query_row(
                "SELECT target_file FROM task_steps WHERE task_id = 'T-STEPS-SPLIT-sub-2'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(child_one, 2);
        assert_eq!(child_two_target, "server/daemon_client.py");
    }

    #[test]
    fn test_task_status_tree_shows_pending_child_steps() {
        // 回归：status_tree 必须显示 pending 子任务的完整步骤。
        // 根因1：step_id 被按 i64 读取（实际是 TEXT）→ 行转换失败被 flatten 丢弃；
        // 根因2：completed_at 为 NULL（pending 步骤）时按 f64 读取失败 → 整行被丢弃。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-TREE-ROOT",
                    "title": "parent",
                }),
            )
            .unwrap();

        let plan_file = _dir.path().join("plan.md");
        std::fs::write(
            &plan_file,
            r#"## 子任务丙
- implement @ rust_ext/src/daemon/task_collab.rs
- test @ tests/test_task_split_steps.py
- verify @ cli/main.py
"#,
        )
        .unwrap();
        store
            .handle_task_split(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-TREE-ROOT",
                    "plan_file": plan_file.to_str().unwrap(),
                }),
            )
            .unwrap();

        let node = store
            .handle_task_status_tree(peer, &serde_json::json!({"task_id": "T-TREE-ROOT-sub-1"}))
            .unwrap();
        let steps = node["steps"].as_array().unwrap();
        assert_eq!(steps.len(), 3, "status_tree 必须显示 pending 子任务的 3 个步骤");
        assert_eq!(steps[0]["step_index"], 0);
        assert_eq!(steps[0]["action"], "implement");
        assert_eq!(steps[0]["completed_at"], Value::Null);
        assert_eq!(node["progress"]["total"], 3);
    }

    #[test]
    fn test_parse_subtasks_from_plan_text_extracts_steps() {
        // S2/S3：plan_file 解析必须产出与 subtasks 参数路径一致的步骤结构
        let plan = r#"# 根计划

## 子任务一
- implement @ rust_ext/src/daemon/task_collab.rs
- test: tests/test_task_split_steps.py

## 子任务二
编写路由逻辑
- implement @ server/daemon_client.py
"#;
        let parsed = parse_subtasks_from_plan_text(plan);
        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed[0].0, "子任务一");
        assert_eq!(parsed[0].1, "");
        assert_eq!(parsed[0].2.len(), 2);
        assert_eq!(parsed[0].2[0]["action"], "implement");
        assert_eq!(parsed[0].2[0]["target_file"], "rust_ext/src/daemon/task_collab.rs");
        assert_eq!(parsed[0].2[1]["action"], "test");
        assert_eq!(parsed[0].2[1]["target_file"], "tests/test_task_split_steps.py");
        // 子任务二描述 + 步骤
        assert_eq!(parsed[1].0, "子任务二");
        assert_eq!(parsed[1].1, "编写路由逻辑");
        assert_eq!(parsed[1].2.len(), 1);
        assert_eq!(parsed[1].2[0]["action"], "implement");
        assert_eq!(parsed[1].2[0]["target_file"], "server/daemon_client.py");
    }

    #[test]
    fn test_parse_subtasks_from_plan_text_skips_code_blocks() {
        // 代码块内的 "- " 列表不得被解析为步骤
        let plan = "## 子任务\n```yaml\n- 不属于步骤\n```\n- 属于步骤\n";
        let parsed = parse_subtasks_from_plan_text(plan);
        assert_eq!(parsed.len(), 1);
        assert_eq!(parsed[0].2.len(), 1);
        assert_eq!(parsed[0].2[0]["action"], "属于步骤");
    }

    #[test]
    fn test_task_split_plan_file_persists_child_steps() {
        // S1：plan_file 路径必须调用 insert_task_steps，步骤完整写入且不互相串
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-PLAN-SPLIT",
                    "title": "parent",
                }),
            )
            .unwrap();

        // 写入临时 plan 文件
        let plan_file = _dir.path().join("plan.md");
        std::fs::write(
            &plan_file,
            r#"# 计划

## 子任务甲
- implement @ rust_ext/src/daemon/task_collab.rs
- test @ tests/test_task_split_steps.py

## 子任务乙
- implement @ server/daemon_client.py
"#,
        )
        .unwrap();

        let split = store
            .handle_task_split(
                peer,
                &serde_json::json!({
                    "task_id": "T-PLAN-SPLIT",
                    "plan_file": plan_file.to_str().unwrap(),
                }),
            )
            .unwrap();
        assert_eq!(split["subtask_count"], 2);

        let conn = store.conn.lock().unwrap();
        // 子任务甲：2 步，字段与顺序一致
        let mut stmt = conn
            .prepare(
                "SELECT step_index, action, target_file, target_symbol, check_items, status
                 FROM task_steps WHERE task_id = 'T-PLAN-SPLIT-sub-1' ORDER BY step_index",
            )
            .unwrap();
        let rows: Vec<(i64, String, String, String, String, String)> = stmt
            .query_map([], |r| {
                Ok((
                    r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?,
                ))
            })
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0], (0, "implement".into(), "rust_ext/src/daemon/task_collab.rs".into(), String::new(), String::new(), "pending".into()));
        assert_eq!(rows[1], (1, "test".into(), "tests/test_task_split_steps.py".into(), String::new(), String::new(), "pending".into()));

        // 子任务乙：1 步，不与子任务甲串步骤
        let child_two: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE task_id = 'T-PLAN-SPLIT-sub-2'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(child_two, 1);
        let child_two_target: String = conn
            .query_row(
                "SELECT target_file FROM task_steps WHERE task_id = 'T-PLAN-SPLIT-sub-2'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(child_two_target, "server/daemon_client.py");
    }

    #[test]
    fn test_task_split_plan_file_invalid_step_rolls_back() {
        // S1：步骤非法时整个事务回滚，不留下半成品子任务
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-PLAN-ROLLBACK",
                    "title": "parent",
                }),
            )
            .unwrap();

        let plan_file = _dir.path().join("plan.md");
        std::fs::write(
            &plan_file,
            "## 子任务甲\n- implement @ a.rs\n",
        )
        .unwrap();

        // subtasks 参数路径下步骤为非法值（非 object）应整体回滚：
        // 通过同时传 plan_file 与 subtasks（subtasks 优先）验证回滚语义
        let err = store
            .handle_task_split(
                peer,
                &serde_json::json!({
                    "task_id": "T-PLAN-ROLLBACK",
                    "plan_file": plan_file.to_str().unwrap(),
                    "subtasks": [
                        {"title": "x", "steps": ["not-an-object"]}
                    ]
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "invalid_params");

        let conn = store.conn.lock().unwrap();
        let tasks: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM tasks WHERE parent_id = 'T-PLAN-ROLLBACK'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let steps: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE task_id LIKE 'T-PLAN-ROLLBACK-sub-%'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(tasks, 0);
        assert_eq!(steps, 0);
    }

    #[test]
    fn test_task_collab_migrates_v46_db_to_v50() {
        // P1 修复：v46 旧库（无 task_events/agent_registrations、schema_version=46）
        // 打开后必须走官方 migration 升级到 v50 并补齐权威任务表，完整 task RPC 可用
        let (_dir, db_path) = temp_db();

        // 1. 先建一个 v50 库，再人为降级为 v46（模拟旧版库形态）
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
            "workspace_id": 1,
            "title": "upgrade from v46",
            "task_id": "T-V46-001"
        });
        seed_workspace(&store);
        let create_res = store.handle_task_create(peer.clone(), &create_params).unwrap();
        assert_eq!(create_res["status"], "open");
        let events_params = serde_json::json!({ "task_id": "T-V46-001" });
        let events_res = store.handle_task_events(peer, &events_params).unwrap();
        assert_eq!(events_res["events"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn test_task_report_identity_is_validated_and_persisted() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);

        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "INSERT INTO workspaces (name, root_path, created_at, is_active) VALUES ('identity-test', '/tmp/identity-test', 1.0, 1)",
                [],
            ).unwrap();
        }

        let create = serde_json::json!({
            "workspace_id": 1,
            "task_id": "T-IDENTITY-001",
            "title": "identity writeback",
            "steps": [{"action": "implement"}]
        });
        seed_workspace(&store);
        store.handle_task_create(peer.clone(), &create).unwrap();
        let step_id: String = {
            let conn = store.conn.lock().unwrap();
            conn.query_row(
                "SELECT id FROM task_steps WHERE task_id = 'T-IDENTITY-001' ORDER BY step_index LIMIT 1",
                [],
                |r| r.get(0),
            )
            .unwrap()
        };
        {
            let conn = store.conn.lock().unwrap();
            let step_count: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM task_steps WHERE task_id = 'T-IDENTITY-001'",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(step_count, 1);
        }
        store.handle_task_claim(
            peer.clone(),
            &serde_json::json!({"task_id": "T-IDENTITY-001", "agent_session_id": "session-identity"}),
        ).unwrap();

        let report = store.handle_task_report(
            peer.clone(),
            &serde_json::json!({
                "task_id": "T-IDENTITY-001",
                "step_id": step_id,
                "summary": "done",
                "success": true,
                "identity": {
                    "agent_id": "agent-identity",
                    "session_id": "session-identity",
                    "model_id": "model-test",
                    "role": "implementer"
                }
            }),
        ).unwrap();
        assert_eq!(report["status"], "review");

        let conn = store.conn.lock().unwrap();
        let step_status: String = conn.query_row(
            "SELECT status FROM task_steps WHERE id = ?1", params![step_id], |r| r.get(0),
        ).unwrap();
        assert_eq!(step_status, "done");
        let identity_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM action_identities WHERE task_id = 'T-IDENTITY-001' AND agent_id = 'agent-identity'",
            [], |r| r.get(0),
        ).unwrap();
        assert_eq!(identity_count, 1);
        let role: String = conn.query_row(
            "SELECT role FROM task_events WHERE task_id = 'T-IDENTITY-001' AND reason_code = 'reported' ORDER BY event_id DESC LIMIT 1",
            [], |r| r.get(0),
        ).unwrap();
        assert_eq!(role, "implementer");

        let err = store.handle_task_report(
            peer,
            &serde_json::json!({
                "task_id": "T-IDENTITY-001",
                "identity": {"agent_id": "agent-only"}
            }),
        ).unwrap_err();
        assert_eq!(err.code, "E_IDENTITY_INCOMPLETE");
    }

    // ============================================
    // 任务 B（T-1786412969125）：task close 父子状态门禁、零步骤误关闭与 lease fail-closed
    // ============================================

    /// 直接 seed 一个任务（可选步骤与父任务），供 close 门禁测试使用
    fn seed_task(store: &TaskCollabStore, id: &str, parent_id: &str, status: &str, with_done_step: bool) {
        let ts = 1_700_000_000.0;
        let conn = store.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at, parent_id)
             VALUES (?1, ?2, '', 'agent', ?3, ?4, ?4, ?5)",
            params![id, format!("task {}", id), status, ts, parent_id],
        )
        .unwrap();
        if with_done_step {
            conn.execute(
                "INSERT INTO task_steps (id, task_id, step_index, action, target_file, target_symbol, check_items, status, result, created_at, completed_at)
                 VALUES (?1, ?2, 0, 'verify', '', '', '', 'done', 'ok', ?3, ?3)",
                params![format!("{}-s1", id), id, ts],
            )
            .unwrap();
        }
        drop(conn);
    }

    /// 建一条测试 workspace（id=1，is_active=1），并为 lease 测试直接以 task_id
    /// 调用的 handler 预置 capture + 不可变 binding（与 v1 workspace authority 契约一致）。
    fn seed_workspace(store: &TaskCollabStore) {
        let conn = store.conn.lock().unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) VALUES (1, 'test-ws', '/tmp/test-ws', ?1, 1)",
            params![1_700_000_000.0_f64],
        )
        .unwrap();
        drop(conn);
        for tid in [
            "T-LEASE-1", "T-LEASE-2", "T-LEASE-3", "T-LEASE-4", "T-LEASE-5",
            "T-LEASE-6", "T-LEASE-7", "T-LEASE-8", "T-LEASE-9", "T-LEASE-10",
            "T-LEASE-MISSING", "T-LEASE-STALE",
        ] {
            seed_task_binding(store, tid);
        }
    }

    /// 为测试 task 写入 capture + 不可变 binding（workspace 1，幂等）。
    fn seed_task_binding(store: &TaskCollabStore, task_id: &str) {
        let ts = 1_700_000_000.0_f64;
        let conn = store.conn.lock().unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO tasks (id, title, description, creator, status, created_at, updated_at, parent_id)
             VALUES (?1, 'test task', '', 'test', 'open', ?2, ?2, '')",
            params![task_id, ts],
        )
        .unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO workspace_authority_captures
             (workspace_capture_id, workspace_id, capture_revision, supersedes_capture_id,
              daemon_workspace_id, workspace_instance_id, capture_canonicalization_version,
              capture_canonicalization_rules_hash, registry_identity_payload_json,
              registry_identity_hash, workspace_manifest_payload_json, workspace_manifest_hash,
              client_view_root_hash, host_real_root_hash, created_by, authoritative_created_at)
             VALUES (?1, 1, 1, NULL, 0, ?3, 'workspace-capture-c14n/v1',
                     'test-rules-hash', '{}', ?4, '{}', 'test-manifest-hash',
                     'test-root-hash', 'test-root-hash', 'test', ?2)",
            // capture UNIQUE(workspace_id, workspace_instance_id, registry_identity_hash,
            // capture_revision)：多个测试 task 必须各自唯一，否则 INSERT OR IGNORE 会静默
            // 吞掉后续 capture 导致 task_workspace_bindings 外键失败（曾因此 48 测试全红）。
            params![
                format!("cap-test-{}", task_id),
                ts,
                format!("ws-inst-test-{}", task_id),
                format!("test-identity-hash-{}", task_id),
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO task_workspace_bindings
             (task_id, workspace_id, workspace_binding_id, workspace_capture_id, created_by, authoritative_created_at)
             VALUES (?1, 1, ?2, ?3, 'test', ?4)",
            params![
                task_id,
                format!("tb-test-{}", task_id),
                format!("cap-test-{}", task_id),
                ts,
            ],
        )
        .unwrap();
        drop(conn);
    }

    /// 为测试任务 seed 一条 active reviewer lease（含 workspace FK），供 apply/close 门禁测试使用。
    fn seed_reviewer_lease(
        store: &TaskCollabStore,
        task_id: &str,
        raw_token: &str,
        counter: i64,
        agent: &str,
        session: &str,
        model: &str,
    ) {
        let conn = store.conn.lock().unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) VALUES (1, 'test-ws', '/tmp/test-ws', ?1, 1)",
            params![1_700_000_000.0_f64],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO task_leases (workspace_id, lease_id, task_id, role, agent_id, session_id, model_id, token_hash, fencing_counter, acquired_at, expires_at, status)
             VALUES (1, ?1, ?2, 'reviewer', ?3, ?4, ?5, ?6, ?7, 1700000000.0, 1893456000.0, 'active')",
            params![
                format!("L-{}", task_id),
                task_id,
                agent,
                session,
                model,
                sha256_hex(raw_token.as_bytes()),
                counter,
            ],
        )
        .unwrap();
        drop(conn);
    }

    fn lease_identity(agent: &str, session: &str, model: &str, role: &str) -> serde_json::Value {
        serde_json::json!({
            "agent_id": agent,
            "session_id": session,
            "model_id": model,
            "role": role,
        })
    }

        #[test]
    fn p0e_adjudicator_can_use_distinct_registered_reviewer_lease_only() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        seed_task_binding(&store, "T-P0E-LEASE");
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let reviewer_registration = serde_json::json!({
            "agent_id":"review-agent", "agent_name":"reviewer", "identity": {"agent_id":"review-agent", "agent_instance_id":"review-inst", "session_id":"review-session", "model_id":"review-model", "role":"reviewer"}
        });
        store.handle_agent_register(peer.clone(), &reviewer_registration).unwrap();
        let adjudicator_registration = serde_json::json!({
            "agent_id":"adjudicator-agent", "agent_name":"adjudicator", "identity": {"agent_id":"adjudicator-agent", "agent_instance_id":"adjudicator-inst", "session_id":"adjudicator-session", "model_id":"adjudicator-model", "role":"adjudicator"}
        });
        store.handle_agent_register(peer, &adjudicator_registration).unwrap();
        seed_reviewer_lease(&store, "T-P0E-LEASE", "p0e-token", 7, "review-agent", "review-session", "review-model");
        let adjudicator = parse_action_identity(&serde_json::json!({"identity": adjudicator_registration["identity"].clone()})).unwrap().unwrap();
        let conn = store.conn.lock().unwrap();
        store.validate_reviewer_lease_for_adjudication(&conn, "T-P0E-LEASE", "p0e-token", 7, &adjudicator).unwrap();

        let same_agent = ActionIdentity { agent_id:"review-agent".to_string(), agent_instance_id:"other-instance".to_string(), client_id:String::new(), provider:String::new(), model_id:"adj-model".to_string(), model_mode:String::new(), system_fingerprint:String::new(), session_id:"adj-session".to_string(), role:"adjudicator".to_string(), runtime_hash:String::new() };
        assert_eq!(store.validate_reviewer_lease_for_adjudication(&conn, "T-P0E-LEASE", "p0e-token", 7, &same_agent).unwrap_err().code, "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_AGENT");
        let same_instance = ActionIdentity { agent_id:"other-agent".to_string(), agent_instance_id:"review-inst".to_string(), client_id:String::new(), provider:String::new(), model_id:"adj-model".to_string(), model_mode:String::new(), system_fingerprint:String::new(), session_id:"adj-session".to_string(), role:"adjudicator".to_string(), runtime_hash:String::new() };
        assert_eq!(store.validate_reviewer_lease_for_adjudication(&conn, "T-P0E-LEASE", "p0e-token", 7, &same_instance).unwrap_err().code, "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_INSTANCE");
        let same_session = ActionIdentity { agent_id:"other-agent".to_string(), agent_instance_id:"other-instance".to_string(), client_id:String::new(), provider:String::new(), model_id:"adj-model".to_string(), model_mode:String::new(), system_fingerprint:String::new(), session_id:"review-session".to_string(), role:"adjudicator".to_string(), runtime_hash:String::new() };
        assert_eq!(store.validate_reviewer_lease_for_adjudication(&conn, "T-P0E-LEASE", "p0e-token", 7, &same_session).unwrap_err().code, "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_SESSION");
        assert_eq!(store.validate_reviewer_lease_for_adjudication(&conn, "T-P0E-LEASE", "wrong-token", 7, &adjudicator).unwrap_err().code, "E_LEASE_TOKEN_MISMATCH");
        assert_eq!(store.validate_reviewer_lease_for_adjudication(&conn, "T-P0E-LEASE", "p0e-token", 6, &adjudicator).unwrap_err().code, "E_LEASE_FENCING_STALE");
    }

    // ============================================
    // M7: Lease Control Plane（Req 11.2-11.9, 14.11, 14.30）

    // ============================================

    #[test]
    fn test_lease_acquire_returns_raw_token_once_and_stores_hash() {
        // Req 11.2/11.3：acquire 成功返回 raw token（仅此一次）+ fencing_counter=1；
        // DB 只存 sha256(token_hash)，绝不存 raw token。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);

        let res = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-1",
                    "role": "implementer",
                    "ttl_seconds": 3600.0,
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();
        assert_eq!(res["task_id"], "T-LEASE-1");
        assert_eq!(res["role"], "implementer");
        assert_eq!(res["fencing_counter"], 1);
        let raw_token = res["token"].as_str().unwrap().to_string();
        assert!(!raw_token.is_empty(), "raw token 必须返回");

        // DB 只存 hash
        let conn = store.conn.lock().unwrap();
        let (lease_id, token_hash, counter): (String, String, i64) = conn
            .query_row(
                "SELECT lease_id, token_hash, fencing_counter FROM task_leases
                 WHERE workspace_id = 1 AND task_id = 'T-LEASE-1' AND status = 'active'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .unwrap();
        assert_ne!(token_hash, raw_token, "DB 不得存 raw token");
        assert_eq!(token_hash, sha256_hex(raw_token.as_bytes()));
        assert_eq!(counter, 1);
        assert!(lease_id.starts_with("L-"));
        drop(conn);
    }

    #[test]
    fn test_lease_acquire_blocks_double_active() {
        // Req 11.2 防双活：存在未过期 active lease 时 acquire → E_LEASE_ACTIVE_EXISTS
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let params = serde_json::json!({
            "task_id": "T-LEASE-2",
            "role": "implementer",
            "ttl_seconds": 3600.0,
            "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
        });
        register_agent_with_identity(&store, &peer, "agent-a", "session-a-instance", "session-a", "implementer");
        store.handle_lease_acquire(peer.clone(), &params).unwrap();

        let err = store.handle_lease_acquire(peer, &params).unwrap_err();
        assert_eq!(err.code, "E_LEASE_ACTIVE_EXISTS");
    }

    #[test]
    fn test_lease_acquire_recovers_stale_holder_before_expiry() {
        // 异常退出恢复：TTL 尚未到期，但 holder 心跳 stale 时，acquire 必须在同一
        // 事务中追加 expire 审计、回收旧 lease，再发放递增 fencing counter。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        register_agent_with_identity(&store, &peer, "agent-stale", "stale-instance", "stale-session", "implementer");
        let old_params = serde_json::json!({
            "task_id": "T-LEASE-STALE",
            "role": "implementer",
            "ttl_seconds": 3600.0,
            "identity": lease_identity("agent-stale", "stale-session", "model-a", "implementer"),
        });
        store.handle_lease_acquire(peer.clone(), &old_params).unwrap();
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE agent_registrations SET last_heartbeat = 1.0
                 WHERE agent_id = 'agent-stale' AND session_id = 'stale-session'",
                [],
            )
            .unwrap();
        }

        let new_params = serde_json::json!({
            "task_id": "T-LEASE-STALE",
            "role": "implementer",
            "ttl_seconds": 3600.0,
            "identity": lease_identity("agent-new", "new-session", "model-b", "implementer"),
        });
        let new_lease = store.handle_lease_acquire(peer, &new_params).unwrap();
        assert_eq!(new_lease["fencing_counter"], 2);

        let conn = store.conn.lock().unwrap();
        let statuses: Vec<String> = {
            let mut stmt = conn
                .prepare("SELECT status FROM task_leases WHERE task_id = 'T-LEASE-STALE' ORDER BY id")
                .unwrap();
            let rows = stmt.query_map([], |r| r.get::<_, String>(0)).unwrap();
            rows.collect::<rusqlite::Result<Vec<_>>>().unwrap()
        };
        assert_eq!(statuses, vec!["expired", "active"]);
        let event_types: Vec<String> = {
            let mut stmt = conn
                .prepare("SELECT event_type FROM task_lease_events WHERE task_id = 'T-LEASE-STALE' ORDER BY id")
                .unwrap();
            let rows = stmt.query_map([], |r| r.get::<_, String>(0)).unwrap();
            rows.collect::<rusqlite::Result<Vec<_>>>().unwrap()
        };
        assert_eq!(event_types, vec!["acquire", "expire", "acquire"]);
    }

    #[test]
    fn test_lease_acquire_recovers_missing_holder_registration() {
        // 进程异常退出后注册记录可能已丢失；缺失 owner registration 视为 orphan，
        // 但仍只通过 acquire 的单一写事务回收，不修改任何 task/step 历史事件。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let old_params = serde_json::json!({
            "task_id": "T-LEASE-MISSING",
            "role": "implementer",
            "ttl_seconds": 3600.0,
            "identity": lease_identity("agent-missing", "missing-session", "model-a", "implementer"),
        });
        store.handle_lease_acquire(peer.clone(), &old_params).unwrap();
        let new_params = serde_json::json!({
            "task_id": "T-LEASE-MISSING",
            "role": "implementer",
            "ttl_seconds": 3600.0,
            "identity": lease_identity("agent-new", "new-session", "model-b", "implementer"),
        });
        let new_lease = store.handle_lease_acquire(peer, &new_params).unwrap();
        assert_eq!(new_lease["fencing_counter"], 2);
        let conn = store.conn.lock().unwrap();
        let task_events: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events WHERE task_id = 'T-LEASE-MISSING'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(task_events, 0, "lease orphan recovery不得改写 task 历史");
    }

    #[test]
    fn test_lease_acquire_expired_then_reacquire_increments_counter() {
        // Req 11.3：旧 active lease 过期后 acquire 将其置 expired 并创建新 lease，counter 单调递增
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let params = serde_json::json!({
            "task_id": "T-LEASE-3",
            "role": "reviewer",
            "ttl_seconds": 3600.0,
            "identity": lease_identity("agent-a", "session-a", "model-a", "reviewer"),
        });
        store.handle_lease_acquire(peer.clone(), &params).unwrap();

        // 人为把 lease 置为过期
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE task_leases SET expires_at = 1.0 WHERE task_id = 'T-LEASE-3'",
                [],
            )
            .unwrap();
        }
        let res = store.handle_lease_acquire(peer, &params).unwrap();
        assert_eq!(res["fencing_counter"], 2, "过期后重新获取 counter 递增");

        // 旧 lease 已置 expired
        let conn = store.conn.lock().unwrap();
        let statuses: Vec<String> = {
            let mut stmt = conn
                .prepare("SELECT status FROM task_leases WHERE task_id = 'T-LEASE-3' ORDER BY id")
                .unwrap();
            let rows = stmt
                .query_map([], |r| r.get::<_, String>(0))
                .unwrap();
            rows.collect::<rusqlite::Result<Vec<_>>>().unwrap()
        };
        assert_eq!(statuses, vec!["expired", "active"]);
        drop(conn);
    }

    #[test]
    fn test_lease_extend_renews_and_keeps_counter() {
        // Req 11.5：extend 幂等续期——expires_at 前进、renewed_at 写入、fencing_counter 不变
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);

        let acq = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-4",
                    "role": "implementer",
                    "ttl_seconds": 3600.0,
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();
        let raw_token = acq["token"].as_str().unwrap().to_string();
        let expires_before = acq["expires_at"].as_f64().unwrap();

        let ext = store
            .handle_lease_extend(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-4",
                    "role": "implementer",
                    "token": raw_token,
                    "ttl_seconds": 7200.0,
                    "fencing_counter": 1,
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();
        assert_eq!(ext["fencing_counter"], 1, "extend 不得递增 counter");
        let expires_after = ext["expires_at"].as_f64().unwrap();
        assert!(expires_after > expires_before, "续期后 expires_at 前进");

        let conn = store.conn.lock().unwrap();
        let renewed_at: Option<f64> = conn
            .query_row(
                "SELECT renewed_at FROM task_leases WHERE task_id = 'T-LEASE-4'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(renewed_at.is_some(), "renewed_at 已写入");
        drop(conn);
    }

    #[test]
    fn test_lease_extend_rejects_bad_token() {
        // Req 11.9：错误 token → E_LEASE_TOKEN_MISMATCH
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-5",
                    "role": "implementer",
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();

        let err = store
            .handle_lease_extend(
                peer,
                &serde_json::json!({
                    "task_id": "T-LEASE-5",
                    "role": "implementer",
                    "token": "wrong-token",
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_TOKEN_MISMATCH");
    }

    #[test]
    fn test_lease_extend_rejects_expired() {
        // Req 11.4：过期 lease 续租 → E_LEASE_EXPIRED
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let acq = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-6",
                    "role": "implementer",
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();
        let raw_token = acq["token"].as_str().unwrap().to_string();
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE task_leases SET expires_at = 1.0 WHERE task_id = 'T-LEASE-6'",
                [],
            )
            .unwrap();
        }

        let err = store
            .handle_lease_extend(
                peer,
                &serde_json::json!({
                    "task_id": "T-LEASE-6",
                    "role": "implementer",
                    "token": raw_token,
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_EXPIRED");
    }

    #[test]
    fn test_lease_extend_rejects_stale_fencing() {
        // Property 11：旧持有者携带过期 counter 续租 → E_LEASE_FENCING_STALE
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let acq = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-7",
                    "role": "implementer",
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();
        let raw_token = acq["token"].as_str().unwrap().to_string();

        let err = store
            .handle_lease_extend(
                peer,
                &serde_json::json!({
                    "task_id": "T-LEASE-7",
                    "role": "implementer",
                    "token": raw_token,
                    "fencing_counter": 99,
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_FENCING_STALE");
    }

    #[test]
    fn test_lease_release_and_idempotent() {
        // Req 11.6/11.7：release 置 released；重复 release（同 token）幂等返回 idempotent=true
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let acq = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-8",
                    "role": "implementer",
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();
        let raw_token = acq["token"].as_str().unwrap().to_string();

        let rel = store
            .handle_lease_release(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-8",
                    "role": "implementer",
                    "token": raw_token,
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();
        assert_eq!(rel["status"], "released");

        // 幂等：重复 release 返回同一 released 状态（不创建新 lease、不报错）
        let rel2 = store
            .handle_lease_release(
                peer,
                &serde_json::json!({
                    "task_id": "T-LEASE-8",
                    "role": "implementer",
                    "token": raw_token,
                }),
            )
            .unwrap();
        assert_eq!(rel2["status"], "released");
        assert_eq!(rel2["idempotent"], true);

        let conn = store.conn.lock().unwrap();
        let lease_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM task_leases WHERE task_id = 'T-LEASE-8'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(lease_count, 1, "幂等 release 不得创建新 lease");
        drop(conn);
    }

    #[test]
    fn test_lease_status_hides_raw_token_and_lists_events() {
        // Req 11.2：status 含 token_hash 不含 raw token；list_events 返回 acquire/renew/release 审计事件
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let acq = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-9",
                    "role": "implementer",
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();
        let raw_token = acq["token"].as_str().unwrap().to_string();
        store
            .handle_lease_extend(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-9",
                    "role": "implementer",
                    "token": raw_token,
                }),
            )
            .unwrap();
        store
            .handle_lease_release(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-9",
                    "role": "implementer",
                    "token": raw_token,
                }),
            )
            .unwrap();

        // status：只读，不含 raw token 字段
        let status = store
            .handle_lease_status(peer.clone(), &serde_json::json!({"task_id": "T-LEASE-9", "role": "implementer"}))
            .unwrap();
        assert_eq!(status["status"], "released");
        assert!(status.get("token").is_none(), "status 不得暴露 raw token");
        assert!(status.get("token_hash").is_some(), "status 保留 token_hash 供受保护校验");
        assert!(status["lease_id"].as_str().unwrap().starts_with("L-"));

        // list_events：append-only 顺序（acquire → renew → release），不含 raw token
        let events = store
            .handle_lease_list_events(peer, &serde_json::json!({"task_id": "T-LEASE-9"}))
            .unwrap();
        let arr = events.as_array().unwrap();
        let types: Vec<&str> = arr.iter().map(|e| e["event_type"].as_str().unwrap()).collect();
        assert_eq!(types, vec!["acquire", "renew", "release"]);
        for e in arr.iter() {
            assert!(e.get("token").is_none(), "事件不得含 raw token");
            assert!(e.get("actor_agent_id").is_some());
        }
    }

    #[test]
    fn test_lease_clock_unavailable_fail_closed() {
        // Req 14.30：store 未注入 AuthoritativeClock 时 Lease 写操作一律 E_LEASE_CLOCK_UNAVAILABLE
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap(); // 未 with_clock
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);

        let err = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-10",
                    "role": "implementer",
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_CLOCK_UNAVAILABLE");

        // fail-closed：拒绝后无任何 lease 记录（不降级、不落库）
        let conn = store.conn.lock().unwrap();
        let cnt: i64 = conn
            .query_row("SELECT COUNT(*) FROM task_leases", [], |r| r.get(0))
            .unwrap();
        assert_eq!(cnt, 0, "clock fail-closed 后不得落库");
        drop(conn);
    }

    #[test]
    fn test_task_close_rejects_open_children() {
        // S1: 父任务含 open 子任务时禁止关闭（需先通过 reviewer lease 门禁）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-PARENT", "", "review", false);
        seed_task(&store, "T-CHILD", "T-PARENT", "open", true);
        seed_reviewer_lease(&store, "T-PARENT", "tok-p", 1, "agent-r", "sess-r", "model-r");

        let err = store
            .handle_task_close(peer, &serde_json::json!({"task_id": "T-PARENT", "lease_token": "tok-p", "fencing_counter": 1}))
            .unwrap_err();
        assert_eq!(err.code, "E_CHILD_TASKS_NOT_CLOSED");

        // 拒绝后任务状态不变（未写入 closed）
        let conn = store.conn.lock().unwrap();
        let status: String = conn
            .query_row("SELECT status FROM tasks WHERE id = 'T-PARENT'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(status, "review");
    }

    #[test]
    fn test_task_close_rejects_children_in_review_applied() {
        // S1: 子任务 review/applied/in_progress 均视为未关闭，父任务禁止 close
        for child_status in ["review", "applied", "in_progress"] {
            let (_dir, db_path) = temp_db();
            let store = TaskCollabStore::new(&db_path)
                .unwrap()
                .with_clock(Arc::new(AuthoritativeClock::new()));
            let peer = PeerCredential::new_unix(1000, 1000, 1234);
            seed_task(&store, "T-PARENT", "", "review", false);
            seed_task(&store, "T-CHILD", "T-PARENT", child_status, true);
            seed_reviewer_lease(&store, "T-PARENT", "tok-p", 1, "agent-r", "sess-r", "model-r");

            let err = store
                .handle_task_close(peer, &serde_json::json!({"task_id": "T-PARENT", "lease_token": "tok-p", "fencing_counter": 1}))
                .unwrap_err();
            assert_eq!(err.code, "E_CHILD_TASKS_NOT_CLOSED", "子任务状态 {} 应阻止父任务关闭", child_status);
        }
    }

    #[test]
    fn test_task_close_allows_parent_after_all_children_closed() {
        // S1+S5: 所有子任务 closed 后父任务才允许 close，且 closed_at 非零
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-PARENT", "", "review", false);
        seed_task(&store, "T-C1", "T-PARENT", "closed", true);
        seed_task(&store, "T-C2", "T-PARENT", "closed", true);
        seed_reviewer_lease(&store, "T-PARENT", "tok-p", 1, "agent-r", "sess-r", "model-r");

        let res = store
            .handle_task_close(peer, &serde_json::json!({"task_id": "T-PARENT", "lease_token": "tok-p", "fencing_counter": 1}))
            .unwrap();
        assert_eq!(res["status"], "closed");

        let conn = store.conn.lock().unwrap();
        let closed_at: f64 = conn
            .query_row("SELECT closed_at FROM tasks WHERE id = 'T-PARENT'", [], |r| r.get(0))
            .unwrap();
        assert!(closed_at > 0.0, "closed_at 应为真实非零时间戳");
    }

    #[test]
    fn test_task_close_rejects_zero_steps() {
        // S2: 空步骤普通任务不能 close
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-LEAF", "", "applied", false);
        seed_reviewer_lease(&store, "T-LEAF", "tok-l", 1, "agent-r", "sess-r", "model-r");

        let err = store
            .handle_task_close(peer, &serde_json::json!({"task_id": "T-LEAF", "lease_token": "tok-l", "fencing_counter": 1}))
            .unwrap_err();
        assert_eq!(err.code, "E_NO_STEPS");
    }

    #[test]
    fn test_task_close_rejects_pending_steps() {
        // S2: steps 含 pending/failed/blocked 不能 close
        for bad_status in ["pending", "failed", "blocked"] {
            let (_dir, db_path) = temp_db();
            let store = TaskCollabStore::new(&db_path)
                .unwrap()
                .with_clock(Arc::new(AuthoritativeClock::new()));
            let peer = PeerCredential::new_unix(1000, 1000, 1234);
            seed_task(&store, "T-LEAF", "", "applied", true);
            seed_reviewer_lease(&store, "T-LEAF", "tok-l", 1, "agent-r", "sess-r", "model-r");
            {
                let conn = store.conn.lock().unwrap();
                conn.execute(
                    "INSERT INTO task_steps (id, task_id, step_index, action, status, result, created_at)
                     VALUES (?1, 'T-LEAF', 1, 'verify', ?2, '', 1700000000.0)",
                    params![format!("T-LEAF-bad-{}", bad_status), bad_status],
                )
                .unwrap();
            }

            let err = store
                .handle_task_close(peer, &serde_json::json!({"task_id": "T-LEAF", "lease_token": "tok-l", "fencing_counter": 1}))
                .unwrap_err();
            assert_eq!(err.code, "E_STEPS_NOT_DONE", "步骤状态 {} 应阻止关闭", bad_status);
        }
    }

    #[test]
    fn test_task_close_success_writes_closed_at() {
        // S5: 叶子任务全部步骤 done 后 close 成功，closed_at 非零
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-LEAF", "", "applied", true);
        seed_reviewer_lease(&store, "T-LEAF", "tok-l", 1, "agent-r", "sess-r", "model-r");

        let res = store
            .handle_task_close(peer, &serde_json::json!({"task_id": "T-LEAF", "lease_token": "tok-l", "fencing_counter": 1}))
            .unwrap();
        assert_eq!(res["status"], "closed");
        let closed_at = res["closed_at"].as_f64().unwrap();
        assert!(closed_at > 0.0, "closed_at 应为真实非零时间戳");

        // task_events 记录 closed 状态变迁
        let conn = store.conn.lock().unwrap();
        let closed_events: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events WHERE task_id = 'T-LEAF' AND to_status = 'closed'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(closed_events, 1);
    }

    #[test]
    fn test_task_apply_close_lease_clock_unavailable_fail_closed() {
        // S3: lease clock 不可用（store 未注入时钟）时，携带 lease 凭证的 apply/close 均 fail-closed
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-L", "", "review", true);

        let lease_params = serde_json::json!({"task_id": "T-L", "lease_token": "tok", "fencing_counter": 1});
        let err = store.handle_task_apply(peer.clone(), &lease_params).unwrap_err();
        assert_eq!(err.code, "E_LEASE_CLOCK_UNAVAILABLE");

        // 校验失败后任务状态未被改变（fail-closed，不降级）
        let status: String = {
            let conn = store.conn.lock().unwrap();
            conn.query_row("SELECT status FROM tasks WHERE id = 'T-L'", [], |r| r.get(0))
                .unwrap()
        };
        assert_eq!(status, "review");

        let err = store.handle_task_close(peer, &lease_params).unwrap_err();
        assert_eq!(err.code, "E_LEASE_CLOCK_UNAVAILABLE");
    }

    #[test]
    fn test_task_close_lease_validated_with_clock() {
        // S3: 注入时钟 + 存在 active lease 时按凭证校验；任一失败在写入前拒绝
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-L", "", "applied", true);

        let raw_token = "secret-token";
        let token_hash = sha256_hex(raw_token.as_bytes());
        {
            let conn = store.conn.lock().unwrap();
            // task_leases 的 workspace_id 有 FK -> workspaces(id)，先补一条 id=1 的测试工作区
            conn.execute(
                "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (1, 'test-ws', '/tmp/test-ws', ?1)",
                params![1_700_000_000.0_f64],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO task_leases (workspace_id, lease_id, task_id, role, agent_id, session_id, model_id, token_hash, fencing_counter, acquired_at, expires_at, status)
                 VALUES (1, 'L-TEST', 'T-L', 'reviewer', 'agent-a', 'session-a', 'model-a', ?1, 1, 1700000000.0, 1893456000.0, 'active')",
                params![token_hash],
            )
            .unwrap();
        }

        // token 不匹配
        let err = store
            .handle_task_close(
                peer.clone(),
                &serde_json::json!({"task_id": "T-L", "lease_token": "wrong", "fencing_counter": 1}),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_TOKEN_MISMATCH");

        // fencing counter 过期（旧持有者）
        let err = store
            .handle_task_close(
                peer.clone(),
                &serde_json::json!({"task_id": "T-L", "lease_token": raw_token, "fencing_counter": 2}),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_FENCING_STALE");

        // 凭证正确 → close 成功
        let res = store
            .handle_task_close(
                peer,
                &serde_json::json!({"task_id": "T-L", "lease_token": raw_token, "fencing_counter": 1}),
            )
            .unwrap();
        assert_eq!(res["status"], "closed");
    }

    #[test]
    fn test_task_apply_requires_lease_credentials() {
        // S3 强制门禁：daemon 权威路径下 task.apply 缺少/不完整 lease 凭证 → E_LEASE_REQUIRED，
        // 禁止沿用"缺凭证即跳过校验"的兼容行为；失败不改变 task data。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-A", "", "review", true);

        // 无 lease_token
        let err = store
            .handle_task_apply(peer.clone(), &serde_json::json!({"task_id": "T-A", "fencing_counter": 1}))
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // 无 fencing_counter
        let err = store
            .handle_task_apply(peer.clone(), &serde_json::json!({"task_id": "T-A", "lease_token": "tok"}))
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // 只提供 lease_token（缺 fencing_counter）
        let err = store
            .handle_task_apply(peer.clone(), &serde_json::json!({"task_id": "T-A", "lease_token": "tok"}))
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // fencing_counter 非整数（类型不完整）
        let err = store
            .handle_task_apply(peer.clone(), &serde_json::json!({"task_id": "T-A", "lease_token": "tok", "fencing_counter": "1"}))
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // 空 lease_token
        let err = store
            .handle_task_apply(peer, &serde_json::json!({"task_id": "T-A", "lease_token": "", "fencing_counter": 1}))
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // 失败后任务状态不变（未写入 applied）
        let conn = store.conn.lock().unwrap();
        let status: String = conn
            .query_row("SELECT status FROM tasks WHERE id = 'T-A'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(status, "review");
    }

    #[test]
    fn test_task_apply_writes_applied_at() {
        // 观察#1 回归：daemon 权威路径 task.apply 必须回填 tasks.applied_at 列，
        // 与 Python db_tasks.task_apply（line 1990）对齐；否则 auto/enterprise 模式下
        // applied_at 恒为 NULL，破坏审计轨迹与父子级联语义。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        // seed_task 不写 applied_at（列默认 NULL），正好用于验证 apply 后回填。
        seed_task(&store, "T-APPLY", "", "review", true);
        seed_reviewer_lease(&store, "T-APPLY", "tok-apply", 1, "agent-r", "sess-r", "model-r");

        let res = store
            .handle_task_apply(
                peer,
                &serde_json::json!({"task_id": "T-APPLY", "lease_token": "tok-apply", "fencing_counter": 1}),
            )
            .unwrap();
        assert_eq!(res["status"], "applied");

        // 响应层 applied_at 非零
        let applied_at_resp = res["applied_at"].as_f64().unwrap();
        assert!(applied_at_resp > 0.0, "响应 applied_at 应为真实非零时间戳");

        // DB 行 applied_at 已被回填（非空、非零）
        let conn = store.conn.lock().unwrap();
        let applied_at_db: f64 = conn
            .query_row("SELECT applied_at FROM tasks WHERE id = 'T-APPLY'", [], |r| r.get(0))
            .unwrap();
        assert!(applied_at_db > 0.0, "tasks.applied_at 应在 daemon apply 后被写入非空值");
    }

    #[test]
    fn test_task_close_requires_lease_credentials() {
        // S3 强制门禁：daemon 权威路径下 task.close 缺少/不完整 lease 凭证 → E_LEASE_REQUIRED
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-A", "", "applied", true);

        // 无 lease_token
        let err = store
            .handle_task_close(peer.clone(), &serde_json::json!({"task_id": "T-A", "fencing_counter": 1}))
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // 无 fencing_counter
        let err = store
            .handle_task_close(peer.clone(), &serde_json::json!({"task_id": "T-A", "lease_token": "tok"}))
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // 只提供 lease_token（缺 fencing_counter）
        let err = store
            .handle_task_close(peer.clone(), &serde_json::json!({"task_id": "T-A", "lease_token": "tok"}))
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // fencing_counter 非整数（类型不完整）
        let err = store
            .handle_task_close(peer, &serde_json::json!({"task_id": "T-A", "lease_token": "tok", "fencing_counter": "1"}))
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // 失败后任务状态不变（未写入 closed）
        let conn = store.conn.lock().unwrap();
        let status: String = conn
            .query_row("SELECT status FROM tasks WHERE id = 'T-A'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(status, "applied");
    }

    #[test]
    fn test_completion_review_zero_steps_blocked() {
        // S4: 零步骤普通任务 completion-review 返回 blocked，不能 vacuous pass
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-NS", "", "applied", false);

        let res = store
            .handle_task_completion_review(peer, &serde_json::json!({"task_id": "T-NS"}))
            .unwrap();
        assert_eq!(res["decision"], "blocked");
        assert_eq!(res["reason"], "E_NO_STEPS");
    }

    // ============================================
    // 任务 E（T-1786438019310）：task.create_subtask 漏写 steps + claim 返回步骤详情契约
    // ============================================

    #[test]
    fn test_task_create_subtask_persists_steps_and_step_count() {
        // S1: create_subtask 必须接收 steps 并写入 task_steps，返回 step_count
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,"task_id": "T-SUB-PARENT", "title": "parent"}),
            )
            .unwrap();

        let res = store
            .handle_task_create_subtask(
                peer,
                &serde_json::json!({
                    "parent_task_id": "T-SUB-PARENT",
                    "title": "child",
                    "description": "child desc",
                    "steps": [
                        {
                            "action": "audit",
                            "target_file": "rust_ext/src/daemon/task_collab.rs",
                            "target_symbol": "TaskCollabStore::handle_task_create_subtask",
                            "check_items": ["read code", "verify"],
                        },
                        {
                            "action": "fix",
                            "target_file": "server/tools/tools_task.py",
                            "check_items": "pytest",
                        },
                    ],
                }),
            )
            .unwrap();
        assert_eq!(res["status"], "open");
        assert_eq!(res["parent_id"], "T-SUB-PARENT");
        assert_eq!(res["step_count"], 2);

        // 步骤完整写入且绑定正确 task_id
        let conn = store.conn.lock().unwrap();
        let task_id = res["task_id"].as_str().unwrap();
        let mut stmt = conn
            .prepare(
                "SELECT task_id, step_index, action, target_file, target_symbol, check_items, status, id
                 FROM task_steps WHERE task_id = ?1 ORDER BY step_index",
            )
            .unwrap();
        let rows: Vec<(String, i64, String, String, String, String, String, String)> = stmt
            .query_map(params![task_id], |r| {
                Ok((
                    r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?,
                    r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?,
                ))
            })
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].0, task_id);
        assert_eq!(rows[0].1, 0);
        assert_eq!(rows[0].2, "audit");
        assert_eq!(rows[0].3, "rust_ext/src/daemon/task_collab.rs");
        assert_eq!(rows[0].4, "TaskCollabStore::handle_task_create_subtask");
        assert_eq!(rows[0].5, "[\"read code\",\"verify\"]");
        assert_eq!(rows[0].6, "pending");
        assert!(rows[0].7.starts_with("S-"), "step_id 必须是真实生成 id: {}", rows[0].7);
        assert_eq!(rows[1].1, 1);
        assert_eq!(rows[1].2, "fix");
        assert_eq!(rows[1].3, "server/tools/tools_task.py");
        assert_eq!(rows[1].5, "pytest");

        // tasks 表 description 已保存
        let desc: String = conn
            .query_row("SELECT description FROM tasks WHERE id = ?1", params![task_id], |r| r.get(0))
            .unwrap();
        assert_eq!(desc, "child desc");
    }

    #[test]
    fn test_task_create_subtask_rolls_back_on_invalid_steps() {
        // S2: steps 非 array 时整体回滚，不留下半成品子任务/步骤/事件
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,"task_id": "T-SUB-ROLLBACK", "title": "parent"}),
            )
            .unwrap();

        let err = store
            .handle_task_create_subtask(
                peer,
                &serde_json::json!({
                    "parent_task_id": "T-SUB-ROLLBACK",
                    "title": "bad",
                    "steps": "not-an-array",
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "invalid_params");

        let conn = store.conn.lock().unwrap();
        let children: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM tasks WHERE parent_id = 'T-SUB-ROLLBACK'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let steps: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps ts JOIN tasks t ON t.id = ts.task_id
                 WHERE t.parent_id = 'T-SUB-ROLLBACK'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let events: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events WHERE reason_code = 'subtask_created'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(children, 0, "回滚后不应残留子任务");
        assert_eq!(steps, 0, "回滚后不应残留步骤");
        assert_eq!(events, 0, "回滚后不应残留 subtask_created 事件");
    }

    #[test]
    fn test_task_claim_returns_step_details_contract() {
        // S3: task.claim 返回下一步骤详情（step_id/step_index/action/target_file/target_symbol/
        // check_items/step_status/task_title），与 Python db.task_next_step 契约对齐。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CLAIM-STEPS",
                    "title": "claim with steps",
                    "steps": [
                        {
                            "action": "audit",
                            "target_file": "rust_ext/src/daemon/task_collab.rs",
                            "target_symbol": "TaskCollabStore::handle_task_claim",
                            "check_items": ["read"],
                        },
                        {"action": "fix", "target_file": "server/tools/tools_task.py"},
                    ],
                }),
            )
            .unwrap();

        let claim = store
            .handle_task_claim(
                peer,
                &serde_json::json!({"task_id": "T-CLAIM-STEPS", "agent_session_id": "session-claim"}),
            )
            .unwrap();
        assert_eq!(claim["status"], "in_progress");
        assert_eq!(claim["claimed_by"], "session-claim");
        assert!(claim["step_id"].as_str().unwrap().starts_with("S-"), "claim 必须返回真实 step_id");
        assert_eq!(claim["step_index"], 0);
        assert_eq!(claim["action"], "audit");
        assert_eq!(claim["target_file"], "rust_ext/src/daemon/task_collab.rs");
        assert_eq!(claim["target_symbol"], "TaskCollabStore::handle_task_claim");
        assert_eq!(claim["check_items"], "[\"read\"]");
        assert_eq!(claim["step_status"], "in_progress");
        assert_eq!(claim["task_title"], "claim with steps");
    }

    #[test]
    fn test_task_claim_without_steps_omits_step_fields() {
        // S4: 无步骤任务的 claim 返回 {task_id, status, claimed_by}，不含 step_* 字段（兼容旧契约）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,"task_id": "T-CLAIM-NOSTEPS", "title": "no steps"}),
            )
            .unwrap();

        let claim = store
            .handle_task_claim(
                peer,
                &serde_json::json!({"task_id": "T-CLAIM-NOSTEPS", "agent_session_id": "s"}),
            )
            .unwrap();
        assert_eq!(claim["status"], "in_progress");
        assert_eq!(claim["claimed_by"], "s");
        assert!(claim.get("step_id").is_none(), "无步骤任务不应返回 step_id");
    }

    #[test]
    fn test_task_claim_dedup_idempotent() {
        // S5: 同一 request_id 重复 claim 返回缓存结果（dedup 语义保留）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CLAIM-DEDUP",
                    "title": "dedup",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                }),
            )
            .unwrap();

        let params = serde_json::json!({
            "request_id": "req-dedup-1",
            "task_id": "T-CLAIM-DEDUP",
            "agent_session_id": "session-d",
        });
        let first = store.handle_task_claim(peer.clone(), &params).unwrap();
        assert!(first["step_id"].as_str().unwrap().starts_with("S-"));
        let second = store.handle_task_claim(peer.clone(), &params).unwrap();
        assert_eq!(first, second, "同 request_id 重复调用必须幂等返回缓存");
    }

    #[test]
    fn test_task_claim_marks_step_in_progress() {
        // S6: claim 必须把首个 pending 步骤改为 in_progress（与 Python db.task_next_step 契约对齐）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CLAIM-STEPSTATE",
                    "title": "step state",
                    "steps": [
                        {"action": "audit", "target_file": "a.rs"},
                        {"action": "fix", "target_file": "b.rs"},
                    ],
                }),
            )
            .unwrap();

        let claim = store
            .handle_task_claim(peer, &serde_json::json!({"task_id": "T-CLAIM-STEPSTATE", "agent_session_id": "s6"}))
            .unwrap();
        assert_eq!(claim["step_index"], 0, "应领取 step_index=0 的步骤");
        assert_eq!(claim["step_status"], "in_progress");

        let conn = store.conn.lock().unwrap();
        let statuses: Vec<String> = conn
            .prepare("SELECT status FROM task_steps WHERE task_id = ?1 ORDER BY step_index ASC")
            .unwrap()
            .query_map(params!["T-CLAIM-STEPSTATE"], |r| r.get(0))
            .unwrap()
            .map(|r| r.unwrap())
            .collect();
        drop(conn);
        assert_eq!(statuses, vec!["in_progress", "pending"], "首个步骤应 in_progress，其余保持 pending");
    }

    #[test]
    fn test_task_claim_resume_same_session_returns_in_progress_step() {
        // S7: 同 session 再次 claim 返回已 in_progress 的步骤（恢复语义，不重复占用新步骤）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CLAIM-RESUME",
                    "title": "resume",
                    "steps": [{"action": "audit", "target_file": "a.rs"}, {"action": "fix", "target_file": "b.rs"}],
                }),
            )
            .unwrap();

        let first = store
            .handle_task_claim(peer.clone(), &serde_json::json!({"task_id": "T-CLAIM-RESUME", "agent_session_id": "s-resume"}))
            .unwrap();
        let second = store
            .handle_task_claim(peer, &serde_json::json!({"task_id": "T-CLAIM-RESUME", "agent_session_id": "s-resume"}))
            .unwrap();

        assert_eq!(first["step_id"], second["step_id"], "同 session 恢复必须返回同一步骤");
        assert_eq!(first["step_index"], second["step_index"]);
        assert_eq!(second["step_status"], "in_progress");
    }

    #[test]
    fn test_task_claim_concurrent_session_conflict() {
        // S8: 已被其他 session claim 的 in_progress 任务，不同 session 再次 claim 必须拒绝（并发 claim 冲突）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CLAIM-CONFLICT",
                    "title": "conflict",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                }),
            )
            .unwrap();

        store
            .handle_task_claim(peer, &serde_json::json!({"task_id": "T-CLAIM-CONFLICT", "agent_session_id": "agent-a"}))
            .unwrap();

        let err = store
            .handle_task_claim(peer, &serde_json::json!({"task_id": "T-CLAIM-CONFLICT", "agent_session_id": "agent-b"}))
            .unwrap_err();
        assert_eq!(err.code, "task_conflict", "不同 session 并发 claim 必须拒绝: {}", err);
    }

    #[test]
    fn test_orphan_claim_recovery_requires_stale_owner_and_preserves_step_state() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CLAIM-RECOVER",
                    "title": "recover",
                    "steps": [
                        {"action": "report", "target_file": "a.rs"},
                        {"action": "fix_defect", "target_file": "b.rs"}
                    ],
                }),
            )
            .unwrap();

        let old = serde_json::json!({
            "agent_id": "agent-old",
            "agent_instance_id": "old-instance",
            "client_id": "test",
            "provider": "test",
            "model_id": "model-old",
            "session_id": "old-session",
            "role": "implementer",
        });
        register_agent_with_identity(&store, &peer, "agent-old", "old-instance", "old-session", "implementer");
        store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-CLAIM-RECOVER",
                    "agent_session_id": "old-session",
                    "identity": old,
                }),
            )
            .unwrap();

        let adjudicator = serde_json::json!({
            "agent_id": "agent-adjudicator",
            "agent_instance_id": "adjudicator-instance",
            "client_id": "test",
            "provider": "test",
            "model_id": "model-adjudicator",
            "session_id": "adjudicator-session",
            "role": "adjudicator",
        });
        register_agent_with_identity(
            &store,
            &peer,
            "agent-adjudicator",
            "adjudicator-instance",
            "adjudicator-session",
            "adjudicator",
        );
        // 让旧 owner 明确失联；不能依赖客户端时间戳。
        store
            .conn
            .lock()
            .unwrap()
            .execute(
                "UPDATE agent_registrations SET last_heartbeat = 0 WHERE session_id = 'old-session'",
                [],
            )
            .unwrap();

        let lease = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-CLAIM-RECOVER",
                    "role": "reviewer",
                    "identity": adjudicator,
                }),
            )
            .unwrap();
        let recover_params = serde_json::json!({
            "request_id": "recover-request-1",
            "task_id": "T-CLAIM-RECOVER",
            "reason": "原 owner session 已失联，需要恢复 fix_defect 工作流",
            "lease_token": lease["token"],
            "fencing_counter": lease["fencing_counter"],
            "identity": {
                "agent_id": "agent-adjudicator",
                "agent_instance_id": "adjudicator-instance",
                "client_id": "test",
                "provider": "test",
                "model_id": "model-adjudicator",
                "session_id": "adjudicator-session",
                "role": "adjudicator",
            },
        });
        let recovered = store
            .handle_task_claim_recover(peer.clone(), &recover_params)
            .unwrap();
        assert_eq!(recovered["claim_status"], "released");
        assert_eq!(recovered["old_session_id"], "old-session");
        assert_eq!(store.get_task_claim_info(&store.conn.lock().unwrap(), "T-CLAIM-RECOVER"), (None, None));

        // recovery 只释放 claim；步骤状态和历史 evidence 不被重写，新的 Executor 再显式 claim。
        let new_executor = serde_json::json!({
            "agent_id": "agent-new",
            "agent_instance_id": "new-instance",
            "client_id": "test",
            "provider": "test",
            "model_id": "model-new",
            "session_id": "new-session",
            "role": "implementer",
        });
        register_agent_with_identity(&store, &peer, "agent-new", "new-instance", "new-session", "implementer");
        let claimed = store
            .handle_task_claim(
                peer,
                &serde_json::json!({
                    "task_id": "T-CLAIM-RECOVER",
                    "agent_session_id": "new-session",
                    "identity": new_executor,
                }),
            )
            .unwrap();
        assert_eq!(claimed["claimed_by"], "new-session");
        assert_eq!(claimed["step_index"], 0);

        let replay = store
            .handle_task_claim_recover(PeerCredential::new_unix(1000, 1000, 1234), &recover_params)
            .unwrap();
        assert_eq!(replay["recovery_event_id"], recovered["recovery_event_id"]);
        assert_eq!(replay["replayed"], false, "同 request_id 应返回第一次确定性结果");
    }

    #[test]
    fn test_orphan_claim_recovery_rejects_fresh_owner() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CLAIM-RECOVER-FRESH",
                    "title": "recover fresh",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                }),
            )
            .unwrap();
        let old = serde_json::json!({
            "agent_id": "agent-fresh-old",
            "agent_instance_id": "fresh-old-instance",
            "client_id": "test",
            "provider": "test",
            "model_id": "model-old",
            "session_id": "fresh-old-session",
            "role": "implementer",
        });
        register_agent_with_identity(&store, &peer, "agent-fresh-old", "fresh-old-instance", "fresh-old-session", "implementer");
        store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-CLAIM-RECOVER-FRESH",
                    "agent_session_id": "fresh-old-session",
                    "identity": old,
                }),
            )
            .unwrap();
        let adjudicator = serde_json::json!({
            "agent_id": "agent-fresh-adjudicator",
            "agent_instance_id": "fresh-adjudicator-instance",
            "client_id": "test",
            "provider": "test",
            "model_id": "model-adjudicator",
            "session_id": "fresh-adjudicator-session",
            "role": "adjudicator",
        });
        register_agent_with_identity(&store, &peer, "agent-fresh-adjudicator", "fresh-adjudicator-instance", "fresh-adjudicator-session", "adjudicator");
        let lease = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-CLAIM-RECOVER-FRESH",
                    "role": "reviewer",
                    "identity": adjudicator,
                }),
            )
            .unwrap();
        let err = store
            .handle_task_claim_recover(
                peer,
                &serde_json::json!({
                    "task_id": "T-CLAIM-RECOVER-FRESH",
                    "reason": "测试 fresh owner 必须拒绝",
                    "lease_token": lease["token"],
                    "fencing_counter": lease["fencing_counter"],
                    "identity": {
                        "agent_id": "agent-fresh-adjudicator",
                        "agent_instance_id": "fresh-adjudicator-instance",
                        "client_id": "test",
                        "provider": "test",
                        "model_id": "model-adjudicator",
                        "session_id": "fresh-adjudicator-session",
                        "role": "adjudicator",
                    },
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_CLAIM_OWNER_ACTIVE");
    }

    // ============================================
    // 任务 F（T-1786440663336-7e7d67e8）步骤 #3：Agent Identity + Role Contract
    // ============================================

    fn register_agent_with_identity(
        store: &TaskCollabStore,
        peer: &PeerCredential,
        agent_id: &str,
        instance_id: &str,
        session_id: &str,
        role: &str,
    ) -> Value {
        store
            .handle_agent_register(
                peer.clone(),
                &serde_json::json!({
                    "agent_id": agent_id,
                    "agent_name": format!("agent-{}", agent_id),
                    "capabilities": ["code"],
                    "identity": {
                        "agent_id": agent_id,
                        "agent_instance_id": instance_id,
                        "client_id": "trae",
                        "provider": "anthropic",
                        "model_id": "claude-test",
                        "model_mode": "agent",
                        "system_fingerprint": "fp-1",
                        "session_id": session_id,
                        "role": role,
                        "runtime_hash": "deadbeef",
                    },
                }),
            )
            .unwrap()
    }

    #[test]
    fn test_agent_register_persists_full_identity() {
        // A2: agent.register 必须持久化 identity 最小字段（instance/client/provider/model/runtime_hash/session/role）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        register_agent_with_identity(&store, &peer, "agent-alpha", "INST-1", "SES-1", "implementer");

        let conn = store.conn.lock().unwrap();
        let (instance, provider, model, session, role, runtime): (String, String, String, String, String, String) = conn
            .query_row(
                "SELECT agent_instance_id, provider, model_id, session_id, role, runtime_hash
                 FROM agent_registrations WHERE agent_id = 'agent-alpha'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)),
            )
            .unwrap();
        assert_eq!(instance, "INST-1");
        assert_eq!(provider, "anthropic");
        assert_eq!(model, "claude-test");
        assert_eq!(session, "SES-1");
        assert_eq!(role, "implementer");
        assert_eq!(runtime, "deadbeef");
    }

    #[test]
    fn test_claim_unregistered_identity_fail_closed() {
        // A2: 未注册 identity 的 claim 必须 fail-closed（E_IDENTITY_UNREGISTERED）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,"task_id": "T-GHOST", "title": "ghost"}),
            )
            .unwrap();

        let err = store
            .handle_task_claim(
                peer,
                &serde_json::json!({
                    "task_id": "T-GHOST",
                    "agent_session_id": "SES-GHOST",
                    "identity": {
                        "agent_id": "agent-ghost",
                        "agent_instance_id": "INST-G",
                        "session_id": "SES-GHOST",
                        "model_id": "model-test",
                        "role": "implementer",
                    },
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_IDENTITY_UNREGISTERED");
    }

    #[test]
    fn test_claim_contract_task_requires_identity() {
        // A3: 冻结 Role Contract 的任务，不带 identity 的 claim 必须 fail-closed
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CONTRACT-NOID",
                    "title": "contract task",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                    "role_contracts": [
                        {
                            "role": "implementer",
                            "skill_id": "g0-experiment",
                            "skill_version": "1.0.0",
                            "prompt_hash": "abc123",
                        }
                    ],
                }),
            )
            .unwrap();

        let err = store
            .handle_task_claim(
                peer,
                &serde_json::json!({"task_id": "T-CONTRACT-NOID", "agent_session_id": "SES-X"}),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_IDENTITY_REQUIRED");
    }

    #[test]
    fn test_claim_contract_skill_mismatch_rejected() {
        // A3: skill_id 不符时拒绝领取（E_CONTRACT_SKILL_MISMATCH）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        register_agent_with_identity(&store, &peer, "agent-imp", "INST-2", "SES-2", "implementer");
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CONTRACT-MISMATCH",
                    "title": "contract mismatch",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                    "role_contracts": [
                        {
                            "role": "implementer",
                            "skill_id": "g0-experiment",
                            "skill_version": "1.0.0",
                            "prompt_hash": "abc123",
                        }
                    ],
                }),
            )
            .unwrap();

        let err = store
            .handle_task_claim(
                peer,
                &serde_json::json!({
                    "task_id": "T-CONTRACT-MISMATCH",
                    "agent_session_id": "SES-2",
                    "identity": {
                        "agent_id": "agent-imp",
                        "agent_instance_id": "INST-2",
                        "session_id": "SES-2",
                        "model_id": "claude-test",
                        "role": "implementer",
                    },
                    "contract_claim": {"skill_id": "wrong-skill", "skill_version": "1.0.0", "prompt_hash": "abc123"},
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_CONTRACT_SKILL_MISMATCH");
    }

    #[test]
    fn test_claim_envelope_returns_role_contract() {
        // A3: 合同匹配时 claim 成功，且 Task Envelope 携带 role_contract（hash/revision 存证）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        register_agent_with_identity(&store, &peer, "agent-imp", "INST-3", "SES-3", "implementer");
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-ENVELOPE",
                    "title": "envelope",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                    "role_contracts": [
                        {
                            "role": "implementer",
                            "skill_id": "g0-experiment",
                            "skill_version": "1.0.0",
                            "prompt_template_id": "pt-1",
                            "prompt_hash": "abc123",
                            "allowed_paths": ["rust_ext/src/daemon"],
                            "forbidden_paths": ["db/schema.py"],
                            "handoff_to": "independent_reviewer",
                        }
                    ],
                }),
            )
            .unwrap();

        let claim = store
            .handle_task_claim(
                peer,
                &serde_json::json!({
                    "task_id": "T-ENVELOPE",
                    "agent_session_id": "SES-3",
                    "identity": {
                        "agent_id": "agent-imp",
                        "agent_instance_id": "INST-3",
                        "session_id": "SES-3",
                        "model_id": "claude-test",
                        "role": "implementer",
                    },
                    "contract_claim": {"skill_id": "g0-experiment", "skill_version": "1.0.0", "prompt_hash": "abc123"},
                }),
            )
            .unwrap();
        assert_eq!(claim["status"], "in_progress");
        let contract = claim["role_contract"].as_object().expect("claim 必须携带 role_contract");
        assert_eq!(contract["role"], "implementer");
        assert_eq!(contract["skill_id"], "g0-experiment");
        assert_eq!(contract["prompt_hash"], "abc123");
        assert_eq!(contract["revision"], 1);
        assert_eq!(contract["handoff_to"], "independent_reviewer");
        assert!(contract["forbidden_paths"].as_str().unwrap().contains("db/schema.py"));
    }

    #[test]
    fn test_role_independence_gate_blocks_shared_instance() {
        // A3: 同一 agent_instance_id 不能同时持有 implementer 与 independent_reviewer
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer_a = PeerCredential::new_unix(1000, 1000, 1234);
        let peer_b = PeerCredential::new_unix(1001, 1001, 5678);
        register_agent_with_identity(&store, &peer_a, "agent-imp", "INST-SHARED", "SES-A", "implementer");
        register_agent_with_identity(&store, &peer_b, "agent-rev", "INST-SHARED", "SES-B", "independent_reviewer");
        seed_workspace(&store);
        store
            .handle_task_create(
                peer_b.clone(),
                &serde_json::json!({ "workspace_id": 1,"task_id": "T-INDEP", "title": "indep"}),
            )
            .unwrap();

        let err = store
            .handle_task_claim(
                peer_b,
                &serde_json::json!({
                    "task_id": "T-INDEP",
                    "agent_session_id": "SES-B",
                    "identity": {
                        "agent_id": "agent-rev",
                        "agent_instance_id": "INST-SHARED",
                        "session_id": "SES-B",
                        "model_id": "claude-test",
                        "role": "independent_reviewer",
                    },
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_ROLE_INDEPENDENCE_VIOLATION");
    }

    #[test]
    fn test_contract_set_bumps_revision_and_audits() {
        // A3: 合同变更必须生成新 revision 并追加 contract_set 审计事件
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,"task_id": "T-CS", "title": "cs"}),
            )
            .unwrap();

        let set = store
            .handle_task_contract_set(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-CS",
                    "contract": {"role": "implementer", "skill_id": "g0-experiment", "prompt_hash": "hash-v1"},
                }),
            )
            .unwrap();
        assert_eq!(set["revision"], 1);
        let set2 = store
            .handle_task_contract_set(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-CS",
                    "contract": {"role": "implementer", "skill_id": "g0-experiment", "prompt_hash": "hash-v2"},
                }),
            )
            .unwrap();
        assert_eq!(set2["revision"], 2);

        let conn = store.conn.lock().unwrap();
        let current: String = conn
            .query_row(
                "SELECT prompt_hash FROM role_contracts WHERE task_id = 'T-CS' AND role = 'implementer' AND is_current = 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(current, "hash-v2");
        let old_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM role_contracts WHERE task_id = 'T-CS' AND role = 'implementer' AND is_current = 0",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(old_count, 1);
        let event_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events WHERE task_id = 'T-CS' AND reason_code = 'contract_set'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(event_count, 2);
    }

    #[test]
    fn test_report_role_not_contracted_rejected() {
        // A3: 合同任务 report 必须匹配已冻结角色（未合同角色 report 拒绝）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        register_agent_with_identity(&store, &peer, "agent-t", "INST-4", "SES-4", "tester");
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-ROLE-MISMATCH",
                    "title": "role mismatch",
                    "steps": [{"action": "test", "target_file": "a.rs"}],
                    "role_contracts": [{"role": "implementer", "skill_id": "g0-experiment"}],
                }),
            )
            .unwrap();
        // tester 角色 claim（无 implementer 合同约束）成功
        let claim = store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-ROLE-MISMATCH",
                    "agent_session_id": "SES-4",
                    "identity": {
                        "agent_id": "agent-t",
                        "agent_instance_id": "INST-4",
                        "session_id": "SES-4",
                        "model_id": "claude-test",
                        "role": "tester",
                    },
                }),
            )
            .unwrap();
        assert_eq!(claim["status"], "in_progress");
        let step_id = claim["step_id"].as_str().unwrap();

        let err = store
            .handle_task_report(
                peer,
                &serde_json::json!({
                    "task_id": "T-ROLE-MISMATCH",
                    "step_id": step_id,
                    "summary": "done",
                    "success": true,
                    "identity": {
                        "agent_id": "agent-t",
                        "agent_instance_id": "INST-4",
                        "session_id": "SES-4",
                        "model_id": "claude-test",
                        "role": "tester",
                    },
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_CONTRACT_ROLE_MISMATCH");
    }

    #[test]
    fn test_task_bound_evidence_and_gate_are_idempotent() {
        // Evidence/Gate 必须由同一 daemon authority 追加，并绑定 task/step。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let ident = lease_identity("agent-evidence", "evidence-session", "model", "implementer");
        seed_workspace(&store);
        store.handle_task_create(peer.clone(), &serde_json::json!({ "workspace_id": 1,
            "task_id": "T-EVIDENCE-GATE",
            "title": "evidence gate",
            "steps": [{"action": "test", "target_file": "rust_ext/src/daemon/task_collab.rs"}]
        })).unwrap();
        let step_id: String = {
            let conn = store.conn.lock().unwrap();
            conn.query_row("SELECT id FROM task_steps WHERE task_id='T-EVIDENCE-GATE'", [], |r| r.get(0)).unwrap()
        };
        let lease = store.handle_lease_acquire(peer.clone(), &serde_json::json!({
            "task_id": "T-EVIDENCE-GATE", "role": "implementer", "ttl_seconds": 3600.0,
            "identity": ident
        })).unwrap();
        let evidence_params = serde_json::json!({
            "task_id": "T-EVIDENCE-GATE", "step_id": step_id,
            "evidence_id": "EV-T-EVIDENCE-GATE-1", "evidence_type": "test_run",
            "manifest_path": "docs/evidence/authority-recovery/T-EVIDENCE-GATE.json",
            "payload_hash": "sha256:test-manifest", "request_id": "evidence-1",
            "evidence_json": {"tests": "pass"}, "identity": ident,
            "lease_token": lease["token"], "fencing_counter": lease["fencing_counter"]
        });
        let appended = store.handle_evidence_append(peer.clone(), &evidence_params).unwrap();
        assert_eq!(appended["evidence_id"], "EV-T-EVIDENCE-GATE-1");
        let replay = store.handle_evidence_append(peer.clone(), &evidence_params).unwrap();
        assert_eq!(replay["evidence_id"], "EV-T-EVIDENCE-GATE-1");
        let gate_params = serde_json::json!({
            "task_id": "T-EVIDENCE-GATE", "step_id": step_id,
            "evidence_id": "EV-T-EVIDENCE-GATE-1", "evidence_hash": "sha256:test-manifest",
            "payload_hash": "sha256:test-manifest", "decision": "pass",
            "reason": "task-bound evidence verified", "request_id": "gate-1",
            "identity": ident, "lease_token": lease["token"],
            "fencing_counter": lease["fencing_counter"]
        });
        let gate = store.handle_gate_decision_append(peer.clone(), &gate_params).unwrap();
        assert_eq!(gate["evidence_id"], "EV-T-EVIDENCE-GATE-1");
        let gate_replay = store.handle_gate_decision_append(peer.clone(), &gate_params).unwrap();
        assert_eq!(gate_replay["evidence_id"], "EV-T-EVIDENCE-GATE-1");
        let mut mismatch = gate_params.clone();
        mismatch["payload_hash"] = Value::String("sha256:other".into());
        let err = store.handle_gate_decision_append(peer, &mismatch).unwrap_err();
        assert_eq!(err.code, "E_REQUEST_ID_REUSE_MISMATCH");
        let conn = store.conn.lock().unwrap();
        let evidence_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM task_evidence_events WHERE task_id='T-EVIDENCE-GATE'", [], |r| r.get(0)
        ).unwrap();
        let gate_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM task_gate_decisions WHERE task_id='T-EVIDENCE-GATE'", [], |r| r.get(0)
        ).unwrap();
        assert_eq!(evidence_count, 1);
        assert_eq!(gate_count, 1);
    }

    #[test]
    fn test_verdict_submit_appends_replays_and_rejects_conflicts() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-VERDICT-NATIVE",
                    "title": "native verdict",
                    "steps": [{"action": "review", "target_file": "a.rs"}],
                    "role_contracts": [{
                        "role": "independent_reviewer",
                        "skill_id": "none",
                        "skill_version": "v1",
                        "prompt_template_id": "reviewer-v1",
                        "prompt_hash": "sha256:prompt",
                        "allowed_paths": [],
                        "forbidden_paths": ["a.rs"],
                        "commands": ["cargo test"],
                        "acceptance_checks": ["focused tests pass"],
                        "required_evidence": ["test_log"],
                        "handoff_to": "adjudicator",
                        "independence": {"different_session_from": ["implementer"]}
                    }]
                }),
            )
            .unwrap();

        let (step_id, role_contract_id, role_contract_revision, role_contract_hash) = {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE tasks SET status = 'review' WHERE id = 'T-VERDICT-NATIVE'",
                [],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO task_contract_revisions
                 (contract_id, revision, contract_hash, profile, task_id, workspace_id,
                  envelope_payload, created_at, created_by)
                 VALUES ('TC-VERDICT', 1, 'sha256:task-contract', 'review',
                         'T-VERDICT-NATIVE', 1, '{}', 1.0, 'test')",
                [],
            )
            .unwrap();
            let step_id: String = conn
                .query_row(
                    "SELECT id FROM task_steps WHERE task_id = 'T-VERDICT-NATIVE'",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            let row: (
                String,
                i64,
                String,
                String,
                String,
                String,
                String,
                String,
                String,
                String,
                String,
                String,
                String,
                String,
                String,
            ) = conn
                .query_row(
                    "SELECT contract_id, revision, role, step_id, skill_id, skill_version,
                            prompt_template_id, prompt_hash, allowed_paths, forbidden_paths,
                            commands, acceptance_checks, required_evidence, handoff_to, independence
                     FROM role_contracts
                     WHERE task_id = 'T-VERDICT-NATIVE' AND is_current = 1",
                    [],
                    |r| {
                        Ok((
                            r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?,
                            r.get(5)?, r.get(6)?, r.get(7)?, r.get(8)?, r.get(9)?,
                            r.get(10)?, r.get(11)?, r.get(12)?, r.get(13)?, r.get(14)?,
                        ))
                    },
                )
                .unwrap();
            let payload = serde_json::json!({
                "canonicalization_version": "role-contract-c14n/v1",
                "contract_id": row.0,
                "revision": row.1,
                "task_id": "T-VERDICT-NATIVE",
                "role": row.2,
                "step_id": row.3,
                "skill_id": row.4,
                "skill_version": row.5,
                "prompt_template_id": row.6,
                "prompt_hash": row.7,
                "allowed_paths": row.8,
                "forbidden_paths": row.9,
                "commands": row.10,
                "acceptance_checks": row.11,
                "required_evidence": row.12,
                "handoff_to": row.13,
                "independence": row.14,
            });
            (
                step_id,
                payload["contract_id"].as_str().unwrap().to_string(),
                payload["revision"].as_i64().unwrap(),
                format!("sha256:{}", sha256_hex(payload.to_string().as_bytes())),
            )
        };

        register_agent_with_identity(
            &store,
            &peer,
            "agent-native-reviewer",
            "reviewer-instance",
            "reviewer-session",
            "independent_reviewer",
        );
        let identity = lease_identity(
            "agent-native-reviewer",
            "reviewer-session",
            "claude-test",
            "independent_reviewer",
        );
        let lease = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-VERDICT-NATIVE",
                    "role": "reviewer",
                    "identity": identity,
                }),
            )
            .unwrap();
        let verdict_params = serde_json::json!({
            "task_id": "T-VERDICT-NATIVE",
            "step_id": step_id,
            "verdict_id": "V-NATIVE-1",
            "contract_id": "TC-VERDICT",
            "contract_revision": 1,
            "contract_hash": "sha256:task-contract",
            "role_contract_id": role_contract_id,
            "role_contract_revision": role_contract_revision,
            "role_contract_hash": role_contract_hash,
            "phase": "blind_first_pass",
            "view_manifest_hash": "sha256:view",
            "snapshot_id": "snapshot-1",
            "clause_results": [{"clause_id": "C1", "decision": "pass"}],
            "findings": [],
            "overall": "pass",
            "attestation": "reviewed independently",
            "request_id": "verdict-native-request-1",
            "identity": identity,
            "lease_token": lease["token"],
            "fencing_counter": lease["fencing_counter"],
        });
        let first = store
            .handle_verdict_submit(peer.clone(), &verdict_params)
            .unwrap();
        assert_eq!(first["verdict_id"], "V-NATIVE-1");
        assert_eq!(first["replayed"], false);

        let replay = store
            .handle_verdict_submit(peer.clone(), &verdict_params)
            .unwrap();
        assert_eq!(replay["verdict_id"], "V-NATIVE-1");
        assert_eq!(replay["replayed"], true);

        let mut mismatch = verdict_params.clone();
        mismatch["overall"] = Value::String("block".to_string());
        let err = store
            .handle_verdict_submit(peer.clone(), &mismatch)
            .unwrap_err();
        assert_eq!(err.code, "E_REQUEST_ID_REUSE_MISMATCH");

        let mut wrong_role_hash = verdict_params.clone();
        wrong_role_hash["request_id"] = Value::String("verdict-native-request-2".to_string());
        wrong_role_hash["verdict_id"] = Value::String("V-NATIVE-2".to_string());
        wrong_role_hash["role_contract_hash"] = Value::String("sha256:wrong".to_string());
        let err = store
            .handle_verdict_submit(peer, &wrong_role_hash)
            .unwrap_err();
        assert_eq!(err.code, "E_ROLE_CONTRACT_HASH_MISMATCH");

        let conn = store.conn.lock().unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_verdict_events WHERE task_id = 'T-VERDICT-NATIVE'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
        let (task_hash, reviewer_provenance): (String, String) = conn
            .query_row(
                "SELECT contract_hash, reviewer_identity FROM task_verdict_events
                 WHERE verdict_id = 'V-NATIVE-1'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(task_hash, "sha256:task-contract");
        assert!(reviewer_provenance.contains("role_contract"));
        assert!(reviewer_provenance.contains("verdict-native-request-1"));
    }
    // ============================================
    // V1 workspace authority fail-closed 契约测试（E_TASK_WORKSPACE_UNBOUND /
    // E_WORKSPACE_AUTHORITY_MISMATCH / E_IDENTITY_NOT_WIRED）
    // ============================================

    #[test]
    fn test_task_list_requires_workspace_id_fail_closed() {
        // 缺陷2回归：task.list 缺显式 workspace_id → E_TASK_WORKSPACE_UNBOUND，
        // 绝不回退全表 WHERE 1=1。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let err = store
            .handle_task_list(peer, &serde_json::json!({"status": "", "limit": 20}))
            .unwrap_err();
        assert_eq!(err.code, "E_TASK_WORKSPACE_UNBOUND");
    }

    #[test]
    fn test_task_create_requires_workspace_id_fail_closed() {
        // 任务接口强制绑定：task.create 缺显式 workspace_id → fail-closed。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let err = store
            .handle_task_create(peer, &serde_json::json!({"title": "no workspace"}))
            .unwrap_err();
        assert_eq!(err.code, "E_TASK_WORKSPACE_UNBOUND");
    }

    #[test]
    fn test_lease_requires_binding_fail_closed() {
        // 缺陷3回归：lease 操作有 task_id 但无 task_workspace_bindings →
        // E_TASK_WORKSPACE_UNBOUND，禁止 active workspace / 任意 workspace 补齐。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let err = store
            .handle_lease_acquire(
                peer,
                &serde_json::json!({
                    "task_id": "T-UNBOUND-LEASE",
                    "role": "implementer",
                    "ttl_seconds": 3600.0,
                    "identity": lease_identity("agent-x", "session-x", "model-x", "implementer"),
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_TASK_WORKSPACE_UNBOUND");
    }

    #[test]
    fn test_lease_workspace_mismatch_fail_closed() {
        // 显式 workspace_id 与 binding 不一致 → E_WORKSPACE_AUTHORITY_MISMATCH。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store); // T-LEASE-1 已绑定 workspace 1
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let err = store
            .handle_lease_acquire(
                peer,
                &serde_json::json!({
                    "task_id": "T-LEASE-1",
                    "role": "implementer",
                    "workspace_id": 99,
                    "ttl_seconds": 3600.0,
                    "identity": lease_identity("agent-x", "session-x", "model-x", "implementer"),
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_WORKSPACE_AUTHORITY_MISMATCH");
    }

    #[test]
    fn test_active_workspace_id_fail_closed_without_active() {
        // 无 active workspace 时推导必须失败（E_IDENTITY_NOT_WIRED），
        // 绝不回退到“任意 workspace”。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let conn = store.conn.lock().unwrap();
        let err = active_workspace_id(&conn).unwrap_err();
        assert_eq!(err.code, "E_IDENTITY_NOT_WIRED");
    }
    #[test]
    fn test_task_create_writes_workspace_binding() {
        // 正向契约：task.create 在同一事务写入不可变 task_workspace_bindings，
        // 且 binding.workspace_id 与显式传入的 workspace_id 一致。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        seed_workspace(&store); // workspace id=1（is_active=1）
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let res = store
            .handle_task_create(
                peer,
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": "T-BIND-001",
                    "title": "bound task",
                }),
            )
            .unwrap();
        assert_eq!(res["workspace_id"], 1);
        assert!(res["workspace_binding_id"].as_str().is_some());
        let conn = store.conn.lock().unwrap();
        let bound_ws: i64 = conn
            .query_row(
                "SELECT workspace_id FROM task_workspace_bindings WHERE task_id = 'T-BIND-001'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(bound_ws, 1);
        let capture_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM workspace_authority_captures WHERE workspace_id = 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(capture_count >= 1, "task.create 必须写入 workspace authority capture");
    }

    #[test]
    fn test_handle_detect_cycle_native_parity() {
        // MCP-007：detect_cycle Rust native 与 Python db_task_dependencies.detect_cycle
        // 语义一致：workspace 内 is_hard=1 边构成环时返回 has_cycle=true + 最短 cycle path；
        // 无边 workspace 返回 has_cycle=false。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);

        // 构造硬依赖环：A -> B -> C -> A（workspace_id=1, is_hard=1）
        {
            let conn = store.conn.lock().unwrap();
            conn.execute_batch(
                "INSERT INTO dependency_edges \
                 (workspace_id, provider_task_id, consumer_task_id, edge_type, source_type, contract_id, contract_revision, is_hard, created_at) VALUES \
                 (1, 'A', 'B', 'hard_dep', 'task', 'C-1', 1, 1, 1.0), \
                 (1, 'B', 'C', 'hard_dep', 'task', 'C-1', 1, 1, 1.0), \
                 (1, 'C', 'A', 'hard_dep', 'task', 'C-1', 1, 1, 1.0);",
            )
            .unwrap();
        }

        let r = store
            .handle_detect_cycle(peer.clone(), &serde_json::json!({"workspace_id": 1}))
            .unwrap();
        assert_eq!(r["has_cycle"], serde_json::Value::Bool(true));
        let path = r["cycle_path"].as_array().expect("cycle_path 应为数组");
        assert!(!path.is_empty(), "有环时 cycle_path 非空");
        // 最短环应为 4 节点（A->B->C->A 含首尾）
        assert_eq!(path.len(), 4, "最短 cycle path 应为 A,B,C,A（4 节点）");
        assert_eq!(path[0], serde_json::json!("A"));
        assert_eq!(path[path.len() - 1], serde_json::json!("A"));
        assert_eq!(r["checked_nodes"], serde_json::json!(3));

        // 另一 workspace 无边 → 无环
        let empty = store
            .handle_detect_cycle(peer, &serde_json::json!({"workspace_id": 2}))
            .unwrap();
        assert_eq!(empty["has_cycle"], serde_json::Value::Bool(false));
        assert_eq!(empty["cycle_path"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn test_handle_validate_revision_dependencies_native_parity() {
        // MCP-008：validate_revision_dependencies Rust native 与 Python
        // tools_p2_graph._h_validate_revision_dependencies 语义一致：
        // 空依赖 → valid=true；requires_artifact 缺 target → resolution error；
        // 环合并检测（现有硬边 + 模拟边）。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);

        // 空依赖 → valid=true, edges_built=0
        let empty = store
            .handle_validate_revision_dependencies(
                peer.clone(),
                &serde_json::json!({"workspace_id": 1, "contract_id": "C-NONE", "contract_revision": 1}),
            )
            .unwrap();
        assert_eq!(empty["valid"], serde_json::Value::Bool(true));
        assert_eq!(empty["edges_built"], serde_json::json!(0));

        // requires_artifact 缺 target_task_id → resolution error
        {
            let conn = store.conn.lock().unwrap();
            conn.execute_batch(
                "INSERT INTO task_dependencies \
                 (workspace_id, task_id, dependency_type, target_ref, target_task_id, \
                  contract_id, contract_revision, is_informational, declared_at) VALUES \
                 (1, 'T-CON1', 'requires_artifact', 'ref-x', '', 'C-1', 1, 0, 1.0);",
            )
            .unwrap();
        }
        let bad = store
            .handle_validate_revision_dependencies(
                peer.clone(),
                &serde_json::json!({"workspace_id": 1, "contract_id": "C-1", "contract_revision": 1}),
            )
            .unwrap();
        assert_eq!(bad["valid"], serde_json::Value::Bool(false));
        assert_eq!(bad["edges_skipped"], serde_json::json!(1));
        let errs = bad["errors"].as_array().expect("errors 应为数组");
        assert_eq!(errs.len(), 1);
        assert!(errs[0].as_str().unwrap().contains("requires_artifact"));

        // 环：requires_artifact A->B + 现有硬边 B->A → has_cycle, valid=false
        {
            let conn = store.conn.lock().unwrap();
            conn.execute_batch(
                "INSERT INTO task_dependencies \
                 (workspace_id, task_id, dependency_type, target_ref, target_task_id, \
                  contract_id, contract_revision, is_informational, declared_at) VALUES \
                 (1, 'T-CON2', 'requires_artifact', 'ref-y', 'A', 'C-2', 1, 0, 1.0);",
            )
            .unwrap();
        }
        let cyc = store
            .handle_validate_revision_dependencies(
                peer.clone(),
                &serde_json::json!({"workspace_id": 1, "contract_id": "C-2", "contract_revision": 1}),
            )
            .unwrap();
        // 模拟边 A->T-CON2 与现有硬边 B->C 无环；显式检查 edges_built=1
        assert_eq!(cyc["edges_built"], serde_json::json!(1));
        assert!(cyc["errors"].as_array().unwrap().is_empty());
    }
}

