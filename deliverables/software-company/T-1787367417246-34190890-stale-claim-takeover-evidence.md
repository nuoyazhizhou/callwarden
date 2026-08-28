# P0-G stale claim takeover evidence

- Task: `T-1787367417246-34190890`
- Step: `S-1787367417248-343121b4`
- Scope: same-governance-role stale claim takeover and explicit CLI workspace authority instance forwarding.
- Runtime deployment: `20260827-063124-95cbe8572145-3a4abb3b`

## Implementation

- `rust_ext/src/daemon/task_collab.rs`
  - Normalizes executor runtime modes and reviewer aliases to governance roles.
  - Reads the current claim role from the append-only claim event, falling back to the old owner registration for legacy events.
  - Uses the daemon authoritative clock and the 900-second stale threshold.
  - Performs role validation, stale validation, recovery-event append, task claim, and step ownership update in one SQLite transaction.
  - Preserves fresh-owner and cross-role conflicts; preserves old events and step state.
  - Accepts both comma- and semicolon-separated step target-file whitelist projections.
- `cli/main.py`
  - Adds `task next-action --workspace-instance-id` and forwards an explicit value to `task.next_action`.

## Verification

Commands run from `C:\git_work\callwarden`:

1. `tokenslim run cargo check --manifest-path rust_ext/Cargo.toml` — passed; existing warnings only.
2. `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml same_role` — passed: stale same-role takeover and fresh same-role conflict.
3. `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml test_stale_claim_cannot_be_taken_over_by_different_role` — passed.
4. `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml orphan_claim_recovery` — passed: both protected recovery regressions.
5. `python -m py_compile cli/main.py server/daemon_client.py` — passed.
6. `python cw.py task next-action T-1787367417246-34190890 --workspace-instance-id ws-1 --json` — passed through the refreshed daemon and returned `decision=READY`, `action=CLAIM`.
7. Real Executor claim after deployment — passed; the old claim was taken over by the current same-role Executor session and step `S-1787367417248-343121b4` returned `in_progress`.

The first targeted takeover assertion expected `agent_id` in the historical `actor_identity` field; the implementation correctly preserves the existing peer actor identity (`1000`). The assertion was corrected and the rerun passed.

## Boundaries

- No direct production SQLite governance write was used.
- No old identity/session was impersonated.
- No task apply/close was executed.
- Commit hash is intentionally recorded after `task.report` by the Executor VCS handoff procedure.
