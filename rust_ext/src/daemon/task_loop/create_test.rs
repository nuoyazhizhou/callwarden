//! 1A `create.rs` 领域测试（cw-role-handoff-task-loop.md §8.1.1 / §4.3）。
//!
//! 通过公共 `create_task` 入口验证：同事务写入不可变 `task_workspace_bindings` 与
//! append-only `workspace_authority_captures`、ledger result 联动 commit、
//! 确定性错误经 savepoint 选择性回滚后落可重放 ledger error、基础设施错误回滚
//! 整个 outer transaction（领域写入与 ledger result 一并消失）。

use rusqlite::Connection;

use crate::sqlite_query::migrate_connection;
use super::create::{
    registry_identity_hash, create_task, CreateTaskInput, LedgerKey, WorkspaceCaptureInput,
};
use super::types::FrozenAuthorityInput;

/// 固定 workspace identity（registry 侧由 daemon 校验后传入，此处构造闭环输入）。
fn ws_input(workspace_id: i64, manifest_kind: &str) -> WorkspaceCaptureInput {
    WorkspaceCaptureInput {
        workspace_id,
        daemon_workspace_id: 42,
        workspace_instance_id: "ws-inst-1".to_string(),
        client_view_root_hash: "client-view-hash".to_string(),
        host_real_root_hash: "host-root-hash".to_string(),
        workspace_manifest_payload_json: format!("{{\"kind\":\"{manifest_kind}\"}}"),
        workspace_manifest_hash: format!("manifest-{manifest_kind}"),
        created_by: "test-creator".to_string(),
    }
}

/// 开启内存 task-DB 并跑一遍 migration（播种 operation_params / workspace_capture 两套 rule）。
fn fresh_db() -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    migrate_connection(&conn).expect("migration to v53");
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

fn key(workspace_instance_id: &str, request_id: &str) -> LedgerKey {
    LedgerKey {
        workspace_instance_id: workspace_instance_id.to_string(),
        method: "task.create".to_string(),
        request_id: request_id.to_string(),
    }
}

fn input(task_id: &str) -> CreateTaskInput {
    CreateTaskInput {
        task_id: task_id.to_string(),
        title: format!("task-{task_id}"),
        description: "desc".to_string(),
        creator: "test-creator".to_string(),
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
fn identity_hash_is_deterministic_and_order_sensitive() {
    let ws = ws_input(1, "a");
    let h1 = registry_identity_hash(
        &ws.workspace_instance_id,
        &ws.client_view_root_hash,
        &ws.host_real_root_hash,
        &ws.workspace_manifest_hash,
    );
    let h2 = ws.registry_identity_hash();
    assert_eq!(h1, h2, "同输入必须给出相同 identity hash");
    assert!(h1.starts_with("sha256:"), "hash 前缀应为 sha256:");

    let changed = ws_input(1, "b");
    assert_ne!(h1, changed.registry_identity_hash(), "manifest 变化必须改变 identity");
    // daemon_workspace_id 不参与稳定 identity（仅诊断 provenance）。
    let same_identity = WorkspaceCaptureInput {
        daemon_workspace_id: 999,
        ..ws
    };
    assert_eq!(h1, same_identity.registry_identity_hash());
}

#[test]
fn create_commits_task_binding_capture_and_ledger_together() {
    let mut conn = fresh_db();
    let ws = ws_input(1, "a");
    let resp = create_task(
        &mut conn,
        &frozen(),
        &key(&ws.workspace_instance_id, "req-1"),
        &input("t-1"),
        &ws,
    )
    .expect("首次 create 应成功");

    assert_eq!(resp["ok"].as_bool(), Some(true));
    assert_eq!(resp["task_id"], "t-1");
    assert_eq!(resp["workspace_id"], 1);
    let binding_id = resp["workspace_binding_id"].as_str().unwrap();
    let capture_id = resp["workspace_capture_id"].as_str().unwrap();

    // 领域写入与 ledger 在同一事务提交。
    assert_eq!(count(&conn, "tasks"), 1);
    assert_eq!(count(&conn, "task_workspace_bindings"), 1);
    assert_eq!(count(&conn, "workspace_authority_captures"), 1);
    assert_eq!(count(&conn, "task_operation_ledger"), 1);

    // capture 链首条 revision=1，无 supersedes。
    let (revision, supersedes): (i64, Option<String>) = conn
        .query_row(
            "SELECT capture_revision, supersedes_capture_id FROM workspace_authority_captures",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(revision, 1);
    assert!(supersedes.is_none());

    // binding 指向刚追加的 capture。
    let bound_capture: String = conn
        .query_row(
            "SELECT workspace_capture_id FROM task_workspace_bindings WHERE task_id = 't-1'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(bound_capture, capture_id);

    // ledger result 即成功响应。
    let stored: String = conn
        .query_row(
            "SELECT response_or_error_json FROM task_operation_ledger",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let stored_json: serde_json::Value = serde_json::from_str(&stored).unwrap();
    assert_eq!(stored_json["ok"].as_bool(), Some(true));
    assert_eq!(stored_json["workspace_binding_id"], binding_id);
}

#[test]
fn replay_of_committed_key_does_not_dup() {
    let mut conn = fresh_db();
    let ws = ws_input(1, "a");
    let k = key(&ws.workspace_instance_id, "req-replay");
    let first = create_task(&mut conn, &frozen(), &k, &input("t-r"), &ws).expect("first ok");
    let second = create_task(&mut conn, &frozen(), &k, &input("t-r"), &ws).expect("replay ok");

    assert_eq!(first, second, "幂等重放必须原样返回已保存的结果");
    assert_eq!(count(&conn, "tasks"), 1, "重放不得重复写 task");
    assert_eq!(count(&conn, "task_workspace_bindings"), 1);
    assert_eq!(count(&conn, "workspace_authority_captures"), 1);
    assert_eq!(count(&conn, "task_operation_ledger"), 1);
}

#[test]
fn identity_change_fails_closed_and_records_replayable_error() {
    let mut conn = fresh_db();
    // 首次用 identity 'a' 建立 capture。
    let ws_a = ws_input(1, "a");
    create_task(&mut conn, &frozen(), &key("ws-inst-1", "req-id-a"), &input("t-a"), &ws_a)
        .expect("first ok");

    // 同一 workspace 以新的 identity 'b'、新 request id 追加 → 确定性拒绝。
    let ws_b = ws_input(1, "b");
    let err = create_task(
        &mut conn,
        &frozen(),
        &key("ws-inst-1", "req-id-b"),
        &input("t-b"),
        &ws_b,
    )
    .expect_err("identity 改变必须确定性拒绝");

    assert_eq!(
        err.code, "E_WORKSPACE_AUTHORITY_MISMATCH",
        "失败类别必须是 workspace authority mismatch"
    );
    // savepoint 选择性回滚：领域局部写入不落库，但 ledger error 已提交。
    assert_eq!(count(&conn, "tasks"), 1, "被拒绝的 task 不得写入");
    assert_eq!(count(&conn, "task_workspace_bindings"), 1, "不得新增 binding");
    assert_eq!(count(&conn, "workspace_authority_captures"), 1, "不得追加新 capture");
    assert_eq!(count(&conn, "task_operation_ledger"), 2, "拒绝也必须落可重放 ledger error");

    // 已保存的错误结果必须是 ok:false + 稳定错误码。
    let stored: String = conn
        .query_row(
            "SELECT response_or_error_json FROM task_operation_ledger \
             WHERE request_id = 'req-id-b'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let stored_json: serde_json::Value = serde_json::from_str(&stored).unwrap();
    assert_eq!(stored_json["ok"].as_bool(), Some(false));
    assert_eq!(stored_json["code"], "E_WORKSPACE_AUTHORITY_MISMATCH");
}

#[test]
fn infrastructure_error_rolls_back_everything() {
    let mut conn = fresh_db();
    // 预置同 id task：第二次 create 打到 tasks 主键冲突 → 基础设施错误。
    conn.execute(
        "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at) \
         VALUES ('t-dup', 'x', '', 'c', 'open', 0.0, 0.0)",
        [],
    )
    .unwrap();

    let ws = ws_input(1, "c");
    let err = create_task(
        &mut conn,
        &frozen(),
        &key("ws-inst-1", "req-infra"),
        &input("t-dup"),
        &ws,
    )
    .expect_err("task 主键冲突必须归类为基础设施错误");
    assert_eq!(err.code, "E_TASK_DB_TRANSACTION");

    // 整个 outer transaction 回滚：capture / binding / ledger 一并消失。
    assert_eq!(count(&conn, "workspace_authority_captures"), 0, "回滚后不得遗留 capture");
    assert_eq!(count(&conn, "task_workspace_bindings"), 0, "回滚后不得遗留 binding");
    assert_eq!(count(&conn, "task_operation_ledger"), 0, "回滚后不得遗留 ledger result");
}

#[test]
fn re_attestation_appends_new_capture_and_links_new_binding() {
    let mut conn = fresh_db();
    let ws = ws_input(1, "a");
    create_task(&mut conn, &frozen(), &key("ws-inst-1", "req-1"), &input("t-1"), &ws)
        .expect("first ok");

    // 同 workspace/instance/identity 追加新一轮 capture（re-attestation，允许）。
    let resp2 = create_task(
        &mut conn,
        &frozen(),
        &key("ws-inst-1", "req-2"),
        &input("t-2"),
        &ws,
    )
    .expect("同 identity 的 re-attestation 应成功");

    assert_eq!(count(&conn, "tasks"), 2);
    assert_eq!(count(&conn, "task_workspace_bindings"), 2);
    assert_eq!(count(&conn, "workspace_authority_captures"), 2, "追加一条新 capture");

    // 新 capture revision=2 且 supersedes 指向前一条 capture（revision=1）。
    let first_capture: String = conn
        .query_row(
            "SELECT workspace_capture_id FROM workspace_authority_captures \
             ORDER BY capture_revision ASC LIMIT 1",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let (revision, supersedes): (i64, String) = conn
        .query_row(
            "SELECT capture_revision, supersedes_capture_id FROM workspace_authority_captures \
             WHERE workspace_capture_id = ?1",
            rusqlite::params![resp2["workspace_capture_id"].as_str().unwrap()],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(revision, 2);
    assert_eq!(supersedes, first_capture, "新 capture 必须 supersedes 前一条");

    // 第二条 binding 指向最新的 capture（revision=2）。
    let bound: String = conn
        .query_row(
            "SELECT workspace_capture_id FROM task_workspace_bindings WHERE task_id = 't-2'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let newest: String = conn
        .query_row(
            "SELECT workspace_capture_id FROM workspace_authority_captures \
             ORDER BY capture_revision DESC LIMIT 1",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(bound, newest, "新 task 的 binding 必须指向最新 capture");
}