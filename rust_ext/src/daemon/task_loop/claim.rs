//! 1C：原生 `task.claim` 领域模块（step→Role Contract binding，cw-role-handoff-task-loop.md §8.1.4）。
//!
//! 只实现 `task_step_role_contract_bindings` 的 v1 写入语义 + fail-closed read projection；
//! 不编辑 Role Contract payload/hash、operation store 或 Gate（所有权边界见 `mod.rs`）。
//!
//! 语义（§8.1.4 / §3.4）：
//! - `task_step_role_contract_bindings` 是本协议唯一的 step→Role Contract 真相源；不复用
//!   历史写空字符串的 `role_contracts.step_id`。
//! - 一个可领取步骤必须恰有一个按 binding revision 链推导出的有效 binding；重绑只追加更高
//!   binding_revision，不能 UPDATE 任何历史 payload。binding_revision>1 必须指向同一 step 的
//!   n-1（`supersedes_binding_id`），最高连续 revision 才是 current binding。
//! - 写入事务必须重检三方一致性：task 的逻辑 workspace 只来自不可变 `task_workspace_bindings`，
//!   请求的 `role_contract_revision_id` 必须存在且其 lineage 的 (task_id, workspace_id) 与
//!   task workspace binding 一致；跨 workspace、跨 task、分叉或断链一律确定性拒绝。
//! - `task.claim` 在存在 unresolved failed step 时必须提供显式的 `remediation_step_id`（§3.4）：
//!   缺少 → `E_REMEDIATION_STEP_REQUIRED`；指定步骤不是当前 remediation / 试图领取后续普通
//!   step → `E_REMEDIATION_STEP_MISMATCH`；同一 `(task_id, remediation_step_id, request_id)`
//!   在同一 `TASK_DB_LEDGER` 事务中精确领取该步骤。
//! - fail-closed read projection：读到不连续的 binding 链、悬空 revision、或 lineage 与
//!   task/workspace 不一致 → 一律 `UNVERIFIED`（`read_current_binding` 返回 None）。
//!
//! 事务/savepoint/ledger 语义由 foundation 独占的 `TaskMutationExecutor` wrapper 统一落实
//! （§3.3/§4.3）：本模块只提供 `apply_claim` 领域回调（写 step binding），以 `claim_step` 为
//! 入口构造 `StrictParsedEnvelope` 并委托 wrapper。cutover（1D3A/1D3B）前 route 仍
//! fail-closed，本入口供领域测试与 1C 验收复用。

use rusqlite::{Connection, OptionalExtension};

use crate::daemon::dispatch::DaemonRpcError;
use super::executor::TaskMutationExecutor;
use super::types::{
    DomainOutcome, FrozenAuthorityInput, InfrastructureError, InvocationClass,
    StableDomainError, StrictParsedEnvelope, TaskDomainTx,
};

/// 确定性拒绝：task 没有不可变 task workspace binding → 无法确定逻辑 workspace 归属。
pub const ERR_TASK_BINDING_REQUIRED: &str = "E_TASK_BINDING_REQUIRED";
/// 确定性拒绝：step 不属于 task / revision 或 lineage 悬空 / 三方不一致（跨 workspace、
/// 跨 task）/ binding 链分叉或断链 → 按 §8.1.4 一律拒绝写入。
pub const ERR_STEP_BINDING_INVALID: &str = "E_STEP_BINDING_INVALID";
/// 确定性拒绝：存在 unresolved failed step 但 claim 未提供 `remediation_step_id`（§3.4）。
pub const ERR_REMEDIATION_STEP_REQUIRED: &str = "E_REMEDIATION_STEP_REQUIRED";
/// 确定性拒绝：指定 `remediation_step_id` 不是该失败步骤的当前 remediation，或试图领取后续
/// 普通 step（§3.4）。
pub const ERR_REMEDIATION_STEP_MISMATCH: &str = "E_REMEDIATION_STEP_MISMATCH";
/// 事务/savepoint/ledger 基础设施失败（InfrastructureError 语义，回滚 outer tx）。
pub const ERR_TASK_DB_TRANSACTION: &str = "E_TASK_DB_TRANSACTION";

/// `task.claim` 的 ledger dedup key（固定 (workspace_instance_id, method, request_id)）。
pub struct LedgerKey {
    pub workspace_instance_id: String,
    pub method: String,
    pub request_id: String,
}

/// `task.claim` 的领域参数（经严格 envelope.params 校验后传入；不含 key 内字段）。
#[derive(Debug)]
pub struct ClaimStepInput {
    pub task_id: String,
    /// 要领取/绑定的步骤 id（remediation 场景 = remediation step 本身）。
    pub step_id: String,
    /// 绑定到该 step 的 Role Contract revision id（§8.1.4 以 revision id 外键引用）。
    pub role_contract_revision_id: String,
    /// 显式 remediation step id（§3.4）；空字符串 = 未提供。
    pub remediation_step_id: String,
    /// 授权写入者（daemon 侧 peer identity；进入 executor 前由调用方填充）。
    pub created_by: String,
}

impl ClaimStepInput {
    /// 从 `StrictParsedEnvelope.params` 严格解析领域输入（1D3A cutover 由 `route.rs` 内部
    /// validation 路由调用）。`task_id`/`step_id`/`role_contract_revision_id` 任一缺失或非
    /// 字符串即 `invalid_params`，不进入 executor；`remediation_step_id`/`created_by` 可选。
    pub fn from_params(params: &serde_json::Value) -> Result<Self, DaemonRpcError> {
        let get_required = |key: &str| -> Result<String, DaemonRpcError> {
            params
                .get(key)
                .and_then(|v| v.as_str())
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .ok_or_else(|| {
                    DaemonRpcError::invalid_params(format!("task.claim 缺少字段: {key}"))
                })
        };
        let get_optional = |key: &str| -> String {
            params
                .get(key)
                .and_then(|v| v.as_str())
                .map(|s| s.trim().to_string())
                .unwrap_or_default()
        };
        Ok(ClaimStepInput {
            task_id: get_required("task_id")?,
            step_id: get_required("step_id")?,
            role_contract_revision_id: get_required("role_contract_revision_id")?,
            remediation_step_id: get_optional("remediation_step_id"),
            created_by: get_optional("created_by"),
        })
    }
}

/// fail-closed read projection：step 的 current binding（verified）。
///
/// 只有满足全部条件才返回 `Some`：
/// - 该 step 的 binding 链连续（COUNT == MAX(binding_revision)）且非空；
/// - 引用的 `role_contract_revisions` 行存在；
/// - lineage 的 (task_id, workspace_id) 与 binding 自身一致。
/// 任一不满足即 `None`（UNVERIFIED），调用方不得以进程状态补齐。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StepBindingProjection {
    pub binding_id: String,
    pub workspace_id: i64,
    pub task_id: String,
    pub step_id: String,
    pub role_contract_lineage_id: String,
    pub role_contract_revision_id: String,
    pub role_contract_revision: i64,
    pub role_contract_hash: String,
    pub canonicalization_version: String,
    pub canonicalization_rules_hash: String,
    pub binding_revision: i64,
    pub supersedes_binding_id: Option<String>,
    pub authoritative_created_at: String,
}

/// fail-closed：读取 step 的 current binding；链不连续/悬空/三方不一致 → `None`（UNVERIFIED）。
/// `workspace_id` 必须由调用方从 `task_workspace_bindings` 取逻辑 workspace（§3.4 第 292-296 行）。
pub fn read_current_binding(
    conn: &Connection,
    workspace_id: i64,
    task_id: &str,
    step_id: &str,
) -> Result<Option<StepBindingProjection>, DaemonRpcError> {
    // 1. 链连续性：COUNT 必须等于 MAX(binding_revision)，且至少一条（§8.1.4 分叉/断链即 UNVERIFIED）。
    let (count, max): (i64, i64) = conn
        .query_row(
            "SELECT COUNT(*), COALESCE(MAX(binding_revision), 0) \
             FROM task_step_role_contract_bindings \
             WHERE workspace_id = ?1 AND task_id = ?2 AND step_id = ?3",
            rusqlite::params![workspace_id, task_id, step_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|e| infra_error(&format!("step binding 链读取失败: {e}")))?;
    if count != max || max == 0 {
        return Ok(None);
    }

    // 2. 最高 binding_revision 行即 current binding。
    let binding: StepBindingProjection = conn
        .query_row(
            "SELECT binding_id, workspace_id, task_id, step_id, \
                    role_contract_lineage_id, role_contract_revision_id, \
                    role_contract_revision, role_contract_hash, \
                    canonicalization_version, canonicalization_rules_hash, \
                    binding_revision, supersedes_binding_id, authoritative_created_at \
             FROM task_step_role_contract_bindings \
             WHERE workspace_id = ?1 AND task_id = ?2 AND step_id = ?3 \
             ORDER BY binding_revision DESC LIMIT 1",
            rusqlite::params![workspace_id, task_id, step_id],
            |row| {
                Ok(StepBindingProjection {
                    binding_id: row.get(0)?,
                    workspace_id: row.get(1)?,
                    task_id: row.get(2)?,
                    step_id: row.get(3)?,
                    role_contract_lineage_id: row.get(4)?,
                    role_contract_revision_id: row.get(5)?,
                    role_contract_revision: row.get(6)?,
                    role_contract_hash: row.get(7)?,
                    canonicalization_version: row.get(8)?,
                    canonicalization_rules_hash: row.get(9)?,
                    binding_revision: row.get(10)?,
                    supersedes_binding_id: row.get(11)?,
                    authoritative_created_at: row.get(12)?,
                })
            },
        )
        .map_err(|e| infra_error(&format!("step current binding 读取失败: {e}")))?;

    // 3. revision 行必须存在（悬空即 UNVERIFIED）。
    let revision_exists: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM role_contract_revisions \
             WHERE role_contract_revision_id = ?1",
            [&binding.role_contract_revision_id],
            |row| row.get(0),
        )
        .map_err(|e| infra_error(&format!("role_contract_revisions 读取失败: {e}")))?;
    if revision_exists == 0 {
        return Ok(None);
    }

    // 4. lineage 的 (task_id, workspace_id) 必须与 binding 三方一致。
    let (lineage_task_id, lineage_workspace_id): (String, i64) = conn
        .query_row(
            "SELECT task_id, workspace_id FROM role_contract_lineages \
             WHERE role_contract_lineage_id = ?1",
            [&binding.role_contract_lineage_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|e| infra_error(&format!("role_contract_lineages 读取失败: {e}")))?;
    if lineage_task_id != binding.task_id || lineage_workspace_id != binding.workspace_id {
        return Ok(None);
    }

    Ok(Some(binding))
}

/// 领域写入成功后的响应（wrapper 统一落 ledger result）。
struct DomainWriteOk {
    response: serde_json::Value,
}

/// 领域执行期间的失败类别（封闭、类型化）。
enum ClaimDomainError {
    /// 确定性、可重放失败：外层在 savepoint 回滚后写可重放 ledger error 并 commit。
    Deterministic { code: String, message: String },
    /// 基础设施失败：回滚 outer transaction、领域写入与 ledger result。
    Infrastructure(DaemonRpcError),
}

impl ClaimDomainError {
    fn binding_invalid(message: String) -> Self {
        ClaimDomainError::Deterministic {
            code: ERR_STEP_BINDING_INVALID.to_string(),
            message,
        }
    }
    fn binding_required(message: String) -> Self {
        ClaimDomainError::Deterministic {
            code: ERR_TASK_BINDING_REQUIRED.to_string(),
            message,
        }
    }
    fn remediation_required(message: String) -> Self {
        ClaimDomainError::Deterministic {
            code: ERR_REMEDIATION_STEP_REQUIRED.to_string(),
            message,
        }
    }
    fn remediation_mismatch(message: String) -> Self {
        ClaimDomainError::Deterministic {
            code: ERR_REMEDIATION_STEP_MISMATCH.to_string(),
            message,
        }
    }
    fn infra(e: DaemonRpcError) -> Self {
        ClaimDomainError::Infrastructure(e)
    }
    fn infra_msg(e: rusqlite::Error, context: &str) -> Self {
        ClaimDomainError::Infrastructure(infra_error(&format!("{context}: {e}")))
    }
    /// 把本地失败类别映射为 wrapper 要求的封闭 `DomainOutcome`（§3.3）。
    fn into_outcome(self) -> DomainOutcome {
        match self {
            ClaimDomainError::Deterministic { code, message: _ } => {
                DomainOutcome::CommitDeterministicError {
                    stable_error: StableDomainError::DeterministicReject { code },
                }
            }
            ClaimDomainError::Infrastructure(error) => {
                DomainOutcome::RollbackInfrastructureError {
                    infrastructure_error: InfrastructureError::Internal {
                        detail: error.message,
                    },
                }
            }
        }
    }
}

/// 以 `task.claim` 领域入口构造 `StrictParsedEnvelope`，委托统一的 `TaskMutationExecutor`
/// wrapper 执行 dedupe → savepoint → 分派 → ledger（§3.3/§4.3）。
///
/// 领域语义（§8.1.4 / §3.4）由 `apply_claim` 承担：task 必须已有不可变 workspace binding；
/// step 必须属于 task；存在 unresolved failed step 时 remediation 规则 fail-closed；revision/
/// lineage 三方一致且 binding 链连续才允许追加；任一违反 → 确定性错误（wrapper 回滚回调局部
/// 写入后写可重放 ledger error 并 commit）；任一 infra 失败 → wrapper 回滚整个 outer transaction。
pub fn claim_step(
    conn: &mut Connection,
    frozen: &FrozenAuthorityInput,
    ledger_key: &LedgerKey,
    input: &ClaimStepInput,
) -> Result<serde_json::Value, DaemonRpcError> {
    let envelope = StrictParsedEnvelope {
        workspace_instance_id: ledger_key.workspace_instance_id.clone(),
        canonical_method: ledger_key.method.clone(),
        request_id: ledger_key.request_id.clone(),
        params: serde_json::json!({
            "task_id": input.task_id,
            "step_id": input.step_id,
            "role_contract_revision_id": input.role_contract_revision_id,
            "remediation_step_id": input.remediation_step_id,
            "created_by": input.created_by,
        }),
        invocation_class: InvocationClass::ExternalTransport,
    };
    TaskMutationExecutor::default().run(conn, &envelope, frozen, |domain_tx, frozen_ref| {
        apply_claim(domain_tx, frozen_ref, input)
    })
}

/// `task.claim` 领域回调：在受保护事务内完成 step binding 一致性校验 + 追加写入，
/// 返回封闭 `DomainOutcome` 交由 wrapper 分派（§3.3）。
fn apply_claim(
    tx: &mut TaskDomainTx<'_>,
    _frozen: &FrozenAuthorityInput,
    input: &ClaimStepInput,
) -> DomainOutcome {
    match write_domain(tx.tx(), input) {
        Ok(ok) => DomainOutcome::CommitSuccess { response: ok.response },
        Err(err) => err.into_outcome(),
    }
}

/// 在 savepoint 事务内执行 step binding 一致性校验 + 追加写入（§8.1.4 / §3.4）。
fn write_domain(
    tx: &Connection,
    input: &ClaimStepInput,
) -> Result<DomainWriteOk, ClaimDomainError> {
    // 1. task 必须已有不可变 workspace binding（§3.4：逻辑 workspace 只来自 binding）。
    let workspace_id: Option<i64> = tx
        .query_row(
            "SELECT workspace_id FROM task_workspace_bindings WHERE task_id = ?1",
            [&input.task_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| ClaimDomainError::infra_msg(e, "task workspace binding 查询失败"))?;
    let workspace_id = workspace_id.ok_or_else(|| {
        ClaimDomainError::binding_required(format!(
            "task {} 没有不可变 task workspace binding（v1 claim 拒绝）",
            input.task_id
        ))
    })?;

    // 2. step 必须属于 task（拒绝跨 task 领取）。
    let step_owned: i64 = tx
        .query_row(
            "SELECT COUNT(*) FROM task_steps WHERE id = ?1 AND task_id = ?2",
            rusqlite::params![input.step_id, input.task_id],
            |row| row.get(0),
        )
        .map_err(|e| ClaimDomainError::infra_msg(e, "task step 归属校验失败"))?;
    if step_owned == 0 {
        return Err(ClaimDomainError::binding_invalid(format!(
            "step {} 不属于 task {}（跨 task 领取拒绝）",
            input.step_id, input.task_id
        )));
    }

    // 3. remediation 精确领取规则（§3.4 第 509-515 行，fail-closed）。
    check_remediation(tx, input)?;

    // 4. revision 行读取（§8.1.4：binding 以 revision id 外键引用，必须存在且归属一致）。
    let (lineage_id, revision, hash, version, rules_hash): (String, i64, String, String, String) = tx
        .query_row(
            "SELECT role_contract_lineage_id, revision, role_contract_hash, \
                    canonicalization_version, canonicalization_rules_hash \
             FROM role_contract_revisions WHERE role_contract_revision_id = ?1",
            [&input.role_contract_revision_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?)),
        )
        .map_err(|e| {
            if matches!(e, rusqlite::Error::QueryReturnedNoRows) {
                ClaimDomainError::binding_invalid(format!(
                    "role_contract_revision_id {} 不存在",
                    input.role_contract_revision_id
                ))
            } else {
                ClaimDomainError::infra_msg(e, "role_contract_revision 读取失败")
            }
        })?;

    // 5. lineage 行读取 + 三方一致性（revision → lineage → task/workspace binding）。
    let (lineage_task_id, lineage_workspace_id): (String, i64) = tx
        .query_row(
            "SELECT task_id, workspace_id FROM role_contract_lineages \
             WHERE role_contract_lineage_id = ?1",
            [&lineage_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|e| {
            if matches!(e, rusqlite::Error::QueryReturnedNoRows) {
                ClaimDomainError::binding_invalid(format!(
                    "role_contract_lineage_id {} 悬空（revision 引用的 lineage 不存在）",
                    lineage_id
                ))
            } else {
                ClaimDomainError::infra_msg(e, "role_contract_lineage 读取失败")
            }
        })?;
    if lineage_task_id != input.task_id {
        return Err(ClaimDomainError::binding_invalid(format!(
            "revision {} 的 lineage 归属 task {}，与请求 task {} 跨 task 不一致",
            input.role_contract_revision_id, lineage_task_id, input.task_id
        )));
    }
    if lineage_workspace_id != workspace_id {
        return Err(ClaimDomainError::binding_invalid(format!(
            "revision {} 的 lineage workspace_id={} 与 task workspace binding workspace_id={} \
             跨 workspace 不一致",
            input.role_contract_revision_id, lineage_workspace_id, workspace_id
        )));
    }

    // 6. 该 step 既有 binding 链连续性校验（§8.1.4：分叉/断链一律拒绝追加）。
    let (count, max): (i64, i64) = tx
        .query_row(
            "SELECT COUNT(*), COALESCE(MAX(binding_revision), 0) \
             FROM task_step_role_contract_bindings \
             WHERE workspace_id = ?1 AND task_id = ?2 AND step_id = ?3",
            rusqlite::params![workspace_id, input.task_id, input.step_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|e| ClaimDomainError::infra_msg(e, "step binding 链读取失败"))?;
    if count != max {
        return Err(ClaimDomainError::binding_invalid(format!(
            "step {} 的 binding 链分叉/断链（count={} max={}），按 UNVERIFIED 拒绝追加",
            input.step_id, count, max
        )));
    }
    let (binding_revision, supersedes_binding_id) = if max == 0 {
        (1, None)
    } else {
        let prev_binding_id: String = tx
            .query_row(
                "SELECT binding_id FROM task_step_role_contract_bindings \
                 WHERE workspace_id = ?1 AND task_id = ?2 AND step_id = ?3 \
                 ORDER BY binding_revision DESC LIMIT 1",
                rusqlite::params![workspace_id, input.task_id, input.step_id],
                |row| row.get(0),
            )
            .map_err(|e| ClaimDomainError::infra_msg(e, "前序 binding 查询失败"))?;
        (max + 1, Some(prev_binding_id))
    };

    // 7. 追加不可变 binding（重绑只追加更高 binding_revision，不 UPDATE 历史 payload）。
    let binding_id = format!(
        "sb-{}-{}-r{}",
        input.task_id, input.step_id, binding_revision
    );
    tx.execute(
        "INSERT INTO task_step_role_contract_bindings \
         (binding_id, workspace_id, task_id, step_id, \
          role_contract_lineage_id, role_contract_revision_id, role_contract_revision, \
          role_contract_hash, canonicalization_version, canonicalization_rules_hash, \
          binding_revision, supersedes_binding_id, created_by, authoritative_created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)",
        rusqlite::params![
            binding_id,
            workspace_id,
            input.task_id,
            input.step_id,
            lineage_id,
            input.role_contract_revision_id,
            revision,
            hash,
            version,
            rules_hash,
            binding_revision,
            supersedes_binding_id,
            input.created_by,
            literal_now(),
        ],
    )
    .map_err(|e| ClaimDomainError::infra_msg(e, "写入 task_step_role_contract_bindings 失败"))?;

    Ok(DomainWriteOk {
        response: serde_json::json!({
            "ok": true,
            "task_id": input.task_id,
            "workspace_id": workspace_id,
            "step_id": input.step_id,
            "binding_id": binding_id,
            "binding_revision": binding_revision,
            "supersedes_binding_id": supersedes_binding_id,
            "role_contract_lineage_id": lineage_id,
            "role_contract_revision_id": input.role_contract_revision_id,
            "role_contract_revision": revision,
            "role_contract_hash": hash,
            "canonicalization_version": version,
            "canonicalization_rules_hash": rules_hash,
        }),
    })
}

/// `task.claim` remediation 精确领取校验（§3.4 第 509-515 行，fail-closed）。
///
/// - 存在 unresolved failed step 且存在其 remediation：请求必须提供且等于该 remediation
///   step，且 `step_id` 必须就是该 remediation step 本身；否则 `E_REMEDIATION_STEP_REQUIRED` /
///   `E_REMEDIATION_STEP_MISMATCH`。
/// - 存在 unresolved failed step 但无当前 remediation（异常状态）：拒绝领取任何 step。
/// - 无 unresolved failed step 却提供 `remediation_step_id`：`E_REMEDIATION_STEP_MISMATCH`。
fn check_remediation(tx: &Connection, input: &ClaimStepInput) -> Result<(), ClaimDomainError> {
    let unresolved = unresolved_failed_step_ids(tx, &input.task_id)?;
    let required = required_remediation_step(tx, &input.task_id)?;
    let given = input.remediation_step_id.as_str();

    // §3.4：只要存在 unresolved failed step，claim 就必须显式提供 remediation_step_id。
    // 缺少 → E_REMEDIATION_STEP_REQUIRED（无论当前是否已登记 remediation）。
    if !unresolved.is_empty() && given.is_empty() {
        return Err(ClaimDomainError::remediation_required(format!(
            "task {} 存在未解析 failed step，必须显式提供 remediation_step_id",
            input.task_id
        )));
    }
    match (required, given) {
        (Some(req), "") => Err(ClaimDomainError::remediation_required(format!(
            "task {} 存在待处理 remediation {req}，必须显式提供 remediation_step_id",
            input.task_id
        ))),
        (Some(req), given) if given != req => Err(ClaimDomainError::remediation_mismatch(
            format!("必须精确领取当前 remediation {req}，不能领取步骤 {given}"),
        )),
        (Some(_), _) => {
            // given == req：必须精确领取 remediation 步骤本身（§3.4）。
            if input.step_id != input.remediation_step_id {
                return Err(ClaimDomainError::remediation_mismatch(format!(
                    "remediation 场景必须精确领取 remediation 步骤本身（step_id={} != remediation_step_id={}）",
                    input.step_id, input.remediation_step_id
                )));
            }
            // remediation step 归属与状态在 step 归属校验之外再确认：必须是同 task 的
            // fix_defect 且可领取（pending/in_progress）；result 需含 remediation_of_step_id。
            let (action, status, result): (String, String, String) = tx
                .query_row(
                    "SELECT action, status, result FROM task_steps WHERE id = ?1 AND task_id = ?2",
                    rusqlite::params![input.step_id, input.task_id],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
                .map_err(|e| ClaimDomainError::infra_msg(e, "remediation step 状态读取失败"))?;
            let claimable = action == "fix_defect"
                && matches!(status.as_str(), "pending" | "in_progress")
                && serde_json::from_str::<serde_json::Value>(&result)
                    .ok()
                    .and_then(|v| {
                        v.get("remediation_of_step_id")
                            .and_then(|item| item.as_str())
                            .map(|s| s.to_string())
                    })
                    .map(|linked| unresolved.contains(&linked))
                    .unwrap_or(false);
            if !claimable {
                return Err(ClaimDomainError::remediation_mismatch(format!(
                    "remediation step {} 不是 task {} 当前可领取的 fix_defect 步骤",
                    input.step_id, input.task_id
                )));
            }
            Ok(())
        }
        (None, "") => Ok(()),
        (None, given) => Err(ClaimDomainError::remediation_mismatch(format!(
            "task {} 无待处理 remediation，禁止提供 remediation_step_id={given}",
            input.task_id
        ))),
    }
}

/// 已解析（有 `step_resolved` resolution event）的 failed step ids（§3.4）。
fn resolved_failed_step_ids(
    tx: &Connection,
    task_id: &str,
) -> Result<Vec<String>, ClaimDomainError> {
    let mut stmt = tx
        .prepare(
            "SELECT reason FROM task_events \
             WHERE task_id = ?1 AND reason_code = 'step_resolved'",
        )
        .map_err(|e| ClaimDomainError::infra_msg(e, "resolution ledger 查询失败"))?;
    let rows = stmt
        .query_map([task_id], |row| row.get::<_, String>(0))
        .map_err(|e| ClaimDomainError::infra_msg(e, "resolution ledger 读取失败"))?;
    let mut resolved = Vec::new();
    for row in rows {
        let raw = row.map_err(|e| ClaimDomainError::infra_msg(e, "resolution event 读取失败"))?;
        if let Ok(value) = serde_json::from_str::<serde_json::Value>(&raw) {
            if let Some(step_id) = value
                .get("failed_step_id")
                .and_then(|item| item.as_str())
                .filter(|item| !item.trim().is_empty())
            {
                resolved.push(step_id.to_string());
            }
        }
    }
    Ok(resolved)
}

/// 未解析的 failed step ids（status='failed' 且无 resolution event）（§3.4）。
fn unresolved_failed_step_ids(
    tx: &Connection,
    task_id: &str,
) -> Result<Vec<String>, ClaimDomainError> {
    let resolved = resolved_failed_step_ids(tx, task_id)?;
    let mut stmt = tx
        .prepare(
            "SELECT id FROM task_steps \
             WHERE task_id = ?1 AND status = 'failed' ORDER BY step_index ASC",
        )
        .map_err(|e| ClaimDomainError::infra_msg(e, "failed steps 查询失败"))?;
    let rows = stmt
        .query_map([task_id], |row| row.get::<_, String>(0))
        .map_err(|e| ClaimDomainError::infra_msg(e, "failed steps 读取失败"))?;
    let mut unresolved = Vec::new();
    for row in rows {
        let step_id = row.map_err(|e| ClaimDomainError::infra_msg(e, "failed step 读取失败"))?;
        if !resolved.contains(&step_id) {
            unresolved.push(step_id);
        }
    }
    Ok(unresolved)
}

/// 找出必须显式领取的 remediation step（fix_defect 且指向 unresolved failed 或
/// reviewer_blocked/adjudicator_returned 来源）；无则 None（§3.4 / 旧 claim 语义）。
fn required_remediation_step(
    tx: &Connection,
    task_id: &str,
) -> Result<Option<String>, ClaimDomainError> {
    let unresolved = unresolved_failed_step_ids(tx, task_id)?;
    let mut stmt = tx
        .prepare(
            "SELECT id, result FROM task_steps \
             WHERE task_id = ?1 AND action = 'fix_defect' \
               AND status IN ('pending', 'in_progress') \
             ORDER BY step_index ASC",
        )
        .map_err(|e| ClaimDomainError::infra_msg(e, "remediation steps 查询失败"))?;
    let rows = stmt
        .query_map([task_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(|e| ClaimDomainError::infra_msg(e, "remediation steps 读取失败"))?;
    for row in rows {
        let (step_id, raw) =
            row.map_err(|e| ClaimDomainError::infra_msg(e, "remediation step 读取失败"))?;
        let metadata = serde_json::from_str::<serde_json::Value>(&raw).unwrap_or(serde_json::Value::Null);
        let linked = metadata
            .get("remediation_of_step_id")
            .and_then(|item| item.as_str())
            .unwrap_or("");
        let source_outcome = metadata
            .get("source_outcome")
            .and_then(|item| item.as_str())
            .unwrap_or("");
        if unresolved.iter().any(|u| u == linked)
            || matches!(source_outcome, "reviewer_blocked" | "adjudicator_returned")
        {
            return Ok(Some(step_id));
        }
    }
    Ok(None)
}

/// 权威 UTC 秒值（微秒精度）文本；真实接入由 Authoritative_Clock 产生。
fn literal_now() -> String {
    format!("{:.6}", now_unix())
}

/// Unix 时间戳秒（float，兼容 SQLite REAL created_at）。
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
