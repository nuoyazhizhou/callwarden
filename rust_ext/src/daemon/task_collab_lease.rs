//! Task claim、assignment 与 lease domain。
//!
//! RPC 方法仍实现于同一 `TaskCollabStore`，共享父 `task_collab` 模块的 binding、
//! contract、identity 与 lease validation helper，保持原有事务和 fencing 语义。

use super::*;

/// task-level remediation 的 provenance 判定（无源步骤场景）。
///
/// `required_remediation_step`（task_collab_shared.rs）在 `remediation_of_step_id`
/// 为空时，已按 `source_outcome ∈ {reviewer_blocked, adjudicator_returned}` 选中该
/// remediation；本函数是与之对应的**领取侧**校验，语义必须保持一致：
///
/// 1. `source_outcome` 必须是上述两种治理退回之一；
/// 2. `source_verdict_id` 与 `source_handoff_event_id` 必须存在且非空——它们替代源步骤
///    承载 provenance，缺少任一即无法审计，拒绝领取；
/// 3. 该 task 不能存在 unresolved failed step——存在时必须走 step-level 语义，
///    不允许用 task-level provenance 绕过失败步骤的整改绑定。
///
/// 任一条件不成立即返回 false（fail-closed）。`task_loop/claim.rs` 的 cutover 路径
/// 维护同语义的独立副本（与该模块中 lease/contract 校验的既有双轨做法一致）。
pub(crate) fn task_level_remediation_provenance_ok(
    metadata: &Value,
    unresolved: &[String],
) -> bool {
    if !unresolved.is_empty() {
        return false;
    }
    let non_empty_str = |key: &str| -> bool {
        metadata
            .get(key)
            .and_then(|item| item.as_str())
            .map(|item| !item.trim().is_empty())
            .unwrap_or(false)
    };
    let source_outcome = metadata
        .get("source_outcome")
        .and_then(|item| item.as_str())
        .unwrap_or("");
    if !matches!(source_outcome, "reviewer_blocked" | "adjudicator_returned") {
        return false;
    }
    // handoff event id 由 daemon 以整数 rowid 写入；同时接受非空字符串形式。
    let handoff_event_ok = metadata
        .get("source_handoff_event_id")
        .map(|item| match item {
            Value::Number(_) => true,
            Value::String(text) => !text.trim().is_empty(),
            _ => false,
        })
        .unwrap_or(false);
    non_empty_str("source_verdict_id") && handoff_event_ok
}

impl TaskCollabStore {
    pub fn handle_task_claim(
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
        let owner_key = peer.owner_key();
        let agent_session_id = params
            .get("agent_session_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_else(|| owner_key.clone());
        let mut agent_session_id = agent_session_id;
        let requested_remediation_step_id = params
            .get("remediation_step_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();

        // A2/A3 身份与合同门禁（fail-closed）。
        // P0-L R1：policy 路由先行——仅 legacy 策略（或无合同历史任务）保留 runtime
        // identity 作为强制授权门禁；role_worker_v1 下稳定 Role Worker credential 是
        // 唯一授权锚点，identity 为可选 append-only provenance；missing/unknown
        // policy 一律拒绝，绝不隐式降级。
        let identity = parse_action_identity(params)?;
        let ts = task_now_ts();
        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        // worker 路径状态：claim 角色归属与接管替换 provenance。
        let mut worker_claim_role: Option<String> = None;
        let mut assignment_holder_agent_id = owner_key.clone();
        let mut replacement_info: Option<Value> = None;

        let policy_state = get_current_task_contract_policy_state(&tx, task_id)?;
        match &policy_state {
            TaskContractPolicyState::NoContractRevision => {}
            TaskContractPolicyState::Declared(policy) if policy == POLICY_LEGACY_IDENTITY_V1 => {}
            TaskContractPolicyState::Declared(policy) if policy == POLICY_ROLE_WORKER_V1 => {
                // P0-L R1：worker-first claim 门禁（runtime identity 不参与授权）。
                let bound_workspace = task_bound_workspace_id(&tx, task_id, None)?;
                let auth = parse_role_worker_auth(params)?.ok_or_else(|| {
                    DaemonRpcError::new(
                        ERR_POLICY_REQUIRED,
                        format!(
                            "任务 {task_id} identity_policy=role_worker_v1，claim 必须携带 role_worker_auth（worker-first 授权锚点）"
                        ),
                    )
                })?;
                // worker 映射角色是唯一角色锚点（不供应客户端 identity.role）。
                let worker_role = validate_role_worker_first(
                    &tx,
                    &auth,
                    &owner_key,
                    bound_workspace,
                    task_id,
                    "task.claim",
                )?;
                if !matches!(
                    worker_role.as_str(),
                    "executor" | "reviewer" | "adjudicator"
                ) {
                    return Err(DaemonRpcError::new(
                        ERR_ROLE_MISMATCH,
                        format!("role worker 映射角色 {worker_role} 不是治理角色"),
                    ));
                }
                // Role Contract 校验以 worker 映射角色为准。
                if let Some(contract) = get_current_role_contract(&tx, task_id, &worker_role)? {
                    verify_contract_claim_match(task_id, &worker_role, &contract, params)?;
                }
                // runtime identity（如提供）只追加 provenance，不参与任何授权比较。
                if let Some(id) = identity.as_ref() {
                    record_action_identity(&tx, task_id, id, "task.claim", self.next_seq(), ts)?;
                }
                agent_session_id = auth.role_session_id.clone();
                assignment_holder_agent_id = auth.role_worker_id.clone();
                worker_claim_role = Some(worker_role.clone());
                replacement_info = Some(serde_json::json!({
                    "replacement_kind": "role_worker",
                    "replacement_agent_id": auth.role_worker_id,
                    "replacement_session_id": auth.role_session_id,
                    "replacement_role": worker_role,
                }));
            }
            TaskContractPolicyState::Declared(unknown) => {
                return Err(DaemonRpcError::new(
                    ERR_POLICY_MISMATCH,
                    format!(
                        "任务 {task_id} identity policy {unknown} 未知，禁止 claim（禁止隐式降级为 legacy）"
                    ),
                ));
            }
            TaskContractPolicyState::Unresolved => {
                return Err(DaemonRpcError::new(
                    ERR_POLICY_MISMATCH,
                    format!(
                        "任务 {task_id} 合同 revision 缺少可解析 identity_policy，禁止 claim（禁止隐式降级为 legacy）"
                    ),
                ));
            }
        }

        // legacy 路径：保持全部既有刚性身份门禁不变（事务内执行）。
        if worker_claim_role.is_none() {
            if let Some(id) = &identity {
                // 1. 未注册身份 fail-closed
                let registered = tx
                .query_row(
                    "SELECT agent_instance_id, session_id, role, status FROM agent_registrations
                     WHERE agent_id = ?1",
                    params![id.agent_id],
                    |r| {
                        Ok((
                            r.get::<_, String>(0)?,
                            r.get::<_, String>(1)?,
                            r.get::<_, String>(2)?,
                            r.get::<_, String>(3)?,
                        ))
                    },
                )
                .optional()
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("查询 agent_registrations 失败: {}", e))
                })?;
                let reg = registered.ok_or_else(|| {
                    DaemonRpcError::new(
                        "E_IDENTITY_UNREGISTERED",
                        format!(
                            "agent {} 未注册身份，禁止领取任务（fail-closed）",
                            id.agent_id
                        ),
                    )
                })?;
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
                check_role_independence(
                    &tx,
                    &id.agent_instance_id,
                    &id.session_id,
                    &id.role,
                    &id.agent_id,
                )?;
                // 5. Role Contract 校验（合同任务必须携带 contract_claim 且 skill/prompt hash 一致）
                if let Some(contract) = get_current_role_contract(&tx, task_id, &id.role)? {
                    verify_contract_claim_match(task_id, &id.role, &contract, params)?;
                }
            } else if task_has_contracts(&tx, task_id)? {
                // 合同任务必须携带 identity（fail-closed）
                return Err(DaemonRpcError::new(
                    "E_IDENTITY_REQUIRED",
                    format!(
                        "任务 {} 已冻结 Role Contract，领取必须携带 identity",
                        task_id
                    ),
                ));
            }
            if let Some(id) = identity.as_ref() {
                replacement_info = Some(serde_json::json!({
                    "replacement_agent_id": id.agent_id,
                    "replacement_session_id": id.session_id,
                    "replacement_role": id.role,
                }));
            }
        }

        // 检查 task 当前状态
        let current_status: String = tx
            .query_row(
                "SELECT status FROM tasks WHERE id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .map_err(|_| {
                DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id))
            })?;

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
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("查询 remediation 步骤失败: {}", e))
                })?;
            let Some((action, step_status, result, remediation_task_id)) = remediation else {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_STEP_MISMATCH",
                    "指定 remediation_step_id 不存在",
                ));
            };
            if remediation_task_id != task_id
                || action != "fix_defect"
                || !matches!(step_status.as_str(), "pending" | "in_progress")
            {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_STEP_MISMATCH",
                    "指定 remediation_step_id 不是当前任务可领取的 fix_defect 步骤",
                ));
            }
            let metadata = serde_json::from_str::<Value>(&result).unwrap_or(Value::Null);
            // provenance 必须与 required_remediation_step 选中的同一条完全一致，防止
            // 并发改写后领取到过期 provenance（原实现把该比较与源步骤存在性合并判断）。
            if metadata != required_metadata {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_STEP_MISMATCH",
                    "remediation provenance 与当前 remediation 不一致",
                ));
            }
            match metadata
                .get("remediation_of_step_id")
                .and_then(|item| item.as_str())
                .filter(|item| !item.trim().is_empty())
            {
                // step-level remediation：provenance 必须指向同 task 的真实源步骤。
                Some(linked_step_id) => {
                    let source_step_exists: i64 = tx
                        .query_row(
                            "SELECT COUNT(*) FROM task_steps WHERE id = ?1 AND task_id = ?2",
                            params![linked_step_id, task_id],
                            |r| r.get(0),
                        )
                        .map_err(|e| {
                            DaemonRpcError::internal_error(format!(
                                "校验 remediation provenance 失败: {}",
                                e
                            ))
                        })?;
                    if source_step_exists == 0 {
                        return Err(DaemonRpcError::new(
                            "E_REMEDIATION_STEP_MISMATCH",
                            "remediation provenance 未指向当前任务的源步骤",
                        ));
                    }
                }
                // task-level remediation：role-protocol §5 允许 reviewer_blocked 使用
                // null step_id，daemon 追加的 fix_defect 因此没有源步骤，provenance 由
                // verdict + handoff event 承载。缺少本分支时该 step 结构性不可领取，
                // 任务会永久停在 in_progress/claim_current_step 的活锁中。
                None => {
                    let unresolved = unresolved_failed_step_ids(&tx, task_id)?;
                    if !task_level_remediation_provenance_ok(&metadata, &unresolved) {
                        return Err(DaemonRpcError::new(
                            "E_REMEDIATION_STEP_MISMATCH",
                            "remediation 步骤缺少可校验 provenance：既无 remediation_of_step_id，\
                             也无 task-level verdict/handoff provenance",
                        ));
                    }
                }
            }
        }

        // 检查是否已被其他 agent claim。旧 owner 超过 stale 窗口后，允许新的
        // 同治理角色 agent 在本事务内接管；不要求跨角色 reviewer lease 或
        // adjudicator recovery，避免失联 Executor 把同角色工作流永久锁死。
        let mut claim_recovered = false;
        let mut previous_claim_session = String::new();
        let (_claimed_actor, claimed_session, claimed_role) =
            self.get_task_claim_details(&tx, task_id);
        if let Some(existing_session) = claimed_session {
            if current_status == "in_progress" && existing_session != agent_session_id {
                // P0-L R1：worker 路径接管角色来自 Role Worker 映射（identity 可选）；
                // legacy 路径仍必须携带 identity 证明接管角色。
                let takeover_role_owned: Option<String> = worker_claim_role
                    .clone()
                    .or_else(|| identity.as_ref().map(|id| id.role.clone()));
                let Some(new_role_raw) = takeover_role_owned.as_deref() else {
                    return Err(DaemonRpcError::new(
                        "task_conflict",
                        format!("Task {} 已被 agent {} 抢占", task_id, existing_session),
                    ));
                };

                // claim 事件从 v51 起记录 role；对更早事件回退到旧 owner 的
                // registration。无法证明旧角色时 fail-closed，绝不猜测接管。
                let owner_registration: Option<(String, String, String, f64)> = tx
                    .query_row(
                        "SELECT agent_id, status, role, last_heartbeat FROM agent_registrations
                         WHERE session_id = ?1 ORDER BY registered_at DESC LIMIT 1",
                        params![existing_session],
                        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
                    )
                    .optional()
                    .map_err(|e| {
                        DaemonRpcError::internal_error(format!("查询旧 claim owner 失败: {}", e))
                    })?;
                let old_role = claimed_role.as_deref().unwrap_or("").trim().to_string();
                let old_role = if old_role.is_empty() {
                    owner_registration
                        .as_ref()
                        .map(|(_, _, role, _)| role.trim().to_string())
                        .unwrap_or_default()
                } else {
                    old_role
                };
                let new_role = canonical_claim_role(new_role_raw);
                if old_role.is_empty() || canonical_claim_role(&old_role) != new_role {
                    return Err(DaemonRpcError::new(
                        "task_conflict",
                        format!(
                            "Task {} 的旧 claim 角色与当前 agent 不同或不可证明（old_role={}, new_role={}）",
                            task_id, old_role, new_role
                        ),
                    ));
                }

                // 自动接管只使用 daemon authoritative clock。没有时钟就保持
                // 原有冲突语义，避免客户端时间或本机时间误判 owner 已失联。
                let clock = self.clock.as_ref().ok_or_else(|| {
                    DaemonRpcError::new(
                        "E_CLAIM_RECOVERY_CLOCK_UNAVAILABLE",
                        format!(
                            "Task {} 无 daemon authoritative clock，拒绝 stale claim takeover",
                            task_id
                        ),
                    )
                })?;
                let now = clock.now_secs() as f64;
                let stale = match owner_registration.as_ref() {
                    None => true,
                    Some((_, status, _, heartbeat)) => {
                        status != "active" || now - *heartbeat > ORPHAN_CLAIM_STALE_SECS
                    }
                };
                if !stale {
                    return Err(DaemonRpcError::new(
                        "task_conflict",
                        format!(
                            "Task {} 已被 agent {} 抢占且 owner 仍 active",
                            task_id, existing_session
                        ),
                    ));
                }

                // 接管 provenance 统一合并 worker/legacy 路径的 replacement_info；
                // legacy 路径保持原字段语义，worker 路径额外携带 replacement_kind。
                let mut recovery = serde_json::json!({
                    "old_actor_identity": _claimed_actor,
                    "old_session_id": existing_session,
                    "old_role": old_role,
                    "stale_after_seconds": ORPHAN_CLAIM_STALE_SECS,
                    "recovery": "same_role_claim_takeover",
                });
                if let Some(info) = replacement_info.as_ref() {
                    if let (Some(dst), Some(src)) = (recovery.as_object_mut(), info.as_object()) {
                        for (k, v) in src {
                            dst.insert(k.clone(), v.clone());
                        }
                    }
                }
                let recovery_reason = recovery.to_string();
                let recovery_seq = self.next_seq();
                tx.execute(
                    "INSERT INTO task_events
                     (task_id, from_status, to_status, reason_code, reason, actor_identity,
                      agent_session_id, role, monotonic_seq, authoritative_timestamp)
                     VALUES (?1, ?2, ?2, 'claim_recovered', ?3, ?4, ?5, ?6, ?7, ?8)",
                    params![
                        task_id,
                        current_status,
                        recovery_reason,
                        owner_key,
                        agent_session_id,
                        new_role_raw,
                        recovery_seq,
                        now,
                    ],
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!(
                        "追加 stale claim takeover 事件失败: {}",
                        e
                    ))
                })?;
                claim_recovered = true;
                previous_claim_session = existing_session;
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
            )
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("task_claim 标记步骤失败: {}", e))
            })?
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
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_STEP_MISMATCH",
                    "remediation 步骤无法领取",
                ));
            }
        }

        let seq = self.next_seq();
        // P0-L R1：claim 事件角色归属优先取 worker 映射角色（legacy 仍用 identity.role）。
        let claim_role_owned: String = worker_claim_role
            .clone()
            .or_else(|| identity.as_ref().map(|id| id.role.clone()))
            .unwrap_or_default();
        let claim_role = claim_role_owned.as_str();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity, agent_session_id, role, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'in_progress', 'claimed', 'task claimed by agent', ?3, ?4, ?5, ?6, ?7)",
            params![task_id, current_status, owner_key, agent_session_id, claim_role, seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        let claimed_step_id: Option<String> = tx
            .query_row(
                "SELECT id FROM task_steps WHERE task_id = ?1 AND status = 'in_progress'
                 ORDER BY step_index ASC LIMIT 1",
                params![task_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 claimed assignment 步骤失败: {e}"))
            })?;
        let assignment_request_id = params
            .get("request_id")
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .unwrap_or("task.claim");
        let assignment_id = assignment_queue::claim_assignment(
            &tx,
            task_id,
            claimed_step_id.as_deref(),
            claim_role,
            &assignment_holder_agent_id,
            &agent_session_id,
            identity
                .as_ref()
                .map(|item| item.model_id.as_str())
                .unwrap_or(""),
            assignment_request_id,
            &owner_key,
            claim_recovered,
            self.next_seq(),
            ts,
        )?;

        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 task_claim 事务失败: {}", e))
        })?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert(
            "status".to_string(),
            Value::String("in_progress".to_string()),
        );
        res.insert("claimed_by".to_string(), Value::String(agent_session_id));
        res.insert("claim_recovered".to_string(), Value::Bool(claim_recovered));
        res.insert("assignment_id".to_string(), Value::String(assignment_id));
        if claim_recovered {
            res.insert(
                "previous_claim_session".to_string(),
                Value::String(previous_claim_session),
            );
        }

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

        if let Some((
            step_id,
            step_index,
            action,
            target_file,
            target_symbol,
            check_items,
            step_status,
            task_title,
        )) = step
        {
            res.insert("step_id".to_string(), Value::String(step_id));
            res.insert(
                "step_index".to_string(),
                Value::Number(serde_json::Number::from(step_index)),
            );
            res.insert("action".to_string(), Value::String(action));
            res.insert("target_file".to_string(), Value::String(target_file));
            res.insert("target_symbol".to_string(), Value::String(target_symbol));
            res.insert("check_items".to_string(), Value::String(check_items));
            res.insert("step_status".to_string(), Value::String(step_status));
            res.insert("task_title".to_string(), Value::String(task_title));
        }

        // A3 Task Envelope：携带当前角色的冻结 Role Contract（revision/hash 存证）。
        // P0-L R1：以 effective role 查询（worker 映射角色优先，legacy 用 identity.role）。
        let effective_role: Option<String> = worker_claim_role
            .clone()
            .or_else(|| identity.as_ref().map(|id| id.role.clone()));
        if let Some(role) = effective_role.as_deref() {
            if let Some(contract) = get_current_role_contract(&conn, task_id, role)? {
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
            return Err(DaemonRpcError::invalid_params(
                "orphan claim recovery 必须提供 reason",
            ));
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
        let clock = self
            .clock
            .as_ref()
            .ok_or_else(|| lease_clock_unavailable("task.claim.recover", task_id, "reviewer"))?;
        let now = clock.now_secs() as f64;

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        // 先验证受保护 reviewer lease；失败时零 task-domain 写入。
        // P0-G：跨角色恢复必须使用独立 Reviewer lease + Adjudicator identity
        // 校验（validate_reviewer_lease_for_adjudication），不能用同 holder 的
        // validate_lease_for_mutation——否则独立 Reviewer 的 lease 会与
        // Adjudicator identity 冲突，永远无法完成合法恢复。
        self.validate_reviewer_lease_for_adjudication(
            &tx,
            task_id,
            &lease_token,
            fencing_counter,
            &identity,
        )?;

        let registered: Option<(String, String, String)> = tx
            .query_row(
                "SELECT status, session_id, role FROM agent_registrations WHERE agent_id = ?1",
                params![identity.agent_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 recovery identity 失败: {}", e))
            })?;
        match registered {
            Some((status, session, role))
                if status == "active"
                    && session == identity.session_id
                    && role == identity.role => {}
            _ => {
                return Err(DaemonRpcError::new(
                    "E_IDENTITY_NOT_ACTIVE",
                    "adjudicator identity 未注册为 active，拒绝 orphan claim recovery",
                ));
            }
        }

        let current_status: String = tx
            .query_row(
                "SELECT status FROM tasks WHERE id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .map_err(|_| {
                DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id))
            })?;
        if current_status != "open" && current_status != "in_progress" {
            return Err(DaemonRpcError::new(
                "task_conflict",
                format!(
                    "Task {} 处于不可 recovery 状态 ({})",
                    task_id, current_status
                ),
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
                format!(
                    "旧 claim owner session={} 仍 active，拒绝 recovery",
                    old_session
                ),
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
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("追加 claim recovery 事件失败: {}", e))
        })?;
        let event_id = tx.last_insert_rowid();
        record_action_identity(&tx, task_id, &identity, "task.claim.recover", seq, now)?;

        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 claim recovery 事务失败: {}", e))
        })?;

        let mut result = Map::new();
        result.insert("task_id".to_string(), Value::String(task_id.to_string()));
        result.insert("status".to_string(), Value::String(current_status));
        result.insert(
            "claim_status".to_string(),
            Value::String("released".to_string()),
        );
        result.insert("old_actor_identity".to_string(), Value::String(old_actor));
        result.insert("old_session_id".to_string(), Value::String(old_session));
        result.insert(
            "recovery_event_id".to_string(),
            Value::Number(event_id.into()),
        );
        result.insert(
            "next_action".to_string(),
            Value::String("new Executor must call task.claim".to_string()),
        );
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
            .map_err(|_| {
                DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id))
            })?;

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

        let assignment =
            assignment_queue::current_assignment(&conn, task_id, next_step_id.as_deref(), None)?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String(status));
        res.insert("claimed_by".to_string(), Value::String(claimed_by));
        res.insert(
            "next_step_id".to_string(),
            next_step_id.map(Value::String).unwrap_or(Value::Null),
        );
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
        res.insert(
            "assignment".to_string(),
            assignment
                .map(|item| item.as_value())
                .unwrap_or(Value::Null),
        );
        Ok(Value::Object(res))
    }

    /// 为当前 durable assignment 写入 heartbeat。assignment 状态完全由 daemon
    /// 的 task_events 投影得到，客户端不能直接更新队列或伪造 holder。
    pub fn handle_task_assignment_heartbeat(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let assignment_id = params
            .get("assignment_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 assignment_id"))?;
        let identity = parse_action_identity(params)?;
        let holder_agent_id = identity
            .as_ref()
            .map(|value| value.agent_id.clone())
            .or_else(|| {
                params
                    .get("agent_id")
                    .and_then(Value::as_str)
                    .map(str::to_string)
            })
            .unwrap_or_else(|| peer.owner_key());
        let holder_session_id = params
            .get("agent_session_id")
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .map(str::to_string)
            .or_else(|| identity.as_ref().map(|value| value.session_id.clone()))
            .unwrap_or_else(|| peer.owner_key());
        let holder_model_id = identity
            .as_ref()
            .map(|value| value.model_id.clone())
            .or_else(|| {
                params
                    .get("model_id")
                    .and_then(Value::as_str)
                    .map(str::to_string)
            })
            .unwrap_or_default();
        let request_id = params
            .get("request_id")
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .unwrap_or("task.assignment.heartbeat");
        let ts = task_now_ts();
        let mut conn = self.conn.lock().unwrap();
        let tx = conn.unchecked_transaction().map_err(|e| {
            DaemonRpcError::internal_error(format!("开启 assignment heartbeat 事务失败: {e}"))
        })?;
        let assignment = assignment_queue::heartbeat_assignment(
            &tx,
            task_id,
            assignment_id,
            &holder_agent_id,
            &holder_session_id,
            &holder_model_id,
            &peer.owner_key(),
            request_id,
            self.next_seq(),
            ts,
        )?;
        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 assignment heartbeat 事务失败: {e}"))
        })?;
        let mut result = assignment.as_value();
        if let Some(object) = result.as_object_mut() {
            object.insert(
                "request_id".to_string(),
                Value::String(request_id.to_string()),
            );
            object.insert("replayed".to_string(), Value::Bool(false));
        }
        Ok(result)
    }

    /// 读取 assignment 的完整事件投影；这是只读路由，状态不在 Python/CLI
    /// 侧推导，便于 `task.next_action`、CLI 和 MCP 展示同一事实。
    pub fn handle_task_assignment_status(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let step_id = params.get("step_id").and_then(Value::as_str);
        let role = params.get("role").and_then(Value::as_str);
        let conn = self.conn.lock().unwrap();
        let assignments = assignment_queue::project_task_assignments(&conn, task_id)?;
        let filtered: Vec<Value> = assignments
            .iter()
            .filter(|item| {
                step_id.map_or(true, |expected| item.step_id.as_deref() == Some(expected))
            })
            .filter(|item| role.map_or(true, |expected| item.role == expected))
            .map(AssignmentProjection::as_value)
            .collect();
        // 与 task.next_action 共用 assignment domain 的 event-order 选择规则；
        // assignment_id 仅是稳定标识符，不能用字典序推断当前负责者。
        let current = assignment_queue::current_assignment(&conn, task_id, step_id, role)?;
        Ok(serde_json::json!({
            "task_id": task_id,
            "assignments": filtered,
            "current_assignment": current.map(|item| item.as_value()).unwrap_or(Value::Null),
        }))
    }

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
                workspace_id,
                event_id,
                lease_id,
                task_id,
                role,
                event_type,
                fencing_counter,
                event_at,
                actor.agent_id,
                actor.session_id,
                actor.model_id,
                detail
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("追加 lease 事件失败: {}", e)))?;
        Ok(())
    }

    /// 以 lease holder 身份构造事件 actor（对齐 Python renew/release 事件 actor 取 lease holder）。
    fn holder_identity(
        agent_id: &str,
        session_id: &str,
        model_id: &str,
        role: &str,
    ) -> ActionIdentity {
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
        if let Some(cached) = self.check_dedup(params) {
            return Ok(cached);
        }
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let role = params
            .get("role")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 role"))?;
        let ttl = params
            .get("ttl_seconds")
            .and_then(|v| v.as_f64())
            .unwrap_or(3600.0);
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
        let clock = self
            .clock
            .as_ref()
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
        let workspace_id =
            task_bound_workspace_id(&tx, task_id, optional_workspace_id_param(params))?;

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
        if let Some((
            active_id,
            active_lease_id,
            active_counter,
            active_expires,
            holder_agent,
            holder_session,
            holder_model,
        )) = active
        {
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
                    .map_err(|e| {
                        DaemonRpcError::internal_error(format!("查询 lease holder 注册失败: {}", e))
                    })?;
                let stale_reason = match holder_registration {
                    None => Some("holder_registration_missing"),
                    Some((status, last_heartbeat)) if status != "active" => {
                        Some("holder_registration_inactive")
                    }
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
                    .map_err(|e| {
                        DaemonRpcError::internal_error(format!("回收 stale lease 失败: {}", e))
                    })?;
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
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("过期 lease 置 expired 失败: {}", e))
                })?;
            }
        }

        // 2. 单调递增 fencing counter（Req 11.3）：该 task+role 全历史 MAX + 1
        let fencing_counter: i64 =
            tx.query_row(
                "SELECT COALESCE(MAX(fencing_counter), 0) FROM task_leases
                 WHERE workspace_id = ?1 AND task_id = ?2 AND role = ?3",
                params![workspace_id, task_id, role],
                |r| r.get::<_, i64>(0),
            )
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 fencing counter 失败: {}", e))
            })? + 1;

        // 3. 插入新 lease（唯一索引 idx_task_leases_active_unique 防双活；冲突 → E_LEASE_ALREADY_ACTIVE）
        tx.execute(
            "INSERT INTO task_leases
             (workspace_id, lease_id, task_id, role, agent_id, session_id, model_id,
              token_hash, fencing_counter, acquired_at, expires_at, status)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, 'active')",
            params![
                workspace_id,
                lease_id,
                task_id,
                role,
                id.agent_id,
                id.session_id,
                id.model_id,
                token_hash,
                fencing_counter,
                now,
                expires_at
            ],
        )
        .map_err(|e| {
            if is_unique_violation(&e) {
                DaemonRpcError::new(
                    "E_LEASE_ALREADY_ACTIVE",
                    format!(
                        "task={} role={} 已有 active lease（唯一索引防双活）",
                        task_id, role
                    ),
                )
            } else {
                DaemonRpcError::internal_error(format!("插入 task_leases 失败: {}", e))
            }
        })?;

        // 4. 追加审计事件（append-only，不写 raw token）
        self.append_lease_event(
            &tx,
            workspace_id,
            &lease_id,
            task_id,
            role,
            "acquire",
            fencing_counter,
            now,
            id,
            &format!("acquired, expires_at={:.1}", expires_at),
        )?;

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 acquire 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("lease_id".to_string(), Value::String(lease_id));
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("role".to_string(), Value::String(role.to_string()));
        res.insert("token".to_string(), Value::String(token)); // raw token 仅此一次返回（Req 11.2）
        res.insert(
            "fencing_counter".to_string(),
            Value::Number(serde_json::Number::from(fencing_counter)),
        );
        res.insert(
            "acquired_at".to_string(),
            Value::Number(serde_json::Number::from_f64(now).unwrap()),
        );
        res.insert(
            "expires_at".to_string(),
            Value::Number(serde_json::Number::from_f64(expires_at).unwrap()),
        );
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
        if let Some(cached) = self.check_dedup(params) {
            return Ok(cached);
        }
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
        let ttl = params
            .get("ttl_seconds")
            .and_then(|v| v.as_f64())
            .unwrap_or(3600.0);
        if ttl <= 0.0 {
            return Err(DaemonRpcError::invalid_params("ttl_seconds 必须大于 0"));
        }
        let identity = parse_action_identity(params)?;
        // 调用方可选携带 fencing_counter；提供时强制校验（Property 11，旧持有者续租被拒）
        let provided_counter = params.get("fencing_counter").and_then(|v| v.as_i64());

        let clock = self
            .clock
            .as_ref()
            .ok_or_else(|| lease_clock_unavailable("lease.extend", task_id, role))?;
        let now = clock.now_secs() as f64;

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;
        let workspace_id =
            task_bound_workspace_id(&tx, task_id, optional_workspace_id_param(params))?;

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
                        r.get(0)?,
                        r.get(1)?,
                        r.get(2)?,
                        r.get(3)?,
                        r.get(4)?,
                        r.get(5)?,
                        r.get(6)?,
                    ))
                },
            )
            .optional()
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 active lease 失败: {}", e))
            })?;
        let Some((
            lease_id,
            token_hash,
            active_counter,
            expires_at,
            holder_agent,
            holder_session,
            holder_model,
        )) = active
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
            if id.agent_id != holder_agent
                || id.session_id != holder_session
                || id.model_id != holder_model
            {
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
                format!(
                    "lease {} 已过期 (expires_at={:.1}, now={:.1})",
                    lease_id, expires_at, now
                ),
            ));
        }
        // fencing counter 校验（提供时，Property 11）
        if let Some(c) = provided_counter {
            if c != active_counter {
                return Err(DaemonRpcError::new(
                    "E_LEASE_FENCING_STALE",
                    format!(
                        "fencing counter {} != 当前 {}；旧持有者续租被拒绝",
                        c, active_counter
                    ),
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
            &tx,
            workspace_id,
            &lease_id,
            task_id,
            role,
            "renew",
            active_counter,
            now,
            &actor,
            &format!("renewed, expires_at={:.1}", new_expires),
        )?;

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 renew 事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("lease_id".to_string(), Value::String(lease_id));
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("role".to_string(), Value::String(role.to_string()));
        res.insert(
            "fencing_counter".to_string(),
            Value::Number(serde_json::Number::from(active_counter)),
        );
        res.insert(
            "renewed_at".to_string(),
            Value::Number(serde_json::Number::from_f64(now).unwrap()),
        );
        res.insert(
            "expires_at".to_string(),
            Value::Number(serde_json::Number::from_f64(new_expires).unwrap()),
        );
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
        if let Some(cached) = self.check_dedup(params) {
            return Ok(cached);
        }
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

        let clock = self
            .clock
            .as_ref()
            .ok_or_else(|| lease_clock_unavailable("lease.release", task_id, role))?;
        let now = clock.now_secs() as f64;

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;
        let workspace_id =
            task_bound_workspace_id(&tx, task_id, optional_workspace_id_param(params))?;

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
                        r.get(0)?,
                        r.get(1)?,
                        r.get(2)?,
                        r.get(3)?,
                        r.get(4)?,
                        r.get(5)?,
                        r.get(6)?,
                    ))
                },
            )
            .optional()
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 active lease 失败: {}", e))
            })?;
        if let Some((
            lease_pk,
            lease_id,
            token_hash,
            active_counter,
            holder_agent,
            holder_session,
            holder_model,
        )) = active
        {
            if sha256_hex(token.as_bytes()) != token_hash {
                return Err(DaemonRpcError::new(
                    "E_LEASE_TOKEN_MISMATCH",
                    format!("token hash 不匹配 (lease_id={})", lease_id),
                ));
            }
            // holder Identity 校验（提供时）
            if let Some(id) = identity.as_ref() {
                if id.agent_id != holder_agent
                    || id.session_id != holder_session
                    || id.model_id != holder_model
                {
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
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("release 置 released 失败: {}", e))
            })?;

            let actor = Self::holder_identity(&holder_agent, &holder_session, &holder_model, role);
            self.append_lease_event(
                &tx,
                workspace_id,
                &lease_id,
                task_id,
                role,
                "release",
                active_counter,
                now,
                &actor,
                &format!("released at {:.1}", now),
            )?;

            tx.commit().map_err(|e| {
                DaemonRpcError::internal_error(format!("提交 release 事务失败: {}", e))
            })?;

            let mut res = Map::new();
            res.insert("lease_id".to_string(), Value::String(lease_id));
            res.insert("task_id".to_string(), Value::String(task_id.to_string()));
            res.insert("role".to_string(), Value::String(role.to_string()));
            res.insert(
                "fencing_counter".to_string(),
                Value::Number(serde_json::Number::from(active_counter)),
            );
            res.insert(
                "released_at".to_string(),
                Value::Number(serde_json::Number::from_f64(now).unwrap()),
            );
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
                res.insert(
                    "fencing_counter".to_string(),
                    Value::Number(serde_json::Number::from(hist_counter)),
                );
                res.insert(
                    "released_at".to_string(),
                    Value::Number(serde_json::Number::from_f64(hist_released_at).unwrap()),
                );
                res.insert("status".to_string(), Value::String("released".to_string()));
                res.insert("idempotent".to_string(), Value::Bool(true));
                tx.commit().map_err(|e| {
                    DaemonRpcError::internal_error(format!("提交幂等 release 事务失败: {}", e))
                })?;
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
        if let Some(cached) = self.check_dedup(params) {
            return Ok(cached);
        }
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let role = params.get("role").and_then(|v| v.as_str()).unwrap_or("");

        let conn = self.conn.lock().unwrap();
        let workspace_id =
            task_bound_workspace_id(&conn, task_id, optional_workspace_id_param(params))?;

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
            Some((
                status,
                lease_id,
                row_task_id,
                row_role,
                agent_id,
                session_id,
                model_id,
                token_hash,
                fencing_counter,
                acquired_at,
                expires_at,
                renewed_at,
                released_at,
            )) => {
                let mut res = Map::new();
                res.insert("status".to_string(), Value::String(status));
                res.insert("lease_id".to_string(), Value::String(lease_id));
                res.insert("task_id".to_string(), Value::String(row_task_id));
                res.insert("role".to_string(), Value::String(row_role));
                res.insert("agent_id".to_string(), Value::String(agent_id));
                res.insert("session_id".to_string(), Value::String(session_id));
                res.insert("model_id".to_string(), Value::String(model_id));
                res.insert("token_hash".to_string(), Value::String(token_hash)); // raw token 永不返回（Req 11.2）
                res.insert(
                    "fencing_counter".to_string(),
                    Value::Number(serde_json::Number::from(fencing_counter)),
                );
                res.insert(
                    "acquired_at".to_string(),
                    Value::Number(serde_json::Number::from_f64(acquired_at).unwrap()),
                );
                res.insert(
                    "expires_at".to_string(),
                    Value::Number(serde_json::Number::from_f64(expires_at).unwrap()),
                );
                if let Some(v) = renewed_at {
                    res.insert(
                        "renewed_at".to_string(),
                        Value::Number(serde_json::Number::from_f64(v).unwrap()),
                    );
                }
                if let Some(v) = released_at {
                    res.insert(
                        "released_at".to_string(),
                        Value::Number(serde_json::Number::from_f64(v).unwrap()),
                    );
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

        let mut stmt = conn.prepare(&sql).map_err(|e| {
            DaemonRpcError::internal_error(format!("prepare lease 事件查询失败: {}", e))
        })?;
        let arg_refs: Vec<&dyn rusqlite::ToSql> = args
            .iter()
            .map(|b| b.as_ref() as &dyn rusqlite::ToSql)
            .collect();
        let rows = stmt
            .query_map(rusqlite::params_from_iter(arg_refs.iter().copied()), |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, i64>(5)?,
                    r.get::<_, f64>(6)?,
                    r.get::<_, String>(7)?,
                    r.get::<_, String>(8)?,
                    r.get::<_, String>(9)?,
                    r.get::<_, String>(10)?,
                ))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 lease 事件失败: {}", e)))?;

        let mut events: Vec<Value> = Vec::new();
        for row in rows {
            let (
                event_id,
                lease_id,
                row_task_id,
                row_role,
                event_type,
                fencing_counter,
                event_at,
                actor_agent_id,
                actor_session_id,
                actor_model_id,
                detail,
            ) = row.map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 lease 事件失败: {}", e))
            })?;
            let mut m = Map::new();
            m.insert("event_id".to_string(), Value::String(event_id));
            m.insert("lease_id".to_string(), Value::String(lease_id));
            m.insert("task_id".to_string(), Value::String(row_task_id));
            m.insert("role".to_string(), Value::String(row_role));
            m.insert("event_type".to_string(), Value::String(event_type));
            m.insert(
                "fencing_counter".to_string(),
                Value::Number(serde_json::Number::from(fencing_counter)),
            );
            m.insert(
                "event_at".to_string(),
                Value::Number(serde_json::Number::from_f64(event_at).unwrap()),
            );
            m.insert("actor_agent_id".to_string(), Value::String(actor_agent_id));
            m.insert(
                "actor_session_id".to_string(),
                Value::String(actor_session_id),
            );
            m.insert("actor_model_id".to_string(), Value::String(actor_model_id));
            m.insert("detail".to_string(), Value::String(detail));
            events.push(Value::Object(m));
        }
        Ok(Value::Array(events))
    }
}
