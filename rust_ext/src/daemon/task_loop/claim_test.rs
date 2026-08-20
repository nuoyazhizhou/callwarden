//! 1C `claim.rs` 领域测试（cw-role-handoff-task-loop.md §8.1.4 / §3.4）。
//!
//! 覆盖：首次 binding 写入（binding_revision=1）、重绑追加（=2 + supersedes）、
//! 无 task binding 拒绝（E_TASK_BINDING_REQUIRED）、step 不属于 task 拒绝、
//! revision 悬空拒绝、跨 task lineage 拒绝、binding 链断链拒绝、
//! remediation 精确领取（E_REMEDIATION_STEP_REQUIRED / MISMATCH / 精确命中）、
//! fail-closed read projection（verified / UNVERIFIED）、幂等重放。

use rusqlite::Connection;

use crate::sqlite_query::migrate_connection;
use super::claim::{
    read_current_binding, claim_step, ClaimStepInput, LedgerKey, ERR_REMEDIATION_STEP_MISMATCH,
    ERR_REMEDIATION_STEP_REQUIRED, ERR_STEP_BINDING_INVALID, ERR_TASK_BINDING_REQUIRED,
};
use super::contract_set::{
    set_task_contract, ContractPayload, LedgerKey as ContractLedgerKey, SetContractInput,
};
use super::create::{create_task, CreateTaskInput, LedgerKey as CreateLedgerKey, WorkspaceCaptureInput};
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

/// 复用 1A `create_task` 建立 task + 不可变 workspace binding（依赖链 1A → 1C）。
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

/// 复用 1B `set_task_contract` 建立 lineage + revision（依赖链 1B → 1C）。
/// 返回 revision id（如 rcr-t-1-coder-r1）。
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
        handoff_to: "human".to_string(),
        independence: serde_json::json!({"max_tokens": 100}),
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

/// 追加 revision（同一 lineage 的 r2）供重绑测试。
fn setup_contract_v2(conn: &mut Connection, task_id: &str, role: &str) -> String {
    let mut payload = ContractPayload {
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
        handoff_to: "human".to_string(),
        independence: serde_json::json!({"max_tokens": 100}),
    };
    payload.commands = vec!["echo".to_string(), "fmt".to_string()];
    let resp = set_task_contract(
        conn,
        &frozen(),
        &ContractLedgerKey {
            workspace_instance_id: "ws-inst-1".to_string(),
            method: "task.contract_set".to_string(),
            request_id: format!("contract-{task_id}-{role}-v2"),
        },
        &SetContractInput {
            task_id: task_id.to_string(),
            contract: payload,
            created_by: "test-owner".to_string(),
        },
    )
    .expect("setup contract_set v2 应成功");
    resp["role_contract_revision_id"]
        .as_str()
        .unwrap()
        .to_string()
}

/// 插入一个属于 task 的步骤（id 用整数；claim 的 step_id 以字符串比对，SQLite 亲和转换匹配）。
fn setup_step(conn: &Connection, task_id: &str, step_id: i64, action: &str, status: &str, result: &str) {
    conn.execute(
        "INSERT INTO task_steps \
         (id, task_id, step_index, action, status, result, created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, 0.0)",
        rusqlite::params![step_id, task_id, step_id, action, status, result],
    )
    .unwrap();
}

fn claim_input(task_id: &str, step_id: i64, revision_id: &str) -> ClaimStepInput {
    ClaimStepInput {
        task_id: task_id.to_string(),
        step_id: step_id.to_string(),
        role_contract_revision_id: revision_id.to_string(),
        remediation_step_id: String::new(),
        created_by: "test-claimer".to_string(),
    }
}

fn claim_key(request_id: &str) -> LedgerKey {
    LedgerKey {
        workspace_instance_id: "ws-inst-1".to_string(),
        method: "task.claim".to_string(),
        request_id: request_id.to_string(),
    }
}

fn count(conn: &Connection, table: &str) -> i64 {
    conn.query_row(
        &format!("SELECT COUNT(*) FROM {table}"),
        [],
        |row| row.get(0),
    )
    .unwrap()
}

#[test]
fn from_params_rejects_missing_fields() {
    // 缺 task_id
    let err = ClaimStepInput::from_params(&serde_json::json!({})).unwrap_err();
    assert_eq!(err.code, "invalid_params");
    // 缺 step_id
    let err = ClaimStepInput::from_params(&serde_json::json!({"task_id": "t-1"})).unwrap_err();
    assert_eq!(err.code, "invalid_params");
    // 缺 role_contract_revision_id
    let err = ClaimStepInput::from_params(&serde_json::json!({
        "task_id": "t-1", "step_id": "1"
    }))
    .unwrap_err();
    assert_eq!(err.code, "invalid_params");
    // 可选字段缺省 → 空字符串
    let parsed = ClaimStepInput::from_params(&serde_json::json!({
        "task_id": "t-1", "step_id": "1", "role_contract_revision_id": "rcr-1"
    }))
    .unwrap();
    assert!(parsed.remediation_step_id.is_empty());
    assert!(parsed.created_by.is_empty());
}

#[test]
fn first_claim_writes_binding_revision_1() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let r1 = setup_contract(&mut conn, "t-1", "coder");
    setup_step(&conn, "t-1", 1, "inspect", "pending", "");

    let resp = claim_step(&mut conn, &frozen(), &claim_key("req-1"), &claim_input("t-1", 1, &r1))
        .expect("首次 claim 应成功");
    assert_eq!(resp["ok"].as_bool(), Some(true));
    assert_eq!(resp["binding_id"], "sb-t-1-1-r1");
    assert_eq!(resp["binding_revision"], 1);
    assert_eq!(resp["workspace_id"], 1);
    assert_eq!(resp["role_contract_revision_id"], r1);
    assert!(resp["supersedes_binding_id"].is_null(), "revision 1 的 supersedes 必须为 NULL");

    // 表内只落一条不可变 binding，provenance 与 lineage 一致。
    assert_eq!(count(&conn, "task_step_role_contract_bindings"), 1);
    let (lineage_id, lineage_task, lineage_ws): (String, String, i64) = conn
        .query_row(
            "SELECT role_contract_lineage_id, task_id, workspace_id \
             FROM task_step_role_contract_bindings WHERE binding_id = 'sb-t-1-1-r1'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    assert_eq!(lineage_task, "t-1");
    assert_eq!(lineage_ws, 1);
    assert_eq!(lineage_id, "rcl-t-1-coder");

    // fail-closed read projection：verified。
    let proj = read_current_binding(&conn, 1, "t-1", "1").expect("读取失败").expect("应 verified");
    assert_eq!(proj.binding_revision, 1);
    assert_eq!(proj.binding_id, "sb-t-1-1-r1");
    assert_eq!(proj.role_contract_revision_id, r1);
    assert!(proj.supersedes_binding_id.is_none());
}

#[test]
fn second_claim_appends_revision_2_superseding_1() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let r1 = setup_contract(&mut conn, "t-1", "coder");
    let r2 = setup_contract_v2(&mut conn, "t-1", "coder");
    setup_step(&conn, "t-1", 1, "inspect", "pending", "");

    claim_step(&mut conn, &frozen(), &claim_key("req-1"), &claim_input("t-1", 1, &r1))
        .expect("首次 claim ok");
    let resp2 = claim_step(&mut conn, &frozen(), &claim_key("req-2"), &claim_input("t-1", 1, &r2))
        .expect("重绑追加应成功");
    assert_eq!(resp2["binding_revision"], 2);
    assert_eq!(resp2["binding_id"], "sb-t-1-1-r2");
    assert_eq!(resp2["supersedes_binding_id"], "sb-t-1-1-r1");

    // 追加而非 UPDATE：两条 binding 都保留，链连续。
    assert_eq!(count(&conn, "task_step_role_contract_bindings"), 2);
    let (revision, supersedes): (i64, Option<String>) = conn
        .query_row(
            "SELECT binding_revision, supersedes_binding_id \
             FROM task_step_role_contract_bindings WHERE binding_id = 'sb-t-1-1-r2'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!((revision, supersedes.as_deref()), (2, Some("sb-t-1-1-r1")));

    // current binding = r2。
    let proj = read_current_binding(&conn, 1, "t-1", "1").expect("读取失败").expect("应 verified");
    assert_eq!(proj.binding_revision, 2);
    assert_eq!(proj.role_contract_revision_id, r2);
}

#[test]
fn claim_rejects_task_without_binding() {
    let mut conn = fresh_db();
    // t-no 从未 create（无 task_workspace_bindings 行）。
    let err = claim_step(
        &mut conn,
        &frozen(),
        &claim_key("req-no"),
        &claim_input("t-no", 1, "rcr-t-1-coder-r1"),
    )
    .expect_err("无 binding 的 task 必须确定性拒绝");
    assert_eq!(err.code, ERR_TASK_BINDING_REQUIRED);
    assert_eq!(count(&conn, "task_step_role_contract_bindings"), 0);
}

#[test]
fn claim_rejects_step_not_owned_by_task() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let r1 = setup_contract(&mut conn, "t-1", "coder");
    // step 2 属于其他 task（t-other），不属于 t-1。
    setup_task(&mut conn, "t-other");
    setup_step(&conn, "t-other", 2, "inspect", "pending", "");

    let err = claim_step(&mut conn, &frozen(), &claim_key("req-x"), &claim_input("t-1", 2, &r1))
        .expect_err("step 不属于 task 必须拒绝");
    assert_eq!(err.code, ERR_STEP_BINDING_INVALID);
    assert_eq!(count(&conn, "task_step_role_contract_bindings"), 0);
}

#[test]
fn claim_rejects_missing_revision() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    setup_step(&conn, "t-1", 1, "inspect", "pending", "");

    let err = claim_step(
        &mut conn,
        &frozen(),
        &claim_key("req-x"),
        &claim_input("t-1", 1, "rcr-does-not-exist"),
    )
    .expect_err("悬空 revision 必须拒绝");
    assert_eq!(err.code, ERR_STEP_BINDING_INVALID);
    assert_eq!(count(&conn, "task_step_role_contract_bindings"), 0);
}

#[test]
fn claim_rejects_cross_task_lineage() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    setup_task(&mut conn, "t-2");
    let r2 = setup_contract(&mut conn, "t-2", "coder"); // lineage 归属 t-2
    setup_step(&conn, "t-1", 1, "inspect", "pending", "");

    let err = claim_step(&mut conn, &frozen(), &claim_key("req-x"), &claim_input("t-1", 1, &r2))
        .expect_err("revision lineage 跨 task 必须拒绝");
    assert_eq!(err.code, ERR_STEP_BINDING_INVALID);
    assert_eq!(count(&conn, "task_step_role_contract_bindings"), 0);
}

#[test]
fn claim_rejects_broken_binding_chain() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let r1 = setup_contract(&mut conn, "t-1", "coder");
    setup_step(&conn, "t-1", 1, "inspect", "pending", "");
    claim_step(&mut conn, &frozen(), &claim_key("req-1"), &claim_input("t-1", 1, &r1))
        .expect("首次 claim ok");

    // 人为制造断链：直接插入 binding_revision=3（链 1,3 → count=2 max=3）。
    conn.execute(
        "INSERT INTO task_step_role_contract_bindings \
         (binding_id, workspace_id, task_id, step_id, role_contract_lineage_id, \
          role_contract_revision_id, role_contract_revision, role_contract_hash, \
          canonicalization_version, canonicalization_rules_hash, binding_revision, \
          supersedes_binding_id, created_by, authoritative_created_at) \
         VALUES ('sb-t-1-1-r3', 1, 't-1', '1', 'rcl-t-1-coder', ?1, 1, 'sha256:0', \
                 'role-contract-c14n/v1', 'rules-hash', 3, NULL, 'test', 't')",
        [&r1],
    )
    .unwrap();

    let err = claim_step(&mut conn, &frozen(), &claim_key("req-2"), &claim_input("t-1", 1, &r1))
        .expect_err("断链必须拒绝追加（UNVERIFIED）");
    assert_eq!(err.code, ERR_STEP_BINDING_INVALID);
    assert_eq!(count(&conn, "task_step_role_contract_bindings"), 2, "不得追加新 binding");

    // fail-closed read projection：断链 → None。
    assert!(read_current_binding(&conn, 1, "t-1", "1").expect("读取失败").is_none());
}

#[test]
fn read_projection_unverified_when_lineage_mismatch() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    setup_task(&mut conn, "t-2");
    let r1 = setup_contract(&mut conn, "t-1", "coder");
    let r2 = setup_contract(&mut conn, "t-2", "coder");
    setup_step(&conn, "t-1", 1, "inspect", "pending", "");
    claim_step(&mut conn, &frozen(), &claim_key("req-1"), &claim_input("t-1", 1, &r1))
        .expect("首次 claim ok");

    // 人为改写 binding 指向 t-2 的 revision/lineage（存在但跨 task）。
    conn.execute(
        "UPDATE task_step_role_contract_bindings \
         SET role_contract_lineage_id = 'rcl-t-2-coder', role_contract_revision_id = ?1 \
         WHERE binding_id = 'sb-t-1-1-r1'",
        [&r2],
    )
    .unwrap();

    // revision 存在、链连续，但 lineage task 与 binding task 不一致 → UNVERIFIED。
    assert!(read_current_binding(&conn, 1, "t-1", "1").expect("读取失败").is_none());
}

#[test]
fn remediation_requires_explicit_step() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let r1 = setup_contract(&mut conn, "t-1", "coder");
    // step 1 失败未解析；step 2 是其 fix_defect 整改（pending）。
    setup_step(&conn, "t-1", 1, "inspect", "failed", "");
    setup_step(&conn, "t-1", 2, "fix_defect", "pending", r#"{"remediation_of_step_id":"1"}"#);

    let mut input = claim_input("t-1", 2, &r1);
    let err = claim_step(&mut conn, &frozen(), &claim_key("req-1"), &input)
        .expect_err("存在 unresolved failed step 且未提供 remediation_step_id 必须 REQUIRED");
    assert_eq!(err.code, ERR_REMEDIATION_STEP_REQUIRED);
    assert_eq!(count(&conn, "task_step_role_contract_bindings"), 0);

    // 提供了但指向错误的步骤 → MISMATCH。
    input.remediation_step_id = "1".to_string();
    let err = claim_step(&mut conn, &frozen(), &claim_key("req-2"), &input)
        .expect_err("不是当前 remediation 必须 MISMATCH");
    assert_eq!(err.code, ERR_REMEDIATION_STEP_MISMATCH);

    // 提供正确的 remediation_step_id 但 step_id 不是 remediation 步骤本身 → MISMATCH。
    let mut wrong_step = claim_input("t-1", 1, &r1);
    wrong_step.remediation_step_id = "2".to_string();
    let err = claim_step(&mut conn, &frozen(), &claim_key("req-3"), &wrong_step)
        .expect_err("remediation 必须精确领取 remediation 步骤本身");
    assert_eq!(err.code, ERR_REMEDIATION_STEP_MISMATCH);
}

#[test]
fn remediation_exact_claim_succeeds() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let r1 = setup_contract(&mut conn, "t-1", "coder");
    setup_step(&conn, "t-1", 1, "inspect", "failed", "");
    setup_step(&conn, "t-1", 2, "fix_defect", "pending", r#"{"remediation_of_step_id":"1"}"#);

    let mut input = claim_input("t-1", 2, &r1);
    input.remediation_step_id = "2".to_string();
    let resp = claim_step(&mut conn, &frozen(), &claim_key("req-1"), &input)
        .expect("精确领取 remediation 应成功");
    assert_eq!(resp["ok"].as_bool(), Some(true));
    assert_eq!(resp["step_id"], "2");
    assert_eq!(resp["binding_id"], "sb-t-1-2-r1");
    assert_eq!(count(&conn, "task_step_role_contract_bindings"), 1);

    // 同一 (task, remediation_step, request_id) 幂等：同一 key 重放不重复写入。
    let replay = claim_step(&mut conn, &frozen(), &claim_key("req-1"), &input)
        .expect("幂等重放应成功");
    assert_eq!(replay, resp);
    assert_eq!(count(&conn, "task_step_role_contract_bindings"), 1);
}

#[test]
fn no_remediation_but_provided_remediation_id_rejected() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let r1 = setup_contract(&mut conn, "t-1", "coder");
    setup_step(&conn, "t-1", 1, "inspect", "pending", "");

    let mut input = claim_input("t-1", 1, &r1);
    input.remediation_step_id = "1".to_string();
    let err = claim_step(&mut conn, &frozen(), &claim_key("req-1"), &input)
        .expect_err("无待处理 remediation 却提供 remediation_step_id 必须 MISMATCH");
    assert_eq!(err.code, ERR_REMEDIATION_STEP_MISMATCH);
    assert_eq!(count(&conn, "task_step_role_contract_bindings"), 0);
}

#[test]
fn replay_of_committed_key_does_not_dup() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let r1 = setup_contract(&mut conn, "t-1", "coder");
    setup_step(&conn, "t-1", 1, "inspect", "pending", "");

    let k = claim_key("req-replay");
    let first = claim_step(&mut conn, &frozen(), &k, &claim_input("t-1", 1, &r1)).expect("first ok");
    let second = claim_step(&mut conn, &frozen(), &k, &claim_input("t-1", 1, &r1)).expect("replay ok");
    assert_eq!(first, second, "幂等重放必须原样返回已保存的结果");
    assert_eq!(count(&conn, "task_step_role_contract_bindings"), 1);
}

#[test]
fn deterministic_reject_persists_replayable_ledger_error() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    setup_step(&conn, "t-1", 1, "inspect", "pending", "");

    let err = claim_step(
        &mut conn,
        &frozen(),
        &claim_key("req-bad"),
        &claim_input("t-1", 1, "rcr-missing"),
    )
    .expect_err("悬空 revision 必须拒绝");
    assert_eq!(err.code, ERR_STEP_BINDING_INVALID);
    assert_eq!(count(&conn, "task_step_role_contract_bindings"), 0);

    // 拒绝也落可重放 ledger error（savepoint 选择性回滚 + 提交 error）。
    let stored: String = conn
        .query_row(
            "SELECT response_or_error_json FROM task_operation_ledger WHERE request_id = 'req-bad'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let stored_json: serde_json::Value = serde_json::from_str(&stored).unwrap();
    assert_eq!(stored_json["ok"].as_bool(), Some(false));
    assert_eq!(stored_json["code"], ERR_STEP_BINDING_INVALID);
}
