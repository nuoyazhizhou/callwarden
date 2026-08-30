# P0-L identity policy repair evidence

- task_id: `T-1787801315246-e3e3a08c`
- scope: daemon-only, exact-task, one-time `role_worker_v1` identity-policy repair
- route: `task.p0l_identity_policy_repair`
- source of truth: Rust daemon transaction; no direct SQLite mutation was used

## Implemented safeguards

- Only the frozen P0-L task ID and `p0l_identity_policy_v1` repair code are accepted.
- The request must match the task's immutable workspace binding and authority capture.
- Authorization requires an active, registered adjudicator Role Worker; no legacy identity
  or reviewer lease is accepted as the repair authority.
- The current contract is extended append-only, with previous revision/hash continuity and
  a task event recording the repair provenance.
- The repair is one-shot. The same request ID replays the stored response; a second repair
  request is rejected after the policy is resolved.
- Historical object-shaped contract fields are normalized for the revision validator while
  the original values are preserved under `legacy_contract_fields`.
- The route is a protected mutation and is included in the task operation ledger.

## Verification

Commands run from `C:\git_work\callwarden`:

```text
tokenslim run cargo test --manifest-path rust_ext/Cargo.toml -p callwarden-core --lib p0l_identity_policy_repair
  2 passed; 0 failed

tokenslim run cargo test --manifest-path rust_ext/Cargo.toml -p callwarden-core --lib test_task_next_action_
  2 passed; 0 failed

tokenslim run cargo check --manifest-path rust_ext/Cargo.toml -p callwarden-core --bin cw-daemon --no-default-features
  Finished successfully; pre-existing warnings only
```

`task_collab_contract.rs` remains below the 2,000-line file limit after the repair
normalizer was split into `task_collab_contract_repair.rs`.

## Runtime boundary

The live authority was not mutated from this worktree. The new protected RPC must be
deployed and invoked by an authorized adjudicator Role Worker; raw credentials were not
read or copied into this evidence.
