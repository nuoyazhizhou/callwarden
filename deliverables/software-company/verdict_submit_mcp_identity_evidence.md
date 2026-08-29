# T-1788046887458-b0ad9b68 evidence

## Scope

This atomic remediation fixes only the MCP `submit_verdict` adapter. It now
builds the daemon-native structured `identity` object, including
`agent_instance_id`, and converts the documented JSON-string `clause_results`
and `findings` inputs to arrays before calling `verdict.submit`.

It does not write SQLite directly, change the daemon `reviewer_pass` gate,
modify historical verdicts/evidence, or apply/close any task.

## Implementation

- Implementation commit: `57ab51e4d2b1dedda8c9c5e80b9fefd5a60cd996`
- Changed adapter: `server/tools/tools_collab.py`
- Added regression: `tests/test_task_verdict_mcp.py`

The adapter continues to route through `route_rpc(..., "GOVERNANCE_WRITE")`.
The daemon remains responsible for task, contract, snapshot, identity, lease,
fencing, and Verdict Ledger validation.

## Verification

```text
tokenslim run pytest -q tests/test_task_verdict_mcp.py tests/test_task_verdict_cli.py
7 passed
```

The focused regression proves that a complete Reviewer identity is forwarded
as a nested native `identity` object, that `agent_instance_id` is preserved,
that JSON-array fields are decoded before RPC, and that a non-array finding is
rejected before any RPC call.

An unrelated broader collection check was not used as acceptance evidence:
`tests/test_http_governance_error_cutover.py` currently fails during collection
with `ValueError: methods 不能为空`.

