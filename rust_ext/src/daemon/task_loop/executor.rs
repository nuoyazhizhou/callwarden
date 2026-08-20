//! 通用 `TaskMutationExecutor` 与领域事务 wrapper（计划 §3.3）。
//!
//! foundation 独占。输入是不可序列化、私有字段的 `StrictParsedEnvelope`
//! （`workspace_instance_id, canonical_method, request_id, params, invocation_class`）。
//! wrapper 在同一 task-DB connection 上按 §3.3/§4.3 落实：
//!  1. 幂等 dedupe（读路径，开事务前；同 key 同 hash 只读重放结果）；
//!  2. outer transaction + `SAVEPOINT task_domain_callback` 后调用 caller-supplied
//!     `apply_domain(tx)`（只接收受限 `TaskDomainTx` 与冻结 `FrozenAuthorityInput`）；
//!  3. 依封闭 `DomainOutcome` 分派：
//!     - `CommitSuccess`            → RELEASE + ledger result + commit；
//!     - `CommitDeterministicError` → ROLLBACK TO + RELEASE + 可重放 ledger error + commit；
//!     - `RollbackInfrastructureError` → 回滚整个 outer transaction（领域写入与 ledger 一并消失）。
//!
//! 任一 savepoint/ledger 操作失败都转为基础设施失败并回滚 outer transaction。
//! 允许领域 handler 复写其所在模块的 stub，但不得改动本模块、types、dispatch 或他人模块。

use rusqlite::Connection;

use crate::daemon::dispatch::DaemonRpcError;
use super::operation_store::{DedupeOutcome, LedgerProvenance, OperationStore};
use super::types::{
    DomainOutcome, FrozenAuthorityInput, InfrastructureError, StableDomainError,
    StrictParsedEnvelope, TaskDomainTx,
};

/// 用户提供的领域回调；在 wrapper 的 outer transaction 与 savepoint 内执行。
/// 只接收受限 `TaskDomainTx` 与进入回调前冻结的 `FrozenAuthorityInput`。
pub type DomainApply<'a> =
    dyn FnOnce(&mut TaskDomainTx<'a>, &FrozenAuthorityInput) -> DomainOutcome + 'a;

/// 基础设施失败统一错误码（保存点/事务/连接/registry/时钟/IO/内部失败）。
const ERR_MUTATION_INFRASTRUCTURE: &str = "E_TASK_DB_TRANSACTION";

/// 通用的领域变更执行器。
#[derive(Debug, Default)]
pub struct TaskMutationExecutor;

impl TaskMutationExecutor {
    /// 执行领域回调并落实 v1 的 commit/savepoint/ledger 语义（§3.3）。
    ///
    /// `F` 为 HRTB 领域回调：其 `TaskDomainTx` 事务借用由 wrapper 按调用点新鲜产生，
    /// 与调用方传入的回调生命周期解耦，从而把 dedupe/savepoint/ledger 统一收敛到本
    /// wrapper，领域模块只提供纯领域写入回调。
    pub fn run<F>(
        &self,
        conn: &mut Connection,
        envelope: &StrictParsedEnvelope,
        frozen: &FrozenAuthorityInput,
        apply: F,
    ) -> Result<serde_json::Value, DaemonRpcError>
    where
        F: for<'tx> FnOnce(&mut TaskDomainTx<'tx>, &FrozenAuthorityInput) -> DomainOutcome,
    {
        let ledger = OperationStore::default();

        // 1. 幂等去重（读路径，开事务前执行；key 内字段不进入 payload hash）。
        let (rules, canonical_params_hash) = match ledger.dedupe(
            conn,
            &envelope.workspace_instance_id,
            &envelope.canonical_method,
            &envelope.request_id,
            &envelope.params,
        )? {
            DedupeOutcome::Replay { response_or_error_json } => {
                return Ok(response_or_error_json);
            }
            DedupeOutcome::FirstRequest {
                rules,
                canonical_params_hash,
            } => (rules, canonical_params_hash),
        };

        // 2. outer transaction + 领域回调 savepoint。
        let mut tx = conn
            .transaction()
            .map_err(infra_transaction)?;
        tx.execute_batch("SAVEPOINT task_domain_callback")
            .map_err(infra_transaction)?;

        // 3. 领域回调；受限句柄禁止 commit/rollback/savepoint/另开写连接/外部 I/O。
        let outcome = {
            let mut holder = TaskDomainTx::new(&tx);
            apply(&mut holder, frozen)
        };

        // 4. 依封闭类别分派。
        match outcome {
            DomainOutcome::CommitSuccess { response } => {
                tx.execute_batch("RELEASE SAVEPOINT task_domain_callback")
                    .map_err(infra_transaction)?;
                ledger
                    .record_result(
                        &tx,
                        &envelope.workspace_instance_id,
                        &envelope.canonical_method,
                        &envelope.request_id,
                        &rules,
                        &canonical_params_hash,
                        &LedgerProvenance::default(),
                        &response,
                    )?;
                tx.commit().map_err(infra_transaction)?;
                Ok(response)
            }
            DomainOutcome::CommitDeterministicError { stable_error } => {
                let code = stable_error.code();
                let message = stable_error_message(&stable_error);
                tx.execute_batch("ROLLBACK TO task_domain_callback")
                    .map_err(infra_transaction)?;
                tx.execute_batch("RELEASE SAVEPOINT task_domain_callback")
                    .map_err(infra_transaction)?;
                let err_json = serde_json::json!({
                    "ok": false,
                    "code": code,
                    "message": message,
                });
                ledger
                    .record_result(
                        &tx,
                        &envelope.workspace_instance_id,
                        &envelope.canonical_method,
                        &envelope.request_id,
                        &rules,
                        &canonical_params_hash,
                        &LedgerProvenance::default(),
                        &err_json,
                    )?;
                tx.commit().map_err(infra_transaction)?;
                Err(DaemonRpcError::new(code, message))
            }
            DomainOutcome::RollbackInfrastructureError { infrastructure_error } => {
                // `tx` 在此 drop → outer transaction（含领域写入与 ledger）整体回滚。
                Err(infra_error(&infrastructure_error))
            }
        }
    }
}

/// savepoint/事务/commit 等连接级基础设施失败 → 统一基础设施错误码。
fn infra_transaction(error: rusqlite::Error) -> DaemonRpcError {
    DaemonRpcError::new(
        ERR_MUTATION_INFRASTRUCTURE,
        format!("task-DB 事务/连接基础设施失败：{error}"),
    )
}

/// 领域显式上报的基础设施失败 → 统一错误码（保留 detail 便于诊断）。
fn infra_error(error: &InfrastructureError) -> DaemonRpcError {
    let detail = match error {
        InfrastructureError::Internal { detail } => format!("（{detail}）"),
        other => format!("（{other:?}）"),
    };
    DaemonRpcError::new(
        ERR_MUTATION_INFRASTRUCTURE,
        format!("task-DB 基础设施失败{detail}"),
    )
}

/// 确定性拒绝的可重放 message（与 `StableDomainError::code()` 保持稳定）。
fn stable_error_message(error: &StableDomainError) -> String {
    match error {
        StableDomainError::CapabilityDisabled => "公共能力未激活：cutover 完成前 route fail-closed".to_string(),
        StableDomainError::CapabilityRevoked => "公共能力已撤销：permit/authority/evidence 失效".to_string(),
        StableDomainError::HandoffFieldsRequired => "缺少结构化 handoff 所需字段".to_string(),
        StableDomainError::RequestIdReuseMismatch => "request_id 复用但 canonical 参数不一致".to_string(),
        StableDomainError::DeterministicReject { code } => {
            format!("确定性领域拒绝：{code}")
        }
    }
}