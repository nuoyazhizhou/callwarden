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

Result on clean target worktree `b521a7a8a3ea129337016141dca864a0b2ebb161`: `2 passed, 0 failed`.

Coverage includes:

- missing `view_manifest_hash` is rejected with `invalid_params`;
- whitespace-only `view_manifest_hash` is rejected with `invalid_params`;
- rejected requests leave zero Verdict Ledger rows;
- a non-empty manifest remains accepted and replay-safe;
- native dispatch still routes `verdict.submit` to the handler;

The clean target worktree contains two matching tests for this filter. Additional
shared-worktree tests were not counted because they are unrelated uncommitted
changes and are not part of the target commit.

## Runtime provenance

The patched daemon was built from the clean detached worktree at commit
`b521a7a8a3ea129337016141dca864a0b2ebb161` and deployed with
`scripts/refresh_shared_runtime.ps1`.

- refresh evidence: `C:\Users\wanpi\.callwarden\runtime\evidence\20260830-182053-b521a7a8a3ea-696106e7.json`
- runtime version: `20260830-182053-b521a7a8a3ea-696106e7`
- live PID: `7880`
- executable: `C:\Users\wanpi\.callwarden\runtime\current\cw-daemon.exe`
- SHA-256: `aa1e479ec002023174fc2ad2e9494176d66d9db503a9902e09b242f6baf228de`
- named-pipe endpoint: `\\.\pipe\callwarden-S-1-5-21-1583625257-826939952-3615027596-1001`
- daemon ping: exit code `0`, status `ok`
- rollback: `false`

## Reviewer view provenance

The patched daemon's read-only `get_role_view` for this task and role `reviewer`
returned the real manifest hash:

- `view_manifest_hash`: `5da4de902f28c01c2f9e3016a1ca29acca4904598c169b3be4c76fd057b2f5d9`
- `contract_hash`: `9578790eb26f9f269aa7bac205c6c53c0dfaa925213837eb0c3aad631bd6a8df`
- `degraded`: `false`

## Governance boundary

The prior `V-ce9b6fb191d9fc00baa9cf8a` row is immutable and was not repaired in place. A Reviewer must submit a new task-bound verdict with a real non-empty `view_manifest_hash` after the patched daemon is deployed, then retry the formal `task.handoff`. No `apply` or `close` was performed by the Executor.

## Files

- `rust_ext/src/daemon/task_collab_verdict.rs`
- `rust_ext/src/daemon/task_collab_tests_governance.rs`
- `deliverables/software-company/verdict-view-manifest-evidence.md`
