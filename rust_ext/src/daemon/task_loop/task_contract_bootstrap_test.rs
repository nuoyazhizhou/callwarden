use rusqlite::{params, Connection};

use crate::sqlite_query::migrate_connection;
use super::create::{create_task, CreateTaskInput, LedgerKey as CreateLedgerKey, WorkspaceCaptureInput};
use super::task_contract_bootstrap::{bootstrap_task_governance_contracts, BootstrapInput, ERR_BOOTSTRAP_NOT_EMPTY};
use super::types::FrozenAuthorityInput;

fn fresh_db() -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    migrate_connection(&conn).unwrap();
    conn.execute("INSERT INTO workspaces (id,name,root_path,created_at) VALUES (1,'ws-1','/tmp/ws-1',0)", []).unwrap();
    conn
}

fn seed_task(conn: &mut Connection, task_id: &str) {
    let ws = WorkspaceCaptureInput {
        workspace_id: 1, daemon_workspace_id: 1, workspace_instance_id: "ws-inst-1".to_string(),
        client_view_root_hash: "view".to_string(), host_real_root_hash: "host".to_string(),
        workspace_manifest_payload_json: "{}".to_string(), workspace_manifest_hash: "manifest".to_string(),
        created_by: "seed".to_string(),
    };
    create_task(conn, &FrozenAuthorityInput::default(), &CreateLedgerKey {
        workspace_instance_id: "ws-inst-1".to_string(), method: "task.create".to_string(), request_id: format!("create-{task_id}"),
    }, &CreateTaskInput { task_id: task_id.to_string(), title: "seed".to_string(), description: "seed".to_string(), creator: "seed".to_string() }, &ws).unwrap();
    for role in ["executor", "reviewer", "adjudicator"] {
        conn.execute("INSERT INTO role_contracts (contract_id,task_id,step_id,role,skill_id,skill_version,prompt_template_id,prompt_hash,allowed_paths,forbidden_paths,commands,acceptance_checks,required_evidence,handoff_to,independence,revision,is_current,created_at,created_by) VALUES (?1,?2,'',?3,'none','','pt','ph','[\"src/\"]','[\"target/\"]','[\"cargo test\"]','[\"pass\"]','[\"evidence\"]','next','{}',1,1,0,'seed')", params![format!("legacy-{role}"),task_id,role]).unwrap();
    }
    conn.execute("INSERT INTO task_steps (id,task_id,step_index,action,status,result,created_at) VALUES ('s-1',?1,0,'implement','pending','',0)", [task_id]).unwrap();
}

fn envelope(task_id: &str) -> serde_json::Value {
    serde_json::json!({
        "contract_id": format!("tc-{task_id}"), "revision": 1, "profile": "design",
        "objective": {"statement":"bootstrap"}, "interfaces": {},
        "allowed_edit_scope": {"files":[],"symbols":[],"generated_from":[]},
        "acceptance_clauses": [], "risks": [], "rollback": {}, "dependencies": {}
    })
}

#[test]
fn bootstrap_appends_complete_governance_projection_without_task_mutation() {
    let mut conn = fresh_db();
    seed_task(&mut conn, "t-1");
    let tx = conn.unchecked_transaction().unwrap();
    let response = bootstrap_task_governance_contracts(&tx, &BootstrapInput { task_id:"t-1".to_string(), envelope: envelope("t-1"), created_by:"adj".to_string(), role_contract_source:"legacy".to_string() }, 1).unwrap();
    tx.commit().unwrap();
    assert_eq!(response["contract_revision"], serde_json::json!(1));
    let task_status: String = conn.query_row("SELECT status FROM tasks WHERE id='t-1'", [], |r| r.get(0)).unwrap();
    assert_eq!(task_status, "open");
    for (table, expected) in [("task_contract_revisions",1_i64),("role_contract_lineages",3),("role_contract_revisions",3),("task_step_role_contract_bindings",1)] {
        let count: i64 = conn.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r.get(0)).unwrap();
        assert_eq!(count, expected, "{table}");
    }
}

#[test]
fn bootstrap_rejects_any_existing_projection_append_only() {
    let mut conn = fresh_db();
    seed_task(&mut conn, "t-1");
    let tx = conn.unchecked_transaction().unwrap();
    bootstrap_task_governance_contracts(&tx, &BootstrapInput { task_id:"t-1".to_string(), envelope: envelope("t-1"), created_by:"adj".to_string(), role_contract_source:"legacy".to_string() }, 1).unwrap();
    tx.commit().unwrap();
    let tx = conn.unchecked_transaction().unwrap();
    let err = bootstrap_task_governance_contracts(&tx, &BootstrapInput { task_id:"t-1".to_string(), envelope: envelope("t-1"), created_by:"adj".to_string(), role_contract_source:"legacy".to_string() }, 1).unwrap_err();
    assert_eq!(err.code, ERR_BOOTSTRAP_NOT_EMPTY);
}

fn seed_task_no_roles(conn: &mut Connection, task_id: &str) {
    // 与 seed_task 相同但**不插入** role_contracts（模拟 role_contracts=0 的历史任务）
    let ws = WorkspaceCaptureInput {
        workspace_id: 1, daemon_workspace_id: 1, workspace_instance_id: "ws-inst-1".to_string(),
        client_view_root_hash: "view".to_string(), host_real_root_hash: "host".to_string(),
        workspace_manifest_payload_json: "{}".to_string(), workspace_manifest_hash: "manifest".to_string(),
        created_by: "seed".to_string(),
    };
    create_task(conn, &FrozenAuthorityInput::default(), &CreateLedgerKey {
        workspace_instance_id: "ws-inst-1".to_string(), method: "task.create".to_string(), request_id: format!("create-{task_id}"),
    }, &CreateTaskInput { task_id: task_id.to_string(), title: "seed".to_string(), description: "seed".to_string(), creator: "seed".to_string() }, &ws).unwrap();
    conn.execute("INSERT INTO task_steps (id,task_id,step_index,action,status,result,created_at) VALUES ('s-1',?1,0,'implement','pending','',0)", [task_id]).unwrap();
}

#[test]
fn bootstrap_allowlist_derives_roles_without_legacy_role_contracts() {
    // allowlist 任务（T-1787203937193-0993d120）：role_contracts=0 → 默认三角色模板派生
    let allowlisted = "T-1787203937193-0993d120";
    let mut conn = fresh_db();
    seed_task_no_roles(&mut conn, allowlisted);
    let tx = conn.unchecked_transaction().unwrap();
    let response = bootstrap_task_governance_contracts(&tx, &BootstrapInput {
        task_id: allowlisted.to_string(), envelope: envelope(allowlisted), created_by: "adj".to_string(),
        role_contract_source: "allowlist".to_string(),
    }, 1).unwrap();
    tx.commit().unwrap();
    assert_eq!(response["contract_revision"], serde_json::json!(1));
    let lineages: i64 = conn.query_row("SELECT COUNT(*) FROM role_contract_lineages WHERE task_id=?1", [allowlisted], |r| r.get(0)).unwrap();
    assert_eq!(lineages, 3, "allowlist 模式必须建立三角色 lineage");
    // source_provenance 可审计标注
    let payload: String = conn.query_row(
        "SELECT canonical_payload_json FROM role_contract_revisions r JOIN role_contract_lineages l ON l.role_contract_lineage_id=r.role_contract_lineage_id WHERE l.task_id=?1 AND l.role='executor' LIMIT 1",
        [allowlisted], |r| r.get(0)).unwrap();
    assert!(payload.contains("allowlist_default_template:v1"), "allowlist 模板必须标注来源: {payload}");
}

#[test]
fn bootstrap_allowlist_rejects_non_allowlisted_task() {
    // 非 allowlist 任务 → allowlist 模式必须 governance_blocked（ERR_BOOTSTRAP_ROLE_SOURCE）
    let not_allowlisted = "t-not-allowlisted";
    let mut conn = fresh_db();
    seed_task_no_roles(&mut conn, not_allowlisted);
    let tx = conn.unchecked_transaction().unwrap();
    let err = bootstrap_task_governance_contracts(&tx, &BootstrapInput {
        task_id: not_allowlisted.to_string(), envelope: envelope(not_allowlisted), created_by: "adj".to_string(),
        role_contract_source: "allowlist".to_string(),
    }, 1).unwrap_err();
    assert_eq!(err.code, super::task_contract_bootstrap::ERR_BOOTSTRAP_ROLE_SOURCE);
    // 拒绝路径无状态变化：无 lineage 写入
    let lineages: i64 = conn.query_row("SELECT COUNT(*) FROM role_contract_lineages", [], |r| r.get(0)).unwrap();
    assert_eq!(lineages, 0, "拒绝路径不得写入 lineage");
}
