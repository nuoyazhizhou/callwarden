//! 任务生命周期操作域：rollback 与 reopen。
//! 保留原有权限、lease、状态事件和事务语义。

use super::*;

impl TaskCollabStore {
    pub fn handle_task_rollback(
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
            .unwrap_or("rollback requested");
        let owner_key = peer.owner_key();
        let ts = task_now_ts();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        let current_status: String = tx
            .query_row(
                "SELECT status FROM tasks WHERE id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .map_err(|_| {
                DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id))
            })?;

        tx.execute(
            "UPDATE tasks SET status = 'reverted', updated_at = ?1 WHERE id = ?2",
            params![ts, task_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_rollback 失败: {}", e)))?;

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'reverted', 'rollback', ?3, ?4, ?5, ?6)",
            params![task_id, current_status, reason, owner_key, seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 task_rollback 事务失败: {}", e))
        })?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String("reverted".to_string()));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_reopen(
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
            .unwrap_or("reopen requested");
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

        let current_status: String = tx
            .query_row(
                "SELECT status FROM tasks WHERE id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .map_err(|_| {
                DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id))
            })?;

        tx.execute(
            "UPDATE tasks SET status = 'in_progress', updated_at = ?1 WHERE id = ?2",
            params![ts, task_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_reopen 失败: {}", e)))?;

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity, role, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'in_progress', 'reopened', ?3, ?4, ?5, ?6, ?7)",
            params![task_id, current_status, reason, owner_key,
                    identity.as_ref().map(|id| id.role.as_str()).unwrap_or(""), seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        if let Some(ref id) = identity {
            record_action_identity(&tx, task_id, id, "state_transition", seq, ts)?;
        }

        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 task_reopen 事务失败: {}", e))
        })?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert(
            "status".to_string(),
            Value::String("in_progress".to_string()),
        );
        res.insert("previous_status".to_string(), Value::String(current_status));
        res.insert(
            "reopened_at".to_string(),
            Value::Number(serde_json::Number::from_f64(ts).unwrap()),
        );
        res.insert("reviewer".to_string(), Value::String(reviewer.to_string()));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    /// 强制解析 lease 受保护写凭证（task.apply / task.close / task.supersede 门禁）。
    ///
    /// daemon 权威路径下 apply/close/supersede 必须持有完整 reviewer lease 凭证。
    /// 缺少 lease_token 或 fencing_counter，或只提供其一，一律 fail-closed 返回
    /// E_LEASE_REQUIRED；禁止再沿用旧版"缺凭证即跳过校验"的兼容行为。
    pub(crate) fn require_lease_params(params: &Value) -> Result<(String, i64), DaemonRpcError> {
        let token = params.get("lease_token").and_then(|v| v.as_str());
        let counter = params.get("fencing_counter").and_then(|v| v.as_i64());
        match (token, counter) {
            (Some(t), Some(c)) if !t.is_empty() => Ok((t.to_string(), c)),
            _ => Err(DaemonRpcError::new(
                "E_LEASE_REQUIRED",
                "task.apply/task.close 必须携带完整 reviewer lease 凭证（lease_token + fencing_counter）",
            )),
        }
    }

    /// lease 受保护写校验（Req 11.8-11.9，与 Python `validate_lease_for_mutation` 对齐）。
    ///
    /// 任一校验项失败即返回结构化错误，且**不改变 task data**：
    /// 1. 权威时钟不可用 → E_LEASE_CLOCK_UNAVAILABLE（fail-closed，禁止降级为无凭证写）
    /// 2. 无 active lease → E_LEASE_NOT_FOUND
    /// 3. token hash 不匹配 → E_LEASE_TOKEN_MISMATCH
    /// 4. 已过期（Authoritative_Clock）→ E_LEASE_EXPIRED
    /// 5. fencing counter 不一致 → E_LEASE_FENCING_STALE
    /// 6. holder Identity 不一致（提供时）→ E_LEASE_HOLDER_MISMATCH
    pub(crate) fn validate_lease_for_mutation(
        &self,
        conn: &Connection,
        task_id: &str,
        role: &str,
        token: &str,
        fencing_counter: i64,
        identity: Option<&ActionIdentity>,
    ) -> Result<(), DaemonRpcError> {
        // 1. 权威时钟 fail-closed：store 未注入时钟时直接拒绝，绝不让步
        let clock = self.clock.as_ref().ok_or_else(|| {
            DaemonRpcError::new(
                "E_LEASE_CLOCK_UNAVAILABLE",
                format!(
                    "lease clock 不可用，受保护写操作拒绝（task={} role={}）",
                    task_id, role
                ),
            )
        })?;
        let now = clock.now_secs() as f64;

        // 2. 查找 active lease（同一 task+role 只允许一个 active lease，按获取先后取最早）
        let lease = conn.query_row(
            "SELECT lease_id, token_hash, fencing_counter, expires_at, agent_id, session_id, model_id
             FROM task_leases
             WHERE task_id = ?1 AND role = ?2 AND status = 'active'
             ORDER BY id ASC LIMIT 1",
            params![task_id, role],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, f64>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, String>(5)?,
                    r.get::<_, String>(6)?,
                ))
            },
        );
        let (lease_id, token_hash, active_counter, expires_at, agent_id, session_id, model_id) =
            match lease {
                Ok(v) => v,
                Err(_) => {
                    return Err(DaemonRpcError::new(
                        "E_LEASE_NOT_FOUND",
                        format!(
                            "task={} role={} 无 active lease，受保护写操作需要先 acquire_lease",
                            task_id, role
                        ),
                    ))
                }
            };

        // 3. token hash 匹配（Req 11.2：数据库只存 sha256，永不存 raw token）
        if sha256_hex(token.as_bytes()) != token_hash {
            return Err(DaemonRpcError::new(
                "E_LEASE_TOKEN_MISMATCH",
                format!("token hash 不匹配 (lease_id={})", lease_id),
            ));
        }

        // 4. 未过期（Authoritative_Clock，Req 11.4）
        if now > expires_at {
            return Err(DaemonRpcError::new(
                "E_LEASE_EXPIRED",
                format!(
                    "lease {} 已过期 (expires_at={:.1}, now={:.1})",
                    lease_id, expires_at, now
                ),
            ));
        }

        // 5. fencing counter 等于当前 counter（Property 11）
        if fencing_counter != active_counter {
            return Err(DaemonRpcError::new(
                "E_LEASE_FENCING_STALE",
                format!(
                    "fencing counter {} != 当前 {}；旧持有者写入被拒绝",
                    fencing_counter, active_counter
                ),
            ));
        }

        // 6. holder Identity 匹配（提供时校验，Req 11.2）
        if let Some(id) = identity {
            if id.agent_id != agent_id || id.session_id != session_id || id.model_id != model_id {
                return Err(DaemonRpcError::new(
                    "E_LEASE_HOLDER_MISMATCH",
                    format!("holder Identity 与 lease ({}) 不一致", lease_id),
                ));
            }
        }

        Ok(())
    }

    /// P0-E：治理写入中的 reviewer lease 由独立 Reviewer 持有、由独立
    /// Adjudicator 执行。此方法不得用于普通 mutation：普通 mutation 仍使用
    /// validate_lease_for_mutation 的同一 holder 语义。
    pub(crate) fn validate_reviewer_lease_for_adjudication(
        &self,
        conn: &Connection,
        task_id: &str,
        token: &str,
        fencing_counter: i64,
        adjudicator: &ActionIdentity,
    ) -> Result<(), DaemonRpcError> {
        if adjudicator.role != "adjudicator" {
            return Err(DaemonRpcError::new(
                "E_GOVERNANCE_ADJUDICATOR_ROLE_REQUIRED",
                format!(
                    "跨角色 reviewer lease 仅允许 adjudicator，实际 role={}",
                    adjudicator.role
                ),
            ));
        }
        let clock = self.clock.as_ref().ok_or_else(|| {
            DaemonRpcError::new(
                "E_LEASE_CLOCK_UNAVAILABLE",
                format!("lease clock 不可用，治理写操作拒绝（task={}）", task_id),
            )
        })?;
        let (
            lease_id,
            token_hash,
            active_counter,
            expires_at,
            reviewer_agent_id,
            reviewer_session_id,
        ): (String, String, i64, f64, String, String) = conn
            .query_row(
                "SELECT lease_id, token_hash, fencing_counter, expires_at, agent_id, session_id \
             FROM task_leases WHERE task_id=?1 AND role='reviewer' AND status='active' \
             ORDER BY id ASC LIMIT 1",
                params![task_id],
                |r| {
                    Ok((
                        r.get(0)?,
                        r.get(1)?,
                        r.get(2)?,
                        r.get(3)?,
                        r.get(4)?,
                        r.get(5)?,
                    ))
                },
            )
            .map_err(|_| {
                DaemonRpcError::new(
                    "E_LEASE_NOT_FOUND",
                    format!(
                        "task={} 无 active reviewer lease，治理写操作必须先独立 review",
                        task_id
                    ),
                )
            })?;
        if sha256_hex(token.as_bytes()) != token_hash {
            return Err(DaemonRpcError::new(
                "E_LEASE_TOKEN_MISMATCH",
                format!("token hash 不匹配 (lease_id={})", lease_id),
            ));
        }
        if clock.now_secs() as f64 > expires_at {
            return Err(DaemonRpcError::new(
                "E_LEASE_EXPIRED",
                format!("lease {} 已过期", lease_id),
            ));
        }
        if fencing_counter != active_counter {
            return Err(DaemonRpcError::new(
                "E_LEASE_FENCING_STALE",
                format!(
                    "fencing counter {} != 当前 {}；旧 holder 写入被拒绝",
                    fencing_counter, active_counter
                ),
            ));
        }
        let (reviewer_instance_id, registered_session_id, registered_role, registered_status): (String, String, String, String) = conn.query_row(
            "SELECT agent_instance_id, session_id, role, status FROM agent_registrations WHERE agent_id=?1",
            params![reviewer_agent_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        ).map_err(|_| DaemonRpcError::new(
            "E_GOVERNANCE_REVIEWER_UNREGISTERED",
            format!("reviewer lease holder {} 未注册", reviewer_agent_id),
        ))?;
        if registered_status != "active"
            || registered_role != "reviewer"
            || registered_session_id != reviewer_session_id
        {
            return Err(DaemonRpcError::new(
                "E_GOVERNANCE_REVIEWER_INVALID",
                format!(
                    "reviewer lease holder {} 必须为 active registered reviewer 且 session 一致",
                    reviewer_agent_id
                ),
            ));
        }
        if adjudicator.agent_id == reviewer_agent_id {
            return Err(DaemonRpcError::new(
                "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_AGENT",
                "Adjudicator 不得等于 reviewer lease holder agent_id",
            ));
        }
        if !reviewer_instance_id.is_empty() && reviewer_instance_id == adjudicator.agent_instance_id
        {
            return Err(DaemonRpcError::new(
                "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_INSTANCE",
                "Adjudicator 不得等于 reviewer lease holder agent_instance_id",
            ));
        }
        if adjudicator.session_id == reviewer_session_id {
            return Err(DaemonRpcError::new(
                "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_SESSION",
                "Adjudicator 不得等于 reviewer lease holder session_id",
            ));
        }
        Ok(())
    }

    /// P0-L R2：server-side reviewer proof 校验（role_worker_v1 治理写路径）。
    ///
    /// adjudicator Role Worker 请求只携带 `reviewer_lease_id` + `fencing_counter`
    /// （daemon 存储、capability-scoped、不可导出的 reference），raw reviewer lease
    /// token 永不进入请求/日志/账本。本方法在同一事务内按 lease_id 查找并逐项核验：
    /// 1. 权威时钟可用（fail-closed，禁止降级）；
    /// 2. lease 存在、属于本任务、未过期、fencing 一致；
    /// 3. holder 为 active registered reviewer（注册 session 与 lease 一致）；
    /// 4. 与执行裁决的 Role Worker 分离（agent/instance/session 三项不等）。
    pub(crate) fn validate_reviewer_lease_proof_server_side(
        &self,
        conn: &Connection,
        task_id: &str,
        reviewer_lease_id: &str,
        fencing_counter: i64,
        adjudicator: &RoleWorkerAuth,
    ) -> Result<(), DaemonRpcError> {
        let clock = self.clock.as_ref().ok_or_else(|| {
            DaemonRpcError::new(
                "E_LEASE_CLOCK_UNAVAILABLE",
                format!("lease clock 不可用，治理写操作拒绝（task={}）", task_id),
            )
        })?;
        let (lease_task_id, active_counter, expires_at, reviewer_agent_id, reviewer_session_id): (
            String,
            i64,
            f64,
            String,
            String,
        ) = conn
            .query_row(
                "SELECT task_id, fencing_counter, expires_at, agent_id, session_id \
                 FROM task_leases WHERE lease_id=?1 AND role='reviewer' AND status='active'",
                params![reviewer_lease_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
            )
            .map_err(|_| {
                DaemonRpcError::new(
                    "E_REVIEWER_PROOF_LEASE_NOT_FOUND",
                    format!(
                        "reviewer_lease_id={} 无 active reviewer lease，治理写操作必须先独立 review",
                        reviewer_lease_id
                    ),
                )
            })?;
        if lease_task_id != task_id {
            return Err(DaemonRpcError::new(
                "E_REVIEWER_PROOF_TASK_MISMATCH",
                format!(
                    "reviewer lease {} 属于任务 {}，与当前任务 {} 不符",
                    reviewer_lease_id, lease_task_id, task_id
                ),
            ));
        }
        if clock.now_secs() as f64 > expires_at {
            return Err(DaemonRpcError::new(
                "E_LEASE_EXPIRED",
                format!("lease {} 已过期", reviewer_lease_id),
            ));
        }
        if fencing_counter != active_counter {
            return Err(DaemonRpcError::new(
                "E_LEASE_FENCING_STALE",
                format!(
                    "fencing counter {} != 当前 {}；旧 holder 写入被拒绝",
                    fencing_counter, active_counter
                ),
            ));
        }
        let (reviewer_instance_id, registered_session_id, registered_role, registered_status): (String, String, String, String) = conn.query_row(
            "SELECT agent_instance_id, session_id, role, status FROM agent_registrations WHERE agent_id=?1",
            params![reviewer_agent_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        ).map_err(|_| DaemonRpcError::new(
            "E_GOVERNANCE_REVIEWER_UNREGISTERED",
            format!("reviewer lease holder {} 未注册", reviewer_agent_id),
        ))?;
        if registered_status != "active"
            || registered_role != "reviewer"
            || registered_session_id != reviewer_session_id
        {
            return Err(DaemonRpcError::new(
                "E_GOVERNANCE_REVIEWER_INVALID",
                format!(
                    "reviewer lease holder {} 必须为 active registered reviewer 且 session 一致",
                    reviewer_agent_id
                ),
            ));
        }
        // 分离：执行裁决的 adjudicator Role Worker 不得与 reviewer lease holder 同源。
        if adjudicator.role_worker_id == reviewer_agent_id {
            return Err(DaemonRpcError::new(
                "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_AGENT",
                "Adjudicator Role Worker 不得等于 reviewer lease holder agent_id",
            ));
        }
        if !reviewer_instance_id.is_empty() && reviewer_instance_id == adjudicator.role_instance_id
        {
            return Err(DaemonRpcError::new(
                "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_INSTANCE",
                "Adjudicator Role Worker 不得等于 reviewer lease holder agent_instance_id",
            ));
        }
        if adjudicator.role_session_id == reviewer_session_id {
            return Err(DaemonRpcError::new(
                "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_SESSION",
                "Adjudicator Role Worker 不得等于 reviewer lease holder session_id",
            ));
        }
        Ok(())
    }

}
