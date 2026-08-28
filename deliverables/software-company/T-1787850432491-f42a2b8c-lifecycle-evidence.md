# T-1787850432491-f42a2b8c — task_collab lifecycle domain evidence

## Task binding

- task_id: `T-1787850432491-f42a2b8c`
- step_id: `S-1787850432491-f4338f74`
- scope: `rust_ext/src/daemon/task_collab_lifecycle.rs`
- role: executor

## Change

Extracted the report, remediation creation, step resolution, and task handoff
handlers from `task_collab.rs` into `task_collab_lifecycle.rs`. The extraction
preserves the existing `TaskCollabStore` method surface, transaction boundaries,
provenance checks, status transitions, and error semantics.

## Static verification

| File | Lines |
|---|---:|
| `rust_ext/src/daemon/task_collab_lifecycle.rs` | 1603 |
| `rust_ext/src/daemon/task_collab.rs` | 14019 |

The extracted module remains below the 2000-line per-file target.

## Tests

All commands were run through the project TokenSlim wrapper and exited with code 0:

- `tokenslim run cargo check`
- `tokenslim run cargo test task_collab::tests::test_failed_report`
- `tokenslim run cargo test task_collab::tests::test_reviewer_blocked`
- `tokenslim run cargo test task_collab::tests::test_executor_handoff`

The focused lifecycle tests passed; compiler output contained only pre-existing or
non-fatal unused-code warnings.

## Known environment limitation

The required pre-commit database refresh remains blocked in this checkout:
`python C:/git_work/callwarden/cw.py --refresh-all` returns daemon
`method_not_found: build_full_graph`, while targeted evidence refresh returns
`FOREIGN KEY constraint failed`. No database fallback or direct SQLite write was
used.
