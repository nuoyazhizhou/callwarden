//! task_collab projection、policy 和 role-worker 测试。

use super::*;
use super::support::*;
    #[test]
    fn test_task_list_requires_workspace_id_fail_closed() {
        // 缺陷2回归：task.list 缺显式 workspace_id → E_TASK_WORKSPACE_UNBOUND，
        // 绝不回退全表 WHERE 1=1。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let err = store
            .handle_task_list(peer, &serde_json::json!({"status": "", "limit": 20}))
            .unwrap_err();
        assert_eq!(err.code, "E_TASK_WORKSPACE_UNBOUND");
    }

    #[test]
    fn test_task_list_includes_daemon_governance_projection() {
        // 列表必须与 status/status_tree 暴露同一治理字段；缺合同的历史任务
        // 不能静默按 raw status 猜测，而应明确标记为 governance_blocked。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        seed_workspace(&store);
        seed_task_binding(&store, "T-LIST-PROJECTION");
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let result = store
            .handle_task_list(
                peer,
                &serde_json::json!({
                    "workspace_id": 1,
                    "parent_id": "",
                    "limit": 100
                }),
            )
            .unwrap();
        let task = result["tasks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|value| value["task_id"] == "T-LIST-PROJECTION")
            .expect("列表必须返回绑定到 workspace 的任务");
        assert_eq!(task["lifecycle_status"], "open");
        assert_eq!(task["workflow_status"], "governance_blocked");
        assert_eq!(task["governance"]["workflow_status"], "governance_blocked");
        assert!(task["blocking_reasons"].as_array().unwrap().len() > 0);
    }

    #[test]
    fn test_task_status_includes_steps_and_normalized_progress() {
        // flat status 是 CLI/MCP 的详情来源，必须保留步骤事实并提供明确单位。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": "T-STATUS-PROJECTION",
                    "title": "status projection",
                    "steps": [{"action": "verify", "target_file": "tests/status.py"}]
                }),
            )
            .unwrap();
        let first = store
            .handle_task_status(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-STATUS-PROJECTION"
                }),
            )
            .unwrap();
        assert_eq!(first["steps"].as_array().unwrap().len(), 1);
        assert_eq!(first["progress"]["total"], 1);
        assert_eq!(first["progress"]["done"], 0);
        assert_eq!(first["progress"]["percent"], 0.0);

        let conn = store.conn.lock().unwrap();
        conn.execute(
            "UPDATE task_steps SET status='done', completed_at=1.0
             WHERE task_id='T-STATUS-PROJECTION'",
            [],
        )
        .unwrap();
        drop(conn);
        let completed = store
            .handle_task_status(
                peer,
                &serde_json::json!({
                    "task_id": "T-STATUS-PROJECTION"
                }),
            )
            .unwrap();
        assert_eq!(completed["progress"]["ratio"], 1.0);
        assert_eq!(completed["progress"]["percent"], 100.0);
    }

    #[test]
    fn test_task_create_requires_workspace_id_fail_closed() {
        // 任务接口强制绑定：task.create 缺显式 workspace_id → fail-closed。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let err = store
            .handle_task_create(peer, &serde_json::json!({"title": "no workspace"}))
            .unwrap_err();
        assert_eq!(err.code, "E_TASK_WORKSPACE_UNBOUND");
    }

    #[test]
    fn test_lease_requires_binding_fail_closed() {
        // 缺陷3回归：lease 操作有 task_id 但无 task_workspace_bindings →
        // E_TASK_WORKSPACE_UNBOUND，禁止 active workspace / 任意 workspace 补齐。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let err = store
            .handle_lease_acquire(
                peer,
                &serde_json::json!({
                    "task_id": "T-UNBOUND-LEASE",
                    "role": "implementer",
                    "ttl_seconds": 3600.0,
                    "identity": lease_identity("agent-x", "session-x", "model-x", "implementer"),
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_TASK_WORKSPACE_UNBOUND");
    }

    #[test]
    fn test_lease_workspace_mismatch_fail_closed() {
        // 显式 workspace_id 与 binding 不一致 → E_WORKSPACE_AUTHORITY_MISMATCH。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store); // T-LEASE-1 已绑定 workspace 1
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let err = store
            .handle_lease_acquire(
                peer,
                &serde_json::json!({
                    "task_id": "T-LEASE-1",
                    "role": "implementer",
                    "workspace_id": 99,
                    "ttl_seconds": 3600.0,
                    "identity": lease_identity("agent-x", "session-x", "model-x", "implementer"),
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_WORKSPACE_AUTHORITY_MISMATCH");
    }

    #[test]
    fn test_active_workspace_id_fail_closed_without_active() {
        // 无 active workspace 时推导必须失败（E_IDENTITY_NOT_WIRED），
        // 绝不回退到“任意 workspace”。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let conn = store.conn.lock().unwrap();
        let err = active_workspace_id(&conn).unwrap_err();
        assert_eq!(err.code, "E_IDENTITY_NOT_WIRED");
    }
    #[test]
    fn test_task_create_writes_workspace_binding() {
        // 正向契约：task.create 在同一事务写入不可变 task_workspace_bindings，
        // 且 binding.workspace_id 与显式传入的 workspace_id 一致。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        seed_workspace(&store); // workspace id=1（is_active=1）
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let res = store
            .handle_task_create(
                peer,
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": "T-BIND-001",
                    "title": "bound task",
                }),
            )
            .unwrap();
        assert_eq!(res["workspace_id"], 1);
        assert!(res["workspace_binding_id"].as_str().is_some());
        let conn = store.conn.lock().unwrap();
        let bound_ws: i64 = conn
            .query_row(
                "SELECT workspace_id FROM task_workspace_bindings WHERE task_id = 'T-BIND-001'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(bound_ws, 1);
        let capture_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM workspace_authority_captures WHERE workspace_id = 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(
            capture_count >= 1,
            "task.create 必须写入 workspace authority capture"
        );
    }

    #[test]
    fn test_handle_detect_cycle_native_parity() {
        // MCP-007：detect_cycle Rust native 与 Python db_task_dependencies.detect_cycle
        // 语义一致：workspace 内 is_hard=1 边构成环时返回 has_cycle=true + 最短 cycle path；
        // 无边 workspace 返回 has_cycle=false。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);

        // 构造硬依赖环：A -> B -> C -> A（workspace_id=1, is_hard=1）
        {
            let conn = store.conn.lock().unwrap();
            conn.execute_batch(
                "INSERT INTO dependency_edges \
                 (workspace_id, provider_task_id, consumer_task_id, edge_type, source_type, contract_id, contract_revision, is_hard, created_at) VALUES \
                 (1, 'A', 'B', 'hard_dep', 'task', 'C-1', 1, 1, 1.0), \
                 (1, 'B', 'C', 'hard_dep', 'task', 'C-1', 1, 1, 1.0), \
                 (1, 'C', 'A', 'hard_dep', 'task', 'C-1', 1, 1, 1.0);",
            )
            .unwrap();
        }

        let r = store
            .handle_detect_cycle(peer.clone(), &serde_json::json!({"workspace_id": 1}))
            .unwrap();
        assert_eq!(r["has_cycle"], serde_json::Value::Bool(true));
        let path = r["cycle_path"].as_array().expect("cycle_path 应为数组");
        assert!(!path.is_empty(), "有环时 cycle_path 非空");
        // 最短环应为 4 节点（A->B->C->A 含首尾）
        assert_eq!(path.len(), 4, "最短 cycle path 应为 A,B,C,A（4 节点）");
        assert_eq!(path[0], serde_json::json!("A"));
        assert_eq!(path[path.len() - 1], serde_json::json!("A"));
        assert_eq!(r["checked_nodes"], serde_json::json!(3));

        // 另一 workspace 无边 → 无环
        let empty = store
            .handle_detect_cycle(peer, &serde_json::json!({"workspace_id": 2}))
            .unwrap();
        assert_eq!(empty["has_cycle"], serde_json::Value::Bool(false));
        assert_eq!(empty["cycle_path"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn test_handle_validate_revision_dependencies_native_parity() {
        // MCP-008：validate_revision_dependencies Rust native 与 Python
        // tools_p2_graph._h_validate_revision_dependencies 语义一致：
        // 空依赖 → valid=true；requires_artifact 缺 target → resolution error；
        // 环合并检测（现有硬边 + 模拟边）。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);

        // 空依赖 → valid=true, edges_built=0
        let empty = store
            .handle_validate_revision_dependencies(
                peer.clone(),
                &serde_json::json!({"workspace_id": 1, "contract_id": "C-NONE", "contract_revision": 1}),
            )
            .unwrap();
        assert_eq!(empty["valid"], serde_json::Value::Bool(true));
        assert_eq!(empty["edges_built"], serde_json::json!(0));

        // requires_artifact 缺 target_task_id → resolution error
        {
            let conn = store.conn.lock().unwrap();
            conn.execute_batch(
                "INSERT INTO task_dependencies \
                 (workspace_id, task_id, dependency_type, target_ref, target_task_id, \
                  contract_id, contract_revision, is_informational, declared_at) VALUES \
                 (1, 'T-CON1', 'requires_artifact', 'ref-x', '', 'C-1', 1, 0, 1.0);",
            )
            .unwrap();
        }
        let bad = store
            .handle_validate_revision_dependencies(
                peer.clone(),
                &serde_json::json!({"workspace_id": 1, "contract_id": "C-1", "contract_revision": 1}),
            )
            .unwrap();
        assert_eq!(bad["valid"], serde_json::Value::Bool(false));
        assert_eq!(bad["edges_skipped"], serde_json::json!(1));
        let errs = bad["errors"].as_array().expect("errors 应为数组");
        assert_eq!(errs.len(), 1);
        assert!(errs[0].as_str().unwrap().contains("requires_artifact"));

        // 环：requires_artifact A->B + 现有硬边 B->A → has_cycle, valid=false
        {
            let conn = store.conn.lock().unwrap();
            conn.execute_batch(
                "INSERT INTO task_dependencies \
                 (workspace_id, task_id, dependency_type, target_ref, target_task_id, \
                  contract_id, contract_revision, is_informational, declared_at) VALUES \
                 (1, 'T-CON2', 'requires_artifact', 'ref-y', 'A', 'C-2', 1, 0, 1.0);",
            )
            .unwrap();
        }
        let cyc = store
            .handle_validate_revision_dependencies(
                peer.clone(),
                &serde_json::json!({"workspace_id": 1, "contract_id": "C-2", "contract_revision": 1}),
            )
            .unwrap();
        // 模拟边 A->T-CON2 与现有硬边 B->C 无环；显式检查 edges_built=1
        assert_eq!(cyc["edges_built"], serde_json::json!(1));
        assert!(cyc["errors"].as_array().unwrap().is_empty());
    }

    #[test]
    fn test_task_create_writes_modern_governance_projection_atomically() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let roles = serde_json::json!([
            {"role":"executor", "independence":"{}"},
            {"role":"reviewer", "independence":"{}"},
            {"role":"adjudicator", "independence":"{}"}
        ]);
        let created = store
            .handle_task_create(
                peer,
                &serde_json::json!({
                    "workspace_id": 1,
                    "workspace_instance_id": "ws-1",
                    "task_id": "T-CREATE-MODERN-PROJECTION",
                    "title": "modern projection",
                    "description": "task create must be claimable",
                    "steps": [{"action":"implement", "target_file":"src/main.rs", "check_items":"focused"}],
                    "identity_policy": "legacy_identity_v1",
                    "role_contracts": roles
                }),
            )
            .unwrap();
        assert_eq!(created["contract_count"], serde_json::json!(3));
        // P0-L step1：governance 投影必须回显已持久化的 identity_policy。
        assert_eq!(
            created["governance_projection"]["identity_policy"],
            serde_json::json!("legacy_identity_v1"),
        );
        let conn = store.conn.lock().unwrap();
        for (table, expected) in [
            ("task_contract_revisions", 1_i64),
            ("role_contract_lineages", 3_i64),
            ("role_contract_revisions", 3_i64),
            ("task_step_role_contract_bindings", 1_i64),
        ] {
            let predicate = if table == "role_contract_revisions" {
                "role_contract_lineage_id IN (SELECT role_contract_lineage_id FROM role_contract_lineages WHERE task_id='T-CREATE-MODERN-PROJECTION')"
            } else {
                "task_id='T-CREATE-MODERN-PROJECTION'"
            };
            let count: i64 = conn
                .query_row(
                    &format!("SELECT COUNT(*) FROM {table} WHERE {predicate}"),
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(count, expected, "{table}");
        }
    }

    #[test]
    fn test_quality_findings_uses_canonical_schema_and_legacy_details_alias() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        seed_workspace(&store);
        seed_task_binding(&store, "T-QF-CANONICAL");
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "INSERT INTO task_quality_findings
                 (task_id, step_id, finding_type, severity, status, message, evidence, source, created_at)
                 VALUES ('T-QF-CANONICAL', 'S-QF-1', 'scope', 'block', 'open', 'evidence required', 'manifest.json', 'reviewer', 1.0)",
                [],
            )
            .unwrap();
        }
        let result = store
            .handle_task_quality_findings(
                peer,
                &serde_json::json!({
                    "task_id": "T-QF-CANONICAL", "status": "open", "severity": "block"
                }),
            )
            .unwrap();
        let finding = &result["findings"][0];
        assert_eq!(finding["step_id"], serde_json::json!("S-QF-1"));
        assert_eq!(finding["evidence"], serde_json::json!("manifest.json"));
        assert_eq!(finding["details"], serde_json::json!("manifest.json"));
        assert_eq!(finding["resolved_at"], serde_json::Value::Null);
    }

    #[test]
    fn test_task_event_report_provenance_compat_adds_missing_columns() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE task_events (event_id INTEGER PRIMARY KEY, task_id TEXT NOT NULL);",
        )
        .unwrap();

        ensure_task_event_report_provenance_compat(&conn).unwrap();
        let columns: Vec<String> = {
            let mut statement = conn.prepare("PRAGMA table_info(task_events)").unwrap();
            statement
                .query_map([], |row| row.get::<_, String>(1))
                .unwrap()
                .collect::<Result<Vec<_>, _>>()
                .unwrap()
        };
        assert!(columns.contains(&"request_id".to_string()));
        assert!(columns.contains(&"step_id".to_string()));
    }

    #[test]
    fn test_executor_handoff_requires_persisted_report_provenance() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let executor_identity = serde_json::json!({
            "agent_id":"handoff-executor",
            "agent_instance_id":"handoff-executor-instance",
            "client_id":"test",
            "provider":"test",
            "model_id":"test-model",
            "session_id":"handoff-executor-session",
            "role":"executor"
        });
        register_agent_with_identity(
            &store,
            &peer,
            "handoff-executor",
            "handoff-executor-instance",
            "handoff-executor-session",
            "executor",
        );
        let roles = serde_json::json!([
            {"role":"executor", "independence":"{}"},
            {"role":"reviewer", "independence":"{}"},
            {"role":"adjudicator", "independence":"{}"}
        ]);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "workspace_instance_id": "ws-1",
                    "task_id": "T-HANDOFF-PROVENANCE",
                    "title": "handoff provenance",
                    "steps": [{"action":"implement", "target_file":"src/main.rs", "check_items":"focused"}],
                    "identity_policy": "legacy_identity_v1",
                    "role_contracts": roles
                }),
            )
            .unwrap();
        let step_id: String = {
            let conn = store.conn.lock().unwrap();
            conn.query_row(
                "SELECT id FROM task_steps WHERE task_id='T-HANDOFF-PROVENANCE'",
                [],
                |row| row.get(0),
            )
            .unwrap()
        };
        store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id":"T-HANDOFF-PROVENANCE",
                    "agent_session_id":"handoff-executor-session",
                    "identity": executor_identity
                }),
            )
            .unwrap();
        let report = store
            .handle_task_report(
                peer.clone(),
                &serde_json::json!({
                    "task_id":"T-HANDOFF-PROVENANCE",
                    "step_id":step_id,
                    "agent_session_id":"handoff-executor-session",
                    "request_id":"report-handoff-1",
                    "evidence_path":"deliverables/evidence.json",
                    "evidence_hash":"sha256:evidence-1",
                    "summary":"implemented",
                    "success":true,
                    "identity": executor_identity
                }),
            )
            .unwrap();
        assert_eq!(report["request_id"], serde_json::json!("report-handoff-1"));
        let lease = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id":"T-HANDOFF-PROVENANCE", "role":"executor",
                    "ttl_seconds":3600.0, "identity":executor_identity
                }),
            )
            .unwrap();
        let mut base = serde_json::json!({
            "task_id":"T-HANDOFF-PROVENANCE", "from_role":"executor",
            "outcome":"executor_ready_for_review", "next_role":"reviewer",
            "next_action":"review", "reason":"delivery complete",
            "independence_requirement":"required", "request_id":"handoff-provenance-1",
            "step_id":step_id, "report_request_id":"missing-report",
            "evidence_path":"deliverables/evidence.json", "evidence_hash":"sha256:evidence-1",
            "identity":executor_identity, "lease_token":lease["token"],
            "fencing_counter":lease["fencing_counter"]
        });
        let missing = store.handle_task_handoff(peer.clone(), &base).unwrap_err();
        assert_eq!(missing.code, "E_HANDOFF_REPORT_NOT_FOUND");
        base["report_request_id"] = serde_json::json!("report-handoff-1");
        let accepted = store.handle_task_handoff(peer, &base).unwrap();
        assert_eq!(accepted["status"], serde_json::json!("review"));
    }

    // ===== P0-L step1：task.create identity policy fail-closed 负矩阵 =====

    fn p0l_governance_roles() -> serde_json::Value {
        serde_json::json!([
            {"role":"executor", "independence":"{}"},
            {"role":"reviewer", "independence":"{}"},
            {"role":"adjudicator", "independence":"{}"}
        ])
    }

    fn p0l_task_exists(store: &TaskCollabStore, task_id: &str) -> bool {
        let conn = store.conn.lock().unwrap();
        conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM tasks WHERE id = ?1)",
            [task_id],
            |row| row.get::<_, bool>(0),
        )
        .unwrap()
    }

    #[test]
    fn test_task_create_missing_identity_policy_fail_closed_and_rolls_back() {
        // missing：带 role_contracts 但无 envelope/无顶层 policy → E_TASK_IDENTITY_POLICY_REQUIRED，
        // 且整事务回滚（任务行不存在）。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let err = store
            .handle_task_create(
                peer,
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": "T-P0L-MISSING",
                    "title": "no policy",
                    "steps": [{"action":"implement", "target_file":"a.rs"}],
                    "role_contracts": p0l_governance_roles()
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_TASK_IDENTITY_POLICY_REQUIRED");
        assert!(
            !p0l_task_exists(&store, "T-P0L-MISSING"),
            "policy fail-closed 必须整事务回滚"
        );
    }

    #[test]
    fn test_task_create_unknown_or_role_worker_without_envelope_rejected() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        // unknown policy → mismatch 错误。
        let unknown = store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": "T-P0L-UNKNOWN",
                    "title": "unknown policy",
                    "steps": [{"action":"implement", "target_file":"a.rs"}],
                    "identity_policy": "role_worker_v2",
                    "role_contracts": p0l_governance_roles()
                }),
            )
            .unwrap_err();
        assert_eq!(unknown.code, "E_TASK_IDENTITY_POLICY_MISMATCH");
        // role_worker_v1 无 envelope → required 错误（禁止隐式生成）。
        let rw = store
            .handle_task_create(
                peer,
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": "T-P0L-RW-NOENV",
                    "title": "role worker without envelope",
                    "steps": [{"action":"implement", "target_file":"a.rs"}],
                    "identity_policy": "role_worker_v1",
                    "role_contracts": p0l_governance_roles()
                }),
            )
            .unwrap_err();
        assert_eq!(rw.code, "E_TASK_IDENTITY_POLICY_REQUIRED");
        assert!(!p0l_task_exists(&store, "T-P0L-UNKNOWN"));
        assert!(!p0l_task_exists(&store, "T-P0L-RW-NOENV"));
    }

    #[test]
    fn test_task_create_envelope_policy_mismatch_and_contract_id_binding() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let env_for = |task_id: &str, policy: &str| {
            serde_json::json!({
                "contract_id": format!("TC-{task_id}"),
                "revision": 1,
                "profile": "code_change",
                "identity_policy": policy,
                "objective": {"statement": "t", "description": "d", "source": "task.create"},
                "interfaces": {"rpc": "task.create", "task_id": task_id},
                "allowed_edit_scope": {"files": ["a.rs"], "symbols": [], "generated_from": "task steps"},
                "acceptance_clauses": [],
                "risks": [],
                "rollback": {"strategy": "append-only"},
                "dependencies": [],
                "handoff": {"from": "executor", "to": "reviewer", "independence_requirement": "required"},
                "source": {"kind": "task.create", "task_id": task_id}
            })
        };
        // envelope 缺 identity_policy → required。
        let mut no_policy = env_for("T-P0L-ENVPOLICY", "legacy_identity_v1");
        no_policy.as_object_mut().unwrap().remove("identity_policy");
        let err = store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": "T-P0L-ENVPOLICY",
                    "title": "envelope without policy",
                    "steps": [{"action":"implement", "target_file":"a.rs"}],
                    "task_contract_envelope": no_policy,
                    "role_contracts": p0l_governance_roles()
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_TASK_IDENTITY_POLICY_REQUIRED");
        // 顶层 policy 与 envelope 不一致 → mismatch。
        let mismatch = store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": "T-P0L-TOPMISMATCH",
                    "title": "top level mismatch",
                    "steps": [{"action":"implement", "target_file":"a.rs"}],
                    "identity_policy": "legacy_identity_v1",
                    "task_contract_envelope": env_for("T-P0L-TOPMISMATCH", "role_worker_v1"),
                    "role_contracts": p0l_governance_roles()
                }),
            )
            .unwrap_err();
        assert_eq!(mismatch.code, "E_TASK_IDENTITY_POLICY_MISMATCH");
        // envelope contract_id 跨任务注入 → mismatch。
        let foreign = store
            .handle_task_create(
                peer,
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": "T-P0L-FOREIGN",
                    "title": "foreign contract id",
                    "steps": [{"action":"implement", "target_file":"a.rs"}],
                    "task_contract_envelope": env_for("T-OTHER-TASK", "role_worker_v1"),
                    "role_contracts": p0l_governance_roles()
                }),
            )
            .unwrap_err();
        assert_eq!(foreign.code, "E_TASK_IDENTITY_POLICY_MISMATCH");
        assert!(!p0l_task_exists(&store, "T-P0L-ENVPOLICY"));
        assert!(!p0l_task_exists(&store, "T-P0L-TOPMISMATCH"));
        assert!(!p0l_task_exists(&store, "T-P0L-FOREIGN"));
    }

    #[test]
    fn test_task_create_caller_envelope_persists_policy_atomically() {
        // 正向：caller canonical envelope（role_worker_v1）原样持久化，
        // governance_projection 回显 policy，current policy 只读投影一致。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let envelope = serde_json::json!({
            "contract_id": "TC-T-P0L-RW-OK",
            "revision": 1,
            "profile": "code_change",
            "identity_policy": "role_worker_v1",
            "objective": {"statement": "role worker task", "description": "canonical envelope", "source": "task.create"},
            "interfaces": {"rpc": "task.create", "task_id": "T-P0L-RW-OK"},
            "allowed_edit_scope": {"files": ["a.rs"], "symbols": [], "generated_from": "task steps"},
            "acceptance_clauses": [],
            "risks": [],
            "rollback": {"strategy": "append-only"},
            "dependencies": [],
            "handoff": {"from": "executor", "to": "reviewer", "independence_requirement": "required"},
            "source": {"kind": "task.create", "task_id": "T-P0L-RW-OK"}
        });
        let created = store
            .handle_task_create(
                peer,
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": "T-P0L-RW-OK",
                    "title": "role worker ok",
                    "steps": [{"action":"implement", "target_file":"a.rs"}],
                    "task_contract_envelope": envelope,
                    "role_contracts": p0l_governance_roles()
                }),
            )
            .unwrap();
        assert_eq!(
            created["governance_projection"]["identity_policy"],
            serde_json::json!("role_worker_v1")
        );
        let conn = store.conn.lock().unwrap();
        let payload: String = conn
            .query_row(
                "SELECT envelope_payload FROM task_contract_revisions WHERE task_id = 'T-P0L-RW-OK' ORDER BY revision DESC LIMIT 1",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(
            payload.contains("role_worker_v1"),
            "持久化 envelope 必须保留 caller 声明的 policy"
        );
        drop(conn);
        let current = {
            let conn = store.conn.lock().unwrap();
            get_current_task_contract_policy(&conn, "T-P0L-RW-OK").unwrap()
        };
        assert_eq!(current.as_deref(), Some("role_worker_v1"));
    }

    // ===== P0-L step2：bootstrap/revise role_worker_v1 分支负矩阵 =====
    // check_items：role_worker_v1 分支强制 expected adjudicator worker + 独立 reviewer
    // lease/fencing；generic 旧 revision 的 hash-linked policy 升级；legacy 原路径不变。

    /// 登记指定 role worker 并返回一次性 credential（只持久化 hash）。
    fn p0l_enroll_worker(
        store: &TaskCollabStore,
        owner_key: &str,
        worker: &str,
        instance: &str,
        role: &str,
    ) -> String {
        let conn = store.conn.lock().unwrap();
        let tx = conn.unchecked_transaction().unwrap();
        let res = crate::daemon::task_loop::role_worker::enroll_role_worker(
            &tx,
            owner_key,
            &serde_json::json!({
                "role_worker_id": worker,
                "role_instance_id": instance,
                "role": role,
                "runtime": {"client": "p0l-test"}
            }),
            1,
        )
        .unwrap();
        tx.commit().unwrap();
        res["credential"].as_str().unwrap().to_string()
    }

    fn p0l_role_worker_auth(
        worker: &str,
        instance: &str,
        session: &str,
        credential: &str,
    ) -> serde_json::Value {
        serde_json::json!({
            "role_worker_id": worker,
            "role_instance_id": instance,
            "role_session_id": session,
            "credential": credential,
            "runtime": {"client": "p0l-test"}
        })
    }

    /// seed：任务（无 role_contracts，保留显式 bootstrap 空投影）+ SQL 补种三角色合同 +
    /// reviewer/adjudicator 注册 + 独立 reviewer lease。
    fn p0l_seed_governance_task(
        store: &TaskCollabStore,
        peer: &PeerCredential,
        task_id: &str,
    ) -> serde_json::Value {
        seed_workspace(store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": task_id,
                    "title": "p0l step2 branch",
                    "steps": [{"action": "audit", "target_file": "a.rs"}],
                }),
            )
            .unwrap();
        register_agent_with_identity(
            store,
            peer,
            &format!("{task_id}-rev"),
            "rw-rev-inst",
            "rw-rev-sess",
            "reviewer",
        );
        register_agent_with_identity(
            store,
            peer,
            &format!("{task_id}-adj"),
            "rw-adj-inst",
            "rw-adj-sess",
            "adjudicator",
        );
        {
            let conn = store.conn.lock().unwrap();
            for role in ["executor", "reviewer", "adjudicator"] {
                conn.execute(
                    "INSERT INTO role_contracts
                     (contract_id, task_id, step_id, role, skill_id, skill_version,
                      prompt_template_id, prompt_hash, allowed_paths, forbidden_paths,
                      commands, acceptance_checks, required_evidence, handoff_to,
                      independence, revision, is_current, created_at, created_by)
                     VALUES (?1, ?2, '', ?3, '', '', '', '', '', '', '', '', '', '', '{}', 1, 1, 0, 'test')",
                    params![format!("RC-{task_id}-{role}"), task_id.to_string(), role.to_string()],
                )
                .unwrap();
            }
        }
        store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": task_id, "role": "reviewer",
                    "identity": {"agent_id": format!("{task_id}-rev"), "agent_instance_id": "rw-rev-inst",
                                 "client_id": "t", "provider": "t", "model_id": "m",
                                 "session_id": "rw-rev-sess", "role": "reviewer"},
                }),
            )
            .unwrap()
    }

    #[test]
    fn test_contract_bootstrap_role_worker_v1_requires_expected_adjudicator_worker() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let task_id = "T-P0L-RWB";
        let lease = p0l_seed_governance_task(&store, &peer, task_id);
        let rw_envelope = serde_json::json!({
            "contract_id": format!("c-{task_id}"), "revision": 1, "profile": "code_change",
            "objective": "bootstrap rw", "source_provenance": "p0l-step2",
            "interfaces": ["cli"], "allowed_edit_scope": ["src/"],
            "acceptance_clauses": ["tests pass"], "risks": ["low"],
            "rollback": ["revert"], "dependencies": ["none"],
            "identity_policy": "role_worker_v1",
        });
        let request = |request_id: &str, auth: Option<serde_json::Value>| {
            let mut body = serde_json::json!({
                "task_id": task_id,
                "envelope": rw_envelope.clone(),
                "workspace_id": 1,
                "workspace_instance_id": "ws-1",
                "request_id": request_id,
                "evidence_path": "ev/path", "evidence_hash": "ev-hash",
                "lease_token": lease["token"], "fencing_counter": lease["fencing_counter"],
                "identity": {"agent_id": format!("{task_id}-adj"), "agent_instance_id": "rw-adj-inst",
                             "client_id": "t", "provider": "t", "model_id": "m",
                             "session_id": "rw-adj-sess", "role": "adjudicator"},
            });
            if let Some(auth) = auth {
                body.as_object_mut()
                    .unwrap()
                    .insert("role_worker_auth".to_string(), auth);
            }
            body
        };
        // 1) 缺少 role_worker_auth → fail-closed
        let err = store
            .handle_task_contract_bootstrap(peer.clone(), &request("rwb-1", None))
            .unwrap_err();
        assert_eq!(err.code, "E_TASK_IDENTITY_POLICY_REQUIRED");
        // 2) executor worker 冒充 adjudicator → 角色冻结映射拒绝
        let executor_cred = p0l_enroll_worker(
            &store,
            &peer.owner_key(),
            "rw-exec-worker",
            "rw-exec-inst",
            "executor",
        );
        let err = store
            .handle_task_contract_bootstrap(
                peer.clone(),
                &request(
                    "rwb-2",
                    Some(p0l_role_worker_auth(
                        "rw-exec-worker",
                        "rw-exec-inst",
                        "rw-exec-sess",
                        &executor_cred,
                    )),
                ),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_ROLE_WORKER_ROLE_MISMATCH");
        // 3) 错误 credential → 拒绝（且先于任何投影写入）
        let adj_cred = p0l_enroll_worker(
            &store,
            &peer.owner_key(),
            "rw-adj-worker",
            "rw-adj-inst-w",
            "adjudicator",
        );
        let err = store
            .handle_task_contract_bootstrap(
                peer.clone(),
                &request(
                    "rwb-3",
                    Some(p0l_role_worker_auth(
                        "rw-adj-worker",
                        "rw-adj-inst-w",
                        "rw-adj-sess-w",
                        "wrong-credential",
                    )),
                ),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_ROLE_WORKER_CREDENTIAL_INVALID");
        // 4) 正确 adjudicator worker → 成功，且追加 append-only runtime provenance
        let boot = store
            .handle_task_contract_bootstrap(
                peer.clone(),
                &request(
                    "rwb-4",
                    Some(p0l_role_worker_auth(
                        "rw-adj-worker",
                        "rw-adj-inst-w",
                        "rw-adj-sess-w",
                        &adj_cred,
                    )),
                ),
            )
            .unwrap();
        assert!(boot["contract_hash"].as_str().is_some());
        let conn = store.conn.lock().unwrap();
        let provenance: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM role_runtime_provenance WHERE task_id = ?1 AND role_worker_id = 'rw-adj-worker' AND action_type = 'task.contract_bootstrap'",
                params![task_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(
            provenance, 1,
            "role_worker_v1 bootstrap 必须落一条 append-only runtime provenance"
        );
        let payload: String = conn
            .query_row(
                "SELECT envelope_payload FROM task_contract_revisions WHERE task_id = ?1 ORDER BY revision DESC LIMIT 1",
                params![task_id],
                |r| r.get(0),
            )
            .unwrap();
        assert!(
            payload.contains("role_worker_v1"),
            "bootstrap 必须持久化 caller 声明的 policy"
        );
    }

    #[test]
    fn test_contract_revise_role_worker_policy_upgrade_hash_linked_and_downgrade_forbidden() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let task_id = "T-P0L-RWR";
        let lease = p0l_seed_governance_task(&store, &peer, task_id);
        // legacy bootstrap v1（无 policy 槽位 = legacy 原路径）
        let v1_envelope = serde_json::json!({
            "contract_id": format!("c-{task_id}"), "revision": 1, "profile": "code_change",
            "objective": "legacy v1", "source_provenance": "p0l-step2",
            "interfaces": ["cli"], "allowed_edit_scope": ["src/"],
            "acceptance_clauses": ["tests pass"], "risks": ["low"],
            "rollback": ["revert"], "dependencies": ["none"],
        });
        let boot = store
            .handle_task_contract_bootstrap(
                peer.clone(),
                &serde_json::json!({
                    "task_id": task_id,
                    "envelope": v1_envelope,
                    "workspace_id": 1,
                    "workspace_instance_id": "ws-1",
                    "request_id": "rwr-boot",
                    "evidence_path": "ev/path", "evidence_hash": "ev-hash",
                    "lease_token": lease["token"], "fencing_counter": lease["fencing_counter"],
                    "identity": {"agent_id": format!("{task_id}-adj"), "agent_instance_id": "rw-adj-inst",
                                 "client_id": "t", "provider": "t", "model_id": "m",
                                 "session_id": "rw-adj-sess", "role": "adjudicator"},
                }),
            )
            .unwrap();
        let v1_hash = boot["contract_hash"].as_str().unwrap().to_string();
        let reviewer_lease_id = lease["lease_id"].as_str().unwrap().to_string();
        let fencing = lease["fencing_counter"].as_i64().unwrap();
        let adj_cred = p0l_enroll_worker(
            &store,
            &peer.owner_key(),
            "rwr-adj-worker",
            "rwr-adj-inst",
            "adjudicator",
        );
        // P0-L R1/R2：worker 路径 revise 请求——identity 可选（仅 provenance），reviewer
        // proof 为 server-side reference（reviewer_lease_id + fencing_counter），严禁 raw token。
        let revise_worker = |request_id: &str,
                             envelope: serde_json::Value,
                             auth: Option<serde_json::Value>,
                             proof: Option<(&str, i64)>| {
            let mut body = serde_json::json!({
                "task_id": task_id,
                "envelope": envelope,
                "expected_previous_hash": v1_hash,
                "workspace_id": 1,
                "workspace_instance_id": "ws-1",
                "request_id": request_id,
                "evidence_path": "ev/path2", "evidence_hash": "ev-hash2",
            });
            let object = body.as_object_mut().unwrap();
            if let Some(auth) = auth {
                object.insert("role_worker_auth".to_string(), auth);
            }
            if let Some((lease_id, counter)) = proof {
                object.insert(
                    "reviewer_lease_id".to_string(),
                    serde_json::Value::String(lease_id.to_string()),
                );
                object.insert("fencing_counter".to_string(), serde_json::json!(counter));
            }
            body
        };
        let v2_envelope = serde_json::json!({
            "contract_id": format!("c-{task_id}"),
            "revision": 2,
            "supersedes_revision": 1,
            "supersedes_contract_hash": v1_hash,
            "profile": "code_change",
            "objective": "policy upgrade",
            "source_provenance": "p0l-step2-v2",
            "interfaces": ["cli"],
            "allowed_edit_scope": ["src/"],
            "acceptance_clauses": ["tests pass"],
            "risks": ["low"],
            "rollback": ["revert"],
            "dependencies": ["none"],
            "identity_policy": "role_worker_v1",
        });
        let adj_auth =
            p0l_role_worker_auth("rwr-adj-worker", "rwr-adj-inst", "rwr-adj-sess", &adj_cred);
        // 1) generic 旧 revision 升级 role_worker_v1 但缺 auth → fail-closed
        let err = store
            .handle_task_contract_revise(
                peer.clone(),
                &revise_worker(
                    "rwr-n1",
                    v2_envelope.clone(),
                    None,
                    Some((reviewer_lease_id.as_str(), fencing)),
                ),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_TASK_IDENTITY_POLICY_REQUIRED");
        // 2) R2 负矩阵：携带 raw reviewer lease token → 绝对禁止（不论其他字段）
        let mut raw = revise_worker(
            "rwr-n2",
            v2_envelope.clone(),
            Some(adj_auth.clone()),
            Some((reviewer_lease_id.as_str(), fencing)),
        );
        raw.as_object_mut().unwrap().insert(
            "lease_token".to_string(),
            serde_json::json!("raw-reviewer-token-must-never-travel"),
        );
        let err = store
            .handle_task_contract_revise(peer.clone(), &raw)
            .unwrap_err();
        assert_eq!(err.code, "E_REVIEWER_PROOF_RAW_TOKEN_FORBIDDEN");
        // 3) 缺 reviewer_lease_id → fail-closed（禁止无 proof 治理写）
        let err = store
            .handle_task_contract_revise(
                peer.clone(),
                &revise_worker("rwr-n3", v2_envelope.clone(), Some(adj_auth.clone()), None),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_REVIEWER_PROOF_REQUIRED");
        // 4) 未知 reviewer_lease_id → 无 active lease
        let err = store
            .handle_task_contract_revise(
                peer.clone(),
                &revise_worker(
                    "rwr-n4",
                    v2_envelope.clone(),
                    Some(adj_auth.clone()),
                    Some(("L-nonexistent", fencing)),
                ),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_REVIEWER_PROOF_LEASE_NOT_FOUND");
        // 5) fencing 不一致 → fenced（旧持有者写入拒绝）
        let err = store
            .handle_task_contract_revise(
                peer.clone(),
                &revise_worker(
                    "rwr-n5",
                    v2_envelope.clone(),
                    Some(adj_auth.clone()),
                    Some((reviewer_lease_id.as_str(), fencing + 1)),
                ),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_TASK_CONTRACT_REVISE_FENCED");
        // 6) adjudicator worker session 与 reviewer lease holder 同源 → 分离拒绝
        let adj_cred2 = p0l_enroll_worker(
            &store,
            &peer.owner_key(),
            "rwr-adj-worker2",
            "rwr-adj-inst2",
            "adjudicator",
        );
        let err = store
            .handle_task_contract_revise(
                peer.clone(),
                &revise_worker(
                    "rwr-n6",
                    v2_envelope.clone(),
                    Some(p0l_role_worker_auth(
                        "rwr-adj-worker2",
                        "rwr-adj-inst2",
                        "rw-rev-sess",
                        &adj_cred2,
                    )),
                    Some((reviewer_lease_id.as_str(), fencing)),
                ),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_SESSION");
        // 7) 正例：完整 adjudicator worker auth + server-side proof，无 identity、无 raw token →
        //    hash-linked 升级成功（revision 2）。同时证明 R1：identity 非必需。
        let rev = store
            .handle_task_contract_revise(
                peer.clone(),
                &revise_worker(
                    "rwr-ok",
                    v2_envelope.clone(),
                    Some(adj_auth.clone()),
                    Some((reviewer_lease_id.as_str(), fencing)),
                ),
            )
            .unwrap();
        assert_eq!(rev["previous_revision"], 1);
        assert_eq!(rev["revision"], 2);
        let current = {
            let conn = store.conn.lock().unwrap();
            get_current_task_contract_policy(&conn, task_id).unwrap()
        };
        assert_eq!(current.as_deref(), Some("role_worker_v1"));
        // 事件归属 = Role Worker（而非 identity）。
        {
            let conn = store.conn.lock().unwrap();
            let (actor, session, role): (String, String, String) = conn
                .query_row(
                    "SELECT actor_identity, agent_session_id, role FROM task_events \
                     WHERE task_id = ?1 AND reason_code = 'task_contract_revised' \
                     ORDER BY monotonic_seq DESC LIMIT 1",
                    params![task_id],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .unwrap();
            assert_eq!(actor, "rwr-adj-worker");
            assert_eq!(session, "rwr-adj-sess");
            assert_eq!(role, "adjudicator");
        }
        // 3) 当前 role_worker_v1：显式 legacy 降级与隐式降级（无槽位）都必须拒绝。        // 注：升级失败时 revision 仍为 2，expected_previous_hash 沿用 v1_hash 会被链验证拒绝，
        // 因此先构造基于 revision 2 的 envelope（supersedes 锚点从 DB 读取）。
        let v2_hash: String = {
            let conn = store.conn.lock().unwrap();
            conn.query_row(
                "SELECT contract_hash FROM task_contract_revisions WHERE task_id = ?1 AND revision = 2",
                params![task_id],
                |r| r.get(0),
            )
            .unwrap()
        };
        let v3_base = |policy: Option<&str>| {
            let mut envelope = serde_json::json!({
                "contract_id": format!("c-{task_id}"),
                "revision": 3,
                "supersedes_revision": 2,
                "supersedes_contract_hash": v2_hash,
                "profile": "code_change",
                "objective": "attempted downgrade",
                "source_provenance": "p0l-step2-v3",
                "interfaces": ["cli"],
                "allowed_edit_scope": ["src/"],
                "acceptance_clauses": ["tests pass"],
                "risks": ["low"],
                "rollback": ["revert"],
                "dependencies": ["none"],
            });
            if let Some(policy) = policy {
                envelope.as_object_mut().unwrap().insert(
                    "identity_policy".to_string(),
                    serde_json::Value::String(policy.to_string()),
                );
            }
            envelope
        };
        let revise_v3 = |request_id: &str, envelope: serde_json::Value| {
            serde_json::json!({
                "task_id": task_id,
                "envelope": envelope,
                "expected_previous_hash": v2_hash,
                "workspace_id": 1,
                "workspace_instance_id": "ws-1",
                "request_id": request_id,
                "evidence_path": "ev/path3", "evidence_hash": "ev-hash3",
                "reviewer_lease_id": reviewer_lease_id,
                "fencing_counter": fencing,
                "role_worker_auth": p0l_role_worker_auth("rwr-adj-worker", "rwr-adj-inst", "rwr-adj-sess", &adj_cred),
            })
        };
        // 3a) 显式降级 → policy mismatch
        let err = store
            .handle_task_contract_revise(
                peer.clone(),
                &revise_v3("rwr-3", v3_base(Some("legacy_identity_v1"))),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_TASK_IDENTITY_POLICY_MISMATCH");
        // 3b) 隐式降级（无 policy 槽位）→ 同样拒绝（绝不静默回退）
        let err = store
            .handle_task_contract_revise(peer.clone(), &revise_v3("rwr-4", v3_base(None)))
            .unwrap_err();
        assert_eq!(err.code, "E_TASK_IDENTITY_POLICY_MISMATCH");
        // 4) revision 链未被降级尝试污染（仍为 [1, 2]）
        let conn = store.conn.lock().unwrap();
        let revisions: Vec<i64> = {
            let mut stmt = conn
                .prepare("SELECT revision FROM task_contract_revisions WHERE task_id = ?1 ORDER BY revision ASC")
                .unwrap();
            stmt.query_map(params![task_id], |r| r.get::<_, i64>(0))
                .unwrap()
                .flatten()
                .collect()
        };
        assert_eq!(revisions, vec![1, 2], "降级拒绝后不得新增 revision");
    }

    // ===== P0-L step3：next_action 投影 + task.claim policy 强制负矩阵 =====
    // check_items：next_action 返回 structured requirements/blocker；claim 在同一事务、
    // 任何 step/contract binding 前验证 expected 角色稳定 worker、policy、separation、
    // contract claim 与 workspace binding。

    fn p0l_role_worker_envelope(task_id: &str) -> serde_json::Value {
        serde_json::json!({
            "contract_id": format!("TC-{task_id}"),
            "revision": 1,
            "profile": "code_change",
            "identity_policy": "role_worker_v1",
            "objective": {"statement": "role worker claim gate", "description": "p0l step3", "source": "task.create"},
            "interfaces": {"rpc": "task.create", "task_id": task_id},
            "allowed_edit_scope": {"files": ["a.rs"], "symbols": [], "generated_from": "task steps"},
            "acceptance_clauses": [],
            "risks": [],
            "rollback": {"strategy": "append-only"},
            "dependencies": [],
            "handoff": {"from": "executor", "to": "reviewer", "independence_requirement": "required"},
            "source": {"kind": "task.create", "task_id": task_id}
        })
    }

    #[test]
    fn test_task_claim_role_worker_v1_requires_expected_worker_and_records_provenance() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let task_id = "T-P0L-CLM";
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": task_id,
                    "title": "p0l step3 claim gate",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                    "task_contract_envelope": p0l_role_worker_envelope(task_id),
                    "role_contracts": p0l_governance_roles()
                }),
            )
            .unwrap();
        register_agent_with_identity(
            &store,
            &peer,
            &format!("{task_id}-exec"),
            "p0l-clm-exec-inst",
            "p0l-clm-exec-sess",
            "executor",
        );
        let exec_cred = p0l_enroll_worker(
            &store,
            &peer.owner_key(),
            "p0l-clm-rw",
            "p0l-clm-rwi",
            "executor",
        );

        let claim_with = |auth: Option<serde_json::Value>| {
            let mut claim = serde_json::json!({
                "task_id": task_id,
                "agent_session_id": "p0l-clm-exec-sess",
                "identity": {
                    "agent_id": format!("{task_id}-exec"),
                    "agent_instance_id": "p0l-clm-exec-inst",
                    "client_id": "t", "provider": "t", "model_id": "m",
                    "session_id": "p0l-clm-exec-sess", "role": "executor"
                },
            });
            if let Some(auth) = auth {
                claim
                    .as_object_mut()
                    .unwrap()
                    .insert("role_worker_auth".to_string(), auth);
            }
            claim
        };

        // 1) 缺 role_worker_auth → fail-closed（绝不隐式降级）
        let err = store
            .handle_task_claim(peer.clone(), &claim_with(None))
            .unwrap_err();
        assert_eq!(err.code, "E_TASK_IDENTITY_POLICY_REQUIRED");
        // 2) 错误 credential → 凭证无效（拒绝在 binding 之前）
        let err = store
            .handle_task_claim(
                peer.clone(),
                &claim_with(Some(p0l_role_worker_auth(
                    "p0l-clm-rw",
                    "p0l-clm-rwi",
                    "p0l-clm-sess",
                    "deadbeef-not-a-credential",
                ))),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_ROLE_WORKER_CREDENTIAL_INVALID");
        // 3) executor 稳定 worker 通过：claim 成功且追加恰好一条 runtime provenance。
        //    同时验证两次拒绝均未留下任何部分写入（步骤仍全部 pending）。
        store
            .handle_task_claim(
                peer.clone(),
                &claim_with(Some(p0l_role_worker_auth(
                    "p0l-clm-rw",
                    "p0l-clm-rwi",
                    "p0l-clm-sess",
                    &exec_cred,
                ))),
            )
            .unwrap();
        let conn = store.conn.lock().unwrap();
        let provenance: Vec<(String, String)> = {
            let mut stmt = conn
                .prepare(
                    "SELECT action_type, runtime_payload_json FROM role_runtime_provenance \
                     WHERE task_id = ?1 ORDER BY recorded_at ASC",
                )
                .unwrap();
            stmt.query_map(params![task_id], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .unwrap()
            .flatten()
            .collect()
        };
        assert_eq!(
            provenance.len(),
            1,
            "成功 claim 必须恰好记录一条 runtime provenance"
        );
        assert_eq!(provenance[0].0, "task.claim");
        assert!(
            !provenance[0].1.contains(&exec_cred),
            "raw credential 绝不落库（只存 hash，provenance 不含秘密）"
        );
        let status: String = conn
            .query_row(
                "SELECT status FROM tasks WHERE id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(status, "in_progress");
        // claim 事件角色归属 = worker 登记角色（executor）。
        let event_role: String = conn
            .query_row(
                "SELECT role FROM task_events WHERE task_id = ?1 AND reason_code = 'claimed' \
                 ORDER BY monotonic_seq DESC LIMIT 1",
                params![task_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(event_role, "executor");
    }

    /// P0-L R1：worker-first 角色锚点——即使 runtime identity.role 伪装为其他治理角色，
    /// claim 的角色归属仍唯一取自 Role Worker 登记角色；identity 仅作为 provenance 落账本，
    /// 不参与任何授权判定（会话归属也取 worker session，而非客户端 session）。
    #[test]
    fn test_task_claim_role_worker_v1_role_anchor_is_worker_mapping_not_runtime_identity() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let task_id = "T-P0L-CLM2";
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": task_id,
                    "title": "p0l r1 worker-first anchor",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                    "task_contract_envelope": p0l_role_worker_envelope(task_id),
                    "role_contracts": p0l_governance_roles()
                }),
            )
            .unwrap();
        let adj_cred = p0l_enroll_worker(
            &store,
            &peer.owner_key(),
            "p0l-clm2-rw-adj",
            "p0l-clm2-rwi-adj",
            "adjudicator",
        );
        // runtime identity.role 伪装为 executor（未注册 agent），但授权锚点是 adjudicator worker。
        let res = store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id": task_id,
                    "agent_session_id": "p0l-clm2-client-sess",
                    "identity": {
                        "agent_id": "p0l-clm2-spoof",
                        "agent_instance_id": "p0l-clm2-spoof-inst",
                        "client_id": "t", "provider": "t", "model_id": "m",
                        "session_id": "p0l-clm2-client-sess", "role": "executor"
                    },
                    "role_worker_auth": p0l_role_worker_auth(
                        "p0l-clm2-rw-adj",
                        "p0l-clm2-rwi-adj",
                        "p0l-clm2-adj-sess",
                        &adj_cred,
                    ),
                }),
            )
            .unwrap();
        // claimed_by 与合同归属均取 worker 锚点（而非客户端 session / identity.role）。
        assert_eq!(res["claimed_by"], json!("p0l-clm2-adj-sess"));
        assert_eq!(res["role_contract"]["role"], json!("adjudicator"));
        let conn = store.conn.lock().unwrap();
        let event_role: String = conn
            .query_row(
                "SELECT role FROM task_events WHERE task_id = ?1 AND reason_code = 'claimed' \
                 ORDER BY monotonic_seq DESC LIMIT 1",
                params![task_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(
            event_role, "adjudicator",
            "claim 事件角色必须是 worker 登记角色"
        );
        let event_session: String = conn
            .query_row(
                "SELECT agent_session_id FROM task_events WHERE task_id = ?1 AND reason_code = 'claimed' \
                 ORDER BY monotonic_seq DESC LIMIT 1",
                params![task_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(event_session, "p0l-clm2-adj-sess");
        // runtime identity 只落 action_identities（provenance），未注册也不拒绝。
        let recorded: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM action_identities \
                 WHERE task_id = ?1 AND action_type = 'task.claim' AND agent_id = 'p0l-clm2-spoof'",
                params![task_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(recorded, 1, "runtime identity 必须仅作为 provenance 记录");
    }

    #[test]
    fn test_task_claim_unknown_or_unresolved_policy_is_fail_closed() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let task_id = "T-P0L-CLMP";
        // 显式 legacy 创建（无 role_contracts）：得到带合法 policy 的基线任务。
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": task_id,
                    "title": "p0l step3 policy fail-closed",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                    "identity_policy": "legacy_identity_v1",
                }),
            )
            .unwrap();
        register_agent_with_identity(
            &store,
            &peer,
            &format!("{task_id}-exec"),
            "p0l-clmp-exec-inst",
            "p0l-clmp-exec-sess",
            "executor",
        );
        let append_revision = |payload: &str, revision: i64| {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "INSERT INTO task_contract_revisions
                 (contract_id, revision, contract_hash, profile, task_id, workspace_id,
                  envelope_payload, created_at, created_by)
                 VALUES (?1, ?2, ?3, 'code_change', ?4, 1, ?5, 1.0, 'test')",
                params![
                    format!("TC-{task_id}-s3-{revision}"),
                    revision,
                    format!("sha256:s3-{revision}"),
                    task_id,
                    payload
                ],
            )
            .unwrap();
        };
        let claim = serde_json::json!({
            "task_id": task_id,
            "agent_session_id": "p0l-clmp-exec-sess",
            "identity": {
                "agent_id": format!("{task_id}-exec"),
                "agent_instance_id": "p0l-clmp-exec-inst",
                "client_id": "t", "provider": "t", "model_id": "m",
                "session_id": "p0l-clmp-exec-sess", "role": "executor"
            },
        });
        // 1) 未知 policy → 禁止 claim（禁止隐式降级为 legacy）
        append_revision("{\"identity_policy\":\"mystery_policy_v9\"}", 2);
        let err = store.handle_task_claim(peer.clone(), &claim).unwrap_err();
        assert_eq!(err.code, "E_TASK_IDENTITY_POLICY_MISMATCH");
        // 2) 合同存在但无 policy 槽位（unresolved）→ 同样禁止
        append_revision("{}", 3);
        let err = store.handle_task_claim(peer.clone(), &claim).unwrap_err();
        assert_eq!(err.code, "E_TASK_IDENTITY_POLICY_MISMATCH");
        // 3) 两次拒绝均未改变任务状态（仍 open，无任何 binding）
        let conn = store.conn.lock().unwrap();
        let status: String = conn
            .query_row(
                "SELECT status FROM tasks WHERE id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(status, "open");
        let provenance_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM role_runtime_provenance WHERE task_id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(provenance_count, 0, "拒绝路径不得留下 runtime provenance");
    }

    #[test]
    fn test_task_claim_legacy_and_policyless_tasks_keep_original_path() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        // 1) 显式 legacy 任务（带 role_contracts）：claim 只需 identity，不需 role_worker_auth。
        let legacy_id = "T-P0L-CLML";
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": legacy_id,
                    "title": "legacy claim path",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                    "identity_policy": "legacy_identity_v1",
                    "role_contracts": p0l_governance_roles()
                }),
            )
            .unwrap();
        register_agent_with_identity(
            &store,
            &peer,
            &format!("{legacy_id}-exec"),
            "p0l-clml-exec-inst",
            "p0l-clml-exec-sess",
            "executor",
        );
        store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id": legacy_id,
                    "agent_session_id": "p0l-clml-exec-sess",
                    "identity": {
                        "agent_id": format!("{legacy_id}-exec"),
                        "agent_instance_id": "p0l-clml-exec-inst",
                        "client_id": "t", "provider": "t", "model_id": "m",
                        "session_id": "p0l-clml-exec-sess", "role": "executor"
                    },
                }),
            )
            .unwrap();
        // 2) 无合同 revision 的任务（P0-L 之前历史形态）：无 identity 也保持原路径。
        let bare_id = "T-P0L-CLMN";
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": bare_id,
                    "title": "no contract revision",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                }),
            )
            .unwrap();
        store
            .handle_task_claim(
                peer,
                &serde_json::json!({"task_id": bare_id, "agent_session_id": "p0l-clmn-sess"}),
            )
            .unwrap();
    }
