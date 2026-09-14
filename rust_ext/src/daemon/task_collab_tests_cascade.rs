//! cascade_close 被替代卡豁免回归测试（GOV-FIX-03）。
//!
//! 背景：`task.supersede` 按设计只声明关系、不改 status（见 `task_supersede.rs`），
//! 而被替代的缺合同裸卡无法走 close S2（`E_STEPS_NOT_DONE`），也无法用
//! `task.step.resolve`（只接受 `failed` 步）。故 `task.cascade_close` 对被替代叶子
//! 豁免步骤门禁——其 pending 步骤已由后继卡 scope 承接，属作废遗留。

use super::*;
use super::support::*;

/// 为任务插入一个 `pending` 步骤（`seed_task(with_done_step=false)` 建的是 0 步）。
fn seed_pending_step(store: &TaskCollabStore, task_id: &str) {
    let conn = store.conn.lock().unwrap();
    conn.execute(
        "INSERT INTO task_steps (id, task_id, step_index, action, target_file, target_symbol, check_items, status, result, created_at, completed_at)
         VALUES (?1, ?2, 0, 'port_rust_handler', '', '', '', 'pending', '', ?3, ?3)",
        params![format!("{}-s1", task_id), task_id, 1_700_000_000.0_f64],
    )
    .unwrap();
    drop(conn);
}

/// 直接落一条 supersede 关系（不经过 RPC：本测试只验证 cascade_close 读关系后的行为）。
fn seed_supersede_relation(store: &TaskCollabStore, old: &str, new: &str) {
    let ts = 1_700_000_000.0_f64;
    let conn = store.conn.lock().unwrap();
    conn.execute(
        "INSERT INTO task_supersede_relations
         (superseded_task_id, superseding_task_id, reason, actor, created_at, workspace_id,
          supersedence_id, reason_code, actor_agent_id, actor_session_id, actor_model_id,
          actor_role, request_id, lease_id, fencing_counter, evidence_path, evidence_hash,
          authoritative_timestamp)
         VALUES (?1, ?2, 'test supersede', 'test', ?3, 1, ?4, 'governance_supersede',
                 'test-agent', 'sess-test', 'model-test', 'adjudicator', ?5, 'L-test', 1,
                 'ev.json', 'sha256:test', ?3)",
        params![
            old,
            new,
            ts,
            format!("SUP-test-{old}"),
            format!("req-sup-{old}")
        ],
    )
    .unwrap();
    drop(conn);
}

fn test_peer() -> PeerCredential {
    PeerCredential::new_unix(1000, 1000, 1234)
}

/// 被替代叶子卡（存在 supersede 关系）即使 4 步全 pending 也应被 cascade_close 收尾，
/// 且审计事件 reason 记录后继卡 id。
#[test]
fn test_cascade_close_waives_pending_steps_for_superseded_leaf() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path).unwrap();
    seed_workspace(&store);
    seed_task(&store, "T-OLD-SUP", "", "open", false);
    seed_task(&store, "T-NEW-SUP", "", "closed", false);
    seed_task_binding(&store, "T-OLD-SUP");
    seed_task_binding(&store, "T-NEW-SUP");
    seed_pending_step(&store, "T-OLD-SUP");
    seed_supersede_relation(&store, "T-OLD-SUP", "T-NEW-SUP");

    let params = serde_json::json!({ "task_id": "T-OLD-SUP", "workspace_id": 1 });
    let res = store
        .handle_task_cascade_close(test_peer(), &params)
        .unwrap();
    assert_eq!(res["closed"][0], "T-OLD-SUP");

    let conn = store.conn.lock().unwrap();
    let status: String = conn
        .query_row("SELECT status FROM tasks WHERE id='T-OLD-SUP'", [], |r| r.get(0))
        .unwrap();
    assert_eq!(status, "closed", "被替代叶子必须被收尾");
    let reason: String = conn
        .query_row(
            "SELECT reason FROM task_events \
             WHERE task_id='T-OLD-SUP' AND reason_code='cascade_closed'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(
        reason.contains("superseded by T-NEW-SUP"),
        "审计 reason 必须记录后继卡：{reason}"
    );
}

/// 无 supersede 关系的叶子卡仍受步骤门禁约束（豁免不得扩大化）。
#[test]
fn test_cascade_close_still_blocks_pending_steps_without_supersede() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path).unwrap();
    seed_workspace(&store);
    seed_task(&store, "T-LEAF-PEND", "", "open", false);
    seed_task_binding(&store, "T-LEAF-PEND");
    seed_pending_step(&store, "T-LEAF-PEND");

    let params = serde_json::json!({ "task_id": "T-LEAF-PEND", "workspace_id": 1 });
    let res = store
        .handle_task_cascade_close(test_peer(), &params)
        .unwrap();
    assert_eq!(
        res["closed"].as_array().unwrap().len(),
        0,
        "未 supersede 的 pending 叶子不得被收尾"
    );
    // GOV-FIX-06：拒绝语义——目标卡未收尾时必须显式携带 blocked。
    assert_eq!(res["target_closed"], false, "目标卡未收尾必须回 target_closed=false");
    assert_eq!(res["blocked"]["task_id"], "T-LEAF-PEND");
    assert_eq!(res["blocked"]["reason"], "leaf_steps_pending");

    let conn = store.conn.lock().unwrap();
    let status: String = conn
        .query_row("SELECT status FROM tasks WHERE id='T-LEAF-PEND'", [], |r| r.get(0))
        .unwrap();
    assert_eq!(status, "open");
}

/// GOV-FIX-06：聚合节点存在未 closed 子卡时，cascade_close 必须以
/// `blocked(children_not_closed)` 显式拒绝且 status 不变（假成功回归）。
#[test]
fn test_cascade_close_reports_blocked_for_open_children() {
    let (_dir, db_path) = temp_db();
    let store = TaskCollabStore::new(&db_path).unwrap();
    seed_workspace(&store);
    seed_task(&store, "T-EPIC-ROOT", "", "open", false);
    seed_task(&store, "T-CHILD-OPEN", "T-EPIC-ROOT", "open", false);
    seed_task(&store, "T-CHILD-DONE", "T-EPIC-ROOT", "closed", false);
    seed_task_binding(&store, "T-EPIC-ROOT");

    let params = serde_json::json!({ "task_id": "T-EPIC-ROOT", "workspace_id": 1 });
    let res = store
        .handle_task_cascade_close(test_peer(), &params)
        .unwrap();
    assert_eq!(
        res["closed"].as_array().unwrap().len(),
        0,
        "存在 open 子卡时聚合节点不得被收尾"
    );
    assert_eq!(res["target_closed"], false);
    assert_eq!(res["blocked"]["task_id"], "T-EPIC-ROOT");
    assert_eq!(res["blocked"]["reason"], "children_not_closed");
    assert_eq!(res["blocked"]["open_children"], 1);

    let conn = store.conn.lock().unwrap();
    let status: String = conn
        .query_row("SELECT status FROM tasks WHERE id='T-EPIC-ROOT'", [], |r| r.get(0))
        .unwrap();
    assert_eq!(status, "open", "根卡 status 必须保持 open（不得假成功）");
}
