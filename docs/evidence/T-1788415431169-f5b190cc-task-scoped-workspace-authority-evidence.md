# Task-scoped workspace authority evidence

- Task: `T-1788415431169-f5b190cc`
- Source commit: `323a034ca742d63f3f721284828ad4970f408e9a`
- Runtime receipt: `C:\Users\wanpi\.callwarden\runtime\evidence\20260903-142407-323a034ca742-d69e9837.json`
- Runtime receipt SHA-256: `3080A4311873D1C277265FE91A91429F5542F69330500AEA4A3DF97659CE1386`

## Root cause and repair

For HTTP task and lease RPCs, `server/daemon_client.py` had injected the
legacy active numeric workspace ID even when the request already contained a
`task_id`.  On a host that had another workspace active, that numeric ID could
refer to a different project (workspace `10`) than the immutable task binding
(workspace `1`, instance `4baea3ff12c2ea5c`).  The Rust daemon correctly
rejected that explicit mismatch with `E_WORKSPACE_AUTHORITY_MISMATCH`.

The repair makes a nonblank `task_id` the generic task-scoped discriminator:
the Python thin client does not inject a numeric active-workspace ID for any
`task.*` or `lease.*` request that identifies a task.  The daemon derives the
numeric authority from `task_workspace_bindings`; callers that explicitly pass
a mismatched workspace remain fail-closed.  Workspace-scoped requests without
a task ID retain the existing numeric injection behavior.

## Verification

1. `tokenslim run python -m pytest tests/test_task_scoped_workspace_authority.py tests/test_cli_044_http_rpc.py -q`
   completed with `5 passed`.
2. `tokenslim run cargo test test_lease_` in `rust_ext` completed with
   `14 passed`; this includes the binding-required and explicit-workspace-
   mismatch fail-closed tests.
3. `git diff --check` completed successfully before the source commit.
4. The supported Windows PowerShell refresh completed successfully and its
   receipt is bound above.  The initial TokenSlim-wrapped refresh was rolled
   back before deployment because that wrapper could not resolve
   `Get-FileHash`; it did not switch the live runtime.  The direct supported
   `powershell.exe -File scripts/refresh_shared_runtime.ps1` invocation then
   deployed the committed source successfully.
5. Fresh runtime health returned daemon commit
   `323a034ca742d63f3f721284828ad4970f408e9a` and a healthy worker.
6. The affected task `T-1788392053931-05b8a0e4`, bound to workspace `1`, was
   queried through both `cw lease status ... --json` and the shared
   `server.daemon_client.route_rpc("lease.status", ...)` path.  Both returned
   its released reviewer lease rather than
   `E_WORKSPACE_AUTHORITY_MISMATCH`; no lease, task lifecycle, verdict, or
   historical evidence was mutated.

## Scope and follow-up

Only `server/daemon_client.py` and its targeted Python regression test changed
in the source commit.  This task does not apply or close
`T-1788392053931-05b8a0e4`; a properly independent Adjudicator must perform
that separate finalization after rechecking its existing reviewer verdict and
lease requirements.
