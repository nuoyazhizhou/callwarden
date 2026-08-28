# T-1787850432491-f42a2b8c — task_collab lease domain evidence

## Task binding

- task_id: `T-1787850432491-f42a2b8c`
- step_id: `S-1787850432491-f4336abc`
- scope: `rust_ext/src/daemon/task_collab_lease.rs`
- role: executor

## Change

Extracted the claim/work-next/assignment/stale-recovery and lease lifecycle methods from
`task_collab.rs` into `task_collab_lease.rs`. The module keeps the existing
`TaskCollabStore` methods, SQL, transaction boundaries, fencing checks, and error
semantics; `task_collab.rs` imports it as a private path module without changing the
daemon dispatch API.

## Static verification

| File | Lines |
|---|---:|
| `rust_ext/src/daemon/task_collab_lease.rs` | 1887 |
| `rust_ext/src/daemon/task_collab.rs` | 15614 |

The extracted module remains below the 2000-line per-file target.

## Tests

Command: `tokenslim run cargo test task_collab::tests::test_lease`

Result: **14 passed, 0 failed** (1541 filtered). Covered lease acquire, renewal,
release/idempotence, fencing/token failures, workspace/binding/clock fail-closed
paths, stale-holder recovery, and status/event projection without raw token output.

## Known environment limitation

The required pre-commit database refresh could not be completed in this checkout:
`python C:/git_work/callwarden/cw.py --refresh-all` returned daemon
`method_not_found: build_full_graph`; a targeted evidence refresh returned
`FOREIGN KEY constraint failed`. This is recorded as an authority/runtime blocker,
not treated as a successful refresh.
