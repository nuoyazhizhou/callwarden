# Formal governance write round-trip remediation evidence

- Task: `T-1788065933399-2b3cead8`
- Source blocked task: `T-1788063720353-e7768bb0`
- Root cause: the previous retry helper was wired into `task.report`, while
  `handle_task_handoff` still used `unchecked_transaction()`; the formal
  Reviewer handoff therefore bypassed the retry path.
- Fix commit: `2a6906d4883dbc102955179479a1bd7fdb92cff9`

## Implementation

The actual `handle_task_handoff` writer transaction now uses
`begin_immediate_with_retry`. The helper classifies only SQLite
`DatabaseBusy`/`DatabaseLocked`, retries before the transaction is acquired
with bounded backoff `[100, 300, 750, 1500]` ms, and never replays a handler
after a writer transaction has been obtained. `verdict.submit` uses the same
helper.

## Verification

```text
focused begin_immediate tests: 2 passed / 0 failed
governance module: 20 tests, 19 passed / 1 failed
baseline failure: test_orphan_claim_recovery_requires_stale_owner_and_preserves_step_state
  task_conflict during stale-claim recovery; unrelated to this change
```

The formal daemon CLI `task.handoff` round-trip completed after deployment:

```text
first request_id: executor-T-1788065933399-2b3cead8-ready-r1
first response: status=review, event_id=6223, replayed=false
same request replay: status=review, event_id=6223, replayed=false
task.next-action/task show: one prior_handoff with event_id=6223; no duplicate event
```

The daemon returned the same event id on the identical request and the
projection retained one handoff, proving idempotent no-duplicate persistence.

## Runtime provenance

```text
runtime refresh: 20260830-130802-9b895162d77c-bf4f8a39
daemon PID: 9060
daemon SHA-256: 946e8576db498de86496d82f27fd36534bff2e304e3729e027694c9ae2f6074b
health/ping: passed; transport=http; schema_version=60
source HEAD at refresh: 9b895162d77c4d2af8e799cb6ecaee7af3aed812
```

No direct SQL write, forged verdict/snapshot/lease, historical mutation, or
apply/close was performed.
