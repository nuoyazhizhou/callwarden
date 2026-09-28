//! task_collab S4 verdict 门禁测试（T-1790563271814-14566fa4）。
//! 「独立复审 = 关闭门禁」：review 态 apply/close 必须有 pass verdict 入账或显式豁免。

use super::*;
use super::support::*;

#[test]
fn test_task_apply_from_review_requires_verdict_or_waiver() {
    // 无 verdict 无豁免 → E_VERDICT_REQUIRED fail-closed（状态不变、无事件）；
    // 空 reason 豁免视同未豁免；显式豁免 → 放行且事件落账、响应带 verdict_waived。
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_task(&store, "T-VG", "", "review", true);
    seed_reviewer_lease(&store, "T-VG", "tok-vg", 1, "agent-r", "sess-r", "model-r");

    // 1) 无 verdict 无豁免 → 拒绝
    let err = store
        .handle_task_apply(
            peer.clone(),
            &serde_json::json!({"task_id": "T-VG", "lease_token": "tok-vg", "fencing_counter": 1}),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_VERDICT_REQUIRED");
    {
        let conn = store.conn.lock().unwrap();
        let status: String = conn
            .query_row("SELECT status FROM tasks WHERE id = 'T-VG'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(status, "review", "拒绝后状态不得变化");
        let waiver_events: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events \
                 WHERE task_id = 'T-VG' AND reason_code = 'verdict_waiver'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(waiver_events, 0, "拒绝路径不得写豁免事件");
    }

    // 2) 空 reason 的豁免视同未豁免（不得静默放行）
    let err = store
        .handle_task_apply(
            peer.clone(),
            &serde_json::json!({
                "task_id": "T-VG", "lease_token": "tok-vg", "fencing_counter": 1,
                "verdict_waiver": {"reason": "   "},
            }),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_VERDICT_REQUIRED");

    // 3) 显式豁免 → 放行 + 事件落账 + 响应标记
    let res = store
        .handle_task_apply(
            peer,
            &serde_json::json!({
                "task_id": "T-VG", "lease_token": "tok-vg", "fencing_counter": 1,
                "verdict_waiver": {"reason": "unit-test: waiver audit path"},
            }),
        )
        .unwrap();
    assert_eq!(res["status"], "applied");
    assert_eq!(res["verdict_waived"], true);
    {
        let conn = store.conn.lock().unwrap();
        let waiver_events: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events \
                 WHERE task_id = 'T-VG' AND reason_code = 'verdict_waiver' \
                   AND reason LIKE '%waiver audit path%'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(waiver_events, 1, "豁免必须写入可审计事件");
    }
}

#[test]
fn test_task_apply_with_pass_verdict_skips_gate() {
    // 存在 overall='pass' verdict 入账 → 无需豁免直接放行（正常 A′ 环路径）。
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_task(&store, "T-VG-PASS", "", "review", true);
    seed_reviewer_lease(&store, "T-VG-PASS", "tok-vgp", 1, "agent-r", "sess-r", "model-r");
    {
        let conn = store.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO task_verdict_events
             (verdict_id, task_id, contract_id, contract_revision, contract_hash,
              phase, reviewer_identity, clause_results, findings, overall, attestation, submitted_at)
             VALUES ('V-VG-PASS', 'T-VG-PASS', 'TC-VG', 1, 'sha256:tc',
                     'blind_first_pass', '{}', '[]', '[]', 'pass', 'attested', 1700000000.0)",
            [],
        )
        .unwrap();
    }

    let res = store
        .handle_task_apply(
            peer,
            &serde_json::json!({"task_id": "T-VG-PASS", "lease_token": "tok-vgp", "fencing_counter": 1}),
        )
        .unwrap();
    assert_eq!(res["status"], "applied");
    assert!(
        res.get("verdict_waived").is_none(),
        "verdict 入账路径不得带豁免标记"
    );
}

#[test]
fn test_task_close_from_review_requires_verdict_or_waiver() {
    // review 态直接 close（绕过 apply）同样被 S4 门禁拦截；block verdict 不放行。
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_task(&store, "T-VG-C", "", "review", true);
    seed_reviewer_lease(&store, "T-VG-C", "tok-vgc", 1, "agent-r", "sess-r", "model-r");
    {
        let conn = store.conn.lock().unwrap();
        // block verdict 不构成「独立复审通过」
        conn.execute(
            "INSERT INTO task_verdict_events
             (verdict_id, task_id, contract_id, contract_revision, contract_hash,
              phase, reviewer_identity, clause_results, findings, overall, attestation, submitted_at)
             VALUES ('V-VG-C-BLOCK', 'T-VG-C', 'TC-VG-C', 1, 'sha256:tc',
                     'blind_first_pass', '{}', '[]', '[]', 'block', 'attested', 1700000000.0)",
            [],
        )
        .unwrap();
    }

    let err = store
        .handle_task_close(
            peer.clone(),
            &serde_json::json!({"task_id": "T-VG-C", "lease_token": "tok-vgc", "fencing_counter": 1}),
        )
        .unwrap_err();
    assert_eq!(err.code, "E_VERDICT_REQUIRED", "block verdict 不得放行 close");

    // 显式豁免后才允许 review -> closed 跳变
    let res = store
        .handle_task_close(
            peer,
            &serde_json::json!({
                "task_id": "T-VG-C", "lease_token": "tok-vgc", "fencing_counter": 1,
                "verdict_waiver": {"reason": "unit-test: review direct close waiver"},
            }),
        )
        .unwrap();
    assert_eq!(res["status"], "closed");
    assert_eq!(res["verdict_waived"], true);
}

#[test]
fn test_task_close_from_applied_skips_verdict_gate() {
    // applied -> closed 已在 apply 阶段把过关，close 不重复拦截（既有主路径回归）。
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path)
        .unwrap()
        .with_clock(Arc::new(AuthoritativeClock::new()));
    let peer = PeerCredential::new_unix(1000, 1000, 1234);
    seed_task(&store, "T-VG-APP", "", "applied", true);
    seed_reviewer_lease(&store, "T-VG-APP", "tok-vga", 1, "agent-r", "sess-r", "model-r");

    let res = store
        .handle_task_close(
            peer,
            &serde_json::json!({"task_id": "T-VG-APP", "lease_token": "tok-vga", "fencing_counter": 1}),
        )
        .unwrap();
    assert_eq!(res["status"], "closed");
    assert!(res.get("verdict_waived").is_none());
}
