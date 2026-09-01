//! 任务生命周期域：报告、整改、步骤解析和结构化交接。
//!
//! 方法实现保持原有事务、权限和错误语义；本模块只承担 task lifecycle mutation。

use super::*;

impl TaskCollabStore {
    pub fn handle_task_report(
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
        // review_input_snapshot 的唯一权威来源是 task_events.snapshot_id。
        // report 只接受调用方提供的真实 snapshot reference；缺省时保持
        // no_snapshot，禁止 daemon 猜测或生成 snapshot。
        let snapshot_id = params
            .get("snapshot_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        let step_id = params.get("step_id").and_then(|v| v.as_str()).unwrap_or("");
        let success = params
            .get("success")
            .and_then(|v| v.as_bool())
            .unwrap_or(true);
        // route_task_write 会在客户端生成稳定 request_id；直接 daemon 调用的
        // 旧客户端若未提供，则在本事务内生成可追溯的 daemon report id，并在
        // response/event 中返回，禁止 handoff 再引用聊天里的猜测值。
        let requested_report_request_id = params
            .get("request_id")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_string);
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
                // 合同绑定使用治理层角色；planner/implementer/tester/evidence
                // 是 Executor 的 legacy runtime mode，不能因名称不同拒绝合法 report。
                let contract_role = canonical_claim_role(&id.role);
                if get_current_role_contract(&conn, task_id, contract_role)?.is_none() {
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

        let tx = begin_immediate_with_retry(&conn, "report")?;

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
            .query_row(
                "SELECT status FROM tasks WHERE id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .map_err(|_| {
                DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id))
            })?;

        // task-owned attribution：report 只能把显式白名单步骤目标写入
        // change_audit。禁止把共享工作树中未声明的 dirty/untracked 文件
        // 静默吸入任务证据；路径必须是相对路径且精确匹配 target_file 的
        // 逗号或分号分隔白名单（兼容既有 task step 投影格式）。所有记录与
        // 步骤状态在同一事务提交。
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
                .map_err(|_| {
                    DaemonRpcError::new(
                        "task_step_not_found",
                        format!("步骤不存在或不属于任务: {}", step_id),
                    )
                })?;
            let allowed: Vec<String> = target_file
                .split([',', ';'])
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
                let change_id = format!(
                    "CA-{}",
                    &sha256_hex(format!("{}:{}:{}", task_id, step_id, file_path).as_bytes())[..24]
                );
                let hash_before = obj
                    .get("hash_before")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let hash_after = obj.get("hash_after").and_then(|v| v.as_str()).unwrap_or("");
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
        let mut remediation_step_id_created: Option<String> = None;
        if !step_id.is_empty() {
            let actual_task_id: String = tx
                .query_row(
                    "SELECT task_id FROM task_steps WHERE id = ?1",
                    params![step_id],
                    |r| r.get(0),
                )
                .map_err(|_| {
                    DaemonRpcError::new("task_step_not_found", format!("步骤不存在: {}", step_id))
                })?;
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
                        obj.insert(
                            "resolution_summary".into(),
                            Value::String(summary.to_string()),
                        );
                        metadata.to_string()
                    } else {
                        summary.to_string()
                    }
                } else {
                    summary.to_string()
                }
            } else {
                summary.to_string()
            };
            let step_updated = tx.execute(
                "UPDATE task_steps SET status = ?1, result = ?2, completed_at = ?3 WHERE id = ?4",
                params![step_status, stored_result, ts, step_id],
            ).map_err(|e| DaemonRpcError::internal_error(format!("task_step 更新失败: {}", e)))?;
            if step_updated == 0 {
                return Err(DaemonRpcError::new(
                    "task_step_not_found",
                    format!("步骤不存在: {}", step_id),
                ));
            }
            // 漂移 #1 修复闭环：step 完成时把 `task_assignments` 补偿行从 `active`
            // 收敛为 `completed`，避免物理表残留孤儿 active 行（与事件投影的
            // `completed` 冲突，反而制造新的「误判有 active assignment」漂移）。
            // 仅在 success 时收敛；失败路径由 remediation 重新 claim 时再回写。
            if success {
                let report_role = identity.as_ref().map(|id| id.role.clone()).unwrap_or_default();
                let report_holder = identity
                    .as_ref()
                    .map(|id| id.agent_id.clone())
                    .unwrap_or_else(|| owner_key.clone());
                let report_session = identity
                    .as_ref()
                    .map(|id| id.session_id.clone())
                    .unwrap_or_default();
                let report_model = identity
                    .as_ref()
                    .map(|id| id.model_id.clone())
                    .unwrap_or_default();
                // 物理表可能尚不存在（极旧库）或该行尚未被 claim 补偿写建立；
                // `persist_claimed_assignment` 内部用规范 (task,step,role) id 做
                // INSERT OR REPLACE，保证与 claim 写命中同一行并收敛为 completed。
                let report_workspace_id = task_bound_workspace_id(&tx, &actual_task_id, None)?;
                assignment_queue::persist_claimed_assignment(
                    &tx,
                    report_workspace_id,
                    &actual_task_id,
                    Some(step_id),
                    &report_role,
                    &report_holder,
                    &report_session,
                    &report_model,
                    "completed",
                    ts,
                )?;
            }
            if !success {
                let max_idx: i64 = tx
                    .query_row(
                        "SELECT COALESCE(MAX(step_index), -1) FROM task_steps WHERE task_id = ?1",
                        params![actual_task_id],
                        |r| r.get(0),
                    )
                    .map_err(|e| {
                        DaemonRpcError::internal_error(format!("查询步骤序号失败: {}", e))
                    })?;
                let remediation_id = generate_task_id();
                let remediation_metadata = serde_json::json!({
                    "remediation_of_step_id": step_id,
                    "failed_target_file": failed_scope.0,
                    "failed_target_symbol": failed_scope.1,
                    "failed_check_items": failed_scope.2,
                })
                .to_string();
                tx.execute(
                    "INSERT INTO task_steps (id, task_id, step_index, action, target_file, target_symbol, check_items, status, result, created_at, completed_at)
                     VALUES (?1, ?2, ?3, 'fix_defect', ?4, ?5, ?6, 'pending', ?7, ?8, NULL)",
                    params![remediation_id, actual_task_id, max_idx + 1,
                            failed_scope.0, failed_scope.1, failed_scope.2,
                            remediation_metadata, ts],
                ).map_err(|e| DaemonRpcError::internal_error(format!("插入 fix_defect 步骤失败: {}", e)))?;
                // remediation step 必须与普通 step 一样具备唯一的 Executor Role Contract
                // binding。绑定失败时让整个 report 事务回滚，禁止留下不可领取的半状态步骤。
                bind_step_to_executor_role_contract(
                    &tx,
                    &actual_task_id,
                    &remediation_id,
                    &peer.owner_key(),
                )?;
                remediation_step_id_created = Some(remediation_id.clone());
                next_status = "in_progress".to_string();
            }
        }

        // review 是 projection，不是“没有 pending 就猜测完成”。历史 failed 行保持
        // 不变，仅当它们都有 append-only step_resolved 事件，且没有 pending/
        // in_progress 步骤时，任务才可进入 review。无 step_id 的 legacy report 也
        // 必须经过同一检查，避免绕过 remediation。
        let remaining: i64 = tx
            .query_row(
                "SELECT COUNT(*) FROM task_steps
             WHERE task_id = ?1 AND status IN ('pending', 'in_progress')",
                params![task_id],
                |r| r.get(0),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("查询剩余步骤失败: {}", e)))?;
        let unresolved_failed = unresolved_failed_step_ids(&tx, task_id)?.len() as i64;
        if remaining > 0 || unresolved_failed > 0 {
            next_status = "in_progress".to_string();
        }

        let updated = tx
            .execute(
                "UPDATE tasks SET status = ?1, updated_at = ?2 WHERE id = ?3",
                params![next_status, ts, task_id],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("task_report 失败: {}", e)))?;

        if updated == 0 {
            return Err(DaemonRpcError::new(
                "task_not_found",
                format!("任务不存在: {}", task_id),
            ));
        }

        let seq = self.next_seq();
        let report_request_id = requested_report_request_id
            .unwrap_or_else(|| format!("daemon-report-{}-{}", task_id, seq));
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity,
              agent_session_id, role, monotonic_seq, authoritative_timestamp,
              request_id, step_id, evidence_path, evidence_hash, snapshot_id)
             VALUES (?1, ?2, ?3, 'reported', ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)",
            params![
                task_id,
                current_status,
                next_status,
                summary,
                owner_key,
                agent_session_id,
                identity.as_ref().map(|id| id.role.as_str()).unwrap_or(""),
                seq,
                ts,
                report_request_id,
                step_id,
                evidence_path,
                evidence_hash,
                snapshot_id
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        let source_role = identity.as_ref().map(|item| item.role.as_str());
        let completed_assignment_ids = assignment_queue::complete_assignments(
            &tx,
            task_id,
            (!step_id.is_empty()).then_some(step_id),
            source_role,
            &owner_key,
            &agent_session_id,
            self.next_seq(),
            ts,
        )?;
        let next_assignment_id = if let Some(remediation_id) = remediation_step_id_created.as_deref()
        {
            assignment_queue::queue_assignment(
                &tx,
                task_id,
                Some(remediation_id),
                "executor",
                &report_request_id,
                None,
                &owner_key,
                &agent_session_id,
                self.next_seq(),
                ts,
            )?
        } else {
            assignment_queue::queue_assignment(
                &tx,
                task_id,
                (!step_id.is_empty()).then_some(step_id),
                "reviewer",
                &report_request_id,
                None,
                &owner_key,
                &agent_session_id,
                self.next_seq(),
                ts,
            )?
        };

        if let Some(ref id) = identity {
            record_action_identity(&tx, task_id, id, "state_transition", seq, ts)?;
        }

        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 task_report 事务失败: {}", e))
        })?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String(next_status));
        res.insert("request_id".to_string(), Value::String(report_request_id));
        if !snapshot_id.is_empty() {
            res.insert(
                "snapshot_id".to_string(),
                Value::String(snapshot_id.to_string()),
            );
        }
        if !step_id.is_empty() {
            res.insert("step_id".to_string(), Value::String(step_id.to_string()));
        }
        res.insert("change_ids".to_string(), Value::Array(change_ids));
        res.insert(
            "completed_assignment_ids".to_string(),
            Value::Array(completed_assignment_ids.into_iter().map(Value::String).collect()),
        );
        res.insert(
            "assignment_id".to_string(),
            next_assignment_id.map(Value::String).unwrap_or(Value::Null),
        );
        res.insert(
            "remediation_step_id".to_string(),
            remediation_step_id_created.map(Value::String).unwrap_or(Value::Null),
        );
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
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.trim().is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let source_step_id = params
            .get("source_step_id")
            .or_else(|| params.get("failed_step_id"))
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 source_step_id/failed_step_id"))?;
        let source_outcome = params
            .get("source_outcome")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .unwrap_or("failed_step");
        if !matches!(
            source_outcome,
            "failed_step" | "reviewer_blocked" | "adjudicator_returned"
        ) {
            return Err(DaemonRpcError::new(
                "E_REMEDIATION_SOURCE_OUTCOME_INVALID",
                "source_outcome 必须是 failed_step/reviewer_blocked/adjudicator_returned",
            ));
        }
        let requested_verdict_id = params
            .get("source_verdict_id")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .unwrap_or("");
        let requested_findings = params
            .get("source_findings")
            .cloned()
            .unwrap_or(Value::Null);
        let request_id = params
            .get("request_id")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 request_id"))?;
        let identity = parse_action_identity(params)?;
        let (token, counter) = Self::require_lease_params(params)?;
        let request_fingerprint = sha256_hex(
            serde_json::json!({
                "task_id": task_id,
                "source_step_id": source_step_id,
                "source_outcome": source_outcome,
                "source_verdict_id": requested_verdict_id,
                "source_findings": requested_findings,
            })
            .to_string()
            .as_bytes(),
        );
        let mut conn = self.conn.lock().unwrap();
        let tx = conn.unchecked_transaction().map_err(|e| {
            DaemonRpcError::internal_error(format!("开启 remediation 事务失败: {}", e))
        })?;
        self.validate_lease_for_mutation(
            &tx,
            task_id,
            "implementer",
            &token,
            counter,
            identity.as_ref(),
        )?;
        let current_status: String = tx
            .query_row(
                "SELECT status FROM tasks WHERE id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .map_err(|_| {
                DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id))
            })?;

        // durable request-id replay/mismatch，避免 daemon 重启后重复创建步骤。
        let mut event_stmt = tx
            .prepare(
                "SELECT event_id, reason FROM task_events
             WHERE task_id = ?1 AND reason_code = 'remediation_created'
             ORDER BY event_id DESC",
            )
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 remediation ledger 失败: {}", e))
            })?;
        let event_rows = event_stmt
            .query_map(params![task_id], |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 remediation ledger 失败: {}", e))
            })?;
        for row in event_rows {
            let (event_id, reason) = row.map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 remediation event 失败: {}", e))
            })?;
            if let Ok(existing) = serde_json::from_str::<Value>(&reason) {
                if existing.get("request_id").and_then(|v| v.as_str()) == Some(request_id) {
                    let same = existing.get("request_fingerprint").and_then(|v| v.as_str())
                        == Some(request_fingerprint.as_str());
                    if !same {
                        return Err(DaemonRpcError::new(
                            "E_REQUEST_ID_REUSE_MISMATCH",
                            "remediation request_id 参数冲突",
                        ));
                    }
                    let remediation_step_id = existing
                        .get("remediation_step_id")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    let mut replay = Map::new();
                    replay.insert("task_id".into(), Value::String(task_id.to_string()));
                    replay.insert(
                        "source_step_id".into(),
                        Value::String(source_step_id.to_string()),
                    );
                    replay.insert(
                        "source_outcome".into(),
                        Value::String(source_outcome.to_string()),
                    );
                    replay.insert(
                        "source_verdict_id".into(),
                        Value::String(requested_verdict_id.to_string()),
                    );
                    if source_outcome == "failed_step" {
                        replay.insert(
                            "failed_step_id".into(),
                            Value::String(source_step_id.to_string()),
                        );
                    }
                    replay.insert(
                        "remediation_step_id".into(),
                        Value::String(remediation_step_id.to_string()),
                    );
                    replay.insert("request_id".into(), Value::String(request_id.to_string()));
                    replay.insert(
                        "remediation_event_id".into(),
                        Value::Number(serde_json::Number::from(event_id)),
                    );
                    replay.insert("replayed".into(), Value::Bool(true));
                    return Ok(Value::Object(replay));
                }
            }
        }
        drop(event_stmt);

        let source_scope: (String, String, String, String) = tx
            .query_row(
                "SELECT status, target_file, target_symbol, check_items
             FROM task_steps WHERE id = ?1 AND task_id = ?2",
                params![source_step_id, task_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .map_err(|_| {
                DaemonRpcError::new(
                    "E_REMEDIATION_SOURCE_STEP_INVALID",
                    "source step 不属于任务",
                )
            })?;

        let (source_verdict_id, source_findings) = if source_outcome == "failed_step" {
            if source_scope.0 != "failed" {
                return Err(DaemonRpcError::new(
                    "E_FAILED_STEP_NOT_UNRESOLVED",
                    "failed 步骤已不是未解析 failed 状态",
                ));
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
            let expected_overall = if source_outcome == "reviewer_blocked" {
                "block"
            } else {
                "pass"
            };
            let verdict_findings_raw: String = tx
                .query_row(
                    "SELECT findings FROM task_verdict_events
                 WHERE task_id = ?1 AND verdict_id = ?2 AND overall = ?3",
                    params![task_id, requested_verdict_id, expected_overall],
                    |r| r.get(0),
                )
                .map_err(|_| {
                    DaemonRpcError::new(
                        "E_REMEDIATION_VERDICT_REQUIRED",
                        "source_verdict_id 不属于当前 task/outcome",
                    )
                })?;
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
                || supplied_findings
                    .as_array()
                    .map(|items| items.is_empty())
                    .unwrap_or(true)
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
        let mut existing_stmt = tx
            .prepare(
                "SELECT id, status, result FROM task_steps
             WHERE task_id = ?1 AND action = 'fix_defect' ORDER BY step_index ASC",
            )
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询现有 remediation 步骤失败: {}", e))
            })?;
        let existing_rows = existing_stmt
            .query_map(params![task_id], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                ))
            })
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("读取现有 remediation 步骤失败: {}", e))
            })?;
        for row in existing_rows {
            let (step_id, status, result) = row.map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 remediation 步骤失败: {}", e))
            })?;
            let linked = serde_json::from_str::<Value>(&result).ok().and_then(|v| {
                v.get("remediation_of_step_id")
                    .and_then(|x| x.as_str())
                    .map(str::to_string)
            });
            let existing_metadata = serde_json::from_str::<Value>(&result).unwrap_or(Value::Null);
            let same_source = if source_outcome == "failed_step" {
                linked.as_deref() == Some(source_step_id)
                    && existing_metadata
                        .get("source_outcome")
                        .and_then(Value::as_str)
                        .unwrap_or("failed_step")
                        == "failed_step"
            } else {
                existing_metadata
                    .get("source_verdict_id")
                    .and_then(Value::as_str)
                    == Some(source_verdict_id.as_str())
            };
            if same_source {
                if !matches!(status.as_str(), "pending" | "in_progress" | "done") {
                    return Err(DaemonRpcError::new(
                        "E_REMEDIATION_STEP_MISMATCH",
                        "已有 remediation 步骤状态不可恢复",
                    ));
                }
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_ALREADY_EXISTS",
                    "该 source 已有带 provenance 的 remediation 步骤",
                ));
            }
        }
        drop(existing_stmt);

        let max_idx: i64 = tx
            .query_row(
                "SELECT COALESCE(MAX(step_index), -1) FROM task_steps WHERE task_id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("查询步骤序号失败: {}", e)))?;
        let remediation_step_id = format!(
            "S-{}",
            &sha256_hex(
                format!(
                    "{}:{}:{}:{}:{}",
                    task_id, source_step_id, source_outcome, source_verdict_id, request_id
                )
                .as_bytes()
            )[..24]
        );
        let metadata = serde_json::json!({
            "remediation_of_step_id": source_step_id,
            "source_outcome": source_outcome,
            "source_verdict_id": source_verdict_id,
            "source_findings": source_findings,
            "source_target_file": source_scope.1,
            "source_target_symbol": source_scope.2,
            "source_check_items": source_scope.3,
            "request_id": request_id,
        })
        .to_string();
        let ts = task_now_ts();
        tx.execute(
            "INSERT INTO task_steps
             (id, task_id, step_index, action, target_file, target_symbol, check_items, status, result, created_at, completed_at)
             VALUES (?1, ?2, ?3, 'fix_defect', ?4, ?5, ?6, 'pending', ?7, ?8, NULL)",
            params![remediation_step_id, task_id, max_idx + 1, source_scope.1, source_scope.2, source_scope.3, metadata, ts],
        ).map_err(|e| DaemonRpcError::internal_error(format!("写入 remediation 步骤失败: {}", e)))?;
        // 显式 remediation 与失败 report 共用同一个 binding 写入口，避免两条路径
        // 产生“有 step、无 Role Contract binding”的治理孤儿。
        bind_step_to_executor_role_contract(
            &tx,
            task_id,
            &remediation_step_id,
            &peer.owner_key(),
        )?;
        let reason = serde_json::json!({
            "request_id": request_id,
            "request_fingerprint": request_fingerprint,
            "source_step_id": source_step_id,
            "source_outcome": source_outcome,
            "source_verdict_id": source_verdict_id,
            "source_findings": source_findings,
            "remediation_step_id": remediation_step_id,
        })
        .to_string();
        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity, agent_session_id, role, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'in_progress', 'remediation_created', ?3, ?4, ?5, ?6, ?7, ?8)",
            params![task_id, current_status, reason, peer.owner_key(), identity.as_ref().map(|i| i.session_id.as_str()).unwrap_or(""), identity.as_ref().map(|i| i.role.as_str()).unwrap_or(""), seq, ts],
        ).map_err(|e| DaemonRpcError::internal_error(format!("写入 remediation event 失败: {}", e)))?;
        tx.execute(
            "UPDATE tasks SET status = 'in_progress', updated_at = ?1 WHERE id = ?2",
            params![ts, task_id],
        )
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("更新 remediation task 状态失败: {}", e))
        })?;
        if let Some(id) = &identity {
            record_action_identity(&tx, task_id, id, "task.remediation.create", seq, ts)?;
        }
        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 remediation 事务失败: {}", e))
        })?;

        let mut out = Map::new();
        out.insert("task_id".into(), Value::String(task_id.to_string()));
        out.insert(
            "source_step_id".into(),
            Value::String(source_step_id.to_string()),
        );
        out.insert(
            "source_outcome".into(),
            Value::String(source_outcome.to_string()),
        );
        out.insert("source_verdict_id".into(), Value::String(source_verdict_id));
        if source_outcome == "failed_step" {
            out.insert(
                "failed_step_id".into(),
                Value::String(source_step_id.to_string()),
            );
        }
        out.insert(
            "remediation_step_id".into(),
            Value::String(remediation_step_id),
        );
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
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let failed_step_id = params
            .get("failed_step_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 failed_step_id"))?;
        let remediation_step_id = params
            .get("remediation_step_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 remediation_step_id"))?;
        let request_id = params
            .get("request_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        if request_id.is_empty() {
            return Err(DaemonRpcError::invalid_params("缺少 request_id"));
        }
        let evidence_path = params
            .get("evidence_path")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        let evidence_hash = params
            .get("evidence_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        if evidence_path.is_empty() || evidence_hash.is_empty() {
            return Err(DaemonRpcError::new(
                "E_RESOLUTION_EVIDENCE_REQUIRED",
                "resolution 必须携带 evidence_path/evidence_hash",
            ));
        }
        let identity = parse_action_identity(params)?;
        let (token, counter) = Self::require_lease_params(params)?;
        let mut conn = self.conn.lock().unwrap();
        let tx = conn.unchecked_transaction().map_err(|e| {
            DaemonRpcError::internal_error(format!("开启 resolution 事务失败: {}", e))
        })?;
        self.validate_lease_for_mutation(
            &tx,
            task_id,
            "implementer",
            &token,
            counter,
            identity.as_ref(),
        )?;

        // 先查询同 request_id 的历史 resolution；匹配则稳定重放，参数不同则冲突。
        let mut stmt = tx.prepare(
            "SELECT event_id, reason FROM task_events WHERE task_id = ?1 AND reason_code = 'step_resolved' ORDER BY event_id DESC"
        ).map_err(|e| DaemonRpcError::internal_error(format!("查询 resolution ledger 失败: {}", e)))?;
        let rows = stmt
            .query_map(params![task_id], |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 resolution ledger 失败: {}", e))
            })?;
        for row in rows {
            let (event_id, reason) = row.map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 resolution event 失败: {}", e))
            })?;
            if let Ok(existing) = serde_json::from_str::<Value>(&reason) {
                if existing.get("request_id").and_then(|v| v.as_str()) == Some(request_id) {
                    let same = existing.get("failed_step_id").and_then(|v| v.as_str())
                        == Some(failed_step_id)
                        && existing.get("remediation_step_id").and_then(|v| v.as_str())
                            == Some(remediation_step_id)
                        && existing.get("evidence_path").and_then(|v| v.as_str())
                            == Some(evidence_path)
                        && existing.get("evidence_hash").and_then(|v| v.as_str())
                            == Some(evidence_hash);
                    if !same {
                        return Err(DaemonRpcError::new(
                            "E_REQUEST_ID_REUSE_MISMATCH",
                            "resolution request_id 参数冲突",
                        ));
                    }
                    let mut replay = Map::new();
                    replay.insert("task_id".into(), Value::String(task_id.to_string()));
                    replay.insert(
                        "resolution_event_id".into(),
                        Value::Number(serde_json::Number::from(event_id)),
                    );
                    replay.insert("request_id".into(), Value::String(request_id.to_string()));
                    replay.insert("replayed".into(), Value::Bool(true));
                    let val = Value::Object(replay);
                    self.save_dedup(params, &val);
                    return Ok(val);
                }
            }
        }
        drop(stmt);

        let failed_status: String = tx
            .query_row(
                "SELECT status FROM task_steps WHERE id = ?1 AND task_id = ?2",
                params![failed_step_id, task_id],
                |r| r.get(0),
            )
            .map_err(|_| {
                DaemonRpcError::new("E_FAILED_STEP_NOT_FOUND", "failed_step_id 不属于任务")
            })?;
        if failed_status != "failed" {
            return Err(DaemonRpcError::new(
                "E_FAILED_STEP_NOT_UNRESOLVED",
                "failed 步骤已不是未解析 failed 状态",
            ));
        }
        let remediation_result: String = tx.query_row(
            "SELECT result FROM task_steps WHERE id = ?1 AND task_id = ?2 AND action = 'fix_defect' AND status = 'done'",
            params![remediation_step_id, task_id], |r| r.get(0)
        ).map_err(|_| DaemonRpcError::new("E_REMEDIATION_NOT_DONE", "remediation 步骤不存在或尚未 done"))?;
        let linked_failed = serde_json::from_str::<Value>(&remediation_result)
            .ok()
            .and_then(|v| {
                v.get("remediation_of_step_id")
                    .and_then(|x| x.as_str())
                    .map(str::to_string)
            });
        if linked_failed.as_deref() != Some(failed_step_id) {
            return Err(DaemonRpcError::new(
                "E_REMEDIATION_STEP_MISMATCH",
                "remediation provenance 与 failed_step_id 不一致",
            ));
        }

        let ts = task_now_ts();
        let reason = serde_json::json!({
            "request_id": request_id,
            "failed_step_id": failed_step_id,
            "remediation_step_id": remediation_step_id,
            "evidence_path": evidence_path,
            "evidence_hash": evidence_hash,
        })
        .to_string();
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
        let next_status = if pending == 0 && unresolved == 0 {
            "review"
        } else {
            "in_progress"
        };
        tx.execute(
            "UPDATE tasks SET status = ?1, updated_at = ?2 WHERE id = ?3",
            params![next_status, ts, task_id],
        )
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("更新 resolution 后任务状态失败: {}", e))
        })?;
        if let Some(id) = &identity {
            record_action_identity(&tx, task_id, id, "task.step.resolve", seq, ts)?;
        }
        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 resolution 事务失败: {}", e))
        })?;
        let mut out = Map::new();
        out.insert("task_id".into(), Value::String(task_id.to_string()));
        out.insert(
            "resolution_event_id".into(),
            Value::Number(serde_json::Number::from(resolution_event_id)),
        );
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
                .ok_or_else(|| {
                    DaemonRpcError::new(
                        "E_HANDOFF_STRUCTURED_REQUIRED",
                        format!("task.handoff 缺少结构化字段 {}", name),
                    )
                })
        };
        let task_id = text_field("task_id")?;
        let from_role = text_field("from_role")?;
        let outcome = text_field("outcome")?;
        let next_role = text_field("next_role")?;
        let next_action = text_field("next_action")?;
        let reason = text_field("reason")?;
        let independence_requirement = text_field("independence_requirement")?;
        let request_id = text_field("request_id")?;
        // task-level Reviewer BLOCKED 没有具体源步骤，必须用显式 JSON null
        // 表示，而不是让 CLI/Reviewer 伪造一个 step id。其它 outcome 仍要求
        // 非空步骤；这样既保留 step provenance，又能让 review 状态的空步骤任务
        // 进入 daemon 原子 remediation 路由。
        let step_id = match params.get("step_id") {
            Some(Value::String(value)) if !value.trim().is_empty() => {
                Some(value.trim().to_string())
            }
            Some(Value::Null) => None,
            _ => {
                return Err(DaemonRpcError::new(
                    "E_HANDOFF_STRUCTURED_REQUIRED",
                    "task.handoff 缺少结构化字段 step_id（task-level reviewer_blocked 可为 null）",
                ));
            }
        };
        let report_request_id = text_field("report_request_id")?;
        let evidence_path = text_field("evidence_path")?;
        let evidence_hash = text_field("evidence_hash")?;
        let (lease_token, fencing_counter) = Self::require_lease_params(params)?;
        let identity = parse_action_identity(params)?.ok_or_else(|| {
            DaemonRpcError::new(
                "E_IDENTITY_REQUIRED",
                "结构化 task.handoff 必须携带 identity",
            )
        })?;

        let expected_route = match outcome.as_str() {
            "executor_ready_for_review" => ("executor", "reviewer", "required"),
            "executor_blocked_to_user" => ("executor", "user", "not_applicable"),
            "reviewer_pass" => ("reviewer", "adjudicator", "required"),
            "reviewer_blocked" => ("reviewer", "executor", "not_required"),
            "adjudicator_accepted" => ("adjudicator", "complete", "not_applicable"),
            "adjudicator_returned" => ("adjudicator", "executor", "not_required"),
            _ => {
                return Err(DaemonRpcError::new(
                    "E_HANDOFF_OUTCOME_INVALID",
                    "未知 handoff outcome",
                ))
            }
        };
        if step_id.is_none() && outcome != "reviewer_blocked" {
            return Err(DaemonRpcError::new(
                "E_HANDOFF_STEP_REQUIRED",
                "只有 reviewer_blocked 的 task-level handoff 可以使用 step_id=null",
            ));
        }
        if from_role != expected_route.0
            || next_role != expected_route.1
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
        let tx = begin_immediate_with_retry(&conn, "handoff")?;
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
            .map_err(|_| {
                DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id))
            })?;
        let (claimed_actor, _) = self.get_task_claim_info(&tx, &task_id);
        if creator != owner_key
            && claimed_actor.as_deref() != Some(&owner_key)
            && owner_key != "root"
        {
            return Err(DaemonRpcError::permission_denied(format!(
                "没有对任务 {} 执行 handoff 的权限",
                task_id
            )));
        }

        // Executor→Reviewer 必须引用 daemon 已持久化的真实 report，而不是聊天中
        // 猜测的 report_request_id。step/evidence 三元组同时匹配，避免把另一轮或
        // 另一份 manifest 绑定到本次 handoff；历史库没有该列时由 storage compat
        // migration 追加空值，因此会明确返回可复现的缺失错误。
        if outcome == "executor_ready_for_review" {
            let report: Option<(String, String, String)> = tx
                .query_row(
                    "SELECT step_id, evidence_path, evidence_hash
                     FROM task_events
                     WHERE task_id = ?1 AND reason_code = 'reported' AND request_id = ?2
                     ORDER BY event_id DESC LIMIT 1",
                    params![task_id, report_request_id],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
                .optional()
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("查询 report provenance 失败: {}", e))
                })?;
            let Some((reported_step_id, reported_evidence_path, reported_evidence_hash)) = report
            else {
                return Err(DaemonRpcError::new(
                    "E_HANDOFF_REPORT_NOT_FOUND",
                    format!(
                        "report_request_id={} 未找到该任务已持久化的 report",
                        report_request_id
                    ),
                ));
            };
            if step_id.as_deref() != Some(reported_step_id.as_str())
                || reported_evidence_path != evidence_path
                || reported_evidence_hash != evidence_hash
            {
                return Err(DaemonRpcError::new(
                    "E_HANDOFF_REPORT_PROVENANCE_MISMATCH",
                    "report_request_id 与 step_id/evidence_path/evidence_hash 不一致",
                ));
            }
        }

        // Reviewer PASS 不能仅凭 handoff envelope 进入 Adjudicator 阶段：
        // Verdict Ledger 是 review 结论的唯一权威来源，必须先存在同 task、
        // 同 source step 的真实 pass verdict，并带完整 review snapshot / view
        // manifest / workspace / reviewer identity provenance。此校验必须发生在
        // handoff ledger 写入前，避免留下“PASS handoff + verdicts=[]”的半状态。
        let reviewer_pass_provenance: Option<(String, String, String)> =
            if outcome == "reviewer_pass" {
                if current_status != "review" {
                    return Err(DaemonRpcError::new(
                        "E_HANDOFF_REVIEW_STATE_REQUIRED",
                        "reviewer_pass 仅允许从 review 状态提交",
                    ));
                }
                let source_step_id = step_id.as_deref().ok_or_else(|| {
                    DaemonRpcError::new(
                        "E_HANDOFF_STEP_REQUIRED",
                        "reviewer_pass 必须绑定 source step",
                    )
                })?;
                let verdict: Option<(String, String, String, String, Option<i64>, String)> = tx
                    .query_row(
                        "SELECT verdict_id, step_id, snapshot_id, view_manifest_hash,
                                workspace_id, reviewer_identity
                         FROM task_verdict_events
                         WHERE task_id = ?1 AND overall = 'pass'
                         ORDER BY id DESC LIMIT 1",
                        params![task_id],
                        |row| {
                            Ok((
                                row.get(0)?,
                                row.get(1)?,
                                row.get(2)?,
                                row.get(3)?,
                                row.get(4)?,
                                row.get(5)?,
                            ))
                        },
                    )
                    .optional()
                    .map_err(|e| {
                        DaemonRpcError::internal_error(format!(
                            "读取 reviewer pass verdict provenance 失败: {}",
                            e
                        ))
                    })?;
                let Some((verdict_id, verdict_step_id, snapshot_id, manifest_hash, verdict_workspace, reviewer_raw)) = verdict else {
                    return Err(DaemonRpcError::new(
                        "E_HANDOFF_VERDICT_REQUIRED",
                        "reviewer_pass 必须先通过 verdict.submit 持久化同 task 的 pass Verdict Ledger",
                    ));
                };
                if verdict_step_id.trim() != source_step_id
                    || snapshot_id.trim().is_empty()
                    || manifest_hash.trim().is_empty()
                {
                    return Err(DaemonRpcError::new(
                        "E_HANDOFF_VERDICT_PROVENANCE_MISMATCH",
                        "reviewer_pass 的 verdict 必须绑定同 source step、非空 snapshot_id 和 view_manifest_hash",
                    ));
                }
                let task_workspace_id: Option<i64> = tx
                    .query_row(
                        "SELECT workspace_id FROM task_workspace_bindings WHERE task_id = ?1",
                        params![task_id],
                        |row| row.get(0),
                    )
                    .optional()
                    .map_err(|e| {
                        DaemonRpcError::internal_error(format!(
                            "读取 reviewer pass task workspace 失败: {}",
                            e
                        ))
                    })?;
                if task_workspace_id.is_none() || verdict_workspace != task_workspace_id {
                    return Err(DaemonRpcError::new(
                        "E_HANDOFF_VERDICT_PROVENANCE_MISMATCH",
                        "reviewer_pass 的 verdict workspace_id 与 task workspace binding 不一致",
                    ));
                }
                let reviewer_value = serde_json::from_str::<Value>(&reviewer_raw).map_err(|_| {
                    DaemonRpcError::new(
                        "E_HANDOFF_VERDICT_PROVENANCE_INVALID",
                        "reviewer_pass 的 verdict reviewer_identity 不是有效 JSON",
                    )
                })?;
                let verdict_identity = reviewer_value
                    .get("identity")
                    .unwrap_or(&reviewer_value);
                if identity.agent_instance_id.trim().is_empty() {
                    return Err(DaemonRpcError::new(
                        "E_HANDOFF_VERDICT_IDENTITY_MISMATCH",
                        "reviewer_pass 的当前 handoff identity 必须包含非空 agent_instance_id",
                    ));
                }
                let identity_matches = [
                    "agent_id",
                    "agent_instance_id",
                    "session_id",
                    "model_id",
                ]
                    .into_iter()
                    .all(|field| {
                        verdict_identity
                            .get(field)
                            .and_then(Value::as_str)
                            .is_some_and(|value| {
                                !value.trim().is_empty()
                                    && value
                                        == match field {
                                            "agent_id" => identity.agent_id.as_str(),
                                            "agent_instance_id" => {
                                                identity.agent_instance_id.as_str()
                                            }
                                            "session_id" => identity.session_id.as_str(),
                                            "model_id" => identity.model_id.as_str(),
                                            _ => unreachable!(),
                                        }
                            })
                    });
                let verdict_role_ok = verdict_identity
                    .get("role")
                    .and_then(Value::as_str)
                    .is_some_and(|role| {
                        matches!(role, "reviewer" | "independent_reviewer")
                            && role == identity.role
                    });
                if !identity_matches || !verdict_role_ok {
                    return Err(DaemonRpcError::new(
                        "E_HANDOFF_VERDICT_IDENTITY_MISMATCH",
                        "reviewer_pass 的 verdict reviewer identity 与当前 handoff identity 不一致",
                    ));
                }
                Some((verdict_id, snapshot_id, manifest_hash))
            } else {
                None
            };

        // Reviewer BLOCKED / Adjudicator RETURNED 都是同一主任务上的治理回复：
        // 从唯一 Verdict Ledger 读取 source verdict/findings，并在本事务中准备
        // 一个 provenance-bound fix_defect。不得创建 child task，也不得修改被审
        // 步骤或历史 verdict。两条路由共享同一事务，避免裁决退回留下旧 Reviewer
        // 队列而没有可领取的 Executor remediation。
        let remediation_plan: Option<(String, String, String, String, Value, String)> = if matches!(
            outcome.as_str(),
            "reviewer_blocked" | "adjudicator_returned"
        ) {
            if current_status != "review" {
                let mut replayable = false;
                let mut replay_stmt = tx
                    .prepare(
                        "SELECT reason FROM task_events
                         WHERE task_id = ?1 AND reason_code = 'handoff_structured'
                         ORDER BY event_id DESC",
                    )
                    .map_err(|e| {
                        DaemonRpcError::internal_error(format!("查询 handoff replay 失败: {}", e))
                    })?;
                let rows = replay_stmt
                    .query_map(params![task_id], |row| row.get::<_, String>(0))
                    .map_err(|e| {
                        DaemonRpcError::internal_error(format!("读取 handoff replay 失败: {}", e))
                    })?;
                for row in rows {
                    let raw = row.map_err(|e| {
                        DaemonRpcError::internal_error(format!("读取 handoff replay 失败: {}", e))
                    })?;
                    if serde_json::from_str::<Value>(&raw)
                        .ok()
                        .and_then(|value| {
                            value
                                .get("request_id")
                                .and_then(Value::as_str)
                                .map(str::to_string)
                        })
                        .as_deref()
                        == Some(request_id.as_str())
                    {
                        replayable = true;
                        break;
                    }
                }
                drop(replay_stmt);
                if !replayable {
                    return Err(DaemonRpcError::new(
                        "E_REMEDIATION_REVIEW_STATE_REQUIRED",
                        format!(
                            "{} 只能从 review 状态追加原地整改",
                            outcome
                        ),
                    ));
                }
            }
            let (target_file, target_symbol, check_items): (String, String, String) =
                if let Some(source_step_id) = step_id.as_deref() {
                    tx.query_row(
                        "SELECT target_file, target_symbol, check_items
                             FROM task_steps WHERE id = ?1 AND task_id = ?2",
                        params![source_step_id, task_id],
                        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                    )
                    .map_err(|_| {
                        DaemonRpcError::new(
                            "E_REMEDIATION_SOURCE_STEP_INVALID",
                            "handoff step_id 不属于目标主任务",
                        )
                    })?
                } else {
                    // task-level BLOCKED 没有源步骤；整改步骤仍必须存在，
                    // 但其文件/符号/检查范围由 Executor 在整改合同中重新
                    // 明确，不能从不存在的 step 猜测范围。
                    (String::new(), String::new(), String::new())
                };
            let source_overall = if outcome == "reviewer_blocked" {
                "block"
            } else {
                "pass"
            };
            let (
                source_verdict_id,
                source_findings_raw,
                source_reviewer_raw,
                verdict_step_id,
                verdict_snapshot_id,
                verdict_view_manifest_hash,
                verdict_workspace_id,
            ): (String, String, String, String, String, String, Option<i64>) = tx
                .query_row(
                    "SELECT verdict_id, findings, reviewer_identity, step_id,
                            snapshot_id, view_manifest_hash, workspace_id
                         FROM task_verdict_events
                         WHERE task_id = ?1 AND overall = ?2
                         ORDER BY id DESC LIMIT 1",
                    params![task_id, source_overall],
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
                .map_err(|_| {
                    DaemonRpcError::new(
                        "E_REMEDIATION_VERDICT_REQUIRED",
                        format!(
                            "{} 必须绑定当前任务的权威 {} verdict",
                            outcome, source_overall
                        ),
                    )
                })?;
            let task_workspace_id: Option<i64> = tx
                .query_row(
                    "SELECT workspace_id FROM task_workspace_bindings WHERE task_id = ?1",
                    params![task_id],
                    |row| row.get(0),
                )
                .optional()
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!(
                        "读取 task workspace provenance 失败: {}",
                        e
                    ))
                })?;
            if verdict_snapshot_id.trim().is_empty() || verdict_view_manifest_hash.trim().is_empty()
            {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_REVIEW_PROVENANCE_REQUIRED",
                    "source verdict 必须绑定非空 snapshot_id/view_manifest_hash",
                ));
            }
            if task_workspace_id.is_none() || verdict_workspace_id != task_workspace_id {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_REVIEW_PROVENANCE_MISMATCH",
                    "source verdict workspace_id 与 task workspace binding 不一致",
                ));
            }
            let verdict_step_id = verdict_step_id.trim();
            match (step_id.as_deref(), verdict_step_id.is_empty()) {
                (Some(source_step_id), false) if source_step_id == verdict_step_id => {}
                (Some(_), _) => {
                    return Err(DaemonRpcError::new(
                        "E_REMEDIATION_VERDICT_STEP_MISMATCH",
                        "handoff step_id 与 source verdict step_id 不一致",
                    ));
                }
                (None, true) if outcome == "reviewer_blocked" => {}
                (None, false) if outcome == "reviewer_blocked" => {}
                (None, _) => {
                    return Err(DaemonRpcError::new(
                        "E_REMEDIATION_VERDICT_STEP_MISMATCH",
                        "task-level handoff 不得引用带 source step 的 verdict",
                    ));
                }
            }
            let source_reviewer = serde_json::from_str::<Value>(&source_reviewer_raw).map_err(|_| {
                DaemonRpcError::new(
                    "E_REMEDIATION_REVIEW_PROVENANCE_INVALID",
                    "source verdict reviewer_identity 不是有效 JSON",
                )
            })?;
            let reviewer_identity = source_reviewer
                .get("identity")
                .unwrap_or(&source_reviewer);
            let verdict_role = reviewer_identity.get("role").and_then(Value::as_str);
            let identity_complete = ["agent_id", "session_id", "model_id"]
                .into_iter()
                .all(|field| {
                    reviewer_identity
                        .get(field)
                        .and_then(Value::as_str)
                        .is_some_and(|value| !value.trim().is_empty())
                });
            if !identity_complete
                || verdict_role
                    .is_some_and(|role| !matches!(role, "reviewer" | "independent_reviewer"))
            {
                return Err(DaemonRpcError::new(
                    "E_REMEDIATION_REVIEW_PROVENANCE_INVALID",
                    "source verdict reviewer_identity 缺少完整 reviewer identity",
                ));
            }
            if outcome == "reviewer_blocked" {
                let verdict_agent = reviewer_identity
                    .get("agent_id")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let verdict_session = reviewer_identity
                    .get("session_id")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if verdict_agent != identity.agent_id || verdict_session != identity.session_id {
                    return Err(DaemonRpcError::new(
                        "E_REMEDIATION_VERDICT_IDENTITY_MISMATCH",
                        "handoff Reviewer 与 source verdict identity 不一致",
                    ));
                }
            }
            let source_findings = serde_json::from_str::<Value>(&source_findings_raw)
                .ok()
                .filter(Value::is_array)
                .filter(|value| {
                    outcome == "adjudicator_returned"
                        || value
                            .as_array()
                            .map(|items| !items.is_empty())
                            .unwrap_or(false)
                })
                .ok_or_else(|| {
                    DaemonRpcError::new(
                        "E_REMEDIATION_FINDINGS_REQUIRED",
                        if outcome == "reviewer_blocked" {
                            "block verdict 必须携带至少一个结构化 finding"
                        } else {
                            "pass verdict 的 findings 必须是结构化数组"
                        },
                    )
                })?;

            // 同一 verdict 只能产生一个整改回复；重试必须复用原 request_id，
            // 防止用不同 request_id 复制 remediation。
            let mut existing_steps = tx
                .prepare(
                    "SELECT id, result FROM task_steps
                     WHERE task_id = ?1 AND action = 'fix_defect'
                     ORDER BY step_index ASC",
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("查询 verdict remediation 失败: {}", e))
                })?;
            let rows = existing_steps
                .query_map(params![task_id], |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
                })
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("读取 verdict remediation 失败: {}", e))
                })?;
            for row in rows {
                let (_existing_id, raw) = row.map_err(|e| {
                    DaemonRpcError::internal_error(format!("读取 verdict remediation 失败: {}", e))
                })?;
                let value = serde_json::from_str::<Value>(&raw).unwrap_or(Value::Null);
                if value.get("source_verdict_id").and_then(Value::as_str)
                    == Some(source_verdict_id.as_str())
                {
                    if value
                        .get("source_handoff_request_id")
                        .and_then(Value::as_str)
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
                    format!(
                        "{}:{}:{}:{}",
                        task_id,
                        source_verdict_id,
                        step_id.as_deref().unwrap_or("task-level"),
                        request_id
                    )
                    .as_bytes()
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
            // `target_role` is the canonical handoff envelope field consumed by
            // the read-only inbound_handoff projection. Keep `next_role` as the
            // routing-facing compatibility field, but persist both from the
            // same validated route so the provenance chain is not lossy.
            ("target_role", Value::String(next_role.clone())),
            ("next_role", Value::String(next_role.clone())),
            ("next_action", Value::String(next_action.clone())),
            ("reason", Value::String(reason.clone())),
            (
                "independence_requirement",
                Value::String(independence_requirement.clone()),
            ),
            ("request_id", Value::String(request_id.clone())),
            (
                "step_id",
                step_id.clone().map(Value::String).unwrap_or(Value::Null),
            ),
            (
                "report_request_id",
                Value::String(report_request_id.clone()),
            ),
            ("evidence_path", Value::String(evidence_path.clone())),
            ("evidence_hash", Value::String(evidence_hash.clone())),
            (
                "fencing_counter",
                Value::Number(serde_json::Number::from(fencing_counter)),
            ),
        ] {
            envelope.insert(key.to_string(), value);
        }
        // event_id 是 SQLite 自增实现细节，不能作为后续 remediation 的稳定引用；
        // 先生成与 task/request 绑定的 envelope ID，再把同一值写入 event 和 metadata。
        let handoff_event_id = format!(
            "he-{}",
            &sha256_hex(format!("{}:{}", task_id, request_id).as_bytes())[..24]
        );
        envelope.insert("source_role".to_string(), Value::String(from_role.clone()));
        envelope.insert(
            "handoff_event_id".to_string(),
            Value::String(handoff_event_id.clone()),
        );
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
        if let Some((verdict_id, snapshot_id, manifest_hash)) = &reviewer_pass_provenance {
            envelope.insert(
                "source_verdict_id".to_string(),
                Value::String(verdict_id.clone()),
            );
            envelope.insert(
                "snapshot_id".to_string(),
                Value::String(snapshot_id.clone()),
            );
            envelope.insert(
                "view_manifest_hash".to_string(),
                Value::String(manifest_hash.clone()),
            );
        }
        let envelope_value = Value::Object(envelope);
        let envelope_json = serde_json::to_string(&envelope_value)
            .map_err(|e| DaemonRpcError::internal_error(format!("序列化 handoff 失败: {}", e)))?;

        let mut existing = tx.prepare(
            "SELECT event_id, reason FROM task_events WHERE task_id = ?1 AND reason_code = 'handoff_structured' ORDER BY event_id DESC",
        ).map_err(|e| DaemonRpcError::internal_error(format!("查询 handoff ledger 失败: {}", e)))?;
        let rows = existing
            .query_map(params![task_id], |row| {
                Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
            })
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 handoff ledger 失败: {}", e))
            })?;
        for row in rows {
            let (event_id, raw) = row.map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 handoff 事件失败: {}", e))
            })?;
            if let Ok(previous) = serde_json::from_str::<Value>(&raw) {
                if previous.get("request_id").and_then(|v| v.as_str()) == Some(request_id.as_str())
                {
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
                    return Err(DaemonRpcError::new(
                        "E_REQUEST_ID_REUSE_MISMATCH",
                        "handoff request_id 参数冲突",
                    ));
                }
            }
        }
        drop(existing);

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity,
              agent_session_id, role, monotonic_seq, authoritative_timestamp,
              evidence_path, evidence_hash, snapshot_id)
             VALUES (?1, ?2, ?2, 'handoff_structured', ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            params![
                task_id,
                current_status,
                envelope_json,
                owner_key,
                identity.session_id,
                from_role,
                seq,
                ts,
                evidence_path,
                evidence_hash,
                reviewer_pass_provenance
                    .as_ref()
                    .map(|(_, snapshot_id, _)| snapshot_id.as_str())
                    .unwrap_or("")
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
            let max_idx: i64 = tx
                .query_row(
                    "SELECT COALESCE(MAX(step_index), -1) FROM task_steps WHERE task_id = ?1",
                    params![task_id],
                    |row| row.get(0),
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!(
                        "查询 remediation step_index 失败: {}",
                        e
                    ))
                })?;
            let metadata = serde_json::json!({
                "remediation_of_step_id": step_id,
                "source_outcome": outcome,
                "source_verdict_id": source_verdict_id,
                "source_findings": source_findings,
                "source_handoff_event_id": handoff_event_id,
                "source_handoff_request_id": request_id,
            })
            .to_string();
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
            )
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("追加 governance remediation 失败: {}", e))
            })?;
            // 治理退回产生的 remediation 也必须绑定当前任务的 Executor Role
            // Contract；否则步骤虽已进入 assignment queue，却无法通过合同门禁领取。
            bind_step_to_executor_role_contract(&tx, &task_id, remediation_step_id, &owner_key)?;
            tx.execute(
                "UPDATE tasks SET status = 'in_progress', updated_at = ?1 WHERE id = ?2",
                params![ts, task_id],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("reopen 主任务失败: {}", e)))?;
            "in_progress"
        } else {
            current_status.as_str()
        };
        let completed_assignment_ids = if remediation_plan.is_some() {
            // 治理退回会替换 review 前的整条 assignment 投影：可能同时残留
            // Reviewer、Adjudicator，或已经失效的旧 Executor assignment。全部在
            // 同一事务中追加 completed 事件，再排入唯一 remediation Executor；
            // 历史 assignment 事件本身保持不可变。
            assignment_queue::complete_assignments(
                &tx,
                &task_id,
                None,
                None,
                &owner_key,
                &identity.session_id,
                self.next_seq(),
                ts,
            )?
        } else {
            assignment_queue::complete_assignments(
                &tx,
                &task_id,
                step_id.as_deref(),
                Some(from_role.as_str()),
                &owner_key,
                &identity.session_id,
                self.next_seq(),
                ts,
            )?
        };
        let next_assignment_id = if let Some((remediation_step_id, ..)) = remediation_plan.as_ref() {
            assignment_queue::queue_assignment(
                &tx,
                &task_id,
                Some(remediation_step_id),
                "executor",
                &request_id,
                Some(event_id),
                &owner_key,
                &identity.session_id,
                self.next_seq(),
                ts,
            )?
        } else {
            assignment_queue::queue_assignment(
                &tx,
                &task_id,
                step_id.as_deref(),
                &next_role,
                &request_id,
                Some(event_id),
                &owner_key,
                &identity.session_id,
                self.next_seq(),
                ts,
            )?
        };
        record_action_identity(&tx, &task_id, &identity, "task.handoff", seq, ts)?;
        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 task.handoff 事务失败: {}", e))
        })?;

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
        res.insert(
            "completed_assignment_ids".to_string(),
            Value::Array(completed_assignment_ids.into_iter().map(Value::String).collect()),
        );
        res.insert(
            "assignment_id".to_string(),
            next_assignment_id.map(Value::String).unwrap_or(Value::Null),
        );
        Ok(Value::Object(res))
    }

}
