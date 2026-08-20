# Lease cleanup ledger authority remediation

## Native evidence, gate, and verdict ledger completion

- target: rust_ext/src/daemon/task_collab.rs, rust_ext/src/daemon/dispatch.rs
- Implement the daemon-native verdict.submit path against the existing task_verdict_events ledger, preserve append-only/idempotent semantics, and keep evidence.append and gate.decision.append on the same authority path.
- Add focused Rust tests for evidence append, gate binding, verdict append/replay/conflict, and method routing; record task-bound evidence under docs/evidence/authority-recovery/.

Forbidden: direct SQLite writes, modifying historical tasks/evidence/verdicts, changing unrelated transport behavior, broad capture, local fallback, apply/close.
