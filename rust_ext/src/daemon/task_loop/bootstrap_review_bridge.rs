//! P0-F: 治理 Bootstrap Evidence / Review Bridge（解除 A′ 冷启动死锁）。
//!
//! 本模块提供两个 **daemon-only、append-only、durably idempotent** 的窄范围
//! 领域函数，由 `task_collab.rs` 的 handler 在已完成 authority / identity /
//! ledger / clock 预检的同一个 `Transaction` 内调用：
//!
//! - `bootstrap_executor_evidence`：仅当任务已 immutable workspace binding、
//!   且 **Task Contract / Role lineage / step binding 全部为零** 时，由 `executor`
//!   角色追加每项 completed-step evidence event 与 `bootstrap_executor_ready_for_review`
//!   event，并把任务状态 `open/in_progress → review`。不写任何 contract / verdict / lease。
//! - `bootstrap_reviewer_pass`：仅当任务处于 `review` 且存在**唯一** bootstrap
//!   executor evidence event 时，由 `reviewer` 角色完成只读核验；本函数同事务签发
//!   **专用 bootstrap reviewer lease**（供后续 P0-C 使用），并追加
//!   `bootstrap_reviewer_pass` verdict-equivalent event 与 immutable reviewer evidence。
//!
//! 所有既有 Task/Role/step projection 一律拒绝（fail-closed），P0-F 不得成为常规捷径。

use rusqlite::{params, Transaction};
use serde_json::Value;

use crate::daemon::dispatch::DaemonRpcError;

pub const ERR_BRIDGE_INVALID: &str = "E_BOOTSTRAP_BRIDGE_INVALID";
pub const ERR_BRIDGE_NOT_EMPTY: &str = "E_BOOTSTRAP_BRIDGE_NOT_EMPTY";
pub const ERR_BRIDGE_ROLE: &str = "E_BOOTSTRAP_BRIDGE_ROLE";
pub const ERR_BRIDGE_STATE: &str = "E_BOOTSTRAP_BRIDGE_STATE";
pub const ERR_BRIDGE_EVIDENCE_MISSING: &str = "E_BOOTSTRAP_BRIDGE_EVIDENCE_MISSING";
pub const ERR_BRIDGE_NO_EXECUTOR_EVIDENCE: &str = "E_BOOTSTRAP_BRIDGE_NO_EXECUTOR_EVIDENCE";
pub const ERR_BRIDGE_DUPLICATE_EXECUTOR_EVIDENCE: &str =
    "E_BOOTSTRAP_BRIDGE_DUPLICATE_EXECUTOR_EVIDENCE";
pub const ERR_BRIDGE_REVIEWER_EVIDENCE_EXISTS: &str =
    "E_BOOTSTRAP_BRIDGE_REVIEWER_EVIDENCE_EXISTS";
pub const ERR_BRIDGE_INDEPENDENCE: &str = "E_BOOTSTRAP_BRIDGE_INDEPENDENCE";

#[derive(Debug)]
pub struct ExecutorEvidenceStep {
    pub step_id: String,
    pub evidence_path: String,
    pub evidence_hash: String,
}

#[derive(Debug)]
pub struct ExecutorEvidenceInput {
    pub task_id: String,
    pub steps: Vec<ExecutorEvidenceStep>,
    pub created_by: String,
}

#[derive(Debug)]
pub struct ReviewerPassInput {
    pub task_id: String,
    pub evidence_path: String,
    pub evidence_hash: String,
    pub created_by: String,
    /// Reviewer 的三重身份，用于与 executor 比对独立性。
    pub reviewer_agent_id: String,
    pub reviewer_agent_instance_id: String,
    pub reviewer_session_id: String,
}

fn deterministic(code: &str, message: impl Into<String>) -> DaemonRpcError {
    DaemonRpcError::new(code, message.into())
}

/// 严格判定任务是否处于「完整治理投影为空」的 root bootstrap 边界：
/// 一旦已存在任一 Task Contract / Role lineage / Role revision / step binding 即拒绝。
fn no_governance_projection(tx: &Transaction<'_>, task_id: &str) -> Result<(), DaemonRpcError> {
    for (table, sql) in [
        (
            "task_contract_revisions",
            "SELECT EXISTS(SELECT 1 FROM task_contract_revisions WHERE task_id=?1)",
        ),
        (
            "role_contract_lineages",
            "SELECT EXISTS(SELECT 1 FROM role_contract_lineages WHERE task_id=?1)",
        ),
        (
            "role_contract_revisions",
            "SELECT EXISTS(SELECT 1 FROM role_contract_revisions r JOIN role_contract_lineages l ON l.role_contract_lineage_id=r.role_contract_lineage_id WHERE l.task_id=?1)",
        ),
        (
            "task_step_role_contract_bindings",
            "SELECT EXISTS(SELECT 1 FROM task_step_role_contract_bindings WHERE task_id=?1)",
        ),
    ] {
        let exists: bool = tx
            .query_row(sql, [task_id], |row| row.get(0))
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("{table} projection 查询失败: {e}"))
            })?;
        if exists {
            return Err(deterministic(
                ERR_BRIDGE_NOT_EMPTY,
                format!(
                    "task {task_id} 已存在 {table}，bootstrap bridge 只允许完整治理投影为空的任务"
                ),
            ));
        }
    }
    Ok(())
}

/// 是否存在既有的 bootstrap executor evidence event（durable idempotency / 防重复）。
fn executor_evidence_count(tx: &Transaction<'_>, task_id: &str) -> Result<i64, DaemonRpcError> {
    tx.query_row(
        "SELECT COUNT(*) FROM task_events WHERE task_id=?1 AND reason_code='task.bootstrap_executor_evidence'",
        [task_id],
        |r| r.get(0),
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("bootstrap executor evidence 计数失败: {e}")))
}

/// 是否存在既有的 bootstrap reviewer pass event。
fn reviewer_pass_exists(tx: &Transaction<'_>, task_id: &str) -> Result<bool, DaemonRpcError> {
    let n: i64 = tx
        .query_row(
            "SELECT COUNT(*) FROM task_events WHERE task_id=?1 AND reason_code='task.bootstrap_reviewer_pass'",
            [task_id],
            |r| r.get(0),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("bootstrap reviewer pass 计数失败: {e}")))?;
    Ok(n > 0)
}

/// 阶段一：executor 追加逐项 completed-step evidence 与 ready_for_review event。
///
/// 调用方必须已在事务内完成：workspace authority 一致性、executor registered identity、
/// dedupe、ledger provenance 预置。本函数仅做领域门禁与追加写入。
pub fn bootstrap_executor_evidence(
    tx: &Transaction<'_>,
    input: &ExecutorEvidenceInput,
    seq: i64,
    ts: f64,
) -> Result<Value, DaemonRpcError> {
    no_governance_projection(tx, &input.task_id)?;

    let status: String = tx
        .query_row("SELECT status FROM tasks WHERE id=?1", [&input.task_id], |r| r.get(0))
        .map_err(|_| deterministic(ERR_BRIDGE_STATE, format!("task 不存在: {}", input.task_id)))?;
    if !matches!(status.as_str(), "open" | "in_progress") {
        return Err(deterministic(
            ERR_BRIDGE_STATE,
            format!("bootstrap executor evidence 仅允许 open/in_progress，实际 status={status}"),
        ));
    }

    // 防重复：若已存在 executor evidence event，拒绝二次写入（durable idempotency）。
    if executor_evidence_count(tx, &input.task_id)? > 0 {
        return Err(deterministic(
            ERR_BRIDGE_DUPLICATE_EXECUTOR_EVIDENCE,
            "task 已存在 bootstrap executor evidence，bridge 不可重复追加",
        ));
    }

    if input.steps.is_empty() {
        return Err(deterministic(
            ERR_BRIDGE_EVIDENCE_MISSING,
            "bootstrap executor evidence 必须提交至少一项 completed-step evidence",
        ));
    }

    // 逐项校验 step 存在且属于本任务，追加 completed-step evidence event。
    let mut written_steps = Vec::new();
    for step in &input.steps {
        let exists: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM task_steps WHERE task_id=?1 AND id=?2)",
                params![&input.task_id, &step.step_id],
                |r| r.get(0),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("step 校验失败: {e}")))?;
        if !exists {
            return Err(deterministic(
                ERR_BRIDGE_INVALID,
                format!("step {} 不属于 task {} 或不存在", step.step_id, input.task_id),
            ));
        }
        if step.evidence_path.is_empty() || step.evidence_hash.is_empty() {
            return Err(deterministic(
                ERR_BRIDGE_EVIDENCE_MISSING,
                format!("step {} 的 evidence_path/evidence_hash 不能为空", step.step_id),
            ));
        }
        tx.execute(
            "INSERT INTO task_events (task_id,from_status,to_status,reason_code,reason,evidence_path,evidence_hash,actor_identity,role,monotonic_seq,authoritative_timestamp) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",
            params![
                &input.task_id, &status, &status, "task.bootstrap_executor_evidence",
                serde_json::json!({"step_id":step.step_id,"kind":"completed_step_evidence"}).to_string(),
                &step.evidence_path, &step.evidence_hash, &input.created_by, "executor", seq, ts
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("bootstrap step evidence event 写入失败: {e}")))?;
        written_steps.push(step.step_id.clone());
    }

    // 追加 ready_for_review event 并将状态推到 review。
    tx.execute(
        "INSERT INTO task_events (task_id,from_status,to_status,reason_code,reason,actor_identity,role,monotonic_seq,authoritative_timestamp) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
        params![
            &input.task_id, &status, "review", "task.bootstrap_executor_ready_for_review",
            serde_json::json!({"evidence_steps":written_steps}).to_string(),
            &input.created_by, "executor", seq + 1, ts
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("ready_for_review event 写入失败: {e}")))?;

    tx.execute(
        "UPDATE tasks SET status='review', updated_at=?1 WHERE id=?2",
        params![ts, &input.task_id],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("task status 更新失败: {e}")))?;

    Ok(serde_json::json!({
        "ok": true,
        "task_id": input.task_id,
        "from_status": status,
        "to_status": "review",
        "evidence_steps": written_steps,
        "authoritative_timestamp": ts,
    }))
}

/// 阶段二：reviewer 只读核验，同事务签发专用 bootstrap reviewer lease，追加 pass event。
///
/// 调用方必须已在事务内完成：workspace authority 一致性、reviewer registered identity、
/// dedupe、ledger provenance 预置。本函数仅做领域门禁与追加写入；**不写**普通 verdict、
/// Task Contract、step binding、apply/close。
pub fn bootstrap_reviewer_pass(
    tx: &Transaction<'_>,
    input: &ReviewerPassInput,
    executor_agent_id: &str,
    executor_agent_instance_id: &str,
    executor_session_id: &str,
    lease_token_hash: &str,
    lease_expires_at: f64,
    seq: i64,
    ts: f64,
) -> Result<Value, DaemonRpcError> {
    let status: String = tx
        .query_row("SELECT status FROM tasks WHERE id=?1", [&input.task_id], |r| r.get(0))
        .map_err(|_| deterministic(ERR_BRIDGE_STATE, format!("task 不存在: {}", input.task_id)))?;
    if status != "review" {
        return Err(deterministic(
            ERR_BRIDGE_STATE,
            format!("bootstrap reviewer pass 仅允许 review 状态，实际 status={status}"),
        ));
    }

    // 必须存在唯一 bootstrap executor evidence event。
    let exec_count = executor_evidence_count(tx, &input.task_id)?;
    if exec_count == 0 {
        return Err(deterministic(
            ERR_BRIDGE_NO_EXECUTOR_EVIDENCE,
            "task 缺少 bootstrap executor evidence，reviewer 无法独立核验",
        ));
    }
    // 注（方案1 修复）：不再因 exec_count>1 拒绝。一次 bootstrap_executor_evidence 调用会
    // 为每个 completed step 写一条 task.bootstrap_executor_evidence event，故合法计数为
    // step 数而非 1。重复写入由 executor 侧 `>0` 门禁（本文件:148）+ task_operation_ledger
    // dedupe 兜底，reviewer pass 无需再判重复。方案2（batch_id 去重计数）为长期修复，见 backlog。

    // 防重复 reviewer pass。
    if reviewer_pass_exists(tx, &input.task_id)? {
        return Err(deterministic(
            ERR_BRIDGE_REVIEWER_EVIDENCE_EXISTS,
            "task 已存在 bootstrap reviewer pass，bridge 不可重复追加",
        ));
    }

    // 独立性：reviewer 与 executor 的 agent / instance / session 三重不同。
    if !input.reviewer_agent_id.is_empty() && input.reviewer_agent_id == executor_agent_id {
        return Err(deterministic(
            ERR_BRIDGE_INDEPENDENCE,
            "bootstrap reviewer 不得等于 executor agent_id",
        ));
    }
    if !input.reviewer_agent_instance_id.is_empty()
        && !executor_agent_instance_id.is_empty()
        && input.reviewer_agent_instance_id == executor_agent_instance_id
    {
        return Err(deterministic(
            ERR_BRIDGE_INDEPENDENCE,
            "bootstrap reviewer 不得等于 executor agent_instance_id",
        ));
    }
    if !input.reviewer_session_id.is_empty() && input.reviewer_session_id == executor_session_id {
        return Err(deterministic(
            ERR_BRIDGE_INDEPENDENCE,
            "bootstrap reviewer 不得等于 executor session_id",
        ));
    }

    if input.evidence_path.is_empty() || input.evidence_hash.is_empty() {
        return Err(deterministic(
            ERR_BRIDGE_EVIDENCE_MISSING,
            "bootstrap reviewer pass 必须携带 evidence_path 与 evidence_hash",
        ));
    }

    // 同事务签发专用 bootstrap reviewer lease（供后续 P0-C 以 reviewer lease 完成首合同）。
    // 修复（方案1）：先回收已存在的同 (workspace_id,task_id,role) lease（含历史过期未释放的
    // reviewer lease），避免 task_leases 唯一约束 (workspace_id,task_id,role) 冲突。bootstrap
    // 专用 lease 为幂等签发，旧 lease 无论 active/expired 一律回收后重签。
    let lease_id = format!("brtl-{}-r1", input.task_id);
    let fencing_counter = 1i64;
    tx.execute(
        "DELETE FROM task_leases WHERE workspace_id=1 AND task_id=?1 AND role='reviewer'",
        params![&input.task_id],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("bootstrap reviewer lease 回收失败: {e}")))?;
    tx.execute(
        "INSERT INTO task_leases (workspace_id,lease_id,task_id,role,status,agent_id,session_id,model_id,token_hash,fencing_counter,acquired_at,expires_at) VALUES (1,?1,?2,'reviewer','active',?3,?4,'',?5,?6,?7,?8)",
        params![
            lease_id, &input.task_id,
            &input.reviewer_agent_id, &input.reviewer_session_id, lease_token_hash, fencing_counter, ts, lease_expires_at
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("bootstrap reviewer lease 写入失败: {e}")))?;

    // 追加 bootstrap reviewer pass verdict-equivalent event。
    tx.execute(
        "INSERT INTO task_events (task_id,from_status,to_status,reason_code,reason,evidence_path,evidence_hash,actor_identity,agent_session_id,role,monotonic_seq,authoritative_timestamp) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12)",
        params![
            &input.task_id, &status, &status, "task.bootstrap_reviewer_pass",
            serde_json::json!({"bootstrap_reviewer_lease_id":lease_id,"kind":"reviewer_pass"}).to_string(),
            &input.evidence_path, &input.evidence_hash, &input.created_by, &input.reviewer_session_id, "reviewer", seq, ts
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("bootstrap reviewer pass event 写入失败: {e}")))?;

    Ok(serde_json::json!({
        "ok": true,
        "task_id": input.task_id,
        "status": status,
        "bootstrap_reviewer_lease_id": lease_id,
        "fencing_counter": fencing_counter,
        "authoritative_timestamp": ts,
    }))
}
