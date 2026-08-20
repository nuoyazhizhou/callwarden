//! 1F：Task lifecycle 与 lease wrapper integration（cw-role-handoff-task-loop.md §8.1.6）。
//!
//! 只将 `task.apply`、`task.close`、`lease.acquire`、`lease.renew`/`lease.extend`、
//! `lease.release` 接入统一 `TaskMutationExecutor` wrapper（§3.3/§4.3）；只编辑本模块。
//! 不得修改 wrapper、task.create/contract_set/claim/report/handoff/verdict handler 或
//! 任何 schema（所有权边界见 `mod.rs`）。
//!
//! 语义对齐 daemon 既有权威路径（Req 11.2-11.9；`task_collab.rs`）：
//! - 同 task+role 任一时刻只有一个 active lease（`idx_task_leases_active_unique` 防双活）；
//! - 数据库只存 token hash（sha256），raw token 仅在 acquire 本次成功响应返回一次：
//!   ledger 只落 `token_hash`，raw token 由入口函数在 wrapper 返回后追加，杜绝持久化；
//! - fencing counter 单调递增：该 task+role 全历史 MAX + 1（Req 11.3）；
//! - acquire：已过期 active lease 置 expired；未过期但 holder 注册缺失/非 active/心跳
//!   stale 的 lease 在同一事务回收（追加 `expire` 审计事件，审计链保留旧 counter）；
//! - renew（含 alias `lease.extend`）：token hash/未过期/holder identity/fencing counter
//!   校验通过后仅续期（幂等，不递增 counter、不新建 lease，Req 11.4-11.5）；
//! - release：token 匹配时置 released 并追加事件；重复 release 幂等返回同 released 状态
//!   （Req 11.6-11.7）；
//! - apply/close 必须持有完整 reviewer lease 凭证（token + fencing counter + holder
//!   identity），任一缺失/失配 fail-closed（E_LEASE_REQUIRED / E_LEASE_*）。
//!
//! 每个方法以 `*_entry` 构造 `StrictParsedEnvelope`（`InvocationClass::ExternalTransport`）
//! 并委托 `TaskMutationExecutor`；领域语义由 `apply_*` 回调承担，错误分确定性
//! （`DeterministicReject` → savepoint 回滚 + 可重放 ledger error）与基础设施
//! （`Internal` → 回滚 outer transaction）两类（§3.3）。cutover（1D3A/1D3B）前 route
//! 仍 fail-closed，本入口供领域测试与 1F 验收复用。

use rusqlite::{Connection, OptionalExtension};
use sha2::{Digest, Sha256};

use crate::daemon::dispatch::DaemonRpcError;
use super::executor::TaskMutationExecutor;
use super::types::{
    DomainOutcome, FrozenAuthorityInput, InfrastructureError, InvocationClass,
    StableDomainError, StrictParsedEnvelope, TaskDomainTx,
};

/// 事务/savepoint/ledger 基础设施失败（InfrastructureError 语义，回滚 outer tx）。
pub const ERR_TASK_DB_TRANSACTION: &str = "E_TASK_DB_TRANSACTION";
/// 确定性拒绝：task 没有不可变 task workspace binding，无法确定 lease 归属。
pub const ERR_TASK_BINDING_REQUIRED: &str = "E_TASK_BINDING_REQUIRED";
/// 确定性拒绝：受保护写操作缺少完整 lease 凭证（token + fencing counter）。
pub const ERR_LEASE_REQUIRED: &str = "E_LEASE_REQUIRED";
/// 确定性拒绝：无 active lease。
pub const ERR_LEASE_NOT_FOUND: &str = "E_LEASE_NOT_FOUND";
/// 确定性拒绝：token hash 不匹配。
pub const ERR_LEASE_TOKEN_MISMATCH: &str = "E_LEASE_TOKEN_MISMATCH";
/// 确定性拒绝：lease 已过期（权威时钟判定）。
pub const ERR_LEASE_EXPIRED: &str = "E_LEASE_EXPIRED";
/// 确定性拒绝：fencing counter 与当前 active lease 不一致（Property 11）。
pub const ERR_LEASE_FENCING_STALE: &str = "E_LEASE_FENCING_STALE";
/// 确定性拒绝：holder Identity 与 lease 不一致。
pub const ERR_LEASE_HOLDER_MISMATCH: &str = "E_LEASE_HOLDER_MISMATCH";
/// 确定性拒绝：同 task+role 已存在未过期 active lease（Req 11.2）。
pub const ERR_LEASE_ACTIVE_EXISTS: &str = "E_LEASE_ACTIVE_EXISTS";
/// 确定性拒绝：唯一索引防双活冲突（并发 acquire 竞态，fail-closed）。
pub const ERR_LEASE_ALREADY_ACTIVE: &str = "E_LEASE_ALREADY_ACTIVE";
/// 确定性拒绝：身份不完整（acquire 需要 agent_id/session_id/model_id）。
pub const ERR_IDENTITY_INCOMPLETE: &str = "E_IDENTITY_INCOMPLETE";
/// 确定性拒绝：close 父任务存在未关闭子任务。
pub const ERR_CHILD_TASKS_NOT_CLOSED: &str = "E_CHILD_TASKS_NOT_CLOSED";
/// 确定性拒绝：close 叶子任务无步骤记录。
pub const ERR_NO_STEPS: &str = "E_NO_STEPS";
/// 确定性拒绝：close 叶子任务存在 pending/failed/blocked 步骤。
pub const ERR_STEPS_NOT_DONE: &str = "E_STEPS_NOT_DONE";
/// 确定性拒绝：任务不存在。
pub const ERR_TASK_NOT_FOUND: &str = "task_not_found";

/// holder 注册/心跳 stale 判定阈值（对齐 task_collab ORPHAN_CLAIM_STALE_SECS）。
const ORPHAN_CLAIM_STALE_SECS: f64 = 15.0 * 60.0;
/// acquire 的默认 TTL（秒，对齐 daemon 默认 3600）。
const DEFAULT_LEASE_TTL: f64 = 3600.0;
/// apply/close 使用的固定 lease role（reviewer 受保护写）。
const REVIEWER_ROLE: &str = "reviewer";

/// `task.apply`/`task.close`/`lease.*` 的 ledger dedup key
/// （固定 (workspace_instance_id, method, request_id)）。
pub struct LedgerKey {
    pub workspace_instance_id: String,
    pub method: String,
    pub request_id: String,
}

/// 结构化 Identity（Req 10.1）。与任务/lease 绑定，禁止由自由文本补齐。
#[derive(Debug, Clone)]
pub struct LeaseIdentity {
    pub agent_id: String,
    pub session_id: String,
    pub model_id: String,
    pub role: String,
}

impl LeaseIdentity {
    fn from_params(params: &serde_json::Value) -> Result<Option<Self>, DaemonRpcError> {
        let id = match params.get("identity") {
            None | Some(serde_json::Value::Null) => return Ok(None),
            Some(v) => v,
        };
        let get = |key: &str| -> Result<String, DaemonRpcError> {
            id.get(key).and_then(|v| v.as_str()).map(|s| s.to_string()).ok_or_else(|| {
                DaemonRpcError::invalid_params(format!("identity.{key} 必须是字符串"))
            })
        };
        Ok(Some(LeaseIdentity {
            agent_id: get("agent_id")?,
            session_id: get("session_id")?,
            model_id: get("model_id")?,
            role: id.get("role").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        }))
    }
}

/// `task.apply` 的领域输入。
#[derive(Debug)]
pub struct ApplyInput {
    pub task_id: String,
    pub reviewer: String,
    pub lease_token: String,
    pub fencing_counter: i64,
    pub identity: Option<LeaseIdentity>,
}

impl ApplyInput {
    /// 从 `StrictParsedEnvelope.params` 严格解析（1D3A cutover 复用）。
    pub fn from_params(params: &serde_json::Value) -> Result<Self, DaemonRpcError> {
        let task_id = params.get("task_id").and_then(|v| v.as_str()).map(|s| s.to_string())
            .ok_or_else(|| DaemonRpcError::invalid_params("task.apply 缺少字段: task_id"))?;
        let lease_token = params.get("lease_token").and_then(|v| v.as_str()).map(|s| s.to_string())
            .ok_or_else(|| DaemonRpcError::invalid_params("task.apply 缺少字段: lease_token"))?;
        let fencing_counter = params.get("fencing_counter").and_then(|v| v.as_i64())
            .ok_or_else(|| DaemonRpcError::invalid_params("task.apply 缺少字段: fencing_counter"))?;
        Ok(ApplyInput {
            task_id,
            reviewer: params.get("reviewer").and_then(|v| v.as_str()).unwrap_or(REVIEWER_ROLE).to_string(),
            lease_token,
            fencing_counter,
            identity: LeaseIdentity::from_params(params)?,
        })
    }
}

/// `task.close` 的领域输入（与 apply 同形状）。
#[derive(Debug)]
pub struct CloseInput {
    pub task_id: String,
    pub reviewer: String,
    pub lease_token: String,
    pub fencing_counter: i64,
    pub identity: Option<LeaseIdentity>,
}

impl CloseInput {
    /// 从 `StrictParsedEnvelope.params` 严格解析（1D3A cutover 复用）。
    pub fn from_params(params: &serde_json::Value) -> Result<Self, DaemonRpcError> {
        let task_id = params.get("task_id").and_then(|v| v.as_str()).map(|s| s.to_string())
            .ok_or_else(|| DaemonRpcError::invalid_params("task.close 缺少字段: task_id"))?;
        let lease_token = params.get("lease_token").and_then(|v| v.as_str()).map(|s| s.to_string())
            .ok_or_else(|| DaemonRpcError::invalid_params("task.close 缺少字段: lease_token"))?;
        let fencing_counter = params.get("fencing_counter").and_then(|v| v.as_i64())
            .ok_or_else(|| DaemonRpcError::invalid_params("task.close 缺少字段: fencing_counter"))?;
        Ok(CloseInput {
            task_id,
            reviewer: params.get("reviewer").and_then(|v| v.as_str()).unwrap_or(REVIEWER_ROLE).to_string(),
            lease_token,
            fencing_counter,
            identity: LeaseIdentity::from_params(params)?,
        })
    }
}

/// `lease.acquire` 的领域输入。
#[derive(Debug)]
pub struct AcquireInput {
    pub task_id: String,
    pub role: String,
    pub ttl_seconds: f64,
    pub identity: LeaseIdentity,
}

impl AcquireInput {
    /// 从 `StrictParsedEnvelope.params` 严格解析（1D3A cutover 复用）。
    pub fn from_params(params: &serde_json::Value) -> Result<Self, DaemonRpcError> {
        let task_id = params.get("task_id").and_then(|v| v.as_str()).map(|s| s.to_string())
            .ok_or_else(|| DaemonRpcError::invalid_params("lease.acquire 缺少字段: task_id"))?;
        let role = params.get("role").and_then(|v| v.as_str()).map(|s| s.to_string())
            .ok_or_else(|| DaemonRpcError::invalid_params("lease.acquire 缺少字段: role"))?;
        let ttl = params.get("ttl_seconds").and_then(|v| v.as_f64()).unwrap_or(DEFAULT_LEASE_TTL);
        if ttl <= 0.0 {
            return Err(DaemonRpcError::invalid_params("ttl_seconds 必须大于 0"));
        }
        let identity = LeaseIdentity::from_params(params)?.ok_or_else(|| {
            DaemonRpcError::invalid_params("lease.acquire 需要 identity（agent_id/session_id/model_id）")
        })?;
        Ok(AcquireInput { task_id, role, ttl_seconds: ttl, identity })
    }
}

/// `lease.renew`（含 alias `lease.extend`）的领域输入。
#[derive(Debug)]
pub struct RenewInput {
    pub task_id: String,
    pub role: String,
    pub token: String,
    pub ttl_seconds: f64,
    pub fencing_counter: Option<i64>,
    pub identity: Option<LeaseIdentity>,
}

impl RenewInput {
    /// 从 `StrictParsedEnvelope.params` 严格解析（1D3A cutover 复用）。
    pub fn from_params(params: &serde_json::Value) -> Result<Self, DaemonRpcError> {
        let task_id = params.get("task_id").and_then(|v| v.as_str()).map(|s| s.to_string())
            .ok_or_else(|| DaemonRpcError::invalid_params("lease.renew 缺少字段: task_id"))?;
        let role = params.get("role").and_then(|v| v.as_str()).map(|s| s.to_string())
            .ok_or_else(|| DaemonRpcError::invalid_params("lease.renew 缺少字段: role"))?;
        let token = params.get("token").and_then(|v| v.as_str()).map(|s| s.to_string())
            .ok_or_else(|| DaemonRpcError::invalid_params("lease.renew 缺少字段: token"))?;
        let ttl = params.get("ttl_seconds").and_then(|v| v.as_f64()).unwrap_or(DEFAULT_LEASE_TTL);
        if ttl <= 0.0 {
            return Err(DaemonRpcError::invalid_params("ttl_seconds 必须大于 0"));
        }
        Ok(RenewInput {
            task_id,
            role,
            token,
            ttl_seconds: ttl,
            fencing_counter: params.get("fencing_counter").and_then(|v| v.as_i64()),
            identity: LeaseIdentity::from_params(params)?,
        })
    }
}

/// `lease.release` 的领域输入。
#[derive(Debug)]
pub struct ReleaseInput {
    pub task_id: String,
    pub role: String,
    pub token: String,
    pub identity: Option<LeaseIdentity>,
}

impl ReleaseInput {
    /// 从 `StrictParsedEnvelope.params` 严格解析（1D3A cutover 复用）。
    pub fn from_params(params: &serde_json::Value) -> Result<Self, DaemonRpcError> {
        let task_id = params.get("task_id").and_then(|v| v.as_str()).map(|s| s.to_string())
            .ok_or_else(|| DaemonRpcError::invalid_params("lease.release 缺少字段: task_id"))?;
        let role = params.get("role").and_then(|v| v.as_str()).map(|s| s.to_string())
            .ok_or_else(|| DaemonRpcError::invalid_params("lease.release 缺少字段: role"))?;
        let token = params.get("token").and_then(|v| v.as_str()).map(|s| s.to_string())
            .ok_or_else(|| DaemonRpcError::invalid_params("lease.release 缺少字段: token"))?;
        Ok(ReleaseInput {
            task_id,
            role,
            token,
            identity: LeaseIdentity::from_params(params)?,
        })
    }
}

/// 领域写入成功后的响应（wrapper 统一落 ledger result）。
struct DomainWriteOk {
    response: serde_json::Value,
}

/// 领域执行期间的失败类别（封闭、类型化）。
enum LifecycleDomainError {
    /// 确定性、可重放失败：外层在 savepoint 回滚后写可重放 ledger error 并 commit。
    Deterministic { code: String, message: String },
    /// 基础设施失败：回滚 outer transaction、领域写入与 ledger result。
    Infrastructure(DaemonRpcError),
}

impl LifecycleDomainError {
    fn det(code: &str, message: String) -> Self {
        LifecycleDomainError::Deterministic { code: code.to_string(), message }
    }
    fn infra(e: DaemonRpcError) -> Self {
        LifecycleDomainError::Infrastructure(e)
    }
    fn infra_msg(e: rusqlite::Error, context: &str) -> Self {
        LifecycleDomainError::Infrastructure(DaemonRpcError::new(
            ERR_TASK_DB_TRANSACTION,
            format!("{context}: {e}"),
        ))
    }
    /// 把本地失败类别映射为 wrapper 要求的封闭 `DomainOutcome`（§3.3）。
    fn into_outcome(self) -> DomainOutcome {
        match self {
            LifecycleDomainError::Deterministic { code, message: _ } => {
                DomainOutcome::CommitDeterministicError {
                    stable_error: StableDomainError::DeterministicReject { code },
                }
            }
            LifecycleDomainError::Infrastructure(error) => {
                DomainOutcome::RollbackInfrastructureError {
                    infrastructure_error: InfrastructureError::Internal {
                        detail: error.message,
                    },
                }
            }
        }
    }
}

/// 受保护写操作（apply/close）前校验 reviewer lease（Req 11.2-11.9）。
///
/// 读路径：查询 (workspace_id, task_id, role) 的 active lease；任一凭证失配即返回
/// 确定性拒绝。校验在任何写入前完成，不改变 task/lease 数据。
fn validate_lease_for_mutation(
    tx: &Connection,
    workspace_id: i64,
    task_id: &str,
    role: &str,
    token: &str,
    fencing_counter: i64,
    identity: Option<&LeaseIdentity>,
) -> Result<(), LifecycleDomainError> {
    let now = now_unix();
    let lease = tx
        .query_row(
            "SELECT id, lease_id, token_hash, fencing_counter, expires_at, \
                    agent_id, session_id, model_id \
             FROM task_leases \
             WHERE workspace_id = ?1 AND task_id = ?2 AND role = ?3 AND status = 'active' \
             ORDER BY id ASC LIMIT 1",
            rusqlite::params![workspace_id, task_id, role],
            |r| {
                Ok(LeaseRow {
                    id: r.get(0)?,
                    lease_id: r.get(1)?,
                    token_hash: r.get(2)?,
                    fencing_counter: r.get(3)?,
                    expires_at: r.get(4)?,
                    agent_id: r.get(5)?,
                    session_id: r.get(6)?,
                    model_id: r.get(7)?,
                })
            },
        )
        .optional()
        .map_err(|e| LifecycleDomainError::infra_msg(e, "查询 active lease 失败"))?
        .ok_or_else(|| {
            LifecycleDomainError::det(
                ERR_LEASE_NOT_FOUND,
                format!("task={task_id} role={role} 无 active lease，受保护写操作需要先 acquire"),
            )
        })?;
    if sha256_hex(token.as_bytes()) != lease.token_hash {
        return Err(LifecycleDomainError::det(
            ERR_LEASE_TOKEN_MISMATCH,
            format!("token hash 不匹配 (lease_id={})", lease.lease_id),
        ));
    }
    if now > lease.expires_at {
        return Err(LifecycleDomainError::det(
            ERR_LEASE_EXPIRED,
            format!("lease {} 已过期 (expires_at={:.1}, now={now:.1})", lease.lease_id, lease.expires_at),
        ));
    }
    if fencing_counter != lease.fencing_counter {
        return Err(LifecycleDomainError::det(
            ERR_LEASE_FENCING_STALE,
            format!("fencing counter {fencing_counter} != 当前 {}；旧持有者写入被拒绝", lease.fencing_counter),
        ));
    }
    if let Some(id) = identity {
        if id.agent_id != lease.agent_id || id.session_id != lease.session_id || id.model_id != lease.model_id {
            return Err(LifecycleDomainError::det(
                ERR_LEASE_HOLDER_MISMATCH,
                format!("holder Identity 与 lease ({}) 不一致", lease.lease_id),
            ));
        }
    }
    Ok(())
}

/// active lease 查询行。
struct LeaseRow {
    id: i64,
    lease_id: String,
    token_hash: String,
    fencing_counter: i64,
    expires_at: f64,
    agent_id: String,
    session_id: String,
    model_id: String,
}

/// 从不可变 `task_workspace_bindings` 解析 task 的 workspace_id（lease 归属唯一权威）。
fn workspace_id_of(tx: &Connection, task_id: &str) -> Result<i64, LifecycleDomainError> {
    tx.query_row(
        "SELECT workspace_id FROM task_workspace_bindings WHERE task_id = ?1",
        [task_id],
        |row| row.get(0),
    )
    .map_err(|e| {
        LifecycleDomainError::det(
            ERR_TASK_BINDING_REQUIRED,
            format!("task {task_id} 没有不可变 task workspace binding（{e}）"),
        )
    })
}

/// 当前任务的事件单调序号（MAX + 1；同任务内递增，不依赖全局计数器）。
fn next_monotonic_seq(tx: &Connection, task_id: &str) -> Result<i64, LifecycleDomainError> {
    tx.query_row(
        "SELECT COALESCE(MAX(monotonic_seq), 0) FROM task_events WHERE task_id = ?1",
        [task_id],
        |r| r.get::<_, i64>(0),
    )
    .map(|v| v + 1)
    .map_err(|e| LifecycleDomainError::infra_msg(e, "读取任务事件序号失败"))
}

/// 追加一条 state transition `task_events`（append-only，Req 11.6/11.12）。
#[allow(clippy::too_many_arguments)]
fn append_task_event(
    tx: &Connection,
    workspace_id: i64,
    task_id: &str,
    from_status: &str,
    to_status: &str,
    reason_code: &str,
    actor: &LeaseIdentity,
    seq: i64,
    ts: f64,
) -> Result<(), LifecycleDomainError> {
    tx.execute(
        "INSERT INTO task_events \
         (task_id, workspace_id, from_status, to_status, reason_code, reason, \
          actor_identity, agent_session_id, role, monotonic_seq, authoritative_timestamp) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
        rusqlite::params![
            task_id,
            workspace_id.to_string(),
            from_status,
            to_status,
            reason_code,
            format!("task {to_status}"),
            actor.agent_id,
            actor.session_id,
            actor.role,
            seq,
            ts,
        ],
    )
    .map_err(|e| LifecycleDomainError::infra_msg(e, "追加 task_events 失败"))
    .map(|_| ())
}

/// 记录 state transition 的结构化 action identity（Req 10.1；写入 action_identities）。
fn record_action_identity(
    tx: &Connection,
    workspace_id: i64,
    task_id: &str,
    identity: &LeaseIdentity,
    seq: i64,
    ts: f64,
) -> Result<(), LifecycleDomainError> {
    let action_id = format!("ACT-state_transition-{task_id}-{seq}");
    tx.execute(
        "INSERT INTO action_identities \
         (workspace_id, action_id, action_type, task_id, contract_id, contract_revision, \
          agent_id, session_id, model_id, role, recorded_at) \
         VALUES (?1, ?2, 'state_transition', ?3, '', 0, ?4, ?5, ?6, ?7, ?8)",
        rusqlite::params![
            workspace_id, action_id, task_id,
            identity.agent_id, identity.session_id, identity.model_id, identity.role, ts,
        ],
    )
    .map_err(|e| LifecycleDomainError::infra_msg(e, "记录 action identity 失败"))
    .map(|_| ())
}

/// 追加一条 Lease 审计事件（append-only；不写 raw token）。
#[allow(clippy::too_many_arguments)]
fn append_lease_event(
    tx: &Connection,
    workspace_id: i64,
    lease_id: &str,
    task_id: &str,
    role: &str,
    event_type: &str,
    fencing_counter: i64,
    event_at: f64,
    actor: &LeaseIdentity,
    detail: &str,
) -> Result<(), LifecycleDomainError> {
    tx.execute(
        "INSERT INTO task_lease_events \
         (workspace_id, event_id, lease_id, task_id, role, event_type, \
          fencing_counter, event_at, actor_agent_id, actor_session_id, actor_model_id, detail) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
        rusqlite::params![
            workspace_id, gen_lease_event_id(), lease_id, task_id, role, event_type,
            fencing_counter, event_at, actor.agent_id, actor.session_id, actor.model_id, detail,
        ],
    )
    .map_err(|e| LifecycleDomainError::infra_msg(e, "追加 lease 事件失败"))
    .map(|_| ())
}

/// 判断 rusqlite 错误是否为 UNIQUE 约束冲突（防双活，Req 11.2）。
fn is_unique_violation(err: &rusqlite::Error) -> bool {
    matches!(
        err,
        rusqlite::Error::SqliteFailure(e, _) if e.code == rusqlite::ErrorCode::ConstraintViolation
    )
}

// ============================================================================
// `task.apply`
// ============================================================================

/// 以 `task.apply` 领域入口构造 `StrictParsedEnvelope`，委托统一 wrapper（§3.3/§4.3）。
pub fn apply_task(
    conn: &mut Connection,
    frozen: &FrozenAuthorityInput,
    ledger_key: &LedgerKey,
    input: &ApplyInput,
) -> Result<serde_json::Value, DaemonRpcError> {
    let envelope = StrictParsedEnvelope {
        workspace_instance_id: ledger_key.workspace_instance_id.clone(),
        canonical_method: ledger_key.method.clone(),
        request_id: ledger_key.request_id.clone(),
        params: serde_json::json!({
            "task_id": input.task_id,
            "reviewer": input.reviewer,
            "lease_token": input.lease_token,
            "fencing_counter": input.fencing_counter,
            "identity": {
                "agent_id": input.identity.as_ref().map(|i| i.agent_id.as_str()).unwrap_or(""),
                "session_id": input.identity.as_ref().map(|i| i.session_id.as_str()).unwrap_or(""),
                "model_id": input.identity.as_ref().map(|i| i.model_id.as_str()).unwrap_or(""),
                "role": input.identity.as_ref().map(|i| i.role.as_str()).unwrap_or(""),
            },
        }),
        invocation_class: InvocationClass::ExternalTransport,
    };
    TaskMutationExecutor::default().run(conn, &envelope, frozen, |domain_tx, frozen_ref| {
        apply_apply(domain_tx, frozen_ref, input)
    })
}

/// `task.apply` 领域回调。
fn apply_apply(
    tx: &mut TaskDomainTx<'_>,
    _frozen: &FrozenAuthorityInput,
    input: &ApplyInput,
) -> DomainOutcome {
    match write_apply(tx.tx(), input) {
        Ok(ok) => DomainOutcome::CommitSuccess { response: ok.response },
        Err(err) => err.into_outcome(),
    }
}

/// 在 savepoint 事务内执行 lease 校验 + status 迁移（§8.1.6 / Req 11.2-11.9）。
fn write_apply(tx: &Connection, input: &ApplyInput) -> Result<DomainWriteOk, LifecycleDomainError> {
    let workspace_id = workspace_id_of(tx, &input.task_id)?;
    validate_lease_for_mutation(
        tx,
        workspace_id,
        &input.task_id,
        REVIEWER_ROLE,
        &input.lease_token,
        input.fencing_counter,
        input.identity.as_ref(),
    )?;

    let current_status: String = tx
        .query_row("SELECT status FROM tasks WHERE id = ?1", [&input.task_id], |r| r.get(0))
        .map_err(|_| {
            LifecycleDomainError::det(ERR_TASK_NOT_FOUND, format!("任务不存在: {}", input.task_id))
        })?;
    let ts = now_unix();
    tx.execute(
        "UPDATE tasks SET status = 'applied', applied_at = ?1, updated_at = ?1 WHERE id = ?2",
        rusqlite::params![ts, input.task_id],
    )
    .map_err(|e| LifecycleDomainError::infra_msg(e, "task.apply 失败"))?;

    let actor = input.identity.clone().unwrap_or_else(|| LeaseIdentity {
        agent_id: "reviewer".to_string(),
        session_id: String::new(),
        model_id: String::new(),
        role: REVIEWER_ROLE.to_string(),
    });
    let seq = next_monotonic_seq(tx, &input.task_id)?;
    append_task_event(tx, workspace_id, &input.task_id, &current_status, "applied", "applied", &actor, seq, ts)?;
    if let Some(id) = input.identity.as_ref() {
        record_action_identity(tx, workspace_id, &input.task_id, id, seq, ts)?;
    }

    Ok(DomainWriteOk {
        response: serde_json::json!({
            "task_id": input.task_id,
            "status": "applied",
            "applied_at": ts,
            "reviewer": input.reviewer,
        }),
    })
}

// ============================================================================
// `task.close`
// ============================================================================

/// 以 `task.close` 领域入口构造 `StrictParsedEnvelope`，委托统一 wrapper（§3.3/§4.3）。
pub fn close_task(
    conn: &mut Connection,
    frozen: &FrozenAuthorityInput,
    ledger_key: &LedgerKey,
    input: &CloseInput,
) -> Result<serde_json::Value, DaemonRpcError> {
    let envelope = StrictParsedEnvelope {
        workspace_instance_id: ledger_key.workspace_instance_id.clone(),
        canonical_method: ledger_key.method.clone(),
        request_id: ledger_key.request_id.clone(),
        params: serde_json::json!({
            "task_id": input.task_id,
            "reviewer": input.reviewer,
            "lease_token": input.lease_token,
            "fencing_counter": input.fencing_counter,
            "identity": {
                "agent_id": input.identity.as_ref().map(|i| i.agent_id.as_str()).unwrap_or(""),
                "session_id": input.identity.as_ref().map(|i| i.session_id.as_str()).unwrap_or(""),
                "model_id": input.identity.as_ref().map(|i| i.model_id.as_str()).unwrap_or(""),
                "role": input.identity.as_ref().map(|i| i.role.as_str()).unwrap_or(""),
            },
        }),
        invocation_class: InvocationClass::ExternalTransport,
    };
    TaskMutationExecutor::default().run(conn, &envelope, frozen, |domain_tx, frozen_ref| {
        apply_close(domain_tx, frozen_ref, input)
    })
}

/// `task.close` 领域回调。
fn apply_close(
    tx: &mut TaskDomainTx<'_>,
    _frozen: &FrozenAuthorityInput,
    input: &CloseInput,
) -> DomainOutcome {
    match write_close(tx.tx(), input) {
        Ok(ok) => DomainOutcome::CommitSuccess { response: ok.response },
        Err(err) => err.into_outcome(),
    }
}

/// 在 savepoint 事务内执行 lease 校验 + 子任务/步骤门禁 + status 迁移（§8.1.6）。
fn write_close(tx: &Connection, input: &CloseInput) -> Result<DomainWriteOk, LifecycleDomainError> {
    let workspace_id = workspace_id_of(tx, &input.task_id)?;
    validate_lease_for_mutation(
        tx,
        workspace_id,
        &input.task_id,
        REVIEWER_ROLE,
        &input.lease_token,
        input.fencing_counter,
        input.identity.as_ref(),
    )?;

    let current_status: String = tx
        .query_row("SELECT status FROM tasks WHERE id = ?1", [&input.task_id], |r| r.get(0))
        .map_err(|_| {
            LifecycleDomainError::det(ERR_TASK_NOT_FOUND, format!("任务不存在: {}", input.task_id))
        })?;

    // S1: 子任务状态门禁 —— 存在任何非 closed 子任务时禁止关闭父任务。
    let child_total: i64 = tx
        .query_row("SELECT COUNT(*) FROM tasks WHERE parent_id = ?1", [&input.task_id], |r| r.get(0))
        .map_err(|e| LifecycleDomainError::infra_msg(e, "子任务计数失败"))?;
    if child_total > 0 {
        let open_children: i64 = tx
            .query_row(
                "SELECT COUNT(*) FROM tasks WHERE parent_id = ?1 AND status != 'closed'",
                [&input.task_id],
                |r| r.get(0),
            )
            .map_err(|e| LifecycleDomainError::infra_msg(e, "未关闭子任务计数失败"))?;
        if open_children > 0 {
            return Err(LifecycleDomainError::det(
                ERR_CHILD_TASKS_NOT_CLOSED,
                format!("任务 {} 存在 {open_children} 个未关闭子任务，禁止关闭", input.task_id),
            ));
        }
    } else {
        // S2: 叶子任务步骤门禁 —— 必须有步骤且全部 done/skipped 才能关闭。
        let step_count: i64 = tx
            .query_row("SELECT COUNT(*) FROM task_steps WHERE task_id = ?1", [&input.task_id], |r| r.get(0))
            .map_err(|e| LifecycleDomainError::infra_msg(e, "步骤计数失败"))?;
        if step_count == 0 {
            return Err(LifecycleDomainError::det(
                ERR_NO_STEPS,
                format!("任务 {} 无步骤记录，禁止关闭", input.task_id),
            ));
        }
        let not_done: i64 = tx
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE task_id = ?1 AND status IN ('pending', 'failed', 'blocked')",
                [&input.task_id],
                |r| r.get(0),
            )
            .map_err(|e| LifecycleDomainError::infra_msg(e, "未完成步骤计数失败"))?;
        if not_done > 0 {
            return Err(LifecycleDomainError::det(
                ERR_STEPS_NOT_DONE,
                format!("任务 {} 存在 {not_done} 个未完成步骤，禁止关闭", input.task_id),
            ));
        }
    }

    // S3: closed_at 写入真实非零时间戳（权威时钟）。
    let ts = now_unix();
    tx.execute(
        "UPDATE tasks SET status = 'closed', closed_at = ?1, updated_at = ?1 WHERE id = ?2",
        rusqlite::params![ts, input.task_id],
    )
    .map_err(|e| LifecycleDomainError::infra_msg(e, "task.close 失败"))?;

    let actor = input.identity.clone().unwrap_or_else(|| LeaseIdentity {
        agent_id: "reviewer".to_string(),
        session_id: String::new(),
        model_id: String::new(),
        role: REVIEWER_ROLE.to_string(),
    });
    let seq = next_monotonic_seq(tx, &input.task_id)?;
    append_task_event(tx, workspace_id, &input.task_id, &current_status, "closed", "closed", &actor, seq, ts)?;
    if let Some(id) = input.identity.as_ref() {
        record_action_identity(tx, workspace_id, &input.task_id, id, seq, ts)?;
    }

    Ok(DomainWriteOk {
        response: serde_json::json!({
            "task_id": input.task_id,
            "status": "closed",
            "closed_at": ts,
            "reviewer": input.reviewer,
        }),
    })
}

// ============================================================================
// `lease.acquire`
// ============================================================================

/// 以 `lease.acquire` 领域入口构造 `StrictParsedEnvelope`，委托统一 wrapper。
///
/// ledger 只落 `token_hash`；raw token 由本入口在 wrapper 返回后追加（Req 11.2：
/// 数据库只存 sha256，raw token 仅在本次成功响应返回一次）。
pub fn lease_acquire(
    conn: &mut Connection,
    frozen: &FrozenAuthorityInput,
    ledger_key: &LedgerKey,
    input: &AcquireInput,
) -> Result<serde_json::Value, DaemonRpcError> {
    let envelope = StrictParsedEnvelope {
        workspace_instance_id: ledger_key.workspace_instance_id.clone(),
        canonical_method: ledger_key.method.clone(),
        request_id: ledger_key.request_id.clone(),
        params: serde_json::json!({
            "task_id": input.task_id,
            "role": input.role,
            "ttl_seconds": input.ttl_seconds,
            "identity": {
                "agent_id": input.identity.agent_id,
                "session_id": input.identity.session_id,
                "model_id": input.identity.model_id,
                "role": input.identity.role,
            },
        }),
        invocation_class: InvocationClass::ExternalTransport,
    };
    let mut issued_token: Option<String> = None;
    let mut response = TaskMutationExecutor::default().run(conn, &envelope, frozen, |domain_tx, frozen_ref| {
        apply_acquire(domain_tx, frozen_ref, input, &mut issued_token)
    })?;
    if let Some(token) = issued_token {
        response["token"] = serde_json::Value::String(token);
    }
    Ok(response)
}

/// `lease.acquire` 领域回调。
fn apply_acquire(
    tx: &mut TaskDomainTx<'_>,
    _frozen: &FrozenAuthorityInput,
    input: &AcquireInput,
    issued_token: &mut Option<String>,
) -> DomainOutcome {
    match write_acquire(tx.tx(), input, issued_token) {
        Ok(ok) => DomainOutcome::CommitSuccess { response: ok.response },
        Err(err) => err.into_outcome(),
    }
}

/// 在 savepoint 事务内执行原子比较 + 回收 + 插入 + 审计（Req 11.2-11.3，§8.1.6）。
fn write_acquire(
    tx: &Connection,
    input: &AcquireInput,
    issued_token: &mut Option<String>,
) -> Result<DomainWriteOk, LifecycleDomainError> {
    let workspace_id = workspace_id_of(tx, &input.task_id)?;
    let now = now_unix();
    let ttl = if input.ttl_seconds > 0.0 { input.ttl_seconds } else { DEFAULT_LEASE_TTL };
    let expires_at = now + ttl;

    // 1. 原子比较当前 active lease（Req 11.2）。
    let active: Option<LeaseRow> = tx
        .query_row(
            "SELECT id, lease_id, token_hash, fencing_counter, expires_at, \
                    agent_id, session_id, model_id \
             FROM task_leases \
             WHERE workspace_id = ?1 AND task_id = ?2 AND role = ?3 AND status = 'active' \
             ORDER BY id ASC LIMIT 1",
            rusqlite::params![workspace_id, input.task_id, input.role],
            |r| {
                Ok(LeaseRow {
                    id: r.get(0)?,
                    lease_id: r.get(1)?,
                    token_hash: r.get(2)?,
                    fencing_counter: r.get(3)?,
                    expires_at: r.get(4)?,
                    agent_id: r.get(5)?,
                    session_id: r.get(6)?,
                    model_id: r.get(7)?,
                })
            },
        )
        .optional()
        .map_err(|e| LifecycleDomainError::infra_msg(e, "查询 active lease 失败"))?;
    if let Some(active) = active {
        if now <= active.expires_at {
            // 未过期并不等于仍有可用 owner：Executor 进程异常退出时旧 lease 可能还有
            // 很长 TTL。仅当 holder 注册状态/心跳明确 stale 时才在同一事务回收。
            let holder: Option<(String, f64)> = tx
                .query_row(
                    "SELECT status, last_heartbeat FROM agent_registrations \
                     WHERE agent_id = ?1 AND session_id = ?2 LIMIT 1",
                    rusqlite::params![active.agent_id, active.session_id],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .optional()
                .map_err(|e| LifecycleDomainError::infra_msg(e, "查询 lease holder 注册失败"))?;
            let stale_reason = match holder {
                None => Some("holder_registration_missing"),
                Some((status, _)) if status != "active" => Some("holder_registration_inactive"),
                Some((_, last_heartbeat)) if now - last_heartbeat > ORPHAN_CLAIM_STALE_SECS => {
                    Some("holder_heartbeat_stale")
                }
                _ => None,
            };
            if let Some(reason) = stale_reason {
                tx.execute(
                    "UPDATE task_leases SET status = 'expired', released_at = ?1 WHERE id = ?2",
                    rusqlite::params![now, active.id],
                )
                .map_err(|e| LifecycleDomainError::infra_msg(e, "回收 stale lease 失败"))?;
                let actor = LeaseIdentity {
                    agent_id: active.agent_id.clone(),
                    session_id: active.session_id.clone(),
                    model_id: active.model_id.clone(),
                    role: input.role.clone(),
                };
                append_lease_event(
                    tx, workspace_id, &active.lease_id, &input.task_id, &input.role, "expire",
                    active.fencing_counter, now, &actor,
                    &format!(
                        "stale holder recovered: reason={reason}, holder_agent_id={}, holder_session_id={}",
                        active.agent_id, active.session_id
                    ),
                )?;
            } else {
                return Err(LifecycleDomainError::det(
                    ERR_LEASE_ACTIVE_EXISTS,
                    format!(
                        "task={} role={} 已有未过期 lease ({}, expires_at={:.1})",
                        input.task_id, input.role, active.lease_id, active.expires_at
                    ),
                ));
            }
        }
        if now > active.expires_at {
            // 已过期 → 旧 lease 置 expired（释放唯一 active 槽位，对齐既有 acquire）。
            tx.execute(
                "UPDATE task_leases SET status = 'expired', released_at = ?1 WHERE id = ?2",
                rusqlite::params![now, active.id],
            )
            .map_err(|e| LifecycleDomainError::infra_msg(e, "过期 lease 置 expired 失败"))?;
        }
    }

    // 2. 单调递增 fencing counter（Req 11.3）：该 task+role 全历史 MAX + 1。
    let fencing_counter: i64 = tx
        .query_row(
            "SELECT COALESCE(MAX(fencing_counter), 0) FROM task_leases \
             WHERE workspace_id = ?1 AND task_id = ?2 AND role = ?3",
            rusqlite::params![workspace_id, input.task_id, input.role],
            |r| r.get::<_, i64>(0),
        )
        .map_err(|e| LifecycleDomainError::infra_msg(e, "查询 fencing counter 失败"))?
        + 1;

    // 3. 生成 raw token 与 lease_id（raw token 只在本次响应返回，DB 只存 hash）。
    let token = gen_lease_token();
    let token_hash = sha256_hex(token.as_bytes());
    let lease_id = gen_lease_id();

    // 4. 插入新 lease（唯一索引 idx_task_leases_active_unique 防双活；冲突 → fail-closed）。
    tx.execute(
        "INSERT INTO task_leases \
         (workspace_id, lease_id, task_id, role, agent_id, session_id, model_id, \
          token_hash, fencing_counter, acquired_at, expires_at, status) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, 'active')",
        rusqlite::params![
            workspace_id, lease_id, input.task_id, input.role,
            input.identity.agent_id, input.identity.session_id, input.identity.model_id,
            token_hash, fencing_counter, now, expires_at,
        ],
    )
    .map_err(|e| {
        if is_unique_violation(&e) {
            LifecycleDomainError::det(
                ERR_LEASE_ALREADY_ACTIVE,
                format!(
                    "task={} role={} 已有 active lease（唯一索引防双活）",
                    input.task_id, input.role
                ),
            )
        } else {
            LifecycleDomainError::infra_msg(e, "插入 task_leases 失败")
        }
    })?;

    // 5. 追加审计事件（append-only，不写 raw token）；此时才视为成功签发。
    append_lease_event(
        tx, workspace_id, &lease_id, &input.task_id, &input.role, "acquire",
        fencing_counter, now, &input.identity,
        &format!("acquired, expires_at={expires_at:.1}"),
    )?;
    *issued_token = Some(token);

    Ok(DomainWriteOk {
        response: serde_json::json!({
            "lease_id": lease_id,
            "task_id": input.task_id,
            "role": input.role,
            "token_hash": token_hash,
            "fencing_counter": fencing_counter,
            "acquired_at": now,
            "expires_at": expires_at,
        }),
    })
}

// ============================================================================
// `lease.renew` / `lease.extend`
// ============================================================================

/// 以 `lease.renew`（含 alias `lease.extend`）领域入口构造 `StrictParsedEnvelope`，
/// 委托统一 wrapper。幂等续租（Req 11.5）：不递增 counter、不新建 lease。
pub fn lease_renew(
    conn: &mut Connection,
    frozen: &FrozenAuthorityInput,
    ledger_key: &LedgerKey,
    input: &RenewInput,
) -> Result<serde_json::Value, DaemonRpcError> {
    let envelope = StrictParsedEnvelope {
        workspace_instance_id: ledger_key.workspace_instance_id.clone(),
        canonical_method: ledger_key.method.clone(),
        request_id: ledger_key.request_id.clone(),
        params: serde_json::json!({
            "task_id": input.task_id,
            "role": input.role,
            "token": input.token,
            "ttl_seconds": input.ttl_seconds,
            "fencing_counter": input.fencing_counter,
            "identity": {
                "agent_id": input.identity.as_ref().map(|i| i.agent_id.as_str()).unwrap_or(""),
                "session_id": input.identity.as_ref().map(|i| i.session_id.as_str()).unwrap_or(""),
                "model_id": input.identity.as_ref().map(|i| i.model_id.as_str()).unwrap_or(""),
                "role": input.identity.as_ref().map(|i| i.role.as_str()).unwrap_or(""),
            },
        }),
        invocation_class: InvocationClass::ExternalTransport,
    };
    TaskMutationExecutor::default().run(conn, &envelope, frozen, |domain_tx, frozen_ref| {
        apply_renew(domain_tx, frozen_ref, input)
    })
}

/// `lease.renew` 领域回调。
fn apply_renew(
    tx: &mut TaskDomainTx<'_>,
    _frozen: &FrozenAuthorityInput,
    input: &RenewInput,
) -> DomainOutcome {
    match write_renew(tx.tx(), input) {
        Ok(ok) => DomainOutcome::CommitSuccess { response: ok.response },
        Err(err) => err.into_outcome(),
    }
}

/// 在 savepoint 事务内执行校验 + 续期 + 审计（Req 11.4-11.5，§8.1.6）。
fn write_renew(tx: &Connection, input: &RenewInput) -> Result<DomainWriteOk, LifecycleDomainError> {
    let workspace_id = workspace_id_of(tx, &input.task_id)?;
    let now = now_unix();
    let ttl = if input.ttl_seconds > 0.0 { input.ttl_seconds } else { DEFAULT_LEASE_TTL };

    let lease: LeaseRow = tx
        .query_row(
            "SELECT id, lease_id, token_hash, fencing_counter, expires_at, \
                    agent_id, session_id, model_id \
             FROM task_leases \
             WHERE workspace_id = ?1 AND task_id = ?2 AND role = ?3 AND status = 'active' \
             ORDER BY id ASC LIMIT 1",
            rusqlite::params![workspace_id, input.task_id, input.role],
            |r| {
                Ok(LeaseRow {
                    id: r.get(0)?,
                    lease_id: r.get(1)?,
                    token_hash: r.get(2)?,
                    fencing_counter: r.get(3)?,
                    expires_at: r.get(4)?,
                    agent_id: r.get(5)?,
                    session_id: r.get(6)?,
                    model_id: r.get(7)?,
                })
            },
        )
        .optional()
        .map_err(|e| LifecycleDomainError::infra_msg(e, "查询 active lease 失败"))?
        .ok_or_else(|| {
            LifecycleDomainError::det(
                ERR_LEASE_NOT_FOUND,
                format!("task={} role={} 无 active lease", input.task_id, input.role),
            )
        })?;

    // token hash 校验（Req 11.9）。
    if sha256_hex(input.token.as_bytes()) != lease.token_hash {
        return Err(LifecycleDomainError::det(
            ERR_LEASE_TOKEN_MISMATCH,
            format!("token hash 不匹配 (lease_id={})", lease.lease_id),
        ));
    }
    // holder Identity 校验（提供时，Req 11.4）。
    if let Some(id) = input.identity.as_ref() {
        if id.agent_id != lease.agent_id || id.session_id != lease.session_id || id.model_id != lease.model_id {
            return Err(LifecycleDomainError::det(
                ERR_LEASE_HOLDER_MISMATCH,
                format!("holder Identity 与 lease ({}) 不一致", lease.lease_id),
            ));
        }
    }
    // 过期判定（权威时钟，Req 11.4）。
    if now > lease.expires_at {
        return Err(LifecycleDomainError::det(
            ERR_LEASE_EXPIRED,
            format!("lease {} 已过期 (expires_at={:.1}, now={now:.1})", lease.lease_id, lease.expires_at),
        ));
    }
    // fencing counter 校验（提供时，Property 11：旧持有者续租被拒）。
    if let Some(c) = input.fencing_counter {
        if c != lease.fencing_counter {
            return Err(LifecycleDomainError::det(
                ERR_LEASE_FENCING_STALE,
                format!("fencing counter {c} != 当前 {}；旧持有者续租被拒绝", lease.fencing_counter),
            ));
        }
    }

    // 幂等续租：不递增 counter，不创建新 lease（Req 11.5）。
    let new_expires = now + ttl;
    tx.execute(
        "UPDATE task_leases SET renewed_at = ?1, expires_at = ?2 \
         WHERE workspace_id = ?3 AND task_id = ?4 AND role = ?5 AND status = 'active'",
        rusqlite::params![now, new_expires, workspace_id, input.task_id, input.role],
    )
    .map_err(|e| LifecycleDomainError::infra_msg(e, "renew 续期失败"))?;

    // 事件 actor 取 lease holder（对齐既有 renew 语义）。
    let actor = LeaseIdentity {
        agent_id: lease.agent_id.clone(),
        session_id: lease.session_id.clone(),
        model_id: lease.model_id.clone(),
        role: input.role.clone(),
    };
    append_lease_event(
        tx, workspace_id, &lease.lease_id, &input.task_id, &input.role, "renew",
        lease.fencing_counter, now, &actor,
        &format!("renewed, expires_at={new_expires:.1}"),
    )?;

    Ok(DomainWriteOk {
        response: serde_json::json!({
            "lease_id": lease.lease_id,
            "task_id": input.task_id,
            "role": input.role,
            "fencing_counter": lease.fencing_counter,
            "renewed_at": now,
            "expires_at": new_expires,
        }),
    })
}

// ============================================================================
// `lease.release`
// ============================================================================

/// 以 `lease.release` 领域入口构造 `StrictParsedEnvelope`，委托统一 wrapper。
/// 幂等（Req 11.7）：重复 release 返回同一 released 状态，不改变 counter。
pub fn lease_release(
    conn: &mut Connection,
    frozen: &FrozenAuthorityInput,
    ledger_key: &LedgerKey,
    input: &ReleaseInput,
) -> Result<serde_json::Value, DaemonRpcError> {
    let envelope = StrictParsedEnvelope {
        workspace_instance_id: ledger_key.workspace_instance_id.clone(),
        canonical_method: ledger_key.method.clone(),
        request_id: ledger_key.request_id.clone(),
        params: serde_json::json!({
            "task_id": input.task_id,
            "role": input.role,
            "token": input.token,
            "identity": {
                "agent_id": input.identity.as_ref().map(|i| i.agent_id.as_str()).unwrap_or(""),
                "session_id": input.identity.as_ref().map(|i| i.session_id.as_str()).unwrap_or(""),
                "model_id": input.identity.as_ref().map(|i| i.model_id.as_str()).unwrap_or(""),
                "role": input.identity.as_ref().map(|i| i.role.as_str()).unwrap_or(""),
            },
        }),
        invocation_class: InvocationClass::ExternalTransport,
    };
    TaskMutationExecutor::default().run(conn, &envelope, frozen, |domain_tx, frozen_ref| {
        apply_release(domain_tx, frozen_ref, input)
    })
}

/// `lease.release` 领域回调。
fn apply_release(
    tx: &mut TaskDomainTx<'_>,
    _frozen: &FrozenAuthorityInput,
    input: &ReleaseInput,
) -> DomainOutcome {
    match write_release(tx.tx(), input) {
        Ok(ok) => DomainOutcome::CommitSuccess { response: ok.response },
        Err(err) => err.into_outcome(),
    }
}

/// 在 savepoint 事务内执行 token 校验 + 置 released + 审计（Req 11.6-11.7，§8.1.6）。
fn write_release(tx: &Connection, input: &ReleaseInput) -> Result<DomainWriteOk, LifecycleDomainError> {
    let workspace_id = workspace_id_of(tx, &input.task_id)?;
    let now = now_unix();

    let active: Option<LeaseRow> = tx
        .query_row(
            "SELECT id, lease_id, token_hash, fencing_counter, expires_at, \
                    agent_id, session_id, model_id \
             FROM task_leases \
             WHERE workspace_id = ?1 AND task_id = ?2 AND role = ?3 AND status = 'active' \
             ORDER BY id ASC LIMIT 1",
            rusqlite::params![workspace_id, input.task_id, input.role],
            |r| {
                Ok(LeaseRow {
                    id: r.get(0)?,
                    lease_id: r.get(1)?,
                    token_hash: r.get(2)?,
                    fencing_counter: r.get(3)?,
                    expires_at: r.get(4)?,
                    agent_id: r.get(5)?,
                    session_id: r.get(6)?,
                    model_id: r.get(7)?,
                })
            },
        )
        .optional()
        .map_err(|e| LifecycleDomainError::infra_msg(e, "查询 active lease 失败"))?;
    if let Some(lease) = active {
        if sha256_hex(input.token.as_bytes()) != lease.token_hash {
            return Err(LifecycleDomainError::det(
                ERR_LEASE_TOKEN_MISMATCH,
                format!("token hash 不匹配 (lease_id={})", lease.lease_id),
            ));
        }
        if let Some(id) = input.identity.as_ref() {
            if id.agent_id != lease.agent_id || id.session_id != lease.session_id || id.model_id != lease.model_id {
                return Err(LifecycleDomainError::det(
                    ERR_LEASE_HOLDER_MISMATCH,
                    format!("holder Identity 与 lease ({}) 不一致", lease.lease_id),
                ));
            }
        }
        tx.execute(
            "UPDATE task_leases SET status = 'released', released_at = ?1 WHERE id = ?2",
            rusqlite::params![now, lease.id],
        )
        .map_err(|e| LifecycleDomainError::infra_msg(e, "release 置 released 失败"))?;

        let actor = LeaseIdentity {
            agent_id: lease.agent_id.clone(),
            session_id: lease.session_id.clone(),
            model_id: lease.model_id.clone(),
            role: input.role.clone(),
        };
        append_lease_event(
            tx, workspace_id, &lease.lease_id, &input.task_id, &input.role, "release",
            lease.fencing_counter, now, &actor,
            &format!("released at {now:.1}"),
        )?;

        return Ok(DomainWriteOk {
            response: serde_json::json!({
                "lease_id": lease.lease_id,
                "task_id": input.task_id,
                "role": input.role,
                "fencing_counter": lease.fencing_counter,
                "released_at": now,
                "status": "released",
            }),
        });
    }

    // 无 active lease → 幂等分支（Req 11.7）：最近历史 lease 已 released 且 token 匹配视为已释放。
    let hist: Option<(String, String, i64, String, f64)> = tx
        .query_row(
            "SELECT lease_id, token_hash, fencing_counter, status, COALESCE(released_at, 0) \
             FROM task_leases \
             WHERE workspace_id = ?1 AND task_id = ?2 AND role = ?3 \
             ORDER BY id DESC LIMIT 1",
            rusqlite::params![workspace_id, input.task_id, input.role],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
        )
        .optional()
        .map_err(|e| LifecycleDomainError::infra_msg(e, "查询历史 lease 失败"))?;
    if let Some((lease_id, hist_hash, hist_counter, hist_status, hist_released_at)) = hist {
        if hist_status == "released" && sha256_hex(input.token.as_bytes()) == hist_hash {
            return Ok(DomainWriteOk {
                response: serde_json::json!({
                    "lease_id": lease_id,
                    "task_id": input.task_id,
                    "role": input.role,
                    "fencing_counter": hist_counter,
                    "released_at": hist_released_at,
                    "status": "released",
                    "idempotent": true,
                }),
            });
        }
    }
    Err(LifecycleDomainError::det(
        ERR_LEASE_NOT_FOUND,
        format!("task={} role={} 无 active lease", input.task_id, input.role),
    ))
}

// ============================================================================
// 本地原语（与 task_collab 等价实现；task_collab 内部函数不对外复用）
// ============================================================================

/// SHA-256 hex（token hash 的唯一形态，Req 11.2）。
fn sha256_hex(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

/// 多路熵（纳秒时间戳 + 随机值 + PID）经双重 sha256 单向哈希生成 raw token。
fn gen_lease_token() -> String {
    let raw = format!("{}:{}:{}:{}", now_unix(), rand_val(), std::process::id(), rand_val());
    sha256_hex(format!("{}:{}", raw, sha256_hex(raw.as_bytes())).as_bytes())
}

/// 生成 Lease 唯一标识（对齐既有 `L-<hex16>` 格式）。
fn gen_lease_id() -> String {
    format!("L-{}", &sha256_hex(format!("{}:{}", now_unix(), rand_val()).as_bytes())[..16])
}

/// 生成 Lease 审计事件唯一标识（对齐既有 `EVT-<hex16>` 格式）。
fn gen_lease_event_id() -> String {
    format!("EVT-{}", &sha256_hex(format!("{}:{}", now_unix(), rand_val()).as_bytes())[..16])
}

/// 时间熵分量。
fn rand_val() -> u32 {
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    (ts & 0xffffffff) as u32
}

/// Unix 时间戳秒（float，兼容 SQLite REAL 时间列）。
fn now_unix() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}
