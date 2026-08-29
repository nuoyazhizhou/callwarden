use rusqlite::{params, Connection};

use crate::sqlite_query::migrate_connection;
use super::create::{create_task, CreateTaskInput, LedgerKey as CreateLedgerKey, WorkspaceCaptureInput};
use super::task_contract_bootstrap::{bootstrap_task_governance_contracts, BootstrapInput};
use super::task_contract_revise::{append_task_contract_revision, ContractReviseInput, ERR_REVISE_CONFLICT, ERR_REVISE_INVALID};
use super::types::FrozenAuthorityInput;

fn fresh_db() -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    migrate_connection(&conn).unwrap();
    conn.execute("INSERT INTO workspaces (id,name,root_path,created_at) VALUES (1,'ws-1','/tmp/ws-1',0)", []).unwrap();
    conn
}

fn seed_task(conn: &mut Connection, task_id: &str) {
    let workspace = WorkspaceCaptureInput {
        workspace_id: 1,
        daemon_workspace_id: 1,
        workspace_instance_id: "ws-inst-1".to_string(),
        client_view_root_hash: "view".to_string(),
        host_real_root_hash: "host".to_string(),
        workspace_manifest_payload_json: "{}".to_string(),
        workspace_manifest_hash: "manifest".to_string(),
        created_by: "seed".to_string(),
    };
    create_task(
        conn,
        &FrozenAuthorityInput::default(),
        &CreateLedgerKey {
            workspace_instance_id: "ws-inst-1".to_string(),
            method: "task.create".to_string(),
            request_id: format!("create-{task_id}"),
        },
        &CreateTaskInput {
            task_id: task_id.to_string(),
            title: "seed".to_string(),
            description: "seed".to_string(),
            creator: "seed".to_string(),
        },
        &workspace,
    )
    .unwrap();
    for role in ["executor", "reviewer", "adjudicator"] {
        conn.execute(
            "INSERT INTO role_contracts (contract_id,task_id,step_id,role,skill_id,skill_version,prompt_template_id,prompt_hash,allowed_paths,forbidden_paths,commands,acceptance_checks,required_evidence,handoff_to,independence,revision,is_current,created_at,created_by) VALUES (?1,?2,'',?3,'none','','pt','ph','[\"src/\"]','[\"target/\"]','[\"cargo test\"]','[\"pass\"]','[\"evidence\"]','next','{}',1,1,0,'seed')",
            params![format!("legacy-{role}"), task_id, role],
        )
        .unwrap();
    }
    conn.execute(
        "INSERT INTO task_steps (id,task_id,step_index,action,status,result,created_at) VALUES ('s-1',?1,0,'implement','pending','',0)",
        [task_id],
    )
    .unwrap();
}

fn revision_one(task_id: &str) -> serde_json::Value {
    serde_json::json!({
        "contract_id": format!("tc-{task_id}"),
        "revision": 1,
        "profile": "code_change",
        "objective": "repair governance metadata without mutating history",
        "source_provenance": "P0-G deterministic test fixture",
        "interfaces": ["task.contract_revise"],
        "allowed_edit_scope": ["rust_ext/src/daemon/task_loop"],
        "acceptance_clauses": ["append only", "revision continuity"],
        "risks": ["stale writer"],
        "rollback": ["retain previous revision"],
        "dependencies": ["task_contract_revisions"],
    })
}

fn bootstrap(conn: &mut Connection, task_id: &str) -> String {
    seed_task(conn, task_id);
    let tx = conn.unchecked_transaction().unwrap();
    let result = bootstrap_task_governance_contracts(
        &tx,
        &BootstrapInput {
            task_id: task_id.to_string(),
            envelope: revision_one(task_id),
            created_by: "planner".to_string(),
            role_contract_source: "legacy".to_string(),
        },
        1,
    )
    .unwrap();
    tx.commit().unwrap();
    result["contract_hash"].as_str().unwrap().to_string()
}

fn revision_two(task_id: &str, prior_hash: &str) -> serde_json::Value {
    serde_json::json!({
        "contract_id": format!("tc-{task_id}"),
        "revision": 2,
        "supersedes_revision": 1,
        "supersedes_contract_hash": prior_hash,
        "profile": "code_change",
        "objective": "replace placeholder contract clauses with task-specific clauses",
        "source_provenance": "P0-G targeted regression fixture",
        "interfaces": ["task.contract_revise"],
        "allowed_edit_scope": ["rust_ext/src/daemon/task_loop/task_contract_revise.rs"],
        "acceptance_clauses": ["revision=previous+1", "previous hash anchored"],
        "risks": ["lost update"],
        "rollback": ["append a compensating revision"],
        "dependencies": ["reviewer lease", "operation ledger"],
    })
}

fn append(conn: &mut Connection, task_id: &str, envelope: serde_json::Value, expected_previous_hash: &str) -> Result<serde_json::Value, crate::daemon::dispatch::DaemonRpcError> {
    let tx = conn.unchecked_transaction().unwrap();
    let result = append_task_contract_revision(
        &tx,
        &ContractReviseInput {
            task_id: task_id.to_string(),
            envelope,
            expected_previous_hash: expected_previous_hash.to_string(),
            created_by: "adjudicator".to_string(),
        },
        1,
    );
    if result.is_ok() {
        tx.commit().unwrap();
    }
    result
}

#[test]
fn revision_two_appends_without_mutating_revision_one() {
    let mut conn = fresh_db();
    let prior_hash = bootstrap(&mut conn, "t-revise-ok");

    let result = append(
        &mut conn,
        "t-revise-ok",
        revision_two("t-revise-ok", &prior_hash),
        &prior_hash,
    )
    .unwrap();

    assert_eq!(result["previous_revision"], serde_json::json!(1));
    assert_eq!(result["revision"], serde_json::json!(2));
    assert_eq!(result["previous_contract_hash"], serde_json::json!(prior_hash));
    let rows: i64 = conn.query_row(
        "SELECT COUNT(*) FROM task_contract_revisions WHERE task_id='t-revise-ok'",
        [],
        |row| row.get(0),
    ).unwrap();
    assert_eq!(rows, 2);
    let revision_one_hash: String = conn.query_row(
        "SELECT contract_hash FROM task_contract_revisions WHERE task_id='t-revise-ok' AND revision=1",
        [],
        |row| row.get(0),
    ).unwrap();
    assert_eq!(revision_one_hash, prior_hash);
}

#[test]
fn revision_rejects_non_contiguous_revision() {
    let mut conn = fresh_db();
    let prior_hash = bootstrap(&mut conn, "t-revise-sequence");
    let mut invalid = revision_two("t-revise-sequence", &prior_hash);
    invalid["revision"] = serde_json::json!(3);

    let err = append(&mut conn, "t-revise-sequence", invalid, &prior_hash).unwrap_err();
    assert_eq!(err.code, ERR_REVISE_CONFLICT);
}

#[test]
fn revision_rejects_mismatched_expected_previous_hash() {
    let mut conn = fresh_db();
    let prior_hash = bootstrap(&mut conn, "t-revise-hash");
    let err = append(
        &mut conn,
        "t-revise-hash",
        revision_two("t-revise-hash", &prior_hash),
        "sha256:stale-writer",
    )
    .unwrap_err();
    assert_eq!(err.code, ERR_REVISE_CONFLICT);
}

#[test]
fn revision_rejects_missing_required_task_specific_fields_and_arrays() {
    for field in [
        "objective",
        "source_provenance",
        "interfaces",
        "allowed_edit_scope",
        "acceptance_clauses",
        "risks",
        "rollback",
        "dependencies",
    ] {
        let mut conn = fresh_db();
        let task_id = format!("t-revise-required-{field}");
        let prior_hash = bootstrap(&mut conn, &task_id);
        let mut invalid = revision_two(&task_id, &prior_hash);
        invalid.as_object_mut().unwrap().remove(field);
        let err = append(&mut conn, &task_id, invalid, &prior_hash).unwrap_err();
        assert_eq!(err.code, ERR_REVISE_INVALID, "field={field}");
    }
}

#[test]
fn revision_rejects_stringified_structured_arrays() {
    let mut conn = fresh_db();
    let prior_hash = bootstrap(&mut conn, "t-revise-array");
    let mut invalid = revision_two("t-revise-array", &prior_hash);
    invalid["allowed_edit_scope"] = serde_json::json!("[\"src\"]");
    let err = append(&mut conn, "t-revise-array", invalid, &prior_hash).unwrap_err();
    assert_eq!(err.code, ERR_REVISE_INVALID);
}
