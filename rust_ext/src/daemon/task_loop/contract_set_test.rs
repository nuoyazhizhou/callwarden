//! 1B `contract_set.rs` 领域测试（cw-role-handoff-task-loop.md §8.1.2 / §4.3）。
//!
//! 覆盖：`role-contract-c14n/v1` hash 确定性/键排序/NFC/路径拒绝/列表重复拒绝、
//! lineage+revision 同事务写入与 ledger 联动、revision 链连续性校验、
//! 确定性拒绝（savepoint 选择性回滚 + 可重放 ledger error）、
//! 基础设施错误回滚整个 outer transaction、幂等重放。

use rusqlite::Connection;

use crate::sqlite_query::migrate_connection;
use super::contract_set::{
    canonical_contract_hash, set_task_contract, ContractPayload, LedgerKey, SetContractInput,
    ERR_TASK_BINDING_REQUIRED, ERR_TASK_CONTRACT_INVALID, ROLE_CONTRACT_C14N_VERSION,
};
use super::create::{create_task, CreateTaskInput, LedgerKey as CreateLedgerKey, WorkspaceCaptureInput};
use super::types::FrozenAuthorityInput;

/// 开启内存 task-DB 并跑一遍 migration（v55：含 role_contract rule row）。
fn fresh_db() -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    migrate_connection(&conn).expect("migration to v55");
    conn.execute(
        "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (?1, ?2, ?3, 0.0)",
        rusqlite::params![1, "ws-1", "/tmp/ws-1"],
    )
    .unwrap();
    conn
}

fn frozen() -> FrozenAuthorityInput {
    FrozenAuthorityInput::default()
}

/// 复用 1A `create_task` 建立 task + 不可变 workspace binding（依赖链 1A → 1B）。
fn setup_task(conn: &mut Connection, task_id: &str) {
    let ws = WorkspaceCaptureInput {
        workspace_id: 1,
        daemon_workspace_id: 42,
        workspace_instance_id: "ws-inst-1".to_string(),
        client_view_root_hash: "client-view-hash".to_string(),
        host_real_root_hash: "host-root-hash".to_string(),
        workspace_manifest_payload_json: "{\"kind\":\"a\"}".to_string(),
        workspace_manifest_hash: "manifest-a".to_string(),
        created_by: "test-creator".to_string(),
    };
    let input = CreateTaskInput {
        task_id: task_id.to_string(),
        title: format!("task-{task_id}"),
        description: "desc".to_string(),
        creator: "test-creator".to_string(),
    };
    create_task(
        conn,
        &frozen(),
        &CreateLedgerKey {
            workspace_instance_id: "ws-inst-1".to_string(),
            method: "task.create".to_string(),
            request_id: format!("create-{task_id}"),
        },
        &input,
        &ws,
    )
    .expect("setup create_task 应成功");
}

fn contract(role: &str) -> ContractPayload {
    ContractPayload {
        role: role.to_string(),
        skill_id: "skill-1".to_string(),
        skill_version: "1.0".to_string(),
        prompt_template_id: "pt-1".to_string(),
        prompt_hash: "ph-1".to_string(),
        allowed_paths: vec!["src/".to_string(), "docs/".to_string()],
        forbidden_paths: vec!["target/".to_string()],
        commands: vec!["echo".to_string()],
        acceptance_checks: vec!["pass".to_string()],
        required_evidence: vec!["log".to_string()],
        handoff_to: "human".to_string(),
        independence: serde_json::json!({"max_tokens": 100}),
    }
}

fn input(task_id: &str, contract: ContractPayload) -> SetContractInput {
    SetContractInput {
        task_id: task_id.to_string(),
        contract,
        created_by: "test-owner".to_string(),
    }
}

fn key(request_id: &str) -> LedgerKey {
    LedgerKey {
        workspace_instance_id: "ws-inst-1".to_string(),
        method: "task.contract_set".to_string(),
        request_id: request_id.to_string(),
    }
}

fn count(conn: &Connection, table: &str) -> i64 {
    conn.query_row(
        &format!("SELECT COUNT(*) FROM {table}"),
        [],
        |row| row.get(0),
    )
    .unwrap()
}

#[test]
fn contract_hash_is_deterministic_and_order_sensitive() {
    let c1 = contract("coder");
    let h1 = canonical_contract_hash(&c1).unwrap();
    let h2 = canonical_contract_hash(&c1).unwrap();
    assert_eq!(h1, h2, "同输入必须给出相同 hash");
    assert!(h1.starts_with("sha256:"), "hash 前缀应为 sha256:");

    // 路径集合去重排序：输入顺序不影响 hash（规范化排序）。
    let mut shuffled = contract("coder");
    shuffled.allowed_paths = vec!["docs/".to_string(), "src/".to_string()];
    assert_eq!(
        canonical_contract_hash(&shuffled).unwrap(),
        h1,
        "路径集合是无序集合，输入顺序不得改变 hash"
    );

    // command 列表保序：顺序变化必须改变 hash。
    let mut reordered = contract("coder");
    reordered.commands = vec!["echo".to_string(), "extra".to_string()];
    assert_ne!(canonical_contract_hash(&reordered).unwrap(), h1);

    // NFC：组合/分解形式等价。
    let composed = ContractPayload { role: "café".to_string(), ..contract("coder") };
    let decomposed = ContractPayload { role: "cafe\u{301}".to_string(), ..contract("coder") };
    assert_eq!(
        canonical_contract_hash(&composed).unwrap(),
        canonical_contract_hash(&decomposed).unwrap(),
        "NFC 规范化后组合/分解形式必须同 hash"
    );
}

#[test]
fn contract_hash_rejects_invalid_paths_and_duplicate_lists() {
    // 绝对路径拒绝。
    let mut abs = contract("coder");
    abs.allowed_paths = vec!["/etc".to_string()];
    assert!(canonical_contract_hash(&abs).unwrap_err().contains("绝对路径"));
    // `..` 拒绝。
    let mut parent = contract("coder");
    parent.forbidden_paths = vec!["a/../b".to_string()];
    assert!(canonical_contract_hash(&parent).unwrap_err().contains(".."));
    // 反斜杠拒绝。
    let mut backslash = contract("coder");
    backslash.allowed_paths = vec!["src\\lib".to_string()];
    assert!(canonical_contract_hash(&backslash).unwrap_err().contains("正斜杠"));
    // command 列表重复即拒绝。
    let mut dup = contract("coder");
    dup.commands = vec!["echo".to_string(), "echo".to_string()];
    assert!(canonical_contract_hash(&dup).unwrap_err().contains("重复"));
    // acceptance_checks 重复同样拒绝。
    let mut dup_check = contract("coder");
    dup_check.acceptance_checks = vec!["pass".to_string(), "pass".to_string()];
    assert!(canonical_contract_hash(&dup_check).unwrap_err().contains("重复"));
    // independence 非 object 拒绝。
    let mut bad_ind = contract("coder");
    bad_ind.independence = serde_json::json!([1, 2, 3]);
    assert!(canonical_contract_hash(&bad_ind).unwrap_err().contains("object"));
}

#[test]
fn from_params_rejects_missing_or_malformed_fields() {
    // 缺 task_id
    let err = SetContractInput::from_params(&serde_json::json!({})).unwrap_err();
    assert_eq!(err.code, "invalid_params");
    // 缺 contract
    let err =
        SetContractInput::from_params(&serde_json::json!({"task_id": "t-1"})).unwrap_err();
    assert_eq!(err.code, "invalid_params");
    // contract.role 空
    let err = SetContractInput::from_params(&serde_json::json!({
        "task_id": "t-1", "contract": {"role": "  "}
    }))
    .unwrap_err();
    assert_eq!(err.code, "invalid_params");
    // allowed_paths 不是数组
    let err = SetContractInput::from_params(&serde_json::json!({
        "task_id": "t-1", "contract": {"role": "coder", "allowed_paths": "src/"}
    }))
    .unwrap_err();
    assert_eq!(err.code, "invalid_params");
    // independence 非 object
    let err = SetContractInput::from_params(&serde_json::json!({
        "task_id": "t-1", "contract": {"role": "coder", "independence": "nope"}
    }))
    .unwrap_err();
    assert_eq!(err.code, "invalid_params");
    // 可选字段缺省
    let parsed = SetContractInput::from_params(&serde_json::json!({
        "task_id": "t-1", "contract": {"role": "coder"}
    }))
    .unwrap();
    assert_eq!(parsed.contract.allowed_paths, Vec::<String>::new());
    assert_eq!(parsed.contract.independence, serde_json::json!({}));
    assert!(parsed.contract.skill_id.is_empty());
}

#[test]
fn first_contract_creates_lineage_and_revision_1() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");

    let resp = set_task_contract(&mut conn, &frozen(), &key("req-1"), &input("t-1", contract("coder")))
        .expect("首次 contract_set 应成功");
    assert_eq!(resp["ok"].as_bool(), Some(true));
    assert_eq!(resp["role_contract_lineage_id"], "rcl-t-1-coder");
    assert_eq!(resp["role_contract_revision_id"], "rcr-t-1-coder-r1");
    assert_eq!(resp["revision"], 1);
    assert!(resp["role_contract_hash"].as_str().unwrap().starts_with("sha256:"));

    // lineage + revision 与 ledger 在同一事务提交。
    assert_eq!(count(&conn, "role_contract_lineages"), 1);
    assert_eq!(count(&conn, "role_contract_revisions"), 1);
    assert_eq!(count(&conn, "task_operation_ledger"), 2); // create + contract_set

    // revision 1：supersedes NULL；provenance 四元组完整。
    let (revision, supersedes, version, rules_hash): (i64, Option<String>, String, String) = conn
        .query_row(
            "SELECT revision, supersedes_revision_id, canonicalization_version, canonicalization_rules_hash \
             FROM role_contract_revisions WHERE role_contract_revision_id = 'rcr-t-1-coder-r1'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .unwrap();
    assert_eq!(revision, 1);
    assert!(supersedes.is_none(), "revision 1 的 supersedes 必须为 NULL");
    assert_eq!(version, ROLE_CONTRACT_C14N_VERSION);
    assert!(!rules_hash.is_empty());

    // lineage 归属精确到 workspace binding。
    let (task_id, workspace_id, role): (String, i64, String) = conn
        .query_row(
            "SELECT task_id, workspace_id, role FROM role_contract_lineages \
             WHERE role_contract_lineage_id = 'rcl-t-1-coder'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    assert_eq!((task_id.as_str(), workspace_id, role.as_str()), ("t-1", 1, "coder"));

    // 持久化 hash 与独立计算一致。
    let stored_hash: String = conn
        .query_row(
            "SELECT role_contract_hash FROM role_contract_revisions \
             WHERE role_contract_revision_id = 'rcr-t-1-coder-r1'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(stored_hash, canonical_contract_hash(&contract("coder")).unwrap());
}

#[test]
fn second_contract_appends_revision_2_superseding_1() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    set_task_contract(&mut conn, &frozen(), &key("req-1"), &input("t-1", contract("coder")))
        .expect("first ok");

    let mut v2 = contract("coder");
    v2.commands = vec!["echo".to_string(), "fmt".to_string()];
    let resp2 = set_task_contract(&mut conn, &frozen(), &key("req-2"), &input("t-1", v2))
        .expect("追加 revision 2 应成功");

    assert_eq!(resp2["revision"], 2);
    assert_eq!(resp2["role_contract_revision_id"], "rcr-t-1-coder-r2");
    // n>1 必须指向同 lineage 的 n-1。
    let supersedes: String = conn
        .query_row(
            "SELECT supersedes_revision_id FROM role_contract_revisions \
             WHERE role_contract_revision_id = 'rcr-t-1-coder-r2'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(supersedes, "rcr-t-1-coder-r1");
    // 同一 lineage 两条 revision，链保持连续。
    assert_eq!(count(&conn, "role_contract_lineages"), 1);
    assert_eq!(count(&conn, "role_contract_revisions"), 2);
}

#[test]
fn set_contract_rejects_task_without_binding() {
    let mut conn = fresh_db();
    // t-no 从未 create（无 task_workspace_bindings 行）。
    let err = set_task_contract(
        &mut conn,
        &frozen(),
        &key("req-no"),
        &input("t-no", contract("coder")),
    )
    .expect_err("无 binding 的 task 必须确定性拒绝");
    assert_eq!(err.code, ERR_TASK_BINDING_REQUIRED);
    assert_eq!(count(&conn, "role_contract_lineages"), 0);
    assert_eq!(count(&conn, "role_contract_revisions"), 0);
    // 拒绝也落可重放 ledger error。
    let stored: String = conn
        .query_row(
            "SELECT response_or_error_json FROM task_operation_ledger WHERE request_id = 'req-no'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let stored_json: serde_json::Value = serde_json::from_str(&stored).unwrap();
    assert_eq!(stored_json["ok"].as_bool(), Some(false));
    assert_eq!(stored_json["code"], ERR_TASK_BINDING_REQUIRED);
}

#[test]
fn invalid_payload_fails_closed_and_rolls_back_domain_writes() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");

    let mut bad = contract("coder");
    bad.allowed_paths = vec!["/etc".to_string()];
    let err = set_task_contract(&mut conn, &frozen(), &key("req-bad"), &input("t-1", bad))
        .expect_err("绝对路径必须确定性拒绝");
    assert_eq!(err.code, ERR_TASK_CONTRACT_INVALID);

    // savepoint 选择性回滚：领域局部写入不落库，但 ledger error 已提交。
    assert_eq!(count(&conn, "role_contract_lineages"), 0, "被拒绝的 lineage 不得写入");
    assert_eq!(count(&conn, "role_contract_revisions"), 0, "被拒绝的 revision 不得写入");
    let stored: String = conn
        .query_row(
            "SELECT response_or_error_json FROM task_operation_ledger WHERE request_id = 'req-bad'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let stored_json: serde_json::Value = serde_json::from_str(&stored).unwrap();
    assert_eq!(stored_json["ok"].as_bool(), Some(false));
    assert_eq!(stored_json["code"], ERR_TASK_CONTRACT_INVALID);
}

#[test]
fn broken_revision_chain_refuses_append() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    set_task_contract(&mut conn, &frozen(), &key("req-1"), &input("t-1", contract("coder")))
        .expect("first ok");

    // 人为制造断链：跳过 revision 2 直接插入 revision 3（指向 r1）。
    conn.execute(
        "INSERT INTO role_contract_revisions \
         (role_contract_revision_id, role_contract_lineage_id, revision, \
          supersedes_revision_id, canonical_payload_json, canonicalization_version, \
          canonicalization_rules_hash, role_contract_hash, created_by, \
          authoritative_created_at) \
         VALUES ('rcr-t-1-coder-r3', 'rcl-t-1-coder', 3, 'rcr-t-1-coder-r1', \
                 '{}', 'role-contract-c14n/v1', 'rules-hash', 'sha256:0', 'test', 't')",
        [],
    )
    .unwrap();

    let err = set_task_contract(&mut conn, &frozen(), &key("req-2"), &input("t-1", contract("coder")))
        .expect_err("断链 lineage 必须拒绝追加（UNVERIFIED）");
    assert_eq!(err.code, ERR_TASK_CONTRACT_INVALID);
    // 未追加新 revision（仍只有人为插入的 2 条）。
    assert_eq!(count(&conn, "role_contract_revisions"), 2);
}

#[test]
fn replay_of_committed_key_does_not_dup() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    let k = key("req-replay");
    let first = set_task_contract(&mut conn, &frozen(), &k, &input("t-1", contract("coder")))
        .expect("first ok");
    let second = set_task_contract(&mut conn, &frozen(), &k, &input("t-1", contract("coder")))
        .expect("replay ok");
    assert_eq!(first, second, "幂等重放必须原样返回已保存的结果");
    assert_eq!(count(&conn, "role_contract_lineages"), 1);
    assert_eq!(count(&conn, "role_contract_revisions"), 1);
}

#[test]
fn infrastructure_error_rolls_back_everything() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    setup_task(&mut conn, "t-2");
    // 预置 lineage 主键冲突：t2 占用 'rcl-t-1-coder'（FK (t2,1) 满足 binding）。
    conn.execute(
        "INSERT INTO role_contract_lineages \
         (role_contract_lineage_id, task_id, workspace_id, role, created_by, authoritative_created_at) \
         VALUES ('rcl-t-1-coder', 't-2', 1, 'coder', 'test', 't')",
        [],
    )
    .unwrap();

    let err = set_task_contract(&mut conn, &frozen(), &key("req-infra"), &input("t-1", contract("coder")))
        .expect_err("lineage 主键冲突必须归类为基础设施错误");
    assert_eq!(err.code, "E_TASK_DB_TRANSACTION");

    // 整个 outer transaction 回滚：revision / ledger result 一并消失（lineage 仅剩预置行）。
    assert_eq!(count(&conn, "role_contract_lineages"), 1, "回滚后只保留预置 lineage 行");
    assert_eq!(count(&conn, "role_contract_revisions"), 0, "回滚后不得遗留 revision");
    let ledger_for_infra: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM task_operation_ledger WHERE request_id = 'req-infra'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(ledger_for_infra, 0, "回滚后不得遗留 ledger result");
}

#[test]
fn different_roles_get_distinct_lineages() {
    let mut conn = fresh_db();
    setup_task(&mut conn, "t-1");
    set_task_contract(&mut conn, &frozen(), &key("req-1"), &input("t-1", contract("coder")))
        .expect("coder ok");
    set_task_contract(&mut conn, &frozen(), &key("req-2"), &input("t-1", contract("reviewer")))
        .expect("reviewer ok");
    assert_eq!(count(&conn, "role_contract_lineages"), 2);
    assert_eq!(count(&conn, "role_contract_revisions"), 2);
}
