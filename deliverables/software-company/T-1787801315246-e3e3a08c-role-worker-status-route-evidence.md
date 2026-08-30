# P0-L Role Worker status route repair evidence

- Task: `T-1787801315246-e3e3a08c`
- Scope: expose the existing owner-scoped, credential-free `role_worker.status`
  handler through the Rust daemon dispatch path and keep the thin-client read
  independent from workspace snapshot publication.
- Source commit: `91268cdaef8ac847d8ead284c608047139f50b83`

## Verification

- `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml -p callwarden-core --lib test_role_worker_status_is_routed_and_redacts_secrets`
  passed (1 test).
- `tokenslim run python -m pytest -q tests/test_role_worker_status_route.py`
  passed (1 test).
- Runtime refresh receipt:
  `C:\Users\wanpi\.callwarden\runtime\evidence\20260830-161421-f63a7f7e545e-0dabdf7d.json`
  reports `status=passed`, daemon PID `4240`, and deployed binary hash
  `90d657cc4198dcf3b76895e17408410916a1ec5695842baa61380334bd4503c4`.
- Real repo-local Python HTTP round-trip returned `active` for both
  `cw-reviewer-p0j-v1` (`reviewer`) and `cw-adjudicator-p0j-v1`
  (`adjudicator`). The probe only recorded role/status/count; it did not print
  or persist credential material.
- The Adjudicator `credentials.bin` ACL was checked after hardening the exact
  file path: two explicit entries (current user and `NT AUTHORITY\\SYSTEM`),
  no inherited entries, `AreAccessRulesProtected=True`.

No live SQLite write, task mutation, verdict, lease mutation, apply, close, or
12-card import was performed by this repair.
