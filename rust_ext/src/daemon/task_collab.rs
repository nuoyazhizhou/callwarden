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

use rusqlite::{params, Connection, Error as RusqliteError, OptionalExtension, Transaction,
    TransactionBehavior};
use serde_json::{json, Map, Value};

use super::clock::AuthoritativeClock;
use super::dispatch::{DaemonRpcError, PeerCredential};
use super::assignment_queue::{self, AssignmentProjection};
use super::task_loop::operation_store::{
    DedupeOutcome, LedgerProvenance, OperationStore, ParamsRules,
};
use super::task_loop::role_worker::{
    parse_identity_policy, parse_role_worker_auth, policy_from_envelope_payload,
    validate_and_record as validate_role_worker,
    validate_and_record_worker_first as validate_role_worker_first, RoleWorkerAuth,
    ERR_POLICY_MISMATCH, ERR_POLICY_REQUIRED, ERR_ROLE_MISMATCH, POLICY_LEGACY_IDENTITY_V1,
    POLICY_ROLE_WORKER_V1,
};
use super::task_loop::task_contract_bootstrap::{
    bind_step_to_executor_role_contract, bootstrap_task_governance_contracts, BootstrapInput,
};
use super::task_loop::task_contract_revise::{append_task_contract_revision, ContractReviseInput};
use super::task_supersede::{validate_supersede_schema, verify_registered_identity};

use crate::canonicalize::sha256_hex;
use crate::sqlite_query::{current_schema_version, migrate_connection, RUST_SCHEMA_VERSION};

#[path = "task_collab_types.rs"]
mod task_collab_types;
pub(crate) use task_collab_types::ActionIdentity;
pub use task_collab_types::TaskCollabStore;
#[path = "task_collab_contract.rs"]
mod task_collab_contract;
#[path = "task_collab_contract_repair.rs"]
mod task_collab_contract_repair;
#[path = "task_collab_lease.rs"]
mod task_collab_lease;
#[path = "task_collab_lifecycle.rs"]
mod task_collab_lifecycle;
#[path = "task_collab_query.rs"]
mod task_collab_query;
#[path = "task_collab_evidence.rs"]
mod task_collab_evidence;
#[path = "task_collab_verdict.rs"]
mod task_collab_verdict;
#[path = "task_collab_governance.rs"]
mod task_collab_governance;
#[path = "task_collab_identity.rs"]
mod task_collab_identity;
#[path = "task_collab_lifecycle_ops.rs"]
mod task_collab_lifecycle_ops;
#[path = "task_collab_lifecycle_apply.rs"]
mod task_collab_lifecycle_apply;
#[path = "task_collab_planning.rs"]
mod task_collab_planning;
#[path = "task_collab_symbol.rs"]
mod task_collab_symbol;
#[path = "task_collab_shared.rs"]
mod task_collab_shared;
pub(crate) use task_collab_shared::*;

// ============================================
// 官方迁移后必须存在的 Task 协同权威表（只读校验清单）
// ============================================

const TASK_COLLAB_TABLES: [&str; 5] = [
    "tasks",
    "task_steps",
    "task_events",
    "agent_registrations",
    "action_identities",
];

/// SQLite writer lock acquisition is the only retryable boundary for governance
/// mutations.  Once an immediate transaction has been acquired, the caller
/// owns the writer slot and must not replay a partially executed handler.
const SQLITE_BUSY_RETRY_ATTEMPTS: usize = 3;
const SQLITE_BUSY_RETRY_BACKOFF_MS: [u64; SQLITE_BUSY_RETRY_ATTEMPTS - 1] = [50, 150];

fn is_sqlite_busy(error: &RusqliteError) -> bool {
    matches!(
        error,
        RusqliteError::SqliteFailure(sqlite_error, _)
            if matches!(
                sqlite_error.code,
                rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked
            )
    )
}

/// Start a writer transaction with a bounded retry at the lock-acquisition
/// boundary.  This is deliberately not a generic handler retry: the callback
/// has not run until `BEGIN IMMEDIATE` succeeds, so a retry cannot duplicate
/// task events, verdicts, or ledger rows.
pub(crate) fn begin_immediate_with_retry<'a>(
    conn: &'a Connection,
    operation: &str,
) -> Result<Transaction<'a>, DaemonRpcError> {
    fn attempt<'a>(
        conn: &'a Connection,
        operation: &str,
        attempt_no: usize,
    ) -> Result<Transaction<'a>, DaemonRpcError> {
        match Transaction::new_unchecked(&*conn, TransactionBehavior::Immediate) {
            Ok(transaction) => Ok(transaction),
            Err(error)
                if is_sqlite_busy(&error) && attempt_no + 1 < SQLITE_BUSY_RETRY_ATTEMPTS =>
            {
                std::thread::sleep(Duration::from_millis(
                    SQLITE_BUSY_RETRY_BACKOFF_MS[attempt_no],
                ));
                attempt(conn, operation, attempt_no + 1)
            }
            Err(error) => Err(DaemonRpcError::internal_error(format!(
                "开启 {} 事务失败: {}",
                operation, error
            ))),
        }
    }

    attempt(conn, operation, 0)
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
        None => (format!("wc-{}-{}", instance_id, rand_val()), 1, None),
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
        params![
            task_id,
            workspace_id,
            binding_id,
            capture_id,
            created_by,
            now
        ],
    )
    .map_err(|e| {
        DaemonRpcError::internal_error(format!("写入 task_workspace_bindings 失败: {}", e))
    })?;

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
pub(crate) fn insert_role_contracts(
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

/// 从 task.create 输入构造 revision-1 Task Contract envelope。
///
/// CLI 会在同一个 RPC 中提交三条 legacy role_contracts；这里把同一份任务
/// 范围、步骤验收和交接语义规范化为 daemon 的现代 Task Contract，随后由
/// `bootstrap_task_governance_contracts` 在当前事务内追加 revision/lineage/binding。
/// 这样新任务不会再出现“legacy role_contracts 已有、next-action 却无合同”的
/// 半套投影。
pub(crate) fn task_create_contract_envelope(
    task_id: &str,
    title: &str,
    description: &str,
    steps: &[Value],
) -> Value {
    let files: Vec<Value> = steps
        .iter()
        .filter_map(|step| step.get("target_file").and_then(Value::as_str))
        .flat_map(|raw| raw.split([',', ';', '+']))
        .map(str::trim)
        .filter(|path| !path.is_empty())
        .map(|path| Value::String(path.replace('\\', "/")))
        .collect();
    let acceptance: Vec<Value> = steps
        .iter()
        .filter_map(|step| step.get("check_items"))
        .filter(|items| !items.is_null())
        .cloned()
        .collect();
    serde_json::json!({
        "contract_id": format!("TC-{task_id}"),
        "revision": 1,
        "profile": "code_change",
        "objective": {
            "statement": title,
            "description": description,
            "source": "task.create"
        },
        "interfaces": {
            "rpc": "task.create",
            "task_id": task_id
        },
        "allowed_edit_scope": {
            "files": files,
            "symbols": [],
            "generated_from": "task steps"
        },
        "acceptance_clauses": acceptance,
        "risks": [{"id": "governance", "statement": "partial projection is forbidden"}],
        "rollback": {"strategy": "append-only historical records; failed transaction rolls back"},
        "dependencies": [],
        "handoff": {"from": "executor", "to": "reviewer", "independence_requirement": "required"},
        "source": {"kind": "task.create", "task_id": task_id}
    })
}

/// P0-L step1：task.create 的 identity policy fail-closed 解析。
///
/// 决策矩阵（caller envelope 绝不忽略）：
/// - caller envelope 存在 → envelope 是权威来源，必须含合法 `identity_policy`；
///   顶层 `identity_policy` 只允许与之一致或缺席，否则 mismatch；
/// - envelope 缺失 + 顶层显式 `legacy_identity_v1` → 生成 generic envelope 并注入
///   该 policy（显式 legacy 兼容通道）；
/// - envelope 缺失 + 顶层 `role_worker_v1` → `E_TASK_IDENTITY_POLICY_REQUIRED`
///   （role worker 任务必须携带完整 canonical envelope）；
/// - envelope 缺失 + policy 缺失/非法 → `E_TASK_IDENTITY_POLICY_REQUIRED`。
fn resolve_task_create_identity_policy(
    params: &Value,
    task_id: &str,
) -> Result<(String, Value), DaemonRpcError> {
    let expected_contract_id = format!("TC-{task_id}");
    let envelope_raw = params
        .get("task_contract_envelope")
        .filter(|value| !value.is_null());
    if let Some(raw) = envelope_raw {
        // caller envelope 优先：接受 object 或 JSON string 两种承载形式。
        let envelope: Value = if let Some(text) = raw.as_str() {
            serde_json::from_str(text).map_err(|_| {
                DaemonRpcError::new(
                    ERR_POLICY_REQUIRED,
                    "task_contract_envelope 字符串不是合法 JSON",
                )
            })?
        } else {
            raw.clone()
        };
        let policy = parse_identity_policy(&envelope)?;
        // 顶层 identity_policy 若存在必须与 envelope 一致（mismatch fail-closed）。
        if let Some(top) = params
            .get("identity_policy")
            .filter(|value| !value.is_null())
        {
            let declared = top
                .as_str()
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .ok_or_else(|| {
                    DaemonRpcError::new(
                        ERR_POLICY_REQUIRED,
                        "顶层 identity_policy 必须是非空字符串",
                    )
                })?;
            if declared != policy {
                return Err(DaemonRpcError::new(
                    ERR_POLICY_MISMATCH,
                    format!(
                        "顶层 identity_policy {declared} 与 Task Contract envelope {policy} 不一致"
                    ),
                ));
            }
        }
        // envelope 必须锚定当前任务且是 revision-1（防止跨任务/超版本注入）。
        let object = envelope.as_object().ok_or_else(|| {
            DaemonRpcError::new(
                ERR_POLICY_REQUIRED,
                "task_contract_envelope 必须是 JSON object",
            )
        })?;
        let contract_id = object
            .get("contract_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if contract_id != expected_contract_id {
            return Err(DaemonRpcError::new(
                ERR_POLICY_MISMATCH,
                format!("task_contract_envelope contract_id {contract_id} 必须等于 {expected_contract_id}"),
            ));
        }
        return Ok((policy, envelope));
    }
    // envelope 缺失：只接受顶层显式声明，且只有 legacy 允许由 daemon 生成 generic envelope。
    let declared = params
        .get("identity_policy")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let Some(declared) = declared else {
        return Err(DaemonRpcError::new(
            ERR_POLICY_REQUIRED,
            "task.create 缺少 identity_policy：必须提供含合法 identity_policy 的 task_contract_envelope，或显式顶层 identity_policy",
        ));
    };
    match declared {
        POLICY_LEGACY_IDENTITY_V1 => Ok((POLICY_LEGACY_IDENTITY_V1.to_string(), Value::Null)),
        POLICY_ROLE_WORKER_V1 => Err(DaemonRpcError::new(
            ERR_POLICY_REQUIRED,
            "identity_policy=role_worker_v1 必须携带完整 canonical task_contract_envelope，禁止由 daemon 隐式生成",
        )),
        other => Err(DaemonRpcError::new(
            ERR_POLICY_MISMATCH,
            format!("identity_policy {other} 未知；仅支持 {POLICY_LEGACY_IDENTITY_V1} / {POLICY_ROLE_WORKER_V1}"),
        )),
    }
}

/// P0-L step2：envelope 是否声明了 identity policy 槽位（只判存在性，不做解析）。
///
/// 无槽位 = 未声明，按 legacy 原路径继续（绝不静默默认任何 policy）；
/// 有槽位则必须经 `parse_identity_policy` fail-closed 解析。
fn envelope_has_policy_slot(envelope: &Value) -> bool {
    envelope
        .as_object()
        .map(|object| {
            object.contains_key("identity_policy") || object.contains_key("identity_policies")
        })
        .unwrap_or(false)
}

/// P0-L step2：bootstrap/revise 的 role_worker_v1 门禁（事务内调用）。
///
/// expected adjudicator Role Worker credential 必须在同一事务内通过校验并追加
/// append-only runtime provenance；返回已校验的 `RoleWorkerAuth` 供调用方做事件归属。
/// 独立 reviewer proof 由调用方按路径强制：legacy 路径用
/// `validate_reviewer_lease_for_adjudication`，worker 路径用
/// `validate_reviewer_lease_proof_server_side`（P0-L R2，禁止 raw token）。
fn enforce_role_worker_governance_write(
    tx: &Transaction<'_>,
    params: &Value,
    peer: &PeerCredential,
    bound_workspace: i64,
    task_id: &str,
    method: &str,
    missing_message: &str,
) -> Result<RoleWorkerAuth, DaemonRpcError> {
    let auth = parse_role_worker_auth(params)?
        .ok_or_else(|| DaemonRpcError::new(ERR_POLICY_REQUIRED, missing_message))?;
    validate_role_worker(
        tx,
        &auth,
        &peer.owner_key(),
        bound_workspace,
        task_id,
        method,
        "adjudicator",
    )?;
    Ok(auth)
}

/// P0-L：读取任务当前（最新）Task Contract 的 identity policy。
///
/// 供 bootstrap/revise、next_action、claim 的 policy 路由使用；缺失或非法值返回
/// None，由调用方按 fail-closed 语义拒绝，绝不静默降级为任一 policy。
pub(crate) fn get_current_task_contract_policy(
    conn: &Connection,
    task_id: &str,
) -> Result<Option<String>, DaemonRpcError> {
    let payload: Option<String> = conn
        .query_row(
            "SELECT envelope_payload FROM task_contract_revisions \
             WHERE task_id = ?1 ORDER BY revision DESC LIMIT 1",
            params![task_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("查询 task contract policy 失败: {e}"))
        })?;
    Ok(payload.as_deref().and_then(policy_from_envelope_payload))
}

/// P0-L step3：任务合同 identity policy 三态（供 claim / next_action 区分 fail-closed 语义）。
#[derive(Clone, Debug, PartialEq)]
pub(crate) enum TaskContractPolicyState {
    /// 不存在任何合同 revision：P0-L 之前的历史任务 → 保持 legacy 原路径，绝不引入新门禁。
    NoContractRevision,
    /// 存在合同 revision 但 identity_policy 缺失/为空 → 禁止隐式降级到任一 policy，
    /// 调用方必须 fail-closed 拒绝（或在只读投影中呈现 diagnosis）。
    Unresolved,
    /// 具体 policy 声明（可能是未知字符串，由调用方判定合法性并 fail-closed）。
    Declared(String),
}

/// P0-L step3：读取任务当前（最新）合同 revision 的 identity policy 三态。
///
/// 与 `get_current_task_contract_policy` 的区别：本函数区分「无合同」与「合同但无合法
/// policy」，claim 必须对后者 fail-closed，绝不静默按 legacy 放行。
pub(crate) fn get_current_task_contract_policy_state(
    conn: &Connection,
    task_id: &str,
) -> Result<TaskContractPolicyState, DaemonRpcError> {
    let payload: Option<String> = conn
        .query_row(
            "SELECT envelope_payload FROM task_contract_revisions \
             WHERE task_id = ?1 ORDER BY revision DESC LIMIT 1",
            params![task_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("查询 task contract policy 状态失败: {e}"))
        })?;
    let Some(payload) = payload else {
        return Ok(TaskContractPolicyState::NoContractRevision);
    };
    Ok(match policy_from_envelope_payload(&payload) {
        Some(policy) => TaskContractPolicyState::Declared(policy),
        None => TaskContractPolicyState::Unresolved,
    })
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
    Ok(row.map(
        |(
            contract_id,
            skill_id,
            skill_version,
            prompt_template_id,
            prompt_hash,
            allowed_paths,
            forbidden_paths,
            commands,
            acceptance_checks,
            required_evidence,
            handoff_to,
            independence,
            revision,
            created_by,
        )| {
            let mut m = Map::new();
            m.insert("contract_id".to_string(), Value::String(contract_id));
            m.insert("task_id".to_string(), Value::String(task_id.to_string()));
            m.insert("role".to_string(), Value::String(role.to_string()));
            m.insert("skill_id".to_string(), Value::String(skill_id));
            m.insert("skill_version".to_string(), Value::String(skill_version));
            m.insert(
                "prompt_template_id".to_string(),
                Value::String(prompt_template_id),
            );
            m.insert("prompt_hash".to_string(), Value::String(prompt_hash));
            m.insert("allowed_paths".to_string(), Value::String(allowed_paths));
            m.insert(
                "forbidden_paths".to_string(),
                Value::String(forbidden_paths),
            );
            m.insert("commands".to_string(), Value::String(commands));
            m.insert(
                "acceptance_checks".to_string(),
                Value::String(acceptance_checks),
            );
            m.insert(
                "required_evidence".to_string(),
                Value::String(required_evidence),
            );
            m.insert("handoff_to".to_string(), Value::String(handoff_to));
            m.insert("independence".to_string(), Value::String(independence));
            m.insert(
                "revision".to_string(),
                Value::Number(serde_json::Number::from(revision)),
            );
            m.insert("created_by".to_string(), Value::String(created_by));
            m
        },
    ))
}

/// P0-L R1：claim 时校验 `contract_claim` 与当前 Role Contract 的三项匹配
///（skill_id/skill_version/prompt_hash）。worker-first 与 legacy 路径共用，
/// 保证两条路径的合同校验语义完全一致（任一不符即 fail-closed）。
fn verify_contract_claim_match(
    task_id: &str,
    role: &str,
    contract: &Map<String, Value>,
    params: &Value,
) -> Result<(), DaemonRpcError> {
    let claim = params.get("contract_claim").and_then(|v| v.as_object());
    let claim_field = |name: &str| -> String {
        claim
            .and_then(|c| c.get(name))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string()
    };
    let cfield = |name: &str| -> String {
        contract
            .get(name)
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    };
    if !cfield("skill_id").is_empty() && claim_field("skill_id") != cfield("skill_id") {
        return Err(DaemonRpcError::new(
            "E_CONTRACT_SKILL_MISMATCH",
            format!(
                "任务 {} 的 {} 角色合同 skill_id 不符（期望 {}）",
                task_id,
                role,
                cfield("skill_id")
            ),
        ));
    }
    if !cfield("skill_version").is_empty()
        && claim_field("skill_version") != cfield("skill_version")
    {
        return Err(DaemonRpcError::new(
            "E_CONTRACT_VERSION_MISMATCH",
            format!("任务 {} 的 {} 角色合同 skill_version 不符", task_id, role),
        ));
    }
    if !cfield("prompt_hash").is_empty() && claim_field("prompt_hash") != cfield("prompt_hash") {
        return Err(DaemonRpcError::new(
            "E_CONTRACT_PROMPT_MISMATCH",
            format!("任务 {} 的 {} 角色合同 prompt_hash 不符", task_id, role),
        ));
    }
    Ok(())
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
    m.insert(
        "revision".to_string(),
        Value::Number(serde_json::Number::from(r.get::<_, i64>(12)?)),
    );
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
        let step = raw_step
            .as_object()
            .ok_or_else(|| DaemonRpcError::invalid_params("steps 的每一项必须是 JSON object"))?;
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
fn parse_subtasks_from_plan_text(
    plan_text: &str,
) -> Vec<(String, String, Vec<Map<String, Value>>)> {
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
                        (
                            content[..pos].trim().to_string(),
                            content[pos + 1..].trim().to_string(),
                        )
                    } else if let Some(pos) = content.find(':') {
                        (
                            content[..pos].trim().to_string(),
                            content[pos + 1..].trim().to_string(),
                        )
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
        items.push(("Task Execution Step".to_string(), String::new(), Vec::new()));
    }
    items
}

// ============================================
// TaskCollabStore
// ============================================

/// 为既有 v60 任务库补齐 task.report 的 provenance 列。
///
/// `migrate_connection` 对已达当前版本的数据库会短路返回；因此 TaskCollabStore
/// 必须在官方迁移返回后再次做幂等列检查，避免旧库在 report 时才暴露缺列。
/// 这里只追加缺失列，不修改历史事件内容。
fn ensure_task_event_report_provenance_compat(conn: &Connection) -> Result<(), DaemonRpcError> {
    let exists: bool = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='task_events'",
            [],
            |row| row.get::<_, i64>(0).map(|value| value > 0),
        )
        .map_err(|error| {
            DaemonRpcError::internal_error(format!("检查 task_events 表失败: {error}"))
        })?;
    if !exists {
        return Ok(());
    }

    let existing: Vec<String> = {
        let mut statement = conn
            .prepare("PRAGMA table_info(task_events)")
            .map_err(|error| {
                DaemonRpcError::internal_error(format!("读取 task_events 列失败: {error}"))
            })?;
        let rows = statement
            .query_map([], |row| row.get::<_, String>(1))
            .map_err(|error| {
                DaemonRpcError::internal_error(format!("读取 task_events 列失败: {error}"))
            })?;
        let mut columns = Vec::new();
        for row in rows {
            columns.push(row.map_err(|error| {
                DaemonRpcError::internal_error(format!("读取 task_events 列失败: {error}"))
            })?);
        }
        columns
    };

    for (column, definition) in [
        ("request_id", "TEXT DEFAULT ''"),
        ("step_id", "TEXT DEFAULT ''"),
    ] {
        if !existing.iter().any(|name| name == column) {
            conn.execute_batch(&format!(
                "ALTER TABLE task_events ADD COLUMN {column} {definition}"
            ))
            .map_err(|error| {
                DaemonRpcError::internal_error(format!("补齐 task_events.{column} 失败: {error}"))
            })?;
        }
    }
    Ok(())
}

/// 收集任务及其所有后代，不读取客户端侧 workspace 状态。
fn reconciliation_task_ids(
    conn: &Connection,
    root_task_id: &str,
) -> Result<Vec<String>, DaemonRpcError> {
    let mut stmt = conn
        .prepare(
            "WITH RECURSIVE task_tree(id) AS (
                 SELECT id FROM tasks WHERE id = ?1
                 UNION ALL
                 SELECT t.id FROM tasks t JOIN task_tree p ON t.parent_id = p.id
             )
             SELECT id FROM task_tree ORDER BY id ASC",
        )
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("读取 reconciliation task tree 失败: {e}"))
        })?;
    let rows = stmt
        .query_map(params![root_task_id], |row| row.get::<_, String>(0))
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("映射 reconciliation task tree 失败: {e}"))
        })?;
    rows.collect::<Result<Vec<_>, _>>().map_err(|e| {
        DaemonRpcError::internal_error(format!("收集 reconciliation task tree 失败: {e}"))
    })
}

/// 读取用于授权清洗目标的不可变 binding。
fn reconciliation_binding(
    conn: &Connection,
    task_id: &str,
) -> Result<Option<(i64, String)>, DaemonRpcError> {
    conn.query_row(
        "SELECT b.workspace_id, c.workspace_instance_id
         FROM task_workspace_bindings b
         JOIN workspace_authority_captures c
           ON c.workspace_capture_id = b.workspace_capture_id
         WHERE b.task_id = ?1
         ORDER BY c.capture_revision DESC LIMIT 1",
        params![task_id],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )
    .optional()
    .map_err(|e| DaemonRpcError::internal_error(format!("读取 reconciliation binding 失败: {e}")))
}

/// 只选择可由事实确定的修复对象：处于 review 且含非终态步骤的任务。
fn reconciliation_candidates(
    conn: &Connection,
    task_ids: &[String],
    requested_instance: &str,
) -> Result<Vec<Value>, DaemonRpcError> {
    let mut candidates = Vec::new();
    for task_id in task_ids {
        let status: String = conn
            .query_row(
                "SELECT status FROM tasks WHERE id = ?1",
                params![task_id],
                |row| row.get(0),
            )
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 reconciliation task status 失败: {e}"))
            })?;
        if status != "review" {
            continue;
        }
        let (pending, in_progress): (i64, i64) = conn
            .query_row(
                "SELECT
                    COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END), 0)
                 FROM task_steps WHERE task_id = ?1",
                params![task_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 reconciliation step status 失败: {e}"))
            })?;
        let unresolved_failed = unresolved_failed_step_ids(conn, task_id)?;
        if pending == 0 && in_progress == 0 && unresolved_failed.is_empty() {
            continue;
        }
        let (eligible, workspace_id, blocked_reason) = match reconciliation_binding(conn, task_id)?
        {
            None => (
                false,
                None,
                Some(
                    "E_WORKSPACE_AUTHORITY_UNAVAILABLE: task 缺少不可变 workspace binding/capture"
                        .to_string(),
                ),
            ),
            Some((workspace_id, instance)) if instance != requested_instance => (
                false,
                Some(workspace_id),
                Some(format!(
                    "E_WORKSPACE_AUTHORITY_MISMATCH: binding instance={} request={}",
                    instance, requested_instance
                )),
            ),
            Some((workspace_id, _)) => (true, Some(workspace_id), None),
        };
        let reason = if !unresolved_failed.is_empty() {
            format!(
                "review 包含未解决 failed step: {}",
                unresolved_failed.join(",")
            )
        } else {
            format!(
                "review 包含 pending={} in_progress={} 的非终态 step",
                pending, in_progress
            )
        };
        candidates.push(json!({
            "task_id": task_id,
            "from_status": "review",
            "to_status": "in_progress",
            "pending_steps": pending,
            "in_progress_steps": in_progress,
            "unresolved_failed_step_ids": unresolved_failed,
            "reason": reason,
            "eligible": eligible,
            "workspace_id": workspace_id,
            "blocked_reason": blocked_reason,
        }));
    }
    Ok(candidates)
}

fn reconciliation_skip(task_id: &str, code: &str, reason: &str) -> Value {
    json!({"task_id": task_id, "code": code, "reason": reason})
}

fn reconciliation_response(
    root_task_id: &str,
    workspace_instance_id: &str,
    applied: bool,
    planned: Vec<Value>,
    applied_items: Vec<Value>,
    skipped: Vec<Value>,
) -> Value {
    json!({
        "task_id": root_task_id,
        "workspace_instance_id": workspace_instance_id,
        "applied": applied,
        "planned_count": planned.len(),
        "applied_count": applied_items.len(),
        "skipped_count": skipped.len(),
        "planned": planned,
        "applied_items": applied_items,
        "skipped": skipped,
    })
}

/// 重放持久化操作结果，不能把已保存的错误转成成功。
fn replay_reconciliation_result(value: Value) -> Result<Value, DaemonRpcError> {
    if let Some(error) = value.get("error") {
        return Err(DaemonRpcError::new(
            error
                .get("code")
                .and_then(Value::as_str)
                .unwrap_or("E_RECONCILE_REPLAY"),
            error
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("replayed rejected task.reconcile"),
        ));
    }
    Ok(value)
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
            DaemonRpcError::internal_error(format!(
                "无法打开 Task DB {}: {}",
                db_path.as_ref().display(),
                e
            ))
        })?;

        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;")
            .ok();

        Self::migrate_and_verify(&conn)?;
        // P0-H（T-1787277487109-758e56d0）：supersede 表已入 canonical v59 schema，
        // 此处仅做列级 fail-closed 校验（不再以启动期 DDL 创建/掩盖迁移）。
        validate_supersede_schema(&conn)?;

        let max_seq: i64 = conn
            .query_row(
                "SELECT COALESCE(MAX(monotonic_seq), 0) FROM task_events",
                [],
                |r| r.get(0),
            )
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
            .query_row(
                "SELECT COALESCE(MAX(monotonic_seq), 0) FROM task_events",
                [],
                |r| r.get(0),
            )
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
        migrate_connection(conn).map_err(|e| {
            DaemonRpcError::internal_error(format!("官方 schema migration 失败: {}", e))
        })?;
        ensure_task_event_report_provenance_compat(conn)?;

        // 2. 读取实际 schema 版本并与编译期常量比较（fail-closed：版本不符拒绝服务）
        let actual = current_schema_version(conn).map_err(|e| {
            DaemonRpcError::internal_error(format!("读取 schema_version 失败: {}", e))
        })?;
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
                let rows = stmt.query_map([], |r| r.get::<_, String>(1)).map_err(|e| {
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
                task_id,
                from_status,
                to_status,
                reason_code,
                reason,
                actor_identity,
                actor_session_id,
                role,
                seq,
                ts
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("追加 task_events 失败: {}", e)))?;
        Ok(tx.last_insert_rowid())
    }

    /// 获取任务当前 claim 的声明者、agent_session_id 与声明角色。
    ///
    /// 只看 claim 生命周期事件，而不是所有 `to_status='in_progress'` 事件。
    /// 这样 `task.claim.recover` 追加 `claim_released` 后，旧 claim 不会继续
    /// 被当成当前 owner；历史 claimed/reported 事件保持不可变。
    fn get_task_claim_details(
        &self,
        conn: &Connection,
        task_id: &str,
    ) -> (Option<String>, Option<String>, Option<String>) {
        let res: Result<(String, String, String, String), _> = conn.query_row(
            "SELECT reason_code, actor_identity, agent_session_id, role FROM task_events
             WHERE task_id = ?1 AND reason_code IN ('claimed', 'claim_recovered', 'claim_released')
             ORDER BY monotonic_seq DESC LIMIT 1",
            params![task_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        );
        match res {
            Ok((reason, actor, session, role)) if reason != "claim_released" => {
                (Some(actor), Some(session), Some(role))
            }
            Err(_) => (None, None, None),
            _ => (None, None, None),
        }
    }

    fn get_task_claim_info(
        &self,
        conn: &Connection,
        task_id: &str,
    ) -> (Option<String>, Option<String>) {
        let (actor, session, _) = self.get_task_claim_details(conn, task_id);
        (actor, session)
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
        let (
            i_instance,
            i_client,
            i_provider,
            i_model,
            i_mode,
            i_fingerprint,
            i_session,
            i_role,
            i_runtime,
        ) = if let Some(raw) = params.get("identity") {
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
                agent_id,
                agent_name,
                owner_key,
                capabilities,
                ts,
                ts,
                i_instance,
                i_client,
                i_provider,
                i_model,
                i_mode,
                i_fingerprint,
                i_runtime,
                i_session,
                i_role
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("agent_register 失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("agent_id".to_string(), Value::String(agent_id));
        res.insert(
            "status".to_string(),
            Value::String("registered".to_string()),
        );
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
        res.insert(
            "timestamp".to_string(),
            Value::Number(serde_json::Number::from_f64(ts).unwrap()),
        );
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
            Some(value) => value.as_array().ok_or_else(|| {
                DaemonRpcError::invalid_params("role_contracts 必须是 JSON array")
            })?,
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
            params![
                task_id,
                title,
                description,
                peer.owner_key(),
                ts,
                ts,
                parent_id
            ],
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

        // assignment 是 task_events 上的可重放投影：新任务先排入 Executor
        // 队列，领取和后续 handoff 再追加同一 assignment 的事件。
        let initial_step_id: Option<String> = tx
            .query_row(
                "SELECT id FROM task_steps WHERE task_id = ?1 AND status = 'pending'
                 ORDER BY step_index ASC LIMIT 1",
                params![task_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("查询初始 assignment 步骤失败: {e}")))?;
        let initial_assignment_id = assignment_queue::queue_assignment(
            &tx,
            &task_id,
            initial_step_id.as_deref(),
            "executor",
            &format!("task.create:{task_id}"),
            None,
            &peer.owner_key(),
            "",
            self.next_seq(),
            ts,
        )?;

        // A3：Planner 通过 task.create 一次性冻结 Role Contract（revision=1）。
        // 同一事务继续写入现代 Task Contract/lineage/step binding；不能只留下
        // legacy role_contracts，否则 next-action 会把新任务判为无合同而阻断。
        // P0-L step1：governance 任务必须先解析并原子持久化 canonical identity_policy，
        // missing/unknown/multiple/mismatched 一律 fail-closed 回滚整个事务。
        let mut governance_projection = None;
        if !role_contracts.is_empty() {
            let (policy, caller_envelope) = resolve_task_create_identity_policy(params, &task_id)?;
            insert_role_contracts(&tx, &task_id, role_contracts, &peer.owner_key(), ts)?;
            // envelope 缺失时走显式 legacy 兼容通道：生成 generic envelope 并注入 policy；
            // caller envelope 原样保留（绝不忽略、绝不改写其 policy 以外的字段）。
            let envelope = if caller_envelope.is_null() {
                let mut generated =
                    task_create_contract_envelope(&task_id, title, description, steps);
                generated
                    .as_object_mut()
                    .expect("task_create_contract_envelope 必须返回 object")
                    .insert("identity_policy".to_string(), Value::String(policy.clone()));
                generated
            } else {
                caller_envelope
            };
            let projection = bootstrap_task_governance_contracts(
                &tx,
                &BootstrapInput {
                    task_id: task_id.clone(),
                    envelope,
                    created_by: peer.owner_key(),
                    role_contract_source: "legacy".to_string(),
                },
                workspace_id,
            )?;
            // exact match：事务内回读已持久化 envelope 的 policy，与 caller 输入比对；
            // 不一致即拒绝提交（整事务回滚），杜绝声明与持久化漂移。
            let persisted_payload: String = tx
                .query_row(
                    "SELECT envelope_payload FROM task_contract_revisions \
                     WHERE task_id = ?1 ORDER BY revision DESC LIMIT 1",
                    params![task_id],
                    |row| row.get(0),
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("回读 task contract envelope 失败: {e}"))
                })?;
            if policy_from_envelope_payload(&persisted_payload).as_deref() != Some(policy.as_str())
            {
                return Err(DaemonRpcError::new(
                    ERR_POLICY_MISMATCH,
                    format!(
                        "持久化 Task Contract 的 identity_policy 与 caller 声明 {policy} 不一致"
                    ),
                ));
            }
            let mut projection = projection;
            projection
                .as_object_mut()
                .expect("bootstrap projection 必须是 object")
                .insert("identity_policy".to_string(), Value::String(policy));
            governance_projection = Some(projection);
        }

        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 task_create 事务失败: {}", e))
        })?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id));
        res.insert("status".to_string(), Value::String("open".to_string()));
        res.insert("title".to_string(), Value::String(title.to_string()));
        res.insert(
            "step_count".to_string(),
            Value::Number(serde_json::Number::from(steps.len())),
        );
        res.insert(
            "contract_count".to_string(),
            Value::Number(serde_json::Number::from(role_contracts.len())),
        );
        if let Some(projection) = governance_projection {
            res.insert("governance_projection".to_string(), projection);
        }
        res.insert(
            "monotonic_seq".to_string(),
            Value::Number(serde_json::Number::from(seq)),
        );
        res.insert(
            "workspace_id".to_string(),
            Value::Number(serde_json::Number::from(workspace_id)),
        );
        res.insert(
            "workspace_binding_id".to_string(),
            Value::String(binding_id),
        );
        res.insert(
            "workspace_capture_id".to_string(),
            Value::String(capture_id),
        );
        res.insert(
            "assignment_id".to_string(),
            initial_assignment_id.map(Value::String).unwrap_or(Value::Null),
        );
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

}
#[cfg(test)]
#[path = "task_collab_tests.rs"]
mod tests;
