//! 合同、身份策略与治理投影 domain。
//!
//! 保持既有 `TaskCollabStore::handle_task_contract_*` RPC 方法签名与事务语义；
//! 共享状态和基础 helper 由父 `task_collab` 模块提供。

use super::*;
use crate::daemon::task_loop::task_contract_bootstrap::bind_step_to_executor_role_contract;

impl TaskCollabStore {
    /// P0-L 自举修复：正式 Reviewer 提交 `reviewer_blocked` 后，daemon 在同一
    /// 主任务内追加唯一 P0-L.0 `fix_defect` step（manifest p0l_v3.1 pre-import
    /// gate4 的前置能力）。
    ///
    /// 语义与 `task.handoff(reviewer_blocked)` 的 remediation 分支一致，但作为
    /// 独立受限入口，满足 P0-L 自举任务的受权收口：
    /// - 必须由持有该任务 reviewer lease（token+fencing_counter）的正式 Reviewer
    ///   调用（普通客户端无法伪造 lease，fail-closed）；
    /// - 自动从 Verdict Ledger 绑定最新 block verdict 及其 findings，并校验
    ///   verdict reviewer 与调用者 identity 一致（防冒用）；
    /// - 同一 verdict 只产生一个 fix_defect（唯一性）；request_id 幂等重放
    ///   （daemon 重启后重复请求不重复创建 step）；
    /// - 绑定 task_id / verdict / findings / evidence / workspace binding；
    /// - 不得创建 child task，不得修改被审步骤或历史 verdict。
    pub fn handle_p0l_reviewer_block_repair(
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
                        "E_P0L_REPAIR_PARAM_REQUIRED",
                        format!("task.p0l_reviewer_block_repair 缺少结构化字段 {name}"),
                    )
                })
        };
        let task_id = text_field("task_id")?;
        let request_id = text_field("request_id")?;
        let identity = parse_action_identity(params)?.ok_or_else(|| {
            DaemonRpcError::new(
                "E_P0L_REPAIR_IDENTITY_REQUIRED",
                "task.p0l_reviewer_block_repair 必须携带完整 identity",
            )
        })?;
        if identity.role != "reviewer" && identity.role != "independent_reviewer" {
            return Err(DaemonRpcError::new(
                "E_P0L_REPAIR_REVIEWER_ROLE_REQUIRED",
                format!(
                    "仅允许 role=reviewer/independent_reviewer，实际 role={}",
                    identity.role
                ),
            ));
        }
        let (token, counter) = Self::require_lease_params(params)?;
        let evidence_path = params
            .get("evidence_path")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string())
            .unwrap_or_default();
        let evidence_hash = params
            .get("evidence_hash")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string())
            .unwrap_or_default();
        let requested_workspace = optional_workspace_id_param(params);

        let owner_key = peer.owner_key();
        let ts = task_now_ts();
        let mut conn = self.conn.lock().unwrap();
        let tx = conn.unchecked_transaction().map_err(|e| {
            DaemonRpcError::internal_error(format!("开启 P0-L repair 事务失败: {e}"))
        })?;

        // 权限：受保护 mutation——同一事务内重验 reviewer lease 与 fencing。
        self.validate_lease_for_mutation(
            &tx,
            &task_id,
            identity.role.as_str(),
            &token,
            counter,
            Some(&identity),
        )?;

        let current_status: String = tx
            .query_row(
                "SELECT status FROM tasks WHERE id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .map_err(|_| {
                DaemonRpcError::new("task_not_found", format!("任务不存在: {task_id}"))
            })?;

        // workspace binding：任务必须已有权威 binding；若调用方携带 workspace_id
        // 则必须一致（防止跨 workspace 误修）。
        let bound_workspace = task_bound_workspace_id(&tx, &task_id, requested_workspace)?;

        // request_id 幂等重放：daemon 重启后重复请求不得重复创建 step。
        let request_fingerprint = sha256_hex(
            serde_json::json!({
                "task_id": task_id,
                "source_outcome": "reviewer_blocked",
                "evidence_path": evidence_path,
                "evidence_hash": evidence_hash,
            })
            .to_string()
            .as_bytes(),
        );
        let mut replay_stmt = tx
            .prepare(
                "SELECT reason FROM task_events
                 WHERE task_id = ?1 AND reason_code = 'p0l_repair_created'
                 ORDER BY event_id DESC",
            )
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 P0-L repair ledger 失败: {e}"))
            })?;
        let replay_rows = replay_stmt
            .query_map(params![task_id], |row| row.get::<_, String>(0))
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 P0-L repair ledger 失败: {e}"))
            })?;
        for row in replay_rows {
            let raw = row
                .map_err(|e| DaemonRpcError::internal_error(format!("读取 P0-L repair event 失败: {e}")))?;
            let value = serde_json::from_str::<Value>(&raw).unwrap_or(Value::Null);
            if value.get("request_id").and_then(Value::as_str) == Some(request_id.as_str()) {
                if value.get("request_fingerprint").and_then(Value::as_str)
                    != Some(request_fingerprint.as_str())
                {
                    return Err(DaemonRpcError::new(
                        "E_REQUEST_ID_REUSE_MISMATCH",
                        "P0-L repair request_id 参数冲突",
                    ));
                }
                let mut replay = Map::new();
                replay.insert("ok".into(), Value::Bool(true));
                replay.insert("task_id".into(), Value::String(task_id.clone()));
                replay.insert(
                    "verdict_id".into(),
                    value.get("verdict_id").cloned().unwrap_or(Value::Null),
                );
                replay.insert(
                    "remediation_step_id".into(),
                    value.get("remediation_step_id").cloned().unwrap_or(Value::Null),
                );
                replay.insert("lifecycle_status".into(), Value::String("in_progress".into()));
                replay.insert(
                    "workflow_status".into(),
                    Value::String("remediation_pending".into()),
                );
                replay.insert("replayed".into(), Value::Bool(true));
                return Ok(Value::Object(replay));
            }
        }
        drop(replay_stmt);

        // 状态门禁：仅 review 态可进入整改（重放已在上面处理）。
        if current_status != "review" {
            return Err(DaemonRpcError::new(
                "E_P0L_REPAIR_REVIEW_STATE_REQUIRED",
                "task.p0l_reviewer_block_repair 仅允许从 review 状态追加 P0-L.0 整改",
            ));
        }

        // 从 Verdict Ledger 绑定最新 block verdict 及其 findings。
        let (source_verdict_id, source_findings_raw, source_reviewer_raw): (String, String, String) =
            tx.query_row(
                "SELECT verdict_id, findings, reviewer_identity
                 FROM task_verdict_events
                 WHERE task_id = ?1 AND overall = 'block'
                 ORDER BY id DESC LIMIT 1",
                params![task_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .map_err(|_| {
                DaemonRpcError::new(
                    "E_P0L_REPAIR_VERDICT_REQUIRED",
                    "task.p0l_reviewer_block_repair 必须绑定当前任务的权威 block verdict",
                )
            })?;
        let source_reviewer =
            serde_json::from_str::<Value>(&source_reviewer_raw).unwrap_or(Value::Null);
        let verdict_agent = source_reviewer
            .get("identity")
            .and_then(|item| item.get("agent_id"))
            .and_then(Value::as_str)
            .unwrap_or("");
        let verdict_session = source_reviewer
            .get("identity")
            .and_then(|item| item.get("session_id"))
            .and_then(Value::as_str)
            .unwrap_or("");
        if verdict_agent != identity.agent_id || verdict_session != identity.session_id {
            return Err(DaemonRpcError::new(
                "E_P0L_REPAIR_VERDICT_IDENTITY_MISMATCH",
                "调用 Reviewer 与 source block verdict identity 不一致",
            ));
        }
        let source_findings = serde_json::from_str::<Value>(&source_findings_raw)
            .ok()
            .filter(Value::is_array)
            .filter(|value| {
                value
                    .as_array()
                    .map(|items| !items.is_empty())
                    .unwrap_or(false)
            })
            .ok_or_else(|| {
                DaemonRpcError::new(
                    "E_P0L_REPAIR_FINDINGS_REQUIRED",
                    "block verdict 必须携带至少一个结构化 finding",
                )
            })?;

        // 唯一性：同一 verdict 只产生一个 fix_defect step。
        let mut existing_steps = tx
            .prepare(
                "SELECT id, result FROM task_steps
                 WHERE task_id = ?1 AND action = 'fix_defect'
                 ORDER BY step_index ASC",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 P0-L repair step 失败: {e}")))?;
        let step_rows = existing_steps
            .query_map(params![task_id], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("读取 P0-L repair step 失败: {e}")))?;
        for row in step_rows {
            let (_existing_id, raw) = row
                .map_err(|e| DaemonRpcError::internal_error(format!("读取 P0-L repair step 失败: {e}")))?;
            let value = serde_json::from_str::<Value>(&raw).unwrap_or(Value::Null);
            if value.get("source_verdict_id").and_then(Value::as_str)
                == Some(source_verdict_id.as_str())
            {
                return Err(DaemonRpcError::new(
                    "E_P0L_REPAIR_ALREADY_EXISTS",
                    "该 block verdict 已在当前主任务创建 P0-L.0 整改",
                ));
            }
        }
        drop(existing_steps);

        // 创建唯一 P0-L.0 fix_defect step（绑定 verdict/findings/evidence/workspace）。
        let remediation_step_id = format!(
            "S-{}",
            &sha256_hex(
                format!(
                    "{}:{}:{}:{}",
                    task_id, source_verdict_id, "task-level", request_id
                )
                .as_bytes()
            )[..24]
        );
        let max_idx: i64 = tx
            .query_row(
                "SELECT COALESCE(MAX(step_index), -1) FROM task_steps WHERE task_id = ?1",
                params![task_id],
                |row| row.get(0),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 step_index 失败: {e}")))?;
        let metadata = serde_json::json!({
            "remediation_of_step_id": Value::Null,
            "source_outcome": "reviewer_blocked",
            "source_verdict_id": source_verdict_id,
            "source_findings": source_findings,
            "source_handoff_request_id": request_id,
            "evidence_path": evidence_path,
            "evidence_hash": evidence_hash,
            "workspace_id": bound_workspace,
        })
        .to_string();
        tx.execute(
            "INSERT INTO task_steps
             (id, task_id, step_index, action, target_file, target_symbol,
              check_items, status, result, created_at, completed_at)
             VALUES (?1, ?2, ?3, 'fix_defect', '', '', '', 'pending', ?4, ?5, NULL)",
            params![remediation_step_id, task_id, max_idx + 1, metadata, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("追加 P0-L.0 fix_defect 失败: {e}")))?;
        bind_step_to_executor_role_contract(&tx, &task_id, &remediation_step_id, &owner_key)?;
        tx.execute(
            "UPDATE tasks SET status = 'in_progress', updated_at = ?1 WHERE id = ?2",
            params![ts, task_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("reopen 主任务失败: {e}")))?;

        // 审计事件（含幂等指纹）。
        let seq = self.next_seq();
        let audit = serde_json::json!({
            "task_id": task_id,
            "request_id": request_id,
            "request_fingerprint": request_fingerprint,
            "source_verdict_id": source_verdict_id,
            "remediation_step_id": remediation_step_id,
            "workspace_id": bound_workspace,
        })
        .to_string();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity,
              role, monotonic_seq, authoritative_timestamp, workspace_id)
             VALUES (?1, 'review', 'in_progress', 'p0l_repair_created', ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                task_id, audit, owner_key, identity.role,
                seq, ts, bound_workspace
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("P0-L repair 审计写入失败: {e}")))?;

        // 入 assignment queue（executor 领取 P0-L.0）。
        assignment_queue::queue_assignment(
            &tx,
            &task_id,
            Some(&remediation_step_id),
            "executor",
            &request_id,
            None,
            &owner_key,
            &identity.session_id,
            self.next_seq(),
            ts,
        )?;

        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 P0-L repair 事务失败: {e}"))
        })?;

        let mut res = Map::new();
        res.insert("ok".into(), Value::Bool(true));
        res.insert("task_id".into(), Value::String(task_id));
        res.insert("verdict_id".into(), Value::String(source_verdict_id));
        res.insert("remediation_step_id".into(), Value::String(remediation_step_id));
        res.insert("lifecycle_status".into(), Value::String("in_progress".into()));
        res.insert("workflow_status".into(), Value::String("remediation_pending".into()));
        res.insert("replayed".into(), Value::Bool(false));
        Ok(Value::Object(res))
    }

    /// A3：更新任务某角色的 Role Contract——旧版置 is_current=0，写入新 revision，
    /// 并追加 task_event 审计（合同变更必须生成新版本 + 审计事件）。
    pub fn handle_task_contract_set(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let contract = params
            .get("contract")
            .and_then(|v| v.as_object())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 contract 对象"))?;
        let role = contract
            .get("role")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        if role.is_empty() {
            return Err(DaemonRpcError::invalid_params("contract.role 不能为空"));
        }

        let owner_key = peer.owner_key();
        let ts = task_now_ts();
        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        // 权限：creator / 当前 claimer / root
        let (creator, _status): (String, String) = tx
            .query_row(
                "SELECT creator, status FROM tasks WHERE id = ?1",
                params![task_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .map_err(|_| {
                DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id))
            })?;
        let (claimed_actor, _) = self.get_task_claim_info(&tx, task_id);
        if creator != owner_key
            && claimed_actor.as_deref() != Some(&owner_key)
            && owner_key != "root"
        {
            return Err(DaemonRpcError::permission_denied(format!(
                "没有对任务 {} 更新 Role Contract 的权限",
                task_id
            )));
        }

        // 旧版置非当前，并计算新 revision
        tx.execute(
            "UPDATE role_contracts SET is_current = 0 WHERE task_id = ?1 AND role = ?2 AND is_current = 1",
            params![task_id, role],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("role_contract 旧版下线失败: {}", e)))?;
        let next_revision: i64 = tx
            .query_row(
                "SELECT COALESCE(MAX(revision), 0) + 1 FROM role_contracts WHERE task_id = ?1 AND role = ?2",
                params![task_id, role],
                |r| r.get(0),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("查询合同 revision 失败: {}", e)))?;
        let contract_id = format!("RC-{}-{}-r{}", task_id, role, next_revision);

        tx.execute(
            "INSERT INTO role_contracts
             (contract_id, task_id, step_id, role, skill_id, skill_version,
              prompt_template_id, prompt_hash, allowed_paths, forbidden_paths,
              commands, acceptance_checks, required_evidence, handoff_to,
              independence, revision, is_current, created_at, created_by)
             VALUES (?1, ?2, '', ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, 1, ?16, ?17)",
            params![
                contract_id, task_id, role,
                contract_field(contract, "skill_id"),
                contract_field(contract, "skill_version"),
                contract_field(contract, "prompt_template_id"),
                contract_field(contract, "prompt_hash"),
                contract_field(contract, "allowed_paths"),
                contract_field(contract, "forbidden_paths"),
                contract_field(contract, "commands"),
                contract_field(contract, "acceptance_checks"),
                contract_field(contract, "required_evidence"),
                contract_field(contract, "handoff_to"),
                contract_field(contract, "independence"),
                next_revision, ts, owner_key,
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("role_contract 写入失败: {}", e)))?;

        // 审计事件：合同变更
        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, '', '', 'contract_set', ?2, ?3, ?4, ?5)",
            params![task_id, format!("role_contract {} revision {} 更新", role, next_revision), owner_key, seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 task_contract_set 事务失败: {}", e))
        })?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("role".to_string(), Value::String(role));
        res.insert("contract_id".to_string(), Value::String(contract_id));
        res.insert(
            "revision".to_string(),
            Value::Number(serde_json::Number::from(next_revision)),
        );
        res.insert("status".to_string(), Value::String("frozen".to_string()));
        Ok(Value::Object(res))
    }

    /// A3：读取任务当前冻结的 Role Contract 列表（可指定 role 过滤）。
    /// P0-C：对治理投影完全缺失但已绑定 authority 的历史/自举任务追加 v1 Task
    /// Contract、三角色 lineage/revision 和 executor step bindings。该入口绝不更新
    /// 任何既有 projection；一次成功必须由 adjudicator reviewer lease/fencing 与 evidence
    /// 证明，且经 operation ledger 保证持久幂等。
    pub fn handle_task_contract_bootstrap(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        let request_id = params
            .get("request_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        let workspace_instance_id = params
            .get("workspace_instance_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        let workspace_id = optional_workspace_id_param(params).ok_or_else(|| {
            DaemonRpcError::new(
                "E_TASK_CONTRACT_BOOTSTRAP_WORKSPACE_REQUIRED",
                "task.contract_bootstrap 必须携带 workspace_id > 0",
            )
        })?;
        if task_id.is_empty() || request_id.is_empty() || workspace_instance_id.is_empty() {
            return Err(DaemonRpcError::new(
                "E_TASK_CONTRACT_BOOTSTRAP_PARAMS_REQUIRED",
                "task.contract_bootstrap 必须携带 task_id、request_id、workspace_instance_id",
            ));
        }
        let envelope = params.get("envelope").cloned().ok_or_else(|| {
            DaemonRpcError::new(
                "E_TASK_CONTRACT_BOOTSTRAP_ENVELOPE_REQUIRED",
                "task.contract_bootstrap 必须携带 envelope",
            )
        })?;
        let identity = parse_action_identity(params)?.ok_or_else(|| {
            DaemonRpcError::new(
                "E_TASK_CONTRACT_BOOTSTRAP_IDENTITY_REQUIRED",
                "task.contract_bootstrap 必须携带完整 identity",
            )
        })?;
        if identity.role != "adjudicator" {
            return Err(DaemonRpcError::new(
                "E_TASK_CONTRACT_BOOTSTRAP_ROLE_REQUIRED",
                format!("仅允许 role=adjudicator，实际 role={}", identity.role),
            ));
        }
        let (token, counter) = Self::require_lease_params(params)?;
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
                "E_TASK_CONTRACT_BOOTSTRAP_EVIDENCE_REQUIRED",
                "task.contract_bootstrap 必须携带 evidence_path 与 evidence_hash",
            ));
        }

        let method = "task.contract_bootstrap";
        let operation_store = OperationStore;
        let mut conn = self.conn.lock().unwrap();
        let dedupe =
            operation_store.dedupe(&conn, workspace_instance_id, method, request_id, params)?;
        let (rules, canonical_params_hash): (ParamsRules, String) = match dedupe {
            DedupeOutcome::Replay {
                response_or_error_json,
            } => {
                if let Some(err) = response_or_error_json.get("error") {
                    let code = err
                        .get("code")
                        .and_then(|v| v.as_str())
                        .unwrap_or("E_TASK_CONTRACT_BOOTSTRAP_REPLAY_ERROR");
                    let message = err
                        .get("message")
                        .and_then(|v| v.as_str())
                        .unwrap_or("bootstrap deterministic rejection");
                    return Err(DaemonRpcError::new(code, message));
                }
                return Ok(response_or_error_json);
            }
            DedupeOutcome::FirstRequest {
                rules,
                canonical_params_hash,
            } => (rules, canonical_params_hash),
        };
        let tx = conn.unchecked_transaction().map_err(|e| {
            DaemonRpcError::internal_error(format!("开启 task contract bootstrap 事务失败: {e}"))
        })?;
        let provenance = LedgerProvenance {
            workspace_id: Some(workspace_id),
            task_id: Some(task_id.to_string()),
            ..Default::default()
        };
        let record_reject = |tx: &Transaction<'_>, code: &str, message: &str| -> DaemonRpcError {
            let body = serde_json::json!({"error":{"code":code,"message":message}});
            let _ = operation_store.record_result(
                tx,
                workspace_instance_id,
                method,
                request_id,
                &rules,
                &canonical_params_hash,
                &provenance,
                &body,
            );
            DaemonRpcError::new(code, message)
        };
        macro_rules! reject {
            ($code:expr, $message:expr) => {{
                let err = record_reject(&tx, $code, $message);
                let _ = tx.commit();
                return Err(err);
            }};
        }

        let bound_workspace = match task_bound_workspace_id(&tx, task_id, Some(workspace_id)) {
            Ok(value) => value,
            Err(e) => reject!(&e.code, &e.message),
        };
        let binding_instance: String = match tx.query_row(
            "SELECT c.workspace_instance_id FROM task_workspace_bindings b JOIN workspace_authority_captures c ON c.workspace_capture_id=b.workspace_capture_id WHERE b.task_id=?1 AND b.workspace_id=?2",
            params![task_id, bound_workspace], |r| r.get(0),
        ) {
            Ok(value) => value,
            Err(_) => reject!("E_TASK_CONTRACT_BOOTSTRAP_AUTHORITY_UNAVAILABLE", "task 缺少可复核的 workspace authority capture"),
        };
        if binding_instance != workspace_instance_id {
            reject!(
                "E_WORKSPACE_AUTHORITY_MISMATCH",
                &format!(
                    "task binding workspace_instance_id={} 与请求 {} 不一致",
                    binding_instance, workspace_instance_id
                )
            );
        }
        if let Err(e) = verify_registered_identity(&tx, &identity) {
            reject!(&e.code, &e.message);
        }
        if let Err(e) =
            self.validate_reviewer_lease_for_adjudication(&tx, task_id, &token, counter, &identity)
        {
            let code = if e.code == "E_LEASE_FENCING_STALE" {
                "E_TASK_CONTRACT_BOOTSTRAP_FENCED"
            } else {
                &e.code
            };
            reject!(code, &e.message);
        }
        // P0-L step2：identity policy 路由。无声明槽位或显式 legacy 均保持原路径；
        // role_worker_v1 额外强制 expected adjudicator worker credential（fail-closed）。
        if envelope_has_policy_slot(&envelope) {
            let bootstrap_policy = match parse_identity_policy(&envelope) {
                Ok(value) => value,
                Err(e) => reject!(&e.code, &e.message),
            };
            if bootstrap_policy == POLICY_ROLE_WORKER_V1 {
                if let Err(e) = enforce_role_worker_governance_write(
                    &tx, params, &peer, bound_workspace, task_id, method,
                    "identity_policy=role_worker_v1 的 task.contract_bootstrap 必须携带 role_worker_auth（expected adjudicator worker）",
                ) {
                    reject!(&e.code, &e.message);
                }
            }
        }
        let status: String =
            match tx.query_row("SELECT status FROM tasks WHERE id=?1", [task_id], |r| {
                r.get(0)
            }) {
                Ok(value) => value,
                Err(_) => reject!(
                    "E_TASK_CONTRACT_BOOTSTRAP_TASK_NOT_FOUND",
                    &format!("task 不存在: {task_id}")
                ),
            };
        let input = BootstrapInput {
            task_id: task_id.to_string(),
            envelope,
            created_by: identity.agent_id.clone(),
        };
        let result = match bootstrap_task_governance_contracts(&tx, &input, bound_workspace) {
            Ok(value) => value,
            Err(e) => reject!(&e.code, &e.message),
        };
        let ts = task_now_ts();
        let seq = self.next_seq();
        if let Err(e) = TaskCollabStore::append_task_event(
            &tx, task_id, &status, &status, "task_contract_bootstrapped",
            &serde_json::json!({"request_id":request_id,"evidence_path":evidence_path,"evidence_hash":evidence_hash}).to_string(),
            &identity.agent_id, &identity.session_id, &identity.role, seq, ts,
        ) { return Err(e); }
        if let Err(e) = record_action_identity(&tx, task_id, &identity, method, seq, ts) {
            return Err(e);
        }
        let mut full = result.as_object().cloned().unwrap_or_default();
        full.insert(
            "request_id".to_string(),
            Value::String(request_id.to_string()),
        );
        full.insert(
            "evidence_path".to_string(),
            Value::String(evidence_path.to_string()),
        );
        full.insert(
            "evidence_hash".to_string(),
            Value::String(evidence_hash.to_string()),
        );
        full.insert("fencing_counter".to_string(), Value::Number(counter.into()));
        full.insert("authoritative_timestamp".to_string(), serde_json::json!(ts));
        let full = Value::Object(full);
        operation_store.record_result(
            &tx,
            workspace_instance_id,
            method,
            request_id,
            &rules,
            &canonical_params_hash,
            &provenance,
            &full,
        )?;
        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 task contract bootstrap 事务失败: {e}"))
        })?;
        Ok(full)
    }

    pub fn handle_task_contract_revise(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        let request_id = params
            .get("request_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        let workspace_instance_id = params
            .get("workspace_instance_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        let workspace_id = optional_workspace_id_param(params).ok_or_else(|| {
            DaemonRpcError::new(
                "E_TASK_CONTRACT_REVISE_WORKSPACE_REQUIRED",
                "task.contract_revise 必须携带 workspace_id > 0",
            )
        })?;
        if task_id.is_empty() || request_id.is_empty() || workspace_instance_id.is_empty() {
            return Err(DaemonRpcError::new(
                "E_TASK_CONTRACT_REVISE_PARAMS_REQUIRED",
                "task.contract_revise 必须携带 task_id、request_id、workspace_instance_id",
            ));
        }
        let envelope = params.get("envelope").cloned().ok_or_else(|| {
            DaemonRpcError::new(
                "E_TASK_CONTRACT_REVISE_ENVELOPE_REQUIRED",
                "task.contract_revise 必须携带 envelope",
            )
        })?;
        let expected_previous_hash = params
            .get("expected_previous_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        if expected_previous_hash.is_empty() {
            return Err(DaemonRpcError::new(
                "E_TASK_CONTRACT_REVISE_PREV_HASH_REQUIRED",
                "task.contract_revise 必须携带 expected_previous_hash",
            ));
        }
        // P0-L R1：identity 降级为可选参数——仅当任务不涉及 role_worker_v1（当前与
        // 新声明均非）时，才在事务内恢复 P0-G §3 全部刚性 identity 门禁；worker 路径
        // 下稳定 Role Worker credential 是唯一授权锚点，identity 仅作为 append-only
        // provenance 记录，绝不参与任何授权比较。lease 凭证同样按路径分流（见下）。
        let identity = parse_action_identity(params)?;
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
                "E_TASK_CONTRACT_REVISE_EVIDENCE_REQUIRED",
                "task.contract_revise 必须携带 evidence_path 与 evidence_hash",
            ));
        }

        let method = "task.contract_revise";
        let operation_store = OperationStore;
        let mut conn = self.conn.lock().unwrap();
        let dedupe =
            operation_store.dedupe(&conn, workspace_instance_id, method, request_id, params)?;
        let (rules, canonical_params_hash): (ParamsRules, String) = match dedupe {
            DedupeOutcome::Replay {
                response_or_error_json,
            } => {
                if let Some(err) = response_or_error_json.get("error") {
                    let code = err
                        .get("code")
                        .and_then(|v| v.as_str())
                        .unwrap_or("E_TASK_CONTRACT_REVISE_REPLAY_ERROR");
                    let message = err
                        .get("message")
                        .and_then(|v| v.as_str())
                        .unwrap_or("revise deterministic rejection");
                    return Err(DaemonRpcError::new(code, message));
                }
                return Ok(response_or_error_json);
            }
            DedupeOutcome::FirstRequest {
                rules,
                canonical_params_hash,
            } => (rules, canonical_params_hash),
        };
        let tx = conn.unchecked_transaction().map_err(|e| {
            DaemonRpcError::internal_error(format!("开启 task contract revise 事务失败: {e}"))
        })?;
        let provenance = LedgerProvenance {
            workspace_id: Some(workspace_id),
            task_id: Some(task_id.to_string()),
            ..Default::default()
        };
        let record_reject = |tx: &Transaction<'_>, code: &str, message: &str| -> DaemonRpcError {
            let body = serde_json::json!({"error":{"code":code,"message":message}});
            let _ = operation_store.record_result(
                tx,
                workspace_instance_id,
                method,
                request_id,
                &rules,
                &canonical_params_hash,
                &provenance,
                &body,
            );
            DaemonRpcError::new(code, message)
        };
        macro_rules! reject {
            ($code:expr, $message:expr) => {{
                let err = record_reject(&tx, $code, $message);
                let _ = tx.commit();
                return Err(err);
            }};
        }

        let bound_workspace = match task_bound_workspace_id(&tx, task_id, Some(workspace_id)) {
            Ok(value) => value,
            Err(e) => reject!(&e.code, &e.message),
        };
        let binding_instance: String = match tx.query_row(
            "SELECT c.workspace_instance_id FROM task_workspace_bindings b JOIN workspace_authority_captures c ON c.workspace_capture_id=b.workspace_capture_id WHERE b.task_id=?1 AND b.workspace_id=?2",
            params![task_id, bound_workspace], |r| r.get(0),
        ) {
            Ok(value) => value,
            Err(_) => reject!("E_TASK_CONTRACT_REVISE_AUTHORITY_UNAVAILABLE", "task 缺少可复核的 workspace authority capture"),
        };
        if binding_instance != workspace_instance_id {
            reject!(
                "E_WORKSPACE_AUTHORITY_MISMATCH",
                &format!(
                    "task binding workspace_instance_id={} 与请求 {} 不一致",
                    binding_instance, workspace_instance_id
                )
            );
        }
        // P0-L step2：revise 的 policy 路由（fail-closed）。
        // - 当前 role_worker_v1：任何修订都必须携带 expected adjudicator worker credential，
        //   禁止显式（新声明 legacy）或隐式（未声明）降级回 legacy；
        // - 新声明 role_worker_v1：generic 旧 revision 的 hash-linked policy 升级同样强制 worker 门禁；
        // - 其余（无槽位或显式 legacy 且当前非 role_worker_v1）：legacy 原路径不变。
        let current_policy = match get_current_task_contract_policy(&tx, task_id) {
            Ok(value) => value,
            Err(e) => reject!(&e.code, &e.message),
        };
        let new_policy: Option<String> = if envelope_has_policy_slot(&envelope) {
            match parse_identity_policy(&envelope) {
                Ok(value) => Some(value),
                Err(e) => reject!(&e.code, &e.message),
            }
        } else {
            None
        };
        let current_is_role_worker = current_policy.as_deref() == Some(POLICY_ROLE_WORKER_V1);
        if current_is_role_worker && new_policy.as_deref() != Some(POLICY_ROLE_WORKER_V1) {
            let downgrade_message = "task 当前 identity_policy=role_worker_v1，修订必须显式声明 role_worker_v1；禁止显式或隐式降级回 legacy";
            reject!(ERR_POLICY_MISMATCH, downgrade_message);
        }
        // P0-L R1/R2：按 policy 分流授权、reviewer proof 与事件归属。
        // - worker 路径：Role Worker credential 是唯一授权锚点（enforce 内以 authority
        //   库登记角色为锚）；reviewer proof 为 server-side reference
        //   （reviewer_lease_id + fencing_counter），请求严禁携带 raw reviewer lease token；
        //   identity（如提供）仅记录 provenance。
        // - legacy 路径：保持 P0-G §3 全部既有刚性门禁（完整 identity + raw lease 凭证）。
        let use_worker_path =
            current_is_role_worker || new_policy.as_deref() == Some(POLICY_ROLE_WORKER_V1);
        let (actor_agent_id, actor_session_id, actor_role, counter): (String, String, String, i64) =
            if use_worker_path {
                // P0-L R2：raw reviewer lease token 不得出现在 worker 路径请求中。
                let raw_token_present = params
                    .get("lease_token")
                    .map(|v| !v.is_null())
                    .unwrap_or(false);
                if raw_token_present {
                    reject!(
                        "E_REVIEWER_PROOF_RAW_TOKEN_FORBIDDEN",
                        "identity_policy=role_worker_v1 的 task.contract_revise 禁止携带 raw reviewer lease token；改用 reviewer_lease_id + fencing_counter（server-side reviewer proof）"
                    );
                }
                let reviewer_lease_id = params
                    .get("reviewer_lease_id")
                    .and_then(|v| v.as_str())
                    .map(str::trim)
                    .filter(|value| !value.is_empty());
                let proof_counter = params.get("fencing_counter").and_then(|v| v.as_i64());
                let (Some(reviewer_lease_id), Some(counter)) = (reviewer_lease_id, proof_counter)
                else {
                    reject!(
                        "E_REVIEWER_PROOF_REQUIRED",
                        "identity_policy=role_worker_v1 的 task.contract_revise 必须携带 reviewer_lease_id + fencing_counter（server-side reviewer proof，禁止 raw token）"
                    );
                };
                let auth = match enforce_role_worker_governance_write(
                    &tx, params, &peer, bound_workspace, task_id, method,
                    "identity_policy=role_worker_v1 的 task.contract_revise 必须携带 role_worker_auth（expected adjudicator worker）",
                ) {
                    Ok(value) => value,
                    Err(e) => reject!(&e.code, &e.message),
                };
                if let Err(e) = self.validate_reviewer_lease_proof_server_side(
                    &tx,
                    task_id,
                    reviewer_lease_id,
                    counter,
                    &auth,
                ) {
                    let code = if e.code == "E_LEASE_FENCING_STALE" {
                        "E_TASK_CONTRACT_REVISE_FENCED"
                    } else {
                        &e.code
                    };
                    reject!(code, &e.message);
                }
                (
                    auth.role_worker_id.clone(),
                    auth.role_session_id.clone(),
                    "adjudicator".to_string(),
                    counter,
                )
            } else {
                // legacy 路径：P0-G §3 刚性门禁原样保留。
                let Some(id) = identity.as_ref() else {
                    reject!(
                        "E_TASK_CONTRACT_REVISE_IDENTITY_REQUIRED",
                        "task.contract_revise 必须携带完整 identity"
                    );
                };
                if id.agent_instance_id.is_empty() {
                    reject!(
                        "E_TASK_CONTRACT_REVISE_IDENTITY_INSTANCE_REQUIRED",
                        "task.contract_revise 必须携带非空 agent_instance_id（P0-G §3 identity 全字段门禁）"
                    );
                }
                if id.role != "adjudicator" {
                    reject!(
                        "E_TASK_CONTRACT_REVISE_ROLE_REQUIRED",
                        &format!("仅允许 role=adjudicator，实际 role={}", id.role)
                    );
                }
                let (token, counter) = match Self::require_lease_params(params) {
                    Ok(value) => value,
                    Err(e) => reject!(&e.code, &e.message),
                };
                if let Err(e) = verify_registered_identity(&tx, id) {
                    reject!(&e.code, &e.message);
                }
                if let Err(e) =
                    self.validate_reviewer_lease_for_adjudication(&tx, task_id, &token, counter, id)
                {
                    let code = if e.code == "E_LEASE_FENCING_STALE" {
                        "E_TASK_CONTRACT_REVISE_FENCED"
                    } else {
                        &e.code
                    };
                    reject!(code, &e.message);
                }
                (
                    id.agent_id.clone(),
                    id.session_id.clone(),
                    id.role.clone(),
                    counter,
                )
            };
        let status: String =
            match tx.query_row("SELECT status FROM tasks WHERE id=?1", [task_id], |r| {
                r.get(0)
            }) {
                Ok(value) => value,
                Err(_) => reject!(
                    "E_TASK_CONTRACT_REVISE_TASK_NOT_FOUND",
                    &format!("task 不存在: {task_id}")
                ),
            };
        let input = ContractReviseInput {
            task_id: task_id.to_string(),
            envelope,
            expected_previous_hash: expected_previous_hash.to_string(),
            created_by: actor_agent_id.clone(),
        };
        let result = match append_task_contract_revision(&tx, &input, bound_workspace) {
            Ok(value) => value,
            Err(e) => reject!(&e.code, &e.message),
        };
        let ts = task_now_ts();
        let seq = self.next_seq();
        if let Err(e) = TaskCollabStore::append_task_event(
            &tx, task_id, &status, &status, "task_contract_revised",
            &serde_json::json!({"request_id":request_id,"evidence_path":evidence_path,"evidence_hash":evidence_hash}).to_string(),
            &actor_agent_id, &actor_session_id, &actor_role, seq, ts,
        ) { return Err(e); }
        // identity（如提供）仅作为 append-only provenance 记录，不参与授权判定。
        if let Some(id) = identity.as_ref() {
            if let Err(e) = record_action_identity(&tx, task_id, id, method, seq, ts) {
                return Err(e);
            }
        }
        let mut full = result.as_object().cloned().unwrap_or_default();
        full.insert(
            "request_id".to_string(),
            Value::String(request_id.to_string()),
        );
        full.insert(
            "evidence_path".to_string(),
            Value::String(evidence_path.to_string()),
        );
        full.insert(
            "evidence_hash".to_string(),
            Value::String(evidence_hash.to_string()),
        );
        full.insert("fencing_counter".to_string(), Value::Number(counter.into()));
        full.insert("authoritative_timestamp".to_string(), serde_json::json!(ts));
        let full = Value::Object(full);
        operation_store.record_result(
            &tx,
            workspace_instance_id,
            method,
            request_id,
            &rules,
            &canonical_params_hash,
            &provenance,
            &full,
        )?;
        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 task contract revise 事务失败: {e}"))
        })?;
        Ok(full)
    }

    pub fn handle_task_contract_get(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let role = params
            .get("role")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();

        let conn = self.conn.lock().unwrap();
        let sql = if role.is_empty() {
            "SELECT role, skill_id, skill_version, prompt_template_id, prompt_hash,
                    allowed_paths, forbidden_paths, commands, acceptance_checks,
                    required_evidence, handoff_to, independence, revision, contract_id, created_by
             FROM role_contracts WHERE task_id = ?1 AND is_current = 1 ORDER BY role ASC"
        } else {
            "SELECT role, skill_id, skill_version, prompt_template_id, prompt_hash,
                    allowed_paths, forbidden_paths, commands, acceptance_checks,
                    required_evidence, handoff_to, independence, revision, contract_id, created_by
             FROM role_contracts WHERE task_id = ?1 AND role = ?2 AND is_current = 1
             ORDER BY revision DESC LIMIT 1"
        };
        let mut stmt = conn.prepare(sql).map_err(|e| {
            DaemonRpcError::internal_error(format!("查询 role_contracts 失败: {}", e))
        })?;
        // 统一映射函数为 fn pointer，避免 if/else 分支闭包类型不一致
        let mapper: for<'r> fn(&rusqlite::Row<'r>) -> rusqlite::Result<Map<String, Value>> =
            contract_row_to_map;
        let rows = if role.is_empty() {
            stmt.query_map(params![task_id], mapper)
        } else {
            stmt.query_map(params![task_id, role], mapper)
        }
        .map_err(|e| DaemonRpcError::internal_error(format!("映射 role_contracts 失败: {}", e)))?;

        let contracts: Vec<Value> = rows.flatten().map(Value::Object).collect();

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("contracts".to_string(), Value::Array(contracts));
        Ok(Value::Object(res))
    }

    /// P0-G G3：只读 governance projection（Reviewer 可用的权威投影）。
    ///
    /// 返回 Task Contract / current step / Reviewer lineage / 审阅输入 / 规则状态 /
    /// 诊断；**绝不返回 lease raw token**。只读方法，无 mutation 门禁。
    pub fn handle_task_governance_projection_get(
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

        // 1) Task Contract：当前 revision + canonical hash + normalization
        let contract = conn
            .query_row(
                "SELECT contract_id, revision, contract_hash, normalization_version, normalization_rules_hash \
                 FROM task_contract_revisions WHERE task_id = ?1 ORDER BY revision DESC LIMIT 1",
                params![task_id],
                |r| Ok(serde_json::json!({
                    "contract_id": r.get::<_, String>(0)?,
                    "revision": r.get::<_, i64>(1)?,
                    "hash": r.get::<_, String>(2)?,
                    "normalization_version": r.get::<_, String>(3)?,
                    "normalization_rules_hash": r.get::<_, String>(4)?,
                })),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 Task Contract 失败: {}", e)))?;

        // 2) 当前步骤 + step-role binding（审阅输入）
        let step = conn
            .query_row(
                "SELECT id, action, target_file FROM task_steps \
                 WHERE task_id = ?1 AND status != 'done' ORDER BY step_index ASC LIMIT 1",
                params![task_id],
                |r| {
                    Ok(serde_json::json!({
                        "step_id": r.get::<_, String>(0)?,
                        "action": r.get::<_, String>(1)?,
                        "target_file": r.get::<_, String>(2)?,
                    }))
                },
            )
            .optional()
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 current step 失败: {}", e))
            })?;

        // 3) Reviewer Role Contract lineage（当前 revision/hash）
        let reviewer_role_contract = conn
            .query_row(
                "SELECT contract_id, revision, prompt_template_id, prompt_hash, independence \
                 FROM role_contracts WHERE task_id = ?1 AND role = 'reviewer' AND is_current = 1 \
                 ORDER BY revision DESC LIMIT 1",
                params![task_id],
                |r| {
                    Ok(serde_json::json!({
                        "contract_id": r.get::<_, String>(0)?,
                        "revision": r.get::<_, i64>(1)?,
                        "prompt_template_id": r.get::<_, String>(2)?,
                        "prompt_hash": r.get::<_, String>(3)?,
                        "independence": r.get::<_, String>(4)?,
                    }))
                },
            )
            .optional()
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 Reviewer Role Contract 失败: {}", e))
            })?;

        // 4) 规则状态（normalization rule set，revoked 视为不可用）
        let normalization_rules = conn
            .query_row(
                "SELECT r.verdict_rule_set_id, r.normalization_version, r.rules_hash, r.authoritative_created_at \
                 FROM verdict_normalization_rules r \
                 LEFT JOIN verdict_normalization_rule_revocations v ON v.verdict_rule_set_id = r.verdict_rule_set_id \
                 WHERE v.verdict_rule_set_id IS NULL \
                 ORDER BY r.authoritative_created_at DESC LIMIT 1",
                [],
                |r| Ok(serde_json::json!({
                    "rule_set_id": r.get::<_, String>(0)?,
                    "version": r.get::<_, String>(1)?,
                    "rules_hash": r.get::<_, String>(2)?,
                    "created_at": r.get::<_, String>(3)?,
                    "revoked": false,
                })),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 normalization rules 失败: {}", e)))?;

        // 5) 诊断：effective verdict IDs + normalized state（不回显 lease raw token）
        let verdicts = {
            let mut stmt = conn
                .prepare(
                    "SELECT verdict_id, overall, normalization_version, submitted_at \
                     FROM task_verdict_events WHERE task_id = ?1 ORDER BY submitted_at DESC LIMIT 5",
                )
                .map_err(|e| DaemonRpcError::internal_error(format!("查询 verdicts 失败: {}", e)))?;
            let rows = stmt
                .query_map(params![task_id], |r| {
                    Ok(serde_json::json!({
                        "verdict_id": r.get::<_, String>(0)?,
                        "overall": r.get::<_, String>(1)?,
                        "normalized_state": r.get::<_, String>(2)?,
                        "submitted_at": r.get::<_, f64>(3)?,
                    }))
                })
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("映射 verdicts 失败: {}", e))
                })?;
            rows.flatten().collect::<Vec<Value>>()
        };

        // 6) 审阅输入：从 task_events 提取最近带 snapshot_id 的事件（无独立
        //    task_snapshots 表；snapshot 引用以 task_events.snapshot_id 承载）。
        let snapshot = conn
            .query_row(
                "SELECT snapshot_id, evidence_path FROM task_events \
                 WHERE task_id = ?1 AND snapshot_id IS NOT NULL AND snapshot_id != '' \
                 ORDER BY monotonic_seq DESC LIMIT 1",
                params![task_id],
                |r| {
                    Ok(serde_json::json!({
                        "snapshot_id": r.get::<_, String>(0)?,
                        "evidence_path": r.get::<_, String>(1)?,
                    }))
                },
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 snapshot 失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        let governance = super::task_collab_query::tree_governance_projection(&conn, task_id, &status);
        res.insert("status".to_string(), Value::String(status));
        for key in [
            "lifecycle_status",
            "workflow_status",
            "current_role",
            "next_role",
            "next_action",
            "review",
            "blocking_reasons",
            "decision",
            "action",
            "required_role",
            "step_id",
        ] {
            if let Some(value) = governance.get(key) {
                res.insert(key.to_string(), value.clone());
            }
        }
        res.insert("governance".to_string(), governance);
        res.insert(
            "task_contract".to_string(),
            contract.unwrap_or(serde_json::json!({"diagnosis": "no_task_contract"})),
        );
        res.insert(
            "current_step".to_string(),
            step.unwrap_or(serde_json::json!({"diagnosis": "no_steps"})),
        );
        res.insert(
            "reviewer_role_contract".to_string(),
            reviewer_role_contract
                .unwrap_or(serde_json::json!({"diagnosis": "no_reviewer_role_contract"})),
        );
        res.insert(
            "normalization_rules".to_string(),
            normalization_rules
                .unwrap_or(serde_json::json!({"diagnosis": "no_normalization_rules"})),
        );
        res.insert("verdicts".to_string(), Value::Array(verdicts));
        res.insert(
            "review_input_snapshot".to_string(),
            snapshot.unwrap_or(serde_json::json!({"diagnosis": "no_snapshot"})),
        );
        res.insert("lease_raw_token_omitted".to_string(), Value::Bool(true));
        Ok(Value::Object(res))
    }

    /// 为已存在但缺少 Role Contract 绑定的 remediation 步骤补建 Executor Role Contract 绑定。
    ///
    /// 仅复用 `bind_step_to_executor_role_contract` 的 fail-closed 逻辑（workspace binding /
    /// step 归属 / executor lineage 链连续性 / 历史绑定冲突 等全部前置校验在该函数内完成）。
    /// 这是修复“本地 report 路径绕开 daemon 注入 remediation 步骤却未绑定”遗留治理不一致的
    /// 唯一经 daemon 权威写点的合规入口；任何治理事实缺失都会由下层函数拒绝并回滚事务。
    pub fn handle_task_step_bind_role_contract(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        let step_id = params
            .get("step_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        if task_id.is_empty() || step_id.is_empty() {
            return Err(DaemonRpcError::invalid_params(
                "task.step.bind_role_contract 必须携带 task_id 与 step_id",
            ));
        }
        let identity = parse_action_identity(params)?.ok_or_else(|| {
            DaemonRpcError::new(
                "E_TASK_STEP_BIND_IDENTITY_REQUIRED",
                "task.step.bind_role_contract 必须携带完整 identity",
            )
        })?;
        if identity.role != "executor" {
            return Err(DaemonRpcError::new(
                "E_TASK_STEP_BIND_ROLE_REQUIRED",
                format!(
                    "仅允许 role=executor 补绑 remediation 步骤，实际 role={}",
                    identity.role
                ),
            ));
        }
        let owner_key = peer.owner_key();
        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;
        // 下层函数具备全部 fail-closed 校验；绑定失败时事务随 tx drop 自动回滚。
        let binding_id = bind_step_to_executor_role_contract(&tx, task_id, step_id, &owner_key)?;
        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!(
                "提交 task.step.bind_role_contract 事务失败: {}",
                e
            ))
        })?;
        let mut res = serde_json::Map::new();
        res.insert("ok".to_string(), Value::Bool(true));
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("step_id".to_string(), Value::String(step_id.to_string()));
        res.insert("binding_id".to_string(), Value::String(binding_id));
        Ok(Value::Object(res))
    }
}
