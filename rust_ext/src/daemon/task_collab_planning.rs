//! 任务规划域：split、plan、completion review 和 quality finding。
//! 保留原有步骤解析、事务和治理投影语义。

use super::*;

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

        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;

        if let Some(sub_defs) = subtasks_param {
            for (idx, sub_def) in sub_defs.iter().enumerate() {
                let st_title = sub_def
                    .get("title")
                    .and_then(|v| v.as_str())
                    .unwrap_or_else(|| "subtask");
                let st_desc = sub_def
                    .get("description")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let sub_id = format!("{}-sub-{}", task_id, idx + 1);

                tx.execute(
                    "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at, parent_id)
                     VALUES (?1, ?2, ?3, ?4, 'open', ?5, ?6, ?7)",
                    params![sub_id, st_title, st_desc, peer.owner_key(), ts, ts, task_id],
                ).map_err(|e| DaemonRpcError::internal_error(format!("子任务创建失败: {}", e)))?;

                let steps = match sub_def.get("steps") {
                    None => &[][..],
                    Some(value) => value.as_array().ok_or_else(|| {
                        DaemonRpcError::invalid_params("子任务 steps 必须是 JSON array")
                    })?,
                };
                insert_task_steps(&tx, &sub_id, steps, ts)?;

                created_subtasks.push(sub_id);
            }
        } else {
            let plan_text = if !plan_file.is_empty() {
                std::fs::read_to_string(plan_file).unwrap_or_default()
            } else {
                String::new()
            };

            let parsed = parse_subtasks_from_plan_text(&plan_text);
            for (idx, (st_title, st_desc, steps)) in parsed.into_iter().enumerate() {
                let sub_id = format!("{}-sub-{}", task_id, idx + 1);
                tx.execute(
                    "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at, parent_id)
                     VALUES (?1, ?2, ?3, ?4, 'open', ?5, ?6, ?7)",
                    params![sub_id, st_title, st_desc, peer.owner_key(), ts, ts, task_id],
                ).map_err(|e| DaemonRpcError::internal_error(format!("计划子任务创建失败: {}", e)))?;

                let step_values: Vec<Value> = steps.into_iter().map(Value::Object).collect();
                insert_task_steps(&tx, &sub_id, &step_values, ts)?;

                created_subtasks.push(sub_id);
            }
        }

        let seq = self.next_seq();
        tx.execute(
            "INSERT INTO task_events
             (task_id, workspace_id, from_status, to_status, reason_code, reason, actor_identity, monotonic_seq, authoritative_timestamp)
             VALUES (?1, '', 'in_progress', 'in_progress', 'split', ?2, ?3, ?4, ?5)",
            params![task_id, plan_file, peer.owner_key(), seq, ts],
        ).ok();

        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 task_split 事务失败: {}", e))
        })?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert("status".to_string(), Value::String("split".to_string()));
        res.insert(
            "subtask_count".to_string(),
            Value::Number(serde_json::Number::from(created_subtasks.len())),
        );
        res.insert(
            "subtasks".to_string(),
            Value::Array(created_subtasks.into_iter().map(Value::String).collect()),
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
        let workspace_instance_id = params
            .get("workspace_instance_id")
            .and_then(|v| v.as_str())
            .unwrap_or("");
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

        let (_binding_id, _capture_id) =
            bind_task_to_workspace(&tx, &task_id, workspace_id, "", &peer.owner_key())?;

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
