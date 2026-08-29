# Reviewer BLOCKED provenance remediation

- Task: `T-1788016089952-1647e128`
- Source finding: `T-1788011722055-1b59cb4c` reviewer BLOCKED, provenance references were only checked for non-empty IDs.
- Scope: validate same-task verdict and handoff references, expected outcome, source-step binding, workspace binding, snapshot/manifest presence, and reviewer identity before remediation or review projection.
- Excluded: historical verdict/evidence mutation, T-504 deployment/runtime evidence, P0-L identity policy, CLI/MCP role-session handles, 12-card import, apply, and close.

## Implementation

- `rust_ext/src/daemon/task_collab_lifecycle.rs`
  - remediation creation now requires a real same-task verdict with the expected `block`/`pass` outcome;
  - requires non-empty review snapshot and view-manifest provenance;
  - requires matching task workspace and source-step binding;
  - validates reviewer identity and emits a deterministic handoff event ID into both the handoff envelope and remediation metadata.
- `rust_ext/src/daemon/task_loop/next_action.rs`
  - remediation projection now validates the referenced verdict and structured handoff event instead of accepting non-empty IDs;
  - review projection only routes to `REVISE`/`ADJUDICATE` when snapshot, manifest, workspace, step, and reviewer identity provenance are valid.
- `rust_ext/src/daemon/task_collab_tests_core.rs`
  - fixtures carry the required immutable provenance;
  - task-level BLOCKED coverage proves missing snapshot is rejected before the valid remediation route is accepted.

## Verification

1. `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml task_loop::next_action_test --lib`
   - 20 passed, 0 failed.
2. `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml task_loop::inbound_handoff_test --lib`
   - 8 passed, 0 failed.
3. `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml test_task_level_reviewer_blocked_handoff_creates_fix_defect --lib`
   - 1 passed, 0 failed; missing snapshot rejection included.
4. `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml test_adjudicator_returned_handoff_reopens_executor_remediation_atomically --lib`
   - 1 passed, 0 failed.
5. `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml test_reviewer_blocked_reopens_same_task_for_multiple_revision_rounds --lib`
   - 1 passed, 0 failed.
6. Authority round-trip: official `task.next_action` for this task returned `READY/CLAIM`, exact step `S-1788016089953-16596a38`, and `assignment.status=claimed` for the registered Executor identity.

The broader `task_collab::tests::core` batch also retained two pre-existing baseline failures unrelated to this remediation: `test_task_collab_full_lifecycle` and `test_task_collab_migrates_v46_db_to_v50`.

