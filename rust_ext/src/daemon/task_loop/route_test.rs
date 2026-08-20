//! 1D3A/1D3B 的 route 接线测试（cw-role-handoff-task-loop.md §4.3 / §6 行 622、630）。
//!
//! 覆盖：
//! - 内部 validation 路由（`InternalValidation` + `InternalPreflightPermit`）真实
//!   分派 `task.create` → `create_task`：同事务写入 task/binding/capture + ledger；
//! - admission 终点：permit fingerprint 与当前 schema/rules/generation 失配时
//!   fail-closed 且零领域写入；
//! - `ExternalTransport` 不得进入内部路由；
//! - 公共路由 `dispatch_task_loop`：workspace 未安装 `PublicPreflightPermit` 时
//!   fail-closed（1D3B：permit 安装后由 promotion_test.rs 覆盖真实分派）。

use rusqlite::Connection;

use crate::sqlite_query::migrate_connection;
use super::capability_control::CapabilityMutationGate;
use super::create::WorkspaceCaptureInput;
use super::preflight::run_internal_preflight;
use super::promotion::PublicPermitStore;
use super::route::{dispatch_internal_validation, dispatch_task_loop, RouteContext};
use super::types::{FrozenAuthorityInput, InvocationClass, StrictParsedEnvelope};

const ERR_DISABLED: &str = "E_TASK_LOOP_CAPABILITY_DISABLED";

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

fn ws_input() -> WorkspaceCaptureInput {
    WorkspaceCaptureInput {
        workspace_id: 1,
        daemon_workspace_id: 42,
        workspace_instance_id: "ws-inst-1".to_string(),
        client_view_root_hash: "client-view-hash".to_string(),
        host_real_root_hash: "host-root-hash".to_string(),
        workspace_manifest_payload_json: "{\"kind\":\"a\"}".to_string(),
        workspace_manifest_hash: "manifest-a".to_string(),
        created_by: "test-creator".to_string(),
    }
}

fn env(
    method: &str,
    request_id: &str,
    class: InvocationClass,
    params: serde_json::Value,
) -> StrictParsedEnvelope {
    StrictParsedEnvelope {
        workspace_instance_id: "ws-inst-1".to_string(),
        canonical_method: method.to_string(),
        request_id: request_id.to_string(),
        params,
        invocation_class: class,
    }
}

fn create_params(task_id: &str) -> serde_json::Value {
    serde_json::json!({
        "task_id": task_id,
        "title": format!("task-{task_id}"),
        "description": "desc",
        "creator": "test-creator",
    })
}

fn count(conn: &Connection, table: &str) -> i64 {
    conn.query_row(
        &format!("SELECT COUNT(*) FROM {table}"),
        [],
        |row| row.get(0),
    )
    .unwrap()
}

fn frozen(gen: u64) -> FrozenAuthorityInput {
    FrozenAuthorityInput { daemon_generation: gen, ..Default::default() }
}

#[test]
fn internal_route_dispatches_create_task() {
    let mut conn = fresh_db();
    let ws = ws_input();
    let permit = run_internal_preflight(&conn, 7).expect("迁移后 preflight 应通过");
    let f = frozen(7);
    let store = PublicPermitStore::new();
    let gate = CapabilityMutationGate::default();

    let envelope = env("task.create", "req-cutover-1", InvocationClass::InternalValidation, create_params("t-cutover"));
    let resp = {
        let mut ctx = RouteContext { conn: &mut conn, frozen: &f, store: &store, gate: &gate };
        dispatch_internal_validation(&mut ctx, &envelope, &permit, Some(&ws))
    }
    .expect("内部路由应真实分派 task.create");

    assert_eq!(resp["ok"].as_bool(), Some(true));
    assert_eq!(resp["task_id"], "t-cutover");
    assert_eq!(count(&conn, "tasks"), 1);
    assert_eq!(count(&conn, "task_workspace_bindings"), 1);
    assert_eq!(count(&conn, "workspace_authority_captures"), 1);
    assert_eq!(count(&conn, "task_operation_ledger"), 1);
}

#[test]
fn internal_route_rejects_external_transport() {
    let mut conn = fresh_db();
    let ws = ws_input();
    let permit = run_internal_preflight(&conn, 7).unwrap();
    let f = frozen(7);
    let store = PublicPermitStore::new();
    let gate = CapabilityMutationGate::default();

    // ExternalTransport 只能走公共路由，不得进入内部路由。
    let envelope = env("task.create", "req-ext-1", InvocationClass::ExternalTransport, create_params("t-ext"));
    let err = {
        let mut ctx = RouteContext { conn: &mut conn, frozen: &f, store: &store, gate: &gate };
        dispatch_internal_validation(&mut ctx, &envelope, &permit, Some(&ws))
    }
    .expect_err("ExternalTransport 必须被内部路由拒绝");
    assert_eq!(err.code, ERR_DISABLED);
    assert_eq!(count(&conn, "tasks"), 0, "拒绝路径零领域写入");
}

#[test]
fn internal_route_rejects_stale_permit_with_zero_writes() {
    let mut conn = fresh_db();
    let ws = ws_input();
    // 先取合法 permit，再破坏 fingerprint 模拟 schema 变化后仍持旧 permit。
    let mut permit = run_internal_preflight(&conn, 7).unwrap();
    permit.schema_fingerprint = "sha256:stale".to_string();
    let f = frozen(7);
    let store = PublicPermitStore::new();
    let gate = CapabilityMutationGate::default();

    let envelope = env("task.create", "req-stale-1", InvocationClass::InternalValidation, create_params("t-stale"));
    let err = {
        let mut ctx = RouteContext { conn: &mut conn, frozen: &f, store: &store, gate: &gate };
        dispatch_internal_validation(&mut ctx, &envelope, &permit, Some(&ws))
    }
    .expect_err("stale permit 必须被最终复核拒绝");
    assert_eq!(err.code, ERR_DISABLED);
    assert_eq!(count(&conn, "tasks"), 0, "拒绝路径零领域写入");
    assert_eq!(count(&conn, "task_operation_ledger"), 0, "拒绝路径零 ledger 写入");
}

#[test]
fn internal_route_rejects_unknown_method() {
    let mut conn = fresh_db();
    let ws = ws_input();
    let permit = run_internal_preflight(&conn, 7).unwrap();
    let f = frozen(7);
    let store = PublicPermitStore::new();
    let gate = CapabilityMutationGate::default();

    let envelope = env("task.not_wired", "req-unk-1", InvocationClass::InternalValidation, serde_json::json!({}));
    let err = {
        let mut ctx = RouteContext { conn: &mut conn, frozen: &f, store: &store, gate: &gate };
        dispatch_internal_validation(&mut ctx, &envelope, &permit, Some(&ws))
    }
    .expect_err("未接入方法必须 disabled");
    assert_eq!(err.code, ERR_DISABLED);
}

#[test]
fn internal_route_requires_workspace_capture_input() {
    let mut conn = fresh_db();
    let permit = run_internal_preflight(&conn, 7).unwrap();
    let f = frozen(7);
    let store = PublicPermitStore::new();
    let gate = CapabilityMutationGate::default();

    let envelope = env("task.create", "req-no-ws-1", InvocationClass::InternalValidation, create_params("t-no-ws"));
    let err = {
        let mut ctx = RouteContext { conn: &mut conn, frozen: &f, store: &store, gate: &gate };
        dispatch_internal_validation(&mut ctx, &envelope, &permit, None)
    }
    .expect_err("缺 workspace capture 输入必须拒绝");
    assert_eq!(err.code, "invalid_params");
    assert_eq!(count(&conn, "tasks"), 0);
}

#[test]
fn public_route_disabled_without_installed_permit() {
    // 1D3B：公共路由仅受理已安装 PublicPreflightPermit 的 workspace（§4.3）。
    // 未安装（或已清除）时 fail-closed，且零领域写入。
    let mut conn = fresh_db();
    let f = frozen(7);
    let store = PublicPermitStore::new();
    let gate = CapabilityMutationGate::default();

    let envelope = env("task.create", "req-pub-1", InvocationClass::ExternalTransport, create_params("t-pub"));
    let err = {
        let mut ctx = RouteContext { conn: &mut conn, frozen: &f, store: &store, gate: &gate };
        dispatch_task_loop(&mut ctx, &envelope)
    }
    .expect_err("未安装 permit 的公共 route 必须 disabled");
    assert_eq!(err.code, ERR_DISABLED);
    assert_eq!(count(&conn, "tasks"), 0, "拒绝路径零领域写入");
    assert_eq!(count(&conn, "task_operation_ledger"), 0, "拒绝路径零 ledger 写入");
}

#[test]
fn create_params_missing_field_rejected() {
    use super::create::CreateTaskInput;
    let missing = serde_json::json!({
        "task_id": "t-1",
        "title": "t",
        "description": "d",
        // 缺 creator
    });
    let err = CreateTaskInput::from_params(&missing).expect_err("缺字段必须拒绝");
    assert_eq!(err.code, "invalid_params");
}
