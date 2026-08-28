//! 治理只读域：角色视图、证据新鲜度、依赖图和接口 provider 查询。
//! 保留原有 workspace 过滤、contract 绑定与 fail-closed 错误语义。

use super::*;

impl TaskCollabStore {
    pub fn handle_get_role_view(
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
        let role = if role.is_empty() {
            "implementer"
        } else {
            role.as_str()
        };

        // 从最新契约 Envelope 生成 Role_View（view_type=role, stage=blind）
        let envelope: Value = {
            let conn = self.conn.lock().unwrap();
            let row: Option<String> = conn
                .query_row(
                    "SELECT envelope_payload FROM task_contract_revisions \
                     WHERE task_id = ?1 ORDER BY revision DESC LIMIT 1",
                    params![task_id],
                    |r| r.get(0),
                )
                .optional()
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!(
                        "读取 task_contract_revisions 失败: {e}"
                    ))
                })?;
            match row {
                Some(payload) if !payload.is_empty() => {
                    serde_json::from_str(&payload).unwrap_or(Value::Object(Map::new()))
                }
                _ => Value::Object(Map::new()),
            }
        };

        let allowed: Vec<&str> = match role {
            "planner" => vec![
                "contract_id",
                "profile",
                "title",
                "description",
                "requirements",
                "target_file",
                "target_symbol",
                "clauses",
                "blocking_clauses",
            ],
            "reviewer" => vec![
                "contract_id",
                "profile",
                "title",
                "description",
                "requirements",
                "target_file",
                "target_symbol",
                "allowed_edit_scope",
                "actual_changes",
                "symbol_changes",
                "test_runs",
                "open_quality_findings",
                "clauses",
                "blocking_clauses",
            ],
            "tester" => vec![
                "contract_id",
                "profile",
                "title",
                "description",
                "requirements",
                "target_file",
                "target_symbol",
                "clauses",
                "test_cases",
                "test_runs",
            ],
            // 默认 implementer（含未知 role 兼容 Python 语义）
            _ => vec![
                "contract_id",
                "profile",
                "title",
                "description",
                "requirements",
                "target_file",
                "target_symbol",
                "allowed_edit_scope",
                "clauses",
                "blocking_clauses",
            ],
        };
        let allowed_set: HashSet<&str> = allowed.iter().copied().collect();

        // 过滤 content：envelope 中在 allowlist 内的字段保留，其余进 excluded
        let mut filtered: Map<String, Value> = Map::new();
        let mut excluded: Vec<String> = Vec::new();
        if let Some(obj) = envelope.as_object() {
            for (key, value) in obj {
                if allowed_set.contains(key.as_str()) {
                    filtered.insert(key.clone(), value.clone());
                } else {
                    excluded.push(key.clone());
                }
            }
        }
        excluded.sort();

        let mut sorted_allowed: Vec<&str> = allowed.clone();
        sorted_allowed.sort_unstable();
        let allowlist_def_hash = canonical_json_sha256(&Value::Array(
            sorted_allowed
                .iter()
                .map(|s| Value::String(s.to_string()))
                .collect(),
        ));
        let contract_hash = envelope
            .get("contract_hash")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_else(|| canonical_json_sha256(&envelope));
        let content_hash = canonical_json_sha256(&Value::Object(filtered.clone()));

        let manifest = json!({
            "view_type": role,
            "view_version": "1.0",
            "stage": "blind",
            "contract_hash": contract_hash,
            "allowlist_hash": allowlist_def_hash,
            "content_hash": content_hash,
        });
        let view_manifest_hash = canonical_json_sha256(&manifest);

        let mut result = Map::new();
        result.insert("task_id".to_string(), Value::String(task_id.to_string()));
        result.insert("view_type".to_string(), Value::String(role.to_string()));
        result.insert("view_version".to_string(), Value::String("1.0".to_string()));
        result.insert("stage".to_string(), Value::String("blind".to_string()));
        result.insert(
            "view_manifest_hash".to_string(),
            Value::String(view_manifest_hash),
        );
        result.insert("contract_hash".to_string(), Value::String(contract_hash));
        result.insert("content".to_string(), Value::Object(filtered));
        result.insert(
            "allowed_fields".to_string(),
            Value::Array(
                sorted_allowed
                    .iter()
                    .map(|s| Value::String(s.to_string()))
                    .collect(),
            ),
        );
        result.insert(
            "excluded_fields".to_string(),
            Value::Array(excluded.iter().map(|s| Value::String(s.clone())).collect()),
        );
        Ok(Value::Object(result))
    }

    // MCP-002（T-1787321708760-de068a9c）：find_evidence 从 python_compat 迁移为
    // Rust native。语义与 Python tools_collab._h_find_evidence +
    // db_task_reviews 的 evidence.query 完全一致：从 task_evidence_events 按
    // task_id / contract_id / verifier / limit 过滤查询，返回 {"items": [...], "count": N}。
    pub fn handle_find_evidence(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let contract_id = params
            .get("contract_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let verifier = params
            .get("verifier")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let limit = params.get("limit").and_then(|v| v.as_i64()).unwrap_or(50);
        let limit = if limit < 0 { 50 } else { limit as i64 };

        let conn = self.conn.lock().unwrap();
        let mut sql = String::from(
            "SELECT evidence_id, task_id, evidence_type, event_type, commit_hash,
                    workspace_snapshot_id, file_hashes, symbol_hashes, graph_refresh_version,
                    verifier_name, verifier_version, verifier_config_hash, producer_identity,
                    produced_at, payload_hash
             FROM task_evidence_events WHERE 1=1",
        );
        let mut binds: Vec<String> = Vec::new();
        if let Some(ref t) = task_id {
            sql.push_str(" AND task_id = ?");
            binds.push(t.clone());
        }
        if let Some(ref c) = contract_id {
            sql.push_str(" AND evidence_id LIKE ?");
            binds.push(format!("%{}%", c));
        }
        if let Some(ref v) = verifier {
            sql.push_str(" AND verifier_name = ?");
            binds.push(v.clone());
        }
        sql.push_str(" ORDER BY id DESC LIMIT ?");
        binds.push(limit.to_string());

        let mut stmt = conn.prepare(&sql).map_err(|e| {
            DaemonRpcError::internal_error(format!("查询 task_evidence_events 失败: {}", e))
        })?;
        let rows = stmt
            .query_map(rusqlite::params_from_iter(binds.iter()), |r| {
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
        let mut items = Vec::new();
        for row in rows {
            items.push(row.map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 task_evidence_events 失败: {}", e))
            })?);
        }
        let mut result = Map::new();
        result.insert("items".to_string(), Value::Array(items.clone()));
        result.insert(
            "count".to_string(),
            Value::Number(serde_json::Number::from(items.len())),
        );
        Ok(Value::Object(result))
    }

    // MCP-003 （T-1787321708856-e3c10624）：get_freshness_status 从 python_compat
    // 迁移为 Rust native。语义与 Python db_task_evidence.derive_freshness 一致：
    // 全序优先级 invalid > superseded > stale > fresh（Req 6.15）。当前调用方
    // 仅传 evidence_id/task_id（snapshot/hash 为 None，stale 分支不触发）。
    pub fn handle_get_freshness_status(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let evidence_id = params
            .get("evidence_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        let conn = self.conn.lock().unwrap();
        // 当前契约 revision（无契约时取 0）
        let current_rev: i64 = if let Some(ref t) = task_id {
            conn.query_row(
                "SELECT MAX(revision) FROM task_contract_revisions WHERE task_id = ?",
                params![t],
                |r| r.get::<_, Option<i64>>(0),
            )
            .ok()
            .flatten()
            .unwrap_or(0)
        } else {
            0
        };

        // 收集待查询 evidence_id
        let mut ids: Vec<String> = Vec::new();
        if let Some(ref eid) = evidence_id {
            if !eid.is_empty() {
                ids.push(eid.clone());
            }
        } else if let Some(ref t) = task_id {
            let mut stmt = conn
                .prepare(
                    "SELECT evidence_id FROM task_evidence_events \
                     WHERE task_id = ? AND event_type = ?",
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("查询 task_evidence_events 失败: {}", e))
                })?;
            let rows = stmt
                .query_map(params![t, "evidence_appended"], |r| r.get::<_, String>(0))
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("映射 evidence_id 失败: {}", e))
                })?;
            for row in rows {
                if let Ok(eid) = row {
                    ids.push(eid);
                }
            }
        }

        let mut items: Vec<Value> = Vec::new();
        for eid in ids {
            if eid.is_empty() {
                continue;
            }
            let status = super::TaskCollabStore::derive_evidence_freshness(&conn, &eid, current_rev);
            items.push(json!({"evidence_id": eid, "status": status}));
        }
        let mut result = Map::new();
        result.insert("items".to_string(), Value::Array(items));
        Ok(Value::Object(result))
    }

    // MCP-004（T-1787321708926-e7ebfac4）：get_gate_decision 从 python_compat
    // 迁移为 Rust native。语义与 Python tools_collab._h_gate_decision +
    // gate.decision.query 一致：从 task_gate_decisions 按 task_id/decision_id
    // (gate_id) 过滤查询，按 decision_time DESC 限流。
    pub fn handle_get_gate_decision(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let gate_id = params
            .get("gate_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let limit: i64 = params.get("limit").and_then(|v| v.as_i64()).unwrap_or(20);

        let conn = self.conn.lock().unwrap();
        let mut sql = String::from(
            "SELECT decision_id, task_id, contract_id, contract_revision, contract_hash, \
             decision, reason, clause_decisions, verifier_triples, resolved_stage_toggle_set, \
             independence_policy_value, independence_waiver_marker, event_type, decision_time, \
             step_id, role_contract_lineage_id, role_contract_revision_id, role_contract_revision, \
             role_contract_hash, canonicalization_version, canonicalization_rules_hash, \
             normalization_version, normalization_rules_hash, workspace_id \
             FROM task_gate_decisions WHERE 1=1",
        );
        let mut binds: Vec<String> = Vec::new();
        if let Some(ref t) = task_id {
            sql.push_str(" AND task_id = ?");
            binds.push(t.clone());
        }
        if let Some(ref g) = gate_id {
            if !g.is_empty() {
                sql.push_str(" AND decision_id = ?");
                binds.push(g.clone());
            }
        }
        sql.push_str(" ORDER BY decision_time DESC LIMIT ?");
        binds.push(limit.to_string());

        let mut stmt = conn.prepare(&sql).map_err(|e| {
            DaemonRpcError::internal_error(format!("查询 task_gate_decisions 失败: {}", e))
        })?;
        let rows = stmt
            .query_map(rusqlite::params_from_iter(binds.iter()), |r| {
                let mut m = Map::new();
                m.insert("decision_id".to_string(), Value::String(r.get(0)?));
                m.insert("task_id".to_string(), Value::String(r.get(1)?));
                m.insert("contract_id".to_string(), Value::String(r.get(2)?));
                m.insert(
                    "contract_revision".to_string(),
                    Value::Number(r.get::<_, i64>(3)?.into()),
                );
                m.insert("contract_hash".to_string(), Value::String(r.get(4)?));
                m.insert("decision".to_string(), Value::String(r.get(5)?));
                m.insert("reason".to_string(), Value::String(r.get(6)?));
                m.insert("clause_decisions".to_string(), Value::String(r.get(7)?));
                m.insert("verifier_triples".to_string(), Value::String(r.get(8)?));
                m.insert(
                    "resolved_stage_toggle_set".to_string(),
                    Value::String(r.get(9)?),
                );
                m.insert(
                    "independence_policy_value".to_string(),
                    Value::String(r.get(10)?),
                );
                m.insert(
                    "independence_waiver_marker".to_string(),
                    Value::String(r.get(11)?),
                );
                m.insert("event_type".to_string(), Value::String(r.get(12)?));
                m.insert(
                    "decision_time".to_string(),
                    Value::Number(serde_json::Number::from_f64(r.get(13)?).unwrap()),
                );
                m.insert("step_id".to_string(), Value::String(r.get(14)?));
                m.insert(
                    "role_contract_lineage_id".to_string(),
                    Value::String(r.get(15)?),
                );
                m.insert(
                    "role_contract_revision_id".to_string(),
                    Value::String(r.get(16)?),
                );
                m.insert(
                    "role_contract_revision".to_string(),
                    Value::Number(r.get::<_, i64>(17)?.into()),
                );
                m.insert("role_contract_hash".to_string(), Value::String(r.get(18)?));
                m.insert(
                    "canonicalization_version".to_string(),
                    Value::String(r.get(19)?),
                );
                m.insert(
                    "canonicalization_rules_hash".to_string(),
                    Value::String(r.get(20)?),
                );
                m.insert(
                    "normalization_version".to_string(),
                    Value::String(r.get(21)?),
                );
                m.insert(
                    "normalization_rules_hash".to_string(),
                    Value::String(r.get(22)?),
                );
                m.insert(
                    "workspace_id".to_string(),
                    Value::Number(r.get::<_, i64>(23)?.into()),
                );
                Ok(Value::Object(m))
            })
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("映射 task_gate_decisions 失败: {}", e))
            })?;
        let mut items = Vec::new();
        for row in rows {
            items.push(row.map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 task_gate_decisions 失败: {}", e))
            })?);
        }
        let mut result = Map::new();
        result.insert("items".to_string(), Value::Array(items.clone()));
        result.insert(
            "count".to_string(),
            Value::Number(serde_json::Number::from(items.len())),
        );
        Ok(Value::Object(result))
    }

    // MCP-005（T-1787321709017-ed4e79b0）：get_artifact_freshness 从 python_compat
    // 迁移为 Rust native。语义与 Python tools_p2_graph._h_get_artifact_freshness +
    // db_task_dependencies.get_artifact_freshness 一致：从 artifact_identities 按
    // workspace_id + task_id (+ artifact_ref) 取最新一条。
    pub fn handle_get_artifact_freshness(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id: i64 = params
            .get("workspace_id")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let artifact_ref = params
            .get("artifact_ref")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        let conn = self.conn.lock().unwrap();
        let (sql, binds): (String, Vec<String>) = if let Some(ref a) = artifact_ref {
            if a.is_empty() {
                (
                    "SELECT artifact_id, freshness_status, artifact_hash, produced_at \
                     FROM artifact_identities \
                     WHERE workspace_id = ? AND task_id = ? \
                     ORDER BY produced_at DESC LIMIT 1"
                        .to_string(),
                    vec![
                        workspace_id.to_string(),
                        task_id.clone().unwrap_or_default(),
                    ],
                )
            } else {
                (
                    "SELECT artifact_id, freshness_status, artifact_hash, produced_at \
                     FROM artifact_identities \
                     WHERE workspace_id = ? AND task_id = ? AND artifact_ref = ? \
                     ORDER BY produced_at DESC LIMIT 1"
                        .to_string(),
                    vec![
                        workspace_id.to_string(),
                        task_id.clone().unwrap_or_default(),
                        a.clone(),
                    ],
                )
            }
        } else {
            (
                "SELECT artifact_id, freshness_status, artifact_hash, produced_at \
                 FROM artifact_identities \
                 WHERE workspace_id = ? AND task_id = ? \
                 ORDER BY produced_at DESC LIMIT 1"
                    .to_string(),
                vec![
                    workspace_id.to_string(),
                    task_id.clone().unwrap_or_default(),
                ],
            )
        };

        let row = conn
            .prepare(&sql)
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 artifact_identities 失败: {}", e))
            })?
            .query_row(rusqlite::params_from_iter(binds.iter()), |r| {
                let mut m = Map::new();
                m.insert("artifact_id".to_string(), Value::String(r.get(0)?));
                m.insert("freshness_status".to_string(), Value::String(r.get(1)?));
                m.insert("artifact_hash".to_string(), Value::String(r.get(2)?));
                m.insert(
                    "produced_at".to_string(),
                    Value::Number(serde_json::Number::from_f64(r.get(3)?).unwrap()),
                );
                Ok(Value::Object(m))
            })
            .optional()
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("映射 artifact_identities 失败: {}", e))
            })?;

        match row {
            Some(v) => Ok(v),
            None => {
                let mut nf = Map::new();
                nf.insert("found".to_string(), Value::Bool(false));
                Ok(Value::Object(nf))
            }
        }
    }

    // MCP-006（T-1787321709098-f2236ea0）：get_interface_providers 从 python_compat
    // 迁移为 Rust native。语义与 Python tools_p2_graph._h_get_interface_providers +
    // db_task_dependencies.get_interface_providers 一致：从 interface_identities 按
    // workspace_id + interface_name (+ version) 查询 provider 列表。
    pub fn handle_get_interface_providers(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id: i64 = params
            .get("workspace_id")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let interface_name = params
            .get("interface_name")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_default();
        let version = params
            .get("version")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .unwrap_or_default();

        let conn = self.conn.lock().unwrap();
        let (sql, binds): (String, Vec<String>) = if version.is_empty() {
            (
                "SELECT interface_id, interface_name, version, interface_hash, \
                 provider_task_id, contract_id, contract_revision \
                 FROM interface_identities \
                 WHERE workspace_id = ? AND interface_name = ?"
                    .to_string(),
                vec![workspace_id.to_string(), interface_name.clone()],
            )
        } else {
            (
                "SELECT interface_id, interface_name, version, interface_hash, \
                 provider_task_id, contract_id, contract_revision \
                 FROM interface_identities \
                 WHERE workspace_id = ? AND interface_name = ? AND version = ?"
                    .to_string(),
                vec![
                    workspace_id.to_string(),
                    interface_name.clone(),
                    version.clone(),
                ],
            )
        };

        let mut stmt = conn.prepare(&sql).map_err(|e| {
            DaemonRpcError::internal_error(format!("查询 interface_identities 失败: {}", e))
        })?;
        let rows = stmt
            .query_map(rusqlite::params_from_iter(binds.iter()), |r| {
                let mut m = Map::new();
                m.insert("interface_id".to_string(), Value::String(r.get(0)?));
                m.insert("interface_name".to_string(), Value::String(r.get(1)?));
                m.insert("version".to_string(), Value::String(r.get(2)?));
                m.insert("interface_hash".to_string(), Value::String(r.get(3)?));
                m.insert("provider_task_id".to_string(), Value::String(r.get(4)?));
                m.insert("contract_id".to_string(), Value::String(r.get(5)?));
                m.insert(
                    "contract_revision".to_string(),
                    Value::Number(r.get::<_, i64>(6)?.into()),
                );
                Ok(Value::Object(m))
            })
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("映射 interface_identities 失败: {}", e))
            })?;
        let mut items = Vec::new();
        for row in rows {
            items.push(row.map_err(|e| {
                DaemonRpcError::internal_error(format!("读取 interface_identities 失败: {}", e))
            })?);
        }
        let mut result = Map::new();
        result.insert("items".to_string(), Value::Array(items.clone()));
        result.insert(
            "count".to_string(),
            Value::Number(serde_json::Number::from(items.len())),
        );
        Ok(Value::Object(result))
    }

    // MCP-007（T-1787321709179-f6fdf5bc）：detect_cycle 从 python_compat 迁移为
    // Rust native。语义与 Python db_task_dependencies.detect_cycle 完全一致：
    //   - 从 dependency_edges 取 workspace 内 is_hard=1 的边；
    //   - DFS 三色标记检测环，定位 cycle_start_node；
    //   - BFS 从 cycle_start_node 回到自身找最短 cycle path。
    // 返回 {"has_cycle": bool, "cycle_path": [str], "checked_nodes": int}。
    pub fn handle_detect_cycle(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id: i64 = params
            .get("workspace_id")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);

        let conn = self.conn.lock().unwrap();
        let mut graph: BTreeMap<String, Vec<String>> = BTreeMap::new();
        {
            let mut stmt = conn
                .prepare(
                    "SELECT DISTINCT provider_task_id, consumer_task_id \
                     FROM dependency_edges \
                     WHERE workspace_id = ? AND is_hard = 1",
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("查询 dependency_edges 失败: {}", e))
                })?;
            let rows = stmt
                .query_map(params![workspace_id], |r| {
                    Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
                })
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("读取 dependency_edges 失败: {}", e))
                })?;
            for row in rows {
                let (provider, consumer) = row.map_err(|e| {
                    DaemonRpcError::internal_error(format!("映射 dependency_edges 失败: {}", e))
                })?;
                graph.entry(provider).or_default().push(consumer);
            }
        }

        if graph.is_empty() {
            let mut result = Map::new();
            result.insert("has_cycle".to_string(), Value::Bool(false));
            result.insert("cycle_path".to_string(), Value::Array(vec![]));
            result.insert(
                "checked_nodes".to_string(),
                Value::Number(serde_json::Number::from(0)),
            );
            return Ok(Value::Object(result));
        }

        // DFS 三色标记检测环（0=WHITE, 1=GRAY, 2=BLACK）
        let mut color: HashMap<String, u8> = HashMap::new();
        let mut cycle_start_node: Option<String> = None;

        fn dfs_detect(
            node: &str,
            graph: &BTreeMap<String, Vec<String>>,
            color: &mut HashMap<String, u8>,
            found: &mut Option<String>,
        ) -> bool {
            color.insert(node.to_string(), 1);
            if let Some(neighbors) = graph.get(node) {
                for nb in neighbors {
                    let c = color.get(nb).copied().unwrap_or(0);
                    if c == 1 {
                        *found = Some(nb.clone());
                        return true;
                    }
                    if c == 0 {
                        if dfs_detect(nb, graph, color, found) {
                            return true;
                        }
                    }
                }
            }
            color.insert(node.to_string(), 2);
            false
        }

        for node in graph.keys() {
            if color.get(node).copied().unwrap_or(0) == 0 {
                if dfs_detect(node, &graph, &mut color, &mut cycle_start_node) {
                    break;
                }
            }
        }

        if cycle_start_node.is_none() {
            let mut result = Map::new();
            result.insert("has_cycle".to_string(), Value::Bool(false));
            result.insert("cycle_path".to_string(), Value::Array(vec![]));
            result.insert(
                "checked_nodes".to_string(),
                Value::Number(serde_json::Number::from(graph.len())),
            );
            return Ok(Value::Object(result));
        }

        let start = cycle_start_node.clone().unwrap();
        let cycle_path = super::TaskCollabStore::detect_cycle_find_shortest(&graph, &start);

        let mut result = Map::new();
        result.insert("has_cycle".to_string(), Value::Bool(true));
        result.insert(
            "cycle_path".to_string(),
            Value::Array(cycle_path.into_iter().map(Value::String).collect()),
        );
        result.insert(
            "checked_nodes".to_string(),
            Value::Number(serde_json::Number::from(graph.len())),
        );
        Ok(Value::Object(result))
    }

    // MCP-008（T-1787321709249-fb256530）：validate_revision_dependencies 迁移 rust_native。
    // 语义与 Python tools_p2_graph._h_validate_revision_dependencies 一致：在内存中模拟
    // build_hard_dependency_edges（不写 dependency_edges 表）——查询 task_dependencies →
    // 解析 requires_artifact / requires_interface 的 provider → 检查多 provider 显式选择 →
    // 计算 edges_built/edges_skipped/resolution_errors/provider_conflicts；环检测合并
    // 「现有表硬边 ∪ 本次模拟边」（与 db 层 build 幂等写表后 detect_cycle(整表) 语义等价）。
    pub fn handle_validate_revision_dependencies(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id: i64 = params
            .get("workspace_id")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let contract_id = params
            .get("contract_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let contract_revision: i64 = params
            .get("contract_revision")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);

        let conn = self.conn.lock().unwrap();

        // 1. 内存模拟 build_hard_dependency_edges（不写表）
        let mut edges_built: i64 = 0;
        let mut edges_skipped: i64 = 0;
        let mut resolution_errors: Vec<String> = Vec::new();
        let mut provider_conflicts: Vec<Value> = Vec::new();
        let mut new_edges: BTreeMap<String, Vec<String>> = BTreeMap::new();

        {
            let mut stmt = conn
                .prepare(
                    "SELECT task_id, dependency_type, target_ref, target_task_id, \
                            contract_id, contract_revision \
                     FROM task_dependencies \
                     WHERE workspace_id = ? AND contract_id = ? AND contract_revision = ? \
                       AND is_informational = 0",
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("查询 task_dependencies 失败: {}", e))
                })?;
            let rows = stmt
                .query_map(params![workspace_id, contract_id, contract_revision], |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                        r.get::<_, String>(3)?,
                        r.get::<_, String>(4)?,
                        r.get::<_, i64>(5)?,
                    ))
                })
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("读取 task_dependencies 失败: {}", e))
                })?;
            for row in rows {
                let (
                    consumer_task_id,
                    dtype,
                    target_ref,
                    target_task_id,
                    dep_contract_id,
                    dep_revision,
                ) = row.map_err(|e| {
                    DaemonRpcError::internal_error(format!("映射 task_dependencies 失败: {}", e))
                })?;

                if dtype == "requires_artifact" {
                    // requires_artifact: target_task_id 是 provider
                    if target_task_id.is_empty() {
                        resolution_errors.push(format!(
                            "requires_artifact 依赖缺少 target_task_id (task={}, ref={})",
                            consumer_task_id, target_ref
                        ));
                        edges_skipped += 1;
                        continue;
                    }
                    new_edges
                        .entry(target_task_id.clone())
                        .or_default()
                        .push(consumer_task_id.clone());
                    edges_built += 1;
                } else if dtype == "requires_interface" {
                    // requires_interface: 需要解析 provides_interface
                    let providers =
                        Self::query_interface_providers(&conn, workspace_id, &target_ref, "");
                    if providers.is_empty() {
                        resolution_errors.push(format!(
                            "requires_interface '{}' 无匹配 provider (task={})",
                            target_ref, consumer_task_id
                        ));
                        edges_skipped += 1;
                        continue;
                    }
                    if providers.len() > 1 {
                        // 多 provider：检查是否有显式选择（Req 9.9）
                        let selected = Self::query_provider_selection(
                            &conn,
                            workspace_id,
                            &consumer_task_id,
                            &dep_contract_id,
                            dep_revision,
                            &target_ref,
                        );
                        if selected.is_none() {
                            let provs: Vec<Value> =
                                providers.iter().map(|p| Value::String(p.clone())).collect();
                            let mut conflict = Map::new();
                            conflict.insert(
                                "consumer_task_id".to_string(),
                                Value::String(consumer_task_id.clone()),
                            );
                            conflict.insert(
                                "interface_name".to_string(),
                                Value::String(target_ref.clone()),
                            );
                            conflict.insert("providers".to_string(), Value::Array(provs));
                            provider_conflicts.push(Value::Object(conflict));
                            edges_skipped += 1;
                            continue;
                        }
                        new_edges
                            .entry(selected.unwrap())
                            .or_default()
                            .push(consumer_task_id.clone());
                        edges_built += 1;
                    } else {
                        new_edges
                            .entry(providers[0].clone())
                            .or_default()
                            .push(consumer_task_id.clone());
                        edges_built += 1;
                    }
                }
                // requires_existing 和 provides_interface 不建边
            }
        }

        // 2. 合并现有表硬边（db 层 build 幂等写表后 detect_cycle 检测整表）
        let mut graph: BTreeMap<String, Vec<String>> = BTreeMap::new();
        {
            let mut stmt2 = conn
                .prepare(
                    "SELECT DISTINCT provider_task_id, consumer_task_id \
                     FROM dependency_edges \
                     WHERE workspace_id = ? AND is_hard = 1",
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("查询 dependency_edges 失败: {}", e))
                })?;
            let rows2 = stmt2
                .query_map(params![workspace_id], |r| {
                    Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
                })
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("读取 dependency_edges 失败: {}", e))
                })?;
            for row in rows2 {
                let (provider, consumer) = row.map_err(|e| {
                    DaemonRpcError::internal_error(format!("映射 dependency_edges 失败: {}", e))
                })?;
                graph.entry(provider).or_default().push(consumer);
            }
        }
        for (provider, consumers) in new_edges.iter() {
            graph
                .entry(provider.clone())
                .or_default()
                .extend(consumers.iter().cloned());
        }
        drop(conn);

        // 3. 环检测（复刻 db 层 detect_cycle：DFS 三色 + BFS 最短 cycle path）
        let mut has_cycle = false;
        let mut cycle_path: Vec<String> = Vec::new();
        if !graph.is_empty() {
            let cycle_result = Self::detect_cycle_on_graph(&graph);
            has_cycle = cycle_result.0;
            cycle_path = cycle_result.1;
        }

        // 4. 组装结果（与 db 层 validate_revision_dependencies 相同结构）
        let mut errors: Vec<String> = resolution_errors;
        for conflict in provider_conflicts.iter() {
            let consumer = conflict
                .get("consumer_task_id")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let interface = conflict
                .get("interface_name")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let provs: Vec<&str> = conflict
                .get("providers")
                .and_then(|v| v.as_array())
                .map(|a| a.iter().filter_map(|x| x.as_str()).collect())
                .unwrap_or_default();
            errors.push(format!(
                "interface '{}' 有多个 provider {:?} 但无 Planner 显式选择 (consumer={})",
                interface, provs, consumer
            ));
        }
        if has_cycle {
            errors.push(format!("硬依赖图存在环: {}", cycle_path.join(" → ")));
        }

        let valid = errors.is_empty() && provider_conflicts.is_empty();

        let mut result = Map::new();
        result.insert("valid".to_string(), Value::Bool(valid));
        result.insert(
            "errors".to_string(),
            Value::Array(errors.into_iter().map(Value::String).collect()),
        );
        result.insert(
            "cycle_path".to_string(),
            if has_cycle {
                Value::Array(cycle_path.into_iter().map(Value::String).collect())
            } else {
                Value::Array(vec![])
            },
        );
        result.insert(
            "provider_conflicts".to_string(),
            Value::Array(provider_conflicts),
        );
        result.insert(
            "edges_built".to_string(),
            Value::Number(serde_json::Number::from(edges_built)),
        );
        result.insert(
            "edges_skipped".to_string(),
            Value::Number(serde_json::Number::from(edges_skipped)),
        );
        Ok(Value::Object(result))
    }

    // 查询 interface_identities 中匹配的 provider 列表（复刻 db get_interface_providers）。
    fn query_interface_providers(
        conn: &rusqlite::Connection,
        workspace_id: i64,
        interface_name: &str,
        version: &str,
    ) -> Vec<String> {
        let mut stmt = conn
            .prepare(
                "SELECT provider_task_id FROM interface_identities \
                 WHERE workspace_id = ? AND interface_name = ?",
            )
            .ok();
        let Some(mut stmt) = stmt else { return vec![] };
        let rows = stmt
            .query_map(params![workspace_id, interface_name], |r| {
                r.get::<_, String>(0)
            })
            .ok();
        let Some(rows) = rows else { return vec![] };
        rows.filter_map(|r| r.ok()).collect()
    }

    // 查询已记录的 provider 选择（复刻 db get_provider_selection）。
    fn query_provider_selection(
        conn: &rusqlite::Connection,
        workspace_id: i64,
        consumer_task_id: &str,
        contract_id: &str,
        contract_revision: i64,
        interface_name: &str,
    ) -> Option<String> {
        let mut stmt = conn
            .prepare(
                "SELECT selected_provider_task_id FROM interface_provider_selections \
                 WHERE workspace_id = ? AND consumer_task_id = ? AND contract_id = ? \
                   AND contract_revision = ? AND interface_name = ?",
            )
            .ok()?;
        stmt.query_row(
            params![
                workspace_id,
                consumer_task_id,
                contract_id,
                contract_revision,
                interface_name
            ],
            |r| r.get::<_, String>(0),
        )
        .ok()
    }

    // 在内存边集合上做环检测（复刻 db detect_cycle：DFS 三色 + BFS 最短 cycle path）。
    // 返回 (has_cycle, cycle_path)。
    fn detect_cycle_on_graph(graph: &BTreeMap<String, Vec<String>>) -> (bool, Vec<String>) {
        if graph.is_empty() {
            return (false, vec![]);
        }
        let mut color: HashMap<String, u8> = HashMap::new();
        let mut cycle_start_node: Option<String> = None;

        fn dfs_detect(
            node: &str,
            graph: &BTreeMap<String, Vec<String>>,
            color: &mut HashMap<String, u8>,
            found: &mut Option<String>,
        ) -> bool {
            color.insert(node.to_string(), 1);
            if let Some(neighbors) = graph.get(node) {
                for nb in neighbors {
                    let c = color.get(nb).copied().unwrap_or(0);
                    if c == 1 {
                        *found = Some(nb.clone());
                        return true;
                    }
                    if c == 0 && dfs_detect(nb, graph, color, found) {
                        return true;
                    }
                }
            }
            color.insert(node.to_string(), 2);
            false
        }

        for node in graph.keys() {
            if color.get(node).copied().unwrap_or(0) == 0 {
                if dfs_detect(node, graph, &mut color, &mut cycle_start_node) {
                    break;
                }
            }
        }

        match cycle_start_node {
            None => (false, vec![]),
            Some(start) => (true, super::TaskCollabStore::detect_cycle_find_shortest(graph, &start)),
        }
    }

    // MCP-009（T-1787321709365-021050a8）：get_dependency_edges 迁移 rust_native。
    // 语义与 Python db_task_dependencies.get_dependency_edges 一致：查询硬依赖图边
    // （dependency_edges 全部列，按 created_at 排序），可选按 task_id 过滤
    // （provider_task_id 或 consumer_task_id 匹配）。返回行数组（与 Python dict 行
    // 键名一致：id/workspace_id/provider_task_id/consumer_task_id/edge_type/
    // source_type/contract_id/contract_revision/is_hard/created_at）。
    pub fn handle_get_dependency_edges(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id: i64 = params
            .get("workspace_id")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        let conn = self.conn.lock().unwrap();
        let rows: Vec<Value> = if task_id.is_empty() {
            let mut stmt = conn
                .prepare(
                    "SELECT id, workspace_id, provider_task_id, consumer_task_id, \
                            edge_type, source_type, contract_id, contract_revision, \
                            is_hard, created_at \
                     FROM dependency_edges \
                     WHERE workspace_id = ? \
                     ORDER BY created_at",
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("查询 dependency_edges 失败: {}", e))
                })?;
            let items = stmt
                .query_map(params![workspace_id], |r| Self::map_dependency_edge_row(r))
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("读取 dependency_edges 失败: {}", e))
                })?;
            let mut out = Vec::new();
            for it in items {
                out.push(it.map_err(|e| {
                    DaemonRpcError::internal_error(format!("映射 dependency_edges 失败: {}", e))
                })?);
            }
            out
        } else {
            let mut stmt = conn
                .prepare(
                    "SELECT id, workspace_id, provider_task_id, consumer_task_id, \
                            edge_type, source_type, contract_id, contract_revision, \
                            is_hard, created_at \
                     FROM dependency_edges \
                     WHERE workspace_id = ? AND (provider_task_id = ? OR consumer_task_id = ?) \
                     ORDER BY created_at",
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("查询 dependency_edges 失败: {}", e))
                })?;
            let items = stmt
                .query_map(params![workspace_id, task_id, task_id], |r| {
                    Self::map_dependency_edge_row(r)
                })
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("读取 dependency_edges 失败: {}", e))
                })?;
            let mut out = Vec::new();
            for it in items {
                out.push(it.map_err(|e| {
                    DaemonRpcError::internal_error(format!("映射 dependency_edges 失败: {}", e))
                })?);
            }
            out
        };

        Ok(Value::Array(rows))
    }

    // 将 dependency_edges 一行映射为与 Python dict 行一致的 JSON 对象。
    fn map_dependency_edge_row(r: &rusqlite::Row) -> Result<Value, rusqlite::Error> {
        let mut m = Map::new();
        m.insert("id".to_string(), Value::Number(r.get::<_, i64>(0)?.into()));
        m.insert(
            "workspace_id".to_string(),
            Value::Number(r.get::<_, i64>(1)?.into()),
        );
        m.insert(
            "provider_task_id".to_string(),
            Value::String(r.get::<_, String>(2)?),
        );
        m.insert(
            "consumer_task_id".to_string(),
            Value::String(r.get::<_, String>(3)?),
        );
        m.insert(
            "edge_type".to_string(),
            Value::String(r.get::<_, String>(4)?),
        );
        m.insert(
            "source_type".to_string(),
            Value::String(r.get::<_, String>(5)?),
        );
        m.insert(
            "contract_id".to_string(),
            Value::String(r.get::<_, String>(6)?),
        );
        m.insert(
            "contract_revision".to_string(),
            Value::Number(r.get::<_, i64>(7)?.into()),
        );
        m.insert(
            "is_hard".to_string(),
            Value::Number(r.get::<_, i64>(8)?.into()),
        );
        m.insert(
            "created_at".to_string(),
            Value::Number(
                serde_json::Number::from_f64(r.get::<_, f64>(9)?)
                    .unwrap_or(serde_json::Number::from(0)),
            ),
        );
        Ok(Value::Object(m))
    }

}
