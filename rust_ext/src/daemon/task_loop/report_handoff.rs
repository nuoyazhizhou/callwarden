//! 任务 2：原生 `task.handoff` 领域模块（cw-role-handoff-task-loop.md §4.3/§4.4）。
//!
//! 只实现结构化 handoff 的 v1 语义 + `task.report` 交接保留字段零写入拒绝；不修改
//! `tasks.status`、不创建/释放 lease、不追加 fix_defect、不编辑 Role Contract payload/
//! operation store 或 Gate（所有权边界见 `mod.rs`）。
//!
//! 语义（§4.3 / §4.4 / §6）：
//! - `task.handoff` 必须带 §4 完整结构化 envelope；缺结构化字段 → `E_HANDOFF_FIELDS_REQUIRED`，
//!   且 v1 不沿用遗留 `open` 状态回退（§4.3 第 475-477 行）。
//! - 六种 handoff outcome 与 source/target/independence 组合固定（§6 第 608 行）：
//!   executor_ready_for_review→executor/reviewer/required、executor_blocked_to_user→executor/
//!   user/not_applicable、reviewer_pass→reviewer/adjudicator/required、reviewer_blocked→reviewer/
//!   executor/not_required、adjudicator_accepted→adjudicator/complete/not_applicable、
//!   adjudicator_returned→adjudicator/executor/not_required；其余组合拒绝。
//! - 单事务内依次重检（§4.3 第 342-346 行）：source actor identity 与
//!   `authorization.acting_role`、source lease/fencing（只读 task_leases）、当前 step binding、
//!   Task/Role Contract 三元组与 source Role Contract 的 `handoff_to`。
//! - Role/Task Contract 三元组在读取与写入之间发生 ABA 变化（revision/hash 与当前不一致）
//!   → `E_HANDOFF_CONTRACT_STALE`，零部分提交（§4.3 第 479-481 行）。
//! - 目标角色只记录为下一候选人，绝不因 handoff 获得 agent registration、lease 或 claim
//!   （§4.3 第 345-346 行）。
//! - 领域事件追加到 `task_events`（`reason_code='handoff_structured'`），ledger result 由
//!   wrapper 同一事务写入；已提交 key 重放原结果，不追加事件（§4.3 第 361-371 行）。
//! - `task.report` 含任一交接保留字段（首次 canonical request）→ 在任何 task-domain 查询或
//!   写入之前返回 `E_HANDOFF_REQUIRES_TASK_HANDOFF`（§4.4 第 485-492 行）。
//!
//! 事务/savepoint/ledger 语义由 foundation 独占的 `TaskMutationExecutor` wrapper 统一落实
//! （§3.3/§4.3）：本模块只提供 `apply_handoff` 领域回调（写 handoff event），以
//! `submit_handoff` 为入口构造 `StrictParsedEnvelope` 并委托 wrapper。cutover（1D3A/1D3B）
//! 前 route 仍 fail-closed，本入口供领域测试与任务 2 验收复用。

use rusqlite::{Connection, OptionalExtension};
use sha2::{Digest, Sha256};

use crate::daemon::dispatch::DaemonRpcError;
use super::claim::read_current_binding;
use super::executor::TaskMutationExecutor;
use super::types::{
    DomainOutcome, FrozenAuthorityInput, InfrastructureError, InvocationClass,
    StableDomainError, StrictParsedEnvelope, TaskDomainTx,
};

/// 确定性拒绝：缺少 §4 结构化 handoff envelope 字段（§4.3 第 477 行）。
pub const ERR_HANDOFF_FIELDS_REQUIRED: &str = "E_HANDOFF_FIELDS_REQUIRED";
/// 确定性拒绝：`task.report` 请求含任一交接保留字段（§4.4）。
pub const ERR_HANDOFF_REQUIRES_TASK_HANDOFF: &str = "E_HANDOFF_REQUIRES_TASK_HANDOFF";
/// 确定性拒绝：outcome/source/target/independence 组合不合法（§6 第 608 行）。
pub const ERR_HANDOFF_ROUTE_INVALID: &str = "E_HANDOFF_ROUTE_INVALID";
/// 确定性拒绝：source_role 与 identity.role 不匹配（§4.3 source 授权重检）。
pub const ERR_HANDOFF_ROLE_IDENTITY_MISMATCH: &str = "E_HANDOFF_ROLE_IDENTITY_MISMATCH";
/// 确定性拒绝：Task/Role Contract 三元组与当前不一致（ABA）或 lineage 三方不一致，
/// 或 Role Contract `handoff_to` 与 target_role 不匹配（§4.3 第 479-481 行）。
pub const ERR_HANDOFF_CONTRACT_STALE: &str = "E_HANDOFF_CONTRACT_STALE";
/// 确定性拒绝：task 没有不可变 task workspace binding（§3.4 第 292-296 行）。
pub const ERR_TASK_BINDING_REQUIRED: &str = "E_TASK_BINDING_REQUIRED";
/// 确定性拒绝：step 无 verified current binding（§4.3 当前 step binding 重检）。
pub const ERR_STEP_BINDING_INVALID: &str = "E_STEP_BINDING_INVALID";
/// lease 受保护写凭证缺失（§4.3 source lease/fencing 重检）。
pub const ERR_LEASE_REQUIRED: &str = "E_LEASE_REQUIRED";
/// lease 校验错误（与遗留 `validate_lease_for_mutation` 对齐，§6 第 643 行）。
pub const ERR_LEASE_NOT_FOUND: &str = "E_LEASE_NOT_FOUND";
pub const ERR_LEASE_TOKEN_MISMATCH: &str = "E_LEASE_TOKEN_MISMATCH";
pub const ERR_LEASE_EXPIRED: &str = "E_LEASE_EXPIRED";
pub const ERR_LEASE_FENCING_STALE: &str = "E_LEASE_FENCING_STALE";
pub const ERR_LEASE_HOLDER_MISMATCH: &str = "E_LEASE_HOLDER_MISMATCH";
/// 事务/savepoint/ledger 基础设施失败（InfrastructureError 语义，回滚 outer tx）。
pub const ERR_TASK_DB_TRANSACTION: &str = "E_TASK_DB_TRANSACTION";

/// `task.report` 交接保留字段（§4.4 第 487-489 行）；任意一个出现即 `E_HANDOFF_REQUIRES_TASK_HANDOFF`。
pub const REPORT_RESERVED_FIELDS: &[&str] = &[
    "handoff",
    "target_role",
    "target_agent",
    "source_role",
    "handoff_reason",
    "required_new_instance",
    "required_new_session",
    "handoff_contract",
];

/// `task.handoff` 的 ledger dedup key（固定 (workspace_instance_id, method, request_id)）。
pub struct LedgerKey {
    pub workspace_instance_id: String,
    pub method: String,
    pub request_id: String,
}

/// Task/Role Contract 的版本化三元组（§4.3 envelope 形态）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContractTriple {
    /// Task Contract = `contract_id`；Role Contract = `role_contract_revision_id`。
    pub id: String,
    pub revision: i64,
    pub hash: String,
}

/// `task.handoff` 的领域参数（经严格 envelope.params 校验后传入；不含 key 内字段）。
#[derive(Debug)]
pub struct HandoffInput {
    pub task_id: String,
    /// 交接 source step id（§4.3 当前 step binding 重检）。
    pub step_id: String,
    /// v1 角色枚举：executor / reviewer / adjudicator。
    pub source_role: String,
    /// v1 角色枚举 + complete / user。
    pub target_role: String,
    pub reason: String,
    /// 六种冻结 outcome（§6 第 608 行）。
    pub outcome: String,
    pub task_contract: ContractTriple,
    pub role_contract: ContractTriple,
    pub required_new_instance: bool,
    pub required_new_session: bool,
    /// `authorization.acting_role`（原始 identity.role，如 implementer / reviewer）。
    pub acting_role: String,
    /// source actor identity（§4.3 source 授权重检）。
    pub agent_id: String,
    pub session_id: String,
    pub model_id: String,
    /// source lease 凭证（只读重检，不创建/释放）。
    pub lease_token: String,
    pub fencing_counter: i64,
    /// 授权写入者（daemon 侧 peer identity；进入 executor 前由调用方填充）。
    pub created_by: String,
}

impl HandoffInput {
    /// 从 `StrictParsedEnvelope.params` 严格解析领域输入。
    ///
    /// `task_id` 缺失 → `invalid_params`（连 key 都无法定位）。结构化 envelope 任一
    /// 必需字段（source_role/target_role/outcome/task_contract/role_contract）缺失 →
    /// `E_HANDOFF_FIELDS_REQUIRED`（§4.3 第 477 行），不进入 executor。
    pub fn from_params(params: &serde_json::Value) -> Result<Self, DaemonRpcError> {
        let get_str = |key: &str| -> Result<String, DaemonRpcError> {
            params
                .get(key)
                .and_then(|v| v.as_str())
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .ok_or_else(|| {
                    DaemonRpcError::new(
                        ERR_HANDOFF_FIELDS_REQUIRED,
                        format!("task.handoff 缺少结构化字段: {key}"),
                    )
                })
        };
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("task.handoff 缺少字段: task_id"))?;
        let parse_triple = |key: &str| -> Result<ContractTriple, DaemonRpcError> {
            let obj = params
                .get(key)
                .and_then(|v| v.as_object())
                .ok_or_else(|| {
                    DaemonRpcError::new(
                        ERR_HANDOFF_FIELDS_REQUIRED,
                        format!("task.handoff 缺少结构化字段: {key}"),
                    )
                })?;
            let id = obj
                .get("id")
                .and_then(|v| v.as_str())
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .ok_or_else(|| {
                    DaemonRpcError::new(
                        ERR_HANDOFF_FIELDS_REQUIRED,
                        format!("task.handoff {key}.id 缺失"),
                    )
                })?;
            let revision = obj.get("revision").and_then(|v| v.as_i64()).ok_or_else(|| {
                DaemonRpcError::new(
                    ERR_HANDOFF_FIELDS_REQUIRED,
                    format!("task.handoff {key}.revision 缺失或非整数"),
                )
            })?;
            let hash = obj
                .get("hash")
                .and_then(|v| v.as_str())
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .ok_or_else(|| {
                    DaemonRpcError::new(
                        ERR_HANDOFF_FIELDS_REQUIRED,
                        format!("task.handoff {key}.hash 缺失"),
                    )
                })?;
            Ok(ContractTriple { id, revision, hash })
        };
        let get_bool = |key: &str| -> bool {
            params
                .get(key)
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
        };
        let get_opt_str = |key: &str| -> String {
            params
                .get(key)
                .and_then(|v| v.as_str())
                .map(|s| s.trim().to_string())
                .unwrap_or_default()
        };
        let lease_token = get_opt_str("lease_token");
        let fencing_counter = params
            .get("fencing_counter")
            .and_then(|v| v.as_i64())
            .ok_or_else(|| {
                DaemonRpcError::new(
                    ERR_LEASE_REQUIRED,
                    "task.handoff 必须携带完整 source lease 凭证（lease_token + fencing_counter）",
                )
            })?;
        if lease_token.is_empty() {
            return Err(DaemonRpcError::new(
                ERR_LEASE_REQUIRED,
                "task.handoff 必须携带完整 source lease 凭证（lease_token + fencing_counter）",
            ));
        }
        Ok(HandoffInput {
            task_id,
            step_id: get_str("step_id")?,
            source_role: get_str("source_role")?,
            target_role: get_str("target_role")?,
            reason: get_str("reason")?,
            outcome: get_str("outcome")?,
            task_contract: parse_triple("task_contract")?,
            role_contract: parse_triple("role_contract")?,
            required_new_instance: get_bool("required_new_instance"),
            required_new_session: get_bool("required_new_session"),
            acting_role: get_str("acting_role")?,
            agent_id: get_opt_str("agent_id"),
            session_id: get_opt_str("session_id"),
            model_id: get_opt_str("model_id"),
            lease_token,
            fencing_counter,
            created_by: get_opt_str("created_by"),
        })
    }
}

/// §6 第 608 行冻结的六种 outcome 路由：outcome → (source_role, target_role, independence)。
fn outcome_route(outcome: &str) -> Option<(&'static str, &'static str, &'static str)> {
    match outcome {
        "executor_ready_for_review" => Some(("executor", "reviewer", "required")),
        "executor_blocked_to_user" => Some(("executor", "user", "not_applicable")),
        "reviewer_pass" => Some(("reviewer", "adjudicator", "required")),
        "reviewer_blocked" => Some(("reviewer", "executor", "not_required")),
        "adjudicator_accepted" => Some(("adjudicator", "complete", "not_applicable")),
        "adjudicator_returned" => Some(("adjudicator", "executor", "not_required")),
        _ => None,
    }
}

/// identity.role（acting_role）到 v1 角色枚举的运行时映射（与遗留 claim/handoff 对齐）。
fn runtime_role(acting_role: &str) -> &'static str {
    match acting_role {
        "planner" | "implementer" | "tester" | "evidence" | "executor" => "executor",
        "reviewer" | "independent_reviewer" => "reviewer",
        "adjudicator" => "adjudicator",
        _ => "",
    }
}

/// 领域写入成功后的响应（wrapper 统一落 ledger result）。
struct DomainWriteOk {
    response: serde_json::Value,
}

/// 领域执行期间的失败类别（封闭、类型化）。
enum HandoffDomainError {
    /// 确定性、可重放失败：外层在 savepoint 回滚后写可重放 ledger error 并 commit。
    Deterministic { code: String, message: String },
    /// 基础设施失败：回滚 outer transaction、领域写入与 ledger result。
    Infrastructure(DaemonRpcError),
}

impl HandoffDomainError {
    fn fields_required(message: String) -> Self {
        HandoffDomainError::Deterministic {
            code: ERR_HANDOFF_FIELDS_REQUIRED.to_string(),
            message,
        }
    }
    fn route_invalid(message: String) -> Self {
        HandoffDomainError::Deterministic {
            code: ERR_HANDOFF_ROUTE_INVALID.to_string(),
            message,
        }
    }
    fn role_identity_mismatch(message: String) -> Self {
        HandoffDomainError::Deterministic {
            code: ERR_HANDOFF_ROLE_IDENTITY_MISMATCH.to_string(),
            message,
        }
    }
    fn contract_stale(message: String) -> Self {
        HandoffDomainError::Deterministic {
            code: ERR_HANDOFF_CONTRACT_STALE.to_string(),
            message,
        }
    }
    fn binding_required(message: String) -> Self {
        HandoffDomainError::Deterministic {
            code: ERR_TASK_BINDING_REQUIRED.to_string(),
            message,
        }
    }
    fn binding_invalid(message: String) -> Self {
        HandoffDomainError::Deterministic {
            code: ERR_STEP_BINDING_INVALID.to_string(),
            message,
        }
    }
    fn lease(code: &'static str, message: String) -> Self {
        HandoffDomainError::Deterministic {
            code: code.to_string(),
            message,
        }
    }
    fn infra(e: DaemonRpcError) -> Self {
        HandoffDomainError::Infrastructure(e)
    }
    fn infra_msg(e: rusqlite::Error, context: &str) -> Self {
        HandoffDomainError::Infrastructure(infra_error(&format!("{context}: {e}")))
    }
    /// 把本地失败类别映射为 wrapper 要求的封闭 `DomainOutcome`（§3.3）。
    fn into_outcome(self) -> DomainOutcome {
        match self {
            HandoffDomainError::Deterministic { code, message: _ } => {
                DomainOutcome::CommitDeterministicError {
                    stable_error: StableDomainError::DeterministicReject { code },
                }
            }
            HandoffDomainError::Infrastructure(error) => {
                DomainOutcome::RollbackInfrastructureError {
                    infrastructure_error: InfrastructureError::Internal {
                        detail: error.message,
                    },
                }
            }
        }
    }
}

/// 以 `task.handoff` 领域入口构造 `StrictParsedEnvelope`，委托统一的
/// `TaskMutationExecutor` wrapper 执行 dedupe → savepoint → 分派 → ledger（§3.3/§4.3）。
///
/// 领域语义（§4.3）由 `apply_handoff` 承担：source 授权重检（identity/acting_role、
/// lease/fencing、当前 step binding、Task/Role Contract 三元组与 `handoff_to`）任一违反 →
/// 确定性错误（wrapper 回滚回调局部写入后写可重放 ledger error 并 commit）；任一 infra
/// 失败 → wrapper 回滚整个 outer transaction。v1 不修改 `tasks.status`。
pub fn submit_handoff(
    conn: &mut Connection,
    frozen: &FrozenAuthorityInput,
    ledger_key: &LedgerKey,
    input: &HandoffInput,
) -> Result<serde_json::Value, DaemonRpcError> {
    let envelope = StrictParsedEnvelope {
        workspace_instance_id: ledger_key.workspace_instance_id.clone(),
        canonical_method: ledger_key.method.clone(),
        request_id: ledger_key.request_id.clone(),
        params: serde_json::json!({
            "task_id": input.task_id,
            "step_id": input.step_id,
            "source_role": input.source_role,
            "target_role": input.target_role,
            "reason": input.reason,
            "outcome": input.outcome,
            "task_contract": {
                "id": input.task_contract.id,
                "revision": input.task_contract.revision,
                "hash": input.task_contract.hash,
            },
            "role_contract": {
                "id": input.role_contract.id,
                "revision": input.role_contract.revision,
                "hash": input.role_contract.hash,
            },
            "required_new_instance": input.required_new_instance,
            "required_new_session": input.required_new_session,
            "acting_role": input.acting_role,
            "agent_id": input.agent_id,
            "session_id": input.session_id,
            "model_id": input.model_id,
            "lease_token": input.lease_token,
            "fencing_counter": input.fencing_counter,
            "created_by": input.created_by,
        }),
        invocation_class: InvocationClass::ExternalTransport,
    };
    let request_id = ledger_key.request_id.clone();
    TaskMutationExecutor::default().run(conn, &envelope, frozen, |domain_tx, frozen_ref| {
        apply_handoff(domain_tx, frozen_ref, input, &request_id)
    })
}

/// `task.handoff` 领域回调：在受保护事务内完成 source 授权重检 + handoff event 追加，
/// 返回封闭 `DomainOutcome` 交由 wrapper 分派（§3.3）。
fn apply_handoff(
    tx: &mut TaskDomainTx<'_>,
    _frozen: &FrozenAuthorityInput,
    input: &HandoffInput,
    request_id: &str,
) -> DomainOutcome {
    match write_domain(tx.tx(), input, request_id) {
        Ok(ok) => DomainOutcome::CommitSuccess { response: ok.response },
        Err(err) => err.into_outcome(),
    }
}

/// 在 savepoint 事务内执行 source 授权重检 + handoff event 追加（§4.3/§4.4）。
fn write_domain(
    tx: &Connection,
    input: &HandoffInput,
    request_id: &str,
) -> Result<DomainWriteOk, HandoffDomainError> {
    // 1. task 必须已有不可变 workspace binding（§3.4：逻辑 workspace 只来自 binding）。
    let workspace_id: Option<i64> = tx
        .query_row(
            "SELECT workspace_id FROM task_workspace_bindings WHERE task_id = ?1",
            [&input.task_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| HandoffDomainError::infra_msg(e, "task workspace binding 查询失败"))?;
    let workspace_id = workspace_id.ok_or_else(|| {
        HandoffDomainError::binding_required(format!(
            "task {} 没有不可变 task workspace binding（v1 handoff 拒绝）",
            input.task_id
        ))
    })?;

    // 2. 六种 outcome 路由校验（§6 第 608 行：outcome → source/target/independence 固定）。
    let (expected_source, expected_target, independence) = outcome_route(&input.outcome)
        .ok_or_else(|| {
            HandoffDomainError::route_invalid(format!(
                "未知 handoff outcome: {}（仅接受六种冻结组合）",
                input.outcome
            ))
        })?;
    if input.source_role != expected_source || input.target_role != expected_target {
        return Err(HandoffDomainError::route_invalid(format!(
            "outcome={} 必须 source_role={expected_source} target_role={expected_target}，\
             收到 source_role={} target_role={}",
            input.outcome, input.source_role, input.target_role
        )));
    }

    // 3. source actor identity 与 `authorization.acting_role` 重检（§4.3 第 343-344 行）。
    if runtime_role(&input.acting_role) != input.source_role {
        return Err(HandoffDomainError::role_identity_mismatch(format!(
            "source_role={} 与 identity.role(acting_role={}) 不匹配",
            input.source_role, input.acting_role
        )));
    }

    // 4. source lease/fencing 重检（§4.3 第 344 行；只读 task_leases，不创建/释放）。
    check_lease(
        tx,
        &input.task_id,
        &input.acting_role,
        &input.lease_token,
        input.fencing_counter,
        &input.agent_id,
        &input.session_id,
        &input.model_id,
    )?;

    // 5. 当前 step binding（§4.3 第 344 行）：step 必须已有 verified current binding。
    let binding = read_current_binding(tx, workspace_id, &input.task_id, &input.step_id)
        .map_err(HandoffDomainError::infra)?
        .ok_or_else(|| {
            HandoffDomainError::binding_invalid(format!(
                "step {} 在 task {} 无 verified current binding（v1 handoff 拒绝）",
                input.step_id, input.task_id
            ))
        })?;

    // 6. Role Contract 三元组 + lineage 三方一致性 + 与 step current binding 一致
    //    （读取后 ABA → E_HANDOFF_CONTRACT_STALE，§4.3 第 479-481 行）。
    let (lineage_id, revision, hash, payload): (String, i64, String, String) = tx
        .query_row(
            "SELECT role_contract_lineage_id, revision, role_contract_hash, \
                    canonical_payload_json \
             FROM role_contract_revisions WHERE role_contract_revision_id = ?1",
            [&input.role_contract.id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .map_err(|e| {
            if matches!(e, rusqlite::Error::QueryReturnedNoRows) {
                HandoffDomainError::contract_stale(format!(
                    "role_contract_revision_id {} 不存在（悬空三元组）",
                    input.role_contract.id
                ))
            } else {
                HandoffDomainError::infra_msg(e, "role_contract_revision 读取失败")
            }
        })?;
    if revision != input.role_contract.revision || hash != input.role_contract.hash {
        return Err(HandoffDomainError::contract_stale(format!(
            "role_contract 三元组不一致：请求 revision={} hash={}，当前 revision={revision} hash={hash}",
            input.role_contract.revision, input.role_contract.hash
        )));
    }
    let (lineage_task_id, lineage_workspace_id): (String, i64) = tx
        .query_row(
            "SELECT task_id, workspace_id FROM role_contract_lineages \
             WHERE role_contract_lineage_id = ?1",
            [&lineage_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|e| {
            if matches!(e, rusqlite::Error::QueryReturnedNoRows) {
                HandoffDomainError::contract_stale(format!(
                    "role_contract_lineage_id {lineage_id} 悬空"
                ))
            } else {
                HandoffDomainError::infra_msg(e, "role_contract_lineage 读取失败")
            }
        })?;
    if lineage_task_id != input.task_id || lineage_workspace_id != workspace_id {
        return Err(HandoffDomainError::contract_stale(format!(
            "role_contract 三元组的 lineage 归属 (task={lineage_task_id}, workspace={lineage_workspace_id}) \
             与 task workspace binding (task={}, workspace={workspace_id}) 不一致",
            input.task_id
        )));
    }
    // 与 step 当前 binding 的 revision id/revision/hash 必须逐项一致（ABA fail-closed）。
    if binding.role_contract_revision_id != input.role_contract.id
        || binding.role_contract_revision != input.role_contract.revision
        || binding.role_contract_hash != input.role_contract.hash
    {
        return Err(HandoffDomainError::contract_stale(format!(
            "handoff role_contract 三元组与 step {} 的 current binding 不一致（ABA）",
            input.step_id
        )));
    }
    // source Role Contract 的 `handoff_to` 必须等于 target_role（§4.3 第 345 行）。
    let handoff_to = serde_json::from_str::<serde_json::Value>(&payload)
        .ok()
        .and_then(|v| {
            v.get("handoff_to")
                .and_then(|item| item.as_str())
                .map(|s| s.to_string())
        })
        .unwrap_or_default();
    if handoff_to != input.target_role {
        return Err(HandoffDomainError::contract_stale(format!(
            "source Role Contract handoff_to={handoff_to:?} 与 target_role={} 不一致",
            input.target_role
        )));
    }

    // 7. Task Contract 三元组（§4.3 第 344 行）：revision/hash 必须与当前行一致（ABA）。
    let task_contract_ok: i64 = tx
        .query_row(
            "SELECT COUNT(*) FROM task_contract_revisions \
             WHERE contract_id = ?1 AND revision = ?2 AND contract_hash = ?3 AND task_id = ?4",
            rusqlite::params![
                input.task_contract.id,
                input.task_contract.revision,
                input.task_contract.hash,
                input.task_id,
            ],
            |row| row.get(0),
        )
        .map_err(|e| HandoffDomainError::infra_msg(e, "task_contract_revisions 读取失败"))?;
    if task_contract_ok == 0 {
        return Err(HandoffDomainError::contract_stale(format!(
            "task_contract 三元组 (id={}, revision={}, hash={}) 不存在或 hash 不匹配（ABA）",
            input.task_contract.id, input.task_contract.revision, input.task_contract.hash
        )));
    }

    // 8. 追加不可变 handoff event（§4.3 第 369 行）。v1 不修改 tasks.status（§4.3 第 475 行）。
    let current_status: String = tx
        .query_row("SELECT status FROM tasks WHERE id = ?1", [&input.task_id], |row| row.get(0))
        .map_err(|e| {
            if matches!(e, rusqlite::Error::QueryReturnedNoRows) {
                HandoffDomainError::binding_required(format!(
                    "task {} 不存在（v1 handoff 拒绝）",
                    input.task_id
                ))
            } else {
                HandoffDomainError::infra_msg(e, "task 状态读取失败")
            }
        })?;
    let handoff_event_id = format!("he-{}-{}", input.task_id, request_id);
    let seq = next_seq(tx)?;
    let envelope = serde_json::json!({
        "handoff_event_id": handoff_event_id,
        "task_id": input.task_id,
        "step_id": input.step_id,
        "source_role": input.source_role,
        "target_role": input.target_role,
        "outcome": input.outcome,
        "reason": input.reason,
        "task_contract": {
            "id": input.task_contract.id,
            "revision": input.task_contract.revision,
            "hash": input.task_contract.hash,
        },
        "role_contract": {
            "id": input.role_contract.id,
            "revision": input.role_contract.revision,
            "hash": input.role_contract.hash,
            "lineage_id": lineage_id,
        },
        "independence_requirement": independence,
        "required_new_instance": input.required_new_instance,
        "required_new_session": input.required_new_session,
        "request_id": request_id,
        "fencing_counter": input.fencing_counter,
        "agent_id": input.agent_id,
        "session_id": input.session_id,
        "created_by": input.created_by,
    });
    tx.execute(
        "INSERT INTO task_events \
         (task_id, workspace_id, from_status, to_status, reason_code, reason, \
          actor_identity, agent_session_id, role, contract_hash, snapshot_id, \
          monotonic_seq, authoritative_timestamp, evidence_path, evidence_hash) \
         VALUES (?1, ?2, ?3, ?3, 'handoff_structured', ?4, ?5, ?6, ?7, ?8, '', ?9, ?10, '', '')",
        rusqlite::params![
            input.task_id,
            workspace_id.to_string(),
            current_status,
            envelope.to_string(),
            input.created_by,
            input.session_id,
            input.source_role,
            input.role_contract.hash,
            seq,
            literal_now(),
        ],
    )
    .map_err(|e| HandoffDomainError::infra_msg(e, "追加 task_events handoff_structured 失败"))?;
    let event_id = tx.last_insert_rowid();

    Ok(DomainWriteOk {
        response: serde_json::json!({
            "ok": true,
            "task_id": input.task_id,
            "handoff_event_id": handoff_event_id,
            "event_id": event_id,
            "outcome": input.outcome,
            "request_id": request_id,
            "source_role": input.source_role,
            "target_role": input.target_role,
            "step_id": input.step_id,
            "workspace_id": workspace_id,
            "status": current_status,
            "independence_requirement": independence,
            "task_contract": {
                "id": input.task_contract.id,
                "revision": input.task_contract.revision,
                "hash": input.task_contract.hash,
            },
            "role_contract": {
                "id": input.role_contract.id,
                "revision": input.role_contract.revision,
                "hash": input.role_contract.hash,
                "lineage_id": lineage_id,
            },
            "required_new_instance": input.required_new_instance,
            "required_new_session": input.required_new_session,
        }),
    })
}

/// source lease/fencing 重检（§4.3 第 344 行；只读，不创建/释放/续期）。
///
/// 与遗留 `validate_lease_for_mutation`（task_collab.rs）逐项对齐：active lease 存在、
/// token hash 匹配、未过期（Authoritative_Clock）、fencing counter 一致、holder Identity 一致。
fn check_lease(
    tx: &Connection,
    task_id: &str,
    role: &str,
    token: &str,
    fencing_counter: i64,
    agent_id: &str,
    session_id: &str,
    model_id: &str,
) -> Result<(), HandoffDomainError> {
    let now = now_unix();
    let row: Option<(String, String, i64, f64, String, String, String)> = tx
        .query_row(
            "SELECT lease_id, token_hash, fencing_counter, expires_at, agent_id, session_id, model_id \
             FROM task_leases \
             WHERE task_id = ?1 AND role = ?2 AND status = 'active' \
             ORDER BY id ASC LIMIT 1",
            rusqlite::params![task_id, role],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                    row.get(6)?,
                ))
            },
        )
        .optional()
        .map_err(|e| HandoffDomainError::infra_msg(e, "task_leases 读取失败"))?;
    let (lease_id, token_hash, active_counter, expires_at, lease_agent, lease_session, lease_model) =
        row.ok_or_else(|| {
            HandoffDomainError::lease(
                ERR_LEASE_NOT_FOUND,
                format!(
                    "task={task_id} role={role} 无 active lease，handoff 需要先 acquire_lease"
                ),
            )
        })?;
    if sha256_hex(token.as_bytes()) != token_hash {
        return Err(HandoffDomainError::lease(
            ERR_LEASE_TOKEN_MISMATCH,
            format!("token hash 不匹配 (lease_id={lease_id})"),
        ));
    }
    if now > expires_at {
        return Err(HandoffDomainError::lease(
            ERR_LEASE_EXPIRED,
            format!(
                "lease {lease_id} 已过期 (expires_at={expires_at:.1}, now={now:.1})"
            ),
        ));
    }
    if fencing_counter != active_counter {
        return Err(HandoffDomainError::lease(
            ERR_LEASE_FENCING_STALE,
            format!(
                "fencing counter {fencing_counter} != 当前 {active_counter}；旧持有者写入被拒绝"
            ),
        ));
    }
    if agent_id != lease_agent || session_id != lease_session || model_id != lease_model {
        return Err(HandoffDomainError::lease(
            ERR_LEASE_HOLDER_MISMATCH,
            format!("holder Identity 与 lease ({lease_id}) 不一致"),
        ));
    }
    Ok(())
}

/// 追加 handoff event 前的单调序列号（task_events.monotonic_seq 的 max+1）。
fn next_seq(tx: &Connection) -> Result<i64, HandoffDomainError> {
    tx.query_row(
        "SELECT COALESCE(MAX(monotonic_seq), 0) + 1 FROM task_events",
        [],
        |row| row.get(0),
    )
    .map_err(|e| HandoffDomainError::infra_msg(e, "monotonic_seq 读取失败"))
}

/// 权威 UTC 秒值（微秒精度）文本；真实接入由 Authoritative_Clock 产生。
fn literal_now() -> String {
    format!("{:.6}", now_unix())
}

/// Unix 时间戳秒（float，兼容 SQLite REAL created_at / task_leases.expires_at）。
fn now_unix() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 基础设施失败归类（E_TASK_DB_TRANSACTION）。
fn infra_error(message: &str) -> DaemonRpcError {
    DaemonRpcError::new(ERR_TASK_DB_TRANSACTION, message.to_string())
}

/// sha256 hex（token hash 比对，与 task_leases.token_hash 存 sha256(raw) 对齐）。
fn sha256_hex(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

/// `task.report` 交接保留字段零写入拒绝（§4.4 第 485-492 行）。
///
/// 对首次 canonical `task.report` request，若收到任意交接保留字段——`handoff`、
/// `target_role`、`target_agent`、`source_role`、`handoff_reason`、
/// `required_new_instance`、`required_new_session` 或 `handoff_contract`——必须在任何
/// task-domain 查询或写入之前返回 `E_HANDOFF_REQUIRES_TASK_HANDOFF`。这是领域零写入
/// 拒绝：不得更新 task/step、追加 task/action event、创建 lease 或改变 Gate；authority
/// ledger 可以保存该确定性错误以供相同 request-id 重放。
pub fn reject_report_reserved_fields(params: &serde_json::Value) -> Result<(), DaemonRpcError> {
    let value = params.get("params").unwrap_or(params);
    let object = value.as_object().ok_or_else(|| {
        DaemonRpcError::new(
            ERR_HANDOFF_REQUIRES_TASK_HANDOFF,
            "task.report params 必须是 JSON object",
        )
    })?;
    for field in REPORT_RESERVED_FIELDS {
        if object.contains_key(*field) {
            return Err(DaemonRpcError::new(
                ERR_HANDOFF_REQUIRES_TASK_HANDOFF,
                format!(
                    "task.report 含交接保留字段 `{field}`；结构化交接必须改用 task.handoff（§4.4）"
                ),
            ));
        }
    }
    Ok(())
}
