//! 1B：原生 `task.contract_set` 领域模块（cw-role-handoff-task-loop.md §8.1.2）。
//!
//! 只实现 `role_contract_lineages` / `role_contract_revisions` 的 v1 写入语义：
//! - Role Contract hash 固定 `role-contract-c14n/v1`（§8.1.2）：payload 是 UTF-8、
//!   Unicode NFC、键按 code point 排序、无多余空白的 canonical JSON；SHA-256 表示为
//!   `sha256:<hex>`。纳入字段为 role、skill_id/skill_version、prompt template id/hash、
//!   allowed/forbidden paths、commands、acceptance checks、required evidence、
//!   `handoff_to` 与 independence；lineage/revision id、创建时间、创建者、`is_current`
//!   等派生字段不入 hash。
//! - 路径只能是项目相对、正斜杠、拒绝绝对路径与 `..`；路径集合去重并排序。
//!   command/check/evidence 列表保序且重复即拒绝。
//! - revision 1 的 `supersedes_revision_id` 为 NULL；n>1 必须指向同 lineage 的 n-1，
//!   否则该 lineage 及其 binding 一律 UNVERIFIED（对既有 lineage 追加前校验链连续性）。
//!
//! 事务/savepoint/ledger 语义由 foundation 独占的 `TaskMutationExecutor` wrapper 统一
//! 落实（§3.3/§4.3）：本模块只提供 `apply_set_contract` 领域回调（写 lineage/revision），
//! 以 `set_task_contract` 为入口构造 `StrictParsedEnvelope` 并委托 wrapper。cutover 前
//! route 仍 fail-closed，本入口供领域测试与 1B 验收复用（1C 等下游以 revision 行读取）。

use rusqlite::{Connection, OptionalExtension};
use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;

use crate::daemon::dispatch::DaemonRpcError;
use super::executor::TaskMutationExecutor;
use super::types::{
    DomainOutcome, FrozenAuthorityInput, InfrastructureError, InvocationClass,
    StableDomainError, StrictParsedEnvelope, TaskDomainTx,
};

/// role-contract-c14n/v1 冻结 version（与 `sqlite_query` legacy 回填、rule row 一致）。
pub const ROLE_CONTRACT_C14N_VERSION: &str = "role-contract-c14n/v1";
/// 确定性拒绝：payload 无法 canonicalize（绝对路径/`..`/反斜杠、列表重复、independence
/// 非 object、既有 lineage 断链）→ 可重放 ledger error（§8.1.2 fail closed）。
pub const ERR_TASK_CONTRACT_INVALID: &str = "E_TASK_CONTRACT_INVALID";
/// 确定性拒绝：task 没有不可变 task workspace binding → 无法确定 lineage 归属。
pub const ERR_TASK_BINDING_REQUIRED: &str = "E_TASK_BINDING_REQUIRED";
/// 事务/savepoint/ledger 基础设施失败（InfrastructureError 语义，回滚 outer tx）。
pub const ERR_TASK_DB_TRANSACTION: &str = "E_TASK_DB_TRANSACTION";

/// `task.contract_set` 的 ledger dedup key（固定 (workspace_instance_id, method, request_id)）。
pub struct LedgerKey {
    pub workspace_instance_id: String,
    pub method: String,
    pub request_id: String,
}

/// 客户端提交的 contract 原始字段（`SetContractInput::from_params` 严格解析后传入）。
///
/// 仅做形状校验（字符串/数组/对象类型）；路径相对性、列表去重/排序、independence 等
/// 语义校验在领域回调内完成（确定性拒绝）。
#[derive(Debug)]
pub struct ContractPayload {
    pub role: String,
    pub skill_id: String,
    pub skill_version: String,
    pub prompt_template_id: String,
    pub prompt_hash: String,
    pub allowed_paths: Vec<String>,
    pub forbidden_paths: Vec<String>,
    pub commands: Vec<String>,
    pub acceptance_checks: Vec<String>,
    pub required_evidence: Vec<String>,
    pub handoff_to: String,
    pub independence: serde_json::Value,
}

/// `task.contract_set` 的领域输入。
#[derive(Debug)]
pub struct SetContractInput {
    pub task_id: String,
    pub contract: ContractPayload,
    /// 授权写入者（daemon 侧 peer identity；进入 executor 前由调用方填充）。
    pub created_by: String,
}

impl SetContractInput {
    /// 从 `StrictParsedEnvelope.params` 严格解析领域输入（1D3A cutover 由
    /// `route.rs` 内部 validation 路由调用）。`task_id`/`contract`/`contract.role` 任一
    /// 缺失或非字符串即 `invalid_params`，不进入 executor；`independence` 若非 object
    /// 同样拒绝。其余可选字段缺省为空字符串/空数组/`{}`（与旧 `role_contracts` DDL
    /// DEFAULT 对齐）。
    pub fn from_params(params: &serde_json::Value) -> Result<Self, DaemonRpcError> {
        let task_id = params
            .get("task_id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .ok_or_else(|| DaemonRpcError::invalid_params("task.contract_set 缺少字段: task_id"))?;
        let contract = params
            .get("contract")
            .and_then(|v| v.as_object())
            .ok_or_else(|| DaemonRpcError::invalid_params("task.contract_set 缺少字段: contract"))?;
        let role = contract
            .get("role")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| {
                DaemonRpcError::invalid_params("task.contract_set contract.role 不能为空")
            })?;

        let get_str = |map: &serde_json::Map<String, serde_json::Value>, key: &str| {
            map.get(key).and_then(|v| v.as_str()).unwrap_or("").to_string()
        };
        let get_str_list =
            |map: &serde_json::Map<String, serde_json::Value>, key: &str| -> Result<Vec<String>, DaemonRpcError> {
                match map.get(key) {
                    None => Ok(Vec::new()),
                    Some(value) => value.as_array().ok_or_else(|| {
                        DaemonRpcError::invalid_params(format!(
                            "task.contract_set contract.{key} 必须是 JSON array"
                        ))
                    })?
                    .iter()
                    .map(|item| {
                        item.as_str().map(|s| s.to_string()).ok_or_else(|| {
                            DaemonRpcError::invalid_params(format!(
                                "task.contract_set contract.{key} 元素必须是字符串"
                            ))
                        })
                    })
                    .collect(),
                }
            };
        let independence = match contract.get("independence") {
            None => serde_json::json!({}),
            Some(value) if value.is_object() => value.clone(),
            Some(_) => {
                return Err(DaemonRpcError::invalid_params(
                    "task.contract_set contract.independence 必须是 JSON object",
                ))
            }
        };

        Ok(SetContractInput {
            task_id,
            created_by: params
                .get("created_by")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
                .unwrap_or_default(),
            contract: ContractPayload {
                role,
                skill_id: get_str(contract, "skill_id"),
                skill_version: get_str(contract, "skill_version"),
                prompt_template_id: get_str(contract, "prompt_template_id"),
                prompt_hash: get_str(contract, "prompt_hash"),
                allowed_paths: get_str_list(contract, "allowed_paths")?,
                forbidden_paths: get_str_list(contract, "forbidden_paths")?,
                commands: get_str_list(contract, "commands")?,
                acceptance_checks: get_str_list(contract, "acceptance_checks")?,
                required_evidence: get_str_list(contract, "required_evidence")?,
                handoff_to: get_str(contract, "handoff_to"),
                independence,
            },
        })
    }
}

/// `role-contract-c14n/v1` 校验后不可变规范形态（路径去重排序、列表保序）。
struct CanonicalContract {
    role: String,
    skill_id: String,
    skill_version: String,
    prompt_template_id: String,
    prompt_hash: String,
    allowed_paths: Vec<String>,
    forbidden_paths: Vec<String>,
    commands: Vec<String>,
    acceptance_checks: Vec<String>,
    required_evidence: Vec<String>,
    handoff_to: String,
    independence: serde_json::Value,
}

/// `role-contract-c14n/v1` 稳定 hash（§8.1.2）：canonical JSON → SHA-256，`sha256:<hex>`。
/// 路径集合去重排序、列表保序且重复即拒绝、independence 必须是 object；任一违反返回
/// `Err(reason)`。公开供测试与下游校验复用。
pub fn canonical_contract_hash(contract: &ContractPayload) -> Result<String, String> {
    let canonical = canonicalize_contract(contract)?;
    let payload = contract_canonical_payload(&canonical);
    let c14n = c14n_value(&payload);
    let bytes = serde_json::to_vec(&c14n).map_err(|e| format!("c14n 序列化失败: {e}"))?;
    Ok(format!("sha256:{}", hex::encode(Sha256::digest(&bytes))))
}

/// 语义校验 + 规范化（NFC；路径去重排序；列表保序重复即拒绝）。
fn canonicalize_contract(contract: &ContractPayload) -> Result<CanonicalContract, String> {
    if contract.role.trim().is_empty() {
        return Err("role 为空".to_string());
    }
    if !contract.independence.is_object() {
        return Err("independence 必须是 JSON object".to_string());
    }
    Ok(CanonicalContract {
        role: contract.role.trim().to_string(),
        skill_id: contract.skill_id.clone(),
        skill_version: contract.skill_version.clone(),
        prompt_template_id: contract.prompt_template_id.clone(),
        prompt_hash: contract.prompt_hash.clone(),
        allowed_paths: normalize_paths(&contract.allowed_paths, "allowed_paths")?,
        forbidden_paths: normalize_paths(&contract.forbidden_paths, "forbidden_paths")?,
        commands: normalize_ordered_list(&contract.commands, "commands")?,
        acceptance_checks: normalize_ordered_list(&contract.acceptance_checks, "acceptance_checks")?,
        required_evidence: normalize_ordered_list(&contract.required_evidence, "required_evidence")?,
        handoff_to: contract.handoff_to.clone(),
        independence: contract.independence.clone(),
    })
}

/// 纳入 hash 的 12 字段 canonical payload（§8.1.2）。
fn contract_canonical_payload(c: &CanonicalContract) -> serde_json::Value {
    serde_json::json!({
        "role": c.role,
        "skill_id": c.skill_id,
        "skill_version": c.skill_version,
        "prompt_template_id": c.prompt_template_id,
        "prompt_hash": c.prompt_hash,
        "allowed_paths": c.allowed_paths,
        "forbidden_paths": c.forbidden_paths,
        "commands": c.commands,
        "acceptance_checks": c.acceptance_checks,
        "required_evidence": c.required_evidence,
        "handoff_to": c.handoff_to,
        "independence": c.independence,
    })
}

/// 路径集合规范化：NFC 后校验相对性，去重并排序（§8.1.2）。
fn normalize_paths(paths: &[String], field: &str) -> Result<Vec<String>, String> {
    let mut out: Vec<String> = Vec::new();
    for path in paths {
        let normalized: String = path.nfc().collect();
        validate_relative_path(&normalized).map_err(|e| format!("{field}: {e}"))?;
        if !out.contains(&normalized) {
            out.push(normalized);
        }
    }
    out.sort();
    Ok(out)
}

/// 保序列表规范化：NFC 后保序；重复元素即拒绝（§8.1.2）。
fn normalize_ordered_list(items: &[String], field: &str) -> Result<Vec<String>, String> {
    let mut out: Vec<String> = Vec::new();
    for item in items {
        let normalized: String = item.nfc().collect();
        if out.contains(&normalized) {
            return Err(format!("{field} 存在重复元素（保序且重复即拒绝）"));
        }
        out.push(normalized);
    }
    Ok(out)
}

/// 路径只能项目相对、正斜杠、拒绝绝对路径与 `..`（§8.1.2）。
fn validate_relative_path(path: &str) -> Result<(), String> {
    if path.is_empty() {
        return Err("路径不能为空".to_string());
    }
    if path.starts_with('/') || path.starts_with('\\') {
        return Err(format!("绝对路径拒绝: {path}"));
    }
    if path.contains('\\') {
        return Err(format!("路径必须使用正斜杠（拒绝反斜杠）: {path}"));
    }
    if path.split('/').any(|seg| seg == "..") {
        return Err(format!("拒绝 `..` 路径段: {path}"));
    }
    Ok(())
}

/// 读取 `role_contract` rule row 的 rules_hash，用于 revision 的 c14n provenance。
/// 缺 row 或不可读一律 fail closed（§8.1.3，capability 未就绪）。
fn role_contract_rules_hash(conn: &Connection) -> Result<String, DaemonRpcError> {
    conn.query_row(
        "SELECT rules_hash FROM canonicalization_rule_sets \
         WHERE domain = 'role_contract' AND canonicalization_version = ?1",
        [ROLE_CONTRACT_C14N_VERSION],
        |row| row.get::<_, String>(0),
    )
    .map_err(|e| {
        DaemonRpcError::new(
            ERR_TASK_CONTRACT_INVALID,
            format!("role_contract rule row 不可用（capability 未就绪）: {e}"),
        )
    })
}

/// 领域写入成功后的响应（wrapper 统一落 ledger result）。
struct DomainWriteOk {
    response: serde_json::Value,
}

/// 领域执行期间的失败类别（封闭、类型化）。
enum ContractDomainError {
    /// 确定性、可重放失败：外层在 savepoint 回滚后写可重放 ledger error 并 commit。
    Deterministic { code: String, message: String },
    /// 基础设施失败：回滚 outer transaction、领域写入与 ledger result。
    Infrastructure(DaemonRpcError),
}

impl ContractDomainError {
    fn contract_invalid(message: String) -> Self {
        ContractDomainError::Deterministic {
            code: ERR_TASK_CONTRACT_INVALID.to_string(),
            message,
        }
    }
    fn binding_required(message: String) -> Self {
        ContractDomainError::Deterministic {
            code: ERR_TASK_BINDING_REQUIRED.to_string(),
            message,
        }
    }
    fn infra(e: DaemonRpcError) -> Self {
        ContractDomainError::Infrastructure(e)
    }
    fn infra_msg(e: rusqlite::Error, context: &str) -> Self {
        ContractDomainError::Infrastructure(DaemonRpcError::new(
            ERR_TASK_DB_TRANSACTION,
            format!("{context}: {e}"),
        ))
    }
    /// 把本地失败类别映射为 wrapper 要求的封闭 `DomainOutcome`（§3.3）。
    fn into_outcome(self) -> DomainOutcome {
        match self {
            ContractDomainError::Deterministic { code, message: _ } => {
                DomainOutcome::CommitDeterministicError {
                    stable_error: StableDomainError::DeterministicReject { code },
                }
            }
            ContractDomainError::Infrastructure(error) => {
                DomainOutcome::RollbackInfrastructureError {
                    infrastructure_error: InfrastructureError::Internal {
                        detail: error.message,
                    },
                }
            }
        }
    }
}

/// 以 `task.contract_set` 领域入口构造 `StrictParsedEnvelope`，委托统一的
/// `TaskMutationExecutor` wrapper 执行 dedupe → savepoint → 分派 → ledger（§3.3/§4.3）。
///
/// 领域语义（§8.1.2）由 `apply_set_contract` 承担：task 必须已有不可变 workspace binding；
/// payload 无法 canonicalize / 既有 lineage 断链 → 确定性错误（wrapper 回滚回调局部写入后
/// 写可重放 ledger error 并 commit）；任一 infra 失败 → wrapper 回滚整个 outer transaction。
pub fn set_task_contract(
    conn: &mut Connection,
    frozen: &FrozenAuthorityInput,
    ledger_key: &LedgerKey,
    input: &SetContractInput,
) -> Result<serde_json::Value, DaemonRpcError> {
    let envelope = StrictParsedEnvelope {
        workspace_instance_id: ledger_key.workspace_instance_id.clone(),
        canonical_method: ledger_key.method.clone(),
        request_id: ledger_key.request_id.clone(),
        params: serde_json::json!({
            "task_id": input.task_id,
            "contract": {
                "role": input.contract.role,
                "skill_id": input.contract.skill_id,
                "skill_version": input.contract.skill_version,
                "prompt_template_id": input.contract.prompt_template_id,
                "prompt_hash": input.contract.prompt_hash,
                "allowed_paths": input.contract.allowed_paths,
                "forbidden_paths": input.contract.forbidden_paths,
                "commands": input.contract.commands,
                "acceptance_checks": input.contract.acceptance_checks,
                "required_evidence": input.contract.required_evidence,
                "handoff_to": input.contract.handoff_to,
                "independence": input.contract.independence,
            },
            "created_by": input.created_by,
        }),
        invocation_class: InvocationClass::ExternalTransport,
    };
    TaskMutationExecutor::default().run(conn, &envelope, frozen, |domain_tx, frozen_ref| {
        apply_set_contract(domain_tx, frozen_ref, input)
    })
}

/// `task.contract_set` 领域回调：在受保护事务内完成 c14n 校验 + lineage/revision 写入，
/// 返回封闭 `DomainOutcome` 交由 wrapper 分派（§3.3）。
fn apply_set_contract(
    tx: &mut TaskDomainTx<'_>,
    _frozen: &FrozenAuthorityInput,
    input: &SetContractInput,
) -> DomainOutcome {
    match write_domain(tx.tx(), input) {
        Ok(ok) => DomainOutcome::CommitSuccess { response: ok.response },
        Err(err) => err.into_outcome(),
    }
}

/// 在 savepoint 事务内执行 c14n 校验 + lineage/revision 写入（§8.1.2）。
fn write_domain(
    tx: &Connection,
    input: &SetContractInput,
) -> Result<DomainWriteOk, ContractDomainError> {
    // 1. 语义校验 + 规范化 + hash（绝对路径/`..`/列表重复/independence 非 object → 确定性拒绝）。
    let canonical = canonicalize_contract(&input.contract)
        .map_err(ContractDomainError::contract_invalid)?;
    let payload = contract_canonical_payload(&canonical);
    let c14n = c14n_value(&payload);
    let bytes = serde_json::to_vec(&c14n)
        .map_err(|e| ContractDomainError::contract_invalid(format!("c14n 序列化失败: {e}")))?;
    let hash = format!("sha256:{}", hex::encode(Sha256::digest(&bytes)));
    let rules_hash = role_contract_rules_hash(tx).map_err(ContractDomainError::infra)?;

    // 2. task 必须已有不可变 workspace binding（§8.1.2：缺 binding 即无法确定归属 → 拒绝）。
    let workspace_id: Option<i64> = tx
        .query_row(
            "SELECT workspace_id FROM task_workspace_bindings WHERE task_id = ?1",
            [&input.task_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| ContractDomainError::infra_msg(e, "task workspace binding 查询失败"))?;
    let workspace_id = workspace_id.ok_or_else(|| {
        ContractDomainError::binding_required(format!(
            "task {} 没有不可变 task workspace binding（v1 contract_set 拒绝）",
            input.task_id
        ))
    })?;

    // 3. 既有 lineage 查找（(task_id, workspace_id, role) 唯一）。
    let existing: Option<String> = tx
        .query_row(
            "SELECT role_contract_lineage_id FROM role_contract_lineages \
             WHERE task_id = ?1 AND workspace_id = ?2 AND role = ?3",
            rusqlite::params![input.task_id, workspace_id, canonical.role],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| ContractDomainError::infra_msg(e, "role contract lineage 查询失败"))?;

    let (lineage_id, next_revision, supersedes) = match existing {
        Some(lineage_id) => {
            // 4a. 追加前校验 revision 链连续性（§8.1.2）：COUNT 必须等于 MAX(revision)，
            //     任何缺失/重复/分叉 → 该 lineage 及其 binding 一律 UNVERIFIED，拒绝追加。
            let (count, max): (i64, i64) = tx
                .query_row(
                    "SELECT COUNT(*), COALESCE(MAX(revision), 0) \
                     FROM role_contract_revisions WHERE role_contract_lineage_id = ?1",
                    [&lineage_id],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .map_err(|e| ContractDomainError::infra_msg(e, "revision 链读取失败"))?;
            if count != max || max == 0 {
                return Err(ContractDomainError::contract_invalid(format!(
                    "lineage {lineage_id} revision 链断链/缺失（count={count} max={max}），\
                     按 UNVERIFIED 拒绝追加"
                )));
            }
            // 4b. n>1 必须指向同 lineage 的 n-1（当前 max revision 的不可变 id）。
            let prev_id: String = tx
                .query_row(
                    "SELECT role_contract_revision_id FROM role_contract_revisions \
                     WHERE role_contract_lineage_id = ?1 ORDER BY revision DESC LIMIT 1",
                    [&lineage_id],
                    |row| row.get(0),
                )
                .map_err(|e| ContractDomainError::infra_msg(e, "前序 revision 查询失败"))?;
            (lineage_id, max + 1, Some(prev_id))
        }
        None => {
            // 4c. 首条：创建 lineage，revision 1 的 supersedes 为 NULL。
            let lineage_id = format!("rcl-{}-{}", input.task_id, canonical.role);
            tx.execute(
                "INSERT INTO role_contract_lineages \
                 (role_contract_lineage_id, task_id, workspace_id, role, \
                  created_by, authoritative_created_at) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                rusqlite::params![
                    lineage_id,
                    input.task_id,
                    workspace_id,
                    canonical.role,
                    input.created_by,
                    literal_now(),
                ],
            )
            .map_err(|e| ContractDomainError::infra_msg(e, "写入 role_contract_lineages 失败"))?;
            (lineage_id, 1, None)
        }
    };

    // 5. 追加不可变 revision（revision/hash/c14n 版本/rules hash 全量持久化 provenance）。
    let revision_id = format!("rcr-{}-{}-r{}", input.task_id, canonical.role, next_revision);
    let canonical_payload_json = serde_json::to_string(&c14n)
        .map_err(|e| ContractDomainError::contract_invalid(format!("payload 序列化失败: {e}")))?;
    tx.execute(
        "INSERT INTO role_contract_revisions \
         (role_contract_revision_id, role_contract_lineage_id, revision, \
          supersedes_revision_id, canonical_payload_json, canonicalization_version, \
          canonicalization_rules_hash, role_contract_hash, created_by, \
          authoritative_created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
        rusqlite::params![
            revision_id,
            lineage_id,
            next_revision,
            supersedes,
            canonical_payload_json,
            ROLE_CONTRACT_C14N_VERSION,
            rules_hash,
            hash,
            input.created_by,
            literal_now(),
        ],
    )
    .map_err(|e| ContractDomainError::infra_msg(e, "写入 role_contract_revisions 失败"))?;

    Ok(DomainWriteOk {
        response: serde_json::json!({
            "ok": true,
            "task_id": input.task_id,
            "role": canonical.role,
            "role_contract_lineage_id": lineage_id,
            "role_contract_revision_id": revision_id,
            "revision": next_revision,
            "role_contract_hash": hash,
            "canonicalization_version": ROLE_CONTRACT_C14N_VERSION,
            "canonicalization_rules_hash": rules_hash,
        }),
    })
}

/// 权威 UTC 秒值（微秒精度）文本；真实接入由 Authoritative_Clock 产生。
fn literal_now() -> String {
    format!("{:.6}", now_unix())
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
