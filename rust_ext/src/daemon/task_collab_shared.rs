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

/// 为治理角色列出 task_leases 中可能出现的全部等价角色名（治理角色本身 +
/// runtime 别名）。历史行可能以 runtime 名称（implementer/independent_reviewer
/// 等）落库；C-24 存储侧归一后新行恒为治理角色。查找侧按此变体集匹配，
/// 同时兼容新旧。
pub(crate) fn runtime_role_variants(canonical: &str) -> Vec<String> {
    let variants: &[&str] = match canonical {
        "executor" => &["executor", "planner", "implementer", "tester", "evidence"],
        "reviewer" => &["reviewer", "independent_reviewer"],
        "adjudicator" => &["adjudicator"],
        _ => std::slice::from_ref(&canonical),
    };
    variants.iter().map(|s| (*s).to_string()).collect()
}

/// 生成 task_leases 的 role 兼容匹配片段与参数（C-24）。
/// 返回 `("role IN (?, ?, …)", variants)`；单变体时退化为 `role IN (?)`。
pub(crate) fn role_in_match(canonical: &str) -> (String, Vec<String>) {
    let variants = runtime_role_variants(canonical);
    let placeholders = (0..variants.len())
        .map(|_| "?")
        .collect::<Vec<_>>()
        .join(",");
    (format!("role IN ({placeholders})"), variants)
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

// ============================================================================
// GATE-1A：parent-aware governed task.create daemon hardening
// （docs/design/cw-role-prompt-compiler-v1-frozen-spec.md §13.3，frozen spec
//  SHA-256 95298729F3357CDBE76D8F8E91F12067B54D2661D6E80ABFF561A2E2A8C86CB7）。
// 仅承载纯校验 helper；不承载 RPC handler。
// ============================================================================

/// task.create frozen request contract 字段集合。
///
/// parent-aware 分支（parent_id 非空）启用严格字段校验：白名单之外的顶层字段
/// 一律拒绝（`E_TASK_CREATE_UNKNOWN_FIELD`）。root 分支（parent_id 缺失或
/// 规范化空）保持 Gate 前行为 bit-for-bit，**不**启用此检查。
pub(crate) const TASK_CREATE_FROZEN_FIELDS: &[&str] = &[
    "title",
    "description",
    "parent_id",
    "workspace_id",
    "workspace_instance_id",
    "steps",
    "role_contracts",
    "task_id",
    "task_contract_envelope",
    "identity_policy",
    "request_id",
];

/// GATE-1A：parent-aware 分支必须携带完整 Role Contracts（受治理 child 不允许裸建）。
pub(crate) const ERR_TASK_PARENT_CONTRACT_REQUIRED: &str = "E_TASK_PARENT_CONTRACT_REQUIRED";

/// GATE-1A：parent-aware 分支的未知字段拒绝（frozen request contract）。
pub(crate) fn reject_unknown_create_fields(
    params: &Value,
) -> Result<(), DaemonRpcError> {
    let Some(object) = params.as_object() else {
        return Ok(());
    };
    let mut unknown: Vec<&str> = object
        .keys()
        .filter(|key| !TASK_CREATE_FROZEN_FIELDS.contains(&key.as_str()))
        .map(|key| key.as_str())
        .collect();
    unknown.sort();
    if unknown.is_empty() {
        return Ok(());
    }
    Err(DaemonRpcError::new(
        "E_TASK_CREATE_UNKNOWN_FIELD",
        format!(
            "task.create parent-aware 请求包含未知字段: {}；frozen request contract 仅允许 {}",
            unknown.join(", "),
            TASK_CREATE_FROZEN_FIELDS.join(", ")
        ),
    ))
}

/// GATE-1A：parent 存在性与权威 binding/capture 校验（parent 分支）。
///
/// 在同一事务内读取（调用方持有事务写锁）：
/// - parent 任务必须存在（`E_TASK_PARENT_NOT_FOUND`）；
/// - parent 必须有**恰好一条**不可变 binding（`E_TASK_PARENT_UNBOUND` /
///   `E_TASK_PARENT_AMBIGUOUS_BINDING`），且该 binding 引用的 capture 必须存在
///   （`E_TASK_PARENT_CAPTURE_MISSING`）；
/// - child 请求的 workspace_id / workspace_instance_id 必须与 parent binding 的
///   workspace 及 capture 的 instance **精确一致**
///   （`E_WORKSPACE_AUTHORITY_MISMATCH`）。
///
/// 任何缺口由调用方整事务 rollback，不留部分行。
pub(crate) fn validate_parent_for_child_create(
    tx: &Transaction<'_>,
    parent_id: &str,
    requested_workspace_id: i64,
    requested_workspace_instance_id: &str,
    created_by: &str,
) -> Result<(), DaemonRpcError> {
    // 1. parent 存在。
    let parent_exists: i64 = tx
        .query_row(
            "SELECT COUNT(*) FROM tasks WHERE id = ?1",
            params![parent_id],
            |r| r.get(0),
        )
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("parent 存在性校验失败: {}", e))
        })?;
    if parent_exists == 0 {
        return Err(DaemonRpcError::new(
            "E_TASK_PARENT_NOT_FOUND",
            format!("task.create parent-aware：parent task {} 不存在", parent_id),
        ));
    }

    // 2. parent 唯一 binding（不可变 task→workspace 绑定必须恰好一条）。
    let mut stmt = tx
        .prepare(
            "SELECT workspace_id, workspace_binding_id, workspace_capture_id \
             FROM task_workspace_bindings WHERE task_id = ?1",
        )
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("parent binding 查询准备失败: {}", e))
        })?;
    let rows: Vec<(i64, String, String)> = stmt
        .query_map(params![parent_id], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("parent binding 读取失败: {}", e))
        })?
        .collect::<Result<_, _>>()
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("parent binding 行解析失败: {}", e))
        })?;
    if rows.is_empty() {
        return Err(DaemonRpcError::new(
            "E_TASK_PARENT_UNBOUND",
            format!(
                "task.create parent-aware：parent task {} 无不可变 workspace binding；\
                 必须先 attest 或绑定后才能创建 child",
                parent_id
            ),
        ));
    }
    if rows.len() > 1 {
        return Err(DaemonRpcError::new(
            "E_TASK_PARENT_AMBIGUOUS_BINDING",
            format!(
                "task.create parent-aware：parent task {} 存在 {} 条 binding，权威绑定必须唯一",
                parent_id,
                rows.len()
            ),
        ));
    }
    let (parent_workspace_id, _binding_id, capture_id) = &rows[0];

    // 3. child workspace 与 parent binding workspace 精确一致。
    if *parent_workspace_id != requested_workspace_id {
        return Err(DaemonRpcError::new(
            "E_WORKSPACE_AUTHORITY_MISMATCH",
            format!(
                "task.create parent-aware：parent task {} 绑定 workspace={}，\
                 child 请求 workspace={} 不一致",
                parent_id, parent_workspace_id, requested_workspace_id
            ),
        ));
    }

    // 4. capture 必须存在，且其 instance 与 child 请求精确一致。
    let parent_instance: Option<String> = tx
        .query_row(
            "SELECT workspace_instance_id FROM workspace_authority_captures \
             WHERE workspace_capture_id = ?1",
            params![capture_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("parent capture 读取失败: {}", e))
        })?;
    let parent_instance = parent_instance.ok_or_else(|| {
        DaemonRpcError::new(
            "E_TASK_PARENT_CAPTURE_MISSING",
            format!(
                "task.create parent-aware：parent task {} 的 capture {} 不存在",
                parent_id, capture_id
            ),
        )
    })?;
    if parent_instance != requested_workspace_instance_id {
        // T-1788595874892-c9b82244：legacy parent capture（ws-<digits>）→ canonical child
        // 的**有界**权威等价桥。仅当 daemon 持久 registry、两侧不可变 capture 与
        // root/owner 溯源全部证明同一 authority 才放行；任一缺口 fail-closed（同一
        // E_WORKSPACE_AUTHORITY_MISMATCH）。证明成立时在同一事务 append-only 写
        // 等价 provenance；不改历史 binding/capture、不传播 legacy instance、不放松
        // exact numeric workspace rejection（数字一致已在上方 step 3 强制）。
        crate::daemon::workspace_reconciliation::prove_parent_canonical_equivalence(
            tx,
            *parent_workspace_id,
            capture_id,
            &parent_instance,
            requested_workspace_id,
            requested_workspace_instance_id,
            created_by,
        )?;
    }
    Ok(())
}
