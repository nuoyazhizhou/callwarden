# Reviewer PASS identity/provenance remediation evidence

- remediation_task_id: `T-1788045499955-a314fad0`
- source_review_task_id: `T-1788019804377-eb4595d8`
- source_finding: Reviewer BLOCKED because `reviewer_pass` did not require Verdict Ledger `role` or `agent_instance_id`, and lacked a provenance negative matrix/current live deployment evidence.
- implementation_commit: `d685f31` (`[T-1788045499955-a314fad0] harden reviewer identity provenance`)
- changed_paths:
  - `rust_ext/src/daemon/task_collab_lifecycle.rs`
  - `rust_ext/src/daemon/task_collab_tests_core.rs`

## Implementation

`reviewer_pass` now fails closed unless the Verdict Ledger reviewer identity has a non-empty, allowed `role` matching the current handoff identity, and exact non-empty `agent_id`, `agent_instance_id`, `session_id`, and `model_id` matches. The current handoff identity must also contain a non-empty `agent_instance_id`. Historical verdicts/evidence and the source review task were not modified.

## Regression

Command:

```text
tokenslim run cargo test --manifest-path rust_ext/Cargo.toml test_reviewer_pass_requires_task_bound_verdict_before_handoff --lib
```

Result: `1 passed; 0 failed`.

The test covers missing/wrong role, missing/mismatched agent instance, missing/mismatched agent/session/model identity, wrong source step, missing snapshot, missing manifest, wrong workspace, missing current handoff instance, zero-write rejection, and the valid success path.

## Official live runtime

- evidence_path: `C:\Users\wanpi\.callwarden\runtime\evidence\20260830-073205-d685f31a99fa-1e3639d2.json`
- evidence_sha256: `sha256:7E8D3D9DE48CB9EEE668EDF5E68DAA8E629A53BF140485B13895C9B8D91E4668`
- runtime_version: `20260830-073205-d685f31a99fa-1e3639d2`
- git_head: `d685f31a99faa7b80ec2246cfdf35cabe44d1ae1`
- current_daemon_pid: `41868`
- runtime/current/cw-daemon.exe sha256: `262a9f53fcbc3c6d79022de068df2b9acd59779fe280523c112b63fb28859f07`
- daemon ping: `exit_code=0`
- smoke ping: `exit_code=0`
- transport: Windows Named Pipe

The official refresh was rerun with PowerShell Core after the Windows PowerShell host lacked `Get-FileHash`; the successful evidence above records the current PID and binary hash from the same refresh.

## Governance boundary

Executor did not submit a Reviewer verdict, change the source task's historical verdict/evidence, or execute `task.apply`/`task.close`. The remediation task is handed to an independent Reviewer for verification.
