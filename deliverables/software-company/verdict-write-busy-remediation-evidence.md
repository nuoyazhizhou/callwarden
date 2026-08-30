# Governance write busy remediation evidence

- Task: `T-1788063720353-e7768bb0`
- Source blocked task: `T-1788055266079-7d76f734`
- Scope: bounded SQLite writer-lock acquisition retry for daemon `task.handoff`
  and `verdict.submit`; no history, verdict, snapshot, or task-state repair.
- Source revision at runtime refresh: `2353ae7036aa53913edf66142c34a249514bdd4a`

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
runtime refresh: 20260830-122811-2353ae7036aa-95807976
daemon PID: 46152
daemon: C:\Users\wanpi\.callwarden\runtime\current\cw-daemon.exe
daemon SHA-256: 905845ab8e727826fde0670b87d4ec67fefb923b983ce9545bf1811d23b91849
health: worker_status=healthy, schema_version=60, git_commit=2353ae7036aa53913edf66142c34a249514bdd4a
ping: status=ok, transport=http, authority_id=LINKPLAY-SCM/windows/S-1-5-21-1583625257-826939952-3615027596-1001/ae618ae38a024ae914850dd9894e41e91d94527a15b813b1a331b7f06c29dc06
```

The live checks were performed through the daemon CLI after refresh. No SQL
direct write, forged reviewer lease, Verdict Ledger entry, or apply/close was
performed.
