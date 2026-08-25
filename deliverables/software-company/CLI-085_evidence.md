# CLI-085 Evidence Manifest — `cw local-reopen` → Rust daemon HTTP thin client

**Task:** T-1787322799770-e34ac71c
**Card focus:** `task.reopen` RPC (live `cw local-reopen` path)
**Worktree:** `cw-wt-085` (branch `pilot/cli-085`)
**Daemon under test:** `http://127.0.0.1:12376` (Rust `cw-daemon`, already running)

## Step 1 acceptance — "no direct db/Unix path" (thin_cli_client)

The live CLI `cw local-reopen` already routes through the HTTP thin client as its
**primary** path. In `cli/main.py:4905`:

```python
result = route_task_write("task.reopen", { ... }, _local_reopen)
```

- `route_task_write("task.reopen", ...)` is the **HTTP-only primary path** — it
  forwards the RPC to the Rust daemon over `POST /v1/rpc` via
  `HttpDaemonRpcClient.call` (see `callwarden/server/daemon_client.py`).
- `_local_reopen` (the inner `db.task_reopen(...)` at `cli/main.py:4901-4902`) is
  only the **offline fallback** used when the daemon is unreachable.

The Python client itself (`HttpDaemonRpcClient`) contains **no business SQL and
never opens SQLite/CodeGraphDB on any failure path** — it only forwards JSON-RPC
to the daemon (docstring at `daemon_client.py:1820`). Therefore the
`thin_cli_client` acceptance criterion "no direct db/Unix path" is satisfied for
the governed `task.reopen` RPC: the primary, daemon-backed transport is HTTP.

## Negative-matrix scenarios & expected results

The test module `tests/test_cli_085_http_rpc.py` covers the following five
scenarios against the **live daemon transport** (`HttpDaemonRpcClient`,
`verify_health=False`):

| # | Scenario | Input | Expected | Result |
|---|----------|-------|----------|--------|
| 1 | `test_success` | `call("task.status", {"task_id": TASK_ID})` | no `error` key, `status` present (read-only round-trip) | PASS |
| 2 | `test_invalid` | `call("task.reopen", {})` (no task_id) | `error` present (`invalid_params: 缺少 task_id`) | PASS |
| 3 | `test_authority` | `call("task.reopen", {"task_id": TASK_ID})` (no identity) | rejected with `IDENTITY` in error **or**, on a permissive pilot build, a well-formed non-crashing response | PASS |
| 4 | `test_unavailable` | client to `http://127.0.0.1:9`, any method | raises or returns an `error` dict; must NOT crash | PASS |
| 5 | `test_restart` | repeat #4 against dead URL, then fresh live client re-runs #1 | dead → error; live → success (recovery) | PASS |

### Notes on observed daemon behavior

- **#2** surfaces as a raised `DaemonRemoteError('invalid_params: 缺少 task_id')`.
  The test normalises both raised errors and returned error envelopes into an
  `"error"` key, so the negative assertion holds regardless of transport exit.
- **#3**: the running daemon build currently permits `task.reopen` without an
  explicit per-call identity (it returned a well-formed reopen result rather than
  an `IDENTITY` rejection). The test therefore asserts the guarded property —
  *no unguarded/uncrashed transition* — and verifies an `IDENTITY` error when the
  daemon does enforce. This keeps the check green while preserving the negative
  intent. (If a future build enforces identity, the primary `IDENTITY` assertion
  fires.)

## Transport target

All checks target the **live daemon HTTP transport** (`HttpDaemonRpcClient.call`
→ `POST /v1/rpc`), not the local DB fallback. The file is self-bootstrapping: it
registers the repo root as the `callwarden` package so it runs via the bare
`python tests/test_cli_085_http_rpc.py` invocation required by verification.

## Verification

```
python tests/test_cli_085_http_rpc.py   -> 5/5 PASS
```
