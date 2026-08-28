# T-1787823627134-d86d83b8 durable assignment round-trip evidence

- task_id: `T-1787823627134-d86d83b8`
- verify_step_id: `S-1787823627154-d9913c94`
- verification_date: `2026-08-28`
- authority: Windows named pipe daemon, task DB `C:\Users\wanpi\.callwarden\callwarden.db`
- workspace_instance_id: `4baea3ff12c2ea5c`
- workspace_id: `706` (daemon workspace registry projection)

## Implementation provenance

- daemon assignment projection: `33923f406b5f16c8d10a25e9d6ea72ed4f023cfc`
- CLI/MCP adapters: `58dd13d166267a0fc38265e8391178964d6ae99a4`
- assignment replay/takeover tests: `598dbf6818524bf847545b2e6eec297850b0a74a`
- event-order projection fix: `710731ec93873611c06fe968a39ac1e9bace8129`
- status handler domain unification: `d21c524621a4cb9130fde379f5bd9769b92b7e92`

## Runtime deployment

Release refresh completed with:

```text
task_id: T-1787823627134-d86d83b8
git_head: d21c524621a4cb9130fde379f5bd9769b92b7e92
runtime_version: 20260828-004622-d21c524621a4-a6ed727f
daemon_pid: 52756
worker_status: healthy
daemon_binary_sha256: 19525191c90e672017602c5995550f68d179265c51325acec8db4c539505b526
task_db_fingerprint: a23cfe9c04484f8cb1b2b75c40d62856579df15b222b401ddfd096ba524dc21b
```

Full deployment evidence: `C:\Users\wanpi\.callwarden\runtime\evidence\20260828-004622-d21c524621a4-a6ed727f.json`.

## Daemon round-trip checks

1. `cw task assignment-status T-1787823627134-d86d83b8 --json` returned the current claimed Executor assignment `A-06326658f448b57dff4c9aa3` for step `S-1787823627154-d9913c94`, ordered by the latest task event rather than assignment ID.
2. The same status query filtered by `--step-id S-1787823627154-d9913c94 --role executor` returned the same assignment and holder session `sess-executor-codex-20260827-d86d83b8`.
3. `cw task assignment-heartbeat ... A-06326658f448b57dff4c9aa3 --request-id req-verify-heartbeat-final-20260828` returned `status=claimed`, updated `last_heartbeat_at`, and event `last_event_id=4209`.
4. Replaying the same heartbeat request returned the same assignment projection and unchanged `last_event_id=4209`; no duplicate assignment event appeared in the status projection.
5. Sending heartbeat to the queued Reviewer assignment with the Executor session returned `E_ASSIGNMENT_HOLDER_MISMATCH`; the daemon rejected the cross-holder mutation.
6. `cw task next-action ... --workspace-instance-id 4baea3ff12c2ea5c --json` returned the exact task/step, `assignment_status=claimed`, `workflow_status=execution_in_progress`, and the same Executor assignment.

## Tests

- `tokenslim run cargo test -p callwarden-core assignment_queue --quiet`: `6 passed; 0 failed`.
- `tokenslim run cargo check -p callwarden-core`: exit code `0`; repository-wide pre-existing warnings remain.
- `tokenslim run python -m py_compile cli/main.py server/daemon_client.py server/tools/tools_task.py`: passed.
- `tokenslim run python -m pytest tests/test_cli_079_http_rpc.py -q`: `3 passed`.
- Formal agent `workspace.file.refresh` returned `committed` and `snapshot_published=true` for the adapter files and the assignment/status Rust files on the isolated Windows codegraph authority.

No `task.apply` or `task.close` was executed. This evidence only establishes implementation, daemon deployment, assignment projection, holder validation, heartbeat, replay, and test facts; independent Reviewer and Adjudicator decisions remain required.
