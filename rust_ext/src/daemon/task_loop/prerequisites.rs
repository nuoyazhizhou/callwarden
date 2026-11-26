//! 前置条件告知（prerequisite gate）— 分层前置满足判定引擎 + capability 声明。
//!
//! 设计基线：`.design/preq/`（冻结提交 8b1a1e3，R3）；本模块实现
//! REQ-PREQ-04（判定引擎）与 REQ-PREQ-03（capability 声明）。
//! 数据源全部 reuse 存量依赖建模，零 schema 变更：
//! - `task_dependencies`（`db/schema.py:1433`）：四类依赖声明 + `is_informational`；
//! - `tasks.status`：provider lifecycle（open/in_progress/review/applied/closed/reverted）；
//! - `artifact_identities.freshness_status`（`db/schema.py:1455`）：producing/fresh/stale；
//! - `interface_provider_selections`（`db/schema.py:1492`）：interface 的 provider 选择。
//!
//! 分层判定规则（用户已确认，requirements-brief §4.4）：
//! - `requires_existing`：provider lifecycle ∈ {applied, closed} 即满足，否则
//!   `provider_not_completed`（provider 行缺失 / 空 target_task_id 同样未满足）；
//! - `requires_artifact`：artifact 行缺失 → `artifact_unresolved`（fail-closed）；
//!   lifecycle 未完成 → `provider_not_completed`；freshness != fresh（stale/producing）
//!   → `artifact_stale`；
//! - `requires_interface`：`interface_provider_selections` 存在该 interface 的
//!   selection 行即满足（表无状态列，存在性即生效）；无 → `interface_unselected`；
//! - 未知 dep_type → 一律计为 unmet（`unknown_dep_type`），不静默放行。
//!
//! 纯只读引擎：无任何写路径；DB 读取失败 → `DaemonRpcError::internal_error`
//! 向上抛，由调用方（claim 门禁 / 投影）决定回滚或整体失败，本引擎不静默放行。
//! task 不存在或无依赖声明 → 空结果、`all_satisfied=true`（不报错）。

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};

use crate::daemon::dispatch::DaemonRpcError;

/// REQ-PREQ-03：正式 capability 名称。
pub const CAPABILITY_NAME: &str = "prerequisite_gate_v1";
/// capability 投影自身的 schema 版本（起始值，对齐 task_prompt 惯例）。
pub const CAPABILITY_SCHEMA_VERSION: &str = "prerequisite_capability_v1";

/// provider lifecycle 视为「已完成」的状态集合（异常矩阵 #7：reverted → 未满足）。
const COMPLETED_LIFECYCLE: [&str; 2] = ["applied", "closed"];

/// 单条前置依赖判定结果（仅未满足项进入输出；满足项不产生条目）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PrerequisiteItem {
    /// provider 任务 ID（requires_interface 已选择时为所选 provider；
    /// 无法定位 provider 时为空串）。
    pub provider_task_id: String,
    /// provider 当前 lifecycle（open/in_progress/review/applied/closed/reverted；
    /// 无 provider 行时为空串）。
    pub lifecycle_status: String,
    /// 依赖类型：requires_existing / requires_artifact / requires_interface /
    /// 未知类型原样透传。
    pub dep_type: String,
    /// 未满足原因：provider_not_completed / artifact_stale / artifact_unresolved /
    /// interface_unselected / unknown_dep_type。
    pub unmet_reason: String,
    /// 仅 requires_artifact 携带：artifact 当前 freshness（行缺失时为 None）。
    pub freshness_status: Option<String>,
}

/// 整体判定结果：`all_satisfied` = `hard_unmet` 为空（信息性 warnings 不影响）。
#[derive(Debug, Clone)]
pub(crate) struct PrerequisiteEvaluation {
    /// 未满足的硬依赖（is_hard=1）——claim 门禁据此阻断，投影据此填充
    /// `prerequisites` 字段。
    pub hard_unmet: Vec<PrerequisiteItem>,
    /// 未满足的信息性依赖（is_hard=0）——仅告警不阻断。
    pub warnings: Vec<PrerequisiteItem>,
    /// hard_unmet 为空即 true。
    pub all_satisfied: bool,
}

/// REQ-PREQ-03：v1 无构建资产，随 daemon 交付即启用；后续接运行时开关只改此函数。
pub fn capability_enabled() -> bool {
    true
}

/// health/capability 投影（固定三键，模式对齐 `task_prompt/handler.rs:125-142`）。
pub fn capability_projection() -> Value {
    json!({
        "name": CAPABILITY_NAME,
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "enabled": capability_enabled(),
    })
}

/// 分层前置满足判定引擎（REQ-PREQ-04，纯只读）。
///
/// - `workspace_id`：来自 claim 路径已解析的 workspace binding 或投影路径的
///   task binding；查询按 workspace_id 过滤，不跨区泄露。
/// - `task_id`：consumer 任务主键；task 不存在或无依赖声明 → 空结果。
/// - 依赖声明源为 `task_dependencies`（四类声明 + `is_informational`，
///   `db/schema.py:1433`）；`requires_existing` 与未知类型只存在于声明表
///   （`dependency_edges` 仅物化 artifact/interface 硬边，见 `db/schema.py:1505`）。
pub(crate) fn evaluate_task_prerequisites(
    conn: &Connection,
    workspace_id: i64,
    task_id: &str,
) -> Result<PrerequisiteEvaluation, DaemonRpcError> {
    let declarations = read_declarations(conn, workspace_id, task_id)?;
    let mut hard_unmet: Vec<PrerequisiteItem> = Vec::new();
    let mut warnings: Vec<PrerequisiteItem> = Vec::new();

    for (dep_type, target_ref, target_task_id, is_hard) in declarations {
        let item = match dep_type.as_str() {
            "requires_existing" => evaluate_requires_existing(conn, &target_task_id)?,
            "requires_artifact" => {
                evaluate_requires_artifact(conn, workspace_id, &target_task_id, &target_ref)?
            }
            "requires_interface" => {
                evaluate_requires_interface(
                    conn,
                    workspace_id,
                    task_id,
                    &target_ref,
                    &target_task_id,
                )?
            }
            // provides_interface 是 provider 侧声明，不构成 consumer 前置，跳过。
            "provides_interface" => continue,
            // 未知 dep_type 一律 fail-closed 计为 unmet，不静默放行。
            _ => PrerequisiteItem {
                provider_task_id: target_task_id,
                lifecycle_status: String::new(),
                dep_type,
                unmet_reason: "unknown_dep_type".to_string(),
                freshness_status: None,
            },
        };
        if item.unmet_reason.is_empty() {
            continue;
        }
        if is_hard {
            hard_unmet.push(item);
        } else {
            warnings.push(item);
        }
    }

    Ok(PrerequisiteEvaluation {
        all_satisfied: hard_unmet.is_empty(),
        hard_unmet,
        warnings,
    })
}

/// 依赖声明行：(dependency_type, target_ref, target_task_id, is_hard)。
type Declaration = (String, String, String, bool);

/// 读取 consumer 的全部依赖声明（按声明导入顺序，`task_dependencies` 表）。
fn read_declarations(
    conn: &Connection,
    workspace_id: i64,
    task_id: &str,
) -> Result<Vec<Declaration>, DaemonRpcError> {
    let mut stmt = conn
        .prepare(
            "SELECT dependency_type, target_ref, target_task_id, is_informational \
             FROM task_dependencies \
             WHERE workspace_id = ?1 AND task_id = ?2 \
             ORDER BY id",
        )
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("task_dependencies 读取失败: {e}"))
        })?;
    let rows = stmt
        .query_map(params![workspace_id, task_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
            ))
        })
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("task_dependencies 读取失败: {e}"))
        })?;
    let mut out: Vec<Declaration> = Vec::new();
    for row in rows {
        let (dep_type, target_ref, target_task_id, is_informational) = row.map_err(|e| {
            DaemonRpcError::internal_error(format!("task_dependencies 行读取失败: {e}"))
        })?;
        out.push((dep_type, target_ref, target_task_id, is_informational == 0));
    }
    Ok(out)
}

/// requires_existing：provider lifecycle ∈ {applied, closed} 即满足。
fn evaluate_requires_existing(
    conn: &Connection,
    provider_task_id: &str,
) -> Result<PrerequisiteItem, DaemonRpcError> {
    let lifecycle_status = read_task_status(conn, provider_task_id)?;
    let satisfied = COMPLETED_LIFECYCLE.contains(&lifecycle_status.as_str());
    Ok(PrerequisiteItem {
        provider_task_id: provider_task_id.to_string(),
        lifecycle_status,
        dep_type: "requires_existing".to_string(),
        unmet_reason: if satisfied {
            String::new()
        } else {
            "provider_not_completed".to_string()
        },
        freshness_status: None,
    })
}

/// requires_artifact：artifact 行缺失 → `artifact_unresolved`（fail-closed）；
/// lifecycle 未完成 → `provider_not_completed`；freshness != fresh → `artifact_stale`。
///
/// freshness 关联语义对齐 Python `get_artifact_freshness`
/// （`db/db_task_dependencies.py:179`）：按 (workspace, provider, artifact_ref)
/// 精确匹配，`produced_at` 最新优先。
fn evaluate_requires_artifact(
    conn: &Connection,
    workspace_id: i64,
    provider_task_id: &str,
    artifact_ref: &str,
) -> Result<PrerequisiteItem, DaemonRpcError> {
    let lifecycle_status = read_task_status(conn, provider_task_id)?;
    let freshness_status: Option<String> = conn
        .query_row(
            "SELECT freshness_status FROM artifact_identities \
             WHERE workspace_id = ?1 AND task_id = ?2 AND artifact_ref = ?3 \
             ORDER BY produced_at DESC LIMIT 1",
            params![workspace_id, provider_task_id, artifact_ref],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("artifact_identities 读取失败: {e}"))
        })?;

    let unmet_reason = match &freshness_status {
        // 行缺失：fail-closed（provider 已完成但从未登记 artifact，或声明悬空）。
        None => "artifact_unresolved".to_string(),
        Some(freshness) => {
            if !COMPLETED_LIFECYCLE.contains(&lifecycle_status.as_str()) {
                "provider_not_completed".to_string()
            } else if freshness != "fresh" {
                "artifact_stale".to_string()
            } else {
                String::new()
            }
        }
    };
    Ok(PrerequisiteItem {
        provider_task_id: provider_task_id.to_string(),
        lifecycle_status,
        dep_type: "requires_artifact".to_string(),
        unmet_reason,
        freshness_status,
    })
}

/// requires_interface：`interface_provider_selections` 存在该 interface 的
/// selection 行即满足（active 判定 = 行存在，表无状态列）；无 → `interface_unselected`。
fn evaluate_requires_interface(
    conn: &Connection,
    workspace_id: i64,
    consumer_task_id: &str,
    interface_name: &str,
    fallback_provider_task_id: &str,
) -> Result<PrerequisiteItem, DaemonRpcError> {
    let selected_provider: Option<String> = conn
        .query_row(
            "SELECT selected_provider_task_id FROM interface_provider_selections \
             WHERE workspace_id = ?1 AND consumer_task_id = ?2 AND interface_name = ?3 \
             ORDER BY selected_at DESC LIMIT 1",
            params![workspace_id, consumer_task_id, interface_name],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| {
            DaemonRpcError::internal_error(format!(
                "interface_provider_selections 读取失败: {e}"
            ))
        })?;

    let has_selection = selected_provider.is_some();
    let provider_task_id =
        selected_provider.unwrap_or_else(|| fallback_provider_task_id.to_string());
    let lifecycle_status = read_task_status(conn, &provider_task_id)?;
    let unmet_reason = if has_selection {
        String::new()
    } else {
        "interface_unselected".to_string()
    };
    Ok(PrerequisiteItem {
        provider_task_id,
        lifecycle_status,
        dep_type: "requires_interface".to_string(),
        unmet_reason,
        freshness_status: None,
    })
}

/// 读 provider 任务 lifecycle；任务行缺失或 provider 为空 → 空串（视为未满足）。
fn read_task_status(conn: &Connection, task_id: &str) -> Result<String, DaemonRpcError> {
    if task_id.is_empty() {
        return Ok(String::new());
    }
    let status: Option<String> = conn
        .query_row(
            "SELECT status FROM tasks WHERE id = ?1",
            [task_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("tasks.status 读取失败: {e}")))?;
    Ok(status.unwrap_or_default())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sqlite_query::migrate_connection;
    use std::sync::atomic::{AtomicU64, Ordering};

    fn fresh_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        migrate_connection(&conn).expect("migration 应成功");
        conn.execute(
            "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (?1, ?2, ?3, 0.0)",
            params![1, "ws-1", "/tmp/ws-1"],
        )
        .unwrap();
        conn
    }

    fn insert_task(conn: &Connection, task_id: &str, status: &str) {
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, updated_at) \
             VALUES (?1, ?2, ?3, 0.0, 0.0)",
            params![task_id, format!("task-{task_id}"), status],
        )
        .unwrap();
    }

    fn insert_declaration(
        conn: &Connection,
        consumer: &str,
        dep_type: &str,
        target_ref: &str,
        target_task_id: &str,
        is_informational: bool,
    ) {
        conn.execute(
            "INSERT INTO task_dependencies \
                 (workspace_id, task_id, contract_id, contract_revision, \
                  dependency_type, target_ref, target_task_id, is_informational, declared_at) \
             VALUES (1, ?1, 'c-1', 1, ?2, ?3, ?4, ?5, 0.0)",
            params![
                consumer,
                dep_type,
                target_ref,
                target_task_id,
                if is_informational { 1 } else { 0 }
            ],
        )
        .unwrap();
    }

    fn insert_artifact(conn: &Connection, provider: &str, artifact_ref: &str, freshness: &str) {
        static SEQ: AtomicU64 = AtomicU64::new(1);
        let n = SEQ.fetch_add(1, Ordering::SeqCst);
        conn.execute(
            "INSERT INTO artifact_identities \
                 (workspace_id, artifact_id, task_id, contract_id, contract_revision, \
                  artifact_type, artifact_ref, artifact_hash, freshness_status, \
                  produced_at, workspace_snapshot_id) \
             VALUES (1, ?1, ?2, 'c-1', 1, 'file', ?3, '', ?4, 0.0, '')",
            params![format!("ART-test-{n}"), provider, artifact_ref, freshness],
        )
        .unwrap();
    }

    fn insert_interface_selection(
        conn: &Connection,
        consumer: &str,
        interface_name: &str,
        provider: &str,
    ) {
        conn.execute(
            "INSERT INTO interface_provider_selections \
                 (workspace_id, consumer_task_id, contract_id, contract_revision, \
                  interface_name, selected_provider_task_id, selected_at) \
             VALUES (1, ?1, 'c-1', 1, ?2, ?3, 0.0)",
            params![consumer, interface_name, provider],
        )
        .unwrap();
    }

    #[test]
    fn capability_projection_shape() {
        let projection = capability_projection();
        assert_eq!(projection["name"], "prerequisite_gate_v1");
        assert_eq!(projection["schema_version"], "prerequisite_capability_v1");
        assert_eq!(projection["enabled"], true);
        assert!(capability_enabled());
    }

    #[test]
    fn no_declarations_is_empty_and_satisfied() {
        let conn = fresh_db();
        insert_task(&conn, "T-C", "open");
        let evaluation = evaluate_task_prerequisites(&conn, 1, "T-C").unwrap();
        assert!(evaluation.hard_unmet.is_empty());
        assert!(evaluation.warnings.is_empty());
        assert!(evaluation.all_satisfied);
    }

    #[test]
    fn nonexistent_task_is_empty_and_satisfied() {
        let conn = fresh_db();
        let evaluation = evaluate_task_prerequisites(&conn, 1, "T-NOPE").unwrap();
        assert!(evaluation.hard_unmet.is_empty());
        assert!(evaluation.all_satisfied);
    }

    #[test]
    fn requires_existing_provider_open_is_unmet() {
        let conn = fresh_db();
        insert_task(&conn, "T-C", "open");
        insert_task(&conn, "T-P", "open");
        insert_declaration(&conn, "T-C", "requires_existing", "sym.a", "T-P", false);
        let evaluation = evaluate_task_prerequisites(&conn, 1, "T-C").unwrap();
        assert!(!evaluation.all_satisfied);
        assert_eq!(evaluation.hard_unmet.len(), 1);
        let item = &evaluation.hard_unmet[0];
        assert_eq!(item.provider_task_id, "T-P");
        assert_eq!(item.lifecycle_status, "open");
        assert_eq!(item.dep_type, "requires_existing");
        assert_eq!(item.unmet_reason, "provider_not_completed");
        assert_eq!(item.freshness_status, None);
    }

    #[test]
    fn requires_existing_provider_applied_or_closed_is_satisfied() {
        let conn = fresh_db();
        insert_task(&conn, "T-C", "open");
        insert_task(&conn, "T-P1", "applied");
        insert_task(&conn, "T-P2", "closed");
        insert_declaration(&conn, "T-C", "requires_existing", "sym.a", "T-P1", false);
        insert_declaration(&conn, "T-C", "requires_existing", "sym.b", "T-P2", false);
        let evaluation = evaluate_task_prerequisites(&conn, 1, "T-C").unwrap();
        assert!(evaluation.all_satisfied);
        assert!(evaluation.hard_unmet.is_empty());
        assert!(evaluation.warnings.is_empty());
    }

    #[test]
    fn requires_existing_provider_reverted_or_missing_is_unmet() {
        let conn = fresh_db();
        insert_task(&conn, "T-C", "open");
        insert_task(&conn, "T-P1", "reverted");
        insert_declaration(&conn, "T-C", "requires_existing", "sym.a", "T-P1", false);
        // provider 行整体缺失（悬空声明）同样 fail-closed。
        insert_declaration(&conn, "T-C", "requires_existing", "sym.b", "T-GONE", false);
        let evaluation = evaluate_task_prerequisites(&conn, 1, "T-C").unwrap();
        assert!(!evaluation.all_satisfied);
        assert_eq!(evaluation.hard_unmet.len(), 2);
        assert!(evaluation
            .hard_unmet
            .iter()
            .all(|i| i.unmet_reason == "provider_not_completed"));
        let gone = evaluation
            .hard_unmet
            .iter()
            .find(|i| i.provider_task_id == "T-GONE")
            .unwrap();
        assert_eq!(gone.lifecycle_status, "");
    }

    #[test]
    fn requires_artifact_stale_or_producing_is_unmet() {
        let conn = fresh_db();
        insert_task(&conn, "T-C", "open");
        insert_task(&conn, "T-P1", "closed");
        insert_task(&conn, "T-P2", "applied");
        insert_artifact(&conn, "T-P1", "art.a", "stale");
        insert_artifact(&conn, "T-P2", "art.b", "producing");
        insert_declaration(&conn, "T-C", "requires_artifact", "art.a", "T-P1", false);
        insert_declaration(&conn, "T-C", "requires_artifact", "art.b", "T-P2", false);
        let evaluation = evaluate_task_prerequisites(&conn, 1, "T-C").unwrap();
        assert!(!evaluation.all_satisfied);
        assert_eq!(evaluation.hard_unmet.len(), 2);
        assert!(evaluation
            .hard_unmet
            .iter()
            .all(|i| i.unmet_reason == "artifact_stale"));
        let stale = evaluation
            .hard_unmet
            .iter()
            .find(|i| i.provider_task_id == "T-P1")
            .unwrap();
        assert_eq!(stale.freshness_status.as_deref(), Some("stale"));
    }

    #[test]
    fn requires_artifact_fresh_with_completed_provider_is_satisfied() {
        let conn = fresh_db();
        insert_task(&conn, "T-C", "open");
        insert_task(&conn, "T-P", "closed");
        insert_artifact(&conn, "T-P", "art.a", "fresh");
        insert_declaration(&conn, "T-C", "requires_artifact", "art.a", "T-P", false);
        let evaluation = evaluate_task_prerequisites(&conn, 1, "T-C").unwrap();
        assert!(evaluation.all_satisfied);
        assert!(evaluation.hard_unmet.is_empty());
    }

    #[test]
    fn requires_artifact_missing_row_is_unresolved_fail_closed() {
        let conn = fresh_db();
        insert_task(&conn, "T-C", "open");
        insert_task(&conn, "T-P", "closed");
        insert_declaration(&conn, "T-C", "requires_artifact", "art.missing", "T-P", false);
        let evaluation = evaluate_task_prerequisites(&conn, 1, "T-C").unwrap();
        assert!(!evaluation.all_satisfied);
        assert_eq!(evaluation.hard_unmet.len(), 1);
        let item = &evaluation.hard_unmet[0];
        assert_eq!(item.unmet_reason, "artifact_unresolved");
        assert_eq!(item.freshness_status, None);
        assert_eq!(item.lifecycle_status, "closed");
    }

    #[test]
    fn requires_artifact_open_provider_is_not_completed() {
        let conn = fresh_db();
        insert_task(&conn, "T-C", "open");
        insert_task(&conn, "T-P", "open");
        insert_artifact(&conn, "T-P", "art.a", "fresh");
        insert_declaration(&conn, "T-C", "requires_artifact", "art.a", "T-P", false);
        let evaluation = evaluate_task_prerequisites(&conn, 1, "T-C").unwrap();
        assert_eq!(evaluation.hard_unmet.len(), 1);
        assert_eq!(
            evaluation.hard_unmet[0].unmet_reason,
            "provider_not_completed"
        );
    }

    #[test]
    fn requires_interface_without_selection_is_unmet() {
        let conn = fresh_db();
        insert_task(&conn, "T-C", "open");
        insert_task(&conn, "T-P", "closed");
        insert_declaration(
            &conn,
            "T-C",
            "requires_interface",
            "auth.service.login",
            "T-P",
            false,
        );
        let evaluation = evaluate_task_prerequisites(&conn, 1, "T-C").unwrap();
        assert!(!evaluation.all_satisfied);
        assert_eq!(evaluation.hard_unmet.len(), 1);
        let item = &evaluation.hard_unmet[0];
        assert_eq!(item.unmet_reason, "interface_unselected");
        assert_eq!(item.dep_type, "requires_interface");
        assert_eq!(item.provider_task_id, "T-P");
    }

    #[test]
    fn requires_interface_with_selection_is_satisfied() {
        let conn = fresh_db();
        insert_task(&conn, "T-C", "open");
        insert_task(&conn, "T-P", "closed");
        insert_declaration(
            &conn,
            "T-C",
            "requires_interface",
            "auth.service.login",
            "",
            false,
        );
        insert_interface_selection(&conn, "T-C", "auth.service.login", "T-P");
        let evaluation = evaluate_task_prerequisites(&conn, 1, "T-C").unwrap();
        assert!(evaluation.all_satisfied);
        assert!(evaluation.hard_unmet.is_empty());
    }

    #[test]
    fn informational_unmet_goes_to_warnings_only() {
        let conn = fresh_db();
        insert_task(&conn, "T-C", "open");
        insert_task(&conn, "T-P", "open");
        insert_declaration(&conn, "T-C", "requires_existing", "sym.a", "T-P", true);
        let evaluation = evaluate_task_prerequisites(&conn, 1, "T-C").unwrap();
        // 信息性边不阻断：all_satisfied 仍为 true。
        assert!(evaluation.all_satisfied);
        assert!(evaluation.hard_unmet.is_empty());
        assert_eq!(evaluation.warnings.len(), 1);
        assert_eq!(evaluation.warnings[0].unmet_reason, "provider_not_completed");
    }

    #[test]
    fn unknown_dep_type_is_fail_closed() {
        let conn = fresh_db();
        insert_task(&conn, "T-C", "open");
        insert_declaration(&conn, "T-C", "requires_magic", "spell.a", "T-P", false);
        let evaluation = evaluate_task_prerequisites(&conn, 1, "T-C").unwrap();
        assert!(!evaluation.all_satisfied);
        assert_eq!(evaluation.hard_unmet.len(), 1);
        let item = &evaluation.hard_unmet[0];
        assert_eq!(item.dep_type, "requires_magic");
        assert_eq!(item.unmet_reason, "unknown_dep_type");
    }

    #[test]
    fn provides_interface_declaration_is_skipped() {
        let conn = fresh_db();
        insert_task(&conn, "T-C", "open");
        insert_declaration(&conn, "T-C", "provides_interface", "auth.service.login", "", false);
        let evaluation = evaluate_task_prerequisites(&conn, 1, "T-C").unwrap();
        assert!(evaluation.all_satisfied);
        assert!(evaluation.hard_unmet.is_empty());
        assert!(evaluation.warnings.is_empty());
    }

    #[test]
    fn mixed_hard_and_informational_partition() {
        let conn = fresh_db();
        insert_task(&conn, "T-C", "open");
        insert_task(&conn, "T-P1", "open");
        insert_task(&conn, "T-P2", "open");
        insert_declaration(&conn, "T-C", "requires_existing", "sym.a", "T-P1", false);
        insert_declaration(&conn, "T-C", "requires_existing", "sym.b", "T-P2", true);
        let evaluation = evaluate_task_prerequisites(&conn, 1, "T-C").unwrap();
        assert!(evaluation.all_satisfied == false);
        assert_eq!(evaluation.hard_unmet.len(), 1);
        assert_eq!(evaluation.hard_unmet[0].provider_task_id, "T-P1");
        assert_eq!(evaluation.warnings.len(), 1);
        assert_eq!(evaluation.warnings[0].provider_task_id, "T-P2");
    }

    #[test]
    fn cross_workspace_declarations_are_filtered() {
        let conn = fresh_db();
        insert_task(&conn, "T-C", "open");
        insert_task(&conn, "T-P", "open");
        // workspace 2 的声明不得影响 workspace 1 的判定。
        conn.execute(
            "INSERT INTO task_dependencies \
                 (workspace_id, task_id, contract_id, contract_revision, \
                  dependency_type, target_ref, target_task_id, is_informational, declared_at) \
             VALUES (2, 'T-C', 'c-1', 1, 'requires_existing', 'sym.a', 'T-P', 0, 0.0)",
            [],
        )
        .unwrap();
        let evaluation = evaluate_task_prerequisites(&conn, 1, "T-C").unwrap();
        assert!(evaluation.all_satisfied);
        assert!(evaluation.hard_unmet.is_empty());
    }
}
