//! Internal preflight 与 permit revalidation foundation（计划 §3.3、1D3A）。
//!
//! 1D3A 落地 `task-loop-schema-preflight/v1`：只读验证 schema 版本、1A–1F/2/3 的
//! 关键表与 `canonicalization_rule_sets` 关键 rule row 全部就绪后，才发布绑定
//! 当前 fingerprint 的 `InternalPreflightPermit`（§7 1D3A）。它不得修改 route、
//! 领域 handler、executor、operation store、raw parser、transport dedup 或 schema。
//!
//! 语义：
//! - preflight 通过只发布 Internal permit；**绝不**安装到 public route（§4.3），
//!   公共 discovery/public capability 在 1D3B 前保持 disabled。
//! - `verify_internal_permit` 是内部路由 admission 的最终复核：重算当前
//!   schema/rules fingerprint 与 daemon generation 逐项比对，任何失配一律
//!   `E_TASK_LOOP_CAPABILITY_DISABLED`（§6 行 630）。内存 permit 不是授权终点。
//! - `revalidate_public_permit` 依赖 0A/0B Capability Authority，cutover 前
//!   fail-closed（1D3B 落地）。

use rusqlite::Connection;
use sha2::{Digest, Sha256};

use crate::daemon::dispatch::DaemonRpcError;
use crate::sqlite_query::RUST_SCHEMA_VERSION;
use super::types::{FrozenAuthorityInput, InternalPreflightPermit, PublicPreflightPermit};

/// preflight 的冻结 c14n 版本（1D3A 独占实现）。
pub const INTERNAL_PREFLIGHT_C14N_VERSION: &str = "task-loop-schema-preflight/v1";

/// preflight 校验失败 / permit fingerprint 失配的统一稳定错误码。
const ERR_PREFLIGHT_DISABLED: &str = "E_TASK_LOOP_CAPABILITY_DISABLED";

/// 1D3A 验收的 schema 前置表（1D1 operation ledger、1A capture/binding）。
const REQUIRED_TABLES: &[&str] = &[
    "task_operation_ledger",
    "workspace_authority_captures",
    "task_workspace_bindings",
    "canonicalization_rule_sets",
];

/// `task-loop-schema-preflight/v1`：只读验证 schema/rule 就绪后发布绑定当前
/// fingerprint 的 `InternalPreflightPermit`。
///
/// 校验内容（全部只读，fail closed）：
/// - schema 实际版本必须等于 `RUST_SCHEMA_VERSION`（v53，1A 前置）；
/// - 1D1/1A 关键表必须全部存在；
/// - `workspace_capture` rule row（`workspace-capture-c14n/v1`）必须可读，其
///   rules_hash 即 permit 的 rules fingerprint。
///
/// 任一条件不满足都返回 `E_TASK_LOOP_CAPABILITY_DISABLED`，不发布 permit。
pub fn run_internal_preflight(
    conn: &Connection,
    daemon_generation: u64,
) -> Result<InternalPreflightPermit, DaemonRpcError> {
    let schema_fingerprint = compute_schema_fingerprint(conn)?;
    let rules_hash = read_workspace_capture_rules_hash(conn)?;
    Ok(InternalPreflightPermit {
        schema_fingerprint,
        rules_hash,
        daemon_generation,
    })
}

/// 内部路由 admission 的最终复核（§4.3 / §6 行 630）。
///
/// 在任务打开任何写 transaction 前重算当前 schema/rules fingerprint 与 daemon
/// generation，与 permit 绑定值逐项比对；任一失配/不可读即
/// `E_TASK_LOOP_CAPABILITY_DISABLED`。内存 permit 不是授权终点。
pub fn verify_internal_permit(
    conn: &Connection,
    frozen: &FrozenAuthorityInput,
    permit: &InternalPreflightPermit,
) -> Result<(), DaemonRpcError> {
    let schema_fingerprint = compute_schema_fingerprint(conn)?;
    let rules_hash = read_workspace_capture_rules_hash(conn)?;
    if schema_fingerprint != permit.schema_fingerprint
        || rules_hash != permit.rules_hash
        || frozen.daemon_generation != permit.daemon_generation
    {
        return Err(DaemonRpcError::new(
            ERR_PREFLIGHT_DISABLED,
            "Internal permit fingerprint 与当前 schema/rules/generation 不匹配",
        ));
    }
    Ok(())
}

/// 计算绑定当前 schema 的确定性 fingerprint：`schema_version=<v>;tables=<有序表集>` 的
/// SHA-256，表示为 `sha256:<hex>`。表缺失或版本失配一律 fail closed。
///
/// pub(crate)：1D3A 的 Internal permit 与 1D3B 的 promotion/revalidation 共用。
pub(crate) fn compute_schema_fingerprint(conn: &Connection) -> Result<String, DaemonRpcError> {
    let version: i64 = conn
        .query_row(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version",
            [],
            |row| row.get(0),
        )
        .map_err(|e| preflight_fail(format!("schema 版本不可读: {e}")))?;
    if version != RUST_SCHEMA_VERSION {
        return Err(preflight_fail(format!(
            "schema 版本不匹配：当前={version} 期望={RUST_SCHEMA_VERSION}"
        )));
    }

    for table in REQUIRED_TABLES {
        let exists: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?1",
                [table],
                |row| row.get(0),
            )
            .map_err(|e| preflight_fail(format!("表存在性校验失败（{table}）: {e}")))?;
        if exists == 0 {
            return Err(preflight_fail(format!("缺失前置表：{table}")));
        }
    }

    let payload = format!(
        "schema_version={version};tables={}",
        REQUIRED_TABLES.join(",")
    );
    let digest = Sha256::digest(payload.as_bytes());
    Ok(format!("sha256:{}", hex::encode(digest)))
}

/// 读取 `workspace_capture` rule row 的 rules_hash（1A 前置，§8.1.3）。
/// 缺 row 或不可读一律 fail closed（capability 未就绪）。
///
/// pub(crate)：1D3A 的 Internal permit 与 1D3B 的 promotion/revalidation 共用。
pub(crate) fn read_workspace_capture_rules_hash(conn: &Connection) -> Result<String, DaemonRpcError> {
    conn.query_row(
        "SELECT rules_hash FROM canonicalization_rule_sets \
         WHERE domain = 'workspace_capture' AND canonicalization_version = 'workspace-capture-c14n/v1'",
        [],
        |row| row.get(0),
    )
    .map_err(|e| preflight_fail(format!("workspace_capture rule row 不可用: {e}")))
}

fn preflight_fail(detail: impl Into<String>) -> DaemonRpcError {
    DaemonRpcError::new(
        ERR_PREFLIGHT_DISABLED,
        format!("internal preflight 未通过（{}）", detail.into()),
    )
}

/// 公共 permit 的最终复核（1D3B 落地；由 `capability_control.rs` 的 gate 在提交前
/// 调用，以及公共 route 每次 mutation 前的 admission 终点）。
///
/// 在打开任何 authority-store / task-DB 写 transaction 前重算当前 schema/rules
/// fingerprint，与 permit 绑定值逐项比对；任一失配/不可读即
/// `E_TASK_LOOP_CAPABILITY_DISABLED` 并清除对应内存 permit。内存 permit 不是授权
/// 终点。
///
/// 0A/0B 的完整 Capability Authority store（validity/expiry/revoke 查询）尚未落地：
/// 此处对 authority 做最小凭证校验（id/evidence 非空且与 frozen 一致），完整
/// store 接入后由 gate 在本函数外叠加 authority 有效性复核。
pub fn revalidate_public_permit(
    conn: &Connection,
    frozen: &FrozenAuthorityInput,
    permit: &PublicPreflightPermit,
) -> Result<(), DaemonRpcError> {
    // 1) schema/rules 绑定：permit fingerprint 必须与当前 task-DB 实际值一致。
    let schema_fingerprint = compute_schema_fingerprint(conn)?;
    let rules_hash = read_workspace_capture_rules_hash(conn)?;
    if permit.schema_fingerprint != schema_fingerprint || permit.rules_hash != rules_hash {
        return Err(preflight_fail(format!(
            "public permit fingerprint 与当前 schema/rules 不匹配 (permit schema={} current={})",
            permit.schema_fingerprint, schema_fingerprint
        )));
    }
    // 2) runtime / daemon generation 绑定（与冻结输入一致）。
    if frozen.daemon_generation != permit.daemon_generation {
        return Err(preflight_fail(format!(
            "public permit daemon generation 不匹配 (permit={} current={})",
            permit.daemon_generation, frozen.daemon_generation
        )));
    }
    if !frozen.runtime_binary_hash.is_empty()
        && frozen.runtime_binary_hash != permit.runtime_binary_hash
    {
        return Err(preflight_fail("public permit runtime binary hash 不匹配"));
    }
    // 3) authority / evidence 最小凭证（0A/0B 完整 authority store 落地后在此叠加）。
    if permit.authority_id.is_empty()
        || permit.evidence_id.is_empty()
        || permit.evidence_hash.is_empty()
    {
        return Err(preflight_fail("public permit 缺少 authority/evidence 最小凭证"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sqlite_query::migrate_connection;

    fn migrated_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        migrate_connection(&conn).expect("migration to v53");
        conn
    }

    #[test]
    fn preflight_passes_on_migrated_db() {
        let conn = migrated_db();
        let permit = run_internal_preflight(&conn, 7).expect("迁移后 preflight 应通过");
        assert_eq!(permit.daemon_generation, 7);
        assert!(permit.schema_fingerprint.starts_with("sha256:"));
        assert!(permit.rules_hash.starts_with("sha256:"));

        // 同库复核必须一致（确定性）。
        verify_internal_permit(&conn, &FrozenAuthorityInput { daemon_generation: 7, ..Default::default() }, &permit)
            .expect("同 fingerprint 复核应通过");
    }

    #[test]
    fn preflight_rejects_missing_table() {
        let conn = migrated_db();
        // 删除 1A 前置表之一 → preflight 必须 fail closed。
        conn.execute_batch("DROP TABLE task_workspace_bindings").unwrap();
        let err = run_internal_preflight(&conn, 1).expect_err("缺表必须拒绝");
        assert_eq!(err.code, ERR_PREFLIGHT_DISABLED);
    }

    #[test]
    fn preflight_rejects_wrong_schema_version() {
        let conn = migrated_db();
        conn.execute_batch("INSERT INTO schema_version (version, applied_at, description) VALUES (999, 0.0, 'x')")
            .unwrap();
        let err = run_internal_preflight(&conn, 1).expect_err("版本失配必须拒绝");
        assert_eq!(err.code, ERR_PREFLIGHT_DISABLED);
    }

    #[test]
    fn verify_rejects_generation_mismatch() {
        let conn = migrated_db();
        let permit = run_internal_preflight(&conn, 3).unwrap();
        let err = verify_internal_permit(
            &conn,
            &FrozenAuthorityInput { daemon_generation: 4, ..Default::default() },
            &permit,
        )
        .expect_err("daemon generation 失配必须拒绝");
        assert_eq!(err.code, ERR_PREFLIGHT_DISABLED);
    }
}
