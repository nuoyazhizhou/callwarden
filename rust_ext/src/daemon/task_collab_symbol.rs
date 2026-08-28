//! 任务符号归因域：记录、链接和查询 symbol changes。
//! 保留原有 file version、symbol hash 和审计关联语义。

use super::*;

impl TaskCollabStore {
    pub fn handle_task_record_symbol_change(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) {
            return Ok(cached);
        }
        let _ = peer;
        let task_id = params.get("task_id").and_then(|v| v.as_str()).unwrap_or("");
        let file_path = params
            .get("file_path")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let symbol_hash = params
            .get("symbol_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let symbol_hash_before = params
            .get("symbol_hash_before")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let hash_after = if !symbol_hash.is_empty() {
            symbol_hash
        } else {
            symbol_hash_before
        };
        let change_type = params
            .get("change_type")
            .and_then(|v| v.as_str())
            .unwrap_or("modified");
        let ts = task_now_ts();

        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO task_symbol_changes (task_id, file_path, symbol_hash_after, symbol_hash_before, change_type, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![task_id, file_path, hash_after, symbol_hash_before, change_type, ts],
        ).map_err(|e| DaemonRpcError::internal_error(format!("写入 task_symbol_changes 失败: {}", e)))?;

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert(
            "file_path".to_string(),
            Value::String(file_path.to_string()),
        );
        res.insert("recorded".to_string(), Value::Bool(true));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    pub fn handle_task_link_edit_audit_symbols(
        &self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(cached) = self.check_dedup(params) {
            return Ok(cached);
        }
        let _ = peer;
        let audit_id = params
            .get("audit_id")
            .and_then(|v| v.as_i64())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 audit_id"))?;
        let step_id = params.get("step_id").and_then(|v| v.as_str()).unwrap_or("");

        let conn = self.conn.lock().unwrap();

        // 1. 查 file_edit_audit（对齐 Python link_edit_audit_symbols）
        let audit = conn
            .query_row(
                "SELECT workspace_id, file_path, file_hash_before, file_hash_after, agent_task_id
                 FROM file_edit_audit WHERE id = ?1",
                params![audit_id],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, String>(4)?,
                    ))
                },
            )
            .optional()
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 file_edit_audit 失败: {}", e))
            })?
            .ok_or_else(|| {
                DaemonRpcError::invalid_params(format!("edit audit {} 不存在", audit_id))
            })?;

        let (workspace_id, file_path, hash_before, hash_after, task_id) = audit;
        if task_id.is_empty() {
            let mut res = Map::new();
            res.insert("success".to_string(), Value::Bool(false));
            res.insert("linked".to_string(), serde_json::json!(0));
            res.insert(
                "error".to_string(),
                Value::String("edit audit has no task id".to_string()),
            );
            return Ok(Value::Object(res));
        }
        let step_id = if step_id.is_empty() {
            Self::infer_in_progress_step_id(&conn, &task_id).unwrap_or_default()
        } else {
            step_id.to_string()
        };

        // 2. 匹配 before/after 文件版本（hash 精确匹配 + 位置回退）
        let before_version_id =
            Self::file_version_for_hash(&conn, workspace_id, &file_path, &hash_before, "before");
        let after_version_id =
            Self::file_version_for_hash(&conn, workspace_id, &file_path, &hash_after, "after");
        if before_version_id.is_none() && after_version_id.is_none() {
            let mut res = Map::new();
            res.insert("success".to_string(), Value::Bool(false));
            res.insert("linked".to_string(), serde_json::json!(0));
            res.insert(
                "error".to_string(),
                Value::String("file versions not found; refresh graph first".to_string()),
            );
            return Ok(Value::Object(res));
        }

        // 3. 符号版本快照
        let before = Self::symbols_for_file_version(&conn, before_version_id)?;
        let after = Self::symbols_for_file_version(&conn, after_version_id)?;
        let mut names: Vec<&String> = before.keys().chain(after.keys()).collect();
        names.sort();
        names.dedup();

        // 4. 逐符号对比写入 task_symbol_changes + audit_chain 签名
        let mut linked: Vec<Value> = Vec::new();
        let ts = task_now_ts();
        for qualified_name in names {
            let before_sym = before.get(qualified_name);
            let after_sym = after.get(qualified_name);
            let before_hash = before_sym.map(|s| s.1.as_str()).unwrap_or("");
            let after_hash = after_sym.map(|s| s.1.as_str()).unwrap_or("");
            if before_hash == after_hash {
                continue;
            }
            let change_type = if before_sym.is_some() && after_sym.is_some() {
                "modified"
            } else if after_sym.is_some() {
                "added"
            } else {
                "deleted"
            };
            let symbol_name = after_sym
                .or(before_sym)
                .map(|s| s.0.clone())
                .unwrap_or_default();
            let metadata = serde_json::json!({
                "file_hash_before": hash_before,
                "file_hash_after": hash_after,
                "before_file_version_id": before_version_id.unwrap_or(0),
                "after_file_version_id": after_version_id.unwrap_or(0),
            });
            let row_id = conn
                .execute(
                    "INSERT INTO task_symbol_changes(
                         workspace_id, task_id, step_id, edit_audit_id, change_audit_id, file_path,
                         qualified_name, symbol_name, symbol_hash_before, symbol_hash_after,
                         change_type, source, source_commit_hash, metadata, created_at
                     ) VALUES (?1, ?2, ?3, ?4, '', ?5, ?6, ?7, ?8, ?9, ?10,
                               'edit_audit_symbol_diff', '', ?11, ?12)",
                    params![
                        workspace_id,
                        task_id,
                        step_id,
                        audit_id,
                        file_path,
                        qualified_name,
                        symbol_name,
                        before_hash,
                        after_hash,
                        change_type,
                        metadata.to_string(),
                        ts
                    ],
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("写入 task_symbol_changes 失败: {}", e))
                })?;

            // 审计链签名（失败不吞错）
            crate::cli::task::sign_audit_chain(
                &conn,
                "task_symbol_changes",
                &row_id.to_string(),
                &serde_json::json!({
                    "task_id": task_id,
                    "step_id": step_id,
                    "edit_audit_id": audit_id,
                    "change_audit_id": "",
                    "file_path": file_path,
                    "qualified_name": qualified_name,
                    "symbol_name": symbol_name,
                    "symbol_hash_before": before_hash,
                    "symbol_hash_after": after_hash,
                    "change_type": change_type,
                    "source": "edit_audit_symbol_diff",
                    "metadata": metadata,
                }),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("符号归因审计签名失败: {}", e)))?;

            let mut item = Map::new();
            item.insert("id".to_string(), serde_json::json!(row_id));
            item.insert(
                "qualified_name".to_string(),
                Value::String(qualified_name.clone()),
            );
            item.insert(
                "change_type".to_string(),
                Value::String(change_type.to_string()),
            );
            linked.push(Value::Object(item));
        }

        let mut res = Map::new();
        res.insert("success".to_string(), Value::Bool(true));
        res.insert("audit_id".to_string(), serde_json::json!(audit_id));
        res.insert("linked".to_string(), serde_json::json!(linked.len()));
        res.insert("changes".to_string(), Value::Array(linked));
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    /// 递归查任务树的 in_progress 步骤（对齐 Python db_task_attribution._infer_in_progress_step_id）
    fn infer_in_progress_step_id(conn: &Connection, task_id: &str) -> Option<String> {
        conn.query_row(
            "WITH RECURSIVE task_tree(id) AS (
                 SELECT id FROM tasks WHERE id = ?1
                 UNION ALL
                 SELECT t.id FROM tasks t JOIN task_tree tt ON t.parent_id = tt.id
             )
             SELECT ts.id FROM task_steps ts
             JOIN task_tree tt ON ts.task_id = tt.id
             WHERE ts.status = 'in_progress'
             ORDER BY ts.created_at DESC LIMIT 1",
            params![task_id],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .ok()
        .flatten()
    }

    /// 按文件 hash 查找文件版本 ID（对齐 Python db_task_attribution._file_version_for_hash）
    /// 精确 hash 匹配失败后按版本位置回退（before=is_current 0 / after=is_current 1）。
    fn file_version_for_hash(
        conn: &Connection,
        workspace_id: i64,
        file_path: &str,
        file_hash: &str,
        position: &str,
    ) -> Option<i64> {
        if !file_hash.is_empty() {
            let exact: Option<i64> = conn
                .query_row(
                    "SELECT fv.id FROM file_versions fv
                     JOIN file_instances fi ON fv.file_instance_id = fi.id
                     WHERE fi.workspace_id = ?1 AND fi.rel_path = ?2 AND fv.content_hash = ?3
                     ORDER BY fv.parsed_at DESC, fv.id DESC LIMIT 1",
                    params![workspace_id, file_path, file_hash],
                    |row| row.get(0),
                )
                .ok();
            if exact.is_some() {
                return exact;
            }
        }
        if position == "before" || position == "after" {
            let is_current = if position == "after" { 1 } else { 0 };
            return conn
                .query_row(
                    "SELECT fv.id FROM file_versions fv
                     JOIN file_instances fi ON fv.file_instance_id = fi.id
                     WHERE fi.workspace_id = ?1 AND fi.rel_path = ?2 AND fv.is_current = ?3
                     ORDER BY fv.version_num DESC LIMIT 1",
                    params![workspace_id, file_path, is_current],
                    |row| row.get(0),
                )
                .ok();
        }
        None
    }

    /// 查询文件版本的符号集合（对齐 Python db_task_attribution._symbols_for_file_version）
    /// 返回 qualified_name -> (symbol_name, symbol_hash)。
    fn symbols_for_file_version(
        conn: &Connection,
        version_id: Option<i64>,
    ) -> Result<HashMap<String, (String, String)>, DaemonRpcError> {
        let mut map = HashMap::new();
        if let Some(vid) = version_id {
            let mut stmt = conn
                .prepare(
                    "SELECT fsv.qualified_name, fsv.symbol_hash, sc.name
                     FROM file_symbol_versions fsv
                     LEFT JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
                     WHERE fsv.file_version_id = ?1 AND fsv.is_deleted = 0",
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("准备符号版本查询失败: {}", e))
                })?;
            let rows = stmt
                .query_map(params![vid], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                })
                .map_err(|e| DaemonRpcError::internal_error(format!("查询符号版本失败: {}", e)))?;
            for item in rows.flatten() {
                map.insert(item.0, (item.2, item.1));
            }
        }
        Ok(map)
    }

}
