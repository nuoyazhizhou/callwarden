//! 任务交付生命周期域：apply、close 与 capture diff。
//! 保留原有 lease、子任务门禁、证据和事务语义。

use super::*;

impl TaskCollabStore {
    pub fn handle_task_apply(
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
        let reviewer = params
            .get("reviewer")
            .and_then(|v| v.as_str())
            .unwrap_or("reviewer");
        let identity = parse_action_identity(params)?;
        let owner_key = peer.owner_key();
        let ts = task_now_ts();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        // S3: lease 受保护写门禁（强制）—— daemon 权威路径下 apply 必须持有完整
        // reviewer lease 凭证，缺失/不完整 fail-closed 返回 E_LEASE_REQUIRED。

        // 校验失败在任何写入前拒绝，不改变 task data（与 Python task_apply 对齐）。
        let (token, counter) = Self::require_lease_params(params)?;
        self.validate_lease_for_mutation(
            &tx,
            task_id,
            "reviewer",
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

        // 观察#1 修复：daemon 权威 apply 必须回填 applied_at 列，与 Python
        // db_tasks.task_apply（line 1990）及 CLI apply_task（cli/task.rs:1157）对齐，
        // 否则 auto/enterprise 模式下 applied_at 恒为 NULL，破坏审计轨迹与级联语义。
        tx.execute(
            "UPDATE tasks SET status = 'applied', applied_at = ?1, updated_at = ?1 WHERE id = ?2",
            params![ts, task_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_apply 失败: {}", e)))?;

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity, role, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'applied', 'applied', 'task applied', ?3, ?4, ?5, ?6)",
            params![task_id, current_status, owner_key,
                    identity.as_ref().map(|id| id.role.as_str()).unwrap_or(""), seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        if let Some(ref id) = identity {
            record_action_identity(&tx, task_id, id, "state_transition", seq, ts)?;
        }

        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 task_apply 事务失败: {}", e))
        })?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String("applied".to_string()));
        res.insert(
            "applied_at".to_string(),
            Value::Number(serde_json::Number::from_f64(ts).unwrap()),
        );
        res.insert("reviewer".to_string(), Value::String(reviewer.to_string()));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_close(
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
        let reviewer = params
            .get("reviewer")
            .and_then(|v| v.as_str())
            .unwrap_or("reviewer");
        let identity = parse_action_identity(params)?;
        let owner_key = peer.owner_key();
        let ts = task_now_ts();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        // S3: lease 受保护写门禁（强制）—— daemon 权威路径下 close 必须持有完整
        // reviewer lease 凭证，缺失/不完整 fail-closed 返回 E_LEASE_REQUIRED。
        // 校验失败在任何写入前拒绝，不改变 task data（与 Python task_close 对齐）。
        let (token, counter) = Self::require_lease_params(params)?;
        self.validate_lease_for_mutation(
            &tx,
            task_id,
            "reviewer",
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

        // S1: 子任务状态门禁 —— 存在任何非 closed 子任务时禁止关闭父任务。
        // 所有子任务均已 closed 时父任务才允许关闭（子任务完成步骤即证据）。
        let child_total: i64 = tx
            .query_row(
                "SELECT COUNT(*) FROM tasks WHERE parent_id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .unwrap_or(0);
        if child_total > 0 {
            let open_children: i64 = tx
                .query_row(
                    "SELECT COUNT(*) FROM tasks WHERE parent_id = ?1 AND status != 'closed'",
                    params![task_id],
                    |r| r.get(0),
                )
                .unwrap_or(0);
            if open_children > 0 {
                return Err(DaemonRpcError::new(
                    "E_CHILD_TASKS_NOT_CLOSED",
                    format!(
                        "任务 {} 存在 {} 个未关闭子任务，禁止关闭",
                        task_id, open_children
                    ),
                ));
            }
        } else {
            // S2: 叶子任务步骤门禁 —— 必须有步骤且全部 done/skipped 才能关闭。
            // failed 步骤判定与 next_action 对齐（§3.4）：已由 `step_resolved`
            // resolution event 覆盖的 failed step 视为已解决，不计入未完成；
            // 仅 unresolved failed + pending/blocked 阻塞关闭。
            let step_count: i64 = tx
                .query_row(
                    "SELECT COUNT(*) FROM task_steps WHERE task_id = ?1",
                    params![task_id],
                    |r| r.get(0),
                )
                .unwrap_or(0);
            if step_count == 0 {
                return Err(DaemonRpcError::new(
                    "E_NO_STEPS",
                    format!("任务 {} 无步骤记录，禁止关闭", task_id),
                ));
            }
            let not_done: i64 = tx
                .query_row(
                    "SELECT COUNT(*) FROM task_steps WHERE task_id = ?1 AND status IN ('pending', 'blocked')",
                    params![task_id],
                    |r| r.get(0),
                )
                .unwrap_or(0);
            let unresolved_failed =
                crate::daemon::task_loop::next_action::unresolved_failed_step_ids(&tx, task_id)?
                    .len() as i64;
            let total_not_done = not_done + unresolved_failed;
            if total_not_done > 0 {
                return Err(DaemonRpcError::new(
                    "E_STEPS_NOT_DONE",
                    format!(
                        "任务 {} 存在 {} 个未完成步骤，禁止关闭",
                        task_id, total_not_done
                    ),
                ));
            }
        }

        // S5: closed_at 写入真实非零时间戳
        tx.execute(
            "UPDATE tasks SET status = 'closed', closed_at = ?1, updated_at = ?1 WHERE id = ?2",
            params![ts, task_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_close 失败: {}", e)))?;

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity, role, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'closed', 'closed', 'task closed', ?3, ?4, ?5, ?6)",
            params![task_id, current_status, owner_key,
                    identity.as_ref().map(|id| id.role.as_str()).unwrap_or(""), seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        if let Some(ref id) = identity {
            record_action_identity(&tx, task_id, id, "state_transition", seq, ts)?;
        }

        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 task_close 事务失败: {}", e))
        })?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String("closed".to_string()));
        res.insert(
            "closed_at".to_string(),
            Value::Number(serde_json::Number::from_f64(ts).unwrap()),
        );
        res.insert("reviewer".to_string(), Value::String(reviewer.to_string()));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    // ============================================
    // Lease Control Plane（Req 11.2-11.9, 14.11-14.12, 14.30）
    //
    // daemon 权威路径：全部写操作在单一 `self.conn` 互斥下执行（BEGIN IMMEDIATE 事务），
    // 时间字段一律使用 `self.clock()`（AuthoritativeClock，单调不回退）；
    // clock 未注入（None）时 fail-closed 返回 E_LEASE_CLOCK_UNAVAILABLE，绝不降级。
    // raw token 仅在 acquire 成功响应返回一次，数据库只存 sha256（Req 11.2）。
    // 与 Python `db/db_task_leases.py` 语义对齐；MCP 工具经 server/tools/tools_p4_lease.py 路由至此。
    // ============================================

    /// 追加一条 Lease 审计事件（append-only，Req 11.6/11.12；调用方负责 commit；不写 raw token）。
    pub fn handle_task_capture_diff(
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
        let step_id = params.get("step_id").and_then(|v| v.as_str()).unwrap_or("");
        let base = params
            .get("base")
            .and_then(|v| v.as_str())
            .unwrap_or("HEAD");
        let dry_run = params
            .get("dry_run")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let source_commit_hash = params
            .get("source_commit_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let skip_quality_review = params
            .get("skip_quality_review")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let owner_key = peer.owner_key();
        let ts = task_now_ts();

        let mut conn = self.conn.lock().unwrap();

        // 完整 capture-diff：change_audit（真实 schema）+ task_symbol_changes + audit_chain 签名
        let result = crate::cli::task::capture_task_diff(
            &mut conn,
            task_id,
            step_id,
            Path::new(""),
            base,
            dry_run,
            source_commit_hash,
            skip_quality_review,
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("capture-diff 执行失败: {}", e)))?;

        // 记录 diff_captured 事件（dry_run 不落事件，对齐 Python 语义）
        if !dry_run {
            let seq = self.next_seq();
            conn.execute(
                "INSERT INTO task_events
                 (task_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
                 VALUES (?1, 'in_progress', 'in_progress', 'diff_captured', ?2, ?3, ?4, ?5)",
                params![task_id, format!("base={}", base), owner_key, seq, ts],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;
        }

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(result.task_id.clone()));
        res.insert("step_id".to_string(), Value::String(result.step_id.clone()));
        res.insert("base".to_string(), Value::String(result.base.clone()));
        res.insert("dry_run".to_string(), Value::Bool(result.dry_run));
        res.insert("scan_id".to_string(), serde_json::json!(result.scan_id));
        let changed_files: Vec<Value> = result
            .changed_files
            .iter()
            .map(|f| {
                let mut m = Map::new();
                m.insert("path".to_string(), Value::String(f.path.clone()));
                m.insert("status".to_string(), Value::String(f.status.clone()));
                Value::Object(m)
            })
            .collect();
        res.insert("changed_files".to_string(), Value::Array(changed_files));
        // linked_symbols 对齐 Python 契约：数组 [{file_path, change_id, linked}]
        let linked_symbols: Vec<Value> = result
            .linked_change_ids
            .iter()
            .map(|(file_path, change_id)| {
                let mut m = Map::new();
                m.insert("file_path".to_string(), Value::String(file_path.clone()));
                m.insert("change_id".to_string(), Value::String(change_id.clone()));
                m.insert("linked".to_string(), Value::Bool(true));
                Value::Object(m)
            })
            .collect();
        res.insert("linked_symbols".to_string(), Value::Array(linked_symbols));
        let findings: Vec<Value> = result
            .quality_findings
            .iter()
            .map(|f| {
                let mut m = Map::new();
                m.insert("id".to_string(), serde_json::json!(f.id));
                m.insert("step_id".to_string(), Value::String(f.step_id.clone()));
                m.insert(
                    "finding_type".to_string(),
                    Value::String(f.finding_type.clone()),
                );
                m.insert("severity".to_string(), Value::String(f.severity.clone()));
                m.insert("status".to_string(), Value::String(f.status.clone()));
                m.insert("message".to_string(), Value::String(f.message.clone()));
                m.insert("source".to_string(), Value::String(f.source.clone()));
                Value::Object(m)
            })
            .collect();
        res.insert("quality_findings".to_string(), Value::Array(findings));
        res.insert(
            "quality_decision".to_string(),
            Value::String(result.quality_decision.clone()),
        );
        res.insert(
            "next_action".to_string(),
            Value::String(result.next_action.clone()),
        );
        res.insert("auto".to_string(), Value::Bool(result.auto));
        res.insert("success".to_string(), Value::Bool(result.success));
        res.insert("reason".to_string(), Value::String(result.reason.clone()));
        res.insert("error".to_string(), Value::String(result.error.clone()));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

}
