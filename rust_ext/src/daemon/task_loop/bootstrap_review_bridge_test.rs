use rusqlite::Connection;

use crate::sqlite_query::migrate_connection;
use super::create::{create_task, CreateTaskInput, LedgerKey as CreateLedgerKey, WorkspaceCaptureInput};
use super::types::FrozenAuthorityInput;
use super::bootstrap_review_bridge::{
    bootstrap_executor_evidence, bootstrap_reviewer_pass, ExecutorEvidenceInput, ExecutorEvidenceStep,
    ReviewerPassInput, ERR_BRIDGE_NOT_EMPTY, ERR_BRIDGE_ROLE, ERR_BRIDGE_STATE,
    ERR_BRIDGE_NO_EXECUTOR_EVIDENCE, ERR_BRIDGE_INDEPENDENCE,
};

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
    conn.execute("INSERT INTO task_steps (id,task_id,step_index,action,status,result,created_at) VALUES ('s-1',?1,0,'implement','pending','',0)", [task_id]).unwrap();
}

fn ts() -> f64 { 1_700_000_000.0 }

#[test]
fn executor_evidence_then_reviewer_pass_two_stage_bridge() {
    let mut conn = fresh_db();
    seed_task(&mut conn, "t-1");
    let tx = conn.unchecked_transaction().unwrap();
    let exec = bootstrap_executor_evidence(&tx, &ExecutorEvidenceInput {
        task_id: "t-1".to_string(),
        steps: vec![ExecutorEvidenceStep { step_id: "s-1".to_string(), evidence_path: "e1.txt".to_string(), evidence_hash: "h1".to_string() }],
        created_by: "exec@a".to_string(),
    }, 1, ts()).unwrap();
    assert_eq!(exec["to_status"], serde_json::json!("review"));
    let status: String = tx.query_row("SELECT status FROM tasks WHERE id='t-1'", [], |r| r.get(0)).unwrap();
    assert_eq!(status, "review");

    let pass = bootstrap_reviewer_pass(&tx, &ReviewerPassInput {
        task_id: "t-1".to_string(), evidence_path: "r1.txt".to_string(), evidence_hash: "rh1".to_string(),
        created_by: "rev@b".to_string(), reviewer_agent_id: "rev@b".to_string(),
        reviewer_agent_instance_id: "inst-b".to_string(), reviewer_session_id: "sess-b".to_string(),
    }, "exec@a", "inst-a", "sess-a", "tokhash", ts() + 3600.0, 2, ts()).unwrap();
    assert_eq!(pass["status"], serde_json::json!("review"));
    assert!(pass["bootstrap_reviewer_lease_id"].as_str().unwrap().starts_with("brtl-"));
    // reviewer lease 已签发
    let lease_count: i64 = tx.query_row("SELECT COUNT(*) FROM task_leases WHERE task_id='t-1' AND role='reviewer' AND status='active'", [], |r| r.get(0)).unwrap();
    assert_eq!(lease_count, 1);
    tx.commit().unwrap();
}

#[test]
fn rejects_when_governance_projection_exists() {
    let mut conn = fresh_db();
    seed_task(&mut conn, "t-1");
    // 模拟既有 Task Contract revision（非空投影）
    conn.execute("INSERT INTO task_contract_revisions (contract_id,revision,contract_hash,profile,task_id,workspace_id,envelope_payload,created_at,created_by,normalization_version,normalization_rules_hash) VALUES ('c1',1,'h','code_change','t-1',1,'{}',0,'seed',1,'r')", []).unwrap();
    let tx = conn.unchecked_transaction().unwrap();
    let err = bootstrap_executor_evidence(&tx, &ExecutorEvidenceInput {
        task_id: "t-1".to_string(), steps: vec![ExecutorEvidenceStep { step_id: "s-1".to_string(), evidence_path: "e".to_string(), evidence_hash: "h".to_string() }],
        created_by: "exec".to_string(),
    }, 1, ts()).unwrap_err();
    assert_eq!(err.code, ERR_BRIDGE_NOT_EMPTY);
}

#[test]
fn executor_evidence_rejects_wrong_state() {
    let mut conn = fresh_db();
    seed_task(&mut conn, "t-1");
    // 先推到 review，再试图追加 executor evidence（应被状态门禁拒绝）
    conn.execute("UPDATE tasks SET status='review' WHERE id='t-1'", []).unwrap();
    let tx = conn.unchecked_transaction().unwrap();
    let err = bootstrap_executor_evidence(&tx, &ExecutorEvidenceInput {
        task_id: "t-1".to_string(), steps: vec![ExecutorEvidenceStep { step_id: "s-1".to_string(), evidence_path: "e".to_string(), evidence_hash: "h".to_string() }],
        created_by: "exec".to_string(),
    }, 1, ts()).unwrap_err();
    assert_eq!(err.code, ERR_BRIDGE_STATE);
}

#[test]
fn reviewer_pass_rejects_without_executor_evidence() {
    let mut conn = fresh_db();
    seed_task(&mut conn, "t-1");
    conn.execute("UPDATE tasks SET status='review' WHERE id='t-1'", []).unwrap();
    let tx = conn.unchecked_transaction().unwrap();
    let err = bootstrap_reviewer_pass(&tx, &ReviewerPassInput {
        task_id: "t-1".to_string(), evidence_path: "r".to_string(), evidence_hash: "rh".to_string(),
        created_by: "rev".to_string(), reviewer_agent_id: "rev".to_string(),
        reviewer_agent_instance_id: "i".to_string(), reviewer_session_id: "s".to_string(),
    }, "exec", "inst-a", "sess-a", "tokhash", ts() + 3600.0, 1, ts()).unwrap_err();
    assert_eq!(err.code, ERR_BRIDGE_NO_EXECUTOR_EVIDENCE);
}

#[test]
fn reviewer_pass_rejects_same_agent_as_executor() {
    let mut conn = fresh_db();
    seed_task(&mut conn, "t-1");
    let tx = conn.unchecked_transaction().unwrap();
    bootstrap_executor_evidence(&tx, &ExecutorEvidenceInput {
        task_id: "t-1".to_string(), steps: vec![ExecutorEvidenceStep { step_id: "s-1".to_string(), evidence_path: "e".to_string(), evidence_hash: "h".to_string() }],
        created_by: "same@x".to_string(),
    }, 1, ts()).unwrap();
    // reviewer 与 executor 同 agent_id → 独立性门禁拒绝
    let err = bootstrap_reviewer_pass(&tx, &ReviewerPassInput {
        task_id: "t-1".to_string(), evidence_path: "r".to_string(), evidence_hash: "rh".to_string(),
        created_by: "same@x".to_string(), reviewer_agent_id: "same@x".to_string(),
        reviewer_agent_instance_id: "i".to_string(), reviewer_session_id: "s".to_string(),
    }, "same@x", "inst-a", "sess-a", "tokhash", ts() + 3600.0, 2, ts()).unwrap_err();
    assert_eq!(err.code, ERR_BRIDGE_INDEPENDENCE);
}

#[test]
fn role_constants_present() {
    // 确保角色门禁常量可用（handler 层会用；此处仅静态校验导出）。
    assert_eq!(ERR_BRIDGE_ROLE, "E_BOOTSTRAP_BRIDGE_ROLE");
}
