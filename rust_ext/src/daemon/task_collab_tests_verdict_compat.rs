//! T-1788665370580-83851bf0：verdict.submit pre-snapshot 时代有界兼容回归。
//!
//! 语义：
//! - 任务全事件历史**零非空 snapshot_id**（pre-snapshot 时代存量任务）→ 允许空
//!   snapshot_id 提交 verdict（缺席可审计，verdict 行 snapshot_id 为空）；
//! - 任务历史存在任何非空 snapshot → 空 snapshot 拒绝（invalid_params，fail-closed）；
//!   带一致非空 snapshot 的正常路径行为不变。

use super::support::*;
use super::*;

/// 复用 T-VERDICT-NATIVE 的治理装置：建任务（含 independent_reviewer 合同）、
/// 推入 review、补合同修订、注册 reviewer、取 reviewer lease。
fn seed_review_ready_task(store: &TaskCollabStore, peer: &PeerCredential, task_id: &str) -> (String, String, i64, String, serde_json::Value) {
    seed_workspace(store);
    store
        .handle_task_create(
            peer.clone(),
            &serde_json::json!({ "workspace_id": 1, "workspace_instance_id": "ws-inst-test",
                "task_id": task_id,
                "title": "pre-snapshot compat task",
                "steps": [{"action": "review", "target_file": "a.rs"}],
                "identity_policy": "legacy_identity_v1",
                "role_contracts": [{
                    "role": "independent_reviewer",
                    "skill_id": "none",
                    "skill_version": "v1",
                    "prompt_template_id": "reviewer-v1",
                    "prompt_hash": "sha256:prompt",
                    "allowed_paths": [],
                    "forbidden_paths": ["a.rs"],
                    "commands": ["cargo test"],
                    "acceptance_checks": ["focused tests pass"],
                    "required_evidence": ["test_log"],
                    "handoff_to": "adjudicator",
                    "independence": {"different_session_from": ["implementer"]}
                },
                {"role": "executor", "independence": "{}"},
                {"role": "reviewer", "independence": "{}"},
                {"role": "adjudicator", "independence": "{}"}]
            }),
        )
        .unwrap();

    let (step_id, role_contract_id, role_contract_revision, role_contract_hash) = {
        let conn = store.conn.lock().unwrap();
        conn.execute(
            &format!("UPDATE tasks SET status = 'review' WHERE id = '{}'", task_id),
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO task_contract_revisions
                 (contract_id, revision, contract_hash, profile, task_id, workspace_id,
                  envelope_payload, created_at, created_by)
                 VALUES ('TC-NOSNAP', 1, 'sha256:task-contract', 'review', ?1, 1, '{}', 1.0, 'test')",
            params![task_id],
        )
        .unwrap();
        let step_id: String = conn
            .query_row(
                "SELECT id FROM task_steps WHERE task_id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .unwrap();
        let row: (
                String,
                i64,
                String,
                String,
                String,
                String,
                String,
                String,
                String,
                String,
                String,
                String,
                String,
                String,
                String,
            ) = conn
            .query_row(
                "SELECT contract_id, revision, role, step_id, skill_id, skill_version,
                        prompt_template_id, prompt_hash, allowed_paths, forbidden_paths,
                        commands, acceptance_checks, required_evidence, handoff_to, independence
                 FROM role_contracts
                 WHERE task_id = ?1 AND role = 'independent_reviewer' AND is_current = 1",
                params![task_id],
                |r| {
                    Ok((
                        r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?,
                        r.get(5)?, r.get(6)?, r.get(7)?, r.get(8)?, r.get(9)?,
                        r.get(10)?, r.get(11)?, r.get(12)?, r.get(13)?, r.get(14)?,
                    ))
                },
            )
            .unwrap();
        let payload = serde_json::json!({
            "canonicalization_version": "role-contract-c14n/v1",
            "contract_id": row.0,
            "revision": row.1,
            "task_id": task_id,
            "role": row.2,
            "step_id": row.3,
            "skill_id": row.4,
            "skill_version": row.5,
            "prompt_template_id": row.6,
            "prompt_hash": row.7,
            "allowed_paths": row.8,
            "forbidden_paths": row.9,
            "commands": row.10,
            "acceptance_checks": row.11,
            "required_evidence": row.12,
            "handoff_to": row.13,
            "independence": row.14,
        });
        (
            step_id,
            payload["contract_id"].as_str().unwrap().to_string(),
            payload["revision"].as_i64().unwrap(),
            format!("sha256:{}", sha256_hex(payload.to_string().as_bytes())),
        )
    };

    register_agent_with_identity(
        store,
        peer,
        &format!("{}-rev", task_id),
        "reviewer-instance",
        "reviewer-session",
        "independent_reviewer",
    );
    let identity = lease_identity(
        &format!("{}-rev", task_id),
        "reviewer-session",
        "claude-test",
        "independent_reviewer",
    );
    let lease = store
        .handle_lease_acquire(
            peer.clone(),
            &serde_json::json!({
                "task_id": task_id,
                "role": "reviewer",
                "identity": identity,
            }),
        )
        .unwrap();
    (step_id, role_contract_id, role_contract_revision, role_contract_hash, lease)
}

fn verdict_params(task_id: &str, step_id: &str, rc_id: &str, rc_rev: i64, rc_hash: &str, lease: &serde_json::Value, snapshot_id: Value) -> serde_json::Value {
    serde_json::json!({
        "task_id": task_id,
        "step_id": step_id,
        "verdict_id": format!("V-{}", task_id),
        "contract_id": "TC-NOSNAP",
        "contract_revision": 1,
        "contract_hash": "sha256:task-contract",
        "role_contract_id": rc_id,
        "role_contract_revision": rc_rev,
        "role_contract_hash": rc_hash,
        "phase": "blind_first_pass",
        "view_manifest_hash": "sha256:view",
        "snapshot_id": snapshot_id,
        "clause_results": [{"clause_id": "C1", "decision": "pass"}],
        "findings": [],
        "overall": "pass",
        "attestation": "reviewed independently",
        "request_id": format!("req-{}", task_id),
        "identity": lease_identity(
            &format!("{}-rev", task_id),
            "reviewer-session",
            "claude-test",
            "independent_reviewer",
        ),
        "lease_token": lease["token"],
        "fencing_counter": lease["fencing_counter"],
    })
}

/// 正向：pre-snapshot 时代任务（全历史零非空 snapshot）→ 空 snapshot_id verdict 放行，
/// 落库 snapshot_id 为空（缺席可审计）。
#[test]
fn verdict_submit_pre_snapshot_era_accepts_empty_snapshot() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    let (step_id, rc_id, rc_rev, rc_hash, lease) =
        seed_review_ready_task(&store, &peer, "T-VERDICT-NOSNAP");

    let params = verdict_params(
        "T-VERDICT-NOSNAP",
        &step_id,
        &rc_id,
        rc_rev,
        &rc_hash,
        &lease,
        Value::String("".into()),
    );
    let res = store
        .handle_verdict_submit(peer.clone(), &params)
        .expect("pre-snapshot 时代任务必须允许空 snapshot verdict");
    assert!(res.is_object(), "submit 必须成功返回响应对象");

    let conn = store.conn.lock().unwrap();
    let (stored_snapshot, count): (String, i64) = conn
        .query_row(
            "SELECT snapshot_id, COUNT(*) FROM task_verdict_events \
             WHERE task_id = 'T-VERDICT-NOSNAP'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    drop(conn);
    assert_eq!(count, 1);
    assert_eq!(stored_snapshot, "", "缺席必须可审计（落库为空，非合成值）");
}

/// 负向：任务历史存在非空 snapshot → 空 snapshot 拒绝（fail-closed）；
/// 同一任务带一致非空 snapshot 的 verdict 正常路径不受影响。
#[test]
fn verdict_submit_rejects_empty_snapshot_when_history_has_snapshot() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    let (step_id, rc_id, rc_rev, rc_hash, lease) =
        seed_review_ready_task(&store, &peer, "T-VERDICT-WITHSNAP");

    // 模拟 snapshot 时代任务：事件历史携带非空 snapshot。
    {
        let conn = store.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO task_events (task_id, from_status, to_status, reason_code, \
              authoritative_timestamp, snapshot_id, agent_session_id, request_id, \
              actor_identity, monotonic_seq) \
             VALUES ('T-VERDICT-WITHSNAP', 'in_progress', 'review', 'reported', \
              1.0, 'snap-x', 'test', 'req-snap-x', 'test', 9001)",
            [],
        )
        .unwrap();
        drop(conn);
    }

    let empty_snap = verdict_params(
        "T-VERDICT-WITHSNAP",
        &step_id,
        &rc_id,
        rc_rev,
        &rc_hash,
        &lease,
        Value::String("".into()),
    );
    let err = store
        .handle_verdict_submit(peer.clone(), &empty_snap)
        .expect_err("历史携带 snapshot 的任务拒绝空 snapshot verdict");
    assert_eq!(err.code, "invalid_params");
    assert!(err.message.contains("snapshot_id"));

    // fail-closed 拒绝后零落库。
    let conn = store.conn.lock().unwrap();
    let rejected: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM task_verdict_events WHERE task_id = 'T-VERDICT-WITHSNAP'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    drop(conn);
    assert_eq!(rejected, 0);

    // 正常路径：一致非空 snapshot 仍通过。
    let with_snap = verdict_params(
        "T-VERDICT-WITHSNAP",
        &step_id,
        &rc_id,
        rc_rev,
        &rc_hash,
        &lease,
        Value::String("snap-x".into()),
    );
    let mut with_snap = with_snap;
    with_snap["request_id"] = Value::String("req-withsnap-2".into());
    let res = store
        .handle_verdict_submit(peer, &with_snap)
        .expect("一致非空 snapshot 的正常路径不受影响");
    assert!(res.is_object(), "submit 必须成功返回响应对象");
    let conn = store.conn.lock().unwrap();
    let (applied, stored): (i64, String) = conn
        .query_row(
            "SELECT COUNT(*), MAX(snapshot_id) FROM task_verdict_events \
             WHERE task_id = 'T-VERDICT-WITHSNAP'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    drop(conn);
    assert_eq!(applied, 1);
    assert_eq!(stored, "snap-x");
}
