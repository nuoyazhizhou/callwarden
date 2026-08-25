//! Task 替代（supersede）治理 RPC 模块（T-1787203926824-9f873bfc-sub-1 基础 + T-1787277487109-758e56d0 P0-H 加固）
//!
//! 提供机器可查询、append-only 的任务替代关系：
//! - `task.supersede`：声明「旧任务被新任务替代」，写入关系表 + 一条 append-only
//!   事件 + 权威 `task_events` 审计行 + operation ledger result，**同一 SQLite
//!   transaction** 提交；不改动被替代任务的任何字段（status/closed_at/
//!   description 保持原样）。
//! - `task.superseded_by`：只读查询某任务的替代者（workspace-scoped projection）。
//!
//! P0-H 加固（相对基础任务新增的 authority/durability/provenance/promotion 门禁）：
//! - schema 权威化：`task_supersede_relations` / `task_supersede_events` 已纳入
//!   checksummed `db/schema.py`（v59）。本模块**不再**以启动期 DDL 创建/掩盖迁移
//!   （移除 `SUPERSEDE_SCHEMA_SQL` / `ensure_supersede_schema`），仅做列级
//!   fail-closed 校验（`validate_supersede_schema`）。
//! - 幂等持久化：经 `OperationStore::dedupe` + `record_result`（key =
//!   (workspace_instance_id, 'task.supersede', request_id)）。同 request_id/同
//!   canonical 参数只读重放已保存结果，不追加任何行；同 key/异参数返回
//!   `E_REQUEST_ID_REUSE_MISMATCH`；确定性拒绝（E_SUPERSEDE_*）只写可重放
//!   ledger error，不写 relation/task_event。不再以 `check_dedup/save_dedup`
//!   作为该方法唯一幂等机制。
//! - 治理门禁（写入前全部验证，任一失败返回稳定 code）：predecessor/successor
//!   存在且归属同一 workspace authority binding（`E_SUPERSEDE_TASK_NOT_FOUND` /
//!   `E_SUPERSEDE_CROSS_WORKSPACE`）；非 self edge（`E_SUPERSEDE_SELF_REFERENCE`）；
//!   无重复边（`E_SUPERSEDE_ALREADY_EXISTS`）；无间接环（全出边 BFS，
//!   `E_SUPERSEDE_CYCLE`）；完整 registered identity（`E_SUPERSEDE_IDENTITY_REQUIRED`）；
//!   role 严格 = adjudicator（`E_SUPERSEDE_ROLE_REQUIRED`）；source task 对应
//!   reviewer lease 有效 + fencing counter 当前（`E_SUPERSEDE_LEASE_REQUIRED` /
//!   `E_SUPERSEDE_FENCED`）；证据 manifest/hash 完整（`E_SUPERSEDE_EVIDENCE_REQUIRED`）。
//! - `task.superseded_by` 为 workspace-scoped projection：新行按 workspace 过滤，
//!   既有 workspace_id=0 的 legacy 行（基础任务验收数据，迁移不 UPDATE 历史行）
//!   对任意 workspace 可见，保持向后兼容。

use std::collections::HashSet;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::dispatch::{DaemonRpcError, PeerCredential};
use super::task_collab::{
    bind_task_to_workspace, parse_action_identity, record_action_identity, task_bound_workspace_id,
    ActionIdentity, TaskCollabStore,
};
use super::task_loop::operation_store::{
    DedupeOutcome, LedgerProvenance, OperationStore, ParamsRules,
};

/// 稳定错误码（P0-H 合同要求，至少含下列 10 个；全部为 pub 常量供测试引用）。
pub const ERR_SUPERSEDE_TASK_NOT_FOUND: &str = "E_SUPERSEDE_TASK_NOT_FOUND";
pub const ERR_SUPERSEDE_CROSS_WORKSPACE: &str = "E_SUPERSEDE_CROSS_WORKSPACE";
pub const ERR_SUPERSEDE_SELF_REFERENCE: &str = "E_SUPERSEDE_SELF_REFERENCE";
pub const ERR_SUPERSEDE_ALREADY_EXISTS: &str = "E_SUPERSEDE_ALREADY_EXISTS";
pub const ERR_SUPERSEDE_CYCLE: &str = "E_SUPERSEDE_CYCLE";
pub const ERR_SUPERSEDE_IDENTITY_REQUIRED: &str = "E_SUPERSEDE_IDENTITY_REQUIRED";
pub const ERR_SUPERSEDE_ROLE_REQUIRED: &str = "E_SUPERSEDE_ROLE_REQUIRED";
pub const ERR_SUPERSEDE_LEASE_REQUIRED: &str = "E_SUPERSEDE_LEASE_REQUIRED";
pub const ERR_SUPERSEDE_FENCED: &str = "E_SUPERSEDE_FENCED";
pub const ERR_SUPERSEDE_EVIDENCE_REQUIRED: &str = "E_SUPERSEDE_EVIDENCE_REQUIRED";
/// 额外稳定码：幂等 key 成员缺失（request_id / workspace_instance_id）。
pub const ERR_SUPERSEDE_REQUEST_ID_REQUIRED: &str = "E_SUPERSEDE_REQUEST_ID_REQUIRED";
pub const ERR_SUPERSEDE_WORKSPACE_REQUIRED: &str = "E_SUPERSEDE_WORKSPACE_REQUIRED";

/// P0-B：历史无绑定任务 authority attestation 的稳定错误码。
pub const ERR_LEGACY_BIND_TASK_NOT_FOUND: &str = "E_LEGACY_BIND_TASK_NOT_FOUND";
pub const ERR_LEGACY_BIND_ALREADY_BOUND: &str = "E_LEGACY_BIND_ALREADY_BOUND";
pub const ERR_LEGACY_BIND_ANCHOR_UNBOUND: &str = "E_LEGACY_BIND_ANCHOR_UNBOUND";
pub const ERR_LEGACY_BIND_SELF_REFERENCE: &str = "E_LEGACY_BIND_SELF_REFERENCE";
pub const ERR_LEGACY_BIND_IDENTITY_REQUIRED: &str = "E_LEGACY_BIND_IDENTITY_REQUIRED";
pub const ERR_LEGACY_BIND_ROLE_REQUIRED: &str = "E_LEGACY_BIND_ROLE_REQUIRED";
pub const ERR_LEGACY_BIND_LEASE_REQUIRED: &str = "E_LEGACY_BIND_LEASE_REQUIRED";
pub const ERR_LEGACY_BIND_FENCED: &str = "E_LEGACY_BIND_FENCED";
pub const ERR_LEGACY_BIND_EVIDENCE_REQUIRED: &str = "E_LEGACY_BIND_EVIDENCE_REQUIRED";
pub const ERR_LEGACY_BIND_REQUEST_ID_REQUIRED: &str = "E_LEGACY_BIND_REQUEST_ID_REQUIRED";
pub const ERR_LEGACY_BIND_WORKSPACE_REQUIRED: &str = "E_LEGACY_BIND_WORKSPACE_REQUIRED";

/// 本地权威时钟时间戳（与 task_collab::now_ts 同一实现，模块自包含）。
fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 稳定 supersedence 标识（跨 relation/event/ledger 可追踪）。
fn gen_supersedence_id(superseded: &str, superseding: &str, request_id: &str, ts: f64) -> String {
    let digest = Sha256::digest(
        format!("{}|{}|{}|{:.6}", superseded, superseding, request_id, ts).as_bytes(),
    );
    format!("SUP-{}", hex::encode(&digest[..8]))
}

/// 幂等/持久化前置：已提交 key 的只读重放结果转回结构化结果或结构化错误。
///
/// ledger 保存的 response_or_error_json 若含 `error` 对象（确定性拒绝的
/// 可重放错误），必须转回 `Err`，保证同 request_id 重试仍得到同一错误。
fn replay_to_result(response_or_error_json: Value) -> Result<Value, DaemonRpcError> {
    if let Some(err) = response_or_error_json.get("error") {
        let code = err
            .get("code")
            .and_then(|v| v.as_str())
            .unwrap_or("E_SUPERSEDE_REPLAY");
        let message = err
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or("replayed rejected task.supersede");
        return Err(DaemonRpcError::new(code, message));
    }
    Ok(response_or_error_json)
}

/// 校验请求 peer 是完整 registered identity（与 claim 门禁一致）：
/// - agent 必须已注册（agent_registrations）且 status='active'；
/// - 注册 instance 非空时必须与 identity.agent_instance_id 一致；
/// - identity.session_id 必须等于注册 session_id。
pub(crate) fn verify_registered_identity(
    conn: &Connection,
    id: &ActionIdentity,
) -> Result<(), DaemonRpcError> {
    let registered = conn
        .query_row(
            "SELECT agent_instance_id, session_id, role, status FROM agent_registrations
             WHERE agent_id = ?1",
            params![id.agent_id],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                ))
            },
        )
        .optional()
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("查询 agent_registrations 失败: {}", e))
        })?;
    let reg = registered.ok_or_else(|| {
        DaemonRpcError::new(
            "E_IDENTITY_UNREGISTERED",
            format!(
                "agent {} 未注册身份，task.supersede 拒绝（fail-closed）",
                id.agent_id
            ),
        )
    })?;
    if reg.3 != "active" {
        return Err(DaemonRpcError::new(
            "E_IDENTITY_INACTIVE",
            format!("agent {} 已停用，task.supersede 拒绝", id.agent_id),
        ));
    }
    if !reg.0.is_empty() && reg.0 != id.agent_instance_id {
        return Err(DaemonRpcError::new(
            "E_IDENTITY_INSTANCE_MISMATCH",
            format!(
                "agent {} 注册 instance {} 与本次 {} 不一致",
                id.agent_id, reg.0, id.agent_instance_id
            ),
        ));
    }
    if reg.1 != id.session_id {
        return Err(DaemonRpcError::new(
            "E_IDENTITY_SESSION_MISMATCH",
            format!(
                "agent {} 注册 session {} 与本次 {} 不一致",
                id.agent_id, reg.1, id.session_id
            ),
        ));
    }
    Ok(())
}

/// 列级 schema fail-closed 校验（替代旧启动期 DDL 的职责）。
///
/// 只读 PRAGMA table_info；缺列即拒绝服务，绝不在此处 CREATE/ALTER
/// （迁移由 checksummed `db/schema.py` v59 + `db_base._migrate_v58_to_v59` 负责）。
/// pub(crate)：供 TaskCollabStore::new/from_connection 打开权威库后调用。
pub(crate) fn validate_supersede_schema(conn: &Connection) -> Result<(), DaemonRpcError> {
    const REQUIRED_RELATION_COLS: &[&str] = &[
        "superseded_task_id",
        "superseding_task_id",
        "reason",
        "actor",
        "created_at",
        "workspace_id",
        "supersedence_id",
        "reason_code",
        "actor_agent_id",
        "actor_session_id",
        "actor_model_id",
        "actor_role",
        "request_id",
        "lease_id",
        "fencing_counter",
        "evidence_path",
        "evidence_hash",
        "authoritative_timestamp",
    ];
    const REQUIRED_EVENT_COLS: &[&str] = &[
        "event_id",
        "superseded_task_id",
        "superseding_task_id",
        "reason",
        "actor",
        "monotonic_seq",
        "authoritative_timestamp",
        "workspace_id",
        "supersedence_id",
        "reason_code",
        "actor_agent_id",
        "actor_session_id",
        "actor_model_id",
        "actor_role",
        "request_id",
        "lease_id",
        "fencing_counter",
        "evidence_path",
        "evidence_hash",
    ];
    for (table, required) in [
        ("task_supersede_relations", REQUIRED_RELATION_COLS),
        ("task_supersede_events", REQUIRED_EVENT_COLS),
    ] {
        let mut existing: Vec<String> = Vec::new();
        {
            let mut stmt = conn
                .prepare(&format!("PRAGMA table_info({})", table))
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("读取表 {} 结构失败: {}", table, e))
                })?;
            let rows = stmt
                .query_map([], |r| r.get::<_, String>(1))
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("读取表 {} 列失败: {}", table, e))
                })?;
            for row in rows {
                existing.push(row.map_err(|e| {
                    DaemonRpcError::internal_error(format!("读取表 {} 列失败: {}", table, e))
                })?);
            }
        }
        for col in required {
            if !existing.iter().any(|c| c == col) {
                return Err(DaemonRpcError::internal_error(format!(
                    "task_supersede 表 {} 缺少 v59 列 {}（官方迁移未补齐，fail-closed）",
                    table, col
                )));
            }
        }
    }
    Ok(())
}

impl TaskCollabStore {
    /// 声明任务替代关系（task.supersede，P0-H 治理 mutation）。
    ///
    /// 参数（除基础 superseded_id/superseding_id/reason 外均为 P0-H 新增）：
    /// - request_id：幂等 key 成员（必填，`E_SUPERSEDE_REQUEST_ID_REQUIRED`）
    /// - workspace_instance_id：幂等 key 成员（必填，`E_SUPERSEDE_WORKSPACE_REQUIRED`）
    /// - identity：完整四字段（agent_id/session_id/model_id/role），role 严格 =
    ///   adjudicator（`E_SUPERSEDE_ROLE_REQUIRED`）
    /// - lease_token + fencing_counter：source task（predecessor）的 reviewer lease
    ///   凭证（`E_SUPERSEDE_LEASE_REQUIRED` / `E_SUPERSEDE_FENCED`）
    /// - evidence_path + evidence_hash：证据 manifest/hash（`E_SUPERSEDE_EVIDENCE_REQUIRED`）
    /// - workspace_id（可选，路由注入）：请求 workspace 与 task binding 一致性校验
    ///
    /// 成功：relation + append-only event + 权威 task_events 审计行 + ledger result
    /// 同一 SQLite transaction 提交；同 request_id/同 canonical 参数只读重放。
    /// 确定性拒绝只写可重放 ledger error，不写 relation/task_event。
    pub fn handle_task_supersede(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        // 0) schema fail-closed（旧启动期 DDL 的替代职责）
        {
            let conn = self.conn.lock().unwrap();
            validate_supersede_schema(&conn)?;
        }

        // 1) 幂等/持久化前置参数（缺失即 fail-closed，稳定 code）
        let superseded_id = params
            .get("superseded_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        let superseding_id = params
            .get("superseding_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        if superseded_id.is_empty() || superseding_id.is_empty() {
            return Err(DaemonRpcError::invalid_params(
                "task.supersede 需要 superseded_id 与 superseding_id",
            ));
        }
        let request_id = params
            .get("request_id")
            .and_then(|v| v.as_str())
            .map(|s| s.trim())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| {
                DaemonRpcError::new(
                    ERR_SUPERSEDE_REQUEST_ID_REQUIRED,
                    "task.supersede 必须携带 request_id（幂等 key 成员，fail-closed）",
                )
            })?
            .to_string();
        let workspace_instance_id = params
            .get("workspace_instance_id")
            .and_then(|v| v.as_str())
            .map(|s| s.trim())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| {
                DaemonRpcError::new(
                    ERR_SUPERSEDE_WORKSPACE_REQUIRED,
                    "task.supersede 必须携带 workspace_instance_id（幂等 key 成员，fail-closed）",
                )
            })?
            .to_string();
        let requested_workspace_id = params
            .get("workspace_id")
            .and_then(|v| {
                v.as_i64().or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok()))
            })
            .filter(|id| *id > 0);

        // 2) 身份/角色/租约/证据前置（稳定 code；任一缺失 fail-closed）
        let identity = parse_action_identity(params)?.ok_or_else(|| {
            DaemonRpcError::new(
                ERR_SUPERSEDE_IDENTITY_REQUIRED,
                "task.supersede 必须携带完整 identity（agent_id/session_id/model_id/role）",
            )
        })?;
        if identity.role != "adjudicator" {
            return Err(DaemonRpcError::new(
                ERR_SUPERSEDE_ROLE_REQUIRED,
                format!(
                    "task.supersede 仅允许 role=adjudicator 调用（实际 role={}）",
                    identity.role
                ),
            ));
        }
        let (token, counter) = Self::require_lease_params(params).map_err(|_| {
            DaemonRpcError::new(
                ERR_SUPERSEDE_LEASE_REQUIRED,
                "task.supersede 必须携带完整 reviewer lease 凭证（lease_token + fencing_counter）",
            )
        })?;
        let evidence_path = params
            .get("evidence_path")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        let evidence_hash = params
            .get("evidence_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        if evidence_path.is_empty() || evidence_hash.is_empty() {
            return Err(DaemonRpcError::new(
                ERR_SUPERSEDE_EVIDENCE_REQUIRED,
                "task.supersede 必须携带完整证据 manifest（evidence_path + evidence_hash）",
            ));
        }
        if superseded_id == superseding_id {
            return Err(DaemonRpcError::new(
                ERR_SUPERSEDE_SELF_REFERENCE,
                "task.supersede 不能自替代（superseded_id == superseding_id）",
            ));
        }
        let reason = params
            .get("reason")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let reason_code = params
            .get("reason_code")
            .and_then(|v| v.as_str())
            .filter(|s| !s.trim().is_empty())
            .unwrap_or("governance_supersede")
            .to_string();

        // 3) OperationStore 幂等去重（key = (workspace_instance_id, method, request_id)）
        let mut conn = self.conn.lock().unwrap();
        let outcome = OperationStore.dedupe(
            &conn,
            &workspace_instance_id,
            "task.supersede",
            &request_id,
            params,
        )?;
        let (rules, canonical_params_hash): (ParamsRules, String) = match outcome {
            DedupeOutcome::Replay { response_or_error_json } => {
                return replay_to_result(response_or_error_json);
            }
            DedupeOutcome::FirstRequest { rules, canonical_params_hash } => {
                (rules, canonical_params_hash)
            }
        };

        let tx = conn
            .unchecked_transaction()
            .map_err(|e| DaemonRpcError::internal_error(format!("开启事务失败: {}", e)))?;
        let provenance = LedgerProvenance {
            workspace_id: requested_workspace_id,
            task_id: Some(superseded_id.to_string()),
            ..Default::default()
        };

        // 确定性拒绝 helper：同事务写可重放 ledger error（tx 显式传参，避免
        // 闭包持有借用到作用域尾；commit 由各调用点统一执行）。
        // 不写任何 relation/task_event（contract：确定性拒绝只写可重放 ledger error）。
        let record_error =
            |tx: &Transaction<'_>, code: &str, message: &str| -> DaemonRpcError {
                let err_json = json!({ "error": { "code": code, "message": message } });
                let _ = OperationStore.record_result(
                    tx,
                    &workspace_instance_id,
                    "task.supersede",
                    &request_id,
                    &rules,
                    &canonical_params_hash,
                    &provenance,
                    &err_json,
                );
                DaemonRpcError::new(code, message)
            };
        macro_rules! reject_deterministic {
            ($code:expr, $msg:expr) => {{
                let err = record_error(&tx, $code, $msg);
                let _ = tx.commit();
                return Err(err);
            }};
        }

        // 4) 领域校验（确定性拒绝 → 写可重放 ledger error 后提交）
        let ws_id = match self.validate_supersede_domain(
            &tx,
            superseded_id,
            superseding_id,
            requested_workspace_id,
        ) {
            Ok(ws) => ws,
            Err(e) => reject_deterministic!(&e.code, &e.message),
        };
        if let Err(e) = verify_registered_identity(&tx, &identity) {
            reject_deterministic!(&e.code, &e.message);
        }
        if let Err(e) = self.validate_reviewer_lease_for_adjudication(
            &tx,
            superseded_id,
            &token,
            counter,
            &identity,
        ) {
            // fencing counter 不匹配 → E_SUPERSEDE_FENCED（P0-H 合同稳定码）；
            // 其余 lease 错误（缺 lease/过期/token 不匹配/身份不符）按稳定码传播。
            let code = if e.code == "E_LEASE_FENCING_STALE" {
                ERR_SUPERSEDE_FENCED.to_string()
            } else {
                e.code.clone()
            };
            reject_deterministic!(&code, &e.message);
        }
        // 取 lease_id 供 relation/event/ledger provenance 落库
        let lease_id: String = tx
            .query_row(
                "SELECT lease_id FROM task_leases \
                 WHERE task_id = ?1 AND role = 'reviewer' AND status = 'active' \
                 ORDER BY id ASC LIMIT 1",
                params![superseded_id],
                |r| r.get(0),
            )
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 reviewer lease_id 失败: {}", e))
            })?;

        // 5) 同一事务写入：relation + append-only event + task_events 审计 + ledger result
        let ts = now_ts();
        let supersedence_id = gen_supersedence_id(superseded_id, superseding_id, &request_id, ts);
        let actor = identity.agent_id.clone();
        tx.execute(
            "INSERT INTO task_supersede_relations
             (superseded_task_id, superseding_task_id, reason, actor, created_at,
              workspace_id, supersedence_id, reason_code,
              actor_agent_id, actor_session_id, actor_model_id, actor_role,
              request_id, lease_id, fencing_counter,
              evidence_path, evidence_hash, authoritative_timestamp)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18)",
            params![
                superseded_id,
                superseding_id,
                reason,
                actor,
                ts,
                ws_id,
                supersedence_id,
                reason_code,
                identity.agent_id,
                identity.session_id,
                identity.model_id,
                identity.role,
                request_id,
                lease_id,
                counter,
                evidence_path,
                evidence_hash,
                ts,
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("写入替代关系失败: {}", e)))?;

        let ev_seq: i64 = tx
            .query_row(
                "SELECT COALESCE(MAX(monotonic_seq), 0) + 1 FROM task_supersede_events",
                [],
                |r| r.get(0),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("取事件序号失败: {}", e)))?;
        tx.execute(
            "INSERT INTO task_supersede_events
             (superseded_task_id, superseding_task_id, reason, actor, monotonic_seq,
              authoritative_timestamp, workspace_id, supersedence_id, reason_code,
              actor_agent_id, actor_session_id, actor_model_id, actor_role,
              request_id, lease_id, fencing_counter, evidence_path, evidence_hash)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18)",
            params![
                superseded_id,
                superseding_id,
                reason,
                actor,
                ev_seq,
                ts,
                ws_id,
                supersedence_id,
                reason_code,
                identity.agent_id,
                identity.session_id,
                identity.model_id,
                identity.role,
                request_id,
                lease_id,
                counter,
                evidence_path,
                evidence_hash,
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("写入替代事件失败: {}", e)))?;

        // 权威 task_events 审计行（append-only；被替代任务字段不变）
        let from_status: String = tx
            .query_row(
                "SELECT status FROM tasks WHERE id = ?1",
                params![superseded_id],
                |r| r.get(0),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("读取被替代任务状态失败: {}", e)))?;
        let audit_seq = self.next_seq();
        TaskCollabStore::append_task_event(
            &tx,
            superseded_id,
            &from_status,
            &from_status,
            "task_superseded",
            &format!("{} superseded by {}", superseded_id, superseding_id),
            &identity.agent_id,
            &identity.session_id,
            &identity.role,
            audit_seq,
            ts,
        )?;
        record_action_identity(&tx, superseded_id, &identity, "task.supersede", audit_seq, ts)?;

        // ledger result（同事务；同 key 重放返回同一结果）
        let result = json!({
            "superseded_task_id": superseded_id,
            "superseding_task_id": superseding_id,
            "status": "superseded",
            "reason": reason,
            "reason_code": reason_code,
            "actor": actor,
            "workspace_id": ws_id,
            "supersedence_id": supersedence_id,
            "request_id": request_id,
            "lease_id": lease_id,
            "fencing_counter": counter,
            "evidence_path": evidence_path,
            "evidence_hash": evidence_hash,
            "authoritative_timestamp": ts,
        });
        OperationStore.record_result(
            &tx,
            &workspace_instance_id,
            "task.supersede",
            &request_id,
            &rules,
            &canonical_params_hash,
            &provenance,
            &result,
        )?;
        tx.commit()
            .map_err(|e| DaemonRpcError::internal_error(format!("提交事务失败: {}", e)))?;
        Ok(result)
    }

    /// P0-B：对历史无 binding 的 task 追加一次 authority attestation/binding。
    ///
    /// 该操作使用已绑定 anchor task 的 reviewer lease/fencing 作为 bootstrap
    /// governance 凭证；legacy task 自身必须尚无 binding，成功后只追加
    /// capture、binding、task_events 与 operation-ledger，绝不更新 legacy task 行。
    pub fn handle_task_attest_legacy_workspace_binding(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let legacy_task_id = params
            .get("legacy_task_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        let anchor_task_id = params
            .get("anchor_task_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        if legacy_task_id.is_empty() || anchor_task_id.is_empty() {
            return Err(DaemonRpcError::new(
                ERR_LEGACY_BIND_TASK_NOT_FOUND,
                "task.attest_legacy_workspace_binding 需要 legacy_task_id 与 anchor_task_id",
            ));
        }
        if legacy_task_id == anchor_task_id {
            return Err(DaemonRpcError::new(
                ERR_LEGACY_BIND_SELF_REFERENCE,
                "legacy_task_id 不得等于 anchor_task_id",
            ));
        }
        let request_id = params
            .get("request_id")
            .and_then(|v| v.as_str())
            .map(|s| s.trim())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::new(
                ERR_LEGACY_BIND_REQUEST_ID_REQUIRED,
                "task.attest_legacy_workspace_binding 必须携带 request_id",
            ))?
            .to_string();
        let workspace_instance_id = params
            .get("workspace_instance_id")
            .and_then(|v| v.as_str())
            .map(|s| s.trim())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DaemonRpcError::new(
                ERR_LEGACY_BIND_WORKSPACE_REQUIRED,
                "task.attest_legacy_workspace_binding 必须携带 workspace_instance_id",
            ))?
            .to_string();
        let workspace_id = params
            .get("workspace_id")
            .and_then(|v| v.as_i64().or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok())))
            .filter(|id| *id > 0)
            .ok_or_else(|| DaemonRpcError::new(
                ERR_LEGACY_BIND_WORKSPACE_REQUIRED,
                "task.attest_legacy_workspace_binding 必须携带 workspace_id > 0",
            ))?;
        let identity = parse_action_identity(params)?.ok_or_else(|| DaemonRpcError::new(
            ERR_LEGACY_BIND_IDENTITY_REQUIRED,
            "task.attest_legacy_workspace_binding 必须携带完整 identity",
        ))?;
        if identity.role != "adjudicator" {
            return Err(DaemonRpcError::new(
                ERR_LEGACY_BIND_ROLE_REQUIRED,
                format!("仅允许 role=adjudicator，实际 role={}", identity.role),
            ));
        }
        let (token, counter) = Self::require_lease_params(params).map_err(|_| {
            DaemonRpcError::new(
                ERR_LEGACY_BIND_LEASE_REQUIRED,
                "必须携带 anchor task 的 reviewer lease_token 与 fencing_counter",
            )
        })?;
        let evidence_path = params
            .get("evidence_path")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        let evidence_hash = params
            .get("evidence_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        if evidence_path.is_empty() || evidence_hash.is_empty() {
            return Err(DaemonRpcError::new(
                ERR_LEGACY_BIND_EVIDENCE_REQUIRED,
                "必须携带 evidence_path 与 evidence_hash",
            ));
        }

        let mut conn = self.conn.lock().unwrap();
        let method = "task.attest_legacy_workspace_binding";
        let outcome = OperationStore.dedupe(&conn, &workspace_instance_id, method, &request_id, params)?;
        let (rules, canonical_params_hash): (ParamsRules, String) = match outcome {
            DedupeOutcome::Replay { response_or_error_json } => return replay_to_result(response_or_error_json),
            DedupeOutcome::FirstRequest { rules, canonical_params_hash } => (rules, canonical_params_hash),
        };
        let tx = conn.unchecked_transaction().map_err(|e| {
            DaemonRpcError::internal_error(format!("开启 legacy binding 事务失败: {}", e))
        })?;
        let provenance = LedgerProvenance {
            workspace_id: Some(workspace_id),
            task_id: Some(legacy_task_id.to_string()),
            ..Default::default()
        };
        let record_error = |tx: &Transaction<'_>, code: &str, message: &str| -> DaemonRpcError {
            let err_json = json!({ "error": { "code": code, "message": message } });
            let _ = OperationStore.record_result(
                tx, &workspace_instance_id, method, &request_id, &rules,
                &canonical_params_hash, &provenance, &err_json,
            );
            DaemonRpcError::new(code, message)
        };
        macro_rules! reject_deterministic {
            ($code:expr, $msg:expr) => {{
                let err = record_error(&tx, $code, $msg);
                let _ = tx.commit();
                return Err(err);
            }};
        }

        for task_id in [legacy_task_id, anchor_task_id] {
            let exists: bool = match tx.query_row(
                "SELECT EXISTS(SELECT 1 FROM tasks WHERE id = ?1)",
                params![task_id],
                |row| row.get(0),
            ) {
                Ok(value) => value,
                Err(e) => return Err(DaemonRpcError::internal_error(format!(
                    "查询 legacy binding task 存在性失败: {}", e
                ))),
            };
            if !exists {
                reject_deterministic!(
                    ERR_LEGACY_BIND_TASK_NOT_FOUND,
                    &format!("task.attest_legacy_workspace_binding 引用的任务不存在: {}", task_id)
                );
            }
        }
        let already_bound: bool = match tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM task_workspace_bindings WHERE task_id = ?1)",
            params![legacy_task_id],
            |row| row.get(0),
        ) {
            Ok(value) => value,
            Err(e) => return Err(DaemonRpcError::internal_error(format!(
                "查询 legacy task binding 失败: {}", e
            ))),
        };
        if already_bound {
            reject_deterministic!(
                ERR_LEGACY_BIND_ALREADY_BOUND,
                &format!("legacy task {} 已有不可变 workspace binding", legacy_task_id)
            );
        }
        let anchor_workspace = match task_bound_workspace_id(&tx, anchor_task_id, Some(workspace_id)) {
            Ok(value) => value,
            Err(e) if e.code == "E_TASK_WORKSPACE_UNBOUND" => {
                reject_deterministic!(ERR_LEGACY_BIND_ANCHOR_UNBOUND, &e.message)
            }
            Err(e) => reject_deterministic!(&e.code, &e.message),
        };
        let anchor_instance: String = match tx.query_row(
            "SELECT c.workspace_instance_id \
             FROM task_workspace_bindings b \
             JOIN workspace_authority_captures c ON c.workspace_capture_id = b.workspace_capture_id \
             WHERE b.task_id = ?1 AND b.workspace_id = ?2",
            params![anchor_task_id, anchor_workspace],
            |row| row.get(0),
        ) {
            Ok(value) => value,
            Err(_) => reject_deterministic!(
                ERR_LEGACY_BIND_ANCHOR_UNBOUND,
                "anchor task 缺少可验证的 workspace authority capture"
            ),
        };
        if anchor_instance != workspace_instance_id {
            reject_deterministic!(
                "E_WORKSPACE_AUTHORITY_MISMATCH",
                &format!(
                    "anchor workspace_instance_id={} 与请求 {} 不一致",
                    anchor_instance, workspace_instance_id
                )
            );
        }
        if let Err(e) = verify_registered_identity(&tx, &identity) {
            reject_deterministic!(&e.code, &e.message);
        }
        if let Err(e) = self.validate_reviewer_lease_for_adjudication(
            &tx, anchor_task_id, &token, counter, &identity,
        ) {
            let code = if e.code == "E_LEASE_FENCING_STALE" {
                ERR_LEGACY_BIND_FENCED
            } else {
                &e.code
            };
            reject_deterministic!(code, &e.message);
        }
        let lease_id: String = match tx.query_row(
            "SELECT lease_id FROM task_leases \
             WHERE task_id = ?1 AND role = 'reviewer' AND status = 'active' \
             ORDER BY id ASC LIMIT 1",
            params![anchor_task_id],
            |row| row.get(0),
        ) {
            Ok(value) => value,
            Err(e) => return Err(DaemonRpcError::internal_error(format!(
                "查询 anchor reviewer lease_id 失败: {}", e
            ))),
        };
        let legacy_status: String = match tx.query_row(
            "SELECT status FROM tasks WHERE id = ?1",
            params![legacy_task_id],
            |row| row.get(0),
        ) {
            Ok(value) => value,
            Err(e) => return Err(DaemonRpcError::internal_error(format!(
                "读取 legacy task status 失败: {}", e
            ))),
        };
        let (binding_id, capture_id) = match bind_task_to_workspace(
            &tx, legacy_task_id, workspace_id, &workspace_instance_id, &identity.agent_id,
        ) {
            Ok(value) => value,
            Err(e) if e.code == "E_WORKSPACE_AUTHORITY_MISMATCH" => {
                reject_deterministic!(&e.code, &e.message)
            }
            Err(e) => return Err(e),
        };
        let ts = now_ts();
        let audit_seq = self.next_seq();
        TaskCollabStore::append_task_event(
            &tx,
            legacy_task_id,
            &legacy_status,
            &legacy_status,
            "legacy_workspace_binding_attested",
            &format!("{} attested against anchor {}", legacy_task_id, anchor_task_id),
            &identity.agent_id,
            &identity.session_id,
            &identity.role,
            audit_seq,
            ts,
        )?;
        record_action_identity(
            &tx, legacy_task_id, &identity, method, audit_seq, ts,
        )?;
        let result = json!({
            "legacy_task_id": legacy_task_id,
            "anchor_task_id": anchor_task_id,
            "status": "attested",
            "workspace_id": workspace_id,
            "workspace_instance_id": workspace_instance_id,
            "workspace_binding_id": binding_id,
            "workspace_capture_id": capture_id,
            "request_id": request_id,
            "lease_id": lease_id,
            "fencing_counter": counter,
            "evidence_path": evidence_path,
            "evidence_hash": evidence_hash,
            "authoritative_timestamp": ts,
        });
        OperationStore.record_result(
            &tx, &workspace_instance_id, method, &request_id, &rules,
            &canonical_params_hash, &provenance, &result,
        )?;
        tx.commit().map_err(|e| {
            DaemonRpcError::internal_error(format!("提交 legacy binding 事务失败: {}", e))
        })?;
        Ok(result)
    }

    /// 领域校验（可确定性拒绝的部分；全部在事务内只读执行，不写任何行）。
    ///
    /// 返回两任务共同归属的 workspace_id（来自不可变 binding）。
    fn validate_supersede_domain(
        &self,
        conn: &Connection,
        superseded_id: &str,
        superseding_id: &str,
        requested_workspace_id: Option<i64>,
    ) -> Result<i64, DaemonRpcError> {
        // 1) 两任务必须存在（仅读 tasks 表，不改任何字段）
        for tid in [superseded_id, superseding_id] {
            let exists: bool = conn
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM tasks WHERE id = ?1)",
                    params![tid],
                    |r| r.get(0),
                )
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("查询任务 {} 存在性失败: {}", tid, e))
                })?;
            if !exists {
                return Err(DaemonRpcError::new(
                    ERR_SUPERSEDE_TASK_NOT_FOUND,
                    format!("task.supersede 引用的任务不存在: {}", tid),
                ));
            }
        }
        // 2) 同一 workspace authority binding（不可变 task_workspace_bindings；
        //    请求 workspace 与 binding 不一致时由 task_bound_workspace_id fail-closed）
        let ws_pre = task_bound_workspace_id(conn, superseded_id, requested_workspace_id)?;
        let ws_suc = task_bound_workspace_id(conn, superseding_id, requested_workspace_id)?;
        if ws_pre != ws_suc {
            return Err(DaemonRpcError::new(
                ERR_SUPERSEDE_CROSS_WORKSPACE,
                format!(
                    "task.supersede 跨 workspace: {} (ws={}) -> {} (ws={})",
                    superseded_id, ws_pre, superseding_id, ws_suc
                ),
            ));
        }
        // 3) 不重复（同一对 superseded/superseding 已存在；PK 亦强制唯一）
        let dup: bool = conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM task_supersede_relations \
                 WHERE superseded_task_id = ?1 AND superseding_task_id = ?2)",
                params![superseded_id, superseding_id],
                |r| r.get(0),
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("查重失败: {}", e)))?;
        if dup {
            return Err(DaemonRpcError::new(
                ERR_SUPERSEDE_ALREADY_EXISTS,
                format!("task.supersede 关系已存在: {} -> {}", superseded_id, superseding_id),
            ));
        }
        // 4) 不构成环（全出边 BFS；多出边确定性检测）
        if creates_cycle(conn, superseded_id, superseding_id)? {
            return Err(DaemonRpcError::new(
                ERR_SUPERSEDE_CYCLE,
                format!(
                    "task.supersede 会构成替代环：{} -> {}",
                    superseded_id, superseding_id
                ),
            ));
        }
        Ok(ws_pre)
    }

    /// 只读查询某任务的替代者（task.superseded_by，workspace-scoped projection）。
    ///
    /// - workspace 过滤：关系行 workspace_id = 请求 workspace 或 = 0（legacy 行，
    ///   基础任务验收数据；迁移不 UPDATE 历史行，故对任意 workspace 可见）。
    /// - 输出 workspace/provenance（supersedence_id/reason_code/request_id/lease_id/
    ///   fencing_counter/evidence path-hash/actor 四字段）。
    pub fn handle_task_superseded_by(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .or_else(|| params.get("superseded_id").and_then(|v| v.as_str()))
            .unwrap_or("")
            .trim();
        if task_id.is_empty() {
            return Err(DaemonRpcError::invalid_params(
                "task.superseded_by 需要 task_id",
            ));
        }
        let requested_workspace_id: Option<i64> = params
            .get("workspace_id")
            .and_then(|v| {
                v.as_i64().or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok()))
            })
            .filter(|id| *id > 0);

        let conn = self.conn.lock().unwrap();
        let row: Option<(
            String, String, String, f64, String, String, String, String, String, i64, String,
            String, String, f64, i64,
        )> = match requested_workspace_id {
            Some(ws) => conn
                .query_row(
                    "SELECT superseding_task_id, reason, actor, created_at,
                            supersedence_id, reason_code, request_id, lease_id, evidence_path,
                            fencing_counter, evidence_hash, actor_agent_id, actor_role,
                            authoritative_timestamp, workspace_id
                     FROM task_supersede_relations
                     WHERE superseded_task_id = ?1 AND (workspace_id = ?2 OR workspace_id = 0)
                     ORDER BY authoritative_timestamp DESC, created_at DESC LIMIT 1",
                    params![task_id, ws],
                    |r| {
                        Ok((
                            r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?,
                            r.get(6)?, r.get(7)?, r.get(8)?, r.get(9)?, r.get(10)?, r.get(11)?,
                            r.get(12)?, r.get(13)?, r.get(14)?,
                        ))
                    },
                )
                .optional()
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("查询替代者失败: {}", e))
                })?,
            None => conn
                .query_row(
                    "SELECT superseding_task_id, reason, actor, created_at,
                            supersedence_id, reason_code, request_id, lease_id, evidence_path,
                            fencing_counter, evidence_hash, actor_agent_id, actor_role,
                            authoritative_timestamp, workspace_id
                     FROM task_supersede_relations
                     WHERE superseded_task_id = ?1
                     ORDER BY authoritative_timestamp DESC, created_at DESC LIMIT 1",
                    params![task_id],
                    |r| {
                        Ok((
                            r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?,
                            r.get(6)?, r.get(7)?, r.get(8)?, r.get(9)?, r.get(10)?, r.get(11)?,
                            r.get(12)?, r.get(13)?, r.get(14)?,
                        ))
                    },
                )
                .optional()
                .map_err(|e| {
                    DaemonRpcError::internal_error(format!("查询替代者失败: {}", e))
                })?,
        };

        let mut res = Map::new();
        res.insert("task_id".to_string(), Value::String(task_id.to_string()));
        match row {
            Some((
                superseding, reason, actor, created_at, supersedence_id, reason_code,
                request_id, lease_id, evidence_path, fencing_counter, evidence_hash,
                actor_agent_id, actor_role, authoritative_timestamp, workspace_id,
            )) => {
                res.insert("found".to_string(), Value::Bool(true));
                res.insert("superseding_task_id".to_string(), Value::String(superseding));
                res.insert("reason".to_string(), Value::String(reason));
                res.insert("actor".to_string(), Value::String(actor));
                res.insert("created_at".to_string(), Value::from(created_at));
                res.insert("workspace_id".to_string(), Value::from(workspace_id));
                res.insert("supersedence_id".to_string(), Value::String(supersedence_id));
                res.insert("reason_code".to_string(), Value::String(reason_code));
                res.insert("request_id".to_string(), Value::String(request_id));
                res.insert("lease_id".to_string(), Value::String(lease_id));
                res.insert("fencing_counter".to_string(), Value::from(fencing_counter));
                res.insert("evidence_path".to_string(), Value::String(evidence_path));
                res.insert("evidence_hash".to_string(), Value::String(evidence_hash));
                res.insert("actor_agent_id".to_string(), Value::String(actor_agent_id));
                res.insert("actor_role".to_string(), Value::String(actor_role));
                res.insert(
                    "authoritative_timestamp".to_string(),
                    Value::from(authoritative_timestamp),
                );
            }
            None => {
                res.insert("found".to_string(), Value::Bool(false));
            }
        }
        Ok(Value::Object(res))
    }
}

/// 判断新增边 (superseded -> superseding) 是否会构成替代环。
///
/// 沿「新方向」(superseded_task_id -> superseding_task_id) 从 `superseding` 出发，
/// **遍历全部出边**（复合主键允许一个任务被多个任务替代，A→B、A→C 并存），
/// 任一可达路径回到 `superseded` 即成环。自环由调用方先行拒绝。
///
/// 采用全出边 BFS（确定性，不依赖 SQLite 行序；旧实现 `LIMIT 1` 单出边遍历
/// 会漏检多出边环且结果非确定）。`visited` 防环安全停止（既有数据理论上无环，
/// 防御性保留）。
fn creates_cycle(
    conn: &Connection,
    superseded: &str,
    superseding: &str,
) -> Result<bool, DaemonRpcError> {
    let mut queue: Vec<String> = vec![superseding.to_string()];
    let mut visited: HashSet<String> = HashSet::new();
    visited.insert(superseding.to_string());

    let mut stmt = conn
        .prepare(
            "SELECT superseding_task_id FROM task_supersede_relations \
             WHERE superseded_task_id = ?1",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("环检测准备失败: {}", e)))?;

    while let Some(cur) = queue.pop() {
        let rows = stmt
            .query_map(params![cur], |r| r.get::<_, String>(0))
            .map_err(|e| DaemonRpcError::internal_error(format!("环检测查询失败: {}", e)))?;
        for nxt in rows {
            let nxt = nxt
                .map_err(|e| DaemonRpcError::internal_error(format!("环检测行解析失败: {}", e)))?;
            if nxt == superseded {
                return Ok(true);
            }
            if visited.insert(nxt.clone()) {
                queue.push(nxt);
            }
        }
    }
    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::daemon::clock::AuthoritativeClock;
    use crate::daemon::dispatch::PeerCredential;
    use rusqlite::Connection;
    use std::sync::Arc;

    const TEST_AGENT: &str = "adjudicator-workbuddy-v1";
    const TEST_SESSION: &str = "sess-adjudicator-1";
    const TEST_MODEL: &str = "deepseek-v4";
    const TEST_LEASE_TOKEN: &str = "tok-reviewer-lease-1";
    const TEST_EVIDENCE: &str = "evidence/verdict-pass.json";

    fn sha256_hex(bytes: &[u8]) -> String {
        // 与 task_loop/lifecycle_lease.rs 一致：token hash 为纯 hex（无 sha256: 前缀，Req 11.2）
        let digest = Sha256::digest(bytes);
        hex::encode(digest)
    }

    fn clocked_store() -> TaskCollabStore {
        let conn = Connection::open_in_memory().unwrap();
        TaskCollabStore::from_connection(conn)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()))
    }

    fn peer() -> PeerCredential {
        PeerCredential::new_windows("test-sid-supersede".to_string(), 1234)
    }

    fn insert_task(conn: &Connection, id: &str) {
        let ts = now_ts();
        conn.execute(
            "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at) \
             VALUES (?1, ?2, '', 'agent', 'open', ?3, ?4)",
            params![id, id, ts, ts],
        )
        .unwrap();
    }

    /// 为 task seed workspace(1) + capture + 不可变 binding（幂等，与 task_collab 测试同构）。
    fn seed_task_binding(store: &TaskCollabStore, task_id: &str) {
        let ts = 1_700_000_000.0_f64;
        let conn = store.conn.lock().unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) \
             VALUES (1, 'test-ws', '/tmp/test-ws', ?1, 1)",
            params![ts],
        )
        .unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO workspace_authority_captures
             (workspace_capture_id, workspace_id, capture_revision, supersedes_capture_id,
              daemon_workspace_id, workspace_instance_id, capture_canonicalization_version,
              capture_canonicalization_rules_hash, registry_identity_payload_json,
              registry_identity_hash, workspace_manifest_payload_json, workspace_manifest_hash,
              client_view_root_hash, host_real_root_hash, created_by, authoritative_created_at)
             VALUES (?1, 1, 1, NULL, 0, ?3, 'workspace-capture-c14n/v1',
                     'test-rules-hash', '{}', ?4, '{}', 'test-manifest-hash',
                     'test-root-hash', 'test-root-hash', 'test', ?2)",
            params![
                format!("cap-sup-{}", task_id),
                ts,
                format!("ws-inst-sup-{}", task_id),
                format!("test-identity-hash-{}", task_id),
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO task_workspace_bindings
             (task_id, workspace_id, workspace_binding_id, workspace_capture_id, created_by, authoritative_created_at)
             VALUES (?1, 1, ?2, ?3, 'test', ?4)",
            params![
                task_id,
                format!("tb-sup-{}", task_id),
                format!("cap-sup-{}", task_id),
                ts,
            ],
        )
        .unwrap();
        drop(conn);
    }

    fn register_agent(store: &TaskCollabStore) {
        let ts = now_ts();
        let conn = store.conn.lock().unwrap();
        conn.execute(
            "INSERT OR REPLACE INTO agent_registrations
             (agent_id, agent_name, owner_key, capabilities, registered_at, last_heartbeat, status,
              agent_instance_id, client_id, provider, model_id, model_mode,
              system_fingerprint, runtime_hash, session_id, role)
             VALUES (?1, 'test', 'owner', '{}', ?2, ?2, 'active', 'inst-1', 'cli', 'provider',
                     ?3, 'default', 'fp', 'rh', ?4, 'adjudicator')",
            params![TEST_AGENT, ts, TEST_MODEL, TEST_SESSION],
        )
        .unwrap();
        drop(conn);
    }

    /// 为 source task seed 一条 active reviewer lease（token_hash = sha256(token)，
    /// 有效期基于当前权威时钟，避免过期）。
    fn seed_reviewer_lease(store: &TaskCollabStore, task_id: &str) {
        let now = now_ts();
        let conn = store.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO task_leases
             (workspace_id, lease_id, task_id, role, agent_id, session_id, model_id,
              token_hash, fencing_counter, acquired_at, expires_at, status)
             VALUES (1, ?1, ?2, 'reviewer', ?3, ?4, ?5, ?6, 1, ?7, ?8, 'active')",
            params![
                format!("lease-sup-{}", task_id),
                task_id,
                TEST_AGENT,
                TEST_SESSION,
                TEST_MODEL,
                sha256_hex(TEST_LEASE_TOKEN.as_bytes()),
                now,
                now + 3600.0,
            ],
        )
        .unwrap();
        drop(conn);
    }

    fn base_params(superseded: &str, superseding: &str) -> Value {
        json!({
            "superseded_id": superseded,
            "superseding_id": superseding,
            "reason": "governance supersede under A' plan",
            "request_id": format!("req-sup-{}-{}", superseded, superseding),
            "workspace_instance_id": "ws-inst-sup-test",
            "workspace_id": 1,
            "identity": {
                "agent_id": TEST_AGENT,
                "agent_instance_id": "inst-1",
                "session_id": TEST_SESSION,
                "model_id": TEST_MODEL,
                "role": "adjudicator",
            },
            "lease_token": TEST_LEASE_TOKEN,
            "fencing_counter": 1,
            "evidence_path": TEST_EVIDENCE,
            "evidence_hash": "sha256:ev-1",
            "reason_code": "governance_supersede",
        })
    }

    fn setup_pair(store: &TaskCollabStore, old: &str, new: &str) {
        {
            let conn = store.conn.lock().unwrap();
            insert_task(&conn, old);
            insert_task(&conn, new);
        }
        seed_task_binding(store, old);
        seed_task_binding(store, new);
        seed_reviewer_lease(store, old);
    }

    fn setup_legacy_attestation(store: &TaskCollabStore, legacy: &str, anchor: &str) {
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "INSERT INTO workspaces (id, name, root_path, created_at, is_active) \
                 VALUES (1, 'ws-attest', '/tmp/ws-attest', 0.0, 1)",
                [],
            )
            .unwrap();
            insert_task(&conn, legacy);
            insert_task(&conn, anchor);
        }
        {
            let mut conn = store.conn.lock().unwrap();
            let tx = conn.unchecked_transaction().unwrap();
            bind_task_to_workspace(&tx, anchor, 1, "ws-attest-test", "test").unwrap();
            tx.commit().unwrap();
        }
        seed_reviewer_lease(store, anchor);
        register_agent(store);
    }

    fn legacy_attest_params(legacy: &str, anchor: &str) -> Value {
        json!({
            "legacy_task_id": legacy,
            "anchor_task_id": anchor,
            "workspace_id": 1,
            "workspace_instance_id": "ws-attest-test",
            "request_id": format!("req-attest-{}-{}", legacy, anchor),
            "identity": {
                "agent_id": TEST_AGENT,
                "agent_instance_id": "inst-1",
                "session_id": TEST_SESSION,
                "model_id": TEST_MODEL,
                "role": "adjudicator",
            },
            "lease_token": TEST_LEASE_TOKEN,
            "fencing_counter": 1,
            "evidence_path": TEST_EVIDENCE,
            "evidence_hash": "sha256:legacy-attest-test",
        })
    }

    #[test]
    fn test_legacy_workspace_attestation_appends_binding_without_task_mutation() {
        let store = clocked_store();
        setup_legacy_attestation(&store, "LEGACY", "ANCHOR");
        let params = legacy_attest_params("LEGACY", "ANCHOR");
        let result = store
            .handle_task_attest_legacy_workspace_binding(peer(), &params)
            .unwrap();
        assert_eq!(result["legacy_task_id"], "LEGACY");
        assert_eq!(result["anchor_task_id"], "ANCHOR");
        assert_eq!(result["workspace_id"], 1);
        assert_eq!(result["status"], "attested");

        let conn = store.conn.lock().unwrap();
        let status: String = conn
            .query_row("SELECT status FROM tasks WHERE id='LEGACY'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(status, "open", "attestation must not change task status");
        let bound_workspace: i64 = conn
            .query_row(
                "SELECT workspace_id FROM task_workspace_bindings WHERE task_id='LEGACY'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(bound_workspace, 1);
        let audit_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events \
                 WHERE task_id='LEGACY' AND reason_code='legacy_workspace_binding_attested'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(audit_count, 1);
        let ledger_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_operation_ledger \
                 WHERE method='task.attest_legacy_workspace_binding'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(ledger_count, 1);
    }

    #[test]
    fn test_legacy_workspace_attestation_replays_without_duplicate_binding_or_audit() {
        let store = clocked_store();
        setup_legacy_attestation(&store, "LEGACY", "ANCHOR");
        let params = legacy_attest_params("LEGACY", "ANCHOR");
        let first = store
            .handle_task_attest_legacy_workspace_binding(peer(), &params)
            .unwrap();
        let replay = store
            .handle_task_attest_legacy_workspace_binding(peer(), &params)
            .unwrap();
        assert_eq!(first["workspace_binding_id"], replay["workspace_binding_id"]);
        let conn = store.conn.lock().unwrap();
        let binding_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_workspace_bindings WHERE task_id='LEGACY'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let audit_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events \
                 WHERE task_id='LEGACY' AND reason_code='legacy_workspace_binding_attested'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(binding_count, 1);
        assert_eq!(audit_count, 1);
    }

    #[test]
    fn test_legacy_workspace_attestation_rejects_already_bound_wrong_role_and_stale_fencing() {
        let store = clocked_store();
        setup_legacy_attestation(&store, "LEGACY", "ANCHOR");
        let params = legacy_attest_params("LEGACY", "ANCHOR");
        store
            .handle_task_attest_legacy_workspace_binding(peer(), &params)
            .unwrap();
        let mut duplicate = legacy_attest_params("LEGACY", "ANCHOR");
        duplicate["request_id"] = json!("req-attest-duplicate");
        let err = store
            .handle_task_attest_legacy_workspace_binding(peer(), &duplicate)
            .unwrap_err();
        assert_eq!(err.code, ERR_LEGACY_BIND_ALREADY_BOUND);

        let store = clocked_store();
        setup_legacy_attestation(&store, "LEGACY", "ANCHOR");
        let mut wrong_role = legacy_attest_params("LEGACY", "ANCHOR");
        wrong_role["identity"]["role"] = json!("executor");
        let err = store
            .handle_task_attest_legacy_workspace_binding(peer(), &wrong_role)
            .unwrap_err();
        assert_eq!(err.code, ERR_LEGACY_BIND_ROLE_REQUIRED);
        let mut fenced = legacy_attest_params("LEGACY", "ANCHOR");
        fenced["request_id"] = json!("req-attest-fenced");
        fenced["fencing_counter"] = json!(0);
        let err = store
            .handle_task_attest_legacy_workspace_binding(peer(), &fenced)
            .unwrap_err();
        assert_eq!(err.code, ERR_LEGACY_BIND_FENCED);
    }

    #[test]
    fn test_legacy_workspace_attestation_rejects_unbound_anchor_missing_evidence_and_instance_mismatch() {
        let store = clocked_store();
        {
            let conn = store.conn.lock().unwrap();
            insert_task(&conn, "LEGACY");
            insert_task(&conn, "ANCHOR");
        }
        register_agent(&store);
        let params = legacy_attest_params("LEGACY", "ANCHOR");
        let err = store
            .handle_task_attest_legacy_workspace_binding(peer(), &params)
            .unwrap_err();
        assert_eq!(err.code, ERR_LEGACY_BIND_ANCHOR_UNBOUND);

        let store = clocked_store();
        setup_legacy_attestation(&store, "LEGACY", "ANCHOR");
        let mut missing_evidence = legacy_attest_params("LEGACY", "ANCHOR");
        missing_evidence.as_object_mut().unwrap().remove("evidence_hash");
        let err = store
            .handle_task_attest_legacy_workspace_binding(peer(), &missing_evidence)
            .unwrap_err();
        assert_eq!(err.code, ERR_LEGACY_BIND_EVIDENCE_REQUIRED);

        let mut instance_mismatch = legacy_attest_params("LEGACY", "ANCHOR");
        instance_mismatch["request_id"] = json!("req-attest-instance-mismatch");
        instance_mismatch["workspace_instance_id"] = json!("wrong-instance");
        let err = store
            .handle_task_attest_legacy_workspace_binding(peer(), &instance_mismatch)
            .unwrap_err();
        assert_eq!(err.code, "E_WORKSPACE_AUTHORITY_MISMATCH");
        let conn = store.conn.lock().unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_workspace_bindings WHERE task_id='LEGACY'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 0, "negative cases must not bind legacy task");
    }

    #[test]
    fn test_creates_cycle_detects_self_loop_via_chain() {
        let store = clocked_store();
        let conn = store.conn.lock().unwrap();
        // A -> B -> C（新方向链）；schema 由 from_connection 官方迁移建齐
        conn.execute(
            "INSERT INTO task_supersede_relations (superseded_task_id, superseding_task_id, actor, created_at) \
             VALUES ('A','B','t',1.0), ('B','C','t',2.0)",
            [],
        )
        .unwrap();
        // 新增 C -> A 会成环（A 沿 B->C 回到 A）
        assert!(creates_cycle(&conn, "C", "A").unwrap());
        // 新增 A -> X 不成环
        assert!(!creates_cycle(&conn, "A", "X").unwrap());
    }

    #[test]
    fn test_creates_cycle_multi_outgoing_edge() {
        let store = clocked_store();
        let conn = store.conn.lock().unwrap();
        // 复合主键允许 A 有多条出边：A->B、A->C 并存
        conn.execute(
            "INSERT INTO task_supersede_relations (superseded_task_id, superseding_task_id, actor, created_at) \
             VALUES ('A','B','t',1.0), ('A','C','t',2.0)",
            [],
        )
        .unwrap();
        // 新增 C->A：实际构成 C->A->C 环，必须检测到（旧 LIMIT 1 实现漏检）
        assert!(creates_cycle(&conn, "C", "A").unwrap());
        // 新增 B->X 不成环
        assert!(!creates_cycle(&conn, "B", "X").unwrap());
        // 新增 C->D 不成环（C 出边回到 D 无环）
        assert!(!creates_cycle(&conn, "C", "D").unwrap());
    }

    #[test]
    fn test_supersede_round_trip_status_unchanged_and_provenance() {
        let store = clocked_store();
        setup_pair(&store, "OLD", "NEW");
        register_agent(&store);

        let params = base_params("OLD", "NEW");
        let res = store.handle_task_supersede(peer(), &params).unwrap();
        assert_eq!(res["superseded_task_id"], "OLD");
        assert_eq!(res["superseding_task_id"], "NEW");
        assert_eq!(res["workspace_id"], 1);
        assert!(!res["supersedence_id"].as_str().unwrap().is_empty());
        assert_eq!(res["request_id"], params["request_id"]);
        assert_eq!(res["fencing_counter"], 1);
        assert_eq!(res["evidence_hash"], "sha256:ev-1");

        // 被替代任务 status 不变（append-only 语义）
        let conn = store.conn.lock().unwrap();
        let status: String = conn
            .query_row("SELECT status FROM tasks WHERE id='OLD'", [], |r| {
                r.get::<usize, String>(0)
            })
            .unwrap();
        assert_eq!(status, "open");
        // relation + event + task_events 审计 + ledger 各一行
        let n_rel: i64 = conn
            .query_row("SELECT COUNT(*) FROM task_supersede_relations", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n_rel, 1);
        let n_ev: i64 = conn
            .query_row("SELECT COUNT(*) FROM task_supersede_events", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n_ev, 1);
        let n_audit: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events WHERE task_id='OLD' AND reason_code='task_superseded'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n_audit, 1);
        let n_ledger: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_operation_ledger WHERE method='task.supersede'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n_ledger, 1);
        drop(conn);

        // 只读查询 superseded_by（workspace-scoped projection 含 provenance）
        let q = json!({ "task_id": "OLD", "workspace_id": 1 });
        let got = store.handle_task_superseded_by(peer(), &q).unwrap();
        assert_eq!(got["found"], true);
        assert_eq!(got["superseding_task_id"], "NEW");
        assert_eq!(got["workspace_id"], 1);
        assert_eq!(got["supersedence_id"], res["supersedence_id"]);
    }

    #[test]
    fn test_supersede_replay_same_request_id_no_new_rows() {
        let store = clocked_store();
        setup_pair(&store, "OLD", "NEW");
        register_agent(&store);
        let params = base_params("OLD", "NEW");

        let first = store.handle_task_supersede(peer(), &params).unwrap();
        // 同 request_id 同 canonical 参数 → 只读重放：关键字段一致；
        // authoritative_timestamp 为 f64，JSON 最短表示序列化往返允许末位精度差。
        let replay = store.handle_task_supersede(peer(), &params).unwrap();
        assert_eq!(first["superseded_task_id"], replay["superseded_task_id"]);
        assert_eq!(first["superseding_task_id"], replay["superseding_task_id"]);
        assert_eq!(first["status"], replay["status"]);
        assert_eq!(first["supersedence_id"], replay["supersedence_id"]);
        assert_eq!(first["request_id"], replay["request_id"]);
        let dt = (first["authoritative_timestamp"].as_f64().unwrap()
            - replay["authoritative_timestamp"].as_f64().unwrap())
        .abs();
        assert!(dt < 1e-6, "重放时间戳应一致，差={}", dt);
        let conn = store.conn.lock().unwrap();
        let n_rel: i64 = conn
            .query_row("SELECT COUNT(*) FROM task_supersede_relations", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n_rel, 1, "重放不得追加关系行");
        let n_ev: i64 = conn
            .query_row("SELECT COUNT(*) FROM task_supersede_events", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n_ev, 1, "重放不得追加事件行");
        let n_ledger: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_operation_ledger WHERE method='task.supersede'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n_ledger, 1, "重放不得追加 ledger 行");
        drop(conn);
    }

    #[test]
    fn test_supersede_request_id_reuse_mismatch() {
        let store = clocked_store();
        setup_pair(&store, "OLD", "NEW");
        register_agent(&store);
        let mut params = base_params("OLD", "NEW");
        assert!(store.handle_task_supersede(peer(), &params).is_ok());
        // 同 request_id 但 superseding 参数不同 → E_REQUEST_ID_REUSE_MISMATCH
        params["superseding_id"] = json!("OTHER-NEW");
        let err = store.handle_task_supersede(peer(), &params).unwrap_err();
        assert_eq!(err.code, "E_REQUEST_ID_REUSE_MISMATCH");
    }

    #[test]
    fn test_supersede_deterministic_rejection_durable_and_replayable() {
        let store = clocked_store();
        // 未注册身份
        setup_pair(&store, "OLD", "NEW");
        let params = base_params("OLD", "NEW");
        let err = store.handle_task_supersede(peer(), &params).unwrap_err();
        assert_eq!(err.code, "E_IDENTITY_UNREGISTERED");
        // 确定性拒绝写可重放 ledger error（不写 relation/task_event）
        let conn = store.conn.lock().unwrap();
        let n_rel: i64 = conn
            .query_row("SELECT COUNT(*) FROM task_supersede_relations", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n_rel, 0);
        let n_audit: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events WHERE task_id='OLD'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n_audit, 0);
        let n_ledger: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_operation_ledger WHERE method='task.supersede'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n_ledger, 1, "确定性拒绝必须写可重放 ledger error");
        drop(conn);
        // 同 request_id 重试 → 重放同一拒绝错误
        let retry = store.handle_task_supersede(peer(), &params).unwrap_err();
        assert_eq!(retry.code, "E_IDENTITY_UNREGISTERED");
    }

    #[test]
    fn test_supersede_rejects_missing_identity_role_lease_evidence() {
        let store = clocked_store();
        setup_pair(&store, "OLD", "NEW");
        register_agent(&store);

        // 缺 identity
        let mut p = base_params("OLD", "NEW");
        p.as_object_mut().unwrap().remove("identity");
        let err = store.handle_task_supersede(peer(), &p).unwrap_err();
        assert_eq!(err.code, ERR_SUPERSEDE_IDENTITY_REQUIRED);

        // 角色非 adjudicator
        let mut p = base_params("OLD", "NEW");
        p["identity"]["role"] = json!("implementer");
        let err = store.handle_task_supersede(peer(), &p).unwrap_err();
        assert_eq!(err.code, ERR_SUPERSEDE_ROLE_REQUIRED);

        // 缺 lease 凭证
        let mut p = base_params("OLD", "NEW");
        p.as_object_mut().unwrap().remove("lease_token");
        let err = store.handle_task_supersede(peer(), &p).unwrap_err();
        assert_eq!(err.code, ERR_SUPERSEDE_LEASE_REQUIRED);

        // fencing counter 陈旧（当前 1，传 0）
        let mut p = base_params("OLD", "NEW");
        p["fencing_counter"] = json!(0);
        let err = store.handle_task_supersede(peer(), &p).unwrap_err();
        assert_eq!(err.code, ERR_SUPERSEDE_FENCED);

        // 缺证据
        let mut p = base_params("OLD", "NEW");
        p.as_object_mut().unwrap().remove("evidence_path");
        let err = store.handle_task_supersede(peer(), &p).unwrap_err();
        assert_eq!(err.code, ERR_SUPERSEDE_EVIDENCE_REQUIRED);
    }

    #[test]
    fn test_supersede_rejects_self_duplicate_missing_cross_workspace() {
        let store = clocked_store();
        setup_pair(&store, "OLD", "NEW");
        register_agent(&store);

        // 自替代
        let mut p = base_params("OLD", "OLD");
        let err = store.handle_task_supersede(peer(), &p).unwrap_err();
        assert_eq!(err.code, ERR_SUPERSEDE_SELF_REFERENCE);

        // 引用不存在的任务
        let mut p = base_params("OLD", "NOPE");
        let err = store.handle_task_supersede(peer(), &p).unwrap_err();
        assert_eq!(err.code, ERR_SUPERSEDE_TASK_NOT_FOUND);

        // 正常成功后重复
        let ok = base_params("OLD", "NEW");
        assert!(store.handle_task_supersede(peer(), &ok).is_ok());
        let mut p = base_params("OLD", "NEW");
        p["request_id"] = json!("req-sup-dup");
        let err = store.handle_task_supersede(peer(), &p).unwrap_err();
        assert_eq!(err.code, ERR_SUPERSEDE_ALREADY_EXISTS);

        // 跨 workspace：NEW2 绑定到 workspace 2（predecessor 在 1）。
        // 不传 workspace_id（raw daemon 调用形态），使 task_bound_workspace_id
        // 不做 requested 比对，直接暴露 ws_pre(1) != ws_suc(2) → CROSS_WORKSPACE。
        let conn = store.conn.lock().unwrap();
        insert_task(&conn, "NEW2");
        drop(conn);
        let ts = 1_700_000_000.0_f64;
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) \
                 VALUES (2, 'ws2', '/tmp/ws2', ?1, 0)",
                params![ts],
            )
            .unwrap();
            conn.execute(
                "INSERT OR IGNORE INTO workspace_authority_captures
                 (workspace_capture_id, workspace_id, capture_revision, supersedes_capture_id,
                  daemon_workspace_id, workspace_instance_id, capture_canonicalization_version,
                  capture_canonicalization_rules_hash, registry_identity_payload_json,
                  registry_identity_hash, workspace_manifest_payload_json, workspace_manifest_hash,
                  client_view_root_hash, host_real_root_hash, created_by, authoritative_created_at)
                 VALUES ('cap-sup-NEW2', 2, 1, NULL, 0, 'ws-inst-sup-NEW2', 'workspace-capture-c14n/v1',
                         'test-rules-hash', '{}', 'test-identity-hash-NEW2', '{}', 'test-manifest-hash',
                         'test-root-hash', 'test-root-hash', 'test', ?1)",
                params![ts],
            )
            .unwrap();
            conn.execute(
                "INSERT OR IGNORE INTO task_workspace_bindings
                 (task_id, workspace_id, workspace_binding_id, workspace_capture_id, created_by, authoritative_created_at)
                 VALUES ('NEW2', 2, 'tb-sup-NEW2', 'cap-sup-NEW2', 'test', ?1)",
                params![ts],
            )
            .unwrap();
        }
        let mut p = base_params("OLD", "NEW2");
        p.as_object_mut().unwrap().remove("workspace_id");
        let err = store.handle_task_supersede(peer(), &p).unwrap_err();
        assert_eq!(err.code, ERR_SUPERSEDE_CROSS_WORKSPACE);
    }

    #[test]
    fn test_supersede_rejects_cycle_and_multi_edge() {
        let store = clocked_store();
        // A/B/C 各建任务 + workspace binding + reviewer lease（幂等）
        for id in ["A", "B", "C"] {
            {
                let conn = store.conn.lock().unwrap();
                insert_task(&conn, id);
            }
            seed_task_binding(&store, id);
            seed_reviewer_lease(&store, id);
        }
        register_agent(&store);
        let mut ab = base_params("A", "B");
        ab["request_id"] = json!("req-ab");
        assert!(store.handle_task_supersede(peer(), &ab).is_ok());
        let mut ac = base_params("A", "C");
        ac["request_id"] = json!("req-ac");
        assert!(store.handle_task_supersede(peer(), &ac).is_ok());
        // C -> A 构成 C->A->C 环，必须拒绝（多出边场景）
        let mut ca = base_params("C", "A");
        ca["request_id"] = json!("req-ca");
        let err = store.handle_task_supersede(peer(), &ca).unwrap_err();
        assert_eq!(err.code, ERR_SUPERSEDE_CYCLE);
        // B -> A 也构成环
        let mut ba = base_params("B", "A");
        ba["request_id"] = json!("req-ba");
        let err = store.handle_task_supersede(peer(), &ba).unwrap_err();
        assert_eq!(err.code, ERR_SUPERSEDE_CYCLE);
    }
}
