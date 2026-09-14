//! Role Prompt Compiler v1 — RP-03 单 snapshot authority context domain。
//!
//! 冻结 authority: docs/design/cw-role-prompt-compiler-v1-frozen-spec.md。
//!
//! 职责（RP-03 卡冻结 scope，仅此而已）：
//! - 在**同一条**只读 `Connection` 上聚合 authority（workspace binding/capture/
//!   source event watermark）、in-process next-action 路由与 Task/Role Contract
//!   投影，产出 canonical authority context（spec §5.1 的 bundle 前体）；
//! - 单 snapshot 不变量（spec §2.3-2）：不递归调用第二次 JSON-RPC，全部查询
//!   复用调用方传入的同一连接——RP-05 的 RPC handler 将在 `with_conn` 闭包内
//!   调用本模块，天然与 next-action 共享同一读取现场；
//! - fail-closed：task 不存在、binding/capture 不可解析均返回稳定
//!   `E_TASK_PROMPT_*` 错误（spec §10.2）。
//!
//! 明确不做（RP-04/RP-05 scope）：路由状态机模板选择、renderer、canonical
//! hash、clipping、secret guard、dispatch 注册、capability 发布。

use rusqlite::{Connection, OptionalExtension};
use serde_json::{json, Value};

use crate::daemon::dispatch::DaemonRpcError;
use crate::daemon::task_collab::{
    get_current_task_contract_policy_state, TaskContractPolicyState,
};
use crate::daemon::task_loop::next_action::evaluate_next_action;

/// context 对象 schema 版本（bundle 前体；bundle schema 是 RP-04 的
/// `role_prompt_bundle_v1`，本域只产出 context）。
pub const CONTEXT_SCHEMA_VERSION: &str = "role_prompt_context_v1";

/// 当前 authority 无该 task（spec §10.2，retry `after_authority_change`）。
pub const E_TASK_PROMPT_TASK_NOT_FOUND: &str = "E_TASK_PROMPT_TASK_NOT_FOUND";
/// 唯一 binding/capture/snapshot authority 不可解析（spec §10.2，
/// retry `after_authority_change`）。binding 缺失与 capture 校验失败统一
/// fail-closed 到该码，不向未证明同 workspace 的 caller 细分泄露。
pub const E_TASK_PROMPT_AUTHORITY_UNAVAILABLE: &str = "E_TASK_PROMPT_AUTHORITY_UNAVAILABLE";

/// identity policy 三态到 context 字段的映射（spec §5.1
/// `resolved|unresolved|not_applicable`）。
fn identity_policy_status(state: TaskContractPolicyState) -> &'static str {
    match state {
        TaskContractPolicyState::NoContractRevision => "not_applicable",
        TaskContractPolicyState::Unresolved => "unresolved",
        TaskContractPolicyState::Declared(_) => "resolved",
    }
}

/// routing decision 到 authorization.routing_state 的映射（spec §6）：
/// READY → `action_ready`；BLOCKED/WAITING/COMPLETE/未知 → `non_actionable`
/// （未知值 fail 向 non_actionable，绝不合成 action_ready）。
fn routing_state_for(decision: &str) -> &'static str {
    if decision == "READY" {
        "action_ready"
    } else {
        "non_actionable"
    }
}

/// source event watermark：task_events 单调自增主键的最大值（无事件为 0）。
/// 与 binding/capture 在同一次聚合读取中捕获，供 RP-04/05 检测读取期间
/// authority 变化（`E_TASK_PROMPT_STALE_CONTEXT` 前提）。
fn read_source_event_watermark(conn: &Connection) -> Result<i64, DaemonRpcError> {
    conn.query_row(
        "SELECT COALESCE(MAX(event_id), 0) FROM task_events",
        [],
        |row| row.get(0),
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("watermark 读取失败: {e}")))
}

/// 提取 projection 中嵌套对象字段；缺失一律落 null（context 是只读投影，
/// 不因路由非 READY 而失败）。
fn field(parent: &Value, key: &str, field: &str) -> Value {
    parent
        .get(key)
        .and_then(|v| v.get(field))
        .cloned()
        .unwrap_or(Value::Null)
}

/// 从路由选定的 Role Contract revision 的 canonical payload 中解析
/// prompt_template_id / prompt_hash（next-action 投影不含 prompt_hash，
/// RP-03 在同一 snapshot 内按 revision_id 补齐）。revision 缺失或 payload
/// 不可解析时回退 fallback（保持只读，不失败）。
fn resolve_role_contract_prompt(
    conn: &Connection,
    revision_id: Option<&str>,
    fallback_template_id: Value,
) -> (Value, Value) {
    let Some(revision_id) = revision_id else {
        return (fallback_template_id, Value::Null);
    };
    let payload: Option<String> = conn
        .query_row(
            "SELECT canonical_payload_json FROM role_contract_revisions \
             WHERE role_contract_revision_id = ?1",
            [revision_id],
            |row| row.get(0),
        )
        .optional()
        .unwrap_or(None);
    let Some(payload) = payload else {
        return (fallback_template_id, Value::Null);
    };
    match serde_json::from_str::<Value>(&payload) {
        Ok(payload) => (
            payload
                .get("prompt_template_id")
                .cloned()
                .unwrap_or(fallback_template_id),
            payload.get("prompt_hash").cloned().unwrap_or(Value::Null),
        ),
        Err(_) => (fallback_template_id, Value::Null),
    }
}

/// 单 snapshot authority context 聚合入口。
///
/// 单 snapshot 契约：本函数在调用方给定的同一条 `Connection` 上顺序完成
/// 全部只读查询（task 存在性 → binding → capture 复核 → watermark →
/// in-process next-action 投影 → identity policy 三态），不打开第二个连接、
/// 不递归发起 JSON-RPC。调用方（RP-05 handler）负责在 `with_conn` 闭包内
/// 调用，使整次聚合与既有 task-DB 写路径共享同一连接锁序。
pub fn aggregate_authority_context(
    conn: &Connection,
    workspace_instance_id: &str,
    task_id: &str,
) -> Result<Value, DaemonRpcError> {
    // 1. task 存在性（fail-closed，非泄露错误码）。
    let task_exists: bool = conn
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM tasks WHERE id = ?1)",
            [task_id],
            |row| row.get(0),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("tasks 存在性读取失败: {e}")))?;
    if !task_exists {
        return Err(DaemonRpcError::new(
            E_TASK_PROMPT_TASK_NOT_FOUND,
            format!("task {task_id} 在当前 authority 中不存在"),
        ));
    }

    // 2. 不可变 workspace binding（binding 缺失 → fail-closed）。
    let binding_row: Option<(i64, String, String)> = conn
        .query_row(
            "SELECT workspace_id, workspace_binding_id, workspace_capture_id \
             FROM task_workspace_bindings WHERE task_id = ?1",
            [task_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("task binding 读取失败: {e}")))?;
    let Some((workspace_id, workspace_binding_id, workspace_capture_id)) = binding_row else {
        return Err(DaemonRpcError::new(
            E_TASK_PROMPT_AUTHORITY_UNAVAILABLE,
            format!("task {task_id} 没有可解析的不可变 workspace binding"),
        ));
    };

    // 3. capture 复核（同 snapshot；instance 不匹配或 capture 缺失均 fail-closed）。
    let capture_instance: Option<String> = conn
        .query_row(
            "SELECT workspace_instance_id FROM workspace_authority_captures \
             WHERE workspace_capture_id = ?1",
            [&workspace_capture_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("capture 读取失败: {e}")))?;
    match capture_instance {
        Some(instance) if instance == workspace_instance_id => {}
        _ => {
            return Err(DaemonRpcError::new(
                E_TASK_PROMPT_AUTHORITY_UNAVAILABLE,
                format!("task {task_id} 的 workspace capture 无法对当前 authority 复核"),
            ))
        }
    }

    // 4. source event watermark（与上述读取同一连接）。
    let source_event_watermark = read_source_event_watermark(conn)?;

    // 5. in-process next-action 路由投影（spec §2.3-2：不递归 JSON-RPC）。
    //    失败（含 BLOCKED 之外的 infra 失败）原样传播；e.g. orphan task 的
    //    binding 已在步骤 2 fail-closed，不会到达此处。
    let projection = evaluate_next_action(conn, workspace_instance_id, task_id)?;

    // 6. identity policy 三态（同一连接）。
    let policy_state = get_current_task_contract_policy_state(conn, task_id)?;
    let identity_policy_status = identity_policy_status(policy_state);

    // 6b. 从路由选定 Role Contract revision 的 canonical payload 补齐
    //     prompt_template_id / prompt_hash（同一连接、同一 snapshot）。
    let role_contract_revision_id = field(&projection, "role_contract", "revision_id");
    let (prompt_template_id, prompt_hash) = resolve_role_contract_prompt(
        conn,
        role_contract_revision_id.as_str(),
        field(&projection, "role_contract", "prompt_template_id"),
    );

    // 7. 组装 canonical context（不含 template/render；snapshot_id v1 为 null，
    //    spec §5.2 允许 null 但 binding/capture/watermark 必须可验证）。
    let decision = projection
        .get("decision")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    Ok(json!({
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "task_id": task_id,
        "authority": {
            "workspace_id": workspace_id,
            "workspace_instance_id": workspace_instance_id,
            "workspace_binding_id": workspace_binding_id,
            "workspace_capture_id": workspace_capture_id,
            "snapshot_id": Value::Null,
            "source_event_watermark": source_event_watermark,
        },
        "routing": {
            "decision": decision,
            "action": projection.get("action").cloned().unwrap_or(Value::Null),
            "required_role": projection.get("required_role").cloned().unwrap_or(Value::Null),
            "next_action": projection.get("next_action").cloned().unwrap_or(Value::Null),
            "step_id": projection.get("step_id").cloned().unwrap_or(Value::Null),
        },
        "contract": {
            "task_contract_id": field(&projection, "task_contract", "id"),
            "task_contract_revision": field(&projection, "task_contract", "revision"),
            "task_contract_hash": field(&projection, "task_contract", "hash"),
            "role_contract_revision_id": role_contract_revision_id,
            "role_contract_hash": field(&projection, "role_contract", "hash"),
            "role_contract_prompt_template_id": prompt_template_id,
            "role_contract_prompt_hash": prompt_hash,
            "identity_policy_status": identity_policy_status,
        },
        "authorization": {
            "routing_state": routing_state_for(&decision),
            "valid_for_claim": false,
            "mutation_recheck_required": true,
        },
        "identity_discovery": identity_discovery_projection(conn, workspace_id, task_id),
    }))
}

/// 身份发现投影（修「野身份」漂移）：外部 agent 在无提示词注入时，只能凭空编造
/// `executor-1` 之类 agent_id，导致 action identity 落库为空、后续按 role/session
/// 的治理查询失配。这里把「本任务/本 workspace 已注册的可用身份」直接投影进
/// authority context，使 prompt 自带身份发现能力（只读，不含任何 secret）。
///
/// 字段：
/// - `requirement`：本任务 claim/report/verdict 对身份的要求（固定为注册制）；
/// - `registered_agents`：该 workspace 下已注册且 active 的 agent 摘要（id/role/
///   instance/session），供 caller 选择而非编造；
/// - `registration_hint`：未注册时如何注册（RPC 方法名，供 agent 自救）。
fn identity_discovery_projection(conn: &Connection, workspace_id: i64, task_id: &str) -> Value {
    // 该任务/workspace 最近的 lease 使用者（最贴近「本任务该用谁」的证据）。
    let recent_holders: Vec<Value> = conn
        .prepare(
            "SELECT DISTINCT agent_id, role, session_id FROM task_leases
             WHERE task_id = ?1 AND agent_id <> '' ORDER BY acquired_at DESC LIMIT 5",
        )
        .and_then(|mut stmt| {
            stmt.query_map([task_id], |row| {
                Ok(json!({
                    "agent_id": row.get::<_, String>(0)?,
                    "role": row.get::<_, String>(1)?,
                    "session_id": row.get::<_, String>(2)?,
                    "source": "task_lease",
                }))
            })
            .and_then(|rows| rows.collect::<Result<Vec<_>, _>>())
        })
        .unwrap_or_default();

    // workspace 级已注册身份（active），限 20 条避免 prompt 膨胀。
    let registered: Vec<Value> = conn
        .prepare(
            "SELECT agent_id, role, agent_instance_id, session_id FROM agent_registrations
             WHERE status = 'active' AND agent_id <> ''
             ORDER BY registered_at DESC LIMIT 20",
        )
        .and_then(|mut stmt| {
            stmt.query_map([], |row| {
                Ok(json!({
                    "agent_id": row.get::<_, String>(0)?,
                    "role": row.get::<_, String>(1)?,
                    "agent_instance_id": row.get::<_, String>(2)?,
                    "session_id": row.get::<_, String>(3)?,
                }))
            })
            .and_then(|rows| rows.collect::<Result<Vec<_>, _>>())
        })
        .unwrap_or_default();

    json!({
        "requirement": "claim/report/verdict/apply/close 必须使用已注册身份（agent_id + session_id + model_id + role 四者齐全且与注册行一致）；未注册的 agent_id 会落出空 role 物理行并导致后续治理查询失配。",
        "workspace_id": workspace_id,
        "task_recent_holders": recent_holders,
        "registered_agents": registered,
        "registration_hint": {
            "rpc_method": "agent.register",
            "client": "UnixDaemonRpcClient.agent_register",
            "required_fields": ["agent_id", "agent_instance_id", "session_id", "model_id", "role"],
        },
    })
}
