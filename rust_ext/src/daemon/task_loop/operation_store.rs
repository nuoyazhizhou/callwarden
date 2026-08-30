//! 1D1：task-domain operation store（cw-role-handoff-task-loop.md §4.3 / §8.1.3 / §8.1.4）。
//!
//! 权威 operation/dedup ledger `task_operation_ledger` 的读写原语与
//! `operation-params-c14n/v1` canonicalization。本模块**无状态**：全部权威状态都在
//! task-DB 的 ledger/rule registry 表内，重启即恢复。
//!
//! 语义（§4.3 / §8.1.4）：
//! - 只服务 §4.3 固定的 TASK_DB_LEDGER method scope；key 固定为
//!   `(workspace_instance_id, canonical_method, request_id)`。
//! - 已提交 key 必须用该行**已保存**的 version/rules hash 重算 incoming payload：
//!   同 hash 只读重放该行结果且不追加事件；不同 hash 优先
//!   `E_REQUEST_ID_REUSE_MISMATCH`，绝不修改该行。
//! - 首次 key 使用当前**未 revoked** 的 operation_params rules 计算 hash 并返回
//!   `DedupeOutcome::FirstRequest`，由 wrapper 执行领域校验后在同一事务内
//!   `record_result` 落账（成功或确定性错误）。
//! - 找不到绑定 rules / c14n 失败 / rules hash 不匹配一律 fail closed，不得把
//!   重试误写成新的 operation。

use rusqlite::{params, Connection, Transaction};
use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;

use crate::daemon::dispatch::DaemonRpcError;
use super::types::ERR_REQUEST_ID_REUSE_MISMATCH;

/// 找不到当前未 revoked 或已绑定的 operation_params rules / 无法重放既有结果。
pub const ERR_OPERATION_C14N_UNAVAILABLE: &str = "E_OPERATION_C14N_UNAVAILABLE";
/// canonical payload 计算失败（c14n 序列化失败或不受支持的 rules）。
pub const ERR_CANONICALIZATION_FAILED: &str = "E_CANONICALIZATION_FAILED";
/// ledger 插入违反幂等约束（key 在 dedupe 后被并发写入，必须 fail closed）。
pub const ERR_LEDGER_CONFLICT: &str = "E_OPERATION_LEDGER_CONFLICT";

/// §4.3 固定的 TASK_DB_LEDGER method scope（canonical method 形态）。
const TASK_DB_LEDGER_METHODS: &[&str] = &[
    "task.create",
    "task.contract_set",
    // P0-C：Task/Role/step governance projection bootstrap 与其审计写入必须同样 durable。
    "task.contract_bootstrap",
    // P0-F：Bootstrap Evidence / Review Bridge 的两个 append-only mutation 必须进 ledger。
    "task.bootstrap_executor_evidence",
    "task.bootstrap_reviewer_pass",
    "task.claim",
    "task.report",
    "task.handoff",
    "task.p0l_identity_policy_repair",
    "task.p0l_identity_policy_bootstrap_repair",
    "role_worker.rotate",
    "task.apply",
    "task.close",
    // P0-H（T-1787277487109-758e56d0）：task.supersede 治理 mutation 纳入
    // TASK_DB_LEDGER scope，与 relation/event/task_events 同一事务经
    // OperationStore::dedupe + record_result 持久化（同 request_id 幂等重放）。
    "task.supersede",
    // P0-B：历史无 binding task 的 authority attestation 同样需要持久化重放。
    "task.attest_legacy_workspace_binding",
    // 历史任务状态 reconciliation：只允许 daemon 追加可证明的生命周期事件。
    "task.reconcile",
    "lease.acquire",
    "lease.renew",
    "lease.release",
    "verdict.submit",
    "reveal.submit",
    "evidence.append",
    "gate.decide",
];

/// `operation-params-c14n/v1` 顶层排除的 key：已进入 dedup key，不能重复进入 payload。
const EXCLUDED_TOP_LEVEL_KEYS: &[&str] = &["request_id", "workspace_instance_id"];

/// operation_params 规则集的权威投影（来自 canonicalization_rule_sets 行）。
#[derive(Debug, Clone)]
pub struct ParamsRules {
    pub rule_set_id: String,
    pub canonicalization_version: String,
    pub rules_hash: String,
    pub rules_payload_json: String,
}

/// dedupe 判定结果（封闭、类型化；禁止用错误字符串推断类别）。
#[derive(Debug)]
pub enum DedupeOutcome {
    /// 同 key 同 hash：只读重放已保存的持久化结果（不追加任何事件）。
    Replay {
        response_or_error_json: serde_json::Value,
    },
    /// 首次 key：调用方随后执行领域校验并 `record_result` 落账。
    FirstRequest {
        rules: ParamsRules,
        canonical_params_hash: String,
    },
}

/// ledger 行的 Task/Role Contract provenance（§8.1.4）。
///
/// 1D1 无领域 handler，允许全 NULL；领域任务接入后填充。
#[derive(Debug, Clone, Default)]
pub struct LedgerProvenance {
    pub workspace_id: Option<i64>,
    pub task_id: Option<String>,
    pub role_contract_revision_id: Option<String>,
    pub role_contract_hash: Option<String>,
    pub role_contract_canonicalization_version: Option<String>,
    pub role_contract_canonicalization_rules_hash: Option<String>,
}

/// task-domain operation store（无状态；全部权威状态在 task-DB ledger 表内）。
#[derive(Debug, Default)]
pub struct OperationStore;

impl OperationStore {
    /// 规范化 method（§4.3：`lease.extend` 规范化为 `lease.renew`，杜绝双入口）。
    ///
    /// 返回值固定为 canonical method；对 unknown method 原样返回（由 route 侧
    /// 静态 `method → dedup_route` 表 fail closed，本模块不做领域判定）。
    pub fn canonical_method(method: &str) -> &str {
        match method {
            "lease.extend" => "lease.renew",
            other => other,
        }
    }

    /// method 是否落在 §4.3 TASK_DB_LEDGER scope（入参需先 canonicalize）。
    pub fn is_task_db_ledger_method(method: &str) -> bool {
        TASK_DB_LEDGER_METHODS.contains(&Self::canonical_method(method))
    }

    /// 幂等去重：已提交 key 重放/冲突判定；首次 key 返回 FirstRequest。
    ///
    /// `params` 是完整 method payload（envelope.params）；`request_id` 与
    /// `workspace_instance_id` 在 key 内，c14n 时从顶层排除。
    pub fn dedupe(
        &self,
        conn: &Connection,
        workspace_instance_id: &str,
        method: &str,
        request_id: &str,
        params: &serde_json::Value,
    ) -> Result<DedupeOutcome, DaemonRpcError> {
        let canonical_method = Self::canonical_method(method);

        // 1. 查已提交 key（该行保存的 version/rules hash 是重算的唯一权威）。
        let bound = conn
            .query_row(
                "SELECT params_canonicalization_version, params_canonicalization_rules_hash, \
                        canonical_params_hash, response_or_error_json \
                 FROM task_operation_ledger \
                 WHERE workspace_instance_id = ?1 AND method = ?2 AND request_id = ?3",
                params![workspace_instance_id, canonical_method, request_id],
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
            // 2a. 已提交 key：用保存的 rules 重算，同 hash 只读重放。
            Ok((version, rules_hash, stored_hash, stored_json)) => {
                let saved_rules = params_rules_bound(conn, &version, &rules_hash)?;
                let incoming_hash = canonical_params_hash(&saved_rules, params)?;
                if incoming_hash != stored_hash {
                    return Err(DaemonRpcError::new(
                        ERR_REQUEST_ID_REUSE_MISMATCH,
                        format!(
                            "request_id 复用但 canonical 参数不同 \
                             (workspace_instance_id={} method={} request_id={})",
                            workspace_instance_id, canonical_method, request_id
                        ),
                    ));
                }
                let value = serde_json::from_str(&stored_json).map_err(|e| {
                    DaemonRpcError::new(
                        ERR_OPERATION_C14N_UNAVAILABLE,
                        format!("ledger 保存的结果不可解析: {e}"),
                    )
                })?;
                Ok(DedupeOutcome::Replay {
                    response_or_error_json: value,
                })
            }
            // 2b. 首次 key：取当前未 revoked rules 计算 hash。
            Err(rusqlite::Error::QueryReturnedNoRows) => {
                let rules = params_rules_current(conn)?;
                let canonical_params_hash = canonical_params_hash(&rules, params)?;
                Ok(DedupeOutcome::FirstRequest {
                    rules,
                    canonical_params_hash,
                })
            }
            Err(e) => Err(DaemonRpcError::new(
                ERR_OPERATION_C14N_UNAVAILABLE,
                format!("ledger 读取失败: {e}"),
            )),
        }
    }

    /// 在 outer transaction 内记录首次请求的权威结果（成功或确定性错误）。
    ///
    /// 必须在领域校验之后、同一事务 commit 之前调用；Savepoint/commit/rollback
    /// 语义由 executor wrapper 负责。若 key 在 dedupe 后被并发写入，约束冲突
    /// 映射为 `E_OPERATION_LEDGER_CONFLICT` 并 fail closed（绝不上层覆盖）。
    #[allow(clippy::too_many_arguments)]
    pub fn record_result(
        &self,
        tx: &Transaction<'_>,
        workspace_instance_id: &str,
        method: &str,
        request_id: &str,
        rules: &ParamsRules,
        canonical_params_hash: &str,
        provenance: &LedgerProvenance,
        response_or_error_json: &serde_json::Value,
    ) -> Result<(), DaemonRpcError> {
        let canonical_method = Self::canonical_method(method);
        let durable_json = serde_json::to_string(response_or_error_json).map_err(|e| {
            DaemonRpcError::new(
                ERR_CANONICALIZATION_FAILED,
                format!("结果序列化失败: {e}"),
            )
        })?;
        tx.execute(
            "INSERT INTO task_operation_ledger \
             (workspace_instance_id, method, request_id, \
              params_canonicalization_version, params_canonicalization_rules_hash, \
              canonical_params_hash, \
              workspace_id, task_id, role_contract_revision_id, role_contract_hash, \
              role_contract_canonicalization_version, role_contract_canonicalization_rules_hash, \
              response_or_error_json, authoritative_created_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, \
                     strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            params![
                workspace_instance_id,
                canonical_method,
                request_id,
                rules.canonicalization_version,
                rules.rules_hash,
                canonical_params_hash,
                provenance.workspace_id,
                provenance.task_id,
                provenance.role_contract_revision_id,
                provenance.role_contract_hash,
                provenance.role_contract_canonicalization_version,
                provenance.role_contract_canonicalization_rules_hash,
                durable_json,
            ],
        )
        .map_err(|e| {
            // rusqlite 0.31：ErrorCode 只暴露 SQLITE_CONSTRAINT 基类（ConstraintViolation）；
            // 主键/唯一等细分 extended code 不再作为独立 variant 暴露。
            let is_constraint = e
                .sqlite_error_code()
                .map(|c| c == rusqlite::ErrorCode::ConstraintViolation)
                .unwrap_or(false);
            if is_constraint {
                DaemonRpcError::new(
                    ERR_LEDGER_CONFLICT,
                    format!(
                        "ledger key 已被并发写入 (workspace_instance_id={} method={} request_id={})",
                        workspace_instance_id, canonical_method, request_id
                    ),
                )
            } else {
                DaemonRpcError::new(
                    ERR_OPERATION_C14N_UNAVAILABLE,
                    format!("ledger 写入失败: {e}"),
                )
            }
        })?;
        Ok(())
    }
}

/// 查询当前**未 revoked** 的 operation_params rule set（§8.1.4：首次 key 使用）。
///
/// 只取 domain='operation_params' 且未被 canonicalization_rule_revocations 撤销的
/// rule row，按 authoritative_created_at 取最新一条；无可用规则即 fail closed。
/// 返回前原子校验该行 `rules_hash` 与 `rules_payload_json` 的一致性（rules-c14n/v1）。
///
/// pub(crate)：1D3B 的 promotion 账本 dedupe 复用。
pub(crate) fn params_rules_current(conn: &Connection) -> Result<ParamsRules, DaemonRpcError> {
    let row = conn
        .query_row(
            "SELECT r.rule_set_id, r.canonicalization_version, r.rules_hash, r.rules_payload_json \
             FROM canonicalization_rule_sets r \
             LEFT JOIN canonicalization_rule_revocations rv ON rv.rule_set_id = r.rule_set_id \
             WHERE r.domain = 'operation_params' AND rv.rule_set_id IS NULL \
             ORDER BY r.authoritative_created_at DESC \
             LIMIT 1",
            [],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                ))
            },
        )
        .map_err(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => DaemonRpcError::new(
                ERR_OPERATION_C14N_UNAVAILABLE,
                "无未撤销的 operation_params rule set（capability 未就绪）",
            ),
            other => DaemonRpcError::new(
                ERR_OPERATION_C14N_UNAVAILABLE,
                format!("operation_params rule set 读取失败: {other}"),
            ),
        })?;
    let rules = ParamsRules {
        rule_set_id: row.0,
        canonicalization_version: row.1,
        rules_hash: row.2,
        rules_payload_json: row.3,
    };
    verify_rules_hash(&rules)?;
    Ok(rules)
}

/// 查询**已绑定**（可能已 revoked/升级）的 rule set（§8.1.4：历史 ledger 重放必须
/// 使用其保存的规则，不能按“最新”规则重解释）。
///
/// pub(crate)：1D3B 的 promotion 账本 dedupe 复用。
pub(crate) fn params_rules_bound(
    conn: &Connection,
    canonicalization_version: &str,
    rules_hash: &str,
) -> Result<ParamsRules, DaemonRpcError> {
    let row = conn
        .query_row(
            "SELECT rule_set_id, canonicalization_version, rules_hash, rules_payload_json \
             FROM canonicalization_rule_sets \
             WHERE domain = 'operation_params' \
               AND canonicalization_version = ?1 AND rules_hash = ?2",
            params![canonicalization_version, rules_hash],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                ))
            },
        )
        .map_err(|e| {
            DaemonRpcError::new(
                ERR_OPERATION_C14N_UNAVAILABLE,
                format!("已绑定 rule set 不可用 (version={canonicalization_version} hash={rules_hash}): {e}"),
            )
        })?;
    let rules = ParamsRules {
        rule_set_id: row.0,
        canonicalization_version: row.1,
        rules_hash: row.2,
        rules_payload_json: row.3,
    };
    verify_rules_hash(&rules)?;
    Ok(rules)
}

/// 原子校验 rule row：`rules_hash` 必须是 `rules-c14n/v1` 对 `rules_payload_json`
/// 的 SHA-256（UTF-8、Unicode NFC、键按 code point 排序、无额外空白），格式
/// `sha256:<hex>`。任何失配都 fail closed（§8.1.3：同 version 不同 hash、缺 row
/// 或 hash 不匹配均禁用对应 capability）。
fn verify_rules_hash(rules: &ParamsRules) -> Result<(), DaemonRpcError> {
    let expected = rules_c14n_hash(&rules.rules_payload_json);
    if rules.rules_hash != expected {
        return Err(DaemonRpcError::new(
            ERR_OPERATION_C14N_UNAVAILABLE,
            format!(
                "rules hash 失配 fail closed (version={} stored={} recomputed={})",
                rules.canonicalization_version, rules.rules_hash, expected
            ),
        ));
    }
    Ok(())
}

/// `rules-c14n/v1`：对规则载荷做 UTF-8、Unicode NFC、键按 code point 排序、无额外
/// 空白的 canonical JSON 后取 SHA-256，表示为 `sha256:<hex>`（§8.1.3 自举冻结）。
fn rules_c14n_hash(payload: &str) -> String {
    let value: serde_json::Value = serde_json::from_str(payload).unwrap_or_default();
    let canonical = c14n_value(&value);
    let bytes = serde_json::to_vec(&canonical).unwrap_or_default();
    let digest = Sha256::digest(&bytes);
    format!("sha256:{}", hex::encode(digest))
}

/// `operation-params-c14n/v1` canonical payload hash（§8.1.4）。
///
/// 1. 顶层排除已进入 key 的 `request_id` / `workspace_instance_id`；
/// 2. 递归 UTF-8 / Unicode NFC（字符串值与 key 均规范化）；
/// 3. 对象键按 Unicode code point 排序（NFC 化后 UTF-8 字节序即 code point 序）；
/// 4. 按 RFC 8785 JSON Canonicalization Scheme 的紧凑子集序列化
///    （无空白、非 ASCII 原样 UTF-8 输出、转义规则与 serde_json 一致；
///    浮点/大整数严格 RFC 8785 数字规则不在 1D1 支持范围，params 以字符串/整数为主）；
/// 5. SHA-256 表示为 `sha256:<hex>`。
///
/// 仅支持 `canonicalization_version == operation-params-c14n/v1`；其余 version 一律
/// fail closed（防止新规则被本实现静默按旧算法解释）。
pub fn canonical_params_hash(
    rules: &ParamsRules,
    params: &serde_json::Value,
) -> Result<String, DaemonRpcError> {
    if rules.canonicalization_version != "operation-params-c14n/v1" {
        return Err(DaemonRpcError::new(
            ERR_CANONICALIZATION_FAILED,
            format!(
                "不支持的 canonicalization version: {}（1D1 仅实现 operation-params-c14n/v1）",
                rules.canonicalization_version
            ),
        ));
    }
    // 顶层排除 key（仅对象形态；非对象 payload 视为完整 payload）。
    let mut payload = params.clone();
    if let serde_json::Value::Object(map) = &mut payload {
        map.retain(|k, _| !EXCLUDED_TOP_LEVEL_KEYS.contains(&k.as_str()));
    }
    let canonical = c14n_value(&payload);
    let bytes = serde_json::to_vec(&canonical)
        .map_err(|e| DaemonRpcError::new(ERR_CANONICALIZATION_FAILED, format!("c14n 序列化失败: {e}")))?;
    let digest = Sha256::digest(&bytes);
    Ok(format!("sha256:{}", hex::encode(digest)))
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
        serde_json::Value::String(s) => {
            serde_json::Value::String(s.nfc().collect::<String>())
        }
        other => other.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sqlite_query::migrate_connection;

    fn migrated_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        migrate_connection(&conn).unwrap();
        conn
    }

    fn rules_from_current(conn: &Connection) -> ParamsRules {
        match OperationStore.dedupe(
            conn,
            "ws-test",
            "task.create",
            "req-probe",
            &serde_json::json!({ "probe": true }),
        )
        .unwrap()
        {
            DedupeOutcome::FirstRequest { rules, .. } => rules,
            _ => panic!("probe should be first request"),
        }
    }

    #[test]
    fn canonical_method_normalizes_lease_extend() {
        assert_eq!(OperationStore::canonical_method("lease.extend"), "lease.renew");
        assert_eq!(OperationStore::canonical_method("lease.renew"), "lease.renew");
        assert_eq!(OperationStore::canonical_method("task.create"), "task.create");
    }

    #[test]
    fn method_scope_covers_task_db_ledger() {
        for method in [
            "task.create",
            "task.contract_set",
            "task.claim",
            "task.report",
            "task.handoff",
            "task.apply",
            "task.close",
            "task.supersede",
            "lease.acquire",
            "lease.renew",
            "lease.release",
            "verdict.submit",
            "reveal.submit",
            "evidence.append",
            "gate.decide",
        ] {
            assert!(OperationStore::is_task_db_ledger_method(method), "{method}");
        }
        // lease.extend 规范化后仍落在 scope 内，杜绝双入口获得不同 authority
        assert!(OperationStore::is_task_db_ledger_method("lease.extend"));
        assert!(!OperationStore::is_task_db_ledger_method("snapshot.create"));
        assert!(!OperationStore::is_task_db_ledger_method("task_loop.public_promote"));
    }

    #[test]
    fn canonical_hash_deterministic_and_excludes_key_fields() {
        let conn = migrated_db();
        let rules = rules_from_current(&conn);
        let a = serde_json::json!({
            "request_id": "ignored-request-id",
            "workspace_instance_id": "ignored-ws",
            "task_id": "T-1",
            "note": "hello",
        });
        let b = serde_json::json!({
            "task_id": "T-1",
            "note": "hello",
        });
        let h_a = canonical_params_hash(&rules, &a).unwrap();
        let h_b = canonical_params_hash(&rules, &b).unwrap();
        assert_eq!(h_a, h_b, "key 内字段不得进入 payload hash");
        assert!(h_a.starts_with("sha256:"));

        let c = serde_json::json!({ "task_id": "T-1", "note": "hello!" });
        assert_ne!(
            canonical_params_hash(&rules, &c).unwrap(),
            h_a,
            "参数变化必须改变 hash"
        );
    }

    #[test]
    fn canonical_hash_sorts_keys_and_normalizes_nfc() {
        let conn = migrated_db();
        let rules = rules_from_current(&conn);
        // 键顺序不同但内容相同 → 同 hash（按 code point 排序）
        let x = serde_json::json!({ "b": 2, "a": 1, "c": 3 });
        let y = serde_json::json!({ "c": 3, "a": 1, "b": 2 });
        assert_eq!(
            canonical_params_hash(&rules, &x).unwrap(),
            canonical_params_hash(&rules, &y).unwrap()
        );
        // NFC：é(U+00E9) 与 e+combining acute(U+0065 U+0301) 规范化后同 hash
        let precomposed = serde_json::json!({ "name": "caf\u{e9}" });
        let decomposed = serde_json::json!({ "name": "cafe\u{301}" });
        assert_eq!(
            canonical_params_hash(&rules, &precomposed).unwrap(),
            canonical_params_hash(&rules, &decomposed).unwrap()
        );
        // 数组保持顺序；对象嵌套也排序
        let nested_a = serde_json::json!({ "items": [{ "z": 1, "a": 2 }] });
        let nested_b = serde_json::json!({ "items": [{ "a": 2, "z": 1 }] });
        assert_eq!(
            canonical_params_hash(&rules, &nested_a).unwrap(),
            canonical_params_hash(&rules, &nested_b).unwrap()
        );
    }

    #[test]
    fn first_request_then_replay_same_hash() {
        let mut conn = migrated_db();
        let store = OperationStore;
        let params = serde_json::json!({ "task_id": "T-1", "title": "build" });

        // 首次 → FirstRequest
        let first = store
            .dedupe(&conn, "ws-1", "task.create", "req-1", &params)
            .unwrap();
        let (rules, hash) = match first {
            DedupeOutcome::FirstRequest { rules, canonical_params_hash } => {
                (rules, canonical_params_hash)
            }
            DedupeOutcome::Replay { .. } => panic!("first request must not replay"),
        };

        // 落账（成功结果）
        let tx = conn.transaction().unwrap();
        store
            .record_result(
                &tx,
                "ws-1",
                "task.create",
                "req-1",
                &rules,
                &hash,
                &LedgerProvenance::default(),
                &serde_json::json!({ "ok": true, "task_id": "T-1" }),
            )
            .unwrap();
        tx.commit().unwrap();

        // 同 key 同 hash → 只读重放（不追加任何事件）
        let replay = store
            .dedupe(&conn, "ws-1", "task.create", "req-1", &params)
            .unwrap();
        match replay {
            DedupeOutcome::Replay { response_or_error_json } => {
                assert_eq!(response_or_error_json, serde_json::json!({ "ok": true, "task_id": "T-1" }));
            }
            DedupeOutcome::FirstRequest { .. } => panic!("committed key must replay"),
        }
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM task_operation_ledger", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1, "重放不得追加事件");
    }

    #[test]
    fn reuse_mismatch_rejects_and_preserves_row() {
        let mut conn = migrated_db();
        let store = OperationStore;
        let original = serde_json::json!({ "task_id": "T-1", "title": "build" });
        let first = store
            .dedupe(&conn, "ws-1", "task.create", "req-1", &original)
            .unwrap();
        let (rules, hash) = match first {
            DedupeOutcome::FirstRequest { rules, canonical_params_hash } => {
                (rules, canonical_params_hash)
            }
            DedupeOutcome::Replay { .. } => panic!(),
        };
        let tx = conn.transaction().unwrap();
        store
            .record_result(
                &tx,
                "ws-1",
                "task.create",
                "req-1",
                &rules,
                &hash,
                &LedgerProvenance::default(),
                &serde_json::json!({ "ok": true }),
            )
            .unwrap();
        tx.commit().unwrap();

        // 同 key 不同参数 → E_REQUEST_ID_REUSE_MISMATCH
        let changed = serde_json::json!({ "task_id": "T-1", "title": "rebuild" });
        let err = store
            .dedupe(&conn, "ws-1", "task.create", "req-1", &changed)
            .unwrap_err();
        assert_eq!(err.code, ERR_REQUEST_ID_REUSE_MISMATCH);

        // 行未被修改（仍保存原始 hash 与结果）
        let (stored_hash, stored_json): (String, String) = conn
            .query_row(
                "SELECT canonical_params_hash, response_or_error_json FROM task_operation_ledger \
                 WHERE workspace_instance_id='ws-1' AND method='task.create' AND request_id='req-1'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(stored_hash, hash);
        assert_eq!(stored_json, serde_json::json!({ "ok": true }).to_string());
    }

    #[test]
    fn replay_uses_bound_rules_not_current_after_upgrade() {
        let mut conn = migrated_db();
        let store = OperationStore;
        let params = serde_json::json!({ "task_id": "T-1" });

        // 用当前 v1 rules 落一笔账
        let first = store
            .dedupe(&conn, "ws-1", "task.claim", "req-1", &params)
            .unwrap();
        let (rules_v1, hash_v1) = match first {
            DedupeOutcome::FirstRequest { rules, canonical_params_hash } => {
                (rules, canonical_params_hash)
            }
            DedupeOutcome::Replay { .. } => panic!(),
        };
        let tx = conn.transaction().unwrap();
        store
            .record_result(
                &tx,
                "ws-1",
                "task.claim",
                "req-1",
                &rules_v1,
                &hash_v1,
                &LedgerProvenance::default(),
                &serde_json::json!({ "claimed": true }),
            )
            .unwrap();
        tx.commit().unwrap();

        // 撤销 v1 rule，并插入一个不同 payload/hash 的 v2 rule row（模拟升级）
        let tx = conn.transaction().unwrap();
        tx.execute(
            "INSERT INTO canonicalization_rule_revocations \
             (revocation_id, rule_set_id, reason, revoked_by, authoritative_revoked_at) \
             VALUES ('rev-1', 'operation-params-c14n/v1', 'upgrade', 'test', 't')",
            [],
        )
        .unwrap();
        tx.execute(
            "INSERT INTO canonicalization_rule_sets \
             (rule_set_id, domain, canonicalization_version, rules_payload_json, \
              rules_c14n_version, rules_hash, created_by, authoritative_created_at) \
             VALUES ('operation-params-c14n/v2', 'operation_params', 'operation-params-c14n/v2', \
                     '{\"canonicalization_version\":\"operation-params-c14n/v2\"}', \
                     'rules-c14n/v1', 'sha256:not-v1', 'test', 't')",
            [],
        )
        .unwrap();
        tx.commit().unwrap();

        // 历史 key 必须用**已绑定** v1 rules 重放；daemon 升级不得按最新规则重解释
        let replay = store
            .dedupe(&conn, "ws-1", "task.claim", "req-1", &params)
            .unwrap();
        match replay {
            DedupeOutcome::Replay { response_or_error_json } => {
                assert_eq!(response_or_error_json, serde_json::json!({ "claimed": true }));
            }
            DedupeOutcome::FirstRequest { .. } => panic!("committed key must replay"),
        }
    }

    #[test]
    fn savepoint_rollback_discards_callback_writes_but_keeps_deterministic_error_ledger_row() {
        let mut conn = migrated_db();
        let store = OperationStore;
        let params = serde_json::json!({ "task_id": "T-1" });
        let first = store
            .dedupe(&conn, "ws-1", "task.close", "req-1", &params)
            .unwrap();
        let (rules, hash) = match first {
            DedupeOutcome::FirstRequest { rules, canonical_params_hash } => {
                (rules, canonical_params_hash)
            }
            DedupeOutcome::Replay { .. } => panic!(),
        };

        let tx = conn.transaction().unwrap();
        tx.execute_batch("CREATE TABLE callback_marker (v TEXT)").unwrap();
        tx.execute_batch("SAVEPOINT task_domain_callback").unwrap();
        // 模拟领域回调的局部写入
        tx.execute("INSERT INTO callback_marker VALUES ('written')", []).unwrap();
        // 回调返回确定性错误 → 撤销回调局部写入，再写可重放 ledger error 并 commit
        tx.execute_batch(
            "ROLLBACK TO task_domain_callback; RELEASE SAVEPOINT task_domain_callback",
        )
        .unwrap();
        let marker_count: i64 = tx
            .query_row("SELECT COUNT(*) FROM callback_marker", [], |r| r.get(0))
            .unwrap();
        assert_eq!(marker_count, 0, "savepoint rollback 必须撤销回调局部写入");

        store
            .record_result(
                &tx,
                "ws-1",
                "task.close",
                "req-1",
                &rules,
                &hash,
                &LedgerProvenance::default(),
                &serde_json::json!({ "error": { "code": "E_STABLE_REJECT" } }),
            )
            .unwrap();
        tx.commit().unwrap();

        // 确定性错误以可重放 ledger row 提交；同 key 重放返回该错误结果
        let replay = store
            .dedupe(&conn, "ws-1", "task.close", "req-1", &params)
            .unwrap();
        match replay {
            DedupeOutcome::Replay { response_or_error_json } => {
                assert_eq!(
                    response_or_error_json,
                    serde_json::json!({ "error": { "code": "E_STABLE_REJECT" } })
                );
            }
            DedupeOutcome::FirstRequest { .. } => panic!("deterministic error must persist"),
        }
    }

    #[test]
    fn record_result_conflict_fails_closed() {
        let mut conn = migrated_db();
        let store = OperationStore;
        let params = serde_json::json!({ "task_id": "T-1" });
        let first = store
            .dedupe(&conn, "ws-1", "task.create", "req-1", &params)
            .unwrap();
        let (rules, hash) = match first {
            DedupeOutcome::FirstRequest { rules, canonical_params_hash } => {
                (rules, canonical_params_hash)
            }
            DedupeOutcome::Replay { .. } => panic!(),
        };
        let tx = conn.transaction().unwrap();
        store
            .record_result(
                &tx,
                "ws-1",
                "task.create",
                "req-1",
                &rules,
                &hash,
                &LedgerProvenance::default(),
                &serde_json::json!({ "ok": true }),
            )
            .unwrap();
        tx.commit().unwrap();

        // 同 key 再落不同结果 → 约束冲突映射为 fail-closed，绝不覆盖
        let tx2 = conn.transaction().unwrap();
        let err = store
            .record_result(
                &tx2,
                "ws-1",
                "task.create",
                "req-1",
                &rules,
                &hash,
                &LedgerProvenance::default(),
                &serde_json::json!({ "ok": false }),
            )
            .unwrap_err();
        assert_eq!(err.code, ERR_LEDGER_CONFLICT);
        let _ = tx2.rollback();
    }

    #[test]
    fn missing_ruleset_fails_closed() {
        let conn = Connection::open_in_memory().unwrap();
        // 建 task_operation_ledger 表但不播种 rule row（模拟 rule row 被误删）
        conn.execute_batch(
            "CREATE TABLE task_operation_ledger (
                workspace_instance_id TEXT NOT NULL,
                method TEXT NOT NULL,
                request_id TEXT NOT NULL,
                params_canonicalization_version TEXT NOT NULL,
                params_canonicalization_rules_hash TEXT NOT NULL,
                canonical_params_hash TEXT NOT NULL,
                workspace_id INTEGER NULL,
                task_id TEXT NULL,
                role_contract_revision_id TEXT NULL,
                role_contract_hash TEXT NULL,
                role_contract_canonicalization_version TEXT NULL,
                role_contract_canonicalization_rules_hash TEXT NULL,
                response_or_error_json TEXT NOT NULL,
                authoritative_created_at TEXT NOT NULL,
                PRIMARY KEY(workspace_instance_id, method, request_id)
            )",
        )
        .unwrap();
        let err = OperationStore
            .dedupe(
                &conn,
                "ws-1",
                "task.create",
                "req-1",
                &serde_json::json!({ "task_id": "T-1" }),
            )
            .unwrap_err();
        assert_eq!(err.code, ERR_OPERATION_C14N_UNAVAILABLE);
    }

    #[test]
    fn seeded_rule_hash_verifies() {
        let conn = migrated_db();
        let rules = rules_from_current(&conn);
        assert_eq!(rules.canonicalization_version, "operation-params-c14n/v1");
        assert_eq!(
            rules.rules_hash,
            "sha256:8b7a902fd1463c8a79a6b687161b6be471ad62877d4920f4e874e11bbb2e495c"
        );
        // 校验函数通过（不 panic 且返回 Ok）
        verify_rules_hash(&rules).unwrap();
    }

    #[test]
    fn tampered_rule_row_fails_closed() {
        let conn = migrated_db();
        // 篡改 seed row 的 payload（hash 不变）→ 读取时 fail closed
        conn.execute(
            "UPDATE canonicalization_rule_sets SET rules_payload_json = '{\"tampered\":true}' \
             WHERE domain = 'operation_params'",
            [],
        )
        .unwrap();
        let err = OperationStore
            .dedupe(
                &conn,
                "ws-1",
                "task.create",
                "req-1",
                &serde_json::json!({ "task_id": "T-1" }),
            )
            .unwrap_err();
        assert_eq!(err.code, ERR_OPERATION_C14N_UNAVAILABLE);
        assert!(err.message.contains("rules hash 失配"));
    }
}
