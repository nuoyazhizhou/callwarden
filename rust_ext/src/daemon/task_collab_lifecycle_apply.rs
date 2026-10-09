//! 任务交付生命周期域：apply、close 与 capture diff。
//! 保留原有 lease、子任务门禁、证据和事务语义。

use super::*;

/// S4: verdict ledger 门禁（T-1790563271814-14566fa4）——「独立复审 = 关闭门禁」。
///
/// review 态直接 apply/close（未经 apply 的 review→closed 跳变）必须满足其一：
/// 1. `task_verdict_events` 存在该任务的 `overall='pass'` verdict 入账；
/// 2. 请求携带显式豁免 `verdict_waiver.reason`（非空），豁免由调用方写入
///    task_events（reason_code='verdict_waiver'）供审计，绝不静默放行。
/// 两者皆无 → `E_VERDICT_REQUIRED` fail-closed（在任何写入前拒绝）。
///
/// 刻意只拦 `current_status == "review"`：applied→closed 的 close 已在 apply
/// 阶段把过关；cascade_close 的聚合收尾属系统路径（本就不查 verdict，见其文档）。
/// 返回 Ok(Some(reason)) = 显式豁免生效（调用方需落账）；Ok(None) = verdict 已入账
/// 或不在门禁范围。
fn require_verdict_or_waiver(
    tx: &rusqlite::Transaction<'_>,
    task_id: &str,
    current_status: &str,
    params: &Value,
) -> Result<Option<String>, DaemonRpcError> {
    if current_status != "review" {
        return Ok(None);
    }
    let pass_verdicts: i64 = tx
        .query_row(
            "SELECT COUNT(*) FROM task_verdict_events \
             WHERE task_id = ?1 AND overall = 'pass'",
            params![task_id],
            |r| r.get(0),
        )
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("verdict 门禁查询失败: {}", e))
        })?;
    if pass_verdicts > 0 {
        return Ok(None);
    }
    let waiver_reason = params
        .get("verdict_waiver")
        .and_then(|v| v.get("reason"))
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string);
    match waiver_reason {
        Some(reason) => Ok(Some(reason)),
        None => Err(DaemonRpcError::new(
            "E_VERDICT_REQUIRED",
            format!(
                "任务 {} 处于 review 态且无 reviewer pass verdict 入账，禁止 apply/close；\
                 如确需豁免请携带 verdict_waiver.reason（将写入 task_events 供审计）",
                task_id
            ),
        )),
    }
}

/// 把显式豁免写入 task_events（审计轨迹：谁在什么状态下豁免了 verdict 门禁）。
fn record_verdict_waiver_event(
    store: &TaskCollabStore,
    tx: &rusqlite::Transaction<'_>,
    task_id: &str,
    current_status: &str,
    target_status: &str,
    reason: &str,
    owner_key: &str,
    role: &str,
    ts: f64,
) -> Result<(), DaemonRpcError> {
    let seq = store.next_seq();
    tx.execute(
        "INSERT INTO task_events
         (task_id, from_status, to_status, reason_code, reason, actor_identity, role, monotonic_seq, authoritative_timestamp)
         VALUES (?1, ?2, ?3, 'verdict_waiver', ?4, ?5, ?6, ?7, ?8)",
        params![
            task_id,
            current_status,
            target_status,
            format!("verdict waiver: {}", reason),
            owner_key,
            role,
            seq,
            ts
        ],
    )
    .map_err(|e| {
        DaemonRpcError::internal_error(format!("verdict_waiver 事件写入失败: {}", e))
    })?;
    Ok(())
}

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
        // S4: verdict ledger 门禁（T-1790563271814-14566fa4）——review 态 apply 必须
        // 有 pass verdict 入账或显式豁免；豁免先落账再跳变（任何写入前已 fail-closed）。
        let verdict_waiver =
            require_verdict_or_waiver(&tx, task_id, &current_status, params)?;
        if let Some(ref reason) = verdict_waiver {
            record_verdict_waiver_event(
                self,
                &tx,
                task_id,
                &current_status,
                "applied",
                reason,
                &owner_key,
                identity.as_ref().map(|id| id.role.as_str()).unwrap_or(""),
                ts,
            )?;
        }
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
        if verdict_waiver.is_some() {
            res.insert("verdict_waived".to_string(), Value::Bool(true));
        }
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

        // S4: verdict ledger 门禁（T-1790563271814-14566fa4）——review 态直接 close
        // （绕过 apply）必须有 pass verdict 入账或显式豁免；豁免先落账再跳变。
        // 刻意排在 S1/S2 之后：结构门禁（子任务/步骤）先行，verdict 凭据最后把关。
        // from applied 的 close 已被 apply 阶段的 S4 把关，不重复拦截。
        let verdict_waiver =
            require_verdict_or_waiver(&tx, task_id, &current_status, params)?;
        if let Some(ref reason) = verdict_waiver {
            record_verdict_waiver_event(
                self,
                &tx,
                task_id,
                &current_status,
                "closed",
                reason,
                &owner_key,
                identity.as_ref().map(|id| id.role.as_str()).unwrap_or(""),
                ts,
            )?;
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
        if verdict_waiver.is_some() {
            res.insert("verdict_waived".to_string(), Value::Bool(true));
        }
        let val = Value::Object(res);
        self.save_dedup(params, &val);
        Ok(val)
    }

    /// `task.cascade_close` —— 聚合节点级联收尾（树干=纯聚合投影，审计点只在叶子）。
    ///
    /// 架构语义（2026-08-29 用户定调）：功能都在叶子/枝条上，树干不应有独立审计点；
    /// 子树全 closed 即树干 closed。`handle_task_close` 对聚合节点（有子任务）本就不做
    /// verdict 校验（S1 只查子任务全 closed + reviewer lease）——树干卡 review 的根因是
    /// 派工层把聚合节点当普通任务路由到独立生命周期（缺 identity_policy 即 BLOCKED）。
    ///
    /// 本 RPC 由 coordinator（系统收尾者）调用，对 task_id 向上递归：
    /// 1. 直接子任务全 closed（递归到根）→ 该节点可聚合收尾；
    /// 2. 节点 contract 缺 identity_policy → 自动追加 revision 补 `legacy_identity_v1`
    ///    （复用 `append_task_contract_revision`，不再依赖手工 contract_revise 四步）；
    /// 3. 写 closed（系统权威，reason_code=cascade_closed，actor=coordinator）；
    /// 4. 递归向上直到根或遇到未收尾节点。
    ///
    /// **被替代卡豁免（GOV-FIX-03）**：叶子节点若存在 supersede 关系（即已被后继卡
    /// 收编），其 `pending`/`failed` 步骤已被后继卡 scope 承接、属作废遗留，不再阻塞
    /// 收尾；此时 `reason` 记为 `superseded by <superseding_task_id>（leaf steps waived）`。
    /// 动因：`task.supersede` 按设计只写关系不改 status（见 `task_supersede.rs`），
    /// 而缺合同的裸卡又无法走 close S2 / `task.step.resolve`（后者只吃 `failed` 步），
    /// 若不豁免则被替代裸卡永久 open 且无任何 daemon 写路径可收尾。
    ///
    /// 幂等：已 closed 节点跳过；返回实际收尾的节点清单。
    ///
    /// **拒绝语义（GOV-FIX-06）**：门禁不满足时不再静默返回空 `closed`，响应携带
    /// `target_closed`（目标卡本次是否已收尾，含幂等重提）与 `blocked`（首个未满足
    /// 条件的节点：`children_not_closed` 带未闭子卡数 / `leaf_steps_pending` 带未完成
    /// 步数），供 CLI 与脚本以 RC=2 显式拒绝，杜绝“✓ Task closed 但 status 未变”的
    /// 假成功。
    pub fn handle_task_cascade_close(
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
        let identity = parse_action_identity(params)?;
        let owner_key = peer.owner_key();
        let ts = task_now_ts();
        let workspace_id = optional_workspace_id_param(params).unwrap_or(0);

        // 收集祖先链（含自身）：task_id → parent → ... → root
        let mut chain: Vec<String> = Vec::new();
        {
            let conn = self.conn.lock().unwrap();
            let mut cur = task_id.to_string();
            loop {
                chain.push(cur.clone());
                let parent: Option<String> = conn
                    .query_row(
                        "SELECT parent_id FROM tasks WHERE id = ?1",
                        params![cur],
                        |r| r.get(0),
                    )
                    .optional()
                    .map_err(|e| {
                        DaemonRpcError::internal_error(format!("查询父任务失败: {e}"))
                    })?;
                match parent {
                    Some(p) if !p.is_empty() && p != cur => cur = p,
                    _ => break,
                }
            }
        }

        // 自底向上逐节点聚合判定 + 收尾（chain=[task,parent,...,root] 原序：
        // 从调用方 task_id 开始，先关自身（叶子：步骤 done；聚合：子全 closed），
        // 再逐级向上。切勿 rev()——否则先处理 root 会在自身未满足条件时 break。）
        let mut closed: Vec<String> = Vec::new();
        let mut skipped: Vec<String> = Vec::new();
        // GOV-FIX-06：门禁拒绝语义。break 时记录首个未满足条件的节点，
        // 让 CLI/脚本可程序化判定“请求成功但目标卡未被收尾”（此前返回
        // closed=[] 却仍是 ok，CLI 打印 ✓ Task closed 且 RC=0 —— 假成功缺陷）。
        let mut blocked: Option<Value> = None;
        let mut conn = self.conn.lock().unwrap();
        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启级联事务失败: {e}")))?;

        for node in chain.iter() {
            // 已 closed → 跳过（幂等）
            let node_status: String = tx
                .query_row("SELECT status FROM tasks WHERE id = ?1", params![node], |r| {
                    r.get(0)
                })
                .map_err(|_| DaemonRpcError::new("task_not_found", format!("任务不存在: {node}")))?;
            if node_status == "closed" {
                skipped.push(node.clone());
                continue;
            }

            // 被替代卡（supersede 收编）判定：存在出边即视为已由后继卡承接 scope。
            let superseded_by: Option<String> = tx
                .query_row(
                    "SELECT superseding_task_id FROM task_supersede_relations \
                     WHERE superseded_task_id = ?1 \
                     ORDER BY authoritative_timestamp DESC LIMIT 1",
                    params![node],
                    |r| r.get(0),
                )
                .optional()
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("查询 supersede 关系失败: {e}"))
                })?;

            // S1: 直接子任务全 closed（聚合判定核心）
            let child_total: i64 = tx
                .query_row(
                    "SELECT COUNT(*) FROM tasks WHERE parent_id = ?1",
                    params![node],
                    |r| r.get(0),
                )
                .unwrap_or(0);
            if child_total > 0 {
                let open_children: i64 = tx
                    .query_row(
                        "SELECT COUNT(*) FROM tasks WHERE parent_id = ?1 AND status != 'closed'",
                        params![node],
                        |r| r.get(0),
                    )
                    .unwrap_or(0);
                if open_children > 0 {
                    // 子树未全 closed → 停止级联（不跳过，直接 break 保留现场）
                    blocked = Some(serde_json::json!({
                        "task_id": node,
                        "reason": "children_not_closed",
                        "open_children": open_children,
                    }));
                    break;
                }
            } else {
                // 叶子节点：步骤必须全 done（复用 close S2 语义）。
                // 被替代卡豁免：步骤已由后继卡 scope 承接，属作废遗留，不阻塞收尾。
                let step_count: i64 = tx
                    .query_row(
                        "SELECT COUNT(*) FROM task_steps WHERE task_id = ?1",
                        params![node],
                        |r| r.get(0),
                    )
                    .unwrap_or(0);
                if step_count > 0 && superseded_by.is_none() {
                    let not_done: i64 = tx
                        .query_row(
                            "SELECT COUNT(*) FROM task_steps WHERE task_id = ?1 AND status IN ('pending', 'blocked')",
                            params![node],
                            |r| r.get(0),
                        )
                        .unwrap_or(0);
                    let unresolved_failed =
                        crate::daemon::task_loop::next_action::unresolved_failed_step_ids(&tx, node)?
                            .len() as i64;
                    if not_done + unresolved_failed > 0 {
                        blocked = Some(serde_json::json!({
                            "task_id": node,
                            "reason": "leaf_steps_pending",
                            "not_done_steps": not_done,
                            "unresolved_failed_steps": unresolved_failed,
                        }));
                        break;
                    }
                }
            }

            // 自动补 contract：缺 identity_policy → 追加 revision（legacy 默认）
            let has_policy: bool = tx
                .query_row(
                    "SELECT 1 FROM task_contract_revisions WHERE task_id = ?1 \
                     AND envelope_payload LIKE '%identity_policy%' ORDER BY revision DESC LIMIT 1",
                    params![node],
                    |_| Ok(()),
                )
                .optional()
                .map_err(|e| DaemonRpcError::internal_error(format!("查询 contract 失败: {e}")))?
                .is_some();
            if !has_policy {
                // 读最新 revision 构造 rev+1
                let current: Option<(i64, String, String)> = tx
                    .query_row(
                        "SELECT revision, contract_hash, envelope_payload \
                         FROM task_contract_revisions WHERE task_id = ?1 ORDER BY revision DESC LIMIT 1",
                        params![node],
                        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                    )
                    .optional()
                    .map_err(|e| {
                        DaemonRpcError::internal_error(format!("读取 contract 失败: {e}"))
                    })?;
                if let Some((cur_rev, cur_hash, payload)) = current {
                    let mut env: Value = serde_json::from_str(&payload).unwrap_or(Value::Null);
                    if let Some(obj) = env.as_object_mut() {
                        obj.insert("revision".into(), Value::Number(serde_json::Number::from(cur_rev + 1)));
                        obj.insert("supersedes_revision".into(), Value::Number(serde_json::Number::from(cur_rev)));
                        obj.insert("supersedes_contract_hash".into(), Value::String(cur_hash.clone()));
                        obj.insert("identity_policy".into(), Value::String("legacy_identity_v1".into()));
                        obj.insert(
                            "source_provenance".into(),
                            Value::String(
                                "task.cascade_close 自动补齐 identity_policy（聚合节点收尾，树干=纯聚合投影）"
                                    .to_string(),
                            ),
                        );
                        obj.remove("contract_hash");
                        obj.remove("created_at");
                        obj.remove("created_by");
                        // task.create 生成的 envelope 可能不含 objective（历史任务尤其如此）；
                        // append_task_contract_revision 强校验 objective 非空字符串，
                        // 缺失时补兜底文案，保证 autofill 构造出的 envelope 合法。
                        let objective_ok = obj
                            .get("objective")
                            .and_then(|v| v.as_str())
                            .map(|s| !s.is_empty())
                            .unwrap_or(false);
                        if !objective_ok {
                            obj.insert(
                                "objective".into(),
                                Value::String(
                                    "cascade_close 聚合收尾（子树全 closed，自动补齐 contract）"
                                        .to_string(),
                                ),
                            );
                        }
                        // append_task_contract_revision 强校验：profile 合法枚举 +
                        // 六类数组字段必须为非空 JSON array（空数组同样拒绝）。
                        let profile_ok = obj
                            .get("profile")
                            .and_then(|v| v.as_str())
                            .map(|s| {
                                matches!(
                                    s.trim(),
                                    "research" | "design" | "code_change" | "high_risk" | "review"
                                )
                            })
                            .unwrap_or(false);
                        if !profile_ok {
                            obj.insert("profile".into(), Value::String("review".into()));
                        }
                        for key in [
                            "interfaces",
                            "allowed_edit_scope",
                            "acceptance_clauses",
                            "risks",
                            "rollback",
                            "dependencies",
                        ] {
                            let array_ok = obj
                                .get(key)
                                .and_then(Value::as_array)
                                .map(|a| {
                                    !a.is_empty()
                                        && a.iter().all(|v| {
                                            v.as_str()
                                                .map(str::trim)
                                                .filter(|s| !s.is_empty())
                                                .is_some()
                                        })
                                })
                                .unwrap_or(false);
                            if !array_ok {
                                obj.insert(
                                    key.into(),
                                    Value::Array(vec![Value::String(
                                        "cascade_close 聚合收尾（自动补齐）".to_string(),
                                    )]),
                                );
                            }
                        }
                    }
                    crate::daemon::task_loop::task_contract_revise::append_task_contract_revision(
                        &tx,
                        &crate::daemon::task_loop::task_contract_revise::ContractReviseInput {
                            task_id: node.clone(),
                            envelope: env,
                            expected_previous_hash: cur_hash,
                            created_by: owner_key.clone(),
                        },
                        workspace_id,
                    )
                    .map_err(|e| {
                        DaemonRpcError::internal_error(format!("自动 contract revise 失败: {e}"))
                    })?;
                }
            }

            // 系统权威 close（聚合投影，无独立 verdict）
            tx.execute(
                "UPDATE tasks SET status = 'closed', closed_at = ?1, updated_at = ?1 WHERE id = ?2",
                params![ts, node],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("级联 close 失败: {e}")))?;
            let seq = self.next_seq();
            let close_reason = match superseded_by.as_deref() {
                Some(succ) => format!("superseded by {succ} (leaf steps waived)"),
                None => "subtree aggregate closed".to_string(),
            };
            tx.execute(
                "INSERT INTO task_events
                 (task_id, from_status, to_status, reason_code, reason, actor_identity, role, monotonic_seq, authoritative_timestamp)
                 VALUES (?1, ?2, 'closed', 'cascade_closed', ?3, ?4, ?5, ?6, ?7)",
                params![
                    node,
                    node_status,
                    close_reason,
                    owner_key,
                    identity.as_ref().map(|id| id.role.as_str()).unwrap_or("coordinator"),
                    seq,
                    ts
                ],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("级联事件写入失败: {e}")))?;
            closed.push(node.clone());
        }

        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交级联事务失败: {e}"))
        })?;

        // GOV-FIX-06：目标卡真实收尾判定（含幂等重提：已在 skipped 亦算 closed）。
        let target_closed = closed.iter().any(|n| n == task_id) || skipped.iter().any(|n| n == task_id);
        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        res.insert(
            "closed".to_string(),
            Value::Array(closed.into_iter().map(Value::String).collect()),
        );
        res.insert(
            "skipped".to_string(),
            Value::Array(skipped.into_iter().map(Value::String).collect()),
        );
        res.insert("target_closed".to_string(), Value::Bool(target_closed));
        res.insert(
            "blocked".to_string(),
            blocked.unwrap_or(Value::Null),
        );
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

        // B5（审计 20261009）：daemon 进程 cwd 不属于任何注册工作区，直接把
        // Path::new("") 传给 capture_task_diff 时，内部 active_workspace 会因
        // 多 active workspace 歧义 fail-closed。task_workspace_bindings 绑定
        // 才是任务的工作区权威身份源：显式解析绑定 root_path 传入，走显式
        // workspace 优先路径。
        let mut ws_roots: Vec<String> = conn
            .prepare(
                "SELECT DISTINCT w.root_path FROM workspaces w \
                 JOIN task_workspace_bindings b ON b.workspace_id = w.id \
                 WHERE b.task_id = ?1 ORDER BY w.root_path",
            )
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询任务工作区绑定 prepare 失败: {}", e))
            })?
            .query_map(rusqlite::params![task_id], |r| r.get::<_, String>(0))
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询任务工作区绑定失败: {}", e))
            })?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("读取任务工作区绑定失败: {}", e))
            })?;
        if ws_roots.len() != 1 {
            return Err(DaemonRpcError::invalid_params(format!(
                "任务 {} 的工作区绑定数为 {}（需要恰好 1 个），无法定位 capture-diff 工作区",
                task_id,
                ws_roots.len()
            )));
        }
        let ws_root = ws_roots.remove(0);
        if ws_root.trim().is_empty() {
            return Err(DaemonRpcError::invalid_params(format!(
                "任务 {} 绑定的工作区 root_path 为空，无法执行 capture-diff",
                task_id
            )));
        }

        // 完整 capture-diff：change_audit（真实 schema）+ task_symbol_changes + audit_chain 签名
        let result = crate::cli::task::capture_task_diff(
            &mut conn,
            task_id,
            step_id,
            Path::new(&ws_root),
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
