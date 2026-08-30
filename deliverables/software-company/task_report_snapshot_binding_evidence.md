# task.report review snapshot binding evidence

- Task: `T-1788047855059-fa334ee0`
- Implementation commit: `3775901`
- Scope: `task.report` authoritative daemon event persistence plus Python CLI/MCP thin pass-through.

## Change

`task.report` now accepts the caller-provided `snapshot_id` and stores it on the
append-only `task_events` row with `reason_code=reported`. The governance
projection already reads the latest task-bound `task_events.snapshot_id`, so a
real snapshot reference can now become `review_input_snapshot`. No snapshot is
generated when the field is omitted.

The Python CLI exposes `task report --snapshot-id`; the MCP
`task_report_step` tool and daemon client forward the same field. These adapters
do not invent or validate a snapshot on behalf of the authority.

## Verification

- `tokenslim run -- cargo test --manifest-path rust_ext/Cargo.toml --no-default-features test_task_report_persists_snapshot_for_governance_projection`
  - `1 passed`
- `tokenslim run pytest -q tests/test_task_report_snapshot.py tests/test_task_verdict_mcp.py`
  - `4 passed`
- `python -m py_compile cli/main.py server/daemon_client.py server/tools/tools_task.py tests/test_task_report_snapshot.py`
  - passed
- `python cw.py task report --help | Select-String -Pattern "snapshot-id"`
  - option present
- `git diff --check` for the scoped files
  - passed

The Rust build emitted pre-existing warnings in unrelated modules. Full
`cargo fmt --check` remains red on pre-existing repository-wide formatting
differences; no repository-wide formatting was applied.

## Governance boundary

This evidence does not submit a Reviewer verdict and does not apply or close
any task. The original review-pending task remains unchanged. An independent
Reviewer must use a real snapshot produced by the snapshot authority and then
submit the task-bound Verdict Ledger through `verdict.submit`.
