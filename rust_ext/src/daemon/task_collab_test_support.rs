//! task_collab 测试共享 fixture 与治理 worker helper。
//! 测试代码只复用 daemon public/internal API，不直接修改生产数据库。

use super::*;
    pub(crate) fn temp_db() -> (tempfile::TempDir, std::path::PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test_collab.db");
        (dir, db_path)
    }
    pub(crate) fn seed_task(
        store: &TaskCollabStore,
        id: &str,
        parent_id: &str,
        status: &str,
        with_done_step: bool,
    ) {
        let ts = 1_700_000_000.0;
        let conn = store.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at, parent_id)
             VALUES (?1, ?2, '', 'agent', ?3, ?4, ?4, ?5)",
            params![id, format!("task {}", id), status, ts, parent_id],
        )
        .unwrap();
        if with_done_step {
            conn.execute(
                "INSERT INTO task_steps (id, task_id, step_index, action, target_file, target_symbol, check_items, status, result, created_at, completed_at)
                 VALUES (?1, ?2, 0, 'verify', '', '', '', 'done', 'ok', ?3, ?3)",
                params![format!("{}-s1", id), id, ts],
            )
            .unwrap();
        }
        drop(conn);
    }

    /// 建一条测试 workspace（id=1，is_active=1），并为 lease 测试直接以 task_id
    /// 调用的 handler 预置 capture + 不可变 binding（与 v1 workspace authority 契约一致）。
    pub(crate) fn seed_workspace(store: &TaskCollabStore) {
        let conn = store.conn.lock().unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) VALUES (1, 'test-ws', '/tmp/test-ws', ?1, 1)",
            params![1_700_000_000.0_f64],
        )
        .unwrap();
        drop(conn);
        for tid in [
            "T-LEASE-1",
            "T-LEASE-2",
            "T-LEASE-3",
            "T-LEASE-4",
            "T-LEASE-5",
            "T-LEASE-6",
            "T-LEASE-7",
            "T-LEASE-8",
            "T-LEASE-9",
            "T-LEASE-10",
            "T-LEASE-MISSING",
            "T-LEASE-STALE",
        ] {
            seed_task_binding(store, tid);
        }
    }

    /// 为测试 task 写入 capture + 不可变 binding（workspace 1，幂等）。
    ///
    /// BR-01：capture 必须是 workspace 1 的**合法权威**——instance 统一为 ws-inst-test，
    /// registry_identity_hash 用 workspace-capture-c14n/v1 真实计算（与 create 路径逐字节
    /// 一致），revision 递增规避 UNIQUE(workspace_id, instance, hash, revision) 冲突。
    /// 这样既有 create 测试（workspace 1 + ws-inst-test）的配对与 identity 校验都命中。
    pub(crate) fn seed_task_binding(store: &TaskCollabStore, task_id: &str) {
        let ts = 1_700_000_000.0_f64;
        let conn = store.conn.lock().unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO tasks (id, title, description, creator, status, created_at, updated_at, parent_id)
             VALUES (?1, 'test task', '', 'test', 'open', ?2, ?2, '')",
            params![task_id, ts],
        )
        .unwrap();
        // 与 bind_task_to_workspace（task_collab.rs）对 workspace 1 的计算完全一致：
        // root_path=/tmp/test-ws、name=test-ws、manifest 结构 workspace-manifest-c14n/v1。
        let root_hash = crate::canonicalize::sha256_hex("/tmp/test-ws".as_bytes());
        let manifest_payload = serde_json::json!({
            "workspace_id": 1,
            "workspace_name": "test-ws",
            "root_path_hash": root_hash,
            "manifest_format_version": "workspace-manifest-c14n/v1",
        });
        let manifest_payload_json = manifest_payload.to_string();
        let manifest_hash = crate::canonicalize::sha256_hex(manifest_payload_json.as_bytes());
        let identity_hash = crate::daemon::task_loop::create::registry_identity_hash(
            "ws-inst-test",
            &root_hash,
            &root_hash,
            &manifest_hash,
        );
        let registry_payload = serde_json::json!({
            "workspace_instance_id": "ws-inst-test",
            "client_view_root_hash": root_hash,
            "host_real_root_hash": root_hash,
            "workspace_manifest_hash": manifest_hash,
        })
        .to_string();
        let revision: i64 = conn
            .query_row(
                "SELECT COALESCE(MAX(capture_revision), 0) + 1 \
                 FROM workspace_authority_captures WHERE workspace_id = 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO workspace_authority_captures
             (workspace_capture_id, workspace_id, capture_revision, supersedes_capture_id,
              daemon_workspace_id, workspace_instance_id, capture_canonicalization_version,
              capture_canonicalization_rules_hash, registry_identity_payload_json,
              registry_identity_hash, workspace_manifest_payload_json, workspace_manifest_hash,
              client_view_root_hash, host_real_root_hash, created_by, authoritative_created_at)
             VALUES (?1, 1, ?2, NULL, 0, 'ws-inst-test', 'workspace-capture-c14n/v1',
                     'test-rules-hash', ?3, ?4, ?5, ?6, ?7, ?7, 'test', ?8)",
            params![
                format!("cap-test-{}", task_id),
                revision,
                registry_payload,
                identity_hash,
                manifest_payload_json,
                manifest_hash,
                root_hash,
                ts,
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO task_workspace_bindings
             (task_id, workspace_id, workspace_binding_id, workspace_capture_id, created_by, authoritative_created_at)
             VALUES (?1, 1, ?2, ?3, 'test', ?4)",
            params![
                task_id,
                format!("tb-test-{}", task_id),
                format!("cap-test-{}", task_id),
                ts,
            ],
        )
        .unwrap();
        drop(conn);
    }

    /// 为测试任务 seed 一条 active reviewer lease（含 workspace FK），供 apply/close 门禁测试使用。
    pub(crate) fn seed_reviewer_lease(
        store: &TaskCollabStore,
        task_id: &str,
        raw_token: &str,
        counter: i64,
        agent: &str,
        session: &str,
        model: &str,
    ) {
        let conn = store.conn.lock().unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) VALUES (1, 'test-ws', '/tmp/test-ws', ?1, 1)",
            params![1_700_000_000.0_f64],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO task_leases (workspace_id, lease_id, task_id, role, agent_id, session_id, model_id, token_hash, fencing_counter, acquired_at, expires_at, status)
             VALUES (1, ?1, ?2, 'reviewer', ?3, ?4, ?5, ?6, ?7, 1700000000.0, 1893456000.0, 'active')",
            params![
                format!("L-{}", task_id),
                task_id,
                agent,
                session,
                model,
                sha256_hex(raw_token.as_bytes()),
                counter,
            ],
        )
        .unwrap();
        drop(conn);
    }

    pub(crate) fn lease_identity(agent: &str, session: &str, model: &str, role: &str) -> serde_json::Value {
        serde_json::json!({
            "agent_id": agent,
            "session_id": session,
            "model_id": model,
            "role": role,
        })
    }

    pub(crate) fn register_agent_with_identity(
        store: &TaskCollabStore,
        peer: &PeerCredential,
        agent_id: &str,
        instance_id: &str,
        session_id: &str,
        role: &str,
    ) -> Value {
        store
            .handle_agent_register(
                peer.clone(),
                &serde_json::json!({
                    "agent_id": agent_id,
                    "agent_name": format!("agent-{}", agent_id),
                    "capabilities": ["code"],
                    "identity": {
                        "agent_id": agent_id,
                        "agent_instance_id": instance_id,
                        "client_id": "trae",
                        "provider": "anthropic",
                        "model_id": "claude-test",
                        "model_mode": "agent",
                        "system_fingerprint": "fp-1",
                        "session_id": session_id,
                        "role": role,
                        "runtime_hash": "deadbeef",
                    },
                }),
            )
            .unwrap()
    }

    pub(crate) fn p0l_governance_roles() -> serde_json::Value {
        serde_json::json!([
            {"role":"executor", "independence":"{}"},
            {"role":"reviewer", "independence":"{}"},
            {"role":"adjudicator", "independence":"{}"}
        ])
    }

    pub(crate) fn p0l_task_exists(store: &TaskCollabStore, task_id: &str) -> bool {
        let conn = store.conn.lock().unwrap();
        conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM tasks WHERE id = ?1)",
            [task_id],
            |row| row.get::<_, bool>(0),
        )
        .unwrap()
    }

    pub(crate) fn p0l_enroll_worker(
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

    pub(crate) fn p0l_role_worker_auth(
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
    pub(crate) fn p0l_seed_governance_task(
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

    pub(crate) fn p0l_role_worker_envelope(task_id: &str) -> serde_json::Value {
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
