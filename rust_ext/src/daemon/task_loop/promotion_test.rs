//! 1D3B：`task_loop.public_promote` control-plane promotion 语义测试
//! （cw-role-handoff-task-loop.md §4.3 / §8.1.5）。
//!
//! 覆盖：
//! - authorized：审计事件 commit 成功后才安装内存 permit，响应固定区分
//!   `durable_authorization` 与 `permit_installation`；
//! - replay：同 (workspace_id, request_id) + 同 canonical params hash 只读重放
//!   持久化结果，不追加事件、不重装 permit；
//! - mismatch：同 key 不同 canonical 参数返回 `E_REQUEST_ID_REUSE_MISMATCH`，
//!   既有行绝不被修改；
//! - 确定性拒绝：generation/fingerprint/凭证失配记录可重放
//!   `deterministic_error`，绝不安装 permit；
//! - 重启不恢复：PublicPermitStore 为纯内存，daemon 重启后 permit 清空，公共
//!   路由恢复 fail-closed；replay 报告 `permit_installation=not_installed`。

use rusqlite::Connection;

use crate::sqlite_query::migrate_connection;
use super::capability_control::CapabilityMutationGate;
use super::preflight;
use super::promotion::{promote_public_capability, PublicPermitStore};
use super::route::{dispatch_task_loop, RouteContext};
use super::types::{
    ERR_CAPABILITY_DISABLED, ERR_REQUEST_ID_REUSE_MISMATCH, FrozenAuthorityInput,
    InvocationClass, StrictParsedEnvelope,
};

/// 开启内存 task-DB 并跑一遍 migration + 播种 workspace。
fn fresh_db() -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    migrate_connection(&conn).expect("migration to v54");
    conn.execute(
        "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (?1, ?2, ?3, 0.0)",
        rusqlite::params![1, "ws-1", "/tmp/ws-1"],
    )
    .unwrap();
    conn
}

/// 构造一个与当前 schema/rules/generation 完全匹配的合法 promotion 请求。
fn valid_params(conn: &Connection, workspace_id: i64, request_id: &str) -> serde_json::Value {
    let schema_fingerprint = preflight::compute_schema_fingerprint(conn).expect("schema fp");
    let rules_hash = preflight::read_workspace_capture_rules_hash(conn).expect("rules hash");
    serde_json::json!({
        "workspace_id": workspace_id,
        "workspace_instance_id": format!("ws-inst-{workspace_id}"),
        "request_id": request_id,
        "action_identity": "test-action-identity",
        "authority_id": "auth-1",
        "authority_revision": 1,
        "fencing_counter": 0,
        "internal_permit_schema_fingerprint": schema_fingerprint,
        "internal_permit_rules_hash": rules_hash,
        "internal_permit_daemon_generation": 7,
        "evidence_id": "ev-1",
        "evidence_hash": "sha256:evidence-1",
        "runtime_binary_hash": "sha256:runtime-1",
    })
}

fn frozen(gen: u64) -> FrozenAuthorityInput {
    FrozenAuthorityInput { daemon_generation: gen, ..Default::default() }
}

fn count_events(conn: &Connection, workspace_id: i64, request_id: &str) -> i64 {
    conn.query_row(
        "SELECT COUNT(*) FROM task_loop_capability_promotion_events \
         WHERE workspace_id = ?1 AND request_id = ?2",
        rusqlite::params![workspace_id, request_id],
        |r| r.get(0),
    )
    .unwrap()
}

#[test]
fn authorized_promotion_installs_permit_and_records_event() {
    let mut conn = fresh_db();
    let gate = CapabilityMutationGate::default();
    let store = PublicPermitStore::new();
    let f = frozen(7);
    let params = valid_params(&conn, 1, "req-auth-1");

    let resp = promote_public_capability(&mut conn, &gate, &store, &f, &params)
        .expect("合法请求必须 authorized");

    assert_eq!(resp["ok"].as_bool(), Some(true));
    assert_eq!(resp["durable_authorization"], "authorized");
    assert_eq!(resp["permit_installation"], "installed");
    assert!(resp["promotion_event_id"].as_str().unwrap().starts_with("promote-"));

    // 内存 permit 已安装（与请求绑定）。
    let permit = store.get("ws-inst-1").expect("permit 必须已安装");
    assert_eq!(permit.request_id, "req-auth-1");
    assert_eq!(permit.daemon_generation, 7);
    assert_eq!(permit.workspace_id, "ws-inst-1");

    // 权威账本仅一条 authorized 事件。
    assert_eq!(count_events(&conn, 1, "req-auth-1"), 1);
    let auth: String = conn
        .query_row(
            "SELECT durable_authorization FROM task_loop_capability_promotion_events \
             WHERE workspace_id = 1 AND request_id = 'req-auth-1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(auth, "authorized");
}

#[test]
fn same_request_replays_durable_result_without_new_event() {
    let mut conn = fresh_db();
    let gate = CapabilityMutationGate::default();
    let store = PublicPermitStore::new();
    let f = frozen(7);
    let params = valid_params(&conn, 1, "req-replay-1");

    let first = promote_public_capability(&mut conn, &gate, &store, &f, &params).unwrap();
    assert_eq!(first["permit_installation"], "installed");

    // 同 key 同 hash：只读重放，不追加事件、不重装 permit。
    let replay = promote_public_capability(&mut conn, &gate, &store, &f, &params).unwrap();
    assert_eq!(replay["durable_authorization"], "authorized");
    assert_eq!(replay["permit_installation"], "installed");
    assert_eq!(
        replay["promotion_event_id"],
        first["promotion_event_id"],
        "重放必须回显同一事件 id"
    );
    assert_eq!(count_events(&conn, 1, "req-replay-1"), 1, "重放不得追加事件");
}

#[test]
fn request_id_reuse_with_different_params_rejects_and_preserves_row() {
    let mut conn = fresh_db();
    let gate = CapabilityMutationGate::default();
    let store = PublicPermitStore::new();
    let f = frozen(7);
    let params = valid_params(&conn, 1, "req-mismatch-1");
    promote_public_capability(&mut conn, &gate, &store, &f, &params).unwrap();

    // 同 key 但 authority_revision 不同（不在 key 排除集内）→ canonical hash 变化。
    let mut changed = params.clone();
    changed["authority_revision"] = serde_json::json!(2);
    let err = promote_public_capability(&mut conn, &gate, &store, &f, &changed)
        .expect_err("同 key 不同参数必须拒绝");
    assert_eq!(err.code, ERR_REQUEST_ID_REUSE_MISMATCH);

    // 既有行绝不被修改：结果仍是 authorized。
    let stored: String = conn
        .query_row(
            "SELECT durable_authorization FROM task_loop_capability_promotion_events \
             WHERE workspace_id = 1 AND request_id = 'req-mismatch-1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(stored, "authorized");
    assert_eq!(count_events(&conn, 1, "req-mismatch-1"), 1);
}

#[test]
fn deterministic_reject_records_error_without_installing_permit() {
    let mut conn = fresh_db();
    let gate = CapabilityMutationGate::default();
    let store = PublicPermitStore::new();
    let f = frozen(7);
    // 破坏 daemon generation 绑定 → 确定性拒绝（非基础设施错误）。
    let mut params = valid_params(&conn, 1, "req-reject-1");
    params["internal_permit_daemon_generation"] = serde_json::json!(999);

    let resp = promote_public_capability(&mut conn, &gate, &store, &f, &params)
        .expect("确定性拒绝以成功响应返回可重放结果");

    assert_eq!(resp["durable_authorization"], "deterministic_error");
    assert_eq!(resp["permit_installation"], "not_installed");
    assert!(
        store.get("ws-inst-1").is_none(),
        "确定性拒绝绝不安装 permit"
    );

    // 拒绝也追加可重放审计结果。
    assert_eq!(count_events(&conn, 1, "req-reject-1"), 1);
    let (auth, code): (String, String) = conn
        .query_row(
            "SELECT durable_authorization, authorization_code \
             FROM task_loop_capability_promotion_events \
             WHERE workspace_id = 1 AND request_id = 'req-reject-1'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(auth, "deterministic_error");
    assert_eq!(code, ERR_CAPABILITY_DISABLED);

    // 同 key 重放返回同一确定性拒绝结果。
    let replay = promote_public_capability(&mut conn, &gate, &store, &f, &params).unwrap();
    assert_eq!(replay["durable_authorization"], "deterministic_error");
    assert_eq!(replay["permit_installation"], "not_installed");
    assert_eq!(count_events(&conn, 1, "req-reject-1"), 1);
}

#[test]
fn missing_minimum_credentials_rejects() {
    let mut conn = fresh_db();
    let gate = CapabilityMutationGate::default();
    let store = PublicPermitStore::new();
    let f = frozen(7);
    let mut params = valid_params(&conn, 1, "req-no-creds-1");
    params["evidence_id"] = serde_json::json!("");

    let resp = promote_public_capability(&mut conn, &gate, &store, &f, &params).unwrap();
    assert_eq!(resp["durable_authorization"], "deterministic_error");
    assert_eq!(resp["permit_installation"], "not_installed");
    assert!(store.get("ws-inst-1").is_none());
    assert_eq!(count_events(&conn, 1, "req-no-creds-1"), 1);
}

#[test]
fn restart_clears_permit_and_public_route_fails_closed() {
    let mut conn = fresh_db();
    let gate = CapabilityMutationGate::default();
    let store_a = PublicPermitStore::new();
    let f = frozen(7);
    let params = valid_params(&conn, 1, "req-restart-1");

    // daemon 会话 A：authorized + 安装 permit。
    let resp = promote_public_capability(&mut conn, &gate, &store_a, &f, &params).unwrap();
    assert_eq!(resp["permit_installation"], "installed");
    assert!(store_a.get("ws-inst-1").is_some());

    // 模拟 daemon 重启：PublicPermitStore 纯内存，新建即空（不恢复 permit）。
    let store_b = PublicPermitStore::new();
    assert!(
        store_b.get("ws-inst-1").is_none(),
        "重启后 permit 不得恢复（audit event 不是可恢复 permit）"
    );

    // 同 key 重放：durable 结果不变，但安装状态如实反映当前会话。
    let replay = promote_public_capability(&mut conn, &gate, &store_b, &f, &params).unwrap();
    assert_eq!(replay["durable_authorization"], "authorized");
    assert_eq!(replay["permit_installation"], "not_installed");

    // 重启后未重新 promotion：公共路由 fail-closed，零领域写入。
    let envelope = StrictParsedEnvelope {
        workspace_instance_id: "ws-inst-1".to_string(),
        canonical_method: "task.create".to_string(),
        request_id: "req-pub-restart".to_string(),
        params: serde_json::json!({
            "task_id": "t-restart",
            "title": "t",
            "description": "d",
            "creator": "c",
        }),
        invocation_class: InvocationClass::ExternalTransport,
    };
    let err = {
        let mut ctx = RouteContext { conn: &mut conn, frozen: &f, store: &store_b, gate: &gate };
        dispatch_task_loop(&mut ctx, &envelope)
    }
    .expect_err("重启后未重新 promotion 的公共 route 必须 disabled");
    assert_eq!(err.code, ERR_CAPABILITY_DISABLED);
    let tasks: i64 = conn.query_row("SELECT COUNT(*) FROM tasks", [], |r| r.get(0)).unwrap();
    assert_eq!(tasks, 0, "拒绝路径零领域写入");
}
