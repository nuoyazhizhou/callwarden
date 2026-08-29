//! 任务 5A：纯只读 `task.next_action` 派工 evaluator（cw-role-handoff-task-loop.md §3.1-3.4/§6）。
//!
//! 语义：
//! - 严格只读：不激活 workspace、不创建 lease、不写 task event、不更新 `task_steps`；
//!   只经 daemon/authority 查询（`TaskCollabStore.with_conn`），禁止直读 SQLite 写回退。
//! - 按 §3.2 优先级评估 12 条计算规则，任一 Task/Role Contract 缺失、多版本冲突、
//!   hash 无法验证或 binding 非唯一当前项 → `BLOCKED/NONE`，不得默认 Executor 角色。
//! - `task.next_action` 是无角色来源的系统查询：只输出
//!   `routing{origin_kind=system_evaluator, next_role, next_action, reason}`，不输出
//!   `from_role`、角色 outcome 或伪造已发生的 handoff（§3.3 第 212-218 行）。
//! - 存在 unresolved failed step 时只返回其精确 `fix_defect`/remediation step（§3.4）；
//!   remediation 完成但未调用 resolution → 保持 in_progress，不得自动转 review（§6）。
//!
//! 所有权边界：本模块只实现纯只读 evaluator，不编辑 CLI、Skill、测试断言、route.rs
//! （foundation 独占）或任何写路径。复用 `read_current_binding`（claim.rs，public）与
//! `read_effective_verdicts`（verdict_evidence_gate.rs，public）作为 fail-closed 投影。

use rusqlite::{Connection, OptionalExtension};
use serde_json::{Map, Value};

use crate::canonicalize::sha256_hex;
use crate::daemon::dispatch::DaemonRpcError;
use super::claim::read_current_binding;
use super::create::registry_identity_hash;
use super::verdict_evidence_gate::read_effective_verdicts;

/// 确定性拒绝：task 的 workspace authority 不可达或 binding/capture 缺失（§3.2 规则 1）。
pub const ERR_WORKSPACE_AUTHORITY_UNAVAILABLE: &str = "E_WORKSPACE_AUTHORITY_UNAVAILABLE";
/// 确定性拒绝：task 存在但 binding/capture 与请求 workspace authority 不匹配（§3.2 规则 3）。
pub const ERR_WORKSPACE_AUTHORITY_MISMATCH: &str = "E_WORKSPACE_AUTHORITY_MISMATCH";
/// 非泄露错误：task 不存在（对尚未证明同 workspace 的 caller，外部错误统一为该码）。
pub const ERR_TASK_NOT_FOUND_OR_UNAUTHORIZED: &str = "E_TASK_NOT_FOUND_OR_UNAUTHORIZED";
/// 事务/存储基础设施失败（只读查询 infra，与领域写路径对齐）。
pub const ERR_TASK_DB_TRANSACTION: &str = "E_TASK_DB_TRANSACTION";

/// `task.next_action` 的领域输入（严格只读查询参数）。
#[derive(Debug)]
pub struct NextActionInput {
    pub task_id: String,
    pub workspace_instance_id: String,
}

impl NextActionInput {
    /// 从 RPC params 严格解析；`task_id`/`workspace_instance_id` 任一缺失或非字符串即
    /// `invalid_params`，不进入 evaluator。
    pub fn from_params(params: &Value) -> Result<Self, DaemonRpcError> {
        let get_required = |key: &str| -> Result<String, DaemonRpcError> {
            params
                .get(key)
                .and_then(|v| v.as_str())
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .ok_or_else(|| {
                    DaemonRpcError::invalid_params(format!("task.next_action 缺少字段: {key}"))
                })
        };
        Ok(NextActionInput {
            task_id: get_required("task_id")?,
            workspace_instance_id: get_required("workspace_instance_id")?,
        })
    }
}

/// step 的 verified Role Contract 投影（binding 链 + revision 行 + c14n payload 一致才 Some）。
struct RoleContractProjection {
    lineage_id: String,
    revision_id: String,
    revision: i64,
    hash: String,
    canonicalization_version: String,
    canonicalization_rules_hash: String,
    role: String,
    skill_id: String,
    skill_version: String,
    prompt_template_id: String,
    handoff_to: String,
    allowed_paths: Vec<String>,
    forbidden_paths: Vec<String>,
    different_agent_instance_from: Vec<String>,
    different_session_from: Vec<String>,
}

/// identity.role（Role Contract `role`）到 v1 治理角色的运行时映射（与 claim/handoff 对齐）。
fn runtime_role(acting_role: &str) -> &'static str {
    match acting_role {
        "planner" | "implementer" | "tester" | "evidence" | "executor" => "executor",
        "reviewer" | "independent_reviewer" => "reviewer",
        "adjudicator" => "adjudicator",
        _ => "",
    }
}

/// fail-closed：按 §3.2 规则 1/3 复核 task 的 workspace authority。
///
/// - task 无不可变 binding → `E_WORKSPACE_AUTHORITY_UNAVAILABLE`；
/// - capture 行缺失 → `E_WORKSPACE_AUTHORITY_UNAVAILABLE`；
/// - capture 链断链、workspace 归属冲突、`workspace_instance_id` 不匹配或稳定 identity
///   hash 无法复核 → `E_WORKSPACE_AUTHORITY_MISMATCH`。
fn verify_capture(
    conn: &Connection,
    binding_workspace_id: i64,
    workspace_capture_id: &str,
    workspace_instance_id: &str,
) -> Result<(), DaemonRpcError> {
    let row: Option<(i64, String, String, String, String, String, String)> = conn
        .query_row(
            "SELECT workspace_id, workspace_instance_id, client_view_root_hash, \
                    host_real_root_hash, workspace_manifest_hash, registry_identity_hash, \
                    registry_identity_payload_json \
             FROM workspace_authority_captures WHERE workspace_capture_id = ?1",
            [workspace_capture_id],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                    row.get(6)?,
                ))
            },
        )
        .optional()
        .map_err(|e| infra_error(&format!("workspace_authority_captures 读取失败: {e}")))?;
    let (cap_workspace_id, cap_instance, view_hash, host_hash, manifest_hash, identity_hash, _payload) =
        row.ok_or_else(|| {
            DaemonRpcError::new(
                ERR_WORKSPACE_AUTHORITY_UNAVAILABLE,
                format!("task 的 workspace capture {workspace_capture_id} 缺失（binding 悬空）"),
            )
        })?;
    if cap_workspace_id != binding_workspace_id {
        return Err(DaemonRpcError::new(
            ERR_WORKSPACE_AUTHORITY_MISMATCH,
            format!(
                "capture workspace_id={cap_workspace_id} 与 task binding workspace_id={binding_workspace_id} 不一致"
            ),
        ));
    }
    // capture 链连续性：该 workspace 的 capture 行必须连续（COUNT == MAX，非空）。
    // 按 (workspace_id, workspace_instance_id) 限定：整机单库迁移后同一 workspace_id 可能并存
    // 多个实例链（旧单库模型的实例 id 与规范 `ws-{id}`），capture_revision 在实例内单调；
    // 按 workspace_id 全局统计会把多实例并存的连续链误判为断链（count > max）。
    let (count, max): (i64, i64) = conn
        .query_row(
            "SELECT COUNT(*), COALESCE(MAX(capture_revision), 0) \
             FROM workspace_authority_captures \
             WHERE workspace_id = ?1 AND workspace_instance_id = ?2",
            rusqlite::params![cap_workspace_id, cap_instance],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|e| infra_error(&format!("workspace capture 链读取失败: {e}")))?;
    if count != max || max == 0 {
        return Err(DaemonRpcError::new(
            ERR_WORKSPACE_AUTHORITY_MISMATCH,
            format!(
                "workspace {cap_workspace_id} instance {cap_instance} capture 链断链（count={count} max={max}）"
            ),
        ));
    }
    // 调用方 workspace_instance_id 解析到 workspace_id 后与 task binding 比较（scheme 无关）。
    // 整机单库迁移后同一 workspace 的 instance 标识并存多种方案（旧实例 id / 规范 ws-{id} /
    // CLI 按 root 路径哈希推导），严格字符串相等会把同 workspace 的合法调用误判为跨 workspace。
    let resolved_ws = resolve_workspace_id_by_instance(conn, workspace_instance_id)?;
    if resolved_ws != Some(binding_workspace_id) {
        return Err(DaemonRpcError::new(
            ERR_WORKSPACE_AUTHORITY_MISMATCH,
            format!(
                "调用方 workspace_instance_id={workspace_instance_id:?} 解析为 workspace_id={resolved_ws:?}，\
                 与 task binding workspace_id={binding_workspace_id} 不一致（拒绝跨 workspace）"
            ),
        ));
    }
    // 稳定 identity hash 重算复核（§8.1.1）：根/manifest/instance 任一变化即 UNVERIFIED。
    let recomputed =
        registry_identity_hash(&cap_instance, &view_hash, &host_hash, &manifest_hash);
    if recomputed != identity_hash {
        return Err(DaemonRpcError::new(
            ERR_WORKSPACE_AUTHORITY_MISMATCH,
            format!("capture {workspace_capture_id} 的 registry_identity_hash 无法复核（重算不一致）"),
        ));
    }
    // task binding 引用的是创建时的不可变 provenance snapshot，不能因为同一稳定
    // identity 的后续 re-attestation 推进 capture revision 就失效。读取 current capture
    // 只为验证 authority identity 未发生真实变化；不得 UPDATE binding 指针。
    let current: (String, String, String, String, String) = conn
        .query_row(
            "SELECT workspace_capture_id, client_view_root_hash, host_real_root_hash, \
                    workspace_manifest_hash, registry_identity_hash \
             FROM workspace_authority_captures \
             WHERE workspace_id = ?1 AND workspace_instance_id = ?2 \
             ORDER BY capture_revision DESC LIMIT 1",
            rusqlite::params![cap_workspace_id, cap_instance],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?)),
        )
        .map_err(|e| infra_error(&format!("workspace 当前 capture 读取失败: {e}")))?;
    let (current_capture_id, current_view_hash, current_host_hash, current_manifest_hash, current_identity_hash) = current;
    let current_recomputed = registry_identity_hash(
        &cap_instance,
        &current_view_hash,
        &current_host_hash,
        &current_manifest_hash,
    );
    if current_recomputed != current_identity_hash {
        return Err(DaemonRpcError::new(
            ERR_WORKSPACE_AUTHORITY_MISMATCH,
            format!("当前 capture {current_capture_id} 的 registry_identity_hash 无法复核（重算不一致）"),
        ));
    }
    if current_identity_hash != identity_hash {
        return Err(DaemonRpcError::new(
            ERR_WORKSPACE_AUTHORITY_MISMATCH,
            format!(
                "task binding capture {workspace_capture_id} 的稳定 identity 与当前 capture \
                 {current_capture_id} 不一致；不可变 binding 必须保持 UNVERIFIED"
            ),
        ));
    }
    Ok(())
}

/// 把调用方 workspace_instance_id 解析为 workspace_id（scheme 无关，兼容并存的多实例方案）：
///
/// 1. 精确匹配既有 capture 的 `workspace_instance_id`（覆盖旧实例 id / 已登记实例）；
/// 2. 规范约定 `ws-{id}`（daemon 默认实例标识）；
/// 3. 按 `workspaces.root_path` 重算 CLI 的推导（`sha256(norm(root))[:16]`，对齐
///    `daemon_client.derive_workspace_instance_id`），覆盖 CLI 侧 root 哈希方案。
///
/// 解析不到 → `Ok(None)`（调用方 instance 不可解析，由调用方按 fail-closed 拒绝）。
fn resolve_workspace_id_by_instance(
    conn: &Connection,
    caller_instance: &str,
) -> Result<Option<i64>, DaemonRpcError> {
    let caller_instance = caller_instance.trim();
    if caller_instance.is_empty() {
        return Ok(None);
    }
    // 1) 既有 capture 精确 instance 匹配。
    if let Some(ws_id) = conn
        .query_row(
            "SELECT workspace_id FROM workspace_authority_captures \
             WHERE workspace_instance_id = ?1 LIMIT 1",
            [caller_instance],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| infra_error(&format!("instance→workspace 解析失败: {e}")))?
    {
        return Ok(Some(ws_id));
    }
    // 2) 规范约定 ws-{id}。
    if let Some(rest) = caller_instance.strip_prefix("ws-") {
        if let Ok(ws_id) = rest.parse::<i64>() {
            return Ok(Some(ws_id));
        }
    }
    // 3) 按 root 路径重算 CLI 推导（sha256(norm(root))[:16]）。
    let mut stmt = conn
        .prepare("SELECT id, root_path FROM workspaces")
        .map_err(|e| infra_error(&format!("workspaces 读取失败: {e}")))?;
    let rows = stmt
        .query_map([], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)))
        .map_err(|e| infra_error(&format!("workspaces 遍历失败: {e}")))?;
    for row in rows {
        let (ws_id, root_path) =
            row.map_err(|e| infra_error(&format!("workspaces 行读取失败: {e}")))?;
        let norm = root_path.replace('\\', "/");
        let hash = sha256_hex(norm.as_bytes());
        if hash.starts_with(caller_instance) {
            return Ok(Some(ws_id));
        }
    }
    Ok(None)
}

/// fail-closed：解析 task 的当前 Task Contract 三元组（§3.2 规则 4）。
///
/// - 无行 → None（合同缺失 → BLOCKED）；
/// - 多个不同 `contract_id` 并行 → None（多版本冲突 → BLOCKED）；
/// - 同 contract_id 的 revision 链不连续 → None（hash 无法验证 → BLOCKED）；
/// - 否则返回 `(contract_id, MAX(revision), hash)`。
fn resolve_current_task_contract(
    conn: &Connection,
    task_id: &str,
) -> Result<Option<(String, i64, String)>, DaemonRpcError> {
    let mut stmt = conn
        .prepare(
            "SELECT contract_id, revision, contract_hash FROM task_contract_revisions \
             WHERE task_id = ?1",
        )
        .map_err(|e| infra_error(&format!("task_contract_revisions 读取失败: {e}")))?;
    let rows = stmt
        .query_map([task_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?, row.get::<_, String>(2)?))
        })
        .map_err(|e| infra_error(&format!("task_contract_revisions 遍历失败: {e}")))?;
    let mut triples: Vec<(String, i64, String)> = Vec::new();
    for row in rows {
        triples.push(
            row.map_err(|e| infra_error(&format!("task_contract_revisions 行读取失败: {e}")))?,
        );
    }
    if triples.is_empty() {
        return Ok(None);
    }
    let distinct: std::collections::HashSet<&str> =
        triples.iter().map(|t| t.0.as_str()).collect();
    if distinct.len() > 1 {
        return Ok(None);
    }
    let contract_id = triples[0].0.clone();
    let max_rev = triples.iter().map(|t| t.1).max().unwrap_or(0);
    // 链连续性：同 contract_id 的行数必须等于 MAX(revision) 且非空。
    if triples.len() as i64 != max_rev || max_rev == 0 {
        return Ok(None);
    }
    let hash = triples
        .iter()
        .find(|t| t.1 == max_rev)
        .map(|t| t.2.clone())
        .unwrap_or_default();
    Ok(Some((contract_id, max_rev, hash)))
}

/// fail-closed：把 revision 行的 c14n payload 解析为 Role Contract 投影。
/// revision 行悬空 / 与 binding 逐项不一致 / payload 非法 / role 缺失 → None。
fn project_revision(
    revision_id: &str,
    lineage_id: &str,
    revision: i64,
    hash: &str,
    version: &str,
    rules_hash: &str,
    payload_json: &str,
) -> Option<RoleContractProjection> {
    let payload: Value = serde_json::from_str(payload_json).ok()?;
    let role = payload
        .get("role")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if role.is_empty() {
        return None;
    }
    let str_field = |key: &str| -> String {
        payload
            .get(key)
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    };
    let str_list = |key: &str| -> Vec<String> {
        payload
            .get(key)
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().filter_map(|i| i.as_str()).map(|s| s.to_string()).collect())
            .unwrap_or_default()
    };
    let (diff_agent, diff_session) = {
        let indep = payload.get("independence").and_then(|v| v.as_object());
        (
            indep
                .and_then(|o| o.get("different_agent_instance_from"))
                .and_then(|v| v.as_array())
                .map(|a| a.iter().filter_map(|i| i.as_str()).map(|s| s.to_string()).collect())
                .unwrap_or_default(),
            indep
                .and_then(|o| o.get("different_session_from"))
                .and_then(|v| v.as_array())
                .map(|a| a.iter().filter_map(|i| i.as_str()).map(|s| s.to_string()).collect())
                .unwrap_or_default(),
        )
    };
    Some(RoleContractProjection {
        lineage_id: lineage_id.to_string(),
        revision_id: revision_id.to_string(),
        revision,
        hash: hash.to_string(),
        canonicalization_version: version.to_string(),
        canonicalization_rules_hash: rules_hash.to_string(),
        role,
        skill_id: str_field("skill_id"),
        skill_version: str_field("skill_version"),
        prompt_template_id: str_field("prompt_template_id"),
        handoff_to: str_field("handoff_to"),
        allowed_paths: str_list("allowed_paths"),
        forbidden_paths: str_list("forbidden_paths"),
        different_agent_instance_from: diff_agent,
        different_session_from: diff_session,
    })
}

/// 读取 `role_contract_revisions` 行并投影；行悬空 → None。
fn read_revision_row(
    conn: &Connection,
    revision_id: &str,
) -> Result<Option<(String, i64, String, String, String, String)>, DaemonRpcError> {
    let row: Option<(String, i64, String, String, String, String)> = conn
        .query_row(
            "SELECT role_contract_lineage_id, revision, role_contract_hash, \
                    canonical_payload_json, canonicalization_version, canonicalization_rules_hash \
             FROM role_contract_revisions WHERE role_contract_revision_id = ?1",
            [revision_id],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                ))
            },
        )
        .optional()
        .map_err(|e| infra_error(&format!("role_contract_revision 读取失败: {e}")))?;
    Ok(row)
}

/// fail-closed：step 的唯一 current binding → verified Role Contract 投影。
/// binding 链不连续/悬空、revision 行悬空或与 binding 逐项不一致、payload 非法 → None
/// （§3.2 规则 4/7：绑定不是唯一当前项或任一 hash 无法验证即 BLOCKED）。
fn resolve_step_role_contract(
    conn: &Connection,
    workspace_id: i64,
    task_id: &str,
    step_id: &str,
) -> Result<Option<RoleContractProjection>, DaemonRpcError> {
    let binding = match read_current_binding(conn, workspace_id, task_id, step_id)? {
        Some(b) => b,
        None => return Ok(None),
    };
    let row = match read_revision_row(conn, &binding.role_contract_revision_id)? {
        Some(r) => r,
        None => return Ok(None),
    };
    let (lineage_id, revision, hash, payload_json, version, rules_hash) = row;
    if lineage_id != binding.role_contract_lineage_id
        || revision != binding.role_contract_revision
        || hash != binding.role_contract_hash
        || version != binding.canonicalization_version
        || rules_hash != binding.canonicalization_rules_hash
    {
        return Ok(None);
    }
    Ok(project_revision(
        &binding.role_contract_revision_id,
        &lineage_id,
        revision,
        &hash,
        &version,
        &rules_hash,
        &payload_json,
    ))
}

/// fail-closed：从目标角色的 lineage 读取当前 revision 投影（review/adjudicate/revise 路径）。
/// lineage 缺失、revision 链断链、最高 revision 行悬空/payload 非法 → None（→ BLOCKED）。
fn resolve_lineage_role_contract(
    conn: &Connection,
    workspace_id: i64,
    task_id: &str,
    role: &str,
) -> Result<Option<RoleContractProjection>, DaemonRpcError> {
    let lineage_id: Option<String> = conn
        .query_row(
            "SELECT role_contract_lineage_id FROM role_contract_lineages \
             WHERE task_id = ?1 AND workspace_id = ?2 AND role = ?3",
            rusqlite::params![task_id, workspace_id, role],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| infra_error(&format!("role_contract_lineage 读取失败: {e}")))?;
    let lineage_id = match lineage_id {
        Some(id) => id,
        None => return Ok(None),
    };
    let (count, max): (i64, i64) = conn
        .query_row(
            "SELECT COUNT(*), COALESCE(MAX(revision), 0) \
             FROM role_contract_revisions WHERE role_contract_lineage_id = ?1",
            [&lineage_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|e| infra_error(&format!("role contract revision 链读取失败: {e}")))?;
    if count != max || max == 0 {
        return Ok(None);
    }
    let revision_id: String = conn
        .query_row(
            "SELECT role_contract_revision_id FROM role_contract_revisions \
             WHERE role_contract_lineage_id = ?1 ORDER BY revision DESC LIMIT 1",
            [&lineage_id],
            |row| row.get(0),
        )
        .map_err(|e| infra_error(&format!("role contract 当前 revision 读取失败: {e}")))?;
    let row = match read_revision_row(conn, &revision_id)? {
        Some(r) => r,
        None => return Ok(None),
    };
    let (r_lineage, revision, hash, payload_json, version, rules_hash) = row;
    if r_lineage != lineage_id {
        return Ok(None);
    }
    Ok(project_revision(
        &revision_id, &lineage_id, revision, &hash, &version, &rules_hash, &payload_json,
    ))
}

// ---------------------------------------------------------------------------
// unresolved failed step / remediation（复刻 claim.rs 私有语义；§3.4 / §6）
// ---------------------------------------------------------------------------

/// 已解析（有 `step_resolved` resolution event）的 failed step ids（§3.4）。
fn resolved_failed_step_ids(
    conn: &Connection,
    task_id: &str,
) -> Result<Vec<String>, DaemonRpcError> {
    let mut stmt = conn
        .prepare(
            "SELECT reason FROM task_events \
             WHERE task_id = ?1 AND reason_code = 'step_resolved'",
        )
        .map_err(|e| infra_error(&format!("resolution ledger 查询失败: {e}")))?;
    let rows = stmt
        .query_map([task_id], |row| row.get::<_, String>(0))
        .map_err(|e| infra_error(&format!("resolution ledger 读取失败: {e}")))?;
    let mut resolved = Vec::new();
    for row in rows {
        let raw = row.map_err(|e| infra_error(&format!("resolution event 读取失败: {e}")))?;
        if let Ok(value) = serde_json::from_str::<Value>(&raw) {
            if let Some(step_id) = value
                .get("failed_step_id")
                .and_then(|item| item.as_str())
                .filter(|item| !item.trim().is_empty())
            {
                resolved.push(step_id.to_string());
            }
        }
    }
    Ok(resolved)
}

/// 未解析的 failed step ids（status='failed' 且无 resolution event）（§3.4）。
/// pub(crate)：close 门禁（task_collab_lifecycle_apply.rs）复用同一判定，保证
/// next_action 与 close 对 failed step 的解释一致（resolution 覆盖即视为已解决）。
pub(crate) fn unresolved_failed_step_ids(
    conn: &Connection,
    task_id: &str,
) -> Result<Vec<String>, DaemonRpcError> {
    let resolved = resolved_failed_step_ids(conn, task_id)?;
    let mut stmt = conn
        .prepare(
            "SELECT id FROM task_steps \
             WHERE task_id = ?1 AND status = 'failed' ORDER BY step_index ASC",
        )
        .map_err(|e| infra_error(&format!("failed steps 查询失败: {e}")))?;
    let rows = stmt
        .query_map([task_id], |row| row.get::<_, String>(0))
        .map_err(|e| infra_error(&format!("failed steps 读取失败: {e}")))?;
    let mut unresolved = Vec::new();
    for row in rows {
        let step_id = row.map_err(|e| infra_error(&format!("failed step 读取失败: {e}")))?;
        if !resolved.contains(&step_id) {
            unresolved.push(step_id);
        }
    }
    Ok(unresolved)
}

/// 找出必须显式领取的 remediation step（fix_defect 且指向 unresolved failed 或
/// reviewer_blocked/adjudicator_returned 来源）；无则 None（§3.4）。
fn required_remediation_step(
    conn: &Connection,
    task_id: &str,
) -> Result<Option<String>, DaemonRpcError> {
    let unresolved = unresolved_failed_step_ids(conn, task_id)?;
    let mut stmt = conn
        .prepare(
            "SELECT id, result FROM task_steps \
             WHERE task_id = ?1 AND action = 'fix_defect' \
               AND status IN ('pending', 'in_progress') \
             ORDER BY step_index ASC",
        )
        .map_err(|e| infra_error(&format!("remediation steps 查询失败: {e}")))?;
    let rows = stmt
        .query_map([task_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(|e| infra_error(&format!("remediation steps 读取失败: {e}")))?;
    for row in rows {
        let (step_id, raw) =
            row.map_err(|e| infra_error(&format!("remediation step 读取失败: {e}")))?;
        let metadata =
            serde_json::from_str::<Value>(&raw).unwrap_or(Value::Null);
        let linked = metadata
            .get("remediation_of_step_id")
            .and_then(|item| item.as_str())
            .unwrap_or("");
        let source_outcome = metadata
            .get("source_outcome")
            .and_then(|item| item.as_str())
            .unwrap_or("");
        let source_verdict_ok = metadata
            .get("source_verdict_id")
            .and_then(|item| item.as_str())
            .map(|item| !item.trim().is_empty())
            .unwrap_or(false);
        let source_handoff_ok = metadata
            .get("source_handoff_event_id")
            .map(|item| match item {
                Value::Number(_) => true,
                Value::String(text) => !text.trim().is_empty(),
                _ => false,
            })
            .unwrap_or(false);
        let governance_provenance_ok = source_verdict_ok && source_handoff_ok;
        if unresolved.iter().any(|u| u == linked)
            || (matches!(source_outcome, "reviewer_blocked" | "adjudicator_returned")
                && governance_provenance_ok)
        {
            return Ok(Some(step_id));
        }
    }
    Ok(None)
}

/// 当前 task 的 active 未过期 lease 持有角色（§3.2 规则 6；不泄露 token）。
fn active_lease_role(conn: &Connection, task_id: &str) -> Result<Option<String>, DaemonRpcError> {
    let now = now_unix();
    let row: Option<String> = conn
        .query_row(
            "SELECT role FROM task_leases \
             WHERE task_id = ?1 AND status = 'active' AND expires_at > ?2 \
             ORDER BY id ASC LIMIT 1",
            rusqlite::params![task_id, now],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| infra_error(&format!("task_leases 读取失败: {e}")))?;
    Ok(row)
}

/// 第一个 pending/in_progress step（普通领取目标；也用作 review/applied 的 lease-wait 上下文）。
fn first_claimable_step(
    conn: &Connection,
    task_id: &str,
) -> Result<Option<String>, DaemonRpcError> {
    conn.query_row(
        "SELECT id FROM task_steps \
         WHERE task_id = ?1 AND status IN ('pending', 'in_progress') \
         ORDER BY step_index ASC LIMIT 1",
        [task_id],
        |row| row.get(0),
    )
    .optional()
    .map_err(|e| infra_error(&format!("task_steps 读取失败: {e}")))
}

/// 规则 6 前置：当前步骤仍有 active 未过期 lease → `WAITING/WAIT`（review/applied 也适用）。
fn wait_if_active_lease(
    conn: &Connection,
    task_id: &str,
    task_status: &str,
) -> Result<Option<Value>, DaemonRpcError> {
    match active_lease_role(conn, task_id)? {
        Some(holder) => {
            let step = first_claimable_step(conn, task_id)?.unwrap_or_default();
            Ok(Some(wait_outcome(task_id, task_status, &step, &holder)?))
        }
        None => Ok(None),
    }
}

/// 读取最新 block verdict 的 verdict_id + findings（READY/REVISE 的只读 revision_hint）。
fn latest_block_verdict(
    conn: &Connection,
    task_id: &str,
) -> Result<Option<(String, Vec<Value>)>, DaemonRpcError> {
    let verdicts = read_effective_verdicts(conn, task_id)?;
    let block = verdicts
        .iter()
        .rev()
        .find(|v| v.normalized_overall == "block")
        .map(|v| v.verdict_id.clone());
    let verdict_id = match block {
        Some(id) => id,
        None => return Ok(None),
    };
    let findings_json: String = conn
        .query_row(
            "SELECT findings FROM task_verdict_events WHERE verdict_id = ?1 AND task_id = ?2",
            rusqlite::params![&verdict_id, task_id],
            |row| row.get(0),
        )
        .map_err(|e| infra_error(&format!("verdict findings 读取失败: {e}")))?;
    let findings = serde_json::from_str::<Vec<Value>>(&findings_json).unwrap_or_default();
    Ok(Some((verdict_id, findings)))
}

/// 读取 verdict 的结构化 finding 数量；投影只返回数量，不泄露 finding 原文。
fn verdict_findings_count(
    conn: &Connection,
    task_id: &str,
    verdict_id: &str,
) -> Result<usize, DaemonRpcError> {
    let findings_json: String = conn
        .query_row(
            "SELECT findings FROM task_verdict_events WHERE verdict_id = ?1 AND task_id = ?2",
            rusqlite::params![verdict_id, task_id],
            |row| row.get(0),
        )
        .map_err(|e| infra_error(&format!("verdict findings 读取失败: {e}")))?;
    Ok(serde_json::from_str::<Vec<Value>>(&findings_json)
        .map(|findings| findings.len())
        .unwrap_or(0))
}

// ---------------------------------------------------------------------------
// 响应组装（§3.1 形态；`routing.origin_kind` 固定 `system_evaluator`）
// ---------------------------------------------------------------------------

struct Rendered {
    decision: &'static str,
    action: &'static str,
    required_role: Option<String>,
    step_id: Option<String>,
    task_contract: Option<(String, i64, String)>,
    role_contract: Option<RoleContractProjection>,
    verdict_eligibility: &'static str,
    blocking_conditions: Vec<String>,
    revision_hint: Option<Value>,
    next_role: Option<String>,
    next_action: &'static str,
    routing_reason: Vec<String>,
    next_session: Option<Value>,
    review_verdict_id: Option<String>,
    review_findings_count: Option<usize>,
}

/// 用户可读的治理阶段投影；`tasks.status` 仍保留生命周期门禁语义。
fn workflow_status_for(task_status: &str, action: &str, next_action: &str) -> &'static str {
    if action == "NONE" && next_action == "none" {
        return "governance_blocked";
    }
    match task_status {
        "open" if action == "WAIT" => "execution_in_progress",
        "open" => "queued",
        "in_progress" => {
            if next_action == "revise_current_step" {
                "remediation_in_progress"
            } else {
                "execution_in_progress"
            }
        }
        "review" => match action {
            "REVIEW" => "review_pending",
            "ADJUDICATE" => "adjudication_pending",
            "REVISE" => "remediation_pending",
            _ => "governance_blocked",
        },
        "applied" => "applied_pending_close",
        "closed" => "completed",
        "reverted" => "reverted",
        _ => "unknown",
    }
}

fn review_state_for(task_status: &str, action: &str) -> &'static str {
    if task_status != "review" {
        return "not_in_review";
    }
    match action {
        "REVIEW" => "pending",
        "ADJUDICATE" => "passed",
        "REVISE" => "blocked",
        _ => "unverified",
    }
}

fn render(r: &Rendered, task_status: &str) -> Value {
    let rc_json = |rc: &RoleContractProjection| -> Value {
        serde_json::json!({
            "id": rc.lineage_id,
            "revision_id": rc.revision_id,
            "revision": rc.revision,
            "hash": rc.hash,
            "canonicalization_version": rc.canonicalization_version,
            "canonicalization_rules_hash": rc.canonicalization_rules_hash,
            "skill_id": rc.skill_id,
            "skill_version": rc.skill_version,
            "prompt_template_id": rc.prompt_template_id,
            "handoff_to": rc.handoff_to,
        })
    };
    let tc = &r.task_contract;
    let tc_json = tc
        .as_ref()
        .map(|(id, revision, hash)| {
            serde_json::json!({ "id": id, "revision": revision, "hash": hash })
        })
        .unwrap_or(Value::Null);
    let rc = &r.role_contract;
    let rc_obj = rc.as_ref().map(rc_json).unwrap_or(Value::Null);
    let acting_role = r.required_role.clone().unwrap_or_default();
    // Adjudicator 执行 apply/close 的 acting role 是 `adjudicator`，取得的 lease role 是 `reviewer`（§3.1）。
    let is_adjudicator = acting_role == "adjudicator";
    let lease_role = if is_adjudicator {
        "reviewer".to_string()
    } else {
        acting_role.clone()
    };
    let authorization = serde_json::json!({
        "acting_role": if acting_role.is_empty() { Value::Null } else { Value::String(acting_role) },
        "lease_role": if lease_role.is_empty() { Value::Null } else { Value::String(lease_role) },
        "lease_required": r.decision == "READY",
        "fencing_required": r.decision == "READY",
        "different_agent_instance_from": rc.as_ref().map(|x| x.different_agent_instance_from.clone()).unwrap_or_default(),
        "different_session_from": rc.as_ref().map(|x| x.different_session_from.clone()).unwrap_or_default(),
    });
    let mut m = Map::new();
    // task_id 由各 outcome 调用方在渲染后回填（next_session 可能为 null，此处不派生）。
    m.insert("task_id".to_string(), Value::String(String::new()));
    m.insert("lifecycle_status".to_string(), Value::String(task_status.to_string()));
    m.insert(
        "workflow_status".to_string(),
        Value::String(workflow_status_for(task_status, r.action, r.next_action).to_string()),
    );
    m.insert(
        "current_role".to_string(),
        r.required_role.clone().map(Value::String).unwrap_or(Value::Null),
    );
    m.insert(
        "next_role".to_string(),
        r.next_role.clone().map(Value::String).unwrap_or(Value::Null),
    );
    m.insert("next_action".to_string(), Value::String(r.next_action.to_string()));
    let mut review = serde_json::json!({
        "state": review_state_for(task_status, r.action),
    });
    if let Some(verdict_id) = &r.review_verdict_id {
        review["verdict_id"] = Value::String(verdict_id.clone());
    }
    if let Some(findings_count) = r.review_findings_count {
        review["findings_count"] = serde_json::json!(findings_count);
    }
    m.insert("review".to_string(), review);
    m.insert("decision".to_string(), Value::String(r.decision.to_string()));
    m.insert("action".to_string(), Value::String(r.action.to_string()));
    m.insert(
        "required_role".to_string(),
        match &r.required_role {
            Some(role) => Value::String(role.clone()),
            None => Value::Null,
        },
    );
    m.insert(
        "step_id".to_string(),
        match &r.step_id {
            Some(id) => Value::String(id.clone()),
            None => Value::Null,
        },
    );
    m.insert("task_contract".to_string(), tc_json);
    m.insert("role_contract".to_string(), rc_obj);
    m.insert("authorization".to_string(), authorization);
    m.insert(
        "allowed_paths".to_string(),
        serde_json::json!(rc.as_ref().map(|x| x.allowed_paths.clone()).unwrap_or_default()),
    );
    m.insert(
        "forbidden_paths".to_string(),
        serde_json::json!(rc.as_ref().map(|x| x.forbidden_paths.clone()).unwrap_or_default()),
    );
    m.insert(
        "eligibility".to_string(),
        serde_json::json!({
            "verdict": r.verdict_eligibility,
            "evidence_gate": "not_evaluated",
            "snapshot": "not_evaluated",
            "mutation_recheck_required": true,
        }),
    );
    m.insert(
        "blocking_conditions".to_string(),
        serde_json::json!(r.blocking_conditions),
    );
    // 对外提供稳定的人类语义名称；保留旧字段兼容已有客户端。
    m.insert(
        "blocking_reasons".to_string(),
        serde_json::json!(r.blocking_conditions),
    );
    m.insert(
        "revision_hint".to_string(),
        r.revision_hint.clone().unwrap_or(Value::Null),
    );
    m.insert(
        "routing".to_string(),
        serde_json::json!({
            "origin_kind": "system_evaluator",
            "next_role": match &r.next_role {
                Some(role) => Value::String(role.clone()),
                None => Value::Null,
            },
            "next_action": r.next_action,
            "reason": r.routing_reason,
        }),
    );
    m.insert(
        "next_session".to_string(),
        r.next_session.clone().unwrap_or(Value::Null),
    );
    m.insert(
        "source".to_string(),
        serde_json::json!({
            "task_status": task_status,
            "task_contract_hash": tc.as_ref().map(|t| t.2.clone()).unwrap_or_default(),
            "role_contract_hash": rc.as_ref().map(|x| x.hash.clone()).unwrap_or_default(),
            "evaluated_at": literal_now(),
        }),
    );
    Value::Object(m)
}

fn blocked(
    task_id: &str,
    task_status: &str,
    blocking: String,
) -> Result<Value, DaemonRpcError> {
    let r = Rendered {
        decision: "BLOCKED",
        action: "NONE",
        required_role: None,
        step_id: None,
        task_contract: None,
        role_contract: None,
        verdict_eligibility: "not_evaluated",
        blocking_conditions: vec![blocking.clone()],
        revision_hint: None,
        next_role: None,
        next_action: "none",
        routing_reason: vec![blocking],
        next_session: None,
        review_verdict_id: None,
        review_findings_count: None,
    };
    let mut value = render(&r, task_status);
    if let Value::Object(m) = &mut value {
        m.insert("task_id".to_string(), Value::String(task_id.to_string()));
    }
    Ok(value)
}

#[allow(clippy::too_many_arguments)]
fn claim_outcome(
    task_id: &str,
    task_status: &str,
    step_id: &str,
    tc: (String, i64, String),
    rc: RoleContractProjection,
    kind: &str,
) -> Result<Value, DaemonRpcError> {
    let reason = if kind == "remediation" {
        format!("存在 unresolved failed step，唯一可领取目标为 remediation step {step_id}")
    } else {
        format!("当前步骤 {step_id} 可领取（唯一 verified Role Contract binding）")
    };
    let r = Rendered {
        decision: "READY",
        action: "CLAIM",
        required_role: Some("executor".to_string()),
        step_id: Some(step_id.to_string()),
        task_contract: Some(tc),
        role_contract: Some(rc),
        verdict_eligibility: "not_required_for_claim",
        blocking_conditions: Vec::new(),
        revision_hint: None,
        next_role: Some("executor".to_string()),
        next_action: "claim_current_step",
        routing_reason: vec![reason],
        next_session: Some(serde_json::json!({
            "role": "executor",
            "task_id": task_id,
            "step_id": step_id,
            "must_be_new_session": false,
        })),
        review_verdict_id: None,
        review_findings_count: None,
    };
    let mut value = render(&r, task_status);
    if let Value::Object(m) = &mut value {
        m.insert("task_id".to_string(), Value::String(task_id.to_string()));
    }
    Ok(value)
}

fn wait_outcome(
    task_id: &str,
    task_status: &str,
    step_id: &str,
    holder_role: &str,
) -> Result<Value, DaemonRpcError> {
    let r = Rendered {
        decision: "WAITING",
        action: "WAIT",
        required_role: Some(holder_role.to_string()),
        step_id: Some(step_id.to_string()),
        task_contract: None,
        role_contract: None,
        verdict_eligibility: "not_evaluated",
        blocking_conditions: vec![format!("task 存在 active 未过期 lease（持有角色 {holder_role}），等待其释放")],
        revision_hint: None,
        next_role: None,
        next_action: "wait_for_current_lease",
        routing_reason: vec![format!("当前 lease 未过期，持有角色 {holder_role}（不泄露 token）")],
        next_session: None,
        review_verdict_id: None,
        review_findings_count: None,
    };
    let mut value = render(&r, task_status);
    if let Value::Object(m) = &mut value {
        m.insert("task_id".to_string(), Value::String(task_id.to_string()));
    }
    Ok(value)
}

#[allow(clippy::too_many_arguments)]
fn review_outcome(
    task_id: &str,
    task_status: &str,
    tc: (String, i64, String),
    rc: RoleContractProjection,
) -> Result<Value, DaemonRpcError> {
    let r = Rendered {
        decision: "READY",
        action: "REVIEW",
        required_role: Some("reviewer".to_string()),
        step_id: None,
        task_contract: Some(tc),
        role_contract: Some(rc),
        verdict_eligibility: "pending_review",
        blocking_conditions: Vec::new(),
        revision_hint: None,
        next_role: Some("reviewer".to_string()),
        next_action: "review_current_step",
        routing_reason: vec!["任务在 review 且尚无有效持久化 verdict".to_string()],
        next_session: Some(serde_json::json!({
            "role": "reviewer",
            "task_id": task_id,
            "step_id": Value::Null,
            "must_be_new_session": true,
        })),
        review_verdict_id: None,
        review_findings_count: None,
    };
    let mut value = render(&r, task_status);
    if let Value::Object(m) = &mut value {
        m.insert("task_id".to_string(), Value::String(task_id.to_string()));
    }
    Ok(value)
}

#[allow(clippy::too_many_arguments)]
fn revise_outcome(
    task_id: &str,
    task_status: &str,
    step_id: Option<String>,
    tc: (String, i64, String),
    rc: Option<RoleContractProjection>,
    hint: Option<Value>,
    reason: String,
) -> Result<Value, DaemonRpcError> {
    let review_verdict_id = hint
        .as_ref()
        .and_then(|value| value.get("source_verdict_id"))
        .and_then(Value::as_str)
        .map(str::to_string);
    let review_findings_count = hint
        .as_ref()
        .and_then(|value| value.get("findings"))
        .and_then(Value::as_array)
        .map(Vec::len);
    let r = Rendered {
        decision: "READY",
        action: "REVISE",
        required_role: Some("executor".to_string()),
        step_id: step_id.clone(),
        task_contract: Some(tc),
        role_contract: rc,
        verdict_eligibility: "blocked",
        blocking_conditions: Vec::new(),
        revision_hint: hint,
        next_role: Some("executor".to_string()),
        next_action: "revise_current_step",
        routing_reason: vec![reason.clone()],
        next_session: Some(serde_json::json!({
            "role": "executor",
            "task_id": task_id,
            "step_id": step_id.unwrap_or_default(),
            "must_be_new_session": false,
        })),
        review_verdict_id,
        review_findings_count,
    };
    let mut value = render(&r, task_status);
    if let Value::Object(m) = &mut value {
        m.insert("task_id".to_string(), Value::String(task_id.to_string()));
    }
    Ok(value)
}

#[allow(clippy::too_many_arguments)]
fn adjudicate_outcome(
    task_id: &str,
    task_status: &str,
    tc: (String, i64, String),
    rc: RoleContractProjection,
    reason: String,
    review_verdict_id: Option<String>,
    review_findings_count: Option<usize>,
) -> Result<Value, DaemonRpcError> {
    let r = Rendered {
        decision: "READY",
        action: "ADJUDICATE",
        required_role: Some("adjudicator".to_string()),
        step_id: None,
        task_contract: Some(tc),
        role_contract: Some(rc),
        verdict_eligibility: "passed",
        blocking_conditions: Vec::new(),
        revision_hint: None,
        next_role: Some("adjudicator".to_string()),
        next_action: "adjudicate_current_verdict",
        routing_reason: vec![reason],
        next_session: Some(serde_json::json!({
            "role": "adjudicator",
            "task_id": task_id,
            "step_id": Value::Null,
            "must_be_new_session": true,
        })),
        review_verdict_id,
        review_findings_count,
    };
    let mut value = render(&r, task_status);
    if let Value::Object(m) = &mut value {
        m.insert("task_id".to_string(), Value::String(task_id.to_string()));
    }
    Ok(value)
}

fn complete_outcome(task_id: &str, task_status: &str) -> Result<Value, DaemonRpcError> {
    let r = Rendered {
        decision: "COMPLETE",
        action: "NONE",
        required_role: None,
        step_id: None,
        task_contract: None,
        role_contract: None,
        verdict_eligibility: "not_evaluated",
        blocking_conditions: Vec::new(),
        revision_hint: None,
        next_role: Some("complete".to_string()),
        next_action: "finalize",
        routing_reason: vec!["任务已 closed，所有终态门禁满足".to_string()],
        next_session: None,
        review_verdict_id: None,
        review_findings_count: None,
    };
    let mut value = render(&r, task_status);
    if let Value::Object(m) = &mut value {
        m.insert("task_id".to_string(), Value::String(task_id.to_string()));
    }
    Ok(value)
}

// ---------------------------------------------------------------------------
// 主入口：§3.2 12 条规则（只读，fail-closed）
// ---------------------------------------------------------------------------

/// 纯只读派工 evaluator：计算 task 的下一合法动作（cw-role-handoff-task-loop.md §3.2）。
///
/// 调用方保证只读连接（`TaskCollabStore.with_conn`）；本函数不做任何写入。
/// 任一 workspace authority 不可复核 / task 不存在 → 返回结构化 `DaemonRpcError`；
/// 其余 fail-closed 状态（合同缺失、binding 非唯一、verdict 无法验证等）→ `Ok(BLOCKED)`。
fn evaluate_next_action_inner(
    conn: &Connection,
    workspace_instance_id: &str,
    task_id: &str,
) -> Result<Value, DaemonRpcError> {
    // 规则 2：先查询 task 存在性；不存在 → 非泄露错误（对未证明同 workspace 的 caller）。
    let task_status: String = match conn
        .query_row("SELECT status FROM tasks WHERE id = ?1", [task_id], |row| row.get(0))
        .optional()
        .map_err(|e| infra_error(&format!("tasks 读取失败: {e}")))?
    {
        Some(status) => status,
        None => {
            return Err(DaemonRpcError::new(
                ERR_TASK_NOT_FOUND_OR_UNAUTHORIZED,
                format!("task {task_id} 不存在或无权访问"),
            ))
        }
    };

    // 规则 1/3：不可变 workspace binding + capture 链复核。
    let binding_row: Option<(i64, String)> = conn
        .query_row(
            "SELECT workspace_id, workspace_capture_id FROM task_workspace_bindings \
             WHERE task_id = ?1",
            [task_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(|e| infra_error(&format!("task workspace binding 读取失败: {e}")))?;
    let (workspace_id, workspace_capture_id) = binding_row.ok_or_else(|| {
        DaemonRpcError::new(
            ERR_WORKSPACE_AUTHORITY_UNAVAILABLE,
            format!("task {task_id} 没有不可变 task workspace binding"),
        )
    })?;
    verify_capture(conn, workspace_id, &workspace_capture_id, workspace_instance_id)?;

    // 规则 4：Task Contract 缺失/多版本冲突/revision 断链 → BLOCKED/NONE。
    let task_contract = match resolve_current_task_contract(conn, task_id)? {
        Some(tc) => tc,
        None => {
            return blocked(
                task_id,
                &task_status,
                "Task Contract 缺失、多版本冲突或 revision 链不连续（无法验证 hash）".to_string(),
            )
        }
    };
    let tc = task_contract.clone();

    // 规则 7（前置）：存在 unresolved failed step → 只返回精确 remediation step。
    let unresolved = unresolved_failed_step_ids(conn, task_id)?;
    if !unresolved.is_empty() {
        let remediation = required_remediation_step(conn, task_id)?;
        match remediation {
            Some(step_id) => {
                return match resolve_or_block_step(conn, workspace_id, task_id, &step_id)? {
                    StepResolution::Ready(rc) => {
                        claim_outcome(task_id, &task_status, &step_id, tc, rc, "remediation")
                    }
                    StepResolution::Waiting { holder_role } => {
                        wait_outcome(task_id, &task_status, &step_id, &holder_role)
                    }
                    StepResolution::Blocked { reason } => blocked(task_id, &task_status, reason),
                };
            }
            None => {
                // remediation 完成但未调用 resolution（或异常）：原 failed 仍 failed，
                // 任务保持 in_progress，不得自动转 review（§6 第 597 行）。
                let rc = resolve_lineage_role_contract(conn, workspace_id, task_id, "executor")?;
                return revise_outcome(
                    task_id,
                    &task_status,
                    None,
                    tc,
                    rc,
                    Some(serde_json::json!({
                        "failed_steps": unresolved,
                        "reason": "remediation 已完成但未调用 task.step.resolve；必须 resolve 后才能进入 review",
                    })),
                    "存在 unresolved failed step 且无待领取 remediation，必须 resolve 后继续".to_string(),
                );
            }
        }
    }

    // 规则 12：终态门禁满足 → COMPLETE/NONE。
    if task_status == "closed" {
        return complete_outcome(task_id, &task_status);
    }

    // 规则 8-11：review 状态由 Verdict Ledger 的 versioned 有效投影驱动。
    if task_status == "review" {
        // 规则 6 优先于 8-10：存在 active 未过期 lease。
        // 但若该 lease 由 reviewer 持有（reviewer 已领取本任务的评审 lease），则应允许其继续
        // 评审，而非弹回 WAITING——修复 reviewer_blocked 反复指出的"派工投影不稳定"：
        // 取得 reviewer lease 后 recheck 由 REVIEW 翻转为 WAITING/wait_for_current_lease。
        // 仅当 lease 由非 reviewer 角色持有（如 executor 步骤 lease 残留）时才 WAITING。
        if let Some(holder) = active_lease_role(conn, task_id)? {
            if holder == "reviewer" {
                let rc = match resolve_lineage_role_contract(conn, workspace_id, task_id, "reviewer")? {
                    Some(rc) => rc,
                    None => {
                        return blocked(
                            task_id,
                            &task_status,
                            "review 状态缺少 reviewer Role Contract lineage（合同缺失）".to_string(),
                        )
                    }
                };
                return review_outcome(task_id, &task_status, tc, rc);
            }
            return Ok(wait_outcome(
                task_id,
                &task_status,
                &first_claimable_step(conn, task_id)?.unwrap_or_default(),
                &holder,
            )?);
        }
        let verdicts = read_effective_verdicts(conn, task_id)?;
        let effective = verdicts
            .iter()
            .rev()
            .find(|v| v.normalized_overall == "pass" || v.normalized_overall == "block");
        match effective {
            None => {
                if verdicts.is_empty() {
                    let rc = match resolve_lineage_role_contract(conn, workspace_id, task_id, "reviewer")? {
                        Some(rc) => rc,
                        None => {
                            return blocked(
                                task_id,
                                &task_status,
                                "review 状态缺少 reviewer Role Contract lineage（合同缺失）".to_string(),
                            )
                        }
                    };
                    return review_outcome(task_id, &task_status, tc, rc);
                }
                return blocked(
                    task_id,
                    &task_status,
                    "verdict 无法按持久化 normalization 规则验证（UNVERIFIED），保持 fail-closed".to_string(),
                );
            }
            Some(v) if v.normalized_overall == "block" => {
                let rc = match resolve_lineage_role_contract(conn, workspace_id, task_id, "executor")? {
                    Some(rc) => Some(rc),
                    None => None,
                };
                let hint = latest_block_verdict(conn, task_id)?.map(|(verdict_id, findings)| {
                    serde_json::json!({
                        "source_verdict_id": verdict_id,
                        "findings": findings,
                        "reason": "reviewer BLOCKED：只读回显来源、既有合同约束与观察事实；Executor 自行修订 scope",
                    })
                });
                return revise_outcome(
                    task_id,
                    &task_status,
                    None,
                    tc,
                    rc,
                    hint,
                    "reviewer 有效 verdict 为 BLOCKED，返回 Executor 整改（READY/REVISE）".to_string(),
                );
            }
            Some(v) => {
                // 有效 verdict 为 PASS → READY/ADJUDICATE（新独立 instance/session）。
                let rc = match resolve_lineage_role_contract(conn, workspace_id, task_id, "adjudicator")? {
                    Some(rc) => rc,
                    None => {
                        return blocked(
                            task_id,
                            &task_status,
                            "review PASS 但缺少 adjudicator Role Contract lineage（合同缺失）".to_string(),
                        )
                    }
                };
                return adjudicate_outcome(
                    task_id,
                    &task_status,
                    tc,
                    rc,
                    "reviewer 有效 verdict 为 PASS，交独立 Adjudicator 裁决（不等于 apply/close）".to_string(),
                    Some(v.verdict_id.clone()),
                    Some(verdict_findings_count(conn, task_id, &v.verdict_id)?),
                );
            }
        }
    }

    // 规则 11：adjudicator 已接受（applied），可执行最终 close（仍需独立 instance + 真实 lease）。
    if task_status == "applied" {
        // 规则 6 优先于 11：仍有 active 未过期 lease → WAITING/WAIT。
        if let Some(wait) = wait_if_active_lease(conn, task_id, &task_status)? {
            return Ok(wait);
        }
        let rc = match resolve_lineage_role_contract(conn, workspace_id, task_id, "adjudicator")? {
            Some(rc) => rc,
            None => {
                return blocked(
                    task_id,
                    &task_status,
                    "applied 状态缺少 adjudicator Role Contract lineage（合同缺失）".to_string(),
                )
            }
        };
        return adjudicate_outcome(
            task_id,
            &task_status,
            tc,
            rc,
            "adjudicator 已接受，可执行最终 apply/close（READY/ADJUDICATE）".to_string(),
            None,
            None,
        );
    }

    // 治理退回已经把任务重新打开时，fix_defect remediation 仍是唯一可领取目标；
    // 不能因为没有 unresolved failed step 就落入普通 step 或再次走 PASS 裁决。
    if let Some(step_id) = required_remediation_step(conn, task_id)? {
        return match resolve_or_block_step(conn, workspace_id, task_id, &step_id)? {
            StepResolution::Ready(rc) => {
                claim_outcome(task_id, &task_status, &step_id, tc, rc, "remediation")
            }
            StepResolution::Waiting { holder_role } => {
                wait_outcome(task_id, &task_status, &step_id, &holder_role)
            }
            StepResolution::Blocked { reason } => blocked(task_id, &task_status, reason),
        };
    }

    // 规则 7（普通）：第一个 pending/in_progress step 为唯一可领取目标。
    let step_id = match first_claimable_step(conn, task_id)? {
        Some(id) => id,
        None => {
            // 无 pending 步骤且无 unresolved failed → 依赖/acceptance 未满足（规则 5）→ BLOCKED。
            return blocked(
                task_id,
                &task_status,
                "任务无 pending 步骤且无待处理 remediation（依赖或 acceptance 未满足）".to_string(),
            );
        }
    };
    claim_outcome(
        task_id,
        &task_status,
        &step_id,
        tc,
        match resolve_or_block_step(conn, workspace_id, task_id, &step_id)? {
            StepResolution::Ready(rc) => rc,
            StepResolution::Waiting { holder_role } => {
                return wait_outcome(task_id, &task_status, &step_id, &holder_role);
            }
            StepResolution::Blocked { reason } => {
                return blocked(task_id, &task_status, reason);
            }
        },
        "normal",
    )
}

/// claim 前置解析结果：唯一可领取投影 / 需等待的 lease / fail-closed 阻断。
enum StepResolution {
    Ready(RoleContractProjection),
    Waiting { holder_role: String },
    Blocked { reason: String },
}

/// claim 路径：先查 lease（规则 6），再解析 step 的唯一 Role Contract（规则 4/7）。
/// lease 未过期 → `Waiting`；binding 缺失或不可验证 / role 无法映射 → `Blocked`
/// （fail-closed，不猜测角色）；仅 infra 失败返回 `Err`。
fn resolve_or_block_step(
    conn: &Connection,
    workspace_id: i64,
    task_id: &str,
    step_id: &str,
) -> Result<StepResolution, DaemonRpcError> {
    if let Some(holder) = active_lease_role(conn, task_id)? {
        return Ok(StepResolution::Waiting { holder_role: holder });
    }
    match resolve_step_role_contract(conn, workspace_id, task_id, step_id)? {
        Some(rc) => {
            if runtime_role(&rc.role).is_empty() {
                Ok(StepResolution::Blocked {
                    reason: format!(
                        "step {step_id} 在 task {task_id} 的 Role Contract role={} 无法映射到治理角色",
                        rc.role
                    ),
                })
            } else {
                Ok(StepResolution::Ready(rc))
            }
        }
        None => Ok(StepResolution::Blocked {
            reason: format!("step {step_id} 在 task {task_id} 无唯一可验证的 Role Contract binding"),
        }),
    }
}

// ---------------------------------------------------------------------------
// 基础设施/工具 helpers
// ---------------------------------------------------------------------------

/// 权威 UTC 秒值（微秒精度）文本；真实接入由 Authoritative_Clock 产生。
fn literal_now() -> String {
    format!("{:.6}", now_unix())
}

/// Unix 时间戳秒（float，兼容 SQLite REAL）。
fn now_unix() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 基础设施失败归类（E_TASK_DB_TRANSACTION）。
fn infra_error(message: &str) -> DaemonRpcError {
    DaemonRpcError::new(ERR_TASK_DB_TRANSACTION, message.to_string())
}

/// 只读派工 evaluator 的公共入口：计算既有派工投影后附加 `inbound_handoff` +
/// `work_order` 两个派生只读字段（T-1787912195064；投影逻辑在 `inbound_handoff.rs`）。
///
/// 保持与既有 `evaluate_next_action` 完全相同的签名与行为；投影失败 fail-soft
/// （no_handoff / unparsable_handoff / 空数组），不阻断既有派工。
pub fn evaluate_next_action(
    conn: &Connection,
    workspace_instance_id: &str,
    task_id: &str,
) -> Result<Value, DaemonRpcError> {
    let mut value = evaluate_next_action_inner(conn, workspace_instance_id, task_id)?;
    super::inbound_handoff::attach_projection(conn, task_id, &mut value)?;
    Ok(value)
}
