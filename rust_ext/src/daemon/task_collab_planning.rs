//! 任务规划域：split、plan、completion review 和 quality finding。
//! 保留原有步骤解析、事务和治理投影语义。

use super::*;
use crate::daemon::task_loop::task_contract_bootstrap::{
    bootstrap_task_governance_contracts, BootstrapInput,
};

/// A' 标准三角色 legacy Role Contract 模板（与 CLI `_build_role_contracts(None)`
/// 冻结模板一致）。task.split 必须为每个子任务原子写入该三元组，否则
/// `bootstrap_task_governance_contracts` 因缺 legacy role_contracts 而拒绝派生
/// v1 治理投影（子任务"有任务有步骤但无法 claim/bootstrap"的残缺态）。
fn default_trio_role_contracts() -> Vec<Value> {
    serde_json::json!([
        {
            "role": "executor",
            "skill_id": "none",
            "skill_version": "",
            "prompt_template_id": "cw.aprime.executor.startup.v1",
            "prompt_hash": "59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6",
            "allowed_paths": "task-card scoped paths only",
            "forbidden_paths": "task.apply; task.close; task.supersede; out-of-scope production/schema changes",
            "commands": "task.next_action; task.claim; task.report; task.handoff",
            "acceptance_checks": "one tool/CLI link; tests; evidence manifest/hash; executor_ready_for_review",
            "required_evidence": "implementation plan; test output; negative test; daemon round-trip evidence",
            "handoff_to": "reviewer",
            "independence": "required",
        },
        {
            "role": "reviewer",
            "skill_id": "none",
            "skill_version": "",
            "prompt_template_id": "cw.aprime.reviewer.startup.v1",
            "prompt_hash": "6415033D8F134392DE16FCA130BFB762CB6C70D9F466C770EC18A20FC4CE139E",
            "allowed_paths": "read-only review evidence and structured review handoff",
            "forbidden_paths": "production edits; task.apply; task.close; task.supersede",
            "commands": "task.next_action; task.contract.get; task.handoff",
            "acceptance_checks": "independent verification of scope, diff, tests, evidence, gate and matrix condition",
            "required_evidence": "review record; findings or reviewer_pass evidence manifest/hash",
            "handoff_to": "adjudicator",
            "independence": "required",
        },
        {
            "role": "adjudicator",
            "skill_id": "none",
            "skill_version": "",
            "prompt_template_id": "cw.aprime.adjudicator.startup.v1",
            "prompt_hash": "42A5F1DEFA81008B009058C1BAF5D1A14B3EF4521E291B7B55C19BB473A77C3E",
            "allowed_paths": "final review and protected task finalization within daemon authority",
            "forbidden_paths": "production edits; local SQLite fallback; status forgery",
            "commands": "task.next_action; task.apply; task.close; task.handoff; task.supersede when separately authorized",
            "acceptance_checks": "ACCEPT requires valid reviewer lease/fencing then apply, close and next_action=COMPLETE",
            "required_evidence": "final review; lease/fencing provenance; apply/close/COMPLETE verification",
            "handoff_to": "complete",
            "independence": "required",
        }
    ])
    .as_array()
    .cloned()
    .unwrap_or_default()
}

impl TaskCollabStore {
    pub fn handle_task_split(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) {
            return Ok(cached);
        }
        let task_id = params.get("task_id").and_then(|v| v.as_str()).unwrap_or("");
        let plan_file = params
            .get("plan_file")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let subtasks_param = params.get("subtasks").and_then(|v| v.as_array());

        let ts = task_now_ts();
        let mut created_subtasks = Vec::new();
        let owner = peer.owner_key();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        // 父任务不可变 workspace binding 是子任务 workspace 的权威来源（fail-closed）。
        let workspace_id: i64 = tx
            .query_row(
                "SELECT workspace_id FROM task_workspace_bindings WHERE task_id = ?1 LIMIT 1",
                params![task_id],
                |r| r.get(0),
            )
            .map_err(|_| {
                DaemonRpcError::new(
                    "E_TASK_BINDING_REQUIRED",
                    format!(
                        "父任务 {} 缺少不可变 workspace binding，拒绝 split（子任务无法继承 workspace）",
                        task_id
                    ),
                )
            })?;

        // BR-01：子任务 instance 继承父任务 binding 的权威 capture instance
        //（与 create_subtask 一致；缺失 → fail-closed，绝不合成 ws-{id}）。
        let workspace_instance_id: String = tx
            .query_row(
                "SELECT c.workspace_instance_id FROM task_workspace_bindings b \
                 JOIN workspace_authority_captures c ON c.workspace_capture_id = b.workspace_capture_id \
                 WHERE b.task_id = ?1 LIMIT 1",
                params![task_id],
                |r| r.get::<_, String>(0),
            )
            .map_err(|e| {
                DaemonRpcError::new(
                    "E_TASK_BINDING_REQUIRED",
                    format!(
                        "父任务 {} 缺少权威 workspace capture，拒绝 split（无法继承 workspace_instance_id）: {}",
                        task_id, e
                    ),
                )
            })?;

        // 子任务 identity_policy：显式传入优先，缺省 legacy_identity_v1（与 task.create 兼容）。
        let identity_policy = params
            .get("identity_policy")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or("legacy_identity_v1")
            .to_string();

        // A' 标准三角色 legacy 模板（每个子任务一份）。
        let default_role_contracts = default_trio_role_contracts();

        // 统一子任务定义：(title, description, steps: Vec<Value>)。
        let mut subtask_defs: Vec<(String, String, Vec<Value>)> = Vec::new();
        if let Some(sub_defs) = subtasks_param {
            for sub_def in sub_defs.iter() {
                let st_title = sub_def
                    .get("title")
                    .and_then(|v| v.as_str())
                    .unwrap_or("subtask")
                    .to_string();
                let st_desc = sub_def
                    .get("description")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let steps = match sub_def.get("steps") {
                    None => Vec::new(),
                    Some(value) => value
                        .as_array()
                        .ok_or_else(|| {
                            DaemonRpcError::invalid_params("子任务 steps 必须是 JSON array")
                        })?
                        .clone(),
                };
                subtask_defs.push((st_title, st_desc, steps));
            }
        } else {
            let plan_text = if !plan_file.is_empty() {
                std::fs::read_to_string(plan_file).unwrap_or_default()
            } else {
                String::new()
            };
            let parsed = parse_subtasks_from_plan_text(&plan_text);
            for (st_title, st_desc, steps) in parsed {
                subtask_defs.push((
                    st_title,
                    st_desc,
                    steps.into_iter().map(Value::Object).collect(),
                ));
            }
        }

        for (idx, (st_title, st_desc, steps)) in subtask_defs.into_iter().enumerate() {
            let sub_id = format!("{}-sub-{}", task_id, idx + 1);

            tx.execute(
                "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at, parent_id)
                 VALUES (?1, ?2, ?3, ?4, 'open', ?5, ?6, ?7)",
                params![sub_id, st_title, st_desc, owner, ts, ts, task_id],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("子任务创建失败: {}", e)))?;

            insert_task_steps(&tx, &sub_id, &steps, ts)?;

            // 1) 不可变 workspace binding（继承父任务 workspace + 权威 instance；同一事务原子写入）。
            bind_task_to_workspace(&tx, &sub_id, workspace_id, &workspace_instance_id, &owner)?;

            // 2) legacy Role Contract（A' 三角色；bootstrap 派生 v1 投影的前置）。
            insert_role_contracts(&tx, &sub_id, &default_role_contracts, &owner, ts)?;

            // 3) Task Contract envelope + identity_policy + lineage + step binding。
            //    任一治理事实缺失/冲突 → 整体回滚，绝不留"有任务无合同"的残缺子任务。
            let mut envelope = task_create_contract_envelope(&sub_id, &st_title, &st_desc, &steps);
            envelope
                .as_object_mut()
                .expect("task_create_contract_envelope 必须返回 object")
                .insert(
                    "identity_policy".to_string(),
                    Value::String(identity_policy.clone()),
                );
            bootstrap_task_governance_contracts(
                &tx,
                &BootstrapInput {
                    task_id: sub_id.clone(),
                    envelope,
                    created_by: owner.clone(),
                    role_contract_source: "legacy".to_string(),
                },
                workspace_id,
            )?;

            created_subtasks.push(sub_id);
        }

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, workspace_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'in_progress', 'in_progress', 'split', ?3, ?4, ?5, ?6)",
            params![
                task_id,
                workspace_id.to_string(),
                plan_file,
                owner,
                seq,
                ts
            ],
        )
        .ok();

        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 task_split 事务失败: {}", e))
        })?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String("split".to_string()));
        res.insert(
            "workspace_id".to_string(),
            Value::Number(serde_json::Number::from(workspace_id)),
        );
        res.insert(
            "subtask_count".to_string(),
            Value::Number(serde_json::Number::from(created_subtasks.len())),
        );
        res.insert(
            "subtasks".to_string(),
            Value::Array(
                created_subtasks
                    .into_iter()
                    .map(Value::String)
                    .collect(),
            ),
        );
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_create_from_plan(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) {
            return Ok(cached);
        }
        // workspace authority fail-closed：根任务同样必须显式绑定 workspace。
        let workspace_id = required_workspace_id_param(params)?;
        // BR-01：create_from_plan 根任务也必须显式传入非空 workspace_instance_id，
        // 禁止 empty-instance 合成 ws-{id}。
        let workspace_instance_id = params
            .get("workspace_instance_id")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| {
                DaemonRpcError::new(
                    ERR_TASK_WORKSPACE_INSTANCE_REQUIRED,
                    "task.create_from_plan 必须显式传入非空 workspace_instance_id；禁止 empty-instance 合成 ws-{id}",
                )
            })?;
        let title = params
            .get("title")
            .and_then(|v| v.as_str())
            .unwrap_or("Root Plan Task");
        let plan_file = params
            .get("plan_file")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let root_task_id = generate_task_id();
        let ts = task_now_ts();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        tx.execute(
            "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at, parent_id)
             VALUES (?1, ?2, ?3, ?4, 'open', ?5, ?6, '')",
            params![root_task_id, title, plan_file, peer.owner_key(), ts, ts],
        ).map_err(|e| DaemonRpcError::internal_error(format!("建根任务失败: {}", e)))?;

        let (_binding_id, _capture_id) = bind_task_to_workspace(
            &tx,
            &root_task_id,
            workspace_id,
            workspace_instance_id,
            &peer.owner_key(),
        )?;

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events (task_id, workspace_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, ?2, 'none', 'open', 'created_from_plan', ?3, ?4, ?5, ?6)",
            params![root_task_id, workspace_id.to_string(), plan_file, peer.owner_key(), seq, ts],
        ).ok();

        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 task_create_from_plan 事务失败: {}", e))
        })?;

        let mut res = Map::new();
        res.insert("root_task_id".to_string(), Value::String(root_task_id));
        res.insert(
            "plan_file".to_string(),
            Value::String(plan_file.to_string()),
        );
        res.insert("created".to_string(), Value::Bool(true));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_completion_review(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = peer;
        let task_id = params.get("task_id").and_then(|v| v.as_str()).unwrap_or("");
        let conn = self.conn.lock().unwrap();

        // S4: 零步骤普通任务不能 vacuous pass —— 无步骤即无验收证据，返回 blocked
        let step_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE task_id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .unwrap_or(0);
        if step_count == 0 {
            let mut finding = Map::new();
            finding.insert(
                "finding_type".to_string(),
                Value::String("steps".to_string()),
            );
            finding.insert("severity".to_string(), Value::String("block".to_string()));
            finding.insert(
                "message".to_string(),
                Value::String("任务无步骤记录，无法进行完成性评审（E_NO_STEPS）".to_string()),
            );
            let mut res = Map::new();
            res.insert("task_id".to_string(), Value::String(task_id.to_string()));
            res.insert("decision".to_string(), Value::String("blocked".to_string()));
            res.insert(
                "reason".to_string(),
                Value::String("E_NO_STEPS".to_string()),
            );
            res.insert(
                "findings".to_string(),
                Value::Array(vec![Value::Object(finding)]),
            );
            return Ok(Value::Object(res));
        }

        let mut findings = Vec::new();
        let mut has_block = false;

        let mut stmt = conn.prepare(
            "SELECT id, finding_type, severity, message, status FROM task_quality_findings WHERE task_id = ?1 AND status != 'resolved'"
        ).map_err(|e| DaemonRpcError::internal_error(format!("查询 findings 失败: {}", e)))?;

        let rows = stmt
            .query_map(params![task_id], |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1).unwrap_or_default(),
                    row.get::<_, String>(2).unwrap_or_default(),
                    row.get::<_, String>(3).unwrap_or_default(),
                    row.get::<_, String>(4).unwrap_or_default(),
                ))
            })
            .ok();

        if let Some(rows_iter) = rows {
            for item in rows_iter.flatten() {
                if item.2 == "error" || item.2 == "block" {
                    has_block = true;
                }
                let mut obj = Map::new();
                obj.insert(
                    "id".to_string(),
                    Value::Number(serde_json::Number::from(item.0)),
                );
                obj.insert("finding_type".to_string(), Value::String(item.1));
                obj.insert("severity".to_string(), Value::String(item.2));
                obj.insert("message".to_string(), Value::String(item.3));
                obj.insert("status".to_string(), Value::String(item.4));
                findings.push(Value::Object(obj));
            }
        }

        let decision = if has_block { "block" } else { "pass" };
        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("decision".to_string(), Value::String(decision.to_string()));
        res.insert("findings".to_string(), Value::Array(findings));
        Ok(Value::Object(res))
    }

    pub fn handle_task_resolve_quality_finding(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) {
            return Ok(cached);
        }
        let finding_id = params
            .get("finding_id")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let resolution = params
            .get("resolution")
            .and_then(|v| v.as_str())
            .unwrap_or("fixed");

        let conn = self.conn.lock().unwrap();
        let updated = conn
            .execute(
                "UPDATE task_quality_findings SET status = ?1 WHERE id = ?2",
                params![resolution, finding_id],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("更新 finding 状态失败: {}", e)))?;

        let mut res = Map::new();
        res.insert(
            "finding_id".to_string(),
            Value::Number(serde_json::Number::from(finding_id)),
        );
        res.insert("status".to_string(), Value::String(resolution.to_string()));
        res.insert("updated".to_string(), Value::Bool(updated > 0));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_create_subtask(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) {
            return Ok(cached);
        }
        let parent_id = params
            .get("parent_task_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let title = params
            .get("title")
            .and_then(|v| v.as_str())
            .unwrap_or("subtask");
        let description = params
            .get("description")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let steps = match params.get("steps") {
            None => &[][..],
            Some(value) => value
                .as_array()
                .ok_or_else(|| DaemonRpcError::invalid_params("steps 必须是 JSON array"))?,
        };
        let task_id = generate_task_id();
        let ts = task_now_ts();
        let seq = self.next_seq();

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        // 子任务 workspace 从父任务不可变 binding 继承（§8.1.1：task 逻辑 workspace
        // 只来自 task_workspace_bindings；禁止 active workspace 补齐）。父任务无 binding
        // → E_TASK_WORKSPACE_UNBOUND fail-closed；无父任务（root 子任务）必须显式传入。
        let workspace_id = if parent_id.is_empty() {
            required_workspace_id_param(params)?
        } else {
            task_bound_workspace_id(&tx, parent_id, optional_workspace_id_param(params))?
        };

        // BR-01：子任务 instance 必须与父任务 binding 的权威 capture 一致（继承）；
        // root 子任务（无父）必须显式传入非空 workspace_instance_id，禁止合成 ws-{id}。
        let workspace_instance_id: String = if parent_id.is_empty() {
            params
                .get("workspace_instance_id")
                .and_then(|v| v.as_str())
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .ok_or_else(|| {
                    DaemonRpcError::new(
                        ERR_TASK_WORKSPACE_INSTANCE_REQUIRED,
                        "task.create_subtask（root 子任务）必须显式传入非空 workspace_instance_id",
                    )
                })?
                .to_string()
        } else {
            match tx.query_row(
                "SELECT c.workspace_instance_id FROM task_workspace_bindings b \
                 JOIN workspace_authority_captures c ON c.workspace_capture_id = b.workspace_capture_id \
                 WHERE b.task_id = ?1 LIMIT 1",
                params![parent_id],
                |r| r.get::<_, String>(0),
            ) {
                Ok(v) => v,
                Err(rusqlite::Error::QueryReturnedNoRows) => {
                    return Err(DaemonRpcError::new(
                        "E_TASK_WORKSPACE_UNBOUND",
                        format!(
                            "父任务 {} 无 workspace binding，无法继承 workspace_instance_id",
                            parent_id
                        ),
                    ));
                }
                Err(e) => {
                    return Err(DaemonRpcError::internal_error(format!(
                        "查询父任务 workspace instance 失败: {}",
                        e
                    )));
                }
            }
        };

        tx.execute(
            "INSERT INTO tasks
             (id, title, description, creator, status, created_at, updated_at, parent_id)
             VALUES (?1, ?2, ?3, ?4, 'open', ?5, ?6, ?7)",
            params![
                task_id,
                title,
                description,
                peer.owner_key(),
                ts,
                ts,
                parent_id
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("子任务写入失败: {}", e)))?;

        let (_binding_id, _capture_id) = bind_task_to_workspace(
            &tx,
            &task_id,
            workspace_id,
            &workspace_instance_id,
            &peer.owner_key(),
        )?;

        tx.execute(
            "INSERT INTO task_events (task_id, workspace_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp) VALUES (?1, ?2, 'none', 'open', 'subtask_created', ?3, ?4, ?5, ?6)",
            params![task_id, workspace_id.to_string(), title, peer.owner_key(), seq, ts],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("task_event append 失败: {}", e)))?;

        insert_task_steps(&tx, &task_id, steps, ts)?;

        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交子任务事务失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id));
        res.insert(
            "parent_id".to_string(),
            Value::String(parent_id.to_string()),
        );
        res.insert("status".to_string(), Value::String("open".to_string()));
        res.insert(
            "step_count".to_string(),
            Value::Number(serde_json::Number::from(steps.len())),
        );
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

}
