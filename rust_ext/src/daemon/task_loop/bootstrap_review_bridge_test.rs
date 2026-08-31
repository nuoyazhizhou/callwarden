use rusqlite::Connection;

use crate::sqlite_query::migrate_connection;
use super::create::{create_task, CreateTaskInput, LedgerKey as CreateLedgerKey, WorkspaceCaptureInput};
use super::types::FrozenAuthorityInput;
use super::bootstrap_review_bridge::{
    bootstrap_executor_evidence, bootstrap_reviewer_pass, ExecutorEvidenceInput, ExecutorEvidenceStep,
    ReviewerPassInput, ERR_BRIDGE_INVALID, ERR_BRIDGE_NOT_EMPTY, ERR_BRIDGE_ROLE, ERR_BRIDGE_STATE,
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
    conn.execute("INSERT INTO task_steps (id,task_id,step_index,action,status,result,created_at) VALUES ('s-1',?1,0,'implement','done','',0)", [task_id]).unwrap();
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
        executor_agent_instance_id: "inst-a".to_string(),
    }, 1, ts()).unwrap();
    assert_eq!(exec["to_status"], serde_json::json!("review"));
    let status: String = tx.query_row("SELECT status FROM tasks WHERE id='t-1'", [], |r| r.get(0)).unwrap();
    assert_eq!(status, "review");

    let pass = bootstrap_reviewer_pass(&tx, &ReviewerPassInput {
        task_id: "t-1".to_string(), evidence_path: "r1.txt".to_string(), evidence_hash: "rh1".to_string(),
        created_by: "rev@b".to_string(), reviewer_agent_id: "rev@b".to_string(),
        reviewer_agent_instance_id: "inst-b".to_string(), reviewer_session_id: "sess-b".to_string(),
    }, "exec@a", "inst-a", "sess-a", 1, "model-b", "tokhash", ts() + 3600.0, 2, ts()).unwrap();
    assert_eq!(pass["status"], serde_json::json!("review"));
    assert!(pass["bootstrap_reviewer_lease_id"].as_str().unwrap().starts_with("brtl-"));
    assert_eq!(pass["fencing_counter"], serde_json::json!(1));
    // reviewer lease 已签发，且 workspace_id=1、model_id=model-b（P0F-R3 不再硬编码 model=''）
    let (lease_count, lease_model): (i64, String) = tx.query_row(
        "SELECT COUNT(*), model_id FROM task_leases WHERE task_id='t-1' AND role='reviewer' AND status='active' AND workspace_id=1",
        [], |r| Ok((r.get(0)?, r.get(1)?)),
    ).unwrap();
    assert_eq!(lease_count, 1);
    assert_eq!(lease_model, "model-b");
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
        created_by: "exec".to_string(), executor_agent_instance_id: "inst-a".to_string(),
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
        created_by: "exec".to_string(), executor_agent_instance_id: "inst-a".to_string(),
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
    }, "exec", "inst-a", "sess-a", 1, "model-b", "tokhash", ts() + 3600.0, 1, ts()).unwrap_err();
    assert_eq!(err.code, ERR_BRIDGE_NO_EXECUTOR_EVIDENCE);
}

#[test]
fn reviewer_pass_rejects_same_agent_as_executor() {
    let mut conn = fresh_db();
    seed_task(&mut conn, "t-1");
    let tx = conn.unchecked_transaction().unwrap();
    bootstrap_executor_evidence(&tx, &ExecutorEvidenceInput {
        task_id: "t-1".to_string(), steps: vec![ExecutorEvidenceStep { step_id: "s-1".to_string(), evidence_path: "e".to_string(), evidence_hash: "h".to_string() }],
        created_by: "same@x".to_string(), executor_agent_instance_id: "inst-a".to_string(),
    }, 1, ts()).unwrap();
    // reviewer 与 executor 同 agent_id → 独立性门禁拒绝
    let err = bootstrap_reviewer_pass(&tx, &ReviewerPassInput {
        task_id: "t-1".to_string(), evidence_path: "r".to_string(), evidence_hash: "rh".to_string(),
        created_by: "same@x".to_string(), reviewer_agent_id: "same@x".to_string(),
        reviewer_agent_instance_id: "i".to_string(), reviewer_session_id: "s".to_string(),
    }, "same@x", "inst-a", "sess-a", 1, "model-b", "tokhash", ts() + 3600.0, 2, ts()).unwrap_err();
    assert_eq!(err.code, ERR_BRIDGE_INDEPENDENCE);
}

#[test]
fn role_constants_present() {
    // 确保角色门禁常量可用（handler 层会用；此处仅静态校验导出）。
    assert_eq!(ERR_BRIDGE_ROLE, "E_BOOTSTRAP_BRIDGE_ROLE");
}

// ---- P0F-R1：阶段二也必须强制 empty-projection 门禁 ----
#[test]
fn reviewer_pass_rejects_when_governance_projection_exists_stage_two() {
    let mut conn = fresh_db();
    seed_task(&mut conn, "t-1");
    let tx = conn.unchecked_transaction().unwrap();
    // 阶段一成功：任务到达 review，且无任何 contract/role 投影。
    bootstrap_executor_evidence(&tx, &ExecutorEvidenceInput {
        task_id: "t-1".to_string(), steps: vec![ExecutorEvidenceStep { step_id: "s-1".to_string(), evidence_path: "e".to_string(), evidence_hash: "h".to_string() }],
        created_by: "exec@a".to_string(), executor_agent_instance_id: "inst-a".to_string(),
    }, 1, ts()).unwrap();
    // 模拟既有 Task Contract revision（非空投影）——即便任务已到 review，也须拒绝 stage-two bridge。
    conn.execute("INSERT INTO task_contract_revisions (contract_id,revision,contract_hash,profile,task_id,workspace_id,envelope_payload,created_at,created_by,normalization_version,normalization_rules_hash) VALUES ('c1',1,'h','code_change','t-1',1,'{}',0,'seed',1,'r')", []).unwrap();
    let err = bootstrap_reviewer_pass(&tx, &ReviewerPassInput {
        task_id: "t-1".to_string(), evidence_path: "r".to_string(), evidence_hash: "rh".to_string(),
        created_by: "rev@b".to_string(), reviewer_agent_id: "rev@b".to_string(),
        reviewer_agent_instance_id: "inst-b".to_string(), reviewer_session_id: "sess-b".to_string(),
    }, "exec@a", "inst-a", "sess-a", 1, "model-b", "tokhash", ts() + 3600.0, 2, ts()).unwrap_err();
    assert_eq!(err.code, ERR_BRIDGE_NOT_EMPTY);
    // 拒绝后不得写任何 reviewer pass event / lease。
    let ev: i64 = tx.query_row("SELECT COUNT(*) FROM task_events WHERE task_id='t-1' AND reason_code='task.bootstrap_reviewer_pass'", [], |r| r.get(0)).unwrap();
    let lc: i64 = tx.query_row("SELECT COUNT(*) FROM task_leases WHERE task_id='t-1' AND role='reviewer' AND status='active'", [], |r| r.get(0)).unwrap();
    assert_eq!(ev, 0);
    assert_eq!(lc, 0);
}

// ---- P0F-R2：pending/in_progress 步骤必须拒绝 ----
#[test]
fn executor_evidence_rejects_pending_step() {
    let mut conn = fresh_db();
    seed_task(&mut conn, "t-1");
    // 将步骤回退为 pending，模拟未完成步骤。
    conn.execute("UPDATE task_steps SET status='pending' WHERE id='s-1'", []).unwrap();
    let tx = conn.unchecked_transaction().unwrap();
    let err = bootstrap_executor_evidence(&tx, &ExecutorEvidenceInput {
        task_id: "t-1".to_string(), steps: vec![ExecutorEvidenceStep { step_id: "s-1".to_string(), evidence_path: "e".to_string(), evidence_hash: "h".to_string() }],
        created_by: "exec".to_string(), executor_agent_instance_id: "inst-a".to_string(),
    }, 1, ts()).unwrap_err();
    assert_eq!(err.code, ERR_BRIDGE_STATE);
    let st: String = tx.query_row("SELECT status FROM tasks WHERE id='t-1'", [], |r| r.get(0)).unwrap();
    assert_eq!(st, "open"); // 拒绝后任务状态不得被推到 review
}

// ---- P0F-R2：仅提交部分步骤必须拒绝 ----
#[test]
fn executor_evidence_rejects_partial_step_coverage() {
    let mut conn = fresh_db();
    seed_task(&mut conn, "t-1");
    // 再加第二个已完成步骤，测试只提交一个步骤时的覆盖校验。
    conn.execute("INSERT INTO task_steps (id,task_id,step_index,action,status,result,created_at) VALUES ('s-2','t-1',1,'test','done','',0)", []).unwrap();
    let tx = conn.unchecked_transaction().unwrap();
    let err = bootstrap_executor_evidence(&tx, &ExecutorEvidenceInput {
        task_id: "t-1".to_string(), steps: vec![ExecutorEvidenceStep { step_id: "s-1".to_string(), evidence_path: "e".to_string(), evidence_hash: "h".to_string() }],
        created_by: "exec".to_string(), executor_agent_instance_id: "inst-a".to_string(),
    }, 1, ts()).unwrap_err();
    assert_eq!(err.code, ERR_BRIDGE_INVALID);
}

// ---- P0F-R4：executor 与 reviewer 的 agent_instance_id 相同必须拒绝（三重独立性） ----
#[test]
fn reviewer_pass_rejects_same_agent_instance_as_executor() {
    let mut conn = fresh_db();
    seed_task(&mut conn, "t-1");
    let tx = conn.unchecked_transaction().unwrap();
    bootstrap_executor_evidence(&tx, &ExecutorEvidenceInput {
        task_id: "t-1".to_string(), steps: vec![ExecutorEvidenceStep { step_id: "s-1".to_string(), evidence_path: "e".to_string(), evidence_hash: "h".to_string() }],
        created_by: "exec@a".to_string(), executor_agent_instance_id: "inst-shared".to_string(),
    }, 1, ts()).unwrap();
    // agent_id、session_id 均不同，但 agent_instance_id 复用 → 必须被独立校验拒绝。
    let err = bootstrap_reviewer_pass(&tx, &ReviewerPassInput {
        task_id: "t-1".to_string(), evidence_path: "r".to_string(), evidence_hash: "rh".to_string(),
        created_by: "rev@b".to_string(), reviewer_agent_id: "rev@b".to_string(),
        reviewer_agent_instance_id: "inst-shared".to_string(), reviewer_session_id: "sess-b".to_string(),
    }, "exec@a", "inst-shared", "sess-a", 1, "model-b", "tokhash", ts() + 3600.0, 2, ts()).unwrap_err();
    assert_eq!(err.code, ERR_BRIDGE_INDEPENDENCE);
}

// ---- P0F-R3：lease workspace/model/fencing 不再硬编码 ----
#[test]
fn reviewer_lease_uses_bound_workspace_and_monotonic_fencing() {
    let mut conn = fresh_db();
    // 预置 workspace_id=2，供 bound_workspace≠1 的 FK 约束用例使用。
    conn.execute("INSERT INTO workspaces (id,name,root_path,created_at) VALUES (2,'ws-2','/tmp/ws-2',0)", []).unwrap();
    seed_task(&mut conn, "t-1");
    let tx = conn.unchecked_transaction().unwrap();
    bootstrap_executor_evidence(&tx, &ExecutorEvidenceInput {
        task_id: "t-1".to_string(), steps: vec![ExecutorEvidenceStep { step_id: "s-1".to_string(), evidence_path: "e".to_string(), evidence_hash: "h".to_string() }],
        created_by: "exec@a".to_string(), executor_agent_instance_id: "inst-a".to_string(),
    }, 1, ts()).unwrap();
    // 预置一个高 fencing 的历史 reviewer lease（模拟既有记录），验证重签时单调递增。
    conn.execute(
        "INSERT INTO task_leases (workspace_id,lease_id,task_id,role,status,agent_id,session_id,model_id,token_hash,fencing_counter,acquired_at,expires_at) VALUES (2,'brtl-old','t-1','reviewer','expired','old','old-s','m','t',5,0,0)",
        [], ).unwrap();
    // 以 bound_workspace=2、model_id='model-b' 完成 reviewer pass。
    let pass = bootstrap_reviewer_pass(&tx, &ReviewerPassInput {
        task_id: "t-1".to_string(), evidence_path: "r".to_string(), evidence_hash: "rh".to_string(),
        created_by: "rev@b".to_string(), reviewer_agent_id: "rev@b".to_string(),
        reviewer_agent_instance_id: "inst-b".to_string(), reviewer_session_id: "sess-b".to_string(),
    }, "exec@a", "inst-a", "sess-a", 2, "model-b", "tokhash", ts() + 3600.0, 3, ts()).unwrap();
    assert_eq!(pass["fencing_counter"], serde_json::json!(6)); // 历史 max=5 → +1
    // 只有 workspace_id=2、model=model-b、fencing=6 的 active lease 存在，硬编码 workspace=1/model='' 已消除。
    let (ws, model, fencing, active_count): (i64, String, i64, i64) = tx.query_row(
        "SELECT workspace_id, model_id, fencing_counter, COUNT(*) FROM task_leases WHERE task_id='t-1' AND role='reviewer' AND status='active'",
        [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
    ).unwrap();
    assert_eq!(active_count, 1);
    assert_eq!(ws, 2);
    assert_eq!(model, "model-b");
    assert_eq!(fencing, 6);
    // 历史 expired lease 已被回收（DELETE），world 只剩 1 条 active。
    let all: i64 = tx.query_row("SELECT COUNT(*) FROM task_leases WHERE task_id='t-1' AND role='reviewer'", [], |r| r.get(0)).unwrap();
    assert_eq!(all, 1);
}
