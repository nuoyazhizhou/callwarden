//! RP-10 cross-layer E2E（Rust 侧）：Role Prompt Compiler v1 全链编译不变量。
//!
//! 覆盖（冻结规范 §17.3 / RP-10 卡 check items，`task_prompt_e2e` 验收过滤词）：
//! - 完整链路：create → contract lineage → authority context（RP-03）→
//!   route_and_select（RP-04 §6/§7.1）→ compile_bundle（§5.1/§8/§9）；
//! - bundle hash 闭集：prompt.sha256 / context_hash / bundle_hash 可独立重算；
//! - 确定性：同一 snapshot 两次编译逐字节一致（generated_at 固定时）；
//! - 只读：全链聚合+编译前后任务/合同/事件/binding 行数不变；
//! - capability 门禁（§11.3）：production 资产下 `capability_enabled()` 成立、
//!   registry 完整性校验通过；缺失 task fail-closed（`E_TASK_PROMPT_TASK_NOT_FOUND`）。
//!
//! fixture 与 crate 内 context_tests 同构（1A create / 1B contract / claim 公共 API）。

use callwarden_core::daemon::task_loop::claim::{
    claim_step, ClaimStepInput, LedgerKey as ClaimLedgerKey,
};
use callwarden_core::daemon::task_loop::contract_set::{
    set_task_contract, ContractPayload, LedgerKey as ContractLedgerKey, SetContractInput,
};
use callwarden_core::daemon::task_loop::create::{
    create_task, CreateTaskInput, LedgerKey as CreateLedgerKey, WorkspaceCaptureInput,
};
use callwarden_core::daemon::task_loop::types::FrozenAuthorityInput;
use callwarden_core::daemon::task_prompt::bundle::compile_bundle;
use callwarden_core::daemon::task_prompt::context::aggregate_authority_context;
use callwarden_core::daemon::task_prompt::handler::capability_enabled;
use callwarden_core::daemon::task_prompt::route::{
    route_and_select, verify_registry_integrity, TEMPLATE_REGISTRY,
};
use callwarden_core::daemon::task_collab::TaskCollabStore;
use rusqlite::Connection;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

// ---------------------------------------------------------------------------
// fixture（与 task_prompt/context_tests 同构；integration 侧经 crate 公共 API）
// ---------------------------------------------------------------------------

/// 经官方 migration 建 schema（TaskCollabStore::new 事务化迁移到当前版本），
/// 返回连接 + 需在测试结束时保留的临时目录。
fn fresh_db() -> (Connection, tempfile::TempDir) {
    let dir = tempfile::tempdir().expect("tempdir");
    let db_path = dir.path().join("task.db");
    let _store = TaskCollabStore::new(&db_path).expect("官方 schema migration");
    let conn = Connection::open(&db_path).expect("reopen task db");
    conn.execute(
        "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (?1, ?2, ?3, 0.0)",
        rusqlite::params![1, "ws-1", "/tmp/ws-1"],
    )
    .unwrap();
    (conn, dir)
}

fn frozen() -> FrozenAuthorityInput {
    FrozenAuthorityInput::default()
}

fn sha256_hex(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

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
        description: "RP-10 e2e".to_string(),
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

fn setup_binding(conn: &mut Connection, task_id: &str, step_id: &str, rcr_id: &str, request_id: &str) {
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

/// READY/CLAIM 全量 fixture（与 context_tests 同构）。
fn setup_ready_claim_task(conn: &mut Connection, task_id: &str) {
    setup_task(conn, task_id);
    let rcr = setup_contract(conn, task_id, "implementer");
    setup_task_contract_with_policy(conn, task_id, &format!("tc-{task_id}"));
    setup_step(conn, task_id, 1, "implement", "pending");
    setup_binding(conn, task_id, "1", &rcr, &format!("req-c-{task_id}"));
}

/// 用 manifest 真实 executor v4 模板的 body hash 构造合同 prompt 字段，
/// 使 route §7.1 的 ID+hash 精确 pin 走 production manifest 路径。
fn executor_template_id_and_hash() -> (String, String) {
    let entry = TEMPLATE_REGISTRY
        .iter()
        .find(|t| t.template_id == "cw.aprime.executor.startup.v4")
        .expect("production manifest 必须包含 executor v4 模板");
    (
        entry.template_id.to_string(),
        // spec §9.3：唯一合法 wire form = sha256: 前缀（§8.3 redaction 拒绝裸 64-hex）
        format!("sha256:{}", sha256_hex(entry.body.as_bytes()).to_uppercase()),
    )
}

fn setup_contract_pinned_to_v4(conn: &mut Connection, task_id: &str, role: &str) -> String {
    let (template_id, template_hash) = executor_template_id_and_hash();
    let payload = ContractPayload {
        role: role.to_string(),
        skill_id: "skill-1".to_string(),
        skill_version: "1.0".to_string(),
        prompt_template_id: template_id,
        prompt_hash: template_hash,
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
            request_id: format!("contract-v4-{task_id}-{role}"),
        },
        &SetContractInput {
            task_id: task_id.to_string(),
            contract: payload,
            created_by: "test-owner".to_string(),
        },
    )
    .expect("setup v4 contract_set 应成功");
    resp["role_contract_revision_id"]
        .as_str()
        .unwrap()
        .to_string()
}

// ---------------------------------------------------------------------------
// E2E 正向
// ---------------------------------------------------------------------------

#[test]
fn e2e_role_work_bundle_compiles_with_hash_closure() {
    assert!(capability_enabled(), "production 资产下 capability 必须启用");
    verify_registry_integrity().expect("registry 完整性校验应通过");

    let (mut conn, _dir) = fresh_db();
    setup_task(&mut conn, "T-e2e-compile-1");
    let rcr = setup_contract_pinned_to_v4(&mut conn, "T-e2e-compile-1", "implementer");
    assert!(rcr.starts_with("rcr-"));
    setup_task_contract_with_policy(&conn, "T-e2e-compile-1", "tc-e2e-compile-1");
    setup_step(&conn, "T-e2e-compile-1", 1, "implement", "pending");
    setup_binding(&mut conn, "T-e2e-compile-1", "1", &rcr, "req-e2e-c-1");

    let context =
        aggregate_authority_context(&conn, "ws-inst-1", "T-e2e-compile-1").expect("context 聚合");
    let selection = route_and_select(&context).expect("READY/CLAIM executor v4 应可路由");
    assert_eq!(selection.prompt_kind, "role_work");
    assert_eq!(selection.template.template_id, "cw.aprime.executor.startup.v4");

    let compiled = compile_bundle(&context, &selection, &json!({}), "2026-09-07T00:00:00Z")
        .expect("bundle 编译应成功");
    let bundle = &compiled.bundle;

    // §5.1 结构闭集
    assert_eq!(bundle["schema_version"], "role_prompt_bundle_v1");
    assert_eq!(bundle["task_id"], "T-e2e-compile-1");
    assert_eq!(bundle["prompt_kind"], "role_work");
    assert_eq!(bundle["template"]["source"], "role_contract");

    // §9.7 prompt.sha256 可独立重算
    let prompt_text = bundle["prompt"]["text"].as_str().expect("prompt.text");
    assert!(!prompt_text.is_empty());
    assert_eq!(
        bundle["prompt"]["sha256"],
        format!("sha256:{}", sha256_hex(prompt_text.as_bytes()))
    );

    // §9.6 bundle_hash 最小闭集可独立重算
    let bundle_hash_input = json!({
        "schema_version": "role_prompt_bundle_v1",
        "task_id": bundle["task_id"],
        "prompt_kind": bundle["prompt_kind"],
        "context_hash": bundle["context_hash"],
        "prompt": { "sha256": bundle["prompt"]["sha256"] },
    });
    let canonical = callwarden_core::daemon::task_prompt::canonical::canonical_json(&bundle_hash_input)
        .expect("canonical json");
    assert_eq!(
        bundle["bundle_hash"],
        format!("sha256:{}", sha256_hex(&canonical))
    );

    // §8 不可信数据面进入 prompt 第 3 段（title 进 body，不进任何 hash 输入）
    assert!(prompt_text.contains("T-e2e-compile-1") || prompt_text.contains("task-T-e2e-compile-1"));
}

#[test]
fn e2e_bundle_is_deterministic_and_read_only() {
    let (mut conn, _dir) = fresh_db();
    setup_task(&mut conn, "T-e2e-determinism-1");
    let rcr = setup_contract_pinned_to_v4(&mut conn, "T-e2e-determinism-1", "implementer");
    setup_task_contract_with_policy(&conn, "T-e2e-determinism-1", "tc-e2e-determinism-1");
    setup_step(&conn, "T-e2e-determinism-1", 1, "implement", "pending");
    setup_binding(&mut conn, "T-e2e-determinism-1", "1", &rcr, "req-e2e-det-1");

    let before = (
        count(&conn, "tasks"),
        count(&conn, "role_contract_revisions"),
        count(&conn, "task_events"),
        count(&conn, "task_workspace_bindings"),
    );

    let ctx1 = aggregate_authority_context(&conn, "ws-inst-1", "T-e2e-determinism-1").unwrap();
    let ctx2 = aggregate_authority_context(&conn, "ws-inst-1", "T-e2e-determinism-1").unwrap();
    assert_eq!(ctx1, ctx2, "同一 snapshot 两次聚合必须逐字段一致");

    let sel = route_and_select(&ctx1).unwrap();
    let b1 = compile_bundle(&ctx1, &sel, &json!({}), "2026-09-07T00:00:00Z").unwrap();
    let b2 = compile_bundle(&ctx2, &sel, &json!({}), "2026-09-07T00:00:00Z").unwrap();
    assert_eq!(b1.bundle_json, b2.bundle_json, "同输入两次编译必须逐字节一致");
    assert_eq!(b1.bundle["bundle_hash"], b2.bundle["bundle_hash"]);

    let after = (
        count(&conn, "tasks"),
        count(&conn, "role_contract_revisions"),
        count(&conn, "task_events"),
        count(&conn, "task_workspace_bindings"),
    );
    assert_eq!(before, after, "聚合+编译不得写入任何任务/合同/事件/binding 行");
}

// ---------------------------------------------------------------------------
// E2E fail-closed
// ---------------------------------------------------------------------------

#[test]
fn e2e_missing_task_fails_closed_without_leaking() {
    let (conn, _dir) = fresh_db();
    let err = aggregate_authority_context(&conn, "ws-inst-1", "T-e2e-missing")
        .expect_err("缺失 task 必须 fail closed");
    assert_eq!(err.code, "E_TASK_PROMPT_TASK_NOT_FOUND");
    let msg = err.message.to_lowercase();
    assert!(!msg.contains("password"));
    assert!(!msg.contains("secret"));
}

#[test]
fn e2e_contract_hash_mismatch_fails_closed() {
    // READY/CLAIM executor context，但合同 hash 与 manifest 模板字节不一致
    // （§7.1-5 → E_TASK_PROMPT_TEMPLATE_HASH_MISMATCH；§7.1-3 非法 wire form
    // → E_TASK_PROMPT_TEMPLATE_HASH_REQUIRED）。
    let base = json!({
        "task_id": "T-e2e-route-mismatch",
        "routing": { "decision": "READY", "action": "CLAIM", "required_role": "executor" },
        "contract": {
            "role_contract_prompt_template_id": "cw.aprime.executor.startup.v4",
        },
    });

    // 非法 SHA-256 wire form
    let bad_form = serde_json::json!({
        "contract": { "role_contract_prompt_hash": "deadbeef" },
    });
    let ctx = merge(&base, &bad_form);
    let err = route_and_select(&ctx).expect_err("非法 hash wire form 必须 fail closed");
    assert_eq!(err.code, "E_TASK_PROMPT_TEMPLATE_HASH_REQUIRED");

    // 合法 wire form 但字节不匹配
    let wrong_hash = sha256_hex(b"not-the-template-body").to_uppercase();
    let bad_hash = serde_json::json!({
        "contract": { "role_contract_prompt_hash": wrong_hash },
    });
    let ctx = merge(&base, &bad_hash);
    let err = route_and_select(&ctx).expect_err("hash 不匹配必须 fail closed");
    assert_eq!(err.code, "E_TASK_PROMPT_TEMPLATE_HASH_MISMATCH");
}

fn merge(base: &Value, patch: &Value) -> Value {
    let mut out = base.clone();
    if let (Some(b), Some(p)) = (out.as_object_mut(), patch.as_object()) {
        for (k, v) in p {
            match (b.get_mut(k), v) {
                (Some(Value::Object(bt)), Value::Object(pv)) => {
                    let sub = merge(&Value::Object(bt.clone()), &Value::Object(pv.clone()));
                    b.insert(k.clone(), sub);
                }
                _ => {
                    b.insert(k.clone(), v.clone());
                }
            }
        }
    }
    out
}
