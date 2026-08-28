# T-1787798421878-4a1626e0 test evidence

## Coverage

The existing `task_loop::next_action_test` suite now asserts the user-facing
projection for these transitions:

- unclaimed `open` task: `queued`
- active lease: `execution_in_progress`
- `review` with no verdict: `review_pending` / `review.pending`
- valid reviewer BLOCKED: `remediation_pending` / `review.blocked`, source verdict id and finding count
- valid reviewer PASS: `adjudication_pending` / `review.passed`, source verdict id
- `closed`: `completed`

The tests also retain the existing no-write, workspace binding, lease-token
non-disclosure, and fail-closed contract assertions.

## Verification commands

- `tokenslim run cargo test --manifest-path rust_ext/Cargo.toml task_loop::next_action_test --lib` — 19 passed
- `tokenslim run python -m py_compile cli/main.py server/tools/tools_task.py server/daemon_client.py` — passed
- `tokenslim run git diff --check` — passed (pre-existing CRLF warnings only)
