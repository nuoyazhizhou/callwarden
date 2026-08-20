# Native evidence, gate, and verdict ledger completion

Task: `T-1786983366974-8811ccec-sub-1-sub-1-sub-1`

Role: Executor (`RuntimeRole=implementer`)

## Frozen shared-worktree baseline

- `rust_ext/src/daemon/task_collab.rs`: `98f8733386d93657ff944ab82f27a77f96397fa5`
- `rust_ext/src/daemon/dispatch.rs`: `7c916c9163818593831e99c1c00bd1c712a05b8b`

The files already contained unrelated task lifecycle, evidence, gate, and lease-recovery changes. This task only added the `verdict.submit` handler, its dispatch branch, and focused embedded Rust tests.

## Resulting file hashes

- `rust_ext/src/daemon/task_collab.rs`: `3c5455e13ccb1b3735d0f5bda423ce26cb56bc31`
- `rust_ext/src/daemon/dispatch.rs`: `a876c7bcd0c9b493e1bef2d7c354070ba0298fa6`

## Implemented behavior

- `verdict.submit` now routes to the daemon-native `TaskCollabStore` handler.
- The handler appends only to the existing `task_verdict_events` ledger.
- It requires a complete Reviewer identity plus a valid reviewer lease and fencing counter.
- It validates the exact Task Contract `id/revision/hash`, the current Role Contract `id/revision/canonical hash`, task/step ownership, review status, snapshot, attestation, phase, and canonical overall value.
- `request_id` and a canonical params hash are persisted in structured reviewer provenance. Exact replay returns the existing verdict; changed params return `E_REQUEST_ID_REUSE_MISMATCH`.
- `post_reveal_amendment` must reference the same task's sealed `blind_first_pass` verdict.
- No direct SQLite, Python fallback, historical-row rewrite, apply, or close path was added.

The current schema has no dedicated Role Contract provenance columns or authoritative step-binding table. The handler therefore fail-closes against the existing current `role_contracts` row and stores the independent Role Contract triple in structured reviewer provenance; it does not conflate that hash with the Task Contract `contract_hash` columns.

## Verification

```text
tokenslim run cargo check --manifest-path rust_ext/Cargo.toml --bin cw-daemon
result: PASS (exit 0; existing warnings only)

tokenslim run cargo test --manifest-path rust_ext/Cargo.toml --lib verdict_submit
result: PASS (2 passed)

tokenslim run cargo test --manifest-path rust_ext/Cargo.toml --lib task_bound_evidence_and_gate
result: PASS (1 passed)
```

`git diff --check` reports two trailing-space diagnostics at pre-existing baseline hunks in `task_collab.rs` lines 2443 and 2489. This task did not introduce or modify those lines.

