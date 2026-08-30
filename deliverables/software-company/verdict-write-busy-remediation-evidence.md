# Governance write busy remediation evidence

- Task: `T-1788063720353-e7768bb0`
- Source blocked task: `T-1788055266079-7d76f734`
- Scope: bounded SQLite writer-lock acquisition retry for daemon `task.handoff`
  and `verdict.submit`; no history, verdict, snapshot, or task-state repair.
- Source revision at runtime refresh: `2a360dff3a7a7745c9a53bf6e91a3a4bd1b9774c`

## Implementation

`begin_immediate_with_retry` classifies only SQLite `DatabaseBusy` and
`DatabaseLocked` at `BEGIN IMMEDIATE`. It retries twice with bounded backoff,
then returns a structured internal error. The callback/handler is not entered
until the writer transaction is acquired, so retrying cannot duplicate an
append-only event or verdict. The helper is used by the native `verdict.submit`
and structured `task.handoff` handlers only.

## Focused verification

```text
cargo test --manifest-path rust_ext/Cargo.toml begin_immediate_ --lib
2 passed; 0 failed

cargo test --manifest-path rust_ext/Cargo.toml daemon::task_collab::tests::governance::test_verdict_submit --lib
2 passed; 0 failed
```

The complete governance module ran 17 tests: 16 passed and 1 pre-existing
`test_orphan_claim_recovery_requires_stale_owner_and_preserves_step_state`
failure (`task_conflict` during stale-claim recovery). That failure is outside
the changed transaction entry points and is not represented as a pass.

The new contention tests cover both lock release before a retry and bounded
failure while the writer lock remains held. The existing verdict tests cover
append, idempotent replay, request conflict, and Role Contract rejection.

## Runtime provenance

```text
runtime refresh: 20260830-123905-2a360dff3a7a-0bb1af11
daemon PID: 3884
daemon: C:\Users\wanpi\.callwarden\runtime\current\cw-daemon.exe
daemon SHA-256: 071256aa494738aaf5724f832ca536bcf4281f96ddbb7305f71ca57a55718b81
health: worker_status=healthy, schema_version=60, git_commit=2a360dff3a7a7745c9a53bf6e91a3a4bd1b9774c
ping: status=ok, transport=http, authority_id=LINKPLAY-SCM/windows/S-1-5-21-1583625257-826939952-3615027596-1001/8ddc6657e1a9cdfedfa4470f6adaae4b865e6f19f5a51f2bd958b8347cfa0302
```

The live checks were performed through the daemon CLI after refresh. No SQL
direct write, forged reviewer lease, Verdict Ledger entry, or apply/close was
performed.
