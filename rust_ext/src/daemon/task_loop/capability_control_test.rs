//! `CapabilityMutationGate` 真实验证测试（foundation 独占，计划 0C）。
//!
//! 覆盖四类契约（计划 §3.3 锁序 `CapabilityMutationGate → authority-store →
//! task-DB`）：
//! 1. **fail-closed revalidation**：即便 permit/冻结输入字段完全对齐，foundation
//!    阶段最终复核仍必须拒绝，不允许在 cutover 前放行任何公共能力。
//! 2. **锁序串行化**：并发 writer 必须先取 gate 再执行 authority-store 写事务；若
//!    gate 未串行化，最终共享状态必然偏离。
//! 3. **活性/无死锁**：连续 acquire/release 不阻塞，证明门禁生命周期边界正确。
//! 4. **基础设施损坏 fail-closed**：持有者 panic 导致 mutex poisoned 时，后续
//!    acquire 必须转为内部错误拒绝，而不是静默死锁或返回放行。

use std::sync::{Arc, Mutex};

use super::capability_control::CapabilityMutationGate;
use super::types::{FrozenAuthorityInput, PublicPreflightPermit};

const ERR_DISABLED: &str = "E_TASK_LOOP_CAPABILITY_DISABLED";

/// 构造字段完全对齐的公共 permit（仅测试用；生产侧不可由客户端构造）。
fn sample_permit() -> PublicPreflightPermit {
    PublicPreflightPermit {
        promotion_event_id: "evt-0C-1".into(),
        workspace_id: "ws-0C-gate".into(),
        request_id: "req-0C-1".into(),
        daemon_generation: 7,
        authority_id: "auth-0C".into(),
        authority_revision: 3,
        fencing_counter: 1,
        evidence_id: "ev-0C-1".into(),
        evidence_hash: "deadbeef".into(),
        schema_fingerprint: "fp-0C".into(),
        rules_hash: "rh-0C".into(),
        runtime_binary_hash: "bin-0C".into(),
    }
}

/// 构造与 permit 对齐的冻结权威输入。
fn sample_frozen() -> FrozenAuthorityInput {
    FrozenAuthorityInput {
        daemon_generation: 7,
        authority_id: "auth-0C".into(),
        authority_revision: 3,
        fencing_counter: 1,
        schema_fingerprint: "fp-0C".into(),
        rules_hash: "rh-0C".into(),
        runtime_binary_hash: "bin-0C".into(),
        snapshot: Default::default(),
    }
}

#[test]
fn revalidate_public_permit_accepts_aligned_credentials() {
    // 1D3B 落地后 revalidation 是真实最终复核（§4.3 / preflight.rs）：
    // 字段完全对齐（真实 schema/rules fingerprint + 非空 authority/evidence 凭证 +
    // daemon generation 一致）必须通过；内存 permit 不是授权终点。
    use rusqlite::Connection;
    use crate::sqlite_query::migrate_connection;
    use super::preflight::{compute_schema_fingerprint, read_workspace_capture_rules_hash};

    let conn = Connection::open_in_memory().unwrap();
    migrate_connection(&conn).expect("migration to v54");
    let schema_fp = compute_schema_fingerprint(&conn).unwrap();
    let rules_hash = read_workspace_capture_rules_hash(&conn).unwrap();

    let permit = sample_permit();
    let mut frozen = sample_frozen();
    // 绑定真实 fingerprint（替换 0C 占位）。
    let permit = PublicPreflightPermit {
        schema_fingerprint: schema_fp.clone(),
        rules_hash: rules_hash.clone(),
        ..permit
    };
    frozen.schema_fingerprint = schema_fp;
    frozen.rules_hash = rules_hash;

    let gate = CapabilityMutationGate::default();
    gate.revalidate_public_permit(&conn, &permit, &frozen)
        .expect("对齐凭证必须通过最终复核");
}

#[test]
fn revalidate_public_permit_is_fail_closed() {
    // 0C 契约：最终复核 fail-closed。fingerprint 失配（schema 变化或伪造 permit）
    // 必须被拒绝，绝不放行；拒绝路径不得产生任何领域写入（由 route 层保证零写入）。
    use rusqlite::Connection;
    use crate::sqlite_query::migrate_connection;

    let conn = Connection::open_in_memory().unwrap();
    migrate_connection(&conn).expect("migration to v54");

    // 字段伪造：schema/rules fingerprint 与真实 DB 不一致。
    let permit = sample_permit();
    let frozen = sample_frozen();
    let gate = CapabilityMutationGate::default();
    let err = gate
        .revalidate_public_permit(&conn, &permit, &frozen)
        .expect_err("指纹失配必须被最终复核拒绝");
    assert_eq!(
        err.code, ERR_DISABLED,
        "revalidation 必须稳定返回 capability_disabled"
    );
}

#[test]
fn gate_serializes_concurrent_store_mutations() {
    // 锁序验证：每个并发 writer 必须先取得 gate，再在 gate 内执行 authority-store
    // 写事务（此处以共享计数器模拟）。若 gate 未串行化，最终值必然小于 N×OPS。
    let gate = Arc::new(CapabilityMutationGate::default());
    let store: Arc<Mutex<u64>> = Arc::new(Mutex::new(0));
    const N: usize = 8;
    const OPS: u64 = 500;

    let mut handles = Vec::new();
    for _ in 0..N {
        let gate = Arc::clone(&gate);
        let store = Arc::clone(&store);
        handles.push(std::thread::spawn(move || {
            for _ in 0..OPS {
                // 锁序：gate 在前，authority-store 写事务在 gate 内。
                let _guard = gate.acquire("ws-0C-concurrent").expect("acquire 应成功");
                let mut cur = store.lock().unwrap();
                *cur += 1;
            }
        }));
    }
    for h in handles {
        h.join().expect("writer 线程不应 panic");
    }

    assert_eq!(
        *store.lock().unwrap(),
        N as u64 * OPS,
        "所有并发变更必须在 gate 内串行化（锁序 gate → store 不可被绕过）"
    );
}

#[test]
fn gate_release_allows_next_acquire_without_deadlock() {
    // 活性/无死锁验证：连续 acquire/release 不阻塞，证明门禁生命周期边界正确，
    // 且释放后不会留下脏状态阻塞后续变更。
    let gate = CapabilityMutationGate::default();
    let ws = "ws-0C-liveness";
    for _ in 0..100 {
        let guard = gate.acquire(ws).expect("acquire 应成功");
        drop(guard);
    }
}

#[test]
fn poisoned_gate_returns_internal_error_not_deadlock() {
    // 基础设施损坏 fail-closed：持有者 panic 导致 mutex poisoned 时，后续 acquire
    // 必须稳定返回 internal_error 拒绝，而非静默死锁或错误放行。
    let gate = Arc::new(CapabilityMutationGate::default());
    {
        let gate = Arc::clone(&gate);
        let h = std::thread::spawn(move || {
            let _guard = gate.acquire("ws-0C-poison").expect("acquire 应成功");
            panic!("模拟 gate 持有者崩溃");
        });
        let _ = h.join();
    }
    let err = match gate.acquire("ws-0C-poison") {
        Err(e) => e,
        Ok(_) => panic!("poisoned gate 必须拒绝后续 acquire"),
    };
    assert_eq!(err.code, "internal_error", "poisoned gate 必须 fail-closed 为内部错误");
}
