# T-1787798421878-4a1626e0 implementation evidence

## Scope

This step implements the daemon `task.next_action` governance projection and its
thin CLI/MCP presentation adapters. The raw `tasks.status` lifecycle remains the
permission gate; clients do not re-compute business state.

Changed files:

- `rust_ext/src/daemon/task_loop/next_action.rs`
- `rust_ext/src/daemon/task_loop/next_action_test.rs`
- `cli/main.py`
- `server/tools/tools_task.py`

## Projection contract

`task.next_action` now returns `lifecycle_status`, `workflow_status`,
`current_role`, `next_role`, `next_action`, `review.state`, and the stable
`blocking_reasons` alias while retaining `blocking_conditions` for compatibility.
When a persisted reviewer verdict is available, `review.verdict_id` and
`review.findings_count` are included. The user-facing stages are:

`queued`, `execution_in_progress`, `remediation_in_progress`,
`review_pending`, `adjudication_pending`, `remediation_pending`,
`applied_pending_close`, `completed`, and `reverted`.

An active lease is surfaced as work in progress even if the legacy raw status is
still `open`; a remediation target is surfaced as `remediation_in_progress`.

## Verification

- `tokenslim run cargo check --manifest-path rust_ext/Cargo.toml --lib` — passed
- `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml task_loop::next_action_test --lib` — 19 passed
- `tokenslim run python -m py_compile cli/main.py server/tools/tools_task.py server/daemon_client.py` — passed
- `tokenslim run git diff --check` — passed (pre-existing CRLF warnings only)

## Boundary

The existing `task.governance_projection.get` daemon handler is owned by
`rust_ext/src/daemon/task_collab.rs`, which is outside this step's frozen target
paths. Its response enrichment is therefore tracked as a separate daemon
projection step; this step does not claim that endpoint has already been
changed.
