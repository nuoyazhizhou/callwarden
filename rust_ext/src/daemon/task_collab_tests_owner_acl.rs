//! task-scoped registry owner ACL 回归（Linux 多用户 / 多 workspace）。
//!
//! 目标：证明携带 `task_id` 的 task-scoped 入口按**任务绑定的 workspace**
//! registry owner 裁决，而不是只依赖 `task.create` 期的请求 instance 校验。

use super::support::*;
use super::*;

/// 建只含 `instance` 一行的隔离 registry（owner = `owner_uid`）。
fn seed_registry(dir: &tempfile::TempDir, instance: &str, owner_uid: i64) -> std::path::PathBuf {
    let path = dir.path().join("registry_acl.db");
    let registry = WorkspaceRegistry::open(path.to_str().unwrap()).unwrap();
    drop(registry);
    let conn = Connection::open(&path).unwrap();
    conn.execute(
        "INSERT INTO daemon_workspaces
         (workspace_instance_id, owner_uid, client_view_root, host_real_root,
          registered_at, last_active_at, status)
         VALUES (?1, ?2, '/tmp/test-ws', '/tmp/test-ws', 0.0, 0.0, 'active')",
        params![instance, owner_uid],
    )
    .unwrap();
    drop(conn);
    path
}

#[test]
fn test_task_owner_acl_denies_cross_uid_on_bound_task() {
    let (dir, db_path) = temp_db();
    let registry_path = seed_registry(&dir, "ws-inst-test", 1000);
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_registry_db_path_for_tests(&registry_path);
    // seed_workspace 为 T-LEASE-1..10 写入 instance=ws-inst-test 的 capture+binding。
    seed_workspace(&store);

    assert!(store
        .verify_registry_owner_for_task(1000, "T-LEASE-1")
        .is_ok());
    let err = store
        .verify_registry_owner_for_task(1001, "T-LEASE-1")
        .unwrap_err();
    assert_eq!(err.code, "workspace_forbidden", "跨 UID 必须 fail-closed");

    // task.list 面：workspace 1 最新 capture 也是 ws-inst-test，同款裁决。
    let err = store
        .verify_registry_owner_for_workspace_id(1001, 1)
        .unwrap_err();
    assert_eq!(err.code, "workspace_forbidden");
    assert!(store.verify_registry_owner_for_workspace_id(1000, 1).is_ok());
}

#[test]
fn test_task_read_entries_reject_cross_workspace_scope() {
    // READ-1：客户端自报 workspace 只做一致性校验；与 binding 不一致必须 fail-closed，
    // 不能在 TokenSlim 目录下用其他 workspace 参数读出 callwarden 任务卡。
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path).unwrap();
    seed_workspace(&store);
    seed_task_binding(&store, "T-READ-SCOPE");
    let peer = PeerCredential::new_unix(1000, 1000, 1234);

    let params = serde_json::json!({"task_id": "T-READ-SCOPE", "workspace_id": 2});
    let err = store.handle_task_status(peer.clone(), &params).unwrap_err();
    assert_eq!(err.code, "E_TASK_WORKSPACE_SCOPE_MISMATCH");
    let err = store
        .handle_task_status_tree(peer.clone(), &params)
        .unwrap_err();
    assert_eq!(err.code, "E_TASK_WORKSPACE_SCOPE_MISMATCH");
    let err = store
        .handle_task_governance_projection_get(peer.clone(), &params)
        .unwrap_err();
    assert_eq!(err.code, "E_TASK_WORKSPACE_SCOPE_MISMATCH");

    // 显式同名 binding workspace 时正常放行。
    let ok = store
        .handle_task_status(
            peer,
            &serde_json::json!({"task_id": "T-READ-SCOPE", "workspace_id": 1}),
        )
        .unwrap();
    assert_eq!(ok["task_id"], "T-READ-SCOPE");
}

#[test]
fn test_task_read_entries_reject_missing_binding() {
    // READ-1：无 task_workspace_bindings 的历史/legacy 卡不得默认放行；
    // 必须先走 cw task attest-legacy-workspace-binding 显式补绑。
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path).unwrap();
    seed_workspace(&store);
    {
        let conn = store.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at, parent_id)
             VALUES ('T-READ-NOBIND', 'unbound', '', 'test', 'open', 1.0, 1.0, '')",
            [],
        )
        .unwrap();
    }
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    let params = serde_json::json!({"task_id": "T-READ-NOBIND"});

    let err = store.handle_task_status(peer.clone(), &params).unwrap_err();
    assert_eq!(err.code, "E_TASK_BINDING_MISSING");
    let err = store
        .handle_task_status_tree(peer.clone(), &params)
        .unwrap_err();
    assert_eq!(err.code, "E_TASK_BINDING_MISSING");
    let err = store
        .handle_task_governance_projection_get(peer, &params)
        .unwrap_err();
    assert_eq!(err.code, "E_TASK_BINDING_MISSING");
}

#[test]
fn test_task_read_entries_enforce_workspace_owner_acl() {
    // READ-1：读入口复用 task.list 的 owner ACL，跨 UID 不得读取绑定任务卡。
    let (dir, db_path) = temp_db();
    let registry_path = seed_registry(&dir, "ws-inst-test", 1000);
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_registry_db_path_for_tests(&registry_path);
    seed_workspace(&store);
    seed_task_binding(&store, "T-READ-ACL");
    let owner = PeerCredential::new_unix(1000, 1000, 1234);
    let intruder = PeerCredential::new_unix(1001, 1001, 1234);
    let params = serde_json::json!({"task_id": "T-READ-ACL"});

    assert!(store
        .handle_task_status(owner.clone(), &params)
        .is_ok());
    for code in [
        store
            .handle_task_status(intruder.clone(), &params)
            .unwrap_err()
            .code,
        store
            .handle_task_status_tree(intruder.clone(), &params)
            .unwrap_err()
            .code,
        store
            .handle_task_governance_projection_get(intruder, &params)
            .unwrap_err()
            .code,
    ] {
        assert_eq!(code, "workspace_forbidden", "跨 UID 必须 fail-closed");
    }
}

#[test]
fn test_task_owner_acl_allows_legacy_and_missing_registry_row() {
    let (dir, db_path) = temp_db();
    // registry 存在但无 ws-inst-test 行：未注册 / legacy 项目保持可读写。
    let registry_path = seed_registry(&dir, "other-instance", 1000);
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_registry_db_path_for_tests(&registry_path);
    seed_workspace(&store);

    assert!(store
        .verify_registry_owner_for_task(1001, "T-LEASE-1")
        .is_ok());
    assert!(store.verify_registry_owner_for_workspace_id(1001, 1).is_ok());
    // 无 binding 的 legacy 卡 → 放行。
    assert!(store
        .verify_registry_owner_for_task(1001, "T-NO-BINDING")
        .is_ok());
}

#[test]
fn test_task_owner_acl_strict_requires_binding_for_mutation() {
    let (dir, db_path) = temp_db();
    let registry_path = seed_registry(&dir, "ws-inst-test", 1000);
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_registry_db_path_for_tests(&registry_path);
    seed_workspace(&store);

    // 已绑定：同 UID 放行，跨 UID forbidden。
    assert!(store
        .verify_registry_owner_for_task_strict(1000, "T-LEASE-1")
        .is_ok());
    let err = store
        .verify_registry_owner_for_task_strict(1001, "T-LEASE-1")
        .unwrap_err();
    assert_eq!(err.code, "workspace_forbidden");

    // 未绑定 legacy 卡：写面 fail-closed 要求先 attestation；只读面仍放行。
    let err = store
        .verify_registry_owner_for_task_strict(1000, "T-NO-BINDING")
        .unwrap_err();
    assert_eq!(err.code, "E_TASK_BINDING_ATTESTATION_REQUIRED");
    assert!(store
        .verify_registry_owner_for_task(1000, "T-NO-BINDING")
        .is_ok());
}
