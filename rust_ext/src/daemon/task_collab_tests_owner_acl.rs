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
