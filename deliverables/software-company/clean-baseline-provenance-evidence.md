# Clean baseline provenance remediation

- remediation_task_id: `T-1788067569565-1e5b45ac`
- source_task_id: `T-1788065933399-2b3cead8`
- target_implementation_commit: `2a6906d4883dbc102955179479a1bd7fdb92cff9`
- isolated_worktree: `C:\git_work\callwarden_clean_baseline_20260830`
- isolation: `git status --short` empty at test start; no shared-tree changes were included

## Attributed tests

The target commit's committed governance suite contains 15 tests. In the clean
worktree it ran as `16 passed / 1 failed`; the one failure is the existing
`test_orphan_claim_recovery_requires_stale_owner_and_preserves_step_state`
stale-claim baseline failure (`task_conflict`, line 230), unrelated to the
writer-lock retry change.

Two ephemeral, uncommitted tests were added only in the isolated worktree to
exercise the target commit's `begin_immediate_with_retry` implementation:

```text
clean_baseline_handoff_lock_retry_acquires_after_contention: ok
clean_baseline_handoff_lock_retry_is_bounded: ok
focused result: 2 passed; 0 failed
full clean governance result: 17 tests, 16 passed; 1 existing baseline failure
```

The ephemeral harness was not copied, staged, or committed to the shared
repository. The shared worktree's separate `18 tests / 17 passed` result is
therefore excluded from this evidence.

## Runtime provenance

Runtime refresh was executed from the same isolated worktree and target commit:

```text
runtime evidence: C:\Users\wanpi\.callwarden\runtime\evidence\20260830-133611-2a6906d4883d-7842712c.json
runtime source HEAD: 2a6906d4883dbc102955179479a1bd7fdb92cff9
daemon PID: 11432
daemon binary: C:\Users\wanpi\.callwarden\runtime\current\cw-daemon.exe
daemon SHA-256: 324c8af97a23051f64ba2e2fbf25ebc46807e53da920113188501da4d7ef76c2
expected SHA-256: 324c8af97a23051f64ba2e2fbf25ebc46807e53da920113188501da4d7ef76c2
Python: C:\Python314\python.exe (3.14)
transport: http
health/ping: passed; returned PID 11432 and matching task DB fingerprint
```

The runtime evidence was generated after stopping the prior daemon instance;
PID 11432 is the live instance verified immediately after refresh.
