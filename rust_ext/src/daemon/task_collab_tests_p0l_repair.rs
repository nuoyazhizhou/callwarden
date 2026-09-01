//! P0-L identity policy repair regression tests.

use super::*;
use super::support::*;

fn seed_unresolved_p0l_task(store: &TaskCollabStore, peer: &PeerCredential) {
    let task_id = "T-1787801315246-e3e3a08c";
    seed_workspace(store);
    store
        .handle_task_create(
            peer.clone(),
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-test",
                "task_id": task_id,
                "title": "P0-L bootstrap fixture",
                "identity_policy": "legacy_identity_v1",
                "steps": [{"action": "fix_defect", "target_file": "p0l.rs"}],
                "task_contract_envelope": {
                    "contract_id": "TC-T-1787801315246-e3e3a08c",
                    "revision": 1,
                    "profile": "code_change",
                    "objective": {"statement": "repair the P0-L identity policy"},
                    "interfaces": {"rpc": "task.p0l_identity_policy_bootstrap_repair"},
                    "allowed_edit_scope": {"files": ["p0l.rs"]},
                    "acceptance_clauses": [],
                    "risks": [{"kind": "governance deadlock"}],
                    "rollback": {"strategy": "append-only revision"},
                    "dependencies": [],
                    "source": {"kind": "test fixture"},
                    "identity_policy": "legacy_identity_v1"
                },
                "role_contracts": [
                    {"role": "executor", "independence": "{}"},
                    {"role": "reviewer", "independence": "{}"},
                    {"role": "adjudicator", "independence": "{}"}
                ]
            }),
        )
        .unwrap();
    let conn = store.conn.lock().unwrap();
    let payload: String = conn
        .query_row(
            "SELECT envelope_payload FROM task_contract_revisions WHERE task_id=?1 ORDER BY revision DESC LIMIT 1",
            [task_id],
            |row| row.get(0),
        )
        .unwrap();
    let mut envelope: serde_json::Value = serde_json::from_str(&payload).unwrap();
    envelope.as_object_mut().unwrap().remove("identity_policy");
    conn.execute(
        "UPDATE task_contract_revisions SET envelope_payload=?1 WHERE task_id=?2",
        rusqlite::params![serde_json::to_string(&envelope).unwrap(), task_id],
    )
    .unwrap();
}

#[test]
fn p0l_identity_policy_repair_is_allowlisted_atomic_and_idempotent() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    let task_id = "T-1787801315246-e3e3a08c";
    seed_workspace(&store);
    store
        .handle_task_create(
            peer.clone(),
            &serde_json::json!({
                "workspace_id": 1, "workspace_instance_id": "ws-inst-test",
                "task_id": task_id,
                "title": "P0-L repair fixture",
                "identity_policy": "legacy_identity_v1",
                "steps": [{"action": "fix_defect", "target_file": "p0l.rs"}],
                "task_contract_envelope": {
                    "contract_id": "TC-T-1787801315246-e3e3a08c",
                    "revision": 1,
                    "profile": "code_change",
                    "objective": {"statement": "repair the P0-L identity policy"},
                    "interfaces": {"rpc": "task.p0l_identity_policy_repair"},
                    "allowed_edit_scope": {"files": ["p0l.rs"]},
                    "acceptance_clauses": [],
                    "risks": [{"kind": "governance deadlock"}],
                    "rollback": {"strategy": "append-only revision"},
                    "dependencies": [],
                    "source": {"kind": "test fixture"},
                    "identity_policy": "legacy_identity_v1"
                },
                "role_contracts": [
                    {"role": "executor", "independence": "{}"},
                    {"role": "reviewer", "independence": "{}"},
                    {"role": "adjudicator", "independence": "{}"}
                ]
            }),
        )
        .unwrap();
    // 模拟真实 P0-L 的历史 generic revision：保留 revision/hash 链，只移除 policy 字段。
    {
        let conn = store.conn.lock().unwrap();
        let payload: String = conn
            .query_row(
                "SELECT envelope_payload FROM task_contract_revisions WHERE task_id=?1 ORDER BY revision DESC LIMIT 1",
                [task_id],
                |row| row.get(0),
            )
            .unwrap();
        let mut envelope: serde_json::Value = serde_json::from_str(&payload).unwrap();
        envelope.as_object_mut().unwrap().remove("identity_policy");
        conn.execute(
            "UPDATE task_contract_revisions SET envelope_payload=?1 WHERE task_id=?2",
            rusqlite::params![serde_json::to_string(&envelope).unwrap(), task_id],
        )
        .unwrap();
    }
    let credential = p0l_enroll_worker(
        &store,
        &peer.owner_key(),
        "p0l-repair-adj",
        "p0l-repair-adj-inst",
        "adjudicator",
    );
    let mut request = serde_json::json!({
        "task_id": task_id,
        "workspace_id": 1,
        "workspace_instance_id": "ws-inst-test",
        "request_id": "p0l-policy-repair-1",
        "repair_code": "p0l_identity_policy_v1",
        "evidence_path": "deliverables/p0l-policy-repair.md",
        "evidence_hash": "sha256:p0l-policy-repair",
        "role_worker_auth": p0l_role_worker_auth(
            "p0l-repair-adj",
            "p0l-repair-adj-inst",
            "p0l-repair-session",
            &credential,
        ),
    });
    let repaired = store
        .handle_p0l_identity_policy_repair(peer.clone(), &request)
        .unwrap();
    assert_eq!(repaired["policy"], "role_worker_v1");
    assert_eq!(repaired["revision"], 2);
    assert_eq!(repaired["replayed"], false);

    let replayed = store
        .handle_p0l_identity_policy_repair(peer.clone(), &request)
        .unwrap();
    assert_eq!(replayed["revision"], 2);
    assert_eq!(replayed["replayed"], true);

    request["request_id"] = serde_json::json!("p0l-policy-repair-2");
    let err = store
        .handle_p0l_identity_policy_repair(peer, &request)
        .unwrap_err();
    assert_eq!(err.code, "E_P0L_POLICY_REPAIR_ALREADY_RESOLVED");

    let conn = store.conn.lock().unwrap();
    let revision_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM task_contract_revisions WHERE task_id=?1",
            [task_id],
            |row| row.get(0),
        )
        .unwrap();
    let repair_event_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM task_events WHERE task_id=?1 AND reason_code='p0l_identity_policy_repaired'",
            [task_id],
            |row| row.get(0),
        )
        .unwrap();
    let ledger_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM task_operation_ledger WHERE method='task.p0l_identity_policy_repair'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(revision_count, 2);
    assert_eq!(repair_event_count, 1);
    assert_eq!(ledger_count, 2);
}

#[test]
fn p0l_identity_policy_repair_rejects_non_allowlisted_task() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path).unwrap();
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    let err = store
        .handle_p0l_identity_policy_repair(
            peer,
            &serde_json::json!({
                "task_id": "T-NOT-P0L",
                "workspace_id": 1,
                "workspace_instance_id": "ws-inst-test",
                "request_id": "repair-other",
                "repair_code": "p0l_identity_policy_v1",
                "evidence_path": "evidence.md",
                "evidence_hash": "sha256:evidence"
            }),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_P0L_POLICY_REPAIR_TASK_NOT_ALLOWED");
}

#[test]
fn p0l_identity_policy_bootstrap_repair_is_owner_scoped_one_shot_and_secret_free() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path).unwrap();
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_unresolved_p0l_task(&store, &peer);
    p0l_enroll_worker(
        &store,
        &peer.owner_key(),
        "p0l-bootstrap-adj",
        "p0l-bootstrap-adj-inst",
        "adjudicator",
    );
    let request = serde_json::json!({
        "task_id": "T-1787801315246-e3e3a08c",
        "workspace_id": 1,
        "workspace_instance_id": "ws-inst-test",
        "request_id": "p0l-bootstrap-1",
        "repair_code": "p0l_identity_policy_v1",
        "role_worker_id": "p0l-bootstrap-adj",
        "role_instance_id": "p0l-bootstrap-adj-inst",
        "role_session_id": "p0l-bootstrap-session"
    });

    let repaired = store
        .handle_p0l_identity_policy_bootstrap_repair(peer.clone(), &request)
        .unwrap();
    assert_eq!(repaired["policy"], "role_worker_v1");
    assert_eq!(repaired["revision"], 2);
    assert_eq!(repaired["replayed"], false);

    let replayed = store
        .handle_p0l_identity_policy_bootstrap_repair(peer.clone(), &request)
        .unwrap();
    assert_eq!(replayed["revision"], 2);
    assert_eq!(replayed["replayed"], true);

    let unauthorized_replay = store
        .handle_p0l_identity_policy_bootstrap_repair(
            PeerCredential::new_unix(1001, 1001, 5678),
            &request,
        )
        .unwrap_err();
    assert_eq!(unauthorized_replay.code, "E_P0L_BOOTSTRAP_ADJUDICATOR_REQUIRED");

    let mut conflicting_request = request.clone();
    conflicting_request["role_session_id"] = serde_json::json!("different-session");
    let request_id_conflict = store
        .handle_p0l_identity_policy_bootstrap_repair(peer.clone(), &conflicting_request)
        .unwrap_err();
    assert_eq!(request_id_conflict.code, "E_REQUEST_ID_REUSE_MISMATCH");

    let conn = store.conn.lock().unwrap();
    let revision_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM task_contract_revisions WHERE task_id='T-1787801315246-e3e3a08c'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let policy: String = conn
        .query_row(
            "SELECT envelope_payload FROM task_contract_revisions WHERE task_id='T-1787801315246-e3e3a08c' ORDER BY revision DESC LIMIT 1",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(revision_count, 2);
    assert!(policy.contains("role_worker_v1"));
    let event_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM task_events WHERE task_id='T-1787801315246-e3e3a08c' AND reason_code='p0l_identity_policy_repaired'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let ledger_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM task_operation_ledger WHERE method='task.p0l_identity_policy_bootstrap_repair'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let provenance: String = conn
        .query_row(
            "SELECT runtime_payload_json FROM role_runtime_provenance WHERE action_type='task.p0l_identity_policy_bootstrap_repair'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(event_count, 1);
    assert_eq!(ledger_count, 1);
    assert!(!provenance.contains("credential"));
}

#[test]
fn p0l_identity_policy_bootstrap_repair_rejects_wrong_worker_and_secret_fields() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path).unwrap();
    let owner = PeerCredential::new_unix(1000, 1000, 1234);
    let other = PeerCredential::new_unix(1001, 1001, 5678);
    seed_unresolved_p0l_task(&store, &owner);
    p0l_enroll_worker(
        &store,
        &owner.owner_key(),
        "p0l-bootstrap-exec",
        "p0l-bootstrap-exec-inst",
        "executor",
    );
    let request = serde_json::json!({
        "task_id": "T-1787801315246-e3e3a08c",
        "workspace_id": 1,
        "workspace_instance_id": "ws-inst-test",
        "request_id": "p0l-bootstrap-invalid-1",
        "repair_code": "p0l_identity_policy_v1",
        "role_worker_id": "p0l-bootstrap-exec",
        "role_instance_id": "p0l-bootstrap-exec-inst",
        "role_session_id": "p0l-bootstrap-session"
    });
    let err = store
        .handle_p0l_identity_policy_bootstrap_repair(other, &request)
        .unwrap_err();
    assert_eq!(err.code, "E_P0L_BOOTSTRAP_ADJUDICATOR_REQUIRED");

    let mut secret_request = request;
    secret_request["credential"] = serde_json::json!("must-not-be-accepted");
    let err = store
        .handle_p0l_identity_policy_bootstrap_repair(owner, &secret_request)
        .unwrap_err();
    assert_eq!(err.code, "E_P0L_BOOTSTRAP_PARAM_NOT_ALLOWED");
    let conn = store.conn.lock().unwrap();
    let revisions: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM task_contract_revisions WHERE task_id='T-1787801315246-e3e3a08c'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(revisions, 1);
}

#[test]
fn role_worker_rotate_is_owner_authorized_one_time_and_secret_free_on_replay() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path).unwrap();
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_workspace(&store);
    let old_credential = p0l_enroll_worker(
        &store,
        &peer.owner_key(),
        "rw-recover",
        "rwi-old",
        "adjudicator",
    );
    let request = serde_json::json!({
        "request_id": "role-worker-recovery-1",
        "workspace_id": 1,
        "workspace_instance_id": "ws-inst-test",
        "role_worker_id": "rw-recover",
        "new_role_instance_id": "rwi-new",
        "role_session_id": "recovery-session",
        "rotation_mode": "owner_recovery",
        "reason_code": "lost_credential",
        "runtime": {"provider": "local", "model_id": "test-model"}
    });

    let first = store
        .handle_role_worker_rotate(peer.clone(), &request)
        .unwrap();
    let new_credential = first["credential"].as_str().unwrap();
    assert_eq!(new_credential.len(), 64);
    assert_ne!(new_credential, old_credential);
    assert_eq!(first["credential_delivery"], "response_once");
    assert_eq!(first["replayed"], false);

    let replay = store
        .handle_role_worker_rotate(peer.clone(), &request)
        .unwrap();
    assert_eq!(replay["replayed"], true);
    assert!(replay.get("credential").is_none());
    assert_eq!(replay["role_instance_id"], "rwi-new");

    let unauthorized_replay = store
        .handle_role_worker_rotate(PeerCredential::new_unix(1001, 1001, 5678), &request)
        .unwrap_err();
    assert_eq!(
        unauthorized_replay.code,
        crate::daemon::task_loop::role_worker::ERR_CREDENTIAL_INVALID
    );

    let mut extra_field_request = request.clone();
    extra_field_request["credential"] = serde_json::json!("must-not-be-accepted");
    let extra_field = store
        .handle_role_worker_rotate(peer.clone(), &extra_field_request)
        .unwrap_err();
    assert_eq!(
        extra_field.code,
        crate::daemon::task_loop::role_worker::ERR_ROTATE_PARAM_NOT_ALLOWED
    );

    let mut conflicting_request = request.clone();
    conflicting_request["new_role_instance_id"] = serde_json::json!("rwi-conflict");
    conflicting_request["reason_code"] = serde_json::json!("different-reason");
    let request_id_conflict = store
        .handle_role_worker_rotate(peer.clone(), &conflicting_request)
        .unwrap_err();
    assert_eq!(request_id_conflict.code, "E_REQUEST_ID_REUSE_MISMATCH");

    let conn = store.conn.lock().unwrap();
    let stored_hash: String = conn
        .query_row(
            "SELECT credential_hash FROM role_workers WHERE role_worker_id='rw-recover'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(stored_hash, sha256_hex(new_credential.as_bytes()));
    let old_status: String = conn
        .query_row(
            "SELECT status FROM role_worker_instances WHERE role_instance_id='rwi-old'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(old_status, "retired");
    let new_status: String = conn
        .query_row(
            "SELECT status FROM role_worker_instances WHERE role_instance_id='rwi-new'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(new_status, "active");
    let provenance: String = conn
        .query_row(
            "SELECT runtime_payload_json FROM role_runtime_provenance WHERE action_type='role_worker.rotate'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert!(!provenance.contains(new_credential));
    assert!(!provenance.contains(&stored_hash));
    let ledger: String = conn
        .query_row(
            "SELECT response_or_error_json FROM task_operation_ledger WHERE method='role_worker.rotate'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert!(!ledger.contains(new_credential));
    assert!(!ledger.contains(&stored_hash));
}

#[test]
fn role_worker_rotate_rejects_non_owner_without_mutation() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path).unwrap();
    let owner = PeerCredential::new_unix(1000, 1000, 1234);
    let other = PeerCredential::new_unix(1001, 1001, 5678);
    seed_workspace(&store);
    p0l_enroll_worker(&store, &owner.owner_key(), "rw-owner", "rwi-owner", "adjudicator");
    let request = serde_json::json!({
        "request_id": "role-worker-recovery-wrong-owner",
        "workspace_id": 1,
        "workspace_instance_id": "ws-inst-test",
        "role_worker_id": "rw-owner",
        "new_role_instance_id": "rwi-attacker",
        "role_session_id": "attacker-session",
        "rotation_mode": "owner_recovery"
    });
    let err = store
        .handle_role_worker_rotate(other, &request)
        .unwrap_err();
    assert_eq!(err.code, crate::daemon::task_loop::role_worker::ERR_CREDENTIAL_INVALID);
    let conn = store.conn.lock().unwrap();
    let instance_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM role_worker_instances WHERE role_worker_id='rw-owner' AND status='active'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(instance_count, 1);
}

#[test]
fn role_worker_rotate_rejects_runtime_secret_without_mutation() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path).unwrap();
    let owner = PeerCredential::new_unix(1000, 1000, 1234);
    seed_workspace(&store);
    p0l_enroll_worker(&store, &owner.owner_key(), "rw-runtime", "rwi-runtime", "adjudicator");
    let request = serde_json::json!({
        "request_id": "role-worker-recovery-runtime-secret",
        "workspace_id": 1,
        "workspace_instance_id": "ws-inst-test",
        "role_worker_id": "rw-runtime",
        "new_role_instance_id": "rwi-runtime-new",
        "role_session_id": "runtime-secret-session",
        "rotation_mode": "owner_recovery",
        "runtime": {"token": "must-not-enter-daemon"}
    });

    let err = store
        .handle_role_worker_rotate(owner, &request)
        .unwrap_err();
    assert_eq!(err.code, crate::daemon::task_loop::role_worker::ERR_RUNTIME_SECRET);
    let conn = store.conn.lock().unwrap();
    let active_instance: String = conn
        .query_row(
            "SELECT role_instance_id FROM role_worker_instances WHERE role_worker_id='rw-runtime' AND status='active'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(active_instance, "rwi-runtime");
}
