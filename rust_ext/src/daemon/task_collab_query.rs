//! 任务只读查询域：状态、重conciliation 和事件投影。
//! 保持原有查询过滤、workspace binding 和治理投影语义。

use super::*;

impl TaskCollabStore {
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
            .map_err(|_| {
                DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id))
            })?;

        let (_claimed_actor, claimed_session) = self.get_task_claim_info(&conn, task_id);

        // status 是 flat CLI/MCP 的事实来源，不能只返回任务头部而丢掉
        // task_steps；否则客户端会把已有步骤错误显示为 Steps (0)。
        let steps = {
            let mut stmt = conn
                .prepare(
                    "SELECT id, step_index, action, target_file, target_symbol, check_items,
                            status, result, created_at, completed_at
                     FROM task_steps WHERE task_id = ?1 ORDER BY step_index ASC",
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("查询 task_steps 失败: {e}"))
                })?;
            let rows = stmt
                .query_map(params![task_id], |r| {
                    Ok(serde_json::json!({
                        "step_id": r.get::<_, String>(0)?,
                        "step_index": r.get::<_, i64>(1)?,
                        "action": r.get::<_, String>(2)?,
                        "target_file": r.get::<_, String>(3)?,
                        "target_symbol": r.get::<_, String>(4)?,
                        "check_items": r.get::<_, String>(5)?,
                        "status": r.get::<_, String>(6)?,
                        "result": r.get::<_, String>(7)?,
                        "created_at": r.get::<_, f64>(8)?,
                        "completed_at": r.get::<_, Option<f64>>(9)?,
                    }))
                })
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("映射 task_steps 失败: {e}"))
                })?;
            rows.flatten().collect::<Vec<Value>>()
        };
        let total_steps = steps.len() as i64;
        let done_steps = steps
            .iter()
            .filter(|step| matches!(step["status"].as_str(), Some("done" | "skipped")))
            .count() as i64;
        let ratio = if total_steps > 0 {
            done_steps as f64 / total_steps as f64
        } else {
            0.0
        };

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(row.0));
        res.insert("title".to_string(), Value::String(row.1));
        res.insert("description".to_string(), Value::String(row.2));
        res.insert("parent_id".to_string(), Value::String(row.3));
        let governance = tree_governance_projection(&conn, task_id, &row.4);
        res.insert("status".to_string(), Value::String(row.4));
        for key in [
            "lifecycle_status",
            "workflow_status",
            "review",
            "blocking_reasons",
        ] {
            if let Some(value) = governance.get(key) {
                res.insert(key.to_string(), value.clone());
            }
        }
        res.insert("governance".to_string(), governance);
        res.insert("steps".to_string(), Value::Array(steps));
        res.insert(
            "progress".to_string(),
            serde_json::json!({
                "total": total_steps,
                "done": done_steps,
                "progress": ratio,
                "ratio": ratio,
                "percent": (ratio * 100.0 * 100.0).round() / 100.0,
            }),
        );
        res.insert("creator".to_string(), Value::String(row.5));
        res.insert(
            "claimed_by".to_string(),
            Value::String(claimed_session.unwrap_or_default()),
        );
        res.insert(
            "created_at".to_string(),
            Value::Number(serde_json::Number::from_f64(row.6).unwrap()),
        );
        res.insert(
            "updated_at".to_string(),
            Value::Number(serde_json::Number::from_f64(row.7).unwrap()),
        );
        Ok(Value::Object(res))
    }

    /// 只依据确定性的步骤事实清洗历史生命周期记录。
    pub fn handle_task_reconcile(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let root_task_id = params
            .get("root_task_id")
            .or_else(|| params.get("task_id"))
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("task.reconcile 缺少 root_task_id"))?;
        let workspace_instance_id = params
            .get("workspace_instance_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                DaemonRpcError::invalid_params("task.reconcile 缺少 workspace_instance_id")
            })?;
        let apply = params
            .get("apply")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let request_id = params
            .get("request_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_string);
        let identity = if apply {
            Some(parse_action_identity(params)?.ok_or_else(|| {
                DaemonRpcError::new(
                    "E_IDENTITY_REQUIRED",
                    "task.reconcile apply 必须携带完整 identity",
                )
            })?)
        } else {
            None
        };
        if apply && request_id.is_none() {
            return Err(DaemonRpcError::invalid_params(
                "task.reconcile apply 必须携带 request_id",
            ));
        }

        let mut conn = self
            .conn
            .lock()
            .map_err(|_| DaemonRpcError::internal_error("task reconcile store 连接锁 poisoned"))?;
        // 治理写门禁（P0-E 修复）：apply 必须由 adjudicator 持 root anchor 的独立
        // reviewer lease（token+fencing+holder 校验），与 task.apply/close/attest/
        // contract-bootstrap 一致。校验在任何写（含 dedupe ledger 占位）之前完成，
        // 拒绝路径保持无状态变化。dry-run（apply=false）保持只读无凭证要求。
        if apply {
            let identity_ref = identity.as_ref().ok_or_else(|| {
                DaemonRpcError::new(
                    "E_IDENTITY_REQUIRED",
                    "task.reconcile apply 必须携带完整 identity",
                )
            })?;
            if identity_ref.role != "adjudicator" {
                return Err(DaemonRpcError::new(
                    "E_RECONCILE_ADJUDICATOR_ROLE_REQUIRED",
                    format!(
                        "task.reconcile apply 仅允许 role=adjudicator，实际 role={}",
                        identity_ref.role
                    ),
                ));
            }
            let (token, counter) = Self::require_lease_params(params)?;
            self.validate_reviewer_lease_for_adjudication(
                &conn,
                root_task_id,
                &token,
                counter,
                identity_ref,
            )?;
        }
        let task_ids = reconciliation_task_ids(&conn, root_task_id)?;
        if task_ids.is_empty() {
            return Err(DaemonRpcError::new(
                "task_not_found",
                format!("任务不存在: {}", root_task_id),
            ));
        }
        let method = "task.reconcile";
        let dedupe = if apply {
            Some(OperationStore.dedupe(
                &conn,
                workspace_instance_id,
                method,
                request_id.as_deref().unwrap_or_default(),
                params,
            )?)
        } else {
            None
        };
        if let Some(DedupeOutcome::Replay {
            response_or_error_json,
        }) = dedupe.as_ref()
        {
            return replay_reconciliation_result(response_or_error_json.clone());
        }

        let planned = reconciliation_candidates(&conn, &task_ids, workspace_instance_id)?;
        let dry_run_skipped = planned
            .iter()
            .filter(|item| item.get("eligible").and_then(Value::as_bool) == Some(false))
            .map(|item| {
                reconciliation_skip(
                    item.get("task_id")
                        .and_then(Value::as_str)
                        .unwrap_or_default(),
                    item.get("blocked_reason")
                        .and_then(Value::as_str)
                        .and_then(|value| value.split(':').next())
                        .unwrap_or("E_RECONCILE_BLOCKED"),
                    item.get("blocked_reason")
                        .and_then(Value::as_str)
                        .unwrap_or_default(),
                )
            })
            .collect::<Vec<_>>();
        if !apply {
            return Ok(reconciliation_response(
                root_task_id,
                workspace_instance_id,
                false,
                planned,
                Vec::new(),
                dry_run_skipped,
            ));
        }

        let DedupeOutcome::FirstRequest {
            rules,
            canonical_params_hash,
        } = dedupe.expect("apply reconciliation 必须已完成 dedupe")
        else {
            unreachable!("reconciliation replay 在上方已返回")
        };
        let tx = conn.unchecked_transaction().map_err(|e| {
            DaemonRpcError::internal_error(format!("开启 task.reconcile 事务失败: {}", e))
        })?;
        let current = reconciliation_candidates(&tx, &task_ids, workspace_instance_id)?;
        let mut applied_items = Vec::new();
        let mut skipped = Vec::new();
        let actor = identity.as_ref().expect("apply 已校验 identity");
        for candidate in &current {
            let task_id = candidate
                .get("task_id")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if candidate.get("eligible").and_then(Value::as_bool) != Some(true) {
                let blocked = candidate
                    .get("blocked_reason")
                    .and_then(Value::as_str)
                    .unwrap_or("E_RECONCILE_BLOCKED");
                skipped.push(reconciliation_skip(
                    task_id,
                    blocked.split(':').next().unwrap_or("E_RECONCILE_BLOCKED"),
                    blocked,
                ));
                continue;
            }
            let workspace_id = candidate
                .get("workspace_id")
                .and_then(Value::as_i64)
                .ok_or_else(|| {
                    DaemonRpcError::new(
                        "E_WORKSPACE_AUTHORITY_UNAVAILABLE",
                        "reconciliation target 缺少 workspace binding",
                    )
                })?;
            let changed = tx
                .execute(
                    "UPDATE tasks SET status = 'in_progress', updated_at = ?1
                 WHERE id = ?2 AND status = 'review'",
                    params![task_now_ts(), task_id],
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!(
                        "reconcile 更新 task status 失败: {}",
                        e
                    ))
                })?;
            if changed == 0 {
                skipped.push(reconciliation_skip(
                    task_id,
                    "E_RECONCILE_STATE_CHANGED",
                    "apply 前任务已不再处于 review",
                ));
                continue;
            }
            let seq = self.next_seq();
            let ts = task_now_ts();
            let event_reason = json!({
                "source": method,
                "root_task_id": root_task_id,
                "reason": candidate.get("reason").and_then(Value::as_str).unwrap_or("review has non-terminal task steps"),
                "preserves_steps": true,
            }).to_string();
            tx.execute(
                "INSERT INTO task_events
                 (task_id, workspace_id, from_status, to_status, reason_code, reason,
                  actor_identity, agent_session_id, role, monotonic_seq, authoritative_timestamp)
                 VALUES (?1, ?2, 'review', 'in_progress', 'reconcile_non_terminal_steps',
                         ?3, ?4, ?5, ?6, ?7, ?8)",
                params![
                    task_id,
                    workspace_id,
                    event_reason,
                    actor.agent_id,
                    actor.session_id,
                    actor.role,
                    seq,
                    ts
                ],
            )
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("追加 task.reconcile event 失败: {}", e))
            })?;
            record_action_identity(&tx, task_id, actor, method, seq, ts)?;
            applied_items.push(candidate.clone());
        }
        let response = reconciliation_response(
            root_task_id,
            workspace_instance_id,
            true,
            current,
            applied_items.clone(),
            skipped,
        );
        let provenance = LedgerProvenance {
            workspace_id: applied_items
                .first()
                .and_then(|item| item.get("workspace_id"))
                .and_then(Value::as_i64),
            task_id: Some(root_task_id.to_string()),
            ..Default::default()
        };
        OperationStore.record_result(
            &tx,
            workspace_instance_id,
            method,
            request_id.as_deref().unwrap_or_default(),
            &rules,
            &canonical_params_hash,
            &provenance,
            &response,
        )?;
        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 task.reconcile 事务失败: {}", e))
        })?;
        self.save_dedup(params, &response);
        Ok(response)
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
                m.insert(
                    "event_id".to_string(),
                    Value::Number(r.get::<_, i64>(0)?.into()),
                );
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
                m.insert(
                    "monotonic_seq".to_string(),
                    Value::Number(r.get::<_, i64>(12)?.into()),
                );
                m.insert(
                    "authoritative_timestamp".to_string(),
                    Value::Number(serde_json::Number::from_f64(r.get(13)?).unwrap()),
                );
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

}

impl TaskCollabStore {
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
                    return Err(DaemonRpcError::new(
                        "task_not_found",
                        format!("任务不存在: {}", task_id),
                    ));
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

        let params_refs: Vec<&dyn rusqlite::ToSql> =
            params_vec.iter().map(|p| p.as_ref()).collect();

        let rows = stmt
            .query_map(params_refs.as_slice(), |r| {
                let mut m = Map::new();
                m.insert("task_id".to_string(), Value::String(r.get(0)?));
                m.insert("title".to_string(), Value::String(r.get(1)?));
                m.insert("description".to_string(), Value::String(r.get(2)?));
                m.insert("parent_id".to_string(), Value::String(r.get(3)?));
                m.insert("status".to_string(), Value::String(r.get(4)?));
                m.insert("creator".to_string(), Value::String(r.get(5)?));
                m.insert(
                    "created_at".to_string(),
                    Value::Number(
                        serde_json::Number::from_f64(r.get(6)?)
                            .unwrap_or(serde_json::Number::from(0)),
                    ),
                );
                m.insert(
                    "updated_at".to_string(),
                    Value::Number(
                        serde_json::Number::from_f64(r.get(7)?)
                            .unwrap_or(serde_json::Number::from(0)),
                    ),
                );
                Ok(Value::Object(m))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("query task_list 失败: {}", e)))?;

        let mut tasks = Vec::new();
        for r in rows.flatten() {
            tasks.push(r);
        }

        // 列表与 status/status_tree 使用同一份 daemon 治理投影。历史任务
        // 可能缺少 binding/capture 或合同；tree_governance_projection 会
        // 明确返回 governance_blocked，而不是按 raw status 猜测下一动作。
        for task in &mut tasks {
            let Some(object) = task.as_object_mut() else {
                continue;
            };
            let task_id = object
                .get("task_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let task_status = object
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let governance = tree_governance_projection(&conn, &task_id, &task_status);
            for key in [
                "lifecycle_status",
                "workflow_status",
                "current_role",
                "next_role",
                "next_action",
                "review",
                "blocking_reasons",
            ] {
                if let Some(value) = governance.get(key) {
                    object.insert(key.to_string(), value.clone());
                }
            }
            object.insert("governance".to_string(), governance);
        }

        let mut res = Map::new();
        res.insert("tasks".to_string(), Value::Array(tasks));
        Ok(Value::Object(res))
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
            let rows = st
                .query_map(params![task_id], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, f64>(4)?,
                    ))
                })
                .ok();

            if let Some(iter) = rows {
                for item in iter.flatten() {
                    let mut obj = Map::new();
                    obj.insert("file_path".to_string(), Value::String(item.0));
                    obj.insert("symbol_hash".to_string(), Value::String(item.1));
                    obj.insert("symbol_hash_before".to_string(), Value::String(item.2));
                    obj.insert("change_type".to_string(), Value::String(item.3));
                    obj.insert(
                        "created_at".to_string(),
                        Value::Number(serde_json::Number::from_f64(item.4).unwrap()),
                    );
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
        let severity = params
            .get("severity")
            .and_then(|v| v.as_str())
            .unwrap_or("");

        // 以 db/schema.py 的 canonical v60 列为准：step_id 是 TEXT，证据列名为
        // evidence（不是历史 Python-only 的 details），resolved_at 允许 NULL。
        // 这里显式投影 legacy 兼容的 details 别名，保持 CLI/MCP 返回契约稳定，
        // 同时避免 SELECT details 在 canonical 库上触发 no such column。
        let mut sql = String::from(
            "SELECT id, task_id, step_id, finding_type, severity, status, message,
                    evidence, source, created_at, resolved_at, resolved_by
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
        let mut stmt = conn.prepare(&sql).map_err(|e| {
            DaemonRpcError::internal_error(format!("prepare quality_findings 失败: {}", e))
        })?;
        let bind_refs: Vec<&dyn rusqlite::ToSql> = bind.iter().map(|b| b.as_ref()).collect();

        let rows = stmt
            .query_map(bind_refs.as_slice(), |r| {
                let mut m = Map::new();
                m.insert("id".to_string(), Value::Number(r.get::<_, i64>(0)?.into()));
                m.insert("task_id".to_string(), Value::String(r.get(1)?));
                m.insert("step_id".to_string(), Value::String(r.get(2)?));
                m.insert("finding_type".to_string(), Value::String(r.get(3)?));
                m.insert("severity".to_string(), Value::String(r.get(4)?));
                m.insert("status".to_string(), Value::String(r.get(5)?));
                m.insert("message".to_string(), Value::String(r.get(6)?));
                let evidence: String = r.get(7).unwrap_or_default();
                m.insert("evidence".to_string(), Value::String(evidence.clone()));
                m.insert("details".to_string(), Value::String(evidence));
                m.insert(
                    "source".to_string(),
                    Value::String(r.get(8).unwrap_or_default()),
                );
                m.insert(
                    "created_at".to_string(),
                    Value::Number(serde_json::Number::from_f64(r.get(9)?).unwrap()),
                );
                let resolved_at: Option<f64> = r.get(10)?;
                m.insert(
                    "resolved_at".to_string(),
                    resolved_at
                        .map(|value| Value::Number(serde_json::Number::from_f64(value).unwrap()))
                        .unwrap_or(Value::Null),
                );
                m.insert(
                    "resolved_by".to_string(),
                    Value::String(r.get(11).unwrap_or_default()),
                );
                Ok(Value::Object(m))
            })
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 quality_findings 失败: {}", e))
            })?;

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
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 has_blocking_findings 失败: {}", e))
            })?;

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
                    obj.insert(
                        "change_count".to_string(),
                        Value::Number(serde_json::Number::from(item.1)),
                    );
                    obj.insert(
                        "first_change_at".to_string(),
                        Value::Number(serde_json::Number::from_f64(item.2).unwrap()),
                    );
                    obj.insert(
                        "last_change_at".to_string(),
                        Value::Number(serde_json::Number::from_f64(item.3).unwrap()),
                    );
                    obj.insert("commit_author".to_string(), Value::String(item.4));
                    obj.insert("commit_message".to_string(), Value::String(item.5));
                    obj.insert(
                        "commit_timestamp".to_string(),
                        Value::Number(serde_json::Number::from_f64(item.6).unwrap()),
                    );
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

/// 为任务树节点提供同一份 daemon 治理投影。
///
/// 历史任务可能没有不可变 binding/合同，不能为了“补数据”伪造治理记录；
/// 这类节点仍返回生命周期状态，并明确标为 governance_blocked 及原因。

pub(crate) fn tree_governance_projection(conn: &Connection, task_id: &str, task_status: &str) -> Value {
    let instance: Option<String> = conn
        .query_row(
            "SELECT c.workspace_instance_id
             FROM task_workspace_bindings b
             JOIN workspace_authority_captures c
               ON c.workspace_capture_id = b.workspace_capture_id
             WHERE b.task_id = ?1
             ORDER BY c.capture_revision DESC LIMIT 1",
            params![task_id],
            |row| row.get(0),
        )
        .optional()
        .unwrap_or(None);

    let Some(instance) = instance else {
        return serde_json::json!({
            "task_id": task_id,
            "lifecycle_status": task_status,
            "workflow_status": "governance_blocked",
            "current_role": Value::Null,
            "next_role": Value::Null,
            "next_action": "none",
            "review": {"state": "not_in_review"},
            "blocking_reasons": ["任务缺少不可变 workspace binding/capture，无法验证治理投影"],
            "decision": "BLOCKED",
            "action": "NONE",
        });
    };

    match crate::daemon::task_loop::next_action::evaluate_next_action(conn, &instance, task_id) {
        Ok(mut projection) => {
            // governance-projection and task.next-action must expose the same
            // fail-closed policy decision. Without this overlay, a task with a
            // contract but no identity_policy appeared READY/CLAIM here while
            // task.next-action correctly rejected the claim.
            let policy_state = match get_current_task_contract_policy_state(conn, task_id) {
                Ok(value) => value,
                Err(error) => {
                    return serde_json::json!({
                        "task_id": task_id,
                        "lifecycle_status": task_status,
                        "workflow_status": "governance_blocked",
                        "current_role": Value::Null,
                        "next_role": Value::Null,
                        "next_action": "none",
                        "review": {"state": "not_in_review"},
                        "blocking_reasons": [format!("无法读取 identity policy: {}", error.message)],
                        "decision": "BLOCKED",
                        "action": "NONE",
                    });
                }
            };
            let Some(object) = projection.as_object_mut() else {
                return projection;
            };
            let required_role = object
                .get("required_role")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let (policy_value, policy_status) = match &policy_state {
                TaskContractPolicyState::NoContractRevision => (Value::Null, "no_contract_revision"),
                TaskContractPolicyState::Unresolved => (Value::Null, "unresolved"),
                TaskContractPolicyState::Declared(policy) => {
                    (Value::String(policy.clone()), "declared")
                }
            };
            object.insert("identity_policy".to_string(), policy_value);
            object.insert(
                "identity_policy_status".to_string(),
                Value::String(policy_status.to_string()),
            );
            if matches!(&policy_state, TaskContractPolicyState::Declared(policy) if policy == POLICY_ROLE_WORKER_V1) {
                object.insert(
                    "claim_requirements".to_string(),
                    serde_json::json!({
                        "role_worker_auth": {
                            "required": true,
                            "expected_role": required_role,
                            "credential": "one-time, enrolled via role_worker.enroll"
                        },
                        "identity": {"required": false, "provenance_only": true},
                        "workspace_binding": {"required": true},
                        "separation": {"required": true}
                    }),
                );
            }
            let blocked_reason = match &policy_state {
                TaskContractPolicyState::Unresolved => Some(
                    "合同 revision 缺少可解析 identity_policy，claim fail-closed（禁止隐式降级）"
                        .to_string(),
                ),
                TaskContractPolicyState::Declared(policy)
                    if policy != POLICY_ROLE_WORKER_V1
                        && policy != POLICY_LEGACY_IDENTITY_V1 =>
                {
                    Some(format!(
                        "identity policy {policy} 未知，claim fail-closed（禁止隐式降级）"
                    ))
                }
                _ => None,
            };
            if let Some(reason) = blocked_reason {
                object.insert(
                    "workflow_status".to_string(),
                    Value::String("governance_blocked".to_string()),
                );
                object.insert(
                    "next_role".to_string(),
                    Value::String("adjudicator".to_string()),
                );
                object.insert(
                    "next_action".to_string(),
                    Value::String("system_repair_required".to_string()),
                );
                object.insert("decision".to_string(), Value::String("BLOCKED".to_string()));
                object.insert("action".to_string(), Value::String("BLOCKED".to_string()));
                for key in ["blocking_reasons", "blocking_conditions"] {
                    object.insert(key.to_string(), Value::Array(vec![Value::String(reason.clone())]));
                }
                if let Some(Value::Object(routing)) = object.get_mut("routing") {
                    routing.insert(
                        "next_role".to_string(),
                        Value::String("adjudicator".to_string()),
                    );
                    routing.insert(
                        "next_action".to_string(),
                        Value::String("system_repair_required".to_string()),
                    );
                    routing.insert("reason".to_string(), Value::Array(vec![Value::String(reason)]));
                }
            }
            projection
        }
        Err(error) => serde_json::json!({
            "task_id": task_id,
            "lifecycle_status": task_status,
            "workflow_status": "governance_blocked",
            "current_role": Value::Null,
            "next_role": Value::Null,
            "next_action": "none",
            "review": {"state": "not_in_review"},
            "blocking_reasons": [format!("无法生成治理投影: {}", error.message)],
            "decision": "BLOCKED",
            "action": "NONE",
        }),
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
    if let Ok(mut stmt) =
        conn.prepare("SELECT id FROM tasks WHERE parent_id = ?1 ORDER BY sort_order ASC")
    {
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

    let progress = if total > 0 {
        done as f64 / total as f64
    } else {
        0.0
    };
    let percent = (progress * 100.0 * 100.0).round() / 100.0;
    let governance = tree_governance_projection(conn, &row.0, &row.3);

    let mut res = Map::new();
    res.insert("task_id".to_string(), Value::String(row.0));
    res.insert("title".to_string(), Value::String(row.1));
    res.insert("description".to_string(), Value::String(row.2));
    res.insert("status".to_string(), Value::String(row.3));
    if let Some(value) = governance.get("lifecycle_status") {
        res.insert("lifecycle_status".to_string(), value.clone());
    }
    if let Some(value) = governance.get("workflow_status") {
        res.insert("workflow_status".to_string(), value.clone());
    }
    res.insert("governance".to_string(), governance);
    res.insert("creator".to_string(), Value::String(row.4));
    res.insert("claimed_by".to_string(), Value::String(claimed_by));
    res.insert(
        "depth".to_string(),
        Value::Number(serde_json::Number::from(row.5)),
    );
    res.insert(
        "sort_order".to_string(),
        Value::Number(serde_json::Number::from(row.6)),
    );
    res.insert(
        "created_at".to_string(),
        Value::Number(serde_json::Number::from_f64(row.7).unwrap()),
    );
    res.insert(
        "updated_at".to_string(),
        Value::Number(serde_json::Number::from_f64(row.8).unwrap()),
    );
    res.insert(
        "closed_at".to_string(),
        Value::Number(serde_json::Number::from_f64(row.9).unwrap()),
    );
    let mut prog = Map::new();
    prog.insert(
        "total".to_string(),
        Value::Number(serde_json::Number::from(total)),
    );
    prog.insert(
        "done".to_string(),
        Value::Number(serde_json::Number::from(done)),
    );
    prog.insert(
        "progress".to_string(),
        Value::Number(serde_json::Number::from_f64(progress).unwrap()),
    );
    // 保留 legacy progress=ratio，同时提供带单位的 ratio/percent，避免
    // 客户端把 0..1 的比例误显示成百分比。
    prog.insert(
        "ratio".to_string(),
        Value::Number(serde_json::Number::from_f64(progress).unwrap()),
    );
    prog.insert(
        "percent".to_string(),
        Value::Number(serde_json::Number::from_f64(percent).unwrap()),
    );
    res.insert("progress".to_string(), Value::Object(prog));
    res.insert("steps".to_string(), Value::Array(steps));
    res.insert("subtasks".to_string(), Value::Array(subtasks));
    Value::Object(res)
}

// ============================================
// Tests
// ============================================
