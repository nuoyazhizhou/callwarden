//! 1A：原生 `task.create` 领域模块（cw-role-handoff-task-loop.md §8.1.1）。
//!
//! 只实现 `task.create` 在同一 task-DB transaction 写入不可变 `task_workspace_bindings`
//! 与 append-only `workspace_authority_captures`；不编辑 Role Contract、step binding、
//! operation store 或 CapabilityMutationGate（所有权边界见 `mod.rs`）。
//!
//! 语义（§8.1.1 / §4.3）：
//! - 每个 `task.create` 必须在同一事务写入 `task_workspace_bindings`，其
//!   `(workspace_capture_id, workspace_id)` 指向本次已验证追加的 capture。
//! - capture 的稳定 identity 使用冻结 `workspace-capture-c14n/v1`：canonical payload
//!   只含 `workspace_instance_id` / `client_view_root_hash` / `host_real_root_hash` /
//!   `workspace_manifest_hash`（排除 snapshot_id、last_active_at 等 registry 字段）。
//! - 任一 registry/capture/local workspace 不一致一律 `E_WORKSPACE_AUTHORITY_MISMATCH`；
//!   不用 active workspace / active_task_id / cwd / 客户端 numeric id 补齐。
//!
//! 事务/savepoint/ledger 语义由 foundation 独占的 `TaskMutationExecutor` wrapper 统一
//! 落实（§3.3/§4.3）：本模块只提供 `apply_create` 领域回调（写 capture/task/binding），
//! 以 `create_task` 为入口构造 `StrictParsedEnvelope` 并委托 wrapper 执行 dedupe →
//! savepoint → 分派 → ledger。cutover（1D3A/1D3B）前 route 仍 fail-closed，本入口供
//! 领域测试与 1A 验收复用。

use rusqlite::Connection;
use rusqlite::OptionalExtension;
use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;

use crate::daemon::dispatch::DaemonRpcError;
use super::executor::TaskMutationExecutor;
use super::types::{
    DomainOutcome, FrozenAuthorityInput, InfrastructureError, InvocationClass,
    StableDomainError, StrictParsedEnvelope, TaskDomainTx,
};

/// 任一 registry/capture/local workspace 不一致（§8.1.1 fail-closed）。
pub const ERR_WORKSPACE_AUTHORITY_MISMATCH: &str = "E_WORKSPACE_AUTHORITY_MISMATCH";
/// 事务/savepoint/ledger 基础设施失败（InfrastructureError 语义，回滚 outer tx）。
pub const ERR_TASK_DB_TRANSACTION: &str = "E_TASK_DB_TRANSACTION";

/// workspace capture c14n 的冻结 version（§8.1.1）。
const WORKSPACE_CAPTURE_C14N_VERSION: &str = "workspace-capture-c14n/v1";

/// `task.create` 的 ledger dedup key（固定 (workspace_instance_id, method, request_id)）。
pub struct LedgerKey {
    pub workspace_instance_id: String,
    pub method: String,
    pub request_id: String,
}

/// `task.create` 的领域参数（经严格 envelope.params 校验后传入；不含 key 内字段）。
#[derive(Debug)]
pub struct CreateTaskInput {
    pub task_id: String,
    pub title: String,
    pub description: String,
    pub creator: String,
}

impl CreateTaskInput {
    /// 从 `StrictParsedEnvelope.params` 严格解析领域输入（1D3A cutover 由
    /// `route.rs` 内部 validation 路由调用）。`task_id`/`title`/`description`/
    /// `creator` 任一缺失或非字符串即 `invalid_params`，不进入 executor。
    pub fn from_params(params: &serde_json::Value) -> Result<Self, DaemonRpcError> {
        let get = |key: &str| -> Result<String, DaemonRpcError> {
            params
                .get(key)
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
                .ok_or_else(|| {
                    DaemonRpcError::invalid_params(format!("task.create 缺少字段: {key}"))
                })
        };
        Ok(CreateTaskInput {
            task_id: get("task_id")?,
            title: get("title")?,
            description: get("description")?,
            creator: get("creator")?,
        })
    }
}

/// 受保护写入前 daemon 校验 workspace 后提供的闭环身份输入。registry 的
/// `daemon_workspace_id` 仅作诊断 provenance，不参与稳定 identity 判定（§8.1.1）。
pub struct WorkspaceCaptureInput {
    /// task-DB 的 `workspaces.id`（INTEGER），不是 registry 同名整数，也不是
    /// 字符串 `workspace_instance_id`。
    pub workspace_id: i64,
    /// registry 自增 id，仅诊断 provenance，不参与相等判定。
    pub daemon_workspace_id: i64,
    pub workspace_instance_id: String,
    pub client_view_root_hash: String,
    pub host_real_root_hash: String,
    pub workspace_manifest_payload_json: String,
    pub workspace_manifest_hash: String,
    pub created_by: String,
}

impl WorkspaceCaptureInput {
    /// `workspace-capture-c14n/v1` 稳定 identity hash（§8.1.1）。
    pub fn registry_identity_hash(&self) -> String {
        registry_identity_hash(
            &self.workspace_instance_id,
            &self.client_view_root_hash,
            &self.host_real_root_hash,
            &self.workspace_manifest_hash,
        )
    }

    /// 从 `StrictParsedEnvelope.params` 严格解析 capture 输入（1D3B 公共分派使用）。
    ///
    /// registry 数据管道尚未接入 Rust daemon：`client_view_root_hash` 等字段暂由
    /// 请求携带（registry 管道落地后应由 daemon 从 registry 填充并移除客户端提交
    /// 字段）。任一必需字段缺失/类型错误即 `invalid_params`，不进入 executor。
    pub fn from_params(params: &serde_json::Value) -> Result<Self, DaemonRpcError> {
        let get_str = |key: &str| -> Result<String, DaemonRpcError> {
            params
                .get(key)
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
                .ok_or_else(|| DaemonRpcError::invalid_params(format!("task.create 缺少字段: {key}")))
        };
        let get_i64 = |key: &str| -> Result<i64, DaemonRpcError> {
            params.get(key).and_then(|v| v.as_i64()).ok_or_else(|| {
                DaemonRpcError::invalid_params(format!("task.create 缺少字段: {key}"))
            })
        };
        Ok(WorkspaceCaptureInput {
            workspace_id: get_i64("workspace_id")?,
            daemon_workspace_id: get_i64("daemon_workspace_id")?,
            workspace_instance_id: get_str("workspace_instance_id")?,
            client_view_root_hash: get_str("client_view_root_hash")?,
            host_real_root_hash: get_str("host_real_root_hash")?,
            workspace_manifest_payload_json: get_str("workspace_manifest_payload_json")?,
            workspace_manifest_hash: get_str("workspace_manifest_hash")?,
            created_by: get_str("created_by")?,
        })
    }
}

/// `workspace-capture-c14n/v1`：对四元组稳定的 identity payload 做 UTF-8 / Unicode NFC、
/// 键按 code point 排序、无多余空白的 canonical JSON，取 SHA-256 表示为 `sha256:<hex>`
/// （§8.1.1）。
pub fn registry_identity_hash(
    workspace_instance_id: &str,
    client_view_root_hash: &str,
    host_real_root_hash: &str,
    workspace_manifest_hash: &str,
) -> String {
    let payload = serde_json::json!({
        "workspace_instance_id": workspace_instance_id,
        "client_view_root_hash": client_view_root_hash,
        "host_real_root_hash": host_real_root_hash,
        "workspace_manifest_hash": workspace_manifest_hash,
    });
    let canonical = c14n_value(&payload);
    let bytes = serde_json::to_vec(&canonical).unwrap_or_default();
    let digest = Sha256::digest(&bytes);
    format!("sha256:{}", hex::encode(digest))
}

/// 读取 `workspace_capture` rule row 的 rules_hash，用于 capture 的 c14n provenance。
/// 缺 row 或不可读一律 fail closed（§8.1.3，capability 未就绪）。
fn workspace_capture_rules_hash(conn: &Connection) -> Result<String, DaemonRpcError> {
    conn.query_row(
        "SELECT rules_hash FROM canonicalization_rule_sets \
         WHERE domain = 'workspace_capture' AND canonicalization_version = ?1",
        [WORKSPACE_CAPTURE_C14N_VERSION],
        |row| row.get::<_, String>(0),
    )
    .map_err(|e| {
        DaemonRpcError::new(
            ERR_WORKSPACE_AUTHORITY_MISMATCH,
            format!("workspace_capture rule row 不可用（capability 未就绪）: {e}"),
        )
    })
}

/// 领域写入成功后的响应（wrapper 统一落 ledger result，故无需再携带 provenance 字段）。
struct DomainWriteOk {
    response: serde_json::Value,
}

/// 领域执行期间的失败类别（封闭、类型化）。
enum CreateDomainError {
    /// 确定性、可重放失败：外层在 savepoint 回滚后写可重放 ledger error 并 commit。
    Deterministic { code: String, message: String },
    /// 基础设施失败：回滚 outer transaction、领域写入与 ledger result。
    Infrastructure(DaemonRpcError),
}

impl CreateDomainError {
    fn mismatch(message: String) -> Self {
        CreateDomainError::Deterministic {
            code: ERR_WORKSPACE_AUTHORITY_MISMATCH.to_string(),
            message,
        }
    }
    fn infra(e: DaemonRpcError) -> Self {
        CreateDomainError::Infrastructure(e)
    }
    fn infra_msg(e: rusqlite::Error, context: &str) -> Self {
        CreateDomainError::Infrastructure(DaemonRpcError::new(
            ERR_TASK_DB_TRANSACTION,
            format!("{context}: {e}"),
        ))
    }
    /// 把本地失败类别映射为 wrapper 要求的封闭 `DomainOutcome`（§3.3）。
    fn into_outcome(self) -> DomainOutcome {
        match self {
            CreateDomainError::Deterministic { code, message: _ } => {
                DomainOutcome::CommitDeterministicError {
                    stable_error: StableDomainError::DeterministicReject { code },
                }
            }
            CreateDomainError::Infrastructure(error) => {
                DomainOutcome::RollbackInfrastructureError {
                    infrastructure_error: InfrastructureError::Internal {
                        detail: error.message,
                    },
                }
            }
        }
    }
}

/// 以 `task.create` 领域入口构造 `StrictParsedEnvelope`，委托统一的
/// `TaskMutationExecutor` wrapper 执行 dedupe → savepoint → 分派 → ledger（§3.3/§4.3）。
///
/// 领域语义（§8.1.1）由 `apply_create` 承担：同 workspace/instance/identity 才允许追加
/// re-attestation capture；identity 改变 → 确定性错误（wrapper 回滚回调局部写入后写可重放
/// ledger error 并 commit）；任一 infra 失败 → wrapper 回滚整个 outer transaction。
pub fn create_task(
    conn: &mut Connection,
    frozen: &FrozenAuthorityInput,
    ledger_key: &LedgerKey,
    input: &CreateTaskInput,
    ws: &WorkspaceCaptureInput,
) -> Result<serde_json::Value, DaemonRpcError> {
    let envelope = StrictParsedEnvelope {
        workspace_instance_id: ledger_key.workspace_instance_id.clone(),
        canonical_method: ledger_key.method.clone(),
        request_id: ledger_key.request_id.clone(),
        params: serde_json::json!({
            "task_id": input.task_id,
            "title": input.title,
            "description": input.description,
            "creator": input.creator,
        }),
        invocation_class: InvocationClass::ExternalTransport,
    };
    TaskMutationExecutor::default().run(conn, &envelope, frozen, |domain_tx, frozen_ref| {
        apply_create(domain_tx, frozen_ref, input, ws)
    })
}

/// `task.create` 领域回调：在受保护事务内完成 capture 校验/追加 + task + binding 写入，
/// 返回封闭 `DomainOutcome` 交由 wrapper 分派（§3.3）。
fn apply_create(
    tx: &mut TaskDomainTx<'_>,
    _frozen: &FrozenAuthorityInput,
    input: &CreateTaskInput,
    ws: &WorkspaceCaptureInput,
) -> DomainOutcome {
    match write_domain(tx.tx(), input, ws) {
        Ok(ok) => DomainOutcome::CommitSuccess { response: ok.response },
        Err(err) => err.into_outcome(),
    }
}

/// 在 savepoint 事务内执行 capture 校验/追加 + task + binding 写入（§8.1.1）。
fn write_domain(
    tx: &Connection,
    input: &CreateTaskInput,
    ws: &WorkspaceCaptureInput,
) -> Result<DomainWriteOk, CreateDomainError> {
    // 0. workspace 必须真实存在（task-DB 侧 `workspaces.id`；不用客户端 numeric id 补齐）。
    let ws_exists: i64 = tx
        .query_row(
            "SELECT COUNT(*) FROM workspaces WHERE id = ?1",
            [ws.workspace_id],
            |row| row.get(0),
        )
        .map_err(|e| CreateDomainError::infra_msg(e, "workspace 存在性校验失败"))?;
    if ws_exists == 0 {
        return Err(CreateDomainError::mismatch(format!(
            "task-DB 中不存在 workspace_id={}（不得用客户端 numeric id 补齐）",
            ws.workspace_id
        )));
    }

    let capture_rules_hash = workspace_capture_rules_hash(tx)
        .map_err(CreateDomainError::infra)?;
    let identity_hash = ws.registry_identity_hash();

    // 1. 校验既有 capture 链：同 workspace/instance/identity 才允许追加，否则 mismatch。
    let latest: Option<CaptureRow> = tx
        .query_row(
            "SELECT workspace_capture_id, capture_revision, registry_identity_hash \
             FROM workspace_authority_captures \
             WHERE workspace_id = ?1 AND workspace_instance_id = ?2 \
             ORDER BY capture_revision DESC LIMIT 1",
            rusqlite::params![ws.workspace_id, ws.workspace_instance_id],
            |row| {
                Ok(CaptureRow {
                    workspace_capture_id: row.get(0)?,
                    capture_revision: row.get(1)?,
                    registry_identity_hash: row.get(2)?,
                })
            },
        )
        .optional()
        .map_err(|e| CreateDomainError::infra_msg(e, "capture 链读取失败"))?;

    let (capture_id, revision, supersedes) = match &latest {
        Some(prev) => {
            if prev.registry_identity_hash != identity_hash {
                return Err(CreateDomainError::mismatch(format!(
                    "workspace 稳定 identity 改变：既有 registry_identity_hash={} 与当前={} 不一致；\
                     旧 task 必须 UNVERIFIED，不得 UPDATE 原 binding",
                    prev.registry_identity_hash, identity_hash
                )));
            }
            (
                format!("wc-{}-{}", ws.workspace_instance_id, naming_rand()),
                prev.capture_revision + 1,
                Some(prev.workspace_capture_id.clone()),
            )
        }
        None => (
            format!("wc-{}-{}", ws.workspace_instance_id, naming_rand()),
            1,
            None,
        ),
    };

    // 2. 追加 workspace_authority_captures（append-only；每个受保护 create 一条）。
    tx.execute(
        "INSERT INTO workspace_authority_captures \
         (workspace_capture_id, workspace_id, capture_revision, supersedes_capture_id, \
          daemon_workspace_id, workspace_instance_id, capture_canonicalization_version, \
          capture_canonicalization_rules_hash, registry_identity_payload_json, \
          registry_identity_hash, workspace_manifest_payload_json, workspace_manifest_hash, \
          client_view_root_hash, host_real_root_hash, created_by, authoritative_created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16)",
        rusqlite::params![
            capture_id,
            ws.workspace_id,
            revision,
            supersedes,
            ws.daemon_workspace_id,
            ws.workspace_instance_id,
            WORKSPACE_CAPTURE_C14N_VERSION,
            capture_rules_hash,
            canonical_registry_payload(
                &ws.workspace_instance_id,
                &ws.client_view_root_hash,
                &ws.host_real_root_hash,
                &ws.workspace_manifest_hash,
            ),
            identity_hash,
            ws.workspace_manifest_payload_json,
            ws.workspace_manifest_hash,
            ws.client_view_root_hash,
            ws.host_real_root_hash,
            ws.created_by,
            literal_now(),
        ],
    )
    .map_err(|e| CreateDomainError::infra_msg(e, "追加 workspace_authority_captures 失败"))?;

    // 3. 写 task 行（同一事务）。
    let now_real = now_unix();
    tx.execute(
        "INSERT INTO tasks \
         (id, title, description, creator, status, created_at, updated_at) \
         VALUES (?1, ?2, ?3, ?4, 'open', ?5, ?5)",
        rusqlite::params![
            input.task_id,
            input.title,
            input.description,
            input.creator,
            now_real,
        ],
    )
    .map_err(|e| CreateDomainError::infra_msg(e, "写入 tasks 失败"))?;

    // 4. 不可变 task→workspace binding（同一事务；引用刚追加的 capture）。
    let binding_id = format!("tb-{}-{}", input.task_id, ws.workspace_instance_id);
    tx.execute(
        "INSERT INTO task_workspace_bindings \
         (task_id, workspace_id, workspace_binding_id, workspace_capture_id, \
          created_by, authoritative_created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        rusqlite::params![
            input.task_id,
            ws.workspace_id,
            binding_id,
            capture_id,
            ws.created_by,
            literal_now(),
        ],
    )
    .map_err(|e| CreateDomainError::infra_msg(e, "写入 task_workspace_bindings 失败"))?;

    Ok(DomainWriteOk {
        response: serde_json::json!({
            "ok": true,
            "task_id": input.task_id,
            "workspace_id": ws.workspace_id,
            "workspace_instance_id": ws.workspace_instance_id,
            "workspace_binding_id": binding_id,
            "workspace_capture_id": capture_id,
        }),
    })
}

/// capture 链查询行。
struct CaptureRow {
    workspace_capture_id: String,
    capture_revision: i64,
    registry_identity_hash: String,
}

/// `workspace-capture-c14n/v1` 持久化的 registry identity canonical payload（§8.1.1）。
fn canonical_registry_payload(
    workspace_instance_id: &str,
    client_view_root_hash: &str,
    host_real_root_hash: &str,
    workspace_manifest_hash: &str,
) -> String {
    serde_json::json!({
        "workspace_instance_id": workspace_instance_id,
        "client_view_root_hash": client_view_root_hash,
        "host_real_root_hash": host_real_root_hash,
        "workspace_manifest_hash": workspace_manifest_hash,
    })
    .to_string()
}

/// 一次性后缀，避免多 create 并发生成重复 capture/binding id。
fn naming_rand() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("{:x}", nanos)
}

/// capture/binding 的 `authoritative_created_at` 文本。真实接入由 Authoritative_Clock 产生；
/// 此处以权威 UTC 秒值（微秒精度）表达，避免格式漂移。
fn literal_now() -> String {
    format!("{}", now_unix())
}

/// Unix 时间戳秒（float，兼容 SQLite REAL created_at）。
fn now_unix() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 递归 canonical 化 Value：字符串 NFC、对象键 NFC + 按 code point 排序。
fn c14n_value(value: &serde_json::Value) -> serde_json::Value {
    match value {
        serde_json::Value::Object(map) => {
            let mut sorted: std::collections::BTreeMap<String, serde_json::Value> =
                std::collections::BTreeMap::new();
            for (k, v) in map {
                let key = k.nfc().collect::<String>();
                sorted.insert(key, c14n_value(v));
            }
            let mut out = serde_json::Map::new();
            for (k, v) in sorted {
                out.insert(k, v);
            }
            serde_json::Value::Object(out)
        }
        serde_json::Value::Array(arr) => {
            serde_json::Value::Array(arr.iter().map(c14n_value).collect())
        }
        serde_json::Value::String(s) => serde_json::Value::String(s.nfc().collect::<String>()),
        other => other.clone(),
    }
}