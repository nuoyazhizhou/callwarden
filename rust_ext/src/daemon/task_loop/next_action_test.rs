//! 任务 5A `next_action.rs` 领域测试（cw-role-handoff-task-loop.md §3.1-3.4/§6/§7）。
//!
//! 覆盖（验收矩阵对应行）：
//! - unclaimed executor step 合同完整 → READY/CLAIM/executor（§6 L586）；
//! - 当前 lease 未过期 → WAITING/WAIT 且不泄露 token（§6 L587）；
//! - review 无 verdict → READY/REVIEW/reviewer + 新独立 session（§6 L588）；
//! - reviewer BLOCKED → READY/REVISE/executor + 只读 revision_hint（§6 L589-590）；
//! - reviewer PASS → READY/ADJUDICATE/adjudicator（§6 L604）；
//! - closed → COMPLETE/NONE + routing complete/null（§6 L609）；
//! - 缺 Role Contract binding → BLOCKED/NONE（§6 L606）；
//! - unresolved failed step → 只返回精确 remediation step，不领取后续普通 step（§6 L595）；
//! - remediation 完成但未调用 resolution → 保持 in_progress/REVISE（§6 L597）；
//! - task 不存在 → E_TASK_NOT_FOUND_OR_UNAUTHORIZED（§3.2 规则 2）；
//! - binding 缺失 → E_WORKSPACE_AUTHORITY_UNAVAILABLE；instance 不匹配 → MISMATCH（§3.2 规则 1/3）；
//! - 零写入：evaluate 前后 task_steps/task_leases/task_events/task_verdict_events 行数不变（§6 L612）。

use rusqlite::Connection;
use sha2::{Digest, Sha256};

use crate::sqlite_query::migrate_connection;
use super::claim::{claim_step, ClaimStepInput, LedgerKey as ClaimLedgerKey};
use super::contract_set::{
    set_task_contract, ContractPayload, LedgerKey as ContractLedgerKey, SetContractInput,
};
use super::create::{create_task, CreateTaskInput, LedgerKey as CreateLedgerKey, WorkspaceCaptureInput};
use super::next_action::{
    evaluate_next_action, NextActionInput, ERR_TASK_NOT_FOUND_OR_UNAUTHORIZED,
    ERR_WORKSPACE_AUTHORITY_MISMATCH, ERR_WORKSPACE_AUTHORITY_UNAVAILABLE,
};
use super::types::FrozenAuthorityInput;
use super::verdict_evidence_gate::{
    submit_verdict, ContractTriple, LedgerKey as VerdictLedgerKey, VerdictInput,
};

/// 开启内存 task-DB 并跑一遍 migration。
fn fresh_db() -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    migrate_connection(&conn).expect("migration to v57");
    conn.execute(
        "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (?1, ?2, ?3, 0.0)",
        rusqlite::params![1, "ws-1", "/tmp/ws-1"],
    )
    .unwrap();
    conn
}

fn frozen() -> FrozenAuthorityInput {
    FrozenAuthorityInput::default()
}

fn sha256_hex(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

/// 读取 seed 出的 `verdict-normalization/v1` rules_hash（迁移原子插入）。
fn seeded_norm_hash(conn: &Connection) -> String {
    conn.query_row(
        "SELECT rules_hash FROM verdict_normalization_rules \
         WHERE normalization_version = 'verdict-normalization/v1'",
        [],
        |row| row.get(0),
    )
    .unwrap()
}

/// 复用 1A `create_task` 建立 task + 不可变 workspace binding（workspace_instance_id=ws-inst-1）。
fn setup_task(conn: &mut Connection, task_id: &str) {
    let ws = WorkspaceCaptureInput {
        workspace_id: 1,
        daemon_workspace_id: 42,
        workspace_instance_id: "ws-inst-1".to_string(),
        client_view_root_hash: "client-view-hash".to_string(),
        host_real_root_hash: "host-root-hash".to_string(),
        workspace_manifest_payload_json: "{\"kind\":\"a\"}".to_string(),
        workspace_manifest_hash: "manifest-a".to_string(),
        created_by: "test-creator".to_string(),
    };
    let input = CreateTaskInput {
        task_id: task_id.to_string(),
        title: format!("task-{task_id}"),
        description: "desc".to_string(),
        creator: "test-creator".to_string(),
    };
    create_task(
        conn,
        &frozen(),
        &CreateLedgerKey {
            workspace_instance_id: "ws-inst-1".to_string(),
            method: "task.create".to_string(),
            request_id: format!("create-{task_id}"),
        },
        &input,
        &ws,
    )
    .expect("setup create_task 应成功");
}

/// 复用 1B `set_task_contract` 建立 lineage + revision；返回 revision id。
fn setup_contract(conn: &mut Connection, task_id: &str, role: &str) -> String {
    let payload = ContractPayload {
        role: role.to_string(),
        skill_id: "skill-1".to_string(),
        skill_version: "1.0".to_string(),
        prompt_template_id: "pt-1".to_string(),
        prompt_hash: "ph-1".to_string(),
        allowed_paths: vec!["src/".to_string()],
        forbidden_paths: vec!["target/".to_string()],
        commands: vec!["echo".to_string()],
        acceptance_checks: vec!["pass".to_string()],
        required_evidence: vec!["log".to_string()],
        handoff_to: String::new(),
        independence: serde_json::json!({
            "different_agent_instance_from": [],
            "different_session_from": ["reviewer"],
            "max_tokens": 100,
        }),
    };
    let resp = set_task_contract(
        conn,
        &frozen(),
        &ContractLedgerKey {
            workspace_instance_id: "ws-inst-1".to_string(),
            method: "task.contract_set".to_string(),
            request_id: format!("contract-{task_id}-{role}"),
        },
        &SetContractInput {
            task_id: task_id.to_string(),
            contract: payload,
            created_by: "test-owner".to_string(),
        },
    )
    .expect("setup contract_set 应成功");
    resp["role_contract_revision_id"]
        .as_str()
        .unwrap()
        .to_string()
}

/// 插入 Task Contract 三元组行，并绑定 `verdict-normalization/v1`。
fn setup_task_contract(conn: &Connection, task_id: &str, contract_id: &str) {
    let norm_hash = seeded_norm_hash(conn);
    conn.execute(
        "INSERT INTO task_contract_revisions \
         (contract_id, revision, contract_hash, profile, task_id, workspace_id, \
          envelope_payload, created_at, created_by, \
          normalization_version, normalization_rules_hash) \
         VALUES (?1, 1, 'sha256:task-1', 'review', ?2, 1, '{\"objective\":\"t\"}', 0.0, 'test', \
                 'verdict-normalization/v1', ?3)",
        rusqlite::params![contract_id, task_id, norm_hash],
    )
    .unwrap();
}

/// 插入 active lease（role = 持有角色；expires_at 未来值）。
#[allow(clippy::too_many_arguments)]
fn setup_lease(
    conn: &Connection,
    lease_id: &str,
    role: &str,
    token: &str,
    counter: i64,
    agent: &str,
    session: &str,
    model: &str,
    expires_at: f64,
) {
    conn.execute(
        "INSERT INTO task_leases \
         (workspace_id, lease_id, task_id, role, agent_id, session_id, model_id, token_hash, \
          fencing_counter, acquired_at, expires_at, status) \
         VALUES (1, ?1, 't-1', ?2, ?3, ?4, ?5, ?6, ?7, 0.0, ?8, 'active')",
        rusqlite::params![
            lease_id,
            role,
            agent,
            session,
            model,
            sha256_hex(token.as_bytes()),
            counter,
            expires_at,
        ],
    )
    .unwrap();
}

/// 插入一个属于 task 的步骤。
fn setup_step(conn: &Connection, task_id: &str, step_id: i64, action: &str, status: &str) {
    conn.execute(
        "INSERT INTO task_steps \
         (id, task_id, step_index, action, status, result, created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, '', 0.0)",
        rusqlite::params![step_id, task_id, step_id, action, status],
    )
    .unwrap();
}

fn claim_key(request_id: &str) -> ClaimLedgerKey {
    ClaimLedgerKey {
        workspace_instance_id: "ws-inst-1".to_string(),
        method: "task.claim".to_string(),
        request_id: request_id.to_string(),
    }
}

/// 建立 claim 到 step（创建 verified binding；claim 不创建 lease）。
fn setup_binding(conn: &mut Connection, task_id: &str, step_id: &str, rcr_id: &str, request_id: &str) {
    setup_binding_with_remediation(conn, task_id, step_id, rcr_id, request_id, "");
}

/// 建立 claim 到 step（可携带 remediation_step_id；claim 不创建 lease）。
fn setup_binding_with_remediation(
    conn: &mut Connection,
    task_id: &str,
    step_id: &str,
    rcr_id: &str,
    request_id: &str,
    remediation_step_id: &str,
) {
    claim_step(
        conn,
        &frozen(),
        &claim_key(request_id),
        &ClaimStepInput {
            task_id: task_id.to_string(),
            step_id: step_id.to_string(),
            role_contract_revision_id: rcr_id.to_string(),
            remediation_step_id: remediation_step_id.to_string(),
            created_by: "test-claimer".to_string(),
        },
    )
    .expect("setup claim 应成功");
}

fn count(conn: &Connection, table: &str) -> i64 {
    conn.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| row.get(0))
        .unwrap()
}

fn set_task_status(conn: &Connection, task_id: &str, status: &str) {
    conn.execute(
        "UPDATE tasks SET status = ?1 WHERE id = ?2",
        rusqlite::params![status, task_id],
    )
    .unwrap();
}

// ---------------------------------------------------------------------------
// §6 核心场景
// ---------------------------------------------------------------------------

#[test]
fn unclaimed_executor_step_ready_claim() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let rcr = setup_contract(&mut conn, "t-1", "implementer");
    setup_task_contract(&conn, "t-1", "tc-t-1");
    setup_step(&conn, "t-1", 1, "implement", "pending");
    setup_binding(&mut conn, "t-1", "1", &rcr, "req-c-1");

    let resp = evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");

    assert_eq!(resp["decision"], serde_json::json!("READY"));
    assert_eq!(resp["action"], serde_json::json!("CLAIM"));
    assert_eq!(resp["required_role"], serde_json::json!("executor"));
    assert_eq!(resp["step_id"], serde_json::json!("1"));
    assert_eq!(resp["routing"]["next_role"], serde_json::json!("executor"));
    assert_eq!(resp["routing"]["next_action"], serde_json::json!("claim_current_step"));
    assert_eq!(resp["routing"]["origin_kind"], serde_json::json!("system_evaluator"));
    assert!(resp.get("from_role").is_none(), "不得输出 from_role");
    let ns = &resp["next_session"];
    assert_eq!(ns["role"], serde_json::json!("executor"));
    assert_eq!(ns["step_id"], serde_json::json!("1"));
    assert_eq!(ns["must_be_new_session"], serde_json::json!(false));
    assert_eq!(resp["revision_hint"], serde_json::Value::Null);
    // 任意查询零写入：无 lease、无事件（§6 L612）。
    assert_eq!(count(&conn, "task_leases"), 0);
    assert_eq!(count(&conn, "task_events"), 0);
}

#[test]
fn active_lease_yields_waiting_without_token() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let rcr = setup_contract(&mut conn, "t-1", "implementer");
    setup_task_contract(&conn, "t-1", "tc-t-1");
    setup_step(&conn, "t-1", 1, "implement", "pending");
    setup_binding(&mut conn, "t-1", "1", &rcr, "req-c-1");
    setup_lease(&conn, "L-1", "executor", "token-1", 1, "agent-1", "sess-1", "model-1", 1e18);

    let resp = evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");

    assert_eq!(resp["decision"], serde_json::json!("WAITING"));
    assert_eq!(resp["action"], serde_json::json!("WAIT"));
    assert_eq!(resp["routing"]["next_role"], serde_json::Value::Null);
    assert_eq!(resp["next_session"], serde_json::Value::Null);
    let body = serde_json::to_string(&resp).unwrap();
    assert!(!body.contains("token-1"), "不得泄露 lease token");
}

#[test]
fn review_without_verdict_ready_review() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let rcr = setup_contract(&mut conn, "t-1", "implementer");
    setup_contract(&mut conn, "t-1", "reviewer");
    setup_task_contract(&conn, "t-1", "tc-t-1");
    setup_step(&conn, "t-1", 1, "implement", "pending");
    setup_binding(&mut conn, "t-1", "1", &rcr, "req-c-1");
    set_task_status(&conn, "t-1", "review");

    let resp = evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");

    assert_eq!(resp["decision"], serde_json::json!("READY"));
    assert_eq!(resp["action"], serde_json::json!("REVIEW"));
    assert_eq!(resp["required_role"], serde_json::json!("reviewer"));
    assert_eq!(resp["routing"]["next_role"], serde_json::json!("reviewer"));
    let ns = &resp["next_session"];
    assert_eq!(ns["role"], serde_json::json!("reviewer"));
    assert_eq!(ns["must_be_new_session"], serde_json::json!(true));
    assert_eq!(count(&conn, "task_verdict_events"), 0, "evaluate 不写 verdict ledger");
}

// ---------------------------------------------------------------------------
// reviewer BLOCKED / PASS 场景（使用领域 submit_verdict 构造有效 verdict）
// ---------------------------------------------------------------------------

struct ReviewBase {
    rcr_id: String,
    rcr_revision: i64,
    rcr_hash: String,
    tc_id: String,
}

/// 建立 review 前置：task + 全部 lineage（implementer/reviewer/executor/adjudicator）+
/// task contract + step + step binding + reviewer lease。返回 step 的 Role Contract 三元组。
fn setup_review_base(conn: &mut Connection) -> ReviewBase {
    setup_task(conn, "t-1");
    let rcr_id = setup_contract(conn, "t-1", "implementer");
    setup_contract(conn, "t-1", "reviewer");
    setup_contract(conn, "t-1", "executor");
    setup_contract(conn, "t-1", "adjudicator");
    let (rcr_revision, rcr_hash): (i64, String) = conn
        .query_row(
            "SELECT revision, role_contract_hash FROM role_contract_revisions \
             WHERE role_contract_revision_id = ?1",
            [&rcr_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    let tc_id = "tc-t-1".to_string();
    setup_task_contract(conn, "t-1", &tc_id);
    setup_step(conn, "t-1", 1, "implement", "pending");
    setup_binding(conn, "t-1", "1", &rcr_id, "req-c-1");
    setup_lease(conn, "L-1", "reviewer", "token-1", 1, "agent-1", "sess-1", "model-1", 1e18);
    ReviewBase { rcr_id, rcr_revision, rcr_hash, tc_id }
}

fn verdict_key(request_id: &str) -> VerdictLedgerKey {
    VerdictLedgerKey {
        workspace_instance_id: "ws-inst-1".to_string(),
        method: "verdict.submit".to_string(),
        request_id: request_id.to_string(),
    }
}

fn review_input(base: &ReviewBase, overall: &str) -> VerdictInput {
    VerdictInput {
        task_id: "t-1".to_string(),
        step_id: "1".to_string(),
        overall: overall.to_string(),
        phase: "blind_first_pass".to_string(),
        clause_results: "[]".to_string(),
        findings: "[]".to_string(),
        task_contract: ContractTriple {
            id: base.tc_id.clone(),
            revision: 1,
            hash: "sha256:task-1".to_string(),
        },
        role_contract: ContractTriple {
            id: base.rcr_id.clone(),
            revision: base.rcr_revision,
            hash: base.rcr_hash.clone(),
        },
        amendment_ref: String::new(),
        snapshot_id: "snap-1".to_string(),
        view_manifest_hash: "manifest-1".to_string(),
        attestation: "attest-1".to_string(),
        acting_role: "reviewer".to_string(),
        agent_id: "agent-1".to_string(),
        session_id: "sess-1".to_string(),
        model_id: "model-1".to_string(),
        lease_token: "token-1".to_string(),
        fencing_counter: 1,
        created_by: "test-reviewer".to_string(),
    }
}

/// 提交 verdict 后释放 reviewer lease，并把 task 置为 review。
fn enter_review_with_verdict(conn: &mut Connection, base: &ReviewBase, overall: &str) -> String {
    let resp = submit_verdict(
        conn,
        &frozen(),
        &verdict_key(&format!("v-{overall}")),
        &review_input(base, overall),
    )
    .expect("submit verdict 应成功");
    let verdict_id = resp["verdict_id"].as_str().unwrap().to_string();
    conn.execute(
        "UPDATE task_leases SET status = 'released' WHERE lease_id = 'L-1'",
        [],
    )
    .unwrap();
    set_task_status(conn, "t-1", "review");
    verdict_id
}

#[test]
fn reviewer_blocked_yields_revise_with_readonly_hint() {
    let mut conn = fresh_db();
    let base = setup_review_base(&mut conn);
    let verdict_id = enter_review_with_verdict(&mut conn, &base, "block");

    let resp = evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");

    assert_eq!(resp["decision"], serde_json::json!("READY"));
    assert_eq!(resp["action"], serde_json::json!("REVISE"));
    assert_eq!(resp["required_role"], serde_json::json!("executor"));
    assert_eq!(resp["routing"]["next_role"], serde_json::json!("executor"));
    let ns = &resp["next_session"];
    assert_eq!(ns["role"], serde_json::json!("executor"));
    assert_eq!(ns["must_be_new_session"], serde_json::json!(false));
    let hint = &resp["revision_hint"];
    assert_eq!(hint["source_verdict_id"], serde_json::json!(verdict_id));
    // 旧 verdict 不改写、evaluate 不追加事件（§6 L589）。
    assert_eq!(count(&conn, "task_verdict_events"), 1);
}

#[test]
fn reviewer_pass_yields_adjudicate_with_reviewer_lease_role() {
    let mut conn = fresh_db();
    let base = setup_review_base(&mut conn);
    enter_review_with_verdict(&mut conn, &base, "pass");

    let resp = evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");

    assert_eq!(resp["decision"], serde_json::json!("READY"));
    assert_eq!(resp["action"], serde_json::json!("ADJUDICATE"));
    assert_eq!(resp["required_role"], serde_json::json!("adjudicator"));
    // Adjudicator 执行 apply/close 的 acting role=adjudicator，lease role=reviewer（§3.1）。
    assert_eq!(resp["authorization"]["lease_role"], serde_json::json!("reviewer"));
    let ns = &resp["next_session"];
    assert_eq!(ns["role"], serde_json::json!("adjudicator"));
    assert_eq!(ns["must_be_new_session"], serde_json::json!(true));
}

// ---------------------------------------------------------------------------
// 终态 / fail-closed / remediation
// ---------------------------------------------------------------------------

#[test]
fn closed_task_complete_none() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let rcr = setup_contract(&mut conn, "t-1", "implementer");
    setup_task_contract(&conn, "t-1", "tc-t-1");
    setup_step(&conn, "t-1", 1, "implement", "pending");
    setup_binding(&mut conn, "t-1", "1", &rcr, "req-c-1");
    set_task_status(&conn, "t-1", "closed");

    let resp = evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");

    assert_eq!(resp["decision"], serde_json::json!("COMPLETE"));
    assert_eq!(resp["action"], serde_json::json!("NONE"));
    assert_eq!(resp["routing"]["next_role"], serde_json::json!("complete"));
    assert_eq!(resp["next_session"], serde_json::Value::Null);
    assert!(resp.get("from_role").is_none());
}

#[test]
fn missing_binding_yields_blocked_none() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    setup_contract(&mut conn, "t-1", "implementer");
    setup_task_contract(&conn, "t-1", "tc-t-1");
    setup_step(&conn, "t-1", 1, "implement", "pending");
    // 无 binding（未 claim）→ step 无唯一可验证 Role Contract → BLOCKED/NONE。

    let resp = evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");

    assert_eq!(resp["decision"], serde_json::json!("BLOCKED"));
    assert_eq!(resp["action"], serde_json::json!("NONE"));
    assert_eq!(resp["routing"]["next_role"], serde_json::Value::Null);
    assert_eq!(resp["next_session"], serde_json::Value::Null);
    assert!(!resp["blocking_conditions"].as_array().unwrap().is_empty());
}

#[test]
fn unresolved_failed_returns_only_remediation_step() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let rcr = setup_contract(&mut conn, "t-1", "implementer");
    setup_task_contract(&conn, "t-1", "tc-t-1");
    // step 1 failed（unresolved）；step 2 = fix_defect remediation（pending，绑定来源）；
    // step 3 = 后续普通 pending step（不得被返回）。
    setup_step(&conn, "t-1", 1, "implement", "failed");
    setup_step(&conn, "t-1", 2, "fix_defect", "pending");
    setup_step(&conn, "t-1", 3, "implement", "pending");
    conn.execute(
        "UPDATE task_steps SET result = ?1 WHERE id = '2' AND task_id = 't-1'",
        [r#"{"remediation_of_step_id":"1"}"#],
    )
    .unwrap();
    setup_binding_with_remediation(&mut conn, "t-1", "2", &rcr, "req-c-2", "2");

    let resp = evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");

    assert_eq!(resp["decision"], serde_json::json!("READY"));
    assert_eq!(resp["action"], serde_json::json!("CLAIM"));
    assert_eq!(resp["step_id"], serde_json::json!("2"), "只返回精确 remediation step");
    assert_eq!(resp["next_session"]["step_id"], serde_json::json!("2"));
    assert_ne!(resp["step_id"], serde_json::json!("3"), "不得返回后续普通 step");
}

#[test]
fn remediation_done_without_resolution_stays_revise() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let rcr = setup_contract(&mut conn, "t-1", "implementer");
    setup_task_contract(&conn, "t-1", "tc-t-1");
    setup_step(&conn, "t-1", 1, "implement", "failed");
    // 修复已做（done）但原 failed step 未 resolve：claim 时 step 2 仍 pending，
    // 先写 result 指向 unresolved failed，claim 后再标记 done。
    setup_step(&conn, "t-1", 2, "fix_defect", "pending");
    conn.execute(
        "UPDATE task_steps SET result = ?1 WHERE id = '2' AND task_id = 't-1'",
        [r#"{"remediation_of_step_id":"1"}"#],
    )
    .unwrap();
    setup_binding_with_remediation(&mut conn, "t-1", "2", &rcr, "req-c-2", "2");
    conn.execute(
        "UPDATE task_steps SET status = 'done' WHERE id = '2' AND task_id = 't-1'",
        [],
    )
    .unwrap();

    let resp = evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");

    // §6 L597：原 failed 仍 failed，任务保持 in_progress/REVISE，不得自动转 review。
    assert_eq!(resp["decision"], serde_json::json!("READY"));
    assert_eq!(resp["action"], serde_json::json!("REVISE"));
    let hint = &resp["revision_hint"];
    assert_eq!(hint["failed_steps"], serde_json::json!(["1"]));
    assert_ne!(resp["action"], serde_json::json!("REVIEW"));
}

// ---------------------------------------------------------------------------
// 输入校验 / workspace authority / 零写入
// ---------------------------------------------------------------------------

#[test]
fn from_params_requires_both_fields() {
    let err = NextActionInput::from_params(&serde_json::json!({"task_id": "t-1"}))
        .expect_err("缺 workspace_instance_id 必须拒绝");
    assert_eq!(err.code, "invalid_params");
    let err = NextActionInput::from_params(&serde_json::json!({})).expect_err("缺字段必须拒绝");
    assert_eq!(err.code, "invalid_params");
}

#[test]
fn missing_task_returns_non_leaking_error() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let err = evaluate_next_action(&conn, "ws-inst-1", "t-99").expect_err("不存在必须拒绝");
    assert_eq!(err.code, ERR_TASK_NOT_FOUND_OR_UNAUTHORIZED);
}

#[test]
fn orphan_task_without_binding_authority_unavailable() {
    let conn = fresh_db();
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, updated_at) \
         VALUES ('t-orphan', 'x', 'open', 0.0, 0.0)",
        [],
    )
    .unwrap();
    let err = evaluate_next_action(&conn, "ws-inst-1", "t-orphan").expect_err("无 binding 必须拒绝");
    assert_eq!(err.code, ERR_WORKSPACE_AUTHORITY_UNAVAILABLE);
}

#[test]
fn workspace_instance_mismatch_authority_mismatch() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let err = evaluate_next_action(&conn, "ws-inst-OTHER", "t-1").expect_err("instance 不匹配必须拒绝");
    assert_eq!(err.code, ERR_WORKSPACE_AUTHORITY_MISMATCH);
}

#[test]
fn evaluate_is_strictly_read_only() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let rcr = setup_contract(&mut conn, "t-1", "implementer");
    setup_task_contract(&conn, "t-1", "tc-t-1");
    setup_step(&conn, "t-1", 1, "implement", "pending");
    setup_binding(&mut conn, "t-1", "1", &rcr, "req-c-1");

    let before = (
        count(&conn, "task_steps"),
        count(&conn, "task_leases"),
        count(&conn, "task_events"),
        count(&conn, "task_verdict_events"),
        count(&conn, "task_workspace_bindings"),
        count(&conn, "task_step_role_contract_bindings"),
    );
    evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");
    let after = (
        count(&conn, "task_steps"),
        count(&conn, "task_leases"),
        count(&conn, "task_events"),
        count(&conn, "task_verdict_events"),
        count(&conn, "task_workspace_bindings"),
        count(&conn, "task_step_role_contract_bindings"),
    );
    assert_eq!(before, after, "evaluate 必须零写入");
}
