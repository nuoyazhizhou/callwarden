# Evidence-Gate remediation for lease cleanup

- Remediation task: `T-1786983366974-8811ccec-sub-1-sub-1`
- Historical implementation task: `T-1786983366974-8811ccec-sub-1`
- Executor: `agent-codex-evidence-remediation-20260818`
- Correct task-owned path: `docs/evidence/authority-recovery/T-1786983366974-8811ccec-sub-1-remediation.md`

## Purpose and immutability boundary

This remediation adds an independently capturable evidence artifact for the
previous lease-cleanup implementation. It does not modify the historical
implementation task, its reports, its evidence hash, its failed/review
records, or any task/step row. It does not use broad `capture-diff` and does
not absorb the shared worktree's unrelated dirty paths.

## Implementation under review

The implementation remains in:

`rust_ext/src/daemon/task_collab.rs`

`handle_lease_acquire` checks an unexpired holder's registration and heartbeat
inside the serialization transaction. Missing, inactive, or stale holders are
expired with an append-only `task_lease_events.event_type=expire` event before
the replacement lease is inserted. A fresh holder returns
`E_LEASE_ACTIVE_EXISTS`. The old fencing counter is retained for the expire
event and the replacement receives the next counter.

## Verification evidence

Command:

```text
cargo test --manifest-path rust_ext/Cargo.toml --lib lease_acquire -- --nocapture
```

Observed result: `5 passed, 0 failed`.

The focused tests cover fresh-holder rejection, stale-heartbeat recovery,
missing-registration recovery, fencing monotonicity, `acquire → expire →
acquire` event ordering, and preservation of task history.

Implementation file SHA-256 at capture time:

```text
093c66e1e15e432e97ff6fb54cece507f2585a0e284320615f0d537f47115d55
```

The implementation file was already dirty from earlier shared-worktree work;
this remediation intentionally records the exact evidence artifact instead of
claiming the whole shared implementation path as a new diff.
