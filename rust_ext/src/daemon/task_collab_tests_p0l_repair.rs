//! P0-L identity policy repair regression tests.

use super::*;
use super::support::*;

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
                "workspace_id": 1,
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
        "workspace_instance_id": "ws-1",
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
                "workspace_instance_id": "ws-1",
                "request_id": "repair-other",
                "repair_code": "p0l_identity_policy_v1",
                "evidence_path": "evidence.md",
                "evidence_hash": "sha256:evidence"
            }),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_P0L_POLICY_REPAIR_TASK_NOT_ALLOWED");
}
