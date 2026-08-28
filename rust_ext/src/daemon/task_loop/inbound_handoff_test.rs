//! 任务 T-1787912195064 `inbound_handoff.rs` 领域测试（冻结计划 §8 负向矩阵）。
//!
//! 覆盖 8 条负向用例（全部真实 Rust 测试，禁止源码字符串断言）：
//! 1. 无 handoff 事件 → `inbound_handoff.diagnosis == "no_handoff"`，routing 与实施前一致；
//! 2. 单条 handoff → 逐字段等于落库 envelope（不重算 outcome/reason）；
//! 3. 多条 handoff → `inbound_handoff` 取 `monotonic_seq` 最大者；`prior_handoffs` 升序完整；
//! 4. envelope 非法 JSON → `diagnosis == "unparsable_handoff"`，不 panic，routing 不受影响；
//! 5. `target_role` 与当前 routing 不一致 → `matches_current_routing == false` 且 routing 未改写；
//! 6. 存在 failed step → `prior_attempts` 含该 step 的 `step_id`/`status`/`result`；
//! 7. 超过 20 条 handoff / failed step → 截断且 `truncated == true`；
//! 8. 只读性 → 调用前后 `task_events`/`tasks`/`task_steps` 行数与内容完全不变。

use rusqlite::Connection;

use super::claim::{claim_step, ClaimStepInput, LedgerKey as ClaimLedgerKey};
use super::contract_set::{
    set_task_contract, ContractPayload, LedgerKey as ContractLedgerKey, SetContractInput,
};
use super::create::{
    create_task, CreateTaskInput, LedgerKey as CreateLedgerKey, WorkspaceCaptureInput,
};
use super::next_action::evaluate_next_action;
use super::types::FrozenAuthorityInput;
use crate::sqlite_query::migrate_connection;

/// 开启内存 task-DB 并跑一遍 migration。
fn fresh_db() -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    migrate_connection(&conn).expect("migration");
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

/// 建立 task + 不可变 workspace binding（instance=ws-inst-1）。
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

/// 建立 Role Contract lineage + revision；返回 revision id。
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

/// 插入 Task Contract 三元组行（含 normalization 绑定，供 evaluate 通过）。
fn setup_task_contract(conn: &Connection, task_id: &str, contract_id: &str) {
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
#[allow(clippy::too_many_arguments)]
fn setup_step(
    conn: &Connection,
    task_id: &str,
    step_id: i64,
    action: &str,
    status: &str,
    result: &str,
) {
    conn.execute(
        "INSERT INTO task_steps \
         (id, task_id, step_index, action, status, result, created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, 0.0)",
        rusqlite::params![step_id, task_id, step_id, action, status, result],
    )
    .unwrap();
}

/// claim 到 step（建立 verified binding）。
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

/// 构造合法 handoff envelope JSON 字符串。
fn envelope(
    task_id: &str,
    event_id: &str,
    source_role: &str,
    target_role: &str,
    outcome: &str,
    reason: &str,
) -> String {
    serde_json::json!({
        "handoff_event_id": event_id,
        "task_id": task_id,
        "step_id": "1",
        "source_role": source_role,
        "target_role": target_role,
        "outcome": outcome,
        "reason": reason,
        "task_contract": {"id": "tc-t-1", "revision": 1, "hash": "sha256:task-1"},
        "role_contract": {"id": "rcr-1", "revision": 1, "hash": "sha256:r"},
        "independence_requirement": "required",
        "request_id": format!("req-{event_id}"),
        "fencing_counter": 1,
        "agent_id": "agent-1",
        "session_id": "sess-1",
        "created_by": "test-handoff",
    })
    .to_string()
}

/// 直接插入一条 handoff_structured 事件（模拟 report_handoff 已落库）。
#[allow(clippy::too_many_arguments)]
fn insert_handoff_event(conn: &Connection, task_id: &str, seq: i64, ts: f64, reason_json: &str) {
    conn.execute(
        "INSERT INTO task_events \
         (task_id, workspace_id, from_status, to_status, reason_code, reason, \
          actor_identity, agent_session_id, role, contract_hash, snapshot_id, \
          monotonic_seq, authoritative_timestamp, evidence_path, evidence_hash) \
         VALUES (?1, '1', 'open', 'open', 'handoff_structured', ?2, \
                 'test-handoff', 'sess-1', 'executor', 'sha256:r', '', ?3, ?4, '', '')",
        rusqlite::params![task_id, reason_json, seq, ts],
    )
    .unwrap();
}

/// 标准 claim 前置：task + implementer contract + task contract + step + binding。
fn setup_claim_ready(conn: &mut Connection) {
    setup_task(conn, "t-1");
    let rcr = setup_contract(conn, "t-1", "implementer");
    setup_task_contract(conn, "t-1", "tc-t-1");
    setup_step(conn, "t-1", 1, "implement", "pending", "");
    setup_binding(conn, "t-1", "1", &rcr, "req-c-1");
}

fn count(conn: &Connection, table: &str) -> i64 {
    conn.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
        row.get(0)
    })
    .unwrap()
}

/// dump 全表内容（id + 关键列），用于只读性逐字节比较。
fn dump(conn: &Connection, sql: &str) -> Vec<String> {
    let mut stmt = conn.prepare(sql).unwrap();
    let cols = stmt.column_count();
    let rows = stmt
        .query_map([], |row| {
            let mut parts = Vec::new();
            for i in 0..cols {
                let v = match row.get_ref(i).unwrap() {
                    rusqlite::types::ValueRef::Null => String::new(),
                    rusqlite::types::ValueRef::Integer(n) => n.to_string(),
                    rusqlite::types::ValueRef::Real(f) => f.to_string(),
                    rusqlite::types::ValueRef::Text(t) => String::from_utf8_lossy(t).to_string(),
                    rusqlite::types::ValueRef::Blob(b) => format!("blob:{}", b.len()),
                };
                parts.push(v);
            }
            Ok(parts.join("|"))
        })
        .unwrap();
    let mut out: Vec<String> = rows.map(|r| r.unwrap()).collect();
    out.sort();
    out
}

// ---------------------------------------------------------------------------
// 负向矩阵（冻结计划 §8）
// ---------------------------------------------------------------------------

#[test]
fn no_handoff_yields_diagnosis_no_handoff_and_routing_unchanged() {
    let mut conn = fresh_db();
    setup_claim_ready(&mut conn);

    let resp = evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");

    // 既有派工字段与实施前逐字一致（READY/CLAIM/executor/queued）。
    assert_eq!(resp["decision"], serde_json::json!("READY"));
    assert_eq!(resp["action"], serde_json::json!("CLAIM"));
    assert_eq!(resp["required_role"], serde_json::json!("executor"));
    assert_eq!(resp["workflow_status"], serde_json::json!("queued"));
    assert_eq!(resp["lifecycle_status"], serde_json::json!("open"));
    assert_eq!(resp["routing"]["next_role"], serde_json::json!("executor"));
    assert_eq!(
        resp["routing"]["next_action"],
        serde_json::json!("claim_current_step")
    );
    // 无 handoff → diagnosis=no_handoff，不得省略字段、不得编造。
    assert_eq!(
        resp["inbound_handoff"]["diagnosis"],
        serde_json::json!("no_handoff")
    );
    // work_order 存在且 objective 来自当前 step action。
    assert_eq!(
        resp["work_order"]["objective"],
        serde_json::json!("implement")
    );
    assert_eq!(
        resp["work_order"]["task_title"],
        serde_json::json!("task-t-1")
    );
    assert_eq!(resp["work_order"]["prior_attempts"], serde_json::json!([]));
    assert_eq!(resp["work_order"]["prior_handoffs"], serde_json::json!([]));
}

#[test]
fn single_handoff_projects_each_field_equal_to_envelope() {
    let mut conn = fresh_db();
    setup_claim_ready(&mut conn);
    insert_handoff_event(
        &conn,
        "t-1",
        7,
        1787000000.25,
        &envelope(
            "t-1",
            "he-t-1-r1",
            "executor",
            "reviewer",
            "executor_ready_for_review",
            "已完成，请求评审",
        ),
    );

    let resp = evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");
    let ih = &resp["inbound_handoff"];

    assert_eq!(ih["handoff_event_id"], serde_json::json!("he-t-1-r1"));
    assert_eq!(ih["from_role"], serde_json::json!("executor"));
    assert_eq!(ih["target_role"], serde_json::json!("reviewer"));
    assert_eq!(
        ih["outcome"],
        serde_json::json!("executor_ready_for_review")
    );
    assert_eq!(ih["reason"], serde_json::json!("已完成，请求评审"));
    assert_eq!(ih["request_id"], serde_json::json!("req-he-t-1-r1"));
    assert_eq!(ih["step_id"], serde_json::json!("1"));
    assert_eq!(ih["monotonic_seq"], serde_json::json!(7));
    assert_eq!(
        ih["authoritative_timestamp"],
        serde_json::json!(1787000000.25)
    );
    // 当前 routing 是 executor（claim），envelope target=reviewer → 不一致为 false，
    // 且不得改写 routing。
    assert_eq!(ih["matches_current_routing"], serde_json::json!(false));
    assert_eq!(resp["routing"]["next_role"], serde_json::json!("executor"));
    assert_eq!(
        resp["routing"]["next_action"],
        serde_json::json!("claim_current_step")
    );
    // work_order.prior_handoffs 升序含该交接摘要。
    assert_eq!(
        resp["work_order"]["prior_handoffs"][0]["monotonic_seq"],
        serde_json::json!(7)
    );
}

#[test]
fn multiple_handoffs_take_max_seq_and_list_ascending() {
    let mut conn = fresh_db();
    setup_claim_ready(&mut conn);
    insert_handoff_event(
        &conn,
        "t-1",
        1,
        1787000000.0,
        &envelope(
            "t-1",
            "he-1",
            "executor",
            "reviewer",
            "executor_ready_for_review",
            "first",
        ),
    );
    insert_handoff_event(
        &conn,
        "t-1",
        2,
        1787000001.0,
        &envelope(
            "t-1",
            "he-2",
            "reviewer",
            "executor",
            "adjudicator_returned",
            "second",
        ),
    );
    insert_handoff_event(
        &conn,
        "t-1",
        3,
        1787000002.0,
        &envelope(
            "t-1",
            "he-3",
            "executor",
            "reviewer",
            "executor_ready_for_review",
            "third",
        ),
    );

    let resp = evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");
    let ih = &resp["inbound_handoff"];

    // 取 monotonic_seq 最大者（seq=3）。
    assert_eq!(ih["handoff_event_id"], serde_json::json!("he-3"));
    assert_eq!(ih["monotonic_seq"], serde_json::json!(3));
    // prior_handoffs 升序完整（seq 1,2,3）。
    let ph = resp["work_order"]["prior_handoffs"].as_array().unwrap();
    assert_eq!(ph.len(), 3);
    assert_eq!(ph[0]["monotonic_seq"], serde_json::json!(1));
    assert_eq!(ph[1]["monotonic_seq"], serde_json::json!(2));
    assert_eq!(ph[2]["monotonic_seq"], serde_json::json!(3));
}

#[test]
fn unparsable_envelope_fails_soft_without_panic() {
    let mut conn = fresh_db();
    setup_claim_ready(&mut conn);
    insert_handoff_event(&conn, "t-1", 5, 1787000000.5, r#"{not valid json!!"#);

    let resp = evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");

    assert_eq!(
        resp["inbound_handoff"]["diagnosis"],
        serde_json::json!("unparsable_handoff")
    );
    // 不 panic；routing 不受影响。
    assert_eq!(resp["decision"], serde_json::json!("READY"));
    assert_eq!(resp["routing"]["next_role"], serde_json::json!("executor"));
    // prior_handoffs 对损坏事件过滤（不 panic、不编造字段）。
    assert_eq!(resp["work_order"]["prior_handoffs"], serde_json::json!([]));
}

#[test]
fn mismatched_target_role_reports_false_and_keeps_routing() {
    let mut conn = fresh_db();
    setup_claim_ready(&mut conn);
    // envelope target=adjudicator，而当前 routing 是 executor（claim）→ 不一致。
    insert_handoff_event(
        &conn,
        "t-1",
        4,
        1787000000.4,
        &envelope(
            "t-1",
            "he-4",
            "reviewer",
            "adjudicator",
            "reviewer_pass",
            "PASS",
        ),
    );

    let resp = evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");

    assert_eq!(
        resp["inbound_handoff"]["matches_current_routing"],
        serde_json::json!(false)
    );
    // routing 仍由 evaluator 计算，未被 envelope target 改写。
    assert_eq!(resp["routing"]["next_role"], serde_json::json!("executor"));
    assert_eq!(
        resp["routing"]["next_action"],
        serde_json::json!("claim_current_step")
    );
    assert_eq!(
        resp["routing"]["origin_kind"],
        serde_json::json!("system_evaluator")
    );
}

#[test]
fn failed_step_appears_in_prior_attempts() {
    let mut conn = fresh_db();
    setup_claim_ready(&mut conn);
    // 追加一条 failed step（含 result），未被 resolve。
    setup_step(
        &conn,
        "t-1",
        2,
        "implement",
        "failed",
        r#"{"error":"compile failed"}"#,
    );
    // 注意：failed step 会使 evaluator 走 remediation 分支（REVISE/claim remediation），
    // 但 work_order.prior_attempts 仍应含该 failed step（与派工无关，只读事实）。
    let resp = evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");

    let pa = resp["work_order"]["prior_attempts"].as_array().unwrap();
    assert_eq!(pa.len(), 1);
    assert_eq!(pa[0]["step_id"], serde_json::json!("2"));
    assert_eq!(pa[0]["step_index"], serde_json::json!(2));
    assert_eq!(pa[0]["action"], serde_json::json!("implement"));
    assert_eq!(pa[0]["status"], serde_json::json!("failed"));
    assert_eq!(
        pa[0]["result"],
        serde_json::json!(r#"{"error":"compile failed"}"#)
    );
}

#[test]
fn over_20_handoffs_and_failed_steps_truncate_with_flag() {
    let mut conn = fresh_db();
    setup_claim_ready(&mut conn);
    for i in 0..25 {
        let seq = i as i64 + 1;
        insert_handoff_event(
            &conn,
            "t-1",
            seq,
            1787000000.0 + seq as f64,
            &envelope(
                &format!("t-1"),
                &format!("he-{seq}"),
                "executor",
                "reviewer",
                "executor_ready_for_review",
                &format!("reason-{seq}"),
            ),
        );
    }
    for i in 0..25 {
        setup_step(
            &conn,
            "t-1",
            100 + i,
            "implement",
            "failed",
            &format!("err-{i}"),
        );
    }

    let resp = evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");

    // prior_handoffs 截断到 20 条，且 truncated=true。
    let ph = resp["work_order"]["prior_handoffs"].as_array().unwrap();
    assert_eq!(ph.len(), 20);
    assert_eq!(resp["work_order"]["truncated"], serde_json::json!(true));
    // 保留升序前 20 条（seq 1..=20）。
    assert_eq!(ph[0]["monotonic_seq"], serde_json::json!(1));
    assert_eq!(ph[19]["monotonic_seq"], serde_json::json!(20));
    // prior_attempts 截断到 20 条，按 step_index 升序。
    let pa = resp["work_order"]["prior_attempts"].as_array().unwrap();
    assert_eq!(pa.len(), 20);
    assert_eq!(pa[0]["step_id"], serde_json::json!("100"));
    assert_eq!(pa[19]["step_id"], serde_json::json!("119"));
    // inbound_handoff 仍取最大 seq（25）。
    assert_eq!(
        resp["inbound_handoff"]["monotonic_seq"],
        serde_json::json!(25)
    );
}

#[test]
fn projection_is_strictly_read_only() {
    let mut conn = fresh_db();
    setup_claim_ready(&mut conn);
    insert_handoff_event(
        &conn,
        "t-1",
        1,
        1787000000.0,
        &envelope(
            "t-1",
            "he-1",
            "executor",
            "reviewer",
            "executor_ready_for_review",
            "ro",
        ),
    );
    setup_step(&conn, "t-1", 2, "implement", "failed", "x");

    let dump_tables = |c: &Connection| -> (Vec<String>, Vec<String>, Vec<String>) {
        (
            dump(
                c,
                "SELECT event_id, task_id, reason_code, reason FROM task_events",
            ),
            dump(c, "SELECT id, title, status FROM tasks"),
            dump(
                c,
                "SELECT id, task_id, action, status, result FROM task_steps",
            ),
        )
    };
    let before = dump_tables(&conn);
    let rows_before = (
        count(&conn, "task_events"),
        count(&conn, "tasks"),
        count(&conn, "task_steps"),
    );

    evaluate_next_action(&conn, "ws-inst-1", "t-1").expect("evaluate 应成功");

    let after = dump_tables(&conn);
    let rows_after = (
        count(&conn, "task_events"),
        count(&conn, "tasks"),
        count(&conn, "task_steps"),
    );
    assert_eq!(rows_before, rows_after, "三表行数必须不变");
    assert_eq!(before, after, "三表内容必须逐字节不变（纯只读）");
}
