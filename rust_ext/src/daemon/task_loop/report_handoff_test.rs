//! 任务 2 `report_handoff.rs` 领域测试（cw-role-handoff-task-loop.md §4.3/§4.4/§6）。
//!
//! 覆盖：六种 outcome 成功路径（executor_ready_for_review 为主）、outcome 路由非法、
//! source identity 不匹配、lease 各失败场景（NOT_FOUND/TOKEN_MISMATCH/EXPIRED/
//! FENCING_STALE/HOLDER_MISMATCH）、step 无 binding、Role/Task Contract 三元组 ABA
//! 不一致、`handoff_to` 不匹配、lineage 归属不一致、task 无 workspace binding、
//! 幂等重放不重复追加事件、`tasks.status` 不被修改、`task.report` 保留字段零写入拒绝。

use rusqlite::Connection;
use sha2::{Digest, Sha256};

use crate::sqlite_query::migrate_connection;
use super::claim::{claim_step, ClaimStepInput, LedgerKey as ClaimLedgerKey};
use super::contract_set::{
    set_task_contract, ContractPayload, LedgerKey as ContractLedgerKey, SetContractInput,
};
use super::create::{create_task, CreateTaskInput, LedgerKey as CreateLedgerKey, WorkspaceCaptureInput};
use super::report_handoff::{
    reject_report_reserved_fields, submit_handoff, ContractTriple, HandoffInput, LedgerKey,
    ERR_HANDOFF_CONTRACT_STALE, ERR_HANDOFF_FIELDS_REQUIRED,
    ERR_HANDOFF_REQUIRES_TASK_HANDOFF, ERR_HANDOFF_ROLE_IDENTITY_MISMATCH,
    ERR_HANDOFF_ROUTE_INVALID, ERR_LEASE_EXPIRED, ERR_LEASE_FENCING_STALE,
    ERR_LEASE_HOLDER_MISMATCH, ERR_LEASE_NOT_FOUND, ERR_LEASE_REQUIRED,
    ERR_LEASE_TOKEN_MISMATCH, ERR_STEP_BINDING_INVALID, ERR_TASK_BINDING_REQUIRED,
};
use super::types::FrozenAuthorityInput;

/// 开启内存 task-DB 并跑一遍 migration（v56：含 task_step_role_contract_bindings）。
fn fresh_db() -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    migrate_connection(&conn).expect("migration to v56");
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

/// 复用 1A `create_task` 建立 task + 不可变 workspace binding。
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
fn setup_contract(conn: &mut Connection, task_id: &str, role: &str, handoff_to: &str) -> String {
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
        handoff_to: handoff_to.to_string(),
        independence: serde_json::json!({"max_tokens": 100}),
    };
    let resp = set_task_contract(
        conn,
        &frozen(),
        &ContractLedgerKey {
            workspace_instance_id: "ws-inst-1".to_string(),
            method: "task.contract_set".to_string(),
            request_id: format!("contract-{task_id}-{role}-{handoff_to}"),
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

/// 插入 Task Contract 三元组行（task_contract_revisions，v1 仅做全等匹配校验）。
fn setup_task_contract(conn: &Connection, task_id: &str, contract_id: &str) {
    conn.execute(
        "INSERT INTO task_contract_revisions \
         (contract_id, revision, contract_hash, profile, task_id, workspace_id, \
          envelope_payload, created_at, created_by) \
         VALUES (?1, 1, 'sha256:task-1', 'review', ?2, 1, '{\"objective\":\"t\"}', 0.0, 'test')",
        rusqlite::params![contract_id, task_id],
    )
    .unwrap();
}

/// 插入 active source lease（role = acting_role，与遗留 validate_lease_for_mutation 一致）。
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
fn setup_step(conn: &Connection, task_id: &str, step_id: i64, action: &str, status: &str, result: &str) {
    conn.execute(
        "INSERT INTO task_steps \
         (id, task_id, step_index, action, status, result, created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, 0.0)",
        rusqlite::params![step_id, task_id, step_id, action, status, result],
    )
    .unwrap();
}

/// 标准成功路径 base：task + role contract(handoff_to=reviewer) + task contract + lease +
/// step claim。返回全部三元组，供成功测试与各失败测试构造 input。
struct Base {
    rcr_id: String,
    rcr_revision: i64,
    rcr_hash: String,
    tc_id: String,
}

fn setup_handoff_base(conn: &mut Connection, handoff_to: &str, with_lease: bool) -> Base {
    setup_task(conn, "t-1");
    let rcr_id = setup_contract(conn, "t-1", "coder", handoff_to);
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
    if with_lease {
        setup_lease(conn, "L-1", "implementer", "token-1", 1, "agent-1", "sess-1", "model-1", 1e18);
    }
    setup_step(conn, "t-1", 1, "inspect", "pending", "");
    claim_step(
        conn,
        &frozen(),
        &ClaimLedgerKey {
            workspace_instance_id: "ws-inst-1".to_string(),
            method: "task.claim".to_string(),
            request_id: "req-claim-1".to_string(),
        },
        &ClaimStepInput {
            task_id: "t-1".to_string(),
            step_id: "1".to_string(),
            role_contract_revision_id: rcr_id.clone(),
            remediation_step_id: String::new(),
            created_by: "test-claimer".to_string(),
        },
    )
    .expect("setup claim 应成功");
    Base { rcr_id, rcr_revision, rcr_hash, tc_id }
}

fn handoff_key(request_id: &str) -> LedgerKey {
    LedgerKey {
        workspace_instance_id: "ws-inst-1".to_string(),
        method: "task.handoff".to_string(),
        request_id: request_id.to_string(),
    }
}

fn success_input(base: &Base) -> HandoffInput {
    HandoffInput {
        task_id: "t-1".to_string(),
        step_id: "1".to_string(),
        source_role: "executor".to_string(),
        target_role: "reviewer".to_string(),
        reason: "已完成，请求评审".to_string(),
        outcome: "executor_ready_for_review".to_string(),
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
        required_new_instance: false,
        required_new_session: false,
        acting_role: "implementer".to_string(),
        agent_id: "agent-1".to_string(),
        session_id: "sess-1".to_string(),
        model_id: "model-1".to_string(),
        lease_token: "token-1".to_string(),
        fencing_counter: 1,
        created_by: "test-handoff".to_string(),
    }
}

fn count(conn: &Connection, table: &str) -> i64 {
    conn.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| row.get(0))
        .unwrap()
}

fn count_handoff_events(conn: &Connection) -> i64 {
    conn.query_row(
        "SELECT COUNT(*) FROM task_events WHERE reason_code = 'handoff_structured'",
        [],
        |row| row.get(0),
    )
    .unwrap()
}

// ---------------------------------------------------------------------------
// from_params 严格解析
// ---------------------------------------------------------------------------

#[test]
fn from_params_rejects_missing_structured_fields() {
    // 缺 task_id → invalid_params
    let err = HandoffInput::from_params(&serde_json::json!({})).unwrap_err();
    assert_eq!(err.code, "invalid_params");

    let full = |overrides: serde_json::Value| -> serde_json::Value {
        let mut v = serde_json::json!({
            "task_id": "t-1", "step_id": "1",
            "source_role": "executor", "target_role": "reviewer",
            "reason": "r", "outcome": "executor_ready_for_review",
            "task_contract": {"id": "tc-1", "revision": 1, "hash": "sha256:t"},
            "role_contract": {"id": "rcr-1", "revision": 1, "hash": "sha256:r"},
            "required_new_instance": false, "required_new_session": false,
            "acting_role": "implementer", "lease_token": "tok", "fencing_counter": 1,
        });
        let obj = v.as_object_mut().unwrap();
        for (k, val) in overrides.as_object().unwrap() {
            obj.insert(k.clone(), val.clone());
        }
        v
    };

    // 缺任一结构化字段 → E_HANDOFF_FIELDS_REQUIRED（不进入 executor）。
    for key in ["step_id", "source_role", "target_role", "reason", "outcome"] {
        let mut v = full(serde_json::json!({}));
        v.as_object_mut().unwrap().remove(key);
        let err = HandoffInput::from_params(&v).unwrap_err();
        assert_eq!(err.code, ERR_HANDOFF_FIELDS_REQUIRED, "缺 {key}");
    }
    // 缺 task_contract/role_contract 对象 → E_HANDOFF_FIELDS_REQUIRED。
    let mut v = full(serde_json::json!({}));
    v.as_object_mut().unwrap().remove("task_contract");
    assert_eq!(
        HandoffInput::from_params(&v).unwrap_err().code,
        ERR_HANDOFF_FIELDS_REQUIRED
    );

    // 缺 lease_token / fencing_counter（必须成对）→ E_LEASE_REQUIRED。
    let v = full(serde_json::json!({"lease_token": ""}));
    assert_eq!(HandoffInput::from_params(&v).unwrap_err().code, ERR_LEASE_REQUIRED);
    let mut v = full(serde_json::json!({}));
    v.as_object_mut().unwrap().remove("fencing_counter");
    assert_eq!(HandoffInput::from_params(&v).unwrap_err().code, ERR_LEASE_REQUIRED);

    // 完整字段成功解析。
    let parsed = HandoffInput::from_params(&full(serde_json::json!({}))).unwrap();
    assert_eq!(parsed.source_role, "executor");
    assert_eq!(parsed.task_contract.id, "tc-1");
}

// ---------------------------------------------------------------------------
// 成功路径
// ---------------------------------------------------------------------------

#[test]
fn executor_ready_for_review_succeeds() {
    let mut conn = fresh_db();
    let base = setup_handoff_base(&mut conn, "reviewer", true);
    let resp = submit_handoff(&mut conn, &frozen(), &handoff_key("h-1"), &success_input(&base))
        .expect("executor_ready_for_review 应成功");
    assert_eq!(resp["ok"].as_bool(), Some(true));
    assert_eq!(resp["task_id"], "t-1");
    assert_eq!(resp["step_id"], "1");
    assert_eq!(resp["source_role"], "executor");
    assert_eq!(resp["target_role"], "reviewer");
    assert_eq!(resp["outcome"], "executor_ready_for_review");
    assert_eq!(resp["request_id"], "h-1");
    assert_eq!(resp["handoff_event_id"], "he-t-1-h-1");
    assert_eq!(resp["workspace_id"], 1);
    assert_eq!(resp["independence_requirement"], "required");
    assert_eq!(resp["role_contract"]["lineage_id"], "rcl-t-1-coder");

    // 领域事件落库：reason_code=handoff_structured、envelope 携带 request_id。
    assert_eq!(count_handoff_events(&conn), 1);
    let (reason_code, reason, seq): (String, String, i64) = conn
        .query_row(
            "SELECT reason_code, reason, monotonic_seq FROM task_events \
             WHERE reason_code = 'handoff_structured'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    assert_eq!(reason_code, "handoff_structured");
    assert_eq!(seq, 1);
    let envelope: serde_json::Value = serde_json::from_str(&reason).unwrap();
    assert_eq!(envelope["handoff_event_id"], "he-t-1-h-1");
    assert_eq!(envelope["request_id"], "h-1");
    assert_eq!(envelope["source_role"], "executor");
    assert_eq!(envelope["target_role"], "reviewer");
    assert_eq!(envelope["independence_requirement"], "required");
    assert_eq!(envelope["fencing_counter"], 1);

    // v1 不修改 tasks.status（§4.3 第 475 行）。
    let status: String = conn
        .query_row("SELECT status FROM tasks WHERE id = 't-1'", [], |row| row.get(0))
        .unwrap();
    assert_eq!(status, "open");
    // 目标角色只记录为下一候选人，不写 claim/lease。
    assert_eq!(count(&conn, "task_leases"), 1, "handoff 不得创建 lease");
    assert_eq!(count(&conn, "task_step_role_contract_bindings"), 1, "handoff 不得追加 binding");
}

#[test]
fn adjudicator_accepted_routes_to_complete() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let rcr_id = setup_contract(&mut conn, "t-1", "auditor", "complete");
    let (rcr_revision, rcr_hash): (i64, String) = conn
        .query_row(
            "SELECT revision, role_contract_hash FROM role_contract_revisions \
             WHERE role_contract_revision_id = ?1",
            [&rcr_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    setup_task_contract(&conn, "t-1", "tc-t-1");
    setup_lease(&conn, "L-1", "adjudicator", "token-1", 1, "agent-1", "sess-1", "model-1", 1e18);
    setup_step(&conn, "t-1", 1, "inspect", "pending", "");
    claim_step(
        &mut conn,
        &frozen(),
        &ClaimLedgerKey {
            workspace_instance_id: "ws-inst-1".to_string(),
            method: "task.claim".to_string(),
            request_id: "req-claim-1".to_string(),
        },
        &ClaimStepInput {
            task_id: "t-1".to_string(),
            step_id: "1".to_string(),
            role_contract_revision_id: rcr_id.clone(),
            remediation_step_id: String::new(),
            created_by: "test-claimer".to_string(),
        },
    )
    .expect("claim ok");

    let mut input = success_input(&Base {
        rcr_id: rcr_id.clone(),
        rcr_revision,
        rcr_hash: rcr_hash.clone(),
        tc_id: "tc-t-1".to_string(),
    });
    input.source_role = "adjudicator".to_string();
    input.target_role = "complete".to_string();
    input.outcome = "adjudicator_accepted".to_string();
    input.acting_role = "adjudicator".to_string();
    let resp = submit_handoff(&mut conn, &frozen(), &handoff_key("h-a"), &input)
        .expect("adjudicator_accepted 应成功");
    assert_eq!(resp["target_role"], "complete");
    assert_eq!(resp["independence_requirement"], "not_applicable");
    assert_eq!(count_handoff_events(&conn), 1);
}

// ---------------------------------------------------------------------------
// 确定性拒绝路径
// ---------------------------------------------------------------------------

#[test]
fn rejects_unknown_outcome_route() {
    let mut conn = fresh_db();
    let base = setup_handoff_base(&mut conn, "reviewer", true);
    let mut input = success_input(&base);
    input.outcome = "executor_ready_for_reviewer".to_string(); // 非法 outcome
    let err = submit_handoff(&mut conn, &frozen(), &handoff_key("h-bad"), &input)
        .expect_err("非法 outcome 必须拒绝");
    assert_eq!(err.code, ERR_HANDOFF_ROUTE_INVALID);
    assert_eq!(count_handoff_events(&conn), 0);
}

#[test]
fn rejects_outcome_role_combination_mismatch() {
    let mut conn = fresh_db();
    let base = setup_handoff_base(&mut conn, "reviewer", true);
    let mut input = success_input(&base);
    // outcome 合法但 source/target 组合不匹配（executor→adjudicator 违反 §6 固定路由）。
    input.target_role = "adjudicator".to_string();
    let err = submit_handoff(&mut conn, &frozen(), &handoff_key("h-bad2"), &input)
        .expect_err("outcome 与 source/target 组合不匹配必须拒绝");
    assert_eq!(err.code, ERR_HANDOFF_ROUTE_INVALID);
    assert_eq!(count_handoff_events(&conn), 0);
}

#[test]
fn rejects_identity_role_mismatch() {
    let mut conn = fresh_db();
    let base = setup_handoff_base(&mut conn, "reviewer", true);
    let mut input = success_input(&base);
    input.acting_role = "adjudicator".to_string(); // runtime=adjudicator != executor
    let err = submit_handoff(&mut conn, &frozen(), &handoff_key("h-id"), &input)
        .expect_err("source_role 与 acting_role 不匹配必须拒绝");
    assert_eq!(err.code, ERR_HANDOFF_ROLE_IDENTITY_MISMATCH);
    assert_eq!(count_handoff_events(&conn), 0);
}

#[test]
fn rejects_missing_lease() {
    let mut conn = fresh_db();
    let base = setup_handoff_base(&mut conn, "reviewer", false); // 无 lease
    let err = submit_handoff(&mut conn, &frozen(), &handoff_key("h-lease-0"), &success_input(&base))
        .expect_err("无 active lease 必须拒绝");
    assert_eq!(err.code, ERR_LEASE_NOT_FOUND);
    assert_eq!(count_handoff_events(&conn), 0);
}

#[test]
fn rejects_lease_token_mismatch() {
    let mut conn = fresh_db();
    let base = setup_handoff_base(&mut conn, "reviewer", true);
    let mut input = success_input(&base);
    input.lease_token = "wrong-token".to_string();
    let err = submit_handoff(&mut conn, &frozen(), &handoff_key("h-lease-1"), &input)
        .expect_err("token hash 不匹配必须拒绝");
    assert_eq!(err.code, ERR_LEASE_TOKEN_MISMATCH);
}

#[test]
fn rejects_expired_lease() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let rcr_id = setup_contract(&mut conn, "t-1", "coder", "reviewer");
    let (rcr_revision, rcr_hash): (i64, String) = conn
        .query_row(
            "SELECT revision, role_contract_hash FROM role_contract_revisions \
             WHERE role_contract_revision_id = ?1",
            [&rcr_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    setup_task_contract(&conn, "t-1", "tc-t-1");
    setup_lease(&conn, "L-1", "implementer", "token-1", 1, "agent-1", "sess-1", "model-1", 0.0);
    setup_step(&conn, "t-1", 1, "inspect", "pending", "");
    claim_step(
        &mut conn,
        &frozen(),
        &ClaimLedgerKey {
            workspace_instance_id: "ws-inst-1".to_string(),
            method: "task.claim".to_string(),
            request_id: "req-claim-1".to_string(),
        },
        &ClaimStepInput {
            task_id: "t-1".to_string(),
            step_id: "1".to_string(),
            role_contract_revision_id: rcr_id.clone(),
            remediation_step_id: String::new(),
            created_by: "test-claimer".to_string(),
        },
    )
    .expect("claim ok");

    let base = Base { rcr_id, rcr_revision, rcr_hash, tc_id: "tc-t-1".to_string() };
    let err = submit_handoff(&mut conn, &frozen(), &handoff_key("h-lease-2"), &success_input(&base))
        .expect_err("过期 lease 必须拒绝");
    assert_eq!(err.code, ERR_LEASE_EXPIRED);
}

#[test]
fn rejects_stale_fencing_counter() {
    let mut conn = fresh_db();
    let base = setup_handoff_base(&mut conn, "reviewer", true);
    let mut input = success_input(&base);
    input.fencing_counter = 2; // 当前为 1
    let err = submit_handoff(&mut conn, &frozen(), &handoff_key("h-lease-3"), &input)
        .expect_err("fencing 不一致必须拒绝");
    assert_eq!(err.code, ERR_LEASE_FENCING_STALE);
}

#[test]
fn rejects_holder_identity_mismatch() {
    let mut conn = fresh_db();
    let base = setup_handoff_base(&mut conn, "reviewer", true);
    let mut input = success_input(&base);
    input.agent_id = "other-agent".to_string();
    let err = submit_handoff(&mut conn, &frozen(), &handoff_key("h-lease-4"), &input)
        .expect_err("holder Identity 不一致必须拒绝");
    assert_eq!(err.code, ERR_LEASE_HOLDER_MISMATCH);
}

#[test]
fn rejects_step_without_binding() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let rcr_id = setup_contract(&mut conn, "t-1", "coder", "reviewer");
    let (rcr_revision, rcr_hash): (i64, String) = conn
        .query_row(
            "SELECT revision, role_contract_hash FROM role_contract_revisions \
             WHERE role_contract_revision_id = ?1",
            [&rcr_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    setup_task_contract(&conn, "t-1", "tc-t-1");
    setup_lease(&conn, "L-1", "implementer", "token-1", 1, "agent-1", "sess-1", "model-1", 1e18);
    setup_step(&conn, "t-1", 1, "inspect", "pending", "");
    // 不 claim → step 无 binding。

    let base = Base { rcr_id, rcr_revision, rcr_hash, tc_id: "tc-t-1".to_string() };
    let err = submit_handoff(&mut conn, &frozen(), &handoff_key("h-no-binding"), &success_input(&base))
        .expect_err("step 无 verified binding 必须拒绝");
    assert_eq!(err.code, ERR_STEP_BINDING_INVALID);
    assert_eq!(count_handoff_events(&conn), 0);
}

#[test]
fn rejects_role_contract_stale_triple() {
    let mut conn = fresh_db();
    let base = setup_handoff_base(&mut conn, "reviewer", true);
    let mut input = success_input(&base);
    input.role_contract.revision = base.rcr_revision + 99; // ABA
    let err = submit_handoff(&mut conn, &frozen(), &handoff_key("h-aba-rc"), &input)
        .expect_err("role_contract 三元组不一致必须拒绝");
    assert_eq!(err.code, ERR_HANDOFF_CONTRACT_STALE);

    // 悬空 revision id 同样拒绝。
    let mut input = success_input(&base);
    input.role_contract.id = "rcr-does-not-exist".to_string();
    let err = submit_handoff(&mut conn, &frozen(), &handoff_key("h-aba-rc2"), &input)
        .expect_err("悬空 role_contract revision 必须拒绝");
    assert_eq!(err.code, ERR_HANDOFF_CONTRACT_STALE);
    assert_eq!(count_handoff_events(&conn), 0);
}

#[test]
fn rejects_handoff_to_mismatch() {
    let mut conn = fresh_db();
    let base = setup_handoff_base(&mut conn, "human", true); // handoff_to=human != reviewer
    let err = submit_handoff(&mut conn, &frozen(), &handoff_key("h-ho"), &success_input(&base))
        .expect_err("source Role Contract handoff_to 与 target_role 不一致必须拒绝");
    assert_eq!(err.code, ERR_HANDOFF_CONTRACT_STALE);
    assert_eq!(count_handoff_events(&conn), 0);
}

#[test]
fn rejects_task_contract_stale_triple() {
    let mut conn = fresh_db();
    let base = setup_handoff_base(&mut conn, "reviewer", true);
    let mut input = success_input(&base);
    input.task_contract.hash = "sha256:task-wrong".to_string(); // 与落库行不符
    let err = submit_handoff(&mut conn, &frozen(), &handoff_key("h-aba-tc"), &input)
        .expect_err("task_contract 三元组不一致必须拒绝");
    assert_eq!(err.code, ERR_HANDOFF_CONTRACT_STALE);
    assert_eq!(count_handoff_events(&conn), 0);
}

#[test]
fn rejects_role_contract_lineage_belongs_to_other_task() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    setup_task(&mut conn, "t-2");
    // role contract 属于 t-2 的 lineage；人为为 t-1 step 1 插入指向它的 binding 行
    // （链连续且 revision 存在，但 lineage 归属与 binding task 不一致 → UNVERIFIED）。
    let rcr_id = setup_contract(&mut conn, "t-2", "coder", "reviewer");
    let (rcr_revision, rcr_hash): (i64, String) = conn
        .query_row(
            "SELECT revision, role_contract_hash FROM role_contract_revisions \
             WHERE role_contract_revision_id = ?1",
            [&rcr_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    setup_task_contract(&conn, "t-1", "tc-t-1");
    setup_lease(&conn, "L-1", "implementer", "token-1", 1, "agent-1", "sess-1", "model-1", 1e18);
    setup_step(&conn, "t-1", 1, "inspect", "pending", "");
    conn.execute(
        "INSERT INTO task_step_role_contract_bindings \
         (binding_id, workspace_id, task_id, step_id, role_contract_lineage_id, \
          role_contract_revision_id, role_contract_revision, role_contract_hash, \
          canonicalization_version, canonicalization_rules_hash, binding_revision, \
          supersedes_binding_id, created_by, authoritative_created_at) \
         VALUES ('sb-t-1-1-cross', 1, 't-1', '1', 'rcl-t-2-coder', ?1, ?2, ?3, \
                 'role-contract-c14n/v1', 'rules-hash', 1, NULL, 'test', 't')",
        rusqlite::params![&rcr_id, rcr_revision, &rcr_hash],
    )
    .unwrap();

    let base = Base { rcr_id, rcr_revision, rcr_hash, tc_id: "tc-t-1".to_string() };
    let err = submit_handoff(&mut conn, &frozen(), &handoff_key("h-lineage"), &success_input(&base))
        .expect_err("lineage 归属不一致（projection UNVERIFIED）必须拒绝");
    assert_eq!(err.code, ERR_STEP_BINDING_INVALID);
    assert_eq!(count_handoff_events(&conn), 0);
}

#[test]
fn rejects_task_without_workspace_binding() {
    let mut conn = fresh_db();
    // t-no 从未 create（无 task_workspace_bindings 行）；其余字段照常解析。
    let input = HandoffInput {
        task_id: "t-no".to_string(),
        step_id: "1".to_string(),
        source_role: "executor".to_string(),
        target_role: "reviewer".to_string(),
        reason: "r".to_string(),
        outcome: "executor_ready_for_review".to_string(),
        task_contract: ContractTriple {
            id: "tc-1".to_string(),
            revision: 1,
            hash: "sha256:t".to_string(),
        },
        role_contract: ContractTriple {
            id: "rcr-1".to_string(),
            revision: 1,
            hash: "sha256:r".to_string(),
        },
        required_new_instance: false,
        required_new_session: false,
        acting_role: "implementer".to_string(),
        agent_id: "agent-1".to_string(),
        session_id: "sess-1".to_string(),
        model_id: "model-1".to_string(),
        lease_token: "token-1".to_string(),
        fencing_counter: 1,
        created_by: "test-handoff".to_string(),
    };
    let err = submit_handoff(&mut conn, &frozen(), &handoff_key("h-no-ws"), &input)
        .expect_err("无 task workspace binding 必须拒绝");
    assert_eq!(err.code, ERR_TASK_BINDING_REQUIRED);
    assert_eq!(count_handoff_events(&conn), 0);
}

// ---------------------------------------------------------------------------
// 幂等重放
// ---------------------------------------------------------------------------

#[test]
fn replay_same_key_does_not_duplicate_event() {
    let mut conn = fresh_db();
    let base = setup_handoff_base(&mut conn, "reviewer", true);
    let k = handoff_key("h-replay");
    let first = submit_handoff(&mut conn, &frozen(), &k, &success_input(&base))
        .expect("first handoff ok");
    let second = submit_handoff(&mut conn, &frozen(), &k, &success_input(&base))
        .expect("replay ok");
    assert_eq!(first, second, "幂等重放必须原样返回已保存的结果");
    assert_eq!(count_handoff_events(&conn), 1, "同 key 重放不得追加事件");
}

#[test]
fn different_key_same_payload_appends_second_event() {
    let mut conn = fresh_db();
    let base = setup_handoff_base(&mut conn, "reviewer", true);
    submit_handoff(&mut conn, &frozen(), &handoff_key("h-1"), &success_input(&base))
        .expect("first ok");
    submit_handoff(&mut conn, &frozen(), &handoff_key("h-2"), &success_input(&base))
        .expect("second ok");
    assert_eq!(count_handoff_events(&conn), 2);
}

// ---------------------------------------------------------------------------
// task.report 保留字段零写入拒绝（§4.4）
// ---------------------------------------------------------------------------

#[test]
fn report_reserved_fields_rejected_before_domain_writes() {
    let mut conn = fresh_db();
    for field in [
        "handoff",
        "target_role",
        "target_agent",
        "source_role",
        "handoff_reason",
        "required_new_instance",
        "required_new_session",
        "handoff_contract",
    ] {
        // 首次 canonical request 含任一保留字段 → E_HANDOFF_REQUIRES_TASK_HANDOFF。
        let mut params = serde_json::json!({"task_id": "t-1", "progress": "done"});
        params.as_object_mut().unwrap().insert(field.to_string(), serde_json::json!({}));
        let err = reject_report_reserved_fields(&serde_json::json!({"params": params}))
            .unwrap_err();
        assert_eq!(err.code, ERR_HANDOFF_REQUIRES_TASK_HANDOFF, "字段 {field}");
    }
    // 直接传 params（无包裹）同样拒绝。
    let err = reject_report_reserved_fields(&serde_json::json!({"target_role": "reviewer"}))
        .unwrap_err();
    assert_eq!(err.code, ERR_HANDOFF_REQUIRES_TASK_HANDOFF);
    // 无保留字段 → Ok。
    reject_report_reserved_fields(&serde_json::json!({"params": {
        "task_id": "t-1", "progress": "done", "evidence": [1, 2],
    }}))
    .expect("无保留字段必须放行");
}
