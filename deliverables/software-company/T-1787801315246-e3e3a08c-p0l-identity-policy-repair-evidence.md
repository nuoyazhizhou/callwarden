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

The daemon build was deployed successfully before review:

- receipt: `C:\Users\wanpi\.callwarden\runtime\evidence\20260830-130537-d32326a89041-5e91f929.json`
- receipt_sha256: `sha256:3DE7C6E9594A3A8E8C06A4F431EE38DB02E52649BFD1CA560B80B86ECFE9E02C`
- refresh_status: `passed`
- route probe: non-allowlisted task returned `E_P0L_POLICY_REPAIR_TASK_NOT_ALLOWED`; no mutation was attempted

## Projection parity after the follow-up fix

The previous release exposed a second governance defect: `task.next-action` rejected
the missing policy while `governance-projection` still advertised `READY/CLAIM`.
The follow-up overlay now derives both views from the same daemon policy resolver.
After the second controlled release, both live endpoints for this exact task returned:

- `lifecycle_status=in_progress`
- `workflow_status=governance_blocked`
- `next_role=adjudicator`
- `next_action=resolve_identity_policy`
- `decision=BLOCKED`, `action=BLOCKED`
- `identity_policy=null`, `identity_policy_status=unresolved`
- blocking reason: contract revision has no parseable `identity_policy`

The live daemon was verified at
`C:\Users\wanpi\.callwarden\runtime\current\cw-daemon.exe` (PID 9312,
SHA-256 `605583AE2BCB9CEA8766F373C385919F7CBD93F159617A9D4FCEBBA1EE97795A`).
The refresh receipt is
`C:\Users\wanpi\.callwarden\runtime\evidence\20260830-132334-79197a608f4d-1eff7d10.json`,
with receipt SHA-256
`sha256:2BCE1882684B0139A74D8C91CD790E9F2FB5392AB7D77AA4F659B221DC13CD8E` and
`status=passed`.

This proves the deadlock is now explicit and actionable, not silently claimable.
The live contract remains unchanged (`identity_policy=null`) because no authorized
adjudicator Role Worker mutation was executed from this worktree. The one-time
allowlisted repair RPC is the remaining controlled operation; it must be invoked
with the enrolled local worker credential in memory. No raw credential was read,
stored, or emitted, and no direct SQLite fallback was used.
