# Verdict `view_manifest_hash` provenance remediation

- Remediation task: `T-1788079398046-26c63824`
- Source governance task: `T-1788078550140-bb9d6d44`
- Source finding: `verdict.submit` accepted a missing or whitespace-only `view_manifest_hash`, allowing a Verdict Ledger row that a later `task.handoff(reviewer_pass)` correctly rejected.

## Scope

The daemon verdict handler now parses `view_manifest_hash` with the same non-empty required-field gate as `snapshot_id`. The check runs before lease validation, transaction creation, and any Verdict Ledger write. Historical verdict rows are not modified or deleted.

## Verification

Command:

```text
tokenslim run cargo test --manifest-path rust_ext/Cargo.toml verdict_submit --lib
```

Result: `3 passed, 0 failed`.

Coverage includes:

- missing `view_manifest_hash` is rejected with `invalid_params`;
- whitespace-only `view_manifest_hash` is rejected with `invalid_params`;
- rejected requests leave zero Verdict Ledger rows;
- a non-empty manifest remains accepted and replay-safe;
- native dispatch still routes `verdict.submit` to the handler;
- new-schema Role Contract provenance remains accepted.

## Governance boundary

The prior `V-ce9b6fb191d9fc00baa9cf8a` row is immutable and was not repaired in place. A Reviewer must submit a new task-bound verdict with a real non-empty `view_manifest_hash` after the patched daemon is deployed, then retry the formal `task.handoff`. No `apply` or `close` was performed by the Executor.

## Files

- `rust_ext/src/daemon/task_collab_verdict.rs`
- `rust_ext/src/daemon/task_collab_tests_governance.rs`
- `deliverables/software-company/verdict-view-manifest-evidence.md`
