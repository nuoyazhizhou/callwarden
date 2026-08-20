//! 任务 3 `verdict_evidence_gate.rs` 领域测试（cw-role-handoff-task-loop.md §4.1/§4.2/§8.1.5）。
//!
//! 覆盖：blind_first_pass 成功追加、overall/phase 枚举非法、author 非 reviewer、
//! reviewer lease 各失败（NOT_FOUND/TOKEN_MISMATCH/EXPIRED/FENCING_STALE/HOLDER_MISMATCH）、
//! step 无 binding、Role/Task Contract 三元组 ABA、task 无 workspace binding、
//! normalization 绑定缺失（E_VERDICT_NORMALIZATION_UNAVAILABLE）、post_reveal_amendment
//! 引用合法/非法、幂等重放不重复追加、有效投影的 versioned normalization 消费。

use rusqlite::Connection;
use sha2::{Digest, Sha256};

use crate::sqlite_query::migrate_connection;
use super::claim::{claim_step, ClaimStepInput, LedgerKey as ClaimLedgerKey};
use super::contract_set::{
    set_task_contract, ContractPayload, LedgerKey as ContractLedgerKey, SetContractInput,
};
use super::create::{create_task, CreateTaskInput, LedgerKey as CreateLedgerKey, WorkspaceCaptureInput};
use super::verdict_evidence_gate::{
    normalize_overall, normalize_phase, read_effective_verdicts, submit_verdict, ContractTriple,
    LedgerKey, VerdictInput, ERR_LEASE_EXPIRED, ERR_LEASE_FENCING_STALE,
    ERR_LEASE_HOLDER_MISMATCH, ERR_LEASE_NOT_FOUND, ERR_LEASE_TOKEN_MISMATCH,
    ERR_STEP_BINDING_INVALID, ERR_TASK_BINDING_REQUIRED, ERR_VERDICT_AMENDMENT_REF_INVALID,
    ERR_VERDICT_CONTRACT_STALE, ERR_VERDICT_NORMALIZATION_UNAVAILABLE, ERR_VERDICT_OVERALL_INVALID,
    ERR_VERDICT_PHASE_INVALID, ERR_VERDICT_ROLE_IDENTITY_MISMATCH,
};
use super::types::FrozenAuthorityInput;

/// 开启内存 task-DB 并跑一遍 migration（v57：含 verdict_normalization_rules）。
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

/// 读取 seed 出的 `verdict-normalization/v1` rules_hash（schema v57 由迁移原子插入）。
fn seeded_norm_hash(conn: &Connection) -> String {
    conn.query_row(
        "SELECT rules_hash FROM verdict_normalization_rules \
         WHERE normalization_version = 'verdict-normalization/v1'",
        [],
        |row| row.get(0),
    )
    .unwrap()
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

/// 插入 Task Contract 三元组行，并绑定 `verdict-normalization/v1`（§4.2）。
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

/// 插入无 normalization 绑定的 Task Contract（→ E_VERDICT_NORMALIZATION_UNAVAILABLE）。
fn setup_task_contract_no_norm(conn: &Connection, task_id: &str, contract_id: &str) {
    conn.execute(
        "INSERT INTO task_contract_revisions \
         (contract_id, revision, contract_hash, profile, task_id, workspace_id, \
          envelope_payload, created_at, created_by) \
         VALUES (?1, 1, 'sha256:task-1', 'review', ?2, 1, '{\"objective\":\"t\"}', 0.0, 'test')",
        rusqlite::params![contract_id, task_id],
    )
    .unwrap();
}

/// 插入 active reviewer lease（role = acting_role，与遗留 validate_lease_for_mutation 一致）。
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

/// 标准成功路径 base：task + role contract + (带 normalization 绑定) task contract + reviewer
/// lease + step claim。返回全部三元组，供成功测试与各失败测试构造 input。
struct Base {
    rcr_id: String,
    rcr_revision: i64,
    rcr_hash: String,
    tc_id: String,
}

fn setup_verdict_base(conn: &mut Connection, with_lease: bool) -> Base {
    setup_task(conn, "t-1");
    let rcr_id = setup_contract(conn, "t-1", "coder", "");
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
        setup_lease(conn, "L-1", "reviewer", "token-1", 1, "agent-1", "sess-1", "model-1", 1e18);
    }
    setup_step(conn, "t-1", 1, "review", "pending");
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

fn verdict_key(request_id: &str) -> LedgerKey {
    LedgerKey {
        workspace_instance_id: "ws-inst-1".to_string(),
        method: "verdict.submit".to_string(),
        request_id: request_id.to_string(),
    }
}

fn base_input(base: &Base) -> VerdictInput {
    VerdictInput {
        task_id: "t-1".to_string(),
        step_id: "1".to_string(),
        overall: "pass".to_string(),
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

#[test]
fn blind_first_pass_success_appends_ledger_event() {
    let mut conn = fresh_db();
    let base = setup_verdict_base(&mut conn, true);
    let resp = submit_verdict(
        &mut conn,
        &frozen(),
        &verdict_key("v-req-1"),
        &base_input(&base),
    )
    .expect("blind_first_pass 应成功");

    assert_eq!(resp["ok"], serde_json::json!(true));
    assert_eq!(resp["overall"], serde_json::json!("pass"));
    assert_eq!(resp["phase"], serde_json::json!("blind_first_pass"));
    assert_eq!(resp["normalization_version"], serde_json::json!("verdict-normalization/v1"));

    let verdict_id = resp["verdict_id"].as_str().unwrap().to_string();
    let (phase, overall, norm_version, step_id): (String, String, String, String) = conn
        .query_row(
            "SELECT phase, overall, normalization_version, step_id \
             FROM task_verdict_events WHERE verdict_id = ?1",
            [&verdict_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .unwrap();
    assert_eq!(phase, "blind_first_pass");
    assert_eq!(overall, "pass");
    assert_eq!(norm_version, "verdict-normalization/v1");
    assert_eq!(step_id, "1");
}

#[test]
fn overall_not_binary_is_rejected() {
    let mut conn = fresh_db();
    let base = setup_verdict_base(&mut conn, true);
    let mut input = base_input(&base);
    input.overall = "request_changes".to_string();
    let err = submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-1"), &input).unwrap_err();
    assert_eq!(err.code, ERR_VERDICT_OVERALL_INVALID);
}

#[test]
fn phase_enum_invalid_is_rejected() {
    let mut conn = fresh_db();
    let base = setup_verdict_base(&mut conn, true);
    let mut input = base_input(&base);
    input.phase = "POST_VERDICT".to_string();
    let err = submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-1"), &input).unwrap_err();
    assert_eq!(err.code, ERR_VERDICT_PHASE_INVALID);
}

#[test]
fn non_reviewer_identity_is_rejected() {
    let mut conn = fresh_db();
    let base = setup_verdict_base(&mut conn, false);
    let mut input = base_input(&base);
    input.acting_role = "implementer".to_string();
    let err = submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-1"), &input).unwrap_err();
    assert_eq!(err.code, ERR_VERDICT_ROLE_IDENTITY_MISMATCH);
}

#[test]
fn missing_lease_is_rejected() {
    let mut conn = fresh_db();
    let base = setup_verdict_base(&mut conn, false);
    let mut input = base_input(&base);
    input.lease_token.clear();
    // 直接构造 VerdictInput（不经 from_params），domain 侧 check_lease 发现无 active lease。
    let err = submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-1"), &input).unwrap_err();
    assert_eq!(err.code, ERR_LEASE_NOT_FOUND);
}

#[test]
fn lease_token_mismatch_is_rejected() {
    let mut conn = fresh_db();
    let base = setup_verdict_base(&mut conn, true);
    let mut input = base_input(&base);
    input.lease_token = "wrong-token".to_string();
    let err = submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-1"), &input).unwrap_err();
    assert_eq!(err.code, ERR_LEASE_TOKEN_MISMATCH);
}

#[test]
fn lease_expired_is_rejected() {
    let mut conn = fresh_db();
    let base = setup_verdict_base(&mut conn, false);
    setup_lease(&conn, "L-2", "reviewer", "token-1", 1, "agent-1", "sess-1", "model-1", 0.0);
    let err = submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-1"), &base_input(&base))
        .unwrap_err();
    assert_eq!(err.code, ERR_LEASE_EXPIRED);
}

#[test]
fn lease_fencing_stale_is_rejected() {
    let mut conn = fresh_db();
    let base = setup_verdict_base(&mut conn, true);
    let mut input = base_input(&base);
    input.fencing_counter = 99;
    let err = submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-1"), &input).unwrap_err();
    assert_eq!(err.code, ERR_LEASE_FENCING_STALE);
}

#[test]
fn lease_holder_mismatch_is_rejected() {
    let mut conn = fresh_db();
    let base = setup_verdict_base(&mut conn, true);
    let mut input = base_input(&base);
    input.session_id = "other-sess".to_string();
    let err = submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-1"), &input).unwrap_err();
    assert_eq!(err.code, ERR_LEASE_HOLDER_MISMATCH);
}

#[test]
fn step_without_binding_is_rejected() {
    let mut conn = fresh_db();
    let base = setup_verdict_base(&mut conn, true);
    // 删除 claim 建立的 binding，使 read_current_binding fail-closed 返回 None。
    conn.execute("DELETE FROM task_step_role_contract_bindings", []).unwrap();
    let err = submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-1"), &base_input(&base))
        .unwrap_err();
    assert_eq!(err.code, ERR_STEP_BINDING_INVALID);
}

#[test]
fn role_contract_aba_is_rejected() {
    let mut conn = fresh_db();
    let base = setup_verdict_base(&mut conn, true);
    let mut input = base_input(&base);
    input.role_contract.revision += 1; // 与当前 revision 不一致（ABA）。
    let err = submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-1"), &input).unwrap_err();
    assert_eq!(err.code, ERR_VERDICT_CONTRACT_STALE);
}

#[test]
fn task_without_workspace_binding_is_rejected() {
    let mut conn = fresh_db();
    conn.execute(
        "INSERT INTO tasks (id, title, description, status, created_at, updated_at) \
         VALUES ('t-nobind', 'no-bind', '', 'open', 0.0, 0.0)",
        [],
    )
    .unwrap();
    let input = VerdictInput {
        task_id: "t-nobind".to_string(),
        step_id: "1".to_string(),
        overall: "pass".to_string(),
        phase: "blind_first_pass".to_string(),
        clause_results: "[]".to_string(),
        findings: "[]".to_string(),
        task_contract: ContractTriple {
            id: "tc".to_string(),
            revision: 1,
            hash: "h".to_string(),
        },
        role_contract: ContractTriple {
            id: "rc".to_string(),
            revision: 1,
            hash: "h".to_string(),
        },
        amendment_ref: String::new(),
        snapshot_id: String::new(),
        view_manifest_hash: String::new(),
        attestation: String::new(),
        acting_role: "reviewer".to_string(),
        agent_id: "agent-1".to_string(),
        session_id: "sess-1".to_string(),
        model_id: "model-1".to_string(),
        lease_token: "token-1".to_string(),
        fencing_counter: 1,
        created_by: "test".to_string(),
    };
    // 任务存在但无 binding → ERR_TASK_BINDING_REQUIRED（先于 lease 检查）。
    let err = submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-1"), &input).unwrap_err();
    assert_eq!(err.code, ERR_TASK_BINDING_REQUIRED);
}

#[test]
fn normalization_binding_missing_is_unverified() {
    let mut conn = fresh_db();
    let base = setup_verdict_base(&mut conn, true);
    // 覆盖 task_contract normalization 绑定为空（按未绑定失败闭合）。
    conn.execute(
        "UPDATE task_contract_revisions \
         SET normalization_version = '', normalization_rules_hash = '' \
         WHERE contract_id = ?1",
        [&base.tc_id],
    )
    .unwrap();
    let err = submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-1"), &base_input(&base))
        .unwrap_err();
    assert_eq!(err.code, ERR_VERDICT_NORMALIZATION_UNAVAILABLE);
}

#[test]
fn normalization_binding_without_contract_row_is_unverified() {
    let mut conn = fresh_db();
    // Task Contract 引用不存在的 normalization_version → 规则 row 缺失 fail-closed。
    let base = setup_verdict_base(&mut conn, true);
    conn.execute(
        "UPDATE task_contract_revisions \
         SET normalization_version = 'verdict-normalization/nonexistent-v1' \
         WHERE contract_id = ?1",
        [&base.tc_id],
    )
    .unwrap();
    let err = submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-1"), &base_input(&base))
        .unwrap_err();
    assert_eq!(err.code, ERR_VERDICT_NORMALIZATION_UNAVAILABLE);
}

#[test]
fn amendment_requires_sealed_verdict() {
    let mut conn = fresh_db();
    let base = setup_verdict_base(&mut conn, true);
    let mut input = base_input(&base);
    input.phase = "post_reveal_amendment".to_string();
    // 未引用任何 sealed verdict → 拒绝。
    let err = submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-1"), &input).unwrap_err();
    assert_eq!(err.code, ERR_VERDICT_AMENDMENT_REF_INVALID);

    // 引用悬空 verdict_id → 拒绝。
    input.amendment_ref = "v-nope".to_string();
    let err = submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-2"), &input).unwrap_err();
    assert_eq!(err.code, ERR_VERDICT_AMENDMENT_REF_INVALID);
}

#[test]
fn post_reveal_amendment_after_sealed_verdict_succeeds() {
    let mut conn = fresh_db();
    let base = setup_verdict_base(&mut conn, true);

    // 1. 先封存 blind_first_pass。
    let blind = submit_verdict(
        &mut conn,
        &frozen(),
        &verdict_key("v-req-1"),
        &base_input(&base),
    )
    .expect("blind_first_pass 应成功");
    let verdict_id = blind["verdict_id"].as_str().unwrap().to_string();

    // 2. post_reveal_amendment 引用该 sealed verdict。
    let mut input = base_input(&base);
    input.phase = "post_reveal_amendment".to_string();
    input.amendment_ref = verdict_id.clone();
    input.overall = "block".to_string();
    let amend = submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-2"), &input)
        .expect("post_reveal_amendment 应成功");
    assert_eq!(amend["phase"], serde_json::json!("post_reveal_amendment"));
    assert_eq!(amend["amendment_ref"], serde_json::json!(verdict_id));

    let (phase, amendment_ref): (String, String) = conn
        .query_row(
            "SELECT phase, amendment_ref FROM task_verdict_events \
             ORDER BY id DESC LIMIT 1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(phase, "post_reveal_amendment");
    assert_eq!(amendment_ref, verdict_id);
}

#[test]
fn idempotent_replay_does_not_duplicate_event() {
    let mut conn = fresh_db();
    let base = setup_verdict_base(&mut conn, true);
    let key = verdict_key("v-req-1");
    let first = submit_verdict(&mut conn, &frozen(), &key, &base_input(&base)).unwrap();
    let verdict_id = first["verdict_id"].as_str().unwrap().to_string();
    let second = submit_verdict(&mut conn, &frozen(), &key, &base_input(&base)).unwrap();
    assert_eq!(second["verdict_id"].as_str().unwrap(), verdict_id);
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM task_verdict_events", [], |row| row.get(0))
        .unwrap();
    assert_eq!(count, 1, "同 request-id 重放不得重复追加事件");
}

#[test]
fn normalize_overall_legacy_and_v1_mapping() {
    // 使用 seed 的 v1 规则 payload（等价于 normalize_overall 消费的规则集）。
    let payload: serde_json::Value = serde_json::json!({
        "overall_map": {
            "approved": "pass",
            "needs_changes": "block",
            "request_changes": "block",
            "rejected": "block",
            "unclear": "UNVERIFIED",
            "abstain": "UNVERIFIED",
        },
        "phase_map": {
            "PRE_VERDICT": "blind_first_pass",
            "POST_VERDICT": "post_reveal_amendment",
        },
    });
    assert_eq!(normalize_overall("approved", &payload), "pass");
    assert_eq!(normalize_overall("needs_changes", &payload), "block");
    assert_eq!(normalize_overall("unclear", &payload), "UNVERIFIED");
    assert_eq!(normalize_overall("pass", &payload), "pass");
    assert_eq!(normalize_overall("block", &payload), "block");
    assert_eq!(normalize_overall("unknown", &payload), "UNVERIFIED");
    assert_eq!(normalize_phase("PRE_VERDICT", &payload), "blind_first_pass");
    assert_eq!(normalize_phase("blind_first_pass", &payload), "blind_first_pass");
}

#[test]
fn effective_projection_consumes_stored_normalization_binding() {
    let mut conn = fresh_db();
    let base = setup_verdict_base(&mut conn, true);
    submit_verdict(&mut conn, &frozen(), &verdict_key("v-req-1"), &base_input(&base)).unwrap();

    let projections = read_effective_verdicts(&conn, "t-1").expect("投影应成功");
    assert_eq!(projections.len(), 1);
    let p = &projections[0];
    assert_eq!(p.phase, "blind_first_pass");
    assert_eq!(p.normalized_overall, "pass");
    assert_eq!(p.normalization_version, "verdict-normalization/v1");
    assert_eq!(p.normalized_phase, "blind_first_pass");
}