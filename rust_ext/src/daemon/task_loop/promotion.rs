//! 1D3B：Public capability promotion（cw-role-handoff-task-loop.md §4.3 / §8.1.5）。
//!
//! foundation 独占。`task_loop.public_promote` 是 daemon control-plane 的 Protected
//! Mutation，不是普通 client/MCP capability；本模块实现它的权威账本写入与内存
//! permit 安装：
//!
//! 语义（§4.3）：
//! - 单一串行化点：`CapabilityMutationGate.acquire(workspace_id)`，锁序
//!   `gate → task-DB transaction`；
//! - 幂等：同一 (workspace_id, request_id) + 同 canonical params hash 只读重放
//!   既有持久化结果；不同 hash 返回 `E_REQUEST_ID_REUSE_MISMATCH`，绝不覆盖；
//! - 首次请求：确定性拒绝（校验不通过）也追加可重放审计结果；
//!   基础设施失败（事务/账本写入）回滚审计写入且不安装 permit；
//! - 成功必须先在 task-DB commit 完整审计事件，**仅**在 commit 成功后安装内存
//!   `PublicPreflightPermit`；审计 commit 失败绝不安装；
//! - 响应固定区分 `durable_authorization=(authorized|deterministic_error)` 与仅
//!   反映当前 daemon 会话的 `permit_installation=(installed|not_installed)`；
//! - audit event 只表示"已授权 publication"，**不是**可跨重启恢复的 permit；重启
//!   或 fingerprint/authority/evidence 失效必须清除内存 permit（`PublicPermitStore`
//!   为纯内存注册表，daemon 重启即空）。
//!
//! 0A/0B 的完整 Capability Authority store 尚未落地：此处对 authority 做最小凭证
//! 校验（id/revision/fencing/evidence 非空且与请求一致），完整
//! id/revision/fencing/validity/expiry/revoke 复核由后续 authority store 接入。

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, MutexGuard};

use rusqlite::{params, Connection, Transaction};
use sha2::{Digest, Sha256};

use crate::daemon::dispatch::DaemonRpcError;
use super::capability_control::CapabilityMutationGate;
use super::operation_store::{
    canonical_params_hash, params_rules_bound, params_rules_current, ParamsRules,
};
use super::preflight;
use super::types::{
    ERR_CAPABILITY_DISABLED, ERR_REQUEST_ID_REUSE_MISMATCH, FrozenAuthorityInput,
    PublicPreflightPermit,
};

/// promotion 基础设施失败（事务/账本写入）统一错误码。
const ERR_PROMOTION_INFRASTRUCTURE: &str = "E_TASK_DB_TRANSACTION";

/// 内存 `PublicPreflightPermit` 注册表（daemon 会话内权威）。
///
/// 纯内存：daemon 重启即空，permit 不跨重启恢复；`clear*` 供 fingerprint /
/// authority / evidence 失效路径调用。key 为 workspace_instance_id。
#[derive(Debug, Default)]
pub struct PublicPermitStore {
    inner: Mutex<BTreeMap<String, PublicPreflightPermit>>,
}

/// daemon 级 task_loop control-plane 组件（1D3B 接线用）。
///
/// daemon 启动时组装一次，经 `DaemonState.task_loop_control` 共享：
/// - `gate`：与 0A/0B Capability Authority / stage toggle 共享的全局
///   `CapabilityMutationGate`（锁序 `gate → authority-store → task-DB`），
///   保证 promotion 与公共 mutation 的最终复核在同一串行化点；
/// - `store`：会话内 `PublicPreflightPermit` 注册表（纯内存，重启即空）；
/// - `daemon_generation`：daemon 启动时固定的 generation，重启必须变化；
///   promotion 请求的 `internal_permit_daemon_generation` 必须与其一致，
///   任何不一致都拒绝并清除内存 permit。
#[derive(Debug)]
pub struct TaskLoopControlPlane {
    pub gate: Arc<CapabilityMutationGate>,
    pub store: PublicPermitStore,
    pub daemon_generation: u64,
}

impl TaskLoopControlPlane {
    /// 组装 control-plane。`daemon_generation` 由调用方（daemon 启动）提供，
    /// 必须随重启变化（如启动时刻的 unix 纳秒时间戳）。
    pub fn new(gate: Arc<CapabilityMutationGate>, daemon_generation: u64) -> Self {
        Self {
            gate,
            store: PublicPermitStore::new(),
            daemon_generation,
        }
    }
}

impl PublicPermitStore {
    pub fn new() -> Self {
        Self::default()
    }

    /// 读取当前已安装的 permit 副本（None = 未安装/已清除）。
    pub fn get(&self, workspace_instance_id: &str) -> Option<PublicPreflightPermit> {
        self.lock().get(workspace_instance_id).cloned()
    }

    /// 安装（覆盖）permit。仅限审计事件 commit 成功后调用。
    pub fn install(&self, workspace_instance_id: &str, permit: PublicPreflightPermit) {
        self.lock().insert(workspace_instance_id.to_string(), permit);
    }

    /// 清除单个 workspace 的 permit（失效路径）。
    pub fn clear(&self, workspace_instance_id: &str) {
        self.lock().remove(workspace_instance_id);
    }

    /// 清除全部 permit（daemon 重启/generation 变更时）。
    pub fn clear_all(&self) {
        self.lock().clear();
    }

    fn lock(&self) -> MutexGuard<'_, BTreeMap<String, PublicPreflightPermit>> {
        self.inner.lock().expect("public permit store poisoned")
    }
}

/// 严格解析的 promotion 请求输入（key 内字段与校验所需凭证）。
#[derive(Debug)]
struct PromotionRequest {
    workspace_id: i64,
    workspace_instance_id: String,
    request_id: String,
    action_identity: String,
    authority_id: String,
    authority_revision: u64,
    fencing_counter: u64,
    internal_permit_schema_fingerprint: String,
    internal_permit_rules_hash: String,
    internal_permit_daemon_generation: u64,
    evidence_id: String,
    evidence_hash: String,
    runtime_binary_hash: String,
}

impl PromotionRequest {
    /// 从 `task_loop.public_promote` 的 params 严格解析；任一必需字段缺失/类型错误
    /// 即 invalid_params（不进入账本）。
    fn from_params(params: &serde_json::Value) -> Result<Self, DaemonRpcError> {
        let get_str = |key: &str| -> Result<String, DaemonRpcError> {
            params
                .get(key)
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
                .ok_or_else(|| DaemonRpcError::invalid_params(format!("public_promote 缺少字段: {key}")))
        };
        let get_i64 = |key: &str| -> Result<i64, DaemonRpcError> {
            params.get(key).and_then(|v| v.as_i64()).ok_or_else(|| {
                DaemonRpcError::invalid_params(format!("public_promote 缺少字段: {key}"))
            })
        };
        let get_u64 = |key: &str| -> Result<u64, DaemonRpcError> {
            params.get(key).and_then(|v| v.as_u64()).ok_or_else(|| {
                DaemonRpcError::invalid_params(format!("public_promote 缺少字段: {key}"))
            })
        };
        Ok(PromotionRequest {
            workspace_id: get_i64("workspace_id")?,
            workspace_instance_id: get_str("workspace_instance_id")?,
            request_id: get_str("request_id")?,
            action_identity: get_str("action_identity")?,
            authority_id: get_str("authority_id")?,
            authority_revision: get_u64("authority_revision")?,
            fencing_counter: get_u64("fencing_counter")?,
            internal_permit_schema_fingerprint: get_str("internal_permit_schema_fingerprint")?,
            internal_permit_rules_hash: get_str("internal_permit_rules_hash")?,
            internal_permit_daemon_generation: get_u64("internal_permit_daemon_generation")?,
            evidence_id: get_str("evidence_id")?,
            evidence_hash: get_str("evidence_hash")?,
            runtime_binary_hash: get_str("runtime_binary_hash")?,
        })
    }
}

/// dedupe 判定结果（封闭；禁止用错误字符串推断类别）。
enum PromotionDedupe {
    /// 同 key 同 hash：只读重放既有持久化结果（不追加事件、不重装 permit）。
    Replay {
        response_or_error_json: serde_json::Value,
    },
    /// 首次 key：调用方随后执行校验并落账。
    FirstRequest {
        rules: ParamsRules,
        canonical_params_hash: String,
    },
}

/// 确定性拒绝原因（首次请求校验不通过时的可重放类别）。
#[derive(Debug)]
struct DeterministicReject {
    code: &'static str,
    message: String,
}

/// 执行 `task_loop.public_promote` control-plane mutation（§4.3）。
///
/// 输入 `params` 与冻结权威输入均来自 daemon control-plane（非公共客户端路径）。
/// 返回固定结构：
/// `{ ok, workspace_id, request_id, promotion_event_id, durable_authorization,
///    permit_installation }`。
pub fn promote_public_capability(
    conn: &mut Connection,
    gate: &CapabilityMutationGate,
    store: &PublicPermitStore,
    frozen: &FrozenAuthorityInput,
    params: &serde_json::Value,
) -> Result<serde_json::Value, DaemonRpcError> {
    let req = PromotionRequest::from_params(params)?;

    // 1. 单一串行化点：gate → task-DB transaction（§4.3 锁序）。
    let _guard = gate.acquire(&req.workspace_id.to_string())?;

    // 2. 幂等 dedupe（读路径，开事务前）。
    let (rules, canonical_params_hash) = match promotion_dedupe(conn, &req)? {
        PromotionDedupe::Replay { response_or_error_json } => {
            // 重放只返回持久化结果，不重新安装 permit（audit event 不跨重启恢复）。
            let installed = store
                .get(&req.workspace_instance_id)
                .is_some_and(|p| p.request_id == req.request_id);
            return Ok(replay_response(&response_or_error_json, installed));
        }
        PromotionDedupe::FirstRequest {
            rules,
            canonical_params_hash,
        } => (rules, canonical_params_hash),
    };

    // 3. 首次请求：确定性校验。
    let decision = match validate_promotion(conn, frozen, &req) {
        Ok(()) => Decision::Authorized,
        Err(reject) => Decision::DeterministicError(reject),
    };

    // 4. 写权威审计事件（同一事务）→ 仅 commit 成功后安装内存 permit。
    let mut tx = conn
        .transaction()
        .map_err(|e| infra(format!("promotion 事务启动失败: {e}")))?;
    let event_id = promotion_event_id(&req);
    match decision {
        Decision::Authorized => {
            let mut resp = response_json(&req, "authorized", event_id.clone());
            record_event(
                &tx,
                &req,
                &rules,
                &canonical_params_hash,
                &event_id,
                "authorized",
                "",
                "",
                &resp,
            )?;
            tx.commit()
                .map_err(|e| infra(format!("promotion 审计提交失败: {e}")))?;
            // 仅在 commit 成功后安装。
            store.install(&req.workspace_instance_id, build_permit(&req, &event_id));
            resp["permit_installation"] = serde_json::Value::String("installed".into());
            Ok(resp)
        }
        Decision::DeterministicError(reject) => {
            let resp = response_json(&req, "deterministic_error", event_id.clone());
            record_event(
                &tx,
                &req,
                &rules,
                &canonical_params_hash,
                &event_id,
                "deterministic_error",
                reject.code,
                &reject.message,
                &resp,
            )?;
            tx.commit()
                .map_err(|e| infra(format!("promotion 审计提交失败: {e}")))?;
            // 确定性拒绝不安装 permit。
            Ok(resp)
        }
    }
}

/// promotion 账本 dedupe：已提交 key 重放/冲突判定；首次返回 FirstRequest。
fn promotion_dedupe(
    conn: &Connection,
    req: &PromotionRequest,
) -> Result<PromotionDedupe, DaemonRpcError> {
    let bound = conn.query_row(
        "SELECT params_canonicalization_version, params_canonicalization_rules_hash, \
                canonical_params_hash, response_or_error_json \
         FROM task_loop_capability_promotion_events \
         WHERE workspace_id = ?1 AND request_id = ?2",
        params![req.workspace_id, req.request_id],
        |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
            ))
        },
    );

    match bound {
        Ok((version, rules_hash, stored_hash, stored_json)) => {
            let saved_rules = params_rules_bound(conn, &version, &rules_hash)?;
            // 重算使用请求 params（key 内字段由 operation-params-c14n/v1 顶层排除）。
            let request_params = request_params(&req);
            let incoming_hash = canonical_params_hash(&saved_rules, &request_params)?;
            if incoming_hash != stored_hash {
                return Err(DaemonRpcError::new(
                    ERR_REQUEST_ID_REUSE_MISMATCH,
                    format!(
                        "public_promote request_id 复用但 canonical 参数不同 \
                         (workspace_id={} request_id={})",
                        req.workspace_id, req.request_id
                    ),
                ));
            }
            let value = serde_json::from_str(&stored_json).map_err(|e| {
                infra(format!("promotion 保存的结果不可解析: {e}"))
            })?;
            Ok(PromotionDedupe::Replay {
                response_or_error_json: value,
            })
        }
        Err(rusqlite::Error::QueryReturnedNoRows) => {
            let rules = params_rules_current(conn)?;
            let request_params = request_params(&req);
            let canonical_params_hash = canonical_params_hash(&rules, &request_params)?;
            Ok(PromotionDedupe::FirstRequest {
                rules,
                canonical_params_hash,
            })
        }
        Err(e) => Err(infra(format!("promotion 账本读取失败: {e}"))),
    }
}

/// 首次请求的确定性校验：Internal permit fingerprint 与当前 schema/rules/generation
/// 一致，且 authority/evidence/runtime 最小凭证齐全（0A/0B 完整 store 落地后叠加）。
fn validate_promotion(
    conn: &Connection,
    frozen: &FrozenAuthorityInput,
    req: &PromotionRequest,
) -> Result<(), DeterministicReject> {
    // 1) Internal permit fingerprint 必须与当前 schema/rules 一致。
    let schema_fingerprint = preflight::compute_schema_fingerprint(conn)
        .map_err(|e| DeterministicReject { code: ERR_CAPABILITY_DISABLED, message: e.message })?;
    if schema_fingerprint != req.internal_permit_schema_fingerprint {
        return Err(DeterministicReject {
            code: ERR_CAPABILITY_DISABLED,
            message: format!(
                "internal permit schema fingerprint 不匹配 (request={} current={})",
                req.internal_permit_schema_fingerprint, schema_fingerprint
            ),
        });
    }
    let rules_hash = preflight::read_workspace_capture_rules_hash(conn)
        .map_err(|e| DeterministicReject { code: ERR_CAPABILITY_DISABLED, message: e.message })?;
    if rules_hash != req.internal_permit_rules_hash {
        return Err(DeterministicReject {
            code: ERR_CAPABILITY_DISABLED,
            message: format!(
                "internal permit rules hash 不匹配 (request={} current={})",
                req.internal_permit_rules_hash, rules_hash
            ),
        });
    }
    // 2) daemon generation 与冻结输入一致。
    if frozen.daemon_generation != req.internal_permit_daemon_generation {
        return Err(DeterministicReject {
            code: ERR_CAPABILITY_DISABLED,
            message: format!(
                "daemon generation 不匹配 (request={} current={})",
                req.internal_permit_daemon_generation, frozen.daemon_generation
            ),
        });
    }
    // 3) authority / evidence / runtime 最小凭证（完整 store 校验由 0A/0B 接入）。
    if req.authority_id.is_empty()
        || req.authority_revision == 0
        || req.evidence_id.is_empty()
        || req.evidence_hash.is_empty()
        || req.runtime_binary_hash.is_empty()
        || req.action_identity.is_empty()
    {
        return Err(DeterministicReject {
            code: ERR_CAPABILITY_DISABLED,
            message: "public_promote 缺少 authority/evidence/runtime 最小凭证".into(),
        });
    }
    Ok(())
}

/// 在 promotion 事务内追加权威审计事件。约束冲突/写入失败 → 基础设施错误（回滚）。
fn record_event(
    tx: &Transaction<'_>,
    req: &PromotionRequest,
    rules: &ParamsRules,
    canonical_params_hash: &str,
    event_id: &str,
    durable_authorization: &str,
    authorization_code: &str,
    authorization_message: &str,
    response_or_error_json: &serde_json::Value,
) -> Result<(), DaemonRpcError> {
    let durable_json = serde_json::to_string(response_or_error_json)
        .map_err(|e| infra(format!("promotion 结果序列化失败: {e}")))?;
    tx.execute(
        "INSERT INTO task_loop_capability_promotion_events \
         (promotion_event_id, workspace_id, request_id, action_identity, \
          authority_id, authority_revision, fencing_counter, \
          internal_permit_schema_fingerprint, internal_permit_rules_hash, \
          internal_permit_daemon_generation, \
          evidence_id, evidence_hash, schema_fingerprint, rules_hash, \
          runtime_binary_hash, daemon_generation, \
          params_canonicalization_version, params_canonicalization_rules_hash, \
          canonical_params_hash, \
          durable_authorization, authorization_code, authorization_message, \
          response_or_error_json, authoritative_created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, \
                 ?17, ?18, ?19, ?20, ?21, ?22, ?23, \
                 strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
        params![
            event_id,
            req.workspace_id,
            req.request_id,
            req.action_identity,
            req.authority_id,
            req.authority_revision,
            req.fencing_counter,
            req.internal_permit_schema_fingerprint,
            req.internal_permit_rules_hash,
            req.internal_permit_daemon_generation,
            req.evidence_id,
            req.evidence_hash,
            req.internal_permit_schema_fingerprint,
            req.internal_permit_rules_hash,
            req.runtime_binary_hash,
            req.internal_permit_daemon_generation,
            rules.canonicalization_version,
            rules.rules_hash,
            canonical_params_hash,
            durable_authorization,
            authorization_code,
            authorization_message,
            durable_json,
        ],
    )
    .map_err(|e| {
        let is_constraint = e
            .sqlite_error_code()
            .map(|c| c == rusqlite::ErrorCode::ConstraintViolation)
            .unwrap_or(false);
        if is_constraint {
            infra(format!(
                "promotion 事件 key 已被并发写入 (workspace_id={} request_id={})",
                req.workspace_id, req.request_id
            ))
        } else {
            infra(format!("promotion 事件写入失败: {e}"))
        }
    })?;
    Ok(())
}

/// 构造 c14n 请求 payload（key 内字段由 operation-params-c14n/v1 顶层排除）。
fn request_params(req: &PromotionRequest) -> serde_json::Value {
    serde_json::json!({
        "workspace_id": req.workspace_id,
        "workspace_instance_id": req.workspace_instance_id,
        "request_id": req.request_id,
        "action_identity": req.action_identity,
        "authority_id": req.authority_id,
        "authority_revision": req.authority_revision,
        "fencing_counter": req.fencing_counter,
        "internal_permit_schema_fingerprint": req.internal_permit_schema_fingerprint,
        "internal_permit_rules_hash": req.internal_permit_rules_hash,
        "internal_permit_daemon_generation": req.internal_permit_daemon_generation,
        "evidence_id": req.evidence_id,
        "evidence_hash": req.evidence_hash,
        "runtime_binary_hash": req.runtime_binary_hash,
    })
}

/// 确定性事件 id：`promote-<workspace_id>-<request_id 短哈希>`。
/// 同 (workspace_id, request_id) 只可能有一条事件（UNIQUE 约束），id 唯一。
fn promotion_event_id(req: &PromotionRequest) -> String {
    let digest = Sha256::digest(req.request_id.as_bytes());
    format!("promote-{}-{}", req.workspace_id, &hex::encode(digest)[..12])
}

/// 固定结构响应（§4.3：durable_authorization 与 permit_installation 分离）。
fn response_json(
    req: &PromotionRequest,
    durable_authorization: &str,
    event_id: String,
) -> serde_json::Value {
    serde_json::json!({
        "ok": true,
        "workspace_id": req.workspace_id,
        "request_id": req.request_id,
        "promotion_event_id": event_id,
        "durable_authorization": durable_authorization,
        "permit_installation": "not_installed",
    })
}

/// 重放响应：只回显持久化结果与当前会话安装状态（不重新安装 permit）。
fn replay_response(
    response_or_error_json: &serde_json::Value,
    installed: bool,
) -> serde_json::Value {
    let mut resp = response_or_error_json.clone();
    if let Some(obj) = resp.as_object_mut() {
        obj.insert(
            "permit_installation".to_string(),
            serde_json::Value::String(if installed { "installed" } else { "not_installed" }.into()),
        );
    }
    resp
}

/// 从已授权请求构造内存 permit（事件 id 绑定权威账本行）。
fn build_permit(req: &PromotionRequest, event_id: &str) -> PublicPreflightPermit {
    PublicPreflightPermit {
        promotion_event_id: event_id.to_string(),
        workspace_id: req.workspace_instance_id.clone(),
        request_id: req.request_id.clone(),
        daemon_generation: req.internal_permit_daemon_generation,
        authority_id: req.authority_id.clone(),
        authority_revision: req.authority_revision,
        fencing_counter: req.fencing_counter,
        evidence_id: req.evidence_id.clone(),
        evidence_hash: req.evidence_hash.clone(),
        schema_fingerprint: req.internal_permit_schema_fingerprint.clone(),
        rules_hash: req.internal_permit_rules_hash.clone(),
        runtime_binary_hash: req.runtime_binary_hash.clone(),
    }
}

enum Decision {
    Authorized,
    DeterministicError(DeterministicReject),
}

fn infra(detail: impl Into<String>) -> DaemonRpcError {
    DaemonRpcError::new(
        ERR_PROMOTION_INFRASTRUCTURE,
        format!("promotion 基础设施失败: {}", detail.into()),
    )
}
