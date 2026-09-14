//! RP-03 authority context domain 测试（`task_prompt_context` 验收过滤词）。
//!
//! 覆盖（RP-03 卡 check items）：
//! - READY/CLAIM happy path：authority/routing/contract/authorization 全聚合；
//! - 同 snapshot 确定性：两次聚合结果逐字段一致；
//! - 零写入：聚合前后任务/lease/事件/binding/合同行数不变；
//! - fail-closed：task 不存在 → `E_TASK_PROMPT_TASK_NOT_FOUND`；
//!   binding 缺失 / capture 复核失败 → `E_TASK_PROMPT_AUTHORITY_UNAVAILABLE`；
//! - closed task → COMPLETE → `non_actionable`；
//! - identity policy 三态（resolved / not_applicable）。
//!
//! fixture 复用 1A/1B/claim 的 crate 内公共 API（与 next_action_test 同构），
//! 因 allowed paths 限制在本卡内自持，不改写既有测试模块。

use rusqlite::Connection;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::daemon::task_loop::claim::{claim_step, ClaimStepInput, LedgerKey as ClaimLedgerKey};
use crate::daemon::task_loop::contract_set::{
    set_task_contract, ContractPayload, LedgerKey as ContractLedgerKey, SetContractInput,
};
use crate::daemon::task_loop::create::{
    create_task, CreateTaskInput, LedgerKey as CreateLedgerKey, WorkspaceCaptureInput,
};
use crate::daemon::task_loop::types::FrozenAuthorityInput;
use super::context::{
    aggregate_authority_context, E_TASK_PROMPT_AUTHORITY_UNAVAILABLE,
    E_TASK_PROMPT_TASK_NOT_FOUND,
};

/// 开启内存 task-DB 并跑一遍 migration。
fn fresh_db() -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    crate::sqlite_query::migrate_connection(&conn).expect("migration");
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

/// 复用 1A `create_task` 建立 task + 不可变 workspace binding（ws-inst-1）。
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

/// 复用 1B `set_task_contract` 建立 Role Contract lineage；返回 revision id。
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

/// 插入 Task Contract revision 行（envelope 带 identity_policy → resolved）。
fn setup_task_contract_with_policy(conn: &Connection, task_id: &str, contract_id: &str) {
    let norm_hash: String = conn
        .query_row(
            "SELECT rules_hash FROM verdict_normalization_rules \
             WHERE normalization_version = 'verdict-normalization/v1'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    conn.execute(
        "INSERT INTO task_contract_revisions \
         (contract_id, revision, contract_hash, profile, task_id, workspace_id, \
          envelope_payload, created_at, created_by, \
          normalization_version, normalization_rules_hash) \
         VALUES (?1, 1, 'sha256:task-1', 'review', ?2, 1, ?3, 0.0, 'test', \
                 'verdict-normalization/v1', ?4)",
        rusqlite::params![
            contract_id,
            task_id,
            "{\"objective\":\"t\",\"identity_policy\":\"legacy_identity_v1\"}",
            norm_hash,
        ],
    )
    .unwrap();
}

/// 插入 Task Contract revision 行（envelope 无 identity_policy → unresolved）。
fn setup_task_contract_without_policy(conn: &Connection, task_id: &str, contract_id: &str) {
    let norm_hash: String = conn
        .query_row(
            "SELECT rules_hash FROM verdict_normalization_rules \
             WHERE normalization_version = 'verdict-normalization/v1'",
            [],
            |row| row.get(0),
        )
        .unwrap();
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

/// claim 到 step（创建 verified binding）。
fn setup_binding(
    conn: &mut Connection,
    task_id: &str,
    step_id: &str,
    rcr_id: &str,
    request_id: &str,
) {
    claim_step(
        conn,
        &frozen(),
        &ClaimLedgerKey {
            workspace_instance_id: "ws-inst-1".to_string(),
            method: "task.claim".to_string(),
            request_id: request_id.to_string(),
        },
        &ClaimStepInput {
            task_id: task_id.to_string(),
            step_id: step_id.to_string(),
            role_contract_revision_id: rcr_id.to_string(),
            remediation_step_id: String::new(),
            created_by: "test-claimer".to_string(),
        },
    )
    .expect("setup claim 应成功");
}

fn count(conn: &Connection, table: &str) -> i64 {
    conn.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| row.get(0))
        .unwrap()
}

/// READY/CLAIM 全量 fixture。
fn setup_ready_claim_task(conn: &mut Connection, task_id: &str) {
    setup_task(conn, task_id);
    let rcr = setup_contract(conn, task_id, "implementer");
    setup_task_contract_with_policy(conn, task_id, &format!("tc-{task_id}"));
    setup_step(conn, task_id, 1, "implement", "pending");
    setup_binding(conn, task_id, "1", &rcr, &format!("req-c-{task_id}"));
}

// ---------------------------------------------------------------------------
// 正向场景
// ---------------------------------------------------------------------------

#[test]
fn task_prompt_context_aggregates_ready_claim_snapshot() {
    let mut conn = fresh_db();
    setup_ready_claim_task(&mut conn, "t-1");

    let ctx = aggregate_authority_context(&conn, "ws-inst-1", "t-1").expect("聚合应成功");

    assert_eq!(ctx["schema_version"], json!("role_prompt_context_v1"));
    assert_eq!(ctx["task_id"], json!("t-1"));

    let authority = &ctx["authority"];
    assert_eq!(authority["workspace_id"], json!(1));
    assert_eq!(authority["workspace_instance_id"], json!("ws-inst-1"));
    let binding_id = authority["workspace_binding_id"].as_str().unwrap();
    assert!(binding_id.starts_with("tb-"), "binding id 来自 task_workspace_bindings");
    assert!(!authority["workspace_capture_id"].as_str().unwrap().is_empty());
    assert!(authority["snapshot_id"].is_null(), "v1 snapshot_id 允许为 null");
    assert_eq!(authority["source_event_watermark"], json!(0));

    let routing = &ctx["routing"];
    assert_eq!(routing["decision"], json!("READY"));
    assert_eq!(routing["action"], json!("CLAIM"));
    assert_eq!(routing["required_role"], json!("executor"));
    assert_eq!(routing["step_id"], json!("1"));
    assert!(!routing["next_action"].is_null());

    let contract = &ctx["contract"];
    assert_eq!(contract["task_contract_id"], json!("tc-t-1"));
    assert_eq!(contract["task_contract_revision"], json!(1));
    assert_eq!(contract["task_contract_hash"], json!("sha256:task-1"));
    assert!(!contract["role_contract_revision_id"].is_null());
    assert!(!contract["role_contract_hash"].is_null());
    assert_eq!(contract["role_contract_prompt_template_id"], json!("pt-1"));
    assert_eq!(contract["role_contract_prompt_hash"], json!("ph-1"));
    assert_eq!(contract["identity_policy_status"], json!("resolved"));

    let authorization = &ctx["authorization"];
    assert_eq!(authorization["routing_state"], json!("action_ready"));
    assert_eq!(authorization["valid_for_claim"], json!(false));
    assert_eq!(authorization["mutation_recheck_required"], json!(true));
}

#[test]
fn task_prompt_context_is_deterministic_within_same_snapshot() {
    let mut conn = fresh_db();
    setup_ready_claim_task(&mut conn, "t-1");

    let first = aggregate_authority_context(&conn, "ws-inst-1", "t-1").unwrap();
    let second = aggregate_authority_context(&conn, "ws-inst-1", "t-1").unwrap();

    assert_eq!(first, second, "同一 snapshot 无写入时聚合必须逐字段一致");
}

#[test]
fn task_prompt_context_is_read_only_no_mutation_trace() {
    let mut conn = fresh_db();
    setup_ready_claim_task(&mut conn, "t-1");

    let tables = [
        "tasks",
        "task_steps",
        "task_leases",
        "task_events",
        "task_workspace_bindings",
        "task_contract_revisions",
        "role_contract_revisions",
        "task_verdict_events",
    ];
    let before: Vec<i64> = tables.iter().map(|t| count(&conn, t)).collect();

    aggregate_authority_context(&conn, "ws-inst-1", "t-1").unwrap();

    for (table, before_count) in tables.iter().zip(before) {
        assert_eq!(count(&conn, table), before_count, "{table} 不得被聚合写入");
    }
}

#[test]
fn task_prompt_context_closed_task_is_non_actionable() {
    let mut conn = fresh_db();
    setup_ready_claim_task(&mut conn, "t-1");
    conn.execute("UPDATE tasks SET status = 'closed' WHERE id = 't-1'", [])
        .unwrap();

    let ctx = aggregate_authority_context(&conn, "ws-inst-1", "t-1").unwrap();

    assert_eq!(ctx["routing"]["decision"], json!("COMPLETE"));
    assert_eq!(ctx["authorization"]["routing_state"], json!("non_actionable"));
    assert_eq!(ctx["authorization"]["valid_for_claim"], json!(false));
}

#[test]
fn task_prompt_context_policy_unresolved_and_not_applicable() {
    // unresolved：有 task contract revision 但 envelope 无 identity_policy。
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let rcr = setup_contract(&mut conn, "t-1", "implementer");
    setup_task_contract_without_policy(&conn, "t-1", "tc-t-1");
    setup_step(&conn, "t-1", 1, "implement", "pending");
    setup_binding(&mut conn, "t-1", "1", &rcr, "req-c-unresolved");
    let ctx = aggregate_authority_context(&conn, "ws-inst-1", "t-1").unwrap();
    assert_eq!(ctx["contract"]["identity_policy_status"], json!("unresolved"));

    // not_applicable：无任何 task contract revision。
    let mut conn2 = fresh_db();
    setup_task(&mut conn2, "t-2");
    let rcr2 = setup_contract(&mut conn2, "t-2", "implementer");
    setup_step(&conn2, "t-2", 1, "implement", "pending");
    setup_binding(&mut conn2, "t-2", "1", &rcr2, "req-c-nocontract");
    let ctx2 = aggregate_authority_context(&conn2, "ws-inst-1", "t-2").unwrap();
    assert_eq!(
        ctx2["contract"]["identity_policy_status"],
        json!("not_applicable")
    );
}

// ---------------------------------------------------------------------------
// fail-closed 场景
// ---------------------------------------------------------------------------

#[test]
fn task_prompt_context_missing_task_fails_closed() {
    let conn = fresh_db();
    let err = aggregate_authority_context(&conn, "ws-inst-1", "t-missing").unwrap_err();
    assert_eq!(err.code, E_TASK_PROMPT_TASK_NOT_FOUND);
}

#[test]
fn task_prompt_context_missing_binding_fails_closed() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    conn.execute("DELETE FROM task_workspace_bindings WHERE task_id = 't-1'", [])
        .unwrap();

    let err = aggregate_authority_context(&conn, "ws-inst-1", "t-1").unwrap_err();
    assert_eq!(err.code, E_TASK_PROMPT_AUTHORITY_UNAVAILABLE);
}

#[test]
fn task_prompt_context_capture_mismatch_fails_closed() {
    let mut conn = fresh_db();
    setup_ready_claim_task(&mut conn, "t-1");

    // 用另一个 workspace instance 调用：capture 无法对当前 authority 复核。
    let err = aggregate_authority_context(&conn, "ws-inst-other", "t-1").unwrap_err();
    assert_eq!(err.code, E_TASK_PROMPT_AUTHORITY_UNAVAILABLE);
}

// 反泄露：错误 wire 上不携带本地路径或 token（spec §10.2 details 纪律）。
#[test]
fn task_prompt_context_errors_do_not_leak_secrets() {
    let conn = fresh_db();
    let err = aggregate_authority_context(&conn, "ws-inst-1", "t-missing").unwrap_err();
    let rendered = format!("{} {}", err.code, err.message);
    assert!(!rendered.contains("token"), "错误信息不得含 token 字样");
    assert!(!rendered.contains('/'), "错误信息不得含路径分隔符");
    let _ = Value::Null;
}
