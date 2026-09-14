//! GATE-1A：parent-aware governed task.create daemon 冻结测试。
//!
//! 冻结来源：docs/design/cw-role-prompt-compiler-v1-frozen-spec.md §13.3
//! （frozen spec SHA-256 95298729F3357CDBE76D8F8E91F12067B54D2661D6E80ABFF561A2E2A8C86CB7）。
//! 测试函数名精确冻结，不得改名或用更宽 filter 偷换覆盖范围。
//!
//! 语义：
//! - parent-aware 分支仅在 parent_id 规范化非空时启用；
//! - root 分支（parent_id 缺失/空）保持 Gate 前行为 bit-for-bit；
//! - parent 分支校验 parent 存在、唯一 binding/capture、workspace exact match、
//!   完整 Role Contracts；任何缺口整事务 rollback。

use super::support::*;
use super::*;
use rusqlite::Connection;

/// seed 一个可作 parent 的任务：workspace 1 + ws-inst-test 的不可变 binding/capture
/// （seed_task_binding 已建立 capture 链，capture instance=ws-inst-test）。
fn seed_parent_task(store: &TaskCollabStore, task_id: &str) {
    seed_workspace(store);
    seed_task_binding(store, task_id);
}

/// 构造 child create 的标准 role_contracts（三份最小 legacy 契约）。
fn child_role_contracts() -> serde_json::Value {
    serde_json::json!([
        {"role": "executor", "independence": "{}", "handoff_to": "reviewer"},
        {"role": "reviewer", "independence": "{}", "handoff_to": "adjudicator"},
        {"role": "adjudicator", "independence": "{}", "handoff_to": "complete"},
    ])
}

/// 查询子任务行的数量（用于 rollback 断言）。
fn task_row_count(store: &TaskCollabStore, task_id: &str) -> i64 {
    store
        .conn
        .lock()
        .unwrap()
        .query_row(
            "SELECT COUNT(*) FROM tasks WHERE id = ?1",
            params![task_id],
            |r| r.get(0),
        )
        .unwrap()
}

/// 查询子任务的 binding 数量（rollback 断言）。
fn task_binding_count(store: &TaskCollabStore, task_id: &str) -> i64 {
    store
        .conn
        .lock()
        .unwrap()
        .query_row(
            "SELECT COUNT(*) FROM task_workspace_bindings WHERE task_id = ?1",
            params![task_id],
            |r| r.get(0),
        )
        .unwrap()
}

/// 查询子任务的 event 数量（rollback 断言）。
fn task_event_count(store: &TaskCollabStore, task_id: &str) -> i64 {
    store
        .conn
        .lock()
        .unwrap()
        .query_row(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?1",
            params![task_id],
            |r| r.get(0),
        )
        .unwrap()
}

/// 查询子任务的 step 数量（rollback 断言）。
fn task_step_count(store: &TaskCollabStore, task_id: &str) -> i64 {
    store
        .conn
        .lock()
        .unwrap()
        .query_row(
            "SELECT COUNT(*) FROM task_steps WHERE task_id = ?1",
            params![task_id],
            |r| r.get(0),
        )
        .unwrap()
}

/// 查询子任务的 role_contract 数量（rollback 断言）。
fn task_contract_count(store: &TaskCollabStore, task_id: &str) -> i64 {
    store
        .conn
        .lock()
        .unwrap()
        .query_row(
            "SELECT COUNT(*) FROM role_contracts WHERE task_id = ?1",
            params![task_id],
            |r| r.get(0),
        )
        .unwrap()
}

#[test]
fn parent_aware_task_create_root_request_response_error_parity() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_workspace(&store);

    // root 分支：parent_id 缺失 → 与 Gate 前行为完全一致（成功创建，binding 1 条）。
    let res = store
        .handle_task_create(
            peer.clone(),
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-test",
                "task_id": "T-ROOT-PARITY-1",
                "title": "root parity",
                "steps": [{"action": "implement", "target_file": "a.rs"}],
            }),
        )
        .unwrap();
    // response schema：task_id / status / workspace 配对 / binding / capture / assignment。
    assert_eq!(res["task_id"], "T-ROOT-PARITY-1");
    assert_eq!(res["status"], "open");
    assert_eq!(res["workspace_id"], 1);
    assert_eq!(res["workspace_instance_id"], "ws-inst-test");
    assert!(res["workspace_binding_id"].as_str().unwrap().starts_with("tb-"));
    assert!(res["workspace_capture_id"].as_str().unwrap().starts_with("wc-"));
    assert_eq!(task_binding_count(&store, "T-ROOT-PARITY-1"), 1);
    // root 分支允许空 role_contracts（Gate 前行为，parent-aware 强制检查不启用）。
    let res2 = store
        .handle_task_create(
            peer.clone(),
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-test",
                "task_id": "T-ROOT-PARITY-2",
                "title": "root parity no contracts",
            }),
        )
        .unwrap();
    assert_eq!(res2["task_id"], "T-ROOT-PARITY-2");
    // root 分支不允许未知字段检查（不启用 parent-aware 拒绝）——白名单外字段被忽略。
    let res3 = store
        .handle_task_create(
            peer.clone(),
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-test",
                "task_id": "T-ROOT-PARITY-3",
                "title": "root parity extra field",
                "extra_bogus_field": "ignored-on-root",
            }),
        )
        .unwrap();
    assert_eq!(res3["task_id"], "T-ROOT-PARITY-3");
}

#[test]
fn parent_aware_task_create_governed_parent_success() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_parent_task(&store, "T-PARENT-OK-1");

    // parent-aware 成功：parent 存在 + 唯一 binding + workspace exact match + contracts。
    let res = store
        .handle_task_create(
            peer.clone(),
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-test",
                "task_id": "T-CHILD-OK-1",
                "title": "governed child",
                "parent_id": "T-PARENT-OK-1",
                "steps": [{"action": "implement", "target_file": "child.rs"}],
                "role_contracts": child_role_contracts(),
                "identity_policy": "legacy_identity_v1",
            }),
        )
        .unwrap();
    assert_eq!(res["task_id"], "T-CHILD-OK-1");
    assert_eq!(res["status"], "open");
    assert_eq!(res["workspace_id"], 1);
    assert_eq!(res["workspace_instance_id"], "ws-inst-test");
    assert_eq!(task_binding_count(&store, "T-CHILD-OK-1"), 1);
    // child 的 parent_id 落库。
    let conn = store.conn.lock().unwrap();
    let stored_parent: String = conn
        .query_row(
            "SELECT parent_id FROM tasks WHERE id = 'T-CHILD-OK-1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(stored_parent, "T-PARENT-OK-1");
    // 显式释放 conn guard：后续 task_contract_count 会再次 lock conn
    // （std Mutex 非重入；debug 构建无 drop elaboration，guard 存活到函数末尾）。
    drop(conn);
    // 受治理 child 写入三角色 role_contracts。
    assert_eq!(task_contract_count(&store, "T-CHILD-OK-1"), 3);
}

#[test]
fn parent_aware_task_create_rejects_missing_or_unbound_parent() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_workspace(&store);

    // parent 不存在 → E_TASK_PARENT_NOT_FOUND。
    let err = store
        .handle_task_create(
            peer.clone(),
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-test",
                "task_id": "T-CHILD-NO-PARENT",
                "title": "child missing parent",
                "parent_id": "T-PARENT-GHOST",
                "steps": [{"action": "implement", "target_file": "c.rs"}],
                "role_contracts": child_role_contracts(),
                "identity_policy": "legacy_identity_v1",
            }),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_TASK_PARENT_NOT_FOUND");
    // 失败不留任何 child 行。
    assert_eq!(task_row_count(&store, "T-CHILD-NO-PARENT"), 0);
    assert_eq!(task_binding_count(&store, "T-CHILD-NO-PARENT"), 0);

    // parent 存在但无 binding → E_TASK_PARENT_UNBOUND。
    // 直接 SQL seed 一个无 binding 的 parent（绕过 create 的 binding 强制）。
    {
        let conn = store.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at, parent_id)
             VALUES ('T-PARENT-UNBOUND-2', 'unbound parent', '', 'test', 'open', 1700000000.0, 1700000000.0, '')",
            [],
        )
        .unwrap();
    }
    let err = store
        .handle_task_create(
            peer.clone(),
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-test",
                "task_id": "T-CHILD-UNBOUND",
                "title": "child of unbound parent",
                "parent_id": "T-PARENT-UNBOUND-2",
                "steps": [{"action": "implement", "target_file": "c2.rs"}],
                "role_contracts": child_role_contracts(),
                "identity_policy": "legacy_identity_v1",
            }),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_TASK_PARENT_UNBOUND");
    assert_eq!(task_row_count(&store, "T-CHILD-UNBOUND"), 0);
}

#[test]
fn parent_aware_task_create_rejects_workspace_mismatch() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_parent_task(&store, "T-PARENT-WS-1");

    // child 请求不同 workspace_id（workspace 2 不存在）→ mismatch。
    let err = store
        .handle_task_create(
            peer.clone(),
            &serde_json::json!({
                "workspace_id": 2, "workspace_instance_id": "ws-inst-test",
                "task_id": "T-CHILD-WS-MISMATCH",
                "title": "child ws mismatch",
                "parent_id": "T-PARENT-WS-1",
                "steps": [{"action": "implement", "target_file": "c.rs"}],
                "role_contracts": child_role_contracts(),
                "identity_policy": "legacy_identity_v1",
            }),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_WORKSPACE_AUTHORITY_MISMATCH");
    assert!(err.message.contains("workspace=1"));
    assert_eq!(task_row_count(&store, "T-CHILD-WS-MISMATCH"), 0);
}

#[test]
fn parent_aware_task_create_rejects_missing_contract_or_role_contract() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_parent_task(&store, "T-PARENT-CONTRACT-1");

    // parent-aware child 缺 role_contracts → E_TASK_PARENT_CONTRACT_REQUIRED。
    let err = store
        .handle_task_create(
            peer.clone(),
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-test",
                "task_id": "T-CHILD-NO-CONTRACT",
                "title": "child no contract",
                "parent_id": "T-PARENT-CONTRACT-1",
                "steps": [{"action": "implement", "target_file": "c.rs"}],
            }),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_TASK_PARENT_CONTRACT_REQUIRED");
    assert_eq!(task_row_count(&store, "T-CHILD-NO-CONTRACT"), 0);
    assert_eq!(task_binding_count(&store, "T-CHILD-NO-CONTRACT"), 0);
}

#[test]
fn parent_aware_task_create_rejects_unresolved_identity_policy() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_parent_task(&store, "T-PARENT-POLICY-1");

    // parent-aware child 有 contracts 但 identity_policy 缺失/非法 → E_TASK_IDENTITY_POLICY_REQUIRED。
    let err = store
        .handle_task_create(
            peer.clone(),
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-test",
                "task_id": "T-CHILD-NO-POLICY",
                "title": "child no policy",
                "parent_id": "T-PARENT-POLICY-1",
                "steps": [{"action": "implement", "target_file": "c.rs"}],
                "role_contracts": child_role_contracts(),
            }),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_TASK_IDENTITY_POLICY_REQUIRED");
    assert_eq!(task_row_count(&store, "T-CHILD-NO-POLICY"), 0);
    // 未知 policy 值 → E_TASK_IDENTITY_POLICY_MISMATCH。
    let err2 = store
        .handle_task_create(
            peer.clone(),
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-test",
                "task_id": "T-CHILD-BAD-POLICY",
                "title": "child bad policy",
                "parent_id": "T-PARENT-POLICY-1",
                "steps": [{"action": "implement", "target_file": "c.rs"}],
                "role_contracts": child_role_contracts(),
                "identity_policy": "no_such_policy_v9",
            }),
        )
        .unwrap_err();
    assert_eq!(err2.code, "E_TASK_IDENTITY_POLICY_MISMATCH");
    assert_eq!(task_row_count(&store, "T-CHILD-BAD-POLICY"), 0);
}

#[test]
fn parent_aware_task_create_rejects_unknown_field() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_parent_task(&store, "T-PARENT-UNKNOWN-1");

    // parent-aware 分支：frozen request contract 之外的未知字段 → E_TASK_CREATE_UNKNOWN_FIELD。
    let err = store
        .handle_task_create(
            peer.clone(),
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-test",
                "task_id": "T-CHILD-UNKNOWN",
                "title": "child unknown field",
                "parent_id": "T-PARENT-UNKNOWN-1",
                "steps": [{"action": "implement", "target_file": "c.rs"}],
                "role_contracts": child_role_contracts(),
                "identity_policy": "legacy_identity_v1",
                "totally_unknown_field": "must be rejected",
            }),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_TASK_CREATE_UNKNOWN_FIELD");
    assert!(err.message.contains("totally_unknown_field"));
    assert_eq!(task_row_count(&store, "T-CHILD-UNKNOWN"), 0);
}

#[test]
fn parent_aware_task_create_rolls_back_all_rows_on_failure() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_parent_task(&store, "T-PARENT-ROLLBACK-1");

    // 触发失败：parent-aware + workspace mismatch（在 binding/capture 写入之前）。
    let err = store
        .handle_task_create(
            peer.clone(),
            &serde_json::json!({
                "workspace_id": 9, "workspace_instance_id": "ws-inst-test",
                "task_id": "T-CHILD-ROLLBACK",
                "title": "child rollback",
                "parent_id": "T-PARENT-ROLLBACK-1",
                "steps": [{"action": "implement", "target_file": "c.rs"}],
                "role_contracts": child_role_contracts(),
                "identity_policy": "legacy_identity_v1",
            }),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_WORKSPACE_AUTHORITY_MISMATCH");
    // 零部分行：tasks / binding / events / steps / contracts 全部为 0。
    assert_eq!(task_row_count(&store, "T-CHILD-ROLLBACK"), 0);
    assert_eq!(task_binding_count(&store, "T-CHILD-ROLLBACK"), 0);
    assert_eq!(task_event_count(&store, "T-CHILD-ROLLBACK"), 0);
    assert_eq!(task_step_count(&store, "T-CHILD-ROLLBACK"), 0);
    assert_eq!(task_contract_count(&store, "T-CHILD-ROLLBACK"), 0);
}
