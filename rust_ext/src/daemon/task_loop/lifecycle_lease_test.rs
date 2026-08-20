//! 1F `lifecycle_lease.rs` 领域测试（cw-role-handoff-task-loop.md §8.1.6 / Req 11.2-11.9）。
//!
//! 通过公共入口验证：acquire 原子比较/fencing 递增/防双活/审计事件、renew 幂等续期与
//! token/holder/fencing 校验、release 置 released 与幂等、apply/close 的 reviewer lease
//! 受保护写门禁（缺失/失配 fail-closed）与状态迁移 + 事件 + identity 联动、ledger 不
//! 持久化 raw token。

use rusqlite::Connection;

use crate::sqlite_query::migrate_connection;
use super::create::{
    create_task, CreateTaskInput, LedgerKey as CreateKey, WorkspaceCaptureInput,
};
use super::lifecycle_lease::{
    apply_task, close_task, lease_acquire, lease_renew, lease_release, AcquireInput, ApplyInput,
    CloseInput, LedgerKey, LeaseIdentity, ReleaseInput, RenewInput,
    ERR_CHILD_TASKS_NOT_CLOSED, ERR_LEASE_ACTIVE_EXISTS, ERR_LEASE_FENCING_STALE,
    ERR_LEASE_NOT_FOUND, ERR_LEASE_TOKEN_MISMATCH, ERR_NO_STEPS,
};
use super::types::FrozenAuthorityInput;

fn ws_input(workspace_id: i64) -> WorkspaceCaptureInput {
    WorkspaceCaptureInput {
        workspace_id,
        daemon_workspace_id: 42,
        workspace_instance_id: "ws-inst-1".to_string(),
        client_view_root_hash: "client-view-hash".to_string(),
        host_real_root_hash: "host-root-hash".to_string(),
        workspace_manifest_payload_json: "{\"kind\":\"a\"}".to_string(),
        workspace_manifest_hash: "manifest-a".to_string(),
        created_by: "test-creator".to_string(),
    }
}

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

/// 创建任务（含不可变 workspace binding，供 lease 归属解析）。
fn new_task(conn: &mut Connection, task_id: &str) {
    let ws = ws_input(1);
    create_task(
        conn,
        &frozen(),
        &CreateKey {
            workspace_instance_id: "ws-inst-1".to_string(),
            method: "task.create".to_string(),
            request_id: format!("req-create-{task_id}"),
        },
        &CreateTaskInput {
            task_id: task_id.to_string(),
            title: format!("task-{task_id}"),
            description: "desc".to_string(),
            creator: "test-creator".to_string(),
        },
        &ws,
    )
    .expect("create_task 应成功");
}

fn key(method: &str, request_id: &str) -> LedgerKey {
    LedgerKey {
        workspace_instance_id: "ws-inst-1".to_string(),
        method: method.to_string(),
        request_id: request_id.to_string(),
    }
}

fn identity(role: &str) -> LeaseIdentity {
    LeaseIdentity {
        agent_id: "agent-1".to_string(),
        session_id: "session-1".to_string(),
        model_id: "model-1".to_string(),
        role: role.to_string(),
    }
}

/// 注册 holder（agent_registrations）为 active 且心跳新鲜，供"拒绝已有 active lease"测试。
fn register_agent(conn: &Connection, identity: &LeaseIdentity) {
    conn.execute(
        "INSERT INTO agent_registrations \
         (agent_id, agent_name, owner_key, registered_at, last_heartbeat, status, session_id, model_id) \
         VALUES (?1, ?2, ?3, ?4, ?5, 'active', ?6, ?7) \
         ON CONFLICT(agent_id) DO UPDATE SET \
           status = 'active', last_heartbeat = excluded.last_heartbeat, \
           session_id = excluded.session_id, model_id = excluded.model_id",
        rusqlite::params![
            identity.agent_id,
            identity.agent_id,
            "test-owner",
            now_unix_test(),
            now_unix_test(),
            identity.session_id,
            identity.model_id,
        ],
    )
    .unwrap();
}

fn now_unix_test() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 以 implementer 身份 acquire 并返回 (token, fencing_counter)。
fn acquire_impl(conn: &mut Connection, task_id: &str, role: &str, tag: &str) -> (String, i64) {
    let resp = lease_acquire(
        conn,
        &frozen(),
        &key("lease.acquire", &format!("req-acq-{task_id}-{role}-{tag}")),
        &AcquireInput {
            task_id: task_id.to_string(),
            role: role.to_string(),
            ttl_seconds: 3600.0,
            identity: identity(role),
        },
    )
    .expect("acquire 应成功");
    let token = resp["token"].as_str().unwrap().to_string();
    let counter = resp["fencing_counter"].as_i64().unwrap();
    (token, counter)
}

fn count(conn: &Connection, table: &str) -> i64 {
    conn.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| row.get(0)).unwrap()
}

#[test]
fn acquire_creates_lease_and_audit_event_and_never_persists_raw_token() {
    let mut conn = fresh_db();
    new_task(&mut conn, "t-1");
    let resp = lease_acquire(
        &mut conn,
        &frozen(),
        &key("lease.acquire", "req-1"),
        &AcquireInput {
            task_id: "t-1".to_string(),
            role: "implementer".to_string(),
            ttl_seconds: 3600.0,
            identity: identity("implementer"),
        },
    )
    .expect("acquire 应成功");

    let token = resp["token"].as_str().unwrap();
    let token_hash = resp["token_hash"].as_str().unwrap();
    let counter = resp["fencing_counter"].as_i64().unwrap();
    assert_eq!(counter, 1, "首次 acquire 的 fencing counter 应为 1");
    assert_eq!(resp["status"].as_str(), None); // acquire 响应无 status 字段
    assert_ne!(token, token_hash, "token 与 token_hash 不得相同");

    assert_eq!(count(&conn, "task_leases"), 1);
    let (stored_hash, stored_status, stored_counter): (String, String, i64) = conn
        .query_row(
            "SELECT token_hash, status, fencing_counter FROM task_leases",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(stored_hash, token_hash, "DB 只存 sha256");
    assert_eq!(stored_status, "active");
    assert_eq!(stored_counter, 1);

    // 审计事件：acquire 一条，fencing counter 一致，事件不含 raw token。
    assert_eq!(count(&conn, "task_lease_events"), 1);
    let (event_type, ev_counter, detail): (String, i64, String) = conn
        .query_row("SELECT event_type, fencing_counter, detail FROM task_lease_events", [], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?))
        })
        .unwrap();
    assert_eq!(event_type, "acquire");
    assert_eq!(ev_counter, 1);
    assert!(!detail.contains(token), "审计 detail 不得包含 raw token");

    // ledger 结果不得持久化 raw token（Req 11.2）。
    let ledger_json: String = conn
        .query_row(
            "SELECT response_or_error_json FROM task_operation_ledger \
             WHERE method = 'lease.acquire' AND request_id = 'req-1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(!ledger_json.contains(token), "ledger 不得持久化 raw token");
    assert!(ledger_json.contains(token_hash), "ledger 应携带 token_hash");
}

#[test]
fn acquire_rejects_existing_active_lease() {
    let mut conn = fresh_db();
    new_task(&mut conn, "t-1");
    register_agent(&conn, &identity("implementer"));
    acquire_impl(&mut conn, "t-1", "implementer", "a");
    let err = lease_acquire(
        &mut conn,
        &frozen(),
        &key("lease.acquire", "req-2"),
        &AcquireInput {
            task_id: "t-1".to_string(),
            role: "implementer".to_string(),
            ttl_seconds: 3600.0,
            identity: identity("implementer"),
        },
    )
    .unwrap_err();
    assert_eq!(err.code, ERR_LEASE_ACTIVE_EXISTS);
    assert_eq!(count(&conn, "task_leases"), 1, "拒绝后不得新增 lease");
}

#[test]
fn acquire_recovers_expired_lease_with_incremented_fencing() {
    let mut conn = fresh_db();
    new_task(&mut conn, "t-1");
    acquire_impl(&mut conn, "t-1", "implementer", "a");

    // 将 active lease 置为已过期（模拟 TTL 耗尽）。
    conn.execute(
        "UPDATE task_leases SET expires_at = 0.0 WHERE task_id = 't-1'",
        [],
    )
    .unwrap();

    let resp = lease_acquire(
        &mut conn,
        &frozen(),
        &key("lease.acquire", "req-2"),
        &AcquireInput {
            task_id: "t-1".to_string(),
            role: "implementer".to_string(),
            ttl_seconds: 3600.0,
            identity: identity("implementer"),
        },
    )
    .expect("过期 lease 应被回收并允许重新 acquire");
    assert_eq!(resp["fencing_counter"].as_i64().unwrap(), 2, "fencing counter 单调递增");

    // 旧 lease 置 expired；新 lease active。
    let (active_count, expired_count): (i64, i64) = conn
        .query_row(
            "SELECT \
               SUM(CASE WHEN status='active' THEN 1 ELSE 0 END), \
               SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) \
             FROM task_leases",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(active_count, 1);
    assert_eq!(expired_count, 1);
}

#[test]
fn acquire_recovers_stale_holder_lease_when_registration_inactive() {
    let mut conn = fresh_db();
    new_task(&mut conn, "t-1");
    let (_, counter1) = acquire_impl(&mut conn, "t-1", "implementer", "a");

    // 注册表无 holder row → 视为 stale（进程异常退出场景）。
    let err = lease_acquire(
        &mut conn,
        &frozen(),
        &key("lease.acquire", "req-2"),
        &AcquireInput {
            task_id: "t-1".to_string(),
            role: "implementer".to_string(),
            ttl_seconds: 3600.0,
            identity: identity("implementer"),
        },
    )
    .expect("holder 注册缺失时允许回收 stale lease");
    assert_eq!(err["fencing_counter"].as_i64().unwrap(), counter1 + 1);
    // expire 事件按旧 counter 落审计链。
    let expire_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM task_lease_events WHERE event_type = 'expire' AND fencing_counter = ?1",
            [counter1],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(expire_count, 1, "回收 stale lease 必须追加 expire 审计事件");
}

#[test]
fn renew_validates_and_extends_expiry_without_bumping_fencing() {
    let mut conn = fresh_db();
    new_task(&mut conn, "t-1");
    let (token, counter) = acquire_impl(&mut conn, "t-1", "implementer", "a");
    let expires_before: f64 = conn
        .query_row("SELECT expires_at FROM task_leases", [], |r| r.get(0))
        .unwrap();

    let resp = lease_renew(
        &mut conn,
        &frozen(),
        &key("lease.renew", "req-renew-1"),
        &RenewInput {
            task_id: "t-1".to_string(),
            role: "implementer".to_string(),
            token: token.clone(),
            ttl_seconds: 3600.0,
            fencing_counter: Some(counter),
            identity: Some(identity("implementer")),
        },
    )
    .expect("renew 应成功");
    assert_eq!(resp["fencing_counter"].as_i64().unwrap(), counter, "幂等续租不递增 counter");
    let expires_after: f64 = conn
        .query_row("SELECT expires_at FROM task_leases", [], |r| r.get(0))
        .unwrap();
    assert!(expires_after > expires_before, "续租必须延长 expires_at");
    let renew_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM task_lease_events WHERE event_type = 'renew'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(renew_count, 1);
    assert_eq!(count(&conn, "task_leases"), 1, "续租不得创建新 lease");
}

#[test]
fn renew_rejects_wrong_token() {
    let mut conn = fresh_db();
    new_task(&mut conn, "t-1");
    let (_, counter) = acquire_impl(&mut conn, "t-1", "implementer", "a");
    let err = lease_renew(
        &mut conn,
        &frozen(),
        &key("lease.renew", "req-renew-2"),
        &RenewInput {
            task_id: "t-1".to_string(),
            role: "implementer".to_string(),
            token: "wrong-token".to_string(),
            ttl_seconds: 3600.0,
            fencing_counter: Some(counter),
            identity: Some(identity("implementer")),
        },
    )
    .unwrap_err();
    assert_eq!(err.code, ERR_LEASE_TOKEN_MISMATCH);
}

#[test]
fn renew_rejects_stale_fencing_counter() {
    let mut conn = fresh_db();
    new_task(&mut conn, "t-1");
    let (token, _) = acquire_impl(&mut conn, "t-1", "implementer", "a");
    let err = lease_renew(
        &mut conn,
        &frozen(),
        &key("lease.renew", "req-renew-3"),
        &RenewInput {
            task_id: "t-1".to_string(),
            role: "implementer".to_string(),
            token,
            ttl_seconds: 3600.0,
            fencing_counter: Some(999),
            identity: Some(identity("implementer")),
        },
    )
    .unwrap_err();
    assert_eq!(err.code, ERR_LEASE_FENCING_STALE, "旧持有者续租被拒（Property 11）");
}

#[test]
fn release_marks_released_and_is_idempotent() {
    let mut conn = fresh_db();
    new_task(&mut conn, "t-1");
    let (token, counter) = acquire_impl(&mut conn, "t-1", "implementer", "a");

    let resp = lease_release(
        &mut conn,
        &frozen(),
        &key("lease.release", "req-rel-1"),
        &ReleaseInput {
            task_id: "t-1".to_string(),
            role: "implementer".to_string(),
            token: token.clone(),
            identity: Some(identity("implementer")),
        },
    )
    .expect("release 应成功");
    assert_eq!(resp["status"], "released");
    assert_eq!(resp["fencing_counter"].as_i64().unwrap(), counter);

    let (status, released_count): (String, i64) = conn
        .query_row(
            "SELECT status, \
              (SELECT COUNT(*) FROM task_lease_events WHERE event_type='release') \
             FROM task_leases",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(status, "released");
    assert_eq!(released_count, 1);

    // 幂等：重复 release 返回同一 released 状态，不追加事件、不创建新 lease。
    let again = lease_release(
        &mut conn,
        &frozen(),
        &key("lease.release", "req-rel-2"),
        &ReleaseInput {
            task_id: "t-1".to_string(),
            role: "implementer".to_string(),
            token,
            identity: Some(identity("implementer")),
        },
    )
    .expect("重复 release 应幂等返回");
    assert_eq!(again["idempotent"], true);
    assert_eq!(count(&conn, "task_leases"), 1);
    let ev_count: i64 = conn
        .query_row("SELECT COUNT(*) FROM task_lease_events", [], |r| r.get(0))
        .unwrap();
    assert_eq!(ev_count, 2, "acquire + release，重复 release 不得追加事件");
}

#[test]
fn release_rejects_wrong_token() {
    let mut conn = fresh_db();
    new_task(&mut conn, "t-1");
    acquire_impl(&mut conn, "t-1", "implementer", "a");
    let err = lease_release(
        &mut conn,
        &frozen(),
        &key("lease.release", "req-rel-3"),
        &ReleaseInput {
            task_id: "t-1".to_string(),
            role: "implementer".to_string(),
            token: "wrong-token".to_string(),
            identity: Some(identity("implementer")),
        },
    )
    .unwrap_err();
    assert_eq!(err.code, ERR_LEASE_TOKEN_MISMATCH);
}

#[test]
fn apply_fails_closed_without_reviewer_lease() {
    let mut conn = fresh_db();
    new_task(&mut conn, "t-1");
    let err = apply_task(
        &mut conn,
        &frozen(),
        &key("task.apply", "req-apply-1"),
        &ApplyInput {
            task_id: "t-1".to_string(),
            reviewer: "reviewer".to_string(),
            lease_token: "no-token".to_string(),
            fencing_counter: 1,
            identity: Some(identity("reviewer")),
        },
    )
    .unwrap_err();
    assert_eq!(err.code, ERR_LEASE_NOT_FOUND, "无 active lease 时受保护写必须 fail-closed");
    let status: String = conn
        .query_row("SELECT status FROM tasks WHERE id = 't-1'", [], |r| r.get(0))
        .unwrap();
    assert_eq!(status, "open", "校验失败不得改变 task data");
}

#[test]
fn apply_transitions_to_applied_with_valid_lease() {
    let mut conn = fresh_db();
    new_task(&mut conn, "t-1");
    let (token, counter) = acquire_impl(&mut conn, "t-1", "reviewer", "a");

    let resp = apply_task(
        &mut conn,
        &frozen(),
        &key("task.apply", "req-apply-2"),
        &ApplyInput {
            task_id: "t-1".to_string(),
            reviewer: "reviewer".to_string(),
            lease_token: token,
            fencing_counter: counter,
            identity: Some(identity("reviewer")),
        },
    )
    .expect("持有 reviewer lease 时 apply 应成功");
    assert_eq!(resp["status"], "applied");

    let (status, applied_at): (String, Option<f64>) = conn
        .query_row("SELECT status, applied_at FROM tasks WHERE id = 't-1'", [], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })
        .unwrap();
    assert_eq!(status, "applied");
    assert!(applied_at.is_some(), "apply 必须回填 applied_at");

    // 事件流 + identity 联动。
    let (to_status, actor): (String, String) = conn
        .query_row(
            "SELECT to_status, actor_identity FROM task_events WHERE task_id = 't-1'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(to_status, "applied");
    assert_eq!(actor, "agent-1");
    let identity_rows: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM action_identities WHERE task_id = 't-1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(identity_rows, 1);
}

#[test]
fn apply_rejects_stale_fencing_counter() {
    let mut conn = fresh_db();
    new_task(&mut conn, "t-1");
    let (token, _) = acquire_impl(&mut conn, "t-1", "reviewer", "a");
    let err = apply_task(
        &mut conn,
        &frozen(),
        &key("task.apply", "req-apply-3"),
        &ApplyInput {
            task_id: "t-1".to_string(),
            reviewer: "reviewer".to_string(),
            lease_token: token,
            fencing_counter: 999,
            identity: Some(identity("reviewer")),
        },
    )
    .unwrap_err();
    assert_eq!(err.code, ERR_LEASE_FENCING_STALE);
}

#[test]
fn close_transitions_to_closed_with_steps_done() {
    let mut conn = fresh_db();
    new_task(&mut conn, "t-1");
    conn.execute(
        "INSERT INTO task_steps (id, task_id, step_index, action, status, created_at) \
         VALUES ('s1', 't-1', 0, 'build', 'done', 0.0)",
        [],
    )
    .unwrap();
    let (token, counter) = acquire_impl(&mut conn, "t-1", "reviewer", "a");

    let resp = close_task(
        &mut conn,
        &frozen(),
        &key("task.close", "req-close-1"),
        &CloseInput {
            task_id: "t-1".to_string(),
            reviewer: "reviewer".to_string(),
            lease_token: token,
            fencing_counter: counter,
            identity: Some(identity("reviewer")),
        },
    )
    .expect("步骤全部 done 且持有 reviewer lease 时 close 应成功");
    assert_eq!(resp["status"], "closed");

    let (status, closed_at): (String, Option<f64>) = conn
        .query_row("SELECT status, closed_at FROM tasks WHERE id = 't-1'", [], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })
        .unwrap();
    assert_eq!(status, "closed");
    assert!(closed_at.is_some(), "close 必须写入真实 closed_at");
}

#[test]
fn close_rejects_child_tasks_not_closed() {
    let mut conn = fresh_db();
    new_task(&mut conn, "t-1");
    new_task(&mut conn, "t-2");
    conn.execute("UPDATE tasks SET parent_id = 't-1' WHERE id = 't-2'", []).unwrap();
    let (token, counter) = acquire_impl(&mut conn, "t-1", "reviewer", "a");

    let err = close_task(
        &mut conn,
        &frozen(),
        &key("task.close", "req-close-2"),
        &CloseInput {
            task_id: "t-1".to_string(),
            reviewer: "reviewer".to_string(),
            lease_token: token,
            fencing_counter: counter,
            identity: Some(identity("reviewer")),
        },
    )
    .unwrap_err();
    assert_eq!(err.code, ERR_CHILD_TASKS_NOT_CLOSED);
    let status: String = conn
        .query_row("SELECT status FROM tasks WHERE id = 't-1'", [], |r| r.get(0))
        .unwrap();
    assert_eq!(status, "open", "门禁拒绝不得改变 task data");
}

#[test]
fn close_rejects_leaf_task_without_steps() {
    let mut conn = fresh_db();
    new_task(&mut conn, "t-1");
    let (token, counter) = acquire_impl(&mut conn, "t-1", "reviewer", "a");
    let err = close_task(
        &mut conn,
        &frozen(),
        &key("task.close", "req-close-3"),
        &CloseInput {
            task_id: "t-1".to_string(),
            reviewer: "reviewer".to_string(),
            lease_token: token,
            fencing_counter: counter,
            identity: Some(identity("reviewer")),
        },
    )
    .unwrap_err();
    assert_eq!(err.code, ERR_NO_STEPS);
}
