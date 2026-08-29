# `adjudicator_returned` remediation route evidence

- Task: `T-1788011722055-1b59cb4c`
- Step: `S-1788011722057-1b7d1930` (implementation), `S-1788011722057-1b7d1930` is the
  currently claimed Executor step; the test and evaluator changes are kept in the same
  task scope.
- Scope: daemon `task.handoff` routing, assignment projection, provenance-gated
  `task.next_action`/claim selection, and focused regression coverage.
- Excluded: T-504 deployment/live runtime evidence, P0-L identity-policy repair,
  historical verdict/evidence mutation, `task.apply`, and `task.close`.

## Implementation

`adjudicator_returned` now reads the latest authoritative `pass` verdict, preserves its
structured findings, creates a deterministic provenance-bound `fix_defect` step, binds
that step to the current Executor Role Contract, and reopens the task in one transaction.
The same transaction completes all active legacy assignments and queues exactly one
Executor assignment for the new pending remediation. Handoff replay remains request-ID
deduplicated and does not rewrite historical verdicts, evidence, or handoff events.

`task.next_action` and cutover `task.claim` only recognize governance remediation when
both `source_verdict_id` and `source_handoff_event_id` are present. A valid remediation is
selected before ordinary pending steps; malformed provenance is not promoted to a claim.

## Verification

1. `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml task_collab::tests::core::test_adjudicator_returned_handoff_reopens_executor_remediation_atomically --lib`
   - PASS: 1 passed, 0 failed.
2. `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml task_loop::next_action_test --lib`
   - PASS: 20 passed, 0 failed.
3. `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml task_collab --lib`
   - Route regression PASS; 97 passed.
   - Existing unrelated baseline failures remain: lifecycle event-count expectation,
     v46 migration count expectation, and orphan-claim assignment expectation.

## Regression assertions

- Source step remains `done` and unchanged.
- New step is `action=fix_defect`, `status=pending`, with source step, source verdict,
  source findings, source handoff event, and source request provenance.
- Replaying the same handoff request returns `replayed=true` and creates no duplicate.
- Active assignment projection contains one `role=executor` assignment for the new
  remediation step; stale Reviewer/Adjudicator/old Executor assignments are completed.
- No child task is created and no historical verdict/evidence is overwritten.
