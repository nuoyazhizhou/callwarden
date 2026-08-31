//! task_collab 核心生命周期、任务创建和步骤测试。

use super::*;
use super::support::*;
    #[test]
    fn test_task_collab_full_lifecycle() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);

        // 1. Agent Register
        let reg_params = serde_json::json!({
            "agent_name": "agent-alpha",
            "capabilities": ["code", "review"]
        });
        let reg_res = store
            .handle_agent_register(peer.clone(), &reg_params)
            .unwrap();
        assert_eq!(reg_res["status"], "registered");

        // 2. Task Create
        let create_params = serde_json::json!({
            "workspace_id": 1,
            "title": "Fix memory leak in parser",
            "description": "Investigate tree-sitter memory allocation",
            "task_id": "T-TEST-001"
        });
        seed_workspace(&store);
        let create_res = store
            .handle_task_create(peer.clone(), &create_params)
            .unwrap();
        assert_eq!(create_res["task_id"], "T-TEST-001");
        assert_eq!(create_res["status"], "open");

        // 3. Task Claim
        let claim_params = serde_json::json!({
            "task_id": "T-TEST-001",
            "agent_session_id": "session-123"
        });
        let claim_res = store
            .handle_task_claim(peer.clone(), &claim_params)
            .unwrap();
        assert_eq!(claim_res["status"], "in_progress");
        assert_eq!(claim_res["claimed_by"], "session-123");

        // Concurrent Claim Conflict Test
        let peer2 = PeerCredential::new_unix(1001, 1001, 5678);
        let claim2_params = serde_json::json!({
            "task_id": "T-TEST-001",
            "agent_session_id": "session-456"
        });
        let claim2_err = store
            .handle_task_claim(peer2.clone(), &claim2_params)
            .unwrap_err();
        assert_eq!(claim2_err.code, "task_conflict");

        // 4. Task Report (by unauthorized peer -> expect permission_denied)
        let report_params = serde_json::json!({
            "task_id": "T-TEST-001",
            "summary": "Fixed memory leak",
            "agent_session_id": "session-456"
        });
        let report_err = store
            .handle_task_report(peer2.clone(), &report_params)
            .unwrap_err();
        assert_eq!(report_err.code, "permission_denied");

        // Task Report (by authorized peer)
        let report_valid_params = serde_json::json!({
            "task_id": "T-TEST-001",
            "summary": "Fixed memory leak",
            "agent_session_id": "session-123"
        });
        let report_res = store
            .handle_task_report(peer.clone(), &report_valid_params)
            .unwrap();
        assert_eq!(report_res["status"], "review");

        // 5. Task Events
        let events_params = serde_json::json!({ "task_id": "T-TEST-001" });
        let events_res = store
            .handle_task_events(peer.clone(), &events_params)
            .unwrap();
        let events = events_res["events"].as_array().unwrap();
        // assignment_queue 派工子系统随 create/claim/report 写入 assignment_*
        // 幂等事件；task 生命周期事件只有 created/claimed/reported 三类，其余
        // 为 assignment 派工投影，过滤后断言任务侧事件数不变。
        let lifecycle_events = events
            .iter()
            .filter(|ev| !ev["reason_code"].as_str().unwrap_or("").starts_with("assignment_"));
        assert_eq!(lifecycle_events.count(), 3); // created, claimed, reported
    }

    #[test]
    fn test_failed_report_preserves_scope_and_requires_remediation_claim() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let mut executor_identity = lease_identity("agent-remediation", "executor-session", "model", "implementer");
        executor_identity["agent_instance_id"] = Value::String("instance-remediation".into());
        seed_workspace(&store);
        register_agent_with_identity(
            &store,
            &peer,
            "agent-remediation",
            "instance-remediation",
            "executor-session",
            "implementer",
        );
        store.handle_task_create(peer.clone(), &serde_json::json!({ "workspace_id": 1,
            "task_id": "T-REMEDIATION-SCOPE",
            "title": "failed step scope",
            "steps": [{"action":"capture", "target_file":"docs/design/example.md", "target_symbol":"", "check_items":"isolated capture"}],
            "identity_policy": "legacy_identity_v1",
            "role_contracts": p0l_governance_roles()
        })).unwrap();
        let conn = store.conn.lock().unwrap();
        let failed_id: String = conn.query_row(
            "SELECT id FROM task_steps WHERE task_id='T-REMEDIATION-SCOPE' ORDER BY step_index LIMIT 1", [], |r| r.get(0)
        ).unwrap();
        drop(conn);
        store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id":"T-REMEDIATION-SCOPE", "agent_session_id":"executor-session",
                    "identity": executor_identity.clone()
                }),
            )
            .unwrap();
        store.handle_task_report(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-SCOPE", "step_id":failed_id, "agent_session_id":"executor-session",
            "identity": executor_identity.clone(), "summary":"capture blocked", "success":false
        })).unwrap();
        let missing = store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id":"T-REMEDIATION-SCOPE", "agent_session_id":"executor-session",
                    "identity": executor_identity.clone()
                }),
            )
            .unwrap_err();
        assert_eq!(missing.code, "E_REMEDIATION_STEP_REQUIRED");
        let conn = store.conn.lock().unwrap();
        let (fix_id, target, result): (String, String, String) = conn.query_row(
            "SELECT id, target_file, result FROM task_steps WHERE task_id='T-REMEDIATION-SCOPE' AND action='fix_defect' ORDER BY step_index DESC LIMIT 1", [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))
        ).unwrap();
        let binding_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM task_step_role_contract_bindings WHERE task_id='T-REMEDIATION-SCOPE' AND step_id=?1",
            params![fix_id],
            |r| r.get(0),
        ).unwrap();
        assert_eq!(binding_count, 1, "自动 remediation 必须写入唯一 Executor Role Contract binding");
        drop(conn);
        let claim = store.handle_task_claim(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-SCOPE", "agent_session_id":"executor-session",
            "identity": executor_identity, "remediation_step_id":fix_id
        })).unwrap();
        assert_eq!(claim["step_id"], fix_id);
        assert_eq!(target, "docs/design/example.md");
        assert_eq!(
            serde_json::from_str::<Value>(&result).unwrap()["remediation_of_step_id"],
            failed_id
        );
    }

    #[test]
    fn test_explicit_remediation_create_binds_failed_step_and_resolves() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let mut ident = lease_identity("agent-explicit-remediation", "executor-session", "model", "implementer");
        ident["agent_instance_id"] = Value::String("instance-explicit-remediation".into());
        seed_workspace(&store);
        register_agent_with_identity(
            &store,
            &peer,
            "agent-explicit-remediation",
            "instance-explicit-remediation",
            "executor-session",
            "implementer",
        );
        store.handle_task_create(peer.clone(), &serde_json::json!({ "workspace_id": 1,
            "task_id":"T-REMEDIATION-EXPLICIT", "title":"explicit remediation", "steps":[
                {"action":"capture", "target_file":"docs/design/example.md", "check_items":"isolated"}
            ], "identity_policy":"legacy_identity_v1", "role_contracts":p0l_governance_roles()
        })).unwrap();
        let failed_id: String = {
            let conn = store.conn.lock().unwrap();
            conn.query_row(
                "SELECT id FROM task_steps WHERE task_id='T-REMEDIATION-EXPLICIT'",
                [],
                |r| r.get(0),
            )
            .unwrap()
        };
        store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id":"T-REMEDIATION-EXPLICIT", "agent_session_id":"executor-session",
                    "identity": ident.clone()
                }),
            )
            .unwrap();
        store.handle_task_report(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-EXPLICIT", "step_id":failed_id, "agent_session_id":"executor-session",
            "identity": ident.clone(), "summary":"capture blocked", "success":false
        })).unwrap();
        // 模拟历史任务中的 malformed remediation：旧步骤已完成，但 result
        // 不是带 remediation_of_step_id 的结构化 provenance。显式创建入口
        // 必须为同一 failed 步骤补建一个新的、可解析的 remediation。
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE task_steps SET status='done', result='legacy malformed remediation', completed_at=?1
                 WHERE task_id='T-REMEDIATION-EXPLICIT' AND action='fix_defect'",
                params![now_ts()],
            ).unwrap();
        }
        let lease = store.handle_lease_acquire(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-EXPLICIT", "role":"implementer", "ttl_seconds":3600.0, "identity":ident
        })).unwrap();
        let create_params = serde_json::json!({
            "task_id":"T-REMEDIATION-EXPLICIT", "failed_step_id":failed_id,
            "request_id":"remediation-create-1", "identity":ident,
            "lease_token":lease["token"], "fencing_counter":lease["fencing_counter"]
        });
        let created = store
            .handle_task_remediation_create(peer.clone(), &create_params)
            .unwrap();
        let remediation_id = created["remediation_step_id"].as_str().unwrap().to_string();
        let binding_count: i64 = store.conn.lock().unwrap().query_row(
            "SELECT COUNT(*) FROM task_step_role_contract_bindings WHERE task_id='T-REMEDIATION-EXPLICIT' AND step_id=?1",
            params![remediation_id],
            |r| r.get(0),
        ).unwrap();
        assert_eq!(binding_count, 1, "显式 remediation 必须写入唯一 Executor Role Contract binding");
        let replay = store
            .handle_task_remediation_create(peer.clone(), &create_params)
            .unwrap();
        assert_eq!(replay["replayed"], true);
        assert_eq!(replay["remediation_step_id"], remediation_id);
        store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id":"T-REMEDIATION-EXPLICIT", "agent_session_id":"executor-session",
                    "identity": ident.clone(), "remediation_step_id":remediation_id
                }),
            )
            .unwrap();
        let remediation_report = store
            .handle_task_report(
                peer.clone(),
                &serde_json::json!({
                    "task_id":"T-REMEDIATION-EXPLICIT", "step_id":remediation_id,
                    "agent_session_id":"executor-session", "identity": ident.clone(),
                    "summary":"fixed", "success":true
                }),
            )
            .unwrap();
        assert_eq!(
            remediation_report["status"], "in_progress",
            "没有 resolution event 时不得因 pending=0 伪造 review",
        );
        let resolve_params = serde_json::json!({
            "task_id":"T-REMEDIATION-EXPLICIT", "failed_step_id":failed_id,
            "remediation_step_id":remediation_id, "request_id":"resolution-explicit-1",
            "evidence_path":"docs/evidence/resolution-explicit.json", "evidence_hash":"sha256:explicit",
            "identity":ident, "lease_token":lease["token"], "fencing_counter":lease["fencing_counter"]
        });
        let resolved = store
            .handle_task_step_resolve(peer.clone(), &resolve_params)
            .unwrap();
        assert_eq!(resolved["status"], "review");
        let replay = store
            .handle_task_step_resolve(peer.clone(), &resolve_params)
            .unwrap();
        assert_eq!(replay["replayed"], true);
        let conn = store.conn.lock().unwrap();
        let failed_status: String = conn
            .query_row(
                "SELECT status FROM task_steps WHERE id=?1",
                params![failed_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(failed_status, "failed");
        let result: String = conn
            .query_row(
                "SELECT result FROM task_steps WHERE id=?1",
                params![remediation_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&result).unwrap()["remediation_of_step_id"],
            failed_id
        );
    }

    #[test]
    fn test_step_resolution_is_idempotent_and_keeps_failed_history_immutable() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let mut executor_identity = lease_identity("agent-resolver", "executor-session", "model", "implementer");
        executor_identity["agent_instance_id"] = Value::String("instance-resolver".into());
        seed_workspace(&store);
        register_agent_with_identity(
            &store,
            &peer,
            "agent-resolver",
            "instance-resolver",
            "executor-session",
            "implementer",
        );
        store.handle_task_create(peer.clone(), &serde_json::json!({ "workspace_id": 1,
            "task_id":"T-REMEDIATION-RESOLVE", "title":"resolution", "steps":[
                {"action":"capture", "target_file":"docs/design/example.md", "check_items":"isolated"}
            ], "identity_policy":"legacy_identity_v1", "role_contracts":p0l_governance_roles()
        })).unwrap();
        let conn = store.conn.lock().unwrap();
        let failed_id: String = conn
            .query_row(
                "SELECT id FROM task_steps WHERE task_id='T-REMEDIATION-RESOLVE'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        drop(conn);
        store.handle_task_claim(peer.clone(), &serde_json::json!({"task_id":"T-REMEDIATION-RESOLVE", "agent_session_id":"executor-session", "identity":executor_identity.clone()})).unwrap();
        store.handle_task_report(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-RESOLVE", "step_id":failed_id, "agent_session_id":"executor-session", "identity":executor_identity.clone(), "summary":"failed", "success":false
        })).unwrap();
        let conn = store.conn.lock().unwrap();
        let remediation_id: String = conn.query_row("SELECT id FROM task_steps WHERE task_id='T-REMEDIATION-RESOLVE' AND action='fix_defect'", [], |r| r.get(0)).unwrap();
        drop(conn);
        store.handle_task_claim(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-RESOLVE", "agent_session_id":"executor-session", "identity":executor_identity.clone(), "remediation_step_id":remediation_id
        })).unwrap();
        store.handle_task_report(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-RESOLVE", "step_id":remediation_id, "agent_session_id":"executor-session", "identity":executor_identity.clone(), "summary":"fixed", "success":true
        })).unwrap();
        let ident = executor_identity.clone();
        let lease = store.handle_lease_acquire(peer.clone(), &serde_json::json!({
            "task_id":"T-REMEDIATION-RESOLVE", "role":"implementer", "ttl_seconds":3600.0, "identity":ident
        })).unwrap();
        let params = serde_json::json!({
            "task_id":"T-REMEDIATION-RESOLVE", "failed_step_id":failed_id, "remediation_step_id":remediation_id,
            "request_id":"resolution-1", "evidence_path":"docs/evidence/resolution.md", "evidence_hash":"hash-1",
            "identity":ident, "lease_token":lease["token"], "fencing_counter":lease["fencing_counter"]
        });
        let resolved = store
            .handle_task_step_resolve(peer.clone(), &params)
            .unwrap();
        assert_eq!(resolved["status"], "review");
        let replay = store
            .handle_task_step_resolve(peer.clone(), &params)
            .unwrap();
        assert_eq!(replay["replayed"], true);
        let mut conflict = params.clone();
        conflict["evidence_hash"] = Value::String("hash-2".into());
        let err = store.handle_task_step_resolve(peer, &conflict).unwrap_err();
        assert_eq!(err.code, "E_REQUEST_ID_REUSE_MISMATCH");
        let conn = store.conn.lock().unwrap();
        let status: String = conn
            .query_row(
                "SELECT status FROM task_steps WHERE id=?1",
                params![failed_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(status, "failed");
    }

    #[test]
    fn test_reviewer_blocked_remediation_create_reopens_same_task_with_provenance() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store.handle_task_create(peer.clone(), &serde_json::json!({ "workspace_id": 1,
            "task_id":"T-REMEDIATION-REVIEW", "title":"review remediation", "steps":[
                {"action":"implement", "target_file":"src/review.rs", "check_items":"focused"}
            ], "identity_policy":"legacy_identity_v1", "role_contracts":p0l_governance_roles()
        })).unwrap();
        let source_step_id: String = {
            let conn = store.conn.lock().unwrap();
            conn.query_row(
                "SELECT id FROM task_steps WHERE task_id='T-REMEDIATION-REVIEW'",
                [],
                |r| r.get(0),
            )
            .unwrap()
        };
        let findings = serde_json::json!([
            {"finding_id":"F-REVIEW-1","fact":"event transition is not reconstructable"}
        ]);
        let source_result = "immutable executor delivery";
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE task_steps SET status='done', result=?1, completed_at=?2 WHERE id=?3",
                params![source_result, now_ts(), source_step_id],
            )
            .unwrap();
            conn.execute(
                "UPDATE tasks SET status='review' WHERE id='T-REMEDIATION-REVIEW'",
                [],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO task_verdict_events
                 (verdict_id, task_id, contract_id, contract_revision, contract_hash,
                  phase, reviewer_identity, findings, overall, attestation, submitted_at)
                 VALUES ('V-REVIEW-1', 'T-REMEDIATION-REVIEW', 'TC-REVIEW', 1, 'sha256:task',
                         'blind_first_pass', '{}', ?1, 'block', 'attested', ?2)",
                params![findings.to_string(), now_ts()],
            )
            .unwrap();
        }
        let executor_identity = lease_identity(
            "agent-remediation-executor",
            "executor-remediation-session",
            "executor-model",
            "implementer",
        );
        let lease = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id":"T-REMEDIATION-REVIEW", "role":"implementer",
                    "ttl_seconds":3600.0, "identity":executor_identity
                }),
            )
            .unwrap();
        let create = serde_json::json!({
            "task_id":"T-REMEDIATION-REVIEW",
            "source_step_id":source_step_id,
            "source_outcome":"reviewer_blocked",
            "source_verdict_id":"V-REVIEW-1",
            "source_findings":findings,
            "request_id":"review-remediation-1",
            "identity":executor_identity,
            "lease_token":lease["token"],
            "fencing_counter":lease["fencing_counter"]
        });
        let created = store
            .handle_task_remediation_create(peer.clone(), &create)
            .unwrap();
        assert_eq!(created["source_outcome"], "reviewer_blocked");
        assert_eq!(created["source_verdict_id"], "V-REVIEW-1");
        let remediation_step_id = created["remediation_step_id"].as_str().unwrap();

        let replay = store
            .handle_task_remediation_create(peer.clone(), &create)
            .unwrap();
        assert_eq!(replay["replayed"], true);
        assert_eq!(replay["remediation_step_id"], remediation_step_id);
        let mut conflict = create.clone();
        conflict["source_findings"] = serde_json::json!([
            {"finding_id":"F-REVIEW-CHANGED","fact":"different params"}
        ]);
        let conflict_err = store
            .handle_task_remediation_create(peer, &conflict)
            .unwrap_err();
        assert_eq!(conflict_err.code, "E_REQUEST_ID_REUSE_MISMATCH");

        let conn = store.conn.lock().unwrap();
        let task_status: String = conn
            .query_row(
                "SELECT status FROM tasks WHERE id='T-REMEDIATION-REVIEW'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(task_status, "in_progress");
        let (source_status, result_after): (String, String) = conn
            .query_row(
                "SELECT status, result FROM task_steps WHERE id=?1",
                params![source_step_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(source_status, "done");
        assert_eq!(result_after, source_result);
        let metadata_raw: String = conn
            .query_row(
                "SELECT result FROM task_steps WHERE id=?1",
                params![remediation_step_id],
                |r| r.get(0),
            )
            .unwrap();
        let metadata: Value = serde_json::from_str(&metadata_raw).unwrap();
        assert_eq!(metadata["remediation_of_step_id"], source_step_id);
        assert_eq!(metadata["source_verdict_id"], "V-REVIEW-1");
        assert_eq!(metadata["source_findings"], findings);
        let (from_status, to_status): (String, String) = conn
            .query_row(
                "SELECT from_status, to_status FROM task_events
             WHERE task_id='T-REMEDIATION-REVIEW' AND reason_code='remediation_created'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(
            (from_status.as_str(), to_status.as_str()),
            ("review", "in_progress")
        );
        let verdict_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_verdict_events WHERE task_id='T-REMEDIATION-REVIEW'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(verdict_count, 1);
        let child_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM tasks WHERE parent_id='T-REMEDIATION-REVIEW'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(child_count, 0);
    }

    #[test]
    fn test_task_level_reviewer_blocked_handoff_creates_fix_defect() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let task_id = "T-REVIEWER-BLOCKED-TASK-LEVEL";
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": task_id,
                    "title": "task-level reviewer block",
                    "description": "a review can reject a task without a source step",
                    "identity_policy": "legacy_identity_v1",
                    "role_contracts": p0l_governance_roles()
                }),
            )
            .unwrap();

        let reviewer_identity = lease_identity(
            "agent-task-level-reviewer",
            "reviewer-task-level-session",
            "reviewer-model",
            "reviewer",
        );
        let findings = serde_json::json!([
            {"finding_id": "F-TASK-LEVEL-1", "fact": "task-level governance evidence is incomplete"}
        ]);
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE tasks SET status='review' WHERE id=?1",
                params![task_id],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO task_verdict_events
                (verdict_id, task_id, contract_id, contract_revision, contract_hash,
                  phase, step_id, snapshot_id, view_manifest_hash, reviewer_identity,
                  findings, overall, attestation, submitted_at, workspace_id)
                 VALUES ('V-TASK-LEVEL-1', ?1, 'TC-TASK-LEVEL', 1, 'sha256:task-level',
                         'blind_first_pass', '', '', 'manifest-task-level',
                         ?2, ?3, 'block', 'attested', ?4, 1)",
                params![
                    task_id,
                    serde_json::json!({"identity": reviewer_identity.clone()}).to_string(),
                    findings.to_string(),
                    now_ts(),
                ],
            )
            .unwrap();
        }
        let reviewer_lease = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": task_id,
                    "role": "reviewer",
                    "ttl_seconds": 3600.0,
                    "identity": reviewer_identity.clone()
                }),
            )
            .unwrap();
        let handoff = serde_json::json!({
            "task_id": task_id,
            "from_role": "reviewer",
            "outcome": "reviewer_blocked",
            "next_role": "executor",
            "next_action": "修复 task-level finding",
            "reason": "F-TASK-LEVEL-1",
            "independence_requirement": "not_required",
            "request_id": "handoff-task-level-1",
            "step_id": null,
            "report_request_id": "report-task-level-1",
            "evidence_path": "docs/evidence/task-level-1.json",
            "evidence_hash": "sha256:task-level-1",
            "identity": reviewer_identity,
            "lease_token": reviewer_lease["token"],
            "fencing_counter": reviewer_lease["fencing_counter"]
        });
        let missing_snapshot = store
            .handle_task_handoff(peer.clone(), &handoff)
            .expect_err("缺失 review snapshot 必须拒绝 remediation");
        assert_eq!(missing_snapshot.code, "E_REMEDIATION_REVIEW_PROVENANCE_REQUIRED");
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE task_verdict_events SET snapshot_id='snapshot-task-level'
                 WHERE verdict_id='V-TASK-LEVEL-1' AND task_id=?1",
                params![task_id],
            )
            .unwrap();
        }
        let response = store
            .handle_task_handoff(peer, &handoff)
            .expect("task-level reviewer_blocked 应原子创建 remediation");
        assert_eq!(response["status"], "in_progress");
        let remediation_step_id = response["remediation_step_id"]
            .as_str()
            .expect("应返回 remediation_step_id");

        let conn = store.conn.lock().unwrap();
        let binding_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_step_role_contract_bindings WHERE task_id=?1 AND step_id=?2",
                params![task_id, remediation_step_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(binding_count, 1, "task-level remediation 必须写入唯一 Executor Role Contract binding");
        let task_status: String = conn
            .query_row(
                "SELECT status FROM tasks WHERE id=?1",
                params![task_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(task_status, "in_progress");
        let (action, target_file, target_symbol, check_items, result): (
            String,
            String,
            String,
            String,
            String,
        ) = conn
            .query_row(
                "SELECT action, target_file, target_symbol, check_items, result
                 FROM task_steps WHERE id=?1 AND task_id=?2",
                params![remediation_step_id, task_id],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                    ))
                },
            )
            .unwrap();
        assert_eq!(action, "fix_defect");
        assert!(target_file.is_empty());
        assert!(target_symbol.is_empty());
        assert!(check_items.is_empty());
        let metadata: Value = serde_json::from_str(&result).unwrap();
        assert!(metadata["remediation_of_step_id"].is_null());
        assert_eq!(metadata["source_outcome"], "reviewer_blocked");
        assert_eq!(metadata["source_verdict_id"], "V-TASK-LEVEL-1");
        assert_eq!(metadata["source_findings"], findings);
        let handoff_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events
                 WHERE task_id=?1 AND reason_code='handoff_structured'",
                params![task_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(handoff_count, 1);
        let handoff_envelope: Value = conn
            .query_row(
                "SELECT reason FROM task_events
                 WHERE task_id=?1 AND reason_code='handoff_structured'
                 ORDER BY event_id DESC LIMIT 1",
                params![task_id],
                |row| {
                    let reason: String = row.get(0)?;
                    Ok(serde_json::from_str(&reason).unwrap())
                },
            )
            .unwrap();
        assert_eq!(handoff_envelope["target_role"], "executor");
        assert_eq!(handoff_envelope["next_role"], "executor");
    }

    #[test]
    fn test_adjudicator_returned_handoff_reopens_executor_remediation_atomically() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let task_id = "T-ADJUDICATOR-RETURNED-REMEDIATION";
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": task_id,
                    "title": "adjudicator returned remediation",
                    "steps": [{
                        "action": "deploy_and_round_trip",
                        "target_file": "scripts/refresh_shared_runtime.ps1",
                        "target_symbol": "",
                        "check_items": "fresh PID and binary hash"
                    }],
                    "identity_policy": "legacy_identity_v1",
                    "role_contracts": p0l_governance_roles()
                }),
            )
            .unwrap();

        let reviewer_identity = lease_identity(
            "agent-adjudicator-returned-reviewer",
            "reviewer-returned-session",
            "reviewer-model",
            "reviewer",
        );
        let adjudicator_identity = lease_identity(
            "agent-adjudicator-returned-adjudicator",
            "adjudicator-returned-session",
            "adjudicator-model",
            "adjudicator",
        );
        register_agent_with_identity(
            &store,
            &peer,
            "agent-adjudicator-returned-adjudicator",
            "adjudicator-returned-instance",
            "adjudicator-returned-session",
            "adjudicator",
        );

        let source_step_id: String = {
            let conn = store.conn.lock().unwrap();
            conn.query_row(
                "SELECT id FROM task_steps WHERE task_id=?1 ORDER BY step_index LIMIT 1",
                params![task_id],
                |row| row.get(0),
            )
            .unwrap()
        };
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE tasks SET status='review' WHERE id=?1",
                params![task_id],
            )
            .unwrap();
            conn.execute(
                "UPDATE task_steps SET status='done', result=?2 WHERE id=?1",
                params![source_step_id, serde_json::json!({"success": true}).to_string()],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO task_verdict_events
                (verdict_id, task_id, contract_id, contract_revision, contract_hash,
                  phase, step_id, snapshot_id, view_manifest_hash, reviewer_identity,
                  findings, overall, attestation, submitted_at, workspace_id)
                 VALUES ('V-ADJ-RETURNED-1', ?1, 'TC-ADJ-RETURNED', 1, 'sha256:adj-returned',
                         'adjudication', ?2, 'snapshot-adj-returned', 'manifest-adj-returned',
                         ?3, ?4, 'pass', 'attested', ?5, 1)",
                params![
                    task_id,
                    source_step_id,
                    serde_json::json!({"identity": reviewer_identity}).to_string(),
                    serde_json::json!([{"finding_id":"F-ADJ-RETURNED-1","fact":"deployment evidence is stale"}]).to_string(),
                    now_ts(),
                ],
            )
            .unwrap();
        }
        // 模拟旧投影中同时残留的 Executor/Reviewer/Adjudicator assignment；正式
        // handoff 必须原子完成它们，再只排入 remediation Executor。
        {
            let mut conn = store.conn.lock().unwrap();
            let tx = conn.unchecked_transaction().unwrap();
            assignment_queue::queue_assignment(
                &tx,
                task_id,
                Some(&source_step_id),
                "reviewer",
                "stale-reviewer-source",
                None,
                "test",
                "test-reviewer",
                store.next_seq(),
                now_ts(),
            )
            .unwrap();
            assignment_queue::queue_assignment(
                &tx,
                task_id,
                None,
                "reviewer",
                "stale-reviewer-task",
                None,
                "test",
                "test-reviewer",
                store.next_seq(),
                now_ts(),
            )
            .unwrap();
            assignment_queue::queue_assignment(
                &tx,
                task_id,
                Some(&source_step_id),
                "adjudicator",
                "stale-adjudicator-source",
                None,
                "test",
                "test-adjudicator",
                store.next_seq(),
                now_ts(),
            )
            .unwrap();
            tx.commit().unwrap();
        }

        let adjudicator_lease = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": task_id,
                    "role": "adjudicator",
                    "ttl_seconds": 3600.0,
                    "identity": adjudicator_identity.clone()
                }),
            )
            .unwrap();
        let handoff = serde_json::json!({
            "task_id": task_id,
            "from_role": "adjudicator",
            "outcome": "adjudicator_returned",
            "next_role": "executor",
            "next_action": "claim deployment evidence remediation",
            "reason": "fresh deployment evidence is required",
            "independence_requirement": "not_required",
            "request_id": "handoff-adjudicator-returned-1",
            "step_id": source_step_id,
            "report_request_id": "report-adjudicator-returned-1",
            "evidence_path": "docs/evidence/adjudicator-returned-1.json",
            "evidence_hash": "sha256:adjudicator-returned-1",
            "identity": adjudicator_identity,
            "lease_token": adjudicator_lease["token"],
            "fencing_counter": adjudicator_lease["fencing_counter"]
        });
        let response = store
            .handle_task_handoff(peer.clone(), &handoff)
            .expect("adjudicator_returned 应原子创建 Executor remediation");
        assert_eq!(response["status"], "in_progress");
        let remediation_step_id = response["remediation_step_id"].as_str().unwrap();
        assert!(response["assignment_id"].as_str().is_some());

        let replay = store
            .handle_task_handoff(peer.clone(), &handoff)
            .expect("相同 request_id 必须幂等重放");
        assert_eq!(replay["replayed"], true);
        assert_eq!(replay["remediation_step_id"], remediation_step_id);

        let conn = store.conn.lock().unwrap();
        let (action, status, target_file, result): (String, String, String, String) = conn
            .query_row(
                "SELECT action, status, target_file, result FROM task_steps WHERE id=?1",
                params![remediation_step_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert_eq!(action, "fix_defect");
        assert_eq!(status, "pending");
        assert_eq!(target_file, "scripts/refresh_shared_runtime.ps1");
        let metadata: Value = serde_json::from_str(&result).unwrap();
        assert_eq!(metadata["source_outcome"], "adjudicator_returned");
        assert_eq!(metadata["source_verdict_id"], "V-ADJ-RETURNED-1");
        assert_eq!(metadata["remediation_of_step_id"], source_step_id);
        let active: Vec<_> = assignment_queue::project_task_assignments(&conn, task_id)
            .unwrap()
            .into_iter()
            .filter(|assignment| assignment.is_active())
            .collect();
        assert_eq!(active.len(), 1);
        assert_eq!(active[0].role, "executor");
        assert_eq!(active[0].step_id.as_deref(), Some(remediation_step_id));
    }

    #[test]
    fn test_reviewer_blocked_reopens_same_task_for_multiple_revision_rounds() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let mut executor_identity = lease_identity(
            "agent-thread-executor",
            "executor-session",
            "executor-model",
            "executor",
        );
        executor_identity["agent_instance_id"] = Value::String("instance-thread-executor".into());
        register_agent_with_identity(
            &store,
            &peer,
            "agent-thread-executor",
            "instance-thread-executor",
            "executor-session",
            "executor",
        );
        store.handle_task_create(peer.clone(), &serde_json::json!({ "workspace_id": 1,
            "task_id":"T-THREAD-REVISE", "title":"thread revision", "steps":[
                {"action":"implement", "target_file":"src/thread.rs", "check_items":"focused"}
            ], "identity_policy":"legacy_identity_v1", "role_contracts":p0l_governance_roles()
        })).unwrap();
        let source_step_id: String = {
            let conn = store.conn.lock().unwrap();
            conn.query_row(
                "SELECT id FROM task_steps WHERE task_id='T-THREAD-REVISE'",
                [],
                |row| row.get(0),
            )
            .unwrap()
        };
        let reviewer_identity = lease_identity(
            "agent-thread-reviewer",
            "reviewer-session",
            "reviewer-model",
            "reviewer",
        );
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE task_steps SET status='done', result='executor delivery', completed_at=?1
                 WHERE id=?2",
                params![now_ts(), source_step_id],
            )
            .unwrap();
            conn.execute(
                "UPDATE tasks SET status='review' WHERE id='T-THREAD-REVISE'",
                [],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO task_verdict_events
                (verdict_id, task_id, contract_id, contract_revision, contract_hash,
                  phase, step_id, snapshot_id, view_manifest_hash, reviewer_identity,
                  findings, overall, attestation, submitted_at, workspace_id)
                 VALUES ('V-THREAD-1', 'T-THREAD-REVISE', 'TC-THREAD', 1, 'sha256:task',
                         'blind_first_pass', ?1, 'snapshot-thread-1', 'manifest-thread-1',
                         ?2, ?3, 'block', 'attested', ?4, 1)",
                params![
                    source_step_id,
                    serde_json::json!({"identity": reviewer_identity.clone()}).to_string(),
                    serde_json::json!([{"finding_id":"F-THREAD-1","fact":"first defect"}])
                        .to_string(),
                    now_ts(),
                ],
            )
            .unwrap();
        }
        let reviewer_lease = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id":"T-THREAD-REVISE", "role":"reviewer", "ttl_seconds":3600.0,
                    "identity": reviewer_identity.clone()
                }),
            )
            .unwrap();
        let first_handoff = serde_json::json!({
            "task_id":"T-THREAD-REVISE", "from_role":"reviewer",
            "outcome":"reviewer_blocked", "next_role":"executor",
            "next_action":"revise finding F-THREAD-1", "reason":"F-THREAD-1",
            "independence_requirement":"not_required", "request_id":"handoff-thread-1",
            "step_id":source_step_id, "report_request_id":"report-thread-1",
            "evidence_path":"docs/evidence/thread-1.json", "evidence_hash":"sha256:thread-1",
            "identity":reviewer_identity.clone(), "lease_token":reviewer_lease["token"],
            "fencing_counter":reviewer_lease["fencing_counter"]
        });
        let first = store
            .handle_task_handoff(peer.clone(), &first_handoff)
            .unwrap();
        assert_eq!(first["status"], "in_progress");
        let remediation_one = first["remediation_step_id"].as_str().unwrap().to_string();
        let replay = store
            .handle_task_handoff(peer.clone(), &first_handoff)
            .unwrap();
        assert_eq!(replay["replayed"], true);
        assert_eq!(replay["remediation_step_id"], remediation_one);

        let claim = store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id":"T-THREAD-REVISE", "agent_session_id":"executor-session",
                    "remediation_step_id":remediation_one, "identity":executor_identity.clone()
                }),
            )
            .unwrap();
        assert_eq!(claim["step_id"], remediation_one);
        let report = store.handle_task_report(peer.clone(), &serde_json::json!({
            "task_id":"T-THREAD-REVISE", "step_id":remediation_one,
            "agent_session_id":"executor-session", "identity":executor_identity,
            "summary":"first revision done", "success":true
        })).unwrap();
        assert_eq!(report["status"], "review");

        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "INSERT INTO task_verdict_events
                (verdict_id, task_id, contract_id, contract_revision, contract_hash,
                  phase, step_id, snapshot_id, view_manifest_hash, reviewer_identity,
                  findings, overall, attestation, submitted_at, workspace_id)
                 VALUES ('V-THREAD-2', 'T-THREAD-REVISE', 'TC-THREAD', 1, 'sha256:task',
                         'post_reveal_amendment', ?1, 'snapshot-thread-2', 'manifest-thread-2',
                         ?2, ?3, 'block', 'attested', ?4, 1)",
                params![
                    remediation_one,
                    serde_json::json!({"identity": reviewer_identity.clone()}).to_string(),
                    serde_json::json!([{"finding_id":"F-THREAD-2","fact":"second defect"}])
                        .to_string(),
                    now_ts(),
                ],
            )
            .unwrap();
        }
        let second_handoff = serde_json::json!({
            "task_id":"T-THREAD-REVISE", "from_role":"reviewer",
            "outcome":"reviewer_blocked", "next_role":"executor",
            "next_action":"revise finding F-THREAD-2", "reason":"F-THREAD-2",
            "independence_requirement":"not_required", "request_id":"handoff-thread-2",
            "step_id":remediation_one, "report_request_id":"report-thread-2",
            "evidence_path":"docs/evidence/thread-2.json", "evidence_hash":"sha256:thread-2",
            "identity":reviewer_identity, "lease_token":reviewer_lease["token"],
            "fencing_counter":reviewer_lease["fencing_counter"]
        });
        let second = store.handle_task_handoff(peer, &second_handoff).unwrap();
        let remediation_two = second["remediation_step_id"].as_str().unwrap();
        assert_ne!(remediation_two, remediation_one);

        let conn = store.conn.lock().unwrap();
        let source_state: (String, String) = conn
            .query_row(
                "SELECT status, result FROM task_steps WHERE id=?1",
                params![source_step_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(
            source_state,
            ("done".to_string(), "executor delivery".to_string())
        );
        let verdict_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_verdict_events WHERE task_id='T-THREAD-REVISE'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let remediation_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps
             WHERE task_id='T-THREAD-REVISE' AND action='fix_defect'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let child_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM tasks WHERE parent_id='T-THREAD-REVISE'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(verdict_count, 2);
        assert_eq!(remediation_count, 2);
        assert_eq!(child_count, 0);
    }

    #[test]
    fn test_task_create_persists_steps_atomically() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let create_params = serde_json::json!({
            "workspace_id": 1,
            "task_id": "T-STEPS-CREATE",
            "title": "task with steps",
            "steps": [
                {
                    "action": "implement",
                    "target_file": "rust_ext/src/daemon/task_collab.rs",
                    "target_symbol": "TaskCollabStore::handle_task_create",
                    "check_items": ["cargo test", "audit"],
                },
                {
                    "action": "test",
                    "target_file": "tests/test_task_split_steps.py",
                    "check_items": "pytest",
                },
            ]
        });

        seed_workspace(&store);
        let result = store.handle_task_create(peer, &create_params).unwrap();
        assert_eq!(result["step_count"], 2);

        let conn = store.conn.lock().unwrap();
        let mut stmt = conn
            .prepare(
                "SELECT step_index, action, target_file, target_symbol, check_items, status
                 FROM task_steps WHERE task_id = ?1 ORDER BY step_index",
            )
            .unwrap();
        let rows: Vec<(i64, String, String, String, String, String)> = stmt
            .query_map(params!["T-STEPS-CREATE"], |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                ))
            })
            .unwrap()
            .map(|row| row.unwrap())
            .collect();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].0, 0);
        assert_eq!(rows[0].1, "implement");
        assert_eq!(rows[0].2, "rust_ext/src/daemon/task_collab.rs");
        assert_eq!(rows[0].3, "TaskCollabStore::handle_task_create");
        assert_eq!(rows[0].4, "[\"cargo test\",\"audit\"]");
        assert_eq!(rows[0].5, "pending");
        assert_eq!(rows[1].0, 1);
        assert_eq!(rows[1].1, "test");
        assert_eq!(rows[1].2, "tests/test_task_split_steps.py");
        assert_eq!(rows[1].4, "pytest");
    }

    #[test]
    fn test_task_split_persists_child_steps() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-STEPS-SPLIT",
                    "title": "parent",
                }),
            )
            .unwrap();

        let split = store
            .handle_task_split(
                peer,
                &serde_json::json!({
                    "task_id": "T-STEPS-SPLIT",
                    "subtasks": [
                        {
                            "title": "bridge",
                            "description": "bridge implementation",
                            "steps": [
                                {"action": "implement", "target_file": "rust_ext/src/bin/cw_bridge.rs"},
                                {"action": "test", "target_file": "tests/test_windows_bridge_e2e.py"},
                            ]
                        },
                        {
                            "title": "routing",
                            "steps": [
                                {"action": "implement", "target_file": "server/daemon_client.py"}
                            ]
                        }
                    ]
                }),
            )
            .unwrap();
        assert_eq!(split["subtask_count"], 2);

        let conn = store.conn.lock().unwrap();
        let child_one: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE task_id = 'T-STEPS-SPLIT-sub-1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let child_two_target: String = conn
            .query_row(
                "SELECT target_file FROM task_steps WHERE task_id = 'T-STEPS-SPLIT-sub-2'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(child_one, 2);
        assert_eq!(child_two_target, "server/daemon_client.py");
    }

    #[test]
    fn test_task_status_tree_shows_pending_child_steps() {
        // 回归：status_tree 必须显示 pending 子任务的完整步骤。
        // 根因1：step_id 被按 i64 读取（实际是 TEXT）→ 行转换失败被 flatten 丢弃；
        // 根因2：completed_at 为 NULL（pending 步骤）时按 f64 读取失败 → 整行被丢弃。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-TREE-ROOT",
                    "title": "parent",
                }),
            )
            .unwrap();

        let plan_file = _dir.path().join("plan.md");
        std::fs::write(
            &plan_file,
            r#"## 子任务丙
- implement @ rust_ext/src/daemon/task_collab.rs
- test @ tests/test_task_split_steps.py
- verify @ cli/main.py
"#,
        )
        .unwrap();
        store
            .handle_task_split(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-TREE-ROOT",
                    "plan_file": plan_file.to_str().unwrap(),
                }),
            )
            .unwrap();

        let node = store
            .handle_task_status_tree(peer, &serde_json::json!({"task_id": "T-TREE-ROOT-sub-1"}))
            .unwrap();
        let steps = node["steps"].as_array().unwrap();
        assert_eq!(
            steps.len(),
            3,
            "status_tree 必须显示 pending 子任务的 3 个步骤"
        );
        assert_eq!(steps[0]["step_index"], 0);
        assert_eq!(steps[0]["action"], "implement");
        assert_eq!(steps[0]["completed_at"], Value::Null);
        assert_eq!(node["progress"]["total"], 3);
        assert_eq!(node["progress"]["ratio"], serde_json::json!(0.0));
        assert_eq!(node["progress"]["percent"], serde_json::json!(0.0));
        assert_eq!(node["lifecycle_status"], serde_json::json!("open"));
        assert_eq!(
            node["workflow_status"],
            node["governance"]["workflow_status"]
        );
    }

    #[test]
    fn test_parse_subtasks_from_plan_text_extracts_steps() {
        // S2/S3：plan_file 解析必须产出与 subtasks 参数路径一致的步骤结构
        let plan = r#"# 根计划

## 子任务一
- implement @ rust_ext/src/daemon/task_collab.rs
- test: tests/test_task_split_steps.py

## 子任务二
编写路由逻辑
- implement @ server/daemon_client.py
"#;
        let parsed = parse_subtasks_from_plan_text(plan);
        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed[0].0, "子任务一");
        assert_eq!(parsed[0].1, "");
        assert_eq!(parsed[0].2.len(), 2);
        assert_eq!(parsed[0].2[0]["action"], "implement");
        assert_eq!(
            parsed[0].2[0]["target_file"],
            "rust_ext/src/daemon/task_collab.rs"
        );
        assert_eq!(parsed[0].2[1]["action"], "test");
        assert_eq!(
            parsed[0].2[1]["target_file"],
            "tests/test_task_split_steps.py"
        );
        // 子任务二描述 + 步骤
        assert_eq!(parsed[1].0, "子任务二");
        assert_eq!(parsed[1].1, "编写路由逻辑");
        assert_eq!(parsed[1].2.len(), 1);
        assert_eq!(parsed[1].2[0]["action"], "implement");
        assert_eq!(parsed[1].2[0]["target_file"], "server/daemon_client.py");
    }

    #[test]
    fn test_parse_subtasks_from_plan_text_skips_code_blocks() {
        // 代码块内的 "- " 列表不得被解析为步骤
        let plan = "## 子任务\n```yaml\n- 不属于步骤\n```\n- 属于步骤\n";
        let parsed = parse_subtasks_from_plan_text(plan);
        assert_eq!(parsed.len(), 1);
        assert_eq!(parsed[0].2.len(), 1);
        assert_eq!(parsed[0].2[0]["action"], "属于步骤");
    }

    #[test]
    fn test_task_split_plan_file_persists_child_steps() {
        // S1：plan_file 路径必须调用 insert_task_steps，步骤完整写入且不互相串
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-PLAN-SPLIT",
                    "title": "parent",
                }),
            )
            .unwrap();

        // 写入临时 plan 文件
        let plan_file = _dir.path().join("plan.md");
        std::fs::write(
            &plan_file,
            r#"# 计划

## 子任务甲
- implement @ rust_ext/src/daemon/task_collab.rs
- test @ tests/test_task_split_steps.py

## 子任务乙
- implement @ server/daemon_client.py
"#,
        )
        .unwrap();

        let split = store
            .handle_task_split(
                peer,
                &serde_json::json!({
                    "task_id": "T-PLAN-SPLIT",
                    "plan_file": plan_file.to_str().unwrap(),
                }),
            )
            .unwrap();
        assert_eq!(split["subtask_count"], 2);

        let conn = store.conn.lock().unwrap();
        // 子任务甲：2 步，字段与顺序一致
        let mut stmt = conn
            .prepare(
                "SELECT step_index, action, target_file, target_symbol, check_items, status
                 FROM task_steps WHERE task_id = 'T-PLAN-SPLIT-sub-1' ORDER BY step_index",
            )
            .unwrap();
        let rows: Vec<(i64, String, String, String, String, String)> = stmt
            .query_map([], |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                ))
            })
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(
            rows[0],
            (
                0,
                "implement".into(),
                "rust_ext/src/daemon/task_collab.rs".into(),
                String::new(),
                String::new(),
                "pending".into()
            )
        );
        assert_eq!(
            rows[1],
            (
                1,
                "test".into(),
                "tests/test_task_split_steps.py".into(),
                String::new(),
                String::new(),
                "pending".into()
            )
        );

        // 子任务乙：1 步，不与子任务甲串步骤
        let child_two: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE task_id = 'T-PLAN-SPLIT-sub-2'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(child_two, 1);
        let child_two_target: String = conn
            .query_row(
                "SELECT target_file FROM task_steps WHERE task_id = 'T-PLAN-SPLIT-sub-2'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(child_two_target, "server/daemon_client.py");
    }

    #[test]
    fn test_task_split_plan_file_invalid_step_rolls_back() {
        // S1：步骤非法时整个事务回滚，不留下半成品子任务
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-PLAN-ROLLBACK",
                    "title": "parent",
                }),
            )
            .unwrap();

        let plan_file = _dir.path().join("plan.md");
        std::fs::write(&plan_file, "## 子任务甲\n- implement @ a.rs\n").unwrap();

        // subtasks 参数路径下步骤为非法值（非 object）应整体回滚：
        // 通过同时传 plan_file 与 subtasks（subtasks 优先）验证回滚语义
        let err = store
            .handle_task_split(
                peer,
                &serde_json::json!({
                    "task_id": "T-PLAN-ROLLBACK",
                    "plan_file": plan_file.to_str().unwrap(),
                    "subtasks": [
                        {"title": "x", "steps": ["not-an-object"]}
                    ]
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "invalid_params");

        let conn = store.conn.lock().unwrap();
        let tasks: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM tasks WHERE parent_id = 'T-PLAN-ROLLBACK'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let steps: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE task_id LIKE 'T-PLAN-ROLLBACK-sub-%'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(tasks, 0);
        assert_eq!(steps, 0);
    }

    #[test]
    fn test_task_collab_migrates_v46_db_to_v50() {
        // P1 修复：v46 旧库（无 task_events/agent_registrations、schema_version=46）
        // 打开后必须走官方 migration 升级到 v50 并补齐权威任务表，完整 task RPC 可用
        let (_dir, db_path) = temp_db();

        // 1. 先建一个 v50 库，再人为降级为 v46（模拟旧版库形态）
        {
            let store = TaskCollabStore::new(&db_path).unwrap();
            drop(store);
        }
        {
            let conn = Connection::open(&db_path).unwrap();
            conn.execute_batch(
                "DROP TABLE IF EXISTS task_events;
                 DROP TABLE IF EXISTS agent_registrations;
                 DROP INDEX IF EXISTS idx_task_events_task;
                 UPDATE schema_version SET version = 46 WHERE version >= 47;",
            )
            .unwrap();
            let v: i64 = conn
                .query_row(
                    "SELECT COALESCE(MAX(version),0) FROM schema_version",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(v, 46);
        }

        // 2. 用真实 store 打开迁移后的库，验证完整 task RPC 可用
        let store = TaskCollabStore::new(&db_path).unwrap();

        // 3. 校验实际 schema version == 47（不再依赖编译时常量，读真实 schema_version 表）
        let conn = Connection::open(&db_path).unwrap();
        let v: i64 = conn
            .query_row(
                "SELECT COALESCE(MAX(version),0) FROM schema_version",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(v, RUST_SCHEMA_VERSION);
        // 4. 权威任务表已被官方 migration 补齐
        for table in TASK_COLLAB_TABLES {
            let present: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?1",
                    params![table],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(present, 1, "权威表 {} 未补齐", table);
        }

        // 5. 完整 task RPC 可用（创建 → 查询）
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let create_params = serde_json::json!({
            "workspace_id": 1,
            "title": "upgrade from v46",
            "task_id": "T-V46-001"
        });
        seed_workspace(&store);
        let create_res = store
            .handle_task_create(peer.clone(), &create_params)
            .unwrap();
        assert_eq!(create_res["status"], "open");
        let events_params = serde_json::json!({ "task_id": "T-V46-001" });
        let events_res = store.handle_task_events(peer, &events_params).unwrap();
        // 版本迁移后任务可用；assignment_queue 额外写入 assignment_queued 事件，
        // 过滤后仍是 create 产生的唯一 task 生命周期事件。
        let lifecycle_events = events_res["events"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|ev| !ev["reason_code"].as_str().unwrap_or("").starts_with("assignment_"));
        assert_eq!(lifecycle_events.count(), 1);
    }

    #[test]
    fn test_task_report_identity_is_validated_and_persisted() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);

        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "INSERT INTO workspaces (name, root_path, created_at, is_active) VALUES ('identity-test', '/tmp/identity-test', 1.0, 1)",
                [],
            ).unwrap();
        }

        let create = serde_json::json!({
            "workspace_id": 1,
            "task_id": "T-IDENTITY-001",
            "title": "identity writeback",
            "steps": [{"action": "implement"}]
        });
        seed_workspace(&store);
        store.handle_task_create(peer.clone(), &create).unwrap();
        let step_id: String = {
            let conn = store.conn.lock().unwrap();
            conn.query_row(
                "SELECT id FROM task_steps WHERE task_id = 'T-IDENTITY-001' ORDER BY step_index LIMIT 1",
                [],
                |r| r.get(0),
            )
            .unwrap()
        };
        {
            let conn = store.conn.lock().unwrap();
            let step_count: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM task_steps WHERE task_id = 'T-IDENTITY-001'",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(step_count, 1);
        }
        store.handle_task_claim(
            peer.clone(),
            &serde_json::json!({"task_id": "T-IDENTITY-001", "agent_session_id": "session-identity"}),
        ).unwrap();

        let report = store
            .handle_task_report(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-IDENTITY-001",
                    "step_id": step_id,
                    "summary": "done",
                    "success": true,
                    "identity": {
                        "agent_id": "agent-identity",
                        "session_id": "session-identity",
                        "model_id": "model-test",
                        "role": "implementer"
                    }
                }),
            )
            .unwrap();
        assert_eq!(report["status"], "review");

        let conn = store.conn.lock().unwrap();
        let step_status: String = conn
            .query_row(
                "SELECT status FROM task_steps WHERE id = ?1",
                params![step_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(step_status, "done");
        let identity_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM action_identities WHERE task_id = 'T-IDENTITY-001' AND agent_id = 'agent-identity'",
            [], |r| r.get(0),
        ).unwrap();
        assert_eq!(identity_count, 1);
        let role: String = conn.query_row(
            "SELECT role FROM task_events WHERE task_id = 'T-IDENTITY-001' AND reason_code = 'reported' ORDER BY event_id DESC LIMIT 1",
            [], |r| r.get(0),
        ).unwrap();
        assert_eq!(role, "implementer");

        let err = store
            .handle_task_report(
                peer,
                &serde_json::json!({
                    "task_id": "T-IDENTITY-001",
                    "identity": {"agent_id": "agent-only"}
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_IDENTITY_INCOMPLETE");
    }

    #[test]
    fn test_task_report_persists_snapshot_for_governance_projection() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);

        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": "T-REPORT-SNAPSHOT-001",
                    "title": "report snapshot binding",
                    "steps": [{"action": "implement", "target_file": "a.rs"}]
                }),
            )
            .unwrap();
        let step_id: String = {
            let conn = store.conn.lock().unwrap();
            conn.query_row(
                "SELECT id FROM task_steps WHERE task_id = 'T-REPORT-SNAPSHOT-001'",
                [],
                |r| r.get(0),
            )
            .unwrap()
        };
        store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({"task_id": "T-REPORT-SNAPSHOT-001"}),
            )
            .unwrap();
        store
            .handle_task_report(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-REPORT-SNAPSHOT-001",
                    "step_id": step_id,
                    "summary": "reported with authoritative snapshot",
                    "snapshot_id": "snapshot-report-001",
                    "evidence_path": "deliverables/report.md",
                    "success": true
                }),
            )
            .unwrap();

        let projection = store
            .handle_task_governance_projection_get(
                peer,
                &serde_json::json!({"task_id": "T-REPORT-SNAPSHOT-001"}),
            )
            .unwrap();
        assert_eq!(
            projection["review_input_snapshot"]["snapshot_id"],
            "snapshot-report-001"
        );
        assert_eq!(
            projection["review_input_snapshot"]["evidence_path"],
            "deliverables/report.md"
        );
    }

    // ============================================================
    // P0-L reviewer block repair（task.p0l_reviewer_block_repair）
    // ============================================================

    fn p0l_repair_setup(
        store: &TaskCollabStore,
        peer: &PeerCredential,
        task_id: &str,
    ) -> serde_json::Value {
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": task_id,
                    "title": "P0-L repair target",
                    "description": "p0l repair test",
                    "identity_policy": "legacy_identity_v1",
                    "role_contracts": p0l_governance_roles()
                }),
            )
            .unwrap();
        let reviewer_identity = lease_identity(
            "agent-p0l-repair-reviewer",
            "session-p0l-repair-reviewer",
            "model-p0l-repair",
            "reviewer",
        );
        let findings = serde_json::json!([
            {"finding_id": "F-P0L-REPAIR-1", "fact": "P0-L 合同 policy 缺口"}
        ]);
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE tasks SET status='review' WHERE id=?1",
                params![task_id],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO task_verdict_events
                 (verdict_id, task_id, contract_id, contract_revision, contract_hash,
                  phase, reviewer_identity, findings, overall, attestation, submitted_at)
                 VALUES ('V-P0L-REPAIR-1', ?1, 'TC-P0L-REPAIR', 1, 'sha256:p0l-repair',
                         'blind_first_pass', ?2, ?3, 'block', 'attested', ?4)",
                params![
                    task_id,
                    serde_json::json!({"identity": reviewer_identity.clone()}).to_string(),
                    findings.to_string(),
                    now_ts(),
                ],
            )
            .unwrap();
        }
        let reviewer_lease = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": task_id,
                    "role": "reviewer",
                    "ttl_seconds": 3600.0,
                    "identity": reviewer_identity.clone()
                }),
            )
            .unwrap();
        serde_json::json!({
            "identity": reviewer_identity,
            "lease_token": reviewer_lease["token"],
            "fencing_counter": reviewer_lease["fencing_counter"],
        })
    }

    fn p0l_repair_call(
        store: &TaskCollabStore,
        peer: &PeerCredential,
        task_id: &str,
        request_id: &str,
        auth: &serde_json::Value,
    ) -> Result<serde_json::Value, DaemonRpcError> {
        store.handle_p0l_reviewer_block_repair(
            peer.clone(),
            &serde_json::json!({
                "task_id": task_id,
                "request_id": request_id,
                "identity": auth["identity"],
                "lease_token": auth["lease_token"],
                "fencing_counter": auth["fencing_counter"],
                "workspace_id": 1,
                "evidence_path": "deliverables/software-company/p0l_step5_review_packet_20260828.md",
                "evidence_hash": "sha256:p0l-repair-evidence",
            }),
        )
    }

    #[test]
    fn test_p0l_reviewer_block_repair_success() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap().with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let task_id = "T-P0L-REPAIR-SUCCESS";
        let auth = p0l_repair_setup(&store, &peer, task_id);

        let res = p0l_repair_call(&store, &peer, task_id, "p0l-repair-req-1", &auth)
            .expect("P0-L repair 应成功");
        assert_eq!(res["replayed"], false);
        assert_eq!(res["lifecycle_status"], "in_progress");
        assert_eq!(res["workflow_status"], "remediation_pending");
        assert_eq!(res["verdict_id"], "V-P0L-REPAIR-1");
        let step_id = res["remediation_step_id"].as_str().unwrap();

        let conn = store.conn.lock().unwrap();
        let fix_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE task_id=?1 AND action='fix_defect'",
                params![task_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(fix_count, 1, "必须恰好一个 P0-L.0 fix_defect");
        let (action, status, result): (String, String, String) = conn
            .query_row(
                "SELECT action, status, result FROM task_steps WHERE id=?1",
                params![step_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(action, "fix_defect");
        assert_eq!(status, "pending");
        let metadata: Value = serde_json::from_str(&result).unwrap();
        assert_eq!(metadata["source_outcome"], "reviewer_blocked");
        assert_eq!(metadata["source_verdict_id"], "V-P0L-REPAIR-1");
        assert_eq!(metadata["workspace_id"], 1);
        assert_eq!(metadata["evidence_hash"], "sha256:p0l-repair-evidence");
        assert_eq!(
            metadata["source_findings"][0]["finding_id"], "F-P0L-REPAIR-1",
            "fix_defect 必须绑定 source findings"
        );
        let task_status: String = conn
            .query_row(
                "SELECT status FROM tasks WHERE id=?1",
                params![task_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(task_status, "in_progress");
        let binding_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_step_role_contract_bindings WHERE task_id=?1 AND step_id=?2",
                params![task_id, step_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(binding_count, 1, "P0-L.0 必须绑定唯一 Executor Role Contract");
        let assignment_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events WHERE task_id=?1 AND reason_code='assignment_queued'",
                params![task_id],
                |row| row.get(0),
            )
            .unwrap();
        assert!(assignment_count >= 1, "P0-L.0 的 executor assignment 必须入队");
        let audit_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events WHERE task_id=?1 AND reason_code='p0l_repair_created'",
                params![task_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(audit_count, 1, "p0l_repair_created 审计必须恰好一条");
        drop(conn);
    }

    #[test]
    fn test_p0l_reviewer_block_repair_replay() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap().with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let task_id = "T-P0L-REPAIR-REPLAY";
        let auth = p0l_repair_setup(&store, &peer, task_id);

        let r1 = p0l_repair_call(&store, &peer, task_id, "p0l-repair-req-r", &auth)
            .expect("首次 repair 应成功");
        assert_eq!(r1["replayed"], false);
        // 同一 request_id 重放（daemon 重启场景）
        let r2 = p0l_repair_call(&store, &peer, task_id, "p0l-repair-req-r", &auth)
            .expect("同 request_id 重放应幂等返回");
        assert_eq!(r2["replayed"], true);
        assert_eq!(r2["remediation_step_id"], r1["remediation_step_id"]);

        let conn = store.conn.lock().unwrap();
        let fix_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE task_id=?1 AND action='fix_defect'",
                params![task_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(fix_count, 1, "重放不得重复创建 step");
        drop(conn);
    }

    #[test]
    fn test_p0l_reviewer_block_repair_wrong_task() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap().with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let task_id = "T-P0L-REPAIR-WRONG-TASK";
        let auth = p0l_repair_setup(&store, &peer, task_id);

        // 调用不存在的 task_id：lease 校验先行 fail-closed
        let err = p0l_repair_call(&store, &peer, "T-P0L-NOT-EXIST", "p0l-repair-req-wt", &auth)
            .unwrap_err();
        assert!(
            err.code == "E_LEASE_NOT_FOUND" || err.code == "task_not_found",
            "错误 task 必须被拒，实际 code={}",
            err.code
        );
    }

    #[test]
    fn test_p0l_reviewer_block_repair_wrong_workspace() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap().with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let task_id = "T-P0L-REPAIR-WRONG-WS";
        let auth = p0l_repair_setup(&store, &peer, task_id);

        // workspace_id=2 与 binding(1) 不一致 → 拒绝
        let err = store
            .handle_p0l_reviewer_block_repair(
                peer,
                &serde_json::json!({
                    "task_id": task_id,
                    "request_id": "p0l-repair-req-ww",
                    "identity": auth["identity"],
                    "lease_token": auth["lease_token"],
                    "fencing_counter": auth["fencing_counter"],
                    "workspace_id": 2,
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_WORKSPACE_AUTHORITY_MISMATCH");
    }

    #[test]
    fn test_p0l_reviewer_block_repair_verdict_unique() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap().with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let task_id = "T-P0L-REPAIR-VERDICT-UNIQUE";
        let auth = p0l_repair_setup(&store, &peer, task_id);

        let r1 = p0l_repair_call(&store, &peer, task_id, "p0l-repair-req-v1", &auth)
            .expect("首次 repair 应成功");
        // 同一 verdict 用不同 request_id 重复（顺序重入：任务已 in_progress 时状态门禁先行；
        // 并发窗口内则命中 verdict 唯一性）——两者都必须拒绝，且绝不产生第二个 fix_defect。
        let err = p0l_repair_call(&store, &peer, task_id, "p0l-repair-req-v2", &auth)
            .unwrap_err();
        assert!(
            err.code == "E_P0L_REPAIR_ALREADY_EXISTS"
                || err.code == "E_P0L_REPAIR_REVIEW_STATE_REQUIRED",
            "重复创建必须被拒，实际 code={}",
            err.code
        );
        let conn = store.conn.lock().unwrap();
        let fix_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE task_id=?1 AND action='fix_defect'",
                params![task_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(fix_count, 1, "同一 verdict 只允许一个 P0-L.0");
        assert_eq!(r1["remediation_step_id"].as_str().unwrap().len() > 0, true);
        drop(conn);
    }

    #[test]
    fn test_p0l_reviewer_block_repair_requires_reviewer_role() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap().with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let task_id = "T-P0L-REPAIR-ROLE";
        let auth = p0l_repair_setup(&store, &peer, task_id);

        // 普通客户端（implementer 身份）不能自行创建 P0-L.0 → role 门禁拒绝
        let err = store
            .handle_p0l_reviewer_block_repair(
                peer,
                &serde_json::json!({
                    "task_id": task_id,
                    "request_id": "p0l-repair-req-rr",
                    "identity": {
                        "agent_id": "agent-p0l-repair-implementer",
                        "session_id": "session-p0l-repair-impl",
                        "model_id": "model-p0l-repair",
                        "role": "implementer"
                    },
                    "lease_token": auth["lease_token"],
                    "fencing_counter": auth["fencing_counter"],
                    "workspace_id": 1,
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_P0L_REPAIR_REVIEWER_ROLE_REQUIRED");
    }

    // ============================================================
    // task.steps.bootstrap_legacy（历史任务步骤补建）
    // ============================================================

    fn steps_bootstrap_setup(
        store: &TaskCollabStore,
        peer: &PeerCredential,
        task_id: &str,
    ) -> serde_json::Value {
        // create 任务（不传 steps → task_steps=0；create 自动建 binding + Task Contract）
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": task_id,
                    "title": "legacy steps bootstrap",
                    "description": "steps bootstrap test",
                    "identity_policy": "legacy_identity_v1",
                    "role_contracts": p0l_governance_roles()
                }),
            )
            .unwrap();
        let reviewer_identity = lease_identity(
            "agent-steps-bootstrap-reviewer",
            "session-steps-bootstrap-reviewer",
            "model-steps-bootstrap",
            "reviewer",
        );
        // validate_reviewer_lease_for_adjudication 要求 reviewer lease holder 已注册
        register_agent_with_identity(
            store,
            peer,
            "agent-steps-bootstrap-reviewer",
            "inst-steps-bootstrap-reviewer",
            "session-steps-bootstrap-reviewer",
            "reviewer",
        );
        let reviewer_lease = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": task_id,
                    "role": "reviewer",
                    "ttl_seconds": 3600.0,
                    "identity": reviewer_identity.clone()
                }),
            )
            .unwrap();
        let adjudicator = lease_identity(
            "agent-steps-bootstrap-adjudicator",
            "session-steps-bootstrap-adj",
            "model-steps-bootstrap",
            "adjudicator",
        );
        serde_json::json!({
            "identity": adjudicator,
            "lease_token": reviewer_lease["token"],
            "fencing_counter": reviewer_lease["fencing_counter"],
        })
    }

    fn steps_bootstrap_call(
        store: &TaskCollabStore,
        peer: &PeerCredential,
        task_id: &str,
        request_id: &str,
        steps: &serde_json::Value,
        auth: &serde_json::Value,
    ) -> Result<serde_json::Value, DaemonRpcError> {
        store.handle_task_steps_bootstrap_legacy(
            peer.clone(),
            &serde_json::json!({
                "task_id": task_id,
                "request_id": request_id,
                "steps": steps,
                "identity": auth["identity"],
                "lease_token": auth["lease_token"],
                "fencing_counter": auth["fencing_counter"],
                "workspace_id": 1,
                "evidence_path": "deliverables/software-company/s1_t04_review_packet.md",
                "evidence_hash": "sha256:steps-bootstrap-evidence",
            }),
        )
    }

    #[test]
    fn test_steps_bootstrap_legacy_success_and_replay() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        // allowlist 任务（BOOTSTRAP_ROLE_ALLOWLIST 含 T-1787203937193-0993d120）
        let task_id = "T-1787203937193-0993d120";
        let auth = steps_bootstrap_setup(&store, &peer, task_id);

        let steps = serde_json::json!([
            {"action": "port_rust_authority", "target_file": "cli/main.py", "check_items": "CLI 迁移 daemon RPC 权威路径"},
            {"action": "thin_cli_client", "target_file": "cli/main.py", "check_items": "CLI 变薄客户端"},
            {"action": "fixture_matrix", "target_file": "tests/", "check_items": "fixture 矩阵核对"},
            {"action": "matrix_verify", "target_file": "tests/", "check_items": "矩阵验证通过"}
        ]);
        let r1 = steps_bootstrap_call(&store, &peer, task_id, "steps-bs-req-1", &steps, &auth)
            .expect("steps bootstrap 应成功");
        assert_eq!(r1["replayed"], false);
        assert_eq!(r1["step_count"], 4);

        let conn = store.conn.lock().unwrap();
        let step_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE task_id=?1",
                params![task_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(step_count, 4, "必须补建 4 个步骤");
        let pending: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE task_id=?1 AND status='pending'",
                params![task_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(pending, 4, "补建步骤应为 pending");
        let audit: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events WHERE task_id=?1 AND reason_code='steps_bootstrapped'",
                params![task_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(audit, 1, "steps_bootstrapped 审计必须恰好一条");
        drop(conn);

        // 同 request_id 重放 → replayed=true，不重复创建
        let r2 = steps_bootstrap_call(&store, &peer, task_id, "steps-bs-req-1", &steps, &auth)
            .expect("同 request_id 重放应幂等");
        assert_eq!(r2["replayed"], true);
        let conn = store.conn.lock().unwrap();
        let step_count2: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps WHERE task_id=?1",
                params![task_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(step_count2, 4, "重放不得重复创建步骤");
        drop(conn);
    }

    #[test]
    fn test_steps_bootstrap_legacy_rejects_non_allowlisted() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let task_id = "T-NOT-ALLOWLISTED-LEGACY";
        let auth = steps_bootstrap_setup(&store, &peer, task_id);
        let err = steps_bootstrap_call(
            &store, &peer, task_id, "steps-bs-req-x",
            &serde_json::json!([{"action": "port_rust_authority"}]),
            &auth,
        )
        .unwrap_err();
        assert_eq!(err.code, "E_STEPS_BOOTSTRAP_NOT_ALLOWLISTED");
    }

    #[test]
    fn test_steps_bootstrap_legacy_rejects_non_adjudicator() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        let task_id = "T-1787203937193-0993d120";
        let mut auth = steps_bootstrap_setup(&store, &peer, task_id);
        auth["identity"] = lease_identity(
            "agent-steps-bootstrap-impl",
            "session-steps-bootstrap-impl",
            "model-steps-bootstrap",
            "implementer",
        );
        let err = steps_bootstrap_call(
            &store, &peer, task_id, "steps-bs-req-y",
            &serde_json::json!([{"action": "port_rust_authority"}]),
            &auth,
        )
        .unwrap_err();
        assert_eq!(err.code, "E_STEPS_BOOTSTRAP_ROLE_REQUIRED");
    }

    #[test]
    fn test_reviewer_pass_requires_task_bound_verdict_before_handoff() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let task_id = "T-REVIEWER-PASS-PROVENANCE";
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": task_id,
                    "title": "reviewer pass provenance",
                    "steps": [{"action": "implement", "target_file": "src/pass.rs"}],
                    "identity_policy": "legacy_identity_v1",
                    "role_contracts": p0l_governance_roles()
                }),
            )
            .unwrap();
        let source_step_id: String = {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE tasks SET status='review' WHERE id=?1",
                params![task_id],
            )
            .unwrap();
            let step_id: String = conn
                .query_row(
                    "SELECT id FROM task_steps WHERE task_id=?1",
                    params![task_id],
                    |row| row.get(0),
                )
                .unwrap();
            conn.execute(
                "UPDATE task_steps SET status='done', result='executor delivery', completed_at=?1
                 WHERE id=?2 AND task_id=?3",
                params![now_ts(), step_id, task_id],
            )
            .unwrap();
            step_id
        };
        let mut reviewer_identity = lease_identity(
            "agent-reviewer-pass-provenance",
            "reviewer-pass-session",
            "reviewer-pass-model",
            "reviewer",
        );
        reviewer_identity["agent_instance_id"] = serde_json::json!("reviewer-pass-instance");
        let reviewer_lease = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": task_id,
                    "role": "reviewer",
                    "ttl_seconds": 3600.0,
                    "identity": reviewer_identity.clone()
                }),
            )
            .unwrap();
        let handoff = serde_json::json!({
            "task_id": task_id,
            "from_role": "reviewer",
            "outcome": "reviewer_pass",
            "next_role": "adjudicator",
            "next_action": "adjudicate current verdict",
            "reason": "review passed",
            "independence_requirement": "required",
            "request_id": "handoff-reviewer-pass-provenance-1",
            "step_id": source_step_id,
            "report_request_id": "review-report-1",
            "evidence_path": "docs/evidence/reviewer-pass.json",
            "evidence_hash": "sha256:reviewer-pass",
            "identity": reviewer_identity.clone(),
            "lease_token": reviewer_lease["token"],
            "fencing_counter": reviewer_lease["fencing_counter"]
        });

        let missing_verdict = store
            .handle_task_handoff(peer.clone(), &handoff)
            .expect_err("没有 Verdict Ledger 时 reviewer_pass 必须 fail-closed");
        assert_eq!(missing_verdict.code, "E_HANDOFF_VERDICT_REQUIRED");
        {
            let conn = store.conn.lock().unwrap();
            let handoff_count: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM task_events
                     WHERE task_id=?1 AND reason_code='handoff_structured'",
                    params![task_id],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(handoff_count, 0, "拒绝必须不留下部分 handoff");
        }

        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "INSERT INTO task_verdict_events
                 (verdict_id, task_id, contract_id, contract_revision, contract_hash,
                  phase, step_id, snapshot_id, view_manifest_hash, reviewer_identity,
                  findings, overall, attestation, submitted_at, workspace_id)
                 VALUES ('V-REVIEWER-PASS-1', ?1, 'TC-PASS', 1, 'sha256:task',
                         'blind_first_pass', ?2, 'snapshot-pass-1', 'manifest-pass-1',
                         ?3, '[]', 'pass', 'attested', ?4, 1)",
                params![
                    task_id,
                    source_step_id,
                    serde_json::json!({"identity": reviewer_identity}).to_string(),
                    now_ts(),
                ],
            )
            .unwrap();
        }
        let update_verdict =
            |identity_value: Value, verdict_step: &str, snapshot: &str, manifest: &str, workspace: i64| {
                let conn = store.conn.lock().unwrap();
                conn.execute(
                    "UPDATE task_verdict_events
                     SET step_id=?1, snapshot_id=?2, view_manifest_hash=?3,
                         reviewer_identity=?4, workspace_id=?5
                     WHERE verdict_id='V-REVIEWER-PASS-1'",
                    params![
                        verdict_step,
                        snapshot,
                        manifest,
                        serde_json::json!({"identity": identity_value}).to_string(),
                        workspace,
                    ],
                )
                .unwrap();
            };
        let assert_rejected = |request_id: &str, expected_code: &str| {
            let mut attempt = handoff.clone();
            attempt["request_id"] = serde_json::json!(request_id);
            let rejected = store
                .handle_task_handoff(peer.clone(), &attempt)
                .expect_err("invalid reviewer pass provenance must fail closed");
            assert_eq!(
                rejected.code, expected_code,
                "request_id={request_id}"
            );
        };

        let mut missing_role = reviewer_identity.clone();
        missing_role.as_object_mut().unwrap().remove("role");
        update_verdict(missing_role, &source_step_id, "snapshot-pass-1", "manifest-pass-1", 1);
        assert_rejected("handoff-reviewer-pass-provenance-missing-role", "E_HANDOFF_VERDICT_IDENTITY_MISMATCH");

        let mut wrong_role = reviewer_identity.clone();
        wrong_role["role"] = serde_json::json!("executor");
        update_verdict(wrong_role, &source_step_id, "snapshot-pass-1", "manifest-pass-1", 1);
        assert_rejected("handoff-reviewer-pass-provenance-wrong-role", "E_HANDOFF_VERDICT_IDENTITY_MISMATCH");

        let mut missing_instance = reviewer_identity.clone();
        missing_instance.as_object_mut().unwrap().remove("agent_instance_id");
        update_verdict(missing_instance, &source_step_id, "snapshot-pass-1", "manifest-pass-1", 1);
        assert_rejected("handoff-reviewer-pass-provenance-missing-instance", "E_HANDOFF_VERDICT_IDENTITY_MISMATCH");

        let mut wrong_instance = reviewer_identity.clone();
        wrong_instance["agent_instance_id"] = serde_json::json!("other-reviewer-instance");
        update_verdict(wrong_instance, &source_step_id, "snapshot-pass-1", "manifest-pass-1", 1);
        assert_rejected("handoff-reviewer-pass-provenance-wrong-instance", "E_HANDOFF_VERDICT_IDENTITY_MISMATCH");

        let mut missing_agent = reviewer_identity.clone();
        missing_agent.as_object_mut().unwrap().remove("agent_id");
        update_verdict(missing_agent, &source_step_id, "snapshot-pass-1", "manifest-pass-1", 1);
        assert_rejected("handoff-reviewer-pass-provenance-missing-agent", "E_HANDOFF_VERDICT_IDENTITY_MISMATCH");

        let mut wrong_session = reviewer_identity.clone();
        wrong_session["session_id"] = serde_json::json!("other-reviewer-session");
        update_verdict(wrong_session, &source_step_id, "snapshot-pass-1", "manifest-pass-1", 1);
        assert_rejected("handoff-reviewer-pass-provenance-wrong-session", "E_HANDOFF_VERDICT_IDENTITY_MISMATCH");

        let mut wrong_model = reviewer_identity.clone();
        wrong_model["model_id"] = serde_json::json!("other-reviewer-model");
        update_verdict(wrong_model, &source_step_id, "snapshot-pass-1", "manifest-pass-1", 1);
        assert_rejected("handoff-reviewer-pass-provenance-wrong-model", "E_HANDOFF_VERDICT_IDENTITY_MISMATCH");

        update_verdict(reviewer_identity.clone(), "other-source-step", "snapshot-pass-1", "manifest-pass-1", 1);
        assert_rejected("handoff-reviewer-pass-provenance-wrong-step", "E_HANDOFF_VERDICT_PROVENANCE_MISMATCH");
        update_verdict(reviewer_identity.clone(), &source_step_id, "", "manifest-pass-1", 1);
        assert_rejected("handoff-reviewer-pass-provenance-missing-snapshot", "E_HANDOFF_VERDICT_PROVENANCE_MISMATCH");
        update_verdict(reviewer_identity.clone(), &source_step_id, "snapshot-pass-1", "", 1);
        assert_rejected("handoff-reviewer-pass-provenance-missing-manifest", "E_HANDOFF_VERDICT_PROVENANCE_MISMATCH");
        update_verdict(reviewer_identity.clone(), &source_step_id, "snapshot-pass-1", "manifest-pass-1", 2);
        assert_rejected("handoff-reviewer-pass-provenance-wrong-workspace", "E_HANDOFF_VERDICT_PROVENANCE_MISMATCH");

        update_verdict(reviewer_identity.clone(), &source_step_id, "snapshot-pass-1", "manifest-pass-1", 1);
        let mut current_missing_instance = handoff.clone();
        let mut incomplete_identity = reviewer_identity.clone();
        incomplete_identity
            .as_object_mut()
            .unwrap()
            .remove("agent_instance_id");
        current_missing_instance["identity"] = incomplete_identity;
        current_missing_instance["request_id"] =
            serde_json::json!("handoff-reviewer-pass-provenance-current-missing-instance");
        let current_identity_rejected = store
            .handle_task_handoff(peer.clone(), &current_missing_instance)
            .expect_err("current reviewer identity without instance must fail closed");
        assert_eq!(
            current_identity_rejected.code,
            "E_HANDOFF_VERDICT_IDENTITY_MISMATCH"
        );

        let accepted = store
            .handle_task_handoff(peer, &handoff)
            .expect("真实同 task pass verdict 应允许 reviewer_pass handoff");
        assert_eq!(accepted["status"], "review");

        let conn = store.conn.lock().unwrap();
        let envelope: Value = conn
            .query_row(
                "SELECT reason FROM task_events
                 WHERE task_id=?1 AND reason_code='handoff_structured'
                 ORDER BY event_id DESC LIMIT 1",
                params![task_id],
                |row| {
                    let raw: String = row.get(0)?;
                    Ok(serde_json::from_str(&raw).unwrap())
                },
            )
            .unwrap();
        assert_eq!(envelope["source_verdict_id"], "V-REVIEWER-PASS-1");
        assert_eq!(envelope["snapshot_id"], "snapshot-pass-1");
        assert_eq!(envelope["view_manifest_hash"], "manifest-pass-1");
        let projected_snapshot: String = conn
            .query_row(
                "SELECT snapshot_id FROM task_events
                 WHERE task_id=?1 AND reason_code='handoff_structured'
                 ORDER BY event_id DESC LIMIT 1",
                params![task_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(projected_snapshot, "snapshot-pass-1");
    }
