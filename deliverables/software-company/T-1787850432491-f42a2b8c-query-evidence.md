# T-1787850432491-f42a2b8c — query and domain extraction evidence

## Task binding

- task_id: `T-1787850432491-f42a2b8c`
- step_id: `S-1787850432491-f433abf8`
- scope: `rust_ext/src/daemon/`
- role: executor

## Extraction result

The remaining `task_collab.rs` handlers were moved into domain modules while
keeping the existing `TaskCollabStore` API and daemon dispatch method names:

- `task_collab_query.rs`: task status/reconciliation/events, wait/list, status tree,
  symbol-change and commit queries (1096 lines).
- `task_collab_evidence.rs`: evidence and Gate handlers (593 lines).
- `task_collab_verdict.rs`: verdict ledger handler (438 lines).
- `task_collab_governance.rs`: role view, freshness, dependency and provider queries
  (1240 lines).
- `task_collab_identity.rs`: action identity, session separation and freshness helpers
  (528 lines).
- `task_collab_lifecycle_ops.rs`: rollback/reopen (498 lines).
- `task_collab_lifecycle_apply.rs`: apply/close/capture diff (367 lines).
- `task_collab_planning.rs`: split/plan/completion/quality/subtask handlers (383 lines).
- `task_collab_symbol.rs`: symbol attribution handlers and helpers (329 lines).

Each extracted code file is below the 2000-line limit. The root module retains
shared helpers, store construction, task creation, and tests for the next test
extraction step.

## Verification

Command: `tokenslim run cargo check`

Result: **passed** (exit code 0). Existing compiler warnings remain non-fatal;
no new compile error was introduced by the module split.

## Environment limitation

The required database refresh remains unavailable: `cw --refresh-all` reports
daemon `method_not_found: build_full_graph`, and targeted evidence refresh reports
`FOREIGN KEY constraint failed`. No SQLite fallback was used.
