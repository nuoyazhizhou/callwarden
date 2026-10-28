//! T-1788392053931-05b8a0e4 回归：task.create 在 capture 链多 instance 并存
//! （legacy `ws-1` 为最新、canonical 稳定 instance 已确立）下的绑定语义。
//!
//! - canonical instance 已在本 workspace capture 链确立 → 必须接受（binding/capture
//!   正常追加），不得因「最新 capture 是 legacy ws-1」被重推导拒绝；
//! - 完全未确立的新 instance（合成/跨项目）→ 仍 fail-closed，零部分行；
//! - legacy ws-1（最新 capture instance 本身）→ 保持接受（向后兼容）。

use super::support::*;
use super::*;

/// 在 workspace 1 上插入一条指定 instance/revision 的 capture（identity hash 与
/// bind_task_to_workspace 的 workspace-capture-c14n/v1 公式逐字节一致）。
fn insert_capture_with_instance(store: &TaskCollabStore, instance: &str, revision: i64) {
    let conn = store.conn.lock().unwrap();
    let root_hash = crate::canonicalize::sha256_hex("/tmp/test-ws".as_bytes());
    let manifest_payload = serde_json::json!({
        "workspace_id": 1,
        "workspace_name": "test-ws",
        "root_path_hash": root_hash,
        "manifest_format_version": "workspace-manifest-c14n/v1",
    });
    let manifest_payload_json = manifest_payload.to_string();
    let manifest_hash = crate::canonicalize::sha256_hex(manifest_payload_json.as_bytes());
    let identity_hash = crate::daemon::task_loop::create::registry_identity_hash(
        instance,
        &root_hash,
        &root_hash,
        &manifest_hash,
    );
    let registry_payload = serde_json::json!({
        "workspace_instance_id": instance,
        "client_view_root_hash": root_hash,
        "host_real_root_hash": root_hash,
        "workspace_manifest_hash": manifest_hash,
    })
    .to_string();
    conn.execute(
        "INSERT OR IGNORE INTO workspace_authority_captures
         (workspace_capture_id, workspace_id, capture_revision, supersedes_capture_id,
          daemon_workspace_id, workspace_instance_id, capture_canonicalization_version,
          capture_canonicalization_rules_hash, registry_identity_payload_json,
          registry_identity_hash, workspace_manifest_payload_json, workspace_manifest_hash,
          client_view_root_hash, host_real_root_hash, created_by, authoritative_created_at)
         VALUES (?1, 1, ?2, NULL, 0, ?3, 'workspace-capture-c14n/v1',
                 'test-rules-hash', ?4, ?5, ?6, ?7, ?8, ?8, 'test', ?9)",
        params![
            format!("cap-wsa-{}-{}", instance, revision),
            revision,
            instance,
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

fn capture_count(store: &TaskCollabStore, instance: &str) -> i64 {
    let conn = store.conn.lock().unwrap();
    conn.query_row(
        "SELECT COUNT(*) FROM workspace_authority_captures \
         WHERE workspace_id = 1 AND workspace_instance_id = ?1",
        params![instance],
        |r| r.get(0),
    )
    .unwrap()
}

/// 正向：canonical instance 已确立（即便最新 capture 是 legacy ws-1）→ create 成功，
/// 恰好一条不可变 binding，capture 链按 instance 追加。
#[test]
fn test_live_tuple_canonical_instance_accepted_despite_legacy_latest() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path).unwrap();
    seed_workspace(&store); // ws-inst-test（fixture 权威）
    // 迁移期并存形态：legacy ws-1 是最新（revision 最高），canonical 稳定 instance 已确立。
    insert_capture_with_instance(&store, "ws-inst-canonical", 800);
    insert_capture_with_instance(&store, "ws-1", 900);
    let peer = PeerCredential::new_unix(1000, 1000, 1234);

    let canonical_before = capture_count(&store, "ws-inst-canonical");
    let res = store
        .handle_task_create(
            peer,
            &serde_json::json!({
                "workspace_id": 1,
                "workspace_instance_id": "ws-inst-canonical",
                "task_id": "T-WSA-CANONICAL",
                "title": "live tuple canonical probe",
                "steps": [{"action": "annotate", "target_file": "a.rs"}],
            }),
        )
        .expect("canonical instance 已确立必须被接受，不得因 legacy ws-1 最新被拒");

    assert_eq!(res["task_id"], "T-WSA-CANONICAL");
    let conn = store.conn.lock().unwrap();
    let bindings: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM task_workspace_bindings WHERE task_id = 'T-WSA-CANONICAL'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    drop(conn);
    assert_eq!(bindings, 1, "必须恰好一条不可变 binding");
    assert_eq!(
        capture_count(&store, "ws-inst-canonical"),
        canonical_before + 1,
        "capture 链必须按 canonical instance 追加一条"
    );
}

/// 负向：完全未确立的新 instance（合成形态）→ E_WORKSPACE_AUTHORITY_MISMATCH，
/// 零部分行（task/capture/binding）。
#[test]
fn test_unestablished_new_instance_rejected_no_partial_rows() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path).unwrap();
    seed_workspace(&store);
    insert_capture_with_instance(&store, "ws-inst-canonical", 800);
    insert_capture_with_instance(&store, "ws-1", 900);
    let peer = PeerCredential::new_unix(1000, 1000, 1234);

    let conn = store.conn.lock().unwrap();
    let tasks_before: i64 = conn
        .query_row("SELECT COUNT(*) FROM tasks", [], |r| r.get(0))
        .unwrap();
    let caps_before: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM workspace_authority_captures",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let binds_before: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM task_workspace_bindings",
            [],
            |r| r.get(0),
        )
        .unwrap();
    drop(conn);

    let err = store
        .handle_task_create(
            peer,
            &serde_json::json!({
                "workspace_id": 1,
                "workspace_instance_id": "ws-1-bridge-synthetic-zz",
                "task_id": "T-WSA-SYNTHETIC",
                "title": "synthetic instance probe",
                "steps": [{"action": "annotate", "target_file": "a.rs"}],
            }),
        )
        .unwrap_err();
    assert_eq!(
        err.code, "E_WORKSPACE_AUTHORITY_MISMATCH",
        "未确立 instance 必须 fail-closed，实际 {}",
        err.code
    );

    let conn = store.conn.lock().unwrap();
    assert_eq!(
        conn.query_row("SELECT COUNT(*) FROM tasks", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        tasks_before,
        "不得写 task 行"
    );
    assert_eq!(
        conn.query_row(
            "SELECT COUNT(*) FROM workspace_authority_captures",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        caps_before,
        "不得写 capture 行"
    );
    assert_eq!(
        conn.query_row(
            "SELECT COUNT(*) FROM task_workspace_bindings",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        binds_before,
        "不得写 binding 行"
    );
    drop(conn);
}

/// 只建 task-DB workspace 1 行（root_path=/tmp/test-ws），不 seed 任何 capture。
fn insert_workspace_row_only(store: &TaskCollabStore) {
    let conn = store.conn.lock().unwrap();
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) \
         VALUES (1, 'test-ws', '/tmp/test-ws', ?1, 0)",
        params![1_700_000_000.0_f64],
    )
    .unwrap();
    drop(conn);
}

/// T6 有界再认证正向：capture 链**全为** legacy `ws-<digits>`，且请求的 canonical
/// instance 在 registry 中同 owner + 同归一化 root 且 active → 允许追加 canonical
/// capture（legacy → canonical 收敛），binding 锚定该 canonical instance。
#[test]
fn test_legacy_only_chain_adopts_canonical_instance() {
    let (dir, db_path) = temp_db();
    let registry_path = dir.path().join("registry.db");
    let registry = WorkspaceRegistry::open(registry_path.to_str().unwrap()).unwrap();
    let registered = registry
        .register_workspace(1000, "/tmp/test-ws", "/tmp/test-ws", "", "", "")
        .unwrap();
    let canonical = registered["workspace_instance_id"]
        .as_str()
        .unwrap()
        .to_string();
    assert!(!is_legacy_synthesized_instance(&canonical));

    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_registry_db_path_for_tests(&registry_path);
    insert_workspace_row_only(&store);
    insert_capture_with_instance(&store, "ws-1", 1);
    insert_capture_with_instance(&store, "ws-1", 2);
    let peer = PeerCredential::new_unix(1000, 1000, 1234);

    store
        .handle_task_create(
            peer,
            &serde_json::json!({
                "workspace_id": 1,
                "workspace_instance_id": canonical,
                "task_id": "T-WSA-ADOPT",
                "title": "legacy chain canonical adoption",
                "steps": [{"action": "annotate", "target_file": "a.rs"}],
            }),
        )
        .expect("legacy-only 链 + 同根 registry canonical 行必须允许再认证");

    let conn = store.conn.lock().unwrap();
    let bound_instance: String = conn
        .query_row(
            "SELECT workspace_instance_id FROM workspace_authority_captures \
             WHERE workspace_id = 1 AND workspace_instance_id = ?1 AND capture_revision = 1",
            params![canonical],
            |r| r.get(0),
        )
        .unwrap();
    let supersedes: String = conn
        .query_row(
            "SELECT supersedes_capture_id FROM workspace_authority_captures \
             WHERE workspace_id = 1 AND workspace_instance_id = ?1 AND capture_revision = 1",
            params![canonical],
            |r| r.get(0),
        )
        .unwrap();
    // 新 instance 的 capture 链必须从 revision=1 起（否则链连续性门禁 COUNT==MAX 断链）。
    let canonical_chain: (i64, i64) = conn
        .query_row(
            "SELECT COUNT(*), COALESCE(MAX(capture_revision), 0) \
             FROM workspace_authority_captures \
             WHERE workspace_id = 1 AND workspace_instance_id = ?1",
            params![canonical],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    let binding_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM task_workspace_bindings WHERE task_id = 'T-WSA-ADOPT'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    drop(conn);
    assert_eq!(bound_instance, canonical, "追加 capture 必须是 canonical instance");
    assert_eq!(
        supersedes, "cap-wsa-ws-1-2",
        "canonical capture 必须 supersede legacy 最新一条"
    );
    assert_eq!(canonical_chain, (1, 1), "首个 canonical capture 必须恰好 revision=1");
    assert_eq!(binding_count, 1, "必须恰好一条不可变 binding");

    // 同一 canonical instance 的第二次 create → revision=2（链内单调递增）。
    store
        .handle_task_create(
            peer,
            &serde_json::json!({
                "workspace_id": 1,
                "workspace_instance_id": canonical,
                "task_id": "T-WSA-ADOPT-2",
                "title": "legacy chain canonical adoption second",
                "steps": [{"action": "annotate", "target_file": "b.rs"}],
            }),
        )
        .expect("已确立 canonical instance 的第二次 create 必须允许");
    let conn = store.conn.lock().unwrap();
    let (count2, max2): (i64, i64) = conn
        .query_row(
            "SELECT COUNT(*), COALESCE(MAX(capture_revision), 0) \
             FROM workspace_authority_captures \
             WHERE workspace_id = 1 AND workspace_instance_id = ?1",
            params![canonical],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!((count2, max2), (2, 2), "canonical 链必须连续（count == max）");
}

/// T6 负向：legacy-only 链，但 registry canonical 行的物理根与 task-DB root_path
/// 不一致（跨项目）→ 不得再认证，保持 E_WORKSPACE_AUTHORITY_MISMATCH 且零部分行。
#[test]
fn test_legacy_only_chain_rejects_canonical_from_other_root() {
    let (dir, db_path) = temp_db();
    let registry_path = dir.path().join("registry.db");
    let registry = WorkspaceRegistry::open(registry_path.to_str().unwrap()).unwrap();
    let registered = registry
        .register_workspace(1000, "/tmp/other-project", "/tmp/other-project", "", "", "")
        .unwrap();
    let other = registered["workspace_instance_id"]
        .as_str()
        .unwrap()
        .to_string();

    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_registry_db_path_for_tests(&registry_path);
    insert_workspace_row_only(&store);
    insert_capture_with_instance(&store, "ws-1", 1);

    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    let err = store
        .handle_task_create(
            peer,
            &serde_json::json!({
                "workspace_id": 1,
                "workspace_instance_id": other,
                "task_id": "T-WSA-ADOPT-OTHER-ROOT",
                "title": "cross-root adoption must fail",
                "steps": [{"action": "annotate", "target_file": "a.rs"}],
            }),
        )
        .unwrap_err();
    assert_eq!(
        err.code, "E_WORKSPACE_AUTHORITY_MISMATCH",
        "跨项目 canonical instance 不得被再认证，实际 {}",
        err.code
    );

    let conn = store.conn.lock().unwrap();
    let tasks: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM tasks WHERE id = 'T-WSA-ADOPT-OTHER-ROOT'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let caps: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM workspace_authority_captures WHERE workspace_id = 1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    drop(conn);
    assert_eq!(tasks, 0, "不得写 task 行");
    assert_eq!(caps, 1, "不得追加 capture 行");
}

/// T6 负向：legacy-only 链 + registry 同根但 owner 不是本 peer → 不得再认证。
#[test]
fn test_legacy_only_chain_rejects_canonical_of_other_owner() {
    let (dir, db_path) = temp_db();
    let registry_path = dir.path().join("registry.db");
    let registry = WorkspaceRegistry::open(registry_path.to_str().unwrap()).unwrap();
    let registered = registry
        .register_workspace(2000, "/tmp/test-ws", "/tmp/test-ws", "", "", "")
        .unwrap();
    let foreign = registered["workspace_instance_id"]
        .as_str()
        .unwrap()
        .to_string();

    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_registry_db_path_for_tests(&registry_path);
    insert_workspace_row_only(&store);
    insert_capture_with_instance(&store, "ws-1", 1);

    // peer uid 1000 与 registry owner 2000 不一致：owner ACL 在读事务之外先拒绝。
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    let err = store
        .handle_task_create(
            peer,
            &serde_json::json!({
                "workspace_id": 1,
                "workspace_instance_id": foreign,
                "task_id": "T-WSA-ADOPT-OTHER-OWNER",
                "title": "cross-owner adoption must fail",
                "steps": [{"action": "annotate", "target_file": "a.rs"}],
            }),
        )
        .unwrap_err();
    assert!(
        err.code == "E_WORKSPACE_AUTHORITY_MISMATCH" || err.code == "workspace_forbidden",
        "跨 owner 不得再认证，实际 {}",
        err.code
    );
}

/// A′ 标准三角色 legacy role_contracts 模板（与 cli/main.py `_build_role_contracts`
/// 逐字段一致），触发 task.create 的 governance contract bootstrap。
fn aprime_role_contracts() -> Vec<serde_json::Value> {
    vec![
        serde_json::json!({
            "role": "executor", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.executor.startup.v1",
            "prompt_hash": "59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6",
            "allowed_paths": "task-card scoped paths only",
            "forbidden_paths": "task.apply; task.close; task.supersede",
            "commands": "task.next_action; task.claim; task.report; task.handoff",
            "acceptance_checks": "tests; evidence manifest/hash; executor_ready_for_review",
            "required_evidence": "implementation plan; test output; daemon round-trip evidence",
            "handoff_to": "reviewer", "independence": "required",
        }),
        serde_json::json!({
            "role": "reviewer", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.reviewer.startup.v1",
            "prompt_hash": "6415033D8F134392DE16FCA130BFB762CB6C70D9F466C770EC18A20FC4CE139E",
            "allowed_paths": "read-only review evidence",
            "forbidden_paths": "production edits; task.apply; task.close",
            "commands": "task.next_action; task.contract.get; task.handoff",
            "acceptance_checks": "independent verification of scope, diff, tests, evidence",
            "required_evidence": "review record; reviewer_pass evidence manifest/hash",
            "handoff_to": "adjudicator", "independence": "required",
        }),
        serde_json::json!({
            "role": "adjudicator", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.adjudicator.startup.v1",
            "prompt_hash": "42A5F1DEFA81008B009058C1BAF5D1A14B3EF4521E291B7B55C19BB473A77C3E",
            "allowed_paths": "final review and protected task finalization",
            "forbidden_paths": "production edits; local SQLite fallback; status forgery",
            "commands": "task.next_action; task.apply; task.close; task.handoff",
            "acceptance_checks": "ACCEPT requires valid reviewer lease/fencing then apply, close",
            "required_evidence": "final review; lease/fencing provenance; apply/close verification",
            "handoff_to": "complete", "independence": "required",
        }),
    ]
}

/// 正向（reconcile + bootstrap）：请求的 registry 数字 id（1102）经 instance 对齐
/// reconcile 到 task-DB workspace 1 后，contract bootstrap 也必须写 canonical
/// workspace_id（1），而非请求原值——否则 role_contract_lineages.workspace_id 以
/// 1102 写入会命中 workspaces.id 外键失败（internal_error FK），掩盖本应成功的
/// canonical 绑定。
#[test]
fn test_registry_numeric_id_reconciles_contract_lineage_to_canonical_workspace() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path).unwrap();
    seed_workspace(&store);
    insert_capture_with_instance(&store, "ws-inst-canonical", 800);
    let peer = PeerCredential::new_unix(1000, 1000, 1234);

    let res = store
        .handle_task_create(
            peer,
            &serde_json::json!({
                "workspace_id": 1102,
                "workspace_instance_id": "ws-inst-canonical",
                "task_id": "T-WSA-RECONCILE-BOOTSTRAP",
                "title": "registry numeric id reconcile bootstrap probe",
                "steps": [{"action": "annotate", "target_file": "a.rs"}],
                "role_contracts": aprime_role_contracts(),
                "identity_policy": "legacy_identity_v1",
            }),
        )
        .expect("reconciled canonical workspace 的 contract bootstrap 必须成功，不得 FK 失败");

    assert_eq!(res["workspace_id"], 1, "响应 workspace_id 必须是 canonical task-DB id");
    let conn = store.conn.lock().unwrap();
    // 不可变 binding 锚定 canonical workspace 1。
    let bind_ws: i64 = conn
        .query_row(
            "SELECT workspace_id FROM task_workspace_bindings WHERE task_id = 'T-WSA-RECONCILE-BOOTSTRAP'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    // contract lineage / revision / step-binding 全部锚定 canonical workspace 1。
    let lineage_ws: i64 = conn
        .query_row(
            "SELECT MIN(workspace_id) FROM role_contract_lineages WHERE task_id = 'T-WSA-RECONCILE-BOOTSTRAP'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let lineage_ws_max: i64 = conn
        .query_row(
            "SELECT MAX(workspace_id) FROM role_contract_lineages WHERE task_id = 'T-WSA-RECONCILE-BOOTSTRAP'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let contract_ws: i64 = conn
        .query_row(
            "SELECT workspace_id FROM task_contract_revisions WHERE task_id = 'T-WSA-RECONCILE-BOOTSTRAP' LIMIT 1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let lineage_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM role_contract_lineages WHERE task_id = 'T-WSA-RECONCILE-BOOTSTRAP'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    drop(conn);
    assert_eq!(bind_ws, 1, "binding 必须锚定 canonical workspace 1");
    assert_eq!(lineage_count, 3, "必须派生 executor/reviewer/adjudicator 三条 lineage");
    assert_eq!(lineage_ws, 1, "role_contract_lineages 不得写 registry 数字 id");
    assert_eq!(lineage_ws_max, 1, "所有 lineage 行都必须写 canonical workspace 1");
    assert_eq!(contract_ws, 1, "task_contract_revisions 不得写 registry 数字 id");
}

/// 兼容：legacy ws-1 本身是最新 capture instance → 保持接受（既有行为不回退）。
#[test]
fn test_legacy_latest_instance_still_accepted() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path).unwrap();
    seed_workspace(&store);
    insert_capture_with_instance(&store, "ws-inst-canonical", 800);
    insert_capture_with_instance(&store, "ws-1", 900);
    let peer = PeerCredential::new_unix(1000, 1000, 1234);

    store
        .handle_task_create(
            peer,
            &serde_json::json!({
                "workspace_id": 1,
                "workspace_instance_id": "ws-1",
                "task_id": "T-WSA-LEGACY",
                "title": "legacy latest probe",
                "steps": [{"action": "annotate", "target_file": "a.rs"}],
            }),
        )
        .expect("legacy ws-1（最新 capture instance）必须保持接受");
}

