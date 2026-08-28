//! Evidence 与 Gate 领域：任务证据写入/查询及 Gate 决策投影。
//! 保留原有 evidence provenance、去重、事务和 Evidence Gate 语义。

use super::*;

impl TaskCollabStore {
    pub fn handle_evidence_append(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) {
            let same = cached.get("evidence_id") == params.get("evidence_id")
                && cached.get("step_id") == params.get("step_id")
                && cached.get("payload_hash") == params.get("payload_hash");
            if same {
                return Ok(cached);
            }
            return Err(DaemonRpcError::new(
                "E_REQUEST_ID_REUSE_MISMATCH",
                "同一 request_id 已绑定不同 evidence/step/payload",
            ));
        }
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let step_id = params
            .get("step_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 step_id"))?;
        let evidence_id = params
            .get("evidence_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 evidence_id"))?;
        let evidence_type = params
            .get("evidence_type")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 evidence_type"))?;
        let manifest_path = params
            .get("manifest_path")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 manifest_path"))?;
        let payload_hash = params
            .get("payload_hash")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 payload_hash"))?;
        let request_id = params
            .get("request_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 request_id"))?;
        if request_id
            .chars()
            .any(|c| !c.is_ascii_alphanumeric() && !matches!(c, '-' | '_' | '.' | ':'))
        {
            return Err(DaemonRpcError::invalid_params(
                "request_id 只能包含 ASCII 字母、数字、-_.:",
            ));
        }
        let path = Path::new(manifest_path);
        if path.is_absolute()
            || manifest_path.split(['/', '\\']).any(|part| part == "..")
            || !manifest_path.starts_with("docs/evidence/")
        {
            return Err(DaemonRpcError::new(
                "E_EVIDENCE_MANIFEST_PATH_INVALID",
                "manifest_path 必须是 docs/evidence/ 下的相对路径",
            ));
        }
        let identity = parse_action_identity(params)?.ok_or_else(|| {
            DaemonRpcError::new(
                "E_IDENTITY_REQUIRED",
                "evidence.append 必须携带完整 identity",
            )
        })?;
        let (token, counter) = Self::require_lease_params(params)?;
        let conn = self.conn.lock().unwrap();
        self.validate_lease_for_mutation(
            &conn,
            task_id,
            "implementer",
            &token,
            counter,
            Some(&identity),
        )?;

        let producer_identity = format!(
            "request_id={};step_id={};identity={}",
            request_id,
            step_id,
            serde_json::json!({
                "agent_id": identity.agent_id,
                "agent_instance_id": identity.agent_instance_id,
                "client_id": identity.client_id,
                "session_id": identity.session_id,
                "role": identity.role,
            })
        );
        // request_id 是持久化在 producer_identity 前缀中的 operation key，
        // daemon 重启后仍能区分同参重放与参数冲突。
        let request_prefix = format!("request_id={};%", request_id);
        if let Ok(existing) = conn.query_row(
            "SELECT evidence_id, payload_hash, step_id FROM task_evidence_events
             WHERE task_id = ?1 AND producer_identity LIKE ?2 ORDER BY id ASC LIMIT 1",
            params![task_id, request_prefix],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2).unwrap_or_default(),
                ))
            },
        ) {
            if existing.0 == evidence_id && existing.1 == payload_hash && existing.2 == step_id {
                let mut result = Map::new();
                result.insert("task_id".to_string(), Value::String(task_id.to_string()));
                result.insert("step_id".to_string(), Value::String(step_id.to_string()));
                result.insert(
                    "evidence_id".to_string(),
                    Value::String(evidence_id.to_string()),
                );
                result.insert(
                    "payload_hash".to_string(),
                    Value::String(payload_hash.to_string()),
                );
                result.insert("replayed".to_string(), Value::Bool(true));
                return Ok(Value::Object(result));
            }
            return Err(DaemonRpcError::new(
                "E_REQUEST_ID_REUSE_MISMATCH",
                "同一 request_id 已绑定不同 evidence/step/payload",
            ));
        }

        let task_exists: Result<String, _> = conn.query_row(
            "SELECT status FROM tasks WHERE id = ?1",
            params![task_id],
            |r| r.get(0),
        );
        if task_exists.is_err() {
            return Err(DaemonRpcError::new(
                "task_not_found",
                format!("任务不存在: {}", task_id),
            ));
        }
        let step_belongs: Result<String, _> = conn.query_row(
            "SELECT status FROM task_steps WHERE id = ?1 AND task_id = ?2",
            params![step_id, task_id],
            |r| r.get(0),
        );
        if step_belongs.is_err() {
            return Err(DaemonRpcError::new(
                "E_EVIDENCE_STEP_MISMATCH",
                "evidence 的 step_id 不属于 task",
            ));
        }
        let _evidence_json = params.get("evidence_json").cloned().unwrap_or(Value::Null);
        let file_hashes = params
            .get("file_hashes")
            .map(Value::to_string)
            .unwrap_or_default();
        let symbol_hashes = params
            .get("symbol_hashes")
            .map(Value::to_string)
            .unwrap_or_default();
        let verifier_name = params
            .get("verifier_name")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let verifier_version = params
            .get("verifier_version")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let verifier_config_hash = params
            .get("verifier_config_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let commit_hash = params
            .get("commit_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let workspace_snapshot_id = params
            .get("workspace_snapshot_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let graph_refresh_version = params
            .get("graph_refresh_version")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let ts = task_now_ts();
        conn.execute(
            "INSERT INTO task_evidence_events
             (evidence_id, task_id, contract_id, contract_revision, contract_hash,
              evidence_type, event_type, commit_hash, workspace_snapshot_id, file_hashes,
              symbol_hashes, graph_refresh_version, verifier_name, verifier_version,
              verifier_config_hash, producer_identity, produced_at, payload_hash,
              invalidation_reason, original_evidence_ref, workspace_id)
             VALUES (?1, ?2, '', 0, '', ?3, 'evidence_appended', ?4, ?5, ?6, ?7, ?8,
                     ?9, ?10, ?11, ?12, ?13, ?14, '', '', NULL)",
            params![
                evidence_id,
                task_id,
                evidence_type,
                commit_hash,
                workspace_snapshot_id,
                file_hashes,
                symbol_hashes,
                graph_refresh_version,
                verifier_name,
                verifier_version,
                verifier_config_hash,
                producer_identity,
                ts,
                payload_hash
            ],
        )
        .map_err(|e| {
            if e.to_string()
                .contains("UNIQUE constraint failed: task_evidence_events.evidence_id")
            {
                DaemonRpcError::new(
                    "E_EVIDENCE_ID_REUSE_MISMATCH",
                    "evidence_id 已绑定其他 Evidence",
                )
            } else {
                DaemonRpcError::internal_error(format!("写入 task_evidence_events 失败: {}", e))
            }
        })?;
        let mut result = Map::new();
        result.insert("task_id".to_string(), Value::String(task_id.to_string()));
        result.insert("step_id".to_string(), Value::String(step_id.to_string()));
        result.insert(
            "evidence_id".to_string(),
            Value::String(evidence_id.to_string()),
        );
        result.insert(
            "payload_hash".to_string(),
            Value::String(payload_hash.to_string()),
        );
        result.insert(
            "produced_at".to_string(),
            Value::Number(serde_json::Number::from_f64(ts).unwrap()),
        );
        let value = Value::Object(result);
        self.save_dedup(params, &value);
        Ok(value)
    }

    pub fn handle_evidence_query(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let task_events = self.handle_task_events(peer, params)?;
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn
            .prepare(
                "SELECT id, task_id, step_id, file_path, hash_before, hash_after, diff, author, timestamp
                 FROM change_audit WHERE task_id = ?1 ORDER BY timestamp ASC",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 change_audit 失败: {}", e)))?;
        let rows = stmt
            .query_map(params![task_id], |r| {
                let mut m = Map::new();
                m.insert("id".to_string(), Value::String(r.get(0)?));
                m.insert("task_id".to_string(), Value::String(r.get(1)?));
                m.insert(
                    "step_id".to_string(),
                    Value::String(r.get::<_, Option<String>>(2)?.unwrap_or_default()),
                );
                m.insert("file_path".to_string(), Value::String(r.get(3)?));
                m.insert("hash_before".to_string(), Value::String(r.get(4)?));
                m.insert("hash_after".to_string(), Value::String(r.get(5)?));
                m.insert("diff".to_string(), Value::String(r.get(6)?));
                m.insert("author".to_string(), Value::String(r.get(7)?));
                m.insert(
                    "timestamp".to_string(),
                    Value::Number(serde_json::Number::from_f64(r.get(8)?).unwrap()),
                );
                Ok(Value::Object(m))
            })
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("映射 change_audit 失败: {}", e))
            })?;
        let mut changes = Vec::new();
        for row in rows {
            changes.push(row.map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 change_audit 失败: {}", e))
            })?);
        }
        let mut result = Map::new();
        result.insert("task_id".to_string(), Value::String(task_id.to_string()));
        result.insert(
            "task_events".to_string(),
            task_events
                .get("events")
                .cloned()
                .unwrap_or(Value::Array(Vec::new())),
        );
        result.insert("change_audit".to_string(), Value::Array(changes));
        let mut stmt = conn
            .prepare(
                "SELECT evidence_id, task_id, evidence_type, event_type, commit_hash,
                    workspace_snapshot_id, file_hashes, symbol_hashes, graph_refresh_version,
                    verifier_name, verifier_version, verifier_config_hash, producer_identity,
                    produced_at, payload_hash
             FROM task_evidence_events WHERE task_id = ?1 ORDER BY id ASC",
            )
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 task_evidence_events 失败: {}", e))
            })?;
        let rows = stmt
            .query_map(params![task_id], |r| {
                let mut m = Map::new();
                m.insert("evidence_id".to_string(), Value::String(r.get(0)?));
                m.insert("task_id".to_string(), Value::String(r.get(1)?));
                m.insert("evidence_type".to_string(), Value::String(r.get(2)?));
                m.insert("event_type".to_string(), Value::String(r.get(3)?));
                m.insert("commit_hash".to_string(), Value::String(r.get(4)?));
                m.insert(
                    "workspace_snapshot_id".to_string(),
                    Value::String(r.get(5)?),
                );
                m.insert("file_hashes".to_string(), Value::String(r.get(6)?));
                m.insert("symbol_hashes".to_string(), Value::String(r.get(7)?));
                m.insert(
                    "graph_refresh_version".to_string(),
                    Value::String(r.get(8)?),
                );
                m.insert("verifier_name".to_string(), Value::String(r.get(9)?));
                m.insert("verifier_version".to_string(), Value::String(r.get(10)?));
                m.insert(
                    "verifier_config_hash".to_string(),
                    Value::String(r.get(11)?),
                );
                m.insert("producer_identity".to_string(), Value::String(r.get(12)?));
                m.insert(
                    "produced_at".to_string(),
                    Value::Number(serde_json::Number::from_f64(r.get(13)?).unwrap()),
                );
                m.insert("payload_hash".to_string(), Value::String(r.get(14)?));
                Ok(Value::Object(m))
            })
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("映射 task_evidence_events 失败: {}", e))
            })?;
        let mut evidence = Vec::new();
        for row in rows {
            evidence.push(row.map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 task_evidence_events 失败: {}", e))
            })?);
        }
        result.insert("task_evidence_events".to_string(), Value::Array(evidence));
        Ok(Value::Object(result))
    }

    /// Evidence Gate 决策只读投影；无记录返回空数组，不把空值解释为 PASS。
    pub fn handle_gate_decision_query(
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
                "SELECT decision_id, task_id, decision, reason, requested_transition, event_type, decision_time
                 FROM task_gate_decisions WHERE task_id = ?1 ORDER BY decision_time ASC",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("查询 gate decision 失败: {}", e)))?;
        let rows = stmt
            .query_map(params![task_id], |r| {
                let mut m = Map::new();
                m.insert("decision_id".to_string(), Value::String(r.get(0)?));
                m.insert("task_id".to_string(), Value::String(r.get(1)?));
                m.insert("decision".to_string(), Value::String(r.get(2)?));
                m.insert("reason".to_string(), Value::String(r.get(3)?));
                m.insert("requested_transition".to_string(), Value::String(r.get(4)?));
                m.insert("event_type".to_string(), Value::String(r.get(5)?));
                m.insert(
                    "decision_time".to_string(),
                    Value::Number(serde_json::Number::from_f64(r.get(6)?).unwrap()),
                );
                Ok(Value::Object(m))
            })
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("映射 gate decision 失败: {}", e))
            })?;
        let mut decisions = Vec::new();
        for row in rows {
            decisions.push(row.map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 gate decision 失败: {}", e))
            })?);
        }
        let mut result = Map::new();
        result.insert("task_id".to_string(), Value::String(task_id.to_string()));
        result.insert("decisions".to_string(), Value::Array(decisions));
        Ok(Value::Object(result))
    }

    /// 将已完成的只读 Gate 检查结果追加到权威 ledger。它不是 Verdict，
    /// 也不改变任务状态；仅允许当前 Executor lease 记录 task-owned gate
    /// 证据，所有 provenance 以请求中的 evidence hash 原样保存。
    pub fn handle_gate_decision_append(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) {
            let same = cached.get("evidence_id") == params.get("evidence_id")
                && cached.get("step_id") == params.get("step_id")
                && cached.get("evidence_hash")
                    == params
                        .get("payload_hash")
                        .or_else(|| params.get("evidence_hash"));
            if same {
                return Ok(cached);
            }
            return Err(DaemonRpcError::new(
                "E_REQUEST_ID_REUSE_MISMATCH",
                "同一 request_id 已绑定不同 Gate evidence/step/payload",
            ));
        }
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 task_id"))?;
        let decision = params
            .get("decision")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 decision"))?;
        if !matches!(decision, "pass" | "block" | "warn") {
            return Err(DaemonRpcError::new(
                "E_GATE_DECISION_INVALID",
                "decision 必须是 pass/block/warn",
            ));
        }
        let reason = params.get("reason").and_then(|v| v.as_str()).unwrap_or("");
        let evidence_hash = params
            .get("evidence_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let evidence_id = params
            .get("evidence_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 evidence_id"))?;
        let step_id = params
            .get("step_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 step_id"))?;
        let request_id = params
            .get("request_id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 request_id"))?;
        if request_id
            .chars()
            .any(|c| !c.is_ascii_alphanumeric() && !matches!(c, '-' | '_' | '.' | ':'))
        {
            return Err(DaemonRpcError::invalid_params(
                "request_id 只能包含 ASCII 字母、数字、-_.:",
            ));
        }
        let identity = parse_action_identity(params)?;
        let (token, counter) = Self::require_lease_params(params)?;
        let conn = self.conn.lock().unwrap();
        self.validate_lease_for_mutation(
            &conn,
            task_id,
            "implementer",
            &token,
            counter,
            identity.as_ref(),
        )?;
        let payload_hash = params
            .get("payload_hash")
            .and_then(|v| v.as_str())
            .unwrap_or(evidence_hash);
        let evidence_matches: Result<(String, String), _> = conn.query_row(
            "SELECT payload_hash, producer_identity FROM task_evidence_events
             WHERE task_id = ?1 AND evidence_id = ?2",
            params![task_id, evidence_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        );
        let existing_evidence = evidence_matches.map_err(|_| {
            DaemonRpcError::new(
                "E_GATE_EVIDENCE_REQUIRED",
                "Gate decision 必须绑定已提交的 task Evidence",
            )
        })?;
        if existing_evidence.0 != payload_hash
            || !existing_evidence
                .1
                .contains(&format!("step_id={}", step_id))
        {
            return Err(DaemonRpcError::new(
                "E_GATE_EVIDENCE_MISMATCH",
                "Gate decision 的 evidence_id/payload_hash/step_id 与 Evidence 不匹配",
            ));
        }
        let clause_binding = serde_json::json!({
            "evidence_id": evidence_id,
            "step_id": step_id,
            "payload_hash": payload_hash,
            "request_id": request_id,
        })
        .to_string();
        let request_marker = format!("%\"request_id\":\"{}\"%", request_id);
        if let Ok((existing_id, existing_clause)) = conn.query_row(
            "SELECT decision_id, clause_decisions FROM task_gate_decisions
             WHERE task_id = ?1 AND clause_decisions LIKE ?2 ORDER BY id ASC LIMIT 1",
            params![task_id, request_marker],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)),
        ) {
            if existing_clause == clause_binding {
                let mut replay = Map::new();
                replay.insert("decision_id".to_string(), Value::String(existing_id));
                replay.insert("task_id".to_string(), Value::String(task_id.to_string()));
                replay.insert("decision".to_string(), Value::String(decision.to_string()));
                replay.insert(
                    "evidence_id".to_string(),
                    Value::String(evidence_id.to_string()),
                );
                replay.insert("replayed".to_string(), Value::Bool(true));
                return Ok(Value::Object(replay));
            }
            return Err(DaemonRpcError::new(
                "E_REQUEST_ID_REUSE_MISMATCH",
                "同一 request_id 已绑定不同 Gate evidence/step/payload",
            ));
        }
        let ts = task_now_ts();
        let decision_id = format!(
            "GD-{}",
            &sha256_hex(format!("{}:{}", task_id, request_id).as_bytes())[..24]
        );
        conn.execute(
            "INSERT INTO task_gate_decisions
             (decision_id, task_id, contract_id, contract_revision, contract_hash,
              gate_snapshot_s0, gate_snapshot_s1, requested_transition, decision, reason,
              clause_decisions, verifier_triples, resolved_stage_toggle_set,
              independence_policy_value, independence_waiver_marker, event_type,
              decision_time, workspace_id)
             VALUES (?1, ?2, '', 0, '', '', '', 'review', ?3, ?4, ?5, '', ?6, '', '',
                     'runtime_task_gate', ?7, NULL)",
            params![
                decision_id,
                task_id,
                decision,
                reason,
                clause_binding,
                evidence_id,
                ts
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("写入 gate decision 失败: {}", e)))?;
        let mut result = Map::new();
        result.insert("decision_id".to_string(), Value::String(decision_id));
        result.insert("task_id".to_string(), Value::String(task_id.to_string()));
        result.insert("decision".to_string(), Value::String(decision.to_string()));
        result.insert(
            "evidence_id".to_string(),
            Value::String(evidence_id.to_string()),
        );
        result.insert(
            "evidence_hash".to_string(),
            Value::String(payload_hash.to_string()),
        );
        result.insert("step_id".to_string(), Value::String(step_id.to_string()));
        let value = Value::Object(result);
        self.save_dedup(params, &value);
        Ok(value)
    }

}
