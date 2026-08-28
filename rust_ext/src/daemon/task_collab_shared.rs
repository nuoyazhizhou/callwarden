//! task_collab 跨领域共享辅助函数和 schema 校验清单。
//! 仅承载纯 helper/解析/通用校验，不承载 RPC handler。

use super::*;
pub(crate) fn canonical_json_sha256(value: &Value) -> String {
    fn sort_keys(value: &Value) -> Value {
        match value {
            Value::Object(map) => {
                let mut keys: Vec<&String> = map.keys().collect();
                keys.sort();
                let mut out = Map::new();
                for key in keys {
                    out.insert(key.clone(), sort_keys(&map[key]));
                }
                Value::Object(out)
            }
            Value::Array(items) => Value::Array(items.iter().map(sort_keys).collect()),
            other => other.clone(),
        }
    }
    let sorted = sort_keys(value);
    // serde_json 紧凑序列化（无空格）；ensure_ascii=false 等价于 UTF-8 直出
    let canonical = serde_json::to_string(&sorted).unwrap_or_default();
    sha256_hex(canonical.as_bytes())
}

/// daemon 实际读写依赖的列（官方 v49 schema 权威清单，db/schema.py）。
/// 迁移后只读校验这些列存在，防止历史库缺列导致 daemon 查询失败；
/// 该清单不含旁路扩展列（tasks.claimed_by/claimed_at/workspace_id、task_steps.step_number），
/// 因为这些列 daemon 从不读写。
pub(crate) const TASK_COLLAB_COLUMNS: &[(&str, &[&str])] = &[
    (
        "tasks",
        &[
            "id",
            "title",
            "description",
            "creator",
            "status",
            "created_at",
            "updated_at",
            "parent_id",
        ],
    ),
    (
        "task_steps",
        &[
            "id",
            "step_index",
            "action",
            "target_file",
            "target_symbol",
            "check_items",
            "status",
            "result",
            "created_at",
            "completed_at",
        ],
    ),
    (
        "task_events",
        &[
            "task_id",
            "workspace_id",
            "from_status",
            "to_status",
            "reason_code",
            "reason",
            "actor_identity",
            "agent_session_id",
            "role",
            "contract_hash",
            "snapshot_id",
            "monotonic_seq",
            "authoritative_timestamp",
            "evidence_path",
            "evidence_hash",
        ],
    ),
    (
        "agent_registrations",
        &[
            "agent_id",
            "agent_name",
            "owner_key",
            "capabilities",
            "registered_at",
            "last_heartbeat",
            "status",
        ],
    ),
    (
        "action_identities",
        &[
            "workspace_id",
            "action_id",
            "action_type",
            "task_id",
            "agent_id",
            "session_id",
            "model_id",
            "role",
            "recorded_at",
        ],
    ),
];

pub(crate) fn parse_action_identity(
    params: &Value,
) -> Result<Option<ActionIdentity>, DaemonRpcError> {
    let Some(raw) = params.get("identity") else {
        return Ok(None);
    };
    if raw.is_null() || raw.as_str().map(|s| s.trim().is_empty()).unwrap_or(false) {
        return Ok(None);
    }
    let value = if let Some(text) = raw.as_str() {
        serde_json::from_str::<Value>(text).map_err(|_| {
            DaemonRpcError::new("E_IDENTITY_INCOMPLETE", "identity 必须是 JSON 对象")
        })?
    } else {
        raw.clone()
    };
    let object = value
        .as_object()
        .ok_or_else(|| DaemonRpcError::new("E_IDENTITY_INCOMPLETE", "identity 必须是 JSON 对象"))?;
    let field = |name: &str| {
        object
            .get(name)
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string()
    };
    let identity = ActionIdentity {
        agent_id: field("agent_id"),
        agent_instance_id: field("agent_instance_id"),
        client_id: field("client_id"),
        provider: field("provider"),
        model_id: field("model_id"),
        model_mode: field("model_mode"),
        system_fingerprint: field("system_fingerprint"),
        session_id: field("session_id"),
        role: field("role"),
        runtime_hash: field("runtime_hash"),
    };
    if identity.agent_id.is_empty()
        || identity.session_id.is_empty()
        || identity.model_id.is_empty()
        || identity.role.is_empty()
    {
        return Err(DaemonRpcError::new(
            "E_IDENTITY_INCOMPLETE",
            "identity 必须同时包含 agent_id/session_id/model_id/role",
        ));
    }
    Ok(Some(identity))
}

pub(crate) fn record_action_identity(
    tx: &Transaction<'_>,
    task_id: &str,
    identity: &ActionIdentity,
    action_type: &str,
    seq: i64,
    ts: f64,
) -> Result<(), DaemonRpcError> {
    // provenance 归属优先取任务不可变 binding（多 workspace 下不会记到别的项目）；
    // 无 binding 的 legacy 任务回退到 active workspace（fail-closed：无 active 拒绝）。
    let workspace_id = task_workspace_id_or_active(tx, task_id)?;
    let action_id = format!("ACT-daemon-{}-{}-{}", action_type, task_id, seq);
    tx.execute(
        "INSERT INTO action_identities
         (workspace_id, action_id, action_type, task_id, contract_id, contract_revision,
          agent_id, session_id, model_id, role, recorded_at)
         VALUES (?1, ?2, ?3, ?4, '', 0, ?5, ?6, ?7, ?8, ?9)",
        params![
            workspace_id,
            action_id,
            action_type,
            task_id,
            identity.agent_id,
            identity.session_id,
            identity.model_id,
            identity.role,
            ts
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("记录 action identity 失败: {}", e)))?;
    Ok(())
}

pub(crate) fn task_now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

// 兼容本模块既有内部调用；跨模块只使用 task_now_ts。
pub(crate) fn now_ts() -> f64 {
    task_now_ts()
}

/// 返回已由 append-only `step_resolved` 事件解析的 failed step 集合。
///
/// failed step 自身保持不可变；生命周期投影只消费精确 JSON 字段，禁止用
/// `LIKE` 猜测 resolution，避免 step id 前缀/转义造成误判。
pub(crate) fn resolved_failed_step_ids(
    conn: &Connection,
    task_id: &str,
) -> Result<HashSet<String>, DaemonRpcError> {
    let mut stmt = conn
        .prepare(
            "SELECT reason FROM task_events
             WHERE task_id = ?1 AND reason_code = 'step_resolved'
             ORDER BY event_id ASC",
        )
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("查询 resolution ledger 失败: {}", e))
        })?;
    let rows = stmt
        .query_map(params![task_id], |row| row.get::<_, String>(0))
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("读取 resolution ledger 失败: {}", e))
        })?;
    let mut resolved = HashSet::new();
    for row in rows {
        let raw = row.map_err(|e| {
            DaemonRpcError::internal_error(format!("读取 resolution event 失败: {}", e))
        })?;
        if let Ok(value) = serde_json::from_str::<Value>(&raw) {
            if let Some(step_id) = value
                .get("failed_step_id")
                .and_then(|item| item.as_str())
                .filter(|item| !item.trim().is_empty())
            {
                resolved.insert(step_id.to_string());
            }
        }
    }
    Ok(resolved)
}

pub(crate) fn unresolved_failed_step_ids(
    conn: &Connection,
    task_id: &str,
) -> Result<Vec<String>, DaemonRpcError> {
    let resolved = resolved_failed_step_ids(conn, task_id)?;
    let mut stmt = conn
        .prepare(
            "SELECT id FROM task_steps
             WHERE task_id = ?1 AND status = 'failed'
             ORDER BY step_index ASC",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("查询 failed steps 失败: {}", e)))?;
    let rows = stmt
        .query_map(params![task_id], |row| row.get::<_, String>(0))
        .map_err(|e| DaemonRpcError::internal_error(format!("读取 failed steps 失败: {}", e)))?;
    let mut unresolved = Vec::new();
    for row in rows {
        let step_id = row
            .map_err(|e| DaemonRpcError::internal_error(format!("读取 failed step 失败: {}", e)))?;
        if !resolved.contains(&step_id) {
            unresolved.push(step_id);
        }
    }
    Ok(unresolved)
}

/// 找出必须显式领取的 remediation。Reviewer BLOCKED 生成的整改与未解析
/// failed step 的整改都优先于普通 pending step；已完成的历史整改不会重复命中。
pub(crate) fn required_remediation_step(
    conn: &Connection,
    task_id: &str,
) -> Result<Option<(String, Value)>, DaemonRpcError> {
    let unresolved: HashSet<String> = unresolved_failed_step_ids(conn, task_id)?
        .into_iter()
        .collect();
    let mut stmt = conn
        .prepare(
            "SELECT id, result FROM task_steps
             WHERE task_id = ?1 AND action = 'fix_defect'
               AND status IN ('pending', 'in_progress')
             ORDER BY step_index ASC",
        )
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("查询 remediation steps 失败: {}", e))
        })?;
    let rows = stmt
        .query_map(params![task_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("读取 remediation steps 失败: {}", e))
        })?;
    for row in rows {
        let (step_id, raw) = row.map_err(|e| {
            DaemonRpcError::internal_error(format!("读取 remediation step 失败: {}", e))
        })?;
        let metadata = serde_json::from_str::<Value>(&raw).unwrap_or(Value::Null);
        let linked = metadata
            .get("remediation_of_step_id")
            .and_then(|item| item.as_str())
            .unwrap_or("");
        let source_outcome = metadata
            .get("source_outcome")
            .and_then(|item| item.as_str())
            .unwrap_or("");
        if unresolved.contains(linked)
            || matches!(source_outcome, "reviewer_blocked" | "adjudicator_returned")
        {
            return Ok(Some((step_id, metadata)));
        }
    }
    Ok(None)
}

/// 超过该时间没有 agent heartbeat 的 claim 才允许由受保护的恢复入口释放。
/// 这是安全阈值，不是客户端可覆盖的参数；恢复仍须持有目标任务的 reviewer lease。
pub(crate) const ORPHAN_CLAIM_STALE_SECS: f64 = 15.0 * 60.0;

/// 将遗留 runtime role 映射到治理层角色。
///
/// Executor 的 planner/implementer/tester/evidence 是同一执行角色的工作模式，
/// reviewer 与 independent_reviewer 也是同一审阅角色。claim 接管只允许同一
/// 治理角色之间发生，不能把 runtime 名称差异误判成跨角色恢复。
pub(crate) fn canonical_claim_role(role: &str) -> &str {
    match role.trim() {
        "executor" | "planner" | "implementer" | "tester" | "evidence" => "executor",
        "reviewer" | "independent_reviewer" => "reviewer",
        "adjudicator" => "adjudicator",
        other => other,
    }
}

pub(crate) fn rand_val() -> u32 {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    (ts & 0xffffffff) as u32
}

pub(crate) fn generate_task_id() -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    format!("T-{}-{:08x}", now, rand_val())
}

pub(crate) fn generate_step_id() -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    format!("S-{}-{:08x}", now, rand_val())
}

/// 生成 Lease raw token（Req 11.2：仅成功响应返回一次，DB 只存 sha256）。
///
/// 多路熵（纳秒时间戳 + 随机值 + 进程 PID）经双重 sha256 单向哈希，
/// 保证无法从数据库中的 token_hash 反推 raw token（对齐 Python `secrets.token_urlsafe(32)`）。
pub(crate) fn gen_lease_token() -> String {
    let raw = format!(
        "{}:{}:{}:{}",
        now_ts(),
        rand_val(),
        std::process::id(),
        rand_val()
    );
    sha256_hex(format!("{}:{}", raw, sha256_hex(raw.as_bytes())).as_bytes())
}

/// 生成 Lease 唯一标识（对齐 Python `L-<uuid4.hex[:16]>` 格式）。
pub(crate) fn gen_lease_id() -> String {
    format!(
        "L-{}",
        &sha256_hex(format!("{}:{}", now_ts(), rand_val()).as_bytes())[..16]
    )
}

/// 生成 Lease 审计事件唯一标识（对齐 Python `EVT-<uuid4.hex[:16]>` 格式）。
pub(crate) fn gen_lease_event_id() -> String {
    format!(
        "EVT-{}",
        &sha256_hex(format!("{}:{}", now_ts(), rand_val()).as_bytes())[..16]
    )
}

/// 判断 rusqlite 错误是否为 UNIQUE 约束冲突（SQLITE_CONSTRAINT，code 19/2067）。
///
/// 用于 acquire 时捕获 `idx_task_leases_active_unique` 部分唯一索引冲突（Req 11.2 防双活）。
pub(crate) fn is_unique_violation(err: &rusqlite::Error) -> bool {
    matches!(
        err,
        rusqlite::Error::SqliteFailure(e, _) if e.code == rusqlite::ErrorCode::ConstraintViolation
    )
}

/// 获取当前活动 workspace 的 id（与 `record_action_identity` 同一绑定逻辑）。
///
/// fail-closed：没有 `is_active = 1` 的 workspace 时拒绝，绝不回退到“任意
/// workspace”（旧实现 `ORDER BY id LIMIT 1` 会在多 workspace 单库中把任务
/// 归属错配到其他项目，正是“工作区身份混串”的根因之一）。
pub(crate) fn active_workspace_id(conn: &Connection) -> Result<i64, DaemonRpcError> {
    conn.query_row(
        "SELECT id FROM workspaces WHERE is_active = 1 ORDER BY id LIMIT 1",
        [],
        |r| r.get(0),
    )
    .map_err(|_| {
        DaemonRpcError::new(
            "E_IDENTITY_NOT_WIRED",
            "没有 active workspace（is_active=1），拒绝推导 workspace；必须显式绑定",
        )
    })
}

/// 解析显式 `workspace_id` 参数（abi-error-code-contract.md：生产路径必须显式传入
/// `workspace_id > 0`，禁止用 active workspace / cwd / 客户端 numeric id 补齐）。
pub(crate) fn required_workspace_id_param(params: &Value) -> Result<i64, DaemonRpcError> {
    let raw = params.get("workspace_id").ok_or_else(|| {
        DaemonRpcError::new(
            "E_TASK_WORKSPACE_UNBOUND",
            "缺少显式 workspace_id（> 0）；生产路径禁止用 active workspace / cwd 补齐",
        )
    })?;
    let ws_id = if let Some(i) = raw.as_i64() {
        i
    } else if let Some(s) = raw.as_str() {
        s.trim().parse::<i64>().map_err(|_| {
            DaemonRpcError::new(
                "E_TASK_WORKSPACE_UNBOUND",
                format!("workspace_id 无法解析为整数: {}", s),
            )
        })?
    } else {
        return Err(DaemonRpcError::new(
            "E_TASK_WORKSPACE_UNBOUND",
            "workspace_id 必须是整数或数字字符串",
        ));
    };
    if ws_id <= 0 {
        return Err(DaemonRpcError::new(
            "E_TASK_WORKSPACE_UNBOUND",
            format!("workspace_id 必须 > 0，实际 {}", ws_id),
        ));
    }
    Ok(ws_id)
}

/// 可选解析 `workspace_id` 参数（None 表示未提供；用于与 binding 一致性校验）。
pub(crate) fn optional_workspace_id_param(params: &Value) -> Option<i64> {
    params
        .get("workspace_id")
        .and_then(|v| {
            v.as_i64()
                .or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok()))
        })
        .filter(|id| *id > 0)
}

/// 任务逻辑 workspace 只来自不可变 `task_workspace_bindings`
/// （cw-role-handoff-task-loop.md §8.1.1）。
///
/// - 无 binding → `E_TASK_WORKSPACE_UNBOUND` fail-closed（旧 task 保持无 binding，
///   v1 派工/lease 一律拒绝，绝不回退 active workspace 或客户端 numeric id）；
/// - 显式 requested 与 binding 不一致 → `E_WORKSPACE_AUTHORITY_MISMATCH`。
pub(crate) fn task_bound_workspace_id(
    conn: &Connection,
    task_id: &str,
    requested_workspace_id: Option<i64>,
) -> Result<i64, DaemonRpcError> {
    let bound: Option<i64> = conn
        .query_row(
            "SELECT workspace_id FROM task_workspace_bindings WHERE task_id = ?1",
            params![task_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("查询 task workspace binding 失败: {}", e))
        })?;
    let workspace_id = bound.ok_or_else(|| {
        DaemonRpcError::new(
            "E_TASK_WORKSPACE_UNBOUND",
            format!(
                "task={} 未绑定不可变 workspace（task_workspace_bindings 缺失），拒绝操作",
                task_id
            ),
        )
    })?;
    if let Some(requested) = requested_workspace_id {
        if requested != workspace_id {
            return Err(DaemonRpcError::new(
                "E_WORKSPACE_AUTHORITY_MISMATCH",
                format!(
                    "task={} 绑定 workspace={} 与请求 workspace={} 不一致",
                    task_id, workspace_id, requested
                ),
            ));
        }
    }
    Ok(workspace_id)
}

/// provenance/审计记录的 workspace 归属：优先不可变 binding（多 workspace 下不会把
/// 任务的动作记到其他项目的 workspace）；无 binding 的 legacy 任务回退到 active
/// workspace（fail-closed：无 active workspace 时拒绝，绝不回退到“任意 workspace”）。
pub(crate) fn task_workspace_id_or_active(conn: &Connection, task_id: &str) -> Result<i64, DaemonRpcError> {
    let bound: Option<i64> = conn
        .query_row(
            "SELECT workspace_id FROM task_workspace_bindings WHERE task_id = ?1",
            params![task_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("查询 task workspace binding 失败: {}", e))
        })?;
    if let Some(ws) = bound {
        return Ok(ws);
    }
    active_workspace_id(conn)
}

/// 权威 UTC 秒值文本（capture/binding 的 `authoritative_created_at`）。
pub(crate) fn authoritative_now_text() -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    format!("{}", ts)
}
