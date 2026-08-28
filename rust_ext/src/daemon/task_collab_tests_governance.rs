//! task_collab governance、contract、evidence 和 verdict 测试。

use super::*;
use super::support::*;
    #[test]
    fn test_stale_claim_cannot_be_taken_over_by_different_role() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({
                    "workspace_id": 1,
                    "task_id": "T-CLAIM-STALE-CROSS-ROLE",
                    "title": "cross role must conflict",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                }),
            )
            .unwrap();
        register_agent_with_identity(
            &store,
            &peer,
            "cross-old",
            "cross-old-inst",
            "cross-old-sess",
            "executor",
        );
        store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-CLAIM-STALE-CROSS-ROLE",
                    "agent_session_id": "cross-old-sess",
                    "identity": {
                        "agent_id": "cross-old", "agent_instance_id": "cross-old-inst",
                        "client_id": "test", "provider": "test", "model_id": "model-old",
                        "session_id": "cross-old-sess", "role": "executor",
                    },
                }),
            )
            .unwrap();
        store
            .conn
            .lock()
            .unwrap()
            .execute(
                "UPDATE agent_registrations SET last_heartbeat = 0 WHERE agent_id = 'cross-old'",
                [],
            )
            .unwrap();
        register_agent_with_identity(
            &store,
            &peer,
            "cross-reviewer",
            "cross-reviewer-inst",
            "cross-reviewer-sess",
            "reviewer",
        );
        let err = store
            .handle_task_claim(
                peer,
                &serde_json::json!({
                    "task_id": "T-CLAIM-STALE-CROSS-ROLE",
                    "agent_session_id": "cross-reviewer-sess",
                    "identity": {
                        "agent_id": "cross-reviewer", "agent_instance_id": "cross-reviewer-inst",
                        "client_id": "test", "provider": "test", "model_id": "model-reviewer",
                        "session_id": "cross-reviewer-sess", "role": "reviewer",
                    },
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "task_conflict");
    }

    #[test]
    fn test_orphan_claim_recovery_requires_stale_owner_and_preserves_step_state() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CLAIM-RECOVER",
                    "title": "recover",
                    "steps": [
                        {"action": "report", "target_file": "a.rs"},
                        {"action": "fix_defect", "target_file": "b.rs"}
                    ],
                }),
            )
            .unwrap();

        let old = serde_json::json!({
            "agent_id": "agent-old",
            "agent_instance_id": "old-instance",
            "client_id": "test",
            "provider": "test",
            "model_id": "model-old",
            "session_id": "old-session",
            "role": "implementer",
        });
        register_agent_with_identity(
            &store,
            &peer,
            "agent-old",
            "old-instance",
            "old-session",
            "implementer",
        );
        store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-CLAIM-RECOVER",
                    "agent_session_id": "old-session",
                    "identity": old,
                }),
            )
            .unwrap();

        register_agent_with_identity(
            &store,
            &peer,
            "agent-adjudicator",
            "adjudicator-instance",
            "adjudicator-session",
            "adjudicator",
        );
        // 让旧 owner 明确失联；不能依赖客户端时间戳。
        store
            .conn
            .lock()
            .unwrap()
            .execute(
                "UPDATE agent_registrations SET last_heartbeat = 0 WHERE session_id = 'old-session'",
                [],
            )
            .unwrap();

        // P0-G：跨角色恢复——reviewer lease 必须由独立 Reviewer 持有
        // （agent/instance/session 均与 Adjudicator 不同），Adjudicator 只执行恢复。
        register_agent_with_identity(
            &store,
            &peer,
            "agent-reviewer",
            "reviewer-instance",
            "reviewer-session",
            "reviewer",
        );
        let lease = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-CLAIM-RECOVER",
                    "role": "reviewer",
                    "identity": {
                        "agent_id": "agent-reviewer",
                        "agent_instance_id": "reviewer-instance",
                        "client_id": "test",
                        "provider": "test",
                        "model_id": "model-reviewer",
                        "session_id": "reviewer-session",
                        "role": "reviewer",
                    },
                }),
            )
            .unwrap();
        let recover_params = serde_json::json!({
            "request_id": "recover-request-1",
            "task_id": "T-CLAIM-RECOVER",
            "reason": "原 owner session 已失联，需要恢复 fix_defect 工作流",
            "lease_token": lease["token"],
            "fencing_counter": lease["fencing_counter"],
            "identity": {
                "agent_id": "agent-adjudicator",
                "agent_instance_id": "adjudicator-instance",
                "client_id": "test",
                "provider": "test",
                "model_id": "model-adjudicator",
                "session_id": "adjudicator-session",
                "role": "adjudicator",
            },
        });
        let recovered = store
            .handle_task_claim_recover(peer.clone(), &recover_params)
            .unwrap();
        assert_eq!(recovered["claim_status"], "released");
        assert_eq!(recovered["old_session_id"], "old-session");
        assert_eq!(
            store.get_task_claim_info(&store.conn.lock().unwrap(), "T-CLAIM-RECOVER"),
            (None, None)
        );

        // recovery 只释放 claim；步骤状态和历史 evidence 不被重写，新的 Executor 再显式 claim。
        let new_executor = serde_json::json!({
            "agent_id": "agent-new",
            "agent_instance_id": "new-instance",
            "client_id": "test",
            "provider": "test",
            "model_id": "model-new",
            "session_id": "new-session",
            "role": "implementer",
        });
        register_agent_with_identity(
            &store,
            &peer,
            "agent-new",
            "new-instance",
            "new-session",
            "implementer",
        );
        let claimed = store
            .handle_task_claim(
                peer,
                &serde_json::json!({
                    "task_id": "T-CLAIM-RECOVER",
                    "agent_session_id": "new-session",
                    "identity": new_executor,
                }),
            )
            .unwrap();
        assert_eq!(claimed["claimed_by"], "new-session");
        assert_eq!(claimed["step_index"], 0);

        let replay = store
            .handle_task_claim_recover(PeerCredential::new_unix(1000, 1000, 1234), &recover_params)
            .unwrap();
        assert_eq!(replay["recovery_event_id"], recovered["recovery_event_id"]);
        assert_eq!(
            replay["replayed"], false,
            "同 request_id 应返回第一次确定性结果"
        );
    }

    #[test]
    fn test_orphan_claim_recovery_rejects_fresh_owner() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CLAIM-RECOVER-FRESH",
                    "title": "recover fresh",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                }),
            )
            .unwrap();
        let old = serde_json::json!({
            "agent_id": "agent-fresh-old",
            "agent_instance_id": "fresh-old-instance",
            "client_id": "test",
            "provider": "test",
            "model_id": "model-old",
            "session_id": "fresh-old-session",
            "role": "implementer",
        });
        register_agent_with_identity(
            &store,
            &peer,
            "agent-fresh-old",
            "fresh-old-instance",
            "fresh-old-session",
            "implementer",
        );
        store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-CLAIM-RECOVER-FRESH",
                    "agent_session_id": "fresh-old-session",
                    "identity": old,
                }),
            )
            .unwrap();
        register_agent_with_identity(
            &store,
            &peer,
            "agent-fresh-adjudicator",
            "fresh-adjudicator-instance",
            "fresh-adjudicator-session",
            "adjudicator",
        );
        // P0-G：独立 Reviewer 持 lease（fresh owner 场景下跨角色校验也必须先通过
        // reviewer lease 门禁，才能到达 E_CLAIM_OWNER_ACTIVE 判定）。
        register_agent_with_identity(
            &store,
            &peer,
            "agent-fresh-reviewer",
            "fresh-reviewer-instance",
            "fresh-reviewer-session",
            "reviewer",
        );
        let lease = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-CLAIM-RECOVER-FRESH",
                    "role": "reviewer",
                    "identity": {
                        "agent_id": "agent-fresh-reviewer",
                        "agent_instance_id": "fresh-reviewer-instance",
                        "client_id": "test",
                        "provider": "test",
                        "model_id": "model-reviewer",
                        "session_id": "fresh-reviewer-session",
                        "role": "reviewer",
                    },
                }),
            )
            .unwrap();
        let err = store
            .handle_task_claim_recover(
                peer,
                &serde_json::json!({
                    "task_id": "T-CLAIM-RECOVER-FRESH",
                    "reason": "测试 fresh owner 必须拒绝",
                    "lease_token": lease["token"],
                    "fencing_counter": lease["fencing_counter"],
                    "identity": {
                        "agent_id": "agent-fresh-adjudicator",
                        "agent_instance_id": "fresh-adjudicator-instance",
                        "client_id": "test",
                        "provider": "test",
                        "model_id": "model-adjudicator",
                        "session_id": "fresh-adjudicator-session",
                        "role": "adjudicator",
                    },
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_CLAIM_OWNER_ACTIVE");
    }

    // P0-G G3：只读 governance projection 对无投影任务返回稳定 diagnosis，
    // 对缺失 normalization 不崩溃，且绝不返回 lease raw token。
    #[test]
    fn test_governance_projection_returns_diagnosis_without_lease_token() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-G3-PROJ",
                    "title": "g3 projection",
                    "steps": [{"action": "audit", "target_file": "a.rs"}],
                }),
            )
            .unwrap();

        let proj = store
            .handle_task_governance_projection_get(
                peer,
                &serde_json::json!({"task_id": "T-G3-PROJ"}),
            )
            .unwrap();
        assert_eq!(proj["task_id"], "T-G3-PROJ");
        assert_eq!(proj["lease_raw_token_omitted"], serde_json::json!(true));
        // 无 Task Contract 投影 → 稳定 diagnosis（不崩溃）
        assert!(proj["task_contract"]["diagnosis"].as_str().is_some());
        // normalization：migrate 可能已播种规则（有值）或缺失（diagnosis），两者都合法
        let norm = &proj["normalization_rules"];
        if norm["diagnosis"].as_str().is_none() {
            assert!(
                norm["version"].as_str().is_some(),
                "normalization 有值时必须含 version"
            );
            assert_eq!(norm["revoked"], serde_json::json!(false));
        }
        // verdicts 为空数组
        assert_eq!(proj["verdicts"].as_array().map(Vec::len), Some(0));
    }

    // P0-G G2：task.contract_revise handler 全链路——create → bootstrap v1 →
    // revise n+1 append-only（revision 连续、hash 锚定、事件落账）。
    #[test]
    fn test_contract_revise_appends_revision_n_plus_1_via_handler() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-G2-REVISE",
                    "title": "g2 revise",
                    "steps": [{"action": "audit", "target_file": "a.rs"}],
                }),
            )
            .unwrap();
        // task_create 已自动写 workspace binding（instance=ws-1）；不再重复 seed。
        register_agent_with_identity(
            &store,
            &peer,
            "g2-reviewer",
            "g2-r-inst",
            "g2-r-sess",
            "reviewer",
        );
        register_agent_with_identity(
            &store,
            &peer,
            "g2-adjudicator",
            "g2-a-inst",
            "g2-a-sess",
            "adjudicator",
        );
        // P0-L：显式 bootstrap 仍以三角色 legacy role_contracts 为权威源；本测试的
        // create 不能带 role_contracts（否则 create 已自动建投影，后续显式 bootstrap 会被
        // NOT_EMPTY 拒绝），故直接 SQL 补种三角色合同。
        {
            let conn = store.conn.lock().unwrap();
            for role in ["executor", "reviewer", "adjudicator"] {
                conn.execute(
                    "INSERT INTO role_contracts
                     (contract_id, task_id, step_id, role, skill_id, skill_version,
                      prompt_template_id, prompt_hash, allowed_paths, forbidden_paths,
                      commands, acceptance_checks, required_evidence, handoff_to,
                      independence, revision, is_current, created_at, created_by)
                     VALUES (?1, 'T-G2-REVISE', '', ?2, '', '', '', '', '', '', '', '', '', '', '{}', 1, 1, 0, 'test')",
                    [format!("RC-G2-{role}"), role.to_string()],
                )
                .unwrap();
            }
        }

        // 独立 Reviewer 获取 reviewer lease
        let lease = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-G2-REVISE", "role": "reviewer",
                    "identity": {"agent_id": "g2-reviewer", "agent_instance_id": "g2-r-inst",
                                 "client_id": "t", "provider": "t", "model_id": "m",
                                 "session_id": "g2-r-sess", "role": "reviewer"},
                }),
            )
            .unwrap();

        // 先 bootstrap v1（adjudicator + 独立 reviewer lease）
        let v1_envelope = serde_json::json!({
            "contract_id": "c-g2",
            "revision": 1,
            "profile": "code_change",
            "objective": "bootstrap v1",
            "source_provenance": "p0g-test",
            "interfaces": ["cli"],
            "allowed_edit_scope": ["src/"],
            "acceptance_clauses": ["tests pass"],
            "risks": ["low"],
            "rollback": ["revert"],
            "dependencies": ["none"],
        });
        let boot = store
            .handle_task_contract_bootstrap(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-G2-REVISE",
                    "envelope": v1_envelope,
                    "workspace_id": 1,
                    "workspace_instance_id": "ws-1",
                    "request_id": "g2-boot-1",
                    "evidence_path": "ev/path", "evidence_hash": "ev-hash",
                    "lease_token": lease["token"], "fencing_counter": lease["fencing_counter"],
                    "identity": {"agent_id": "g2-adjudicator", "agent_instance_id": "g2-a-inst",
                                 "client_id": "t", "provider": "t", "model_id": "m",
                                 "session_id": "g2-a-sess", "role": "adjudicator"},
                }),
            )
            .unwrap();
        let v1_hash = boot["contract_hash"].as_str().unwrap().to_string();

        // revise n+1
        let v2_envelope = serde_json::json!({
            "contract_id": "c-g2",
            "revision": 2,
            "supersedes_revision": 1,
            "supersedes_contract_hash": v1_hash,
            "profile": "code_change",
            "objective": "revise v2",
            "source_provenance": "p0g-test-v2",
            "interfaces": ["cli"],
            "allowed_edit_scope": ["src/", "tests/"],
            "acceptance_clauses": ["tests pass", "negative tests"],
            "risks": ["low", "medium"],
            "rollback": ["revert", "git revert"],
            "dependencies": ["none"],
        });
        let rev = store
            .handle_task_contract_revise(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-G2-REVISE",
                    "envelope": v2_envelope,
                    "expected_previous_hash": v1_hash,
                    "workspace_id": 1,
                    "workspace_instance_id": "ws-1",
                    "request_id": "g2-revise-1",
                    "evidence_path": "ev/path2", "evidence_hash": "ev-hash2",
                    "lease_token": lease["token"], "fencing_counter": lease["fencing_counter"],
                    "identity": {"agent_id": "g2-adjudicator", "agent_instance_id": "g2-a-inst",
                                 "client_id": "t", "provider": "t", "model_id": "m",
                                 "session_id": "g2-a-sess", "role": "adjudicator"},
                }),
            )
            .unwrap();
        assert_eq!(rev["previous_revision"], 1);
        assert_eq!(rev["revision"], 2);

        // 校验 revisions 表中 revision 1 与 2 均存在（append-only，r1 未变）。
        // 注意：conn MutexGuard 必须在此块结束时释放，否则下方负向 revise 内部
        // 再次 lock conn 会死锁（std Mutex 不可重入）。
        let revisions: Vec<i64> = {
            let conn = store.conn.lock().unwrap();
            let mut stmt = conn
                .prepare("SELECT revision FROM task_contract_revisions WHERE task_id = ?1 ORDER BY revision ASC")
                .unwrap();
            stmt.query_map(params!["T-G2-REVISE"], |r| r.get::<_, i64>(0))
                .unwrap()
                .flatten()
                .collect()
        };
        assert_eq!(revisions, vec![1, 2], "revision 1 必须保留（append-only）");

        // 错误锚定拒绝（expected_previous_hash 不对 → E_TASK_CONTRACT_REVISE_CONFLICT）
        let err = store
            .handle_task_contract_revise(
                peer,
                &serde_json::json!({
                    "task_id": "T-G2-REVISE",
                    "envelope": v2_envelope,
                    "expected_previous_hash": "sha256:wrong",
                    "workspace_id": 1,
                    "workspace_instance_id": "ws-1",
                    "request_id": "g2-revise-2",
                    "evidence_path": "ev/path2", "evidence_hash": "ev-hash2",
                    "lease_token": lease["token"], "fencing_counter": lease["fencing_counter"],
                    "identity": {"agent_id": "g2-adjudicator", "agent_instance_id": "g2-a-inst",
                                 "client_id": "t", "provider": "t", "model_id": "m",
                                 "session_id": "g2-a-sess", "role": "adjudicator"},
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_TASK_CONTRACT_REVISE_CONFLICT");
    }

    // ============================================
    // 任务 F（T-1786440663336-7e7d67e8）步骤 #3：Agent Identity + Role Contract
    // ============================================

    #[test]
    fn test_agent_register_persists_full_identity() {
        // A2: agent.register 必须持久化 identity 最小字段（instance/client/provider/model/runtime_hash/session/role）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        register_agent_with_identity(
            &store,
            &peer,
            "agent-alpha",
            "INST-1",
            "SES-1",
            "implementer",
        );

        let conn = store.conn.lock().unwrap();
        let (instance, provider, model, session, role, runtime): (
            String,
            String,
            String,
            String,
            String,
            String,
        ) = conn
            .query_row(
                "SELECT agent_instance_id, provider, model_id, session_id, role, runtime_hash
                 FROM agent_registrations WHERE agent_id = 'agent-alpha'",
                [],
                |r| {
                    Ok((
                        r.get(0)?,
                        r.get(1)?,
                        r.get(2)?,
                        r.get(3)?,
                        r.get(4)?,
                        r.get(5)?,
                    ))
                },
            )
            .unwrap();
        assert_eq!(instance, "INST-1");
        assert_eq!(provider, "anthropic");
        assert_eq!(model, "claude-test");
        assert_eq!(session, "SES-1");
        assert_eq!(role, "implementer");
        assert_eq!(runtime, "deadbeef");
    }

    #[test]
    fn test_claim_unregistered_identity_fail_closed() {
        // A2: 未注册 identity 的 claim 必须 fail-closed（E_IDENTITY_UNREGISTERED）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,"task_id": "T-GHOST", "title": "ghost"}),
            )
            .unwrap();

        let err = store
            .handle_task_claim(
                peer,
                &serde_json::json!({
                    "task_id": "T-GHOST",
                    "agent_session_id": "SES-GHOST",
                    "identity": {
                        "agent_id": "agent-ghost",
                        "agent_instance_id": "INST-G",
                        "session_id": "SES-GHOST",
                        "model_id": "model-test",
                        "role": "implementer",
                    },
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_IDENTITY_UNREGISTERED");
    }

    #[test]
    fn test_claim_contract_task_requires_identity() {
        // A3: 冻结 Role Contract 的任务，不带 identity 的 claim 必须 fail-closed
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CONTRACT-NOID",
                    "title": "contract task",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                    "identity_policy": "legacy_identity_v1",
                    "role_contracts": [
                        {
                            "role": "implementer",
                            "skill_id": "g0-experiment",
                            "skill_version": "1.0.0",
                            "prompt_hash": "abc123",
                        },
                        {"role": "executor", "independence": "{}"},
                        {"role": "reviewer", "independence": "{}"},
                        {"role": "adjudicator", "independence": "{}"}
                    ],
                }),
            )
            .unwrap();

        let err = store
            .handle_task_claim(
                peer,
                &serde_json::json!({"task_id": "T-CONTRACT-NOID", "agent_session_id": "SES-X"}),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_IDENTITY_REQUIRED");
    }

    #[test]
    fn test_claim_contract_skill_mismatch_rejected() {
        // A3: skill_id 不符时拒绝领取（E_CONTRACT_SKILL_MISMATCH）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        register_agent_with_identity(&store, &peer, "agent-imp", "INST-2", "SES-2", "implementer");
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CONTRACT-MISMATCH",
                    "title": "contract mismatch",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                    "identity_policy": "legacy_identity_v1",
                    "role_contracts": [
                        {
                            "role": "implementer",
                            "skill_id": "g0-experiment",
                            "skill_version": "1.0.0",
                            "prompt_hash": "abc123",
                        },
                        {"role": "executor", "independence": "{}"},
                        {"role": "reviewer", "independence": "{}"},
                        {"role": "adjudicator", "independence": "{}"}
                    ],
                }),
            )
            .unwrap();

        let err = store
            .handle_task_claim(
                peer,
                &serde_json::json!({
                    "task_id": "T-CONTRACT-MISMATCH",
                    "agent_session_id": "SES-2",
                    "identity": {
                        "agent_id": "agent-imp",
                        "agent_instance_id": "INST-2",
                        "session_id": "SES-2",
                        "model_id": "claude-test",
                        "role": "implementer",
                    },
                    "contract_claim": {"skill_id": "wrong-skill", "skill_version": "1.0.0", "prompt_hash": "abc123"},
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_CONTRACT_SKILL_MISMATCH");
    }

    #[test]
    fn test_claim_envelope_returns_role_contract() {
        // A3: 合同匹配时 claim 成功，且 Task Envelope 携带 role_contract（hash/revision 存证）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        register_agent_with_identity(&store, &peer, "agent-imp", "INST-3", "SES-3", "implementer");
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-ENVELOPE",
                    "title": "envelope",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                    "identity_policy": "legacy_identity_v1",
                    "role_contracts": [
                        {
                            "role": "implementer",
                            "skill_id": "g0-experiment",
                            "skill_version": "1.0.0",
                            "prompt_template_id": "pt-1",
                            "prompt_hash": "abc123",
                            "allowed_paths": ["rust_ext/src/daemon"],
                            "forbidden_paths": ["db/schema.py"],
                            "handoff_to": "independent_reviewer",
                        },
                        {"role": "executor", "independence": "{}"},
                        {"role": "reviewer", "independence": "{}"},
                        {"role": "adjudicator", "independence": "{}"}
                    ],
                }),
            )
            .unwrap();

        let claim = store
            .handle_task_claim(
                peer,
                &serde_json::json!({
                    "task_id": "T-ENVELOPE",
                    "agent_session_id": "SES-3",
                    "identity": {
                        "agent_id": "agent-imp",
                        "agent_instance_id": "INST-3",
                        "session_id": "SES-3",
                        "model_id": "claude-test",
                        "role": "implementer",
                    },
                    "contract_claim": {"skill_id": "g0-experiment", "skill_version": "1.0.0", "prompt_hash": "abc123"},
                }),
            )
            .unwrap();
        assert_eq!(claim["status"], "in_progress");
        let contract = claim["role_contract"]
            .as_object()
            .expect("claim 必须携带 role_contract");
        assert_eq!(contract["role"], "implementer");
        assert_eq!(contract["skill_id"], "g0-experiment");
        assert_eq!(contract["prompt_hash"], "abc123");
        assert_eq!(contract["revision"], 1);
        assert_eq!(contract["handoff_to"], "independent_reviewer");
        assert!(contract["forbidden_paths"]
            .as_str()
            .unwrap()
            .contains("db/schema.py"));
    }

    #[test]
    fn test_role_independence_gate_blocks_shared_instance() {
        // A3: 同一 agent_instance_id 不能同时持有 implementer 与 independent_reviewer
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer_a = PeerCredential::new_unix(1000, 1000, 1234);
        let peer_b = PeerCredential::new_unix(1001, 1001, 5678);
        register_agent_with_identity(
            &store,
            &peer_a,
            "agent-imp",
            "INST-SHARED",
            "SES-A",
            "implementer",
        );
        register_agent_with_identity(
            &store,
            &peer_b,
            "agent-rev",
            "INST-SHARED",
            "SES-B",
            "independent_reviewer",
        );
        seed_workspace(&store);
        store
            .handle_task_create(
                peer_b.clone(),
                &serde_json::json!({ "workspace_id": 1,"task_id": "T-INDEP", "title": "indep"}),
            )
            .unwrap();

        let err = store
            .handle_task_claim(
                peer_b,
                &serde_json::json!({
                    "task_id": "T-INDEP",
                    "agent_session_id": "SES-B",
                    "identity": {
                        "agent_id": "agent-rev",
                        "agent_instance_id": "INST-SHARED",
                        "session_id": "SES-B",
                        "model_id": "claude-test",
                        "role": "independent_reviewer",
                    },
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_ROLE_INDEPENDENCE_VIOLATION");
    }

    #[test]
    fn test_contract_set_bumps_revision_and_audits() {
        // A3: 合同变更必须生成新 revision 并追加 contract_set 审计事件
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,"task_id": "T-CS", "title": "cs"}),
            )
            .unwrap();

        let set = store
            .handle_task_contract_set(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-CS",
                    "contract": {"role": "implementer", "skill_id": "g0-experiment", "prompt_hash": "hash-v1"},
                }),
            )
            .unwrap();
        assert_eq!(set["revision"], 1);
        let set2 = store
            .handle_task_contract_set(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-CS",
                    "contract": {"role": "implementer", "skill_id": "g0-experiment", "prompt_hash": "hash-v2"},
                }),
            )
            .unwrap();
        assert_eq!(set2["revision"], 2);

        let conn = store.conn.lock().unwrap();
        let current: String = conn
            .query_row(
                "SELECT prompt_hash FROM role_contracts WHERE task_id = 'T-CS' AND role = 'implementer' AND is_current = 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(current, "hash-v2");
        let old_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM role_contracts WHERE task_id = 'T-CS' AND role = 'implementer' AND is_current = 0",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(old_count, 1);
        let event_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events WHERE task_id = 'T-CS' AND reason_code = 'contract_set'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(event_count, 2);
    }

    #[test]
    fn test_report_role_not_contracted_rejected() {
        // A3: 合同任务 report 必须匹配已冻结角色（未合同角色 report 拒绝）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        register_agent_with_identity(&store, &peer, "agent-t", "INST-4", "SES-4", "observer");
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-ROLE-MISMATCH",
                    "title": "role mismatch",
                    "steps": [{"action": "test", "target_file": "a.rs"}],
                    "identity_policy": "legacy_identity_v1",
                    "role_contracts": [
                        {"role": "implementer", "skill_id": "g0-experiment"},
                        {"role": "executor", "independence": "{}"},
                        {"role": "reviewer", "independence": "{}"},
                        {"role": "adjudicator", "independence": "{}"}
                    ],
                }),
            )
            .unwrap();
        // 未知治理 runtime role claim（无对应 Executor/Reviewer/Adjudicator 合同）仍成功，
        // 但 report 必须在合同门禁处拒绝。
        let claim = store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-ROLE-MISMATCH",
                    "agent_session_id": "SES-4",
                    "identity": {
                        "agent_id": "agent-t",
                        "agent_instance_id": "INST-4",
                        "session_id": "SES-4",
                        "model_id": "claude-test",
                        "role": "observer",
                    },
                }),
            )
            .unwrap();
        assert_eq!(claim["status"], "in_progress");
        let step_id = claim["step_id"].as_str().unwrap();

        let err = store
            .handle_task_report(
                peer,
                &serde_json::json!({
                    "task_id": "T-ROLE-MISMATCH",
                    "step_id": step_id,
                    "summary": "done",
                    "success": true,
                    "identity": {
                        "agent_id": "agent-t",
                        "agent_instance_id": "INST-4",
                        "session_id": "SES-4",
                        "model_id": "claude-test",
                        "role": "observer",
                    },
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_CONTRACT_ROLE_MISMATCH");
    }

    #[test]
    fn test_task_bound_evidence_and_gate_are_idempotent() {
        // Evidence/Gate 必须由同一 daemon authority 追加，并绑定 task/step。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let ident = lease_identity("agent-evidence", "evidence-session", "model", "implementer");
        seed_workspace(&store);
        store.handle_task_create(peer.clone(), &serde_json::json!({ "workspace_id": 1,
            "task_id": "T-EVIDENCE-GATE",
            "title": "evidence gate",
            "steps": [{"action": "test", "target_file": "rust_ext/src/daemon/task_collab.rs"}]
        })).unwrap();
        let step_id: String = {
            let conn = store.conn.lock().unwrap();
            conn.query_row(
                "SELECT id FROM task_steps WHERE task_id='T-EVIDENCE-GATE'",
                [],
                |r| r.get(0),
            )
            .unwrap()
        };
        let lease = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-EVIDENCE-GATE", "role": "implementer", "ttl_seconds": 3600.0,
                    "identity": ident
                }),
            )
            .unwrap();
        let evidence_params = serde_json::json!({
            "task_id": "T-EVIDENCE-GATE", "step_id": step_id,
            "evidence_id": "EV-T-EVIDENCE-GATE-1", "evidence_type": "test_run",
            "manifest_path": "docs/evidence/authority-recovery/T-EVIDENCE-GATE.json",
            "payload_hash": "sha256:test-manifest", "request_id": "evidence-1",
            "evidence_json": {"tests": "pass"}, "identity": ident,
            "lease_token": lease["token"], "fencing_counter": lease["fencing_counter"]
        });
        let appended = store
            .handle_evidence_append(peer.clone(), &evidence_params)
            .unwrap();
        assert_eq!(appended["evidence_id"], "EV-T-EVIDENCE-GATE-1");
        let replay = store
            .handle_evidence_append(peer.clone(), &evidence_params)
            .unwrap();
        assert_eq!(replay["evidence_id"], "EV-T-EVIDENCE-GATE-1");
        let gate_params = serde_json::json!({
            "task_id": "T-EVIDENCE-GATE", "step_id": step_id,
            "evidence_id": "EV-T-EVIDENCE-GATE-1", "evidence_hash": "sha256:test-manifest",
            "payload_hash": "sha256:test-manifest", "decision": "pass",
            "reason": "task-bound evidence verified", "request_id": "gate-1",
            "identity": ident, "lease_token": lease["token"],
            "fencing_counter": lease["fencing_counter"]
        });
        let gate = store
            .handle_gate_decision_append(peer.clone(), &gate_params)
            .unwrap();
        assert_eq!(gate["evidence_id"], "EV-T-EVIDENCE-GATE-1");
        let gate_replay = store
            .handle_gate_decision_append(peer.clone(), &gate_params)
            .unwrap();
        assert_eq!(gate_replay["evidence_id"], "EV-T-EVIDENCE-GATE-1");
        let mut mismatch = gate_params.clone();
        mismatch["payload_hash"] = Value::String("sha256:other".into());
        let err = store
            .handle_gate_decision_append(peer, &mismatch)
            .unwrap_err();
        assert_eq!(err.code, "E_REQUEST_ID_REUSE_MISMATCH");
        let conn = store.conn.lock().unwrap();
        let evidence_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_evidence_events WHERE task_id='T-EVIDENCE-GATE'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let gate_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_gate_decisions WHERE task_id='T-EVIDENCE-GATE'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(evidence_count, 1);
        assert_eq!(gate_count, 1);
    }

    #[test]
    fn test_verdict_submit_appends_replays_and_rejects_conflicts() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-VERDICT-NATIVE",
                    "title": "native verdict",
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
                "UPDATE tasks SET status = 'review' WHERE id = 'T-VERDICT-NATIVE'",
                [],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO task_contract_revisions
                 (contract_id, revision, contract_hash, profile, task_id, workspace_id,
                  envelope_payload, created_at, created_by)
                 VALUES ('TC-VERDICT', 1, 'sha256:task-contract', 'review',
                         'T-VERDICT-NATIVE', 1, '{}', 1.0, 'test')",
                [],
            )
            .unwrap();
            let step_id: String = conn
                .query_row(
                    "SELECT id FROM task_steps WHERE task_id = 'T-VERDICT-NATIVE'",
                    [],
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
                     WHERE task_id = 'T-VERDICT-NATIVE' AND role = 'independent_reviewer' AND is_current = 1",
                    [],
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
                "task_id": "T-VERDICT-NATIVE",
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
            &store,
            &peer,
            "agent-native-reviewer",
            "reviewer-instance",
            "reviewer-session",
            "independent_reviewer",
        );
        let identity = lease_identity(
            "agent-native-reviewer",
            "reviewer-session",
            "claude-test",
            "independent_reviewer",
        );
        let lease = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-VERDICT-NATIVE",
                    "role": "reviewer",
                    "identity": identity,
                }),
            )
            .unwrap();
        let verdict_params = serde_json::json!({
            "task_id": "T-VERDICT-NATIVE",
            "step_id": step_id,
            "verdict_id": "V-NATIVE-1",
            "contract_id": "TC-VERDICT",
            "contract_revision": 1,
            "contract_hash": "sha256:task-contract",
            "role_contract_id": role_contract_id,
            "role_contract_revision": role_contract_revision,
            "role_contract_hash": role_contract_hash,
            "phase": "blind_first_pass",
            "view_manifest_hash": "sha256:view",
            "snapshot_id": "snapshot-1",
            "clause_results": [{"clause_id": "C1", "decision": "pass"}],
            "findings": [],
            "overall": "pass",
            "attestation": "reviewed independently",
            "request_id": "verdict-native-request-1",
            "identity": identity,
            "lease_token": lease["token"],
            "fencing_counter": lease["fencing_counter"],
        });
        let first = store
            .handle_verdict_submit(peer.clone(), &verdict_params)
            .unwrap();
        assert_eq!(first["verdict_id"], "V-NATIVE-1");
        assert_eq!(first["replayed"], false);

        let replay = store
            .handle_verdict_submit(peer.clone(), &verdict_params)
            .unwrap();
        assert_eq!(replay["verdict_id"], "V-NATIVE-1");
        assert_eq!(replay["replayed"], true);

        let mut mismatch = verdict_params.clone();
        mismatch["overall"] = Value::String("block".to_string());
        let err = store
            .handle_verdict_submit(peer.clone(), &mismatch)
            .unwrap_err();
        assert_eq!(err.code, "E_REQUEST_ID_REUSE_MISMATCH");

        let mut wrong_role_hash = verdict_params.clone();
        wrong_role_hash["request_id"] = Value::String("verdict-native-request-2".to_string());
        wrong_role_hash["verdict_id"] = Value::String("V-NATIVE-2".to_string());
        wrong_role_hash["role_contract_hash"] = Value::String("sha256:wrong".to_string());
        let err = store
            .handle_verdict_submit(peer, &wrong_role_hash)
            .unwrap_err();
        assert_eq!(err.code, "E_ROLE_CONTRACT_HASH_MISMATCH");

        let conn = store.conn.lock().unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_verdict_events WHERE task_id = 'T-VERDICT-NATIVE'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
        let (task_hash, reviewer_provenance): (String, String) = conn
            .query_row(
                "SELECT contract_hash, reviewer_identity FROM task_verdict_events
                 WHERE verdict_id = 'V-NATIVE-1'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(task_hash, "sha256:task-contract");
        assert!(reviewer_provenance.contains("role_contract"));
        assert!(reviewer_provenance.contains("verdict-native-request-1"));
    }
    // ============================================
    // V1 workspace authority fail-closed 契约测试（E_TASK_WORKSPACE_UNBOUND /
    // E_WORKSPACE_AUTHORITY_MISMATCH / E_IDENTITY_NOT_WIRED）
    // ============================================
