//! 任务 3：原生 `verdict.submit`、有效投影与 Evidence Gate 的 versioned normalization 消费
//! （cw-role-handoff-task-loop.md §4.1/§4.2/§3.4/§8.1.5）。
//!
//! 只实现 daemon verdict 追加、fail-closed 有效投影与 `verdict-normalization/v1` 规则的
//! 版本化消费；不创建/释放 lease、不写 `task_gate_decisions`、不编辑 Role Contract
//! payload/operation store（所有权边界见 `mod.rs`）。
//!
//! 语义：
//! - `verdict.submit` 在受保护 mutation 串行化点（`TaskMutationExecutor` wrapper）内
//!   单事务依次重检（§4.1）：task 必须已有不可变 workspace binding；reviewer
//!   identity/acting_role 匹配；reviewer lease/fencing 只读重检；当前 step binding；
//!   Task/Role Contract 双三元组（读取后 ABA → `E_VERDICT_CONTRACT_STALE`）；Task Contract
//!   必须绑定 `(normalization_version, normalization_rules_hash)` 且该 row 存在、hash 匹配、
//!   未被撤销（§4.2/§8.1.5）→ 缺绑定/撤销/hash 不匹配一律 `E_VERDICT_NORMALIZATION_UNAVAILABLE`，
//!   绝不写入 `pass`。
//! - v1 新写入冻结：`overall ∈ {pass, block}`、`phase ∈ {blind_first_pass, post_reveal_amendment}`
//!   （§4.2 表）。`post_reveal_amendment` 必须引用同一 task 已封存的 `blind_first_pass` verdict_id
//!   （`amendment_ref`，Req 4.5）。
//! - 领域事件追加到 `task_verdict_events`（append-only）；事件持久化所用的
//!   initialization version/hash（Task Contract 绑定）与 Role Contract 完整 provenance。
//!   ledger result 由 wrapper 同一事务写入；已提交 key 重放原结果，不追加事件。
//!
//! 事务/savepoint/ledger 语义由 foundation 独占的 `TaskMutationExecutor` wrapper 统一落实
//! （§3.3/§4.3）：本模块只提供 `apply_verdict` 领域回调（写 verdict event），以
//! `submit_verdict` 为入口构造 `StrictParsedEnvelope` 并委托 wrapper。cutover（1D3A/1D3B）
//! 前 route 仍 fail-closed，本入口供领域测试与任务 3 验收复用。

use rusqlite::{Connection, OptionalExtension};
use sha2::{Digest, Sha256};

use crate::daemon::dispatch::DaemonRpcError;
use super::claim::read_current_binding;
use super::executor::TaskMutationExecutor;
use super::types::{
    DomainOutcome, FrozenAuthorityInput, InfrastructureError, InvocationClass,
    StableDomainError, StrictParsedEnvelope, TaskDomainTx,
};

/// 确定性拒绝：缺少 §4 结构化 verdict envelope 字段（§4.1 / §4.2）。
pub const ERR_VERDICT_FIELDS_REQUIRED: &str = "E_VERDICT_FIELDS_REQUIRED";
/// 确定性拒绝：`overall` 不是 v1 冻结枚举 `pass`/`block`（§4.2 表）。
pub const ERR_VERDICT_OVERALL_INVALID: &str = "E_VERDICT_OVERALL_INVALID";
/// 确定性拒绝：`phase` 不是 v1 冻结枚举 `blind_first_pass`/`post_reveal_amendment`（§4.2 表）。
pub const ERR_VERDICT_PHASE_INVALID: &str = "E_VERDICT_PHASE_INVALID";
/// 确定性拒绝：author 非 reviewer（identity/acting_role 与 §4.1 不符）。
pub const ERR_VERDICT_ROLE_IDENTITY_MISMATCH: &str = "E_VERDICT_ROLE_IDENTITY_MISMATCH";
/// 确定性拒绝：Task/Role Contract 三元组与当前不一致（ABA）或 lineage 三方不一致。
pub const ERR_VERDICT_CONTRACT_STALE: &str = "E_VERDICT_CONTRACT_STALE";
/// 确定性拒绝：Task Contract 未绑定 normalization 规则，或绑定 row 缺失/hash 不匹配/已撤销
/// （§4.2/§8.1.5）→ 结果保持 `UNVERIFIED`，绝不写入 `pass`。
pub const ERR_VERDICT_NORMALIZATION_UNAVAILABLE: &str = "E_VERDICT_NORMALIZATION_UNAVAILABLE";
/// 确定性拒绝：`post_reveal_amendment` 的 `amendment_ref` 缺失、悬空或引用了非
/// `blind_first_pass` 的 verdict（Req 4.5）。
pub const ERR_VERDICT_AMENDMENT_REF_INVALID: &str = "E_VERDICT_AMENDMENT_REF_INVALID";
/// 确定性拒绝：task 没有不可变 task workspace binding（§3.4 第 292-296 行）。
pub const ERR_TASK_BINDING_REQUIRED: &str = "E_TASK_BINDING_REQUIRED";
/// 确定性拒绝：step 无 verified current binding（§3.4 / bindings 链不连续即 UNVERIFIED）。
pub const ERR_STEP_BINDING_INVALID: &str = "E_STEP_BINDING_INVALID";
/// lease 受保护写凭证缺失（§4.1 source lease/fencing 重检）。
pub const ERR_LEASE_REQUIRED: &str = "E_LEASE_REQUIRED";
/// lease 校验错误（与遗留 `validate_lease_for_mutation` 对齐，§6 第 643 行）。
pub const ERR_LEASE_NOT_FOUND: &str = "E_LEASE_NOT_FOUND";
pub const ERR_LEASE_TOKEN_MISMATCH: &str = "E_LEASE_TOKEN_MISMATCH";
pub const ERR_LEASE_EXPIRED: &str = "E_LEASE_EXPIRED";
pub const ERR_LEASE_FENCING_STALE: &str = "E_LEASE_FENCING_STALE";
pub const ERR_LEASE_HOLDER_MISMATCH: &str = "E_LEASE_HOLDER_MISMATCH";
/// 事务/savepoint/ledger 基础设施失败（InfrastructureError 语义，回滚 outer tx）。
pub const ERR_TASK_DB_TRANSACTION: &str = "E_TASK_DB_TRANSACTION";

/// v1 冻结的 `normalization_version`（Task Contract 绑定；seed 于 schema v57）。
pub const VERDICT_NORMALIZATION_V1: &str = "verdict-normalization/v1";

/// `verdict.submit` 的 ledger dedup key（固定 (workspace_instance_id, method, request_id)）。
pub struct LedgerKey {
    pub workspace_instance_id: String,
    pub method: String,
    pub request_id: String,
}

/// Task/Role Contract 的版本化三元组（§4.1 envelope 形态）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContractTriple {
    /// Task Contract = `contract_id`；Role Contract = `role_contract_revision_id`。
    pub id: String,
    pub revision: i64,
    pub hash: String,
}

/// `verdict.submit` 的领域参数（经严格 envelope.params 校验后传入；不含 key 内字段）。
#[derive(Debug)]
pub struct VerdictInput {
    pub task_id: String,
    /// 被审步骤 id（当前 step binding 重检，§4.1）。
    pub step_id: String,
    /// v1 冻结 overall：`pass` / `block`。
    pub overall: String,
    /// v1 冻结 phase：`blind_first_pass` / `post_reveal_amendment`。
    pub phase: String,
    /// `clause_results` structured JSON（`[{clause_id, decision, evidence_ids}]`）。
    pub clause_results: String,
    /// `findings` structured JSON（`[{severity, subject, fact}]`）。
    pub findings: String,
    pub task_contract: ContractTriple,
    pub role_contract: ContractTriple,
    /// `post_reveal_amendment` 引用的已封存 `blind_first_pass` verdict_id；其余 phase 为空。
    pub amendment_ref: String,
    pub snapshot_id: String,
    pub view_manifest_hash: String,
    pub attestation: String,
    /// `authorization.acting_role`（原始 identity.role，如 reviewer）。
    pub acting_role: String,
    /// author actor identity（§4.1 source 授权重检）。
    pub agent_id: String,
    pub session_id: String,
    pub model_id: String,
    /// reviewer lease 凭证（只读重检，不创建/释放）。
    pub lease_token: String,
    pub fencing_counter: i64,
    /// 授权写入者（daemon 侧 peer identity；进入 executor 前由调用方填充）。
    pub created_by: String,
}

impl VerdictInput {
    /// 从 `StrictParsedEnvelope.params` 严格解析领域输入。
    ///
    /// `task_id` 缺失 → `invalid_params`（连 key 都无法定位）。结构化 envelope 任一必需字段
    /// （overall/phase/task_contract/role_contract）缺失或非期望类型 → `E_VERDICT_FIELDS_REQUIRED`
    /// （§4.1），不进入 executor。v1 枚举允许值在领域回调中 fail-closed 校验
    /// （`E_VERDICT_OVERALL_INVALID` / `E_VERDICT_PHASE_INVALID`）。
    pub fn from_params(params: &serde_json::Value) -> Result<Self, DaemonRpcError> {
        let get_str = |key: &str| -> Result<String, DaemonRpcError> {
            params
                .get(key)
                .and_then(|v| v.as_str())
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .ok_or_else(|| {
                    DaemonRpcError::new(
                        ERR_VERDICT_FIELDS_REQUIRED,
                        format!("verdict.submit 缺少结构化字段: {key}"),
                    )
                })
        };
        let get_str_or_default = |key: &str, default: &str| -> String {
            params
                .get(key)
                .and_then(|v| v.as_str())
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| default.to_string())
        };
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("verdict.submit 缺少字段: task_id"))?;
        let parse_triple = |key: &str| -> Result<ContractTriple, DaemonRpcError> {
            let obj = params
                .get(key)
                .and_then(|v| v.as_object())
                .ok_or_else(|| {
                    DaemonRpcError::new(
                        ERR_VERDICT_FIELDS_REQUIRED,
                        format!("verdict.submit 缺少结构化字段: {key}"),
                    )
                })?;
            let id = obj
                .get("id")
                .and_then(|v| v.as_str())
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .ok_or_else(|| {
                    DaemonRpcError::new(
                        ERR_VERDICT_FIELDS_REQUIRED,
                        format!("verdict.submit {key}.id 缺失"),
                    )
                })?;
            let revision = obj.get("revision").and_then(|v| v.as_i64()).ok_or_else(|| {
                DaemonRpcError::new(
                    ERR_VERDICT_FIELDS_REQUIRED,
                    format!("verdict.submit {key}.revision 缺失或非整数"),
                )
            })?;
            let hash = obj
                .get("hash")
                .and_then(|v| v.as_str())
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .ok_or_else(|| {
                    DaemonRpcError::new(
                        ERR_VERDICT_FIELDS_REQUIRED,
                        format!("verdict.submit {key}.hash 缺失"),
                    )
                })?;
            Ok(ContractTriple { id, revision, hash })
        };
        let lease_token = get_str_or_default("lease_token", "");
        let fencing_counter = params
            .get("fencing_counter")
            .and_then(|v| v.as_i64())
            .ok_or_else(|| {
                DaemonRpcError::new(
                    ERR_LEASE_REQUIRED,
                    "verdict.submit 必须携带完整 reviewer lease 凭证（lease_token + fencing_counter）",
                )
            })?;
        if lease_token.is_empty() {
            return Err(DaemonRpcError::new(
                ERR_LEASE_REQUIRED,
                "verdict.submit 必须携带完整 reviewer lease 凭证（lease_token + fencing_counter）",
            ));
        }
        Ok(VerdictInput {
            task_id,
            step_id: get_str("step_id")?,
            overall: get_str("overall")?,
            phase: get_str("phase")?,
            clause_results: get_str_or_default("clause_results", "[]"),
            findings: get_str_or_default("findings", "[]"),
            task_contract: parse_triple("task_contract")?,
            role_contract: parse_triple("role_contract")?,
            amendment_ref: get_str_or_default("amendment_ref", ""),
            snapshot_id: get_str_or_default("snapshot_id", ""),
            view_manifest_hash: get_str_or_default("view_manifest_hash", ""),
            attestation: get_str_or_default("attestation", ""),
            acting_role: get_str("acting_role")?,
            agent_id: get_str_or_default("agent_id", ""),
            session_id: get_str_or_default("session_id", ""),
            model_id: get_str_or_default("model_id", ""),
            lease_token,
            fencing_counter,
            created_by: get_str_or_default("created_by", ""),
        })
    }
}

/// identity.role（acting_role）到 v1 角色枚举的运行时映射（verdict 必须由 reviewer 提交，
/// §4.1；与遗留 claim/handoff 的 runtime_role 对齐）。
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
enum VerdictDomainError {
    /// 确定性、可重放失败：外层在 savepoint 回滚后写可重放 ledger error 并 commit。
    Deterministic { code: String, message: String },
    /// 基础设施失败：回滚 outer transaction、领域写入与 ledger result。
    Infrastructure(DaemonRpcError),
}

impl VerdictDomainError {
    fn overall_invalid(message: String) -> Self {
        VerdictDomainError::Deterministic {
            code: ERR_VERDICT_OVERALL_INVALID.to_string(),
            message,
        }
    }
    fn phase_invalid(message: String) -> Self {
        VerdictDomainError::Deterministic {
            code: ERR_VERDICT_PHASE_INVALID.to_string(),
            message,
        }
    }
    fn role_identity_mismatch(message: String) -> Self {
        VerdictDomainError::Deterministic {
            code: ERR_VERDICT_ROLE_IDENTITY_MISMATCH.to_string(),
            message,
        }
    }
    fn contract_stale(message: String) -> Self {
        VerdictDomainError::Deterministic {
            code: ERR_VERDICT_CONTRACT_STALE.to_string(),
            message,
        }
    }
    fn normalization_unavailable(message: String) -> Self {
        VerdictDomainError::Deterministic {
            code: ERR_VERDICT_NORMALIZATION_UNAVAILABLE.to_string(),
            message,
        }
    }
    fn amendment_ref_invalid(message: String) -> Self {
        VerdictDomainError::Deterministic {
            code: ERR_VERDICT_AMENDMENT_REF_INVALID.to_string(),
            message,
        }
    }
    fn binding_required(message: String) -> Self {
        VerdictDomainError::Deterministic {
            code: ERR_TASK_BINDING_REQUIRED.to_string(),
            message,
        }
    }
    fn binding_invalid(message: String) -> Self {
        VerdictDomainError::Deterministic {
            code: ERR_STEP_BINDING_INVALID.to_string(),
            message,
        }
    }
    fn lease(code: &'static str, message: String) -> Self {
        VerdictDomainError::Deterministic {
            code: code.to_string(),
            message,
        }
    }
    fn infra(e: DaemonRpcError) -> Self {
        VerdictDomainError::Infrastructure(e)
    }
    fn infra_msg(e: rusqlite::Error, context: &str) -> Self {
        VerdictDomainError::Infrastructure(infra_error(&format!("{context}: {e}")))
    }
    /// 把本地失败类别映射为 wrapper 要求的封闭 `DomainOutcome`（§3.3）。
    fn into_outcome(self) -> DomainOutcome {
        match self {
            VerdictDomainError::Deterministic { code, message: _ } => {
                DomainOutcome::CommitDeterministicError {
                    stable_error: StableDomainError::DeterministicReject { code },
                }
            }
            VerdictDomainError::Infrastructure(error) => {
                DomainOutcome::RollbackInfrastructureError {
                    infrastructure_error: InfrastructureError::Internal {
                        detail: error.message,
                    },
                }
            }
        }
    }
}

/// 以 `verdict.submit` 领域入口构造 `StrictParsedEnvelope`，委托统一的
/// `TaskMutationExecutor` wrapper 执行 dedupe → savepoint → 分派 → ledger（§3.3/§4.1）。
///
/// 领域语义（§4.1/§4.2）由 `apply_verdict` 承担：reviewer 授权重检（identity/acting_role、
/// lease/fencing、当前 step binding、Task/Role Contract 双三元组与 normalization 绑定）
/// 任一违反 → 确定性错误（wrapper 回滚回调局部写入后写可重放 ledger error 并 commit）；
/// 任一 infra 失败 → wrapper 回滚整个 outer transaction。
pub fn submit_verdict(
    conn: &mut Connection,
    frozen: &FrozenAuthorityInput,
    ledger_key: &LedgerKey,
    input: &VerdictInput,
) -> Result<serde_json::Value, DaemonRpcError> {
    let envelope = StrictParsedEnvelope {
        workspace_instance_id: ledger_key.workspace_instance_id.clone(),
        canonical_method: ledger_key.method.clone(),
        request_id: ledger_key.request_id.clone(),
        params: serde_json::json!({
            "task_id": input.task_id,
            "step_id": input.step_id,
            "overall": input.overall,
            "phase": input.phase,
            "clause_results": input.clause_results,
            "findings": input.findings,
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
            "amendment_ref": input.amendment_ref,
            "snapshot_id": input.snapshot_id,
            "view_manifest_hash": input.view_manifest_hash,
            "attestation": input.attestation,
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
        apply_verdict(domain_tx, frozen_ref, input, &request_id)
    })
}

/// `verdict.submit` 领域回调：在受保护事务内完成 reviewer 授权重检 + verdict event 追加，
/// 返回封闭 `DomainOutcome` 交由 wrapper 分派（§3.3）。
fn apply_verdict(
    tx: &mut TaskDomainTx<'_>,
    _frozen: &FrozenAuthorityInput,
    input: &VerdictInput,
    request_id: &str,
) -> DomainOutcome {
    match write_domain(tx.tx(), input, request_id) {
        Ok(ok) => DomainOutcome::CommitSuccess { response: ok.response },
        Err(err) => err.into_outcome(),
    }
}

/// 在 savepoint 事务内执行 reviewer 授权重检 + verdict event 追加（§4.1/§4.2）。
fn write_domain(
    tx: &Connection,
    input: &VerdictInput,
    request_id: &str,
) -> Result<DomainWriteOk, VerdictDomainError> {
    // 0. v1 冻结枚举校验（§4.2 表），先于一切 storage 重检 fail-closed。
    if input.overall != "pass" && input.overall != "block" {
        return Err(VerdictDomainError::overall_invalid(format!(
            "overall={} 不是 v1 新写入冻结枚举 pass/block（§4.2 表）",
            input.overall
        )));
    }
    if input.phase != "blind_first_pass" && input.phase != "post_reveal_amendment" {
        return Err(VerdictDomainError::phase_invalid(format!(
            "phase={} 不是 v1 新写入冻结枚举 blind_first_pass/post_reveal_amendment（§4.2 表）",
            input.phase
        )));
    }

    // 1. task 必须已有不可变 workspace binding（§3.4：逻辑 workspace 只来自 binding）。
    let workspace_id: Option<i64> = tx
        .query_row(
            "SELECT workspace_id FROM task_workspace_bindings WHERE task_id = ?1",
            [&input.task_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| VerdictDomainError::infra_msg(e, "task workspace binding 查询失败"))?;
    let workspace_id = workspace_id.ok_or_else(|| {
        VerdictDomainError::binding_required(format!(
            "task {} 没有不可变 task workspace binding（v1 verdict.submit 拒绝）",
            input.task_id
        ))
    })?;

    // 2. author 必须是 reviewer（§4.1 source 授权重检）。
    if runtime_role(&input.acting_role) != "reviewer" {
        return Err(VerdictDomainError::role_identity_mismatch(format!(
            "verdict 必须由 reviewer 提交，但 identity.role(acting_role={}) 映射为 {}",
            input.acting_role,
            runtime_role(&input.acting_role)
        )));
    }

    // 3. reviewer lease/fencing 重检（§4.1；只读 task_leases，不创建/释放）。
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

    // 4. 当前 step binding（§4.1）：step 必须已有 verified current binding。
    let binding = read_current_binding(tx, workspace_id, &input.task_id, &input.step_id)
        .map_err(VerdictDomainError::infra)?
        .ok_or_else(|| {
            VerdictDomainError::binding_invalid(format!(
                "step {} 在 task {} 无 verified current binding（v1 verdict.submit 拒绝）",
                input.step_id, input.task_id
            ))
        })?;

    // 5. Role Contract 三元组 + lineage 三方一致性 + 与 step current binding 一致
    //    （读取后 ABA → E_VERDICT_CONTRACT_STALE，§4.1）。
    let (lineage_id, revision, hash): (String, i64, String) = tx
        .query_row(
            "SELECT role_contract_lineage_id, revision, role_contract_hash \
             FROM role_contract_revisions WHERE role_contract_revision_id = ?1",
            [&input.role_contract.id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .map_err(|e| {
            if matches!(e, rusqlite::Error::QueryReturnedNoRows) {
                VerdictDomainError::contract_stale(format!(
                    "role_contract_revision_id {} 不存在（悬空三元组）",
                    input.role_contract.id
                ))
            } else {
                VerdictDomainError::infra_msg(e, "role_contract_revision 读取失败")
            }
        })?;
    if revision != input.role_contract.revision || hash != input.role_contract.hash {
        return Err(VerdictDomainError::contract_stale(format!(
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
                VerdictDomainError::contract_stale(format!(
                    "role_contract_lineage_id {lineage_id} 悬空"
                ))
            } else {
                VerdictDomainError::infra_msg(e, "role_contract_lineage 读取失败")
            }
        })?;
    if lineage_task_id != input.task_id || lineage_workspace_id != workspace_id {
        return Err(VerdictDomainError::contract_stale(format!(
            "role_contract 三元组的 lineage 归属 (task={lineage_task_id}, workspace={lineage_workspace_id}) \
             与 task workspace binding (task={}, workspace={workspace_id}) 不一致",
            input.task_id
        )));
    }
    // 与 step 当前 binding 的 revision id/revision/hash 逐项一致（ABA fail-closed）。
    if binding.role_contract_revision_id != input.role_contract.id
        || binding.role_contract_revision != input.role_contract.revision
        || binding.role_contract_hash != input.role_contract.hash
    {
        return Err(VerdictDomainError::contract_stale(format!(
            "verdict role_contract 三元组与 step {} 的 current binding 不一致（ABA）",
            input.step_id
        )));
    }

    // 6. Task Contract 三元组 + normalization 绑定（§4.2/§8.1.5）：revision/hash 与当前行一致
    //    （ABA），并从该行读取 normalization binding；缺绑定/hash 不匹配/已撤销 →
    //    E_VERDICT_NORMALIZATION_UNAVAILABLE（绝不写入 pass）。
    let task_contract_row: Option<(String, String)> = tx
        .query_row(
            "SELECT normalization_version, normalization_rules_hash \
             FROM task_contract_revisions \
             WHERE contract_id = ?1 AND revision = ?2 AND contract_hash = ?3 AND task_id = ?4",
            rusqlite::params![
                input.task_contract.id,
                input.task_contract.revision,
                input.task_contract.hash,
                input.task_id,
            ],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(|e| VerdictDomainError::infra_msg(e, "task_contract_revisions 读取失败"))?;
    let (norm_version, norm_rules_hash) =
        task_contract_row.ok_or_else(|| {
            VerdictDomainError::contract_stale(format!(
                "task_contract 三元组 (id={}, revision={}, hash={}) 不存在或 hash 不匹配（ABA）",
                input.task_contract.id, input.task_contract.revision, input.task_contract.hash
            ))
        })?;
    let (norm_version, norm_rules_hash) =
        validate_normalization_binding(tx, &norm_version, &norm_rules_hash)?;

    // 7. post_reveal_amendment 必须引用同一 task 已封存的 blind_first_pass verdict（Req 4.5）。
    if input.phase == "post_reveal_amendment" {
        if input.amendment_ref.is_empty() {
            return Err(VerdictDomainError::amendment_ref_invalid(
                "phase=post_reveal_amendment 必须提供 amendment_ref（引用的 sealed verdict_id）"
                    .to_string(),
            ));
        }
        let sealed_ok: i64 = tx
            .query_row(
                "SELECT COUNT(*) FROM task_verdict_events \
                 WHERE verdict_id = ?1 AND task_id = ?2 AND phase = 'blind_first_pass'",
                rusqlite::params![&input.amendment_ref, &input.task_id],
                |row| row.get(0),
            )
            .map_err(|e| VerdictDomainError::infra_msg(e, "amendment_ref 解析失败"))?;
        if sealed_ok == 0 {
            return Err(VerdictDomainError::amendment_ref_invalid(format!(
                "amendment_ref={} 未指向 task {} 已封存的 blind_first_pass verdict（Req 4.5）",
                input.amendment_ref, input.task_id
            )));
        }
    }

    // 8. 追加不可变 verdict event（§4.1）。
    let verdict_id = format!("v-{}-{}", input.task_id, request_id);
    let events: i64 = tx
        .query_row(
            "SELECT COUNT(*) FROM task_verdict_events WHERE task_id = ?1",
            [&input.task_id],
            |row| row.get(0),
        )
        .map_err(|e| VerdictDomainError::infra_msg(e, "verdict event 计数失败"))?;
    let seq = events + 1;
    tx.execute(
        "INSERT INTO task_verdict_events \
         (verdict_id, task_id, contract_id, contract_revision, contract_hash, phase, \
          view_manifest_hash, snapshot_id, reviewer_identity, clause_results, findings, \
          overall, attestation, amendment_ref, submitted_at, workspace_id, step_id, \
          role_contract_lineage_id, role_contract_revision_id, role_contract_revision, \
          role_contract_hash, canonicalization_version, canonicalization_rules_hash, \
          normalization_version, normalization_rules_hash) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, \
                 ?18, ?19, ?20, ?21, ?22, ?23, ?24, ?25)",
        rusqlite::params![
            verdict_id,
            input.task_id,
            input.task_contract.id,
            input.task_contract.revision,
            input.task_contract.hash,
            input.phase,
            input.view_manifest_hash,
            input.snapshot_id,
            review_identity(&input.agent_id, &input.session_id, &input.model_id),
            input.clause_results,
            input.findings,
            input.overall,
            input.attestation,
            input.amendment_ref,
            literal_now(),
            workspace_id,
            input.step_id,
            binding.role_contract_lineage_id,
            binding.role_contract_revision_id,
            binding.role_contract_revision,
            binding.role_contract_hash,
            binding.canonicalization_version,
            binding.canonicalization_rules_hash,
            norm_version,
            norm_rules_hash,
        ],
    )
    .map_err(|e| VerdictDomainError::infra_msg(e, "追加 task_verdict_events 失败"))?;
    let event_id = tx.last_insert_rowid();

    Ok(DomainWriteOk {
        response: serde_json::json!({
            "ok": true,
            "task_id": input.task_id,
            "verdict_id": verdict_id,
            "event_id": event_id,
            "seq": seq,
            "overall": input.overall,
            "phase": input.phase,
            "request_id": request_id,
            "step_id": input.step_id,
            "workspace_id": workspace_id,
            "normalization_version": norm_version,
            "normalization_rules_hash": norm_rules_hash,
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
            "amendment_ref": input.amendment_ref,
        }),
    })
}

/// reviewer lease/fencing 重检（§4.1；只读，不创建/释放/续期）。
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
) -> Result<(), VerdictDomainError> {
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
        .map_err(|e| VerdictDomainError::infra_msg(e, "task_leases 读取失败"))?;
    let (lease_id, token_hash, active_counter, expires_at, lease_agent, lease_session, lease_model) =
        row.ok_or_else(|| {
            VerdictDomainError::lease(
                ERR_LEASE_NOT_FOUND,
                format!(
                    "task={task_id} role={role} 无 active lease，verdict.submit 需要先 acquire_lease"
                ),
            )
        })?;
    if sha256_hex(token.as_bytes()) != token_hash {
        return Err(VerdictDomainError::lease(
            ERR_LEASE_TOKEN_MISMATCH,
            format!("token hash 不匹配 (lease_id={lease_id})"),
        ));
    }
    if now > expires_at {
        return Err(VerdictDomainError::lease(
            ERR_LEASE_EXPIRED,
            format!("lease {lease_id} 已过期 (expires_at={expires_at:.1}, now={now:.1})"),
        ));
    }
    if fencing_counter != active_counter {
        return Err(VerdictDomainError::lease(
            ERR_LEASE_FENCING_STALE,
            format!(
                "fencing counter {fencing_counter} != 当前 {active_counter}；旧持有者写入被拒绝"
            ),
        ));
    }
    if agent_id != lease_agent || session_id != lease_session || model_id != lease_model {
        return Err(VerdictDomainError::lease(
            ERR_LEASE_HOLDER_MISMATCH,
            format!("holder Identity 与 lease ({lease_id}) 不一致"),
        ));
    }
    Ok(())
}

/// 校验 Task Contract 绑定的 normalization 规则可用（§4.2/§8.1.5）：
/// - 绑定非空；
/// - `verdict_normalization_rules` 存在该 version 且 `rules_hash` 与绑定完全一致；
/// - 该 rule_set 未被撤销；
/// 任一违反 → `E_VERDICT_NORMALIZATION_UNAVAILABLE`（一律 UNVERIFIED，绝不进 pass 路径）。
/// 返回校验通过的 `(normalization_version, normalization_rules_hash)`。
fn validate_normalization_binding(
    tx: &Connection,
    bound_version: &str,
    bound_hash: &str,
) -> Result<(String, String), VerdictDomainError> {
    if bound_version.is_empty() || bound_hash.is_empty() {
        return Err(VerdictDomainError::normalization_unavailable(format!(
            "Task Contract 未绑定 normalization 规则（version={bound_version:?} hash={bound_hash:?}）；\
             verdict.submit 拒绝（§4.2）"
        )));
    }
    let row: Option<(String, String)> = tx
        .query_row(
            "SELECT verdict_rule_set_id, rules_hash FROM verdict_normalization_rules \
             WHERE normalization_version = ?1",
            [bound_version],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(|e| VerdictDomainError::infra_msg(e, "verdict_normalization_rules 读取失败"))?;
    let (rule_set_id, rules_hash) = row.ok_or_else(|| {
        VerdictDomainError::normalization_unavailable(format!(
            "normalization_version={bound_version} 无对应规则 row（缺失 → UNVERIFIED）"
        ))
    })?;
    if rules_hash != bound_hash {
        return Err(VerdictDomainError::normalization_unavailable(format!(
            "normalization binding hash 不匹配 bound={bound_hash} row={rules_hash}（→ UNVERIFIED）"
        )));
    }
    let revoked: i64 = tx
        .query_row(
            "SELECT COUNT(*) FROM verdict_normalization_rule_revocations \
             WHERE verdict_rule_set_id = ?1",
            [&rule_set_id],
            |row| row.get(0),
        )
        .map_err(|e| VerdictDomainError::infra_msg(e, "normalization revocation 读取失败"))?;
    if revoked > 0 {
        return Err(VerdictDomainError::normalization_unavailable(format!(
            "normalization rule_set {rule_set_id} 已撤销（首次 evaluation 禁止使用 → UNVERIFIED）"
        )));
    }
    Ok((bound_version.to_string(), rules_hash))
}

// ---------------------------------------------------------------------------
// Versioned normalization 消费与有效投影（只读，fail-closed）
// ---------------------------------------------------------------------------

/// 从 `rules_c14n` payload 规范化 `overall`（§4.2）：映射命中 → 映射值；否则 v1 冻结值
/// `pass`/`block` 保持自身；任何无法无歧义映射的值 → `UNVERIFIED`，绝不默认成 `pass`。
pub fn normalize_overall(raw: &str, payload: &serde_json::Value) -> String {
    if let Some(map) = payload.get("overall_map").and_then(|v| v.as_object()) {
        if let Some(mapped) = map.get(raw).and_then(|v| v.as_str()) {
            return mapped.to_string();
        }
    }
    match raw {
        "pass" => "pass".to_string(),
        "block" => "block".to_string(),
        _ => "UNVERIFIED".to_string(),
    }
}

/// 从 `rules_c14n` payload 规范化 `phase`（§4.2）：映射命中用映射值；未命中保持原值。
pub fn normalize_phase(raw: &str, payload: &serde_json::Value) -> String {
    if let Some(map) = payload.get("phase_map").and_then(|v| v.as_object()) {
        if let Some(mapped) = map.get(raw).and_then(|v| v.as_str()) {
            return mapped.to_string();
        }
    }
    raw.to_string()
}

/// 已解析的 normalization 规则（有效投影消费用）。
pub struct NormalizationRules {
    pub normalization_version: String,
    pub rules_hash: String,
    pub payload: serde_json::Value,
}

/// grep 一个有效（非撤销）normalization rule_set 供只读投影/Gate 消费；去重并校验 row
/// 自洽：缺 row / rules_hash 不是 `sha256:` 前缀 / 已撤销 → `Err(...)`（fail-closed）。
pub fn load_active_normalization_rules(
    tx: &Connection,
    normalization_version: &str,
) -> Result<NormalizationRules, DaemonRpcError> {
    let row: Option<(String, String, String)> = tx
        .query_row(
            "SELECT verdict_rule_set_id, rules_hash, rules_payload_json \
             FROM verdict_normalization_rules WHERE normalization_version = ?1",
            [normalization_version],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()
        .map_err(|e| infra_error(&format!("verdict_normalization_rules 读取失败: {e}")))?;
    let (rule_set_id, rules_hash, rules_payload_json) = row.ok_or_else(|| {
        infra_error(&format!(
            "normalization_version {normalization_version} 无规则 row → UNVERIFIED (fail-closed)"
        ))
    })?;
    // 已验证规则被 revoke：首次 evaluation 禁止使用（历史读取按原 row 重放，见调用方）。
    let revoked: i64 = tx
        .query_row(
            "SELECT COUNT(*) FROM verdict_normalization_rule_revocations \
             WHERE verdict_rule_set_id = ?1",
            [&rule_set_id],
            |row| row.get(0),
        )
        .map_err(|e| infra_error(&format!("normalization revocation 读取失败: {e}")))?;
    if revoked > 0 {
        return Err(infra_error(&format!(
            "normalization rule_set {rule_set_id} 已撤销 → UNVERIFIED (fail-closed)"
        )));
    }
    let payload = serde_json::from_str(&rules_payload_json).map_err(|e| {
        infra_error(&format!("normalization rules payload 非法 JSON: {e}"))
    })?;
    Ok(NormalizationRules {
        normalization_version: normalization_version.to_string(),
        rules_hash,
        payload,
    })
}

/// 有效 verdict 投影（§4.1/§4.2）：由 task_verdict_events 派生，versioned normalization 消费。
#[derive(Debug, Clone, PartialEq)]
pub struct VerdictProjection {
    pub verdict_id: String,
    pub task_id: String,
    pub step_id: String,
    pub phase: String,
    /// 按该事件持久化的 normalization binding 复算后的 `pass`/`block`/`UNVERIFIED`。
    pub normalized_overall: String,
    pub normalized_phase: String,
    pub normalization_version: String,
    pub normalization_rules_hash: String,
    pub amendment_ref: String,
    pub submitted_at: f64,
}

/// fail-closed：读取某 task 的有效 verdict 投影（按 `submitted_at`/id 升序，即先
/// `blind_first_pass` 后 `post_reveal_amendment`）。每个事件用它自己持久化的
/// normalization binding 规则集复算（§8.1.5："同一历史 raw payload 永远可按当时规则复算"）；
/// 该事件绑定缺失/规则 row 撤销/缺失或 hash 不匹配 → 该事件投影为 `UNVERIFIED`，不以其
/// 它规则替代。查询/解析 infra 失败返回 `Err`；无事件返回空 Vec（由调用方决定 Gate 结果）。
pub fn read_effective_verdicts(
    tx: &Connection,
    task_id: &str,
) -> Result<Vec<VerdictProjection>, DaemonRpcError> {
    let mut statement = tx
        .prepare(
            "SELECT verdict_id, task_id, step_id, phase, overall, normalization_version, \
                    normalization_rules_hash, amendment_ref, submitted_at \
             FROM task_verdict_events WHERE task_id = ?1 \
             ORDER BY id ASC",
        )
        .map_err(|e| infra_error(&format!("task_verdict_events 读取失败: {e}")))?;
    let rows = statement
        .query_map([task_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, String>(6)?,
                row.get::<_, String>(7)?,
                row.get::<_, f64>(8)?,
            ))
        })
        .map_err(|e| infra_error(&format!("task_verdict_events 遍历失败: {e}")))?;
    let mut projections = Vec::new();
    for row in rows {
        let (
            verdict_id,
            task_id,
            step_id,
            phase,
            overall,
            norm_version,
            norm_hash,
            amendment_ref,
            submitted_at,
        ) = row.map_err(|e| infra_error(&format!("task_verdict_events 行读取失败: {e}")))?;
        // 每个事件用其持久化的规则集复算；绑定缺失/撤销/规则缺失 → UNVERIFIED。
        let loaded = if norm_version.is_empty() {
            None
        } else {
            load_active_normalization_rules(tx, &norm_version).ok()
        };
        let (normalized_overall, normalized_phase) = match &loaded {
            Some(rules) => (
                normalize_overall(&overall, &rules.payload),
                normalize_phase(&phase, &rules.payload),
            ),
            None => ("UNVERIFIED".to_string(), phase.clone()),
        };
        projections.push(VerdictProjection {
            verdict_id,
            task_id,
            step_id,
            phase,
            normalized_overall,
            normalized_phase,
            normalization_version: norm_version,
            normalization_rules_hash: norm_hash,
            amendment_ref,
            submitted_at,
        });
    }
    Ok(projections)
}

// ---------------------------------------------------------------------------
// 基础设施/工具 helpers（与 task 2 对齐的本地副本；所有权边界禁止越界编辑）
// ---------------------------------------------------------------------------

/// reviewer_identity 摘要：P3 形态 agent_id/session_id/model_id（§8.1.5 注释）。
fn review_identity(agent_id: &str, session_id: &str, model_id: &str) -> String {
    if agent_id.is_empty() && session_id.is_empty() && model_id.is_empty() {
        String::new()
    } else {
        serde_json::json!({
            "agent_id": agent_id,
            "session_id": session_id,
            "model_id": model_id,
        })
        .to_string()
    }
}

/// 权威 UTC 秒值（微秒精度）文本；真实接入由 Authoritative_Clock 产生。
fn literal_now() -> String {
    format!("{:.6}", now_unix())
}

/// Unix 时间戳秒（float，兼容 task_verdict_events.submitted_at REAL）。
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