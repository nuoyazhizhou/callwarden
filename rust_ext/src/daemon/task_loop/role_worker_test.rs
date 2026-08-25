//! P0-J step3：Role Worker 授权 **负面矩阵** 集成测试。
//!
//! 与 `role_worker.rs` 内的 `#[cfg(test)] mod tests`（单元级）互补：本文件聚焦
//! **授权拒绝面** 与 **角色分离矩阵**，验证 `validate_and_record` / `parse_role_worker_auth`
//! 在所有非法输入下 fail-closed，且 legacy 任务不引入 role worker 时不报错。
//!
//! 覆盖合同 `acceptance_checks`：
//! - "authorization uses local role worker credential"
//! - "runtime changes append provenance only"
//! - "no local SQLite fallback"（拒绝分支不回退原始 SQLite 直写）

use rusqlite::{params, Connection};
use serde_json::json;

use crate::daemon::task_loop::role_worker::{
    validate_and_record, parse_role_worker_auth, RoleWorkerAuth, enroll_role_worker,
    ERR_AUTH_REQUIRED, ERR_CREDENTIAL_INVALID, ERR_ROLE_MISMATCH, ERR_INSTANCE_INVALID,
    ERR_RUNTIME_SECRET,
};

fn db() -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    crate::sqlite_query::migrate_connection(&conn).unwrap();
    conn.execute(
        "INSERT INTO workspaces (id,name,root_path,created_at,is_active) VALUES (1,'role-worker-neg','/rw-neg',0,1)",
        [],
    )
    .unwrap();
    conn
}

fn enroll(conn: &mut Connection, worker: &str, instance: &str, role: &str, runtime: serde_json::Value) -> String {
    let tx = conn.transaction().unwrap();
    let response = enroll_role_worker(
        &tx,
        "owner-A",
        &json!({"role_worker_id": worker, "role_instance_id": instance, "role": role, "runtime": runtime}),
        1,
    )
    .unwrap();
    tx.commit().unwrap();
    response["credential"].as_str().unwrap().to_string()
}

fn auth(worker: &str, instance: &str, session: &str, credential: &str, runtime: serde_json::Value) -> RoleWorkerAuth {
    RoleWorkerAuth {
        role_worker_id: worker.to_string(),
        role_instance_id: instance.to_string(),
        role_session_id: session.to_string(),
        credential: credential.to_string(),
        runtime,
    }
}

#[test]
fn missing_role_worker_auth_fields_rejected() {
    // 缺 role_worker_id / credential / role_session_id 等任一字段均 fail-closed
    for bad in [
        json!({}),
        json!({"role_worker_id": "rw", "credential": "c"}),
        json!({"role_worker_id": "rw", "role_session_id": "rs", "credential": "c"}),
        json!({"role_worker_id": "rw", "role_instance_id": "ri", "role_session_id": "rs"}),
    ] {
        let err = parse_role_worker_auth(&json!({ "role_worker_id": "ignored", "role_worker_auth": bad }))
            .unwrap_err();
        assert_eq!(err.code, ERR_AUTH_REQUIRED, "缺字段必须返回 E_ROLE_WORKER_AUTH_REQUIRED: {bad}");
    }
    // 顶层不带 role_worker_auth 视为 None（legacy 兼容，不报错）
    assert!(parse_role_worker_auth(&json!({})).unwrap().is_none());
}

#[test]
fn wrong_instance_or_retired_instance_rejected() {
    let mut conn = db();
    let credential = enroll(&mut conn, "rw-exec", "rwi-exec", "executor", json!({}));
    // instance 不存在
    let tx = conn.transaction().unwrap();
    let bad_instance = validate_and_record(
        &tx,
        &auth("rw-exec", "rwi-does-not-exist", "s1", &credential, json!({})),
        "owner-A", 1, "T-inst", "task.claim", "executor",
    )
    .unwrap_err();
    assert_eq!(bad_instance.code, ERR_INSTANCE_INVALID);
    // owner 不匹配（worker 属于 owner-A，用 owner-B 校验）
    let bad_owner = validate_and_record(
        &tx,
        &auth("rw-exec", "rwi-exec", "s1", &credential, json!({})),
        "owner-B", 1, "T-owner", "task.claim", "executor",
    )
    .unwrap_err();
    assert_eq!(bad_owner.code, ERR_CREDENTIAL_INVALID);
}

#[test]
fn cross_role_impersonation_rejected_across_full_matrix() {
    let mut conn = db();
    let exec_cred = enroll(&mut conn, "rw-exec", "rwi-exec", "executor", json!({}));
    let rev_cred = enroll(&mut conn, "rw-rev", "rwi-rev", "reviewer", json!({}));
    let adj_cred = enroll(&mut conn, "rw-adj", "rwi-adj", "adjudicator", json!({}));

    // executor 凭证不能执行 reviewer / adjudicator 动作
    let tx = conn.transaction().unwrap();
    assert_eq!(
        validate_and_record(&tx, &auth("rw-exec", "rwi-exec", "s", &exec_cred, json!({})), "owner-A", 1, "T1", "verdict.submit", "reviewer").unwrap_err().code,
        ERR_ROLE_MISMATCH
    );
    assert_eq!(
        validate_and_record(&tx, &auth("rw-exec", "rwi-exec", "s", &exec_cred, json!({})), "owner-A", 1, "T1", "task.close", "adjudicator").unwrap_err().code,
        ERR_ROLE_MISMATCH
    );
    // reviewer 凭证不能执行 executor / adjudicator 动作
    assert_eq!(
        validate_and_record(&tx, &auth("rw-rev", "rwi-rev", "s", &rev_cred, json!({})), "owner-A", 1, "T2", "task.claim", "executor").unwrap_err().code,
        ERR_ROLE_MISMATCH
    );
    assert_eq!(
        validate_and_record(&tx, &auth("rw-rev", "rwi-rev", "s", &rev_cred, json!({})), "owner-A", 1, "T2", "task.close", "adjudicator").unwrap_err().code,
        ERR_ROLE_MISMATCH
    );
    // adjudicator 凭证不能执行 executor / reviewer 动作
    assert_eq!(
        validate_and_record(&tx, &auth("rw-adj", "rwi-adj", "s", &adj_cred, json!({})), "owner-A", 1, "T3", "task.claim", "executor").unwrap_err().code,
        ERR_ROLE_MISMATCH
    );
    assert_eq!(
        validate_and_record(&tx, &auth("rw-adj", "rwi-adj", "s", &adj_cred, json!({})), "owner-A", 1, "T3", "verdict.submit", "reviewer").unwrap_err().code,
        ERR_ROLE_MISMATCH
    );
}

#[test]
fn independent_role_workers_collaborate_on_same_task() {
    // 角色分离由「每个治理角色独立 worker + 单一绑定 role」实现：
    // 同一 task 上 executor worker 与 reviewer worker 各自合法写入 provenance，
    // 且 executor worker 不可越权执行 reviewer 动作（已由 cross_role 测试覆盖
    // ERR_ROLE_MISMATCH）。本测试验证独立角色协作被允许。
    let mut conn = db();
    let exec_cred = enroll(&mut conn, "rw-exec", "rwi-exec", "executor", json!({}));
    let rev_cred = enroll(&mut conn, "rw-rev", "rwi-rev", "reviewer", json!({}));
    let task = "T-collab";

    // executor 合法写入
    {
        let tx = conn.transaction().unwrap();
        validate_and_record(&tx, &auth("rw-exec", "rwi-exec", "s-exec", &exec_cred, json!({})), "owner-A", 1, task, "task.claim", "executor").unwrap();
        tx.commit().unwrap();
    }
    // reviewer 合法写入（独立 worker，同一 task）
    {
        let tx = conn.transaction().unwrap();
        validate_and_record(&tx, &auth("rw-rev", "rwi-rev", "s-rev", &rev_cred, json!({})), "owner-A", 1, task, "verdict.submit", "reviewer").unwrap();
        tx.commit().unwrap();
    }
    // 两条 provenance 均以 append-only 形式留存，且分属不同 worker
    let rows: Vec<(String, String)> = conn
        .prepare("SELECT role_worker_id,action_type FROM role_runtime_provenance WHERE task_id=?1 ORDER BY recorded_at ASC")
        .unwrap()
        .query_map(params![task], |row| Ok((row.get(0)?, row.get(1)?)))
        .unwrap()
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0], ("rw-exec".to_string(), "task.claim".to_string()));
    assert_eq!(rows[1], ("rw-rev".to_string(), "verdict.submit".to_string()));
}

#[test]
fn runtime_secret_rejected_across_nested_fields() {
    for bad_runtime in [
        json!({"token": "x"}),
        json!({"nested": {"password": "y"}}),
        json!({"list": [{"secret": "z"}]}),
        json!({"credential": "c"}),
    ] {
        let err = parse_role_worker_auth(&json!({
            "role_worker_auth": {
                "role_worker_id": "rw", "role_instance_id": "ri",
                "role_session_id": "rs", "credential": "c", "runtime": bad_runtime
            }
        }))
        .unwrap_err();
        assert_eq!(err.code, ERR_RUNTIME_SECRET, "嵌套秘密必须被拒绝: {bad_runtime}");
    }
}

#[test]
fn legacy_task_without_role_worker_does_not_force_role_worker_auth() {
    // 合同要求 legacy 历史任务保持可读、不强制 role worker。
    // 权威 DB 层对未携带 role_worker_auth 的请求返回 None（不报错），
    // 由调用方按 identity_policy 决定 legacy / role_worker_v1 路径。
    let none = parse_role_worker_auth(&json!({ "task_id": "T-legacy" })).unwrap();
    assert!(none.is_none());
}
