//! task_collab lease、apply/close 和 claim 测试。

use super::*;
use super::support::*;
    #[test]
    fn p0e_adjudicator_can_use_distinct_registered_reviewer_lease_only() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        seed_task_binding(&store, "T-P0E-LEASE");
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let reviewer_registration = serde_json::json!({
            "agent_id":"review-agent", "agent_name":"reviewer", "identity": {"agent_id":"review-agent", "agent_instance_id":"review-inst", "session_id":"review-session", "model_id":"review-model", "role":"reviewer"}
        });
        store
            .handle_agent_register(peer.clone(), &reviewer_registration)
            .unwrap();
        let adjudicator_registration = serde_json::json!({
            "agent_id":"adjudicator-agent", "agent_name":"adjudicator", "identity": {"agent_id":"adjudicator-agent", "agent_instance_id":"adjudicator-inst", "session_id":"adjudicator-session", "model_id":"adjudicator-model", "role":"adjudicator"}
        });
        store
            .handle_agent_register(peer, &adjudicator_registration)
            .unwrap();
        seed_reviewer_lease(
            &store,
            "T-P0E-LEASE",
            "p0e-token",
            7,
            "review-agent",
            "review-session",
            "review-model",
        );
        let adjudicator = parse_action_identity(
            &serde_json::json!({"identity": adjudicator_registration["identity"].clone()}),
        )
        .unwrap()
        .unwrap();
        let conn = store.conn.lock().unwrap();
        store
            .validate_reviewer_lease_for_adjudication(
                &conn,
                "T-P0E-LEASE",
                "p0e-token",
                7,
                &adjudicator,
            )
            .unwrap();

        let same_agent = ActionIdentity {
            agent_id: "review-agent".to_string(),
            agent_instance_id: "other-instance".to_string(),
            client_id: String::new(),
            provider: String::new(),
            model_id: "adj-model".to_string(),
            model_mode: String::new(),
            system_fingerprint: String::new(),
            session_id: "adj-session".to_string(),
            role: "adjudicator".to_string(),
            runtime_hash: String::new(),
        };
        assert_eq!(
            store
                .validate_reviewer_lease_for_adjudication(
                    &conn,
                    "T-P0E-LEASE",
                    "p0e-token",
                    7,
                    &same_agent
                )
                .unwrap_err()
                .code,
            "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_AGENT"
        );
        let same_instance = ActionIdentity {
            agent_id: "other-agent".to_string(),
            agent_instance_id: "review-inst".to_string(),
            client_id: String::new(),
            provider: String::new(),
            model_id: "adj-model".to_string(),
            model_mode: String::new(),
            system_fingerprint: String::new(),
            session_id: "adj-session".to_string(),
            role: "adjudicator".to_string(),
            runtime_hash: String::new(),
        };
        assert_eq!(
            store
                .validate_reviewer_lease_for_adjudication(
                    &conn,
                    "T-P0E-LEASE",
                    "p0e-token",
                    7,
                    &same_instance
                )
                .unwrap_err()
                .code,
            "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_INSTANCE"
        );
        let same_session = ActionIdentity {
            agent_id: "other-agent".to_string(),
            agent_instance_id: "other-instance".to_string(),
            client_id: String::new(),
            provider: String::new(),
            model_id: "adj-model".to_string(),
            model_mode: String::new(),
            system_fingerprint: String::new(),
            session_id: "review-session".to_string(),
            role: "adjudicator".to_string(),
            runtime_hash: String::new(),
        };
        assert_eq!(
            store
                .validate_reviewer_lease_for_adjudication(
                    &conn,
                    "T-P0E-LEASE",
                    "p0e-token",
                    7,
                    &same_session
                )
                .unwrap_err()
                .code,
            "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_SESSION"
        );
        assert_eq!(
            store
                .validate_reviewer_lease_for_adjudication(
                    &conn,
                    "T-P0E-LEASE",
                    "wrong-token",
                    7,
                    &adjudicator
                )
                .unwrap_err()
                .code,
            "E_LEASE_TOKEN_MISMATCH"
        );
        assert_eq!(
            store
                .validate_reviewer_lease_for_adjudication(
                    &conn,
                    "T-P0E-LEASE",
                    "p0e-token",
                    6,
                    &adjudicator
                )
                .unwrap_err()
                .code,
            "E_LEASE_FENCING_STALE"
        );
    }

    #[test]
    fn p0e_adjudication_reviewer_lease_negative_holder_and_expiry() {
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        seed_task_binding(&store, "T-P0E-NEG");
        let peer = PeerCredential::new_unix(1000, 1000, 1234);

        // 注册一个真实 reviewer holder（将被不规范/不活跃变体复用审计）。
        let reviewer_reg = serde_json::json!({
            "agent_id":"neg-review", "agent_name":"reviewer",
            "identity": {"agent_id":"neg-review","agent_instance_id":"neg-review-inst",
            "session_id":"neg-session","model_id":"neg-model","role":"reviewer"}
        });
        store.handle_agent_register(peer.clone(), &reviewer_reg).unwrap();
        let adjudicator_reg = serde_json::json!({
            "agent_id":"neg-adj", "agent_name":"adjudicator",
            "identity": {"agent_id":"neg-adj","agent_instance_id":"neg-adj-inst",
            "session_id":"neg-adj-session","model_id":"neg-adj-model","role":"adjudicator"}
        });
        store.handle_agent_register(peer, &adjudicator_reg).unwrap();
        let adjudicator = parse_action_identity(
            &serde_json::json!({"identity": adjudicator_reg["identity"].clone()}),
        ).unwrap().unwrap();

        // (b) 未注册 holder -> E_GOVERNANCE_REVIEWER_UNREGISTERED
        seed_reviewer_lease(&store, "T-P0E-NEGB", "tok-b", 1, "unregistered-agent", "sess-b", "m-b");
        {
            let conn = store.conn.lock().unwrap();
            assert_eq!(
                store.validate_reviewer_lease_for_adjudication(
                    &conn, "T-P0E-NEGB", "tok-b", 1, &adjudicator,
                ).unwrap_err().code,
                "E_GOVERNANCE_REVIEWER_UNREGISTERED"
            );
        }

        // (c) holder 已注册但 role != reviewer -> E_GOVERNANCE_REVIEWER_INVALID
        let non_rev = serde_json::json!({
            "agent_id":"imp-agent","agent_name":"implementer",
            "identity":{"agent_id":"imp-agent","agent_instance_id":"imp-inst",
            "session_id":"sess-c","model_id":"m-c","role":"implementer"}
        });
        store.handle_agent_register(PeerCredential::new_unix(1000,1000,200), &non_rev).unwrap();
        seed_reviewer_lease(&store, "T-P0E-NEGC", "tok-c", 1, "imp-agent", "sess-c", "m-c");
        {
            let conn = store.conn.lock().unwrap();
            assert_eq!(
                store.validate_reviewer_lease_for_adjudication(
                    &conn, "T-P0E-NEGC", "tok-c", 1, &adjudicator,
                ).unwrap_err().code,
                "E_GOVERNANCE_REVIEWER_INVALID"
            );
        }

        // (d) holder session 与注册不匹配 -> E_GOVERNANCE_REVIEWER_INVALID
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "INSERT INTO task_leases (workspace_id, lease_id, task_id, role, agent_id, session_id, model_id, token_hash, fencing_counter, acquired_at, expires_at, status)
                 VALUES (1, 'L-neg-d', 'T-P0E-NEGD', 'reviewer', 'neg-review', 'wrong-session', 'neg-model', ?1, 1, 1700000000.0, 1893456000.0, 'active')",
                rusqlite::params![sha256_hex("tok-d".as_bytes())],
            ).unwrap();
            assert_eq!(
                store.validate_reviewer_lease_for_adjudication(
                    &conn, "T-P0E-NEGD", "tok-d", 1, &adjudicator,
                ).unwrap_err().code,
                "E_GOVERNANCE_REVIEWER_INVALID"
            );
        }

        // (e) holder 不活跃 -> E_GOVERNANCE_REVIEWER_INVALID
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE agent_registrations SET status='inactive' WHERE agent_id='neg-review'",
                [],
            ).unwrap();
            conn.execute(
                "INSERT INTO task_leases (workspace_id, lease_id, task_id, role, agent_id, session_id, model_id, token_hash, fencing_counter, acquired_at, expires_at, status)
                 VALUES (1, 'L-neg-e', 'T-P0E-NEGE', 'reviewer', 'neg-review', 'neg-session', 'neg-model', ?1, 1, 1700000000.0, 1893456000.0, 'active')",
                rusqlite::params![sha256_hex("tok-e".as_bytes())],
            ).unwrap();
            assert_eq!(
                store.validate_reviewer_lease_for_adjudication(
                    &conn, "T-P0E-NEGE", "tok-e", 1, &adjudicator,
                ).unwrap_err().code,
                "E_GOVERNANCE_REVIEWER_INVALID"
            );
        }

        // (f) release daemon 时钟当作待续期已完成，校验无泄漏。
        let _ = store;
    }

    #[test]
    fn p0e_validate_reviewer_lease_uses_plain_identity_path() {
        // 普通同一 role 校验（validate_lease_for_mutation）不受 P0-E 影响，
        // 回归占位：跨角色方法只接受 adjudicator role。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        seed_task_binding(&store, "T-P0E-ROLE");
        seed_reviewer_lease(&store, "T-P0E-ROLE", "tok", 1, "some-reviewer", "sess", "m");
        let impersonator = ActionIdentity {
            agent_id: "adj".to_string(),
            agent_instance_id: "adj-inst".to_string(),
            client_id: String::new(),
            provider: String::new(),
            model_id: "m".to_string(),
            model_mode: String::new(),
            system_fingerprint: String::new(),
            session_id: "adj-sess".to_string(),
            role: "reviewer".to_string(),
            runtime_hash: String::new(),
        };
        {
            let conn = store.conn.lock().unwrap();
            assert_eq!(
                store.validate_reviewer_lease_for_adjudication(
                    &conn, "T-P0E-ROLE", "tok", 1, &impersonator,
                ).unwrap_err().code,
                "E_GOVERNANCE_ADJUDICATOR_ROLE_REQUIRED"
            );
        }
    }

    // ============================================
    // M7: Lease Control Plane（Req 11.2-11.9, 14.11, 14.30）

    // ============================================

    #[test]
    fn test_lease_acquire_returns_raw_token_once_and_stores_hash() {
        // Req 11.2/11.3：acquire 成功返回 raw token（仅此一次）+ fencing_counter=1；
        // DB 只存 sha256(token_hash)，绝不存 raw token。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);

        let res = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-1",
                    "role": "implementer",
                    "ttl_seconds": 3600.0,
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();
        assert_eq!(res["task_id"], "T-LEASE-1");
        assert_eq!(res["role"], "implementer");
        assert_eq!(res["fencing_counter"], 1);
        let raw_token = res["token"].as_str().unwrap().to_string();
        assert!(!raw_token.is_empty(), "raw token 必须返回");

        // DB 只存 hash
        let conn = store.conn.lock().unwrap();
        let (lease_id, token_hash, counter): (String, String, i64) = conn
            .query_row(
                "SELECT lease_id, token_hash, fencing_counter FROM task_leases
                 WHERE workspace_id = 1 AND task_id = 'T-LEASE-1' AND status = 'active'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .unwrap();
        assert_ne!(token_hash, raw_token, "DB 不得存 raw token");
        assert_eq!(token_hash, sha256_hex(raw_token.as_bytes()));
        assert_eq!(counter, 1);
        assert!(lease_id.starts_with("L-"));
        drop(conn);
    }

    #[test]
    fn test_lease_acquire_blocks_double_active() {
        // Req 11.2 防双活：存在未过期 active lease 时 acquire → E_LEASE_ACTIVE_EXISTS
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let params = serde_json::json!({
            "task_id": "T-LEASE-2",
            "role": "implementer",
            "ttl_seconds": 3600.0,
            "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
        });
        register_agent_with_identity(
            &store,
            &peer,
            "agent-a",
            "session-a-instance",
            "session-a",
            "implementer",
        );
        store.handle_lease_acquire(peer.clone(), &params).unwrap();

        let err = store.handle_lease_acquire(peer, &params).unwrap_err();
        assert_eq!(err.code, "E_LEASE_ACTIVE_EXISTS");
    }

    #[test]
    fn test_lease_acquire_recovers_stale_holder_before_expiry() {
        // 异常退出恢复：TTL 尚未到期，但 holder 心跳 stale 时，acquire 必须在同一
        // 事务中追加 expire 审计、回收旧 lease，再发放递增 fencing counter。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        register_agent_with_identity(
            &store,
            &peer,
            "agent-stale",
            "stale-instance",
            "stale-session",
            "implementer",
        );
        let old_params = serde_json::json!({
            "task_id": "T-LEASE-STALE",
            "role": "implementer",
            "ttl_seconds": 3600.0,
            "identity": lease_identity("agent-stale", "stale-session", "model-a", "implementer"),
        });
        store
            .handle_lease_acquire(peer.clone(), &old_params)
            .unwrap();
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE agent_registrations SET last_heartbeat = 1.0
                 WHERE agent_id = 'agent-stale' AND session_id = 'stale-session'",
                [],
            )
            .unwrap();
        }

        let new_params = serde_json::json!({
            "task_id": "T-LEASE-STALE",
            "role": "implementer",
            "ttl_seconds": 3600.0,
            "identity": lease_identity("agent-new", "new-session", "model-b", "implementer"),
        });
        let new_lease = store.handle_lease_acquire(peer, &new_params).unwrap();
        assert_eq!(new_lease["fencing_counter"], 2);

        let conn = store.conn.lock().unwrap();
        let statuses: Vec<String> = {
            let mut stmt = conn
                .prepare(
                    "SELECT status FROM task_leases WHERE task_id = 'T-LEASE-STALE' ORDER BY id",
                )
                .unwrap();
            let rows = stmt.query_map([], |r| r.get::<_, String>(0)).unwrap();
            rows.collect::<rusqlite::Result<Vec<_>>>().unwrap()
        };
        assert_eq!(statuses, vec!["expired", "active"]);
        let event_types: Vec<String> = {
            let mut stmt = conn
                .prepare("SELECT event_type FROM task_lease_events WHERE task_id = 'T-LEASE-STALE' ORDER BY id")
                .unwrap();
            let rows = stmt.query_map([], |r| r.get::<_, String>(0)).unwrap();
            rows.collect::<rusqlite::Result<Vec<_>>>().unwrap()
        };
        assert_eq!(event_types, vec!["acquire", "expire", "acquire"]);
    }

    #[test]
    fn test_lease_acquire_recovers_missing_holder_registration() {
        // 进程异常退出后注册记录可能已丢失；缺失 owner registration 视为 orphan，
        // 但仍只通过 acquire 的单一写事务回收，不修改任何 task/step 历史事件。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let old_params = serde_json::json!({
            "task_id": "T-LEASE-MISSING",
            "role": "implementer",
            "ttl_seconds": 3600.0,
            "identity": lease_identity("agent-missing", "missing-session", "model-a", "implementer"),
        });
        store
            .handle_lease_acquire(peer.clone(), &old_params)
            .unwrap();
        let new_params = serde_json::json!({
            "task_id": "T-LEASE-MISSING",
            "role": "implementer",
            "ttl_seconds": 3600.0,
            "identity": lease_identity("agent-new", "new-session", "model-b", "implementer"),
        });
        let new_lease = store.handle_lease_acquire(peer, &new_params).unwrap();
        assert_eq!(new_lease["fencing_counter"], 2);
        let conn = store.conn.lock().unwrap();
        let task_events: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events WHERE task_id = 'T-LEASE-MISSING'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(task_events, 0, "lease orphan recovery不得改写 task 历史");
    }

    #[test]
    fn test_lease_acquire_expired_then_reacquire_increments_counter() {
        // Req 11.3：旧 active lease 过期后 acquire 将其置 expired 并创建新 lease，counter 单调递增
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let params = serde_json::json!({
            "task_id": "T-LEASE-3",
            "role": "reviewer",
            "ttl_seconds": 3600.0,
            "identity": lease_identity("agent-a", "session-a", "model-a", "reviewer"),
        });
        store.handle_lease_acquire(peer.clone(), &params).unwrap();

        // 人为把 lease 置为过期
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE task_leases SET expires_at = 1.0 WHERE task_id = 'T-LEASE-3'",
                [],
            )
            .unwrap();
        }
        let res = store.handle_lease_acquire(peer, &params).unwrap();
        assert_eq!(res["fencing_counter"], 2, "过期后重新获取 counter 递增");

        // 旧 lease 已置 expired
        let conn = store.conn.lock().unwrap();
        let statuses: Vec<String> = {
            let mut stmt = conn
                .prepare("SELECT status FROM task_leases WHERE task_id = 'T-LEASE-3' ORDER BY id")
                .unwrap();
            let rows = stmt.query_map([], |r| r.get::<_, String>(0)).unwrap();
            rows.collect::<rusqlite::Result<Vec<_>>>().unwrap()
        };
        assert_eq!(statuses, vec!["expired", "active"]);
        drop(conn);
    }

    #[test]
    fn test_lease_extend_renews_and_keeps_counter() {
        // Req 11.5：extend 幂等续期——expires_at 前进、renewed_at 写入、fencing_counter 不变
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);

        let acq = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-4",
                    "role": "implementer",
                    "ttl_seconds": 3600.0,
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();
        let raw_token = acq["token"].as_str().unwrap().to_string();
        let expires_before = acq["expires_at"].as_f64().unwrap();

        let ext = store
            .handle_lease_extend(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-4",
                    "role": "implementer",
                    "token": raw_token,
                    "ttl_seconds": 7200.0,
                    "fencing_counter": 1,
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();
        assert_eq!(ext["fencing_counter"], 1, "extend 不得递增 counter");
        let expires_after = ext["expires_at"].as_f64().unwrap();
        assert!(expires_after > expires_before, "续期后 expires_at 前进");

        let conn = store.conn.lock().unwrap();
        let renewed_at: Option<f64> = conn
            .query_row(
                "SELECT renewed_at FROM task_leases WHERE task_id = 'T-LEASE-4'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(renewed_at.is_some(), "renewed_at 已写入");
        drop(conn);
    }

    #[test]
    fn test_lease_extend_rejects_bad_token() {
        // Req 11.9：错误 token → E_LEASE_TOKEN_MISMATCH
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-5",
                    "role": "implementer",
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();

        let err = store
            .handle_lease_extend(
                peer,
                &serde_json::json!({
                    "task_id": "T-LEASE-5",
                    "role": "implementer",
                    "token": "wrong-token",
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_TOKEN_MISMATCH");
    }

    #[test]
    fn test_lease_extend_rejects_expired() {
        // Req 11.4：过期 lease 续租 → E_LEASE_EXPIRED
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let acq = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-6",
                    "role": "implementer",
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();
        let raw_token = acq["token"].as_str().unwrap().to_string();
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "UPDATE task_leases SET expires_at = 1.0 WHERE task_id = 'T-LEASE-6'",
                [],
            )
            .unwrap();
        }

        let err = store
            .handle_lease_extend(
                peer,
                &serde_json::json!({
                    "task_id": "T-LEASE-6",
                    "role": "implementer",
                    "token": raw_token,
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_EXPIRED");
    }

    #[test]
    fn test_lease_extend_rejects_stale_fencing() {
        // Property 11：旧持有者携带过期 counter 续租 → E_LEASE_FENCING_STALE
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let acq = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-7",
                    "role": "implementer",
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();
        let raw_token = acq["token"].as_str().unwrap().to_string();

        let err = store
            .handle_lease_extend(
                peer,
                &serde_json::json!({
                    "task_id": "T-LEASE-7",
                    "role": "implementer",
                    "token": raw_token,
                    "fencing_counter": 99,
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_FENCING_STALE");
    }

    #[test]
    fn test_lease_release_and_idempotent() {
        // Req 11.6/11.7：release 置 released；重复 release（同 token）幂等返回 idempotent=true
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let acq = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-8",
                    "role": "implementer",
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();
        let raw_token = acq["token"].as_str().unwrap().to_string();

        let rel = store
            .handle_lease_release(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-8",
                    "role": "implementer",
                    "token": raw_token,
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();
        assert_eq!(rel["status"], "released");

        // 幂等：重复 release 返回同一 released 状态（不创建新 lease、不报错）
        let rel2 = store
            .handle_lease_release(
                peer,
                &serde_json::json!({
                    "task_id": "T-LEASE-8",
                    "role": "implementer",
                    "token": raw_token,
                }),
            )
            .unwrap();
        assert_eq!(rel2["status"], "released");
        assert_eq!(rel2["idempotent"], true);

        let conn = store.conn.lock().unwrap();
        let lease_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_leases WHERE task_id = 'T-LEASE-8'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(lease_count, 1, "幂等 release 不得创建新 lease");
        drop(conn);
    }

    #[test]
    fn test_lease_status_hides_raw_token_and_lists_events() {
        // Req 11.2：status 含 token_hash 不含 raw token；list_events 返回 acquire/renew/release 审计事件
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        let acq = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-9",
                    "role": "implementer",
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap();
        let raw_token = acq["token"].as_str().unwrap().to_string();
        store
            .handle_lease_extend(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-9",
                    "role": "implementer",
                    "token": raw_token,
                }),
            )
            .unwrap();
        store
            .handle_lease_release(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-9",
                    "role": "implementer",
                    "token": raw_token,
                }),
            )
            .unwrap();

        // status：只读，不含 raw token 字段
        let status = store
            .handle_lease_status(
                peer.clone(),
                &serde_json::json!({"task_id": "T-LEASE-9", "role": "implementer"}),
            )
            .unwrap();
        assert_eq!(status["status"], "released");
        assert!(status.get("token").is_none(), "status 不得暴露 raw token");
        assert!(
            status.get("token_hash").is_some(),
            "status 保留 token_hash 供受保护校验"
        );
        assert!(status["lease_id"].as_str().unwrap().starts_with("L-"));

        // list_events：append-only 顺序（acquire → renew → release），不含 raw token
        let events = store
            .handle_lease_list_events(peer, &serde_json::json!({"task_id": "T-LEASE-9"}))
            .unwrap();
        let arr = events.as_array().unwrap();
        let types: Vec<&str> = arr
            .iter()
            .map(|e| e["event_type"].as_str().unwrap())
            .collect();
        assert_eq!(types, vec!["acquire", "renew", "release"]);
        for e in arr.iter() {
            assert!(e.get("token").is_none(), "事件不得含 raw token");
            assert!(e.get("actor_agent_id").is_some());
        }
    }

    #[test]
    fn test_lease_clock_unavailable_fail_closed() {
        // Req 14.30：store 未注入 AuthoritativeClock 时 Lease 写操作一律 E_LEASE_CLOCK_UNAVAILABLE
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap(); // 未 with_clock
        seed_workspace(&store);
        let peer = PeerCredential::new_unix(1000, 1000, 1234);

        let err = store
            .handle_lease_acquire(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-LEASE-10",
                    "role": "implementer",
                    "identity": lease_identity("agent-a", "session-a", "model-a", "implementer"),
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_CLOCK_UNAVAILABLE");

        // fail-closed：拒绝后无任何 lease 记录（不降级、不落库）
        let conn = store.conn.lock().unwrap();
        let cnt: i64 = conn
            .query_row("SELECT COUNT(*) FROM task_leases", [], |r| r.get(0))
            .unwrap();
        assert_eq!(cnt, 0, "clock fail-closed 后不得落库");
        drop(conn);
    }

    #[test]
    fn test_task_close_rejects_open_children() {
        // S1: 父任务含 open 子任务时禁止关闭（需先通过 reviewer lease 门禁）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-PARENT", "", "review", false);
        seed_task(&store, "T-CHILD", "T-PARENT", "open", true);
        seed_reviewer_lease(
            &store, "T-PARENT", "tok-p", 1, "agent-r", "sess-r", "model-r",
        );

        let err = store
            .handle_task_close(peer, &serde_json::json!({"task_id": "T-PARENT", "lease_token": "tok-p", "fencing_counter": 1}))
            .unwrap_err();
        assert_eq!(err.code, "E_CHILD_TASKS_NOT_CLOSED");

        // 拒绝后任务状态不变（未写入 closed）
        let conn = store.conn.lock().unwrap();
        let status: String = conn
            .query_row("SELECT status FROM tasks WHERE id = 'T-PARENT'", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(status, "review");
    }

    #[test]
    fn test_task_close_rejects_children_in_review_applied() {
        // S1: 子任务 review/applied/in_progress 均视为未关闭，父任务禁止 close
        for child_status in ["review", "applied", "in_progress"] {
            let (_dir, db_path) = temp_db();
            let store = TaskCollabStore::new(&db_path)
                .unwrap()
                .with_clock(Arc::new(AuthoritativeClock::new()));
            let peer = PeerCredential::new_unix(1000, 1000, 1234);
            seed_task(&store, "T-PARENT", "", "review", false);
            seed_task(&store, "T-CHILD", "T-PARENT", child_status, true);
            seed_reviewer_lease(
                &store, "T-PARENT", "tok-p", 1, "agent-r", "sess-r", "model-r",
            );

            let err = store
                .handle_task_close(peer, &serde_json::json!({"task_id": "T-PARENT", "lease_token": "tok-p", "fencing_counter": 1}))
                .unwrap_err();
            assert_eq!(
                err.code, "E_CHILD_TASKS_NOT_CLOSED",
                "子任务状态 {} 应阻止父任务关闭",
                child_status
            );
        }
    }

    #[test]
    fn test_task_close_allows_parent_after_all_children_closed() {
        // S1+S5: 所有子任务 closed 后父任务才允许 close，且 closed_at 非零
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-PARENT", "", "review", false);
        seed_task(&store, "T-C1", "T-PARENT", "closed", true);
        seed_task(&store, "T-C2", "T-PARENT", "closed", true);
        seed_reviewer_lease(
            &store, "T-PARENT", "tok-p", 1, "agent-r", "sess-r", "model-r",
        );

        let res = store
            .handle_task_close(peer, &serde_json::json!({"task_id": "T-PARENT", "lease_token": "tok-p", "fencing_counter": 1}))
            .unwrap();
        assert_eq!(res["status"], "closed");

        let conn = store.conn.lock().unwrap();
        let closed_at: f64 = conn
            .query_row(
                "SELECT closed_at FROM tasks WHERE id = 'T-PARENT'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(closed_at > 0.0, "closed_at 应为真实非零时间戳");
    }

    #[test]
    fn test_task_close_rejects_zero_steps() {
        // S2: 空步骤普通任务不能 close
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-LEAF", "", "applied", false);
        seed_reviewer_lease(&store, "T-LEAF", "tok-l", 1, "agent-r", "sess-r", "model-r");

        let err = store
            .handle_task_close(peer, &serde_json::json!({"task_id": "T-LEAF", "lease_token": "tok-l", "fencing_counter": 1}))
            .unwrap_err();
        assert_eq!(err.code, "E_NO_STEPS");
    }

    #[test]
    fn test_task_close_rejects_pending_steps() {
        // S2: steps 含 pending/failed/blocked 不能 close
        for bad_status in ["pending", "failed", "blocked"] {
            let (_dir, db_path) = temp_db();
            let store = TaskCollabStore::new(&db_path)
                .unwrap()
                .with_clock(Arc::new(AuthoritativeClock::new()));
            let peer = PeerCredential::new_unix(1000, 1000, 1234);
            seed_task(&store, "T-LEAF", "", "applied", true);
            seed_reviewer_lease(&store, "T-LEAF", "tok-l", 1, "agent-r", "sess-r", "model-r");
            {
                let conn = store.conn.lock().unwrap();
                conn.execute(
                    "INSERT INTO task_steps (id, task_id, step_index, action, status, result, created_at)
                     VALUES (?1, 'T-LEAF', 1, 'verify', ?2, '', 1700000000.0)",
                    params![format!("T-LEAF-bad-{}", bad_status), bad_status],
                )
                .unwrap();
            }

            let err = store
                .handle_task_close(peer, &serde_json::json!({"task_id": "T-LEAF", "lease_token": "tok-l", "fencing_counter": 1}))
                .unwrap_err();
            assert_eq!(
                err.code, "E_STEPS_NOT_DONE",
                "步骤状态 {} 应阻止关闭",
                bad_status
            );
        }
    }

    #[test]
    fn test_task_close_success_writes_closed_at() {
        // S5: 叶子任务全部步骤 done 后 close 成功，closed_at 非零
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-LEAF", "", "applied", true);
        seed_reviewer_lease(&store, "T-LEAF", "tok-l", 1, "agent-r", "sess-r", "model-r");

        let res = store
            .handle_task_close(peer, &serde_json::json!({"task_id": "T-LEAF", "lease_token": "tok-l", "fencing_counter": 1}))
            .unwrap();
        assert_eq!(res["status"], "closed");
        let closed_at = res["closed_at"].as_f64().unwrap();
        assert!(closed_at > 0.0, "closed_at 应为真实非零时间戳");

        // task_events 记录 closed 状态变迁
        let conn = store.conn.lock().unwrap();
        let closed_events: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events WHERE task_id = 'T-LEAF' AND to_status = 'closed'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(closed_events, 1);
    }

    #[test]
    fn test_task_apply_close_lease_clock_unavailable_fail_closed() {
        // S3: lease clock 不可用（store 未注入时钟）时，携带 lease 凭证的 apply/close 均 fail-closed
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-L", "", "review", true);

        let lease_params =
            serde_json::json!({"task_id": "T-L", "lease_token": "tok", "fencing_counter": 1});
        let err = store
            .handle_task_apply(peer.clone(), &lease_params)
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_CLOCK_UNAVAILABLE");

        // 校验失败后任务状态未被改变（fail-closed，不降级）
        let status: String = {
            let conn = store.conn.lock().unwrap();
            conn.query_row("SELECT status FROM tasks WHERE id = 'T-L'", [], |r| {
                r.get(0)
            })
            .unwrap()
        };
        assert_eq!(status, "review");

        let err = store.handle_task_close(peer, &lease_params).unwrap_err();
        assert_eq!(err.code, "E_LEASE_CLOCK_UNAVAILABLE");
    }

    #[test]
    fn test_task_close_lease_validated_with_clock() {
        // S3: 注入时钟 + 存在 active lease 时按凭证校验；任一失败在写入前拒绝
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-L", "", "applied", true);

        let raw_token = "secret-token";
        let token_hash = sha256_hex(raw_token.as_bytes());
        {
            let conn = store.conn.lock().unwrap();
            // task_leases 的 workspace_id 有 FK -> workspaces(id)，先补一条 id=1 的测试工作区
            conn.execute(
                "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (1, 'test-ws', '/tmp/test-ws', ?1)",
                params![1_700_000_000.0_f64],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO task_leases (workspace_id, lease_id, task_id, role, agent_id, session_id, model_id, token_hash, fencing_counter, acquired_at, expires_at, status)
                 VALUES (1, 'L-TEST', 'T-L', 'reviewer', 'agent-a', 'session-a', 'model-a', ?1, 1, 1700000000.0, 1893456000.0, 'active')",
                params![token_hash],
            )
            .unwrap();
        }

        // token 不匹配
        let err = store
            .handle_task_close(
                peer.clone(),
                &serde_json::json!({"task_id": "T-L", "lease_token": "wrong", "fencing_counter": 1}),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_TOKEN_MISMATCH");

        // fencing counter 过期（旧持有者）
        let err = store
            .handle_task_close(
                peer.clone(),
                &serde_json::json!({"task_id": "T-L", "lease_token": raw_token, "fencing_counter": 2}),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_FENCING_STALE");

        // 凭证正确 → close 成功
        let res = store
            .handle_task_close(
                peer,
                &serde_json::json!({"task_id": "T-L", "lease_token": raw_token, "fencing_counter": 1}),
            )
            .unwrap();
        assert_eq!(res["status"], "closed");
    }

    #[test]
    fn test_task_apply_requires_lease_credentials() {
        // S3 强制门禁：daemon 权威路径下 task.apply 缺少/不完整 lease 凭证 → E_LEASE_REQUIRED，
        // 禁止沿用"缺凭证即跳过校验"的兼容行为；失败不改变 task data。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-A", "", "review", true);

        // 无 lease_token
        let err = store
            .handle_task_apply(
                peer.clone(),
                &serde_json::json!({"task_id": "T-A", "fencing_counter": 1}),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // 无 fencing_counter
        let err = store
            .handle_task_apply(
                peer.clone(),
                &serde_json::json!({"task_id": "T-A", "lease_token": "tok"}),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // 只提供 lease_token（缺 fencing_counter）
        let err = store
            .handle_task_apply(
                peer.clone(),
                &serde_json::json!({"task_id": "T-A", "lease_token": "tok"}),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // fencing_counter 非整数（类型不完整）
        let err = store
            .handle_task_apply(peer.clone(), &serde_json::json!({"task_id": "T-A", "lease_token": "tok", "fencing_counter": "1"}))
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // 空 lease_token
        let err = store
            .handle_task_apply(
                peer,
                &serde_json::json!({"task_id": "T-A", "lease_token": "", "fencing_counter": 1}),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // 失败后任务状态不变（未写入 applied）
        let conn = store.conn.lock().unwrap();
        let status: String = conn
            .query_row("SELECT status FROM tasks WHERE id = 'T-A'", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(status, "review");
    }

    #[test]
    fn test_task_apply_writes_applied_at() {
        // 观察#1 回归：daemon 权威路径 task.apply 必须回填 tasks.applied_at 列，
        // 与 Python db_tasks.task_apply（line 1990）对齐；否则 auto/enterprise 模式下
        // applied_at 恒为 NULL，破坏审计轨迹与父子级联语义。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        // seed_task 不写 applied_at（列默认 NULL），正好用于验证 apply 后回填。
        seed_task(&store, "T-APPLY", "", "review", true);
        seed_reviewer_lease(
            &store,
            "T-APPLY",
            "tok-apply",
            1,
            "agent-r",
            "sess-r",
            "model-r",
        );

        let res = store
            .handle_task_apply(
                peer,
                &serde_json::json!({"task_id": "T-APPLY", "lease_token": "tok-apply", "fencing_counter": 1}),
            )
            .unwrap();
        assert_eq!(res["status"], "applied");

        // 响应层 applied_at 非零
        let applied_at_resp = res["applied_at"].as_f64().unwrap();
        assert!(applied_at_resp > 0.0, "响应 applied_at 应为真实非零时间戳");

        // DB 行 applied_at 已被回填（非空、非零）
        let conn = store.conn.lock().unwrap();
        let applied_at_db: f64 = conn
            .query_row(
                "SELECT applied_at FROM tasks WHERE id = 'T-APPLY'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(
            applied_at_db > 0.0,
            "tasks.applied_at 应在 daemon apply 后被写入非空值"
        );
    }

    #[test]
    fn test_task_close_requires_lease_credentials() {
        // S3 强制门禁：daemon 权威路径下 task.close 缺少/不完整 lease 凭证 → E_LEASE_REQUIRED
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path)
            .unwrap()
            .with_clock(Arc::new(AuthoritativeClock::new()));
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-A", "", "applied", true);

        // 无 lease_token
        let err = store
            .handle_task_close(
                peer.clone(),
                &serde_json::json!({"task_id": "T-A", "fencing_counter": 1}),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // 无 fencing_counter
        let err = store
            .handle_task_close(
                peer.clone(),
                &serde_json::json!({"task_id": "T-A", "lease_token": "tok"}),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // 只提供 lease_token（缺 fencing_counter）
        let err = store
            .handle_task_close(
                peer.clone(),
                &serde_json::json!({"task_id": "T-A", "lease_token": "tok"}),
            )
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // fencing_counter 非整数（类型不完整）
        let err = store
            .handle_task_close(peer, &serde_json::json!({"task_id": "T-A", "lease_token": "tok", "fencing_counter": "1"}))
            .unwrap_err();
        assert_eq!(err.code, "E_LEASE_REQUIRED");

        // 失败后任务状态不变（未写入 closed）
        let conn = store.conn.lock().unwrap();
        let status: String = conn
            .query_row("SELECT status FROM tasks WHERE id = 'T-A'", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(status, "applied");
    }

    #[test]
    fn test_completion_review_zero_steps_blocked() {
        // S4: 零步骤普通任务 completion-review 返回 blocked，不能 vacuous pass
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_task(&store, "T-NS", "", "applied", false);

        let res = store
            .handle_task_completion_review(peer, &serde_json::json!({"task_id": "T-NS"}))
            .unwrap();
        assert_eq!(res["decision"], "blocked");
        assert_eq!(res["reason"], "E_NO_STEPS");
    }

    // ============================================
    // 任务 E（T-1786438019310）：task.create_subtask 漏写 steps + claim 返回步骤详情契约
    // ============================================

    #[test]
    fn test_task_create_subtask_persists_steps_and_step_count() {
        // S1: create_subtask 必须接收 steps 并写入 task_steps，返回 step_count
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,"task_id": "T-SUB-PARENT", "title": "parent"}),
            )
            .unwrap();

        let res = store
            .handle_task_create_subtask(
                peer,
                &serde_json::json!({
                    "parent_task_id": "T-SUB-PARENT",
                    "title": "child",
                    "description": "child desc",
                    "steps": [
                        {
                            "action": "audit",
                            "target_file": "rust_ext/src/daemon/task_collab.rs",
                            "target_symbol": "TaskCollabStore::handle_task_create_subtask",
                            "check_items": ["read code", "verify"],
                        },
                        {
                            "action": "fix",
                            "target_file": "server/tools/tools_task.py",
                            "check_items": "pytest",
                        },
                    ],
                }),
            )
            .unwrap();
        assert_eq!(res["status"], "open");
        assert_eq!(res["parent_id"], "T-SUB-PARENT");
        assert_eq!(res["step_count"], 2);

        // 步骤完整写入且绑定正确 task_id
        let conn = store.conn.lock().unwrap();
        let task_id = res["task_id"].as_str().unwrap();
        let mut stmt = conn
            .prepare(
                "SELECT task_id, step_index, action, target_file, target_symbol, check_items, status, id
                 FROM task_steps WHERE task_id = ?1 ORDER BY step_index",
            )
            .unwrap();
        let rows: Vec<(String, i64, String, String, String, String, String, String)> = stmt
            .query_map(params![task_id], |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                    r.get(6)?,
                    r.get(7)?,
                ))
            })
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].0, task_id);
        assert_eq!(rows[0].1, 0);
        assert_eq!(rows[0].2, "audit");
        assert_eq!(rows[0].3, "rust_ext/src/daemon/task_collab.rs");
        assert_eq!(rows[0].4, "TaskCollabStore::handle_task_create_subtask");
        assert_eq!(rows[0].5, "[\"read code\",\"verify\"]");
        assert_eq!(rows[0].6, "pending");
        assert!(
            rows[0].7.starts_with("S-"),
            "step_id 必须是真实生成 id: {}",
            rows[0].7
        );
        assert_eq!(rows[1].1, 1);
        assert_eq!(rows[1].2, "fix");
        assert_eq!(rows[1].3, "server/tools/tools_task.py");
        assert_eq!(rows[1].5, "pytest");

        // tasks 表 description 已保存
        let desc: String = conn
            .query_row(
                "SELECT description FROM tasks WHERE id = ?1",
                params![task_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(desc, "child desc");
    }

    #[test]
    fn test_task_create_subtask_rolls_back_on_invalid_steps() {
        // S2: steps 非 array 时整体回滚，不留下半成品子任务/步骤/事件
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,"task_id": "T-SUB-ROLLBACK", "title": "parent"}),
            )
            .unwrap();

        let err = store
            .handle_task_create_subtask(
                peer,
                &serde_json::json!({
                    "parent_task_id": "T-SUB-ROLLBACK",
                    "title": "bad",
                    "steps": "not-an-array",
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "invalid_params");

        let conn = store.conn.lock().unwrap();
        let children: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM tasks WHERE parent_id = 'T-SUB-ROLLBACK'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let steps: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_steps ts JOIN tasks t ON t.id = ts.task_id
                 WHERE t.parent_id = 'T-SUB-ROLLBACK'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let events: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events WHERE reason_code = 'subtask_created'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(children, 0, "回滚后不应残留子任务");
        assert_eq!(steps, 0, "回滚后不应残留步骤");
        assert_eq!(events, 0, "回滚后不应残留 subtask_created 事件");
    }

    #[test]
    fn test_task_claim_returns_step_details_contract() {
        // S3: task.claim 返回下一步骤详情（step_id/step_index/action/target_file/target_symbol/
        // check_items/step_status/task_title），与 Python db.task_next_step 契约对齐。
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CLAIM-STEPS",
                    "title": "claim with steps",
                    "steps": [
                        {
                            "action": "audit",
                            "target_file": "rust_ext/src/daemon/task_collab.rs",
                            "target_symbol": "TaskCollabStore::handle_task_claim",
                            "check_items": ["read"],
                        },
                        {"action": "fix", "target_file": "server/tools/tools_task.py"},
                    ],
                }),
            )
            .unwrap();

        let claim = store
            .handle_task_claim(
                peer,
                &serde_json::json!({"task_id": "T-CLAIM-STEPS", "agent_session_id": "session-claim"}),
            )
            .unwrap();
        assert_eq!(claim["status"], "in_progress");
        assert_eq!(claim["claimed_by"], "session-claim");
        assert!(
            claim["step_id"].as_str().unwrap().starts_with("S-"),
            "claim 必须返回真实 step_id"
        );
        assert_eq!(claim["step_index"], 0);
        assert_eq!(claim["action"], "audit");
        assert_eq!(claim["target_file"], "rust_ext/src/daemon/task_collab.rs");
        assert_eq!(claim["target_symbol"], "TaskCollabStore::handle_task_claim");
        assert_eq!(claim["check_items"], "[\"read\"]");
        assert_eq!(claim["step_status"], "in_progress");
        assert_eq!(claim["task_title"], "claim with steps");
    }

    #[test]
    fn test_task_claim_without_steps_omits_step_fields() {
        // S4: 无步骤任务的 claim 返回 {task_id, status, claimed_by}，不含 step_* 字段（兼容旧契约）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,"task_id": "T-CLAIM-NOSTEPS", "title": "no steps"}),
            )
            .unwrap();

        let claim = store
            .handle_task_claim(
                peer,
                &serde_json::json!({"task_id": "T-CLAIM-NOSTEPS", "agent_session_id": "s"}),
            )
            .unwrap();
        assert_eq!(claim["status"], "in_progress");
        assert_eq!(claim["claimed_by"], "s");
        assert!(claim.get("step_id").is_none(), "无步骤任务不应返回 step_id");
    }

    #[test]
    fn test_task_claim_dedup_idempotent() {
        // S5: 同一 request_id 重复 claim 返回缓存结果（dedup 语义保留）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CLAIM-DEDUP",
                    "title": "dedup",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                }),
            )
            .unwrap();

        let params = serde_json::json!({
            "request_id": "req-dedup-1",
            "task_id": "T-CLAIM-DEDUP",
            "agent_session_id": "session-d",
        });
        let first = store.handle_task_claim(peer.clone(), &params).unwrap();
        assert!(first["step_id"].as_str().unwrap().starts_with("S-"));
        let second = store.handle_task_claim(peer.clone(), &params).unwrap();
        assert_eq!(first, second, "同 request_id 重复调用必须幂等返回缓存");
    }

    #[test]
    fn test_task_claim_marks_step_in_progress() {
        // S6: claim 必须把首个 pending 步骤改为 in_progress（与 Python db.task_next_step 契约对齐）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CLAIM-STEPSTATE",
                    "title": "step state",
                    "steps": [
                        {"action": "audit", "target_file": "a.rs"},
                        {"action": "fix", "target_file": "b.rs"},
                    ],
                }),
            )
            .unwrap();

        let claim = store
            .handle_task_claim(
                peer,
                &serde_json::json!({"task_id": "T-CLAIM-STEPSTATE", "agent_session_id": "s6"}),
            )
            .unwrap();
        assert_eq!(claim["step_index"], 0, "应领取 step_index=0 的步骤");
        assert_eq!(claim["step_status"], "in_progress");

        let conn = store.conn.lock().unwrap();
        let statuses: Vec<String> = conn
            .prepare("SELECT status FROM task_steps WHERE task_id = ?1 ORDER BY step_index ASC")
            .unwrap()
            .query_map(params!["T-CLAIM-STEPSTATE"], |r| r.get(0))
            .unwrap()
            .map(|r| r.unwrap())
            .collect();
        drop(conn);
        assert_eq!(
            statuses,
            vec!["in_progress", "pending"],
            "首个步骤应 in_progress，其余保持 pending"
        );
    }

    #[test]
    fn test_task_claim_resume_same_session_returns_in_progress_step() {
        // S7: 同 session 再次 claim 返回已 in_progress 的步骤（恢复语义，不重复占用新步骤）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CLAIM-RESUME",
                    "title": "resume",
                    "steps": [{"action": "audit", "target_file": "a.rs"}, {"action": "fix", "target_file": "b.rs"}],
                }),
            )
            .unwrap();

        let first = store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({"task_id": "T-CLAIM-RESUME", "agent_session_id": "s-resume"}),
            )
            .unwrap();
        let second = store
            .handle_task_claim(
                peer,
                &serde_json::json!({"task_id": "T-CLAIM-RESUME", "agent_session_id": "s-resume"}),
            )
            .unwrap();

        assert_eq!(
            first["step_id"], second["step_id"],
            "同 session 恢复必须返回同一步骤"
        );
        assert_eq!(first["step_index"], second["step_index"]);
        assert_eq!(second["step_status"], "in_progress");
    }

    #[test]
    fn test_task_claim_concurrent_session_conflict() {
        // S8: 已被其他 session claim 的 in_progress 任务，不同 session 再次 claim 必须拒绝（并发 claim 冲突）
        let (_dir, db_path) = temp_db();
        let store = TaskCollabStore::new(&db_path).unwrap();
        let peer = PeerCredential::new_unix(1000, 1000, 1234);
        seed_workspace(&store);
        store
            .handle_task_create(
                peer.clone(),
                &serde_json::json!({ "workspace_id": 1,
                    "task_id": "T-CLAIM-CONFLICT",
                    "title": "conflict",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                }),
            )
            .unwrap();

        store
            .handle_task_claim(
                peer,
                &serde_json::json!({"task_id": "T-CLAIM-CONFLICT", "agent_session_id": "agent-a"}),
            )
            .unwrap();

        let err = store
            .handle_task_claim(
                peer,
                &serde_json::json!({"task_id": "T-CLAIM-CONFLICT", "agent_session_id": "agent-b"}),
            )
            .unwrap_err();
        assert_eq!(
            err.code, "task_conflict",
            "不同 session 并发 claim 必须拒绝: {}",
            err
        );
    }

    #[test]
    fn test_same_role_stale_claim_is_taken_over_atomically() {
        // P0-G：同治理角色的新 agent 可在 daemon authoritative clock 证明旧
        // owner stale 后直接接管；不需要 reviewer lease，也不重置已占用步骤。
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
                    "task_id": "T-CLAIM-STALE-SAME-ROLE",
                    "title": "same role takeover",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                }),
            )
            .unwrap();

        register_agent_with_identity(
            &store,
            &peer,
            "old-executor",
            "old-executor-inst",
            "old-executor-sess",
            "executor",
        );
        let old_identity = serde_json::json!({
            "agent_id": "old-executor",
            "agent_instance_id": "old-executor-inst",
            "client_id": "test",
            "provider": "test",
            "model_id": "model-old",
            "session_id": "old-executor-sess",
            "role": "executor",
        });
        store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-CLAIM-STALE-SAME-ROLE",
                    "agent_session_id": "old-executor-sess",
                    "identity": old_identity,
                }),
            )
            .unwrap();
        store
            .conn
            .lock()
            .unwrap()
            .execute(
                "UPDATE agent_registrations SET last_heartbeat = 0 WHERE agent_id = 'old-executor'",
                [],
            )
            .unwrap();

        register_agent_with_identity(
            &store,
            &peer,
            "new-executor",
            "new-executor-inst",
            "new-executor-sess",
            "executor",
        );
        let taken = store
            .handle_task_claim(
                peer,
                &serde_json::json!({
                    "task_id": "T-CLAIM-STALE-SAME-ROLE",
                    "agent_session_id": "new-executor-sess",
                    "identity": {
                        "agent_id": "new-executor",
                        "agent_instance_id": "new-executor-inst",
                        "client_id": "test",
                        "provider": "test",
                        "model_id": "model-new",
                        "session_id": "new-executor-sess",
                        "role": "executor",
                    },
                }),
            )
            .unwrap();

        assert_eq!(taken["claim_recovered"], serde_json::json!(true));
        assert_eq!(taken["previous_claim_session"], "old-executor-sess");
        assert_eq!(taken["step_status"], "in_progress");
        let conn = store.conn.lock().unwrap();
        assert_eq!(
            store.get_task_claim_info(&conn, "T-CLAIM-STALE-SAME-ROLE"),
            (
                Some("1000".to_string()),
                Some("new-executor-sess".to_string())
            )
        );
        let recovery_events: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ?1 AND reason_code = 'claim_recovered'",
                params!["T-CLAIM-STALE-SAME-ROLE"],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(
            recovery_events, 1,
            "接管必须追加一条 append-only recovery event"
        );
    }

    #[test]
    fn test_same_role_fresh_claim_remains_conflict() {
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
                    "task_id": "T-CLAIM-FRESH-SAME-ROLE",
                    "title": "fresh same role conflict",
                    "steps": [{"action": "implement", "target_file": "a.rs"}],
                }),
            )
            .unwrap();
        register_agent_with_identity(
            &store,
            &peer,
            "fresh-old",
            "fresh-old-inst",
            "fresh-old-sess",
            "executor",
        );
        store
            .handle_task_claim(
                peer.clone(),
                &serde_json::json!({
                    "task_id": "T-CLAIM-FRESH-SAME-ROLE",
                    "agent_session_id": "fresh-old-sess",
                    "identity": {
                        "agent_id": "fresh-old", "agent_instance_id": "fresh-old-inst",
                        "client_id": "test", "provider": "test", "model_id": "model-old",
                        "session_id": "fresh-old-sess", "role": "executor",
                    },
                }),
            )
            .unwrap();
        register_agent_with_identity(
            &store,
            &peer,
            "fresh-new",
            "fresh-new-inst",
            "fresh-new-sess",
            "executor",
        );
        let err = store
            .handle_task_claim(
                peer,
                &serde_json::json!({
                    "task_id": "T-CLAIM-FRESH-SAME-ROLE",
                    "agent_session_id": "fresh-new-sess",
                    "identity": {
                        "agent_id": "fresh-new", "agent_instance_id": "fresh-new-inst",
                        "client_id": "test", "provider": "test", "model_id": "model-new",
                        "session_id": "fresh-new-sess", "role": "executor",
                    },
                }),
            )
            .unwrap_err();
        assert_eq!(err.code, "task_conflict");
    }
