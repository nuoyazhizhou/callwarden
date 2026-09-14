//! T-1788595874892-c9b82244：legacy parent → canonical child 有界权威桥回归。
//!
//! 卡片语义（冻结）：
//! - 仅当 daemon 持久 registry、两侧不可变 capture 与 project root/owner 溯源全部
//!   证明同一 authority 才放行 canonical child；
//! - 不可证/歧义必须 fail-closed（E_WORKSPACE_AUTHORITY_MISMATCH，零部分行、零 alias）；
//! - append-only 等价 provenance（workspace_reconciliation_aliases），不改历史
//!   binding/capture、不传播 legacy instance、不放松 exact numeric workspace rejection；
//! - root create 与 ordinary same-instance parent 行为不变。

use super::support::*;
use super::*;
use rusqlite::params;

/// 测试用共享 root hash（与 fixture /tmp/test-ws 一致）。
fn bridge_root_hash() -> String {
    crate::canonicalize::sha256_hex("/tmp/test-ws".as_bytes())
}

/// 构造与 bind_task_to_workspace 公式逐字节一致的 capture INSERT 参数组。
struct CaptureSeed {
    capture_id: String,
    workspace_id: i64,
    instance: String,
    root_hash: String,
    identity_payload_override: Option<serde_json::Value>,
}

fn insert_capture(store: &TaskCollabStore, seed: &CaptureSeed) {
    let conn = store.conn.lock().unwrap();
    if seed.workspace_id != 1 {
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) \
             VALUES (?1, 'bridge-ws', ?2, ?3, 1)",
            params![
                seed.workspace_id,
                format!("/tmp/bridge-ws-{}", seed.workspace_id),
                1_700_000_000.0_f64
            ],
        )
        .unwrap();
    }
    let root_hash = seed.root_hash.clone();
    let manifest_payload = serde_json::json!({
        "workspace_id": seed.workspace_id,
        "workspace_name": "test-ws",
        "root_path_hash": root_hash,
        "manifest_format_version": "workspace-manifest-c14n/v1",
    });
    let manifest_payload_json = manifest_payload.to_string();
    let manifest_hash = crate::canonicalize::sha256_hex(manifest_payload_json.as_bytes());
    let identity_hash = crate::daemon::task_loop::create::registry_identity_hash(
        &seed.instance,
        &root_hash,
        &root_hash,
        &manifest_hash,
    );
    let registry_payload = seed
        .identity_payload_override
        .clone()
        .unwrap_or_else(|| {
            serde_json::json!({
                "workspace_instance_id": seed.instance,
                "client_view_root_hash": root_hash,
                "host_real_root_hash": root_hash,
                "workspace_manifest_hash": manifest_hash,
            })
        })
        .to_string();
    let revision: i64 = conn
        .query_row(
            "SELECT COALESCE(MAX(capture_revision), 0) + 1 \
             FROM workspace_authority_captures WHERE workspace_id = ?1",
            params![seed.workspace_id],
            |r| r.get(0),
        )
        .unwrap();
    conn.execute(
        "INSERT OR IGNORE INTO workspace_authority_captures
         (workspace_capture_id, workspace_id, capture_revision, supersedes_capture_id,
          daemon_workspace_id, workspace_instance_id, capture_canonicalization_version,
          capture_canonicalization_rules_hash, registry_identity_payload_json,
          registry_identity_hash, workspace_manifest_payload_json, workspace_manifest_hash,
          client_view_root_hash, host_real_root_hash, created_by, authoritative_created_at)
         VALUES (?1, ?2, ?3, NULL, 0, ?4, 'workspace-capture-c14n/v1',
                 'test-rules-hash', ?5, ?6, ?7, ?8, ?9, ?9, 'test', ?10)",
        params![
            seed.capture_id,
            seed.workspace_id,
            revision,
            seed.instance,
            registry_payload,
            identity_hash,
            manifest_payload_json,
            manifest_hash,
            root_hash,
            1_700_000_000.0_f64,
        ],
    )
    .unwrap();
    drop(conn);
}

/// seed 一个 parent 任务：workspace 1 + 指定 instance/root hash 的 capture + binding。
fn seed_parent_task_with_instance(
    store: &TaskCollabStore,
    task_id: &str,
    instance: &str,
    root_hash: &str,
) {
    seed_workspace(store);
    let ts = 1_700_000_000.0_f64;
    let conn = store.conn.lock().unwrap();
    conn.execute(
        "INSERT OR IGNORE INTO tasks (id, title, description, creator, status, created_at, updated_at, parent_id)
         VALUES (?1, 'bridge parent', '', 'test', 'open', ?2, ?2, '')",
        params![task_id, ts],
    )
    .unwrap();
    drop(conn);
    insert_capture(
        store,
        &CaptureSeed {
            capture_id: format!("cap-bridge-{}", task_id),
            workspace_id: 1,
            instance: instance.to_string(),
            root_hash: root_hash.to_string(),
            identity_payload_override: None,
        },
    );
    let conn = store.conn.lock().unwrap();
    conn.execute(
        "INSERT OR IGNORE INTO task_workspace_bindings
         (task_id, workspace_id, workspace_binding_id, workspace_capture_id, created_by, authoritative_created_at)
         VALUES (?1, 1, ?2, ?3, 'test', ?4)",
        params![
            task_id,
            format!("tb-bridge-{}", task_id),
            format!("cap-bridge-{}", task_id),
            ts,
        ],
    )
    .unwrap();
    drop(conn);
}

fn child_role_contracts() -> serde_json::Value {
    serde_json::json!([
        {"role": "executor", "independence": "{}", "handoff_to": "reviewer"},
        {"role": "reviewer", "independence": "{}", "handoff_to": "adjudicator"},
        {"role": "adjudicator", "independence": "{}", "handoff_to": "complete"},
    ])
}

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

/// 查询 child 实际落库 binding 引用的 capture instance（验证 legacy 不传播）。
fn child_binding_instance(store: &TaskCollabStore, task_id: &str) -> String {
    store
        .conn
        .lock()
        .unwrap()
        .query_row(
            "SELECT c.workspace_instance_id FROM task_workspace_bindings b \
             JOIN workspace_authority_captures c \
               ON c.workspace_capture_id = b.workspace_capture_id \
             WHERE b.task_id = ?1",
            params![task_id],
            |r| r.get(0),
        )
        .unwrap()
}

/// 查询等价 provenance alias 行数。
fn alias_count(store: &TaskCollabStore, alias_id: &str) -> i64 {
    store
        .conn
        .lock()
        .unwrap()
        .query_row(
            "SELECT COUNT(*) FROM workspace_reconciliation_aliases WHERE alias_id = ?1",
            params![alias_id],
            |r| r.get(0),
        )
        .unwrap()
}

/// 正向：legacy ws-1 parent + canonical child（全溯源一致）→ 放行；
/// legacy 不传播、alias append-only 且幂等。
#[test]
fn parent_aware_task_create_legacy_parent_bridge_to_canonical_child_success() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    let root = bridge_root_hash();
    seed_parent_task_with_instance(&store, "T-PARENT-LEGACY-1", "ws-1", &root);
    insert_capture(
        &store,
        &CaptureSeed {
            capture_id: "cap-canonical-1".into(),
            workspace_id: 1,
            instance: "ws-inst-canonical".into(),
            root_hash: root.clone(),
            identity_payload_override: None,
        },
    );

    let res = store
        .handle_task_create(
            peer.clone(),
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-canonical",
                "task_id": "T-CHILD-BRIDGE-1",
                "title": "bridge child",
                "parent_id": "T-PARENT-LEGACY-1",
                "steps": [{"action": "implement", "target_file": "child.rs"}],
                "role_contracts": child_role_contracts(),
                "identity_policy": "legacy_identity_v1",
            }),
        )
        .expect("legacy→canonical 有界桥：溯源一致必须放行");
    assert_eq!(res["task_id"], "T-CHILD-BRIDGE-1");
    assert_eq!(res["workspace_instance_id"], "ws-inst-canonical");
    // legacy instance 不得传播给新任务。
    assert_eq!(
        child_binding_instance(&store, "T-CHILD-BRIDGE-1"),
        "ws-inst-canonical"
    );
    // append-only 等价 provenance，root_path_hash 记录真实共享 root。
    assert_eq!(alias_count(&store, "wa-ws-inst-canonical-ws-1"), 1);
    let conn = store.conn.lock().unwrap();
    let alias_root: String = conn
        .query_row(
            "SELECT root_path_hash FROM workspace_reconciliation_aliases \
             WHERE alias_id = 'wa-ws-inst-canonical-ws-1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    drop(conn);
    assert_eq!(alias_root, root);
    // 历史行未被改写：parent binding/capture 仍是 legacy ws-1。
    assert_eq!(
        child_binding_instance(&store, "T-PARENT-LEGACY-1"),
        "ws-1"
    );

    // 幂等：第二个 child 不重复写 alias。
    store
        .handle_task_create(
            peer,
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-canonical",
                "task_id": "T-CHILD-BRIDGE-2",
                "title": "bridge child 2",
                "parent_id": "T-PARENT-LEGACY-1",
                "steps": [{"action": "implement", "target_file": "child2.rs"}],
                "role_contracts": child_role_contracts(),
                "identity_policy": "legacy_identity_v1",
            }),
        )
        .expect("第二个 canonical child 同样放行");
    assert_eq!(alias_count(&store, "wa-ws-inst-canonical-ws-1"), 1);
}

/// 负向：canonical capture root 与 parent 不同 → 非同一 authority → fail-closed。
#[test]
fn parent_aware_task_create_rejects_bridge_on_root_hash_divergence() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_parent_task_with_instance(&store, "T-PARENT-LEGACY-2", "ws-1", &bridge_root_hash());
    let other_root = crate::canonicalize::sha256_hex("/tmp/other-project".as_bytes());
    insert_capture(
        &store,
        &CaptureSeed {
            capture_id: "cap-canonical-2".into(),
            workspace_id: 1,
            instance: "ws-inst-canonical".into(),
            root_hash: other_root,
            identity_payload_override: None,
        },
    );

    let err = store
        .handle_task_create(
            peer,
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-canonical",
                "task_id": "T-CHILD-BRIDGE-DIVERGE",
                "title": "bridge diverge",
                "parent_id": "T-PARENT-LEGACY-2",
                "steps": [{"action": "implement", "target_file": "c.rs"}],
                "role_contracts": child_role_contracts(),
                "identity_policy": "legacy_identity_v1",
            }),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_WORKSPACE_AUTHORITY_MISMATCH");
    assert_eq!(task_row_count(&store, "T-CHILD-BRIDGE-DIVERGE"), 0);
    assert_eq!(task_binding_count(&store, "T-CHILD-BRIDGE-DIVERGE"), 0);
    assert_eq!(alias_count(&store, "wa-ws-inst-canonical-ws-1"), 0);
}

/// 负向：parent capture instance 非 legacy ws-<digits> 形态 → 桥不适用。
#[test]
fn parent_aware_task_create_rejects_bridge_for_non_legacy_parent_instance() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_parent_task_with_instance(
        &store,
        "T-PARENT-NONLEGACY-1",
        "ws-inst-test",
        &bridge_root_hash(),
    );
    insert_capture(
        &store,
        &CaptureSeed {
            capture_id: "cap-canonical-3".into(),
            workspace_id: 1,
            instance: "ws-inst-canonical".into(),
            root_hash: bridge_root_hash(),
            identity_payload_override: None,
        },
    );

    let err = store
        .handle_task_create(
            peer,
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-canonical",
                "task_id": "T-CHILD-BRIDGE-NONLEGACY",
                "title": "bridge nonlegacy",
                "parent_id": "T-PARENT-NONLEGACY-1",
                "steps": [{"action": "implement", "target_file": "c.rs"}],
                "role_contracts": child_role_contracts(),
                "identity_policy": "legacy_identity_v1",
            }),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_WORKSPACE_AUTHORITY_MISMATCH");
    assert_eq!(task_row_count(&store, "T-CHILD-BRIDGE-NONLEGACY"), 0);
}

/// 负向：canonical instance 只在其它数字 workspace 有 capture → exact numeric
/// workspace rejection 不放松。
#[test]
fn parent_aware_task_create_rejects_bridge_on_canonical_workspace_divergence() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_parent_task_with_instance(&store, "T-PARENT-LEGACY-3", "ws-1", &bridge_root_hash());
    insert_capture(
        &store,
        &CaptureSeed {
            capture_id: "cap-cross-1".into(),
            workspace_id: 2,
            instance: "ws-inst-cross".into(),
            root_hash: bridge_root_hash(),
            identity_payload_override: None,
        },
    );

    let err = store
        .handle_task_create(
            peer,
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-cross",
                "task_id": "T-CHILD-BRIDGE-CROSS",
                "title": "bridge cross ws",
                "parent_id": "T-PARENT-LEGACY-3",
                "steps": [{"action": "implement", "target_file": "c.rs"}],
                "role_contracts": child_role_contracts(),
                "identity_policy": "legacy_identity_v1",
            }),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_WORKSPACE_AUTHORITY_MISMATCH");
    assert_eq!(task_row_count(&store, "T-CHILD-BRIDGE-CROSS"), 0);
}

/// 负向：requested instance 在 daemon 持久 capture 域无记录 → 不得 invent。
#[test]
fn parent_aware_task_create_rejects_bridge_on_missing_canonical_authority() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_parent_task_with_instance(&store, "T-PARENT-LEGACY-4", "ws-1", &bridge_root_hash());

    let err = store
        .handle_task_create(
            peer,
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-unknown",
                "task_id": "T-CHILD-BRIDGE-MISSING",
                "title": "bridge missing",
                "parent_id": "T-PARENT-LEGACY-4",
                "steps": [{"action": "implement", "target_file": "c.rs"}],
                "role_contracts": child_role_contracts(),
                "identity_policy": "legacy_identity_v1",
            }),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_WORKSPACE_AUTHORITY_MISMATCH");
    assert_eq!(task_row_count(&store, "T-CHILD-BRIDGE-MISSING"), 0);
    assert_eq!(alias_count(&store, "wa-ws-inst-unknown-ws-1"), 0);
}

/// 负向：registry identity payload 与共享 root/instance 不符（身份不可证）→ fail-closed。
#[test]
fn parent_aware_task_create_rejects_bridge_on_identity_payload_tamper() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    let root = bridge_root_hash();
    seed_parent_task_with_instance(&store, "T-PARENT-LEGACY-5", "ws-1", &root);
    // 列值 root 一致，但 daemon identity payload 被篡改（client root 指向别处）→ 拒绝。
    let tampered = serde_json::json!({
        "workspace_instance_id": "ws-inst-canonical",
        "client_view_root_hash": "deadbeef-tampered",
        "host_real_root_hash": "deadbeef-tampered",
        "workspace_manifest_hash": "deadbeef-tampered",
    });
    insert_capture(
        &store,
        &CaptureSeed {
            capture_id: "cap-canonical-5".into(),
            workspace_id: 1,
            instance: "ws-inst-canonical".into(),
            root_hash: root,
            identity_payload_override: Some(tampered),
        },
    );

    let err = store
        .handle_task_create(
            peer,
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-canonical",
                "task_id": "T-CHILD-BRIDGE-TAMPER",
                "title": "bridge tamper",
                "parent_id": "T-PARENT-LEGACY-5",
                "steps": [{"action": "implement", "target_file": "c.rs"}],
                "role_contracts": child_role_contracts(),
                "identity_policy": "legacy_identity_v1",
            }),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_WORKSPACE_AUTHORITY_MISMATCH");
    assert_eq!(task_row_count(&store, "T-CHILD-BRIDGE-TAMPER"), 0);
}
