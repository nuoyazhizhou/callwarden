//! Reviewer verdict 领域：Evidence Gate 后的 verdict ledger 追加。
//! 保留原有 contract、identity、evidence 和事务校验语义。

use super::*;

impl TaskCollabStore {
    pub fn handle_verdict_submit(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let required = |name: &str| -> Result<&str, DaemonRpcError> {
            params
                .get(name)
                .and_then(|v| v.as_str())
                .map(str::trim)
                .filter(|v| !v.is_empty())
                .ok_or_else(|| DaemonRpcError::invalid_params(format!("缺少 {}", name)))
        };
        let task_id = required("task_id")?;
        let step_id = required("step_id")?;
        let contract_id = required("contract_id")?;
        let contract_hash = required("contract_hash")?;
        let role_contract_id = required("role_contract_id")?;
        let role_contract_hash = required("role_contract_hash")?;
        let snapshot_id = required("snapshot_id")?;
        let request_id = required("request_id")?;
        if request_id
            .chars()
            .any(|c| !c.is_ascii_alphanumeric() && !matches!(c, '-' | '_' | '.' | ':'))
        {
            return Err(DaemonRpcError::invalid_params(
                "request_id 只能包含 ASCII 字母、数字、-_.:",
            ));
        }
        let contract_revision = params
            .get("contract_revision")
            .and_then(|v| v.as_i64())
            .filter(|v| *v > 0)
            .ok_or_else(|| DaemonRpcError::invalid_params("contract_revision 必须为正整数"))?;
        let role_contract_revision = params
            .get("role_contract_revision")
            .and_then(|v| v.as_i64())
            .filter(|v| *v > 0)
            .ok_or_else(|| DaemonRpcError::invalid_params("role_contract_revision 必须为正整数"))?;
        let phase = required("phase")?;
        if !matches!(phase, "blind_first_pass" | "post_reveal_amendment") {
            return Err(DaemonRpcError::new(
                "E_VERDICT_PHASE_INVALID",
                "phase 必须是 blind_first_pass 或 post_reveal_amendment",
            ));
        }
        let overall = required("overall")?;
        if !matches!(overall, "pass" | "block") {
            return Err(DaemonRpcError::new(
                "E_VERDICT_OVERALL_INVALID",
                "overall 必须是 pass 或 block",
            ));
        }
        let attestation = required("attestation")?;
        let amendment_ref = params
            .get("amendment_ref")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        if phase == "post_reveal_amendment" && amendment_ref.is_empty() {
            return Err(DaemonRpcError::new(
                "E_VERDICT_AMENDMENT_REF_REQUIRED",
                "post_reveal_amendment 必须引用 sealed verdict",
            ));
        }
        if phase == "blind_first_pass" && !amendment_ref.is_empty() {
            return Err(DaemonRpcError::new(
                "E_VERDICT_AMENDMENT_REF_INVALID",
                "blind_first_pass 不得携带 amendment_ref",
            ));
        }
        let clause_results = params
            .get("clause_results")
            .cloned()
            .unwrap_or_else(|| Value::Array(Vec::new()));
        if !clause_results.is_array() {
            return Err(DaemonRpcError::invalid_params(
                "clause_results 必须是 JSON array",
            ));
        }
        let findings = params
            .get("findings")
            .cloned()
            .unwrap_or_else(|| Value::Array(Vec::new()));
        if !findings.is_array() {
            return Err(DaemonRpcError::invalid_params("findings 必须是 JSON array"));
        }
        let identity = parse_action_identity(params)?.ok_or_else(|| {
            DaemonRpcError::new(
                "E_IDENTITY_REQUIRED",
                "verdict.submit 必须携带完整 Reviewer identity",
            )
        })?;
        if !matches!(identity.role.as_str(), "reviewer" | "independent_reviewer") {
            return Err(DaemonRpcError::new(
                "E_VERDICT_REVIEWER_ROLE_REQUIRED",
                "verdict.submit 只允许 reviewer/independent_reviewer identity",
            ));
        }
        let (lease_token, fencing_counter) = Self::require_lease_params(params)?;
        let clock = self
            .clock
            .as_ref()
            .ok_or_else(|| lease_clock_unavailable("verdict.submit", task_id, "reviewer"))?;
        let submitted_at = clock.now_secs() as f64;

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启 verdict 事务失败: {}", e)))?;
        self.validate_lease_for_mutation(
            &tx,
            task_id,
            "reviewer",
            &lease_token,
            fencing_counter,
            Some(&identity),
        )?;

        let task_status: String = tx
            .query_row(
                "SELECT status FROM tasks WHERE id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .map_err(|_| {
                DaemonRpcError::new("task_not_found", format!("任务不存在: {}", task_id))
            })?;
        if task_status != "review" {
            return Err(DaemonRpcError::new(
                "E_VERDICT_TASK_NOT_IN_REVIEW",
                format!(
                    "任务 {} 当前状态为 {}，不能提交 Reviewer verdict",
                    task_id, task_status
                ),
            ));
        }
        let step_exists: i64 = tx
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE id = ?1 AND task_id = ?2",
                params![step_id, task_id],
                |r| r.get(0),
            )
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("校验 verdict step 失败: {}", e))
            })?;
        if step_exists != 1 {
            return Err(DaemonRpcError::new(
                "E_VERDICT_STEP_MISMATCH",
                "step_id 不属于目标 task",
            ));
        }

        let task_contract_workspace: Option<i64> = tx
            .query_row(
                "SELECT workspace_id FROM task_contract_revisions
                 WHERE task_id = ?1 AND contract_id = ?2 AND revision = ?3 AND contract_hash = ?4",
                params![task_id, contract_id, contract_revision, contract_hash],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("校验 Task Contract 失败: {}", e)))?
            .ok_or_else(|| {
                DaemonRpcError::new(
                    "E_TASK_CONTRACT_BINDING_INVALID",
                    "Task Contract id/revision/hash 未精确绑定目标 task",
                )
            })?;

        let role_row: Option<(
            String,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
        )> = tx
            .query_row(
                "SELECT role, step_id, skill_id, skill_version, prompt_template_id, prompt_hash,
                        allowed_paths, forbidden_paths, commands, acceptance_checks,
                        required_evidence, handoff_to, independence
                 FROM role_contracts
                 WHERE contract_id = ?1 AND task_id = ?2 AND revision = ?3 AND is_current = 1",
                params![role_contract_id, task_id, role_contract_revision],
                |r| {
                    Ok((
                        r.get(0)?,
                        r.get(1)?,
                        r.get(2)?,
                        r.get(3)?,
                        r.get(4)?,
                        r.get(5)?,
                        r.get(6)?,
                        r.get(7)?,
                        r.get(8)?,
                        r.get(9)?,
                        r.get(10)?,
                        r.get(11)?,
                        r.get(12)?,
                    ))
                },
            )
            .optional()
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("校验 Role Contract 失败: {}", e))
            })?;
        let role_row = role_row.ok_or_else(|| {
            DaemonRpcError::new(
                "E_ROLE_CONTRACT_BINDING_INVALID",
                "Role Contract id/revision 未精确绑定目标 task 的当前合同",
            )
        })?;
        if !matches!(role_row.0.as_str(), "reviewer" | "independent_reviewer") {
            return Err(DaemonRpcError::new(
                "E_ROLE_CONTRACT_BINDING_INVALID",
                "Role Contract 不是 Reviewer 合同",
            ));
        }
        if !role_row.1.is_empty() && role_row.1 != step_id {
            return Err(DaemonRpcError::new(
                "E_ROLE_CONTRACT_STEP_MISMATCH",
                "Role Contract step_id 与 verdict step_id 不一致",
            ));
        }
        let role_contract_payload = serde_json::json!({
            "canonicalization_version": "role-contract-c14n/v1",
            "contract_id": role_contract_id,
            "revision": role_contract_revision,
            "task_id": task_id,
            "role": role_row.0,
            "step_id": role_row.1,
            "skill_id": role_row.2,
            "skill_version": role_row.3,
            "prompt_template_id": role_row.4,
            "prompt_hash": role_row.5,
            "allowed_paths": role_row.6,
            "forbidden_paths": role_row.7,
            "commands": role_row.8,
            "acceptance_checks": role_row.9,
            "required_evidence": role_row.10,
            "handoff_to": role_row.11,
            "independence": role_row.12,
        });
        let actual_role_contract_hash = format!(
            "sha256:{}",
            sha256_hex(role_contract_payload.to_string().as_bytes())
        );
        if actual_role_contract_hash != role_contract_hash {
            return Err(DaemonRpcError::new(
                "E_ROLE_CONTRACT_HASH_MISMATCH",
                "Role Contract canonical hash 不匹配",
            ));
        }

        if !amendment_ref.is_empty() {
            let sealed_exists: i64 = tx
                .query_row(
                    "SELECT COUNT(*) FROM task_verdict_events
                     WHERE verdict_id = ?1 AND task_id = ?2 AND phase = 'blind_first_pass'",
                    params![amendment_ref, task_id],
                    |r| r.get(0),
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("校验 sealed verdict 失败: {}", e))
                })?;
            if sealed_exists != 1 {
                return Err(DaemonRpcError::new(
                    "E_VERDICT_AMENDMENT_REF_INVALID",
                    "amendment_ref 未引用当前任务的 sealed verdict",
                ));
            }
        }

        let verdict_id = params
            .get("verdict_id")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|v| !v.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| {
                format!(
                    "V-{}",
                    &sha256_hex(format!("{}:{}", task_id, request_id).as_bytes())[..24]
                )
            });
        let view_manifest_hash = params
            .get("view_manifest_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let canonical_params = serde_json::json!({
            "task_id": task_id,
            "step_id": step_id,
            "verdict_id": verdict_id,
            "contract_id": contract_id,
            "contract_revision": contract_revision,
            "contract_hash": contract_hash,
            "role_contract_id": role_contract_id,
            "role_contract_revision": role_contract_revision,
            "role_contract_hash": role_contract_hash,
            "phase": phase,
            "view_manifest_hash": view_manifest_hash,
            "snapshot_id": snapshot_id,
            "clause_results": clause_results,
            "findings": findings,
            "overall": overall,
            "attestation": attestation,
            "amendment_ref": amendment_ref,
            "identity": {
                "agent_id": identity.agent_id,
                "agent_instance_id": identity.agent_instance_id,
                "client_id": identity.client_id,
                "model_id": identity.model_id,
                "session_id": identity.session_id,
                "role": identity.role,
            },
        });
        let params_hash = format!(
            "sha256:{}",
            sha256_hex(canonical_params.to_string().as_bytes())
        );
        let request_marker = format!("%\"request_id\":\"{}\"%", request_id);
        if let Some((existing_id, existing_identity)) = tx
            .query_row(
                "SELECT verdict_id, reviewer_identity FROM task_verdict_events
                 WHERE task_id = ?1 AND reviewer_identity LIKE ?2 ORDER BY id ASC LIMIT 1",
                params![task_id, request_marker],
                |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)),
            )
            .optional()
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 verdict 幂等记录失败: {}", e))
            })?
        {
            let saved_hash = serde_json::from_str::<Value>(&existing_identity)
                .ok()
                .and_then(|v| {
                    v.get("params_hash")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                });
            if saved_hash.as_deref() == Some(params_hash.as_str()) {
                return Ok(serde_json::json!({
                    "success": true,
                    "task_id": task_id,
                    "verdict_id": existing_id,
                    "replayed": true,
                }));
            }
            return Err(DaemonRpcError::new(
                "E_REQUEST_ID_REUSE_MISMATCH",
                "同一 request_id 已绑定不同 verdict params",
            ));
        }
        let reviewer_identity = serde_json::json!({
            "request_id": request_id,
            "params_hash": params_hash,
            "step_id": step_id,
            "identity": canonical_params["identity"],
            "role_contract": {
                "id": role_contract_id,
                "revision": role_contract_revision,
                "hash": role_contract_hash,
                "canonicalization_version": "role-contract-c14n/v1",
            },
        })
        .to_string();
        tx.execute(
            "INSERT INTO task_verdict_events
             (verdict_id, task_id, contract_id, contract_revision, contract_hash,
              phase, view_manifest_hash, snapshot_id, reviewer_identity, clause_results,
              findings, overall, attestation, amendment_ref, submitted_at, workspace_id)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16)",
            params![
                verdict_id,
                task_id,
                contract_id,
                contract_revision,
                contract_hash,
                phase,
                view_manifest_hash,
                snapshot_id,
                reviewer_identity,
                clause_results.to_string(),
                findings.to_string(),
                overall,
                attestation,
                amendment_ref,
                submitted_at,
                task_contract_workspace,
            ],
        )
        .map_err(|e| {
            if e.to_string()
                .contains("UNIQUE constraint failed: task_verdict_events.verdict_id")
            {
                DaemonRpcError::new(
                    "E_VERDICT_ID_REUSE_MISMATCH",
                    "verdict_id 已绑定其他 verdict",
                )
            } else {
                DaemonRpcError::internal_error(format!("写入 task_verdict_events 失败: {}", e))
            }
        })?;
        let event_id = tx.last_insert_rowid();
        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交 verdict 事务失败: {}", e)))?;

        Ok(serde_json::json!({
            "success": true,
            "task_id": task_id,
            "verdict_id": verdict_id,
            "event_id": event_id,
            "submitted_at": submitted_at,
            "replayed": false,
        }))
    }

    // MCP-001（T-1787321708699-da5d8224）：role_view.get 从 python_compat 迁移为
    // Rust native。语义与 Python db_task_reviews.get_role_view + tools_collab
    // _collab_direct_read("role_view.get") 完全一致：
    //   - 从 task_contract_revisions 取最新 envelope_payload；
    //   - 按 (role, "1.0", "blind") allowlist 过滤字段；
    //   - allowlist/contract/content/view_manifest 四类 hash 用规范 JSON
    //     （key 排序、无空格，等价 Python _canonical_json）计算。
}
