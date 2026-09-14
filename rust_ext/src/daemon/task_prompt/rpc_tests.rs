//! RP-05 `task.prompt.compile` RPC handler 测试（`task_prompt_rpc` 验收过滤词）。
//!
//! 覆盖（RP-05 卡 check items）：
//! - §4.1 严格 request schema：未知字段 / 缺失或非法 task_id →
//!   `E_TASK_PROMPT_TASK_ID_REQUIRED`（fail closed）；
//! - workspace 自解析：task 不存在 → `E_TASK_PROMPT_TASK_NOT_FOUND`；
//!   binding 缺失 → `E_TASK_PROMPT_AUTHORITY_UNAVAILABLE`；
//! - §4.2 guard：`expected_workspace_instance_id` mismatch →
//!   `E_TASK_PROMPT_AUTHORITY_MISMATCH`；一致时通过；
//! - READY/CLAIM happy path：bundle schema/valid_for_claim=false/hash 字段
//!   完整，不可信 title/description 只进入第 3 段 canonical JSON 块；
//! - 零写入：8 张任务/lease/事件/binding/合同表计数不变；
//! - 同 snapshot 确定性：两次 compile 的 context/prompt/bundle hash 一致；
//! - §11.3 capability 投影：schema version、manifest hash、policy hash、
//!   enabled 状态，hash 为 lowercase `sha256:` 规范形式。
//!
//! fixture 与 context_tests 同构（1A create_task / 1B set_task_contract /
//! claim_step crate 内公共 API），因 allowed paths 限制在本卡内自持。

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
use crate::daemon::dispatch::DaemonRpcError;
use crate::daemon::task_prompt::context::E_TASK_PROMPT_AUTHORITY_UNAVAILABLE;
use super::handler::{
    capability_projection, compile, E_TASK_PROMPT_AUTHORITY_MISMATCH,
    E_TASK_PROMPT_TASK_ID_REQUIRED,
};
use crate::daemon::task_prompt::context::E_TASK_PROMPT_TASK_NOT_FOUND;

/// §7.2 legacy executor 模板冻结 content hash（manifest 权威，与
/// render_tests::EXEC_V1_HASH 一致；自持避免跨测试模块依赖）。
const EXEC_V1_HASH: &str =
    "sha256:59a459f7786097c671d48fbeec6e361c12d7a95bdec4e3722169d68d5d6a73f6";

fn err_code(e: &DaemonRpcError) -> String {
    e.code.clone()
}

/// 开启内存 task-DB 并跑一遍 migration + workspace 行。
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

/// 1A `create_task`：task + 不可变 workspace binding（ws-inst-1）。
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
        title: format!("task-title-{task_id}"),
        description: format!("task-desc-{task_id} & <script>"),
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

/// 1B `set_task_contract`：Role Contract lineage（executor v1 真实模板三元组）。
fn setup_contract(conn: &mut Connection, task_id: &str, role: &str) -> String {
    let payload = ContractPayload {
        role: role.to_string(),
        skill_id: "skill-1".to_string(),
        skill_version: "1.0".to_string(),
        prompt_template_id: "cw.aprime.executor.startup.v1".to_string(),
        prompt_hash: EXEC_V1_HASH.to_string(),
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

/// Task Contract revision 行（envelope 带 identity_policy → resolved）。
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

fn req_params(task_id: &str) -> Value {
    json!({ "task_id": task_id })
}

// ---------------------------------------------------------------------------
// §4.1 严格 request schema
// ---------------------------------------------------------------------------

#[test]
fn task_prompt_rpc_rejects_unknown_fields() {
    let conn = fresh_db();
    for extra in [
        json!({ "task_id": "t-1", "role": "executor" }),
        json!({ "task_id": "t-1", "mode": "llm" }),
        json!({ "task_id": "t-1", "format": "json" }),
        json!({ "task_id": "t-1", "context_profile": "full" }),
        json!({ "task_id": "t-1", "preview_role": "reviewer" }),
        json!({ "task_id": "t-1", "lease_token": "x" }),
        json!({ "task_id": "t-1", "workspace_id": 1 }),
    ] {
        let err = compile(&conn, &extra).unwrap_err();
        assert_eq!(err_code(&err), E_TASK_PROMPT_TASK_ID_REQUIRED, "extra: {extra}");
    }
}

#[test]
fn task_prompt_rpc_requires_non_empty_string_task_id() {
    let conn = fresh_db();
    for bad in [
        json!({}),
        json!({ "task_id": "" }),
        json!({ "task_id": "   " }),
        json!({ "task_id": 123 }),
        json!([]),
        json!(null),
    ] {
        let err = compile(&conn, &bad).unwrap_err();
        assert_eq!(err_code(&err), E_TASK_PROMPT_TASK_ID_REQUIRED, "bad: {bad}");
    }
}

// ---------------------------------------------------------------------------
// workspace 自解析 + fail-closed
// ---------------------------------------------------------------------------

#[test]
fn task_prompt_rpc_task_not_found_fail_closed() {
    let conn = fresh_db();
    let err = compile(&conn, &req_params("t-missing")).unwrap_err();
    assert_eq!(err_code(&err), E_TASK_PROMPT_TASK_NOT_FOUND);
}

#[test]
fn task_prompt_rpc_missing_binding_fail_closed() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    conn.execute("DELETE FROM task_workspace_bindings WHERE task_id = 't-1'", [])
        .unwrap();
    let err = compile(&conn, &req_params("t-1")).unwrap_err();
    assert_eq!(err_code(&err), E_TASK_PROMPT_AUTHORITY_UNAVAILABLE);
}

// ---------------------------------------------------------------------------
// §4.2 workspace guard
// ---------------------------------------------------------------------------

#[test]
fn task_prompt_rpc_workspace_guard_mismatch_fails_closed() {
    let mut conn = fresh_db();
    setup_ready_claim_task(&mut conn, "t-1");
    let err = compile(
        &conn,
        &json!({ "task_id": "t-1", "expected_workspace_instance_id": "ws-stale" }),
    )
    .unwrap_err();
    assert_eq!(err_code(&err), E_TASK_PROMPT_AUTHORITY_MISMATCH);
}

#[test]
fn task_prompt_rpc_workspace_guard_match_passes() {
    let mut conn = fresh_db();
    setup_ready_claim_task(&mut conn, "t-1");
    let out = compile(
        &conn,
        &json!({ "task_id": "t-1", "expected_workspace_instance_id": "ws-inst-1" }),
    )
    .expect("guard 一致应通过");
    assert_eq!(out["schema_version"], json!("role_prompt_bundle_v1"));
}

// ---------------------------------------------------------------------------
// READY/CLAIM happy path
// ---------------------------------------------------------------------------

#[test]
fn task_prompt_rpc_happy_round_trip_ready_claim() {
    let mut conn = fresh_db();
    setup_ready_claim_task(&mut conn, "t-1");

    let out = compile(&conn, &req_params("t-1")).expect("compile 应成功");

    assert_eq!(out["schema_version"], json!("role_prompt_bundle_v1"));
    assert_eq!(out["task_id"], json!("t-1"));
    assert_eq!(out["routing"]["decision"], json!("READY"));
    assert_eq!(out["routing"]["action"], json!("CLAIM"));
    // §4.1-6 / RP-03 authorization：bundle 永不充当 fencing 票据。
    assert_eq!(out["authorization"]["valid_for_claim"], json!(false));
    assert_eq!(out["authorization"]["mutation_recheck_required"], json!(true));
    for key in ["context_hash", "bundle_hash"] {
        let h = out[key].as_str().expect("hash field present");
        assert!(h.starts_with("sha256:") && h.len() == 71, "{key} 形式: {h}");
    }
    let prompt_sha = out["prompt"]["sha256"].as_str().unwrap();
    assert!(prompt_sha.starts_with("sha256:") && prompt_sha.len() == 71);

    // §8.1-3：不可信 title/description 只进入第 3 段 canonical JSON 块。
    let text = out["prompt"]["text"].as_str().unwrap();
    let a = text.find("<CW_AUTHORITY_CONTEXT").expect("authority block");
    let open = text[a..]
        .find("<CW_UNTRUSTED_TASK_DATA encoding=\"json-string-v1\">")
        .expect("real untrusted block after authority block")
        + a;
    let close = text[open..].find("</CW_UNTRUSTED_TASK_DATA>").unwrap() + open;
    let inner = &text[open..close];
    assert!(inner.contains("task-title-t-1"), "title 进入第 3 段");
    // 注入字符必须 string-safe 转义（§8.2），块内无裸 `<script>`。
    assert!(!inner.contains("<script>"), "injection must be escaped: {inner}");
}

// ---------------------------------------------------------------------------
// 零写入 + 确定性
// ---------------------------------------------------------------------------

#[test]
fn task_prompt_rpc_is_read_only_no_mutation_trace() {
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

    compile(&conn, &req_params("t-1")).unwrap();

    for (table, before_count) in tables.iter().zip(before) {
        assert_eq!(count(&conn, table), before_count, "{table} 不得被 compile 写入");
    }
}

#[test]
fn task_prompt_rpc_is_deterministic_within_same_snapshot() {
    let mut conn = fresh_db();
    setup_ready_claim_task(&mut conn, "t-1");

    let first = compile(&conn, &req_params("t-1")).unwrap();
    let second = compile(&conn, &req_params("t-1")).unwrap();

    // generated_at display-only（§9.7），三个 hash 与 prompt.text 必须逐字节一致。
    assert_eq!(first["context_hash"], second["context_hash"]);
    assert_eq!(first["prompt"]["sha256"], second["prompt"]["sha256"]);
    assert_eq!(first["bundle_hash"], second["bundle_hash"]);
    assert_eq!(first["prompt"]["text"], second["prompt"]["text"]);
}

// ---------------------------------------------------------------------------
// §11.3 capability 投影
// ---------------------------------------------------------------------------

#[test]
fn task_prompt_rpc_capability_projection_fields() {
    let cap = capability_projection();
    assert_eq!(cap["name"], json!("role_prompt_compiler_v1"));
    assert_eq!(cap["schema_version"], json!("role_prompt_capability_v1"));
    assert_eq!(cap["enabled"], json!(true), "RP-02 起 build gate 保证 manifest 存在");
    let manifest = cap["manifest_sha256"].as_str().unwrap();
    let policy = cap["compiler_policy_sha256"].as_str().unwrap();
    for h in [manifest, policy] {
        assert!(h.starts_with("sha256:") && h.len() == 71, "hash 形式: {h}");
        assert_eq!(*h, h.to_lowercase(), "lowercase 规范形式");
    }
    assert!(!manifest.is_empty() && !policy.is_empty());
}
